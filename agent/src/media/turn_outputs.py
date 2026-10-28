"""Per-turn ledger of the files THIS runtime wrote, and what each was made from (lineage).

Two consumers, one ledger:

* The render auto-harvester's cross-session URL filter (``hooks.actions``). A path is not an
  identity: the main agent's code tool once wrote the user's clip to ``artifacts/<invented
  uuid>/output/`` (staging session 833a079f), and the filter -- which reads the session from the
  PATH -- dropped the user's own clip as "another session's". A key this runtime is known to
  have written during the turn is this session's output wherever it sits.
* The turn-end ``execution_summary.lineage`` (``main.py``), forwarded by the proxy in the
  Generated_Media turn message so the registrar can record ``derivedFrom`` (lineage design §3/§4):
  ``[{output, inputs, tool}]`` in canonical ``s3://bucket/key`` form.

Fed from the hook provider's ``AfterToolCallEvent`` (before the render rule fires, so the filter
sees this result's writes) and reset when an invocation starts. Module state, like the render
batch beside it: one invocation runs at a time per runtime process.

What counts as a write, per tool:

* ``artifact`` -- the ``key`` an upload/adopt/import returned (an import is derived from its
  ``source_uri``);
* ``crop_video`` -- the ``key`` it returned, derived from its ``source_url``;
* ``code_interpreter`` -- own refs (skills-bucket ``artifacts/`` keys) first appearing in the tool
  RESULT are outputs (when the path names ANOTHER session, only if one HEAD shows the object was
  written this turn -- the invented-uuid case, without letting a printed listing of someone
  else's files pass); a ref the code DID contain is an output only when its object was modified
  during this turn and no earlier tool this turn wrote it. Own ``artifacts/`` / ``projects/``
  refs in the code are the inputs. HEADs are capped per call.

Never raises into the hook: every entry point swallows its own failures.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from functools import lru_cache
from typing import Any, Iterable, Mapping, Optional

from media import artifact_fetch
from projects import lineage_contract as lc

logger = logging.getLogger(__name__)

__all__ = [
    "open_turn",
    "note_write",
    "note_tool_result",
    "is_own_write",
    "lineage",
    "own_writes",
    "note_external_copy",
    "merge_lineage",
]

# A candidate reference inside free text (code, stdout, a JSON tool result). Stops at whitespace,
# quotes, brackets, backslashes (JSON escapes) and commas.
_STOP = r"\s'\"<>()\[\]{}`\\,"
_URL_RE = re.compile(rf"(?:s3|https?)://[^{_STOP}]+", re.IGNORECASE)
_TRAILING_PUNCT = ".;:!?"

_MAX_LINEAGE = 100
_MAX_INPUTS = 20          # derivedFrom is capped at 20 asset ids (design §2)
_MAX_HEADS_PER_CALL = 8   # bound the HEADs one code_interpreter result can cost
_CLOCK_SKEW_S = 5.0

_turn_started_at: float = 0.0
_own: set[str] = set()
# output canonical uri -> {"tool": str, "inputs": [canonical uri, ...]}, insertion-ordered.
_lineage: dict[str, dict] = {}

# ---- lineage edges (``projects.lineageEdges``; nothing below is used while the flag is off) ----
# A third-party URL (fal.media) -> the canonical uri of OUR mirrored copy. Process-level, NOT reset per turn: a
# follow-up turn ("now animate it") routinely quotes the URL an earlier turn produced. Bounded LRU.
_external_by_origin: "dict[str, str]" = {}
_MAX_EXTERNAL_ORIGINS = 2000
# Copies the mirror made this turn that no tool result has claimed yet (canonical uri -> origin url).
_unclaimed_copies: dict[str, str] = {}
# An external output URL a tool returned before anything mirrored it (origin url -> entry kwargs).
_pending_external: dict[str, dict] = {}
# A submitted async job (fal queue request id, ...) -> what its eventual output is made from. Process-level.
_pending_jobs: dict[str, dict] = {}
_MAX_PENDING = 500
# code_interpreter, best effort across cells of one session: variable -> refs, local path -> ref.
_ci_session: str = ""
_ci_vars: dict[str, list[str]] = {}
_ci_local: dict[str, str] = {}
_MAX_CI_VARS = 400
# Locations code cells of THIS turn fetched or named: the fallback inputs of a cell that writes from local files.
_ci_fetched_turn: list[str] = []
# External URLs this turn's cells / http_request / shell fetched (credential-free): fallback external sources.
_ci_fetched_ext_turn: list[str] = []


def _edges_on() -> bool:
    try:
        return lc.edges_enabled()
    except Exception:  # noqa: BLE001 - a flag read never breaks bookkeeping
        return False


def _max_inputs() -> int:
    return lc.MAX_EDGE_INPUTS if _edges_on() else _MAX_INPUTS


def open_turn(now: Optional[float] = None) -> None:
    """Start a new turn: forget the previous turn's writes and lineage."""
    global _turn_started_at
    _turn_started_at = time.time() if now is None else float(now)
    _own.clear()
    _lineage.clear()
    _unclaimed_copies.clear()
    _pending_external.clear()
    _ci_fetched_turn.clear()
    _ci_fetched_ext_turn.clear()


