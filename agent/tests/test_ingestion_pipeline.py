"""Dispatcher + IngestTick end to end over moto DynamoDB/S3 with a fake M2C (design "Local development" tier 2).

The fake M2C replays the REAL staging outputs in ``fixtures/ingestion/evil_santa`` and checks every
``index_content`` document against the Harness contract, so a run that reaches ``indexed`` proves the whole
chain: registration → stream dispatch → claim → wave 1 → wave 2 → build → collection → chunked index → slot
release, with META counters exact at every step.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import uuid

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ingestion import collection, contract, records  # noqa: E402
from ingestion import reducer  # noqa: E402
from ingestion import workflows as W  # noqa: E402
from ingestion.handlers.dispatcher import Dispatcher, execution_name  # noqa: E402
from ingestion.handlers.tick import Deps, Tick  # noqa: E402
from ingestion.m2c import ToolError, Transient  # noqa: E402
from ingestion.pipeline_store import PipelineStore, from_ddb  # noqa: E402

REGION = "us-west-2"
TABLE = "projects-test"
PID = "p" + "c" * 32
FX = pathlib.Path(__file__).parent / "fixtures" / "ingestion" / "evil_santa"
M2C = "m2c-proxy"
INGEST = "ingest"
FRAMES_PREFIX = "micro-workflow/media-processing/80adacd5-b9df-536f-a8bc-a52225c893f4/frames"
OUT = {
    W.SHOT_SEGMENTATION: ("shot-list", "shots/shots.json", "shots.json"),
    W.SPEECH_DIARIZATION: ("transcript", "diar/final_transcript.json", "transcript.json"),
    W.FRAME_CAPTURE: ("frames", f"{FRAMES_PREFIX}/frame_manifest.json", "frame_manifest.json"),
    W.ENRICHMENT: ("shots", "enrich/shot_metadata.jsonl", "shot_metadata.jsonl"),
    W.CONVERSATION_ANALYSIS: ("analysis", "conv/conversation_analysis.json", "analysis.json"),
}
BY_TOOL = {spec.submit_tool: step for step, spec in W.WORKFLOWS.items()}


class FakeM2C:
    def __init__(self, *, fail=None, diar_error=None, cold_start_index=0, polls_before_done=1):
        self.jobs: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.fail, self.diar_error = fail, diar_error
        self.cold, self.polls = cold_start_index, polls_before_done
        self.indexed: list[dict] = []
        self.collections: list[dict] = []
        self.s3 = None
        self.out = dict(OUT)  # step -> (label, key, fixture); extended by the entity-metadata test
        self.image_answer = json.dumps({"description": "A red bicycle against a white wall.",
                                        "objects": ["bicycle"], "entities": ["Acme"]})

    def call_tool(self, name, args):
        self.calls.append((name, args))
        if name in BY_TOOL:
            step = BY_TOOL[name]
            job = f"arn:aws:states:us-west-2:1:execution:{step}:{uuid.uuid4()}"
            self.jobs[job] = {"step": step, "polls": 0, "args": args}
            return {"job_id": job, "status": "RUNNING", "output_s3_prefix": f"s3://{M2C}/{step}-run"}
        if name == "hybrid-search___create_collection":
            self.collections.append(args)
            return {"collection_id": args["collection_id"], "created": True}
        if name == "hybrid-search-admin___configure_search_backend":
            self.configured = getattr(self, "configured", []) + [args]
            return {"collection_id": args["collection_id"], "updated": True, "weights_locked": True}
        if name == "hybrid-search___index_content":
            for d in args["documents"]:
                assert contract.first_violation({k: v for k, v in d.items() if k != "refresh"},
                                                d["asset_id"], 10**9) is None
            job = f"arn:aws:states:us-west-2:1:execution:idx:{uuid.uuid4()}"
            self.jobs[job] = {"step": "index", "polls": 0, "args": args}
            return {"job_id": job, "status": "RUNNING"}
        if name.endswith("get_job_status"):
            job = self.jobs[args["job_id"]]
            job["polls"] += 1
            if job["polls"] <= self.polls:
                return {"job_id": args["job_id"], "status": "RUNNING"}
            if job["step"] == "index":
                if self.cold:
                    self.cold -= 1
                    return {"status": "FAILED", "error": "Lambda.AWSLambdaException"}
                n = len(job["args"]["documents"])
                self.indexed.append(job["args"])
                return {"status": "SUCCEEDED", "prepared": n, "skipped": 0, "indexed": n}
            step = job["step"]
            if step == self.fail:
                return {"status": "FAILED", "error": f"{step} exploded"}
            if step == W.SPEECH_DIARIZATION and self.diar_error:
                return {"status": "FAILED", "error": self.diar_error}
            if step == W.IMAGE_DESCRIPTION:
                rec = job["args"]["records"][0]
                assert rec["messages"][0]["content"][1]["image"]["format"] == "jpeg"
                line = {"record_id": rec["record_id"], "status": "SUCCESS", "output_text": self.image_answer}
                key = f"bedrock-inference/{uuid.uuid4()}/response.jsonl"
                self.s3.put_object(Bucket=M2C, Key=key, Body=json.dumps(line).encode())
                return {"status": "SUCCEEDED", "outputs": [{"label": "response", "uri": f"s3://{M2C}/{key}"}]}
            label, key, _ = self.out[step]
            return {"status": "SUCCEEDED", "output_s3_prefix": f"s3://{M2C}/{step}-run",
                    "outputs": [{"label": "other", "uri": f"s3://{M2C}/x"}, {"label": label, "uri": f"s3://{M2C}/{key}"}]}
        if name == "hybrid-search-admin___delete_collection":
            self.deleted_collections = getattr(self, "deleted_collections", []) + [args["collection_id"]]
            return {"collection_id": args["collection_id"], "deleted": True}
        raise AssertionError(f"unexpected tool {name}")


class FakeSFN:
    def __init__(self):
        self.started: list[dict] = []

    def start_execution(self, stateMachineArn, name, input):
        if any(s["name"] == name for s in self.started):
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "ExecutionAlreadyExists"}}, "StartExecution")
        self.started.append({"name": name, "input": json.loads(input)})
        return {"executionArn": f"arn:aws:states:us-west-2:1:execution:ai:{name}"}


@pytest.fixture
def env(monkeypatch):
    for k, v in (("AWS_ACCESS_KEY_ID", "t"), ("AWS_SECRET_ACCESS_KEY", "t"), ("AWS_DEFAULT_REGION", REGION)):
        monkeypatch.setenv(k, v)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        t = ddb.create_table(
            TableName=TABLE, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"}
                                  for n in ("PK", "SK", "assetListPk", "assetListSk")],
            GlobalSecondaryIndexes=[{"IndexName": "assetsByRegistered", "Projection": {"ProjectionType": "ALL"},
                                     "KeySchema": [{"AttributeName": "assetListPk", "KeyType": "HASH"},
                                                   {"AttributeName": "assetListSk", "KeyType": "RANGE"}]}])
        t.put_item(Item={"PK": f"PROJECT#{PID}", "SK": "META", "projectId": PID, "status": "active",
                         "name": "Trailer cuts", "collectionState": "absent"})
        s3 = boto3.client("s3", region_name=REGION)
        for b in (M2C, INGEST):
            s3.create_bucket(Bucket=b, CreateBucketConfiguration={"LocationConstraint": REGION})
        for _label, key, fx in OUT.values():
            s3.put_object(Bucket=M2C, Key=key, Body=(FX / fx).read_bytes())
        yield PipelineStore(t), s3


def register(store, n=1, kind="video", ext="mp4"):
    ids = []
    for i in range(n):
        aid = f"{kind}:{uuid.uuid4()}"
        key = f"projects/{PID}/{aid}/clip{i}.{ext}"
        store.register_asset(pid=PID, asset_id=aid, source="upload", source_uri=f"s3://{INGEST}/{key}",
                             source_bucket=INGEST, source_key=key, source_size=10, content_type=f"{kind}/{ext}",
                             file_name=f"clip{i}.{ext}", added_by="u", added_by_display="u")
        ids.append(aid)
    return ids


def stream_event(aids):
    return {"Records": [{"eventName": "INSERT", "dynamodb": {"NewImage": {
        "PK": {"S": f"PROJECT#{PID}"}, "SK": {"S": f"QUEUE#0000000000000#{a}"}}}} for a in aids]}


class Harness:
    def __init__(self, store, s3, m2c, entity_metadata=False):
        self.clock = [1_000_000.0]
        self.store, self.m2c, self.sfn = store, m2c, FakeSFN()
        m2c.s3 = s3
        store._clock = lambda: self.clock[0]
        self.dispatcher = Dispatcher(store, self.sfn, "arn:aws:states:us-west-2:1:stateMachine:ai",
                                     clock=lambda: self.clock[0])
        self.tick = Tick(Deps(store=store, m2c=m2c, s3=s3, ingest_bucket=INGEST,
                              dispatch=self.dispatcher.dispatch_project, clock=lambda: self.clock[0],
                              cfg=reducer.Config(entity_metadata=entity_metadata)))

    def drive(self, max_ticks=200):
        """Run every started execution's tick loop until all are done (round robin, like parallel SFNs)."""
        live = {s["name"]: s["input"] for s in self.sfn.started}
        seen = set(live)
        for _ in range(max_ticks):
            if not live:
                return
            wait = 30
            for name, inp in list(live.items()):
                out = self.tick.run(inp)
                if out["done"]:
                    del live[name]
                wait = min(wait, out["wait_s"])
            for s in self.sfn.started:
                if s["name"] not in seen:
                    seen.add(s["name"])
                    live[s["name"]] = s["input"]
            self.clock[0] += wait
        raise AssertionError("did not finish")

    def meta(self):
        return from_ddb(self.store.get_meta(PID))

    def row(self, aid):
        return from_ddb(self.store.get_asset(PID, aid))


