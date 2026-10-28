"""Lineage edges at the registration chokepoint and the ``lineage`` read API (``projects.lineageEdges``), over moto.

Covers: UP/DOWN edge items with tool/job/turn/session/time range/settings; ORIGIN guards (a fal URL resolves to the
asset mirrored from it); dropped edges (logged, a gap, the stat) that resolve when their parent registers later;
``add_derived_from`` after first registration (idempotent); the lifted 20-input cap; a multi-hop plan (M2C shots →
frames → restyles → in-node assemble) recorded hop by hop; flag off writes nothing; and the read API — directions,
depth / node caps, per-node permissions ("this chat only" files contracted away), removed parents, purge.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ingestion import generated as gen  # noqa: E402
from ingestion.handlers.asset_service import AssetService  # noqa: E402
from ingestion.handlers.generated_media import Deps, GeneratedMedia, handler, work_from_job_image  # noqa: E402
from ingestion.handlers.purge import PurgeDeps, handler as purge_handler  # noqa: E402,F401
from ingestion.lineage_edges import LineageEdges  # noqa: E402
from ingestion.limits import DEFAULT_UPLOAD_LIMITS  # noqa: E402
from ingestion.store import AssetStore  # noqa: E402
from projects import lineage_contract as lc  # noqa: E402
from projects import schema  # noqa: E402

REGION = "us-west-2"
TABLE = "projects"
JOBS = "jobs"
BUCKET = "ingest"
M2C = "m2c-proxy"
PID = "p" + "b" * 32
ACTOR = "user-1"
OTHER = "user-2"
SID = "11111111-2222-4333-8444-555555555555"
OUT = f"artifacts/{SID}/output"
EXT = "artifacts/external/" + "ab" * 12 + ".png"
FAL = "https://v3b.fal.media/files/b/x/restyle.png"
ON = json.dumps({"lineageEdges": True})


@pytest.fixture
def world(monkeypatch):
    for k, v in (("AWS_ACCESS_KEY_ID", "t"), ("AWS_SECRET_ACCESS_KEY", "t"), ("AWS_DEFAULT_REGION", REGION)):
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("INGESTION_CONFIG", ON)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        t = ddb.create_table(
            TableName=TABLE, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                  {"AttributeName": "SK", "AttributeType": "S"}])
        t.put_item(Item={**schema.meta_key(PID), "projectId": PID, "status": "active", "name": "P"})
        t.put_item(Item={**schema.member_key(PID, ACTOR), "role": "owner"})
        t.put_item(Item={**schema.member_key(PID, OTHER), "role": "editor"})
        jobs = ddb.create_table(TableName=JOBS, BillingMode="PAY_PER_REQUEST",
                                KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
                                AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}])
        s3 = boto3.client("s3", region_name=REGION)
        for b in (BUCKET, M2C):
            s3.create_bucket(Bucket=b, CreateBucketConfiguration={"LocationConstraint": REGION})
            s3.put_bucket_versioning(Bucket=b, VersioningConfiguration={"Status": "Enabled"})
        svc = GeneratedMedia(Deps(table=t, s3=s3, bucket=BUCKET, limits=dict(DEFAULT_UPLOAD_LIMITS), job_table=jobs,
                                  m2c_bucket=M2C))
        yield svc, t, s3, jobs


def _put(s3, key, body=None, ct="video/mp4", bucket=BUCKET):
    s3.put_object(Bucket=bucket, Key=key, Body=body or uuid.uuid4().bytes, ContentType=ct)


def _turn(svc, lineage, turn="t1", refs=()):
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    msg = {"kind": "turn", "sessionId": SID, "turnId": turn, "projectId": PID, "actorSub": ACTOR,
           "startedAt": now - 60_000, "endedAt": now + 1_000, "externalRefs": list(refs), "lineage": lineage}
    out = handler({"Records": [{"eventSource": "aws:sqs", "messageId": "m", "body": json.dumps(msg)}]}, service=svc)
    assert out == {"batchItemFailures": []}


def _aid(t, bucket, key):
    return AssetStore(t).asset_id_for_location(PID, bucket, key)


def _items(t, prefix):
    return t.query(KeyConditionExpression=Key("PK").eq(schema.project_pk(PID)) & Key("SK").begins_with(prefix),
                   ConsistentRead=True)["Items"]


def _register_source(t, s3, key="projects/{pid}/src.mp4", visibility=None, added_by=ACTOR, session_id=None):
    aid = f"video:{uuid.uuid4()}"
    key = key.format(pid=PID)
    _put(s3, key)
    AssetStore(t).register_asset(pid=PID, asset_id=aid, source="upload", source_uri=f"s3://{BUCKET}/{key}",
                                 source_bucket=BUCKET, source_key=key, source_size=5, content_type="video/mp4",
                                 file_name=key.rsplit("/", 1)[-1], added_by=added_by, added_by_display=added_by,
                                 visibility=visibility, session_id=session_id)
    return aid


# ------------------------------------------------------------------ edges from a turn
def test_a_turn_writes_both_directions_with_tool_job_range_and_settings(world):
    svc, t, s3, _ = world
    src = _register_source(t, s3)
    _put(s3, f"{OUT}/frame_0012.jpg", ct="image/jpeg")
    _turn(svc, [{"output": f"s3://{BUCKET}/{OUT}/frame_0012.jpg",
                 "inputs": [f"s3://{BUCKET}/projects/{PID}/src.mp4"], "tool": "timeline_frame", "via": "timeline",
                 "ranges": [{"input": f"s3://{BUCKET}/projects/{PID}/src.mp4", "start": 12.5, "end": 12.5}],
                 "settings": {"op": "frame", "token": "nope"}}])
    frame = _aid(t, BUCKET, f"{OUT}/frame_0012.jpg")
    (up,) = _items(t, f"UP#{frame}#")
    (down,) = _items(t, f"DOWN#{src}#")
    for e in (up, down):
        assert e["child"] == frame and e["parent"] == src and e["tool"] == "timeline_frame"
        assert e["via"] == "timeline" and e["turnId"] == "t1" and e["sessionId"] == SID
        assert e["ranges"] == [{"start": 12.5, "end": 12.5, "unit": "s"}] or \
            [{k: float(v) if k != "unit" else v for k, v in r.items()} for r in e["ranges"]] == \
            [{"start": 12.5, "end": 12.5, "unit": "s"}]
        assert e["settings"] == {"op": "frame"}
    assert t.get_item(Key=schema.asset_key(PID, frame))["Item"]["derivedFrom"] == [src]


def test_an_input_scraped_from_escaped_text_is_cut_at_the_escape(world):
    svc, t, s3, _ = world
    src = _register_source(t, s3)
    _put(s3, f"{OUT}/joined.mp4")
    _turn(svc, [{"output": f"s3://{BUCKET}/{OUT}/joined.mp4",
                 "inputs": [f"s3://{BUCKET}/projects/{PID}/src.mp4\\n\\n**clip2:**"]}])
    assert {e["parent"] for e in _items(t, f"UP#{_aid(t, BUCKET, f'{OUT}/joined.mp4')}#")} == {src}
    assert _items(t, lc.GAP_SK_PREFIX) == []


def test_replays_are_idempotent_and_a_new_range_is_appended_once(world):
    svc, t, s3, _ = world
    src = _register_source(t, s3)
    _put(s3, f"{OUT}/two_cuts.mp4")
    base = {"output": f"s3://{BUCKET}/{OUT}/two_cuts.mp4", "inputs": [f"s3://{BUCKET}/projects/{PID}/src.mp4"],
            "tool": "code_interpreter"}
    for _ in range(2):
        _turn(svc, [{**base, "ranges": [{"input": base["inputs"][0], "start": 1, "end": 3}]}])
    _turn(svc, [{**base, "ranges": [{"input": base["inputs"][0], "start": 10, "end": 12}]}], turn="t2")
    child = _aid(t, BUCKET, f"{OUT}/two_cuts.mp4")
    (up,) = _items(t, f"UP#{child}#")
    assert [(float(r["start"]), float(r["end"])) for r in up["ranges"]] == [(1.0, 3.0), (10.0, 12.0)]
    assert up["turnId"] == "t1"  # the first writer's producer fields stay
    assert t.get_item(Key=schema.asset_key(PID, child))["Item"]["derivedFrom"] == [src]


def test_lineage_learned_after_registration_is_added(world):
    svc, t, s3, _ = world
    a = _register_source(t, s3, key="projects/{pid}/a.mp4")
    b = _register_source(t, s3, key="projects/{pid}/b.mp4")
    _put(s3, f"{OUT}/mix.mp4")
    _turn(svc, [{"output": f"s3://{BUCKET}/{OUT}/mix.mp4", "inputs": [f"s3://{BUCKET}/projects/{PID}/a.mp4"]}])
    mix = _aid(t, BUCKET, f"{OUT}/mix.mp4")
    # A later, richer message (a replay of the sub-agent's ledger) names a second input: it lands on the row.
    _turn(svc, [{"output": f"s3://{BUCKET}/{OUT}/mix.mp4",
                 "inputs": [f"s3://{BUCKET}/projects/{PID}/a.mp4", f"s3://{BUCKET}/projects/{PID}/b.mp4"]}], turn="t2")
    assert t.get_item(Key=schema.asset_key(PID, mix))["Item"]["derivedFrom"] == [a, b]
    assert {e["parent"] for e in _items(t, f"UP#{mix}#")} == {a, b}
    assert LineageEdges(t).add_derived_from(PID, mix, [a, b]) == []  # idempotent


def test_thirty_inputs_make_thirty_edges(world):
    svc, t, s3, _ = world
    clips = []
    for i in range(30):
        _put(s3, f"{OUT}/clip_{i:02d}.mp4")
        clips.append(f"s3://{BUCKET}/{OUT}/clip_{i:02d}.mp4")
    _turn(svc, [{"output": c, "inputs": []} for c in clips])
    _put(s3, f"{OUT}/assembly.mp4")
    _turn(svc, [{"output": f"s3://{BUCKET}/{OUT}/assembly.mp4", "inputs": clips, "tool": "code_interpreter"}],
          turn="t2")
    final = _aid(t, BUCKET, f"{OUT}/assembly.mp4")
    assert len(_items(t, f"UP#{final}#")) == 30
    assert len(t.get_item(Key=schema.asset_key(PID, final))["Item"]["derivedFrom"]) == 30


def test_fal_origin_resolves_a_later_input_that_quotes_the_fal_url(world):
    svc, t, s3, _ = world
    _put(s3, f"{OUT}/frame.jpg", ct="image/jpeg")
    _put(s3, EXT, ct="image/png")
    _turn(svc, [{"output": f"s3://{BUCKET}/{OUT}/frame.jpg", "inputs": []},
                {"output": f"s3://{BUCKET}/{EXT}", "inputs": [f"s3://{BUCKET}/{OUT}/frame.jpg"],
                 "tool": "usr-fal___run_model", "origin": FAL, "settings": {"model": "flux-kontext", "seed": 3}}],
          refs=[EXT])
    restyle = _aid(t, BUCKET, EXT)
    frame = _aid(t, BUCKET, f"{OUT}/frame.jpg")
    assert _items(t, f"UP#{restyle}#")[0]["parent"] == frame
    assert LineageEdges(t).asset_for_origin(PID, FAL + "?x=1") == restyle
    # Next turn: an image-to-video whose input is the ORIGINAL fal URL (no mirror mapping in this runtime).
    vid = "artifacts/external/" + "cd" * 12 + ".mp4"
    _put(s3, vid)
    _turn(svc, [{"output": f"s3://{BUCKET}/{vid}", "inputs": [], "tool": "usr-fal___image_to_video",
                 "unresolved": [{"ref": FAL, "reason": "external_url"}]}], turn="t2", refs=[vid])
    assert _items(t, f"UP#{_aid(t, BUCKET, vid)}#")[0]["parent"] == restyle


def test_a_dropped_edge_is_logged_counted_and_resolved_when_its_parent_registers(world, caplog):
    svc, t, s3, _ = world
    _put(s3, f"{OUT}/clip.mp4")
    caplog.set_level(logging.INFO)
    # The clip's input is an M2C output nobody registered yet (the job path lands later).
    _turn(svc, [{"output": f"s3://{BUCKET}/{OUT}/clip.mp4", "inputs": [f"s3://{M2C}/wf/e1/shot_3.mp4"],
                 "tool": "code_interpreter", "ranges": [{"input": f"s3://{M2C}/wf/e1/shot_3.mp4", "start": 4,
                                                         "end": 9}]},
                {"output": f"s3://{BUCKET}/{OUT}/clip.mp4", "inputs": [],
                 "unresolved": [{"ref": "https://elsewhere.example/a.png?sig=1", "reason": "external_url"}]}])
    clip = _aid(t, BUCKET, f"{OUT}/clip.mp4")
    assert _items(t, f"UP#{clip}#") == []
    gaps = _items(t, lc.GAP_SK_PREFIX)
    assert {g["reason"] for g in gaps} == {"untracked_location"}
    assert LineageEdges(t).stats(PID)["droppedEdges"] == 1
    dropped = [r.getMessage() for r in caplog.records if "lineage_edge_dropped" in r.getMessage()]
    assert dropped and "untracked_location" in dropped[0] and "sig=" not in "".join(dropped)
    # The M2C output registers (job path): the waiting edge is written, the gap closes.
    _put(s3, "wf/e1/shot_3.mp4", bucket=M2C)
    cand, _ = gen.candidate_from_head("wf/e1/shot_3.mp4", s3.head_object(Bucket=M2C, Key="wf/e1/shot_3.mp4",
                                                                        ChecksumMode="ENABLED"),
                                      DEFAULT_UPLOAD_LIMITS, bucket=M2C)
    from ingestion.handlers.generated_media import Work
    w = Work(project_id=PID, actor_sub=ACTOR, session_id=SID, producer="job:x", job_id="x")
    svc._made = []
    out = {"registered": [], "existing": [], "deduped": [], "skipped": [], "jobs": []}
    svc._session, svc._session_read, svc._names = None, True, {}
    shot = svc._register(w, cand, out, source="m2c", role="deliverable", folder="Chats/x")
    svc._capture_lineage(w)
    (up,) = _items(t, f"UP#{clip}#")
    assert up["parent"] == shot and float(up["ranges"][0]["end"]) == 9.0
    assert LineageEdges(t).stats(PID)["droppedEdges"] == 0
    assert t.get_item(Key=schema.asset_key(PID, clip))["Item"]["derivedFrom"] == [shot]


def test_flag_off_writes_no_edge_items(world, monkeypatch):
    _, t, s3, jobs = world
    monkeypatch.setenv("INGESTION_CONFIG", json.dumps({"frameIntervalSec": 1}))
    off = GeneratedMedia(Deps(table=t, s3=s3, bucket=BUCKET, limits=dict(DEFAULT_UPLOAD_LIMITS), job_table=jobs))
    src = _register_source(t, s3)
    _put(s3, f"{OUT}/f.jpg", ct="image/jpeg")
    _turn(off, [{"output": f"s3://{BUCKET}/{OUT}/f.jpg", "inputs": [f"s3://{BUCKET}/projects/{PID}/src.mp4"],
                 "ranges": [{"input": f"s3://{BUCKET}/projects/{PID}/src.mp4", "start": 1, "end": 1}]}])
    for prefix in ("UP#", "DOWN#", "ORIGIN#", "LINEAGE"):
        assert _items(t, prefix) == []
    # ...and the row keeps the pre-flag derivedFrom.
    assert t.get_item(Key=schema.asset_key(PID, _aid(t, BUCKET, f"{OUT}/f.jpg")))["Item"]["derivedFrom"] == [src]


# ------------------------------------------------------------------ a multi-hop plan
def _plan_body(nodes):
    return json.dumps({"schema": "plan-result/v1", "plan_id": "plan-9", "status": "SUCCEEDED", "nodes": nodes})


def test_an_m2c_transcode_of_an_mxf_links_to_its_source_through_the_rows_lineage_inputs(world):
    """versant-poc01: process_media ran on a session-made .mxf; `sourceMedia` keeps only playable media (no .mxf),
    the lone job's result names no source, so the transcode registered with no lineage. `lineageInputs` fixes it."""
    svc, t, s3, _ = world
    mxf = f"projects/{PID}/sessions/{SID}/output/sample_xdcam_op1a.mxf"
    _put(s3, mxf, ct="application/mxf")
    _turn(svc, [{"output": f"s3://{BUCKET}/{mxf}", "inputs": []}])
    src = _aid(t, BUCKET, mxf)
    out_key = "micro-workflow/media-processing/e34/mp4_proxy/sample_xdcam_op1a.mp4"
    _put(s3, out_key, bucket=M2C)
    arn = "arn:aws:states:us-west-2:1:execution:media-processing:e34"
    body = json.dumps({"schema": "plan-result/v1", "nodes": [{"node_id": arn, "status": "completed", "artifacts": [
        {"url": f"https://cdn/{OUT}/e34/sample.mp4", "role": "output", "source_ref": f"s3://{M2C}/{out_key}"}]}]})
    s3.put_object(Bucket=BUCKET, Key=f"jobs/{arn}/result.json", Body=body.encode())
    svc.run(work_from_job_image({"jobId": arn, "status": "SUCCEEDED", "resultKey": f"jobs/{arn}/result.json",
                                 "actorId": ACTOR, "sessionId": SID, "producingProjectId": PID,
                                 "trioId": "media-processing|process_media",
                                 "lineageInputs": [f"s3://{BUCKET}/{mxf}"]}, {"status": "RUNNING"}, svc.deps))
    (up,) = _items(t, f"UP#{_aid(t, M2C, out_key)}#")
    assert up["parent"] == src and up["tool"] == "media-processing|process_media" and up["via"] == "job"


