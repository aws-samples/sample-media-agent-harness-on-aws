"""Task 4 — HarnessHookProvider bridge tests.

Uses lightweight stubs for Strands events (the normalizers read plain
attributes) and a fake registry to verify callback registration.
"""

from __future__ import annotations

import json
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from orchestration import progress_channel  # noqa: E402
from hooks.engine import HookEngine, HookSpec  # noqa: E402
from hooks.provider import (  # noqa: E402
    HarnessHookProvider,
    ctx_after_tool_result,
    extract_output_uris,
    extract_urls,
    role_for_tool,
)


def _after_tool_event(name, urls_text):
    return types.SimpleNamespace(
        tool_use={"name": name, "toolUseId": "tu-1", "input": {}},
        result={"toolUseId": "tu-1", "status": "success", "content": [{"text": urls_text}]},
    )


def test_role_classification():
    assert role_for_tool("x___start_diarization") == "start"
    assert role_for_tool("x___get_diarization_status") == "status"
    assert role_for_tool("x___get_diarization_results") == "results"
    assert role_for_tool("render_artifacts") == "other"


def test_extract_urls_from_text_and_json():
    result = {"content": [
        {"text": "see https://cf/a.wav and https://cf/b.vtt"},
        {"json": {"tracks": [{"url": "https://cf/c.mp3"}]}},
    ]}
    urls = extract_urls(result)
    assert urls == ["https://cf/a.wav", "https://cf/b.vtt", "https://cf/c.mp3"]


def test_ctx_after_tool_result_shape():
    ev = _after_tool_event("gw___get_diarization_results", "url https://cf/x.wav")
    ctx = ctx_after_tool_result(ev, {"actor": {"id": "u"}})
    assert ctx["event"] == "after_tool_result"
    assert ctx["tool"]["role"] == "results"
    assert ctx["result"]["urls"] == ["https://cf/x.wav"]
    assert "audio/wav" in ctx["result"]["mimes"]
    assert ctx["actor"] == {"id": "u"}


def test_provider_fires_engine_on_after_tool_result():
    ch = progress_channel.ProgressChannel()
    progress_channel.set_active_channel(ch)
    eng = HookEngine([HookSpec(
        "notify-results", "after_tool_result", 'tool.role == "results" and result.urls',
        "notify", {"message": "got {result.status}"}, priority=10,
    )])
    provider = HarnessHookProvider(eng)
    provider._on_after_tool(_after_tool_event("gw___get_diarization_results", "https://cf/x.wav"))
    events = ch.drain()
    assert any(e.agent == "hook" and "got success" in e.label for e in events)
    progress_channel.set_active_channel(None)


def test_layer1_convention_envelope():
    """Layer 1: tool returns outputs: [{uri, ...}] — the standard convention."""
    result = {"content": [{"json": {
        "status": "SUCCEEDED",
        "outputs": [
            {"uri": "s3://my-bucket/out/speech.wav", "mime": "audio/wav", "label": "Speech"},
            {"uri": "s3://my-bucket/out/music.wav", "mime": "audio/wav", "label": "Music"},
            {"uri": "s3://my-bucket/out/metadata.json", "mime": "application/json"},
        ],
    }}]}
    uris = extract_output_uris(result)
    assert "s3://my-bucket/out/speech.wav" in uris
    assert "s3://my-bucket/out/music.wav" in uris
    # metadata.json is never a renderable deliverable (it's a spec/answer) — excluded even as a
    # declared Layer-1 output, so a probe/analysis doesn't spawn a redundant metadata card tab.
    assert "s3://my-bucket/out/metadata.json" not in uris


def test_layer1_stops_early():
    """When Layer 1 matches, layers 2+3 don't add duplicates or extras."""
    result = {"content": [{"json": {
        "outputs": [{"uri": "s3://b/clip.mp4"}],
        "output_bucket": "b",
        "some_key": "other/file.wav",
    }}]}
    uris = extract_output_uris(result)
    assert uris == ["s3://b/clip.mp4"]


def test_layer2_explicit_s3_uris():
    """Layer 2: tool embeds full s3:// URIs in string values."""
    result = {"content": [{"json": {
        "result_uri": "s3://proxy-bucket/jobs/123/output.wav",
        "log_uri": "s3://proxy-bucket/jobs/123/log.json",
    }}]}
    uris = extract_output_uris(result)
    assert "s3://proxy-bucket/jobs/123/output.wav" in uris
    assert not any("log.json" in u for u in uris)


