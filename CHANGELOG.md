# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.2.1] — the guardrail checks only what you wrote

### Fixed
- **The platform guardrail blocked ordinary messages.** When long-term memory added past context to a message, the
  guardrail checked that harness-added text too, read its instructions as a prompt attack, and replaced the reply
  with "This request was blocked by the platform's content guardrail." It now checks only the words a person wrote
  (the chat message, or an external caller's prompt). The orchestrator's instructions to sub-agents and plan nodes
  are no longer checked either: the person's words behind them already were. Real prompt-injection attempts are
  still blocked.

## [1.2.0] — the agent manages projects, one platform guardrail, host-exec tools removed

### Security
- **No host-exec tools on any agent.** The Main and Task agents no longer bind `shell`, `file_read` or
  `load_tool` (from `strands-agents-tools`, which is removed). With tool consent bypassed, any prompt or
  injected text could run commands with the runtime's role. `http_request` is rebuilt on the harness's URL
  guard: every redirect hop is vetted, and it never signs a request with the runtime's credentials. The code
  interpreter tool is vendored with the same tool spec. A test fails if a host-exec tool is ever bound again.
- **Dependency updates:** Pillow 13.0.0 and pypdf 6.21.4 (seven denial-of-service advisories), in the runtime,
  CI and the ingestion Lambdas.

### Added
- **The agent manages your projects.** `list_projects`, `switch_project`, `create_project` and `delete_project`.
  Switching or creating opens a NEW chat in that project and sends it your request, so a skill can say "switch
  to workshop-lab01 and continue". Nothing in the current chat moves. Deleting only shows a card: you type the
  project's exact name to confirm, and only the owner can delete. The agent calls the same `/projects` API as
  the browser, with your sign-in, so it has exactly your rights.
- **One Bedrock guardrail for the whole platform.** Admins (`gateway-admins`) choose it in **Settings → Safety**:
  the platform default, a pinned version of any guardrail in the account, or none. Every reply of the Main and
  Task agents runs with it, including sub-agents, plan nodes and external custom-agent calls. A blocked reply
  says so in the chat. Staging and prod deploys create a light default (prompt attacks blocked on input; other
  content categories only when clearly harmful, so film analysis and moderation reports pass). Prompts sent to
  media-generation tools are not covered.
- **Built-in video-ad-creator skill:** a generic, spec-driven ad engine.

### Changed
- **One file sets every default model:** `agent/src/model/models.json`. The orchestrator defaults to Claude
  Opus 5.5 (reasoning effort low); task, planning, render, lab chat and the Improve judge use Claude Sonnet 5.5.
  The task agent used to run Opus 5 because of a deploy-time override, and lab chat and the judge were still on
  Sonnet 5.
  **Removed deploy parameters:** `-c defaultModelId` and `-c reasoningEffort` no longer exist. Set the models and
  `reasoning_effort` in `agent/src/model/models.json` instead.

### Fixed
- **A chat whose project was deleted** used to be stuck ("This chat already belongs to another project"). It now
  ends: your next message opens a new Personal chat with the message in its box and a line saying why.
- **A Browse tab for a deleted project** says the project no longer exists, and stops polling it.
- The `content_moderation` skill folder is renamed `content-moderation` to match its name.

## [1.1.1] — chat images at a sensible size

### Fixed
- **Chat images fit by their longer side.** An image in chat is scaled down to fit the bubble's width and a
  380 px height, whichever runs out first, and is never enlarged past its own size. A small portrait (a
  204×271 face crop) used to be stretched to the full bubble width and stood about 840 px tall; it now shows
  at its own size. Wide images still span the bubble, and the card shrinks to the picture.

### Added
- **A preload runbook** (`docs/preload-runbook.md`): how to build a package, where to upload it, the deploy
  flags, what a rerun changes, verification and troubleshooting. Packages that contain video should be
  uploaded as a folder rather than a `.zip`, because the deploy-time function reads a zip into memory.

## [1.1.0] — asset lineage, entity search, frame-accurate clips

### Added
- **Asset lineage.** The harness records what produced each project asset (gateway tools, sub-agents, plan
  nodes and code cells) and from which inputs, serves it through a lineage read API, and backfills it for
  existing assets. The Projects **Lineage** view, marked Beta, draws the chain for a whole project or for one
  asset (⋯ → Lineage), with external sources, the chat each output came from, and previews. Off by default;
  turn it on with the `projects.lineageEdges` environment setting.
- **People, characters and segments in search.** With `projects.ingestion.entityMetadata` on, video ingest
  runs face recognition, cast resolution and program-structure analysis, and folds the people, characters
  and segments they find into each scene's searchable entities, so a search can find the shots a person or
  character appears in. Off by default; turn it on only where the toolkit gateway exposes those three
  workflows. The workflows are best-effort: one that fails is reported and never fails the asset.
- **Deploy-time preload.** A package in the Ingest bucket creates its owner, projects, assets and skills
  during `cdk deploy`, before anyone signs in. The package holds one folder per project: a `manifest.json`
  (`{"version": 2, "name": …, "indexing": …}`), an `assets/` folder whose files are all loaded by default
  (subfolders become project folders; `assets` in the manifest takes `*` / `**` patterns and explicit
  entries), and an optional `skills/SKILL.md` that becomes the project's skill. Upload it as a folder or a
  `.zip`. Inactive unless the deploy passes `-c ownerEmail=`; `-c preloadPath=` names the package (default
  `preload/`), and `-c preloadNonce=` forces a rerun after the package changes. Reruns do not duplicate
  users, projects or assets; a changed skill is published as a new version of the same skill (including a
  skill of that name the owner already published), and an unchanged one is left alone. An item that fails (a skill that does not validate, a missing file) is
  skipped and logged by the `<projectName>-preload` function; the deploy still succeeds.
- **A demo video in every Personal project.** Every user finds `0-demo.mp4`, a short screen recording of the
  harness used by the in-app tour, pinned first in their Personal project. It ships in the repository
  (`infra/assets/samples/`), the deploy uploads it once, and each user gets their own copy the first time
  their project list loads. Personal projects don't index, so the copy is not analyzed. A user who deletes
  it doesn't get it back. Turned on by `projects.ingestion.personalSamples`.

### Changed
- **Default models.** The orchestrator uses Claude Opus 5.5 (`global.anthropic.claude-opus-5-5`). The task,
  planning and render sub-agents use Claude Sonnet 5.5.
- **The Design agent is retired.** The planning agent designs and plans a DAG in one call and retries itself
  on a validation error, so there is one fewer AgentCore runtime; a deploy removes the old one.
- **Faster video ingest.** With entity metadata on, face recognition starts in the first wave, alongside
  shot segmentation, diarization and frame capture.
- **Frame-accurate clips and frames.** In and out points snap to the source's frame grid, and clips are
  re-encoded by default so they start and end on the marked frames. `fast: true` keeps the faster stream
  copy, which cuts at the keyframe before the in point.
- **Images in chat keep their shape.** An image card shows the whole picture at its own aspect ratio, scaled
  to the chat bubble's width, instead of cropping it to 16:9.

### Fixed
- **fal model calls follow each model's own schema.** The harness no longer adds `prompt_expansion_mode` to
  every `minimax/h3-max/*` call; that made every `minimax/h3-max/recast` submit fail, because recast does not
  accept the field. Instead, a submit (`submit_job`, `run_model`, and each request of `submit_jobs`) is
  checked against the schema the model publishes through `get_model_schema`. An unknown field, a missing
  required field or a value outside the allowed list is refused before anything is sent or billed, with the
  fields the model accepts. If the schema cannot be read, the call goes through unchecked;
  `MODEL_INPUT_CHECK=off` turns the check off.
- An inline image in chat that is named by its file name now resolves instead of returning 404.
- Lineage accuracy: a download that tried fallback URLs records only the URL that produced the file; frames
  taken from a toolkit output link to that output; a toolkit job's outputs link to everything it was given;
  plan nodes and code cells link to the inputs they were handed.

### Security
- pypdf 6.21.2 in the agent and the ingestion functions.

## [1.0.0] — first public release

The first public release of Media Agent Harness: an operating layer for media agents on Amazon Bedrock
AgentCore and Strands Agents, deployed with the Unified Media Agent Toolkit in one `cdk deploy`.

### Capabilities
- **A visual media workspace.** Chat beside a Renderer with timelines (shot, speaker and caption lanes),
  image galleries and document previews. Frame-accurate ranges, markers and crops go from the timeline into
  the conversation; clips and renders come back next to their source.
- **An orchestrator with sub-agents.** The main agent answers directly or dispatches task, planning, design
  and render sub-agents, each on its own AgentCore runtime. Multi-step requests become a DAG plan that fans
  out, waits on long-running jobs and hands results forward, so the work outlives the chat turn.
- **Media workflows as tools.** The Unified Media Agent Toolkit's micro-workflows (shot segmentation,
  diarization, transcoding, face recognition and more) sit behind an AgentCore Gateway; a job registry tracks
  them and their results stream back to the chat when they finish.
