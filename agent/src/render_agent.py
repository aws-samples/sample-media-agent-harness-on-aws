"""Render Agent — specialized runtime for artifact analysis and manifest generation.

Follows the same BedrockAgentCoreApp pattern as task_agent.py but is dedicated
to media/artifact rendering. Loads only media-specific tools (media_probe,
peak_extractor, caption_parser, artifact) and uses render-* skills from
the SkillsManager to dispatch to the appropriate rendering strategy.

The pipeline orchestrates: probe → extract peaks → parse captions → detect clips
→ validate → assemble manifest → write to S3 → emit output_ref SSE event.

Enforces a 30-second total pipeline timeout and implements self-correction on
validation failure (one retry within 5 seconds).
"""

import asyncio
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Optional

import boto3
from strands import Agent, AgentSkills, Skill, tool
from strands.models import BedrockModel
import runtime_config_bootstrap  # noqa: F401,E402  — hydrate env before first-party imports (V2 1536B cap)
from model.factory import build_model
from model.defaults import model_for
from botocore.config import Config as BotoConfig
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from skills_manager import SkillsManager
from tools.media_probe import media_probe
import render.projection as _proj
import render.types as render_types
from render.skill_catalog import (  # the single skill-declaration reader (see module docstring)
    RENDER_SKILLS_DIR,
    _parse_skill_frontmatter,
    _discover_s3_render_skills,
    _discover_render_skills,
    select_render_skill_by_tool,
    deliverables_for_tool,
    _FEDERATED_SEP,
    reset_render_skill_cache,
)
from tools.mp4_duration import find_mvhd_duration as _find_mvhd
from tools.peak_extractor import peak_extractor
from tools.caption_parser import caption_parser, parse_caption_content
from task_agent import artifact  # Reuse the artifact tool from task_agent
from prompts.render import RENDER_SYSTEM_PROMPT

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()

# --- Environment configuration ---
REGION = os.environ.get("AWS_REGION", "us-west-2")
S3_BUCKET = os.environ.get("SKILLS_S3_BUCKET", "")
S3_PREFIX = os.environ.get("ARTIFACTS_PREFIX", "artifacts/")

# The Render Agent is PINNED to a specific model. Its work is largely
# deterministic — build_render_manifest does the projection; the LLM only selects URLs and
# a skill — so it does not need the reasoning tier, and it runs on every artifact. Overridable
# via RENDER_MODEL_ID but never takes the caller's orchestrator model pick.
RENDER_MODEL_ID = model_for("render")  # agent/src/model/models.json; no env override
ARTIFACTS_DOMAIN = os.environ.get("ARTIFACTS_DOMAIN", "")
_boto_cfg = BotoConfig(read_timeout=300, connect_timeout=10, retries={"max_attempts": 4, "mode": "standard"})

# --- S3 client (lazy init) ---
_s3 = boto3.client("s3", region_name=REGION) if S3_BUCKET else None


def _public_or_presigned_url(cli, bucket: str, key: str) -> str:
    """URL for an S3 object, preferring the permanent CloudFront (ArtifactsDomain) URL.

    Objects in the skills bucket under the ``artifacts/`` prefix are served permanently by
    the ArtifactsDomain CloudFront distribution (OAC origin = that bucket). Return that URL
    instead of presigning: a presigned URL is signed with the render role's TEMPORARY STS
    creds, whose session token expires (S3 ``ExpiredToken``) long before the ``ExpiresIn``
    window — which broke rendered HTML/games once the token lapsed (e.g. a document-viewer
    ``previewUrl``; session 46446dba). Presign ONLY as the fallback: no CloudFront domain
    (local dev), a non-artifacts key, or a different bucket (e.g. an M2C proxy object) that
    ArtifactsDomain does not front. Mirrors the same rule the UI proxy applies for uploads.
    """
    if ARTIFACTS_DOMAIN and bucket == S3_BUCKET and key.startswith(S3_PREFIX):
        return f"https://{ARTIFACTS_DOMAIN}/{key}"
    return cli.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=86400)

@tool
def validate_render_urls(chosen_json: str, candidates_json: str,
                         source_json: str = "", rendered_json: str = "") -> str:
    """Validate the artifact URLs you chose to render against the job's ACTUAL outputs.

    Call this AFTER you resolve which artifacts to render and BEFORE building the manifest. It
    confirms every url you picked is a real job output (not hallucinated), is not the input, and
    was not already rendered. Use its verdict: render only the `valid` urls.

    Args:
        chosen_json: JSON array of the urls you chose to render.
        candidates_json: JSON array of the job's real output urls (given to you as candidates).
        source_json: JSON array of the input/source urls (optional).
        rendered_json: JSON array of urls already rendered this session (optional).

    Returns a JSON object {"valid": [...], "rejected": [{"url","reason"}]}. Drop the rejected.
    """
    from render.review import validate_resolved_urls as _validate

    def _arr(s: str) -> list:
        if not s:
            return []
        try:
            v = json.loads(s) if isinstance(s, str) else s
            return v if isinstance(v, list) else []
        except (ValueError, TypeError):
            return []

    result = _validate(_arr(chosen_json), _arr(candidates_json),
                       source_urls=_arr(source_json), rendered_urls=_arr(rendered_json))
    return json.dumps(result)


@tool
def review_render_manifest(manifest_json: str, source_json: str = "") -> str:
    """Review a render manifest against the SOURCE DATA it was built from, and report problems.

    Call this AFTER composing a manifest and BEFORE finalizing. It is the correctness check for
    an agent-driven render: it catches a manifest that renders nothing, and one that dropped
    elements the data supports (e.g. source has face boxes but the manifest has no annotations).

    Args:
        manifest_json: the manifest you produced, as a JSON string.
        source_json: the source sidecar data, as a JSON string (an object mapping file name ->
            its parsed content), so the review can compare manifest against data. Optional.

    Returns:
        "OK" when the manifest is sound, else a newline-separated list of problems to FIX
        (e.g. take the box path, populate the missing lanes) before finalizing.
    """
    from render.review import review_manifest as _review

    try:
        manifest = json.loads(manifest_json) if isinstance(manifest_json, str) else manifest_json
    except (ValueError, TypeError):
        return "manifest_json was not valid JSON"
    sidecars = None
    if source_json:
        try:
            sidecars = json.loads(source_json) if isinstance(source_json, str) else source_json
        except (ValueError, TypeError):
            sidecars = None
    problems = _review(manifest, sidecars)
    return "OK" if not problems else "\n".join(problems)


# --- Tools available to the Render Agent ---
# review_render_manifest is the reviewer construct (F8): the agent calls it to check its own
# manifest against the source data and self-correct, rather than a hardcoded gate deciding for
# it. A deterministic floor still runs at finalize as defense in depth.
# Manifests written by the build_render_manifest tool, keyed by manifest_id. The agent entrypoint
# reads this after the agent stream to emit the output_ref -- so the deterministic assembly (which
# projects 60+ boxes, markers, lanes) does the heavy lifting and the LLM never hand-emits a large
# manifest. Keyed by manifest_id (unique per invoke) so concurrent renders don't collide.
_RENDER_WRITES: dict = {}
# Rebuild cap per manifest_id (guards the review->rebuild self-recovery loop from spinning on a
# manifest that keeps failing review -- the deterministic builder can't fix itself by re-running).
_RENDER_BUILD_COUNTS: dict = {}
_MAX_BUILDS_PER_MANIFEST = 3


def _manifest_is_renderable(m) -> bool:
    """True if a manifest can actually display something: a media track with a src, images, a
    document preview, or data rows. Used to decide whether an enriched (overlay) build should
    degrade to the base medium -- enrichment is additive and must never yield a dead render.
    """
    if not isinstance(m, dict) or m.get("errors"):
        return False
    for t in (m.get("tracks") or []):
        if isinstance(t, dict) and t.get("src"):
            return True
    if m.get("images"):
        return True
    if isinstance(m.get("document"), dict) and m["document"].get("previewUrl"):
        return True
    if m.get("rows") or m.get("tables"):
        return True
    return False


def _manifest_rendered_urls(m) -> set:
    """Normalized set of source urls a manifest actually renders (media tracks, images, document
    preview, top-level src). Used by the completeness VERIFY to detect a dropped deliverable."""
    from render.review import _norm_url
    out: set = set()
    if not isinstance(m, dict):
        return out
    for t in (m.get("tracks") or []):
        if isinstance(t, dict) and t.get("src"):
            out.add(_norm_url(t["src"]))
    for im in (m.get("images") or []):
        s = im.get("src") if isinstance(im, dict) else im
        if s:
            out.add(_norm_url(s))
    for doc in [m.get("document"), *(m.get("documents") or [])]:
        if isinstance(doc, dict) and doc.get("previewUrl"):
            out.add(_norm_url(doc["previewUrl"]))
    if m.get("src"):
        out.add(_norm_url(m["src"]))
    return out


def _is_descriptive_sidecar(url: str) -> bool:
    """True when `url` names data ABOUT the media rather than data to draw ON it.

    Reuses the ONE operational-name rule (``hooks.provider._is_operational_name``) rather than
    adding a filename list here -- same deploy unit, so this is a shared predicate, not a copy.
    probe.json is the case: a spec of the source (codec, duration, profile) with nothing to
    project, so it must not put the source video on screen.
    """
    from hooks.provider import _is_operational_name
    return _is_operational_name(url.split("?", 1)[0].rsplit("/", 1)[-1])


def _dropped_renderable_candidates(urls, manifest) -> list:
    """Renderable media/image/pdf candidates that did NOT make it into the manifest. Analysis JSON
    (projected as overlays, not rendered as media) is excluded, as is anything already present."""
    from render.review import _norm_url
    rendered = _manifest_rendered_urls(manifest)
    renderable_exts = _VIDEO_EXTS | _AUDIO_EXTS | _IMAGE_EXTS | {"pdf", "html"}
    cands = [u for u in (urls or []) if _ext_from_url(u) in renderable_exts]
    return [u for u in cands if _norm_url(u) not in rendered]


# `artifacts/<session>/output/<plan_id>/<node_id>/<file>` — the canonical output location (or, with
# the project layout, `projects/<pid>/sessions/<session>/output/...`). The node id is a path
# SEGMENT, so it can be read back without any lookup. Anchored on either session root so only our
# own tree matches, and `[^/]+/` for the file means a bare
# `artifacts/<sid>/output/<file>` (no node) correctly fails to match.
_OUTPUT_NODE_RE = re.compile(
    r"/(?:artifacts|projects/p[0-9a-f]{32}/sessions)/(?P<sid>[^/]+)/output/(?P<plan>[^/]+)/(?P<node>[^/]+)/[^/]+$",
    re.I)


def render_key_from_artifact_paths(urls: Optional[list]) -> str:
    """The render key implied by the artifacts' own LOCATION, or "" when it cannot be known.

    This is how the EXPLICIT render path (`dispatch_tool(target="render")`) gets a stable key.
    It has no job id in scope — no tool result, no registry handle, just urls in a prompt — so it
    used to fall through to `_manifest_id_for(cands)`, a content hash. The hook path meanwhile
    keys on the job (`canonical_render_key`), so the SAME job rendered both ways produced two S3
    objects and therefore two tabs. Only the url-dedup layers hid it.

    The urls already carry the answer: outputs live at
    ``artifacts/<session>/output/<plan_id>/<node_id>/<file>``, so the node id is just a path
    segment. Reading it back needs no lookup, no reverse index and no model, and it yields the
    SAME `node_id` that `canonical_render_key` resolves from the registry row — so the two paths
    converge. Same principle as source-role derivation (`provider._has_input_path`): make
    identity a property of the durable LOCATION rather than of transient context.

    Declines (returns "") rather than guessing when:
      * the urls span MORE THAN ONE node — keying on either would file one node's manifest onto
        another node's tab, which is worse than a content hash
      * they are `/input/` (not deliverables), `/render/` (another render's manifest), legacy flat
        keys, or anything outside the convention
    """
    from render.keys import plan_node_render_key
    nodes: set[tuple[str, str]] = set()
    for u in (urls or []):
        if not isinstance(u, str) or not u.strip():
            continue
        m = _OUTPUT_NODE_RE.search(u.split("?", 1)[0])
        if not m:
            # Not a session-scoped output: an /input/, a /render/ manifest, a legacy flat key or a
            # foreign url. Any one of those means we cannot claim a node for this render.
            return ""
        nodes.add((m.group("plan"), m.group("node")))
    if len(nodes) != 1:
        return ""
    # Plan-scoped (render.keys): the same node id in two plans is two manifests.
    plan, node = next(iter(nodes))
    return re.sub(r"[^A-Za-z0-9._-]", "_", plan_node_render_key(plan, node))[:120]


def _apply_subsumes(cands: list[str]) -> list[str]:
    """Drop candidate outputs that a present, richer deliverable subsumes.

    A render skill may declare ``subsumes: <basenames>`` alongside its own
    ``deliverable:``. It is a data-driven statement that the skill's canonical
    output is a superset of those siblings (face-recognition's facegroup.json
    carries everything facemetadata.json / face_clusters.json do PLUS per-face
    boxes). When the subsuming deliverable is present among ``cands``, the
    subsumed siblings are removed so they never render as their own duplicate
    tab — keeping the "which file" choice declarative and per-skill instead of
    sniffing near-identical shapes.

    Scoped to a single render's candidate set, so it can only ever collapse
    siblings of ONE result, never unrelated deliverables. Degrades to a no-op if
    skills are unavailable or if it would drop everything.
    """
    if not cands:
        return cands
    try:
        skills = _discover_render_skills()
    except Exception as exc:  # noqa: BLE001 - never fail a render over skills
        logger.warning("render: subsumes skipped (skills unavailable): %s", exc)
        return cands

    def _bn(u: str) -> str:
        return u.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1].lower()

    present = {_bn(u) for u in cands}
    drop_names: set[str] = set()
    for s in skills:
        decl = s.get("declaration") or {}
        subs = decl.get("subsumes")
        if not subs:
            continue
        delivs = [d.strip().lower() for d in str(decl.get("deliverable") or "").split(",") if d.strip()]
        # The subsuming deliverable must itself be present for its claim to apply;
        # with facegroup absent, facemetadata legitimately renders on its own.
        if delivs and not any(d in present for d in delivs):
            continue
        for x in str(subs).split(","):
            x = x.strip().lower()
            if x:
                drop_names.add(x)
    if not drop_names:
        return cands
    kept = [u for u in cands if _bn(u) not in drop_names]
    if kept and len(kept) != len(cands):
        logger.info("render: subsumes dropped %d candidate(s) (%s)",
                    len(cands) - len(kept), ", ".join(sorted(drop_names)))
        return kept
    return cands


@tool
def _resolve_manifest_id(payload) -> str:
    """Use the content-addressed manifest_id that dispatch_render computed from the candidate URL
    set, so identical/concurrent dispatches converge on ONE manifest S3 key (one tab) instead of
    minting a fresh uuid per render (the duplicate-tabs bug — the render agent was ignoring the
    id and generating its own uuid every time). Falls back to EMPTY when none was passed, so
    build_render_manifest derives a DETERMINISTIC content-hash from the rendered urls (an EXPLICIT
    render then converges on the hook's tab) rather than a random uuid."""
    mid = payload.get("manifest_id") if isinstance(payload, dict) else None
    return mid if isinstance(mid, str) and mid.strip() else ""


def _split_candidate_urls(v) -> list:
    """Parse a ``candidate_urls`` / ``source_urls`` value into a FLAT list of individual URLs.

    Robust to the three shapes the Render Agent hands over: a JSON array, a single URL, or a
    whitespace/newline-JOINED blob of several URLs. The joined-blob case is the important one:
    session 46480f72 passed 8 frame URLs as one ``"\\n"``-joined string, and the old parser
    wrapped the whole blob as a SINGLE candidate → one ``images[]`` entry whose ``src`` held all
    8 URLs → an unloadable image → a blank gallery. Splitting here makes each frame its own
    render entry. A plain non-URL string (no scheme) is preserved as-is (trimmed) so
    non-URL candidates still pass through.
    """
    def _split(s: str) -> list:
        # Exclude backslash from URL chars so a literal "\n"/"\r" escape between joined
        # URLs (how session 46480f72's blob was actually encoded — backslash+n, not a real
        # newline) breaks one URL from the next; findall then picks up each scheme-prefixed
        # URL. Real URLs never contain a raw backslash (it'd be %5C), so this is safe.
        found = re.findall(r'(?:https?://|s3://)[^\s"\'<>)\]\\]+', s)
        if found:
            return found
        s = s.strip()
        return [s] if s else []

    def _flatten(items) -> list:
        out: list = []
        for u in items:
            if isinstance(u, str) and u.strip():
                out.extend(_split(u))
        return out

    if isinstance(v, list):
        return _flatten(v)
    try:
        parsed = json.loads(v) if isinstance(v, str) and v.strip() else []
    except (json.JSONDecodeError, TypeError, ValueError):
        parsed = None
    if isinstance(parsed, list):
        return _flatten(parsed)
    return _split(v) if isinstance(v, str) and v.strip() else []


def _sanitize_url_list(items: list) -> list:
    """Flatten + clean a url list so no MALFORMED url reaches a manifest.

    ``_split_candidate_urls`` handles a whitespace/newline-joined blob AND a single url that
    glued a second filename on with a literal backslash-escape — its regex stops at ``\\``, so
    ``.../a.jpg\\nb.jpg`` yields ``.../a.jpg``. Session 3445660b's 8-keyframe gallery arrived with
    7 of 8 srcs shaped ``.../shot_0N_key.jpg\\nshot_0(N+1)_key.jpg`` (every object existed, but the
    URL was unfetchable → 7 tiles 404'd, only the last clean one loaded, and Refresh could never
    fix it because the dead string was baked into the persisted manifest). Cleaning each entry to
    its first valid url token recovers ALL N: each frame is the first token of exactly one entry.
    Idempotent on already-clean urls; non-string items pass through untouched.
    """
    out: list = []
    for u in (items or []):
        if isinstance(u, str) and u.strip():
            out.extend(_split_candidate_urls(u))
        elif u:
            out.append(u)
    return out


def _recover_missing_deliverable(skill: "Optional[dict]", cands: list) -> dict:
    """Recover a declared JSON deliverable that a PROJECTING render skill needs but that is
    ABSENT from the candidate urls, returning ``{url: parsed_json}`` to feed a re-projection.

    Why: a plan node occasionally adopts a media tool's outputs WITHOUT the JSON sidecar the tool
    wrote a beat later (an adoption/write race), so e.g. a shot-segmentation render arrives with the
    segment + keyframes but no ``shots.json`` -> the projection has nothing and the timeline is
    empty (session 08c4f282, while a sibling job folder held the real 23-shot ``shots.json``). This
    finds the declared deliverable under THIS session's own ``output/**`` tree and hands its
    content back so the existing projector can run.

    Strictly scoped + best-effort, so it can only recover — never error or alter a working render:
      * runs only for a matched skill that declares BOTH a projection (markers/segments/boxes/rows)
        AND a ``.json`` deliverable that is NOT already among ``cands`` (so plain media renders —
        render-video's ``transcoded.mp4``, render-audio's ``*.wav`` — are a no-op);
      * derives the session from the candidate keys, so it never reaches into another session;
      * any S3 error yields ``{}`` (the render proceeds exactly as before).
    """
    if not (_s3 and S3_BUCKET and isinstance(skill, dict)):
        return {}
    decl = skill.get("declaration") or {}
    if not any(decl.get(k) for k in ("markers", "segments", "boxes", "rows")):
        return {}  # not a projecting skill — a sidecar would add nothing
    wanted = {d.strip().lower() for d in str(decl.get("deliverable") or "").split(",")
              if d.strip().lower().endswith(".json")}
    if not wanted:
        return {}
    have = {u.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1].lower() for u in cands}
    wanted -= have
    if not wanted:
        return {}
    # Derive the session OUTPUT prefix from a candidate key, under either root:
    # artifacts/<session>/output/<job>/<file> or projects/<pid>/sessions/<session>/output/<job>/<file>
    # (asset-lineage Phase 3 (a)).
    from projects.storage_layout import find_session_key, project_session_root
    prefix = ""
    for u in cands:
        sk = find_session_key(u)
        if sk is not None:
            prefix = (f"{project_session_root(sk.pid, sk.sid)}output/" if sk.pid
                      else f"{S3_PREFIX}{sk.sid}/output/")
            break
    if not prefix:
        return {}
    hits: dict = {}
    try:
        for page in _s3.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=prefix):
            for o in page.get("Contents", []):
                key = o["Key"]
                base = key.rsplit("/", 1)[-1].lower()
                if base in wanted:
                    hits.setdefault(base, []).append((str(o.get("LastModified") or ""), key))
        out: dict = {}
        for base, found in hits.items():
            key = sorted(found)[-1][1]  # newest under this session for that deliverable name
            data = json.loads(_s3.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read())
            url = (f"https://{ARTIFACTS_DOMAIN}/{key}"
                   if (ARTIFACTS_DOMAIN and key.startswith(S3_PREFIX)) else key)
            out[url] = data
        if out:
            logger.info("render: recovered missing deliverable(s) %s from session output",
                        sorted(out))
        return out
    except Exception as e:  # noqa: BLE001 — discovery is best-effort; never fail the render
        logger.warning("render: deliverable discovery failed (%s); rendering without it", e)
        return {}