def test_a_plan_records_every_hop(world):
    svc, t, s3, jobs = world
    src = _register_source(t, s3, key="projects/{pid}/interview.mp4")
    src_uri = f"s3://{BUCKET}/projects/{PID}/interview.mp4"
    # shots (M2C) -> frames (task-agent node, handed the shots) -> restyle (fal; its own ledger) -> assemble (an
    # in-node ffmpeg; its outputs only on the chainer row, inputs undeclared -> inferred from depends_on).
    _put(s3, "wf/e9/shots/shot_001.mp4", bucket=M2C)
    _put(s3, "wf/e9/shots.json", ct="application/json", bucket=M2C, body=json.dumps({"shots": [
        {"shot_id": 0, "start_time": 0.0, "end_time": 2.5}, {"shot_id": 1, "start_time": 2.533, "end_time": 7.2}]}).encode())
    _put(s3, f"{OUT}/plan-9/frames/f1.jpg", ct="image/jpeg")
    _put(s3, EXT, ct="image/png")
    _put(s3, f"{OUT}/plan-9/final.mp4")
    jobs.put_item(Item={"jobId": "plan-9#frames", "dependsOn": ["shots"],
                        "inputRefs": [f"s3://{M2C}/wf/e9/shots/shot_001.mp4"]})
    jobs.put_item(Item={"jobId": "plan-9#assemble", "dependsOn": ["restyle"],
                        "outputs": [{"uri": f"s3://{BUCKET}/{OUT}/plan-9/final.mp4",
                                     "key": f"{OUT}/plan-9/final.mp4", "kind": "video"}]})
    s3.put_object(Bucket=BUCKET, Key=f"plans/{SID}/plan-9/restyle/lineage.json", Body=json.dumps({
        "lineage": [{"output": f"s3://{BUCKET}/{EXT}", "inputs": [f"s3://{BUCKET}/{OUT}/plan-9/frames/f1.jpg"],
                     "tool": "usr-fal___run_model", "settings": {"model": "flux"}}]}).encode())
    body = _plan_body([
        {"node_id": "shots", "status": "completed", "tool": "m2c___start_shot_segmentation", "depends_on": [],
         "artifacts": [{"url": f"s3://{M2C}/wf/e9/shots/shot_001.mp4", "role": "output"},
                       {"url": f"s3://{M2C}/wf/e9/shots.json", "role": "analysis"},
                       {"url": src_uri, "role": "source"}]},
        {"node_id": "frames", "status": "completed", "tool": "code_interpreter", "depends_on": ["shots"],
         "artifacts": [{"url": f"https://cdn/{OUT}/plan-9/frames/f1.jpg", "role": "output"},
                       {"url": f"s3://{M2C}/wf/e9/shots/shot_001.mp4", "role": "output"}]},  # prose naming its input
        {"node_id": "restyle", "status": "completed", "tool": "usr-fal___run_model", "depends_on": ["frames"],
         "artifacts": [{"url": f"https://cdn/{EXT}", "role": "output"}]},
        {"node_id": "assemble", "status": "completed", "depends_on": ["restyle"], "artifacts": []},
    ])
    s3.put_object(Bucket=BUCKET, Key=f"plans/{SID}/plan-9/result.json", Body=body.encode())
    new = {"jobId": "plan-9", "status": "SUCCEEDED", "resultKey": f"plans/{SID}/plan-9/result.json", "actorId": ACTOR,
           "sessionId": SID, "producingProjectId": PID, "sourceMedia": [src_uri]}
    w = work_from_job_image(new, {"status": "RUNNING"}, svc.deps)
    assert f"{OUT}/plan-9/final.mp4" not in w.keys  # the in-node film: only the chainer's node row names it
    out = svc.run(w)
    assert _aid(t, BUCKET, f"{OUT}/plan-9/final.mp4") in out["registered"]
    assert out["lineage"]["edges"] >= 4
    shot = _aid(t, M2C, "wf/e9/shots/shot_001.mp4")
    frame = _aid(t, BUCKET, f"{OUT}/plan-9/frames/f1.jpg")
    restyle = _aid(t, BUCKET, EXT)
    final = _aid(t, BUCKET, f"{OUT}/plan-9/final.mp4")

    def parents(c):
        return {e["parent"]: e for e in _items(t, f"UP#{c}#")}

    assert set(parents(shot)) == {src}                          # shot <- source (M2C job)
    # ...over that shot's time range, read from the job's shots.json.
    assert [(float(r["start"]), float(r["end"])) for r in parents(shot)[src]["ranges"]] == [(2.533, 7.2)]
    assert parents(shot)[src]["settings"] == {"shot": 1}
    assert set(parents(frame)) == {shot}                        # frame <- shot   (handed input), not <- source
    assert parents(frame)[shot]["jobId"] == "plan-9#frames" and parents(frame)[shot]["via"] == "plan_node"
    assert set(parents(restyle)) == {frame}                     # restyle <- frame (the node's own ledger)
    assert parents(restyle)[frame]["settings"] == {"model": "flux"}
    assert set(parents(final)) == {restyle}                     # final <- restyle (inferred from depends_on)
    assert parents(final)[restyle].get("inferred") is True
    assert "inferred" not in parents(frame)[shot]

    # ...and the read API walks the whole chain from the final cut.
    api = _api(t, s3)
    g = api.handle(_ev("lineage", {"assetId": final, "direction": "ancestors"}))["value"]
    assert {n["id"]: n["distance"] for n in g["nodes"]} == {final: 0, restyle: -1, frame: -2, shot: -3, src: -4}
    stages = {n["id"]: n["stage"] for n in g["nodes"]}
    assert stages[src] == "source" and stages[final] == "final" and stages[frame] == "intermediate"
    by = {(e["from"], e["to"]): e for e in g["edges"]}
    assert by[(src, shot)]["tool"] == "m2c___start_shot_segmentation"
    assert by[(frame, restyle)]["settings"] == {"model": "flux"}


