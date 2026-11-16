"""Project_Asset row reader/writer over the projects table (design §2 "the Python registry").

Every Project_Asset write outside the pipeline goes through the Asset_Service Lambda, and every one of
those writes goes through this module. It owns:

- the read side the Asset_Service ``resolve_assets`` and the proxy Asset_URL_Route depend on
  (``batch_get_assets`` / ``get_asset``), always a consistent read of ``PROJECT#<pid> / ASSET#<id>``
  rows so an asset of another Project is simply absent (Req 3.11);
- the registration transaction (design §2 "Registration transaction"): one ``TransactWriteItems`` per
  asset putting the ASSET row, the SRC guard, the DNAME guard, the QUEUE item, and the META counter
  update, each conditioned so ``statusCounts`` stays exactly the per-status count of the rows
  (Property 6), plus the Display_Name ` (n)` allocation of Req 3.5/3.6.

``table`` is a boto3 DynamoDB **resource** ``Table`` (``boto3.resource("dynamodb").Table(name)``); its
``meta.client`` carries boto3's DynamoDB type transforms, so ``batch_get_item`` and
``transact_write_items`` take and return plain Python values (the same pattern as
``projects.access`` and ``lab.asset_index``). No ``{"S": ...}`` wrappers appear here.
"""
from __future__ import annotations

import logging
import re
import time
from decimal import Decimal
from typing import Any, Mapping, Optional, Sequence

from botocore.exceptions import ClientError

from projects import schema
from projects.ids import is_project_id

from .folders import folder_path_text, normalize_folder
from .names import derive_display_name, with_suffix

log = logging.getLogger(__name__)

# `find_by_name` collects EVERY match before ranking, bounded so a huge project cannot run away. 50 query
# pages is thousands of rows (DynamoDB returns up to 1 MB per page); matches past it are not seen.
_FIND_SCAN_MAX_PAGES = 50


def _name_rank(row: Mapping[str, Any], needle: str) -> int:
    """Relevance of a row to a folded name query, lower is better: 0 exact Display_Name, 1 prefix, 2 substring,
    3 matched only via its folder path. Twin of the proxy's `nameScore` (ui/server/projects/ingestionRoutes.ts)."""
    name = schema.fold_name(str(row.get("displayName") or ""))
    # A name typed without its extension is still the exact name (the original `.mp4` before its `_clip_*` copies).
    if name == needle or re.sub(r"\.[^./]{1,8}$", "", name) == needle:
        return 0
    if name.startswith(needle):
        return 1
    if needle in name:
        return 2
    return 3

__all__ = [
    "AssetStore",
    "ProjectStore",
    "RegistrationConflict",
    "ProjectNotActive",
    "AssetNotFound",
    "WrongStatus",
    "asset_public_view",
    "asset_public_row",
    "checksum_from_head",
    "jsonable",
    "queueable",
    "CHAT_COPY_KEY_RE",
    "CHAT_SESSION_ID_RE",
    "DNAME_ALLOCATION_SLOTS",
    "MAX_DERIVED_FROM",
    "NON_INDEXED_ROLES",
    "ROLES",
    "STATUS_COUNT_KEYS",
    "VISIBILITIES",
    "indexing_on",
    "graph_on",
]

# How many ` (n)` candidates (base, base (2) .. base (N)) are considered per allocation (Req 3.5).
DNAME_ALLOCATION_SLOTS = 21
# Attempts before giving up on a Display_Name allocation that keeps losing its guard race (Req 3.5).
_MAX_ALLOC_ATTEMPTS = 5
# Full-jitter backoff is a product concern; the base here is small and deterministic for tests.
_RETRY_BASE_SLEEP_S = 0.0


class RegistrationConflict(Exception):
    """A guard the registration transaction expected to be free was taken (SRC, DNAME, or ASSET)."""


class ProjectNotActive(Exception):
    """META is absent, a tombstone, or not ``active`` — a deleted/purged Project registers nothing."""


class AssetNotFound(Exception):
    """No ASSET row (or a removed one, for writes that need a live asset) — the uniform not-found."""


class WrongStatus(Exception):
    """A retry or re-add of an asset that is not ``failed`` / ``removed``; the message is its current status."""


# A trailing " (n)" Display_Name suffix (Req 3.5), dropped before a re-add re-derives the name.
_SUFFIX_RE = re.compile(r" \((\d+)\)\Z")


# The per-status leaves of META ``statusCounts``. The pipeline's statuses (analyzing/indexed) and the
# Projects-surface ones (uploading/ready) are both listed so a decrement of any of them has a leaf.
STATUS_COUNT_KEYS = (
    "uploading", "queued", "analyzing", "indexing", "indexed", "ready", "failed", "removed", "stored",
)

# Asset lineage (asset-lineage design §2). An ASSET row without `role` is a legacy `source` row and one
# without `visibility` is project-visible; both are only ever WRITTEN when the registration names them.
ROLES = frozenset({"source", "deliverable", "analysis", "intermediate"})
VISIBILITIES = frozenset({"project", "session"})
# These roles are registered `stored` and never queued for indexing, whatever META.indexing says (R3.5).
NON_INDEXED_ROLES = frozenset({"analysis", "intermediate"})
MAX_DERIVED_FROM = 20


def queueable(row: Optional[Mapping[str, Any]]) -> bool:
    """Whether an ASSET row may ever be queued for indexing.

    An analysis/intermediate row never is, nor one a run already found unindexable (``indexSkippedReason``, e.g.
    an audio stem with no speech): queueing it again would only rediscover that, so turning indexing on leaves it
    ``stored``."""
    if not isinstance(row, Mapping):
        return True
    return row.get("role") not in NON_INDEXED_ROLES and not row.get("indexSkippedReason")


def checksum_from_head(head: Optional[Mapping[str, Any]]) -> Optional[dict]:
    """``{algo, value, size}`` from a ``HeadObject(ChecksumMode="ENABLED")`` reply, else ``None``.

    S3's full-object ``CRC64NVME`` (the default on new objects) is preferred, then ``SHA256``. An object
    with neither (some M2C outputs, or a CRC32-only upload) has no checksum and skips the HASH guard.
    """
    if not isinstance(head, Mapping):
        return None
    size = head.get("ContentLength")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        return None
    for algo, field in (("CRC64NVME", "ChecksumCRC64NVME"), ("SHA256", "ChecksumSHA256")):
        value = head.get(field)
        if isinstance(value, str) and value:
            return {"algo": algo, "value": value, "size": size}
    return None


def indexing_on(meta: Optional[Mapping[str, Any]]) -> bool:
    """Whether a Project ingests its assets (Project settings, ``META.indexing``).

    New Projects are written with ``indexing: false`` (the default is OFF) and the owner can only turn it
    ON. A META row with no ``indexing`` attribute predates the setting and was already ingesting, so it
    counts as ON. Anything else (``false``, a malformed value) is OFF.
    """
    if not isinstance(meta, Mapping) or "indexing" not in meta:
        return True
    return meta.get("indexing") is True


def graph_on(meta: Optional[Mapping[str, Any]]) -> bool:
    """Whether a Project has a knowledge graph (Project settings, ``META.graph``).

    Unlike ``indexing``, an absent attribute is OFF: no Project had a graph before the setting existed, and a
    deploy without the graph feature never writes the attribute at all. Only ``True`` is ON.
    """
    return isinstance(meta, Mapping) and meta.get("graph") is True

# A chat copy of a Project_Asset lived under a session's INPUT prefix (``ui/server/uploadKey.ts``):
# ``artifacts/<sessionId>/input/<leaf>``. Phase 2 attaches by reference instead (``add_chat_session``), so no
# new copy is ever made; the shape is still enforced for the legacy keys ``remove`` must still delete.
CHAT_COPY_KEY_RE = re.compile(r"^artifacts/[A-Za-z0-9._-]+/input/[^/]+$")
# A chat session id, the same safe class the proxy accepts (``ui/server/projects/uploadRoutes.ts``).
CHAT_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def jsonable(value: Any) -> Any:
    """A DynamoDB-resource value made JSON-serializable: Decimal → int/float, set → sorted list.

    A Lambda reply is serialized by the runtime with plain ``json``, which refuses ``Decimal`` (every
    number boto3 reads back) and ``set`` (a string set such as ``chatCopyKeys``).
    """
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (set, frozenset)):
        return sorted(jsonable(v) for v in value)
    if isinstance(value, Mapping):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def asset_public_row(row: Mapping[str, Any]) -> dict:
    """An ASSET row without its table keys, JSON-serializable (what Asset_Service replies carry)."""
    return {k: jsonable(v) for k, v in row.items() if k not in ("PK", "SK")}