def build_render_manifest(candidate_urls: str, manifest_id: str, session_id: str = "",
                          source_urls: str = "") -> str:
    """Deterministically ASSEMBLE and WRITE the render manifest for the urls you chose.

    This is how you render analysis results: you DECIDE which files to render (using intent + each
    type's deliverable, skipping scratch); this tool does the mechanical projection (face boxes,
    shot markers, speaker lanes) via the render skill declarations, picks the right template,
    writes the manifest to S3, and returns a COMPACT summary + s3_key. Do NOT hand-build large
    manifests yourself.

    Args:
        candidate_urls: JSON array of the deliverable urls to render (media and/or analysis JSON).
        manifest_id: the "Manifest ID to use" given in your prompt — pass it verbatim.
        session_id: the session id given in your prompt.
        source_urls: JSON array of SOURCE media the analysis describes (e.g. the video to draw
            face boxes on). Optional.

    Returns: JSON {s3_key, manifest_id, template, annotations, markers, segments, tracks, errors}.
    Call review_render_manifest next, then finalize.
    """
    def _split_urls(s: str) -> list:
        return _split_candidate_urls(s)

    def _arr(v):
        return _split_candidate_urls(v)

    cands, srcs = _arr(candidate_urls), _arr(source_urls)
    # A SOURCE/INPUT url is a canvas for projections, NEVER a deliverable track. Every other
    # layer asserts this (the hook keeps source_urls out of `urls`; validate_resolved_urls
    # rejects "this is an input/source, not an output"); the builder was the one place that
    # did not. Enforce it here so a source that reaches the candidates — e.g. the Render Agent
    # echoing the context video into candidate_urls — cannot become a track. Without this an
    # audio-only result like stem separation rendered the full-length input video as a track
    # that set the timeline duration and dominated the 3 short stems. Match leniently (path OR
    # bare filename) so a CloudFront mirror of an s3 source is still caught. Source stays
    # available as src_video below for the projection (video-annotations) path.
    if srcs:
        def _basename(u: str) -> str:
            return u.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1].lower()
        _src_paths = {s.split("?", 1)[0].lower() for s in srcs}
        _src_names = {_basename(s) for s in srcs if _basename(s)}
        _before = len(cands)
        cands = [u for u in cands
                 if u.split("?", 1)[0].lower() not in _src_paths and _basename(u) not in _src_names]
        if len(cands) != _before:
            logger.info("render: dropped %d source url(s) from deliverables (source is a canvas, "
                        "not an output)", _before - len(cands))
    cands = _apply_subsumes(cands)

    # NOTHING LEFT TO RENDER -> render nothing. This is the deterministic slice of intent
    # filtering: a result whose only "outputs" were the source it ran on has definitively no
    # deliverable, and the right answer is zero tabs.
    #
    # Before this guard the opposite happened, twice over. `_manifest_id_for([])` hits its
    # "no urls" branch and returns a RANDOM uuid, so an empty `nle-timeline` manifest was
    # written under a non-convergent key — a spurious tab that a retry could not even overwrite
    # idempotently, so every attempt added another. Worse, the dropped SOURCE was still stamped
    # into that manifest's `candidate_keys`, which `_already_rendered_urls` reads back — so the
    # no-op render taught the durable dedup that the user's INPUT had been rendered, suppressing
    # a later legitimate render of it.
    #
    # Note the asymmetry with the other two pruners, which is deliberate: `_apply_subsumes` and
    # `intermediate_node_ids` both refuse to prune to nothing, because "which of these siblings
    # is richer" is a judgement that can be wrong. "Every candidate was the source" is not a
    # judgement — it is a fact about the set — so failing open here would mean knowingly
    # rendering an input as an output.
    if not cands:
        logger.info("render: no deliverable left after pruning (all source and/or subsumed); "
                    "rendering nothing")
        return json.dumps({
            "s3_key": None,
            "manifest_id": None,
            "note": "No deliverable to render: every candidate was source media or was subsumed "
                    "by another deliverable. Nothing was written.",
        })

    # Deterministic identity when no manifest_id was passed — the EXPLICIT/agentic render path
    # (`dispatch_tool target=render` sends a prompt with no manifest_id). A caller-supplied id
    # (the hook, via dispatch_render) always wins. Then, in order:
    #
    #   1. the artifact PATH  `artifacts/<sid>/output/<plan>/<node>/<file>` -> `node_id`, which is
    #      exactly what `canonical_render_key` resolves for the hook path, so the two paths
    #      converge on ONE S3 key. They used to disagree — node-keyed vs hash-keyed for the same
    #      job — which is two objects and two tabs, hidden only by the url-dedup layers.
    #   2. a content hash of the chosen deliverables (`render_review._norm_url` + sha256, the same
    #      scheme dispatch_render uses), for a render with no job AND no derivable node.
    mid = manifest_id.strip() if isinstance(manifest_id, str) else ""
    if not mid:
        mid = render_key_from_artifact_paths(cands)
        if mid:
            logger.info("render: keyed on the artifact path -> node %s", mid)
    if not mid:
        try:
            from render.artifacts import _manifest_id_for
            mid = _manifest_id_for(cands)
        except Exception:
            mid = str(uuid.uuid4())
    sid = session_id or "agent-build"
    # Cap rebuilds per manifest: the self-recovery loop (review -> rebuild) must never spin. If a
    # manifest keeps failing review, rebuilding won't help (the builder is deterministic), so after
    # a few attempts return the last build with a hard "finalize now" so the agent stops.
    _RENDER_BUILD_COUNTS[mid] = _RENDER_BUILD_COUNTS.get(mid, 0) + 1
    if _RENDER_BUILD_COUNTS[mid] > _MAX_BUILDS_PER_MANIFEST and mid in _RENDER_WRITES:
        prev = _RENDER_WRITES[mid]
        pm = prev.get("manifest") or {}
        return json.dumps({
            "s3_key": prev.get("s3_key"), "manifest_id": mid,
            "template": pm.get("template"),
            "note": ("Already built %d times — the deterministic output will not change. STOP "
                     "rebuilding and finalize now." % _RENDER_BUILD_COUNTS[mid]),
        })
    # ONE canonical assembler (shared with the render entrypoint _render_deterministic) does
    # classify → skill-select → build → stamp (template, renderSkill, candidate_keys, sourceKey).
    # `srcs` is the analysed CANVAS (already dropped from `cands` above); passing it lets
    # sourceKey trace to the source so every analysis of one video merges onto one timeline.
    jsons = [u for u in cands if _ext_from_url(u) in _JSON_EXTS]
    fetched = _fetch_json_many(jsons) if jsons else {}
    # RAW candidate keys (pre-mirror) so the durable dedup recognises this render regardless of
    # how each url is later mirrored/imported.
    try:
        from render.review import _norm_url as _nrm
        _ck = sorted({_nrm(u) for u in _arr(candidate_urls) if isinstance(u, str) and u.strip()})
    except Exception:  # noqa: BLE001 - dedup metadata is a nicety, never a blocker
        _ck = []
    manifest, _projected, _skill = assemble_manifest(
        artifact_urls=cands, source_urls=srcs, tool=None, manifest_id=mid,
        render_options={}, fetched=fetched, extra_images=[], candidate_keys=_ck)
    if not _projected:
        # A projecting skill matched but produced nothing — its declared JSON deliverable was
        # missing from the candidates (dropped-sidecar adoption race; see _recover_missing_deliverable).
        # Discover it under this session's output and re-assemble. No-op for non-projecting/plain-media.
        _recovered = _recover_missing_deliverable(_skill, cands)
        if _recovered:
            cands = cands + list(_recovered.keys())
            fetched = {**fetched, **_recovered}
            manifest, _projected, _skill = assemble_manifest(
                artifact_urls=cands, source_urls=srcs, tool=None, manifest_id=mid,
                render_options={}, fetched=fetched, extra_images=[], candidate_keys=_ck)
    s3_key = _write_manifest_to_s3(format_manifest_safe(manifest), sid, mid)
    _RENDER_WRITES[mid] = {"s3_key": s3_key, "manifest": manifest}
    return json.dumps({
        "s3_key": s3_key, "manifest_id": mid, "template": manifest.get("template"),
        "sourceKey": manifest.get("sourceKey"),
        "annotations": len(manifest.get("annotations") or []),
        "markers": len(manifest.get("markers") or []),
        "segments": len(manifest.get("segments") or []),
        "tracks": len(manifest.get("tracks") or []),
        "errors": [e.get("code") for e in (manifest.get("errors") or []) if isinstance(e, dict)],
    })


def _build_clip_gallery_manifest(items: list, manifest_id: str,
                                 title: str = "", subtitle: str = "") -> dict:
    """Pure builder for an independent-clip gallery manifest (no I/O — testable).

    Each item is {label, url|src, text?, kind?, durationHint?, status?}; `kind` is inferred
    from the url extension when omitted, and only https urls survive (the element src cannot
    be an s3:// uri). Returns a manifest with template ``clip-gallery`` and ``galleryClips`` —
    a DISTINCT field from a timeline manifest's ``clips`` (which are segments of one source).
    """
    gallery: list = []
    for i, it in enumerate(items if isinstance(items, list) else []):
        if not isinstance(it, dict):
            continue
        picked = _sanitize_url_list([str(it.get("url") or it.get("src") or "")])
        url = picked[0] if picked else ""
        if not url.startswith("https://"):
            continue  # element src must be a browser-fetchable https url, never s3://
        ext = _ext_from_url(url)
        kind = str(it.get("kind") or "").lower()
        if kind not in ("audio", "video"):
            kind = "video" if ext in _VIDEO_EXTS else "audio"
        entry: dict = {
            "id": str(it.get("id") or f"clip{i + 1}"),
            "label": str(it.get("label") or f"Clip {i + 1}"),
            "src": url,
            "kind": kind,
        }
        if it.get("text"):
            entry["text"] = str(it["text"])
        dh = it.get("durationHint")
        if isinstance(dh, (int, float)) and not isinstance(dh, bool) and dh > 0:
            entry["durationHint"] = float(dh)
        if it.get("status"):
            entry["status"] = str(it["status"])
        gallery.append(entry)

    manifest: dict = {
        "id": (manifest_id.strip() if isinstance(manifest_id, str) and manifest_id.strip() else str(uuid.uuid4())),
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "template": "clip-gallery",
        "galleryClips": gallery,
    }
    if title:
        manifest["title"] = title
    if subtitle:
        manifest["subtitle"] = subtitle
    return manifest


def build_clip_gallery_manifest(clips_json: str, manifest_id: str, session_id: str = "",
                                title: str = "", subtitle: str = "") -> str:
    """ASSEMBLE and WRITE an INDEPENDENT-CLIP GALLERY for review + per-clip mark-in/out trimming.

    Use this to present SEVERAL independent takes the user reviews and edits ONE BY ONE — e.g.
    Agent Olive's per-chunk TTS audio (each gets a waveform + a mark-in/out ruler to trim the
    leading blip) or per-shot lip-sync videos. Each clip becomes its own card; a batch
    "Trim/Clip all marked" action composes ONE turn naming each clip + its keep-range. This is
    NOT the timeline: use ``build_render_manifest`` when tracks share ONE time axis; use THIS when
    the clips are independent and NOT time-aligned.

    Args:
        clips_json: JSON array of {label, url, text?, kind?('audio'|'video'), durationHint?, status?}.
                    `kind` is inferred from the url extension when omitted; only https urls render.
        manifest_id: the "Manifest ID to use" from your prompt — pass it verbatim.
        session_id: the session id from your prompt.
        title, subtitle: optional gallery headings.

    Returns: JSON {s3_key, manifest_id, template, clips}.
    """
    try:
        items = json.loads(clips_json) if isinstance(clips_json, str) else clips_json
    except (json.JSONDecodeError, TypeError, ValueError):
        items = None
    if not isinstance(items, list) or not items:
        return json.dumps({"s3_key": None, "manifest_id": None,
                           "note": "clips_json must be a non-empty JSON array of {label, url, ...}"})
    manifest = _build_clip_gallery_manifest(items, manifest_id, title=title, subtitle=subtitle)
    if not manifest["galleryClips"]:
        return json.dumps({"s3_key": None, "manifest_id": None,
                           "note": "no clips with an https url; nothing to render"})
    sid = session_id or "agent-build"
    s3_key = _write_manifest_to_s3(json.dumps(manifest), sid, manifest["id"])
    return json.dumps({"s3_key": s3_key, "manifest_id": manifest["id"],
                       "template": "clip-gallery", "clips": len(manifest["galleryClips"])})


RENDER_TOOLS = [media_probe, peak_extractor, caption_parser, artifact, validate_render_urls, review_render_manifest, build_render_manifest, build_clip_gallery_manifest]

# --- Skills Manager for render skills ---
_skills_manager = SkillsManager(skills_dir=RENDER_SKILLS_DIR, s3_bucket=S3_BUCKET)

# --- Pipeline timeout budget (seconds) ---
PIPELINE_TIMEOUT_SECONDS = 240

# --- Self-correction retry budget (seconds) ---
SELF_CORRECTION_TIMEOUT_SECONDS = 5

# --- System prompt for the Render Agent LLM ---


def _build_error_manifest(error_code: str, message: str, source_url: str = "") -> dict:
    """Build a structured error manifest.

    `duration` is the schema minimum rather than 0: the renderer validates before it
    reads `errors`, so a zero duration made it reject the manifest and show "Field
    duration must be >= 0.001, got 0" INSTEAD of the actual failure. The error text is
    the only useful thing here, so it must survive validation to be displayed.
    """
    manifest = {
        "id": str(uuid.uuid4()),
        "duration": 0.001,
        "tracks": [],
        "clips": [],
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "errors": [{"code": error_code, "message": message}],
    }
    clean = _first_clean_url(source_url)
    if clean:
        manifest["sourceUrl"] = clean
    return manifest


_FIRST_URL_RE = re.compile(r"(?:https?|s3)://[^\s\"'<>()\[\]]+")


def _first_clean_url(text: str) -> str:
    """The first URL in ``text`` with its query string dropped, else "".

    Callers passed the whole render PROMPT as an error manifest's ``sourceUrl`` (staging session a2d759c2:
    1.5 KB of instructions plus a presigned URL, X-Amz-Security-Token included, persisted in S3). A manifest
    names its source; it never carries a prompt or a credential.
    """
    if not isinstance(text, str) or not text:
        return ""
    m = _FIRST_URL_RE.search(text)
    return m.group(0).split("?", 1)[0].split("#", 1)[0] if m else ""


# Manifest fields that make something worth showing. An ERROR-ONLY manifest (non-empty `errors`, none of these)
# is a failure record, not a view: written under `render/_errors/` (never listed as a session render) and its
# output_ref always carries `error: True`. Mirrors ui/server/renderSession.ts `isErrorOnlyManifest`.
_VIEW_CONTENT_KEYS = ("template", "tracks", "clips", "images", "tables", "rows", "document", "markers",
                      "segments", "annotations", "captions", "galleryClips", "galleryMoments", "nodes")

ERRORS_SUBDIR = "_errors"


def is_error_only_manifest(manifest) -> bool:
    if not isinstance(manifest, dict) or not manifest.get("errors"):
        return False
    return not any(manifest.get(k) for k in _VIEW_CONTENT_KEYS)


def _normalize_output_ref(ev):
    """An output_ref naming an error record is ALWAYS flagged ``error: True``, whichever site emitted it.

    Several sites wrote an error manifest and announced it as a plain output_ref (the agentic
    VALIDATION_FAILED, the deterministic AGENT_ERROR / NO_URL / UNSUPPORTED_FORMAT), so callers treated a
    failure record as a render and it became a tab. One choke point instead of ten call sites.
    """
    try:
        obj = json.loads(ev) if isinstance(ev, str) else None
    except (json.JSONDecodeError, TypeError, ValueError):
        return ev
    if (isinstance(obj, dict) and obj.get("type") == "output_ref" and not obj.get("error")
            and f"/render/{ERRORS_SUBDIR}/" in str(obj.get("s3_key") or "")):
        obj["error"] = True
        obj.setdefault("error_code", "RENDER_ERROR")
        return json.dumps(obj)
    return ev


def _no_view(reason: str, detail: str = "") -> str:
    """The typed TERMINAL outcome of a deliberate decline: there is nothing to show, and that is not a failure.

    ``invoke`` never escalates it to the LLM agent (a2d759c2: the deterministic "JSON is data, not a view"
    decline escalated, the agent built a timeline from JSON, failed validation, and its error manifest became
    a tab). Callers treat it as a clean no-op and drop any placeholder tab.
    """
    return json.dumps({"type": "no_view", "reason": reason, "detail": detail[:300]})


def _build_timeout_manifest(source_url: str = "") -> dict:
    """Build a TIMEOUT error manifest."""
    return _build_error_manifest(
        error_code="TIMEOUT",
        message="Processing exceeded 30-second pipeline timeout",
        source_url=source_url,
    )


def _load_render_skills() -> list[Skill]:
    """Load render-specific skills from the skills directory (for the agent's AgentSkills).

    Uses RENDER_SKILLS_DIR (the resolved dir that works in both the repo `agent/skills` and the
    flat runtime `skills/` layouts) -- hardcoding ``__file__/skills`` found nothing locally and
    only worked in the deployed layout.
    """
    skills = []
    skills_dir = RENDER_SKILLS_DIR

    if not skills_dir.exists():
        return skills

    for skill_dir in skills_dir.iterdir():
        if skill_dir.is_dir() and skill_dir.name.startswith("render-"):
            skill_md = skill_dir / "SKILL.md"
            if skill_md.exists():
                try:
                    skill = Skill.from_file(str(skill_dir))
                    skills.append(skill)
                    logger.info("Loaded render skill: %s", skill.name)
                except Exception as e:
                    logger.warning("Failed to load render skill from %s: %s", skill_dir, e)

    return skills


def _write_manifest_to_s3(manifest_json: str, session_id: str, manifest_id: str) -> Optional[str]:
    """Write manifest JSON to S3. Returns S3 key on success, None on failure."""
    if not _s3 or not S3_BUCKET:
        return None

    # A manifest_id must NEVER be empty: an empty id writes `.../render/.json`, which every
    # empty-id render then overwrites/collides on (and the UI showed it as a broken tab). This is a
    # safety net for any caller that reaches here without a derived id — prefer the manifest's own
    # `id`, else a deterministic content hash of the body (so identical manifests still converge).
    if not str(manifest_id or "").strip():
        import hashlib
        derived = ""
        try:
            derived = str((json.loads(manifest_json) or {}).get("id") or "").strip()
        except Exception:  # noqa: BLE001 - body may not be valid json
            derived = ""
        manifest_id = derived or hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()[:32]
        logger.warning("render: empty manifest_id — derived '%s' to avoid a '.json' key", manifest_id)

    try:
        _error_only = is_error_only_manifest(json.loads(manifest_json))
    except Exception:  # noqa: BLE001 - an unparseable body is written where it always was
        _error_only = False
    if _error_only:
        # A failure record, not a view: kept for debugging where the renders listing never sees it.
        s3_key = f"{S3_PREFIX}{session_id}/render/{ERRORS_SUBDIR}/{manifest_id}.json"
        try:
            _s3.put_object(Bucket=S3_BUCKET, Key=s3_key, Body=manifest_json.encode("utf-8"),
                           ContentType="application/json")
            return s3_key
        except Exception as e:  # noqa: BLE001
            logger.warning("S3 write failed for %s: %s", s3_key, e)
            return None

    s3_key = f"{S3_PREFIX}{session_id}/render/{manifest_id}.json"

    # Richer wins (R7.3): never replace a manifest with one carrying fewer analysis items. The
    # existing key is still returned, so the caller announces it and the tab shows the richer view.
    try:
        from render.richness import prepare_write
        keep, manifest_json = prepare_write(_s3, S3_BUCKET, s3_key, manifest_json)
        if keep:
            return s3_key
    except Exception as e:  # noqa: BLE001 - the guard must never block a write
        logger.debug("richer-wins check skipped for %s: %s", s3_key, e)

    try:
        _s3.put_object(
            Bucket=S3_BUCKET,
            Key=s3_key,
            Body=manifest_json.encode("utf-8"),
            ContentType="application/json",
        )
        return s3_key
    except Exception as e:
        logger.warning("S3 write failed for %s: %s", s3_key, e)
        return None


def _extract_json_from_text(text: str) -> Optional[str]:
    """Extract JSON from agent text output, handling potential markdown fences."""
    stripped = text.strip()

    # Try direct parse first
    try:
        json.loads(stripped)
        return stripped
    except json.JSONDecodeError:
        pass

    # Try stripping markdown code fences
    if stripped.startswith("```"):
        lines = stripped.split("\n")
        # Remove first line (```json or ```) and last line (```)
        if lines[-1].strip() == "```":
            content = "\n".join(lines[1:-1]).strip()
            try:
                json.loads(content)
                return content
            except json.JSONDecodeError:
                pass

    # Try finding JSON object in the text
    start = stripped.find("{")
    if start >= 0:
        # Find the matching closing brace
        depth = 0
        for i in range(start, len(stripped)):
            if stripped[i] == "{":
                depth += 1
            elif stripped[i] == "}":
                depth -= 1
                if depth == 0:
                    candidate = stripped[start:i + 1]
                    try:
                        json.loads(candidate)
                        return candidate
                    except json.JSONDecodeError:
                        break

    return None


def _validate_manifest_json(manifest_json: str) -> tuple[bool, list[str]]:
    """Validate manifest JSON structure. Returns (is_valid, error_messages)."""
    errors = []

    try:
        data = json.loads(manifest_json)
    except json.JSONDecodeError as e:
        return False, [f"Invalid JSON: {e}"]

    if not isinstance(data, dict):
        return False, ["Manifest must be a JSON object"]

    # Check required fields
    required_fields = ["id", "duration", "tracks", "clips", "createdAt"]
    for field_name in required_fields:
        if field_name not in data:
            errors.append(f"Missing required field: {field_name}")

    # The UI rejects a manifest with duration < 0.001 ("Field \"duration\" must be >= 0.001, got 0") — a
    # duration of 0 is the signature of a dead analysis node that produced no usable timeline. Enforce the
    # schema floor here so such a manifest is treated as INVALID and never WRITTEN as a timeline; the agentic
    # path then falls back to a clean error manifest, the deterministic path records the validation error.
    # Every real builder uses >= 0.001 (error/generic/image use the floor; media uses max track end), so this
    # only ever rejects the malformed shape (staging session 12f6f088).
    if "duration" in data:
        try:
            if float(data["duration"]) < 0.001:
                errors.append(f'duration must be >= 0.001, got {data["duration"]}')
        except (ValueError, TypeError):
            errors.append("duration is not a number")

    # Validate tracks
    if "tracks" in data and isinstance(data["tracks"], list):
        valid_types = {"video", "audio", "overlay", "subtitle"}
        for i, track in enumerate(data["tracks"]):
            if not isinstance(track, dict):
                errors.append(f"tracks[{i}] is not an object")
                continue
            # A timeline track MUST carry both start and end. The malformed sg4 manifest hand-emitted a
            # single overlay track with id/type/src/label/metadata but NO start/end, which the old check
            # (only validating when BOTH were present) waved through — and the UI then threw the hard
            # "tracks[0].start is missing" error. Require them so the agent never writes that shape.
            if "start" not in track or "end" not in track:
                errors.append(f"tracks[{i}]: missing start/end")
                continue
            try:
                start = float(track["start"])
                end = float(track["end"])
                if start >= end:
                    errors.append(f"tracks[{i}]: start ({start}) >= end ({end})")
            except (ValueError, TypeError):
                errors.append(f"tracks[{i}]: invalid start/end values")

    # Validate clips
    if "clips" in data and isinstance(data["clips"], list):
        for i, clip in enumerate(data["clips"]):
            if not isinstance(clip, dict):
                errors.append(f"clips[{i}] is not an object")
                continue
            if "start" in clip and "end" in clip:
                try:
                    start = float(clip["start"])
                    end = float(clip["end"])
                    if start >= end:
                        errors.append(f"clips[{i}]: start ({start}) >= end ({end})")
                except (ValueError, TypeError):
                    errors.append(f"clips[{i}]: invalid start/end values")

    # Validate captions
    if "captions" in data and isinstance(data["captions"], list):
        for i, cue in enumerate(data["captions"]):
            if not isinstance(cue, dict):
                errors.append(f"captions[{i}] is not an object")
                continue
            if "start" in cue and "end" in cue:
                try:
                    start = float(cue["start"])
                    end = float(cue["end"])
                    if start >= end:
                        errors.append(f"captions[{i}]: start ({start}) >= end ({end})")
                except (ValueError, TypeError):
                    errors.append(f"captions[{i}]: invalid start/end values")

    return len(errors) == 0, errors


