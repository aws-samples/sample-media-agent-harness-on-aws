"""The builtins that replaced strands-agents-tools: same model-facing tools, no runtime credentials.

* ``code_interpreter`` (``agentcore_code_interpreter``, vendored): its tool spec is pinned by digest to
  what strands-agents-tools 0.8.7 produced, so the model, prompt caching and ``sandbox_text`` see no
  change.
* ``current_time`` (``tools/current_time.py``): spec pinned the same way; same output.
* ``http_request`` (``tools/http_request.py``): every request goes through ``security.url_guard.fetch``;
  the credentialed modes (SigV4 with the runtime role, basic/digest, env-var tokens, cookie files) are
  refused, never sent. No network: DNS is faked and the socket layer is faked at ``HTTPAdapter.send``.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import sys
from pathlib import Path

import pytest
import requests
from requests.adapters import HTTPAdapter

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from security import url_guard  # noqa: E402
from tools import http_request as hr  # noqa: E402

# sha256 of json.dumps(tool_spec, sort_keys=True), computed against strands-agents-tools 0.8.7.
CODE_INTERPRETER_SPEC_SHA = "070e2e3ae5afdb6516f37afc0f40d542b873da00ed287bccc50fc578228e12ae"
CURRENT_TIME_SPEC = {
    "name": "current_time",
    "inputSchema": {"json": {"type": "object", "properties": {"timezone": {
        "type": "string", "default": None,
        "description": "The timezone to use (e.g., 'UTC', 'US/Pacific', 'Europe/London', 'Asia/Tokyo').\n"
                       "Defaults to environment variable DEFAULT_TIMEZONE ('UTC' if not set)."}}}},
}


def _sha(spec) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------------------
# code_interpreter / current_time: unchanged for the model
# ---------------------------------------------------------------------------

def test_code_interpreter_spec_is_byte_identical_to_the_upstream_tool():
    from agentcore_code_interpreter import AgentCoreCodeInterpreter

    spec = AgentCoreCodeInterpreter(region="us-west-2").code_interpreter.tool_spec
    assert spec["name"] == "code_interpreter"
    assert _sha(spec) == CODE_INTERPRETER_SPEC_SHA


def test_code_interpreter_keeps_the_session_bookkeeping_the_wrappers_use():
    import agentcore_code_interpreter as aci

    ci = aci.AgentCoreCodeInterpreter(region="us-west-2", identifier="my-ci")
    assert ci.identifier == "my-ci" and ci.default_session.startswith("session-")
    assert isinstance(aci._session_mapping, dict) and ci._sessions == {}
    assert ci.code_interpreter._tool_func.__self__ is ci

    class _Client:
        session_id = "aws-1"

        def __init__(self, **kw):
            self.calls = []

        def start(self, **kw):
            self.started = kw

        def invoke(self, op, params):
            self.calls.append((op, params))
            return {"stream": [{"result": {"content": [{"type": "text", "text": "2"}]}}]}

    clients = []

    def _new(**kw):
        clients.append(_Client(**kw))
        return clients[-1]

    orig = aci.BedrockAgentCoreCodeInterpreterClient
    aci.BedrockAgentCoreCodeInterpreterClient = _new
    try:
        out = ci.code_interpreter(code_interpreter_input={
            "action": {"type": "executeCode", "code": "print(1+1)", "session_name": "s1"}})
        name, err = ci._ensure_session("s1")
    finally:
        aci.BedrockAgentCoreCodeInterpreterClient = orig
        aci._session_mapping.pop("s1", None)
    assert out["status"] == "success" and "'text': '2'" in out["content"][0]["text"]
    assert (name, err) == ("s1", None) and len(clients) == 1        # reused, not started twice
    assert clients[0].started == {"identifier": "my-ci", "name": "s1", "session_timeout_seconds": 900}
    assert clients[0].calls == [("executeCode", {"code": "print(1+1)", "language": "python",
                                                 "clearContext": False})]
    assert ci._sessions["s1"].session_id == "aws-1"


def test_current_time_spec_and_output_are_unchanged(monkeypatch):
    from tools.current_time import current_time

    spec = current_time.tool_spec
    assert {k: spec[k] for k in ("name", "inputSchema")} == CURRENT_TIME_SPEC
    assert spec["description"].startswith("Get the current time in ISO 8601 format.")
    monkeypatch.delenv("DEFAULT_TIMEZONE", raising=False)
    assert current_time().endswith("+00:00")
    assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", current_time(timezone="Asia/Tokyo"))
    assert current_time(timezone="Asia/Tokyo").endswith("+09:00")
    with pytest.raises(ValueError, match="Error getting current time"):
        current_time(timezone="Not/AZone")


# ---------------------------------------------------------------------------
# http_request
# ---------------------------------------------------------------------------

@pytest.fixture
def net(monkeypatch):
    """Public DNS for every host and a scripted transport; records each request the guard let out."""
    monkeypatch.setattr(url_guard, "_resolver", lambda host, port: ["93.184.215.14"])
    sent: list = []
    script: dict = {}

    def _send(self, request, **kw):
        sent.append(request)
        status, body, headers = script.get(request.url, (404, b"", {}))
        r = requests.Response()
        r.status_code, r.raw, r.url, r.request = status, io.BytesIO(body), request.url, request
        r.headers.update(headers)
        r.reason = "OK"
        return r

    monkeypatch.setattr(HTTPAdapter, "send", _send)
    return sent, script


def _text(result) -> str:
    return "\n".join(c["text"] for c in result["content"])


def test_get_reports_status_headers_and_body_like_the_old_tool(net):
    sent, script = net
    script["https://api.example.com/v1"] = (200, b'{"ok": true}', {
        "Content-Type": "application/json", "X-Other": "x"})
    out = hr.send({"method": "GET", "url": "https://api.example.com/v1"})
    assert out["status"] == "success"
    assert [c["text"].split(":")[0] for c in out["content"]] == ["Status Code", "Headers", "Body"]
    assert out["content"][0]["text"] == "Status Code: 200"
    assert out["content"][1]["text"] == "Headers: {'Content-Type': 'application/json'}"
    assert out["content"][2]["text"] == 'Body: {"ok": true}'
    assert sent[0].method == "GET"


@pytest.mark.parametrize("method", ["HEAD", "POST", "PUT", "PATCH", "DELETE"])
def test_every_method_and_body_goes_through(net, method):
    sent, script = net
    script["https://api.example.com/x"] = (201, b"", {})
    out = hr.send({"method": method.lower(), "url": "https://api.example.com/x", "body": '{"a": "é"}',
                   "headers": {"Content-Type": "application/json"}})
    assert out["status"] == "success" and "Status Code: 201" in _text(out)
    assert sent[0].method == method and sent[0].body == '{"a": "é"}'.encode()
    assert sent[0].headers["Content-Type"] == "application/json"


def test_unknown_method_is_refused(net):
    sent, _ = net
    out = hr.send({"method": "TRACE", "url": "https://api.example.com/x"})
    assert out["status"] == "error" and "not supported" in _text(out) and sent == []


@pytest.mark.parametrize("args, needle", [
    ({"auth_type": "aws_sig_v4", "aws_auth": {"service": "s3"}}, "aws_sig_v4"),
    ({"auth_type": "AWS_SIG_V4"}, "never signs"),
    ({"aws_auth": {"service": "s3"}}, "aws_auth"),
    ({"auth_type": "basic", "basic_auth": {"username": "u", "password": "p"}}, "basic"),
    ({"basic_auth": {"username": "u", "password": "p"}}, "basic_auth"),
    ({"auth_type": "digest", "digest_auth": {"username": "u", "password": "p"}}, "digest"),
    ({"auth_type": "Bearer", "auth_env_var": "RUNTIME_CONFIG_SECRET_ARN"}, "environment"),
    ({"cookie": "~/.aws/credentials"}, "cookie"),
    ({"cookie_jar": "/tmp/jar"}, "cookie_jar"),
    ({"verify_ssl": False}, "SSL"),
])
def test_credentialed_and_host_file_modes_are_refused_before_any_request(net, args, needle):
    sent, _ = net
    out = hr.send({"method": "GET", "url": "https://bucket.s3.amazonaws.com/k", **args})
    assert out["status"] == "error" and needle in _text(out)
    assert sent == []                                   # nothing left the process, nothing was signed


def test_the_runtime_role_is_never_consulted(net, monkeypatch):
    import boto3

    def _boom(*a, **k):
        raise AssertionError("http_request touched the runtime's AWS credentials")

    monkeypatch.setattr(boto3, "Session", _boom)
    sent, script = net
    script["https://bucket.s3.amazonaws.com/k"] = (403, b"<Error/>", {})
    out = hr.send({"method": "GET", "url": "https://bucket.s3.amazonaws.com/k"})
    assert "Status Code: 403" in _text(out)
    assert "Authorization" not in sent[0].headers and not any(h.lower().startswith("x-amz") for h in sent[0].headers)


@pytest.mark.parametrize("auth_type, token, header, value", [
    ("Bearer", "t0k", "Authorization", "Bearer t0k"),
    ("token", "t0k", "Authorization", "token t0k"),
    ("custom", "Key abc", "Authorization", "Key abc"),
    ("api_key", "k1", "X-API-Key", "k1"),
])
def test_the_models_own_token_is_still_sent(net, auth_type, token, header, value):
    sent, _ = net
    hr.send({"method": "GET", "url": "https://api.example.com/", "auth_type": auth_type, "auth_token": token})
    assert sent[0].headers[header] == value


def test_every_redirect_hop_is_vetted_and_reported(net, monkeypatch):
    sent, script = net
    script["https://a.example.com/"] = (302, b"", {"Location": "https://b.example.com/"})
    script["https://b.example.com/"] = (200, b"done", {})
    out = hr.send({"method": "GET", "url": "https://a.example.com/"})
    assert "Redirects: 1 redirects followed (302 -> 200)" in _text(out)
    assert "Body: done" in _text(out)

    # a hop to a private address is refused by the adapter itself, mid-chain
    monkeypatch.setattr(url_guard, "_resolver",
                        lambda host, port: ["10.0.0.5"] if host == "evil.example.com" else ["93.184.215.14"])
    script["https://a.example.com/"] = (302, b"", {"Location": "https://evil.example.com/"})
    out = hr.send({"method": "GET", "url": "https://a.example.com/"})
    assert out["status"] == "error" and "publicly reachable" in _text(out)
    assert sent[-1].url == "https://a.example.com/"


def test_redirects_can_be_turned_off_and_are_capped(net):
    sent, script = net
    script["https://a.example.com/"] = (301, b"", {"Location": "https://b.example.com/"})
    out = hr.send({"method": "GET", "url": "https://a.example.com/", "allow_redirects": False})
    assert "Status Code: 301" in _text(out) and len(sent) == 1
    for i in range(10):
        script[f"https://h{i}.example.com/"] = (302, b"", {"Location": f"https://h{i + 1}.example.com/"})
    out = hr.send({"method": "GET", "url": "https://h0.example.com/", "max_redirects": 30})
    assert out["status"] == "error" and "redirect" in _text(out).lower()


def test_a_large_body_is_truncated_and_says_so(net, monkeypatch):
    monkeypatch.setattr(hr, "MAX_RESPONSE_BYTES", 1000)
    _, script = net
    script["https://a.example.com/big"] = (200, b"x" * 5000, {"Content-Type": "text/plain"})
    out = hr.send({"method": "GET", "url": "https://a.example.com/big"})
    body = next(c["text"] for c in out["content"] if c["text"].startswith("Body: "))
    assert len(body) == len("Body: ") + 1000
    assert "cut at 1000 bytes" in _text(out)


def test_timeout_is_bounded(net, monkeypatch):
    seen = {}
    real = url_guard.fetch

    def _fetch(url, **kw):
        seen.update(kw)
        return real(url, **kw)

    monkeypatch.setattr(url_guard, "fetch", _fetch)
    hr.send({"method": "GET", "url": "https://a.example.com/", "timeout": 10_000})
    assert seen["timeout"] == hr.MAX_TIMEOUT_S and seen["deadline_s"] == hr.MAX_DEADLINE_S
    hr.send({"method": "GET", "url": "https://a.example.com/"})
    assert seen["timeout"] == 30.0 and seen["deadline_s"] == 90.0


def test_html_converts_to_markdown_and_metrics_are_reported(net):
    _, script = net
    script["https://a.example.com/p"] = (200, b"<html><body><h1>Title</h1><p>Hi</p></body></html>",
                                         {"Content-Type": "text/html; charset=utf-8"})
    out = hr.send({"method": "GET", "url": "https://a.example.com/p", "convert_to_markdown": True,
                   "metrics": True})
    assert "# Title" in _text(out) and "<h1>" not in _text(out)
    assert any(c["text"].startswith("Metrics: {'duration'") for c in out["content"])


def test_the_decorated_tool_streams_a_result_and_ignores_unknown_noise(net):
    _, script = net
    script["https://a.example.com/"] = (200, b"ok", {})
    use = {"toolUseId": "t9", "name": "http_request",
           "input": {"method": "GET", "url": "https://a.example.com/", "session_config": {"pool_size": 3}}}

    async def _run():
        return [ev async for ev in hr.http_request.stream(use, {})]

    result = asyncio.run(_run())[-1].tool_result
    assert result["toolUseId"] == "t9" and result["status"] == "success"
    assert "Body: ok" in _text(result)


def test_the_model_facing_schema_has_no_credentialed_option():
    spec = hr.http_request.tool_spec
    assert spec["name"] == "http_request"
    props = spec["inputSchema"]["json"]["properties"]
    assert spec["inputSchema"]["json"]["required"] == ["method", "url"]
    assert not {"aws_auth", "basic_auth", "digest_auth", "auth_env_var", "cookie", "cookie_jar",
                "verify_ssl"} & set(props)
    assert not {"aws_sig_v4", "basic", "digest"} & set(props["auth_type"]["enum"])
    assert "SigV4" not in spec["description"]
