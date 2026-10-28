"""Place a preload package's assets so the deployed pipeline ingests them (design Part 2).

Two modes, no new ingestion code:

- ``raw`` — copy the media into the project's import inbox (``projects/<pid>/import/``). The deployed
  ``ImportSweepFn`` turns it into a ``queued`` asset and the pipeline analyzes it normally.
- ``preanalyzed`` — copy the media + its analysis files UNDER the project (``projects/<pid>/<assetId>/``,
  where the IngestTick role can read them and project purge can delete them), then register a normal
  ``queued`` asset whose ``steps`` are pre-seeded SUCCEEDED (see :mod:`preload.analysis`). The next tick
  builds + indexes with no analysis job submitted.

Where preanalyzed bytes live is a design fork the doc left loose (manifest paths are project-relative).
Resolved here: copy them under ``projects/<pid>/<assetId>/`` — the only location the ingestion roles read AND
the purge cleans. The in-place source is the copied media; the SRC guard keys dedupe a re-deploy.

Everything is injected (``s3`` client, ``store`` with ``register_asset``), so this is testable with moto /
fakes.
"""
from __future__ import annotations

import json
import posixpath
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from ingestion.folders import folder_or_error
from ingestion.import_inbox import import_prefix
from ingestion.store import checksum_from_head
from projects import schema
from projects.ids import asset_id_for

from . import analysis
from .manifest import AssetSpec

# The analysis-key docs whose shape :mod:`preload.analysis` reads to compute facts (and so to validate).
_FACT_DOCS = ("shot-list", "transcript", "probe")


class AssetPlacementError(RuntimeError):
    """Placing or registering one asset failed; the caller fails that asset, not the whole deploy."""


@dataclass(frozen=True)
class PlacedAsset:
    asset_id: Optional[str]   # None for raw (the sweep assigns it later)
    mode: str
    key: str                  # the import key (raw) or the registered source key (preanalyzed)
    created: bool


def _leaf(key: str) -> str:
    return posixpath.basename(key.rstrip("/"))


def _object_exists(s3: Any, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except Exception:  # noqa: BLE001 — head raises 404/NoSuchKey; treat any miss as "not there"
        return False


def _copy_if_absent(s3: Any, bucket: str, src_key: str, dst_key: str) -> bool:
    """Copy within the bucket unless the destination already exists. Returns whether it copied."""
    if _object_exists(s3, bucket, dst_key):
        return False
    s3.copy_object(Bucket=bucket, Key=dst_key, CopySource={"Bucket": bucket, "Key": src_key})
    return True


def place_raw(s3: Any, bucket: str, pid: str, spec: AssetSpec, *, folder: Optional[str] = None) -> PlacedAsset:
    """Copy the media into ``projects/<pid>/import/`` (optionally under ``folder``); the sweep does the rest.

    The sweep files ``projects/<pid>/import/<folder>/<file>`` in project folder ``<folder>``
    (``ingestion.import_inbox.import_folder``), so this is how a package's ``assets/clips/x.mp4`` lands in
    ``clips``. ``spec.media`` is a bucket key.
    """
    leaf = _leaf(spec.media)
    dst = import_prefix(pid) + (f"{folder.strip('/')}/" if folder else "") + leaf
    created = _copy_if_absent(s3, bucket, spec.media, dst)
    return PlacedAsset(asset_id=None, mode="raw", key=dst, created=created)


def _read_json(s3: Any, bucket: str, key: str) -> Any:
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    return json.loads(body)


def register_preanalyzed(
    s3: Any, store: Any, bucket: str, pid: str, owner_sub: str, owner_display: str, spec: AssetSpec,
    *, folder: Optional[str] = None,
) -> PlacedAsset:
    """Copy media + analysis under the project and register a ``queued`` asset with seeded ``steps``.

    ``spec`` paths are bucket keys (the runner resolves the manifest's project-relative paths). ``folder``
    files the asset in that project folder; one that does not normalize files it at the top level, the
    same fallback the import sweep applies to a raw file.

    Raises :class:`AssetPlacementError` (or :class:`preload.analysis.PreanalyzedError`) on a shape mismatch,
    so the caller fails this one asset cleanly rather than indexing garbage.
    """
    if spec.kind != "video":
        raise AssetPlacementError(f"preanalyzed is supported for 'video' only, not {spec.kind!r}")

    # A stable asset id derived from the PACKAGE source key, so a re-deploy maps to the same asset.
    asset_id = asset_id_for(spec.kind, pid, spec.media, "preload")
    base = f"{schema.project_prefix(pid)}{asset_id}/"
    media_dst = f"{base}source/{_leaf(spec.media)}"
    _copy_if_absent(s3, bucket, spec.media, media_dst)

    # Copy every analysis object under the asset, recording its new uri per manifest key. Each value is an
    # OBJECT key the step output points at (for ``frames`` that is the frame-manifest JSON the tick reads via
    # ``_read``; the jpg frames pick_frames resolves are best-effort — the build succeeds without them, so
    # co-locating the image prefix is a documented refinement, not a correctness requirement).
    uris: dict[str, str] = {}
    for key, src in spec.analysis.items():
        if src.endswith("/"):
            raise AssetPlacementError(
                f"analysis {key!r} must point at an object (e.g. the frame-manifest JSON), not a prefix {src!r}")
        dst = f"{base}analysis/{key}/{_leaf(src)}"
        _copy_if_absent(s3, bucket, src, dst)
        uris[key] = f"s3://{bucket}/{dst}"

    # Read only the docs needed to compute (and validate) facts.
    docs: dict[str, Any] = {}
    for key in _FACT_DOCS:
        src = spec.analysis.get(key)
        if src and not src.endswith("/"):
            try:
                docs[key] = _read_json(s3, bucket, f"{base}analysis/{key}/{_leaf(src)}")
            except Exception as exc:  # noqa: BLE001
                raise AssetPlacementError(f"could not read analysis {key!r}: {exc}") from exc

    steps = analysis.build_video_steps(asset_id, uris, docs)
    analysis.verify_builds(steps)  # the load-bearing invariant: builds with nothing to submit

    head = None
    try:
        head = s3.head_object(Bucket=bucket, Key=media_dst, ChecksumMode="ENABLED")
    except Exception:  # noqa: BLE001 — no checksum just skips the HASH guard
        head = None
    checksum = checksum_from_head(head) if head else None
    size = int((head or {}).get("ContentLength") or 0)

    folder_norm, folder_err = folder_or_error(folder) if folder else (None, None)
    if folder_err:
        folder_norm = None
    existing = store.get_asset(pid, asset_id)
    row = store.register_asset(
        pid=pid,
        asset_id=asset_id,
        source="preload",
        source_uri=f"s3://{bucket}/{media_dst}",
        source_bucket=bucket,
        source_key=media_dst,
        source_size=size,
        content_type="video/mp4",
        file_name=_leaf(spec.media),
        added_by=owner_sub,
        added_by_display=owner_display,
        extra_attrs={"steps": steps},
        folder=folder_norm,
        checksum=checksum,
    )
    created = existing is None and row.get("assetId") == asset_id and not row.get("deduped")
    return PlacedAsset(asset_id=asset_id, mode="preanalyzed", key=media_dst, created=created)
