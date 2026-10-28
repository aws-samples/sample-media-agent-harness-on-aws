"""Lineage edge contract — pure, shared by the agent runtime (producer) and the ingestion Lambdas (registrar,
Asset_Service). No boto3, no I/O.

Lineage lives in DynamoDB as the source of truth (decision 2026-10-22): one Project is one partition of the
projects table, and every "made from" relation is TWO items in it, one per direction::

    PK = PROJECT#<pid>   SK = UP#<child>#<parent>      "what was <child> made from"   (Query begins_with UP#<child>#)
    PK = PROJECT#<pid>   SK = DOWN#<parent>#<child>    "where was <parent> used"      (Query begins_with DOWN#<parent>#)

Why two items rather than a GSI keyed on the parent: both directions are then a strongly consistent Query of the
partition the registration already writes (a GSI is eventually consistent and a new table resource in every env),
the project purge's ``records`` step already deletes every item of the partition, and nothing new is synthesized.
An asset id never contains ``#`` (``<kind>:<uuid>``), so the SK splits unambiguously.

Each edge item carries (all optional except the ids)::

    child, parent            asset ids (the same ids the video/knowledge graph keys its nodes on)
    tool                     the producing tool (``fal___flux_kontext``, ``code_interpreter``, ``timeline_clip``, ...)
    jobId, turnId, sessionId what ran it
    via                      turn | task_agent | plan_node | job | timeline | backfill | manual
    ranges                   [{start, end, unit}] the PARENT's time range the child was made from (``s`` seconds or
                             ``frame``), e.g. a frame at 12.5 s, a clip of 3.0–7.5 s, an M2C shot
    settings                 allowlisted producer settings (model, prompt, seed, ...), never a URL or a secret, ≤1 KB
    inferred                 true when the edge was inferred from a plan's depends_on rather than observed
    createdAt

A later read-only projection (the video/knowledge graph) can consume the edges as-is: the node key is the asset id
and the time range is on the edge, so a ``DERIVED_FROM`` relationship with ``(start, end)`` properties maps 1:1.

The producer → registrar wire format (one ``lineage`` entry of a turn message / task-agent summary / plan node)::

    {output, inputs: [canonical s3 uri], tool?, unresolved?: [{ref, reason}], origin?, ranges?: [{input, start, end,
     unit}], settings?, jobId?, via?}
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional

__all__ = [
    "EDGE_UP_PREFIX",
    "EDGE_DOWN_PREFIX",
    "ORIGIN_SK_PREFIX",
    "GAP_SK_PREFIX",
    "STATS_SK",
    "MAX_EDGE_INPUTS",
    "MAX_ROW_DERIVED_FROM",
    "MAX_SETTINGS_BYTES",
    "VIA",
    "edges_enabled",
    "edge_up_sk",
    "edge_down_sk",
    "parse_edge_sk",
    "origin_sk",
    "gap_sk",
    "normalize_origin_url",
    "sanitize_ref",
    "sanitize_settings",
    "normalize_range",
    "range_key",
    "ranges_from_args",
    "parse_entry",
]

EDGE_UP_PREFIX = "UP#"
EDGE_DOWN_PREFIX = "DOWN#"
# An external URL (fal.media, ...) that one of this Project's assets was mirrored from: lets a later tool input that
# quotes the ORIGINAL url (not our copy) resolve to the asset.
ORIGIN_SK_PREFIX = "ORIGIN#"
# An input a producer named that could not be resolved to an asset (a dropped edge), kept for the read API's `gaps`.
GAP_SK_PREFIX = "LINEAGEGAP#"
STATS_SK = "LINEAGE#STATS"

# The 20-input cap of design §2 is lifted for EDGES (a 30-clip assembly keeps all 30). Bounded anyway, so one
# pathological entry cannot write thousands of items. The ASSET row's own `derivedFrom` (kept for compatibility)
# keeps a larger but still bounded list.
MAX_EDGE_INPUTS = 500
MAX_ROW_DERIVED_FROM = 200
MAX_SETTINGS_BYTES = 1024
MAX_RANGES_PER_EDGE = 20
MAX_REF_CHARS = 512

VIA = frozenset({"turn", "task_agent", "plan_node", "job", "timeline", "backfill", "manual"})

_ASSET_ID_RE = re.compile(r"^[a-z]+:[0-9a-fA-F-]{36}$")


def edges_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """The ``projects.lineageEdges`` env-config flag, as it reaches each consumer.

    The runtimes, the proxy, the Asset_Service and GeneratedMediaFn read it from ``INGESTION_CONFIG.lineageEdges``
    (the JSON that already reaches all of them, and carries the key only where the flag is on, so an off env's
    config is byte-identical); the plan chainer, which has no INGESTION_CONFIG, from ``LINEAGE_EDGES=true``.
    """
    e = os.environ if env is None else env
    if str(e.get("LINEAGE_EDGES") or "").strip().lower() == "true":
        return True
    raw = e.get("INGESTION_CONFIG") or ""
    if not raw or "lineageEdges" not in raw:
        return False
    try:
        cfg = json.loads(raw)
    except (TypeError, ValueError):
        return False
    return isinstance(cfg, dict) and cfg.get("lineageEdges") is True


def _is_asset_id(v: Any) -> bool:
    return isinstance(v, str) and bool(_ASSET_ID_RE.match(v)) and "#" not in v


# ------------------------------------------------------------------ external sources
# Media that entered the Project from OUTSIDE (a download from the public internet, a bucket we do not own) is a
# lineage ROOT: an `ext:<32 hex>` node (EXTSRC#<hash> item: the credential-free URL, host, first seen, tool, chat
# turn, license "unknown") with an ordinary edge to the asset made from it. The id is derived from the sanitized
# URL, so the same source is one node however many files came from it.
EXTERNAL_PREFIX = "ext:"
EXTSRC_SK_PREFIX = "EXTSRC#"
ROOT_SK_PREFIX = "ROOT#"
# `ALIAS#<sha256(bucket/key)>`: a location that holds a COPY of an asset's bytes (the durable copy the job settle makes
# of an M2C output under the session's output prefix), so an input naming the copy links to the asset itself.
ALIAS_SK_PREFIX = "ALIAS#"
# An `unresolved` reason: one of several URLs a download step named, but which one produced the output could not be
# decided. Never an edge; the output is a ROOT of kind `external_ambiguous` that keeps the candidates.
AMBIGUOUS_EXTERNAL = "external_ambiguous"


def alias_sk(bucket: str, key: str) -> str:
    return ALIAS_SK_PREFIX + hashlib.sha256(f"{bucket}/{key}".encode("utf-8")).hexdigest()[:40]
_EXT_ID_RE = re.compile(r"^ext:[0-9a-f]{32}$")


def is_external_id(v: Any) -> bool:
    return isinstance(v, str) and bool(_EXT_ID_RE.match(v))


def is_node_id(v: Any) -> bool:
    """An edge end: an asset id, or an external source id."""
    return _is_asset_id(v) or is_external_id(v)


def external_url(ref: Any) -> Optional[str]:
    """The stored form of an external source: scheme + lower-cased host + path for http(s) (EVERY query string and
    fragment dropped -- a presigned or tokened URL never reaches a record), ``s3://bucket/key`` for S3."""
    if not isinstance(ref, str):
        return None
    s = ref.strip()
    if s.lower().startswith("s3://"):
        bucket, _, key = s[5:].split("?", 1)[0].partition("/")
        return f"s3://{bucket.lower()}/{key}" if bucket and key else None
    norm = normalize_origin_url(s)
    # A site root (`https://host/`) names no file: code that builds `f"https://{CDN}/{key}"` mentions one in passing.
    return norm if norm and urlsplit_path(norm) not in ("", "/") else None


