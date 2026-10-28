"""Task sub-agent — receives model, tools, skills, prompt and executes.

Loads built-in tools directly. Gateway tools come from the DDB-backed
gateway registry, the same source the Main Agent uses
(`MCPManager.load_gateways_from_registry`). The legacy single-gateway
``GATEWAYS`` env-var path was retired so registering a new gateway
through the admin UI makes its tools available to the task agent on
the next cold start, with no code change.

When the caller (Main Agent) requests a specific set of tools by name,
we resolve every name or fail the request — silently dropping a
missing tool was the bug that let the LLM fabricate tool calls and
emit hallucinated results in chat.
"""

import asyncio
import base64
import contextvars
import hashlib
import json
import logging
import os
import re
import time
from typing import Optional

import boto3
from strands import Agent, AgentSkills, Skill, tool
from strands.models import BedrockModel
import runtime_config_bootstrap  # noqa: F401,E402  — hydrate env before first-party imports (V2 1536B cap)
from model.factory import build_model
from strands_tools import file_read, http_request, current_time, load_tool, shell
from tools.result_files import read_result_files
from tools.crop_video import crop_video
from tools.media_quick import clip_video, extract_frames
from tools.media_gallery import show_media_gallery
from botocore.config import Config as BotoConfig
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)

from security import identity
from custom_agent_resolver import (
    resolve_config as _resolve_custom_agent_config,
    authorize_external_tools as _authorize_external_tools,
    external_opt_in_tool_names as _external_opt_in_tool_names,
    narrow_external_tools as _narrow_external_tools,
)
from security.redaction import redact_secrets as _redact_secrets
from security import compliance
from gateway.registry import GatewayRegistry
from mcp_manager import MCPManager, _user_token_is_expired
from orchestration.progress_channel import ProgressChannel, set_active_channel
from project_search.scrub import payload_summary as _payload_summary
from orchestration.step_labels import (
    coerce_input as _safe_input,
    has_input_payload,
    label_for_tool_done,
    label_for_tool_use,
    short_tool_name,
)
from prompts.task import DEFAULT_TASK_SYSTEM_PROMPT, ASYNC_JOBS_GUIDANCE_TEMPLATE

logging.basicConfig(level=logging.INFO)

# A plan node's durable output file is the AUTHORITATIVE handoff to downstream nodes, so a
# transient S3 error must not silently cost the write and leave the node "succeeded" with no
# result. Retry a few times before the node is failed.
_S3_WRITE_ATTEMPTS = 3
logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()
REGION = os.environ.get("AWS_REGION", "us-west-2")
S3_BUCKET = os.environ.get("SKILLS_S3_BUCKET", "")
UPLOADS_BUCKET = os.environ.get("UPLOADS_S3_BUCKET", "")
S3_PREFIX = os.environ.get("ARTIFACTS_PREFIX", "artifacts/")
ARTIFACTS_DOMAIN = os.environ.get("ARTIFACTS_DOMAIN", "")
DDB_TABLE = os.environ.get("PLAN_STATE_TABLE", "")
GATEWAY_REGISTRY_TABLE = os.environ.get("GATEWAY_REGISTRY_TABLE", "")
JOB_REGISTRY_TABLE = os.environ.get("JOB_REGISTRY_TABLE", "")
# AgentCore durable-memory store id. When set, external (principal-bearing)
# invocations get caller-scoped memory keyed on the segregated EXT# keyspace
# (task 12.2). When unset, the Task Agent degrades gracefully to no memory —
# same fallback shape as main.py.
MEMORY_ID = os.environ.get("MEMORY_ID", "")
# Lab asset store — where published Custom Agent personas, SKILL.md files, and
# the AssetIndex rows live. Read-only on this runtime: the external invoke
# path resolves a Custom Agent's published config server-side (persona, skill
# defs, tool collections) from these two stores (custom-agents seam fix; the
# proxy stamps only identity + agent_id/version, never config content).
ASSET_INDEX_TABLE = os.environ.get("ASSET_INDEX_TABLE", "")
LAB_ARTIFACTS_BUCKET = os.environ.get("ARTIFACTS_BUCKET", "")
# Optional comma-separated list of known Owner secret *values* to strip from
# external (Caller-visible) output via exact-match redaction (task 13.1,
# Req 11.2). Pattern-based redaction in ``redaction.redact_secrets`` always
# applies as a backstop; this env var is only for exact known values.
OWNER_REDACT_SECRETS = os.environ.get("OWNER_REDACT_SECRETS", "")
# Whole-text redaction of external (Caller-visible) output requires buffering the
# ENTIRE answer before emitting any of it, so redaction and streaming are the same
# decision — the patterns match whole credentials and cannot be applied per token.
# Default OFF: streaming is the product behavior in every environment. Set to
# "on"/"true"/"1" on the runtime to restore buffered, redacted output. Deliberate
# deviation from Req 11.2, recorded in
# docs/superpowers/specs/2026-09-01-custom-agent-streaming-design.md.
EXTERNAL_OUTPUT_REDACTION = os.environ.get(
    "EXTERNAL_OUTPUT_REDACTION", "off"
).strip().lower() in {"on", "true", "1"}
DEFAULT_MODEL_ID = os.environ.get("DEFAULT_MODEL_ID", "global.anthropic.claude-sonnet-5-5")
# Output token ceiling for a plan-chained task node (_execute_plan_node).
# Generic to any node type — not specific to translation or media — a
# node whose task legitimately produces a lot of output (long summaries,
# multi-language translation, generated reports) needs headroom above
# Bedrock's un-configured default. See the comment at node_model's
# construction in _execute_plan_node for the incident this fixes.
NODE_MAX_OUTPUT_TOKENS = int(os.environ.get("NODE_MAX_OUTPUT_TOKENS", "8192"))
# Wall-clock ceiling for a single plan node's execution (_execute_plan_node). A node's tool
# work — most sharply a code_interpreter cell (ffprobe/ASR/download) — can HANG with no bound:
# session 20f7315e's seg1_validate ran a code_interpreter probe that blocked ~15 min and never
# terminalized, so the whole plan hung RUNNING until TTL and the Phase 6 review never fired. Cap
# it so a hung node FAILS fast (a terminal status) and the plan-chainer's skip-dependents logic
# settles the plan. Generous enough for legitimate long code_interpreter work (stitch/probe);
# override per-env with PLAN_NODE_EXEC_TIMEOUT.
PLAN_NODE_EXEC_TIMEOUT = float(os.environ.get("PLAN_NODE_EXEC_TIMEOUT", "600"))
# The MAIN task-agent invocation (chat/external path) takes its cap from
# `model_factory.build_model` (BEDROCK_MAX_TOKENS, default 16384) rather than a
# constant here. It briefly had its own TASK_MAX_OUTPUT_TOKENS; the factory
# supersedes it, and routing through one seam is what keeps the ten
# construction sites from drifting apart.


def _output_format_for(output_type) -> str:
    """The node's OUTPUT FORMAT instruction, chosen by its declared output_type.

    A TEXT node emits plain markdown: its persisted RESPONSE is the deliverable, read downstream BY
    REFERENCE, so wrapping a long document in a {"text": <whole doc>} JSON envelope only guarantees
    the output-token cap mid-emission (the research->PDF failure, session 36c99c2d). Every other
    output_type (s3_uri / json / default) still emits the structured ```json block that downstream
    consumers depend on."""
    if str(output_type or "").strip().lower() == "text":
        return (
            "OUTPUT FORMAT: Emit the document as your NORMAL response text (markdown). Do NOT wrap "
            "it in a ```json block and do NOT put the document inside a JSON string — your full "
            "response text is stored and read by downstream steps BY REFERENCE. If it is long, "
            "write it in clearly delimited SECTIONS and stop at a section boundary; the platform "
            "continues you past the output-token cap and assembles the full text. NEVER substitute "
            "a summary, an outline, or a placeholder like \"(full report above)\" for the real content."
        )
    return (
        "OUTPUT FORMAT: At the end of your response, include a JSON block with the key outputs "
        "that downstream steps need. Wrap it in ```json\\n...\\n``` fences. Example:\n"
        '```json\n{"speech_stem_uri": "s3://bucket/path/audio_speech.wav", "duration_sec": 120}\n```\n'
        "If the output is a single URI, use: {\"uri\": \"s3://...\"}. "
        "This structured block is critical — downstream nodes depend on it."
    )


def _maxtokens_failure_output(stop_reason: str, text_chunks: list) -> Optional[str]:
    """P4 MaxTokens resilience. If a plan node's inline generation stopped at the output-
    token cap (``stop_reason == "max_tokens"``), its document is TRUNCATED — the
    compile_pdf/report failure class. Return an actionable error so the node is recorded
    failed (not silently completed with half a document) and P2's error-aware retry
    re-dispatches it to build the document OUT-OF-BAND (write the file with the
    code_interpreter and register it via the artifact tool's ``adopt``) or in sections.
    Returns None when the node was not truncated.
    """
    if stop_reason != "max_tokens":
        return None
    return (
        f"Error: your response hit the per-response output-token limit ({NODE_MAX_OUTPUT_TOKENS} "
        "tokens) and was cut off. This usually means you tried to emit the whole document at once — "
        "either as inline text OR as a single tool-call argument. Produce it as your NORMAL TEXT "
        "output; if it is long, write it in clearly delimited SECTIONS and stop at a section "
        "boundary — the platform automatically continues you past the limit and assembles the full "
        "result, then stores it. NEVER substitute a summary, an outline, or a placeholder such as "
        "\"(the full report is included above / in this response)\" for the actual content — the "
        "downstream step reads your REAL stored text by reference, so a placeholder makes it render "
        "an empty document. Emit the real content in full. Do NOT put the entire document inside a "
        "tool call (e.g. do not pass "
        "the whole report as a code_interpreter argument) — that hits the same limit mid-call and "
        "cannot be recovered. If you were asked to produce a PDF or other rendered artifact from this "
        "large document, it was mis-shaped as one step: emit the document as TEXT here, and let a "
        "SEPARATE downstream step READ this stored text BY REFERENCE (its result key / s3 uri) and "
        "render the artifact with code_interpreter — the renderer must load the text from the file, "
        "never receive it inline. Partial output so far follows:\n\n" + "".join(text_chunks)[:1500]
    )


def _is_maxtokens_error(message: str) -> bool:
    """True if an exception message indicates a max-tokens truncation. When a node truncates
    mid-generation or mid-tool-use, Strands raises 'Model stopped generating due to maximum
    token limit' rather than returning a clean stop_reason (the aws_mcp research node hit
    this), so the exception path must recognise it as the same F4 class."""
    m = (message or "").lower()
    return "maximum token" in m or "max_tokens" in m or "max tokens" in m


# How many times a node may auto-continue after truncating at the output-token cap. This
# is the canonical way to complete an output that exceeds one response; bounded so a
# runaway generation cannot loop forever (it then falls back to out-of-band guidance).
MAX_NODE_CONTINUATIONS = int(os.environ.get("NODE_MAX_CONTINUATIONS", "6"))


def _should_continue(stop_reason: str, continuations: int) -> bool:
    """Continue generation when the model stopped at the token cap and we are still under the
    continuation budget."""
    return stop_reason == "max_tokens" and continuations < MAX_NODE_CONTINUATIONS


async def _generate_with_continuation(node_agent, prompt, node_id: str = ""):
    """Stream a node's generation, auto-continuing when it truncates at the output-token cap.

    Canonical max_tokens handling: on stop_reason=='max_tokens' re-prompt the node agent to
    continue exactly where it stopped (the agent keeps its conversation, so the truncated
    response is already in history) and concatenate, bounded by MAX_NODE_CONTINUATIONS.

    Returns (output_text, final_stop_reason, continuations_used). A final_stop_reason of
    'max_tokens' means it was STILL truncated after exhausting the budget (caller then routes
    it out-of-band). Exceptions (e.g. a mid-tool-use truncation) propagate to the caller.
    """
    text_chunks: list[str] = []
    continuations = 0
    next_prompt = prompt
    stop_reason = ""
    while True:
        stop_reason = ""
        try:
            async for event in node_agent.stream_async(next_prompt):
                if "data" in event and isinstance(event["data"], str):
                    text_chunks.append(event["data"])
                elif "result" in event:
                    stop_reason = getattr(event["result"], "stop_reason", "") or ""
        except Exception as e:
            # A mid-stream truncation surfaces as an EXCEPTION ("...maximum token limit") rather
            # than a clean stop_reason. Treat it as max_tokens so the partial ALREADY accumulated
            # in text_chunks is KEPT and we continue — instead of aborting and losing everything,
            # which left the author node's persisted file holding only the error (session 36c99c2d
            # research->PDF). Any other exception is a real failure and still propagates.
            if _is_maxtokens_error(str(e)):
                stop_reason = "max_tokens"
            else:
                raise
        if not _should_continue(stop_reason, continuations):
            break
        continuations += 1
        logger.info("_execute_plan_node %s: hit output-token cap — auto-continuing (%d/%d)",
                    node_id, continuations, MAX_NODE_CONTINUATIONS)
        next_prompt = (
            "You were cut off at the output token limit mid-response. Continue EXACTLY "
            "where you stopped — do not repeat anything already written and add no preamble."
        )
    return "".join(text_chunks) or "(no output)", stop_reason, continuations
# ---- Async-plan delegated gateway auth (Wave 1 / option E) ----
# A plan node runs without the user's JWT. To reach a user_jwt-gated harness
# gateway it crosses the front door with a machine (client_credentials) token
# minted against the END-USER pool's async-plan client (whose id is in the
# gateway's allowedClients) and presents the signed delegation grant as the
# header the interceptor derives the actor from. These env vars name that
# client. Unset in dev → the delegated path is skipped and user_jwt gateways
# stay deferred (pre-option-E behavior), so this is strictly additive.
ASYNC_PLAN_TOKEN_ENDPOINT = os.environ.get("ASYNC_PLAN_TOKEN_ENDPOINT", "")
ASYNC_PLAN_CLIENT_ID = os.environ.get("ASYNC_PLAN_CLIENT_ID", "")
ASYNC_PLAN_CLIENT_SECRET = os.environ.get("ASYNC_PLAN_CLIENT_SECRET", "")
ASYNC_PLAN_SCOPE = os.environ.get("ASYNC_PLAN_SCOPE", "")
_boto_cfg = BotoConfig(read_timeout=300, connect_timeout=10, retries={"max_attempts": 4, "mode": "standard"})


