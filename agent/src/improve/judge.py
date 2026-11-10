"""Real LLM judge for goal achievement, scored in two tiers.

For a long-running media harness, "did the agent succeed?" is not one question.
The judge returns two separate scores in one call:

  dispatch_correct  - did the agent understand the request and correctly kick off
                      the right work (right tools, right plan)? This is what
                      AgentCore's lenient GoalSuccessRate effectively measures, so
                      it is the calibration reference.
  results_delivered - did the final, usable result actually arrive in-session
                      (not just "I'll get back to you")?

Collapsing these hides the async-dispatch case (correctly dispatched, delivery
pending) that a single pass/fail scores wrongly either way.

The evidence the judge reads is config-driven (goal_source, input_set) so the
best combination is chosen by measured agreement, not asserted. Real Bedrock
calls only; no cached scores.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional, Sequence

try:
    from .contract import EvaluatorResult, Outcome, Run, tool_name_sequence
except ImportError:
    from improve.contract import EvaluatorResult, Outcome, Run, tool_name_sequence  # type: ignore

from model.defaults import model_for as _model_for

DEFAULT_MODEL = _model_for("improve_judge")  # agent/src/model/models.json
DEFAULT_INPUT_SET = ("goal", "response", "trajectory", "plan")
DEFAULT_GOAL_SOURCE = "full_conversation"
# Graded LLM scores land ~0.7-0.9 for "substantially correct"; 1.0 is reserved
# for flawless. A pass boundary of 0.7 treats "substantially achieved" as pass.
# This is a calibration knob — the right value should be confirmed by measuring
# where the judge's pass/fail boundary best matches trusted labels, not asserted.
DEFAULT_PASS_THRESHOLD = 0.7

# How the judge determines what the user wanted. Chosen by measured agreement
# (the sweep found full_conversation is the only stable source on multi-turn
# sessions); kept configurable so that choice stays a knob, not a hardcode.
GOAL_SOURCES = ("first_user", "all_user", "plan_state", "plan_or_first_user", "full_conversation")

_RUBRIC = (
    "You are grading an AI agent's session for a long-running media platform. "
    "Score TWO independent things, each 0.0-1.0:\n"
    "  dispatch_correct: did the agent correctly understand the request and kick "
    "off the right work (correct tools / plan)? 1.0 = fully correct dispatch.\n"
    "  results_delivered: did the final, usable result actually arrive in this "
    "session? 1.0 = delivered; 0.0 = only promised/pending. If the task needed no "
    "async work, results_delivered mirrors whether the answer was given.\n"
    'Respond with ONE JSON object: {"dispatch_correct": <0-1>, '
    '"results_delivered": <0-1>, "reason": "<one sentence>"}. No other text.'
)


def _resolve_goal(run: Run, goal_source: str) -> Optional[str]:
    conversation = run.turns or []
    user_texts = [t["text"] for t in conversation if t.get("role") == "USER" and t.get("text")]
    plan_goal = getattr(run.plan, "goal", "") if run.plan and not isinstance(run.plan, str) else ""
    if goal_source == "plan_state":
        return plan_goal or None
    if goal_source == "first_user":
        return user_texts[0] if user_texts else None
    if goal_source == "all_user":
        return "\n".join(user_texts) or None
    if goal_source == "plan_or_first_user":
        return plan_goal or (user_texts[0] if user_texts else None)
    return None  # full_conversation


def _build_prompt(run: Run, input_set: Sequence[str], goal_source: str) -> str:
    parts: list[str] = [_RUBRIC, ""]
    if goal_source == "full_conversation":
        transcript = "\n".join(f"{t['role']}: {t['text']}" for t in (run.turns or []) if t.get("text"))
        parts += ["FULL CONVERSATION (infer what the user wanted):", transcript or "(none)", ""]
    else:
        parts += ["USER GOAL:", _resolve_goal(run, goal_source) or "(goal unavailable)", ""]
    if "response" in input_set:
        response = run.response if isinstance(run.response, str) else "(no response captured)"
        parts += ["AGENT FINAL RESPONSE:", response, ""]
    if "trajectory" in input_set:
        parts += ["TOOLS THE AGENT USED (in order):", ", ".join(tool_name_sequence(run.trajectory)) or "(none)", ""]
    if "plan" in input_set and run.plan and not isinstance(run.plan, str):
        node_states = [f"{n.id}={n.status}" for n in getattr(run.plan, "nodes", [])]
        parts += ["DAG NODE STATUS:", ", ".join(node_states) or "(no plan)", ""]
    parts.append('Return only the JSON object.')
    return "\n".join(parts)


@dataclass
class JudgeConfig:
    model: str = DEFAULT_MODEL
    input_set: Sequence[str] = DEFAULT_INPUT_SET
    goal_source: str = DEFAULT_GOAL_SOURCE
    pass_threshold: float = DEFAULT_PASS_THRESHOLD
    runs: int = 1            # average this many judge calls per tier (reduces LLM variance)
    max_tokens: int = 2048   # headroom: Sonnet5 may emit a reasoningContent block first


@dataclass
class TwoTierScore:
    dispatch_correct: EvaluatorResult
    results_delivered: EvaluatorResult
    reason: str = ""


def _extract_text(resp: dict) -> Optional[str]:
    for block in resp.get("output", {}).get("message", {}).get("content", []):
        if isinstance(block, dict) and "text" in block:
            return block["text"]
    return None


def _parse_two_tier(text: str) -> tuple[Optional[float], Optional[float], str]:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None, None, f"unparseable judge reply: {text[:120]}"
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None, None, f"invalid judge JSON: {text[:120]}"

    def _num(key):
        try:
            return float(obj[key])
        except (KeyError, TypeError, ValueError):
            return None

    return _num("dispatch_correct"), _num("results_delivered"), str(obj.get("reason", ""))


def _no_gradable_evidence(run: Run, goal_source: str = DEFAULT_GOAL_SOURCE) -> Optional[str]:
    """Why this run cannot be graded at all, or None when it can.

    Both tiers need something to grade: ``dispatch_correct`` needs the request in order to say
    whether it was understood, and ``results_delivered`` needs the response in order to say
    whether anything arrived. With NEITHER there is nothing, and the DAG statuses still in the
    prompt describe the plan rather than the request.

    The request is resolved through ``goal_source``, not read off ``run.turns``. An earlier
    version checked turns alone, which silently broke the two sources that exist precisely for
    transcript-less runs: ``plan_state`` and ``plan_or_first_user`` take the goal from
    ``run.plan.goal``, so a plan-bearing run with no Memory events IS gradable under them and
    was being refused. ``judge_sweep.py`` sweeps all five sources, so the guard has to agree
    with whichever one is configured or it empties an arm of the calibration.
    """
    if goal_source == "full_conversation":
        request = "\n".join(t["text"] for t in (run.turns or []) if t.get("text"))
    else:
        request = _resolve_goal(run, goal_source) or ""
    response = run.response if isinstance(run.response, str) else ""
    if request.strip() or response.strip():
        return None
    return (f"no request (goal_source={goal_source}) and no final response were captured for "
            "this session, so neither tier has anything to grade (this is an evidence gap, "
            "not a failure)")


def _to_result(name: str, score: Optional[float], reason: str, threshold: float) -> EvaluatorResult:
    if score is None:
        return EvaluatorResult(name, "llm", Outcome.NO_DATA, None, reason or f"{name}: no score.")
    outcome = Outcome.PASS if score >= threshold else Outcome.FAIL
    return EvaluatorResult(name, "llm", outcome, score, reason)


class GoalSuccessJudge:
    """Two-tier goal judge (dispatch_correct + results_delivered) via one real call."""

    def __init__(self, config: Optional[JudgeConfig] = None, region: str = "us-west-2",
                 profile: Optional[str] = None) -> None:
        self._config = config or JudgeConfig()
        self._region = region
        self._profile = profile
        self._client = None

    def _bedrock(self):
        if self._client is None:
            import boto3
            session = boto3.Session(profile_name=self._profile) if self._profile else boto3.Session()
            self._client = session.client("bedrock-runtime", region_name=self._region)
        return self._client

    def _judge_once(self, run: Run) -> tuple[Optional[float], Optional[float], str]:
        """One real judge call. Returns (dispatch, delivered, reason); None on failure."""
        cfg = self._config
        prompt = _build_prompt(run, cfg.input_set, cfg.goal_source)
        try:
            resp = self._bedrock().converse(
                modelId=cfg.model,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={"maxTokens": cfg.max_tokens},
            )
        except Exception as e:
            return None, None, f"judge call failed: {type(e).__name__}: {e}"
        reply = _extract_text(resp)
        if reply is None:
            return None, None, f"judge returned no answer text (stopReason={resp.get('stopReason')})."
        return _parse_two_tier(reply)

    def judge(self, run: Run) -> TwoTierScore:
        """Average ``runs`` judge calls per tier to damp LLM variance."""
        cfg = self._config
        absent = _no_gradable_evidence(run, cfg.goal_source)
        if absent:
            # Refuse to grade rather than grade nothing. Asked to score a prompt reading
            # "(none)" / "(no response captured)", the model does not decline: it returned a
            # confident dispatch_correct of 0.3 for 30 of the 50 harvested sessions, and that
            # invented number was then averaged, thresholded, folded into the verdict as a
            # FAIL and mined as a failure. A NO_DATA tier says the same thing honestly and
            # cannot be mistaken for a measurement.
            return TwoTierScore(
                dispatch_correct=_to_result("dispatch_correct", None, absent, cfg.pass_threshold),
                results_delivered=_to_result("results_delivered", None, absent, cfg.pass_threshold),
                reason=absent)
        dispatch_scores, delivered_scores, reasons = [], [], []
        for _ in range(max(1, cfg.runs)):
            dispatch, delivered, reason = self._judge_once(run)
            if dispatch is not None:
                dispatch_scores.append(dispatch)
            if delivered is not None:
                delivered_scores.append(delivered)
            if reason:
                reasons.append(reason)

        def _mean(values):
            return sum(values) / len(values) if values else None

        dispatch_mean = _mean(dispatch_scores)
        delivered_mean = _mean(delivered_scores)
        reason = reasons[0] if reasons else "no judge reason"
        if cfg.runs > 1:
            reason = f"[mean of {len(dispatch_scores)} runs] {reason}"
        return TwoTierScore(
            dispatch_correct=_to_result("dispatch_correct", dispatch_mean, reason, cfg.pass_threshold),
            results_delivered=_to_result("results_delivered", delivered_mean, reason, cfg.pass_threshold),
            reason=reason,
        )