def counts_match_rows(h):
    rows = h.store.assets_in(PID, ("queued", "analyzing", "indexing", "indexed", "failed", "removed"))
    meta = h.meta()
    for st in ("queued", "analyzing", "indexing", "indexed", "failed"):
        assert meta["statusCounts"][st] == sum(r["status"] == st for r in rows), st
    assert meta["processingCount"] == sum(r["status"] in ("analyzing", "indexing") for r in rows)
    assert meta["processingCount"] == sum(r.get("slotHeld") is True for r in rows)


# ---------------------------------------------------------------------------------------------- tests
def test_video_reaches_indexed_with_real_outputs(env):
    store, s3 = env
    h = Harness(store, s3, FakeM2C())
    (aid,) = register(store)
    assert store.scheduled_projects() == [PID]
    assert h.dispatcher.handle(stream_event([aid])) == {"projects": 1, "started": 1}
    assert h.sfn.started[0]["name"] == execution_name(aid, 1)
    assert h.row(aid)["status"] == "analyzing" and h.meta()["processingCount"] == 1
    # A duplicate stream record starts nothing (Req 4.19).
    assert h.dispatcher.handle(stream_event([aid]))["started"] == 0

    h.drive()
    row = h.row(aid)
    assert row["status"] == "indexed", row.get("failure")
    assert row["doc"]["counts"] == {"shot": 15, "scene": 15, "utterance": 11, "topic": 1}
    assert row["index"]["totals"] == {"prepared": 42, "skipped": 0, "indexed": 42}
    assert row["index"]["byIncarnation"]["1"]["lastCompleted"]["sha256"] == row["doc"]["sha256"]
    assert "slotHeld" not in row
    meta = h.meta()
    assert (meta["collectionState"], meta["incarnation"], meta["processingCount"]) == ("ready", 1, 0)
    counts_match_rows(h)

    # Exactly one submission per step (Req 4.19 / Property 13), with the fixed arguments (Req 4.10, 4.21).
    submits = [(n, a) for n, a in h.m2c.calls if n in BY_TOOL]
    assert sorted(BY_TOOL[n] for n, _ in submits) == sorted(W.WORKFLOWS[s].step for s in
                                                             (W.SHOT_SEGMENTATION, W.SPEECH_DIARIZATION,
                                                              W.FRAME_CAPTURE, W.ENRICHMENT,
                                                              W.CONVERSATION_ANALYSIS))
    args = dict((BY_TOOL[n], a) for n, a in submits)
    assert args[W.FRAME_CAPTURE]["output_groups"] == [{"group_name": "frames", "format": "jpeg",
                                                       "frame_interval_sec": 1.0}]
    assert args[W.ENRICHMENT]["use_batch"] is False and "transcript_s3uri" in args[W.ENRICHMENT]
    assert all("idempotency_token" not in a and "history_scope" not in a for a in args.values())
    assert h.m2c.collections == [{"collection_id": PID, "active_scopes": ["shot", "scene", "utterance", "topic"],
                                  "name": "Trailer cuts"}]
    # The new collection gets the Project weights, so topic summaries are searchable (collection.RETRIEVAL_PATHS).
    assert h.m2c.configured == [{"collection_id": PID, "retrieval_paths": collection.RETRIEVAL_PATHS}]
    assert "topic_ann" in collection.RETRIEVAL_PATHS["visual"]
    (chunk,) = h.m2c.indexed
    assert chunk["collection_id"] == PID and chunk["asset_metadata"] == {"title": "clip0.mp4"}
    assert not any(d["refresh"] for d in chunk["documents"])  # first submission (Req 6.4)

    # The stored Document_Record round-trips and is what was indexed.
    body = s3.get_object(Bucket=INGEST, Key=row["doc"]["recordKey"])["Body"].read().decode()
    assert records.digest(body) == row["doc"]["sha256"]
    assert [{k: v for k, v in d.items() if k != "refresh"} for d in chunk["documents"]] == records.parse(body)

    # Idle now: the sweep clears the SCHED marker.
    h.dispatcher.handle({})
    assert store.scheduled_projects() == []


