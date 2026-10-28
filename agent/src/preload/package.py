"""Find the project directories of a preload package in the Ingest bucket (design "package standard").

A package root holds one directory per project; a directory is a project only if it has a
``manifest.json`` directly inside it. Anything else at the root (a README, a directory with no manifest,
``__MACOSX/`` from a Finder zip) is ignored and logged. There is no root manifest.

All functions take an injected ``s3`` client, so they run against moto in tests.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterator, Optional

log = logging.getLogger("preload.package")

MANIFEST_NAME = "manifest.json"


@dataclass(frozen=True)
class ProjectDir:
    name: str     # the directory name, e.g. ``lab01``
    prefix: str   # the bucket prefix, ending in ``/`` (e.g. ``preload/lab01/``)


def root_prefix(root: str) -> str:
    """``root`` as a listing prefix: ``""`` for the bucket root, else ending in exactly one ``/``."""
    root = (root or "").strip().strip("/")
    return f"{root}/" if root else ""


def object_exists(s3: Any, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except Exception:  # noqa: BLE001 — head raises 404/NoSuchKey; treat any miss as "not there"
        return False


def _paginate(s3: Any, **kw: Any) -> Iterator[dict]:
    token: Optional[str] = None
    while True:
        page = dict(kw)
        if token:
            page["ContinuationToken"] = token
        resp = s3.list_objects_v2(**page)
        yield resp
        if not resp.get("IsTruncated"):
            return
        token = resp.get("NextContinuationToken")


def discover_projects(s3: Any, bucket: str, root: str) -> list[ProjectDir]:
    """The project directories directly under ``root``, in key order. A directory without a
    ``manifest.json`` is skipped with a log line."""
    prefix = root_prefix(root)
    found: list[ProjectDir] = []
    for resp in _paginate(s3, Bucket=bucket, Prefix=prefix, Delimiter="/"):
        for cp in resp.get("CommonPrefixes", []) or []:
            dir_prefix = cp["Prefix"]
            name = dir_prefix[len(prefix):].rstrip("/")
            if not name:
                continue
            if object_exists(s3, bucket, f"{dir_prefix}{MANIFEST_NAME}"):
                found.append(ProjectDir(name=name, prefix=dir_prefix))
            else:
                log.info("preload: ignoring s3://%s/%s (no %s)", bucket, dir_prefix, MANIFEST_NAME)
    found.sort(key=lambda d: d.prefix)
    return found


def list_files(s3: Any, bucket: str, prefix: str) -> list[str]:
    """Every object key under ``prefix``, relative to it (``/`` folder markers included; callers filter)."""
    out: list[str] = []
    for resp in _paginate(s3, Bucket=bucket, Prefix=prefix):
        for obj in resp.get("Contents", []) or []:
            rel = obj["Key"][len(prefix):]
            if rel:
                out.append(rel)
    return out


def read_bytes(s3: Any, bucket: str, key: str) -> bytes:
    return s3.get_object(Bucket=bucket, Key=key)["Body"].read()
