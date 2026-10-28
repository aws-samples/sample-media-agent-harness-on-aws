"""runtime_arns: sub-agent runtime ARNs resolve at CALL time (env first, then SSM by path).

The deploy no longer puts TASK_/PLANNING_/RENDER_AGENT_RUNTIME_ARN in any runtime's env —
that made CloudFormation update the runtimes one after another. They are SSM parameters, and on a
brand-new environment a runtime can start before they exist, so every use site must resolve them
lazily when the import-time constant is empty. These tests pin that contract with a fake SSM
client: no network, no credentials.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import runtime_arns  # noqa: E402

PATH = "/mediaworks-harness-test/runtime-arns"
ARN_VARS = (
    "TASK_AGENT_RUNTIME_ARN",
    "PLANNING_AGENT_RUNTIME_ARN",
    "RENDER_AGENT_RUNTIME_ARN",
)
TASK = "arn:aws:bedrock-agentcore:us-west-2:1:runtime/mediaworks_harness_task_agent-AAA"
PLANNING = "arn:aws:bedrock-agentcore:us-west-2:1:runtime/mediaworks_harness_planning_agent-BBB"
RENDER = "arn:aws:bedrock-agentcore:us-west-2:1:runtime/mediaworks_harness_render_agent-DDD"


class FakeSSM:
    def __init__(self, params: dict[str, str] | None = None, fail: Exception | None = None, page: int = 2):
        self.params = dict(params or {})
        self.fail = fail
        self.page = page
        self.calls: list[dict] = []

    def get_parameters_by_path(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise self.fail
        assert kwargs["Path"] == PATH
        items = [{"Name": f"{PATH}/{k}", "Value": v} for k, v in sorted(self.params.items())]
        start = int(kwargs.get("NextToken") or 0)
        resp = {"Parameters": items[start:start + self.page]}
        if start + self.page < len(items):
            resp["NextToken"] = str(start + self.page)
        return resp


@pytest.fixture
def clean_env(monkeypatch):
    """Start with no ARN vars and no path; undo anything runtime_arns writes into os.environ."""
    for k in (*ARN_VARS, "RUNTIME_ARN_PARAM_PATH"):
        # setenv-then-delenv records the original, so monkeypatch restores it even for keys the
        # code under test sets directly on os.environ.
        monkeypatch.setenv(k, "placeholder")
        monkeypatch.delenv(k)
    runtime_arns._reset_for_tests()
    yield monkeypatch
    runtime_arns._reset_for_tests()


def _install(monkeypatch, fake: FakeSSM) -> FakeSSM:
    monkeypatch.setattr(runtime_arns, "_ssm_client", lambda region: fake)
    return fake


def test_env_wins_without_touching_ssm(clean_env):
    fake = _install(clean_env, FakeSSM({"TASK_AGENT_RUNTIME_ARN": "from-ssm"}))
    clean_env.setenv("RUNTIME_ARN_PARAM_PATH", PATH)
    clean_env.setenv("TASK_AGENT_RUNTIME_ARN", TASK)
    assert runtime_arns.get("TASK_AGENT_RUNTIME_ARN") == TASK
    assert fake.calls == []


def test_no_path_returns_empty_without_ssm(clean_env):
    fake = _install(clean_env, FakeSSM({"TASK_AGENT_RUNTIME_ARN": TASK}))
    assert runtime_arns.get("TASK_AGENT_RUNTIME_ARN") == ""
    assert fake.calls == []


def test_ssm_fallback_reads_path_once_and_populates_env(clean_env):
    fake = _install(clean_env, FakeSSM(
        {"TASK_AGENT_RUNTIME_ARN": TASK, "PLANNING_AGENT_RUNTIME_ARN": PLANNING,
         "RENDER_AGENT_RUNTIME_ARN": RENDER},
        page=2,  # forces pagination (3 params over pages of 2)
    ))
    clean_env.setenv("RUNTIME_ARN_PARAM_PATH", PATH)
    assert runtime_arns.get("RENDER_AGENT_RUNTIME_ARN") == RENDER
    assert len(fake.calls) == 2  # two pages, one logical read
    # Every parameter under the path is now cached in os.environ — no further SSM calls.
    assert os.environ["TASK_AGENT_RUNTIME_ARN"] == TASK
    for name, want in (("TASK_AGENT_RUNTIME_ARN", TASK), ("PLANNING_AGENT_RUNTIME_ARN", PLANNING),
                       ("RENDER_AGENT_RUNTIME_ARN", RENDER)):
        assert runtime_arns.get(name) == want
    assert len(fake.calls) == 2


def test_miss_is_retried_at_most_every_30s(clean_env):
    fake = _install(clean_env, FakeSSM({}))  # parameters not written yet (fresh env)
    clean_env.setenv("RUNTIME_ARN_PARAM_PATH", PATH)
    now = [1000.0]
    clean_env.setattr(runtime_arns.time, "monotonic", lambda: now[0])

    assert runtime_arns.get("TASK_AGENT_RUNTIME_ARN") == ""
    assert len(fake.calls) == 1
    now[0] += 29.0
    assert runtime_arns.get("TASK_AGENT_RUNTIME_ARN") == ""
    assert len(fake.calls) == 1  # inside the window: no SSM call

    fake.params["TASK_AGENT_RUNTIME_ARN"] = TASK  # the deploy writes the parameter
    now[0] += 2.0  # 31 s after the miss
    assert runtime_arns.get("TASK_AGENT_RUNTIME_ARN") == TASK
    assert len(fake.calls) == 2


def test_failure_returns_empty_and_warns_once(clean_env, caplog):
    fake = _install(clean_env, FakeSSM(fail=RuntimeError("AccessDenied")))
    clean_env.setenv("RUNTIME_ARN_PARAM_PATH", PATH)
    now = [0.0]
    clean_env.setattr(runtime_arns.time, "monotonic", lambda: now[0])
    with caplog.at_level("WARNING", logger="runtime_arns"):
        assert runtime_arns.get("TASK_AGENT_RUNTIME_ARN") == ""
        now[0] += 31.0
        assert runtime_arns.get("TASK_AGENT_RUNTIME_ARN") == ""
    assert len(fake.calls) == 2
    assert len([r for r in caplog.records if r.name == "runtime_arns"]) == 1


def test_client_construction_failure_returns_empty(clean_env):
    def _boom(region):
        raise RuntimeError("no boto3 creds")

    clean_env.setattr(runtime_arns, "_ssm_client", _boom)
    clean_env.setenv("RUNTIME_ARN_PARAM_PATH", PATH)
    assert runtime_arns.get("TASK_AGENT_RUNTIME_ARN") == ""


# --- use sites resolve lazily when the import-time constant is empty --------------------------


def test_dispatch_tool_resolves_task_arn_lazily(clean_env):
    from unittest.mock import MagicMock, patch

    from orchestration import dispatch_tool

    _install(clean_env, FakeSSM({"TASK_AGENT_RUNTIME_ARN": TASK, "RENDER_AGENT_RUNTIME_ARN": RENDER}))
    clean_env.setattr(dispatch_tool, "TASK_AGENT_ARN", "")  # runtime started before the param existed
    clean_env.setattr(dispatch_tool, "RENDER_AGENT_ARN", "")
    clean_env.setenv("RUNTIME_ARN_PARAM_PATH", PATH)

    client = MagicMock()
    client.invoke_agent_runtime.return_value = {"response": b"data: ok"}
    with patch.object(dispatch_tool, "_get_client", return_value=client):
        result = dispatch_tool.dispatch_tool({"toolUseId": "t1", "input": {"prompt": "go"}})
        assert result["status"] == "success"
        assert client.invoke_agent_runtime.call_args[1]["agentRuntimeArn"] == TASK

        dispatch_tool.dispatch_tool({"toolUseId": "t2", "input": {"prompt": "go", "target": "render"}})
        assert client.invoke_agent_runtime.call_args[1]["agentRuntimeArn"] == RENDER


def test_dispatch_tool_still_reports_unconfigured(clean_env):
    from orchestration import dispatch_tool

    _install(clean_env, FakeSSM({}))
    clean_env.setattr(dispatch_tool, "TASK_AGENT_ARN", "")
    clean_env.setattr(dispatch_tool, "RENDER_AGENT_ARN", "")
    clean_env.setenv("RUNTIME_ARN_PARAM_PATH", PATH)
    result = dispatch_tool.dispatch_tool({"toolUseId": "t1", "input": {"prompt": "go"}})
    assert result["status"] == "error"
    assert "No target runtime ARN configured" in result["content"][0]["text"]


def test_plan_and_execute_resolves_planning_and_task_arns_lazily(clean_env):
    from orchestration import plan_and_execute_tool as pae

    _install(clean_env, FakeSSM({"PLANNING_AGENT_RUNTIME_ARN": PLANNING, "TASK_AGENT_RUNTIME_ARN": TASK}))
    clean_env.setattr(pae, "PLANNING_AGENT_ARN", "")
    clean_env.setattr(pae, "TASK_AGENT_ARN", "")
    clean_env.setenv("RUNTIME_ARN_PARAM_PATH", PATH)

    high_level_plan = {
        "execution_mode": "direct",
        "sub_goals": [],
        "suggested_tools": {},
        "suggested_skills": {},
        "reasoning": "single-shot task",
        "direct_dispatch_params": {"prompt": "do the thing", "tools": [], "skills": []},
    }
    invoked: list[str] = []

    def fake_invoke_runtime(arn, payload):
        invoked.append(arn)
        if arn == PLANNING:
            return json.dumps(high_level_plan), {"inputTokens": 1, "outputTokens": 2}
        return "task result body", {"inputTokens": 3, "outputTokens": 4}

    clean_env.setattr(pae, "_invoke_runtime", fake_invoke_runtime)
    result = pae.plan_and_execute({"toolUseId": "tu-1", "input": {"goal": "do the thing"}})
    assert invoked == [PLANNING, TASK]
    assert "task result body" in result["content"][0]["text"]


def test_plan_and_execute_unconfigured_planning_is_an_error(clean_env):
    from orchestration import plan_and_execute_tool as pae

    _install(clean_env, FakeSSM({}))
    clean_env.setattr(pae, "PLANNING_AGENT_ARN", "")
    clean_env.setenv("RUNTIME_ARN_PARAM_PATH", PATH)
    result = pae.plan_and_execute({"toolUseId": "tu-1", "input": {"goal": "x"}})
    assert result["status"] == "error"
    assert "PLANNING_AGENT_RUNTIME_ARN not configured" in result["content"][0]["text"]


def test_render_artifacts_resolves_lazily(clean_env):
    from render import artifacts as render_artifacts

    _install(clean_env, FakeSSM({"RENDER_AGENT_RUNTIME_ARN": RENDER}))
    clean_env.setattr(render_artifacts, "RENDER_AGENT_ARN", "")
    clean_env.setenv("RUNTIME_ARN_PARAM_PATH", PATH)
    assert render_artifacts._render_agent_arn() == RENDER


def test_builtin_runtime_state_resolves_lazily(clean_env):
    from capability import sources

    _install(clean_env, FakeSSM({"RENDER_AGENT_RUNTIME_ARN": RENDER}))
    clean_env.setenv("RUNTIME_ARN_PARAM_PATH", PATH)
    state = sources._builtin_runtime_state()
    assert state["render-agent"] == (True, RENDER.rsplit("/", 1)[-1])
    assert state["task-agent"] == (False, "")