def _entity_fixtures(s3):
    """Upload the three entity-metadata workflow output fixtures to the fake M2C bucket; return the ``out`` map."""
    ent_out = {
        W.FACE_RECOGNITION: ("face-metadata", "face/facemetadata.json", "entities/facemetadata.json"),
        W.PROGRAM_STRUCTURE: ("structure", "psa/structure.json", "entities/structure.json"),
        W.CAST_RESOLUTION: ("persons", "cast/persons.json", "entities/persons.json"),
    }
    for _label, key, fx in ent_out.values():
        s3.put_object(Bucket=M2C, Key=key, Body=(FX.parent / fx).read_bytes())
    return ent_out


def _scene_entities(s3, row):
    """Every scene doc's named_entities string, read back from the stored Document_Record."""
    body = s3.get_object(Bucket=INGEST, Key=row["doc"]["recordKey"])["Body"].read().decode()
    out = []
    for d in records.parse(body):
        if d["doc_type"] == "scene":
            out.append(next((c["value"] for c in d["content"] if c["field"] == "named_entities"), ""))
    return out


def test_video_with_entity_metadata_folds_names_into_scene_named_entities(env):
    store, s3 = env
    # The entity-metadata workflows' outputs (real staging facemetadata/structure + the cast describe_tool
    # contract example) uploaded to the fake M2C bucket.
    m2c = FakeM2C()
    m2c.out.update(_entity_fixtures(s3))
    h = Harness(store, s3, m2c, entity_metadata=True)
    (aid,) = register(store)
    h.dispatcher.handle(stream_event([aid]))
    h.drive()

    row = h.row(aid)
    assert row["status"] == "indexed", row.get("failure")
    # Phase 1 adds NO new doc types / no new scope — the indexed document counts match the flag-off run.
    assert row["doc"]["counts"] == {"shot": 15, "scene": 15, "utterance": 11, "topic": 1}
    # The three workflows were actually submitted (right tool names).
    called = {name for name, _ in m2c.calls}
    assert {"face-recognition___recognize_faces", "program-structure-analysis___analyze_program_structure",
            "cast-resolution___resolve_cast"} <= called
    # The entities are now SEARCHABLE: they live in the scene docs' named_entities, NOT an asset blob.
    assert "ontology" not in row and "entities" not in row
    scene_ents = _scene_entities(s3, row)
    joined = " || ".join(scene_ents)
    # The resolved celebrity identity, a cast actor name, and a program-segment label all folded (the
    # names whose appearance spans fall within this 52 s clip's scene timeline).
    assert "Kevin Kilner" in joined          # face-recognition identity
    assert "Miriam Okafor" in joined         # cast-resolution actor name
    assert any(("office" in e or "beach coast" in e) for e in scene_ents)  # program-structure sequence setting
    # Every enriched scene doc still passes the contract (named_entities is a valid scene field).
    body = s3.get_object(Bucket=INGEST, Key=row["doc"]["recordKey"])["Body"].read().decode()
    contract.check(records.parse(body), aid, 10**9)
    # A scene only carries names whose appearance span overlaps it — not every scene gets every name.
    assert any(e == "" or "Kevin Kilner" not in e for e in scene_ents)
    counts_match_rows(h)


def test_video_without_entity_metadata_scene_docs_are_unchanged(env):
    store, s3 = env
    # Baseline: the flag-OFF run's stored record digest (no folding, no entity inputs).
    base = FakeM2C()
    hb = Harness(store, s3, base)
    (aid0,) = register(store)
    hb.dispatcher.handle(stream_event([aid0]))
    hb.drive()
    base_row = hb.row(aid0)
    base_scene_ents = _scene_entities(s3, base_row)

    # Even with the fixtures present, flag OFF means the workflows never run and the scene docs are identical.
    m2c = FakeM2C()
    m2c.out.update(_entity_fixtures(s3))
    h = Harness(store, s3, m2c)  # flag OFF (default)
    (aid,) = register(store)
    h.dispatcher.handle(stream_event([aid]))
    h.drive()
    row = h.row(aid)
    assert row["status"] == "indexed", row.get("failure")
    assert "ontology" not in row and "entities" not in row
    # Identical scene named_entities as the flag-off baseline (the fold is a pure no-op when off; the
    # document digests themselves differ only because the two assets have different asset ids).
    assert _scene_entities(s3, row) == base_scene_ents
    # None of the entity-metadata workflows were submitted.
    called = {name for name, _ in m2c.calls}
    assert not any("face-recognition" in c or "program-structure" in c or "cast-resolution" in c
                   for c in called)
    counts_match_rows(h)


def test_face_failure_still_indexes_with_cast_blocked_and_other_names_still_folded(env):
    # A terminal face-recognition failure is best-effort: the asset still reaches `indexed`, cast-resolution
    # is SKIPPED (blocked on face), and the program-structure names it DID get still fold into the scenes.
    store, s3 = env
    m2c = FakeM2C(fail=W.FACE_RECOGNITION)
    m2c.out.update(_entity_fixtures(s3))
    h = Harness(store, s3, m2c, entity_metadata=True)
    (aid,) = register(store)
    h.dispatcher.handle(stream_event([aid]))
    h.drive()

    row = h.row(aid)
    assert row["status"] == "indexed", row.get("failure")
    assert row["doc"]["counts"] == {"shot": 15, "scene": 15, "utterance": 11, "topic": 1}
    steps = row["steps"]
    assert steps[W.FACE_RECOGNITION]["status"] == "FAILED"
    assert "exploded" in steps[W.FACE_RECOGNITION]["error"]  # per-step error persisted for the panel
    assert steps[W.CAST_RESOLUTION]["status"] == reducer.SKIPPED
    assert W.FACE_RECOGNITION in steps[W.CAST_RESOLUTION]["error"]  # reason names the blocking step
    assert steps[W.PROGRAM_STRUCTURE]["status"] == "SUCCEEDED"
    # cast-resolution never submitted; program-structure was.
    called = {name for name, _ in m2c.calls}
    assert "cast-resolution___resolve_cast" not in called
    assert "program-structure-analysis___analyze_program_structure" in called
    # No people/cast names (those workflows did not succeed), but the program-structure labels still folded.
    joined = " || ".join(_scene_entities(s3, row))
    assert "Kevin Kilner" not in joined and "Miriam Okafor" not in joined
    assert any(("office" in e or "beach coast" in e or "blackframes" in e) for e in _scene_entities(s3, row))
    assert "ontology" not in row and "entities" not in row
    counts_match_rows(h)


