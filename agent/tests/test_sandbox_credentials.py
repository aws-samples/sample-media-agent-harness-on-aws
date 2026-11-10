"""Scoped sandbox credentials (asset-lineage Phase 3 (b), design §9.2).

- the ``AssumeRole`` request validates against botocore's own STS model (no creds, no network);
- the credentials are cached per (project, session) and refreshed under 45 minutes of life, so a call never starts on short-lived credentials;
- no role configured → no STS call; an STS failure → ``None``, never a raise, never a logged secret;
- the injection program writes a 0600 ``~/.aws/credentials`` default profile and prints only its sentinel;
- the code_interpreter wrapper installs them in the TARGET sandbox through the interpreter client — the
  model-visible tool input and the tool result never carry them — and re-injects only when the
  fingerprint changes;
- FAIL CLOSED (B2): with the role configured, ANY failure (no session, STS refused, sandbox call error,
  no confirmation, an interpreter shape change) answers with an error result and the model's code
  never runs — the sandbox's own role has no artifact/project S3 access to fall back to;
- ``crop_video`` and the timeline media actions inject into their own sandboxes, and refuse the same way.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import botocore.session
import pytest
from botocore.validate import ParamValidator

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import sandbox_credentials as sc  # noqa: E402

ARN = "arn:aws:iam::100000000002:role/mediaworks-harness-sandbox-scoped-s3"
PID = "p" + "c" * 32
SID = "4f9d0d02-1111-4222-8333-444455556666"
SECRET = "SECRETVALUEnever-logged/xyz"


def _creds(minutes=60, key="ASIAEXAMPLE"):
    return {"AccessKeyId": key, "SecretAccessKey": SECRET, "SessionToken": "TOKEN" * 10,
            "Expiration": datetime.now(timezone.utc) + timedelta(minutes=minutes)}


class _FakeSts:
    def __init__(self, fail=False, minutes=60):
        self.calls, self.fail, self.minutes = [], fail, minutes

    def assume_role(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise RuntimeError(f"AccessDenied {SECRET}")
        return {"Credentials": _creds(self.minutes, key=f"ASIA{len(self.calls)}")}


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    sc._reset_for_tests()
    yield
    sc._reset_for_tests()


@pytest.fixture
def sts(monkeypatch):
    fake = _FakeSts()
    monkeypatch.setenv(sc.ROLE_ARN_ENV, ARN)
    monkeypatch.setattr(sc, "_client", lambda: fake)
    return fake


def test_assume_role_params_validate_against_the_sts_model():
    model = botocore.session.get_session().get_service_model("sts")
    shape = model.operation_model("AssumeRole").input_shape
    for pid in (PID, None):
        report = ParamValidator().validate(sc.assume_role_params(pid, SID, ARN), shape)
        assert not report.has_errors(), report.generate_report()
    p = sc.assume_role_params(PID, SID, ARN)
    assert p["Tags"] == [{"Key": "project_id", "Value": PID}, {"Key": "session_id", "Value": SID}]
    assert p["DurationSeconds"] == 3600 and len(p["RoleSessionName"]) <= 64
    assert sc.assume_role_params(None, SID, ARN)["Tags"][0] == {"Key": "project_id", "Value": "none"}


def test_no_role_means_no_sts_call(monkeypatch):
    monkeypatch.delenv(sc.ROLE_ARN_ENV, raising=False)
    monkeypatch.setattr(sc, "_client", lambda: pytest.fail("STS must not be called"))
    assert sc.credentials_for(PID, SID) is None


def test_cached_per_project_and_session_and_refreshed(sts):
    a = sc.credentials_for(PID, SID)
    assert sc.credentials_for(PID, SID) is a and len(sts.calls) == 1
    sc.credentials_for(PID, "7e1f0a0b-1111-4222-8333-444455556666")
    sc.credentials_for(None, SID)
    assert len(sts.calls) == 3, "one STS session per (project, session)"
    soon = datetime.now(timezone.utc) + timedelta(minutes=10)  # 50 min left: still served
    assert sc.credentials_for(PID, SID, now=soon) is a and len(sts.calls) == 3
    later = datetime.now(timezone.utc) + timedelta(minutes=20)  # < 45 min left
    b = sc.credentials_for(PID, SID, now=later)
    assert b is not a and len(sts.calls) == 4


def test_served_credentials_always_leave_a_long_call_window(monkeypatch):
    """A long encode must not start on credentials about to expire (no in-sandbox refresh exists)."""
    monkeypatch.setenv(sc.ROLE_ARN_ENV, ARN)
    clock = {"now": datetime.now(timezone.utc)}

    class _ClockSts:
        def assume_role(self, **kw):  # STS issues a full hour from the CURRENT time
            return {"Credentials": {"AccessKeyId": "ASIAX", "SecretAccessKey": SECRET, "SessionToken": "t",
                                    "Expiration": clock["now"] + timedelta(hours=1)}}

    monkeypatch.setattr(sc, "_client", lambda: _ClockSts())
    start = clock["now"]
    for minutes in range(0, 180, 3):
        clock["now"] = start + timedelta(minutes=minutes)
        c = sc.credentials_for(PID, SID, now=clock["now"])
        assert c["Expiration"] - clock["now"] >= timedelta(minutes=45), minutes


def test_failure_returns_none_and_never_logs_a_secret(monkeypatch, caplog):
    monkeypatch.setenv(sc.ROLE_ARN_ENV, ARN)
    monkeypatch.setattr(sc, "_client", lambda: _FakeSts(fail=True))
    with caplog.at_level(logging.DEBUG):
        assert sc.credentials_for(PID, SID) is None
    assert "assume failed" in caplog.text and SECRET not in caplog.text


def test_success_log_carries_no_credential(sts, caplog):
    with caplog.at_level(logging.DEBUG):
        c = sc.credentials_for(PID, SID)
    assert c["AccessKeyId"] not in caplog.text and SECRET not in caplog.text


def test_injection_program_writes_a_private_default_profile(monkeypatch):
    with tempfile.TemporaryDirectory() as home:
        code = sc.injection_code(_creds()) + "\nimport os; print('MODE', oct(os.stat(os.path.expanduser('~/.aws/credentials')).st_mode & 0o777))"
        out = subprocess.run([sys.executable, "-c", code], env={**os.environ, "HOME": home},
                             capture_output=True, text=True, check=True).stdout
        assert out.strip().splitlines() == [sc.INSTALLED_SENTINEL, "MODE 0o600"], "prints only the sentinel"
        text = open(os.path.join(home, ".aws", "credentials")).read()
        assert text.startswith("[default]\n") and SECRET in text and "aws_session_token" in text


# ------------------------------------------------------------------ the code_interpreter wrapper
def _stdout_response(text):
    """The `invoke("executeCode")` response shape (a `stream` of result events) carrying `text` as stdout."""
    return {"stream": [{"result": {"structuredContent": {"stdout": text}, "content": [{"type": "text", "text": text}]}}]}


class _Client:
    """A sandbox client whose kernel runs the injection program (prints the sentinel) unless told not to."""

    def __init__(self, confirm=True, raises=None):
        self.invokes = []
        self.confirm, self.raises = confirm, raises

    def invoke(self, method, params):
        self.invokes.append((method, params))
        if self.raises is not None:
            raise self.raises
        printed = sc.INSTALLED_SENTINEL if self.confirm and sc.INSTALLED_SENTINEL in params.get("code", "") else ""
        return _stdout_response(printed)


class _Info:
    def __init__(self, sid="aws-sess-1"):
        self.session_id, self.client = sid, _Client()


class _FakeInterpreter:
    def __init__(self):
        self._sessions: dict = {}
        self.default_session = "session-default"
        self.calls = []

    def _ensure_session(self, name):
        target = name or self.default_session
        self._sessions.setdefault(target, _Info())
        return target, None

    def code_interpreter(self, code_interpreter_input):
        self.calls.append(code_interpreter_input)
        return {"status": "success", "content": [{"text": "ok"}]}


class _InnerTool:
    """Stands in for strands' DecoratedFunctionTool: a bound `_tool_func` and an async `stream`."""

    tool_name = "code_interpreter"
    tool_spec = {"name": "code_interpreter"}
    tool_type = "python"

    def __init__(self, ci):
        self._tool_func = ci.code_interpreter
        self.seen = []

    async def stream(self, tool_use, invocation_state, **kw):
        self.seen.append(tool_use)
        yield {"result": self._tool_func(tool_use["input"]["code_interpreter_input"])}


