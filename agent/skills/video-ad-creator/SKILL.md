---
name: video-ad-creator
description: "Create a short branded video AD (6-60 s; 16:9, 9:16, 1:1 or 4:5) for any business, product or offer from a conversation. Interview the user into a brand brief, propose an editable ad spec (a preset segment plan the user can change: presenter-led, product showcase, promo/offer), take whatever assets it needs (logo, product, place, screenshot or people photos), then generate narration (ElevenLabs), an optional lip-synced presenter (synthetic or the user's own photo), image-to-video motion clips, an optional music bed, and assemble one ad per aspect ratio with an end card. Use when the user wants an ad, commercial, promo, launch or social video. NOT for a multi-segment character monologue reel (agent-olive), editing an existing video, or analysing media."
---

# Video Ad Creator

Turn a short chat into finished video ads. The user defines the ad; this skill turns their
answers into an **ad spec** (format, brand, presenter, assets, segments, music, end card), shows it
for approval, and then runs one fixed engine over whatever the spec says. Every model call uses
tools the harness already has: fal image/video/music generation (`media-references`), ElevenLabs
TTS on fal, MiniMax H3 lip-sync on fal, and ffmpeg in `code_interpreter` (`video-processing` is
authoritative for all combining).

Write for any business. Never reuse wording, products, people or music from the examples below;
they show the shape only. The presenter is "the presenter" (no assumed gender) until one is chosen.

## The ad spec (what the user approves)

Build it from the brief, show it as a readable table (not raw JSON), let the user edit any field,
and save the approved spec as `ad-spec.json` in the session output location.

```
format:    {aspects: ["16:9"], length_s: 30, language: "en", loudness: "web" | "ctv"}
brand:     {name, url, socials: [{network, handle}], logo: <asset id>, colors: "auto" | [hex]}
presenter: {mode: "synthetic" | "user_photo" | "none", description, voice}
assets:    [{id, role, file, note}]          # role: logo | product | place | screenshot | people | graphic
segments:  [{id, purpose, visual, speaker, assets: [ids], line, max_words}]
music:     {mode: "generate" | "user_file" | "none", mood | file}
end_card:  {cta, url, qr: true|false, socials: true|false, sign_off, offer_code}
```

**Visual types** (one per segment):

| `visual` | Picture |
|---|---|
| `presenter_on_brand` | Brand-colour background, logo, presenter cut-out |
| `presenter_over_clip` | The segment's asset clip(s) full-frame, presenter cut-out inset |
| `clip` | The segment's asset clip(s) full-frame, no presenter |
| `text_card` | Brand-colour background, one large line of text (an offer, a date, a price the user gave), small logo |
| `end_card` | Closing card from `end_card` |

**Speaker types:** `on_camera` (lip-synced; only with a `presenter_*` visual), `voice_over`
(narration, no lip-sync), `none` (music only). With `presenter.mode: "none"`, every segment is
`voice_over` or `none`, and no lip-sync runs.

**Word budget.** The voice speaks about 2.6 words/s. Give each segment `max_words` =
round(segment seconds × 2.6) so the spoken total fits `length_s` minus ~1 s per segment of pauses
and dissolves. Every `on_camera` line needs at LEAST 14 words: the lip-sync model rejects audio
under 5 s. If a short ad (≤ 15 s) cannot afford that, make its presenter segment `voice_over`.

## Presets (starting points; the user may add, drop, reorder or retype any segment)

**Presenter-led** (local business, service, venue; 25–35 s):
hook `presenter_on_brand`/on_camera → place or experience `presenter_over_clip`/on_camera →
product or proof `clip`/voice_over → audience + invitation `presenter_on_brand`/on_camera →
`end_card`/voice_over.

**Product showcase** (e-commerce, consumer goods, apps; 15–30 s; presenter `none` by default):
hook `clip`/voice_over (hero product) → feature `clip`/voice_over (detail or second product,
1–2 assets) → benefit or proof `clip` or `text_card`/voice_over → `end_card`/voice_over.

**Promo / offer** (sale, event, launch, limited time; 10–20 s):
hook `clip` or `presenter_on_brand` → offer `text_card`/voice_over (the exact offer and its end
date, as the user gave them) → product or place `clip`/voice_over → `end_card` with
`offer_code`/voice_over.

