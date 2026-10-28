"""Strands tool for planning and executing complex multi-step goals.

Wires the full pipeline: Planning Agent (one fused call that returns both the
HighLevelPlan and the execution DAG) → Plan Chainer, with execution_mode routing,
plan validation with a Planning-agent self-retry, progress event forwarding, and
cumulative token tracking. Execution is async-only: the plan is written to the
Plan State table and the Plan Chainer lambda drives the DAG reactively.

Fusion is unconditional: the single Planning call is the ONLY way to obtain a DAG.
When the fused DAG is missing or fails validation, the Planning call is re-invoked
with the validation errors fed back for correction, bounded to a few attempts.
"""
import asyncio
import json
import logging
import os
import time
import uuid

import boto3

import runtime_arns
from planning.dag_schema import Plan, normalize_async_job_nodes, normalize_tool_nodes
from planning.for_each import ForEachError, expand_for_each
from planning.high_level_plan import HighLevelPlan, SubGoal
from orchestration.progress_channel import get_active_channel
from planning.validator import validate_plan

logger = logging.getLogger(__name__)


def _emit(status: str, label: str, **kwargs) -> None:
    """Push a progress event to the active channel (if any).

    Known kwargs (agent, phase, plan_id, node_id, duration_ms) are passed
    directly to ProgressEvent. All other kwargs go into the ``extra`` dict.
    """
    ch = get_active_channel()
    if ch:
        known = {"agent", "phase", "plan_id", "node_id", "duration_ms", "extra"}
        event_kwargs = {k: v for k, v in kwargs.items() if k in known}
        extra_kwargs = {k: v for k, v in kwargs.items() if k not in known}
        if extra_kwargs:
            event_kwargs.setdefault("extra", {}).update(extra_kwargs)
        ch.push(status, label, **event_kwargs)


def _gate(gate_id: str, timeout: float = 300.0) -> tuple[bool, str]:
    """Block until the user approves/rejects via the UI, or auto-approve on timeout.

    Returns (approved: bool, feedback: str).
    """
    ch = get_active_channel()
    if ch is None:
        return True, ""  # No channel — auto-approve
    result = ch.wait_for_approval(gate_id, timeout=timeout)
    return result.approved, result.feedback


TOOL_SPEC = {
    "name": "plan_and_execute_tool",
    "description": (
        "For multi-step goals with REAL DEPENDENCIES between the steps. Calls the "
        "Planning Agent to decompose the goal AND design the execution DAG in one "
        "call, routes by execution_mode (direct dispatch, or DAG → Coordinator for "
        "standard/complex workflows), validates the plan, and returns consolidated "
        "results with token usage.\n"
        "Use this ONLY when a later step consumes an earlier step's OUTPUT, so the "
        "order cannot be left to a single sub-agent -- for example generate a video, "
        "generate a soundtrack, then mux the two together.\n"
        "Do NOT use it for a single generation, a single lookup, or a single model "
        "run, even if choosing the model or reading a schema first makes it feel like "
        "two steps: one sub-agent handles that in one turn. If dispatch_tool could do "
        "the whole thing, use dispatch_tool."
    ),
    "inputSchema": {
        "json": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "The complex goal to plan and execute.",
                },
                "constraints": {
                    "type": "string",
                    "description": "Optional constraints or preferences for execution.",
                },
            },
            "required": ["goal"],
        },
    },
}

# --- Environment ---
PLANNING_AGENT_ARN = os.environ.get("PLANNING_AGENT_RUNTIME_ARN", "")
TASK_AGENT_ARN = os.environ.get("TASK_AGENT_RUNTIME_ARN", "")
PLAN_STATE_TABLE = os.environ.get("PLAN_STATE_TABLE", "")
SKILLS_S3_BUCKET = os.environ.get("SKILLS_S3_BUCKET", "")
REGION = os.environ.get("AWS_REGION", "us-west-2")
JOB_REGISTRY_TABLE = os.environ.get("JOB_REGISTRY_TABLE", "")
PLAN_EXECUTION_BUDGET_SECONDS = int(os.environ.get("PLAN_EXECUTION_BUDGET_SECONDS", "600"))

# How many times the fused Planning call may be re-invoked to correct a missing or invalid DAG,
# INCLUDING the first attempt. The first call produced the DAG already; a small ceiling bounds the
# self-retry (each retry is a full Planning invocation on the pinned reasoning model).
PLAN_DAG_MAX_ATTEMPTS = max(1, int(os.environ.get("PLAN_DAG_MAX_ATTEMPTS", "2")))


# The ARN constants above are an import-time snapshot (tests patch them). The deploy no longer puts the
# sub-agent ARNs in runtime env — they are SSM parameters — so every use resolves at CALL time and
# falls back to runtime_arns.get() when the snapshot is empty (e.g. a runtime that started before
# the parameters existed on a brand-new environment).
def _planning_agent_arn() -> str:
    return PLANNING_AGENT_ARN or runtime_arns.get("PLANNING_AGENT_RUNTIME_ARN")


def _task_agent_arn() -> str:
    return TASK_AGENT_ARN or runtime_arns.get("TASK_AGENT_RUNTIME_ARN")


_client = None


def _get_client():
    """Lazy-init the bedrock-agentcore boto3 client."""
    global _client
    if not _client:
        from botocore.config import Config as BotoConfig
        _client = boto3.client(
            "bedrock-agentcore",
            region_name=REGION,
            config=BotoConfig(read_timeout=900, connect_timeout=10, retries={"max_attempts": 1}),
        )
    return _client


# ---------------------------------------------------------------------------
# Per-node skill relevance (scope skills to what a node's TOOLS need)
# ---------------------------------------------------------------------------
#
# When a plan/dispatch node declares no skills, it inherits the session's ACTIVE
# skills (see the inheritance sites below). Inheriting ALL of them buries the one that matters: run 863816b4's fal submit
# nodes carried all 11 active skills, so media-references' "MiniMax has no
# `fal-ai/` prefix" rule competed with ten unrelated bodies (pdf, aws-research,
# graph-assistant, capability-editor, skill-creator, agent-harness-help, ...),
# the agent emitted the prefixed id, and fal rejected it ("unavailable for the
# connected account"). Scoping the inherited set to the node's declared tools
# makes the relevant skill prominent.
#
# Design is fail-toward-keeping (this is a QUALITY layer; L1a/L1b already make a
# bad submit safe):
#   * A skill NOT listed here is broadly useful -> ALWAYS inherited.
#   * A domain-gated skill (listed) is inherited only when a declared tool matches
#     one of its substrings (matched on the bare tool name, so a gateway-namespaced
#     `usr-xxx___submit_job` still matches "submit_job").
#   * A node that declares NO tools can't be scoped -> keep ALL active (preserves
#     the tools=[] mux-node inheritance fix this whole block was added for).
#   * If scoping would remove EVERYTHING -> keep ALL active (never inherit zero).
_SKILL_TOOL_RELEVANCE: dict[str, tuple[str, ...]] = {
    "media-references": (
        "submit_job", "run_model", "check_job", "get_job_result", "get_model_schema",
        "recommend_model", "get_pricing", "cancel_job", "generate_", "edit_",
        "restyle_", "inpaint_", "run_inference",
    ),
    "video-processing": (
        "code_interpreter", "diarize_speech", "segment_shots", "recognize_faces",
        "analyze_conversation", "process_media", "transcribe", "run_inference",
        "get_diarization", "get_transcription", "get_shot", "get_face",
    ),
    "agent-olive": (
        "submit_job", "check_job", "get_job_result", "get_model_schema",
        "search_models", "run_model", "generate_", "edit_", "code_interpreter",
        "artifact", "transcribe", "diarize", "asr", "whisper", "pitch",
        "voice", "mouth", "motion", "probe", "measure",
        # lip-sync pipeline: TTS voice (clone/synthesize), presign, trim, and the
        # sync-lipsync generation — so a TTS / trim / lip-sync node still inherits
        # the agent-olive guidance. (Dropped "endpoint": the anchor-endpoint gate is
        # gone in the lip-sync workflow.)
        "synthesize_speech", "speech", "register_voice", "list_voice",
        "get_download_url", "process_media", "mark_in_out", "lipsync", "sync",
    ),
    "pdf": ("pdf", "extract_text", "read_pdf"),
    "aws-research": ("search_documentation", "read_documentation", "aws___", "aws_mcp"),
    "graph-assistant": ("graph", "cypher", "neptune"),
    "skill-creator": ("create_skill", "skill_crud", "lab_asset"),
    "capability-editor": ("capability", "specialty", "tool_collection", "lab_asset"),
    "agent-harness-help": ("get_available_capabilities",),
}