def _use(code="print(1)", session=None):
    action = {"type": "executeCode", "code": code, "language": "python"}
    if session:
        action["session_name"] = session
    return {"toolUseId": "t1", "name": "code_interpreter", "input": {"code_interpreter_input": {"action": action}}}


def _run(tool, use):
    async def go():
        return [e async for e in tool.stream(use, {})]
    return asyncio.run(go())


@pytest.fixture
def wrapped(sts, monkeypatch):
    from jobs import context as jc
    from tool_input_normalize import NormalizedCodeInterpreterTool

    ci = _FakeInterpreter()
    inner = _InnerTool(ci)
    toks = jc.set_request_context(actor_id="a", session_id=SID)
    ptok = jc.set_producing_project_id(PID)
    yield NormalizedCodeInterpreterTool(inner), ci, inner, sts
    jc.reset_producing_project_id(ptok)
    jc.reset_request_context(toks)


def test_credentials_reach_the_sandbox_out_of_band_only(wrapped):
    tool, ci, inner, sts = wrapped
    events = _run(tool, _use())
    info = ci._sessions["session-default"]
    [(method, params)] = info.client.invokes
    assert method == "executeCode" and SECRET in params["code"]
    # The model-visible input and the result never carry a credential.
    assert SECRET not in json.dumps(inner.seen) and SECRET not in json.dumps(events, default=str)
    assert sts.calls[0]["Tags"][0] == {"Key": "project_id", "Value": PID}


