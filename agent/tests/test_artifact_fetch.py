"""Agent self-fetches of OUR artifact URLs go through S3 by key; external tools get presigned GETs.

An artifact URL (``https://<ARTIFACTS_DOMAIN>/artifacts/<key>``, ``/api/artifact?key=artifacts/<key>``,
or a bare ``artifacts/<key>``) is a stable identifier. When the artifacts CDN is private an unsigned
HTTP GET of it 403s, so every agent-side read resolves it to the S3 object instead, and — only when
``ARTIFACTS_CDN_PRIVATE`` is on — tool inputs bound for external tools (fal.ai, user MCP servers)
are rewritten to short-lived presigned S3 GETs. No network, no real AWS: a fake S3 client.
"""
from __future__ import annotations

import asyncio
import io
import json
import struct
import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from media import artifact_fetch as af  # noqa: E402

DOMAIN = "d123.cloudfront.net"
BUCKET = "skills-bucket"


class FakeS3:
    """Records calls; serves objects from an in-memory dict."""

    def __init__(self, objects=None, presign_error=None):
        self.objects = dict(objects or {})
        self.calls: list = []
        self.presign_error = presign_error

    def _missing(self, op):
        from botocore.exceptions import ClientError
        return ClientError({"Error": {"Code": "404", "Message": "Not Found"},
                            "ResponseMetadata": {"HTTPStatusCode": 404}}, op)

    def head_object(self, Bucket, Key):
        self.calls.append(("head_object", Bucket, Key))
        if Key not in self.objects:
            raise self._missing("HeadObject")
        body, ct = self.objects[Key]
        return {"ContentLength": len(body), "ContentType": ct}

    def get_object(self, Bucket, Key, Range=None):
        self.calls.append(("get_object", Bucket, Key, Range))
        if Key not in self.objects:
            raise self._missing("GetObject")
        body, ct = self.objects[Key]
        if Range:
            spec = Range.split("=", 1)[1]
            a, b = spec.split("-", 1)
            if a == "":
                body = body[-int(b):]
            else:
                body = body[int(a): int(b) + 1 if b else None]
        return {"Body": io.BytesIO(body), "ContentType": ct}

    def generate_presigned_url(self, op, Params, ExpiresIn):
        self.calls.append(("presign", Params["Bucket"], Params["Key"], ExpiresIn))
        if self.presign_error:
            raise self.presign_error
        return f"https://{Params['Bucket']}.s3.us-west-2.amazonaws.com/{Params['Key']}?X-Amz-Signature=sig&X-Amz-Expires={ExpiresIn}"

    def copy_object(self, **k):
        self.calls.append(("copy_object", k))


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setenv("ARTIFACTS_DOMAIN", DOMAIN)
    monkeypatch.setenv("SKILLS_S3_BUCKET", BUCKET)
    monkeypatch.delenv("ARTIFACTS_CDN_PRIVATE", raising=False)
    monkeypatch.setattr(af, "_client", lambda: fake)
    return fake


def _no_http(*a, **k):
    raise AssertionError("an own artifact URL must not be fetched over HTTP")


class _GuardResp:
    """A streamed response as ``security.url_guard.get`` returns it (foreign urls only)."""

    def __init__(self, data: bytes, url: str):
        self._d, self.url, self.status_code, self.headers = data, url, 200, {}

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=65536):
        yield self._d

    def close(self):
        return None


def _patch_http(monkeypatch, body_for):
    """Foreign urls go through the shared SSRF guard; fake its transport (``url_guard.get``).
    ``body_for(url, headers)`` returns the body, or raises."""
    from security import url_guard
    monkeypatch.setattr(url_guard, "get",
                        lambda url, headers=None, **kw: _GuardResp(body_for(url, headers or {}), url))


# ---------------------------------------------------------------------------
# own_artifact_key
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,key", [
    (f"https://{DOMAIN}/artifacts/s1/output/a.mp4", "artifacts/s1/output/a.mp4"),
    (f"https://{DOMAIN.upper()}/artifacts/s1/a.png?v=2", "artifacts/s1/a.png"),
    (f"https://{DOMAIN}/artifacts/s1/my%20clip.mp4", "artifacts/s1/my clip.mp4"),
    (f"https://{DOMAIN}/artifacts/artifacts/s1/a.mp4", "artifacts/s1/a.mp4"),
    ("/api/artifact?key=artifacts/s1/doc.pdf", "artifacts/s1/doc.pdf"),
    ("/api/artifact?key=artifacts%2Fs1%2Fdoc.pdf&download=1", "artifacts/s1/doc.pdf"),
    ("https://app.example.com/api/artifact?key=artifacts/s1/m.json", "artifacts/s1/m.json"),
    ("artifacts/s1/output/a.wav", "artifacts/s1/output/a.wav"),
    ("artifacts/artifacts/x.wav", "artifacts/x.wav"),
    ("artifacts/external/abc.png", "artifacts/external/abc.png"),
])
def test_own_artifact_key_accepts_our_forms(s3, url, key):
    assert af.own_artifact_key(url) == key