def _repair_untemplated_manifest(manifest_json: str) -> str:
    """Make a template-less MEDIA manifest routable, DETERMINISTICALLY (no LLM, zero latency).

    The Render Agent prompt schema is ``{id,duration,tracks,clips,captions,createdAt,errors}`` —
    it has NO ``template``/``sourceKey``. When the model hand-builds that in its text instead of
    calling ``build_render_manifest`` (which stamps a template + drops source tracks), the manifest
    is unroutable and the UI shows a broken raw tab (session 368f1233). We must NOT reject it — that
    trips the self-correction LLM retry (adds latency). Instead stamp a default ``template`` +
    derive ``sourceKey`` so it renders as an nle-timeline and FOLDS with the real render of the
    same source (composeTimeline groups by sourceKey). Error manifests (no template but carrying
    ``errors``) are left untouched — the UI routes those to the error view."""
    try:
        m = json.loads(manifest_json)
    except (ValueError, TypeError):
        return manifest_json
    if not isinstance(m, dict) or m.get("template") or m.get("errors"):
        return manifest_json
    tracks = m.get("tracks") or []
    media = [t.get("src") for t in tracks
             if isinstance(t, dict) and t.get("src") and t.get("type") in ("video", "audio")]
    if not media:
        return manifest_json  # nothing renderable (e.g. an empty/error shell) — leave it alone
    m["template"] = "nle-timeline"
    if not m.get("sourceKey"):
        try:
            sk = derive_source_key([], media, media, has_analysis=_manifest_has_analysis(m))
            if sk:
                m["sourceKey"] = sk
        except Exception:  # noqa: BLE001 - sourceKey is best-effort; template alone unbreaks it
            pass
    logger.info("render: repaired a template-less hand-built manifest -> nle-timeline (id=%s)",
                m.get("id"))
    return json.dumps(m)


# ---------------------------------------------------------------------------
# Procedural pipeline — deterministic manifest generation
# ---------------------------------------------------------------------------

import re as _re

# s3:// included so canonical artifact refs are extracted; they are
# resolved to fetchable URLs by _resolve_s3_urls_via_gateway.
_URL_RE = _re.compile(r"(?:https?|s3)://[^\s\"'<>)\\]+")

_VIDEO_EXTS = render_types.VIDEO_EXTS
_AUDIO_EXTS = render_types.AUDIO_EXTS
_IMAGE_EXTS = render_types.IMAGE_EXTS

# The render dispatcher lists the media an analysis result describes under this
# heading. Keeping it a separate section (instead of mixing it into the artifact
# list) preserves the rule that source media never makes a job renderable.
_SOURCE_SECTION_RE = _re.compile(r"^[ \t]*source media\b.*$", _re.IGNORECASE | _re.MULTILINE)

# Other CONTEXT sections the dispatcher appends after the deliverables: prior renders this
# session ("ALREADY RENDERED ...") and the recent-intent turns ("RECENT USER REQUESTS ...").
# Their urls are context, never deliverables — they bound the deliverable scan just like the
# source heading does. Missing them let a no-source dispatch scrape the already-rendered urls as
# deliverables (5 images from a 1-url panel dispatch, session ccbc9559; the prior source video
# over a PDF, session 2693e705).
_CONTEXT_SECTION_RE = _re.compile(
    r"^[ \t]*(?:already rendered|recent user requests)\b.*$", _re.IGNORECASE | _re.MULTILINE)


def _split_render_request(prompt: str) -> tuple[list[str], list[str]]:
    """Split a render prompt into (deliverable URLs, source-media URLs).

    The dispatch prompt (render_artifacts.dispatch_render) has up to four labeled sections, in
    order: the CANDIDATE OUTPUT URLS (the deliverables), then optional context — "Source media
    ..." (inputs), "ALREADY RENDERED this session ..." (prior renders), and "RECENT USER REQUESTS
    ..." (intent text). ONLY the candidate section holds deliverables. Bounding the deliverable
    scan by the SOURCE heading alone let the ALREADY-RENDERED urls (prior reference images /
    analyzed source videos) be scraped as deliverables whenever no source section was present —
    the verified root of "1-url panel dispatch renders 5 images" (ccbc9559) and "PDF turn renders
    the source video" (2693e705). Bound the deliverable scan by the FIRST context heading of ANY
    kind; read sources only from the SOURCE section (up to the next context heading).
    """
    p = prompt or ""
    src_m = _SOURCE_SECTION_RE.search(p)
    ctx_m = _CONTEXT_SECTION_RE.search(p)
    boundaries = [m.start() for m in (src_m, ctx_m) if m]
    art_end = min(boundaries) if boundaries else len(p)
    artifacts = _extract_urls(p[:art_end])
    sources: list[str] = []
    if src_m:
        nxt = _CONTEXT_SECTION_RE.search(p, src_m.end())
        src_end = nxt.start() if nxt else len(p)
        sources = [u for u in _extract_urls(p[src_m.end():src_end]) if u not in artifacts]
    return artifacts, sources
    match = _SOURCE_SECTION_RE.search(prompt or "")
    if not match:
        return _extract_urls(prompt), []
    artifacts = _extract_urls(prompt[: match.start()])
    sources = [u for u in _extract_urls(prompt[match.end():]) if u not in artifacts]
    return artifacts, sources


def _extract_url(text: str) -> Optional[str]:
    """Pull the first http(s) URL out of a prompt string."""
    m = _URL_RE.search(text or "")
    return m.group(0) if m else None


def _extract_urls(text: str) -> list[str]:
    """Pull all http(s) URLs out of a prompt, de-duplicated, order-preserved.

    The Main Agent dispatches a single render request listing every related
    artifact (e.g. the speech/music/sfx stems of one job) so they render as
    one unified multi-track timeline rather than N disconnected views.
    """
    seen: set[str] = set()
    urls: list[str] = []
    for m in _URL_RE.finditer(text or ""):
        u = m.group(0).rstrip(".,)]}\"'")
        if u not in seen:
            seen.add(u)
            urls.append(u)
    return urls


def _humanize_skill_name(skill_name: str) -> str:
    """`render-shot-segmentation` -> `Shot Segmentation`: a tab title naming the work.

    Strips the `render-` prefix (routing convention, not meaning) and titleizes the rest.
    """
    text = str(skill_name or "").strip()
    if text.lower().startswith("render-"):
        text = text[len("render-"):]
    words = [w for w in _re.split(r"[-_\s]+", text) if w]
    return " ".join(w[:1].upper() + w[1:] for w in words)


def _label_from_url(url: str) -> str:
    """Human-friendly track label from the URL's filename (no extension)."""
    path = url.split("?", 1)[0].split("#", 1)[0]
    fname = path.rsplit("/", 1)[-1]
    stem = fname.rsplit(".", 1)[0] if "." in fname else fname
    return stem or "track"


def _ext_from_url(url: str) -> str:
    """Return the lowercased file extension (no dot) from a URL path."""
    path = url.split("?", 1)[0].split("#", 1)[0]
    if "." in path.rsplit("/", 1)[-1]:
        return path.rsplit(".", 1)[-1].lower()
    return ""



# --- s3:// resolution -------------------------------------------------------
# Artifact refs handed to the Render Agent are canonical `s3://bucket/key` URIs
# (that is what the job registry stores). The URL extractor only matches http(s),
# so an s3 ref produced a NO_URL error manifest and the renderer showed
# "Field duration must be >= 0.001, got 0". Resolve s3 refs to fetchable URLs via
# the media gateway's download-url tool (least privilege: no raw S3 IAM on this
# runtime) before building the manifest.
from concurrent.futures import ThreadPoolExecutor as _ThreadPool
from mcp_manager import MCPManager
from gateway.registry import GatewayRegistry

GATEWAY_REGISTRY_TABLE = os.environ.get("GATEWAY_REGISTRY_TABLE", "")
# http-only matcher used when scraping a gateway response for the presigned URL.
_URL_IN_TEXT = _re.compile(r"https?://[^\s\"'<>)\\]+")

# Module state the resolution helpers rely on. Names must match the helpers
# exactly — a mismatched name only fails at RUNTIME (NameError inside the
# request), which is how a wrong name shipped once already.
# `mcp_manager` here is an INSTANCE, not the module: the helpers call
# .load_gateways_from_registry() / .connect_all() / ._gateways on it, which are
# MCPManager methods. Importing the module instead type-checks and compiles, then
# fails at request time with AttributeError. Mirrors task_agent.py.
mcp_manager = MCPManager()

# Without this the registry stays None, _ensure_gateways_loaded returns
# immediately, and resolution becomes a SILENT no-op: s3:// refs pass through
# unresolved and the manifest comes back NO_URL with nothing logged. Mirrors
# task_agent's initialization.
_gateway_registry: Optional[GatewayRegistry] = None
if GATEWAY_REGISTRY_TABLE:
    try:
        import boto3 as _gw_boto3
        _gw_ddb = _gw_boto3.resource("dynamodb", region_name=REGION)
        _gateway_registry = GatewayRegistry(ddb_table=_gw_ddb.Table(GATEWAY_REGISTRY_TABLE))
        logger.info("Render Agent GatewayRegistry initialized against %s", GATEWAY_REGISTRY_TABLE)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Render Agent: failed to init GatewayRegistry: %s", exc)
        _gateway_registry = None
else:
    logger.info("GATEWAY_REGISTRY_TABLE not set; render agent will not resolve s3:// via gateway")
_gateway_initialized = False
_gateway_init_lock = asyncio.Lock()
_resolved_url_cache: dict = {}

async def _ensure_gateways_loaded() -> None:
    """Cold-load + connect gateways from the registry on first invoke.

    Mirrors ``task_agent._ensure_gateways_loaded`` (minus job tracking). Gives
    the render agent the SAME gateway tools the task agent already uses — in
    particular the media-outputs ``get_download_url`` tool it needs to resolve
    gateway-managed ``s3://`` artifacts to fetchable http URLs. Idempotent and
    concurrency-safe.
    """
    global _gateway_initialized
    if _gateway_initialized:
        return
    async with _gateway_init_lock:
        if _gateway_initialized:
            return
        if _gateway_registry is None:
            _gateway_initialized = True
            return
        try:
            loaded = await mcp_manager.load_gateways_from_registry(_gateway_registry)
            logger.info("Render Agent loaded %d gateway(s): %s", len(loaded), loaded)
        except Exception as e:
            logger.error("Render Agent: failed to load gateways: %s", e)
            _gateway_initialized = True
            return
        try:
            results = await asyncio.to_thread(mcp_manager.connect_all)
            for gw_id, result in results.items():
                logger.info("Render Agent gateway '%s': %s", gw_id, result.get("status", "unknown"))
        except Exception as e:
            logger.error("Render Agent: failed to connect gateways: %s", e)
        _gateway_initialized = True

def _tool_arg_name(tool) -> Optional[str]:
    """Pick the input property that takes the S3 object reference.

    Read from the tool's own (externally federated) ``inputSchema`` so nothing
    is hardcoded: prefer a required string property, preferring conventional
    names, else the only string property.
    """
    mcp_tool = getattr(tool, "mcp_tool", None)
    schema = getattr(mcp_tool, "inputSchema", None) or getattr(mcp_tool, "input_schema", None)
    if not isinstance(schema, dict):
        return None
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        return None
    required = [r for r in (schema.get("required") or []) if isinstance(r, str)]
    strings = [k for k, v in props.items() if isinstance(v, dict) and v.get("type") in (None, "string")]
    preferred = ("s3_uri", "s3uri", "uri", "url", "s3_key", "key", "object_key", "asset_s3uri", "path")
    for name in preferred:
        for cand in (required or strings):
            if cand.lower() == name:
                return cand
    for cand in (required or strings):
        if cand in props:
            return cand
    return None

def _resolve_one(gw, tool, arg: str, s3_uri: str) -> Optional[str]:
    """Call the gateway download-url tool once and pull the http URL out of its
    response. Tolerant of the response shape (text/content/JSON)."""
    client = getattr(gw, "_client", None)
    tool_name = getattr(tool, "tool_name", "") or getattr(tool, "name", "")
    if client is None or not tool_name:
        return None
    try:
        # Strands signature: call_tool_sync(tool_use_id, name, arguments).
        result = client.call_tool_sync(
            f"render-presign-{uuid.uuid4().hex[:8]}", tool_name, {arg: s3_uri}
        )
    except Exception as exc:
        logger.warning("render: download-url call failed for %s: %s", s3_uri, exc)
        return None
    blob = json.dumps(result, default=str)
    # Take the LAST http URL that isn't obviously diagnostic — an error payload
    # can embed doc links (e.g. errors.pydantic.dev), and grabbing one of those
    # would silently poison the manifest with an unplayable track source.
    candidates = [
        u.rstrip('\\"') for u in _URL_IN_TEXT.findall(blob)
        if not any(bad in u for bad in ("errors.pydantic.dev", "docs.", "schema.org", "json-schema.org"))
    ]
    if not candidates:
        logger.warning("render: no http URL in download-url response for %s: %s", s3_uri, blob[:200])
        return None
    # Prefer a candidate that still carries the object's file extension.
    tail = s3_uri.rsplit("/", 1)[-1].split("?", 1)[0]
    for u in candidates:
        if tail and tail in u:
            return u
    return candidates[0]

def _download_url_tools() -> list:
    """Connected gateway tools that resolve an object to a download/http URL
    (e.g. ``media-outputs___get_download_url``)."""
    tools = []
    for gw in mcp_manager._gateways.values():
        if not getattr(gw, "is_connected", False):
            continue
        for t in gw.get_all_tools():
            name = getattr(t, "tool_name", "") or getattr(t, "name", "") or ""
            if "download_url" in name:
                tools.append((gw, t, name))
    return tools


async def _resolve_s3_urls_via_gateway(urls: list, *, budget_s: float = 60.0) -> list:
    """Resolve gateway-managed ``s3://`` URIs to fetchable http URLs.

    The render agent OWNS this resolution (single owner, least privilege — no
    direct proxy-bucket S3 IAM). It calls the gateway's own download-url tool
    DETERMINISTICALLY (no LLM): the tool's input property is discovered from its
    published ``inputSchema``, so nothing about the externally-federated schema
    is hardcoded. Deterministic calls are ~0.3s each vs ~6s for an LLM
    tool-loop, which is what previously blew the budget on multi-asset plans and
    silently left unplayable ``s3://`` track sources in the manifest.

    Non-``s3://`` URLs pass through; anything unresolved is left as-is (best
    effort — never blocks a render).
    """
    s3_urls = [u for u in urls if isinstance(u, str) and u.startswith("s3://")]
    if not s3_urls:
        return urls
    await _ensure_gateways_loaded()
    dl = _download_url_tools()
    if not dl:
        logger.warning("render: no gateway download-url tool; passing %d s3:// URI(s) through", len(s3_urls))
        return urls
    gw, tool, tool_name = dl[0]
    arg = _tool_arg_name(tool)
    if not arg:
        logger.warning("render: could not determine %s input arg from its schema", tool_name)
        return urls

    todo = [u for u in dict.fromkeys(s3_urls) if u not in _resolved_url_cache]

    def _run_all() -> None:
        # Presign in parallel — a multi-asset plan was paying ~0.3s per asset
        # sequentially. Each call is an independent MCP round trip.
        if not todo:
            return
        with _ThreadPool(max_workers=min(8, len(todo))) as pool:
            for u, resolved in zip(todo, pool.map(lambda x: _resolve_one(gw, tool, arg, x), todo)):
                if resolved:
                    _resolved_url_cache[u] = resolved
            
    try:
        await asyncio.wait_for(asyncio.to_thread(_run_all), timeout=budget_s)
    except asyncio.TimeoutError:
        logger.warning("render: resolution budget (%ss) hit; %d/%d resolved",
                       budget_s, sum(1 for u in s3_urls if u in _resolved_url_cache), len(s3_urls))
    except Exception as exc:
        logger.warning("render: resolution failed (%s)", exc)

    # Fallback for refs the gateway download tool does NOT own (it only presigns the M2C proxy
    # bucket): self-presign whatever is still unresolved — typically a SOURCE upload in the app's
    # own skills bucket, which the render role can read. Mirrors _resolve_prompt_s3_refs. Without
    # it a skills-bucket source stays s3://, the DETERMINISTIC build fails ("not a clean manifest"),
    # and the render needlessly escalates to the slow LLM path and then times out (the trim/
    # people-detection source-video case, sessions 96198138 / 443eb166).
    for u in s3_urls:
        if u in _resolved_url_cache:
            continue
        try:
            bucket, key = u[len("s3://"):].split("/", 1)
            cli = _s3 or boto3.client("s3", region_name=REGION)
            _resolved_url_cache[u] = _public_or_presigned_url(cli, bucket, key)
        except Exception:  # noqa: BLE001 - best effort; still-unresolved passes through as before
            logger.warning("render: self-presign fallback failed for %s", u, exc_info=True)

    out = [(_resolved_url_cache.get(u) or u) if isinstance(u, str) else u for u in urls]
    logger.info("render: resolved %d/%d s3:// URI(s) via %s",
                sum(1 for u in s3_urls if u in _resolved_url_cache), len(s3_urls), tool_name)
    return out

# --- Skill-driven dispatch (Req 11) -----------------------------------------
# The Render Agent selects a Render_Skill by matching the artifact's MIME type
# / extension against each skill's `handles` frontmatter. Skills are the unit
# of extensibility: users improve rendering by adding render-* skills (local
# disk, S3, or Lab CRUD) that declare new `handles`. Built-in defaults ship
# for video and audio (render-video / render-audio).

_EXT_MIME = render_types.EXT_MIME_NODOT


def _ext_to_mime(ext: str) -> str:
    return _EXT_MIME.get(ext.lower(), "")


# Declaration keys that actually PROJECT a sidecar onto view primitives. A skill participates in
# correlation only if it declares one of these -- a `deliverable:`-only contract (render-audio,
# render-video) must NOT enter correlation, or its detect-less declaration would match every
# sidecar first and suppress the real projection (shots -> 0 markers).
_PROJECTION_DECL_KEYS = frozenset({"markers", "segments", "lanes", "boxes", "boxes_by",
                                   "rows", "thumbnails", "duration", "summary"})


def select_render_skill(mime_type: str, extension: str) -> Optional[dict]:
    """Return the render skill whose `handles` match this artifact, else None.

    Matches by MIME type or `.ext` token. Discovered (custom/local) skills are
    checked in order; the first match wins, so a customer skill that declares a
    MIME already handled by a built-in overrides it when it sorts first — the
    intended extension mechanism (Req 11.3). Returns None → caller uses the
    render-generic fallback (Req 11.4).
    """
    ext_token = f".{extension.lower()}" if extension else ""
    mime = (mime_type or "").lower()
    for skill in _discover_render_skills():
        # A skill declaring `detect:` is SPECIALISED and requires positive evidence — either
        # matching content (select_render_skill_by_content) or its declared tool
        # (select_render_skill_by_tool). A MIME match is NOT evidence: four analysis skills all
        # declare `handles: application/json`, so bare-MIME selection returned whichever sorted
        # first and rendered an UNKNOWN tool's json as a FACE view (`render-face-people`).
        # Confidently wrong is worse than generic, and an inherited tool has no way to opt out.
        # Only MEDIUM skills (video/audio/image/document/data — no `detect:`) are MIME-selectable.
        if (skill.get("declaration") or {}).get("detect"):
            continue
        handles = skill.get("handles", set())
        if (mime and mime in handles) or (ext_token and ext_token in handles):
            return skill
    return None


# A skill declaring any of these projects findings ONTO A TIMELINE, so it needs a media
# canvas rather than a still gallery.
_TIMELINE_DECL_KEYS = frozenset({"markers", "segments", "lanes"})


def _declares_timeline_view(skill: Optional[dict]) -> bool:
    """True when this skill's view is a timeline (markers / segments / lanes).

    Used to stop the images-only branch pre-empting a timeline view: a findings skill's
    canvas often arrives as a SOURCE video rather than a deliverable, while the tool also
    ships stills (shot thumbnails, speaker avatars). Without this, the stills won and the
    markers were dropped.
    """
    if not skill:
        return False
    return bool(_TIMELINE_DECL_KEYS & set((skill.get("declaration") or {}).keys()))


def images_only_view_wins(image_urls, media_urls, doc_urls, timeline_media, skill) -> bool:
    """Should the images-only gallery view be chosen over a timeline view?

    Extracted as a pure predicate so the branch ORDER is explicit and testable — it was
    an inline `elif` inside a ~200-line async generator, which is how the bug below
    survived: `media_urls` counts DELIVERABLES only, so a findings tool that ships stills
    (shot thumbnails) but whose canvas is a SOURCE video took the gallery branch and
    dropped its markers.

    Gallery wins only when there are images, nothing else renderable, AND we are not
    about to discard a declared timeline view that has a canvas to draw on -- including a box
    overlay (faces): boxes are drawn on the source video when one exists, not on a still.
    """
    if not image_urls or media_urls or doc_urls:
        return False
    return not (timeline_media and (_declares_timeline_view(skill) or _projects_onto_source(skill)))


