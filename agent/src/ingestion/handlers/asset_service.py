"""Asset_Service Lambda — the only writer of Project_Asset rows outside the pipeline (design §2).

IAM limits who may invoke it (the proxy and the Main_Agent runtime roles); the ``userToken`` in the
event fixes which Actor the call is for, exactly like the Token_Issuer (projects-access D2). Every
command runs the same prologue — verify the User_Token, then ``check_access`` with a consistent read —
before it touches a row, and every reply is a JSON object (never a Lambda error) so a domain refusal
is a ``{ok:false,status,error}`` body the agent-side client already understands.

Event / reply contract (must match ``agent/src/project_search/assets.py`` for ``resolve_assets``):

    { "userToken", "projectId", "command", "args", "caller", "sessionId"?, "turnId"? }
    { "ok": true, "value": {...} }
    | { "ok": false, "status": 400|401|404|409|503, "error": {"code", "message"} }

Commands: ``resolve_assets`` (the one the Main_Agent turn-end path calls), ``register_upload``,
``register_s3_location``, ``remove``, ``record_chat_session``, ``remove_session_assets``,
``record_chat_copy`` (legacy), and ``capabilities``. ``retry``/``readd``/
``project_status``/``rebuild_retry`` are owned by the same spec and return ``unknown_command`` until their
waves land.

``remove`` (Projects Phase B) moves the asset to ``removed`` (``AssetStore.remove_asset``), then deletes
bytes the Harness owns, and only those: the source object of an uploaded or generated asset (its key is
under ``projects/<pid>/``, which includes a ``visibility=session`` "this chat only" upload — the session
owns those bytes), the in-place source of a ``session`` asset (asset-lineage design §2, a key under
``artifacts/<…>/output/``), and every chat copy a legacy row recorded (``chatCopyKeys``, each under
``artifacts/<sessionId>/input/``; Phase 2 copies no bytes, so no new row gets one). An asset registered from an S3 location, and an ``m2c`` asset (D4: M2C
bytes are kept for audit), is only unregistered; its source object is never deleted. The Ingest bucket is versioned, so a delete writes a delete marker and
the bucket's noncurrent-version lifecycle (30 days) reclaims the bytes.
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional
from urllib.parse import unquote

from projects import schema
from projects.access import AccessCheckIncomplete, check_access
from projects.ids import is_project_id

from .. import catalog, rebuild, rendition
from ..personal_samples import samples_from_env, seed_personal_samples
from ..folders import FOLDER_METADATA_KEY, date_from, folder_or_error, session_folder
from ..limits import (
    MAX_FILES_PER_BATCH,
    MULTIPART_THRESHOLD_BYTES,
    PART_SIZE_BYTES,
    admit,
    kind_for,
    limits_from_env,
    entity_metadata_enabled,
)
from ..names import sanitize
from ..registrar import LocationError, S3LocationRegistrar, allowlist_from_env
from ..store import (
    CHAT_COPY_KEY_RE,
    CHAT_SESSION_ID_RE,
    AssetNotFound,
    AssetStore,
    ProjectNotActive,
    RegistrationConflict,
    WrongStatus,
    asset_public_row,
    asset_public_view,
    indexing_on,
    jsonable,
    queueable,
)

__all__ = ["AssetService", "handler", "ServiceError", "bookmark_view"]


def bookmark_view(row: Mapping[str, Any]) -> dict:
    """The client view of a bookmark row (the twin of the proxy's): ids, range, label, folder, its asset's name."""
    out = {k: row.get(k) for k in ("bookmarkId", "assetId", "startMs", "label", "assetKind", "assetDisplayName",
                                   "createdAt") if row.get(k) is not None}
    for k in ("endMs", "folder", "sessionId"):
        if row.get(k) not in (None, ""):
            out[k] = row.get(k)
    return jsonable(out)

_COMMANDS = (
    "resolve_assets",
    "register_upload",
    "register_s3_location",
    "remove",
    "record_chat_session",
    "remove_session_assets",
    "record_chat_copy",
    "capabilities",
    "enable_indexing",
    "move_assets",
    "rename_folder",
    "create_folder",
    "resolve_locations",
    "create_bookmarks",
    "remove_bookmarks",
    "import_status",
    "rescan_import",
    "seed_personal_samples",
)
_RESOLVE_PAGE_MAX = 100
# One move/resolve call names at most this many assets or locations; a folder rename rewrites at most
# _RENAME_PAGE_MAX rows per call and reports how many remain, so a huge folder is renamed in a few calls rather
# than one call that outlives its invoke.
_MOVE_MAX = 100
_RENAME_PAGE_MAX = 500
# Only an uploaded or generated asset's bytes are the Harness's to delete (never an s3-location source).
_M2C_REF_PREFIX = "artifacts/m2c/"
# A session's copy of a job output: ``<session output prefix><execution id>/<path>`` in either layout.
_JOB_OUTPUT_COPY_RE = re.compile(
    r"^(?:projects/p[0-9a-f]{32}/sessions/[^/]+|artifacts/[^/]+)/output/(?P<exec>[A-Za-z0-9-]{8,80})/(?P<rel>[^?#]+)$")
_OWNED_SOURCES = frozenset({"upload", "generated"})
# A `session` asset is registered in place (asset-lineage design §2): its bytes are a session output, and
# only a key under a session's `output/` is ever deleted with it. An `m2c` asset is never deleted: M2C
# outputs are kept for audit and traceability (D4), so removing one drops the harness record only.
_SESSION_OUTPUT_KEY_RE = re.compile(r"^artifacts/[A-Za-z0-9._-]+/output/.+[^/]$")
# The project layout (asset-lineage Phase 3 (a)): owned only when its <pid> is the asset's own Project.
_PROJECT_SESSION_OUTPUT_KEY_RE = re.compile(r"^projects/(p[0-9a-f]{32})/sessions/[A-Za-z0-9._-]+/output/.+[^/]$")


class ServiceError(Exception):
    """A domain refusal: carries the HTTP-ish ``status`` and the ``{code, message}`` the reply uses."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _ok(value: dict) -> dict:
    # A Lambda reply is serialized with plain json: Decimal (every number boto3 reads) and set would fail.
    return {"ok": True, "value": jsonable(value)}


def _err(status: int, code: str, message: str) -> dict:
    return {"ok": False, "status": status, "error": {"code": code, "message": message}}


log = logging.getLogger(__name__)

# Uniform not-found body for a denial, a malformed/other-Project asset, or a missing Project (Req 3.11).
_PROJECT_NOT_FOUND = ("project_not_found", "Project not found")
_ASSET_NOT_FOUND = ("asset_not_found", "Asset not found")


class AssetService:
    """The Asset_Service, with every external dependency injectable for unit tests.

    Defaults wire the deployed path: a Cognito verifier from the env, ``projects.access.check_access``
    over the projects table, an ``AssetStore`` for the row writes, and a boto3 S3 client for
    ``HeadObject`` / listing. Tests pass a fake verifier and a moto-backed store, with no network.
    """

    def __init__(
        self,
        *,
        store: AssetStore,
        verifier: Any,
        s3_client: Any,
        ingest_bucket: str,
        allowlist: Optional[list[str]] = None,
        access_check: Callable[[Any, str, str], Any] = check_access,
        clock: Callable[[], float] = time.time,
        limits: Optional[dict] = None,
        catalog_tools: Optional[Callable[[], Any]] = None,
        request_rebuild: Optional[Callable[[str, str], Any]] = None,
        retry_rebuild: Optional[Callable[[str], Any]] = None,
        entity_metadata: Optional[bool] = None,
        personal_samples: Optional[list] = None,
    ):
        self._personal_samples = personal_samples
        self._limits = limits or limits_from_env()
        self._catalog = catalog_tools
        self._entity_metadata = entity_metadata_enabled() if entity_metadata is None else entity_metadata
        self._request_rebuild = request_rebuild
        self._retry_rebuild = retry_rebuild
        self._store = store
        self._verifier = verifier
        self._s3 = s3_client
        self._ingest_bucket = ingest_bucket
        self._allowlist = allowlist or []
        self._access_check = access_check
        self._clock = clock

    # ------------------------------------------------------------------ entrypoint
    def handle(self, event: object) -> dict:
        try:
            actor, pid, role = self._prologue(event)
            command = event.get("command") if isinstance(event, dict) else None
            args = (event.get("args") if isinstance(event, dict) else None) or {}
            if not isinstance(args, dict):
                raise ServiceError(400, "invalid_args", "args must be an object")
            if command == "resolve_assets":
                return _ok(self._resolve_assets(pid, args))
            if command == "search_images":
                return _ok(self._search_images(pid, args))
            if command == "register_upload":
                return _ok(self._register_upload(pid, actor, args))
            if command == "register_s3_location":
                return _ok(self._register_s3_location(pid, actor, args))
            if command == "remove":
                return _ok(self._remove(pid, actor, args))
            if command == "record_chat_session":
                return _ok(self._record_chat_session(pid, args))
            if command == "remove_session_assets":
                return _ok(self._remove_session_assets(pid, actor, args))
            if command == "record_chat_copy":
                return _ok(self._record_chat_copy(pid, args))
            if command == "capabilities":
                return _ok(self._capabilities())
            if command == "retry":
                return _ok(self._retry(pid, args))
            if command == "readd":
                return _ok(self._readd(pid, args))
            if command == "project_status":
                return _ok(self._project_status(pid, args))
            if command == "rebuild_retry":
                return _ok(self._rebuild_retry(pid))
            if command == "enable_indexing":
                return _ok(self._enable_indexing(pid, actor, role))
            if command == "move_assets":
                return _ok(self._move_assets(pid, args))
            if command == "rename_folder":
                return _ok(self._rename_folder(pid, args))
            if command == "create_folder":
                return _ok(self._create_folder(pid, actor, args))
            if command == "resolve_locations":
                return _ok(self._resolve_locations(pid, args))
            if command == "create_bookmarks":
                return _ok(self._create_bookmarks(pid, actor, args))
            if command == "remove_bookmarks":
                return _ok(self._remove_bookmarks(pid, args))
            if command == "import_status":
                return _ok(self._import_status(pid))
            if command == "rescan_import":
                return _ok(self._rescan_import(pid))
            if command == "seed_personal_samples":
                return _ok(self._seed_personal_samples(pid, actor, role))
            if command == "lineage":
                return _ok(self._lineage(pid, actor, args))
            if command in _ALL_FUTURE_COMMANDS:
                raise ServiceError(400, "unknown_command", f"{command} is not available yet")
            raise ServiceError(400, "unknown_command", f"Unknown command: {command!r}")
        except ServiceError as exc:
            return _err(exc.status, exc.code, exc.message)

    # ------------------------------------------------------------------ prologue
    def _prologue(self, event: object) -> tuple[str, str, Optional[str]]:
        """Verify the User_Token and the membership; return ``(actor_sub, project_id, role)``.

        Any token failure is ``401 unauthenticated``; any access denial, incomplete check, or missing
        Project is the uniform ``404 project_not_found`` (an incomplete read is ``503`` — a throttle is
        not a denial).
        """
        if not isinstance(event, dict):
            raise ServiceError(400, "invalid_event", "event must be an object")
        token = event.get("userToken")
        pid = event.get("projectId")
        try:
            claims = self._verifier.verify(token, now=self._clock())
        except Exception as exc:  # noqa: BLE001 — any verifier failure is a 401, text is not echoed
            name = type(exc).__name__
            if name == "JwksUnavailable":
                raise ServiceError(503, "verifier_unavailable", "Sign in again") from exc
            raise ServiceError(401, "unauthenticated", "Sign in again") from exc
        actor = claims.get("sub") if isinstance(claims, dict) else None
        if not isinstance(actor, str) or not actor:
            raise ServiceError(401, "unauthenticated", "Sign in again")
        if not is_project_id(pid):
            raise ServiceError(404, *_PROJECT_NOT_FOUND)
        try:
            result = self._access_check(self._store.table, actor, pid)
        except AccessCheckIncomplete as exc:
            raise ServiceError(503, "access_check_incomplete", "Try again") from exc
        if not getattr(result, "ok", False):
            raise ServiceError(404, *_PROJECT_NOT_FOUND)
        return actor, pid, getattr(result, "role", None)

    # ------------------------------------------------------------------ resolve_assets
    def _resolve_assets(self, pid: str, args: dict) -> dict:
        ids = args.get("assetIds")
        if not isinstance(ids, list):
            raise ServiceError(400, "invalid_args", "assetIds must be a list")
        if len(ids) > _RESOLVE_PAGE_MAX:
            raise ServiceError(400, "too_many_ids", f"At most {_RESOLVE_PAGE_MAX} ids per call")
        rows = self._store.batch_get_assets(pid, ids)
        assets: dict[str, dict] = {}
        for asset_id, row in rows.items():
            view = asset_public_view(row)
            if view is not None:
                assets[asset_id] = view
        return {"assets": assets}

    # ------------------------------------------------------------------ search_images
    def _search_images(self, pid: str, args: dict) -> dict:
        """Each image asset's Search_Rendition, the form a search query image takes (or why it can't be one).

        The index embeds an image's rendition, never its original, and M2C's search role may read only
        renditions; so a query by an image is a query by its rendition. Per id ``{"uri", "displayName"}``, or
        ``{"reason"}``: ``not_found`` (absent, removed, or another chat's "this chat only" upload),
        ``not_image`` (video, audio, documents: search by those is not supported), ``not_ready`` (ingestion is
        still preparing it), ``unreadable`` (the image cannot become a rendition). An image ingestion will
        never render (a "this chat only" upload, a stored or failed asset) is rendered here, on demand, with
        the same writer as IngestTick.
        """
        ids = args.get("assetIds")
        session_id = args.get("sessionId")
        if not isinstance(ids, list) or not ids or len(ids) > _SEARCH_IMAGES_MAX:
            raise ServiceError(400, "invalid_args", f"assetIds must be a list of 1-{_SEARCH_IMAGES_MAX} ids")
        if not isinstance(session_id, str) or not CHAT_SESSION_ID_RE.fullmatch(session_id):
            raise ServiceError(400, "invalid_args", "sessionId must be 1-128 of [A-Za-z0-9._-]")
        wanted = [i for i in dict.fromkeys(ids) if schema.is_asset_id(i)]
        rows = self._store.batch_get_assets(pid, wanted) if wanted else {}
        images: dict[str, dict] = {}
        for asset_id in dict.fromkeys(i for i in ids if isinstance(i, str)):
            row = rows.get(asset_id)
            if _needs_rendition_now(row, session_id):
                row = self._render_now(pid, asset_id, row)
            images[asset_id] = _search_image(row, session_id, self._ingest_bucket)
        return {"images": images}

    def _render_now(self, pid: str, asset_id: str, row: Mapping) -> Mapping:
        """Make the Search_Rendition of an image ingestion will never render, and return the updated row.

        The same writer as IngestTick (``rendition.store_search_rendition``). An image that cannot become one
        comes back marked ``_unreadable`` so the caller reports it instead of "still being prepared".
        """
        out_key = f"{schema.project_prefix(pid)}{asset_id}/rendition.jpg"
        try:
            bucket, key = _split_s3_uri(str(row.get("sourceUri") or ""))
            made = rendition.store_search_rendition(self._s3, source_bucket=bucket, source_key=key,
                                                    ingest_bucket=self._ingest_bucket, out_key=out_key)
        except Exception as exc:  # noqa: BLE001 — undecodable, too large, or unreadable: not a usable query image
            log.info("search_images: no rendition for %s (%s)", asset_id, type(exc).__name__)
            return {**row, "_unreadable": True}
        self._store.set_rendition_if_absent(pid, asset_id, made)
        return {**row, "rendition": made}

    # ------------------------------------------------------------------ register_upload
    def _register_upload(self, pid: str, actor: str, args: dict) -> dict:
        asset_id = args.get("assetId")
        file_name = args.get("fileName")
        size = args.get("size")
        content_type = args.get("contentType")
        kind = schema.asset_kind(asset_id) if isinstance(asset_id, str) else None
        if kind is None:
            raise ServiceError(404, *_ASSET_NOT_FOUND)
        if not isinstance(file_name, str) or not file_name:
            raise ServiceError(400, "invalid_args", "fileName is required")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ServiceError(400, "invalid_args", "size must be a positive integer")
        if not isinstance(content_type, str) or not content_type:
            raise ServiceError(400, "invalid_args", "contentType is required")
        if kind_for(content_type, file_name) != kind:
            raise ServiceError(400, "type_mismatch", "The declared type does not match the asset id")
        if admit(kind, size, self._limits) is not None:
            raise ServiceError(400, "too_large", f"The file exceeds the {kind} size limit")
        # The folder (design D20): an explicit `args.folder` wins and must be valid; otherwise the one the
        # proxy signed into the upload as object metadata (read from the HEAD below).
        folder_arg = args.get("folder")
        if folder_arg is not None:
            folder_arg, reason = folder_or_error(folder_arg)
            if reason:
                raise ServiceError(400, "invalid_folder", f"The folder is not valid ({reason})")
        # "This chat only" (asset-lineage D2): a hidden asset of the session, filed in its Sessions folder and
        # never indexed — the user asked for it NOT to join the project's searchable files.
        session = _session_visibility(args)
        if session is not None:
            folder_arg = _session_asset_folder(args)
        # Only an asset that will be ingested needs the M2C tools now; a stored one is checked when the
        # owner turns indexing on (`enable_indexing`).
        if session is None and kind in ("video", "audio", "image") and indexing_on(self._store.get_meta(pid)):
            self._check_catalog([kind])

        # Re-derive the key so a key outside the Target_Project or without the assetId cannot be named
        # (Req 1.17-1.19): projects/<pid>/<assetId>/<sanitize(fileName)>.
        key = f"{schema.project_prefix(pid)}{asset_id}/{sanitize(file_name)}"
        head = self._head_object(self._ingest_bucket, key)
        if head is None:
            raise ServiceError(409, "object_missing", "The uploaded object was not found")
        actual = int(head.get("ContentLength", -1))
        if actual != size:
            raise ServiceError(409, "size_mismatch", "The uploaded object size does not match")
        folder = folder_arg if folder_arg is not None else _folder_from_metadata(head)

        asset = self._register(
            pid=pid,
            asset_id=asset_id,
            source="upload",
            source_uri=f"s3://{self._ingest_bucket}/{key}",
            source_bucket=self._ingest_bucket,
            source_key=key,
            source_size=size,
            content_type=content_type,
            file_name=file_name,
            actor=actor,
            folder=folder,
            **(
                {"role": "source", "visibility": "session", "session_id": session,
                 "producer": {"sessionId": session}, "queue_for_indexing": False}
                if session is not None
                else {}
            ),
        )
        return {"asset": asset_public_row(asset)}

    # ------------------------------------------------------------------ register_s3_location
    def _register_s3_location(self, pid: str, actor: str, args: dict) -> dict:
        location = args.get("location")
        if not isinstance(location, str) or not location:
            raise ServiceError(400, "invalid_args", "location is required")
        registrar = S3LocationRegistrar(
            self._store,
            s3_client=self._s3,
            ingest_bucket=self._ingest_bucket,
            allowlist=self._allowlist,
            limits=self._limits,
        )
        try:
            summary = registrar.register(
                pid=pid,
                location=location,
                added_by=actor,
                added_by_display=self._actor_display(actor),
            )
        except LocationError as exc:
            raise ServiceError(exc.status, exc.code, exc.message) from exc
        except ProjectNotActive as exc:
            raise ServiceError(404, *_PROJECT_NOT_FOUND) from exc
        return summary

    # ------------------------------------------------------------------ remove
    def _remove(self, pid: str, actor: str, args: dict) -> dict:
        asset_id = args.get("assetId")
        if not schema.is_asset_id(asset_id):
            raise ServiceError(404, *_ASSET_NOT_FOUND)
        try:
            row, changed = self._store.remove_asset(pid, asset_id, removed_by=actor)
        except AssetNotFound as exc:
            raise ServiceError(404, *_ASSET_NOT_FOUND) from exc
        except RegistrationConflict as exc:
            raise ServiceError(409, "conflict", "The asset changed while it was being removed; try again") from exc
        # Bytes are deleted AFTER the row is removed, so a failure here never leaves a live asset pointing
        # at missing bytes. A repeat remove retries the deletes until one call finished them all
        # (`cleanupDone`); after that a repeat deletes nothing (in a versioned bucket every repeat delete
        # would add another delete marker).
        if not changed and row.get("cleanupDone") is True:
            deleted, failed = [], []
        else:
            deleted, failed = self._delete_owned_objects(pid, asset_id, row)
            if not failed:
                self._store.mark_cleanup_done(pid, asset_id)
        # Its rows stay in the collection until a rebuild replaces it with the indexed assets only (Req 10.5).
        if changed and self._request_rebuild is not None:
            meta = self._store.get_meta(pid) or {}
            if rebuild.accepted_in_current(row, meta):
                self._request_rebuild(pid, f"{asset_id} removed")
        return {
            "asset": asset_public_row(row),
            "changed": changed,
            "deleted": deleted,
            "failed": failed,
        }

    def _owned_keys(self, pid: str, asset_id: str, row: dict) -> list[str]:
        """The Ingest-bucket keys ``remove`` may delete for ``row`` (design: never an s3-location source).

        ``upload``/``generated``: the source under ``projects/<pid>/`` — a ``visibility=session`` upload ("this
        chat only", asset-lineage D2) is one of these, and its bytes go with it. ``session``: the source under
        ``artifacts/<…>/output/``, or ``projects/<pid>/sessions/<…>/output/`` of THIS Project. ``s3``/``m2c``: none (record only). Plus every chat copy a legacy row
        recorded (Phase 2 makes none).
        """
        keys: list[str] = []
        source_uri = row.get("sourceUri")
        prefix = f"s3://{self._ingest_bucket}/"
        if (
            row.get("source") in _OWNED_SOURCES | {"session"}
            and isinstance(source_uri, str)
            and self._ingest_bucket
            and source_uri.startswith(prefix)
        ):
            key = source_uri[len(prefix):]
            if row.get("source") == "session":
                m = _PROJECT_SESSION_OUTPUT_KEY_RE.fullmatch(key)
                owned = (_SESSION_OUTPUT_KEY_RE.fullmatch(key) is not None
                         or (m is not None and m.group(1) == pid))
            else:
                owned = key.startswith(schema.project_prefix(pid))
            if owned and ".." not in key:
                keys.append(key)
        copies = row.get("chatCopyKeys") or ()
        for key in sorted(k for k in copies if isinstance(k, str)):
            if CHAT_COPY_KEY_RE.fullmatch(key) and ".." not in key:
                keys.append(key)
        return keys

    def _delete_owned_objects(self, pid: str, asset_id: str, row: dict) -> tuple[list[str], list[str]]:
        deleted: list[str] = []
        failed: list[str] = []
        for key in self._owned_keys(pid, asset_id, row):
            try:
                self._s3.delete_object(Bucket=self._ingest_bucket, Key=key)
                deleted.append(key)
            except Exception:  # noqa: BLE001 — reported, and retried by the next (idempotent) remove
                failed.append(key)
        return deleted, failed

    # ------------------------------------------------------------------ record_chat_session
    def _record_chat_session(self, pid: str, args: dict) -> dict:
        """Record that a chat session holds a reference to this asset (asset-lineage Phase 2).

        The Phase 2 replacement for ``record_chat_copy``: an attachment references the Project_Asset, so there
        is no copy to record (or to chase on ``remove``) — only the association.
        """
        asset_id = args.get("assetId")
        session_id = args.get("sessionId")
        if not schema.is_asset_id(asset_id):
            raise ServiceError(404, *_ASSET_NOT_FOUND)
        if not isinstance(session_id, str) or not CHAT_SESSION_ID_RE.fullmatch(session_id):
            raise ServiceError(400, "invalid_args", "sessionId must be 1-128 of [A-Za-z0-9._-]")
        # A "this chat only" asset belongs to ONE chat (asset-lineage D2): never referenced from another. Here, not
        # only in the proxy's attach route, so the agent's `use_asset` gets the same rule.
        current = self._store.get_asset(pid, asset_id)
        if current is not None and current.get("visibility") == "session" and current.get("sessionId") != session_id:
            raise ServiceError(404, *_ASSET_NOT_FOUND)
        try:
            row = self._store.add_chat_session(pid, asset_id, session_id)
        except AssetNotFound as exc:
            raise ServiceError(404, *_ASSET_NOT_FOUND) from exc
        return {"asset": asset_public_row(row)}

    # ------------------------------------------------------------------ remove_session_assets
    def _remove_session_assets(self, pid: str, actor: str, args: dict) -> dict:
        """Remove every ``visibility=session`` asset of one chat session (asset-lineage D2).

        What a deleted session's "this chat only" files are: hidden project rows whose bytes the session owns,
        so each goes through the ordinary ``remove`` (record + bytes). Idempotent — an already-removed row is
        not listed by ``session_assets`` — and per-asset: one failure does not stop the rest.
        """
        session_id = args.get("sessionId")
        if not isinstance(session_id, str) or not CHAT_SESSION_ID_RE.fullmatch(session_id):
            raise ServiceError(400, "invalid_args", "sessionId must be 1-128 of [A-Za-z0-9._-]")
        removed: list[str] = []
        failed: list[dict] = []
        for row in self._store.session_assets(pid, session_id):
            asset_id = str(row.get("assetId") or "")
            try:
                result = self._remove(pid, actor, {"assetId": asset_id})
            except ServiceError as exc:
                failed.append({"assetId": asset_id, "code": exc.code})
                continue
            if result.get("failed"):
                failed.append({"assetId": asset_id, "code": "cleanup_incomplete"})
            else:
                removed.append(asset_id)
        return {"removed": removed, "failed": failed}

    # ------------------------------------------------------------------ record_chat_copy (legacy)
    def _record_chat_copy(self, pid: str, args: dict) -> dict:
        asset_id = args.get("assetId")
        key = args.get("key")
        if not schema.is_asset_id(asset_id):
            raise ServiceError(404, *_ASSET_NOT_FOUND)
        if not isinstance(key, str) or not CHAT_COPY_KEY_RE.fullmatch(key) or ".." in key:
            raise ServiceError(400, "invalid_args", "key must be artifacts/<sessionId>/input/<name>")
        try:
            row = self._store.add_chat_copy(pid, asset_id, key, bucket=self._ingest_bucket or None)
        except AssetNotFound as exc:
            raise ServiceError(404, *_ASSET_NOT_FOUND) from exc
        return {"asset": asset_public_row(row)}

    # ------------------------------------------------------------------ retry / readd / status
    def _check_catalog(self, kinds: list[str]) -> None:
        """Refuse before writing when M2C lacks a tool the kind needs (Req 8.15, 11.2). Skipped when this
        environment has no M2C configured: then no pipeline exists to run the asset either."""
        if self._catalog is None:
            return
        try:
            listed = self._catalog()
        except Exception as exc:  # noqa: BLE001 — an unreadable catalog refuses, it never passes
            raise ServiceError(503, "catalog_unreadable", catalog.UNREADABLE) from exc
        missing = catalog.missing_tools(kinds, listed, entity_metadata=self._entity_metadata)
        if missing:
            raise ServiceError(503, "catalog_missing", catalog.refusal(missing))

    def _retry(self, pid: str, args: dict) -> dict:
        asset_id = args.get("assetId")
        if not schema.is_asset_id(asset_id):
            raise ServiceError(404, *_ASSET_NOT_FOUND)
        kind = schema.asset_kind(asset_id) or ""
        if kind in ("video", "audio", "image"):
            self._check_catalog([kind])
        try:
            row = self._store.retry_asset(pid, asset_id)
        except AssetNotFound as exc:
            raise ServiceError(404, *_ASSET_NOT_FOUND) from exc
        except WrongStatus as exc:
            raise ServiceError(409, "not_failed", f"Only a failed asset can be retried; it is {exc}") from exc
        except ProjectNotActive as exc:
            raise ServiceError(404, *_PROJECT_NOT_FOUND) from exc
        return {"asset": asset_public_row(row)}

    def _readd(self, pid: str, args: dict) -> dict:
        asset_id = args.get("assetId")
        if not schema.is_asset_id(asset_id):
            raise ServiceError(404, *_ASSET_NOT_FOUND)
        row = self._store.get_asset(pid, asset_id)
        if row is None:
            raise ServiceError(404, *_ASSET_NOT_FOUND)
        if row.get("status") != "removed":
            raise ServiceError(409, "not_removed", f"Only a removed asset can be re-added; it is {row.get('status')}")
        # Removing an uploaded or generated asset deleted its bytes (Projects Phase B), so it can come back
        # only while its source still answers.
        bucket, _, key = str(row.get("sourceUri") or "")[5:].partition("/")
        if not key or self._head_object(bucket, key) is None:
            raise ServiceError(409, "source_gone", "The file was deleted when the asset was removed; upload it again")
        kind = schema.asset_kind(asset_id) or ""
        if kind in ("video", "audio", "image") and indexing_on(self._store.get_meta(pid)):
            self._check_catalog([kind])
        try:
            row = self._store.readd_asset(pid, asset_id)
        except AssetNotFound as exc:
            raise ServiceError(404, *_ASSET_NOT_FOUND) from exc
        except WrongStatus as exc:
            raise ServiceError(409, "not_removed", f"Only a removed asset can be re-added; it is {exc}") from exc
        except RegistrationConflict as exc:
            raise ServiceError(409, "conflict", str(exc)) from exc
        return {"asset": asset_public_row(row)}

    def _project_status(self, pid: str, args: dict) -> dict:
        """Counts per status, Collection_State, the newest failures, and an optional name match (Req 7.14)."""
        meta = self._store.get_meta(pid) or {}
        counts = {k: int((meta.get("statusCounts") or {}).get(k) or 0) for k in _STATUS_KEYS}
        failed = self._store.failed_assets(pid, _FAILED_LISTED)
        out: dict = {
            "counts": counts,
            # OFF: files are stored and findable by name, but nothing is analyzed or searchable.
            "indexing": indexing_on(meta),
            "collectionState": meta.get("collectionState") or "absent",
            "failed": [{"assetId": r.get("assetId"), "displayName": r.get("displayName"),
                        "reason": (r.get("failure") or {}).get("reason")} for r in failed],
            "failedNotListed": max(0, counts["failed"] - len(failed)),
        }
        name = args.get("name")
        if name is not None:
            if not isinstance(name, str) or not (1 <= len(name) <= 255):
                raise ServiceError(400, "invalid_args", "name must be 1-255 characters")
            out["matches"] = [asset_public_row(r) for r in self._store.find_by_name(pid, name)
                              if r.get("status") != "removed"]
        return out

    def _enable_indexing(self, pid: str, actor: str, role: Optional[str]) -> dict:
        """Turn the Project's indexing ON (owner only, one way) and queue every stored file for analysis.

        Idempotent: on a Project already ON it only queues stored files that are left (a crash between the
        flip and the queueing), so the UI can simply call it again. The M2C catalog is checked first for the
        kinds about to be queued, so an environment missing a workflow refuses up front rather than failing
        every file.
        """
        if role != "owner":
            raise ServiceError(403, "owner_only", "Only the project owner can turn on indexing")
        kinds = sorted({schema.asset_kind(str(r.get("assetId"))) or "" for r in self._store.stored_assets(pid)
                        if queueable(r)} & {"video", "audio", "image"})
        if kinds:
            self._check_catalog(kinds)
        try:
            changed = self._store.enable_indexing(pid, by=actor)
            queued = self._store.queue_stored(pid)
        except ProjectNotActive as exc:
            raise ServiceError(404, *_PROJECT_NOT_FOUND) from exc
        return {"indexing": True, "changed": changed, "queued": queued}

    def _rebuild_retry(self, pid: str) -> dict:
        """Only from ``rebuild_failed``: a new Collection_Rebuild from step (a) (Req 10.12, 10.13)."""
        if self._retry_rebuild is None:
            raise ServiceError(503, "rebuild_unavailable", "Collection rebuild is not configured here")
        seq = self._retry_rebuild(pid)
        if seq is None:
            state = (self._store.get_meta(pid) or {}).get("collectionState") or "absent"
            raise ServiceError(409, "not_rebuild_failed", f"Only a failed rebuild can be retried; the collection is {state}")
        return {"collectionState": "rebuilding", "rebuildSeq": int(seq)}

    # ------------------------------------------------------------------ folders (organize)
    def _folder_arg(self, raw: object, name: str) -> Optional[str]:
        """A normalized folder from ``args[name]``: ``None``/``""`` is the root; an invalid path is a 400."""
        if raw is None or raw == "":
            return None
        folder, err = folder_or_error(raw)
        if err:
            raise ServiceError(400, "invalid_folder", f"{name}: {err}")
        return folder

    def _move_assets(self, pid: str, args: dict) -> dict:
        """File each of ``assetIds`` in ``folder`` (root when absent). Metadata only: no bytes move, nothing is
        re-indexed. Returns ``{folder, moved: [ids], failed: [{assetId, code}]}``; a missing or removed asset fails
        alone and never fails the batch."""
        ids = args.get("assetIds")
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
            raise ServiceError(400, "invalid_args", "assetIds must be a non-empty list of ids")
        if len(ids) > _MOVE_MAX:
            raise ServiceError(400, "too_many_ids", f"At most {_MOVE_MAX} ids per call")
        folder = self._folder_arg(args.get("folder"), "folder")
        moved: list[str] = []
        failed: list[dict] = []
        for asset_id in dict.fromkeys(ids):
            try:
                if schema.is_bookmark_id(asset_id):   # a bookmark files like a file
                    self._store.set_bookmark_folder(pid, asset_id, folder)
                else:
                    self._store.set_folder(pid, asset_id, folder)
                moved.append(asset_id)
            except AssetNotFound:
                failed.append({"assetId": asset_id, "code": "asset_not_found"})
        return {"folder": folder or "", "moved": moved, "failed": failed}

    def _rename_folder(self, pid: str, args: dict) -> dict:
        """Rename (or move) a folder: every asset in ``from`` or below it gets the ``to`` prefix instead.

        ``to`` may be the root (its subfolders then come up a level). Moving a folder into itself is refused. A
        path that would exceed the folder limits fails for that asset alone. At most :data:`_RENAME_PAGE_MAX` rows
        per call; ``remaining`` > 0 means call again with the same arguments. ``moves`` names each item's old and
        new folder, which is all an Undo needs (a delete merges a folder into its parent, so ids alone can't)."""
        src = self._folder_arg(args.get("from"), "from")
        if not src:
            raise ServiceError(400, "invalid_args", "from must name a folder")
        dst = self._folder_arg(args.get("to"), "to") or ""
        if dst == src or dst.startswith(f"{src}/"):
            raise ServiceError(400, "invalid_folder", "A folder cannot be moved into itself")
        rows = self._store.assets_under_folder(pid, src) + [
            b for b in self._store.list_bookmarks(pid)
            if isinstance(b.get("folder"), str) and (b["folder"] == src or b["folder"].startswith(f"{src}/"))]
        page, rest = rows[:_RENAME_PAGE_MAX], rows[_RENAME_PAGE_MAX:]
        moved: list[str] = []
        moves: list[dict] = []
        failed: list[dict] = []
        for row in page:
            sk = str(row.get("SK", ""))
            asset_id = sk.split("#", 1)[1] if "#" in sk else sk
            tail = str(row.get("folder", ""))[len(src):].lstrip("/")
            target = "/".join(p for p in (dst, tail) if p)
            folder, err = folder_or_error(target) if target else (None, None)
            if err:
                failed.append({"assetId": asset_id, "code": "invalid_folder"})
                continue
            try:
                if schema.is_bookmark_id(asset_id):
                    self._store.set_bookmark_folder(pid, asset_id, folder)
                else:
                    self._store.set_folder(pid, asset_id, folder)
                moved.append(asset_id)
                moves.append({"assetId": asset_id, "from": str(row.get("folder", "")), "to": folder or ""})
            except AssetNotFound:
                failed.append({"assetId": asset_id, "code": "asset_not_found"})
        if not rest:
            # Recorded (possibly empty) folders follow: each one at or under `from` is re-keyed under `to`; the
            # top-level `to` itself needs no row when it is the root.
            for path in self._store.list_folder_rows(pid):
                if path == src or path.startswith(f"{src}/"):
                    target = "/".join(p for p in (dst, path[len(src):].lstrip("/")) if p)
                    folder, err = folder_or_error(target) if target else (None, None)
                    if folder and not err:
                        self._store.create_folder(pid, folder, created_by="rename")
                    self._store.remove_folder_row(pid, path)
        return {"from": src, "to": dst, "moved": moved, "moves": moves, "failed": failed, "remaining": len(rest)}

    def _create_folder(self, pid: str, actor: str, args: dict) -> dict:
        """Record ``folder`` so it exists (for every member, on every device) before anything is filed in it."""
        folder = self._folder_arg(args.get("folder"), "folder")
        if not folder:
            raise ServiceError(400, "invalid_args", "folder must name a folder")
        self._store.create_folder(pid, folder, created_by=actor)
        return {"folder": folder}

    def _create_bookmarks(self, pid: str, actor: str, args: dict) -> dict:
        """Save moments as bookmarks in ``folder``: ``items: [{assetId, startMs, endMs?, label?}]`` (<= 100).

        A bookmark references its asset with a range; nothing is cut, copied or indexed. An item whose asset is
        missing or removed fails alone. ``{folder, created: [bookmark views], failed: [{assetId, code}]}``."""
        items = args.get("items")
        if not isinstance(items, list) or not items:
            raise ServiceError(400, "invalid_args", "items must be a non-empty list")
        if len(items) > _MOVE_MAX:
            raise ServiceError(400, "too_many_ids", f"At most {_MOVE_MAX} items per call")
        folder = self._folder_arg(args.get("folder"), "folder")
        session_id = args.get("sessionId") if isinstance(args.get("sessionId"), str) else None
        created: list[dict] = []
        failed: list[dict] = []
        for it in items:
            if not isinstance(it, dict):
                raise ServiceError(400, "invalid_args", "each item must be an object")
            aid, start, end = it.get("assetId"), it.get("startMs"), it.get("endMs")
            ok_int = lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0  # noqa: E731
            if not ok_int(start) or (end is not None and (not ok_int(end) or end <= start)):
                raise ServiceError(400, "invalid_range", "startMs must be >= 0 and endMs, when given, after it")
            label = it.get("label") if isinstance(it.get("label"), str) else ""
            try:
                row = self._store.create_bookmark(pid, asset_id=str(aid), start_ms=start, end_ms=end,
                                                  label=label.strip()[:200], folder=folder, created_by=actor,
                                                  session_id=session_id)
                created.append(bookmark_view(row))
            except AssetNotFound:
                failed.append({"assetId": aid, "code": "asset_not_found"})
        return {"folder": folder or "", "created": created, "failed": failed}

    def _remove_bookmarks(self, pid: str, args: dict) -> dict:
        """Delete bookmarks (their assets are untouched): ``{removed: [ids], missing: [ids]}``."""
        ids = args.get("bookmarkIds")
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
            raise ServiceError(400, "invalid_args", "bookmarkIds must be a non-empty list of ids")
        if len(ids) > _MOVE_MAX:
            raise ServiceError(400, "too_many_ids", f"At most {_MOVE_MAX} ids per call")
        removed, missing = [], []
        for bid in dict.fromkeys(ids):
            (removed if self._store.remove_bookmark(pid, bid) else missing).append(bid)
        return {"removed": removed, "missing": missing}

    # ------------------------------------------------------------------ lineage (projects.lineageEdges)
    def _lineage(self, pid: str, actor: str, args: dict) -> dict:
        """The Project's lineage graph: one asset's ancestors / descendants (``assetId`` + ``direction``), or the
        whole Project (``direction: project``). ``depth`` (1-50, default 10) and ``maxNodes`` (1-2000, default 500)
        bound it (``truncated`` says when they cut). Each node is checked like an asset read: another member's
        "this chat only" file is never a node (its edges are contracted), and the focus itself is a uniform 404
        when it is not this actor's to see. ``sessionId`` reveals the caller's own hidden files of THAT chat."""
        from projects import lineage_contract as lc

        from ..lineage import DEFAULT_DEPTH, DEFAULT_NODES, DIRECTIONS, MAX_DEPTH, MAX_NODES, LineageReader

        if not lc.edges_enabled():
            raise ServiceError(404, "lineage_not_enabled", "Lineage is not enabled in this environment")
        direction = args.get("direction", "both" if args.get("assetId") else "project")
        if direction not in DIRECTIONS:
            raise ServiceError(400, "invalid_args", f"direction must be one of {', '.join(DIRECTIONS)}")
        asset_id = args.get("assetId")
        if direction != "project":
            if not schema.is_asset_id(asset_id):
                raise ServiceError(400, "invalid_args", "assetId must be an asset id (or use direction: project)")
        else:
            asset_id = None

        def bounded(name: str, default: int, hi: int) -> int:
            v = args.get(name, default)
            if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= hi:
                raise ServiceError(400, "invalid_args", f"{name} must be an integer 1-{hi}")
            return v

        depth = bounded("depth", DEFAULT_DEPTH, MAX_DEPTH)
        max_nodes = bounded("maxNodes", DEFAULT_NODES, MAX_NODES)
        session_id = args.get("sessionId")
        if session_id is not None and (not isinstance(session_id, str) or not CHAT_SESSION_ID_RE.fullmatch(session_id)):
            raise ServiceError(400, "invalid_args", "sessionId must be 1-128 of [A-Za-z0-9._-]")
        try:
            return LineageReader(self._store.table, self._store).lineage(
                pid, actor, asset_id=asset_id, direction=direction, depth=depth, max_nodes=max_nodes,
                session_id=session_id)
        except LookupError as exc:
            raise ServiceError(404, *_ASSET_NOT_FOUND) from exc

    # ------------------------------------------------------------------ import inbox
    def _import_status(self, pid: str) -> dict:
        """Where to copy files (``bucket``, ``prefix``), what the inbox did with them, and the project's progress."""
        from ..import_inbox import import_prefix, import_status

        meta = self._store.get_meta(pid) or {}
        status = import_status(self._store, pid)
        return {"bucket": self._ingest_bucket, "prefix": import_prefix(pid), **status,
                "statusCounts": meta.get("statusCounts") or {}, "indexing": indexing_on(meta)}

    def _rescan_import(self, pid: str) -> dict:
        """Sweep the inbox now, every object (and re-check the ones that needed attention)."""
        from ..import_inbox import sweep_project

        if not self._ingest_bucket:
            raise ServiceError(503, "unavailable", "No ingest bucket is configured")
        return sweep_project(self._store, self._s3, self._ingest_bucket, pid, full=True, limits=self._limits)

    def _resolve_locations(self, pid: str, args: dict) -> dict:
        """``{locations: {<location>: assetId | null}}`` for chat/Renderer URLs of this Project's assets.

        Accepts what the chat shows: an ``s3://`` uri, an artifacts-CDN URL or bare key (``projects/…``,
        ``artifacts/…``, including an ``artifacts/m2c/<bucket>/<key>`` M2C reference), or a presigned S3 URL. An
        asset key (``projects/<pid>/<assetId>/…``) names its id directly; anything else is looked up by its SRC
        guard, the one record that maps a location to the asset registered there."""
        from ..generated import asset_id_in_project_key, parse_location

        locs = args.get("locations")
        if not isinstance(locs, list) or not all(isinstance(x, str) for x in locs):
            raise ServiceError(400, "invalid_args", "locations must be a list of strings")
        if len(locs) > _MOVE_MAX:
            raise ServiceError(400, "too_many_ids", f"At most {_MOVE_MAX} locations per call")
        out: dict[str, Optional[str]] = {}
        for loc in dict.fromkeys(locs):
            out[loc] = None
            parsed = parse_location(loc, self._ingest_bucket or "")
            if not parsed:
                continue
            bucket, key = parsed
            if key.startswith(_M2C_REF_PREFIX):  # artifacts/m2c/<bucket>/<key>: the object M2C wrote, in place
                bucket, _, key = key[len(_M2C_REF_PREFIX):].partition("/")
                if not bucket or not key:
                    continue
            aid = (asset_id_in_project_key(key, pid) or self._store.asset_id_for_location(pid, bucket, key)
                   or self._job_output_copy_asset(pid, key))
            row = self._store.get_asset(pid, aid) if aid else None
            if row is not None and row.get("status") != "removed":
                out[loc] = aid
        return {"locations": out}

    def _job_output_copy_asset(self, pid: str, key: str) -> Optional[str]:
        """The asset a session's COPY of an M2C job output stands for, or ``None``.

        When a job settles, its outputs are copied into the session's output prefix under the execution id
        (``<session output>/<execution id>/<file>``, the polling worker / plan chainer importers) so their links
        do not expire, while the asset stays registered where M2C wrote it. A Renderer tab or chat link of the
        copy therefore matched no SRC guard and showed no location. The JOB record names the original's prefix
        (``outputPrefix``) and its ``outputs``: the copy resolves to the output at ``<prefix><file>``, else to
        the one output with the same file name.
        """
        m = _JOB_OUTPUT_COPY_RE.match(key or "")
        if not m:
            return None
        execution_id, rel = m.group("exec"), m.group("rel")
        job = self._store.job_for_execution(pid, execution_id)
        if not job:
            return None
        prefix = str(job.get("outputPrefix") or "")
        if prefix.startswith("s3://") and "/" in prefix[5:]:
            p_bucket, _, p_key = prefix[5:].partition("/")
            aid = self._store.asset_id_for_location(pid, p_bucket, f"{p_key}{rel}")
            if aid:
                return aid
        name = rel.rsplit("/", 1)[-1]
        matches = []
        for out_id in list(job.get("outputs") or [])[:200]:
            row = self._store.get_asset(pid, out_id) if schema.is_asset_id(out_id) else None
            if row and str(row.get("sourceUri") or "").rsplit("/", 1)[-1] == name:
                matches.append(out_id)
        return matches[0] if len(matches) == 1 else None

    # ------------------------------------------------------------------ capabilities
    def _capabilities(self) -> dict:
        return {
            "maxFilesPerBatch": MAX_FILES_PER_BATCH,
            "limits": dict(self._limits),
            "multipartThresholdBytes": MULTIPART_THRESHOLD_BYTES,
            "partSizeBytes": PART_SIZE_BYTES,
        }

    # ------------------------------------------------------------------ shared write path
    def _register(self, **kw) -> dict:
        actor = kw.pop("actor")
        try:
            return self._store.register_asset(
                added_by=actor, added_by_display=self._actor_display(actor), **kw
            )
        except RegistrationConflict as exc:
            raise ServiceError(409, "already_registered", str(exc)) from exc
        except ProjectNotActive as exc:
            raise ServiceError(404, *_PROJECT_NOT_FOUND) from exc

    # ------------------------------------------------------------------ seed_personal_samples
    def _seed_personal_samples(self, pid: str, actor: str, role: Optional[str]) -> dict:
        """Copy the configured samples (``PERSONAL_SAMPLES``) into the caller's OWN Personal project, pinned.

        Owner-only and Personal-only (see ``ingestion.personal_samples``): a member, or any other project, is a
        no-op. No samples configured is a no-op too, so the proxy may call this in every env.
        """
        samples = self._personal_samples if self._personal_samples is not None else samples_from_env()
        if role != "owner" or not samples:
            return {"seeded": [], "skipped": []}
        return seed_personal_samples(store=self._store, s3=self._s3, bucket=self._ingest_bucket, pid=pid,
                                     actor=actor, samples=samples, register=self._register)

    def _actor_display(self, actor: str) -> str:
        # P0: the MEMBER.display lookup (Req 3 "addedByDisplay") lands with the Projects_Surface wave;
        # until then the Actor sub stands in and is overwritten on the first list render.
        return actor

    def _head_object(self, bucket: str, key: str) -> Optional[dict]:
        try:
            return self._s3.head_object(Bucket=bucket, Key=key)
        except Exception:  # noqa: BLE001 — a 404/403 head means "no verifiable object" (Req 1.19)
            return None


# Commands owned by this spec but not built yet; they answer a clear unknown_command.
_ALL_FUTURE_COMMANDS: frozenset = frozenset()
_SEARCH_IMAGES_MAX = 5


# An image ingestion will render shortly (its run is pending or in flight) is "not ready"; one it never will
# (a "this chat only" upload, a stored or failed asset) is rendered on demand by ``search_images``.
_RENDERED_BY_INGESTION = frozenset({"queued", "analyzing", "indexing"})


def _needs_rendition_now(row: Optional[Mapping], session_id: str) -> bool:
    if not isinstance(row, Mapping) or row.get("kind") != "image" or isinstance(row.get("rendition"), Mapping):
        return False
    if row.get("status") == "removed" or row.get("status") in _RENDERED_BY_INGESTION:
        return False
    return not (row.get("visibility") == "session" and row.get("sessionId") != session_id)


def _split_s3_uri(uri: str) -> tuple[str, str]:
    bucket, _, key = uri[5:].partition("/") if uri.startswith("s3://") else ("", "", "")
    if not bucket or not key:
        raise ValueError("not an s3:// uri")
    return bucket, key


def _search_image(row: Optional[Mapping], session_id: str, ingest_bucket: str) -> dict:
    """One ``search_images`` entry: the rendition URI, or ``{"reason"}`` (see ``AssetService._search_images``)."""
    if (not isinstance(row, Mapping) or row.get("status") == "removed"
            or (row.get("visibility") == "session" and row.get("sessionId") != session_id)):
        return {"reason": "not_found"}
    name = row.get("displayName") if isinstance(row.get("displayName"), str) else ""
    if row.get("kind") != "image":
        return {"reason": "not_image", "kind": str(row.get("kind") or ""), "displayName": name}
    if row.get("_unreadable"):
        return {"reason": "unreadable", "displayName": name}
    made = row.get("rendition") if isinstance(row.get("rendition"), Mapping) else {}
    key = made.get("key")
    if not isinstance(key, str) or not key:
        return {"reason": "not_ready", "displayName": name}
    return {"uri": f"s3://{ingest_bucket}/{key}", "displayName": name}
_STATUS_KEYS = ("stored", "queued", "analyzing", "indexing", "indexed", "failed", "removed")
_FAILED_LISTED = 20


def _session_visibility(args: dict) -> Optional[str]:
    """The session id of a ``visibility=session`` registration ("this chat only"), else ``None``.

    Only ``"session"`` may be asked for: ``project`` is the default an absent field already means, and any other
    value is a caller bug rather than something to guess at.
    """
    visibility = args.get("visibility")
    if visibility is None:
        return None
    if visibility != "session":
        raise ServiceError(400, "invalid_args", "visibility must be \"session\"")
    session_id = args.get("sessionId")
    if not isinstance(session_id, str) or not CHAT_SESSION_ID_RE.fullmatch(session_id):
        raise ServiceError(400, "invalid_args", "sessionId must be 1-128 of [A-Za-z0-9._-]")
    return session_id


def _session_asset_folder(args: dict) -> str:
    """``Sessions/<title> · <YYYY-MM-DD>`` for a session-visibility upload (asset-lineage R3.3).

    ``sessionTitle``/``sessionCreatedAt`` are read from the session row by the caller and are display only:
    :func:`ingestion.folders.session_folder` sanitizes the title and falls back to
    ``Sessions/Session <sid[:8]> · <today>`` when there is none, so a session output and a "this chat only"
    file of the same chat land in the same folder.
    """
    session_id = args.get("sessionId")
    day = date_from(args.get("sessionCreatedAt")) or datetime.now(timezone.utc).date().isoformat()
    return session_folder(title=args.get("sessionTitle"), session_id=session_id, day=day)


def _folder_from_metadata(head: Optional[dict]) -> Optional[str]:
    """The folder the proxy signed into an upload (``x-amz-meta-harness-folder``, percent-encoded).

    Anything unreadable or invalid is the project root: the upload itself is good, and a folder is only
    where it is shown, so it never fails a registration.
    """
    raw = ((head or {}).get("Metadata") or {}).get(FOLDER_METADATA_KEY)
    if not isinstance(raw, str) or not raw:
        return None
    try:
        decoded = unquote(raw, errors="strict")
    except UnicodeDecodeError:
        return None
    folder, _reason = folder_or_error(decoded)
    return folder


# ---------------------------------------------------------------------------------------------------
# Deployed entrypoint
# ---------------------------------------------------------------------------------------------------
def _default_service() -> AssetService:
    import boto3

    table_name = os.environ.get("PROJECTS_TABLE", "")
    region = os.environ.get("AWS_REGION", "us-west-2")
    ingest_bucket = os.environ.get("INGEST_BUCKET", "")
    store = AssetStore.open(table_name, region_name=region)
    verifier = _default_verifier(region)
    catalog_tools = None
    if os.environ.get("M2C_GATEWAY_URL"):
        from ..m2c import M2CClient, M2CConfig

        catalog_tools = M2CClient(M2CConfig.from_env()).list_tools  # cached 5 min on success
    request_rebuild = retry_rebuild = None
    rebuild_arn = os.environ.get("COLLECTION_REBUILD_SFN_ARN", "")
    if rebuild_arn:
        from ..store import _iso_now

        sfn = boto3.client("stepfunctions", region_name=region)

        def request_rebuild(pid: str, reason: str) -> str:
            return rebuild.request_rebuild(store.table, sfn, rebuild_arn, pid, reason=reason,
                                           now_iso=_iso_now(time.time))

        def retry_rebuild(pid: str):
            return rebuild.retry_rebuild(store.table, sfn, rebuild_arn, pid, now_iso=_iso_now(time.time))
    return AssetService(
        store=store,
        verifier=verifier,
        s3_client=boto3.client("s3", region_name=region),
        ingest_bucket=ingest_bucket,
        allowlist=allowlist_from_env(),
        catalog_tools=catalog_tools,
        request_rebuild=request_rebuild,
        retry_rebuild=retry_rebuild,
    )


def _default_verifier(region: str):
    # token-issuer/cognito.py is bundled at the package root by the deploy packager (design §20).
    from cognito import CognitoUserTokenVerifier  # type: ignore

    return CognitoUserTokenVerifier(
        region=region,
        user_pool_id=os.environ.get("USER_POOL_ID", ""),
        client_id=os.environ.get("USER_POOL_CLIENT_ID", ""),
    )


_SERVICE: Optional[AssetService] = None


def handler(event, context=None) -> dict:  # noqa: ARG001 — Lambda signature
    """Lambda entrypoint. Builds the service once per container, then dispatches the event."""
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = _default_service()
    return _SERVICE.handle(event)
