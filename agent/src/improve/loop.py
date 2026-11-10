"""The thin orchestrator — wires stages, consults config, branches on `mode`.

Phase M1 implements the ``observe_and_score`` branch: OBSERVE -> EVALUATE ->
report + calibrate, returning before MINE. It proves a run can FAIL honestly. No
proposer.

Design invariants (implementation-plan):
  * REQ-010 — thin orchestrator; no `if target_type ==` anywhere.
  * REQ-009 — all policy from the config file; nothing hard-coded here.
  * REQ-014 — output is universal ``Verdict`` objects, never an ``overall_pass`` bool.
"""
from __future__ import annotations

import json
import os
from typing import Optional

try:
    from .contract import Outcome, Overall, Scenario, Verdict
    from .evaluate import evaluate
    from .judge import DEFAULT_MODEL as DEFAULT_JUDGE_MODEL, GoalSuccessJudge, JudgeConfig
    from .observe.build_run import HarvestedPlanSource, build_run
    from .observe.memory_reader import DEFAULT_MEMORY_ID, MemoryReader
    from .observe.span_trajectory import HarvestedSpanSource
except ImportError:  # pragma: no cover
    from improve.contract import Outcome, Overall, Scenario, Verdict  # type: ignore
    from improve.evaluate import evaluate  # type: ignore
    from improve.judge import DEFAULT_MODEL as DEFAULT_JUDGE_MODEL, GoalSuccessJudge, JudgeConfig  # type: ignore
    from improve.observe.build_run import HarvestedPlanSource, build_run  # type: ignore
    from improve.observe.memory_reader import DEFAULT_MEMORY_ID, MemoryReader  # type: ignore
    from improve.observe.span_trajectory import HarvestedSpanSource  # type: ignore


def _repo_root() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", "..", ".."))


def _workspace_root() -> str:
    """The directory containing this checkout — where the out-of-repo harvest lives."""
    return os.path.dirname(_repo_root())


def load_config(path: Optional[str] = None) -> dict:
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "loop.config.yaml")
    import yaml  # PyYAML is a project dependency (via bedrock-agentcore/strands)
    with open(path) as f:
        return yaml.safe_load(f)


def load_golden(dataset: str = "golden") -> list[Scenario]:
    path = os.path.join(_repo_root(), "tests", "evaluation", "datasets", dataset, "golden.jsonl")
    scenarios = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            scenarios.append(Scenario(
                scenario_id=record["scenario_id"], goal=record.get("goal", ""),
                category=record.get("category", ""), evaluators=record.get("evaluators", []),
                claims=record.get("claims", {}), expected=record.get("expected", {}),
                source_session=record.get("source_session", record["scenario_id"]),
                label_provenance=record.get("label_provenance", ""),
            ))
    return scenarios


def _agentcore_reference() -> dict:
    """Harvested AgentCore online-eval GoalSuccessRate per session (the calibration
    reference for the dispatch_correct tier). Keyed by 8-char session prefix."""
    path = os.path.join(_workspace_root(), "ideas", "staging-data", "fresh", "judge-scores-fresh.json")
    if not os.path.exists(path):
        return {}
    raw = json.load(open(path))
    out = {}
    for session_id, scores in raw.items():
        gsr = scores.get("Builtin.GoalSuccessRate")
        if gsr is not None:
            try:
                out[session_id[:8]] = float(gsr)
            except (TypeError, ValueError):
                pass
    return out


def _make_judge(cfg: dict, profile: Optional[str]) -> GoalSuccessJudge:
    jc = cfg.get("judge", {})
    return GoalSuccessJudge(
        JudgeConfig(
            model=jc.get("model", DEFAULT_JUDGE_MODEL),
            goal_source=jc.get("goal_source", "full_conversation"),
            input_set=tuple(jc.get("input_set", ("goal", "response", "trajectory", "plan"))),
            pass_threshold=jc.get("pass_threshold", 0.7),
            runs=jc.get("runs", 1),
        ),
        profile=profile,
    )


