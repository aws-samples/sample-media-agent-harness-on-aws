"""Project analysis JSON onto render-template props using a skill DECLARATION.

Why this exists
---------------
Media tools emit one media file plus their findings in JSON sidecars. The manifest
builder only mapped media files to tracks, so every analysis result rendered as a
single bare track and the findings were dropped: shot segmentation showed 1 track
and 0 markers while `shots.json` held 9 shots; face recognition fell back to a
generic preview while `face_tracks.json` held 93 tracks with normalized boxes and
`identity_clusters.json` named 5 people.

The fix follows the bounded-generation pattern that the generative-UI literature
has converged on (see Portal UX Agent, arXiv:2511.00843): an LLM may PLAN at a
high level, but assembly is deterministic and constrained to a vetted component
inventory. Here the plan is a per-skill declaration — data, not code — and this
module is the deterministic projector.

Adding support for a new analysis tool therefore means shipping a SKILL.md, not
editing this file. That is the whole point: no per-tool branching lives here.

Declaration grammar (see `_parse_skill_frontmatter`)
    markers:  <listPath> time=<f> [end=<f>] [label=<f>] [category=<f>]
    segments: <listPath> start=<f> end=<f> [name=<f>] [group=<f>]
    boxes:    <listPath> box=<f> [time=<f>] [label=<f>]
    rows:     <listPath> cols=<f1>,<f2>,…
    duration: <f>
    summary:  <f1>,<f2>,…

`<listPath>` and `<f>` are dotted paths. A path segment may be `[]` to flat-map a
nested list, so `tracks[].faces[]` walks every face of every track.
"""
from __future__ import annotations

import json
from typing import Any, Optional

__all__ = [
    "dedupe_rows",
    "dedupe_tables",
    "dedupe_segments",
    "dedupe_boxes",
    "dedupe_thumbnails",
    "parse_spec",
    "resolve_path",
    "collect_list",
    "project_markers",
    "project_lanes",
    "project_segments",
    "project_boxes",
    "project_boxes_by_entity",
    "group_boxes_by_frame",
    "to_percent_boxes",
    "project_rows",
    "project_thumbnails",
    "project_duration",
    "project_summary",
    "declaration_applies",
]


def _row_signature(row: dict) -> str:
    """Order-independent signature of a table row."""
    return json.dumps({k: row[k] for k in sorted(row)}, sort_keys=True, default=str)


def dedupe_rows(rows: list[dict]) -> list[dict]:
    """Drop repeated rows, preserving order.

    Guards against a result restating the same entity — for instance a face
    collection that has accumulated the same identity across repeated ingests.
    """
    seen: set[str] = set()
    out: list[dict] = []
    for r in rows:
        sig = _row_signature(r)
        if sig in seen:
            continue
        seen.add(sig)
        out.append(r)
    return out


_IDENTITY_COLUMNS = ("name", "label", "title", "id", "person_id", "face_group_id", "speaker")


def _table_identity(table: dict) -> str:
    """What a table is ABOUT, so two descriptions of it can be recognised as one.

    Keys on the values of the most identifying column rather than the whole table,
    because the point is to spot tables describing the same entities with different
    columns.
    """
    rows = table.get("rows") or []
    if not rows:
        return "empty"
    keys = {k for r in rows for k in r}
    for col in _IDENTITY_COLUMNS:
        if col in keys:
            values = sorted(str(r.get(col)) for r in rows if r.get(col) is not None)
            if values:
                return f"{col}:" + "|".join(values)
    return "rows:" + "|".join(sorted(_row_signature(r) for r in rows))


def _table_richness(table: dict) -> tuple[int, int]:
    """More columns, then more populated cells. Used to pick the best duplicate."""
    cols = table.get("columns") or []
    rows = table.get("rows") or []
    filled = sum(1 for r in rows for v in r.values() if v not in (None, "", 0))
    return (len(cols), filled)


