"""Direct timeline actions — Frame / Clip / Crop with NO model turn (design 2026-10-18 §1, re-assessed).

The user already said exactly what to do (times, ranges, a box), so a model turn adds 5–15 s and a failure mode
(guessing an ``s3://`` URI from a presigned URL) and nothing else. The proxy's ``POST /media/actions`` resolves
the source to a project asset of the chat's project, presigns it, and invokes the MAIN runtime with
``{"__media_action": {...}}`` under the chat's runtime session (so it lands in the warm microVM). This module
then runs ffmpeg deterministically in the code-interpreter SANDBOX — the same substrate ``tools/crop_video``
uses: isolated from the agent process, holding per-(project, session) scoped S3 credentials, and with the
bundled crop script as the single definition of the crop math.

What makes it fast, beyond skipping the model:
  * ONE warm sandbox per chat session, reused across actions (``_SANDBOXES``), recreated when it died;
  * ffmpeg provisioned once per sandbox — the bundled script's ``_ensure_ffmpeg_ffprobe`` finds the binary it
    already unpacked under the sandbox temp dir before downloading anything;
  * the source downloaded once per sandbox (cached by its ``s3://`` identity), so a Frame then a Clip of the same
    video reads it from local disk.

This path NEVER builds a Strands Agent or calls a model (a test pins it). Outputs go to the session OUTPUT prefix;
the proxy then sends the GeneratedMedia turn message that registers them as project assets with ``derivedFrom``
= the source asset.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

RESULT_TYPE = "media_action_result"
OPS = ("frame", "clip", "crop")
# Longer than this goes back to the agent / an M2C job (the UI falls back to the chat path).
MAX_RANGE_S = 120.0
MAX_POINTS = 20
MAX_RANGES = 10
# A warm sandbox lives this long; the per-call timeout below bounds one action.
SANDBOX_SESSION_TIMEOUT_S = 1800
ACTION_TIMEOUT_S = 240.0
# Scoped credentials are served with >= 45 min left; re-inject well before that runs out.
CREDENTIAL_REFRESH_S = 30 * 60

_SENTINEL = "MEDIA_ACTION_JSON:"


class MediaActionError(ValueError):
    """A request this path refuses (the UI falls back to the chat path)."""


# --------------------------------------------------------------------------------------------- request parsing

@dataclass
class MediaAction:
    action_id: str
    op: str
    source_url: str          # presigned GET the sandbox downloads
    source_uri: str          # canonical s3://bucket/key (cache identity + lineage)
    source_name: str
    asset_id: str = ""
    points_ms: list[int] = field(default_factory=list)
    ranges_ms: list[tuple[int, int]] = field(default_factory=list)
    box: Optional[tuple[float, float, float, float]] = None
    # Clip only: opt in to the fast keyframe-granular STREAM COPY instead of the frame-accurate
    # re-encode default. Not frame-accurate — it cuts at the keyframe before `in`.
    fast: bool = False


_ACTION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


def parse_action(raw: Any) -> MediaAction:
    """Validate the proxy's ``__media_action`` object. Raises :class:`MediaActionError` with a reason."""
    if not isinstance(raw, dict):
        raise MediaActionError("__media_action must be an object")
    op = raw.get("op")
    if op not in OPS:
        raise MediaActionError(f"op must be one of {', '.join(OPS)}")
    action_id = str(raw.get("action_id") or "")
    if not _ACTION_ID_RE.match(action_id):
        raise MediaActionError("action_id is required")
    src = raw.get("source") if isinstance(raw.get("source"), dict) else {}
    url, uri = str(src.get("url") or ""), str(src.get("s3uri") or "")
    if not url.startswith("https://") or not uri.startswith("s3://"):
        raise MediaActionError("source.url (https) and source.s3uri are required")
    act = MediaAction(action_id=action_id, op=op, source_url=url, source_uri=uri,
                      source_name=str(src.get("name") or uri.rsplit("/", 1)[-1]),
                      asset_id=str(src.get("asset_id") or ""))
    if op == "frame":
        pts = raw.get("points")
        if not isinstance(pts, list) or not pts:
            raise MediaActionError("frame needs points (ms)")
        if len(pts) > MAX_POINTS:
            raise MediaActionError(f"at most {MAX_POINTS} frames per action")
        act.points_ms = [_ms(p, "points") for p in pts]
    else:
        rng = raw.get("ranges") or []
        if not isinstance(rng, list) or len(rng) > MAX_RANGES:
            raise MediaActionError(f"at most {MAX_RANGES} ranges per action")
        for r in rng:
            if not isinstance(r, dict):
                raise MediaActionError("each range is {inMs, outMs}")
            a, b = _ms(r.get("inMs"), "inMs"), _ms(r.get("outMs"), "outMs")
            if b <= a:
                raise MediaActionError("a range must end after it starts")
            # A clip now RE-ENCODES by default (frame-accurate), but we keep no length cap: a long
            # clip can opt into the fast stream copy. Only a CROP, which always re-encodes a full
            # pass with a filter, is capped here.
            if op == "crop" and (b - a) / 1000.0 > MAX_RANGE_S:
                raise MediaActionError(f"crops longer than {int(MAX_RANGE_S)} s go through the agent")
            act.ranges_ms.append((a, b))
        if op == "clip" and not act.ranges_ms:
            raise MediaActionError("clip needs at least one range")
        if op == "clip":
            # Opt-in fast path: keyframe-granular stream copy, explicitly NOT frame-accurate.
            act.fast = bool(raw.get("fast"))
        if op == "crop":
            box = raw.get("box")
            if not isinstance(box, dict):
                raise MediaActionError("crop needs box {x,y,w,h} as 0..1 fractions")
            try:
                vals = tuple(float(box[k]) for k in ("x", "y", "w", "h"))
            except (KeyError, TypeError, ValueError) as exc:
                raise MediaActionError("crop box needs numeric x, y, w, h") from exc
            if not all(0.0 <= v <= 1.0 for v in vals) or vals[2] <= 0 or vals[3] <= 0:
                raise MediaActionError("crop box values are fractions of the frame (0..1), w and h > 0")
            act.box = vals  # type: ignore[assignment]
            if len(act.ranges_ms) > 1:
                raise MediaActionError("crop takes at most one range")
    return act


