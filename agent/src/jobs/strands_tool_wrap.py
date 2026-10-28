"""Strands-side per-tool wrapper for async-job-tracking.

Background — why this module exists
------------------------------------
The original spec (task 5.3) wired :class:`JobDispatchInterceptor` over
:meth:`MCPManager.dispatch_tool_call`. That method, however, is **not**
on the path Strands' ``Agent`` takes when the LLM picks an MCP tool:
Strands instead invokes :class:`strands.tools.mcp.MCPAgentTool.stream`
directly (which calls :meth:`MCPClient.call_tool_async` under the
hood). The chokepoint we registered against was therefore unreachable
for every real start-tool / results-tool call, and zero registry rows
were ever written.

The fix is to step into the Strands tool-call path itself, not as a
generic wrapper around every MCP tool, but selectively for the
specific :class:`MCPAgentTool` instances that participate in a known
:class:`~agent.src.jobs.models.JobTrio`. Non-trio tools flow through
unchanged.

Hook shape
----------
:class:`JobTrackingMCPAgentTool` subclasses :class:`MCPAgentTool` and
overrides :meth:`stream` to:

* For ``start`` role members: forward to ``super().stream`` to obtain
  the real :class:`~strands.types.tools.ToolResult`, extract the
  ``job_id`` from its content (if any), and register a
  :class:`~agent.src.jobs.models.JobRecord` *before yielding the
  result*. Ordering matters — R3.2 says the registry write must
  precede the response handed back to the agent loop.

* For ``results`` role members: read the registry first; if status is
  not ``SUCCEEDED`` (active or non-success terminal), short-circuit
  with a synthetic :class:`~strands.types.tools.ToolResult` and skip
  the underlying call entirely (R10.2, R10.3). Otherwise pass through
  to ``super().stream`` and let the underlying results tool fetch the
  outputs.

The reused helpers from :mod:`agent.src.jobs.interceptor`
(:func:`_extract_job_id`, :func:`_resolve_gateway_id_for_job`,
:func:`_build_synthetic_results_response`) keep the registration /
gating semantics consistent with the now-dead
``MCPManager.dispatch_tool_call`` chokepoint, so the spec contracts
that the interceptor used to enforce still hold — they just hold via
this code path now.

Failure handling
----------------
The wrapper is **strictly additive**: any failure inside the
job-tracking side effects (registry write fails, response-shape parse
errors, contextvar resolution misses) MUST NOT corrupt or replace the
underlying tool result. Every defensive ``try/except`` here logs and
swallows; the original :class:`ToolResultEvent` always reaches the
agent loop unchanged. R7.4 ("IF the Inline_UI fails to render the
reference for an Async_Job, THEN the start tool invocation SHALL
still complete") falls naturally out of this contract.

Requirements: 3.1, 3.2, 3.3, 3.4, 10.2, 10.3, 11.3
"""
from __future__ import annotations

import asyncio
import re
import json
import logging
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from strands.tools.mcp.mcp_agent_tool import MCPAgentTool
from strands.types._events import ToolResultEvent
from strands.types.tools import ToolResult, ToolUse

from .detector import DetectionResult
from .interceptor import (
    _build_synthetic_results_response,
    _resolve_gateway_id_for_job,
    is_malformed_sfn_arn,
)
from .models import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    JobRecord,
    JobTrio,
)
from .registry import JobRegistry

logger = logging.getLogger(__name__)

# Cached boto3 S3 client for the results source-mirror (see JobTrackingMCPAgentTool.
# _mirror_result_media). Lazily created on first use in a worker thread; a benign
# first-call race just builds two clients.
_MIRROR_S3_CLIENT: Any = None


# ---------------------------------------------------------------------------
# Graceful recovery from a dead MCP client session (transport self-heal)
# ---------------------------------------------------------------------------
# Strands runs each gateway's MCP transport on a background thread; the
# streamable-HTTP session the gateway/upstream can idle-close server-side while
# the local ``_connected`` flag stays sticky-True. The NEXT tool call then dies
# with ``MCPClientInitializationError: the client session is not running`` — the
# request never leaves the client (no request_id, nothing billed), so it is
# safe and idempotent to re-establish the session and retry ONCE. A real
# tool/model error or a 4xx DID reach the upstream and must propagate unchanged
# (retrying could double-submit / double-bill), so the classifier below is
# deliberately narrow: only PRE-REQUEST session/transport-init failures qualify.
#
# NOT obsoleted by the mcp 2.x / "stateless MCP" upgrade. The 2026-07-28
# protocol is stateless, but strands 1.55's ``MCPClient`` is still a long-lived,
# background-thread ``ClientSession`` — the idle-close-then-reuse failure above
# is still reachable. Gate any removal on ``tests/test_mcp_idle_close_recovery.py``
# (does strands self-recover a server-closed session without this wrapper?),
# not on the version bump.

def _is_session_down_error(exc: BaseException) -> bool:
    """True only when ``exc`` means the MCP client SESSION/transport was down
    *before* the request left the client — i.e. nothing reached the upstream,
    so a reconnect+retry is idempotent.

    Narrow by design. Matches Strands' ``MCPClientInitializationError`` (by class
    name, to avoid a hard import that couples us to its module path) and the
    unambiguous "session is not running" / uninitialised-transport messages.
    A model error, tool error, or HTTP 4xx/5xx (which reached the service and
    may carry a request_id) is NOT matched and propagates unchanged.
    """
    if type(exc).__name__ == "MCPClientInitializationError":
        return True
    msg = str(exc).lower()
    return any(s in msg for s in (
        "client session is not running",
        "session is not running",
        "mcp client context manager",
        "used within the mcp client",
    ))


def _is_auth_refresh_error(exc: BaseException) -> bool:
    """True when a tool call was REJECTED for an auth/scope reason that a fresh
    session (reconnect + token/grant refresh) can clear — e.g. a ``user_jwt`` /
    delegated gateway whose machine token or forwarded grant went stale mid-session.

    Distinct from :func:`_is_session_down_error` (which means the transport never
    left the client): here the request DID reach the gateway and was rejected, but
    a REJECTED auth call executed nothing upstream — no generation ran, no job was
    submitted — so re-issuing it once against a freshly-authed session is still
    idempotent (no double-submit / double-bill risk).

    Evidence (session 6c69afb4 / 252dee7c): a warm microVM's fal session degraded to
    ``insufficient_scope`` and every subsequent call failed for ~45 min because nothing
    reconnected; a fresh session worked. Matches the MCP ``insufficient_scope`` (JSON-RPC
    -32002) and tool-call-time 401/403, NOT a generic 4xx/5xx (which is a real upstream
    result and must propagate unchanged).
    """
    msg = str(exc).lower()
    return (
        "insufficient_scope" in msg
        or "insufficient scope" in msg
        or "-32002" in msg
        or ("403" in msg and "forbidden" in msg)
        or ("401" in msg and "unauthorized" in msg)
    )


#: Lifetime of the presigned GETs handed to external tools. Long enough for a queued fal job
#: to fetch its inputs; the signing role's own credential expiry can cap it sooner.
ARTIFACT_INPUT_PRESIGN_TTL_S = 21600