def _int_env(name: str, default: int) -> int:
    """Parse a non-negative int env var, falling back to ``default``.

    A value of ``0`` disables the corresponding ceiling (unbounded).
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
        return v if v >= 0 else default
    except ValueError:
        return default


# ---- Per-invocation execution ceilings (task 14.1, Req 14.3 / 14.4) ----
# When a ceiling is reached the invocation is terminated and a terminal SSE
# ``error`` event is emitted. ``0`` disables a ceiling.
#
# The general (INTERNAL) ceilings are a GENEROUS RUNAWAY BACKSTOP, not a tuning
# knob: they exist only to stop a single node/dispatch turn from looping a tool
# forever. Run 863816b4 fired ``submit_job`` 236 times in ONE turn against a
# rejected endpoint id (fal returned "unavailable for the connected account",
# read by the model as insufficient_scope, and it kept retrying) — the internal
# ceilings were ``0`` (unbounded), so nothing stopped it and it burned calls
# then rolled up as a false SUCCEEDED. A finite cap turns that 236-loop into a
# bounded, terminal error the chainer can fail honestly.
#
# The cap is set WELL ABOVE any legitimate internal flow (a chatty in-node
# poll loop is "tens of cycles"), so happy-path turns are untouched — it only
# fires on a genuine runaway. The EXT_* ceilings still apply (tighter) on the
# external path (a ``principal`` is stamped); each falls back to the general
# ceiling when its EXT_* var is unset.
MAX_CYCLES = _int_env("TASK_AGENT_MAX_CYCLES", 100)
MAX_TOKENS = _int_env("TASK_AGENT_MAX_TOKENS", 0)
MAX_WALL_CLOCK_SEC = _int_env("TASK_AGENT_MAX_WALL_CLOCK_SEC", 900)
EXT_MAX_CYCLES = _int_env("TASK_AGENT_EXT_MAX_CYCLES", 50)
EXT_MAX_TOKENS = _int_env("TASK_AGENT_EXT_MAX_TOKENS", 400_000)
EXT_MAX_WALL_CLOCK_SEC = _int_env("TASK_AGENT_EXT_MAX_WALL_CLOCK_SEC", 240)


def _select_ceilings(is_external: bool) -> dict:
    """Resolve the active per-invocation ceilings for this call.

    External (principal-bearing) invocations use the EXT_* ceilings, each
    falling back to the general ceiling when its EXT_* var is unset (``0``).
    Internal invocations use the general ceilings (unbounded by default).
    """
    if is_external:
        return {
            "cycles": EXT_MAX_CYCLES or MAX_CYCLES,
            "tokens": EXT_MAX_TOKENS or MAX_TOKENS,
            "wall_clock_sec": EXT_MAX_WALL_CLOCK_SEC or MAX_WALL_CLOCK_SEC,
        }
    return {
        "cycles": MAX_CYCLES,
        "tokens": MAX_TOKENS,
        "wall_clock_sec": MAX_WALL_CLOCK_SEC,
    }


# ---- Base system prompt + async-job contract ----
# Extracted to module scope so ``_resolve_system_prompt`` can compose them and
# tests can assert on each part independently.

# The async-job contract. ``main.py`` has always carried an equivalent block for
# the orchestrator, but this runtime — which hosts every Custom Agent — had
# none, and a caller-supplied ``system_prompt`` (i.e. a persona) replaces the
# default wholesale. External callers therefore ran with no polling instruction
# at all and looped on a gateway ``*_get_job_status`` tool until the EXT token
# ceiling terminated the invocation (observed: 17 cycles / 418k tokens against a
# 400k limit, every poll returning a byte-identical ``RUNNING``).
#
# Scoped to what THIS runtime can do: no ``list_my_jobs`` / ``get_job`` (bound on
# the Main Agent, not here) and no ``dispatch_tool`` (the Task Agent is the
# dispatch target). Naming absent tools would contradict the TOOL USAGE RULES
# above.
# Template: ``{download_base}`` is filled in per-invocation by
# :func:`_async_jobs_guidance` with this agent's real download path. The model
# has no other way to learn its own agent id — asked to "use the agent_id from
# this invocation" it substituted the persona's display name instead, producing
# links that fell through CloudFront to the SPA and returned HTML.


def _async_jobs_guidance(agent_id: str = "", conversation_id: str = "") -> str:
    """Render the async-job guidance for a specific agent and conversation.

    ``agent_id`` is the published Custom Agent id the proxy stamped on this
    invocation. When present the download path is concrete, so the model copies
    it verbatim instead of inventing an identifier. When absent (internal
    dispatch, tests) the placeholder form is kept — a link the model builds from
    it is no worse than the one it would have guessed.

    ``conversation_id`` is the caller's optional conversation nonce (from the
    stamped principal). The download route serves a caller only the work of the
    ONE derived session a link names, and that session folds the conversation in,
    so a link written in a named conversation must carry it or it would not open
    the caller's own output. Sanitized exactly as the proxy does, so the session
    the route re-derives is the one this invocation runs under.
    """
    base = (
        f"/invoke/{agent_id}/download"
        if agent_id
        else "/invoke/<agent_id>/download"
    )
    conv = re.sub(r"[^A-Za-z0-9._-]+", "", conversation_id or "")[:64]
    link = f"{base}?conversation_id={conv}&ref=" if conv else f"{base}?ref="
    return ASYNC_JOBS_GUIDANCE_TEMPLATE.format(download_link=link)


# Rendered with the placeholder — kept so callers/tests that just want the text
# (rather than a per-agent rendering) keep working.
ASYNC_JOBS_GUIDANCE = _async_jobs_guidance()


def _resolve_system_prompt(payload: dict) -> str:
    """Resolve the system prompt for this invocation.

    Honors a caller-supplied ``system_prompt`` (the external resolver writes
    ``persona.md`` here), falling back to :data:`DEFAULT_TASK_SYSTEM_PROMPT`.

    For external invocations the async-job contract is **appended** rather than
    left to the caller, because a persona otherwise replaces the default and
    silently drops it — precisely on the path where the bounded EXT_* ceilings
    make a status-poll loop fatal. Internal invocations are returned byte-identical
    so existing dispatch behavior is not regressed.

    Externality is ``isinstance(principal, dict)``, matching
    :func:`_select_ceilings` and :func:`_usage_attribution`.
    """
    system_prompt = payload.get("system_prompt", DEFAULT_TASK_SYSTEM_PROMPT)
    # Append the SESSION-SCOPED file-output guidance for every invocation (default OR persona), so
    # code_interpreter writes directly to artifacts/<session>/output/. session_id is read from the
    # payload because the request context is not bound until after this prompt is built.
    # The project is the dispatcher-forwarded `producing_project_id` (the trusted source for a task
    # agent / plan node, asset-lineage Phase 3 (a)); read explicitly, as the contextvar is bound later.
    _pp = payload.get("producing_project_id")
    parts = [system_prompt, _s3_context_guidance(
        str(payload.get("session_id") or ""),
        _pp if isinstance(_pp, str) and _pp else "")]
    # Async-job contract, gated on the CAPABILITY need rather than identity: it is required by
    #   (a) external (principal-bearing) invocations — the bounded EXT_* ceilings make a naive
    #       status-poll loop fatal, and a persona would otherwise drop the default; and
    #   (b) plan nodes — they submit gateway jobs and must poll -> fetch results, not treat a
    #       job_id/ARN as the deliverable. Gating on `principal` alone starved plan nodes of it,
    #       so an async-submitting node could satisfy a text contract with prose (the stitch race).
    # A plan-node invocation is identified by its plan_id + node_id.
    if isinstance(payload.get("principal"), dict) or (payload.get("plan_id") and payload.get("node_id")):
        _principal = payload.get("principal") if isinstance(payload.get("principal"), dict) else {}
        parts.append(_async_jobs_guidance(
            str(payload.get("agent_id") or ""),
            str(_principal.get("conversation_id") or ""),
        ))
    return "\n\n".join(parts)


def _usage_attribution(
    *,
    agent_id: str,
    principal,
    owner_actor_id: str,
    usage: dict,
    cycles: int,
    duration_ms: int,
    terminated: bool,
    ceiling: Optional[str] = None,
) -> dict:
    """Attribute an external invocation's cost/usage on completion/termination.

    Req 14.5: attribute to ``(agentId, End_User)`` — **Owner** carries the quota
    attribution, the issuer-qualified **Principal** carries the usage record.
    Emits a structured log line keyed by ``agentId`` + End_User and returns the
    attribution dict for inclusion in the ``execution_summary``. Returns ``{}``
    for internal (no-principal) invocations, which are unchanged.
    """
    if not isinstance(principal, dict):
        return {}
    issuer = principal.get("issuer") or ""
    sub = principal.get("sub") or ""
    end_user = f"{issuer}#{sub}" if issuer or sub else ""
    attribution = {
        "agent_id": agent_id,
        "end_user": end_user,          # usage records → Principal (issuer-qualified)
        "quota_owner": owner_actor_id,  # quota → Owner (resource actor)
        "usage": usage or {},
        "cycles": cycles,
        "duration_ms": duration_ms,
        "terminated": terminated,
    }
    if ceiling:
        attribution["ceiling"] = ceiling
    try:
        logger.info(
            "usage_attribution agent_id=%s end_user=%s quota_owner=%s "
            "total_tokens=%s cycles=%d duration_ms=%d terminated=%s ceiling=%s",
            agent_id,
            end_user,
            owner_actor_id,
            (usage or {}).get("totalTokens", 0),
            cycles,
            duration_ms,
            terminated,
            ceiling or "none",
        )
    except Exception:  # noqa: BLE001 — attribution logging must never break invoke
        logger.debug("usage_attribution logging failed", exc_info=True)
    return attribution


def _owner_known_secrets() -> list[str]:
    """Known Owner secret values to strip from external output (Req 11.2).

    Sourced from the ``OWNER_REDACT_SECRETS`` env var (comma-separated). Returns
    an empty list when unset — pattern-based redaction still applies. Kept as a
    tiny seam so a future task can source secrets from a manager instead.
    """
    if not OWNER_REDACT_SECRETS.strip():
        return []
    return [s.strip() for s in OWNER_REDACT_SECRETS.split(",") if s.strip()]

# ---- async-job-tracking JobRegistry singleton (mirrors main.py) ----
# The Task Agent shares the per-tool job-tracking wrap with the Main
# Agent: any `start_*` MCP tool dispatched by the sub-agent needs to
# land in the same DDB-backed registry the sidebar reads from. We
# construct the singleton once at module import so the wrap wiring
# in ``_ensure_gateways_loaded`` doesn't pay the boto3 resource cost
# on every invocation. When ``JOB_REGISTRY_TABLE`` is unset (dev
# environments without the registry table), the singleton stays None
# and the wrap wiring is skipped — the task agent then runs without
# job tracking, same fallback shape as main.py.
from jobs.registry import JobRegistry  # noqa: E402 — local module

_job_registry: Optional[JobRegistry] = None
if JOB_REGISTRY_TABLE:
    try:
        _job_registry = JobRegistry(table_name=JOB_REGISTRY_TABLE, region=REGION)
        logger.info(
            "TaskAgent JobRegistry initialized against table %s",
            JOB_REGISTRY_TABLE,
        )
    except Exception as exc:
        logger.error(
            "TaskAgent JobRegistry init failed for table %s: %s; "
            "per-tool job tracking will not be installed",
            JOB_REGISTRY_TABLE, exc,
        )
        _job_registry = None
else:
    logger.warning(
        "TaskAgent: JOB_REGISTRY_TABLE not set; per-tool job tracking "
        "will not be installed and async job tracking is disabled for this runtime"
    )

# --- Code interpreter (optional, cached) ---
_code_interpreter_tool = None
try:
    from strands_tools.code_interpreter import AgentCoreCodeInterpreter
    _ci_id = os.environ.get("CODE_INTERPRETER_ID", "")
    _ci = AgentCoreCodeInterpreter(region=REGION, identifier=_ci_id) if _ci_id else AgentCoreCodeInterpreter(region=REGION)
    # Coerce a model-stringified `code_interpreter_input` back to a dict before the
    # tool validates it (the sub-agent inherits the orchestrator's model, so it hits
    # the same intermittent stringification / retry storm otherwise).
    from tool_input_normalize import wrap_code_interpreter
    _code_interpreter_tool = wrap_code_interpreter(_ci.code_interpreter)
    logger.info("Code interpreter ready")
except Exception as e:
    logger.warning("Code interpreter unavailable: %s", e)

# --- S3 artifact tool ---
_s3 = boto3.client("s3", region_name=REGION) if S3_BUCKET else None

_MIME_BY_EXT = {
    "pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "mp4": "video/mp4", "webm": "video/webm", "mov": "video/quicktime",
    "mp3": "audio/mpeg", "wav": "audio/wav", "flac": "audio/flac",
    "json": "application/json", "csv": "text/csv", "txt": "text/plain",
    "py": "text/x-python", "ts": "text/typescript", "js": "application/javascript",
    "html": "text/html", "xml": "application/xml", "zip": "application/zip",
}


_MAX_INLINE_UPLOAD_B64_CHARS = 16384


def _artifact_session_id(session_id: str = "") -> str:
    sid = (session_id or "").strip()
    if not sid:
        try:
            from jobs import context as _jc
            sid = (_jc.current_session_id() or "").strip()
        except Exception:  # noqa: BLE001 - context may be unbound; fall back safely
            sid = ""
    return sid.replace("/", "_").strip() if sid else ""


def _artifact_output_prefix(session_id: str = "", project_id: Optional[str] = None) -> str:
    """The consistent, session-scoped OUTPUT location for generated artifacts.

    ``projects/<pid>/sessions/<sid>/output/`` where the env opts into the project layout and a
    trusted project is known (asset-lineage Phase 3 (a)), else ``artifacts/<session_id>/output/``.
    Prefers an explicit ``session_id`` / ``project_id`` — the invoke handler builds the system prompt
    BEFORE binding the request context, so the contextvars are not yet set there — else the request
    context (``jobs.context.trusted_project_id``: the forwarded ``producing_project_id``), else the
    flat ``ARTIFACTS_PREFIX`` when no session is known, so it never raises."""
    from projects.storage_layout import session_output_prefix

    if project_id is None:
        try:
            from jobs import context as _jc
            project_id = _jc.trusted_project_id()
        except Exception:  # noqa: BLE001
            project_id = None
    return session_output_prefix(_artifact_session_id(session_id), project_id, artifacts_prefix=S3_PREFIX)


def _legacy_output_prefix(session_id: str = "") -> str:
    """``artifacts/<sid>/output/`` (or flat): where this session's pre-Phase-3 outputs live."""
    sid = _artifact_session_id(session_id)
    return f"{S3_PREFIX}{sid}/output/" if sid else S3_PREFIX


def _s3_context_guidance(session_id: str = "", project_id: Optional[str] = None) -> str:
    """Session-scoped S3/file-output guidance, built per invocation.

    Directs code_interpreter to write files DIRECTLY to the managed, session-scoped output
    location ``artifacts/<session>/output/`` — the same place the plan-node path writes — instead
    of a flat ``artifacts/<file>`` key that then needs ``adopt`` to become servable. A direct
    write lands in the CloudFront-served managed location, so the render/download URL resolves
    immediately: no flat key, no wrong/presigned URL, no duplicate render tab, and no dependency on
    the model remembering to call ``adopt``."""
    out = _artifact_output_prefix(session_id, project_id)
    _pfx = S3_PREFIX.rstrip("/")  # the configured prefix name (default "artifacts"); never hardcode it
    url_hint = f" Its permanent URL is `https://{ARTIFACTS_DOMAIN}/{out}<filename>`." if ARTIFACTS_DOMAIN else ""
    return (
        f"S3 CONTEXT: the workspace bucket is `{S3_BUCKET}` in region `{REGION}` — create your boto3 "
        f"S3 client with `region_name='{REGION}'` (the bucket is regional; a client with a different "
        f"default region can fail the write with PermanentRedirect). To produce a file, write it from "
        f"code_interpreter with boto3 DIRECTLY to `s3://{S3_BUCKET}/{out}<filename>`, passing the "
        f"correct ContentType for the file (e.g. `application/pdf`, `image/png`).{url_hint} That "
        f"path is the managed, session-scoped OUTPUT location and is served as-is — do NOT write to "
        f"a flat `{_pfx}/<filename>` key. `{out.rstrip('/')}` is a KEY PREFIX in `{S3_BUCKET}`, NOT "
        f"a bucket — never write to a bucket literally named `{_pfx}` and never invent bucket names; "
        f"read inputs from the exact s3:// URIs given in the prompt. (artifact(operation='adopt', "
        f"key='<filename>') still returns the URL, but a direct write to the path above is already "
        f"durable.) Create the boto3 client inside the SAME code_interpreter cell that uses it: the "
        f"sandbox's S3 credentials are scoped to this session and rotate, so a client kept from an "
        f"earlier cell can fail with ExpiredToken/AccessDenied, and a write outside the OUTPUT prefix "
        f"above is denied."
    )


@tool
def artifact(operation: str, key: str = "", data_b64: str = "", content_type: str = "", source_uri: str = "") -> str:
    """Upload (tiny inline only), adopt, import, download, or list files in S3 artifact storage.
    Args:
        operation: 'upload' | 'adopt' | 'import' | 'download' | 'list'
        key: filename relative to the artifacts prefix (no prefix). For 'import', OPTIONAL dest filename.
        data_b64: base64 content for 'upload' — TINY files only (< ~12 KB). For anything larger,
            and ALWAYS for a code_interpreter-produced file (games, apps, HTML, PDFs, charts),
            do NOT relay bytes here (it blows the output-token limit) — from code_interpreter write
            the file DIRECTLY to the session OUTPUT prefix given in the S3 CONTEXT guidance, via
            boto3; that is already durable, so operation='adopt' is optional.
        content_type: MIME type for upload/adopt/import. Auto-detected from extension if not provided.
        source_uri: for 'import' — either a full ``s3://bucket/key`` URI (e.g. an M2C
            micro-workflow result in the proxy bucket, copied server-side) OR an ``https://`` URL
            (e.g. a fal generation output) which is fetched and stored durably. Use this to persist
            a generated image/video into the session output prefix from a plan node.
    """
    prefix = _artifact_output_prefix()
    legacy_prefix = _legacy_output_prefix()
    # Robustly strip ANY leading copy of our known prefixes so the prefix is never DOUBLED
    # (a "<session>/output/report.pdf" key must not become artifacts/<session>/output/<session>/
    # output/report.pdf — that 404s and the renderer falls back to the harness SPA).
    _sess_seg = legacy_prefix[len(S3_PREFIX):] if legacy_prefix.startswith(S3_PREFIX) else ""
    _changed = True
    while _changed:
        _changed = False
        for _p in (prefix, legacy_prefix, _sess_seg, S3_PREFIX):
            if _p and key.startswith(_p):
                key = key[len(_p):]
                _changed = True
                break
    # A 'download' whose key is actually an s3://bucket/key URI names an object in ANOTHER bucket
    # (an M2C/gateway result). Building an artifacts CloudFront URL for it returns a dead 403 that
    # gets baked into render manifests (prod session 1792bb4d). Route it through the 'import' path,
    # which server-side copies the object in BY KEY and returns a URL that actually resolves.
    if operation == "download" and (key or "").strip().startswith("s3://"):
        source_uri = (key or "").strip()
        key = ""
        operation = "import"
    if operation == "upload" and len(data_b64 or "") > _MAX_INLINE_UPLOAD_B64_CHARS:
        fk = f"{prefix}{key}"
        return json.dumps({"error": (
            "INLINE_UPLOAD_TOO_LARGE: do NOT base64-relay this file — it blows the output-token "
            "limit and can corrupt on transcription. From code_interpreter write it to "
            f"s3://{S3_BUCKET}/{fk} via boto3, then call artifact(operation='adopt', key='{key}'). "
            "This is the required path for any code_interpreter-produced file.")})
    if not _s3 or not S3_BUCKET:
        return json.dumps({"error": "S3 not configured"})
    if operation == "upload":
        fk = f"{prefix}{key}"
        if not content_type:
            ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
            content_type = _MIME_BY_EXT.get(ext, "application/octet-stream")
        _s3.put_object(Bucket=S3_BUCKET, Key=fk, Body=base64.b64decode(data_b64), ContentType=content_type)
        if ARTIFACTS_DOMAIN:
            url = f"https://{ARTIFACTS_DOMAIN}/{fk}"
        else:
            url = _s3.generate_presigned_url("get_object", Params={"Bucket": S3_BUCKET, "Key": fk}, ExpiresIn=3600)
        return json.dumps({"status": "uploaded", "key": fk, "url": url})
    elif operation == "adopt":
        # Register a file code_interpreter already wrote to the artifacts prefix — by KEY, no bytes.
        # The escape hatch for large files: relaying them as data_b64 blows the output-token cap and
        # corrupts on transcription. Backfills content-type so CloudFront serves it renderable.
        fk = f"{prefix}{key}"
        src_key = fk
        try:
            head = _s3.head_object(Bucket=S3_BUCKET, Key=fk)
        except Exception as e:  # noqa: BLE001 - clear, actionable message
            # The file may have been written to the legacy session prefix (older skill text) or the
            # flat artifacts/ prefix instead of the session output prefix; find it there and
            # relocate into the canonical key.
            head = None
            for alt_key in dict.fromkeys((f"{legacy_prefix}{key}", f"{S3_PREFIX}{key}")):
                if alt_key == fk:
                    continue
                try:
                    head = _s3.head_object(Bucket=S3_BUCKET, Key=alt_key)
                    src_key = alt_key
                    break
                except Exception:  # noqa: BLE001 - genuinely absent
                    head = None
            if head is None:
                return json.dumps({"error": (
                    f"No object at '{fk}' to adopt ({e}). From code_interpreter, write the file to "
                    f"s3://{S3_BUCKET}/{fk} via boto3 first, then adopt it by filename.")})
        existing_ct = head.get("ContentType") or ""
        ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
        ct = content_type or _MIME_BY_EXT.get(ext, "") or existing_ct or "application/octet-stream"
        if src_key != fk:
            try:
                _s3.copy_object(Bucket=S3_BUCKET, Key=fk, CopySource={"Bucket": S3_BUCKET, "Key": src_key},
                                ContentType=ct, MetadataDirective="REPLACE")
            except Exception:  # noqa: BLE001 - non-fatal; serve from where it is
                fk = src_key
        elif ct != existing_ct:
            try:
                _s3.copy_object(Bucket=S3_BUCKET, Key=fk, CopySource={"Bucket": S3_BUCKET, "Key": fk},
                                ContentType=ct, MetadataDirective="REPLACE")
            except Exception:  # noqa: BLE001 - non-fatal; the URL still resolves
                pass
        if ARTIFACTS_DOMAIN:
            url = f"https://{ARTIFACTS_DOMAIN}/{fk}"
        else:
            url = _s3.generate_presigned_url("get_object", Params={"Bucket": S3_BUCKET, "Key": fk}, ExpiresIn=3600)
        return json.dumps({"status": "adopted", "key": fk, "url": url, "content_type": ct})
    elif operation == "import":
        # Server-side copy an object from another readable bucket (the M2C proxy bucket holds
        # micro-workflow result files) into our artifacts prefix — by key, no bytes through the
        # model. Makes M2C results (probe.json, shots.json, facemetadata.json, stems, .wav)
        # downloadable/playable via our CloudFront instead of an un-fetchable s3:// URI.
        src = (source_uri or "").strip()
        if src.startswith("https://"):
            # One of OUR artifact URLs (CloudFront or /api/artifact?key=): copy it server-side
            # by key via the s3:// branch below instead of fetching it back over HTTP — the
            # CDN may be private, and no bytes need to pass through this process.
            from artifact_import import own_artifact_s3_uri
            src = own_artifact_s3_uri(src, S3_BUCKET, ARTIFACTS_DOMAIN) or src
        if src.startswith("https://"):
            # Fetch a third-party https artifact (a fal.media generation output, or our own
            # CloudFront mirror of one) and store it durably in the output prefix. This is the
            # reliable https->s3 persist for image-to-image / generation plan nodes, which have
            # no code_interpreter and used to guess the auto-mirrored s3 key (run 627fb96e: 6/8
            # oil-paint nodes persisted nothing because `import` only took s3://).
            from artifact_import import fetch_https_bytes
            dest_name = (key or src.split("?", 1)[0].rsplit("/", 1)[-1] or "imported.bin").rsplit("/", 1)[-1]
            fk = f"{prefix}{dest_name}"
            ext = dest_name.rsplit(".", 1)[-1].lower() if "." in dest_name else ""
            try:
                body, remote_ct = fetch_https_bytes(src)
            except Exception as e:  # noqa: BLE001 - clear, actionable message
                return json.dumps({"error": f"import failed fetching {src} ({e})"})
            ct = content_type or _MIME_BY_EXT.get(ext, "") or remote_ct or "application/octet-stream"
            _s3.put_object(Bucket=S3_BUCKET, Key=fk, Body=body, ContentType=ct)
            if ARTIFACTS_DOMAIN:
                url = f"https://{ARTIFACTS_DOMAIN}/{fk}"
            else:
                url = _s3.generate_presigned_url("get_object", Params={"Bucket": S3_BUCKET, "Key": fk}, ExpiresIn=3600)
            return json.dumps({"status": "imported", "key": fk, "url": url, "content_type": ct})
        if not src.startswith("s3://") or "/" not in src[len("s3://"):]:
            return json.dumps({"error": "import requires source_uri as a full s3://bucket/key or https:// URI"})
        src_bucket, src_key = src[len("s3://"):].split("/", 1)
        dest_name = (key or src_key.rsplit("/", 1)[-1]).rsplit("/", 1)[-1]
        fk = f"{prefix}{dest_name}"
        ext = dest_name.rsplit(".", 1)[-1].lower() if "." in dest_name else ""
        ct = content_type or _MIME_BY_EXT.get(ext, "application/octet-stream")
        try:
            _s3.copy_object(Bucket=S3_BUCKET, Key=fk,
                            CopySource={"Bucket": src_bucket, "Key": src_key},
                            ContentType=ct, MetadataDirective="REPLACE")
        except Exception as e:  # noqa: BLE001 - clear, actionable message
            return json.dumps({"error": (
                f"import failed copying s3://{src_bucket}/{src_key} ({e}). The runtime can only "
                "read buckets it has been granted (e.g. the M2C proxy bucket).")})
        if ARTIFACTS_DOMAIN:
            url = f"https://{ARTIFACTS_DOMAIN}/{fk}"
        else:
            url = _s3.generate_presigned_url("get_object", Params={"Bucket": S3_BUCKET, "Key": fk}, ExpiresIn=3600)
        return json.dumps({"status": "imported", "key": fk, "url": url, "content_type": ct})
    elif operation == "download":
        fk = f"{prefix}{key}"
        # Verify the object exists at the expected key BEFORE returning a URL. An unverified
        # artifacts URL for a key that actually lives in another bucket returns a dead 403 that
        # then gets baked into render manifests (prod session 1792bb4d). On a miss, point the
        # model at 'import' (server-side copy) rather than handing back a URL that 403s.
        try:
            _s3.head_object(Bucket=S3_BUCKET, Key=fk)
        except Exception:  # noqa: BLE001 - absent/unreadable; return an actionable error, not a dead URL
            fk = None
            # An output this session wrote before the project layout (asset-lineage Phase 3 (a)).
            if legacy_prefix != prefix:
                try:
                    _s3.head_object(Bucket=S3_BUCKET, Key=f"{legacy_prefix}{key}")
                    fk = f"{legacy_prefix}{key}"
                except Exception:  # noqa: BLE001 - genuinely absent
                    fk = None
        if fk is None:
            fk = f"{prefix}{key}"
            return json.dumps({"error": (
                f"No object at s3://{S3_BUCKET}/{fk}. If the file is an M2C/gateway result in "
                "another bucket, call artifact(operation='import', "
                "source_uri='s3://<bucket>/<key>').")})
        if ARTIFACTS_DOMAIN:
            url = f"https://{ARTIFACTS_DOMAIN}/{fk}"
        else:
            url = _s3.generate_presigned_url("get_object", Params={"Bucket": S3_BUCKET, "Key": fk}, ExpiresIn=3600)
        return json.dumps({"key": fk, "url": url})
    elif operation == "list":
        # Both roots: a session that spans the layout flip keeps its pre-flip outputs listable
        # (asset-lineage Phase 3 (a); adopt/download already fall back the same way).
        keys: list = []
        for _pfx in dict.fromkeys((prefix, legacy_prefix)):
            r = _s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=_pfx, MaxKeys=50)
            keys.extend(o["Key"] for o in r.get("Contents", []))
        return json.dumps({"keys": keys[:50]})
    return json.dumps({"error": f"Unknown: {operation}"})


