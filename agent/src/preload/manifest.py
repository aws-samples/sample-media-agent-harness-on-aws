"""A preload project's ``manifest.json`` — parse and validate (design "The preload package standard").

Pure: this module reads no S3 and no DynamoDB. It turns one project directory's raw JSON into a typed
record and refuses a malformed manifest with a precise :class:`PreloadManifestError`, so a bad project
fails with a message that names the offending field rather than crashing deep in a registrar.

A package is a folder of project directories; each directory that holds a ``manifest.json`` is one
project (see :mod:`preload.package`). The manifest describes only that project, and every path in it is
RELATIVE TO THE PROJECT DIRECTORY::

    {
      "version": 2,
      "name": "Demo Catalog",
      "indexing": true,                       // optional, default true
      "assets": [                             // optional, default ["assets/**"]
        "assets/*.mp4",                       // a glob: every match is placed raw
        "assets/clips/**",
        { "media": "assets/x.mp4", "mode": "preanalyzed",          // an explicit entry
          "analysis": { "shot-list": "analysis/x/shots.json", "…": "…" } }
      ]
    }

An explicit entry's ``kind`` is optional (inferred from the extension with ``ingestion.limits.kind_for``);
``mode`` defaults to ``raw`` without ``analysis`` and ``preanalyzed`` with it. Owner is NOT in the
manifest — it is the single ``ownerEmail`` deploy input. The project's skill is not in the manifest
either: it is ``skills/SKILL.md`` beside it.
"""
from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from typing import Any, Mapping, Union

from ingestion.limits import kind_for
from projects.schema import ASSET_KINDS

SUPPORTED_VERSION = 2
RAW = "raw"
PREANALYZED = "preanalyzed"
MODES = (RAW, PREANALYZED)
DEFAULT_ASSETS = ("assets/**",)


class PreloadManifestError(ValueError):
    """A manifest that cannot be used as written (names the offending field)."""


@dataclass(frozen=True)
class AssetSpec:
    """One asset to place. ``media`` / ``analysis`` values are project-relative in a parsed manifest and
    bucket keys once the runner resolves them against the project directory."""

    media: str
    kind: str
    mode: str
    analysis: dict[str, str] = field(default_factory=dict)


# One ``assets`` item: a glob pattern (always raw) or an explicit entry.
AssetItem = Union[str, AssetSpec]


@dataclass(frozen=True)
class ProjectManifest:
    name: str
    indexing: bool
    assets: tuple[AssetItem, ...]


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise PreloadManifestError(msg)


def _str(value: Any, where: str) -> str:
    _require(isinstance(value, str) and value.strip() != "", f"{where} must be a non-empty string")
    return value


def _rel_path(value: Any, where: str) -> str:
    """A non-empty path relative to the project directory that cannot climb out of it."""
    raw = _str(value, where).strip()
    _require(not raw.startswith("/"), f"{where} must be relative to the project directory, got {raw!r}")
    norm = posixpath.normpath(raw)
    _require(norm != "." and norm != ".." and not norm.startswith("../"),
             f"{where} must stay inside the project directory, got {raw!r}")
    # normpath drops a trailing "/"; keep it so a prefix (rejected later where it matters) stays visible.
    return norm + ("/" if raw.endswith("/") else "")


def _asset(raw: Mapping, where: str) -> AssetSpec:
    media = _rel_path(raw.get("media"), f"{where}.media")
    _require(not media.endswith("/"), f"{where}.media must name a file, not a folder: {media!r}")
    kind_raw = raw.get("kind")
    if kind_raw is None:
        kind = kind_for(None, media)
    else:
        kind = _str(kind_raw, f"{where}.kind")
    _require(kind in ASSET_KINDS, f"{where}.kind must be one of {ASSET_KINDS}, got {kind!r}")
    analysis_raw = raw.get("analysis")
    analysis: dict[str, str] = {}
    if analysis_raw is not None:
        _require(isinstance(analysis_raw, Mapping), f"{where}.analysis must be an object")
        for label, uri in analysis_raw.items():
            analysis[_str(label, f"{where}.analysis key")] = _rel_path(uri, f"{where}.analysis[{label!r}]")
    mode = raw.get("mode") or (PREANALYZED if analysis else RAW)
    _require(mode in MODES, f"{where}.mode must be one of {MODES}, got {mode!r}")
    if mode == PREANALYZED:
        _require(bool(analysis), f"{where} is preanalyzed but lists no analysis outputs")
    return AssetSpec(media=media, kind=kind, mode=mode, analysis=analysis)


def _item(raw: Any, where: str) -> AssetItem:
    if isinstance(raw, str):
        return _rel_path(raw, where)
    _require(isinstance(raw, Mapping), f"{where} must be a glob string or an object")
    return _asset(raw, where)


def parse_manifest(raw: Any, *, where: str = "manifest") -> ProjectManifest:
    """Validate one project's decoded ``manifest.json``, or raise :class:`PreloadManifestError`."""
    _require(isinstance(raw, Mapping), f"{where} must be a JSON object")
    version = raw.get("version")
    _require(version == SUPPORTED_VERSION and not isinstance(version, bool),
             f"{where}.version must be {SUPPORTED_VERSION}, got {version!r}")
    name = _str(raw.get("name"), f"{where}.name").strip()
    indexing = raw.get("indexing", True)
    _require(isinstance(indexing, bool), f"{where}.indexing must be true or false")
    if "assets" in raw and raw["assets"] is not None:
        assets_raw = raw["assets"]
        _require(isinstance(assets_raw, list), f"{where}.assets must be a list")
        assets = tuple(_item(a, f"{where}.assets[{i}]") for i, a in enumerate(assets_raw))
    else:
        assets = DEFAULT_ASSETS
    return ProjectManifest(name=name, indexing=indexing, assets=assets)
