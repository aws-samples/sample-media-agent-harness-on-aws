"""Organizing a Project: ``move_assets``, ``rename_folder`` and ``resolve_locations`` on the Asset_Service.

A folder is metadata on the ASSET row, so a move rewrites one attribute: no bytes, no re-index. The property the
feature stands on is that nothing re-files a moved asset afterwards — a re-registration of the same location (a
re-harvest, an M2C job's late results) finds the id it already holds and writes nothing.
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from test_ingestion_asset_service import BUCKET, OWNER, PID, _event, ctx  # noqa: E402,F401
from ingestion.handlers import asset_service as svc  # noqa: E402
from projects import schema  # noqa: E402


def _asset(store, *, folder=None, bucket=BUCKET, key=None, name="clip.mp4", source="upload"):
    aid = f"video:{uuid.uuid4()}"
    key = key or f"projects/{PID}/{aid}/{name}"
    store.register_asset(
        pid=PID, asset_id=aid, source=source, source_uri=f"s3://{bucket}/{key}", source_bucket=bucket,
        source_key=key, source_size=1, content_type="video/mp4", file_name=name, added_by=OWNER,
        added_by_display=OWNER, folder=folder,
    )
    return aid


def _folder(store, aid):
    return store.get_asset(PID, aid).get("folder")


# ------------------------------------------------------------------ move_assets
def test_move_files_assets_and_the_root_clears_the_folder(ctx):
    service, store, _ = ctx
    a, b = _asset(store, folder="Sessions/Chat · 2026-10-15"), _asset(store)
    reply = service.handle(_event("move_assets", {"assetIds": [a, b], "folder": " Boards / Board 07 "}))
    assert reply["ok"] is True
    assert reply["value"] == {"folder": "Boards/Board 07", "moved": [a, b], "failed": []}
    assert _folder(store, a) == _folder(store, b) == "Boards/Board 07"   # normalized like every folder
    reply = service.handle(_event("move_assets", {"assetIds": [a]}))     # no folder: the root
    assert reply["value"]["moved"] == [a] and _folder(store, a) is None


def test_move_does_not_bump_row_version(ctx):
    """An annotation: the pipeline's version-conditioned writes must not fail because a user moved the file."""
    service, store, _ = ctx
    a = _asset(store)
    before = store.get_asset(PID, a).get("rowVersion")
    service.handle(_event("move_assets", {"assetIds": [a], "folder": "Day 1"}))
    assert store.get_asset(PID, a).get("rowVersion") == before


def test_a_missing_or_removed_asset_fails_alone(ctx):
    service, store, _ = ctx
    a, gone = _asset(store), _asset(store)
    store.remove_asset(PID, gone, removed_by=OWNER)
    ghost = f"video:{uuid.uuid4()}"
    reply = service.handle(_event("move_assets", {"assetIds": [a, gone, ghost], "folder": "Day 1"}))
    assert reply["value"]["moved"] == [a]
    assert reply["value"]["failed"] == [{"assetId": gone, "code": "asset_not_found"},
                                        {"assetId": ghost, "code": "asset_not_found"}]


@pytest.mark.parametrize("args, code", [
    ({"assetIds": [], "folder": "x"}, "invalid_args"),
    ({"assetIds": "video:x", "folder": "x"}, "invalid_args"),
    ({"assetIds": [f"video:{i}" for i in range(svc._MOVE_MAX + 1)]}, "too_many_ids"),
    ({"assetIds": ["video:x"], "folder": "a/../b"}, "invalid_folder"),
])
def test_move_refuses_bad_arguments(ctx, args, code):
    service, _, _ = ctx
    reply = service.handle(_event("move_assets", args))
    assert reply["ok"] is False and reply["status"] == 400 and reply["error"]["code"] == code


def test_a_moved_asset_stays_moved_when_its_location_is_registered_again(ctx):
    """The re-harvest case: the session registrar sees the same output again and must not re-file it."""
    service, store, _ = ctx
    key = "artifacts/sess-1/output/cut.mp4"
    a = _asset(store, key=key, folder="Sessions/Grip review · 2026-10-16", source="session")
    service.handle(_event("move_assets", {"assetIds": [a], "folder": "Boards/Board 07"}))
    again = store.register_asset(
        pid=PID, asset_id=a, source="session", source_uri=f"s3://{BUCKET}/{key}", source_bucket=BUCKET,
        source_key=key, source_size=1, content_type="video/mp4", file_name="cut.mp4", added_by=OWNER,
        added_by_display=OWNER, folder="Sessions/Grip review · 2026-10-16",
    )
    assert again.get("folder") == "Boards/Board 07" and _folder(store, a) == "Boards/Board 07"


