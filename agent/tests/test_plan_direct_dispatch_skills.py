"""A ``direct`` plan whose planner names its skills as bare strings must still dispatch.

Staging 2026-10-09 (session 9d649e32, plan-session-1791548896): the Planning Agent returned
``direct_dispatch_params.skills: ["video-processing"]``. ``DirectDispatchParams.from_dict`` ran
``dict(s)`` on each entry, so every such plan died with ``ValueError: dictionary update sequence
element #0 has length 1; 2 is required`` before anything was dispatched, and the orchestrator fell
back to a bare ``dispatch_tool`` (losing the plan's tracking).

A bare name is a reference: the Task Agent only builds a skill from a dict carrying
``instructions``, so a name must resolve to the turn's active skill of that name, and a name with
no active match falls back to the same tool-scoped inheritance an empty list gets.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from orchestration import plan_and_execute_tool as pae  # noqa: E402
from planning.high_level_plan import DirectDispatchParams, HighLevelPlan  # noqa: E402

VIDEO = {"name": "video-processing", "description": "d", "instructions": "VIDEO BODY"}
OTHER = {"name": "pdf", "description": "d", "instructions": "PDF BODY"}


def _plan(skills):
    return {
        "execution_mode": "direct",
        "sub_goals": [{"id": "sg1", "description": "x", "reasoning": "r",
                       "suggested_tools": [], "suggested_skills": [], "depends_on": []}],
        "suggested_tools": {},
        "suggested_skills": {},
        "reasoning": "one tool",
        "direct_dispatch_params": {
            "prompt": "segment shots",
            "tools": ["shot-segmentation___segment_shots"],
            "skills": skills,
            "model_id": None,
        },
    }


def test_bare_string_skills_parse():
    hlp = HighLevelPlan.from_dict(_plan(["video-processing"]))
    assert hlp.direct_dispatch_params.skills == [{"name": "video-processing"}]
    # Round-trips, and dict entries are untouched.
    assert DirectDispatchParams.from_dict({"prompt": "p", "tools": [], "skills": [VIDEO]}).skills == [VIDEO]
    assert DirectDispatchParams.from_dict({"prompt": "p", "tools": []}).skills == []


def _run(monkeypatch, skills, active):
    sent = {}

    def fake_invoke_runtime(arn, payload):
        if arn == "planning-arn":
            return json.dumps(_plan(skills)), {"inputTokens": 1, "outputTokens": 1}
        sent.update(payload)
        return "submitted job-1", {"inputTokens": 1, "outputTokens": 1}

    monkeypatch.setattr(pae, "PLANNING_AGENT_ARN", "planning-arn")
    monkeypatch.setattr(pae, "TASK_AGENT_ARN", "task-arn")
    monkeypatch.setattr(pae, "_invoke_runtime", fake_invoke_runtime)
    monkeypatch.setattr("jobs.context.current_active_skills", lambda: list(active))
    result = pae.plan_and_execute({"toolUseId": "tu-1", "input": {"goal": "segment shots"}})
    return result, sent


def test_direct_plan_with_a_named_skill_dispatches_the_active_skill_body(monkeypatch):
    result, sent = _run(monkeypatch, ["video-processing"], [VIDEO, OTHER])
    assert result["status"] == "success", result
    assert sent["skills"] == [VIDEO]


def test_unknown_skill_name_falls_back_to_inheritance(monkeypatch):
    result, sent = _run(monkeypatch, ["no-such-skill"], [VIDEO])
    assert result["status"] == "success", result
    assert sent["skills"] == [VIDEO]


# ------------------------------------------------------------- prior-phase inputs never go direct
REPORT_GOAL = ("Phase 3 report for nba.mp4: one node sg5_compliance_report with "
               "external_inputs [{plan: plan-content-moderation-foundation, node: sg1_segment_shots}]")


def test_a_direct_plan_whose_goal_consumes_external_inputs_takes_the_plan_path(monkeypatch):
    """Staging session d6a560a8: the single-node Phase 3 report plans went direct, so the chainer never
    resolved their external_inputs and every report guessed keys and invented findings. A goal that
    consumes a prior phase must take the DAG (plan) path, never direct dispatch."""
    calls = []

    def fake_invoke_runtime(arn, payload):
        calls.append(arn)
        if arn == "planning-arn":
            return json.dumps(_plan([])), {"inputTokens": 1, "outputTokens": 1}
        raise AssertionError(f"direct dispatch to {arn}")

    monkeypatch.setattr(pae, "PLANNING_AGENT_ARN", "planning-arn")
    monkeypatch.setattr(pae, "TASK_AGENT_ARN", "task-arn")
    monkeypatch.setattr(pae, "_invoke_runtime", fake_invoke_runtime)
    monkeypatch.setattr(pae, "_dispatch_direct",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("_dispatch_direct called")))
    seen = {}

    def fake_self_retry(*, fused_dag, planning_payload, available_tools, cumulative_tokens):
        # Reaching here proves the reroute flipped the plan onto the standard/DAG path.
        seen["reached"] = True
        seen["fused_dag"] = fused_dag
        return None, "plan path stopped by the test"

    monkeypatch.setattr(pae, "_plan_with_self_retry", fake_self_retry)
    result = pae.plan_and_execute({"toolUseId": "tu-1", "input": {"goal": REPORT_GOAL}})
    assert seen.get("reached") is True, "a prior-phase goal must reach the DAG self-retry path"
    # A rerouted direct plan carries no fused DAG, so the self-retry starts from nothing and re-plans.
    assert seen["fused_dag"] is None
    assert calls == ["planning-arn"]
    assert result["status"] == "error"


def test_consumes_prior_phase_reads_goal_constraints_and_the_planners_text():
    hlp = HighLevelPlan.from_dict(_plan([]))
    assert not pae._consumes_prior_phase("segment shots", "", hlp)
    assert pae._consumes_prior_phase(REPORT_GOAL, "", hlp)
    assert pae._consumes_prior_phase("report", "use external_inputs from phase 2", hlp)
    hlp.direct_dispatch_params.prompt = "read external_inputs[0]"
    assert pae._consumes_prior_phase("report", "", hlp)


def test_a_no_subgoal_direct_plan_reroutes_without_crashing(monkeypatch):
    """A rerouted direct plan with no sub_goals synthesizes one so the HLP stays coherent, and still
    takes the plan path (never direct dispatch)."""
    plan = _plan(["content_moderation", {"name": "pdf"}])
    plan["sub_goals"] = []

    def fake_invoke_runtime(arn, payload):
        if arn == "planning-arn":
            return json.dumps(plan), {"inputTokens": 1, "outputTokens": 1}
        raise AssertionError(f"direct dispatch to {arn}")

    monkeypatch.setattr(pae, "PLANNING_AGENT_ARN", "planning-arn")
    monkeypatch.setattr(pae, "_invoke_runtime", fake_invoke_runtime)
    monkeypatch.setattr(pae, "_dispatch_direct",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("_dispatch_direct called")))
    seen = {}

    def fake_self_retry(*, fused_dag, planning_payload, available_tools, cumulative_tokens):
        seen["reached"] = True
        return None, "plan path stopped by the test"

    monkeypatch.setattr(pae, "_plan_with_self_retry", fake_self_retry)
    result = pae.plan_and_execute({"toolUseId": "tu-1", "input": {"goal": REPORT_GOAL}})
    assert seen.get("reached") is True
    assert result["status"] == "error"

