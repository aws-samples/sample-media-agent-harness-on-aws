"""``project_search.project_tools`` — the six Project_Tools (ingestion design Component 17, Req 7.14, 10.2)."""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from project_search import project_tools as PT  # noqa: E402

PID = "p" + "a" * 32
AID = "video:6f1c0000-0000-4000-8000-000000000001"


class FakeLambda:
    def __init__(self, replies):
        self.replies, self.events = replies, []

    def invoke(self, FunctionName, Payload):
        ev = json.loads(Payload)
        self.events.append(ev)
        body = self.replies[ev["command"]](ev) if callable(self.replies[ev["command"]]) else self.replies[ev["command"]]
        return {"Payload": io.BytesIO(json.dumps(body).encode())}


def tools(fake):
    ts = PT.make_project_tools(PID, function_name="asset-fn", client=fake, token_source=lambda: "tok")
    return {t.tool_name: t for t in ts}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def turn(monkeypatch):
    state = {"turn": "t1"}
    monkeypatch.setattr(PT, "_request_ids", lambda: ("sess-1", state["turn"]))
    PT._PENDING.clear()
    return state


def test_no_asset_service_binds_nothing(monkeypatch):
    monkeypatch.delenv("ASSET_SERVICE_FN", raising=False)
    assert PT.make_project_tools(PID) == []


def test_tool_names_and_no_project_argument():
    ts = tools(FakeLambda({}))
    assert tuple(ts) == PT.TOOL_NAMES
    for t in ts.values():
        props = t.tool_spec["inputSchema"]["json"]["properties"]
        assert not {"project_id", "projectId", "collection_id"} & set(props)


def test_project_status_forwards_the_turn_project_and_token():
    fake = FakeLambda({"project_status": {"ok": True, "value": {"counts": {"failed": 1}, "collectionState": "ready"}}})
    out = run(tools(fake)["project_status"]())
    assert out == {"counts": {"failed": 1}, "collectionState": "ready"}  # no name: the project-wide view as is
    run(tools(fake)["project_status"](name="clip"))
    ev = fake.events[-1]
    assert (ev["projectId"], ev["userToken"], ev["caller"], ev["args"]) == (PID, "tok", "main_agent", {"name": "clip"})
    assert ev["sessionId"] == "sess-1" and ev["turnId"] == "t1"


def _raw_row(asset_id, name, kind, status, **extra):
    """A full Asset_Service match row, with the raw fields the model must not see."""
    return {"assetId": asset_id, "displayName": name, "kind": kind, "status": status,
            "sourceUri": f"s3://ingest/projects/{PID}/{name}", "originUri": f"s3://up/{name}",
            "chatCopyKeys": [f"artifacts/other-session/{name}"], "doc": {"k": "v"}, "run": {"id": "r1"},
            "index": {"docs": 4}, "contentType": "video/mp4", "addedBy": "u-1",
            "steps": {"shot_segmentation": {"facts": {"durationMs": 31000},
                                            "outputs": {"shots": f"s3://proxy/{name}.json"}}},
            **extra}


def test_project_status_with_name_returns_only_slim_matches_and_their_counts():
    value = {
        "counts": {"indexed": 40, "failed": 1, "removed": 1}, "indexing": True, "collectionState": "rebuilding",
        "failed": [{"assetId": "video:x", "displayName": "meridian_3d_reel.mp4", "reason": "codec"}],
        "failedNotListed": 0,
        "matches": [
            _raw_row("video:1", "tidal_a.mp4", "video", "indexed"),
            _raw_row("video:2", "tidal_b.mp4", "video", "failed", failure={"reason": "no audio", "step": "x"}),
            {"assetId": "image:3", "displayName": "tidal.png", "kind": "image", "status": "indexed",
             "sourceUri": "s3://ingest/tidal.png"},
        ],
    }
    out = run(tools(FakeLambda({"project_status": {"ok": True, "value": value}}))["project_status"](name="tidal"))
    assert out == {
        "matches": [
            {"assetId": "video:1", "displayName": "tidal_a.mp4", "kind": "video", "status": "indexed",
             "durationMs": 31000},
            {"assetId": "video:2", "displayName": "tidal_b.mp4", "kind": "video", "status": "failed",
             "durationMs": 31000, "failure": {"reason": "no audio"}},
            {"assetId": "image:3", "displayName": "tidal.png", "kind": "image", "status": "indexed"},
        ],
        "matchCounts": {"byKind": {"video": 2, "image": 1}, "byStatus": {"indexed": 2, "failed": 1}},
    }
    # Nothing project-wide, and no URI of any kind, reaches the model on a name lookup.
    text = json.dumps(out)
    assert "s3://" not in text and "artifacts/" not in text and "meridian" not in text and "rebuilding" not in text


