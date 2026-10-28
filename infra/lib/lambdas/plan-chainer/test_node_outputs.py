"""A completed node records the media it made on its job-registry row (`outputs`), for any node type, and every
dispatched node what it was HANDED (`inputRefs`) — both only with ``projects.lineageEdges`` (``LINEAGE_EDGES``).

Ported from the chat-first branch's chainer change (outputs) and extended for lineage edges: the Generated_Media
registrar turns a node's handed inputs and its outputs into per-hop edges.

The chat-first progress reply shows each node's output on its tile and, when the plan's last step made a film,
becomes that film. Gateway jobs carried their files (`artifactUrls`), but a node whose work ran INSIDE its own
turn — an ffmpeg assemble, a code-interpreter restyle — registers no job, so its output existed only in the
node's stored result and the card could neither paint the tile nor find the film (Round 5, plan
oilpaint-slideshow). The chainer sees every node completion on the plan-state stream, whoever wrote it, so it
records them there, once.
"""
from __future__ import annotations

import importlib.util
import io
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUCKET = "skills-bkt"
SID = "6bdef689-84cf-44da-a3e6-773d0c39918e"


def _load():
    for name in ("JOB_REGISTRY_TABLE", "PLAN_STATE_TABLE", "GATEWAY_REGISTRY_TABLE",
                 "SKILLS_S3_BUCKET", "TASK_AGENT_RUNTIME_ARN", "PLAN_RESULT_QUEUE_URL"):
        os.environ.setdefault(name, "t")
    spec = importlib.util.spec_from_file_location("_chainer_node_outputs", HERE / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_chainer_node_outputs"] = mod
    spec.loader.exec_module(mod)
    return mod


pc = _load()


class _CCF(Exception):
    pass


class FakeJobs:
    def __init__(self, rows):
        self.meta = type("M", (), {"client": type("C", (), {"exceptions": type(
            "E", (), {"ConditionalCheckFailedException": _CCF})})})
        self.rows = rows
        self.updates = []

    def get_item(self, Key):
        row = self.rows.get(Key["jobId"])
        return {"Item": row} if row is not None else {}

    def update_item(self, Key, UpdateExpression, ConditionExpression, ExpressionAttributeValues, **kw):
        row = self.rows.get(Key["jobId"])
        if ":r" in ExpressionAttributeValues:  # inputRefs: overwritten, needs the row
            if row is None:
                raise _CCF()
            row["inputRefs"] = ExpressionAttributeValues[":r"]
            self.updates.append(("inputRefs", Key["jobId"]))
            return {}
        if row is None or "outputs" in row:
            raise _CCF()
        row["outputs"] = ExpressionAttributeValues[":o"]
        self.updates.append(Key["jobId"])
        return {}


class FakeS3:
    def __init__(self, objects):
        self.objects = objects
        self.reads = 0

    def get_object(self, Bucket, Key, **kw):
        self.reads += 1
        return {"Body": io.BytesIO(self.objects[Key].encode("utf-8"))}


def _wire(monkeypatch, rows, objects):
    jobs, s3 = FakeJobs(rows), FakeS3(objects)
    monkeypatch.setattr(pc, "LINEAGE_EDGES", True, raising=False)
    monkeypatch.setattr(pc, "_job_table", jobs, raising=False)
    monkeypatch.setattr(pc, "_s3", s3, raising=False)
    monkeypatch.setattr(pc, "SKILLS_S3_BUCKET", BUCKET, raising=False)
    monkeypatch.setattr(pc, "ARTIFACTS_DOMAIN", "", raising=False)
    return jobs, s3


FRAME = f"artifacts/{SID}/output/frame_s3.png"
OIL = f"artifacts/external/2c4ce7ee46f1a379abc35efd.png"
FILM = f"projects/p1/sessions/{SID}/output/slideshow.mp4"
SOURCE = "projects/p1/0b7a3c1e-4d2f-4a8e-9a51-9f7f2a7e2b10/meridian_3min_720p.mp4"


def test_an_in_node_ffmpeg_assemble_records_its_film_not_its_inputs(monkeypatch):
    rows = {
        "P#oil_s3": {"jobId": "P#oil_s3", "dependsOn": [], "outputs": [
            {"uri": f"s3://{BUCKET}/{OIL}", "key": OIL, "kind": "image"}]},
        "P#assemble": {"jobId": "P#assemble", "dependsOn": ["oil_s3"]},
    }
    prose = (f"Joined the paintings (s3://{BUCKET}/{OIL}) from the source s3://{BUCKET}/{SOURCE} with 0.5 s "
             f"crossfades via ffmpeg. Output: s3://{BUCKET}/{FILM} (6.0 s, 1920x1080). Log: "
             f"s3://{BUCKET}/{SID}/output/ffmpeg.log")
    jobs, _ = _wire(monkeypatch, rows, {"plans/s/P/assemble/output": prose})
    pc._record_node_outputs("P", "assemble", "plans/s/P/assemble/output")
    assert rows["P#assemble"]["outputs"] == [{"uri": f"s3://{BUCKET}/{FILM}", "key": FILM, "kind": "video"}]


def test_a_gateway_result_json_records_each_media_file_with_its_kind(monkeypatch):
    rows = {"P#frame_s3": {"jobId": "P#frame_s3", "dependsOn": []}}
    result = ('{"status": "SUCCEEDED", "outputs": [{"uri": "s3://%s/%s"}, {"uri": "s3://%s/%s"}],'
              ' "meta": "s3://%s/artifacts/%s/output/meta.json"}') % (BUCKET, FRAME, BUCKET, OIL, BUCKET, SID)
    _wire(monkeypatch, rows, {"k": result})
    pc._record_node_outputs("P", "frame_s3", "k")
    assert [(o["key"], o["kind"]) for o in rows["P#frame_s3"]["outputs"]] == [(FRAME, "image"), (OIL, "image")]


def test_once_only_and_empty_is_recorded_so_a_replay_reads_nothing(monkeypatch):
    rows = {"P#n": {"jobId": "P#n", "dependsOn": []}}
    jobs, s3 = _wire(monkeypatch, rows, {"k": "No files, just an answer."})
    pc._record_node_outputs("P", "n", "k")
    assert rows["P#n"]["outputs"] == []
    pc._record_node_outputs("P", "n", "k")
    assert s3.reads == 1
    assert jobs.updates == ["P#n"]


def test_no_registry_row_or_plan_row_is_a_no_op(monkeypatch):
    jobs, s3 = _wire(monkeypatch, {}, {})
    pc._record_node_outputs("P", "ghost", "k")
    pc._record_node_outputs("P", "PLAN", "k")
    pc._record_node_outputs("", "n", "k")
    assert jobs.updates == [] and s3.reads == 0


def test_the_stream_records_outputs_only_on_the_transition_into_completed(monkeypatch):
    calls = []
    monkeypatch.setattr(pc, "LINEAGE_EDGES", True, raising=False)
    monkeypatch.setattr(pc, "_record_node_outputs", lambda *a: calls.append(a))
    monkeypatch.setattr(pc, "_process_plan", lambda sid: None)

    def rec(old, new):
        image = {"node_id": {"S": "P#assemble"}, "status": {"S": new}, "session_id": {"S": "s"},
                 "result_s3_key": {"S": "plans/s/P/assemble/output"}}
        d = {"NewImage": image}
        if old is not None:
            d["OldImage"] = {"status": {"S": old}}
        return {"eventName": "MODIFY", "dynamodb": d}

    pc.handler({"Records": [rec("running", "completed")]}, None)
    pc.handler({"Records": [rec("completed", "completed")]}, None)  # a later touch of the row
    pc.handler({"Records": [rec("running", "failed")]}, None)
    assert calls == [("P", "assemble", "plans/s/P/assemble/output")]


def test_a_failure_to_record_never_stops_chaining(monkeypatch):
    processed = []
    monkeypatch.setattr(pc, "LINEAGE_EDGES", True, raising=False)

    def boom(*a):
        raise RuntimeError("s3 down")
    monkeypatch.setattr(pc, "_record_node_outputs", boom)
    monkeypatch.setattr(pc, "_process_plan", lambda sid: processed.append(sid))
    image = {"node_id": {"S": "P#n"}, "status": {"S": "completed"}, "session_id": {"S": "s"}}
    pc.handler({"Records": [{"eventName": "INSERT", "dynamodb": {"NewImage": image}}]}, None)
    assert processed == ["s"]


def test_media_kinds_and_output_roots():
    assert pc._media_kind("a/b/clip.MP4") == "video"
    assert pc._media_kind("a/b/still.webp") == "image"
    assert pc._media_kind("a/b/voice.wav") == "audio"
    assert pc._media_kind("a/b/data.json") is None
    assert pc._media_kind("a/b/noext") is None
    assert pc._is_session_output_key(FILM) and pc._is_session_output_key(FRAME) and pc._is_session_output_key(OIL)
    assert not pc._is_session_output_key(SOURCE)
    assert not pc._is_session_output_key(f"artifacts/{SID}/input/upload.mp4")


def test_flag_off_records_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(pc, "LINEAGE_EDGES", False, raising=False)
    monkeypatch.setattr(pc, "_record_node_outputs", lambda *a: calls.append(a))
    monkeypatch.setattr(pc, "_process_plan", lambda sid: None)
    image = {"node_id": {"S": "P#n"}, "status": {"S": "completed"}, "session_id": {"S": "s"}}
    pc.handler({"Records": [{"eventName": "INSERT", "dynamodb": {"NewImage": image}}]}, None)
    assert calls == []
    rows = {"P#n": {"jobId": "P#n"}}
    jobs, _ = _wire(monkeypatch, rows, {})
    monkeypatch.setattr(pc, "LINEAGE_EDGES", False, raising=False)
    pc._record_node_inputs("P", "n", ["s3://b/k.mp4"])
    assert "inputRefs" not in rows["P#n"] and jobs.updates == []


def test_what_a_node_was_handed_is_recorded_and_never_becomes_its_output(monkeypatch):
    shot = f"micro-workflow/x/shots/shot_001.mp4"
    handed = pc.handed_input_refs(
        f"--- Output of shots ---\nDELIVERABLE FILES:\n  - s3://m2c-proxy/{shot}  (shot_001.mp4)\n"
        f"  - s3://{BUCKET}/{FRAME}  (frame)\n  - s3://m2c-proxy/micro-workflow/x/  (prefix, ignored)",
        "", {"image_url": f"s3://{BUCKET}/{FRAME}", "n": 1})
    assert handed == [f"s3://m2c-proxy/{shot}", f"s3://{BUCKET}/{FRAME}"]
    # The node's own prompt names the source as our CDN URL or a presigned GET; a node's stored text is not media.
    monkeypatch.setattr(pc, "ARTIFACTS_DOMAIN", "d1.cloudfront.net", raising=False)
    monkeypatch.setattr(pc, "SKILLS_S3_BUCKET", BUCKET, raising=False)
    assert pc.handed_input_refs(
        f"Cut the video https://d1.cloudfront.net/{SOURCE} and "
        f"https://{BUCKET}.s3.us-west-2.amazonaws.com/{OIL}?X-Amz-Signature=abc. Upstream text: "
        f"s3://{BUCKET}/plans/{SID}/P/cuts/output") == [f"s3://{BUCKET}/{SOURCE}", f"s3://{BUCKET}/{OIL}"]
    # JSON-escaped preview text (staging plan 0cc48c4d): `\\n` ends a uri, it is not part of the key.
    assert pc.handed_input_refs(f'"clip1": "s3://{BUCKET}/{FRAME}\\\\n\\\\n**clip2:** s3://{BUCKET}/{OIL}"') == [
        f"s3://{BUCKET}/{FRAME}", f"s3://{BUCKET}/{OIL}"]
    rows = {"P#restyle": {"jobId": "P#restyle", "dependsOn": []}}
    jobs, _ = _wire(monkeypatch, rows, {"k": f"Restyled s3://{BUCKET}/{FRAME} into s3://{BUCKET}/{OIL}."})
    pc._record_node_inputs("P", "restyle", handed)
    assert rows["P#restyle"]["inputRefs"] == handed
    pc._record_node_outputs("P", "restyle", "k")
    assert [o["key"] for o in rows["P#restyle"]["outputs"]] == [OIL]  # the handed frame is an input


def test_dispatch_records_the_injected_inputs(monkeypatch):
    """The dependency + prior-phase injection blocks are what the node is handed; _dispatch_node records them."""
    src = Path(HERE / "handler.py").read_text()
    assert "_record_node_inputs(plan_id, node_id, handed_input_refs(" in src
    assert "if LINEAGE_EDGES:" in src
