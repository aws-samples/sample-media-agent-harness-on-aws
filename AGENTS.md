# AGENTS.md

Guide for coding agents (and people) working on Media Agent Harness in their own AWS account.
A multi-agent media platform on Amazon Bedrock AgentCore and Strands Agents: React workspace →
authenticating proxy → main orchestrator agent → sub-agent runtimes, with the media toolkit's
workflows behind an AgentCore Gateway. Start with [README.md](README.md).

## What is where

| Path | What |
|---|---|
| `agent/src/` | Python. Agent entrypoints (`main.py` orchestrator, `task_agent.py`, `planning_agent.py`, `render_agent.py`), the Lab service (`lab/`), the Capability API (`capability_api/`), tools (`tools/`), hooks, jobs, ingestion and search. |
| `agent/skills/` | Built-in skills, one directory per skill with a `SKILL.md` (plus optional `references/`). |
| `agent/tests/` | The agent test suite. |
| `ui/src/` | The React workspace (Vite). |
| `ui/server/` | The Express proxy on Fargate: auth, routing to the runtimes, artifact serving. |
| `infra/lib/` | The AWS CDK app. `app.ts` → `build-app.ts`; most resources are in `agent-infra-stack.ts`; env settings in `env-config.ts`; the toolkit stack in `toolkit/`. |
| `infra/test/` | Synth tests (`npm run test:<name>`, see `infra/package.json`). |
| `scripts/`, `tests/` | Operator helpers, gateway smoke tests, evaluation and load tooling. |

## Deploy, update, tear down