def urlsplit_path(url: str) -> str:
    m = re.match(r"^[a-z0-9]+://[^/]+(/[^?#]*)?", url or "", re.I)
    return (m.group(1) or "") if m else ""


def external_id(ref: Any) -> Optional[str]:
    url = external_url(ref)
    return EXTERNAL_PREFIX + hashlib.sha256(url.encode("utf-8")).hexdigest()[:32] if url else None


def external_host(url: str) -> str:
    m = re.match(r"^[a-z0-9]+://([^/]+)", url or "", re.I)
    return m.group(1).lower() if m else ""


def edge_up_sk(child: str, parent: str) -> str:
    return f"{EDGE_UP_PREFIX}{child}#{parent}"


def edge_down_sk(parent: str, child: str) -> str:
    return f"{EDGE_DOWN_PREFIX}{parent}#{child}"


def parse_edge_sk(sk: Any) -> Optional[tuple[str, str, str]]:
    """``(direction, a, b)`` of an edge SK: ``("up", child, parent)`` or ``("down", parent, child)``."""
    if not isinstance(sk, str):
        return None
    for prefix, direction in ((EDGE_UP_PREFIX, "up"), (EDGE_DOWN_PREFIX, "down")):
        if sk.startswith(prefix):
            a, sep, b = sk[len(prefix):].partition("#")
            if sep and is_node_id(a) and is_node_id(b):
                return direction, a, b
    return None


