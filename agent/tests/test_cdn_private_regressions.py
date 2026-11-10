"""Regressions found in staging after the artifacts CDN went private (``artifactsCdnPrivate``).

* A1 — session 36685185: ``code_interpreter`` executeCommand ``curl <cdn>/artifacts/...mp4`` -> 403;
  only ``action.code`` was presigned, so ``command`` / writeFiles ``content[].text`` bypassed it.
* A2 — session 36685185: ``http_request`` HEAD on ``<cdn>/artifacts/.../shot01.jpg`` -> 403.
* A3 — the model stringifies ``code_interpreter_input`` with ONE extra trailing ``}``.
* A4 — session 865950fd: the end-of-turn harvester took ``...report.json\\nhttps://...report.pdf``
  (a LITERAL backslash-n) as ONE URL and opened a document tab on it.
* A5 — ``fal-ai/minimax/h3-max/...`` routes to the app ``fal-ai/minimax`` (COMPLETED in seconds,
  result 404 every time); the real id is ``minimax/h3-max/...``.

No network, no real AWS: a fake S3 client that records the presign operation.
"""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest

from media import artifact_fetch as af

DOMAIN = "dexamplecdn002.cloudfront.net"
BUCKET = "skills-bucket"
SID = "36685185-e66a-4e1d-b599-e4dbbcef03c9"
CLIP = f"https://{DOMAIN}/artifacts/{SID}/output/meridian_trim_0_75019.mp4"
FRAME = f"https://{DOMAIN}/artifacts/{SID}/output/shot01.jpg"
FAL = "https://v3b.fal.media/files/b/abc/out.mp4"


class FakeS3:
    def __init__(self, presign_error=None):
        self.calls: list = []
        self.presign_error = presign_error

    def generate_presigned_url(self, op, Params, ExpiresIn):
        self.calls.append((op, Params["Key"], ExpiresIn))
        if self.presign_error:
            raise self.presign_error
        return (f"https://{Params['Bucket']}.s3.us-west-2.amazonaws.com/{Params['Key']}"
                f"?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=sig&op={op}")


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setenv("ARTIFACTS_DOMAIN", DOMAIN)
    monkeypatch.setenv("SKILLS_S3_BUCKET", BUCKET)
    monkeypatch.setenv("ARTIFACTS_CDN_PRIVATE", "true")
    monkeypatch.setattr(af, "_client", lambda: fake)
    return fake


def _ci(action: dict) -> dict:
    return {"toolUseId": "t1", "name": "code_interpreter",
            "input": {"code_interpreter_input": {"action": action}}}


# ---------------------------------------------------------------------------
# A1 — every string under code_interpreter_input.action
# ---------------------------------------------------------------------------

def test_execute_command_curl_is_presigned_and_shell_quoted(s3):
    import tool_input_normalize as tin
    # The exact session-36685185 shapes: a bare curl, and curl -D (dump headers).
    use = _ci({"type": "executeCommand",
               "command": f"curl -sS -o /tmp/in.mp4 {CLIP} && curl -s -D - -o /dev/null {CLIP}"})
    before = copy.deepcopy(use)
    out = tin.presign_artifact_urls_in_code(use)
    cmd = out["input"]["code_interpreter_input"]["action"]["command"]
    assert f"https://{DOMAIN}/artifacts/" not in cmd
    assert cmd.count("X-Amz-Signature") == 2
    # A presigned URL carries `&`: unquoted, the shell would background the command at it.
    assert cmd.count(f"'https://{BUCKET}.s3.us-west-2.amazonaws.com/") == 2
    assert out["input"]["code_interpreter_input"]["action"]["type"] == "executeCommand"
    assert use == before                                       # caller's object untouched


def test_execute_command_keeps_existing_quotes(s3):
    import tool_input_normalize as tin
    out = tin.presign_artifact_urls_in_code(
        _ci({"type": "executeCommand", "command": f'curl -o a.mp4 "{CLIP}"'}))
    cmd = out["input"]["code_interpreter_input"]["action"]["command"]
    assert cmd.startswith('curl -o a.mp4 "https://' + BUCKET)
    assert "'\"" not in cmd and "\"'" not in cmd             # not double-wrapped