def select_render_skill_by_content(sidecars: dict) -> Optional[dict]:
    """Return the skill whose ``detect:`` paths are present in these payloads.

    MIME cannot disambiguate: five media tools all emit ``application/json``,
    so extension-based selection returns whichever JSON-handling skill sorts
    first. That is how a shot-segmentation result picked up the face skill and
    rendered as a thumbnail grid instead of a marker timeline. Content is the
    authority — a skill applies when the payload actually contains what it
    declares it needs.

    Several skills can legitimately match one payload — a face result carries
    both ``people`` (render-face-people) and ``tracks`` (render-face-recognition).
    Among those whose prerequisites are present, the winner is the one that
    surfaces the MOST of the payload: primary key is the number of projections
    it declares, tie-broken on how many ``detect`` paths matched. Ranking on
    detect count first would be wrong — declaring two detect paths makes a
    skill broader, not more specific, and it demoted the richer face view.

    Skills with no ``detect`` are skipped: they always "apply", so including
    them would match everything and defeat the purpose.

    Returns None when nothing matches → caller keeps its MIME-based choice.
    """
    best: Optional[dict] = None
    best_score = (0, 0, 0)
    for skill in _discover_render_skills():
        declaration = skill.get("declaration") or {}
        detect = str(declaration.get("detect") or "").strip()
        if not detect:
            continue
        paths = [p.strip() for p in detect.split(",") if p.strip()]
        matched = 0
        for data in (sidecars or {}).values():
            if not isinstance(data, (dict, list)):
                continue
            hits = sum(1 for path in paths
                       if _proj.resolve_path(data, path) not in (None, [], {}))
            matched = max(matched, hits)
        if not matched:
            continue
        # `detect` gates applicability and `deliverable` is the output contract -- neither is a
        # projection, so exclude both from the richness score (adding a deliverable must not
        # change which skill wins).
        proj_keys = [k for k in declaration if k not in ("detect", "deliverable")]
        # A drawable BOX OVERLAY (boxes / boxes_by) is the primary, media-native visual, so it
        # ranks ABOVE lane/table/thumbnail-only views. Without this, a face result picked
        # render-face-people (composite-timeline, no boxes) over render-face-recognition
        # (video-annotations, boxes-on-video labeled by name from facegroup.json) purely because
        # face-people declares more projection keys — the "boxes never drawn" regression (b4ed0dd).
        # Generic: ANY box-declaring skill gets priority, and it only competes when its own `detect`
        # paths matched the payload, so non-face skills are unaffected.
        has_boxes = 1 if ("boxes" in declaration or "boxes_by" in declaration) else 0
        score = (has_boxes, len(proj_keys), matched)
        if score > best_score:
            best_score = score
            best = skill
    return best


def _declared_image_keys(sidecars: dict) -> list[str]:
    """Stored image keys that skills declare for their entities.

    Reads `thumbnails:` declarations, so this stays generic: any tool that names an
    image per entity is covered without naming the tool here.
    """
    try:
        declared = [s for s in _discover_render_skills() if (s.get("declaration") or {}).get("thumbnails")]
    except Exception:  # noqa: BLE001
        return []
    keys: list[str] = []
    for data in sidecars.values():
        if not isinstance(data, (dict, list)):
            continue
        for spec in declared:
            decl = spec["declaration"]
            if not _proj.declaration_applies(data, decl):
                continue
            for t in _proj.project_thumbnails(data, decl["thumbnails"]):
                k = t.get("key")
                if k and k not in keys:
                    keys.append(k)
            break
    return keys


_VHOST_S3_RE = _re.compile(r"^https?://([a-z0-9.\-]+)\.s3[.\-][a-z0-9.\-]*amazonaws\.com/", _re.I)
_PATH_S3_RE = _re.compile(r"^https?://s3[.\-][a-z0-9.\-]*amazonaws\.com/([^/]+)/", _re.I)


def _bucket_from_refs(urls: list[str]) -> Optional[str]:
    """Bucket named by any reference in the request, canonical OR already resolved.

    Canonical ``s3://`` refs are rewritten to https early in the request, so by the
    time declared images are resolved there may be no ``s3://`` left — which is why
    thumbnails silently failed to resolve on the first deployed run. Virtual-hosted
    and path-style S3 URLs are therefore parsed as well.
    """
    for u in urls:
        if not isinstance(u, str):
            continue
        if u.startswith("s3://"):
            bucket = u[5:].split("/", 1)[0].strip()
            if bucket:
                return bucket
        m = _VHOST_S3_RE.match(u) or _PATH_S3_RE.match(u)
        if m and m.group(1):
            return m.group(1)
    return None


