#!/usr/bin/env python3
"""Put a scene back together after some of its shots were replaced (recast, restyled, regenerated), frame-exact.

    python scripts/reassemble_shots.py spec.json out.mp4

    spec = {"source": "<the original clip: its audio, frame rate and size are the reel's>",
            "shots": [{"start": 0.0, "pieces": [{"src": "shot00_recast.mp4", "offset": 0.0}]},
                      {"start": 6.34},                       # no pieces: this shot stays as in the source
                      ...],
            "end": 168.268}                                  # optional; default the source's length

Every shot gets EXACTLY the source's frames from its start to the next shot's start (``round(t * fps)``), so the
reel has the source's frame count and the original dialogue stays in sync to the frame. A shot's pieces fill it in
order (a shot split in halves for a model's length limit lists both halves); the last piece is cut or held on its
last frame to land on the count, so a replacement that comes back shorter, longer, or at another frame rate (fal
returns 24 fps) never shifts what follows. Each piece is encoded once, at the source's frame rate and size, then the
pieces are joined by stream copy and the source's audio is muxed over them. The result is verified: its frame
count is the source's, and video and audio differ by less than a frame.

Pure stdlib; ``ffmpeg`` / ``ffprobe`` are on PATH in the sandbox. ``plan_frames`` is the pure part (unit-tested).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from crop_video import h264_encoder, video_codec_args  # noqa: E402  (one encoder choice for every script)


def plan_frames(starts: list[float], end: float, fps: Fraction) -> list[tuple[int, int]]:
    """``[(first_frame, frame_count)]`` per shot: each shot runs from its start to the next one's (the last to
    ``end``), on the source's frame grid. Raises if the starts are not ascending or a shot would have no frame."""
    if not starts:
        raise ValueError("no shots")
    bounds = [round(Fraction(str(s)) * fps) for s in starts] + [round(Fraction(str(end)) * fps)]
    out = []
    for i in range(len(starts)):
        n = bounds[i + 1] - bounds[i]
        if n <= 0:
            raise ValueError(f"shot {i} (start {starts[i]}) has no frames before the next shot / the end")
        out.append((bounds[i], n))
    return out


def _probe(path: str) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "stream=codec_type,r_frame_rate,width,height,nb_frames,duration:format=duration",
                        "-of", "json", path], capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def _video(info: dict) -> dict:
    return next(s for s in info["streams"] if s["codec_type"] == "video")


def _duration(info: dict, kind: str) -> float:
    s = next((s for s in info["streams"] if s["codec_type"] == kind), None)
    return float((s or {}).get("duration") or info["format"]["duration"])


def _run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {' '.join(cmd[:6])} …\n{r.stderr[-1500:]}")


def reassemble(spec: dict, out: str) -> dict:
    source = spec["source"]
    info = _probe(source)
    v = _video(info)
    fps = Fraction(v["r_frame_rate"])
    width, height = int(v["width"]), int(v["height"])
    end = float(spec.get("end") or _duration(info, "video"))
    shots = spec["shots"]
    frames = plan_frames([float(s["start"]) for s in shots], end, fps)
    enc = video_codec_args(h264_encoder("ffmpeg"))
    vf = (f"fps={fps.numerator}/{fps.denominator},scale={width}:{height}:force_original_aspect_ratio=decrease,"
          f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,tpad=stop_mode=clone:stop=-1")
    with tempfile.TemporaryDirectory() as tmp:
        parts: list[str] = []
        for i, (shot, (first, count)) in enumerate(zip(shots, frames)):
            pieces = shot.get("pieces") or [{"src": source, "offset": first / fps}]
            left = count
            for j, p in enumerate(pieces):
                if left <= 0:
                    break
                offset = float(p.get("offset") or 0.0)
                if j < len(pieces) - 1:  # an inner piece gives what it has; the last one lands on the count
                    have = int((Fraction(str(_duration(_probe(p["src"]), "video") - offset))) * fps)
                    n = max(1, min(left, have))
                else:
                    n = left
                part = os.path.join(tmp, f"s{i:03d}_{j:02d}.mp4")
                _run(["ffmpeg", "-y", "-v", "error", "-ss", f"{offset:.6f}", "-i", p["src"], "-vf", vf,
                      "-frames:v", str(n), "-an", *enc, "-video_track_timescale", str(fps.numerator), part])
                parts.append(part)
                left -= n
        listing = os.path.join(tmp, "parts.txt")
        with open(listing, "w") as fh:
            fh.writelines(f"file '{p}'\n" for p in parts)
        video = os.path.join(tmp, "video.mp4")
        _run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", listing, "-c", "copy", video])
        total = sum(n for _, n in frames)
        t0 = frames[0][0] / fps
        _run(["ffmpeg", "-y", "-v", "error", "-i", video, "-ss", f"{float(t0):.6f}", "-t", f"{float(total / fps):.6f}",
              "-i", source, "-map", "0:v", "-map", "1:a?", "-c:v", "copy", "-c:a", "aac", "-movflags", "+faststart",
              out])
    got = _probe(out)
    got_frames = int(_video(got).get("nb_frames") or 0)
    has_audio = any(s["codec_type"] == "audio" for s in got["streams"])
    gap = abs(_duration(got, "video") - _duration(got, "audio")) if has_audio else 0.0
    report = {"frames": got_frames, "expected_frames": total, "fps": f"{fps.numerator}/{fps.denominator}",
              "duration": round(_duration(got, "video"), 6), "av_gap": round(gap, 6), "shots": len(shots)}
    if got_frames != total or gap >= float(1 / fps):
        raise RuntimeError(f"reassembly is not frame-exact: {report}")
    return report


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    with open(sys.argv[1]) as fh:
        print(json.dumps(reassemble(json.load(fh), sys.argv[2])))
