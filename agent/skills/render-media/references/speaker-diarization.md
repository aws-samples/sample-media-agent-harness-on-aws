---
name: render-speaker-diarization
description: Renders speaker diarization JSON as a timeline with one lane per speaker and spans for their speech segments (from final_transcript.json sentences[] grouped by speaker_id). Used when a media analysis result contains diarization/transcript output.
handles: application/json, .json
detect: sentences
template: composite-timeline
tool: speech-diarization
deliverable: final_transcript.json, speaker_manifest.json, global_speakers.json
segments: sentences[] start=start end=end group=speaker_id name=text
lane_set: speakers
summary: num_global_speakers, num_sentences, language
---

# Render: speaker diarization (catalog declaration)

> **Not an agent skill.** The deterministic renderer reads the **frontmatter above**
> (`detect`/`template`/`segments`/`summary`) to build the manifest. This body is human reference
> only.

Projects a diarization result onto **per-speaker lanes** — one row per speaker, each turn a
segment on that row (`group=speaker_id`). No duration is declared on purpose: `total_speech_sec`
is the SUM of speaking turns, not the media length, so the audio track's measured duration is
authoritative; declaring it would compress the timeline and misplace every turn.

When the source video is known it is the FIRST track, beside any per-speaker audio the job
emitted — the audio-only trim used for stems does not apply to a diarization view.

Shape:
```json
{ "num_global_speakers": 2, "num_sentences_total": 37,
  "diarization": [ { "start": 26.978, "end": 29.318, "speaker": "SPEAKER_00" } ] }
```