def test_layer2_s3_uri_in_text_block():
    """Layer 2: s3:// URI embedded in a text content block."""
    result = {"content": [
        {"text": '{"file": "s3://bucket/dir/track.m4a"}'},
    ]}
    uris = extract_output_uris(result)
    assert "s3://bucket/dir/track.m4a" in uris


def test_layer3_bucket_plus_keys_reconstruction():
    """Layer 3: bucket field + renderable keys in sibling fields (diarization shape)."""
    result = {"content": [{"json": {
        "status": "SUCCEEDED",
        "output_s3_prefix": "s3://mediaworks-proxy-100000000003-us-west-2/test-out",
        "transcript_vtt_key": "test-out/transcripts/final_transcript.vtt",
        "transcript_json_key": "test-out/transcripts/final_transcript.json",
        "usage_key": "test-out/usage_tracking.json",
    }}]}
    uris = extract_output_uris(result)
    assert "s3://mediaworks-proxy-100000000003-us-west-2/test-out/transcripts/final_transcript.vtt" in uris
    assert not any("usage_tracking.json" in u for u in uris)


def test_layer3_bucket_field_plus_nested_dict(  ):
    """Layer 3: output_bucket + output_keys dict (stem separation shape)."""
    result = {"content": [{"json": {
        "status": "SUCCEEDED",
        "output_bucket": "mediaworks-proxy-100000000003-us-west-2",
        "output_keys": {
            "speech": "stems/clip/audio_speech.wav",
            "music": "stems/clip/audio_music.wav",
            "sfx": "stems/clip/audio_sfx.wav",
        },
    }}]}
    uris = extract_output_uris(result)
    for stem in ("audio_speech.wav", "audio_music.wav", "audio_sfx.wav"):
        assert any(u.endswith(stem) for u in uris), f"missing {stem}"
    assert all(
        u.startswith("s3://mediaworks-proxy-100000000003-us-west-2/stems/clip/")
        for u in uris
    )


def test_layer3_unknown_tool_shape():
    """Layer 3: a new tool with a bucket and a LIST of keys under a field that says so."""
    result = {"content": [{"json": {
        "targetBucket": "new-tool-output-bucket",
        "artifact_keys": ["renders/frame_001.png", "renders/frame_002.png"],
        "status_file": "renders/status.txt",
    }}]}
    uris = extract_output_uris(result)
    assert "s3://new-tool-output-bucket/renders/frame_001.png" in uris
    assert "s3://new-tool-output-bucket/renders/frame_002.png" in uris
    assert not any("status.txt" in u for u in uris)


def test_layer3_never_turns_a_name_into_a_location():
    """A display name is not an object key. Redesign 2026-11-11: a content review's `videos[].name`
    ("Netflix_Open_Content_Meridian.mp4") sat beside its `sourceUri`, became
    `s3://<bucket>/Netflix_Open_Content_Meridian.mp4` (no such object) and opened a dead 10:00 video row."""
    src = "s3://skills-bucket/projects/p1/video:3a8c4a0e/Netflix_Open_Content_Meridian.mp4"
    result = {"content": [{"text": json.dumps({"review": {"videos": [
        {"assetId": "video:3a8c4a0e", "sourceUri": src, "name": "Netflix_Open_Content_Meridian.mp4",
         "title": "Meridian.mov", "fileName": "clip.mp4"}]}})}]}
    uris = extract_output_uris(result)
    assert uris == [src]
    # A bucket with names in a plain list is no shape of keys either.
    assert extract_output_uris({"content": [{"json": {
        "targetBucket": "b", "artifacts": ["renders/frame_001.png"]}}]}) == []


def test_layer2_does_not_suppress_layer3_vtt():
    """When Layer 2 finds an explicit input s3:// URI (e.g. asset_s3uri with .mp4),
    Layer 3 must still run to reconstruct output URIs from relative keys (the VTT).
    Regression: Layer 2's early-return previously short-circuited Layer 3."""
    result = {"content": [{"json": {
        "status": "SUCCEEDED",
        "execution_arn": "arn:aws:states:us-west-2:100000000002:execution:mediaworks-speech-diarization:abc",
        "asset_s3uri": "s3://mediaworks-harness-skills-store-100000000002-us-west-2/artifacts/Netflix.mp4",
        "output_s3_prefix": "s3://mediaworks-proxy-100000000002-us-west-2/diarization/meridian",
        "transcript_vtt_key": "diarization/meridian/transcripts/final_transcript.vtt",
        "transcript_json_key": "diarization/meridian/transcripts/final_transcript.json",
        "speaker_manifest_key": "diarization/meridian/speakers/speaker_manifest.json",
    }}]}
    uris = extract_output_uris(result)
    assert any("final_transcript.vtt" in u for u in uris), \
        f"VTT must be found even when Layer 2 already matched input MP4; got {uris}"
    assert any("Netflix.mp4" in u for u in uris), \
        f"Input MP4 should also be present; got {uris}"


