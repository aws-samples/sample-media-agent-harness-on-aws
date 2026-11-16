"""``show_media_gallery``: open ONE gallery tab of playable media, labelled exactly as given, with no Render Agent.

For voice auditions, chunk / segment reviews and any "show me these N files": the Render Agent picked its own
layout and labelled cards by file id (session a5b5acaf). This writes the gallery manifest itself and announces it
like any render, so the tab opens (and is focused) at once.
"""
from __future__ import annotations

import json
import logging
import uuid

from strands import tool

logger = logging.getLogger(__name__)

MAX_ITEMS = 60


MAX_PEAKS = 1000


def _fetchable(src: str) -> str:
    """A URL this process can download ``src`` from: an M2C reference or one of our artifacts is presigned."""
    from media.artifact_fetch import M2C_REF_PREFIX
    if src.startswith(M2C_REF_PREFIX):
        bucket, _, key = src[len(M2C_REF_PREFIX):].partition("/")
        from render import artifacts as ra
        return ra._s3_client().generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key},
                                                      ExpiresIn=600)
    try:
        from jobs.context import trusted_project_id
        from media import artifact_fetch
        return artifact_fetch.presign_for_trusted_turn(src, 600, trusted_project_id()) or src
    except Exception:  # noqa: BLE001 - a public url still downloads as is
        return src


def _peaks_for(src: str):
    """Waveform peaks for one audio file (WAV), or None. Never raises."""
    try:
        from tools.peak_extractor import peak_extractor, resample_peaks
        out = json.loads(peak_extractor(_fetchable(src), 100))
        peaks = out.get("peaks") if isinstance(out, dict) else None
        if not isinstance(peaks, list) or not peaks:
            return None
        return resample_peaks(peaks, MAX_PEAKS) if len(peaks) > MAX_PEAKS else peaks
    except Exception as exc:  # noqa: BLE001 - a card without a waveform still plays
        logger.info("show_media_gallery: no waveform for %s: %s", src[-60:], exc)
        return None


def _add_waveforms(manifest: dict) -> None:
    """Give every audio card its waveform, so the user can see where to set In / Out.

    The Render Agent's views carried peaks (it ran ``peak_extractor``); a named gallery written here did
    not, so its audio cards were a bare player with nothing to trim by (staging 20fd99a7). Same extractor,
    run in parallel. Best-effort per card.
    """
    clips = [c for c in manifest.get("galleryClips") or [] if isinstance(c, dict) and c.get("kind") == "audio"
             and isinstance(c.get("src"), str) and not c.get("peaks")]
    if not clips:
        return
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(8, len(clips))) as pool:
        for clip, peaks in zip(clips, pool.map(lambda c: _peaks_for(c["src"]), clips)):
            if peaks:
                clip["peaks"] = peaks


def _stable_src(url):
    """An ``s3://`` uri as the reference the player resolves itself, so no presign step (and no ~1.5 KB
    presigned link for the model to copy out, which made a 12-voice audition's gallery call take ~57 s).
    Our artifacts bucket -> its CloudFront URL; any other bucket (M2C) -> ``artifacts/m2c/<bucket>/<key>``."""
    if not (isinstance(url, str) and url.startswith("s3://")):
        return url
    bucket, _, key = url[5:].partition("/")
    try:
        from render import artifacts as ra
        if bucket == ra._SKILLS_BUCKET and ra._ARTIFACTS_DOMAIN and key:
            return f"https://{ra._ARTIFACTS_DOMAIN}/{key}"
        from media.artifact_fetch import m2c_artifact_ref
        return m2c_artifact_ref(bucket, key) or url
    except Exception:  # noqa: BLE001 - an unconvertible uri is dropped by the builder like any non-url
        return url


