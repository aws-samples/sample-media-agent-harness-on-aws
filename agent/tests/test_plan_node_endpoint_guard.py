"""A plan node may only submit to the fal endpoint(s) its plan named (``DAGNode.endpoint_ids``).

Session c3447913: node ``animate_shot2`` was planned for ``minimax/h3-max/image-to-video``; its
sub-agent submitted to ``fal-ai/minimax/hailuo-02/standard/image-to-video`` instead, and the clip
422'd on ``duration`` minutes later. The submit is now refused BEFORE it reaches fal, so nothing
is generated or billed and the agent reads the planned id in the same turn.
"""
from __future__ import annotations

import asyncio
import contextvars
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from jobs import context as jobs_context  # noqa: E402
from planning.dag_schema import DAG_JSON_SCHEMA, DAGNode  # noqa: E402
from tool_args import endpoint_matches_plan, submitted_endpoint  # noqa: E402

H3 = "minimax/h3-max/image-to-video"
HAILUO = "fal-ai/minimax/hailuo-02/standard/image-to-video"


# ---------------------------------------------------------------- pure matching rule
def test_submitted_endpoint_reads_the_top_level_id_normalized():
    assert submitted_endpoint({"endpoint_id": f" {H3} ", "input": {"model_id": "x"}}) == H3
    assert submitted_endpoint({"model_id": "fal-ai/minimax/h3-max/image-to-video"}) == H3  # known fix-up
    assert submitted_endpoint({"input": {"endpoint_id": H3}}) == ""  # only the top level names the app
    assert submitted_endpoint(None) == ""


def test_endpoint_matches_exact_or_under_a_planned_family():
    assert endpoint_matches_plan(H3, [H3])
    assert endpoint_matches_plan(H3, ["minimax/h3-max"])            # family admits its paths
    assert endpoint_matches_plan(H3, ["fal-ai/kling-video/v3", H3])  # any of several
    assert not endpoint_matches_plan(HAILUO, [H3])                  # the c3447913 drift
    assert not endpoint_matches_plan("fal-ai/minimax-music", ["fal-ai/minimax"])  # path boundary
    assert not endpoint_matches_plan("minimax/h3-max", [H3])        # bare app is not the planned model


def test_no_plan_or_no_endpoint_is_no_constraint():
    assert endpoint_matches_plan(HAILUO, [])
    assert endpoint_matches_plan(HAILUO, None)
    assert endpoint_matches_plan("", [H3])


# ---------------------------------------------------------------- the node carries the field
def test_dag_node_round_trips_endpoint_ids_and_omits_it_when_empty():
    n = DAGNode.from_dict({"id": "a", "prompt": "p", "tools": [], "depends_on": [], "output_key": "k",
                           "output_type": "json", "endpoint_ids": [f" {H3} ", "", 7]})
    assert n.endpoint_ids == [H3]
    assert n.to_dict()["endpoint_ids"] == [H3]
    plain = DAGNode.from_dict({"id": "b", "prompt": "p", "tools": [], "depends_on": [], "output_key": "k",
                               "output_type": "text", "endpoint_ids": H3})  # a string is not a list
    assert plain.endpoint_ids == [] and "endpoint_ids" not in plain.to_dict()
    assert "endpoint_ids" in DAG_JSON_SCHEMA["properties"]["nodes"]["items"]["properties"]


def test_planned_endpoints_context_round_trip():
    def run():
        assert jobs_context.current_planned_endpoints() == ()
        tok = jobs_context.set_planned_endpoints([H3, " ", None])
        assert jobs_context.current_planned_endpoints() == (H3,)
        jobs_context.reset_planned_endpoints(tok)
        assert jobs_context.current_planned_endpoints() == ()

    contextvars.copy_context().run(run)


def test_execute_plan_node_binds_and_resets_the_planned_endpoints():
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1] / "src" / "task_agent.py").read_text()
    body = src[src.index("async def _execute_plan_node"):src.index("async def _fetch_plan_node_results")]
    assert 'jobs_context.set_planned_endpoints(payload.get("endpoint_ids")' in body
    assert "jobs_context.reset_planned_endpoints(planned_endpoints_token)" in body


# ---------------------------------------------------------------- the call path refuses the drift
def _tool(name: str):
    from jobs import strands_tool_wrap as stw

    tool = stw.ResilientMCPAgentTool.__new__(stw.ResilientMCPAgentTool)
    tool._agent_tool_name = name
    tool._call_lease = None
    tool.mcp_client = object()
    return tool


