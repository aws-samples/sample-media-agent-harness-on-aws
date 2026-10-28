"""``ingestion.workflows`` / ``reasons`` / ``catalog`` / ``outputs`` (Req 4.10, 4.11, 7.3, 7.15, 7.16, 11.2)."""
from __future__ import annotations

import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ingestion import catalog, outputs, reasons, workflows as W  # noqa: E402

SRC = "s3://ingest/projects/p1/video:abc/source.mp4"


# -------------------------------------------------------------------------------------- arguments
def test_first_run_args_are_fixed_and_carry_no_token_or_history_scope():
    assert W.shot_segmentation_args(SRC) == {"asset_s3uri": SRC}
    assert W.speech_diarization_args(SRC) == {"asset_s3uri": SRC}
    assert W.frame_capture_args(SRC) == {"asset_s3uri": SRC, "width": 960, "height": 540, "output_groups": [
        {"group_name": "frames", "format": "jpeg", "frame_interval_sec": 1.0}]}
    assert W.enrichment_args("s3://m2c/run/") == {"shot_segmentation_s3uri": "s3://m2c/run", "use_batch": False}
    assert W.enrichment_args("s3://m2c/run", "s3://m2c/t.json")["transcript_s3uri"] == "s3://m2c/t.json"
    assert W.conversation_analysis_args("s3://m2c/t.json") == {"transcript_s3uri": "s3://m2c/t.json"}
    for args in (W.shot_segmentation_args(SRC), W.enrichment_args("s3://m2c/run")):
        assert "history_scope" not in args and "idempotency_token" not in args


def test_rerun_token():
    tok = W.idempotency_token(3, "ab12")
    assert tok == "r3-ab12"
    assert W.shot_segmentation_args(SRC, tok)["idempotency_token"] == tok


def test_every_spec_names_real_tools():
    listed = {"shot-segmentation___segment_shots", "shot-segmentation___get_job_status",
              "speech-diarization___diarize_speech", "speech-diarization___get_job_status",
              "media-processing___process_media", "media-processing___get_job_status",
              "shot-metadata-enrichment___enrich_shot_metadata", "shot-metadata-enrichment___get_job_status",
              "conversation-analysis___analyze_conversation", "conversation-analysis___get_job_status",
              "bedrock-inference___run_inference", "bedrock-inference___get_job_status",
              # Entity-metadata workflows (Phase 1) — verified live against the staging M2C gateway target config
              # (docs/entity-metadata-workflows.md).
              "face-recognition___recognize_faces", "face-recognition___get_job_status",
              "program-structure-analysis___analyze_program_structure",
              "program-structure-analysis___get_job_status",
              "cast-resolution___resolve_cast", "cast-resolution___get_job_status"}
    for spec in W.WORKFLOWS.values():
        assert spec.submit_tool in listed and spec.status_tool in listed


def test_entity_metadata_specs_and_args():
    assert W.WORKFLOWS[W.FACE_RECOGNITION].labels == ("face-metadata",)
    assert W.WORKFLOWS[W.PROGRAM_STRUCTURE].labels == ("structure",)
    assert W.WORKFLOWS[W.CAST_RESOLUTION].labels == ("persons",)
    # face-recognition: only the source URI, no use_batch / history_scope.
    assert W.face_recognition_args(SRC) == {"asset_s3uri": SRC}
    assert "use_batch" not in W.face_recognition_args(SRC)
    # program-structure: the two required run prefixes; conversation + transcript only when supplied.
    assert W.program_structure_args("s3://m/shots/", "s3://m/enrich/") == {
        "shot_segmentation_s3uri": "s3://m/shots", "shot_metadata_s3uri": "s3://m/enrich"}
    full = W.program_structure_args("s3://m/shots", "s3://m/enrich", "s3://m/conv/", "s3://m/t.json")
    assert full["conversation_analysis_s3uri"] == "s3://m/conv" and full["transcript_s3uri"] == "s3://m/t.json"
    assert "use_batch" not in full
    # cast-resolution: face + shots + shot-metadata prefixes; speech-diar only when supplied.
    assert W.cast_resolution_args("s3://m/face/", "s3://m/shots/", "s3://m/enrich/") == {
        "face_recognition_s3uri": "s3://m/face", "shot_segmentation_s3uri": "s3://m/shots",
        "shot_metadata_s3uri": "s3://m/enrich"}
    withspk = W.cast_resolution_args("s3://m/face", "s3://m/shots", "s3://m/enrich", "s3://m/diar/")
    assert withspk["speech_diarization_s3uri"] == "s3://m/diar"
    # Rerun tokens thread through all three.
    for args in (W.face_recognition_args(SRC, "r2-ab"), W.program_structure_args("a", "b", token="r2-ab"),
                 W.cast_resolution_args("a", "b", "c", token="r2-ab")):
        assert args["idempotency_token"] == "r2-ab"