def test_program_structure_failure_still_indexes_with_people_and_cast_folded(env):
    # The independent program-structure step fails; face + cast succeed → indexed, their names still fold,
    # no segment labels, asset never fails.
    store, s3 = env
    m2c = FakeM2C(fail=W.PROGRAM_STRUCTURE)
    m2c.out.update(_entity_fixtures(s3))
    h = Harness(store, s3, m2c, entity_metadata=True)
    (aid,) = register(store)
    h.dispatcher.handle(stream_event([aid]))
    h.drive()

    row = h.row(aid)
    assert row["status"] == "indexed", row.get("failure")
    assert row["steps"][W.PROGRAM_STRUCTURE]["status"] == "FAILED"
    joined = " || ".join(_scene_entities(s3, row))
    assert "Kevin Kilner" in joined and "Miriam Okafor" in joined
    # program-structure labels absent (that step failed); people/cast still fold.
    assert not any(("office" in e or "beach coast" in e or "blackframes" in e)
                   for e in _scene_entities(s3, row))
    assert "ontology" not in row and "entities" not in row
    counts_match_rows(h)


def test_five_slots_then_the_rest_start_in_order(env):
    store, s3 = env
    h = Harness(store, s3, FakeM2C())
    aids = register(store, 7)
    h.dispatcher.handle(stream_event(aids))
    assert len(h.sfn.started) == 5 and h.meta()["processingCount"] == 5
    queued = [a for a in aids if h.row(a)["status"] == "queued"]
    assert len(queued) == 2
    h.drive(max_ticks=400)
    assert all(h.row(a)["status"] == "indexed" for a in aids)
    assert len(h.sfn.started) == 7
    counts_match_rows(h)


def test_workflow_failure_fails_with_reason_and_releases_slot(env):
    store, s3 = env
    h = Harness(store, s3, FakeM2C(fail=W.SHOT_SEGMENTATION))
    aids = register(store, 6)
    h.dispatcher.handle(stream_event(aids))
    h.drive(max_ticks=400)
    for a in aids:
        row = h.row(a)
        assert row["status"] == "failed"
        assert row["failure"] == {"step": "shot-segmentation", "reason": "shot-segmentation: shot-segmentation exploded",
                                  "at": row["failure"]["at"]}
    assert h.meta()["processingCount"] == 0
    assert not any(n == "hybrid-search___index_content" for n, _ in h.m2c.calls)
    counts_match_rows(h)
    fail_idx = store.table.query(KeyConditionExpression="PK = :p AND begins_with(SK, :f)",
                                 ExpressionAttributeValues={":p": f"PROJECT#{PID}", ":f": "FAILIDX#"})["Items"]
    assert len(fail_idx) == 6


def test_silent_video_is_indexed_visually(env):
    store, s3 = env
    h = Harness(store, s3, FakeM2C(diar_error="Stream map '0:a:0' matches no streams."))
    (aid,) = register(store)
    h.dispatcher.handle(stream_event([aid]))
    h.drive()
    row = h.row(aid)
    assert row["status"] == "indexed", row.get("failure")
    assert row["steps"][W.SPEECH_DIARIZATION]["status"] == "NO_AUDIO"
    assert row["doc"]["counts"] == {"shot": 15, "scene": 15, "utterance": 0, "topic": 0}
    assert not any(n == "conversation-analysis___analyze_conversation" for n, _ in h.m2c.calls)
    enr = next(a for n, a in h.m2c.calls if n == "shot-metadata-enrichment___enrich_shot_metadata")
    assert "transcript_s3uri" not in enr


# A near-silent VIDEO whose diarization fails for lack of usable speech ("No high-quality embeddings
# found" / "No VAD turns found") must degrade to indexed-visually, NOT a hard Failed. Verbatim staging
# text: project p925d3f6's crop_025720af.mp4 (a 127 KB clip) hard-failed before this fix because the
# "embeddings" string lived only in the no-SPEECH patterns, which the video branch did not consult.
@pytest.mark.parametrize("diar_error", [
    '{"errorMessage":"No high-quality embeddings found","errorType":"RuntimeError","requestId":"db34669f-ce4b-4f95-aad9-e5ceb0037d11","stackTrace":["  File \\"/var/task/states/global_speakers_clustering/lambda_function.py\\", line 150, in lambda_handler\\n    raise RuntimeError(\\"No high-quality embeddings found\\")\\n"]}',
    '{"errorMessage":"No VAD turns found after flattening \u2014 check VAD output","errorType":"RuntimeError"}',
])
def test_near_silent_video_without_usable_speech_is_indexed_visually(env, diar_error):
    store, s3 = env
    h = Harness(store, s3, FakeM2C(diar_error=diar_error))
    (aid,) = register(store)  # kind="video" by default
    h.dispatcher.handle(stream_event([aid]))
    h.drive()
    row = h.row(aid)
    assert row["status"] == "indexed", row.get("failure")
    assert "failure" not in row
    assert row["steps"][W.SPEECH_DIARIZATION]["status"] == "NO_AUDIO"
    # Visual-only: shots/scenes indexed, no speech, and conversation-analysis never runs.
    assert row["doc"]["counts"] == {"shot": 15, "scene": 15, "utterance": 0, "topic": 0}
    assert not any(n == "conversation-analysis___analyze_conversation" for n, _ in h.m2c.calls)


@pytest.mark.parametrize("diar_error", [
    "Lambda.Unknown: out of memory",
    "AccessDenied reading the source object",
])
def test_video_with_an_unrelated_diarization_failure_still_fails(env, diar_error):
    # A real decode/infra error on a video is NOT no-speech: the asset must still fail so the user sees it.
    store, s3 = env
    h = Harness(store, s3, FakeM2C(diar_error=diar_error))
    (aid,) = register(store)
    h.dispatcher.handle(stream_event([aid]))
    h.drive()
    row = h.row(aid)
    assert row["status"] == "failed"
    assert row["failure"]["step"] == "speech-diarization"


