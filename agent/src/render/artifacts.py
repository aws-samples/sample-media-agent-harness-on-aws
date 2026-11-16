"""Strands tool: render_artifacts — dispatch artifacts to the Render Agent.

A dedicated, single-purpose tool (rather than a parameter on the general
``dispatch_tool``) because the orchestrator LLM reliably calls a clearly
named tool with a URL list. This is the designed
Main Agent → Render Agent → output_ref → UI path.

``dispatch_render(urls)`` is the shared, tool-agnostic core: it invokes the
Render Agent, forwards its progress to the active progress channel, and RETURNS
the render-manifest S3 key. It is used by both this tool AND the declarative
``render`` hook action (``hooks.actions``). Surfacing to the UI relies on the
existing ``output_ref → render_manifest_key`` plumbing in
``dispatch_tool._consume_stream``.
"""
import json
import logging
import os
import time
import uuid
from typing import Optional

import runtime_arns
from orchestration.dispatch_tool import _get_client, RENDER_AGENT_ARN
from orchestration.progress_channel import get_active_channel

logger = logging.getLogger(__name__)


def _render_agent_arn() -> str:
    """``RENDER_AGENT_ARN`` (import-time; tests patch it), else resolved at CALL time.

    The deploy no longer puts the ARN in runtime env — it is an SSM parameter — so a runtime that
    started before the parameter existed (brand-new env, or a V2 snapshot) still finds it here.
    """
    return RENDER_AGENT_ARN or runtime_arns.get("RENDER_AGENT_RUNTIME_ARN")

# Publish target for s3:// artifacts (e.g. M2C media-workflow outputs, which
# live in the M2C proxy bucket). The Render Agent + UI can only fetch http
# URLs, so s3:// inputs are copied into the CloudFront-backed artifacts bucket.
_SKILLS_BUCKET = os.environ.get("SKILLS_S3_BUCKET", "")
_ARTIFACTS_DOMAIN = os.environ.get("ARTIFACTS_DOMAIN", "")
_ARTIFACTS_PREFIX = os.environ.get("ARTIFACTS_PREFIX", "artifacts/")

_s3_client_cache = None


def _s3_client():
    global _s3_client_cache
    if _s3_client_cache is None:
        import boto3
        _s3_client_cache = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-2"))
    return _s3_client_cache

_memory_client_cache = None


def _memory_client():
    """boto3 bedrock-agentcore client for reading conversation memory (intent recovery)."""
    global _memory_client_cache
    if _memory_client_cache is None:
        import boto3
        _memory_client_cache = boto3.client(
            "bedrock-agentcore", region_name=os.environ.get("AWS_REGION", "us-west-2"))
    return _memory_client_cache


def _expand_m2c_siblings(urls: list[str]) -> list[str]:
    """For M2C audio-stem outputs, pull in the sibling stem WAVs (speech / music / sfx) so each
    stem gets its own track — ``get_job_status`` surfaces them as labeled outputs but only one
    uri reaches the render. The stem keys are not all in the payload, so we list the run dir (the
    task agent has read on the M2C proxy bucket). Best-effort: any listing failure just leaves the
    original URLs untouched.

    Diarization is intentionally NOT expanded here: its speaker WAVs only manufactured a redundant
    second render tab once the source video began resolving on its own — the Data view already
    carries the video track, speaker lanes, and captions.
    """
    if not _SKILLS_BUCKET:
        return urls
    extra: list[str] = []

    def _push(su: str) -> None:
        if su not in urls and su not in extra:
            extra.append(su)

    _SIB_AUDIO_EXTS = (".wav", ".mp3", ".m4a", ".flac", ".mp4")
    for u in urls:
        low = u.lower()
        if not u.startswith("s3://"):
            continue
        # Audio stem separation -> ALL sibling stems (speech / music / sfx).
        # (Diarization no longer pulls sibling speaker WAVs here. That branch existed so a
        # caption-only, TRACK-LESS timeline had audio to play — but the source video now resolves
        # via its path-derived role, so the diarization Data view already carries a video track +
        # speaker lanes + timestamped captions. The WAVs only manufactured a redundant second
        # "Audio" tab, so the branch is gone.)
        # get_job_status surfaces the stems as labeled outputs, but only one uri
        # reaches the render (session 1b42d744 rendered audio_music alone), so the
        # timeline showed a single track. The stems are direct-child audio_<name>.wav
        # files under the run dir; list it with Delimiter='/' (keeps the intermediate
        # media_processing/ and segment_chunks/ subdirs out) and add the rest so each
        # stem gets its own track (the render-media/audio skill already renders one
        # track per url).
        if "/audio-stem/" in u and low.endswith(_SIB_AUDIO_EXTS) and low.rsplit("/", 1)[-1].startswith("audio_"):
            try:
                bucket, key = u[len("s3://"):].split("/", 1)
                parent = key.rsplit("/", 1)[0] + "/"
                resp = _s3_client().list_objects_v2(Bucket=bucket, Prefix=parent, Delimiter="/")
                for obj in resp.get("Contents", []):
                    k = obj.get("Key", "")
                    name = k.rsplit("/", 1)[-1].lower()
                    if name.startswith("audio_") and name.endswith(_SIB_AUDIO_EXTS):
                        _push(f"s3://{bucket}/{k}")
            except Exception:
                logger.warning("dispatch_render: stem-sibling expand failed for %s", u, exc_info=True)
    return urls + extra


_PREFIX_RENDERABLE_EXTS = (
    ".vtt", ".srt", ".wav", ".mp3", ".m4a", ".flac",
    ".mp4", ".mov", ".webm",
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".pdf", ".json", ".csv",
)
_PREFIX_RENDER_LIMIT = 50