def test_a_node_ledger_with_no_inputs_falls_back_to_what_the_chainer_handed_it(world):
    """Staging plan f8ce0d3f: both nodes' ledgers had inputs [] (the code built its keys in a loop); the chainer
    had recorded what it handed each node. The edges come from those, and the clips the assemble node's result
    merely names stay the cuts node's outputs."""
    svc, t, s3, jobs = world
    src = _register_source(t, s3, key="projects/{pid}/meridian.mp4")
    src_uri = f"s3://{BUCKET}/projects/{PID}/meridian.mp4"
    c1, c2, fin = (f"{OUT}/p2/clip_20_23.mp4", f"{OUT}/p2/clip_40_43.mp4", f"{OUT}/p2/assembled_cuts.mp4")
    for k in (c1, c2, fin):
        _put(s3, k)
    row = lambda k: {"uri": f"s3://{BUCKET}/{k}", "key": k, "kind": "video"}  # noqa: E731
    jobs.put_item(Item={"jobId": "p2#cuts", "dependsOn": [], "inputRefs": [src_uri], "outputs": [row(c1), row(c2)]})
    jobs.put_item(Item={"jobId": "p2#assemble", "dependsOn": ["cuts"], "outputs": [row(fin)],
                        "inputRefs": [f"s3://{BUCKET}/{c1}", f"s3://{BUCKET}/{c2}"]})
    for node, outs in (("cuts", [c1, c2]), ("assemble", [fin])):
        s3.put_object(Bucket=BUCKET, Key=f"plans/{SID}/p2/{node}/lineage.json", Body=json.dumps({"lineage": [
            {"output": f"s3://{BUCKET}/{o}", "inputs": [], "tool": "code_interpreter"} for o in outs]}).encode())
    body = _plan_body([
        {"node_id": "assemble", "status": "completed", "tool": "code_interpreter", "depends_on": ["cuts"],
         "artifacts": [{"url": f"https://cdn/{k}", "role": "output"} for k in (c1, c2, fin)]},
        {"node_id": "cuts", "status": "completed", "tool": "code_interpreter", "depends_on": [],
         "artifacts": [{"url": f"https://cdn/{c1}", "role": "output"}]},
    ]).replace('"plan-9"', '"p2"')
    s3.put_object(Bucket=BUCKET, Key=f"plans/{SID}/p2/result.json", Body=body.encode())
    w = work_from_job_image({"jobId": "p2", "status": "SUCCEEDED", "resultKey": f"plans/{SID}/p2/result.json",
                             "actorId": ACTOR, "sessionId": SID, "producingProjectId": PID},
                            {"status": "RUNNING"}, svc.deps)
    svc.run(w)

    def parents(k):
        return {e["parent"] for e in _items(t, f"UP#{_aid(t, BUCKET, k)}#")}

    assert parents(c1) == {src} and parents(c2) == {src}
    assert parents(fin) == {_aid(t, BUCKET, c1), _aid(t, BUCKET, c2)}
    assert "inferred" not in _items(t, f"UP#{_aid(t, BUCKET, fin)}#")[0]