def test_cold_start_chunk_is_resubmitted_unchanged(env):
    store, s3 = env
    h = Harness(store, s3, FakeM2C(cold_start_index=2))
    (aid,) = register(store)
    h.dispatcher.handle(stream_event([aid]))
    h.drive()
    assert h.row(aid)["status"] == "indexed"
    idx_calls = [a for n, a in h.m2c.calls if n == "hybrid-search___index_content"]
    assert len(idx_calls) == 3 and idx_calls[0] == idx_calls[1] == idx_calls[2]


def test_removed_asset_stops_the_run(env):
    store, s3 = env
    h = Harness(store, s3, FakeM2C(polls_before_done=50))
    (aid,) = register(store)
    h.dispatcher.handle(stream_event([aid]))
    inp = h.sfn.started[0]["input"]
    assert not h.tick.run(inp)["done"]
    store.remove_asset(PID, aid, removed_by="u")
    assert h.tick.run(inp)["done"] is True
    assert h.row(aid)["status"] == "removed" and h.meta()["processingCount"] == 0


class FlakyM2C(FakeM2C):
    """Every submit call fails transiently ``n`` times first."""

    def __init__(self, n, **kw):
        super().__init__(**kw)
        self.n = n

    def call_tool(self, name, args):
        if name in BY_TOOL and self.n:
            self.n -= 1
            raise Transient("503 from gateway")
        return super().call_tool(name, args)


def test_transient_submit_failures_retry_then_fail_after_three(env):
    store, s3 = env
    h = Harness(store, s3, FlakyM2C(1))
    (aid,) = register(store)
    h.dispatcher.handle(stream_event([aid]))
    h.drive()
    assert h.row(aid)["status"] == "indexed"  # one transient failure is retried ≥ 30 s later

    store2_aid = register(store)[0]
    h.m2c = FlakyM2C(10**6)
    h.tick.d.m2c = h.m2c
    h.dispatcher.handle(stream_event([store2_aid]))
    h.drive()
    row = h.row(store2_aid)
    assert row["status"] == "failed" and row["failure"]["reason"].endswith("503 from gateway")


class RejectingM2C(FakeM2C):
    def call_tool(self, name, args):
        if name == W.WORKFLOWS[W.FRAME_CAPTURE].submit_tool:
            raise ToolError("width must be ≤ 4096", "INVALID_PARAMETER")
        return super().call_tool(name, args)


def test_rejected_submission_fails_the_asset(env):
    store, s3 = env
    h = Harness(store, s3, RejectingM2C())
    (aid,) = register(store)
    h.dispatcher.handle(stream_event([aid]))
    h.drive()
    assert h.row(aid)["failure"]["reason"] == "frame capture: width must be ≤ 4096"


def test_audio_runs_diarization_then_conversation_analysis(env):
    store, s3 = env
    h = Harness(store, s3, FakeM2C())
    (aid,) = register(store, kind="audio", ext="mp3")
    h.dispatcher.handle(stream_event([aid]))
    h.drive()
    row = h.row(aid)
    assert row["status"] == "indexed", row.get("failure")
    assert row["doc"]["counts"] == {"shot": 0, "scene": 0, "utterance": 11, "topic": 1}
    tools = [n for n, _ in h.m2c.calls if n in BY_TOOL]
    assert tools == ["speech-diarization___diarize_speech", "conversation-analysis___analyze_conversation"]
    body = s3.get_object(Bucket=INGEST, Key=row["doc"]["recordKey"])["Body"].read().decode()
    assert not any("scene_parent_id" in (d.get("fields") or {}) for d in records.parse(body))


def test_document_pdf_reaches_indexed_with_page_encoded_spans(env):
    """A text-layer PDF: extract (in-tick) → build scene docs → index → indexed, with the page in the span."""
    from ingestion import documents as D
    from test_ingestion_pdf import make_pdf

    store, s3 = env
    h = Harness(store, s3, FakeM2C())
    (aid,) = register(store, kind="document", ext="pdf")
    s3.put_object(Bucket=INGEST, Key=f"projects/{PID}/{aid}/clip0.pdf",
                  Body=make_pdf([["Quarterly Report", "Revenue grew."], ["Appendix", "Table of data."]]))
    h.dispatcher.handle(stream_event([aid]))
    assert h.row(aid)["status"] == "analyzing"
    h.drive()
    row = h.row(aid)
    assert row["status"] == "indexed", row.get("failure")
    assert row["doc"]["counts"] == {"shot": 0, "scene": row["doc"]["counts"]["scene"], "utterance": 0, "topic": 0}
    assert row["doc"]["counts"]["scene"] >= 2
    assert "slotHeld" not in row
    # The extract step wrote pages.json and never submitted an M2C analysis workflow.
    ex = row["steps"][W.DOCUMENT_EXTRACT]
    assert ex["status"] == "SUCCEEDED" and ex["hasText"] is True
    assert ex["key"] == f"projects/{PID}/{aid}/extract/pages.json"
    assert not any(n in BY_TOOL for n, _ in h.m2c.calls)  # no shot-seg/diarization/etc. for a document
    # The indexed documents are all scenes and their spans decode to the right page.
    (chunk,) = h.m2c.indexed
    pages = {D.page_of_span(d["start_ms"]) for d in chunk["documents"]}
    assert pages == {1, 2}
    assert all(d["doc_type"] == "scene" for d in chunk["documents"])
    # The Document_Record round-trips from projects/<pid>/<aid>/documents/<sha>.jsonl.
    body = s3.get_object(Bucket=INGEST, Key=row["doc"]["recordKey"])["Body"].read().decode()
    assert row["doc"]["recordKey"].startswith(f"projects/{PID}/{aid}/documents/")
    assert records.digest(body) == row["doc"]["sha256"]
    counts_match_rows(h)


def test_scanned_pdf_degrades_to_stored_not_failed(env):
    """A PDF with no text layer (a scan): kept as ``stored`` with indexSkippedReason, never Failed, never requeued."""
    from test_ingestion_pdf import make_pdf

    store, s3 = env
    h = Harness(store, s3, FakeM2C())
    (aid,) = register(store, kind="document", ext="pdf")
    s3.put_object(Bucket=INGEST, Key=f"projects/{PID}/{aid}/clip0.pdf", Body=make_pdf([[], []]))  # graphics only
    h.dispatcher.handle(stream_event([aid]))
    h.drive()
    row = h.row(aid)
    assert row["status"] == "stored", row.get("failure")
    assert row["indexSkippedReason"] == "no_text_layer"
    assert row["indexSkippedStep"] == "document text"
    assert "failure" not in row and "slotHeld" not in row
    assert not any(n == "hybrid-search___index_content" for n, _ in h.m2c.calls)
    meta = h.meta()
    assert meta["processingCount"] == 0
    assert meta["statusCounts"]["failed"] == 0 and meta["statusCounts"]["stored"] == 1
    fail_idx = store.table.query(KeyConditionExpression="PK = :p AND begins_with(SK, :f)",
                                 ExpressionAttributeValues={":p": f"PROJECT#{PID}", ":f": "FAILIDX#"})["Items"]
    assert fail_idx == []
    # Turning indexing on never requeues a no-text document (store.queueable is False once a reason is set).
    assert store.queue_stored(PID) == 0
    assert h.row(aid)["status"] == "stored"
    counts_match_rows(h)