def test_project_status_name_lookup_passes_a_refusal_through():
    fake = FakeLambda({"project_status": {"ok": False, "status": 400,
                                          "error": {"code": "invalid_args", "message": "name must be 1-255"}}})
    assert run(tools(fake)["project_status"](name="x")) == {"error": "invalid_args", "message": "name must be 1-255"}


def test_project_status_docstring_scopes_a_name_answer_to_its_matches():
    desc = " ".join(tools(FakeLambda({}))["project_status"].tool_spec["description"].split())
    assert ("When name is given, answer from matches only (count them by kind) in one or two sentences; mention a "
            "failure only if it is one of the matches; do not mention removed files, other projects, or the "
            "collection state unless the user asks about search or ingestion.") in desc
    assert "Without name, report every count, the collection state when it is not ready, and each failure." in desc


def test_refusals_reach_the_model_as_error_and_message():
    fake = FakeLambda({"retry": {"ok": False, "status": 409, "error": {
        "code": "not_failed", "message": "Only a failed asset can be retried; it is indexed"}}})
    assert run(tools(fake)["retry_asset"](asset_id=AID)) == {
        "error": "not_failed", "message": "Only a failed asset can be retried; it is indexed"}


def test_remove_needs_a_confirmation_from_a_later_turn(turn):
    resolve = {"ok": True, "value": {"assets": {AID: {"kind": "video", "displayName": "cut.mp4", "status": "indexed"}}}}
    fake = FakeLambda({"resolve_assets": resolve, "remove": {"ok": True, "value": {"changed": True}}})
    t = tools(fake)
    first = run(t["remove_asset"](asset_id=AID))
    assert first["needs_confirmation"] is True and first["display_name"] == "cut.mp4"
    assert [e["command"] for e in fake.events] == ["resolve_assets"]  # nothing removed yet
    same_turn = run(t["remove_asset"](asset_id=AID, confirmation=first["confirmation"]))
    assert same_turn["error"] == "confirmation_too_early"
    assert run(t["remove_asset"](asset_id=AID, confirmation="bogus"))["error"] == "confirmation_invalid"
    turn["turn"] = "t2"
    done = run(t["remove_asset"](asset_id=AID, confirmation=first["confirmation"]))
    assert done == {"removed": True, "display_name": "cut.mp4"}
    assert fake.events[-1]["command"] == "remove" and fake.events[-1]["args"] == {"assetId": AID}
    # The code is single-use.
    assert run(t["remove_asset"](asset_id=AID, confirmation=first["confirmation"]))["error"] == "confirmation_invalid"


def test_remove_unknown_asset():
    fake = FakeLambda({"resolve_assets": {"ok": True, "value": {"assets": {}}}})
    assert run(tools(fake)["remove_asset"](asset_id=AID))["error"] == "asset_not_found"


def test_invoke_failure_is_a_plain_refusal():
    class Broken:
        def invoke(self, **kw):
            raise TimeoutError("slow")
    out = run(tools(Broken())["readd_asset"](asset_id=AID))
    assert out["error"] == "unavailable"


# ------------------------------------------------------------------ show_asset

IMG = "image:6f1c0000-0000-4000-8000-0000000000aa"


@pytest.fixture
def renderer(monkeypatch):
    """Capture manifest writes and progress pushes; no S3, no network."""
    from orchestration.progress_channel import ProgressChannel, set_active_channel
    from render import artifacts

    writes = []

    def _write(session_id, manifest_id, manifest):
        writes.append((session_id, manifest_id, manifest))
        return f"artifacts/{session_id}/render/{manifest_id}.json"

    monkeypatch.setattr(artifacts, "write_session_manifest", _write)
    ch = ProgressChannel()
    set_active_channel(ch)
    try:
        yield writes, ch
    finally:
        set_active_channel(None)