def own_writes() -> set[str]:
    return set(_own)


def _artifacts_prefix() -> str:
    return (os.environ.get("ARTIFACTS_PREFIX") or "artifacts/").lstrip("/")


def _own_bucket() -> str:
    return artifact_fetch.artifacts_bucket()


def _split(uri: str) -> tuple[str, str]:
    bucket, _, key = uri[len("s3://"):].partition("/")
    return bucket, key


def _is_own_output_key(uri: str) -> bool:
    """A skills-bucket key under the artifacts prefix — or a projects-session OUTPUT key
    (asset-lineage Phase 3 (a)) — that can be a deliverable (not an input upload, not a render
    manifest)."""
    from projects.storage_layout import is_project_session_output_key

    bucket, key = _split(uri)
    if not bucket or bucket != _own_bucket():
        return False
    if is_project_session_output_key(key):
        return True
    if not key.startswith(_artifacts_prefix()):
        return False
    return "/input/" not in key and "/render/" not in key


def _is_own_ref(uri: str) -> bool:
    bucket, _ = _split(uri)
    return bool(bucket) and bucket == _own_bucket()


def canonical(ref: Any) -> Optional[str]:
    try:
        return artifact_fetch.canonical_location(ref)
    except Exception:  # noqa: BLE001 - an unparseable ref is simply not a location
        return None


@lru_cache(maxsize=8)
def _key_re(roots: tuple[str, ...]) -> "re.Pattern[str]":
    """Bare ``artifacts/...`` / ``projects/...`` keys not embedded in a longer path or URL."""
    return re.compile(r"(?<![\w/.:-])(?:" + "|".join(re.escape(r) for r in roots) + rf")[^{_STOP}]+")


def refs_in_text(text: str) -> list[str]:
    """Canonical locations named in ``text``, in first-seen order (URLs, s3 URIs, bare keys)."""
    if not isinstance(text, str) or not text:
        return []
    found: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        loc = canonical(raw.rstrip(_TRAILING_PUNCT))
        if loc and loc not in seen:
            seen.add(loc)
            found.append(loc)

    for m in _URL_RE.finditer(text):
        _add(m.group(0))
    roots = tuple(sorted({_artifacts_prefix(), "artifacts/", "projects/"}, key=len, reverse=True))
    for m in _key_re(roots).finditer(_URL_RE.sub(" ", text)):
        _add(m.group(0))
    return found


def _strings(node: Any, depth: int = 0) -> Iterable[str]:
    """Every string inside a tool input/result structure (bounded depth)."""
    if depth > 8:
        return
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _strings(v, depth + 1)
    elif isinstance(node, (list, tuple)):
        for v in node:
            yield from _strings(v, depth + 1)


def _refs_in(node: Any) -> list[str]:
    out: list[str] = []
    for s in _strings(node):
        for r in refs_in_text(s):
            if r not in out:
                out.append(r)
    return out


def _json_payloads(result: Any) -> list[dict]:
    """The JSON objects a tool returned as text blocks (``@tool`` functions return a JSON string)."""
    out: list[dict] = []
    for s in _strings(result):
        t = s.strip()
        if not t.startswith("{"):
            continue
        try:
            obj = json.loads(t)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def note_write(output: Any, inputs: Iterable[Any] = (), tool: str = "", *,
               unresolved: Iterable[Mapping] = (), origin: Optional[str] = None,
               ranges: Iterable[Mapping] = (), settings: Optional[Mapping] = None,
               via: str = "", job_id: str = "", inferred: bool = False, scratch: bool = False) -> Optional[str]:
    """Record that this runtime wrote ``output`` during the turn, made from ``inputs``.

    Returns the canonical output location, or None when ``output`` is not a location. The keyword-only
    fields (unresolved inputs, the mirrored-from origin, source time ranges, producer settings) are the
    lineage-edge extension and are recorded only with ``projects.lineageEdges`` on.
    """
    out = canonical(output)
    if not out:
        return None
    _own.add(out)
    ins: list[str] = []
    for i in inputs or ():
        loc = canonical(i)
        if loc and loc != out and loc not in ins:
            ins.append(loc)
    entry = _lineage.get(out)
    if entry is None:
        if len(_lineage) >= _MAX_LINEAGE:
            return out
        entry = {"tool": tool or "", "inputs": []}
        _lineage[out] = entry
    cap = _max_inputs()
    for loc in ins:
        if loc not in entry["inputs"] and len(entry["inputs"]) < cap:
            entry["inputs"].append(loc)
    if tool and not entry["tool"]:
        entry["tool"] = tool
    if _edges_on():
        _extend_entry(entry, out, unresolved=unresolved, origin=origin, ranges=ranges, settings=settings,
                      via=via, job_id=job_id, inferred=inferred)
        if scratch and not entry["inputs"] and not entry.get("unresolved"):
            entry["scratch"] = True
        elif entry.get("inputs") or entry.get("unresolved"):
            entry.pop("scratch", None)
    return out