def run_improvement_loop(config: Optional[dict] = None, profile: Optional[str] = None) -> list[Verdict]:
    """OBSERVE -> EVALUATE over the configured dataset. Returns [Verdict].

    A pre-built judge (from config) is passed to evaluate(), so evaluate's own
    judge-construction fallback is never used here — we pass only the evaluate
    keys it actually reads (evaluators, gating_evaluators).
    """
    cfg = config or load_config()
    mode = cfg.get("mode", "observe_and_score")
    eval_cfg = cfg.get("evaluate", {})
    profile = profile or os.environ.get("AWS_PROFILE")
    observe_cfg = cfg.get("observe", {})

    scenarios = load_golden(observe_cfg.get("dataset", "golden"))
    span_source = HarvestedSpanSource()
    plan_source = HarvestedPlanSource()
    memory_reader = MemoryReader(
        memory_id=observe_cfg.get("memory_id", DEFAULT_MEMORY_ID), profile=profile)
    judge = _make_judge(cfg, profile)

    verdicts: list[Verdict] = []
    for scenario in scenarios:
        run = build_run(scenario, span_source=span_source, plan_source=plan_source,
                        memory_reader=memory_reader)
        verdicts.append(evaluate(run, config=eval_cfg, judge=judge))

    if mode == "observe_and_score":
        return verdicts
    raise NotImplementedError(f"mode {mode!r} is M2/M3 — not built in this phase.")


def calibrate(verdicts: list[Verdict], compare_tier: str = "dispatch_correct") -> dict:
    """Compare our tier score against the AgentCore reference (agreement on
    PASS/FAIL). AgentCore's lenient GoalSuccessRate maps to dispatch_correct."""
    reference = _agentcore_reference()
    rows, agree, comparable = [], 0, 0
    for verdict in verdicts:
        tier = next((r for r in verdict.evaluator_results if r.name == compare_tier), None)
        ref = reference.get(verdict.run_id[:8])
        ours_pass = tier.outcome == Outcome.PASS if tier else None
        ref_pass = (ref >= 1.0) if ref is not None else None
        if ours_pass is not None and ref_pass is not None:
            comparable += 1
            if ours_pass == ref_pass:
                agree += 1
        rows.append({"run_id": verdict.run_id, "our_tier": tier.score if tier else None,
                     "our_pass": ours_pass, "agentcore": ref, "agentcore_pass": ref_pass})
    return {"tier": compare_tier, "agreement": f"{agree}/{comparable}", "rows": rows}


def report(verdicts: list[Verdict], pass_rate_threshold: Optional[float] = None) -> dict:
    total = len(verdicts)
    passed = sum(1 for v in verdicts if v.overall == Overall.PASS)
    pass_rate = (passed / total) if total else 0.0
    result = {
        "total": total, "passed": passed, "pass_rate": pass_rate,
        "runs": [{"run_id": v.run_id, "overall": v.overall.value, "reason": v.reason,
                  "utility": v.utility.terms} for v in verdicts],
    }
    if pass_rate_threshold is not None:
        result["threshold"] = pass_rate_threshold
        result["gate"] = "PASS" if pass_rate >= pass_rate_threshold else "FAIL"
    return result


if __name__ == "__main__":
    cfg = load_config()
    verdicts = run_improvement_loop(cfg)
    rep = report(verdicts, cfg.get("overall_gate", {}).get("pass_rate_threshold"))
    cal = calibrate(verdicts, cfg.get("calibration", {}).get("compare_tier", "dispatch_correct"))
    gate = f"  gate={rep['gate']} (>= {rep['threshold']})" if "gate" in rep else ""
    print(f"pass_rate = {rep['pass_rate']:.2f} ({rep['passed']}/{rep['total']}){gate}")
    for r in rep["runs"]:
        print(f"  {r['overall']:8} {r['run_id'][:12]}  {r['reason'][:66]}")
    print(f"\ncalibration ({cal['tier']} vs AgentCore): agreement {cal['agreement']}")
    for row in cal["rows"]:
        print(f"  {row['run_id'][:12]}: ours={row['our_tier']} ({row['our_pass']}) "
              f"agentcore={row['agentcore']} ({row['agentcore_pass']})")
