"""Generated_Media candidate selection (ingestion Req 9, design Component 16) — pure, no AWS.

Media a turn (or async work the turn dispatched) produces is registered into the Producing_Turn's Project.
This module decides WHICH objects qualify and how each is named and identified; the
``handlers.generated_media`` Lambda does the S3 and DynamoDB work.

Sources (Req 9.1, 9.6):

- ``artifacts/<sessionId>/…`` objects — and, under the project layout (asset-lineage Phase 3 (a)),
  ``projects/<pid>/sessions/<sessionId>/…`` objects of THIS turn's Project — except ``input/`` (chat copies of things the user attached),
  ``render/`` (render manifests), and files directly in ``output/<uuid>/`` — an M2C workflow's working
  directory, whose stems, face crops, and diarization tracks are analysis intermediates, not deliverables.
  Registering them would flood the Project and run a full ingestion for each. A plan node's deliverable
  (``output/<plan>/<node>/<file>``) is one level deeper and counts.
- ``artifacts/external/<24 hex>[.ext]`` objects (``media.persist_media.key_for``: fal outputs, copied
  content-addressed and session-less), reached only through something that references them.
- Any other ``artifacts/<folder>/…`` key a render manifest of the turn names (:func:`is_rendered_source`):
  the agent sometimes saves under a folder it named itself, and what the user was shown is a deliverable.

- ``artifacts/<uuid>/output/…`` keys of ANOTHER uuid that the turn's ``lineage`` or a render manifest of
  the turn names (asset-lineage R3.4, :func:`invented_output_namespace`): the main agent's code tool, never
  told its session id, writes under a folder it invents. The handler accepts such a key only when it was
  written inside the turn window and the uuid is not an existing session (see ``handlers.generated_media``).

Video, image, audio, ``data`` (JSON/CSV/text/subtitles, asset-lineage design §2) and PDF documents qualify; other
documents are not tracked yet. Each within its size limit. At most :data:`MAX_PER_MESSAGE` per producer
message, in ``LastModified`` then key order.

Naming (:func:`output_display_name`): a workflow names its outputs after itself, not after the media it
ran on, so an M2C output — and a session output whose own name is generic (``transcoded``, ``output``,
``segment_0000``) — is qualified with its source asset and its job: ``meridian_left_trim — Process Media
(transcoded).mp4``. The raw file name stays on the row's ``sourceUri``.

Identity: a session or M2C output is registered IN PLACE (asset-lineage design §3), so its asset id is
derived from (Project, bucket, key) (:func:`location_asset_id`): a replayed message, a duplicate stream
record, or the turn and async paths both seeing the same object all land on the same ``asset_id``, and
``AssetStore.register_asset`` is idempotent on it. A rewrite of the same key is the same asset. (Legacy
``generated`` copies used (Project, key, version), :func:`asset_id_for`; their rows stay readable.)

Job results (:func:`job_outputs`): a ``plan-result/v1`` artifact whose producer ref (``source_ref``, else
``url``) is in the M2C proxy bucket is an M2C output, registered by reference (``source = m2c``); the
durable copy the resolver made of it under ``artifacts/`` is then NOT also registered.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping, Optional
from urllib.parse import unquote

# Defined in projects/ids.py (ships in the agent runtime too: the Moments dedup derives the same ids).
from projects.ids import asset_id_for, location_asset_id  # noqa: F401 -- re-exported

from .limits import admit, output_kind_for
from .names import DISPLAY_NAME_MAX, derive_display_name, split_ext

__all__ = [
    "MAX_PER_MESSAGE",
    "MEDIA_KINDS",
    "REGISTRABLE_KINDS",
    "is_registrable",
    "WINDOW_SLACK_MS",
    "Candidate",
    "JobNode",
    "LineageEntry",
    "asset_id_for",
    "asset_id_in_project_key",
    "display_name_for",
    "external_refs_in",
    "invented_output_namespace",
    "invented_refs_in",
    "is_generic_name",
    "is_session_id",
    "is_session_source",
    "job_label",
    "job_outputs",
    "lineage_entries",
    "location_asset_id",
    "m2c_role",
    "object_version",
    "output_display_name",
    "output_label",
    "parse_location",
    "qualify_name",
    "refs_in_result",
    "select",
    "session_prefix",
    "session_role",
    "session_roots",
]

MAX_PER_MESSAGE = 50
MEDIA_KINDS = frozenset({"video", "image", "audio"})
# Registered in place, so no CopyObject size cap applies; `data` is an analysis file (never indexed).
REGISTRABLE_KINDS = MEDIA_KINDS | {"data"}
# Documents a turn produces that ARE deliverables. A PDF report (content moderation's sg5, staging session
# d6a560a8, was skipped `unsupported_type` and never reached the Project) is indexed like an upload (page text,
# `ingestion/documents.py`), so a report is searchable by what it says. The other deliverable formats — an HTML
# storyboard (staging 2026-10-10..15: three `*_storyboard.html` were skipped and never reached their Projects),
# Markdown, Word, PowerPoint, Excel — are registered too, but STORED: nothing indexes them yet. Archives and
# scripts (zip, py, sh, log) stay untracked: they are working files, not something to hand a person.
_INDEXED_DOCUMENT_EXT = frozenset({"pdf"})
_INDEXED_DOCUMENT_TYPES = frozenset({"application/pdf"})
_STORED_DOCUMENT_EXT = frozenset({"html", "htm", "md", "markdown", "docx", "pptx", "xlsx"})
_STORED_DOCUMENT_TYPES = frozenset({
    "text/html", "text/markdown",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
})
_REGISTRABLE_DOCUMENT_EXT = _INDEXED_DOCUMENT_EXT | _STORED_DOCUMENT_EXT
_REGISTRABLE_DOCUMENT_TYPES = _INDEXED_DOCUMENT_TYPES | _STORED_DOCUMENT_TYPES


def is_registrable(kind: str, content_type: str, key: str) -> bool:
    """True for a kind registered as Generated_Media: media, ``data``, or a PDF ``document``."""
    if kind in REGISTRABLE_KINDS:
        return True
    if kind != "document":
        return False
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    ext = key.rsplit("/", 1)[-1].rsplit(".", 1)[-1].lower() if "." in key.rsplit("/", 1)[-1] else ""
    return ct in _REGISTRABLE_DOCUMENT_TYPES or ext in _REGISTRABLE_DOCUMENT_EXT


def is_indexed_document(content_type: str, key: str) -> bool:
    """True for a document output the pipeline indexes (a PDF); every other registrable document is stored."""
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    name = key.rsplit("/", 1)[-1]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return ct in _INDEXED_DOCUMENT_TYPES or ext in _INDEXED_DOCUMENT_EXT


WINDOW_SLACK_MS = 5_000
MAX_LINEAGE_ENTRIES = 100
MAX_LINEAGE_INPUTS = 20

# Namespaces directly under artifacts/ that are not chat sessions.
_SHARED_NAMESPACES = frozenset({"external", "durable", "imports"})
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# A file directly in an M2C workflow's working directory (`output/<execution uuid>/SPEAKER_00.wav`). A plan
# node's deliverable sits one level deeper (`output/<plan id>/<node id>/<file>`) and is NOT excluded.
_WORKFLOW_FILE_RE = re.compile(rf"^output/{_UUID}/[^/]+$")
_UUID_RE = re.compile(rf"^{_UUID}$")
_EXCLUDED_SESSION_DIRS = ("input/", "render/")
EXTERNAL_REF_RE = re.compile(r"artifacts/external/[0-9a-f]{24}(?:\.[A-Za-z0-9]{2,5})?(?![A-Za-z0-9])")
_EXTERNAL_KEY_RE = re.compile(r"^artifacts/external/([0-9a-f]{24})(\.[A-Za-z0-9]{2,5})?$")
# A key ends at a quote, whitespace, a query string, a fragment, or a JSON escape.
_KEY_TAIL = r"[^\s\"'?#\\<>)\]]+"
_ARTIFACT_REF_RE = re.compile(r"artifacts/[A-Za-z0-9._:-]+/" + _KEY_TAIL)
# A projects-session key (asset-lineage Phase 3 (a)), named in a manifest or result.
_PROJECT_SESSION_REF_RE = re.compile(r"projects/p[0-9a-f]{32}/sessions/[A-Za-z0-9._-]+/" + _KEY_TAIL)


def is_session_id(sid: object) -> bool:
    return isinstance(sid, str) and bool(_SESSION_ID_RE.match(sid)) and sid not in _SHARED_NAMESPACES


def session_prefix(sid: str) -> str:
    """The LEGACY session root ``artifacts/<sid>/`` (render manifests and transcripts always live here)."""
    return f"artifacts/{sid}/"


def session_roots(sid: str, pid: Optional[str] = None) -> list[str]:
    """Every root this session's outputs may be under: legacy first, then ``projects/<pid>/sessions/<sid>/``
    when ``pid`` is a project id (asset-lineage Phase 3 (a)). Flag-independent."""
    from projects.storage_layout import session_roots as _roots

    return _roots(sid, pid)


def _session_rel(key: str, sid: str, pid: Optional[str]) -> Optional[str]:
    """The path of ``key`` under one of this session's roots, else ``None``. A projects-session key counts only
    for THIS Project (``pid``)."""
    for root in session_roots(sid, pid):
        if key.startswith(root):
            return key[len(root):]
    return None


def is_session_source(key: str, sid: str, pid: Optional[str] = None) -> bool:
    """True for a key under one of this session's roots (``artifacts/<sid>/``, or
    ``projects/<pid>/sessions/<sid>/`` of THIS Project) that may be Generated_Media (see module docstring)."""
    if key.endswith("/"):
        return False
    rel = _session_rel(key, sid, pid)
    if not rel or rel.startswith(_EXCLUDED_SESSION_DIRS) or _WORKFLOW_FILE_RE.match(rel):
        return False
    # A data file (JSON/CSV/text) is an output only under `output/`: the session folder's own top-level
    # files are harness state (the proxy's `transcript.json`), never something the turn produced.
    if not rel.startswith("output/") and output_kind_for(None, rel.rsplit("/", 1)[-1]) == "data":
        return False
    return True


def is_rendered_source(key: str, sid: str, pid: Optional[str] = None) -> bool:
    """True for a key a render manifest of THIS session names that may be Generated_Media.

    The agent does not always write under its own session folder: a code-interpreter crop landed at
    ``artifacts/wolf-crop-888104cf/output/…`` (a folder the model named), so the time-window listing of
    ``artifacts/<sid>/`` never saw it. What the turn RENDERED is the evidence it is a deliverable, wherever
    it lives — except another chat's folder (a UUID that is not this session), the shared ``durable/`` and
    ``imports/`` namespaces, an M2C execution folder (``arn:…``), a chat attachment (``/input/``), and an
    M2C workflow's working files.
    """
    if _EXTERNAL_KEY_RE.match(key):
        return True
    if key.startswith("projects/"):
        # The project layout: only THIS session of THIS Project. Another session (or Project) is another
        # chat's folder, exactly like another uuid under artifacts/.
        return is_session_source(key, sid, pid) if key.startswith(tuple(session_roots(sid, pid)[1:])) else False
    m = re.match(r"^artifacts/([^/]+)/(.+)$", key)
    if not m or key.endswith("/"):
        return False
    ns, rel = m.group(1), m.group(2)
    if ns == sid:
        return is_session_source(key, sid)
    if ns in _SHARED_NAMESPACES or ns.startswith(("arn:", "arn_")) or _UUID_RE.match(ns):
        return False
    if f"/{rel}".find("/input/") >= 0 or rel.startswith("render/") or _WORKFLOW_FILE_RE.match(rel):
        return False
    return True


def invented_output_namespace(key: str, sid: Optional[str]) -> Optional[str]:
    """The uuid of an ``artifacts/<uuid>/output/<file>`` key whose uuid is not ``sid``, else ``None``.

    The code tool's invented session-shaped folder (asset-lineage R3.4). An M2C workflow's working files
    (``output/<uuid>/<file>``) stay excluded, as in a session's own folder.
    """
    m = re.match(r"^artifacts/([^/]+)/(output/.+)$", key or "")
    if not m or key.endswith("/") or m.group(1) == sid or not _UUID_RE.match(m.group(1)):
        return None
    return None if _WORKFLOW_FILE_RE.match(m.group(2)) else m.group(1)


def invented_refs_in(text: str, sid: Optional[str]) -> list[str]:
    """Every invented-folder output key (:func:`invented_output_namespace`) named in ``text``."""
    return sorted({k for k in _ARTIFACT_REF_RE.findall(text or "") if invented_output_namespace(k, sid)})


def _all_refs(text: str) -> list[str]:
    return _ARTIFACT_REF_RE.findall(text or "") + _PROJECT_SESSION_REF_RE.findall(text or "")


def rendered_refs_in(text: str, sid: str, pid: Optional[str] = None) -> list[str]:
    """Every key in a render manifest that :func:`is_rendered_source` admits, sorted and unique."""
    return sorted({k for k in _all_refs(text) if is_rendered_source(k, sid, pid)})


def external_refs_in(text: str) -> list[str]:
    """Every ``artifacts/external/<hash>[.ext]`` key named in ``text``, sorted and unique."""
    return sorted(set(EXTERNAL_REF_RE.findall(text or "")))


def _session_refs_in(text: str, sid: str, pid: Optional[str] = None) -> list[str]:
    pat = re.compile("(?:" + "|".join(re.escape(r) for r in session_roots(sid, pid)) + ")" + _KEY_TAIL)
    return sorted({k for k in pat.findall(text or "") if is_session_source(k, sid, pid)})


def refs_in_result(body: str, sid: Optional[str], pid: Optional[str] = None) -> list[str]:
    """Candidate keys named by an async job's or plan's ``result.json``.

    A ``plan-result/v1`` document labels each artifact with a ``role``; a ``source`` is what the work
    READ, so only the other roles count. Any other document is scanned as text.
    """
    text = body or ""
    try:
        doc = json.loads(text)
    except (TypeError, ValueError):
        doc = None
    if isinstance(doc, dict) and doc.get("schema") == "plan-result/v1":
        urls = [
            a.get("url", "")
            for n in doc.get("nodes") or []
            if isinstance(n, dict)
            for a in n.get("artifacts") or []
            if isinstance(a, dict) and a.get("role") != "source" and isinstance(a.get("url"), str)
        ]
        text = "\n".join(urls)
    keys = set(external_refs_in(text))
    if is_session_id(sid):
        keys.update(_session_refs_in(text, sid, pid))
    return sorted(keys)


@dataclass(frozen=True)
class Candidate:
    """One HEADed object that may be registered."""

    key: str
    size: int
    content_type: str
    last_modified: datetime
    version: str  # the S3 VersionId, else the ETag (unversioned bucket)
    versioned: bool  # True when ``version`` is a VersionId
    kind: str
    bucket: str = ""
    checksum: Optional[dict] = None  # ``store.checksum_from_head`` of the HEAD (R5)


def object_version(head: Mapping) -> tuple[str, bool]:
    """``(version, is_version_id)``: the ``VersionId``, or the ETag when the bucket is unversioned."""
    vid = head.get("VersionId")
    if isinstance(vid, str) and vid and vid != "null":
        return vid, True
    return str(head.get("ETag") or "").strip('"'), False


def candidate_from_head(key: str, head: Mapping, limits: Mapping[str, int],
                        bucket: str = "") -> tuple[Optional[Candidate], str]:
    """``(candidate, "")`` when the object qualifies, else ``(None, reason)``."""
    from .store import checksum_from_head

    if (head.get("Metadata") or {}).get("harness-origin") == "project-asset":
        return None, "project_asset_copy"
    content_type = str(head.get("ContentType") or "")
    kind = output_kind_for(content_type, key)
    if not is_registrable(kind, content_type, key):
        return None, "unsupported_type"
    size = int(head.get("ContentLength") or 0)
    refusal = admit(kind, size, limits)
    if refusal is not None:
        return None, refusal
    version, versioned = object_version(head)
    if not version:
        return None, "no_version"
    lm = head.get("LastModified")
    if not isinstance(lm, datetime):
        lm = datetime.fromtimestamp(0, tz=timezone.utc)
    return Candidate(key=key, size=size, content_type=content_type or "application/octet-stream",
                     last_modified=lm, version=version, versioned=versioned, kind=kind, bucket=bucket,
                     checksum=checksum_from_head(head)), ""


def select(candidates: Iterable[Candidate]) -> tuple[list[Candidate], int]:
    """The first :data:`MAX_PER_MESSAGE` in ``LastModified`` then key order, and how many were cut."""
    ordered = sorted({c.key: c for c in candidates}.values(), key=lambda c: (c.last_modified, c.key))
    return ordered[:MAX_PER_MESSAGE], max(0, len(ordered) - MAX_PER_MESSAGE)


def display_name_for(c: Candidate) -> str:
    """A session object keeps its file name; an external one is ``generated-<UTC time>-<hash6><ext>``.

    The time is the object's ``LastModified`` and ``hash6`` comes from its content-addressed key, so a
    replayed message derives the same name (Req 9.13).
    """
    m = _EXTERNAL_KEY_RE.match(c.key)
    if not m:
        return c.key.rsplit("/", 1)[-1]
    ext = m.group(2) or (mimetypes.guess_extension(c.content_type.split(";")[0].strip()) or "")
    stamp = c.last_modified.astimezone(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"generated-{stamp}-{m.group(1)[:6]}{ext}"


# A workflow's own file name carries no information inside a Project: `transcoded.mp4` is what EVERY
# process_media transcode is called, whatever it ran on (verified live: an M2C trim of
# `meridian_left_trim.mp4` was registered as `transcoded.mp4`). Such a stem is qualified with the source
# asset and the job that made it; a specific stem (`meridian_reel`, `shots`) is kept as it is.
_GENERIC_STEMS = frozenset({
    "audio", "audio_16k_mono", "clip", "file", "final", "media", "out", "output", "outputs", "proxy",
    "result", "results", "temp", "tmp", "transcoded", "untitled", "video",
})
# `segment_0000`, `part-2`, `chunk3`, `0001`: a bare index, meaningless without what it indexes.
_NUMBERED_STEM_RE = re.compile(r"^[A-Za-z]{0,16}[ _-]?\d{1,6}$")
# The per-Project ` (n)` the Display_Name allocator appends (``names.with_suffix``), after any extension.
_ALLOCATED_SUFFIX_RE = re.compile(r"\s\(\d+\)$")
_QUALIFIER_SEP = " — "


def is_generic_name(name: str) -> bool:
    """True when ``name``'s stem says nothing about WHICH media it is (see :data:`_GENERIC_STEMS`)."""
    stem = split_ext(name or "")[0].strip().lower()
    return bool(stem) and (stem in _GENERIC_STEMS or bool(_NUMBERED_STEM_RE.match(stem)))


