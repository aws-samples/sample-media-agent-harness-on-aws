"""Which asset of a Project is at an S3 location — ONE answer for every path that asks.

The UI's ``resolve_locations`` (trim, keyframes, chat cards, galleries, move-to-folder), lineage capture ("what was
this made from") and generated-media registration all call :func:`asset_for_location`, so a new way of finding an asset
reaches every path at once. It used to be written out three times, and only one of them followed a copy to its
original: trimming a TTS chunk worked while its "made from" link was still dropped.

The chain, in order (each step only if the caller can run it — a missing client or table skips it):
  1. an ``artifacts/m2c/<bucket>/<key>`` reference names the object M2C wrote, in place;
  2. an asset key (``projects/<pid>/<assetId>/…`` in our bucket) names its id;
  3. the SRC guard — the record that maps a registered location to its asset;
  4. our COPY of another system's output → the original it records (``origin`` metadata, written by
     result_resolution.make_s3_importer) → that original's asset;
  5. a lineage alias (a durable copy the lineage edges recorded);
  6. LEGACY — remove once copies made before 2026-11-15 are gone: a pre-origin session copy of a job output,
     reconstructed from the job record.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from projects import schema

from . import generated as gen

_M2C_REF_PREFIX = "artifacts/m2c/"
_JOB_OUTPUT_COPY_RE = re.compile(
    r"^(?:projects/p[0-9a-f]{32}/sessions/[^/]+|artifacts/[^/]+)/output/(?P<exec>[A-Za-z0-9-]{8,80})/(?P<rel>[^?#]+)$")


def asset_for_location(store: Any, pid: str, bucket: str, key: str, *, ingest_bucket: str = "",
                       s3: Any = None, edges: Any = None) -> Optional[str]:
    """The asset id at ``bucket``/``key`` in Project ``pid``, or ``None``. Never raises."""
    if not bucket or not key:
        return None
    if key.startswith(_M2C_REF_PREFIX):
        bucket, _, key = key[len(_M2C_REF_PREFIX):].partition("/")
        if not bucket or not key:
            return None
    ours = not ingest_bucket or bucket == ingest_bucket
    if ours:
        aid = gen.asset_id_in_project_key(key, pid)
        if aid:
            return aid
    aid = store.asset_id_for_location(pid, bucket, key)
    if aid:
        return aid
    if ours and s3 is not None:
        aid = _copy_origin_asset(store, pid, bucket, key, s3)
        if aid:
            return aid
    if edges is not None:
        try:
            aid = edges.alias_of(pid, bucket, key)
        except Exception:  # noqa: BLE001 - an unreadable alias table answers nothing
            aid = None
        if aid:
            return aid
    return _job_output_copy_asset(store, pid, key) if ours else None


def _copy_origin_asset(store: Any, pid: str, bucket: str, key: str, s3: Any) -> Optional[str]:
    try:
        meta = s3.head_object(Bucket=bucket, Key=key).get("Metadata") or {}
    except Exception:  # noqa: BLE001 - not there, or not readable: no origin to follow
        return None
    origin = str(meta.get("origin") or "")
    if not origin.startswith("s3://") or "/" not in origin[5:]:
        return None
    o_bucket, _, o_key = origin[5:].partition("/")
    return store.asset_id_for_location(pid, o_bucket, o_key)


def _job_output_copy_asset(store: Any, pid: str, key: str) -> Optional[str]:
    """LEGACY (step 6): a session's pre-origin copy of a job output (``<session output>/<execution id>/<file>``),
    found through the job record: the output at ``<outputPrefix><file>``, else the one output of that file name."""
    m = _JOB_OUTPUT_COPY_RE.match(key or "")
    finder = getattr(store, "job_for_execution", None)
    if not m or finder is None:
        return None
    rel = m.group("rel")
    job = finder(pid, m.group("exec"))
    if not job:
        return None
    prefix = str(job.get("outputPrefix") or "")
    if prefix.startswith("s3://") and "/" in prefix[5:]:
        p_bucket, _, p_key = prefix[5:].partition("/")
        aid = store.asset_id_for_location(pid, p_bucket, f"{p_key}{rel}")
        if aid:
            return aid
    name = rel.rsplit("/", 1)[-1]
    matches = []
    for out_id in list(job.get("outputs") or [])[:200]:
        row = store.get_asset(pid, out_id) if schema.is_asset_id(out_id) else None
        if row and str(row.get("sourceUri") or "").rsplit("/", 1)[-1] == name:
            matches.append(out_id)
    return matches[0] if len(matches) == 1 else None
