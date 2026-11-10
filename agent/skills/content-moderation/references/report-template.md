# Final moderation report skeleton (sg5)

**The format is code, not prose:** `scripts/build_report.py` turns a `findings.json`
(schema `content-moderation/findings-v1`, documented in the script and in SKILL.md
sg5 step 4) into `<video-stem>_compliance_report.json` (`content-moderation/v2`) and a
PDF with exactly the six sections below, so every video and every run gets the SAME
report. Never hand-write the report, its JSON keys or its PDF layout; this page only
explains what each section means.

The report merges VISUAL findings (classified from `shot_metadata.jsonl`
descriptions, timestamped by joining `shot_id` → `shots.json`) with SPOKEN
findings (word-list + LLM scan of the diarization transcript), using
`conversation_analysis.json` tone/topic only as supporting corroboration.

Confidence is SOURCE-BASED, not a similarity score: a description that explicitly
depicts a category, or a verbatim transcript quote, is VERIFIED evidence; shot
`sentiment` / segment `tone` is SUPPORTING evidence.

**Section 1 — General Rating System Analysis:** Suggested Rating (one of the
[rating-system.md](rating-system.md) levels), Justification (referencing specific
timestamps + the evidence they come from), Elements (comma-separated).

**Section 2 — Content Compliance Summary:** table of Category | Severity |
Evidence count | Strongest finding — highest severity first.

**Section 3 — Flagged Moments Timeline:** table of Timestamp | Category | Source
(`shot N` for visual, `transcript` for spoken) | Detail (the shot description
excerpt or the verbatim quote). Sorted by timestamp. Visual timestamps come from
`shots.json` (`start_time`/`end_time` for the shot_id); spoken timestamps from
the transcript cue.

**Section 4 — Categories not flagged:** distinguish "attested clean" (the
evidence covered it and found nothing) from "NOT ASSESSED" (the evidence layer
didn't cover it — e.g. no dialogue for LANGUAGE, or shots with empty
descriptions).

**Section 5 — Evidence provenance:** shots enriched (count), transcript scanned
(yes/no + duration), conversation segments used for corroboration. State the
standing limitation: shot descriptions summarize each shot, so a brief or
background visual event can be omitted — absence of a visual flag is not proof of
absence; ESCALATE routes flagged spans to human review.

**Section 6 — Overall Assessment:** Profile (mild / moderate / severe) and
Recommendation:
- CLEAN = no verified findings in any category
- REVIEW = verified findings of moderate severity
- ESCALATE = high-severity verified findings — route the flagged spans to human review