def _expand_s3_prefixes(urls: list[str]) -> list[str]:
    """Replace S3 directory outputs with bounded concrete artifacts.

    Async tools commonly return a prefix instead of enumerating every result.
    Prefixes are not valid Render Agent inputs, so resolve each one to at most
    50 supported objects. Listing is best-effort; other concrete artifacts can
    still render if a prefix cannot be read.
    """
    expanded: list[str] = []
    seen: set[str] = set()
    for u in urls:
        if not (u.startswith("s3://") and u.endswith("/")):
            if u not in seen:
                seen.add(u)
                expanded.append(u)
            continue
        try:
            bucket, prefix = u[len("s3://"):].split("/", 1)
            resp = _s3_client().list_objects_v2(
                Bucket=bucket,
                Prefix=prefix,
                MaxKeys=200,
            )
            added = 0
            for obj in resp.get("Contents", []):
                key = obj.get("Key", "")
                if not key.lower().endswith(_PREFIX_RENDERABLE_EXTS):
                    continue
                artifact_uri = f"s3://{bucket}/{key}"
                if artifact_uri not in seen:
                    seen.add(artifact_uri)
                    expanded.append(artifact_uri)
                    added += 1
                if added >= _PREFIX_RENDER_LIMIT:
                    break
            logger.info("dispatch_render: expanded artifact prefix %s to %d object(s)", u, added)
        except Exception:
            logger.warning("dispatch_render: artifact-prefix expand failed for %s", u, exc_info=True)
    return expanded


def _publish_s3_uris(urls: list[str]) -> list[str]:
    """Convert s3:// URIs to presigned GET URLs (valid 24h).

    Previous approach copied artifacts into the CloudFront bucket (200-500ms
    per file). Presigned URLs eliminate the copy latency entirely while still
    giving the Render Agent + UI an http-fetchable URL.
    """
    out: list[str] = []
    for u in urls:
        if not u.startswith("s3://"):
            out.append(u)
            continue
        try:
            src_bucket, src_key = u[len("s3://"):].split("/", 1)
            presigned = _s3_client().generate_presigned_url(
                "get_object",
                Params={"Bucket": src_bucket, "Key": src_key},
                ExpiresIn=86400,
            )
            out.append(presigned)
            logger.info("dispatch_render: presigned %s", u)
        except Exception:
            logger.warning("dispatch_render: failed to presign s3 uri %s", u, exc_info=True)
    return out

# Per-process URL-set dedup: prevents rendering the same set of artifact URLs
# twice within a single runtime invocation (e.g. hook fires on get_job_status
# AND the LLM also calls render_artifacts with the same URLs in the same turn).
# Does NOT block re-renders of modified artifacts (different URL or explicit
# user request clears the guard). Cross-process dedup (multiple dispatches from
# the orchestrator) is handled by the orchestrator's system prompt instruction
# to stop after the first render.
_rendered_url_sets: set[frozenset[str]] = set()
# Media a named gallery (show_media_gallery) has shown in THIS process: never auto-rendered again, keyed or not.
# The end-of-turn harvest of the same turn's presigned previews otherwise opened extra tabs beside the gallery,
# keyed renders bypassing the durable dedup below (staging be68313c: three broken tabs next to "Voice audition").
_gallery_shown: set[str] = set()


def note_gallery_media(urls) -> None:
    """Record media a named gallery shows (normalised + by object identity)."""
    from render.review import _norm_url, _media_identity
    for u in urls or ():
        if isinstance(u, str) and u.strip():
            _gallery_shown.add(_norm_url(u))
            mid = _media_identity(u)
            if mid:
                _gallery_shown.add(mid)
    if len(_gallery_shown) > 2000:
        _gallery_shown.clear()


def _shown_in_gallery(url: str) -> bool:
    if not _gallery_shown:
        return False
    from render.review import _norm_url, _media_identity
    mid = _media_identity(url)
    return _norm_url(url) in _gallery_shown or (bool(mid) and mid in _gallery_shown)
_RENDER_DEDUP_MAX = 200

TOOL_SPEC = {
    "name": "render_artifacts",
    "description": (
        "Render one or more artifact URLs (media, etc.) into an interactive "
        "viewer for the user. Pass EVERY related artifact URL together in a "
        "single call (e.g. all stems of a job, or a video plus its audio) — "
        "they are assembled into ONE unified multi-track timeline. Call this "
        "after producing or referencing renderable artifacts. Do not call it "
        "for plain-text answers with no artifacts."
    ),
    "inputSchema": {
        "json": {
            "type": "object",
            "properties": {
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "All artifact URLs to render together in one timeline.",
                },
            },
            "required": ["urls"],
        },
    },
}


def _iter_lines(body):
    """Normalize an invoke_agent_runtime response body into a line iterator."""
    if hasattr(body, "iter_lines"):
        return body.iter_lines()
    if hasattr(body, "read"):
        raw = body.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return raw.split("\n")
    return [str(body)]


# NOTE: there was a _manifest_has_content() gate here that suppressed "empty"
# manifests. It was built on a misreading and removed.
#
# The tabs that showed nothing were `generic-preview` with `tracks: []`, so the gate
# treated an empty `tracks` as "nothing to draw". But generic-preview carries its
# content in `artifacts`/`sources`, not `tracks` -- the manifest for two generated
# images has `tracks: []` and `artifacts: [2 images]`, and the gate threw it away.
# Image rendering broke completely: two sessions produced correct manifests
# (1004B, 1005B) that never became tabs.
#
# The manifest behind the original complaint is not structurally empty either: it
# has `artifacts: list[4]` of JSON files with `mimeType: application/json`. It looked
# useless because render-generic was chosen for four unrelated analysis files at
# once -- which is fixed by dispatching one render per NODE so the specific render
# skills can match, not by discarding manifests.
#
# If suppression is ever wanted again, decide it from the manifest's own emptiness
# across ALL its content fields (artifacts, sources, tracks, clips, captions,
# entities, annotations, rows, tables), and verify against real manifests first.


def _manifest_exists(key: str) -> bool:
    """Verify a render-manifest object is durably present before surfacing it.

    The Render Agent runs in a separate runtime and emits an ``output_ref``
    naming the manifest S3 key. If that object was never actually written (an
    aborted/duplicate render dispatch announcing a phantom key), the UI would
    fetch it and show a transient "Failed: 404". Gate on a HEAD (with a short
    retry for any write lag) so only real manifests reach the UI. When the
    bucket is unknown (unit tests), assume present — no behavior change.
    """
    if not _SKILLS_BUCKET:
        return True
    import time
    for attempt in range(3):
        try:
            _s3_client().head_object(Bucket=_SKILLS_BUCKET, Key=key)
            return True
        except Exception:
            if attempt < 2:
                time.sleep(0.4)
    return False


SOURCE_MEDIA_PROMPT_HEADING = "Source media (context only, not an output artifact):"