def output_label(tool: object) -> Optional[str]:
    """:func:`job_label` of ``tool``, or ``None`` when there is no tool to name (so no qualifier)."""
    return job_label(tool) if isinstance(tool, str) and tool.strip() else None


def qualify_name(raw_name: str, source_name: Optional[str] = None, label: Optional[str] = None) -> str:
    """``<source> — <Job label> (<raw stem>)<ext>``: a Display_Name that says what this output IS.

    Deterministic in its inputs, keeps ``raw_name``'s extension, and keeps its stem in the parentheses so
    the N outputs of one job stay distinguishable (``segment_0000``, ``segment_0001``) instead of relying
    on the ``(n)`` the allocator would then have to add. Either part may be missing; with NEITHER a
    source asset nor a tool there is nothing to add and ``raw_name`` is returned unchanged. At most
    :data:`ingestion.names.DISPLAY_NAME_MAX` code points — the source name gives way first, since the job
    label and the raw stem are the shorter, more identifying halves.
    """
    stem, ext = split_ext(raw_name or "")
    source = split_ext(_ALLOCATED_SUFFIX_RE.sub("", (source_name or "").strip()))[0].strip()
    parts = [p for p in (source, label) if p]
    if not parts:
        return raw_name
    tail = f" ({stem})" if stem else ""
    over = len(_QUALIFIER_SEP.join(parts)) + len(tail) + len(ext) - DISPLAY_NAME_MAX
    if over > 0 and source:
        source = source[: max(0, len(source) - over)].rstrip()
        parts = [p for p in (source, label) if p]
        if not parts:
            return derive_display_name(raw_name, "")
    return derive_display_name(f"{_QUALIFIER_SEP.join(parts)}{tail}{ext}", "")


