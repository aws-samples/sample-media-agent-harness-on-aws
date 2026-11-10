# Q&A Flow for New Skill Authoring

This file describes how to interview the user when authoring a
brand-new skill. The objective is to gather enough signal to
write a spec-compliant, well-triggering SKILL.md without making
the user fill out a form.

## Principles

1. **Batch into rounds, never a wall of questions.** Each round
   fits in one turn (3-4 questions max). The user answers, you
   move on.
2. **Skip rounds when you have enough.** Round 3 is conditional —
   skip it for simple skills.
3. **Lead with examples, follow with abstractions.** "Give me 3
   concrete user requests that should trigger this" beats "What
   is the activation criterion?"
4. **Suggest a default, don't demand a decision.** When you have
   a reasonable assumption, state it and ask to confirm rather
   than asking the user to choose blind.
5. **Stop early if the user is impatient.** If the first answer
   gives you 80% of what you need, write a draft and let the
   user correct it. The Q&A is a tool, not a ritual.

## Round 1 — Purpose & triggers (always)

This round establishes what the skill is for and when it should
fire. It is the most important round — get this wrong and the
skill won't trigger, or will trigger on the wrong things.

**Ask:**

1. "What concrete user requests should activate this skill?
   Give me 3-5 example prompts."
2. "What requests should *not* activate it? Anything close that
   you want to keep separate?"
3. "In one line, what does the skill do?"

**Why these questions:**

- The 3-5 examples become the seed for trigger keywords in
  `description`. Without concrete examples the description will
  drift toward vague "helps with X" phrasing.
- Negative triggers are the most-skipped step in skill authoring.
  Without them the skill fires on adjacent requests
  (e.g., a "PDF processing" skill that also fires on "process
  this image"). Force the user to name two negative triggers
  even if they didn't think they had any.
- The one-line summary becomes the `description` opener.

**Output of Round 1:**

- A working `description:` (you compose it; don't ask the user to)
- A working `name:` slug derived from the one-liner
- A list of triggers + anti-triggers you can verify in Round 2

## Round 2 — Tools, dependencies, outputs (always)

This round establishes what the skill *does at runtime* and what
the agent needs to do its job.

**Ask:**

1. "Which tools does this skill use? Pick from built-in
   (`code_interpreter`, `artifact`, `read_result_files`, `http_request`,
   etc.) or name a gateway tool. Don't list tools you're not
   sure exist."
2. "What does the skill produce as output? A file, a JSON
   structure, a written report, a tool dispatch?"
3. "Any external dependencies? Network access, system packages,
   API credentials, libraries the agent doesn't have by default?"

**Why these questions:**

- Tool selection is the place where authors most often fabricate
  references — listing tools the runtime doesn't have. Asking the
  user to commit to a tool list early forces a reality check.
- The output shape becomes the body's "Output Format" section.
  Skills with vague output specs produce inconsistent results.
- Dependencies map onto the optional `compatibility:` frontmatter
  field. If the answer is "none unusual", skip the field — most
  skills don't need it.

**Output of Round 2:**

- A list of tool names that goes into the body's "Tools" section
- The skill's output contract
- A `compatibility:` value (or confirmation it's not needed)

## Round 3 — Edge cases & supporting files (conditional)

**Skip this round when:**

- The skill is a single-step transformation (e.g., "summarize a
  doc")
- Rounds 1+2 already covered failure modes
- The user has expressed time pressure or impatience

**Run this round when:**

- The skill has multiple branches based on user intent
- The skill involves long-running operations (async jobs,
  retries)
- The user mentioned a methodology, framework, or process that
  warrants its own reference file

**Ask:**

1. "What failure modes should the skill handle explicitly? (e.g.,
   missing files, network errors, malformed input, ambiguous user
   intent)"
2. "Are there long reference materials (a spec, a methodology, a
   list of allowed values) that belong in `references/` rather
   than the SKILL.md body?"
3. "Any reusable scripts that should live in `scripts/`? (Code
   the agent runs verbatim, not pseudocode in the body.)"

**Why these questions:**

- Explicit failure handling separates production-quality skills
  from prototypes.
- Reference files are the lever for keeping SKILL.md under 500
  lines on non-trivial skills. Pulling the long content out of
  the body early is easier than refactoring later.
- Scripts vs. body code: code in the body documents intent;
  scripts are run. Don't conflate them.

**Output of Round 3:**

- An edge-cases section in the body
- A list of `references/*.md` files to create (titles + content)
- A list of `scripts/*` files to create (paths + purposes)

## After the rounds

1. Synthesize the SKILL.md following the spec
   ([spec.md](spec.md)) and avoiding the smells
   ([anti-patterns.md](anti-patterns.md)).
2. Show the result to the user. Don't persist before they've
   seen it.
3. Iterate on the user's feedback. Common fixes: tightening the
   description, adding a negative trigger they think of after
   reading the draft, splitting a body section out into a
   reference file.
4. Persist via `create_skill_draft(name=..., skill_md=...)`.
5. Report the assetId and tell the user how to publish:
   `publish_asset(category="skill", asset_id=<id>, scope="private")`

## Worked example flow

User: *"Help me make a skill for exporting query results from our
analytics warehouse to CSV or Excel."*

**Round 1:**

> What user requests should activate this skill — 3-5 examples?
> What should *not*? And in one line, what does it do?

User answers with five export requests, one anti-trigger ("don't
fire on 'show me the data', that's just a SELECT"), and a
one-liner.

**Round 2:**

> Which tools? I'm assuming `code_interpreter` for the
> warehouse query + format conversion, and `artifact` to return
> the file. Confirm or correct.
> What's the output — a CSV file, an Excel file, or do you let
> the user pick?
> Any dependencies — does the warehouse need creds, or is it
> wired through a gateway?

User confirms tools, picks "let the user choose CSV/XLSX",
mentions creds are already in the runtime's env.

**Round 3 — skip.** This is a single-purpose skill, no methodology
to capture, no scripts complex enough to externalize.

**Synthesize → show → persist.** Three rounds → two questions
each → done in three turns.

## Pitfalls

- **Asking everything in one turn.** The user sees a wall of
  questions and answers tersely. You miss signal.
- **Demanding format decisions early.** "Should the description
  be under 200 chars?" — the user has no opinion. Decide for them.
- **Forgetting negative triggers.** Round 1 is incomplete without
  at least two anti-trigger examples. Push if the user shrugs.
- **Skipping Round 2 on the assumption that "tools come later".**
  Tool selection is the most common place skills go wrong —
  fabricating tools, missing the right tool, listing too many.
  Always run Round 2.
- **Treating Round 3 as mandatory.** Many skills don't need it.
  Running it when the skill is trivial pads the conversation and
  pads the eventual body.
