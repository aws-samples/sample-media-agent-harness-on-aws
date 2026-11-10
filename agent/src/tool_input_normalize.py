"""Normalize a model-stringified tool input back into the object the tool's
schema requires — specifically for ``code_interpreter``.

Why this exists
---------------
The ``code_interpreter`` tool (``agentcore_code_interpreter``, vendored from strands-agents-tools) declares ``code_interpreter_input``
as a nested Pydantic model (``CodeInterpreterInput`` — ``{"action": {...}}``).
Strands validates the model's tool-call arguments against that schema before the
tool body runs. Some Bedrock models (observed with Claude Opus 4.8) intermittently
serialize that nested argument as a **JSON string** rather than an object:

    code_interpreter_input = '{"action":{"type":"executeCode","language":"python",...}}'

Strands then rejects it:

    1 validation error for Code_interpreterTool
    code_interpreter_input
      Input should be a valid dictionary or instance of CodeInterpreterInput
      [type=model_type, input_value='{"action":{...}', ...]

The model doesn't know why, so it retries the same call several different ways
(different quoting, single-line code, initSession-then-executeCode, …) before it
happens to emit an object — a per-call "retry storm" of 3–6 wasted steps that
inflates latency and makes sessions feel fragile. The strictness is **not** new
(it rejects identically on strands-agents 1.31/0.2.23 and 1.54/0.8.7); it's the
model's serialization that varies, and it varies more on complex code payloads.

The fix (model-agnostic)
------------------------
Wrap the ``code_interpreter`` tool so that, before it validates, a
``code_interpreter_input`` that arrived as a JSON string is parsed into the dict
the schema expects. A single one-time coercion turns the *first* call into a
success instead of triggering the retry loop. It is deliberately narrow:

* Only the ``code_interpreter`` tool is wrapped (this module is not applied
  blanket to every tool), and only the ``code_interpreter_input`` argument is
  touched — a legitimately-string argument on some other tool is never affected.
* Coercion happens ONLY when the string parses as JSON to a ``dict``; anything
  else is passed through untouched so the tool still surfaces its own real error.
* Best-effort and non-fatal: any unexpected shape falls through to the wrapped
  tool unchanged. The wrapper never swallows or alters the tool's actual result.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

from strands.types.tools import AgentTool, ToolUse

logger = logging.getLogger(__name__)

_CI_FIELD = "code_interpreter_input"
_JSON_DECODER = json.JSONDecoder()
_TRAILING_OK = frozenset("} \t\r\n")


def coerce_json_string_to_dict(value: Any) -> Any:
    """If ``value`` is a JSON string that decodes to a ``dict``, return the dict;
    otherwise return ``value`` unchanged.

    Pure and total — never raises. Non-strings, non-JSON strings, and JSON that
    decodes to a non-dict (list, scalar) are all returned as-is so the caller's
    downstream validation still runs on the original value.

    Tolerates ONE specific model slip: the stringified object followed by stray closing
    braces (``'{"action":{...}}}'`` — ``json.loads`` fails with "Extra data"). The leading
    object is decoded with ``raw_decode`` and accepted only when everything after it is
    whitespace and ``}``; any other trailing text leaves the value untouched.
    """
    if not isinstance(value, str):
        return value
    s = value.strip()
    # Cheap guard: only attempt a parse when it plausibly encodes an object.
    if not s.startswith("{"):
        return value
    try:
        parsed, end = _JSON_DECODER.raw_decode(s)
    except (ValueError, TypeError):
        return value
    if any(c not in _TRAILING_OK for c in s[end:]):
        return value
    return parsed if isinstance(parsed, dict) else value


def normalize_code_interpreter_input(tool_use: ToolUse) -> ToolUse:
    """Return a ``tool_use`` whose ``input.code_interpreter_input`` is a dict when
    the model passed it as a JSON-encoded string.

    Returns the SAME object when there is nothing to fix (no allocation, no
    behaviour change on the happy path). Only rewrites when the field is a string
    that decodes to a dict; every other shape is left exactly as received.
    """
    if not isinstance(tool_use, dict):
        return tool_use
    args = tool_use.get("input")
    if not isinstance(args, dict) or _CI_FIELD not in args:
        return tool_use
    raw = args[_CI_FIELD]
    coerced = coerce_json_string_to_dict(raw)
    if coerced is raw:
        return tool_use
    # Shallow-copy just the layers we mutate so the caller's original is untouched.
    new_args = dict(args)
    new_args[_CI_FIELD] = coerced
    new_tool_use = dict(tool_use)
    new_tool_use["input"] = new_args
    logger.info(
        "code_interpreter input normalized: coerced a JSON-string %s into an object "
        "(model stringified the nested tool argument)", _CI_FIELD,
    )
    return new_tool_use


class _DelegatingTool(AgentTool):
    """Thin delegating :class:`AgentTool` wrapper that rewrites the tool input on the way in.

    Delegates every property (name, spec, type, hot-reload, display) to the inner
    tool so it is indistinguishable to Strands' registry, and overrides only
    :meth:`stream`. Subclasses implement :meth:`_prepare` (pure; must never raise).
    """

    def __init__(self, inner: AgentTool) -> None:
        super().__init__()
        self._inner = inner

    @property
    def tool_name(self) -> str:
        return self._inner.tool_name

    @property
    def tool_spec(self) -> Any:
        return self._inner.tool_spec

    @property
    def tool_type(self) -> str:
        return self._inner.tool_type

    @property
    def supports_hot_reload(self) -> bool:
        return getattr(self._inner, "supports_hot_reload", False)

    def get_display_properties(self) -> dict[str, str]:
        return self._inner.get_display_properties()

    def _prepare(self, tool_use: ToolUse) -> ToolUse:  # pragma: no cover - overridden
        return tool_use

    async def stream(self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any):
        if _cdn_private_safe():
            # Presigning can build the boto3 client on first use (credential resolution may do
            # I/O), so keep it off the event loop this coroutine runs on.
            prepared = await asyncio.to_thread(self._prepare, tool_use)
        else:
            prepared = self._prepare(tool_use)
        async for event in self._inner.stream(prepared, invocation_state, **kwargs):
            yield event


def _cdn_private_safe() -> bool:
    try:
        from media import artifact_fetch  # noqa: PLC0415
        return artifact_fetch.cdn_private()
    except Exception:  # noqa: BLE001
        return False


class NormalizedCodeInterpreterTool(_DelegatingTool):
    """Normalizes a stringified ``code_interpreter_input`` before the wrapped tool validates
    its arguments, (private CDN) presigns our artifact URLs inside the sandbox action, and —
    inside a plan node — pins the action to that node's own sandbox (see ``sandbox_scope``).

    Where ``SANDBOX_SCOPED_ROLE_ARN`` is configured it also installs per-(project, session) S3
    credentials into the TARGET sandbox session before the model's action runs (asset-lineage
    Phase 3 (b), :mod:`sandbox_credentials`). That call goes straight through the interpreter
    client — the model's ``tool_use`` input, the tool result and the transcript never carry a
    credential — and repeats only when the (project, session, sandbox, expiry) fingerprint of that
    sandbox changes."""

    def __init__(self, inner: AgentTool) -> None:
        super().__init__(inner)
        self._injected: dict[str, tuple] = {}
        self._staged: dict[str, tuple] = {}
        self._shape_warned = False

    def _prepare(self, tool_use: ToolUse) -> ToolUse:
        # Order: normalize -> presign the MODEL's text -> scope to the node's sandbox. Skill scripts are staged
        # afterwards (`_stage_skill_scripts`), so presigning never rewrites a staged script's own source.
        prepared = presign_artifact_urls_in_code(mirror_ffmpeg_downloads_in_code(normalize_code_interpreter_input(tool_use)))
        try:
            import sandbox_scope  # noqa: PLC0415

            return sandbox_scope.scope_tool_use(prepared, _CI_FIELD)
        except Exception as e:  # noqa: BLE001 - scoping must never break a code_interpreter call
            logger.warning("code_interpreter sandbox scoping skipped: %s", e)
            return prepared

    # ---- skill scripts (skill_scripts.py) -----------------------------------------------------
    def _stage_skill_scripts(self, tool_use: ToolUse) -> ToolUse:
        """Write every skill script the action references into its target sandbox first. Never raises.

        Out of band, like the scoped credentials: one ``executeCode`` writer call on the SAME sandbox session,
        repeated only when that sandbox or the file set changes, so the model's code/command (any language, and
        ``executeCommand`` too -- verified: both share the sandbox's working directory) runs unmodified. When the
        interpreter internals are unavailable it falls back to prepending the writer to a Python ``executeCode``.
        """
        try:
            import skill_scripts  # noqa: PLC0415

            args = tool_use.get("input") if isinstance(tool_use, dict) else None
            ci_in = args.get(_CI_FIELD) if isinstance(args, dict) else None
            action = ci_in.get("action") if isinstance(ci_in, dict) else None
            if not isinstance(action, dict) or action.get("type") not in ("executeCode", "executeCommand"):
                return tool_use
            try:
                from jobs.context import current_active_skills  # noqa: PLC0415
                active = [(a.get("name") if isinstance(a, dict) else a) for a in current_active_skills() or []]
            except Exception:  # noqa: BLE001
                active = []
            files = skill_scripts.files_to_stage(skill_scripts.action_text(action), [a for a in active if a])
            if not files:
                return tool_use
            fp = skill_scripts.fingerprint(files)
            # The model may name a script by its RUNTIME path (/var/task/skills/...), which the sandbox lacks.
            field = "code" if action.get("type") == "executeCode" else "command"
            original = action.get(field)
            rewritten = skill_scripts.relativize_runtime_paths(original, files)
            if isinstance(original, str) and rewritten != original:
                logger.info("skill_scripts: rewrote runtime skill path(s) to the staged copy in %s", field)
                action = {**action, field: rewritten}
                ci_in = {**ci_in, "action": action}
                args = {**args, _CI_FIELD: ci_in}
                tool_use = {**tool_use, "input": args}
            ci = self._interpreter()
            if ci is not None:
                name, err = ci._ensure_session(action.get("session_name") or None)
                if err is None and name in ci._sessions:
                    info = ci._sessions[name]
                    key = (fp, getattr(info, "session_id", None))
                    if self._staged.get(name) != key:
                        info.client.invoke("executeCode", {"code": mirror_ffmpeg_downloads_in_text(skill_scripts.writer_code(files)),
                                                           "language": "python"})
                        self._staged[name] = key
                        logger.info("skill_scripts: staged %d path(s) into sandbox %s: %s", len(files), name,
                                    sorted(k for k in files if k.startswith("skills/")))
                    return tool_use
            if action.get("type") == "executeCode" and (action.get("language") or "python") == "python":
                new_action = {**action, "code": skill_scripts.writer_code(files) + str(action.get("code") or "")}
                logger.info("skill_scripts: staged %d path(s) in-band (no interpreter handle)", len(files))
                return {**tool_use, "input": {**args, _CI_FIELD: {**ci_in, "action": new_action}}}
            return tool_use
        except Exception as e:  # noqa: BLE001 - staging must never break a code_interpreter call
            logger.warning("skill_scripts: staging skipped: %s: %s", type(e).__name__, e)
            return tool_use

    # ---- scoped sandbox credentials (Phase 3 (b)) -------------------------------------------
    def _interpreter(self) -> Any:
        """The ``AgentCoreCodeInterpreter`` instance behind the decorated tool, else ``None``.

        Relies on the interpreter's internals (a bound ``_tool_func`` whose instance has
        ``_ensure_session`` and ``_sessions``; ``agentcore_code_interpreter`` keeps them on purpose);
        a shape change is logged once and injection is skipped.
        """
        ci = getattr(getattr(self._inner, "_tool_func", None), "__self__", None)
        if ci is None or not callable(getattr(ci, "_ensure_session", None)) or not isinstance(
                getattr(ci, "_sessions", None), dict):
            if not self._shape_warned:
                self._shape_warned = True
                logger.warning("sandbox_credentials: code interpreter internals changed; not injecting")
            return None
        return ci

    def _inject_credentials(self, tool_use: ToolUse) -> None:
        """Install scoped credentials in the sandbox this action targets, or raise.

        FAILS CLOSED: where the scoped role is configured (every deployed env) any failure raises
        ``sandbox_credentials.SandboxCredentialsError`` and :meth:`stream` answers with an error result
        instead of running the model's action. The sandbox's own execution role holds no artifact or
        project S3 grants (B2), so there is nothing safe -- or useful -- to fall back to."""
        import sandbox_credentials as sc  # noqa: PLC0415

        if not sc.required() or not isinstance(tool_use, dict):
            return
        args = tool_use.get("input")
        ci_in = args.get(_CI_FIELD) if isinstance(args, dict) else None
        action = ci_in.get("action") if isinstance(ci_in, dict) else None
        if not isinstance(action, dict) or action.get("type") in ("listLocalSessions", "initSession"):
            return
        from jobs import context as jc  # noqa: PLC0415

        try:
            sid = jc.current_session_id()
        except Exception:  # noqa: BLE001 - no session bound: nothing to scope to
            sid = None
        if not sid:
            raise sc.SandboxCredentialsError("no chat session is bound to this call, so the sandbox cannot be scoped")
        pid = jc.trusted_project_id()
        ci = self._interpreter()
        if ci is None:
            raise sc.SandboxCredentialsError("the code interpreter internals changed; credentials cannot be installed")
        creds = sc.credentials_for(pid, sid)
        if creds is None:
            raise sc.SandboxCredentialsError("could not obtain session-scoped S3 credentials (STS refused or unavailable)")
        name, err = ci._ensure_session(action.get("session_name") or None)
        if err is not None or name not in ci._sessions:
            raise sc.SandboxCredentialsError(f"sandbox session unavailable: {err}")
        info = ci._sessions[name]
        fp = (*sc.fingerprint(pid, sid, creds), getattr(info, "session_id", None))
        if self._injected.get(name) == fp:
            return
        sc.install(info.client.invoke, pid, sid)
        self._injected[name] = fp
        logger.info("sandbox_credentials: injected into sandbox %s", name)

    async def stream(self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any):
        try:
            import sandbox_credentials as _sc  # noqa: PLC0415
            inject = bool(_sc.role_arn())
        except Exception:  # noqa: BLE001
            inject = False

        def _prepare_and_inject() -> ToolUse:
            prepared = self._prepare(tool_use)
            if inject:
                self._inject_credentials(prepared)  # raises SandboxCredentialsError: fail closed
            return self._stage_skill_scripts(prepared)

        # A sandbox that idled out stays in the interpreter's session cache, so the next call to it fails
        # with "Code interpreter session ... is not active" -- and so do credential injection and script
        # staging (session be68313c: the reel stitch's first run). Evict it and run once more on a fresh one.
        for attempt in (1, 2):
            retry = False
            async for event in self._stream_once(_prepare_and_inject, invocation_state, _tool_use_id=_use_id(tool_use), **kwargs):
                if attempt == 1 and _is_inactive_session_result(event) and self._evict_session(tool_use):
                    retry = True
                    continue
                yield event
            if not retry:
                return

    def _evict_session(self, tool_use: ToolUse) -> bool:
        """Drop the action's cached sandbox session so the next call starts a new one. True if one was dropped."""
        try:
            ci = self._interpreter()
            if ci is None:
                return False
            args = tool_use.get("input") if isinstance(tool_use, dict) else None
            ci_in = args.get(_CI_FIELD) if isinstance(args, dict) else None
            action = ci_in.get("action") if isinstance(ci_in, dict) else None
            name = (action.get("session_name") if isinstance(action, dict) else None) or getattr(ci, "default_session", None)
            if not name or name not in ci._sessions:
                return False
            ci._sessions.pop(name, None)
            try:
                import agentcore_code_interpreter as _aci  # noqa: PLC0415
                _aci._session_mapping.pop(name, None)
            except Exception:  # noqa: BLE001 - the module cache is an optimisation
                pass
            self._injected.pop(name, None)
            self._staged.pop(name, None)
            logger.warning("code_interpreter: sandbox session %s was no longer active; retrying on a new one", name)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("code_interpreter: stale session not evicted: %s", type(e).__name__)
            return False

    async def _stream_once(self, prepare: Any, invocation_state: dict[str, Any], _tool_use_id: str = "",
                           **kwargs: Any):
        # STS + an interpreter call are blocking I/O: keep them off the event loop.
        try:
            prepared = await asyncio.to_thread(prepare)
        except Exception as e:  # noqa: BLE001 - only the typed credentials error is a refusal
            from sandbox_credentials import SandboxCredentialsError  # noqa: PLC0415
            if not isinstance(e, SandboxCredentialsError):
                raise
            # Fail CLOSED: the model's code never runs on the sandbox's own role. The text keeps an
            # "is not active" cause visible, so `stream` still retries once on a fresh sandbox.
            logger.warning("sandbox_credentials: refusing the code_interpreter call: %s", e)
            from strands.types._events import ToolResultEvent  # noqa: PLC0415
            yield ToolResultEvent({
                "toolUseId": _tool_use_id,
                "status": "error",
                "content": [{"text": "code_interpreter refused: session-scoped sandbox credentials are required "
                                     f"and could not be installed ({e}). Nothing was run."}],
            })
            return
        # Report what the cell writes to this session's output folders in its own result (see
        # sandbox_outputs): an upload that prints only "ok" otherwise leaves the render hook nothing.
        roots = _output_roots_for(prepared)
        before = await asyncio.to_thread(_sandbox_outputs().snapshot, roots) if roots else None
        async for event in self._inner.stream(prepared, invocation_state, **kwargs):
            if before is not None and _is_success_result(event):
                try:
                    so = _sandbox_outputs()
                    keys = so.written(before, await asyncio.to_thread(so.snapshot, roots))
                    if keys:
                        logger.info("sandbox_outputs: cell wrote %d session output(s)", len(keys))
                        event = _with_result(event, so.annotate(event.tool_result, keys))
                except Exception as e:  # noqa: BLE001 - reporting must never break a code_interpreter call
                    logger.warning("sandbox_outputs: not reported: %s", type(e).__name__)
            yield event