def _already_rendered_urls(session_id: str) -> set:
    """URLs this session has ALREADY rendered, from its persisted render manifests (F4b).

    The in-process ``_rendered_url_sets`` only dedups within one warm runtime, so a cold start
    or a later turn re-rendered the same artifact (the 3x duplicate tabs). The render manifests
    at ``artifacts/<session>/render/*.json`` are the durable record -- in the skills bucket the
    agent can read -- so deriving the rendered set from them survives process recycling. Bounded
    and best-effort: any failure returns an empty set, so dedup never blocks a render.
    """
    if not session_id or not _SKILLS_BUCKET:
        return set()
    from render.review import _norm_url, _media_identity
    seen: set = set()

    def _add(x):
        if isinstance(x, str) and x.strip():
            seen.add(_norm_url(x))
            mid = _media_identity(x)
            if mid:
                seen.add(mid)
    try:
        resp = _s3_client().list_objects_v2(
            Bucket=_SKILLS_BUCKET, Prefix=f"artifacts/{session_id}/render/", MaxKeys=100)
        keys = [o["Key"] for o in (resp.get("Contents") or []) if o.get("Key", "").endswith(".json")]
        for key in keys[-50:]:
            try:
                body = _s3_client().get_object(Bucket=_SKILLS_BUCKET, Key=key)["Body"].read()
                man = json.loads(body)
            except Exception:  # noqa: BLE001
                continue
            for t in (man.get("tracks") or []):
                if isinstance(t, dict) and t.get("src"):
                    _add(t["src"])
            for a in (man.get("artifacts") or []):
                if isinstance(a, dict) and a.get("url"):
                    _add(a["url"])
            # A gallery's cards (show_media_gallery / a Render Agent clip-gallery) and image galleries: a file
            # shown there must not come back as a second, unlabelled auto-render tab (staging fe200e49).
            for field in ("galleryClips", "clips", "images"):
                for c in (man.get(field) or []):
                    if isinstance(c, dict) and c.get("src"):
                        _add(c["src"])
                    elif isinstance(c, str):
                        _add(c)
            # Raw candidate keys recorded by build_render_manifest (already normalised). These are
            # the PRE-mirror candidates, so a re-dispatch of the same source s3:// is recognised
            # even though the track src above is the resolved/mirrored copy (the duplicate-tab fix).
            for ck in (man.get("candidate_keys") or []):
                if isinstance(ck, str) and ck:
                    seen.add(ck)
                    mid = _media_identity(ck)
                    if mid:
                        seen.add(mid)
    except Exception:  # noqa: BLE001 - dedup must never break a render
        logger.info("dispatch_render: could not read prior renders for %s (non-fatal)", session_id)
    return {s for s in seen if s}


_MEDIA_KIND_LABELS = {
    "mp4": "Video", "mov": "Video", "webm": "Video", "m4v": "Video",
    "mp3": "Audio", "wav": "Audio", "flac": "Audio", "m4a": "Audio",
    "png": "Image", "jpg": "Image", "jpeg": "Image", "gif": "Image", "webp": "Image",
    "pdf": "Document", "html": "Document",
}


def _looks_like_raw_id(label: Optional[str]) -> bool:
    """True when a tab label is really a job/request id / ARN (nothing human).

    A fal submit_job produced a node whose only id was its request_id; with no tool name the label
    fell through to the titleized request_id ("019feeea F88c 71b3 927e 13e752299e46"), which is
    inconsistent with every other tab. Catch the raw and the titleized (hex-word) forms.
    """
    s = (label or "").strip().lower()
    if not s:
        return True
    if "arn:aws" in s or "execution:" in s:
        return True
    compact = s.replace(" ", "").replace("-", "")
    return len(compact) >= 24 and all(c in "0123456789abcdef" for c in compact)


def _media_kind_label(urls: list[str]) -> str:
    """A consistent, human tab label from the primary artifact's type, e.g. "Video"."""
    for u in urls:
        ext = u.split("?", 1)[0].rsplit(".", 1)[-1].lower()
        if ext in _MEDIA_KIND_LABELS:
            return _MEDIA_KIND_LABELS[ext]
    return "Result"


def _last_exec_uuid(ident: str) -> str:
    """The LAST hyphenated UUID in a url path (the producing M2C execution), or "".

    Only the hyphenated 8-4-4-4-12 form matches, so a 32-hex content digest (our own durable
    key segment) is never mistaken for an execution id."""
    try:
        from render.review import _EXEC_UUID_RE
    except Exception:  # pragma: no cover - import guard
        return ""
    found = _EXEC_UUID_RE.findall(ident.split("?", 1)[0])
    return found[-1].lower() if found else ""


def _basename_key(ident: str) -> str:
    """The url's last path segment as an S3 key segment (percent-decoded)."""
    from urllib.parse import unquote
    return unquote(ident.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1])


def _adopted_output_key(session_id: str, exec_uuid: str, basename: str) -> str:
    """Where an adopted M2C output of this session lives (legacy root):
    ``artifacts/<sid>/output/<uuid>/<name>``. ``basename`` is an already-decoded key segment (see
    :func:`_basename_key`)."""
    sid = str(session_id).replace("/", "_").strip()
    return f"{_ARTIFACTS_PREFIX}{sid}/output/{exec_uuid}/{basename}"


def _adopted_output_keys(session_id: str, exec_uuid: str, basename: str, project_ids=()) -> list:
    """Every key an adopted M2C output of this session may live at: the legacy root first, then the
    project root of each known project (asset-lineage Phase 3 (a): the polling worker adopts under
    ``projects/<pid>/sessions/<sid>/output/<uuid>/<name>`` where the env opts in)."""
    from projects.storage_layout import session_output_roots

    sid = str(session_id).replace("/", "_").strip()
    keys = [_adopted_output_key(session_id, exec_uuid, basename)]
    for pid in project_ids:
        for root in session_output_roots(sid, pid)[1:]:
            keys.append(f"{root}{exec_uuid}/{basename}")
    return list(dict.fromkeys(keys))


def _manifest_project_ids(manifest, session_id) -> list:
    """Project ids this session's outputs are known to live under: the turn's trusted project, plus any
    ``projects/<pid>/sessions/<sid>/`` key already named in the manifest (the render runtime has no
    turn context of its own)."""
    from projects.storage_layout import find_session_key

    pids: list = []
    try:
        from jobs import context as _jc
        tp = _jc.trusted_project_id()
        if tp:
            pids.append(tp)
    except Exception:  # noqa: BLE001
        pass

    def walk(v):
        if isinstance(v, str):
            sk = find_session_key(v)
            if sk is not None and sk.pid and sk.sid == session_id and sk.pid not in pids:
                pids.append(sk.pid)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(manifest)
    return pids


