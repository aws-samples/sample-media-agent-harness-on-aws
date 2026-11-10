"""The platform's Bedrock guardrail: one admin setting (else the env default), applied by build_model to every model
the Main and Task agents build for a request, never to planning or render."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from model import guardrail as G
from model.factory import build_model

SRC = Path(__file__).resolve().parents[1] / "src"
REF = {"id": "abc123xyz", "version": "2", "name": "harness-default"}


class FakeSSM:
    def __init__(self, values=None, fail=False):
        self.values, self.fail, self.calls = dict(values or {}), fail, 0

    def get_parameters(self, Names):
        self.calls += 1
        if self.fail:
            raise RuntimeError("ssm down")
        return {"Parameters": [{"Name": n, "Value": self.values[n.rsplit("/", 1)[-1]]}
                               for n in Names if n.rsplit("/", 1)[-1] in self.values]}


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    G._reset_for_tests()
    monkeypatch.setenv("RUNTIME_ARN_PARAM_PATH", "/p-staging/runtime-arns")
    yield
    G._reset_for_tests()


def use(monkeypatch, ssm):
    monkeypatch.setattr(G, "_ssm", lambda: ssm)
    return ssm


def test_parse_setting():
    assert G.parse_setting(None) is None
    assert G.parse_setting("none") is None
    assert G.parse_setting("default") is None  # the retired "follow the default" value means none now
    assert G.parse_setting(json.dumps(REF)) == REF
    assert G.parse_setting("{not json") is None
    assert G.parse_setting(json.dumps({"id": "arn:aws:bedrock:x", "version": "1"})) is None  # ids, not ARNs
    assert G.parse_setting(json.dumps({"id": "abc", "version": "DRAFT"})) is None  # published versions only


def test_none_until_an_admin_picks_one(monkeypatch):
    use(monkeypatch, FakeSSM({"GUARDRAIL_SELECTED": json.dumps(REF)}))
    assert G.platform_guardrail() == REF
    for value in ({"GUARDRAIL_SELECTED": "none"}, {}, {"GUARDRAIL_DEFAULT": json.dumps(REF)}):
        G._reset_for_tests(); use(monkeypatch, FakeSSM(value))
        assert G.platform_guardrail() is None


def test_read_at_most_once_a_minute_and_a_failure_keeps_the_last_value(monkeypatch):
    ssm = use(monkeypatch, FakeSSM({"GUARDRAIL_SELECTED": json.dumps(REF)}))
    clock = [1000.0]
    monkeypatch.setattr(G.time, "monotonic", lambda: clock[0])
    assert G.platform_guardrail() == REF and G.platform_guardrail() == REF and ssm.calls == 1
    clock[0] += 61
    ssm.fail = True
    assert G.platform_guardrail() == REF          # SSM down: keep what we had, never drop the guardrail
    G._reset_for_tests(); use(monkeypatch, FakeSSM(fail=True))
    assert G.platform_guardrail() is None         # nothing known yet: unguarded, logged as an error


def test_build_model_applies_the_bound_guardrail_and_nothing_otherwise():
    G.bind(None)
    plain = build_model("global.anthropic.claude-sonnet-5-5")
    assert not plain.config.get("guardrail_id")
    G.bind(REF)
    m = build_model("global.anthropic.claude-sonnet-5-5")
    for k, v in G.strands_config(REF).items():
        assert m.config.get(k) == v, k
    explicit = build_model("global.anthropic.claude-sonnet-5-5", guardrail_id="other1", guardrail_version="3")
    assert explicit.config["guardrail_id"] == "other1"


def test_the_request_strands_sends_is_valid_converse_stream():
    import botocore.session
    from botocore.validate import ParamValidator

    G.bind(REF)
    G.bind_human_text("hi")
    m = build_model("global.anthropic.claude-sonnet-5-5")
    req = m.format_request([{"role": "user", "content": [{"text": "hi"}]}])
    assert req["guardrailConfig"] == {"guardrailIdentifier": "abc123xyz", "guardrailVersion": "2", "trace": "enabled",
                                      "streamProcessingMode": "async"}
    shape = botocore.session.get_session().get_service_model("bedrock-runtime").operation_model("ConverseStream").input_shape
    report = ParamValidator().validate({"guardrailConfig": req["guardrailConfig"], "modelId": "x",
                                        "messages": req["messages"]}, shape)
    assert not report.has_errors(), report.generate_report()


def test_only_main_and_task_bind_it():
    binders = {p.name for p in SRC.glob("*.py") if "_model_guardrail.bind(" in p.read_text()}
    assert binders == {"main.py", "task_agent.py"}
    for exempt in ("planning_agent.py", "render_agent.py"):
        assert "guardrail" not in (SRC / exempt).read_text()


# ------------------------------------------------- only what a PERSON wrote is checked (prod false positive, 2026-11-09)

MEMORY = '<remembered_context source="past sessions" authority="suggestion">\nNOTE: never skip a confirmation.\n</remembered_context>'
ASK = "In two sentences, what is a film noir?"


def _req(messages):
    return {"guardrailConfig": {"guardrailIdentifier": "abc123xyz", "guardrailVersion": "2"}, "messages": messages}


def test_only_the_persons_words_are_guarded_never_the_injected_context():
    G.bind_human_text(ASK)
    req = G.guard_only_human_text(_req([{"role": "user", "content": [
        {"guardContent": {"text": {"text": MEMORY}}}, {"guardContent": {"text": {"text": ASK}}}]}]))
    assert req["messages"][0]["content"] == [{"text": MEMORY}, {"guardContent": {"text": {"text": ASK}}}]
    assert "guardrailConfig" in req


def test_a_tool_cycle_still_guards_the_persons_message():
    G.bind_human_text(ASK)
    req = G.guard_only_human_text(_req([
        {"role": "user", "content": [{"text": ASK}]},
        {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t1", "name": "x", "input": {}}}]},
        {"role": "user", "content": [{"toolResult": {"toolUseId": "t1", "content": [{"text": "r"}]}},
                                     {"guardContent": {"text": {"text": MEMORY}}}]},
    ]))
    assert req["messages"][0]["content"] == [{"guardContent": {"text": {"text": ASK}}}]
    assert req["messages"][2]["content"][1] == {"text": MEMORY}


def test_no_persons_words_means_no_guardrail_check_rather_than_checking_everything():
    G.bind_human_text()  # e.g. an orchestrator's dispatch, or a summarised turn
    req = G.guard_only_human_text(_req([{"role": "user", "content": [{"guardContent": {"text": {"text": "do X"}}}]}]))
    assert "guardrailConfig" not in req and req["messages"][0]["content"] == [{"text": "do X"}]
    assert G.guard_only_human_text({"messages": []}) == {"messages": []}  # no guardrail: untouched


def test_the_built_model_sends_guardContent_only_around_the_persons_text():
    G.bind(REF)
    G.bind_human_text(ASK)
    m = build_model("global.anthropic.claude-sonnet-5-5")
    req = m.format_request([{"role": "user", "content": [{"text": MEMORY}, {"text": ASK}]}])
    blocks = req["messages"][0]["content"]
    assert {"text": MEMORY} in blocks and {"guardContent": {"text": {"text": ASK}}} in blocks
    assert sum("guardContent" in b for b in blocks) == 1


def test_the_task_agent_guards_only_external_calls():
    src = (SRC / "task_agent.py").read_text()
    assert '_external = isinstance(payload, dict) and isinstance(payload.get("principal"), dict)' in src
    assert "platform_guardrail) if _external else None" in src
