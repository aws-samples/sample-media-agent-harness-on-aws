---
name: content-moderation
description: "Analyzes video content for moderation flags with timestamped findings. Orchestrates video-processing capabilities and reads the pipeline's structured evidence — per-shot visual descriptions from shot-metadata-enrichment, the speaker-attributed transcript from diarization, and topic/tone segments from conversation-analysis — then classifies under a fictional General Rating System. Use when the user wants to detect violence, harm, language, or other flagged content and see exactly when it occurs, review a video for broadcast/distribution readiness, or ask what rating content would get. NOT for transcription-only, shot detection, or media generation on their own."
allowed-tools: plan_and_execute_tool shot-segmentation___segment_shots shot-segmentation___get_job_status speech-diarization___diarize_speech speech-diarization___get_job_status shot-metadata-enrichment___enrich_shot_metadata shot-metadata-enrichment___get_job_status conversation-analysis___analyze_conversation conversation-analysis___get_job_status media-outputs___get_download_url media-outputs___get_download_urls
---

# Content Moderation

Analyze a video for moderation flags and produce a timestamped report of what was
detected and when. Cover what is SEEN (weapons, violence, substances, nudity,
etc.) and what is SAID (profanity, threats, slurs).

**Evidence model — read this first.** This skill does NOT do its own keyframe
indexing or visual search. The pipeline already EXTRACTS structured evidence;
this skill READS it and produces the VERDICT:

1. **Per-shot visual evidence** — `shot-metadata-enrichment` writes
   `shot_metadata.jsonl`: one JSON per shot with **`depicted[]`** — a closed
   moderation vocabulary as `{act, detail}` (e.g. `alcohol`, `firearm`,
   `physical_violence`, `blood`, `smoking`, `nudity_partial`, `weapon_threat`,
   `drug_use`) — PLUS free-text `description`, `objects[]`, `actions[]`,
   `expressions[]`, `notable[]`, `brands[]`, `sentiment`, `people[]`,
   `texts_on_screen[]`. `depicted` is the PRIMARY moderation signal; the free-text
   fields corroborate and catch anything outside the vocab. Timestamps come from
   joining `shot_id` to shot-segmentation's `shots.json`.
2. **Spoken content** — the diarization transcript (`final_transcript.json`):
   scan verbatim text with a word-list + an LLM pass for profanity, threats,
   hate/slurs, sexual and substance references, with timecodes.
3. **Topic/tone context** — `conversation-analysis` writes
   `conversation_analysis.json` (`audio_segments[]` with `segment_topic`,
   `sentiment`, `tone`, `key_events`). This is SUPPORTING corroboration only.

`depicted` gives a closed moderation vocabulary directly; still run the LLM
classification over `description`/`objects`/`actions`/`expressions` to catch
anything the closed vocab misses. `conversation_analysis.json` carries
`discourse_facts`, a closed spoken-moderation vocabulary (see the conversation-analysis
reference): use it as spoken evidence, and still scan the transcript for what it misses.

Inputs are videos already in the M2C bucket. All M2C tools are async: after
submitting, poll the TARGET's own `get_job_status` with the returned `job_id`
until complete before a dependent step reads its output. Every workflow is
idempotent — re-submitting an already-run asset absorbs into the existing run.

## Hard constraints

- Use ONLY the fictional General Rating System in
  [references/rating-system.md](references/rating-system.md). NEVER use any
  real-world rating (MPAA, BBFC, PEGI, FSK, NICAM, ACB) or levels like
  G / PG / PG-13 / R / NC-17.
- Name the plan `plan-content-moderation` (never `plan-mpaa` or similar).
- **Reading a completed job's output (ANY node that does this):** plan nodes
  CANNOT raw-read the M2C proxy bucket, and the inline job-result **preview is
  TRUNCATED**. So the reader node MUST carry `media-outputs___get_download_url`
  (plus `http_request`), PRESIGN the exact output `s3://` key, and read the FULL
  file — never extract values from the preview, never try to read the raw key. A
  node given only a job's START tool (`diarize_speech`, `segment_shots`,
  `enrich_shot_metadata`, `analyze_conversation`) is misconfigured — those launch
  a NEW job, they do not read results (session 62fa161a: a reader node with only
  the start tool got a truncated preview and failed).
- Do NOT SPLIT a stage into "start" + "report"/"read" nodes. Each stage below is
  EXACTLY ONE node. sg1/sg2/sg3/sg4 are single async job nodes; the node that
  CONSUMES their outputs is sg5 (the report), which carries the presign+read
  tools.