Pick the preset from the ad goal; say why in one sentence and offer the other two.

Shape-only examples (do not reuse their words):
- Presenter-led for a family restaurant, 16:9, 30 s: hook names the brand and speaks to the
  audience; place segment shows two room photos; proof segment shows the signature dish; invitation
  names who it is for; end card says how to book.
- Product showcase for a watch brand, 9:16 + 1:1, 15 s, no presenter: hero watch push-in; strap and
  dial detail; a `text_card` with the user's stated guarantee; end card with the shop URL.

## Formats

| Aspect | Canvas | Typical use |
|---|---|---|
| 16:9 | 1920×1080 | YouTube, web, connected TV |
| 9:16 | 1080×1920 | Stories, Reels, TikTok, Shorts |
| 1:1 | 1080×1080 | Feed |
| 4:5 | 1080×1350 | Feed (portrait) |

All aspects in `format.aspects` are assembled from ONE generation pass: the presenter plate is
always 1920×1080 and every presenter clip is cropped to the presenter before placing, and asset
clips are cover-cropped per canvas. Loudness: `web` (social, YouTube, web) -14 LUFS, `ctv` -24.

## Asset roles and motion

| Role | Becomes | Motion |
|---|---|---|
| `logo` | Used as-is on brand and end cards. Never regenerated, restyled, animated by a model, or cropped. | none |
| `product` | `minimax/h3-max/image-to-video` clip | "slow camera push-in, the subject stays exactly as in the photo, soft light, shallow depth of field" (+ food: "gentle steam"; fashion: "subtle fabric movement") |
| `place` | `minimax/h3-max/image-to-video` clip | "slow smooth camera glide, the scene stays exactly as in the photo, natural light, no people talking, cinematic" |
| `people` | `minimax/h3-max/image-to-video` clip | "subtle natural movement, gentle camera drift, faces unchanged"; only with the user's confirmation that they may use the photo |
| `screenshot`, `graphic` | NO model call: ffmpeg pan/zoom on the still (`zoompan`), so text stays sharp | slow 8–12% zoom |

Motion prompts describe CAMERA MOVEMENT only. Never name or describe what the photo shows
("the signature dish", "a red bike"): the video model then repaints the subject to match the
words (a plated dish came back as a different dish because the prompt named one). The photo is
the subject; the prompt is the move.

One clip per asset, NO `end_image_url`: a first→last-frame clip between two different photos
morphs through a blurred "wall" for seconds; two clips joined by a dissolve do not. A segment with
k assets shows them in order, each for segment length ÷ k, joined by 0.4 s dissolves.

## Sandbox I/O (every `code_interpreter` call in this skill)

These rules apply to the key plate and asset copies (Phase 2), the voice probe and padding
(Phase 3), the review frame check (Phase 5) and assembly (Phase 6). Each one was a failed call in a
test run.

- Paths: inputs go to `/tmp/ad/in/`, intermediates to `/tmp/ad/work/`, outputs to `/tmp/ad/out/`.
  Never write a file to a path you also read (a music source and a music intermediate both named
  `music.wav` failed with "Error opening output file ... Invalid argument").
- Download every input ONCE. A URL on the artifacts CDN (`https://<artifacts-domain>/projects/...`
  or `/artifacts/...`) is NOT public: urllib gets HTTP 403. Read it with boto3 by KEY (the URL path
  without the leading `/`) from the workspace bucket named in the S3 CONTEXT. Only `fal.media` URLs
  are fetched with urllib.
- ffmpeg + ffprobe (the sandbox has neither; `imageio_ffmpeg` has no ffprobe). Never `list_objects`
  to find it (AccessDenied for the sandbox role); use exactly this key:
  ```
  s3.download_file(BUCKET, 'tools/ffmpeg/ffmpeg-release-arm64-static.tar.xz', '/tmp/ff.tar.xz')
  tarfile.open('/tmp/ff.tar.xz').extractall('/tmp/ffm', filter='data')
  FD = os.path.dirname(glob.glob('/tmp/ffm/*/ffmpeg')[0]); os.environ['PATH'] = FD + ':' + os.environ['PATH']
  ```
  It has libx264: encode with `-c:v libx264 -preset veryfast -crf 20 -pix_fmt yuv420p -r 30`.