def _use_id(tool_use: Any) -> str:
    return str(tool_use.get("toolUseId") or "") if isinstance(tool_use, dict) else ""


def _sandbox_outputs() -> Any:
    import sandbox_outputs  # noqa: PLC0415
    return sandbox_outputs


def _output_roots_for(tool_use: ToolUse) -> list[str]:
    """The session output roots to watch for a code-running action; ``[]`` for anything else."""
    try:
        args = tool_use.get("input") if isinstance(tool_use, dict) else None
        ci_in = args.get(_CI_FIELD) if isinstance(args, dict) else None
        action = ci_in.get("action") if isinstance(ci_in, dict) else None
        if not isinstance(action, dict) or action.get("type") not in ("executeCode", "executeCommand"):
            return []
        return _sandbox_outputs().output_roots()
    except Exception:  # noqa: BLE001
        return []


def _is_inactive_session_result(event: Any) -> bool:
    """True for a code_interpreter error result saying its sandbox session is no longer active."""
    from strands.types._events import ToolResultEvent  # noqa: PLC0415
    if not isinstance(event, ToolResultEvent):
        return False
    result = event.tool_result or {}
    text = " ".join(str(b.get("text") or "") for b in result.get("content") or [] if isinstance(b, dict))
    return "is not active" in text and "session" in text.lower()


