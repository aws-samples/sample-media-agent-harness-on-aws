"""Strands tool for dispatching tasks to the sub-agent runtime.

Reads the sub-agent's streaming response incrementally for faster results.

Progress streaming
------------------
The sub-agent (``task_agent.py``) yields a mix of:
  * step events  — JSON `{"type":"step","status":...,"label":...}` lines that
    track its tool-use lifecycle. We forward these to the orchestrator's
    progress channel so the user sees what the sub-agent is doing instead
    of staring at a stream of generic "Working..." keepalives while a
    long gateway call (e.g. speech diarization) is in flight.
  * output events — `{"type":"output_ref",...}` (S3 write) which we forward
    as an info-level step so the user knows artifacts have landed.
  * raw text     — the final response payload, accumulated and returned as
    the tool result so Strands can hand it back to the orchestrator.
"""
import json, logging, os, time
import boto3

import runtime_arns
from orchestration.progress_channel import get_active_channel
from jobs.context import invoke_agent_runtime_traced as _invoke_traced
from project_search.scrub import payload_summary

logger = logging.getLogger(__name__)

TOOL_SPEC = {
    "name": "dispatch_tool",
    "description": (
        "THE DEFAULT for tool work. Dispatch a task to a sub-agent with specific "
        "tools, skills, and model. The sub-agent connects to the gateway, loads only "
        "the requested tools, executes the task, and returns the result.\n"
        "Use this whenever ONE sub-agent turn can finish the job -- including when "
        "several tools are involved, as long as the sub-agent can work out the order "
        "itself. Generating one image or video, one documentation lookup, probing one "
        "media file, running one model: all of these are THIS tool, not a plan. "
        "Wrapping a single generation in a plan adds a planning round trip and a "
        "coordinator for no benefit.\n"
        "Reach for plan_and_execute_tool only when a later step needs an EARLIER "
        "STEP'S OUTPUT as its input and the dependency has to be laid out up front."
    ),
    "inputSchema": {
        "json": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "The task prompt for the sub-agent."},
                "tools": {"type": "array", "items": {"type": "string"}, "description": "Tool names to load from gateway."},
                "skills": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "description": {"type": "string"}, "instructions": {"type": "string"}}}, "description": "Skill definitions."},
                "model_id": {"type": "string", "description": (
                    "OMIT THIS unless the user asked for a specific model. Left "
                    "unset, the sub-agent inherits the model the user selected for "
                    "this session, which is almost always what they want. Set it "
                    "only to honour an explicit request, or to pick a cheaper model "
                    "for a deliberately trivial subtask."
                )},
                "system_prompt": {"type": "string", "description": "Custom system prompt."},
                "target": {"type": "string", "enum": ["task", "render"], "description": "Which sub-agent runtime to dispatch to: 'task' (default, general task agent) or 'render' (the Render Agent that turns artifact URLs into an interactive viewer). Prefer this over runtime_arn."},
                "runtime_arn": {"type": "string", "description": "Advanced: explicit AgentCore runtime ARN. Normally leave unset and use `target` instead. Defaults to the task agent."},
            },
            "required": ["prompt"],
        },
    },
}

_client = None
# Import-time snapshot (tests patch these). The deploy no longer puts the ARNs in runtime env —
# they are SSM parameters — so every use goes through task_agent_arn()/render_agent_arn(), which
# fall back to runtime_arns.get() at CALL time when the snapshot is empty.
TASK_AGENT_ARN = os.environ.get("TASK_AGENT_RUNTIME_ARN", "")
RENDER_AGENT_ARN = os.environ.get("RENDER_AGENT_RUNTIME_ARN", "")


def task_agent_arn() -> str:
    return TASK_AGENT_ARN or runtime_arns.get("TASK_AGENT_RUNTIME_ARN")


def render_agent_arn() -> str:
    return RENDER_AGENT_ARN or runtime_arns.get("RENDER_AGENT_RUNTIME_ARN")