- Upload outputs with boto3 `put_object` to the session output location named in the S3 CONTEXT
  (`projects/<pid>/sessions/<sid>/output/` or `artifacts/<sid>/output/`, depending on the env); it
  is the only place the sandbox may write.
- **Model inputs must be session-output URLs.** fal can download a file under the session output
  location, but NOT a project-asset URL (`https://<artifacts-domain>/projects/<pid>/image:<id>/…`,
  what an upload or attachment resolves to) in every env: prod 1.1.1 does not resolve those for the
  gateway, and every model that got one failed with `file_download_error (input.image_url)`. So every
  user asset is copied into the session output first (Phase 2 step 4) as `src-<asset id>.<ext>`, and
  only those copies are passed to models and to assembly.

## Phases and stops

The harness has no in-plan pause. Each **stop** ends the turn; continue in the next turn. Only an
explicit statement in THIS conversation ("just make it", "use defaults") waives a stop.
Remembered preferences never do.

### Phase 0: reuse a brief or spec if one exists

If the project already holds a `brand-brief.md` or `ad-spec.json` (look in the project files
first), show it and ask: "Use this, or update it?" If the user keeps it, go to the asset request at
the end of Phase 1 (or to Phase 2 if the assets are already attached).

### Phase 1: brand brief interview

Ask ONE question per turn, in this order, skipping anything already answered (by the user, an
attached document, or the brand's website text the user pasted):

1. Brand name, website, and the social handles to show (and on which networks).
2. What they offer and to whom: the business or product, and the primary audience.
3. The goal of this ad: awareness, a specific offer (what exactly, and until when), a launch, or
   bookings / sign-ups / sales.
4. What makes them different: a story, a signature product, proof points. Only claims the user
   states may appear in the ad.
5. Where it will run and how long: social vertical (9:16), feed (1:1 or 4:5), YouTube / web / CTV
   (16:9); 6, 15, 30 or 60 s. Several formats are fine (one generation, one ad per aspect).
6. Tone, and who presents: suggest a tone from the channel and brand (the user decides), and ask
   for a synthetic presenter (and who it should look like), the user's own photo (they must be that
   person or have the right to use it), or no presenter (voice-over only).
7. The call to action: what viewers should do, and where.

After EVERY answer, acknowledge it in one sentence, ask the next question, then print the running
brief in this exact Markdown shape (omit empty sections):

```
# Brand Brief: <name>
## Brand
## Website / Social
## Offering
## Audience
## Ad Goal
## Offer
## Differentiators
- ...
## Formats and Length
## Tone
## Presenter
## Call to Action
```

When the brief is complete, save it as `brand-brief.md` in the session output location, propose a
preset (see "Presets") with its segment list in one short table, and ask for the assets it needs BY
ROLE, for example: "your logo; 1–3 product photos; 1–2 photos of the place". Say what each becomes.
Offer an optional own music track. Ask the user to say which file is which if the names are not
obvious. **Stop** until the assets are attached.

### Phase 2: spec, script, presenter, voice, music, spend

In ONE turn:

1. **The spec.** Fill the chosen preset with the attached assets (give each an `id` and `role`),
   the formats, the end card, and one line per spoken segment. Apply the word budget.
