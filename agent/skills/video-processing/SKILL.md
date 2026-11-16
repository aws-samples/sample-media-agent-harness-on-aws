---
name: video-processing
description: "Use for the managed M2C micro-workflows: ANALYSE or transform media that ALREADY EXISTS (trim, transcode, extract audio or frames, resize, separate speech/music/SFX stems, transcribe and diarize, detect shots and faces); SYNTHESISE speech from text via zero-shot voice cloning (text-to-speech); and retrieve those async results. NOT for combining two separate files or muxing an external audio track: process_media takes exactly ONE asset already in the M2C bucket. VIDEO and IMAGE generation are fal.ai (see media-references); MUX and multi-clip COMBINING (mux external audio, concatenate, crossfade) is ffmpeg in code_interpreter, NOT fal -- but SPEECH generation is the text-to-speech tool here. Searching or indexing media (meaning/keyword search, collections, media Q&A) is NOT here -- that is the media-search-qa skill."
---

# Video & Audio Processing

## Select and read the workflow reference first

Keep this file as the router. Before calling a micro-workflow tool, read the one
applicable author-team guide below. For a compound request, read each guide whose
output or parameters are needed; do not load unrelated references.

| User intent | Gateway submit tool | Required reference |
|---|---|---|
| Trim, clip, transcode, extract audio, resize, or select channels — and probe (technical metadata), which is answered IN-TURN, see §4a, not run as a normal async job. Frames at timestamps: §4b; N clips of one source: §4 b2; a region crop: the `crop_video` tool | `media-processing___process_media` | [Media processing](references/media_processing_agent.md) |
| Separate dialogue/speech, music, and SFX | `audio-stem___separate_audio_stems` | [Audio stem separation](references/audio_stem_agent.md) |
| Transcribe, identify speakers, create captions, or extract speaker channels | `speech-diarization___diarize_speech` | [Speech diarization](references/speech_diarization_agent.md) |
| Segment a transcript into topics/genres with authoritative timestamps, tone, sentiment, and key events (depends on a completed diarization transcript) | `conversation-analysis___analyze_conversation` | [Conversation analysis](references/conversation_analysis_agent.md) |
| Detect shots, scenes, cuts, recurrence, or produce shot/frame embeddings | `shot-segmentation___segment_shots` | [Shot segmentation](references/shot_segmentation_agent.md) |
| Enrich detected shots with per-shot descriptions, objects, setting, sentiment, on-screen text, and brands (depends on a completed shot-segmentation run; optional face-recognition run for people names, diarization transcript for dialogue context) | `shot-metadata-enrichment___enrich_shot_metadata` | [Shot metadata enrichment](references/shot_metadata_enrichment_agent.md) |
| Detect, track, or identify people; inspect screen time; manage face collections | `face-recognition___recognize_faces` and the `face-recognition___*` management tools | [Face recognition](references/face_recognition_agent.md) |
| Synthesize speech from text, clone a voice from a short reference clip, or manage the voice catalog | `text-to-speech___synthesize_speech` and `text-to-speech___register_voice`/`get_voice` | [Text-to-speech](references/tts_agent.md) |
| Make dialogue easier to hear IN PLACE (screener, review copy, accessibility listening) — one enhanced WAV, not stems | `speech-enhancement___enhance_speech` | [Speech enhancement](references/speech_enhancement_agent.md) |
| Program structure: sequences, mode detection, news rundown, break candidates (needs shot metadata) | `program-structure-analysis___analyze_program_structure` | [Program structure analysis](references/program_structure_analysis_agent.md) |
| Bind speakers + faces + on-screen text into NAMED person entities with evidence | `cast-resolution___resolve_cast` | [Cast resolution](references/cast_resolution_agent.md) |
| Audio-description script + narration for an analyzed video (render the mix via `process_media` `ad_mix`) | `audio-description___generate_ad` | [Audio description](references/audio_description_agent.md) |
| Run rendered prompts through Bedrock models (sync or half-cost batch) | `bedrock-inference___run_inference` | [Bedrock inference](references/bedrock_inference_agent.md) |

