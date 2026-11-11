import { App } from "aws-cdk-lib";
import * as fs from "fs";
import * as path from "path";

/**
 * Deployment environments (clean 2-account / 2-env; one env per account because
 * resource physical names are keyed on account+region, not env):
 *  - staging: account 100000000002, us-west-2 — its own M2C media-workflow-gateway.
 *  - prod:    account 100000000003, us-west-2 — reuses the co-located M2C gateway.
 *  - sandbox: account 100000000001, us-west-2 — standalone harness, no M2C
 *             federation yet (media undefined). Federate later once M2C is
 *             deployed in this account and its outputs are pasted below.
 *  - dev:     retired to local-only (config retained for local runs; not deployed).
 *  - customer: any account/region (from the CLI credentials) — a fresh customer install. Deploys
 *             the media toolkit underneath (`MediaToolkitStack`) unless `-c toolkitStackName=` names
 *             an existing one, creates its own buckets, VPC and Agent Registry.
 *
 * Selected via `-c env=<name>` (CDK context) or the DEPLOY_ENV env var. Defaults to `customer`, so a bare
 * `cdk bootstrap` / `cdk deploy` from the shared package targets the caller's own account and region — never
 * an internal env's fixed account (a bare `cdk bootstrap` once tried the dev placeholder account).
 */
export type EnvName = "dev" | "staging" | "prod" | "sandbox" | "customer";

/**
 * Federation to the M2C `media-workflow-gateway` (path A). Populated from the
 * m2c-agent-tools stack outputs (GatewayUrl / ClientId / SecretArn /
 * TokenEndpoint) once M2C is deployed in the target account. When set, the
 * harness registers this gateway in its gateway registry and consumes
 * process_media / diarize_speech / separate_stems / get_job_status from it.
 */
export interface MediaWorkflowGatewayConfig {
  gatewayUrl?: string;
  clientId?: string;
  credentialsSecretArn?: string;
  tokenEndpoint?: string;
  /**
   * OIDC discovery URL for the M2C auth server. The Search_Gateway's OUTBOUND OAuth2 credential
   * provider uses it to find the token endpoint. For a Cognito pool this is the IdP endpoint
   * `https://cognito-idp.<region>.amazonaws.com/<userPoolId>/.well-known/openid-configuration` — NOT
   * the hosted-UI domain (`<domain>.auth.<region>.amazoncognito.com`), which returns 404 for
   * `/.well-known/openid-configuration`. It CANNOT be derived from `tokenEndpoint` (the hosted-domain
   * prefix does not encode the user-pool id), so it must be set explicitly; a wrong/absent value makes
   * the Search target sync fail with `SYNCHRONIZE_UNSUCCESSFUL`.
   */
  oidcDiscoveryUrl?: string;
  scope?: string;
  /**
   * The M2C proxy (working/output) bucket where the media-workflow tools write
   * their outputs. The task agent is granted read on it so the render pipeline
   * can republish those outputs into the CloudFront-backed artifacts bucket.
   */
  proxyBucket?: string;
  /**
   * IAM role ARNs for the independently-deployed toolkit's workflow stacks, whose identity
   * policies are scoped to their own IngestBucket rather than this harness's upload bucket.
   * Bridged onto the harness's skills bucket so a toolkit workflow can read a harness-uploaded
   * source asset.
   */
  m2cServiceRoleArns?: string[];

  /**
   * Optional NAME prefix of the M2C state machines (e.g. `m2cv6-0e0000000002-`) that narrows the
   * job-completion EventBridge rule (`JobSfnCompletionRule`). Absent = every state machine in this
   * account and region, which is always correct: the polling worker ignores an execution that is
   * not a registered job after one GetItem. Getting it WRONG cannot change an outcome either — M2C
   * jobs just fall back to the once-a-minute poll — so it is purely a noise filter, and must change
   * together with `proxyBucket` (same hash) if M2C is redeployed. Ignored where M2C runs in another
   * account: its executions never reach this account's default bus.
   */
  jobEventStateMachinePrefix?: string;

  /**
   * M2C hybrid-search indexer role(s) that must read an image asset's Search_Rendition
   * (`projects/<pid>/<assetId>/rendition.jpg` in the Ingest bucket) to embed it: an image's `shot`
   * document points at the rendition, not at an M2C output (media-harness-project-ingestion Req 8.11).
   * Granted GetObject on rendition objects under `projects/` only, nothing else in the bucket.
   */
  renditionReaderRoleArns?: string[];
}

/**
 * Federation to the OMS Core hosted MCP endpoint (path B — a Runtime-direct
 * JWT gateway). Unlike the M2C `media` gateway (which fronts an AgentCore
 * Gateway URL), this points straight at the OMS Core Bedrock AgentCore
 * Runtime `invocations` data-plane endpoint and authenticates with an OAuth2
 * client-credentials (M2M) JWT minted from the OMS platform Cognito pool.
 *
 * When set, the harness seeds an `oms-core` row into its gateway registry and
 * consumes the OMS federation tools (list_integrations, get_integration,
 * get_action_schema, validate_template, save_template, activate_template).
 *
 * NOTE (cross-account / cross-region): the M2M client secret lives in the OMS
 * account (100000000006) in `us-east-1`, while this harness runs in
 * `us-west-2`. The OMS side attaches a Secrets Manager resource policy allowing
 * this harness account to GetSecretValue on that secret, and the agent derives
 * the secret's region from its ARN (see `agent/src/mcp_manager.py`).
 */
export interface OmsCoreGatewayConfig {
  /** Runtime-direct `invocations` data-plane endpoint (percent-encoded ARN form). */
  endpointUrl?: string;
  clientId?: string;
  /** ARN of the OMS-owned M2M client secret (lives cross-account/cross-region). */
  credentialsSecretArn?: string;
  /**
   * ARN of the customer-managed KMS key encrypting the OMS client secret.
   * Cross-account GetSecretValue requires the caller to hold `kms:Decrypt` on
   * this key (the OMS key policy grants this account; the agent roles are
   * granted `kms:Decrypt` on this ARN). The AWS-managed key cannot be shared
   * cross-account, hence the dedicated CMK. Published by OMS at SSM
   * `/oms/core/mcp/client-secret-kms-arn`.
   */
  kmsKeyArn?: string;
  tokenEndpoint?: string;
  scope?: string;
}

/**
 * The environment's AWS Agent Registry — the harness-shared capability
 * discovery catalog (`agent-registry` namespace, introduced 2026-08-06).
 *
 * DynamoDB/S3 remain canonical; this Registry is an eventually consistent
 * projection of the subset of capabilities shared with EVERY authenticated
 * user of the environment. One Registry per environment, never a shared
 * cross-environment one — staging must not be able to write prod's catalog.
 *
 * There is no CloudFormation resource for `agent-registry` yet, so the
 * Registry itself is created out of band by
 * `agent/scripts/registry_provision.py --env <env> --execute` (idempotent by
 * name) and its returned id pasted here. Absent this block the Capability API
 * still runs and serves canonical state; the Registry source reports itself as
 * unconfigured rather than falling back to the agent runtime (R12).
 */
export interface AgentRegistryConfig {
  /** Registry id from `create-registry`. Empty/undefined = not provisioned. */
  registryId?: string;
  /**
   * Registry name. MUST end in `-<env>`: `capability/config.py` refuses to
   * start when it does not, which is the cheap guard that catches a staging
   * task handed prod's registry id before IAM has to.
   */
  registryName?: string;
  /**
   * Whether the projection Lambda writes to the Registry. False (the default)
   * leaves the outbox rows accumulating and the projector inert — safe, and
   * exactly the state a category rollback wants (R14).
   */
  projectionEnabled?: boolean;
  /**
   * Per-category read source. Anything omitted defaults to `legacy`, so adding
   * this block changes no UI behaviour until a category is explicitly flipped
   * (R14).
   *
   *   `legacy`    — read through the Main Agent AgentCore Runtime (`/lab`).
   *   `shadow`    — serve legacy, compare against the Capability API off-band.
   *   `canonical` — Capability API serves from DynamoDB + built-ins. No Registry.
   *   `registry`  — Capability API additionally merges Agent Registry rows.
   *
   * `canonical` is where the latency win is: an always-on Fargate task reading
   * DynamoDB directly, instead of cold-starting a runtime to read the same
   * table. `registry` is the later step that needs the shadow-parity gate,
   * because it is the one that puts projected rows in front of a user.
   */
  capabilitySources?: Partial<Record<
    "skill" | "specialty-agent" | "tool" | "tool-collection" | "workflow" | "hook",
    "legacy" | "shadow" | "canonical" | "registry"
  >>;
  /** Percentage of eligible reads that also run a shadow comparison (0-100). */
  shadowSamplePercent?: number;
}