def test_corrupt_pdf_degrades_to_stored(env):
    store, s3 = env
    h = Harness(store, s3, FakeM2C())
    (aid,) = register(store, kind="document", ext="pdf")
    s3.put_object(Bucket=INGEST, Key=f"projects/{PID}/{aid}/clip0.pdf", Body=b"%PDF-1.4 then garbage")
    h.dispatcher.handle(stream_event([aid]))
    h.drive()
    row = h.row(aid)
    assert row["status"] == "stored" and row["indexSkippedReason"] == "no_text_layer"


def test_metadata_data_kind_is_not_an_indexing_pipeline_kind(env):
    """Metadata/analysis `data` files are genuinely excluded from text indexing (not merely hidden): the
    dispatcher has no pipeline for `data`, and the registrar keeps analysis-role rows out of the queue."""
    from ingestion.handlers.dispatcher import PIPELINE_KINDS
    from ingestion.store import NON_INDEXED_ROLES, queueable

    assert "data" not in PIPELINE_KINDS
    assert "document" in PIPELINE_KINDS  # the kind we DID add this round
    assert "analysis" in NON_INDEXED_ROLES and "intermediate" in NON_INDEXED_ROLES
    assert queueable({"role": "analysis"}) is False
    assert queueable({"kind": "data", "role": "analysis"}) is False
    """Every registered kind now has a pipeline, so a queued document is claimed alongside a video."""
    from test_ingestion_pdf import make_pdf

    store, s3 = env
    h = Harness(store, s3, FakeM2C())
    doc = register(store, kind="document", ext="pdf")[0]
    vid = register(store)[0]
    s3.put_object(Bucket=INGEST, Key=f"projects/{PID}/{doc}/clip0.pdf", Body=make_pdf([["Hello", "world text."]]))
    h.dispatcher.handle(stream_event([doc, vid]))
    assert {s["input"]["assetId"] for s in h.sfn.started} == {doc, vid}
    h.drive()
    assert h.row(doc)["status"] == "indexed" and h.row(vid)["status"] == "indexed"
    h.dispatcher.handle({})
    assert store.scheduled_projects() == []  # nothing left queued or processing


def _png(w, h, rgba=True):
    import io

    from PIL import Image
    im = Image.new("RGBA" if rgba else "RGB", (w, h), (200, 30, 30, 128) if rgba else (200, 30, 30))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def test_image_rendition_description_and_two_documents(env):
    store, s3 = env
    h = Harness(store, s3, FakeM2C())
    (aid,) = register(store, kind="image", ext="png")
    s3.put_object(Bucket=INGEST, Key=f"projects/{PID}/{aid}/clip0.png", Body=_png(3000, 1500))
    h.dispatcher.handle(stream_event([aid]))
    h.drive()
    row = h.row(aid)
    assert row["status"] == "indexed", row.get("failure")
    rend = row["steps"]["image rendition"]
    assert (rend["width"], rend["height"]) == (1568, 784) and rend["bytes"] <= 1_500_000
    assert rend["key"] == f"projects/{PID}/{aid}/rendition.jpg"
    assert row["doc"]["counts"] == {"shot": 1, "scene": 1, "utterance": 0, "topic": 0}
    sub = next(a for n, a in h.m2c.calls if n == "bedrock-inference___run_inference")
    assert sub["use_batch"] is False and sub["temperature"] == 0 and sub["records"][0]["record_id"] == aid
    (chunk,) = h.m2c.indexed
    shot, scene = chunk["documents"]
    assert shot["content"][0]["value"] == f"s3://{INGEST}/{rend['key']}"
    assert shot["fields"] == {"frame_s3_key": rend["key"], "scene_parent_id": "scene:0:1"}
    assert scene["content"][0]["value"] == "A red bicycle against a white wall.\nObjects: bicycle"
    assert scene["content"][2] == {"kind": "text", "field": "named_entities", "value": "Acme"}


def test_image_undecodable_fails_at_rendition(env):
    store, s3 = env
    h = Harness(store, s3, FakeM2C())
    (aid,) = register(store, kind="image", ext="png")
    s3.put_object(Bucket=INGEST, Key=f"projects/{PID}/{aid}/clip0.png", Body=b"not an image")
    h.dispatcher.handle(stream_event([aid]))
    h.drive()
    row = h.row(aid)
    assert row["failure"]["step"] == "image rendition"
    assert row["failure"]["reason"].startswith("image rendition: the image cannot be decoded")
    assert not any(n == "bedrock-inference___run_inference" for n, _ in h.m2c.calls)


def test_image_bad_description_fails_at_description(env):
    store, s3 = env
    m2c = FakeM2C()
    m2c.image_answer = "I cannot describe this."
    h = Harness(store, s3, m2c)
    (aid,) = register(store, kind="image", ext="png")
    s3.put_object(Bucket=INGEST, Key=f"projects/{PID}/{aid}/clip0.png", Body=_png(10, 10, rgba=False))
    h.dispatcher.handle(stream_event([aid]))
    h.drive()
    row = h.row(aid)
    assert row["failure"]["reason"] == ("image description: the answer is not a JSON object with a "
                                        "non-empty description")
    assert not any(n == "hybrid-search___index_content" for n, _ in h.m2c.calls)


