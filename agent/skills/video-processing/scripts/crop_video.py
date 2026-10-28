"""Crop a video to an arbitrary bounding box with ffmpeg, in the code interpreter.

Why this exists: ``media-processing___process_media`` can only express a
top-left-anchored crop (``crop_x``/``crop_y`` + output scale), NOT an arbitrary
``(x, y, w, h)`` box. An arbitrary bbox is the ffmpeg escape-hatch class (like
xfade/overlay), so this script applies the ffmpeg ``crop`` filter with a web-safe
re-encode and returns a new clip (the source is never modified).

The bounding box may be given NORMALIZED (fractions of the frame, 0..1) — what the
render UI's crop selector emits, and what a face/person detection sidecar stores —
or in absolute PIXELS. Either way this is the single conversion funnel: probe the
real source dimensions, convert to integer pixels, snap width/height to EVEN values
(yuv420p will not encode odd dimensions), and clamp within the frame.

Self-provisioning: the AgentCore code interpreter has no system ffmpeg/ffprobe
(``imageio-ffmpeg`` ships ffmpeg only). This mirrors ``agent-olive/scripts/
assemble_reel.py``: the harness deploy stages its OWN static LGPL ffmpeg + ffprobe build
(FFmpeg + OpenH264 from pinned sources, ``infra/lib/distribution/ffmpeg/Dockerfile``) at
``s3://<skills-bucket>/tools/ffmpeg/``. ``FFMPEG_S3_URI`` (``s3://<skills-bucket>/tools/ffmpeg``)
fetches it with the sandbox's IAM role; ``FFMPEG_STATIC_URL`` takes a presigned URL instead. No
public link serves it, and there is no external download source.

Encoding: that build's H.264 encoder is ``libopenh264`` (bitrate-driven; it has no CRF or
presets). A PATH ffmpeg that already has ``libx264`` is used as-is.

Usage:
    crop_video.py <source_url_or_path> <out.mp4> \\
        (--box x,y,w,h | --box-px x,y,w,h) [--trim in,out] [--execute]

Without ``--execute`` (or with ``--dry-run``) it prints the resolved pixel box and
the exact ffmpeg command as JSON and does nothing else.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

MIN_CROP_PX = 16  # a crop smaller than this on either axis is almost certainly a mis-drag

# H.264 encoders, in preference order. The harness-built ffmpeg ships libopenh264 only; libx264 is
# used when a binary already on the sandbox's PATH happens to have it (nothing here downloads one).
H264_ENCODERS = ("libx264", "libopenh264")
# libopenh264 is rate-controlled by bitrate, not CRF: use the source's bitrate, clamped.
OPENH264_DEFAULT_KBPS = 5000
OPENH264_MIN_KBPS = 1000
OPENH264_MAX_KBPS = 20000
STAGED_FFMPEG_HINT = (
    "the harness deploy stages its ffmpeg build at s3://<skills-bucket>/tools/ffmpeg/; pass "
    "FFMPEG_S3_URI=s3://<skills-bucket>/tools/ffmpeg (the sandbox role reads it) or FFMPEG_STATIC_URL="
    "<presigned URL>, and redeploy the harness if it is missing"
)


def _ffmpeg_has_encoder(ffmpeg_path: str, encoder: str = "libopenh264") -> bool:
    """True if ``ffmpeg_path`` was built WITH ``encoder``. A PATH ffmpeg can have the binary but
    no H.264 encoder (e.g. imageio-ffmpeg's stripped build), which fails encoding with
    ``Unknown encoder`` (session 00ef689c). Best-effort: any probe failure returns False so the
    caller provisions the harness build."""
    try:
        result = subprocess.run([ffmpeg_path, "-hide_banner", "-encoders"],
                                capture_output=True, text=True, timeout=30)
        return f" {encoder} " in f" {(result.stdout or '')} ".replace("\n", " ")
    except (OSError, subprocess.SubprocessError):
        return False


def h264_encoder(ffmpeg_path: str) -> Optional[str]:
    """The first available encoder of ``H264_ENCODERS`` in ``ffmpeg_path``, else None."""
    for encoder in H264_ENCODERS:
        if _ffmpeg_has_encoder(ffmpeg_path, encoder):
            return encoder
    return None


def video_codec_args(encoder: Optional[str], bitrate_bps: Optional[int] = None) -> list[str]:
    """The ``-c:v`` arguments for ``encoder``. Pure. libopenh264 has no CRF, so it is driven by the
    source's video bitrate, clamped to a sane range (a default when the bitrate is unknown)."""
    if encoder == "libx264":
        return ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if encoder == "libopenh264":
        kbps = OPENH264_DEFAULT_KBPS if not bitrate_bps else int(round(bitrate_bps / 1000))
        kbps = max(OPENH264_MIN_KBPS, min(kbps, OPENH264_MAX_KBPS))
        return ["-c:v", "libopenh264", "-b:v", f"{kbps}k", "-pix_fmt", "yuv420p"]
    raise ValueError(f"ffmpeg has no H.264 encoder (need one of {', '.join(H264_ENCODERS)}); "
                     + STAGED_FFMPEG_HINT)


def _ensure_ffmpeg_ffprobe(ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> tuple[str, str]:
    """Resolve BOTH binaries, provisioning the harness's staged build if the sandbox lacks them.

    Identical strategy to ``agent-olive/scripts/assemble_reel.py`` (there is no shared
    helper module — each bundled script inlines this). The source is ``FFMPEG_S3_URI`` (the
    staged prefix or tarball, read with the sandbox's IAM role) or ``FFMPEG_STATIC_URL`` (a full,
    presigned tarball URL). There is deliberately no external fallback.
    """
    resolved_ff = shutil.which(ffmpeg)
    resolved_fp = shutil.which(ffprobe)
    # Require an H.264 ENCODER, not merely the binary: a PATH ffmpeg without one fails with
    # "Unknown encoder". Fall through to the staged build.
    if resolved_ff and resolved_fp and h264_encoder(resolved_ff):
        return resolved_ff, resolved_fp

    import platform
    import tarfile
    import tempfile
    import urllib.request

    machine = platform.machine().lower()
    arch = "arm64" if machine in ("aarch64", "arm64") else "amd64"
    tarball = f"ffmpeg-release-{arch}-static.tar.xz"
    s3_uri = os.environ.get("FFMPEG_S3_URI", "").strip()
    url = os.environ.get("FFMPEG_STATIC_URL", "").strip()
    bucket = key = ""
    if s3_uri:
        if not s3_uri.startswith("s3://"):
            raise ValueError(f"FFMPEG_S3_URI must be an s3:// URI, got {s3_uri!r}")
        bucket, _, key = s3_uri[len("s3://"):].partition("/")
        key = key.strip("/")
        if not key.endswith(".tar.xz"):
            key = f"{key or 'tools/ffmpeg'}/{tarball}"
        url = f"s3://{bucket}/{key}"
    if not url:
        raise ValueError("no ffmpeg source configured (FFMPEG_S3_URI / FFMPEG_STATIC_URL); "
                         + STAGED_FFMPEG_HINT)
    root = Path(tempfile.gettempdir()) / "ffmpeg-static"
    root.mkdir(parents=True, exist_ok=True)

    def _find(name: str) -> Optional[str]:
        for candidate in root.rglob(name):
            if candidate.is_file():
                return str(candidate)
        return None

    ff_path = _find("ffmpeg")
    fp_path = _find("ffprobe")
    if not (ff_path and fp_path):
        archive = root / "ffmpeg-static.tar.xz"
        try:
            if bucket:
                # The sandbox's own IAM role reads the staged build: no public or signed link at all.
                import boto3
                boto3.client("s3", region_name=os.environ.get("AWS_REGION") or None).download_file(
                    bucket, key, str(archive))
            else:
                urllib.request.urlretrieve(url, archive)  # noqa: S310 — a presigned URL to our own build
        except Exception as exc:  # noqa: BLE001 - boto3 / urllib errors alike → one actionable message
            raise ValueError(f"could not download ffmpeg from {url} ({exc}); " + STAGED_FFMPEG_HINT) from exc
        with tarfile.open(archive) as tar:
            try:
                tar.extractall(root, filter="data")  # Python 3.12+ safe extraction
            except TypeError:
                tar.extractall(root)
        ff_path = _find("ffmpeg")
        fp_path = _find("ffprobe")

    if not (ff_path and fp_path):
        raise ValueError(f"the ffmpeg archive at {url} has no ffmpeg+ffprobe; " + STAGED_FFMPEG_HINT)
    for binary in (ff_path, fp_path):
        os.chmod(binary, 0o755)
    if not h264_encoder(ff_path):
        raise ValueError(f"provisioned ffmpeg at {ff_path} has no H.264 encoder "
                         f"(need one of {', '.join(H264_ENCODERS)}); " + STAGED_FFMPEG_HINT)
    os.environ["PATH"] = str(Path(ff_path).parent) + os.pathsep + os.environ.get("PATH", "")
    return ff_path, fp_path


# ----------------------------------------------------------------------------
# Pure helpers (no ffmpeg / no network) — unit-tested directly.
# ----------------------------------------------------------------------------

def _even_floor(value: float) -> int:
    """Round to the nearest integer, then floor to an EVEN number (yuv420p needs even dims)."""
    n = int(round(value))
    return n - (n % 2)


def parse_box(spec: str) -> tuple[float, float, float, float]:
    """Parse ``"x,y,w,h"`` into four floats. Raises ValueError on a malformed spec."""
    parts = [p.strip() for p in str(spec).split(",")]
    if len(parts) != 4:
        raise ValueError(f"box must be 'x,y,w,h' (four values), got {spec!r}")
    try:
        x, y, w, h = (float(p) for p in parts)
    except ValueError as exc:
        raise ValueError(f"box values must be numbers, got {spec!r}") from exc
    if w <= 0 or h <= 0:
        raise ValueError(f"box width/height must be positive, got w={w} h={h}")
    return x, y, w, h


def parse_aspect(spec: str) -> tuple[float, float]:
    """``"9:16"`` / ``"1:1"`` / ``"4/5"`` -> ``(9.0, 16.0)``. Raises ValueError for anything else."""
    parts = [p.strip() for p in str(spec or "").replace("/", ":").split(":")]
    try:
        aw, ah = (float(parts[0]), float(parts[1])) if len(parts) == 2 else (0.0, 0.0)
    except ValueError:
        aw = ah = 0.0
    if not (aw > 0 and ah > 0):
        raise ValueError(f"aspect must look like '9:16' or '1:1', got {spec!r}")
    return aw, ah


def aspect_box(spec: str, width: int, height: int) -> tuple[float, float, float, float]:
    """The largest CENTERED normalized box of aspect ``spec`` that fits a ``width`` x ``height`` frame.

    Computed from the PROBED size, so a caller never has to know it: a model asked for "1:1, centered"
    assumed 1920x1080, passed x=0.25,w=0.5, and on the real 1280x720 got a 640x720 non-square crop.
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"invalid source dimensions {width}x{height}")
    aw, ah = parse_aspect(spec)
    target = aw / ah
    if width / height > target:          # frame is wider: full height, narrower width
        w = (height * target) / width
        return ((1 - w) / 2, 0.0, w, 1.0)
    h = (width / target) / height        # frame is taller: full width, shorter height
    return (0.0, (1 - h) / 2, 1.0, h)


def resolve_crop(
    box: tuple[float, float, float, float],
    mode: str,
    width: int,
    height: int,
    min_px: int = MIN_CROP_PX,
) -> dict:
    """Convert a normalized (0..1) or pixel box to an integer, even, in-frame crop.

    Returns ``{"w","h","x","y"}`` (ffmpeg ``crop=w:h:x:y``). Raises ValueError when the
    resolved crop is smaller than ``min_px`` on either axis (a likely mis-drag).
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"invalid source dimensions {width}x{height}")
    x, y, w, h = box
    if mode == "normalized":
        # clamp the normalized inputs defensively, then scale to pixels
        x, y = max(0.0, min(x, 1.0)), max(0.0, min(y, 1.0))
        w, h = max(0.0, min(w, 1.0)), max(0.0, min(h, 1.0))
        px_x, px_y, px_w, px_h = x * width, y * height, w * width, h * height
    elif mode == "pixels":
        px_x, px_y, px_w, px_h = x, y, w, h
    else:
        raise ValueError(f"mode must be 'normalized' or 'pixels', got {mode!r}")

    cw = _even_floor(px_w)
    ch = _even_floor(px_h)
    frame_w_even = _even_floor(width)
    frame_h_even = _even_floor(height)
    cw = max(2, min(cw, frame_w_even))
    ch = max(2, min(ch, frame_h_even))
    cx = max(0, min(int(round(px_x)), width - cw))
    cy = max(0, min(int(round(px_y)), height - ch))
    if cw < min_px or ch < min_px:
        raise ValueError(
            f"resolved crop {cw}x{ch} is smaller than the {min_px}px minimum "
            "(likely a mis-drag); pick a larger region"
        )
    return {"w": cw, "h": ch, "x": cx, "y": cy}


def build_ffmpeg_cmd(
    ffmpeg: str,
    source: str,
    out: str,
    crop: dict,
    trim: Optional[tuple[float, float]] = None,
    has_audio: bool = True,
    video_args: Optional[list[str]] = None,
) -> list[str]:
    """Assemble the exact ffmpeg command. Web-safe re-encode; keeps audio when present.

    ``video_args`` comes from ``video_codec_args`` for the encoder the binary actually has; the
    default is the harness build's (libopenh264 at its default bitrate)."""
    cmd = [ffmpeg, "-y"]
    if trim is not None:
        cmd += ["-ss", str(trim[0]), "-to", str(trim[1])]
    cmd += ["-i", source]
    cmd += ["-vf", f"crop={crop['w']}:{crop['h']}:{crop['x']}:{crop['y']}"]
    cmd += list(video_args) if video_args is not None else video_codec_args("libopenh264")
    cmd += ["-c:a", "aac"] if has_audio else ["-an"]
    cmd += ["-movflags", "+faststart", out]
    return cmd


# ----------------------------------------------------------------------------
# ffprobe-backed helpers (subprocess) — patched out in tests.
# ----------------------------------------------------------------------------


# The sandbox's ffmpeg/ffprobe are a STATIC build, and a static glibc binary cannot resolve hostnames: given an
# https:// input they fail with "Failed to resolve hostname <host>: System error" for every host (S3, CloudFront),
# while Python in the same sandbox resolves them fine (verified in the harness code interpreter, 2026-10-20; prod
# session f0c50162's reel stitch failed exactly so). So every remote input is downloaded by Python FIRST and the
# binaries only ever see local paths.
_STALL_SECONDS = 60  # a download that receives NOTHING for this long has stalled (not a cap on its length)


def localize_input(source: str, dest_dir: str, index: int = 0) -> str:
    """A local path for ``source``: unchanged when local; ``s3://`` via boto3 (the sandbox role); http(s) via Python."""
    import shutil
    import tempfile
    import urllib.parse
    import urllib.request

    src = (source or "").strip()
    if not src.startswith(("http://", "https://", "s3://")):
        return src
    parsed = urllib.parse.urlparse(src)
    name = os.path.basename(urllib.parse.unquote(parsed.path)) or "input"
    os.makedirs(dest_dir or tempfile.gettempdir(), exist_ok=True)
    local = os.path.join(dest_dir or tempfile.gettempdir(), f"{index:03d}_{name}")
    if os.path.isfile(local) and os.path.getsize(local) > 0:
        return local
    try:
        if src.startswith("s3://"):
            import boto3  # noqa: PLC0415 — only a sandbox run reaches here
            boto3.client("s3", region_name=os.environ.get("AWS_REGION") or None).download_file(
                parsed.netloc, parsed.path.lstrip("/"), local)
        else:
            with urllib.request.urlopen(src, timeout=_STALL_SECONDS) as resp, open(local, "wb") as out:  # noqa: S310
                shutil.copyfileobj(resp, out, 1 << 20)
    except Exception as exc:  # noqa: BLE001 — one clear error for the caller's existing handling
        if os.path.exists(local):
            os.remove(local)
        raise OSError(f"could not download {name} ({type(exc).__name__}: {exc})") from exc
    return local


def probe_dimensions(ffprobe: str, source: str) -> tuple[int, int]:
    """Return (width, height) of the first video stream via ffprobe.

    On a non-zero exit, RAISE with ffprobe's own stderr — otherwise a remote-URL failure
    (TLS/CA, missing https protocol, redirect, network egress) surfaces only as an opaque
    "returned non-zero exit status 1" and the real cause is lost (sessions 54a9b4a7 / d1a7f019).
    """
    result = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", source],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise ValueError(
            f"ffprobe exit {result.returncode} reading source: "
            f"{(result.stderr or '').strip()[-400:] or '<no stderr>'}"
        )
    streams = json.loads(result.stdout).get("streams") or []
    if not streams:
        raise ValueError("no video stream found in source")
    s = streams[0]
    return int(s["width"]), int(s["height"])