Searching indexed media by meaning or keyword, indexing media, and media
Q&A are **not** tools in this skill. That capability belongs to Projects and is
handled by the `media-search-qa` skill — do not call any `hybrid-search` or
collection-management tool from here.

These references carry the M2C client kit's (`client-kit-alpha.20261013a`) video-processing
reference bodies VERBATIM, under a harness header that wins where they differ. They provide parameter choices,
output labels, idempotency rules, progress states, and troubleshooting detail.
The live gateway tool schema is authoritative if it differs from a reference. If
an optional target (such as face recognition in some environments) is absent
from the connected catalog, state that it is unavailable; do not fabricate or
substitute a tool call.

## Harness job lifecycle overrides reference polling defaults

The M2C submit tools are asynchronous. In this application:

1. Call the applicable fully namespaced submit tool with a full
   `s3://<bucket>/<key>` `asset_s3uri`.
2. Capture the returned `job_id`/execution ARN and `workflow_id`.
3. For a single workflow, return immediately with the submitted job identifier.
   The Jobs panel monitors completion, fetches results, and triggers rendering.
4. Do **not** poll a `___get_job_status` tool in a loop unless the user explicitly
   asks to wait, or a dependent workflow step cannot start until this output exists.
   For an explicit wait, use bounded checks about every 10–30 seconds, at most 20.

   **EXCEPTION — fast ops answer IN THE TURN (no async job, no fan-out).**
   a) Technical metadata / `probe` (media inspection). For a file in the active
      project, FIRST call `project_status(name=<file name>)` when it is bound: a probed project video
      carries `technical` (container, encoder, video codec/profile/width/height/fps/
      bitRate, audio codec/channels/sampleRate) — answer from it, no job at all.
      Only when that is absent, `process_media` a probe and poll `get_job_status`
      inline to completion (bounded, ~5–15s). The status result carries
      `duration_sec`, `has_video`/`has_audio`, `is_all_intra` and `probe_s3_key` —
      it does NOT carry codec, resolution, fps or bitrate. For those, make ONE
      `read_result_files` call with the exact `s3://<bucket>/<probe_s3_key>` URIs
      of every probed file (all N in the same call) and answer from
      `probe_info.streams` (codec_name, profile, width, height, avg_frame_rate,
      bit_rate). Never rebuild or shorten a key from memory, never read the M2C
      bucket from `code_interpreter` (the sandbox cannot), and never `artifact
      import` a metadata JSON (it starts a render). When you report a result key to
      another agent or the user, write the FULL `s3://` URI — never an ellipsized
      `…/probe/metadata.json`. Metadata is NOT renderable: it is answered in the
      chat, so do not expect (or build) a render tab for it. Probing N assets is N
      bounded inline polls in ONE turn, not a plan or a fan-out. If any single probe
      exceeds the inline bound, return its job id and let the Jobs panel finish it
      rather than holding the turn open.
   b) FRAME EXTRACTION at one OR many timestamps is ONE step, in-turn — NOT a
      fan-out, NOT fire-and-forget, NOT one `process_media` per frame. Call the
      `extract_frames` tool ONCE with every timestamp (e.g. one per shot, from the shot
      list): it runs in the chat's warm sandbox with ffmpeg and the video already cached,
      ~1 s for all of them, and returns the JPEG URLs. Write your own `code_interpreter`
      cell only for what it can't do (a filter, an overlay, a grid). In that case run a
      SINGLE ffmpeg pass that grabs EVERY requested
      frame from the one source (e.g. one `-ss <t> -frames:v 1` per output in a
      single invocation, or the `select='eq(n,…)+…'`/timestamp filter), then upload
      the JPEGs with the `artifact` tool — the render hook shows them. Three
      timestamps = one script, never three dispatches and never a 3-node plan. It
      stays fast; do not create a job for it. Do NOT fold frame extraction into the
      parallel-analyses fan-out above as its own async node, and NEVER create one
      node (or one `process_media` call) per timestamp: all requested frames come
      from ONE in-turn ffmpeg pass. Frames become a `process_media` job ONLY when
      bundled as one output_group alongside other managed outputs (proxy + WAV +
      frames) — e.g. the content-moderation keyframe step.
   b2) TRIMMING N CLIPS OUT OF ONE SOURCE is ONE step too, in-turn — NOT a fan-out,
      NOT one `process_media` per clip, NOT one plan node per clip. For plain cuts call the
      `clip_video` tool ONCE with every range: a frame-accurate stream copy in the warm
      sandbox, ~0.1 s per clip. The `code_interpreter` batch below is for re-encodes
      (resize, codec, burned-in text) or joining clips. Same reasoning as
      (b): the ranges are slices of the SAME file, so a SINGLE `code_interpreter`
      invocation downloads the source ONCE and writes every clip. Measured in this
      sandbox on session 27c33c57's own 227 MB source and 7 ranges: download 2–3 s,
      all 7 stream-copy cuts 0.4 s, all 7 frame-accurate re-encodes ~30 s. The media
      work was never the cost — "clip shots 3-9" as a 7-node plan took 7m39s because
      every node paid its own dispatch, sandbox, ffmpeg provisioning, full download and
      model rounds, and the plan paid ~60 s of planning and design before any of it.
      Requirements for the batch:
        * Pair ranges to outputs BY POSITION with `scripts/trim_plan.py`
          (`build_trim_plan`), never by hand-indexing — a hand-indexed loop silently
          gave shot 8 shot 1's length (a 28.46s reel came out 27.17s).
        * `verify_trim` (ffprobe) EVERY output and report per-clip status, so one bad
          clip cannot silently take the rest with it. Return a per-clip array.
        * Choose the cut mode on purpose. `-ss` before `-i` with `-c copy` is near-instant
          but snaps to the nearest keyframe: asking for 4.958 s gave 5.042 s. When the
          marks must be frame-exact, re-encode web-safe (`-c:v libopenh264 -b:v 5M
          -pix_fmt yuv420p -c:a aac -movflags +faststart`), which gives the exact duration.
          The staged build has ONE H.264 encoder — `libopenh264` in some envs, `libx264` in
          others: read `ffmpeg -hide_banner -encoders` once and use whichever it lists.
        * Write each output to the session OUTPUT prefix given in the S3 CONTEXT section and finish with ONE
          `build_clip_gallery_manifest` for the combined tab.
      Size is NOT the reason to split (a 227 MB source downloads in seconds). ENCODE TIME
      is: re-encoding runs at roughly 0.5x realtime at 1080p in this sandbox, and a plan
      step has 600 s for EVERYTHING, model turns included. Count every encode pass — each
      clip you re-encode AND a final join/mux you re-encode — and split a step whose total
      re-encoded footage is over about 2 minutes across parallel steps, several clips per
      step — never one node per clip. Encode the pieces once with identical settings, then
      join them with `-c copy` (near-instant) and mux the audio without re-encoding the
      video: a 168 s reel re-encoded twice in one step ran out its 600 s (c7d66001).
   **`ffmpeg` and `ffprobe` are already on `PATH` in every `code_interpreter` cell** (Python
   `subprocess` and shell commands alike): the harness provisions its own static build the first time
   either runs. Call them by bare name; never download ffmpeg yourself, and do not rely on
   `imageio-ffmpeg` (it has no ffprobe). Only if a cell reports them missing: the build is in the
   workspace (skills) bucket (`SKILLS_S3_BUCKET`), key = `tools/ffmpeg/ffmpeg-release-arm64-static.tar.xz`,
   read by KEY with boto3. There is NO public or CDN link to it — `https://<artifacts-domain>/tools/ffmpeg/…`
   answers **403 by design**, so do not try URLs or list the prefix.
   **Reading source media in `code_interpreter`:** our artifacts (`https://<artifacts-domain>/artifacts/…`)
   are NOT public — an unsigned download can 403. Either read the object by KEY with boto3 from
   the workspace (skills) bucket named in the S3 CONTEXT guidance (`SKILLS_S3_BUCKET`; key =
   the URL path, `artifacts/…`), or use the artifact URL as-is: CDN artifact URLs in your
   `code_interpreter` code/commands and in `http_request` are swapped for presigned URLs
   automatically. Inside the sandbox those presigned URLs are GET-only, so fetch with GET
   (`curl -r 0-0 -D -` to see headers), not `curl -I`. The M2C proxy bucket is NOT readable from
   the sandbox: use its mirror under `artifacts/` (the results tools mirror outputs there), or
   copy the object in first with `artifact(operation="import", source_uri="s3://…")`. Never submit an
   exploratory `process_media` job just to get standalone frames — extract them here (§4b).
   Everything heavier — trim, transcode, extract-audio, stems, shots, faces,
   diarize, transcribe — stays async per (3).
