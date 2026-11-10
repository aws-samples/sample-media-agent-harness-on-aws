"""No agent ever binds a HOST-EXEC tool.

Security review (2026-11, critical): the Main Agent and the Task Agent bound strands-agents-tools'
``shell``, ``file_read`` and ``load_tool`` on every turn, with ``BYPASS_TOOL_CONSENT=true``. They run
IN the runtime process, with the runtime role: any user -- or any prompt injection in a fetched page
or a tool result -- could run commands that read every user's data, call ``kms:Sign`` on the
delegation key, or print the ``RuntimeConfigSecret`` values hydrated into ``os.environ``. Code runs
in the Code Interpreter sandbox (its own role, scoped credentials), never on the runtime host.

These tests fail if one comes back by ANY route: a registration table, a ``@tool`` of that name, a
module-style ``TOOL_SPEC``, the strands-agents-tools package, or the live builtin tables of ``main``
and ``task_agent``.
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path


AGENT = Path(__file__).resolve().parents[1]
SRC = AGENT / "src"
sys.path.insert(0, str(SRC))

#: Tools that execute on, read from or write to the RUNTIME host, or call AWS as the runtime role.
HOST_EXEC = frozenset({
    "shell", "file_read", "file_write", "load_tool", "python_repl", "use_aws", "editor",
})


def _src_files():
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts or path.name.startswith("test_"):
            continue
        yield path


def _is_tool_decorator(dec: ast.expr) -> bool:
    target = dec.func if isinstance(dec, ast.Call) else dec
    return (isinstance(target, ast.Name) and target.id == "tool") or (
        isinstance(target, ast.Attribute) and target.attr == "tool")


def _tool_name_kw(dec: ast.expr):
    if isinstance(dec, ast.Call):
        for kw in dec.keywords:
            if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                return kw.value.value
    return None


def _violations(path: Path, root: Path = AGENT) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    rel = path.relative_to(root)
    out: list[str] = []
    for node in ast.walk(tree):
        # 1. the package that ships them
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "strands_tools":
            out.append(f"{rel}:{node.lineno} imports {node.module}")
        if isinstance(node, ast.Import):
            out += [f"{rel}:{node.lineno} imports {a.name}" for a in node.names
                    if a.name.split(".")[0] == "strands_tools"]
        # 2. a @tool of that name
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if _is_tool_decorator(dec) and (_tool_name_kw(dec) or node.name) in HOST_EXEC:
                    out.append(f"{rel}:{node.lineno} defines tool {_tool_name_kw(dec) or node.name!r}")
        if isinstance(node, ast.Call) and _is_tool_decorator(node) and _tool_name_kw(node) in HOST_EXEC:
            out.append(f"{rel}:{node.lineno} names a tool {_tool_name_kw(node)!r}")
        if isinstance(node, ast.Dict):
            keys = {k.value: v for k, v in zip(node.keys, node.values) if isinstance(k, ast.Constant)}
            # 3. a module-style TOOL_SPEC of that name
            name = keys.get("name")
            if "inputSchema" in keys and isinstance(name, ast.Constant) and name.value in HOST_EXEC:
                out.append(f"{rel}:{node.lineno} declares a TOOL_SPEC {name.value!r}")
            # 4. a registration table entry ({"shell": shell}); a label string value is fine
            for key, value in keys.items():
                if key in HOST_EXEC and not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                    out.append(f"{rel}:{node.lineno} registers {key!r}")
        # 5. table["shell"] = ...
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if (isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
                        and t.slice.value in HOST_EXEC):
                    out.append(f"{rel}:{node.lineno} registers {t.slice.value!r}")
    return out


def test_no_source_module_defines_imports_or_registers_a_host_exec_tool():
    found = [v for p in _src_files() for v in _violations(p)]
    assert not found, "host-exec tool bound in agent/src:\n" + "\n".join(found)


def test_the_scan_catches_each_route(tmp_path):
    """The scanner itself: every way such a tool came (or could come) in is flagged."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from strands_tools import shell\n"
        "import strands_tools.file_read\n"
        "from strands import tool\n"
        "@tool\ndef python_repl(code: str) -> str: ...\n"
        "@tool(name='use_aws')\ndef aws(x: str) -> str: ...\n"
        "TOOL_SPEC = {'name': 'editor', 'inputSchema': {}}\n"
        "T = {'file_read': file_read}\nT['load_tool'] = object()\n"
        "LABELS = {'file_read': 'Reading file...'}\n"  # a label, not a tool: allowed
    )
    found = _violations(bad, root=tmp_path)
    assert len(found) == 8, found
    for name in ("strands_tools", "python_repl", "use_aws", "editor", "file_read", "load_tool"):
        assert any(name in v for v in found), (name, found)


def _host_exec_in(tools: dict) -> set[str]:
    names = set(tools)
    for t in tools.values():
        spec = getattr(t, "tool_spec", None)
        if isinstance(spec, dict):
            names.add(spec.get("name"))
        names.add(getattr(t, "tool_name", None))
    return names & HOST_EXEC


def test_main_agent_builtin_table_has_no_host_exec_tool(monkeypatch):
    import main

    monkeypatch.setattr(main, "_all_builtin_tools", {})
    monkeypatch.setattr(main, "_added_builtins", set())
    monkeypatch.setattr(main, "_trusted_builtins", set())
    monkeypatch.setattr(main, "code_interpreter", None)
    main._register_builtin_tools()
    assert main._all_builtin_tools, "the builtin table was not filled"
    assert not _host_exec_in(main._all_builtin_tools)
    assert not (main._added_builtins | main._trusted_builtins) & HOST_EXEC
    # the replacements the model still sees
    assert {"http_request", "current_time"} <= set(main._all_builtin_tools)


def test_task_agent_builtin_table_has_no_host_exec_tool():
    import task_agent

    assert task_agent._BUILTINS, "the builtin table was not filled"
    assert not _host_exec_in(task_agent._BUILTINS)
    assert {"http_request", "current_time"} <= set(task_agent._BUILTINS)


def test_strands_agents_tools_is_not_a_dependency():
    files = [AGENT / "pyproject.toml", AGENT / "requirements.txt", AGENT / "src" / "requirements.txt",
             AGENT / "uv.lock"]
    for f in files:
        body = f.read_text(encoding="utf-8")
        reqs = [ln for ln in body.splitlines() if not ln.lstrip().startswith("#")]
        assert not any("strands-agents-tools" in ln or "strands_agents_tools" in ln for ln in reqs), f


def test_strands_tools_is_not_installed():
    """A stale environment would let a stray import keep working locally while the deploy breaks."""
    assert importlib.util.find_spec("strands_tools") is None