def _extend_entry(entry: dict, out: str, *, unresolved: Iterable[Mapping], origin: Optional[str],
                  ranges: Iterable[Mapping], settings: Optional[Mapping], via: str, job_id: str,
                  inferred: bool) -> None:
    for u in unresolved or ():
        ref = lc.sanitize_ref(u.get("ref")) if isinstance(u, Mapping) else ""
        if not ref:
            continue
        item = {"ref": ref, "reason": str(u.get("reason") or "unresolved")[:64]}
        bucket = entry.setdefault("unresolved", [])
        if item not in bucket and len(bucket) < lc.MAX_EDGE_INPUTS:
            bucket.append(item)
    norm = lc.normalize_origin_url(origin) if origin else None
    if norm and not entry.get("origin"):
        entry["origin"] = norm
    for r in ranges or ():
        if not isinstance(r, Mapping):
            continue
        loc = canonical(r.get("input"))
        nr = lc.normalize_range(r.get("start"), r.get("end"), r.get("unit", "s"))
        if loc and nr and loc != out:
            item = {"input": loc, **nr}
            bucket = entry.setdefault("ranges", [])
            if item not in bucket and len(bucket) < lc.MAX_EDGE_INPUTS:
                bucket.append(item)
    if settings and not entry.get("settings"):
        clean = lc.sanitize_settings(settings)
        if clean:
            entry["settings"] = clean
    if via and not entry.get("via"):
        entry["via"] = via
    if job_id and not entry.get("jobId"):
        entry["jobId"] = str(job_id)[:512]
    if inferred:
        entry["inferred"] = True


def is_own_write(url: Any) -> bool:
    """True when ``url`` names an object this runtime wrote during the current turn."""
    loc = canonical(url)
    return bool(loc) and loc in _own


_EXTENDED_FIELDS = ("unresolved", "origin", "ranges", "settings", "via", "jobId", "inferred", "scratch")


def lineage() -> list[dict]:
    """The turn's lineage, ``[{output, inputs, tool}]`` in canonical ``s3://bucket/key`` form (plus the
    lineage-edge fields of :func:`note_write` when ``projects.lineageEdges`` is on)."""
    out_list = []
    for out, e in _lineage.items():
        item = {"output": out, "inputs": list(e["inputs"]), "tool": e["tool"]}
        for k in _EXTENDED_FIELDS:
            if e.get(k):
                item[k] = e[k]
        out_list.append(item)
    # projects.lineageEdges: a copy the mirror made that no tool call claimed (get_plan_results mirroring a plan
    # node's fal result) still names its origin, so the registrar can attach the lineage parked under that URL.
    for loc, norm in _unclaimed_copies.items():
        if loc not in _lineage and len(out_list) < _MAX_LINEAGE:
            out_list.append({"output": loc, "inputs": [], "tool": "", "origin": norm})
    return out_list


def _modified_this_turn(uri: str) -> bool:
    """One HEAD: was this object last written at/after the turn started? False on any failure."""
    if not _turn_started_at:
        return False
    bucket, key = _split(uri)
    try:
        head = artifact_fetch._client().head_object(Bucket=bucket, Key=key)
    except Exception:  # noqa: BLE001 - absent / unreadable: not provably ours
        return False
    lm = head.get("LastModified")
    try:
        ts = lm.timestamp() if hasattr(lm, "timestamp") else float(lm)
    except (TypeError, ValueError):
        return False
    return ts >= _turn_started_at - _CLOCK_SKEW_S


_SESSION_SEG_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _current_session() -> str:
    try:
        from jobs.context import current_session_id
        return (current_session_id() or "").strip()
    except Exception:  # noqa: BLE001 - unbound outside a request
        return ""


def _under_other_session(uri: str) -> bool:
    """True when the key sits under a session root — ``artifacts/<uuid>/`` or
    ``projects/<pid>/sessions/<uuid>/`` — for a uuid that is not this session."""
    from projects.storage_layout import parse_session_key

    _, key = _split(uri)
    sk = parse_session_key(key)
    if sk is not None and sk.layout == "project":
        seg = sk.sid
    elif key.startswith(_artifacts_prefix()):
        seg = key[len(_artifacts_prefix()):].split("/", 1)[0]
    else:
        return False
    return bool(_SESSION_SEG_RE.match(seg)) and seg.lower() != _current_session().lower()


