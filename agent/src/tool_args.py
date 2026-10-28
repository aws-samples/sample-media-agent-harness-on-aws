"""Normalize model-emitted tool arguments before they cross a gateway.

fal video models validate ``duration`` leniently: MiniMax H3-Max declares an integer but accepts
a bare digit string (``"7"`` parses), while Kling declares a STRING enum (``"5"``/``"10"``). What
both reject is a value with a unit or a fraction — ``"7s"``, ``"7 sec"``, ``"6.0"`` — which the
model emits often enough to fail clips at RESULT time with a 422 ``body.duration: unable to parse
string as an integer`` (staging 9fb4e17b: gen_shot2/gen_shot8 sent ``"7s"``; the six clips sent
``"7"`` succeeded). So normalize such a value to a bare digit STRING: valid for both families.
Never convert to ``int`` — that would break Kling's string enum.

Applied on the path Strands actually invokes (``_ResilientMCPMixin._base_stream_once``) and on
``GatewayConnection.call_tool_sync``. Pure, non-mutating, depth-bounded; leaves ``"auto"``, ints,
floats, and any non-duration-shaped string untouched.
"""
from __future__ import annotations

import re
from typing import Any

# "7", "7s", "7 s", "7sec", "7 secs", "7 second(s)", "7.0", "7.0s" — whole-second values only.
_DURATION_RE = re.compile(r"^\s*(\d+)(?:\.0+)?\s*(?:s|sec|secs|second|seconds)?\s*$", re.IGNORECASE)


def normalize_duration(arguments: Any, _depth: int = 0) -> Any:
    """Return ``arguments`` with any whole-second string ``duration`` reduced to its digits."""
    if _depth > 4 or not isinstance(arguments, dict):
        return arguments
    out = {}
    for k, v in arguments.items():
        if k == "duration" and isinstance(v, str):
            m = _DURATION_RE.match(v)
            out[k] = m.group(1) if m else v
        elif isinstance(v, dict):
            out[k] = normalize_duration(v, _depth + 1)
        else:
            out[k] = v
    return out


# fal hosts MiniMax H3-Max at ``minimax/h3-max/...`` — no ``fal-ai/`` prefix. The prefixed id
# routes to the unrelated app ``fal-ai/minimax``: the job reports COMPLETED within seconds and
# every result fetch 404s (session 36685185, three clips lost). Only this exact prefix is
# rewritten; every other endpoint id, including other ``fal-ai/minimax/...`` models, is untouched.
_ENDPOINT_KEYS = ("endpoint_id", "model_id", "application")
_WRONG_H3_MAX_PREFIX = "fal-ai/minimax/h3-max/"
_RIGHT_H3_MAX_PREFIX = "minimax/h3-max/"


def normalize_endpoint_ids(arguments: Any, _depth: int = 0) -> Any:
    """Return ``arguments`` with a ``fal-ai/minimax/h3-max/...`` endpoint id rewritten to
    ``minimax/h3-max/...`` under ``endpoint_id`` / ``model_id`` / ``application``.

    Pure, non-mutating, depth-bounded like :func:`normalize_duration`; returns the SAME object
    when there is nothing to rewrite.
    """
    if _depth > 4 or not isinstance(arguments, dict):
        return arguments
    out = {}
    changed = False
    for k, v in arguments.items():
        nv = v
        if k in _ENDPOINT_KEYS and isinstance(v, str):
            s = v.strip()
            if s.startswith(_WRONG_H3_MAX_PREFIX):
                nv = _RIGHT_H3_MAX_PREFIX + s[len(_WRONG_H3_MAX_PREFIX):]
        elif isinstance(v, dict):
            nv = normalize_endpoint_ids(v, _depth + 1)
        out[k] = nv
        changed = changed or nv is not v
    return out if changed else arguments


def _normalized_endpoint(value: str) -> str:
    s = value.strip().strip("/")
    if s.startswith(_WRONG_H3_MAX_PREFIX):
        s = _RIGHT_H3_MAX_PREFIX + s[len(_WRONG_H3_MAX_PREFIX):]
    return s


def submitted_endpoint(arguments: Any) -> str:
    """The endpoint id a submit call names at its top level (``endpoint_id`` / ``model_id`` /
    ``application``), normalized like :func:`normalize_endpoint_ids`; ``""`` when it names none."""
    if not isinstance(arguments, dict):
        return ""
    for k in _ENDPOINT_KEYS:
        v = arguments.get(k)
        if isinstance(v, str) and v.strip():
            return _normalized_endpoint(v)
    return ""