# --- Built-in tools (instant, no network) ---
_BUILTINS: dict = {
    "file_read": file_read,
    "http_request": http_request,
    # One concurrent call instead of one round-trip per file.
    "read_result_files": read_result_files,
    # Deterministic arbitrary-box video crop (typed tool; pass the UI box verbatim).
    "crop_video": crop_video,
    "extract_frames": extract_frames,
    "clip_video": clip_video, "show_media_gallery": show_media_gallery,
    "current_time": current_time,
    "load_tool": load_tool,
    "shell": shell,
    "artifact": artifact,
}
if _code_interpreter_tool:
    _BUILTINS["code_interpreter"] = _code_interpreter_tool
# Private artifacts CDN: `http_request` on one of OUR artifact URLs gets a presigned S3 URL
# (GET or HEAD) instead of a 403 from CloudFront. No-op unless ARTIFACTS_CDN_PRIVATE.
from tool_input_normalize import wrap_http_request  # noqa: E402

_BUILTINS["http_request"] = wrap_http_request(http_request)


# ---------------------------------------------------------------------------
# Gateway registry singleton (same pattern as main.py)
#
# The DDB-backed registry replaces the legacy ``GATEWAYS`` env-var path.
# When the registry table isn't bound (dev runs against a stack without
# the gateway-registry-admin migration applied), the runtime starts with
# zero gateway tools — built-in tools still work.
# ---------------------------------------------------------------------------

_gateway_registry: Optional[GatewayRegistry] = None
if GATEWAY_REGISTRY_TABLE:
    _gw_ddb = boto3.resource("dynamodb", region_name=REGION)
    _gateway_registry = GatewayRegistry(
        ddb_table=_gw_ddb.Table(GATEWAY_REGISTRY_TABLE),
    )
    logger.info("GatewayRegistry initialized against table %s", GATEWAY_REGISTRY_TABLE)
else:
    logger.warning(
        "GATEWAY_REGISTRY_TABLE not set; task agent will start with zero gateway tools"
    )

mcp_manager = MCPManager()
_gateway_initialized = False
_gateway_init_lock = asyncio.Lock()
# Invocation-local expected tenant-gateway credential. This prevents a warm
# task with no usable credential from inheriting tools connected for an earlier
# invocation, even though the MCP manager itself is process-global.
_gateway_auth_fingerprint: contextvars.ContextVar[tuple[str, str] | None] = (
    contextvars.ContextVar("task_gateway_auth_fingerprint", default=None)
)


def _credential_fingerprint(mode: str, credential: str) -> tuple[str, str] | None:
    if not credential:
        return None
    return mode, hashlib.sha256(credential.encode("utf-8")).hexdigest()


def _active_gateway_fingerprint(gw) -> tuple[str, str] | None:
    if not gw.is_connected:
        return None
    bound_user_token = getattr(gw, "_bound_user_token", "") or ""
    if bound_user_token:
        return _credential_fingerprint("user", bound_user_token)
    grant = getattr(gw, "delegation_grant", "") or ""
    if grant:
        return _credential_fingerprint("grant", grant)
    return None


async def _connect_delegated_gateways(delegation_grant: str) -> bool:
    """Connect ``user_jwt`` gateways for an async plan node via machine token + grant.

    Wave 1 / option E. A plan node carries a signed delegation grant but no
    user JWT; this mints a machine token against the async-plan client and
    hands both to :meth:`MCPManager.bind_delegation_grant_and_connect`, which
    connects the harness gateway with the machine token as the front-door
    credential and the grant as ``DELEGATION_GRANT_HEADER``.

    No-op when the async-plan machine creds aren't configured (dev), so the
    user_jwt gateways simply stay deferred — the pre-option-E behavior — rather
    than erroring.
    """
    if not delegation_grant:
        return False
    if not (ASYNC_PLAN_TOKEN_ENDPOINT and ASYNC_PLAN_CLIENT_ID and ASYNC_PLAN_CLIENT_SECRET):
        logger.warning(
            "delegation grant present but async-plan machine creds are not "
            "configured (ASYNC_PLAN_TOKEN_ENDPOINT / ASYNC_PLAN_CLIENT_ID / "
            "ASYNC_PLAN_CLIENT_SECRET); user_jwt gateways stay deferred"
        )
        return False
    try:
        await asyncio.to_thread(
            lambda: mcp_manager.bind_delegation_grant_and_connect(
                delegation_grant,
                token_endpoint=ASYNC_PLAN_TOKEN_ENDPOINT,
                client_id=ASYNC_PLAN_CLIENT_ID,
                client_secret=ASYNC_PLAN_CLIENT_SECRET,
                scope=ASYNC_PLAN_SCOPE,
            )
        )
        expected = _credential_fingerprint("grant", delegation_grant)
        return any(
            gw.auth_type == "user_jwt"
            and _active_gateway_fingerprint(gw) == expected
            for gw in mcp_manager._gateways.values()
        )
    except Exception as e:  # noqa: BLE001 — connect failure must not break the node
        logger.warning("delegated gateway connect failed: %s", e)
        return False


async def _bind_invocation_gateway_auth(
    *, user_token: str = "", delegation_grant: str = ""
) -> bool:
    """Apply this invocation's auth context to an already-loaded manager.

    A delegation grant belongs to detached work and takes precedence over any
    user token that happens to share the payload. Interactive calls use the
    current user token only when no grant is present.
    """
    expected = (
        _credential_fingerprint("grant", delegation_grant)
        if delegation_grant
        else _credential_fingerprint("user", user_token)
    )
    _gateway_auth_fingerprint.set(expected)

    if delegation_grant:
        return await _connect_delegated_gateways(delegation_grant)
    if user_token:
        try:
            await asyncio.to_thread(
                mcp_manager.bind_user_token_and_connect, user_token
            )
            return any(
                gw.auth_type == "user_jwt"
                and _active_gateway_fingerprint(gw) == expected
                for gw in mcp_manager._gateways.values()
            )
        except Exception as e:
            logger.warning("user_jwt retry failed: %s", e)
    return False


async def _ensure_gateways_loaded(
    actor_id: str = "", user_token: str = "", delegation_grant: str = ""
) -> None:
    """Cold-load gateways from the registry on first invocation.

    Mirrors ``main.py::_ensure_gateway_connected`` so the task agent
    sees the same gateways the Main Agent sees. Idempotent and
    concurrency-safe — the lock guards against two concurrent invokes
    racing to load the same registry on a freshly-warmed runtime.

    ``delegation_grant`` (async plan nodes only) arms the machine-token +
    grant inbound path for ``user_jwt`` gateways so a node with no user token
    can still reach a user-gated tool (Wave 1 / option E).
    """
    global _gateway_initialized
    if user_token and _user_token_is_expired(user_token):
        # A forwarded token that has already lapsed can only earn a front-door 403
        # (insufficient_scope). Treat it as absent: a grant (if present) still connects, and a
        # warm connection is never re-pinned to a dead bearer.
        logger.warning("TaskAgent: ignoring an expired forwarded user token")
        user_token = ""
    expected = (
        _credential_fingerprint("grant", delegation_grant)
        if delegation_grant
        else _credential_fingerprint("user", user_token)
    )
    _gateway_auth_fingerprint.set(expected)
    if _gateway_initialized:
        # The latch is per container while credentials arrive per dispatch.
        # Re-apply this invocation's auth context every time. Grant-bearing
        # detached work deliberately outranks a user token.
        await _bind_invocation_gateway_auth(
            user_token=user_token, delegation_grant=delegation_grant
        )
        return
    async with _gateway_init_lock:
        if _gateway_initialized:
            # Another invocation completed initialization while this caller
            # waited. The old early return skipped THIS caller's token/grant and
            # could leave it using the winner's stale connection.
            await _bind_invocation_gateway_auth(
                user_token=user_token, delegation_grant=delegation_grant
            )
            return
        if _gateway_registry is None:
            _gateway_initialized = True
            return
        # A grant-bearing invocation must cold-load the tenant gateway without
        # an interactive token so connect_all defers it until the explicit
        # machine+grant bind below. Otherwise static user-token headers win.
        connection_user_token = "" if delegation_grant else user_token
        try:
            loaded_ids = await mcp_manager.load_gateways_from_registry(
                _gateway_registry,
                actor_id=actor_id,
                user_token=connection_user_token,
            )
            logger.info("Loaded %d gateway(s) from registry: %s", len(loaded_ids), loaded_ids)
        except Exception as e:
            logger.error("Failed to load gateways from registry: %s", e)
            _gateway_initialized = True
            return

        # Connect the shared Platform gateway (SigV4, plane=platform) so shared tools that
        # were moved off the tenant gateway (e.g. AWS-doc search, Wave 4 / Option B) also load
        # on the sub-agent — the search is dispatched HERE, so without this the tool is missing
        # on the task runtime and callers see "AWS docs tools couldn't load".
        try:
            _pgid = await mcp_manager.load_platform_gateway_from_env(
                actor_id=actor_id, user_token=connection_user_token
            )
            if _pgid:
                logger.info("Loaded platform gateway: %s", _pgid)
        except Exception as e:
            logger.error("Failed to load platform gateway: %s", e)

        # Connect synchronously on a worker thread — `connect_all` is
        # blocking (httpx + MCP context manager). The result is stored
        # on each `GatewayConnection`; we just log the per-gateway
        # outcome here so a flaky gateway is visible at startup rather
        # than only when the LLM tries to call one of its tools.
        try:
            results = await asyncio.to_thread(mcp_manager.connect_all)
            for gw_id, result in results.items():
                status = result.get("status", "unknown")
                if status == "connected":
                    logger.info(
                        "Gateway '%s': connected with %d tools",
                        gw_id, len(result.get("tools", [])),
                    )
                else:
                    logger.warning("Gateway '%s': %s", gw_id, result)
        except Exception as e:
            logger.error("Failed to connect gateways: %s", e)

        # ---- async-job-tracking per-tool wrap wiring ----
        # The tool path the LLM takes is :class:`MCPAgentTool.stream`,
        # which Strands' ``Agent`` invokes directly with no
        # manager-level chokepoint to wrap. The set_job_tracking
        # call below subclasses ``MCPAgentTool`` for trio members
        # (see :mod:`agent.src.jobs.strands_tool_wrap`) so the
        # registration / results gate runs on the path Strands
        # actually invokes.
        if _job_registry is not None:
            try:
                from jobs import context as jobs_context
                mcp_manager.set_job_tracking(
                    _job_registry,
                    actor_id_resolver=jobs_context.current_actor_id,
                    session_id_resolver=jobs_context.current_session_id,
                )
            except Exception as exc:
                logger.error(
                    "TaskAgent: failed to install per-tool job tracking: %s; "
                    "async job tracking will be inactive on this runtime",
                    exc,
                )
        # Async plan node cold-loading this container: connect the user_jwt
        # gateways with the machine token + grant. Done AFTER set_job_tracking
        # above so bind_delegation_grant_and_connect's per-gateway wrap applies
        # (connect_all deferred these gateways — no user token, no grant yet).
        if delegation_grant:
            await _connect_delegated_gateways(delegation_grant)
        _gateway_initialized = True


_cached_tools_by_name: dict | None = None


def _all_gateway_tools_by_name() -> dict:
    """Index every connected gateway tool by its name.

    Returns the same shape the legacy ``_gw_tools`` dict had so the
    rest of the file can stay shaped around name-keyed lookup. Pulled
    from every connected ``GatewayConnection`` rather than only the
    "added" set — the task agent doesn't model the consent-style
    added/trusted distinction the Main Agent uses; the caller's
    ``tools`` parameter is the authoritative gate.

    NOTE: Cache disabled to ensure job-tracking wraps are always picked up.
    """
    by_name: dict = {}
    expected_auth = _gateway_auth_fingerprint.get()
    for gw in mcp_manager._gateways.values():
        if not gw.is_connected:
            continue
        # Tenant tools are visible only when this invocation's exact credential
        # fingerprint matches the live transport. No credential, failed bind,
        # or another invocation's token/grant all fail closed. Platform and
        # other machine-authenticated gateways remain available.
        if gw.auth_type == "user_jwt" and (
            expected_auth is None
            or _active_gateway_fingerprint(gw) != expected_auth
        ):
            continue
        for t in gw.get_all_tools():
            # Resolve by the immutable MCP route name, not the mutable
            # model-facing alias. Oversized names are shortened later for
            # Bedrock, and mutating that alias must not poison canonical lookup
            # on the next warm invocation.
            mcp_tool = getattr(t, "mcp_tool", None)
            name = getattr(mcp_tool, "name", "") or (
                t.tool_spec.get("name", "")
                if hasattr(t, "tool_spec") and isinstance(t.tool_spec, dict)
                else getattr(t, "name", str(t))
            )
            if name:
                by_name[name] = t
    # The WebSearch tool resolves only for work dispatched from a turn that may search (web_search.py).
    import web_search as _web_search
    if not _web_search.turn_allows():
        by_name = {n: t for n, t in by_name.items() if not _web_search.is_web_search_tool(n)}
    return by_name


# Known Bedrock inference-profile ids, discovered once via ``model_catalog``
# and reused by the Custom Agent resolver so a stored model that the runtime
# cannot serve falls back to the runtime default instead of erroring. Cached
# across invocations; a discovery failure yields an empty set, which the
# resolver treats as "pass the configured model through unchanged".
_known_model_ids_cache: Optional[list[str]] = None


def _known_model_ids() -> Optional[list[str]]:
    """Best-effort catalog model-id set for the Custom Agent resolver.

    Reuses ``model_catalog.discover_models()`` (the same id source the Lab uses
    for validation). Cached after first call. Returns ``None`` only when the
    import itself fails, so the resolver passes the model through rather than
    dropping it on a transient catalog outage.
    """
    global _known_model_ids_cache
    if _known_model_ids_cache is not None:
        return _known_model_ids_cache
    try:
        import model.catalog as model_catalog
        _known_model_ids_cache = [
            m["id"] for m in model_catalog.discover_models() if m.get("id")
        ]
    except Exception as exc:  # noqa: BLE001 — model check must never break invoke
        logger.warning(
            "TaskAgent: model catalog discovery failed (%s); Custom Agent model "
            "ids will pass through unchecked",
            exc,
        )
        return None
    return _known_model_ids_cache


from strands.tools.mcp.mcp_agent_tool import MCPAgentTool

# Bedrock's Converse API caps ``toolConfig.tools[].toolSpec.name`` at 64
# characters. AgentCore gateway tools are namespaced ``<target>___<tool>``,
# which can exceed that and make the ENTIRE ConverseStream request fail with
# a ValidationException (not just drop the one tool). We alias oversized
# names below.
_MAX_TOOL_NAME_LEN = 64


def _shorten_tool_name(full_name: str, used: set[str]) -> str:
    """Derive a stable, unique, <=64-char agent-facing name.

    Prefers the bare tool name after the last ``___`` gateway separator
    (which keeps the semantic ``_status``/``_results`` suffix the
    Job_Detector keys on). Falls back to a truncated name plus a short
    hash of the full name when the bare name is still too long or would
    collide with another tool in the same set.
    """
    bare = full_name.rsplit("___", 1)[-1]
    if len(bare) <= _MAX_TOOL_NAME_LEN and bare not in used:
        return bare
    h = hashlib.sha1(full_name.encode()).hexdigest()[:8]
    base = bare[: _MAX_TOOL_NAME_LEN - 9]
    return f"{base}_{h}"[:_MAX_TOOL_NAME_LEN]


def _alias_oversized_tool_names(tools: list) -> list:
    """Return a tool list where every agent-facing name fits Bedrock's limit.

    MCP wrappers keep their existing reconnect/job/lease behavior while their
    model-facing names are recomputed from immutable server route names for the
    current invocation. Oversized names and collisions receive stable aliases;
    non-MCP tools pass through untouched.
    """
    result: list = []
    used: set[str] = set()
    for t in tools:
        if isinstance(t, MCPAgentTool):
            # Recompute from the immutable server route on EVERY invocation.
            # The wrapper is process-global and may carry a set-specific alias
            # from an earlier warm invocation; treating that alias as canonical
            # lets separately selected tools converge on the same name later.
            canonical = getattr(getattr(t, "mcp_tool", None), "name", "") or t.tool_name
            alias = canonical
            if len(canonical) > _MAX_TOOL_NAME_LEN or canonical in used:
                alias = _shorten_tool_name(canonical, used)
            t._agent_tool_name = alias
            used.add(alias)
            result.append(t)
            if alias != canonical:
                logger.info("Aliased gateway tool %r -> %r", canonical, alias)
            continue

        name = getattr(t, "tool_name", None)
        if not name and hasattr(t, "tool_spec") and isinstance(t.tool_spec, dict):
            name = t.tool_spec.get("name")
        if name:
            used.add(name)
        result.append(t)
    return result


def _drain_hook_lines(channel: Optional[ProgressChannel]) -> list[str]:
    """Drain hook-emitted progress events into task-agent SSE lines.

    A hook action (e.g. ``render``) running in a Strands hook callback can't
    ``yield`` into this generator, so it pushes to the active ProgressChannel;
    we drain it between stream events. Render events (carrying
    ``render_manifest_key``) are re-emitted as ``output_ref`` lines so the main
    agent's ``dispatch_tool._consume_stream`` routes them through the designed
    render path; everything else is forwarded as a step line.
    """
    if channel is None:
        return []
    lines: list[str] = []
    for pe in channel.drain():
        key = pe.extra.get("render_manifest_key") if pe.extra else None
        if key:
            ref = {"type": "output_ref", "s3_key": key}
            # Carry the view's name across the sub-agent boundary; without it the
            # main agent can only surface an unnamed render.
            label = pe.extra.get("render_label") if pe.extra else None
            if label:
                ref["label"] = label
            # Carry the render correlation id too — the UI matches the manifest to
            # its "Composing…" placeholder tab by this. Dropping it here is what
            # made the manifest arrive id-less, so it opened a second tab and left
            # the placeholder spinning forever.
            rid = pe.extra.get("render_request_id") if pe.extra else None
            if rid:
                ref["render_request_id"] = rid
            lines.append(json.dumps(ref))
        else:
            lines.append(pe.to_step_json())
    return lines


_CANONICAL_PREFIX = "canonical://"


def _resolve_tools(names: list[str]) -> tuple[list, list[str]]:
    """Resolve tool names to tool objects; return (tools, missing_names).

    Built-ins take precedence over gateway tools. Unlike the previous
    implementation, missing names are returned to the caller rather
    than logged-and-dropped — letting the LLM run with a partial tool
    set was the bug behind the diarization-hallucination incident.
    """
    gw_tools = _all_gateway_tools_by_name()
    tools: list = []
    missing: list[str] = []
    for raw_name in names:
        name = raw_name.removeprefix(_CANONICAL_PREFIX)
        if name in _BUILTINS:
            tools.append(_BUILTINS[name])
        elif name in gw_tools:
            tools.append(gw_tools[name])
        else:
            missing.append(name)
    return tools, missing


# Task Agent core builtins: it IS the code+artifact sub-agent, so on an internal
# dispatch these must always be available even if the orchestrator forwarded a
# narrower list.
_ESSENTIAL_DISPATCH_BUILTINS = ("artifact", "code_interpreter")


def _ensure_essential_builtins(tools: list, requested_tools, *, principal) -> list:
    """Guarantee ``artifact`` + ``code_interpreter`` on an INTERNAL dispatch that
    named a specific, non-empty tool list.

    Why: the Task Agent is the harness's code+artifact sub-agent, but tools are
    resolved purely from the forwarded list. When the orchestrator dispatched a
    PDF/code task and forwarded only ``artifact`` (prod session 738cd97b), the
    sub-agent had no ``code_interpreter`` and could not build the file. These two
    are the sub-agent's defining capabilities, so they are always made available.

    Tightly scoped so nothing else shifts:
      * external (``principal`` present) path is deliberately narrowed for the
        confused-deputy guard — NEVER widened here;
      * ``tools == []`` (pure-LLM node) and ``tools is None`` (full catalog,
        already includes both) go through other branches and are not passed here.
    The code_interpreter sandbox is created lazily on first use, so an unused
    tool added here costs nothing.
    """
    if isinstance(principal, dict):
        return tools
    if not (isinstance(requested_tools, list) and requested_tools):
        return tools
    out = list(tools)
    for name in _ESSENTIAL_DISPATCH_BUILTINS:
        t = _BUILTINS.get(name)
        if t is not None and t not in out:
            out.append(t)
    return out


