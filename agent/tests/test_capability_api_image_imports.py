"""The Capability API image must ship every module its code can import.

The Hooks tab broke on exactly this. Flipping `hook` to `canonical` routed
``ListHooks`` to the Capability API; ``lab/service.py`` resolves the built-in specs
with ``from hooks.defaults import default_hook_specs`` *inside* the handler; and
`capability.Dockerfile` copied only `lab`, `capability` and `capability_api`. The
tab rendered ``No module named 'hooks'`` — carried on an HTTP **200**, because the
control-plane lab route mirrors the runtime's envelope and reports failures in the
body, so nothing looked like an outage.

`model_catalog` was the same bug one step from surfacing: ``_validate_model``
returns early when the model is ``None``, so it only fires when an agent is saved
with an EXPLICIT model.

A lazy import is invisible to every other check we have — the module loads fine in
tests (which run against the whole source tree) and fine on the agent runtime
(whose zip carries everything). Only the image is short, and only for the one
handler that reaches it. So this test reads the Dockerfile's COPY list and the
import graph and compares them.

Deliberately AST-based rather than regex: the imports that matter are nested
inside functions, which is precisely why they were missed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

AGENT_ROOT = Path(__file__).resolve().parents[1]
SRC = AGENT_ROOT / "src"
DOCKERFILE = AGENT_ROOT / "capability.Dockerfile"

#: Packages whose code runs inside the Capability API.
CONTROL_PLANE_PACKAGES = ("lab", "capability", "capability_api")

#: Local modules the image may omit, each because ONLY a `RUNTIME_ONLY_ACTIONS`
#: entry can reach it — the service refuses those before dispatch, so the absence
#: is a second line of defence rather than a gap. Keyed to the action so a future
#: reclassification has to revisit this entry.
RUNTIME_ONLY_DEPS = {
    "skills_manager": "RunHarness (lab/test_harness.py)",
    "dag_schema": "RunHarness (lab/test_harness.py)",
    "custom_agent_resolver": "ResolveAgentConfig (lab/service.py)",
}


def _shipped_modules() -> set[str]:
    """Top-level module names the Dockerfile copies under `src/`."""
    shipped: set[str] = set()
    for line in DOCKERFILE.read_text().splitlines():
        stripped = line.strip()
        if not stripped.startswith("COPY "):
            continue
        parts = stripped.split()
        if len(parts) < 3:
            continue
        source = parts[1]
        if not source.startswith("src/"):
            continue
        name = source[len("src/") :].strip("/")
        # `src/model_catalog.py` ships the module `model_catalog`.
        shipped.add(name[: -len(".py")] if name.endswith(".py") else name)
    return shipped


def _local_module_names() -> set[str]:
    """Everything importable as a top-level module from `agent/src`."""
    names = {p.stem for p in SRC.glob("*.py") if p.stem != "__init__"}
    names |= {p.name for p in SRC.iterdir() if p.is_dir() and (p / "__init__.py").exists()}
    return names


def _imported_local_modules() -> dict[str, set[str]]:
    """Local top-level modules imported by control-plane code → where.

    Walks the full AST, so an import nested in a function or a `try:` block counts
    the same as one at the top of the file.
    """
    local = _local_module_names()
    found: dict[str, set[str]] = {}
    for package in CONTROL_PLANE_PACKAGES:
        for path in (SRC / package).rglob("*.py"):
            if "test" in path.name:
                continue  # test modules do not ship in the image
            try:
                tree = ast.parse(path.read_text(), filename=str(path))
            except SyntaxError:  # pragma: no cover - would fail the build elsewhere
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    # `level > 0` is a relative import — same package, always shipped.
                    if node.level or not node.module:
                        continue
                    roots = [node.module.split(".")[0]]
                else:
                    continue
                for root in roots:
                    if root in local and root not in CONTROL_PLANE_PACKAGES:
                        found.setdefault(root, set()).add(
                            f"{path.relative_to(SRC)}:{node.lineno}"
                        )
    return found


def test_every_imported_local_module_is_shipped_or_runtime_only() -> None:
    shipped = _shipped_modules()
    imported = _imported_local_modules()

    missing = {
        module: sorted(sites)
        for module, sites in imported.items()
        if module not in shipped and module not in RUNTIME_ONLY_DEPS
    }

    assert not missing, (
        "capability.Dockerfile does not ship these modules, and they are not "
        "recorded as runtime-only:\n"
        + "\n".join(f"  {m} imported at {', '.join(s)}" for m, s in missing.items())
        + "\n\nAdd a COPY line, or add the module to RUNTIME_ONLY_DEPS naming the "
        "RUNTIME_ONLY_ACTIONS entry that is its only caller."
    )


def test_the_modules_the_hooks_tab_needs_are_shipped() -> None:
    """The two that were actually missing, named so a regression is unambiguous."""
    shipped = _shipped_modules()
    assert "hooks" in shipped, "ListHooks / ValidateHookCondition / Create+UpdateHook"
    assert "model" in shipped, "Create+UpdateSpecialtyAgentDraft with a model (model.catalog / model.factory)"


@pytest.mark.parametrize("module", sorted(RUNTIME_ONLY_DEPS))
def test_runtime_only_deps_stay_out_of_the_image(module: str) -> None:
    """Their absence is load-bearing, so shipping one should need a decision.

    If an action that needs `module` is reclassified as control-plane, this test
    fails and points at the COPY list — rather than the action failing in
    production with a `ModuleNotFoundError` on an HTTP 200.
    """
    assert module not in _shipped_modules(), (
        f"{module} is now shipped, but RUNTIME_ONLY_DEPS says only "
        f"{RUNTIME_ONLY_DEPS[module]} needs it. If that changed, move it out of "
        "RUNTIME_ONLY_DEPS deliberately."
    )


def test_control_plane_imports_nothing_from_the_runtime_stack() -> None:
    """The original rule the COPY list exists to keep.

    `main.py`, `task_agent.py`, `mcp_manager` and the `tools` package pull the
    Strands/MCP stack, which this image must not carry. Checked separately from the
    shipped-vs-imported test above so widening the COPY list can never quietly
    admit one of them.
    """
    forbidden = {"main", "task_agent", "mcp_manager", "tools", "render_agent",
                 "planning_agent", "mcp_server"}
    imported = _imported_local_modules()
    offenders = {m: sorted(s) for m, s in imported.items() if m in forbidden}
    assert not offenders, (
        "control-plane code imports the runtime stack:\n"
        + "\n".join(f"  {m} at {', '.join(s)}" for m, s in offenders.items())
    )


def _module_file(dotted: str) -> Path | None:
    """`agent/src` file that the dotted module name resolves to, if it is local."""
    base = SRC.joinpath(*dotted.split("."))
    if (base / "__init__.py").exists():
        return base / "__init__.py"
    if base.with_suffix(".py").exists():
        return base.with_suffix(".py")
    return None


def _dotted_of(path: Path) -> str:
    rel = path.relative_to(SRC).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports_of(path: Path, *, module_level_only: bool) -> list[tuple[str, int]]:
    """Dotted local targets imported by `path` (relative imports resolved).

    `module_level_only` skips anything inside a function body: those run only when
    that function does, whereas a module-level import runs the moment ANYTHING
    imports the file — which is how a one-constant import of `hooks.loader` dragged
    `hooks.provider` → `render` into the image's ListHooks.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    pkg = _dotted_of(path).split(".")
    if path.name != "__init__.py":
        pkg = pkg[:-1]

    def walk(node, in_def):
        for child in ast.iter_child_nodes(node):
            is_def = isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            if isinstance(child, ast.Import) and not (module_level_only and in_def):
                for a in child.names:
                    yield a.name, child.lineno
            elif isinstance(child, ast.ImportFrom) and not (module_level_only and in_def):
                if child.level:
                    anchor = pkg[: len(pkg) - (child.level - 1)] if child.level > 1 else pkg
                    mod = ".".join(anchor + ([child.module] if child.module else []))
                else:
                    mod = child.module or ""
                yield mod, child.lineno
                for a in child.names:  # `from pkg import submodule`
                    yield f"{mod}.{a.name}", child.lineno
            yield from walk(child, in_def or is_def)

    return [(m, n) for m, n in walk(tree, False) if m and _module_file(m)]


