"""Plan and design in ONE Planning Agent call, with a Planning-agent self-retry (no Design Agent).

Staging plan 81d9198e: planning 36.7s (5,054 output tokens) then design 28.6s (4,182 tokens) — two
runtime invokes, two times-to-first-token, the catalog read twice. plan_and_execute now asks the
planning runtime for {"high_level_plan", "dag"} at once. Fusion is unconditional: the Planning call
is the ONLY producer of a DAG. The fused DAG goes through the same expansion, normalisation and
validation as before; when it is missing, unparseable or invalid the SAME fused Planning call is
re-invoked with the validation errors fed back for correction (bounded to PLAN_DAG_MAX_ATTEMPTS),
and exhausting the attempts fails cleanly — there is no Design Agent fallback.
"""
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from orchestration import plan_and_execute_tool as pae  # noqa: E402

SUBMIT = "fal___submit_job"
CATALOG = {"tools": [{"name": SUBMIT, "description": "submit"},
                     {"name": "fal___check_job", "description": "status"},
                     {"name": "fal___get_job_result", "description": "result"}],
           "gateway_tools": [], "skills": [], "models": []}

HLP = {"execution_mode": "standard", "reasoning": "fan out",
       "sub_goals": [{"id": "sg1", "description": "restyle shots", "reasoning": "-",
                      "suggested_tools": [SUBMIT], "depends_on": []}],
       "suggested_tools": {"sg1": [SUBMIT]}, "suggested_skills": {}}

DAG = {"plan_id": "plan-anime", "goal": "restyle", "nodes": [{
    "id": "restyle", "kind": "tool", "depends_on": [], "output_key": "clip", "output_type": "json",
    "tools": [SUBMIT], "prompt": "Restyle {{shot.url}}",
    "config": {"tool": SUBMIT, "arguments": {"endpoint_id": "fal-ai/x", "input": {"image_url": "{{shot.url}}"}}},
    "for_each": {"var": "shot", "items": [{"id": f"shot{i}", "url": f"https://c/{i}.png"} for i in range(1, 9)]},
}]}

# A second, valid DAG a correction retry can return (distinct plan_id so a test can prove it was used).
CORRECTED = {"plan_id": "plan-corrected", "goal": "restyle", "nodes": [{
    "id": "t1", "depends_on": [], "output_key": "o", "output_type": "json",
    "tools": [SUBMIT], "prompt": "restyle"}]}

BAD_DAG = {"plan_id": "p", "goal": "g", "nodes": [{
    "id": "t1", "depends_on": ["nope"], "output_key": "o", "output_type": "json",
    "tools": [SUBMIT], "prompt": "x"}]}


@pytest.fixture
def env(monkeypatch):
    calls = {"planning": [], "dispatched": []}
    monkeypatch.setattr(pae, "PLANNING_AGENT_ARN", "planning-arn")
    monkeypatch.setattr(pae, "TASK_AGENT_ARN", "task-arn")
    monkeypatch.setattr(pae, "PLAN_DAG_MAX_ATTEMPTS", 2)
    monkeypatch.setitem(sys.modules, "main", types.SimpleNamespace(get_catalog=lambda: CATALOG))
    # state["planning_replies"] is a queue of fused responses; each planning invoke pops the next
    # (the last one repeats once the queue is drained).
    state = {"planning_replies": []}

    def fake_invoke(arn, payload):
        if arn == "planning-arn":
            calls["planning"].append(payload)
            replies = state["planning_replies"]
            reply = replies.pop(0) if len(replies) > 1 else replies[0]
            return json.dumps(reply), {"inputTokens": 1, "outputTokens": 1}
        return "direct result", {"inputTokens": 1, "outputTokens": 1}

    monkeypatch.setattr(pae, "_invoke_runtime", fake_invoke)

    def fake_dispatch(**kw):
        calls["dispatched"].append(kw["plan"])
        return {"toolUseId": kw["tool_use_id"], "status": "success", "content": [{"text": "ok"}]}

    monkeypatch.setattr(pae, "_dispatch_plan_async", fake_dispatch)
    return calls, state