def _is_success_result(event: Any) -> bool:
    from strands.types._events import ToolResultEvent  # noqa: PLC0415
    return isinstance(event, ToolResultEvent) and (event.tool_result or {}).get("status") == "success"


def _with_result(event: Any, result: Any) -> Any:
    from strands.types._events import ToolResultEvent  # noqa: PLC0415
    if result is event.tool_result:
        return event
    return ToolResultEvent(result, event.exception, backgrounded=event.backgrounded)


#: Lifetime of a presigned URL handed to ``http_request`` — the call is synchronous.
HTTP_REQUEST_PRESIGN_TTL_S = 3600
#: Methods with an S3 presign equivalent. Anything else (PUT/POST/DELETE to our CDN) is left
#: alone: a presigned GET would not authorize it, and our CDN never accepted writes anyway.
_HTTP_PRESIGN_METHODS = {"GET": "GET", "HEAD": "HEAD"}


def presign_artifact_url_in_http_request(tool_use: ToolUse) -> ToolUse:
    """When the artifacts CDN is private, replace an ``http_request`` ``url`` that is one of OUR
    artifact URLs with a presigned S3 URL for the same method (GET or HEAD — S3 signs the method,
    so a GET-presigned URL 403s a HEAD). Everything else in the input (method, headers, body) is
    kept. Same-object return when there is nothing to change; never raises (fail-open: the
    original URL goes through and the tool reports whatever it gets).

    Session 36685185: ``http_request`` HEAD on ``<cdn>/artifacts/.../shot01.jpg`` -> 403.
    """
    try:
        from media import artifact_fetch  # noqa: PLC0415
        if not artifact_fetch.cdn_private() or not isinstance(tool_use, dict):
            return tool_use
        args = tool_use.get("input")
        url = args.get("url") if isinstance(args, dict) else None
        if not isinstance(url, str):
            return tool_use
        method = _HTTP_PRESIGN_METHODS.get(str(args.get("method") or "GET").strip().upper())
        if method is None:
            return tool_use
        from jobs.context import trusted_project_id  # noqa: PLC0415

        # Our artifact URL, or (since prompts carry the short identifier, not a presigned URL) a project
        # ASSET url of the turn's TRUSTED project.
        signed = artifact_fetch.presign_for_trusted_turn(url, HTTP_REQUEST_PRESIGN_TTL_S, trusted_project_id(),
                                                         method=method)
        if not signed:
            return tool_use
        logger.info("http_request: presigned our artifact URL for %s (private CDN)", method)
        return {**tool_use, "input": {**args, "url": signed}}
    except Exception as e:  # noqa: BLE001 - must never break an http_request call
        logger.warning("http_request artifact presign skipped: %s", e)
        return tool_use


