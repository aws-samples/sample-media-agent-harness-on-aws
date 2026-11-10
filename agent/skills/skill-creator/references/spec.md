# agentskills.io Specification — Field Reference

This is a structured restatement of the [agentskills.io
specification](https://agentskills.io/specification) for quick
lookup during authoring and review. When a rule below conflicts
with the live spec, the live spec wins — open an issue (or update
this file) rather than working from a stale rule.

## Directory structure

A skill is a directory whose name **must match** the `name:` field
in its SKILL.md frontmatter exactly.

```
skill-name/
├── SKILL.md          # Required: frontmatter + instructions
├── scripts/          # Optional: executable code agents can run
├── references/       # Optional: on-demand documentation
├── assets/           # Optional: templates, schemas, images, data files
└── ...               # Any additional files or directories
```

Only `SKILL.md` is mandatory. The three optional directories
(`scripts/`, `references/`, `assets/`) are loaded by the agent
**on demand** — i.e., only when SKILL.md instructions tell it to.
Anything the agent needs immediately on activation must live in
SKILL.md itself.

## SKILL.md format

The file is YAML frontmatter (between `---` markers) followed by
Markdown content. There must be exactly one frontmatter block at
the top of the file.

```
---
name: skill-name
description: What this skill does and when to use it.
---

# Skill Name

Body content in Markdown...
```

## Frontmatter fields

### `name` (required)

| Constraint | Rule |
|---|---|
| Length | 1 – 64 characters |
| Charset | Unicode lowercase alphanumeric (`a-z`, `0-9`) and hyphens (`-`) only |
| Leading/trailing | Must not start or end with `-` |
| Repetition | Must not contain consecutive hyphens (`--`) |
| Parent directory | Must match the parent directory name exactly |

Valid: `pdf-processing`, `data-analysis`, `code-review`, `aws-research`

Invalid:
- `PDF-Processing` (uppercase)
- `-pdf` (leading hyphen)
- `pdf-` (trailing hyphen)
- `pdf--processing` (consecutive hyphens)
- `pdf_processing` (underscore not in charset)

### `description` (required)

| Constraint | Rule |
|---|---|
| Length | 1 – 1024 characters |
| Content | Non-empty; describes both what the skill does *and* when to use it |
| Trigger keywords | Should include specific keywords agents can match against user requests |

**Good** (213 chars):

```
description: Extracts text and tables from PDF files, fills PDF forms, and merges multiple PDFs. Use when working with PDF documents or when the user mentions PDFs, forms, or document extraction.
```

This works because:
- Says **what** ("Extracts text and tables...")
- Says **when** ("Use when working with PDF documents...")
- Carries trigger keywords (`PDF`, `forms`, `extraction`, `merges`)

**Poor**:

```
description: Helps with PDFs.
```

Why it fails:
- No "when to use"
- No trigger keywords beyond `PDFs`
- The model cannot distinguish "do you handle PDFs?" from "extract this PDF" — both match equally poorly

### `license` (optional)

The license applied to the skill. Keep it short — either the name
of a known license or a reference to a bundled license file.

```
license: Apache-2.0
license: Proprietary. LICENSE.txt has complete terms
```

### `compatibility` (optional)

| Constraint | Rule |
|---|---|
| Length | 1 – 500 characters |
| When to include | Only if the skill has specific environment requirements |

Indicates intended product, required system packages, network
access needs, etc. **Most skills do not need this field.**

```
compatibility: Designed for Claude Code (or similar products)
compatibility: Requires git, docker, jq, and access to the internet
compatibility: Requires Python 3.14+ and uv
```

When reviewing, flag a `compatibility` field that doesn't actually
state a requirement (e.g., "Works with any agent") — it adds
metadata cost for no value.

### `metadata` (optional)

Arbitrary string-to-string key-value map. Clients use it to store
additional properties not defined by the spec. Key names should be
reasonably unique to avoid conflicts.

```
metadata:
  author: example-org
  version: "1.0"
  source-repo: https://github.com/example-org/skills
```

### `allowed-tools` (optional, experimental)

A space-separated string of tools that are pre-approved to run.
**Support varies between agent implementations** — treat this as
advisory and confirm whether the target runtime honours it before
relying on the field.

```
allowed-tools: Bash(git:*) Bash(jq:*) Read
```

In this codebase the field is not currently consumed; users should
not rely on it as a security boundary.

## Body content

The Markdown body after the frontmatter contains the skill
instructions. **There are no format restrictions.** Write what
helps the agent perform the task.

**Recommended sections** (not mandatory):

- Step-by-step instructions
- Examples of inputs and outputs
- Common edge cases
- Decision points / branches based on user intent

The body is loaded **in full** when the agent decides to activate
the skill. That cost matters — keep the body focused and push
detail into `references/`.

## Optional directories

### `scripts/`

Executable code the agent can run. Scripts should:

- Be self-contained, or clearly document dependencies
- Include helpful error messages on common failure modes
- Handle edge cases gracefully (don't crash on missing files)

Supported languages depend on the agent runtime; Python, Bash, and
JavaScript are common. In this codebase the orchestrator's
`code_interpreter` tool runs Python, and bash runs in the same
sandbox through its `executeCommand` action (there is no host
`shell` tool).

### `references/`

Additional documentation the agent loads on demand. Naming
conventions are flexible; the spec examples show both `REFERENCE.md`
(uppercase) and domain-specific lowercase names (`finance.md`,
`legal.md`). **This repo uses lowercase** (`reference.md`,
`forms.md`) — match that for consistency.

Keep individual reference files focused. Agents load them
on demand, so smaller files mean lower context cost when only one
section is needed.

### `assets/`

Static resources:

- Templates (document templates, configuration templates)
- Images (diagrams, examples)
- Data files (lookup tables, schemas, fixtures)

Assets are referenced from instructions but rarely loaded into
context directly — they're typically read by scripts or attached
to outputs.

## Progressive disclosure

Agents load skills in three tiers, by cost:

| Tier | What | Cost (approximate) | When loaded |
|---|---|---|---|
| Metadata | `name` + `description` | ~100 tokens | At startup, for every installed skill |
| Instructions | Full SKILL.md body | < 5000 tokens recommended | When the skill is activated |
| Resources | Files under `scripts/`, `references/`, `assets/` | Variable | Only when instructions tell the agent to load them |

**Hard guideline:** Keep SKILL.md **under 500 lines**. If you're
approaching the limit, push detail into `references/`.

The agent pays for every active skill's metadata on every turn —
descriptions that are 200 chars instead of 800 chars actually
matter at scale.

## File references

When referencing other files in your skill, use **relative paths
from the skill root**:

```markdown
See [the spec reference](references/spec.md) for details.

Run the extraction script:
scripts/extract.py
```

**Keep file references one level deep from SKILL.md.** Avoid
deeply nested reference chains (e.g., `references/section/topic/details.md`)
— they're harder for the agent to follow and the spec
explicitly recommends against them.

## Validation

The agentskills.io project ships a CLI validator:

```bash
skills-ref validate ./my-skill
```

Source: https://github.com/agentskills/agentskills/tree/main/skills-ref

It checks frontmatter shape and naming conventions. It does **not**
validate content quality — that's what the anti-patterns review
covers ([anti-patterns.md](anti-patterns.md)).

## Quick validation checklist (no tooling required)

Run this checklist on any SKILL.md before declaring it compliant:

- [ ] Frontmatter is present, parseable YAML, between `---` markers
- [ ] `name` is 1-64 chars of `[a-z0-9-]`, no leading/trailing/consecutive hyphens
- [ ] `name` matches the parent directory name exactly
- [ ] `description` is ≤ 1024 chars and includes both "what" and "when"
- [ ] `description` contains trigger keywords agents can match against user prompts
- [ ] `compatibility` (if present) is ≤ 500 chars and states a real requirement
- [ ] Body is under 500 lines and under ~5000 tokens
- [ ] File references are relative and one level deep
- [ ] All tools referenced in the body actually exist in the target runtime
- [ ] No future-state language ("we will support…")