def test_a_plan_nodes_unmirrored_fal_result_gets_its_lineage_when_the_copy_registers(world):
    """Staging plan 8aab6ff8: animate_shot2's result was still a fal.media URL when the plan settled (nothing to
    register); the copy get_plan_results mirrored later registered from a turn. The lineage parked under the URL
    lands on that copy."""
    svc, t, s3, jobs = world
    _put(s3, EXT, ct="image/png")
    _turn(svc, [{"output": f"s3://{BUCKET}/{EXT}", "inputs": []}], refs=[EXT])
    restyle = _aid(t, BUCKET, EXT)
    fal_mp4 = "https://v3b.fal.media/files/b/0aaf7e06/ru0M4-1z2Fdg4Mf5b2rev_minimax-h3.mp4"
    jobs.put_item(Item={"jobId": "p3#animate", "dependsOn": ["restyle"], "inputRefs": [f"s3://{BUCKET}/{EXT}"]})
    body = _plan_body([
        {"node_id": "restyle", "status": "completed", "tool": "usr-fal___run_model", "depends_on": [],
         "artifacts": [{"url": f"https://cdn/{EXT}", "role": "output"}]},
        {"node_id": "animate", "status": "completed", "tool": "usr-fal___submit_job", "depends_on": ["restyle"],
         "artifacts": [{"url": fal_mp4, "media_kind": "video", "role": "output"}]},
    ]).replace('"plan-9"', '"p3"')
    s3.put_object(Bucket=BUCKET, Key=f"plans/{SID}/p3/result.json", Body=body.encode())
    svc.run(work_from_job_image({"jobId": "p3", "status": "SUCCEEDED", "resultKey": f"plans/{SID}/p3/result.json",
                                 "actorId": ACTOR, "sessionId": SID, "producingProjectId": PID},
                                {"status": "RUNNING"}, svc.deps))
    assert len(_items(t, "LINEAGEORIGINWAIT#")) == 1
    vid = "artifacts/external/" + "ef" * 12 + ".mp4"
    _put(s3, vid)
    _turn(svc, [{"output": f"s3://{BUCKET}/{vid}", "inputs": [], "tool": "", "origin": fal_mp4}], turn="t9",
          refs=[vid])
    (up,) = _items(t, f"UP#{_aid(t, BUCKET, vid)}#")
    assert up["parent"] == restyle and up["tool"] == "usr-fal___submit_job" and up["jobId"] == "p3#animate"
    assert _items(t, "LINEAGEORIGINWAIT#")[0]["resolvedBy"] == _aid(t, BUCKET, vid)