5. On success, choose files from `outputs[]` by stable `label`, never by parsing
   filenames. `media-outputs___get_download_url` is for handing the USER an HTTP
   link they asked for — not a way to read a result yourself. For a plan, the
   results are already resolved on `get_plan_results`; presigning and fetching
   them again is what turned one retrieval into ~40 model steps, and it renders
   inputs as though they were results. A prefix URI cannot be presigned.
6. Rendering is automatic. Do not call or invent a render tool.

Never pass `output_s3_prefix` or `output_bucket`; each service derives its own
output location. Identical parameters are absorbed into the existing run; pass a NEW
`idempotency_token` only when the user intentionally requests a fresh run of otherwise
identical parameters. (There is no `uuid` parameter: the tools reject it.)

## Route single versus compound workflows

Use `dispatch_tool` ONLY when the whole request is a SINGLE gateway submit tool
call.

Use `plan_and_execute_tool` whenever the request needs MORE THAN ONE submit tool
call — whether the calls are SEQUENTIAL (one workflow's completed output becomes
the input of the next) OR INDEPENDENT/PARALLEL (several analyses of the same
asset with no dependency between them, e.g. "diarize speakers, segment shots,
and detect people"). Do NOT decompose one request into several separate
`dispatch_tool` calls, even when each tool could be invoked independently: a plan
keeps the request as ONE tracked unit of work — one job to follow, one completion
signal, one dependency view, and one place to collect every result — while
separate dispatches lose all of that and read to the user as unrelated jobs.
Express independent steps as plan nodes with NO `depends_on` (they run in
parallel); give a dependent step a `depends_on` on the upstream node whose
output it consumes. Running the SAME operation over multiple inputs is itself ONE
fan-out plan (one node per input, no `depends_on`), never repeated separate
dispatches.

**Parallel analyses of ONE asset = ONE plan, N parallel nodes.** When the user
wants several INDEPENDENT analyses of the same asset — e.g. "segment shots,
diarize speech, and capture frames for indexing" — this is a SINGLE
`plan_and_execute_tool` call with one node per analysis and NO `depends_on`
between them (they run in parallel). It is NOT three separate `dispatch_tool`
calls: those read to the user as three unrelated jobs and lose the single
completion signal.

    shots   ─┐
    diarize ─┼─ (no depends_on; all parallel)  → one plan, one tracked job
    frames  ─┘

Add `depends_on` ONLY where one node consumes another's completed output
(e.g. trim → stems, or diarize → index the transcript).

A single trim (`mark_in_out`) goes to `process_media`, which writes a 960×540 proxy unless told the size:
pass the source's `width` and `height` (from `project_status` `technical` for a project file, or a §4a
metadata probe) so the clip keeps its resolution, each an even number.

