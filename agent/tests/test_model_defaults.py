"""agent/src/model/models.json is the ONE place the harness's default models are set.

The task agent once ran Opus 5 while its code said Sonnet 5.5: the deploy put DEFAULT_MODEL_ID in every runtime's
config, which overrode the code. These tests keep a model id from being set anywhere else again.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "agent" / "src"
MODELS = json.loads((SRC / "model" / "models.json").read_text())

# A full inference-profile id in quotes: what a default looks like. Capability tables match bare families
# ("claude-sonnet-5") or unprefixed ids, which this does not catch.
PROFILE_LITERAL = re.compile(r"""["'](?:global|us|eu|apac)\.anthropic\.claude-[a-z0-9.:-]+["']""")


def test_models_json_names_every_agent_and_an_effort():
    assert set(MODELS["agents"]) == {"orchestrator", "task", "planning", "render", "lab_chat", "improve_judge"}
    for agent, mid in MODELS["agents"].items():
        assert mid.startswith("global.anthropic.claude-"), (agent, mid)
    assert MODELS["reasoning_effort"] in {"low", "medium", "high"}
    assert MODELS["thinking_prefix_mismatch"] in {"drop_block", "error", "off"}


def test_the_agents_use_it():
    import main
    import planning_agent
    import render_agent
    import task_agent
    from improve import judge
    from model.defaults import model_for

    assert main.DEFAULT_ORCHESTRATOR_MODEL_ID == model_for("orchestrator")
    assert task_agent.DEFAULT_MODEL_ID == model_for("task")
    assert planning_agent.DEFAULT_MODEL_ID == model_for("planning")
    assert render_agent.RENDER_MODEL_ID == model_for("render")
    assert judge.DEFAULT_MODEL == model_for("improve_judge")


def test_no_environment_variable_overrides_a_default(monkeypatch):
    import importlib

    import task_agent
    for name in ("DEFAULT_MODEL_ID", "PLANNING_MODEL_ID", "RENDER_MODEL_ID", "CHAT_MODEL_ID"):
        monkeypatch.setenv(name, "global.anthropic.claude-haiku-4-5")
    from model import defaults
    importlib.reload(defaults)
    assert defaults.model_for("task") == MODELS["agents"]["task"]
    assert task_agent.DEFAULT_MODEL_ID == MODELS["agents"]["task"]


def test_no_model_id_is_hard_coded_anywhere_else():
    roots = [SRC, ROOT / "infra" / "lib", ROOT / "infra" / "bin", ROOT / "ui" / "server"]
    hits = []
    for root in roots:
        for p in root.rglob("*"):
            if p.suffix not in {".py", ".ts"} or "node_modules" in p.parts or "botocore_models" in p.parts:
                continue
            if p.name.startswith("test_") or ".test." in p.name or "tests" in p.parts:
                continue
            for n, line in enumerate(p.read_text(errors="ignore").splitlines(), 1):
                if PROFILE_LITERAL.search(line):
                    hits.append(f"{p.relative_to(ROOT)}:{n}: {line.strip()}")
    assert hits == [], "set models in agent/src/model/models.json, not here:\n" + "\n".join(hits)


def test_the_deploy_reads_the_same_file():
    stack = (ROOT / "infra" / "lib" / "agent-infra-stack.ts").read_text()
    assert "agent/src/model/models.json" in stack
    assert "modelDefaults.reasoning_effort" in stack and 'tryGetContext("defaultModelId")' not in stack
    assert 'tryGetContext("reasoningEffort")' not in stack