def normalize_origin_url(url: Any) -> Optional[str]:
    """An external URL without its query/fragment, lower-cased host; ``None`` when not an http(s) URL."""
    if not isinstance(url, str):
        return None
    s = url.strip()
    m = re.match(r"^(https?)://([^/?#]+)([^?#]*)", s, re.IGNORECASE)
    if not m:
        return None
    path = m.group(3) or "/"
    return f"{m.group(1).lower()}://{m.group(2).lower()}{path}"


def origin_sk(url: Any) -> Optional[str]:
    norm = normalize_origin_url(url)
    if not norm:
        return None
    return ORIGIN_SK_PREFIX + hashlib.sha256(norm.encode("utf-8")).hexdigest()


def gap_sk(child: str, ref: str, reason: str) -> str:
    h = hashlib.sha256(f"{ref}\n{reason}".encode("utf-8")).hexdigest()[:20]
    return f"{GAP_SK_PREFIX}{child}#{h}"


def sanitize_ref(ref: Any) -> str:
    """A reference fit to store or log: no query string (presigned signatures, tokens), bounded."""
    if not isinstance(ref, str):
        return ""
    s = ref.strip().split("?", 1)[0].split("#", 1)[0]
    return s[:MAX_REF_CHARS]


# ------------------------------------------------------------------ settings
# Producer settings worth keeping on an edge: what model, what prompt, what knobs. An allowlist, so a tool that
# takes an api key, a callback URL or a signed upload URL never has it recorded.
_SETTING_KEYS = (
    "model", "model_id", "modelId", "endpoint", "endpoint_id", "app", "workflow", "op", "operation", "mode",
    "prompt", "negative_prompt", "style", "preset", "seed", "strength", "guidance_scale", "cfg_scale",
    "num_inference_steps", "steps", "aspect_ratio", "resolution", "width", "height", "size", "image_size",
    "duration", "fps", "frame_rate", "codec", "crf", "bitrate", "quality", "format", "output_format", "scale",
    "speed", "loop", "voice", "language", "box", "crop", "aspect", "num_images", "motion", "camera",
    "transition", "shot_index", "shot", "segment", "start", "end", "start_time", "end_time", "timestamp",
)
_SETTING_KEY_SET = frozenset(_SETTING_KEYS)
_SECRETISH = re.compile(r"(key|token|secret|password|passwd|auth|credential|signature|cookie|session)", re.I)
_URLISH = re.compile(r"(?:[a-z][a-z0-9+.-]*://|x-amz-|amzn|AKIA[0-9A-Z]{8})", re.I)
_MAX_SETTING_STR = 300


def _setting_value(v: Any) -> Any:
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return v if (isinstance(v, int) or math.isfinite(v)) else None
    if isinstance(v, str):
        s = v.strip()
        if not s or _URLISH.search(s):
            return None
        return s[:_MAX_SETTING_STR]
    if isinstance(v, (list, tuple)) and len(v) <= 8 and all(
            isinstance(x, (int, float)) and not isinstance(x, bool) for x in v):
        return [x for x in v if isinstance(x, int) or math.isfinite(x)]
    return None