def _ms(v: Any, name: str) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
        raise MediaActionError(f"{name} must be a non-negative number of milliseconds")
    return int(round(v))


# --------------------------------------------------------------------------------------------- output naming

def _stem(name: str) -> str:
    base = name.rsplit("/", 1)[-1]
    base = base.rsplit(".", 1)[0] if "." in base else base
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._-")
    return (safe or "media")[:60]


def _tc(ms: int) -> str:
    s = ms // 1000
    return f"{s // 60:02d}{s % 60:02d}"


def plan_outputs(act: MediaAction, prefix: str) -> list[dict]:
    """The deterministic jobs the sandbox runs, one per output, with their keys under ``prefix``."""
    stem, tag = _stem(act.source_name), act.action_id[-6:]
    jobs: list[dict] = []
    if act.op == "frame":
        for i, p in enumerate(act.points_ms):
            jobs.append({"kind": "image", "at_ms": p,
                         "key": f"{prefix}{stem}_frame_{_tc(p)}_{p % 1000:03d}_{tag}{i}.jpg"})
    elif act.op == "clip":
        for i, (a, b) in enumerate(act.ranges_ms):
            jobs.append({"kind": "video", "in_ms": a, "out_ms": b, "fast": bool(act.fast),
                         "key": f"{prefix}{stem}_clip_{_tc(a)}-{_tc(b)}_{tag}{i}.mp4"})
    else:
        trim = act.ranges_ms[0] if act.ranges_ms else None
        jobs.append({"kind": "video", "box": list(act.box or ()),
                     "in_ms": trim[0] if trim else None, "out_ms": trim[1] if trim else None,
                     "key": f"{prefix}{stem}_crop_{tag}.mp4"})
    return jobs