def probe_video_bitrate(ffprobe: str, source: str) -> Optional[int]:
    """The first video stream's bitrate in bit/s (the container's when the stream has none), or
    None. Best-effort: it only tunes libopenh264's target, so any failure returns None."""
    for entries in (["-select_streams", "v:0", "-show_entries", "stream=bit_rate"],
                    ["-show_entries", "format=bit_rate"]):
        try:
            result = subprocess.run([ffprobe, "-v", "error", *entries, "-of", "csv=p=0", source],
                                    capture_output=True, text=True, timeout=60)
            value = (result.stdout or "").strip().splitlines()[0].strip() if result.stdout else ""
            if value.isdigit() and int(value) > 0:
                return int(value)
        except (OSError, subprocess.SubprocessError, IndexError):
            pass
    return None


def _parse_rational_fps(value: Optional[str]) -> Optional[float]:
    """Parse an ffprobe rational frame rate (``"num/den"``, e.g. ``"30000/1001"``, or a bare number)
    to a positive float, else None. ``"0/0"`` or a zero denominator means 'unknown'."""
    if not value:
        return None
    text = str(value).strip()
    try:
        if "/" in text:
            num, den = text.split("/", 1)
            den_f = float(den)
            if den_f == 0:
                return None
            fps = float(num) / den_f
        else:
            fps = float(text)
    except (TypeError, ValueError):
        return None
    return fps if fps > 0 else None