# ---- External invoke: server-side Custom Agent config resolution ----
#
# The proxy's external dispatch (`ui/server/invoke/stampIdentity.ts`) stamps
# ONLY identity + the published-version reference (`agent_id`, `version`) —
# never config content. This runtime resolves the referenced published config
# itself (persona.md, skill defs, tool collections, external opt-in) from the
# AssetIndex + lab artifacts bucket and hands the result to
# `custom_agent_resolver.resolve_config` exactly as an inline `custom_agent`
# dict would be. Resolution is Owner-scoped: every read is keyed on the
# stamped `actor_id` (the Owner), never the Principal (Req 6.2).
#
# Failure semantics are fail-closed-and-degrade (matching the resolver's own
# norms): any store/read error logs and returns None, leaving the invocation
# with no persona/skills/tools rather than raising — the caller still gets a
# well-formed (if bare) turn, and the external tool-narrowing path keeps
# `tools=[]`.

_lab_ddb_table = None
_lab_s3_client = None


def _get_lab_table():
    global _lab_ddb_table
    if _lab_ddb_table is None and ASSET_INDEX_TABLE:
        _lab_ddb_table = boto3.resource("dynamodb", region_name=REGION).Table(
            ASSET_INDEX_TABLE
        )
    return _lab_ddb_table


def _get_lab_s3():
    global _lab_s3_client
    if _lab_s3_client is None and LAB_ARTIFACTS_BUCKET:
        _lab_s3_client = boto3.client("s3", region_name=REGION, config=_boto_cfg)
    return _lab_s3_client


def _read_lab_text(key: str) -> Optional[str]:
    """Read a UTF-8 text object from the lab artifacts bucket, or None."""
    s3 = _get_lab_s3()
    if s3 is None:
        return None
    try:
        resp = s3.get_object(Bucket=LAB_ARTIFACTS_BUCKET, Key=key)
        return resp["Body"].read().decode("utf-8")
    except Exception:
        logger.warning("external config: failed to read s3://%s/%s",
                       LAB_ARTIFACTS_BUCKET, key, exc_info=True)
        return None


def _pick_lineage_row(rows: list, version) -> Optional[object]:
    """Pick the row the proxy authorized: exact `version` match when stamped,
    else the row flagged `current`, else the highest non-deprecated version.

    `query_lineage` returns ascending version order, so a reversed scan finds
    the highest first.
    """
    if version is not None:
        for row in rows:
            if getattr(row, "version", None) == version:
                return row
    for row in reversed(rows):
        if getattr(row, "current", False) and not getattr(row, "deprecated", False):
            return row
    for row in reversed(rows):
        if not getattr(row, "deprecated", False):
            return row
    return None


async def _fetch_external_custom_agent_config(payload: dict) -> Optional[dict]:
    """Resolve a published Custom Agent's config for an external invocation.

    Returns the same dict shape an inline ``payload["custom_agent"]`` carries
    (persona / model / skills / tool_collections / external_tool_collections /
    compatible_skills), or ``None`` when resolution is impossible — in which
    case the invocation proceeds bare (fail-closed on tools, generic prompt).
    """
    agent_id = payload.get("agent_id") or ""
    owner = payload.get("actor_id") or ""
    version = payload.get("version")
    # ``agent_source`` selects which row to resolve. Absent/"published" keeps
    # the external contract unchanged; "draft" is used only by the owner's
    # internal test console (`POST /agent-test`), which needs to try a working
    # copy before publishing it. External callers can never reach the draft
    # branch: the proxy's reserved-field strip drops `agent_source` from any
    # caller body, and only a published lineage is addressable by agent id.
    source = payload.get("agent_source") or "published"
    table = _get_lab_table()
    if not agent_id or not owner or table is None:
        logger.warning(
            "external config: cannot resolve (agent_id=%r, owner present=%s, "
            "table configured=%s)", agent_id, bool(owner), table is not None,
        )
        return None

    from lab.asset_index import AssetIndex, make_pk_user_owned, make_sk_owned

    index = AssetIndex(ddb_table=table, actor_id=owner)

    if source == "draft":
        # Working copy lives in the owner's OWNED partition, so the get is
        # already actor-keyed — a cross-actor draft simply isn't found.
        try:
            agent_row = await index.get(
                make_pk_user_owned(owner),
                make_sk_owned("specialty-agent", agent_id),
            )
        except Exception:
            logger.warning("agent config: draft lookup failed for agent %s",
                           agent_id, exc_info=True)
            return None
        if agent_row is None:
            logger.warning("agent config: no draft row for agent %s", agent_id)
            return None
    else:
        try:
            lineage = await index.query_lineage(agent_id)
        except Exception:
            logger.warning("external config: lineage query failed for agent %s",
                           agent_id, exc_info=True)
            return None
        agent_row = _pick_lineage_row(lineage, version)
    if agent_row is None or getattr(agent_row, "category", "") != "specialty-agent":
        logger.warning("external config: no published row for agent %s (version=%r)",
                       agent_id, version)
        return None

    # Owner-scope check (Req 6.2): the resolved row must belong to the stamped
    # Owner. Published prefixes carry `users/<owner>/published/...`; group/org
    # scoped rows are still owner-published assets, so match on the row's
    # recorded prefix only when it is user-scoped.
    prefix = getattr(agent_row, "s3_prefix", "") or ""
    if prefix.startswith("users/") and not prefix.startswith(f"users/{owner}/"):
        logger.warning(
            "external config: owner mismatch for agent %s (stamped owner=%s, "
            "row prefix=%s)", agent_id, owner, prefix,
        )
        return None

    persona = _read_lab_text(f"{prefix}persona.md") if prefix else None

    # ---- Skill defs: owner's current published version, else owner draft ----
    from skills_manager import SkillsManager

    skill_defs: list[dict] = []
    for skill_id in list(getattr(agent_row, "compatible_skills", []) or []):
        content = None
        try:
            skill_lineage = await index.query_lineage(skill_id)
        except Exception:
            skill_lineage = []
        skill_row = _pick_lineage_row(skill_lineage, None)
        if skill_row is not None:
            sp = getattr(skill_row, "s3_prefix", "") or ""
            if sp:
                content = _read_lab_text(f"{sp}SKILL.md")
        if content is None:
            try:
                draft = await index.get(
                    make_pk_user_owned(owner), make_sk_owned("skill", skill_id)
                )
            except Exception:
                draft = None
            if draft is not None:
                dp = getattr(draft, "s3_prefix", "") or ""
                if dp:
                    content = _read_lab_text(f"{dp}SKILL.md")
        if content is None:
            logger.warning("external config: skill %s unresolvable; skipping", skill_id)
            continue
        name, description, instructions = SkillsManager._parse_skill_md(content)
        if not name or not instructions:
            logger.warning("external config: skill %s has no name/instructions; skipping",
                           skill_id)
            continue
        skill_defs.append({
            "id": skill_id,
            "name": name,
            "description": description,
            "instructions": instructions,
        })

    # ---- Tool collections: the version's pin, else the owner's live rows ----
    #
    # A published row carries `tool_collection_snapshots`: the tool refs resolved
    # at publish time. Preferring it is what makes the version's tool set
    # immutable — reading the owner's working-copy rows instead meant editing a
    # draft collection retroactively changed every published version, and
    # deleting one reduced them to zero tools with only a log line to show for it.
    #
    # `is None` and not falsiness: `[]` is a version published with no
    # collections, which must bind nothing, while `None` is an unpinned row (a
    # working copy, or a version published before the field existed) that still
    # has to resolve live. `or []` here would collapse the two and unpin the
    # former.
    #
    # Snapshots are handed to the resolver as-is; `ToolCollectionSnapshot` names
    # its fields `asset_id`/`tool_refs` precisely so `custom_agent_resolver`
    # reads them with no branch of its own.
    collections: list = []
    pinned = getattr(agent_row, "tool_collection_snapshots", None)
    if pinned is not None:
        collections = list(pinned)
    else:
        for cid in list(getattr(agent_row, "tool_collections", []) or []):
            try:
                row = await index.get(
                    make_pk_user_owned(owner), make_sk_owned("tool-collection", cid)
                )
            except Exception:
                row = None
            if row is None:
                # Dropped-and-logged degradation is the resolver's job; hand it an
                # unresolvable marker (dict without tool refs) so it logs the id.
                collections.append({"id": cid})
                continue
            collections.append(row)

    integration = getattr(agent_row, "integration_config", None)
    external_opt_in = list(getattr(integration, "external_tool_collections", []) or [])

    return {
        "persona": persona,
        "model": getattr(agent_row, "model", None),
        "skills": skill_defs,
        "tool_collections": collections,
        "external_tool_collections": external_opt_in,
        "compatible_skills": list(getattr(agent_row, "compatible_skills", []) or []),
    }

# ---------------------------------------------------------------------------
# Plan Node Execution (Pattern D — single-action mode)
# ---------------------------------------------------------------------------


def _node_source_media(payload) -> list:
    """The ``s3://`` source media a plan node analyses, from its (resolved) input/prompt.

    Plan-node jobs register explicitly (bypassing the auto tool-wrap), so the source is
    captured here from the node payload — structural + tool-agnostic, same rule as the
    wrap. Best-effort: a miss must never fail node registration.
    """
    try:
        from media.source_media import extract_source_media, malformed_media_s3uris
        srcs = extract_source_media(payload)
        # Correct-by-construction guard: surface a mistyped opaque id (actor/session UUID) in a
        # source media path at the source, so it reads as a clear, correctable signal instead of
        # a cryptic HeadObject 404 deep in the media workflow (the doubled-char actor-id class).
        bad = malformed_media_s3uris(payload)
        if bad:
            logger.warning(
                "source media URI has a malformed artifacts/<id> segment (likely a mis-copied "
                "opaque id — copy it verbatim from context): %s", bad,
            )
        return srcs
    except Exception:  # noqa: BLE001
        return []


def _extract_structured_output(text: str):
    """Extract structured JSON output from agent text.

    The node agent is instructed to include a ```json block at the end of its
    response. This function extracts and parses that block. Falls back to
    extracting S3 URIs, or the raw text if nothing structured is found.
    """
    import re as _re

    # Try to find a ```json ... ``` block (last one wins if multiple)
    json_blocks = _re.findall(r'```json\s*\n(.*?)\n```', text, _re.DOTALL)
    if json_blocks:
        try:
            return json.loads(json_blocks[-1])
        except (json.JSONDecodeError, ValueError):
            pass

    # Fallback: extract S3 URIs from the text
    s3_uris = _re.findall(r's3://[a-zA-Z0-9\-_.]+/[^\s\'"`,\)]+', text)
    if s3_uris:
        if len(s3_uris) == 1:
            return {"uri": s3_uris[0]}
        return {"uris": s3_uris}

    # Last resort: return the text itself (truncated)
    return {"text": text[:2000]}


def _declared_error(structured_value) -> str:
    """Return a node's self-declared error string, else "".

    A node that emits a top-level ``{"error": ...}`` in its structured JSON block is using
    the agent's own convention for "I could not complete this step" — e.g. t4_synthesize_fr
    returned ``{"error": "Cannot proceed: upstream step ... produced no ... data"}``. That is
    a definitive failure signal (not fuzzy NLP): honoring it marks the node failed so the plan
    status is honest and P2's error-aware retry can act, instead of reporting SUCCEEDED over a
    step that plainly said it failed.
    """
    if isinstance(structured_value, dict):
        err = structured_value.get("error")
        if err and str(err).strip():
            return str(err)
    return ""


_NODE_ERROR_MAX_CHARS = 2000


def _tool_result_error(block: dict) -> str:
    """The error a single Strands ``toolResult`` block carries, else "".

    Gateway tools report a rejected call INSIDE a successful MCP result: staging session
    0d71f16b's shot-segmentation node got ``status: "success"`` with content
    ``{"error": true, "errorCode": "INVALID_PARAMETER", "message": "asset_s3uri must be a valid
    S3 URI ..."}``. So both the block status and that JSON envelope count.
    """
    if not isinstance(block, dict):
        return ""
    texts = [c.get("text") for c in (block.get("content") or []) if isinstance(c, dict)
             and isinstance(c.get("text"), str)]
    for t in texts:
        try:
            body = json.loads(t)
        except (ValueError, TypeError):
            continue
        if isinstance(body, dict) and body.get("error") is True:
            code = str(body.get("errorCode") or "error")
            return f"{code}: {str(body.get('message') or '')[:300]}".rstrip(": ")
    if block.get("status") == "error":
        return (texts[0] if texts else "tool error")[:300]
    return ""


def _all_tool_calls_failed(messages) -> str:
    """A summary when the node called tools and EVERY call came back an error, else "".

    A node whose every tool call was rejected produced nothing, whatever its prose says. A TEXT
    node has no JSON block to declare ``{"error": ...}`` in, so without this the 0d71f16b nodes
    ("ERROR: The job could not be submitted ...") were recorded ``completed`` and the plan
    SUCCEEDED with no job behind any node. One successful call is enough to leave the node alone.
    """
    errors, calls = [], 0
    for msg in messages or []:
        for c in (msg.get("content") or []) if isinstance(msg, dict) else []:
            block = c.get("toolResult") if isinstance(c, dict) else None
            if block is None:
                continue
            calls += 1
            err = _tool_result_error(block)
            if not err:
                return ""
            errors.append(err)
    if not calls:
        return ""
    uniq = list(dict.fromkeys(errors))
    return f"all {calls} tool call(s) returned an error: " + "; ".join(uniq)[:600]


def _node_row_error(output: str, failure_notes: list) -> str:
    """The plan-state ``error`` for a failed node: the framework's ``[plan] ...`` reasons FIRST,
    then the agent's prose, bounded to ``_NODE_ERROR_MAX_CHARS``.

    The notes are also appended to ``output`` (the agent-facing record), so they are stripped from
    its tail here rather than stored twice. Leading with them is what lets the chainer classify the
    failure: ``_is_missing_input_error`` / content-policy fail-fast read this field, and a long
    refusal pushed "Missing required input ..." past the 2000-char cut (staging dcd11946).
    """
    prose = output or ""
    for note in reversed(failure_notes or []):
        suffix = f"\n\n{note}"
        if prose.endswith(suffix):
            prose = prose[:-len(suffix)]
    lead = "\n".join(n for n in (failure_notes or []) if n)
    if lead and prose.strip():
        text = f"{lead}\n\n{prose}"
    else:
        text = lead or prose or "node failed"
    return text[:_NODE_ERROR_MAX_CHARS]


_SFN_TERMINAL_SUCCESS = {"SUCCEEDED"}
_SFN_TERMINAL_FAILURE = {"FAILED", "TIMED_OUT", "ABORTED"}


def _node_status_for_adopted_job(sfn_status: Optional[str]) -> str:
    """Map an adopted async job's *current* SFN status to the node's plan-state status.

    Reconcile-on-adopt: a plan node that adopts an SFN execution which is
    ALREADY terminal (e.g. ``process_media`` deduplicated to a prior completed
    execution, or the job finished before the poller's first observation) must
    NOT be parked in ``"processing"``. The poller -> changefeed-bridge pipeline
    only writes ``"results_ready"`` on a live PENDING->terminal *transition*,
    which never occurs for an already-terminal job — so the node would hang
    forever (observed: sg1_trim_proxy stuck "Running" for 4h). Instead reconcile
    against the job's actual current state at adoption time:
      - already SUCCEEDED       -> "results_ready" (chainer fetches results now)
      - already FAILED/TIMED_OUT/ABORTED -> "failed"
      - still running / unknown -> "processing" (normal async path)
    """
    if sfn_status in _SFN_TERMINAL_SUCCESS:
        return "results_ready"
    if sfn_status in _SFN_TERMINAL_FAILURE:
        return "failed"
    return "processing"


# Plan-state statuses a node's own end-of-run write must not overwrite: each means the row moved on
# while the node was still running (bridged, fetched, failed by the chainer, or skipped). A retry
# re-dispatch resets the row to "running" first, so a re-run is never blocked by this.
_NODE_ROW_ADVANCED = ("results_ready", "completed", "failed", "skipped")


def _node_status_for_queue_job(job_id: str) -> tuple[str, str]:
    """``(plan-state status, error)`` for a gateway QUEUE job (fal) the node submitted.

    The queue counterpart of :func:`_node_status_for_adopted_job`, reading the Job Registry row the
    polling worker keeps current. A node that waits for its own job (polls it, or just runs long)
    finishes AFTER the job, whose one terminal event has then already fired, so ``processing`` would
    wait forever (session c3447913, ``plan-meridian-reel-recovery``). Unknown or unreadable →
    ``processing``, the previous behaviour.
    """
    if not job_id or not JOB_REGISTRY_TABLE:
        return "processing", ""
    try:
        item = boto3.resource("dynamodb", region_name=REGION).Table(JOB_REGISTRY_TABLE).get_item(
            Key={"jobId": job_id}, ConsistentRead=True).get("Item") or {}
    except Exception as exc:  # noqa: BLE001 - fall back to the async path
        logger.debug("queue job %s status unreadable: %s", job_id, exc)
        return "processing", ""
    status = _node_status_for_adopted_job(item.get("status"))
    error = item.get("lastError") if status == "failed" else ""
    return status, error if isinstance(error, str) else ""


def _describe_sfn_status(execution_arn: str) -> Optional[str]:
    """Return the current SFN execution status, or ``None`` if it can't be read.

    Bounded, best-effort: any error (missing IAM, throttle, malformed ARN)
    returns ``None`` so the caller falls back to the normal ``"processing"``
    async path. Reconcile-on-adopt is an optimization that must never break
    dispatch.
    """
    try:
        import boto3 as _boto3
        region = execution_arn.split(":")[3]
        sfn = _boto3.client("stepfunctions", region_name=region)
        resp = sfn.describe_execution(executionArn=execution_arn)
        return resp.get("status")
    except Exception as e:  # noqa: BLE001 — best-effort reconcile, never fatal
        logger.info("reconcile-on-adopt: DescribeExecution failed for %s: %s", execution_arn[:60], e)
        return None


def _extract_queue_handle(value) -> Optional[dict]:
    """Return ``{"job_id", "status_url", "endpoint_id"}`` for a queue submission.

    A submission is not a result: fal's submit_job returns an id plus the endpoint it
    was sent to, and the finished file is only reachable through the trio's results
    tool. Recognising that shape is what lets the job be registered, polled and
    notified like any other async job.

    Field names come from OBSERVED responses, not documentation, because fal is not
    consistent: the id is ``request_id`` or ``job_id``, and the endpoint has appeared as
    ``model``, ``endpoint_id`` and ``model_id`` across three different endpoints in one
    plan. The endpoint matters as much as the id -- check_job rejects a call without it
    ("Missing required field(s): 'endpoint_id'").

    The ENDPOINT is the discriminator, not ``status_url``. Requiring status_url missed
    seedance entirely -- it returns request_id + model and no status_url -- so that
    video's job was never tracked. An id alone is ambiguous; an id plus an endpoint is
    unambiguously a queue submission.
    """
    if not isinstance(value, dict):
        return None
    job_id = value.get("request_id") or value.get("job_id") or ""
    if not isinstance(job_id, str) or not job_id:
        return None
    endpoint_id = (
        value.get("endpoint_id") or value.get("model_id") or value.get("model") or ""
    )
    if not isinstance(endpoint_id, str):
        endpoint_id = ""
    status_url = value.get("status_url") or ""
    if not isinstance(status_url, str):
        status_url = ""
    # An explicit endpoint field is NOT required when status_url carries it. stable-audio
    # returned only {job_id, request_id, status_url, response_url} -- no model field -- so
    # requiring one rejected it, its job was never registered or polled, and the merge
    # node was left holding a handle it could not resolve. The status_url
    # ("queue.fal.run/fal-ai/stable-audio-25/requests/<id>/status") names the endpoint
    # perfectly well; either source is sufficient.
    if not endpoint_id and not status_url:
        return None
    resolved_endpoint = _queue_app_id(endpoint_id, status_url)
    if not resolved_endpoint:
        # Neither source yielded an endpoint: registering now would create a row the
        # poller can never use, which is worse than not registering.
        return None
    return {
        "job_id": job_id,
        "status_url": status_url,
        "endpoint_id": resolved_endpoint,
    }


def _queue_app_id(model: str, status_url: str = "") -> str:
    """The endpoint id the QUEUE addresses, which is not the full model path.

    fal's own status_url shows the form it expects -- only the first two segments:

        model:      fal-ai/stable-audio-3/small/music/text-to-audio
        status_url: https://queue.fal.run/fal-ai/stable-audio-3/requests/<id>/status

    Sending the full model path produced "Status check failed (405)": the queue built a
    url that does not exist. So prefer parsing status_url, which is authoritative when
    fal supplies it, and otherwise take the first two segments of the model -- which is
    what status_url itself contains.
    """
    # Both markers must be present. Without the "/requests/" check a url like
    # "https://queue.fal.run/x/status" yielded "x/status" -- a parse that looked
    # successful and produced nonsense, which is worse than not parsing at all.
    if status_url and "queue.fal.run/" in status_url and "/requests/" in status_url:
        tail = status_url.split("queue.fal.run/", 1)[1]
        # ".../<owner>/<app>/requests/<id>/status" -> "<owner>/<app>"
        before = tail.split("/requests/", 1)[0].strip("/")
        if before:
            return before
    parts = [p for p in (model or "").split("/") if p]
    if len(parts) >= 2:
        return "/".join(parts[:2])
    return model or ""