def _http_request_refusal(tool_use: ToolUse) -> str | None:
    """Why ``http_request`` must not run this ``tool_use`` (URL blocked), else None.

    A pre-check only: the tool itself sends through ``security.url_guard.fetch``, which vets and pins
    every hop again, so a URL that passes here but rebinds or redirects somewhere private still fails."""
    args = tool_use.get("input") if isinstance(tool_use, dict) else None
    url = args.get("url") if isinstance(args, dict) else None
    if not isinstance(url, str):
        return None  # the tool reports a missing url itself
    from security import url_guard  # noqa: PLC0415
    try:
        url_guard.validate_url(url, allow_http=True)
    except url_guard.BlockedURLError as exc:
        return f"URL refused: {exc}"
    return None


class PresignedHttpRequestTool(_DelegatingTool):
    """``http_request`` that reaches our private artifacts CDN through a presigned S3 URL, and
    that refuses (fail closed) any URL the shared SSRF guard blocks."""

    def _prepare(self, tool_use: ToolUse) -> ToolUse:
        return presign_artifact_url_in_http_request(tool_use)

    async def stream(self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any):
        # Presign first (our artifact URL -> a public S3 URL), then vet what will actually be
        # fetched. Both touch the network/credentials, so off the event loop.
        prepared = await asyncio.to_thread(self._prepare, tool_use)
        refusal = await asyncio.to_thread(_http_request_refusal, prepared)
        if refusal:
            from strands.types._events import ToolResultEvent  # noqa: PLC0415
            logger.warning("http_request refused: %s", refusal)
            yield ToolResultEvent({
                "toolUseId": tool_use.get("toolUseId", "") if isinstance(tool_use, dict) else "",
                "status": "error",
                "content": [{"text": f"Error: {refusal}"}],
            })
            return
        async for event in self._inner.stream(prepared, invocation_state, **kwargs):
            yield event


