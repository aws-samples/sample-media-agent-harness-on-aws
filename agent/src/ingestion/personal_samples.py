"""Personal samples: files every user finds in their Personal project (the demo tour's ``0-demo.mp4``).

The deploy uploads each sample ONCE to the Ingest bucket under ``samples/`` and names them in the Asset_Service
env ``PERSONAL_SAMPLES`` (a JSON list of ``{key, fileName, contentType, version}``). When a user's project list
loads, the proxy asks the Asset_Service to seed that user's Personal project; for each sample not yet seeded
there it:

1. copies the shared object to ``projects/<pid>/<assetId>/<fileName>`` (the upload layout, so every existing
   read, serve, purge and remove path treats it as an upload — the user owns their copy and may delete it);
2. registers it as an ``upload`` asset. A Personal project does not index, so nothing is analyzed and there is no
   per-user toolkit cost;
3. PINS it: ``pinned`` on the row, and its ``assetListSk`` (the newest-first ``assetsByRegistered`` order the grid
   and the + menu list) prefixed with ``~`` so it sorts above every real timestamp, and the sample stays first;
4. writes a ``SAMPLE#<fileName>`` marker, so a user who deletes the sample never gets it back.

Idempotent and race-safe: the asset id is derived from (project, sample key, version), so two concurrent seeds
write the same object and the second registration is a no-op conflict. A sample missing from the bucket is
skipped WITHOUT a marker, so it is seeded once a later deploy uploads it.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from projects import schema

from .limits import kind_for
from .names import sanitize

#: Namespace for the derived sample asset ids (stable across deploys; never change it).
SAMPLE_NAMESPACE = uuid.UUID("2b6f1c0e-4d8a-5f3b-9c71-0a5e8d4b6c12")
SAMPLE_SK_PREFIX = "SAMPLE#"
#: ``assetListSk`` prefix of a pinned asset. ``~`` (0x7E) sorts above every ISO timestamp, and the list is read
#: newest-first (descending), so a pinned asset is the first row of the first page.
PINNED_LIST_PREFIX = "~pinned#"


@dataclass(frozen=True)
class Sample:
    key: str            # the shared object in the Ingest bucket, e.g. samples/0-demo.mp4
    file_name: str      # the name the user sees, e.g. 0-demo.mp4
    content_type: str
    version: str


def samples_from_env(raw: Optional[str] = None) -> list[Sample]:
    """The configured samples (``PERSONAL_SAMPLES``); malformed entries are dropped, absent config is ``[]``."""
    raw = os.environ.get("PERSONAL_SAMPLES", "") if raw is None else raw
    if not raw.strip():
        return []
    try:
        items = json.loads(raw)
    except ValueError:
        return []
    out: list[Sample] = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        key, name = it.get("key"), it.get("fileName")
        if not (isinstance(key, str) and key.startswith("samples/") and isinstance(name, str) and name):
            continue
        out.append(Sample(key=key, file_name=name, content_type=str(it.get("contentType") or "video/mp4"),
                          version=str(it.get("version") or "1")))
    return out


def sample_asset_id(pid: str, sample: Sample) -> Optional[str]:
    """``<kind>:<uuid5>`` for this project's copy of ``sample`` (``None`` when the kind is not a media kind)."""
    kind = kind_for(sample.content_type, sample.file_name)
    if kind not in ("video", "audio", "image", "document"):
        return None
    # Asset ids must be UUID v4 in form (``schema.asset_kind``). Stamp the v4 version/variant bits onto the
    # deterministic uuid5 digest: still stable per (project, sample, version), and a valid v4 id.
    digest = uuid.uuid5(SAMPLE_NAMESPACE, f"{pid}|{sample.key}|{sample.version}").bytes
    return f"{kind}:{uuid.UUID(bytes=digest, version=4)}"


def seed_personal_samples(*, store: Any, s3: Any, bucket: str, pid: str, actor: str,
                          samples: list[Sample], register: Callable[..., dict]) -> dict:
    """Seed ``samples`` into the Personal project ``pid`` of ``actor``. Returns ``{seeded, skipped}``."""
    meta = store.get_meta(pid) or {}
    if meta.get("personal") is not True or meta.get("ownerSub") != actor or meta.get("status") != "active":
        return {"seeded": [], "skipped": [{"reason": "not_personal"}]}
    seeded: list[str] = []
    skipped: list[dict] = []
    for sample in samples:
        marker_key = {"PK": schema.project_pk(pid), "SK": f"{SAMPLE_SK_PREFIX}{sample.file_name}"}
        if store.table.get_item(Key=marker_key, ConsistentRead=True).get("Item"):
            skipped.append({"fileName": sample.file_name, "reason": "already_seeded"})
            continue
        asset_id = sample_asset_id(pid, sample)
        if asset_id is None:
            skipped.append({"fileName": sample.file_name, "reason": "unsupported_kind"})
            continue
        try:
            head = s3.head_object(Bucket=bucket, Key=sample.key)
        except Exception:  # noqa: BLE001 — not deployed yet: no marker, so a later call seeds it
            skipped.append({"fileName": sample.file_name, "reason": "sample_missing"})
            continue
        dst = f"{schema.project_prefix(pid)}{asset_id}/{sanitize(sample.file_name)}"
        s3.copy_object(Bucket=bucket, Key=dst, CopySource={"Bucket": bucket, "Key": sample.key},
                       ContentType=sample.content_type, MetadataDirective="REPLACE",
                       # No tags carried over, so the copy needs no s3:GetObjectTagging / s3:PutObjectTagging.
                       TaggingDirective="REPLACE")
        try:
            row = register(pid=pid, asset_id=asset_id, source="upload", source_uri=f"s3://{bucket}/{dst}",
                           source_bucket=bucket, source_key=dst, source_size=int(head.get("ContentLength") or 0),
                           content_type=sample.content_type, file_name=sample.file_name, actor=actor,
                           extra_attrs={"pinned": True, "sample": sample.file_name})
        except Exception as exc:  # noqa: BLE001 — a concurrent seed registered it first
            if "already_registered" not in str(getattr(exc, "code", "")) + str(exc):
                raise
            row = {}
        registered_at = row.get("registeredAt")
        pinned_id = row.get("assetId") if isinstance(row.get("assetId"), str) else asset_id
        if isinstance(registered_at, str) and row.get("status") != "removed":
            try:
                store.table.update_item(
                    Key={"PK": schema.project_pk(pid), "SK": f"{schema.ASSET_SK_PREFIX}{pinned_id}"},
                    UpdateExpression="SET assetListSk = :sk, pinned = :t",
                    ConditionExpression="attribute_exists(PK) AND #s <> :removed",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":sk": f"{PINNED_LIST_PREFIX}{registered_at}#{pinned_id}",
                                               ":removed": "removed", ":t": True},
                )
            except Exception as exc:  # noqa: BLE001 — removed in between: leave it unpinned
                if "ConditionalCheckFailed" not in type(exc).__name__ + str(exc):
                    raise
        store.table.put_item(Item={**marker_key, "assetId": pinned_id, "version": sample.version,
                                   "seededAt": registered_at or ""})
        seeded.append(pinned_id)
    return {"seeded": seeded, "skipped": skipped}