def _code_interpreter(tool_input: Any, result: Any) -> None:
    edges = _edges_on()
    all_code_refs = _refs_in(tool_input)
    code_refs = [r for r in all_code_refs if _is_own_ref(r)]
    result_refs = [r for r in _refs_in(result) if _is_own_ref(r)]
    outputs: list[str] = []
    heads = 0

    def _fresh(r: str) -> bool:
        nonlocal heads
        if heads >= _MAX_HEADS_PER_CALL:
            return False
        heads += 1
        return _modified_this_turn(r)

    # A ref first appearing in the RESULT is an output -- verified by one HEAD when its path names
    # another session, so a listing that merely PRINTS someone else's files never passes as ours.
    for r in result_refs:
        if r in code_refs or r in _own or not _is_own_output_key(r):
            continue
        if not _under_other_session(r) or _fresh(r):
            outputs.append(r)
    # A key literally in the code is an input, unless the code wrote it this turn (a model that
    # types its output key -- invented uuid and all -- into put_object). Something an earlier tool
    # call wrote this turn is an input to this one.
    for r in code_refs:
        if r in outputs or r in _own or not _is_own_output_key(r):
            continue
        if _fresh(r):
            outputs.append(r)
    inputs = [r for r in code_refs if r not in outputs]
    if not edges:
        for out in outputs:
            note_write(out, inputs, tool="code_interpreter")
        return
    # lineage edges: inputs from OTHER buckets the code reads (the M2C proxy bucket's clips, an allowlisted
    # location), and refs that reach this cell through a variable or a local file an EARLIER cell defined.
    code = "\n".join(_strings(tool_input))
    carried = _ci_carried_refs(code)
    downloaded = _ci_track(code)
    # A content-addressed copy the MIRROR made this turn (artifacts/external/<hash>) is never a cell's output: the
    # cell only read it (staging 71f1073f: the animated clip get_plan_results mirrored was taken as the stitch
    # cell's output because its object was modified this turn).
    mirrored = set(_external_by_origin.values())
    demoted = [o for o in outputs if o in mirrored or _split(o)[1].startswith("artifacts/external/")]
    outputs = [o for o in outputs if o not in demoted]
    inputs.extend(o for o in demoted if o not in inputs)
    others = [r for r in all_code_refs if r not in code_refs]  # another bucket (the M2C proxy bucket, ...)
    for r in [*others, *downloaded, *carried]:
        if r not in outputs and r not in inputs and _is_input_location(r):
            inputs.append(r)
    # Media fetched from OUTSIDE (a public sample, a bucket we do not own) by this cell: external sources.
    external = _external_fetches(code)
    inferred = False
    if outputs and not inputs and not external:
        # The cell writes from LOCAL files whose paths could not be tied to a location; what earlier cells of this
        # turn fetched is the best evidence of what they were. Marked inferred, never presented as observed.
        inputs = [r for r in _ci_fetched_turn if r not in outputs]
        external = list(_ci_fetched_ext_turn)
        inferred = bool(inputs or external)
    for r in [*downloaded, *[x for x in all_code_refs if _is_input_location(x)]]:
        if r not in outputs and r not in _ci_fetched_turn and len(_ci_fetched_turn) < lc.MAX_EDGE_INPUTS:
            _ci_fetched_turn.append(r)
    _note_fetched_external(_external_fetches(code))
    # Created from scratch: the cell read NOTHING -- no location, no URL, and no media/data file name besides its
    # own outputs (a cell reading `screenshot1.png` from disk is a gap, not a root).
    scratch = bool(outputs) and not inputs and not external and not _names_other_media(code, outputs)
    result_text = "\n".join(_strings(result))
    for out in outputs:
        picked, ambiguous = pick_external_sources(external, out, result_text)
        unresolved = [{"ref": u, "reason": "external_url"} for u in picked]
        unresolved += [{"ref": u, "reason": lc.AMBIGUOUS_EXTERNAL} for u in ambiguous]
        note_write(out, inputs, tool="code_interpreter", ranges=_ffmpeg_ranges(code, out, inputs), inferred=inferred,
                   unresolved=unresolved, scratch=scratch)


def _url_basename(url: str) -> str:
    return url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1].lower()


def pick_external_sources(candidates: list[str], output: str, result_text: str) -> tuple[list[str], list[str]]:
    """``(sources, ambiguous)``: which of the external URLs a cell named actually produced ``output``.

    A download cell often lists FALLBACK URLs and keeps the first that works (staging session a4a205a9 named three
    sample sites and kept one); recording all of them would claim parents the file never came from. One candidate is
    the source. Several: the ones the cell's OUTPUT names (it printed the URL it fetched, or that URL's host when the
    host is unique among the candidates), else the one whose file name is the output's. Undecidable: no source,
    every candidate returned as ``ambiguous`` -- never a false parent."""
    if len(candidates) <= 1:
        return list(candidates), []
    text = result_text or ""
    named = [u for u in candidates if u in text]
    if 0 < len(named) < len(candidates):
        return named, []
    hosts = [lc.external_host(u) for u in candidates]
    by_host = [u for u, h in zip(candidates, hosts) if h and hosts.count(h) == 1 and h in text]
    if 0 < len(by_host) < len(candidates):
        return by_host, []
    name = _split(output)[1].rsplit("/", 1)[-1].lower() if output else ""
    by_name = [u for u in candidates if name and _url_basename(u) == name]
    if len(by_name) == 1:
        return by_name, []
    return [], list(candidates)


# A download from outside the Project, in a cell or a shell command.
_FETCH_HINT_RE = re.compile(r"(requests\.(get|request)|urlretrieve|urlopen|httpx\.|curl\s|wget\s|download|"
                            r"aiohttp|\.get\(\s*['\"]https?://)", re.IGNORECASE)