def _is_auth_context_changed_error(exc: BaseException) -> bool:
    """True for :meth:`GatewayConnection.call_lease`'s refusal of a tool whose client was
    replaced. Raised BEFORE the request is sent, so nothing reached the upstream."""
    return isinstance(exc, RuntimeError) and (
        "auth context changed before tool execution" in str(exc)
    )


async def _presign_own_artifact_inputs(tool_use):
    """Return ``tool_use`` with our artifact URLs in its ``input`` presigned, when
    ``ARTIFACTS_CDN_PRIVATE`` is on. Never mutates the caller's object; any failure
    returns the original so a tool call is never broken by this rewrite."""
    if not isinstance(tool_use, dict) or "input" not in tool_use:
        return tool_use
    try:
        from media.artifact_fetch import cdn_private, presign_artifact_urls_in

        if not cdn_private():
            return tool_use
        # First use builds a boto3 client (credential resolution can do I/O), so keep it
        # off the event loop — this coroutine runs on the agent's loop.
        from . import context as jobs_context

        # A project ASSET url is presigned only for the turn's TRUSTED project (verified membership, or
        # forwarded by the dispatcher) -- the asset-id gate, enforced where the bytes leave for a third party.
        new_input, changed = await asyncio.to_thread(
            presign_artifact_urls_in, tool_use["input"], ARTIFACT_INPUT_PRESIGN_TTL_S,
            jobs_context.trusted_project_id())
    except Exception as exc:  # noqa: BLE001 — strictly additive
        logger.warning("artifact input presign skipped: %s", exc)
        return tool_use
    return {**tool_use, "input": new_input} if changed else tool_use


# Verbs that START paid work on an endpoint. Only these are held to a plan node's planned
# endpoints: a status/result read or a `get_model_schema` lookup names an endpoint too, but sends
# nothing to generate.
_SUBMIT_VERBS = frozenset({"submit_job", "run_model"})


def _off_plan_submit_refusal(tool_name: str, tool_use: Any) -> Optional[ToolResult]:
    """An error result when a plan node submits to an endpoint its plan did not name, else None.

    Refused BEFORE the call, so nothing is generated or billed and the node's agent reads the
    planned id in the same turn. Fails open: no planned ids (non-fal nodes, older plans), no
    endpoint in the call, or a call outside a plan node is never refused.
    """
    from tool_args import endpoint_matches_plan, submitted_endpoint

    from . import context as jobs_context

    if (tool_name or "").rsplit("___", 1)[-1] not in _SUBMIT_VERBS:
        return None
    planned = jobs_context.current_planned_endpoints()
    if not planned or not isinstance(tool_use, dict):
        return None
    endpoint = submitted_endpoint(tool_use.get("input"))
    if endpoint_matches_plan(endpoint, planned):
        return None
    node = jobs_context.current_node_id() or "?"
    logger.warning("off_plan_submit_refused node=%r tool=%r endpoint=%r planned=%r",
                   node, tool_name, endpoint, list(planned))
    return {
        "toolUseId": tool_use.get("toolUseId", ""),
        "status": "error",
        "content": [{"text": (
            f"Refused: this plan step ({node!r}) is planned to use "
            f"{' or '.join(repr(p) for p in planned)}, but this call names {endpoint!r}. Nothing "
            f"was submitted. Call {tool_name} again with endpoint_id set to the planned endpoint, "
            f"copied exactly, and that model's own parameters."
        )}],
    }


_MODEL_NOT_FOUND_RE = re.compile(r'"status"\s*:\s*"not_found"|Model not found', re.IGNORECASE)


def _retire_missing_planned_endpoint(tool_name: str, tool_use: Any, events: list) -> None:
    """When a submit to a PLANNED endpoint answers "model not found", lift that id's constraint for the node,
    so the node may submit to the endpoint it finds instead of being refused (plan 07fd91bd). Never raises."""
    try:
        from tool_args import submitted_endpoint

        from . import context as jobs_context

        if (tool_name or "").rsplit("___", 1)[-1] not in _SUBMIT_VERBS or not isinstance(tool_use, dict):
            return
        endpoint = submitted_endpoint(tool_use.get("input"))
        if not endpoint or not jobs_context.current_planned_endpoints():
            return
        for ev in events:
            result = ev.get("tool_result") if isinstance(ev, ToolResultEvent) else None
            text = " ".join(str(b.get("text") or "") for b in (result or {}).get("content") or [] if isinstance(b, dict))
            if _MODEL_NOT_FOUND_RE.search(text) and jobs_context.retire_planned_endpoint(endpoint):
                logger.warning("planned endpoint %r does not exist; the node may use another", endpoint)
                return
    except Exception:  # noqa: BLE001 - a guard nicety must never break the call
        return


def _note_citable_sources(tool_name: str, events: list) -> list:
    """Append the citation note to a successful web-search / AWS-documentation result.

    The rule rides on the result itself, so it reaches whichever agent called the tool -- the Main Agent directly or
    a dispatched sub-agent, whose prompt carries no citation rule -- and a sub-agent is told to hand the links back.
    """
    import web_search

    if not web_search.is_source_tool(tool_name):
        return events
    out = []
    for ev in events:
        result = ev.get("tool_result") if isinstance(ev, ToolResultEvent) else None
        if isinstance(result, dict) and result.get("status") != "error" and isinstance(result.get("content"), list):
            ev = ToolResultEvent({**result, "content": [*result["content"], {"text": web_search.citation_note()}]},
                                 ev.exception, backgrounded=ev.backgrounded)
        out.append(ev)
    return out


def _web_search_refusal(tool_name: str, tool_use: Any) -> Optional[ToolResult]:
    """An error result for a WebSearch call on a turn that may not search, else None.

    The toolset already leaves WebSearch out on such a turn (web_search.filter_tools); this is the deterministic
    backstop for a call that arrives anyway (a stale tool list, a sub-agent's own catalog). Refused before the
    call, so nothing is searched.
    """
    import web_search

    if not web_search.is_web_search_tool(tool_name) or web_search.turn_allows():
        return None
    logger.warning("web_search_refused tool=%r deployed=%s turn=%s", tool_name, web_search.deployed(),
                   web_search.turn_value())
    return {
        "toolUseId": tool_use.get("toolUseId", "") if isinstance(tool_use, dict) else "",
        "status": "error",
        "content": [{"text": web_search.refusal_text(tool_name)}],
    }


def _untagged_voice_refusal(tool_name: str, tool_use: Any) -> Optional[ToolResult]:
    """An error result for a ``register_voice`` with no gender, else None. Refused before the call,
    so no permanent untagged voice is created (there is no delete- or update-voice tool)."""
    from tool_args import register_voice_missing_gender

    if not isinstance(tool_use, dict) or not register_voice_missing_gender(tool_name, tool_use.get("input")):
        return None
    logger.warning("register_voice_refused_no_gender tool=%r", tool_name)
    return {
        "toolUseId": tool_use.get("toolUseId", ""),
        "status": "error",
        "content": [{"text": (
            "Refused: register_voice needs `gender` (e.g. \"female\", \"male\"). Nothing was registered. "
            "A voice without one is invisible to every gender-filtered list_voice_collection call, and a "
            "registered voice can never be edited. Take it from the speaker or character; if you cannot "
            "tell, ask the user, then call register_voice again with gender set."
        )}],
    }