/**
 * Projects, the per-turn Project_Token, and the Cedar-governed Search_Gateway
 * (media-harness-projects-access, design Component 9 "Env gating", D10). Present only where the
 * nested `ProjectsStack` should be created: `sandbox`, `staging`, and `prod` (since 2026-10).
 * Absent in `dev`, where no nested stack, token functions, Search_Gateway, or `PROJECTS_TABLE`
 * exist and `/config.projectsEnabled` is false.
 */
export interface ProjectSigningKey {
  /**
   * Stable logical id for this signing key. The KMS key's CDK construct id derives from it, so the
   * id must not change across deploys for an existing key (D10 three-deploy rotation). It is NOT the
   * KMS key id — the published `kid` is the KMS key id resolved at deploy.
   */
  id: string;
  /**
   * A key kept in the JWKS for verification during rotation but no longer used to sign (rotation
   * step 3). Exactly one entry must be non-retired: that is the current signer.
   */
  retired?: boolean;
}

/**
 * media-harness-project-ingestion: the nested `IngestionStack` configuration (design §20
 * "Configuration"). Shipped as the `INGESTION_CONFIG` JSON in `RuntimeConfigSecret` for the
 * ingestion Lambdas, with sensible defaults applied in the stack when a field is omitted.
 */
export interface IngestionConfig {
  /** Master switch — the Asset_Service Lambda (and later the pipeline) is created only when true. */
  enabled: boolean;
  /** Extra buckets (besides the Ingest bucket) a Project may register an S3 location from (Req 2.6). */
  s3Allowlist?: string[];
  /** Frame-capture cadence in seconds (design §8 M2C arguments). Default 1. */
  frameIntervalSec?: number;
  /** Frame-capture width/height (design §8). Defaults 960×540. */
  frameWidth?: number;
  frameHeight?: number;
  /** Bedrock model chain for the image description step (design §12). */
  imageModelChain?: string[];
  /** `thinking` for the image description run: M2C `run_inference` accepts "disabled" | "adaptive". Unset → M2C's default. */
  imageThinking?: "disabled" | "adaptive";
  /** Collection_Rebuild Distributed Map concurrency (design §14). Default 8. */
  rebuildConcurrency?: number;
  /** Whether the daily orphan sweep runs (design §19). Default true. */
  orphanSweep?: boolean;
  /**
   * Generated_Media auto-registration (Req 9, design §16): media a Projects turn makes joins the turn's
   * Project. Default false — creates GeneratedMediaQueue + GeneratedMediaFn and wires the proxy's turn-end
   * message and the job-registry stream consumer only when true.
   */
  generatedMedia?: boolean;
  /**
   * Phase 1 entity-metadata workflows (face-recognition → Person identities, program-structure-analysis →
   * Program elements / Segments, cast-resolution → Characters). On: the VIDEO pipeline also submits these
   * three toolkit workflows and FOLDS their outputs (person / character / actor names and Segment labels)
   * into the scene documents' searchable `named_entities` by time overlap — NOT a side-channel asset blob —
   * and registration requires their gateway tools. Default/absent: the current two-wave video pipeline, the
   * new tools are neither required nor submitted, and the key is omitted so an off env's INGESTION_CONFIG
   * — and every template embedding it — is byte-identical. Reaches the reducer as
   * `INGESTION_CONFIG.entityMetadata` (read into `reducer.Config.entity_metadata`) and the Asset_Service
   * catalog gate. Only enable where the deployed toolkit gateway exposes the three targets.
   */
  entityMetadata?: boolean;
  /**
   * Personal samples (`infra/lib/personal-samples.ts`): the deploy uploads `infra/assets/samples/` to the Ingest
   * bucket under `samples/`, and each user's Personal project gets its own pinned copy (the demo tour's
   * `0-demo.mp4`) when their project list loads. A Personal project does not index, so there is no per-user
   * analysis. Default/absent: nothing is uploaded and the Asset_Service gets no `PERSONAL_SAMPLES`.
   */
  personalSamples?: boolean;
}

export interface ProjectsConfig {
  /** Master switch — the nested `ProjectsStack` is created only when true. */
  enabled: boolean;
  /**
   * media-harness-project-ingestion: the nested `IngestionStack` (Asset_Service and, later, the
   * pipeline). Present only where `ProjectsStack` is (sandbox, staging, source). The Asset_Service
   * writer is created only when `ingestion.enabled`; `ASSET_SERVICE_FN` is absent otherwise so the
   * Project_Tools are not bound (design §20 "prod").
   */
  ingestion?: IngestionConfig;
  /**
   * Per-Project knowledge graph (nested `GraphStack`: x86 container-image writer + reader Lambdas over Lance
   * tables in `projects/<pid>/graph/`). Requires `ingestion.enabled`, and the stack is created only when the
   * deploy also passes `-c graphLambdaImageTag=<tag>` (the image CI pushed). Staging only for now: `source`
   * builds images in CodeBuild, which does not build this one yet.
   */
  graph?: { enabled: boolean };
  /**
   * Asset lineage EDGES (docs/proposals/asset-lineage-graph.md §2.2, decision 2026-10-22: DynamoDB is the source of
   * truth). On: the runtimes record what every gateway (fal) / sub-agent / plan node / code_interpreter output was
   * made from (with source time ranges and allowlisted producer settings), Generated_Media writes
   * `UP#<child>#<parent>` / `DOWN#<parent>#<child>` edge items in the Project's partition and logs every dropped
   * edge, the plan chainer records each node's handed inputs and outputs on its job row, and the Asset_Service
   * answers `lineage` (`POST /projects/:pid/assets/lineage`). Reaches the consumers as `lineageEdges` in the
   * INGESTION_CONFIG JSON (written only when on) and `LINEAGE_EDGES=true` on the plan chainer. Absent/false: the
   * current behaviour, and a byte-identical template. Requires `ingestion.generatedMedia`.
   */
  lineageEdges?: boolean;
  /**
   * Project_Token lifetime in seconds. Must be 60–3600; defaults to 3600 (Req 4.3). `resolveEnvConfig`
   * throws, naming the env and value, for anything outside the range.
   */
  tokenLifetimeSeconds?: number;
  /**
   * Extra IAM role ARNs allowed to invoke `TokenIssuerFn`/`TokenRefreshFn` directly — the deployed
   * live test's role, set in `sandbox` and `staging` only (Testing Strategy). `resolveEnvConfig` throws
   * if this is set for `source`. TODO(operator): fill from the deployed-test role ARN before running
   * the live gateway test; the functions deploy without it (the test simply cannot invoke them).
   */
  tokenTestInvokerRoleArns?: string[];
  /**
   * Signing keys. One KMS `RSA_2048` SIGN_VERIFY key is created per entry; all are published in the
   * JWKS and accepted by Token_Refresh, and the single non-retired entry signs (D10).
   */
  signingKeys: ProjectSigningKey[];
}

/**
 * Deploy-time preload inputs (CDK context `ownerEmail` / `preloadPath`). See `EnvConfig.preload`.
 */
export interface PreloadConfig {
  /** The one deploy input: owns the Cognito user, every seeded project, and every installed skill. */
  ownerEmail?: string;
  /** Where the package lives in the Ingest bucket. Default `preload/` (folder) or `preload.zip` (archive). */
  preloadPath?: string;
  /**
   * An explicit re-run trigger for the `PreloadFn` custom resource (CDK context `-c preloadNonce=`). Set it
   * (e.g. a timestamp) to force preload to re-run after uploading a new package to `preloadPath` on an
   * EXISTING stack of the SAME commit — otherwise CloudFormation sees no property change and skips the
   * handler. CI deploys change the app version each build, so they re-run without a nonce.
   */
  preloadNonce?: string;
}

