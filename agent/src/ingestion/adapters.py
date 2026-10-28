"""M2C workflow outputs → the document builder's typed inputs (design Component 1, ``adapters.py``).

Pure. Every timestamp is converted here, once, to whole milliseconds rounded half up (Req 5.11). Conversion goes
through ``Decimal(str(x))`` so the decimal the workflow wrote is rounded, not its binary float: ``1.0005`` s is
1001 ms, where ``round(1.0005 * 1000)`` would give 1000. A value that cannot be read becomes ``None``, which the
builder counts as an omitted document (Req 5.14) instead of the build raising half-way.

Output shapes, measured on the staging M2C (fixtures under ``agent/tests/fixtures/ingestion/``):

- shot-segmentation ``shot-list``: ``{actual_duration_sec, shots: [{shot_id, start_frame, end_frame, start_time,
  end_time, selected_frames}]}`` (seconds).
- media-processing frame group ``frames``: ``{prefix: <key prefix>, keys: ["frame_0000006.jpg", ...]}``, where N
  is the SOURCE frame number, the numbering of ``start_frame``/``end_frame``/``selected_frames``.
- shot-metadata-enrichment ``shots`` (JSONL): ``{shot_id, description, objects: [{name}], setting,
  texts_on_screen, brands, sentiment, named_entities, people}``.
- speech-diarization ``transcript``: ``{audio_duration_sec, sentences: [{start, end, text, speaker_id}]}``.
- conversation-analysis ``analysis``: ``{audio_segments: [{start_time: "HH:MM:SS.mmm", end_time, segment_topic,
  key_events, named_entities}]}``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Optional

import logging

log = logging.getLogger(__name__)

_FRAME_RE = re.compile(r"frame_(\d+)\.jpe?g$", re.IGNORECASE)
_HMS_RE = re.compile(r"^(\d+):([0-5]?\d):([0-5]?\d(?:\.\d+)?)$")


@dataclass(frozen=True)
class ShotIn:
    start_ms: Optional[int]
    end_ms: Optional[int]
    frame_uri: str  # "" when no captured frame lies inside the shot (Req 5.24)


@dataclass(frozen=True)
class SceneIn:
    start_ms: Optional[int]
    end_ms: Optional[int]
    description: str = ""
    objects: tuple[str, ...] = ()
    setting: str = ""
    on_screen_text: str = ""
    brands: tuple[str, ...] = ()
    sentiment: str = ""
    entities: tuple[str, ...] = ()


@dataclass(frozen=True)
class TurnIn:
    start_ms: Optional[int]
    end_ms: Optional[int]
    text: str
    speaker: Optional[str]


@dataclass(frozen=True)
class TopicIn:
    start_ms: Optional[int]
    end_ms: Optional[int]
    summary: str
    entities: tuple[str, ...] = ()


# --------------------------------------------------------------------------------------------- time
def sec_to_ms(v: Any) -> Optional[int]:
    """Seconds (number or numeric string) → whole ms, half up. ``None`` for anything unreadable."""
    if isinstance(v, bool) or v is None:
        return None
    try:
        d = Decimal(str(v).strip())
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite():
        return None
    return int((d * 1000).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def hms_to_ms(v: Any) -> Optional[int]:
    """``HH:MM:SS(.fff)`` (conversation-analysis) or plain seconds → whole ms. ``None`` when malformed."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return sec_to_ms(v)
    m = _HMS_RE.match(str(v or "").strip())
    if not m:
        return None
    total = Decimal(int(m.group(1)) * 3600 + int(m.group(2)) * 60) + Decimal(m.group(3))
    return int((total * 1000).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def duration_ms(value_sec: Any) -> Optional[int]:
    """D for Req 5.12 / 12.9: ``None`` when missing, unreadable, or ≤ 0."""
    ms = sec_to_ms(value_sec)
    return ms if ms is not None and ms > 0 else None


# -------------------------------------------------------------------------------------------- lists
def strs(items: Any, *keys: str) -> tuple[str, ...]:
    """A list of strings or of objects carrying the first present of ``keys`` → non-blank strings, in order."""
    out: list[str] = []
    if isinstance(items, str):
        items = [items]
    for it in items if isinstance(items, list) else []:
        if isinstance(it, str):
            out.append(it)
        elif isinstance(it, dict):
            for k in keys:
                if isinstance(it.get(k), str) and it[k].strip():
                    out.append(it[k])
                    break
    return tuple(s for s in out if s.strip())


def _s(v: Any) -> str:
    return v if isinstance(v, str) else ""


# ------------------------------------------------------------------------------------------- frames
def frame_number(key: str) -> Optional[int]:
    m = _FRAME_RE.search(key or "")
    return int(m.group(1)) if m else None


def pick_frames(shots: list[dict], manifest: dict, manifest_uri: str) -> dict[Any, str]:
    """``shot_id`` → S3 URI of the shot's representative frame (Req 5.24).

    Only frames with ``start_frame <= N <= end_frame``; the one nearest the shot's first ``selected_frames``
    entry (or ``floor((start_frame + end_frame) / 2)``), ties to the smaller N. A shot with no frame inside its
    span is absent from the result.
    """
    if not manifest_uri.startswith("s3://"):
        return {}
    bucket = manifest_uri[5:].split("/", 1)[0]
    prefix = str(manifest.get("prefix") or "").strip("/")
    if prefix.startswith("s3://"):
        prefix = prefix[5:].split("/", 1)[1] if "/" in prefix[5:] else ""
    frames: list[tuple[int, str]] = []
    for k in manifest.get("keys") or []:
        n = frame_number(str(k))
        if n is not None:
            key = f"{prefix}/{k}" if prefix else str(k)
            frames.append((n, f"s3://{bucket}/{key}"))
    frames.sort()
    out: dict[Any, str] = {}
    for sh in shots:
        try:
            s, e = int(sh["start_frame"]), int(sh["end_frame"])
        except (KeyError, TypeError, ValueError):
            continue
        sel = sh.get("selected_frames") or []
        try:
            target = int(sel[0]) if sel else (s + e) // 2
        except (TypeError, ValueError):
            target = (s + e) // 2
        inside = [f for f in frames if s <= f[0] <= e]
        if inside:
            out[sh.get("shot_id")] = min(inside, key=lambda f: (abs(f[0] - target), f[0]))[1]
    return out


# ------------------------------------------------------------------------------------------- inputs
def shots_input(shot_list: dict, frame_for: dict[Any, str]) -> list[ShotIn]:
    return [ShotIn(sec_to_ms(sh.get("start_time")), sec_to_ms(sh.get("end_time")),
                   frame_for.get(sh.get("shot_id"), ""))
            for sh in shot_list.get("shots") or [] if isinstance(sh, dict)]


def enrich_input(shot_list: dict, shot_md: list[dict]) -> list[SceneIn]:
    """One scene input per enrichment line, in the enrichment output's order, timed by its shot."""
    times = {sh.get("shot_id"): sh for sh in shot_list.get("shots") or [] if isinstance(sh, dict)}
    out: list[SceneIn] = []
    for md in shot_md:
        if not isinstance(md, dict):
            continue
        sh = times.get(md.get("shot_id")) or {}
        out.append(SceneIn(
            start_ms=sec_to_ms(sh.get("start_time")), end_ms=sec_to_ms(sh.get("end_time")),
            description=_s(md.get("description")),
            objects=strs(md.get("objects"), "name"),
            setting=_s(md.get("setting")),
            on_screen_text=", ".join(s.strip() for s in strs(md.get("texts_on_screen"), "text", "value", "content")),
            brands=strs(md.get("brands"), "name"),
            sentiment=_s(md.get("sentiment")),
            entities=strs(md.get("named_entities"), "name", "text") + strs(md.get("people"), "name"),
        ))
    return out


def has_speech(transcript: Optional[dict]) -> bool:
    """Req 4.7: some transcript sentence with non-blank text."""
    return any(isinstance(s, dict) and _s(s.get("text")).strip()
               for s in (transcript or {}).get("sentences") or [])


def diar_input(transcript: dict) -> list[TurnIn]:
    out: list[TurnIn] = []
    for s in transcript.get("sentences") or []:
        if not isinstance(s, dict):
            continue
        spk = s.get("speaker_id")
        out.append(TurnIn(sec_to_ms(s.get("start")), sec_to_ms(s.get("end")), _s(s.get("text")),
                          str(spk) if spk not in (None, "") else None))
    return out


def topic_summary(seg: dict) -> str:
    """conversation-analysis has no ``summary``: the topic title plus its key events."""
    title = _s(seg.get("segment_topic")).strip()
    events = "; ".join(e.strip() for e in strs(seg.get("key_events")))
    return f"{title}. {events}" if title and events else (title or events)


def topics_input(analysis: dict) -> list[TopicIn]:
    return [TopicIn(hms_to_ms(s.get("start_time")), hms_to_ms(s.get("end_time")), topic_summary(s),
                    strs(s.get("named_entities"), "name"))
            for s in analysis.get("audio_segments") or [] if isinstance(s, dict)]


def _num(v: Any) -> Optional[float]:
    """A positive finite number from an ffprobe value (they are often strings: ``"16981548"``), else ``None``."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 and f == f and f != float("inf") else None


def _int(v: Any) -> Optional[int]:
    f = _num(v)
    return int(f) if f is not None else None


def _fps(rate: Any) -> Optional[float]:
    """``"24/1"`` / ``"30000/1001"`` → 24.0 / 29.97 (2 dp); ``"0/0"`` and junk → ``None``."""
    if not isinstance(rate, str) or "/" not in rate:
        return None
    n, _, d = rate.partition("/")
    num, den = _num(n), _num(d)
    return round(num / den, 2) if num and den else None


def _compact(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "", {})}


def technical_from_probe(probe: Any) -> Optional[dict]:
    """The media-processing ``probe.json`` → a compact, model-readable technical summary, or ``None``.

    Shape measured on the staging M2C (2026-10-09, ``micro-workflow/media-processing/<uuid>/probe.json``):
    ``{format_name, duration_sec, file_size, probe_info: {streams: [...ffprobe streams], format: {bit_rate,
    tags: {encoder}}}}``. Only an allowlist is copied: ``probe_info.format.filename`` is a presigned URL that
    carries a session token, and must never reach the asset row.
    """
    if not isinstance(probe, dict):
        return None
    info = probe.get("probe_info") if isinstance(probe.get("probe_info"), dict) else {}
    streams = [s for s in (info.get("streams") or []) if isinstance(s, dict)]
    fmt = info.get("format") if isinstance(info.get("format"), dict) else {}
    tags = fmt.get("tags") if isinstance(fmt.get("tags"), dict) else {}
    v = next((s for s in streams if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    out = _compact({
        "container": _s(probe.get("format_name") or fmt.get("format_name")) or None,
        "durationSec": _num(probe.get("duration_sec") or fmt.get("duration")),
        "sizeBytes": _int(probe.get("file_size") or fmt.get("size")),
        "bitRate": _int(fmt.get("bit_rate")),
        "encoder": _s(tags.get("encoder")) or None,
        "video": _compact({
            "codec": _s(v.get("codec_name")) or None, "profile": _s(v.get("profile")) or None,
            "width": _int(v.get("width")), "height": _int(v.get("height")),
            "fps": _fps(v.get("avg_frame_rate")) or _fps(v.get("r_frame_rate")),
            "bitRate": _int(v.get("bit_rate")), "pixFmt": _s(v.get("pix_fmt")) or None,
        }) if v else None,
        "audio": _compact({
            "codec": _s(a.get("codec_name")) or None, "channels": _int(a.get("channels")),
            "sampleRate": _int(a.get("sample_rate")), "bitRate": _int(a.get("bit_rate")),
        }) if a else None,
    })
    return out if (out.get("video") or out.get("audio")) else None


# ----------------------------------------------------------------------------- entity metadata (Phase 1)
# These convert a raw toolkit output into a compact, JSON-safe structure captured in the step's ``facts``.
# They are pure and never raise: an unreadable field is dropped, exactly like the document adapters above.
# The compact rows are then flattened into timed entity MENTIONS (:func:`person_mentions`,
# :func:`cast_mentions`, :func:`segment_mentions`) that the document builder folds into the member scenes'
# ``named_entities`` by time overlap, so the names are searchable in hybrid search (they are NOT stored as a
# side-channel metadata blob on the asset row). Shapes verified against live staging outputs and the
# targets' describe_tool contracts (docs/entity-metadata-workflows.md).

def _is_index(v: Any) -> bool:
    """A non-negative integer id (``person_id`` / ``sequence_id`` are 0-based), not a bool."""
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _index(v: Any) -> Optional[int]:
    return v if _is_index(v) else None


def _ints(items: Any) -> tuple[int, ...]:
    """Non-negative ints from a list (``shot_ids`` / ``scene_ids``), dropping anything else."""
    return tuple(x for x in (items if isinstance(items, list) else []) if _is_index(x))


def ms_int(v: Any) -> Optional[int]:
    """A value already in milliseconds (int/float/str) → whole ms, half up. ``None`` if unreadable.

    face-recognition and cast-resolution emit ``begin_ms``/``end_ms`` directly; this is the ms twin of
    :func:`sec_to_ms` (which is for the seconds-valued fields).
    """
    if isinstance(v, bool) or v is None:
        return None
    try:
        d = Decimal(str(v).strip())
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite():
        return None
    return int(d.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _score(v: Any) -> Optional[float]:
    """A finite number (break-candidate ``score`` may legitimately be 0 or negative), else ``None``."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 4) if f == f and f not in (float("inf"), float("-inf")) else None


def _spans(appearances: Any) -> list[dict]:
    """``[{begin_ms, end_ms}]`` (already ms) → ``[{start_ms, end_ms}]`` with start < end, dropping junk."""
    out: list[dict] = []
    for a in appearances if isinstance(appearances, list) else []:
        if not isinstance(a, dict):
            continue
        b, e = ms_int(a.get("begin_ms")), ms_int(a.get("end_ms"))
        if b is not None and e is not None and b < e:
            out.append({"start_ms": b, "end_ms": e})
    return out


def _warn_if_dropped(tool: str, field: str, raw: Any, emitted: list) -> None:
    """These three adapters were written against a ``describe_tool`` example / a handful of live runs; a
    silent output-shape drift would make them yield nothing while the workflow reported success. A
    non-empty input that produces zero emitted rows is that signature — log it (INFO) so drift is visible
    in the step logs instead of surfacing as inexplicably empty entity-metadata rows."""
    if isinstance(raw, list) and raw and not emitted:
        log.info("%s: %d %s in the output produced 0 compact rows - possible output-shape drift",
                 tool, len(raw), field)


def person_identities(face_metadata: Optional[dict]) -> list[dict]:
    """face-recognition ``facemetadata.json`` → compact Person-identity rows (asset metadata).

    These are REAL identities (celebrity recognition / collection matches), not the generic names the
    enrichment step attaches per scene: ``named`` distinguishes a resolved identity from an anonymous
    track (which keeps its ``Person_N`` placeholder). ``screen_ms`` comes from ``total_screen_time_sec``;
    appearance spans are already in ms.
    """
    out: list[dict] = []
    for p in (face_metadata or {}).get("people") or []:
        if not isinstance(p, dict) or not _is_index(p.get("person_id")):
            continue
        out.append(_compact({
            "person_id": int(p["person_id"]),
            "name": _s(p.get("name")),
            "named": bool(p.get("named")),
            "name_source": _s(p.get("name_source")),
            "screen_ms": sec_to_ms(p.get("total_screen_time_sec")),
            "appearances": _spans(p.get("appearances")),
        }))
    _warn_if_dropped("face-recognition", "people", (face_metadata or {}).get("people"), out)
    return out


def program_structure(structure: Optional[dict]) -> dict:
    """program-structure-analysis ``structure.json`` → compact Program-element metadata.

    Marker and sequence spans are in SECONDS in the source → whole ms. Only marker kinds that are present
    (non-empty) are kept; empty structural slots are dropped.
    """
    s = structure if isinstance(structure, dict) else {}
    markers_in = s.get("markers") if isinstance(s.get("markers"), dict) else {}
    markers_out: dict[str, list] = {}
    for kind, spans in markers_in.items():
        rows = []
        for m in spans if isinstance(spans, list) else []:
            if not isinstance(m, dict):
                continue
            st, en = sec_to_ms(m.get("start")), sec_to_ms(m.get("end"))
            if st is not None and en is not None:
                rows.append(_compact({"start_ms": st, "end_ms": en, "kinds": list(strs(m.get("kinds"))),
                                      "shot_ids": list(_ints(m.get("shot_ids")))}))
        if rows:
            markers_out[str(kind)] = rows
    sequences = []
    for seq in s.get("sequences") or []:
        if not isinstance(seq, dict):
            continue
        sequences.append(_compact({
            "sequence_id": _index(seq.get("sequence_id")),
            "start_ms": sec_to_ms(seq.get("start")),
            "end_ms": sec_to_ms(seq.get("end")),
            "scene_ids": list(_ints(seq.get("scene_ids"))),
            "settings": list(strs(seq.get("settings"))),
        }))
    breaks = []
    for b in s.get("break_candidates") or []:
        if isinstance(b, dict):
            at = sec_to_ms(b.get("at"))
            if at is not None:
                breaks.append(_compact({"at_ms": at, "score": _score(b.get("score"))}))
    out: dict = {"mode": _s(s.get("mode"))}
    if markers_out:
        out["markers"] = markers_out
    if sequences:
        out["sequences"] = sequences
    if breaks:
        out["break_candidates"] = breaks
    if (markers_in or s.get("sequences") or s.get("break_candidates")) and not (
            markers_out or sequences or breaks):
        log.info("program-structure-analysis: a non-empty structure.json produced no markers, sequences "
                 "or break candidates - possible output-shape drift")
    return _compact(out)


def cast_characters(persons_doc: Optional[dict]) -> list[dict]:
    """cast-resolution ``persons.json`` → compact Character rows (actor → role).

    ``name`` is the actor (``null`` means the pipeline abstained — a wrong name is worse than none, so
    ``named`` is False then); ``character_name`` is additive, present only when a credits card paired the
    actor with a character. ``screen_ms``/``speech_ms`` from the seconds fields; spans already in ms.
    """
    out: list[dict] = []
    for p in (persons_doc or {}).get("persons") or []:
        if not isinstance(p, dict) or not _is_index(p.get("person_id")):
            continue
        name = _s(p.get("name"))
        out.append(_compact({
            "person_id": int(p["person_id"]),
            "name": name,
            "named": bool(name),
            "character_name": _s(p.get("character_name")),
            "role": _s(p.get("role")),
            "type": _s(p.get("type")),
            "aliases": list(strs(p.get("aliases"))),
            "speaker_ids": list(strs(p.get("speaker_ids"))),
            "screen_ms": sec_to_ms(p.get("screen_sec")),
            "speech_ms": sec_to_ms(p.get("speech_sec")),
            "appearances": _spans(p.get("appearances")),
        }))
    _warn_if_dropped("cast-resolution", "persons", (persons_doc or {}).get("persons"), out)
    return out


# ----------------------------------------------------------------- entity mentions (fold into scenes)
@dataclass(frozen=True)
class EntityMentionIn:
    """A single display name/label and the ms span it appears in. The document builder folds each mention
    into the ``named_entities`` of every scene whose span overlaps ``[start_ms, end_ms)`` (Req: entities
    searchable in hybrid search). ``start_ms < end_ms`` is guaranteed by the builders below."""
    name: str
    start_ms: int
    end_ms: int


def _mention_spans(appearances: Any) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for a in appearances if isinstance(appearances, list) else []:
        if not isinstance(a, dict):
            continue
        s, e = a.get("start_ms"), a.get("end_ms")
        if isinstance(s, int) and not isinstance(s, bool) and isinstance(e, int) and not isinstance(e, bool) \
                and s < e:
            out.append((s, e))
    return out


def person_mentions(people: Any) -> list[EntityMentionIn]:
    """face-recognition :func:`person_identities` rows → one mention per (name, appearance).

    The display name is the resolved identity (a recognised celebrity like ``Kevin Kilner``) or, for an
    anonymous track, its ``Person_N`` placeholder. A row with no name or no appearance span contributes
    nothing."""
    out: list[EntityMentionIn] = []
    for p in people if isinstance(people, list) else []:
        if not isinstance(p, dict):
            continue
        name = _s(p.get("name")).strip()
        if not name:
            continue
        for s, e in _mention_spans(p.get("appearances")):
            out.append(EntityMentionIn(name, s, e))
    return out


def cast_mentions(cast: Any) -> list[EntityMentionIn]:
    """cast-resolution :func:`cast_characters` rows → mentions for the character name AND the actor name.

    Both are folded (per appearance) when present: the character (``Det. Rios``) and the actor (``Tomas
    Vega``). A row that abstained on the actor name still contributes its character name, and vice versa."""
    out: list[EntityMentionIn] = []
    for c in cast if isinstance(cast, list) else []:
        if not isinstance(c, dict):
            continue
        names = [n for n in (_s(c.get("character_name")).strip(), _s(c.get("name")).strip()) if n]
        if not names:
            continue
        for s, e in _mention_spans(c.get("appearances")):
            for name in names:
                out.append(EntityMentionIn(name, s, e))
    return out


def _humanize(v: Any) -> str:
    """A marker kind / sequence setting label for search: ``end_credits`` → ``end credits``. ``""`` for junk."""
    return " ".join(str(v).replace("_", " ").split()) if isinstance(v, str) and v.strip() else ""


def segment_mentions(program: Any) -> list[EntityMentionIn]:
    """program-structure-analysis :func:`program_structure` → Segment / Program-element label mentions.

    A sequence contributes each of its ``settings`` (the setting of that part of the program) over the
    sequence span; a marker contributes its kind (``opening credits``, ``end credits``, …) over the marker
    span. Labels are humanised (underscores → spaces) so they read as search terms. Structural grouping of
    the member scenes under a Segment parent is a future enhancement (it needs a hybrid-search backend
    change — see ``contract.first_violation``'s ``segment_parent_id`` rejection)."""
    prog = program if isinstance(program, dict) else {}
    out: list[EntityMentionIn] = []
    for seq in prog.get("sequences") or []:
        if not isinstance(seq, dict):
            continue
        s, e = seq.get("start_ms"), seq.get("end_ms")
        if not (isinstance(s, int) and not isinstance(s, bool) and isinstance(e, int)
                and not isinstance(e, bool) and s < e):
            continue
        for setting in seq.get("settings") or []:
            label = _humanize(setting)
            if label:
                out.append(EntityMentionIn(label, s, e))
    markers = prog.get("markers") if isinstance(prog.get("markers"), dict) else {}
    for kind, spans in markers.items():
        label = _humanize(kind)
        if not label:
            continue
        for m in spans if isinstance(spans, list) else []:
            if not isinstance(m, dict):
                continue
            s, e = m.get("start_ms"), m.get("end_ms")
            if isinstance(s, int) and not isinstance(s, bool) and isinstance(e, int) \
                    and not isinstance(e, bool) and s < e:
                out.append(EntityMentionIn(label, s, e))
    return out