def test_job_status_extracted_from_result():
    """ctx_after_tool_result exposes the async job status (SUCCEEDED/RUNNING)."""
    ev_succeeded = types.SimpleNamespace(
        tool_use={"name": "audio-stem___get_job_status", "toolUseId": "tu-4", "input": {}},
        result={"toolUseId": "tu-4", "status": "success", "content": [
            {"json": {"status": "SUCCEEDED", "output_bucket": "b", "output_keys": {"x": "a.wav"}}},
        ]},
    )
    ctx = ctx_after_tool_result(ev_succeeded)
    assert ctx["result"]["job_status"] == "SUCCEEDED"

    ev_running = types.SimpleNamespace(
        tool_use={"name": "audio-stem___get_job_status", "toolUseId": "tu-5", "input": {}},
        result={"toolUseId": "tu-5", "status": "success", "content": [
            {"json": {"status": "RUNNING", "output_bucket": "b", "output_keys": {"x": "a.wav"}}},
        ]},
    )
    ctx = ctx_after_tool_result(ev_running)
    assert ctx["result"]["job_status"] == "RUNNING"


def test_job_status_none_for_non_job_tools():
    """Non-job tools (artifact, etc.) have job_status = None."""
    ev = types.SimpleNamespace(
        tool_use={"name": "artifact", "toolUseId": "tu-6", "input": {}},
        result={"toolUseId": "tu-6", "status": "success", "content": [
            {"text": "https://cf/file.mp4"},
        ]},
    )
    ctx = ctx_after_tool_result(ev)
    assert ctx["result"]["job_status"] is None


def test_ctx_extracts_m2c_output_keys():
    """Regression: diarization shape flows through ctx_after_tool_result."""
    ev = types.SimpleNamespace(
        tool_use={"name": "speech-diarization___get_job_status", "toolUseId": "tu-2", "input": {}},
        result={"toolUseId": "tu-2", "status": "success", "content": [
            {"json": {
                "status": "SUCCEEDED",
                "output_s3_prefix": "s3://mediaworks-proxy-100000000003-us-west-2/test-out",
                "transcript_vtt_key": "test-out/transcripts/final_transcript.vtt",
                "transcript_json_key": "test-out/transcripts/final_transcript.json",
                "usage_key": "test-out/usage_tracking.json",
            }},
        ]},
    )
    ctx = ctx_after_tool_result(ev)
    urls = ctx["result"]["urls"]
    assert ctx["tool"]["role"] == "status"
    assert "s3://mediaworks-proxy-100000000003-us-west-2/test-out/transcripts/final_transcript.vtt" in urls
    assert "text/vtt" in ctx["result"]["mimes"]
    assert not any(u.endswith("usage_tracking.json") for u in urls)


def test_ctx_extracts_m2c_stem_output_keys():
    """Regression: stem separation shape flows through ctx_after_tool_result."""
    ev = types.SimpleNamespace(
        tool_use={"name": "audio-stem___get_job_status", "toolUseId": "tu-3", "input": {}},
        result={"toolUseId": "tu-3", "status": "success", "content": [
            {"json": {
                "status": "SUCCEEDED",
                "output_bucket": "mediaworks-proxy-100000000003-us-west-2",
                "output_keys": {
                    "speech": "stems/clip/audio_speech.wav",
                    "music": "stems/clip/audio_music.wav",
                    "sfx": "stems/clip/audio_sfx.wav",
                },
            }},
        ]},
    )
    ctx = ctx_after_tool_result(ev)
    urls = ctx["result"]["urls"]
    for stem in ("audio_speech.wav", "audio_music.wav", "audio_sfx.wav"):
        assert any(u.endswith(stem) for u in urls), f"missing {stem}"
    assert all(
        u.startswith("s3://mediaworks-proxy-100000000003-us-west-2/stems/clip/")
        for u in urls
    )


def test_provider_registers_expected_events():
    calls = []

    class FakeRegistry:
        def add_callback(self, event_type, cb):
            calls.append(event_type.__name__)

    HarnessHookProvider(HookEngine([])).register_hooks(FakeRegistry())
    assert set(calls) == {
        "AfterToolCallEvent", "BeforeToolCallEvent",
        "BeforeInvocationEvent", "AfterInvocationEvent",
    }