export interface EnvConfig {
  env: EnvName;
  /** Target account. Undefined for dev → resolved from CDK_DEFAULT_ACCOUNT. */
  account?: string;
  region?: string;
  /** Present where the harness federates to the M2C media-workflow-gateway. */
  media?: MediaWorkflowGatewayConfig;
  /** Present where the harness federates to the OMS Core hosted MCP endpoint. */
  oms?: OmsCoreGatewayConfig;
  /** Harness-shared capability discovery catalog. Optional per environment. */
  agentRegistry?: AgentRegistryConfig;
  /**
   * Projects, Project_Token, and the Cedar-governed Search_Gateway. Present only for `sandbox`,
   * `staging`, and `prod`; absent elsewhere so no nested `ProjectsStack` is created.
   */
  projects?: ProjectsConfig;
  /**
   * Deploy-time preload (design docs/design-2026-10-27-admin-preload-and-preanalyzed-ingest.md). When a
   * preload package is present in the Ingest bucket at `preloadPath`, the `PreloadFn` custom resource
   * creates the owner, seeds the project(s), places the assets and installs the paired skill(s) at deploy
   * time. Supplied via CDK context `-c ownerEmail=… -c preloadPath=…` (both optional; a package present
   * without `ownerEmail` fails the deploy loudly, a package absent is a clean no-op). Requires
   * `projects.ingestion.enabled` (the import sweep + pipeline do the actual ingestion).
   */
  preload?: PreloadConfig;
  /**
   * Demonstration-only Slack ingress. Absent or `enabled:false` = the Slack
   * construct is not created at all, so the synthesized stack is identical to
   * before (additive, zero-diff-when-off). Enabled only where explicitly set.
   */
  slack?: SlackConfig;
  /**
   * Lock the artifacts CloudFront distribution and the proxy's public `/api/artifact` read-through.
   * When true: the ArtifactsCdn viewer-request function answers 403 to EVERY request (default
   * behaviour and `projects/*`), the UI proxy gets `ARTIFACTS_REQUIRE_AUTH=true` (so `/api/artifact`
   * is authenticated and session-ownership checked), and the agent runtime config gets
   * `ARTIFACTS_CDN_PRIVATE=true` (tool inputs carrying artifact URLs are rewritten to presigned GETs).
   * Artifact URLs stay stable identifiers; every consumer resolves them on use (browser via
   * `POST /api/artifact-urls`, agent and Slack Lambdas via S3 by key). Absent/false = the CDN stays
   * public and nothing changes.
   *
   * On in staging since 2026-10, and in prod + sandbox since the asset-lineage Phase 3 change — media
   * is private in every env that serves it. The private CDN answers NOTHING: the code-interpreter
   * sandbox reads its ffmpeg (`tools/ffmpeg/ffmpeg-release-<arch>-static.tar.xz`) by S3 key with its own
   * IAM role (`agent/src/tools/crop_video.py`), so no public link exists.
   */
  artifactsCdnPrivate?: boolean;
  /**
   * Append-only `asset_events` audit history for project assets (asset-lineage Phase 3, D6): one S3
   * Tables table bucket + namespace + Iceberg table, fed from the EXISTING projects-table stream by a
   * batching appender Lambda (`AssetAuditStack`). Additive and OFF unless set — absent/false means the
   * nested stack is not created at all and the template is unchanged. Requires `projects.enabled`
   * (there is no projects table to read otherwise); a synth warning says so and the stack is skipped.
   * Staging only while the Iceberg write path is being verified there.
   */
  assetAuditTable?: boolean;
  /**
   * Whether the `asset_events` appender consumes the projects stream (its event-source mapping's
   * `Enabled`). Separate from `assetAuditTable` so the table can deploy BEFORE the account's
   * `s3tablescatalog` Glue integration exists (an operator step, see `AssetAuditStack`). Absent/false =
   * the mapping is created disabled. Meaningless without `assetAuditTable`. Enabling it makes the
   * appender the projects stream's FOURTH Lambda reader (after PurgeTrigger, the ingestion Dispatcher
   * and the GraphWriter — staging, 2026-10-09); DynamoDB Streams throttles above ~2 concurrent readers per shard, so check the
   * stream's `ReadThrottleEvents` and the mappings' `IteratorAge` after turning it on.
   */
  assetAuditAppend?: boolean;
  /**
   * Where NEW session outputs are written (asset-lineage Phase 3 (a), design §9.1):
   *   - absent / `"artifacts"`: `artifacts/<sid>/output/` (legacy, every env before Phase 3);
   *   - `"project"`: `projects/<pid>/sessions/<sid>/output/` when the writer has a trusted project id,
   *     else the legacy prefix.
   * Gates WRITERS only — every reader accepts both roots regardless. Reaches the runtimes as
   * `SESSION_OUTPUT_LAYOUT` in RuntimeConfigSecret (never runtime env: V2's 1536-byte cap) and the
   * plan-chainer / polling-worker Lambdas as a Lambda env var. Synth REFUSES `"project"` without
   * `artifactsCdnPrivate`: a `projects/` CDN URL is always 403, so the layout is only coherent where
   * artifact URLs are identifiers resolved through the proxy.
   */
  sessionOutputLayout?: "artifacts" | "project";
  /**
   * Per-(project, session) S3 credentials for the code sandbox (asset-lineage Phase 3 (b), design
   * §9.2): creates `SandboxScopedS3Role` (STS session tags `project_id` / `session_id`,
   * `${aws:PrincipalTag/...}` resource paths, no delete) assumable ONLY by the Main and Task runtime
   * roles, and puts its ARN in RuntimeConfigSecret as `SANDBOX_SCOPED_ROLE_ARN`, which makes those
   * runtimes inject the credentials into every sandbox out of band.
   *
   * ON BY DEFAULT in every env (absent = on) since B2: the code interpreter's own execution role
   * now holds no artifact/project S3 access (only `tools/ffmpeg/*`), and the runtimes FAIL CLOSED when
   * they cannot install these credentials, so the scoped role's prefixes are the only places a cell
   * can read or write (every shipped skill is checked against them by
   * agent/tests/test_skill_sandbox_write_prefixes.py). `false` is an explicit opt-out that leaves the
   * sandbox with no S3 access at all — not a way back to the old shared grants.
   */
  sandboxScopedCredentials?: boolean;
  /**
   * Create the harness's light Bedrock guardrail (CfnGuardrail + a published version) so an admin has one to pick in
   * Settings -> Safety. It is NOT applied until an admin picks it: the platform runs with no guardrail by default
   * (agent/src/model/guardrail.py; docs/superpowers/specs/2026-11-09-bedrock-guardrails-design.md). Every filter at
   * LOW strength: only HIGH-confidence prompt attacks and clearly harmful content are blocked.
   */
  harnessGuardrail?: boolean;
  /**
   * Preserved-thinking prefix check for this env, overriding agent/src/model/models.json `thinking_prefix_mismatch`
   * (default `drop_block`). `error` forces Bedrock's thinking-signature check on in an account that does not enforce
   * it by default (created before 2026-08-31): a test env runs with it to prove every turn keeps its prefix stable.
   */
  thinkingPrefixMismatch?: "drop_block" | "error" | "off";
  /**
   * AgentCore Web Search on the shared Platform Gateway (agent-harness-help-web-search R6-R8,
   * media-harness-web-search-toggle). `enabled` creates the `web-search` connector target on the Platform
   * Gateway, grants its role `bedrock-agentcore:InvokeWebSearch`, and adds a Cedar permit for exactly the
   * WebSearch action; absent/false creates none of them (Cedar default-deny then blocks it too).
   * `includeDomains` / `excludeDomains` are the target-level domain policy the connector enforces; a
   * request's own filters can only narrow them. Region-gated: synthesis fails outside
   * `WEB_SEARCH_SUPPORTED_REGIONS`. Whether a given TURN may search is the per-chat toggle (enforced by tool
   * binding + a wrapper refusal in the agent), never this flag alone.
   */
  webSearch?: { enabled: boolean; includeDomains?: string[]; excludeDomains?: string[] };
  /**
   * Wire the media toolkit at DEPLOY time instead of from the static `media` block: the harness's
   * ToolkitLookup custom resource reads the toolkit stack's Outputs + credentials secret (the harvest
   * script's logic). `deploy: true` also stands the toolkit up underneath, from its published
   * template, in a separate `<project>-media-toolkit` stack that `cdk deploy --all` runs first (it
   * owns the shared Skills/Ingest bucket). `stackName` instead names an already-deployed toolkit.
   * Absent = the static `media` block, as before. CLI: `-c deployToolkit=true|false`,
   * `-c toolkitStackName=<name>`.
   */
  /**
   * Build the Code Interpreter sandbox's ffmpeg at deploy time (CodeBuild, static LGPL FFmpeg +
   * OpenH264, arm64) and stage it at `s3://<skills-bucket>/tools/ffmpeg/`, where video crop and reel
   * assembly read it with the sandbox's IAM role. Off = that path is provisioned some other way.
   */
  ffmpegMirror?: boolean;
  toolkit?: {
    deploy?: boolean;
    stackName?: string;
    /** Overrides for the toolkit template's own parameters (Prefix, GatewayName, Environment, …). */
    parameters?: Record<string, string>;
  };
  /**
   * Create the harness-owned buckets on first deploy rather than importing them by name (a fresh
   * account has none). Same as `-c createBuckets=true`; per-bucket context flags still win. The
   * Skills bucket is never created here when `toolkit.deploy` is set — MediaToolkitStack owns it.
   */
  createBuckets?: boolean;
  /**
   * VPC for the Fargate tier (UI proxy + Capability API, the only VPC-attached resources). Absent =
   * the account's default VPC (looked up). `createVpc` = a new 2-AZ public VPC with no NAT; `vpcId`
   * (`-c vpcId=`) = an existing VPC, which must have public subnets in at least two AZs.
   */
  createVpc?: boolean;
  vpcId?: string;
  /**
   * Wider AgentCore online evaluation (Improve's per-model AgentCore scores). Absent/false = the one
   * main-agent config exactly as before (GoalSuccessRate/Helpfulness/Correctness over the main
   * runtime's log group only). True:
   *   - the main config ALSO reads the Task and Render runtimes' log groups, so a session that
   *     dispatches to them stops failing with LogEventMissingException (their prompt/response log
   *     events live in their own groups). `ServiceNames` stays the main runtime's alone: the CFN
   *     schema caps it at ONE, so sub-agent turns are still not scored as sessions of their own;
   *   - a SECOND config, `<project>_component_online_eval`, samples 25% of the same traffic with the
   *     tool/skill/instruction evaluators (ToolSelectionAccuracy, ToolParameterAccuracy,
   *     SkillSelectionAccuracy, SkillInstructionFollowing — TOOL_CALL level — and
   *     InstructionFollowing, TRACE level). Its results land in their own
   *     `/aws/bedrock-agentcore/evaluations/results/<project>_component_online_eval-*` group, which the
   *     Improve harvest discovers by prefix.
   * Both need the sub-agent log groups to EXIST (create-time validation), i.e. those runtimes must
   * have logged once; every live env's have. Meaningless with `-c enableOnlineEval=false`.
   */
  onlineEvalExpanded?: boolean;
}

