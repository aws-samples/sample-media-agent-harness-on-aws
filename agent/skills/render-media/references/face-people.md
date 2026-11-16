---
name: render-face-people
description: "Renders face/people detection JSON as a timeline with one lane per person, their appearance spans and thumbnails. Use when the question is WHO appears and WHEN. NOT for shot boundaries (see render-shot-segmentation) and NOT for speaker turns (see render-speaker-diarization)."
handles: application/json, .json
detect: stats.num_people
template: composite-timeline
deliverable: facemetadata.json
lanes: people[] name=name spans=appearances start=begin_ms end=end_ms units=ms
lane_set: people
thumbnails: people[] key=thumbnail_key label=name
rows: people[] cols=name,gender,age_range,total_screen_time_sec,confidence,name_source,num_tracks
duration: duration_sec
summary: stats.num_people, stats.num_tracks, stats.faces_detected, stats.frames_analyzed
---

# Render: identified people (catalog declaration)

> **Not an agent skill.** The deterministic renderer reads the **frontmatter above**
> (`detect`/`template`/`lanes`/`thumbnails`/`rows`/`duration`) to build the manifest. This body is
> human reference only.

Projects `facemetadata.json` — the canonical face output — into **per-person appearance lanes**
plus a **who-was-found table** and a thumbnail gallery.

**This is the no-geometry FALLBACK face view.** It is selected when only `facemetadata.json`
(people + appearances, no box geometry) is present. When `facegroup.json` / `face_tracks.json`
carry box geometry, `render-face-recognition` wins instead — the content selector ranks a box
overlay first (`score = (has_boxes, …)`) — and it `subsumes` this view.

Two shape rules the frontmatter encodes:
- **Appearances are milliseconds** (`units=ms`): `62996` → `62.996s`.
- **Spans belong to the person, not the file** (`lanes` names the entity and its span list), so
  each person keeps their own row.

`duration_sec` is authoritative (e.g. 300.1s vs the 600s fallback).