async def _resolve_declared_images(sidecars: dict, request_urls: list[str]) -> list[str]:
    """Turn declared image keys into fetchable URLs via the gateway.

    A result can reference its images by stored key while never listing them as
    artifacts — face recognition names a `thumbnail_key` per person, yet the job's
    artifact list carried only JSON. Without this the images exist, are named, and
    still cannot be shown. Resolution reuses the gateway download-url path that
    already handles canonical refs, so no extra S3 permissions are involved.

    Best-effort: returns [] when there is no bucket to resolve against or the gateway
    cannot help, leaving the caller's existing fallbacks in place.
    """
    keys = _declared_image_keys(sidecars)
    if not keys:
        return []
    bucket = _bucket_from_refs(request_urls)
    if not bucket:
        logger.info("render: %d declared image(s) but no canonical bucket to resolve against",
                    len(keys))
        return []
    refs = [f"s3://{bucket}/{k.lstrip('/')}" for k in keys]
    try:
        resolved = await _resolve_s3_urls_via_gateway(refs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("render: could not resolve declared images: %s", exc)
        return []
    out = [u for u in (resolved or []) if isinstance(u, str) and not u.startswith("s3://")]
    logger.info("render: resolved %d/%d declared image(s)", len(out), len(keys))
    return out


def _fetch_json_many(urls: list[str]) -> dict:
    """Fetch several JSON sidecars concurrently -> {url: parsed-or-None}.

    These are independent network reads with a per-request timeout, so doing them
    one after another makes the render cost the SUM of the timeouts. A job with
    several data artifacts paid that serially on every render.

    A failed fetch is recorded as None rather than omitted, so callers can tell
    "not attempted" from "attempted and unavailable" and do not retry a URL that
    just cost a full timeout. Callers wanting only usable payloads should filter.
    """
    out: dict = {}
    todo = [u for u in urls if u]
    if not todo:
        return out
    if len(todo) == 1:
        out[todo[0]] = _fetch_json(todo[0])
        return out
    with _ThreadPool(max_workers=min(8, len(todo))) as pool:
        for url, data in zip(todo, pool.map(_fetch_json, todo)):
            out[url] = data
    return out


def _container_duration(url: str, timeout: float = 4.0) -> Optional[float]:
    """Real duration for an MP4/MOV from its container header, via ranged reads.

    Nothing else can supply one for video: media_probe does not inspect media and
    peak extraction is WAV-only, so without this a video render falls back to a fixed
    hint and a short clip draws on a ten-minute timeline. Best-effort by design —
    any failure returns None and the caller keeps its fallback.
    """
    from media.artifact_fetch import is_own_artifact, read_artifact_bytes

    own = is_own_artifact(url)

    def fetch(start: int, length: int) -> Optional[bytes]:
        rng = f"bytes={start}-{start + length - 1}" if start >= 0 else f"bytes=-{length}"
        if own:
            # Our artifact -> ranged get_object by key (the CDN may be private).
            return read_artifact_bytes(url, rng)[:length]
        # Not ours -> the shared SSRF guard (https, public addresses only, pinned).
        from security import url_guard  # noqa: PLC0415
        return url_guard.fetch(url, max_bytes=length, timeout=timeout, headers={"Range": rng},
                               truncate=True).body

    try:
        return _find_mvhd(fetch)
    except Exception as exc:  # noqa: BLE001
        logger.info("container duration unavailable for %s: %s", url.split("/")[-1], exc)
        return None


# shots.json -> shot markers, fixed in CODE (the same mapping render-shot-segmentation declares).
# A shot timeline is mechanical; when the catalog was unavailable or another declaration claimed
# the payload first, the render used to fall through to "JSON is data, not a view" and then to an
# LLM fallback that wrote ONE marker the UI could not draw (staging session 6ac33e9e).
_SHOTS_SKILL_NAME = "render-shot-segmentation"
_SHOTS_MARKERS_SPEC = "shots[] time=start_time end=end_time label=shot_id category=shot_type"
_SHOTS_DURATION_SPEC = "actual_duration_sec"


def _shot_markers_from_shots_json(data) -> tuple[list, Optional[float]]:
    """``(markers, duration)`` for a shots.json payload (``{shots: [{start_time, ...}]}``), else ``([], None)``."""
    if not (isinstance(data, dict) and isinstance(data.get("shots"), list) and data["shots"]):
        return [], None
    try:
        return (_proj.project_markers(data, _SHOTS_MARKERS_SPEC),
                _proj.project_duration(data, _SHOTS_DURATION_SPEC))
    except Exception:  # noqa: BLE001 - a malformed payload projects nothing
        return [], None


def _correlate_sidecars(sidecar_urls: list[str],
                        prefetched: Optional[dict] = None) -> dict:
    """Project analysis sidecars onto template props using skill DECLARATIONS.

    This is the deterministic assembly half of bounded generation: the mapping
    from a tool's JSON shape to view primitives is declared as data in a render
    SKILL.md, and applied here generically. Supporting a new analysis tool is a
    new skill file, not a new branch in this function.

    `prefetched` supplies payloads the caller already downloaded, so a sidecar is
    never fetched twice for one render.

    Returns the union of everything the matching skills could project:
    {markers, segments, annotations, tables, duration, summary, skills}.
    """
    found: dict = {"markers": [], "segments": [], "annotations": [], "thumbnails": [],
                   "tables": [], "duration": None, "summary": {}, "skills": []}
    if not sidecar_urls:
        return found
    try:
        skills = _discover_render_skills()
    except Exception as exc:  # noqa: BLE001 - never fail a render over skills
        logger.warning("render: skill specs unavailable: %s", exc)
        skills = []

    declared = [s for s in skills if _PROJECTION_DECL_KEYS & set((s.get("declaration") or {}).keys())]
    if not declared:
        logger.info("render: no render skills declare projections; only built-in shot markers apply")

    have = dict(prefetched or {})
    missing = [u for u in sidecar_urls if u not in have]
    if missing:
        have.update(_fetch_json_many(missing))

    for url in sidecar_urls:
        data = have.get(url)
        if not isinstance(data, (dict, list)):
            continue
        markers_before = len(found["markers"])
        for spec in declared:
            decl = spec["declaration"]
            if not _proj.declaration_applies(data, decl):
                continue
            name = spec.get("name") or "render-skill"
            if "markers" in decl:
                found["markers"].extend(_proj.project_markers(data, decl["markers"]))
            if "segments" in decl:
                found["segments"].extend(_proj.project_segments(data, decl["segments"]))
            if "lanes" in decl:
                found["segments"].extend(_proj.project_lanes(data, decl["lanes"]))
            if "boxes" in decl:
                found["annotations"].extend(_proj.project_boxes(data, decl["boxes"]))
            if "boxes_by" in decl:
                found["annotations"].extend(_proj.project_boxes_by_entity(data, decl["boxes_by"]))
            if "thumbnails" in decl:
                found["thumbnails"].extend(_proj.project_thumbnails(data, decl["thumbnails"]))
            if "rows" in decl:
                tbl = _proj.project_rows(data, decl["rows"])
                if tbl.get("rows"):
                    found["tables"].append(tbl)
            if "duration" in decl:
                d = _proj.project_duration(data, decl["duration"])
                if d and (found["duration"] is None or d > found["duration"]):
                    found["duration"] = d
            if "summary" in decl:
                found["summary"].update(_proj.project_summary(data, decl["summary"]))
            if name not in found["skills"]:
                found["skills"].append(name)
            logger.info("render: skill %s matched %s", name, url.split("/")[-1].split("?")[0])
            break   # first matching declaration wins for a given sidecar
        if len(found["markers"]) == markers_before:
            # shots.json ALWAYS projects to shot markers, in code, whatever the catalog matched
            # (or failed to load) -- never left to the "JSON is data" bail-out or an LLM marker.
            shot_markers, shot_duration = _shot_markers_from_shots_json(data)
            if shot_markers:
                found["markers"].extend(shot_markers)
                if shot_duration and (found["duration"] is None or shot_duration > found["duration"]):
                    found["duration"] = shot_duration
                if _SHOTS_SKILL_NAME not in found["skills"]:
                    found["skills"].append(_SHOTS_SKILL_NAME)
                logger.info("render: built %d shot marker(s) from %s", len(shot_markers),
                            url.split("/")[-1].split("?")[0])

    # Several artifacts routinely restate one finding, so projecting each in turn
    # yields duplicates. Collapse on content, which also absorbs duplicates that
    # originate inside a single file (e.g. an identity indexed more than once).
    before = (len(found["tables"]), len(found["segments"]),
              len(found["annotations"]), len(found["thumbnails"]))
    found["tables"] = _proj.dedupe_tables(found["tables"])
    found["segments"] = _proj.dedupe_segments(found["segments"])
    found["annotations"] = _proj.dedupe_boxes(found["annotations"])
    found["thumbnails"] = _proj.dedupe_thumbnails(found["thumbnails"])
    after = (len(found["tables"]), len(found["segments"]),
             len(found["annotations"]), len(found["thumbnails"]))
    if before != after:
        logger.info("render: collapsed restated findings tables/segments/boxes/thumbs %s -> %s",
                    before, after)
    return found


def _thumbnail_gallery(thumbnails: list[dict], image_urls: list[str]) -> Optional[dict]:
    """Per-entity stored images -> a gallery, when no boxed frame is available.

    Some results name a thumbnail per entity instead of shipping boxes reachable by
    the renderer, so this is what actually shows WHO was found. Boxes are preferred
    when present; this is the next best view, and both beat a table of names alone.
    """
    if not thumbnails or not image_urls:
        return None
    images = []
    for t in thumbnails:
        url = _match_asset_url(t.get("key"), image_urls)
        if url:
            images.append({"src": url, "label": t.get("label") or "", "annotations": []})
    if not images:
        return None
    return {"src": images[0]["src"], "annotations": [], "images": images}


def _annotated_still(annotations: list[dict], image_urls: list[str]) -> Optional[dict]:
    """Pick the still with the most detections and convert its boxes for display.

    Detections carry the frame they came from (`frame_key`), and the extracted
    frames ship alongside the analysis, so boxes can be drawn on a real image even
    when the tool emits no video. Returns None when nothing lines up, so the caller
    falls back to a table rather than showing an empty picture.
    """
    if not annotations or not image_urls:
        return None
    by_frame = _proj.group_boxes_by_frame(annotations)
    if not by_frame:
        return None

    matched: list[tuple[str, str, list[dict]]] = []
    for key, boxes in by_frame.items():
        url = _match_asset_url(key, image_urls)
        if url:
            matched.append((key, url, boxes))
    if not matched:
        return None

    matched.sort(key=lambda m: (-len(m[2]), m[0]))
    _, best_url, best_boxes = matched[0]
    return {
        "src": best_url,
        "annotations": _proj.to_percent_boxes(best_boxes),
        # Every matched still, so a gallery can offer the rest.
        "images": [{"src": u, "label": k, "detections": len(b)} for k, u, b in matched],
    }


def _build_analysis_manifest(correlated: dict, urls: list[str], manifest_id: str,
                             image_urls: Optional[list[str]] = None) -> Optional[dict]:
    """Render a findings-only result (no playable media) from its projections.

    Returns None when the skills projected nothing, so the caller can fall through
    to a real error rather than showing an empty view.

    Preference order reflects how much a view actually explains: a table of named
    people is more use than a bare list of appearance spans, which in turn beats a
    file listing. Boxes are deliberately NOT used here — they need a video to draw
    on, and are surfaced instead as a note so the tab says why.
    """
    tables = correlated.get("tables") or []
    segments = correlated.get("segments") or []
    annotations = correlated.get("annotations") or []
    markers = correlated.get("markers") or []
    if not (tables or segments or annotations or markers):
        return None

    # A duration the tool reported describes the whole media; the spans only
    # describe where things happened in it. Using the last span would place a
    # person appearing one minute into a five-minute video at the far right.
    spans_end = max([s["end"] for s in segments]
                    + [m.get("endTime", m.get("time", 0.0)) for m in markers], default=0.0)
    stated = correlated.get("duration") or 0.0
    manifest: dict = {
        "id": manifest_id,
        "duration": round(max(spans_end, stated), 3),
        "tracks": [],
        "clips": [],
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "artifacts": _artifact_descriptors(urls),
    }
    if correlated.get("summary"):
        manifest["metadata"] = dict(correlated["summary"])
    if correlated.get("skills"):
        manifest["renderSkill"] = ", ".join(correlated["skills"])
        # Tab title from the skill that actually rendered (independent of the plan's
        # bookkeeping node_id like "t1", which titleized to "T1", and never an ARN).
        manifest["title"] = _humanize_skill_name(correlated["skills"][0])

    # Boxes on a real still beat any table: it is the only view that actually shows
    # what was detected. Preferred whenever the detections name their frame and
    # that frame is among the artifacts — no source video required.
    framed = _annotated_still(annotations, image_urls or []) \
        or _thumbnail_gallery(correlated.get("thumbnails") or [], image_urls or [])
    if framed:
        manifest["template"] = "image-annotated"
        manifest["src"] = framed["src"]
        manifest["images"] = framed["images"]
        # The thumbnail-gallery form carries no boxes of its own; keep the detected
        # ones rather than overwriting them with an empty list. They are what a later
        # render with the source video draws, and what the note below counts.
        manifest["annotations"] = framed["annotations"] or annotations
        if tables:
            manifest["tables"] = tables
        if segments:
            manifest["segments"] = segments
        if markers:
            manifest["markers"] = markers
        if correlated.get("summary"):
            manifest["metadata"] = dict(correlated["summary"])
        if not framed["annotations"] and annotations:
            manifest.setdefault("notes", []).append(
                f"{len(annotations)} detections available; supply the source video to overlay them."
            )
        return manifest

    if tables:
        manifest["template"] = "data-table"
        primary = tables[0]
        manifest["columns"] = primary["columns"]
        manifest["rows"] = primary["rows"]
        # Only the OTHERS: the promoted table is already the top-level rows, and
        # repeating it here made the same people appear twice in the manifest.
        if len(tables) > 1:
            manifest["tables"] = tables[1:]
    elif segments:
        manifest["template"] = "composite-timeline"
    elif markers and not annotations:
        # Shot markers with no media to play: the timeline container still draws the marker lane
        # (NleTimeline renders markers without tracks), and a later render that HAS the source
        # folds onto it. A data dump or "nothing to display" is the failure this replaces.
        manifest["template"] = "nle-timeline"
    else:
        manifest["template"] = "data-table"
        manifest["columns"] = [{"key": "label", "label": "Label", "sortable": True},
                               {"key": "time", "label": "Time (s)", "sortable": True}]
        manifest["rows"] = [{"label": a.get("label", ""), "time": a.get("time")}
                            for a in annotations]

    if markers:
        manifest["markers"] = markers
    if segments:
        manifest["segments"] = segments
    if annotations:
        # Kept on the manifest so a later render that DOES have the source video
        # can draw them, and so the count is visible rather than silently lost.
        manifest["annotations"] = annotations
        manifest.setdefault("notes", []).append(
            f"{len(annotations)} detections available; supply the source video to overlay them."
        )
    return manifest


def _build_media_manifest(urls: list[str], manifest_id: str, render_options: dict,
                          prefetched: Optional[dict] = None) -> dict:
    """Build one Render_Manifest deterministically from one or more media URLs.

    No LLM loop. Each URL becomes its own labeled track so related artifacts
    (e.g. the speech/music/sfx stems of a single job) render as ONE unified
    multi-track timeline. Peak extraction is best-effort (WAV only) per audio
    track and never blocks manifest production. Durations are hints; the
    frontend clamps to the real media duration on load (Req 8.5).
    """
    if isinstance(urls, str):
        urls = [urls]
    urls = [u for u in urls if u]
    if not urls:
        return _build_error_manifest("NO_URL", "No artifact URL provided")

    duration_hint = float(render_options.get("durationHint") or 0) or 600.0
    extract_peaks = render_options.get("extractPeaks", True)

    tracks: list[dict] = []
    peaks: dict = {}
    mime_type: Optional[str] = None
    unsupported: list[str] = []
    # Disambiguate duplicate labels/ids across tracks.
    used_ids: set[str] = set()

    # Analysis sidecars are not "unsupported" — they carry the tool's FINDINGS.
    # Lumping them in with unsupported files is why a 9-shot segmentation rendered
    # as one bare track: the shots were in shots.json and never looked at.
    sidecar_urls: list[str] = []
    media_urls: list[str] = []
    image_urls: list[str] = []
    for u in urls:
        e = _ext_from_url(u)
        if e in _VIDEO_EXTS or e in _AUDIO_EXTS:
            media_urls.append(u)
        elif e == "json":
            sidecar_urls.append(u)
        elif e in _IMAGE_EXTS:
            # Stills are evidence, not junk. Face detections name the frame they
            # were found in, so these are exactly what the boxes draw on.
            image_urls.append(u)
        else:
            unsupported.append(u)

    correlated = _correlate_sidecars(sidecar_urls, prefetched)

    for url in media_urls:
        ext = _ext_from_url(url)

        # Probe (best-effort — metadata only, non-fatal).
        try:
            probe_raw = media_probe(url)
            probe_meta = json.loads(probe_raw) if isinstance(probe_raw, str) else {}
            if not mime_type and probe_meta.get("mime_type"):
                mime_type = probe_meta["mime_type"]
        except Exception as e:
            logger.warning("media_probe failed (non-fatal) for %s: %s", url, e)

        is_video = ext in _VIDEO_EXTS
        track_type = "video" if is_video else "audio"
        label = _label_from_url(url)
        base_id = f"{track_type}-{label}".lower().replace(" ", "-")[:60]
        track_id = base_id
        n = 1
        while track_id in used_ids:
            n += 1
            track_id = f"{base_id}-{n}"
        used_ids.add(track_id)

        # Remember whether this end is a real measurement or just the generic
        # hint. A hinted end must never outrank a duration the tool itself
        # reported, which is how a 30s clip ended up on a 600s timeline.
        track_end = round(duration_hint, 3)
        end_is_hint = True

        # A video's real length is in its container header. Only consulted when
        # nothing better is known, so it never overrides a measurement.
        if end_is_hint and is_video:
            container = _container_duration(url)
            if container:
                track_end = container
                end_is_hint = False

        # Best-effort peak extraction for audio (WAV only; single attempt).
        if extract_peaks and ext == "wav":
            try:
                pk_raw = peak_extractor(url, 100)
                pk = json.loads(pk_raw) if isinstance(pk_raw, str) else {}
                if isinstance(pk.get("peaks"), list):
                    peaks[track_id] = pk["peaks"]
                    if pk.get("duration"):
                        track_end = round(float(pk["duration"]), 3)
                        end_is_hint = False
            except Exception as e:
                logger.warning("peak_extractor failed (non-fatal) for %s: %s", url, e)

        tracks.append({
            "id": track_id,
            "type": track_type,
            "src": url,
            "label": label,
            "start": 0.0,
            "end": track_end,
            "_endIsHint": end_is_hint,
        })

    if not tracks:
        # Some tools emit NO media at all — face recognition returns only JSON.
        # Erroring here is the "specialised visual replaced by a generic one"
        # failure: the richest result in the pipeline (93 tracks, per-face boxes,
        # 5 named people) showed up as an unsupported-format notice. If the skills
        # projected anything meaningful, render THAT instead.
        analysis = _build_analysis_manifest(correlated, urls, manifest_id, image_urls)
        if analysis:
            return analysis
        return _build_error_manifest(
            "UNSUPPORTED_FORMAT",
            "No supported media types (mp4/webm/mov/mp3/wav/flac) in: "
            + ", ".join(unsupported[:5]),
            source_url=urls[0],
        )

    manifest = {
        "id": manifest_id,
        "template": "nle-timeline",
        "duration": max((t["end"] for t in tracks), default=round(duration_hint, 3)),
        "tracks": tracks,
        "clips": [],
        "captions": [],
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
    }

    # --- Bounded assembly: choose a template from the VETTED INVENTORY based on
    # what the skills actually projected, and attach those props. The inventory is
    # RendererRegistry's registered templates; emitting anything else would render
    # as nothing, so selection is deliberately closed over these names.
    if correlated["markers"]:
        manifest["markers"] = correlated["markers"]
    if correlated["segments"]:
        manifest["segments"] = correlated["segments"]
    if correlated["annotations"]:
        manifest["annotations"] = correlated["annotations"]
    if correlated["tables"]:
        manifest["tables"] = correlated["tables"]
    if correlated["summary"]:
        manifest.setdefault("metadata", {}).update(correlated["summary"])
    if correlated["skills"]:
        manifest["renderSkill"] = ", ".join(correlated["skills"])

    # Duration precedence: a real measurement > a duration the tool reported >
    # the generic hint. A hinted end is not evidence, so when a sidecar states the
    # duration we also clamp the hinted tracks to it — otherwise a 30s clip keeps
    # drawing on a 600s timeline.
    if correlated["duration"]:
        real = correlated["duration"]
        for t in tracks:
            if t.get("_endIsHint"):
                t["end"] = real
                t["_endIsHint"] = False
        measured = max((t["end"] for t in tracks), default=0.0)
        manifest["duration"] = round(max(measured, real), 3)
    # Publish whether an end is measured or guessed, instead of discarding it.
    #
    # A track's end falls back to a fixed 600s hint when nothing can measure it: peak
    # extraction is WAV-only and the container probe reads the MP4/MOV mvhd box, so an mp3
    # music track -- what the audio models return -- is measurable by neither. A 5.04s track
    # was published as 600s and drew a ten-minute timeline.
    #
    # The right correction is not a better guess. The browser knows the real duration once the
    # media loads, so say which ends are estimates and let the player replace them. Asserting a
    # number we do not have is what made this look like a generation bug for days.
    for t in tracks:
        if t.pop("_endIsHint", False):
            t["endIsEstimate"] = True
    if any(t.get("endIsEstimate") for t in tracks):
        manifest["durationIsEstimate"] = True

    has_video = any(t["type"] == "video" for t in tracks)
    # Pick the template that READS the most of what correlation filled.
    #
    # This was a first-match priority chain, which silently discarded work: a node
    # carrying shots AND diarization fills both `markers` and `segments`, matched the
    # markers branch, and video-markers reads ONLY markers -- so the speaker segments
    # were computed and then dropped. Ordering by breadth instead of by position fixes
    # that without adding a template, because composite-timeline (NleTimeline) already
    # composes MarkerTrack with SegmentTable/SegmentLanes.
    #
    # Annotations no longer force an exclusive branch. That comment used to read "boxes over
    # video render only in video-annotations, and NleTimeline has no box overlay at all" -- and
    # it was STALE: the timeline container draws boxes on the frame (activeBoxesAt + per-box
    # label + a Faces toggle) AND puts each appearance on the shared time gutter. Every branch
    # below now routes to that one container, so the old tradeoff (an annotated result silently
    # dropping its markers/segments) is gone: correlation fills the fields and the container
    # renders all of them.
    if correlated["annotations"] and has_video:
        # Boxes over the source video, on the one timeline container.
        manifest["template"] = "nle-timeline"
    elif correlated["markers"] and correlated["segments"]:
        # Both kinds present, and only this template renders both.
        manifest["template"] = "composite-timeline"
    elif correlated["markers"] and has_video:
        # Shot boundaries on the timeline. ONE container for the time axis: the old
        # `video-markers` template rendered a <video> plus a clickable LIST of shots — a strict
        # subset of this data (src + markers) with no playhead, no zoom, no shared gutter — and
        # it made the SAME analysis look different depending on whether a sibling manifest
        # happened to render (alone: the list; composited: a timeline). `nle-timeline` composes
        # MarkerTrack in the unified gutter, so nothing is lost and the divergence is gone.
        # The name `video-markers` remains registered UI-side so stored manifests still resolve.
        manifest["template"] = "nle-timeline"
        # Kept: the UI synthesizes a track from `src` when a manifest declares none, which is
        # what lets older marker-only manifests keep playing in this container.
        manifest["src"] = next((t["src"] for t in tracks if t["type"] == "video"), "")
    elif correlated["segments"]:
        # Grouped lanes (e.g. one row per speaker).
        manifest["template"] = "composite-timeline"

    if peaks:
        manifest["peaks"] = peaks
    if mime_type:
        manifest["mimeType"] = mime_type
    if unsupported:
        manifest["errors"] = [{"code": "UNSUPPORTED_FORMAT", "message": f"Skipped unsupported artifact: {u}"} for u in unsupported[:5]]
    return manifest


def _artifact_descriptors(urls: list[str]) -> list[dict]:
    """Describe artifacts for download/preview cards: {url, label, mime, kind}."""
    out: list[dict] = []
    for u in urls:
        ext = _ext_from_url(u)
        mime = _ext_to_mime(ext) or "application/octet-stream"
        kind = "image" if ext in _IMAGE_EXTS else (
            "video" if ext in _VIDEO_EXTS else (
                "audio" if ext in _AUDIO_EXTS else (
                    "data" if ext in _JSON_EXTS or ext == "csv" else "file")))
        out.append({
            "url": u,
            "label": _label_from_url(u).replace("_", " ").replace("-", " ").strip() or "artifact",
            "mime": mime,
            "kind": kind,
        })
    return out


def _build_generic_manifest(urls: list[str], manifest_id: str) -> dict:
    """Fallback manifest for artifacts with no matching render skill (Req 11.4).

    Produces a `generic-preview` manifest carrying every artifact with a label,
    MIME type and coarse kind so the frontend can show an image gallery plus
    download cards instead of an empty timeline. No media tracks.
    """
    primary = urls[0] if urls else ""
    ext = _ext_from_url(primary)
    manifest = {
        "id": manifest_id,
        "template": "generic-preview",
        "duration": 0.001,
        "tracks": [],
        "clips": [],
        "captions": [],
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "sourceUrl": primary,
        "sources": urls,
        "artifacts": _artifact_descriptors(urls),
        "mimeType": _ext_to_mime(ext) or "application/octet-stream",
    }
    return manifest


# ---------------------------------------------------------------------------
# Video annotations (generic, structure-driven)
# ---------------------------------------------------------------------------
# Many analysis tools return JSON describing *where and when* something appears
# in the source media: a timestamp plus a normalized bounding box. That shape is
# recognized structurally here — no tool names, gateway ids, or field-name
# allowlists — and converted into a `video-annotations` manifest so the frontend
# can draw the boxes over the original media on a shared clock.

_BOX_FIELD_NAMES = ("box", "bbox", "boundingbox", "bounding_box", "rect", "region")
_BOX_KEY_SETS = (
    ("l", "t", "w", "h"),
    ("left", "top", "width", "height"),
    ("x", "y", "w", "h"),
    ("x", "y", "width", "height"),
)
_TIME_FIELD_NAMES = ("timestamp", "time", "time_sec", "timestamp_sec", "start_sec")
_MS_TIME_FIELD_NAMES = ("timestamp_ms", "time_ms", "begin_ms", "start_ms")
_ANNOTATION_HOLD_SECONDS = 1.0
_ANNOTATION_MAX_BRIDGE_SECONDS = 2.5
_MAX_ANNOTATIONS = 5000
_MAX_ENTITIES = 60


def _normalize_box(value) -> Optional[dict]:
    """Coerce a bounding box into normalized {x, y, width, height} (0–1).

    Accepts the common key spellings and 4-element sequences. Pixel-space boxes
    are rejected because without frame dimensions they cannot be positioned.
    """
    candidate: Optional[list] = None
    if isinstance(value, dict):
        lowered = {str(k).lower(): v for k, v in value.items()}
        for keys in _BOX_KEY_SETS:
            if all(k in lowered for k in keys):
                candidate = [_num(lowered[k]) for k in keys]
                break
    elif isinstance(value, (list, tuple)) and len(value) == 4:
        candidate = [_num(v) for v in value]
    if not candidate or any(n is None for n in candidate):
        return None
    x, y, w, h = candidate
    if w <= 0 or h <= 0 or max(abs(x), abs(y), w, h) > 1.5:
        return None
    return {
        "x": round(min(max(x, 0.0), 1.0), 5),
        "y": round(min(max(y, 0.0), 1.0), 5),
        "width": round(min(w, 1.0), 5),
        "height": round(min(h, 1.0), 5),
    }


def _record_time(node: dict) -> Optional[float]:
    """Seconds for a record carrying a second- or millisecond-based timestamp."""
    lowered = {str(k).lower(): v for k, v in node.items()}
    for key in _TIME_FIELD_NAMES:
        if key in lowered:
            t = _num(lowered[key])
            if t is not None:
                return t
    for key in _MS_TIME_FIELD_NAMES:
        if key in lowered:
            t = _num(lowered[key])
            if t is not None:
                return round(t / 1000.0, 3)
    return None


def _collect_timed_boxes(node, out: Optional[list] = None, depth: int = 0) -> list[dict]:
    """Find every {time, box} pair anywhere inside a JSON structure."""
    if out is None:
        out = []
    if depth > 8 or len(out) >= _MAX_ANNOTATIONS:
        return out
    if isinstance(node, dict):
        box = None
        for key, val in node.items():
            if str(key).lower() in _BOX_FIELD_NAMES:
                box = _normalize_box(val)
                if box:
                    break
        if box is None:
            box = _normalize_box(node)
        when = _record_time(node)
        if box and when is not None:
            entry = {"time": when, "box": box}
            confidence = _num(node.get("confidence") or node.get("score"))
            if confidence is not None:
                entry["confidence"] = confidence
            out.append(entry)
        for val in node.values():
            _collect_timed_boxes(val, out, depth + 1)
    elif isinstance(node, (list, tuple)):
        for val in node:
            _collect_timed_boxes(val, out, depth + 1)
    return out


def _find_entity_list(node, depth: int = 0) -> Optional[list]:
    """Shallowest list of dicts whose members carry timed boxes (the entities)."""
    if depth > 4:
        return None
    if isinstance(node, dict):
        for val in node.values():
            if isinstance(val, list) and val and all(isinstance(i, dict) for i in val):
                if any(_collect_timed_boxes(i) for i in val[:8]):
                    return val
        for val in node.values():
            found = _find_entity_list(val, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for val in node[:5]:
            found = _find_entity_list(val, depth + 1)
            if found:
                return found
    return None


def _entity_label(node: dict, index: int) -> str:
    for key in ("name", "label", "title", "celebrity_name", "celeb"):
        val = node.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()[:80]
    for key in ("person_id", "entity_id", "track_id", "id"):
        if key in node and isinstance(node[key], (str, int)):
            return f"Person {node[key]}"
    return f"Entity {index + 1}"


def _is_grouped_identity_list(entities: list) -> bool:
    """True if members look like GROUPED IDENTITIES (facegroup.json people[]) rather than
    raw per-detection tracks (face_tracks.json tracks[]).

    A grouped identity carries an identity key (person_id, or a real name) AND aggregates a
    nested ``tracks`` list — it is the display-preferred representation (2 named people beat
    57 anonymous tracks). A raw track carries only a track_id and its own faces. Used to make
    the annotations builder prefer the grouped list even when the raw list scores more boxes.
    """
    if not isinstance(entities, list) or not entities:
        return False
    for e in entities[:8]:
        if not isinstance(e, dict):
            continue
        has_identity = ("person_id" in e) or (isinstance(e.get("name"), str) and e.get("name").strip())
        aggregates_tracks = isinstance(e.get("tracks"), list)
        if has_identity and aggregates_tracks:
            return True
    return False


def _entity_appearances(node: dict) -> list[dict]:
    """Normalize an entity's appearance intervals into seconds."""
    out: list[dict] = []
    items = node.get("appearances")
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        start = _num(item.get("begin_ms"))
        end = _num(item.get("end_ms"))
        if start is not None and end is not None:
            start, end = start / 1000.0, end / 1000.0
        else:
            start = _num(item.get("start", item.get("startTime")))
            end = _num(item.get("end", item.get("endTime")))
        if start is None or end is None or end <= start:
            continue
        out.append({"start": round(start, 3), "end": round(end, 3)})
    return out


def _annotations_for_entity(entity_id: str, boxes: list[dict]) -> list[dict]:
    """Turn sampled boxes into time-bounded annotations.

    Detections are sampled (often ~1/second), so each box is held until the next
    sample when the gap is small, and otherwise for a short fixed window. This
    keeps overlays stable without inventing detections across long gaps.
    """
    ordered = sorted(boxes, key=lambda b: b["time"])
    out: list[dict] = []
    for i, box in enumerate(ordered):
        nxt = ordered[i + 1]["time"] if i + 1 < len(ordered) else None
        if nxt is not None and 0 < nxt - box["time"] <= _ANNOTATION_MAX_BRIDGE_SECONDS:
            end = nxt
        else:
            end = box["time"] + _ANNOTATION_HOLD_SECONDS
        entry = {
            "entityId": entity_id,
            "start": round(box["time"], 3),
            "end": round(end, 3),
            "box": box["box"],
        }
        if "confidence" in box:
            entry["confidence"] = box["confidence"]
        out.append(entry)
    return out


def _match_asset_url(reference: object, candidates: list[str]) -> Optional[str]:
    """Match a stored key/filename to one of the presigned artifact URLs."""
    if not isinstance(reference, str) or not reference.strip():
        return None
    basename = reference.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1].lower()
    if not basename:
        return None
    for candidate in candidates:
        if basename in candidate.split("?", 1)[0].lower():
            return candidate
    return None


def _sidecar_scalar(sidecars: dict, *names: str) -> Optional[float]:
    for data in sidecars.values():
        if not isinstance(data, dict):
            continue
        for name in names:
            if name in data:
                value = _num(data[name])
                if value is not None:
                    return value
    return None


def _build_video_annotations_manifest(
    video_url: str,
    sidecars: dict,
    image_urls: list[str],
    artifact_urls: list[str],
    manifest_id: str,
) -> Optional[dict]:
    """Compose a `video-annotations` manifest, or None when the data lacks boxes.

    Generic by construction: any result JSON that describes entities with
    timestamped normalized boxes renders as synchronized overlays on the source
    media, regardless of which tool produced it.
    """
    if not video_url:
        return None

    best_entities: Optional[list] = None
    best_count = 0
    best_grouped = False
    for data in sidecars.values():
        found = _find_entity_list(data)
        if not found:
            continue
        count = sum(len(_collect_timed_boxes(item)) for item in found)
        if count == 0:
            continue
        grouped = _is_grouped_identity_list(found)
        # Prefer a GROUPED-IDENTITY list (facegroup.json people[] — named, aggregating many
        # tracks) over a raw per-track list (face_tracks.json tracks[]), even when the raw
        # list scores more boxes. Job 8ec0687a grouped 57 tracks / 58 detections into 2 named
        # people (Kevin Kilner, Reid Scott), but selecting purely by box count rendered 57
        # anonymous "Person N" subjects. Within the same class, more boxes still wins.
        better = (grouped and not best_grouped) or (grouped == best_grouped and count > best_count)
        if best_entities is None or better:
            best_entities, best_count, best_grouped = found, count, grouped
    if not best_entities or best_count == 0:
        return None

    entities: list[dict] = []
    annotations: list[dict] = []
    appearances: list[dict] = []

    for index, raw in enumerate(best_entities[:_MAX_ENTITIES]):
        boxes = _collect_timed_boxes(raw)
        if not boxes:
            continue
        entity_id = f"entity-{index}"
        entity_annotations = _annotations_for_entity(entity_id, boxes)
        entity_appearances = _entity_appearances(raw) or [
            {"start": a["start"], "end": a["end"]} for a in entity_annotations
        ]
        entity: dict = {
            "id": entity_id,
            "label": _entity_label(raw, index),
            "detectionCount": len(entity_annotations),
            "appearanceCount": len(entity_appearances),
        }
        confidence = _num(raw.get("confidence"))
        if confidence is not None:
            entity["confidence"] = confidence
        screen_time = _num(raw.get("total_screen_time_sec", raw.get("screenTimeSec")))
        if screen_time is None:
            screen_time = round(sum(a["end"] - a["start"] for a in entity_appearances), 3)
        entity["screenTimeSec"] = screen_time
        for key in ("named", "name_source", "gender"):
            value = raw.get(key)
            if isinstance(value, (str, bool)):
                entity[key if key != "name_source" else "nameSource"] = value
        thumbnail = _match_asset_url(raw.get("thumbnail_key") or raw.get("thumbnail"), image_urls)
        if thumbnail:
            entity["thumbnail"] = thumbnail
        entities.append(entity)
        annotations.extend(entity_annotations)
        appearances.extend({"entityId": entity_id, **a} for a in entity_appearances)

    if not entities or not annotations:
        return None

    duration = _sidecar_scalar(sidecars, "duration_sec", "duration")
    max_end = max((a["end"] for a in annotations), default=0.0)
    if duration is None or duration <= 0:
        duration = max_end
    duration = round(max(duration, max_end, 0.001), 3)

    metadata: dict = {}
    for data in sidecars.values():
        if not isinstance(data, dict):
            continue
        stats = data.get("stats")
        if isinstance(stats, dict):
            metadata.update({k: v for k, v in stats.items() if isinstance(v, (str, int, float, bool))})
    metadata["entities"] = len(entities)
    metadata["detections"] = len(annotations)

    gallery = [u for u in image_urls if u]

    return {
        "id": manifest_id,
        "template": "video-annotations",
        "duration": duration,
        "tracks": [{
            "id": "source-video",
            "type": "video",
            "src": video_url,
            "label": _label_from_url(video_url),
            "start": 0.0,
            "end": duration,
        }],
        "clips": [],
        "captions": [],
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "media": {"src": video_url, "duration": duration},
        "entities": entities,
        "annotations": annotations,
        "appearances": appearances,
        "metadata": metadata,
        "gallery": gallery,
        "artifacts": _artifact_descriptors(artifact_urls),
        "sourceUrl": video_url,
        "mimeType": _ext_to_mime(_ext_from_url(video_url)) or "video/mp4",
    }


def _build_image_manifest(urls, manifest_id: str) -> dict:
    """One or more standalone images → the `image-annotated` template (gallery, no overlays).

    Images were never a render pipeline type (the 7 legacy skills had none) -- the UI displayed
    image urls directly. Now that the agent is primary and fires on artifacts, a bare image must
    still produce a real manifest instead of an empty one. Accepts a single url or a list: a
    text-to-image call that returns TWO options must render BOTH (ImageAnnotated renders the whole
    `images` array as a gallery); rendering only images[0] dropped the second option.
    """
    us = [urls] if isinstance(urls, str) else [u for u in urls if isinstance(u, str) and u]
    us = us or [""]
    return {
        "id": manifest_id,
        "template": "image-annotated",
        "src": us[0],
        "images": [{"src": u, "label": _label_from_url(u), "annotations": []} for u in us],
        "annotations": [],
        "duration": 0.001,
        "tracks": [],
        "clips": [],
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
    }


def _document_title(url: str) -> str:
    """The file name a document is listed under in the viewer's selector."""
    from urllib.parse import unquote
    return unquote(url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]) or "Document"


def _build_documents_manifest(urls: list, manifest_id: str) -> dict:
    """Document manifest for EVERY document of a render, not just the first.

    ``document`` stays the first (older viewers read only it); ``documents`` lists all of them, in
    order, and the viewer offers a selector when there is more than one. Staging session d6a560a8:
    three per-video compliance PDFs were dispatched together and only ``doc_urls[0]`` rendered
    ("2 candidate(s) not rendered"), while the reply claimed all three were open.
    """
    uniq: list = []
    for u in urls or []:
        if isinstance(u, str) and u and u not in uniq:
            uniq.append(u)
    manifest = _build_document_manifest(uniq[0], manifest_id)
    if len(uniq) > 1:
        manifest["documents"] = [
            {"previewUrl": u, "title": _document_title(u), "pageCount": None, "outline": [],
             "mimeType": _ext_to_mime(_ext_from_url(u)) or "application/pdf"}
            for u in uniq
        ]
    return manifest