_MEDIA_NAME_RE = re.compile(
    r"['\"]([^'\"\s]+\.(?:mp4|mov|mxf|mkv|webm|m4v|avi|png|jpe?g|webp|gif|tiff?|wav|mp3|m4a|aac|flac|ogg|pdf|json|csv"
    r"|srt|vtt|txt))['\"]", re.IGNORECASE)


def _external_fetches(code: str) -> list[str]:
    """Credential-free URLs (and non-own S3 refs) of media this code DOWNLOADS from outside: an http(s) URL in a
    cell that fetches (requests / urllib / curl / wget / ...), excluding our own CDN and buckets."""
    if not code or not _FETCH_HINT_RE.search(code):
        return []
    out: list[str] = []
    own_cdn = artifact_fetch.artifacts_domain()
    for m in _HTTP_RE.finditer(code):
        url = m.group(0).rstrip(_TRAILING_PUNCT)
        if canonical(url):
            continue  # one of ours (CDN, a presigned GET of our bucket): an input, handled as a location
        if own_cdn and lc.external_host(url) == own_cdn:
            continue  # our CDN, under any path (`f"https://{CDN}/{key}"` leaves the bare root in the code)
        norm = lc.external_url(url)
        if norm and norm not in out and len(out) < 50:
            out.append(norm)
    return out


def _note_fetched_external(urls: Iterable[str]) -> None:
    for u in urls:
        if u not in _ci_fetched_ext_turn and len(_ci_fetched_ext_turn) < 50:
            _ci_fetched_ext_turn.append(u)


def _names_other_media(code: str, outputs: list[str]) -> bool:
    own = {_split(o)[1].rsplit("/", 1)[-1].lower() for o in outputs}
    for m in _MEDIA_NAME_RE.finditer(code or ""):
        name = m.group(1).rsplit("/", 1)[-1].lower()
        if name not in own:
            return True
    return False


def _is_input_location(uri: str) -> bool:
    """A canonical location that can be an asset: any other bucket, or an own-bucket session/project key (never
    the harness's own tools/, plans/, jobs/ objects)."""
    bucket, key = _split(uri)
    if not bucket or not key:
        return False
    if bucket != _own_bucket():
        return True
    return key.startswith((_artifacts_prefix(), "artifacts/", "projects/"))


_ASSIGN_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*(\+?=)\s*(?!=)(.+)$")
_APPEND_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*\.\s*(?:append|extend)\s*\((.+)\)\s*$")
# A call argument: a string literal, or a variable bound earlier (``still_url = "https://…"``).
_ARG = r"(?:['\"]([^'\"]+)['\"]|([A-Za-z_]\w*))"
_DOWNLOAD_RE = re.compile(
    rf"download_file\(\s*(?:Bucket\s*=\s*)?{_ARG}\s*,\s*(?:Key\s*=\s*)?{_ARG}\s*,\s*(?:Filename\s*=\s*)?{_ARG}")
_S3CP_RE = re.compile(r"aws\s+s3\s+cp\s+['\"]?(s3://[^\s'\"]+)['\"]?\s+['\"]?([^\s'\"]+)")
_RETRIEVE_RE = re.compile(rf"urlretrieve\(\s*{_ARG}\s*,\s*{_ARG}")
_STR_ASSIGN_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*=\s*['\"]([^'\"]*)['\"]\s*(?:#.*)?$")
# variable -> its plain string literal (a bucket name, a local path), for resolving call arguments.
_ci_strs: dict[str, str] = {}


def _arg(lit: Optional[str], var: Optional[str]) -> Optional[str]:
    """The string a call argument names: the literal, or the variable's string / single ref."""
    if lit is not None:
        return lit
    if var and var in _ci_strs:
        return _ci_strs[var]
    if var and len(_ci_vars.get(var) or []) == 1:
        return _ci_vars[var][0]
    return None


def _ci_reset_for_session() -> None:
    global _ci_session
    sid = _current_session()
    if sid != _ci_session:
        _ci_session = sid
        _ci_vars.clear()
        _ci_local.clear()
        _ci_strs.clear()


def _ci_carried_refs(code: str) -> list[str]:
    """Refs an earlier cell bound to a variable or a local file that THIS cell uses (and does not rebind)."""
    _ci_reset_for_session()
    rebound = set()
    for ln in code.splitlines():
        m = _ASSIGN_RE.match(ln)
        if m and m.group(2) == "=":
            rebound.add(m.group(1))
    out: list[str] = []
    for name, refs in _ci_vars.items():
        if name in rebound or not re.search(rf"(?<![\w.]){re.escape(name)}(?!\w)", code):
            continue
        out.extend(r for r in refs if r not in out)
    for path, ref in _ci_local.items():
        if path in code and ref not in out:
            out.append(ref)
    return out