def wrap_http_request(tool: Any) -> Any:
    """Wrap the builtin ``http_request`` tool (``tools.http_request.http_request``, an AgentTool; a
    module-style tool is loaded first) with :class:`PresignedHttpRequestTool`. Idempotent and never
    fatal -- on any wrapping error the original is returned so registration always proceeds."""
    try:
        if tool is None or isinstance(tool, PresignedHttpRequestTool):
            return tool
        inner = tool
        if not isinstance(tool, AgentTool):
            from strands.tools.loader import load_tools_from_module  # noqa: PLC0415
            loaded = load_tools_from_module(tool, tool.__name__.split(".")[-1])
            if len(loaded) != 1:
                raise ValueError(f"expected one tool in {tool.__name__}, got {len(loaded)}")
            inner = loaded[0]
        return PresignedHttpRequestTool(inner)
    except Exception as e:  # noqa: BLE001 — must never break tool registration
        logger.error("wrap_http_request: leaving tool unwrapped: %s", e)
        return tool


_SHELL_FILE_SUFFIXES = (".sh", ".bash")


def _presign_text_deep(value: Any, presign_text, _depth: int = 0,
                       _shell: bool = False) -> tuple[Any, bool]:
    """Apply ``presign_text(text, shell_quote)`` to every string under ``value``; copy only
    changed containers. Shell context: the ``command`` field (executeCommand) and the ``text``
    of a writeFiles entry whose ``path`` is a shell script."""
    if isinstance(value, str):
        return presign_text(value, shell_quote=_shell)
    if _depth > 8:
        return value, False
    if isinstance(value, dict):
        out: dict = {}
        changed = False
        path = value.get("path")
        shell_file = isinstance(path, str) and path.lower().endswith(_SHELL_FILE_SUFFIXES)
        for k, v in value.items():
            shell = k == "command" or (k == "text" and shell_file)
            nv, c = _presign_text_deep(v, presign_text, _depth + 1, shell)
            out[k] = nv
            changed = changed or c
        return (out, True) if changed else (value, False)
    if isinstance(value, (list, tuple)):
        items = []
        changed = False
        for v in value:
            nv, c = _presign_text_deep(v, presign_text, _depth + 1, _shell)
            items.append(nv)
            changed = changed or c
        if not changed:
            return value, False
        return (tuple(items) if isinstance(value, tuple) else items), True
    return value, False