# ------------------------------------------------------------------ the read API
class FakeVerifier:
    def __init__(self, sub=ACTOR):
        self.sub = sub

    def verify(self, token, now):
        return {"sub": self.sub}


def _api(t, s3, sub=ACTOR):
    return AssetService(store=AssetStore(t), verifier=FakeVerifier(sub), s3_client=s3, ingest_bucket=BUCKET)


def _ev(command, args):
    return {"userToken": "x", "projectId": PID, "command": command, "args": args, "caller": "proxy"}


def _chain(t, s3, n=4):
    """src -> a1 -> a2 -> ... edges written directly."""
    ids = [_register_source(t, s3, key=f"projects/{{pid}}/n{i}.mp4") for i in range(n)]
    e = LineageEdges(t)
    for p, c in zip(ids, ids[1:]):
        e.put_edge(PID, c, p, tool="code_interpreter", ranges=[{"start": 1, "end": 2}])
    return ids


def test_api_directions_depth_and_node_caps(world):
    _, t, s3, _ = world
    ids = _chain(t, s3, 5)
    api = _api(t, s3)
    up = api.handle(_ev("lineage", {"assetId": ids[4], "direction": "ancestors"}))["value"]
    assert sorted(n["distance"] for n in up["nodes"]) == [-4, -3, -2, -1, 0] and not up["truncated"]
    down = api.handle(_ev("lineage", {"assetId": ids[0], "direction": "descendants", "depth": 2}))["value"]
    assert sorted(n["distance"] for n in down["nodes"]) == [0, 1, 2] and down["truncated"]
    both = api.handle(_ev("lineage", {"assetId": ids[2]}))["value"]
    assert len(both["nodes"]) == 5 and len(both["edges"]) == 4
    assert both["edges"][0]["ranges"] == [{"start": 1, "end": 2, "unit": "s"}]
    capped = api.handle(_ev("lineage", {"assetId": ids[0], "direction": "descendants", "maxNodes": 3}))["value"]
    assert len(capped["nodes"]) == 3 and capped["truncated"]
    whole = api.handle(_ev("lineage", {"direction": "project"}))["value"]
    assert len(whole["nodes"]) == 5 and whole["stats"]["edges"] == 4
    for bad in ({"direction": "sideways"}, {"assetId": "nope", "direction": "ancestors"}, {"assetId": ids[0], "depth": 0},
                {"assetId": ids[0], "maxNodes": 99999}, {"assetId": ids[0], "depth": True}):
        r = api.handle(_ev("lineage", bad))
        assert r["ok"] is False and r["status"] == 400, bad