def _ci_track(code: str) -> list[str]:
    """Remember this cell's variable bindings and downloads for later cells; returns the locations this cell
    downloads (``download_file(bucket, key, path)`` names its source as two strings, not one URI)."""
    downloaded: list[str] = []
    for ln in code.splitlines():
        s = _STR_ASSIGN_RE.match(ln)
        if s:
            _ci_strs[s.group(1)] = s.group(2)
        m = _ASSIGN_RE.match(ln)
        if m:
            name, op, rhs = m.group(1), m.group(2), m.group(3)
        else:
            m = _APPEND_RE.match(ln)
            if not m:
                continue
            name, op, rhs = m.group(1), "+=", m.group(2)
        refs = [r for r in refs_in_text(rhs) if _is_input_location(r)]
        if refs:
            cur = _ci_vars.get(name, []) if op == "+=" else []
            _ci_vars[name] = list(dict.fromkeys([*cur, *refs]))[: lc.MAX_EDGE_INPUTS]
    for m in _DOWNLOAD_RE.finditer(code):
        bucket, key, path = _arg(m.group(1), m.group(2)), _arg(m.group(3), m.group(4)), _arg(m.group(5), m.group(6))
        loc = canonical(f"s3://{bucket}/{key}") if bucket and key else None
        if loc and _is_input_location(loc):
            if path:
                _ci_local[path] = loc
            downloaded.append(loc)
    for m in _S3CP_RE.finditer(code):
        loc = canonical(m.group(1))
        if loc and not m.group(2).startswith("s3://"):
            _ci_local[m.group(2)] = loc
    for m in _RETRIEVE_RE.finditer(code):
        src, path = _arg(m.group(1), m.group(2)), _arg(m.group(3), m.group(4))
        loc = canonical(src) if src else None
        if loc and _is_input_location(loc):
            if path:
                _ci_local[path] = loc
            downloaded.append(loc)
    for bucket in (_ci_vars, _ci_local, _ci_strs):
        while len(bucket) > _MAX_CI_VARS:
            bucket.pop(next(iter(bucket)))
    return downloaded


_TIME = r"['\"]?([0-9]+(?::[0-9]{1,2}){0,2}(?:\.[0-9]+)?)['\"]?"
_SS_RE = re.compile(r"['\"]?-ss['\"]?\s*[,= ]\s*" + _TIME)
_TO_RE = re.compile(r"['\"]?-to['\"]?\s*[,= ]\s*" + _TIME)
_T_RE = re.compile(r"['\"]?-t['\"]?\s*[,= ]\s*" + _TIME)
_ONE_FRAME_RE = re.compile(r"-(?:frames:v|vframes)['\"]?\s*[,= ]\s*['\"]?1(?![0-9])")
_IN_RE = re.compile(r"['\"]?-i['\"]?\s*[,= ]\s*['\"]([^'\"]+)['\"]")


def _ffmpeg_ranges(code: str, out: str, inputs: list[str]) -> list[dict]:
    """The source time range of ``out`` when one ffmpeg command in ``code`` names it with literal ``-ss`` and
    ``-to``/``-t`` (or a single frame). Best effort: a range built from variables is not known."""
    name = _split(out)[1].rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0]
    if not name:
        return []
    for cmd in code.splitlines():
        if "ffmpeg" not in cmd or (name not in cmd and stem not in cmd):
            continue
        ss = _SS_RE.search(cmd)
        if not ss:
            continue
        end = None
        to = _TO_RE.search(cmd)
        dur = _T_RE.search(cmd)
        if to:
            end = to.group(1)
        elif dur:
            a, b = lc.normalize_range(ss.group(1)), lc.normalize_range(dur.group(1))
            end = (a["start"] + b["start"]) if a and b else None
        elif _ONE_FRAME_RE.search(cmd):
            end = ss.group(1)
        if end is None:
            continue
        src = None
        m = _IN_RE.search(cmd)
        if m:
            src = _ci_local.get(m.group(1)) or canonical(m.group(1))
        if not src and len(inputs) == 1:
            src = inputs[0]
        nr = lc.normalize_range(ss.group(1), end)
        if src and nr:
            return [{"input": src, **nr}]
    return []


def _keyed_result(result: Any, ok_status: tuple[str, ...]) -> Optional[dict]:
    for obj in _json_payloads(result):
        if obj.get("error"):
            continue
        if str(obj.get("status") or "") in ok_status and isinstance(obj.get("key"), str):
            return obj
    return None


# Gateway tools that only READ (search, graph, plan results): whatever URLs their results name are not outputs.
_READER_PREFIXES = ("project-search___", "project-graph___", "project_search", "project_graph", "web_search",
                    "web-search___", "aws-docs___", "aws_docs", "lab_asset", "get_plan_results", "read_result_files")
_REQUEST_ID_KEYS = ("request_id", "requestId", "job_id", "jobId", "execution_arn", "executionArn")
_HTTP_RE = re.compile(rf"https?://[^{_STOP}]+", re.IGNORECASE)


def _is_reader(name: str) -> bool:
    low = name.lower()
    if low.startswith(_READER_PREFIXES) or "___search" in low:
        return True
    try:
        import web_search as _ws
        return bool(_ws.is_source_tool(name))
    except Exception:  # noqa: BLE001 - optional module
        return False


# Media and data files a URL may name beyond what the renderer plays (broadcast formats, stills, documents).
_FILE_URL_RE = re.compile(r"\.(mxf|avi|mts|m2ts|ts|mpg|mpeg|wmv|flv|3gp|mkv|mov|mp4|m4v|webm|tiff?|bmp|heic|png|jpe?g|"
                          r"webp|gif|wav|aiff?|flac|ogg|opus|aac|mp3|m4a|pdf|json|csv|srt|vtt|xml)$", re.IGNORECASE)