def _queue_trio_for(gateway_id: str = "") -> Optional[dict]:
    """The declared status/results tools for a gateway's queue trio.

    Read from the live job detection rather than hardcoded: roles are declared on
    the gateway's registry row and resolved at connect, so this stays correct if the
    server renames a tool or a second queue-style server is registered.
    """
    try:
        for gw in (getattr(mcp_manager, "_gateways", {}) or {}).values():
            detection = getattr(gw, "job_detection", None)
            if not detection:
                continue
            for trio in getattr(detection, "trios", []) or []:
                if gateway_id and getattr(trio, "gateway_id", "") != gateway_id:
                    continue
                status_tool = getattr(trio, "status_tool", "")
                results_tool = getattr(trio, "results_tool", "")
                if status_tool and results_tool:
                    return {
                        "gateway_id": getattr(trio, "gateway_id", "") or gw.id,
                        "trio_id": getattr(trio, "trio_id", "") or "queue|job",
                        "status_tool": status_tool,
                        "results_tool": results_tool,
                    }
    except Exception as exc:  # noqa: BLE001 — registration is best-effort
        logger.debug("could not resolve a queue trio: %s", exc)
    return None


_URI_BEARING_OUTPUT_TYPES = ("s3_uri", "uri", "url", "media", "file", "artifact")


def _unmet_output_contract(output_type, structured_value, text) -> str:
    """Return why a node failed its declared output contract, or "" if it met it.

    A node declares output_type in the plan. When that names a locatable artifact,
    prose is not a result: the node must surface at least one s3:// or https:// URI
    somewhere in its structured value or its text. Types like "text" and "json"
    impose no such requirement and are left alone.

    Deliberately permissive about WHERE the uri appears -- structured extraction is
    best-effort, and failing a node that produced a real file but formatted its
    summary oddly would be worse than the bug this closes.
    """
    declared = str(output_type or "").strip().lower()
    if not declared or not any(t in declared for t in _URI_BEARING_OUTPUT_TYPES):
        return ""
    blob = ""
    if structured_value is not None:
        try:
            blob = json.dumps(structured_value)
        except (TypeError, ValueError):
            blob = str(structured_value)
    haystack = f"{blob}\n{text or ''}"
    if "s3://" in haystack or "https://" in haystack:
        return ""
    return f"declared output_type {declared!r} but produced no s3:// or https:// uri"


# The prefix of a node error raised while BUILDING the node (its agent, its tools) rather than running it. The
# plan chainer does not error-retry it: the same payload builds the same way, so a retry only doubles the wait.
NODE_SETUP_FAILED = "Node setup failed"


def _dedupe_tools_by_name(tools):
    """`tools` with each tool name kept once (the first), in order. A tool without a name is kept as is."""
    seen = set()
    out = []
    for tool in tools or []:
        name = getattr(tool, "tool_name", None)
        if isinstance(name, str) and name:
            if name in seen:
                continue
            seen.add(name)
        out.append(tool)
    return out