def _cdn_url(key: str) -> str:
    from urllib.parse import quote
    return f"https://{_ARTIFACTS_DOMAIN}/{quote(key, safe='/')}"


def _stabilize_manifest_media_urls(manifest_key: str, session_id: Optional[str]) -> None:
    """Rewrite a persisted manifest's EXPIRING media URLs to STABLE artifact references.

    Media deliverables arrive as presigned M2C URLs that expire in < 1 h, so a persisted render
    manifest breaks when the session is reloaded later (dead image/video/audio). A stored
    manifest must therefore hold an identifier, never a URL — and never a COPY of the bytes
    either (asset-lineage D4/R4: an M2C output is referenced where M2C wrote it).

    Per field, in this order:
      * an already-stable value (artifacts domain, non-S3 host, ``s3://``) is left alone;
      * a presigned URL of an object in OUR artifacts bucket (e.g. the uploaded source video)
        becomes its CloudFront path — a rewrite, not a copy;
      * an M2C output already adopted into this session's output tree
        (``artifacts/<sid>/output/<execUUID>/<basename>``, same bytes) becomes THAT copy, so the
        clip and its analyses keep one ``sourceKey`` and one tab (session 764a71c0);
      * anything else that names an S3 bucket + key becomes an M2C_REFERENCE
        ``artifacts/m2c/<bucket>/<key>`` (``media.artifact_fetch.m2c_artifact_ref``), which the
        proxy presigns at view time for an actor who can read the project that references it.
      * a value we cannot name an object from keeps its presigned URL (unchanged fallback).

    This REPLACED a byte mirror into ``artifacts/durable/<digest>/…``: those copies expire after
    60 days while the M2C originals never do, so every render older than that broke while its
    data still existed. Existing ``artifacts/durable/`` keys keep resolving — nothing here writes
    a new one. Never raises; a failure must never break the render.
    """
    if not (_SKILLS_BUCKET and _ARTIFACTS_DOMAIN and manifest_key):
        return
    s3 = _s3_client()
    try:
        obj = s3.get_object(Bucket=_SKILLS_BUCKET, Key=manifest_key)
        manifest = json.loads(obj["Body"].read())
    except Exception:
        logger.warning("stable refs: cannot read manifest %s", manifest_key, exc_info=True)
        return
    if not isinstance(manifest, dict):
        return

    cache: dict[str, str] = {}  # object-identity (url sans query) -> stable reference
    project_ids = _manifest_project_ids(manifest, session_id) if session_id else []

    def _stable(url: str) -> str:
        if not isinstance(url, str) or not url.startswith("https://"):
            return url
        if _ARTIFACTS_DOMAIN in url:
            return url  # already a stable artifact URL
        host = url.split("/", 3)[2].split("?", 1)[0] if "//" in url else ""
        if not host.endswith(".amazonaws.com"):
            return url  # only an S3 REST URL names a bucket + key
        # An object already in OUR artifacts bucket (e.g. the uploaded source video) is rewritten
        # to its CloudFront path: it is already durable, so referencing it anywhere else would
        # just give the same bytes a second identity.
        if _SKILLS_BUCKET:
            from urllib.parse import urlparse
            _p = urlparse(url)
            _path = _p.path.lstrip("/")
            _own_key = None
            if _p.netloc.split(".", 1)[0] == _SKILLS_BUCKET:        # virtual-hosted
                _own_key = _path
            elif _path.startswith(_SKILLS_BUCKET + "/"):            # path-style
                _own_key = _path[len(_SKILLS_BUCKET) + 1:]
            if _own_key:
                return f"https://{_ARTIFACTS_DOMAIN}/{_own_key}"
        ident = url.split("?", 1)[0]
        if ident in cache:
            return cache[ident]
        last = _basename_key(ident)
        exec_uuid = _last_exec_uuid(ident)
        # Session 764a71c0: an M2C output ALREADY adopted into this session's output tree is the
        # same bytes under a key the clip's analyses also key to. Prefer it, or the clip and its
        # analyses get different sourceKeys and the UI opens a second tab for the same media.
        if exec_uuid and session_id and last:
            for adopted in _adopted_output_keys(session_id, exec_uuid, last, project_ids):
                try:
                    s3.head_object(Bucket=_SKILLS_BUCKET, Key=adopted)
                    stable = _cdn_url(adopted)
                    cache[ident] = stable
                    logger.info("stable refs: %s already adopted at %s", ident[-60:], adopted)
                    return stable
                except Exception:  # noqa: BLE001 - absent (404) or unreadable: try the next root
                    continue
        try:
            from media.artifact_fetch import canonical_location, m2c_artifact_ref
            location = canonical_location(ident) or ""
            bucket, _, key = location[len("s3://"):].partition("/") if location else ("", "", "")
            ref = m2c_artifact_ref(bucket, key)
        except Exception:  # noqa: BLE001 - identity is best-effort, never break a render
            logger.warning("stable refs: cannot reference %s; keeping presigned",
                           ident[-60:], exc_info=True)
            return url
        if not ref:
            logger.warning("stable refs: %s names no S3 object; keeping presigned", ident[-60:])
            return url
        cache[ident] = ref
        logger.info("stable refs: %s -> %s", ident[-60:], ref)
        return ref

    changed = False

    def _rewrite(container: dict, field: str) -> None:
        nonlocal changed
        v = container.get(field)
        if isinstance(v, str):
            nv = _stable(v)
            if nv != v:
                container[field] = nv
                changed = True

    for t in (manifest.get("tracks") or []):
        if isinstance(t, dict):
            _rewrite(t, "src")
            _rewrite(t, "poster")

    # `media.src` and `sourceUrl` too — NOT just the tracks.
    #
    # Templates do not all read the same field. `VideoAnnotations.readMediaSrc` prefers
    # `media.src` and only falls back to the video track, so a manifest whose track was
    # rewritten while `media.src` kept its presigned URL plays for as long as the
    # signature lives and then shows "Source video could not be loaded" — with a stable
    # reference to the same bytes sitting one field away. Session 59c46b2c: the
    # face-recognition tab had `tracks[0].src` on CloudFront and `media.src` /
    # `sourceUrl` still pointing at `m2cv6-…-proxy.s3.amazonaws.com/...?AWSAccessKeyId=…`.
    #
    # `_stable` is identity-cached on the URL sans query, so these resolve to the same
    # reference the track already produced.
    media = manifest.get("media")
    if isinstance(media, dict):
        _rewrite(media, "src")
        _rewrite(media, "poster")
    # The analysed source, which several templates display and one plays. Rewriting it
    # does not disturb render dedup: that compares `candidate_keys`, which are recorded
    # raw precisely so referenced and presigned forms of one object still match.
    _rewrite(manifest, "sourceUrl")

    # A clip gallery's cards (`galleryClips`, or a client-built gallery's `clips`) play their own src too.
    for field in ("galleryClips", "clips"):
        for c in (manifest.get(field) or []):
            if isinstance(c, dict):
                _rewrite(c, "src")

    imgs = manifest.get("images")
    if isinstance(imgs, list):
        for i, im in enumerate(imgs):
            if isinstance(im, dict):
                _rewrite(im, "src")
            elif isinstance(im, str):
                nv = _stable(im)
                if nv != im:
                    imgs[i] = nv
                    changed = True
    _rewrite(manifest, "src")
    _rewrite(manifest, "poster")

    if changed:
        try:
            s3.put_object(Bucket=_SKILLS_BUCKET, Key=manifest_key,
                          Body=json.dumps(manifest).encode(), ContentType="application/json")
            logger.info("stable refs: rewrote manifest %s (%d stable reference(s))",
                        manifest_key, len(cache))
        except Exception:
            logger.warning("stable refs: cannot write back manifest %s", manifest_key, exc_info=True)