def output_display_name(c: Candidate, source_name: Optional[str] = None, label: Optional[str] = None,
                        *, always: bool = False) -> str:
    """The Display_Name of a registered output: :func:`display_name_for`, qualified by the source asset
    and the job that produced it when the raw name is generic — or ``always``, for an M2C job output,
    since every one of those is named by the workflow rather than by the media it ran on."""
    raw = display_name_for(c)
    if not always and not is_generic_name(raw):
        return raw
    return qualify_name(raw, source_name, label)


def session_role(kind: str) -> str:
    """A session output's role (design §3): media is a ``deliverable``, a data file an ``analysis``."""
    return "analysis" if kind == "data" else "deliverable"


_INTERMEDIATE_SEGMENTS = frozenset({"frames", "frame", "segments", "segment", "crops", "thumbnails", "keyframes"})


def m2c_role(kind: str, key: str) -> str:
    """An M2C output's role (design §3): data → ``analysis``; frames, crops and segments (and every image)
    → ``intermediate``; other media (stems, clips, transcodes) → ``deliverable``."""
    if kind == "data":
        return "analysis"
    parts = {p.lower() for p in (key or "").split("/")[:-1]}
    if kind == "image" or parts & _INTERMEDIATE_SEGMENTS:
        return "intermediate"
    return "deliverable"