def _resolving(asset):
    return FakeLambda({"resolve_assets": {"ok": True, "value": {"assets": {IMG: asset} if asset else {}}}})


def _strings(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v)
    elif isinstance(obj, str):
        yield obj


def test_show_asset_writes_an_asset_player_manifest_and_opens_one_tab(renderer):
    import hashlib

    writes, ch = renderer
    fake = _resolving({"kind": "image", "displayName": "sam.png", "status": "indexed"})
    out = run(tools(fake)["show_asset"](asset_id=IMG))
    assert out == {"shown": True, "display_name": "sam.png"}
    assert fake.events[0]["command"] == "resolve_assets" and fake.events[0]["args"] == {"assetIds": [IMG]}

    [(sid, mid, manifest)] = writes
    expected_id = "asset-" + hashlib.sha256(f"sess-1|{IMG}".encode()).hexdigest()[:24]
    assert (sid, mid) == ("sess-1", expected_id)
    created = manifest.pop("createdAt")
    assert created.endswith("+00:00")
    assert manifest == {
        "id": expected_id, "template": "asset-player", "title": "sam.png",
        "ref": {"project_id": PID, "asset_id": IMG, "frame_s3_key": None},
        "kind": "image", "displayName": "sam.png", "startMs": 0, "origin": "agent",
    }
    # No URL anywhere: the SPA presigns at view time.
    for s in _strings(manifest):
        assert s not in {"url", "src"} and "https://" not in s and "http://" not in s and "s3://" not in s

    events = ch.drain()
    rendered = [e for e in events if e.extra.get("render_manifest_key")]
    assert len(rendered) == 1 and len(events) == 1
    e = rendered[0]
    assert (e.status, e.label, e.agent) == ("done", "↳ sam.png", "render")
    assert e.extra == {"render_manifest_key": f"artifacts/sess-1/render/{expected_id}.json",
                       "render_request_id": expected_id, "render_label": "sam.png"}


@pytest.mark.parametrize("asset,error", [
    (None, "asset_not_found"),
    ({"kind": "image", "displayName": "sam.png", "status": "removed"}, "asset_removed"),
    ({"kind": "data", "displayName": "shots.json", "status": "stored"}, "not_viewable"),
])
def test_show_asset_refusals_write_and_open_nothing(renderer, asset, error):
    writes, ch = renderer
    out = run(tools(_resolving(asset))["show_asset"](asset_id=IMG))
    assert out["error"] == error and out["message"]
    assert writes == [] and ch.drain() == []


def test_show_asset_service_refusal_writes_nothing(renderer):
    writes, ch = renderer
    fake = FakeLambda({"resolve_assets": {"ok": False, "status": 404,
                                          "error": {"code": "project_not_found", "message": "Project not found"}}})
    assert run(tools(fake)["show_asset"](asset_id=IMG)) == {"error": "project_not_found",
                                                             "message": "Project not found"}
    assert writes == [] and ch.drain() == []


def test_show_asset_is_a_project_tool_taking_only_asset_id():
    assert "show_asset" in PT.TOOL_NAMES
    spec = tools(FakeLambda({}))["show_asset"].tool_spec
    assert set(spec["inputSchema"]["json"]["properties"]) == {"asset_id"}
    assert spec["inputSchema"]["json"].get("required") == ["asset_id"]
    desc = " ".join(spec["description"].split())
    assert ("Use this ONLY when the user explicitly asks to open, play or show a project file" in desc
            and "Never use it to answer a search or a question about the media's content" in desc)


def test_project_status_name_match_carries_the_ingestion_probe_summary():
    """Staging session 7e92b95d: "what is the encoder and resolution" took 55 steps because the probe the
    frame-capture job already made never reached the model. A probed file now answers it from `technical`."""
    tech = {"container": "mov,mp4,m4a,3gp,3g2,mj2", "encoder": "Lavf61.7.100",
            "video": {"codec": "h264", "profile": "High", "width": 1920, "height": 1080, "fps": 24.0}}
    row = _raw_row("video:1", "tidal_a.mp4", "video", "indexed")
    row["steps"]["frame capture"] = {"facts": {"technical": tech}, "outputs": {"frames": "s3://proxy/f.json"}}
    value = {"matches": [row, _raw_row("video:2", "tidal_b.mp4", "video", "queued")]}
    out = run(tools(FakeLambda({"project_status": {"ok": True, "value": value}}))["project_status"](name="tidal"))
    assert out["matches"][0]["technical"] == tech
    assert "technical" not in out["matches"][1]
    assert "s3://" not in json.dumps(out)
    desc = " ".join(tools(FakeLambda({}))["project_status"].tool_spec["description"].split())
    assert "Answer codec, resolution, frame rate or bitrate questions about a project file from it" in desc


