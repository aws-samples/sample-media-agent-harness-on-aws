---
name: media-references
description: "Use for CREATING media with fal.ai -- generate or edit images, video, music, speech. Default video model MiniMax H3 Max has NO native audio, so plan music separately and combine via ffmpeg in code_interpreter (see video-processing for ALL combining: mux, concatenate, crossfade); native-audio Kling v3 Pro makes a soundtracked video in ONE node, no separate music+mux. NOT for analysing existing media: transcription, diarization, shot detection, face recognition are the M2C gateway (see video-processing), and NOT for research, code or data work. Supplies exact model ids + input field names so no schema lookup is needed, requires TWO options for every image including edits, keeps media as URLs not bytes."
---

# Media stays a reference

Generation tools return a **link**, not bytes. fal returns
`https://v3b.fal.media/files/...`; the media gateway returns `s3://` URIs. That link is
already everything the system needs: the render hook mirrors external media to
CloudFront and dispatches it to the Render Agent, which is how a generated image
appears in the UI without anyone downloading it.

So the default is: **pass the reference along and stop.**

## Generation rules that fail plans — read these first

These cause silent, expensive failures on async plans, so they lead the skill rather than
sit at the bottom where a focused node never reaches them.

1. **`submit_job`: send only `prompt` and `duration`. Never guess a tuning parameter**
   (`num_inference_steps`, `guidance_scale`, `steps`, `seed` ranges, …). A wrong async param
   is NOT rejected at call time — it is accepted, queued, and 422s MINUTES later inside the
   result body, after dependent steps already dispatched. That is how a soundtrack was lost:
   `num_inference_steps: 50` on a model capped at 8, discovered only after the mux node ran.
   If you must set a tuning param, `get_model_schema` first. Otherwise let the model default it.
   But ALWAYS send the fields THAT model requires. Requirements differ even within one family:
   `minimax/h3-max/image-to-video` (and its `text-to-video` / `reference-to-video` siblings) REQUIRE
   `prompt_expansion_mode: "disabled"` (valid: `disabled`|`fast`|`balanced`|`quality` — never
   `off`/`auto`), while `minimax/h3-max/recast` does NOT accept it at all. A submit that breaks the
   model's own schema is refused before anything is sent, and the refusal lists the fields that model
   accepts: fix the input from that list, and keep the same submit tool.

2. **The default video model (MiniMax H3 Max) has NO native audio — plan music + mux separately.**
   `minimax/h3-max/*` outputs video only (no audio track, no `generate_audio` flag), so a "video with
   music" request is THREE steps: generate the video, generate the music, then combine them with ffmpeg
   in `code_interpreter` (see the `video-processing` skill — authoritative for all combining). The
   one-call shortcut applies ONLY when you deliberately pick a native-audio model: the Kling v3 Pro
   second pick (`fal-ai/kling-video/v3/pro/*`) makes a soundtracked video in a single call WHEN you pass
   `generate_audio: true`, and for THAT model you must NOT add separate music+mux nodes.

3. **Aspect ratio / dimensions are an INTENT parameter — set them, don't let the model default.**
   Edit and image→video models do NOT infer the source frame's aspect ratio; each falls back to its
   own default shape (`fal-ai/flux-2/turbo/edit` → **square 1024×1024**), so a 16:9 frame silently
   comes back square and every downstream step inherits it — a square "video" from a widescreen still
   is THIS bug, not the model. When the user wants a specific aspect (or "keep it 16:9 / widescreen"),
   pass it EXPLICITLY: `image_size` on an edit, `aspect_ratio` (or `resolution`) on image→video. This
   is NOT the "never guess a tuning param" case of rule 1 — it is a value the user gave you, so setting
   it is required, not guessing. If unsure of the exact field name for the chosen model, one
   `get_model_schema` is cheap next to re-running a wrong-shape generation.
   **When the task is derived from a SOURCE frame or clip (recreate, re-animate, restyle, extend), the
   default aspect is the SOURCE's aspect — never a generic vertical/`9:16`.** "Recreate these 16:9 shots"
   means 16:9; do not offer or apply a standing "default 9:16 vertical" that contradicts the material in
   hand (that is what pillarboxed a widescreen cityscape reel into a vertical canvas with blur bars).
   Read the source dimensions and carry that aspect through generation AND assembly. If the user
   genuinely wants a different shape than the source, confirm it explicitly rather than assuming a
   vertical default. And when a model IGNORES the requested aspect and returns the source shape anyway
   (h3-max returns 16:9 from a 16:9 frame regardless of an `aspect_ratio` hint), the ASSEMBLY step must
   match the clips' NATIVE shape — do not force the stitched output onto a conflicting canvas that adds
   letterbox/pillarbox bars.