def _note_truncated_voice_list(tool_name: str, events: list) -> list:
    """Append a warning to a ``list_voice_collection`` result that filled a whole page.

    The tool has no pagination and no total, so a full page means voices may be missing and the
    model must not conclude a named voice does not exist. Returns ``events`` unchanged otherwise.
    """
    from tool_args import VOICE_LIST_LIMIT, voice_list_may_be_truncated

    if not (tool_name or "").endswith("list_voice_collection"):
        return events
    out = []
    for ev in events:
        result = ev.get("tool_result") if isinstance(ev, ToolResultEvent) else None
        if isinstance(result, dict):
            content = result.get("content") or []
            payload = None
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("json"), dict):
                    payload = block["json"]
                    break
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    try:
                        payload = json.loads(block["text"])
                    except ValueError:
                        continue
                    break
            if voice_list_may_be_truncated(tool_name, payload):
                logger.warning("voice_list_truncated count=%s limit=%s", payload.get("count"), VOICE_LIST_LIMIT)
                note = (f"WARNING: this list returned the tool's maximum of {VOICE_LIST_LIMIT} voices and the "
                        "tool has no pagination, so the catalog may hold more (the newest are the ones cut). "
                        "If the voice you need is not here, ask the user for its voice_id and use get_voice; "
                        "never say it does not exist.")
                ev = ToolResultEvent({**result, "content": [*content, {"text": note}]},
                                     ev.exception, backgrounded=ev.backgrounded)
        out.append(ev)
    return out


_ELIDED_URL_RE = re.compile(r"https?://\S*(?:\.\.\.|\u2026)")


def _elided_url_refusal(tool_use: Any) -> Optional[ToolResult]:
    """An error result when a tool input carries an ABBREVIATED url (``…/xi.webp?...``), else None.

    A model copying a long URL through a plan elides it (session e2fb2692: every node received
    ``https://…amazonaws.com/.../xi.webp?...``); sent on, fal fails on an unreachable URL after a queue wait and
    the node reports a vague download error. Refused before the call, nothing is billed and the agent reads WHY.
    """
    if not isinstance(tool_use, dict):
        return None
    bad: list[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, str):
            if _ELIDED_URL_RE.search(v):
                bad.append(v[:160])
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)

    walk(tool_use.get("input"))
    if not bad:
        return None
    logger.warning("elided_url_refused tool_use=%r urls=%r", tool_use.get("toolUseId"), bad)
    return {
        "toolUseId": tool_use.get("toolUseId", ""),
        "status": "error",
        "content": [{"text": (
            "Refused: this call contains an abbreviated URL (it has '...' in it), which no service can fetch: "
            f"{bad[0]!r}. Nothing was submitted. Pass the COMPLETE URL exactly as it appears in your inputs; "
            "for one of our files, the short https://<artifacts domain>/<key> form is complete and is fine."
        )}],
    }