# --------------------------------------------------------------------------------------------- frame-grid snapping

# PURE, importable helpers (unit-tested in test_media_actions.py) that the sandbox program ALSO uses —
# their source is injected verbatim by build_sandbox_code so there is a single definition. They take
# the UI's integer MILLISECONDS and a probed fps, and return exact ffmpeg seek times on the frame grid.


def _fmt_seconds(t: float) -> str:
    """Format a seconds value for an ffmpeg ``-ss``/``-t`` argument (sub-millisecond precision)."""
    return f"{float(t):.6f}"


def snap_frame(at_ms: int, fps: Optional[float]) -> tuple:
    """Snap an instant (ms) to the nearest frame on the fps grid.

    Returns ``(at_t, at_frame)`` where ``at_t`` is that frame's exact presentation time in seconds.
    With ``fps`` None/<=0 (unknown) returns the unsnapped time and ``at_frame`` None."""
    at_s = at_ms / 1000.0
    if not fps or fps <= 0:
        return at_s, None
    at_frame = int(round(at_s * fps))
    return at_frame / fps, at_frame


def snap_clip(in_ms: int, out_ms: int, fps: Optional[float]) -> tuple:
    """Snap a clip's in/out (ms) to the fps frame grid.

    Returns ``(in_t, dur, in_frame, out_frame)``. The clip covers frames ``[in_frame, out_frame)``,
    so its duration is ``(out_frame - in_frame) / fps`` and ``in_t`` is frame ``in_frame``'s exact
    time. ``out_frame`` is guarded to be at least ``in_frame + 1`` (one whole frame). With ``fps``
    None/<=0 (unknown) returns the unsnapped in/out and both frame indices None."""
    in_s = in_ms / 1000.0
    out_s = out_ms / 1000.0
    if not fps or fps <= 0:
        return in_s, max(0.0, out_s - in_s), None, None
    in_frame = int(round(in_s * fps))
    out_frame = int(round(out_s * fps))
    if out_frame <= in_frame:
        out_frame = in_frame + 1
    return in_frame / fps, (out_frame - in_frame) / fps, in_frame, out_frame


def frame_cmd(ffmpeg: str, src: str, dst: str, at_t: float) -> list:
    """ffmpeg argv to extract exactly one frame at ``at_t`` (accurate input seek + ``-frames:v 1``)."""
    return [ffmpeg, "-y", "-ss", _fmt_seconds(at_t), "-i", src, "-frames:v", "1", "-q:v", "2", dst]


def clip_cmd(ffmpeg: str, src: str, dst: str, in_t: float, dur: float, has_audio: bool,
             fast: bool = False) -> list:
    """ffmpeg argv for a clip.

    DEFAULT: a frame-accurate accurate-seek RE-ENCODE — ``-ss`` before ``-i`` then ``-t``, with the
    crop path's x264 settings (libx264 / superfast / crf 20 / yuv420p), AAC audio when present else
    ``-an``, ``+faststart``. Modern ffmpeg input-seek + re-encode is frame-exact.

    ``fast=True``: the keyframe-granular STREAM COPY (``-c copy``). It is NOT frame-accurate — it can
    only cut at the keyframe before ``in_t`` and relies on the MP4 edit list to mask the pre-roll."""
    ss, t = _fmt_seconds(in_t), _fmt_seconds(dur)
    if fast:
        return [ffmpeg, "-y", "-ss", ss, "-i", src, "-t", t,
                "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-movflags", "+faststart", dst]
    cmd = [ffmpeg, "-y", "-ss", ss, "-i", src, "-t", t,
           "-c:v", "libx264", "-preset", "superfast", "-crf", "20", "-pix_fmt", "yuv420p", "-threads", "0"]
    cmd += ["-c:a", "aac"] if has_audio else ["-an"]
    cmd += ["-movflags", "+faststart", dst]
    return cmd