def probe_fps(ffprobe: str, source: str) -> Optional[float]:
    """The first video stream's frame rate in frames/second, or None if it can't be determined.

    Parses ``avg_frame_rate`` (the true average over the stream, e.g. ``"30000/1001"`` -> 29.97),
    falling back to ``r_frame_rate`` (the base rate). Both are ffprobe rationals ``"num/den"``.
    Best-effort: any ffprobe/parse failure returns None so the caller falls back to unsnapped times
    rather than failing."""
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=avg_frame_rate,r_frame_rate", "-of", "json", source],
            capture_output=True, text=True, timeout=60,
        )
        streams = json.loads(result.stdout or "{}").get("streams") or []
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not streams:
        return None
    s = streams[0]
    for key in ("avg_frame_rate", "r_frame_rate"):
        fps = _parse_rational_fps(s.get(key))
        if fps is not None:
            return fps
    return None


def probe_has_audio(ffprobe: str, source: str) -> bool:
    """True if the source has at least one audio stream."""
    result = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index", "-of", "csv=p=0", source],
        capture_output=True, text=True, check=True,
    )
    return bool(result.stdout.strip())


def parse_trim(spec: Optional[str]) -> Optional[tuple[float, float]]:
    if not spec:
        return None
    parts = [p.strip() for p in spec.split(",")]
    if len(parts) != 2:
        raise ValueError(f"--trim must be 'in,out' (seconds), got {spec!r}")
    try:
        a, b = float(parts[0]), float(parts[1])
    except ValueError as exc:
        raise ValueError(f"--trim values must be numbers, got {spec!r}") from exc
    if b <= a or a < 0:
        raise ValueError(f"--trim must have 0 <= in < out, got {spec!r}")
    return a, b


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Crop a video to a bounding box (ffmpeg crop filter).")
    p.add_argument("source", help="source video URL or local path")
    p.add_argument("out", help="output .mp4 path")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--box", help="normalized box 'x,y,w,h' in 0..1 (UI / detection sidecar)")
    g.add_argument("--box-px", dest="box_px", help="pixel box 'x,y,w,h' in source pixels")
    g.add_argument("--aspect", help="a centered crop of this aspect, e.g. '9:16' or '1:1' (sized from the probe)")
    p.add_argument("--trim", help="optional temporal trim 'in,out' in seconds (fused into one pass)")
    p.add_argument("--execute", action="store_true", help="actually run ffmpeg (default: dry run)")
    p.add_argument("--dry-run", action="store_true", help="print the command contract and exit")
    p.add_argument("--min-px", type=int, default=MIN_CROP_PX)
    args = p.parse_args(argv)

    # Parse the box + trim (bad input -> exit 2).
    try:
        mode = "normalized" if args.box else ("aspect" if args.aspect else "pixels")
        box = parse_aspect(args.aspect) if args.aspect else parse_box(args.box if args.box else args.box_px)
        trim = parse_trim(args.trim)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}))
        return 2

    dry = args.dry_run or not args.execute

    # Provision + probe (failures -> exit 3). Dry run still probes so the reported crop is real.
    try:
        ffmpeg, ffprobe = _ensure_ffmpeg_ffprobe()
        # ffprobe/ffmpeg get the LOCAL copy (a static binary cannot resolve any hostname).
        source = localize_input(args.source, os.path.join(os.path.dirname(os.path.abspath(args.out)) or ".", "_inputs"))
        width, height = probe_dimensions(ffprobe, source)
        audio = probe_has_audio(ffprobe, source)
    except (OSError, ValueError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": f"probe failed: {exc}"}))
        return 3

    # Resolve the crop (bad/too-small box -> exit 2).
    try:
        if mode == "aspect":
            box, mode = aspect_box(args.aspect, width, height), "normalized"
        crop = resolve_crop(box, mode, width, height, min_px=args.min_px)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}))
        return 2

    try:
        video_args = video_codec_args(h264_encoder(ffmpeg), probe_video_bitrate(ffprobe, source))
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}))
        return 3
    cmd = build_ffmpeg_cmd(ffmpeg, source, args.out, crop, trim=trim, has_audio=audio,
                           video_args=video_args)

    if dry:
        print(json.dumps({
            "dryRun": True, "sourceDims": [width, height], "crop": crop,
            "trim": list(trim) if trim else None, "audio": audio, "command": cmd,
        }))
        return 0

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        print(json.dumps({"error": f"ffmpeg failed: {exc}", "stderr": stderr[-800:]}))
        return 3

    print(json.dumps({
        "cropped": True, "sourceDims": [width, height], "crop": crop,
        "trim": list(trim) if trim else None, "audio": audio, "out": args.out,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