async def _execute_plan_node(payload: dict):
    """Execute a single plan node — one tool call, write result back.

    Invoked by the Plan Chainer Lambda. The Task Agent:
    1. Calls the specified tool(s) with the given prompt
    2. Writes the result to S3 (plan output)
    3. Updates the Plan State table: node → SUCCEEDED/FAILED
    4. Updates the Job Registry node entry (for Activity panel)
    5. Returns immediately (~15-30s total)

    The Plan Chainer detects the plan state write via DDB Stream and
    dispatches the next ready node(s).
    """
    from state_store import StateStore
    from jobs import context as jobs_context

    prompt = payload.get("prompt", "")
    # Default None (not []) so downstream logic can distinguish "caller
    # didn't specify tools" (fall back to full catalog) from "caller
    # explicitly wants no tools" (respect the empty list). See the note
    # at the resolved_tools block below.
    tools_list = payload.get("tools")
    node_id = payload.get("node_id", "")
    plan_id = payload.get("plan_id", "")
    session_id = payload.get("__plan_session_id", payload.get("session_id", ""))
    s3_bucket = payload.get("s3_bucket", S3_BUCKET)
    ddb_table = payload.get("ddb_table", DDB_TABLE)

    logger.info("_execute_plan_node: node=%s, plan=%s", node_id, plan_id)
    yield json.dumps({"type": "step", "status": "running", "label": f"Executing node {node_id}"})

    def _write_node_failed(error_msg: str) -> None:
        """Mark this node FAILED in the plan state and the job registry, now. A node that dies before its turn
        and says nothing sits `running` until the chainer's 900s stall watchdog reaps it (plan 593333b5: eight
        fal shots crashed in 2s on a duplicate tool name and looked "slow" for 15 minutes, twice)."""
        if ddb_table and session_id and node_id:
            try:
                StateStore(table_name=ddb_table, region=REGION).put_node_status(
                    session_id, node_id, "failed", plan_id=plan_id, error=error_msg,
                )
            except Exception:
                logger.exception("Node %s: failed to write failed status to plan state", node_id)
        if JOB_REGISTRY_TABLE:
            try:
                from jobs.registry import JobRegistry as _JR
                _JR(table_name=JOB_REGISTRY_TABLE, region=REGION).set_terminal(
                    f"{plan_id}#{node_id}", status="FAILED", error=error_msg,
                )
            except Exception:
                logger.exception("Node %s: failed to write FAILED status to job registry", node_id)

    # Load gateways for tool resolution
    await _ensure_gateways_loaded(
        actor_id=payload.get("actor_id", "") or "",
        user_token=payload.get("user_token", "") or "",
        delegation_grant=payload.get("delegation_grant", "") or "",
    )

    # Resolve tools with flexible name matching
    gw_tools_map = _all_gateway_tools_by_name()
    gw_by_bare = {}
    for full_name in gw_tools_map:
        if '___' in full_name:
            bare = full_name.rsplit('___', 1)[-1]
            gw_by_bare[bare] = gw_tools_map[full_name]

    # NOTE: `tools_list is None` means "not specified — fall back to the
    # full catalog"; `tools_list == []` means "explicitly no tools" and
    # MUST be respected as-is. Using `if tools_list:` here treated an
    # intentional empty list the same as "not specified" (both are falsy),
    # silently handing sync/text-only nodes (e.g. a translation node with
    # tools=[]) the entire gateway tool catalog. That let a node whose
    # design assumed it could only work from its injected prompt instead
    # reach for a results tool itself, pulling in a full raw transcript
    # and blowing the node's output token budget (MaxTokensReachedException
    # on t3_translate). Nodes that genuinely need no tools must get none.
    resolved_tools = []
    if tools_list:
        for name in tools_list:
            if name in _BUILTINS:
                resolved_tools.append(_BUILTINS[name])
            elif name in gw_tools_map:
                resolved_tools.append(gw_tools_map[name])
            elif name in gw_by_bare:
                resolved_tools.append(gw_by_bare[name])

    # Fallback: gateway tools + builtins (no code_interpreter, no http_request)
    # — only when tools_list was never specified (None), not when it was an
    # explicit empty list.
    if tools_list is None:
        excluded = ('code_interpreter', 'http_request')
        resolved_tools = list(gw_tools_map.values()) + [
            _BUILTINS[name] for name in _BUILTINS if name not in excluded
        ]
    elif tools_list and not resolved_tools:
        # The Design Agent named one or more tools that don't exist under
        # any known name (builtin, gateway full name, or gateway bare
        # name) — e.g. "start_separate_audio_stems" when the real tool is
        # "start_stem_separation". Failing loudly here beats the
        # alternative: handing the node agent zero tools and a prompt
        # that instructs it to call one anyway. With nothing callable,
        # the model has been observed fabricating a fake job_id and
        # reporting success (```json {"job_id": "job_stub_..."}```)
        # instead of raising an error — a hallucination that got written
        # to the plan manifest as if it were real, and downstream nodes
        # were also invoked (and correctly refused to fabricate further
        # in the incident this fix addresses, but that's not guaranteed).
        available_bare = sorted(gw_by_bare.keys())
        error_msg = (
            f"None of the requested tools {tools_list!r} could be resolved "
            f"(not a builtin, gateway tool, or gateway tool by bare name). "
            f"Available bare tool names: {available_bare}"
        )
        logger.error("Node %s: %s", node_id, error_msg)
        yield json.dumps({"type": "step", "status": "failed", "label": error_msg})
        _write_node_failed(error_msg)
        return

    # A plan node that named a specific, non-empty tool list gets ONLY those tools (the same
    # confused-deputy-safe scoping as a normal dispatch) — but the main dispatch path guarantees
    # code_interpreter + artifact via _ensure_essential_builtins and THIS plan-node path did not,
    # so a node that must BUILD a deliverable and ADOPT it had no way to. A gallery/assembly node
    # authored with only media_* tools (the Design Agent cannot list render-only or dispatch tools)
    # therefore failed 100% — it could presign clips but not build/adopt the clip-gallery manifest
    # (session 1a4b98f2). Bind the sub-agent's defining code+artifact builtins here too. No-ops for
    # tools_list None (full catalog) / [] (pure-LLM) and for the external principal path.
    resolved_tools = _ensure_essential_builtins(resolved_tools, tools_list, principal=payload.get("principal"))

    # Knowledge graph (the graph extension of project search): when the plan's turn ran in a graph-enabled
    # Project, the node gets the graph tools through the Search_Gateway with its own delegated Project_Token.
    from project_search.delegated_token import subagent_graph_binding
    _node_graph_tools, _node_graph_prompt = await subagent_graph_binding(payload, caller="plan_node")
    if _node_graph_tools:
        resolved_tools = list(resolved_tools or []) + _node_graph_tools

    # Bind job context so any gateway jobs get registered with the right run_id
    actor_id = payload.get("actor_id", "")
    run_id_token = None
    jobs_ctx_token = None
    # Forwarded by the plan chainer from the PLAN row, so every job this node
    # dispatches is attributed to the turn that asked for the plan — nodes run
    # asynchronously, long after that invocation's contextvars are gone.
    node_turn_token = None
    _node_turn = payload.get("turn_id") or ""
    if isinstance(_node_turn, str) and _node_turn.strip():
        try:
            node_turn_token = jobs_context.set_turn_id(_node_turn.strip())
        except Exception:
            pass
    if plan_id:
        run_id_token = jobs_context.set_run_id(plan_id)
    # Every gateway job this node submits is registered with its node (see jobs.context.set_node_id).
    node_id_token = jobs_context.set_node_id(node_id) if plan_id and node_id else None
    # ...and is held to the fal endpoint(s) its plan named (DAGNode.endpoint_ids), so a drift to
    # another model is refused before anything is generated (session c3447913).
    planned_endpoints_token = (
        jobs_context.set_planned_endpoints(payload.get("endpoint_ids") or [])
        if plan_id and node_id else None
    )
    if actor_id and session_id:
        try:
            jobs_ctx_token = jobs_context.set_request_context(
                actor_id=actor_id, session_id=session_id,
            )
        except Exception:
            pass
    # The plan's producing project, forwarded by the chainer from the plan row (asset-lineage
    # Phase 3 (a)): the node's outputs go under projects/<pid>/sessions/<sid>/output/ where the env
    # opts in, and jobs it registers carry the project (ingestion Req 9.3).
    _node_pid = payload.get("producing_project_id")
    node_project_token = (
        jobs_context.set_producing_project_id(_node_pid)
        if isinstance(_node_pid, str) and _node_pid else None
    )
    # This node's OWN code-interpreter sandbox. Nodes of a plan used to share one microVM (the
    # chainer now gives each node its own runtime session, but a direct dispatch, a results fetch
    # and older chainers still share) and sandboxes are looked up by name,
    # so without a per-node scope concurrent nodes shared one sandbox — one filesystem, so a
    # node could read another node's /tmp/src.mp4. See sandbox_scope for the measurement.
    import sandbox_scope

    _sandbox_scope = sandbox_scope.node_scope(plan_id, node_id)
    sandbox_token = sandbox_scope.bind(_sandbox_scope)
    #
    # max_tokens is set explicitly (generic to ANY plan node, not specific
    # to translation/media): with no max_tokens, Strands' BedrockModel
    # omits inferenceConfig.maxTokens from the Converse request entirely
    # (see format_request in strands/models/bedrock.py — it only populates
    # maxTokens from the top-level `max_tokens` config key, NOT from
    # additional_request_fields), so Bedrock falls back to its own default
    # ceiling. That default is too small for a node whose task legitimately
    # produces a lot of output — e.g. translating a diarized transcript
    # into two languages — and the request fails with
    # MaxTokensReachedException even when the INPUT prompt is small and
    # clean (confirmed via plan 70cb768c-89ae-4536-8c4b-c29bebdc2b4a's
    # t3_translate: the injected dialogue was ~4.4KB of clean text, not
    # noise, yet output generation still got cut off). This is an output-
    # budget gap, independent of prompt content — any node asked to
    # produce substantial text (long summaries, multi-language output,
    # generated reports) can hit the same ceiling regardless of domain.
    # Model sync with the orchestrator: use the model_id the node was dispatched with (a node
    # may carry its own explicit pick, and the orchestrator's user-selected model is propagated
    # onto the node payload), falling back to the configured default — so node execution runs on
    # the model the user picked rather than always the default.
    node_model_id = (payload.get("model_id") or "").strip() or DEFAULT_MODEL_ID
    node_model = build_model(
        model_id=node_model_id,
        boto_client_config=_boto_cfg,
        max_tokens=NODE_MAX_OUTPUT_TOKENS,
    )
    # Load the node's skills INTO the node agent.
    #
    # This was the silent root cause of "skill guidance is ignored in plan nodes": the plan
    # path never built skills at all. The planner sees only a skill's name+description; the
    # node inherits skill NAMES (79da82c); and _execute_plan_node then constructed its agent
    # with model+tools+a generic prompt and NO AgentSkills plugin. So the model table, the
    # field names, and "submit_job: prompt+duration only, no guessed tuning params" reached
    # neither agent -- the music node guessed num_inference_steps=50 because the rule
    # forbidding it was never in its context.
    #
    # Names are resolved to full instructions here (the node carries names only) via the
    # bundled SkillsManager, mirroring what the direct-dispatch handler does with skill_defs.
    node_plugins = []
    try:
        _skill_names = [
            (x.get("name") if isinstance(x, dict) else x)
            for x in (payload.get("skills") or [])
        ]
        _skill_names = [n for n in _skill_names if isinstance(n, str) and n]
        if _skill_names:
            from skills_manager import SkillsManager

            _by_name = {sk.name: sk for sk in SkillsManager()._load_from_disk()}
            _resolved = [_by_name[n] for n in _skill_names if n in _by_name]
            if _resolved:
                node_plugins.append(AgentSkills(skills=_resolved))
                logger.info("_execute_plan_node %s: loaded skills %s",
                            node_id, [sk.name for sk in _resolved])
    except Exception as exc:  # noqa: BLE001 - a node must run even if skills fail to load
        logger.warning("_execute_plan_node %s: skill load failed (non-fatal): %s", node_id, exc)

    # OUTPUT FORMAT is data-driven by the node's declared output_type (see _output_format_for):
    # a TEXT node emits plain markdown (persisted response IS the deliverable, read downstream by
    # reference) instead of a {"text": <whole doc>} JSON envelope that guarantees the output-token
    # cap mid-emission (research->PDF, 36c99c2d); structured nodes keep the JSON block.
    _output_format = _output_format_for(payload.get("output_type"))

    # One tool per name: a node's list can name the same gateway tool twice (prefixed AND bare, e.g.
    # "usr-…___submit_job" + "submit_job"), and Strands refuses a duplicate registration.
    resolved_tools = _dedupe_tools_by_name(resolved_tools)
    # Lineage (projects.lineageEdges): a node has no hook provider, so it gets the ledger callback alone and its
    # ledger is persisted beside its output (media.lineage_ledger). [] with the flag off: the node runs as before.
    from media import lineage_ledger as _lineage_ledger
    _node_lineage_hooks = _lineage_ledger.node_hooks()
    if _node_lineage_hooks:
        from media import turn_outputs as _node_turn_outputs
        _node_turn_outputs.open_turn()
    try:
        node_agent = Agent(
            model=node_model,
            tools=resolved_tools,
            plugins=node_plugins,
            **({"hooks": _node_lineage_hooks} if _node_lineage_hooks else {}),
            system_prompt=_node_graph_prompt + (
                "You are a focused task execution node in a pipeline. "
                "Complete ONLY the specific task in the prompt. "
                "Call the required tool(s) and return the result. "
                "For async jobs: call the start tool ONCE, return the job_id. Do NOT poll. "
                "Do NOT download files. Keep responses concise.\n\n"
                f"ARTIFACTS — one consistent, per-session place, never a guessed bucket: the workspace "
                f"bucket is `{S3_BUCKET}` and this session's OUTPUT artifacts live under the "
                f"`{_artifact_output_prefix()}` key prefix. To produce a file (PDF, image, chart, "
                f"dataset), write it from code_interpreter with boto3 to "
                f"`s3://{S3_BUCKET}/{_artifact_output_prefix()}<filename>`, then call "
                f"artifact(operation='adopt', key='<filename>') to register it and get a durable "
                f"CloudFront URL. `{S3_PREFIX.rstrip('/')}` is a KEY PREFIX inside `{S3_BUCKET}`, NOT a "
                f"bucket — NEVER write to a bucket literally named 'artifacts', and never invent a "
                f"bucket name. Read inputs from the exact s3:// URIs given in the prompt; do not "
                f"reconstruct them.\n\n"
                "TOOL USAGE RULES (CRITICAL):\n"
                "- You may ONLY call tools that have been explicitly provided to you in this turn.\n"
                "- If a tool you need is not available, STOP and report that the tool is missing. "
                "Do NOT invent tool calls, fabricate job_ids or results, or pretend a tool ran. "
                'A response like `{"job_id": "job_stub_..."}` when no tool was actually called is a '
                "fabrication and is never acceptable — report the missing tool as an error instead.\n"
                "- Never produce text that mimics tool output (`<tool_call>`, `<tool_response>`, "
                "`<function_calls>`, etc.) — if you cannot complete the task with the tools you have, "
                "say so plainly and clearly mark the result as an error, not a success.\n\n"
                + _output_format
            ),
        )
    except Exception as exc:  # noqa: BLE001 - report it; never leave the node `running` for the watchdog
        error_msg = f"{NODE_SETUP_FAILED}: {type(exc).__name__}: {exc}"
        logger.exception("Node %s: %s", node_id, error_msg)
        yield json.dumps({"type": "step", "status": "failed", "label": error_msg})
        _write_node_failed(error_msg)
        return

    output = ""
    status = "completed"
    stop_reason = ""
    continuations = 0
    # FAST PATH (kind "tool"): the chainer forwarded one fully specified call. Make it directly
    # through the tool's own stream (every wrap applies) instead of running a model loop to arrive
    # at the same call. Any reason it cannot be made as written falls back to the model turn below.
    import plan_node_fast_path

    fast = plan_node_fast_path.resolve_fast_path_call(
        payload.get("tool_call"), tools_list, resolved_tools)
    # The messages _all_tool_calls_failed inspects: the fast call's own result, or the agent's.
    fast_messages = None
    _node_started = time.monotonic()
    try:
        if fast is not None:
            _fast_tool, _fast_args = fast
            try:
                _fast_result = await asyncio.wait_for(
                    plan_node_fast_path.run_tool_call(_fast_tool, _fast_args, node_id),
                    timeout=PLAN_NODE_EXEC_TIMEOUT,
                )
            except Exception as e:  # noqa: BLE001 - the model turn below is the fallback
                if isinstance(e, asyncio.TimeoutError):
                    raise  # the node's wall-clock budget is spent: a node timeout, handled below
                logger.warning("_execute_plan_node %s: fast-path call raised (%s); using the model turn",
                               node_id, e)
                _fast_result = None
            _fast_err = _tool_result_error(_fast_result) if _fast_result else "no result"
            if _fast_result and not _fast_err and _node_lineage_hooks:
                try:  # the fast path makes the call without a model turn, so no hook saw it
                    _node_turn_outputs.note_tool_result(
                        str(getattr(_fast_tool, "tool_name", "") or ""), _fast_args, _fast_result)
                except Exception:  # noqa: BLE001
                    logger.debug("fast-path lineage not recorded", exc_info=True)
            if _fast_result and not _fast_err:
                output = plan_node_fast_path.fast_path_output(_fast_result)
                fast_messages = [{"role": "user", "content": [{"toolResult": _fast_result}]}]
                logger.info("_execute_plan_node %s: fast path — one direct call, no model turn (%.1fs)",
                            node_id, time.monotonic() - _node_started)
            else:
                logger.info("_execute_plan_node %s: fast-path call did not succeed (%s); using the "
                            "model turn", node_id, str(_fast_err)[:200])
        if fast_messages is None:
            output, stop_reason, continuations = await asyncio.wait_for(
                _generate_with_continuation(node_agent, prompt, node_id),
                timeout=max(1.0, PLAN_NODE_EXEC_TIMEOUT - (time.monotonic() - _node_started)),
            )
        # Still truncated after exhausting the continuation budget → the output is genuinely
        # too large for inline generation. Fail with out-of-band guidance so P2's retry
        # rebuilds it out-of-band (write to a file + adopt) rather than shipping a partial.
        if stop_reason == "max_tokens":
            output = _maxtokens_failure_output("max_tokens", [output]) or output
            status = "failed"
            logger.warning("_execute_plan_node %s: still truncated after %d continuation(s) → out-of-band",
                           node_id, continuations)
    except asyncio.TimeoutError:
        # A hung tool (most often a code_interpreter cell that never returns) would otherwise
        # leave this node RUNNING forever and hang the whole plan (session 20f7315e). Fail it with
        # a terminal status so the plan-chainer settles; P2's retry can re-attempt the step.
        logger.error(
            "_execute_plan_node %s: exceeded %.0fs wall clock — failing the node so the plan "
            "can settle (likely a hung code_interpreter/tool)", node_id, PLAN_NODE_EXEC_TIMEOUT)
        status = "failed"
        output = json.dumps({
            "error": "node_timeout",
            "message": (f"This step exceeded {PLAN_NODE_EXEC_TIMEOUT:.0f}s and was stopped so the "
                        "plan would not hang — most likely a tool (e.g. a code_interpreter probe) "
                        "that never returned."),
        })
    except Exception as e:
        logger.error("_execute_plan_node %s failed: %s", node_id, e)
        # P4 (exception path): when a node truncates mid-generation or mid-tool-use, Strands
        # raises "Model stopped generating due to maximum token limit" rather than returning a
        # clean stop_reason (the aws_mcp research node hit this). Same F4 class → same
        # out-of-band remedy, so P2's retry steers it to build out-of-band instead of retrying
        # the same oversized inline generation.
        if _is_maxtokens_error(str(e)):
            # The partial text lived inside _generate_with_continuation and is lost when it
            # raises (mid-tool-use), so there is no preview to attach — the guidance stands
            # on its own and P2's retry rebuilds it out-of-band.
            output = _maxtokens_failure_output("max_tokens", []) or f"Error: {e}"
        else:
            output = f"Error: {e}"
        status = "failed"

    # Write output to S3. This durable file is the plan's AUTHORITATIVE handoff — downstream
    # nodes are handed `result_s3_key` and told to read it for the COMPLETE content — so `s3_key`
    # is set ONLY after a confirmed put. A write that did not land must leave it EMPTY, never a
    # path to a file that does not exist (the "authoritative URI to nothing" dead end). Retry a
    # few times so a transient S3 error does not fail an otherwise-good node.
    s3_key = ""
    if s3_bucket and plan_id and node_id:
        _durable_key = f"plans/{session_id}/{plan_id}/{node_id}/output"
        for _attempt in range(_S3_WRITE_ATTEMPTS):
            try:
                boto3.client("s3", region_name=REGION).put_object(
                    Bucket=s3_bucket, Key=_durable_key, Body=output.encode("utf-8"))
                s3_key = _durable_key
                logger.info("Node %s output written to s3://%s/%s", node_id, s3_bucket, s3_key)
                break
            except Exception as e:
                logger.warning("Node %s S3 write attempt %d/%d failed: %s",
                               node_id, _attempt + 1, _S3_WRITE_ATTEMPTS, e)

    if _node_lineage_hooks and s3_key:
        _lineage_ledger.write_node_lineage(boto3.client("s3", region_name=REGION), s3_bucket, session_id,
                                           plan_id, node_id)

    # Extract structured output value from the agent's response.
    # The agent is instructed to include a ```json block at the end.
    structured_value = _extract_structured_output(output)
    # Post-condition: did the node produce what it DECLARED?
    #
    # status was "completed" unless the agent raised. So a node that correctly
    # refuses -- "I cannot complete the merge step... calling it and claiming it
    # merged would be fabricating a result" -- was recorded as completed, and the
    # plan reported SUCCEEDED with no merged file. Three separate sessions read as
    # a silent mux failure when the node had in fact said so plainly.
    #
    # Checked against the node's OWN declaration rather than a list of node names,
    # so it covers any node that promises a locatable artifact.
    #
    # Each framework-detected reason is ALSO collected here so the plan-state `error` can lead
    # with it (see _node_row_error): appended after the agent's prose, a 2000-char cut dropped it,
    # and the chainer's fail-fast classifiers never saw "Missing required input" (dcd11946).
    failure_notes: list[str] = []
    if status == "completed":
        declared_err = _declared_error(structured_value)
        if declared_err:
            status = "failed"
            failure_notes.append(f"[plan] node declared failure: {declared_err}")
            output = f"{output}\n\n{failure_notes[-1]}"
            logger.warning("Node %s returned a top-level error payload → marking failed: %s",
                           node_id, declared_err[:200])
    if status == "completed":
        unmet = _unmet_output_contract(payload.get("output_type"), structured_value, output)
        if unmet:
            status = "failed"
            failure_notes.append(f"[plan] node did not satisfy its declared output: {unmet}")
            output = f"{output}\n\n{failure_notes[-1]}"
            logger.warning("Node %s declared %r but %s", node_id,
                           payload.get("output_type"), unmet)
    if status == "completed":
        try:
            rejected = _all_tool_calls_failed(
                fast_messages if fast_messages is not None else getattr(node_agent, "messages", None))
        except Exception:  # noqa: BLE001 - the check must never break a node
            rejected = ""
        if rejected:
            status = "failed"
            failure_notes.append(f"[plan] node produced nothing: {rejected}")
            output = f"{output}\n\n{failure_notes[-1]}"
            logger.warning("Node %s: %s → marking failed", node_id, rejected[:200])

    # A plan node that was meant to persist a durable output but did not has NOT met the plan
    # contract: reporting completion would hand downstream a `result_s3_key` pointing at a file
    # that was never written — the exact silent failure that made an upstream bug invisible. Fail
    # instead, so it is seen and the plan's retry can re-run it, rather than a fabricated URI to
    # nothing. Scoped to nodes that were SUPPOSED to persist (bucket + plan + node present).
    if status == "completed" and s3_bucket and plan_id and node_id and not s3_key:
        status = "failed"
        failure_notes.append(f"[plan] durable output write failed after {_S3_WRITE_ATTEMPTS} "
                             f"attempt(s); node cannot report success without a persisted result.")
        output = f"{output}\n\n{failure_notes[-1]}"
        logger.error("Node %s: durable S3 write did not land → marking failed", node_id)


    # Detect if this was an async gateway job (output contains a job_id/ARN).
    # If so, the node enters "processing" state — the polling worker tracks the
    # SFN job, and the bridge will write "results_ready" when it completes.
    # A results-fetch agent then calls get_results and writes "completed".
    # For sync nodes (no job_id in output), mark "completed" directly.
    import re
    # Exclude backticks, brackets, parens, and other markdown/code chars from ARN match
    sfn_arn_match = re.search(r'(arn:aws:states:[a-zA-Z0-9:/_\-]+)', output) if status == "completed" else None
    is_async_job = sfn_arn_match is not None

    # A gateway queue handle is the OTHER kind of async job, and it was invisible.
    # This explicit registration only ever recognised Step Functions, so every job
    # row in the registry was __aws_sfn__ and a fal render -- which returns
    # {request_id, status_url} rather than an ARN -- fell through both this branch
    # and the automatic JobTrackingMCPAgentTool wrap (see the note below on why the
    # wrap cannot be relied on here). The job was therefore never registered, never
    # polled, and never notified, unlike every other async job in the system.
    queue_handle = _extract_queue_handle(structured_value) if status == "completed" else None

    if is_async_job:
        # Reconcile-on-adopt: check the adopted execution's CURRENT status.
        # If it's already terminal (dedup to a prior completed run, or a job
        # that finished before the first poll), skip "processing" and go
        # straight to results_ready/failed — otherwise the node hangs forever
        # waiting on a PENDING->terminal transition that will never fire.
        sfn_arn = sfn_arn_match.group(1)
        adopted_sfn_status = _describe_sfn_status(sfn_arn)
        effective_status = _node_status_for_adopted_job(adopted_sfn_status)
        _reg_status = (
            "SUCCEEDED" if effective_status == "results_ready"
            else "FAILED" if effective_status == "failed"
            else "PENDING"
        )
        logger.info(
            "Node %s is async (SFN ARN: %s) — SFN status=%s -> node %s",
            node_id, sfn_arn[:60], adopted_sfn_status, effective_status,
        )

        # Explicitly register the gateway job in the Job Registry so the
        # polling worker can track it and the bridge can fire on completion.
        # This bypasses the automatic JobTrackingMCPAgentTool wrap (which may
        # not fire depending on the Strands execution path).
        if JOB_REGISTRY_TABLE and actor_id:
            try:
                from jobs.registry import JobRegistry
                from jobs.models import JobRecord
                from datetime import datetime, timezone as tz
                registry = JobRegistry(table_name=JOB_REGISTRY_TABLE, region=REGION)
                registry.register(JobRecord(
                    job_id=sfn_arn,
                    actor_id=actor_id,
                    session_id=session_id,
                    trio_id=f"plan-node|{node_id}",
                    gateway_id="__aws_sfn__",
                    status_tool="",
                    results_tool="",
                    status=_reg_status,
                    start_at=datetime.now(tz.utc).isoformat(),
                    updated_at=datetime.now(tz.utc).isoformat(),
                    run_id=plan_id,
                    # JOINABLE TO ITS PLAN NODE. Without this, one logical job had two
                    # UNLINKED registry rows: `<PLAN>#<node>` (has nodeId, no sourceMedia)
                    # and this ARN row (has sourceMedia, no nodeId). The render resolves
                    # source media BY NODE, found none, and drew the analysis with no video
                    # to overlay — the empty sourceUrl in sessions a2ebd1e9 / a5a1b4fa.
                    #
                    # `node_id` was already in scope (used by `trio_id` above and the log
                    # line below), and the QUEUE registration ~60 lines down has always
                    # passed it: this was an inconsistency between two adjacent call sites,
                    # not a missing capability. `JobRecord.node_id` predates both.
                    #
                    # A run-level fallback is NOT a substitute: a fanout plan runs ONE tool
                    # over SEVERAL different media (a5a1b4fa: 3 shot-segmentations on 3
                    # videos), so borrowing any source from the run attaches the wrong one.
                    node_id=node_id,
                    plan_node_id=node_id,
                    source_media=_node_source_media(payload),
                ))
                logger.info("Node %s: registered SFN job %s in Job Registry (%s)", node_id, sfn_arn[:40], _reg_status)
                # Mirror the source media onto the PLAN-NODE row as well.
                #
                # The render resolves a plan node's canvas with a get_item by PRIMARY KEY, and
                # inside a plan the row it can name is `<plan_id>#<node_id>` (written by the Plan
                # Chainer) — not this ARN row. So recording the source only here left the plan
                # path unable to find it, which is the empty `sourceUrl` in a2ebd1e9 / a5a1b4fa.
                # This site is the only one that holds all three facts (plan, node, source), so it
                # is where the two rows get reconciled.
                #
                # Best-effort and non-destructive: it only sets sourceMedia, only when we actually
                # captured one, and never fails the dispatch. `if_not_exists` so a value already
                # recorded by another path wins over a re-registration.
                _srcs = _node_source_media(payload)
                if _srcs and plan_id and node_id:
                    try:
                        boto3.resource("dynamodb", region_name=REGION).Table(
                            JOB_REGISTRY_TABLE
                        ).update_item(
                            Key={"jobId": f"{plan_id}#{node_id}"},
                            UpdateExpression=(
                                "SET sourceMedia = if_not_exists(sourceMedia, :s), "
                                "executionArn = if_not_exists(executionArn, :arn)"
                            ),
                            ExpressionAttributeValues={":s": _srcs, ":arn": sfn_arn},
                        )
                    except Exception as exc:  # noqa: BLE001 - never fail a dispatch over a mirror
                        logger.warning(
                            "Node %s: could not mirror sourceMedia onto %s#%s: %s",
                            node_id, plan_id, node_id, exc)
            except Exception as exc:
                logger.warning("Node %s: SFN job registration failed: %s", node_id, exc)

    elif queue_handle:
        # A submitted queue job is NOT finished: the node has handed work to the
        # gateway and the polling worker drives it to terminal state, exactly as for
        # an adopted SFN execution. Set this BEFORE the registration below, which is
        # best-effort -- forgetting it left effective_status unbound and every node
        # died with "cannot access local variable 'effective_status'" AFTER doing its
        # work, so a succeeded node was reported as Failed.
        #
        # Reconcile-on-adopt, as for SFN: a node that waits for its own job (polls it, or
        # just takes long) can finish AFTER the job did. The job's single terminal event
        # has then already fired, so "processing" would never be followed by anything and
        # the plan hung (session c3447913). Read the job's current status instead.
        effective_status, _queue_error = _node_status_for_queue_job(queue_handle["job_id"])
        if effective_status == "failed" and _queue_error:
            failure_notes.append(f"[plan] the submitted job failed: {_queue_error}")
            output = f"{output}\n\n{failure_notes[-1]}"

    else:
        # A synchronous node: its own status stands. This `else` closes the async/queue chain above. It
        # used to sit on the queue-REGISTRATION `if` below (split out 2026-08-06), so a node that started
        # a Step Functions job and had no fal queue handle fell into it and was written `completed` while
        # its job still ran. Then the bridge (which never reopens a completed row) applied nothing, the
        # chainer waited for an event that never came, and the plan hung (staging 82654e2f, 2f8bd1c4).
        effective_status = status

    if queue_handle and JOB_REGISTRY_TABLE and actor_id:
        # Same purpose as the SFN branch, for a gateway queue submission. Registering
        # with the trio's REAL status/results tools is the point: the plan/node
        # coordinator rows carry empty tools deliberately and are therefore
        # unpollable, so a row without them would appear in the panel and never move.
        trio = _queue_trio_for()
        if not trio:
            logger.warning(
                "Node %s produced queue handle %s but no queue trio is declared on any "
                "connected gateway, so it cannot be polled; declare job roles on the "
                "gateway's registry row (job_role_overrides)",
                node_id, queue_handle["job_id"][:40],
            )
        else:
            try:
                from jobs.registry import JobRegistry
                from jobs.models import JobRecord
                from datetime import datetime, timezone as tz

                # The polling worker reaches a user_jwt gateway as a machine and proves
                # WHOSE job it polls with this grant; without it the interceptor can
                # resolve no actor and correctly denies.
                _grant = None
                try:
                    from security import delegation as _delegation

                    if _delegation.grants_enabled() and _delegation.can_delegate(actor_id):
                        _grant = _delegation.mint_grant(actor_id, queue_handle["job_id"])
                except Exception as exc:  # noqa: BLE001
                    logger.debug("no delegation grant for queue job: %s", exc)

                registry = JobRegistry(table_name=JOB_REGISTRY_TABLE, region=REGION)
                _now = datetime.now(tz.utc).isoformat()
                registry.register(JobRecord(
                    job_id=queue_handle["job_id"],
                    actor_id=actor_id,
                    session_id=session_id,
                    trio_id=trio["trio_id"],
                    gateway_id=trio["gateway_id"],
                    status_tool=trio["status_tool"],
                    results_tool=trio["results_tool"],
                    status="PENDING",
                    start_at=_now,
                    updated_at=_now,
                    run_id=plan_id,
                    node_id=node_id,
                    plan_node_id=node_id,
                    delegation_grant=_grant,
                    endpoint_id=queue_handle.get("endpoint_id") or None,
                    source_media=_node_source_media(payload),
                ))
                logger.info(
                    "Node %s: registered queue job %s on %s (status=%s results=%s grant=%s)",
                    node_id, queue_handle["job_id"][:40], trio["gateway_id"],
                    trio["status_tool"], trio["results_tool"], "yes" if _grant else "no",
                )
            except Exception as e:
                # Guards the QUEUE registration above -- the SFN branch has its own
                # handler. Registration is best-effort: losing the job row costs the
                # notification, it must not fail the node that already did the work.
                logger.warning("Node %s: failed to register queue job: %s", node_id, e)

    # Update Plan State table
    if ddb_table and session_id and node_id:
        try:
            state_store = StateStore(table_name=ddb_table, region=REGION)
            # For async nodes, store the gateway job_id (SFN ARN) on the plan
            # state record. The changefeed bridge uses this to match the
            # completed gateway job back to this specific node.
            extra_attrs = {}
            if is_async_job and sfn_arn_match:
                extra_attrs["job_id"] = sfn_arn_match.group(1)
            elif queue_handle:
                # The endpoint travels with the id: the queue's results tool needs both,
                # and the chainer's results fetch reads them from this row.
                if queue_handle.get("endpoint_id"):
                    extra_attrs["endpoint_id"] = queue_handle["endpoint_id"]
                # The SAME field, for a gateway queue job. The changefeed bridge maps a
                # completed job back to its node through this id; without it both jobs
                # reached SUCCEEDED while their nodes sat in "processing" forever and
                # the dependent mux node was never dispatched. Only the SFN branch ever
                # wrote it, so queue jobs completed invisibly.
                extra_attrs["job_id"] = queue_handle["job_id"]
            # Record the failure reason on the node row (Phase 1.5). Previously a
            # failed node (e.g. output truncated at the token cap) wrote its guidance
            # ONLY to the S3 output, leaving the plan-state `error` empty — so the
            # chainer's self-correction retry (_should_retry_node needs a non-empty
            # error) never fired, and the plan could hang. `output` here carries the
            # actionable message (the truncation guidance is the corrective the retry
            # injects), so persist a bounded copy as the error — framework reasons FIRST.
            if status == "failed" or effective_status == "failed":
                extra_attrs["error"] = _node_row_error(output, failure_notes)
            # Never regress a row that moved on while this node ran: its job may already have been
            # bridged to results_ready and fetched to completed by the chainer.
            wrote = state_store.put_node_status(
                session_id, node_id, effective_status,
                plan_id=plan_id,
                result_s3_key=s3_key,
                output_type="text",
                unless_status=_NODE_ROW_ADVANCED,
                **extra_attrs,
            )
            if wrote:
                logger.info("Node %s plan state updated: %s", node_id, effective_status)
            else:
                logger.info("Node %s plan state already advanced past this run; left as is "
                            "(would have written %s)", node_id, effective_status)
        except Exception as e:
            logger.warning("Node %s plan state update failed: %s", node_id, e)

    # Update Job Registry node entry (for Activity panel)
    if JOB_REGISTRY_TABLE and plan_id and node_id:
        try:
            from jobs.registry import JobRegistry
            registry = JobRegistry(table_name=JOB_REGISTRY_TABLE, region=REGION)
            if is_async_job:
                if effective_status == "failed":
                    # Reconcile-on-adopt found the job already terminal-failed;
                    # mark the node terminal so the run can finalize.
                    registry.set_terminal(
                        job_id=f"{plan_id}#{node_id}",
                        status="FAILED",
                    )
                else:
                    # processing OR results_ready — still active; the chainer's
                    # results-fetch (or the bridge) will terminalize it.
                    registry.set_active_status(
                        job_id=f"{plan_id}#{node_id}",
                        status="RUNNING",
                        expected_status="PENDING",
                    )
            else:
                job_status = "SUCCEEDED" if status == "completed" else "FAILED"
                registry.set_terminal(
                    job_id=f"{plan_id}#{node_id}",
                    status=job_status,
                    **({"error": (output or "")[:1000]} if status == "failed" else {}),
            )
        except Exception as e:
            logger.debug("Node %s job registry update failed: %s", node_id, e)

    # Write to plan manifest (for sync completed nodes only — async nodes
    # get their manifest entry written by the results-fetch agent)
    if not is_async_job and status == "completed" and s3_bucket and plan_id:
        try:
            from planning.plan_manifest import PlanManifest
            manifest = PlanManifest(
                bucket=s3_bucket, session_id=session_id,
                plan_id=plan_id, region=REGION,
            )
            manifest.write_node_output(node_id, value=structured_value, status="completed")
            logger.info("Node %s manifest updated", node_id)
        except Exception as e:
            logger.debug("Node %s manifest write failed: %s", node_id, e)

    # Cleanup context
    if run_id_token:
        jobs_context.reset_run_id(run_id_token)
    if jobs_ctx_token:
        jobs_context.reset_request_context(jobs_ctx_token)
    if node_project_token is not None:
        try:
            jobs_context.reset_producing_project_id(node_project_token)
        except Exception:  # noqa: BLE001 — a different context; nothing to restore
            pass
    if node_turn_token:
        jobs_context.reset_turn_id(node_turn_token)
    if node_id_token:
        jobs_context.reset_node_id(node_id_token)
    if planned_endpoints_token:
        jobs_context.reset_planned_endpoints(planned_endpoints_token)
    # Stop this node's sandboxes now rather than let them idle to the 900s session timeout.
    # After the plan-state write on purpose, so the plan advances without waiting on it.
    sandbox_scope.reset(sandbox_token)
    try:
        await asyncio.to_thread(sandbox_scope.release, globals().get("_ci"), _sandbox_scope)
    except Exception as e:  # noqa: BLE001 - cleanup must never fail a finished node
        logger.debug("Node %s sandbox release skipped: %s", node_id, e)

    yield json.dumps({"type": "step", "status": status, "label": f"Node {node_id}: {status}"})
    yield output