# ------------------------------------------------------------------------------------- retry / re-add
def test_retry_reruns_only_the_failed_step_with_a_token(env):
    store, s3 = env
    m2c = FakeM2C(fail=W.SHOT_SEGMENTATION)
    h = Harness(store, s3, m2c)
    (aid,) = register(store)
    h.dispatcher.handle(stream_event([aid]))
    h.drive()
    assert h.row(aid)["status"] == "failed"
    first_calls = len(m2c.calls)

    m2c.fail = None
    row = store.retry_asset(PID, aid)
    assert row["status"] == "queued" and "failure" not in row
    counts_match_rows(h)
    h.dispatcher.handle(stream_event([aid]))
    assert h.sfn.started[-1]["name"] == execution_name(aid, 2)
    h.drive()
    row = h.row(aid)
    assert row["status"] == "indexed", row.get("failure")
    second = [(n, a) for n, a in m2c.calls[first_calls:] if n in BY_TOOL]
    tools = [BY_TOOL[n] for n, _ in second]
    # diarization + frames succeeded in run 1 and are reused; shot-seg reruns; wave 2 never ran in run 1.
    assert sorted(tools) == sorted([W.SHOT_SEGMENTATION, W.ENRICHMENT, W.CONVERSATION_ANALYSIS])
    shot_args = next(a for n, a in second if BY_TOOL[n] == W.SHOT_SEGMENTATION)
    assert shot_args["idempotency_token"].startswith("r2-")
    conv_args = next(a for n, a in second if BY_TOOL[n] == W.CONVERSATION_ANALYSIS)
    assert "idempotency_token" not in conv_args  # its first run
    counts_match_rows(h)


def test_retry_only_from_failed_and_exactly_once(env):
    from ingestion.store import WrongStatus

    store, s3 = env
    h = Harness(store, s3, FakeM2C(fail=W.SHOT_SEGMENTATION))
    (aid,) = register(store)
    with pytest.raises(WrongStatus, match="queued"):
        store.retry_asset(PID, aid)
    h.dispatcher.handle(stream_event([aid]))
    h.drive()
    store.retry_asset(PID, aid)
    with pytest.raises(WrongStatus, match="queued"):
        store.retry_asset(PID, aid)
    counts_match_rows(h)


def test_readd_requeues_and_reuses_every_output(env):
    store, s3 = env
    m2c = FakeM2C()
    h = Harness(store, s3, m2c)
    (aid,) = register(store)
    h.dispatcher.handle(stream_event([aid]))
    h.drive()
    n_before = len([n for n, _ in m2c.calls if n in BY_TOOL])
    store.remove_asset(PID, aid, removed_by="u")
    row = store.readd_asset(PID, aid)
    assert row["status"] == "queued" and row["displayName"] == "clip0.mp4"
    h.dispatcher.handle(stream_event([aid]))
    h.drive()
    assert h.row(aid)["status"] == "indexed"
    assert len([n for n, _ in m2c.calls if n in BY_TOOL]) == n_before  # nothing reran
    counts_match_rows(h)


def test_retry_reruns_a_step_whose_output_is_gone(env):
    store, s3 = env
    m2c = FakeM2C(fail=W.ENRICHMENT)
    h = Harness(store, s3, m2c)
    (aid,) = register(store)
    h.dispatcher.handle(stream_event([aid]))
    h.drive()
    assert h.row(aid)["failure"]["step"] == W.ENRICHMENT
    s3.delete_object(Bucket=M2C, Key=OUT[W.FRAME_CAPTURE][1])  # frame capture's output expired
    s3.put_object(Bucket=M2C, Key="placeholder", Body=b"")
    m2c.fail = None
    n_before = len(m2c.calls)
    store.retry_asset(PID, aid)
    h.dispatcher.handle(stream_event([aid]))
    h.tick.run(h.sfn.started[-1]["input"])  # run 2's first tick: the reuse check sees the output gone
    # Put the manifest back, as M2C's rerun would write it.
    s3.put_object(Bucket=M2C, Key=OUT[W.FRAME_CAPTURE][1], Body=(FX / OUT[W.FRAME_CAPTURE][2]).read_bytes())
    h.drive()
    rerun = sorted(BY_TOOL[n] for n, _ in m2c.calls[n_before:] if n in BY_TOOL)
    assert rerun == sorted([W.FRAME_CAPTURE, W.ENRICHMENT])
    assert h.row(aid)["status"] == "indexed"


def test_video_gets_a_hero_image(env):
    store, s3 = env
    manifest = json.loads((FX / "frame_manifest.json").read_text())
    for k in manifest["keys"]:
        s3.put_object(Bucket=M2C, Key=f"{FRAMES_PREFIX}/{k}", Body=b"\xff\xd8jpeg")
    h = Harness(store, s3, FakeM2C())
    (aid,) = register(store)
    h.dispatcher.handle(stream_event([aid]))
    h.drive()
    row = h.row(aid)
    rend = row["rendition"]
    assert rend["key"] == f"projects/{PID}/{aid}/rendition.jpg" and (rend["width"], rend["height"]) == (960, 540)
    assert s3.get_object(Bucket=INGEST, Key=rend["key"])["Body"].read() == b"\xff\xd8jpeg"
    body = s3.get_object(Bucket=INGEST, Key=row["doc"]["recordKey"])["Body"].read().decode()
    shots = [d for d in records.parse(body) if d["doc_type"] == "shot"]
    from ingestion import poster
    hero = poster.pick_frame(records.parse(body))
    assert rend["fromFrame"] == hero.split("/", 3)[3]
    assert hero in {d["content"][0]["value"] for d in shots}


def test_hero_frame_skips_credits_and_title_cards():
    from ingestion import poster

    def shot(s, e, f):
        return {"doc_type": "shot", "start_ms": s, "end_ms": e, "fields": {"scene_parent_id": f"scene:{s}:{e}"},
                "content": [{"kind": "image", "field": "vector", "value": f"s3://b/{f}.jpg"}]}

    def scene(s, e, text):
        return {"doc_type": "scene", "start_ms": s, "end_ms": e, "content": [{"kind": "text", "field": "text", "value": text}]}
    docs = [shot(0, 900, "title"), shot(1000, 1300, "talk"), shot(1300, 1500, "walk"), shot(9000, 10000, "credits"),
            scene(1000, 1300, "Two people talk.\nSetting: kitchen"),
            scene(1300, 1500, "A man walks.\nOn-screen text: EXIT"),
            scene(9000, 10000, "Credits roll.\nOn-screen text: Cast, KEVIN KILNER")]
    assert poster.pick_frame(docs) == "s3://b/talk.jpg"            # middle, no on-screen text
    assert poster.pick_frame([shot(9000, 10000, "credits")]) == "s3://b/credits.jpg"  # fallback
    assert poster.pick_frame([]) is None


def test_missing_frame_never_fails_ingestion(env):
    store, s3 = env
    h = Harness(store, s3, FakeM2C())  # the fixture's frames were never written to the M2C bucket
    (aid,) = register(store)
    h.dispatcher.handle(stream_event([aid]))
    h.drive()
    row = h.row(aid)
    assert row["status"] == "indexed" and "rendition" not in row