def _skill_name(s) -> str:
    """Extract a skill's name from a bare string or a ``{"name": ...}`` dict."""
    if isinstance(s, str):
        return s
    if isinstance(s, dict):
        return s.get("name", "")
    return str(s)


def _resolve_skill_refs(skills: list) -> list:
    """Replace name-only skill references with the turn's active skill of that name.

    The Task Agent builds a skill only from a dict carrying ``instructions``, so a bare name (or
    ``{"name": ...}``) the planner returned would be dropped there. A name with no active match is
    dropped here, which lets the caller's inheritance (tool-scoped active skills) apply.
    """
    if not skills:
        return []
    by_name: dict = {}
    try:
        from jobs.context import current_active_skills
        for a in current_active_skills() or []:
            if isinstance(a, dict) and a.get("instructions"):
                by_name.setdefault(_skill_name(a), a)
    except Exception:  # noqa: BLE001 - resolution is best-effort; inheritance still applies
        pass
    out: list = []
    for s in skills:
        if isinstance(s, dict) and s.get("instructions"):
            out.append(s)
            continue
        hit = by_name.get(_skill_name(s))
        if hit is not None and hit not in out:
            out.append(hit)
    return out


def _bare_tool_name(t) -> str:
    """The bare tool name after any gateway prefix (``gw___tool`` -> ``tool``)."""
    if not isinstance(t, str):
        return ""
    return t.split("___")[-1]


def _relevant_skills(active_skills: list, node_tools: list) -> list:
    """Filter the session's ACTIVE skills to those a node with *node_tools* needs.

    See ``_SKILL_TOOL_RELEVANCE`` for the fail-toward-keeping rules. Returns the
    ORIGINAL objects (strings or dicts) from ``active_skills``, unchanged.
    """
    if not node_tools:
        return active_skills  # can't infer relevance -> keep all (mux-node fix)
    tool_names = [_bare_tool_name(t) for t in node_tools]

    def _keep(skill) -> bool:
        subs = _SKILL_TOOL_RELEVANCE.get(_skill_name(skill))
        if subs is None:
            return True  # not domain-gated -> broadly useful, always keep
        return any(sub in tn for tn in tool_names for sub in subs)

    filtered = [s for s in active_skills if _keep(s)]
    return filtered or active_skills  # never inherit zero


# ---------------------------------------------------------------------------
# Runtime invocation helpers
# ---------------------------------------------------------------------------

def _invoke_runtime(arn: str, payload: dict) -> tuple[str, dict]:
    """Invoke an AgentCore runtime and read the streaming response.

    Returns (text_content, token_usage) where token_usage is
    {"inputTokens": N, "outputTokens": N}.

    If the sub-agent emits an output_ref message, reads the output from S3
    instead of relying on SSE chunk reassembly. Falls back to chunk-based
    assembly if no output_ref is found.
    """
    client = _get_client()
    from jobs.context import invoke_agent_runtime_traced as _invoke_traced
    response = _invoke_traced(
        client,
        session_id=payload.get("session_id"),
        agentRuntimeArn=arn,
        qualifier="DEFAULT",
        payload=json.dumps(payload).encode(),
        contentType="application/json",
    )

    body = response.get("response", b"")
    chunks: list[str] = []
    usage = {"inputTokens": 0, "outputTokens": 0}
    _raw_line_samples: list[str] = []
    _output_ref_key: list[str] = []  # mutable container for output_ref s3 key

    def _process_chunk(raw_text: str):
        """Process a chunk of text that may contain concatenated JSON objects."""
        # Split on newlines first
        for line in raw_text.split("\n"):
            # Capture raw line samples for debugging
            if len(_raw_line_samples) < 30:
                _raw_line_samples.append(repr(line))

            stripped = line.strip()
            if not stripped:
                continue
            # Strip SSE prefix
            if stripped.startswith("data: "):
                stripped = stripped[6:]
            elif stripped.startswith("data:"):
                stripped = stripped[5:]
            if not stripped:
                continue

            # Quick check: if the line is a JSON string, decode it first
            # AgentCore wraps yielded values as JSON-encoded strings in SSE
            decoded_line = stripped
            try:
                maybe_decoded = json.loads(stripped)
                if isinstance(maybe_decoded, str):
                    decoded_line = maybe_decoded
                    # If the decoded string is whitespace-only, it's an inter-token
                    # space from the LLM — preserve it directly
                    if not decoded_line.strip():
                        chunks.append(decoded_line)
                        continue
            except (json.JSONDecodeError, TypeError):
                pass

            # Handle concatenated JSON objects: }{ → split them
            parts = _split_concatenated_json(decoded_line)
            for part in parts:
                _process_json_or_text(part)

    def _split_concatenated_json(text: str) -> list[str]:
        """Split a string that may contain concatenated JSON objects like '{}{}...'."""
        results: list[str] = []
        depth = 0
        start = 0
        in_string = False
        escape_next = False

        for i, ch in enumerate(text):
            if escape_next:
                escape_next = False
                continue
            if ch == '\\':
                escape_next = True
                continue
            if ch == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == '{':
                if depth == 0:
                    # Capture any text before this JSON object (preserve whitespace)
                    prefix = text[start:i]
                    if prefix:
                        results.append(prefix)
                    start = i
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    results.append(text[start:i + 1])
                    start = i + 1

        # Capture any trailing text (preserve whitespace)
        remainder = text[start:]
        if remainder:
            results.append(remainder)

        return results if results else [text]

    def _process_json_or_text(part: str):
        """Process a single JSON object or text fragment."""
        stripped = part.strip()
        if not stripped:
            # Preserve whitespace-only fragments — they are inter-token spaces
            if part:
                chunks.append(part)
            return

        # Try to parse as JSON
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, TypeError):
            # Not JSON — treat as raw text (could be a text chunk from the agent)
            # Preserve original whitespace (don't use stripped)
            chunks.append(part)
            return

        # If it's a dict, check if it's a step event, summary, or output_ref (skip/capture)
        if isinstance(parsed, dict):
            evt_type = parsed.get("type", "")
            if evt_type in ("step", "execution_summary"):
                if evt_type == "execution_summary" and parsed.get("lineage"):
                    # projects.lineageEdges: a directly-run step's outputs join this turn's ledger.
                    try:
                        from media import turn_outputs as _turn_outputs
                        _turn_outputs.merge_lineage(parsed.get("lineage"), via="task_agent")
                    except Exception:  # noqa: BLE001
                        pass
                return
            if evt_type == "output_ref" and parsed.get("s3_key"):
                # A render manifest is NOT this node's text output — it is a
                # render artifact that reaches the UI via the changefeed /
                # dispatch_tool render path, never through here. Capturing it as a
                # node-text candidate only worked because the real text output_ref
                # happened to be emitted last (see task_agent's collision note); a
                # reorder would make the manifest JSON masquerade as the node's
                # output. Skip render manifests so node text is unambiguous.
                if "/render/" not in parsed["s3_key"]:
                    _output_ref_key.append(parsed["s3_key"])
                return
            if "error" in parsed:
                chunks.append(json.dumps(parsed))
                return
            # Could be the actual response content (e.g., HighLevelPlan JSON)
            chunks.append(json.dumps(parsed))
            return

        # If it's a string, check if it's a step event encoded as string
        if isinstance(parsed, str):
            # The string might itself be a JSON step event (double-encoded)
            try:
                inner = json.loads(parsed)
                if isinstance(inner, dict) and inner.get("type") in ("step", "execution_summary"):
                    return  # Skip step events even when double-encoded
            except (json.JSONDecodeError, TypeError):
                pass
            chunks.append(parsed)

    if hasattr(body, "iter_lines"):
        for line in body.iter_lines():
            if isinstance(line, bytes):
                line = line.decode("utf-8")
            _process_chunk(line)
    elif hasattr(body, "read"):
        raw = body.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        _process_chunk(raw)
    else:
        chunks.append(str(body))

    text = "".join(chunks)

    # If an output_ref was emitted, read the output from S3 instead of using
    # the chunk-assembled text (which may have SSE parsing artifacts).
    # Use the last output_ref — the task agent emits its text output last.
    if _output_ref_key:
        s3_key = _output_ref_key[-1]
        try:
            s3 = boto3.client("s3", region_name=REGION)
            s3_response = s3.get_object(Bucket=SKILLS_S3_BUCKET, Key=s3_key)
            text = s3_response["Body"].read().decode("utf-8")
            logger.info("_invoke_runtime: read output from S3 (%s, %d bytes)", s3_key, len(text))
        except Exception as e:
            logger.warning("_invoke_runtime: failed to read S3 output_ref %s: %s, falling back to chunks", s3_key, e)

    # Debug: log chunk count and first/last 200 chars
    logger.info("_invoke_runtime: %d chunks, first 200: %s", len(chunks), repr(text[:200]))
    logger.info("_invoke_runtime: LAST 200 chars: %s", repr(text[-200:]))

    return text, usage