def test_api_hides_another_members_chat_only_file_and_contracts_through_it(world):
    _, t, s3, _ = world
    src = _register_source(t, s3, key="projects/{pid}/s.mp4")
    hidden = _register_source(t, s3, key="projects/{pid}/mine.png", visibility="session", added_by=ACTOR,
                              session_id=SID)
    final = _register_source(t, s3, key="projects/{pid}/final.mp4")
    e = LineageEdges(t)
    e.put_edge(PID, hidden, src, tool="t1")
    e.put_edge(PID, final, hidden, tool="t2")
    # Another member: the hidden file is not a node; the chain reads src -> final.
    g = _api(t, s3, OTHER).handle(_ev("lineage", {"assetId": final, "direction": "ancestors"}))["value"]
    assert {n["id"] for n in g["nodes"]} == {src, final}
    assert g["edges"] == [{"from": src, "to": final, "contracted": True}]
    assert hidden not in json.dumps(g)
    # Focusing on it is the uniform not-found.
    r = _api(t, s3, OTHER).handle(_ev("lineage", {"assetId": hidden}))
    assert r["status"] == 404 and r["error"]["code"] == "asset_not_found"
    # Its owner, in its chat, sees it.
    g = _api(t, s3, ACTOR).handle(_ev("lineage", {"assetId": final, "direction": "ancestors",
                                                  "sessionId": SID}))["value"]
    assert {n["id"] for n in g["nodes"]} == {src, hidden, final}


def test_api_keeps_removed_parents_and_counts_unlinked_generated_files(world):
    svc, t, s3, _ = world
    src = _register_source(t, s3, key="projects/{pid}/old.mp4")
    _put(s3, f"{OUT}/cut.mp4")
    _put(s3, f"{OUT}/orphan.png", ct="image/png")
    _put(s3, f"{OUT}/gap.png", ct="image/png")
    _turn(svc, [{"output": f"s3://{BUCKET}/{OUT}/cut.mp4", "inputs": [f"s3://{BUCKET}/projects/{PID}/old.mp4"]},
                {"output": f"s3://{BUCKET}/{OUT}/orphan.png", "inputs": ["s3://not-ours/x.png"]},
                {"output": f"s3://{BUCKET}/{OUT}/gap.png", "inputs": [f"s3://{BUCKET}/{OUT}/never-registered.png"]}])
    AssetStore(t).remove_asset(PID, src, removed_by=ACTOR)
    g = _api(t, s3).handle(_ev("lineage", {"direction": "project"}))["value"]
    node = {n["id"]: n for n in g["nodes"]}[src]
    assert node["removed"] is True and node["status"] == "removed"
    # A bucket we do not own is an external ROOT, not a gap; an untracked object in OUR bucket is a gap.
    assert g["stats"]["generated"] == 3 and g["stats"]["generatedWithoutLineage"] == 1
    assert g["stats"]["origins"]["external"] == 1 and g["stats"]["origins"]["unrecorded"] == 1
    assert g["stats"]["droppedEdges"] == 1
    assert [x["reason"] for x in g["gaps"]] == ["untracked_location"]
    ext = next(n for n in g["nodes"] if n.get("external"))
    assert ext["displayName"] == "From s3://not-ours/x.png" and ext["license"] == "unknown"
    orphan = next(n for n in g["nodes"] if n["displayName"] == "orphan.png")
    assert orphan["origin"] == "external"