4. **Video `duration` is a constrained enum with a MODEL MINIMUM, and a target TOTAL length can be
   INFEASIBLE — check both before planning.** Video models accept `duration` = `"auto"` or a
   model-specific set of seconds — and that set is NOT the same across models: some take any INTEGER in
   a range (e.g. `>= 5`), others accept only a CLOSED ENUM of a few values (observed live:
   `Input should be '6' or '10'` — 6 or 10 ONLY, nothing between). A value outside the model's set is
   NOT rejected at call time — it 422s MINUTES later inside the result body (rule 1's failure mode).
   So do NOT hardcode or assume a floor: **default to `duration: "auto"`, and when a specific length
   matters read the ALLOWED set from `get_model_schema` and pick from THAT set.** Never send a bare
   guessed integer. This is how an 8-clip crossfade scene lost every clip — each animate node forced a
   tiny per-clip `duration` the model didn't accept, and all 422'd, leaving the stitch step with no inputs.
   **Write the value exactly as the schema lists it — digits only, NO unit:** `"7"` or `7`, never
   `"7s"` / `"7 sec"`. A unit suffix 422s at result time (`body.duration: unable to parse string as an
   integer` — staging 9fb4e17b lost two of eight clips to `"7s"`).
   **Animate-long-then-trim:** when you plan to generate a clip and TRIM it to an exact fractional target
   (e.g. recreating shots at their original 4.17s / 1.90s durations), do NOT request the fractional or
   sub-minimum length. Pick the **smallest value the model's schema ALLOWS that is ≥ your target**
   (for an enum model that means the next allowed value up — a 6.61s target → `10` when the set is
   `{6,10}`; a 1.9s target → the smallest allowed value, e.g. `6`), generate at that, then trim down with
   ffmpeg. If in doubt use `"auto"` and trim. The trim makes over-generating free; guessing a value the
   model rejects does not.
   **Feasibility:** when the user gives a target total length, reconcile it against a realistic per-clip
   floor × clip_count (minus crossfade overlaps) BEFORE building the plan. If the target is below that
   floor (e.g. "all 8 clips" at a few seconds each), you CANNOT satisfy both — REDUCE the clip count
   (animate a representative subset) rather than shrinking clips below the model minimum. If the user
   insists on both, say plainly the two can't hold together and offer the two honest options (fewer clips
   at the target length, or all N clips at the longer real length). Never silently pick invalid durations
   to make impossible math "work".

Full rationale for rule 1 is under "Do NOT call recommend_model or get_model_schema first";
model ids and their input field names are in "Model shortlist". The rest of this file is
retrieval and orchestration detail the main agent uses.

## Never put file content in a tool result

A turn that had already generated a video, muxed it with a soundtrack and uploaded the
artifact was destroyed by this:

```
Failed to create message: An error occurred (413) when calling the CreateEvent operation
```

The work was finished. Session memory refused the event because the result carried the
file's bytes — almost certainly base64 from a download step. The answer existed and was
thrown away.

Inline bytes are also paid for on **every later turn** in that session, and the renderer
cannot use them: it wants a URL.

## What to do instead

**To show media to the user.** Return the URL in your response. Do not download it, do
not base64 it, do not read it into the conversation. The render hook handles mirroring
and display. An image, a video and an audio clip are all the same case.

A final video is exactly this: return the fal URL. The renderer plays it, and the render
hook mirrors it to the CloudFront artifacts bucket so the link outlives fal's temporary
one. If you want the durable URL in your own reply, use the `artifact` tool to mirror it
and quote what that returns — but never download the file into the conversation to do it.

