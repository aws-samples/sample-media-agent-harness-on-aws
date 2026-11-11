"""One place that builds every Bedrock model in the harness.

Why this exists
---------------
`BedrockModel(...)` was constructed at ten sites across six modules, each passing only
`model_id` and a boto config. Two consequences, both measured:

1. `max_tokens` was never set, so generation ran to the service default. A multi-node
   DAG turn died mid-plan with `MaxTokensReachedException`, and because nothing was
   yielded on that path the UI showed a step indicator for an hour.

2. Prompt caching was never enabled. A fresh session answering "Reply with just: ok"
   with ONE cycle and ZERO tool calls cost **26,507 input tokens**. That request had no
   gateway tools registered at all, so the floor is the system prompt (~1,900 tokens
   measured) plus the harness's own 44 builtin tool schemas (43,074 characters of JSON,
   which tokenises far worse than prose). Federated gateway schemas are additional on
   top whenever a session has a gateway registered. The floor is paid on every model
   call, and an agentic turn makes several.

   Measured after enabling caching, same probe: InputTokenCount 2,
   CacheWriteInputTokenCount 26,505 on the first call and CacheReadInputTokenCount
   26,505 on the next — the prefix is written once and then read, and reads bill at a
   fraction of fresh input.

Configuring ten call sites individually is how they drift apart, so they share this
factory instead: fix the default once and every agent inherits it.

Caching notes
-------------
`cache_tools` puts a cache point after the tool specs, which is the single largest
stable block. `cache_config(strategy="auto")` lets Strands detect model support and
place further points to maximise coverage; it logs and no-ops on models without
support, so it is safe to set unconditionally.

Cache writes cost more than plain input and entries have a short TTL, so caching only
pays when the prefix is both large and stable and is reused promptly. Keep that in mind
before adding anything volatile ahead of the tools in the request — a prefix that
changes per turn caches nothing and costs extra.

That is exactly why the cache point goes after the BUILTIN tools rather than after all
of them: see StablePrefixBedrockModel. Strands would otherwise place it at the very end,
so narrowing the federated tool set per turn (which is the point of gateway semantic
search) would invalidate the whole prefix and turn cheap reads into a write every turn.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from strands.models import BedrockModel

try:  # CacheConfig is the modern replacement for the deprecated cache_prompt flag.
    from strands.models.model import CacheConfig
except Exception:  # noqa: BLE001 - older strands: fall back to tool-only caching
    CacheConfig = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

__all__ = ["build_model", "DEFAULT_MAX_TOKENS", "caching_enabled", "max_tokens_for",
           "output_ceiling", "StablePrefixBedrockModel", "is_federated_tool_name",
           "reasoning_effort", "reasoning_request_fields", "supports_reasoning_effort",
           "ToolResultImageHoistBedrockModel", "hoist_tool_result_images", "rejects_tool_result_images",
           "HarnessBedrockModel", "drop_prior_turn_reasoning", "is_turn_start",
           "place_history_cache_point"]

# Generous enough for a multi-step plan plus its written summary, bounded so a runaway
# generation fails fast instead of burning a whole context window.
DEFAULT_MAX_TOKENS = 16384

_TRUTHY = {"1", "true", "yes", "on"}

# AgentCore Gateway namespaces federated tools as `target___tool`; the harness's own
# builtins are plain names. That separator is therefore the builtin/federated boundary.
_FEDERATION_SEPARATOR = "___"


def is_federated_tool_name(name: str) -> bool:
    """Whether a tool comes from a gateway rather than the harness itself."""
    return _FEDERATION_SEPARATOR in (name or "")


def is_turn_start(msg: Any) -> bool:
    """Whether ``msg`` opens a turn: a ``user`` message that carries no tool result.

    A message holding tool results continues the current turn's tool cycle even when it also
    carries text -- per-call context folded in after the results (``ContextInjector``, which
    the main agent uses for retrieved memory) must not make it look like a new user ask, or the
    in-progress cycle's reasoning would be dropped and the turn's cache boundary would move.
    """
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return False
    blocks = msg.get("content") or []
    return not any(isinstance(b, dict) and "toolResult" in b for b in blocks)


def _cache_point_of(msg: Any) -> Optional[dict]:
    for b in (msg.get("content") or []) if isinstance(msg, dict) else []:
        if isinstance(b, dict) and isinstance(b.get("cachePoint"), dict):
            return b["cachePoint"]
    return None


def place_history_cache_point(messages: list) -> list:
    """Add ONE cache point at the end of the newest STABLE turn prefix. Mutates ``messages``.

    Strands (``cache_config strategy="auto"``) caches only at the end of the LAST user message,
    and Anthropic looks back just ~20 content blocks from a cache point for an earlier entry. So
    the first call of turn N (whose prefix through turn N-1's prompt was written in turn N-1)
    hit only if turn N-1 was short, and after a long tool-using turn the whole history was
    re-written (measured 29k->95k tokens of cache WRITE per later turn, TTFT 6.5-9.7s).

    The point marks a prefix that is byte-identical from call to call:

    * first call of a turn (the last message IS the turn's prompt): the END of the previous
      turn's prompt message -- exactly where turn N-1 wrote its first entry -- so the read is
      exact rather than left to the look-back window;
    * later calls of the turn (a tool cycle is in progress): the end of THIS turn's prompt, the
      entry this turn's first call wrote.

    Budget: Bedrock allows four cache points per request; with the tools point, one system
    point and the automatic last-message point this is the fourth, and never more than one is
    added. Nothing is added unless the request already carries the automatic message point (so
    caching off / unsupported models are untouched), and never directly after a non-PDF
    document block (Bedrock rejects that), mirroring Strands' own placement rule.
    """
    if not messages:
        return messages
    auto = _cache_point_of(messages[-1])
    if auto is None:
        return messages
    starts = [i for i, m in enumerate(messages) if is_turn_start(m)]
    if not starts:
        return messages
    last = len(messages) - 1
    if starts[-1] == last:
        if len(starts) < 2:
            return messages
        target = starts[-2]
    else:
        target = starts[-1]
    msg = messages[target]
    content = list(msg.get("content") or [])
    if not content or _cache_point_of(msg) is not None:
        return messages
    tail = content[-1]
    if isinstance(tail, dict) and "document" in tail and (tail["document"] or {}).get("format", "") != "pdf":
        return messages
    content.append({"cachePoint": dict(auto)})
    messages[target] = {**msg, "content": content}
    return messages


def drop_prior_turn_reasoning(messages: list) -> list:
    """Remove ``reasoningContent`` blocks from every assistant message BEFORE the current turn.

    The current turn starts at the last user message that is not purely tool results; reasoning inside it is kept,
    because a model resuming its own tool-use cycle may need its thinking (Anthropic: thinking blocks from previous
    turns may be omitted, the in-progress tool cycle's must be kept). Older reasoning is dead weight for every model
    -- and POISON across families: reasoning is provider-encrypted (``redactedContent`` / signed ``reasoningText``),
    so a session that ran on GPT-6.1-Sol and then switched to Claude failed every turn with ``Invalid data in
    redacted_thinking block`` (staging session 3d82d059). An assistant message left empty keeps a one-word text so
    the alternation stays valid. Operates on the formatted request copy; the agent's own history is untouched.
    """
    turn_start = max((i for i, m in enumerate(messages) if is_turn_start(m)), default=-1)
    out: list = []
    for i, msg in enumerate(messages):
        if i >= turn_start or not isinstance(msg, dict) or msg.get("role") != "assistant":
            out.append(msg)
            continue
        blocks = msg.get("content") or []
        kept = [b for b in blocks if not (isinstance(b, dict) and "reasoningContent" in b)]
        if len(kept) == len(blocks):
            out.append(msg)
            continue
        out.append({**msg, "content": kept or [{"text": "(done)"}]})
    return out


class HarnessBedrockModel(BedrockModel):
    """The base every harness model builds on: never resends earlier turns' reasoning (:func:`drop_prior_turn_reasoning`),
    and caches the stable history prefix across turns (:func:`place_history_cache_point`).

    Prior-turn reasoning is dropped at the same point for every request, so a turn's earlier history
    is formatted identically on every later turn -- the property the history cache point relies on.

    Seam: strands' private ``_format_bedrock_messages``; a test asserts the installed strands exposes it.
    """

    SEAM_METHOD = "_format_bedrock_messages"

    def _format_bedrock_messages(self, *args: Any, **kwargs: Any) -> Any:
        return place_history_cache_point(
            drop_prior_turn_reasoning(super()._format_bedrock_messages(*args, **kwargs))
        )

    def format_request(self, *args: Any, **kwargs: Any) -> Any:
        # The platform guardrail checks only what a person wrote (model/guardrail.py).
        from model import guardrail as _guardrail  # noqa: PLC0415

        return _guardrail.guard_only_human_text(super().format_request(*args, **kwargs))


class StablePrefixBedrockModel(HarnessBedrockModel):
    """Puts the tools cache point after the BUILTIN tools, not after every tool.

    Strands' `cache_tools` appends the cache point to the end of `toolConfig.tools`,
    which makes the cached prefix cover the federated gateway tools too. That is the
    wrong boundary here: the harness's builtin tools are identical on every request,
    while the federated set changes whenever tool selection narrows to what a request
    needs. With one cache point at the very end, changing a single federated tool
    invalidates the entire prefix — so per-turn narrowing would turn cheap cache reads
    into a full cache write every turn, costing more than not narrowing at all.

    Splitting at the boundary lets both work together: builtins stay cached across
    turns, and the federated tail is free to vary.

    Tools are also REORDERED so the stable ones precede the cache point. A cache point
    only covers what comes before it, and nothing else guarantees the tool registry
    hands them over in that order.

    On the seam
    -----------
    Request formatting is not public API and its name has changed across releases:
    strands 1.33 exposes `_format_request`, later versions `format_request`. Pinning was
    rejected in favour of hooking whichever exists — the requirement is `>=1.13.0`, so a
    build machine can legitimately resolve a different minor than a workstation, which
    is exactly how the first version of this class passed locally and broke in CI.
    `SEAM_METHODS` records the names tried, and a test asserts the installed version
    exposes one of them so a future rename fails with that message instead of an
    AttributeError from inside a request.

    The reposition step is idempotent: it rebuilds from the tool specs alone, so it is
    harmless if a version routes the public method through the private one.
    """

    #: Candidate names for the request-formatting seam, newest convention first.
    SEAM_METHODS = ("format_request", "_format_request")

    def _apply_cache_boundary(self, request: Any) -> Any:
        if not isinstance(request, dict):
            return request
        tool_config = request.get("toolConfig")
        if not isinstance(tool_config, dict):
            return request
        entries = tool_config.get("tools")
        if not isinstance(entries, list) or not entries:
            return request

        specs = [e for e in entries if isinstance(e, dict) and "toolSpec" in e]
        points = [e for e in entries if isinstance(e, dict) and "cachePoint" in e]
        # No tool specs, or strands emitted no tools cache point (caching is off for this
        # model/strategy): there is nothing to reposition. We RELOCATE the point strands
        # already placed rather than inventing one — this both keeps its full type+ttl
        # and means the deprecated ``cache_tools`` and modern ``cache_config.tools_ttl``
        # injection paths are handled identically.
        if not specs or not points:
            return request
        point = points[0]  # strands emits at most one tools cache point

        stable, volatile = [], []
        for entry in specs:
            name = (entry.get("toolSpec") or {}).get("name") or ""
            (volatile if is_federated_tool_name(name) else stable).append(entry)

        # Nothing federated: strands' trailing placement is already correct.
        if not volatile:
            return request
        # Nothing stable: a cache point covering nothing is pointless, and caching a
        # varying set would write on every turn — drop the point.
        if not stable:
            tool_config["tools"] = specs
            return request

        tool_config["tools"] = [*stable, point, *volatile]
        return request

    def format_request(self, *args: Any, **kwargs: Any) -> Any:
        parent = getattr(super(), "format_request", None)
        if parent is None:  # this version uses the private name; nothing calls this
            raise AttributeError("format_request is not provided by this strands version")
        return self._apply_cache_boundary(parent(*args, **kwargs))

    def _format_request(self, *args: Any, **kwargs: Any) -> Any:
        parent = getattr(super(), "_format_request", None)
        if parent is None:  # this version uses the public name; nothing calls this
            raise AttributeError("_format_request is not provided by this strands version")
        return self._apply_cache_boundary(parent(*args, **kwargs))


# Families whose Bedrock integration rejects an `image` block INSIDE a `toolResult` ("This model doesn't
# support the image field for user messages") while accepting the same image as a plain user-message block —
# verified live 2026-10-10 on global.openai.gpt-6.1-sol (vision works; tool-result images do not). Claude
# accepts both. fal's `run_model` returns an MCP ImageContent preview, which Strands maps INTO the toolResult,
# so every fal image step failed on GPT (staging sessions caa62e03, e2fb2692).
_TOOL_RESULT_IMAGE_REJECTING = ("openai.",)


def rejects_tool_result_images(model_id: str) -> bool:
    mid = (model_id or "").lower()
    return any(fam in mid for fam in _TOOL_RESULT_IMAGE_REJECTING)


def hoist_tool_result_images(messages: list) -> list:
    """Move every image out of a user message's toolResult blocks to sibling blocks after them.

    Each toolResult keeps its text (a placeholder note if the image was all it had), and the hoisted images
    follow ALL the message's toolResults, each preceded by a one-line label naming the tool use — so the model
    still SEES the image. Operates on the formatted request copy; the agent's own history is untouched.
    """
    out: list = []
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            out.append(msg)
            continue
        hoisted: list = []
        content: list = []
        for block in msg.get("content") or []:
            tr = block.get("toolResult") if isinstance(block, dict) else None
            if not isinstance(tr, dict) or not any(isinstance(c, dict) and "image" in c
                                                   for c in tr.get("content") or []):
                content.append(block)
                continue
            kept = [c for c in tr["content"] if not (isinstance(c, dict) and "image" in c)]
            images = [c for c in tr["content"] if isinstance(c, dict) and "image" in c]
            n = len(images)
            note = f"[{n} image{'s' if n != 1 else ''} returned by this tool are attached after the tool results]"
            content.append({"toolResult": {**tr, "content": [*kept, {"text": note}]}})
            hoisted.append({"text": f"[image(s) from tool result {tr.get('toolUseId', '')}]"})
            hoisted.extend(images)
        if not hoisted:
            out.append(msg)
            continue
        # toolResult blocks first, then everything else (the hoisted images), as Bedrock expects.
        results = [b for b in content if isinstance(b, dict) and "toolResult" in b]
        rest = [b for b in content if not (isinstance(b, dict) and "toolResult" in b)]
        out.append({**msg, "content": [*results, *rest, *hoisted]})
    return out


class ToolResultImageHoistBedrockModel(HarnessBedrockModel):
    """BedrockModel for families that reject images inside toolResult (see `_TOOL_RESULT_IMAGE_REJECTING`).

    Seam: strands' private `_format_bedrock_messages` (it builds the Converse `messages` from the agent's).
    A test asserts the installed strands exposes it, so a rename fails loudly instead of the fix silently
    stopping — which would only show up as GPT image steps failing again.
    """

    SEAM_METHOD = "_format_bedrock_messages"

    def _format_bedrock_messages(self, *args: Any, **kwargs: Any) -> Any:
        return hoist_tool_result_images(super()._format_bedrock_messages(*args, **kwargs))




# Families that support Bedrock prompt caching. There is no API that reports
# this, so it has to be a list -- which means it needs maintaining when a new
# family ships. The failure it prevents is severe and unhelpful: asking an
# unsupported model to cache returns
#
#   AccessDeniedException ... You invoked an unsupported model or your request
#   did not allow prompt caching
#
# on EVERY turn, with no indication of which model or that caching is the cause.
# The picker offers 40+ models, so this was reachable by an ordinary click.
#
# Claude 3.x is deliberately absent: the 2024 generation predates caching.
_CACHE_CAPABLE = ("anthropic.claude-sonnet-", "anthropic.claude-opus-",
                  "anthropic.claude-haiku-", "anthropic.claude-fable-",
                  "amazon.nova-")


def supports_prompt_caching(model_id: str) -> bool:
    """Whether `model_id` accepts cache points.

    Matches on the family segment so the region prefix (`us.`, `global.`) and
    the version suffix are both irrelevant.
    """
    mid = (model_id or "").lower()
    return any(fam in mid for fam in _CACHE_CAPABLE)


def caching_enabled() -> bool:
    """Prompt caching on unless explicitly disabled (BEDROCK_PROMPT_CACHE=off)."""
    raw = (os.environ.get("BEDROCK_PROMPT_CACHE") or "").strip().lower()
    if not raw:
        return True
    return raw in _TRUTHY


# ---- Reasoning effort (latency lever) --------------------------------------
# Lower effort scales the model's adaptive thinking — and therefore both latency
# and billed thinking tokens — down on EVERY call. On the agentic orchestrator and
# plan nodes (many heavy calls per turn) this is the single biggest untouched speed
# lever once max_tokens and prompt caching are already in place.
_REASONING_EFFORTS = ("low", "medium", "high")

# Schema VERIFIED empirically against the live model (Bedrock Converse, us-west-2,
# 2026-10). What the probe found, and why we send ONLY output_config.effort:
#   - `effort` / `reasoning_effort` as TOP-LEVEL additionalModelRequestFields:
#     REJECTED — "effort: Extra inputs are not permitted".
#   - `thinking:{type:"enabled",budget_tokens:N}`: REJECTED — the model's own error
#     names the lever: 'Use "thinking.type.adaptive" and "output_config.effort"'.
#   - `output_config:{effort:...}` ALONE: ACCEPTED, and this is the actual win —
#     measured on a hard reasoning prompt, effort=low cut total wall time ~17%
#     (30.2s -> 25.0s) and output tokens ~16% (2457 -> 2058) vs sending no field,
#     while holding TTFT at a rock-solid ~0.75s across every trial.
#   - `thinking:{type:"adaptive"}` added ON TOP of effort: ACCEPTED but HARMFUL —
#     it front-loads reasoning, so TTFT spiked to 8-18s on 4 of 5 trials (vs 0.75s)
#     for no better total time. That is a perceived-latency regression, the opposite
#     of the goal, so we DO NOT send the thinking field. Omitting it leaves the
#     model's own default thinking disposition (adaptive) in place at the reduced
#     effort — i.e. thinking is scaled down, never disabled.
# Support is per-GENERATION, verified live via Bedrock Converse output_config.effort
# (us-west-2, probe 2026-10) — NOT a family prefix. The rule the matrix encodes:
# generation >= 4.6 or >= 5 across opus/sonnet/haiku SUPPORTS the field; everything
# <= 4.5 (incl. opus-4-5 / sonnet-4-5 / haiku-4-5), 4.1 and 3.x REJECTS it with a
# botocore ValidationException (HTTP 400) that fails the WHOLE invoke. So this gate is
# mandatory and must stay an explicit allowlist; the earlier `('anthropic.claude-opus-',)`
# prefix was wrong both ways (it let opus-4-5 through and excluded supported sonnet/haiku).
# Same shape/reason as _CACHE_CAPABLE: no API reports support, so it is a maintained list.
# Entries are id SUBSTRINGS so the region prefix (`us.`/`global.`) and the date suffix are
# irrelevant, and the vendor prefix is dropped so a bare model id still matches.
# 'claude-opus-5' matches opus-5 AND opus-5-5; 'claude-sonnet-5' matches sonnet-5 AND
# sonnet-5-5. opus-4-5 / sonnet-4-5 / haiku-4-5 are DELIBERATELY absent (they 400).
_REASONING_EFFORT_CAPABLE = (
    "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8",
    "claude-opus-5",            # opus-5 and opus-5-5
    "claude-sonnet-4-6",
    "claude-sonnet-5",          # sonnet-5 and sonnet-5-5
    "claude-haiku-5-5",
)


def supports_reasoning_effort(model_id: str) -> bool:
    """Whether `model_id` accepts the output_config.effort cap.

    Matches on the id substring (so `us.`/`global.` prefix and the date suffix are
    irrelevant) against the verified-capable allowlist. Support is per-GENERATION
    (>= 4.6 or >= 5 across opus/sonnet/haiku), not per family — opus-4-5 / sonnet-4-5 /
    haiku-4-5 and everything older are excluded because they reject the field (HTTP 400).
    """
    mid = (model_id or "").lower()
    return any(fam in mid for fam in _REASONING_EFFORT_CAPABLE)


def reasoning_effort() -> Optional[str]:
    """Configured reasoning effort from env BEDROCK_REASONING_EFFORT, or None.

    Returns one of ``low|medium|high`` (lower-cased), or None when unset or invalid —
    None means "send no effort field", i.e. today's default adaptive behavior. The
    deployed default is set via the ``BEDROCK_REASONING_EFFORT`` runtimeConfig key
    (RuntimeConfigSecret), so the level is tunable without a code change.
    """
    raw = (os.environ.get("BEDROCK_REASONING_EFFORT") or "").strip().lower()
    return raw if raw in _REASONING_EFFORTS else None


def reasoning_request_fields(model_id: str, effort: Optional[str] = None) -> dict[str, Any]:
    """additionalModelRequestFields that cap `model_id`'s thinking at `effort`.

    Returns ``{}`` (send nothing) unless BOTH an effort level is in play (explicit
    arg, else env) AND the model is in the verified-capable family. The explicit arg
    wins over the env so a specific build site can override the global default.

    Emits ONLY ``output_config.effort`` — NOT a ``thinking`` field. Omitting thinking
    keeps the model's default adaptive thinking (scaled down by effort, never
    disabled) and avoids the 8-18s TTFT front-load that an explicit
    ``thinking:{type:adaptive}`` caused in the live probe. See the block comment above.
    """
    level = effort or reasoning_effort()
    if not level or level not in _REASONING_EFFORTS:
        return {}
    if not supports_reasoning_effort(model_id):
        return {}
    return {"output_config": {"effort": level}}


# ---- Preserved thinking: the history-editing check ------------------------
# Claude Opus 5.5, Sonnet 5.5 and Haiku 5.5 sign each thinking block over the prefix that produced it (the system
# prompt, the tools, every earlier message) and reject a replayed block whose prefix changed - "Invalid `signature`
# in `thinking` block. The block is bound to a different conversation". Enforced by default for AWS accounts created
# on or after 2026-08-31 (every new account, a workshop's included); older accounts are enforced only when a request
# sets the field below. The harness keeps the prefix stable within a turn (turn_context_injector); this field is the
# safety net for any other mid-turn edit: "drop_block" drops the stale blocks and the call proceeds, where the
# default fails the whole turn. Env BEDROCK_THINKING_PREFIX_MISMATCH: drop_block (default) | error (opts an
# unenforced account INTO enforcement - how the sandbox proves the prefix is stable) | off (send nothing).
# Shapes VERIFIED on Bedrock Converse (us-east-1, a new account, 2026-11-09): Opus 5.5 takes block_binding alone;
# Sonnet 5.5 and Haiku 5.5 reject it without thinking.type ("thinking.type: Field required"), so they get
# "adaptive" - their default when the field is omitted, so it changes nothing else.
THINKING_BINDING_BETA = "thinking-binding-controls-2026-08-01"
_THINKING_BINDING_MODES = ("drop_block", "error")
_THINKING_BINDING_MODELS: dict[str, Optional[str]] = {   # id substring -> thinking.type to send alongside, or None
    "claude-opus-5-5": None,
    "claude-sonnet-5-5": "adaptive",
    "claude-haiku-5-5": "adaptive",
}


def thinking_binding_mode() -> Optional[str]:
    """``drop_block`` (default) or ``error`` from env BEDROCK_THINKING_PREFIX_MISMATCH; None for ``off``."""
    from model.defaults import THINKING_PREFIX_MISMATCH  # noqa: PLC0415 - the deploy sets the env from the same file

    raw = (os.environ.get("BEDROCK_THINKING_PREFIX_MISMATCH") or THINKING_PREFIX_MISMATCH).strip().lower()
    return raw if raw in _THINKING_BINDING_MODES else None


def thinking_binding_request_fields(model_id: str) -> dict[str, Any]:
    """additionalModelRequestFields that set the preserved-thinking prefix check's behaviour for ``model_id``.

    ``{}`` for a model without the check (or with the mode ``off``): the beta header and field are sent only to the
    verified models, as an unknown field fails the whole invoke."""
    mode = thinking_binding_mode()
    mid = (model_id or "").lower()
    match = next((fam for fam in _THINKING_BINDING_MODELS if fam in mid), None)
    if not mode or match is None:
        return {}
    thinking: dict[str, Any] = {"block_binding": {"prefix_mismatch_behavior": mode}}
    if _THINKING_BINDING_MODELS[match]:
        thinking = {"type": _THINKING_BINDING_MODELS[match], **thinking}
    return {"anthropic_beta": [THINKING_BINDING_BETA], "thinking": thinking}


def _merge_request_fields(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """``base`` updated with ``extra``: ``anthropic_beta`` lists are unioned and ``thinking`` objects merged, so a
    caller's own beta or thinking setting survives (the caller's ``thinking.type`` wins)."""
    out = dict(base)
    for key, value in extra.items():
        if key == "anthropic_beta":
            out[key] = list(dict.fromkeys([*(out.get(key) or []), *value]))
        elif key == "thinking" and isinstance(out.get(key), dict):
            out[key] = {**value, **out[key], "block_binding": value.get("block_binding", out[key].get("block_binding"))}
        else:
            out[key] = value
    return out


# Families whose output ceiling is BELOW DEFAULT_MAX_TOKENS. Same shape, and the
# same reason, as _CACHE_CAPABLE above: no API reports this, so it is a list that
# needs maintaining, and the failure it prevents is total rather than degraded —
#
#   ValidationException ... The maximum tokens you requested exceeds the model
#   limit of 4096. Try again with a maximum tokens value that is lower than 4096
#
# on EVERY turn, with nothing naming the model as the cause. Observed live in
# sandbox: the model picker defaulted to Claude 3 Haiku and every chat turn died
# this way. The picker offers 40+ models, so it is an ordinary click away.
#
# Only families known to sit below the default belong here. An unlisted model
# keeps the configured cap, so a missing entry degrades to today's behaviour
# while a wrong entry can only ask for LESS than the model allows.
#
# Patterns match the family segment, so the region prefix (`us.`, `global.`) and
# the date/version suffix are both irrelevant. `claude-3-` is spelled out per
# family rather than as a prefix so it cannot swallow `claude-3-5-sonnet` (8192).
_MAX_OUTPUT_TOKENS: dict[str, int] = {
    "anthropic.claude-3-haiku": 4096,
    "anthropic.claude-3-sonnet": 4096,
    "anthropic.claude-3-opus": 4096,
}


def output_ceiling(model_id: str) -> Optional[int]:
    """The model's output cap when it is known to be below our default."""
    mid = (model_id or "").lower()
    for family, cap in _MAX_OUTPUT_TOKENS.items():
        if family in mid:
            return cap
    return None