/** Demonstration-only Slack adapter config (see infra/lib/slack-integration.ts). */
export interface SlackConfig {
  /** Master switch — false/absent means the construct is not synthesized. */
  enabled: boolean;
  /** Shared demo actor stamped as __actor_id on every Slack turn. */
  demoActorId: string;
  /** Session salt; changing it resets all Slack thread sessions. Defaults to env name. */
  sessionSalt?: string;
  /** Harness UI base URL for /render-view links; empty omits renderer links. */
  rendererBaseUrl?: string;
  /**
   * Max artifact size to upload natively into Slack (bytes). Files larger than
   * this are posted as CloudFront links instead. Default 50 MiB. Raise/lower
   * without a code change.
   */
  maxUploadBytes?: number;
  /** Max Slack files ingested per turn. Default 5. */
  maxFiles?: number;
  /** Max per-file ingest size (bytes). Default 1 GiB. Streamed multipart. */
  maxFileBytes?: number;
  /** Max aggregate ingest size per turn (bytes). Default 2 GiB. */
  maxAggregateBytes?: number;
}

const CONFIGS: Record<EnvName, EnvConfig> = {
  // A fresh customer install. Account/region (and the Agent Registry name, which must end in
  // `-customer`) are filled in by resolveEnvConfig from the CLI credentials and `projectName`.
  customer: {
    env: "customer",
    artifactsCdnPrivate: true,
    createBuckets: true,
    createVpc: true,
    ffmpegMirror: true,
    toolkit: { deploy: true },
    agentRegistry: {
      projectionEnabled: true,
      capabilitySources: {
        "specialty-agent": "registry",
        "skill": "canonical",
        "tool-collection": "canonical",
        "workflow": "canonical",
        "hook": "canonical",
      },
    },
    projects: {
      enabled: true,
      signingKeys: [{ id: "projects-signing-1" }],
      ingestion: { enabled: true, generatedMedia: true, personalSamples: true },
      graph: { enabled: false },
    },
  },
  dev: {
    env: "dev",
    account: "100000000004",
    region: "us-west-2",
    // Shawn's test account. M2C gateway is registered dynamically via the
    // harness gateway registry (not statically via env config). Only the
    // m2cServiceRoleArns are needed here so the CDK grants the M2C Lambda
    // roles read access on the skills bucket (prevents the 403 HeadObject
    // error when media-processing probes an uploaded asset).
    media: {
      m2cServiceRoleArns: [
        "arn:aws:iam::100000000004:role/mediaworks-processing/mediaworks-audiostem-separat-AudioStemSeparatorRole-EXAMPLE00001",
        "arn:aws:iam::100000000004:role/mediaworks-processing/mediaworks-speech-diarization-SpeechDiarizationRole-EXAMPLE00002",
        "arn:aws:iam::100000000004:role/mediaworks-processing/m2c-media-processing-stack-MediaProcessingRole-EXAMPLE00003",
      ],
      proxyBucket: "media-processing-proxy-100000000004-us-west-2-an",
    },
  },
  staging: {
    env: "staging",
    account: "100000000002",
    region: "us-west-2",
    // Artifacts CDN + public /api/artifact locked; consumers resolve artifact identifiers on use.
    // Verified here first; prod and sandbox followed in the asset-lineage Phase 3 change.
    artifactsCdnPrivate: true,
    // Append-only asset_events audit history (asset-lineage Phase 3). The 2026-10-08 create was
    // rejected by CloudFormation's property validation (read with `describe-events --operation-id`):
    // `AWS::S3Tables::Table` Compaction/SnapshotManagement `Status` must be LOWER case (`enabled`),
    // unlike the TableBucket's title-case `UnreferencedFileRemoval.Status`. Fixed and proven with a
    // throwaway stack in this account (design §9.3). The appender stays OFF (`assetAuditAppend`)
    // until the account's `s3tablescatalog` integration exists (operator step, AssetAuditStack doc).
    assetAuditTable: true,
    // New session outputs under projects/<pid>/sessions/<sid>/output/ (asset-lineage Phase 3 (a)).
    // Staging first; readers everywhere accept both roots, so flipping this back is safe.
    sessionOutputLayout: "project",
    // Per-(project, session) S3 credentials injected into the code sandbox (Phase 3 (b)). Once injected
    // they are the sandbox's ONLY S3 path (the credentials file wins boto3's chain); the execution
    // role's own grants only serve a call where injection failed.
    sandboxScopedCredentials: true,
    // The harness's light Bedrock guardrail, for admins to pick in Settings -> Safety (off until they do).
    harnessGuardrail: true,
    // AgentCore Web Search on the Platform Gateway (verified in us-west-2 2026-10-20, connector 1.3.0).
    // On in staging and prod (prod approved for 0.13.0); sandbox stays off.
    webSearch: { enabled: true },
    // Wider AgentCore online evaluation: sub-agent log groups on the main config + a 25% tool/skill/
    // instruction config. Staging first; Improve's model cards show whatever these produce.
    onlineEvalExpanded: true,
    // Demonstration-only Slack ingress (additive; staging only). The construct
    // is created solely because this block is present + enabled; prod/sandbox/dev
    // omit it and synthesize unchanged. Secret is populated out-of-band.
    //
    // demoActorId is a DEDICATED synthetic actor — NOT a real user's Cognito sub.
    // (We briefly pointed it at the smoketest sub to inherit that user's fal MCP,
    // but fal is behind the user_jwt gateway which the no-JWT Slack path can't
    // authorize, so it gave no benefit and mixed Slack traffic into a real user.)
    slack: {
      enabled: true,
      demoActorId: "slack-demo",
      sessionSalt: "staging",
      rendererBaseUrl: "",
      maxUploadBytes: 52428800,
      maxFiles: 5,
      maxFileBytes: 1073741824,
      maxAggregateBytes: 2147483648,
    },
    // Path A — dedicated M2C media-workflow-gateway (v6) deployed in this
    // account (100000000002 / us-west-2) from the micro-workflow-bundle
    // (stack mediaworks-micro-workflow-bundle, artifacts m2cv6-micro-workflow/
    // alpha.0807a). Exposes all 8 workflows as MCP tools (media-processing,
    // audiostem, speech-diarization, shot-segmentation, face-recognition,
    // hybrid-search, tts) via gateway media-workflow-gateway-v6-staging, with
    // its own isolated proxy bucket. Superseded the old 6 separate mediaworks-*
    // stacks (media-workflow-gateway-staging-examplegw6); those are retained but
    // unused — retire once v6 is validated on a live render.
    media: {
      gatewayUrl:
        "https://media-workflow-gateway-v6-staging-examplegw2.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp",
      clientId: "1exampleclientid0000000001",
      credentialsSecretArn:
        "arn:aws:secretsmanager:us-west-2:100000000002:secret:/media-workflow-gateway-v6/staging/gateway-credentials-EXMPL2",
      tokenEndpoint:
        "https://media-workflow-gateway-v6-staging-100000000002.auth.us-west-2.amazoncognito.com/oauth2/token",
      // Cognito IdP discovery endpoint (NOT the hosted-UI domain, which 404s). Pool id resolved from
      // the hosted domain via DescribeUserPoolDomain. Verified: this URL's token_endpoint matches
      // `tokenEndpoint` above. Required for the Search_Gateway target sync (SYNCHRONIZE_UNSUCCESSFUL
      // otherwise).
      oidcDiscoveryUrl:
        "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_EXAMPLE02/.well-known/openid-configuration",
      scope: "media-workflows/invoke",
      proxyBucket: "m2cv6-0e0000000002-100000000002-us-west-2-proxy",
      // Same hash as proxyBucket: every M2C state machine and SFN job in the registry is
      // `m2cv6-0e0000000002-*`.
      jobEventStateMachinePrefix: "m2cv6-0e0000000002-",
      m2cServiceRoleArns: [
        "arn:aws:iam::100000000002:role/m2cv6-0e0000000002/mediaworks-micro-workflow-bu-AudioStemSeparatorRole-EXAMPLE00004",
        "arn:aws:iam::100000000002:role/m2cv6-0e0000000002/mediaworks-micro-workflow-bun-SpeechDiarizationRole-EXAMPLE00005",
        "arn:aws:iam::100000000002:role/m2cv6-0e0000000002/mediaworks-micro-workflow-bundl-MediaProcessingRole-EXAMPLE00006",
        // shot-segmentation reads the source object DIRECTLY from its own
        // Lambda -- its first state, probe_video_for_chunk_transcode, calls
        // HeadObject in _raise_on_large_file before any handoff to the shared
        // media-processing sub-workflow. Its identity policy covers only the
        // M2C ingest and proxy buckets, so without this grant every job on a
        // harness-uploaded asset fails `403 HeadObject Forbidden` (403 rather
        // than 404 because it also lacks ListBucket here). It had never
        // succeeded once: 0 SUCCEEDED / 3 FAILED across 2026-07-15 and
        // 2026-08-08. Same read-side gap this block already fixed for
        // MediaProcessingRole; shot-segmentation was simply never added.
        //
        // face-recognition is deliberately NOT listed: it never touches the
        // source bucket itself (it works off the proxy bucket and delegates
        // the source read to media-processing), and it passed this regression.
        "arn:aws:iam::100000000002:role/m2cv6-0e0000000002/mediaworks-micro-workflow-bund-ShotSegmentationRole-EXAMPLE00007",
      ],
      // hybrid-search's indexer embeds an image's Search_Rendition straight from the Ingest bucket; its
      // identity policy covers only the M2C proxy bucket (measured: AccessDenied on GetObject of
      // projects/<pid>/<asset>/rendition.jpg, 2026-10-06).
      renditionReaderRoleArns: [
        "arn:aws:iam::100000000002:role/m2cv6-0e0000000002/mediaworks-micro-workflow-bu-HybridSearchLambdaRole-EXAMPLE00008",
      ],
    },
    // OMS Core federation REMOVED from staging.
    //
    // It contributed 22 gateway tools (~2,900 bytes of schema) that no current
    // workflow uses, on top of a serial connect at every runtime cold start —
    // measured at roughly 3s of the ~11.5s catalog load. Dropping it also
    // removes a correctness hazard: OMS Core exposed no semantic search, and
    // tool narrowing builds its keep-set purely from search hits, so any turn
    // where narrowing engaged would have silently excluded every OMS tool.
    //
    // Nothing else depended on this: no runtime env vars are derived from it,
    // and the agent's OMS client reads its config from the gateway registry
    // row rather than from here. Removing the block also drops the conditional
    // secret/KMS grants and the `SeedOmsCoreGateway` custom resource.
    //
    // The existing registry row is left in place (already `enabled: false`) —
    // the seed has no onDelete, so removing it does not delete the row. To
    // restore federation, reinstate this block from git history and redeploy;
    // the seed will re-create the row with `enabled: true`.

    // Harness-shared capability catalog, created 2026-08-25 by
    // `AWS_PROFILE=ibc2026 uv run python agent/scripts/registry_provision.py
    //  --env staging --execute` (idempotent by name; the script refuses to run
    // unless the ambient credentials are 100000000002, so a wrong-profile run
    // cannot create a registry in the wrong account). APPROVE_ALL, status READY.
    //
    // The registry object is the ONE genuinely out-of-band resource here: there
    // is no CDK/CloudFormation resource type for `agent-registry`, so it cannot
    // be created by the deploy that consumes it. Everything else derives from
    // the id below — the ARN is BUILT (`agent-infra-stack.ts`), never pasted,
    // and the three IAM grants plus the `REGISTRY_*` env vars on both the
    // projector Lambda and the Capability API task follow from it. Two guards
    // catch a wrong value: synth throws unless the name ends `-staging`, and
    // `capability/config.py` repeats the check and refuses
    // PROJECTION_ENABLED=true without an id.
    //
    // Projection ON: org-scoped publishes in staging now write records. Private
    // publishes emit nothing at all (`ELIGIBLE_SCOPES = {"org"}` in
    // `lab/outbox.py`), so this is a no-op until an org-scoped asset ships.
    //
    // `capabilitySources` matches sandbox as of 2026-08-25, and was added only
    // AFTER the service was verified serving — this path has no automatic
    // fallback, so flipping a category early takes that tab from slow to blank
    // (`docs/incident-2026-08-25-staging-built-in-panels.md`). What was checked
    // against the deployed service, not inferred:
    //   * service 1/1, rollout COMPLETED, both ALB targets healthy
    //   * `/control-plane/config` 200 in 241ms, `registryConfigured: true`
    //   * authenticated `bootstrap` 200 for all five categories (85–1210ms),
    //     returning real rows (7 built-in skills, 7 agents, 1 tool-collection)
    //     with no per-category `error` and no `partialErrors`
    //   * the task role really has `bedrock:ListInferenceProfiles` and the
    //     registry reads — the IAM half that no test guards, and that bit in
    //     sandbox as an HTTP 200 carrying an exception
    // `tool` stays out for the reason given in the sandbox block.
    agentRegistry: {
      registryName: "mediaworks-harness-capabilities-staging",
      registryId: "EXAMPLEregistry2",
      projectionEnabled: true,
      capabilitySources: {
        // `registry` to match sandbox. Same weak-gate caveat as there: staging's
        // Registry holds no records (every asset here is `private`, and only
        // `org` scope is registry-eligible), so the merge is a no-op on results
        // today and exercises the path rather than adding data. It also means a
        // Registry failure now surfaces as "Partial results — unavailable:
        // agent-registry" even when the agent list is complete from DynamoDB;
        // drop to `canonical` if that proves noisy.
        "specialty-agent": "registry",
        "skill": "canonical",
        "tool-collection": "canonical",
        "workflow": "canonical",
        "hook": "canonical",
      },
    },
    // Projects / Project_Token / Search_Gateway (media-harness-projects-access). Enables the nested
    // ProjectsStack in staging. One signing key; default 3600 s token lifetime.
    // TODO(operator): set `tokenTestInvokerRoleArns` to the deployed live-test role ARN before
    // running agent/tests/live/test_search_gateway_live.py.
    projects: {
      enabled: true,
      signingKeys: [{ id: "projects-signing-1" }],
      // media-harness-project-ingestion: enable the nested IngestionStack (Asset_Service) in staging.
      // Image descriptions run on Claude Sonnet 5.5 with thinking "adaptive": M2C alpha.20261014a sends this
      // model `thinking: disabled` by default, which Bedrock rejects, so every image description failed (2026-10-14).
      ingestion: {
        enabled: true, generatedMedia: true,
        imageModelChain: ["anthropic.claude-sonnet-5-5"], imageThinking: "adaptive",
        // Entity metadata → hybrid search: face-recognition (Person), cast-resolution (Character),
        // program-structure-analysis (Segment/Program element) fold into searchable scene.named_entities.
        // Best-effort — a workflow failure never fails the asset. cast-resolution has no prior live runs.
        entityMetadata: true,
        // The demo tour's 0-demo.mp4, pinned first in every user's Personal project (personal-samples.ts).
        personalSamples: true,
      },
      // Per-Project knowledge graph (GraphStack) — parked 2026-10: it only ever held demo data, since nothing
      // extracts entities from the M2C outputs yet. Off removes the Graph tab, graph on new projects, the graph
      // Lambdas and the agent's graph tools; existing META.graph rows and S3 graph data are kept. Flip to re-enable.
      graph: { enabled: false },
      // Asset lineage edges (capture + `lineage` read API). Staging first; prod/sandbox/customer stay off.
      lineageEdges: true,
    },
  },
  sandbox: {
    env: "sandbox",
    account: "100000000001",
    region: "us-west-2",
    // Path A: federates to the toolkit's media-workflow-gateway, deployed
    // independently (separate repo, separate deploy) in this same account.
    // Wired below from the `media-toolkit-sandbox` stack's Outputs + its
    // gateway-credentials secret (see the toolkit's
    // media-toolkit-gateway/README.md Consumer Guide). This replaces the
    // old pre-consolidation stacks (mediaworks-{media-processing,
    // speech-diarization,audiostem-separation,shot-segmentation,
    // agent-tools}) with the consolidated toolkit repo's micro-workflow-bundle
    // + media-toolkit-gateway, deployed 2026-10-10. Re-harvest these values
    // (describe-stacks + get-secret-value) and `cdk deploy -c env=sandbox`
    // whenever the toolkit stack is redeployed with changed identity —
    // gateway registration itself re-seeds automatically on every harness
    // deploy (infra/lib/media-gateway-seed.ts).
    //
    // Artifacts CDN + public /api/artifact locked, as in staging (asset-lineage Phase 3: media is
    // private in every env). Nothing here reads artifacts over the public CDN: the browser resolves
    // keys through `POST /api/artifact-urls`, the agent rewrites artifact URLs to presigned GETs
    // (`ARTIFACTS_CDN_PRIVATE` in the runtime config), and the code-interpreter sandbox still gets
    // the ffmpeg tarballs, the one path the deny function allows. Sandbox has no Slack block, so the
    // `/open?key=` reply rewrite does not apply here.
    artifactsCdnPrivate: true,
    media: {
      gatewayUrl:
        "https://media-toolkit-sandbox-dev-examplegw1.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp",
      clientId: "1exampleclientid0000000002",
      credentialsSecretArn:
        "arn:aws:secretsmanager:us-west-2:100000000001:secret:/media-toolkit-sandbox/dev/gateway-credentials-EXMPL1",
      tokenEndpoint:
        "https://media-toolkit-sandbox-dev-100000000001.auth.us-west-2.amazoncognito.com/oauth2/token",
      scope: "media-workflows/invoke",
      proxyBucket: "umt-0e0000000001-100000000001-us-west-2-proxy",
      m2cServiceRoleArns: [
        "arn:aws:iam::100000000001:role/umt-0e0000000001/media-toolkit-sandbox-MediaProc-MediaProcessingRole-EXAMPLE00009",
        "arn:aws:iam::100000000001:role/umt-0e0000000001/media-toolkit-sandbox-ShotSegm-ShotSegmentationRole-EXAMPLE00010",
        "arn:aws:iam::100000000001:role/umt-0e0000000001/media-toolkit-sandbox-Sho-ShotSegmentationStateMach-EXAMPLE00011",
        "arn:aws:iam::100000000001:role/umt-0e0000000001/media-toolkit-sandbox-Audios-AudioStemSeparatorRole-EXAMPLE00012",
        "arn:aws:iam::100000000001:role/umt-0e0000000001/media-toolkit-sandbox-SpeechD-SpeechDiarizationRole-EXAMPLE00013",
        "arn:aws:iam::100000000001:role/media-toolkit-sandbox-M2cAgentToolsS-IngestListRole-EXAMPLE00014",
      ],
    },
    // Harness-shared capability catalog, created 2026-08-13 by
    // `agent/scripts/registry_provision.py --env sandbox --execute`
    // (idempotent by name; re-running reports the existing registry).
    // APPROVE_ALL is enabled — see the projector for why that is a policy
    // choice and not an authorization gate.
    //
    // Projection is ON: org-scoped publishes in sandbox now write records.
    //
    // `specialty-agent` reads are cut over to the Capability API and now merge
    // Registry rows (`registry` mode). The other five remain `legacy`. The
    // cutover gate (shadow parity, empty DLQ, rollback drill — R14, tasks.md
    // Task 21) is satisfied here only in the weak sense that the Registry holds
    // no specialty-agent records yet, so there is nothing to diverge; it must be
    // taken seriously before an org-scoped agent is published.
    agentRegistry: {
      registryName: "mediaworks-harness-capabilities-sandbox",
      registryId: "EXAMPLEregistry1",
      projectionEnabled: true,
      // specialty-agent reads move off the Runtime. Measured in sandbox before
      // this flip: the Agents tab cost 7.5s on first open — two `/lab` calls
      // (`ListMyPublishedAssets` + `ListSpecialtyAgentDrafts`) whose latency was
      // almost entirely Main Agent runtime cold start (warm: ~290ms). The
      // Capability API answers the same question from the same DynamoDB table in
      // ~220ms, cold or warm, because it is always-on Fargate.
      //
      // `registry` — the Capability API additionally merges Agent Registry rows
      // into the served answer. Promoted from `canonical` on 2026-08-13 after
      // verifying the Registry read path end to end.
      //
      // What this does NOT change: the latency win, which came entirely from
      // moving off the Runtime (`canonical`). The Registry is an additional
      // source queried concurrently under a bounded timeout, not a replacement
      // for DynamoDB — `_source_plan` runs asset-index in every mode.
      //
      // What it DOES change: a Registry failure now lands in `partialErrors` and
      // the UI shows "Partial results — unavailable: agent-registry" even when
      // the agent list is complete from DynamoDB. Drop back to `canonical` if
      // that proves noisy; it keeps the speed and loses only the merge.
      //
      // Registry contents today: empty. Every agent in sandbox is `private`
      // scope and only `org` scope is registry-eligible (`lab/outbox.py`), so the
      // projection-state table has zero rows and this merge is currently a no-op
      // on the results. It exercises the path, it does not yet add data.
      //
      // Rollback is this line to `canonical` (keeps the speed) or `legacy` (back
      // to the Runtime) + a redeploy. There is no automatic fallback to the
      // Runtime, by design (R12 / Property 5).
      //
      // `skill` follows for the same measured reason, and is deliberately
      // `canonical` rather than `registry`: skills are not projected (only
      // `org`-scoped rows are registry-eligible, `lab/outbox.py`), so a Registry
      // query would add a concurrent read and a possible "Partial results —
      // unavailable: agent-registry" banner in exchange for zero extra rows.
      // Measured on the Skills tab before this flip: TWO `/lab` calls at 7,000ms
      // and 7,114ms. That pairing is structural, not a double-fetch bug — the
      // legacy path needs one round trip for drafts and another for published
      // versions (`LabContext.fetchCategory`), where the Capability API answers
      // both from one `bootstrapCapabilities` call. So this removes a request as
      // well as the cold start.
      //
      // NOT flipped for prod, which has no `capabilitySources` at all, because
      // this path has no automatic fallback — flipping a category before the
      // target env's Capability API is serving takes that tab from slow to
      // blank. staging matched this block on 2026-08-25, but only after its
      // service was verified serving; the evidence is listed in its own
      // `agentRegistry` comment. prod already runs the service and needs the
      // same verification plus a registry of its own before it follows.
      // `tool-collection`, `workflow` and `hook` follow as `canonical`: all three
      // are already in `aggregate._ASSET_CATEGORIES`, so the read path needed no
      // new code — only this flag. `canonical` rather than `registry` for the same
      // reason as `skill` for the first two, and because `hook` is excluded from
      // the Registry by design (R3).
      //
      // Since `capabilitySources` is also what gates the control-plane LAB route
      // (`controlPlaneServesLabActions` treats any canonical/registry category as
      // proof the service is deployed and wanted here), these flips additionally
      // move the 35 control-plane lab actions — every drill-in, Save and Publish
      // — off the Runtime for every category, not just these three.
      //
      // `tool` stays `legacy` deliberately. The Tools tab already reads through
      // `/catalog`, which is control-plane-first with a runtime fallback (measured
      // 8.6s → ~0.5s in `ui/server/app.ts`), and flipping `tool` would route the
      // catalog through `read_gateway_catalog`, whose gateway enumeration returns
      // nothing for MCP-server targets — trading a solved problem for missing
      // federated tools.
      capabilitySources: {
        "specialty-agent": "registry",
        "skill": "canonical",
        "tool-collection": "canonical",
        "workflow": "canonical",
        "hook": "canonical",
      },
      shadowSamplePercent: 0,
    },
    // Projects / Project_Token / Search_Gateway (media-harness-projects-access). Enables the nested
    // ProjectsStack in sandbox. One signing key; default 3600 s token lifetime.
    // TODO(operator): set `tokenTestInvokerRoleArns` to the deployed live-test role ARN before
    // running agent/tests/live/test_search_gateway_live.py.
    projects: {
      enabled: true,
      signingKeys: [{ id: "projects-signing-1" }],
      // media-harness-project-ingestion: enable the nested IngestionStack (Asset_Service) in sandbox.
      ingestion: { enabled: true },
    },
  },
  prod: {
    env: "prod",
    account: "100000000003",
    region: "us-west-2",
    // The harness's light Bedrock guardrail, for admins to pick in Settings -> Safety (off until they do).
    harnessGuardrail: true,
    // AgentCore Web Search on the shared Platform Gateway, switched per chat from the + menu (on by default).
    // Approved for prod in 0.13.0 (2026-10-21) after the staging test matrix; us-west-2 verified 2026-10-20.
    webSearch: { enabled: true },
    // Artifacts CDN + public /api/artifact locked (asset-lineage Phase 3: media is private in every
    // env, which is the point of the phase — a project's media was reachable by URL alone). Same
    // shape as staging, which has run it since 2026-10: the viewer-request function denies every
    // request except the two public ffmpeg release tarballs the code-interpreter sandbox downloads,
    // `/api/artifact` becomes authenticated + session-ownership checked (`ARTIFACTS_REQUIRE_AUTH`),
    // the agent rewrites artifact URLs in tool inputs to presigned GETs (`ARTIFACTS_CDN_PRIVATE`),
    // and prod's Slack replies link the UI opener (`/open?key=`) instead of a CDN URL that would 403.
    artifactsCdnPrivate: true,
    // Slack ingress — the SAME additive construct staging uses, now enabled for
    // prod. Presence of this block (enabled:true) is what makes agent-infra-stack
    // create the SlackIntegration (API Gateway POST /slack/events + worker/notifier
    // Lambdas + an EMPTY Secrets Manager entry). After `cdk deploy`, populate the
    // SlackSecretArn out-of-band (signing secret + bot token) and paste the
    // SlackEventsUrl output into Slack's Event Subscriptions Request URL.
    // demoActorId is a DEDICATED synthetic actor (not a real user's Cognito sub),
    // so Slack traffic is isolated from human sessions.
    slack: {
      enabled: true,
      demoActorId: "slack-demo",
      sessionSalt: "prod",
      rendererBaseUrl: "https://dexamplecdn005.cloudfront.net",
      maxUploadBytes: 52428800,
      maxFiles: 5,
      maxFileBytes: 1073741824,
      maxAggregateBytes: 2147483648,
    },
    // Path A — dedicated M2C media-workflow-gateway (v6) deployed in this account
    // (100000000003 / us-west-2) from the micro-workflow-bundle (stack
    // mediaworks-micro-workflow-bundle, CREATE_COMPLETE 2026-08-16). Exposes all 8
    // workflows as MCP tools via gateway media-workflow-gateway-v6-prod, with its
    // own isolated proxy bucket (m2cv6-0e0000000003-*-proxy). Superseded the legacy
    // co-located gateway media-workflow-gateway-staging-staging-wpxlou2m0h (retained
    // until the v6 live render is validated, then deleted).
    media: {
      gatewayUrl:
        "https://media-workflow-gateway-v6-prod-examplegw3.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp",
      clientId: "1exampleclientid0000000003",
      credentialsSecretArn:
        "arn:aws:secretsmanager:us-west-2:100000000003:secret:/media-workflow-gateway-v6/prod/gateway-credentials-EXMPL3",
      tokenEndpoint:
        "https://media-workflow-gateway-v6-prod-100000000003.auth.us-west-2.amazoncognito.com/oauth2/token",
      // Cognito IdP discovery endpoint of the prod M2C pool (NOT the hosted-UI domain). Pool id
      // resolved from the token domain via DescribeUserPoolDomain (2026-10-11); its token_endpoint
      // matches `tokenEndpoint` above. Required for the Search_Gateway target sync
      // (SYNCHRONIZE_UNSUCCESSFUL otherwise) once `projects` is enabled.
      oidcDiscoveryUrl:
        "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_EXAMPLE03/.well-known/openid-configuration",
      scope: "media-workflows/invoke",
      proxyBucket: "m2cv6-0e0000000003-100000000003-us-west-2-proxy",
      m2cServiceRoleArns: [
        "arn:aws:iam::100000000003:role/m2cv6-0e0000000003/mediaworks-micro-workflow-bu-AudioStemSeparatorRole-EXAMPLE00015",
        "arn:aws:iam::100000000003:role/m2cv6-0e0000000003/mediaworks-micro-workflow-bun-SpeechDiarizationRole-EXAMPLE00016",
        "arn:aws:iam::100000000003:role/m2cv6-0e0000000003/mediaworks-micro-workflow-bundl-MediaProcessingRole-EXAMPLE00017",
        // shot-segmentation reads the source object DIRECTLY from its own Lambda
        // (HeadObject in probe_video_for_chunk_transcode before any handoff), so it
        // needs the same skills/uploads read grant as MediaProcessingRole or every
        // job on a harness-uploaded asset fails 403 HeadObject Forbidden. face-
        // recognition is deliberately omitted (it works off the proxy bucket and
        // delegates the source read to media-processing).
        "arn:aws:iam::100000000003:role/m2cv6-0e0000000003/mediaworks-micro-workflow-bund-ShotSegmentationRole-EXAMPLE00018",
      ],
      // hybrid-search's indexer embeds an image's Search_Rendition straight from the Ingest bucket
      // (projects/<pid>/<asset>/rendition.jpg); its identity policy covers only the M2C proxy bucket.
      // Same gap staging measured 2026-10-06. Role ARN read from IAM 2026-10-11.
      renditionReaderRoleArns: [
        "arn:aws:iam::100000000003:role/m2cv6-0e0000000003/mediaworks-micro-workflow-bu-HybridSearchLambdaRole-EXAMPLE00019",
      ],
    },
    // Harness-shared capability catalog. Created out of band by
    // `AWS_PROFILE=<prod> uv run python agent/scripts/registry_provision.py
    //  --env prod --execute --i-understand-this-is-production` — prod is the one
    // env where `--execute` alone is refused (R16), and the script still verifies
    // the ambient account is 100000000003 before acting, so a wrong-profile run
    // cannot create prod's registry somewhere else. Paste ONLY `registryId`: the
    // ARN, the three IAM grants, and the `REGISTRY_*` env vars on both the
    // projector Lambda and the Capability API task all derive from it.
    //
    // Brings prod to parity with staging, which has run this exact shape since
    // 2026-08-25. Verified against the DEPLOYED prod service before flipping —
    // this path has no automatic fallback, so a category flipped ahead of a
    // serving Capability API turns a slow tab blank
    // (`docs/incident-2026-08-25-staging-built-in-panels.md`):
    //   * CapabilityApiService 1/1, both ALB target groups `healthy`
    //   * `/control-plane/config` 200 in ~220ms on the internet-facing ALB
    //   * the task role really holds `bedrock:ListInferenceProfiles` — the IAM
    //     half no test guards, and the half that bit sandbox as an HTTP 200
    //     carrying a `ModuleNotFoundError`-shaped exception
    //   * `mediaworks-harness-registry-projector` already exists here, disarmed
    //     (`REGISTRY_ID=""`, `REGISTRY_PROJECTION_ENABLED=false`), so this block
    //     arms existing infrastructure rather than adding any
    //
    // Projection ON. As in staging this is a no-op until an org-scoped asset
    // ships: `lab/outbox.py` filters to `ELIGIBLE_SCOPES = {"org"}`, and a
    // `private` publish emits no outbox event at all.
    agentRegistry: {
      registryName: "mediaworks-harness-capabilities-prod",
      registryId: "EXAMPLEregistry3",
      projectionEnabled: true,
      capabilitySources: {
        // Byte-for-byte staging's map, so "prod matches staging" is checkable by
        // diffing `/control-plane/config` between the two ALBs rather than by
        // reading this file. `tool` stays `legacy` for the reason in the sandbox
        // block: the Tools tab reads through `/catalog`, which is control-plane
        // first, and routing it through `read_gateway_catalog` returns nothing
        // for MCP-server targets.
        "specialty-agent": "registry",
        "skill": "canonical",
        "tool-collection": "canonical",
        "workflow": "canonical",
        "hook": "canonical",
      },
    },
    // Projects (media-harness-projects-access + project-ingestion + asset-lineage), enabled for the
    // 2026-10 release. Same shape as staging, which has run it since 2026-10; graph stays parked.
    // sessionOutputLayout / assetAuditTable are deliberately NOT set here (sandboxScopedCredentials is on
    // by default everywhere since B2 — see its doc comment):
    // they are independent staging-first flags, and readers accept both output roots, so they can
    // follow in a later deploy without any data change (docs/prod-projects-enablement-2026-10-11.md).
    projects: {
      enabled: true,
      signingKeys: [{ id: "projects-signing-1" }],
      // Same as staging, for M2C alpha.20261014a (prod upgraded 2026-10-15): its default Sonnet 5.5 rejects
      // the `thinking: disabled` M2C sends, so image descriptions need "adaptive".
      ingestion: {
        enabled: true, generatedMedia: true,
        imageModelChain: ["anthropic.claude-sonnet-5-5"], imageThinking: "adaptive",
        // The demo tour's 0-demo.mp4, pinned first in every user's Personal project (personal-samples.ts).
        personalSamples: true,
      },
      graph: { enabled: false },
    },
  },
};