Everything deploys with ONE command from `infra/`, always with `-c env=customer` (the README's
[Quick Start](README.md#quick-start) has prerequisites, first sign-in and teardown). The source is
the public repository <https://github.com/aws-samples/sample-media-agent-harness-on-aws>; work in a
clone of it (or of your fork):

```bash
git clone https://github.com/aws-samples/sample-media-agent-harness-on-aws.git
cd sample-media-agent-harness-on-aws
export AWS_REGION=us-west-2        # set it explicitly; the deploy goes where the CLI resolves
cd infra && npm ci
npx cdk bootstrap -c env=customer  # once per account/region
npx cdk deploy -c env=customer --all --require-approval never
```

- **An update is the same command.** It rebuilds every artifact from your working tree: the agent code
  is zipped by CDK, and the proxy image, Capability API image and SPA are built in CodeBuild in your
  account (`infra/lib/distribution/harness-assets.ts`) — no local Docker. A build reruns only when its
  source (`ui/` or `agent/`) changed. Do not pass `-c uiProxyImageTag` / `-c capabilityApiImageTag`:
  they select a pre-pushed image in a separately managed repository this install does not have.
- **Keep the same `-c projectName=`** (default `mediaworks-harness`) and any `-c toolkitStackName=` /
  `-c vpcId=` on every deploy; a different `projectName` is a second install, not an update.
- Two stacks: `<projectName>-media-toolkit` (deployed first), then `<projectName>-infra-customer`.
  `cdk deploy` output is buffered when not on a TTY; poll progress with
  `aws cloudformation describe-stacks --stack-name <projectName>-infra-customer --query 'Stacks[0].StackStatus'`.
- **Tear down** in the order the README gives (harness, then toolkit). Buckets, tables, image
  repositories, the user pool and a KMS key are retained on purpose; record their names first.
- Do not delete or modify deployed resources by hand to "fix" a deploy; change the code and redeploy.

## Did my deploy land?

A stack reaching `UPDATE_COMPLETE` is not proof by itself. Check what you changed:

- **Version label.** `GET https://<UiDistributionDomainName>/config` returns `appVersion`, also shown
  in the sidebar's user row and the `AppVersion` stack output. It defaults to the repo's `VERSION`
  file, so it does NOT change between your own deploys unless you pass `-c appVersion=<label>`.
- **Agent code / skills** (`agent/src/**`, `agent/skills/**`): the AgentCore runtime's version
  increments (`aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id <AgentRuntimeId output>
  --query agentRuntimeVersion`, or the AgentCore console). A warm session keeps the old code; see below.
- **Proxy / Capability API** (`ui/server/**`, `agent/src/capability_api/**`, `agent/src/capability/**`,
  `agent/src/lab/**`): the ECS service runs a new task definition whose image tag changed.
- **SPA** (`ui/src/**`): the served `index.html` names a new `assets/index-<hash>.js` (Vite hashes
  content). The app shell is served no-cache, so a reload picks it up.

## Testing

- Agent: `cd agent && uv run --extra dev pytest -q [path]`. The suite has zero expected failures;
  tests needing live AWS skip without `AWS_PROFILE`. The knowledge-graph tests need the `graph` extra
  (`uv run --extra dev --extra graph pytest ...`).
- UI: `cd ui && npm ci && npx vitest run && npx tsc --noEmit`. Run vitest from `ui/`, not the repo root.
  Vitest exits non-zero on unhandled errors even when every test passes, so read the summary's
  `Errors` line too.
  - `ui/vitest.setup.ts` only stubs `fetch` (answers `/config`, rejects any other relative URL with a
    named error). It sets neither the environment nor jest-dom: a component test needs a
    `@vitest-environment jsdom` docblock pragma AND `import '@testing-library/jest-dom'`.
  - Use `fireEvent`; `@testing-library/user-event` is not a dependency.
  - `vi.mock('../lab-api')`-style factories are explicit allowlists: adding a component that uses a
    new export breaks that view's tests until the export is added to the mock.
- Infra: `cd infra && npx tsc --noEmit`, then `npx cdk synth -c env=customer` (needs credentials for
  the target account) and `npm run test:template-budget` (CloudFormation's 1,000,000-byte template
  limit, checked over `cdk.out`). Each `npm run test:<name>` in `infra/package.json` is a standalone test.
- A `@tool`-decorated async function is directly awaitable in tests: `await mod.my_tool(x=1)`; its
  schema is at `.tool_spec`.
- A fake looser than the real API hides breakage. For AWS calls, validate parameters against
  botocore's model (`agent/tests/test_capability_registry_param_shapes.py` shows how) instead of a
  mock that accepts anything.

## How to extend

### Add a proxy route: FOUR registrations

A new `ui/server` path needs all four, or it fails silently:

1. the route in `ui/server/app.ts`;
2. its prefix in `authPaths` (`ui/server/app.ts`), so the JWT middleware runs;
3. its prefix in `PROTECTED_PREFIXES` (`ui/src/auth/authFetch.ts`), so the SPA sends the token;
4. a CloudFront `additionalBehaviors` entry in `infra/lib/agent-infra-stack.ts` (bare `/x` AND `/x/*`
   if both are called). The list is an allowlist: an unlisted path falls through to the SPA bucket and
   returns `index.html` with a 200, which looks like the agent answering with raw HTML.

### Add a center mode (main-nav surface)

- Add it to `CENTER_MODES` in `ui/src/views/WorkspaceView.tsx` AND `SIDEBAR_CENTER_MODES` in
  `ui/src/components/SessionSidebar.tsx` (`center-mode-unions.contract.test.ts` fails on drift), add
  nav entries in both the collapsed rail and the expanded header, extend the
  `onNewItemAction`/`newItemLabel` morph, and add the render branch. Use a distinct icon.
- If it hosts `CenterEditor`: an effect forces `centerMode='chat'` for any `editingTarget` whose category
  is not in `HOSTS_OWN_EDITOR`; add yours. The chat pane stays mounted (`display:none`), so guard its
  `CenterEditor` too, and assert `getAllByTestId('center-editor')` has length 1.

### Add a lab action

Lab CRUD tools are not top-level tools: the model sees one `lab_asset` multiplexer
(`agent/src/main_lab_tools.py`). Add the wrapper to `_INDIVIDUAL_LAB_TOOLS` (the action registry is
derived from it), add its name to `_LAB_CRUD_TOOLS` in `agent/src/capability/descriptors.py`, and name the
action in `lab_asset`'s docstring, which IS the schema the model reads. Assert
`lab_asset.tool_spec["description"]` contains the action name.

### Add or change a built-in skill

Create `agent/skills/<name>/SKILL.md` (frontmatter `name` + `description`) with optional
`references/*.md`; `agent/skills/skill-creator/` is the in-product guide. `cdk deploy` alone ships it
to TWO consumers:

- **the agent-runtime zip** (every file under `agent/skills/`), which is what activates the skill; and
- **a DynamoDB catalog** (`<projectName>-gateway-registry`, one `__skill__<name>` row per skill,
  written at deploy by `infra/lib/builtin-skill-catalog/`), which is what the Skills panel paints from.

The catalog is current as soon as the stack finishes; a warm runtime session still serves the old zip
until it idles out (below), so the UI can show new text before the agent uses it. References under the
`SKILL_BUNDLE_REFERENCE_MAX_BYTES` budget (default 24 KB) are inlined into the skill's instructions;
larger sets stay progressive-disclosure.

### Runtime configuration goes in `RuntimeConfigSecret`, not runtime env

AgentCore Runtime V2 caps a runtime's environment-variable payload at 1536 bytes, and exceeding it
fails the update (`ui/src/runtime-v2-env-budget.contract.test.ts` guards it). Add static config to
the `runtimeConfig` map in `infra/lib/agent-infra-stack.ts`; only `AWS_REGION`, `DEPLOY_ENV`,
`BYPASS_TOOL_CONSENT` and `RUNTIME_CONFIG_SECRET_ARN` stay inline. The agent loads the secret into
`os.environ` through `runtime_config_bootstrap`, which must be the first first-party import in every
entrypoint (a test checks the ordering). Sub-agent runtime ARNs are read from SSM at startup
(`agent/src/runtime_arns.py`, `ui/server/runtimeArns.ts`); never put a runtime's `GetAtt` into another
runtime's or a task's env (`npm run test:runtime-parallel`).

## Rules that bite

- **Runtime changes don't reach a warm session.** The Lab session id is `lab-<sha256(actorId)>`
  (`ui/server/labSession.ts`) and chat sessions are pinned per session, so after a deploy an existing
  microVM keeps the old code and env until it is idle for 15 minutes. To force a fresh one, call the
  `bedrock-agentcore` `StopRuntimeSession` API with that exact session id and runtime ARN, or start a new
  chat. A cold start takes several seconds; that is how you know you got one.
- **An async `@tool` must not block.** Strands awaits a `@tool async def` on the event loop, so a blocking
  boto3 call inside one stalls progress streaming and keepalives. Wrap it: `await asyncio.to_thread(fn, ...)`.
- **A missing `tools` key means ALL tools.** `task_agent` treats an absent `tools` in the payload as "load
  every built-in and gateway tool". Any agent-scoped invoke must send an explicit `tools: []` or a list.
- **The code sandbox has scoped credentials.** The runtime installs per-(project, session) S3 credentials
  into the Code Interpreter sandbox (`agent/src/sandbox_credentials.py`); once installed they are the
  sandbox's ONLY S3 access. A cell may write only under the session output prefix that the prompt's
  S3 CONTEXT / SESSION OUTPUT LOCATION section names (`projects/<pid>/sessions/<sid>/` or `artifacts/<sid>/`). Skill text that
  says "upload to `artifacts/<name>`" fails with `AccessDenied`; `agent/tests/test_skill_sandbox_write_prefixes.py`
  checks every shipped skill. Build session paths with `agent/src/projects/storage_layout.py` or
  `ui/src/shared/sessionLayout.ts`, never by hand. Create boto3 clients per cell.
- **Never put a token in a URL.** The proxy reads the bearer token only from the `Authorization` header;
  `?access_token=` is ignored. A link or frame a browser loads without a header carries a view ticket
  (`ui/server/artifacts/viewTicket.ts`), valid for one key and read-only.
- **Every server-side fetch of a model- or user-chosen URL goes through the SSRF guard.** Python:
  `agent/src/security/url_guard.py` (`fetch`, `get`, `session`, `validate_url`: https only, every
  resolved address public, connection pinned, redirects re-validated, bytes and time capped). Node:
  `ui/server/auth/ssrfFetch.ts`. Never call `requests`/`fetch` on such a URL directly.
- **The load balancer is origin-locked.** CloudFront adds an `X-Origin-Verify` header from a Secrets
  Manager secret; the proxy (`ui/server/originVerify.ts`) and the Capability API
  (`agent/src/capability_api/origin_verify.py`) refuse requests without it (`403 origin_not_allowed`),
  except `/health`. Call the API through the CloudFront domain, not the ALB. Unset secret (local dev) =
  no enforcement.
- **Absent is not empty.** `?? []` / `or []` on a field the source never populates turns "unknown" into
  "none". Leave it `undefined`/`None` and branch on that.
- **Media moves by reference.** Agents pass S3 locations and asset ids, never bytes; the artifacts CDN
  serves nothing publicly, and media resolves through the authenticated proxy.
- **The model cannot purge.** Destructive control-plane routes live only in the Capability API
  (`/control-plane/...`), never in the Lab tool table; `agent/tests/test_capability_api_isolation.py`
  enforces it. Don't wire them into `lab_asset`.
- Read the deployed feature flags from `GET /control-plane/config` (unauthenticated, through CloudFront).
- Commit no credentials, client secrets or test passwords, including in docs.