def test_image_row_carries_its_rendition_for_the_tile(env):
    store, s3 = env
    h = Harness(store, s3, FakeM2C())
    (aid,) = register(store, kind="image", ext="png")
    s3.put_object(Bucket=INGEST, Key=f"projects/{PID}/{aid}/clip0.png", Body=_png(40, 20))
    h.dispatcher.handle(stream_event([aid]))
    h.drive()
    assert h.row(aid)["rendition"]["key"] == f"projects/{PID}/{aid}/rendition.jpg"


# ------------------------------------------------------------------------- technical metadata (issue 2, Oct 9)
PROBE_KEY = "micro-workflow/media-processing/80adacd5-b9df-536f-a8bc-a52225c893f4/probe.json"


class ProbingM2C(FakeM2C):
    """Frame capture's status carries ``probe_s3_key`` (media-processing contract), as the staging M2C's does."""

    def call_tool(self, name, args):
        out = super().call_tool(name, args)
        if name.endswith("get_job_status") and out.get("status") == "SUCCEEDED" \
                and self.jobs[args["job_id"]]["step"] == W.FRAME_CAPTURE:
            out = {**out, "probe_s3_key": PROBE_KEY}
        return out


def test_frame_capture_keeps_the_probe_as_technical_facts(env):
    """The frame-capture job's probe.json (real staging shape, tidal_line_trailer_enUS.mp4) lands on the row."""
    store, s3 = env
    s3.put_object(Bucket=M2C, Key=PROBE_KEY, Body=(FX.parent / "probe_tidal.json").read_bytes())
    h = Harness(store, s3, ProbingM2C())
    (aid,) = register(store)
    h.dispatcher.handle(stream_event([aid]))
    h.drive()
    row = h.row(aid)
    assert row["status"] == "indexed", row.get("failure")
    tech = row["steps"][W.FRAME_CAPTURE]["facts"]["technical"]
    assert tech["video"] == {"codec": "h264", "profile": "High", "width": 1920, "height": 1080, "fps": 24.0,
                             "bitRate": 16981548, "pixFmt": "yuvj420p"}
    assert tech["audio"] == {"codec": "aac", "channels": 2, "sampleRate": 48000, "bitRate": 317375}
    assert (tech["container"], tech["encoder"], tech["durationSec"], tech["sizeBytes"]) == \
        ("mov,mp4,m4a,3gp,3g2,mj2", "Lavf61.7.100", 105.0, 227124839)
    # probe_info.format.filename is a presigned URL with a session token: it never reaches the row.
    assert "FAKE-TOKEN" not in json.dumps(row) and "filename" not in json.dumps(tech)


def test_an_unreadable_probe_never_fails_frame_capture(env):
    store, s3 = env  # no probe.json in the bucket: GetObject raises NoSuchKey
    h = Harness(store, s3, ProbingM2C())
    (aid,) = register(store)
    h.dispatcher.handle(stream_event([aid]))
    h.drive()
    row = h.row(aid)
    assert row["status"] == "indexed", row.get("failure")
    assert "technical" not in (row["steps"][W.FRAME_CAPTURE].get("facts") or {})


def test_technical_from_probe_rejects_shapes_it_cannot_read():
    from ingestion.adapters import technical_from_probe
    assert technical_from_probe(None) is None
    assert technical_from_probe({"probe_info": {"streams": "x"}}) is None
    assert technical_from_probe({"probe_info": {"streams": [{"codec_type": "video", "r_frame_rate": "0/0"}]}}) is None
    got = technical_from_probe({"probe_info": {"streams": [{"codec_type": "video", "codec_name": "prores",
                                                            "avg_frame_rate": "30000/1001", "width": "3840"}]}})
    assert got == {"video": {"codec": "prores", "width": 3840, "fps": 29.97}}


# Audio with no speech (music / sound-effect stems): kept but NOT indexed, never Failed (Req 12.7, 2026-10).
# The verbatim M2C texts from staging project p925d3f6 (`audio_sfx.wav`, `audio_music.wav`).
@pytest.mark.parametrize("diar_error", [
    '{"errorMessage":"No VAD turns found after flattening \u2014 check VAD output","errorType":"RuntimeError"}',
    '{"errorMessage":"No high-quality embeddings found","errorType":"ValueError"}',
])
def test_audio_without_speech_is_stored_not_failed(env, diar_error):
    store, s3 = env
    h = Harness(store, s3, FakeM2C(diar_error=diar_error))
    (aid,) = register(store, kind="audio", ext="wav")
    h.dispatcher.handle(stream_event([aid]))
    h.drive()
    row = h.row(aid)
    assert row["status"] == "stored", row.get("failure")
    assert row["indexSkippedReason"] == "no_speech"
    assert "failure" not in row
    assert row["steps"][W.SPEECH_DIARIZATION]["status"] == "NO_AUDIO"
    assert "slotHeld" not in row
    meta = h.meta()
    assert meta["processingCount"] == 0
    assert meta["statusCounts"]["failed"] == 0 and meta["statusCounts"]["stored"] == 1
    fail_idx = store.table.query(KeyConditionExpression="PK = :p AND begins_with(SK, :f)",
                                 ExpressionAttributeValues={":p": f"PROJECT#{PID}", ":f": "FAILIDX#"})["Items"]
    assert fail_idx == []
    assert not any(n == "conversation-analysis___analyze_conversation" for n, _ in h.m2c.calls)
    # Not retried endlessly: turning indexing on (queue_stored) never requeues it.
    assert store.queue_stored(PID) == 0
    assert h.row(aid)["status"] == "stored"


@pytest.mark.parametrize("diar_error", [
    "Source has no audio stream",
    "Stream map '0:a' matches no streams.",
])
def test_audio_with_no_decodable_audio_stream_still_fails(env, diar_error):
    # For a VIDEO these texts mean "silent video"; for an AUDIO asset they mean a broken file the user must see.
    store, s3 = env
    h = Harness(store, s3, FakeM2C(diar_error=diar_error))
    (aid,) = register(store, kind="audio", ext="wav")
    h.dispatcher.handle(stream_event([aid]))
    h.drive()
    row = h.row(aid)
    assert row["status"] == "failed" and "indexSkippedReason" not in row


def test_audio_with_an_unrelated_diarization_failure_still_fails(env):
    store, s3 = env
    h = Harness(store, s3, FakeM2C(diar_error="Lambda.Unknown: out of memory"))
    (aid,) = register(store, kind="audio", ext="wav")
    h.dispatcher.handle(stream_event([aid]))
    h.drive()
    row = h.row(aid)
    assert row["status"] == "failed" and "indexSkippedReason" not in row
