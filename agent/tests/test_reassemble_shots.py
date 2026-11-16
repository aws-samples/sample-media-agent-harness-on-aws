"""reassemble_shots: a scene rebuilt from replaced shots has the source's frame rate, size and frame count, and its
audio stays in sync (8e2acba8's reel came back at fal's 24 fps, 4033 frames for a 5044-frame scene). The pure frame
plan is tested always; the ffmpeg run needs ffmpeg/ffprobe on PATH (skipped otherwise)."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "video-processing" / "scripts"))

import reassemble_shots as rs  # noqa: E402

NTSC = Fraction(30000, 1001)


def test_each_shot_runs_start_to_start_on_the_source_grid():
    plan = rs.plan_frames([0.0, 3.2, 6.7], 10.0, NTSC)
    assert plan == [(0, 96), (96, 105), (201, 99)]
    assert sum(n for _, n in plan) == round(10 * NTSC)  # gaps between detected shots are absorbed


def test_a_shot_with_no_frames_is_refused():
    with pytest.raises(ValueError):
        rs.plan_frames([0.0, 0.01, 2.0], 3.0, NTSC)


needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                  reason="needs ffmpeg + ffprobe")


def _ff(*args):
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


@needs_ffmpeg
def test_replacements_at_another_rate_and_size_land_frame_exact(tmp_path):
    src = str(tmp_path / "src.mp4")
    _ff("-f", "lavfi", "-i", "testsrc=size=640x360:rate=30000/1001:duration=10",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=10", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", src)
    short = str(tmp_path / "short24.mp4")   # 2.5 s for a 3.2 s shot: its last frame is held
    _ff("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24:duration=2.5", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        short)
    half = str(tmp_path / "half24.mp4")     # two 2 s halves for a 3.5 s shot: the second is cut to fit
    _ff("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        half)
    spec = {"source": src, "end": 10.0, "shots": [
        {"start": 0.0, "pieces": [{"src": short}]},
        {"start": 3.2, "pieces": [{"src": half}, {"src": half}]},
        {"start": 6.7},
    ]}
    report = rs.reassemble(spec, str(tmp_path / "out.mp4"))
    assert report["fps"] == "30000/1001"
    assert report["frames"] == report["expected_frames"] == round(10 * NTSC)
    assert report["av_gap"] < 1 / 29.97
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                       "stream=width,height", "-of", "json", str(tmp_path / "out.mp4")],
                                      capture_output=True, text=True, check=True).stdout)
    assert (probe["streams"][0]["width"], probe["streams"][0]["height"]) == (640, 360)