def test_future_status_tool_preserves_explicit_results_and_prefixes():
    """Any status tool using an outputs envelope gets the same hook behavior."""
    ev = types.SimpleNamespace(
        tool_use={"name": "future-tool___get_job_status", "toolUseId": "tu-future", "input": {}},
        result={"toolUseId": "tu-future", "status": "success", "content": [{"json": {
            "status": "SUCCEEDED",
            "outputs": [
                {"uri": "s3://proxy/run/analysis_metadata.json", "mime": "application/json"},
                {"uri": "s3://proxy/run/results.csv", "mime": "text/csv"},
                {"uri": "s3://proxy/run/usage_tracking.json", "label": "usage"},
                {"uri": "s3://proxy/run/previews/", "mime": "image/jpeg"},
            ],
        }}]},
    )
    ctx = ctx_after_tool_result(ev)
    assert ctx["tool"]["role"] == "status"
    assert ctx["result"]["job_status"] == "SUCCEEDED"
    assert ctx["result"]["urls"] == [
        "s3://proxy/run/analysis_metadata.json",
        "s3://proxy/run/results.csv",
        "s3://proxy/run/previews/",
    ]
    assert ctx["result"]["mimes"] == ["application/json", "text/csv"]


def test_layer3_nested_wav_key_in_list_of_dicts():
    # TTS puts the wav under data.iterators[].item_data.outputs[].wav_key, with
    # out_bucket in a sibling dict. The bucket+key reconstruction must walk the
    # full subtree (list-of-dicts included) or the audio never auto-renders.
    pref = "micro-workflow/tts-on-aws/output/cc1a0b45"
    result = {"content": [{"json": {"data": {"iterators": [{"item_data": {
        "out_bucket": "buck", "out_prefix": pref,
        "voice": {"s3_conditioning_key": "x/conditioning.npz"},
        "outputs": [{"wav_key": f"{pref}/cc1a0b45_0_0.wav", "duration_sec": 3.08}],
    }}]}}}]}
    uris = extract_output_uris(result)
    assert uris == [f"s3://buck/{pref}/cc1a0b45_0_0.wav"]


def test_html_artifact_url_is_a_renderable_new_artifact():
    # A main-agent HTML artifact is served from CloudFront (https), not s3://.
    # The render hook keys on result.new_artifacts, which was empty for .html
    # because the mime/renderable sets omitted it → the render never fired.
    class Ev:
        tool_name = "artifact"
        tool_use_id = "t1"
        result = {"content": [{"text": "game: https://dexamplecdn002.cloudfront.net/artifacts/snake_game.html"}]}
    r = ctx_after_tool_result(Ev()).get("result", {})
    assert r["mimes"] == ["text/html"]
    assert r["new_artifacts"] == ["https://dexamplecdn002.cloudfront.net/artifacts/snake_game.html"]



def test_session_end_flush_runs_off_the_event_loop(monkeypatch):
    """The blocking render flush must run on a WORKER thread so the event loop keeps
    draining progress. Inline, dispatch_render (~90s render-agent compose) froze
    main._progress_poller and starved the keepalive -- a dead gap after the answer,
    then a burst of render steps at the very end. Offloading with asyncio.to_thread
    yields the loop back so live render progress streams during compose.
    """
    import asyncio
    import threading
    import time as _time
    from hooks import actions

    main_thread = threading.get_ident()
    flush_thread: dict[str, int] = {}

    def _blocking_flush():
        flush_thread["id"] = threading.get_ident()
        _time.sleep(0.2)  # stand-in for a slow render-agent compose
        return []

    monkeypatch.setattr(actions, "flush_render_batch", _blocking_flush)

    provider = HarnessHookProvider(HookEngine([]))
    assert asyncio.iscoroutinefunction(provider._on_session_end), (
        "AfterInvocationEvent is fired via invoke_callbacks_async; the hook must be a "
        "coroutine so it can offload the blocking flush"
    )

    ticks = {"n": 0}

    async def _poller():  # stands in for main._progress_poller
        while True:
            await asyncio.sleep(0.02)
            ticks["n"] += 1

    async def _run():
        poll = asyncio.ensure_future(_poller())
        try:
            await provider._on_session_end(None)
        finally:
            poll.cancel()

    asyncio.run(_run())

    assert flush_thread.get("id") is not None, "flush must have run"
    assert flush_thread["id"] != main_thread, "flush must run off the event-loop thread"
    assert ticks["n"] >= 3, "the poller must keep ticking while the flush composes"