@pytest.mark.parametrize("text,match", [
    ("Source has no audio stream", True),
    ("ValueError: input does not contain an audio stream", True),
    ("Stream map '0:a:0' matches no streams.", True),
    ("Output file #0 does not contain any stream", True),
    ('{"errorMessage":"No VAD turns found after flattening \u2014 check VAD output","errorType":"RuntimeError"}', True),
    ('{"errorMessage":"No output_groups are compatible with source (no audio)","errorType":"ValueError"}', True),
    ("ThrottlingException: slow down", False),
    ("", False), (None, False),
])
def test_no_audio_stream(text, match):
    assert W.is_no_audio_stream(text) is match


@pytest.mark.parametrize("text,match", [
    ('{"errorMessage":"No high-quality embeddings found","errorType":"ValueError"}', True),
    ('{"errorMessage":"No sentences in micro-workflow/speech-diarization/6efe/dynamic/transcripts/transcript_0.json",'
     '"errorType":"RuntimeError"}', True),
    ('{"errorMessage":"No VAD turns found after flattening \u2014 check VAD output"}', True),
    # alpha.20261017a's wording (prod `Separate Audio Stems (audio_sfx).wav`, 2026-10-19)
    ("speech-diarization: NoSpeechDetected: no usable speech reached clustering \u2014 0 high-quality embeddings "
     "(low=0, skipped=1, iterators=1)", True),
    # An AUDIO asset with no decodable audio stream is a broken file, not "no speech": it must stay failed.
    ("Source has no audio stream", False),
    ("Stream map '0:a:0' matches no streams.", False),
    ('{"has_audio": false}', False),
    ("Lambda.Unknown: out of memory", False),
    ("", False), (None, False),
])
def test_no_speech(text, match):
    assert W.is_no_speech(text) is match
    # "No high-quality embeddings" alone is NOT a no-audio video (that video has speech-shaped audio).
    if text and "embeddings" in text:
        assert W.is_no_audio_stream(text) is False


# --------------------------------------------------------------------------------------- reasons
def test_reason_composition_redacts_then_truncates():
    akid = "AKIA" + "ABCDEFGHIJKLMNOP"
    msg = (f"bad key {akid} url https://b.s3/x?X-Amz-Credential=abc%2F&X-Amz-Signature=deadbeef "
           f"Authorization: Bearer abc.def.ghi aws_secret_access_key=s3cr3t session_token=tok "
           "eyJhbGciOi.eyJzdWIiOiJ4.sig")
    out = reasons.compose("indexing", msg)
    for secret in (akid, "abc%2F", "deadbeef", "abc.def.ghi", "s3cr3t", "=tok", "eyJhbGciOi"):
        assert secret not in out
    assert out.startswith("indexing: bad key [REDACTED]")
    assert "X-Amz-Signature=[REDACTED]" in out
    long = reasons.compose("document build", "x" * 5000)
    assert len(long) == reasons.MAX_REASON_CHARS