A time window alone does NOT force a plan or a preliminary trim: a single tool
that supports `mark_in_out` still maps to ONE `dispatch_tool` call (see below).

### Time-window support

Do not apply the old blanket rule that every time range requires a preliminary
trim. Current author guidance documents:

- `media-processing___process_media`: supports `mark_in_out` directly.
- `speech-diarization___diarize_speech`: supports `mark_in_out` directly; the
  window must exceed 10 seconds. That limit applies ONLY to a `mark_in_out`
  window, never to a whole file: a short clip diarizes fine. When a diarization
  job fails, report its actual error: `No VAD turns found` (or `No high-quality
  embeddings found`) means the media has no detectable speech (music, effects,
  silence), not that it is too short.
- `face-recognition___recognize_faces`: supports `mark_in_out` directly.
- `audio-stem___separate_audio_stems`: no documented time-window parameter;
  first trim with `process_media`, then pass the completed clip URI.
- `shot-segmentation___segment_shots`: no documented time-window parameter;
  first trim with `process_media`, then pass the completed clip URI.

Examples:

- “Transcribe the first minute” → one diarization job with
  `mark_in_out: [0, 60]`.
- “Identify people between 2:00 and 3:00” → one face-recognition job with
  `mark_in_out: [120, 180]`.
- “Separate stems from the first minute” → process-media trim, then stem
  separation after the clip output is available.
