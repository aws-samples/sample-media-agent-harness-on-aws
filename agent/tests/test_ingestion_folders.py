"""Project_Asset folders (design D20; ingestion Req 1, 9, 14).

- ``ingestion.folders.normalize_folder`` agrees with the proxy's ``normalizeFolder`` over the shared fixture
  ``fixtures/folder_cases.json`` (``ui/server/projects/folders.test.ts`` runs the same file), and
  ``derive_display_name`` agrees with the proxy's ``deriveDisplayName`` over its ``displayName`` cases;
- ``AssetStore.register_asset(folder=)`` writes the normalized folder and keeps Display_Name unique per
  Project (decision 1); ``find_by_name`` matches the folder path;
- the Asset_Service ``register_upload`` takes the folder from ``args.folder`` or the upload's signed
  ``x-amz-meta-harness-folder`` metadata;
- the session output registrar files under ``Chats/<session title> · <YYYY-MM-DD>`` with every fallback;
- ``project_status`` name matches carry ``folder``.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ingestion import folders as F  # noqa: E402
from ingestion.handlers.asset_service import AssetService  # noqa: E402
from ingestion.handlers.generated_media import (  # noqa: E402
    Deps, GeneratedMedia, Work, folder_for, session_row, work_from_turn,
)
from ingestion.limits import DEFAULT_UPLOAD_LIMITS  # noqa: E402
from ingestion.names import derive_display_name  # noqa: E402
from ingestion.store import AssetStore  # noqa: E402
from project_search import project_tools as PT  # noqa: E402
from projects import schema  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "..", "src", "ingestion", "fixtures", "folder_cases.json")
REGION = "us-west-2"
TABLE = "projects"
SESSIONS = "sessions"
BUCKET = "ingest"
PID = "p" + "d" * 32
OWNER = "owner-sub"
SID = "11111111-2222-4333-8444-555555555555"
EXT_KEY = "artifacts/external/" + "ab" * 12 + ".mp4"


# ------------------------------------------------------------------ the shared TS/Python fixture
def _cases():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


@pytest.mark.parametrize("case", _cases()["folder"], ids=lambda c: repr(c["raw"])[:40])
def test_folder_fixture_matches_python(case):
    folder, reason = F.folder_or_error(case["raw"])
    if "error" in case:
        assert (folder, reason) == (None, case["error"])
        with pytest.raises(F.InvalidFolder):
            F.normalize_folder(case["raw"])
    else:
        assert (folder, reason) == (case["folder"], None)
        if folder is not None:
            assert F.normalize_folder(folder) == folder  # idempotent


@pytest.mark.parametrize("case", _cases()["displayName"], ids=lambda c: repr(c["name"])[:40])
def test_display_name_fixture_matches_python(case):
    assert derive_display_name(case["name"], case["kind"]) == case["displayName"]


# ------------------------------------------------------------------ pure helpers
def test_sanitize_segment():
    assert F.sanitize_segment("  Launch [v2] @team #1 / cut\n\tfinal  ") == "Launch v2 team 1 - cut final"
    assert F.sanitize_segment("x" * 80) == "x" * 64
    assert F.sanitize_segment("..") == "" and F.sanitize_segment("[]@#") == "" and F.sanitize_segment(None) == ""
    assert F.sanitize_segment("ab   cd", 4) == "ab c"[:4].rstrip() and F.sanitize_segment("a\u200bb") == "ab"


def test_date_from():
    assert F.date_from("2026-10-08T23:59:59.000Z") == "2026-10-08"
    assert F.date_from("2026-10-08T23:30:00-02:00") == "2026-10-09"  # UTC date
    assert F.date_from(datetime(2026, 1, 2, 3, tzinfo=timezone.utc)) == "2026-01-02"
    assert F.date_from(1_700_000_000_000) == "2023-11-14"
    assert F.date_from("2026-10-08") == "2026-10-08"
    assert F.date_from("not a date") is None and F.date_from(None) is None and F.date_from(True) is None


def test_generated_folder_rules():
    day = "2026-10-08"
    assert F.generated_folder(title="Launch reel", session_id=SID, day=day) == "Generated/Launch reel · 2026-10-08"
    assert F.generated_folder(title=None, session_id=SID, day=day) == "Generated/Session 11111111 · 2026-10-08"
    assert F.generated_folder(title="  ", session_id=SID, day=day) == "Generated/Session 11111111 · 2026-10-08"
    assert F.generated_folder(title="New Session", session_id=SID, day=day) == "Generated/Session 11111111 · 2026-10-08"
    assert F.generated_folder(title="[]@#", session_id=SID, day=day) == "Generated/Session 11111111 · 2026-10-08"
    assert F.generated_folder(title="Cut/A [draft]", session_id=SID, day=day) == "Generated/Cut-A draft · 2026-10-08"
    assert F.generated_folder(title="x", session_id=None, day=day) == "Generated/2026-10-08"
    long = F.generated_folder(title="t" * 200, session_id=SID, day=day)
    seg = long.split("/", 1)[1]
    assert len(seg) == 64 and seg.endswith(" · 2026-10-08") and F.normalize_folder(long) == long


# ------------------------------------------------------------------ moto world
@pytest.fixture
def world(monkeypatch):
    for k, v in (("AWS_ACCESS_KEY_ID", "t"), ("AWS_SECRET_ACCESS_KEY", "t"), ("AWS_DEFAULT_REGION", REGION)):
        monkeypatch.setenv(k, v)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        t = ddb.create_table(
            TableName=TABLE, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                  {"AttributeName": "SK", "AttributeType": "S"}])
        t.put_item(Item={**schema.meta_key(PID), "projectId": PID, "status": "active", "name": "P"})
        t.put_item(Item={**schema.member_key(PID, OWNER), "role": "owner"})
        sessions = ddb.create_table(TableName=SESSIONS, BillingMode="PAY_PER_REQUEST",
                                    KeySchema=[{"AttributeName": "sessionId", "KeyType": "HASH"}],
                                    AttributeDefinitions=[{"AttributeName": "sessionId", "AttributeType": "S"}])
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        s3.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
        yield t, sessions, s3


class FakeVerifier:
    def verify(self, token, now):
        if token != "good":
            raise ValueError("bad")
        return {"sub": OWNER}


def _register(store, name, folder=None, size=1):
    aid = f"video:{uuid.uuid4()}"
    return store.register_asset(
        pid=PID, asset_id=aid, source="upload", source_uri=f"s3://{BUCKET}/projects/{PID}/{aid}/{name}",
        source_bucket=BUCKET, source_key=f"projects/{PID}/{aid}/{name}", source_size=size,
        content_type="video/mp4", file_name=name, added_by=OWNER, added_by_display=OWNER, folder=folder)


def test_store_writes_the_normalized_folder_and_names_stay_unique_per_project(world):
    t, _, _ = world
    store = AssetStore(t)
    a = _register(store, "clip.mp4", folder=" Footage / Day 1/")
    b = _register(store, "clip.mp4", folder="Footage/Day 2")
    c = _register(store, "clip.mp4")
    assert a["folder"] == "Footage/Day 1" and store.get_asset(PID, a["assetId"])["folder"] == "Footage/Day 1"
    # Decision 1: a second clip.mp4 anywhere in the Project is ` (2)`, whatever its folder.
    assert [a["displayName"], b["displayName"], c["displayName"]] == ["clip.mp4", "clip.mp4 (2)", "clip.mp4 (3)"]
    assert "folder" not in c and "folder" not in store.get_asset(PID, c["assetId"])
    with pytest.raises(F.InvalidFolder):
        _register(store, "x.mp4", folder="a/../b")


def test_find_by_name_matches_the_folder_path(world):
    t, _, _ = world
    store = AssetStore(t)
    _register(store, "a.mp4", folder="Footage/Day 1")
    _register(store, "b.mp4", folder="Footage/Day 2")
    _register(store, "day 1 notes.mp4")
    assert sorted(r["displayName"] for r in store.find_by_name(PID, "DAY 1")) == ["a.mp4", "day 1 notes.mp4"]
    assert [r["displayName"] for r in store.find_by_name(PID, "day 1/a")] == ["a.mp4"]
    assert sorted(r["displayName"] for r in store.find_by_name(PID, "footage")) == ["a.mp4", "b.mp4"]


def _register_id(store, asset_id, name, size=1):
    """Register with an EXPLICIT asset id, so the base-table SK order (ASSET#<id>) is controllable — the
    order `find_by_name` saw before ranking."""
    return store.register_asset(
        pid=PID, asset_id=asset_id, source="upload",
        source_uri=f"s3://{BUCKET}/projects/{PID}/{asset_id}/{name}",
        source_bucket=BUCKET, source_key=f"projects/{PID}/{asset_id}/{name}", source_size=size,
        content_type="video/mp4", file_name=name, added_by=OWNER, added_by_display=OWNER)


# UUIDs chosen so ASSET#<id> sorts substring < prefix < exact — the REVERSE of the ranked order, so a
# test asserting ranked order fails on the old (unranked, SK-order) code.
_ID_SUBSTRING = "video:00000000-0000-4000-8000-000000000001"
_ID_PREFIX = "video:00000000-0000-4000-8000-000000000002"
_ID_EXACT = "video:00000000-0000-4000-8000-000000000003"


def test_find_by_name_ranks_exact_prefix_substring(world):
    # Session 6eb1761a: the agent could not find a video by its exact name. find_by_name returned matches
    # in base-table (asset-id) order with no ranking, so the exact name did not come first (and past the
    # limit, not at all). Ranking must put exact > prefix > substring regardless of asset-id order.
    t, _, _ = world
    store = AssetStore(t)
    _register_id(store, _ID_SUBSTRING, "tidal_meridian.mp4")
    _register_id(store, _ID_PREFIX, "meridian_trailer.mp4")
    _register_id(store, _ID_EXACT, "meridian")
    assert [r["displayName"] for r in store.find_by_name(PID, "meridian")] == [
        "meridian", "meridian_trailer.mp4", "tidal_meridian.mp4",
    ]


def test_a_name_without_its_extension_is_exact():
    """`Netflix_Open_Content_Meridian` is the original `.mp4`, ahead of its newer `_clip_*` copies (prefix)."""
    from ingestion.store import _name_rank
    assert _name_rank({"displayName": "netflix_open_content_meridian.mp4"}, "netflix_open_content_meridian") == 0
    assert _name_rank({"displayName": "netflix_open_content_meridian_clip_0000.mp4"},
                      "netflix_open_content_meridian") == 1


def test_find_by_name_surfaces_exact_name_past_the_limit(world):
    # The reported failure: typing the exact filename still did not find it, because 20+ newer substring
    # matches (crop derivatives) filled the limit and the exact upload sorted after them. Ranking must
    # surface the exact match first even when more substring matches than the limit exist.
    t, _, _ = world
    store = AssetStore(t)
    exact = "Netflix_Open_Content_Meridian.mp4"
    # The exact upload gets the HIGHEST asset id, so unranked SK-order would place it LAST (excluded by
    # the limit). 25 crop derivatives (substring matches) get lower ids.
    for i in range(25):
        _register_id(store, f"video:00000000-0000-4000-8000-{i:012d}", f"crop_{i}_{exact}")
    _register_id(store, "video:ffffffff-0000-4000-8000-000000000000", exact)
    matches = store.find_by_name(PID, exact, limit=20)
    assert matches[0]["displayName"] == exact
    assert any(r["displayName"] == exact for r in matches)


def _service(t, s3):
    return AssetService(store=AssetStore(t), verifier=FakeVerifier(), s3_client=s3, ingest_bucket=BUCKET)


def _upload(s3, aid, name, metadata=None):
    s3.put_object(Bucket=BUCKET, Key=f"projects/{PID}/{aid}/{name}", Body=b"x" * 10, ContentType="video/mp4",
                  Metadata=metadata or {})


def _reg_event(aid, name="clip.mp4", **extra):
    return {"userToken": "good", "projectId": PID, "command": "register_upload", "caller": "proxy",
            "args": {"assetId": aid, "fileName": name, "size": 10, "contentType": "video/mp4", **extra}}


def test_register_upload_takes_the_folder_from_signed_metadata(world):
    t, _, s3 = world
    svc = _service(t, s3)
    aid = f"video:{uuid.uuid4()}"
    _upload(s3, aid, "clip.mp4", {F.FOLDER_METADATA_KEY: quote("Footage/Día 1 · A", safe="")})
    reply = svc.handle(_reg_event(aid))
    assert reply["ok"] and reply["value"]["asset"]["folder"] == "Footage/Día 1 · A"
    # Invalid or undecodable metadata is the root, never a failed registration.
    for bad in (quote("a/../b", safe=""), "%ff%fe"):
        aid2 = f"video:{uuid.uuid4()}"
        _upload(s3, aid2, "other.mp4", {F.FOLDER_METADATA_KEY: bad})
        r = svc.handle(_reg_event(aid2, "other.mp4"))
        assert r["ok"] and "folder" not in r["value"]["asset"], bad


def test_register_upload_args_folder_wins_and_is_validated(world):
    t, _, s3 = world
    svc = _service(t, s3)
    aid = f"video:{uuid.uuid4()}"
    _upload(s3, aid, "clip.mp4", {F.FOLDER_METADATA_KEY: quote("From/Metadata", safe="")})
    bad = svc.handle(_reg_event(aid, folder="x/./y"))
    assert bad["status"] == 400 and bad["error"]["code"] == "invalid_folder"
    assert AssetStore(t).get_asset(PID, aid) is None  # nothing written
    ok = svc.handle(_reg_event(aid, folder="Explicit/One"))
    assert ok["value"]["asset"]["folder"] == "Explicit/One"


def test_project_status_matches_carry_and_match_the_folder(world):
    t, _, s3 = world
    store = AssetStore(t)
    _register(store, "a.mp4", folder="Footage/Day 1")
    _register(store, "b.mp4")
    v = _service(t, s3).handle({"userToken": "good", "projectId": PID, "command": "project_status",
                                "caller": "main_agent", "args": {"name": "day 1"}})["value"]
    assert [(m["displayName"], m["folder"]) for m in v["matches"]] == [("a.mp4", "Footage/Day 1")]
    slim = PT._name_lookup(v)["matches"]
    assert slim == [{"assetId": slim[0]["assetId"], "displayName": "a.mp4", "kind": "video", "status": "queued",
                     "folder": "Footage/Day 1"}]
    assert "folder" not in PT._slim_match({"assetId": "x", "displayName": "b", "kind": "video", "status": "queued"})


# ------------------------------------------------------------------ Generated_Media
def _gm(t, s3, sessions):
    return GeneratedMedia(Deps(table=t, s3=s3, bucket=BUCKET, limits=dict(DEFAULT_UPLOAD_LIMITS),
                               sessions_table=sessions))


def _turn():
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    return work_from_turn({"kind": "turn", "sessionId": SID, "turnId": "t1", "projectId": PID, "actorSub": OWNER,
                           "startedAt": now - 60_000, "endedAt": now + 1_000, "externalRefs": [EXT_KEY]})


def _rows(t):
    from boto3.dynamodb.conditions import Key
    items = t.query(KeyConditionExpression=Key("PK").eq(schema.project_pk(PID)) & Key("SK").begins_with("ASSET#"))
    return {i["displayName"]: i for i in items["Items"]}


def test_generated_media_files_under_the_session_title(world):
    t, sessions, s3 = world
    sessions.put_item(Item={"sessionId": SID, "title": "Launch [reel] / cut", "createdAt": "2026-10-07T23:10:00.000Z",
                            "actorId": OWNER})
    s3.put_object(Bucket=BUCKET, Key=f"artifacts/{SID}/output/cut.mp4", Body=b"v", ContentType="video/mp4")
    s3.put_object(Bucket=BUCKET, Key=EXT_KEY, Body=b"e", ContentType="video/mp4")
    out = _gm(t, s3, sessions).run(_turn())
    assert len(out["registered"]) == 2
    assert {r["folder"] for r in _rows(t).values()} == {"Chats/Launch reel - cut · 2026-10-07"}


def test_generated_media_falls_back_without_a_title(world):
    t, sessions, s3 = world
    s3.put_object(Bucket=BUCKET, Key=f"artifacts/{SID}/output/cut.mp4", Body=b"v", ContentType="video/mp4")
    _gm(t, s3, sessions).run(_turn())  # no session row at all
    today = datetime.now(timezone.utc).date().isoformat()  # the object's LastModified date
    assert _rows(t)["cut.mp4"]["folder"] == f"Chats/Session 11111111 · {today}"


def test_generated_media_title_lookup_failure_never_fails_registration(world):
    t, _, s3 = world

    class Broken:
        def get_item(self, **_kw):
            raise RuntimeError("throttled")

    s3.put_object(Bucket=BUCKET, Key=f"artifacts/{SID}/output/cut.mp4", Body=b"v", ContentType="video/mp4")
    out = _gm(t, s3, Broken()).run(_turn())
    assert len(out["registered"]) == 1
    assert _rows(t)["cut.mp4"]["folder"].startswith("Chats/Session 11111111 · ")
    assert session_row(Broken(), SID) is None and session_row(None, SID) is None


def test_generated_media_external_ref_without_a_session(world):
    t, sessions, s3 = world
    s3.put_object(Bucket=BUCKET, Key=EXT_KEY, Body=b"e", ContentType="video/mp4")
    work = Work(project_id=PID, actor_sub=OWNER, session_id=None, producer="job:j1", keys=[EXT_KEY])
    assert len(_gm(t, s3, sessions).run(work)["registered"]) == 1
    (row,) = _rows(t).values()
    assert row["folder"] == f"Chats/{datetime.now(timezone.utc).date().isoformat()}"


def test_folder_for_prefers_the_session_date():
    lm = datetime(2026, 10, 9, 1, tzinfo=timezone.utc)
    assert folder_for(SID, {"title": "Promo", "createdAt": "2026-10-01T10:00:00Z"}, lm) == "Chats/Promo · 2026-10-01"
    assert folder_for(SID, {"title": "Promo"}, lm) == "Chats/Promo · 2026-10-09"
    assert folder_for(None, None, lm) == "Chats/2026-10-09"
    # An M2C job's outputs sit one level deeper, under the job label (asset-lineage R3.3).
    assert folder_for(SID, {"title": "Promo"}, lm, label="Shot Segmentation") == \
        "Chats/Promo · 2026-10-09/Shot Segmentation"
    assert folder_for(SID, {"title": "Promo"}, lm, label="[]#") == "Chats/Promo · 2026-10-09"


# ------------------------------------------------------------------ backfill script (never run here against AWS)
def _backfill_module():
    import importlib.util

    path = os.path.join(os.path.dirname(__file__), "..", "scripts", "backfill_generated_folders.py")
    spec = importlib.util.spec_from_file_location("backfill_generated_folders", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_backfill_refuses_the_wrong_account():
    mod = _backfill_module()

    class Sts:
        def get_caller_identity(self):
            return {"Account": "000000000000", "Arn": "arn:aws:sts::000000000000:assumed-role/x"}

    with pytest.raises(SystemExit, match="refusing to act"):
        mod.verify_caller("staging", Sts())
    with pytest.raises(SystemExit, match="production"):
        mod.main(["--env", "prod", "--execute"])


def test_backfill_dry_run_then_execute(world):
    t, sessions, _ = world
    mod = _backfill_module()
    store = AssetStore(t)
    sessions.put_item(Item={"sessionId": SID, "title": "Promo cut", "createdAt": "2026-09-30T08:00:00Z"})

    def gen_row(name, sid):
        aid = f"video:{uuid.uuid4()}"
        return store.register_asset(
            pid=PID, asset_id=aid, source="generated", source_uri=f"s3://{BUCKET}/projects/{PID}/{aid}/{name}",
            source_bucket=BUCKET, source_key=f"projects/{PID}/{aid}/{name}", source_size=1, content_type="video/mp4",
            file_name=name, added_by=OWNER, added_by_display=OWNER, extra_attrs={"originSessionId": sid})

    titled = gen_row("a.mp4", SID)
    untitled = gen_row("b.mp4", "22222222-2222-4333-8444-555555555555")
    sessionless = gen_row("c.mp4", None)
    filed = gen_row("d.mp4", SID)
    t.update_item(Key=schema.asset_key(PID, filed["assetId"]), UpdateExpression="SET folder = :f",
                  ExpressionAttributeValues={":f": "Generated/Kept"})
    upload = _register(store, "e.mp4")

    lines: list[str] = []
    dry = mod.run(t, sessions, execute=False, out=lines.append)
    assert dry == {"rows": 3, "written": 0, "skipped": 0, "dryRun": True}
    assert "folder" not in store.get_asset(PID, titled["assetId"])

    assert mod.run(t, sessions, execute=True, out=lines.append)["written"] == 3
    day = untitled["registeredAt"][:10]
    assert store.get_asset(PID, titled["assetId"])["folder"] == "Generated/Promo cut · 2026-09-30"
    assert store.get_asset(PID, untitled["assetId"])["folder"] == f"Generated/Session 22222222 · {day}"
    assert store.get_asset(PID, sessionless["assetId"])["folder"] == f"Generated/{day}"
    assert store.get_asset(PID, filed["assetId"])["folder"] == "Generated/Kept"
    assert "folder" not in store.get_asset(PID, upload["assetId"])
    assert mod.run(t, sessions, execute=True, out=lines.append)["rows"] == 0  # idempotent