_TOOL_VERB_PREFIXES = ("start_", "submit_", "run_", "create_", "invoke_")
_TOOL_SUFFIXES = ("_job", "_start", "_async", "_status")


def job_label(tool: object) -> str:
    """The folder label of a job's outputs from its tool name: ``m2c___start_shot_segmentation`` →
    ``Shot Segmentation``. A gateway namespace (``<target>___``), a ``node|`` trio prefix, and a leading
    start/submit verb are dropped. ``M2C job`` when nothing usable is left."""
    name = tool if isinstance(tool, str) else ""
    name = name.split("|", 1)[-1].rsplit("___", 1)[-1].strip().lower()
    for p in _TOOL_VERB_PREFIXES:
        if name.startswith(p):
            name = name[len(p):]
            break
    for suf in _TOOL_SUFFIXES:
        if name.endswith(suf):
            name = name[: -len(suf)]
            break
    words = [w for w in re.split(r"[_\-\s.]+", name) if w]
    return " ".join(w[:1].upper() + w[1:] for w in words) or "M2C job"


def parse_location(uri: object, ingest_bucket: str) -> Optional[tuple[str, str]]:
    """``(bucket, key)`` of an ``s3://bucket/key`` ref, or of an https/CloudFront URL or bare key whose path
    is under ``artifacts/`` or ``projects/`` (those live in the Ingest bucket). ``None`` otherwise."""
    if not isinstance(uri, str) or not uri:
        return None
    if uri.startswith("s3://"):
        bucket, _, key = uri[5:].partition("/")
        key = key.split("?", 1)[0]
        return (bucket, key) if bucket and key and not key.endswith("/") else None
    path = uri.split("?", 1)[0].split("#", 1)[0]
    host = ""
    if path.startswith(("https://", "http://")):
        host = path.split("/", 3)[2].lower() if path.count("/") >= 2 else ""
        path = path.split("/", 3)[3] if path.count("/") >= 3 else ""
    path = unquote(path.lstrip("/"))
    if not path or path.endswith("/") or ".." in path:
        return None
    vhost = _S3_VHOST_RE.match(host)
    if vhost:  # a presigned virtual-hosted S3 URL names its bucket in the host
        return vhost.group(1), path
    if ingest_bucket and path.startswith(("artifacts/", "projects/")):
        return ingest_bucket, path
    return None