def _manifest_id_for(urls: list[str]) -> str:
    """Deterministic manifest id = hash of the normalized candidate URL set.

    Making the render manifest CONTENT-ADDRESSED closes the render-dedup concurrency race
    without a lock: two truly-simultaneous dispatches of the same artifact set (e.g. the hook
    firing as a tool result streams AND again when the async job settles, or two runtimes) each
    used to mint a random uuid and write a DIFFERENT S3 manifest key -> two tabs. A deterministic
    id makes both converge on ONE key -> the second write idempotently overwrites the first ->
    one tab. Order-independent and stable across presigned/mirrored forms (via _norm_url). Falls
    back to a random id only when there are no urls (never happens on a real dispatch)."""
    from render.review import _norm_url
    import hashlib
    keys = sorted({_norm_url(u) for u in urls if isinstance(u, str) and u.strip()})
    if not keys:
        return uuid.uuid4().hex
    return hashlib.sha256("|".join(keys).encode("utf-8")).hexdigest()[:32]


def _render_manifest_id(render_key: str, urls: list[str]) -> str:
    """The render manifest id = the STABLE job/node id when known, else the content hash.

    Keying on the job/node (design §4 B) makes rendering idempotent PER JOB: a re-render on a
    later turn, or the same media surfaced under a different url form, converges on ONE manifest
    S3 key (one tab). The url content-hash is only the fallback for a direct render that has no
    job/node context."""
    if render_key:
        import re
        mid = re.sub(r"[^A-Za-z0-9._-]", "_", render_key)[:120]
        if mid:
            return mid
    return _manifest_id_for(urls)


def write_session_manifest(session_id: str, manifest_id: str, manifest: dict) -> Optional[str]:
    """Write a session-scoped render manifest to S3; return its key, or ``None``.

    Key format matches ``render_agent._write_manifest_to_s3``:
    ``<ARTIFACTS_PREFIX><session_id>/render/<manifest_id>.json``. Used for a Moments manifest
    (``moments_render`` / the batch flush), which carries Asset_References and NO URL, so
    ``_stabilize_manifest_media_urls`` is deliberately NOT called: there is no expiring deliverable
    URL to rewrite. The
    Main_Agent role already writes this prefix, so no IAM change. Returns ``None`` when the bucket
    is unknown (unit tests) or the PutObject fails -- a write failure must never break the hook or
    the flush that calls it.
    """
    if not (_SKILLS_BUCKET and session_id):
        return None
    mid = str(manifest_id or "").strip()
    if not mid:
        # Never key a manifest `.../render/.json` (every empty-id write would collide). Prefer the
        # body's own id, else a deterministic content hash -- mirrors _write_manifest_to_s3.
        import hashlib
        body = json.dumps(manifest, sort_keys=True)
        mid = str((manifest or {}).get("id") or "").strip() or \
            hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]
    s3_key = f"{_ARTIFACTS_PREFIX}{session_id}/render/{mid}.json"
    try:
        from render.richness import prepare_write
        keep, manifest = prepare_write(_s3_client(), _SKILLS_BUCKET, s3_key, manifest)
        if keep:
            return s3_key  # richer wins (R7.3); the existing key is still announced
    except Exception:  # noqa: BLE001 - the guard must never block a write
        logger.debug("write_session_manifest: richer-wins check skipped for %s", s3_key)
    try:
        _s3_client().put_object(
            Bucket=_SKILLS_BUCKET,
            Key=s3_key,
            Body=json.dumps(manifest).encode("utf-8"),
            ContentType="application/json",
        )
        return s3_key
    except Exception:  # noqa: BLE001 - never break a hook/flush path over a write
        logger.warning("write_session_manifest: S3 write failed for %s", s3_key, exc_info=True)
        return None


def _rendered_in(url: str, already: set) -> bool:
    """``url`` (in any of its forms: CDN, presigned, s3://) is one of the session's rendered media."""
    from render.review import _norm_url, _media_identity
    if _norm_url(url) in already:
        return True
    m = _media_identity(url)
    return bool(m) and m in already


def rendered_this_session(url: str) -> bool:
    """True when this session already shows ``url`` in a rendered tab (the durable, manifest-derived set)."""
    try:
        from jobs.context import current_session_id
        return _rendered_in(url, _already_rendered_urls(current_session_id() or ""))
    except Exception:  # noqa: BLE001 - unknown means "not shown": the caller renders it
        return False


def _file_label(urls: list[str]) -> Optional[str]:
    """The first url's file name (``+N`` for the rest), for a render no caller named; ``None`` without one."""
    from urllib.parse import unquote, urlparse
    names = [unquote(urlparse(u).path.rsplit("/", 1)[-1]) for u in urls if isinstance(u, str)]
    names = [n for n in names if n]
    if not names:
        return None
    return names[0] if len(names) == 1 else f"{names[0]} +{len(names) - 1}"