# ---------------------------------------------------------------------------
# Plan Node Results Fetch (Pattern D — async job results retrieval)
# ---------------------------------------------------------------------------

async def _fetch_plan_node_results(payload: dict):
    """Fetch results for a completed async job and write to plan state.

    Dispatched by the Plan Chainer when a node reaches "results_ready".
    This agent:
    1. Calls the results tool for the completed async job
    2. Writes the actual output to S3
    3. Transitions the node to "completed" with result_s3_key

    This is a short-lived invocation (~10-30s) — just one tool call.
    """
    from state_store import StateStore
    from jobs import context as jobs_context

    node_id = payload.get("node_id", "")
    plan_id = payload.get("plan_id", "")
    session_id = payload.get("__plan_session_id", payload.get("session_id", ""))
    actor_id = payload.get("actor_id", "")
    s3_bucket = payload.get("s3_bucket", S3_BUCKET)
    ddb_table = payload.get("ddb_table", DDB_TABLE)
    job_id = payload.get("job_id", "")  # The SFN ARN
    results_tool = payload.get("results_tool", "")  # e.g. "audio-stem-separation___get_stem_separation_results"

    logger.info("_fetch_plan_node_results: node=%s, plan=%s, job_id=%s", node_id, plan_id, job_id[:50] if job_id else "")
    yield json.dumps({"type": "step", "status": "running", "label": f"Fetching results for {node_id}"})

    # Load gateways for tool resolution
    await _ensure_gateways_loaded(
        actor_id=payload.get("actor_id", "") or "",
        user_token=payload.get("user_token", "") or "",
        delegation_grant=payload.get("delegation_grant", "") or "",
    )

    # Resolve the results tool
    gw_tools_map = _all_gateway_tools_by_name()
    resolved_tools = []
    if results_tool and results_tool in gw_tools_map:
        resolved_tools = [gw_tools_map[results_tool]]
    elif results_tool:
        # Try bare name match
        gw_by_bare = {}
        for full_name in gw_tools_map:
            if '___' in full_name:
                bare = full_name.rsplit('___', 1)[-1]
                gw_by_bare[bare] = gw_tools_map[full_name]
        if results_tool in gw_by_bare:
            resolved_tools = [gw_by_bare[results_tool]]

    # If we couldn't resolve a specific tool, give it all gateway tools
    if not resolved_tools:
        excluded = ('code_interpreter', 'http_request')
        resolved_tools = list(gw_tools_map.values()) + [
            _BUILTINS[name] for name in _BUILTINS if name not in excluded
        ]

    # Bind job context
    run_id_token = None
    jobs_ctx_token = None
    if plan_id:
        run_id_token = jobs_context.set_run_id(plan_id)
    if actor_id and session_id:
        try:
            jobs_ctx_token = jobs_context.set_request_context(
                actor_id=actor_id, session_id=session_id,
            )
        except Exception:
            pass

    # Build the prompt for the results-fetch agent
    prompt = (
        f"The async job has completed successfully. "
        f"Job ID: {job_id}\n\n"
        f"Call the results tool to retrieve the output of this completed job. "
        f"Return the results (file locations, S3 URIs, or text output) clearly."
    )
    if results_tool:
        prompt += f"\n\nUse this tool: {results_tool.split('___')[-1] if '___' in results_tool else results_tool}"

    # Execute with a focused system prompt — minimal reasoning, just fetch and return
    #
    # NOTE: previously this passed
    # additional_request_fields={"inferenceConfig": {"maxTokens": 8192}},
    # which is a no-op — additional_request_fields maps to Bedrock's
    # additionalModelRequestFields, a separate field for model-specific
    # extras; nesting inferenceConfig inside it does nothing. maxTokens
    # is only honored via the top-level max_tokens config key (see
    # BedrockModel.format_request in strands/models/bedrock.py). Fixed
    # to use max_tokens directly so this node's output cap is actually
    # applied instead of silently falling back to Bedrock's default.
    node_model = build_model(
        model_id=DEFAULT_MODEL_ID,
        boto_client_config=_boto_cfg,
        max_tokens=NODE_MAX_OUTPUT_TOKENS,
    )
    node_agent = Agent(
        model=node_model,
        tools=resolved_tools,
        system_prompt=(
            "You are fetching results for a completed async job. "
            "Call the results/get tool with the provided job ID. "
            "Return ONLY the key output data — S3 URIs of produced files. "
            "Do NOT include presigned URLs, cost breakdowns, or verbose metadata. "
            "Do NOT start new jobs. Keep your response under 500 words.\n\n"
            "OUTPUT FORMAT: End with a ```json block containing the key outputs:\n"
            '```json\n{"speech_stem": "s3://...", "music_stem": "s3://..."}\n```'
        ),
    )

    output = ""
    status = "completed"
    try:
        text_chunks: list[str] = []
        stream = node_agent.stream_async(prompt)
        async for event in stream:
            if "data" in event and isinstance(event["data"], str):
                text_chunks.append(event["data"])
        output = "".join(text_chunks) or "(no output)"
    except Exception as e:
        logger.error("_fetch_plan_node_results %s failed: %s", node_id, e)
        output = f"Error fetching results: {e}"
        status = "failed"

    # Write output to S3
    s3_key = ""
    if s3_bucket and plan_id and node_id:
        try:
            s3 = boto3.client("s3", region_name=REGION)
            s3_key = f"plans/{session_id}/{plan_id}/{node_id}/output"
            s3.put_object(Bucket=s3_bucket, Key=s3_key, Body=output.encode("utf-8"))
            logger.info("Node %s results written to s3://%s/%s", node_id, s3_bucket, s3_key)
        except Exception as e:
            logger.warning("Node %s S3 write failed: %s", node_id, e)

    # Update Plan State: transition from results_ready → completed (or failed)
    if ddb_table and session_id and node_id:
        try:
            state_store = StateStore(table_name=ddb_table, region=REGION)
            state_store.put_node_status(
                session_id, node_id, status,
                plan_id=plan_id,
                result_s3_key=s3_key,
                output_type="text",
            )
            logger.info("Node %s results fetch: %s", node_id, status)
        except Exception as e:
            logger.warning("Node %s plan state update failed: %s", node_id, e)

    # Update Job Registry node entry
    if JOB_REGISTRY_TABLE and plan_id and node_id:
        try:
            from jobs.registry import JobRegistry
            registry = JobRegistry(table_name=JOB_REGISTRY_TABLE, region=REGION)
            job_status = "SUCCEEDED" if status == "completed" else "FAILED"
            registry.set_terminal(
                job_id=f"{plan_id}#{node_id}",
                status=job_status,
            )
        except Exception as e:
            logger.debug("Node %s job registry update failed: %s", node_id, e)

    # Write structured output to plan manifest
    if status == "completed" and s3_bucket and plan_id:
        try:
            from planning.plan_manifest import PlanManifest
            structured_value = _extract_structured_output(output)
            manifest = PlanManifest(
                bucket=s3_bucket, session_id=session_id,
                plan_id=plan_id, region=REGION,
            )
            manifest.write_node_output(node_id, value=structured_value, status="completed")
            logger.info("Node %s manifest updated (results-fetch)", node_id)
        except Exception as e:
            logger.debug("Node %s manifest write failed: %s", node_id, e)

    # Cleanup context
    if run_id_token:
        jobs_context.reset_run_id(run_id_token)
    if jobs_ctx_token:
        jobs_context.reset_request_context(jobs_ctx_token)

    yield json.dumps({"type": "step", "status": status, "label": f"Results {node_id}: {status}"})
    yield output