def _run():
    return pae.plan_and_execute({"toolUseId": "tu", "input": {"goal": "restyle 8 shots"}})


def test_a_valid_fused_dag_runs_with_one_planning_call(env):
    calls, state = env
    state["planning_replies"] = [{"high_level_plan": HLP, "dag": DAG}]
    _run()
    assert calls["planning"][0]["mode"] == "plan_and_design"
    assert len(calls["planning"]) == 1, "a valid fused DAG needs no self-retry"
    plan = calls["dispatched"][0]
    assert [n.id for n in plan.nodes] == [f"restyle__shot{i}" for i in range(1, 9)]
    # Same normalisation as before: the async trio is granted to every submit node.
    assert all({"fal___check_job", "fal___get_job_result"} <= set(n.tools) for n in plan.nodes)


def test_fusion_is_unconditional(env):
    """There is no off switch any more — every Planning call asks for the fused response."""
    calls, state = env
    state["planning_replies"] = [{"high_level_plan": HLP, "dag": DAG}]
    _run()
    assert calls["planning"][0]["mode"] == "plan_and_design"
    assert not hasattr(pae, "_fused_design_enabled")


@pytest.mark.parametrize("bad_dag", [
    None,                                                                   # planner returned no DAG
    {"plan_id": "p", "goal": "g", "nodes": [{"id": "x", "for_each": {"var": "s", "items": []}}]},
    {"plan_id": "p", "goal": "g", "nodes": [{"id": "x", "prompt": "p"}]},   # unparseable node
    BAD_DAG,                                                                # fails validation
])
def test_a_bad_fused_dag_triggers_the_planning_self_retry(env, bad_dag):
    calls, state = env
    # First fused reply is bad; the retry returns a valid DAG.
    state["planning_replies"] = [{"high_level_plan": HLP, "dag": bad_dag},
                                 {"high_level_plan": HLP, "dag": CORRECTED}]
    _run()
    assert len(calls["planning"]) == 2, "a bad DAG must re-invoke the fused Planning call"
    # The retry carries the validation errors back for correction, on the SAME planning runtime.
    assert "validation_errors" in calls["planning"][1]
    assert calls["planning"][1]["mode"] == "plan_and_design"
    assert calls["dispatched"][0].plan_id == "plan-corrected"


def test_exhausting_the_retries_fails_cleanly_with_the_validation_problem(env):
    calls, state = env
    # Every reply is invalid — the self-retry can never succeed.
    state["planning_replies"] = [{"high_level_plan": HLP, "dag": BAD_DAG}]
    result = _run()
    # Two attempts (PLAN_DAG_MAX_ATTEMPTS), then a clean failure — never a dispatch.
    assert len(calls["planning"]) == 2
    assert calls["dispatched"] == []
    assert result["status"] == "error"
    text = result["content"][0]["text"]
    assert "validation failed" in text.lower()
    # It names the validation problem (the dangling dependency), not a Design Agent.
    assert "design agent" not in text.lower()


def test_fused_direct_mode_dispatches_directly(env):
    calls, state = env
    direct = {**HLP, "execution_mode": "direct",
              "direct_dispatch_params": {"prompt": "do it", "tools": [], "skills": []}}
    state["planning_replies"] = [{"high_level_plan": direct, "dag": None}]
    result = _run()
    assert result["executionMode"] == "direct"
    assert calls["dispatched"] == []
    assert len(calls["planning"]) == 1


def test_a_bare_high_level_plan_from_an_older_planning_runtime_self_retries(env):
    """A planning runtime predating the mode returns a bare HLP (no DAG) — self-retry supplies one."""
    calls, state = env
    state["planning_replies"] = [HLP, {"high_level_plan": HLP, "dag": CORRECTED}]
    _run()
    assert len(calls["planning"]) == 2
    assert calls["dispatched"][0].plan_id == "plan-corrected"