- **Media by reference.** Agents pass storage locations and stable links, never bytes or expiring URLs.
  Every output becomes an asset of its project, with lineage back to its inputs.
- **Projects.** Folders, bookmarks (saved moments), an import inbox, and search across video, audio, images
  and documents with citations that open at the cited moment or page.
- **Governed capabilities.** Skills, tool collections, hooks and custom agents are versioned assets with a
  draft → publish lifecycle, discoverable through the AgentCore Agent Registry. Built-in skills ship with the
  harness.
- **Custom agents for any OIDC application.** Publish an agent and call it from your own application with your
  own identity provider; external callers upload media and download results within their own scope.
- **A code sandbox** (AgentCore Code Interpreter) for analysis, documents and media edits, with an LGPL
  ffmpeg built in your account from pinned, checksummed sources.
- **One-command deploy.** A single `cdk deploy` (see the README's Quick Start) deploys the toolkit and the
  harness; the container images and the web app build in AWS CodeBuild, so no local Docker is needed.

### Security
- Session ownership is enforced wherever a request names a session: chat turns, the render endpoint, the
  render store and uploads. A session-less upload or render is tagged with its owner, and only that user can
  read it back.
- Media is private: the artifacts CDN refuses every path, and the authenticated proxy serves media through
  short-lived presigned URLs after an ownership or project-membership check. A key nothing attributes to the
  caller is served only to members of a project that registered it. Agent-written documents open in a
  sandboxed viewer with a single-document view ticket.
- The code sandbox runs with per-project, per-session scoped S3 credentials and fails closed when they cannot
  be installed.
- The UI load balancer accepts only CloudFront (origin-facing prefix list plus a secret origin header).
  Clients cannot send the proxy-reserved request fields the runtimes trust.
- Cognito self sign-up is off by default.
- Dependencies are on fixed releases: `bedrock-agentcore` 1.18.1 (CVE-2026-12530, CVE-2026-16796),
  Starlette 1.x, PyJWT 2.15, cryptography 50, pypdf 6.21 and Pillow 13 in the ingestion functions,
  `aws-cdk-lib` 2.273, and a web app and proxy with no known npm advisories.

### Known limitations
- CloudFront reaches the UI load balancer over HTTP inside AWS; see [SECURITY.md](SECURITY.md#known-limitations).
  A CloudFront VPC origin is the planned hardening.
- `mcp` stays on 2.1.x until Strands Agents allows 2.2 (the open advisory concerns MCP *servers*; the harness
  uses the client only), and the agent runtimes' Pillow stays on 12.x until Strands Agents Tools allows 13.
- One install per `projectName` per account and Region; deploy only to the toolkit's published Regions.
- This is sample code, not a production-ready service. It creates billable resources, and some (media buckets,
  image repositories, the user pool) are retained on teardown on purpose.

## Before 1.0.0

### Added
- Whole-skill loading: `skills_manager` now inlines a skill's text `references/*.md` into its
  instructions under a byte budget (`SKILL_BUNDLE_REFERENCE_MAX_BYTES`, default 24KB), so
  references reach detached `plan_and_execute` (DAG) nodes; over-budget skills keep pure
  progressive disclosure. S3-authored skills now download the whole package, not just `SKILL.md`.
- AgentCore **Cedar** policy engine on the shared Platform gateway (IaC, `ENFORCE`): scoped
  `permit` for the read-only AWS-doc tools, default-deny otherwise.
- Guard tests pinning the MCP 2.x SDK's automatic `2026-07-28` protocol negotiation.

### Changed
- Model tool caching migrated from the deprecated `cache_tools` flag to
  `CacheConfig(tools_ttl=…)` (behavior-preserving; removes the strands ≥1.55 DeprecationWarning).
- Content-moderation skill: the rating enum is self-contained in `sg5` with a deterministic code
  guard; the report is always emitted as a **PDF**; `sg5` outputs the report + PDF CloudFront URLs
  so `get_plan_results` surfaces them as downloads (no post-completion fan-out).

### Fixed
- Render image duplicates (two distinct causes): client-preview entries lingering beside the
  server-composed gallery (`mergeServerRenders` now dedups by content), and `preview`/`thumbnail`
  URLs being harvested as separate gallery tiles (`extract_urls` now excludes them).
- Platform-gateway federation: the task-agent runtime was missing the platform-gateway env, so
  shared AWS-doc tools didn't load there; fixed and verified live.
- Delegated (async-plan) gateway auth: `bind_delegation_grant_and_connect` kept a live
  user-token connection even after that token expired, so detached plan nodes presented a stale
  JWT and the AgentCore gateway rejected them with `403 insufficient_scope` (upstream of the
  ownership interceptor, never reaching the target). The pinned user token now carries expiry
  awareness; when provably expired it is cleared and the connection rebinds on the machine
  token + delegation grant. This was the intermittent user-MCP (fal) `insufficient_scope` seen on
  long/detached reels — it was never a fal or per-model problem.
- Cross-session render bleed: the end-of-turn render harvester rendered any media URL in a tool
  result, so a continuation session that loaded prior reusable work auto-opened a tab for a
  *different* session's asset. The auto-harvester now drops candidates under
  `artifacts/<uuid>/` for a uuid ≠ the current session (allows the shared `external/` namespace,
  the current session's prefix, and non-artifact URLs; fails open when the session is unknown).
  Explicit displays are unaffected.
- UI localStorage quota (`Failed to execute 'setItem' … exceeded the quota` on
  `lab-orchestrator-model-id`): the culprit was a **write-only** `lab-sessions` blob (never read
  back — the list reloads from DynamoDB) that carried every session's `renderHistory` and grew
  until the store filled. Removed that write and purge the stale key; model-id writes now go
  through `safeSetItem` (prune-and-retry); the per-session `lab-render-<id>` cache is bounded by
  an LRU and reclaimed by the pruner.
- **agent-olive skill (Phase 7 stitch):** the skill now tells the stitch dispatch to reuse the
  exact segment URLs approved at the Phase 6 gate (verbatim, in script order) and forbids
  re-discovering inputs (no S3 listing, no searching for "the reel", never a clip from another
  session). Removes the input re-hunt that inflated a one-shot concat into ~70 steps and could
  splice in a prior session's reel.