**To combine media** (mux video+audio, concatenate, crossfade, overlay/PiP, trim, transcode) — this
is ffmpeg in `code_interpreter`, per the **`video-processing` skill, which is authoritative for ALL
combining**. Do NOT route combining through fal (`fal-ai/ffmpeg-api/*`, `compose`) — the deterministic
code_interpreter path composites reliably and writes to the session output prefix. Hand the combine
node the input **URLs** the earlier steps returned; video-processing downloads, composites, and
returns one file. No media ever enters the conversation.

The combine writes the final file to the session **output prefix**, so **emit that output's
URL** (`https://<ARTIFACTS_DOMAIN>/<session OUTPUT prefix from S3 CONTEXT><file>.mp4`) as the node's
result — the render hook displays media from the URL in your output. Do this EXACTLY once: do NOT
then dispatch a separate render, and do NOT re-run the combine to "make it show up". A
`code_interpreter` `put_object` does not itself surface a URL to the render hook, so if you skip the
URL the final video will not auto-display and you'll be tempted to re-render — which is what produced
TWO manifests for one video and the "render 2–3 times" churn. One combine, one emitted URL, one tab.

Do not confuse muxING with **Mux the company** (video hosting, playback ids, MuxPlayer). fal has no
Mux-the-company integration and does not need one: combining a video with an audio track is ffmpeg in
`code_interpreter` (video-processing). A search for "fal mux" surfaces the hosting product and reads
like a missing capability. It is not.

The M2C media gateway also cannot do this: `process_media` operates on assets already in its own
bucket and cannot mux two external URLs. Reaching for it produces a node that retries forever.

**To inspect a result file** (a JSON manifest, a transcript, a caption file), use
`read_result_files`. That is for small structured outputs. It is not for media.

## Fetch the FINAL output only

When a plan finishes, the user wants the thing the plan was for. Return the FINAL node's
output and stop. Do not walk the intermediate nodes, and do not read their files.

In a video + soundtrack + mux plan, the answer is the muxed video from the mux node. The
video-only and audio-only outputs are scaffolding: they were inputs to the final step and
are of no interest on their own. Fetching them costs round-trips, pads the conversation
and buries the one link the user asked for.

Mention an intermediate node only when the final node FAILED and its inputs explain why.

## Video with a soundtrack: three-step by default, one-call only for a native-audio model

The default video model (MiniMax H3 Max) has NO native audio, so a "video with music" request is the
three-step plan: generate the video, generate the music, then combine them with ffmpeg in
`code_interpreter` (the `video-processing` skill). That is the normal path now — not a fallback.

The one-call shortcut applies ONLY when you deliberately pick a native-audio model:
`fal-ai/kling-video/v3/pro/*` (the second pick, with `generate_audio: true`) or `fal-ai/ltx-2-19b`
generate a soundtracked video in a single call — for those, ask for video-with-audio and stop (no
separate music, no mux, no DAG).

## Use the ASYNC tool for video and music

`run_model` is SYNCHRONOUS: it holds the call open until the render finishes. That is
right for an image, which takes seconds. It is wrong for video and music, which take
minutes -- the node blocks, the step can time out, and because no job exists there is
nothing in the Activity panel and no notification when it finishes.

So:
- **image** -> `run_model` (fast, sync, no job needed)
- **video, music, speech, upscale, or anything else measured in minutes** -> `submit_job`

`submit_job` returns a handle, which is what registers the work as a tracked job: it then
appears in the Activity panel, is polled to completion, and notifies you when done. The
result is fetched with the trio's results tool. Choosing `run_model` for a video skips all
of that and hopes the call outlives the render.

## A terminal job is not a successful job

A queue can report COMPLETED for a job whose inference FAILED. The observed case reached
"COMPLETED" while its result body carried an HTTP 422, so the status alone was misleading.

So when you fetch a result, READ it before using it. If it carries an `error`, `detail`,
or an HTTP status outside 2xx, treat the step as failed and say so -- do not pass it to a
dependent step, and do not report success because the job status said COMPLETED. Quote
the error to the user: a parameter they can see is fixable, a silent absence is not.

## Async generation returns a handle, not a file