def test_write_files_text_is_presigned_and_unchanged_entries_shared(s3):
    import tool_input_normalize as tin
    other = {"path": "notes.txt", "text": "no urls here"}
    use = _ci({"type": "writeFiles", "content": [
        {"path": "fetch.py", "text": f'SRC = "{CLIP}"\n'},
        {"path": "run.sh", "text": f"curl -o f.jpg {FRAME}\n"},
        other,
    ]})
    out = tin.presign_artifact_urls_in_code(use)
    content = out["input"]["code_interpreter_input"]["action"]["content"]
    assert "X-Amz-Signature" in content[0]["text"] and DOMAIN not in content[0]["text"]
    assert content[0]["text"].startswith('SRC = "https://' + BUCKET)   # python: no extra quotes
    assert content[1]["text"].startswith("curl -o f.jpg 'https://" + BUCKET)  # shell file: quoted
    assert content[2] is other                                 # only changed containers copied
    assert use["input"]["code_interpreter_input"]["action"]["content"][0]["text"] == f'SRC = "{CLIP}"\n'


def test_execute_code_still_presigned(s3):
    import tool_input_normalize as tin
    out = tin.presign_artifact_urls_in_code(
        _ci({"type": "executeCode", "language": "python", "code": f'u = "{CLIP}"'}))
    code = out["input"]["code_interpreter_input"]["action"]["code"]
    assert "X-Amz-Signature" in code and DOMAIN not in code


def test_ci_rewrite_flag_off_and_no_url_return_same_object(s3, monkeypatch):
    import tool_input_normalize as tin
    use = _ci({"type": "executeCommand", "command": f"curl {CLIP}"})
    monkeypatch.setenv("ARTIFACTS_CDN_PRIVATE", "false")
    assert tin.presign_artifact_urls_in_code(use) is use
    monkeypatch.setenv("ARTIFACTS_CDN_PRIVATE", "true")
    plain = _ci({"type": "executeCommand", "command": f"curl {FAL}"})
    assert tin.presign_artifact_urls_in_code(plain) is plain


def test_ci_rewrite_never_raises(s3, monkeypatch):
    import tool_input_normalize as tin

    def boom(*a, **k):
        raise RuntimeError("signer exploded")

    monkeypatch.setattr(af, "presign_artifact_urls_in_text", boom)
    use = _ci({"type": "executeCommand", "command": f"curl {CLIP}"})
    assert tin.presign_artifact_urls_in_code(use) is use


def test_ci_presign_failure_leaves_url(monkeypatch):
    import tool_input_normalize as tin
    monkeypatch.setenv("ARTIFACTS_DOMAIN", DOMAIN)
    monkeypatch.setenv("SKILLS_S3_BUCKET", BUCKET)
    monkeypatch.setenv("ARTIFACTS_CDN_PRIVATE", "true")
    monkeypatch.setattr(af, "_client", lambda: FakeS3(presign_error=RuntimeError("no creds")))
    use = _ci({"type": "executeCommand", "command": f"curl {CLIP}"})
    assert tin.presign_artifact_urls_in_code(use) is use


# ---------------------------------------------------------------------------
# A2 — http_request
# ---------------------------------------------------------------------------

def _http(url, method="GET", **extra):
    return {"toolUseId": "h1", "name": "http_request",
            "input": {"method": method, "url": url, **extra}}


def test_http_request_head_gets_head_presign_and_keeps_other_fields(s3):
    import tool_input_normalize as tin
    use = _http(FRAME, "HEAD", headers={"Range": "bytes=0-0"})
    out = tin.presign_artifact_url_in_http_request(use)
    assert out["input"]["url"].startswith(f"https://{BUCKET}.s3.us-west-2.amazonaws.com/")
    # S3 signs the method: a GET-presigned URL would 403 this HEAD.
    assert s3.calls == [("head_object", f"artifacts/{SID}/output/shot01.jpg",
                         tin.HTTP_REQUEST_PRESIGN_TTL_S)]
    assert tin.HTTP_REQUEST_PRESIGN_TTL_S == 3600
    assert out["input"]["method"] == "HEAD"
    assert out["input"]["headers"] == {"Range": "bytes=0-0"}
    assert use["input"]["url"] == FRAME                        # caller's object untouched


def test_http_request_get_and_default_method(s3):
    import tool_input_normalize as tin
    out = tin.presign_artifact_url_in_http_request(
        {"toolUseId": "h", "input": {"url": CLIP}})
    assert "X-Amz-Signature" in out["input"]["url"]
    assert s3.calls[-1][0] == "get_object"


