"""The keyframe times of an MP4/MOV, read from its index (``moov``) — never by decoding the media.

ONE implementation for both places that need them:
  * the timeline's snap targets (the Asset_Service ``keyframes`` command, over a few byte ranges of the source), so a
    range placed by eye starts on a keyframe;
  * the clip action's copy decision (``media_actions``: the sandbox reads its own local copy), so a clip that starts
    on one is a stream copy — instant, and exact.

A keyframe here is a SYNC SAMPLE (``stss``): a frame every later frame decodes from. An open-GOP I-frame is not one,
and a copy from it carries frames from before the cut that only an edit list hides (10 of 24 sampled on Meridian), so
the index is exactly the right set. Presentation time, as ffprobe reports it: decode time (``stts``) plus the sample's
``ctts`` offset, minus the edit list's media start (``elst``), plus any leading empty edit — over the track's ``mdhd``
timescale (empty edits are in the movie's ``mvhd`` timescale). Pure stdlib: the sandbox program injects it verbatim.
"""
from __future__ import annotations

from typing import Callable, Optional

def keyframes_from_moov(moov: bytes) -> Optional[list]:
    """Keyframe times (s, ascending) from a complete ``moov`` box's bytes; ``None`` without a video track, a sync
    table (every frame is a keyframe: nothing worth snapping to) or a readable index."""
    import struct

    def boxes(start, end):
        out, at = [], start
        while at + 8 <= end:
            size, typ = struct.unpack_from(">I4s", moov, at)
            head = 8
            if size == 1:
                if at + 16 > end:
                    break
                size, head = struct.unpack_from(">Q", moov, at + 8)[0], 16
            elif size == 0:
                size = end - at
            if size < head or at + size > end:
                break
            out.append((typ.decode("latin1"), at, size, head))
            at += size
        return out

    def child(box, typ):
        return next((b for b in boxes(box[1] + box[3], box[1] + box[2]) if b[0] == typ), None)

    def path(box, *types):
        for t in types:
            box = child(box, t) if box else None
        return box

    def u32(at):
        return struct.unpack_from(">I", moov, at)[0]

    root = next((b for b in boxes(0, len(moov)) if b[0] == "moov"), None)
    if not root:
        return None
    mvhd = child(root, "mvhd")
    movie_scale = u32(mvhd[1] + mvhd[3] + (20 if moov[mvhd[1] + mvhd[3]] == 1 else 12)) if mvhd else 0
    for trak in (b for b in boxes(root[1] + root[3], root[1] + root[2]) if b[0] == "trak"):
        hdlr = path(trak, "mdia", "hdlr")
        if not hdlr or moov[hdlr[1] + hdlr[3] + 8:hdlr[1] + hdlr[3] + 12] != b"vide":
            continue
        mdhd, stbl = path(trak, "mdia", "mdhd"), path(trak, "mdia", "minf", "stbl")
        if not mdhd or not stbl:
            return None
        scale = u32(mdhd[1] + mdhd[3] + (20 if moov[mdhd[1] + mdhd[3]] == 1 else 12))
        stss, stts, ctts = child(stbl, "stss"), child(stbl, "stts"), child(stbl, "ctts")
        if not scale or not stts or not stss:
            return None

        media_start, empty_lead = 0, 0.0
        elst = path(trak, "edts", "elst")
        if elst:
            p = elst[1] + elst[3]
            v, n = moov[p], u32(p + 4)
            q = p + 8
            for _ in range(n):
                if v == 1:
                    seg, mt = struct.unpack_from(">Qq", moov, q)
                    q += 20
                else:
                    seg, mt = struct.unpack_from(">Ii", moov, q)
                    q += 12
                if mt == -1:
                    if movie_scale:
                        empty_lead += seg / movie_scale
                    continue
                media_start = mt
                break

        sp = stss[1] + stss[3]
        sync = [u32(sp + 8 + i * 4) for i in range(u32(sp + 4))]

        offsets: list = []  # (count, offset) runs of ctts, consumed alongside the samples
        if ctts:
            cp = ctts[1] + ctts[3]
            signed = moov[cp] == 1
            for i in range(u32(cp + 4)):
                count = u32(cp + 8 + i * 8)
                off = struct.unpack_from(">i" if signed else ">I", moov, cp + 12 + i * 8)[0]
                offsets.append([count, off])

        out: list = []
        tp = stts[1] + stts[3]
        sample, dts, k, ci = 1, 0, 0, 0
        for e in range(u32(tp + 4)):
            if k >= len(sync):
                break
            count, delta = u32(tp + 8 + e * 8), u32(tp + 12 + e * 8)
            for _ in range(count):
                if k >= len(sync):
                    break
                while ci < len(offsets) and offsets[ci][0] == 0:
                    ci += 1
                off = offsets[ci][1] if ci < len(offsets) else 0
                if sample == sync[k]:
                    out.append(empty_lead + (dts + off - media_start) / scale)
                    k += 1
                if ci < len(offsets):
                    offsets[ci][0] -= 1
                sample += 1
                dts += delta
        return sorted(max(0.0, round(t, 6)) for t in out if t >= -1e-6)
    return None


def read_keyframes(read: Callable[[int, int], bytes], file_size: int, head_bytes: int = 64 * 1024,
                   max_moov_bytes: int = 32 * 1024 * 1024) -> Optional[list]:
    """Find the ``moov`` by walking the top-level boxes through ``read(start, end_inclusive)`` (a few small reads),
    then parse it. Works whether the index is at the front (faststart) or the end. A moov over ``max_moov_bytes`` is
    skipped (tens of MB means hours of media; snapping is a nicety)."""
    import struct

    at = 0
    for _ in range(64):
        if at + 8 > file_size:
            return None
        head = read(at, min(file_size, at + head_bytes) - 1)
        if len(head) < 8:
            return None
        size, typ = struct.unpack_from(">I4s", head, 0)
        if size == 1:
            size = struct.unpack_from(">Q", head, 8)[0] if len(head) >= 16 else 0
        elif size == 0:
            size = file_size - at
        if size < 8:
            return None
        if typ == b"moov":
            if size > max_moov_bytes:
                return None
            moov = head[:size] if size <= len(head) else read(at, at + size - 1)
            return keyframes_from_moov(moov) if len(moov) == size else None
        at += size
    return None


def file_keyframes(path_: str) -> Optional[list]:
    """:func:`read_keyframes` of a local file (the sandbox's copy of the source)."""
    import os

    size = os.path.getsize(path_)
    with open(path_, "rb") as fh:
        def read(start, end):
            fh.seek(start)
            return fh.read(end - start + 1)
        return read_keyframes(read, size)