2. **The script.** Spoken style, no stage directions, no emoji. Each line does its segment's
   `purpose`: the hook speaks to the audience and names the brand; the close is the CTA plus a
   short sign-off in the brand's tone. Use exclamation marks where the energy should lift: this
   voice performs punctuation. Write URLs as words ("<brand> dot com"); never spell a brand letter
   by letter or with hyphens. Keep every line in plain English (or the spec's language): TTS
   voices mangle foreign phrases inside an English read, so put a foreign sign-off on the end card
   as text. A synthetic presenter never speaks as the owner ("our founder's recipe", not "my").
   Never invent prices, dates, awards or statistics.
3. **Presenter** (skip if `none`). With `user_photo`, use their photo. Otherwise generate TWO
   options (the `media-references` two-option rule) with this prompt shape: "Waist-up photo of
   <the presenter description from the brief>, looking into the camera, neutral expression with
   mouth closed, even studio light, plain solid pure green (#00FF00) background, centred, 16:9,
   both shoulders and upper arms fully inside the frame with green space on both sides, arms
   relaxed at the sides, wearing a smooth plain woven top (a blouse, shirt or T-shirt) in any
   colour except green; no knit, ribbed, cable or patterned fabric". Reject an option whose
   clothing is green, knitted or ribbed, or whose arms reach towards the frame edges. (MiniMax
   lip-sync turned a ribbed knit sweater into ghost lettering that read "MINIMAX" across the chest
   in every clip; smooth tops never did.) If both options show teeth (image models often ignore
   "mouth closed"), do not regenerate: edit the better one with `fal-ai/flux-2/turbo/edit`
   (`image_urls: [url]`, prompt "close the mouth into a soft closed-lip smile, keep face, hair,
   clothing, lighting and background identical") as two variants, and let the user pick. Do not
   try to fix a washed-out green or a wrong size by regenerating: image models rarely return pure
   green or the exact size asked for. A user photo gets the same checks; if it fails one, say which
   and ask for another photo. Run these checks by LOOKING: `view_images` on both options (and on a
   user photo) before describing or recommending them; never relay the generator's own description.
4. **Key plate and asset copies** (after the presenter pick, or now if `none`). Run
   `fal-ai/bria/background/remove` (`run_model`, `image_url` = the picked presenter) to get a
   transparent PNG. Then, in ONE `code_interpreter` call following "Sandbox I/O": paste that
   cut-out onto a solid RGB(0,255,0) 1920×1080 canvas, scaled so the head-to-bottom height is 92%
   of the canvas height, centred horizontally and aligned to the bottom edge. Before pasting, check
   the cut-out's alpha bounding box: if it touches the LEFT or RIGHT edge of the source image, the
   presenter was cropped at the sides and the composite will show straight vertical cut lines on
   the sleeves; regenerate (or ask for another photo) instead (touching the bottom edge is fine).
   Save the plate as a JPEG (quality 95) in the session output location. In the SAME call, copy
   every user asset into the session output location as `src-<asset id>.<ext>` (read each by key
   with boto3, re-save unchanged), and use THOSE URLs for every later model call and for assembly.
   This plate, not the raw generation, is the lip-sync `image_url` for every `on_camera` segment.
5. **Voice.** Default: ElevenLabs on fal, **"Jessica"** for a female-presenting voice (warm,
   upbeat; chosen over the M2C catalog voices, which read flat even with `style: "happy"`),
   **"Chris"** for a male-presenting one, matching the presenter (or the brand tone if `none`).
   Offer "Sarah" / "Laura" or "Brian" / "Liam" as alternatives to audition. Only if the user wants
   their OWN cloned voice, use the M2C `text-to-speech` micro-workflow instead (`video-processing`
   TTS reference).
6. **Music.** With `generate`, one mood line from the brand, tone and audience (instruments, feel,
   bpm, "no vocals"). With `user_file`, use their track as the music bed. With `none`, skip.
7. **Spend estimate**, computed from the spec with `get_pricing` and the `media-references`
   `estimate_cost.py` script: lip-sync (sum of `on_camera` line seconds, ~$0.05/s), one
   image-to-video clip per model-animated asset (~6 s each), music (if generated), the voice
   (~$0.05 per 1,000 characters), one background removal (if a presenter). Call `get_pricing`
   BEFORE showing the estimate and show one line per item and a single dollar total, never a range
   or "I'll confirm pricing later".

Show the spec table, the script, the presenter options, the voice and the music, and the cost.
**Stop** for: spec and script approval, presenter pick, voice confirmation, and spend approval.

### Phase 3: voice track

Direct dispatch: one `run_model` call per spoken segment (all in ONE message; each returns in
seconds) on `fal-ai/elevenlabs/tts/turbo-v2.5` with exactly:

```
{"text": "<line>", "voice": "Jessica", "stability": 0.3, "similarity_boost": 0.75, "style": 0.5,
 "previous_text": "<the line before, if any>", "next_text": "<the line after, if any>"}
```

(with `voice` = the confirmed voice). `stability` 0.3 + `style` 0.5 is what makes the read lively;
the defaults (0.5 / 0) sound plain. `previous_text`/`next_text` keep the intonation continuous
across the separate calls. The result is `audio.url` (MP3, 44.1 kHz mono): an HTTPS URL that goes
straight into lip-sync and assembly, no presigning. Probe each with ffprobe in one
`code_interpreter` call ("Sandbox I/O"; do the 5 s padding below in the SAME call) and show them
in ONE `show_media_gallery` (`kind: "audio"`, text = the line, label = segment id + duration).

**Quality gate (never skipped, even when the user waived the review stops):** ElevenLabs returns
no QC score, so check each take's duration: it must be between 0.6× and 1.4× (word count ÷ 2.6 s).
Outside that range the take is garbled or truncated: re-run just that line once, and if it fails
again, reword it slightly. No failed line may reach lip-sync or assembly.

**Lip-sync minimum: 5 s.** `minimax/h3-max/lip-sync` accepts audio of 5–900 s; shorter audio is
accepted at submit and fails minutes later with `value_error (audio_url)` ("Audio duration must be
between 5 and 900 seconds"). For any `on_camera` take under 5.0 s, pad the END with silence to
5.2 s (`apad=whole_dur=5.2`, saved as WAV to the session output location; use that HTTPS URL as
the lip-sync `audio_url`). In assembly, cut that clip back to the line's real length + 0.3 s.

Each segment's final length is its audio duration plus 0.3 s of tail (a `none` segment: its spec
seconds). **Stop** for the user to listen and approve, or to ask for a re-take of any line (re-run
only that line).

### Phase 4: generate the visuals (one tracked plan, generation only)

Launch ONE `plan_and_execute_tool` plan. All nodes are independent siblings. No validation node and
no stitch node. Each node calls its model once and stops; the harness polls and injects results.
Use `submit_job` for every node (all are slow). `screenshot`/`graphic` assets get no node.

Lip-sync model choice (A/B tested, same plate and voice): `minimax/h3-max/lip-sync` moves the mouth
~19% less than `fal-ai/sync-lipsync/v3`, which over-opens and over-shapes words, keeps the green
flat enough to key, returns full 1080p, and costs ~$0.05/s. Do not substitute: Kling AI Avatar v2
Pro is calmer but smeared the clothing in every frame and shifted the lipstick colour; LongCat
returned 896×448 (2:1) and padded a 7.7 s line to 10.8 s.

| Node | Model | Inputs |
|---|---|---|
| `lipsync-<segment id>`, one per `on_camera` segment | `minimax/h3-max/lip-sync/image-to-video` (note: NO `fal-ai/` prefix) | Exactly `image_url` = the green key plate (HTTPS), `audio_url` = that segment's approved voice `audio.url`, or its 5.2 s padded copy if the line is under 5 s (Phase 3) (HTTPS, never `s3://`), `resolution: "1080P"` (allowed: `480P`, `768P`, `1080P`, `2K`; the default `768P` is too soft). No duration field: output length follows the audio. Result at `video.url`. |
| `clip-<asset id>`, one per `product`/`place`/`people` asset a segment uses | `minimax/h3-max/image-to-video` | `image_url` = `src-<asset id>` (session-output copy), `prompt_expansion_mode: "disabled"`, the role's motion prompt (see "Asset roles and motion") filled from the brief, NO `end_image_url`, `duration` = the smallest allowed value ≥ max(5, segment length ÷ assets in the segment + 0.4) (read the allowed set with `get_model_schema`; never send `"auto"` or a value under 5: a short request came back as a 0.9 s clip). Assembly trims each clip to its slot. |
| `music` (only with `generate`) | `fal-ai/stable-audio-25/text-to-audio` | the approved mood line; duration = total ad length + 2 s. |

If an id 404s, `search_models` on the fal gateway is the authority. Do NOT add music to the video
models; the default video model has no audio, and the ad's sound is built in assembly.

### Phase 5: review

After the plan is terminal, call `get_plan_results`. Drop any node that returned an error or no URL
and re-run only that node. Then, in ONE `code_interpreter` call, grab the middle frame of each
lip-sync clip, crop the presenter's chest, and `view_images` them: if any shows text-like marks
that are not in the plate, regenerate the presenter in a smooth top (Phase 2 step 3) and re-run the
lip-syncs before showing the review. Show ONE `show_media_gallery` with every clip in segment
order, then the music. Before showing it, in the SAME `code_interpreter` call as the chest crops,
put each asset clip's middle and last frame side by side with its source photo and `view_images`
them: if the subject changed (a different dish, product, vehicle, room or face), regenerate that
clip with a camera-only prompt before the review. Ask the user to flag anything to regenerate: mouth out of sync, presenter
identity drift, a product, place or person that no longer looks like the user's own photo (compare
with the photo, not with the script), music mood. **Stop** until every clip is approved.

### Phase 6: assemble (in THIS chat, two calls)

Run assembly with `code_interpreter` directly in this chat, NOT as a background plan node: a detached
node can be refused the session's sandbox credentials ("session-scoped sandbox credentials are
required"), and a chat `code_interpreter` call is cut off at 5 minutes. So split it into TWO calls.
Do NOT read other skills or shell out first: everything needed is below. With several aspects,
build the first aspect fully, then repeat both calls per extra aspect (inputs stay in `/tmp/ad/in/`).

**Call 1: inputs, ffmpeg, segments.**
- Follow "Sandbox I/O" (above) for paths, downloads and ffmpeg.
- Render each segment to `/tmp/ad/work/<aspect>/seg<N>.mp4` at the aspect's canvas, 30 fps, picture
  only (`-an`): every segment except the last is `len_i + 0.4` s long (the extra 0.4 s is eaten by
  the dissolve into the next segment, see "Canvas and timing"), the last is exactly `len_last`. Run
  every ffmpeg call with `capture_output=True`, check its return code, and on failure print the
  last 30 lines of stderr and fix only that segment. If a call raises before it finishes, re-run
  the WHOLE call after fixing it: the helpers it defines after the failing line do not exist yet
  (a font error left `seg_brand` undefined and cost two extra calls).
- **Frame-rate rule (a dissolve failed without it):** `tpad`, `trim`, `setpts` and `zoompan` drop
  or change the frame rate, and `xfade` then fails with "The inputs needs to be a constant frame
  rate; current rate of 1/0 is invalid". End EVERY chain that feeds an `xfade` or `overlay` with
  `fps=30,settb=1/30`, after any `tpad`/`trim`/`setpts`/`zoompan`.
- Save the lengths and paths to `/tmp/ad/work/state.json` for call 2.

**Call 2: join, audio, encode, checks.** Join the segments with 0.4 s `xfade` dissolves (each input
`fps=30,settb=1/30`), build the voice stem, music bed and mix as in "Audio" below, mux, and run all
of "Check before returning". Upload the ad and the check frames to the session output location,
as `ad-<brand>-<WxH>.mp4` and `check-<WxH>-<segment id>.jpg`.

Return the final URL(s) and show them once. The harness registers them as project assets. Then
offer: another aspect ratio (Phase 6 only), a different music bed, a length cut-down (re-voice the
shortened lines, Phase 3 onward for those segments only), or a script tweak.

## Assembly recipe (Phase 6)

Probe every input with ffprobe first; never trust a duration you were told.

### Canvas and timing

- Canvas: from "Formats" (W×H). 30 fps, `yuv420p`. Landscape = 16:9; portrait = 9:16 and 4:5;
  square = 1:1.
- `len_i` for segment i = its approved voice take's duration + 0.3 s (`none` speaker: its spec
  seconds).
- Every segment's PICTURE except the last is built `len_i + 0.4` s long, because each 0.4 s
  dissolve (`xfade=transition=fade:duration=0.4`) overlaps two segments. The xfade `offset` for the
  join after segment k is `sum(len_1..len_k)`. Total ad length = `sum(len_i)`.
- If a source clip is shorter than its slot, hold its last frame (`tpad=stop_mode=clone`); if
  longer, `trim` it. Never speed-change a lip-sync clip.
- Asset clips and stills fill the canvas by cover-crop:
  `scale=W:H:force_original_aspect_ratio=increase,crop=W:H`. A `screenshot`/`graphic` still is
  first scaled to 1.15× cover, then `zoompan=z='min(zoom+0.0006,1.12)':d=<frames>:s=WxH:fps=30`.

### Brand colours

With `brand.colors` given, use the first as the background and the second (or white/black by
contrast) for text. With `auto`: open the logo with Pillow and take the median colour of its 10-px
border. If that border is OPAQUE (any colour, cream and white included), use it as the background
so the logo blends in with no visible tile edge. Only if the border is transparent, use the logo's
dominant non-background colour at 15% lightness (dark logos: at 92% lightness), so the logo stays
readable. Text is white on a dark
background and near-black on a light one.

### Presenter cut-out

Lip-sync clips were generated on a pure green background. Key them with
`chromakey=0x00FF00:0.12:0.08,despill=type=green`, then `format=yuva420p`. MiniMax H3 shifts the
green slightly (measured RGB ~7,232,8), so sample the key colour from the clip's top-left 40×40
corner and key on that instead of hard-coding 0x00FF00. If a review frame shows a green fringe,
raise similarity to 0.16. If hair or clothing disappears, lower it to 0.09.

### Segment layouts

**Crop before you place.** The plate centres the presenter on a full-width green canvas, so the
keyed lip-sync clip is mostly empty. Measure the presenter's alpha bounding box ONCE on a middle
frame of the first lip-sync clip (after chromakey: the columns/rows whose alpha > 0), widen it by
2% on each side, and `crop` every presenter clip to that same box BEFORE `scale`/`overlay`. The
sizes and positions below refer to that cropped presenter, never to the full 1920-wide plate;
placing the uncropped plate at "x = 4%" puts the presenter in the middle of the frame, over the
logo. Self-check on each `presenter_on_brand` check frame: the presenter and the logo must not
overlap.

If a presenter that was already lip-synced turns out to be cut off at one side (a straight
vertical edge on a sleeve), do not regenerate: `hflip` the keyed clip if needed so the cut side
faces the nearest frame edge, and place it so the cut edge sits exactly on that edge,
bottom-aligned. It then reads as the frame cropping the presenter.

| `visual` | Landscape (16:9) and square (1:1) | Portrait (9:16, 4:5) |
|---|---|---|
| `presenter_on_brand` | brand colour; presenter 85% of H, bottom-left, x = 4% of W; logo up to 60% of H, centred in the space right of the presenter | brand colour; logo up to 60% of W, centred in the top 35% of H; presenter 60% of H, bottom, centred |
| `presenter_over_clip` | the segment's clip(s); presenter 75% of H, bottom-left | the segment's clip(s); presenter 55% of H, bottom, centred |
| `clip` | the segment's clip(s), full-frame | same |
| `text_card` | brand colour; the line at ~9% of H font size, centred, wrapped to 80% of W; logo 12% of H, bottom-right | same, logo bottom-centre |

Text cards hold at most 8 words in at most 3 lines; wrap by measuring with `ImageFont.getlength`
and never leave a single word alone on the last line (shorten the text instead). If the logo has
an opaque border, the card background is that colour (same rule as the end card).
| `end_card` | see "End card" | same, stacked vertically |

### End card (Pillow, rendered to PNG per canvas)

Background: if the logo has an OPAQUE border (any colour), use that exact border colour, so the
logo never sits as a visible tile on a different colour; otherwise the brand colour darkened to
≤ 20% lightness. Text colour by contrast. From
top to bottom, only the elements the spec turns on: the optional `sign_off` (a short phrase kept
out of the voice); the logo (up to 20% of H); the socials as "<network>.com/<handle>" or
"@<handle> on <network>"; a QR code of `end_card.url` (about 25% of the shorter side); the
`offer_code` in a box; and the CTA line, large and centred, worded to match the spoken last line.
Landscape puts socials left and the QR right on one row; portrait stacks them.

- Font, exactly (`matplotlib.font_manager` is a submodule: `import matplotlib` alone, or
  `__import__('matplotlib').font_manager`, raises `AttributeError` and aborts the cell):
  ```
  from matplotlib import font_manager
  from matplotlib.font_manager import FontProperties
  REG = font_manager.findfont(FontProperties(family="DejaVu Sans"))
  BOLD = font_manager.findfont(FontProperties(family="DejaVu Sans", weight="bold"))
  ```
  If matplotlib is missing, use `ImageFont.load_default(size=...)`.
- QR: try `import segno`, then `import qrcode`. If neither imports, leave the QR out and make the
  URL text larger. Do not pip-install.

### Audio

The narrator must be clearly on top: music sits **18 dB under the voice** during speech and rises
only a little in the gaps. Never set the music level as a fixed `volume=` factor; the generated
track's loudness varies run to run, so normalize both stems first. With `music.mode: "none"`, skip
steps 2, 3 and 5 and mix the voice alone.

1. Voice: for each segment, resample its take to 48 kHz stereo
   (`aresample=48000,aformat=channel_layouts=stereo`) and pad it to exactly `len_i`
   (`apad,atrim=0:len_i`; a `none` segment is silence of `len_i`), then `concat` all of them
   (`concat=n=<segments>:v=0:a=1`), then `loudnorm=I=-16:TP=-1.5:LRA=7`.
2. Music (generated or the user's file): generated tracks often end early and leave a silent tail
   (a 36 s request went silent at 27.9 s), or simply end early (an 18 s request returned 15.7 s).
   Its usable length is the earlier of its first trailing silence (`silencedetect=n=-30dB:d=0.3`) and
   its file duration. If that is shorter than the ad's total length + 1.5 s, cut it there and
   extend it with a second copy joined by `acrossfade=d=2`. Trim the track's LEADING silence (often ~0.8 s) off BOTH
   copies first, or the second copy's silent opening lands as a dropout mid-ad. Load the music file
   as TWO separate `-i` inputs and crossfade those. Never feed `acrossfade` from one `asplit`: on
   the sandbox's ffmpeg 7.0.2 it then outputs only the first copy (25 s of a needed 34 s), and
   `amix` hides it by running on with the voice alone. Render the music branch alone first and
   assert its length is at least the ad's total before mixing. Then `atrim` to the total length,
   `loudnorm=I=-30:TP=-6:LRA=7` (14 dB under the voice before ducking), `afade=t=in:d=1`,
   `afade=t=out:st=<total-1.5>:d=1.5`.
3. Duck: `sidechaincompress=threshold=0.015:ratio=10:attack=15:release=250:makeup=1` on the music
   with the normalized voice as the sidechain (≈ a further 4–6 dB under speech).
4. `amix=inputs=2:normalize=0`, then `loudnorm`: I=-14 for `web`, I=-24 for `ctv`.
5. **Balance check (part of "Check before returning"):** render the voice stem and the final
   ducked-music stem to WAV and measure each with `ebur128`: the voice's integrated loudness must
   be at least 15 LU above the ducked music's. If not, lower the music's loudnorm target by the
   shortfall and rebuild the audio only.

### Encode

`-c:v libx264 -crf 18` when the ffmpeg build has it. If a build has only `libopenh264`, use
`-c:v libopenh264 -b:v 8M`. Always `-pix_fmt yuv420p -r 30 -c:a aac -b:a 192k
-movflags +faststart`.

### Check before returning

ffprobe the output: duration within 0.2 s of `sum(len_i)`, one video and one audio stream, the
expected WxH, and `silencedetect=n=-45dB:d=0.3` on the final mix reports no silence before the last
0.5 s (with music, it must run under the end card). Extract one frame from the middle of each
segment to the output location and `view_images` them, so a keying or layout fault is visible
without watching the whole ad.

## Guardrails

- Never invent facts about the business (prices, dates, awards, statistics, opening hours). The
  script, text cards and end card use only what is in the brief.
- Never alter the logo. Product, place and people clips must stay recognisably the user's photos;
  if a clip changes them beyond recognition, regenerate it.
- A synthetic presenter is never presented as a real employee or the owner. A `user_photo`
  presenter needs the user's statement that they are that person or may use the photo.
- The voice approximates; it is not a guaranteed reproduction of any real person.