def test_reason_unknown_step_rejected():
    with pytest.raises(ValueError):
        reasons.compose("segmentation", "x")


# --------------------------------------------------------------------------------------- catalog
def test_catalog_required_and_missing():
    assert "hybrid-search-admin___delete_collection" in catalog.required_tools("audio")
    assert "shot-segmentation___segment_shots" not in catalog.required_tools("audio")
    have = set(catalog.required_tools("video")) - {"media-processing___process_media"}
    assert catalog.missing_tools(["video", "audio"], have) == ("media-processing___process_media",)
    assert catalog.missing_tools(["video"], catalog.required_tools("video")) == ()
    assert "bedrock-inference___get_model" in catalog.missing_tools(["image"], have)
    assert catalog.refusal(("a", "b")) == "the M2C catalog is missing a, b"
    with pytest.raises(ValueError):
        catalog.required_tools("document")


def test_catalog_entity_metadata_flag_gates_the_new_tools():
    # OFF (default): the entity-metadata tools are neither required nor reported missing.
    base = catalog.required_tools("video")
    assert "face-recognition___recognize_faces" not in base
    assert catalog.missing_tools(["video"], base) == ()
    # ON: the three entity-metadata workflows' tools become required for video (and only video).
    ent = catalog.required_tools("video", entity_metadata=True)
    for t in ("face-recognition___recognize_faces", "face-recognition___get_job_status",
              "program-structure-analysis___analyze_program_structure",
              "program-structure-analysis___get_job_status",
              "cast-resolution___resolve_cast", "cast-resolution___get_job_status"):
        assert t in ent
    assert catalog.required_tools("audio", entity_metadata=True) == catalog.required_tools("audio")  # video-only
    # An env missing the entity-metadata tools passes when OFF, refuses when ON.
    assert catalog.missing_tools(["video"], base, entity_metadata=False) == ()
    missing = catalog.missing_tools(["video"], base, entity_metadata=True)
    assert set(missing) == set(ent) - set(base) and missing[0].startswith("face-recognition")


# --------------------------------------------------------------------------------------- outputs
class S3:
    def __init__(self, objs):
        self.objs = objs

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objs:
            raise KeyError(Key)
        b = self.objs[(Bucket, Key)]
        return {"ContentLength": len(b), "Body": io.BytesIO(b)}

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objs:
            raise KeyError(Key)
        return {}


def test_output_by_label_and_reads():
    st = {"outputs": [{"label": "probe", "uri": "s3://m/p.json"}, {"label": "shots", "uri": "s3://m/s.jsonl"}]}
    assert outputs.output_uri(st, "shots", "shot-metadata-enrichment") == "s3://m/s.jsonl"
    with pytest.raises(outputs.OutputError) as e:
        outputs.output_uri(st, "shot-list", "shot-segmentation")
    assert (e.value.step, e.value.label) == ("shot-segmentation", "shot-list")
    s3 = S3({("m", "s.jsonl"): '{"a":1}\n{"b":"\u2028"}\n'.encode(), ("m", "p.json"): b'{"x":1}'})
    assert outputs.read_output(s3, "s3://m/s.jsonl", "e", "shots") == [{"a": 1}, {"b": "\u2028"}]
    assert outputs.read_output(s3, "s3://m/p.json", "e", "probe") == {"x": 1}
    with pytest.raises(outputs.OutputError, match="unreadable"):
        outputs.read_output(s3, "s3://m/gone.json", "e", "probe")
    assert outputs.readable(s3, "s3://m/p.json") and not outputs.readable(s3, "s3://m/gone.json")
    assert not outputs.readable(s3, "https://m/p.json")


def test_output_size_cap(monkeypatch):
    monkeypatch.setattr(outputs, "MAX_OUTPUT_BYTES", 4)
    with pytest.raises(outputs.OutputError, match="limit"):
        outputs.read_output(S3({("m", "a.json"): json.dumps({"k": 1}).encode()}), "s3://m/a.json", "e", "l")