def test_reinjects_only_when_the_fingerprint_changes(wrapped):
    tool, ci, _, _ = wrapped
    _run(tool, _use())
    _run(tool, _use("print(2)"))
    assert len(ci._sessions["session-default"].client.invokes) == 1
    # A different target sandbox gets its own injection.
    _run(tool, _use(session="other"))
    assert len(ci._sessions["other"].client.invokes) == 1
    # A re-created sandbox (new AWS session id) is injected again.
    ci._sessions["session-default"].session_id = "aws-sess-2"
    _run(tool, _use("print(3)"))
    assert len(ci._sessions["session-default"].client.invokes) == 2


def test_no_injection_for_init_and_list_sessions(wrapped):
    tool, ci, _, _ = wrapped
    use = {"toolUseId": "t", "name": "code_interpreter",
           "input": {"code_interpreter_input": {"action": {"type": "initSession", "session_name": "x",
                                                           "description": "d"}}}}
    _run(tool, use)
    assert ci._sessions == {}


def _refused(events):
    from strands.types._events import ToolResultEvent

    [ev] = [e for e in events if isinstance(e, ToolResultEvent)]
    assert ev.tool_result["status"] == "error"
    text = ev.tool_result["content"][0]["text"]
    assert "code_interpreter refused" in text and SECRET not in text
    return text


def test_a_changed_strands_shape_fails_closed(sts, caplog):
    from jobs import context as jc
    from tool_input_normalize import NormalizedCodeInterpreterTool

    class _Opaque(_InnerTool):
        def __init__(self):
            self.seen = []
            self._tool_func = lambda inp: {"status": "success"}

    toks = jc.set_request_context(actor_id="a", session_id=SID)
    try:
        inner = _Opaque()
        events = _run(NormalizedCodeInterpreterTool(inner), _use())
        assert not inner.seen, "the model's action must NOT run without scoped credentials"
        assert "internals changed" in _refused(events)
    finally:
        jc.reset_request_context(toks)