def sanitize_settings(raw: Any) -> dict:
    """The allowlisted, URL-free, secret-free subset of a tool's arguments, at most :data:`MAX_SETTINGS_BYTES`.

    Nested ``arguments``/``input``/``params``/``config`` dicts (a gateway call's real args) are looked into one
    level. Over budget, the longest values (the prompt, first) are cut, then keys are dropped from the end.
    """
    if not isinstance(raw, Mapping):
        return {}
    flat: dict[str, Any] = {}
    sources: list[Mapping] = [raw]
    for k in ("arguments", "args", "input", "params", "parameters", "config", "options"):
        v = raw.get(k)
        if isinstance(v, Mapping):
            sources.append(v)
    for src in sources:
        for k, v in src.items():
            if not isinstance(k, str) or k not in _SETTING_KEY_SET or _SECRETISH.search(k) or k in flat:
                continue
            val = _setting_value(v)
            if val is not None:
                flat[k] = val
    ordered = {k: flat[k] for k in _SETTING_KEYS if k in flat}

    def size(d: dict) -> int:
        return len(json.dumps(d, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))

    while ordered and size(ordered) > MAX_SETTINGS_BYTES:
        longest = max(ordered, key=lambda k: len(str(ordered[k])))
        v = ordered[longest]
        if isinstance(v, str) and len(v) > 40:
            ordered[longest] = v[: max(40, len(v) // 2)]
        else:
            ordered.pop(next(reversed(ordered)))
    return ordered


# ------------------------------------------------------------------ time ranges
def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float, Decimal)):  # Decimal: a range read back from DynamoDB
        f = float(v)
    elif isinstance(v, str):
        s = v.strip().lower().rstrip("s")
        m = re.fullmatch(r"(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)", s)  # [hh:]mm:ss(.ms)
        if m:
            f = int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        else:
            try:
                f = float(s)
            except ValueError:
                return None
    else:
        return None
    return f if math.isfinite(f) and f >= 0 else None


def normalize_range(start: Any, end: Any = None, unit: Any = "s") -> Optional[dict]:
    """``{start, end, unit}`` (``end`` defaults to ``start``: a single frame/instant), or ``None``."""
    u = unit if unit in ("s", "frame") else "s"
    a = _num(start)
    b = _num(end) if end is not None else a
    if a is None or b is None:
        return None
    if b < a:
        a, b = b, a
    if u == "frame":
        return {"start": int(a), "end": int(b), "unit": "frame"}
    return {"start": round(a, 3), "end": round(b, 3), "unit": "s"}


def range_key(r: Mapping[str, Any]) -> str:
    return f"{r.get('unit', 's')}:{r.get('start')}-{r.get('end')}"


_START_KEYS = ("start", "start_time", "startTime", "start_s", "start_sec", "start_seconds", "in", "in_s",
               "from", "ss", "timestamp", "time", "at", "t")
_END_KEYS = ("end", "end_time", "endTime", "end_s", "end_sec", "end_seconds", "out", "out_s", "to")
_DURATION_KEYS = ("clip_duration", "segment_duration", "duration", "length")
_FRAME_KEYS = ("frame", "frame_index", "frame_number", "frameIndex")


def ranges_from_args(args: Any) -> Optional[dict]:
    """A source time range named by a tool's arguments (``start``/``end``, ``start_time`` + ``duration``,
    ``timestamp``, ``frame_index``), looked for at the top level and one level into ``arguments``/``config``."""
    if not isinstance(args, Mapping):
        return None
    sources: list[Mapping] = [args]
    for k in ("arguments", "args", "input", "params", "parameters", "config", "options", "range", "segment"):
        v = args.get(k)
        if isinstance(v, Mapping):
            sources.append(v)
    for src in sources:
        for fk in _FRAME_KEYS:
            if _num(src.get(fk)) is not None:
                return normalize_range(src.get(fk), None, "frame")
        start = next((src.get(k) for k in _START_KEYS if _num(src.get(k)) is not None), None)
        if start is None:
            continue
        end = next((src.get(k) for k in _END_KEYS if _num(src.get(k)) is not None), None)
        if end is None:
            dur = next((_num(src.get(k)) for k in _DURATION_KEYS if _num(src.get(k)) is not None), None)
            if dur is not None and _num(start) is not None:
                end = _num(start) + dur
        return normalize_range(start, end)
    return None