/**
 * A harvested `media` config for one env, written by
 * `scripts/harvest-media-toolkit-config.sh` from a deployed media-toolkit
 * stack's Outputs + credentials secret. Lets a fresh deploy (a new customer
 * account, a redeployed toolkit) pick up federation values without editing
 * this file: run the harvest script, then `cdk deploy` — no code change.
 *
 * Wins over whatever `CONFIGS[env].media` has inline when present, so
 * existing environments with literal values keep working unchanged until
 * someone harvests a fresher file for them.
 */
function loadHarvestedMediaConfig(envName: string): MediaWorkflowGatewayConfig | undefined {
  const file = path.join(__dirname, "..", "media-config", `${envName}.json`);
  if (!fs.existsSync(file)) return undefined;
  return JSON.parse(fs.readFileSync(file, "utf-8")) as MediaWorkflowGatewayConfig;
}

/** Resolve the active EnvConfig from `-c env=<name>` / DEPLOY_ENV (default customer). */
export function resolveEnvConfig(app: App): EnvConfig {
  const name = (app.node.tryGetContext("env") ||
    process.env.DEPLOY_ENV ||
    "customer") as EnvName;
  const cfg = CONFIGS[name];
  if (!cfg) {
    throw new Error(
      `Unknown env '${name}'. Valid values: ${Object.keys(CONFIGS).join(", ")}`,
    );
  }
  const harvested = loadHarvestedMediaConfig(name);
  let resolved: EnvConfig = harvested ? { ...cfg, media: harvested } : cfg;
  if (name === "customer") {
    const projectName = app.node.tryGetContext("projectName") || "mediaworks-harness";
    resolved = {
      ...resolved,
      account: process.env.CDK_DEFAULT_ACCOUNT,
      region: process.env.CDK_DEFAULT_REGION || process.env.AWS_REGION,
      agentRegistry: { ...resolved.agentRegistry, registryName: `${projectName}-capabilities-customer` },
    };
  }
  // CLI overrides for the toolkit wiring and the VPC, valid for any env.
  const toolkitStackName: string | undefined = app.node.tryGetContext("toolkitStackName");
  const deployToolkit = app.node.tryGetContext("deployToolkit");
  if (toolkitStackName) {
    resolved = { ...resolved, toolkit: { ...resolved.toolkit, deploy: false, stackName: toolkitStackName } };
  } else if (deployToolkit !== undefined) {
    resolved = { ...resolved, toolkit: { ...resolved.toolkit, deploy: deployToolkit === true || deployToolkit === "true" } };
  }
  const vpcId: string | undefined = app.node.tryGetContext("vpcId");
  if (vpcId) resolved = { ...resolved, vpcId };
  // Deploy-time preload inputs (design Task outline 9): the owner email and the package path.
  const ownerEmail: string | undefined = app.node.tryGetContext("ownerEmail");
  const preloadPath: string | undefined = app.node.tryGetContext("preloadPath");
  // An explicit re-run nonce to force preload after uploading a package to an existing same-commit stack.
  // A nonce ALONE does not arm preload (that still needs ownerEmail/preloadPath, or base-config preload);
  // it only rides along when preload is otherwise configured.
  const preloadNonce: string | undefined = app.node.tryGetContext("preloadNonce");
  if (ownerEmail || preloadPath || (preloadNonce && resolved.preload)) {
    resolved = {
      ...resolved,
      preload: {
        ...resolved.preload,
        ...(ownerEmail ? { ownerEmail } : {}),
        ...(preloadPath ? { preloadPath } : {}),
        ...(preloadNonce ? { preloadNonce } : {}),
      },
    };
  }
  validateProjectsConfig(resolved);
  return resolved;
}

