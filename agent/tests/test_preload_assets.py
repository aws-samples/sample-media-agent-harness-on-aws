"""Asset placement over moto S3 + DynamoDB (design Part 2).

Proves: a ``preanalyzed`` asset registers as a ``queued`` row whose seeded ``steps`` make the reducer build
with NO submission; a ``raw`` asset lands in the import inbox; both are idempotent on re-deploy.
"""
import pathlib

import boto3
import pytest
from moto import mock_aws

from ingestion.import_inbox import import_prefix
from ingestion.pipeline_store import PipelineStore, from_ddb
from preload import analysis, projects
from preload.assets import place_raw, register_preanalyzed
from preload.manifest import AssetSpec

REGION = "us-west-2"
TABLE = "projects-test"
INGEST = "ingest"
PID = "p" + "d" * 32
SUB = "owner-sub"
FX = pathlib.Path(__file__).parent / "fixtures" / "ingestion" / "evil_santa"
PKG = "preload/projects/demo"


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
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=INGEST, CreateBucketConfiguration={"LocationConstraint": REGION})
        # The preload package: media + its real staging analysis outputs.
        s3.put_object(Bucket=INGEST, Key=f"{PKG}/clip.mp4", Body=b"\x00\x00mp4")
        for key, fx in (("shot-list", "shots.json"), ("transcript", "transcript.json"),
                        ("frames", "frame_manifest.json"), ("enrichment", "shot_metadata.jsonl"),
                        ("analysis", "analysis.json"), ("probe", None)):
            if fx is None:
                continue
            s3.put_object(Bucket=INGEST, Key=f"{PKG}/analysis/{fx}", Body=(FX / fx).read_bytes())
        # Seed the owning project (indexing ON so a registered asset is queued).
        projects.seed_project(t.meta.client, TABLE, pid=PID, name="Demo", owner_sub=SUB,
                              owner_display="owner@example.com", at="2026-10-27T00:00:00Z", indexing=True)
        yield PipelineStore(t), s3


def _spec():
    return AssetSpec(media=f"{PKG}/clip.mp4", kind="video", mode="preanalyzed", analysis={
        "shot-list": f"{PKG}/analysis/shots.json",
        "transcript": f"{PKG}/analysis/transcript.json",
        "frames": f"{PKG}/analysis/frame_manifest.json",
        "enrichment": f"{PKG}/analysis/shot_metadata.jsonl",
        "analysis": f"{PKG}/analysis/analysis.json",
    })


def test_preanalyzed_registers_queued_with_buildable_steps(env):
    store, s3 = env
    placed = register_preanalyzed(s3, store, INGEST, PID, SUB, "owner@example.com", _spec())
    assert placed.created is True and placed.asset_id and placed.asset_id.startswith("video:")

    row = from_ddb(store.get_asset(PID, placed.asset_id))
    assert row["status"] == "queued"
    assert row["source"] == "preload"
    steps = row["steps"]
    # Five SUCCEEDED analysis steps, no job was ever submitted (synthetic job ids).
    assert all(steps[s]["status"] in ("SUCCEEDED", "NO_AUDIO") for s in steps)
    # The load-bearing invariant: the stored steps make the tick build with nothing to submit.
    analysis.verify_builds(steps)

    # Bytes were copied under the project where the ingestion roles can read them.
    base = f"projects/{PID}/{placed.asset_id}/"
    s3.head_object(Bucket=INGEST, Key=f"{base}source/clip.mp4")
    s3.head_object(Bucket=INGEST, Key=f"{base}analysis/shot-list/shots.json")


def test_preanalyzed_is_idempotent(env):
    store, s3 = env
    a = register_preanalyzed(s3, store, INGEST, PID, SUB, "o", _spec())
    b = register_preanalyzed(s3, store, INGEST, PID, SUB, "o", _spec())
    assert a.asset_id == b.asset_id
    assert b.created is False
    rows = store.assets_in(PID, ("queued", "analyzing", "indexing", "indexed", "failed"))
    assert len(rows) == 1


def test_raw_places_into_import_inbox_idempotently(env):
    store, s3 = env
    spec = AssetSpec(media=f"{PKG}/clip.mp4", kind="video", mode="raw", analysis={})
    first = place_raw(s3, INGEST, PID, spec)
    assert first.created is True
    assert first.key == import_prefix(PID) + "clip.mp4"
    s3.head_object(Bucket=INGEST, Key=first.key)
    second = place_raw(s3, INGEST, PID, spec)
    assert second.created is False      # already there: no re-copy


def test_non_video_preanalyzed_fails_cleanly(env):
    store, s3 = env
    spec = AssetSpec(media=f"{PKG}/clip.mp4", kind="audio", mode="preanalyzed",
                     analysis={"transcript": f"{PKG}/analysis/transcript.json"})
    from preload.assets import AssetPlacementError
    with pytest.raises(AssetPlacementError):
        register_preanalyzed(s3, store, INGEST, PID, SUB, "o", spec)


def test_raw_with_a_folder_lands_in_that_inbox_folder(env):
    from ingestion.import_inbox import import_folder
    store, s3 = env
    spec = AssetSpec(media=f"{PKG}/clip.mp4", kind="video", mode="raw", analysis={})
    placed = place_raw(s3, INGEST, PID, spec, folder="clips/day1")
    assert placed.key == import_prefix(PID) + "clips/day1/clip.mp4"
    assert import_folder(PID, placed.key) == "clips/day1"


def test_preanalyzed_with_a_folder_files_the_asset_there(env):
    store, s3 = env
    placed = register_preanalyzed(s3, store, INGEST, PID, SUB, "o", _spec(), folder="clips/day1")
    assert from_ddb(store.get_asset(PID, placed.asset_id)).get("folder") == "clips/day1"