`submit_job` returns `{request_id, status_url, ...}`. That is a queue handle, and its
`status_url` is unauthenticated — fetching it directly returns 401. Resolve it with
`get_job_result` (polling `check_job` until the job reports a terminal state) to obtain
the real file URL, then treat that URL as above.

If a job reports **COMPLETED within seconds and `get_job_result` then 404s**, the endpoint id
is wrong — fal routed the job to a different app, so there is no result to fetch. Do NOT
re-poll or wait; fix the id (check it against the shortlist below — MiniMax is
`minimax/h3-max/...`, with NO `fal-ai/` prefix) and resubmit. Do NOT reconstruct the result
URL from the model/endpoint id.

A handle is not a result. Reporting "here is your video" with a `status_url` is wrong,
and so is trying to mux one. The same holds for a canary/probe run before a fan-out: it
passes only when the job reached a terminal success AND its result (the file URL) was
fetched. A submit that was accepted, or a job still IN_PROGRESS, has proved nothing about
the result path — report it as not yet verified, not as a pass.

## Quick check before you answer

- Am I about to include file content in a result? Return the reference instead.
- Do I have a `status_url` rather than a file URL? Resolve it first.
- Did I download something only to hand it back? Upload it and return the URI.

## Model shortlist — start here

Two fast / good-value options per category. **Google models are deliberately excluded**
(no Veo, Gemini, or Nano Banana). Prefer the first pick; use the second when the first
is unavailable or its output is unsuitable.

| Task | First pick | Second pick |
|---|---|---|
| Text → image | **BOTH, always — see below** | `fal-ai/flux-1/schnell` + `fal-ai/z-image/turbo` |
| Image edit (reference image) | `fal-ai/flux-2/turbo/edit` (takes `image_urls`, array) | `fal-ai/flux-pro/kontext` (takes `image_url`, **singular**) |
| Text → video | `minimax/h3-max/text-to-video` | `fal-ai/kling-video/v3/pro/text-to-video` (native audio via `generate_audio`) |
| Image → video | `minimax/h3-max/image-to-video` (takes `image_url`) | `fal-ai/kling-video/v3/pro/image-to-video` (takes `start_image_url`) |
| Music / SFX | `fal-ai/stable-audio-25/text-to-audio` | `fal-ai/ace-step/prompt-to-audio` |
| Speech (TTS) | `fal-ai/minimax/speech-2.8-turbo` | `fal-ai/elevenlabs/tts/turbo-v2.5` |
| Upscale | `fal-ai/seedvr/upscale/image` | `fal-ai/topaz/upscale/video` (video) |
| Background removal | `fal-ai/bria/background/remove` | — |
| Combine clips — mux (video+audio), concatenate, crossfade, overlay/PiP | **ffmpeg in `code_interpreter`** — see the `video-processing` skill (authoritative for ALL combining). NOT a fal endpoint. | — |

### The video model ids, and how to confirm them

Standardized on **MiniMax H3 Max** for both text→video and image→video, with **Kling v3 Pro**
as the second pick. The exact ids (note MiniMax has **no** `fal-ai/` prefix, unlike Kling):

  minimax/h3-max/text-to-video
  minimax/h3-max/image-to-video
  fal-ai/kling-video/v3/pro/text-to-video
  fal-ai/kling-video/v3/pro/image-to-video

If an id ever 404s, `search_models` on the gateway target is the authority — public docs tell
you a model exists, not that YOUR gateway exposes it. There is also a distinct base
`minimax/h3/*` family; do not confuse it with `h3-max`.

### Image → video field name: MiniMax uses `image_url`, Kling uses `start_image_url`

`minimax/h3-max/image-to-video` inputs (from the fal schema — so no lookup needed): `prompt`
(required), `prompt_expansion_mode` (REQUIRED — exactly one of `disabled`|`fast`|`balanced`|`quality`;
send `"disabled"` so fal does not rewrite your prompt), `image_url` (OPTIONAL — the
first frame; when set the OUTPUT ASPECT RATIO FOLLOWS THAT IMAGE, and omitting it runs the same
endpoint as text→video at 16:9), `end_image_url` (optional last frame for a first→last transition),
`duration` (model-specific — `"auto"` or a value from the model's allowed set; the set is NOT always
"any integer ≥ 5": some h3-max variants accept only a closed enum like `{6, 10}`, so read it from
`get_model_schema` and never guess — see rule 4),
`resolution` (`480P`/`768P`). It outputs video ONLY —
no audio, no `generate_audio` — so plan music + mux separately (rule 2).