def _parse_json_from_text(text: str) -> dict | None:
    """Extract the first complete JSON object from text.

    Handles markdown fences, execution_summary objects, and other noise.
    Finds the first '{' and its matching '}' using depth counting.
    """
    if not text or not text.strip():
        return None

    # Find the first '{' in the text
    first_brace = text.find("{")
    if first_brace == -1:
        return None

    # Find the matching closing brace by counting depth, respecting strings
    depth = 0
    in_string = False
    escape_next = False

    for i in range(first_brace, len(text)):
        ch = text[i]
        if escape_next:
            escape_next = False
            continue
        if ch == '\\':
            escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                # Found the first complete JSON object
                candidate = text[first_brace:i + 1]
                try:
                    parsed = json.loads(candidate)
                    if isinstance(parsed, dict):
                        # Skip step/execution_summary events
                        if parsed.get("type") in ("step", "execution_summary"):
                            # Continue scanning for the next JSON object
                            return _parse_json_from_text(text[i + 1:])
                        return parsed
                except (json.JSONDecodeError, TypeError):
                    pass
                # If parse failed, continue scanning for next object
                return _parse_json_from_text(text[i + 1:])

    return None


# ---------------------------------------------------------------------------
# Direct dispatch (execution_mode = "direct")
# ---------------------------------------------------------------------------

def _consumes_prior_phase(goal: str, constraints: str, hlp: HighLevelPlan) -> bool:
    """True when the goal (or the plan the planner made of it) reads an earlier phase's outputs.

    Guided skills hand off between phase plans with ``external_inputs`` (a node field the planner
    writes and the Plan Chainer resolves), so a goal that names it must take the plan path.
    """
    texts = [goal or "", constraints or ""]
    texts += [sg.description or "" for sg in (hlp.sub_goals or [])]
    if hlp.direct_dispatch_params is not None:
        texts.append(hlp.direct_dispatch_params.prompt or "")
    return any("external_inputs" in t for t in texts)


def _dispatch_direct(params: dict, tool_use_id: str) -> dict:
    """Handle direct execution_mode by dispatching via the Task Agent."""
    task_arn = _task_agent_arn()
    if not task_arn:
        return _error(tool_use_id, "TASK_AGENT_RUNTIME_ARN not configured")

    prompt = params.get("prompt", "")
    tools = params.get("tools", [])
    skills = _resolve_skill_refs(params.get("skills", []))
    # Same inheritance as dispatch_tool: a plan node that names no skills runs
    # with the turn's active skills, not with none -- the planner has no reason
    # to re-serialize skill bodies any more than the orchestrator LLM did, and
    # dropping them was what produced "0 skills" dispatches while the turn used
    # one. Explicit wins, silence inherits, nothing bound sends none. The inherited
    # set is SCOPED to this node's declared tools (see _relevant_skills) so a
    # focused node does not carry the whole active catalog (run 863816b4 bloat).
    if not skills:
        try:
            from jobs.context import current_active_skills
            skills = _relevant_skills(current_active_skills() or [], tools)
        except Exception:  # noqa: BLE001 - never let inheritance break execution
            skills = []
    # Same inheritance as dispatch_tool: a plan node with no explicit model runs
    # on the turn's model, not on whatever the planner happened to name.
    model_id = params.get("model_id")
    if not model_id:
        try:
            from jobs.context import current_model_id
            model_id = current_model_id() or None
        except Exception:  # noqa: BLE001 - never let inheritance break execution
            model_id = None

    payload: dict = {"prompt": prompt}
    if tools:
        # The plan runtime needs the caller's token for the same reason the
        # dispatch path does: without it the user_jwt gateway has no credential,
        # so a user-registered MCP server's tools are absent and the agent falls
        # back to direct dispatch -- which is what it reported doing.
        # Same credential rule as dispatch_tool: a freshly minted delegation grant first (it
        # does not lapse under a long sub-agent turn), the user token only as a still-valid
        # fallback. The grant names the actor, so forward it too.
        try:
            from jobs.context import current_actor_id, current_user_token
            from orchestration.dispatch_credentials import dispatch_credentials
            _actor = current_actor_id() or ""
            if _actor:
                payload["actor_id"] = _actor
            payload.update(dispatch_credentials(_actor, current_user_token() or ""))
        except Exception:
            pass
        import web_search as _web_search
        payload["tools"] = _web_search.strip_tool_names(tools, allowed=False)
    # Knowledge-graph tools for the node: the active Project's id only (the node mints its own token).
    try:
        from project_search.context import graph_scope_project_id
        _graph_pid = graph_scope_project_id()
        if _graph_pid:
            payload["project_scope"] = {"project_id": _graph_pid}
    except Exception:  # noqa: BLE001 — the graph is optional
        pass
    if skills:
        payload["skills"] = skills
    if model_id:
        payload["model_id"] = model_id

    text, usage = _invoke_runtime(task_arn, payload)

    return {
        "toolUseId": tool_use_id,
        "status": "success",
        "content": [{"text": text}],
        "tokenUsage": usage,
    }


# ---------------------------------------------------------------------------
# Main tool handler
# ---------------------------------------------------------------------------

def _new_plan_session_id() -> str:
    """Mint the fallback session id used when no chat session is bound.

    Length is load-bearing, not cosmetic. This id becomes
    ``effective_session`` whenever ``current_session_id()`` is unbound (a plan
    started outside a UI chat turn — 4 of 12 plan-state rows in a production
    snapshot are keyed this way), and that value is what gets propagated as
    AgentCore's ``runtimeSessionId`` to the planning agent here and to
    every node's Task Agent in the plan chainer. AgentCore rejects a
    ``runtimeSessionId`` shorter than 33 chars, so both propagation paths skip it
    for a too-short id — meaning the old ``plan-session-<epoch>`` form (23 chars)
    silently disabled span correlation in exactly the sessions that had no chat
    session id to correlate to in the first place.

    The ``plan-session-`` PREFIX is a consumer-visible contract
    (``improve.observe.span_trajectory.PLAN_SESSION_PREFIX`` classifies these as
    sub-sessions of a parent run), so only the suffix grew. The added uuid also
    removes a latent collision: two plans dispatched in the same second used to
    share one plan-state partition key.
    """
    return f"plan-session-{int(time.time())}-{uuid.uuid4().hex}"