# ------------------------------------------------------------------ rename_folder
def test_rename_moves_the_folder_and_everything_below_it(ctx):
    service, store, _ = ctx
    top, sub, other, sibling = (_asset(store, folder="Footage/Day 1"), _asset(store, folder="Footage/Day 1/Drone"),
                                _asset(store, folder="Footage/Day 10"), _asset(store, folder="Footage"))
    reply = service.handle(_event("rename_folder", {"from": "Footage/Day 1", "to": "Shoot/Day One"}))
    assert reply["ok"] is True and reply["value"]["remaining"] == 0
    assert sorted(reply["value"]["moved"]) == sorted([top, sub])
    assert _folder(store, top) == "Shoot/Day One"
    assert _folder(store, sub) == "Shoot/Day One/Drone"
    assert _folder(store, other) == "Footage/Day 10"      # a prefix of the NAME is not the folder
    assert _folder(store, sibling) == "Footage"


def test_rename_to_the_root_lifts_subfolders_a_level(ctx):
    service, store, _ = ctx
    top, sub = _asset(store, folder="Sessions"), _asset(store, folder="Sessions/Chat A")
    service.handle(_event("rename_folder", {"from": "Sessions", "to": ""}))
    assert _folder(store, top) is None and _folder(store, sub) == "Chat A"


def test_deleting_a_folder_merges_it_into_its_parent_and_reports_each_move_for_undo(ctx):
    """Delete folder = rename it onto its parent: its files come up a level, its subfolders survive, and the
    reply names every item's old and new folder, so Undo can put each back even though the parent already held
    files of its own."""
    service, store, _ = ctx
    own, inside, deeper = (_asset(store, folder="Footage"), _asset(store, folder="Footage/Day 1"),
                           _asset(store, folder="Footage/Day 1/Drone"))
    reply = service.handle(_event("rename_folder", {"from": "Footage/Day 1", "to": "Footage"}))["value"]
    assert _folder(store, inside) == "Footage" and _folder(store, deeper) == "Footage/Drone"
    assert _folder(store, own) == "Footage"
    assert sorted((m["assetId"], m["from"], m["to"]) for m in reply["moves"]) == sorted([
        (inside, "Footage/Day 1", "Footage"), (deeper, "Footage/Day 1/Drone", "Footage/Drone")])
    top = service.handle(_event("rename_folder", {"from": "Footage", "to": ""}))["value"]
    assert {m["to"] for m in top["moves"]} == {"", "Drone"}


def test_an_empty_folder_is_recorded_and_follows_a_rename_or_a_delete(ctx):
    """New folder is recorded so every device sees it while empty; renaming re-keys it, and deleting its parent
    lifts it a level like a file."""
    service, store, _ = ctx
    assert service.handle(_event("create_folder", {"folder": " Boards / Board 07 "}))["value"] == {"folder": "Boards/Board 07"}
    service.handle(_event("create_folder", {"folder": "Boards/Board 07"}))          # idempotent
    assert store.list_folder_rows(PID) == ["Boards/Board 07"]
    service.handle(_event("rename_folder", {"from": "Boards", "to": "Storyboards"}))
    assert store.list_folder_rows(PID) == ["Storyboards/Board 07"]
    service.handle(_event("rename_folder", {"from": "Storyboards", "to": ""}))        # delete a top-level folder
    assert store.list_folder_rows(PID) == ["Board 07"]
    service.handle(_event("rename_folder", {"from": "Board 07", "to": ""}))
    assert store.list_folder_rows(PID) == []
    bad = service.handle(_event("create_folder", {"folder": "Day #2"}))
    assert bad["status"] == 400 and bad["error"]["code"] == "invalid_folder"


def test_rename_refuses_a_move_into_itself_and_a_missing_source(ctx):
    service, _, _ = ctx
    for args in ({"from": "A", "to": "A/B"}, {"from": "A", "to": "A"}):
        reply = service.handle(_event("rename_folder", args))
        assert reply["status"] == 400 and reply["error"]["code"] == "invalid_folder"
    assert service.handle(_event("rename_folder", {"to": "B"}))["error"]["code"] == "invalid_args"