def _build_document_manifest(url: str, manifest_id: str) -> dict:
    """Document manifest (Req 14). Template `document-viewer`.

    Minimal, reliable: hands the frontend a preview URL it can render natively
    (browser PDF viewer via the /api/artifact proxy). Page count / outline are
    optional and may be enriched from a sidecar JSON.
    """
    return {
        "id": manifest_id,
        "template": "document-viewer",
        "duration": 0.001,
        "tracks": [],
        "clips": [],
        "captions": [],
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "document": {"previewUrl": url, "pageCount": None, "outline": []},
        "sourceUrl": url,
        "mimeType": _ext_to_mime(_ext_from_url(url)) or "application/pdf",
    }


# --- Sidecar-JSON ingestion (composable views, decision 3) ------------------

_JSON_EXTS = {"json"}
_SUBTITLE_EXTS = {"vtt", "srt"}


def _read_artifact_or_url(url: str, timeout: float) -> bytes:
    """Body of ``url``: S3 ``get_object`` by key for OUR artifact URLs (the CDN may be
    private), the shared SSRF guard for anything else (the url comes from a tool result or
    the model, so it is not trusted: https, public addresses only, pinned, size-capped)."""
    from media.artifact_fetch import is_own_artifact, read_artifact_bytes

    if is_own_artifact(url):
        return read_artifact_bytes(url)
    from security import url_guard  # noqa: PLC0415
    return url_guard.fetch(url, max_bytes=_SIDECAR_MAX_BYTES, timeout=timeout).body


#: Cap on one fetched sidecar (JSON / VTT / SRT) body.
_SIDECAR_MAX_BYTES = 64 * 1024 * 1024


def _fetch_text(url: str, timeout: float = 5.0) -> Optional[str]:
    """Fetch a text artifact (e.g. a VTT/SRT caption file). Best-effort."""
    try:
        data = _read_artifact_or_url(url, timeout)
        return data.decode("utf-8", "replace")
    except Exception as e:
        logger.warning("caption fetch failed for %s: %s", url, e)
        return None


def _captions_from_subtitles(subtitle_urls: list[str]) -> list[dict]:
    """Fetch + parse VTT/SRT subtitle artifacts into normalized caption cues.

    The deterministic pipeline previously dropped .vtt/.srt as "unsupported",
    so a diarization job's subtitle sidecar never reached the timeline. Here we
    fetch each caption file, parse it via the shared caption_parser core, and
    normalize into {start,end,speaker,text} cues the frontend overlays on the
    media track(s).
    """
    cues: list[dict] = []
    for url in subtitle_urls:
        text = _fetch_text(url)
        if not text:
            continue
        try:
            parsed = parse_caption_content(text, "auto")
            cues.extend(_normalize_captions(parsed.get("cues", [])))
        except Exception as e:
            logger.warning("caption parse failed for %s: %s", url, e)
    return cues


def _fetch_json(url: str, timeout: float = 5.0) -> Optional[object]:
    """Fetch and parse a JSON sidecar artifact. Best-effort; None on failure."""
    try:
        data = _read_artifact_or_url(url, timeout)
        return json.loads(data.decode("utf-8"))
    except Exception as e:
        logger.warning("sidecar fetch failed for %s: %s", url, e)
        return None


def _num(v) -> Optional[float]:
    try:
        f = float(v)
        if f != f or f in (float("inf"), float("-inf")):
            return None
        return round(f, 3)
    except (TypeError, ValueError):
        return None


def _normalize_markers(items) -> list[dict]:
    """Coerce a list into valid marker entries: {id,time,label,category,...}."""
    out: list[dict] = []
    if not isinstance(items, list):
        return out
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        t = _num(it.get("time", it.get("start", it.get("timestamp"))))
        if t is None:
            continue
        end = _num(it.get("endTime", it.get("end")))
        m = {
            "id": str(it.get("id") or f"marker-{i}"),
            "time": t,
            "label": str(it.get("label") or it.get("name") or it.get("category") or f"Marker {i+1}"),
            "category": str(it.get("category") or it.get("type") or "event"),
        }
        if end is not None and end > t:
            m["endTime"] = end
        if it.get("color"):
            m["color"] = str(it["color"])
        if it.get("thumbnail") or it.get("thumbnailUrl"):
            m["thumbnail"] = str(it.get("thumbnail") or it.get("thumbnailUrl"))
        out.append(m)
    return out


def _normalize_segments(items) -> list[dict]:
    """Coerce a list into valid segment rows: {id,name,start,end,...}."""
    out: list[dict] = []
    if not isinstance(items, list):
        return out
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        start = _num(it.get("start", it.get("startTime")))
        end = _num(it.get("end", it.get("endTime")))
        if start is None or end is None or start >= end:
            continue
        seg = {
            "id": str(it.get("id") or f"seg-{i}"),
            "name": str(it.get("name") or it.get("label") or it.get("title") or f"Segment {i+1}"),
            "start": start,
            "end": end,
        }
        if it.get("sourceTrack"):
            seg["sourceTrack"] = str(it["sourceTrack"])
        extra = {k: v for k, v in it.items()
                 if k not in {"id", "name", "label", "title", "start", "end", "startTime", "endTime", "sourceTrack"}
                 and isinstance(v, (str, int, float, bool))}
        if extra:
            seg["fields"] = extra
        out.append(seg)
    return out


def _normalize_captions(items) -> list[dict]:
    out: list[dict] = []
    if not isinstance(items, list):
        return out
    for it in items:
        if not isinstance(it, dict):
            continue
        start = _num(it.get("start")); end = _num(it.get("end"))
        text = it.get("text"); speaker = it.get("speaker")
        if start is None or end is None or start >= end or not text:
            continue
        out.append({
            "start": start, "end": end,
            "speaker": str(speaker or "Speaker")[:50],
            "text": str(text)[:300],
        })
    return out


def _heuristic_sidecar_map(sidecars: dict) -> dict:
    """Deterministic best-effort mapping of sidecar JSON → composable sections.

    Fallback when the LLM populate path is unavailable or fails. Recognizes:
    - dicts already keyed by markers/segments/captions/metadata → used directly
    - a list of {start,end,...} → segments
    - a list of {time|timestamp,...} → markers
    - a flat dict of scalars → metadata
    """
    enr = {"markers": [], "segments": [], "metadata": {}, "captions": []}
    for _url, data in sidecars.items():
        if isinstance(data, dict):
            if isinstance(data.get("markers"), list):
                enr["markers"] += _normalize_markers(data["markers"])
            if isinstance(data.get("segments"), list):
                enr["segments"] += _normalize_segments(data["segments"])
            if isinstance(data.get("captions"), list):
                enr["captions"] += _normalize_captions(data["captions"])
            if isinstance(data.get("metadata"), dict):
                enr["metadata"].update({k: v for k, v in data["metadata"].items()
                                        if isinstance(v, (str, int, float, bool))})
            if not any(k in data for k in ("markers", "segments", "captions", "metadata")):
                enr["metadata"].update({k: v for k, v in data.items()
                                        if isinstance(v, (str, int, float, bool))})
        elif isinstance(data, list):
            seg = _normalize_segments(data)
            if seg:
                enr["segments"] += seg
            else:
                enr["markers"] += _normalize_markers(data)
    return enr


async def _populate_from_sidecars_llm(sidecars: dict, skill: Optional[dict],
                                      manifest: dict, budget_s: float = 15.0) -> dict:
    """Map sidecar JSON → composable sections via a single bounded LLM call.

    The LLM sees the skill's workflow (field-mapping guidance), the media
    context (track ids + duration), and the raw sidecar JSON, and returns a
    strict JSON object {markers, segments, metadata, captions}. No tools are
    provided, so it cannot loop. On timeout/parse/validation failure we fall
    back to the deterministic heuristic mapper (never blocks the manifest).
    """
    fallback = _heuristic_sidecar_map(sidecars)
    try:
        skill_workflow = ""
        if skill and skill.get("dir"):
            try:
                sd = skill["dir"]
                if not sd.startswith("s3://"):
                    skill_workflow = (Path(sd) / "SKILL.md").read_text()[:4000]
            except Exception:
                pass
        track_ctx = [{"id": t["id"], "type": t["type"]} for t in manifest.get("tracks", [])]
        sys_prompt = (
            "You map media sidecar JSON into a composable render manifest. "
            "Return ONLY a raw JSON object with keys: markers, segments, metadata, captions. "
            "markers: [{id,time,label,category}] (time in seconds). "
            "segments: [{id,name,start,end}] (seconds, start<end). "
            "captions: [{start,end,speaker,text}]. metadata: flat object of scalars. "
            "Use ONLY data present in the sidecars — never invent timings or content. "
            "Omit a key if you have no data for it. No prose, no code fences.\n\n"
            + ("Skill workflow guidance:\n" + skill_workflow if skill_workflow else "")
        )
        user_prompt = (
            f"Media tracks: {json.dumps(track_ctx)}\n"
            f"Duration: {manifest.get('duration')}\n\n"
            f"Sidecar artifacts:\n{json.dumps(sidecars)[:12000]}"
        )
        model = build_model(model_id=RENDER_MODEL_ID, boto_client_config=_boto_cfg)
        agent = Agent(model=model, system_prompt=sys_prompt, tools=[])

        async def _run():
            chunks: list[str] = []
            async for ev in agent.stream_async(user_prompt):
                if isinstance(ev, dict) and isinstance(ev.get("data"), str):
                    chunks.append(ev["data"])
            return "".join(chunks)

        text = await asyncio.wait_for(_run(), timeout=budget_s)
        raw = _extract_json_from_text(text)
        if not raw:
            return fallback
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return fallback
        return {
            "markers": _normalize_markers(parsed.get("markers", [])),
            "segments": _normalize_segments(parsed.get("segments", [])),
            "captions": _normalize_captions(parsed.get("captions", [])),
            "metadata": {k: v for k, v in (parsed.get("metadata") or {}).items()
                         if isinstance(v, (str, int, float, bool))},
        }
    except Exception as e:
        logger.warning("LLM sidecar populate failed (%s); using heuristic fallback", e)
        return fallback


async def _resolve_prompt_s3_refs(prompt: str, *, budget_s: float = 60.0) -> tuple:
    """Resolve every ``s3://`` ref in the render prompt to a fetchable http url via the media
    gateway's download-url tool. Rewrites by string-substitution to preserve the prompt's
    structure (candidate / source / already-rendered sections). Returns (rewritten, unresolved)."""
    s3_refs = [u for u in _extract_urls(prompt) if u.startswith("s3://")]
    if not s3_refs:
        return prompt, []
    resolved = await _resolve_s3_urls_via_gateway(s3_refs, budget_s=budget_s)
    mapping = {s: r for s, r in zip(s3_refs, resolved)
               if isinstance(r, str) and r and not r.startswith("s3://")}
    # Fallback: the gateway download tool only owns M2C OUTPUTS (the proxy bucket). Refs it can't
    # resolve -- notably SOURCE uploads in the app's own skills bucket -- are self-presigned here
    # (the render role owns that bucket; it writes manifests there). Gateway-first keeps the M2C
    # bucket least-privilege; the fallback keeps skills-bucket sources working. Without it, any
    # render carrying a skills-bucket source errored UNRESOLVED_S3 and showed nothing.
    for u in s3_refs:
        if u in mapping:
            continue
        try:
            bucket, key = u[len("s3://"):].split("/", 1)
            cli = _s3 or boto3.client("s3", region_name=REGION)
            mapping[u] = _public_or_presigned_url(cli, bucket, key)
        except Exception:  # noqa: BLE001 - fallback is best-effort; unresolved surfaces below
            logger.warning("render: self-presign fallback failed for %s", u, exc_info=True)
    out = prompt
    for s3_uri, http in mapping.items():
        out = out.replace(s3_uri, http)
    unresolved = [u for u in s3_refs if u not in mapping]
    return out, unresolved


_MEDIA_ID_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

# `artifacts/durable/<32-hex digest>[.<ext> | /<execUUID>/<basename> | /<basename>]`.
_DURABLE_KEY_RE = re.compile(
    r"(?:^|/)artifacts/durable/[0-9a-f]{32}"
    r"(?:\.[^/]*"
    r"|/(?P<uuid>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/(?P<base>[^/]+)"
    r"|/[^/]+)?$", re.I)


def _canonical_source_identity(norm: str) -> str:
    """Collapse the SAME media stored at DIFFERENT roots to ONE identity, so a derived clip's
    video render and every analysis of that clip merge onto ONE composite timeline (the mock),
    instead of one solo tab each.

    The trim/transcode output lives at two places with different key strings but the SAME media:
      - durable:   ``artifacts/<sid>/output/<execUUID>/meridian_trim.mp4`` (the clip's own render)
      - ephemeral: ``micro-workflow/media-processing/<execUUID>/trim_clip/meridian_trim.mp4``
                   (what a chained shot/stem analysis actually ran on)
    Both embed the SAME producing execution UUID + basename, so we key on ``<last-UUID>/<basename>``
    — location-independent. A url without any UUID (a plain CDN url, an uploaded original whose only
    UUID is its session dir) falls back to the normalised url unchanged, so unrelated media never
    collide. THE single identity authority: both manifest assemblers route through this.
    """
    if not norm:
        return norm
    path = norm.split("?", 1)[0]
    # Our own durable mirror (render/artifacts._durable_key). Parsed explicitly so the 32-hex
    # content digest segment can never stand in for an execution UUID:
    #   new:  durable/<digest>/<execUUID>/<basename>  -> <execUUID>/<basename> (same as the clip)
    #   new:  durable/<digest>/<basename>             -> unchanged (source had no exec id)
    #   old:  durable/<sha32>.<ext>                   -> unchanged (identity was lost at copy time)
    m = _DURABLE_KEY_RE.search(path)
    if m:
        if m.group("uuid") and m.group("base"):
            return f"{m.group('uuid').lower()}/{m.group('base')}"
        return norm
    base = path.rsplit("/", 1)[-1]
    uuids = _MEDIA_ID_UUID_RE.findall(path)
    if uuids and base:
        return f"{uuids[-1].lower()}/{base}"
    return norm


def _manifest_has_analysis(manifest) -> bool:
    """True when a manifest carries real ANALYSIS data (non-empty markers/segments/annotations/
    captions) — i.e. it PROJECTS onto a source rather than BEING a source. Distinguishes a shot/
    face/diarization render (analysis → keys to its source) from a plain clip/transcode (a new
    source → keys to itself). Empty init lists are falsy, so only populated analyses count."""
    if not isinstance(manifest, dict):
        return False
    return any(manifest.get(k) for k in ("markers", "segments", "annotations", "captions"))


def _source_url_for(source_video_urls: list, media: list, cands: list,
                    has_analysis: bool = False) -> str:
    """The url of the media a render is ABOUT (see :func:`derive_source_key` for the rule)."""
    media_video = [u for u in media if _ext_from_url(u) in _VIDEO_EXTS]
    if has_analysis:
        return (source_video_urls[0] if source_video_urls
                else (media_video[0] if media_video
                      else (media[0] if media else (cands[0] if cands else ""))))
    return (media_video[0] if media_video
            else (source_video_urls[0] if source_video_urls
                  else (media[0] if media else (cands[0] if cands else ""))))


def derive_source_location(source_video_urls: list, media: list, cands: list,
                           has_analysis: bool = False) -> str:
    """Canonical ``s3://bucket/key`` of the media a render is about (manifest ``sourceLocation``).

    The same source :func:`derive_source_key` keys on, normalised from its CloudFront /
    ``/api/artifact`` / presigned (skills bucket or M2C proxy bucket) / ``s3://`` form, so the UI
    can fold every view of one object onto one tab by LOCATION rather than by a url string (asset
    lineage R7.1, design §4/§6). "" when the source is not one of those forms.
    """
    src = _source_url_for(source_video_urls, media, cands, has_analysis)
    if not src:
        return ""
    try:
        from media.artifact_fetch import canonical_location
        return canonical_location(src) or ""
    except Exception:  # noqa: BLE001 - identity is best-effort
        return ""


def _stamp_source_identity(manifest, source_video_urls: list, media: list, cands: list) -> None:
    """Stamp ``sourceKey`` and ``sourceLocation`` (each only when absent) — the ONE place both
    manifest assemblers set source identity, so they can never drift."""
    if not isinstance(manifest, dict):
        return
    has_analysis = _manifest_has_analysis(manifest)
    if not manifest.get("sourceKey"):
        _sk = derive_source_key(source_video_urls, media, cands, has_analysis=has_analysis)
        if _sk:
            manifest["sourceKey"] = _sk
    if not manifest.get("sourceLocation"):
        _loc = derive_source_location(source_video_urls, media, cands, has_analysis=has_analysis)
        if _loc:
            manifest["sourceLocation"] = _loc


# Declarations that PROJECT findings onto the media they analysed (shot markers, speaker
# segments/lanes, face boxes). Such a view's first track is the SOURCE video.
_SOURCE_PROJECTION_DECL_KEYS = frozenset({"markers", "segments", "lanes", "boxes", "boxes_by"})


def _projects_onto_source(skill: Optional[dict]) -> bool:
    """True for an analysis skill (shots / diarization / faces): it draws on its source media."""
    if not skill:
        return False
    return bool(_SOURCE_PROJECTION_DECL_KEYS & set((skill.get("declaration") or {}).keys()))


def _analysis_timeline_media(media_urls: list, source_video_urls: list,
                             skill: Optional[dict]) -> list:
    """The tracks a timeline view is built on.

    An ANALYSIS render (shots / diarization / faces) is ABOUT its source video, so the source is
    the first track and the job's own media (a proxy segment, per-speaker audio) rides beside it:
    ``source_video_urls + media_urls``. It used to be ``media_urls or source_video_urls`` -- the
    source only when the job shipped no media at all -- so a diarization that also emitted speaker
    audio rendered audio-only, with no video to fold onto (staging 833a079f). Any other render keeps
    that rule: its own media IS the deliverable.
    """
    if source_video_urls and _projects_onto_source(skill):
        out: list = []
        seen: set = set()
        for u in list(source_video_urls) + list(media_urls):
            ident = u.split("?", 1)[0]
            if ident not in seen:
                seen.add(ident)
                out.append(u)
        return out
    return media_urls or source_video_urls


def derive_source_key(source_video_urls: list, media: list, cands: list,
                      has_analysis: bool = False) -> str:
    """THE single authority for a manifest's ``sourceKey`` — the stable identity of the
    SOURCE media a render is about, so the UI (composeTimeline) merges every analysis of
    the same source (stems, shots, faces, diarization) onto ONE composite timeline.

    A video output can be either a NEW source or an analysis canvas, so identity splits:
      - ANALYSIS render (``has_analysis`` — carries markers/segments/annotations/captions):
        it PROJECTS onto the source it analysed → key on ``source_video`` so it groups onto
        that source's tab. A shot render's own ``segment_0000.mp4`` is an analysis byproduct,
        NOT the identity — so never let a media video win here.
      - PLAIN MEDIA render (no analysis data — a clip/transcode/extracted audio): its OWN video
        is the identity (a derived clip is its own timeline, so later analyses of the CLIP group
        onto IT, not onto the original it was cut from). Audio-only deliverables (extract-audio,
        stems) have no video of their own → fall to the ``source_video`` they belong to.

    Normalised with ``_norm_url`` so a CloudFront mirror and its s3:// origin collapse to one
    key. Both manifest assemblers call THIS, so the key can never drift between them.
    Best-effort: returns "" if identity can't be derived.
    """
    try:
        from render.review import _norm_url as _nrm
    except Exception:  # noqa: BLE001
        return ""
    src = _source_url_for(source_video_urls, media, cands, has_analysis)
    try:
        return _canonical_source_identity(_nrm(src)) if src else ""
    except Exception:  # noqa: BLE001
        return ""