- “Extract speech and diarize it” → stem separation with only the speech stem,
  select the `speech` output label, then diarize that URI.
- “Make a proxy, WAV, and frame captures” → one media-processing job with
  multiple `output_groups`, not three separate jobs.

For a dependent chain, select each upstream result by its documented output
label and pass that entry's full `uri` to the next workflow. Bounded status
checks are allowed here because completion is a dependency, not for generic job
monitoring. Finish the dependent submissions before returning when the plan can
continue within those bounds; otherwise report the completed and pending steps.

## Face-recognition safety defaults

Face collection writes persist biometric identifiers. For exploratory requests
such as “who appears in this video,” prefer the no-persistence posture from the
face-recognition reference (`collection_id: ""`) unless the user explicitly asks
to match against or update a collection. Do not create, index, rename, delete
faces, or delete a collection without clear user intent. Before collection
mutations, read the management and guardrail section in
[the face-recognition reference](references/face_recognition_agent.md).

## Combining media (mux, stitch, crossfade, transition) → ffmpeg in code_interpreter

`media-processing___process_media` and the gateway compose endpoint operate on a
SINGLE video track — compose rejects two or more with
`400 "Multiple video tracks are not supported"`. Route by what you need:

**Web-playable output (REQUIRED for every MP4 you write for playback).** A browser
`<video>` and the `/api/artifact` proxy read the file head-first and need the `moov`
atom at the FRONT; ffmpeg writes it at the END by default, producing a valid MP4 that
still WON'T PLAY in a browser (blank/black, no error, no matter the codec). So EVERY MP4
you write MUST include `-movflags +faststart`:
- Re-encode paths (xfade/crossfade, overlay/hstack/vstack, `amix`/audio mix, any
  filtergraph) also encode web-safe: `-c:v libopenh264 -b:v 5M -pix_fmt yuv420p -c:a aac -movflags +faststart`
  (`yuv420p` matters — `yuv444p`/High-4:4:4 won't decode in browsers). **The sandbox's ffmpeg is
  the harness's LGPL build: its H.264 encoder is `libopenh264`, which is bitrate-driven (`-b:v`;
  no `-crf`/`-preset`). `libx264` is NOT available and fails with `Unknown encoder 'libx264'`.
  Do NOT set `codec` on `process_media` — omit it; the M2C workflow's default is `libopenh264` too.**
- Stream-copy paths (`-c copy` mux/concat): STILL pass `-movflags +faststart` — it only
  relocates the `moov` atom to the front (no re-encode, still lossless and fast).