def _get_client():
    global _client
    if not _client:
        from botocore.config import Config as BotoConfig
        _client = boto3.client(
            "bedrock-agentcore",
            region_name=os.environ.get("AWS_REGION", "us-west-2"),
            config=BotoConfig(read_timeout=600, connect_timeout=10, retries={"max_attempts": 3}),
        )
    return _client


def dispatch_tool(tool, **kwargs):
    """Strands tool handler — invokes task agent and reads streaming response."""
    tool_use_id = tool["toolUseId"]
    inp = tool["input"]
    prompt = inp.get("prompt", "")
    tools_list = inp.get("tools", [])
    skills_list = inp.get("skills", [])
    # Inherit the turn's active skills when the caller names none. Skills reached
    # the orchestrator's per-request skills plugin, but a sub-agent's skills used
    # to arrive only as this LLM-filled argument the model left empty -- observed
    # as "Dispatching to task-agent: 1 tools, 0 skills" while the same turn
    # narrated using the aws-research skill. Explicit skills still win; silence
    # now inherits the turn's skills instead of dropping them; nothing bound
    # sends none. Mirrors the model_id inheritance directly below.
    if not skills_list:
        try:
            from jobs.context import current_active_skills
            skills_list = current_active_skills() or []
        except Exception:  # noqa: BLE001 - never let inheritance break dispatch
            skills_list = []
    # Inherit the turn's model when the caller does not name one. The LLM sees a
    # model catalogue in its prompt and was free-choosing from it, so a session
    # set to Sonnet 5 dispatched its actual work to claude-sonnet-4-6. An
    # explicit model_id still wins -- naming a cheap model for a trivial subtask
    # is legitimate -- but silence now means "same model as this turn" rather
    # than "whatever the model feels like".
    model_id = inp.get("model_id")
    if not model_id:
        try:
            from jobs.context import current_model_id
            model_id = current_model_id() or None
        except Exception:  # noqa: BLE001 - never let inheritance break dispatch
            model_id = None
    system_prompt = inp.get("system_prompt")

    # Resolve target runtime ARN. Prefer the `target` enum (the LLM can't
    # read env vars, so it must NOT be asked to pass a raw ARN — doing so
    # produced the literal string "${RENDER_AGENT_RUNTIME_ARN}"). Order:
    #   1. target == "render" → render_agent_arn() (env, else SSM)
    #   2. explicit runtime_arn (only if it looks like a real ARN)
    #   3. task_agent_arn() (default)
    target = (inp.get("target") or "").strip().lower()
    raw_runtime_arn = inp.get("runtime_arn") or ""
    # Guard against unresolved placeholders / non-ARN junk from the LLM.
    explicit_arn = raw_runtime_arn if raw_runtime_arn.startswith("arn:aws:") else ""

    render_arn = render_agent_arn() if target == "render" else ""
    if target == "render":
        target_arn = render_arn or explicit_arn or task_agent_arn()
    else:
        target_arn = explicit_arn or task_agent_arn()

    if target == "render" and not render_arn:
        return {"toolUseId": tool_use_id, "status": "error", "content": [{"text": "Render Agent runtime not configured (RENDER_AGENT_RUNTIME_ARN unset)"}]}

    if not target_arn:
        return {"toolUseId": tool_use_id, "status": "error", "content": [{"text": "No target runtime ARN configured (set TASK_AGENT_RUNTIME_ARN or pass runtime_arn)"}]}
    if not prompt:
        return {"toolUseId": tool_use_id, "status": "error", "content": [{"text": "prompt is required"}]}

    # Web Search follows the dispatching turn: off there -> the name is stripped here AND the sub-agent is told
    # (`web_search`), which also covers an empty `tools` (= "load all" over there). On -> it may be forwarded.
    import web_search as _web_search
    tools_list = _web_search.strip_tool_names(tools_list)
    payload = {"prompt": prompt, "web_search": _web_search.turn_allows()}
    if tools_list: payload["tools"] = tools_list
    if skills_list: payload["skills"] = skills_list
    if model_id: payload["model_id"] = model_id
    if system_prompt: payload["system_prompt"] = system_prompt

    # ---- async-job-tracking: forward actor/session identity (R3.1, R11.3) ----
    # The task agent runs in a separate runtime, so contextvars set in
    # the orchestrator's process don't propagate. Forward them on the
    # invoke payload so the sub-agent can bind its own
    # `jobs.context.set_request_context` before any tool dispatch and
    # the per-tool job-tracking wrap over there sees the original
    # Actor identity (rather than the Lambda role) on registry writes.
    # Resolution failures are non-fatal — if the orchestrator hasn't
    # bound a context (legacy paths, unit tests), we just skip the
    # forward and the sub-agent's wrap declines registration cleanly.
    _forward_request_context(payload)

    # Log which runtime is being targeted
    runtime_label = "render-agent" if render_arn and target_arn == render_arn else "task-agent"
    logger.info("Dispatching to %s: %d tools, %d skills, model=%s, arn=%s",
                runtime_label, len(tools_list), len(skills_list), model_id or "default",
                target_arn[-40:] if target_arn else "NONE")
    # Payload logging always goes through the scrubber: the grant and any user token are
    # credentials (SECRET_PAYLOAD_KEYS), and this payload never carries a project_token.
    logger.debug("dispatch_tool payload=%s", payload_summary(payload))

    try:
        # Read streaming response incrementally. Step events from the
        # sub-agent get forwarded to the orchestrator's progress channel
        # (so the UI sees per-tool progress in real time) while raw text
        # chunks accumulate into the final tool result.
        rendered: dict = {}
        result, had_error, saw_completion = _invoke_runtime_stream(target_arn, payload, rendered=rendered)

        logger.info("Task agent returned %d chars (error=%s, completion=%s)", len(result), had_error, saw_completion)

        if had_error:
            return {"toolUseId": tool_use_id, "status": "error", "content": [{"text": result or "Sub-agent encountered an error"}]}
        if not result and not saw_completion:
            if rendered:
                # The Render Agent ends on its manifest, with no text and no summary. That manifest was
                # forwarded and its tab opened, so this is the result, not "no output" (session acb56629:
                # the error read as a failure and the model re-dispatched the same render).
                what = rendered.get("label") or rendered.get("template") or "the result"
                return {"toolUseId": tool_use_id, "status": "success", "content": [{
                    "text": f"Rendered: {what} — the viewer tab is open. Do not re-dispatch."}]}
            return {"toolUseId": tool_use_id, "status": "error", "content": [{"text": "Sub-agent returned no output"}]}
        return {"toolUseId": tool_use_id, "status": "success", "content": [{"text": result}]}

    except Exception as e:
        logger.exception("Failed to invoke task agent")
        return {"toolUseId": tool_use_id, "status": "error", "content": [{"text": f"Error: {e}"}]}


