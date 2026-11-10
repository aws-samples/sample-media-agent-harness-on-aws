"""The platform's Bedrock guardrail, applied by ``model.factory.build_model`` to every model built for a request.

ONE guardrail for the whole platform, chosen by an admin in Settings -> Safety (the proxy writes
``<RUNTIME_ARN_PARAM_PATH>/GUARDRAIL_SELECTED``). NONE until an admin picks one: the deploy creates the harness's
light guardrail so there is one to pick, but never applies it by itself (1.2.2: a default that blocks ordinary work
is worse than none). The Main and Task agents :func:`bind` it at the start of each request; the planning and render
runtimes never do (their output is internal JSON, not prose). So dispatched work, plan nodes and external custom-agent
calls are covered without anything forwarding it. Design: docs/superpowers/specs/2026-11-09-bedrock-guardrails-design.md.

Input checks cover only what a person wrote (:func:`bind_human_text`, :func:`guard_only_human_text`), never the
harness's injected context or an orchestrator's instructions: the Task agent binds the guardrail only for an external
custom-agent call (a person's prompt). Dispatches and plan nodes started from a chat were checked at the orchestrator.

Read from SSM at most once a minute, so an admin's change applies within about a minute. A read failure keeps the last
value read; with none yet, the request runs unguarded and the failure is logged as an error.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

SELECTED_NAME = "GUARDRAIL_SELECTED"
_TTL_SECONDS = 60.0

# A guardrail id (never an ARN: an ARN could name another account's guardrail) and a PUBLISHED version (never DRAFT).
_ID = re.compile(r"^[a-z0-9]{1,64}$")
_VERSION = re.compile(r"^[0-9]{1,8}$")

_current: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar("current_guardrail", default=None)
# The words a PERSON wrote for this request (the chat message, an external caller's prompt). Only these are checked
# on input: the harness's own context (remembered memories, project rules) and an orchestrator's instructions to a
# sub-agent read like instructions, and the prompt-attack filter blocked ordinary turns on them (prod, 2026-11-09).
_human_texts: contextvars.ContextVar[tuple] = contextvars.ContextVar("guardrail_human_texts", default=())
_lock = threading.Lock()
_cache: dict = {"at": None, "value": None, "known": False}
_client = None


def normalize(value: Any) -> Optional[dict]:
    """``{id, version, name}`` for a well-formed guardrail reference, else ``None``."""
    if not isinstance(value, dict):
        return None
    gid, ver = str(value.get("id") or ""), str(value.get("version") or "")
    if not _ID.match(gid) or not _VERSION.match(ver):
        return None
    return {"id": gid, "version": ver, "name": str(value.get("name") or "")[:100]}


def parse_setting(raw: Optional[str]) -> Optional[dict]:
    """The guardrail a ``GUARDRAIL_SELECTED`` value names, or ``None`` (absent, ``none``, or anything malformed)."""
    if not raw:
        return None
    try:
        return normalize(json.loads(raw))
    except (ValueError, TypeError):
        return None


def _ssm():
    global _client
    if _client is None:
        import boto3  # noqa: PLC0415

        _client = boto3.client("ssm", region_name=os.environ.get("AWS_REGION") or "us-west-2")
    return _client


def _read() -> Optional[dict]:
    path = os.environ.get("RUNTIME_ARN_PARAM_PATH", "").strip().rstrip("/")
    if not path:
        return None
    found = _ssm().get_parameters(Names=[f"{path}/{SELECTED_NAME}"]).get("Parameters", [])
    return parse_setting(found[0].get("Value") if found else None)


def platform_guardrail() -> Optional[dict]:
    """The platform's guardrail now (cached up to a minute), or ``None``. Never raises."""
    now = time.monotonic()
    with _lock:
        if _cache["at"] is not None and now - _cache["at"] < _TTL_SECONDS:
            return _cache["value"]
    try:
        value = _read()
    except Exception as exc:  # noqa: BLE001 - keep the last value read rather than drop the guardrail
        with _lock:
            _cache["at"] = now
            if _cache["known"]:
                logger.warning("guardrail setting unreadable (%s); keeping the last one", type(exc).__name__)
                return _cache["value"]
        logger.error("guardrail setting unreadable (%s) and none known yet: this request runs unguarded",
                     type(exc).__name__)
        return None
    with _lock:
        _cache.update(at=now, value=value, known=True)
    return value


def bind(value: Any) -> Optional[dict]:
    """Bind ``value`` (normalized) for this request; ``None`` clears it. Returns what was bound."""
    g = normalize(value)
    _current.set(g)
    return g


def current() -> Optional[dict]:
    return _current.get()


def bind_human_text(*texts: Any) -> None:
    """Bind what a person wrote for this request: the only input the guardrail checks."""
    _human_texts.set(tuple(t for t in texts if isinstance(t, str) and t.strip()))


def guard_only_human_text(request: dict) -> dict:
    """Make a Converse request check only the person's words on input.

    Strands wraps EVERY text block of the latest user message in ``guardContent``, including the harness's injected
    context. Unwrap all of it, then wrap just the person's text in the latest user message that has it. Without
    ``guardContent`` Bedrock would check the WHOLE conversation, so when no such text is present (a summarised turn,
    or a request with no person behind it) the call is sent without ``guardrailConfig`` rather than mis-checked.
    """
    if not isinstance(request, dict) or "guardrailConfig" not in request:
        return request
    human = set(_human_texts.get())
    messages = request.get("messages") or []
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else None
        if not isinstance(content, list):
            continue
        for i, block in enumerate(content):
            gc = block.get("guardContent") if isinstance(block, dict) else None
            if isinstance(gc, dict):
                if "text" in gc and isinstance(gc["text"], dict):
                    content[i] = {"text": gc["text"].get("text", "")}
                elif "image" in gc:
                    content[i] = {"image": gc["image"]}
    for m in reversed(messages):
        if not isinstance(m, dict) or m.get("role") != "user" or not isinstance(m.get("content"), list):
            continue
        hits = [i for i, b in enumerate(m["content"]) if isinstance(b, dict) and b.get("text") in human]
        if hits:
            for i in hits:
                m["content"][i] = {"guardContent": {"text": {"text": m["content"][i]["text"]}}}
            return request
    request.pop("guardrailConfig", None)
    if human:
        logger.info("guardrail: the person's message is not in this request; sent without a guardrail check")
    return request


def strands_config(g: dict) -> dict:
    """The ``BedrockModel`` fields that apply ``g``.

    ``async`` streaming keeps text flowing (an intervention retracts it) instead of holding every token until checked;
    ``latest_message`` checks the new user message, not the whole history and every tool result on each cycle.
    """
    return {
        "guardrail_id": g["id"],
        "guardrail_version": g["version"],
        "guardrail_trace": "enabled",
        "guardrail_stream_processing_mode": "async",
        "guardrail_latest_message": True,
        "guardrail_redact_input": True,
        "guardrail_redact_output": True,
    }


def _reset_for_tests() -> None:
    global _client
    with _lock:
        _cache.update(at=None, value=None, known=False)
    _client = None
    _current.set(None)
    _human_texts.set(())