_PROJECT_KEY_RE = re.compile(r"^projects/([^/]+)/([^/]+)/")
_S3_VHOST_RE = re.compile(r"^([a-z0-9][a-z0-9.-]{1,61}[a-z0-9])\.s3(?:[.-][a-z0-9-]+)*\.amazonaws\.com$")


def asset_id_in_project_key(key: str, pid: str) -> Optional[str]:
    """The asset id of a ``projects/<pid>/<assetId>/…`` key of THIS Project, else ``None``."""
    from projects import schema

    m = _PROJECT_KEY_RE.match(key or "")
    if not m or m.group(1) != pid or not schema.is_asset_id(m.group(2)):
        return None
    return m.group(2)


@dataclass(frozen=True)
class LineageEntry:
    """One ``lineage`` item of a turn message (design §3): an output and the inputs it was made from."""

    output: str
    inputs: tuple[str, ...]
    tool: Optional[str] = None


def lineage_entries(raw: object) -> list[LineageEntry]:
    """The well-formed ``lineage`` entries of a turn message; anything malformed is dropped, never raised."""
    out: list[LineageEntry] = []
    for e in (raw if isinstance(raw, list) else [])[:MAX_LINEAGE_ENTRIES]:
        if not isinstance(e, dict) or not isinstance(e.get("output"), str) or not e["output"]:
            continue
        inputs = tuple(dict.fromkeys(i for i in (e.get("inputs") or []) if isinstance(i, str) and i))
        tool = e.get("tool") if isinstance(e.get("tool"), str) and e.get("tool") else None
        out.append(LineageEntry(output=e["output"], inputs=inputs[:MAX_LINEAGE_INPUTS], tool=tool))
    return out