def test_module_level_imports_reached_from_the_control_plane_are_shipped() -> None:
    """Follow the import graph OUT of the control-plane packages, transitively.

    The first test only looks at what `lab`/`capability`/`capability_api` import
    directly. That missed ListHooks: it lazily imported `hooks.loader` (shipped) for
    one constant, `hooks.loader` imports `.provider` at module level, and
    `hooks.provider` imports `render.types` at module level — `render` is not in the
    image. Every Hooks-tab load in staging and prod was a `No module named 'render'`
    on an HTTP 200. Here a reached file's MODULE-LEVEL imports are followed (they
    execute on import); its function-local ones are not (they execute only if the
    control plane calls that function, which the direct test covers for its own code).
    """
    shipped = _shipped_modules()
    seen: set[str] = set()
    frontier: list[tuple[str, str]] = []
    for package in CONTROL_PLANE_PACKAGES:
        for path in (SRC / package).rglob("*.py"):
            if "test" in path.name:
                continue
            for mod, line in _imports_of(path, module_level_only=False):
                frontier.append((mod, f"{path.relative_to(SRC)}:{line}"))

    missing: dict[str, str] = {}
    while frontier:
        mod, via = frontier.pop()
        if mod in seen:
            continue
        seen.add(mod)
        root = mod.split(".")[0]
        if root in RUNTIME_ONLY_DEPS:
            continue
        if root not in shipped:
            missing.setdefault(mod, via)
            continue
        path = _module_file(mod)
        for child, line in _imports_of(path, module_level_only=True):
            frontier.append((child, f"{path.relative_to(SRC)}:{line} (via {via})"))

    assert not missing, (
        "the Capability API image would raise ModuleNotFoundError — these local "
        "modules are imported at module level along a control-plane import chain, "
        "but capability.Dockerfile does not ship them:\n"
        + "\n".join(f"  {m} <- {v}" for m, v in sorted(missing.items()))
    )