def _media_url(url: str) -> bool:
    try:
        from media import persist_media
        if persist_media.media_type_for(url):
            return True
    except Exception:  # noqa: BLE001
        pass
    return bool(_FILE_URL_RE.search((url or "").split("?", 1)[0].split("#", 1)[0]))


def _input_refs(tool_input: Any) -> tuple[list[str], list[dict]]:
    """``(canonical inputs, unresolved)`` a tool's arguments name. A third-party media URL is resolved through the
    mirror's origin map (a fal output we copied); otherwise it is kept, query-less, as ``unresolved`` so the
    registrar can try the Project's ORIGIN guard and, failing that, log the dropped edge."""
    canon: list[str] = []
    unresolved: list[dict] = []
    for r in _refs_in(tool_input):
        if _is_input_location(r) and r not in canon:
            canon.append(r)
    for text in _strings(tool_input):
        for m in _HTTP_RE.finditer(text):
            url = m.group(0).rstrip(_TRAILING_PUNCT)
            if canonical(url):
                continue
            norm = lc.normalize_origin_url(url)
            if not norm:
                continue
            mapped = _external_by_origin.get(norm)
            if mapped:
                if mapped not in canon:
                    canon.append(mapped)
            elif _media_url(norm) and all(u["ref"] != norm for u in unresolved):
                unresolved.append({"ref": norm, "reason": "external_url"})
    return canon, unresolved


def _ids_in(node: Any, depth: int = 0) -> list[str]:
    out: list[str] = []
    if depth > 4:
        return out
    if isinstance(node, dict):
        for k, v in node.items():
            if k in _REQUEST_ID_KEYS and isinstance(v, str) and 6 <= len(v) <= 256:
                out.append(v)
            elif isinstance(v, (dict, list)):
                out.extend(_ids_in(v, depth + 1))
    elif isinstance(node, list):
        for v in node:
            out.extend(_ids_in(v, depth + 1))
    return out


def _result_ids(result: Any) -> list[str]:
    ids: list[str] = []
    for obj in _json_payloads(result):
        ids.extend(i for i in _ids_in(obj) if i not in ids)
    if isinstance(result, dict):
        ids.extend(i for i in _ids_in(result.get("structuredContent")) if i not in ids)
    return ids


def _remember(bucket: dict, key: str, value: Any, cap: int) -> None:
    bucket.pop(key, None)
    bucket[key] = value
    while len(bucket) > cap:
        bucket.pop(next(iter(bucket)))


def _gateway(name: str, tool_input: Any, result: Any) -> None:
    """A gateway (MCP) tool -- fal and every other generator: its outputs are the external media the mirror
    copied for THIS call (or a fresh own-bucket output its result names); its inputs, the media its arguments
    name. An async submission's inputs wait for the result call that names its request id."""
    if _is_reader(name):
        return
    inp = tool_input if isinstance(tool_input, dict) else {}
    canon, unresolved = _input_refs(inp)
    kw: dict[str, Any] = {"tool": name, "unresolved": unresolved, "settings": lc.sanitize_settings(inp)}
    rng = lc.ranges_from_args(inp)
    pending = next((_pending_jobs[rid] for rid in _ids_in(inp) if rid in _pending_jobs), None)
    if pending:
        canon = list(dict.fromkeys([*pending.get("inputs", []), *canon]))
        kw = {"tool": pending.get("tool") or name,
              "unresolved": [*pending.get("unresolved", []), *unresolved],
              "settings": pending.get("settings") or kw["settings"], "job_id": pending.get("job_id", "")}
        rng = rng or pending.get("range")
    ranges = [{"input": canon[0], **rng}] if rng and len(canon) == 1 else []
    # A generation that named no media at all (text-to-image) is a root; a result fetch (it names a request id) is
    # not -- its inputs were the submission's.
    kw["scratch"] = not canon and not kw["unresolved"] and not _ids_in(inp)
    outputs: list[str] = []
    origins: dict[str, str] = {}
    result_refs = _refs_in(result)
    for r in result_refs:
        if r in _unclaimed_copies and r not in canon:
            origins[r] = _unclaimed_copies.pop(r)
            outputs.append(r)
    heads = 0
    for r in result_refs:
        if r in outputs or r in canon or r in _own or not _is_own_output_key(r):
            continue
        if heads < _MAX_HEADS_PER_CALL:
            heads += 1
            if _modified_this_turn(r):
                outputs.append(r)
    for out in outputs:
        note_write(out, canon, origin=origins.get(out), ranges=ranges, **kw)
    # An external output nothing has mirrored yet: claimed when the mirror copies it (note_external_copy).
    external: list[str] = []
    for text in _strings(result):
        for m in _HTTP_RE.finditer(text):
            url = m.group(0).rstrip(_TRAILING_PUNCT)
            norm = lc.normalize_origin_url(url)
            if not norm or canonical(url) or not _media_url(norm) or norm in external:
                continue
            if any(u["ref"] == norm for u in kw["unresolved"]):
                continue  # an input echoed back
            external.append(norm)
    for norm in external:
        mapped = _external_by_origin.get(norm)
        if mapped:
            if mapped not in _lineage:
                note_write(mapped, canon, origin=norm, ranges=ranges, **kw)
        else:
            _remember(_pending_external, norm, {"inputs": canon, "ranges": ranges, **kw}, _MAX_PENDING)
    if not outputs and not external and (canon or kw["unresolved"]):
        for rid in _result_ids(result):
            _remember(_pending_jobs, rid, {"inputs": canon, "unresolved": kw["unresolved"], "tool": kw["tool"],
                                           "settings": kw["settings"], "range": rng, "job_id": rid}, _MAX_PENDING)