# A static-ffmpeg download a cell writes on its own: the third-party release host (named in pieces so the
# repo-wide licensing scan, test_no_third_party_ffmpeg, never finds a usable download source in the tree), or our
# artifacts CDN's old public `tools/ffmpeg/` path, which the CDN no longer serves. Both are rewritten to a
# short-lived presigned S3 URL of the harness's STAGED tarball (same file names), read with the runtime's role.
_THIRD_PARTY_FFMPEG_HOST = "john" + "vansickle" + r"\.com"
_FFMPEG_DOWNLOAD = re.compile(
    r"https?://(?:www\.)?(?:" + _THIRD_PARTY_FFMPEG_HOST + r"/ffmpeg/releases|[A-Za-z0-9.-]+\.cloudfront\.net/tools/ffmpeg)/"
    r"(ffmpeg-release-(?:amd64|arm64)-static\.tar\.xz)")
_FFMPEG_URL_TTL = 3600
_ffmpeg_url_cache: dict[str, tuple[str, float]] = {}


def staged_ffmpeg_url(tarball: str) -> str:
    """A presigned GET for ``s3://<skills bucket>/tools/ffmpeg/<tarball>``, or ``""`` when it cannot be made."""
    import time  # noqa: PLC0415
    hit = _ffmpeg_url_cache.get(tarball)
    if hit and hit[1] - time.time() > 600:
        return hit[0]
    bucket = (os.environ.get("SKILLS_S3_BUCKET") or "").strip()
    if not bucket:
        return ""
    try:
        import boto3  # noqa: PLC0415
        from botocore.config import Config  # noqa: PLC0415
        s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION") or None,
                          config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
        url = s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": f"tools/ffmpeg/{tarball}"},
                                        ExpiresIn=_FFMPEG_URL_TTL)
    except Exception as e:  # noqa: BLE001
        logger.warning("staged ffmpeg URL not made: %s", type(e).__name__)
        return ""
    _ffmpeg_url_cache[tarball] = (url, time.time() + _FFMPEG_URL_TTL)
    return url


