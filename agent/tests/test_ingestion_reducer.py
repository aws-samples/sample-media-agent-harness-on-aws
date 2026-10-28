"""``ingestion.reducer`` — the rule table of design Component 8, without I/O (Properties 11–14)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ingestion import reducer as R  # noqa: E402
from ingestion import workflows as W  # noqa: E402

NOW = 10_000.0


def ok(job, **kw):
    return {"status": "SUCCEEDED", "jobId": job, "runSeq": 1, "inputs": [], "submittedAt": 0, **kw}


def running(job, **kw):
    return {"status": "RUNNING", "jobId": job, "runSeq": 1, "inputs": [], "submittedAt": NOW - 10, **kw}


SPEECH = {"facts": {"hasSpeech": True}, "outputs": {"transcript": "s3://m/t.json"}}


def wave1(diar=None):
    return {W.SHOT_SEGMENTATION: ok("s", outputPrefix="s3://m/shots-run"),
            W.SPEECH_DIARIZATION: diar or ok("d", **SPEECH), W.FRAME_CAPTURE: ok("f")}


def test_start_submits_wave1_together():
    assert R.plan_video({}, NOW, 1) == R.Plan(submit=R.VIDEO_WAVE1)


def test_wave1_running_waits():
    steps = {s: running(s) for s in R.VIDEO_WAVE1}
    assert R.plan_video(steps, NOW, 1) == R.Plan()


def test_speech_submits_enrichment_with_transcript_and_conversation_analysis():
    plan = R.plan_video(wave1(), NOW, 1)
    assert plan.submit == (W.ENRICHMENT, W.CONVERSATION_ANALYSIS)
    args = R.submit_arguments(W.ENRICHMENT, wave1(), "s3://src", None)
    assert args == {"shot_segmentation_s3uri": "s3://m/shots-run", "use_batch": False,
                    "transcript_s3uri": "s3://m/t.json"}
    assert R.submit_arguments(W.CONVERSATION_ANALYSIS, wave1(), "s3://src", None) == {
        "transcript_s3uri": "s3://m/t.json"}


def test_no_speech_and_no_audio_submit_enrichment_only_without_transcript():
    silent = ok("d", facts={"hasSpeech": False}, outputs={"transcript": "s3://m/t.json"})
    no_audio = {"status": R.NO_AUDIO, "jobId": "d", "runSeq": 1, "inputs": [], "error": "no audio stream"}
    for diar in (silent, no_audio):
        steps = wave1(diar)
        assert R.plan_video(steps, NOW, 1).submit == (W.ENRICHMENT,)
        assert "transcript_s3uri" not in R.submit_arguments(W.ENRICHMENT, steps, "s3://src", None)


def test_build_when_wave2_done():
    steps = {**wave1(), W.ENRICHMENT: ok("e", inputs=["s", "d"]), W.CONVERSATION_ANALYSIS: ok("c", inputs=["d"])}
    assert R.plan_video(steps, NOW, 1) == R.Plan(build=True)


def test_rerun_upstream_invalidates_consumers():
    steps = {**wave1(), W.ENRICHMENT: ok("e", inputs=["s-old", "d"]), W.CONVERSATION_ANALYSIS: ok("c", inputs=["d"])}
    assert R.plan_video(steps, NOW, 2).submit == (W.ENRICHMENT,)


def test_failure_in_this_run_fails_with_step_and_error():
    steps = wave1(diar={"status": "FAILED", "jobId": "d", "runSeq": 1, "inputs": [], "error": "boom"})
    assert R.plan_video(steps, NOW, 1).fail == (W.SPEECH_DIARIZATION, "boom")


# alpha.20261014a reports a silent source only as an opaque media-processing failure (staging project
# p49b9cc6, 2026-10-14): the verdict comes from frame capture's probe instead.
OPAQUE = {"status": "FAILED", "jobId": "d", "runSeq": 1, "inputs": [],
          "error": "media-processing run c98934e4 ended FAILED: see the run prefix"}
SILENT_PROBE = {"facts": {"technical": {"video": {"codec": "h264", "width": 512}, "durationSec": 51.7}}}


def test_probe_with_no_audio_stream_turns_a_diarization_failure_into_no_audio():
    steps = {**wave1(diar=OPAQUE), W.FRAME_CAPTURE: ok("f", **SILENT_PROBE)}
    plan = R.plan_video(steps, NOW, 1)
    assert plan.fail is None and plan.submit == (W.ENRICHMENT,)
    assert "transcript_s3uri" not in R.submit_arguments(W.ENRICHMENT, steps, "s3://src", None)
    # runSeq read back from DynamoDB is a string
    assert R.plan_video({**steps, W.SPEECH_DIARIZATION: {**OPAQUE, "runSeq": "1"}}, NOW, 1).fail is None


def test_diarization_failure_waits_for_the_probe():
    steps = {**wave1(diar=OPAQUE), W.FRAME_CAPTURE: running("f")}
    assert R.plan_video(steps, NOW, 1) == R.Plan()


def test_diarization_failure_stands_when_the_probe_shows_audio_or_nothing():
    with_audio = {"facts": {"technical": {"video": {"codec": "h264"}, "audio": {"codec": "aac", "channels": 2}}}}
    for fc in (ok("f", **with_audio), ok("f"), {"status": "FAILED", "jobId": "f", "runSeq": 1, "inputs": [],
                                                   "error": "probe failed", **SILENT_PROBE}):
        steps = {**wave1(diar=OPAQUE), W.FRAME_CAPTURE: fc}
        assert R.plan_video(steps, NOW, 1).fail is not None, fc


def test_an_earlier_runs_diarization_failure_is_still_rerun_on_a_silent_probe():
    steps = {**wave1(diar=OPAQUE), W.FRAME_CAPTURE: ok("f", **SILENT_PROBE)}
    assert R.plan_video(steps, NOW, 2).submit == (W.SPEECH_DIARIZATION,)


def test_failure_from_an_earlier_run_is_rerun_with_a_token():
    failed = {"status": "FAILED", "jobId": "d", "runSeq": 1, "inputs": [], "error": "boom"}
    steps = wave1(diar=failed)
    assert R.plan_video(steps, NOW, 2) == R.Plan(submit=(W.SPEECH_DIARIZATION,))
    tok = R.token_for(W.SPEECH_DIARIZATION, failed, 2, "video:x")
    assert tok and tok.startswith("r2-") and tok == R.token_for(W.SPEECH_DIARIZATION, failed, 2, "video:x")
    assert tok != R.token_for(W.SPEECH_DIARIZATION, failed, 3, "video:x")
    assert R.token_for(W.SPEECH_DIARIZATION, {}, 1, "video:x") is None


def test_timeout_and_call_failures():
    late = running("s", submittedAt=NOW - 6 * 3600 - 1)
    assert R.plan_video({W.SHOT_SEGMENTATION: late}, NOW, 1).fail == (W.SHOT_SEGMENTATION, "timed out after 6 h")
    flaky = running("s", callFailures=3, error="Transient: 503")
    assert R.plan_video({W.SHOT_SEGMENTATION: flaky}, NOW, 1).fail == (W.SHOT_SEGMENTATION, "Transient: 503")


def test_submitting_record_is_resubmitted_after_its_gap():
    rec = {"status": R.SUBMITTING, "runSeq": 1, "inputs": [], "notBefore": NOW + 5, "callFailures": 1}
    plan = R.plan_video({W.SHOT_SEGMENTATION: rec}, NOW, 1)
    assert W.SHOT_SEGMENTATION in plan.submit
    assert not R.submit_due(rec, NOW) and R.submit_due(rec, NOW + 5)


def test_audio_rules():
    assert R.plan_audio({}, NOW, 1) == R.Plan(submit=(W.SPEECH_DIARIZATION,))
    silent = {W.SPEECH_DIARIZATION: ok("d", facts={"hasSpeech": False})}
    # Audio with no speech is kept but NOT indexed -- never a failure (Req 12.7, 2026-10).
    plan = R.plan_audio(silent, NOW, 1)
    assert plan.fail is None
    assert plan.skip == (W.SPEECH_DIARIZATION, R.NO_SPEECH_REASON) == (W.SPEECH_DIARIZATION, "no_speech")
    # A diarization that failed for lack of speech is recorded NO_AUDIO by the tick: same outcome.
    no_vad = {W.SPEECH_DIARIZATION: {"status": R.NO_AUDIO, "runSeq": 1, "inputs": [],
                                     "error": "No VAD turns found after flattening"}}
    assert R.plan_audio(no_vad, NOW, 1) == R.Plan(skip=(W.SPEECH_DIARIZATION, "no_speech"))
    # A genuine diarization failure on audio still fails the asset.
    broke = {W.SPEECH_DIARIZATION: {"status": "FAILED", "runSeq": 1, "inputs": [], "error": "boom"}}
    assert R.plan_audio(broke, NOW, 1).fail == (W.SPEECH_DIARIZATION, "boom")
    talk = {W.SPEECH_DIARIZATION: ok("d", **SPEECH)}
    assert R.plan_audio(talk, NOW, 1) == R.Plan(submit=(W.CONVERSATION_ANALYSIS,))
    done = {**talk, W.CONVERSATION_ANALYSIS: ok("c", inputs=["d"])}
    assert R.plan_audio(done, NOW, 1) == R.Plan(build=True)


def test_wait_seconds():
    assert R.wait_seconds({"a": {"submittedAt": NOW - 60}}, NOW) == 15
    assert R.wait_seconds({"a": {"submittedAt": NOW - 600}}, NOW) == 30
    assert R.wait_seconds({}, NOW, NOW - 10) == 15


def test_document_rules():
    # Nothing extracted yet → extract in the tick.
    assert R.plan_document({}, NOW, 1) == R.Plan(extract=True)
    # Text found → build.
    done = {W.DOCUMENT_EXTRACT: {"status": "SUCCEEDED", "runSeq": 1, "hasText": True, "key": "k"}}
    assert R.plan_document(done, NOW, 1) == R.Plan(build=True)
    # No text layer → kept but NOT indexed (stored + indexSkippedReason), never a failure.
    none = {W.DOCUMENT_EXTRACT: {"status": "SUCCEEDED", "runSeq": 1, "hasText": False,
                                 "skipReason": R.NO_TEXT_REASON}}
    plan = R.plan_document(none, NOW, 1)
    assert plan.fail is None and plan.skip == ("document text", "no_text_layer")
    # Too large to extract → its own reason, still a skip.
    big = {W.DOCUMENT_EXTRACT: {"status": "SUCCEEDED", "runSeq": 1, "hasText": False,
                                "skipReason": R.DOC_TOO_LARGE_REASON}}
    assert R.plan_document(big, NOW, 1).skip == ("document text", "document_too_large")
    # An extract that failed in THIS run fails the asset (infra); an earlier run's failure re-extracts.
    failed = {W.DOCUMENT_EXTRACT: {"status": "FAILED", "runSeq": 1, "error": "AccessDenied"}}
    assert R.plan_document(failed, NOW, 1).fail == (W.DOCUMENT_EXTRACT, "AccessDenied")
    assert R.plan_document({W.DOCUMENT_EXTRACT: {**failed[W.DOCUMENT_EXTRACT], "runSeq": 1}}, NOW, 2) == \
        R.Plan(extract=True)


def test_failure_step_for_document():
    assert R.failure_step_for("document", "analyzing", {}) == W.DOCUMENT_EXTRACT
    done = {W.DOCUMENT_EXTRACT: {"status": "SUCCEEDED"}}
    assert R.failure_step_for("document", "analyzing", done) == "document build"


# --------------------------------------------------------------- entity-metadata workflows (Phase 1, flag-gated)
ENTITY_CFG = R.Config(entity_metadata=True)


def wave2_ready(speech=True):
    """Wave 1 done + wave 2 done (enrichment, conversation if speech, face-recognition), with the output
    prefixes a wave-3 submission reads."""
    diar = ok("d", outputPrefix="s3://m/diar-run", **SPEECH) if speech else \
        ok("d", outputPrefix="s3://m/diar-run", facts={"hasSpeech": False}, outputs={})
    steps = {W.SHOT_SEGMENTATION: ok("s", outputPrefix="s3://m/shots-run"),
             W.SPEECH_DIARIZATION: diar, W.FRAME_CAPTURE: ok("f"),
             W.ENRICHMENT: ok("e", outputPrefix="s3://m/enrich-run",
                              inputs=["s", "d"] if speech else ["s"]),
             W.FACE_RECOGNITION: ok("frec", outputPrefix="s3://m/face-run", inputs=[])}
    if speech:
        steps[W.CONVERSATION_ANALYSIS] = ok("c", outputPrefix="s3://m/conv-run", inputs=["d"])
    return steps


def test_flag_off_is_unchanged_video_pipeline():
    # Start: wave 2 is enrichment (+conversation), NOT face-recognition.
    assert R.plan_video(wave1(), NOW, 1).submit == (W.ENRICHMENT, W.CONVERSATION_ANALYSIS)
    assert W.FACE_RECOGNITION not in R.plan_video(wave1(), NOW, 1).submit
    # A seeded face step is ignored, and the build happens after wave 2 (no wave 3 required).
    assert R.plan_video(wave2_ready(), NOW, 1) == R.Plan(build=True)


def test_entity_face_recognition_submits_in_wave1():
    # Change: face-recognition has no upstream dep, so with the flag ON it is in the FIRST submit batch,
    # alongside shot-segmentation / speech-diarization / frame-capture — not deferred until wave 1 finishes.
    plan = R.plan_video({}, NOW, 1, ENTITY_CFG)
    assert plan.submit == R.VIDEO_WAVE1 + (W.FACE_RECOGNITION,)
    assert plan.fail is None and plan.build is False
    # Flag OFF: the first batch is wave 1 only, face is NOT submitted.
    assert R.plan_video({}, NOW, 1).submit == R.VIDEO_WAVE1
    assert W.FACE_RECOGNITION not in R.plan_video({}, NOW, 1).submit
    # Even while wave 1 is still running, face rides along (submitted once, then tracked as in-flight).
    running_w1 = {s: running(s) for s in R.VIDEO_WAVE1}
    assert R.plan_video(running_w1, NOW, 1, ENTITY_CFG).submit == (W.FACE_RECOGNITION,)


def test_entity_face_in_wave1_is_best_effort_and_never_fatal():
    # face-recognition FAILS this run while wave 1 is still in flight: it is skipped (not in the fatal
    # wave-1 gate), the asset is NOT failed, and wave 1 is still "not done" purely on its own three steps.
    steps = {W.SHOT_SEGMENTATION: running("s"), W.SPEECH_DIARIZATION: running("d"),
             W.FRAME_CAPTURE: running("f"), W.FACE_RECOGNITION: failed("frec", inputs=[])}
    plan = R.plan_video(steps, NOW, 1, ENTITY_CFG)
    assert plan.fail is None and plan.build is False and plan.submit == ()


def test_entity_face_recognition_catches_up_alongside_enrichment():
    # If wave 1 completed before face had been submitted (synthetic state: no face record yet), face still
    # submits — now in the enrichment batch — so it is never dropped. The build still gates on it later.
    plan = R.plan_video(wave1(), NOW, 1, ENTITY_CFG)
    assert plan.submit == (W.ENRICHMENT, W.CONVERSATION_ANALYSIS, W.FACE_RECOGNITION)
    assert R.inputs_for(W.FACE_RECOGNITION, wave1(), True) == ()
    assert R.submit_arguments(W.FACE_RECOGNITION, wave1(), "s3://src", None) == {"asset_s3uri": "s3://src"}


def test_entity_wave3_submits_program_structure_and_cast_after_wave2():
    steps = wave2_ready()
    plan = R.plan_video(steps, NOW, 1, ENTITY_CFG)
    assert plan.fail is None and plan.build is False
    assert plan.submit == (W.PROGRAM_STRUCTURE, W.CAST_RESOLUTION)
    # Inputs (reuse keys): PSA consumes shots+enrichment (+conversation+diar when speech);
    # cast consumes face+shots+enrichment (+diar when speech).
    assert R.inputs_for(W.PROGRAM_STRUCTURE, steps, True) == ("s", "e", "c", "d")
    assert R.inputs_for(W.CAST_RESOLUTION, steps, True) == ("frec", "s", "e", "d")
    ps = R.submit_arguments(W.PROGRAM_STRUCTURE, steps, "s3://src", None)
    assert ps == {"shot_segmentation_s3uri": "s3://m/shots-run", "shot_metadata_s3uri": "s3://m/enrich-run",
                  "conversation_analysis_s3uri": "s3://m/conv-run", "transcript_s3uri": "s3://m/t.json"}
    cr = R.submit_arguments(W.CAST_RESOLUTION, steps, "s3://src", None)
    assert cr == {"face_recognition_s3uri": "s3://m/face-run", "shot_segmentation_s3uri": "s3://m/shots-run",
                  "shot_metadata_s3uri": "s3://m/enrich-run", "speech_diarization_s3uri": "s3://m/diar-run"}


def test_entity_build_only_after_wave3_done():
    steps = {**wave2_ready(),
             W.PROGRAM_STRUCTURE: ok("ps", inputs=["s", "e", "c", "d"]),
             W.CAST_RESOLUTION: ok("cr", inputs=["frec", "s", "e", "d"])}
    assert R.plan_video(steps, NOW, 1, ENTITY_CFG) == R.Plan(build=True)


def test_cast_and_program_gate_behind_wave2():
    # face-recognition still running → wave 2 not done → no wave-3 submission, no build.
    running_face = {**wave2_ready(), W.FACE_RECOGNITION: running("frec")}
    assert R.plan_video(running_face, NOW, 1, ENTITY_CFG) == R.Plan()
    # enrichment still running → same (cast/program depend on the enrichment run).
    running_enrich = {**wave2_ready(), W.ENRICHMENT: running("e", inputs=["s", "d"])}
    assert R.plan_video(running_enrich, NOW, 1, ENTITY_CFG) == R.Plan()


def test_entity_silent_video_omits_speech_inputs():
    steps = wave2_ready(speech=False)
    plan = R.plan_video(steps, NOW, 1, ENTITY_CFG)
    assert plan.submit == (W.PROGRAM_STRUCTURE, W.CAST_RESOLUTION)
    assert R.inputs_for(W.PROGRAM_STRUCTURE, steps, False) == ("s", "e")
    assert R.inputs_for(W.CAST_RESOLUTION, steps, False) == ("frec", "s", "e")
    ps = R.submit_arguments(W.PROGRAM_STRUCTURE, steps, "s3://src", None)
    assert ps == {"shot_segmentation_s3uri": "s3://m/shots-run", "shot_metadata_s3uri": "s3://m/enrich-run"}
    assert "transcript_s3uri" not in ps and "conversation_analysis_s3uri" not in ps
    cr = R.submit_arguments(W.CAST_RESOLUTION, steps, "s3://src", None)
    assert "speech_diarization_s3uri" not in cr


# ------------------------------------------------------- entity-metadata best-effort (non-fatal), Change 1 + 2
def failed(job, seq=1, **kw):
    return {"status": "FAILED", "jobId": job, "runSeq": seq, "inputs": [], "error": "boom", **kw}


def test_entity_step_failure_is_never_fatal_and_does_not_hold_the_build():
    # A load-bearing wave is all done; every entity-metadata step terminally FAILED this run → build, not fail.
    steps = {**wave2_ready(), W.FACE_RECOGNITION: failed("frec", inputs=[]),
             W.PROGRAM_STRUCTURE: failed("ps", inputs=["s", "e", "c", "d"]),
             W.CAST_RESOLUTION: failed("cr", inputs=["frec", "s", "e", "d"])}
    plan = R.plan_video(steps, NOW, 1, ENTITY_CFG)
    assert plan.fail is None and plan.build is True and plan.submit == ()


def test_program_structure_failure_still_builds_with_the_cast_that_succeeded():
    steps = {**wave2_ready(),
             W.PROGRAM_STRUCTURE: failed("ps", inputs=["s", "e", "c", "d"]),
             W.CAST_RESOLUTION: ok("cr", inputs=["frec", "s", "e", "d"])}
    plan = R.plan_video(steps, NOW, 1, ENTITY_CFG)
    assert plan.fail is None and plan.build is True


def test_face_failure_blocks_cast_rather_than_failing_it():
    # face-recognition failed this run → cast-resolution is BLOCKED (its identities can't resolve), not
    # submitted and not an independent failure; program-structure (independent) still submits.
    steps = {**wave2_ready(), W.FACE_RECOGNITION: failed("frec", inputs=[])}
    plan = R.plan_video(steps, NOW, 1, ENTITY_CFG)
    assert plan.fail is None
    assert plan.submit == (W.PROGRAM_STRUCTURE,)
    assert W.CAST_RESOLUTION not in plan.submit
    assert [s for s, _ in plan.blocked] == [W.CAST_RESOLUTION]
    assert W.FACE_RECOGNITION in plan.blocked[0][1]  # reason names the failed upstream step


def test_face_failure_then_program_done_builds_with_cast_blocked():
    steps = {**wave2_ready(), W.FACE_RECOGNITION: failed("frec", inputs=[]),
             W.PROGRAM_STRUCTURE: ok("ps", inputs=["s", "e", "c", "d"]),
             W.CAST_RESOLUTION: {"status": R.SKIPPED, "runSeq": 1, "inputs": ["frec", "s", "e", "d"]}}
    plan = R.plan_video(steps, NOW, 1, ENTITY_CFG)
    assert plan.build is True and plan.fail is None and plan.submit == ()


def test_an_already_skipped_cast_is_still_reported_blocked_but_not_resubmitted():
    steps = {**wave2_ready(), W.FACE_RECOGNITION: failed("frec", inputs=[]),
             W.CAST_RESOLUTION: {"status": R.SKIPPED, "runSeq": 1, "inputs": ["frec", "s", "e", "d"]}}
    plan = R.plan_video(steps, NOW, 1, ENTITY_CFG)
    assert W.CAST_RESOLUTION not in plan.submit
    assert [s for s, _ in plan.blocked] == [W.CAST_RESOLUTION]
    assert plan.submit == (W.PROGRAM_STRUCTURE,)


def test_an_entity_failure_from_an_earlier_run_is_rerun():
    # Re-add (run 2): face FAILED in run 1 → not current → resubmitted; program SUCCEEDED in run 1 with
    # unchanged inputs → reused (NOT resubmitted). Only the failed entity-metadata step reruns.
    steps = {**wave2_ready(),
             W.FACE_RECOGNITION: failed("frec", seq=1, inputs=[]),
             W.PROGRAM_STRUCTURE: ok("ps", inputs=["s", "e", "c", "d"])}
    plan = R.plan_video(steps, NOW, 2, ENTITY_CFG)
    assert plan.fail is None
    assert plan.submit == (W.FACE_RECOGNITION,)
    assert W.PROGRAM_STRUCTURE not in plan.submit


def test_entity_timeout_or_call_failures_are_skipped_not_fatal():
    late = running("frec", inputs=[], submittedAt=NOW - 6 * 3600 - 1)
    steps = {**wave2_ready(), W.FACE_RECOGNITION: late,
             W.PROGRAM_STRUCTURE: ok("ps", inputs=["s", "e", "c", "d"])}
    plan = R.plan_video(steps, NOW, 1, ENTITY_CFG)
    assert plan.fail is None  # a timed-out entity-metadata step does not fail the asset
    assert [s for s, _ in plan.blocked] == [W.CAST_RESOLUTION]  # face unusable → cast blocked


def test_flag_off_failure_step_attribution_ignores_entity_steps():
    # Change 2: with the flag OFF the entity-metadata steps never exist, so an unexpected internal error on an
    # otherwise-complete video must fall back to "document build", NOT the never-run "face-recognition".
    assert R.VIDEO_STEPS == R.VIDEO_WAVE1 + (W.ENRICHMENT, W.CONVERSATION_ANALYSIS)
    assert W.FACE_RECOGNITION not in R.VIDEO_STEPS
    complete = {W.SHOT_SEGMENTATION: ok("s"), W.SPEECH_DIARIZATION: ok("d", **SPEECH),
                W.FRAME_CAPTURE: ok("f"), W.ENRICHMENT: ok("e"), W.CONVERSATION_ANALYSIS: ok("c")}
    assert R.failure_step_for("video", "analyzing", complete) == "document build"
    # An incomplete video still attributes to the first missing load-bearing step.
    assert R.failure_step_for("video", "analyzing", {}) == W.SHOT_SEGMENTATION