def note_external_copy(source_url: Any, key: Any) -> None:
    """The mirror copied third-party media ``source_url`` to our ``key`` (``persist_media``). Remembers the origin
    (a later input quoting the original URL resolves to our copy), and attributes the copy to the tool call that
    returned it. No-op with ``projects.lineageEdges`` off; never raises."""
    try:
        if not _edges_on():
            return
        loc = canonical(key)
        norm = lc.normalize_origin_url(source_url)
        if not loc or not norm:
            return
        _remember(_external_by_origin, norm, loc, _MAX_EXTERNAL_ORIGINS)
        pend = _pending_external.pop(norm, None)
        if pend is not None:
            pend = dict(pend)
            inputs = pend.pop("inputs", [])
            note_write(loc, inputs, origin=norm, **pend)
        elif loc not in _lineage:
            _remember(_unclaimed_copies, loc, norm, _MAX_PENDING)
    except Exception as exc:  # noqa: BLE001 - bookkeeping must never break a copy
        logger.debug("turn_outputs: external copy not recorded: %s", exc)


def _is_m2c_ref(uri: str) -> bool:
    bucket = _split(uri)[0] if isinstance(uri, str) and uri.startswith("s3://") else ""
    m2c = os.environ.get("MEDIA_PROXY_BUCKET", "")
    return bool(m2c) and bucket == m2c


def merge_lineage(entries: Any, via: str = "") -> int:
    """Fold a sub-agent's (or plan node's) ``lineage`` into this turn's ledger, so its outputs register with
    their inputs. Only entries whose output is one of OUR output locations are taken (a sub-agent's text cannot
    smuggle in an edge for an object that is not a session output). Returns how many were merged; never raises."""
    n = 0
    try:
        if not _edges_on():
            return 0
        for e in lc.entries(entries):
            out = canonical(e["output"])
            if not out or not (_is_own_output_key(out) or _is_m2c_ref(out)):
                continue
            if note_write(out, e["inputs"], e.get("tool") or "", unresolved=e.get("unresolved", ()),
                          origin=e.get("origin"), ranges=e.get("ranges", ()), settings=e.get("settings"),
                          via=e.get("via") or via, job_id=e.get("jobId", ""), inferred=bool(e.get("inferred"))):
                n += 1
    except Exception as exc:  # noqa: BLE001
        logger.debug("turn_outputs: sub-agent lineage not merged: %s", exc)
    return n


def note_tool_result(tool_name: str, tool_input: Any, result: Any) -> None:
    """Record the writes (and lineage) of one finished tool call. Never raises."""
    try:
        name = str(tool_name or "")
        if isinstance(result, dict) and str(result.get("status") or "") == "error":
            return
        inp = tool_input if isinstance(tool_input, dict) else {}
        edges = _edges_on()
        if name == "artifact":
            obj = _keyed_result(result, ("uploaded", "adopted", "imported"))
            if obj:
                src = inp.get("source_uri") if obj.get("status") == "imported" else None
                note_write(obj["key"], [src] if src else [], tool="artifact")
        elif name == "crop_video":
            obj = _keyed_result(result, ("cropped",))
            if obj and edges:
                src = canonical(inp.get("source_url"))
                trim = obj.get("trim") if isinstance(obj.get("trim"), list) else None
                ranges = ([{"input": src, "start": trim[0], "end": trim[1]}]
                          if src and trim and len(trim) == 2 else [])
                settings = {k: inp.get(k) for k in ("box", "aspect") if inp.get(k)}
                note_write(obj["key"], [inp.get("source_url")], tool="crop_video", ranges=ranges, settings=settings)
            elif obj:
                note_write(obj["key"], [inp.get("source_url")], tool="crop_video")
        elif name == "code_interpreter":
            _code_interpreter(tool_input, result)
        elif edges and "___" in name:
            _gateway(name, tool_input, result)
        elif edges and name == "http_request":
            # Fetched into memory: a later output this turn with no visible input falls back to it.
            url = lc.external_url(inp.get("url")) if not canonical(inp.get("url")) else None
            if url:
                _note_fetched_external([url])
        elif edges and name == "shell":
            _note_fetched_external(_external_fetches("\n".join(_strings(tool_input))))
    except Exception as exc:  # noqa: BLE001 - bookkeeping must never break a tool call
        logger.debug("turn_outputs: could not record %s: %s", tool_name, exc)