def plan_and_execute(tool, **kwargs) -> dict:
    """Strands tool handler — plan and execute a complex goal.

    Pipeline:
    1. Call Planning Agent (fused) → {HighLevelPlan, DAG}
    2. Route by execution_mode:
       - "direct" → dispatch via Task Agent, return result
       - "standard"/"complex" → use the fused DAG
    3. Validate the DAG; on a missing/invalid DAG, re-invoke the fused Planning
       call with the validation errors fed back, bounded to PLAN_DAG_MAX_ATTEMPTS
    4. Execute via the Plan Chainer (async, reactive)
    5. Return consolidated result with token usage
    """
    tool_use_id = tool["toolUseId"]
    inp = tool["input"]
    goal = inp.get("goal", "")
    constraints = inp.get("constraints", "")

    if not goal:
        return _error(tool_use_id, "goal is required")
    if not _planning_agent_arn():
        return _error(tool_use_id, "PLANNING_AGENT_RUNTIME_ARN not configured")

    cumulative_tokens = {"inputTokens": 0, "outputTokens": 0}
    progress_events: list[dict] = []
    exec_start = time.time()
    session_id = _new_plan_session_id()
    # Capture the plan-session id separately: `session_id` gets reassigned later
    # (Step 5) for the Coordinator run, but the Planning Agent writes the
    # HighLevelPlan to the Plan State Table + S3 under THIS id
    # (plans/{plan_session_id}/planning/output.json). We must surface it to the
    # caller so a user can pass it to persist_workflow(session_id=...) — without
    # it, persist_workflow fails with MissingPlanError.
    plan_session_id = session_id

    def _finalize(result: dict) -> dict:
        """Surface the plan-session id on every returned result.

        Adds a top-level ``planSessionId`` for programmatic use and appends a
        human-readable line to the first text content block so the agent
        reliably reports it to the user (who feeds it to persist_workflow).
        """
        if not isinstance(result, dict):
            return result
        result.setdefault("planSessionId", plan_session_id)
        content = result.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and "text" in block:
                    block["text"] = (
                        f"{block['text']}\n\n"
                        f"(plan session id: {plan_session_id} — pass this to persist_workflow)"
                    )
                    break
        return result

    # Build catalog from the orchestrator's live tool/skill/model registry.
    #
    # CRITICAL: an empty/missing catalog here means the Planning Agent gets
    # "No catalog provided." in its prompt and has NO real tool names to
    # ground on. It will then invent plausible-sounding tool names from
    # context (e.g. "start_separate_audio_stems" when the real tool is
    # "separate_audio_stems") — a hallucination that silently propagates
    # into a DAG with unresolvable tools. Task Agent nodes given those
    # names get zero resolved tools and (absent other guardrails) can
    # fabricate fake job_ids and report success. This was an observed
    # incident, not a hypothetical — see plan deb1cec6-c2d1-4f28-bc9e-
    # 8c61a8038daf. A bare `except Exception: catalog = {}` here made the
    # root cause invisible: whatever actually failed in get_catalog()
    # (gateway not yet connected, an attribute error, etc.) was silently
    # discarded and the plan proceeded anyway with no tool grounding.
    try:
        import main as _main_module
        catalog = _main_module.get_catalog()
        catalog = _without_web_search(catalog)
    except Exception:
        logger.exception(
            "plan_and_execute: get_catalog() raised — proceeding with an "
            "empty catalog. The Planning Agent will have NO real tool names "
            "to ground on and may hallucinate tool names."
        )
        catalog = {}

    if not catalog.get("tools"):
        logger.warning(
            "plan_and_execute: catalog has no tools (gateway_tools=%d, tools=%d) "
            "— Planning Agent will receive 'No catalog provided.' and has no real "
            "tool names to ground on. Proceeding anyway per degraded-mode policy, "
            "but any node names it invents will fail at dispatch time.",
            len(catalog.get("gateway_tools", [])), len(catalog.get("tools", [])),
        )

    # ------------------------------------------------------------------
    # Step 1: Call Planning Agent
    # ------------------------------------------------------------------
    logger.info("plan_and_execute: calling Planning Agent for goal: %s", goal[:100])
    _emit("running", "Planning Agent: analyzing goal...", agent="planning", phase="planning")
    planning_payload: dict = {"goal": goal, "catalog": catalog,
                              "session_id": session_id, "s3_bucket": SKILLS_S3_BUCKET, "ddb_table": PLAN_STATE_TABLE}
    # One call for the plan AND its DAG (planning_agent.FUSED_MODE). Fusion is unconditional: this
    # is the only way to produce a DAG. A planning runtime that predates the mode ignores it and
    # returns a bare HighLevelPlan, which parses as before and takes the self-retry path for a DAG.
    planning_payload["mode"] = "plan_and_design"
    if constraints:
        planning_payload["goal"] = f"{goal}\n\nConstraints: {constraints}"

    try:
        planning_text, planning_usage = _invoke_runtime(_planning_agent_arn(), planning_payload)
        _accumulate_tokens(cumulative_tokens, planning_usage)
    except Exception as e:
        logger.exception("Planning Agent invocation failed")
        return _error(tool_use_id, f"Planning Agent failed: {e}")

    # Parse HighLevelPlan (with one retry on invalid JSON — Fix 15)
    hlp_data = _parse_json_from_text(planning_text)
    if hlp_data is None:
        logger.warning("plan_and_execute: Planning Agent returned invalid JSON, retrying once")
        _emit("running", "Planning Agent: retrying (invalid JSON)...", agent="planning", phase="planning")
        try:
            planning_text, planning_usage = _invoke_runtime(_planning_agent_arn(), planning_payload)
            _accumulate_tokens(cumulative_tokens, planning_usage)
            hlp_data = _parse_json_from_text(planning_text)
        except Exception:
            hlp_data = None
        if hlp_data is None:
            # Deterministic fallback: construct a minimal "direct" plan from the goal
            logger.warning("plan_and_execute: retry failed; falling back to direct dispatch of goal")
            _emit("running", "Planning failed — falling back to direct dispatch", agent="planning", phase="planning")
            fallback_params = {"prompt": goal, "tools": [], "skills": []}
            result = _dispatch_direct(fallback_params, tool_use_id)
            result["executionMode"] = "direct_fallback"
            return _finalize(result)

    if "error" in hlp_data:
        return _finalize(_error(tool_use_id, f"Planning Agent error: {hlp_data['error']}"))

    # Fused response: {"high_level_plan": {...}, "dag": {...} | null}. A bare HighLevelPlan (an
    # older planning runtime, or the mode turned off) has no "high_level_plan" key.
    fused_dag = None
    if isinstance(hlp_data.get("high_level_plan"), dict):
        fused_dag = hlp_data.get("dag") if isinstance(hlp_data.get("dag"), dict) else None
        hlp_data = hlp_data["high_level_plan"]
        if "error" in hlp_data:
            return _finalize(_error(tool_use_id, f"Planning Agent error: {hlp_data['error']}"))

    try:
        high_level_plan = HighLevelPlan.from_dict(hlp_data)
    except (KeyError, TypeError) as e:
        return _finalize(_error(tool_use_id, f"Failed to parse HighLevelPlan: {e}"))

    execution_mode = high_level_plan.execution_mode
    logger.info("plan_and_execute: execution_mode=%s, sub_goals=%d", execution_mode, len(high_level_plan.sub_goals))
    _emit("done", f"Planning Agent: {execution_mode} mode, {len(high_level_plan.sub_goals)} sub-goal(s)", agent="planning", phase="planning")

    # Share the HighLevelPlan with the UI for review
    _emit("plan_ready", "High-level plan ready for review", agent="planning", phase="planning",
          plan_type="high_level_plan", plan_data=high_level_plan.to_dict(),
          gate_id="gate_high_level_plan")

    # NOTE: Gating is disabled — AgentCore runtimes are single-request, so the
    # resolve_gate API call can't reach the in-flight request. Plans are shown
    # for informational purposes. Gating will be re-enabled with the DDB-based
    # state management approach (separate polling endpoint).

    # ------------------------------------------------------------------
    # Step 2: Route by execution_mode
    # ------------------------------------------------------------------
    if execution_mode == "direct" and _consumes_prior_phase(goal, constraints, high_level_plan):
        # Direct dispatch skips the Design Agent AND the Plan Chainer, and only the chainer resolves
        # `external_inputs`. Staging session d6a560a8: three single-node Phase 3 report plans went
        # direct, no prior-phase uris were injected, and each report guessed keys (AccessDenied) and
        # invented findings. A goal that consumes an earlier phase is a plan, even with one node.
        logger.info("plan_and_execute: goal references external_inputs; routing direct -> standard")
        _emit("running", "Prior-phase inputs referenced: running as a plan so they resolve",
              agent="planning", phase="planning")
        execution_mode = "standard"
        high_level_plan.execution_mode = "standard"
        if not high_level_plan.sub_goals:
            ddp = high_level_plan.direct_dispatch_params
            high_level_plan.sub_goals = [SubGoal(
                id="sg1", description=(ddp.prompt if ddp else "") or goal, reasoning="single step",
                suggested_tools=list(ddp.tools) if ddp else [],
                # The planner's chosen skills travel with the step (inheritance alone would hide a
                # dropped skill only while it happens to be active and ungated).
                suggested_skills=[n for n in (_skill_name(s) for s in (ddp.skills or [])) if n] if ddp else [],
                depends_on=[])]

    if execution_mode == "direct":
        # Direct dispatch — skip the DAG and Coordinator
        if high_level_plan.direct_dispatch_params is None:
            return _finalize(_error(tool_use_id, "execution_mode is 'direct' but direct_dispatch_params is missing"))

        _emit("running", "Direct dispatch: executing task...", agent="task:direct", phase="direct")
        result = _dispatch_direct(high_level_plan.direct_dispatch_params.to_dict(), tool_use_id)
        _emit("done", "Direct dispatch: complete", agent="task:direct", phase="direct")
        _accumulate_tokens(cumulative_tokens, result.get("tokenUsage", {}))
        result["tokenUsage"] = cumulative_tokens
        result["executionMode"] = "direct"
        return _finalize(result)

    # standard / complex → fused DAG → Coordinator
    if execution_mode not in ("standard", "complex"):
        return _finalize(_error(tool_use_id, f"Unknown execution_mode: {execution_mode}"))

    # ------------------------------------------------------------------
    # Step 3 + 4: Build and validate the DAG from the fused Planning response.
    #
    # Fusion is unconditional, so the Planning call is the ONLY source of a DAG. When the fused
    # DAG is missing, unparseable, or fails validation, the fused Planning call is RE-INVOKED with
    # the validation errors fed back for correction (the Planning Agent appends them to its prompt,
    # exactly as the retired Design Agent did), bounded to PLAN_DAG_MAX_ATTEMPTS. If no attempt
    # produces a valid DAG, fail cleanly naming the validation problem.
    # ------------------------------------------------------------------
    available_tools = _extract_available_tools(catalog)
    logger.info("plan_and_execute: available_tools for validation (%d): %s",
                len(available_tools), sorted(available_tools)[:30])
    plan, validation_error = _plan_with_self_retry(
        fused_dag=fused_dag,
        planning_payload=planning_payload,
        available_tools=available_tools,
        cumulative_tokens=cumulative_tokens,
    )

    if plan is None:
        _emit("failed", f"Plan validation failed: {(validation_error or '')[:200]}",
              agent="planning", phase="design")
        return _finalize(_error(tool_use_id, validation_error or "Plan validation failed after retry"))

    logger.info("plan_and_execute: DAG node tools: %s", {n.id: n.tools for n in plan.nodes})
    _emit("done", "Plan validated", agent="coordinator", phase="validation")

    # A node that declares no skills inherits the session's active skills.
    #
    # Direct dispatch already does exactly this a few hundred lines up ("if not skills:
    # skills = current_active_skills()"); DAG nodes did not, and the planner emitted
    # skills=[] for all three nodes of a video+music+mux plan. The mux node therefore had
    # no media skill, did not know fal's merge-audio-video endpoint existed, and followed a
    # prompt that named an M2C tool which takes a single asset. It refused, correctly, and
    # the plan reported SUCCEEDED with no merged file.
    #
    # Same rule as direct dispatch, so the two paths cannot drift again. Only fills the gap
    # -- a node that DID name skills keeps exactly those. The inherited set is SCOPED to each
    # node's declared tools (see _relevant_skills) so a focused node (e.g. a fal submit) does
    # not carry all 11 active skills -- that bloat is what buried media-references' "no
    # fal-ai/ prefix" rule on run 863816b4.
    try:
        from jobs.context import current_active_skills

        _active = current_active_skills() or []
    except Exception:  # noqa: BLE001 - never fail a plan over skill lookup
        _active = []
    if _active:
        _inherited = [n.id for n in plan.nodes if not n.skills]
        for _n in plan.nodes:
            if not _n.skills:
                _scoped = _relevant_skills(_active, _n.tools)
                # NAMES only. The plan-node executor resolves a skill by name from the bundle
                # (task_agent._execute_plan_node), so a body here is never read — but active skills
                # carry their whole instructions (references bundled, up to ~24 KB each), and
                # copying them onto every node made plan 81d9198e's 8-node nodes_json 1.73 MB:
                # offloaded to S3, re-read and re-parsed on every chainer pass, and written again
                # into the manifest.
                _n.skills = [{"name": _skill_name(s)} for s in _scoped if _skill_name(s)]
        if _inherited:
            logger.info("Plan %s: nodes %s inherited active skills (scoped per node's tools) from %s",
                        plan.plan_id, _inherited,
                        [s if isinstance(s, str) else s.get("name") for s in _active])

    # Share the execution DAG with the UI for review before execution
    dag_summary = {
        "plan_id": plan.plan_id,
        "goal": plan.goal,
        "nodes": [
            {
                "id": n.id,
                "prompt": n.prompt[:200] + ("..." if len(n.prompt) > 200 else ""),
                "tools": n.tools,
                "skills": [s if isinstance(s, str) else s.get("name", str(s)) for s in n.skills],
                "depends_on": n.depends_on,
                "output_key": n.output_key,
                "output_type": n.output_type,
            }
            for n in plan.nodes
        ],
    }
    _emit("plan_ready", "Execution DAG ready for review", agent="planning", phase="validation",
          plan_type="execution_dag", plan_data=dag_summary,
          gate_id="gate_execution_dag")

    # NOTE: Gating disabled — see comment above at gate_high_level_plan.

    # ------------------------------------------------------------------
    # Step 5: Execute via the Plan Chainer (async, reactive — sync path removed)
    # ------------------------------------------------------------------

    # Execute via the reactive Pattern-D path (Plan Chainer). Async is the
    # ONLY plan-execution mode: it keeps chat responsive for long media DAGs
    # and gives live tracking in the Workflow view. The old inline sync path
    # was removed to cut complexity (single execution path, no toggle).
    return _dispatch_plan_async(
        tool_use_id=tool_use_id,
        plan=plan,
        execution_mode=execution_mode,
        cumulative_tokens=cumulative_tokens,
        session_id=session_id,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _dispatch_plan_async(
    *,
    tool_use_id: str,
    plan: Plan,
    execution_mode: str,
    cumulative_tokens: dict,
    session_id: str,
) -> dict:
    """Dispatch plan for reactive execution via Pattern D.

    Writes the plan + node records to the Plan State table and Job Registry.
    The Plan Chainer Lambda (triggered by DDB Stream) detects the insertion,
    finds the first READY node(s), and dispatches them to the Task Agent.
    Each subsequent node is dispatched reactively as prior nodes complete.

    Returns immediately — no background threads, no blocking.
    """
    import uuid

    # The planner generates a readable plan name (e.g. "plan-stemtranslate01").
    # We use a UUID as the actual system key to guarantee uniqueness across dispatches.
    # The planner's name becomes the display_name for the Activity Panel.
    display_name = plan.plan_id
    plan_id = str(uuid.uuid4())

    logger.info(
        "plan_and_execute [ASYNC/PatternD]: writing plan %s (%s, %d nodes) to state table",
        plan_id[:8], display_name, len(plan.nodes),
    )

    _emit("running", f"Dispatching plan {display_name} for async execution ({len(plan.nodes)} nodes)...",
          agent="coordinator", phase="async_dispatch")

    # Resolve actor_id and chat session_id from context
    from jobs import context as jobs_context
    actor_id = ""
    chat_session_id = ""
    turn_id = ""
    model_id = ""
    try:
        actor_id = jobs_context.current_actor_id()
    except Exception:
        pass
    try:
        chat_session_id = jobs_context.current_session_id()
    except Exception:
        pass
    try:
        turn_id = jobs_context.current_turn_id() or ""
    except Exception:
        turn_id = ""
    # The orchestrator's user-selected model. Persisted on the PLAN row so the async
    # chainer can forward it onto each node's dispatch, keeping plan-node execution in
    # sync with the model the user picked (the task agent honors payload.model_id).
    try:
        model_id = jobs_context.current_model_id() or ""
    except Exception:
        model_id = ""

    effective_session = chat_session_id or session_id

    logger.info(
        "plan_and_execute [ASYNC/PatternD]: actor_id=%s, session_id=%s, plan_id=%s",
        actor_id[:20] if actor_id else "(empty)",
        effective_session[:20],
        plan_id[:8],
    )

    # ------------------------------------------------------------------
    # Option C (fail loudly) + option E (signed delegation grant).
    #
    # A plan node runs asynchronously in the plan-chainer → Task Agent, where
    # the user's JWT is gone. A node that needs a user_jwt-gated gateway tool
    # (a ``usr-`` target) therefore has no verified identity at the harness
    # gateway and cannot run. Option E mints a signed delegation grant here —
    # while this request still holds the actor — so the interceptor can derive
    # the actor from the grant instead. When a grant cannot be minted (feature
    # not enabled, no signing key, or no actor), option C rejects the plan with
    # an actionable message rather than writing nodes that would silently never
    # run and forever report "running in background".
    # ------------------------------------------------------------------
    delegation_grant = ""
    # Resolve which referenced tools live on a user_jwt-auth gateway from the
    # live catalog (auth_type per gateway tool). This catches user_jwt targets
    # that are NOT usr- prefixed (e.g. the aws-mcp documentation target on the
    # harness gateway), which the name-only fast-path cannot see. Falls back to
    # the usr- prefix alone if the catalog is unavailable (e.g. the user_jwt
    # gateway is not connected in this request).
    user_jwt_tool_names: "set[str]" = set()
    try:
        import main as _main_module
        _cat = _main_module.get_catalog()
        user_jwt_tool_names = {
            t["name"]
            for t in _cat.get("gateway_tools", [])
            if t.get("auth_type") == "user_jwt" and t.get("name")
        }
    except Exception:
        logger.exception(
            "plan_and_execute: could not resolve user_jwt tool set from catalog; "
            "falling back to usr- prefix detection only"
        )
    gated_tools = _user_gated_tools_in_plan(plan, user_jwt_tool_names)
    if gated_tools:
        from security import delegation as _delegation

        if _delegation.can_delegate(actor_id):
            try:
                delegation_grant = _delegation.mint_grant(actor_id, plan_id)
            except Exception:
                logger.exception("plan_and_execute: delegation grant minting failed")
                delegation_grant = ""

        if not delegation_grant:
            if not actor_id:
                why = "this request carries no actor identity"
            elif not _delegation.grants_enabled():
                why = ("delegated (background) execution of user-registered tools "
                       "is not enabled in this environment")
            elif not _delegation.signing_key_arn():
                why = "no delegation signing key is configured"
            else:
                why = "the delegation grant could not be minted"
            tool_list = ", ".join(gated_tools)
            logger.warning(
                "plan_and_execute: rejecting plan (option C) — %d user_jwt-gated "
                "tool(s) [%s], %s", len(gated_tools), tool_list, why,
            )
            _emit("failed",
                  "Plan rejected: needs user-registered tools that can't run in the background",
                  agent="coordinator", phase="async_dispatch")
            return _error(
                tool_use_id,
                (
                    "This plan can't run as a background task because one or more "
                    f"steps need user-registered gateway tool(s) [{tool_list}] that "
                    "require your authenticated session (user_jwt-gated), and "
                    f"{why}. Background plan nodes execute later, out of your "
                    "session, so they cannot present your credentials.\n\n"
                    "Run this request directly (e.g. via a single dispatch, which "
                    "executes in your current session) instead of as a multi-step "
                    "plan, or retry once delegated background execution is enabled."
                ),
            )

    # Knowledge graph: a node mints its Project_Token from the plan's delegation grant, so a plan in a
    # graph-enabled Project needs a grant even when no step uses a user_jwt-gated tool. Best effort — unlike the
    # gated case above, no grant only means the nodes run without the graph tools.
    if not delegation_grant:
        try:
            from project_search.context import graph_scope_project_id

            if graph_scope_project_id():
                from security import delegation as _delegation

                if _delegation.can_delegate(actor_id):
                    delegation_grant = _delegation.mint_grant(actor_id, plan_id)
        except Exception:  # noqa: BLE001
            logger.warning("plan_and_execute: no delegation grant for the graph tools; nodes run without them")
            delegation_grant = ""

    # Plan nodes never get Web Search in this release (the chainer does not forward the turn's toggle), so a
    # node can never search from a turn whose chat has it off. The planner's catalog drops it too.
    import web_search as _web_search
    for _n in plan.nodes:
        _n.tools = _web_search.strip_tool_names(list(_n.tools or []), allowed=False)

    # Write plan to State Store (the INSERT triggers DDB Stream → Plan Chainer)
    if PLAN_STATE_TABLE:
        try:
            from state_store import StateStore, node_sk, plan_sk
            import time as _time
            from datetime import datetime, timezone
            state_store = StateStore(table_name=PLAN_STATE_TABLE, region=REGION)
            now = datetime.now(timezone.utc).isoformat()
            ttl = int(_time.time()) + 86400

            # Write PLAN record with actor_id so chainer can pass it to Task Agent.
            # Keyed by plan id ("PLAN#<plan_id>", nodes "<plan_id>#<node_id>"): the literal
            # "PLAN" let a second plan in the session overwrite the first, and repeated node
            # ids (sg1..sg5) overwrote each other's rows (session 7b74d118).
            plan_item = {
                "session_id": effective_session,
                "node_id": plan_sk(plan_id),
                "plan_id": plan_id,
                "node": "PLAN",
                "display_name": display_name,
                "goal": plan.goal,
                "nodes_json": json.dumps([n.to_dict() for n in plan.nodes]),
                "status": "pending",
                "actor_id": actor_id,
                # Plan nodes are dispatched asynchronously by the chainer, long
                # after this invocation's contextvars are gone, so the turn that
                # asked for the plan has to be persisted here to survive.
                "turn_id": turn_id,
                "updated_at": now,
                "ttl": ttl,
            }
            # Orchestrator's picked model, forwarded by the chainer onto node dispatches so
            # plan nodes run on the model the user selected (empty → node uses its default).
            if model_id:
                plan_item["model_id"] = model_id
            # The TRUSTED producing project, on the row that fires the chainer, so the very first
            # dispatch already has it. The job-registry plan row below carries it too but is written
            # AFTER these rows; read from there alone, plan 73d92d46's nodes ran with no project.
            try:
                _plan_pid = jobs_context.trusted_project_id()
            except Exception:  # noqa: BLE001 — no project: the legacy layout
                _plan_pid = None
            if _plan_pid:
                plan_item["producing_project_id"] = _plan_pid
            # Signed delegation grant (option E), persisted beside actor_id /
            # turn_id as a capability — NOT a credential to any upstream and NOT
            # the user's JWT. It authorises exactly this actor's tools for
            # exactly this plan until it expires. Only present when the plan
            # needs a user_jwt-gated tool (otherwise no grant was minted).
            if delegation_grant:
                plan_item["delegation_grant"] = delegation_grant
                # Knowledge-graph tools on the nodes: the Project id the chainer forwards as `project_scope`.
                # Only with a grant — a node mints its Project_Token FROM the grant, so without one it could not.
                try:
                    from project_search.context import graph_scope_project_id
                    _graph_pid = graph_scope_project_id()
                    if _graph_pid:
                        plan_item["project_scope_id"] = _graph_pid
                except Exception:  # noqa: BLE001
                    pass
            # Offload the node list to S3 when it would breach DynamoDB's 400KB item cap (a wide
            # fan-out plan). The chainer's _load_nodes_json reads the pointer; small plans stay
            # inline (backward-compatible). Threshold mirrors the chainer's NODES_JSON_INLINE_MAX.
            _nodes_serialized = plan_item["nodes_json"]
            if len(_nodes_serialized.encode("utf-8")) > 300_000 and SKILLS_S3_BUCKET:
                _nodes_key = f"plans/{effective_session}/{plan_id}/nodes.json"
                try:
                    boto3.client("s3", region_name=REGION).put_object(
                        Bucket=SKILLS_S3_BUCKET, Key=_nodes_key,
                        Body=_nodes_serialized.encode("utf-8"), ContentType="application/json")
                    plan_item["nodes_s3_key"] = _nodes_key
                    plan_item["nodes_json"] = ""
                    logger.info("Plan %s: offloaded %d-byte nodes_json to s3://%s/%s",
                                plan_id[:8], len(_nodes_serialized), SKILLS_S3_BUCKET, _nodes_key)
                except Exception as e:  # noqa: BLE001 — inline write still succeeds below the cap
                    logger.warning("Plan %s: nodes_json offload failed (%s); writing inline",
                                   plan_id[:8], e)
            state_store._table.put_item(Item=plan_item)

            # Write all node records as PENDING
            for node in plan.nodes:
                state_store._table.put_item(Item={
                    "session_id": effective_session,
                    "node_id": node_sk(plan_id, node.id),
                    "plan_id": plan_id,
                    "node": node.id,
                    "status": "pending",
                    "depends_on": json.dumps(node.depends_on),
                    "tools": json.dumps(node.tools),
                    "prompt": node.prompt,
                    "output_type": node.output_type,
                    "updated_at": now,
                    "ttl": ttl,
                })
            logger.info("Plan %s (%s) written to state table (%d nodes)", plan_id[:8], display_name, len(plan.nodes))
        except Exception as exc:
            logger.exception("Failed to write plan to state table")
            return _error(tool_use_id, f"Failed to write plan to state table: {exc}")
    else:
        return _error(tool_use_id, "PLAN_STATE_TABLE not configured")

    # Create the plan manifest (structured output registry)
    try:
        from planning.plan_manifest import PlanManifest
        manifest = PlanManifest(
            bucket=SKILLS_S3_BUCKET, session_id=effective_session,
            plan_id=plan_id, region=REGION,
        )
        manifest.create(
            display_name=display_name,
            goal=plan.goal,
            nodes=[n.to_dict() for n in plan.nodes],
        )
        logger.info("Plan manifest created at %s", manifest.s3_uri)
    except Exception as exc:
        logger.warning("Failed to create plan manifest: %s", exc)

    # Register plan + nodes in Job Registry (for Activity panel)
    if JOB_REGISTRY_TABLE and actor_id:
        try:
            from jobs.registry import JobRegistry
            from jobs.models import JobRecord
            from datetime import datetime, timezone
            registry = JobRegistry(table_name=JOB_REGISTRY_TABLE, region=REGION)
            now_iso = datetime.now(timezone.utc).isoformat()

            registry.register(JobRecord(
                job_id=plan_id,
                actor_id=actor_id,
                session_id=effective_session,
                trio_id=f"plan|{display_name}",
                gateway_id="__coordinator__",
                status_tool="", results_tool="",
                status="RUNNING",
                start_at=now_iso, updated_at=now_iso,
                run_id=plan_id,
                # Media the plan produces registers into the dispatching turn's Project (ingestion Req 9.3).
                # TRUSTED id only: the chainer forwards it to every node, which writes under it and gets
                # sandbox credentials for it (asset-lineage Phase 3 (a)).
                producing_project_id=jobs_context.trusted_project_id(),
            ))
            for node in plan.nodes:
                registry.register(JobRecord(
                    job_id=f"{plan_id}#{node.id}",
                    actor_id=actor_id,
                    session_id=effective_session,
                    trio_id=f"node|{node.id}",
                    gateway_id="__coordinator__",
                    status_tool="", results_tool="",
                    status="PENDING",
                    start_at=now_iso, updated_at=now_iso,
                    run_id=plan_id,
                    node_id=node.id,
                    depends_on=list(node.depends_on),
                ))

            # Remove pollPartition from plan/node entries so the polling worker
            # ignores them — their lifecycle is managed by the Plan Chainer, not polling.
            import boto3 as _boto3
            _jr_table = _boto3.resource("dynamodb", region_name=REGION).Table(JOB_REGISTRY_TABLE)
            _jr_table.update_item(
                Key={"jobId": plan_id},
                UpdateExpression="REMOVE pollPartition",
            )
            for node in plan.nodes:
                _jr_table.update_item(
                    Key={"jobId": f"{plan_id}#{node.id}"},
                    UpdateExpression="REMOVE pollPartition",
                )
        except Exception as exc:
            logger.warning("Failed to register plan in job registry: %s", exc)

    _emit("done", f"Plan {display_name} dispatched — track progress in Activity",
          agent="coordinator", phase="async_dispatch",
          plan_id=plan_id, total_nodes=len(plan.nodes))

    # Build a user-friendly summary of what was dispatched
    node_names = [n.id for n in plan.nodes]
    content_text = (
        f"[PLAN DISPATCHED ASYNC] Plan `{display_name}` with {len(plan.nodes)} nodes "
        f"is now executing in the background.\n\n"
        f"**Nodes:** {', '.join(node_names)}\n\n"
        f"Track progress in the **Activity** panel. I'll continue to be available "
        f"for other questions while the plan runs."
    )

    return {
        "toolUseId": tool_use_id,
        "status": "success",
        "content": [{"text": content_text}],
        "executionSummary": {
            "plan_id": plan_id,
            "display_name": display_name,
            "plan_status": "dispatched_async",
            "execution_mode": execution_mode,
            "total_nodes": len(plan.nodes),
            "completed_nodes": 0,
            "failed_nodes": 0,
            "skipped_nodes": 0,
            "duration_ms": 0,
            "token_usage": cumulative_tokens,
            "async": True,
        },
    }


def _fused_dag_from_text(text: str) -> dict | None:
    """Extract the ``dag`` object from a fused ``{high_level_plan, dag}`` Planning response.

    Returns the DAG dict, or ``None`` when the response is not a fused object or carries no DAG
    (e.g. an older planning runtime that returned a bare HighLevelPlan).
    """
    data = _parse_json_from_text(text)
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("high_level_plan"), dict):
        dag = data.get("dag")
        return dag if isinstance(dag, dict) else None
    return None


