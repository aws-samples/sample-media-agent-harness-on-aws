"""Personal samples: the demo video every user finds, pinned first, in their Personal project.

Real AssetService + AssetStore over moto (DynamoDB + S3); only the Cognito verifier is faked.
"""
from __future__ import annotations

import os
import sys

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ingestion.handlers.asset_service import AssetService  # noqa: E402
from ingestion.personal_samples import (  # noqa: E402
    PINNED_LIST_PREFIX,
    Sample,
    sample_asset_id,
    samples_from_env,
)
from ingestion.store import AssetStore  # noqa: E402

REGION = "us-west-2"
TABLE = "projects-test"
BUCKET = "ingest-bucket"
PERSONAL = "p" + "a" * 32
SHARED = "p" + "b" * 32
OWNER = "owner-sub"
MEMBER = "member-sub"
DEMO = Sample(key="samples/0-demo.mp4", file_name="0-demo.mp4", content_type="video/mp4", version="1")
BODY = b"\x00\x00\x00\x18ftypmp42" + b"x" * 2048


class FakeVerifier:
    def __init__(self, sub=OWNER):
        self._sub = sub

    def verify(self, token, now):
        return {"sub": self._sub}


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        t = boto3.resource("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                  {"AttributeName": "SK", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        # The Personal project (indexing off, like ensurePersonal writes it) and a shared one the owner owns.
        for pid, personal in ((PERSONAL, True), (SHARED, False)):
            t.put_item(Item={"PK": f"PROJECT#{pid}", "SK": "META", "projectId": pid, "status": "active",
                             "personal": personal, "ownerSub": OWNER, "indexing": False})
            t.put_item(Item={"PK": f"PROJECT#{pid}", "SK": f"MEMBER#{OWNER}", "role": "owner"})
        t.put_item(Item={"PK": f"PROJECT#{SHARED}", "SK": f"MEMBER#{MEMBER}", "role": "member"})
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        s3.put_object(Bucket=BUCKET, Key=DEMO.key, Body=BODY, ContentType="video/mp4")
        store = AssetStore(t)

        def service(sub=OWNER, samples=(DEMO,)):
            return AssetService(store=store, verifier=FakeVerifier(sub), s3_client=s3, ingest_bucket=BUCKET,
                                personal_samples=list(samples))

        yield service, store, s3, t


def _seed(svc, pid=PERSONAL):
    return svc.handle({"userToken": "t", "projectId": pid, "command": "seed_personal_samples", "args": {},
                       "caller": "proxy"})


def _asset(t, pid, aid):
    return t.get_item(Key={"PK": f"PROJECT#{pid}", "SK": f"ASSET#{aid}"}).get("Item")


def test_seeds_the_demo_into_personal_pinned_and_stored(ctx):
    service, _, s3, t = ctx
    reply = _seed(service())
    assert reply["ok"] is True, reply
    aid = sample_asset_id(PERSONAL, DEMO)
    assert reply["value"]["seeded"] == [aid]
    row = _asset(t, PERSONAL, aid)
    assert row["displayName"] == "0-demo.mp4"
    assert row["source"] == "upload"
    # A Personal project does not index: stored, never queued for the toolkit.
    assert row["status"] == "stored"
    assert row["pinned"] is True
    # Pinned in the newest-first list: the prefix sorts above every ISO timestamp.
    assert row["assetListSk"].startswith(PINNED_LIST_PREFIX)
    assert row["assetListSk"] > "9999-12-31T23:59:59.999Z#"
    # The bytes are the user's own copy, in the upload layout (so remove deletes them).
    key = row["sourceUri"].split(f"s3://{BUCKET}/", 1)[1]
    assert key == f"projects/{PERSONAL}/{aid}/0-demo.mp4"
    assert s3.get_object(Bucket=BUCKET, Key=key)["Body"].read() == BODY


def test_seeding_again_is_a_no_op(ctx):
    service, _, _, t = ctx
    _seed(service())
    reply = _seed(service())
    assert reply["value"]["seeded"] == []
    assert reply["value"]["skipped"] == [{"fileName": "0-demo.mp4", "reason": "already_seeded"}]
    rows = t.query(KeyConditionExpression="PK = :p AND begins_with(SK, :s)",
                   ExpressionAttributeValues={":p": f"PROJECT#{PERSONAL}", ":s": "ASSET#"})["Items"]
    assert len(rows) == 1


def test_a_deleted_sample_is_never_seeded_again(ctx):
    service, store, _, t = ctx
    _seed(service())
    aid = sample_asset_id(PERSONAL, DEMO)
    removed = service().handle({"userToken": "t", "projectId": PERSONAL, "command": "remove",
                                "args": {"assetId": aid}, "caller": "proxy"})
    assert removed["ok"] is True, removed
    reply = _seed(service())
    assert reply["value"]["seeded"] == []
    assert _asset(t, PERSONAL, aid)["status"] == "removed"


def test_a_missing_sample_is_skipped_and_seeded_once_deployed(ctx):
    service, _, s3, _ = ctx
    s3.delete_object(Bucket=BUCKET, Key=DEMO.key)
    reply = _seed(service())
    assert reply["value"] == {"seeded": [], "skipped": [{"fileName": "0-demo.mp4", "reason": "sample_missing"}]}
    s3.put_object(Bucket=BUCKET, Key=DEMO.key, Body=BODY, ContentType="video/mp4")
    assert _seed(service())["value"]["seeded"] == [sample_asset_id(PERSONAL, DEMO)]


def test_only_the_owners_personal_project_is_seeded(ctx):
    service, _, _, _ = ctx
    # Another project the actor owns: not Personal, so nothing.
    assert _seed(service(), pid=SHARED)["value"]["seeded"] == []
    # A member (not the owner) of a project: nothing either.
    assert _seed(service(sub=MEMBER), pid=SHARED)["value"]["seeded"] == []


def test_no_samples_configured_is_a_no_op(ctx):
    service, _, _, _ = ctx
    assert _seed(service(samples=()))["value"] == {"seeded": [], "skipped": []}


def test_samples_from_env_accepts_only_keys_under_samples():
    raw = ('[{"key":"samples/0-demo.mp4","fileName":"0-demo.mp4","contentType":"video/mp4","version":"1"},'
           '{"key":"projects/x/y.mp4","fileName":"y.mp4"},{"key":"samples/z.mp4"},"junk"]')
    assert samples_from_env(raw) == [DEMO]
    assert samples_from_env("") == [] and samples_from_env("not json") == []