def endpoint_matches_plan(endpoint: str, planned: Any) -> bool:
    """Whether ``endpoint`` is one of the ``planned`` ids, or under one of them at a path boundary
    (planned ``minimax/h3-max`` admits ``minimax/h3-max/image-to-video``; ``fal-ai/minimax`` does
    NOT admit ``fal-ai/minimax-music``). No planned ids, or no endpoint, is no constraint."""
    ids = [_normalized_endpoint(p) for p in (planned or ()) if isinstance(p, str) and p.strip()]
    if not ids or not endpoint:
        return True
    e = _normalized_endpoint(endpoint)
    return any(e == p or e.startswith(p + "/") for p in ids)


# M2C alpha.20261014a's default ``run_inference`` model (Claude Sonnet 5.5) requires a thinking field, and M2C's
# registry sends it ``disabled``, which Bedrock rejects ("send between_tools instead"); every call that leaves
# ``thinking`` out therefore fails (staging: 24 bedrock-inference runs in a day, 2026-10-14). ``adaptive`` works
# (verified on run 1f49e26d), and M2C drops it for models without thinking. Ingestion sets it from its config;
# this covers the agent calling the tool itself.
_INFERENCE_TOOL = "bedrock-inference___run_inference"


def declared_properties(tool_spec: Any) -> frozenset:
    """Input property names a tool spec declares (bare JSON schema or Strands' ``{"json": {...}}``)."""
    schema = tool_spec.get("inputSchema") if isinstance(tool_spec, dict) else None
    if not isinstance(schema, dict):
        return frozenset()
    inner = schema.get("json") if isinstance(schema.get("json"), dict) else schema
    props = inner.get("properties") if isinstance(inner, dict) else None
    return frozenset(props) if isinstance(props, dict) else frozenset()


def default_inference_thinking(tool_name: str, arguments: Any, declared: Any) -> Any:
    """``thinking: "adaptive"`` for an M2C ``run_inference`` call that omits it, when the schema declares it.

    Never overrides a value the caller chose; a schema without ``thinking`` (an older bundle) or any other
    tool is left alone. Non-mutating.
    """
    if not (isinstance(tool_name, str) and tool_name.endswith(_INFERENCE_TOOL)):
        return arguments
    if not isinstance(arguments, dict) or "thinking" in arguments or "thinking" not in (declared or ()):
        return arguments
    return {**arguments, "thinking": "adaptive"}



_VOICE_LIST_TOOL = "text-to-speech___list_voice_collection"
_VOICE_REGISTER_TOOL = "text-to-speech___register_voice"
# The tool's own maximum (schema: "default 50, max 100"). It has no next token, no offset and no
# total, and lists OLDEST first, so anything short of the maximum drops the newest voices -- the
# user's own clones (staging 2026-10-20: 50 of 54 returned, every agent_olive_voice* missing).
VOICE_LIST_LIMIT = 100


def default_voice_listing(tool_name: str, arguments: Any, declared: Any) -> Any:
    """Every ``list_voice_collection`` call asks for the tool's maximum page, whatever limit it chose.

    A lower limit is raised to ``VOICE_LIST_LIMIT``: with no pagination, a smaller page is never
    "the first page", it is a silent cut-off of the newest voices. Leaves a schema that declares
    no ``limit`` alone. Non-mutating.
    """
    if not (isinstance(tool_name, str) and tool_name.endswith(_VOICE_LIST_TOOL)):
        return arguments
    if not isinstance(arguments, dict):
        return arguments
    if declared and "limit" not in declared:
        return arguments
    current = arguments.get("limit")
    if isinstance(current, int) and not isinstance(current, bool) and current >= VOICE_LIST_LIMIT:
        return arguments
    return {**arguments, "limit": VOICE_LIST_LIMIT}


def voice_list_may_be_truncated(tool_name: str, payload: Any) -> bool:
    """True when a ``list_voice_collection`` result filled a whole maximum page.

    The tool reports only ``count`` (voices returned), never a total or a next token, so a full
    page is the only sign that more voices exist than it can return.
    """
    if not (isinstance(tool_name, str) and tool_name.endswith(_VOICE_LIST_TOOL)):
        return False
    if not isinstance(payload, dict):
        return False
    voices = payload.get("voices")
    count = payload.get("count", len(voices) if isinstance(voices, list) else None)
    return isinstance(count, int) and count >= VOICE_LIST_LIMIT


def register_voice_missing_gender(tool_name: str, arguments: Any) -> bool:
    """True for a ``register_voice`` call that carries no gender.

    ``gender`` is optional to the tool, but ``list_voice_collection``'s gender filter drops every
    voice without one -- 7 of 8 clones in staging were untagged and never surfaced for a
    "female voice" search. There is no update-voice tool, so the tag must go on at registration.
    """
    if not (isinstance(tool_name, str) and tool_name.endswith(_VOICE_REGISTER_TOOL)):
        return False
    if not isinstance(arguments, dict):
        return False
    gender = arguments.get("gender")
    return not (isinstance(gender, str) and gender.strip())