def _plan_with_self_retry(
    *,
    fused_dag: dict | None,
    planning_payload: dict,
    available_tools: set[str],
    cumulative_tokens: dict,
) -> tuple[Plan | None, str | None]:
    """Build a validated Plan from the fused DAG, re-invoking the fused Planning call on failure.

    Fusion is unconditional, so the Planning Agent is the ONLY producer of a DAG — there is no
    Design Agent fallback. Attempt 1 uses the DAG the Planning call already returned. If it is
    missing, unparseable, or fails validation, the SAME fused Planning call is re-invoked with the
    validation errors fed back (``payload["validation_errors"]`` — the Planning Agent appends them
    to its prompt so the retry differs from attempt 1), up to ``PLAN_DAG_MAX_ATTEMPTS`` attempts.

    Each candidate is normalised (``normalize_async_job_nodes`` — the catalog-derivable async-trio
    repair) before validation, exactly as a designed plan was. Returns ``(plan, None)`` on success
    or ``(None, error_message)`` naming the validation problem when every attempt is exhausted.
    """
    current_dag = fused_dag
    last_error: str | None = None

    for attempt in range(1, PLAN_DAG_MAX_ATTEMPTS + 1):
        errors_text: str
        if current_dag is None:
            last_error = "the Planning Agent returned no DAG"
            errors_text = last_error
        else:
            plan, parse_error = _plan_from_data(current_dag)
            if plan is None:
                last_error = parse_error or "the DAG could not be parsed"
                errors_text = last_error
            else:
                repairs = normalize_async_job_nodes(plan, available_tools)
                if repairs:
                    logger.info("plan_and_execute: auto-repaired async job node(s): %s", "; ".join(repairs))
                errors = validate_plan(plan, available_tools)
                if not errors:
                    logger.info("plan_and_execute: fused DAG valid on attempt %d (%d nodes)",
                                attempt, len(plan.nodes))
                    _emit("done",
                          f"Planning Agent: DAG with {len(plan.nodes)} node(s) validated",
                          agent="planning", phase="design")
                    return plan, None
                last_error = "; ".join(e.message for e in errors)
                errors_text = "\n".join(f"- {e.error_type}: {e.message}" for e in errors)

        logger.warning("plan_and_execute: DAG attempt %d invalid: %s", attempt, last_error)

        if attempt >= PLAN_DAG_MAX_ATTEMPTS:
            break

        # Re-invoke the fused Planning call, feeding the validation errors back for correction.
        _emit("running",
              f"Planning Agent: correcting the execution DAG (attempt {attempt + 1})...",
              agent="planning", phase="design")
        retry_payload = {**planning_payload, "validation_errors": errors_text}
        try:
            retry_text, retry_usage = _invoke_runtime(_planning_agent_arn(), retry_payload)
            _accumulate_tokens(cumulative_tokens, retry_usage)
        except Exception as e:  # noqa: BLE001 — surface as a clean failure, not a stack trace
            logger.exception("Planning Agent self-retry invocation failed")
            return None, f"Planning Agent retry failed: {e}"
        current_dag = _fused_dag_from_text(retry_text)

    return None, f"Plan validation failed after {PLAN_DAG_MAX_ATTEMPTS} attempt(s): {last_error}"


