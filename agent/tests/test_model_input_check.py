"""Schema-driven model submits: a call is checked against the model's OWN schema (``get_model_schema``),
never rewritten by endpoint-family rules.

The bug this replaces: ``normalize_minimax_h3_args`` added ``prompt_expansion_mode`` to every
``minimax/h3-max/*`` call. image-to-video REQUIRES it; recast's schema (``additionalProperties: false``)
forbids it, so every recast submit 422'd, and an agent "fixed" it by switching to ``submit_jobs``, whose jobs
the registry cannot see. Fixtures are fal's published OpenAPI documents for the two endpoints.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import model_input_check as mic  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "fal_schemas")
RECAST = "minimax/h3-max/recast"
I2V = "minimax/h3-max/image-to-video"


def _doc(name):
    with open(os.path.join(FIX, f"h3-max-{name}.json")) as f:
        return json.load(f)


def _schema(name, endpoint):
    s = mic.input_schema(_doc(name), endpoint)
    assert s is not None
    return s


# ---------------------------------------------------------------- reading the schema
def test_reads_the_input_schema_from_falls_openapi_document():
    s = _schema("recast", RECAST)
    assert s["additionalProperties"] is False
    assert set(s["required"]) == {"video_url", "reference_image_urls"}
    assert "prompt_expansion_mode" not in s["properties"]
    i2v = _schema("image-to-video", I2V)
    assert "prompt_expansion_mode" in i2v["required"]


def test_reads_a_wrapped_or_bare_json_schema_and_a_json_text_payload():
    bare = _schema("recast", RECAST)
    assert mic.input_schema({"input_schema": bare}, RECAST)["required"] == bare["required"]
    assert mic.input_schema(json.dumps(_doc("recast")), RECAST)["required"] == bare["required"]
    assert mic.input_schema({"schema": _doc("recast")}, RECAST)["required"] == bare["required"]
    assert mic.input_schema({"nothing": 1}, RECAST) is None
    assert mic.input_schema("not json", RECAST) is None


def test_payload_of_reads_structured_content_json_blocks_and_json_text():
    d = {"a": 1}
    assert mic.payload_of({"status": "success", "structuredContent": d}) == d
    assert mic.payload_of({"status": "success", "content": [{"json": d}]}) == d
    assert mic.payload_of({"status": "success", "content": [{"text": json.dumps(d)}]}) == d
    assert mic.payload_of({"status": "error", "content": [{"text": json.dumps(d)}]}) is None


# ---------------------------------------------------------------- what the check catches
def test_the_recast_payload_from_the_bug_passes_as_sent():
    good = {"video_url": "https://x/v.mp4", "reference_image_urls": ["https://x/r.jpg"],
            "resolution": "1080P", "enhance_realism": True}
    assert mic.problems(_schema("recast", RECAST), good) == []


def test_recast_with_prompt_expansion_mode_is_caught_before_fal_sees_it():
    bad = {"video_url": "v", "reference_image_urls": ["r"], "prompt_expansion_mode": "disabled"}
    assert mic.problems(_schema("recast", RECAST), bad) == ["`prompt_expansion_mode` is not a field of this model"]


def test_missing_required_and_out_of_enum_values():
    p = mic.problems(_schema("recast", RECAST), {"video_url": "v", "resolution": "480P"})
    assert "`reference_image_urls` is required" in p
    assert any(x.startswith("`resolution` must be one of \"768P\", \"1080P\"") for x in p)
    # image-to-video: prompt_expansion_mode is REQUIRED there (the same family, the opposite rule).
    assert "`prompt_expansion_mode` is required" in mic.problems(_schema("image-to-video", I2V), {"prompt": "p"})


def test_a_schema_that_allows_extra_fields_never_refuses_them():
    i2v = _schema("image-to-video", I2V)  # no additionalProperties:false
    assert mic.problems(i2v, {"prompt": "p", "prompt_expansion_mode": "disabled", "anything": 1}) == []


def test_accepted_fields_lists_required_enums_and_examples():
    text = mic.accepted_fields(_schema("recast", RECAST))
    assert "video_url (required)" in text and "reference_image_urls (required)" in text
    assert "resolution (768P|1080P)" in text
    assert "prompt_expansion_mode" not in text
    assert "prompt_expansion_mode (required; e.g. disabled|balanced|quality)" in mic.accepted_fields(
        _schema("image-to-video", I2V))


def test_submissions_cover_single_and_batch_tools():
    one = {"endpoint_id": RECAST, "input": {"video_url": "v"}}
    assert mic.submissions("submit_job", one) == [("input", RECAST, {"video_url": "v"})]
    assert mic.submissions("run_model", one) == [("input", RECAST, {"video_url": "v"})]
    batch = {"requests": [{"index": 3, "endpoint_id": RECAST, "input": {"a": 1}}, {"endpoint_id": "", "input": {}}]}
    assert mic.submissions("submit_jobs", batch) == [("requests[3].input", RECAST, {"a": 1})]
    assert mic.submissions("check_job", one) == []


def test_schema_tool_is_the_sibling_on_the_same_target():
    assert mic.schema_tool_name("usr-abc___submit_job") == "usr-abc___get_model_schema"
    assert mic.schema_tool_name("submit_job") == "get_model_schema"


# ---------------------------------------------------------------- the wrapper path
class _FakeClient:
    """Answers get_model_schema from the fixtures and counts calls."""

    def __init__(self, docs=None, fail=False):
        self.calls = []
        self._docs = docs if docs is not None else {RECAST: _doc("recast"), I2V: _doc("image-to-video")}
        self._fail = fail

    async def call_tool_async(self, tool_use_id, name, arguments=None, read_timeout_seconds=None, **kw):
        self.calls.append((name, dict(arguments or {})))
        if self._fail:
            raise RuntimeError("schema tool down")
        doc = self._docs.get(arguments["endpoint_id"])
        if doc is None:
            return {"toolUseId": tool_use_id, "status": "error", "content": [{"text": "not found"}]}
        return {"toolUseId": tool_use_id, "status": "success", "content": [{"text": json.dumps(doc)}]}


def _tool(name, client):
    from jobs import strands_tool_wrap as stw

    tool = stw.ResilientMCPAgentTool.__new__(stw.ResilientMCPAgentTool)
    tool._agent_tool_name = name
    tool._call_lease = None
    tool.mcp_client = client
    return tool


def _drive(tool, tool_input, monkeypatch):
    from jobs import strands_tool_wrap as stw
    from strands.types._events import ToolResultEvent

    sent = []

    async def fake_stream(self, tool_use, invocation_state, **kwargs):
        sent.append(tool_use["input"])
        yield ToolResultEvent({"toolUseId": tool_use["toolUseId"], "status": "success",
                               "content": [{"text": "{\"request_id\": \"r1\"}"}]})

    monkeypatch.setattr(stw.MCPAgentTool, "stream", fake_stream)

    async def run():
        return [ev async for ev in tool._base_stream_once(
            {"toolUseId": "t1", "name": tool.tool_name, "input": tool_input}, {}, {})]

    events = contextvars.copy_context().run(asyncio.run, run())
    return [ev.tool_result for ev in events], sent


def setup_function(_):
    mic.clear_cache()


def test_recast_goes_through_unchanged_no_field_is_injected(monkeypatch):
    client = _FakeClient()
    payload = {"endpoint_id": RECAST, "input": {"video_url": "https://x/v.mp4",
                                                "reference_image_urls": ["https://x/r.jpg"],
                                                "resolution": "1080P", "enhance_realism": True}}
    results, sent = _drive(_tool("usr-1___submit_job", client), payload, monkeypatch)
    assert results[0]["status"] == "success"
    assert sent == [payload]  # exactly what the model sent: no prompt_expansion_mode
    assert client.calls == [("usr-1___get_model_schema", {"endpoint_id": RECAST})]


def test_a_bad_field_is_refused_before_the_call_and_names_what_the_model_accepts(monkeypatch):
    client = _FakeClient()
    payload = {"endpoint_id": RECAST, "input": {"video_url": "v", "reference_image_urls": ["r"],
                                                "prompt_expansion_mode": "disabled"}}
    results, sent = _drive(_tool("usr-1___run_model", client), payload, monkeypatch)
    assert sent == []  # nothing reached fal
    r = results[0]
    assert r["status"] == "error" and r["toolUseId"] == "t1"
    text = r["content"][0]["text"]
    assert "nothing was sent or billed" in text
    assert "`prompt_expansion_mode` is not a field of this model" in text
    assert "video_url (required)" in text and "Call run_model again" in text


def test_batch_submits_are_checked_per_request(monkeypatch):
    client = _FakeClient()
    payload = {"user_intent": "x", "requests": [
        {"index": 0, "endpoint_id": RECAST, "input": {"video_url": "v", "reference_image_urls": ["r"]}},
        {"index": 1, "endpoint_id": I2V, "input": {"prompt": "p"}},
    ]}
    results, sent = _drive(_tool("usr-1___submit_jobs", client), payload, monkeypatch)
    assert sent == []
    text = results[0]["content"][0]["text"]
    assert "minimax/h3-max/image-to-video (requests[1].input): `prompt_expansion_mode` is required" in text
    assert RECAST + " (requests[0].input)" not in text  # the valid request is not reported


def test_the_schema_is_fetched_once_per_endpoint(monkeypatch):
    client = _FakeClient()
    payload = {"endpoint_id": RECAST, "input": {"video_url": "v", "reference_image_urls": ["r"]}}
    for _ in range(3):
        _drive(_tool("usr-1___submit_job", client), payload, monkeypatch)
    assert len(client.calls) == 1


def test_fails_open_when_the_schema_cannot_be_read(monkeypatch):
    for client in (_FakeClient(fail=True), _FakeClient(docs={})):
        mic.clear_cache()
        payload = {"endpoint_id": RECAST, "input": {"anything": 1}}
        results, sent = _drive(_tool("usr-1___submit_job", client), payload, monkeypatch)
        assert results[0]["status"] == "success" and sent == [payload]


def test_reads_and_other_tools_are_never_checked(monkeypatch):
    client = _FakeClient()
    _drive(_tool("usr-1___check_job", client), {"endpoint_id": RECAST, "request_id": "r"}, monkeypatch)
    _drive(_tool("usr-1___get_model_schema", client), {"endpoint_id": RECAST}, monkeypatch)
    assert client.calls == []


def test_the_kill_switch_sends_everything_unchecked(monkeypatch):
    monkeypatch.setenv("MODEL_INPUT_CHECK", "off")
    client = _FakeClient()
    payload = {"endpoint_id": RECAST, "input": {"prompt_expansion_mode": "disabled"}}
    results, sent = _drive(_tool("usr-1___submit_job", client), payload, monkeypatch)
    assert sent == [payload] and client.calls == []