def dedupe_tables(tables: list[dict]) -> list[dict]:
    """Collapse tables describing the same entities, keeping the richest.

    These pipelines routinely emit several artifacts restating one finding — face
    recognition writes `people[]` into facemetadata.json, facegroup.json AND
    face_clusters.json. Projecting each independently produced one table per file:
    the same person listed several times over. Deduping on content rather than on
    which file it came from keeps that per-tool knowledge out of the code.
    """
    best: dict[str, dict] = {}
    order: list[str] = []
    for t in tables:
        t = {**t, "rows": dedupe_rows(t.get("rows") or [])}
        ident = _table_identity(t)
        if ident not in best:
            best[ident] = t
            order.append(ident)
        elif _table_richness(t) > _table_richness(best[ident]):
            best[ident] = t
    return [best[k] for k in order]


def dedupe_segments(segments: list[dict]) -> list[dict]:
    """Drop segments repeating the same group and span."""
    seen: set[tuple] = set()
    out: list[dict] = []
    for s in segments:
        key = (s.get("group"), s.get("name"), s.get("start"), s.get("end"))
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def dedupe_boxes(boxes: list[dict]) -> list[dict]:
    """Drop boxes repeating the same frame, geometry and label."""
    seen: set[tuple] = set()
    out: list[dict] = []
    for b in boxes:
        key = (b.get("frame"), b.get("time"), b.get("x"), b.get("y"),
               b.get("w"), b.get("h"), b.get("label"))
        if key in seen:
            continue
        seen.add(key)
        out.append(b)
    return out