def test_api_is_off_without_the_flag(world, monkeypatch):
    _, t, s3, _ = world
    monkeypatch.setenv("INGESTION_CONFIG", "{}")
    r = _api(t, s3).handle(_ev("lineage", {"direction": "project"}))
    assert r["status"] == 404 and r["error"]["code"] == "lineage_not_enabled"


def test_purge_records_step_deletes_edges_with_the_project(world):
    _, t, s3, _ = world
    _chain(t, s3, 3)
    assert _items(t, "UP#") and _items(t, "DOWN#")
    # The purge `records` step deletes every item of the partition except META (ingestion.handlers.purge).
    from ingestion.handlers import purge
    import inspect
    src = inspect.getsource(purge)
    assert "if it[\"SK\"] != schema.META_SK" in src
    with t.batch_writer() as bw:
        for it in t.query(KeyConditionExpression=Key("PK").eq(schema.project_pk(PID)))["Items"]:
            if it["SK"] != schema.META_SK:
                bw.delete_item(Key={"PK": it["PK"], "SK": it["SK"]})
    assert _items(t, "UP#") == [] and _items(t, "DOWN#") == []


def test_edge_store_refuses_self_and_direct_cycles(world):
    _, t, s3, _ = world
    a, b = _chain(t, s3, 2)
    e = LineageEdges(t)
    assert e.put_edge(PID, a, a) is False
    assert e.put_edge(PID, a, b) is False  # b is already made from a
    assert lc.parse_edge_sk(lc.edge_up_sk(b, a)) == ("up", b, a)


# ------------------------------------------------------------------ backfill
def test_backfill_builds_edges_from_derived_from_dry_run_first(world):
    _, t, s3, _ = world
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    import backfill_lineage_edges as bf

    a = _register_source(t, s3, key="projects/{pid}/a.mp4")
    b = _register_source(t, s3, key="projects/{pid}/b.mp4")
    t.update_item(Key=schema.asset_key(PID, b), UpdateExpression="SET derivedFrom = :d, producer = :p",
                  ExpressionAttributeValues={":d": [a, "junk", b], ":p": {"tool": "crop_video", "turnId": "t9",
                                                                          "sessionId": SID}})
    lines: list[str] = []
    dry = bf.run(t, execute=False, out=lines.append)
    assert dry == {"pairs": 1, "written": 0, "alreadyThere": 0, "skipped": 2, "parentMissing": 0, "dryRun": True}
    assert _items(t, "UP#") == []
    assert bf.run(t, execute=True, projects=[PID], out=lines.append)["written"] == 1
    (up,) = _items(t, f"UP#{b}#")
    assert up["parent"] == a and up["via"] == "backfill" and up["tool"] == "crop_video" and up["turnId"] == "t9"
    assert bf.run(t, execute=True, out=lines.append)["alreadyThere"] == 1  # re-running writes nothing
    assert len(_items(t, "UP#")) == 1 and len(_items(t, "DOWN#")) == 1


# ------------------------------------------------------------------ external sources, roots
def test_a_download_from_the_internet_is_an_external_root_without_its_credentials(world):
    svc, t, s3, _ = world
    _put(s3, f"{OUT}/sample_xdcam_op1a.mxf", ct="application/mxf")
    _put(s3, f"{OUT}/sample.mp4")
    url = "https://filesamples.example.com/samples/video/mxf/sample_xdcam_op1a.mxf?X-Amz-Signature=SECRET&token=T"
    _turn(svc, [{"output": f"s3://{BUCKET}/{OUT}/sample.mp4", "inputs": [], "tool": "code_interpreter",
                 "unresolved": [{"ref": url, "reason": "external_url"}]}])
    child = _aid(t, BUCKET, f"{OUT}/sample.mp4")
    (up,) = _items(t, f"UP#{child}#")
    assert lc.is_external_id(up["parent"])
    (src,) = _items(t, lc.EXTSRC_SK_PREFIX)
    assert src["url"] == "https://filesamples.example.com/samples/video/mxf/sample_xdcam_op1a.mxf"
    assert src["host"] == "filesamples.example.com" and src["license"] == "unknown" and src["turnId"] == "t1"
    everything = t.query(KeyConditionExpression=Key("PK").eq(schema.project_pk(PID)))["Items"]
    assert "SECRET" not in json.dumps(everything, default=str) and _items(t, lc.GAP_SK_PREFIX) == []
    # The same URL is ONE source node however many files came from it.
    _put(s3, f"{OUT}/sample2.mp4")
    _turn(svc, [{"output": f"s3://{BUCKET}/{OUT}/sample2.mp4", "inputs": [],
                 "unresolved": [{"ref": url.split("?")[0] + "?other=1", "reason": "external_url"}]}], turn="t2")
    assert len(_items(t, lc.EXTSRC_SK_PREFIX)) == 1
    g = _api(t, s3).handle(_ev("lineage", {"assetId": child, "direction": "ancestors"}))["value"]
    ext = next(n for n in g["nodes"] if n["id"] == up["parent"])
    assert ext["displayName"] == "Downloaded from filesamples.example.com" and ext["origin"] == "external"
    assert next(n for n in g["nodes"] if n["id"] == child)["origin"] == "external"
    down = _api(t, s3).handle(_ev("lineage", {"assetId": child, "direction": "both"}))["value"]
    assert {n["id"] for n in down["nodes"]} >= {child, up["parent"]}