@pytest.mark.parametrize("url", [
    "https://v3b.fal.media/files/b/abc/out.mp4",
    "https://other-bucket.s3.us-west-2.amazonaws.com/artifacts/s1/a.mp4?X-Amz-Signature=x",
    "https://s3.us-west-2.amazonaws.com/other-bucket/artifacts/s1/a.mp4?X-Amz-Signature=x",
    f"https://{BUCKET}.s3.us-west-2.amazonaws.com/projects/p1/a.mp4?X-Amz-Signature=x",
    "https://other.cloudfront.net/artifacts/s1/a.mp4",
    f"https://{DOMAIN}/uploads/s1/a.mp4",            # not under artifacts/
    f"https://{DOMAIN}/artifacts/../secrets.txt",
    "artifacts/s1/../../x",
    "artifacts/s1\\x",
    "/api/artifact?key=uploads/x.png",
    "/api/artifact?key=artifacts/../x",
    "/api/artifact",
    "s3://skills-bucket/artifacts/s1/a.mp4",
    "ftp://d123.cloudfront.net/artifacts/a",
    "//d123.cloudfront.net/artifacts/a",
    "artifacts/",
    "",
    None,
    42,
])
def test_own_artifact_key_rejects_everything_else(s3, url):
    assert af.own_artifact_key(url) is None


@pytest.mark.parametrize("url,key", [
    # A presigned GET of OUR bucket (what "Copy source URL" copies) is an identifier, not an
    # expiring external URL: read by key, re-presigned fresh for external tools.
    (f"https://{BUCKET}.s3.us-west-2.amazonaws.com/artifacts/s1/a.mp4?X-Amz-Signature=x&X-Amz-Expires=3600", "artifacts/s1/a.mp4"),
    (f"https://{BUCKET}.s3.amazonaws.com/artifacts/s1/my%20clip.mp4?X-Amz-Signature=x", "artifacts/s1/my clip.mp4"),
    (f"https://s3.us-west-2.amazonaws.com/{BUCKET}/artifacts/s1/a.wav?X-Amz-Signature=x", "artifacts/s1/a.wav"),
])
def test_own_artifact_key_accepts_our_presigned_urls(s3, url, key):
    assert af.own_artifact_key(url) == key