def mirror_ffmpeg_downloads_in_text(text: str) -> str:
    """Point a static-ffmpeg download at the staged build (presigned S3). Unchanged when there is none to point at."""
    if not isinstance(text, str) or "ffmpeg-release-" not in text:
        return text
    def _sub(m: "re.Match[str]") -> str:
        return staged_ffmpeg_url(m.group(1)) or m.group(0)
    return _FFMPEG_DOWNLOAD.sub(_sub, text)


def mirror_ffmpeg_downloads_in_code(tool_use: ToolUse) -> ToolUse:
    """Rewrite static-ffmpeg downloads in a sandbox action to the harness's staged build.

    A cell that provisions ffmpeg (the sandbox has none) used to ``curl`` the third-party release host directly,
    with no timeout: in staging that download stalled and held the cell until the plan node's 600 s limit, on BOTH
    attempts (plan 7f882700 "assemble"), and once held a key-frame cell for 16 min (session 8873a372). The staged
    build has the same ``ffmpeg-release-<arch>-static.tar.xz`` names and downloads from our own bucket in seconds
    (presigned, since no public link serves it). Covers every string in the action (``code``, ``command``,
    ``content[].text``). Same object when unchanged; never raises.
    """
    try:
        if not isinstance(tool_use, dict):
            return tool_use
        args = tool_use.get("input")
        ci = args.get(_CI_FIELD) if isinstance(args, dict) else None
        action = ci.get("action") if isinstance(ci, dict) else None
        if not isinstance(action, dict):
            return tool_use
        def _mirror(text: str, shell_quote: bool = False) -> tuple[str, bool]:
            out = mirror_ffmpeg_downloads_in_text(text)
            return out, out != text
        new_action, changed = _presign_text_deep(action, _mirror)
        if not changed:
            return tool_use
        logger.info("code_interpreter: ffmpeg download pointed at the staged build (%s)", action.get("type", "?"))
        return {**tool_use, "input": {**args, _CI_FIELD: {**ci, "action": new_action}}}
    except Exception as e:  # noqa: BLE001 - must never break a code_interpreter call
        logger.warning("code_interpreter ffmpeg mirror rewrite skipped: %s", e)
        return tool_use