@tool
def show_media_gallery(items: list[dict], title: str = "", subtitle: str = "") -> str:
    """Open ONE gallery tab with these media files, labelled as you name them. Fast; no render agent.

    Use it to let the user HEAR or SEE several files side by side: a voice audition (label each with the voice's
    NAME, e.g. "agent_olive_voice_v4 · female · en · your clone"), TTS chunks or lip-sync segments to review,
    or any "show me these" set. Audio and video become playable cards with a mark-in/out ruler; all-image sets
    become an image gallery; a single video opens in the video view (its timeline). Prefer this over dispatching
    the Render Agent for a gallery.

    Args:
        items: ``[{"url": <an s3:// uri as a tool returned it (preferred: e.g. get_voice's preview_uri -- no
            presign needed) or an https url>, "label": <what the user should read>,
            "text"?: <a line to show under it, e.g. the chunk's script>, "durationHint"?: <seconds>}]``
            (up to 60). A file that is already open in a tab is fine: its tab is brought to the front.
        title: the tab's heading, e.g. "Voice audition".
        subtitle: an optional second line.

    Returns:
        JSON ``{"status": "opened", "items": n}`` or ``{"error": ...}``.
    """
    from jobs import context as jc
    from orchestration.progress_channel import get_active_channel
    from render.artifacts import _stabilize_manifest_media_urls, write_session_manifest
    from render.gallery import build_gallery_manifest

    if not isinstance(items, list) or not items:
        return json.dumps({"error": "items must be a non-empty list of {url, label}"})
    if len(items) > MAX_ITEMS:
        return json.dumps({"error": f"at most {MAX_ITEMS} items per gallery"})
    items = [{**it, "url": _stable_src(it.get("url") or it.get("src"))} if isinstance(it, dict) else it
             for it in items]
    manifest = build_gallery_manifest(items, title=title, subtitle=subtitle)
    clips = manifest.get("galleryClips") or []
    if len(clips) == 1 and clips[0].get("kind") == "video" and str(clips[0].get("src") or "").startswith("https://"):
        # ONE video is not a gallery: it opens in the video view (timeline, Clip / Frame / Crop), as any rendered
        # video does. c7d66001's final reel came up as a one-card "Review & clip takes" gallery instead.
        from render.artifacts import dispatch_render, rendered_this_session
        if rendered_this_session(clips[0]["src"]):  # already open in its own video tab (a plan's render did it)
            return json.dumps({"status": "already_open", "items": 1, "template": "video"})
        if dispatch_render([clips[0]["src"]], label=title or clips[0].get("label") or None):
            return json.dumps({"status": "opened", "items": 1, "template": "video"})
    n = len(manifest.get("galleryClips") or manifest.get("images") or [])
    try:
        # The ORIGINAL (presigned) links, normalised: the end-of-turn auto-render matches these and skips them,
        # so the named gallery is the only tab (the stabilizer below rewrites the cards' own src).
        from render.review import _norm_url
        srcs = [c["src"] for c in (manifest.get("galleryClips") or manifest.get("images") or []) if isinstance(c, dict)]
        manifest["candidate_keys"] = sorted({_norm_url(u) for u in srcs if u})
        from render.artifacts import note_gallery_media
        note_gallery_media(srcs)
        from hooks.actions import note_gallery_classes
        note_gallery_classes(srcs)
    except Exception:  # noqa: BLE001 - dedup metadata is a nicety
        pass
    if not n:
        return json.dumps({"error": "no item has an https url or s3:// uri"})
    sid = jc.current_session_id() or ""
    if not sid:
        return json.dumps({"error": "no chat session for this call"})
    _add_waveforms(manifest)
    key = write_session_manifest(sid, manifest["id"], manifest)
    if not key:
        return json.dumps({"error": "the gallery could not be saved"})
    # Expiring presigned links become stable references, so the tab still plays after the links expire.
    _stabilize_manifest_media_urls(key, sid)
    channel = get_active_channel()
    if channel is not None:
        extra = {"render_manifest_key": key, "render_request_id": uuid.uuid4().hex}
        if title:
            extra["render_label"] = title
        channel.push("done", f"↳ Opened {title or 'gallery'}", agent="render", extra=extra)
    logger.info("show_media_gallery: %s (%d item(s), %s)", key, n, manifest["template"])
    return json.dumps({"status": "opened", "items": n, "template": manifest["template"]})
