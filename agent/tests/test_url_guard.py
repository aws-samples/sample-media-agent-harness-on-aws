"""The shared SSRF guard (``security.url_guard``) and every caller that must use it.

No network: DNS is a fake resolver (``url_guard.set_resolver``) and the socket layer is
either faked at ``HTTPAdapter.send`` (redirects, size caps) or at urllib3's
``create_connection`` (pinning — proves WHICH address the socket would open).
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path

import pytest
import requests
import urllib3.util.connection as u3conn
from requests.adapters import HTTPAdapter

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from security import url_guard  # noqa: E402

PUBLIC = "93.184.215.14"
PUBLIC6 = "2606:2800:21f:cb07:6820:80da:af6b:8b2c"


class FakeDNS:
    """``host -> [ips]``; a list of lists answers successive lookups in turn (rebinding)."""

    def __init__(self, table=None, default=(PUBLIC,)):
        self.table = dict(table or {})
        self.default = list(default)
        self.calls: list[str] = []

    def __call__(self, host, port):
        self.calls.append(host)
        answer = self.table.get(host, self.default)
        if answer and isinstance(answer[0], list):
            return answer.pop(0) if len(answer) > 1 else answer[0]
        return list(answer)


@pytest.fixture
def dns(monkeypatch):
    fake = FakeDNS()
    monkeypatch.setattr(url_guard, "_resolver", fake)
    return fake


class NoSocket(AssertionError):
    pass


@pytest.fixture
def no_socket(monkeypatch):
    """Any real connect attempt records the address it would open, then fails the test path."""
    opened: list[tuple] = []

    def _connect(address, *a, **k):
        opened.append(address)
        raise NoSocket(f"socket opened to {address}")

    monkeypatch.setattr(u3conn, "create_connection", _connect)
    return opened


def _resp(status=200, body=b"", headers=None, url="https://example.com/"):
    r = requests.Response()
    r.status_code = status
    r.headers.update(headers or {})
    r.raw = io.BytesIO(body)
    r.url = url
    r.reason = "OK" if status < 400 else "ERR"
    return r


@pytest.fixture
def transport(monkeypatch):
    """Fake what HTTPAdapter.send would get from the network, AFTER the guard has run.

    ``script`` maps url -> response factory; records every url the guard let through.
    """
    sent: list[str] = []
    script: dict = {}

    def _send(self, request, **kw):
        sent.append(request.url)
        factory = script.get(request.url)
        if factory is None:
            return _resp(404, b"", url=request.url)
        r = factory()
        r.request = request
        r.url = request.url
        return r

    monkeypatch.setattr(HTTPAdapter, "send", _send)
    return sent, script


# ---------------------------------------------------------------------------
# Address classification
# ---------------------------------------------------------------------------

BLOCKED_IPS = [
    "127.0.0.1", "127.255.255.254", "0.0.0.0", "0.1.2.3",
    "10.0.0.1", "172.16.0.1", "172.31.255.255", "192.168.1.1",
    "169.254.169.254", "169.254.170.2", "169.254.0.1",
    "100.64.0.1", "100.127.255.254",
    "192.0.0.1", "192.0.2.1", "198.18.0.1", "198.51.100.1", "203.0.113.1",
    "224.0.0.1", "239.255.255.250", "240.0.0.1", "255.255.255.255",
    "::", "::1", "fe80::1", "fc00::1", "fd00:ec2::254", "fd12:3456::1", "fec0::1",
    "ff02::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:169.254.169.254",
    "64:ff9b::7f00:1", "64:ff9b::a9fe:a9fe", "2002:7f00:1::", "2002:a9fe:a9fe::",
    "2001:db8::1",
]
PUBLIC_IPS = ["93.184.215.14", "8.8.8.8", "52.92.0.1", "13.32.0.1", "2600:1f14::1",
              PUBLIC6, "::ffff:8.8.8.8"]


@pytest.mark.parametrize("ip", BLOCKED_IPS)
def test_non_public_addresses_are_blocked(ip):
    assert url_guard.is_blocked_ip(ip) is True


@pytest.mark.parametrize("ip", PUBLIC_IPS)
def test_public_addresses_pass(ip):
    assert url_guard.is_blocked_ip(ip) is False


def test_garbage_address_is_blocked():
    assert url_guard.is_blocked_ip("not-an-ip") is True


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------

def test_public_https_host_passes_and_is_resolved(dns):
    t = url_guard.validate_url("https://cdn.example.com/a.json?x=1")
    assert (t.hostname, t.ip, t.port, t.host_header) == ("cdn.example.com", PUBLIC, 443, "cdn.example.com")
    assert dns.calls == ["cdn.example.com"]


def test_presigned_s3_and_cloudfront_urls_pass(dns):
    s3 = ("https://skills-bucket.s3.us-west-2.amazonaws.com/artifacts/s/output/x.json"
          "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=ASIA%2F20261021%2Fus-west-2%2Fs3"
          "&X-Amz-Signature=abc")
    dns.table["skills-bucket.s3.us-west-2.amazonaws.com"] = ["52.92.0.1"]
    dns.table["d111111abcdef8.cloudfront.net"] = ["13.32.0.1", "2600:9000::1"]
    assert url_guard.validate_url(s3).ip == "52.92.0.1"
    cf = "https://d111111abcdef8.cloudfront.net/artifacts/a.mp4?Expires=1&Signature=x&Key-Pair-Id=K"
    assert url_guard.validate_url(cf).ip == "13.32.0.1"


@pytest.mark.parametrize("url", [
    "http://cdn.example.com/a", "ftp://cdn.example.com/a", "file:///etc/passwd",
    "gopher://x.example.com/", "s3://bucket/key", "//cdn.example.com/a", "cdn.example.com/a",
])
def test_only_https_by_default(dns, url):
    with pytest.raises(url_guard.BlockedURLError):
        url_guard.validate_url(url)


def test_http_is_allowed_only_when_asked_and_still_address_checked(dns):
    assert url_guard.validate_url("http://cdn.example.com/a", allow_http=True).port == 80
    dns.table["evil.example.com"] = ["10.1.2.3"]
    with pytest.raises(url_guard.BlockedURLError, match="publicly reachable"):
        url_guard.validate_url("http://evil.example.com/", allow_http=True)


@pytest.mark.parametrize("host", [
    "127.0.0.1", "10.0.0.5", "169.254.169.254", "169.254.170.2", "100.64.1.1", "0.0.0.0",
    "[::1]", "[fd00:ec2::254]", "[::ffff:127.0.0.1]", "[::ffff:a9fe:a9fe]", "[fe80::1]",
])
def test_ip_literals_are_checked(dns, host):
    with pytest.raises(url_guard.BlockedURLError):
        url_guard.validate_url(f"https://{host}/latest/meta-data/")
    assert dns.calls == []  # a literal never reaches DNS


@pytest.mark.parametrize("host", [
    "2130706433", "0177.0.0.1", "0x7f000001", "0x7f.0.0.1", "127.1", "127.0.1", "017700000001",
    "0xa9fea9fe", "2852039166", "0251.0376.0251.0376", "10.1",
])
def test_non_canonical_numeric_literals_are_refused(dns, host):
    with pytest.raises(url_guard.BlockedURLError, match="non-canonical"):
        url_guard.validate_url(f"https://{host}/")
    assert dns.calls == []


@pytest.mark.parametrize("host", [
    "localhost", "LOCALHOST", "foo.localhost", "svc.internal", "ip-10-0-0-1.ec2.internal",
    "printer.local", "nas.home.arpa", "intranet",
])
def test_private_only_names_are_refused_before_dns(dns, host):
    with pytest.raises(url_guard.BlockedURLError):
        url_guard.validate_url(f"https://{host}/")
    assert dns.calls == []


@pytest.mark.parametrize("url", [
    "https://exa mple.com/", "https://example.com/\r\nHost: x", "https://ex\\ample.com/",
    "https://", "https:///path", "https://example.com:99999/", "", None, 42,
    "https://bad_host$.example.com/",
])
def test_malformed_urls_are_refused(dns, url):
    with pytest.raises(url_guard.BlockedURLError):
        url_guard.validate_url(url)


def test_userinfo_does_not_hide_the_real_host(dns):
    dns.table["internal.attacker.example"] = ["10.0.0.9"]
    with pytest.raises(url_guard.BlockedURLError):
        url_guard.validate_url("https://cdn.example.com@internal.attacker.example/")


def test_a_public_name_resolving_to_private_space_is_refused(dns):
    dns.table["rebind.attacker.example"] = ["169.254.169.254"]
    with pytest.raises(url_guard.BlockedURLError, match="publicly reachable"):
        url_guard.validate_url("https://rebind.attacker.example/")


def test_any_private_answer_among_several_refuses(dns):
    dns.table["mixed.example.com"] = [PUBLIC, "10.0.0.1"]
    with pytest.raises(url_guard.BlockedURLError):
        url_guard.validate_url("https://mixed.example.com/")


def test_unresolvable_host_is_refused(monkeypatch):
    def _fail(host, port):
        raise OSError("NXDOMAIN")
    monkeypatch.setattr(url_guard, "_resolver", _fail)
    with pytest.raises(url_guard.BlockedURLError, match="does not resolve"):
        url_guard.validate_url("https://nope.example.com/")
    monkeypatch.setattr(url_guard, "_resolver", lambda h, p: [])
    with pytest.raises(url_guard.BlockedURLError, match="does not resolve"):
        url_guard.validate_url("https://nope.example.com/")


def test_non_default_port_is_kept_in_host_header(dns):
    t = url_guard.validate_url("https://cdn.example.com:8443/x")
    assert (t.port, t.host_header) == (8443, "cdn.example.com:8443")


def test_set_resolver_round_trips():
    fake = FakeDNS()
    previous = url_guard.set_resolver(fake)
    try:
        url_guard.validate_url("https://cdn.example.com/")
        assert fake.calls == ["cdn.example.com"]
    finally:
        url_guard.set_resolver(previous)
    assert url_guard._resolver is previous


# ---------------------------------------------------------------------------
# Pinning (the socket opens to the VETTED address) and DNS rebinding
# ---------------------------------------------------------------------------

def test_connection_is_pinned_to_the_vetted_ip(dns, no_socket):
    with pytest.raises(NoSocket):
        url_guard.get("https://cdn.example.com/a.json")
    assert no_socket and no_socket[0][0] == PUBLIC and no_socket[0][1] == 443


def test_ipv6_connection_is_pinned_too(dns, no_socket):
    dns.table["v6.example.com"] = [PUBLIC6]
    with pytest.raises(NoSocket):
        url_guard.get("https://v6.example.com/a.json")
    assert no_socket[0][0].strip("[]") == PUBLIC6


def test_dns_rebinding_is_defeated(dns, no_socket):
    """First answer public, every later answer the metadata endpoint: the socket must
    still open to the address that was vetted, and DNS is not asked again at connect time."""
    dns.table["rebind.example.com"] = [[PUBLIC], ["169.254.169.254"]]
    with pytest.raises(NoSocket):
        url_guard.get("https://rebind.example.com/latest/meta-data/")
    assert [a[0] for a in no_socket] == [PUBLIC]
    assert "169.254.169.254" not in [a[0] for a in no_socket]


def test_tls_keeps_the_hostname_for_sni_and_cert_checks(dns):
    adapter = url_guard.GuardedAdapter()
    req = requests.Request("GET", "https://cdn.example.com/x").prepare()
    req._url_guard_target = url_guard.validate_url(req.url)
    host_params, pool_kwargs = adapter.build_connection_pool_key_attributes(req, True)
    assert host_params["host"] == PUBLIC
    assert pool_kwargs["server_hostname"] == "cdn.example.com"
    assert pool_kwargs["assert_hostname"] == "cdn.example.com"
    assert pool_kwargs["cert_reqs"] == "CERT_REQUIRED"


def test_an_unvetted_request_never_gets_a_connection():
    adapter = url_guard.GuardedAdapter()
    req = requests.Request("GET", "https://cdn.example.com/x").prepare()
    with pytest.raises(url_guard.BlockedURLError, match="not vetted"):
        adapter.build_connection_pool_key_attributes(req, True)


def test_host_header_is_rewritten_to_the_real_host(dns, transport):
    sent, script = transport
    script["https://cdn.example.com/x"] = lambda: _resp(200, b"ok")
    seen = {}
    real_send = HTTPAdapter.send

    def _spy(self, request, **kw):
        seen["host"] = request.headers.get("Host")
        return real_send(self, request, **kw)

    import unittest.mock as um
    with um.patch.object(HTTPAdapter, "send", _spy):
        url_guard.get("https://cdn.example.com/x", headers={"Host": "169.254.169.254"})
    assert seen["host"] == "cdn.example.com"


def test_session_ignores_environment_proxies(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://10.0.0.1:3128")
    s = url_guard.session()
    assert s.trust_env is False
    assert isinstance(s.get_adapter("https://x.example.com/"), url_guard.GuardedAdapter)
    assert isinstance(s.get_adapter("http://x.example.com/"), url_guard.GuardedAdapter)


# ---------------------------------------------------------------------------
# Redirects: every hop re-validated, hop cap
# ---------------------------------------------------------------------------

def test_redirect_to_a_public_host_is_followed(dns, transport):
    sent, script = transport
    script["https://a.example.com/x"] = lambda: _resp(302, headers={"Location": "https://b.example.com/y"})
    script["https://b.example.com/y"] = lambda: _resp(200, b"done")
    r = url_guard.get("https://a.example.com/x")
    assert r.status_code == 200 and sent == ["https://a.example.com/x", "https://b.example.com/y"]


@pytest.mark.parametrize("location", [
    "https://169.254.169.254/latest/meta-data/iam/security-credentials/",
    "http://169.254.170.2/v2/credentials",
    "https://[::1]/admin", "https://127.1/", "https://internal.example.com/",
    "http://b.example.com/plain-http", "file:///etc/passwd",
])
def test_redirect_to_a_blocked_target_is_refused(dns, transport, location):
    sent, script = transport
    dns.table["internal.example.com"] = ["10.0.0.7"]
    script["https://a.example.com/x"] = lambda: _resp(302, headers={"Location": location})
    with pytest.raises((url_guard.BlockedURLError, requests.exceptions.InvalidSchema)):
        url_guard.get("https://a.example.com/x")
    assert sent == ["https://a.example.com/x"]  # the blocked hop never reached the transport


def test_redirect_into_a_rebinding_name_is_refused(dns, transport):
    sent, script = transport
    dns.table["second.example.com"] = ["192.168.0.10"]
    script["https://a.example.com/x"] = lambda: _resp(301, headers={"Location": "https://second.example.com/"})
    with pytest.raises(url_guard.BlockedURLError):
        url_guard.get("https://a.example.com/x")


def test_redirect_hop_cap(dns, transport):
    sent, script = transport
    for i in range(20):
        script[f"https://a.example.com/{i}"] = (
            lambda i=i: _resp(302, headers={"Location": f"https://a.example.com/{i + 1}"}))
    with pytest.raises(requests.TooManyRedirects):
        url_guard.get("https://a.example.com/0")
    assert len(sent) == url_guard.MAX_REDIRECTS + 1
    sent.clear()
    with pytest.raises(requests.TooManyRedirects):
        url_guard.get("https://a.example.com/0", max_redirects=2)
    assert len(sent) == 3


def test_redirects_can_be_disabled(dns, transport):
    sent, script = transport
    script["https://a.example.com/x"] = lambda: _resp(302, headers={"Location": "https://b.example.com/"})
    r = url_guard.get("https://a.example.com/x", allow_redirects=False)
    assert r.status_code == 302 and sent == ["https://a.example.com/x"]


# ---------------------------------------------------------------------------
# fetch(): byte cap, truncation, status, deadline
# ---------------------------------------------------------------------------

def test_fetch_returns_body_and_headers(dns, transport):
    _, script = transport
    script["https://cdn.example.com/a.json"] = lambda: _resp(200, b'{"a":1}', {"Content-Type": "application/json"})
    r = url_guard.fetch("https://cdn.example.com/a.json", max_bytes=100)
    assert (r.status, r.body, r.truncated, r.ok) == (200, b'{"a":1}', False, True)
    assert json.loads(r.text()) == {"a": 1}
    assert r.headers["Content-Type"] == "application/json"


def test_fetch_size_cap_on_streamed_body(dns, transport):
    _, script = transport
    script["https://cdn.example.com/big"] = lambda: _resp(200, b"x" * 300_000)
    with pytest.raises(url_guard.ResponseTooLarge):
        url_guard.fetch("https://cdn.example.com/big", max_bytes=1000)


def test_fetch_size_cap_on_declared_length(dns, transport):
    _, script = transport
    script["https://cdn.example.com/big"] = lambda: _resp(200, b"x", {"Content-Length": "999999999"})
    with pytest.raises(url_guard.ResponseTooLarge):
        url_guard.fetch("https://cdn.example.com/big", max_bytes=1000)


def test_fetch_truncates_when_asked(dns, transport):
    _, script = transport
    script["https://cdn.example.com/big"] = lambda: _resp(200, b"y" * 300_000, {"Content-Length": "300000"})
    r = url_guard.fetch("https://cdn.example.com/big", max_bytes=1000, truncate=True)
    assert r.truncated and r.body == b"y" * 1000


def test_fetch_raises_for_status_unless_told_not_to(dns, transport):
    _, script = transport
    script["https://cdn.example.com/gone"] = lambda: _resp(404, b"nope")
    with pytest.raises(requests.HTTPError):
        url_guard.fetch("https://cdn.example.com/gone", max_bytes=10)
    r = url_guard.fetch("https://cdn.example.com/gone", max_bytes=10, raise_for_status=False)
    assert r.status == 404 and not r.ok


def test_fetch_deadline(dns, transport, monkeypatch):
    _, script = transport
    script["https://cdn.example.com/slow"] = lambda: _resp(200, b"z" * 200_000)
    clock = iter([0.0] + [1000.0] * 50)
    monkeypatch.setattr(url_guard.time, "monotonic", lambda: next(clock))
    with pytest.raises(TimeoutError):
        url_guard.fetch("https://cdn.example.com/slow", max_bytes=10**6, deadline_s=5)


def test_fetch_refuses_before_any_connection(dns, no_socket):
    with pytest.raises(url_guard.BlockedURLError):
        url_guard.fetch("https://169.254.169.254/latest/meta-data/", max_bytes=10)
    assert no_socket == []


# ---------------------------------------------------------------------------
# Every caller goes through the guard
# ---------------------------------------------------------------------------

METADATA = "https://169.254.169.254/latest/meta-data/iam/security-credentials/x.json"
REBIND = "https://rebind.attacker.example/x.json"


@pytest.fixture
def hostile(dns, no_socket, monkeypatch):
    """DNS points rebind.attacker.example at the metadata endpoint; no artifact env, so
    nothing is treated as one of OUR artifact urls; any socket open fails loudly."""
    dns.table["rebind.attacker.example"] = ["169.254.169.254"]
    for var in ("ARTIFACTS_DOMAIN", "SKILLS_S3_BUCKET", "ARTIFACTS_BUCKET"):
        monkeypatch.delenv(var, raising=False)
    return no_socket


@pytest.mark.parametrize("url", [METADATA, REBIND])
def test_read_result_files_refuses(hostile, url):
    from tools import result_files as rf
    out = rf._read_one(url, 1000)
    assert out["ok"] is False and "refused" in out["error"]
    assert hostile == []


@pytest.mark.parametrize("url", [METADATA, REBIND])
def test_view_images_refuses(hostile, url, monkeypatch):
    from tools import view_images as vi
    data, why = vi._fetch(url, [])
    assert data is None and "publicly reachable" in why
    assert hostile == []


@pytest.mark.parametrize("url", [METADATA.replace(".json", ".mp4"), REBIND.replace(".json", ".mp4")])
def test_media_probe_refuses(hostile, url):
    from tools.media_probe import media_probe
    out = json.loads(media_probe(url=url))
    assert out["error"]["code"] == "UNREACHABLE" and "refused" in out["error"]["message"]
    assert hostile == []


@pytest.mark.parametrize("url", [METADATA.replace(".json", ".vtt"), REBIND.replace(".json", ".vtt"),
                                 "http://cdn.example.com/c.vtt"])
def test_caption_parser_refuses(hostile, url):
    from tools.caption_parser import caption_parser
    out = json.loads(caption_parser(url=url))
    assert out["cues"] == [] and out["warnings"][0]["code"] == "FETCH_ERROR"
    assert hostile == []


@pytest.mark.parametrize("url", [METADATA.replace(".json", ".wav"), REBIND.replace(".json", ".wav")])
def test_peak_extractor_refuses(hostile, url):
    from tools import peak_extractor as pe
    with pytest.raises(ConnectionError, match="refused"):
        pe._download_audio(url, 5.0)
    assert hostile == []


@pytest.mark.parametrize("url", [METADATA, REBIND])
def test_render_agent_sidecar_and_container_reads_refuse(hostile, url):
    import render_agent as ra
    assert ra._fetch_json(url) is None
    assert ra._fetch_text(url.replace(".json", ".vtt")) is None
    assert ra._container_duration(url.replace(".json", ".mp4")) is None
    with pytest.raises(url_guard.BlockedURLError):
        ra._read_artifact_or_url(url, 2.0)
    assert hostile == []


@pytest.mark.parametrize("url", [METADATA, REBIND])
def test_artifact_import_refuses(hostile, url):
    import artifact_import
    with pytest.raises(url_guard.BlockedURLError):
        artifact_import.fetch_https_bytes(url)
    assert hostile == []


@pytest.mark.parametrize("url", [METADATA, REBIND])
def test_user_mcp_registration_and_probe_refuse(hostile, url):
    from gateway import user_mcp
    with pytest.raises(user_mcp.RegistrationError, match="publicly reachable"):
        user_mcp.validate_endpoint(url)
    out = user_mcp.probe_mcp_endpoint(url, token="t")
    assert out["ok"] is False and "publicly reachable" in out["detail"]
    assert hostile == []


def test_user_mcp_probe_uses_the_pinned_session_without_redirects(dns, transport):
    from gateway import user_mcp
    sent, script = transport
    script["https://mcp.example.com/mcp"] = lambda: _resp(
        307, headers={"Location": "https://169.254.169.254/"})
    out = user_mcp.probe_mcp_endpoint("https://mcp.example.com/mcp", token="t")
    assert out["ok"] is False
    assert sent == ["https://mcp.example.com/mcp"]  # the redirect was NOT followed


@pytest.mark.parametrize("url", [METADATA, REBIND, "http://169.254.170.2/v2/credentials",
                                 "http://2130706433/", "http://localhost:8080/"])
def test_http_request_tool_refuses_without_a_socket(hostile, url):
    import tool_input_normalize as tin
    from tools.http_request import http_request
    wrapped = tin.wrap_http_request(http_request)
    use = {"toolUseId": "t1", "name": "http_request", "input": {"method": "GET", "url": url}}

    async def _run():
        return [ev async for ev in wrapped.stream(use, {})]

    events = asyncio.run(_run())
    result = events[-1]["tool_result"] if isinstance(events[-1], dict) else events[-1].tool_result
    assert result["status"] == "error" and "refused" in result["content"][0]["text"]
    assert hostile == []


def test_http_request_tool_is_guarded_even_when_called_unwrapped(hostile):
    """The tool itself sends through url_guard.fetch, so an unwrapped call is refused too."""
    from tools import http_request as hr
    out = hr.send({"method": "GET", "url": REBIND})
    assert out["status"] == "error"
    assert "publicly reachable" in out["content"][0]["text"]  # refused by the adapter itself
    assert hostile == []


GUARDED_MODULES = [
    "tools/result_files.py", "tools/view_images.py", "tools/media_probe.py",
    "tools/caption_parser.py", "tools/peak_extractor.py", "artifact_import.py",
    "tools/http_request.py",
]


@pytest.mark.parametrize("rel", GUARDED_MODULES)
def test_no_raw_http_client_in_guarded_callers(rel):
    """Regression: a raw client here would bypass the guard (and its pinning)."""
    src = (SRC / rel).read_text()
    for raw in ("requests.get(", "requests.post(", "requests.head(", "urlopen(", "httpx.",
                "requests.Session("):
        assert raw not in src, f"{rel} uses {raw} — route it through security.url_guard"
    assert "url_guard" in src


def test_render_agent_has_no_raw_urlopen():
    src = (SRC / "render_agent.py").read_text()
    assert "urlopen(" not in src
    assert src.count("url_guard.fetch(") >= 2
