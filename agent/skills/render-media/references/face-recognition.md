---
name: render-face-recognition
description: "Renders face recognition JSON as bounding BOXES drawn over the source video (from facegroup.json people[].tracks[].faces[].box), plus a per-identity table and appearance spans. Use when the question is WHERE in the frame a face is, or to see detections on the picture. NOT for shot boundaries (see render-shot-segmentation) and NOT for speaker turns (see render-speaker-diarization)."
handles: application/json, .json
detect: people[].tracks
template: nle-timeline
tool: face-recognition
deliverable: facegroup.json
subsumes: facemetadata.json, face_clusters.json, identity_clusters.json, face_tracks.json
boxes_by: people[] label=name faces=tracks[].faces[] box=box time=timestamp frame=frame_key
lanes: people[] name=name spans=appearances start=begin_ms end=end_ms units=ms
lane_set: people
rows: people[] cols=name,gender,name_source,confidence,total_screen_time_sec,num_tracks
summary: stats.num_people, stats.num_tracks, stats.faces_detected
---

# Render: face recognition (catalog declaration)

> **Not an agent skill.** The deterministic renderer reads the **frontmatter above**
> (`detect`/`template`/`boxes_by`/`lanes`/`rows`/`subsumes`/`summary`) to build the manifest. This
> body is human reference only.

The **primary** face view: projects face analysis onto the source video/still as **timestamped
bounding boxes**, plus an appearance timeline and a table of identified people.

**Selection (current behaviour):** the content selector ranks a box overlay first —
`score = (has_boxes, num_projections, detect_matches)` — so whenever box geometry is present this
view wins, and it `subsumes` `facemetadata.json`. `render-face-people` is the no-geometry fallback
(only `facemetadata.json`, no boxes). (An earlier version of this note said the box path "is not
chosen"; that predated the `has_boxes` boost and is no longer true.)

## Shapes this handles

`face_tracks.json` — boxes are normalized 0..1 as `{l,t,w,h}` (also seen as `{x,y,w,h}` /
`{Left,Top,Width,Height}`; `project_boxes` accepts all three), one per detection:
```json
{ "tracks": [ { "track_id": 0, "gender": "Male",
                "faces": [ { "timestamp": 30.03, "confidence": 99.95,
                             "box": {"l":0.30,"t":0.17,"w":0.14,"h":0.36} } ] } ] }
```
`facegroup.json` carries the same boxes per person; `identity_clusters.json` adds the celebrity
match + confidence. `facemetadata.json` is the IDENTITY summary (people[] + appearances +
thumbnails) with no geometry — which is exactly why the `render-face-people` fallback exists.

## Where the boxes are drawn
Each face names its `frame_key`, and the extracted frames ship beside the analysis, so boxes draw
on a real still (the frame with the most detections, others as a gallery). Given a source video,
they overlay playback instead, using each face's `timestamp` — the source video always wins over
the stills when it is known.