- **EVERY encoded dimension MUST BE EVEN, and geometry is PROBED BEFORE the first encode.**
  H.264 4:2:0 chroma subsampling requires even width and height, so ffmpeg rounds an odd one and
  the output silently comes out a pixel or two off what you asked for. Asking for an odd panel
  width is therefore UNSATISFIABLE, not a bug to retry: a side-by-side specified as "2710x768,
  1355px per panel" produced 2708x768 twice in a row, and discovering that cost THREE full
  encodes of the same 28.7s video — 225s, 65% of the node (session 764a71c0). So:
    * `ffprobe` every input's real `width`/`height` FIRST, then compute the canvas from those
      numbers. Never encode against a geometry you were handed without probing it.
    * Round each panel UP to an even number (1356, not 1355), hstack, then `crop` to the exact
      target if a precise total matters. `pad` with `(ow-iw)/2` re-introduces an odd offset.
    * Verify geometry on a `-frames:v 1` TEST ENCODE (under a second), not on the full render.
      Only then encode the whole thing. One cheap probe replaces a 75s round trip.

- **Trimming N clips to N per-shot targets → pair by POSITION, never hand-index.** Import
  `scripts/trim_plan.py` and use `build_trim_plan(clips, targets)` so clip[i] always trims to
  target[i] (mismatched counts raise up front). A hand-written loop that indexes `targets`
  separately is how shot 8 got shot 1's length and a 28.46s reel silently became 27.17s. After each
  `ffmpeg -t <target>`, `ffprobe` the output and call `verify_trim(i, target, actual)` so an
  over/under-trim FAILS LOUDLY instead of shipping a short reel.
  **Shots that go back over the ORIGINAL audio** (a recast or restyle of a scene, reassembled with
  its dialogue): run `scripts/reassemble_shots.py spec.json out.mp4` (its docstring has the spec) — never a
  hand-written loop. Each shot gets exactly the source's frames from its start to the NEXT shot's start (detected
  shots leave gaps, so per-shot lengths add up short and the dialogue drifts: c7d66001), every piece is encoded at
  the source's frame rate and size (a replacement at fal's 24 fps made 8e2acba8's reel 4033 frames for a 5044-frame
  scene), and the script verifies the frame count and the A/V sync before it returns.

- Video + external audio track (mux) → ffmpeg in `code_interpreter`. Download both
  URLs, remux with `-c copy -movflags +faststart` (stream copy, lossless, fast; faststart
  relocates the moov atom for browser playback) and write to the session
  output prefix. fal's `merge-audio-video` does NOT work — do not route mux there.
  M2C `process_media` also cannot mux (it operates on a single asset already in its
  own bucket, not two external URLs). ffmpeg in `code_interpreter` is the only mux path.
- Plain hard-cut concatenation → ffmpeg in `code_interpreter` (the `concat` demuxer with
  `-c copy -movflags +faststart` for same-codec clips — lossless and fast, no re-encode; write to the session
  output prefix). Do NOT route it to `fal-ai/ffmpeg-api/merge-videos`: keep ALL combining on
  the deterministic `code_interpreter` path (the `media-references` skill defers here for it).