_DISPATCH_BUDGET_SECONDS = int(os.environ.get("DISPATCH_BUDGET_SECONDS", "300"))


def _forward_request_context(payload: dict) -> None:
    """Copy the turn's actor/session/turn identity onto an outbound payload.

    The sub-agent runs in a separate runtime, so contextvars set here do not
    propagate; they travel on the invoke payload instead. Resolution failures
    are non-fatal — an unbound context (legacy paths, unit tests) just means
    the sub-agent's job-tracking wrap declines registration cleanly.
    """
    try:
        from jobs.context import (
            current_actor_id,
            current_session_id,
            current_turn_id,
            current_user_token,
        )
        payload["actor_id"] = current_actor_id()
        # The sub-agent needs a credential for the user_jwt gateway (fal / user MCP servers).
        # Prefer a FRESHLY MINTED delegation grant on every dispatch: the browser's access token
        # may lapse while the sub-agent reuses its warm connection (prod 45d2139c: 403
        # insufficient_scope six minutes after the token was bound). The user token is only a
        # fallback when no grant can be minted, and only while it has enough life left. Sent in
        # the payload (SigV4-signed hop to our own runtime); omitted keys never appear as "".
        from orchestration.dispatch_credentials import dispatch_credentials
        payload.update(dispatch_credentials(payload.get("actor_id") or "", current_user_token() or ""))
        payload["session_id"] = current_session_id()
        # Forward the turn so jobs the sub-agent dispatches land in the SAME
        # group as the parent's. Minting a fresh one in the sub-agent would
        # split one user request across several groups, which is precisely
        # what makes scoped retrieval useless.
        _turn = current_turn_id()
        if _turn:
            payload["turn_id"] = _turn
        # Media the sub-agent makes belongs to THIS turn's Project (ingestion Req 9.3). Forward the
        # TRUSTED id only (asset-lineage Phase 3 (a)): the sub-agent writes its outputs under it and
        # gets sandbox credentials for it, so a main turn whose project the proxy did not verify
        # (`project_access != "ok"`) must not hand it on — main itself writes legacy on such a turn.
        from jobs.context import trusted_project_id
        _pid = trusted_project_id()
        if _pid:
            payload["producing_project_id"] = _pid
    except Exception as e:
        logger.debug(
            "dispatch_tool: actor/session context unavailable; sub-agent "
            "job-tracking wrap will run without identity: %s", e,
        )
    # Knowledge-graph tools for the sub-agent: the active Project's id only (never a token — the sub-agent mints
    # its own Project_Token from the delegation grant). Independent of the block above, so an identity or
    # credential failure there cannot silently drop the scope.
    try:
        from project_search.context import graph_scope_project_id
        _graph_pid = graph_scope_project_id()
        if _graph_pid:
            payload["project_scope"] = {"project_id": _graph_pid}
    except Exception as e:  # noqa: BLE001 — the graph is optional
        logger.debug("dispatch_tool: no graph scope for the sub-agent: %s", e)