@pytest.mark.parametrize("use", [
    _http(FAL),                                 # foreign URL
    _http(f"https://{DOMAIN}/tools/ffmpeg/ffmpeg-release-amd64-static.tar.xz"),  # non-artifact path
    _http(CLIP, "PUT"),                         # no presign equivalent
    {"toolUseId": "h", "input": {"method": "GET"}},
    {"toolUseId": "h", "input": "not-a-dict"},
    "not-a-dict",
])
def test_http_request_untouched_shapes(s3, use):
    import tool_input_normalize as tin
    assert tin.presign_artifact_url_in_http_request(use) is use


def test_http_request_flag_off_and_presign_failure_fail_open(s3, monkeypatch):
    import tool_input_normalize as tin
    use = _http(CLIP)
    monkeypatch.setenv("ARTIFACTS_CDN_PRIVATE", "0")
    assert tin.presign_artifact_url_in_http_request(use) is use
    monkeypatch.setenv("ARTIFACTS_CDN_PRIVATE", "true")
    s3.presign_error = RuntimeError("expired")
    assert tin.presign_artifact_url_in_http_request(use) is use


def test_wrap_http_request_wraps_the_real_tool_and_rewrites_on_stream(s3, monkeypatch):
    import tool_input_normalize as tin
    from security import url_guard
    from tools.http_request import http_request

    # The presigned S3 URL is then vetted by the SSRF guard; resolve it without network.
    monkeypatch.setattr(url_guard, "_resolver", lambda host, port: ["52.92.0.1"])

    wrapped = tin.wrap_http_request(http_request)
    assert isinstance(wrapped, tin.PresignedHttpRequestTool)
    assert wrapped.tool_name == "http_request"
    assert wrapped.tool_spec["name"] == "http_request"
    assert tin.wrap_http_request(wrapped) is wrapped           # idempotent

    seen = {}

    class Inner:
        async def stream(self, tool_use, invocation_state, **kwargs):
            seen["tool_use"] = tool_use
            yield "RESULT"

    wrapped._inner = Inner()

    async def _run():
        return [ev async for ev in wrapped.stream(_http(FRAME, "HEAD"), {})]

    assert asyncio.run(_run()) == ["RESULT"]
    assert "X-Amz-Signature" in seen["tool_use"]["input"]["url"]


def test_wrap_http_request_never_breaks_registration():
    import tool_input_normalize as tin
    junk = object()
    assert tin.wrap_http_request(junk) is junk
    assert tin.wrap_http_request(None) is None


def test_main_and_task_agent_register_the_wrapped_http_request():
    src = Path(__file__).resolve().parents[1] / "src"
    assert '_all_builtin_tools["http_request"] = wrap_http_request(http_request)' in (src / "main.py").read_text()
    assert '_BUILTINS["http_request"] = wrap_http_request(http_request)' in (src / "task_agent.py").read_text()


# ---------------------------------------------------------------------------
# A3 — stringified object with a trailing extra '}'
# ---------------------------------------------------------------------------

_OBJ = {"action": {"type": "executeCommand", "command": "ls -la /tmp"}}


def test_coerce_accepts_exact_failure_shape_valid_object_plus_brace():
    from tool_input_normalize import coerce_json_string_to_dict
    s = json.dumps(_OBJ) + "}"
    with pytest.raises(json.JSONDecodeError, match="Extra data"):
        json.loads(s)                                          # the observed failure
    assert coerce_json_string_to_dict(s) == _OBJ


@pytest.mark.parametrize("tail", ["}}", " }\n", "}\n}", " } } "])
def test_coerce_accepts_only_brace_and_whitespace_tails(tail):
    from tool_input_normalize import coerce_json_string_to_dict
    assert coerce_json_string_to_dict(json.dumps(_OBJ) + tail) == _OBJ


@pytest.mark.parametrize("tail", ["]", "}x", ', "b": 1}', "{}", "}]"])
def test_coerce_rejects_other_trailing_text(tail):
    from tool_input_normalize import coerce_json_string_to_dict
    s = json.dumps(_OBJ) + tail
    assert coerce_json_string_to_dict(s) is s


def test_normalize_tool_use_with_trailing_brace():
    from tool_input_normalize import normalize_code_interpreter_input
    use = {"toolUseId": "t", "input": {"code_interpreter_input": json.dumps(_OBJ) + "}"}}
    out = normalize_code_interpreter_input(use)
    assert out["input"]["code_interpreter_input"] == _OBJ
    assert isinstance(use["input"]["code_interpreter_input"], str)