# The helpers injected into the sandbox program (single definition, shared with the unit tests).
_SANDBOX_HELPERS = (_fmt_seconds, snap_frame, snap_clip, frame_cmd, clip_cmd)


# --------------------------------------------------------------------------------------------- sandbox program

# Appended after the bundled crop script (its helpers: _ensure_ffmpeg_ffprobe, probe_dimensions, probe_has_audio,
# probe_fps, resolve_crop) and the snapping/command helpers injected from this module. Every parameter arrives
# as a JSON literal. The source is cached per sandbox by its s3 identity.
_RUNNER = r'''
import os, json, hashlib, subprocess, tempfile, urllib.request

# The staged ffmpeg is read with the sandbox's IAM role (tools/ffmpeg/* grant), never via a public link.
if _BUCKET and not os.environ.get("FFMPEG_S3_URI") and not os.environ.get("FFMPEG_STATIC_URL"):
    os.environ["FFMPEG_S3_URI"] = "s3://" + _BUCKET + "/tools/ffmpeg"
if _REGION:
    os.environ.setdefault("AWS_REGION", _REGION)

def _cached_source():
    root = os.path.join(tempfile.gettempdir(), "media-src")
    os.makedirs(root, exist_ok=True)
    ext = os.path.splitext(_SRC_URI)[1][:6] or ".mp4"
    path = os.path.join(root, hashlib.sha256(_SRC_URI.encode()).hexdigest()[:32] + ext)
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        tmp = path + ".part"
        urllib.request.urlretrieve(_SRC_URL, tmp)  # public egress; ffmpeg then reads LOCAL
        os.replace(tmp, path)
        return path, False
    return path, True

def _copyable(ffprobe, path):
    """Whether a clip CAN be a stream copy into MP4: H.264/HEVC video, and AAC/MP3 audio or none."""
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "stream=codec_type,codec_name", "-of", "json", path],
                           capture_output=True, text=True, timeout=30)
        streams = json.loads(r.stdout or "{}").get("streams", [])
    except Exception:  # noqa: BLE001 - unknown: re-encode, the safe path
        return False
    v = [x.get("codec_name") for x in streams if x.get("codec_type") == "video"]
    a = [x.get("codec_name") for x in streams if x.get("codec_type") == "audio"]
    return bool(v) and v[0] in ("h264", "hevc") and (not a or a[0] in ("aac", "mp3"))

_out = {"error": "media action did not run"}
try:
    import boto3
    _s3 = boto3.client("s3", region_name=_REGION)  # per cell: credentials can be re-injected between cells
    _ffmpeg, _ffprobe = _ensure_ffmpeg_ffprobe()
    _local, _hit = _cached_source()
    _fps = probe_fps(_ffprobe, _local)          # frames/sec on the real frame grid, or None (unknown)
    _audio = probe_has_audio(_ffprobe, _local)
    _outs = []
    with tempfile.TemporaryDirectory() as _td:
        for _i, _j in enumerate(_JOBS):
            _dst = os.path.join(_td, "out%d%s" % (_i, ".jpg" if _j["kind"] == "image" else ".mp4"))
            if _j["kind"] == "image":
                # Snap the requested instant to the nearest real frame and seek there exactly.
                _at_t, _at_frame = snap_frame(_j["at_ms"], _fps)
                _cmd = frame_cmd(_ffmpeg, _local, _dst, _at_t)
                _ct = "image/jpeg"
            elif not _j.get("box"):
                # A plain clip: snap in/out to the frame grid and RE-ENCODE for frame accuracy by
                # default. The opt-in `fast` flag restores the keyframe-granular stream copy, used
                # only when the source codecs are actually copyable into MP4.
                _in_t, _dur, _inf, _outf = snap_clip(_j["in_ms"], _j["out_ms"], _fps)
                _fast = bool(_j.get("fast")) and _copyable(_ffprobe, _local)
                _cmd = clip_cmd(_ffmpeg, _local, _dst, _in_t, _dur, _audio, _fast)
                _ct = "video/mp4"
            else:
                # Crop: always re-encode (filtered pass), with the snapped trim when a range is given.
                _cmd = [_ffmpeg, "-y"]
                _in_ms, _out_ms = _j.get("in_ms"), _j.get("out_ms")
                if _in_ms is not None and _out_ms is not None:
                    _in_t, _dur, _inf, _outf = snap_clip(_in_ms, _out_ms, _fps)
                    _cmd += ["-ss", _fmt_seconds(_in_t), "-i", _local, "-t", _fmt_seconds(_dur)]
                elif _in_ms is not None:
                    _in_t, _in_frame = snap_frame(_in_ms, _fps)
                    _cmd += ["-ss", _fmt_seconds(_in_t), "-i", _local]
                else:
                    _cmd += ["-i", _local]
                _w, _h = probe_dimensions(_ffprobe, _local)
                _c = resolve_crop(tuple(_j["box"]), "normalized", _w, _h)
                _cmd += ["-vf", "crop=%d:%d:%d:%d" % (_c["w"], _c["h"], _c["x"], _c["y"])]
                # superfast keeps a 10 s crop to a few seconds.
                _cmd += ["-c:v", "libx264", "-preset", "superfast", "-crf", "20", "-pix_fmt", "yuv420p", "-threads", "0"]
                _cmd += ["-c:a", "aac"] if _audio else ["-an"]
                _cmd += ["-movflags", "+faststart", _dst]
                _ct = "video/mp4"
            _r = subprocess.run(_cmd, capture_output=True, text=True, timeout=_TIMEOUT)
            if _r.returncode != 0 or not os.path.isfile(_dst):
                raise RuntimeError("ffmpeg failed: " + (_r.stderr or "")[-600:].strip())
            with open(_dst, "rb") as _fh:
                _s3.put_object(Bucket=_BUCKET, Key=_j["key"], Body=_fh.read(), ContentType=_ct)
            _outs.append({"key": _j["key"], "kind": _j["kind"]})
    _out = {"ok": True, "outputs": _outs, "sourceCached": _hit}
except Exception as _e:  # noqa: BLE001 - the real cause goes back to the caller
    _out = {"error": str(_e)[:800] or repr(_e)}

print(_SENTINEL_TOKEN + json.dumps(_out))
'''