- **Wiring an upstream transcript into a later job — use the EXACT key, never a
  hand-built path.** sg3 (`enrich_shot_metadata`, optional `transcript_s3uri`) and
  sg4 (`analyze_conversation`, required `transcript_s3uri`) both consume the
  diarization transcript. Its key is
  `micro-workflow/speech-diarization/<uuid>/transcripts/final_transcript.json` —
  resolve it from sg2's diarization result (`outputs[]` entry labeled `transcript`),
  do NOT construct `micro-workflow/speech-diarization/<uuid>/final_transcript.json`.
  Dropping the `transcripts/` segment yields a nonexistent key and the job fails
  immediately with `NoSuchKey` at `validate_plan`, failing the whole plan
  (observed: session f9638739, sg4). Same rule for any upstream `*_s3uri` input:
  take the exact key from the producer's result, never guess the path. In GUIDED
  mode the producer runs in an EARLIER phase's plan, so it cannot be a `depends_on`
  of a later phase — reference it via `external_inputs` (see the guided phases
  below) and use the injected uri. NEVER hand-build the path across a phase
  boundary; that is exactly the wrong-uuid failure this rule exists to prevent
  (session 368f1233).
- **Every async node SUBMITS its M2C job ONCE and RETURNS the job handle — it does NOT
  inline-poll to `SUCCEEDED`.** sg1/sg2/sg3/sg4 each launch an async M2C Step Functions job
  whose output a LATER stage reads. RETURN the job's execution handle (the `arn:aws:states:…`
  ARN the submit yields) as the node's result and let the node finish. The harness registers
  that SFN as a PENDING job, holds the node in `processing`, and does NOT dispatch the dependent
  stage (sg5) or mark the plan complete until every upstream SFN is terminal — so sg5 always
  reads outputs that already exist. Do NOT loop on `get_job_status` until `SUCCEEDED`: a long job
  (enrich of ~94 shots) blows the 600s node budget, and — worse — a node that reports `completed`
  while its SFN is still RUNNING (observed: sg3 finished at ~50s while its enrich SFN kept running)
  leaves the coordinator with NO pending SFN to wait on, so the plan is marked terminal early and
  sg5 reads a `shot_metadata.jsonl` / `conversation_analysis.json` not yet written. Returning the
  SFN handle (never swallowing it into a prose summary) is what lets the harness gate the
  dependents on the REAL job status — the same fire-and-forget pattern video-processing /
  agent-olive use. The reader stage (sg5) runs only after those SFNs are terminal, so it is the
  node that presigns + reads the finished outputs.
- **The completion predicate DIFFERS by node kind — do not apply one to the other.**
    - **Async SUBMIT nodes (sg1/sg2/sg3/sg4):** COMPLETE the moment the node RETURNS
      its SFN handle (`arn:aws:states:…`). It must NOT wait for `SUCCEEDED` and must
      NOT resolve/read output keys — the job is still running and its output object
      does not exist yet. Returning only `{uuid, total_estimated_cost}` (with the
      handle swallowed into a prose summary) IS the failure: the harness then has no
      SFN to gate on and marks the plan terminal early. The fix is to surface the
      handle, not to poll. Each stage's job WRITES the object below, which sg5 later
      reads — so this list is the READER's contract, not a per-node completion gate:
        - sg1 → `<prefix>/embeddings/shots.json`
        - sg2 → `<prefix>/transcripts/final_transcript.json`
        - sg3 → `<prefix>/shot_metadata.jsonl`
        - sg4 → `<prefix>/conversation_analysis.json`
    - **Reader/report node (sg5):** COMPLETE only when it has SURFACED its declared
      outputs — it runs after every upstream SFN is terminal, so it MUST presign + read
      the FULL files above (never the truncated preview), then write and record the
      EXACT report artifact key(s) it produces. Returning a uuid or a summary without
      the written report key is a FAILED sg5. (Observed: when sg3/sg4 tried to carry
      output keys themselves they finished before their SFN, so sg5 read files not yet
      written and the report was empty — hence sg1–sg4 return handles only, sg5 owns the
      reads.)