The Kling v3 Pro second pick takes `start_image_url` (NOT `image_url`) and produces native audio only
when you pass `generate_audio: true`. The wrong image field name is not a validation error — it is an
unconditioned generation that ignores your image, so when you fall back from MiniMax to Kling, change
`image_url` → `start_image_url` too.

Because h3-max image→video follows the INPUT image's aspect, a square input (e.g. a flux edit that
defaulted to 1024×1024) still yields a square video — the fix is UPSTREAM at that edit (pass
`image_size` to hold 16:9; see rule 3), not on the animate step. For text→video use `aspect_ratio`
(default 16:9). `duration` is model-specific — read the allowed set from `get_model_schema` (it may be
an integer range OR a closed enum like `{6,10}`) and use `"auto"` or a value from that set; omit it for the
model default rather than guessing a value that 422s in the result body MINUTES later (rule 1). For a
fixed-length scene from several clips, pick the clip count to fit the target rather than shrinking
clips (rule 4).

### The rule for edits: an edit model must ACCEPT the reference image

A model only conditions on a reference photo if its schema has an image input --
usually `image_urls` (array) or `image_url`. A text-to-image model given an image
does not error: it **silently ignores it** and returns something that does not
resemble the subject at all.

**The two picks do not use the same field name.** This is the whole trap: pass
`image_urls` to Kontext and it is not a validation error, it is a silently unconditioned
generation that returns a stranger.

| Model | Endpoint | Reference field |
|---|---|---|
| FLUX.2 [dev] Turbo Edit | `fal-ai/flux-2/turbo/edit` | `image_urls` (array) |
| FLUX.1 Kontext [pro] | `fal-ai/flux-pro/kontext` | `image_url` (**string, singular**) |

Both are Black Forest Labs, so neither trips the no-Google rule. Turbo Edit is the
first pick on speed -- 8-step distilled inference, ~6s at 1024x1024 -- which is why it
replaced `seedream/v4.5/edit`: seedream produced correct results but was slow enough to
dominate the turn.

A model only conditions on a reference photo if the field it actually declares is the
one you send. An endpoint given the WRONG image field does not error: it **silently
ignores it** and returns something that does not resemble the subject at all.

That has already cost a full re-run once. The table used to list `fal-ai/flux-2/klein/9b`
as "native editing"; that path is text-to-image, so a manga character sheet came back
bearing no resemblance to the person. Note the precise shape of that mistake, because
the first correction overstated it: klein DOES have an edit endpoint at
`fal-ai/flux-2/klein/9b/edit`. The base path is not it. A family having an edit variant
says nothing about whether the URL you typed is that variant.

So when a reference image is present:

1. Prefer `fal-ai/flux-2/turbo/edit` with `image_urls: [url]`.
2. Use `fal-ai/flux-pro/kontext` with `image_url: url` when the first is unavailable
   or its output is unsuitable -- and send the SINGULAR field.
3. **No schema lookup for either pick above** -- their field names are in the table, which
   is why the table now carries them. A measured run spent **61 seconds** in
   `get_model_schema` before generating, to learn what is written here. Look up a schema
   only for a THIRD candidate this skill does not list; there the silent failure justifies
   the round-trip.
4. Never pass a reference image to a `text-to-...` endpoint and hope.
5. **The output aspect ratio is not inherited from the reference — set it.** `fal-ai/flux-2/turbo/edit`
   defaults to **square 1024×1024**, so editing a 16:9 frame returns a square image (and any
   image→video built on it is square too). Pass `image_size` to match the source aspect when the user
   wants the original framing preserved. See rule 3 at the top.
6. **The reference must be an `https://` URL, never `s3://`.** fal fetches the image over
   HTTP and cannot read an S3 URI. A run that dispatched with the uploaded asset's `s3://`
   uri burned a generation, then had to re-dispatch with the CloudFront url -- three
   `run_model` calls for a two-variant request. Convert first: an uploaded file already has
   a CloudFront url on the artifacts domain, so use that and dispatch ONCE.