@dataclass(frozen=True)
class JobNode:
    """One succeeded node of a job result whose outputs are in the M2C proxy bucket (design §3 "Job stream")."""

    node_id: str
    tool: Optional[str]
    keys: tuple[str, ...]  # M2C-bucket keys of its output/analysis artifacts
    sources: tuple[str, ...]  # the media it ran on (role=source urls)
    output_prefix: Optional[str] = None
    # (M2C key, url of the durable copy the job settle made of it) — the copy is not registered, but an input that
    # names it must link to the M2C output's asset (``lineage_contract.ALIAS_SK_PREFIX``).
    copies: tuple[tuple[str, str], ...] = ()


_SUCCEEDED_NODE = frozenset({"completed", "succeeded"})


def _m2c_key(art: Mapping, m2c_bucket: str) -> Optional[str]:
    for field_ in ("source_ref", "url"):
        v = art.get(field_)
        if isinstance(v, str) and v.startswith(f"s3://{m2c_bucket}/"):
            key = v[len(f"s3://{m2c_bucket}/"):].split("?", 1)[0]
            # A harness key is never an M2C output, even where one bucket serves both (source mode).
            if key and not key.endswith("/") and not key.startswith(("artifacts/", "projects/")):
                return key
    return None


def job_outputs(body: str, sid: Optional[str], m2c_bucket: str,
                pid: Optional[str] = None) -> tuple[list[str], list[JobNode]]:
    """``(session_keys, m2c_nodes)`` a job's or plan's ``result.json`` names.

    ``session_keys`` is :func:`refs_in_result` minus every artifact whose producer ref is in the M2C bucket
    (its durable copy is not registered, the original is). ``m2c_nodes`` lists each SUCCEEDED node with its
    M2C-bucket outputs. With no M2C bucket configured, every artifact is treated as before.
    """
    try:
        doc = json.loads(body or "")
    except (TypeError, ValueError):
        doc = None
    if not m2c_bucket or not (isinstance(doc, dict) and doc.get("schema") == "plan-result/v1"):
        return refs_in_result(body, sid, pid), []
    kept: list[dict] = []
    nodes: list[JobNode] = []
    for n in doc.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        arts = [a for a in n.get("artifacts") or [] if isinstance(a, dict)]
        keys: list[str] = []
        copies: list[tuple[str, str]] = []
        for a in arts:
            if a.get("role") == "source":
                continue
            k = _m2c_key(a, m2c_bucket)
            if k is None:
                kept.append(a)
            elif str(n.get("status") or "").lower() in _SUCCEEDED_NODE:
                keys.append(k)
                url = a.get("url")
                if isinstance(url, str) and url and not url.startswith(f"s3://{m2c_bucket}/"):
                    copies.append((k, url.split("?", 1)[0]))
        if keys:
            handle = n.get("handle") if isinstance(n.get("handle"), dict) else {}
            prefix = handle.get("output_prefix") if isinstance(handle.get("output_prefix"), str) else None
            nodes.append(JobNode(
                node_id=str(n.get("node_id") or ""),
                tool=n.get("tool") if isinstance(n.get("tool"), str) else None,
                keys=tuple(dict.fromkeys(keys)),
                sources=tuple(a["url"] for a in arts if a.get("role") == "source" and isinstance(a.get("url"), str)),
                output_prefix=prefix or None,
                copies=tuple(dict.fromkeys(copies)),
            ))
    rest = json.dumps({"schema": "plan-result/v1", "nodes": [{"artifacts": kept}]})
    return refs_in_result(rest, sid, pid), nodes