- **Self-check before reporting `completed`.** A node may report completion ONLY if
  it can answer YES to the checks for its kind:
    - Async submit node (sg1–sg4):
        1. Did I SUBMIT the M2C job exactly once (no inline polling)?
        2. Is the SFN handle (`arn:aws:states:…`) surfaced as the node result, not
           buried in a prose summary?
    - Reader/report node (sg5):
        1. Did I presign + read the FULL upstream file(s) (not the truncated preview)?
        2. Can I name the EXACT report artifact key(s) I wrote to the bucket?
        3. Is `suggested_rating` a member of the four-value enum?
  If any answer is NO, the node is INCOMPLETE — keep working or surface the failure;
  do NOT report success. A plan that reports SUCCEEDED while sg5's report is missing
  is a false success and must be treated as a bug.

## When to activate

- Analyze a video for content moderation or compliance
- Identify flagged content with timestamps
- Produce a moderation report of what was detected and when
- Flag specific moments (violence, harm, language, etc.)
- Review content for broadcast or distribution readiness
- "What rating would this content get?"

## Do NOT activate when

- The user wants only a transcript / diarization with no moderation judgment,
  only shot detection / enrichment, or only topic segmentation — that is plain
  video-processing.
- The user wants to GENERATE or edit media — that is media-references (fal.ai).
- The user asks about an already-completed moderation job's results — just
  retrieve and summarize; do not re-run the pipeline.

## Execution modes