def dispatch_render(urls: list[str], source_urls: Optional[list[str]] = None,
                    label: Optional[str] = None,
                    render_request_id: Optional[str] = None,
                    render_key: str = "",
                    tool_name: str = "",
                    gallery: bool = False) -> Optional[str]:
    """Invoke the Render Agent for ``urls``; forward its progress to the active
    channel and RETURN the render-manifest S3 key (or None).

    ``source_urls`` carries the media the artifacts describe (e.g. the original
    video behind a detection result). It is presigned and listed under a
    separate prompt heading so the Render Agent can compose an overlay view,
    and is excluded from the dedup key so it never changes render eligibility.

    Surfaces the manifest by pushing a step carrying ``render_manifest_key`` to
    the active progress channel:
      * main agent  → the main streaming loop yields it → UI render event;
      * task agent  → drained into the task agent's SSE stream →
        ``dispatch_tool._consume_stream`` → main channel → UI.
    ``label`` is a human name for the view (e.g. "Shot Segmentation"). It rides
    along with the manifest key so the UI can title the tab after the work that
    produced it instead of a generic placeholder.

    Also returns the key for programmatic callers (e.g. the render hook action).
    """
    urls = [u for u in (urls or []) if isinstance(u, str) and u.strip()]
    # A render nobody named is named after its file, never left to the UI's "Render Output N" (8e2acba8's three
    # recast results arrived as three "Render Output" tabs).
    label = label or _file_label(urls)
    if _gallery_shown:
        shown = [u for u in urls if _shown_in_gallery(u)]
        if shown:
            logger.info("dispatch_render: %d url(s) already in a named gallery; not rendered again", len(shown))
            urls = [u for u in urls if not _shown_in_gallery(u)]
    sources = [u for u in (source_urls or []) if isinstance(u, str) and u.strip()]

    def _resolve_orphan_marker() -> None:
        # A composing marker emitted by the caller (a batched render announces it at
        # BUFFER time) must always get a terminal signal, or its UI tab spins forever.
        # When we bail before producing a manifest, resolve it with render_failed.
        # Direct callers pass no rid and emit their own marker below, after these
        # guards, so they never orphan.
        if render_request_id is not None:
            _ch = get_active_channel()
            if _ch is not None:
                _ch.push("failed", "↳ Render produced no viewable output", agent="render",
                         extra={"render_request_id": render_request_id, "render_failed": True})

    if not urls:
        _resolve_orphan_marker()
        return None
    # In-process dedup: skip if we already rendered this exact URL set in the current runtime
    # invocation (prevents a hook + LLM double-render).
    #
    # RECLASSIFIED (2026-08-18). Now that both render paths converge on one key -- the hook via
    # `canonical_render_key(job_id)`, the explicit path via `render_key_from_artifact_paths` --
    # a duplicate dispatch writes the SAME S3 key and overwrites idempotently, so this is no
    # longer needed for CORRECTNESS. It is kept as a COST short-circuit: without it a duplicate
    # costs a full render-agent invocation (up to RENDER_DISPATCH_BUDGET_SECONDS, default 90s)
    # instead of one frozenset lookup, and that latency lands in the user's turn.
    #
    # Verified by experiment, not assumed: disabling this check leaves the whole suite green
    # except one test that PRE-SEEDS this set as a fixture to reach the "bailed without a
    # manifest" branch (`test_dispatch_render_with_rid_does_not_double_announce_and_resolves_orphan`)
    # -- i.e. the dedup was that test's fixture, not its subject. So the redesign's §7 plan to
    # delete this layer is now safe on correctness grounds and is being declined on cost grounds.
    url_key = frozenset(urls)
    if url_key in _rendered_url_sets:
        logger.info("dispatch_render: skipping duplicate render for same URL set")
        _resolve_orphan_marker()
        return None
    if len(_rendered_url_sets) > _RENDER_DEDUP_MAX:
        _rendered_url_sets.clear()
    _rendered_url_sets.add(url_key)
    # Expand directory outputs to concrete artifacts, then add diarization sibling audio tracks.
    urls = _expand_s3_prefixes(urls)
    urls = _expand_m2c_siblings(urls)
    # Do NOT self-presign the OUTPUT candidates here. Job outputs live in the M2C bucket, which the
    # media gateway's download-url tool resolves; letting the Render Agent resolve them via that tool
    # (its single, least-privilege owner) means the main/task role needs no raw S3 IAM on that bucket.
    # Self-presigning here (main-role generate_presigned_url) shadowed that path -- the agent's gateway
    # resolver only acts on s3://, so a pre-presigned https url made it a no-op. Keep s3:// (resolved
    # downstream) alongside any already-http url; sources are still presigned below (skills-bucket
    # uploads the gateway does not own).
    urls = [u for u in urls if u and (u.startswith("http") or u.startswith("s3://"))]
    if not urls:
        logger.warning("dispatch_render: no renderable URLs to dispatch")
        return None
    # A tab must never be titled with a raw job/request id. If the caller had no meaningful label
    # (a fal submit_job node whose only id was its request_id), title the tab by the media kind so
    # it reads "Video"/"Image"/"Audio" consistently with every other render tab.
    if _looks_like_raw_id(label):
        label = _media_kind_label(urls)
    # Durable, session-scoped dedup (F4b): drop urls this session already rendered, derived from
    # persisted manifests so it survives a cold start / a later turn -- the in-process set above
    # cannot. Never blocks on failure (empty set). The already-rendered set is also handed to the
    # render agent so validate_render_urls can reject a duplicate the model re-picks.
    #
    # SKIPPED when this render already has a stable key. The scan costs one list_objects_v2 plus a
    # get_object for up to 50 manifests, on EVERY dispatch. It exists because url-keyed identity
    # could not tell that two url forms were the same object -- but a render with a `render_key`
    # (a plan node, `canonical_render_key(job_id)`, or a path-derived node) writes a DETERMINISTIC
    # S3 key, so a re-render overwrites the one manifest and the tab count is right by
    # construction. The scan could then only tell us something we would act on identically.
    # Verified by experiment: disabling it entirely leaves the suite green except one test that
    # uses it as a fixture to reach the "dropped, resolve the marker" branch.
    #
    # Kept for the KEYLESS case, where it is still load-bearing: no job and no derivable node (a
    # synchronous `artifact` call, a legacy flat upload key) means identity falls back to a content
    # hash. It also still covers the narrow window where a cold start coincides with a registry
    # outage, so `canonical_render_key` degrades to the job id rather than the node id and the two
    # renders key differently (asserted in test_render_key_cold_start.py).
    _already = set()
    if not render_key:
        try:
            from jobs.context import current_session_id as _csid
            _already = _already_rendered_urls(_csid() or "")
        except Exception:  # noqa: BLE001
            _already = set()
    if _already:
        fresh = [u for u in urls if not _rendered_in(u, _already)]
        if not fresh:
            logger.info("dispatch_render: all %d url(s) already rendered this session; skipping", len(urls))
            # Resolve the composing marker emitted upstream (batched render announces it at BUFFER
            # time). Without this the deduped render's "Composing…" tab spins forever — it LOOKS
            # like a second tab even though the render was correctly dropped (session a1dfec9c).
            _resolve_orphan_marker()
            return None
        if len(fresh) != len(urls):
            logger.info("dispatch_render: dropped %d already-rendered url(s)", len(urls) - len(fresh))
        urls = fresh
    render_arn = _render_agent_arn()
    if not render_arn:
        logger.warning("dispatch_render: RENDER_AGENT_RUNTIME_ARN unset; cannot render")
        return None

    # These urls are the CANDIDATE OUTPUTS the render agent resolves from and must validate its
    # choice against (validate_render_urls). Labeling them explicitly — rather than only
    # "render these" — gives the agent the exact array to pass as candidates_json, so the guard
    # has a real feed instead of being a no-op.
    # Manifest identity = the JOB/NODE (design §4 B): one render per job, keyed by a STABLE id, so
    # a re-render (a later "retrieve/summarise" turn) or the same media under a different url form
    # converges on ONE manifest S3 key -> one tab. Falls back to the content hash only for a direct
    # render with no job/node context.
    manifest_id = _render_manifest_id(render_key, urls)
    from render.review import _norm_url as _nrm_ck
    candidate_keys = sorted({_nrm_ck(u) for u in urls if isinstance(u, str) and u.strip()})
    payload = {"prompt": (
        "Render these artifacts as one unified timeline. These are the CANDIDATE OUTPUT URLS "
        "(pass them as candidates to validate_render_urls, and render only the urls it returns "
        "as valid):\n" + "\n".join(urls)
        + f"\n\nManifest ID to use — pass this VERBATIM as the manifest_id argument to "
          f"build_render_manifest (do NOT invent your own; it makes the render content-addressed "
          f"so a duplicate dispatch overwrites instead of creating a second tab): {manifest_id}"
    )}
    # Pass the content-addressed id + candidate keys as STRUCTURED fields too. The render agent
    # generates its own uuid manifest_id and injects THAT into the model prompt, so the id in the
    # prompt above was discarded every time -> every render got a fresh uuid -> duplicate tabs.
    # The render entrypoint now uses payload["manifest_id"] deterministically (both the agentic and
    # deterministic paths), and stamps payload["candidate_keys"] onto the manifest for dedup.
    payload["manifest_id"] = manifest_id
    payload["candidate_keys"] = candidate_keys
    # Coalesced FAN-OUT of independent clips (e.g. a lip-sync per-segment review): tell the
    # render entrypoint to build ONE clip-gallery (multiview), not a stacked timeline. The
    # deterministic path honors this before skill selection (see _render_deterministic). The
    # signal rides the render_key prefix set by render_action's fan-out coalescing, so the flush
    # call site (and its test mocks) need no new argument.
    if gallery or str(render_key or "").startswith("seggallery--"):
        payload["gallery"] = True
    # The producing tool — the AUTHORITATIVE view key. `_render_deterministic` prefers
    # the skill this maps to over MIME/content inference, so a shot-segmentation job
    # always renders the shot view regardless of its JSON shape.
    if tool_name:
        payload["tool"] = tool_name
    if sources:
        published_sources = [u for u in _publish_s3_uris(sources) if u and u.startswith("http")]
        if published_sources:
            payload["prompt"] += (
                "\n\n" + SOURCE_MEDIA_PROMPT_HEADING + "\n" + "\n".join(published_sources)
                + "\n(These SOURCE urls are inputs — pass them as source to validate_render_urls; "
                "never render them as outputs.)"
            )
    if _already:
        payload["prompt"] += (
            "\n\nALREADY RENDERED this session (pass as rendered to validate_render_urls; do not "
            "re-render):\n" + "\n".join(sorted(_already))
        )
    try:
        from jobs.context import current_actor_id, current_session_id
        _actor = current_actor_id()
        _session = current_session_id()
        payload["actor_id"] = _actor
        payload["session_id"] = _session
    except Exception:
        _actor = _session = None

    # Intent recovery (best-effort): the hook fires on a tool result whose turn text is useless
    # for intent ("retrieve and summarise"); the request that explains this artifact ("give me the
    # shot segments") was made turns earlier. Feed the last few genuine human turns so the render
    # agent can pick, among a skill's declared deliverables, the one matching what the user asked.
    # Any failure is silently skipped -- the render still runs on the skill's primary deliverable.
    try:
        import os as _os
        from render.intent import read_recent_user_turns
        _mid = _os.environ.get("MEMORY_ID", "")
        _turns = read_recent_user_turns(_memory_client(), _mid, _actor or "", _session or "",
                                        max_turns=6)
        if _turns:
            payload["prompt"] += (
                "\n\nRECENT USER REQUESTS (most recent first) — when a job produced several "
                "candidate outputs, render the one(s) matching what the user actually asked for:\n"
                + "\n".join(f"- {t}" for t in _turns)
            )
    except Exception:  # noqa: BLE001 - intent is a nicety, never a blocker
        pass

    import uuid as _uuid
    if render_request_id is None:
        # Direct (non-batched) caller: no marker was emitted upstream, so generate the
        # id and announce the composing render here.
        #
        # Batched renders (the render-artifacts hook) instead announce this marker at
        # BUFFER time — while the event loop is still free — and pass the id in here.
        # That is deliberate: this dispatch runs inside the synchronous session_end
        # hook and BLOCKS the event loop for the whole render (plus the stable-ref pass),
        # starving the progress poller. A marker emitted *here* would therefore not
        # reach the UI until the manifest did (both drained together at teardown), so
        # the per-tab composing animation never gets a frame. Announcing at buffer time
        # lets the poller flush the marker live, and the animation then covers the
        # entire render + copy.
        render_request_id = _uuid.uuid4().hex
        _early_channel = get_active_channel()
        if _early_channel is not None:
            _early_extra = {"render_request_id": render_request_id}
            if label:
                _early_extra["render_label"] = label
            _early_channel.push("running", "↳ Composing render…", agent="render", extra=_early_extra)
    logger.info("dispatch_render: composing marker rid=%s label=%r urls=%d",
                render_request_id, label, len(urls))

    logger.info("dispatch_render: %d URL(s) → render agent %s", len(urls), render_arn[-40:])
    client = _get_client()
    from jobs.context import invoke_agent_runtime_traced as _invoke_traced
    response = _invoke_traced(
        client,
        session_id=payload.get("session_id"),
        agentRuntimeArn=render_arn,
        qualifier="DEFAULT",
        payload=json.dumps(payload).encode(),
        contentType="application/json",
    )

    channel = get_active_channel()
    manifest_key: Optional[str] = None
    # A deliberate "nothing to show" (render agent `no_view`): a clean, terminal no-op, not a failure.
    no_view_reason: Optional[str] = None
    # Bulkhead: bound how long a render can block the turn. The render agent has
    # its own 30s pipeline budget; if it hangs beyond this wall-clock budget we
    # abandon the render (the turn continues without it) rather than blocking up
    # to the invoke read timeout (~600s).
    _budget = float(os.environ.get("RENDER_DISPATCH_BUDGET_SECONDS", "90"))
    _start = time.time()
    for line in _iter_lines(response.get("response", b"")):
        if time.time() - _start > _budget:
            logger.warning(
                "dispatch_render: exceeded %.0fs budget; abandoning render "
                "(turn continues without it)", _budget,
            )
            break
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        if not line or not line.startswith("data: "):
            continue
        try:
            parsed = json.loads(line[6:])
            if isinstance(parsed, str):
                parsed = json.loads(parsed)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(parsed, dict):
            continue
        ev_type = parsed.get("type")
        if ev_type == "step" and channel is not None:
            channel.push(parsed.get("status") or "running",
                         f"↳ {parsed.get('label') or 'Render Agent working...'}",
                         agent="render")
        elif ev_type == "no_view":
            no_view_reason = str(parsed.get("reason") or "no_view")
            logger.info("dispatch_render: render agent declined rid=%s (%s)", render_request_id, no_view_reason)
        elif ev_type == "output_ref":
            key = parsed.get("s3_key") or ""
            if "/render/" in key and key.endswith(".json") and not parsed.get("error"):
                if not _manifest_exists(key):
                    logger.warning(
                        "dispatch_render: render agent announced manifest %s but it is "
                        "not present in %s — skipping surface (avoids UI 404)",
                        key, _SKILLS_BUCKET,
                    )
                    continue
                manifest_key = key
                if channel is not None:
                    extra = {"render_manifest_key": key, "render_request_id": render_request_id}
                    if label:
                        extra["render_label"] = label
                    logger.info("dispatch_render: manifest rid=%s key=%s", render_request_id, key)
                    channel.push("done", "↳ Rendered artifacts", agent="render",
                                 extra=extra)
    if manifest_key:
        # Rewrite the manifest's expiring deliverable URLs to stable references so it survives
        # the presigned URLs expiring. Runs before the manifest step reaches the UI (drained at
        # teardown), so even the first view holds references. The composing animation (marker
        # emitted at buffer time) covers this latency.
        try:
            # Generated images group into ONE gallery PER SESSION (the composer's default), which is
            # what users expect for a storyboard: every panel together, nothing left in its own tab.
            # So we do NOT stamp a finer (per-turn / per-generation) renderGroup here — a jobless fal
            # generation gets a UNIQUE job id that would fragment the gallery (verified session
            # 66bc0733). Leaving renderGroup unset routes every generated image into the composer's
            # session bucket, and skips a manifest re-write when nothing else changed.
            _stabilize_manifest_media_urls(manifest_key, payload.get("session_id"))
        except Exception:  # noqa: BLE001 - best-effort, never break render
            logger.warning("dispatch_render: stable-ref pass raised for %s", manifest_key, exc_info=True)
    if manifest_key is None and no_view_reason and channel is not None:
        # Nothing to show, by design (e.g. data-only JSON). Same terminal id as a failure so the UI drops any
        # composing placeholder, but a neutral "done" step -- not "Render produced no viewable output".
        channel.push("done", "↳ Nothing to display — the result is data, not a view", agent="render",
                     extra={"render_request_id": render_request_id, "render_failed": True,
                            "render_no_view": True, "render_no_view_reason": no_view_reason})
        return None
    if manifest_key is None and channel is not None:
        # The composing marker was emitted but no manifest resulted (deduped
        # downstream, budget exceeded, no renderable output, or an error). Resolve
        # the placeholder with a terminal signal so the UI tab does not spin
        # forever — the client removes the composing tab on this render_failed id.
        logger.info("dispatch_render: NO manifest for rid=%s — emitting render_failed", render_request_id)
        channel.push("failed", "↳ Render produced no viewable output", agent="render",
                     extra={"render_request_id": render_request_id, "render_failed": True})
    return manifest_key