def test_rename_pages_and_reports_what_remains(ctx, monkeypatch):
    service, store, _ = ctx
    monkeypatch.setattr(svc, "_RENAME_PAGE_MAX", 2)
    ids = [_asset(store, folder="Old") for _ in range(3)]
    first = service.handle(_event("rename_folder", {"from": "Old", "to": "New"}))["value"]
    assert len(first["moved"]) == 2 and first["remaining"] == 1
    second = service.handle(_event("rename_folder", {"from": "Old", "to": "New"}))["value"]
    assert len(second["moved"]) == 1 and second["remaining"] == 0
    assert all(_folder(store, a) == "New" for a in ids)


# ------------------------------------------------------------------ resolve_locations
def test_resolve_locations_maps_chat_urls_to_their_assets(ctx):
    service, store, _ = ctx
    upload = _asset(store, name="frame.jpg")
    session = _asset(store, key=f"projects/{PID}/sessions/sess-1/output/sum.pdf", source="session")
    m2c = _asset(store, bucket="m2c-proxy", key="micro-workflow/edit/abc/transcoded.mp4", source="m2c")
    removed = _asset(store, key="artifacts/sess-2/output/old.mp4", source="session")
    store.remove_asset(PID, removed, removed_by=OWNER)
    locs = [
        f"https://d123.cloudfront.net/projects/{PID}/{upload}/frame.jpg",
        f"https://d123.cloudfront.net/projects/{PID}/sessions/sess-1/output/sum.pdf",
        "https://d123.cloudfront.net/artifacts/m2c/m2c-proxy/micro-workflow/edit/abc/transcoded.mp4",
        f"s3://{BUCKET}/artifacts/sess-2/output/old.mp4",
        "https://example.com/elsewhere.mp4",
    ]
    reply = service.handle(_event("resolve_locations", {"locations": locs}))
    assert reply["ok"] is True
    assert reply["value"]["locations"] == {locs[0]: upload, locs[1]: session, locs[2]: m2c, locs[3]: None,
                                           locs[4]: None}


def test_a_sessions_copy_of_a_job_output_resolves_to_the_jobs_asset(ctx):
    # A settled job's outputs are copied to <session output>/<execution id>/<file> so links stay live; the asset
    # stays where M2C wrote it. The copy (what a Renderer tab plays) must resolve to that asset (staging 2026-10-17:
    # a trimmed clip's timeline tab showed no location).
    service, store, _ = ctx
    exe = "f29bd1dc-4974-5fe6-accf-b596b66f35ca"
    clip = _asset(store, bucket="m2c-proxy", key=f"micro-workflow/media-processing/{exe}/clip/trim.mp4",
                  name="trim.mp4", source="m2c")
    other = _asset(store, bucket="m2c-proxy", key="micro-workflow/media-processing/zzz/clip/trim.mp4",
                   name="trim.mp4", source="m2c")
    store.put_job(PID, {"jobId": f"arn:aws:states:us-west-2:1:execution:m2c-media-processing:{exe}",
                        "outputs": [clip], "outputPrefix": f"s3://m2c-proxy/micro-workflow/media-processing/{exe}/clip/"})
    store.put_job(PID, {"jobId": "arn:aws:states:us-west-2:1:execution:m2c-shots:other-run-1234",
                        "outputs": [other], "outputPrefix": "s3://m2c-proxy/elsewhere/"})
    new_layout = f"https://d1.cloudfront.net/projects/{PID}/sessions/sess-1/output/{exe}/trim.mp4"
    legacy = f"s3://{BUCKET}/artifacts/sess-1/output/{exe}/trim.mp4"
    unknown_run = f"https://d1.cloudfront.net/projects/{PID}/sessions/sess-1/output/no-such-run-99/trim.mp4"
    reply = service.handle(_event("resolve_locations", {"locations": [new_layout, legacy, unknown_run]}))
    assert reply["value"]["locations"] == {new_layout: clip, legacy: clip, unknown_run: None}


def test_a_job_output_copy_falls_back_to_the_one_output_of_that_name(ctx):
    service, store, _ = ctx
    exe = "abcdef12-0000-4000-8000-000000000001"
    shots = _asset(store, bucket="m2c-proxy", key=f"mw/shots/{exe}/deep/segment_0000.mp4", name="segment_0000.mp4",
                   source="m2c")
    store.put_job(PID, {"jobId": f"arn:aws:states:us-west-2:1:execution:m2c-shots:{exe}", "outputs": [shots],
                        "outputPrefix": f"s3://m2c-proxy/mw/shots/{exe}/"})
    url = f"https://d1.cloudfront.net/projects/{PID}/sessions/s-1/output/{exe}/segment_0000.mp4"
    assert service.handle(_event("resolve_locations", {"locations": [url]}))["value"]["locations"] == {url: shots}