def asset_public_view(row: Mapping[str, Any]) -> Optional[dict]:
    """The ``{kind, displayName, status}`` an ok ``resolve_assets`` entry carries, or ``None``.

    ``None`` for a malformed row (missing/!str kind, displayName, or status, or an unknown kind), so a
    corrupt row is treated as absent exactly like the agent-side resolver client expects.
    """
    if not isinstance(row, Mapping):
        return None
    kind = row.get("kind")
    display_name = row.get("displayName")
    status = row.get("status")
    if not (isinstance(kind, str) and isinstance(display_name, str) and isinstance(status, str)):
        return None
    if kind not in schema.ASSET_KINDS:
        return None
    return {"kind": kind, "displayName": display_name, "status": status}


class AssetStore:
    """Reader/writer of Project_Asset rows for one projects table."""

    def __init__(self, table: Any, *, clock=time.time, sleep=time.sleep):
        self.table = table
        self.client = table.meta.client
        self.name = table.name
        self._clock = clock
        self._sleep = sleep

    @classmethod
    def open(
        cls,
        table_name: str,
        *,
        endpoint_url: Optional[str] = None,
        region_name: Optional[str] = None,
    ) -> "AssetStore":
        import boto3

        if not isinstance(table_name, str) or not table_name:
            raise ValueError("AssetStore.open needs a non-empty table name (PROJECTS_TABLE)")
        resource = boto3.resource("dynamodb", endpoint_url=endpoint_url or None, region_name=region_name)
        return cls(resource.Table(table_name))

    # ---------------------------------------------------------------- reads
    def get_asset(self, pid: str, asset_id: str) -> Optional[dict]:
        """One consistent ``GetItem`` of ``ASSET#<assetId>``; ``None`` if malformed or absent."""
        if not is_project_id(pid) or not schema.is_asset_id(asset_id):
            return None
        resp = self.table.get_item(Key=schema.asset_key(pid, asset_id), ConsistentRead=True)
        item = resp.get("Item")
        return item if isinstance(item, dict) else None

    def get_meta(self, pid: str) -> Optional[dict]:
        resp = self.table.get_item(Key=schema.meta_key(pid), ConsistentRead=True)
        item = resp.get("Item")
        return item if isinstance(item, dict) else None

    def asset_id_for_location(self, pid: str, bucket: str, key: str) -> Optional[str]:
        """The asset id tracking ``s3://<bucket>/<key>`` in this Project, from its SRC guard (one GetItem, R1.2)."""
        if not is_project_id(pid) or not isinstance(bucket, str) or not bucket or not isinstance(key, str) or not key:
            return None
        item = self.table.get_item(Key=schema.src_guard_key(pid, bucket, key), ConsistentRead=True).get("Item")
        aid = (item or {}).get("assetId")
        return aid if schema.is_asset_id(aid) else None

    def get_job(self, pid: str, job_id: str) -> Optional[dict]:
        """The ``JOB#<jobId>`` record, or ``None``."""
        if not is_project_id(pid) or not isinstance(job_id, str) or not job_id:
            return None
        item = self.table.get_item(Key=schema.job_key(pid, job_id), ConsistentRead=True).get("Item")
        return item if isinstance(item, dict) else None

    def put_job(self, pid: str, job: Mapping[str, Any]) -> dict:
        """Write the ``JOB#<jobId>`` record (design §2; R4.1). Idempotent: a replay merges onto the existing
        record, unions ``outputs`` and keeps the first ``createdAt``. ``None`` fields are not written."""
        job_id = job.get("jobId")
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("jobId is required")
        existing = self.get_job(pid, job_id) or {}
        outputs = list(dict.fromkeys([*(existing.get("outputs") or []), *(job.get("outputs") or [])]))
        item = {
            **existing,
            **{k: v for k, v in job.items() if v is not None},
            **schema.job_key(pid, job_id),
            "jobId": job_id,
            "role": "analysis",
            "outputs": outputs,
            "createdAt": existing.get("createdAt") or job.get("createdAt") or _iso_now(self._clock),
        }
        self.table.put_item(Item=item)
        return item

    def batch_get_assets(self, pid: str, asset_ids: Sequence[str]) -> dict[str, dict]:
        """Consistent ``BatchGetItem`` of ``ASSET#<id>`` rows; ``{asset_id: row}``.

        Malformed ids are dropped before the read; absent and other-Project ids are simply not in the
        response (both keys name ``PROJECT#<pid>``). Pages at the DynamoDB 100-key BatchGet limit and
        re-requests ``UnprocessedKeys`` once per page.
        """
        if not is_project_id(pid):
            return {}
        wanted = [aid for aid in dict.fromkeys(asset_ids) if schema.is_asset_id(aid)]
        out: dict[str, dict] = {}
        for i in range(0, len(wanted), 100):
            page = wanted[i : i + 100]
            keys = [schema.asset_key(pid, aid) for aid in page]
            for item in self._batch_get(keys):
                sk = item.get("SK")
                if isinstance(sk, str) and sk.startswith(schema.ASSET_SK_PREFIX):
                    out[sk[len(schema.ASSET_SK_PREFIX) :]] = item
        return out

    def _batch_get(self, keys: list[dict]) -> list[dict]:
        items: list[dict] = []
        request = {self.name: {"Keys": keys, "ConsistentRead": True}}
        resp = self.client.batch_get_item(RequestItems=request)
        items.extend(resp.get("Responses", {}).get(self.name, []))
        unprocessed = (resp.get("UnprocessedKeys") or {}).get(self.name, {}).get("Keys")
        if unprocessed:
            resp = self.client.batch_get_item(RequestItems={self.name: {"Keys": unprocessed, "ConsistentRead": True}})
            items.extend(resp.get("Responses", {}).get(self.name, []))
        return items

    def _present_dname_guards(self, pid: str, candidates: list[str]) -> set[str]:
        """The subset of ``candidates`` whose DNAME guard already exists (one consistent BatchGet)."""
        key_by_sk = {schema.dname_guard_key(pid, c)["SK"]: c for c in candidates}
        keys = [{"PK": schema.project_pk(pid), "SK": sk} for sk in key_by_sk]
        taken: set[str] = set()
        for item in self._batch_get(keys):
            sk = item.get("SK")
            cand = key_by_sk.get(sk) if isinstance(sk, str) else None
            if cand is not None:
                taken.add(cand)
        return taken

    def _allocate_display_name(self, pid: str, base: str) -> str:
        """The smallest free ``base (n)`` (n>=1) among the first :data:`DNAME_ALLOCATION_SLOTS` slots."""
        candidates = [with_suffix(base, n) for n in range(1, DNAME_ALLOCATION_SLOTS + 1)]
        taken = self._present_dname_guards(pid, candidates)
        for cand in candidates:
            if cand not in taken:
                return cand
        # All slots taken: fall back to the last; the transaction's DNAME guard will reject it and the
        # caller retries, which re-reads the (by then possibly freed) guards.
        return candidates[-1]

    # ---------------------------------------------------------------- writes
    def register_asset(
        self,
        *,
        pid: str,
        asset_id: str,
        source: str,
        source_uri: str,
        source_bucket: str,
        source_key: str,
        source_size: int,
        content_type: str,
        file_name: str,
        added_by: str,
        added_by_display: str,
        extra_attrs: Optional[Mapping[str, Any]] = None,
        folder: Optional[str] = None,
        role: Optional[str] = None,
        visibility: Optional[str] = None,
        session_id: Optional[str] = None,
        producer: Optional[Mapping[str, Any]] = None,
        derived_from: Optional[Sequence[str]] = None,
        job_id: Optional[str] = None,
        checksum: Optional[Mapping[str, Any]] = None,
        queue_for_indexing: Optional[bool] = None,
    ) -> dict:
        """Register one Project_Asset (design §2 "Registration transaction"). Idempotent on ``asset_id``.

        ``extra_attrs`` are written onto the ASSET row as given (Generated_Media's ``originUri`` and kin);
        they can never replace a key the registration itself sets.

        ``folder`` (design D20) is normalized with :func:`ingestion.folders.normalize_folder` and written as
        the row's ``folder`` attribute; ``None``/empty is the project root (no attribute). An invalid folder
        raises :class:`ingestion.folders.InvalidFolder` before anything is written. The Display_Name stays
        unique per Project, not per folder (decision 1).

        Lineage (asset-lineage design §2), each written only when given: ``role`` (one of :data:`ROLES`),
        ``visibility`` (:data:`VISIBILITIES`), ``sessionId``, ``producer`` (``{sessionId, turnId?, tool?,
        jobId?}``), ``derivedFrom`` (well-formed asset ids other than this one, at most
        :data:`MAX_DERIVED_FROM`), ``jobId``, and ``checksum`` (``{algo, value, size}``, see
        :func:`checksum_from_head`). ``queue_for_indexing``: ``None`` follows ``META.indexing``; ``False``
        registers ``stored`` whatever the flag says. An ``analysis``/``intermediate`` role is never queued.

        With a ``checksum`` the transaction also claims ``HASH#<algo>:<value>:<size>`` (R5); when another live
        asset of the Project already holds it, nothing is written and that asset's row is returned with
        ``deduped: True``.

        Returns the ASSET row (the newly written one, or the pre-existing one for a repeated
        ``asset_id`` — Req 1.17-1.19). Raises :class:`ProjectNotActive` if META is gone/not active and
        :class:`RegistrationConflict` if the SRC guard (another asset already owns this bucket+key) or
        the Display_Name allocation cannot converge.
        """
        if not is_project_id(pid):
            raise ValueError(f"not a project id: {pid!r}")
        kind = schema.asset_kind(asset_id)
        if kind is None:
            raise ValueError(f"not an asset id: {asset_id!r}")
        if role is not None and role not in ROLES:
            raise ValueError(f"not an asset role: {role!r}")
        if visibility is not None and visibility not in VISIBILITIES:
            raise ValueError(f"not an asset visibility: {visibility!r}")
        hash_key = _hash_key(pid, checksum)

        folder = normalize_folder(folder)

        existing = self.get_asset(pid, asset_id)
        if existing is not None:
            return existing

        lineage = _lineage_attrs(asset_id=asset_id, role=role, visibility=visibility, session_id=session_id,
                                 producer=producer, derived_from=derived_from, job_id=job_id,
                                 checksum=checksum if hash_key else None)
        never_queue = queue_for_indexing is False or role in NON_INDEXED_ROLES

        meta = self._ensure_meta_counts(pid)
        base = derive_display_name(file_name, kind)

        last_error: Optional[ClientError] = None
        for attempt in range(_MAX_ALLOC_ATTEMPTS):
            if attempt:
                meta = self.get_meta(pid) or meta  # a lost race may have been the indexing flag flipping
            indexing = indexing_on(meta)
            queued = indexing and not never_queue
            display_name = self._allocate_display_name(pid, base)
            registered_at = _iso_now(self._clock)
            queued_at_ms = int(self._clock() * 1000)
            item = _asset_item(
                pid=pid,
                asset_id=asset_id,
                kind=kind,
                source=source,
                source_uri=source_uri,
                source_size=source_size,
                content_type=content_type,
                display_name=display_name,
                added_by=added_by,
                added_by_display=added_by_display,
                registered_at=registered_at,
                queued_at_ms=queued_at_ms,
                indexing=queued,
            )
            if extra_attrs:
                item = {**{k: v for k, v in extra_attrs.items() if v is not None}, **item}
            item.update(lineage)
            if folder:
                item["folder"] = folder
            ops = self._registration_ops(
                pid=pid,
                asset_id=asset_id,
                item=item,
                display_name=display_name,
                source_bucket=source_bucket,
                source_key=source_key,
                queued_at_ms=queued_at_ms,
                kind=kind,
                indexing=queued,
                any_indexing=never_queue,
            )
            if hash_key:
                ops.append({"Put": {"TableName": self.name, "Item": {**hash_key, "assetId": asset_id},
                                    "ConditionExpression": "attribute_not_exists(PK)"}})
            try:
                self.client.transact_write_items(TransactItems=ops)
                return item
            except ClientError as exc:
                last_error = exc
                if not _is_txn_canceled(exc):
                    raise
                # A repeated asset_id (ASSET guard) means someone registered it concurrently: return it.
                existing = self.get_asset(pid, asset_id)
                if existing is not None:
                    return existing
                # The same bytes are already an asset of this Project (R5): return it, write nothing.
                if hash_key:
                    dup = self._hash_holder(pid, hash_key, asset_id)
                    if dup is not None:
                        return {**dup, "deduped": True}
                # Otherwise it was a DNAME race (or a SRC collision); re-read guards and retry.
                if _src_guard_rejected(exc):
                    raise RegistrationConflict(
                        "another asset already owns this source location"
                    ) from exc
                if _RETRY_BASE_SLEEP_S:
                    self._sleep(_RETRY_BASE_SLEEP_S * (2**attempt))
        raise RegistrationConflict("Display_Name allocation did not converge") from last_error

    def _hash_holder(self, pid: str, hash_key: dict, asset_id: str) -> Optional[dict]:
        """The live asset holding a HASH guard, or ``None``. A guard left by a missing or removed asset is
        deleted (conditioned on still naming it), so the caller's retry can claim it."""
        guard = self.table.get_item(Key=hash_key, ConsistentRead=True).get("Item")
        holder = (guard or {}).get("assetId")
        if not isinstance(holder, str) or holder == asset_id:
            return None
        row = self.get_asset(pid, holder)
        if row is not None and row.get("status") != "removed":
            return row
        try:
            self.table.delete_item(Key=hash_key, ConditionExpression="assetId = :aid",
                                   ExpressionAttributeValues={":aid": holder})
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
        return None

    def remove_asset(self, pid: str, asset_id: str, *, removed_by: str) -> tuple[dict, bool]:
        """Move one asset to ``removed`` (design §2 ``remove``). Returns ``(row, changed)``.

        One ``TransactWriteItems``: the ASSET update (conditioned on the status and ``rowVersion`` it was
        read with, so a concurrent transition retries rather than double-counts), the QUEUE item and the
        DNAME guard deleted (the name is free again, Req 10.16), the FAILIDX item deleted for a
        ``failed`` asset, a held processing slot released, and the META counters moved from the old
        status to ``removed``. The ASSET row also loses its ``assetsByRegistered`` GSI attributes, so a
        removed asset drops out of the asset list. Idempotent: an already-removed asset returns
        ``(row, False)`` without a write. Raises :class:`AssetNotFound` when there is no row.

        The SRC guard stays (at most one asset per bucket+key in any status, Req 2.14–2.16). The
        returned row is a consistent re-read AFTER the commit, so its ``chatCopyKeys`` is complete:
        ``add_chat_copy`` is conditioned on ``status <> removed`` and cannot add to it any more.
        """
        if not is_project_id(pid) or not schema.is_asset_id(asset_id):
            raise AssetNotFound(asset_id)
        last_error: Optional[ClientError] = None
        for _attempt in range(_MAX_ALLOC_ATTEMPTS):
            row = self.get_asset(pid, asset_id)
            if row is None:
                raise AssetNotFound(asset_id)
            old = row.get("status")
            if old == "removed":
                return row, False
            ops = self._removal_ops(pid, asset_id, row, removed_by=removed_by)
            try:
                self.client.transact_write_items(TransactItems=ops)
            except ClientError as exc:
                last_error = exc
                if not _is_txn_canceled(exc):
                    raise
                continue  # a concurrent transition: re-read and retry
            self._release_hash_guard(pid, asset_id, row)
            after = self.get_asset(pid, asset_id)
            return (after if after is not None else row), True
        raise RegistrationConflict("removal kept losing a concurrent transition") from last_error

    def _release_hash_guard(self, pid: str, asset_id: str, row: Mapping[str, Any]) -> None:
        """Free a removed asset's HASH guard (R5): identical bytes registered later become a new asset.

        After the removal commit and best-effort, conditioned on the guard still naming this asset (a re-added
        asset may no longer hold it), so it can never block a removal. A guard left behind is cleaned up by
        the next registration that meets it (``_hash_holder``)."""
        hash_key = _hash_key(pid, row.get("checksum"))
        if not hash_key:
            return
        try:
            self.table.delete_item(Key=hash_key, ConditionExpression="assetId = :aid",
                                   ExpressionAttributeValues={":aid": asset_id})
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                log.warning("store: HASH guard of %s not released: %s", asset_id, exc)

    def _removal_ops(self, pid: str, asset_id: str, row: Mapping[str, Any], *, removed_by: str) -> list[dict]:
        name = self.name
        old = row.get("status")
        if not isinstance(old, str) or not old:
            old = "queued"
        now_iso = _iso_now(self._clock)
        values: dict[str, Any] = {":removed": "removed", ":old": old, ":now": now_iso, ":by": removed_by,
                                  ":one": 1, ":zero": 0}
        condition = "#st = :old"
        rv = row.get("rowVersion")
        if rv is None:
            condition += " AND attribute_not_exists(rowVersion)"
        else:
            condition += " AND rowVersion = :rv"
            values[":rv"] = rv
        set_parts = [
            "#st = :removed",
            "statusChangedAt = :now",
            "removedAt = :now",
            "removedBy = :by",
            "previousStatus = :old",
            "rowVersion = if_not_exists(rowVersion, :zero) + :one",
        ]
        remove_parts = ["assetListPk", "assetListSk"]
        slot_held = row.get("slotHeld") is True
        if slot_held:
            remove_parts.append("slotHeld")
        ops: list[dict] = [
            {
                "Update": {
                    "TableName": name,
                    "Key": schema.asset_key(pid, asset_id),
                    "UpdateExpression": "SET " + ", ".join(set_parts) + " REMOVE " + ", ".join(remove_parts),
                    "ConditionExpression": condition,
                    "ExpressionAttributeNames": {"#st": "status"},
                    "ExpressionAttributeValues": values,
                }
            }
        ]
        queued_at = _as_int(row.get("queuedAt"))
        if queued_at is not None:
            ops.append({"Delete": {"TableName": name, "Key": schema.queue_key(pid, queued_at, asset_id)}})
        display_name = row.get("displayName")
        if isinstance(display_name, str) and display_name:
            ops.append({
                "Delete": {
                    "TableName": name,
                    "Key": schema.dname_guard_key(pid, display_name),
                    # Never free a name another asset holds (it cannot, while this one is live; belt and braces).
                    "ConditionExpression": "attribute_not_exists(PK) OR assetId = :aid",
                    "ExpressionAttributeValues": {":aid": asset_id},
                }
            })
        failed_at = _as_int(row.get("failedAt"))
        if old == "failed" and failed_at is not None:
            ops.append({"Delete": {"TableName": name, "Key": schema.failidx_key(pid, failed_at, asset_id)}})
        meta_set = [
            "statusCounts.#old = if_not_exists(statusCounts.#old, :zero) - :one",
            "statusCounts.#removed = if_not_exists(statusCounts.#removed, :zero) + :one",
            "activeAssetCount = if_not_exists(activeAssetCount, :one) - :one",
            "changeSeq = if_not_exists(changeSeq, :zero) + :one",
        ]
        if slot_held:
            meta_set.append("processingCount = if_not_exists(processingCount, :one) - :one")
        ops.append({
            "Update": {
                "TableName": name,
                "Key": schema.meta_key(pid),
                "UpdateExpression": "SET " + ", ".join(meta_set),
                "ConditionExpression": "attribute_exists(SK) AND attribute_exists(statusCounts)",
                "ExpressionAttributeNames": {"#old": old, "#removed": "removed"},
                "ExpressionAttributeValues": {":one": 1, ":zero": 0},
            }
        })
        return ops

    # ---------------------------------------------------------------- retry / re-add
    def _requeue_meta_op(self, pid: str, frm: str, *, active_delta: int = 0) -> dict:
        """``frm`` → ``queued`` on META. Conditioned on indexing being ON, so a re-add or a stored-asset queue
        can never put work on the Dispatcher for a Project that does not ingest."""
        sets = ["statusCounts.#f = statusCounts.#f - :one", "statusCounts.queued = statusCounts.queued + :one",
                "changeSeq = changeSeq + :one", "assetListPk = :sched", "assetListSk = :pid"]
        if active_delta:
            sets.append("activeAssetCount = activeAssetCount + :one")
        return {"Update": {
            "TableName": self.name, "Key": schema.meta_key(pid), "UpdateExpression": "SET " + ", ".join(sets),
            "ConditionExpression": ("attribute_exists(SK) AND #st = :active AND attribute_exists(statusCounts) "
                                    "AND (attribute_not_exists(#ix) OR #ix = :true)"),
            "ExpressionAttributeNames": {"#st": "status", "#f": frm, "#ix": "indexing"},
            "ExpressionAttributeValues": {":one": 1, ":active": "active", ":sched": "SCHED", ":pid": pid,
                                          ":true": True}}}

    def _store_meta_op(self, pid: str, frm: str, *, any_indexing: bool = False) -> dict:
        """``frm`` → ``stored`` on META (a re-add into a Project with indexing OFF): no SCHED pair. A row that is
        never queued (``any_indexing``, an analysis file) is stored under either flag, so no flag is named."""
        cond = "attribute_exists(SK) AND #st = :active AND attribute_exists(statusCounts)"
        names = {"#st": "status", "#f": frm, "#sd": "stored"}
        values: dict = {":one": 1, ":zero": 0, ":active": "active"}
        if not any_indexing:
            cond += " AND #ix = :false"
            names["#ix"] = "indexing"
            values[":false"] = False
        return {"Update": {
            "TableName": self.name, "Key": schema.meta_key(pid),
            "UpdateExpression": ("SET statusCounts.#f = statusCounts.#f - :one, "
                                 "statusCounts.#sd = if_not_exists(statusCounts.#sd, :zero) + :one, "
                                 "changeSeq = changeSeq + :one, activeAssetCount = activeAssetCount + :one"),
            "ConditionExpression": cond, "ExpressionAttributeNames": names, "ExpressionAttributeValues": values}}

    def retry_asset(self, pid: str, asset_id: str) -> dict:
        """``failed`` → ``queued`` (Req 7.10, 7.12, 7.13). The step records stay, so the next run reuses what
        succeeded and reruns what failed. Raises :class:`AssetNotFound`, or :class:`WrongStatus` with the current
        status; of concurrent retries exactly one commits (the condition is ``status = failed``)."""
        row = self.get_asset(pid, asset_id)
        if row is None:
            raise AssetNotFound(asset_id)
        if row.get("status") != "failed":
            raise WrongStatus(str(row.get("status")))
        now_ms = int(self._clock() * 1000)
        ops: list[dict] = [{"Update": {
            "TableName": self.name, "Key": schema.asset_key(pid, asset_id),
            "UpdateExpression": ("SET #st = :q, statusChangedAt = :now, queuedAt = :qa, "
                                 "rowVersion = rowVersion + :one REMOVE failure, failedAt"),
            "ConditionExpression": "#st = :failed AND rowVersion = :rv",
            "ExpressionAttributeNames": {"#st": "status"},
            "ExpressionAttributeValues": {":q": "queued", ":failed": "failed", ":now": _iso_now(self._clock),
                                          ":qa": now_ms, ":one": 1, ":rv": row.get("rowVersion")}}},
            {"Put": {"TableName": self.name, "Item": {**schema.queue_key(pid, now_ms, asset_id), "assetId": asset_id}}},
            self._requeue_meta_op(pid, "failed")]
        failed_at = _as_int(row.get("failedAt"))
        if failed_at is not None:
            ops.append({"Delete": {"TableName": self.name, "Key": schema.failidx_key(pid, failed_at, asset_id)}})
        try:
            self.client.transact_write_items(TransactItems=ops)
        except ClientError as exc:
            if not _is_txn_canceled(exc):
                raise
            now = self.get_asset(pid, asset_id)
            raise WrongStatus(str((now or {}).get("status") or "unknown")) from exc
        return self.get_asset(pid, asset_id) or row

    def readd_asset(self, pid: str, asset_id: str) -> dict:
        """``removed`` → ``queued`` with the same ``asset_id`` and a re-derived Display_Name (Req 10.16, 10.17)."""
        row = self.get_asset(pid, asset_id)
        if row is None:
            raise AssetNotFound(asset_id)
        if row.get("status") != "removed":
            raise WrongStatus(str(row.get("status")))
        kind = schema.asset_kind(asset_id) or "video"
        base = derive_display_name(_SUFFIX_RE.sub("", str(row.get("displayName") or "")), kind)
        last: Optional[ClientError] = None
        for _attempt in range(_MAX_ALLOC_ATTEMPTS):
            # Re-read every attempt: a cancelled transaction may have been the indexing flag turning ON.
            indexing = indexing_on(self.get_meta(pid)) and queueable(row)
            name = self._allocate_display_name(pid, base)
            now_ms = int(self._clock() * 1000)
            if not indexing:
                ops = [{"Update": {
                    "TableName": self.name, "Key": schema.asset_key(pid, asset_id),
                    "UpdateExpression": ("SET #st = :s, statusChangedAt = :now, displayName = :dn, "
                                         "displayFold = :df, assetListPk = :lpk, assetListSk = :lsk, "
                                         "rowVersion = rowVersion + :one "
                                         "REMOVE removedAt, removedBy, previousStatus, cleanupDone, queuedAt"),
                    "ConditionExpression": "#st = :removed AND rowVersion = :rv",
                    "ExpressionAttributeNames": {"#st": "status"},
                    "ExpressionAttributeValues": {
                        ":s": "stored", ":removed": "removed", ":now": _iso_now(self._clock),
                        ":dn": name, ":df": schema.fold_name(name), ":lpk": schema.project_pk(pid),
                        ":lsk": f"{row.get('registeredAt')}#{asset_id}", ":one": 1, ":rv": row.get("rowVersion")}}},
                    {"Put": {"TableName": self.name, "Item": {**schema.dname_guard_key(pid, name), "assetId": asset_id},
                             "ConditionExpression": "attribute_not_exists(PK)"}},
                    self._store_meta_op(pid, "removed", any_indexing=not queueable(row))]
                try:
                    self.client.transact_write_items(TransactItems=ops)
                    return self.get_asset(pid, asset_id) or row
                except ClientError as exc:
                    last = exc
                    if not _is_txn_canceled(exc):
                        raise
                    now = self.get_asset(pid, asset_id)
                    if (now or {}).get("status") != "removed":
                        raise WrongStatus(str((now or {}).get("status") or "unknown")) from exc
                    row = now
                    continue
            ops = [{"Update": {
                "TableName": self.name, "Key": schema.asset_key(pid, asset_id),
                "UpdateExpression": ("SET #st = :q, statusChangedAt = :now, queuedAt = :qa, displayName = :dn, "
                                     "displayFold = :df, assetListPk = :lpk, assetListSk = :lsk, "
                                     "rowVersion = rowVersion + :one "
                                     "REMOVE removedAt, removedBy, previousStatus, cleanupDone"),
                "ConditionExpression": "#st = :removed AND rowVersion = :rv",
                "ExpressionAttributeNames": {"#st": "status"},
                "ExpressionAttributeValues": {
                    ":q": "queued", ":removed": "removed", ":now": _iso_now(self._clock), ":qa": now_ms,
                    ":dn": name, ":df": schema.fold_name(name), ":lpk": schema.project_pk(pid),
                    ":lsk": f"{row.get('registeredAt')}#{asset_id}", ":one": 1, ":rv": row.get("rowVersion")}}},
                {"Put": {"TableName": self.name, "Item": {**schema.dname_guard_key(pid, name), "assetId": asset_id},
                         "ConditionExpression": "attribute_not_exists(PK)"}},
                {"Put": {"TableName": self.name,
                         "Item": {**schema.queue_key(pid, now_ms, asset_id), "assetId": asset_id}}},
                self._requeue_meta_op(pid, "removed", active_delta=1)]
            try:
                self.client.transact_write_items(TransactItems=ops)
                return self.get_asset(pid, asset_id) or row
            except ClientError as exc:
                last = exc
                if not _is_txn_canceled(exc):
                    raise
                now = self.get_asset(pid, asset_id)
                if (now or {}).get("status") != "removed":
                    raise WrongStatus(str((now or {}).get("status") or "unknown")) from exc
                row = now  # a Display_Name race: re-read the guards and try the next free name
        raise RegistrationConflict("Display_Name allocation did not converge") from last

    def enable_indexing(self, pid: str, *, by: str) -> bool:
        """Turn the Project's indexing ON. One way: there is no OFF. ``True`` when this call flipped it.

        Conditioned on ``indexing = false``, so a repeat (or a legacy row with no flag, already ON) is a no-op
        that returns ``False``; a deleted Project raises :class:`ProjectNotActive`. The caller then runs
        :meth:`queue_stored`, which is idempotent, so a crash between the two is repaired by calling again.
        """
        try:
            self.table.update_item(
                Key=schema.meta_key(pid),
                UpdateExpression="SET #ix = :true, indexingEnabledAt = :now, indexingEnabledBy = :by",
                ConditionExpression="attribute_exists(SK) AND #st = :active AND #ix = :false",
                ExpressionAttributeNames={"#st": "status", "#ix": "indexing"},
                ExpressionAttributeValues={":true": True, ":false": False, ":active": "active",
                                           ":now": _iso_now(self._clock), ":by": by},
            )
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
        meta = self.get_meta(pid)
        if not meta or meta.get("status") != "active":
            raise ProjectNotActive("the Project is not active")
        return False

    def stored_assets(self, pid: str) -> list[dict]:
        """Every ``stored`` ASSET row of the Project (consistent; pages the base-table partition)."""
        from boto3.dynamodb.conditions import Attr, Key

        out: list[dict] = []
        kwargs: dict = {"KeyConditionExpression": Key("PK").eq(schema.project_pk(pid))
                        & Key("SK").begins_with(schema.ASSET_SK_PREFIX),
                        "FilterExpression": Attr("status").eq("stored"), "ConsistentRead": True}
        while True:
            resp = self.table.query(**kwargs)
            out += resp.get("Items", [])
            if not resp.get("LastEvaluatedKey"):
                return out
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    def queue_stored(self, pid: str, *, kind: Optional[str] = None) -> int:
        """``stored`` → ``queued`` for every stored asset, once indexing is ON. Returns how many moved.

        ``kind`` limits it to one kind (``scripts/requeue_stored_documents.py`` re-queues only the PDFs that were
        registered ``stored`` before session documents were indexed).

        One transaction per asset (ASSET update + QUEUE put + META counters), each conditioned on the row still
        being ``stored`` at the version read, so a repeat call or a concurrent one moves nothing twice. The META
        op requires indexing ON, so this can never queue work for a Project that does not ingest.
        """
        if not indexing_on(self.get_meta(pid)):
            return 0
        moved = 0
        for row in self.stored_assets(pid):
            if not queueable(row):
                continue  # an analysis/intermediate file stays stored (asset-lineage R3.5)
            if kind is not None and not str(row.get("assetId", "")).startswith(f"{kind}:"):
                continue
            asset_id = str(row.get("assetId"))
            now_ms = int(self._clock() * 1000)
            ops = [{"Update": {
                "TableName": self.name, "Key": schema.asset_key(pid, asset_id),
                "UpdateExpression": ("SET #st = :q, statusChangedAt = :now, queuedAt = :qa, "
                                     "rowVersion = rowVersion + :one"),
                "ConditionExpression": "#st = :stored AND rowVersion = :rv",
                "ExpressionAttributeNames": {"#st": "status"},
                "ExpressionAttributeValues": {":q": "queued", ":stored": "stored", ":now": _iso_now(self._clock),
                                              ":qa": now_ms, ":one": 1, ":rv": row.get("rowVersion")}}},
                {"Put": {"TableName": self.name,
                         "Item": {**schema.queue_key(pid, now_ms, asset_id), "assetId": asset_id}}},
                self._requeue_meta_op(pid, "stored")]
            try:
                self.client.transact_write_items(TransactItems=ops)
                moved += 1
            except ClientError as exc:
                if not _is_txn_canceled(exc):
                    raise  # a real error: the caller retries the whole (idempotent) call
        return moved

    def failed_assets(self, pid: str, limit: int = 20) -> list[dict]:
        """Newest failure first, from the FAILIDX items (design §2; Req 3.7, 7.14)."""
        from boto3.dynamodb.conditions import Key

        resp = self.table.query(KeyConditionExpression=Key("PK").eq(schema.project_pk(pid))
                                & Key("SK").begins_with(schema.FAILIDX_SK_PREFIX),
                                ScanIndexForward=False, Limit=limit, ConsistentRead=True)
        ids = [str(it.get("assetId")) for it in resp.get("Items", []) if it.get("assetId")]
        rows = self.batch_get_assets(pid, ids)
        return [rows[i] for i in ids if i in rows and rows[i].get("status") == "failed"]

    def find_by_name(self, pid: str, name: str, limit: int = 20) -> list[dict]:
        """Live assets whose folded ``folder/Display_Name`` contains the folded ``name`` (Req 3.8, 10.18, 14).

        A root asset matches on its Display_Name alone; an asset in a folder also matches on its folder path
        (``Day 1`` finds every file in ``Footage/Day 1``) and across the boundary (``Day 1/clip``). DynamoDB
        cannot filter on a concatenation, so the query keeps rows whose name matches OR that have a folder,
        and the folder rows are matched here.

        RANKED by relevance, newest-first within a tier: exact Display_Name > prefix > substring > folder-only.
        Ranking needs the whole match set, so this collects EVERY match up to a scan bound and then returns the
        top ``limit`` — otherwise an older exact-named upload (e.g. ``Netflix_Open_Content_Meridian.mp4``) was
        buried under newer substring matches (``crop_Netflix_Open_*.mp4``) and the agent reported the file was
        not in the project even when given its exact name (staging session 6eb1761a).
        """
        from boto3.dynamodb.conditions import Attr, Key

        needle = schema.fold_name(name)
        matches: list[dict] = []
        kwargs: dict = {"KeyConditionExpression": Key("PK").eq(schema.project_pk(pid))
                        & Key("SK").begins_with(schema.ASSET_SK_PREFIX),
                        "FilterExpression": Attr("displayFold").contains(needle) | Attr("folder").exists(),
                        "ConsistentRead": True}
        for _ in range(_FIND_SCAN_MAX_PAGES):
            resp = self.table.query(**kwargs)
            matches += [r for r in resp.get("Items", [])
                        if r.get("status") != "removed" and needle in schema.fold_name(folder_path_text(r))]
            if not resp.get("LastEvaluatedKey"):
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        # Newest-first first (stable), then by relevance tier — Python's stable sort keeps the recency order
        # within each tier, so the exact name wins and ties break on recency.
        matches.sort(key=lambda r: str(r.get("registeredAt") or ""), reverse=True)
        matches.sort(key=lambda r: _name_rank(r, needle))
        return matches[:limit]

    def mark_cleanup_done(self, pid: str, asset_id: str) -> None:
        """Note on a removed ASSET row that every owned object was deleted (``cleanupDone``).

        A repeat ``remove`` then skips the deletes: in a versioned bucket each repeat delete would add
        another delete marker. Conditioned on ``status = removed``; a lost condition is ignored.
        """
        try:
            self.table.update_item(
                Key=schema.asset_key(pid, asset_id),
                UpdateExpression="SET cleanupDone = :t",
                ConditionExpression="#st = :removed",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={":t": True, ":removed": "removed"},
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise

    def add_chat_copy(self, pid: str, asset_id: str, key: str, *, bucket: Optional[str] = None) -> dict:
        """Record a chat copy's key on the ASSET row (string set ``chatCopyKeys``) so ``remove`` deletes it.

        LEGACY (asset-lineage Phase 2 retired the chat-copy byte copy): no new copy is made, so nothing calls
        this on the current path. It stays for a deploy where an older proxy image still copies, and because
        ``remove`` must keep deleting the copies rows already carry.

        Conditioned on the row existing and not being ``removed``: a removed asset accepts no new copy
        (the caller must then delete any copy it made). Raises :class:`AssetNotFound` otherwise.

        With the copy's ``bucket``, the copy's location also gets a SRC guard naming this asset, so
        :meth:`asset_id_for_location` maps the chat copy a tool read back to its Project_Asset (lineage, R1.2).
        The guard is best-effort: one another asset holds is left alone.
        """
        if not is_project_id(pid) or not schema.is_asset_id(asset_id):
            raise AssetNotFound(asset_id)
        if not isinstance(key, str) or not CHAT_COPY_KEY_RE.fullmatch(key):
            raise ValueError("not a chat-copy key")
        try:
            resp = self.table.update_item(
                Key=schema.asset_key(pid, asset_id),
                UpdateExpression="ADD chatCopyKeys :k",
                ConditionExpression="attribute_exists(PK) AND #st <> :removed",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={":k": {key}, ":removed": "removed"},
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise AssetNotFound(asset_id) from exc
            raise
        if isinstance(bucket, str) and bucket:
            try:
                self.table.put_item(
                    Item={**schema.src_guard_key(pid, bucket, key), "assetId": asset_id},
                    ConditionExpression="attribute_not_exists(PK) OR assetId = :aid",
                    ExpressionAttributeValues={":aid": asset_id},
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                    raise
        return resp.get("Attributes") or {}

    def set_rendition_if_absent(self, pid: str, asset_id: str, rendition: Mapping[str, Any]) -> bool:
        """Record an on-demand Search_Rendition on an image row that has none (Asset_Service ``search_images``).

        Like ``add_chat_session`` an annotation, not a status change: no ``rowVersion`` bump, so a pipeline write
        conditioned on the version is unaffected. Conditioned on the row being present, not ``removed``, and
        still without a rendition; ``False`` when that no longer holds (the bytes written are identical either way).
        """
        try:
            self.table.update_item(
                Key=schema.asset_key(pid, asset_id),
                UpdateExpression="SET rendition = :r",
                ConditionExpression="attribute_exists(PK) AND attribute_not_exists(rendition) AND #st <> :removed",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={":r": dict(rendition), ":removed": "removed"},
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def add_chat_session(self, pid: str, asset_id: str, session_id: str) -> dict:
        """Record that a chat session references this asset (string set ``chatSessions``, asset-lineage Phase 2).

        Phase 2 attaches a Project_Asset to a chat BY REFERENCE: no bytes are copied, so there is nothing for
        ``remove`` to chase afterwards. The set is the association itself — which chats hold a reference —
        and is what a session's cleanup and an audit read.

        Conditioned on the row existing and not being ``removed`` (a removed asset accepts no new reference), so
        the caller learns the asset is gone instead of writing onto a tombstone. Raises
        :class:`AssetNotFound` otherwise.
        """
        if not is_project_id(pid) or not schema.is_asset_id(asset_id):
            raise AssetNotFound(asset_id)
        if not isinstance(session_id, str) or not CHAT_SESSION_ID_RE.fullmatch(session_id):
            raise ValueError("not a chat session id")
        try:
            resp = self.table.update_item(
                Key=schema.asset_key(pid, asset_id),
                UpdateExpression="ADD chatSessions :s",
                ConditionExpression="attribute_exists(PK) AND #st <> :removed",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={":s": {session_id}, ":removed": "removed"},
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise AssetNotFound(asset_id) from exc
            raise
        return resp.get("Attributes") or {}

    def set_folder(self, pid: str, asset_id: str, folder: Optional[str]) -> dict:
        """File the asset in ``folder`` (an already-normalized path; ``None`` or ``""`` is the project root).

        A folder is metadata only (:mod:`ingestion.folders`): no bytes move, nothing is re-indexed, and no
        registration path ever rewrites ``folder`` afterwards (each returns early for an id it already holds), so a
        user's move sticks. Deliberately does NOT bump ``rowVersion`` — like :meth:`set_rendition_if_absent`, an
        annotation must not fail the pipeline's version-conditioned writes. Conditioned on the row existing and not
        being ``removed``; raises :class:`AssetNotFound` otherwise.
        """
        if not is_project_id(pid) or not schema.is_asset_id(asset_id):
            raise AssetNotFound(asset_id)
        kw: dict = {
            "Key": schema.asset_key(pid, asset_id),
            "ConditionExpression": "attribute_exists(PK) AND #st <> :removed",
            "ExpressionAttributeNames": {"#st": "status"},
            "ExpressionAttributeValues": {":removed": "removed"},
            "ReturnValues": "ALL_NEW",
        }
        if folder:
            kw["UpdateExpression"] = "SET folder = :f"
            kw["ExpressionAttributeValues"][":f"] = folder
        else:
            kw["UpdateExpression"] = "REMOVE folder"
        try:
            resp = self.table.update_item(**kw)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise AssetNotFound(asset_id) from exc
            raise
        return resp.get("Attributes") or {}

    def assets_under_folder(self, pid: str, folder: str) -> list[dict]:
        """Every live ASSET row in ``folder`` or below it (consistent; pages the base-table partition)."""
        from boto3.dynamodb.conditions import Attr, Key

        out: list[dict] = []
        kwargs: dict = {"KeyConditionExpression": Key("PK").eq(schema.project_pk(pid))
                        & Key("SK").begins_with(schema.ASSET_SK_PREFIX),
                        "FilterExpression": (Attr("folder").eq(folder) | Attr("folder").begins_with(f"{folder}/"))
                        & Attr("status").ne("removed"),
                        "ConsistentRead": True}
        while True:
            resp = self.table.query(**kwargs)
            out += resp.get("Items", [])
            if not resp.get("LastEvaluatedKey"):
                return out
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    # ------------------------------------------------------------------ bookmarks
    def create_bookmark(self, pid: str, *, asset_id: str, start_ms: int, end_ms: Optional[int], label: str,
                        folder: Optional[str], created_by: str, session_id: Optional[str] = None) -> dict:
        """Save a moment of ``asset_id`` (``start_ms``–``end_ms``) as a bookmark filed in ``folder``.

        The asset must exist and not be removed. A bookmark holds no bytes and is never indexed: it is a reference
        with a range, so saving one is instant and one take can be filed in several folders. Returns the row.
        """
        import uuid as _uuid
        from datetime import datetime, timezone

        if not is_project_id(pid) or not schema.is_asset_id(asset_id):
            raise AssetNotFound(asset_id)
        parent = self.get_asset(pid, asset_id)
        if parent is None or parent.get("status") == "removed":
            raise AssetNotFound(asset_id)
        bid = f"bookmark:{_uuid.uuid4()}"
        item: dict = {
            **schema.bookmark_key(pid, bid), "bookmarkId": bid, "assetId": asset_id, "startMs": int(start_ms),
            "label": label, "createdBy": created_by,
            "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "assetKind": parent.get("kind"), "assetDisplayName": parent.get("displayName"),
        }
        if end_ms is not None:
            item["endMs"] = int(end_ms)
        if folder:
            item["folder"] = folder
        if session_id:
            item["sessionId"] = session_id
        self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
        return item

    def list_bookmarks(self, pid: str) -> list[dict]:
        """Every bookmark row of the Project (consistent; pages the partition's ``BOOKMARK#`` range)."""
        from boto3.dynamodb.conditions import Key

        out: list[dict] = []
        kwargs: dict = {"KeyConditionExpression": Key("PK").eq(schema.project_pk(pid))
                        & Key("SK").begins_with(schema.BOOKMARK_SK_PREFIX), "ConsistentRead": True}
        while True:
            resp = self.table.query(**kwargs)
            out += resp.get("Items", [])
            if not resp.get("LastEvaluatedKey"):
                return out
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    def set_bookmark_folder(self, pid: str, bookmark_id: str, folder: Optional[str]) -> dict:
        """File a bookmark in ``folder`` (``None``/``""`` = the root). Raises :class:`AssetNotFound` if absent."""
        if not is_project_id(pid) or not schema.is_bookmark_id(bookmark_id):
            raise AssetNotFound(bookmark_id)
        kw: dict = {"Key": schema.bookmark_key(pid, bookmark_id), "ConditionExpression": "attribute_exists(PK)",
                    "ReturnValues": "ALL_NEW"}
        if folder:
            kw["UpdateExpression"] = "SET folder = :f"
            kw["ExpressionAttributeValues"] = {":f": folder}
        else:
            kw["UpdateExpression"] = "REMOVE folder"
        try:
            resp = self.table.update_item(**kw)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise AssetNotFound(bookmark_id) from exc
            raise
        return resp.get("Attributes") or {}

    def create_folder(self, pid: str, folder: str, *, created_by: str) -> None:
        """Record a folder (normalized path) so it exists while empty. Idempotent: an existing row is kept."""
        from datetime import datetime, timezone

        if not is_project_id(pid) or not folder:
            return
        try:
            self.table.put_item(Item={**schema.folder_key(pid, folder), "folder": folder, "createdBy": created_by,
                                      "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
                                ConditionExpression="attribute_not_exists(PK)")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise

    def job_for_execution(self, pid: str, execution_id: str) -> Optional[dict]:
        """The ``JOB#`` record of this Project whose Step Functions execution id (the ARN's last segment) is
        ``execution_id``, or ``None``. One filtered Query over the Project's job records."""
        from boto3.dynamodb.conditions import Attr, Key

        if not is_project_id(pid) or not isinstance(execution_id, str) or not execution_id:
            return None
        kwargs: dict = {"KeyConditionExpression": Key("PK").eq(schema.project_pk(pid))
                        & Key("SK").begins_with(schema.JOB_SK_PREFIX),
                        "FilterExpression": Attr("jobId").contains(":" + execution_id)}
        while True:
            resp = self.table.query(**kwargs)
            for item in resp.get("Items", []):
                if str(item.get("jobId") or "").rsplit(":", 1)[-1] == execution_id:
                    return item
            if not resp.get("LastEvaluatedKey"):
                return None
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    def list_folder_rows(self, pid: str) -> list[str]:
        """Every recorded folder path of the Project (consistent)."""
        from boto3.dynamodb.conditions import Key

        out: list[str] = []
        kwargs: dict = {"KeyConditionExpression": Key("PK").eq(schema.project_pk(pid))
                        & Key("SK").begins_with(schema.FOLDER_SK_PREFIX), "ConsistentRead": True}
        while True:
            resp = self.table.query(**kwargs)
            out += [str(i["SK"])[len(schema.FOLDER_SK_PREFIX):] for i in resp.get("Items", [])]
            if not resp.get("LastEvaluatedKey"):
                return out
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    def remove_folder_row(self, pid: str, folder: str) -> None:
        """Forget a recorded folder (its files, if any, are untouched)."""
        if is_project_id(pid) and folder:
            self.table.delete_item(Key=schema.folder_key(pid, folder))

    def remove_bookmark(self, pid: str, bookmark_id: str) -> bool:
        """Delete a bookmark (its asset is untouched). ``False`` when there was none."""
        if not is_project_id(pid) or not schema.is_bookmark_id(bookmark_id):
            return False
        resp = self.table.delete_item(Key=schema.bookmark_key(pid, bookmark_id), ReturnValues="ALL_OLD")
        return bool(resp.get("Attributes"))

    def session_assets(self, pid: str, session_id: str) -> list[dict]:
        """Every live ``visibility=session`` ASSET row of one chat session (asset-lineage D2).

        These are the "this chat only" files: project rows hidden from the Assets grid, owned by the session and
        deleted with it. An already-``removed`` row is left out (its removal is idempotent anyway). Consistent,
        and pages the base-table partition like :meth:`stored_assets`.
        """
        from boto3.dynamodb.conditions import Attr, Key

        if not is_project_id(pid) or not isinstance(session_id, str) or not session_id:
            return []
        out: list[dict] = []
        kwargs: dict = {"KeyConditionExpression": Key("PK").eq(schema.project_pk(pid))
                        & Key("SK").begins_with(schema.ASSET_SK_PREFIX),
                        "FilterExpression": Attr("visibility").eq("session") & Attr("sessionId").eq(session_id)
                        & Attr("status").ne("removed"),
                        "ConsistentRead": True}
        while True:
            resp = self.table.query(**kwargs)
            out += resp.get("Items", [])
            if not resp.get("LastEvaluatedKey"):
                return out
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    def _ensure_meta_counts(self, pid: str) -> dict:
        """Idempotently initialize META's ingestion counters so the transaction can increment leaves.

        This spec is the first writer of ``statusCounts``; a nested arithmetic update needs the parent
        map and the leaves to exist. The update only fills what is absent, so it never changes a count
        and is safe to run before every registration.
        """
        empty_counts = {s: 0 for s in STATUS_COUNT_KEYS}
        try:
            return self._update_meta_defaults(pid, empty_counts)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise ProjectNotActive("the Project is not active") from exc
            raise

    def _update_meta_defaults(self, pid: str, empty_counts: dict) -> dict:
        """The META row after the defaults are filled (``ReturnValues=ALL_NEW``), for the indexing flag."""
        resp = self.table.update_item(
            Key=schema.meta_key(pid),
            UpdateExpression=(
                "SET statusCounts = if_not_exists(statusCounts, :counts), "
                "activeAssetCount = if_not_exists(activeAssetCount, :zero), "
                "processingCount = if_not_exists(processingCount, :zero), "
                "changeSeq = if_not_exists(changeSeq, :zero), "
                "collectionState = if_not_exists(collectionState, :absent), "
                "incarnation = if_not_exists(incarnation, :zero)"
            ),
            ConditionExpression="attribute_exists(SK) AND #st = :active",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":counts": empty_counts,
                ":zero": 0,
                ":absent": "absent",
                ":active": "active",
            },
            ReturnValues="ALL_NEW",
        )
        return resp.get("Attributes") or {}

    def _registration_ops(
        self,
        *,
        pid: str,
        asset_id: str,
        item: dict,
        display_name: str,
        source_bucket: str,
        source_key: str,
        queued_at_ms: int,
        kind: str,
        indexing: bool = True,
        any_indexing: bool = False,
    ) -> list[dict]:
        """The registration transaction. With indexing OFF the asset is ``stored``: no QUEUE item (its INSERT is
        what wakes the Dispatcher) and no SCHED pair (the sweep), so nothing ever ingests it. The META update is
        conditioned on the flag it was built for, so a registration racing an OFF→ON flip cancels and retries
        with the new value instead of miscounting. ``any_indexing`` is a row that is never queued (an analysis
        file, asset-lineage R3.5): it is ``stored`` under either flag, so its META update names no flag. The
        ASSET, SRC, and DNAME puts keep their positions (the SRC guard must stay at index 1 for
        :func:`_src_guard_rejected`)."""
        name = self.name
        pk = schema.project_pk(pid)
        if not indexing:
            flag_cond = "" if any_indexing else " AND #ix = :false"
            names = {"#st": "status", "#sd": "stored", **({} if any_indexing else {"#ix": "indexing"})}
            values = {":one": 1, ":zero": 0, ":active": "active", **({} if any_indexing else {":false": False})}
            return self._registration_ops(
                pid=pid, asset_id=asset_id, item=item, display_name=display_name, source_bucket=source_bucket,
                source_key=source_key, queued_at_ms=queued_at_ms, kind=kind, indexing=True,
            )[:3] + [{
                "Update": {
                    "TableName": name,
                    "Key": {"PK": pk, "SK": schema.META_SK},
                    "UpdateExpression": (
                        # The leaf may be missing on a META row older than `stored`.
                        "SET statusCounts.#sd = if_not_exists(statusCounts.#sd, :zero) + :one, "
                        "activeAssetCount = activeAssetCount + :one, changeSeq = changeSeq + :one"
                    ),
                    "ConditionExpression": "attribute_exists(SK) AND #st = :active" + flag_cond,
                    # `stored` is a DynamoDB reserved word.
                    "ExpressionAttributeNames": names,
                    "ExpressionAttributeValues": values,
                }
            }]
        return [
            {
                "Put": {
                    "TableName": name,
                    "Item": item,
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            },
            {
                "Put": {
                    "TableName": name,
                    "Item": {**schema.src_guard_key(pid, source_bucket, source_key), "assetId": asset_id},
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            },
            {
                "Put": {
                    "TableName": name,
                    "Item": {**schema.dname_guard_key(pid, display_name), "assetId": asset_id},
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            },
            {
                "Put": {
                    "TableName": name,
                    "Item": {**schema.queue_key(pid, queued_at_ms, asset_id), "assetId": asset_id},
                }
            },
            {
                "Update": {
                    "TableName": name,
                    "Key": {"PK": pk, "SK": schema.META_SK},
                    "UpdateExpression": (
                        "SET statusCounts.queued = statusCounts.queued + :one, "
                        "activeAssetCount = activeAssetCount + :one, "
                        "changeSeq = changeSeq + :one, "
                        # The sparse SCHED pair puts the Project on the Dispatcher's 1-minute sweep (design §7);
                        # the Dispatcher clears it once nothing is queued or processing.
                        "assetListPk = :sched, assetListSk = :pid"
                    ),
                    "ConditionExpression": ("attribute_exists(SK) AND #st = :active "
                                            "AND (attribute_not_exists(#ix) OR #ix = :true)"),
                    "ExpressionAttributeNames": {"#st": "status", "#ix": "indexing"},
                    "ExpressionAttributeValues": {":one": 1, ":active": "active", ":sched": "SCHED",
                                                  ":pid": pid, ":true": True},
                }
            },
        ]


def _asset_item(
    *,
    pid: str,
    asset_id: str,
    kind: str,
    source: str,
    source_uri: str,
    source_size: int,
    content_type: str,
    display_name: str,
    added_by: str,
    added_by_display: str,
    registered_at: str,
    queued_at_ms: int,
    indexing: bool = True,
) -> dict:
    if not indexing:
        item = _asset_item(pid=pid, asset_id=asset_id, kind=kind, source=source, source_uri=source_uri,
                           source_size=source_size, content_type=content_type, display_name=display_name,
                           added_by=added_by, added_by_display=added_by_display, registered_at=registered_at,
                           queued_at_ms=queued_at_ms)
        item.pop("queuedAt")
        item["status"] = "stored"
        return item
    return {
        **schema.asset_key(pid, asset_id),
        "schemaVersion": 1,
        "rowVersion": 1,
        "assetId": asset_id,
        "kind": kind,
        "source": source,
        "sourceUri": source_uri,
        "sourceSize": source_size,
        "contentType": content_type,
        "addedBy": added_by,
        "addedByDisplay": added_by_display,
        "displayName": display_name,
        "displayFold": schema.fold_name(display_name),
        "registeredAt": registered_at,
        "queuedAt": queued_at_ms,
        "status": "queued",
        "statusChangedAt": registered_at,
        "assetListPk": schema.project_pk(pid),
        "assetListSk": f"{registered_at}#{asset_id}",
    }


def _hash_key(pid: str, checksum: Optional[Mapping[str, Any]]) -> Optional[dict]:
    """The HASH guard key for a ``{algo, value, size}`` checksum, or ``None`` (no/malformed checksum: no guard)."""
    if not isinstance(checksum, Mapping):
        return None
    try:
        return schema.hash_guard_key(pid, checksum.get("algo"), checksum.get("value"), _as_int(checksum.get("size")))
    except ValueError:
        return None


def _lineage_attrs(
    *,
    asset_id: str,
    role: Optional[str],
    visibility: Optional[str],
    session_id: Optional[str],
    producer: Optional[Mapping[str, Any]],
    derived_from: Optional[Sequence[str]],
    job_id: Optional[str],
    checksum: Optional[Mapping[str, Any]],
) -> dict:
    """The asset-lineage ASSET attributes that were given (design §2); an absent one is simply not written."""
    out: dict[str, Any] = {}
    if role:
        out["role"] = role
    if visibility:
        out["visibility"] = visibility
    if isinstance(session_id, str) and session_id:
        out["sessionId"] = session_id
    if isinstance(producer, Mapping):
        p = {k: v for k, v in producer.items() if isinstance(v, str) and v}
        if p:
            out["producer"] = p
    if derived_from:
        ids = [a for a in dict.fromkeys(derived_from) if schema.is_asset_id(a) and a != asset_id]
        if ids:
            out["derivedFrom"] = ids[:MAX_DERIVED_FROM]
    if isinstance(job_id, str) and job_id:
        out["jobId"] = job_id
    if isinstance(checksum, Mapping):
        out["checksum"] = {"algo": checksum["algo"], "value": checksum["value"], "size": _as_int(checksum["size"])}
    return out


def _as_int(v: Any) -> Optional[int]:
    """An int from a DynamoDB number (``Decimal``) or int; ``None`` for anything else (incl. bool)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, Decimal) and v == v.to_integral_value():
        return int(v)
    return None


def _iso_now(clock) -> str:
    from datetime import datetime, timezone

    return (
        datetime.fromtimestamp(clock(), tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _is_txn_canceled(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") in {
        "TransactionCanceledException",
        "ConditionalCheckFailedException",
    }


def _src_guard_rejected(exc: ClientError) -> bool:
    """True when a TransactionCanceled names the SRC guard (item index 1) as the ConditionalCheckFailed.

    DynamoDB returns per-item ``CancellationReasons`` in request order; the SRC guard is the second
    operation (index 1). Absent reasons (older botocore / moto), fall back to treating it as a DNAME
    race, which is retried rather than reported — the conservative choice.
    """
    reasons = exc.response.get("CancellationReasons")
    if not isinstance(reasons, list) or len(reasons) < 2:
        return False
    return reasons[1].get("Code") == "ConditionalCheckFailed"


# The asset-lineage design names the store ``ProjectStore`` (``ProjectStore.asset_id_for_location``).
ProjectStore = AssetStore
