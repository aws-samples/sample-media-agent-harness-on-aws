"""Deterministic resolution of a finished node/job output into a typed record.

WHY THIS EXISTS
---------------
``get_plan_results`` used to hand the orchestrator each node's RAW recorded
output: an ``s3://`` ref for an M2C node, or ``{"request_id": ...}`` for a fal
queue node. Its docstring made that a contract -- the agent should "feed any
s3:// refs it finds to read_result_files", and queue submissions were surfaced
"verbatim". The cost was paid on every retrieval:

  * one "retrieve the results" turn became 1-3 further tool calls PER NODE, so a
    four-node plan took ~38 steps and two minutes;
  * every one of those tool results passed the render hook, so session 842bb417
    produced 10 render manifests for 4 analyses, of which only 8 surfaced -- and
    the two dropped ones included the single richest view in the run
    (``video-annotations``, 5 identities, 40 face boxes);
  * three of the eight tabs that DID surface were empty ``generic-preview``
    manifests, because a reader and a presigned-url vendor were being treated as
    artifact producers;
  * worst, the model was left GUESSING endpoint ids. In one run it tried variant
    after variant of ``get_job_result`` and collected 404/405s, then reported the
    video "unreachable" -- when the rule for deriving that endpoint already
    existed server-side and had been fixed three separate times.

Resolution is not a judgement call, so it does not belong in a model turn. This
module does it in code, once, at settle -- off the interactive path, durable, and
re-fetchable without re-resolving.

WHERE IT RUNS
-------------
Shipped as a Lambda layer so ONE copy serves both consumers:

  * the plan chainer, at plan settle (``_mark_run_complete``), which already
    fires exactly once per plan for SUCCEEDED *and* FAILED and already holds
    every node's status;
  * the polling worker, at job terminal, so a lone async job with no plan
    produces the same artifact shape.

A layer rather than a copy in each bundle: both lambdas are packaged with
``lambda.Code.fromAsset(<dir>)`` and have no bundling step, so a shared file
would have to be duplicated -- and duplication is precisely how the endpoint rule
came to be fixed three times in three places. The module name is also deliberately
unique: the two bundles both already define ``handler`` and ``gateway_invoker``,
and a ``sys.path`` insert lets whichever imports first shadow the other.

THE CONTRACT
------------
``resolve_node(...)`` NEVER raises and NEVER returns None. A node whose
resolution fails comes back as ``kind="error"`` carrying the reason, because a
plan where three of four nodes worked must still produce a usable artifact --
and because today a failed plan yields nothing at all, which is how a failure
stays invisible.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Bound what we will pull into Lambda memory to inline as findings. Anything
# larger is referenced by url instead of embedded; the point of the artifact is to
# be small enough to fetch and render in one hop.
MAX_INLINE_FINDINGS_BYTES = 256 * 1024

_S3URI_RE = re.compile(r"^s3://([^/]+)/(.+)$")

# Any UUID in a key. Deliberately the SAME shape the render agent's identity rule matches
# (`render_agent._MEDIA_ID_UUID_RE`), and we take the LAST one for the same reason it does —
# so a mirror key and the render identity derived from it can never disagree.
_KEY_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _source_exec_uuid(src_key: str) -> str:
    """The producing execution's UUID as it appears in the SOURCE key, lowercased.

    ``micro-workflow/media-processing/<execUUID>/scene1_trim/clip.mp4`` -> ``<execuuid>``.
    Returns "" when the source carries no UUID (a gateway that doesn't scope by execution),
    which the caller treats as "keep the legacy plan/node layout".
    """
    found = _KEY_UUID_RE.findall(src_key or "")
    return found[-1].lower() if found else ""


def _source_parent_segment(src_key: str) -> str:
    """The source's immediate parent directory (``scene1_trim``), used ONLY to disambiguate
    two artifacts of the SAME execution that share a basename. Source-derived on purpose: the
    polling worker never sees a ``node_id``, so a node-based token would make the two resolvers
    compute different keys — which is the very duplication this module now avoids."""
    parts = [p for p in (src_key or "").split("/")[:-1] if p]
    return parts[-1] if parts else ""

_MEDIA_EXTS = {
    "video": (".mp4", ".mov", ".webm", ".mkv", ".m4v"),
    "audio": (".wav", ".mp3", ".flac", ".m4a", ".ogg", ".aac"),
    "image": (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"),
}

# Record kinds. `analysis` carries findings a template can draw; `media` carries
# something playable; `pending` is a handle that has not settled; `error` is a
# resolution failure that must not sink the whole aggregate.
KIND_ANALYSIS = "analysis"
KIND_MEDIA = "media"
KIND_PENDING = "pending"
KIND_ERROR = "error"


def parse_s3_uri(uri: Any) -> Optional[tuple]:
    """Parse ``s3://bucket/key`` into ``(bucket, key)``, else None."""
    if not isinstance(uri, str):
        return None
    match = _S3URI_RE.match(uri.strip())
    return (match.group(1), match.group(2)) if match else None


def media_kind_for(url: str) -> str:
    """Coarse media class for a url, or "" when it is not recognisable media."""
    lowered = (url or "").lower().split("?")[0]
    for kind, exts in _MEDIA_EXTS.items():
        if lowered.endswith(exts):
            return kind
    return ""


def queue_app_id(model: str, status_url: str = "") -> str:
    """The endpoint id the QUEUE addresses, which is not the full model path.

    Ported verbatim from ``task_agent._queue_app_id`` -- the rule that took three
    fixes to get right, and the one the model was left guessing at. fal's own
    status_url shows the form it expects, only the first two segments:

        model:      fal-ai/stable-audio-3/small/music/text-to-audio
        status_url: https://queue.fal.run/fal-ai/stable-audio-3/requests/<id>/status

    Sending the full model path produced "Status check failed (405)": the queue
    built a url that does not exist. So prefer parsing status_url, which is
    authoritative when supplied, and otherwise take the model's first two
    segments -- which is what status_url itself contains.
    """
    # Both markers must be present. Without the "/requests/" check a url like
    # "https://queue.fal.run/x/status" yielded "x/status" -- a parse that looked
    # successful and produced nonsense, which is worse than not parsing at all.
    if status_url and "queue.fal.run/" in status_url and "/requests/" in status_url:
        tail = status_url.split("queue.fal.run/", 1)[1]
        before = tail.split("/requests/", 1)[0].strip("/")
        if before:
            return before
    parts = [p for p in (model or "").split("/") if p]
    if len(parts) >= 2:
        return "/".join(parts[:2])
    return model or ""


def queue_handle(value: Any) -> Optional[dict]:
    """``{"job_id", "status_url", "endpoint_id"}`` if `value` is a queue submission.

    Mirrors ``task_agent._queue_handle``. The ENDPOINT is the discriminator, not
    status_url: requiring status_url missed seedance entirely (it returns
    request_id + model and no status_url), and requiring an explicit endpoint
    field missed stable-audio (which returns only ids and urls). Either one is
    sufficient evidence, and both feed the same derivation.
    """
    if not isinstance(value, dict):
        return None
    job_id = value.get("request_id") or value.get("job_id") or ""
    if not isinstance(job_id, str) or not job_id:
        return None
    endpoint_id = (
        value.get("endpoint_id") or value.get("model_id") or value.get("model") or ""
    )
    if not isinstance(endpoint_id, str):
        endpoint_id = ""
    status_url = value.get("status_url") or ""
    if not isinstance(status_url, str):
        status_url = ""
    if not endpoint_id and not status_url:
        return None
    return {
        "job_id": job_id,
        "status_url": status_url,
        "endpoint_id": queue_app_id(endpoint_id, status_url),
    }


def _urls_in(value: Any, out: list, path: tuple = ()) -> None:
    """Collect http(s) urls and s3 uris, skipping anything under an INPUT path.

    Path-awareness is not a nicety. A real M2C node output carries both, e.g.
    t3_detect_faces from session 842bb417:

        .output.s3uri -> .../face-recognition/<exec>/     (the analysis)
        .input.s3uri  -> .../proxy/transcoded.mp4         (the SOURCE video)

    A naive walk takes whichever media url it meets first and can surface the
    input as though the node had produced it -- which is precisely the "I don't
    need the source videos" complaint this change exists to fix.
    """
    if isinstance(value, str):
        text = value.strip()
        if text.startswith(("http://", "https://", "s3://")) and text not in out:
            out.append(text)
        return
    if isinstance(value, dict):
        for key, sub in value.items():
            lowered = str(key).lower()
            if lowered in _INPUT_KEYS or lowered.startswith("source"):
                continue
            _urls_in(sub, out, path + (lowered,))
        return
    if isinstance(value, list):
        for sub in value:
            _urls_in(sub, out, path)


# Keys whose subtree describes what went IN, never what came out.
_INPUT_KEYS = frozenset({"input", "inputs", "input_config", "source", "sources", "request"})

# Keys whose subtree holds the finished result of an async submission.
_RESULT_KEYS = ("result", "results", "output", "outputs")


def resolved_result_of(value: Any) -> Any:
    """The already-resolved result carried on a node value, or None.

    A settled queue node records the handle AND the answer together:

        {"status": "completed", "endpoint_id": "fal-ai/ffmpeg-api",
         "request_id": "019fe249-...", "result": {"video": {"url": "https://..."}}}

    Treating that as an unresolved handle and going to the registry reported
    `pending` for three nodes whose urls were sitting in the value -- so the
    resolved subtree is checked FIRST.
    """
    if not isinstance(value, dict):
        return None
    for key in _RESULT_KEYS:
        sub = value.get(key)
        if sub in (None, "", {}, []):
            continue
        urls: list = []
        _urls_in(sub, urls)
        if any(media_kind_for(u) for u in urls):
            return sub
    return None


def artifacts_from(value: Any, *, role: str = "output") -> list:
    """Typed artifact records for every media url reachable from `value`.

    Non-media urls are deliberately dropped rather than passed through: a status
    url or an api endpoint is not something a template can display, and feeding
    those to the renderer is what produced empty players.
    """
    found: list = []
    _urls_in(value, found)
    artifacts = []
    for url in found:
        kind = media_kind_for(url)
        if not kind:
            continue
        artifacts.append({"url": url, "media_kind": kind, "role": role})
    return artifacts


def _output_prefix(value: Any) -> str:
    """The s3 prefix a node's result files live under, for a human to follow.

    M2C reports ``output.s3uri`` as a PREFIX rather than a file, so it is kept as
    a pointer on the record instead of being read as JSON.
    """
    if not isinstance(value, dict):
        return ""
    out = value.get("output")
    if isinstance(out, dict) and isinstance(out.get("s3uri"), str):
        return out["s3uri"]
    return ""


# Public name for the chainer / poller (the prefix a node's result files live under, or "").
output_prefix = _output_prefix


def output_s3_ref(value: Any) -> Optional[tuple]:
    """The ``s3uri`` describing this node's OUTPUT, as ``(bucket, key)``.

    Real M2C node values nest s3uris under both sides of the job. Session
    842bb417's t3_detect_faces carried:

        .output.s3uri -> .../face-recognition/<exec>/    (the analysis)
        .input.s3uri  -> .../proxy/transcoded.mp4        (the SOURCE video)

    Taking the first s3uri encountered surfaced the input as the result. Output
    keys are searched first, input subtrees are never searched, and a top-level
    s3uri is accepted last so simpler shapes keep working.
    """
    if not isinstance(value, dict):
        return None
    for key in _RESULT_KEYS:
        sub = value.get(key)
        if isinstance(sub, dict):
            ref = parse_s3_uri(sub.get("s3uri"))
            if ref:
                return ref
    ref = parse_s3_uri(value.get("s3uri"))
    if ref:
        return ref
    # Last resort: any s3uri that is not under an input subtree.
    found: list = []
    _urls_in(value, found)
    for candidate in found:
        ref = parse_s3_uri(candidate)
        if ref:
            return ref
    return None


# The result files a view actually consumes, most useful first. Taken from what
# the render skills declare they read (render-face-people reads facemetadata.json,
# render-face-recognition reads face_tracks/identity_clusters, and so on), so this
# list and the templates cannot drift apart silently.
#
# Ordered by ANSWER DENSITY, not just relevance. facemetadata.json is 29KB and
# carries stats + named people; facegroup.json repeats the same people in 229KB
# of per-frame detail, so it sits below the small files it duplicates.
_CANONICAL_RESULT_FILES = (
    "facemetadata.json",
    "shots.json",
    "speaker_manifest.json",
    "identity_clusters.json",
    "face_clusters.json",
    "recurrence.json",
    "global_speakers.json",
    "final_transcript.json",
    "face_tracks.json",
    "facegroup.json",
    "probe.json",
)

# Files that are READABLE findings (so "what codec / how many audio tracks" is answered from the
# aggregate without downloading the media) but are NOT render deliverables -- they must never
# become a render tab. probe.json is the source media spec: valuable to read, pointless to render.
_FINDINGS_ONLY_BASENAMES = {"probe.json"}

_CANONICAL_BASENAMES = {f.lower() for f in _CANONICAL_RESULT_FILES}

# Matching is a pure basename allowlist. An earlier path-based exclusion list
# dropped anything under /embeddings/ -- which is where shot segmentation actually
# writes shots.json and recurrence.json, so the one node whose answer is a shot
# COUNT came back with nothing but probe.json. Scratch files (seg0000_win0001.json,
# segment_0000_pts.json) are excluded by simply not being on the list.

# A result file bigger than this is referenced, not embedded. 96KB admits
# shots.json (69KB) and facemetadata.json (29KB) while keeping facegroup.json
# (229KB) out.
MAX_INLINE_FILE_BYTES = 96 * 1024

# Total inline budget for one plan's artifact. The point of aggregating is an
# object small enough to fetch and render in one hop; reading three files per node
# without a ceiling produced a 340KB artifact, which defeats that.
MAX_TOTAL_INLINE_BYTES = 192 * 1024

# How many result files to inline per node.
MAX_RESULT_FILES_PER_NODE = 2


def canonical_result_refs(value: Any, *, limit: Optional[int] = MAX_RESULT_FILES_PER_NODE) -> list:
    """``(bucket, key, basename)`` for the recognized deliverable result files.

    M2C records the real outputs as named ``*_key`` fields inside ``data``
    (``facemetadata_key``, ``speaker_manifest_key``, ``final_transcript_key``)
    while ``output.s3uri`` is only the prefix they sit under. Inlining ``data``
    itself gave 25KB of INPUT CONFIGURATION -- keys like ``frames_to_shots_cfg``
    and ``is_large_lambda`` -- so the agent still had to go fetch the answers, and
    the whole point of aggregating was lost.

    ``limit`` caps how many are returned (default 2) -- appropriate for INLINING,
    which costs tokens. Pass ``limit=None`` to get every present deliverable, which
    is what the artifact (render URL) surfacing wants: a URL is cheap, and capping
    it dropped the box-bearing ``face_tracks.json`` (9th in the canonical order)
    so the face render never received boxes and fell back to a boxless template.
    """
    prefix = _output_prefix(value)
    parsed = parse_s3_uri(prefix)
    if not parsed:
        return []
    bucket = parsed[0]

    found: dict = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, sub in node.items():
                if (
                    isinstance(sub, str)
                    and str(key).lower().endswith("_key")
                    and sub.endswith(".json")
                ):
                    found.setdefault(sub.rsplit("/", 1)[-1], sub)
                else:
                    walk(sub)
        elif isinstance(node, list):
            for sub in node:
                walk(sub)

    walk(value.get("data") if isinstance(value, dict) else None)

    ordered = []
    for name in _CANONICAL_RESULT_FILES:
        if name in found:
            ordered.append((bucket, found[name], name))
    return ordered if limit is None else ordered[:limit]


def source_media_of(value: Any) -> list:
    """The media an analysis DESCRIBES, for overlay views.

    This is the one place the input side is read on purpose. Everywhere else it is
    skipped, because surfacing the input as a node's output is what put the
    untouched source video in its own render tab. But a face `video-annotations`
    view needs a canvas to draw 40 boxes on, and dispatch_render already has the
    right channel for it -- `source_urls`, which is presigned under a separate
    prompt heading and excluded from the dedup key so it never changes render
    eligibility.

    Prefers the analysis's own input (usually the proxy that was actually
    analysed, so timestamps line up) over any other media reachable from the value.
    """
    if not isinstance(value, dict):
        return []
    out: list = []
    for key in ("input", "inputs", "input_config", "source"):
        sub = value.get(key)
        if not isinstance(sub, (dict, list)):
            continue
        found: list = []
        _urls_in_unfiltered(sub, found)
        for url in found:
            if media_kind_for(url) in ("video", "audio") and url not in out:
                out.append(url)
        if out:
            break
    return out[:1]


def _urls_in_unfiltered(value: Any, out: list) -> None:
    """`_urls_in` without the input-skipping, for reading the input on purpose."""
    if isinstance(value, str):
        text = value.strip()
        if text.startswith(("http://", "https://", "s3://")) and text not in out:
            out.append(text)
        return
    if isinstance(value, dict):
        for sub in value.values():
            _urls_in_unfiltered(sub, out)
        return
    if isinstance(value, list):
        for sub in value:
            _urls_in_unfiltered(sub, out)


def media_outputs_of(value: Any) -> list:
    """``(bucket, key, basename)`` for MEDIA files a node produced.

    Not every analysis result is JSON. Audio stem separation's deliverables are
    three WAVs -- ``speech_key``, ``music_key``, ``sfx_key`` under
    ``audiostem_sep_output`` -- and because canonical_result_refs only accepted
    ``.json`` it found nothing, emitted no artifacts, and the stem node produced no
    render tab at all while its siblings each got one.

    Only keys under a subtree whose name mentions OUTPUT count. There is no
    fallback, and that is deliberate: hedging with "any media key we can find" made
    diarization surface its own INPUT (``data.audio_key`` is the 16kHz mono file fed
    in) and shot segmentation surface per-segment scratch (``segment_0000.mp4``,
    ``transcoded.mp4``) as though those were products. A node whose deliverables are
    JSON has no media output, and its analysis files render on their own.
    """
    prefix = _output_prefix(value)
    parsed = parse_s3_uri(prefix)
    if not parsed or not isinstance(value, dict):
        return []
    bucket = parsed[0]

    found: dict = {}

    def walk(node: Any, under_output: bool) -> None:
        if isinstance(node, dict):
            for key, sub in node.items():
                lowered = str(key).lower()
                if lowered in _INPUT_KEYS or lowered == "usage_tracking":
                    continue
                if (
                    under_output
                    and isinstance(sub, str)
                    and lowered.endswith("_key")
                    and media_kind_for(sub)
                ):
                    found.setdefault(sub.rsplit("/", 1)[-1], sub)
                else:
                    walk(sub, under_output or "output" in lowered)
        elif isinstance(node, list):
            for sub in node:
                walk(sub, under_output)

    walk(value.get("data"), False)
    return [(bucket, key, name) for name, key in sorted(found.items())][:MAX_MEDIA_OUTPUTS_PER_NODE]


# Audio stem separation emits three (speech/music/sfx); a cap keeps a pathological
# node from filling the artifact with per-segment scratch media.
MAX_MEDIA_OUTPUTS_PER_NODE = 6

# Cap on producer-declared outputs carried per node (M2C declares one to a handful).
MAX_DECLARED_OUTPUTS_PER_NODE = 12


def _status_body(status: Any) -> Any:
    """The parsed body of an M2C status reply (MCP envelope or already-parsed dict)."""
    if not isinstance(status, dict):
        return status
    structured = status.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    content = status.get("content")
    if isinstance(content, list):
        for block in content:
            if (isinstance(block, dict) and block.get("type") == "text"
                    and isinstance(block.get("text"), str)):
                try:
                    parsed = json.loads(block["text"])
                except (ValueError, TypeError):
                    continue
                if isinstance(parsed, dict):
                    return parsed
    return status


# Declared outputs that are M2C bookkeeping, not deliverables: the enrichment status tool lists
# window_manifest.json (its internal windowing plan) and usage_tracking.json (cost accounting)
# beside shots/scenes, and conversation-analysis lists chunk_manifest.json (its chunking plan) beside
# conversation_analysis.json (staging session 00ad3584). Listed to Phase 3 prompts and auto-imported,
# they became project analysis assets nobody asked for. Matched by label OR basename so a relabel does
# not let them back in.
_BOOKKEEPING_OUTPUT_LABELS = frozenset({"usage", "window-manifest", "window_manifest",
                                        "chunk-manifest", "chunk_manifest"})
_BOOKKEEPING_OUTPUT_BASENAMES = frozenset({"usage_tracking.json", "window_manifest.json",
                                           "chunk_manifest.json"})


def _declared_entries(outputs: Any) -> list:
    """``[{label, uri}]`` entries of an ``outputs`` list whose uri is an ``s3://`` OBJECT."""
    out: list = []
    if not isinstance(outputs, list):
        return out
    for o in outputs:
        if not isinstance(o, dict):
            continue
        label, uri = o.get("label"), o.get("uri")
        if (isinstance(label, str) and label and parse_s3_uri(uri)
                and not str(uri).rstrip().endswith("/")
                and label.strip().lower() not in _BOOKKEEPING_OUTPUT_LABELS
                and str(uri).split("?", 1)[0].rsplit("/", 1)[-1].lower()
                not in _BOOKKEEPING_OUTPUT_BASENAMES):
            entry = {"label": label, "uri": uri}
            if entry not in out:
                out.append(entry)
    return out[:MAX_DECLARED_OUTPUTS_PER_NODE]


def merge_status_outputs(sfn_value: Any, status: Any) -> Any:
    """Fold an M2C status reply's DECLARED outputs into a Step Functions execution output.

    Some M2C state machines (``shot-metadata-enrichment``, ``conversation-analysis``) end with an
    execution output of only ``{"uuid", "total_estimated_cost"}``: their files are declared solely
    by the status tool (``<svc>___get_job_status`` -> ``outputs[{label, uri}]`` +
    ``output_s3_prefix``), the same contract project ingestion reads. Stored verbatim, that value
    names no file and no prefix, so every downstream resolver found nothing and Phase 3 report nodes
    guessed keys (staging session b8a59e30).

    Copies the status ``outputs`` (string ``label`` + ``s3://`` ``uri`` only) into
    ``value["outputs"]`` and sets ``value["output"]["s3uri"]`` from ``output_s3_prefix`` when the
    value has no output prefix. Returns ``sfn_value`` unchanged when the status declares nothing.
    Never raises, never mutates its inputs.
    """
    body = _status_body(status)
    if not isinstance(sfn_value, dict) or not isinstance(body, dict):
        return sfn_value
    declared = _declared_entries(body.get("outputs"))
    if not declared:
        return sfn_value
    merged = dict(sfn_value)
    merged["outputs"] = declared
    prefix = body.get("output_s3_prefix")
    if not _output_prefix(merged) and isinstance(prefix, str) and parse_s3_uri(prefix):
        out = merged.get("output")
        merged["output"] = {**(out if isinstance(out, dict) else {}), "s3uri": prefix}
    return merged


def declared_output_refs(value: Any) -> list:
    """``(bucket, key, "<basename> — <label>")`` for every output the producer DECLARED.

    No basename allowlist: unlike files found by listing a prefix (which include scratch), an entry
    in ``outputs[]`` is a deliverable by the producer's own contract -- ``shot_metadata.jsonl`` and
    ``conversation_analysis.json`` are not on ``_CANONICAL_RESULT_FILES`` and must not be dropped.
    """
    if not isinstance(value, dict):
        return []
    refs: list = []
    for entry in _declared_entries(value.get("outputs")):
        bucket, key = parse_s3_uri(entry["uri"])
        refs.append((bucket, key, f'{key.rsplit("/", 1)[-1]} — {entry["label"]}'))
    return refs


def _declared_source(value: Any) -> Optional[dict]:
    """The dict carrying declared ``outputs``: the value itself, or the poller's wrapped
    ``executionOutput``. None when neither declares anything."""
    if not isinstance(value, dict):
        return None
    if _declared_entries(value.get("outputs")):
        return value
    inner = value.get("executionOutput")
    if isinstance(inner, dict) and _declared_entries(inner.get("outputs")):
        return inner
    return None


def _declared_output_artifacts(value: Any) -> list:
    """Artifacts for the declared outputs: media -> role=output, anything else -> data/analysis."""
    arts = []
    for bucket, key, _name in declared_output_refs(value):
        uri = f"s3://{bucket}/{key}"
        kind = media_kind_for(key)
        base = key.rsplit("/", 1)[-1]
        if kind:
            arts.append({"url": uri, "media_kind": kind, "role": "output", "name": base})
        else:
            arts.append({"url": uri, "media_kind": "data", "role": "analysis", "name": base})
    return arts


def _record(
    node_id: str,
    status: str,
    kind: str,
    *,
    artifacts: Optional[list] = None,
    findings: Any = None,
    error: str = "",
    handle: Optional[dict] = None,
) -> dict:
    """One node's resolved record. Shape is the renderer's stable contract."""
    return {
        "node_id": node_id,
        "status": status,
        "kind": kind,
        "artifacts": artifacts or [],
        "findings": findings,
        "error": error,
        "handle": handle or {},
    }


def _enumerate_prefix_media_outputs(bucket: str, prefix: str, list_prefix) -> list:
    """List an M2C output PREFIX and return its renderable media files as role=output artifacts.

    M2C reports ``output.s3uri`` as a prefix (e.g. ``.../media-processing/<exec>/``); the concrete
    products (``transcoded.mp4``, an extracted ``.wav``) live under it. Skips input/source-named
    objects; returns ``[]`` when nothing renderable is found or no lister is wired.
    """
    if list_prefix is None or not bucket or not prefix:
        return []
    pfx = prefix if prefix.endswith("/") else prefix + "/"
    try:
        keys = list_prefix(bucket, pfx) or []
    except Exception:  # noqa: BLE001 - never fail a node over listing
        return []
    arts, seen = [], set()
    for k in keys:
        name = k.rsplit("/", 1)[-1]
        if not name or name in seen or any(tok in k.lower() for tok in _INPUT_KEYS):
            continue
        kind = media_kind_for(k)
        if kind:
            seen.add(name)
            arts.append({"url": f"s3://{bucket}/{k}", "media_kind": kind,
                         "role": "output", "name": name})
    return arts[:MAX_MEDIA_OUTPUTS_PER_NODE]


def _enumerate_prefix_canonical_json(bucket: str, prefix: str, list_prefix) -> list:
    """List an output PREFIX and return recognized CANONICAL JSON deliverables as role=analysis.

    Some M2C nodes (shot-segmentation especially) report only ``output.s3uri`` (a prefix) with NO
    ``data.*_key`` naming the deliverable, so canonical_result_refs (which walks ``data``) finds
    nothing and the analysis JSON (``shots.json``) never reaches the renderer — the node then renders
    its INPUT source video as a plain track instead of shot markers. Enumerate the prefix and surface
    the recognized canonical deliverables (allowlist), dropping scratch + findings-only files."""
    if list_prefix is None or not bucket or not prefix:
        return []
    pfx = prefix if prefix.endswith("/") else prefix + "/"
    try:
        keys = list_prefix(bucket, pfx) or []
    except Exception:  # noqa: BLE001 - never fail a node over listing
        return []
    arts, seen = [], set()
    for k in keys:
        base = k.rsplit("/", 1)[-1].lower()
        if base in seen or base not in _CANONICAL_BASENAMES or base in _FINDINGS_ONLY_BASENAMES:
            continue
        seen.add(base)
        arts.append({"url": f"s3://{bucket}/{k}", "media_kind": "data",
                     "role": "analysis", "name": base})
    return arts[:MAX_MEDIA_OUTPUTS_PER_NODE]


def _canonical_analysis_artifacts(value: Any, list_prefix) -> list:
    """role=analysis artifacts for the canonical JSON deliverables of a node.

    Prefers the deliverables NAMED in ``data.*_key`` (canonical_result_refs). When ``data`` names
    none — the shot-segmentation shape, whose ``data`` is only config and whose ``shots.json`` sits
    under ``output.s3uri`` with nothing naming it — enumerate the output prefix instead, so the
    analysis JSON still reaches the renderer (video-markers/video-annotations) rather than the node
    falling back to rendering its INPUT source video as a plain track (session a314ed42)."""
    arts = [
        {"url": f"s3://{b}/{k}", "media_kind": "data", "role": "analysis", "name": name}
        for b, k, name in canonical_result_refs(value, limit=None)
        if name.lower() not in _FINDINGS_ONLY_BASENAMES
    ]
    if arts:
        return arts
    ref = parse_s3_uri(_output_prefix(value))
    if ref:
        return _enumerate_prefix_canonical_json(ref[0], ref[1], list_prefix)
    return []


def _media_output_artifacts(value: Any, list_prefix) -> list:
    """role=output media artifacts for a `data`-shape node.

    Prefers the ``data.*_key`` media outputs (stem WAVs, etc.). When there are none — the
    ``process_media`` extract-audio/frames case, whose product sits under the ``output.s3uri``
    prefix with NO ``data`` media key — enumerate that prefix so the extracted media is surfaced
    (this is why session 7eaa1a75's audio never rendered: the input was shown as role=source and
    the audio output was dropped).
    """
    arts = [
        {"url": f"s3://{b}/{k}", "media_kind": media_kind_for(k) or "data",
         "role": "output", "name": nm}
        for b, k, nm in media_outputs_of(value)
    ]
    if arts:
        return arts
    ref = parse_s3_uri(_output_prefix(value))
    if not ref:
        return []
    return _enumerate_prefix_media_outputs(ref[0], ref[1], list_prefix)


def resolve_node(
    node_id: str,
    entry: Any,
    *,
    read_s3_json: Optional[Callable] = None,
    resolve_queue: Optional[Callable] = None,
    list_prefix: Optional[Callable] = None,
    budget: Optional[dict] = None,
) -> dict:
    """Resolve one node's recorded output into a typed record. Never raises.

    `read_s3_json(bucket, key) -> (parsed_or_none, size_bytes)` reads an s3 ref.
    `resolve_queue(handle) -> dict` fetches a queue job's result.
    `list_prefix(bucket, prefix) -> [keys]` enumerates the real objects under an output
        prefix, so a thin node value (prefix only) still yields its actual output files
        instead of a bare pointer (F4a). Injected, so this module stays boto3-free.

    Both are injected rather than imported so this module stays free of boto3 and
    of either lambda's gateway plumbing -- which is what makes it testable without
    AWS and safe to share.
    """
    try:
        if not isinstance(entry, dict):
            return _record(node_id, "unknown", KIND_ERROR, error="node entry was not an object")

        status = str(entry.get("status") or "pending")
        value = entry.get("value")

        if entry.get("error"):
            return _record(node_id, status, KIND_ERROR, error=str(entry.get("error"))[:2000])

        # Authoritative output list. The poller/chainer already resolved this job's outputs from
        # the GATEWAY response (renderable_artifact_urls) and put them on the row -- needing no
        # S3 access. Prefer that over enumerating the output prefix: the prefix lives in the M2C
        # proxy bucket, which the poller role cannot ListBucket (implicitDeny), so list_prefix
        # silently returned nothing and the aggregate came back empty (artifact_count 0) -- the
        # 39-step hand-fetch the user saw. When the caller hands us the resolved urls, use them.
        explicit = entry.get("artifact_urls")
        if isinstance(explicit, list) and explicit:
            arts = []
            for u in explicit:
                if not isinstance(u, str) or not u.strip():
                    continue
                if any(tok in u.lower() for tok in _INPUT_KEYS):
                    continue
                mk = media_kind_for(u)
                if mk:
                    # A playable media output (transcoded.mp4, audio.wav) is always a deliverable.
                    arts.append({"url": u, "media_kind": mk, "role": "output"})
                    continue
                # A data/JSON output is surfaced ONLY if it is a recognized deliverable
                # (_CANONICAL_RESULT_FILES). A media tool emits the deliverable (shots.json,
                # face_tracks.json) beside heavy scratch (seg*_win*, segment_*_pts, frame_index,
                # detection_plan, search_index_*); surfacing scratch turned it into extra tabs and
                # -- because the box-bearing face_tracks.json ranked below the 2-file findings cap
                # -- starved the face render of boxes. Filtering on the allowlist here surfaces the
                # deliverable (incl. face_tracks.json) and drops the scratch in one rule.
                base = u.split("?", 1)[0].rsplit("/", 1)[-1].lower()
                if base in _CANONICAL_BASENAMES and base not in _FINDINGS_ONLY_BASENAMES:
                    arts.append({"url": u, "media_kind": "data", "role": "analysis"})
            if arts:
                return _record(node_id, status, KIND_MEDIA, artifacts=arts)

        # An error can be recorded INSIDE the value while the node itself is marked
        # completed -- plan-dogvideo-merge's t3_merge_submit was status "completed"
        # carrying {"error": "Missing resolved video output URL from t1_video_gen"}.
        # Read as findings, that surfaced a failure as though it were a result.
        if isinstance(value, dict) and value.get("error") and len(value) <= 2:
            return _record(node_id, status, KIND_ERROR, error=str(value["error"])[:2000])

        # 0. Producer-DECLARED outputs (an M2C status reply's outputs[] merged into the value by
        #    merge_status_outputs). They are the deliverables by contract, so they are emitted as
        #    artifacts directly -- a data-shape value keeps the richer step 3 below.
        #    The poller's lone-job payload wraps the execution output one level down
        #    (``{status, error, executionOutput}``), so look there too -- otherwise the last-resort
        #    URL scrape below picks the declared .jsonl and fails reading it as JSON.
        declared_src = _declared_source(value)
        if declared_src is not None and not isinstance(declared_src.get("data"), (dict, list)):
            declared = _declared_output_artifacts(declared_src)
            if declared:
                media_only = all(a["role"] == "output" for a in declared)
                return _record(
                    node_id, status, KIND_MEDIA if media_only else KIND_ANALYSIS,
                    artifacts=declared,
                    findings=None if media_only else declared_src,
                    handle={"output_prefix": _output_prefix(declared_src),
                            "result_files": [a["name"] for a in declared]},
                )

        # 1. Already resolved? A settled queue node records the handle AND the
        #    answer together, so look at the answer before chasing the handle.
        settled = resolved_result_of(value)
        if settled is not None:
            arts = artifacts_from(settled)
            if arts:
                return _record(
                    node_id, status, KIND_MEDIA,
                    artifacts=arts,
                    handle=queue_handle(value) or {},
                )

        # 2. A queue handle. Resolve it here so the model never guesses an endpoint.
        handle = queue_handle(value)
        request_id = ""
        if isinstance(value, dict):
            candidate = value.get("request_id") or value.get("job_id") or ""
            request_id = candidate if isinstance(candidate, str) else ""
        # A bare {"request_id": ...} carries no endpoint, so queue_handle declines
        # it -- but the REGISTRY row is keyed by that id and holds both the
        # endpoint and the resolved urls. Declining to look was how a video fal
        # had already produced came back as findings=['request_id'].
        if handle is None and request_id:
            handle = {"job_id": request_id, "status_url": "", "endpoint_id": ""}
        if handle:
            if resolve_queue is None:
                return _record(
                    node_id, status, KIND_PENDING,
                    handle=handle,
                    error="queue handle not resolved: no resolver wired",
                )
            try:
                resolved = resolve_queue(handle)
            except Exception as exc:  # noqa: BLE001 - see module docstring
                return _record(
                    node_id, status, KIND_ERROR, handle=handle,
                    error=f"queue resolution failed: {exc}"[:2000],
                )
            if not isinstance(resolved, dict) or not resolved:
                return _record(
                    node_id, status, KIND_PENDING, handle=handle,
                    error="queue job has not produced a result yet",
                )
            arts = artifacts_from(resolved)
            return _record(
                node_id, status,
                KIND_MEDIA if arts else KIND_ANALYSIS,
                artifacts=arts,
                findings=None if arts else resolved,
                handle=handle,
            )

        # 3. The M2C shape: the real outputs are named *_key fields inside `data`,
        #    while output.s3uri is only the prefix they live under. Read the
        #    canonical ones so `findings` carries ANSWERS (shot counts, identities,
        #    speakers) rather than the input configuration `data` is mostly made of.
        if isinstance(value, dict) and isinstance(value.get("data"), (dict, list)):
            refs = canonical_result_refs(value)
            files: dict = {}
            problems = []
            remaining = MAX_TOTAL_INLINE_BYTES if budget is None else budget.get(
                "remaining", MAX_TOTAL_INLINE_BYTES
            )
            if refs and read_s3_json is not None:
                for bucket, key, name in refs:
                    if remaining <= 0:
                        problems.append(f"{name}: inline budget exhausted")
                        continue
                    try:
                        parsed, size = read_s3_json(
                            bucket, key, max_bytes=min(MAX_INLINE_FILE_BYTES, remaining)
                        )
                    except Exception as exc:  # noqa: BLE001 - see module docstring
                        problems.append(f"{name}: {exc}")
                        continue
                    if parsed is None:
                        problems.append(f"{name}: {size} bytes, referenced not inlined")
                        continue
                    files[name] = parsed
                    remaining -= size or 0
                if budget is not None:
                    budget["remaining"] = remaining
            return _record(
                node_id, status, KIND_ANALYSIS,
                # The render path is URL-BASED: dispatch_render takes urls, and the
                # render skills pick a template by fetching them. Inlining findings
                # served the agent and left the renderer with nothing, so a plan of
                # four analyses produced one duplicated video and an empty
                # generic-preview instead of face/shot/speaker timelines.
                #
                # So the canonical files are emitted as artifacts TOO, with
                # role="analysis" -- the same files whose contents are inlined
                # above. The agent reads `findings`; the renderer reads these.
                #
                # role="source" is the media the analysis DESCRIBES. dispatch_render
                # already has a channel for exactly this (`source_urls`: "the
                # original video behind a detection result ... so the Render Agent
                # can compose an overlay view", excluded from the dedup key), which
                # is what the face video-annotations view draws boxes on.
                artifacts=(
                    _canonical_analysis_artifacts(value, list_prefix)
                    + _media_output_artifacts(value, list_prefix)
                    + [
                        {"url": src, "media_kind": media_kind_for(src) or "video",
                         "role": "source"}
                        for src in source_media_of(value)
                    ]
                ),
                # Fall back to the raw data only when no result file could be read,
                # so a shape we do not recognise still says something.
                findings=files or value.get("data"),
                error="; ".join(problems)[:2000] if problems and not files else "",
                handle={
                    "output_prefix": _output_prefix(value),
                    "result_files": [name for _, _, name in refs],
                },
            )

        # 4. An s3 ref. Read small JSON inline as findings; treat media as an artifact.
        ref = parse_s3_uri(value if isinstance(value, str) else None)
        if ref is None:
            ref = output_s3_ref(value)
        if ref is not None:
            bucket, key = ref
            if media_kind_for(key):
                return _record(
                    node_id, status, KIND_MEDIA,
                    artifacts=[{
                        "url": f"s3://{bucket}/{key}",
                        "media_kind": media_kind_for(key),
                        "role": "output",
                    }],
                )
            # A PREFIX, not a file. M2C reports output.s3uri as a prefix (e.g.
            # .../media-processing/<exec>/), and when the recorded value is thin -- no
            # data.*_key file list for canonical_result_refs to inline -- that prefix
            # reached here and was read as an object. HeadObject on a prefix 403s, so the
            # node was recorded kind=error, artifact_count 0, and get_plan_results returned
            # nothing -- which is why retrieving a single proxy job cost 32 hand-fetch steps.
            # A prefix is never JSON: record it as a pointer the render/expand path can
            # enumerate, instead of failing the node.
            if key.endswith("/") or "." not in key.rsplit("/", 1)[-1]:
                prefix = key if key.endswith("/") else key + "/"
                # F4a: enumerate the prefix into REAL output artifacts when a lister is wired,
                # so get_plan_results returns the actual files instead of a bare pointer (which
                # cost the 32-step hand-fetch). Media files become output artifacts; obvious
                # scratch/input-named keys are skipped. Falls back to the pointer if listing
                # yields nothing or no lister is available.
                if list_prefix is not None:
                    try:
                        keys = list_prefix(bucket, prefix) or []
                    except Exception:  # noqa: BLE001 - never fail a node over listing
                        keys = []
                    arts = []
                    seen_names: set = set()
                    for k in keys:
                        name = k.rsplit("/", 1)[-1]
                        low = k.lower()
                        if not name or name in seen_names:
                            continue
                        if any(tok in low for tok in _INPUT_KEYS):
                            continue  # skip input/source-named objects
                        kind = media_kind_for(k)
                        if kind:
                            seen_names.add(name)
                            arts.append({"url": f"s3://{bucket}/{k}", "media_kind": kind,
                                         "role": "output"})
                    if arts:
                        return _record(
                            node_id, status, KIND_MEDIA,
                            artifacts=arts[:MAX_MEDIA_OUTPUTS_PER_NODE],
                            handle={"output_prefix": f"s3://{bucket}/{key}"},
                        )
                return _record(
                    node_id, status, KIND_MEDIA,
                    handle={"output_prefix": f"s3://{bucket}/{key}"},
                )
            if read_s3_json is None:
                return _record(
                    node_id, status, KIND_ERROR,
                    error="s3 ref not resolved: no reader wired",
                )
            try:
                parsed, size = read_s3_json(bucket, key)
            except Exception as exc:  # noqa: BLE001 - see module docstring
                return _record(
                    node_id, status, KIND_ERROR,
                    error=f"could not read s3://{bucket}/{key}: {exc}"[:2000],
                )
            if parsed is None:
                return _record(
                    node_id, status, KIND_ERROR,
                    error=(
                        f"s3://{bucket}/{key} is {size} bytes, over the "
                        f"{MAX_INLINE_FINDINGS_BYTES} inline limit"
                        if size and size > MAX_INLINE_FINDINGS_BYTES
                        else f"s3://{bucket}/{key} was not readable json"
                    ),
                )
            return _record(
                node_id, status, KIND_ANALYSIS,
                artifacts=artifacts_from(parsed),
                findings=parsed,
            )

        # 5. Already resolved, or a plain value. Media becomes artifacts; the rest
        #    is findings the summariser can read directly.
        arts = artifacts_from(value)
        if arts:
            return _record(node_id, status, KIND_MEDIA, artifacts=arts)
        if value in (None, "", {}, []):
            return _record(node_id, status, KIND_PENDING, error="node recorded no output")
        return _record(node_id, status, KIND_ANALYSIS, findings=value)
    except Exception as exc:  # noqa: BLE001 - the whole point is never to raise
        logger.exception("resolve_node failed for %s", node_id)
        return _record(node_id, "unknown", KIND_ERROR, error=f"resolver crashed: {exc}"[:2000])


# Total files auto-imported per aggregate (deliverable JSON + media), so a pathological plan
# can't copy an unbounded number of objects into our bucket. Deliverables per node are already
# capped (canonical files + MAX_MEDIA_OUTPUTS_PER_NODE); this bounds the whole plan.
MAX_AUTO_IMPORTS = 24


# A projects-session OUTPUT key (asset-lineage Phase 3 (a)): ours and durable, like artifacts/. A copy of
# agent/src/projects/storage_layout.is_project_session_output_key, pinned by the shared fixture
# (agent/tests/fixtures/session_layout_cases.json, _tests/test_result_resolution.py).
_PROJECT_SESSION_OUTPUT_RE = re.compile(
    r"^projects/p[0-9a-f]{32}/sessions/[A-Za-z0-9][A-Za-z0-9._-]{7,127}/output/[^/\\][^\\]*$")
_PROJECT_SESSION_SHARED = frozenset({"external", "durable", "imports", "uploads", "m2c"})


def is_project_session_output_key(key) -> bool:
    if not isinstance(key, str) or ".." in key or key.endswith("/") or not _PROJECT_SESSION_OUTPUT_RE.match(key):
        return False
    return key.split("/")[3] not in _PROJECT_SESSION_SHARED


def make_s3_importer(s3_client, skills_bucket: str, artifacts_domain: str,
                     import_prefix: str = "artifacts/imports/") -> Callable:
    """Build an ``import_artifact(src_bucket, src_key, relpath) -> url|None``.

    Copies a producer artifact from a foreign bucket (the M2C proxy) into our
    CloudFront-backed bucket and returns a PERMANENT https url — so result
    aggregates surface durable links instead of the upstream's short-lived
    presigned/proxy refs (the "links dead after an hour" bug).

    Shared by the plan-chainer (plan settle) AND the polling worker (lone/direct
    job settle) so BOTH paths persist artifacts identically — previously only the
    chainer imported, so any direct-dispatch media job's links expired. Best-effort:
    returns None on any failure (caller keeps the original ref) and never raises.
    Skips the copy when the artifact is already in our bucket, just forming the
    CloudFront url for the existing key. Idempotent server-side copy.
    """
    import mimetypes

    def _import(src_bucket, src_key, relpath):
        if not skills_bucket or not artifacts_domain:
            return None
        if src_bucket == skills_bucket and (
                src_key.startswith("artifacts/") or is_project_session_output_key(src_key)):
            # Already under a durable session prefix (artifacts/, or a projects-session output) —
            # reuse in place.
            return f"https://{artifacts_domain}/{src_key}"
        # Otherwise COPY into artifacts/ even when the source is our OWN bucket: M2C writes
        # job outputs under an EPHEMERAL micro-workflow/ prefix that gets cleaned up, so a
        # bare CloudFront URL to that key 403s / NoSuchKeys once it's purged (observed:
        # shot-segmentation shots.json/recurrence.json became unreadable → the render agent
        # 403'd and the shots lane stalled). Copying makes the deliverable durable.
        dest_key = f"{import_prefix}{(relpath or '').lstrip('/')}"
        ct = mimetypes.guess_type(dest_key)[0] or "application/octet-stream"
        try:
            # The copy says where it came from (`x-amz-meta-origin`): our copy is for durable links and display,
            # but a tool of the producer (M2C reads only its own proxy bucket) must be handed the ORIGINAL — the
            # gateway call path swaps a copy for its origin (agent jobs/origin_refs). Without it, a scene
            # enrichment handed the workspace copy of shots.json failed "must be on the proxy bucket".
            s3_client.copy_object(Bucket=skills_bucket, Key=dest_key,
                                  CopySource={"Bucket": src_bucket, "Key": src_key},
                                  ContentType=ct, MetadataDirective="REPLACE",
                                  Metadata={"origin": f"s3://{src_bucket}/{src_key}"})
        except Exception:
            return None
        return f"https://{artifacts_domain}/{dest_key}"

    return _import


def _auto_import_deliverables(plan_id: str, records: list, import_artifact: Callable) -> None:
    """Rewrite deliverable artifact urls from foreign ``s3://`` refs to our CloudFront copies.

    Mutates ``records`` in place. Only role output/analysis (never role="source", which is the
    presigned overlay canvas), only ``s3://`` urls (an https url is already resolved/ours),
    capped at MAX_AUTO_IMPORTS. Best-effort: a copy failure leaves the original ``s3://`` ref, and
    the original is preserved on ``source_ref`` when a copy succeeds.
    """
    imported = 0

    # ONE durable copy per source object, whatever resolves it.
    #
    # This function runs in TWO Lambdas over the SAME artifact: the polling worker (lone job
    # settle) and the plan-chainer (plan settle). Both read the artifact while its url is still
    # the producer's ephemeral `s3://` ref, so both copy it — and they used to copy it to
    # DIFFERENT keys, because the destination was scoped by the RESOLVER:
    #   polling worker : artifacts/<sid>/output/<execUUID>/clip.mp4          (import_prefix)
    #   plan-chainer   : artifacts/<sid>/output/<planRunUUID>/<node>/clip.mp4 (relpath)
    # That stored every plan-produced clip twice AND — because the render identity is
    # `<last-UUID>/<basename>` (render_agent._canonical_source_identity) — gave ONE video TWO
    # identities: `<execUUID>/clip.mp4` and `<planRunUUID>/clip.mp4`. The clip's own tab was
    # rendered from one copy while later analyses of that clip ran on the other, so the analysis
    # lanes could never merge onto the clip's timeline (prod session c80b7144: a trimmed scene
    # whose face-detection and diarization each opened their own tab instead of folding in).
    #
    # The destination is now derived from the SOURCE, so both resolvers compute the same key,
    # the second copy is an idempotent overwrite of identical bytes, and one video has one
    # identity. Sources with no UUID keep the legacy plan/node layout (see _source_exec_uuid).
    #
    # Sibling artifacts that share a basename WITHIN one execution would collide on that shared
    # key, so they are disambiguated by the source's parent segment. Counting is keyed by
    # (source execution, basename) rather than basename alone: both resolvers see all of an
    # execution's artifacts, so they agree on the count — whereas the previous node-id-based
    # disambiguation was invisible to the polling worker and reintroduced the split key.
    _src_name_counts: dict = {}
    for _rec in records:
        for _art in _rec.get("artifacts", []):
            if _art.get("role") not in ("output", "analysis"):
                continue
            _ref = parse_s3_uri(_art.get("url"))
            if not _ref:
                continue
            _nm = _art.get("name") or _ref[1].rsplit("/", 1)[-1]
            _ck = (_source_exec_uuid(_ref[1]), _nm)
            _src_name_counts[_ck] = _src_name_counts.get(_ck, 0) + 1

    def _try_import(rec, art) -> None:
        nonlocal imported
        node_id = rec.get("node_id") or "node"
        ref = parse_s3_uri(art.get("url"))
        if not ref:
            return
        src_bucket, src_key = ref
        name = art.get("name") or src_key.rsplit("/", 1)[-1]
        src_uuid = _source_exec_uuid(src_key)
        # Two artifacts of the SAME execution sharing a basename would land on one key and the
        # second would overwrite the first. Disambiguate with the source's parent segment
        # (`scene1_trim-clip.mp4`), which both resolvers derive identically.
        if _src_name_counts.get((src_uuid, name), 0) > 1:
            parent = _source_parent_segment(src_key)
            if parent:
                name = f"{parent}-{name}"
        if node_id == plan_id:
            # A lone/direct job (polling worker) passes its execution ARN as BOTH plan_id and
            # node_id, and its import_prefix ALREADY scopes the output by that execution
            # (artifacts/<sid>/output/<uuid>/) — so the filename alone is the relpath. The old
            # "{plan_id}/{node_id}/{name}" doubled the ARN (colons and all) into the key.
            relpath = name
        elif src_uuid:
            # A plan node: scope by the SOURCE execution so this lands on the SAME key the
            # polling worker uses for the same object — one copy, one render identity.
            relpath = f"{src_uuid}/{name}"
        else:
            # No UUID to key on (a producer that doesn't scope by execution): keep the legacy
            # plan/node layout rather than inventing an opaque path for an unknown gateway.
            relpath = f"{plan_id}/{node_id}/{name}"
        try:
            new_url = import_artifact(src_bucket, src_key, relpath)
        except Exception as exc:  # noqa: BLE001 - a copy failure must never sink the aggregate
            # Never sinking the aggregate does not mean saying nothing. Degrading silently
            # leaves an undisplayable s3:// ref, which surfaces as a blank player + presigned
            # download and reads as a render bug rather than a failed copy.
            logger.warning(
                "Plan %s: failed to import %s/%s -> %s (%s); the artifact keeps its s3:// ref "
                "and will not display", plan_id, src_bucket, src_key, relpath, exc)
            new_url = None
        if new_url:
            art["source_ref"] = art["url"]
            art["url"] = new_url
            imported += 1

    # Import in PRIORITY order, not a single positional pass. role="output" is the media
    # the user actually asked for (a trimmed clip, a transcode); role="analysis" is
    # secondary (face crops, shot thumbnails). A single ordered pass let a flood of
    # analysis artifacts exhaust MAX_AUTO_IMPORTS BEFORE reaching the output, so the
    # requested media stayed an undisplayable s3:// ref (blank player + presigned
    # download). Importing every output first guarantees the primary deliverable is
    # converted to CloudFront even when analysis artifacts outnumber the cap. role="source"
    # (the presigned overlay canvas) is never imported.
    for role in ("output", "analysis"):
        for rec in records:
            for art in rec.get("artifacts", []):
                if imported >= MAX_AUTO_IMPORTS:
                    _log_cap_truncation(plan_id, records)
                    return
                if art.get("role") != role:
                    continue
                if not parse_s3_uri(art.get("url")):
                    continue
                _try_import(rec, art)


def _log_cap_truncation(plan_id: str, records: list) -> None:
    """Say what the cap dropped.

    A cap that truncates in silence reads as "we imported everything". Every artifact past
    MAX_AUTO_IMPORTS keeps its foreign ``s3://`` url, which the UI cannot display -- a blank
    player plus a presigned download -- and that is indistinguishable from a render bug unless
    the truncation is on the record. The COUNT is the actionable part.
    """
    remaining = [
        art.get("name") or art.get("url")
        for rec in records
        for art in rec.get("artifacts", [])
        if art.get("role") in ("output", "analysis")
        and parse_s3_uri(art.get("url"))          # still foreign => was not imported
    ]
    logger.warning(
        "Plan %s: auto-import cap MAX_AUTO_IMPORTS=%d reached; %d deliverable(s) left as "
        "undisplayable s3:// refs: %s", plan_id, MAX_AUTO_IMPORTS, len(remaining),
        ", ".join(str(r) for r in remaining[:10]) + (" ..." if len(remaining) > 10 else ""))


def build_aggregate(
    *,
    plan_id: str,
    status: str,
    nodes: dict,
    display_name: str = "",
    goal: str = "",
    read_s3_json: Optional[Callable] = None,
    resolve_queue: Optional[Callable] = None,
    list_prefix: Optional[Callable] = None,
    import_artifact: Optional[Callable] = None,
) -> dict:
    """The single artifact a settled plan (or job) publishes.

    One record per node, plus counts so a reader can describe the outcome without
    walking the list. Written even when the plan FAILED: a run where three of four
    nodes worked has three results worth showing, and the failure is the part the
    user most needs to see.

    ``import_artifact(src_bucket, src_key, relpath) -> url_or_none`` (injected, so this module
    stays boto3-free) copies a DELIVERABLE that lives in another bucket (the M2C proxy) into our
    own bucket and returns a permanent CloudFront url. Without it, deliverables come back as
    ``s3://`` refs the browser can't fetch — so metadata files couldn't be downloaded and audio
    couldn't play inline. Bounded and best-effort: only role output/analysis (never the source
    canvas), only ``s3://`` urls (https are already resolved/ours), capped at MAX_AUTO_IMPORTS,
    and a copy failure leaves the original ``s3://`` ref untouched.
    """
    # Shared across nodes so one verbose node cannot spend the whole artifact.
    budget = {"remaining": MAX_TOTAL_INLINE_BYTES}
    records = []
    for node_id in sorted(nodes.keys()):
        records.append(
            resolve_node(
                node_id,
                nodes.get(node_id),
                read_s3_json=read_s3_json,
                resolve_queue=resolve_queue,
                list_prefix=list_prefix,
                budget=budget,
            )
        )
    if import_artifact is not None:
        _auto_import_deliverables(plan_id, records, import_artifact)
    counts = {}
    for rec in records:
        counts[rec["kind"]] = counts.get(rec["kind"], 0) + 1
    return {
        "schema": "plan-result/v1",
        "plan_id": plan_id,
        "display_name": display_name,
        "goal": goal,
        "status": status,
        "counts": counts,
        "artifact_count": sum(len(r["artifacts"]) for r in records),
        "nodes": records,
    }


def aggregate_json(**kwargs) -> str:
    """`build_aggregate` serialised, with non-serialisable values stringified."""
    return json.dumps(build_aggregate(**kwargs), default=str)
