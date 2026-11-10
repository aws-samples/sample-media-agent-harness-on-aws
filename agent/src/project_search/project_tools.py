"""Project_Tools: the Main_Agent's handles on the Active_Project's assets (ingestion design Component 17).

``@tool async def`` functions bound per turn, only on the Main_Agent and only when the turn has an
Active_Project and ``ASSET_SERVICE_FN`` is configured:

| Tool | Asset_Service command |
|---|---|
| ``project_status(name?)`` | ``project_status`` |
| ``register_s3_location(location)`` | ``register_s3_location`` |
| ``retry_asset(asset_id)`` | ``retry`` |
| ``remove_asset(asset_id, confirmation?)`` | ``remove`` |
| ``readd_asset(asset_id)`` | ``readd`` |
| ``show_asset(asset_id)`` | ``resolve_assets`` (then an ``asset-player`` manifest) |
| ``browse_project(query?, folder?, kind?)`` | none (a ``project-browser`` manifest) |

The project-level tools work on the user's project LIST, not the Active_Project, through the proxy's own
``/projects`` API (:mod:`.registry_client`, the same routes and rules as the browser):

| Tool | ``/projects`` call | Then |
|---|---|---|
| ``list_projects()`` | ``GET /projects`` | ids stay out of the reply; ``current`` marks this chat's project |
| ``switch_project(name, prompt?)`` | ``GET /projects`` | ``open_project`` on the turn's summary (:mod:`.turn_actions`) |
| ``create_project(name, …, prompt?)`` | ``POST /projects`` | ``open_project``, as above |
| ``delete_project(name)`` | ``GET /projects`` | a ``project:delete`` card; only the user's typed-name confirmation deletes |

``open_project`` makes the chat open a NEW chat in that project and send it ``prompt``: a chat's project is fixed
by its first turn, so nothing of this chat moves.

``project_status(name=...)`` returns name-scoped facts only (:func:`_name_lookup`): the Asset_Service also
replies with project-wide counts, failures and the collection state, which drowned a "how many X files"
answer in unrelated caveats (session 07703a35), and full asset rows whose raw fields the model has no use for.

``show_asset`` opens a Project_Asset in the Renderer the way a Citation does: an ``asset-player`` manifest
holding an Asset_Reference and NO URL (the SPA presigns at view time), announced on the progress channel like
the Moments tab. A link in the reply opens nothing, and a remembered URL may belong to another session.

``browse_project`` opens the Active_Project's file grid as a Renderer tab (a ``project-browser`` manifest: the
project id and an optional starting filter, no asset rows): the user browses and filters there, and opening a
media file from it adds an ``asset-player`` tab. It reads nothing itself; the SPA lists the assets as the user.

The project id comes from the turn (a closure), never from the model; no tool takes a project id, a
``collection_id``, a workflow name, or document content (Req 3.2, 4.17). The Asset_Service verifies the
forwarded User_Token and the membership on every call. Each call runs ``lambda.invoke`` in a worker thread so
the tool never blocks the event loop (AGENTS.md, "An async @tool on the orchestrator must not block").

**Removal confirmation (Req 10.2).** ``remove_asset`` without ``confirmation`` changes nothing: it returns
``{needs_confirmation, display_name, confirmation}`` and records the code for this session and turn. The code is
accepted only in a LATER turn of the same session, so at least one user message always sits between the question
and the removal. A lost map (recycled microVM) only makes the model ask again.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import quote

from strands import tool

from .assets import ASSET_SERVICE_FN_ENV, CALLER, _default_token_source, _request_ids
from . import turn_actions
from .compact import OPENABLE_KINDS
from .registry_client import RegistryClient

logger = logging.getLogger(__name__)

TOOL_NAMES = ("project_status", "register_s3_location", "retry_asset", "remove_asset", "readd_asset",
              "show_asset", "use_asset", "browse_project", "organize_project", "list_projects", "switch_project",
              "create_project", "delete_project")

# Above this many items (or for any folder rename) organize_project asks for the user's go-ahead first: a move
# changes where the whole team finds the files.
ORGANIZE_CONFIRM_OVER = 10

# Filters the project grid understands (ProjectAssetsSection's type menu).
_BROWSE_KINDS = ("video", "image", "audio", "document", "data")

# session id → {code: (asset_id, display_name, turn_id)}; bounded per session.
_PENDING: dict[str, dict[str, tuple[str, str, str]]] = {}
_PENDING_LOCK = threading.Lock()
_PENDING_PER_SESSION = 20


class _Invoker:
    def __init__(self, function_name: str, client: Any = None,
                 token_source: Callable[[], str] = _default_token_source):
        self.function_name = function_name
        self._client = client
        self._token = token_source

    def _lambda(self) -> Any:
        if self._client is None:
            import boto3
            from botocore.config import Config

            self._client = boto3.client("lambda", region_name=os.environ.get("AWS_REGION", "us-west-2"),
                                        config=Config(connect_timeout=2, read_timeout=80,
                                                      retries={"max_attempts": 2, "mode": "standard"}))
        return self._client

    def call(self, project_id: str, command: str, args: dict) -> dict:
        """The Asset_Service reply, or ``{ok: false, ...}`` for a failed invoke (never raises)."""
        event: dict = {"userToken": self._token() or "", "projectId": project_id, "command": command,
                       "args": args, "caller": CALLER}
        session_id, turn_id = _request_ids()
        if session_id:
            event["sessionId"] = session_id
        if turn_id:
            event["turnId"] = turn_id
        try:
            resp = self._lambda().invoke(FunctionName=self.function_name, Payload=json.dumps(event).encode())
            if resp.get("FunctionError"):
                return {"ok": False, "status": 503, "error": {"code": "unavailable", "message": "Try again"}}
            body = json.loads(resp["Payload"].read())
            return body if isinstance(body, dict) else {"ok": False, "status": 503,
                                                         "error": {"code": "unavailable", "message": "Try again"}}
        except Exception as exc:  # noqa: BLE001 — the model gets a plain refusal, not a stack
            return {"ok": False, "status": 503,
                    "error": {"code": "unavailable", "message": f"The project service is unavailable ({type(exc).__name__})"}}


def asset_source_refs(source_uri: Any) -> dict:
    """`source_uri` (s3://) and, for an object in the harness's own storage, `source_url`: its short stable
    identifier ``https://<ARTIFACTS_DOMAIN>/<key>`` -- the form the tool wrapper presigns at call time
    (``media.artifact_fetch.presign_for_trusted_turn``: an artifact key, or an asset key of the turn's trusted
    project). Never a presigned URL: those are ~1.5 KB and a model truncates them (session e2fb2692).
    An M2C output (an object in the M2C proxy bucket) gets its M2C_REFERENCE,
    ``https://<ARTIFACTS_DOMAIN>/artifacts/m2c/<bucket>/<key>``: the same wrapper presigns it in the proxy
    bucket, and the chat resolves it through the proxy's project-membership check. Without it the model had
    only the s3:// form and grafted the bare key onto the CDN domain, a link to no object (403).
    """
    if not isinstance(source_uri, str) or not source_uri.startswith("s3://"):
        return {}
    bucket, _, key = source_uri[len("s3://"):].partition("/")
    out: dict = {"source_uri": source_uri}
    from media.artifact_fetch import artifacts_bucket, artifacts_domain, m2c_artifact_ref  # noqa: PLC0415

    domain, own = artifacts_domain(), artifacts_bucket()
    proxy = os.environ.get("MEDIA_PROXY_BUCKET", "")
    if key and domain and own and bucket == own and key.startswith(("artifacts/", "projects/")):
        out["source_url"] = f"https://{domain}/{key}"
    elif key and domain and proxy and bucket == proxy:
        ref = m2c_artifact_ref(bucket, key)
        if ref:
            out["source_url"] = f"https://{domain}/{quote(ref, safe='/')}"
    return out


def _reply(body: dict) -> dict:
    """Asset_Service envelope → what the model reads: the value, or ``{error, message}``."""
    if body.get("ok"):
        return body.get("value") or {}
    err = body.get("error") or {}
    return {"error": err.get("code") or "error", "message": err.get("message") or "The request failed"}


def _duration_ms(row: dict) -> Optional[int]:
    """The asset's duration when the row records one (top level, else a step's facts), or ``None``."""
    candidates = [row.get("durationMs")]
    steps = row.get("steps")
    if isinstance(steps, dict):
        candidates += [(s.get("facts") or {}).get("durationMs") for s in steps.values() if isinstance(s, dict)]
    for v in candidates:
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            return int(v)
    return None


def _technical(row: dict) -> Optional[dict]:
    """The ingestion probe summary (``facts.technical`` of a step: codec, resolution, fps, …), or ``None``."""
    steps = row.get("steps")
    if isinstance(steps, dict):
        for s in steps.values():
            tech = (s.get("facts") or {}).get("technical") if isinstance(s, dict) else None
            if isinstance(tech, dict) and tech:
                return tech
    return None


def _slim_match(row: dict) -> dict:
    """A name match as the model sees it: an allowlist, so raw fields (steps, URIs, run state) never leak."""
    out = {k: row.get(k) for k in ("assetId", "displayName", "kind", "status")}
    folder = row.get("folder")
    if isinstance(folder, str) and folder:
        out["folder"] = folder
    dur = _duration_ms(row)
    if dur is not None:
        out["durationMs"] = dur
    tech = _technical(row)
    if tech is not None:
        out["technical"] = tech
    if row.get("status") == "failed":
        out["failure"] = {"reason": (row.get("failure") or {}).get("reason")}
    if row.get("status") == "stored" and row.get("indexSkippedReason"):
        # Kept but deliberately not indexed (e.g. an audio stem with no speech) -- not a failure.
        out["indexSkippedReason"] = row.get("indexSkippedReason")
    return out


def _name_lookup(value: dict) -> dict:
    """``project_status(name=...)`` → the matches and their counts, without the project-wide facts."""
    matches = [_slim_match(r) for r in (value.get("matches") or []) if isinstance(r, dict)]
    by_kind: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for m in matches:
        by_kind[str(m["kind"])] = by_kind.get(str(m["kind"]), 0) + 1
        by_status[str(m["status"])] = by_status.get(str(m["status"]), 0) + 1
    return {"matches": matches, "matchCounts": {"byKind": by_kind, "byStatus": by_status}}


def _asset_player_manifest(session_id: str, project_id: str, asset_id: str, kind: str,
                           display_name: str) -> dict:
    """An ``asset-player`` manifest: an Asset_Reference and display facts, never a URL.

    The id is deterministic per (session, asset), so showing the same file twice rewrites one object and
    the SPA's ``render_request_id`` dedup keeps one tab. ``ManifestRouter`` spreads the manifest into the
    template's ``data``, which is exactly the block shape a Citation open builds (``WorkspaceView``).
    """
    manifest_id = "asset-" + hashlib.sha256(f"{session_id}|{asset_id}".encode()).hexdigest()[:24]
    return {
        "id": manifest_id,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "template": "asset-player",
        "title": display_name,
        "ref": {"project_id": project_id, "asset_id": asset_id, "frame_s3_key": None},
        "kind": kind,
        "displayName": display_name,
        "startMs": 0,
        "origin": "agent",
    }


def _project_browser_manifest(session_id: str, project_id: str, project_name: str, query: str, folder: str,
                              kind: str) -> dict:
    """A ``project-browser`` manifest: which project and the starting filter, never asset rows or URLs.

    The id is deterministic per (session, project, filter): browsing the same view twice rewrites one tab, while a
    different filter opens its own.
    """
    flt = {"q": query, "folder": folder, "kind": kind}
    manifest_id = "browse-" + hashlib.sha256(
        json.dumps([session_id, project_id, flt], sort_keys=True).encode()).hexdigest()[:24]
    return {
        "id": manifest_id,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "template": "project-browser",
        "title": f"Browse · {project_name}",
        "project_id": project_id,
        "project_name": project_name,
        "filter": flt,
        "origin": "agent",
    }


def _announce(key: str, manifest_id: str, display_name: str) -> bool:
    """Push the render step that opens the tab (as ``hooks.actions._flush_moments_slot`` does)."""
    try:
        from orchestration.progress_channel import get_active_channel

        ch = get_active_channel()
        if ch is None:
            return False
        ch.push("done", f"↳ {display_name}", agent="render",
                extra={"render_manifest_key": key, "render_request_id": manifest_id,
                       "render_label": display_name})
        return True
    except Exception as exc:  # noqa: BLE001 - the manifest is written; announcing is best-effort
        logger.debug("show_asset: could not announce %s: %s", key, exc)
        return False


def _remember(session_id: str, code: str, entry: tuple[str, str, str]) -> None:
    with _PENDING_LOCK:
        codes = _PENDING.setdefault(session_id, {})
        codes[code] = entry
        while len(codes) > _PENDING_PER_SESSION:
            codes.pop(next(iter(codes)))


def _take(session_id: str, code: str) -> Optional[tuple[str, str, str]]:
    with _PENDING_LOCK:
        return (_PENDING.get(session_id) or {}).get(code)


def _forget(session_id: str, code: str) -> None:
    with _PENDING_LOCK:
        (_PENDING.get(session_id) or {}).pop(code, None)


def _project_row(p: dict, current_id: str) -> dict:
    row = {"name": p.get("name"), "role": p.get("role"), "personal": bool(p.get("personal"))}
    for k in ("indexing", "activeAssetCount"):
        if k in p:
            row[k] = p[k]
    if p.get("projectId") == current_id:
        row["current"] = True
    return row


def _match_project(rows: list, name: str) -> dict:
    """``{project}`` for the one project named ``name`` (case- and space-insensitive), else ``{error}``."""
    want = " ".join(str(name or "").split()).casefold()
    if not want:
        return {"error": "invalid_name", "message": "Say which project (by name)."}
    exact = [p for p in rows if " ".join(str(p.get("name") or "").split()).casefold() == want]
    if len(exact) == 1:
        return {"project": exact[0]}
    near = [str(p.get("name")) for p in rows if want in str(p.get("name") or "").casefold()]
    if len(exact) > 1:
        return {"error": "ambiguous_name", "message": "More than one project has that name; ask the user which.",
                "candidates": [str(p.get("name")) for p in exact]}
    return {"error": "project_not_found",
            "message": "No project of yours has that name" + ("; did they mean one of these?" if near else
                                                              "; list_projects shows the ones they can use."),
            **({"candidates": near[:10]} if near else {})}


def make_project_tools(project_id: str, *, function_name: Optional[str] = None, client: Any = None,
                       token_source: Optional[Callable[[], str]] = None, project_name: str = "",
                       registry: Any = None) -> list:
    """The project tools for ``project_id`` (the turn's Active_Project), or ``[]`` when no Asset_Service is set."""
    fn = function_name if function_name is not None else os.environ.get(ASSET_SERVICE_FN_ENV, "")
    if not fn:
        return []
    inv = _Invoker(fn, client, token_source or _default_token_source)

    async def call(command: str, args: dict) -> dict:
        return await asyncio.to_thread(inv.call, project_id, command, args)

    @tool
    async def project_status(name: Optional[str] = None) -> dict:
        """Report ingestion progress for the Active_Project, or find its files by name.

        Without `name`: the number of assets in each status (stored, queued, analyzing, indexing, indexed,
        failed, removed), whether the project's `indexing` is on, the search collection state (anything but
        "ready" means searches can return partial results), the newest failed assets with their reasons, and
        how many failed assets were not listed. `stored` files are kept but not analyzed: usually because indexing
        is off (the owner can turn it on in the project's settings); a `stored` match with
        `indexSkippedReason: "no_speech"` is an audio file with no speech (music, sound effects), which cannot be
        indexed yet — it is not a failure.

        With `name`: only `matches` (assets whose name or folder path contains it, case-insensitive; each with
        assetId, displayName, kind, status, folder when the file is in one, durationMs when known, `technical`
        when the file has been probed, and failure reason when failed) and `matchCounts` (byKind, byStatus).
        This works whether indexing is on or off, so use it to locate a file by name or to list a folder
        ("Day 1"). `technical` is the probe of a project video: container, durationSec, sizeBytes, bitRate,
        encoder, video {codec, profile, width, height, fps, bitRate, pixFmt} and audio {codec, channels,
        sampleRate, bitRate}. Answer codec, resolution, frame rate or bitrate questions about a project file
        from it, without probing the file again. Each chat's outputs sit in "Chats/<chat title> · <date>"
        folders ("Sessions/…" or "Generated/…" in older projects).

        When name is given, answer from matches only (count them by kind) in one or two sentences; mention a
        failure only if it is one of the matches; do not mention removed files, other projects, or the
        collection state unless the user asks about search or ingestion. Without name, report every count,
        the collection state when it is not ready, and each failure.

        Args:
            name: Optional part of an asset name to look up (1-255 characters).
        """
        args: dict = {}
        if name:
            args["name"] = name
        out = _reply(await call("project_status", args))
        return _name_lookup(out) if name and "error" not in out else out

    @tool
    async def register_s3_location(location: str) -> dict:
        """Add every supported video, image, and audio object under an S3 location to the Active_Project.

        Use only when the user gives an S3 location (s3://bucket/prefix/ or an object key) and asks to add it.
        Returns how many objects were registered, skipped, or refused, with reasons.

        Args:
            location: The s3:// URI of a prefix or an object.
        """
        return _reply(await call("register_s3_location", {"location": location}))

    @tool
    async def retry_asset(asset_id: str) -> dict:
        """Retry a FAILED asset of the Active_Project. Only steps that failed (or whose outputs are gone) rerun.

        Use the asset_id from project_status. Refused, naming the current status, when the asset is not failed.

        Args:
            asset_id: The asset's id, e.g. "video:6f1c...".
        """
        return _reply(await call("retry", {"assetId": asset_id}))

    @tool
    async def readd_asset(asset_id: str) -> dict:
        """Re-add a REMOVED asset of the Active_Project under the same id; recorded analysis is reused.

        Refused when the asset is not removed, or when its uploaded file was deleted by the removal.

        Args:
            asset_id: The asset's id.
        """
        return _reply(await call("readd", {"assetId": asset_id}))

    @tool
    async def remove_asset(asset_id: str, confirmation: Optional[str] = None) -> dict:
        """Remove an asset from the Active_Project (it stops appearing in search).

        Two steps, never in one turn: call WITHOUT `confirmation` first; it changes nothing and returns
        `needs_confirmation`, the asset's `display_name`, and a `confirmation` code. Ask the user to confirm
        removing that name. Only if the user's NEXT message explicitly confirms, call again with that code. If the
        user declines or says anything else, do not call again.

        Args:
            asset_id: The asset's id.
            confirmation: The code from the first call, only after the user explicitly confirmed.
        """
        session_id, turn_id = _request_ids()
        if not confirmation:
            looked = await call("resolve_assets", {"assetIds": [asset_id]})
            asset = ((looked.get("value") or {}).get("assets") or {}).get(asset_id) if looked.get("ok") else None
            if not asset:
                return _reply(looked) if not looked.get("ok") else {"error": "asset_not_found",
                                                                     "message": "Asset not found"}
            if asset.get("status") == "removed":
                return {"removed": True, "display_name": asset.get("displayName"), "message": "Already removed"}
            code = secrets.token_hex(4)
            _remember(session_id or "-", code, (asset_id, str(asset.get("displayName") or ""), turn_id or ""))
            return {"needs_confirmation": True, "display_name": asset.get("displayName"), "confirmation": code}
        pending = _take(session_id or "-", confirmation)
        if pending is None or pending[0] != asset_id:
            return {"error": "confirmation_invalid",
                    "message": "That confirmation does not match; call remove_asset without one and ask again"}
        if turn_id and pending[2] == turn_id:
            return {"error": "confirmation_too_early",
                    "message": f"Ask the user to confirm removing {pending[1]} first; confirm in their next message"}
        _forget(session_id or "-", confirmation)
        out = _reply(await call("remove", {"assetId": asset_id}))
        return {"removed": True, "display_name": pending[1]} if "error" not in out else out

    @tool
    async def show_asset(asset_id: str) -> dict:
        """Open a project file in the Renderer panel. Find it first with project_status(name=...) and pass its
        assetId. Use this ONLY when the user explicitly asks to open, play or show a project file; a link in
        your reply does not open it. Never use it to answer a search or a question about the media's content
        ("find the moment where...", "what is X about") -- the Moments tab already plays those.

        Refused (nothing opens) when the asset is not in the Active_Project, was removed, or is a data file
        (JSON/CSV/text). A document (PDF) opens in the Renderer's PDF viewer. Returns `shown` and the file's
        `display_name`.

        To show a project IMAGE inside your reply instead -- a diagram or still beside the text it explains --
        do not call this; write `![<caption>](asset:<asset_id>)` in your reply. It renders inline, and the user
        can expand it to the Renderer. An image you saved in THIS turn has no asset id yet: write its output file
        name instead, `![<caption>](asset:<file name>)` (it resolves in this chat's output folder). Keep video,
        timelines and analysis results in the Renderer.

        Args:
            asset_id: The asset's id from project_status, e.g. "image:6f1c...".
        """
        looked = await call("resolve_assets", {"assetIds": [asset_id]})
        if not looked.get("ok"):
            return _reply(looked)
        asset = ((looked.get("value") or {}).get("assets") or {}).get(asset_id)
        if not asset:
            return {"error": "asset_not_found", "message": "Asset not found in the active project"}
        display_name = str(asset.get("displayName") or asset_id)
        if asset.get("status") == "removed":
            return {"error": "asset_removed", "message": f"{display_name} was removed from the project"}
        kind = asset.get("kind")
        if kind not in OPENABLE_KINDS:
            return {"error": "not_viewable", "message": f"{display_name} is not a video, image, audio or document file"}
        session_id, _turn = _request_ids()
        if not session_id:
            return {"error": "show_unavailable", "message": "The Renderer is not available in this turn"}
        manifest = _asset_player_manifest(session_id, project_id, asset_id, kind, display_name)
        from render.artifacts import write_session_manifest

        key = await asyncio.to_thread(write_session_manifest, session_id, manifest["id"], manifest)
        if not key or not _announce(key, manifest["id"], display_name):
            return {"error": "show_unavailable", "message": f"Could not open {display_name}; try again"}
        # The turn's Moments gallery skips this picture (it would be the same image in a second tab).
        try:
            from hooks.actions import note_shown_asset
            from project_search.context import current_turn_context

            tc = current_turn_context.get()
            for tid in {_turn or "", getattr(tc, "turn_id", "") or ""}:
                note_shown_asset(tid, asset_id)
        except Exception as exc:  # noqa: BLE001 - de-duplication is best-effort
            logger.debug("show_asset: could not note %s: %s", asset_id, exc)
        return {"shown": True, "display_name": display_name}

    @tool
    async def use_asset(asset_id: str) -> dict:
        """Reference a project file in THIS chat so another tool can read it -- the file an image or video edit,
        restyle, upscale, transcode or code cell should work on. Call it BEFORE handing the file to such a tool
        (find it first with project_status(name=...)), then pass the returned `source_url` as the file's URL.
        It is a short stable link the harness signs at the moment each tool runs: never ask the user for a link,
        never construct S3 keys, and never paste a presigned URL in its place.

        Same as the user attaching the file in chat: the chat is recorded on the asset. Refused when the asset
        is not in the Active_Project, was removed, or belongs to another chat. Returns `asset_id`,
        `display_name`, `kind`, `source_url` (https, the link to give the user and to pass to tools) and
        `source_uri` (s3://).

        Args:
            asset_id: The asset's id from project_status or show_asset, e.g. "image:6f1c...".
        """
        session_id, _turn = _request_ids()
        if not session_id:
            return {"error": "use_unavailable", "message": "No chat session for this turn"}
        recorded = await call("record_chat_session", {"assetId": asset_id, "sessionId": session_id})
        if not recorded.get("ok"):
            return _reply(recorded)
        row = (recorded.get("value") or {}).get("asset") or {}
        display_name = str(row.get("displayName") or asset_id)
        if row.get("status") == "removed":
            return {"error": "asset_removed", "message": f"{display_name} was removed from the project"}
        return {"asset_id": asset_id, "display_name": display_name, "kind": row.get("kind"),
                **asset_source_refs(row.get("sourceUri"))}

    @tool
    async def browse_project(query: Optional[str] = None, folder: Optional[str] = None,
                             kind: Optional[str] = None) -> dict:
        """Open the Active_Project's files as a browsable grid in a Renderer tab. Use this when the user asks to
        browse, list, look through or open the project (or "show me the project's files / videos / images"),
        rather than listing files in your reply. The user can search and filter there, and clicking a video,
        image or audio file opens it in its own tab. To open ONE specific file, use show_asset instead.

        All arguments are optional starting filters; omit them to show everything.

        Args:
            query: A name keyword to pre-fill the grid's search, e.g. "meridian".
            folder: A folder path to start in, e.g. "Chats/Storyboard · Oct 10".
            kind: Only one kind of file: "video", "image", "audio", "document" or "data".
        """
        k = (kind or "").strip().lower()
        if k and k not in _BROWSE_KINDS:
            return {"error": "invalid_kind", "message": f"kind must be one of {', '.join(_BROWSE_KINDS)}"}
        session_id, _turn = _request_ids()
        if not session_id:
            return {"error": "browse_unavailable", "message": "The Renderer is not available in this turn"}
        name = project_name or "Project"
        manifest = _project_browser_manifest(session_id, project_id, name, (query or "").strip()[:200],
                                             (folder or "").strip()[:500], k)
        from render.artifacts import write_session_manifest

        key = await asyncio.to_thread(write_session_manifest, session_id, manifest["id"], manifest)
        if not key or not _announce(key, manifest["id"], manifest["title"]):
            return {"error": "browse_unavailable", "message": "Could not open the project browser; try again"}
        return {"opened": True, "project": name, "filter": {k2: v for k2, v in manifest["filter"].items() if v}}

    @tool
    async def organize_project(folder: str = "", asset_ids: Optional[list[str]] = None,
                               moments: Optional[list[dict]] = None, rename_from: Optional[str] = None,
                               folders: Optional[list[str]] = None, confirmed: bool = False) -> dict:
        """Organize the Active_Project's files into folders, the way the user would in the Assets grid.

        `folders`: CREATE these folders, empty, inside `folder` ("" = the top level). Each is a "/" path, so a whole
        tree is one call — e.g. a bin structure from a screenshot: ["AUDIO/MUSIC", "AUDIO/SFX",
        "FOOTAGE/STOCK FOOTAGE/Licensed", ...] (parents are implied). Empty folders are real and every member sees
        them at once; nothing needs to be filed in them first.

        Three more things, any combination, all filed in `folder` (a "/" path such as "Boards/Board 07"; "" is the
        top level; missing folders are created):
          - `asset_ids`: MOVE these files (ids from project_status or a search hit) into `folder`. Only the folder
            changes: nothing is copied, re-uploaded or re-indexed, and every chat that used a file still finds it.
          - `moments`: SAVE these moments as bookmarks in `folder`, each {"asset_id", "start_ms", "end_ms"
            (optional), "label" (optional)} — e.g. the grip close-ups a search found. A bookmark is a range of the
            file, not a copy, so one take can be saved in several folders. Use this for a moment, `asset_ids` for a
            whole file.
          - `rename_from`: rename (or move) the folder `rename_from` and everything in it to `folder`.

        Ask the user first, and call again with `confirmed: true` once they agree, when moving or bookmarking more
        than 10 items or renaming a folder (the reply says `needs_confirmation`). Do not invent folder names the
        user did not ask for; use the names they gave or ones already in the project (project_status lists
        folders). Returns what moved, what was saved and what failed (with reasons).

        Args:
            folder: The destination folder path ("" = the top level).
            folders: Empty folders to create inside `folder` (paths; parents are implied).
            asset_ids: Files to move into `folder`.
            moments: Moments to save as bookmarks in `folder`.
            rename_from: A folder to rename to `folder`.
            confirmed: True once the user has agreed to a large move or a rename.
        """
        ids = [a for a in (asset_ids or []) if isinstance(a, str) and a]
        marks = [m for m in (moments or []) if isinstance(m, dict)]
        new = [f.strip().strip("/") for f in (folders or []) if isinstance(f, str) and f.strip().strip("/")]
        if not ids and not marks and not rename_from and not new:
            return {"error": "nothing_to_do",
                    "message": "Give folders to create, asset_ids to move, moments to save, or rename_from"}
        if not confirmed and (rename_from or len(ids) + len(marks) > ORGANIZE_CONFIRM_OVER):
            what = (f"rename the folder {rename_from!r} to {folder!r}" if rename_from
                    else f"file {len(ids) + len(marks)} items in {folder or 'the top level'!r}")
            return {"needs_confirmation": True, "message": f"Ask the user before you {what}; then call again with "
                                                            f"confirmed: true."}
        out: dict = {"folder": folder}
        if new:
            base = (folder or "").strip().strip("/")
            created, failed_folders = [], []
            for path in dict.fromkeys(new[:200]):
                full = f"{base}/{path}" if base else path
                r = _reply(await call("create_folder", {"folder": full}))
                if "error" in r:
                    failed_folders.append({"folder": full, "error": r.get("message") or r.get("error")})
                else:
                    created.append(r.get("folder", full))
            out["created"] = {"folders": created, "failed": failed_folders}
        if rename_from:
            moved, failed, remaining = 0, [], 1
            for _ in range(50):  # 500 rows per call; a huge folder takes a few
                r = _reply(await call("rename_folder", {"from": rename_from, "to": folder}))
                if "error" in r:
                    return r
                moved += len(r.get("moved") or [])
                failed += r.get("failed") or []
                remaining = int(r.get("remaining") or 0)
                if remaining == 0:
                    break
            out["renamed"] = {"from": rename_from, "moved": moved, "failed": failed, "remaining": remaining}
        if ids:
            moved_ids, failed = [], []
            for i in range(0, len(ids), 100):
                r = _reply(await call("move_assets", {"assetIds": ids[i:i + 100], "folder": folder}))
                if "error" in r:
                    return {**out, **r}
                moved_ids += r.get("moved") or []
                failed += r.get("failed") or []
                out["folder"] = r.get("folder", folder)
            out["moved"] = {"count": len(moved_ids), "failed": failed}
        if marks:
            items = []
            for m in marks:
                item = {"assetId": m.get("asset_id"), "startMs": m.get("start_ms")}
                if m.get("end_ms") is not None:
                    item["endMs"] = m.get("end_ms")
                if isinstance(m.get("label"), str):
                    item["label"] = m["label"]
                items.append(item)
            session_id, _turn = _request_ids()
            saved, failed = 0, []
            for i in range(0, len(items), 100):
                args: dict = {"items": items[i:i + 100], "folder": folder}
                if session_id:
                    args["sessionId"] = session_id
                r = _reply(await call("create_bookmarks", args))
                if "error" in r:
                    return {**out, **r}
                saved += len(r.get("created") or [])
                failed += r.get("failed") or []
            out["bookmarked"] = {"count": saved, "failed": failed}
        return out

    reg = registry if registry is not None else RegistryClient()

    async def _projects() -> tuple[list, Optional[dict]]:
        out = await asyncio.to_thread(reg.list)
        if "error" in out:
            return [], out
        return out["projects"], None

    def _open_in(pid: str, pname: str, prompt: str) -> dict:
        session_id, _turn = _request_ids()
        if not turn_actions.request_open_project(session_id, pid, pname, prompt):
            return {"error": "already_switching",
                    "message": "This turn already opens a chat in another project; finish the turn there."}
        return {"status": "opening_new_chat", "project": {"projectId": pid, "name": pname},
                "message": (f"A new chat in \"{pname}\" opens when this reply ends"
                            + (" and sends it the prompt" if prompt.strip() else "")
                            + ". Say so in one short sentence and stop: the rest happens in that chat.")}

    @tool
    async def list_projects() -> dict:
        """List the projects the user can work in: their Personal project, the ones they own, and the ones they
        were invited to. Each has `name`, `role` (owner | member), `personal`, `indexing` (files analyzed for
        search) and `activeAssetCount` when known; `current` marks the project THIS chat is in.
        """
        rows, err = await _projects()
        if err:
            return err
        return {"projects": [_project_row(p, project_id) for p in rows], "current": project_name}

    @tool
    async def switch_project(name: str, prompt: str = "") -> dict:
        """Move the user to another project: when this reply ends the chat opens a NEW chat in that project and,
        when `prompt` is given, sends it as that chat's first message. This chat, its files and its history stay
        where they are; nothing is moved or copied. A chat's project is fixed by its first message, which is why
        switching always means a new chat.

        Use it when the user asks to go to, open, or work in another project, or when a skill says to switch.
        Pass the user's request (or the skill's next step) as `prompt` so the work carries on there. Returns
        `already_here` when this chat is already in that project: then simply continue in this chat.
        After `opening_new_chat`, say so in one short sentence and stop.

        Args:
            name: The project's name (case-insensitive). Use list_projects when unsure.
            prompt: Optional first message for the new chat (up to 2000 characters).
        """
        rows, err = await _projects()
        if err:
            return err
        match = _match_project(rows, name)
        if "error" in match:
            return match
        p = match["project"]
        if p.get("projectId") == project_id:
            return {"status": "already_here", "project": {"name": p.get("name")},
                    "message": "This chat is already in that project; continue here."}
        return _open_in(str(p.get("projectId")), str(p.get("name") or name), prompt)

    @tool
    async def create_project(name: str, description: str = "", indexing: bool = True, prompt: str = "") -> dict:
        """Create a NEW project owned by the user, then move them into it: when this reply ends the chat opens a
        new chat in that project and, when `prompt` is given, sends it as that chat's first message. Nothing in
        this chat is moved. Create one only when the user asks for it (or a skill they started says to); never to
        work around a refused or missing project. Fails with `name_taken` when they already have one of that name.

        Args:
            name: The project name the user asked for (keep their spelling; up to 100 characters).
            description: Optional one-line description.
            indexing: Analyze files for search as they are added (default true; false = store only).
            prompt: Optional first message for the new chat (up to 2000 characters).
        """
        clean = " ".join(str(name or "").split())[:100]
        if not clean:
            return {"error": "invalid_name", "message": "Ask the user what to call the project."}
        out = await asyncio.to_thread(reg.create, clean, description=" ".join(str(description or "").split())[:300],
                                      indexing=bool(indexing))
        if "error" in out:
            return out
        made = out.get("project") if isinstance(out.get("project"), dict) else {}
        pid = str(made.get("projectId") or "")
        if not pid:
            return {"error": "unavailable", "message": "The project service gave an unexpected reply; try again."}
        opened = _open_in(pid, str(made.get("name") or clean), prompt)
        return {**opened, "created": True}

    @tool
    async def delete_project(name: str) -> dict:
        """Offer to DELETE one of the user's projects. You cannot delete it yourself: this returns a card for your
        reply, and the project is deleted only if the user types its exact name there and confirms. Deleting removes
        every file in it for every member and cannot be undone. Only the owner can delete; the Personal project
        cannot be deleted. Use it only when the user asks to delete a project. Put the returned `markdown` in your
        reply exactly as given, on its own line, and do not say the project is deleted.

        Args:
            name: The project's name (case-insensitive). Use list_projects when unsure.
        """
        rows, err = await _projects()
        if err:
            return err
        match = _match_project(rows, name)
        if "error" in match:
            return match
        p = match["project"]
        if p.get("personal"):
            return {"error": "personal_project", "message": "The Personal project cannot be deleted."}
        if p.get("role") != "owner":
            return {"error": "not_owner", "message": "Only the project's owner can delete it; a member can leave it."}
        pid, pname = str(p.get("projectId")), str(p.get("name") or "")
        return {
            "status": "awaiting_user_confirmation",
            "markdown": f"[Delete project “{pname}”](project:delete?id={quote(pid)}&name={quote(pname)})",
            "message": "Include `markdown` in your reply verbatim. Nothing is deleted unless the user confirms there.",
        }

    return [project_status, register_s3_location, retry_asset, remove_asset, readd_asset, show_asset,
            use_asset, browse_project, organize_project, list_projects, switch_project, create_project,
            delete_project]