/**
 * Validate `projects` env-config (Req 4.3, 8.6). Throws naming the env and the offending value for a
 * token lifetime outside 60–3600.
 */
export function validateProjectsConfig(cfg: EnvConfig): void {
  const projects = cfg.projects;
  if (!projects) return;
  const lifetime = projects.tokenLifetimeSeconds;
  if (lifetime !== undefined && (!Number.isInteger(lifetime) || lifetime < 60 || lifetime > 3600)) {
    throw new Error(
      `env '${cfg.env}': projects.tokenLifetimeSeconds=${lifetime} is outside the allowed range ` +
        `60–3600 seconds`,
    );
  }
  if (projects.enabled) {
    const live = (projects.signingKeys ?? []).filter((k) => !k.retired);
    if (live.length !== 1) {
      throw new Error(
        `env '${cfg.env}': projects.signingKeys must have exactly one non-retired key (found ` +
          `${live.length})`,
      );
    }
  }
  if (projects.lineageEdges && !(projects.ingestion?.enabled && projects.ingestion.generatedMedia)) {
    throw new Error(
      `env '${cfg.env}': projects.lineageEdges requires projects.ingestion.enabled and ingestion.generatedMedia ` +
        "(the Generated_Media registrar is the edge writer)",
    );
  }
}

/** Regions the AgentCore Web Search connector supports (reviewed 2026-10-20; us-west-2 verified live). */
export const WEB_SEARCH_SUPPORTED_REGIONS: readonly string[] = [
  "us-east-1", "us-east-2", "us-west-2", "eu-west-1", "ap-southeast-2", "ap-northeast-1", "us-gov-west-1",
];