# --------------------------------------------------------------------------------------------- browse_project
def _browse_tools(name="Storyboards"):
    ts = PT.make_project_tools(PID, function_name="asset-fn", client=FakeLambda({}), token_source=lambda: "tok",
                               project_name=name)
    return {t.tool_name: t for t in ts}


def test_browse_project_writes_a_project_browser_manifest_and_opens_one_tab(renderer):
    writes, ch = renderer
    out = run(_browse_tools()["browse_project"](query="meridian", kind="Video"))
    assert out == {"opened": True, "project": "Storyboards", "filter": {"q": "meridian", "kind": "video"}}
    [(sid, mid, manifest)] = writes
    assert sid == "sess-1" and mid == manifest["id"] and mid.startswith("browse-")
    manifest.pop("createdAt")
    assert manifest == {
        "id": mid, "template": "project-browser", "title": "Browse · Storyboards",
        "project_id": PID, "project_name": "Storyboards",
        "filter": {"q": "meridian", "folder": "", "kind": "video"}, "origin": "agent",
    }
    for s in _strings(manifest):
        assert "https://" not in s and "s3://" not in s
    [e] = ch.drain()
    assert e.extra == {"render_manifest_key": f"artifacts/sess-1/render/{mid}.json",
                       "render_request_id": mid, "render_label": "Browse · Storyboards"}


def test_browse_project_id_is_deterministic_per_session_and_filter(renderer):
    writes, _ = renderer
    t = _browse_tools()["browse_project"]
    run(t())
    run(t())
    run(t(kind="image"))
    ids = [w[1] for w in writes]
    assert ids[0] == ids[1] and ids[2] != ids[0]


def test_browse_project_project_comes_from_the_closure_not_the_model():
    spec = _browse_tools()["browse_project"].tool_spec
    props = spec["inputSchema"]["json"]["properties"]
    assert set(props) == {"query", "folder", "kind"}
    assert not spec["inputSchema"]["json"].get("required")
    desc = " ".join(spec["description"].split())
    assert "browsable grid in a Renderer tab" in desc and "use show_asset instead" in desc
    assert "browse_project" in PT.TOOL_NAMES


def test_browse_project_refuses_an_unknown_kind_and_writes_nothing(renderer):
    writes, ch = renderer
    out = run(_browse_tools()["browse_project"](kind="spreadsheet"))
    assert out["error"] == "invalid_kind" and writes == [] and ch.drain() == []


def test_browse_project_without_a_session_opens_nothing(renderer, monkeypatch):
    writes, ch = renderer
    monkeypatch.setattr(PT, "_request_ids", lambda: ("", ""))
    assert run(_browse_tools()["browse_project"]())["error"] == "browse_unavailable"
    assert writes == [] and ch.drain() == []


DOC_ID = "document:1428cc1a-dca3-408f-9e1c-7dff957839f0"


def test_show_asset_opens_a_document(renderer):
    """Staging session 853bdc11: "find the compliance report for Meridian" found the PDF, then show_asset refused
    it ("the viewer can't open it directly"). A document opens as an asset-player tab (an embedded PDF)."""
    writes, ch = renderer
    fake = FakeLambda({"resolve_assets": {"ok": True, "value": {"assets": {DOC_ID: {
        "kind": "document", "displayName": "report.pdf", "status": "indexed"}}}}})
    out = run(tools(fake)["show_asset"](asset_id=DOC_ID))
    assert out == {"shown": True, "display_name": "report.pdf"}
    [(_sid, _mid, manifest)] = writes
    assert (manifest["template"], manifest["kind"], manifest["startMs"]) == ("asset-player", "document", 0)
    assert len(ch.drain()) == 1