def render_artifacts(tool, **kwargs):
    """Strands tool handler — invoke the Render Agent for the given URLs."""
    tool_use_id = tool["toolUseId"]
    inp = tool["input"]
    urls = inp.get("urls") or []
    if isinstance(urls, str):
        urls = [urls]
    urls = [u for u in urls if isinstance(u, str) and u.strip()]

    if not urls:
        return {"toolUseId": tool_use_id, "status": "error",
                "content": [{"text": "render_artifacts requires at least one URL"}]}
    if not _render_agent_arn():
        return {"toolUseId": tool_use_id, "status": "error",
                "content": [{"text": "Render Agent runtime not configured (RENDER_AGENT_RUNTIME_ARN unset)"}]}

    try:
        key = dispatch_render(urls)
        if key:
            return {"toolUseId": tool_use_id, "status": "success",
                    "content": [{"text": f"Rendered {len(urls)} artifact(s) into a unified timeline."}]}
        else:
            return {"toolUseId": tool_use_id, "status": "error",
                    "content": [{"text": "Render was attempted but no manifest was produced. The artifacts may not be viewable."}]}
    except Exception as e:
        logger.exception("render_artifacts failed")
        return {"toolUseId": tool_use_id, "status": "error", "content": [{"text": f"Render failed: {e}"}]}
