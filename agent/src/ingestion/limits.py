"""Asset kinds and per-kind upload limits (Projects Phase B/C; design §3 "projectUploadLimits").

The proxy's ``GET /projects/:pid/ingestion/capabilities`` and its ``POST /projects/:pid/uploads`` admission
(``ui/server/projects/ingestionNames.ts``, the TypeScript twin) and the Asset_Service ``register_upload`` /
``register_s3_location`` checks all classify and bound a file the same way, so a file the proxy admits is
never refused at registration for its kind or size:

- **kind**: from the content type (``video/*``, ``audio/*``, ``image/*``), falling back to the file-name
  extension, and ``document`` for anything else (every file type is a Project_Asset; documents wait in
  ``queued`` until their indexing phase lands);
- **limits**: per kind, overridable by ``INGESTION_CONFIG.uploadLimits`` (the same JSON the proxy reads).

Pure, no I/O.
"""
from __future__ import annotations

import json
import os
import re
from typing import Mapping, Optional

__all__ = [
    "DEFAULT_UPLOAD_LIMITS",
    "MAX_FILES_PER_BATCH",
    "MULTIPART_THRESHOLD_BYTES",
    "PART_SIZE_BYTES",
    "kind_for",
    "limit_for",
    "limits_from_env",
    "entity_metadata_enabled",
    "admit",
    "output_kind_for",
]

GIB = 1024 * 1024 * 1024
MIB = 1024 * 1024

DEFAULT_UPLOAD_LIMITS: dict[str, int] = {
    "video": 5 * GIB,
    "audio": 5 * GIB,
    "image": 50 * MIB,
    "document": 500 * MIB,
}
MAX_FILES_PER_BATCH = 20
MULTIPART_THRESHOLD_BYTES = 100 * MIB
PART_SIZE_BYTES = 64 * MIB

# Extension fallback when the content type is not a media type (browsers send "" for many files). The
# same sets as the SPA's Upload_Client (`ui/src/projects/upload/kinds.ts`) and the proxy
# (`ui/server/projects/ingestionNames.ts`), so the client picks the limit the server will apply.
VIDEO_EXT = frozenset({"mp4", "mov", "m4v", "webm", "mkv", "avi", "mpg", "mpeg", "mxf", "ts", "mts", "wmv",
                       "flv", "3gp"})
IMAGE_EXT = frozenset({"png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff", "heic", "heif", "avif"})
AUDIO_EXT = frozenset({"mp3", "wav", "m4a", "aac", "ogg", "oga", "flac", "aif", "aiff", "opus", "wma"})

_EXT_RE = re.compile(r"\.([A-Za-z0-9]{1,10})\Z")

# Analysis files a session or an M2C job writes (asset-lineage design §2): registered as kind ``data`` by the
# session output registrar only. Uploads keep :func:`kind_for` (``document``), which the TS twin mirrors.
DATA_EXT = frozenset({"json", "csv", "txt", "srt", "vtt"})
_DATA_TYPES = frozenset({"application/json", "text/csv", "text/plain", "text/vtt", "application/x-subrip"})


def _ext(name: str) -> str:
    """The lower-cased extension (no dot) of the last path segment of ``name``, or ``""``."""
    m = _EXT_RE.search(name.rsplit("/", 1)[-1])
    return m.group(1).lower() if m else ""


def kind_for(content_type: Optional[str], name: Optional[str]) -> str:
    """``video`` | ``audio`` | ``image`` | ``document`` for a declared type and a file name or key.

    The content type wins when it is a media type; otherwise the extension decides; otherwise
    ``document``. Total: every input has a kind.
    """
    ct = (content_type or "").lower()
    if ct.startswith("video/"):
        return "video"
    if ct.startswith("audio/"):
        return "audio"
    if ct.startswith("image/"):
        return "image"
    ext = _ext(name if isinstance(name, str) else "")
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in IMAGE_EXT:
        return "image"
    return "document"


def output_kind_for(content_type: Optional[str], name: Optional[str]) -> str:
    """:func:`kind_for`, except an analysis file (``.json/.csv/.txt/.srt/.vtt`` or its type) is ``data``.

    For session and M2C outputs only; a media type or extension still wins.
    """
    kind = kind_for(content_type, name)
    if kind != "document":
        return kind
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if _ext(name if isinstance(name, str) else "") in DATA_EXT or ct in _DATA_TYPES:
        return "data"
    return kind


def limit_for(kind: str, limits: Mapping[str, int]) -> int:
    """The byte limit for ``kind``; ``data`` files share the ``document`` limit; unknown kinds get 0."""
    if kind == "data" and kind not in limits:
        kind = "document"
    return limits.get(kind, 0)


def limits_from_env(env: Optional[Mapping[str, str]] = None) -> dict[str, int]:
    """The per-kind byte limits: defaults, overridden by ``INGESTION_CONFIG.uploadLimits`` positive ints."""
    env = os.environ if env is None else env
    limits = dict(DEFAULT_UPLOAD_LIMITS)
    raw = env.get("INGESTION_CONFIG", "")
    if not raw:
        return limits
    try:
        cfg = json.loads(raw)
    except (ValueError, TypeError):
        return limits
    overrides = cfg.get("uploadLimits") if isinstance(cfg, dict) else None
    if isinstance(overrides, dict):
        for kind in DEFAULT_UPLOAD_LIMITS:
            v = overrides.get(kind)
            if isinstance(v, int) and not isinstance(v, bool) and v > 0:
                limits[kind] = v
    return limits


def entity_metadata_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """``INGESTION_CONFIG.entityMetadata`` (Phase 1 entity-metadata workflows). Default False.

    Gates whether the entity-metadata toolkit tools are REQUIRED at registration
    (``catalog.missing_tools``); the reducer's own :class:`reducer.Config.entity_metadata` gates
    submission. Both read the same key.
    """
    env = os.environ if env is None else env
    raw = env.get("INGESTION_CONFIG", "")
    if not raw:
        return False
    try:
        cfg = json.loads(raw)
    except (ValueError, TypeError):
        return False
    return bool(cfg.get("entityMetadata")) if isinstance(cfg, dict) else False


def admit(kind: str, size: int, limits: Mapping[str, int]) -> Optional[str]:
    """``None`` when ``size`` fits ``kind``'s limit, else the refusal reason (``empty`` / ``too_large``)."""
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        return "empty"
    if size > limit_for(kind, limits):
        return "too_large"
    return None