def _invoke_runtime_stream(target_arn: str, payload: dict, on_frame=None,
                           rendered: dict | None = None) -> tuple[str, bool, bool]:
    """Invoke an AgentCore runtime and consume its SSE stream.

    Returns ``(text, had_error, saw_completion)``. ``on_frame`` and ``rendered`` are passed to
    :func:`_consume_stream` (``search_dispatch`` uses ``on_frame``); the payload is never logged.
    """
    # Read streaming response incrementally. Step events from the
    # sub-agent get forwarded to the orchestrator's progress channel
    # (so the UI sees per-tool progress in real time) while raw text
    # chunks accumulate into the final tool result.
    client = _get_client()
    response = _invoke_traced(
        client,
        session_id=payload.get("session_id"),
        agentRuntimeArn=target_arn,
        qualifier="DEFAULT",
        payload=json.dumps(payload).encode(),
        contentType="application/json",
    )

    body = response.get("response", b"")
    if hasattr(body, "iter_lines"):
        iter_source = body.iter_lines()
    elif hasattr(body, "read"):
        raw = body.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        iter_source = raw.split("\n")
    elif isinstance(body, bytes):
        iter_source = body.decode("utf-8").split("\n")
    else:
        iter_source = str(body).split("\n")

    if on_frame is None:
        return _consume_stream(iter_source, rendered=rendered)
    return _consume_stream(iter_source, on_frame=on_frame, rendered=rendered)


# Synthetic ``on_frame`` type for "the wall-clock budget truncated the stream". Delivered with
# ``parsed=None``, which no real frame has (real frames are always dicts), so stream text shaped
# like ``{"type": "__budget_exceeded__"}`` cannot be mistaken for it.
BUDGET_EXCEEDED = "__budget_exceeded__"


def _notify_frame(on_frame, ev_type, parsed) -> bool:
    """Call ``on_frame``; a raising callback consumes nothing and never breaks the stream."""
    try:
        return bool(on_frame(ev_type, parsed))
    except Exception as e:  # noqa: BLE001
        logger.warning("dispatch_tool: on_frame callback failed (%s) for frame type %r",
                       type(e).__name__, ev_type if isinstance(ev_type, str) else type(ev_type).__name__)
        return False


def _note_render(rendered: dict, label, template) -> None:
    """Record a forwarded render manifest; the first label/template seen wins."""
    rendered["forwarded"] = True
    if isinstance(label, str) and label.strip():
        rendered.setdefault("label", label.strip())
    if isinstance(template, str) and template.strip():
        rendered.setdefault("template", template.strip())