def _drive(tool, tool_input, *, planned, node="animate_shot2", monkeypatch):
    """Run one `_base_stream_once` inside a plan node; return (results, inputs that reached fal)."""
    from jobs import strands_tool_wrap as stw
    from strands.types._events import ToolResultEvent

    sent = []

    async def fake_stream(self, tool_use, invocation_state, **kwargs):
        sent.append(tool_use["input"])
        yield ToolResultEvent({"toolUseId": tool_use["toolUseId"], "status": "success",
                               "content": [{"text": "{\"request_id\": \"r1\"}"}]})

    monkeypatch.setattr(stw.MCPAgentTool, "stream", fake_stream)

    async def run():
        ntok = jobs_context.set_node_id(node) if node else None
        ptok = jobs_context.set_planned_endpoints(planned)
        try:
            return [ev async for ev in tool._base_stream_once(
                {"toolUseId": "t1", "name": tool.tool_name, "input": tool_input}, {}, {})]
        finally:
            jobs_context.reset_planned_endpoints(ptok)
            if ntok:
                jobs_context.reset_node_id(ntok)

    events = contextvars.copy_context().run(asyncio.run, run())
    return [ev.tool_result for ev in events], sent


def test_off_plan_submit_is_refused_before_it_reaches_fal(monkeypatch):
    results, sent = _drive(_tool("usr-1___submit_job"), {"endpoint_id": HAILUO, "input": {"duration": "6"}},
                           planned=[H3], monkeypatch=monkeypatch)
    assert sent == []
    assert results[0]["status"] == "error" and results[0]["toolUseId"] == "t1"
    text = results[0]["content"][0]["text"]
    assert H3 in text and HAILUO in text and "animate_shot2" in text and "Nothing was submitted" in text


def test_planned_submit_and_sync_run_pass_through(monkeypatch):
    for name in ("usr-1___submit_job", "usr-1___run_model"):
        results, sent = _drive(_tool(name), {"endpoint_id": H3, "input": {}}, planned=["minimax/h3-max"],
                               monkeypatch=monkeypatch)
        assert results[0]["status"] == "success"
        # Passes the guard unchanged: no model-specific field is added on the way (the model's own schema is
        # checked instead, and with no schema tool on this fake client that check fails open).
        assert sent == [{"endpoint_id": H3, "input": {}}]


def test_sync_run_model_is_also_held_to_the_plan(monkeypatch):
    results, sent = _drive(_tool("usr-1___run_model"), {"endpoint_id": "fal-ai/flux-2/edit", "input": {}},
                           planned=["fal-ai/flux-pro"], monkeypatch=monkeypatch)
    assert sent == [] and results[0]["status"] == "error"


def test_reads_and_unconstrained_nodes_are_never_refused(monkeypatch):
    # Status/result reads and schema lookups name an endpoint but generate nothing.
    for name in ("usr-1___check_job", "usr-1___get_job_result", "usr-1___get_model_schema"):
        results, sent = _drive(_tool(name), {"endpoint_id": "minimax/h3-max", "request_id": "r1"},
                               planned=[H3], monkeypatch=monkeypatch)
        assert results[0]["status"] == "success" and len(sent) == 1
    # A node planned with no endpoint (older plans, non-fal nodes) is unconstrained.
    results, sent = _drive(_tool("usr-1___submit_job"), {"endpoint_id": HAILUO}, planned=[],
                           monkeypatch=monkeypatch)
    assert results[0]["status"] == "success" and len(sent) == 1


def test_a_planned_endpoint_that_does_not_exist_stops_constraining_the_node():
    """Plan 07fd91bd: the planner named `fal-ai/flux-kontext/pro` (no such model; the real id is
    `fal-ai/flux-pro/kontext`). The planned id 404'd, the guard refused the correct one, the plan failed."""
    from strands.types._events import ToolResultEvent
    from jobs.strands_tool_wrap import _off_plan_submit_refusal, _retire_missing_planned_endpoint

    def run():
        jobs_context.set_planned_endpoints(["fal-ai/flux-kontext/pro"])
        tool = "usr-8ddf___run_model"
        good = {"toolUseId": "u", "input": {"endpoint_id": "fal-ai/flux-pro/kontext"}}
        assert _off_plan_submit_refusal(tool, good) is not None            # still guarded before any 404
        bad = {"toolUseId": "u", "input": {"endpoint_id": "fal-ai/flux-kontext/pro"}}
        not_found = ToolResultEvent({"toolUseId": "u", "status": "success", "content": [{"text":
            '{"status":"not_found","endpoint_id":"fal-ai/flux-kontext/pro","error":"Model not found"}'}]})
        _retire_missing_planned_endpoint(tool, bad, [not_found])
        assert jobs_context.current_planned_endpoints() == ()
        assert _off_plan_submit_refusal(tool, good) is None                # the correction now runs

    contextvars.copy_context().run(run)


def test_a_real_planned_endpoint_is_never_retired_by_an_ordinary_error():
    from strands.types._events import ToolResultEvent
    from jobs.strands_tool_wrap import _retire_missing_planned_endpoint

    def run():
        jobs_context.set_planned_endpoints([H3])
        err = ToolResultEvent({"toolUseId": "u", "status": "error", "content": [{"text": "422 int_parsing duration"}]})
        _retire_missing_planned_endpoint("fal___submit_job", {"input": {"endpoint_id": H3}}, [err])
        assert jobs_context.current_planned_endpoints() == (H3,)

    contextvars.copy_context().run(run)