# ------------------------------------------------------------------ wire entries
def _str(v: Any, n: int = 2048) -> Optional[str]:
    return v[:n] if isinstance(v, str) and v else None


def parse_entry(raw: Any) -> Optional[dict]:
    """One well-formed lineage entry (see the module docstring) or ``None``. Never raises; bounds everything."""
    if not isinstance(raw, Mapping) or not isinstance(raw.get("output"), str) or not raw["output"]:
        return None
    inputs = [i for i in dict.fromkeys(raw.get("inputs") or []) if isinstance(i, str) and i and len(i) <= 2048]
    out: dict[str, Any] = {"output": raw["output"][:2048], "inputs": inputs[:MAX_EDGE_INPUTS]}
    tool = _str(raw.get("tool"), 96)
    if tool:
        out["tool"] = tool
    unresolved = []
    for u in raw.get("unresolved") or []:
        if isinstance(u, Mapping) and isinstance(u.get("ref"), str) and u["ref"]:
            unresolved.append({"ref": sanitize_ref(u["ref"]), "reason": _str(u.get("reason"), 64) or "unresolved"})
    if unresolved:
        out["unresolved"] = unresolved[:MAX_EDGE_INPUTS]
    origin = normalize_origin_url(raw.get("origin"))
    if origin:
        out["origin"] = origin[:MAX_REF_CHARS]
    ranges = []
    for r in raw.get("ranges") or []:
        if not isinstance(r, Mapping) or not isinstance(r.get("input"), str):
            continue
        nr = normalize_range(r.get("start"), r.get("end"), r.get("unit", "s"))
        if nr:
            ranges.append({"input": r["input"][:2048], **nr})
    if ranges:
        out["ranges"] = ranges[:MAX_EDGE_INPUTS]
    settings = sanitize_settings(raw.get("settings"))
    if settings:
        out["settings"] = settings
    job = _str(raw.get("jobId"), 512)
    if job:
        out["jobId"] = job
    via = raw.get("via")
    if isinstance(via, str) and via in VIA:
        out["via"] = via
    if raw.get("inferred") is True:
        out["inferred"] = True
    if raw.get("scratch") is True and not out["inputs"] and not out.get("unresolved"):
        out["scratch"] = True  # the producing step demonstrably read no media: a root, not a gap
    return out


def entries(raw: Any, limit: int = 200) -> list[dict]:
    out: list[dict] = []
    for e in (raw if isinstance(raw, list) else [])[:limit]:
        p = parse_entry(e)
        if p:
            out.append(p)
    return out


def merge_entries(a: Iterable[Mapping], b: Iterable[Mapping]) -> list[dict]:
    """Union of two entry lists by output (inputs, unresolved and ranges unioned; first tool/settings win)."""
    by_out: dict[str, dict] = {}
    for e in [*a, *b]:
        p = parse_entry(e)
        if not p:
            continue
        cur = by_out.get(p["output"])
        if cur is None:
            by_out[p["output"]] = p
            continue
        cur["inputs"] = list(dict.fromkeys([*cur["inputs"], *p["inputs"]]))[:MAX_EDGE_INPUTS]
        for k in ("unresolved", "ranges"):
            if p.get(k):
                seen = {json.dumps(x, sort_keys=True) for x in cur.get(k, [])}
                cur[k] = [*cur.get(k, []), *[x for x in p[k] if json.dumps(x, sort_keys=True) not in seen]]
        for k in ("tool", "settings", "origin", "jobId", "via"):
            if k not in cur and p.get(k):
                cur[k] = p[k]
    return list(by_out.values())
