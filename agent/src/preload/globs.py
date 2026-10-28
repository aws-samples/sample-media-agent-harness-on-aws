"""Expand a project manifest's ``assets`` list against the files in its project directory (pure).

Glob semantics (path-aware; :mod:`fnmatch` is not used because its ``*`` crosses ``/``):

- ``*``  matches any run of characters within ONE path segment (never ``/``);
- ``?``  matches exactly one character other than ``/``;
- ``**`` as a whole segment matches any number of segments, including zero
  (``assets/**`` matches ``assets/a.png`` and ``assets/x/y/b.mp4``; ``a/**/b`` matches ``a/b``).
  A ``**`` that is not a whole segment (``a**b``) behaves like ``*``.

Every other character matches itself. A pattern must match the WHOLE project-relative path.
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from ingestion.limits import kind_for

from .manifest import RAW, AssetItem, AssetSpec

MANIFEST_NAME = "manifest.json"
ASSETS_DIR = "assets"


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a glob (semantics in the module docstring) into a full-match regex."""
    segments = pattern.split("/")
    out: list[str] = []
    last = len(segments) - 1
    for i, seg in enumerate(segments):
        if seg == "**":
            # A globstar segment absorbs its own separator: zero or more whole segments.
            out.append(".*" if i == last else "(?:[^/]+/)*")
            continue
        part: list[str] = []
        j = 0
        while j < len(seg):
            ch = seg[j]
            if ch == "*":
                while j + 1 < len(seg) and seg[j + 1] == "*":
                    j += 1
                part.append("[^/]*")
            elif ch == "?":
                part.append("[^/]")
            else:
                part.append(re.escape(ch))
            j += 1
        out.append("".join(part) + ("/" if i != last else ""))
    return re.compile("".join(out))


def glob_match(pattern: str, path: str) -> bool:
    return glob_to_regex(pattern).fullmatch(path) is not None


def is_placeable(rel: str) -> bool:
    """A project-relative key a glob may place: a file (not a ``/`` marker), not a dotfile, not the manifest."""
    if not rel or rel.endswith("/"):
        return False
    if posixpath.basename(rel).startswith("."):
        return False
    return rel != MANIFEST_NAME


def folder_for(rel: str) -> Optional[str]:
    """The project folder a project-relative file files into: its directory with a leading ``assets/``
    removed. ``assets/a.png`` → ``None`` (top level); ``assets/clips/day1/x.mp4`` → ``clips/day1``;
    ``media/x.mp4`` → ``media``."""
    parent = posixpath.dirname(rel)
    if parent == ASSETS_DIR:
        return None
    if parent.startswith(ASSETS_DIR + "/"):
        parent = parent[len(ASSETS_DIR) + 1:]
    return parent or None


@dataclass(frozen=True)
class PlannedAsset:
    """One asset to place, with its project-relative ``spec`` and target ``folder``."""

    spec: AssetSpec
    folder: Optional[str]


def expand_assets(items: Sequence[AssetItem], files: Iterable[str]) -> tuple[list[PlannedAsset], list[str]]:
    """Resolve manifest ``items`` against the project-relative ``files``.

    Returns ``(planned, unmatched_patterns)``. Each file is placed at most once: an explicit entry wins
    over a glob match of the same file (it carries the mode and analysis), and among globs the first
    match wins. Files an explicit entry names as its ``analysis`` outputs are never also placed by a glob,
    so ``assets/**`` does not ingest a preanalyzed bundle's JSON as separate documents. Explicit entries
    are kept even when their media is absent from ``files`` (placement then reports the missing object).
    """
    placeable = sorted(f for f in set(files) if is_placeable(f))
    explicit = [it for it in items if isinstance(it, AssetSpec)]
    claimed: set[str] = {e.media for e in explicit}
    reserved: set[str] = {v for e in explicit for v in e.analysis.values()}

    planned: list[PlannedAsset] = []
    seen: set[str] = set()
    unmatched: list[str] = []
    for item in items:
        if isinstance(item, AssetSpec):
            if item.media in seen:
                continue
            seen.add(item.media)
            planned.append(PlannedAsset(spec=item, folder=folder_for(item.media)))
            continue
        rx = glob_to_regex(item)
        hits = [f for f in placeable if rx.fullmatch(f)]
        if not hits:
            unmatched.append(item)
        for rel in hits:
            if rel in seen or rel in claimed or rel in reserved:
                continue
            seen.add(rel)
            planned.append(PlannedAsset(
                spec=AssetSpec(media=rel, kind=kind_for(None, rel), mode=RAW, analysis={}),
                folder=folder_for(rel)))
    return planned, unmatched