def assemble_manifest(*, artifact_urls: list, source_urls: list | None = None,
                      tool: object = None, manifest_id: str,
                      render_options: dict | None = None, fetched: dict | None = None,
                      extra_images: list | None = None,
                      candidate_keys: list | None = None) -> tuple[dict, bool, object]:
    """The ONE canonical manifest assembler. Classifies inputs, selects the render skill
    (MIME → tool → content), builds via the shared ``_build_*_manifest`` helpers, sets the
    template + ``renderSkill``, and stamps identity (``candidate_keys`` + ``sourceKey``)
    via :func:`derive_source_key`.

    Both entry points call this — the deterministic render entrypoint (``_render_deterministic``)
    and the ``build_render_manifest`` tool — so SELECTION and IDENTITY can never diverge (the
    drift that left every real manifest with ``sourceKey: null`` and broke the lane-merge).

    SYNC + deterministic. The async pre-steps (s3 resolution, JSON fetch, declared-image
    resolution) and post-steps (LLM sidecar enrichment, subtitle overlay, review) stay in the
    entrypoint wrapper — this covers the exact selection/build/stamp the two paths duplicated.

    Returns ``(manifest, projected)`` where ``projected`` is whether the builder produced
    markers/segments/annotations/tables (so the caller knows if LLM enrichment is needed).
    """
    urls = artifact_urls or source_urls or []
    source_urls = source_urls or []
    fetched = fetched or {}
    extra_images = extra_images or []
    # Clean every incoming url FIRST, in the one assembler both entry points share, so a
    # malformed src (a newline/blob-joined url, or ``.../a.jpg\nb.jpg``) can never reach a
    # manifest regardless of which path built it — the hook path bypasses build_render_manifest's
    # own `_split_candidate_urls`, which is how session 3445660b's keyframe gallery persisted 7/8
    # unfetchable tiles. Idempotent on clean urls.
    urls = _sanitize_url_list(urls)
    source_urls = _sanitize_url_list(source_urls)
    extra_images = _sanitize_url_list(extra_images)
    opts = render_options if isinstance(render_options, dict) else {}
    sidecars = {k: v for k, v in fetched.items() if v is not None}

    media_urls = [u for u in urls if _ext_from_url(u) in _VIDEO_EXTS or _ext_from_url(u) in _AUDIO_EXTS]
    doc_urls = [u for u in urls if _ext_from_url(u) in ("pdf", "html")]
    json_urls = [u for u in urls if _ext_from_url(u) in _JSON_EXTS]
    subtitle_urls = [u for u in urls if _ext_from_url(u) in _SUBTITLE_EXTS]  # noqa: F841 - caller overlays these
    image_urls = [u for u in urls if _ext_from_url(u) in _IMAGE_EXTS]
    source_video_urls = [u for u in source_urls if _ext_from_url(u) in _VIDEO_EXTS]

    # Skill selection: MIME (baseline) → tool (authoritative) → content (fallback refinement).
    primary_url = media_urls[0] if media_urls else (doc_urls[0] if doc_urls else (urls[0] if urls else ""))
    primary_ext = _ext_from_url(primary_url) if primary_url else ""
    skill = select_render_skill(_ext_to_mime(primary_ext), primary_ext) if primary_url else None
    skill_name = skill["name"] if skill else "render-generic"
    skill_template = (skill.get("template") if skill else "") or ""
    by_tool = select_render_skill_by_tool(tool)
    if by_tool is not None:
        skill = by_tool
        skill_name = by_tool.get("name") or skill_name
        skill_template = (by_tool.get("template") or "") or skill_template
    if sidecars and by_tool is None:
        refined = select_render_skill_by_content(sidecars)
        if refined is not None:
            skill = refined
            skill_name = refined.get("name") or skill_name
            skill_template = (refined.get("template") or "") or skill_template

    timeline_media = _analysis_timeline_media(media_urls, source_video_urls, skill)
    # Descriptive-only sidecars (e.g. probe.json = the source SPEC) can't annotate anything,
    # so the SOURCE must NOT become a rendered canvas/track for them — else the job's INPUT
    # renders as its output. Ported from build_render_manifest so BOTH entries share this guard
    # (the entrypoint previously lacked it). Gated on the DATA being descriptive, not on whether
    # projection happened, so shots.json keeps its canvas while probe.json does not.
    if json_urls and sidecars and all(_is_descriptive_sidecar(u) for u in json_urls):
        source_video_urls = []
        timeline_media = media_urls  # drop the source-video canvas for a pure spec
    annotation_manifest = None
    if source_video_urls and sidecars:
        annotation_manifest = _build_video_annotations_manifest(
            source_video_urls[0], sidecars, image_urls, urls, manifest_id)

    projected = False
    if doc_urls:
        manifest = _build_documents_manifest(doc_urls, manifest_id)
        manifest["renderSkill"] = skill_name if skill else "render-document"
    elif annotation_manifest:
        manifest = annotation_manifest
        manifest["renderSkill"] = skill_name if skill else "render-annotations"
    elif images_only_view_wins(image_urls, media_urls, doc_urls, timeline_media, skill):
        manifest = _build_image_manifest(image_urls, manifest_id)
        manifest["renderSkill"] = skill_name if skill else "render-image"
    elif timeline_media:
        # An audio-deliverable render (stems, extracted audio, speaker channels) never
        # co-renders a source VIDEO track — audio doesn't project onto video, so a video in the
        # timeline is the analysed INPUT, not an output (it would set the duration and show a
        # black player dwarfing short stems). Ported from build_render_manifest so BOTH paths
        # trim it. (The entrypoint usually never hits this — its source video sits in source_urls,
        # not media_urls — but the tool path can receive mixed candidates.) NOT for an analysis
        # skill that projects segments/markers/boxes: its video IS the canvas the findings draw
        # on, and trimming it rendered a diarization tab audio-only (staging 833a079f).
        audio_tl = [u for u in timeline_media if _ext_from_url(u) in _AUDIO_EXTS]
        if audio_tl and len(audio_tl) != len(timeline_media) and not _projects_onto_source(skill):
            timeline_media = audio_tl
        manifest = _build_media_manifest(
            timeline_media + json_urls + image_urls + extra_images, manifest_id, opts, prefetched=fetched)
        projected = any(manifest.get(k) for k in ("markers", "segments", "annotations", "tables"))
        if not projected:
            manifest["template"] = skill_template or "nle-timeline"
        elif skill_template and manifest.get("template") == "nle-timeline":
            manifest["template"] = skill_template
        if skill_name and not manifest.get("renderSkill"):
            manifest["renderSkill"] = skill_name
    else:
        manifest = None
        if json_urls:
            candidate = _build_media_manifest(
                json_urls + image_urls + extra_images, manifest_id, opts, prefetched=fetched)
            if any(candidate.get(k) for k in ("markers", "segments", "annotations", "tables", "rows", "images")):
                manifest = candidate
                projected = True
                if skill_name and not manifest.get("renderSkill"):
                    manifest["renderSkill"] = skill_name
        if manifest is None:
            manifest = _build_generic_manifest(urls, manifest_id)
            manifest["renderSkill"] = "render-generic"

    # IDENTITY — stamped in ONE place for BOTH entries so they can't drift.
    if isinstance(manifest, dict):
        if candidate_keys and not manifest.get("candidate_keys"):
            manifest["candidate_keys"] = list(candidate_keys)
        _stamp_source_identity(manifest, source_video_urls, media_urls, urls)
    return manifest, projected, skill


@app.entrypoint
async def invoke(payload, context):
    """Every event leaves through :func:`_normalize_output_ref` (error records are always flagged)."""
    async for ev in _invoke(payload, context):
        yield _normalize_output_ref(ev)


async def _invoke(payload, context):
    """Render Agent entrypoint — DETERMINISTIC-FIRST.

    Rendering is mechanical: MIME -> template, skill declarations -> projections (boxes/markers/
    lanes), deliverable declarations -> which files, output-vs-source by role. So the PRIMARY path
    is the deterministic pipeline (``_render_deterministic``): instant, no LLM, immune to model
    capacity blips (Sonnet 5 ServiceUnavailable), no url/template variance, and it renders ALL
    deliverables (never cherry-picks 1 of 2 images). The LLM agent (``_render_agentic``) is the
    FALLBACK, for genuinely ambiguous cases the deterministic build can't resolve cleanly. See
    docs/render-redesign.md.
    """
    # Resolve s3:// OUTPUT refs via the media gateway ONCE, up front, so the agentic (primary) and
    # deterministic paths both work off fetchable urls (the agentic path does not resolve on its
    # own). Job outputs live in the M2C bucket the gateway owns; resolving here (vs the main/task
    # role self-presigning at dispatch) keeps that role free of raw S3 IAM on the bucket -- the
    # gateway tool is the single least-privilege owner. If a ref cannot be resolved, surface it
    # instead of shipping an unplayable track.
    _prompt = payload.get("prompt", "") if isinstance(payload, dict) else ""
    if "s3://" in _prompt:
        _resolved_prompt, _unresolved = await _resolve_prompt_s3_refs(_prompt)
        if _unresolved:
            logger.error("render: %d s3 ref(s) unresolved via gateway: %s",
                         len(_unresolved), _unresolved[:3])
            _sid = payload.get("session_id", str(uuid.uuid4())) if isinstance(payload, dict) else str(uuid.uuid4())
            _man = _build_error_manifest(
                "UNRESOLVED_S3",
                f"Could not resolve {len(_unresolved)} artifact reference(s) to a fetchable URL. "
                "The media gateway may be unavailable.",
                source_url=_unresolved[0],
            )
            _mid = _man.get("id") or str(uuid.uuid4())
            _key = _write_manifest_to_s3(format_manifest_safe(_man), _sid, _mid)
            if _key:
                yield json.dumps({"type": "output_ref", "s3_key": _key, "manifest_id": _mid,
                                  "error": True, "error_code": "UNRESOLVED_S3"})
            return
        payload = {**payload, "prompt": _resolved_prompt}

    # Deterministic-first: run the mechanical pipeline as PRIMARY. It emits an output_ref with
    # error=True when it cannot produce a clean manifest; only then do we escalate to the LLM agent
    # (the fallback for genuine ambiguity). The common cases (single media, proxy, 2-image gallery,
    # faces boxes, shot markers) render here with zero LLM calls.
    det_succeeded = False
    det_declined = False
    try:
        async for ev in _render_deterministic(payload, context):
            ev = _normalize_output_ref(ev)
            try:
                obj = json.loads(ev) if isinstance(ev, str) else None
                if isinstance(obj, dict) and obj.get("type") == "output_ref" and not obj.get("error"):
                    det_succeeded = True
                if isinstance(obj, dict) and obj.get("type") == "no_view":
                    det_declined = True
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
            yield ev
    except Exception:  # noqa: BLE001 - a crashed pipeline must still get a chance at the agent
        logger.exception("deterministic render crashed; escalating to the agent")
    if det_declined and not det_succeeded:
        # A deliberate "nothing to show" is terminal: the LLM agent would only invent a view for data.
        logger.info("render: deterministic pipeline declined (no view) — not escalating")
        return
    if not det_succeeded:
        logger.warning("deterministic render did not emit a clean manifest; escalating to the LLM agent")
        yield json.dumps({"type": "step", "status": "running",
                          "label": "Escalating to render agent"})
        async for ev in _render_agentic(payload, context):
            yield ev


async def _render_deterministic(payload, context):
    """Deterministic media rendering pipeline — the AGENT'S FALLBACK.

    Procedural (no LLM loop): extract URL -> probe -> classify -> assemble manifest ->
    write to S3 -> emit output_ref. Always produces a valid manifest within the timeout budget.
    Runs only when the primary agent path fails to emit a clean manifest, so a render never
    shows nothing.
    """
    prompt = payload.get("prompt", "")
    if not prompt:
        yield json.dumps({"error": "prompt is required"})
        return

    session_id = payload.get("session_id", str(uuid.uuid4()))
    render_options = payload.get("render_options", {})
    manifest_id = _resolve_manifest_id(payload)
    pipeline_start = time.time()

    yield json.dumps({"type": "step", "status": "running", "label": "Render Agent: analyzing artifact"})

    artifact_urls, source_urls = _split_render_request(prompt)
    # Canonical s3:// refs are not fetchable by the manifest builder; presign them
    # through the media gateway first. Non-s3 URLs pass through unchanged.
    if any(u.startswith("s3://") for u in artifact_urls + source_urls):
        artifact_urls = await _resolve_s3_urls_via_gateway(artifact_urls)
        source_urls = await _resolve_s3_urls_via_gateway(source_urls)
        # A media element cannot fetch an s3:// URI. If resolution did not happen
        # (e.g. the gateway registry is unavailable) the builder would still emit a
        # track whose src is the raw ref: the render LOOKS successful and silently
        # plays nothing. Surface it as an error instead of shipping a broken track.
        unresolved = [u for u in artifact_urls + source_urls if u.startswith("s3://")]
        if unresolved:
            logger.error("render: %d s3 ref(s) unresolved, refusing to emit unplayable tracks: %s",
                         len(unresolved), unresolved[:3])
            yield json.dumps({"type": "step", "status": "failed",
                              "label": "Render Agent: could not resolve artifact URLs"})
            manifest = _build_error_manifest(
                "UNRESOLVED_S3",
                f"Could not resolve {len(unresolved)} artifact reference(s) to a fetchable URL. "
                "The media gateway may be unavailable.",
                source_url=unresolved[0],
            )
            manifest_id_local = manifest.get("id") or manifest_id
            s3_key = _write_manifest_to_s3(json.dumps(manifest), session_id, manifest_id_local)
            if s3_key:
                yield json.dumps({"type": "output_ref", "s3_key": s3_key,
                                  "manifest_id": manifest_id_local,
                                  "error": True, "error_code": "UNRESOLVED_S3"})
            return
    urls = artifact_urls or source_urls
    if not urls and isinstance(render_options, dict):
        opt_url = render_options.get("url")
        if opt_url:
            urls = [opt_url]
    if not urls:
        error_manifest = _build_error_manifest("NO_URL", "No artifact URL found in the render request")
        manifest_json = format_manifest_safe(error_manifest)
        s3_key = _write_manifest_to_s3(manifest_json, session_id, manifest_id)
        if s3_key:
            yield json.dumps({"type": "output_ref", "s3_key": s3_key, "manifest_id": manifest_id, "error": True, "error_code": "NO_URL"})
        else:
            yield manifest_json
        return

    # Deterministic id from the resolved deliverables when the caller passed none (agentic/direct
    # render). Content-addressed so identical renders converge on ONE tab — and never empty (which
    # would key the manifest as `.json`). A caller-supplied id still wins.
    if not str(manifest_id or "").strip():
        from render.artifacts import _manifest_id_for
        manifest_id = _manifest_id_for(urls)

    # COALESCED FAN-OUT -> ONE multiview clip-gallery (deterministic; no LLM). dispatch_render
    # sets payload["gallery"] when the render hook collapsed N sibling same-tool VIDEO segment
    # nodes (a lip-sync per-segment review) into one slot. Render them as independent clips in
    # ONE gallery tab instead of a stacked timeline (session 99246b15: 8 tabs -> 1 review). Falls
    # through to the normal skill path if there are fewer than 2 clips.
    if isinstance(payload, dict) and payload.get("gallery"):
        clip_urls = [u for u in urls
                     if _ext_from_url(u) in _VIDEO_EXTS or _ext_from_url(u) in _AUDIO_EXTS]
        if len(clip_urls) >= 2:
            items = [{"label": f"Segment {i + 1}", "url": u,
                      "kind": "video" if _ext_from_url(u) in _VIDEO_EXTS else "audio"}
                     for i, u in enumerate(clip_urls)]
            gallery_manifest = _build_clip_gallery_manifest(items, manifest_id)
            if gallery_manifest.get("galleryClips"):
                gjson = json.dumps(gallery_manifest)
                s3_key = _write_manifest_to_s3(gjson, session_id, gallery_manifest["id"])
                if s3_key:
                    yield json.dumps({"type": "output_ref", "s3_key": s3_key,
                                      "manifest_id": gallery_manifest["id"],
                                      "template": "clip-gallery"})
                else:
                    yield gjson
                return

    # --- Skill-driven, composable dispatch (Req 11 + design-v2) ---
    # Classify inputs: media (tracks), PDF (document), JSON (sidecar overlays).
    media_urls = [u for u in urls if _ext_from_url(u) in _VIDEO_EXTS or _ext_from_url(u) in _AUDIO_EXTS]
    doc_urls = [u for u in urls if _ext_from_url(u) in ("pdf", "html")]
    json_urls = [u for u in urls if _ext_from_url(u) in _JSON_EXTS]
    subtitle_urls = [u for u in urls if _ext_from_url(u) in _SUBTITLE_EXTS]
    image_urls = [u for u in urls if _ext_from_url(u) in _IMAGE_EXTS]
    source_video_urls = [u for u in source_urls if _ext_from_url(u) in _VIDEO_EXTS]
    # JSON-only input: projectable JSON (e.g. faces -> a data-table findings view) still renders
    # below; but if projection yields only the GENERIC fallback (render-generic / generic-preview),
    # that is a raw file-viewer tab for plumbing data — suppressed at the emit point so no tab opens.
    _json_only = bool(json_urls) and not (media_urls or doc_urls or image_urls or subtitle_urls or source_video_urls)

    # MULTIVIEW BY DEFAULT for a batch of INDEPENDENT peer video clips. A lip-sync per-segment
    # review — or any collect-and-render of N separate clips — arrives here as N video urls with no
    # source canvas, no document, and no JSON overlays: independent clips to WATCH SEPARATELY, not
    # layers of one composite. The skill path would stack them into ONE nle-timeline (8 video tracks
    # on a single time axis, observed session 20f7315e), which is not a review grid. Render a
    # clip-gallery multiview instead. The explicit `payload["gallery"]` fast-path above already
    # covers the SAME-INVOKE fan-out coalesce (and carries audio); this is the fallback for clips
    # gathered ACROSS plan nodes, where that coalesce cannot fire. Guarded so a document (PDF wins),
    # a source+overlay analysis, or a JSON-sidecar findings view is never turned into a grid; a
    # single clip (len < 2) still takes the normal single-video/timeline path.
    _peer_video_clips = [u for u in urls if _ext_from_url(u) in _VIDEO_EXTS]
    if len(_peer_video_clips) >= 2 and not doc_urls and not json_urls and not source_video_urls:
        items = [{"label": f"Segment {i + 1}", "url": u, "kind": "video"}
                 for i, u in enumerate(_peer_video_clips)]
        gallery_manifest = _build_clip_gallery_manifest(items, manifest_id)
        if gallery_manifest.get("galleryClips"):
            gjson = json.dumps(gallery_manifest)
            s3_key = _write_manifest_to_s3(gjson, session_id, gallery_manifest["id"])
            if s3_key:
                yield json.dumps({"type": "output_ref", "s3_key": s3_key,
                                  "manifest_id": gallery_manifest["id"],
                                  "template": "clip-gallery"})
            else:
                yield gjson
            return

    # Pick the primary artifact for skill selection: media first, else PDF, else first.
    primary_url = media_urls[0] if media_urls else (doc_urls[0] if doc_urls else urls[0])
    primary_ext = _ext_from_url(primary_url)
    primary_mime = _ext_to_mime(primary_ext)
    skill = select_render_skill(primary_mime, primary_ext)
    skill_name = skill["name"] if skill else "render-generic"
    skill_template = (skill.get("template") if skill else "") or ""

    # AUTHORITATIVE: the tool that produced this output determines the view. Known at
    # dispatch, threaded through as `payload["tool"]`. When it maps to a declared skill
    # this wins over BOTH the MIME pick above and the content refinement below — that is
    # the whole fix for "same op rendered three different views". An unmapped tool leaves
    # `by_tool` None and the existing MIME/content path runs unchanged.
    by_tool = select_render_skill_by_tool(payload.get("tool") if isinstance(payload, dict) else None)
    if by_tool is not None:
        skill = by_tool
        skill_name = by_tool.get("name") or skill_name
        skill_template = (by_tool.get("template") or "") or skill_template

    try:
        opts = render_options if isinstance(render_options, dict) else {}
        sidecars: dict = {}
        # Initialised unconditionally: the media branch below passes this as
        # `prefetched`, and a render with media but NO json sidecars — a plain video,
        # the commonest case — would otherwise hit an unbound local and fail the whole
        # render with an opaque AGENT_ERROR.
        fetched: dict = {}
        if json_urls:
            yield json.dumps({"type": "step", "status": "running",
                              "label": f"Ingesting {len(json_urls)} data artifact(s)"})
            # Concurrent: independent reads, and the builder reuses these rather
            # than downloading each file a second time.
            fetched = await asyncio.to_thread(_fetch_json_many, json_urls)
            # Successes only for the fallback path; the full attempt map (including
            # failures) is what the builder reuses so nothing is retried.
            sidecars = {k: v for k, v in fetched.items() if v is not None}
            # Content outranks extension — but the TOOL outranks content. If the tool
            # already picked the view (`by_tool`), keep it: content scoring is only the
            # fallback for outputs whose tool we don't recognise, not a re-litigation of
            # a known tool's view.
            refined = None if by_tool is not None else select_render_skill_by_content(sidecars)
            if refined is not None:
                skill = refined
                skill_name = refined.get("name") or skill_name
                skill_template = (refined.get("template") or "") or skill_template

        yield json.dumps({"type": "step", "status": "running",
                          "label": f"Render skill selected: {skill_name}"})

        # The media a findings view sits on: the tool's own media output when it
        # has one, else the source it analysed. Previously the source video was
        # consumed ONLY by the annotation branch below, which needs per-entity
        # boxes; a marker or segment declaration with no media artifact of its
        # own therefore had no timeline to project onto and fell through to the
        # generic preview. That is what rendered shot segmentation as a grid of
        # stills instead of markers over the video. An ANALYSIS skill puts the source
        # FIRST even when the job shipped media of its own (_analysis_timeline_media).
        timeline_media = _analysis_timeline_media(media_urls, source_video_urls, skill)

        annotation_manifest = None
        if source_video_urls and sidecars:
            annotation_manifest = _build_video_annotations_manifest(
                source_video_urls[0], sidecars, image_urls, urls, manifest_id,
            )
            if annotation_manifest:
                yield json.dumps({
                    "type": "step", "status": "running",
                    "label": (
                        f"Composing overlays for {len(annotation_manifest['entities'])} "
                        f"subject(s) on the source video"
                    ),
                })

        if doc_urls:
            # DELIVERABLE-TYPE PRECEDENCE (see build_render_manifest for the full rationale). A
            # document is an OUTPUT you never project onto, so a co-present video/audio is the
            # analyzed SOURCE, not a rival deliverable — render the document. Guarding on
            # `not media_urls` was the verified root cause of "PDF renders the source video"
            # (session 2693e705): the dispatch prompt's deliverable section carried the PDF AND
            # the prior source videos, so media_urls was non-empty, this branch was skipped, and
            # the 718s source rendered while the PDF was dropped. The projection branch
            # (source video + sidecars → annotations) is checked below and stays the choice for a
            # findings-on-video view. Document (PDF) view (Req 14).
            manifest = _build_documents_manifest(doc_urls, manifest_id)
            manifest["renderSkill"] = skill_name if skill else "render-document"
        elif annotation_manifest:
            # Timestamped boxes + source media → synchronized overlay view.
            manifest = annotation_manifest
            manifest["renderSkill"] = skill_name if skill else "render-annotations"
        elif images_only_view_wins(image_urls, media_urls, doc_urls, timeline_media, skill):
            # A standalone image (no video/audio/pdf) → image-annotated view. Render ALL images
            # (a two-option text-to-image result must show both), not just the first.
            #
            # `_declares_timeline_view` guard: `media_urls` counts DELIVERABLES only, but the
            # canvas for a findings timeline usually arrives as a SOURCE (`timeline_media =
            # media_urls or source_video_urls`). Shot segmentation ships shot_*.jpg thumbnails
            # as deliverables, so this branch used to win and the marker timeline below was
            # never reached — shot segmentation rendered as a thumbnail grid with 0 markers
            # even when the source video was available (sessions a2ebd1e9 / a5a1b4fa; confirmed
            # by replaying the real artifacts against a local render agent). When the selected
            # skill declares a timeline projection AND a canvas exists, the timeline wins and
            # the images ride along inside it as gallery content.
            manifest = _build_image_manifest(image_urls, manifest_id)
            manifest["renderSkill"] = skill_name if skill else "render-image"
        elif timeline_media:
            # Composite timeline: media tracks + data-driven sections.
            # The JSON sidecars go in WITH the media: the builder correlates them
            # through the skill declarations, which is how a tool's findings reach
            # the view. Passing media only is what made a 9-shot segmentation
            # render as one bare track.
            # Images a result names by key but never lists as artifacts.
            extra_images = await _resolve_declared_images(sidecars, urls)
            manifest = _build_media_manifest(
                timeline_media + json_urls + image_urls + extra_images,
                manifest_id, opts, prefetched=fetched)

            # A template the builder chose from the vetted inventory reflects what
            # the data actually is, so it outranks the skill's static default.
            # Unconditionally assigning here previously clobbered that choice.
            projected = any(manifest.get(k) for k in ("markers", "segments", "annotations", "tables"))
            if not projected:
                # Projecting skill matched but produced nothing — its declared JSON deliverable
                # (e.g. shots.json) was missing from the candidates (dropped-sidecar adoption race,
                # session 08c4f282). Recover it from this session's output and re-project. No-op for
                # plain-media renders (render-video/-audio/-image declare no .json deliverable).
                _recovered = _recover_missing_deliverable(skill, urls)
                if _recovered:
                    sidecars = {**sidecars, **_recovered}
                    fetched = {**fetched, **_recovered}
                    json_urls = json_urls + list(_recovered.keys())
                    manifest = _build_media_manifest(
                        timeline_media + json_urls + image_urls + extra_images,
                        manifest_id, opts, prefetched=fetched)
                    projected = any(manifest.get(k) for k in ("markers", "segments", "annotations", "tables"))
            if not projected:
                manifest["template"] = skill_template or "nle-timeline"
            elif skill_template and manifest.get("template") == "nle-timeline":
                manifest["template"] = skill_template
            if skill_name and not manifest.get("renderSkill"):
                manifest["renderSkill"] = skill_name

            # Sidecar JSON → markers / segments / metadata / captions.
            # Deterministic projection already covers any shape a skill declares,
            # so the LLM pass is a FALLBACK for undeclared shapes only. Skipping it
            # when a declaration matched removes a model round-trip from the hot
            # path — the reason declarative rendering is fast as well as dynamic.
            if sidecars and projected:
                logger.info("render: skill declarations satisfied the view; skipping LLM enrichment")
            if sidecars and not projected:
                enrichment = await _populate_from_sidecars_llm(sidecars, skill, manifest)
                if enrichment.get("markers"):
                    manifest["markers"] = enrichment["markers"]
                if enrichment.get("segments"):
                    manifest["segments"] = enrichment["segments"]
                if enrichment.get("captions"):
                    manifest["captions"] = enrichment["captions"]
                if enrichment.get("metadata"):
                    manifest["metadata"] = enrichment["metadata"]

            # VTT/SRT subtitles → caption cues overlaid on the media track(s),
            # plus an explicit subtitle track so the timeline shows a caption
            # row. Previously .vtt/.srt were dropped as "unsupported", so a
            # diarization job's subtitles never rendered on the video.
            if subtitle_urls:
                yield json.dumps({"type": "step", "status": "running",
                                  "label": f"Parsing {len(subtitle_urls)} subtitle file(s)"})
                sub_caps = _captions_from_subtitles(subtitle_urls)
                if sub_caps:
                    existing = manifest.get("captions") or []
                    manifest["captions"] = existing + sub_caps
                    track_end = manifest.get("duration") or max((c["end"] for c in sub_caps), default=0.001)
                    manifest["tracks"].append({
                        "id": "subtitle-captions",
                        "type": "subtitle",
                        "src": subtitle_urls[0],
                        "label": "Subtitles",
                        "start": 0.0,
                        "end": round(float(track_end), 3),
                    })
        else:
            # No playable media, but a findings-only result can still be rendered
            # from its projections — face recognition emits JSON only. Without this
            # the builder was never reached for those jobs (it sits behind the
            # `media_urls` branch) and they degraded to an artifact listing, which is
            # what made Recognize Faces look empty even with the skills loaded.
            manifest = None
            if json_urls:
                extra_images = await _resolve_declared_images(sidecars, urls)
                candidate = _build_media_manifest(
                    json_urls + image_urls + extra_images, manifest_id, opts,
                    prefetched=fetched)
                if any(candidate.get(k) for k in
                       ("markers", "segments", "annotations", "tables", "rows", "images")):
                    manifest = candidate
                    if skill_name and not manifest.get("renderSkill"):
                        manifest["renderSkill"] = skill_name
                    yield json.dumps({
                        "type": "step", "status": "running",
                        "label": f"Composing {manifest['template']} from analysis findings",
                    })
            if manifest is None:
                if _json_only:
                    # JSON-only input that projected to NOTHING is the raw generic file-viewer tab
                    # users hit. JSON is plumbing/overlay data (manifests, probes, sidecars), never
                    # a standalone deliverable — show NO tab rather than a generic artifact listing.
                    # Projectable JSON (e.g. faces -> data-table findings view) set `manifest` above
                    # and never reaches here, so real findings views are unaffected.
                    logger.info("render: JSON-only input projected nothing — suppressing generic listing (no tab)")
                    yield json.dumps({"type": "step", "status": "done",
                                      "label": "Render Agent: JSON is data, not a view — nothing to display"})
                    yield _no_view("data_only", "JSON-only input projected no view")
                    return
                # Nothing projected: an artifact listing beats an empty view.
                manifest = _build_generic_manifest(urls, manifest_id)
                manifest["renderSkill"] = "render-generic"
    except Exception as e:
        logger.exception("Composable manifest build failed")
        manifest = _build_error_manifest("AGENT_ERROR", f"Manifest build failed: {str(e)[:200]}", source_url=primary_url)

    # Stamp the raw candidate keys (from dispatch_render) so the durable dedup can recognise this
    # render regardless of how each url was later mirrored/imported — content-addressed manifest_id
    # already collapses identical dispatches; this covers the same media under different key forms.
    if isinstance(payload, dict) and payload.get("candidate_keys") and isinstance(manifest, dict) \
            and not manifest.get("candidate_keys"):
        manifest["candidate_keys"] = payload["candidate_keys"]
    # sourceKey via the SINGLE shared authority (the same derive_source_key that
    # build_render_manifest/assemble_manifest use) — so the UI (composeTimeline) merges every
    # analysis of one source onto ONE composite timeline. THIS is what was missing on the primary
    # render path: the deterministic assembly stamped renderSkill but never sourceKey, so every
    # real manifest had sourceKey: null and nothing ever merged. media_urls/source_video_urls/urls
    # are the classified inputs computed above; source_video_urls is the analysed canvas.
    _stamp_source_identity(manifest, source_video_urls, media_urls, urls)
    manifest_json = format_manifest_safe(manifest)

    # Validate (schema)
    is_valid, validation_errors = _validate_manifest_json(manifest_json)
    if not is_valid and "errors" not in manifest:
        logger.warning("Manifest validation failed: %s", validation_errors)
        manifest["errors"] = [{"code": "VALIDATION_FAILED", "message": e} for e in validation_errors[:5]]
        manifest_json = format_manifest_safe(manifest)

    # Review (DATA-AWARE, F8) — the guarantee for an agent-driven render. A schema check passes
    # an empty-but-valid manifest and a manifest that dropped boxes the source data carries; this
    # checks the manifest against the sidecars it was built from. Flag-and-log for now (the
    # agentic re-dispatch-with-critique loop is a later enhancement); surfaced as a step and
    # recorded on the manifest so the reviewer's findings are visible rather than silent.
    try:
        from render.review import review_manifest

        review_problems = review_manifest(manifest, locals().get("sidecars"))
    except Exception as _rev_exc:  # noqa: BLE001 - review must never break a render
        review_problems = []
        logger.warning("manifest review skipped (non-fatal): %s", _rev_exc)
    if review_problems:
        logger.warning("Manifest review flagged %d issue(s) for %s: %s",
                       len(review_problems), manifest_id, review_problems)
        manifest.setdefault("reviewFindings", review_problems)
        manifest_json = format_manifest_safe(manifest)
        yield json.dumps({"type": "step", "status": "warning",
                          "label": f"Render review: {review_problems[0][:80]}"})

    # Completeness VERIFY (deterministic): every renderable OUTPUT candidate should be represented
    # in the manifest. A dropped deliverable (e.g. 1 of 2 images) is flagged for visibility and
    # guards regressions. We do NOT escalate to the LLM agent here -- a partial deterministic render
    # beats risking a throttled AGENT_ERROR; the flag makes the gap visible without losing the render.
    try:
        _dropped = _dropped_renderable_candidates(urls, manifest)
        if _dropped and not manifest.get("errors"):
            logger.warning("render: completeness — %d candidate(s) not rendered in %s: %s",
                           len(_dropped), manifest_id, _dropped[:3])
            manifest.setdefault("reviewFindings", []).append(
                f"{len(_dropped)} candidate deliverable(s) not rendered")
            manifest_json = format_manifest_safe(manifest)
    except Exception:  # noqa: BLE001 - verify must never break a render
        pass

    yield json.dumps({"type": "step", "status": "running", "label": "Writing render manifest"})

    # Write to S3 (retry once)
    s3_key = _write_manifest_to_s3(manifest_json, session_id, manifest_id)
    if not s3_key:
        s3_key = _write_manifest_to_s3(manifest_json, session_id, manifest_id)

    if s3_key:
        yield json.dumps({"type": "output_ref", "s3_key": s3_key, "manifest_id": manifest_id})
        yield json.dumps({"type": "step", "status": "success", "label": f"Manifest written: {s3_key}"})
    else:
        yield manifest_json
        yield json.dumps({"type": "step", "status": "warning", "label": "S3 write failed — manifest delivered inline"})

    yield json.dumps({
        "type": "execution_summary",
        "total_duration_ms": int((time.time() - pipeline_start) * 1000),
        "manifest_id": manifest_id,
        "session_id": session_id,
    })


