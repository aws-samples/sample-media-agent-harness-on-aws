"""``extract_frames`` and ``clip_video``: the timeline's fast Frame / Clip, as agent tools.

They run the same deterministic code as the timeline buttons (:mod:`media_actions`) in the chat's WARM sandbox:
one code-interpreter session per chat, kept alive between calls, with ffmpeg and the source video cached in it.
A request like "extract a clear key frame from shots 1-8" is then one call that takes about a second, instead of
a hand-written cell that downloads ffmpeg and the source again (staging 2026-10-19: one such cell took 16 min
when a download stalled; the identical work is 9.6 s cold and ~1 s warm). There is no time cap: long or complex
sandbox work still goes through ``code_interpreter`` unchanged.

A clip is a stream copy (frame-accurate through the MP4 edit list, ~0.1 s of ffmpeg); frames are JPEGs at the
exact timestamps. Outputs land in the session's output folder and become project assets like any session output.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

from strands import tool

logger = logging.getLogger(__name__)

from media_actions import MAX_POINTS as MAX_FRAMES, MAX_RANGES as MAX_CLIPS  # noqa: E402  (one cap, not two)


def _resolve_source(source_url: str) -> tuple[Optional[dict], Optional[str]]:
    """``({"url", "s3uri", "name"}, None)`` for a fetchable video the turn may read, else ``(None, reason)``."""
    src = (source_url or "").strip()
    if not src:
        return None, "source_url is required"
    try:
        from media import persist_media

        persist_media.validate_fetchable(src)  # SSRF guard: the URL is model-supplied
    except Exception as exc:  # noqa: BLE001
        return None, f"source not fetchable: {exc}"
    from jobs.context import trusted_project_id
    from media import artifact_fetch

    uri = artifact_fetch.canonical_location(src)
    if not uri or not uri.startswith("s3://"):
        return None, ("source_url must be a file of this chat or project (its artifact / project URL); "
                      "for other URLs use code_interpreter")
    fetch = artifact_fetch.presign_for_trusted_turn(src, 3600, trusted_project_id()) or src
    return {"url": fetch, "s3uri": uri, "name": uri.rsplit("/", 1)[-1]}, None


def _run(op: str, source: dict, **spec) -> str:
    import media_actions as ma
    from jobs import context as jc

    session_id = jc.current_session_id() or ""
    if not session_id:
        return json.dumps({"error": "no chat session for this call"})
    raw = {"op": op, "action_id": f"agent-{uuid.uuid4().hex[:10]}", "source": source, **spec}
    try:
        act = ma.parse_action(raw)
    except ma.MediaActionError as exc:
        return json.dumps({"error": str(exc)})
    res = ma.run_action(act, session_id=session_id, project_id=jc.trusted_project_id())
    if not res.get("ok"):
        return json.dumps({"error": res.get("error") or "the action failed"})
    _note_lineage(op, source, res.get("outputs") or [])
    return json.dumps({"status": "done", "elapsedMs": res.get("elapsedMs"),
                       "outputs": [{"name": o["name"], "url": o["url"], "kind": o["kind"]} for o in res["outputs"]]})


_TOOL_OF_OP = {"frame": "extract_frames", "clip": "clip_video"}


def _note_lineage(op: str, source: dict, outputs: list) -> None:
    """projects.lineageEdges: each output is made from the source over its own time range (a frame's instant, a
    clip's in/out). The tool's result names only CDN URLs and none of its source, so the ledger's generic paths
    cannot see this; it is recorded here, where both are known. A no-op with the flag off; never raises."""
    try:
        from projects import lineage_contract as lc

        if not lc.edges_enabled():
            return
        from media import turn_outputs

        src = source.get("s3uri")
        for o in outputs:
            rng = o.get("range") if isinstance(o.get("range"), dict) else None
            turn_outputs.note_write(o.get("s3uri"), [src], tool=_TOOL_OF_OP.get(op, op),
                                    ranges=[{"input": src, **rng}] if rng and src else [],
                                    settings={"op": op})
    except Exception:  # noqa: BLE001 - lineage never breaks the tool
        logger.debug("media_quick: lineage not recorded", exc_info=True)


@tool
def extract_frames(source_url: str, times: list[float]) -> str:
    """Extract still frames (JPEG) of ONE video at exact times — fast (~1 s), in the chat's warm sandbox.

    Use this for "a frame at 0:23", "key frames of shots 1-8" (one time per shot, e.g. its midpoint from the
    shot list), thumbnails or stills. Prefer it to writing an ffmpeg cell in code_interpreter: ffmpeg and the
    video are already cached here, so nothing is downloaded again.

    Args:
        source_url: URL of the video — a file of this chat or project (the ``[source: …]`` URL, an artifact URL,
            or a project file URL). Not an s3:// URI.
        times: Seconds into the video, one per frame (up to 20), e.g. [4.5, 7.2, 11.1].

    Returns:
        JSON ``{"status":"done","outputs":[{"name","url","kind":"image"}],"elapsedMs"}`` or ``{"error": ...}``.
    """
    pts = [t for t in (times or []) if isinstance(t, (int, float)) and t >= 0]
    if not pts:
        return json.dumps({"error": "times must be a list of seconds (>= 0)"})
    if len(pts) > MAX_FRAMES:
        return json.dumps({"error": f"at most {MAX_FRAMES} frames per call"})
    source, why = _resolve_source(source_url)
    if source is None:
        return json.dumps({"error": why})
    return _run("frame", source, points=[int(round(t * 1000)) for t in pts])


@tool
def clip_video(source_url: str, ranges: list[list[float]]) -> str:
    """Cut one or more clips of ONE video by time range — a stream copy (~0.1 s each), frame-accurate start.

    Use this for "clip 0:23-0:30", "cut shots 2, 5 and 7 into separate clips". For a re-encode (resize,
    transcode, a different codec) or joining clips with transitions, use the media-processing tools instead.

    Args:
        source_url: URL of the video — a file of this chat or project (the ``[source: …]`` URL, an artifact URL,
            or a project file URL). Not an s3:// URI.
        ranges: ``[[in_seconds, out_seconds], ...]`` (up to 10 clips), each ``out > in``.

    Returns:
        JSON ``{"status":"done","outputs":[{"name","url","kind":"video"}],"elapsedMs"}`` or ``{"error": ...}``.
    """
    spans = [r for r in (ranges or []) if isinstance(r, (list, tuple)) and len(r) == 2
             and all(isinstance(x, (int, float)) for x in r) and r[1] > r[0] >= 0]
    if not spans:
        return json.dumps({"error": "ranges must be [[in_seconds, out_seconds], ...] with out > in"})
    if len(spans) > MAX_CLIPS:
        return json.dumps({"error": f"at most {MAX_CLIPS} clips per call"})
    source, why = _resolve_source(source_url)
    if source is None:
        return json.dumps({"error": why})
    return _run("clip", source, ranges=[{"inMs": int(round(a * 1000)), "outMs": int(round(b * 1000))} for a, b in spans])
