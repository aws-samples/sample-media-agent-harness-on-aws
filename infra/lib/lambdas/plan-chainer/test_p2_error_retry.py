"""P2 Tier-1 error-aware self-correction: a failed node is re-dispatched WITH its error
in context, bounded by MAX_NODE_RETRIES and an error-signature stop (a deterministic
failure won't fix itself by retrying the same way), so it cannot loop.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load():
    for name in ("JOB_REGISTRY_TABLE", "PLAN_STATE_TABLE", "GATEWAY_REGISTRY_TABLE", "SKILLS_S3_BUCKET"):
        os.environ.setdefault(name, "t")
    spec = importlib.util.spec_from_file_location("_chainer_p2", HERE / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_chainer_p2"] = mod
    spec.loader.exec_module(mod)
    return mod


pc = _load()


def test_retry_eligible_on_first_failure_with_error(monkeypatch):
    monkeypatch.setattr(pc, "MAX_NODE_RETRIES", 1)
    assert pc._should_retry_node({"status": "failed", "error": "duration must be >= 10", "retries": 0}) is True


def test_no_retry_without_an_error(monkeypatch):
    monkeypatch.setattr(pc, "MAX_NODE_RETRIES", 1)
    assert pc._should_retry_node({"status": "failed", "retries": 0}) is False
    assert pc._should_retry_node({"status": "failed", "error": "   ", "retries": 0}) is False


def test_no_retry_past_cap(monkeypatch):
    monkeypatch.setattr(pc, "MAX_NODE_RETRIES", 1)
    assert pc._should_retry_node({"error": "boom", "retries": 1}) is False


def test_error_signature_stop(monkeypatch):
    monkeypatch.setattr(pc, "MAX_NODE_RETRIES", 3)
    # same error after a correction attempt -> give up (no loop)
    assert pc._should_retry_node({"error": "boom", "retries": 1, "last_retry_error": "boom"}) is False
    # a genuinely NEW error -> keep self-correcting (still under the cap)
    assert pc._should_retry_node({"error": "different", "retries": 1, "last_retry_error": "boom"}) is True


def test_retry_failed_node_bumps_counter_and_reinjects_error(monkeypatch):
    calls = {}

    class _T:
        def update_item(self, **kw):
            calls["update"] = kw

    monkeypatch.setattr(pc, "_plan_table", _T())
    monkeypatch.setattr(pc, "_dispatch_node", lambda *a, **k: calls.setdefault("dispatch", k))
    row = {"error": "duration must be >= 10, got 5", "retries": 0}
    pc._retry_failed_node("sess", "plan", "actor", {"id": "n1"}, [{"id": "n1"}], {"n1": row}, row)
    vals = calls["update"]["ExpressionAttributeValues"]
    assert vals[":n"] == 1                                   # retries bumped
    assert vals[":r"] == "running"                           # not terminal while retrying
    assert vals[":e"] == "duration must be >= 10, got 5"     # error recorded for signature stop
    assert calls["update"]["Key"]["node_id"] == "n1"         # bare node id (matches live table)
    assert calls["dispatch"]["retry_error"] == "duration must be >= 10, got 5"  # re-dispatched WITH the error


def test_missing_input_error_is_not_retried(monkeypatch):
    # Design §5: a missing / unresolvable INPUT is a handoff gap, not a correctable parameter —
    # retrying by guessing a key just fails differently, so fail fast instead.
    monkeypatch.setattr(pc, "MAX_NODE_RETRIES", 1)
    for err in (
        "NoSuchKey: the specified key does not exist",
        "input file final_transcript.json does not exist",
        "could not resolve reference to author_summary",
        "gateway result does not contain an s3 uri",
        "no s3 uri provided for the media input",
        "referenced file not found",
    ):
        assert pc._is_missing_input_error(err) is True
        assert pc._should_retry_node({"status": "failed", "error": err, "retries": 0}) is False


def test_correctable_parameter_error_is_still_retried(monkeypatch):
    # A wrong/empty PARAMETER the agent CAN fix is still eligible (regression guard).
    monkeypatch.setattr(pc, "MAX_NODE_RETRIES", 1)
    for err in ("duration must be >= 10", "invalid aspect_ratio '3:5'", "empty output produced"):
        assert pc._is_missing_input_error(err) is False
        assert pc._should_retry_node({"status": "failed", "error": err, "retries": 0}) is True


# --- Validity-based node terminalization: _result_error_text -------------------
# Keystone for self-healing: a job can COMPLETE (SFN SUCCEEDED / MCP returned)
# while its body is a failure (fal 422, M2C status:FAILED, MCP isError,
# moderation). These must resolve the node FAILED — visible to _should_retry_node
# — instead of being laundered "completed" (which hid the 8 animate 422s).

def test_result_error_detects_http_status_bodies():
    assert pc._result_error_text({"status": 422, "detail": "duration must be auto or 4..30"})
    assert pc._result_error_text({"statusCode": 422, "detail": "bad"})
    assert pc._result_error_text({"http_status": 500})
    assert pc._result_error_text({"statusCode": "422"})


def test_result_error_detects_mcp_iserror():
    assert pc._result_error_text({"isError": True, "content": [{"text": "boom"}]})


def test_result_error_detects_workflow_failed_status():
    assert pc._result_error_text({"status": "FAILED", "error": "stem sep crashed"})
    assert pc._result_error_text({"status": "TIMED_OUT"})


def test_result_error_detects_populated_error_field():
    assert pc._result_error_text({"error": "file_download_error"})


def test_result_error_detects_content_policy():
    assert pc._result_error_text('{"detail":"content_policy_violation"}') == "content_policy_violation"
    assert pc._result_error_text(
        {"content": [{"type": "text", "text": "content_policy_violation: denied"}]}
    ) == "content_policy_violation"


def test_result_error_detects_provider_access_rejection():
    """A wrong / unentitled endpoint id (fal 'unavailable for the connected account' /
    insufficient_scope) is returned on a COMPLETED call (run 863816b4). It must resolve
    the node FAILED, not launder to SUCCEEDED, regardless of which key carries it."""
    # top-level error field (fal get_model_schema-style rejection)
    assert pc._result_error_text(
        {"error": "This endpoint is unavailable for the connected account. "
                  "Select an accessible model before submitting."}
    )
    # nested under a non-'error' key (detail) — caught by the serialized scan
    assert pc._result_error_text({"detail": "insufficient_scope"})
    # inside an MCP content list
    assert pc._result_error_text(
        {"content": [{"type": "text", "text": "insufficient_scope: endpoint unavailable"}]}
    )
    # bare strings
    assert pc._result_error_text("insufficient_scope")
    assert pc._result_error_text("This endpoint is unavailable for the connected account")


def test_provider_access_error_gets_one_self_correcting_retry_then_terminal():
    """Deliberate: a provider access rejection (a WRONG endpoint id) is NOT fail-fast.

    Unlike a content-policy block on a handed-in image (which retry can't fix), the
    agent CAN fix a wrong id — drop the bad `fal-ai/` prefix / pick an accessible
    model — so it gets ONE error-aware retry. The cap + error-signature stop then
    terminalise it, so there is no loop (contrast run 863816b4's 236 in-turn submits,
    which L1a's ceiling caps separately)."""
    assert pc._should_retry_node(
        {"error": "This endpoint is unavailable for the connected account", "retries": 0}
    ) is True
    # the SAME error again does not retry — error-signature stop => terminal FAILED
    assert pc._should_retry_node(
        {"error": "This endpoint is unavailable for the connected account",
         "retries": 1,
         "last_retry_error": "This endpoint is unavailable for the connected account"}
    ) is False


def test_result_fetch_failure_is_not_retried_no_resubmit():
    """Fix 2 (P0-refined): a GENUINE post-submit fetch failure (carries upstream_request_id /
    http_status) must fail-fast — re-dispatching re-submits a fresh paid generation and loops
    (45x observed on 6c69afb4)."""
    row = {"error": '{"error":"Result fetch failed (404)","http_status":404,'
                    '"upstream_request_id":"01a0b5c7-faa3-7870-940d-11662e875c21"}'}
    assert pc._is_result_fetch_error(row["error"])
    assert pc._should_retry_node(row) is False  # terminal -> no re-submit, plan settles


def test_wrong_endpoint_result_fetch_404_stays_retryable():
    """P0: the CORRECTABLE wrong-endpoint variant ('Path ... not found', no upstream_request_id)
    is a submit-time error the model can re-emit correctly — it must NOT be fail-fasted, or a
    recoverable run dies (regression: e7292dc1 gen_shot1-8 all recovered on retry)."""
    err = ('{"status":"error","error":"Result fetch failed (404): '
           '{\\"detail\\":\\"Path /h3-max/image-to-video not found\\"}"}')
    assert pc._is_result_fetch_error(err) is False        # not the burn variant
    assert pc._should_retry_node({"error": err}) is True  # first attempt still gets its retry


def test_422_validation_result_fetch_stays_retryable():
    """P0: a (422) validation error on the parameters is correctable, not a post-submit fetch
    burn — it must stay retry-eligible."""
    err = ('{"status":"error","error":"Result fetch failed (422): '
           '{\\"detail\\":[{\\"type\\":\\"value_error\\"}]}"}')
    assert pc._is_result_fetch_error(err) is False
    assert pc._should_retry_node({"error": err}) is True


def test_varying_payload_error_signature_stops_even_when_retries_reset():
    """Fix 1: a 404 with a fresh upstream_request_id each attempt used to defeat the
    'same error -> stop' guard, and the changefeed bridge can reset ``retries`` to 0. The
    NORMALISED signatures are equal, so the stop fires regardless of the (reset) counter."""
    e1 = '{"error":"boom","upstream_request_id":"01a0b5c7-faa3-7870-940d-11662e875c21"}'
    e2 = '{"error":"boom","upstream_request_id":"01a0b5f3-01b9-7001-893b-ed8a6b7192b3"}'
    assert pc._normalize_error_signature(e1) == pc._normalize_error_signature(e2)
    assert pc._should_retry_node({"error": e2, "retries": 0, "last_retry_error": e1}) is False


def test_first_correctable_error_still_gets_its_one_retry():
    """A correctable parameter error with no prior retry recorded still gets its single
    self-correction attempt (the guards must not over-suppress)."""
    assert pc._should_retry_node(
        {"error": "ValidationException: duration must be 4..30", "retries": 0}) is True


def test_result_error_passes_valid_success():
    # M2C success shape — the common case, must NOT be flagged
    assert pc._result_error_text({"status": "SUCCEEDED", "outputs": [{"uri": "s3://x", "label": "meta"}]}) is None
    # non-terminal / other statuses
    assert pc._result_error_text({"status": "RUNNING"}) is None
    # empty / null / falsy error fields are NOT failures
    assert pc._result_error_text({"status": "SUCCEEDED", "error": None}) is None
    assert pc._result_error_text({"error": ""}) is None
    assert pc._result_error_text({"error": []}) is None
    assert pc._result_error_text({"error": {}}) is None
    # a 2xx status code is fine
    assert pc._result_error_text({"statusCode": 200, "outputs": []}) is None
    # bare prose that merely contains "error" must not be flagged
    assert pc._result_error_text("Transcription complete: 2 speakers, no errors") is None
    # non-dict / non-str inputs
    assert pc._result_error_text(None) is None
    assert pc._result_error_text([1, 2, 3]) is None


def test_error_body_then_retry_gate_engages():
    # End-to-end of the fix's intent: an error body → FAILED-with-error → retry-eligible.
    err = pc._result_error_text({"status": 422, "detail": "duration '3' not permitted"})
    assert err
    assert pc._should_retry_node({"status": "failed", "error": err, "retries": 0}) is True
    # and once the SAME error repeats after a correction attempt, it terminates (no loop)
    assert pc._should_retry_node({"status": "failed", "error": err, "retries": 1, "last_retry_error": err}) is False


def test_content_policy_is_not_retryable(monkeypatch):
    # A provider content-policy rejection is non-correctable — re-dispatching re-submits
    # the SAME flagged image and 422s again (the shot6 loop). Must NOT be retry-eligible.
    monkeypatch.setattr(pc, "MAX_NODE_RETRIES", 1)
    cp = ("Content policy violation (422): the provided image_url was rejected by the model "
          "provider's partner validation as potentially containing likenesses of real people")
    assert pc._is_content_policy_error(cp) is True
    assert pc._should_retry_node({"status": "failed", "error": cp, "retries": 0}) is False
    # variants
    assert pc._is_content_policy_error("content_policy_violation") is True
    assert pc._is_content_policy_error("blocked by content filter") is True
    # a genuine correctable PARAMETER error is still retryable (regression guard)
    assert pc._is_content_policy_error("duration must be one of auto, 4..30") is False
    assert pc._should_retry_node({"status": "failed", "error": "duration must be 4-30", "retries": 0}) is True


# --- P3: a terminally-FAILED gateway producer must not count as succeeded --------------------
# A node is marked "completed" when it SUBMITS its fire-and-forget SFN, so a producer can read
# "completed" while its Step Functions execution later FAILS (probe_media_file 404 on a corrupted
# source). _node_gateway_failed lets the readiness gate exclude it, so a dependent (sg4) is not
# dispatched against an output that never materialised.

class _FakeJobTable:
    def __init__(self, status):
        self._status = status

    def get_item(self, Key):
        if self._status is None:
            return {}
        return {"Item": {"jobId": Key["jobId"], "status": self._status}}


_SFN = "arn:aws:states:us-west-2:100000000002:execution:m2cv6-media-processing:037ec05c"


def test_node_gateway_failed_true_on_terminal_failure(monkeypatch):
    for st in ("FAILED", "TIMED_OUT", "ABORTED", "failed"):
        monkeypatch.setattr(pc, "_job_table", _FakeJobTable(st))
        assert pc._node_gateway_failed({"job_id": _SFN}) is True, st


def test_node_gateway_failed_false_on_success_or_running(monkeypatch):
    for st in ("SUCCEEDED", "completed", "RUNNING", "PENDING"):
        monkeypatch.setattr(pc, "_job_table", _FakeJobTable(st))
        assert pc._node_gateway_failed({"job_id": _SFN}) is False, st


def test_node_gateway_failed_is_registry_based(monkeypatch):
    # Registry-based: an unregistered coordinator id / no-job is not "failed"; a REGISTERED fal
    # queue job that FAILED is (so its dependents are skipped, not run against a phantom output).
    class _Reg:
        def __init__(self, reg):
            self.reg = reg

        def get_item(self, Key):
            jid = Key["jobId"]
            return {"Item": {"jobId": jid, "status": self.reg[jid]}} if jid in self.reg else {}

    monkeypatch.setattr(pc, "_job_table", _Reg({}))
    assert pc._node_gateway_failed({"job_id": "plan#node"}) is False   # coordinator id, not registered
    assert pc._node_gateway_failed({}) is False                          # no gateway job at all
    monkeypatch.setattr(pc, "_job_table", _Reg({"req_abc": "FAILED"}))
    assert pc._node_gateway_failed({"job_id": "req_abc"}) is True        # registered fal job, FAILED
    monkeypatch.setattr(pc, "_job_table", _FakeJobTable(None))
    assert pc._node_gateway_failed({"job_id": _SFN}) is False           # job row absent -> fail-open


# --- #2-data: _mark_node_failed must mirror the reason onto the job-registry row -------------
# The Jobs panel / failed-notification reads the job row, whose `lastError` was null (the reason
# lived only on plan-state) — so a failed-job click surfaced nothing. _mark_node_failed now writes
# lastError (the canonical attribute the proxy projects to job.lastError -> the notification).

class _CaptureTable:
    def __init__(self):
        self.calls = []

    def update_item(self, **kw):
        self.calls.append(kw)


def test_mark_node_failed_mirrors_reason_onto_job_row(monkeypatch):
    plan_t, job_t = _CaptureTable(), _CaptureTable()
    monkeypatch.setattr(pc, "_plan_table", plan_t)
    monkeypatch.setattr(pc, "_job_table", job_t)
    monkeypatch.setattr(pc, "_slide_plan_ttl", lambda *a, **k: None)

    pc._mark_node_failed("sess", "plan1", "separate_stems", "missing input: trimmed clip URI")

    jw = job_t.calls[-1]
    assert jw["Key"]["jobId"] == "plan1#separate_stems"
    assert "lastError" in jw["UpdateExpression"]                       # reason mirrored to job row
    assert jw["ExpressionAttributeValues"][":s"] == "FAILED"
    assert jw["ExpressionAttributeValues"][":reason"].startswith("missing input: trimmed clip URI")
    # plan-state still records it too (unchanged)
    assert plan_t.calls[-1]["ExpressionAttributeValues"][":reason"] == "missing input: trimmed clip URI"


def test_mark_node_failed_defaults_reason_when_empty(monkeypatch):
    plan_t, job_t = _CaptureTable(), _CaptureTable()
    monkeypatch.setattr(pc, "_plan_table", plan_t)
    monkeypatch.setattr(pc, "_job_table", job_t)
    monkeypatch.setattr(pc, "_slide_plan_ttl", lambda *a, **k: None)
    pc._mark_node_failed("sess", "plan1", "n1", "")
    assert job_t.calls[-1]["ExpressionAttributeValues"][":reason"] == "node failed"


def test_no_retry_for_a_node_the_bridge_failed_because_its_job_failed(monkeypatch):
    # The changefeed bridge now writes the job's real reason with gateway_status FAILED
    # (staging plan-shotdiar-render, sg2_diarize_speech). That deterministic provider failure
    # must stay terminal, as it was when the bridge wrote no error at all.
    monkeypatch.setattr(pc, "MAX_NODE_RETRIES", 1)
    for err in ("No VAD turns found after flattening — check VAD output (ValueError)",
                "job FAILED"):
        assert pc._should_retry_node({"status": "failed", "error": err, "retries": 0,
                                      "gateway_status": "FAILED", "job_id": "arn:x"}) is False
    # The same text without a bridge-recorded job failure keeps the existing P2 behaviour.
    assert pc._should_retry_node({"status": "failed", "error": "duration must be >= 10",
                                  "retries": 0, "gateway_status": "SUCCEEDED"}) is True


def test_no_retry_for_a_node_that_could_not_be_built(monkeypatch):
    # The same payload builds the same way: re-dispatching only doubles the wait (plan 593333b5).
    monkeypatch.setattr(pc, "MAX_NODE_RETRIES", 1)
    err = "Node setup failed: ValueError: Failed to load tool <X>: Tool name 'a___submit_job' already exists"
    assert pc._should_retry_node({"status": "failed", "error": err, "retries": 0}) is False


def test_setup_prefix_matches_the_task_agent():
    src = (HERE.parents[3] / "agent" / "src" / "task_agent.py").read_text()
    assert 'NODE_SETUP_FAILED = "Node setup failed"' in src


def test_a_consumer_is_not_given_a_producer_tool_it_already_has_under_its_other_name():
    # "<target>___submit_job" and "submit_job" are one tool: listing both crashes the node agent (plan 593333b5).
    nodes = [{"id": "p", "tools": ["usr-1___submit_job"]},
             {"id": "c", "tools": ["submit_job"], "depends_on": ["p"]}]
    tools, note = pc._ensure_node_can_reach_its_inputs(nodes[1], nodes)
    assert tools == ["submit_job"] and note == ""


def test_a_job_whose_input_the_provider_refused_is_retried_once(monkeypatch):
    """8e2acba8: fal refused a recast clip's input (`value_error (video_url)`, nothing ran or billed) and the whole
    plan ended. An input rejection is correctable with its reason; a job that RAN and failed stays terminal."""
    monkeypatch.setattr(pc, "MAX_NODE_RETRIES", 1)
    row = {"status": "failed", "retries": 0, "gateway_status": "FAILED", "job_id": "req-1",
           "error": "The model rejected this job: value_error (video_url). No output was produced."}
    assert pc._should_retry_node(row) is True
    assert pc._should_retry_node({**row, "retries": 1}) is False  # bounded
    assert pc._should_retry_node({**row, "last_retry_error": row["error"]}) is False  # same error: stop
    assert pc._should_retry_node({**row, "error": "422: content_policy_violation (likeness)"}) is False
