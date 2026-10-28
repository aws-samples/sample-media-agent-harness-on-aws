"""Preanalyzed step-seeding → reducer build with NO submission (design Part 2, the load-bearing test)."""
import pytest

from ingestion import reducer
from ingestion import workflows as W
from preload import analysis
from preload.analysis import PreanalyzedError, build_video_steps, verify_builds

AID = "video:11111111-1111-4111-8111-111111111111"
B = "s3://bucket"

SHOT_LIST = {"actual_duration_sec": 12.5, "shots": [{"shot_id": 0, "start_time": 0, "end_time": 12.5,
                                                     "start_frame": 0, "end_frame": 300}]}
TRANSCRIPT_SPEECH = {"audio_duration_sec": 12.5,
                     "sentences": [{"text": "hello there", "start": 0, "end": 2, "speaker_id": "a"}]}
TRANSCRIPT_SILENT = {"audio_duration_sec": 12.5, "sentences": []}


def _uris(include_analysis=True, include_transcript=True):
    u = {"shot-list": f"{B}/p/a/analysis/shot-list/shot-list.json",
         "frames": f"{B}/p/a/analysis/frames",
         "enrichment": f"{B}/p/a/analysis/enrichment/shots.jsonl"}
    if include_transcript:
        u["transcript"] = f"{B}/p/a/analysis/transcript/t.json"
    if include_analysis:
        u["analysis"] = f"{B}/p/a/analysis/analysis/a.json"
    return u


def test_speech_video_builds_with_no_submission():
    steps = build_video_steps(AID, _uris(), {"shot-list": SHOT_LIST, "transcript": TRANSCRIPT_SPEECH})
    # Every analysis step is present and SUCCEEDED.
    for step in (W.SHOT_SEGMENTATION, W.SPEECH_DIARIZATION, W.FRAME_CAPTURE, W.ENRICHMENT,
                 W.CONVERSATION_ANALYSIS):
        assert steps[step]["status"] == reducer.SUCCEEDED
    # inputs wired exactly as reducer.inputs_for expects.
    shot_job = steps[W.SHOT_SEGMENTATION]["jobId"]
    diar_job = steps[W.SPEECH_DIARIZATION]["jobId"]
    assert tuple(steps[W.ENRICHMENT]["inputs"]) == (shot_job, diar_job)
    assert tuple(steps[W.CONVERSATION_ANALYSIS]["inputs"]) == (diar_job,)
    assert steps[W.SHOT_SEGMENTATION]["facts"]["durationMs"] == 12500
    assert steps[W.SPEECH_DIARIZATION]["facts"]["hasSpeech"] is True

    plan = reducer.plan_video(steps, now=0.0, run_seq=1)
    assert plan.build is True
    assert plan.submit == ()
    assert plan.fail is None
    verify_builds(steps)  # must not raise


def test_silent_video_builds_without_conversation_step():
    steps = build_video_steps(AID, _uris(include_analysis=False), {"shot-list": SHOT_LIST,
                                                                    "transcript": TRANSCRIPT_SILENT})
    assert steps[W.SPEECH_DIARIZATION]["status"] in (reducer.SUCCEEDED, reducer.NO_AUDIO)
    assert reducer.has_speech(steps) is False
    assert W.CONVERSATION_ANALYSIS not in steps          # no speech → conversation is not part of the plan
    assert tuple(steps[W.ENRICHMENT]["inputs"]) == (steps[W.SHOT_SEGMENTATION]["jobId"],)
    plan = reducer.plan_video(steps, now=0.0, run_seq=1)
    assert plan.build is True and plan.submit == ()
    verify_builds(steps)


def test_no_transcript_means_silent():
    steps = build_video_steps(AID, _uris(include_analysis=False, include_transcript=False),
                              {"shot-list": SHOT_LIST})
    assert steps[W.SPEECH_DIARIZATION]["status"] == reducer.NO_AUDIO
    plan = reducer.plan_video(steps, now=0.0, run_seq=1)
    assert plan.build is True and plan.submit == ()


def test_missing_duration_fails_cleanly():
    with pytest.raises(PreanalyzedError):
        build_video_steps(AID, _uris(), {"shot-list": {"shots": []}, "transcript": TRANSCRIPT_SPEECH})


def test_speech_without_conversation_output_fails_cleanly():
    with pytest.raises(PreanalyzedError):
        build_video_steps(AID, _uris(include_analysis=False),
                          {"shot-list": SHOT_LIST, "transcript": TRANSCRIPT_SPEECH})


def test_missing_required_output_fails_cleanly():
    u = _uris()
    del u["enrichment"]
    with pytest.raises(PreanalyzedError):
        build_video_steps(AID, u, {"shot-list": SHOT_LIST, "transcript": TRANSCRIPT_SPEECH})


def test_stable_job_ids_across_rebuild():
    a = build_video_steps(AID, _uris(), {"shot-list": SHOT_LIST, "transcript": TRANSCRIPT_SPEECH})
    b = build_video_steps(AID, _uris(), {"shot-list": SHOT_LIST, "transcript": TRANSCRIPT_SPEECH})
    assert a[W.SHOT_SEGMENTATION]["jobId"] == b[W.SHOT_SEGMENTATION]["jobId"]