def test_resolve_locations_never_names_another_projects_asset(ctx):
    service, store, _ = ctx
    other_pid = "p" + "d" * 32
    aid = f"video:{uuid.uuid4()}"
    url = f"https://d123.cloudfront.net/projects/{other_pid}/{aid}/x.mp4"
    assert service.handle(_event("resolve_locations", {"locations": [url]}))["value"]["locations"] == {url: None}


# ------------------------------------------------------------------ migrate_chat_folders
def test_migration_moves_legacy_chat_roots_under_chats_once(ctx):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    import migrate_chat_folders as mig

    _, store, _ = ctx
    s = _asset(store, folder="Sessions/Promo · 2026-10-01/Shot Segmentation")
    g = _asset(store, folder="Generated/Promo · 2026-09-20")
    mine = _asset(store, folder="Footage/Sessions")       # a user folder that merely contains the word
    new = _asset(store, folder="Chats/Promo · 2026-10-15")
    dry = mig.run(store.table, execute=False, out=lambda *_: None)
    assert dry["legacyRows"] == 2 and _folder(store, s).startswith("Sessions/")   # a dry run writes nothing
    done = mig.run(store.table, execute=True, out=lambda *_: None)
    assert done["moved"] == 2
    assert _folder(store, s) == "Chats/Promo · 2026-10-01/Shot Segmentation"
    assert _folder(store, g) == "Chats/Promo · 2026-09-20"
    assert _folder(store, mine) == "Footage/Sessions" and _folder(store, new) == "Chats/Promo · 2026-10-15"
    assert mig.run(store.table, execute=True, out=lambda *_: None)["legacyRows"] == 0   # idempotent


# ------------------------------------------------------------------ bookmarks
def test_bookmarks_save_moments_in_a_folder_without_touching_the_asset(ctx):
    service, store, _ = ctx
    a = _asset(store, folder="Footage/Drone", name="DJI_0042.MOV")
    before = store.get_asset(PID, a)
    reply = service.handle(_event("create_bookmarks", {"folder": "Boards/Board 07", "items": [
        {"assetId": a, "startMs": 62000, "endMs": 65000, "label": "DJI_0042 · 1:02–1:05"},
        {"assetId": a, "startMs": 1000}]}))
    assert reply["ok"] is True
    created = reply["value"]["created"]
    assert [b["startMs"] for b in created] == [62000, 1000]
    assert created[0]["endMs"] == 65000 and "endMs" not in created[1]
    assert all(b["folder"] == "Boards/Board 07" and b["assetId"] == a and b["bookmarkId"].startswith("bookmark:")
               for b in created)
    assert created[0]["assetDisplayName"] == "DJI_0042.MOV" and created[0]["assetKind"] == "video"
    assert store.get_asset(PID, a) == before            # the asset is not moved, copied or changed
    assert len(store.list_bookmarks(PID)) == 2          # one take, two saved moments


def test_bookmarks_move_and_rename_like_files_and_delete_alone(ctx):
    service, store, _ = ctx
    a = _asset(store)
    b = service.handle(_event("create_bookmarks", {"folder": "Old/Sub", "items": [{"assetId": a, "startMs": 0}]}))
    bid = b["value"]["created"][0]["bookmarkId"]
    moved = service.handle(_event("move_assets", {"assetIds": [bid, a], "folder": "Old"}))
    assert sorted(moved["value"]["moved"]) == sorted([bid, a])
    service.handle(_event("rename_folder", {"from": "Old", "to": "New"}))
    assert store.list_bookmarks(PID)[0]["folder"] == "New" and _folder(store, a) == "New"
    gone = service.handle(_event("remove_bookmarks", {"bookmarkIds": [bid, "bookmark:" + str(uuid.uuid4())]}))
    assert gone["value"]["removed"] == [bid] and len(gone["value"]["missing"]) == 1
    assert store.list_bookmarks(PID) == [] and store.get_asset(PID, a) is not None


@pytest.mark.parametrize("item, code", [
    ({"startMs": -1}, "invalid_range"),
    ({"startMs": 5000, "endMs": 5000}, "invalid_range"),
    ({"startMs": True}, "invalid_range"),
])
def test_bookmarks_refuse_a_bad_range(ctx, item, code):
    service, store, _ = ctx
    a = _asset(store)
    reply = service.handle(_event("create_bookmarks", {"items": [{"assetId": a, **item}]}))
    assert reply["status"] == 400 and reply["error"]["code"] == code