class _ResilientMCPMixin:
    """Mixin giving an :class:`MCPAgentTool` a reconnect+retry-once around the
    base MCP round-trip, keyed off a ``self._reconnect`` callable.

    ``self._reconnect() -> Optional[MCPClient]`` must force a FRESH session
    (tear down any idle-closed one) and return the new live client, or ``None``
    if it could not rebind (the retry then runs on the existing client and, if
    still dead, surfaces the original error). Recovery fires ONLY for a
    pre-request session-down error (see :func:`_is_session_down_error`) and
    ONLY before any event has been yielded — once the round-trip has started
    emitting we never re-issue it.
    """

    _reconnect = None  # set by the constructor of concrete subclasses
    _call_lease = None
    # ``() -> Optional[MCPClient]``: the connection's CURRENT client when it is still bound to
    # the credential this tool was wrapped under, else None (see
    # ``GatewayConnection.current_client_for_auth``).
    _relink = None

    async def _base_stream_once(self, tool_use, invocation_state, kwargs):
        """Run one MCP round trip while its exact client holds a call lease."""
        lease_factory = getattr(self, "_call_lease", None)
        lease = (
            lease_factory(self.mcp_client)
            if callable(lease_factory)
            else nullcontext()
        )
        # Normalize model-emitted args on the path Strands ACTUALLY invokes (it calls
        # MCPAgentTool.stream directly, never GatewayConnection.call_tool_sync): a unit-suffixed
        # fal ``duration`` ("7s") fails at result time with 422 int_parsing (staging 9fb4e17b),
        # and ``fal-ai/minimax/h3-max/...`` routes to the wrong fal app (session 36685185).
        # Model-specific fields are NOT added here: a submit is checked against the model's own
        # schema instead (``_model_input_refusal`` below, ``model_input_check``).
        from tool_args import normalize_duration, normalize_endpoint_ids

        if isinstance(tool_use, dict) and isinstance(tool_use.get("input"), dict):
            normalized = normalize_endpoint_ids(normalize_duration(tool_use["input"]))
            from tool_args import declared_properties, default_inference_thinking

            props = declared_properties(getattr(self, "tool_spec", None))

            normalized = default_inference_thinking(getattr(self, "tool_name", ""), normalized, props)
            from tool_args import default_voice_listing

            normalized = default_voice_listing(getattr(self, "tool_name", ""), normalized, props)
            if normalized != tool_use["input"]:
                tool_use = {**tool_use, "input": normalized}
        refusal = (_elided_url_refusal(tool_use)
                   or _off_plan_submit_refusal(getattr(self, "tool_name", ""), tool_use)
                   or _untagged_voice_refusal(getattr(self, "tool_name", ""), tool_use)
                   or _web_search_refusal(getattr(self, "tool_name", ""), tool_use))
        if refusal is None:
            refusal = await self._model_input_refusal(tool_use)
        if refusal is not None:
            yield ToolResultEvent(refusal)
            return
        # Private artifacts CDN: an external tool (fal.ai, a user MCP server) fetches its
        # inputs over the internet and gets 403 from our CloudFront, so hand it short-lived
        # presigned S3 GETs instead. Only the outbound copy changes — the identifier the agent
        # holds (and stores) stays the stable artifact URL. Off unless ARTIFACTS_CDN_PRIVATE.
        tool_use = await _presign_own_artifact_inputs(tool_use)
        events = []
        with lease:
            async for ev in MCPAgentTool.stream(
                self, tool_use, invocation_state, **kwargs
            ):
                events.append(ev)
        # Release the transport lease before yielding. Strands stops consuming
        # as soon as it sees ToolResultEvent, so yielding inside ``with lease``
        # can leave the async generator suspended and block every later auth
        # transition indefinitely.
        events = _note_truncated_voice_list(getattr(self, "tool_name", ""), events)
        events = _note_citable_sources(getattr(self, "tool_name", ""), events)
        _retire_missing_planned_endpoint(getattr(self, "tool_name", ""), tool_use, events)
        for ev in events:
            yield ev

    async def _model_input_refusal(self, tool_use):
        """A pre-call error result when a model submit breaks THAT model's own schema, else None.

        The schema comes from the same server's ``get_model_schema`` (cached per endpoint), so nothing about any
        model is hard-coded here. Fails OPEN on everything: not a submit, the kill switch off, no schema tool,
        an unreadable schema or any error → None and the call proceeds unchecked (``model_input_check``).
        """
        try:
            import model_input_check as mic

            tool_name = getattr(self, "tool_name", "") or ""
            verb = mic.verb_of(tool_name)
            if not mic.enabled() or verb not in mic.SUBMIT_VERBS or not isinstance(tool_use, dict):
                return None
            subs = mic.submissions(verb, tool_use.get("input"))
            if not subs:
                return None
            server_name = getattr(getattr(self, "mcp_tool", None), "name", "") or tool_name
            schema_tool = mic.schema_tool_name(server_name)
            failures = []
            for where, endpoint, model_input in subs:
                hit, schema = mic.cached_schema(endpoint)
                if not hit:
                    schema = await self._fetch_model_schema(mic, schema_tool, endpoint)
                    mic.remember(endpoint, schema)
                if schema is None:
                    continue
                probs = mic.problems(schema, model_input)
                if probs:
                    failures.append((where, endpoint, schema, probs))
            if not failures:
                return None
            logger.warning("model_input_refused tool=%r %s", tool_name,
                           "; ".join(f"{ep}: {', '.join(p)}" for _, ep, _, p in failures))
            return {"toolUseId": tool_use.get("toolUseId", ""), "status": "error",
                    "content": [{"text": mic.refusal_text(tool_name, failures)}]}
        except Exception as exc:  # noqa: BLE001 — a check must never break a call
            logger.warning("model input check skipped: %s", exc)
            return None

    async def _fetch_model_schema(self, mic, schema_tool: str, endpoint: str):
        """The model's input schema from the server's ``get_model_schema``; None on any failure."""
        try:
            from datetime import timedelta

            lease_factory = getattr(self, "_call_lease", None)
            lease = lease_factory(self.mcp_client) if callable(lease_factory) else nullcontext()
            with lease:
                result = await self.mcp_client.call_tool_async(
                    tool_use_id=f"model-schema-{uuid.uuid4().hex[:12]}", name=schema_tool,
                    arguments={"endpoint_id": endpoint}, read_timeout_seconds=timedelta(seconds=15))
            schema = mic.input_schema(mic.payload_of(result), endpoint)
            if schema is None:
                logger.info("model schema unavailable endpoint=%r tool=%r", endpoint, schema_tool)
            return schema
        except Exception as exc:  # noqa: BLE001 — no schema means no check
            logger.info("model schema fetch failed endpoint=%r: %s", endpoint, exc)
            return None

    async def _relink_current_client(self) -> bool:
        """Point this tool at its connection's CURRENT client when, and only when, that client
        carries the same credential the tool was wrapped under. True when rebound."""
        relink = getattr(self, "_relink", None)
        if not callable(relink):
            return False
        try:
            # May wait (bounded) for an in-flight auth transition: keep it off the event loop.
            current = await asyncio.to_thread(relink)
        except Exception as exc:  # noqa: BLE001 — fall back to surfacing the lease error
            logger.warning("MCP tool %r: re-lease lookup failed: %s",
                           getattr(self, "tool_name", "?"), exc)
            return False
        if current is None:
            return False
        self.mcp_client = current
        return True

    async def _resilient_base_stream(self, tool_use, invocation_state, kwargs):
        yielded = False
        relink_original: Optional[BaseException] = None
        try:
            async for ev in self._base_stream_once(tool_use, invocation_state, kwargs):
                yielded = True
                yield ev
            return
        except BaseException as e:  # noqa: BLE001 — reclassified immediately below
            if not yielded and _is_auth_context_changed_error(e):
                # The connection was rebuilt after this tool was handed out, so its client is
                # gone and the lease refused it BEFORE anything was sent. Re-lease the CURRENT
                # client once — but only if it is bound to the same credential (same grant /
                # user token, hence the same actor). A different credential stays fail-closed.
                if not await self._relink_current_client():
                    raise
                logger.warning(
                    "MCP tool %r: gateway session was rebuilt under the same credential; "
                    "re-leasing its current client and retrying once",
                    getattr(self, "tool_name", "?"),
                )
                relink_original = e
            else:
                reconnect = getattr(self, "_reconnect", None)
                # Reconnect+retry-once for EITHER a pre-request session-down error OR an
                # auth/scope rejection a fresh session can clear (stale machine token / grant).
                # Both are idempotent to re-issue: a down transport never reached upstream, and
                # a rejected auth call executed nothing. A real tool/model error or generic
                # 4xx/5xx still propagates unchanged.
                _recoverable = _is_session_down_error(e) or _is_auth_refresh_error(e)
                if yielded or reconnect is None or not _recoverable:
                    raise
                logger.warning(
                    "MCP tool %r: recoverable MCP failure (%s); reconnecting (fresh session/auth) "
                    "and retrying once", getattr(self, "tool_name", "?"), type(e).__name__,
                )
        if relink_original is not None:
            try:
                async for ev in self._base_stream_once(tool_use, invocation_state, kwargs):
                    yield ev
            except RuntimeError as again:
                if _is_auth_context_changed_error(again):
                    raise relink_original from None
                raise
            return
        reconnect = getattr(self, "_reconnect", None)
        # The failed call lease has exited before reconnect waits for peers to
        # drain. The retry acquires a new lease on the fresh client.
        fresh = await asyncio.to_thread(reconnect)
        if fresh is not None:
            self.mcp_client = fresh
        async for ev in self._base_stream_once(tool_use, invocation_state, kwargs):
            yield ev

    async def _mirror_result_media(self, event: ToolResultEvent) -> ToolResultEvent:
        """Copy external media in a tool result to our durable store and return
        the event with URLs rewritten to the durable copies.

        The chokepoint for third-party (fal.media) output durability: swapping here,
        before the result reaches the agent, means the chat text the agent writes, the
        render manifest, and any follow-up edit all reference the permanent CloudFront
        copy — nothing points at an ephemeral URL that later 404s. Lives on the mixin
        so it applies to EVERY wrapped MCP tool: async-trio results AND synchronous
        single-call tools (e.g. fal `run_model` image gen), which previously slipped
        through because the mirror was gated to the trio "results" role only.

        Runs the blocking fetch+PUT off the event loop. Best-effort and never fatal:
        any failure (or nothing external to mirror) returns the ORIGINAL event
        unchanged, so a working tool is never reported as failed. Idempotent with the
        render hook's own mirror (URLs already ours are not re-copied), so double
        coverage across wrappers/hook is a cheap no-op.

        Never for a web-search / AWS-documentation result: its links are SOURCES to cite, not media this
        session produced -- a searched PDF was copied into artifacts/external and opened as a "Document" tab
        on a web-research question (staging ca859a24, 2026-10-21).
        """
        import web_search as _ws
        if _ws.is_source_tool(getattr(self, "tool_name", "")):
            return event
        try:
            import os
            from media import persist_media

            result = event.tool_result
            if not isinstance(result, dict):
                return event
            bucket = os.environ.get("SKILLS_S3_BUCKET") or os.environ.get("S3_BUCKET", "")
            domain = os.environ.get("ARTIFACTS_DOMAIN", "")
            prefix = os.environ.get("ARTIFACTS_PREFIX") or os.environ.get("S3_PREFIX", "artifacts/")
            if not bucket:
                logger.warning(
                    "results mirror: no bucket configured (SKILLS_S3_BUCKET unset); "
                    "leaving %r result URLs unmirrored", getattr(self, "tool_name", "?"),
                )
                return event
            global _MIRROR_S3_CLIENT
            if _MIRROR_S3_CLIENT is None:
                import boto3
                _MIRROR_S3_CLIENT = boto3.client("s3")
            new_result, copied = await asyncio.to_thread(
                persist_media.swap_result_media, result,
                s3_client=_MIRROR_S3_CLIENT, bucket=bucket, prefix=prefix,
                artifacts_domain=domain,
            )
            if not copied or new_result is result:
                return event
            logger.info(
                "results mirror: copied %d external asset(s) into durable store for tool=%r",
                len(copied), getattr(self, "tool_name", "?"),
            )
            return ToolResultEvent(new_result)
        except Exception as e:  # noqa: BLE001 — mirroring must never break a tool result
            logger.warning(
                "results mirror: passthrough for tool=%r (%s: %s)",
                getattr(self, "tool_name", "?"), type(e).__name__, e,
            )
            return event