- Crossfade, transition, overlay, or side-by-side/picture-in-picture → ffmpeg in
  `code_interpreter` in this skill (compose rejects multi-track; merge-videos only
  hard-cuts; M2C `quick_edit` can cut, fade and crossfade sources it can read, but this
  skill does its combining here). Do it with ffmpeg in `code_interpreter`: the `xfade` filter for a
  crossfade/transition, `overlay`/`hstack`/`vstack` for compositing. This holds in
  plan/workflow mode too: do NOT let a plan route a crossfade/transition to a
  `fal-ai/ffmpeg-api/compose` node — the ffmpeg-in-`code_interpreter` path is authoritative
  and the `media-references` skill defers here for transitions. ("compose rejects multi-track"
  above is the M2C gateway compose; fal's compose is not the crossfade path here either.)
- Crop to an ARBITRARY bounding box `(x, y, w, h)` → the **`crop_video` tool** (see
  "Crop to a bounding box" below), NOT a code_interpreter cell. `process_media`'s
  `crop_x`/`crop_y` is a TOP-LEFT offset + scale only and CANNOT express an arbitrary
  box, so a region crop is a first-class typed tool, not the ffmpeg escape hatch.

Caveat: the binding cost in `code_interpreter` is ENCODE time, not transfer size. Measured
in this sandbox: a 227 MB source downloads in 2–3 s, while a 1080p re-encode runs near 0.5x
realtime (the side-by-side composite in session 764a71c0 ran at 0.458x). Stream-copy remux
(mux, concat, trim with `-c copy`) is near-instant at any size. For xfade/compositing of
long or high-resolution clips, downscale FIRST (in the same sandbox, or a managed
`process_media` job when the encode itself would take many minutes), and keep the total
re-encoded footage in one cell to a few minutes.

Content moderation is a SECOND reason to keep stitching here, not just the multi-track
limit: fal re-moderates every clip passed to its ffmpeg/compose endpoints, so stitching
ALREADY-GENERATED shots into a montage THROUGH fal can fail with `content_policy_violation`
on benign footage — and those denials spike when fal tightens its thresholds. The
`code_interpreter` xfade/concat path applies no moderation to already-approved media, so
prefer it for stitching generated shots, and never fall back to a fal/gateway compose to
"get past" a moderation denial. (A moderation denial on the SHOT GENERATION step is a
different case — rephrase-and-retry once; see the `media-references` skill.)

## Managed tools before custom ffmpeg (single-track operations)

Use `media-processing___process_media` for every SINGLE-track operation it can
express: trimming, transcoding, audio extraction, frame capture, resize, crop,
and channel selection. Use ffmpeg in `code_interpreter` for a genuine gap: mux
(video + external audio), a crossfade/transition or compositing (above), or an
unsupported exotic filter.

**Frame capture, reconciled:** a standalone request for a handful of frames at
given timestamps is ONE in-turn `code_interpreter` ffmpeg pass (see §4b) — NOT a
`process_media` job. Use `process_media` frame capture only when frames are one of
several `output_groups` in a larger managed job (e.g. proxy + WAV + frames
together). Standalone = `code_interpreter`; bundled with other managed outputs =
`process_media`. Never submit a `process_media` job to "try" frame capture or to
locate the source first — if the sandbox cannot read the source, fix the read
(see "Reading source media" in §4), do not route around it with a job whose
frames you then have to delete.

For a custom ffmpeg fallback:

- Complete download, processing, and upload in one `code_interpreter` call.
- Get ffmpeg from `imageio_ffmpeg.get_ffmpeg_exe()`; never search the filesystem.
- Write the result DIRECTLY to the session output prefix named in the S3 CONTEXT
  guidance (`<that prefix><filename>`; it may be `artifacts/<session>/output/` or
  `projects/<project>/sessions/<session>/output/` — copy it exactly), with a uniqueness token in
  the filename and the correct `ContentType`. Never write a flat, session-less
  `artifacts/<filename>` key, and never overwrite source or prior output. That
  path is served as-is, so `artifact(operation="download")` (or `adopt`) is only
  needed to obtain the URL.
- Use stream copy where safe for lossless, fast concatenation or remuxing — and ALWAYS
  add `-movflags +faststart` so the output plays in a browser (moov atom at the front).
  Any re-encode must be web-safe: `-c:v libopenh264 -b:v 5M -pix_fmt yuv420p -c:a aac -movflags +faststart`.

## Crop to a bounding box

An arbitrary region crop `(x, y, w, h)` is NOT expressible by `process_media`
(its `crop_x`/`crop_y` is a top-left offset + scale only), so call the **`crop_video`
tool** — a first-class typed tool (NOT a hand-authored `code_interpreter` cell). It
applies ffmpeg's `crop` filter with a web-safe re-encode and returns a NEW clip (the
source is never modified) at a permanent CloudFront URL that auto-renders.

Why a tool and not a script you run: a crop is deterministic, but authoring the ffmpeg
inline let the model reverse the box — in efa3569d a 9:16 crop shipped as a 540×540
square because the aspect ratio `9/16` was used as the width fraction. `crop_video` is
the same footing as `process_media`'s `mark_in_out` trim: pass the numbers, nothing to
recompute.