def test_our_presigned_url_in_tool_input_is_re_presigned(s3, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CDN_PRIVATE", "true")
    old = f"https://{BUCKET}.s3.us-west-2.amazonaws.com/artifacts/s1/a.mp4?X-Amz-Signature=old&X-Amz-Expires=60"
    out, changed = af.presign_artifact_urls_in({"image_url": old}, 21600)
    assert changed and "X-Amz-Expires=21600" in out["image_url"] and "Signature=old" not in out["image_url"]


def test_cdn_form_needs_a_configured_domain(monkeypatch):
    monkeypatch.delenv("ARTIFACTS_DOMAIN", raising=False)
    assert af.own_artifact_key(f"https://{DOMAIN}/artifacts/a.mp4") is None
    # ...but an explicit domain (a module's own ARTIFACTS_DOMAIN) is honoured
    assert af.own_artifact_key(f"https://{DOMAIN}/artifacts/a.mp4", DOMAIN) == "artifacts/a.mp4"
    # the proxy and bare forms do not depend on the domain
    assert af.own_artifact_key("artifacts/a.mp4") == "artifacts/a.mp4"


def test_bucket_resolution_order(monkeypatch):
    monkeypatch.setenv("SKILLS_S3_BUCKET", "primary")
    monkeypatch.setenv("S3_BUCKET", "fallback")
    assert af.artifacts_bucket() == "primary"
    monkeypatch.delenv("SKILLS_S3_BUCKET")
    assert af.artifacts_bucket() == "fallback"


def test_no_bucket_means_not_resolvable(s3, monkeypatch):
    monkeypatch.delenv("SKILLS_S3_BUCKET")
    monkeypatch.delenv("S3_BUCKET", raising=False)
    url = f"https://{DOMAIN}/artifacts/a.mp4"
    assert af.is_own_artifact(url) is False
    assert af.head_artifact(url) is None
    assert af.presign_artifact_url(url) is None
    assert s3.calls == []


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------

def test_head_read_presign_use_the_key(s3):
    s3.objects["artifacts/s1/a.json"] = (b'{"ok": 1}', "application/json")
    url = f"https://{DOMAIN}/artifacts/s1/a.json"
    assert af.head_artifact(url)["ContentLength"] == 9
    assert af.read_artifact_bytes(url) == b'{"ok": 1}'
    assert af.read_artifact_bytes(url, "bytes=0-1") == b'{"'
    signed = af.presign_artifact_url(url, 600)
    assert "X-Amz-Signature" in signed and "artifacts/s1/a.json" in signed
    assert ("presign", BUCKET, "artifacts/s1/a.json", 600) in s3.calls
    assert ("get_object", BUCKET, "artifacts/s1/a.json", "bytes=0-1") in s3.calls


def test_foreign_url_is_not_read_from_s3(s3):
    url = "https://v3b.fal.media/files/x.png"
    assert af.head_artifact(url) is None
    assert af.presign_artifact_url(url) is None
    with pytest.raises(ValueError):
        af.read_artifact_bytes(url)
    assert s3.calls == []


def test_presign_failure_returns_none(s3):
    s3.presign_error = RuntimeError("no credentials")
    assert af.presign_artifact_url(f"https://{DOMAIN}/artifacts/a.png") is None


# ---------------------------------------------------------------------------
# render_agent: _fetch_json / _fetch_json_many / _fetch_text / _container_duration
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, data: bytes):
        self._d = data

    def read(self, n=-1):
        return self._d if n is None or n < 0 else self._d[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_render_fetch_json_own_uses_s3(s3, monkeypatch):
    import render_agent as ra
    s3.objects["artifacts/s1/shots.json"] = (b'{"shots": [1, 2]}', "application/json")
    _patch_http(monkeypatch, _no_http)
    assert ra._fetch_json(f"https://{DOMAIN}/artifacts/s1/shots.json") == {"shots": [1, 2]}
    assert ra._fetch_json("/api/artifact?key=artifacts/s1/shots.json") == {"shots": [1, 2]}


def test_render_fetch_json_foreign_uses_http(s3, monkeypatch):
    import render_agent as ra
    seen = []
    _patch_http(monkeypatch, lambda url, h: (seen.append(url), b'{"a": 1}')[1])
    url = "https://bucket.s3.amazonaws.com/x.json?X-Amz-Signature=s"
    assert ra._fetch_json(url) == {"a": 1}
    assert seen == [url]
    assert s3.calls == []


def test_render_fetch_json_own_missing_is_none(s3, monkeypatch):
    import render_agent as ra
    _patch_http(monkeypatch, _no_http)
    assert ra._fetch_json(f"https://{DOMAIN}/artifacts/s1/missing.json") is None


def test_render_fetch_json_many_mixes_s3_and_http(s3, monkeypatch):
    import render_agent as ra
    s3.objects["artifacts/s1/a.json"] = (b'{"src": "s3"}', "application/json")
    _patch_http(monkeypatch, lambda url, h: b'{"src": "http"}')
    own = f"https://{DOMAIN}/artifacts/s1/a.json"
    foreign = "https://example.com/b.json"
    out = ra._fetch_json_many([own, foreign])
    assert out == {own: {"src": "s3"}, foreign: {"src": "http"}}


def test_render_fetch_text_own_and_foreign(s3, monkeypatch):
    import render_agent as ra
    s3.objects["artifacts/s1/c.vtt"] = (b"WEBVTT\n\n", "text/vtt")
    _patch_http(monkeypatch, lambda url, h: b"HTTP-VTT")
    assert ra._fetch_text(f"https://{DOMAIN}/artifacts/s1/c.vtt") == "WEBVTT\n\n"
    assert ra._fetch_text("https://example.com/c.vtt") == "HTTP-VTT"
    assert [c[0] for c in s3.calls] == ["get_object"]


def _mp4_with_duration(seconds: int, timescale: int = 1000) -> bytes:
    """Minimal faststart MP4: ftyp + moov{mvhd v0}."""
    ftyp = struct.pack(">I4s4sI4s", 20, b"ftyp", b"isom", 0, b"isom")
    mvhd_body = struct.pack(">B3sIIII", 0, b"\0\0\0", 0, 0, timescale, seconds * timescale) + b"\0" * 80
    mvhd = struct.pack(">I4s", 8 + len(mvhd_body), b"mvhd") + mvhd_body
    moov = struct.pack(">I4s", 8 + len(mvhd), b"moov") + mvhd
    return ftyp + moov + b"\0" * 64


def test_render_container_duration_own_uses_ranged_s3(s3, monkeypatch):
    import render_agent as ra
    s3.objects["artifacts/s1/clip.mp4"] = (_mp4_with_duration(12), "video/mp4")
    _patch_http(monkeypatch, _no_http)
    assert ra._container_duration(f"https://{DOMAIN}/artifacts/s1/clip.mp4") == pytest.approx(12.0)
    gets = [c for c in s3.calls if c[0] == "get_object"]
    assert gets and all(c[3] and c[3].startswith("bytes=") for c in gets)


def test_render_container_duration_foreign_uses_http_range(s3, monkeypatch):
    import render_agent as ra
    data = _mp4_with_duration(7)
    ranges = []

    def _open(url, headers):
        rng = headers.get("Range")
        ranges.append(rng)
        a, b = rng.split("=", 1)[1].split("-", 1)
        return data[-int(b):] if a == "" else data[int(a): int(b) + 1]

    _patch_http(monkeypatch, _open)
    assert ra._container_duration("https://v3b.fal.media/files/clip.mp4") == pytest.approx(7.0)
    assert ranges and s3.calls == []


# ---------------------------------------------------------------------------
# peak_extractor download
# ---------------------------------------------------------------------------

def test_peak_extractor_download_own_uses_s3(s3, monkeypatch):
    from tools import peak_extractor as pe
    s3.objects["artifacts/s1/a.wav"] = (b"RIFFwav", "audio/wav")
    _patch_http(monkeypatch, _no_http)
    data, _ = pe._download_audio(f"https://{DOMAIN}/artifacts/s1/a.wav", 5.0)
    assert data == b"RIFFwav"


def test_peak_extractor_download_own_missing_is_connection_error(s3, monkeypatch):
    from tools import peak_extractor as pe
    _patch_http(monkeypatch, _no_http)
    with pytest.raises(ConnectionError):
        pe._download_audio(f"https://{DOMAIN}/artifacts/s1/none.wav", 5.0)


# ---------------------------------------------------------------------------
# artifact_import + the `import` operation in main.py and task_agent.py
# ---------------------------------------------------------------------------

def test_fetch_https_bytes_own_reads_s3(s3, monkeypatch):
    import artifact_import
    s3.objects["artifacts/s1/x.png"] = (b"\x89PNG", "image/png")
    _patch_http(monkeypatch, _no_http)
    assert artifact_import.fetch_https_bytes(f"https://{DOMAIN}/artifacts/s1/x.png") == (b"\x89PNG", "image/png")


def test_fetch_https_bytes_own_respects_cap(s3, monkeypatch):
    import artifact_import
    s3.objects["artifacts/s1/big.bin"] = (b"x" * 50, "application/octet-stream")
    with pytest.raises(ValueError, match="import cap"):
        artifact_import.fetch_https_bytes(f"https://{DOMAIN}/artifacts/s1/big.bin", max_bytes=10)


def test_own_artifact_s3_uri():
    import artifact_import
    assert artifact_import.own_artifact_s3_uri(f"https://{DOMAIN}/artifacts/s/a.png", "b", DOMAIN) \
        == "s3://b/artifacts/s/a.png"
    assert artifact_import.own_artifact_s3_uri("https://v3b.fal.media/a.png", "b", DOMAIN) is None
    assert artifact_import.own_artifact_s3_uri(f"https://{DOMAIN}/artifacts/s/a.png", "", DOMAIN) is None


def _fn(mod):
    a = mod.artifact
    return getattr(a, "_tool_func", a)


class _CopyS3:
    def __init__(self):
        self.copied = {}

    def copy_object(self, **k):
        self.copied.update(k)

    def put_object(self, **k):
        raise AssertionError("an own artifact import must be a server-side copy, not a put")


def _boom_fetch(*a, **k):
    raise AssertionError("an own artifact import must not fetch over HTTP")


@pytest.mark.parametrize("source", [
    f"https://{DOMAIN}/artifacts/sessA/output/shot06.png",
    "https://app.example.com/api/artifact?key=artifacts/sessA/output/shot06.png",
])
def test_main_import_own_https_is_server_side_copy(monkeypatch, source):
    import main
    import artifact_import
    fake = _CopyS3()
    monkeypatch.setattr(main, "s3_client", fake)
    monkeypatch.setattr(main, "S3_BUCKET", "skills-store")
    monkeypatch.setattr(main, "ARTIFACTS_DOMAIN", DOMAIN, raising=False)
    monkeypatch.setattr(main, "_artifact_output_prefix", lambda: "artifacts/sessB/output/")
    monkeypatch.setattr(artifact_import, "fetch_https_bytes", _boom_fetch)
    out = json.loads(_fn(main)(operation="import", source_uri=source))
    assert out["status"] == "imported"
    assert fake.copied["CopySource"] == {"Bucket": "skills-store", "Key": "artifacts/sessA/output/shot06.png"}
    assert fake.copied["Key"] == "artifacts/sessB/output/shot06.png"
    assert out["url"] == f"https://{DOMAIN}/artifacts/sessB/output/shot06.png"


def test_main_import_foreign_https_still_fetches(monkeypatch):
    import main
    import artifact_import
    puts = {}

    class _S3:
        def put_object(self, **k):
            puts.update(k)

        def copy_object(self, **k):
            raise AssertionError("a foreign https import must not copy_object")

    monkeypatch.setattr(main, "s3_client", _S3())
    monkeypatch.setattr(main, "ARTIFACTS_DOMAIN", DOMAIN, raising=False)
    monkeypatch.setattr(artifact_import, "fetch_https_bytes", lambda url, *a, **k: (b"img", "image/png"))
    out = json.loads(_fn(main)(operation="import", key="o.png",
                               source_uri="https://v3b.fal.media/files/b/o.png"))
    assert out["status"] == "imported" and puts["Body"] == b"img"


def test_task_agent_import_own_https_is_server_side_copy(monkeypatch):
    import task_agent
    import artifact_import
    fake = _CopyS3()
    monkeypatch.setattr(task_agent, "_s3", fake)
    monkeypatch.setattr(task_agent, "S3_BUCKET", "skills-store")
    monkeypatch.setattr(task_agent, "ARTIFACTS_DOMAIN", DOMAIN, raising=False)
    monkeypatch.setattr(task_agent, "_artifact_output_prefix", lambda: "artifacts/sessB/output/")
    monkeypatch.setattr(artifact_import, "fetch_https_bytes", _boom_fetch)
    out = json.loads(_fn(task_agent)(operation="import", key="seg01.mp4",
                                     source_uri=f"https://{DOMAIN}/artifacts/sessA/output/out.mp4"))
    assert out["status"] == "imported"
    assert fake.copied["CopySource"] == {"Bucket": "skills-store", "Key": "artifacts/sessA/output/out.mp4"}
    assert fake.copied["Key"] == "artifacts/sessB/output/seg01.mp4"


def test_task_agent_import_foreign_https_still_fetches(monkeypatch):
    import task_agent
    import artifact_import
    puts = {}

    class _S3:
        def put_object(self, **k):
            puts.update(k)

    monkeypatch.setattr(task_agent, "_s3", _S3())
    monkeypatch.setattr(task_agent, "S3_BUCKET", "b")
    monkeypatch.setattr(task_agent, "ARTIFACTS_DOMAIN", DOMAIN, raising=False)
    monkeypatch.setattr(artifact_import, "fetch_https_bytes", lambda url, *a, **k: (b"v", "video/mp4"))
    out = json.loads(_fn(task_agent)(operation="import", key="s.mp4",
                                     source_uri="https://v3b.fal.media/files/b/s.mp4"))
    assert out["status"] == "imported" and puts["Body"] == b"v"


# ---------------------------------------------------------------------------
# Tool-input presign rewrite (ARTIFACTS_CDN_PRIVATE)
# ---------------------------------------------------------------------------

OWN_A = f"https://{DOMAIN}/artifacts/s1/output/a.png"
OWN_B = "/api/artifact?key=artifacts/s1/output/b.mp4"
FAL = "https://v3b.fal.media/files/b/abc/ref.png"


def _nested_input():
    return {
        "endpoint_id": "fal-ai/flux/dev/image-to-image",
        "input": {
            "image_url": OWN_A,
            "reference_images": [OWN_A, FAL, {"url": OWN_B}],
            "prompt": f"see {OWN_A} for style",   # not EXACTLY a URL -> untouched
            "key": "artifacts/s1/output/a.png",    # bare key -> untouched (likely a key param)
            "n": 2,
        },
    }


def test_presign_walk_rewrites_only_exact_own_urls(s3):
    src = _nested_input()
    out, changed = af.presign_artifact_urls_in(src, 21600)
    assert changed
    inp = out["input"]
    assert inp["image_url"].startswith(f"https://{BUCKET}.s3.") and "X-Amz-Signature" in inp["image_url"]
    assert inp["reference_images"][0] == inp["image_url"]
    assert inp["reference_images"][1] == FAL
    assert "artifacts/s1/output/b.mp4" in inp["reference_images"][2]["url"]
    assert inp["prompt"] == f"see {OWN_A} for style"
    assert inp["key"] == "artifacts/s1/output/a.png"
    assert inp["n"] == 2
    assert src == _nested_input()  # caller's object untouched
    assert all(c[3] == 21600 for c in s3.calls if c[0] == "presign")


def test_presign_walk_no_match_returns_same_object(s3):
    src = {"input": {"image_url": FAL, "list": [1, "x"]}}
    out, changed = af.presign_artifact_urls_in(src)
    assert out is src and changed is False
    assert s3.calls == []


def test_presign_walk_failure_keeps_original(s3, caplog):
    s3.presign_error = RuntimeError("no credentials")
    src = {"image_url": OWN_A}
    with caplog.at_level("WARNING"):
        out, changed = af.presign_artifact_urls_in(src)
    assert out == {"image_url": OWN_A} and changed is False
    assert any("presign" in r.getMessage() for r in caplog.records)


def _run_base_stream(monkeypatch, tool_use):
    from jobs import strands_tool_wrap as stw
    captured = {}

    async def _fake_stream(self, tu, invocation_state, **kwargs):
        captured["tool_use"] = tu
        yield {"done": True}

    monkeypatch.setattr(stw.MCPAgentTool, "stream", _fake_stream)

    class _Tool(stw._ResilientMCPMixin):
        mcp_client = object()

    async def _run():
        return [ev async for ev in _Tool()._base_stream_once(tool_use, {}, {})]

    events = asyncio.run(_run())
    assert events == [{"done": True}]
    return captured["tool_use"]


@pytest.mark.parametrize("flag", ["true", "TRUE", "1", "yes", "Yes"])
def test_stream_rewrites_when_flag_on(s3, monkeypatch, flag):
    monkeypatch.setenv("ARTIFACTS_CDN_PRIVATE", flag)
    tool_use = {"toolUseId": "t1", "name": "usr-x___submit_job", "input": _nested_input()}
    sent = _run_base_stream(monkeypatch, tool_use)
    assert "X-Amz-Signature" in sent["input"]["input"]["image_url"]
    assert sent["input"]["input"]["reference_images"][1] == FAL
    assert sent["toolUseId"] == "t1" and sent["name"] == "usr-x___submit_job"
    assert tool_use["input"] == _nested_input()  # the agent's copy keeps the stable identifier


@pytest.mark.parametrize("flag", [None, "", "false", "0", "no", "off"])
def test_stream_never_rewrites_when_flag_off(s3, monkeypatch, flag):
    if flag is None:
        monkeypatch.delenv("ARTIFACTS_CDN_PRIVATE", raising=False)
    else:
        monkeypatch.setenv("ARTIFACTS_CDN_PRIVATE", flag)
    tool_use = {"toolUseId": "t1", "name": "usr-x___submit_job", "input": _nested_input()}
    sent = _run_base_stream(monkeypatch, tool_use)
    assert sent["input"] == _nested_input()
    assert not [c for c in s3.calls if c[0] == "presign"]


def test_stream_presign_failure_passes_original(s3, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CDN_PRIVATE", "true")
    s3.presign_error = RuntimeError("expired token")
    tool_use = {"toolUseId": "t1", "name": "fal___submit", "input": {"image_url": OWN_A}}
    sent = _run_base_stream(monkeypatch, tool_use)
    assert sent["input"] == {"image_url": OWN_A}


def test_stream_still_normalizes_duration_with_flag_on(s3, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_CDN_PRIVATE", "true")
    tool_use = {"toolUseId": "t1", "name": "fal___submit",
                "input": {"input": {"duration": "7s", "image_url": OWN_A}}}
    sent = _run_base_stream(monkeypatch, tool_use)
    assert sent["input"]["input"]["duration"] == "7"
    assert "X-Amz-Signature" in sent["input"]["input"]["image_url"]


# ---------------------------------------------------------------------------
# Code-interpreter sandbox: CDN artifact URLs embedded in code / the crop source
# ---------------------------------------------------------------------------

def _ci_use(code: str) -> dict:
    return {"toolUseId": "t1", "name": "code_interpreter",
            "input": {"code_interpreter_input": {"action": {"type": "executeCode", "language": "python",
                                                            "code": code}}}}


def test_presign_in_text_replaces_only_cdn_artifact_urls(s3):
    code = (f'a = "https://{DOMAIN}/artifacts/s1/output/a.mp4"\n'
            f"b = 'https://{DOMAIN}/artifacts/s1/output/b.wav'\n"
            'c = "https://v3b.fal.media/files/x.mp4"\n'
            f'd = "https://{DOMAIN}/tools/ffmpeg/ffmpeg-release-amd64-static.tar.xz"\n')
    out, changed = af.presign_artifact_urls_in_text(code)
    assert changed
    assert f"https://{DOMAIN}/artifacts/" not in out
    assert out.count("X-Amz-Signature") == 2
    assert "https://v3b.fal.media/files/x.mp4" in out          # foreign URL untouched
    assert f"https://{DOMAIN}/tools/ffmpeg/" in out             # non-artifact path untouched


def test_presign_in_text_keeps_url_when_presign_fails(monkeypatch):
    fake = FakeS3(presign_error=RuntimeError("no creds"))
    monkeypatch.setenv("ARTIFACTS_DOMAIN", DOMAIN)
    monkeypatch.setenv("SKILLS_S3_BUCKET", BUCKET)
    monkeypatch.setattr(af, "_client", lambda: fake)
    code = f'x = "https://{DOMAIN}/artifacts/s1/a.mp4"'
    assert af.presign_artifact_urls_in_text(code) == (code, False)


def test_code_interpreter_rewrite_only_when_cdn_private(s3, monkeypatch):
    import tool_input_normalize as tin
    use = _ci_use(f'src = "https://{DOMAIN}/artifacts/s1/output/a.mp4"')
    assert tin.presign_artifact_urls_in_code(use) is use       # flag off: same object, untouched

    monkeypatch.setenv("ARTIFACTS_CDN_PRIVATE", "true")
    out = tin.presign_artifact_urls_in_code(use)
    new_code = out["input"]["code_interpreter_input"]["action"]["code"]
    assert "X-Amz-Signature" in new_code
    assert use["input"]["code_interpreter_input"]["action"]["code"].startswith('src = "https://' + DOMAIN)  # original intact
    assert out["input"]["code_interpreter_input"]["action"]["type"] == "executeCode"


def test_code_interpreter_rewrite_ignores_other_shapes(s3, monkeypatch):
    import tool_input_normalize as tin
    monkeypatch.setenv("ARTIFACTS_CDN_PRIVATE", "1")
    for use in [{"input": {}}, {"input": {"code_interpreter_input": "not-a-dict"}},
                {"input": {"code_interpreter_input": {"action": {"type": "listFiles"}}}},
                _ci_use("print('no urls here')"), "not-a-dict"]:
        assert tin.presign_artifact_urls_in_code(use) is use


def test_crop_video_sandbox_gets_presigned_source_when_private(s3, monkeypatch):
    import tools.crop_video as cv
    seen = {}

    def _capture(code, timeout):
        seen["code"] = code
        return ""

    monkeypatch.setattr(cv, "_run_in_sandbox", _capture)
    from media import persist_media
    monkeypatch.setattr(persist_media, "validate_fetchable", lambda url: None)
    src = f"https://{DOMAIN}/artifacts/s1/input/clip.mp4"

    cv.crop_video(source_url=src, box="0,0,0.5,0.5")
    assert f"_SRC = {json.dumps(src)}" in seen["code"]         # flag off: original URL

    seen.clear()
    monkeypatch.setenv("ARTIFACTS_CDN_PRIVATE", "true")
    cv.crop_video(source_url=src, box="0,0,0.5,0.5")
    assert "X-Amz-Signature" in seen["code"]                  # flag on: presigned S3 GET
    assert f"_SRC = {json.dumps(src)}" not in seen["code"]


def test_presign_in_text_re_signs_our_expired_presigned_url(s3):
    old = (f"https://{BUCKET}.s3.us-west-2.amazonaws.com/artifacts/s1/a.mp4"
           "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Expires=1&X-Amz-Signature=old")
    code = f'urllib.request.urlretrieve("{old}", "/tmp/a.mp4")'
    out, changed = af.presign_artifact_urls_in_text(code, 3600)
    assert changed
    assert "Signature=old" not in out and "X-Amz-Expires=3600" in out
    assert out.startswith('urllib.request.urlretrieve("https://') and out.endswith('", "/tmp/a.mp4")')


def test_presign_in_text_leaves_other_buckets_alone(s3):
    code = 'x = "https://m2cv6-proxy.s3.us-west-2.amazonaws.com/artifacts/s1/a.mp4?X-Amz-Signature=z"'
    assert af.presign_artifact_urls_in_text(code) == (code, False)


def test_presigned_urls_use_the_regional_host(monkeypatch):
    """The real shared client presigns on ``<bucket>.s3.<region>.amazonaws.com``: on the global host a
    day-old bucket routes to us-east-1 and rejects the regional SigV4 credential (fresh-install 403)."""
    monkeypatch.setattr(af, "_S3_CLIENT", None)
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    url = af._client().generate_presigned_url(
        "get_object", Params={"Bucket": "my-bucket", "Key": "artifacts/a.mp4"}, ExpiresIn=60)
    monkeypatch.setattr(af, "_S3_CLIENT", None)
    assert url.startswith("https://my-bucket.s3.us-west-2.amazonaws.com/artifacts/a.mp4?")
    assert "X-Amz-Credential=" in url


# --- Our copy of another system's output is handed to tools as the ORIGINAL (staging 0f31776c: a scene enrichment was
# given the workspace copy of shots.json and failed "must be on the proxy bucket").

class _HeadS3:
    def __init__(self, objects):
        self.objects, self.heads = objects, []

    def head_object(self, Bucket, Key):
        self.heads.append(f"{Bucket}/{Key}")
        if f"{Bucket}/{Key}" not in self.objects:
            err = Exception("404")
            err.response = {"Error": {"Code": "404"}}
            raise err
        return {"Metadata": self.objects[f"{Bucket}/{Key}"]}


COPY = "projects/p1/sessions/s1/output/72b9/shots.json"
ORIGIN = "s3://m2c-proxy/micro-workflow/shot-segmentation/72b9/embeddings/shots.json"


def _origin_env(monkeypatch, objects):
    monkeypatch.setenv("SKILLS_S3_BUCKET", "ours")
    af._ORIGIN_CACHE.clear()
    fake = _HeadS3(objects)
    monkeypatch.setattr(af, "_client", lambda: fake)
    return fake


def test_an_s3_uri_of_our_copy_becomes_its_original_anywhere_in_the_input(monkeypatch):
    _origin_env(monkeypatch, {f"ours/{COPY}": {"origin": ORIGIN}, ORIGIN[5:]: {}})
    args = {"shot_segmentation_s3uri": f"s3://ours/{COPY}", "use_batch": False, "more": [f"s3://ours/{COPY}"]}
    out, changed = af.origin_refs_in(args)
    assert changed and out == {"shot_segmentation_s3uri": ORIGIN, "use_batch": False, "more": [ORIGIN]}
    assert args["shot_segmentation_s3uri"] == f"s3://ours/{COPY}"  # the caller's input is never mutated


def test_an_https_link_to_the_copy_is_left_alone_so_a_web_fetching_tool_still_can(monkeypatch):
    fake = _origin_env(monkeypatch, {f"ours/{COPY}": {"origin": ORIGIN}, ORIGIN[5:]: {}})
    assert af.origin_refs_in({"image_url": f"https://cdn.example/{COPY}"}) == ({"image_url": f"https://cdn.example/{COPY}"}, False)
    assert fake.heads == []


def test_no_origin_or_an_original_that_is_gone_keeps_the_copy(monkeypatch):
    _origin_env(monkeypatch, {f"ours/{COPY}": {"origin": ORIGIN}, "ours/projects/p1/x.wav": {}})
    assert af.origin_refs_in(f"s3://ours/{COPY}") == (f"s3://ours/{COPY}", False)          # the original was cleaned up
    assert af.origin_refs_in("s3://ours/projects/p1/x.wav") == ("s3://ours/projects/p1/x.wav", False)  # ours, no origin
    assert af.origin_refs_in("s3://elsewhere/k.json") == ("s3://elsewhere/k.json", False)  # not ours: untouched


def test_an_original_we_may_not_read_still_stands_and_each_copy_is_looked_up_once(monkeypatch):
    class _Denied(_HeadS3):
        def head_object(self, Bucket, Key):
            if Bucket == "m2c-proxy":
                self.heads.append(f"{Bucket}/{Key}")
                err = Exception("403"); err.response = {"Error": {"Code": "403"}}
                raise err
            return super().head_object(Bucket, Key)
    monkeypatch.setenv("SKILLS_S3_BUCKET", "ours")
    af._ORIGIN_CACHE.clear()
    fake = _Denied({f"ours/{COPY}": {"origin": ORIGIN}})
    monkeypatch.setattr(af, "_client", lambda: fake)
    assert af.origin_refs_in(f"s3://ours/{COPY}")[0] == ORIGIN   # the producer's own tools read it with their role
    af.origin_refs_in(f"s3://ours/{COPY}")
    assert fake.heads.count(f"ours/{COPY}") == 1


def test_every_gateway_tool_call_swaps_copies_for_originals_before_presigning():
    src = (Path(__file__).resolve().parents[1] / "src" / "jobs" / "strands_tool_wrap.py").read_text()
    body = src[src.index("async def _base_stream_once("):]
    assert body.index("await _origin_refs_inputs(tool_use)") < body.index("await _presign_own_artifact_inputs(tool_use)")