class ResilientMCPAgentTool(_ResilientMCPMixin, MCPAgentTool):
    """Drop-in :class:`MCPAgentTool` that self-heals a dead MCP session.

    Constructed by :func:`wrap_resilient` for every non-trio MCP tool (trio
    members get the same behaviour via :class:`JobTrackingMCPAgentTool`, which
    also mixes in :class:`_ResilientMCPMixin`).
    """

    def __init__(
        self,
        *,
        original: MCPAgentTool,
        reconnect: Optional[Callable[[], Any]],
        call_lease: Optional[Callable[[Any], Any]] = None,
        relink: Optional[Callable[[], Any]] = None,
    ) -> None:
        super().__init__(
            mcp_tool=original.mcp_tool,
            mcp_client=original.mcp_client,
            name_override=original.tool_name,
            timeout=original.timeout,
        )
        self._reconnect = reconnect
        self._call_lease = call_lease
        self._relink = relink

    async def stream(  # type: ignore[override]
        self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any,
    ):
        async for ev in self._resilient_base_stream(tool_use, invocation_state, kwargs):
            # SOURCE-MIRROR for non-trio (incl. SYNCHRONOUS) tools: copy any external
            # (e.g. fal.media) media in the result into our durable store and rewrite
            # the URLs BEFORE the agent sees the result — so the chat text quotes the
            # permanent CloudFront copy. This is what lets a synchronous fal image
            # (`run_model`) inline in Slack and outlive fal's temporary URL, matching
            # the async-trio results path. Best-effort / never fatal / idempotent.
            if isinstance(ev, ToolResultEvent):
                ev = await self._mirror_result_media(ev)
            yield ev


def wrap_resilient(
    tools: list,
    *,
    reconnect: Optional[Callable[[], Any]],
    call_lease: Optional[Callable[[Any], Any]] = None,
    relink: Optional[Callable[[], Any]] = None,
) -> list:
    """Return ``tools`` with every plain :class:`MCPAgentTool` replaced by a
    :class:`ResilientMCPAgentTool`. Idempotent: tools already carrying the
    reconnect mixin (resilient or job-tracking) and non-MCP tools pass through
    unchanged. Never raises — on any failure the original tool is kept.
    """
    out: list = []
    for tool in tools:
        try:
            if isinstance(tool, _ResilientMCPMixin) or not isinstance(tool, MCPAgentTool):
                out.append(tool)
                continue
            out.append(ResilientMCPAgentTool(
                original=tool,
                reconnect=reconnect,
                call_lease=call_lease,
                relink=relink,
            ))
        except Exception as e:  # noqa: BLE001 — strict-additive: never break the tool list
            logger.error("wrap_resilient: leaving %r unwrapped: %s", getattr(tool, "tool_name", "?"), e)
            out.append(tool)
    return out


# Re-derived locally rather than imported from interceptor.py so the
# wrapper module stays self-contained even if interceptor.py is
# eventually retired (the chokepoint pattern it implements has no
# remaining caller after this change). Kept structurally identical to
# ``interceptor._extract_job_id`` so the audiostem / SFN / generic
# trio responses parse the same way.
def _extract_job_id_from_strands_result(result: ToolResult) -> Optional[str]:
    """Recover ``job_id`` from a Strands :class:`ToolResult`.

    The Strands ``ToolResult.content`` is a list of typed-dict blocks
    where each block holds at most one of ``text`` / ``json`` /
    ``document`` / ``image``. MCP servers emit either a ``json`` block
    (when the server returns a typed structuredContent payload) or a
    ``text`` block whose body is the JSON-encoded response. Both
    shapes are supported here.

    Returns ``None`` whenever the response carries no non-empty
    string ``job_id`` — the caller logs a diagnostic and skips
    registration (R3.3).
    """
    content = result.get("content")
    if not isinstance(content, list):
        return None

    # fal's queue tools return the id as `request_id`, not `job_id`; accept both (job_id first)
    # so a fal submit_job registers instead of being dropped as unrecognized.
    _id_keys = ("job_id", "request_id")

    for block in content:
        if not isinstance(block, dict):
            continue

        # 1. ``json`` content block — typed structured payload.
        if "json" in block:
            payload = block["json"]
            if isinstance(payload, dict):
                for k in _id_keys:
                    jid = payload.get(k)
                    if isinstance(jid, str) and jid:
                        return jid

        # 2. ``text`` content block — JSON-encoded payload (the
        #    audiostem path: FastMCP serialises the dict return
        #    into a single text block).
        if "text" in block:
            text = block["text"]
            if isinstance(text, str):
                try:
                    parsed = json.loads(text)
                except (json.JSONDecodeError, ValueError, TypeError):
                    continue
                if isinstance(parsed, dict):
                    for k in _id_keys:
                        jid = parsed.get(k)
                        if isinstance(jid, str) and jid:
                            return jid

    return None


_QUEUE_HOST = "queue.fal.run/"


def _is_transient_missing_result(result: ToolResult) -> bool:
    """True when a results fetch returned a 404 / no-payload — a possible gap between a COMPLETED
    status and the result being retrievable. Retriable because ``get_job_result`` is an idempotent
    read, and bounded so a permanent 404 resurfaces promptly. NOTE: a job that reports COMPLETED
    within seconds and then 404s on EVERY fetch is a wrong endpoint id, not eventual consistency
    — ``fal-ai/minimax/h3-max/image-to-video`` routes to the app ``fal-ai/minimax`` and never has
    a result (session 36685185); the real id is ``minimax/h3-max/image-to-video``, which
    ``tool_args.normalize_endpoint_ids`` now rewrites on the way in."""
    try:
        blob = json.dumps(result).lower()
    except (TypeError, ValueError):
        blob = str(result).lower()
    if "404" in blob:
        return True
    # A results tool that reports an error with a not-yet-available payload.
    return result.get("status") == "error" and (
        "not found" in blob or "no retrievable" in blob or "result body" in blob
    )


def _lineage_inputs_from_tool_use(tool_use: Optional[ToolUse]) -> list[str]:
    """projects.lineageEdges: every input location (canonical ``s3://``) and external URL (query-less) the start
    tool was given, for the registrar to link the job's outputs to. ``[]`` with the flag off; never raises."""
    if not isinstance(tool_use, dict):
        return []
    try:
        from projects import lineage_contract as lc
        if not lc.edges_enabled():
            return []
        from media import turn_outputs
        canon, unresolved = turn_outputs._input_refs(tool_use.get("input"))
        return list(dict.fromkeys([*canon, *(u["ref"] for u in unresolved)]))[:50]
    except Exception:  # noqa: BLE001 — lineage capture is never worth failing a job over
        return []