# Session 34ca292e: asked to restyle a project image, the agent opened it, then guessed S3 keys in the sandbox
# (AccessDenied) and asked the user for a link. `use_asset` gives it the file the way attaching it would.
def _use(fake, monkeypatch, domain="d123.cloudfront.net", bucket="store-bucket"):
    monkeypatch.setenv("ARTIFACTS_DOMAIN", domain)
    monkeypatch.setenv("SKILLS_S3_BUCKET", bucket)
    return run(tools(fake)["use_asset"](asset_id=IMG))


def _recording(row):
    return FakeLambda({"record_chat_session": lambda ev: {"ok": True, "value": {"asset": row}}})


def test_use_asset_records_the_chat_and_returns_a_signable_identifier(monkeypatch):
    fake = _recording({"assetId": IMG, "kind": "image", "displayName": "sam.png", "status": "indexed",
                       "sourceUri": f"s3://store-bucket/projects/{PID}/{IMG}/sam.png"})
    out = _use(fake, monkeypatch)
    assert fake.events[0]["command"] == "record_chat_session"
    assert fake.events[0]["args"] == {"assetId": IMG, "sessionId": "sess-1"}
    assert out == {"asset_id": IMG, "display_name": "sam.png", "kind": "image",
                   "source_uri": f"s3://store-bucket/projects/{PID}/{IMG}/sam.png",
                   "source_url": f"https://d123.cloudfront.net/projects/{PID}/{IMG}/sam.png"}
    assert "X-Amz" not in json.dumps(out)  # an identifier, never a presigned URL


def test_use_asset_source_url_is_what_the_tool_wrapper_signs_for_the_trusted_project(monkeypatch):
    from media import artifact_fetch as af

    fake = _recording({"kind": "image", "displayName": "g.png", "status": "indexed",
                       "sourceUri": "s3://store-bucket/artifacts/external/0123456789abcdef01234567.png"})
    url = _use(fake, monkeypatch)["source_url"]
    assert af.own_artifact_key(url) == "artifacts/external/0123456789abcdef01234567.png"
    upload = f"https://d123.cloudfront.net/projects/{PID}/{IMG}/sam.png"
    assert af.trusted_project_asset_key(upload, PID) == f"projects/{PID}/{IMG}/sam.png"
    assert af.trusted_project_asset_key(upload, "p" + "b" * 32) is None  # another project's file: never signed


def test_use_asset_outside_our_storage_gives_only_the_s3_uri(monkeypatch):
    monkeypatch.setenv("MEDIA_PROXY_BUCKET", "m2c-proxy-bucket")
    fake = _recording({"kind": "audio", "displayName": "SPEAKER_00.wav", "status": "stored",
                       "sourceUri": "s3://someone-elses-bucket/output/x/SPEAKER_00.wav"})
    out = _use(fake, monkeypatch)
    assert out["source_uri"] == "s3://someone-elses-bucket/output/x/SPEAKER_00.wav" and "source_url" not in out


# Staging 2026-10-15: given only `s3://<m2c-proxy>/micro-workflow/.../transcoded.mp4`, the agent linked
# `https://<ARTIFACTS_DOMAIN>/micro-workflow/...` -- the proxy-bucket key grafted onto our CDN, no object, 403.
def test_use_asset_m2c_output_gets_its_m2c_reference(monkeypatch):
    from media import artifact_fetch as af

    monkeypatch.setenv("MEDIA_PROXY_BUCKET", "m2c-proxy-bucket")
    key = "micro-workflow/media-processing/fc08a4a0/edit/transcoded.mp4"
    fake = _recording({"kind": "video", "displayName": "transcoded.mp4", "status": "stored",
                       "sourceUri": f"s3://m2c-proxy-bucket/{key}"})
    out = _use(fake, monkeypatch)
    assert out["source_url"] == f"https://d123.cloudfront.net/artifacts/m2c/m2c-proxy-bucket/{key}"
    # The tool wrapper signs it IN the proxy bucket (bytes never copied), not in ours.
    assert af.parse_m2c_artifact_ref(out["source_url"]) == ("m2c-proxy-bucket", key)
    assert af.canonical_location(out["source_url"]) == f"s3://m2c-proxy-bucket/{key}"


def test_use_asset_refusals(monkeypatch):
    removed = _recording({"kind": "image", "displayName": "sam.png", "status": "removed", "sourceUri": "s3://b/k"})
    assert _use(removed, monkeypatch)["error"] == "asset_removed"
    denied = FakeLambda({"record_chat_session": {"ok": False, "status": 404,
                                                 "error": {"code": "asset_not_found", "message": "Asset not found"}}})
    assert _use(denied, monkeypatch)["error"] == "asset_not_found"