# ---------------------------------------------------------------------------
# A4 — a literal backslash ends a harvested URL
# ---------------------------------------------------------------------------

_HARVEST_SID = "865950fd-1111-2222-3333-444455556666"
_JSON_URL = f"https://{DOMAIN}/artifacts/{_HARVEST_SID}/output/compliance_report.json"
_PDF_URL = f"https://{DOMAIN}/artifacts/{_HARVEST_SID}/output/compliance_report.pdf"
_ESCAPED = _JSON_URL + "\\n" + _PDF_URL                        # LITERAL backslash + n


def test_exact_string_has_a_literal_backslash():
    assert "\\" in _ESCAPED and "\n" not in _ESCAPED


def test_provider_extract_urls_splits_on_literal_backslash_n():
    from hooks.provider import extract_urls
    out = extract_urls({"content": [{"text": f"Wrote:\\n{_ESCAPED}\\n"}]})
    assert out == [_JSON_URL, _PDF_URL]
    assert all("\\" not in u for u in out)


def test_provider_regex_and_json_block():
    from hooks import provider
    assert provider._URL_RE.findall(_ESCAPED) == [_JSON_URL, _PDF_URL]
    out = provider.extract_urls({"content": [{"json": {"stdout": _ESCAPED}}]})
    assert out == [_JSON_URL, _PDF_URL]


def test_provider_drops_any_candidate_containing_a_backslash(monkeypatch):
    import re
    from hooks import provider
    # Even if a future regex edit lets a backslash through, the candidate is dropped.
    monkeypatch.setattr(provider, "_URL_RE", re.compile(r"https?://\S+"))
    out = provider.extract_urls({"content": [{"text": _ESCAPED + " " + FAL}]})
    assert out == [FAL]


def test_persist_media_regex_splits_on_literal_backslash():
    from media import persist_media
    assert persist_media._URL_RE.findall(_ESCAPED) == [_JSON_URL, _PDF_URL]
    fal_pair = "https://v3b.fal.media/files/a.mp4\\nhttps://v3b.fal.media/files/b.png"
    assert persist_media.external_media_urls({"text": fal_pair}, DOMAIN) == [
        "https://v3b.fal.media/files/a.mp4", "https://v3b.fal.media/files/b.png"]


def test_render_agent_and_source_media_regexes_stop_at_backslash():
    import render_agent
    from media import source_media
    assert render_agent._extract_urls(_ESCAPED) == [_JSON_URL, _PDF_URL]
    assert source_media._S3_URI_RE.findall("s3://b/k/a.mp4\\ns3://b/k/b.wav") == [
        "s3://b/k/a.mp4", "s3://b/k/b.wav"]


def test_provider_s3_uri_regex_stops_at_backslash():
    from hooks import provider
    assert [m.group(0) for m in provider._S3_URI_RE.finditer("s3://my-bucket/k/a.mp4\\ns3://my-bucket/k/b.wav")] == [
        "s3://my-bucket/k/a.mp4", "s3://my-bucket/k/b.wav"]


# ---------------------------------------------------------------------------
# A5 — fal-ai/minimax/h3-max -> minimax/h3-max
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["endpoint_id", "model_id", "application"])
def test_wrong_h3_max_prefix_rewritten(key):
    from tool_args import normalize_endpoint_ids
    src = {key: "fal-ai/minimax/h3-max/image-to-video", "input": {"image_url": "x"}}
    out = normalize_endpoint_ids(src)
    assert out[key] == "minimax/h3-max/image-to-video"
    assert src[key] == "fal-ai/minimax/h3-max/image-to-video"  # not mutated


@pytest.mark.parametrize("value", [
    "minimax/h3-max/image-to-video",          # already right
    "fal-ai/minimax/speech-2.8-turbo",        # other fal-ai/minimax models untouched
    "fal-ai/minimax",                         # the queue app id the poller derives
    "fal-ai/kling-video/v3/pro/image-to-video",
    "xfal-ai/minimax/h3-max/image-to-video",  # only the exact prefix
])
def test_other_ids_untouched(value):
    from tool_args import normalize_endpoint_ids
    src = {"endpoint_id": value}
    assert normalize_endpoint_ids(src) is src


