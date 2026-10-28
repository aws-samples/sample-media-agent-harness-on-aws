"""Planning Agent — the "thinker" AgentCore runtime.

Receives a user goal + tool/skill/model catalog, performs deep reasoning,
and produces a HighLevelPlan JSON with execution_mode, sub_goals, suggested_tools,
and reasoning. Uses `think` as primary tool for cognitive analysis.
"""

import json, logging, os, time
from datetime import datetime, timezone

# How much of a skill description the planner is allowed to see.
#
# This was 120, which cut every description off mid-sentence -- and the ONE skill that
# said what it is NOT for put that clause at character 498, so the planner never saw it.
# It then authored a mux node telling the agent to combine two files with a tool that
# takes one, because nothing in its 120 characters said otherwise. Skills are the routing
# unit and there are only ~14, so the tokens buy real routing accuracy.
_SKILL_DESC_CHARS = 420


import boto3
from strands import Agent
from strands.models import BedrockModel
import runtime_config_bootstrap  # noqa: F401,E402  — hydrate env before first-party imports (V2 1536B cap)
from model.factory import build_model
from strands_tools import think  # noqa: F401 — kept for potential future use
from botocore.config import Config as BotoConfig
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from planning.high_level_plan import HIGH_LEVEL_PLAN_JSON_SCHEMA
from planning.dag_schema import dynamic_fanout_enabled
from prompts.planning import FUSED_DESIGN_SECTION_TEMPLATE, PLANNING_SYSTEM_PROMPT_TEMPLATE

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()
REGION = os.environ.get("AWS_REGION", "us-west-2")
S3_BUCKET = os.environ.get("SKILLS_S3_BUCKET", "")
DDB_TABLE = os.environ.get("PLAN_STATE_TABLE", "")
_boto_cfg = BotoConfig(read_timeout=300, connect_timeout=10, retries={"max_attempts": 4, "mode": "standard"})

# Default to a reasoning-capable model
DEFAULT_MODEL_ID = os.environ.get("PLANNING_MODEL_ID", "global.anthropic.claude-sonnet-5-5")

PLANNING_SYSTEM_PROMPT = PLANNING_SYSTEM_PROMPT_TEMPLATE.format(schema=json.dumps(HIGH_LEVEL_PLAN_JSON_SCHEMA, indent=2), fanout_guidance="""
RUNTIME FAN-OUT — collections discovered mid-plan:
- If a step must repeat the SAME operation once per element of a collection whose COUNT is only \
known AT RUNTIME — either AFTER an earlier step runs (one output per speaker AFTER diarization) \
OR by listing a source (one job per .mp4 under an S3 prefix) — do NOT try to enumerate sibling \
sub-goals; you cannot know how many there are. Emit ONE sub-goal phrased "for each <item> \
discovered by <upstream sub-goal>" (or "for each <item> in <source to list>, <operation>"), with \
depends_on referencing the upstream sub-goal when there is one. The design agent expands it into \
a collector + runtime map/gather.
- This is the UNKNOWN-COUNT case. Contrast with "FAN-OUT FOR ENUMERABLE TARGETS" above (a list \
known at plan time, like French/Spanish/German), which still fans out into one sibling sub-goal \
per named target.
""" if dynamic_fanout_enabled() else "")


def _fused_system_prompt() -> str:
    """Planning rules + the Design Agent's own rules, for one-call plan-and-design."""
    from prompts.design import design_system_prompt

    return PLANNING_SYSTEM_PROMPT + FUSED_DESIGN_SECTION_TEMPLATE.format(
        design_rules=design_system_prompt())


# Payload mode that asks for {"high_level_plan", "dag"} in one response (plan_and_execute).
FUSED_MODE = "plan_and_design"


def _fused_high_level_plan(text: str):
    """The ``high_level_plan`` object of a fused response, or None when it is not one."""
    start = (text or "").find("{")
    if start < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return None
    hlp = obj.get("high_level_plan") if isinstance(obj, dict) else None
    return hlp if isinstance(hlp, dict) else None