def max_tokens_for(override: Optional[int] = None, model_id: Optional[str] = None) -> int:
    """Output cap: explicit argument, then BEDROCK_MAX_TOKENS, then the default.

    Clamped to the model's own ceiling when one is known. Bedrock treats an
    over-limit `max_tokens` as a hard ValidationException rather than silently
    capping, so without this a single unlucky model selection breaks every turn.
    `model_id` is optional to keep existing call sites working; they simply get
    the unclamped value, exactly as before.
    """
    if isinstance(override, int) and override > 0:
        requested = override
    else:
        raw = (os.environ.get("BEDROCK_MAX_TOKENS") or "").strip()
        requested = int(raw) if raw.isdigit() and int(raw) > 0 else DEFAULT_MAX_TOKENS

    ceiling = output_ceiling(model_id) if model_id else None
    if ceiling is not None and requested > ceiling:
        logger.info(
            "clamping max_tokens %d -> %d for %s (model ceiling)",
            requested, ceiling, model_id,
        )
        return ceiling
    return requested


def build_model(
    model_id: str,
    *,
    boto_client_config: Any = None,
    max_tokens: Optional[int] = None,
    cache: Optional[bool] = None,
    effort: Optional[str] = None,
    **extra: Any,
) -> BedrockModel:
    """Build a BedrockModel with the harness defaults applied.

    Unknown keyword arguments are forwarded, so a caller needing something specific
    (temperature, guardrails) still gets the shared defaults.

    `effort` caps the model's adaptive thinking (and so its latency) for the
    reasoning-capable family; None falls back to env BEDROCK_REASONING_EFFORT, and
    an unset/invalid value leaves today's default adaptive behavior. See
    reasoning_request_fields for the verified schema and gating.
    """
    kwargs: dict[str, Any] = {
        "model_id": model_id,
        # Pass model_id so the cap is clamped to this model's ceiling.
        "max_tokens": max_tokens_for(max_tokens, model_id),
    }
    if boto_client_config is not None:
        kwargs["boto_client_config"] = boto_client_config

    # Reasoning-effort plumb: merge into any caller-supplied additional_request_fields
    # so a lower effort scales thinking (and latency) down. Built into `extra` BEFORE
    # the construction so BOTH the normal path and the TypeError fallback carry it —
    # the fallback must never silently drop it (it would quietly restore full effort).
    arf = dict(extra.pop("additional_request_fields", None) or {})
    arf.update(reasoning_request_fields(model_id, effort))
    # Preserved thinking: a mid-turn prefix edit drops the stale thinking blocks instead of failing the turn.
    arf = _merge_request_fields(arf, thinking_binding_request_fields(model_id))
    if arf:
        extra["additional_request_fields"] = arf

    # The request's Bedrock guardrail (model/guardrail.py), unless the caller set one itself. Into `extra` for the same
    # reason as effort: the TypeError fallback below carries `extra`, so a guardrail is never silently dropped.
    if "guardrail_id" not in extra:
        from model import guardrail as _guardrail  # noqa: PLC0415

        bound = _guardrail.current()
        if bound:
            extra.update(_guardrail.strands_config(bound))

    use_cache = caching_enabled() if cache is None else bool(cache)
    # A model that cannot cache must not be sent cache options: the tools cache point
    # would be injected for a model that rejects it, so the request is rejected outright
    # rather than degrading.
    if use_cache and not supports_prompt_caching(model_id):
        logger.info("prompt caching unsupported for %s; building uncached", model_id)
        use_cache = False
    if use_cache:
        # The tool specs are the largest stable block; cache them explicitly.
        # ``CacheConfig(tools_ttl=True)`` is the modern replacement for the deprecated
        # model-level ``cache_tools`` flag (which emits a DeprecationWarning on strands
        # >=1.55): it is what INJECTS the tools cachePoint that StablePrefixBedrockModel
        # then repositions after the builtin tools. ``strategy="auto"`` both requests
        # automatic system/message cache points AND gates the tools point to models
        # strands can cache (Claude/anthropic) — the same gate the deprecated flag
        # already passed through once ``cache_config`` was set, so this preserves the
        # existing behavior (tool caching is Claude-only; a Nova id resolves to strategy
        # None and gets no tools point either way).
        if CacheConfig is not None:
            kwargs["cache_config"] = CacheConfig(strategy="auto", tools_ttl=True)
        else:
            # Older strands without CacheConfig: fall back to the (now-deprecated) flag,
            # which the same repositioning seam handles.
            kwargs["cache_tools"] = "default"

    kwargs.update(extra)
    # StablePrefixBedrockModel only changes cache-point PLACEMENT; with caching off it
    # behaves exactly like BedrockModel.
    # A family that rejects toolResult images never caches (no OpenAI id is cache-capable), so the two
    # subclasses never need combining.
    plain: type = ToolResultImageHoistBedrockModel if rejects_tool_result_images(model_id) else HarnessBedrockModel
    cls = StablePrefixBedrockModel if use_cache else plain
    try:
        return cls(**kwargs)
    except TypeError as exc:
        # A strands version without one of the CACHE options must not stop the agent
        # from starting; degrade to an uncached model rather than failing outright.
        # `extra` (incl. any reasoning-effort additional_request_fields) is carried into
        # the rebuild below — so effort is NEVER silently dropped. If BedrockModel itself
        # rejects a field in `extra`, this rebuild raises TypeError again and it is left
        # UNCAUGHT (loud failure), which is the intended behavior.
        logger.warning("model options unsupported (%s); building without caching", exc)
        base: dict[str, Any] = {"model_id": model_id, "max_tokens": kwargs["max_tokens"]}
        if boto_client_config is not None:
            base["boto_client_config"] = boto_client_config
        base.update(extra)
        return plain(**base)