def test_rewrite_only_under_endpoint_keys_and_nested():
    from tool_args import normalize_endpoint_ids
    src = {"prompt": "fal-ai/minimax/h3-max/text-to-video", "input": {
        "model_id": "fal-ai/minimax/h3-max/text-to-video"}}
    out = normalize_endpoint_ids(src)
    assert out["prompt"] == "fal-ai/minimax/h3-max/text-to-video"  # free text untouched
    assert out["input"]["model_id"] == "minimax/h3-max/text-to-video"


def test_base_stream_once_applies_endpoint_and_duration_normalization(monkeypatch):
    """On the path Strands actually invokes (``_ResilientMCPMixin._base_stream_once``)."""
    import jobs.strands_tool_wrap as stw
    sent = {}

    class FakeBase:
        async def stream(self, tool_use, invocation_state, **kwargs):
            sent.update(tool_use)
            yield "OK"

    monkeypatch.setattr(stw, "MCPAgentTool", FakeBase)
    monkeypatch.delenv("ARTIFACTS_CDN_PRIVATE", raising=False)

    class Tool(stw._ResilientMCPMixin):
        pass

    t = Tool()
    t.mcp_client = "c"
    use = {"toolUseId": "t1", "name": "usr-x___submit_job",
           "input": {"endpoint_id": "fal-ai/minimax/h3-max/image-to-video",
                     "input": {"duration": "7s", "image_url": "https://x/y.png"}}}

    async def _run():
        return [ev async for ev in t._base_stream_once(use, {}, {})]

    assert asyncio.run(_run()) == ["OK"]
    assert sent["input"]["endpoint_id"] == "minimax/h3-max/image-to-video"
    assert sent["input"]["input"]["duration"] == "7"
    assert use["input"]["endpoint_id"] == "fal-ai/minimax/h3-max/image-to-video"


def test_skills_never_name_the_prefixed_h3_max_id_or_eventual_consistency():
    skills = Path(__file__).resolve().parents[1] / "skills"
    for p in skills.rglob("*.md"):
        t = p.read_text()
        assert "fal-ai/minimax/h3-max" not in t, p
    mr = (skills / "media-references" / "SKILL.md").read_text()
    assert "eventual consistency" not in mr
    assert "endpoint id" in mr and "is wrong" in mr and "re-poll" in mr


def test_video_processing_skill_states_how_the_sandbox_reads_media():
    t = (Path(__file__).resolve().parents[1] / "skills" / "video-processing" / "SKILL.md").read_text()
    assert "the same host you build clip URLs from" not in t
    assert "SKILLS_S3_BUCKET" in t and "M2C proxy bucket is NOT readable" in t
    assert "exploratory `process_media` job" in t


# Prod session d52d544e (2026-10-12): the picture-book page built each panel address from a base, and the FOLDER
# `artifacts/external/` was signed with the panel name appended after the signature -- every image broke. A URL
# prefix or template is never signed; a complete URL in the same code still is.
@pytest.mark.parametrize("code", [
    f'BASE = "https://{DOMAIN}/artifacts/external/"\nsrc = BASE + pid + ".jpg"',
    f'src = f"https://{DOMAIN}/artifacts/external/{{pid}}.jpg"',
    f'src = "https://{DOMAIN}/artifacts/external/panel_" + str(i) + ".jpg"',
    f'src = "https://{DOMAIN}/artifacts/external/panel_%s.jpg" % i',
    f'src = "https://{DOMAIN}/artifacts/external/panel_{{}}.jpg".format(i)',
    f'const src = `https://{DOMAIN}/artifacts/external/${{pid}}.jpg`;',
])
def test_a_url_template_or_prefix_is_never_signed(s3, code):
    out, changed = af.presign_artifact_urls_in_text(code, trusted_project_id=None)
    assert out == code and not changed
    assert s3.calls == []


def test_a_complete_url_next_to_a_template_is_still_signed(s3):
    whole = f"https://{DOMAIN}/artifacts/external/47a92488316043d3c789ab82.jpg"
    code = f'urls = ["{whole}"]\nBASE = "https://{DOMAIN}/artifacts/external/"'
    out, changed = af.presign_artifact_urls_in_text(code, trusted_project_id=None)
    assert changed and [c[1] for c in s3.calls] == ["artifacts/external/47a92488316043d3c789ab82.jpg"]
    assert f'BASE = "https://{DOMAIN}/artifacts/external/"' in out  # the prefix is untouched