Default is **step-by-step (guided)** — run the pipeline as a sequence of
per-phase DAGs (each phase is one tracked plan with its own parallel tasks),
showing the result and waiting for the user's go-ahead after each phase. Switch
to **end-to-end** only when the user explicitly asks ("run the whole thing", "do
it all automatically", "end to end", "don't stop between steps").

An OUTCOME request is NOT an automation request. "Produce a moderation rating /
report", "what rating would this get", "moderate this video" all describe the
GOAL, which BOTH modes achieve — so do NOT read them as "run end-to-end". When
the request names an outcome but no explicit mode, ASK ONE quick question first —
"Want me to walk through it stage-by-stage (guided), or run the whole pipeline
automatically end-to-end?" — and only then proceed. Never silently launch the
full end-to-end plan off an outcome request.

This mode question is NOT waived by remembered or long-term preferences, saved defaults, or a
choice made in a prior session: only an explicit statement in THIS conversation answers it. The
remembered choice may be offered as the suggested answer ("Last time you ran it end-to-end, same
again?").

### Guided (default) — one DAG per PHASE, pause between phases

Each phase is ONE `plan_and_execute_tool` plan (a mini-DAG with parallel nodes);
show that phase's result and WAIT for the go-ahead before the next. Each phase
REUSES the prior phases' completed outputs — never re-run a finished stage.

Guided runs the SAME logical DAG (sg1∥sg2 → sg3∥sg4 → sg5) as THREE separate
plans, one per phase, so you can review between phases. Because each phase is its
OWN plan, a later phase's node CANNOT `depends_on` an earlier phase's node — that
edge is rejected (it is not a node in this plan). Instead, a node that consumes an
EARLIER phase's output declares `external_inputs` — `{"plan": "<producer phase's
exact plan name>", "node": "<producer node id>"}` — and the engine injects that
producer's EXACT output uris (transcript file, shots prefix, etc.) into the node's
prompt. NEVER hand-build a cross-phase uri from a job uuid; that reused the wrong
upstream uuid and failed `NoSuchKey` (session 368f1233). Use these EXACT plan names
and node ids so the references resolve:

- **Phase 1 — Foundation.** ONE plan named EXACTLY `plan-content-moderation-foundation`:
  `sg1_segment_shots` ∥ `sg2_diarize_speech` (both no deps, run in parallel). These
  are the two async foundation jobs. Pause after the plan finishes; show shot count
  + transcript summary and confirm both SUCCEEDED.
- **Phase 2 — Analysis.** ONE plan named EXACTLY `plan-content-moderation-analysis`:
  `sg3_enrich_shot_metadata` ∥ `sg4_analyze_conversation` (parallel). They consume
  Phase 1 via `external_inputs` (NOT `depends_on`):
    - sg3_enrich_shot_metadata:
          "external_inputs": [
            {"plan": "plan-content-moderation-foundation", "node": "sg1_segment_shots"},
            {"plan": "plan-content-moderation-foundation", "node": "sg2_diarize_speech"}
          ]
      Set `shot_segmentation_s3uri` from sg1's injected output prefix and
      `transcript_s3uri` from sg2's injected `final_transcript.json` — both come
      from the injected block; do NOT construct either.
    - sg4_analyze_conversation:
          "external_inputs": [
            {"plan": "plan-content-moderation-foundation", "node": "sg2_diarize_speech"}
          ]
      Set `transcript_s3uri` from sg2's injected `final_transcript.json` uri.
  Pause after the plan finishes; confirm sg3/sg4 SUCCEEDED.
- **Phase 3 — Report.** ONE plan named `plan-content-moderation-report` with a single
  node sg5. It reads shots + transcript + enrichment + conversation, all from prior
  phases, via `external_inputs`:
        "external_inputs": [
          {"plan": "plan-content-moderation-foundation", "node": "sg1_segment_shots"},
          {"plan": "plan-content-moderation-foundation", "node": "sg2_diarize_speech"},
          {"plan": "plan-content-moderation-analysis", "node": "sg3_enrich_shot_metadata"},
          {"plan": "plan-content-moderation-analysis", "node": "sg4_analyze_conversation"}
        ]
  The engine injects the EXACT `shots.json`, `final_transcript.json`,
  `shot_metadata.jsonl` and `conversation_analysis.json` uris. sg5 presigns + reads
  THOSE injected uris verbatim — it NEVER builds a `speech-diarization/<uuid>/...`
  path from a job uuid. If an injected input has no DELIVERABLE FILES line, report that
  dependency as missing and stop — a presign of a guessed key returns 403 (S3 hides a
  missing key behind AccessDenied), so guessing never "works around" it. Then classify
  and produce the timestamped report + rating exactly as sg5 does end-to-end.

**Several videos (N > 1) — one plan per PHASE, one node per VIDEO.** Never run one
video's pipeline ahead of the others, and never make one plan per video: the phase
plans share a name, so a later phase cannot tell which video's producer it means —
the engine now FAILS such a node ("ambiguous external_inputs") instead of guessing
(session d6a560a8 handed NBA's shots and transcript to all three videos). Instead:
- Give every node a per-video suffix `__<v>`, where `<v>` is a short stable tag of
  that video (the first 8 hex of its asset id, e.g. `__4b8511d6`): Phase 1 is ONE
  plan `plan-content-moderation-foundation` with `sg1_segment_shots__<v>` ∥
  `sg2_diarize_speech__<v>` for EVERY video (2N parallel nodes); Phase 2 is ONE plan
  `plan-content-moderation-analysis` with `sg3_enrich_shot_metadata__<v>` ∥
  `sg4_analyze_conversation__<v>` per video; Phase 3 is ONE plan
  `plan-content-moderation-report` with one `sg5_report__<v>` per video. Each node's
  `external_inputs` names ITS video's producer nodes (`"node": "sg1_segment_shots__<v>"`).
- Each sg5 node reads only its own video's injected inputs and writes
  `<video-stem>_compliance_report.json` / `.pdf` (see sg5 step 5), so N reports never
  share a key. Phase 3 is still a PLAN (each node already runs isolated, in its own
  Task Agent turn) — never a direct dispatch, which cannot resolve `external_inputs`.
- One "show results" per phase covers every video. When rendering the reports, pass
  ALL N PDFs together; the viewer shows them as tabs — say so only after the render
  reports all N.

**Re-running a phase (any number of videos).** A re-run of a phase that FAILED may keep
its name — the engine ignores a failed attempt's producers. A re-run of a phase that
COMPLETED (or is still running) must get a fresh plan name (e.g.
`plan-content-moderation-analysis-r2`), and the next phase must reference THAT name;
otherwise two live plans hold the node and the next phase fails as ambiguous.

### End-to-end (opt-in)

When the user asks to run automatically, run the WHOLE pipeline as ONE
`plan_and_execute_tool` plan named `plan-content-moderation` — all 5 stages as a
single DAG (sg1 ∥ sg2 → sg3 ∥ sg4 → sg5). Do NOT collapse the report into an
earlier node, and do NOT split any stage into start/report nodes (see the hard
constraints).

## Pipeline steps

Both modes use the same 5 stages and dependency graph:

```
sg1: segment_shots ─────────────┐ (parallel, no deps)
sg2: diarize_speech ────────────┘ (parallel, no deps)
                                |
sg3: enrich_shot_metadata ──────┤ (depends on: sg1, sg2)
sg4: analyze_conversation ──────┤ (depends on: sg2)
                                |
sg5: classify + report ─────────┘ (depends on: sg3, sg4, sg2)
```

- **sg1 — Segment (async).** `shot-segmentation___segment_shots` on the source
  video → `shots.json` (per-shot `shot_id` + `start_time`/`end_time` — the
  timestamp authority for every visual finding), embeddings. No deps.
- **sg2 — Transcribe (async).** `speech-diarization___diarize_speech` on the
  source video → speaker-attributed transcript at
  `<output_s3_prefix>/transcripts/final_transcript.json`. No deps.
- **sg3 — Enrich shots (async).** `shot-metadata-enrichment___enrich_shot_metadata`
  with `shot_segmentation_s3uri` = sg1's `output_s3_prefix` and
  `transcript_s3uri` = sg2's transcript (dialogue context). **Poll
  `shot-metadata-enrichment___get_job_status` until `SUCCEEDED` before this node
  completes** — enrich takes several minutes and sg5 needs the finished output.
  Output: `shot_metadata.jsonl` (per-shot `depicted[]` `{act, detail}` +
  `description`/`objects`/`actions`/`expressions`/`notable`/`brands`/`sentiment`/
  `people`/`texts_on_screen`). Depends on sg1 (+ sg2 for the transcript pointer).
  See
  [video-processing shot-metadata-enrichment reference](../video-processing/references/shot_metadata_enrichment_agent.md).
- **sg4 — Analyze conversation (async).** `conversation-analysis___analyze_conversation`
  with `transcript_s3uri` = sg2's transcript. Output:
  `conversation_analysis.json` (`audio_segments[]` topic/tone/sentiment/key_events)
  — SUPPORTING context. Depends on sg2. Runs parallel with sg3. See
  [video-processing conversation-analysis reference](../video-processing/references/conversation_analysis_agent.md).
- **sg5 — Classify + report.** No gateway tool; MUST be provisioned with
  `media-outputs___get_download_url` + `http_request` + `code_interpreter`
  (all three — `http_request` fetches the presigned URL, `code_interpreter`
  writes the report). In this ONE node:
  1. **Visual findings.** Presign + read the FULL `shot_metadata.jsonl` (sg3) AND
     `shots.json` (sg1). For each shot, take its **`depicted[]`** `{act, detail}`
     as the PRIMARY moderation evidence (map each `act` to a category below), and
     ALSO scan `description`/`objects`/`actions`/`expressions`/`notable`/`brands`
     for anything outside that vocab. Attach the shot's `start_time`/`end_time`
     from `shots.json` (join by `shot_id`). A `depicted` act — or a description
     that explicitly depicts a category — is VERIFIED; `sentiment`/`expressions`
     alone are SUPPORTING.
  2. **Spoken findings.** Presign + read the FULL diarization transcript (sg2).
     Run the profanity word-list (verbatim hits, confidence 1.0) AND an LLM scan
     for threats, hate/slurs, sexual and substance references — with exact
     timecodes and verbatim quotes (re-verify each quote is a substring of the
     transcript).
  3. **Corroboration.** Presign + read `conversation_analysis.json` (sg4); use
     `audio_segments[]` `tone`/`sentiment`/`key_events` as SUPPORTING signal only,
     never as a standalone finding.
  4. **Classify into `findings.json` (your ONLY judgement step).** Write every
     finding in this FIXED schema (`content-moderation/findings-v1`) — no other
     fields, no invented category names:

         {"asset": {"video_tag": "<v>", "file_name": "<video file name>", "duration_sec": <s>},
          "evidence": {"shots_enriched": <n>, "shots_without_description": <n>,
                       "transcript_scanned": true|false, "transcript_duration_sec": <s>|null,
                       "transcript_sentences": <n>, "conversation_segments": <n>},
          "findings": [{"start": <s>, "end": <s>, "category": "<CATEGORY>",
                        "severity": "mild"|"moderate"|"strong", "source": "shot <N>"|"transcript",
                        "detail": "<short>", "evidence": "<verbatim quote / description excerpt>",
                        "act": "<the shot's depicted[] act, verbatim>"|null,
                        "confidence": "verified"|"supporting"}],
          "corroboration": [{"start": <s>, "end": <s>, "tone": "...", "topic": "..."}],
          "not_assessed": ["<CATEGORY the evidence could not cover>"]}

     `category` is one of the EIGHT canonical categories under "Moderation
     categories" (VIOLENCE, HARM, LANGUAGE, SEXUAL, SUBSTANCE, FRIGHTENING,
     DISCRIMINATION, IMITABLE). A visual finding MUST carry its `act` quoted from
     `depicted[]`; a spoken finding has `act: null`. Assign `severity` consistently:
     `strong` = graphic/explicit (graphic violence or gore, strong profanity/slurs,
     sexual content/nudity, hard-drug use, a weapon aimed at or fired toward a
     person, assault causing injury); `moderate` = mild language, thematic
     elements, brief intense/frightening scenes, alcohol/tobacco, a weapon merely
     present/carried; `mild` = passing mild themes. Sentiment/tone-only signals are
     `"confidence": "supporting"`.
  5. **Build the report with the bundled builder — never write report code yourself.**
     The harness stages `scripts/build_report.py` into the sandbox when your cell
     names it. In ONE `code_interpreter` cell, write `findings.json` and run:

         python scripts/build_report.py findings.json --bucket <bucket from S3 CONTEXT> \
             --region <region from S3 CONTEXT> --prefix <session OUTPUT prefix, verbatim> \
             --domain <ARTIFACTS_DOMAIN>

     The builder is the SINGLE source of the report: it applies the deterministic
     severity floors (spoken LANGUAGE is at least `moderate`; the controlled
     `depicted` act sets a minimum, e.g. `drug_material`/`weapon_threat`/`injury` ⇒
     `strong`, `alcohol`/`firearm` ⇒ `moderate`), computes `suggested_rating`
     (highest VERIFIED severity wins: strong ⇒ `"Adult 18+"`, moderate ⇒
     `"Teen 13+"`, mild ⇒ `"Child 7+"`, none ⇒ `"All Ages"` — the four literal
     values of the fictional General Rating System, never a real-world rating), and
     writes `<video-stem>_compliance_report.json` (schema `content-moderation/v2`)
     plus the six-section PDF of
     [references/report-template.md](references/report-template.md), uploading both
     under the session OUTPUT prefix (the sandbox can write ONLY that prefix). It
     prints one JSON line with `json_url` and `pdf_url`. If it exits with
     `INVALID FINDINGS: …`, fix `findings.json` and re-run that one command — never
     hand-write a report or a rating.

     **The node's FINAL output MUST be the two durable URLs** the builder printed —
     NOT the inline JSON, NOT bare S3 keys:

         https://<ARTIFACTS_DOMAIN>/<session OUTPUT prefix><video-stem>_compliance_report.json
         https://<ARTIFACTS_DOMAIN>/<session OUTPUT prefix><video-stem>_compliance_report.pdf

     `<video-stem>` is the video's file name without its extension (`nba_compliance_report.pdf`
     for `nba.mp4`); every report of a session shares the ONE OUTPUT prefix, so always prefix the
     stem, even for a single video (the builder does). "Show results" builds its downloads
     from these URLs. sg5 is NOT complete unless BOTH files exist AND their URLs are the
     node's output. Do NOT hand-write a render manifest; auto-render handles the files.
  Depends on sg3 + sg4 (+ sg2's transcript). Carries
  `media-outputs___get_download_url`; NEVER reads from a truncated preview.

## Moderation categories

Classify each shot description / spoken line into these categories (used in the
report; not tied to any real rating body):

| Category | Detect in shot `description`/`objects`/`notable` | Detect in transcript |
|---|---|---|
| VIOLENCE | weapons, fighting, blood/gore, gunfire, injury, corpses | threats of violence |
| HARM | dangerous acts, reckless driving, peril, stunts | instructions for dangerous/criminal acts |
| LANGUAGE | on-screen slurs/profanity in `texts_on_screen` | profanity word-list + slurs (primary) |
| SEXUAL | nudity, intimate contact, suggestive content | sexual references |
| SUBSTANCE | drugs, alcohol, smoking, paraphernalia | substance references |
| FRIGHTENING | horror, disturbing imagery, intense peril (+ `sentiment`) | menacing/distressing speech (+ `tone`) |
| DISCRIMINATION | hate symbols, stereotyping imagery | hate/discriminatory language |
| IMITABLE | replicable dangerous techniques | crime/technique instructions |

## Scoping the run

| User says | Action |
|---|---|
| "Analyze this video for content compliance" | Full pipeline, all 5 steps |
| "Check for violence in this clip" | Full pipeline; report VIOLENCE + HARM only |
| "Flag any inappropriate content with timestamps" | Full pipeline, all categories |
| "What rating would this content get?" | Full pipeline; emphasize the rating in sg5 |
| "Is there any profanity in this video?" | sg2 + the sg5 transcript scan only |
| "Walk me through it one stage at a time" | Guided (per-phase), all steps |
| "Generate a moderation report" | Full pipeline, all steps |

## References

- [references/rating-system.md](references/rating-system.md) — the fictional
  General Rating System and its assignment rules.
- [references/report-template.md](references/report-template.md) — the final
  report skeleton.