def test_inherited_skills_are_stored_as_names_only(env, monkeypatch):
    """The executor resolves plan-node skills by NAME; bodies made 81d9198e's nodes_json 1.73 MB."""
    calls, state = env
    import jobs.context as jc

    body = "x" * 24_000
    monkeypatch.setattr(jc, "current_active_skills",
                        lambda: [{"name": "media-references", "instructions": body},
                                 {"name": "pdf", "instructions": body}])
    state["planning_replies"] = [{"high_level_plan": HLP, "dag": DAG}]
    _run()
    plan = calls["dispatched"][0]
    assert all(n.skills == [{"name": "media-references"}] for n in plan.nodes)
    assert len(json.dumps([n.to_dict() for n in plan.nodes])) < 20_000


def test_planning_runtime_keeps_output_json_a_high_level_plan(monkeypatch):
    """persist_workflow parses planning/output.json as a HighLevelPlan; a fused reply must not break it."""
    import asyncio

    import boto3
    from moto import mock_aws

    import planning_agent
    from planning.high_level_plan import HighLevelPlan

    for k, v in (("AWS_ACCESS_KEY_ID", "t"), ("AWS_SECRET_ACCESS_KEY", "t"),
                 ("AWS_DEFAULT_REGION", "us-west-2")):
        monkeypatch.setenv(k, v)
    fused_text = json.dumps({"high_level_plan": HLP, "dag": DAG})
    seen = {}

    class _StubAgent:
        def __init__(self, **kw):
            seen["system_prompt"] = kw["system_prompt"]

        async def stream_async(self, prompt):
            seen["prompt"] = prompt
            yield {"data": fused_text}

    monkeypatch.setattr(planning_agent, "Agent", _StubAgent)
    monkeypatch.setattr(planning_agent, "build_model", lambda **kw: object())
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-west-2")
        s3.create_bucket(Bucket="plan-bkt", CreateBucketConfiguration={"LocationConstraint": "us-west-2"})
        boto3.resource("dynamodb", region_name="us-west-2").create_table(
            TableName="ps", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"},
                       {"AttributeName": "node_id", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"},
                                  {"AttributeName": "node_id", "AttributeType": "S"}])

        async def go():
            return [e async for e in planning_agent.invoke(
                {"goal": "g", "catalog": CATALOG, "session_id": "plan-session-1-abc", "s3_bucket": "plan-bkt",
                 "ddb_table": "ps", "mode": "plan_and_design"}, None)]

        events = asyncio.run(go())
        ref = next(json.loads(e) for e in events if '"output_ref"' in e)
        assert ref["s3_key"] == "plans/plan-session-1-abc/planning/fused.json"
        body = s3.get_object(Bucket="plan-bkt", Key=ref["s3_key"])["Body"].read().decode()
        assert json.loads(body)["dag"] == DAG
        hlp = s3.get_object(Bucket="plan-bkt", Key="plans/plan-session-1-abc/planning/output.json")["Body"].read()
        assert HighLevelPlan.from_json(hlp.decode()).execution_mode == "standard"
    assert "PLAN AND DESIGN IN ONE RESPONSE" in seen["system_prompt"]
    assert '"dag"' in seen["prompt"]


def test_planning_retry_prompt_carries_the_validation_errors(monkeypatch):
    """The fused self-retry reuses the planning agent's correction prompt: errors + framing appended."""
    import asyncio

    import planning_agent

    for k, v in (("AWS_ACCESS_KEY_ID", "t"), ("AWS_SECRET_ACCESS_KEY", "t"),
                 ("AWS_DEFAULT_REGION", "us-west-2")):
        monkeypatch.setenv(k, v)
    seen = {}

    class _StubAgent:
        def __init__(self, **kw):
            pass

        async def stream_async(self, prompt):
            seen["prompt"] = prompt
            yield {"data": "{}"}

    monkeypatch.setattr(planning_agent, "Agent", _StubAgent)
    monkeypatch.setattr(planning_agent, "build_model", lambda **kw: object())

    errors = "- unknown_dependency: node t1 depends on 'nope'"

    async def go():
        return [e async for e in planning_agent.invoke(
            {"goal": "g", "catalog": CATALOG, "mode": "plan_and_design",
             "validation_errors": errors}, None)]

    asyncio.run(go())
    assert "failed validation with these errors" in seen["prompt"]
    assert errors in seen["prompt"]
    assert "Produce a corrected response that fixes them." in seen["prompt"]