def build_sandbox_code(script_src: str, act: MediaAction, jobs: list[dict], *, bucket: str, domain: str,
                       region: str, timeout: float = ACTION_TIMEOUT_S) -> str:
    """The bundled crop script (as a library) + the snapping helpers + the injected params + the runner."""
    from tools.crop_video import _as_library

    params = {
        "_SRC_URL": act.source_url, "_SRC_URI": act.source_uri, "_JOBS": jobs, "_BUCKET": bucket,
        "_DOMAIN": domain, "_REGION": region, "_TIMEOUT": float(timeout), "_SENTINEL_TOKEN": _SENTINEL,
    }
    # Each value as json.loads of a JSON string literal: JSON is NOT Python (`null` / `true` raised NameError in the
    # sandbox for a crop with no time range, whose jobs carry "in_ms": null), and a quoted JSON string always is.
    header = "import json as _json\n" + "\n".join(f"{k} = _json.loads({json.dumps(json.dumps(v))})" for k, v in params.items())
    # The pure snapping/command helpers, injected verbatim so the sandbox and the unit tests share one definition.
    helpers = "\n\n".join(inspect.getsource(fn) for fn in _SANDBOX_HELPERS)
    return (f"{_as_library(script_src)}\n\n# --- snapping + command helpers ---\n{helpers}\n"
            f"\n# --- injected params + runner ---\n{header}\n{_RUNNER}")