def _plan_from_data(plan_data: dict) -> tuple[Plan | None, str | None]:
    """A Plan from DAG JSON (designed or fused): compact for_each nodes expanded first.

    Expansion happens HERE, before validation, so everything downstream — the validator, the
    plan-state rows, the chainer's nodes_json — sees only ordinary nodes.
    """
    if not isinstance(plan_data, dict):
        return None, "DAG is not a JSON object"
    if "error" in plan_data:
        return None, f"DAG error: {plan_data['error']}"
    try:
        nodes = expand_for_each(plan_data.get("nodes") or [])
    except ForEachError as e:
        return None, f"Invalid for_each node: {e}"
    try:
        plan = Plan.from_dict({**plan_data, "nodes": nodes})
    except (KeyError, TypeError, AttributeError) as e:
        return None, f"Failed to parse Plan: {e}"
    notes = normalize_tool_nodes(plan)
    if notes:
        logger.info("plan_and_execute: tool-node repairs: %s", "; ".join(notes))
    return plan, None


def _without_web_search(catalog: dict) -> dict:
    """The catalog the planner sees, minus the Web Search tool (plan nodes never search in this release)."""
    import web_search as _web_search
    if not isinstance(catalog, dict):
        return catalog
    out = dict(catalog)
    for key in ("tools", "gateway_tools"):
        items = out.get(key)
        if isinstance(items, list):
            out[key] = [t for t in items if not _web_search.is_web_search_tool(
                t if isinstance(t, str) else (t.get("name") if isinstance(t, dict) else ""))]
    return out


