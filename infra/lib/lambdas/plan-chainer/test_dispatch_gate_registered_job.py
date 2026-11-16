"""A completed dep whose jobs are still registered as running must not release its dependents.

The plan-state row names at most ONE job, but a node can submit several and settle before they finish: staging
c7d66001's recast_amit submitted two fal recasts, `reassemble` ran while both still rendered, and the reel was built
from the un-recast input strips (Scott was never replaced). The dispatch gate asks the job registry (runId +
planNodeId) for every job the dep registered.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load():
    for name in ("JOB_REGISTRY_TABLE", "PLAN_STATE_TABLE", "GATEWAY_REGISTRY_TABLE", "SKILLS_S3_BUCKET"):
        os.environ.setdefault(name, "t")
    os.environ.setdefault("TASK_AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:runtime/task")
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
    spec = importlib.util.spec_from_file_location("_chainer_gate_reg", HERE / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_chainer_gate_reg"] = mod
    spec.loader.exec_module(mod)
    return mod


pc = _load()
PID = "p1"


class _Registry:
    def __init__(self, items):
        self.items = items
        self.queries = 0

    def get_item(self, Key):
        hit = next((i for i in self.items if i.get("jobId") == Key.get("jobId")), None)
        return {"Item": hit} if hit else {}

    def query(self, **kw):
        self.queries += 1
        return {"Items": self.items}


def _plan(dep_row=None):
    nodes = [{"id": "recast", "depends_on": []}, {"id": "assemble", "depends_on": ["recast"]}]
    rec = {"node_id": f"PLAN#{PID}", "plan_id": PID, "actor_id": "a", "turn_id": "", "delegation_grant": "",
           "nodes_json": json.dumps(nodes)}
    items = [rec, {"node_id": f"{PID}#recast", "plan_id": PID, "node": "recast", "status": "completed",
                   **(dep_row or {})},
             {"node_id": f"{PID}#assemble", "plan_id": PID, "node": "assemble", "status": "pending"}]
    return rec, items


def _run(monkeypatch, registry, dep_row=None):
    monkeypatch.setattr(pc, "_job_table", registry)
    out: list = []
    monkeypatch.setattr(pc, "_dispatch_node", lambda *a, **k: out.append(a[3]["id"]))
    monkeypatch.setattr(pc, "_dispatch_ready_nodes", lambda *a, **k: out.extend(n["id"] for n in a[3]))
    rec, items = _plan(dep_row)
    pc._advance_plan("sess", rec, items)
    return out


def _job(rid, status, node="recast", run=PID):
    return {"jobId": rid, "runId": run, "planNodeId": node, "status": status, "gatewayId": "fal"}


def test_waits_while_any_registered_job_of_the_dep_runs(monkeypatch):
    assert _run(monkeypatch, _Registry([_job("req1", "SUCCEEDED"), _job("req2", "IN_PROGRESS")])) == []


def test_waits_even_when_the_row_names_a_finished_job(monkeypatch):
    """The row's job_id is one of the dep's jobs; another one still rendering keeps the gate shut."""
    reg = _Registry([_job("req1", "SUCCEEDED"), _job("req2", "IN_PROGRESS")])
    assert _run(monkeypatch, reg, {"job_id": "req1"}) == []


def test_dispatches_once_every_job_is_terminal(monkeypatch):
    assert _run(monkeypatch, _Registry([_job("req1", "SUCCEEDED"), _job("req2", "FAILED")])) == ["assemble"]


def test_other_runs_nodes_and_coordinator_rows_do_not_gate(monkeypatch):
    reg = _Registry([_job("x", "IN_PROGRESS", run="p2"), _job("y", "IN_PROGRESS", node="other"),
                     {**_job(f"{PID}#recast", "RUNNING"), "gatewayId": "__coordinator__"}])
    assert _run(monkeypatch, reg) == ["assemble"]


def test_a_registry_read_error_fails_open(monkeypatch):
    class _Boom(_Registry):
        def query(self, **kw):
            raise RuntimeError("ddb down")
    assert _run(monkeypatch, _Boom([])) == ["assemble"]


def test_the_watchdog_still_fails_closed_on_a_read_error(monkeypatch):
    class _Boom(_Registry):
        def query(self, **kw):
            raise RuntimeError("ddb down")
    monkeypatch.setattr(pc, "_job_table", _Boom([]))
    assert pc._node_has_inflight_registered_job("a", PID, "recast", 600) is True