# --------------------------------------------------------------------------------------------- warm sandboxes

@dataclass
class _Sandbox:
    ci: Any
    started_at: float
    creds_at: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)


_SANDBOXES: dict[str, _Sandbox] = {}
_REGISTRY_LOCK = threading.Lock()


def _new_code_interpreter():  # seam for tests
    from bedrock_agentcore.tools.code_interpreter_client import CodeInterpreter

    ci = CodeInterpreter(os.environ.get("AWS_REGION", "us-west-2"))
    ci.start(identifier=os.environ.get("CODE_INTERPRETER_ID") or "aws.codeinterpreter.v1",
             session_timeout_seconds=SANDBOX_SESSION_TIMEOUT_S)
    return ci


def _inject_credentials(ci, project_id: Optional[str], session_id: str) -> None:
    """Scoped per-(project, session) S3 credentials into the sandbox, out of band (Phase 3 (b)).

    FAILS CLOSED: raises ``SandboxCredentialsError`` where the scoped role is configured and they could
    not be installed, so the action never runs on the sandbox's own role (no artifact S3 since B2)."""
    import sandbox_credentials as sc

    if not sc.required():
        return
    sc.install(ci.invoke, project_id, session_id)


def _sandbox_for(session_id: str) -> _Sandbox:
    with _REGISTRY_LOCK:
        box = _SANDBOXES.get(session_id)
        if box is not None and time.time() - box.started_at < SANDBOX_SESSION_TIMEOUT_S - ACTION_TIMEOUT_S - 30:
            return box
        if box is not None:
            _stop_quietly(box.ci)
        box = _Sandbox(ci=_new_code_interpreter(), started_at=time.time())
        _SANDBOXES[session_id] = box
        return box


def _discard(session_id: str, box: _Sandbox) -> None:
    with _REGISTRY_LOCK:
        if _SANDBOXES.get(session_id) is box:
            del _SANDBOXES[session_id]
    _stop_quietly(box.ci)


def _stop_quietly(ci) -> None:
    try:
        ci.stop()
    except Exception:  # noqa: BLE001 - best effort
        pass


def run_in_warm_sandbox(session_id: str, project_id: Optional[str], code: str) -> str:
    """Run ``code`` in this chat's warm sandbox (one retry on a fresh one if the warm one died)."""
    from tools.crop_video import _extract_stdout

    for attempt in (0, 1):
        box = _sandbox_for(session_id)
        try:
            with box.lock:
                if time.time() - box.creds_at > CREDENTIAL_REFRESH_S:
                    _inject_credentials(box.ci, project_id, session_id)
                    box.creds_at = time.time()
                return _extract_stdout(box.ci.execute_code(code))
        except Exception:  # noqa: BLE001 - an expired / dead sandbox: replace it once
            _discard(session_id, box)
            if attempt:
                raise
    raise RuntimeError("unreachable")


# --------------------------------------------------------------------------------------------- the action

def _output_prefix(session_id: str, project_id: Optional[str]) -> str:
    from projects.storage_layout import session_output_prefix

    safe = session_id.replace("/", "_").strip()
    return session_output_prefix(safe, project_id, artifacts_prefix=os.environ.get("ARTIFACTS_PREFIX", "artifacts/"))


def _script_source() -> str:
    from tools.crop_video import _script_source as src

    return src()