def _consume_stream(iter_source, budget_seconds: int = _DISPATCH_BUDGET_SECONDS,
                    on_frame=None, rendered: dict | None = None) -> tuple[str, bool, bool]:
    """Walk the sub-agent SSE stream, forwarding step events to the
    orchestrator's progress channel and collecting raw text chunks.

    Returns a tuple of:
      - text: the concatenated text payload (the sub-agent's final response)
      - had_error: True if any ``{"type":"error"}`` event was seen
      - saw_completion: True if an ``execution_summary`` event was seen

    Step events are NOT included in the return string — they're
    side-channeled through ``progress_channel`` so the orchestrator's
    HTTP stream can interleave them with its own steps.

    ``on_frame(type, parsed)``, when given, sees every structured (dict) frame
    before the default handling; a truthy return consumes the frame. It is also
    called once with ``(BUDGET_EXCEEDED, None)`` when the budget truncates the
    stream. Note that string chunks are re-parsed, so model TEXT shaped like a
    frame reaches ``on_frame`` too: a callback must authenticate what it accepts.

    ``rendered``, when given, is filled with ``{label?, template?}`` once a render
    manifest has been forwarded to the progress channel (the UI opened its tab), so
    the caller can tell a render that ended without text from a run with no output.
    """
    channel = get_active_channel()
    chunks: list[str] = []
    had_error = False
    saw_completion = False
    start = time.time()

    for line in iter_source:
        if time.time() - start > budget_seconds:
            warning = f"[dispatch_tool] Stream exceeded {budget_seconds}s wall-clock budget — truncating."
            logger.warning(warning)
            chunks.append(warning)
            if on_frame is not None:
                _notify_frame(on_frame, BUDGET_EXCEEDED, None)
            break

        if isinstance(line, bytes):
            line = line.decode("utf-8")
        if not line or not line.startswith("data: "):
            continue
        val = line[6:]
        try:
            parsed = json.loads(val)
        except (json.JSONDecodeError, TypeError):
            # Not JSON — must be raw text from older SSE formats.
            chunks.append(val)
            continue

        # The sub-agent double-wraps strings sometimes (json.dumps of a
        # JSON string). Re-parse if we got a string back, otherwise treat
        # whatever we have as the actual payload.
        if isinstance(parsed, str):
            try:
                inner = json.loads(parsed)
                parsed = inner if isinstance(inner, (dict, list)) else parsed
            except (json.JSONDecodeError, TypeError):
                pass

        if isinstance(parsed, str):
            chunks.append(parsed)
            continue

        if isinstance(parsed, dict):
            ev_type = parsed.get("type")
            if on_frame is not None and _notify_frame(on_frame, ev_type, parsed):
                continue
            # The type dispatch below is UNCONDITIONAL — only the
            # ``channel.push`` calls are gated on having a channel. Conjoining
            # the type check with ``channel is not None`` (as this did) made a
            # ``step`` or ``output_ref`` arriving with no active progress
            # channel fall through to the error-key fallback at the bottom,
            # which read their optional ``error`` field as a stream failure.
            # An event's classification cannot depend on whether anyone is
            # listening to progress.
            if ev_type == "step":
                if channel is not None:
                    # Mark the event as coming from the sub-agent so the
                    # UI can group/colour steps by source. Drop it on the
                    # progress channel for the orchestrator to forward.
                    label = parsed.get("label") or "Sub-agent working..."
                    status = parsed.get("status") or "running"
                    duration_ms = parsed.get("duration_ms") or 0
                    extra = {k: v for k, v in parsed.items()
                             if k not in {"type", "status", "label", "duration_ms"}}
                    if parsed.get("render_manifest_key") or parsed.get("render_request_id") or parsed.get("render_failed"):
                        logger.info("dispatch_tool: forwarding render STEP rid=%s manifest=%s failed=%s",
                                    parsed.get("render_request_id"), bool(parsed.get("render_manifest_key")),
                                    parsed.get("render_failed"))
                    channel.push(
                        status,
                        f"↳ {label}",
                        agent="task",
                        duration_ms=int(duration_ms) if duration_ms else 0,
                        extra=extra,
                    )
                    if (rendered is not None and parsed.get("render_manifest_key")
                            and not parsed.get("render_failed")):
                        _note_render(rendered, parsed.get("render_label"), parsed.get("template"))
                # A step is progress and nothing else. With no channel there is
                # nowhere to forward it, but it is still CONSUMED here — a step
                # whose own ``error`` field is set (a failed tool the sub-agent
                # recovered from) must not become a stream-level failure.
                continue
            if ev_type == "output_ref":
                key = parsed.get("s3_key") or "output"
                # Render Agent manifests live at artifacts/*/render/*.json.
                # Forward the key structurally (via the step's `extra`) so the
                # frontend can fetch the manifest and render the NLE view —
                # this is the designed output_ref → UI render path. Other
                # sub-agent artifacts just get an informational step.
                is_render_manifest = "/render/" in key and key.endswith(".json")
                if channel is not None:
                    if is_render_manifest and not parsed.get("error"):
                        logger.info("dispatch_tool: forwarding render via OUTPUT_REF path key=%s rid=%s",
                                    key, parsed.get("render_request_id"))
                        extra = {"render_manifest_key": key}
                        label = parsed.get("label")
                        if isinstance(label, str) and label.strip():
                            extra["render_label"] = label.strip()
                        # Preserve the render correlation id so the UI fills the
                        # composing placeholder tab instead of opening a second one.
                        rid = parsed.get("render_request_id")
                        if rid:
                            extra["render_request_id"] = rid
                        channel.push(
                            "done",
                            "↳ Rendered artifacts",
                            agent="render",
                            extra=extra,
                        )
                        if rendered is not None:
                            _note_render(rendered, extra.get("render_label"), parsed.get("template"))
                    else:
                        channel.push(
                            "done",
                            f"↳ Sub-agent wrote {key}",
                            agent="task",
                        )
                # Consumed either way: ``output_ref`` carries an OPTIONAL
                # ``error`` describing that one artifact, which is what the
                # fallback below must never see.
                continue
            if ev_type == "execution_summary":
                # Internal accounting; don't surface to the user — the
                # orchestrator emits its own summary at the end.
                saw_completion = True
                # Lineage (projects.lineageEdges): the sub-agent's outputs join THIS turn's ledger with their
                # inputs, so the turn's Generated_Media message registers them with lineage. The merge takes
                # only our own output locations (text shaped like a frame cannot smuggle in a foreign edge).
                if parsed.get("lineage"):
                    try:
                        from media import turn_outputs as _turn_outputs
                        _turn_outputs.merge_lineage(parsed.get("lineage"), via="task_agent")
                    except Exception:  # noqa: BLE001 - lineage never breaks a dispatch
                        logger.debug("dispatch_tool: sub-agent lineage not merged", exc_info=True)
                continue
            if ev_type == "error":
                had_error = True
                err = parsed.get("error") or "Sub-agent error"
                if channel is not None:
                    channel.push("failed", f"↳ Sub-agent error: {err}", agent="task")
                # Still return the error text to the orchestrator so the
                # model can surface it in chat.
                chunks.append(str(err))
                continue
            # No recognized ``type``. A dict carrying an ``error`` key is a
            # structured runtime error (``tool_not_found``,
            # ``agent_config_unresolved``) — the task agent yields these
            # WITHOUT a ``type``, and dropping them surfaced as "Sub-agent
            # returned no output" instead of the actual cause. Checked last so
            # ``output_ref``'s own optional ``error`` field (handled above) is
            # never misread as a failure — which holds only because every known
            # type above ``continue``s unconditionally, channel or no channel.
            if parsed.get("error"):
                had_error = True
                err = parsed.get("message") or parsed.get("error")
                if channel is not None:
                    channel.push("failed", f"↳ Sub-agent error: {err}", agent="task")
                chunks.append(str(err))
                continue
            # Genuinely unknown structured event — ignore.
            continue

    return "".join(chunks), had_error, saw_completion
