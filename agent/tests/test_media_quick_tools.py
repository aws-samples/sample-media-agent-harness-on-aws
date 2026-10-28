"""extract_frames / clip_video: the timeline's warm-sandbox Frame / Clip as agent tools (no cell re-downloading
ffmpeg and the source; staging 2026-10-19 a hand-written key-frame cell took 16 min on a stalled download)."""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tools import media_quick as mq  # noqa: E402


@pytest.fixture
def captured(monkeypatch):
    seen: dict = {}
    import media_actions as ma
    from jobs import context as jc

    monkeypatch.setattr(mq, "_resolve_source", lambda url: ({"url": "https://s3/presigned", "s3uri": "s3://b/k/v.mp4", "name": "v.mp4"}, None))
    monkeypatch.setattr(jc, "current_session_id", lambda: "sess-1")
    monkeypatch.setattr(jc, "trusted_project_id", lambda: "p" + "a" * 32)

    def fake_run(act, *, session_id, project_id):
        seen.update(act=act, session_id=session_id, project_id=project_id)
        return {"ok": True, "elapsedMs": 900, "outputs": [{"name": "f.jpg", "url": "https://cdn/f.jpg", "kind": "image", "key": "k"}]}

    monkeypatch.setattr(ma, "run_action", fake_run)
    return seen


def test_extract_frames_runs_one_warm_sandbox_action_with_every_time(captured):
    out = json.loads(mq.extract_frames(source_url="https://cdn/v.mp4", times=[4.5, 7.2, 11.1]))
    assert out["status"] == "done" and out["outputs"][0]["url"] == "https://cdn/f.jpg"
    act = captured["act"]
    assert act.op == "frame" and act.points_ms == [4500, 7200, 11100]
    assert captured["session_id"] == "sess-1" and captured["project_id"].startswith("p")


def test_clip_video_takes_every_range_and_has_no_length_cap(captured):
    out = json.loads(mq.clip_video(source_url="https://cdn/v.mp4", ranges=[[0, 1], [10, 600.5]]))
    assert out["status"] == "done"
    assert captured["act"].ranges_ms == [(0, 1000), (10000, 600500)]  # a 10-min clip is a stream copy: allowed


def test_lineage_each_output_is_made_from_the_source_over_its_time_range(captured, monkeypatch):
    import media_actions as ma
    from media import turn_outputs

    monkeypatch.setenv("INGESTION_CONFIG", json.dumps({"lineageEdges": True}))
    monkeypatch.setattr(ma, "run_action", lambda act, **kw: {"ok": True, "outputs": [
        {"name": "f.jpg", "url": "https://cdn/f.jpg", "kind": "image", "key": "artifacts/s/output/f.jpg",
         "s3uri": "s3://b/artifacts/s/output/f.jpg", "range": {"start": 7.19, "end": 7.19}}]})
    turn_outputs.open_turn()
    try:
        mq.extract_frames(source_url="https://cdn/v.mp4", times=[7.19])
        (e,) = turn_outputs.lineage()
        assert e["output"] == "s3://b/artifacts/s/output/f.jpg" and e["inputs"] == ["s3://b/k/v.mp4"]
        assert e["tool"] == "extract_frames" and e["ranges"] == [
            {"input": "s3://b/k/v.mp4", "start": 7.19, "end": 7.19, "unit": "s"}]
        monkeypatch.delenv("INGESTION_CONFIG")
        turn_outputs.open_turn()
        mq.extract_frames(source_url="https://cdn/v.mp4", times=[7.19])
        assert turn_outputs.lineage() == []  # flag off: exactly as before
    finally:
        turn_outputs.open_turn()


def test_bad_inputs_are_explained_not_run(captured):
    assert "times" in json.loads(mq.extract_frames(source_url="https://cdn/v.mp4", times=[]))["error"]
    assert "ranges" in json.loads(mq.clip_video(source_url="https://cdn/v.mp4", ranges=[[5, 2]]))["error"]
    assert "act" not in captured


def test_a_url_that_is_no_chat_or_project_file_is_refused(monkeypatch):
    from media import artifact_fetch, persist_media
    monkeypatch.setattr(persist_media, "validate_fetchable", lambda u: None)
    monkeypatch.setattr(artifact_fetch, "canonical_location", lambda u: None)
    out = json.loads(mq.extract_frames(source_url="https://example.com/x.mp4", times=[1.0]))
    assert "file of this chat or project" in out["error"]


def test_both_agents_and_the_skill_offer_the_tools():
    src = os.path.join(os.path.dirname(__file__), "..", "src")
    for f in ("main.py", "task_agent.py"):
        text = open(os.path.join(src, f)).read()
        assert '"extract_frames": extract_frames' in text and '"clip_video": clip_video' in text
    skill = open(os.path.join(os.path.dirname(__file__), "..", "skills", "video-processing", "SKILL.md")).read()
    assert "`extract_frames` tool ONCE" in skill and "`clip_video` tool ONCE" in skill