7. **Restyle = content-preserving style transfer.** When the goal is to RESTYLE an existing frame
   (cartoon/anime/paint/sketch) rather than change its content, default the edit prompt to:
   *"restyle only; keep the identical composition, subjects, objects, poses and background; do not
   add, remove, or invent anything."* A vague "make it a cartoon" drifts the content and then costs
   several correction turns; the explicit keep-everything framing lands it in ONE pass.

## Image generation: always produce TWO options

For a text-to-image request, run BOTH `fal-ai/flux-1/schnell` AND `fal-ai/z-image/turbo`
with the same prompt, and return both URLs so the user can pick. Two fast models cost
less than one premium model and give a real choice; a single image gives the user
nothing to compare and usually leads to a re-roll anyway.

**This applies to image EDITS too, not just text-to-image.** The rule used to say
"for a text-to-image request", so an edit fell outside it and returned a single
result -- exactly when a choice matters most, because an edit either preserves the
subject's likeness or it does not, and one attempt gives the user nothing to judge
that against. Run two edit variants in ONE `dispatch_tool` call: the same verified
edit model twice with different seeds or phrasings, or two models that both accept
an image input. If only one edit model is verified available, say so rather than
silently returning one image.

Label which model produced which. Both are fast tiers, so run them together rather than
one after the other.

**Both models must run inside ONE `dispatch_tool` call.** Not two parallel dispatches.
This is not a style preference: renders are grouped per sub-agent invocation, so two
dispatches produce two Renderer tabs with one image each no matter what else is
configured. One dispatch that calls `run_model` twice produces one tab with both. Ask
the sub-agent for both models in a single task — "generate this prompt with
fal-ai/flux-1/schnell AND fal-ai/z-image/turbo, return both urls".

**Render both in ONE tab, side by side — not two tabs.** The renderer shows several
images in a single entry when they arrive as one artifact set, so emit both URLs together
as one render (`artifacts: [{url, label}, {url, label}]`, labelled by model) rather than
letting each generation render separately. Two tabs make the user click between them to
compare, which defeats the point of offering a choice.

This applies to IMAGES only — generated OR edited. Video, audio and speech return ONE
result: a second video doubles both the wait and the cost, and video is already the
slowest step in any plan.

Escalate to a premium tier only when the user asks for higher quality, or when a fast
tier has already produced something unsatisfactory. Premium equivalents:
`fal-ai/flux-pro/v1.1-ultra` (image) and `fal-ai/kling-video/v3/pro/*` (video, native audio). These
cost materially more and are slower — for
video that means a longer wait before anything exists to combine.

## The rule behind the table

The table is a shortcut, and it WILL go stale -- fal's catalog changes weekly. So learn the
rule it encodes, and apply it when a name here no longer resolves:

- **Prefer the cheap, fast tier.** fal labels them in the id itself: `schnell`, `turbo`,
  `flash`, `fast`, `mini`, `lite`, `klein`, `draft`. Reach for `pro`, `ultra`, `master` or
  an unlabelled flagship only when the user asks for higher quality or a fast tier has
  already disappointed.
- **Prefer a model already proven in this session** over an unfamiliar one of the same tier.
- **Never pick a Google model** (Veo, Gemini, Nano Banana) -- excluded by policy here.

With that rule, `search_models` output is enough to choose correctly without the table.

**These names came from fal's public gallery, and "value" is inferred from fal's own
tier labels (`schnell`, `turbo`, `mini`, `fast`, "lower cost"), not from measured
prices.** Call `get_pricing` before promising a user anything about cost, and
`search_models` if a name above 404s — the catalog changes weekly and a stale id fails
the whole step.

## Cost: report ACTUAL for images, estimate BEFORE video