def _extract_available_tools(catalog: dict) -> set[str]:
    """Extract tool names from the catalog for validation."""
    tools: set[str] = set()
    for t in catalog.get("tools", []):
        if isinstance(t, str):
            tools.add(t)
        elif isinstance(t, dict):
            name = t.get("name")
            if name:
                tools.add(name)
    return tools


def _accumulate_tokens(cumulative: dict, new: dict) -> None:
    """Add new token counts to cumulative totals."""
    cumulative["inputTokens"] = cumulative.get("inputTokens", 0) + new.get("inputTokens", 0)
    cumulative["outputTokens"] = cumulative.get("outputTokens", 0) + new.get("outputTokens", 0)


# ---------------------------------------------------------------------------
# user_jwt-gated tool detection (option C / option E gate)
# ---------------------------------------------------------------------------

# Must match owner_naming.USER_TARGET_PREFIX (interceptor) and user_mcp._TARGET_PREFIX:
# a user-registered MCP server's gateway target is named ``usr-<...>`` and its
# tools are published as ``usr-<...>___<tool>``. These are exactly the tools the
# gateway interceptor gates per-actor and that therefore need the caller's
# verified identity — which an async plan node does not carry.
_USER_TARGET_PREFIX = "usr-"
_TARGET_TOOL_SEPARATOR = "___"


