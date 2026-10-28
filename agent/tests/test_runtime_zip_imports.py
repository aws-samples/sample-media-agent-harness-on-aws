"""The agent runtime zip must ship every module the runtimes can import.

`infra/lib/agent-infra-stack.ts` bundles every `agent/src` package into the runtime zip EXCEPT the ones in
`lambdaOnlyPackages` (today `ingestion/`, which ships only in the ingestion Lambda zip, so ingestion-only
commits do not redeploy all five runtimes). Tests run against the whole source tree, where every import
resolves -- so a runtime import of a Lambda-only package is invisible until it runs deployed:

* Staging session 47b6a8cf (2026-10-12): `project_search` began importing `ingestion.documents` for the
  document page constants, and the Main Agent failed with `No module named 'ingestion'`.
* `hooks.actions`' Moments image dedup imported `ingestion.generated` lazily inside a try/except, so it
  silently did nothing in every deployed environment since it shipped.

Shared definitions belong in a package both zips carry (`projects/`). This test reads the exclusion set from
the CDK source and walks the import graph out of every runtime entrypoint -- module-level AND function-local
imports, since runtime code runs its lazy imports eventually -- and fails on any path into a Lambda-only
package.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

AGENT = Path(__file__).resolve().parents[1]
SRC = AGENT / "src"
STACK = AGENT.parent / "infra" / "lib" / "agent-infra-stack.ts"

# The AgentCore runtimes' entrypoints (`opentelemetry-instrument <file>` in the stack).
ENTRYPOINTS = ("main.py", "task_agent.py", "planning_agent.py", "render_agent.py")


def _lambda_only_packages() -> set[str]:
    m = re.search(r"lambdaOnlyPackages\s*=\s*new Set\(\[([^\]]*)\]\)", STACK.read_text())
    assert m, "lambdaOnlyPackages not found in agent-infra-stack.ts -- update this test with the stack"
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def _module_file(dotted: str) -> Path | None:
    base = SRC.joinpath(*dotted.split("."))
    if (base / "__init__.py").exists():
        return base / "__init__.py"
    if base.with_suffix(".py").exists():
        return base.with_suffix(".py")
    return None


def _imports(path: Path) -> list[tuple[str, int]]:
    rel = path.relative_to(SRC).with_suffix("")
    pkg = list(rel.parts[:-1]) if rel.name != "__init__" else list(rel.parts[:-1])
    if rel.name == "__init__":
        pkg = list(rel.parts[:-1])
    out: list[tuple[str, int]] = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            out += [(a.name, node.lineno) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = pkg[: len(pkg) - (node.level - 1)] if node.level > 1 else pkg
                mod = ".".join(anchor + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            if mod:
                out.append((mod, node.lineno))
                out += [(f"{mod}.{a.name}", node.lineno) for a in node.names]
    return out


def test_no_runtime_import_path_reaches_a_lambda_only_package():
    lambda_only = _lambda_only_packages()
    assert "ingestion" in lambda_only  # sanity: the parse found the real set
    seen: set[Path] = set()
    frontier: list[tuple[Path, str]] = [(SRC / e, e) for e in ENTRYPOINTS]
    offenders: dict[str, str] = {}
    while frontier:
        path, via = frontier.pop()
        if path in seen:
            continue
        seen.add(path)
        for mod, line in _imports(path):
            root = mod.split(".")[0]
            where = f"{path.relative_to(SRC)}:{line}"
            if root in lambda_only:
                offenders.setdefault(mod, f"{where} (reached from {via})")
                continue
            target = _module_file(mod)
            if target is not None and target not in seen:
                frontier.append((target, via))
    assert not offenders, (
        "a runtime import path reaches a package the runtime zip does not ship (lambdaOnlyPackages); move the "
        "shared code into a package both zips carry (projects/):\n"
        + "\n".join(f"  {m} <- {w}" for m, w in sorted(offenders.items()))
    )


def test_the_runtime_zip_ships_the_data_files_runtime_modules_open():
    """`mcp_manager` opens `project_search/withheld-tool-prefixes.json`; a walker that kept only `.py`/`.txt`
    left it out, so the runtime silently fell back to two built-in prefixes and the prompt-registry withhold
    (00e663d8) never reached the model. The package walker must keep `.json`."""
    src = STACK.read_text()
    walker = src[src.index("const bundleAgentPackage"):src.index("const lambdaOnlyPackages")]
    assert 'entry.name.endsWith(".json")' in walker
    assert (SRC / "project_search" / "withheld-tool-prefixes.json").exists()
