"""The ONE keyframe reader (projects.keyframes): the timeline's snap targets and the clip's copy decision both use it.
Fixtures: 4 s of 25 fps H.264 with B-frames and a keyframe every 20 frames, its index (moov) at the front and at the
end. Expected = ffprobe's keyframe packets: 0, 0.8, 1.6, 2.4, 3.2."""
from __future__ import annotations

import inspect
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from projects import keyframes as kf  # noqa: E402

FX = Path(__file__).parent / "fixtures" / "mp4"


def _reader(data: bytes):
    reads = []

    def read(s, e):
        reads.append((s, e))
        return data[s:e + 1]
    return read, reads


def test_both_index_layouts_give_ffprobes_keyframes_through_a_few_reads():
    for name in ("kf-front.mp4", "kf-tail.mp4"):
        data = (FX / name).read_bytes()
        read, reads = _reader(data)
        assert kf.read_keyframes(read, len(data)) == [0.0, 0.8, 1.6, 2.4, 3.2], name
        assert len(reads) <= 4
        assert kf.file_keyframes(str(FX / name)) == [0.0, 0.8, 1.6, 2.4, 3.2]


def test_not_an_mp4_or_a_truncated_one_is_none_never_a_raise():
    junk = b"this is not a video file at all, just some bytes"
    assert kf.read_keyframes(_reader(junk)[0], len(junk)) is None
    cut = (FX / "kf-tail.mp4").read_bytes()[:9000]  # the trailing index is missing
    assert kf.read_keyframes(_reader(cut)[0], len(cut)) is None
    assert kf.keyframes_from_moov(struct.pack(">I4sI4s", 16, b"moov", 8, b"free")) is None  # no video track


def test_each_function_is_self_contained_so_the_sandbox_can_run_its_source():
    ns: dict = {}
    for fn in (kf.keyframes_from_moov, kf.read_keyframes, kf.file_keyframes):
        exec(compile("from typing import Callable, Optional\n" + inspect.getsource(fn), "sandbox", "exec"), ns)
    assert ns["file_keyframes"](str(FX / "kf-front.mp4")) == [0.0, 0.8, 1.6, 2.4, 3.2]