def run_action(act: MediaAction, *, session_id: str, project_id: Optional[str]) -> dict:
    """Run one action and return the ``media_action_result`` frame (never raises)."""
    started = time.monotonic()
    bucket = os.environ.get("SKILLS_S3_BUCKET", "")
    domain = os.environ.get("ARTIFACTS_DOMAIN", "")
    region = os.environ.get("AWS_REGION", "us-west-2")
    try:
        if not bucket:
            raise MediaActionError("SKILLS_S3_BUCKET is not configured")
        jobs = plan_outputs(act, _output_prefix(session_id, project_id))
        code = build_sandbox_code(_script_source(), act, jobs, bucket=bucket, domain=domain, region=region)
        raw = run_in_warm_sandbox(session_id, project_id, code)
        line = next((ln for ln in raw.splitlines() if ln.startswith(_SENTINEL)), None)
        if line is None:
            raise RuntimeError(f"the sandbox produced no result: {raw[-300:].strip()}")
        res = json.loads(line[len(_SENTINEL):])
        if not res.get("ok"):
            raise RuntimeError(res.get("error") or "the sandbox reported a failure")
        outputs = []
        planned = {j["key"]: j for j in jobs}
        for o in res.get("outputs") or []:
            key = o["key"]
            item = {"kind": o["kind"], "name": key.rsplit("/", 1)[-1], "key": key,
                    "s3uri": f"s3://{bucket}/{key}",
                    "url": f"https://{domain}/{key}" if domain else key}
            # The SOURCE time range the output was cut from (lineage edges): a frame's instant, a clip's in/out.
            job = planned.get(key) or {}
            if job.get("at_ms") is not None:
                at_s = round(job["at_ms"] / 1000.0, 3)
                item["range"] = {"start": at_s, "end": at_s}
            elif job.get("in_ms") is not None and job.get("out_ms") is not None:
                item["range"] = {"start": round(job["in_ms"] / 1000.0, 3),
                                 "end": round(job["out_ms"] / 1000.0, 3)}
            outputs.append(item)
        return {"type": RESULT_TYPE, "ok": True, "action_id": act.action_id, "op": act.op, "outputs": outputs,
                "source": {"s3uri": act.source_uri, "asset_id": act.asset_id, "name": act.source_name},
                "sourceCached": bool(res.get("sourceCached")),
                "elapsedMs": int((time.monotonic() - started) * 1000)}
    except Exception as exc:  # noqa: BLE001 - one JSON frame either way
        logger.warning("media action %s (%s) failed: %s", act.action_id, act.op, exc)
        return {"type": RESULT_TYPE, "ok": False, "action_id": act.action_id, "op": act.op,
                "error": str(exc)[:500] or type(exc).__name__,
                "elapsedMs": int((time.monotonic() - started) * 1000)}


async def handle(payload: dict):
    """The ``__media_action`` entrypoint branch: one JSON frame, no model, no Agent."""
    import asyncio

    from jobs import context as jobs_context
    from projects.ids import is_project_id

    try:
        act = parse_action(payload.get("__media_action"))
    except MediaActionError as exc:
        yield json.dumps({"type": RESULT_TYPE, "ok": False, "error": str(exc)})
        return
    session_id = str(payload.get("session_id") or "")
    pid = payload.get("active_project_id")
    project_id = pid if isinstance(pid, str) and is_project_id(pid) else None
    if not session_id or project_id is None:
        yield json.dumps({"type": RESULT_TYPE, "ok": False, "action_id": act.action_id,
                          "error": "a session with an active project is required"})
        return
    # The proxy verified the actor's access to this project (it resolved the source as one of its assets).
    tokens = jobs_context.set_request_context(actor_id=str(payload.get("__actor_id") or ""), session_id=session_id)
    ptoken = jobs_context.set_producing_project_id(project_id)
    try:
        frame = await asyncio.to_thread(run_action, act, session_id=session_id, project_id=project_id)
    finally:
        jobs_context.reset_producing_project_id(ptoken)
        jobs_context.reset_request_context(tokens)
    yield json.dumps(frame)


def _reset_for_tests() -> None:
    with _REGISTRY_LOCK:
        _SANDBOXES.clear()


# Exposed for the warm-cache digest tests.
def source_cache_name(uri: str) -> str:
    return hashlib.sha256(uri.encode()).hexdigest()[:32]