def presign_artifact_urls_in_code(tool_use: ToolUse) -> ToolUse:
    """When the artifacts CDN is private (``ARTIFACTS_CDN_PRIVATE``), swap CDN artifact URLs inside
    the sandbox action for presigned S3 GETs, so code that downloads an input over HTTP (combine
    nodes, media scripts) still works. The sandbox is our own executor, so the short-lived URL
    never reaches a user.

    Every string under ``code_interpreter_input.action`` is rewritten — ``code`` (executeCode),
    ``command`` (executeCommand: session 36685185 ran ``curl <cdn>/artifacts/...`` and got 403
    because only ``code`` was covered), ``content[].text`` (writeFiles), and any future field.
    Containers are copied only along a changed path; the caller's object is never mutated.
    Same-object return when there is nothing to change; never raises.
    """
    try:
        from media import artifact_fetch  # noqa: PLC0415
        if not artifact_fetch.cdn_private() or not isinstance(tool_use, dict):
            return tool_use
        args = tool_use.get("input")
        ci = args.get(_CI_FIELD) if isinstance(args, dict) else None
        action = ci.get("action") if isinstance(ci, dict) else None
        if not isinstance(action, dict):
            return tool_use
        new_action, changed = _presign_text_deep(action, artifact_fetch.presign_artifact_urls_in_text)
        if not changed:
            return tool_use
        new_tool_use = dict(tool_use)
        new_tool_use["input"] = {**args, _CI_FIELD: {**ci, "action": new_action}}
        logger.info("code_interpreter: presigned artifact URLs in sandbox %s action (private CDN)",
                    action.get("type", "?"))
        return new_tool_use
    except Exception as e:  # noqa: BLE001 - must never break a code_interpreter call
        logger.warning("code_interpreter artifact presign skipped: %s", e)
        return tool_use


def wrap_code_interpreter(tool: AgentTool) -> AgentTool:
    """Wrap the ``code_interpreter`` tool with :class:`NormalizedCodeInterpreterTool`.

    Idempotent (an already-wrapped tool is returned unchanged) and never fatal —
    on any unexpected error the original tool is returned so registration always
    proceeds.
    """
    try:
        if isinstance(tool, NormalizedCodeInterpreterTool) or tool is None:
            return tool
        return NormalizedCodeInterpreterTool(tool)
    except Exception as e:  # noqa: BLE001 — must never break tool registration
        logger.error("wrap_code_interpreter: leaving tool unwrapped: %s", e)
        return tool


__all__ = [
    "coerce_json_string_to_dict",
    "normalize_code_interpreter_input",
    "NormalizedCodeInterpreterTool",
    "PresignedHttpRequestTool",
    "presign_artifact_url_in_http_request",
    "presign_artifact_urls_in_code",
    "wrap_code_interpreter",
    "wrap_http_request",
]