**Images — report the actual cost AFTER generating; do not price one before.** Pricing an image
before you generate it is exactly the pre-generation round-trip the rest of this skill tells you to
skip, which is why a "quick estimate" here just gets dropped. So don't estimate up front — generate
the two options, then report what they actually cost. Read each returned image's real `width`/`height`
from the result, make ONE `get_pricing` call (cached ~5 min) for the model's rate, and compute the
exact cost with `scripts/estimate_cost.py`. This runs AFTER generation, so it never gates or delays
the image, and it is NOT the pre-generation model-selection chain forbidden below. Read
`get_pricing`'s `unit`: most image models bill per **`megapixels`** (flux-1/schnell $0.003,
z-image/turbo $0.005), a few per **`images`** (flat, flux-pro/kontext $0.04).

- per-megapixel model → `--meter per_megapixel --width W --height H --segments N`, using the
  RETURNED dimensions (each image is one frame, so `--segments` counts images);
- flat per-image model → `--meter per_run --segments N`.

Report it as one line under the images — e.g. `actual cost: $0.006 (2 images, 1024x1024 at $0.003/MP)`.
It is exact because it uses the real output size, not an assumed one. Reuse the cached rate for later
images of the same model rather than calling `get_pricing` again.

    # after two flux-1/schnell images came back 1024x1024, at $0.003/megapixel
    python scripts/estimate_cost.py --meter per_megapixel --rate 0.003 \
      --rate-source live:get_pricing --width 1024 --height 1024 --segments 2

**Video and music — estimate AND stop for approval, BEFORE generating.** This is where the number really matters,
especially a multi-clip plan. `get_pricing` returns a per-unit RATE, not a total, and the total
depends on the model's METER, which differs by model:

- **per megapixel** (e.g. `fal-ai/ltx-2-19b/*`): cost = rate × width × height × frames ÷ 1e6 —
  resolution and frame count move it a lot.
- **per second**: cost = rate × seconds (frames ÷ fps).
- **per run**: a flat rate per clip.

Do not multiply that across a reel in your head. `scripts/estimate_cost.py` does it fail-closed:
feed it the LIVE rate from `get_pricing` (label its provenance with `--rate-source`), the meter
from the model schema, and the per-segment lengths; it returns per-segment and total USD. Show
the total before submitting a paid batch, and confirm it when the user is spending materially.
This spend approval is NOT waived by remembered or long-term preferences, saved defaults, or a
prior session's approval: only an explicit approval in THIS conversation, for this estimate,
waives it. A remembered choice may be offered as the suggested answer.

    python scripts/estimate_cost.py --meter per_megapixel --rate 0.0008 \
      --rate-source live:get_pricing --width 512 --height 512 --lengths 121,121,89

The script embeds NO prices on purpose — a rate baked into a skill would quietly lie as the
catalog moves. The rate is always caller-supplied and always from a live lookup. This is the
one deterministic helper this skill bundles; everything else here is orchestration prose.

## Do NOT call recommend_model or get_model_schema first

The table above IS the model decision. A real trace spent 25s in `recommend_model` and
21s in `get_model_schema` before a 17s generation -- 47 seconds of lookup to answer a
question already answered here, and it still returned one image instead of two.

Call `run_model` directly with a model id from the table. For a SYNCHRONOUS call this is
safe: a bad argument fails in seconds and you can fix it and retry.

**`submit_job` is the exception — check the schema first.** An async parameter error does
not fail the call; it is accepted, queued, and surfaces MINUTES later inside the job
result, after dependent steps have already been dispatched. That is exactly how a
soundtrack was lost:

    HTTP 422 - "num_inference_steps" must be <= 8, request sent 30

The submission succeeded, the queue reported the job COMPLETED, and only the result body
held the error -- by which time the merge step had already run and blocked. One
`get_model_schema` call before a `submit_job` on an unfamiliar endpoint is far cheaper
than discovering the mistake after the plan finishes.

If you do not check the schema, send only the parameters you are SURE of -- prompt,
duration -- and let the model default everything else. A guessed tuning parameter is the
most common way an async job dies.

Use `recommend_model` only when the user needs something the table does not cover.

## Choosing a model is one step, not a project

Pick from the table and go. Do not chain `search_models` → `recommend_model` →
`get_model_schema` → `get_pricing` before every generation; that turns a single image
into a dozen model round-trips. Reach for `get_model_schema` only when a call has
already failed on its arguments, and `search_models` only when you need something the
table does not cover.