def _source_media_from_tool_use(tool_use: Optional[ToolUse]) -> list[str]:
    """The ``s3://`` source media the start tool was invoked on, from its input args.

    Best-effort and tool-agnostic: any media-extension ``s3://`` URI in the input is the
    thing being analysed. Never raises — a capture miss must not fail registration.
    """
    if not isinstance(tool_use, dict):
        return []
    try:
        from media.source_media import extract_source_media
        return extract_source_media(tool_use.get("input"))
    except Exception:  # noqa: BLE001 — source capture is never worth failing a job over
        return []


def _endpoint_id_from_status_url(status_url: Any) -> Optional[str]:
    """`https://queue.fal.run/bytedance/seedance-2.5/requests/<id>/status` -> `bytedance/seedance-2.5`.

    fal's queue status check requires the model ENDPOINT alongside the request_id; it is not in
    the submit response as a field but is embedded in the status_url path.
    """
    if not isinstance(status_url, str) or _QUEUE_HOST not in status_url:
        return None
    tail = status_url.split(_QUEUE_HOST, 1)[1]
    endpoint = tail.split("/requests/", 1)[0].strip("/")
    return endpoint or None


def _extract_endpoint_id_from_strands_result(result: ToolResult) -> Optional[str]:
    """The fal queue endpoint_id for this job, so the poller can check status.

    Prefers an explicit ``endpoint_id`` field; otherwise derives it from the ``status_url`` the
    submit response carries. Without it the poller called the status tool with no endpoint_id and
    fal rejected it ("Missing required field(s): 'endpoint_id'").
    """
    content = result.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if not isinstance(block, dict):
            continue
        payload: Any = None
        if "json" in block and isinstance(block["json"], dict):
            payload = block["json"]
        elif "text" in block and isinstance(block.get("text"), str):
            try:
                payload = json.loads(block["text"])
            except (json.JSONDecodeError, ValueError, TypeError):
                payload = None
        if isinstance(payload, dict):
            # The status_url is authoritative for the QUEUE app id: fal's queue lives under the
            # base app (queue.fal.run/<app>/requests/...), which is what check_job/get_job_result
            # need. The explicit `endpoint_id` field is the SUBMIT model path and carries an extra
            # sub-endpoint segment (bytedance/seedance-2.5/text-to-video) that 405s the status
            # check -- so derive from status_url first, fall back to the explicit field only when
            # there is no status_url.
            derived = _endpoint_id_from_status_url(payload.get("status_url"))
            if derived:
                return derived
            explicit = payload.get("endpoint_id")
            if isinstance(explicit, str) and explicit:
                return explicit
    return None


def _build_synthetic_tool_result(
    *,
    tool_use_id: str,
    payload: dict,
) -> ToolResult:
    """Wrap a synthetic response payload into a Strands
    :class:`ToolResult`.

    The results-gate path (active / non-success terminal) bypasses the
    underlying MCP tool entirely. Strands still expects a well-formed
    :class:`ToolResult` with the original ``toolUseId`` so the
    conversation history threads correctly. Emit one ``text`` content
    block whose body is the JSON-encoded synthetic payload — same
    parsing path the downstream agent runtime takes when the real
    tool is invoked.
    """
    return {
        "toolUseId": tool_use_id,
        "status": "success",
        "content": [{"text": json.dumps(payload)}],
    }