def test_every_project_tool_is_kept_out_of_the_render_harvest():
    """A project tool's reply names files (use_asset returns a URL): harvesting it would open a stray tab."""
    from hooks import provider

    assert set(PT.TOOL_NAMES) <= provider.NON_ARTIFACT_TOOLS


# ------------------------------------------------------------------ organize_project
def _organize(fake, **kw):
    return run(tools(fake)["organize_project"](**kw))


def test_organize_moves_files_and_saves_moments_as_bookmarks():
    fake = FakeLambda({
        "move_assets": lambda ev: {"ok": True, "value": {"folder": "Boards/Board 07", "moved": ev["args"]["assetIds"], "failed": []}},
        "create_bookmarks": lambda ev: {"ok": True, "value": {"folder": "Boards/Board 07",
                                                              "created": [{"bookmarkId": "bookmark:x"}] * len(ev["args"]["items"]), "failed": []}},
    })
    out = _organize(fake, folder="Boards/Board 07", asset_ids=[IMG],
                    moments=[{"asset_id": IMG, "start_ms": 62000, "end_ms": 65000, "label": "grip"}])
    assert out == {"folder": "Boards/Board 07", "moved": {"count": 1, "failed": []}, "bookmarked": {"count": 1, "failed": []}}
    move, mark = fake.events
    assert move["command"] == "move_assets" and move["args"] == {"assetIds": [IMG], "folder": "Boards/Board 07"}
    assert mark["command"] == "create_bookmarks"
    assert mark["args"] == {"items": [{"assetId": IMG, "startMs": 62000, "endMs": 65000, "label": "grip"}],
                            "folder": "Boards/Board 07", "sessionId": "sess-1"}


def test_organize_asks_first_for_a_large_move_or_a_rename():
    fake = FakeLambda({"move_assets": {"ok": True, "value": {"folder": "X", "moved": [], "failed": []}},
                       "rename_folder": {"ok": True, "value": {"moved": ["a"], "failed": [], "remaining": 0}}})
    many = [f"video:{i}" for i in range(PT.ORGANIZE_CONFIRM_OVER + 1)]
    assert _organize(fake, folder="X", asset_ids=many)["needs_confirmation"] is True
    assert _organize(fake, folder="New", rename_from="Old")["needs_confirmation"] is True
    assert fake.events == []                                  # nothing happened before the user agreed
    out = _organize(fake, folder="New", rename_from="Old", confirmed=True)
    assert out["renamed"] == {"from": "Old", "moved": 1, "failed": [], "remaining": 0}
    assert _organize(fake, folder="X")["error"] == "nothing_to_do"


def test_organize_creates_an_empty_folder_tree_in_one_call():
    """A bin structure from a screenshot: every path created empty, under `folder`, parents implied."""
    fake = FakeLambda({"create_folder": lambda ev: {"ok": True, "value": {"folder": ev["args"]["folder"]}}})
    out = _organize(fake, folders=["AUDIO/MUSIC", "AUDIO/SFX", " FOOTAGE/STOCK FOOTAGE/Licensed/ ", "AUDIO/MUSIC"])
    assert [e["args"]["folder"] for e in fake.events] == ["AUDIO/MUSIC", "AUDIO/SFX", "FOOTAGE/STOCK FOOTAGE/Licensed"]
    assert out["created"] == {"folders": ["AUDIO/MUSIC", "AUDIO/SFX", "FOOTAGE/STOCK FOOTAGE/Licensed"], "failed": []}
    nested = FakeLambda({"create_folder": lambda ev: {"ok": True, "value": {"folder": ev["args"]["folder"]}}})
    _organize(nested, folder="Show", folders=["GFX/LOGO"])
    assert nested.events[0]["args"] == {"folder": "Show/GFX/LOGO"}


def test_organize_project_is_in_the_turn_prompt_and_its_docstring_names_each_mode():
    from project_search import turn as T
    assert "organize_project" in T._ORGANIZE_LINE
    spec = tools(FakeLambda({}))["organize_project"].tool_spec["description"]
    for word in ("folders", "asset_ids", "moments", "rename_from", "bookmark", "confirmed"):
        assert word in spec