@app.entrypoint
async def invoke(payload, context):
    logger.debug("TaskAgent: invoke payload=%s", _payload_summary(payload))
    # Web Search follows the turn that dispatched this work: `web_search` true only from a chat with it on
    # (dispatch_tool). A plan node, or any payload without it, never searches (fail closed).
    import web_search as _web_search
    _web_search.set_turn_allowed(payload.get("web_search") is True if isinstance(payload, dict) else False)

    # --- Pattern D: Plan Node single-action execution ---
    # Dispatched by the Plan Chainer Lambda for each ready node.
    if payload.get("__plan_node"):
        async for event in _execute_plan_node(payload):
            yield event
        return

    # --- Pattern D: Plan Node results-fetch ---
    # Dispatched by the Plan Chainer when a node reaches "results_ready".
    # Calls the results tool for the completed async job, writes output to S3,
    # then transitions the node to "completed" with result_s3_key.
    if payload.get("__plan_node_results"):
        async for event in _fetch_plan_node_results(payload):
            yield event
        return

    prompt = payload.get("prompt", "")
    if not prompt:
        yield json.dumps({"error": "prompt is required"})
        return

    # First-call gateway load. Cheap on subsequent invokes (idempotent
    # check before the lock). Same pattern as `main.py`.
    await _ensure_gateways_loaded(
        actor_id=payload.get("actor_id", "") or "",
        user_token=payload.get("user_token", "") or "",
        # Token-less dispatches (e.g. from a Slack turn) forward a delegation
        # grant instead of a user JWT; thread it so the shared user_jwt gateways
        # (fal) connect via the machine-token + grant path rather than deferring.
        # The plan-node handlers already pass this; the dispatch entrypoint must
        # too, or dispatched image/gen work sees only M2C on this runtime.
        delegation_grant=payload.get("delegation_grant", "") or "",
    )

    # ---- Custom Agent invoke-time config resolution (task 3.1) ----
    # A Custom Agent multiplexes onto this shared runtime. When its stored
    # config arrives under ``custom_agent`` it is mapped onto the payload
    # fields this entrypoint already consumes: persona -> system_prompt,
    # model -> model_id (omitted when unset so the runtime default applies),
    # compatible_skills -> skills, and tool_collections -> gateway tool names
    # -> tools. Unresolvable tool collections are dropped-and-logged and the
    # invocation continues with the resolvable remainder (skills-only when
    # none resolve). ``tools`` is ALWAYS emitted as an explicit list so the
    # owner-selected allowlist is never bypassed. Runs after the gateway load
    # so the resolver can bind against the connected tool names.
    _custom_agent_cfg = payload.get("custom_agent")

    # An invocation is *agent-scoped* when it names an agent AND carries a
    # marker saying who asked:
    #   principal      → external HTTP invoke (proxy-stamped)
    #   agent_test     → the owner's test console (POST /agent-test)
    #   delegated      → the orchestrator's invoke_custom_agent tool
    # Kept as one predicate so every agent-scoped path shares the fail-closed
    # guarantee below. `delegated` is deliberately NOT spelled `agent_test`:
    # the two will want different audit lines, rate limits, and ceilings, and
    # conflating them makes that inexpressible.
    _agent_scoped = bool(payload.get("agent_id")) and (
        isinstance(payload.get("principal"), dict)
        or payload.get("agent_test") is True
        or payload.get("delegated") is True
    )

    if _custom_agent_cfg is None and _agent_scoped:
        try:
            _custom_agent_cfg = await _fetch_external_custom_agent_config(payload)
        except Exception:  # noqa: BLE001 — resolution must never break invoke
            logger.exception("agent config: fetch failed")
            _custom_agent_cfg = None

    if _agent_scoped and not isinstance(_custom_agent_cfg, dict):
        # Never proceed bare on an agent-scoped path. `payload["tools"]` may be
        # absent, and absent means "load ALL tools" (see below) — so a
        # swallowed resolution failure would silently hand a generic agent
        # every gateway tool. Erroring also beats running with `tools: []`: a
        # toolless generic assistant still answers confidently in the wrong
        # persona, which reads as success.
        _aid = str(payload.get("agent_id") or "")
        _ver = payload.get("version")
        logger.error(
            "agent config unresolved for agent_id=%s version=%r; refusing to "
            "run with a generic configuration", _aid, _ver,
        )
        yield json.dumps({
            "error": "agent_config_unresolved",
            "message": (
                f"Custom Agent {_aid} (version {_ver}) could not be resolved; "
                "refusing to run with a generic configuration."
            ),
            "agent_id": _aid,
            "version": _ver,
        })
        return

    if isinstance(_custom_agent_cfg, dict):
        try:
            _available_tool_names = set(_BUILTINS) | set(_all_gateway_tools_by_name())
            _resolved = _resolve_custom_agent_config(
                persona=_custom_agent_cfg.get("persona"),
                model=_custom_agent_cfg.get("model"),
                skills=_custom_agent_cfg.get("skills") or [],
                tool_collections=_custom_agent_cfg.get("tool_collections") or [],
                available_tool_names=_available_tool_names,
                known_model_ids=_known_model_ids(),
                compatible_skills=_custom_agent_cfg.get("compatible_skills"),
            )
            _resolved.apply_to_payload(payload)
            if _resolved.dropped_collections:
                logger.info(
                    "Custom Agent resolver: dropped %d unresolvable tool "
                    "collection(s): %s",
                    len(_resolved.dropped_collections),
                    _resolved.dropped_collections,
                )
            logger.info(
                "Custom Agent resolver: bound %d tool(s), %d skill(s), model_id=%s",
                len(_resolved.tools),
                len(_resolved.skills),
                _resolved.model_id or "<default>",
            )
        except Exception:  # noqa: BLE001 — resolution must never break invoke
            logger.exception(
                "Custom Agent resolver failed; continuing with raw payload fields"
            )

    requested_tools = payload.get("tools", None)  # None = load all; [] = load none

    # Fail closed for EVERY agent-scoped marker, not just ``principal``.
    #
    # ``tools`` absent means "load ALL tools" (see the resolution block below),
    # so an agent-scoped invocation that reaches here without an explicit list
    # gets every built-in and every connected gateway tool — the exact opposite
    # of the owner's allowlist. Two routes lead here even with the
    # ``_agent_scoped`` guard above satisfied:
    #   * the resolver RAISES (not returns None). That exception is caught and
    #     logged "continuing with raw payload fields", so ``apply_to_payload``
    #     never runs and ``tools`` is never written onto the payload.
    #   * a caller sends no ``tools`` key and no ``custom_agent`` block that
    #     resolves to one.
    # The ``agent_test`` path hits the first: ``buildAgentTestPayload`` sends no
    # ``tools``, so before this coercion a resolver failure produced a generic
    # assistant holding every gateway tool and a green-looking run. ``delegated``
    # was safe only because ``invoke_custom_agent`` puts ``tools: []`` on the
    # wire explicitly — defence in depth carrying load the guard should carry.
    #
    # Coerced at the point the "absent = all" semantics enter, so no future code
    # between here and the resolution block can reintroduce the gap. The
    # principal-gated coercion further down is kept as a second layer.
    if _agent_scoped and not isinstance(requested_tools, list):
        logger.error(
            "agent-scoped invoke reached tool resolution without an explicit "
            "tool list (agent_id=%s); binding NO tools rather than all",
            payload.get("agent_id"),
        )
        requested_tools = []

    skill_defs = payload.get("skills", [])
    model_id = payload.get("model_id", DEFAULT_MODEL_ID)
    session_id = payload.get("session_id", "")
    # Appends the async-job contract on the external path so a persona can't
    # drop it (see :func:`_resolve_system_prompt`).
    system_prompt = _resolve_system_prompt(payload)

    # Set OTEL baggage for trace correlation
    if session_id:
        try:
            from opentelemetry import baggage, context
            ctx = baggage.set_baggage("session.id", session_id)
            context.attach(ctx)
        except Exception:
            pass

    # ---- async-job-tracking request-context binding (R3.1) ----
    # The per-tool wrap's actor/session resolvers read these
    # contextvars on every dispatch. Bind them BEFORE any tool runs
    # so the first ``start_*`` call in this invocation sees an actor.
    # Both fields come from the orchestrator's invoke payload (added
    # in ``dispatch_tool.py``); when missing (legacy callers, dev
    # paths), we leave the contextvars unbound so the wrap's own
    # defensive try/except declines registration cleanly.
    actor_id = payload.get("actor_id", "")
    job_ctx_tokens = None
    if actor_id and session_id:
        try:
            from jobs import context as jobs_context
            job_ctx_tokens = jobs_context.set_request_context(
                actor_id=actor_id,
                session_id=session_id,
            )
        except Exception as exc:
            logger.warning(
                "TaskAgent: failed to bind jobs.context for actor=%r session=%r: %s",
                actor_id, session_id, exc,
            )

    # Inherit the dispatcher's turn so jobs registered here group with the
    # parent request's. Absent (a direct invoke, or an older caller) leaves it
    # unbound, which degrades to session-wide scoping rather than failing.
    turn_ctx_token = None
    _turn_id = payload.get("turn_id") if isinstance(payload, dict) else None
    if isinstance(_turn_id, str) and _turn_id.strip():
        try:
            from jobs import context as jobs_context
            turn_ctx_token = jobs_context.set_turn_id(_turn_id.strip())
        except Exception as exc:
            logger.warning("TaskAgent: failed to bind turn_id %r: %s", _turn_id, exc)

    # The dispatching turn's Active_Project, so jobs registered here carry it (ingestion Req 9.3).
    _producing = payload.get("producing_project_id") if isinstance(payload, dict) else None
    if isinstance(_producing, str) and _producing:
        try:
            from jobs import context as jobs_context
            jobs_context.set_producing_project_id(_producing)
        except Exception as exc:  # noqa: BLE001
            logger.warning("TaskAgent: failed to bind producing project: %s", exc)

    node_id = payload.get("node_id", "")

    # ---- Two-identity parsing + caller-scoped memory (task 12.2) ----
    # External HTTP invocations of a Custom Agent are stamped by the proxy
    # with TWO distinct identities (custom-agents design, Component 5;
    # Req 6, 10, 19):
    #   * ``actor_id`` (Owner) — already parsed above. It keeps driving
    #     hooks, job/quota attribution, and skill/tool/model resolution.
    #     This is unchanged: the Principal is NEVER used as a resource actor.
    #   * ``principal = {issuer, sub}`` (the issuer-qualified external
    #     caller) — used ONLY as the End_User for conversation memory /
    #     session / audit.
    #
    # When a ``principal`` is present we add the caller-scoped AgentCore
    # Memory the Task Agent has no notion of today, keyed on the segregated
    # ``EXT#`` external key derived from ``(agent_id, issuer, sub)`` — never
    # the Owner ``actor_id``, never ``sub`` alone. The key is a pure,
    # collision-free function of the full triple (identity.derive_external_
    # memory_key), so a request can only ever load/store state under its own
    # (agentId, issuer, sub) and can never touch another principal's,
    # issuer's, or agent's state (Req 10.1-10.5, 19.2).
    #
    # Internal invocations (no ``principal``) preserve existing behavior
    # exactly: no session manager is attached, same as before this task.
    external_session_manager = None
    principal = payload.get("principal")
    if isinstance(principal, dict):
        issuer = principal.get("issuer")
        sub = principal.get("sub")
        # Optional per-conversation nonce (sub-partitions this caller's own
        # memory so "new conversation" starts a fresh thread). Absent → "",
        # which reproduces the caller's default-conversation key.
        conversation_id = principal.get("conversation_id") or ""
        agent_id = payload.get("agent_id", "")
        if not MEMORY_ID:
            # Degrade gracefully, mirroring main.py: with no durable store
            # configured the caller simply runs without memory.
            logger.warning(
                "TaskAgent: external principal present but MEMORY_ID unset; "
                "running external invocation without caller-scoped memory"
            )
        elif issuer and sub and agent_id:
            try:
                mem_config = identity.build_external_memory_config(
                    memory_id=MEMORY_ID,
                    agent_id=agent_id,
                    issuer=issuer,
                    sub=sub,
                    conversation_id=conversation_id,
                )
                # Region/data-residency pinning (Req 18.3): externally-sourced
                # End_User memory is stored/processed in the region required for
                # the caller's issuer. When no residency constraint applies this
                # resolves to the ambient region (unchanged behavior).
                _residency_region = compliance.resolve_residency_region(
                    issuer, default=REGION
                )
                external_session_manager = AgentCoreMemorySessionManager(
                    agentcore_memory_config=mem_config, region_name=_residency_region
                )
                logger.info(
                    "TaskAgent: caller-scoped memory bound to external key %s "
                    "(agent_id=%s region=%s)",
                    mem_config.actor_id, agent_id, _residency_region,
                )
            except Exception as exc:
                # Memory init must never break the turn — same defensive
                # posture as main.py. Fall back to no memory.
                logger.warning(
                    "TaskAgent: external memory init failed (agent_id=%s): %s; "
                    "continuing without caller-scoped memory",
                    agent_id, exc,
                )
        else:
            # A principal was stamped but is under-qualified — refuse to
            # derive an unsafe (issuer-less or agent-less) key. Run without
            # memory rather than key it incorrectly.
            logger.warning(
                "TaskAgent: external principal missing issuer/sub/agent_id "
                "(issuer=%s sub_present=%s agent_id=%s); running without "
                "caller-scoped memory",
                issuer, bool(sub), bool(agent_id),
            )

    # ---- External opt-in tool narrowing + authorization seam (task 13.1) ----
    # Confused-deputy guardrail (Req 11): an externally-invoked agent runs with
    # the Owner's resource access but is driven by Caller prompts, so on the
    # external path the bound tool set is narrowed to the Owner's explicit
    # external opt-in (``integration_config.external_tool_collections``):
    #
    #     external_bound = external_opt_in ∩ resolvable  ⊆ external_opt_in ⊆ allowlist
    #
    # This is computed ONCE here — after session/memory-key derivation and
    # BEFORE dispatch — from config-derived inputs only, so untrusted tool
    # results produced during the turn can never widen it (Req 11.3 / the
    # Property 13 invariance clause). The per-tool/per-caller authorization
    # extension point (Req 11.4) runs in the same seam, above the opt-in set;
    # v1 is a no-op allow-all. Internal (no-principal) invocations are untouched
    # and keep the full Req 7 allowlist resolved above.
    if isinstance(principal, dict):
        _ext_cfg = _custom_agent_cfg if isinstance(_custom_agent_cfg, dict) else {}
        _ext_agent_id = payload.get("agent_id", "")
        # Fail closed: the external path must dispatch an explicit tool list.
        # A missing/None ``tools`` would mean "load all" — never allowed here.
        # Mostly redundant now that the agent-scoped coercion above runs first,
        # but NOT entirely: ``_agent_scoped`` also requires ``agent_id``, so a
        # principal stamped without one still only meets this check.
        if not isinstance(requested_tools, list):
            requested_tools = []
        _external_opt_in = _external_opt_in_tool_names(
            _ext_cfg.get("tool_collections") or [],
            _ext_cfg.get("external_tool_collections") or [],
        )
        # Extension point (Req 11.4): layered above the opt-in set; only ever
        # narrows. v1 pass-through.
        _external_opt_in = set(
            _authorize_external_tools(
                _external_opt_in, principal=principal, agent_id=_ext_agent_id
            )
        )
        _pre = len(requested_tools)
        requested_tools = _narrow_external_tools(requested_tools, _external_opt_in)
        logger.info(
            "TaskAgent: external opt-in narrowing bound %d/%d tool(s) "
            "(agent_id=%s)",
            len(requested_tools), _pre, _ext_agent_id,
        )

    # Resolve tools:
    # - None (not provided) = load ALL tools (built-ins + every connected gateway)
    # - [] (empty list)     = no tools — pure LLM response
    # - ["tool_a", ...]     = only those specific tools; fail if any are missing
    if requested_tools is None:
        tools = list(_BUILTINS.values()) + list(_all_gateway_tools_by_name().values())
    elif len(requested_tools) == 0:
        tools = []
    else:
        tools, missing = _resolve_tools(requested_tools)
        if missing:
            available_gw = sorted(_all_gateway_tools_by_name().keys())
            available_builtin = sorted(_BUILTINS.keys())
            logger.error(
                "Tool resolution failed: missing=%s requested=%s",
                missing, requested_tools,
            )
            yield json.dumps({
                "error": "tool_not_found",
                "message": (
                    f"The following requested tools are not available on this runtime: "
                    f"{missing}. The task cannot proceed."
                ),
                "missing_tools": missing,
                "requested_tools": requested_tools,
                "available_builtin_tools": available_builtin,
                "available_gateway_tools": available_gw,
            })
            return

    # Guarantee the sub-agent's core builtins (artifact, code_interpreter) on an
    # internal dispatch — see _ensure_essential_builtins. No-op for the external
    # (narrowed) path and for tools=[]/None.
    tools = _ensure_essential_builtins(tools, requested_tools, principal=principal)

    # Knowledge graph (the graph extension of project search): a dispatch from a graph-enabled Project's turn
    # carries `project_scope`; the tools reach the Search_Gateway with a Project_Token this runtime mints from
    # its delegation grant (never a forwarded token). Never on an agent-scoped (custom-agent) invoke.
    if not _agent_scoped:
        from project_search.delegated_token import subagent_graph_binding
        _graph_tools, _graph_prompt = await subagent_graph_binding(payload, caller="task")
        if _graph_tools:
            tools = list(tools or []) + _graph_tools
            system_prompt = (system_prompt or "") + _graph_prompt

    # Build skills
    plugins = []
    if skill_defs:
        # Non-dict entries (e.g. bare skill-id strings from a stale caller
        # during a mixed-version rollout) are skipped, not fatal.
        skills = [Skill(name=s["name"], description=s.get("description",""), instructions=s.get("instructions",""))
                  for s in skill_defs
                  if isinstance(s, dict) and s.get("name") and s.get("instructions")]
        if skills:
            plugins.append(AgentSkills(skills=skills))

    # `build_model` (not BedrockModel directly) because it always applies an
    # output cap via `max_tokens_for` — default 16384, BEDROCK_MAX_TOKENS to
    # override. That cap matters here beyond the DAG failure it was written for:
    # with `max_tokens` unset, Strands omits inferenceConfig.maxTokens entirely
    # and Bedrock applies its own smaller default, which truncated a reply
    # mid-URL when an agent inlined several ~1700-char presigned download URLs —
    # losing the whole answer on a job that had actually succeeded. The
    # ASYNC_JOBS_GUIDANCE download rules keep replies small; this is the
    # backstop for any other legitimately long output (transcripts, big tables).
    # test_model_factory asserts no agent module constructs BedrockModel
    # directly, so this must stay routed through the factory.
    model = build_model(model_id=model_id, boto_client_config=_boto_cfg)
    # Ensure no gateway tool name exceeds Bedrock's 64-char toolSpec limit,
    # which would otherwise fail the entire ConverseStream request.
    tools = _alias_oversized_tool_names(tools)
    # Declarative hooks: the render-artifacts default fires here (gateway
    # results tools run on the task agent). Also load THIS actor's enabled
    # hook assets so user-created hooks fire for dispatched results too.
    # Built defensively per request — never breaks the turn.
    #
    # EXCEPTION: when running inside a plan (node_id present), suppress
    # hooks entirely. plan_and_execute_tool renders ONCE at the end after
    # all nodes complete — firing mid-plan wastes work and creates stream
    # collisions (render output_ref clobbers the task text output_ref).
    _hook_providers: list = []
    if not node_id:
        try:
            from hooks.loader import build_provider as _build_hook_provider, load_actor_hook_specs
            _user_hooks = await load_actor_hook_specs(actor_id, session_id)
            _hook_providers = [_build_hook_provider(_user_hooks)]
        except Exception:
            logger.exception("task_agent: failed to build hook provider; continuing without hooks")
    # Attach the caller-scoped session manager only for external
    # (principal-bearing) invocations; internal invocations pass None and
    # retain the pre-task-12.2 stateless behavior. Load/store is therefore
    # confined to the EXT# key derived from (agent_id, issuer, sub).
    _agent_kwargs = dict(
        model=model,
        system_prompt=system_prompt,
        tools=tools,
        plugins=plugins,
        hooks=_hook_providers,
    )
    if external_session_manager is not None:
        _agent_kwargs["session_manager"] = external_session_manager
    task_agent = Agent(**_agent_kwargs)

    logger.info("Executing: %d tools, %d skills, model=%s", len(tools), len(skill_defs), model_id)

    try:
        import time as _time
        _exec_start = _time.time()
        _tool_timings: dict[str, list[float]] = {}
        _cycle_count = 0
        _text_chunks: list[str] = []
        usage: dict = {}

        # ---- Per-invocation execution ceilings (task 14.1, Req 14.3/14.4) ----
        # External (principal-bearing) invocations get bounded iterations,
        # tokens, and wall-clock time; internal invocations use the general
        # (unbounded-by-default) ceilings so existing behavior is preserved.
        # When a ceiling is hit we break out of the stream and emit a terminal
        # SSE ``error`` frame below (never the normal output/summary).
        _is_external = isinstance(principal, dict)
        _ceilings = _select_ceilings(_is_external)
        _ceiling_hit: Optional[dict] = None
        _tokens_used = 0

        # Throttle "Agent cycle #N" emissions. The sub-agent often loops
        # for tens of cycles polling job status, which would dump 70+ rows
        # into the user's steps panel. We emit the first cycle, every
        # CYCLE_EMIT_EVERY thereafter, and any cycle that immediately
        # precedes a tool use (so the user sees activity before each
        # action). This keeps the panel signal-rich without spam.
        CYCLE_EMIT_EVERY = 5

        # ---- Stream vs buffer, decided ONCE before the loop ----
        # Two things need the WHOLE answer before anything is emitted:
        #   * the S3 output_ref path — the plan chainer reads the object, not frames
        #   * whole-text external redaction (Task 2 puts this behind a flag)
        # Otherwise each text delta goes out as it arrives. `session_id` and
        # `node_id` are already bound above; `s3_bucket` used to be read after the
        # loop and moves here so the decision can be made up front.
        _s3_bucket = payload.get("s3_bucket", S3_BUCKET)
        _will_write_s3 = bool(_s3_bucket and session_id and node_id)
        _needs_whole_text_redaction = (
            isinstance(principal, dict) and EXTERNAL_OUTPUT_REDACTION
        )
        _stream_text = not (_will_write_s3 or _needs_whole_text_redaction)

        stream = task_agent.stream_async(prompt)
        # Active channel so hook actions (render/notify) firing in Strands hook
        # callbacks can surface events into this SSE stream. Drained each event.
        _hook_channel = ProgressChannel()
        set_active_channel(_hook_channel)
        # Per-toolUseId tracking — same fix as the orchestrator. Keying on
        # tool name was wrong for poll loops where the same tool runs many
        # times: later closes inherited the first start_time and produced
        # bogus 2000+ second durations.
        active: dict[str, dict] = {}
        async for event in stream:
            # Surface any hook-emitted events (render/notify) produced since the
            # last iteration.
            for _ln in _drain_hook_lines(_hook_channel):
                yield _ln

            # ---- Ceiling: wall-clock (Req 14.3/14.4) ----
            if _ceilings["wall_clock_sec"] and (
                _time.time() - _exec_start
            ) > _ceilings["wall_clock_sec"]:
                _ceiling_hit = {
                    "ceiling": "wall_clock",
                    "limit": _ceilings["wall_clock_sec"],
                    "observed": round(_time.time() - _exec_start, 2),
                }
                break

            # Track event loop cycles (throttled)
            if event.get("start_event_loop"):
                _cycle_count += 1
                # ---- Ceiling: iterations/cycles (Req 14.3/14.4) ----
                if _ceilings["cycles"] and _cycle_count > _ceilings["cycles"]:
                    _ceiling_hit = {
                        "ceiling": "cycles",
                        "limit": _ceilings["cycles"],
                        "observed": _cycle_count,
                    }
                    break
                if _cycle_count == 1 or _cycle_count % CYCLE_EMIT_EVERY == 0:
                    yield json.dumps({
                        "type": "step",
                        "status": "running",
                        "label": f"Agent cycle #{_cycle_count}",
                    })

            # ---- Ceiling: tokens (Req 14.3/14.4) ----
            # Best-effort running total from the agent's accumulated usage
            # metrics (available at cycle boundaries). Never breaks the turn if
            # the metrics surface isn't present.
            if _ceilings["tokens"]:
                try:
                    _elm = getattr(task_agent, "event_loop_metrics", None)
                    _au = getattr(_elm, "accumulated_usage", None) if _elm else None
                    if _au:
                        _tokens_used = int(_au.get("totalTokens", 0) or 0)
                except Exception:
                    pass
                if _tokens_used > _ceilings["tokens"]:
                    _ceiling_hit = {
                        "ceiling": "tokens",
                        "limit": _ceilings["tokens"],
                        "observed": _tokens_used,
                    }
                    break

            # Track tool use start. Emit running on first sight, then
            # refresh the cached info on subsequent current_tool_use
            # events so the close-time label can be rich even if the
            # initial event arrived before input deltas had landed.
            if "current_tool_use" in event:
                ti = event["current_tool_use"]
                if isinstance(ti, dict):
                    tu_id = ti.get("toolUseId") or ""
                    tn = ti.get("name") or ""
                    if tu_id and tn:
                        if tu_id not in active:
                            active[tu_id] = {
                                "name": tn,
                                "started_at": _time.time(),
                                "info": ti,
                            }
                            label = label_for_tool_use(ti)
                            yield json.dumps({
                                "type": "step",
                                "status": "running",
                                "label": label,
                            })
                        else:
                            current = active[tu_id]["info"]
                            if has_input_payload(ti) and (
                                not has_input_payload(current)
                                or len(_safe_input(ti)) > len(_safe_input(current))
                            ):
                                active[tu_id]["info"] = ti

            # Track tool use end — close exactly the matching toolUseId
            if "data" in event and isinstance(event["data"], dict):
                tu_id = event["data"].get("toolUseId")
                if tu_id and tu_id in active:
                    entry = active.pop(tu_id)
                    duration = _time.time() - entry["started_at"]
                    _tool_timings.setdefault(entry["name"], []).append(duration)
                    status, label = label_for_tool_done(entry["info"], result=event["data"])
                    yield json.dumps({
                        "type": "step",
                        "status": status,
                        "label": label,
                        "duration_ms": int(duration * 1000),
                    })

            # Text deltas. When streaming, each one goes out immediately; the
            # accumulation is still needed for the S3 write and the byte-count log.
            if "data" in event and isinstance(event["data"], str):
                _text_chunks.append(event["data"])
                if _stream_text:
                    yield event["data"]

            # Capture final result metrics
            if "result" in event:
                result = event["result"]
                try:
                    metrics = result.metrics if hasattr(result, "metrics") else None
                    summary = metrics.get_summary() if metrics and hasattr(metrics, "get_summary") else {}
                    usage = {}
                    if hasattr(metrics, "accumulated_usage") and metrics.accumulated_usage:
                        u = metrics.accumulated_usage
                        usage = {"inputTokens": u.get("inputTokens", 0), "outputTokens": u.get("outputTokens", 0), "totalTokens": u.get("totalTokens", 0)}
                except Exception:
                    summary = {}
                    usage = {}

        # Close any remaining active tool calls (stream cut short, etc.)
        for tu_id, entry in list(active.items()):
            duration = _time.time() - entry["started_at"]
            _tool_timings.setdefault(entry["name"], []).append(duration)
            status, label = label_for_tool_done(entry["info"])
            yield json.dumps({
                "type": "step",
                "status": status,
                "label": label,
                "duration_ms": int(duration * 1000),
            })
        active.clear()

        # Final drain of any hook-emitted events (e.g. a render fired on the
        # last AfterToolCallEvent), then release the active channel.
        for _ln in _drain_hook_lines(_hook_channel):
            yield _ln
        set_active_channel(None)

        # ---- Per-invocation ceiling termination (task 14.1, Req 14.4) ----
        # A ceiling was reached mid-stream: terminate the invocation with a
        # terminal SSE ``error`` event (not the normal output/summary path) and
        # attribute the partial usage to (agentId, End_User) before returning.
        if _ceiling_hit is not None:
            _agent_id = payload.get("agent_id", "")
            _term_duration_ms = int((_time.time() - _exec_start) * 1000)
            if _ceiling_hit.get("ceiling") == "tokens":
                usage = usage or {"totalTokens": _tokens_used}
            _attribution = _usage_attribution(
                agent_id=_agent_id,
                principal=principal,
                owner_actor_id=actor_id,
                usage=usage,
                cycles=_cycle_count,
                duration_ms=_term_duration_ms,
                terminated=True,
                ceiling=_ceiling_hit.get("ceiling"),
            )
            logger.warning(
                "Task invocation terminated by %s ceiling (limit=%s observed=%s)",
                _ceiling_hit.get("ceiling"),
                _ceiling_hit.get("limit"),
                _ceiling_hit.get("observed"),
            )
            yield json.dumps({
                "type": "error",
                "error": "invocation_ceiling_exceeded",
                "message": (
                    f"Per-invocation {_ceiling_hit.get('ceiling')} ceiling reached "
                    f"(limit={_ceiling_hit.get('limit')}); invocation terminated."
                ),
                "ceiling": _ceiling_hit,
                "attribution": _attribution,
            })
            return

        # Write output to S3 if session context provided
        ddb_table = payload.get("ddb_table", DDB_TABLE)
        full_text = "".join(_text_chunks)

        # ---- External output redaction (task 13.1, Req 11.2 / shared 18.2) ----
        # When EXTERNAL_OUTPUT_REDACTION is enabled (off by default), scrub Owner
        # secrets/tokens/credentials from the output before any external (Caller-visible)
        # SSE frame is emitted. Applied only on the external path when the flag is on
        # (principal present AND EXTERNAL_OUTPUT_REDACTION=true); internal invocations
        # and the default external behavior stream unmodified. Owner known-secret values
        # may be supplied via env for exact-match stripping; pattern redaction always
        # applies as a backstop. See docs/superpowers/specs/2026-09-01-custom-agent-streaming-design.md
        # for the security trade-off.
        if _needs_whole_text_redaction:
            full_text = _redact_secrets(
                full_text, known_secrets=_owner_known_secrets()
            )

        if _will_write_s3:
            try:
                from datetime import datetime, timezone
                s3_key = f"plans/{session_id}/nodes/{node_id}/output"
                s3 = boto3.client("s3", region_name=REGION)
                s3.put_object(Bucket=_s3_bucket, Key=s3_key, Body=full_text.encode("utf-8"), ContentType="text/plain")

                if ddb_table:
                    ddb = boto3.resource("dynamodb", region_name=REGION).Table(ddb_table)
                    now = datetime.now(timezone.utc).isoformat()
                    ddb.put_item(Item={
                        "session_id": session_id,
                        "node_id": f"node:{node_id}",
                        "status": "completed",
                        "output_s3_key": s3_key,
                        "updated_at": now,
                        "completed_at": now,
                        "ttl": int(_time.time()) + 86400,
                    })

                logger.info("Task output written to s3://%s/%s (%d bytes)", _s3_bucket, s3_key, len(full_text))
                yield json.dumps({"type": "output_ref", "s3_key": s3_key, "node_id": node_id})
            except Exception as e:
                logger.warning("Failed to write task output to S3/DDB: %s", e)
                # Fallback: yield the text directly
                yield full_text
        else:
            # No S3 config. When streaming, the deltas above WERE the answer:
            # yielding it again here would deliver it twice to every consumer
            # that accumulates (dispatch_tool._consume_stream, plan_and_execute_tool,
            # and all three UI surfaces).
            if not _stream_text:
                yield full_text

        # Emit execution summary
        _exec_duration = _time.time() - _exec_start
        tool_summary = {}
        for tn, durations in _tool_timings.items():
            tool_summary[tn] = {"calls": len(durations), "total_ms": int(sum(durations) * 1000), "avg_ms": int(sum(durations) / len(durations) * 1000)}

        # ---- Usage attribution on completion (task 14.1, Req 14.5) ----
        # Attribute cost/usage to (agentId, End_User): Owner carries the quota
        # attribution, the issuer-qualified Principal carries the usage record.
        # No-op ({}) for internal invocations (no principal).
        _attribution = _usage_attribution(
            agent_id=payload.get("agent_id", ""),
            principal=principal,
            owner_actor_id=actor_id,
            usage=usage,
            cycles=_cycle_count,
            duration_ms=int(_exec_duration * 1000),
            terminated=False,
        )

        _summary_frame = {
            "type": "execution_summary",
            "total_duration_ms": int(_exec_duration * 1000),
            "cycles": _cycle_count,
            "tools": tool_summary,
            "usage": usage,
            "model": model_id,
            "attribution": _attribution,
        }
        # Lineage (projects.lineageEdges): what this sub-agent wrote and what from, so the dispatching turn can
        # register its outputs with their inputs (orchestration.dispatch_tool merges it into its own ledger).
        try:
            from projects import lineage_contract as _lc
            if _lc.edges_enabled():
                from media import turn_outputs as _to
                _sub_lineage = [{**e, "via": e.get("via") or "task_agent"} for e in _to.lineage()]
                if _sub_lineage:
                    _summary_frame["lineage"] = _sub_lineage
        except Exception:  # noqa: BLE001 - a summary is emitted with or without lineage
            logger.debug("task_agent: lineage not attached", exc_info=True)
        yield json.dumps(_summary_frame)
    except Exception as e:
        logger.exception("Task execution failed")
        yield json.dumps({"error": str(e)})
    finally:
        # ---- async-job-tracking: reset request-context binding ----
        # Pair-matched with ``set_request_context`` above. Wrapped in
        # try/except so a cleanup failure can't mask the original
        # request outcome — the underlying ``contextvars.Token.reset``
        # raises ValueError on double-reset, which we swallow here
        # because re-running an already-cleaned generator is benign
        # in this codebase.
        if job_ctx_tokens is not None:
            try:
                from jobs import context as jobs_context
                jobs_context.reset_request_context(job_ctx_tokens)
            except Exception as exc:
                logger.debug(
                    "TaskAgent: jobs.context reset failed: %s", exc,
                )
        if turn_ctx_token is not None:
            try:
                from jobs import context as jobs_context
                jobs_context.reset_turn_id(turn_ctx_token)
            except Exception as exc:
                logger.debug(
                    "TaskAgent: turn_id reset failed: %s", exc,
                )


if __name__ == "__main__":
    app.run()