def test_scratch_outputs_are_roots_and_genuine_gaps_are_the_only_problem(world):
    svc, t, s3, _ = world
    up = _register_source(t, s3, key="projects/{pid}/brief.png")
    _put(s3, f"{OUT}/chart.png", ct="image/png")
    _put(s3, f"{OUT}/report.pdf", ct="application/pdf")
    _turn(svc, [{"output": f"s3://{BUCKET}/{OUT}/chart.png", "inputs": [], "tool": "code_interpreter", "scratch": True},
                {"output": f"s3://{BUCKET}/{OUT}/report.pdf", "inputs": [], "tool": "code_interpreter"}])
    chart = _aid(t, BUCKET, f"{OUT}/chart.png")
    assert list(LineageEdges(t).roots(PID)) == [chart]
    g = _api(t, s3).handle(_ev("lineage", {"direction": "project"}))["value"]
    assert g["stats"]["origins"] == {"uploaded": 1, "imported": 0, "external": 0, "scratch": 1, "unrecorded": 1,
                                       "externalUndecided": 0}
    assert g["stats"]["generatedWithoutLineage"] == 1  # the report: its inputs were not recorded
    assert up  # the upload is a root, never counted as a problem
    # A scratch claim with inputs is not a root.
    assert lc.parse_entry({"output": "s3://b/x", "inputs": ["s3://b/y"], "scratch": True}).get("scratch") is None


def test_a_frame_of_an_m2c_outputs_durable_copy_links_to_the_m2c_output(world):
    """staging session d829030f: the job settle copies an M2C output to `<session output>/<execution>/<name>` and the
    agent hands THAT url to extract_frames; the copy is not an asset, so the frame's edge was a gap
    (`untracked_location`). The job's registration now records the copy as an alias of the M2C output's asset."""
    svc, t, s3, _ = world
    copy_key = f"projects/{PID}/sessions/{SID}/output/aa31e24b-ebff-50a0-80bd-bf0d9dafc80c/proxy_640.mp4"
    _put(s3, copy_key)
    early, late = f"{OUT}/early_frame.jpg", f"{OUT}/late_frame.jpg"
    _put(s3, early, ct="image/jpeg")
    # A frame registered BEFORE the job settles waits on the copy's location...
    _turn(svc, [{"output": f"s3://{BUCKET}/{early}", "inputs": [f"s3://{BUCKET}/{copy_key}"],
                 "tool": "extract_frames"}], turn="t1")
    assert _items(t, "LINEAGEGAP#")
    out_key = "micro-workflow/media-processing/e35/proxy/proxy_640.mp4"
    _put(s3, out_key, bucket=M2C)
    arn = "arn:aws:states:us-west-2:1:execution:media-processing:e35"
    body = json.dumps({"schema": "plan-result/v1", "nodes": [{"node_id": arn, "status": "SUCCEEDED", "artifacts": [
        {"url": f"https://cdn.example/{copy_key}", "role": "output", "source_ref": f"s3://{M2C}/{out_key}"}]}]})
    s3.put_object(Bucket=BUCKET, Key=f"jobs/{arn}/result.json", Body=body.encode())
    svc.run(work_from_job_image({"jobId": arn, "status": "SUCCEEDED", "resultKey": f"jobs/{arn}/result.json",
                                 "actorId": ACTOR, "sessionId": SID, "producingProjectId": PID,
                                 "trioId": "media-processing|process_media"}, {"status": "RUNNING"}, svc.deps))
    proxy = _aid(t, M2C, out_key)
    assert proxy and _aid(t, BUCKET, copy_key) is None  # the copy is still not an asset of its own
    (up,) = _items(t, f"UP#{_aid(t, BUCKET, early)}#")
    assert up["parent"] == proxy  # ...and is linked the moment the alias is written
    # ...and one registered AFTER resolves through the alias directly.
    _put(s3, late, ct="image/jpeg")
    _turn(svc, [{"output": f"s3://{BUCKET}/{late}", "inputs": [f"https://cdn.example/{copy_key}"],
                 "tool": "extract_frames"}], turn="t2")
    (up,) = _items(t, f"UP#{_aid(t, BUCKET, late)}#")
    assert up["parent"] == proxy and up["tool"] == "extract_frames"


def test_an_undecided_download_is_a_root_naming_its_candidates_never_a_false_parent(world):
    """staging session a4a205a9: a fallback-URL cell made three external parents for one file. When the runtime
    cannot tell which URL produced it, no EXTSRC and no edge is written; the file is a root that keeps the candidates,
    reported as `externalUndecided` -- not as a missing input."""
    svc, t, s3, _ = world
    _put(s3, f"{OUT}/recheck_sample.mp4")
    cands = ["https://download.samplelib.com/mp4/sample-5s.mp4?sig=SECRET",
             "https://sample-videos.com/video321/mp4/360/big_buck_bunny_360p_1mb.mp4"]
    _turn(svc, [{"output": f"s3://{BUCKET}/{OUT}/recheck_sample.mp4", "inputs": [], "tool": "code_interpreter",
                 "unresolved": [{"ref": u, "reason": lc.AMBIGUOUS_EXTERNAL} for u in cands]}])
    child = _aid(t, BUCKET, f"{OUT}/recheck_sample.mp4")
    assert _items(t, f"UP#{child}#") == [] and _items(t, lc.EXTSRC_SK_PREFIX) == []
    assert _items(t, lc.GAP_SK_PREFIX) == [] and _items(t, "LINEAGEWAIT#") == []
    root = LineageEdges(t).roots(PID)[child]
    assert root["kind"] == lc.AMBIGUOUS_EXTERNAL
    assert root["candidates"] == ["https://download.samplelib.com/mp4/sample-5s.mp4", cands[1]]
    focus = _api(t, s3).handle(_ev("lineage", {"assetId": child, "direction": "ancestors"}))["value"]
    node = next(n for n in focus["nodes"] if n["id"] == child)
    assert node["origin"] == "external_undecided" and len(node["candidates"]) == 2
    g = _api(t, s3).handle(_ev("lineage", {"direction": "project"}))["value"]
    assert g["stats"]["origins"]["externalUndecided"] == 1 and g["stats"]["origins"]["scratch"] == 0
    assert g["stats"]["generatedWithoutLineage"] == 0
    assert "SECRET" not in json.dumps(t.query(KeyConditionExpression=Key("PK").eq(schema.project_pk(PID)))["Items"],
                                      default=str)