- **Where the box comes from (one funnel):**
  - The render UI's crop selector emits a **normalized** box (fractions of the
    frame, 0..1) → pass it as `box="x,y,w,h"`. **The emitted box IS the final crop —
    the aspect the user picked (9:16, 1:1, …) is ALREADY baked into `w`/`h`. Pass those
    four numbers VERBATIM. NEVER recompute the box from the aspect-ratio label:**
    substituting the ratio for the width fraction (e.g. treating "9:16" as
    `w=9/16=0.5625`) crops a square in the wrong place — the real efa3569d bug. The
    crop prompt carries a copy-ready `--box x,y,w,h` token; pass exactly those values.
  - "Crop to \<person\>" / an analyzed entity → read that entity's normalized boxes
    from the detection sidecar (`annotations[]` / face-clustering), take the
    **union box over the requested time range** (a static crop that always contains
    the subject), and pass `box=`. Disambiguate by the labeled `name`/`entityId`.
  - Explicit pixel numbers in the request → pass `box_px="x,y,w,h"` (pass exactly ONE
    of `box` or `box_px`).
  - "Crop to 1:1 / 9:16 / vertical / square" with NO box → `aspect="1:1"` (or `"9:16"`, …): the
    largest CENTERED crop of that aspect, sized from the source's PROBED dimensions. Never compute a
    box from a resolution you assumed (a guessed 1920x1080 turned "1:1" into 640x720 on a 1280x720 file).
  - "Crop the segment where X appears" (temporal + spatial) → add `trim="in,out"`
    (seconds) to fuse the trim into the same pass, or trim first as a separate op.
- **Call the tool — do NOT hand-roll the ffmpeg graph inline.** Hand-rolling an inline
  `crop=`/`ffprobe` cell is exactly where the box got recomputed and reversed in
  efa3569d. `crop_video(source_url=<the [source:] url>, box="x,y,w,h")` probes the real
  source dimensions, converts to integer pixels, snaps to EVEN dimensions (yuv420p),
  clamps within the frame, keeps audio, self-provisions ffmpeg+ffprobe, writes to the
  session output prefix, and returns the permanent CloudFront URL. Nothing to `adopt`;
  the URL is ready.
- **Batch** (the gallery's "Crop all"): the prompt pairs each `label` → its
  `[sources:]` url → its own `--box`; call `crop_video` once per clip with that clip's
  url + box, then recombine the returned urls into ONE `build_clip_gallery_manifest`
  gallery.
- **Fallback only** if the `crop_video` tool is somehow unavailable: the same math ships
  as the bundled `scripts/crop_video.py` you may run in `code_interpreter`
  (`… python scripts/crop_video.py <src> <out.mp4> --box x,y,w,h --execute`), then
  `artifact(operation="adopt")`. Prefer the tool.
- **Two errors end this path.** If `crop_video` returns an error twice for the same
  request, stop: run that ONE documented fallback once, and if it fails too, report the
  tool's error with the source url and box you passed. Do NOT escalate through other
  mechanisms — no hand-rolled inline `crop=` cell, no fetching an ffmpeg build to repair a
  broken tool (the provisioning in §4b is for work the sandbox is meant to do, not a
  workaround), and no `process_media` job, which cannot express an arbitrary box anyway.
  Measured: that escalation cost 220s and 26 steps in one turn and still produced no crop.

Bundled: `scripts/crop_video.py` — the fallback implementation of the same probe →
normalized/pixel box → even-snapped, clamped `crop=w:h:x:y` + `-c:v libopenh264 -b:v <source
bitrate> -pix_fmt yuv420p -c:a aac -movflags +faststart` (the `crop_video` tool reuses these helpers).

## Never

- Never fabricate durations, transcripts, identities, shots, output URIs, or
  completion states.
- Never choose outputs by guessed filenames when a stable output label exists.
- Never retry a deterministic failure blindly; explain it and adjust only when
  the reference or returned error supports a correction. After two errors from the same
  tool for one request, take its documented fallback once or stop and report the error —
  never go looking for a different engine to do that tool's job.
- Never persist or delete face data without explicit user intent.