def dedupe_thumbnails(thumbnails: list[dict]) -> list[dict]:
    """One entry per stored image key."""
    seen: set[str] = set()
    out: list[dict] = []
    for t in thumbnails:
        key = str(t.get("key") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def parse_spec(spec: str) -> tuple[str, dict[str, str]]:
    """Split `"shots[] time=start_time label=shot_id"` into (path, field map)."""
    if not spec or not isinstance(spec, str):
        return "", {}
    parts = spec.strip().split()
    if not parts:
        return "", {}
    path = parts[0]
    fields: dict[str, str] = {}
    for tok in parts[1:]:
        if "=" in tok:
            k, v = tok.split("=", 1)
            k, v = k.strip(), v.strip()
            if k and v:
                fields[k] = v
    return path, fields


def resolve_path(data: Any, path: str) -> Any:
    """Follow a dotted path, treating `[]` as flat-map over a list."""
    if not path:
        return None
    cur: Any = data
    for raw in path.split("."):
        seg = raw.strip()
        if not seg:
            continue
        flat = seg.endswith("[]")
        key = seg[:-2] if flat else seg
        if key:
            if isinstance(cur, list):
                nxt = []
                for item in cur:
                    if isinstance(item, dict) and key in item:
                        nxt.append(item[key])
                cur = nxt
                if flat:
                    cur = [x for sub in cur if isinstance(sub, list) for x in sub]
                continue
            if not isinstance(cur, dict) or key not in cur:
                return None
            cur = cur[key]
        if flat and isinstance(cur, list):
            if cur and isinstance(cur[0], list):
                cur = [x for sub in cur if isinstance(sub, list) for x in sub]
    return cur


def collect_list(data: Any, path: str) -> list[dict]:
    """Resolve `path` to a list of dicts (empty when absent or wrong shape)."""
    got = resolve_path(data, path)
    if isinstance(got, dict):
        got = [got]
    if not isinstance(got, list):
        return []
    return [x for x in got if isinstance(x, dict)]


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def declaration_applies(data: Any, declaration: dict) -> bool:
    """Whether a skill's `detect` path is present in this payload.

    Several tools emit `application/json`, so MIME alone cannot pick the right
    skill. `detect` disambiguates: `shots` matches shots.json, `tracks` matches
    face_tracks.json. A skill with no `detect` always applies.
    """
    detect = (declaration or {}).get("detect")
    if not detect:
        return True
    for path in [p.strip() for p in detect.split(",") if p.strip()]:
        got = resolve_path(data, path)
        if got not in (None, [], {}):
            return True
    return False


def project_markers(data: Any, spec: str, lane_set: str = "") -> list[dict]:
    """-> [{time, endTime?, label, category?, note?, laneSet?}] for `video-markers` / manifest markers.

    Spans that tile the film (shots, scenes) are markers: each SET is its own band row on the timeline, so
    ``lane_set`` stamps it and prefixes the id (shot 5 and scene 5 must not merge). ``note=<field>`` is shown
    on hover (a scene's boundary reason).
    """
    path, f = parse_spec(spec)
    scale = _scale_for(f)
    if not scale:
        return []
    out: list[dict] = []
    for i, item in enumerate(collect_list(data, path)):
        t = _num(item.get(f.get("time", "time")))
        if t is None:
            continue
        t *= scale
        m: dict[str, Any] = {"time": round(t, 3)}
        end = _num(item.get(f.get("end", "__none__")))
        if end is not None:
            end *= scale
        if end is not None and end > t:
            m["endTime"] = round(end, 3)
        raw_label = item.get(f.get("label", "__none__"))
        label = "" if raw_label is None else str(raw_label)
        cat = item.get(f.get("category", "__none__"))
        m["label"] = label if label else f"{i + 1}"
        if cat is not None:
            m["category"] = str(cat)
        note = item.get(f.get("note", "__none__"))
        if note is not None and str(note):
            m["note"] = str(note)
        m["id"] = f"{lane_set}-{label or i}" if lane_set else f"m-{label or i}"
        if lane_set:
            m["laneSet"] = lane_set
        out.append(m)
    return out


def project_segments(data: Any, spec: str, lane_set: str = "") -> list[dict]:
    """-> [{id, name, start, end, group?, laneSet?}] for interactive segment rows / lanes.

    ``group=<field>`` names each segment's row from the item; ``lane=<Name>`` puts them all on ONE named row
    (scenes, chapters). ``lane_set`` stamps the declaration's lane set on every segment.
    """
    path, f = parse_spec(spec)
    scale = _scale_for(f)
    if not scale:
        return []
    out: list[dict] = []
    for i, item in enumerate(collect_list(data, path)):
        s = _num(item.get(f.get("start", "start")))
        e = _num(item.get(f.get("end", "end")))
        if s is None or e is None:
            continue
        s, e = s * scale, e * scale
        if e <= s:
            continue
        group = f["lane"] if f.get("lane") else item.get(f.get("group", "__none__"))
        name = item.get(f.get("name", "__none__"))
        seg: dict[str, Any] = {
            # Prefixed by the set: views of one file merge by segment id, and two analyses' positional ids collide.
            "id": f"{lane_set}-{i}" if lane_set else f"s-{i}",
            "name": str(name) if name is not None else (str(group) if group is not None else f"segment {i + 1}"),
            "start": round(s, 3),
            "end": round(e, 3),
        }
        if group is not None:
            seg["group"] = str(group)
        if lane_set:
            seg["laneSet"] = lane_set
        out.append(seg)
    return out


def _scale_for(fields: dict[str, str]) -> float:
    """Multiplier that converts a declared time unit into seconds.

    Timelines are in seconds, but tools report whatever suits them — face
    appearances come back as `begin_ms`/`end_ms`. Without this, a person appearing
    at 62996 ms lands 62996 seconds down the timeline and the lane looks empty.
    """
    unit = (fields.get("units") or fields.get("unit") or "").lower()
    if unit in ("ms", "msec", "millisecond", "milliseconds"):
        return 0.001
    if unit in ("us", "usec", "microsecond", "microseconds"):
        return 0.000001
    if unit in ("frame", "frames"):
        return 0.0  # frames need an fps we do not have here; caller must not use
    scale = _num(fields.get("scale"))
    return scale if scale else 1.0


def project_lanes(data: Any, spec: str, lane_set: str = "") -> list[dict]:
    """Entities that each own a list of spans -> segments grouped by entity.

    `lanes: people[] name=name spans=appearances start=begin_ms end=end_ms units=ms`

    Needed because a flat-map (`people[].appearances[]`) reaches the spans but
    loses the parent, so there is nothing left to group by — and grouping by
    entity is the whole point of a lane view. Any tool whose results are
    "entities, each appearing at times" fits: people, speakers, tracked objects.
    """
    path, f = parse_spec(spec)
    span_key = f.get("spans", "spans")
    name_field = f.get("name", "name")
    start_field = f.get("start", "start")
    end_field = f.get("end", "end")
    scale = _scale_for(f)
    if not scale:
        return []
    out: list[dict] = []
    for ei, entity in enumerate(collect_list(data, path)):
        raw_name = entity.get(name_field)
        group = str(raw_name) if raw_name is not None else f"{ei + 1}"
        spans = entity.get(span_key)
        if isinstance(spans, dict):
            spans = [spans]
        if not isinstance(spans, list):
            continue
        for si, sp in enumerate(spans):
            if not isinstance(sp, dict):
                continue
            s = _num(sp.get(start_field))
            e = _num(sp.get(end_field))
            if s is None or e is None:
                continue
            s, e = s * scale, e * scale
            if e <= s:
                continue
            out.append({
                "id": f"{lane_set}-{ei}-{si}" if lane_set else f"l-{ei}-{si}",
                "name": group,
                "group": group,
                "start": round(s, 3),
                "end": round(e, 3),
                **({"laneSet": lane_set} if lane_set else {}),
            })
    return out


def project_boxes(data: Any, spec: str) -> list[dict]:
    """-> [{time?, x, y, w, h, label}] from normalized boxes.

    Accepts the `{l,t,w,h}` convention used by the face tools as well as
    `{x,y,w,h}` and `{Left,Top,Width,Height}`, so a new detector that follows any
    of those needs no code change.
    """
    path, f = parse_spec(spec)
    box_key = f.get("box", "box")
    out: list[dict] = []
    for i, item in enumerate(collect_list(data, path)):
        b = item.get(box_key)
        if not isinstance(b, dict):
            continue
        x = _num(b.get("l", b.get("x", b.get("Left"))))
        y = _num(b.get("t", b.get("y", b.get("Top"))))
        w = _num(b.get("w", b.get("width", b.get("Width"))))
        h = _num(b.get("h", b.get("height", b.get("Height"))))
        if None in (x, y, w, h) or w <= 0 or h <= 0:
            continue
        ann: dict[str, Any] = {"x": round(x, 6), "y": round(y, 6),
                               "w": round(w, 6), "h": round(h, 6)}
        t = _num(item.get(f.get("time", "__none__")))
        if t is not None:
            ann["time"] = round(t, 3)
        label = item.get(f.get("label", "__none__"))
        ann["label"] = str(label) if label is not None else f"{i + 1}"
        frame = item.get(f.get("frame", "__none__"))
        if frame is not None:
            # Which still the box belongs to. Detections carry the frame they were
            # found in, which is what lets boxes be drawn without a source video.
            ann["frame"] = str(frame)
        out.append(ann)
    return out


def project_boxes_by_entity(data: Any, spec: str) -> list[dict]:
    """Boxes whose label comes from the ENTITY that owns them.

    ``boxes_by: people[] label=name faces=tracks[].faces[] box=box time=timestamp frame=frame_key``

    ``project_boxes`` flat-maps ``people[].tracks[].faces[]`` and loses the parent,
    so a per-face box can only be labelled by a per-face field (gender), never by
    the person's identity. This mirrors ``project_lanes``: iterate the entities,
    read the parent ``label``, resolve the nested ``faces`` path WITHIN each entity,
    and stamp every box with that label — so a face box reads as WHO it is.
    """
    path, f = parse_spec(spec)
    label_field = f.get("label", "name")
    faces_path = f.get("faces", "faces[]")
    box_key = f.get("box", "box")
    time_field = f.get("time", "__none__")
    frame_field = f.get("frame", "__none__")
    out: list[dict] = []
    for ei, entity in enumerate(collect_list(data, path)):
        raw_label = entity.get(label_field)
        label = str(raw_label) if raw_label is not None else f"{ei + 1}"
        for face in collect_list(entity, faces_path):
            b = face.get(box_key)
            if not isinstance(b, dict):
                continue
            x = _num(b.get("l", b.get("x", b.get("Left"))))
            y = _num(b.get("t", b.get("y", b.get("Top"))))
            w = _num(b.get("w", b.get("width", b.get("Width"))))
            h = _num(b.get("h", b.get("height", b.get("Height"))))
            if None in (x, y, w, h) or w <= 0 or h <= 0:
                continue
            ann: dict[str, Any] = {"x": round(x, 6), "y": round(y, 6),
                                   "w": round(w, 6), "h": round(h, 6), "label": label}
            t = _num(face.get(time_field))
            if t is not None:
                ann["time"] = round(t, 3)
            frame = face.get(frame_field)
            if frame is not None:
                ann["frame"] = str(frame)
            out.append(ann)
    return out


def group_boxes_by_frame(boxes: list[dict]) -> dict[str, list[dict]]:
    """Bucket boxes by their `frame`, preserving order. Unframed boxes are dropped."""
    by: dict[str, list[dict]] = {}
    for b in boxes:
        key = b.get("frame")
        if not isinstance(key, str) or not key:
            continue
        by.setdefault(key, []).append(b)
    return by


def to_percent_boxes(boxes: list[dict]) -> list[dict]:
    """Normalized 0..1 boxes -> percent, the convention `image-annotated` renders.

    The projector keeps boxes normalized because that is what the source data uses
    and what the video-overlay path consumes; only this template needs percent.
    Values are clamped so a box never renders outside the image.
    """
    out: list[dict] = []
    for b in boxes:
        x, y = max(0.0, b["x"] * 100.0), max(0.0, b["y"] * 100.0)
        out.append({
            "x": round(x, 3),
            "y": round(y, 3),
            "w": round(max(0.0, min(100.0 - x, b["w"] * 100.0)), 3),
            "h": round(max(0.0, min(100.0 - y, b["h"] * 100.0)), 3),
            "label": b.get("label", ""),
        })
    return out


def project_thumbnails(data: Any, spec: str) -> list[dict]:
    """Entities that reference a stored image -> [{key, label}].

    `thumbnails: people[] key=thumbnail_key label=name`

    Lets a result show WHO was found rather than just naming them. The key is a
    stored object key; the caller matches it to an artifact URL.
    """
    path, f = parse_spec(spec)
    key_field = f.get("key", "thumbnail_key")
    label_field = f.get("label", "name")
    out: list[dict] = []
    for i, item in enumerate(collect_list(data, path)):
        key = item.get(key_field)
        if not isinstance(key, str) or not key.strip():
            continue
        label = item.get(label_field)
        out.append({"key": key, "label": str(label) if label is not None else f"{i + 1}"})
    return out


def project_rows(data: Any, spec: str) -> dict:
    """-> {columns, rows} for `data-table`."""
    path, f = parse_spec(spec)
    cols = [c.strip() for c in (f.get("cols", "") or "").split(",") if c.strip()]
    items = collect_list(data, path)
    if not items:
        return {"columns": [], "rows": []}
    if not cols:
        # No explicit columns: use the scalar fields of the first row so a table
        # still renders rather than coming back empty.
        cols = [k for k, v in items[0].items() if not isinstance(v, (dict, list))][:8]

    def flat(v: Any) -> Any:
        if isinstance(v, dict):
            for pref in ("name", "label", "id"):
                if pref in v:
                    return v[pref]
            return ", ".join(f"{k}={v[k]}" for k in list(v)[:3])
        if isinstance(v, list):
            scalars = [x for x in v if not isinstance(x, (dict, list))]
            # A short list of scalars is usually a value, not a collection:
            # age_range [28, 36] must read as a range, not as "2".
            if len(scalars) == len(v) and 0 < len(v) <= 4:
                if len(v) == 2 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v):
                    return f"{v[0]}\u2013{v[1]}"
                return ", ".join(str(x) for x in v)
            return len(v)
        return v

    rows = [{c: flat(resolve_path(it, c) if "." in c else it.get(c)) for c in cols} for it in items]
    return {
        "columns": [{"key": c, "label": c.replace("_", " ").strip().title(), "sortable": True} for c in cols],
        "rows": rows,
    }


def project_duration(data: Any, spec: str) -> Optional[float]:
    """Authoritative duration in seconds, when the sidecar states one."""
    for path in [p.strip() for p in (spec or "").split(",") if p.strip()]:
        d = _num(resolve_path(data, path))
        if d is not None and d > 0:
            return round(d, 3)
    return None


def project_summary(data: Any, spec: str) -> dict:
    """Scalar highlights worth surfacing as manifest metadata."""
    out: dict[str, Any] = {}
    for path in [p.strip() for p in (spec or "").split(",") if p.strip()]:
        v = resolve_path(data, path)
        if v is None or isinstance(v, (dict, list)):
            continue
        out[path.split(".")[-1]] = v
    return out