class JobTrackingMCPAgentTool(_ResilientMCPMixin, MCPAgentTool):
    """:class:`MCPAgentTool` subclass with R3 / R10 side effects.

    Constructed by :func:`wrap_trio_tools` — copies the underlying
    tool's MCP descriptor and client reference into a new instance so
    the existing tool slot in ``GatewayConnection._all_tools`` can be
    replaced without disturbing the rest of Strands' tool registry
    semantics.

    The ``role`` argument selects the side-effect path:

    * ``"start"`` — register on success.
    * ``"results"`` — gate on registry status before calling.
    """

    def __init__(
        self,
        *,
        original: MCPAgentTool,
        role: str,
        trio: JobTrio,
        registry: JobRegistry,
        actor_id_resolver: Callable[[], str],
        session_id_resolver: Callable[[], str],
        reconnect: Optional[Callable[[], Any]] = None,
        call_lease: Optional[Callable[[Any], Any]] = None,
        relink: Optional[Callable[[], Any]] = None,
    ) -> None:
        # Re-construct the parent using the original's underlying
        # references. Do NOT hold ``original`` directly — the parent
        # holds those same fields under names Strands expects, and
        # owning two copies of the state would invite drift if
        # MCPAgentTool's ``__init__`` ever changes shape.
        super().__init__(
            mcp_tool=original.mcp_tool,
            mcp_client=original.mcp_client,
            name_override=original.tool_name,
            timeout=original.timeout,
        )
        if role not in ("start", "results"):
            raise ValueError(
                f"JobTrackingMCPAgentTool: unsupported role {role!r}; "
                "expected 'start' or 'results'"
            )
        self._role = role
        self._trio = trio
        self._registry = registry
        self._actor_id_resolver = actor_id_resolver
        self._session_id_resolver = session_id_resolver
        self._reconnect = reconnect
        self._call_lease = call_lease
        self._relink = relink

    # The Strands base class declares ``stream`` as ``async def`` that
    # returns a ``ToolGenerator`` (an async iterator yielding events).
    # We must match that shape.
    async def stream(  # type: ignore[override]
        self,
        tool_use: ToolUse,
        invocation_state: dict[str, Any],
        **kwargs: Any,
    ):
        if self._role == "results":
            gate_event = await self._maybe_gate_results(tool_use)
            if gate_event is not None:
                yield gate_event
                return
            # Past the gate the job is SUCCEEDED (or unknown to the registry). Retry a transient
            # result-fetch 404 with bounded backoff before surfacing failure — an idempotent read.
            async for event in self._results_stream_with_retry(tool_use, invocation_state, kwargs):
                yield event
            return

        # Forward to the underlying MCP tool. The base class yields
        # exactly one ``ToolResultEvent`` after the MCP round-trip
        # returns. CRITICAL: the Strands tool executor (see
        # ``strands/tools/executors/_executor.py``) ``break``s out of
        # the iteration the moment it sees a ToolResultEvent, so any
        # work scheduled AFTER the ``async for`` loop never runs.
        # The registration must therefore happen *before* the yield,
        # which also tightens R3.2 ("persist before returning the
        # start tool response") — the registry row is durable by the
        # time the agent loop sees the result.
        async for event in self._resilient_base_stream(
            tool_use, invocation_state, kwargs
        ):
            if self._role == "start" and isinstance(event, ToolResultEvent):
                await self._maybe_register_start(event.tool_result, tool_use)
            yield event

    async def _results_stream_with_retry(
        self, tool_use: ToolUse, invocation_state: dict[str, Any], kwargs: dict[str, Any],
    ):
        """Run the underlying results tool; on a transient missing-result 404 retry with bounded
        backoff. Mirrors external media on the final result.

        The retry is safe because ``get_job_result`` is an idempotent read reached only past the
        SUCCEEDED gate. Bounded (3 retries, ~14s total) so a permanent 404 still surfaces promptly.
        """
        delays = (2.0, 4.0, 8.0)
        attempt = 0
        while True:
            events: list[Any] = []
            async for ev in self._resilient_base_stream(tool_use, invocation_state, kwargs):
                events.append(ev)
            result_ev = next(
                (e for e in reversed(events) if isinstance(e, ToolResultEvent)), None)
            if (result_ev is not None and attempt < len(delays)
                    and _is_transient_missing_result(result_ev.tool_result)):
                logger.info(
                    "results_tool_retry_transient_404 tool=%r attempt=%d/%d — job COMPLETED "
                    "but result not yet retrievable; backing off %.0fs",
                    self.tool_name, attempt + 1, len(delays), delays[attempt],
                )
                await asyncio.sleep(delays[attempt])
                attempt += 1
                continue
            for ev in events:
                if isinstance(ev, ToolResultEvent):
                    # SOURCE-MIRROR: copy external (e.g. fal.media) media into our durable
                    # store and rewrite URLs BEFORE the agent sees the result.
                    ev = await self._mirror_result_media(ev)
                yield ev
            return

    # ------------------------------------------------------------------
    # Side-effect implementations
    # ------------------------------------------------------------------

    # _mirror_result_media lives on _ResilientMCPMixin (shared) so BOTH the
    # non-trio ResilientMCPAgentTool and this trio wrapper mirror external media
    # on their result — synchronous media tools (e.g. fal `run_model` image gen)
    # are covered too, not just async-trio results.

    async def _maybe_register_start(self, result: ToolResult, tool_use: Optional[ToolUse] = None) -> None:
        """Register a :class:`JobRecord` for a successful start call.

        No-op (with a diagnostic log) when:

        * the underlying tool reported ``status != "success"`` — there
          is no live job to track;
        * the response carries no ``job_id`` — R3.3 says skip and
          diagnose, do not error;
        * the actor / session contextvars are unbound — the registry
          row would be missing its scoping key, so we log and skip;
        * the registry write itself raises — R7.4 says the underlying
          tool result still completes; we only swallow the error.
        """
        if result.get("status") != "success":
            logger.info(
                "start_tool_status_not_success tool=%r status=%r — "
                "skipping registration",
                self.tool_name, result.get("status"),
            )
            return

        job_id = _extract_job_id_from_strands_result(result)
        if job_id and is_malformed_sfn_arn(job_id):
            # The workflow was submitted but its Step Functions execution never started
            # (the ARN has no execution name) — almost always an empty/invalid required
            # input (e.g. an unresolved upstream URI). Do NOT register a phantom job that
            # would hang forever; surface a clear diagnostic instead (DAG design P1).
            logger.warning(
                "start_tool_malformed_sfn_arn tool=%r job_id=%r — execution never "
                "started (likely an empty/invalid required input); NOT registering a "
                "phantom job",
                self.tool_name, job_id,
            )
            return
        if not job_id:
            logger.info(
                "start_tool_no_job_id tool=%r content_blocks=%d — "
                "skipping registration (R3.3)",
                self.tool_name,
                len(result.get("content", []) or []),
            )
            return

        try:
            actor_id = self._actor_id_resolver()
            session_id = self._session_id_resolver()
        except Exception as e:  # noqa: BLE001 — defensive
            logger.warning(
                "JobTrackingMCPAgentTool: actor/session context unbound "
                "for tool=%r job_id=%r: %s — skipping registration",
                self.tool_name, job_id, e,
            )
            return

        gateway_id = _resolve_gateway_id_for_job(
            job_id=job_id, trio_gateway_id=self._trio.gateway_id,
        )

        # Resolve optional run_id from context (set by plan_and_execute_tool
        # when executing a DAG via the Coordinator). None when the tool call
        # happens outside a plan execution — the field is simply omitted from
        # the DDB item in that case.
        from . import context as jobs_context
        run_id: str | None = None
        try:
            run_id = jobs_context.current_run_id()
        except Exception:
            pass

        # The invocation that caused this job. Bound for every request, unlike
        # run_id, so a standalone dispatch is still groupable.
        turn_id: str | None = None
        try:
            turn_id = jobs_context.current_turn_id()
        except Exception:
            pass

        # A user_jwt gateway has no user present when the polling worker later
        # checks this job: the turn is over and its JWT is unpersistable and
        # short-lived. Mint a grant scoped to THIS job so the poller can prove
        # whose job it is; the interceptor verifies the signature and derives the
        # actor from it. Best-effort by design -- an exception means "no grant",
        # and a user_jwt job without one simply cannot be polled (the poller says
        # so loudly) rather than being polled as the wrong actor.
        delegation_grant = None
        try:
            from security import delegation as _delegation

            if _delegation.grants_enabled() and _delegation.can_delegate(actor_id):
                delegation_grant = _delegation.mint_grant(actor_id, job_id)
        except Exception as exc:  # noqa: BLE001 — see comment above
            logger.debug("no delegation grant for job %s: %s", job_id, exc)

        record = JobRecord(
            job_id=job_id,
            actor_id=actor_id,
            session_id=session_id,
            trio_id=self._trio.trio_id,
            gateway_id=gateway_id,
            status_tool=self._trio.status_tool,
            results_tool=self._trio.results_tool,
            status="PENDING",
            start_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
            run_id=run_id,
            turn_id=turn_id,
            delegation_grant=delegation_grant,
            # Always the trio's real gateway — NOT the possibly-SFN-
            # overridden gateway_id above. results_tool lives on this
            # gateway regardless of which path the polling worker takes
            # for status checks.
            results_gateway_id=self._trio.gateway_id,
            # fal queue jobs need the model endpoint to poll status; derive it from the submit
            # response's status_url (None for non-queue jobs -> field omitted from the row).
            endpoint_id=_extract_endpoint_id_from_strands_result(result),
            # The media this job analyses, taken from the start-tool INPUT. The async
            # completion render reads it back by job_id to overlay the analysis on its
            # source. Structural + tool-agnostic (see source_media.extract_source_media).
            source_media=_source_media_from_tool_use(tool_use),
            lineage_inputs=_lineage_inputs_from_tool_use(tool_use),
            # TRUSTED id only (asset-lineage Phase 3 (a)): the polling worker imports this job's outputs
            # under it, so a main turn whose project access is not `ok` must not stamp it.
            producing_project_id=jobs_context.trusted_project_id(),
            # Inside a plan node: the node this job belongs to, so the bridge can map the finished
            # job back to it even before the node has recorded the job id (session c3447913).
            # Its own field, not `node_id`: a row with nodeId is drawn and notified as a plan
            # NODE by the UI (PlanDagView, run-row notify), which this gateway job is not.
            plan_node_id=jobs_context.current_node_id(),
        )

        try:
            await asyncio.to_thread(self._registry.register, record)
            logger.info(
                "registered_async_job tool=%r trio_id=%r job_id=%r "
                "actor_id=%r session_id=%r gateway_id=%r",
                self.tool_name, self._trio.trio_id, job_id,
                actor_id, session_id, gateway_id,
            )
        except Exception as e:  # noqa: BLE001 — defensive
            # R7.4 / strict-additive: registry failures must not
            # corrupt the tool response that we already yielded.
            logger.error(
                "JobTrackingMCPAgentTool: registry.register failed for "
                "tool=%r job_id=%r: %s",
                self.tool_name, job_id, e,
            )

    async def _maybe_gate_results(
        self, tool_use: ToolUse,
    ) -> Optional[ToolResultEvent]:
        """Return a synthetic :class:`ToolResultEvent` when the gate
        should short-circuit, or ``None`` to fall through.

        R10.2 / R10.3 mirror the previous interceptor-based gate:

        * ``status`` in :data:`ACTIVE_STATUSES` -> emit
          ``{job_id, status}`` synthetic response without calling the
          underlying tool.
        * ``status`` in :data:`TERMINAL_STATUSES` and not
          ``SUCCEEDED`` -> emit ``{job_id, status, lastError}``.
        * Missing args / missing record / cross-actor mismatch / any
          unexpected failure -> fall through to the underlying tool.
        """
        args = tool_use.get("input")
        job_id = args.get("job_id") if isinstance(args, dict) else None
        if not isinstance(job_id, str) or not job_id:
            # The underlying tool will surface its own missing-arg
            # error; better that than synthesising one here.
            return None

        try:
            actor_id = self._actor_id_resolver()
        except Exception as e:  # noqa: BLE001 — defensive
            logger.warning(
                "JobTrackingMCPAgentTool: actor context unbound for "
                "results-tool gate tool=%r job_id=%r: %s",
                self.tool_name, job_id, e,
            )
            return None

        try:
            record = await asyncio.to_thread(
                self._registry.get, job_id, actor_id=actor_id,
            )
        except Exception as e:  # noqa: BLE001 — defensive
            logger.warning(
                "JobTrackingMCPAgentTool: registry.get failed for "
                "results-tool gate tool=%r job_id=%r: %s",
                self.tool_name, job_id, e,
            )
            return None

        if record is None:
            # Either the job was never registered, or it belongs to a
            # different actor (R11.2 — cross-actor lookup returns
            # not-found). Fall through; the underlying tool will
            # decide how to handle the unknown id.
            return None

        if record.status == "SUCCEEDED":
            # Happy path — no gating needed; let the underlying
            # results tool fetch and return.
            return None

        if record.status in ACTIVE_STATUSES:
            payload = _build_synthetic_results_response(
                job_id=job_id, status=record.status,
            )
            logger.info(
                "results_tool_gate_active tool=%r trio_id=%r "
                "job_id=%r status=%r",
                self.tool_name, self._trio.trio_id, job_id, record.status,
            )
            return ToolResultEvent(
                _build_synthetic_tool_result(
                    tool_use_id=tool_use["toolUseId"], payload=payload,
                )
            )

        if record.status in TERMINAL_STATUSES:
            payload = _build_synthetic_results_response(
                job_id=job_id,
                status=record.status,
                last_error=record.last_error,
            )
            logger.info(
                "results_tool_gate_terminal tool=%r trio_id=%r "
                "job_id=%r status=%r",
                self.tool_name, self._trio.trio_id, job_id, record.status,
            )
            return ToolResultEvent(
                _build_synthetic_tool_result(
                    tool_use_id=tool_use["toolUseId"], payload=payload,
                )
            )

        # Unknown status — fall through rather than fabricate.
        logger.warning(
            "JobTrackingMCPAgentTool: unknown status %r for job_id=%r; "
            "falling through to underlying results tool",
            record.status, job_id,
        )
        return None