def format_manifest_safe(manifest: dict) -> str:
    """Serialize a manifest dict to 2-space JSON, tolerant of any input."""
    try:
        return json.dumps(manifest, indent=2)
    except Exception:
        return json.dumps({"id": manifest.get("id", ""), "duration": 0, "tracks": [], "clips": [], "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()), "errors": [{"code": "SERIALIZE_ERROR", "message": "manifest not serializable"}]}, indent=2)


async def _render_agentic(payload, context):
    """PRIMARY render path: an LLM agent (Sonnet) that reasons over the artifacts, loads render
    skills via progressive disclosure (AgentSkills), and SELF-RECOVERS -- it validates its chosen
    urls (validate_render_urls), reviews the manifest against the data (review_render_manifest),
    takes the box path and re-reviews if it rendered nothing, and self-corrects once on a
    validation error. Emits an error manifest on hard failure, which the entrypoint treats as a
    signal to fall back to the deterministic pipeline."""
    prompt = payload.get("prompt", "")
    if not prompt:
        yield json.dumps({"error": "prompt is required"})
        return

    session_id = payload.get("session_id", str(uuid.uuid4()))
    # Render is PINNED to Haiku 4.5 (fast/cheap): manifest building is largely deterministic
    # and runs on every artifact. Ignore any model_id propagated from the caller's pick.
    model_id = RENDER_MODEL_ID
    render_options = payload.get("render_options", {})

    # Default render options — all enabled
    extract_peaks = render_options.get("extractPeaks", True)
    parse_captions = render_options.get("parseCaptions", True)
    detect_clips = render_options.get("detectClips", True)

    manifest_id = _resolve_manifest_id(payload)
    pipeline_start = time.time()

    yield json.dumps({
        "type": "step",
        "status": "running",
        "label": "Render Agent: starting pipeline",
    })

    # Build the augmented prompt with render options context
    options_context = (
        f"\n\n## Render Options\n"
        f"- extractPeaks: {extract_peaks}\n"
        f"- parseCaptions: {parse_captions}\n"
        f"- detectClips: {detect_clips}\n"
        f"\nManifest ID to use: {manifest_id}\n"
        f"Session ID to use: {session_id}\n"
        f"\nTo render: DECIDE which url(s) to render (match RECENT USER REQUESTS + each type's "
        f"deliverable; skip scratch), then call build_render_manifest(candidate_urls=[...], "
        f"manifest_id=\"{manifest_id}\", session_id=\"{session_id}\", source_urls=[...]) to assemble "
        f"and write it. Then call review_render_manifest; if it flags missing boxes, re-run "
        f"build_render_manifest with the source video. You do NOT need to output the manifest JSON "
        f"yourself — the tool writes it.\n"
    )
    augmented_prompt = prompt + options_context

    # Load render skills
    # Load render skills through the SAME SkillsManager path every other agent uses (S3 custom
    # skill discovery + lifecycle), scoped to `render-` so ONLY render skills are exposed -- the
    # consolidated render-media bundle plus any customer-added S3 render-* skills.
    plugins = []
    try:
        render_plugin = _skills_manager.create_plugin(name_prefix="render-")
        n_render = len(getattr(_skills_manager, "_shared_skills", None) or [])
        if n_render:
            plugins.append(render_plugin)
            yield json.dumps({
                "type": "step", "status": "running",
                "label": f"Loaded {n_render} render skill(s) via SkillsManager",
            })
    except Exception:  # noqa: BLE001 - a skills-load failure must not abort the render
        logger.warning("render skills via SkillsManager failed; falling back to direct load",
                       exc_info=True)
        render_skills = _load_render_skills()
        if render_skills:
            plugins.append(AgentSkills(skills=render_skills))

    # Create the Strands Agent with render-specific configuration
    model = build_model(model_id=model_id, boto_client_config=_boto_cfg)
    render_agent = Agent(
        model=model,
        system_prompt=RENDER_SYSTEM_PROMPT,
        tools=RENDER_TOOLS,
        plugins=plugins,
    )

    # Execute the agent with timeout enforcement
    text_chunks: list[str] = []
    tool_timings: dict[str, list[float]] = {}
    cycle_count = 0
    # Strands re-emits current_tool_use on EVERY argument-streaming delta, so a single tool call
    # surfaced dozens of "Running tool" steps (review_render_manifest, whose arg is the whole
    # manifest, alone streamed ~75) -- a clean 4-call render looked like a 2000-step runaway. Emit
    # once per call by tracking the last (toolUseId, name) and only stepping when it changes.
    _last_tool_key = None

    try:
        stream = render_agent.stream_async(augmented_prompt)

        async for event in stream:
            # Check pipeline timeout
            elapsed = time.time() - pipeline_start
            if elapsed > PIPELINE_TIMEOUT_SECONDS:
                logger.warning("Pipeline timeout exceeded at %.1fs", elapsed)
                # Emit timeout error manifest
                timeout_manifest = _build_timeout_manifest(source_url=_first_clean_url(prompt))
                manifest_json = json.dumps(timeout_manifest, indent=2)

                # Attempt S3 write for timeout manifest
                s3_key = _write_manifest_to_s3(manifest_json, session_id, manifest_id)
                if s3_key:
                    yield json.dumps({
                        "type": "output_ref",
                        "s3_key": s3_key,
                        "manifest_id": manifest_id,
                        "error": True,
                        "error_code": "TIMEOUT",
                    })
                else:
                    # Fallback: inline manifest
                    yield manifest_json

                yield json.dumps({
                    "type": "step",
                    "status": "error",
                    "label": "Pipeline timeout — emitted TIMEOUT error manifest",
                })
                return

            # Track cycles
            if event.get("start_event_loop"):
                cycle_count += 1
                if cycle_count == 1 or cycle_count % 3 == 0:
                    yield json.dumps({
                        "type": "step",
                        "status": "running",
                        "label": f"Render Agent cycle #{cycle_count}",
                    })

            # Track tool use
            if "current_tool_use" in event:
                ti = event["current_tool_use"]
                if isinstance(ti, dict):
                    tool_name = ti.get("name", "")
                    tool_key = (ti.get("toolUseId") or ti.get("tool_use_id") or "", tool_name)
                    if tool_name and tool_key != _last_tool_key:
                        _last_tool_key = tool_key
                        yield json.dumps({
                            "type": "step",
                            "status": "running",
                            "label": f"Running tool: {tool_name}",
                        })

            # Collect text output
            if "data" in event and isinstance(event["data"], str):
                text_chunks.append(event["data"])

    except Exception as e:
        logger.exception("Render Agent execution failed")
        error_manifest = _build_error_manifest(
            error_code="AGENT_ERROR",
            message=f"Agent execution failed: {str(e)[:200]}",
            source_url=prompt,
        )
        manifest_json = json.dumps(error_manifest, indent=2)

        s3_key = _write_manifest_to_s3(manifest_json, session_id, manifest_id)
        if s3_key:
            yield json.dumps({
                "type": "output_ref",
                "s3_key": s3_key,
                "manifest_id": manifest_id,
                "error": True,
                "error_code": "AGENT_ERROR",
            })
        else:
            yield manifest_json
        return

    # Process agent output — extract manifest JSON
    # PREFERRED path: the agent called build_render_manifest, which assembled + WROTE the manifest
    # deterministically (boxes/markers/lanes) and recorded its key here. Emit that -- the LLM never
    # had to hand-emit a large manifest, which is what made analysis renders loop/time out.
    written = _RENDER_WRITES.pop(manifest_id, None)
    if written and written.get("s3_key"):
        wm = written.get("manifest") or {}
        yield json.dumps({"type": "output_ref", "s3_key": written["s3_key"],
                          "manifest_id": manifest_id})
        yield json.dumps({"type": "step", "status": "success",
                          "label": f"Manifest written to S3: {written['s3_key']}"})
        yield json.dumps({
            "type": "execution_summary",
            "total_duration_ms": int((time.time() - pipeline_start) * 1000),
            "cycles": cycle_count, "manifest_id": manifest_id, "session_id": session_id,
            "model": model_id, "template": wm.get("template"),
        })
        return

    full_text = "".join(text_chunks)
    manifest_json = _extract_json_from_text(full_text)
    if manifest_json:
        # Zero-latency repair: a hand-built manifest lacks `template` (the prompt schema omits it),
        # which renders as a broken raw tab. Stamp template+sourceKey deterministically here, BEFORE
        # validation, so it never fails validation (which would trip the self-correction LLM retry).
        manifest_json = _repair_untemplated_manifest(manifest_json)

    if not manifest_json:
        logger.error("Agent produced no valid JSON manifest. Raw output length: %d", len(full_text))
        error_manifest = _build_error_manifest(
            error_code="INVALID_OUTPUT",
            message="Agent failed to produce a valid JSON manifest",
            source_url=prompt,
        )
        manifest_json = json.dumps(error_manifest, indent=2)

        s3_key = _write_manifest_to_s3(manifest_json, session_id, manifest_id)
        if s3_key:
            yield json.dumps({
                "type": "output_ref",
                "s3_key": s3_key,
                "manifest_id": manifest_id,
                "error": True,
                "error_code": "INVALID_OUTPUT",
            })
        else:
            yield manifest_json
        return

    # Validate the manifest
    is_valid, validation_errors = _validate_manifest_json(manifest_json)

    if not is_valid:
        yield json.dumps({
            "type": "step",
            "status": "running",
            "label": "Validation failed — attempting self-correction",
        })

        # Self-correction: retry once within 5 seconds
        correction_start = time.time()
        remaining_budget = PIPELINE_TIMEOUT_SECONDS - (time.time() - pipeline_start)

        if remaining_budget > SELF_CORRECTION_TIMEOUT_SECONDS:
            correction_prompt = (
                f"The manifest you produced has validation errors:\n"
                f"{json.dumps(validation_errors, indent=2)}\n\n"
                f"Please fix these errors and output a corrected Render_Manifest JSON. "
                f"Output ONLY the raw JSON, no markdown fences or explanation."
            )

            try:
                correction_chunks: list[str] = []
                correction_stream = render_agent.stream_async(correction_prompt)

                async for event in correction_stream:
                    # Check self-correction timeout
                    if time.time() - correction_start > SELF_CORRECTION_TIMEOUT_SECONDS:
                        break
                    if "data" in event and isinstance(event["data"], str):
                        correction_chunks.append(event["data"])

                corrected_text = "".join(correction_chunks)
                corrected_json = _extract_json_from_text(corrected_text)

                if corrected_json:
                    is_valid_corrected, _ = _validate_manifest_json(corrected_json)
                    if is_valid_corrected:
                        manifest_json = corrected_json
                        is_valid = True
                        yield json.dumps({
                            "type": "step",
                            "status": "success",
                            "label": "Self-correction succeeded",
                        })
            except Exception as e:
                logger.warning("Self-correction failed: %s", e)

        # If still invalid after self-correction, emit error manifest
        if not is_valid:
            yield json.dumps({
                "type": "step",
                "status": "error",
                "label": "Self-correction failed — emitting error manifest",
            })
            error_manifest = _build_error_manifest(
                error_code="VALIDATION_FAILED",
                message=f"Manifest validation failed after self-correction: {'; '.join(validation_errors[:5])}",
                source_url=prompt,
            )
            manifest_json = json.dumps(error_manifest, indent=2)

    # Write manifest to S3
    s3_key = _write_manifest_to_s3(manifest_json, session_id, manifest_id)

    if not s3_key:
        # Retry S3 write once
        yield json.dumps({
            "type": "step",
            "status": "running",
            "label": "S3 write failed — retrying",
        })
        s3_key = _write_manifest_to_s3(manifest_json, session_id, manifest_id)

    if s3_key:
        # Success — emit output_ref SSE event
        yield json.dumps({
            "type": "output_ref",
            "s3_key": s3_key,
            "manifest_id": manifest_id,
        })
        yield json.dumps({
            "type": "step",
            "status": "success",
            "label": f"Manifest written to S3: {s3_key}",
        })
    else:
        # Fallback: yield manifest inline in SSE text
        logger.warning("S3 write failed after retry — falling back to inline manifest")
        yield manifest_json
        yield json.dumps({
            "type": "step",
            "status": "warning",
            "label": "S3 write failed — manifest delivered inline",
        })

    # Emit execution summary
    total_duration = time.time() - pipeline_start
    yield json.dumps({
        "type": "execution_summary",
        "total_duration_ms": int(total_duration * 1000),
        "cycles": cycle_count,
        "manifest_id": manifest_id,
        "session_id": session_id,
        "model": model_id,
        "render_options": {
            "extractPeaks": extract_peaks,
            "parseCaptions": parse_captions,
            "detectClips": detect_clips,
        },
    })


if __name__ == "__main__":
    app.run()