def _tool_is_user_gated(tool_name: str) -> bool:
    """Whether a tool name belongs to a user-registered (``usr-``) gateway target.

    Splits on the FIRST separator only: a bare tool name can itself contain
    ``___`` (e.g. ``usr-abc___aws___call_aws``), and the target is the first
    segment. Splitting on every separator has caused a bug three times.
    """
    if not isinstance(tool_name, str) or _TARGET_TOOL_SEPARATOR not in tool_name:
        return False
    target = tool_name.split(_TARGET_TOOL_SEPARATOR, 1)[0]
    return target.startswith(_USER_TARGET_PREFIX)


def _user_gated_tools_in_plan(plan, user_jwt_tool_names: "set[str] | None" = None) -> list[str]:
    """Distinct user_jwt-gated tool names referenced by any node in the plan.

    A tool is user_jwt-gated if EITHER:
      - its target name has the ``usr-`` prefix — a user-registered MCP server
        (the catalog-free fast-path, see ``_tool_is_user_gated``); or
      - it is published by a gateway whose ``auth_type`` is ``user_jwt``
        (``user_jwt_tool_names``, resolved from the live catalog).

    The second arm catches PLATFORM targets on a user_jwt gateway whose target
    name is NOT ``usr-`` prefixed — e.g. the aws-mcp documentation target on the
    harness gateway (``mediaworks-harness-aws-mcp___aws___...``). Those still
    need the caller's verified identity, so an async plan node referencing them
    needs a delegation grant; without this arm no grant was minted and the node
    failed at resolution (only machine-auth gateways were connected).
    """
    gated_set = user_jwt_tool_names or set()
    found: set[str] = set()
    for node in plan.nodes:
        for tool_name in (node.tools or []):
            if _tool_is_user_gated(tool_name) or tool_name in gated_set:
                found.add(tool_name)
    return sorted(found)


def _error(tool_use_id: str, message: str) -> dict:
    """Build a standard error response."""
    logger.error("plan_and_execute error: %s", message)
    return {
        "toolUseId": tool_use_id,
        "status": "error",
        "content": [{"text": message}],
    }


# Alias: Strands expects module-based tools to have a function matching the module name
plan_and_execute_tool = plan_and_execute