def test_a_bookmark_of_a_removed_or_missing_asset_fails_alone(ctx):
    service, store, _ = ctx
    a, gone = _asset(store), _asset(store)
    store.remove_asset(PID, gone, removed_by=OWNER)
    reply = service.handle(_event("create_bookmarks", {"items": [
        {"assetId": a, "startMs": 0}, {"assetId": gone, "startMs": 0}, {"assetId": "video:nope", "startMs": 0}]}))
    assert len(reply["value"]["created"]) == 1
    assert [f["assetId"] for f in reply["value"]["failed"]] == [gone, "video:nope"]


def test_bookmarks_are_not_asset_rows():
    """Nothing that reads ASSET rows (ingestion, search, purge scans, the asset list GSI) can meet one."""
    from projects import schema as sch
    bid = "bookmark:" + str(uuid.uuid4())
    assert not sch.is_asset_id(bid) and sch.is_bookmark_id(bid)
    assert not sch.bookmark_key(PID, bid)["SK"].startswith(sch.ASSET_SK_PREFIX)


def test_resolve_locations_reads_a_presigned_url_of_an_asset_key(ctx):
    """What a Renderer asset-player tab's Copy/Save holds: a presigned GET of the asset's own key."""
    service, store, _ = ctx
    a = _asset(store, name="sam.png")
    url = f"https://{BUCKET}.s3.us-west-2.amazonaws.com/projects/{PID}/{a}/sam.png?X-Amz-Signature=abc&X-Amz-Expires=900"
    assert service.handle(_event("resolve_locations", {"locations": [url]}))["value"]["locations"] == {url: a}


def test_our_copy_resolves_to_the_asset_its_origin_names(ctx):
    # Staging 242794f0: a gallery of TTS chunks played our session COPIES; "Trim all marked" asked which project file
    # each copy is, matched none, and was handed to the agent as a 7-node plan (seven tabs). The copy records where it
    # came from (result_resolution.make_s3_importer), so it resolves to the asset registered there — even for a job
    # run inside a plan, which has no JOB record for the older heuristic to read.
    service, store, s3 = ctx
    origin_key = "micro-workflow/tts-on-aws/output/ca3d/ca3d_0_0_c0.wav"
    chunk = _asset(store, bucket="m2c-proxy", key=origin_key, name="ca3d_0_0_c0.wav", source="m2c")
    copy_key = f"projects/{PID}/sessions/sess-1/output/ca3d/ca3d_0_0_c0.wav"
    s3.put_object(Bucket=BUCKET, Key=copy_key, Body=b"RIFF", Metadata={"origin": f"s3://m2c-proxy/{origin_key}"})
    s3.put_object(Bucket=BUCKET, Key=f"projects/{PID}/sessions/sess-1/output/x/plain.wav", Body=b"RIFF")
    locs = [f"https://d1.cloudfront.net/{copy_key}", f"s3://{BUCKET}/{copy_key}",
            f"https://d1.cloudfront.net/projects/{PID}/sessions/sess-1/output/x/plain.wav"]
    reply = service.handle(_event("resolve_locations", {"locations": locs}))
    assert reply["value"]["locations"] == {locs[0]: chunk, locs[1]: chunk, locs[2]: None}


def test_keyframes_of_a_video_asset_from_its_index(ctx):
    # The timeline's snap targets: read from the MP4 index in byte ranges by the ONE reader (projects.keyframes).
    from pathlib import Path
    service, store, s3 = ctx
    svc._KEYFRAME_CACHE.clear()
    data = (Path(__file__).parent / "fixtures" / "mp4" / "kf-tail.mp4").read_bytes()
    key = f"projects/{PID}/v/kf.mp4"
    s3.put_object(Bucket=BUCKET, Key=key, Body=data)
    vid = _asset(store, key=key, name="kf.mp4")
    reply = service.handle(_event("keyframes", {"asset_id": vid}))
    assert reply["value"] == {"keyframes": [0.0, 0.8, 1.6, 2.4, 3.2]}
    s3.delete_object(Bucket=BUCKET, Key=key)                     # cached: the object's bytes never change
    assert service.handle(_event("keyframes", {"asset_id": vid}))["value"]["keyframes"] == [0.0, 0.8, 1.6, 2.4, 3.2]
    gone = service.handle(_event("keyframes", {"asset_id": "video:00000000-0000-4000-8000-000000000000"}))
    assert gone["ok"] is False and gone["status"] == 404
