"""Give each plan node its OWN code-interpreter sandbox.

The Task Agent builds ONE ``AgentCoreCodeInterpreter`` per process, and the plan chainer routes
every node of a plan to the SAME runtime session (one microVM) so the plan pays about one cold
start. Sandbox sessions are looked up BY NAME in a module-level map, so every node running in
that microVM resolved the same names to the same sandbox:

  * a node that passes no ``session_name`` gets the instance's single ``default_session``;
  * nodes that happen to pick the same name ("ffmpeg-trim" was chosen 611 times in one
    staging window) get each other's sandbox.

One sandbox is one machine with one filesystem, so concurrent nodes sharing it clobber each other's
working files. Measured against the real Code Interpreter, through this exact tool path, with two
concurrent "nodes" that each write ``/tmp/src.mp4`` and read it back: unscoped, one node read the
OTHER node's file in 2 of 2 runs; scoped, each read its own every time. Seven trims of one source
all downloading to the same path is therefore a correctness bug — a clip can be cut from the wrong
file — not only a contention one. (Whether a shared sandbox also serializes cells was inconsistent:
a raw-client test took 12.2 s for two concurrent 6 s cells, the tool path overlapped them.)

The fix is a per-node namespace: while a plan node runs, every sandbox session name it uses —
the default included — is prefixed with that node's scope, so concurrent nodes can never share
a sandbox, and a node's own successive calls still share theirs (download once, cut many).
Outside a plan node (the orchestrator's own turns) no scope is bound and nothing changes: there
the shared, persistent sandbox across a conversation is the intended behavior.

When the node finishes, its sandboxes are stopped instead of idling to the 900 s session timeout.
"""

from __future__ import annotations

import contextvars
import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: AgentCore's ``StartCodeInterpreterSession.name`` limit.
_MAX_SESSION_NAME = 100
#: Separates the node scope from the name the model asked for.
_SEP = "--"
#: The name used when the model passes no ``session_name``.
_DEFAULT_LEAF = "default"

_scope: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("sandbox_scope", default=None)


def node_scope(plan_id: str, node_id: str) -> str:
    """A stable, name-safe scope for one plan node (retries of the node reuse it)."""
    raw = f"pn-{(plan_id or '')[:8]}-{node_id or 'node'}"
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", raw)[:60]


def bind(scope: str) -> contextvars.Token:
    """Bind ``scope`` for the current context (a plan node's execution)."""
    return _scope.set(scope)


def reset(token: contextvars.Token) -> None:
    _scope.reset(token)


def current() -> Optional[str]:
    return _scope.get()


def scoped_name(name: Optional[str], scope: Optional[str] = None) -> Optional[str]:
    """The sandbox session name to use for ``name`` under ``scope`` (default: the bound one).

    No scope bound → ``name`` unchanged (``None`` keeps the tool's own default session).
    Already scoped → unchanged, so applying this twice is harmless.
    """
    scope = scope if scope is not None else _scope.get()
    if not scope:
        return name
    leaf = name.strip() if isinstance(name, str) and name.strip() else _DEFAULT_LEAF
    prefix = f"{scope}{_SEP}"
    if leaf.startswith(prefix):
        return leaf[:_MAX_SESSION_NAME]
    return f"{prefix}{leaf}"[:_MAX_SESSION_NAME]


def scope_tool_use(tool_use: Any, field: str = "code_interpreter_input") -> Any:
    """Rewrite a code_interpreter tool use so its action runs in this node's sandbox.

    Same-object return when no scope is bound or the input is not the expected shape; the caller's
    object is never mutated. ``listLocalSessions`` takes no session and is left alone.
    """
    scope = _scope.get()
    if not scope or not isinstance(tool_use, dict):
        return tool_use
    args = tool_use.get("input")
    ci = args.get(field) if isinstance(args, dict) else None
    action = ci.get("action") if isinstance(ci, dict) else None
    if not isinstance(action, dict) or action.get("type") == "listLocalSessions":
        return tool_use
    target = scoped_name(action.get("session_name"), scope)
    if action.get("session_name") == target:
        return tool_use
    new_action = {**action, "session_name": target}
    return {**tool_use, "input": {**args, field: {**ci, "action": new_action}}}


def release(code_interpreter: Any, scope: str) -> int:
    """Stop every sandbox session this node opened and forget it. Returns how many were stopped.

    Reaches into the interpreter's session bookkeeping (``_sessions`` on the instance, the
    module-level ``_session_mapping``) because the tool offers no public "stop by name". Every
    step is best-effort and never raises: a session that cannot be stopped simply ages out at its
    900 s timeout, which is what happened to all of them before.
    """
    if not scope or code_interpreter is None:
        return 0
    prefix = f"{scope}{_SEP}"
    stopped = 0
    try:
        sessions = getattr(code_interpreter, "_sessions", None) or {}
        for name in [n for n in list(sessions) if isinstance(n, str) and n.startswith(prefix)]:
            info = sessions.pop(name, None)
            client = getattr(info, "client", None)
            try:
                if client is not None:
                    client.stop()
                    stopped += 1
            except Exception as e:  # noqa: BLE001
                logger.debug("sandbox_scope: stop %s failed: %s", name, e)
        try:
            import agentcore_code_interpreter as _acci  # noqa: PLC0415

            mapping = getattr(_acci, "_session_mapping", None)
            if isinstance(mapping, dict):
                for name in [n for n in list(mapping) if isinstance(n, str) and n.startswith(prefix)]:
                    mapping.pop(name, None)
        except Exception as e:  # noqa: BLE001
            logger.debug("sandbox_scope: mapping cleanup skipped: %s", e)
    except Exception as e:  # noqa: BLE001
        logger.warning("sandbox_scope: release of %s failed: %s", scope, e)
    if stopped:
        logger.info("sandbox_scope: stopped %d sandbox session(s) for %s", stopped, scope)
    return stopped
