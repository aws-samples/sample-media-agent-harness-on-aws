---
name: render-scenes
description: Renders a shot metadata enrichment run's scenes.json as a Scenes band on the video's timeline, drawn like the Shots band (numbered blocks, a gap at each boundary). Used when an enrichment run (shot-metadata-enrichment) finishes.
handles: application/json, .json
detect: scenes[].start_shot_id
template: composite-timeline
tool: shot-metadata-enrichment
deliverable: scenes.json
markers: scenes[] time=start_time end=end_time label=scene_id category=setting note=boundary_reason
lane_set: scenes
summary: shots_total, pairing_repair_rate
---

# Render: scenes (catalog declaration)

> **Not an agent skill.** The deterministic renderer reads the **frontmatter above** to build the manifest.
> This body is human reference only.

Projects `scenes.json` from **Shot Metadata Enrichment** onto a **Scenes** band drawn like the Shots band: each
scene is a numbered block from `start_time` to `end_time` (seconds); hover shows its `setting` and why it starts
(`boundary_reason`). Markers, not segments: scenes tile the film like shots do, and segments belong to the
transcript list. The scenes are assembled in code from
the per-shot `starts_new_scene` flags (the repaired, assembly-grade value), so they match the scenes the agent
lists. On interleaved genres (news, magazine) they are shot groups, not final scene structure.

The enrichment run is handed a shot-segmentation RUN, not the video; the render recovers the video through that
upstream run's job (`hooks.provider.upstream_job_ids`), so the lane lands in the video's tab beside its shots and
speakers. `lane_set: scenes` means a newer scenes view (a re-run, or the agent's `update_timeline(..., "scenes")`)
replaces this one and leaves every other lane alone.

Shape:
```json
{ "scenes": [ { "scene_id": 1, "start_shot_id": 1, "end_shot_id": 8, "start_time": 1.001, "end_time": 29.696,
                "boundary_reason": "location_change", "setting": "street_sidewalk" } ],
  "shots_total": 94, "pairing_repair_rate": 0.0 }
```
