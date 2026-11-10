"""Strands ``HookProvider`` that bridges native lifecycle events to the
declarative hook engine.

We register ONE ``HarnessHookProvider`` on each ``Agent(hooks=[...])`` (main,
task, render). It subscribes to the Strands events, normalizes each into our
event-context dict, and calls ``engine.fire(event_name, ctx)``.

Declarative event names ↔ Strands events:
  * ``after_tool_result`` ← ``AfterToolCallEvent`` (carries tool_use + result;
    fires on the agent that ran the tool — e.g. the task agent for gateway
    results tools, where structured URLs live).
  * ``before_tool``        ← ``BeforeToolCallEvent``
  * ``session_start``      ← ``BeforeInvocationEvent``
  * ``session_end``        ← ``AfterInvocationEvent``

The normalizers below read only plain attributes/keys, so they also work with
lightweight stubs in unit tests.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import json
from typing import Any, Callable, Optional

from .engine import HookEngine, HookSpec

logger = logging.getLogger(__name__)

__all__ = [
    "HarnessHookProvider",
    "role_for_tool",
    "extract_urls",
    "extract_output_uris",
    "extract_source_uris",
    "ctx_after_tool_result",
]

# A URL ends at whitespace, a quote, a closing bracket, or a BACKSLASH. The backslash matters for
# code_interpreter output, which arrives JSON/repr-escaped: session 865950fd harvested
# `<cdn>/.../compliance_report.json\nhttps://<cdn>/.../compliance_report.pdf` (a LITERAL `\n`)
# as ONE URL and opened a document tab on it ("artifact key is not readable by this caller").
_URL_RE = re.compile(r"https?://[^\s\"'<>)\]}\\]+")

import render.types as render_types

# Type data lives in the single render-type registry (render_types); provider
# only keeps the filtering logic (operational/source/dedup) on top of it.
_MIME_BY_EXT = render_types.MIME_BY_EXT


def role_for_tool(name: str) -> str:
    """Classify a tool name into a job-trio role (mirrors jobs.detector naming).

    Gateway-prefixed names (``target___start_widget``) are split on ``___``.
    """
    if not name:
        return "other"
    bare = name.split("___")[-1]
    if bare.startswith("start_"):
        return "start"
    if bare.endswith("_status"):
        return "status"
    if bare.endswith("_results"):
        return "results"
    return "other"


def _walk_urls(obj: Any, out: list[str]) -> None:
    if isinstance(obj, str):
        out.extend(_URL_RE.findall(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _walk_urls(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _walk_urls(v, out)


# Keys whose URL value is a downscaled PREVIEW/THUMBNAIL of an original that is already present
# elsewhere in the result (fal image results pair `previews[].preview_url` — a .webp thumbnail —
# with the original in `previews[].original_url` / `media[].url` / `result.images[].url`).
# Harvesting the preview as well rendered each generated image TWICE (session 9aabc7ce: a
# generated .jpg/.png plus its .webp preview as two gallery tiles). We collect these separately
# and subtract them so only the originals render.
_PREVIEW_URL_KEYS = frozenset({"preview_url", "thumbnail_url", "thumb_url", "thumbnail"})


def _collect_keyed_urls(obj: Any, keys: frozenset, out: set[str]) -> None:
    """Collect URL values sitting directly under any key in ``keys`` (recursively)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in keys and isinstance(v, str):
                out.update(_URL_RE.findall(v))
            else:
                _collect_keyed_urls(v, keys, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _collect_keyed_urls(v, keys, out)


def extract_urls(result: Any) -> list[str]:
    """Pull http(s) URLs out of a Strands ``ToolResult`` (text + json blocks).

    Preview/thumbnail URLs are excluded: they are downscaled copies of an original that is also
    present, so including them duplicates each image in the render gallery.
    """
    content = result.get("content") if isinstance(result, dict) else None
    urls: list[str] = []
    preview_urls: set[str] = set()
    for block in content or []:
        if not isinstance(block, dict):
            continue
        if isinstance(block.get("text"), str):
            text = block["text"]
            _walk_urls(text, urls)
            stripped = text.strip()
            if stripped[:1] in ("{", "["):  # a JSON tool result carried as text
                try:
                    _collect_keyed_urls(json.loads(stripped), _PREVIEW_URL_KEYS, preview_urls)
                except Exception:  # noqa: BLE001 - not JSON / unparssable: nothing to subtract
                    pass
        if "json" in block:
            _walk_urls(block["json"], urls)
            _collect_keyed_urls(block["json"], _PREVIEW_URL_KEYS, preview_urls)
    seen: set[str] = set()
    deduped: list[str] = []
    for u in urls:
        if u in preview_urls or u in seen:  # drop preview thumbnails + exact dups
            continue
        if "\\" in u:  # an escaped-text artifact, never a real URL (see _URL_RE)
            continue
        seen.add(u)
        deduped.append(u)
    return deduped


def _mime_by_url(urls: list[str]) -> dict[str, str]:
    """Map each URL to its mime, keeping only those with a known artifact type.

    Per-URL rather than the aggregate `_mimes_for` list, because deciding which
    individual URL is new requires knowing which individual URL is renderable.
    """
    out: dict[str, str] = {}
    for url in urls:
        base = url.split("?", 1)[0].lower()
        for ext, mime in _MIME_BY_EXT.items():
            if base.endswith(ext):
                out[url] = mime
                break
    return out


def _mimes_for(urls: list[str]) -> list[str]:
    out: list[str] = []
    for u in urls:
        base = u.lower().split("?")[0]
        for ext, mime in _MIME_BY_EXT.items():
            if base.endswith(ext):
                out.append(mime)
                break
    return out


_RENDERABLE_EXTS = render_types.RENDERABLE_EXTS

# Names that describe an OPERATIONAL artifact rather than a deliverable.
#
# ONE definition, mirrored from the polling worker's
# ``artifact_capability._OPERATIONAL_HINTS``. Both modules classify the SAME ``outputs[]``
# envelope (emitted by M2C, so neither of us can add a ``render: false`` at the producer), and
# ``test_operational_hints_match_poller`` fails the build if the two drift.
#
# That drift was live, and it is what this constant exists to stop: the poller's tuple carried
# "probe" and this module's did not, so a media-processing job's probe.json -- the source SPEC,
# never a deliverable -- passed the explicit path and opened a second Renderer tab beside the
# real output. Four other places in the repo already stated the rule (the poller, the
# result-resolution layer's _FINDINGS_ONLY_BASENAMES, render-media/SKILL.md); the one reader
# that lacked it is the one that decided.
_OPERATIONAL_HINTS = (
    "log", "usage", "tracking", "config", "manifest", "probe", "ephemeral",
)


def _is_operational_name(value: str) -> bool:
    """True when `value` names an operational artifact rather than a deliverable.

    Whole-TOKEN matching, not substring: ``facemetadata.json`` and ``final_transcript.json`` are
    real deliverables (both are on the result-resolution layer's canonical list), so a substring
    rule that ate "metadata"/"transcript" would drop the answer along with the noise.
    """
    tokens = set(re.findall(r"[a-z0-9]+", value.lower()))
    return any(hint in tokens for hint in _OPERATIONAL_HINTS)


# Extra conservatism for the HEURISTIC layers only (2 and 3), which reconstruct urls from field
# names rather than reading a declaration. Deliberately NOT folded into _OPERATIONAL_HINTS: this
# answers "we inferred this url, so be cautious", which is a different question from "the
# producer says this file is operational" -- and merging them would either loosen the explicit
# path or drop real deliverables from it.
_JSON_EXCLUDE_PATTERNS = ("log", "usage", "tracking", "config", "manifest", "metadata", "transcript")

# Exact JSON basenames that are a spec/ANSWER, never a renderable deliverable — mirrors the
# poller's artifact_capability._NON_RENDERABLE_JSON_BASENAMES. A metadata PROBE emits metadata.json
# (+ probe.json) as the answer, shown in the chat turn; it must NOT render as a separate video+card
# tab even though it arrives as a DECLARED output (Layer 1 trusts declared outputs / forces .json
# renderable). Exact-match so real deliverables like analysis_metadata.json still render.
_NON_RENDERABLE_JSON_BASENAMES = ("metadata.json", "probe.json")
_PROBE_ANSWER_SUFFIXES = ("_metadata.json", "-metadata.json", "_probe.json", "-probe.json")
_AGENT_OUTPUT_RE = re.compile(r"/(?:artifacts|projects/p[0-9a-f]{32}/sessions)/[^/]+/output/")


def _is_probe_answer_json(url: str) -> bool:
    """A technical-metadata / probe answer the AGENT saved (`artifacts/<session>/output/`), by name:
    `metadata.json`, `probe.json`, `<asset>_metadata.json` or `<asset>_probe.json`. Workflow outputs
    (M2C, `analysis_metadata.json`, ...) live elsewhere and keep their rendering."""
    path = (url or "").lower().split("?", 1)[0]
    if not _AGENT_OUTPUT_RE.search(path):
        return False
    base = path.rsplit("/", 1)[-1]
    return base in _NON_RENDERABLE_JSON_BASENAMES or base.endswith(_PROBE_ANSWER_SUFFIXES)


def _is_explicit_operational(value: str) -> bool:
    """Operational check for a DECLARED output (``outputs[]`` label or basename)."""
    return _is_operational_name(value)


def _is_renderable(path: str) -> bool:
    clean = path.lower().split("?")[0]
    if not any(clean.endswith(ext) for ext in _RENDERABLE_EXTS):
        return False
    basename = clean.rsplit("/", 1)[-1]
    if _is_operational_name(basename):
        return False
    # An intermediate under an operational DIRECTORY (e.g. .../ephemeral/foo.json) is
    # not a deliverable even when its basename looks clean. face-recognition writes
    # ephemeral/identity_clusters.json + ephemeral/face_tracks.json (36 raw tracks);
    # rendering those produced 36 unnamed "Person N" clusters instead of the 5 grouped,
    # named people in facegroup.json. The basename check missed the dir segment.
    dirs = clean.rsplit("/", 1)[0].split("/") if "/" in clean else []
    if any(seg in _OPERATIONAL_HINTS for seg in dirs):
        return False
    if clean.endswith(".json"):
        if any(pat in basename for pat in _JSON_EXCLUDE_PATTERNS):
            return False
    return True

_S3_URI_RE = re.compile(r"s3://[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]/[^\s\\]+")
_BUCKET_KEY_HINTS = ("bucket", "Bucket")


def _walk_dicts(obj: Any):
    """Yield every dict nested anywhere within ``obj``."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk_dicts(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk_dicts(v)


def _content_dicts(result: Any) -> list[dict]:
    """Extract all nested dicts from a Strands ToolResult's content blocks."""
    content = result.get("content") if isinstance(result, dict) else None
    dicts: list[dict] = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        if isinstance(block.get("json"), dict):
            dicts.extend(_walk_dicts(block["json"]))
        text = block.get("text")
        if isinstance(text, str):
            try:
                dicts.extend(_walk_dicts(json.loads(text)))
            except (ValueError, TypeError):
                pass
    return dicts


def extract_output_uris(result: Any) -> list[str]:
    """Extract renderable ``s3://`` URIs from any tool result — tool-agnostic.

    Three layers, tried in order:

    1. **Convention envelope** — ``outputs: [{uri, ...}]``. If the result
       contains an ``outputs`` list whose items have ``uri`` fields, trust
       those directly. This is the canonical contract any new integration
       should follow.

    2. **Explicit s3:// URIs** — scan all string values in the result for
       ``s3://bucket/key`` patterns. Catches tools that embed full URIs
       anywhere in their response.

    3. **Bucket + key reconstruction** — find any key whose name contains
       "bucket" (value = bucket name) paired with string values elsewhere in
       the same dict that end in a renderable extension. Handles legacy M2C
       shapes (``output_s3_prefix`` + ``*_key`` fields, ``output_bucket`` +
       ``output_keys`` dict) and any future tool that follows a similar
       bucket-plus-relative-keys pattern.

    All layers filter to renderable extensions and deduplicate.
    """
    dicts = _content_dicts(result)
    out: list[str] = []
    seen: set[str] = set()

    def _add(uri: str, *, allow_excluded_json: bool = False) -> None:
        if uri in seen:
            return
        renderable = _is_renderable(uri)
        if allow_excluded_json:
            clean = uri.lower().split("?", 1)[0]
            if clean.endswith(".json"):
                renderable = True
        if renderable:
            seen.add(uri)
            out.append(uri)

    def _add_prefix(uri: str) -> None:
        if uri not in seen and uri.startswith("s3://") and uri.endswith("/"):
            seen.add(uri)
            out.append(uri)

    # --- Layer 1: convention envelope (outputs: [{uri, ...}]) ---
    for d in dicts:
        outputs = d.get("outputs")
        if isinstance(outputs, list):
            for item in outputs:
                if not isinstance(item, dict):
                    continue
                if item.get("render") is False:
                    continue
                uri = item.get("uri") or ""
                if not (isinstance(uri, str) and uri.startswith("s3://")):
                    continue
                label = str(item.get("label") or item.get("role") or "").strip().lower()
                basename = uri.lower().rstrip("/").rsplit("/", 1)[-1]
                if _is_explicit_operational(label) or _is_explicit_operational(basename) \
                        or basename in _NON_RENDERABLE_JSON_BASENAMES:
                    continue
                if uri.endswith("/"):
                    _add_prefix(uri)
                else:
                    _add(uri, allow_excluded_json=True)

    if out:
        return out

    # --- Layer 2: explicit s3:// URIs anywhere in string values ---
    def _scan_s3_uris(obj: Any) -> None:
        if isinstance(obj, str):
            for m in _S3_URI_RE.finditer(obj):
                _add(m.group(0).rstrip(".,;)]}"))
        elif isinstance(obj, dict):
            for v in obj.values():
                _scan_s3_uris(v)
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                _scan_s3_uris(v)

    for d in dicts:
        _scan_s3_uris(d)

    # NOTE: no early return here — Layer 2 may find explicit s3:// URIs (e.g.
    # an input asset_s3uri) while Layer 3 still needs to run to reconstruct
    # *output* URIs from output_s3_prefix + relative *_key fields (the M2C
    # diarization shape). Skipping Layer 3 would lose the VTT caption file.

    # --- Layer 3: bucket + key reconstruction (heuristic) ---
    def _add_with_bucket(bucket: str, val: Any) -> None:
        if not (isinstance(val, str) and val and bucket):
            return
        # Absolute URIs are Layer 2's job; prefixing them with a bucket would
        # produce a nonsense key like ``s3://bucket/s3://bucket/key``.
        if "://" in val:
            return
        if _is_renderable(val):
            _add(f"s3://{bucket}/{val.lstrip('/')}")

    for d in dicts:
        # Find bucket from s3:// prefix fields (e.g. output_s3_prefix)
        bucket: str | None = None
        for key, val in d.items():
            if isinstance(val, str) and val.startswith("s3://"):
                bucket = val[len("s3://"):].split("/", 1)[0]
                break

        # Find bucket from fields whose key contains "bucket"
        if not bucket:
            for key, val in d.items():
                if isinstance(key, str) and any(h in key for h in _BUCKET_KEY_HINTS):
                    if isinstance(val, str) and val and not val.startswith("s3://"):
                        bucket = val
                        break

        if not bucket:
            continue

        # Collect renderable keys from ANYWHERE in this dict's subtree (full
        # depth, not just one level) so a bucket declared on a parent still
        # reaches a relative key nested inside a list-of-dicts. This resolves the
        # TTS shape (out_bucket sibling + outputs[].wav_key) generically — any
        # tool that pairs a bucket with nested *_key values works, with no
        # field-name hardcoding. Renderability + operational filtering still gate
        # every add, so the deeper walk cannot pull in scratch.
        def _collect_subtree(node: Any) -> None:
            if isinstance(node, str):
                _add_with_bucket(bucket, node)
            elif isinstance(node, dict):
                for v in node.values():
                    _collect_subtree(v)
            elif isinstance(node, (list, tuple)):
                for v in node:
                    _collect_subtree(v)
        _collect_subtree(d)

    return out


_SOURCE_KEY_TOKENS = ("input", "source", "asset", "original", "src")

# A key that is JUST a media type is the thing the tool was GIVEN, not what it made.
# Audio stem separation names its input `data.audio_key` (audio_48k_mono.wav) and its
# products under `audiostem_sep_output.{speech,music,sfx}_key`. `audio_key` carries
# none of the tokens above, so it was scraped as a deliverable and the 120s INPUT
# audio got its own render tab -- a second tab with the same label as the real stems.
# The resolver already applies this rule (_INPUT_KEYS); the scrape path did not.
_BARE_MEDIA_KEYS = frozenset({
    "audio_key", "video_key", "image_key", "media_key", "file_key", "key",
})
_SOURCE_MEDIA_EXTS = (
    ".mp4", ".mov", ".webm", ".mkv", ".m4v",
    ".wav", ".mp3", ".m4a", ".flac",
)

# A url is ELIGIBLE to be source media if it is ours to read: an s3:// URI, or an https url
# inside our own `artifacts/` tree (CloudFront-served). Being ours is separate from being a
# source -- the caller still has to establish the role.
# Also a projects-session OUTPUT (asset-lineage Phase 3 (a)): `projects/<pid>/sessions/<sid>/output/`.
_OWN_ARTIFACT_PATH_RE = re.compile(
    r"https?://[^/]+/(?:[^?#]*/)?(?:artifacts/|projects/p[0-9a-f]{32}/sessions/[^/?#]+/output/)", re.I)

# `artifacts/<session>/input/...` -- an `/input/` segment marks media a user handed us, as
# opposed to `/output/` which the pipeline produced. Written by `ui/server/uploadKey.ts` and
# `main._artifact_input_prefix`. Matched as a whole segment so a file merely NAMED
# "input_notes.mp4" does not qualify.
_INPUT_SEGMENT_RE = re.compile(r"(?:^|/)input/", re.I)


def _is_own_media_ref(uri: Any) -> bool:
    """True if ``uri`` is a media reference we control (s3:// or our artifacts tree).

    Deliberately excludes arbitrary external https: a third-party url in an input-named
    field is not session media we can overlay a render on.
    """
    if not isinstance(uri, str) or not uri:
        return False
    if uri.startswith("s3://"):
        return True
    return bool(_OWN_ARTIFACT_PATH_RE.match(uri))


def _has_input_path(uri: Any) -> bool:
    """True if ``uri`` is one of our artifacts sitting under an ``/input/`` segment."""
    if not _is_own_media_ref(uri):
        return False
    return bool(_INPUT_SEGMENT_RE.search(str(uri).split("?", 1)[0]))


def _aggregate_artifacts(result: Any) -> Optional[dict]:
    """Role-split artifacts for a plan-result payload, plus per-node groups.

    The plan/job aggregate states each artifact's ROLE, so it does not need to be
    guessed from field names or url shape. Returns None for anything else, so every
    other tool keeps the structural extraction below.

    This exists because the render path is URL-based and the aggregate inlines its
    findings: a four-analysis plan reported artifact_count=0, dispatch_render got
    only a stray presigned video, and the result was one duplicated video track and
    an empty generic-preview instead of face/shot/speaker timelines.

    ``groups`` is per NODE, because one render dispatch is one manifest is one
    template. Dispatching all seven files together left the Render Agent to choose
    a single template for face metadata, shot boundaries and speaker turns at once
    -- collapsing to a generic "N data files" tab. Per node it can pick the face
    timeline for faces and the shot timeline for shots, which is what the render
    skills are written to do. The node id also names the tab after the work rather
    than after the tool that fetched it.
    """
    payload = result
    if isinstance(payload, dict) and "content" in payload:
        text = ""
        for block in payload.get("content") or []:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                text += block["text"]
        payload = text
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", "replace")
    if isinstance(payload, str):
        stripped = payload.strip()
        if not stripped.startswith("{") or "plan-result/" not in stripped:
            return None
        try:
            payload = json.loads(stripped)
        except Exception:
            return None
    if not isinstance(payload, dict) or not str(payload.get("schema", "")).startswith("plan-result/"):
        return None

    buckets: dict[str, list[str]] = {"analysis": [], "source": [], "media": []}
    groups: list[dict] = []
    # Plumbing nodes get no view of their own — see intermediate_node_ids. Their artifacts
    # still reach `buckets` (so a downstream analysis can use the proxy as its overlay
    # canvas); they simply do not become a tab.
    plumbing = intermediate_node_ids(payload.get("nodes") or [])
    for node in payload.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        group = {"label": _label_from_tool(node.get("tool")) or _humanize_node(node.get("node_id")),
                 "node_id": node.get("node_id"),
                 # Node ids repeat across a session's plans; the render key is plan-scoped
                 # (render.keys.plan_node_render_key).
                 "plan_id": payload.get("plan_id"),
                 # The plan's own name (`plan-lipsync8` -> "Lipsync8"): the title of a fan-out gallery.
                 "plan_label": _humanize_plan(payload.get("display_name")),
                 # Raw federated tool name (e.g. "shot-segmentation___get_job_status"). The
                 # AUTHORITATIVE view key downstream — the tool determines the render view.
                 "tool": node.get("tool"),
                 "urls": [], "source_urls": []}
        for art in node.get("artifacts") or []:
            if not isinstance(art, dict):
                continue
            url = art.get("url")
            if not isinstance(url, str) or not url.strip():
                continue
            role = str(art.get("role") or "output")
            key = "source" if role == "source" else ("analysis" if role == "analysis" else "media")
            if url not in buckets[key]:
                buckets[key].append(url)
            target = "source_urls" if key == "source" else "urls"
            if url not in group[target]:
                group[target].append(url)
        if group["urls"] and str(node.get("node_id")) not in plumbing:
            # The render_groups path renders group.source_urls as the overlay canvas. When the
            # plan-result carried no role=source for this node (resolve_node couldn't derive it
            # from the value), recover the media the job was invoked on from its durable record —
            # the group IS a job, so node_id == the execution ARN == job_id (design #1). Uniform
            # for faces/shots/diarization/stems; a no-op when nothing was recorded.
            if not group["source_urls"]:
                # Try the registry keys this node could be filed under, in order. The lookup is a
                # get_item by PRIMARY KEY (`jobId`), so a bare node id only works when the node id
                # IS the job id (standalone dispatch). Inside a plan the chainer files the row as
                # `<plan_id>#<node_id>`, which is why a plan node's source lookup found nothing and
                # the analysis rendered with no canvas (sessions a2ebd1e9 / a5a1b4fa).
                for src in _recorded_source_media_for_node(payload.get("plan_id"),
                                                           node.get("node_id")):
                    if src not in group["source_urls"]:
                        group["source_urls"].append(src)
                    if src not in buckets["source"]:
                        buckets["source"].append(src)
            groups.append(group)
    buckets["groups"] = groups
    return buckets


# A node kind whose findings ARE the user's answer, so it keeps its view even when a
# downstream node consumes it (diarize -> translate still owes the user the diarization).
# `media` is the convertible kind (proxy/transcode/trim) — the one safe to treat as plumbing.
_DROPPABLE_KINDS = frozenset({"media"})


def intermediate_node_ids(nodes: Any) -> set:
    """Node ids whose output is plumbing for another node, so they get no render tab.

    A GRAPH property, not a guess: if node B declares `depends_on: [A]`, then A's output
    exists to feed B. The proxy transcode behind three analyses should not become its own
    "Media Processing" tab (session a2ebd1e9 did exactly that). This is the deterministic
    replacement for the hard-coded `_CANONICAL_BASENAMES` allowlist, which cannot know
    anything about a tool it was not taught, and it is also a SPEED win — every dropped
    node is one fewer Render-Agent invocation (90s dispatch budget each).

    Only ever REMOVES a tab, so every ambiguous case fails OPEN (renders):
      * no `depends_on` present anywhere -> nothing is provably intermediate -> drop nothing
        (older aggregates predate the field);
      * a drop set that would leave NOTHING to render -> drop nothing (cycles, malformed DAGs);
      * only `media`-kind nodes are droppable — an `analysis`/`error`/`pending` node keeps its
        view because its findings (or its failure) are what the user asked for;
      * a `depends_on` naming a node absent from this aggregate is ignored, not a consumer;
      * a node with no artifacts was never going to render, so it is not reported.

    Applies to the plan flow only. Single-tool dispatch has no plan-result envelope, hence no
    groups, so this never runs on it.
    """
    if not isinstance(nodes, list) or len(nodes) < 2:
        return set()
    by_id: dict = {}
    for node in nodes:
        if isinstance(node, dict) and node.get("node_id"):
            by_id[str(node["node_id"])] = node
    if len(by_id) < 2:
        return set()

    # Consumers, counted only for edges that resolve inside this aggregate.
    consumed: set = set()
    saw_any_edge = False
    for node in by_id.values():
        deps = node.get("depends_on")
        if not isinstance(deps, list):
            continue
        for dep in deps:
            dep = str(dep)
            if dep in by_id:
                saw_any_edge = True
                consumed.add(dep)
    if not saw_any_edge:
        return set()  # no usable edges -> prove nothing, drop nothing

    droppable = {
        nid for nid in consumed
        if str(by_id[nid].get("kind") or "").lower() in _DROPPABLE_KINDS
        and (by_id[nid].get("artifacts") or [])
    }
    # Never render nothing: if every renderable node would be dropped, keep them all.
    renderable = {nid for nid, n in by_id.items() if (n.get("artifacts") or [])}
    if renderable and droppable >= renderable:
        return set()
    return droppable


def _label_from_tool(tool_name: Any) -> str:
    """`face-recognition___recognize_faces` -> `Face Recognition`: tab title from the work.

    Federated media tools are `<target>___<tool>`; the target is what a user thinks of as the
    job, matching the job names shown elsewhere. Returns "" when there is no usable tool so the
    caller falls back to the node id.
    """
    name = str(tool_name or "").strip()
    if not name:
        return ""
    target = name.split("___", 1)[0]
    # A user MCP server's target is an opaque id (`usr-8ddf64af…`): it names nothing a user knows,
    # and as a title it read "Usr 8ddf64af24be45c894a7a8c1f254c83f" (session be68313c).
    if re.fullmatch(r"(?:usr-)?[0-9a-f]{16,}", target, flags=re.IGNORECASE):
        return ""
    words = [w for w in re.split(r"[-_\s]+", target) if w]
    return " ".join(w[:1].upper() + w[1:] for w in words)


def _humanize_plan(display_name: Any) -> str:
    """``plan-lipsync8`` -> ``Lipsync8``; ``""`` for no name. The ``plan-`` prefix is bookkeeping."""
    text = re.sub(r"^plan[-_\s]+", "", str(display_name or "").strip(), flags=re.IGNORECASE)
    return _humanize_node(text) if text else ""


def _humanize_node(node_id: Any) -> str:
    """``t3_detect_faces`` -> ``Detect Faces``: a tab title naming the work.

    Leading ordering prefixes (``t3_``, ``sg7_``) are positional bookkeeping, not
    meaning, so they are dropped.
    """
    text = str(node_id or "").strip()
    if not text:
        return ""
    parts = [p for p in re.split(r"[-_\s]+", text) if p]
    if parts and re.fullmatch(r"[a-z]{0,3}\d+", parts[0]):
        parts = parts[1:] or parts
    return " ".join(p[:1].upper() + p[1:] for p in parts)


def extract_source_uris(result: Any) -> list[str]:
    """Extract the *source* media a tool operated on — context, never output.

    Rendering an analysis result is far more useful when the original media is
    available (e.g. drawing detection boxes over the video the boxes describe).
    Source references are therefore surfaced separately from ``result.urls``:
    they are passed to the Render Agent as context but never make a job
    "renderable" on their own, so an input asset alone can't trigger a render.

    Recognition is structural and tool-agnostic, from two independent signals:

    * **The field name** — any key carrying an input/source/asset token, whose value is a
      media URI (directly or via a nested ``{"s3uri": ...}`` wrapper). This is the tool
      *stating* what it ran on, so it is the strong signal.
    * **The path** — a url under an ``/input/`` segment of our own ``artifacts/`` tree is
      the media a user handed us, whatever the field is called.

    The two answer different questions and the field name wins where it exists: it is the
    role *relative to this tool*, while the path is the role *relative to the session*. In a
    DAG, node B's input is node A's ``/output/``, so path must not veto a source-named field.
    Conversely the path fallback promotes only ``/input/`` — inferring "source" from
    ``/output/`` with no field-name signal would swallow the deliverables we exist to render.

    A plan-result aggregate states the role outright, so it is read rather than inferred.

    Eligibility is scheme-independent: an ``s3://`` URI or an https url inside our artifacts
    tree both count. The previous ``startswith("s3://")`` test used the SCHEME as a proxy for
    the ROLE, which breaks as references become canonical CloudFront https — an input arriving
    as ``https://<cdn>/artifacts/<sid>/input/clip.mp4`` was classified as an output and
    rendered its own spurious tab.
    """
    aggregate = _aggregate_artifacts(result)
    if aggregate is not None:
        return list(aggregate["source"])
    out: list[str] = []
    seen: set[str] = set()

    def _add(uri: Any) -> None:
        if not _is_own_media_ref(uri):
            return
        clean = uri.split("?", 1)[0].lower()
        if uri in seen or not clean.endswith(_SOURCE_MEDIA_EXTS):
            return
        seen.add(uri)
        out.append(uri)

    for d in _content_dicts(result):
        for key, val in d.items():
            lowered = str(key).lower()
            tokens = set(re.findall(r"[a-z0-9]+", lowered))
            named_source = bool(tokens & set(_SOURCE_KEY_TOKENS) or lowered in _BARE_MEDIA_KEYS)
            candidates = [val] if isinstance(val, str) else (
                list(val.values()) if isinstance(val, dict) else [])
            for cand in candidates:
                # Field name wins; otherwise fall back to the path, which only ever
                # promotes `/input/`.
                if named_source or _has_input_path(cand):
                    _add(cand)
    return out


# --------------------------------------------------------------------------
# Async completion -> source media (design #1)
# --------------------------------------------------------------------------
# An async job's completion result (get_job_status / results) does NOT echo the
# media it ran on, and it is not a plan-result envelope, so extract_source_uris
# finds nothing and the render falls back to a table with no video canvas. The
# source WAS recorded on the durable JobRecord at start (source_media.py); read it
# back by job_id here so faces/shots/diarization/stems overlay on their source
# uniformly. The lookup is INJECTABLE (tests set a callable) and otherwise reads the
# Job Registry lazily + best-effort: the hook must stay importable without the
# jobs/DDB stack, and a miss must never break a render.
_job_source_media_lookup: Optional[Any] = None
# Run-scoped source lookup (a run's shared source, via the actor-status GSI).
# Injectable for tests, same pattern as ``_job_source_media_lookup``.
_run_source_media_lookup: Optional[Any] = None

_JOB_ID_KEYS = ("job_id", "request_id", "jobId", "executionArn", "execution_arn", "arn")


def _job_id_from_result(result: Any) -> Optional[str]:
    """The job id / execution ARN this tool result belongs to, or None."""
    for d in _content_dicts(result):
        for k in _JOB_ID_KEYS:
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def _job_id_from_tool_use(tool_use: Any) -> Optional[str]:
    """The job id from a status/results CALL's input args (e.g. get_job_status(job_id=...)).

    Most reliable at completion: the agent polls the status tool WITH the job id, so it is
    present in the input even when the result body names it differently or not at all.
    """
    if not isinstance(tool_use, dict):
        return None
    args = tool_use.get("input")
    if not isinstance(args, dict):
        return None
    for k in _JOB_ID_KEYS:
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _default_job_source_lookup(job_id: str) -> list:
    """Read the recorded source media for a job from the Job Registry (best-effort).

    Reads ``sourceMedia`` directly by ``jobId`` — NOT via the actor-scoped ``get`` — because
    the job id arrives from THIS session's own render aggregate/completion (ownership already
    established) and the value is a non-sensitive ``s3://`` URI the session can already render.
    Depending on an actor contextvar being bound at render time is exactly what made the first
    attempt silently return nothing.
    """
    import os
    table = os.environ.get("JOB_REGISTRY_TABLE", "")
    if not table:
        return []
    try:
        import boto3
        region = os.environ.get("AWS_REGION") or os.environ.get("REGION") or "us-west-2"
        item = boto3.resource("dynamodb", region_name=region).Table(table).get_item(
            Key={"jobId": job_id}
        ).get("Item")
        return list(item.get("sourceMedia", []) or []) if item else []
    except Exception:  # noqa: BLE001 - never fail a render over a source lookup
        return []


def source_media_for_completion(result: Any, tool_use: Any = None) -> list:
    """Source media recorded for the job this result belongs to (empty if none/unknown).

    Resolves the job id from the status CALL's input first (reliable), then the result body.
    """
    jid = _job_id_from_tool_use(tool_use) or _job_id_from_result(result)
    return _recorded_source_media(jid)


def _recorded_source_media_for_node(plan_id: Optional[str], node_id: Optional[str]) -> list:
    """Source media for a PLAN NODE, trying every key the row could be filed under.

    The registry lookup is a `get_item` by primary key (`jobId`), so the key has to be exact.
    One logical job is reachable under two of them:

      `<plan_id>#<node_id>`  the row the Plan Chainer writes for a plan node
      `<node_id>`            correct only when the node id IS the job id (standalone dispatch,
                             where the "node id" is the execution ARN)

    Previously only the second was tried, so inside a plan the lookup missed every time and the
    analysis rendered with no canvas. Ordered plan-scoped first because it is the specific one;
    the bare id is the standalone fallback. Returns on the first hit.
    """
    for key in (f"{plan_id}#{node_id}" if plan_id and node_id else None, node_id):
        found = _recorded_source_media(key)
        if found:
            return found
    # Node row recorded no source. The chainer files a plan node WITHOUT sourceMedia even
    # though the gateway job it spawned recorded it (proven on session feb23706: node
    # `<plan>#sg2_diarize` has no sourceMedia, its diarization job has the input video). So an
    # analysis rendered with no canvas and did not fold onto the source's timeline. Recover the
    # run's shared source — read actor/run from the node's OWN row (self-contained, no ambient
    # context) and accept only a single unambiguous source (guards multi-source/clip runs).
    #
    # Best-effort like the rest of this module: the row lookup is a live boto3 read, so ANY
    # failure (no credentials in CI/unit tests, missing table, throttling) must degrade to [],
    # never raise — a source-recovery miss can never break a render or a test.
    if plan_id and node_id:
        try:
            row = _job_row_lookup(f"{plan_id}#{node_id}") or {}
            recovered = _run_shared_source_media(row.get("actorId"), row.get("runId"))
            if recovered:
                return recovered
        except Exception:  # noqa: BLE001 - recovery is a nicety, never a blocker
            return []
    return []


def _recorded_source_media(job_id: Optional[str]) -> list:
    """The source media recorded on a job at registration, by job id (empty if none/unknown).

    Injectable (``_job_source_media_lookup``) for tests; otherwise a best-effort registry read.
    """
    if not job_id:
        return []
    lookup = _job_source_media_lookup or _default_job_source_lookup
    try:
        return list(lookup(job_id) or [])
    except Exception:  # noqa: BLE001
        return []


def _default_run_source_lookup(actor_id: str, run_id: str) -> list:
    """Distinct source media recorded across a RUN's jobs (best-effort), via the
    actor-status GSI.

    A run's analysis NODE rows are filed without ``sourceMedia`` even though the gateway
    JOBS they spawn record it, so the per-node lookup misses. Every job in a run carries
    ``runId``; the actor-status index (``actorId`` HASH) is the only run-reachable index,
    so we query the actor's rows and filter to ``run_id`` in-process. Returns the de-duped
    union of every matching row's ``sourceMedia`` (order-stable). Bounded pagination so a
    heavy actor can't stall a render; nothing is hardcoded — the source comes from the run's
    own rows, of whatever media type they recorded.
    """
    import os
    table = os.environ.get("JOB_REGISTRY_TABLE", "")
    if not (table and actor_id and run_id):
        return []
    import boto3
    from boto3.dynamodb.conditions import Key
    region = os.environ.get("AWS_REGION") or os.environ.get("REGION") or "us-west-2"
    tbl = boto3.resource("dynamodb", region_name=region).Table(table)
    seen: set = set()
    out: list = []
    kwargs: dict = {
        "IndexName": "actor-status-index",
        "KeyConditionExpression": Key("actorId").eq(actor_id),
        "ScanIndexForward": False,  # newest-first
    }
    for _ in range(5):  # bounded: at most 5 GSI pages
        resp = tbl.query(**kwargs)
        for it in resp.get("Items", []):
            if it.get("runId") != run_id:
                continue
            for uri in (it.get("sourceMedia") or []):
                if uri not in seen:
                    seen.add(uri)
                    out.append(uri)
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    return out


def _run_shared_source_media(actor_id: Optional[str], run_id: Optional[str]) -> list:
    """The run's SINGLE shared source media, or ``[]`` when absent OR ambiguous.

    Generic recovery for a plan node whose OWN row recorded no source: every analysis in one
    run is about the same uploaded source, so a sibling job's recorded source is this node's
    too. Guarded to a SINGLE distinct source — a multi-source run (clips, several uploads)
    cannot be disambiguated cheaply here, so we decline rather than risk folding an analysis
    onto the wrong timeline (the ``derive_source_key`` clip-identity rule must not be undone).
    Best-effort: any failure returns ``[]`` and the caller keeps today's behavior.
    """
    if not (actor_id and run_id):
        return []
    lookup = _run_source_media_lookup or _default_run_source_lookup
    try:
        srcs = list(lookup(actor_id, run_id) or [])
    except Exception:  # noqa: BLE001
        return []
    return srcs if len(srcs) == 1 else []


def _job_row_lookup(job_id: str) -> Optional[dict]:
    """The Job Registry row for ``job_id`` (best-effort; None when unknown).

    Separate from ``_default_job_source_lookup``, which returns only ``sourceMedia``. This
    returns the whole row because the render key needs ``node_id``. Reads by primary key with
    no actor scoping, for the same reason documented there: the job id arrives from THIS
    session's own aggregate/completion, so ownership is already established, and depending on
    an actor contextvar being bound at render time is what silently returned nothing before.
    Monkeypatched by tests.
    """
    import os
    table = os.environ.get("JOB_REGISTRY_TABLE", "")
    if not table or not job_id:
        return None
    import boto3
    region = os.environ.get("AWS_REGION") or os.environ.get("REGION") or "us-west-2"
    return boto3.resource("dynamodb", region_name=region).Table(table).get_item(
        Key={"jobId": job_id}).get("Item")


def canonical_render_key(job_id: Optional[str]) -> str:
    """ONE render key per logical job, whichever path reached it (redesign §4 Rule 1).

    A media job is reachable two ways in one session, and they used to produce different
    manifest keys for the same work:

      IN-TURN      plan-result aggregate -> render_groups -> render_key = ``node_id``
                   -> ``artifacts/<sid>/render/sg_shots.json``
      OUT-OF-TURN  ``autoRenderOnComplete`` re-fires the turn -> a status tool for one job ->
                   single dispatch, whose result carries only ``job_id`` / ``executionArn``

    Session a2ebd1e9 shows the split in production: ARN-keyed manifests
    (``arn_aws_states_…face-recognition_…``) sitting beside ``sg_shots.json`` for one plan.

    Resolving through the registry row's own ``node_id`` collapses them: an ARN-reached render
    lands on the node-keyed manifest instead of minting its own. When the job is NOT a plan node
    (a standalone dispatch), its sanitized job id is the key — which is the half of Rule 1 that
    was missing, since ``render_key`` was previously empty for single dispatch and identity fell
    to the url content hash.

    Returns "" when there is no job at all (a truly synchronous tool). That is deliberate: the
    caller then keeps the content hash, which at least derives from the durable artifacts.
    Inventing a key here would be derived from nothing and would NOT converge across renders.

    Never raises and never depends on the registry read succeeding — a job id is itself a stable
    durable identifier, so a lookup failure degrades to it rather than to a url hash.
    """
    if not job_id or not isinstance(job_id, str) or not job_id.strip():
        return ""
    raw = job_id.strip()
    if ":stateMachine:" in raw:
        # A state-machine ARN names a WORKFLOW, not a run (a TTS describe_tool / get_voice result carries one): keying
        # a render on it gave every catalog lookup the same "job" tab (staging be68313c). No job -> no key.
        return ""
    try:
        row = _job_row_lookup(raw) or {}
        # Real Job Registry rows (jobs/registry.py `_to_item`) carry ``planNodeId`` (a job run
        # inside a plan node) or ``nodeId`` (the node's own coordinator row) and the plan as
        # ``runId`` -- never ``node_id``/``plan_id``; staging shot job 2d2596d9 has
        # planNodeId=sg1_segment_shots, runId=92f2811a-…. ``node_id``/``plan_id`` are kept for
        # older callers' dict shapes.
        node = str(row.get("planNodeId") or row.get("nodeId") or row.get("node_id") or "").strip()
        if node:
            # Plan-scoped, like the in-turn render groups (render.keys).
            from render.keys import plan_node_render_key
            return _sanitize_render_key(plan_node_render_key(row.get("runId") or row.get("plan_id"), node))
    except Exception:  # noqa: BLE001 - identity must not depend on a DDB call
        logger.info("canonical_render_key: registry lookup failed for %s; using the job id", raw)
    return _sanitize_render_key(raw)


def _sanitize_render_key(value: str) -> str:
    """S3-key-safe form, matching `_render_manifest_id`'s own sanitisation."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)[:120]


def _extract_job_status(result: Any) -> Optional[str]:
    """Extract the async job status (SUCCEEDED/RUNNING/FAILED) from a tool result.

    This is the *job-level* status from within the content payload — not the
    Strands-level tool-call status ("success"/"error"). Returns None if no
    recognizable job status is found.
    """
    _JOB_STATUSES = {"SUCCEEDED", "RUNNING", "FAILED", "PENDING", "TIMED_OUT"}
    for d in _content_dicts(result):
        s = d.get("status")
        if isinstance(s, str) and s in _JOB_STATUSES:
            return s
    return None


# Artifacts this container has already handed to the renderer.
#
# This replaces a tool-NAME allowlist. The old condition asked "is this tool
# named like a producer?" -- `*_results`, `get_job_status`, or literally
# `artifact` -- as a proxy for "did this call produce the artifact". The proxy
# failed the obvious way: any new artifact-producing tool renders nothing until
# someone remembers to widen the allowlist, which is precisely what happened when
# a user-registered MCP server started returning images.
#
# Asking about the ARTIFACT instead answers all three cases the allowlist was
# hand-carving:
#
#   relay echo      dispatch_tool quotes the sub-agent's URL -> already seen
#   polling storm   get_job_status repeats the URL each poll -> already seen
#   new producer    a URL nobody has rendered yet            -> renders
#
# A set in module scope is genuinely per-session here, not a shortcut: AgentCore
# gives each session its own microVM and a session belongs to one user, so this
# container's lifetime IS the session's. It is deliberately NOT durable -- losing
# it on a cold start re-renders an artifact at worst, while persisting it would
# mean a storage round trip on every tool result to prevent a duplicate preview.
# Tools that RELAY a sub-agent's output rather than producing anything. They must
# be excluded by name, and novelty cannot replace that: the sub-agent runs in its
# OWN container with its own `_dispatched_artifacts`, so a URL it already rendered
# is still novel to the orchestrator relaying it. Shipping novelty alone produced
# exactly two render tabs per image.
#
# This is a fact about what these tools ARE, not a naming-convention guess about
# which tools might produce artifacts -- which is what the removed allowlist was.
# `plan_and_execute_tool` is deliberately ABSENT. The two orchestration tools look
# alike and behave oppositely:
#
#   dispatch_tool          the sub-agent runs hooks (task_agent loads them when
#                          node_id is absent), so it renders and the relay must
#                          not render again
#   plan_and_execute_tool  the sub-agent's hooks are SUPPRESSED inside a plan
#                          (node_id present, "plan_and_execute_tool renders ONCE
#                          at the end"), and that end-of-plan render IS this
#                          hook firing on the plan tool's result -- there is no
#                          render call anywhere in plan_and_execute_tool.py
#
# Excluding both removed the only render path for anything executed through a
# plan: the sub-agent was already silenced by node_id, and the orchestrator was
# then silenced by name, so an image generated inside a plan rendered nowhere.
RELAY_TOOLS = frozenset({"dispatch_tool"})


def is_relay_tool(name: str) -> bool:
    """True when `name` only forwards another agent's output."""
    if not name:
        return False
    bare = name.split("___")[-1]
    return bare in RELAY_TOOLS or name in RELAY_TOOLS


# Tools whose result is PROSE that quotes media URLs rather than media the tool
# produced. There is no structural difference to detect: fal.ai returns a
# markdown image tag inside a text block, and an AWS docs page embeds an image
# the SAME way -- so URL-in-text detection cannot tell "here is the image I
# generated" from "here is an image this page happens to contain". A size
# threshold does not help either: doc pages contain both a 1067-byte console
# warning glyph and a full-size console screenshot. The only reliable signal is
# what the tool IS, declared here.
#
# DEFAULT IS ARTIFACT-PRODUCING. A tool NOT in this set renders. This direction
# is load-bearing and is the whole point of the fix: the removed condition was a
# tool-name ALLOWlist, and it silently rendered nothing for any producer nobody
# had thought to enumerate -- which is exactly how a user-registered MCP server
# (fal.ai) returning images rendered nowhere. So we only ever SUBTRACT known
# non-producers here, and only platform tools we control; we never require a
# user-registered server to announce itself as a producer.
#
# Matching is on the BARE name after stripping the target prefix, so it holds
# regardless of gateway/target name (e.g.
# `mediaworks-harness-aws-mcp___aws___read_documentation`). CRITICAL: these
# names THEMSELVES contain `___` (`aws___read_documentation`), so the prefix is
# split off with maxsplit=1 -- NOT `split("___")[-1]`, which would yield
# `read_documentation` and match nothing. That exact off-by-a-separator bug has
# already happened in this repo; see is_relay_tool above, which can split on all
# separators only because its members (`dispatch_tool`) contain none.
NON_ARTIFACT_TOOLS = frozenset({
    "aws___search_documentation",
    "aws___read_documentation",
    "aws___retrieve_skill",
    # The gateway's own tool-search helper. It returns tool DESCRIPTIONS, and those
    # descriptions quote example urls -- which the render hook was treating as
    # output, producing an empty video player captioned
    # "X Amz Bedrock Agentcore Search". A search tool cannot produce an artifact by
    # definition, so this is a statement of fact rather than a judgement call.
    "x_amz_bedrock_agentcore_search",
    # Readers and URL vendors. Both hand back something that ALREADY existed:
    # read_result_files returns file CONTENT (which quotes the s3 uris and http
    # urls inside it, including the SOURCE media), and get_download_url returns a
    # presigned GET for an object someone else produced. Rendering either one
    # re-renders an input as though it were a result.
    #
    # Observed cost, session 842bb417: of 8 render tabs, three were empty
    # generic-preview manifests (tracks: 0) attributed to "Media Outputs" and
    # "Read Result Files", and one played the untouched 11:58 source video --
    # while the genuinely rich face view (video-annotations, 5 identities, 40
    # boxes) was crowded out. Neither tool can produce an artifact, so this is
    # the same statement of fact as the search helper above.
    "read_result_files",
    "get_download_url",
    # view_images shows the model images that already exist (its refs are its INPUTS).
    "view_images",
    # Model-gateway METADATA/QUERY tools (fal and any other model server). They return a
    # model's SCHEMA, PRICING, or a RECOMMENDATION -- prose/data that QUOTES the model's
    # EXAMPLE media (a demo still/clip), never a deliverable THIS call produced. The render
    # hook was harvesting those example urls and opening real tabs: a fal example image (a
    # mountain-biker demo) surfaced inside a Meridian oil-reel gallery, and a fal video
    # model's example clip rendered as a stray ~5s "Video" tab in a voice-clone session
    # (sessions d92e6328 / 4681f2da). A schema/pricing/recommendation lookup cannot produce
    # an artifact, so this is the same statement of fact as the search helper and readers
    # above. run_model / submit_job -- which DO produce media -- are deliberately absent, so
    # real generation still renders.
    "get_model_schema",
    "recommend_model",
    "get_pricing",
    # Project media search. A Hit carries no URL of its own, but its Compact_Hit
    # `text` can QUOTE one -- a scene's on-screen text may read
    # `https://example.com/promo.mp4`. `extract_urls` would find that in the tool
    # result and the URL_Render_Rule would render a URL taken from a Hit. A search
    # tool cannot produce an artifact, so Moments (from ledger Hits) and URL renders
    # stay disjoint and the same media is never rendered twice in a turn (Req 3.7,
    # 3.25). `search_dispatch` is arm B's relay of the same search. The default
    # arm-A tool name is `search_content`; a non-default `SEARCH_TOOL_NAME` env value
    # is matched in is_non_artifact_tool below.
    "search_content",
    "search_dispatch",
    # The Project_Tools (project_search.project_tools). They report on and manage the
    # Active_Project's EXISTING assets, and their replies quote those assets' source
    # s3:// / https URIs (`project_status` matches, the `asset` row `retry`/`readd`
    # return, `register_s3_location`'s objects). Harvesting those rendered an input as a
    # result: staging opened "Project Status — Video/Data" tabs and spent ~90s in the
    # render agent. `show_asset` opens its own asset-player tab, so harvesting
    # its reply could only duplicate that tab; `browse_project` likewise opens its own project-browser tab.
    "project_status",
    "register_s3_location",
    "retry_asset",
    "remove_asset",
    "readd_asset",
    "show_asset",
    "use_asset",  # returns the file's reference for another tool, not media to render
    "browse_project",
    "organize_project",  # files are moved / bookmarked, nothing new is produced to render
    "list_projects",     # names the user's projects; nothing to render
    "switch_project",    # opens a new chat in another project; nothing to render
    "create_project",    # creates a project and opens a chat in it; nothing to render
    "delete_project",    # returns a confirmation card; nothing to render
    # The knowledge-graph tools (project_search.graph_tools). Read-only: their results name nodes and
    # files but never produce media; what they touched renders once, as the turn's "↳ Graph" tab
    # (render-graph), so a harvested URL in a node's props must not open a second tab.
    "graph_schema",
    "graph_find",
    "graph_neighbours",
    "graph_paths",
    "graph_context",
    "graph_query",
})

#: Default arm-A Search_Tool name. A deployment may override it with the
#: ``SEARCH_TOOL_NAME`` env var; is_non_artifact_tool matches that exact value too,
#: so ``<target>___<custom>`` is still recognised as a non-producing search tool.
_DEFAULT_SEARCH_TOOL_NAME = "search_content"


def is_non_artifact_tool(name: str) -> bool:
    """True when `name` returns prose that may QUOTE media it did not produce.

    Asks the PROTOCOL first. MCP tools carry an optional ``annotations`` block
    whose ``readOnlyHint`` states whether the tool changes anything; a read-only
    tool cannot have produced a new artifact, so that hint answers this question
    definitionally and covers servers nobody has registered yet. NON_ARTIFACT_TOOLS
    survives only as a fallback for servers that omit annotations -- the spec makes
    them optional, so absence must not be read as "false".

    Advisory use only. Annotations are server-supplied, so they are fine for
    deciding what to render and wrong for deciding what may run; authorisation
    stays an explicit denylist in the gateway interceptor.
    """
    if not name:
        return False
    # Strip a single target prefix (maxsplit=1). The set members contain `___`,
    # so splitting on every separator would drop the `aws___` and match nothing.
    bare = name.split("___", 1)[-1]
    try:
        from mcp_manager import read_only_tool_names

        read_only = read_only_tool_names()
    except Exception:  # noqa: BLE001 - rendering must not depend on the manager
        read_only = set()
    if name in read_only or bare in read_only:
        return True
    if bare in NON_ARTIFACT_TOOLS or name in NON_ARTIFACT_TOOLS:
        return True
    # A deployment may rename the arm-A Search_Tool via SEARCH_TOOL_NAME. Its default
    # (`search_content`) is already in the set above; match a non-default value here so
    # `<target>___<custom>` is recognised too (bare uses the same maxsplit=1 prefix rule).
    search_name = os.environ.get("SEARCH_TOOL_NAME", "") or _DEFAULT_SEARCH_TOOL_NAME
    if search_name != _DEFAULT_SEARCH_TOOL_NAME and search_name in (name, bare):
        return True
    return False


_dispatched_artifacts: set[str] = set()

# Every SOURCE/input url this session has ever seen, normalized to its object key. Source
# recognition is per-result (extract_source_uris reads a role or an input/source/asset key), but
# the SAME asset resurfaces later WITHOUT that context -- e.g. the plan aggregate correctly tags
# the original upload as `source`, then a later get_download_url/read of the job hands back that
# same object as a bare url. Excluding only the current result's sources let the second sighting
# render the INPUT as an output (a 60s proxy job that rendered its 300s original). Remembering
# sources for the whole session, keyed by _norm_url so s3:// and a presigned https of the same
# object compare equal, keeps an input an input no matter which tool later echoes it. Generic:
# role/key-based, no tool-name or media-type list.
_session_source_urls: set[str] = set()


def mark_artifacts_dispatched(urls: list[str]) -> None:
    """Record that `urls` have been sent to the renderer."""
    for url in urls:
        if url:
            _dispatched_artifacts.add(url)


def remember_source_urls(uris: Optional[list[str]]) -> None:
    """Record source/input uris for the session so a later sighting can't render them as output."""
    from render.review import _norm_url
    for u in uris or ():
        if isinstance(u, str) and u.strip():
            key = _norm_url(u)
            if key:
                _session_source_urls.add(key)


#: A session output under `output/inline/` is an illustration for the CHAT (a frame strip, a grid shown beside
#: the text that explains it): the answer shows it inline, so it never also opens a Renderer tab.
INLINE_OUTPUT_SEGMENT = "/output/inline/"


def is_inline_only(url: str) -> bool:
    return INLINE_OUTPUT_SEGMENT in (url or "").split("?", 1)[0]


def new_artifact_urls(urls: list[str], mimes_by_url: dict[str, str],
                      source_urls: Optional[list[str]] = None) -> list[str]:
    """Renderable OUTPUT URLs not already dispatched, in order.

    Excluding source media is not the same concern as identity and cannot be
    folded into it. A job status poll echoes the asset that was fed IN, so a
    rule based on novelty alone renders the input of a job that has not
    finished -- which a test caught, and which is worse than the duplicate
    previews the old tool-name allowlist existed to prevent.

    So three orthogonal rules, each answering its own question:
      is this an output?   -> not in source_urls (this result) AND not a known session source
      is it new?           -> not already dispatched
    Session sources are matched by normalized object key so a presigned https of an s3:// source
    is still recognised as the input it is.
    """
    from render.review import _norm_url
    excluded = set(source_urls or ())
    return [
        u for u in urls
        if u in mimes_by_url
        and not is_inline_only(u)
        and u not in excluded
        and _norm_url(u) not in _session_source_urls
        and u not in _dispatched_artifacts
    ]


def reset_dispatched_artifacts() -> None:
    """Test seam. Never called in production -- the container's death is the reset."""
    _dispatched_artifacts.clear()
    _session_source_urls.clear()


_CELL_OUTPUTS_HEADER = "Files this cell wrote to the session output folder:"


def _code_cell_outputs_only(tool_name: str, result: Any, urls: list) -> list:
    """For a code_interpreter result, only the files the cell WROTE (sandbox_outputs' report) are output.

    A cell prints whatever it touches -- the reel stitch printed the 8 presigned INPUT clips while building
    its manifest, and they opened as a "Code Interpreter" gallery instead of the reel (staging c4a4f142).
    The sandbox can only write to this session's output prefixes, and every write there is reported, so
    while that reporting is live the report IS the cell's output. Without a bound session (no reporting)
    the urls are left as they were.
    """
    if (tool_name or "").rsplit("___", 1)[-1] != "code_interpreter":
        return urls
    try:
        import sandbox_outputs  # noqa: PLC0415
        if not sandbox_outputs.output_roots():
            return urls
    except Exception:  # noqa: BLE001 - reporting unavailable: leave the urls alone
        return urls
    written: list[str] = []
    for block in (result.get("content") if isinstance(result, dict) else None) or []:
        text = block.get("text") if isinstance(block, dict) else None
        if isinstance(text, str) and text.startswith(_CELL_OUTPUTS_HEADER):
            _walk_urls(text, written)
    try:
        from media.turn_outputs import is_own_write  # a write this turn verified (HEAD), any prefix
    except Exception:  # noqa: BLE001
        def is_own_write(_u: Any) -> bool:
            return False
    return [u for u in urls if u in written or is_own_write(u)]


def ctx_after_tool_result(event: Any, base: Optional[dict] = None) -> dict:
    """Normalize an ``AfterToolCallEvent`` (or stub) → event context."""
    tool_use = getattr(event, "tool_use", None) or {}
    name = tool_use.get("name", "") if isinstance(tool_use, dict) else ""
    use_id = tool_use.get("toolUseId", "") if isinstance(tool_use, dict) else ""
    result = getattr(event, "result", None) or {}
    # A plan/job aggregate states each artifact's role, so read it rather than
    # scraping. Regex extraction would put the role="source" proxy in `urls` and
    # thereby render an INPUT as an output -- the defect that gave the original
    # upload its own "Media Processing" tab.
    aggregate = _aggregate_artifacts(result)
    render_groups: list = []
    if aggregate is not None:
        urls = list(aggregate["analysis"]) + [
            u for u in aggregate["media"] if u not in aggregate["analysis"]
        ]
        render_groups = aggregate.get("groups") or []
    else:
        urls = extract_urls(result)
        # A probe answer the agent SAVED itself (the `artifact` tool returns its https URL, e.g.
        # `meridian_metadata.json`) is the same answer as a probe's declared `metadata.json`: it is
        # shown as text in the chat and must not become a render tab. These urls reach here through
        # regex extraction, which never ran the JSON rules (session fd4ce1bd paired one upload's
        # video with the OTHER upload's metadata in a "Render Output" tab).
        urls = [u for u in urls if not _is_probe_answer_json(u)]
        for uri in extract_output_uris(result):
            if uri not in urls:
                urls.append(uri)
        urls = _code_cell_outputs_only(name, result, urls)

    # Resolve run_id from context — when set, this tool call is part of an
    # async plan execution and hooks should suppress auto-rendering.
    run_id: Optional[str] = None
    try:
        from jobs.context import current_run_id
        run_id = current_run_id()
    except Exception:
        pass

    src_uris = extract_source_uris(result)
    if not src_uris:
        # An async completion result does not echo the media it ran on; recover the
        # source recorded on the job at start so the analysis renders over its media
        # instead of collapsing to a table (design #1).
        src_uris = source_media_for_completion(result, tool_use)
    # Remember these sources for the whole session so a later tool that echoes the same asset
    # (without the source role/key) can't render the input as an output.
    remember_source_urls(src_uris)

    ctx: dict[str, Any] = {
        "event": "after_tool_result",
        "tool": {"name": name, "role": role_for_tool(name), "use_id": use_id},
        "result": {
            "urls": urls,
            # Context only — deliberately excluded from `mimes` so a source
            # asset can never satisfy the render condition by itself.
            "source_urls": src_uris,
            # One dispatch per NODE, so each analysis gets its own manifest and the
            # Render Agent can choose the face timeline for faces and the shot
            # timeline for shots instead of one generic "N data files" tab.
            "render_groups": render_groups,
            "mimes": _mimes_for(urls),
            # Renderable URLs this session has not already rendered. The render
            # condition keys off THIS rather than the tool's name, so a producer
            # does not need to be known in advance to have its output shown.
            # Empty for relay tools: whatever they quote was produced (and
            # rendered) elsewhere, in a container whose dedupe state this one
            # cannot see. Also empty for NON-artifact-producing tools (AWS docs
            # etc.): their prose merely QUOTES image URLs it did not produce, and
            # `urls`/`mimes` stay populated for other consumers -- only this
            # gates rendering.
            "new_artifacts": [] if (is_relay_tool(name) or is_non_artifact_tool(name)) else new_artifact_urls(
                urls, _mime_by_url(urls), src_uris
            ),
            "status": result.get("status") if isinstance(result, dict) else None,
            "job_status": _extract_job_status(result),
            # The job this result belongs to, resolved HERE because this is the only place the
            # RAW result and tool_use are in scope. Consumers see the derived `result` dict, so
            # they cannot recompute it -- an earlier attempt to do so downstream silently
            # produced nothing, which is the failure mode this key exists to prevent.
            # `render_action` turns it into the render key for single dispatch (redesign Rule 1),
            # so a job reached out-of-turn by ARN converges on its in-turn node-keyed manifest.
            "job_id": _job_id_from_tool_use(tool_use) or _job_id_from_result(result) or "",
        },
        "run_id": run_id or "",
    }
    for k, v in (base or {}).items():
        ctx.setdefault(k, v)
    return ctx


def _ctx_before_tool(event: Any, base: Optional[dict] = None) -> dict:
    tool_use = getattr(event, "tool_use", None) or {}
    name = tool_use.get("name", "") if isinstance(tool_use, dict) else ""
    ctx: dict[str, Any] = {
        "event": "before_tool",
        "tool": {"name": name, "role": role_for_tool(name)},
    }
    for k, v in (base or {}).items():
        ctx.setdefault(k, v)
    return ctx


# AgentCore Memory rejects an oversized CreateEvent with a 413, and that failure
# lands AFTER the work is done: a turn that had already generated, muxed and
# uploaded the final video died with "Failed to create message: An error occurred
# (413) when calling the CreateEvent operation", losing the answer. The usual cause
# is a tool returning inline base64 media or a very large blob, which is also paid
# for on every subsequent turn once it is in the history.
#
# Capping here is deliberate: a truncated result the model can still read is
# strictly better than a 413 that discards a completed turn. Artifacts and renders
# travel as URLs, not inline bytes, so bounding a text block does not cost a render.
_MAX_TOOL_RESULT_CHARS = 60_000
_TRUNCATION_NOTE = (
    "\n\n[... truncated to keep the conversation writable — the full output was too "
    "large for session memory. Artifacts and files are referenced by URL, so use "
    "those rather than inline content.]"
)


def _cap_tool_result_size(event: Any) -> None:
    """Truncate oversized text blocks in a tool result, in place.

    Best-effort and never raises: a failure here must not break tool execution,
    which is the whole point of the tool call. Only text blocks are touched, and
    only those over the cap, so ordinary results are returned byte-identical.
    """
    try:
        result = getattr(event, "result", None)
        if not isinstance(result, dict):
            return
        blocks = result.get("content")
        if not isinstance(blocks, list):
            return
        for block in blocks:
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if isinstance(text, str) and len(text) > _MAX_TOOL_RESULT_CHARS:
                block["text"] = text[:_MAX_TOOL_RESULT_CHARS] + _TRUNCATION_NOTE
                logger.warning(
                    "Capped a %d-char tool result to %d chars; an oversized result "
                    "makes the session memory write fail with 413 and loses the turn",
                    len(text), _MAX_TOOL_RESULT_CHARS,
                )
    except Exception as exc:  # noqa: BLE001 — see docstring
        logger.debug("tool-result cap skipped: %s", exc)


class HarnessHookProvider:
    """Strands ``HookProvider`` bridging native events to the hook engine.

    ``identity`` is an optional zero-arg callable returning a base context with
    ``session``/``actor`` sub-dicts to merge into every event context.
    """

    def __init__(self, engine: HookEngine, *, identity: Optional[Callable[[], dict]] = None):
        self._engine = engine
        self._identity = identity

    # ---- Strands HookProvider protocol ----
    def register_hooks(self, registry: Any, **_kwargs: Any) -> None:
        from strands.hooks.events import (
            AfterInvocationEvent,
            AfterToolCallEvent,
            BeforeInvocationEvent,
            BeforeToolCallEvent,
        )

        registry.add_callback(AfterToolCallEvent, self._on_after_tool)
        registry.add_callback(BeforeToolCallEvent, self._on_before_tool)
        registry.add_callback(BeforeInvocationEvent, self._on_session_start)
        registry.add_callback(AfterInvocationEvent, self._on_session_end)

    # ---- internals ----
    def _base(self) -> dict:
        if self._identity is None:
            return {}
        try:
            return self._identity() or {}
        except Exception:  # identity resolution must never break a turn
            logger.debug("hook identity resolver failed", exc_info=True)
            return {}

    def _fire(self, event_name: str, ctx: dict) -> None:
        try:
            self._engine.fire(event_name, ctx)
        except Exception:  # engine.fire already isolates, this is belt-and-suspenders
            logger.exception("HarnessHookProvider: fire(%s) failed", event_name)

    def _on_after_tool(self, event: Any) -> None:
        # Ledger FIRST: the render rule this fires checks the turn's own writes, so a file this
        # very call wrote (under any session-shaped prefix) must already be recorded.
        try:
            from media import turn_outputs
            tool_use = getattr(event, "tool_use", None) or {}
            if isinstance(tool_use, dict):
                turn_outputs.note_tool_result(tool_use.get("name", ""), tool_use.get("input"),
                                              getattr(event, "result", None))
        except Exception as exc:  # noqa: BLE001 - bookkeeping must never break a tool call
            logger.debug("turn output ledger skipped: %s", exc)
        self._fire("after_tool_result", ctx_after_tool_result(event, self._base()))
        _cap_tool_result_size(event)

    def _on_before_tool(self, event: Any) -> None:
        self._fire("before_tool", _ctx_before_tool(event, self._base()))

    def _on_session_start(self, event: Any) -> None:
        # FIRST, before any context building. _base() resolves actor/session and can
        # raise; if it did, the batch would never open and renders would fan out one
        # tab per tool result again. Opening first makes the batch independent of it.
        try:
            from .actions import open_render_batch
            open_render_batch()
        except Exception as exc:  # noqa: BLE001 - never break an invocation
            logger.debug("could not open render batch: %s", exc)
        try:
            from media import turn_outputs
            turn_outputs.open_turn()
        except Exception as exc:  # noqa: BLE001 - never break an invocation
            logger.debug("could not open the turn output ledger: %s", exc)
        ctx = {"event": "session_start"}
        ctx.update(self._base())
        self._fire("session_start", ctx)

    async def _on_session_end(self, event: Any) -> None:
        # The flush is in a FINALLY. Marking happens at buffer time, so anything that
        # prevents the flush leaves artifacts recorded-as-dispatched and never shown --
        # a silently blank Renderer, worse than the duplicate tabs this replaced. It
        # therefore must not depend on _base() succeeding or on user hooks behaving.
        #
        # ASYNC + OFFLOADED. Strands fires AfterInvocationEvent via
        # `invoke_callbacks_async` (awaited inside `stream_async`), so this callback may
        # be a coroutine. `flush_render_batch` calls `dispatch_render`, which BLOCKS on a
        # synchronous render-agent invoke (up to RENDER_DISPATCH_BUDGET_SECONDS, ~90s).
        # Running it inline on the event loop froze `main._progress_poller` for the whole
        # compose -- the render agent's own progress steps and the 30s keepalive could not
        # drain, so the user saw a dead gap after their answer and then a burst of steps at
        # the very end. `asyncio.to_thread` runs the blocking flush on a worker thread
        # (copying contextvars, so job/trace context is preserved) and yields the loop back
        # to the poller, which streams the render agent's live progress and keeps the SSE
        # warm. The turn still waits for the flush to finish (we await it) before the stream
        # ends, so the manifest is surfaced exactly as before -- same functionality, no
        # frozen wait. `progress_channel` is thread-safe and `_active_channel` is a module
        # global (not a contextvar), so pushing from the worker thread is safe.
        try:
            ctx = {"event": "session_end"}
            ctx.update(self._base())
            # Classify this turn's Citations and persist its delivered Hits BEFORE the session_end
            # rules fire, so the Moments_Rule (a session_end rule) always sees the TurnOutcome
            # (design Component 4, Req 2.5-2.7). A no-op off the Main_Agent.
            await self._verify_citations()
            # Fire user hooks BEFORE flushing, so a hook that adds an artifact this
            # turn still lands in the same tab.
            self._fire("session_end", ctx)
        finally:
            try:
                from .actions import flush_render_batch
                await asyncio.to_thread(flush_render_batch)
            except Exception as exc:  # noqa: BLE001 - see above
                logger.warning("render batch flush failed: %s", exc)

    async def _verify_citations(self) -> None:
        """Run turn-end citation verification off the event loop (design Component 4, Req 2.5-2.7).

        Builds the Session_Hit_Store (``DynamoSessionHitStore.from_env``, falling back to
        ``MemorySessionHitStore`` when no table is configured) and the AssetResolver (the local-dev
        factory of task 5.2, else the deployed ``AssetServiceResolver``), then runs
        ``turn_end.finish_turn`` through ``asyncio.to_thread`` — ``finish_turn`` makes boto3/Lambda
        calls that would stall ``_progress_poller`` and the 30 s keepalive if run on the loop, exactly
        like the render flush above.

        A no-op on every runtime but the Main_Agent: ``current_turn_context`` is set only by
        ``main.prepare_turn``, so on the Task_Agent (and any non-Projects turn) this returns before
        constructing a store or resolver. ``finish_turn`` itself never raises; this wrapper gives the
        store/resolver construction the same guarantee so verification can never break a turn.
        """
        try:
            from project_search.context import current_turn_context
        except Exception:  # noqa: BLE001 - project_search absent (e.g. a non-Projects runtime)
            return
        if current_turn_context.get() is None:
            return  # Task_Agent / non-Projects turn: nothing to verify
        try:
            from project_search import turn_end
            from project_search.assets import AssetServiceResolver, resolver_from_env
            from project_search.session_hits import (
                DynamoSessionHitStore,
                MemorySessionHitStore,
                SessionHitStoreError,
            )

            try:
                store = DynamoSessionHitStore.from_env()
            except SessionHitStoreError:
                store = MemorySessionHitStore()
            # resolver_from_env honors LOCAL_DEV_ASSET_RESOLVER locally and returns None in a
            # deployed env, where the deployed AssetServiceResolver is used.
            resolver = resolver_from_env(hit_store=store) or AssetServiceResolver()
            await asyncio.to_thread(turn_end.finish_turn, store=store, resolver=resolver)
        except Exception as exc:  # noqa: BLE001 - verification must never break a turn
            logger.warning("citation verification failed: %s", exc)