@app.entrypoint
async def invoke(payload, context):
    goal = payload.get("goal", "")
    if not goal:
        yield json.dumps({"error": "goal is required"})
        return

    catalog = payload.get("catalog", {})
    # Planning is PINNED to the reasoning model (global Sonnet 5.5): DAG quality must not
    # degrade because the user selected a faster/cheaper orchestrator model. Ignore any
    # model_id propagated from the caller's pick.
    model_id = DEFAULT_MODEL_ID
    session_id = payload.get("session_id", "")

    # Set OTEL baggage for trace correlation
    if session_id:
        try:
            from opentelemetry import baggage, context as otel_context
            ctx = baggage.set_baggage("session.id", session_id)
            otel_context.attach(ctx)
        except Exception:
            pass

    # Build the user prompt with catalog context
    catalog_text = _format_catalog(catalog)
    fused = payload.get("mode") == FUSED_MODE
    user_prompt = f"Goal: {goal}\n\n{catalog_text}\n\n" + (
        'Produce the {"high_level_plan": ..., "dag": ...} JSON.' if fused
        else "Produce the HighLevelPlan JSON.")

    # Self-correction retry: plan_and_execute re-invokes the fused call with the DAG validation
    # errors from the previous attempt (a preformatted "- type: message" string). Frame them as
    # feedback on the model's OWN prior output so the retry prompt DIFFERS from attempt #1 — an
    # identical prompt on the pinned reasoning model just reproduces the same invalid DAG. This
    # replaces the retired Design Agent's identical retry mechanism.
    validation_errors = payload.get("validation_errors")
    if validation_errors and str(validation_errors).strip():
        what = "DAG" if fused else "HighLevelPlan"
        user_prompt += (
            f"\n\nYour previous {what} JSON failed validation with these errors:\n"
            f"{validation_errors}\n"
            "Produce a corrected response that fixes them."
        )

    # No tools — the Planning Agent produces JSON output directly
    # The think tool adds unnecessary latency for structured output tasks
    tools = []

    model = build_model(model_id=model_id, boto_client_config=_boto_cfg)
    planning_agent = Agent(
        model=model, system_prompt=_fused_system_prompt() if fused else PLANNING_SYSTEM_PROMPT,
        tools=tools)

    logger.info("Planning: goal=%s, model=%s, catalog_keys=%s", goal[:100], model_id, list(catalog.keys()))

    try:
        _exec_start = time.time()
        _cycle_count = 0
        _text_chunks: list[str] = []

        stream = planning_agent.stream_async(user_prompt)
        async for event in stream:
            if event.get("start_event_loop"):
                _cycle_count += 1
                yield json.dumps({"type": "step", "status": "running", "label": f"Planning cycle #{_cycle_count}"})

            if "current_tool_use" in event:
                ti = event["current_tool_use"]
                if isinstance(ti, dict):
                    tn = ti.get("name", "")
                    if tn:
                        yield json.dumps({"type": "step", "status": "running", "label": f"Using {tn}..."})

            # Collect text (do NOT stream via SSE — output goes to S3)
            if "data" in event and isinstance(event["data"], str):
                _text_chunks.append(event["data"])

        _exec_duration = time.time() - _exec_start
        full_text = "".join(_text_chunks)

        # Write output to S3 and update DDB
        s3_bucket = payload.get("s3_bucket", S3_BUCKET)
        ddb_table = payload.get("ddb_table", DDB_TABLE)
        if s3_bucket and ddb_table and session_id:
            try:
                s3_key = f"plans/{session_id}/planning/output.json"
                s3 = boto3.client("s3", region_name=REGION)
                # planning/output.json is a HighLevelPlan by contract (persistence.plan_reader
                # parses it for persist_workflow), so a fused response stores its HighLevelPlan
                # there and the whole {high_level_plan, dag} beside it, which is what the caller
                # reads back through output_ref.
                hlp_text, ref_key = full_text, s3_key
                if fused:
                    hlp_only = _fused_high_level_plan(full_text)
                    if hlp_only is not None:
                        hlp_text = json.dumps(hlp_only)
                        ref_key = f"plans/{session_id}/planning/fused.json"
                        s3.put_object(Bucket=s3_bucket, Key=ref_key, Body=full_text.encode("utf-8"),
                                      ContentType="application/json")
                s3.put_object(Bucket=s3_bucket, Key=s3_key, Body=hlp_text.encode("utf-8"), ContentType="application/json")
                ddb = boto3.resource("dynamodb", region_name=REGION).Table(ddb_table)
                now = datetime.now(timezone.utc).isoformat()
                ddb.put_item(Item={
                    "session_id": session_id, "node_id": "PLAN", "status": "completed",
                    "output_s3_key": s3_key, "updated_at": now, "completed_at": now,
                    "ttl": int(time.time()) + 86400,
                })
                logger.info("Planning output written to s3://%s/%s (%d bytes)", s3_bucket, ref_key, len(full_text))
                yield json.dumps({"type": "output_ref", "s3_key": ref_key})
            except Exception as e:
                logger.warning("Failed to write planning output to S3/DDB: %s", e)
                # Fallback: stream the text directly
                yield full_text
        else:
            # No S3 config — stream text directly as fallback
            yield full_text

        yield json.dumps({
            "type": "execution_summary",
            "total_duration_ms": int(_exec_duration * 1000),
            "cycles": _cycle_count,
            "model": model_id,
        })
    except Exception as e:
        logger.exception("Planning failed")
        yield json.dumps({"error": str(e)})


def _format_catalog(catalog: dict) -> str:
    """Format the tool/skill/model catalog for the user prompt."""
    sections = []

    tools = catalog.get("tools", [])
    if tools:
        lines = [f"  - {t}" if isinstance(t, str) else f"  - {t.get('name', t)}: {t.get('description', '')[:120]}" for t in tools]
        sections.append("Available Tools:\n" + "\n".join(lines))

    skills = catalog.get("skills", [])
    if skills:
        lines = [f"  - {s.get('name', s)}: {s.get('description', '')[:_SKILL_DESC_CHARS]}" if isinstance(s, dict) else f"  - {s}" for s in skills]
        sections.append("Available Skills:\n" + "\n".join(lines))

    models = catalog.get("models", [])
    if models:
        lines = [f"  - {m.get('id', m)} ({m.get('name', '')})" if isinstance(m, dict) else f"  - {m}" for m in models]
        sections.append("Available Models:\n" + "\n".join(lines))

    return "\n\n".join(sections) if sections else "No catalog provided."


if __name__ == "__main__":
    app.run()