def test_sts_refusal_fails_closed_and_runs_nothing(wrapped):
    tool, ci, inner, sts = wrapped
    sts.fail = True
    text = _refused(_run(tool, _use()))
    assert not inner.seen and not ci.calls
    assert "STS" in text


def test_an_unconfirmed_install_fails_closed(wrapped):
    """The kernel call 'succeeded' but never printed the sentinel: the file may not exist, so refuse."""
    tool, ci, inner, _ = wrapped
    ci._ensure_session(None)
    ci._sessions["session-default"].client.confirm = False
    _refused(_run(tool, _use()))
    assert not inner.seen


def test_no_bound_session_fails_closed(sts):
    from jobs import context as jc
    from tool_input_normalize import NormalizedCodeInterpreterTool

    ci = _FakeInterpreter()
    inner = _InnerTool(ci)
    # Explicitly unbound (empty), so a session another test left in the context cannot mask this.
    toks = jc.set_request_context(actor_id="a", session_id="")
    try:
        assert "no chat session" in _refused(_run(NormalizedCodeInterpreterTool(inner), _use()))
    finally:
        jc.reset_request_context(toks)
    assert not inner.seen


def test_an_inactive_sandbox_during_injection_is_retried_on_a_fresh_one(wrapped):
    tool, ci, inner, _ = wrapped
    ci._ensure_session(None)
    stale = ci._sessions["session-default"]
    stale.client.raises = RuntimeError("Code interpreter session abc is not active")
    events = _run(tool, _use())
    assert ci._sessions["session-default"] is not stale, "evicted and recreated"
    assert inner.seen, "ran on the fresh, injected sandbox"
    assert all(getattr(e, "tool_result", {}).get("status") != "error" for e in events)


def test_crop_video_refuses_without_scoped_credentials(sts):
    from jobs import context as jc
    from tools import crop_video

    sts.fail = True
    toks = jc.set_request_context(actor_id="a", session_id=SID)
    try:
        with pytest.raises(sc.SandboxCredentialsError):
            crop_video._inject_scoped_credentials(_Client())
    finally:
        jc.reset_request_context(toks)


def test_media_actions_refuse_without_scoped_credentials(sts):
    import media_actions as ma

    with pytest.raises(sc.SandboxCredentialsError):
        ma._inject_credentials(_Client(confirm=False), PID, SID)
    ok = _Client()
    ma._inject_credentials(ok, PID, SID)
    assert ok.invokes and SECRET in ok.invokes[0][1]["code"]


def test_the_interpreter_still_has_the_internals_injection_relies_on():
    """Fails loudly if the vendored interpreter drops the private attributes the injection reaches into."""
    from agentcore_code_interpreter import AgentCoreCodeInterpreter

    ci = AgentCoreCodeInterpreter(region="us-west-2")
    assert callable(getattr(ci, "_ensure_session", None))
    assert isinstance(getattr(ci, "_sessions", None), dict)
    assert getattr(ci.code_interpreter._tool_func, "__self__", None) is ci


def test_without_a_role_the_wrapper_never_touches_the_sandbox(monkeypatch):
    from tool_input_normalize import NormalizedCodeInterpreterTool

    monkeypatch.delenv(sc.ROLE_ARN_ENV, raising=False)
    ci = _FakeInterpreter()
    _run(NormalizedCodeInterpreterTool(_InnerTool(ci)), _use())
    assert ci._sessions == {}


def test_crop_video_injects_into_its_own_sandbox(sts):
    from jobs import context as jc
    from tools import crop_video

    client = _Client()
    toks = jc.set_request_context(actor_id="a", session_id=SID)
    try:
        crop_video._inject_scoped_credentials(client)
    finally:
        jc.reset_request_context(toks)
    [(method, params)] = client.invokes
    assert method == "executeCode" and SECRET in params["code"]
    assert sts.calls[0]["Tags"][0] == {"Key": "project_id", "Value": "none"}