def wrap_trio_tools(
    tools: list,
    detection: DetectionResult,
    *,
    registry: JobRegistry,
    actor_id_resolver: Callable[[], str],
    session_id_resolver: Callable[[], str],
    reconnect: Optional[Callable[[], Any]] = None,
    call_lease: Optional[Callable[[Any], Any]] = None,
    relink: Optional[Callable[[], Any]] = None,
) -> list:
    """Return a new tool list with trio members wrapped in proxies.

    Walks ``tools`` once, replacing each :class:`MCPAgentTool` whose
    name is the ``start_tool`` or ``results_tool`` of any trio in
    ``detection`` with a :class:`JobTrackingMCPAgentTool`. Non-trio
    tools and non-MCP tools are returned unchanged.

    This helper is idempotent in the sense that wrapping an already-
    wrapped tool (whose ``__class__`` is :class:`JobTrackingMCPAgentTool`)
    is a no-op — it is returned as-is. ``GatewayConnection`` rebuilds
    ``_all_tools`` from scratch on every connect/refresh, so the
    idempotency only matters defensively if a caller invokes
    :func:`wrap_trio_tools` twice without a tool-list refresh in
    between.

    Args:
        tools: The raw Strands tools list — typically
            :attr:`GatewayConnection._all_tools` after
            :meth:`MCPClient.list_tools_sync`.
        detection: The :class:`DetectionResult` produced by
            :meth:`GatewayConnection._run_job_detection` for the same
            tool list. Used to resolve which tool names are trio
            members and to stamp ``trio_id`` / ``gateway_id`` onto
            every wrapper.
        registry: Module-level :class:`JobRegistry` singleton from
            ``main.py`` / ``task_agent.py``.
        actor_id_resolver / session_id_resolver: Callables that read
            the per-request contextvars
            (:func:`agent.src.jobs.context.current_actor_id` /
            :func:`agent.src.jobs.context.current_session_id`).

    Returns:
        A new list, same length as ``tools``, with trio members
        replaced. The original list is not mutated; the caller
        decides whether to assign back to ``_all_tools``.
    """
    # Build per-name lookup maps so the wrap step is O(N) over tools.
    start_by_name: dict[str, JobTrio] = {
        t.start_tool: t for t in detection.trios
    }
    results_by_name: dict[str, JobTrio] = {
        t.results_tool: t for t in detection.trios
    }

    wrapped: list = []
    for tool in tools:
        # Idempotency guard — see docstring.
        if isinstance(tool, JobTrackingMCPAgentTool):
            wrapped.append(tool)
            continue

        # Only MCPAgentTools participate in trios; built-in / decorated
        # Strands tools (e.g. the ``artifact`` tool) flow through.
        if not isinstance(tool, MCPAgentTool):
            wrapped.append(tool)
            continue

        name = tool.tool_name
        if name in start_by_name:
            wrapped.append(JobTrackingMCPAgentTool(
                original=tool,
                role="start",
                trio=start_by_name[name],
                registry=registry,
                actor_id_resolver=actor_id_resolver,
                session_id_resolver=session_id_resolver,
                reconnect=reconnect,
                call_lease=call_lease,
                relink=relink,
            ))
            continue

        if name in results_by_name:
            wrapped.append(JobTrackingMCPAgentTool(
                original=tool,
                role="results",
                trio=results_by_name[name],
                registry=registry,
                actor_id_resolver=actor_id_resolver,
                session_id_resolver=session_id_resolver,
                reconnect=reconnect,
                call_lease=call_lease,
                relink=relink,
            ))
            continue

        # Status tools deliberately not wrapped — the polling worker
        # owns the status-fetch contract; the agent never calls the
        # status tool directly. If a future requirement adds a
        # status-tool side effect, we extend this branch.
        wrapped.append(tool)

    return wrapped


__all__ = [
    "JobTrackingMCPAgentTool",
    "ResilientMCPAgentTool",
    "wrap_trio_tools",
    "wrap_resilient",
    "_is_session_down_error",
]
