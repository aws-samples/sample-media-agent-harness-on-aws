import * as cdk from "aws-cdk-lib";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as s3deploy from "aws-cdk-lib/aws-s3-deployment";
import * as s3assets from "aws-cdk-lib/aws-s3-assets";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { NodejsFunction } from "aws-cdk-lib/aws-lambda-nodejs";
import * as lambdaEventSources from "aws-cdk-lib/aws-lambda-event-sources";
import * as cognito from "aws-cdk-lib/aws-cognito";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as kms from "aws-cdk-lib/aws-kms";
import * as sqs from "aws-cdk-lib/aws-sqs";
import * as cr from "aws-cdk-lib/custom-resources";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as origins from "aws-cdk-lib/aws-cloudfront-origins";
import * as events from "aws-cdk-lib/aws-events";
import * as eventsTargets from "aws-cdk-lib/aws-events-targets";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cloudwatchActions from "aws-cdk-lib/aws-cloudwatch-actions";
import * as logs from "aws-cdk-lib/aws-logs";
import * as sns from "aws-cdk-lib/aws-sns";
import * as ssm from "aws-cdk-lib/aws-ssm";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as ecsPatterns from "aws-cdk-lib/aws-ecs-patterns";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as path from "path";
import * as agentcore from "@aws-cdk/aws-bedrock-agentcore-alpha";
import * as bedrock from "aws-cdk-lib/aws-bedrock";
import { Construct } from "constructs";
import {
  assertSkillItemsFit,
  readBuiltinSkills,
  stageBuiltinSkillCatalog,
} from "./builtin-skills";
import { AgentRegistryConfig, EnvConfig, WEB_SEARCH_SUPPORTED_REGIONS } from "./env-config";
import { addMediaGatewaySeed } from "./media-gateway-seed";
import { SlackIntegration } from "./slack-integration";
import { ProjectsStack } from "./projects/projects-stack";
import { IngestionStack, assetServiceFunctionName, generatedMediaQueueName } from "./ingestion/ingestion-stack";
import { GraphStack, graphReaderFunctionName, graphRepositoryName } from "./graph/graph-stack";
import { AssetAuditStack } from "./audit/asset-audit-stack";
import { HarnessAssets } from "./distribution/harness-assets";
import { skillsBucketName as skillsBucketNameFor, skillsBucketProps } from "./skills-bucket";
import { ToolkitLookup, ToolkitLookupProps } from "./toolkit/toolkit-lookup";
import { grantRenditionRead, HybridSearchRoleLookup } from "./toolkit/hybrid-search-role-lookup";
import { AgentRegistryProvider } from "./distribution/agent-registry-resource";
import { delegatedTokenIssuerFunctionName } from "./projects/projects-stack";
import { addPreloadResource } from "./preload";
import { PERSONAL_SAMPLES, PERSONAL_SAMPLES_DIR } from "./personal-samples";

/**
 * Render per-category cutover flags as `CAPABILITY_SOURCE_*` env vars.
 *
 * Keys must match `_FLAG_SUFFIX` in `agent/src/capability/config.py`; an
 * unknown value there is a startup ConfigError rather than a silent default,
 * so a typo here fails the task's health check instead of quietly serving the
 * legacy source forever.
 *
 * Only non-default entries are emitted. An absent var means `legacy`, which
 * keeps the task definition's environment small and makes a `describe-task-
 * definition` diff show exactly which categories have been cut over.
 */
function capabilitySourceEnv(
  sources: AgentRegistryConfig["capabilitySources"],
): Record<string, string> {
  const suffixes: Record<string, string> = {
    "skill": "SKILLS",
    "specialty-agent": "AGENTS",
    "tool": "TOOLS",
    "tool-collection": "TOOL_COLLECTIONS",
    "workflow": "WORKFLOWS",
    "hook": "HOOKS",
  };
  const out: Record<string, string> = {};
  for (const [category, mode] of Object.entries(sources ?? {})) {
    if (!mode || mode === "legacy") continue;
    const suffix = suffixes[category];
    if (!suffix) {
      throw new Error(`unknown capability category '${category}' in env config`);
    }
    out[`CAPABILITY_SOURCE_${suffix}`] = mode;
  }
  return out;
}

export interface AgentInfraStackProps extends cdk.StackProps {
  projectName: string;
  codeInterpreterIdParam?: string;
  /**
   * The release this deploy is (`-c appVersion=`, computed by CI: `YYYY.MM.DD.<pipeline>` for prod,
   * `YYYY.MM.DD-rc.<pipeline>` for staging; `dev` otherwise). Stamped where an operator looks — the stack
   * description and an output, RuntimeConfigSecret (APP_VERSION), the proxy and Capability API task env —
   * and reported by `/config`, `/control-plane/config` and every turn's execution_summary. Deliberately NOT
   * a resource tag: tags propagate to every resource, so each deploy would rewrite all of them.
   */
  appVersion?: string;

  /**
   * Active deployment environment config (account/region + media integration).
   * Resolved in app.ts via `-c env=<name>`. Optional so existing test
   * instantiations remain valid; the diarization/audiostem custom runtimes are
   * gated on `envConfig.legacyM2c` (present for dev, absent for staging/prod
   * which federate to the M2C media-workflow-gateway instead).
   */
  envConfig?: EnvConfig;

  /**
   * Read the media toolkit's wiring at DEPLOY time (ToolkitLookup) and use it as `envConfig.media`,
   * replacing any static block. Set by app.ts for `envConfig.toolkit` — either the toolkit this app
   * deploys underneath (MediaToolkitStack + its nested stack's logical id) or an existing stack.
   */
  toolkitLookup?: ToolkitLookupProps;

  // ---- User pool (human auth) configuration ----
  //
  // The Lab UI authenticates humans against a Cognito user pool that is
  // distinct from the M2M pool used for Gateway-to-Runtime outbound auth.
  // These props control closed-beta behavior and the OAuth redirect URLs
  // registered on the hosted UI client. Sensible defaults are provided so
  // the stack remains deployable without any prop wiring (dev-only
  // localhost callbacks).

  /**
   * OAuth callback URLs registered on the user pool client. The hosted UI
   * will refuse any redirect_uri not present in this list. Defaults to
   * `['http://localhost:3000/auth/callback']` for dev-only deployments
   * (matches the Vite dev server port in `ui/vite.config.ts`).
   */
  userPoolCallbackUrls?: string[];

  /**
   * OAuth logout URLs registered on the user pool client. Defaults to
   * `['http://localhost:3000/']` for dev-only deployments.
   */
  userPoolLogoutUrls?: string[];

  /**
   * Cognito hosted UI domain prefix. Must be globally unique within the
   * region. Defaults to `${projectName}-users-${account}` so each
   * deployment account gets its own prefix without collisions.
   */
  userPoolDomainPrefix?: string;

  // ---- async-job-tracking Polling_Worker configuration ----
  //
  // The Polling_Worker Lambda is fired by an EventBridge schedule rule
  // that runs every `pollIntervalMinutes` minutes. The default of 1
  // minute is the floor EventBridge `Rule` schedules support — sub-
  // minute rates require the newer EventBridge `Scheduler` service
  // (not used here) or a self-scheduling pattern. The design doc
  // (R4.1) called for 15 seconds, but that's not achievable with
  // `events.Rule` + `Schedule.rate(...)`; the trade-off is a worst-
  // case ~1 minute of UI status lag, which is well within R6.3's
  // 5-second SSE-delivery budget once a registry write actually
  // happens (the bound is on push delivery latency, not on poll
  // cadence). Operators can tune it per environment via the optional
  // prop without changing the Lambda code.

  /**
   * EventBridge schedule rate (whole minutes) for the Polling_Worker
   * Lambda. Must be a positive integer >= 1. Defaults to `1`. The
   * schedule is built with `Schedule.rate(Duration.minutes(...))`;
   * EventBridge `Rule`s do NOT support sub-minute rates — switch to
   * EventBridge `Scheduler` (or a self-scheduling Lambda) if a
   * tighter cadence is required.
   */
  pollIntervalMinutes?: number;
}

export class AgentInfraStack extends cdk.Stack {
  // ---- Human auth user pool exports ----
  //
  // Exposed so downstream stacks (UI build env, proxy, runtime) can wire
  // these without re-deriving them from CfnOutput lookups.
  public readonly userPoolId: string;
  public readonly userPoolClientId: string;
  public readonly userPoolDomain: string;
  public readonly userPoolIssuer: string;

  /**
   * The nested ProjectsStack (media-harness-projects-access). Present only when
   * `envConfig.projects.enabled` — undefined in prod/dev. Exposed for the synth test (task 8.12).
   */
  public readonly projectsStack?: ProjectsStack;

  /**
   * The nested IngestionStack (media-harness-project-ingestion). Present only when
   * `envConfig.projects.ingestion.enabled` — undefined in prod/dev and when projects are off. Exposed
   * for the synth test (infra/test/ingestion-stack.synth.test.ts).
   */
  public readonly ingestionStack?: IngestionStack;
  /** The knowledge-graph nested stack; absent unless `projects.graph.enabled` and an image tag is given. */
  public readonly graphStack?: GraphStack;
  /**
   * The append-only `asset_events` audit nested stack (asset-lineage Phase 3). Present only when
   * `envConfig.assetAuditTable` and projects are enabled. Exposed for the synth test.
   */
  public readonly assetAuditStack?: AssetAuditStack;

  constructor(scope: Construct, id: string, props: AgentInfraStackProps) {
    // `suppressTemplateIndentation` strips whitespace from the synthesized
    // template. This stack was at 934KB of CloudFormation's 1MB limit (93.5%)
    // with more resources still to add for user-registered gateway targets, and
    // hitting that ceiling fails the deploy rather than degrading. Indentation
    // is pure formatting -- nothing reads the template by hand -- so this buys
    // headroom at no cost. A stack split is the real fix if it fills up again.
    super(scope, id, { suppressTemplateIndentation: true, ...props });

    // ---- Media toolkit wiring read at deploy time ----
    // Must run before ANY read of `props.envConfig.media` below: every consumer sees the
    // lookup's deploy-time tokens instead of a static block. The toolkit's ingest-bucket reader
    // roles are bridged in only for an EXTERNAL toolkit — the one this app deploys reads the
    // shared Skills bucket (its IngestBucket) through its own identity policies.
    let toolkitIngestReadRolesCsv: string | undefined;
    // Hand-listed hybrid-search roles (staging/prod's static `media` block) survive the lookup's
    // replacement of `media`, so an env can still pin one explicitly.
    const staticRenditionReaderArns = props.envConfig?.media?.renditionReaderRoleArns ?? [];
    const toolkitLookup = props.toolkitLookup;
    if (toolkitLookup) {
      const lookup = new ToolkitLookup(this, "MediaToolkitLookup", toolkitLookup);
      props = { ...props, envConfig: { ...props.envConfig!, media: lookup.media } };
      if (!toolkitLookup.nestedLogicalId) toolkitIngestReadRolesCsv = lookup.ingestReadRoleArnsCsv;
    }

    const { projectName } = props;
    const codeInterpreterId = props.codeInterpreterIdParam ?? "";

    // Environment marker stamped on outbox events, projection-state partition
    // keys, and every Python service's `DEPLOY_ENV`. Falls back to "dev" for the
    // dev/local config, matching `resolveEnvConfig`'s own default.
    //
    // Declared HERE, at the top of the constructor, rather than next to the
    // registry config where it used to live: the agent runtimes are defined
    // earlier in this file and they need it too, because an outbox event written
    // without it is stamped "unknown" and the projector drops it.
    const envName = props.envConfig?.env ?? "dev";
    const appVersion = props.appVersion || "dev";
    new cdk.CfnOutput(this, "AppVersion", { value: appVersion, description: "The release this stack runs" });

    // Default models and reasoning effort: agent/src/model/models.json is THE one place they are set. The agents
    // read it from their own bundle (model/defaults.py); the deploy reads it here for the two values outside the
    // agents: the UI proxy's `GET /models` default (so the SPA never invents one from list position — it once took
    // `models[0]`, Claude 3 Haiku, and every turn failed) and BEDROCK_REASONING_EFFORT. No context override: a
    // second place to set a model is how the task agent once ran Opus 5 while its code said Sonnet 5.5.
    const modelDefaults = JSON.parse(
      require("fs").readFileSync(path.join(__dirname, "../../agent/src/model/models.json"), "utf-8"),
    ) as { reasoning_effort: string; agents: Record<string, string> };
    const defaultModelId = modelDefaults.agents.orchestrator;
    if (!defaultModelId || !modelDefaults.reasoning_effort) {
      throw new Error("agent/src/model/models.json needs agents.orchestrator and reasoning_effort");
    }

    // ---- Container images + SPA: pushed by CI, or built here in CodeBuild ----
    //
    // CI (kaniko) pushes each image to an out-of-band ECR repo and passes its tag as
    // `-c uiProxyImageTag` / `-c capabilityApiImageTag` / `-c graphLambdaImageTag`. Any image
    // whose tag is NOT passed is built at deploy time in CodeBuild (HarnessAssets) into a
    // CDK-owned repo, so a plain `cdk deploy` needs no local Docker, ECR setup or tags. The UI
    // build also produces the SPA, replacing the prebuilt `ui/dist` asset.
    const uiProxyImageTagCtx: string | undefined =
      this.node.tryGetContext("uiProxyImageTag") ?? process.env.UI_PROXY_IMAGE_TAG;
    const capabilityApiImageTagCtx: string | undefined =
      this.node.tryGetContext("capabilityApiImageTag") ?? process.env.CAPABILITY_API_IMAGE_TAG;
    const graphImageTagCtx: string | undefined =
      this.node.tryGetContext("graphLambdaImageTag") ?? process.env.GRAPH_LAMBDA_IMAGE_TAG;
    const graphWanted =
      props.envConfig?.projects?.enabled === true &&
      props.envConfig?.projects?.ingestion?.enabled === true &&
      props.envConfig?.projects?.graph?.enabled === true;
    // The sandbox's ffmpeg, staged on the Skills bucket (`tools/ffmpeg/`); the sandbox role reads it.
    const ffmpegWanted = props.envConfig?.ffmpegMirror === true;
    const harnessAssets =
      !uiProxyImageTagCtx || !capabilityApiImageTagCtx || (graphWanted && !graphImageTagCtx) || ffmpegWanted
        ? new HarnessAssets(this, "HarnessBuilds", {
            ui: !uiProxyImageTagCtx,
            capability: !capabilityApiImageTagCtx,
            graph: graphWanted && !graphImageTagCtx,
            ffmpeg: ffmpegWanted ? { bucketName: skillsBucketNameFor(projectName, this) } : undefined,
          })
        : undefined;

    // ---- Bucket reuse vs create policy ----
    //
    // The stack defaults to importing buckets that already exist — every
    // bucket in the platform uses `RemovalPolicy.RETAIN`, so they
    // typically survive a `cdk destroy` and re-creating them on every
    // deploy would either fail (name collision) or wipe content.
    //
    // To create a brand-new bucket on this deploy (e.g. fresh dev
    // environment, region migration, name change), pass either:
    //   * `cdk deploy -c createBuckets=true` — applies to ALL buckets
    //   * `cdk deploy -c createSkillsBucket=true` — only the skills bucket
    //   * `cdk deploy -c createLabArtifactsBucket=true` — only the lab bucket
    //   * `cdk deploy -c createUploadsBucket=true` — only the uploads bucket
    //
    // `createBuckets=true` is the override; per-bucket flags fall back
    // to it. When a per-bucket flag is set, it wins over the global one.
    const createBucketsCtx = this.node.tryGetContext("createBuckets");
    const createAllBuckets = createBucketsCtx === undefined
      ? props.envConfig?.createBuckets === true
      : createBucketsCtx === true || createBucketsCtx === "true";
    // When this app deploys the toolkit, MediaToolkitStack creates the shared Skills/Ingest bucket.
    const skillsBucketOwnedElsewhere = props.envConfig?.toolkit?.deploy === true;
    const shouldCreate = (key: string): boolean => {
      const v = this.node.tryGetContext(key);
      if (v === undefined) {
        return key === "createSkillsBucket" && skillsBucketOwnedElsewhere ? false : createAllBuckets;
      }
      return v === true || v === "true";
    };

    // Runtime fan-out (map/gather) master switch. One context flag flips it on across
    // the places that must agree — main agent (validator executability gate),
    // the planning agent (map/gather emission in the fused DAG), and the plan-chainer Lambda
    // (runtime expansion). Off by default; enable with `-c dynamicFanoutEnabled=true`.
    const dynamicFanoutEnabled =
      this.node.tryGetContext("dynamicFanoutEnabled") === true ||
      this.node.tryGetContext("dynamicFanoutEnabled") === "true";
    const dynamicFanoutFlag = dynamicFanoutEnabled ? "true" : "false";

    // ---- Skills S3 Bucket ----
    //
    // Holds the legacy `skills/` tree that the unified-mediaworks-platform
    // migration reads from, plus task-agent plan state under `plans/`.
    // Imported by default; pass `-c createSkillsBucket=true` to create.
    const skillsBucketName = skillsBucketNameFor(projectName, this);
    const skillsBucket: s3.IBucket = shouldCreate("createSkillsBucket")
      ? new s3.Bucket(this, "SkillsBucket", skillsBucketProps(skillsBucketName))
      : s3.Bucket.fromBucketName(this, "SkillsBucket", skillsBucketName);
    if (harnessAssets?.ffmpegBuild && skillsBucket instanceof s3.Bucket) {
      harnessAssets.ffmpegBuild.node.addDependency(skillsBucket);
    }

    const skillsAccessPolicy = new iam.ManagedPolicy(this, "SkillsAccessPolicy", {
      statements: [
        new iam.PolicyStatement({
          actions: ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"],
          resources: [skillsBucket.bucketArn, `${skillsBucket.bucketArn}/*`],
        }),
      ],
    });

    // ---- Static-federation M2C read grant on SkillsBucket ----
    // The toolkit's independently-deployed workflow stacks are scoped to their own
    // IngestBucket, not this harness's SkillsBucket, so bridge their roles in explicitly.
    const m2cServiceRoleArns = props.envConfig?.media?.m2cServiceRoleArns ?? [];
    if (m2cServiceRoleArns.length > 0) {
      const m2cPrincipals = m2cServiceRoleArns.map(
        (arn) => new iam.ArnPrincipal(arn),
      );
      skillsBucket.addToResourcePolicy(
        new iam.PolicyStatement({
          sid: "AllowM2CServiceRolesListBucket",
          effect: iam.Effect.ALLOW,
          principals: m2cPrincipals,
          actions: ["s3:ListBucket"],
          resources: [skillsBucket.bucketArn],
        }),
      );
      skillsBucket.addToResourcePolicy(
        new iam.PolicyStatement({
          sid: "AllowM2CServiceRolesGetObject",
          effect: iam.Effect.ALLOW,
          principals: m2cPrincipals,
          actions: ["s3:GetObject"],
          resources: [`${skillsBucket.bucketArn}/*`],
        }),
      );
    }
    // Same bridge for an EXTERNAL toolkit read at deploy time (ToolkitLookup): its role ARNs are a
    // deploy-time comma-joined token, so they cannot become one ArnPrincipal each. Grant via the
    // principal's ARN instead — fixed-value aws:PrincipalArn/PrincipalAccount conditions keep the
    // statement non-public under BlockPublicAccess.
    if (toolkitIngestReadRolesCsv !== undefined) {
      const toolkitReaders = {
        StringEquals: { "aws:PrincipalAccount": this.account },
        ArnEquals: { "aws:PrincipalArn": cdk.Fn.split(",", toolkitIngestReadRolesCsv) },
      };
      skillsBucket.addToResourcePolicy(new iam.PolicyStatement({
        sid: "AllowMediaToolkitReadersListBucket",
        principals: [new iam.AnyPrincipal()],
        actions: ["s3:ListBucket"],
        resources: [skillsBucket.bucketArn],
        conditions: toolkitReaders,
      }));
      skillsBucket.addToResourcePolicy(new iam.PolicyStatement({
        sid: "AllowMediaToolkitReadersGetObject",
        principals: [new iam.AnyPrincipal()],
        actions: ["s3:GetObject"],
        resources: [`${skillsBucket.bucketArn}/*`],
        conditions: toolkitReaders,
      }));
    }
    // hybrid-search reads an image's Search_Rendition from the Ingest bucket to embed it (Req 8.11);
    // its identity policy covers only the toolkit's proxy bucket. Two sources for the role, one grant:
    //  - static `media` envs (staging/prod) hand-list it in `renditionReaderRoleArns`;
    //  - an EXTERNAL toolkit read at deploy time (`-c toolkitStackName=`) on a bucket this stack owns
    //    has it resolved from the toolkit's stack tree, exactly as MediaToolkitStack does for the
    //    toolkit it deploys (that stack owns the bucket, so it grants there and nothing is added here).
    const renditionReaders: iam.IPrincipal[] = staticRenditionReaderArns.map((arn) => new iam.ArnPrincipal(arn));
    if (toolkitLookup && !toolkitLookup.nestedLogicalId && skillsBucket instanceof s3.Bucket) {
      const role = new HybridSearchRoleLookup(this, "MediaToolkitHybridSearchRole", {
        stackName: toolkitLookup.stackName,
        stackNamePrefix: toolkitLookup.stackName,
        revision: toolkitLookup.revision,
      });
      renditionReaders.push(new iam.ArnPrincipal(role.roleArn));
    }
    if (renditionReaders.length > 0) grantRenditionRead(skillsBucket, renditionReaders);

    // ---- Legacy auxiliary uploads bucket ----
    // Retained for runtime compatibility with `UPLOADS_S3_BUCKET` and existing outputs. The
    // active HTTP `/upload` route writes to SkillsBucket, which source mode also supplies to M2C
    // as IngestBucket.
    // Imported by default outside source mode; pass `-c createUploadsBucket=true` to create.
    const uploadsBucketName: string =
      this.node.tryGetContext("uploadsBucketName") ??
      `${projectName}-uploads-${this.account}-${this.region}`;
    const uploadsBucket: s3.IBucket = shouldCreate("createUploadsBucket")
      ? new s3.Bucket(this, "UploadsBucket", {
          bucketName: uploadsBucketName,
          removalPolicy: cdk.RemovalPolicy.RETAIN,
          versioned: false,
          encryption: s3.BucketEncryption.S3_MANAGED,
          blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
        })
      : s3.Bucket.fromBucketName(this, "UploadsBucket", uploadsBucketName);

    // M2C service roles need read access on the uploads bucket for stem
    // separation and other workflows. The bucket policy is managed outside
    // CDK (applied directly via AWS API) to avoid conflicts with imported
    // bucket scenarios. See docs/async-activity-panel-status.md for context.

    // ---- M2M Cognito Pool (Gateway -> Runtime outbound auth) ----
    const m2mPool = new cognito.UserPool(this, "M2MPool", {
      userPoolName: `${projectName}-m2m-pool`,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const m2mDomain = m2mPool.addDomain("M2MDomain", {
      cognitoDomain: {
        domainPrefix: `${projectName}-m2m-${this.account}`,
      },
    });

    const resourceServer = m2mPool.addResourceServer("McpRuntimeResource", {
      identifier: `${projectName}-mcp-runtime`,
      scopes: [
        { scopeName: "invoke", scopeDescription: "Invoke MCP tools" },
      ],
    });

    const m2mClient = m2mPool.addClient("M2MClient", {
      userPoolClientName: `${projectName}-m2m-client`,
      generateSecret: true,
      oAuth: {
        flows: { clientCredentials: true },
        scopes: [
          cognito.OAuthScope.custom(`${projectName}-mcp-runtime/invoke`),
        ],
      },
    });
    m2mClient.node.addDependency(resourceServer);

    // Get the client secret via custom resource (Cognito doesn't expose it directly)
    const describeClient = new cr.AwsCustomResource(this, "DescribeM2MClient", {
      installLatestAwsSdk: true,
      onCreate: {
        service: "CognitoIdentityServiceProvider",
        action: "describeUserPoolClient",
        parameters: {
          UserPoolId: m2mPool.userPoolId,
          ClientId: m2mClient.userPoolClientId,
        },
        physicalResourceId: cr.PhysicalResourceId.of("m2m-client-secret"),
      },
      policy: cr.AwsCustomResourcePolicy.fromSdkCalls({
        resources: [m2mPool.userPoolArn],
      }),
    });
    describeClient.node.addDependency(m2mClient);
    const m2mClientSecret = describeClient.getResponseField("UserPoolClient.ClientSecret");

    const m2mDiscoveryUrl = `https://cognito-idp.${this.region}.amazonaws.com/${m2mPool.userPoolId}/.well-known/openid-configuration`;
    const m2mScope = `${projectName}-mcp-runtime/invoke`;

    // ---- User Pool (Lab UI human auth) ----
    //
    // Distinct from the M2M pool above. This pool backs the Lab UI's
    // hosted sign-in flow: humans authenticate via Cognito hosted UI,
    // receive an ID + access + refresh token, and the access token is
    // forwarded to the Vite proxy + AgentCore runtime for actor-keyed
    // authorization. Email is the primary sign-in alias; passwords +
    // self sign-up are configurable per environment.
    //
    // The runtime authorizer flip from IAM to JWT against this pool is
    // staged separately (see user-auth task 7.1) — this construct is
    // non-disruptive on its own.
    // Self-registration MUST remain disabled (Palisade V2284101886).
    // Admin-created users only — no public sign-up.
    const userPoolSelfSignUpEnabled = false;
    const userPoolCallbackUrls =
      props.userPoolCallbackUrls ?? ["http://localhost:3000/auth/callback"];
    const userPoolLogoutUrls =
      props.userPoolLogoutUrls ?? ["http://localhost:3000/"];
    const userPoolDomainPrefix =
      props.userPoolDomainPrefix ?? `${projectName}-users-${this.account}`;

    const userPool = new cognito.UserPool(this, "UserPool", {
      userPoolName: `${projectName}-user-pool`,
      signInAliases: { email: true },
      selfSignUpEnabled: userPoolSelfSignUpEnabled,
      autoVerify: { email: true },
      standardAttributes: {
        email: { required: true, mutable: true },
      },
      passwordPolicy: {
        minLength: 8,
        requireLowercase: true,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: false,
      },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    const userPoolDomain = userPool.addDomain("UserPoolDomain", {
      cognitoDomain: {
        domainPrefix: userPoolDomainPrefix,
      },
      // Managed Login (v2) instead of the classic Hosted UI — required for
      // the CfnManagedLoginBranding resource below to apply (branding only
      // renders on Managed Login pages, not the classic hosted sign-in).
      managedLoginVersion: cognito.ManagedLoginVersion.NEWER_MANAGED_LOGIN,
    });

    const userPoolClient = userPool.addClient("LabUIClient", {
      userPoolClientName: `${projectName}-lab-ui-client`,
      generateSecret: false, // Public SPA client — PKCE on auth code grant
      authFlows: {
        userSrp: true,
      },
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [
          cognito.OAuthScope.OPENID,
          cognito.OAuthScope.EMAIL,
          cognito.OAuthScope.PROFILE,
        ],
        callbackUrls: userPoolCallbackUrls,
        logoutUrls: userPoolLogoutUrls,
      },
      // Refresh tokens enabled with a 30-day validity (R2 in user-auth
      // requirements). Access + ID tokens default to 1 hour.
      refreshTokenValidity: cdk.Duration.days(30),
      accessTokenValidity: cdk.Duration.hours(1),
      idTokenValidity: cdk.Duration.hours(1),
      preventUserExistenceErrors: true,
      supportedIdentityProviders: [
        cognito.UserPoolClientIdentityProvider.COGNITO,
      ],
    });

    // ---- Async-plan machine client (Wave 1 / option E — front door) ----
    //
    // A plan node runs asynchronously (plan-chainer -> Task Agent) with no
    // user JWT, so it cannot authenticate to this gateway, whose CUSTOM_JWT
    // authorizer takes exactly ONE discoveryUrl (this end-user pool). Option E
    // decouples ACTOR IDENTITY from the token: the actor rides a signed
    // delegation grant the interceptor verifies. So the front door only needs
    // a VERIFIABLE MACHINE caller — a token with no user `sub` is fine.
    //
    // A CUSTOM_JWT authorizer has one discoveryUrl, so this machine client MUST
    // live in THIS pool (the M2M pool is a different issuer the gateway would
    // reject). The end-user pool had no client_credentials client, so one is
    // added here: a confidential client + resource server + custom scope. The
    // pool's own domain (userPoolDomain) provides the token endpoint. Its id is
    // added to the gateway's allowedClients below; the Task Agent mints a token
    // with it and presents the grant as the interceptor's grant header.
    const asyncPlanResourceServer = userPool.addResourceServer(
      "AsyncPlanResource",
      {
        identifier: `${projectName}-async-plan`,
        scopes: [
          {
            scopeName: "invoke",
            scopeDescription:
              "Cross the harness gateway front door for async plan nodes; the actor is carried by a signed delegation grant, not this token.",
          },
        ],
      },
    );
    const asyncPlanScope = `${projectName}-async-plan/invoke`;
    const asyncPlanClient = userPool.addClient("AsyncPlanClient", {
      userPoolClientName: `${projectName}-async-plan-client`,
      // Confidential client: client_credentials REQUIRES a secret. Kept
      // separate from LabUIClient because client_credentials cannot coexist
      // with the authorizationCodeGrant/SRP flow on one client.
      generateSecret: true,
      oAuth: {
        flows: { clientCredentials: true },
        scopes: [cognito.OAuthScope.custom(asyncPlanScope)],
      },
    });
    asyncPlanClient.node.addDependency(asyncPlanResourceServer);
    const asyncPlanTokenEndpoint = `https://${userPoolDomain.domainName}.auth.${this.region}.amazoncognito.com/oauth2/token`;

    // Managed Login branding for `LabUIClient`'s sign-in page. Starts from
    // Cognito's default style but swaps in the app's own logo (rendered
    // above the sign-in form) — everything else (colors, layout) stays at
    // Cognito's defaults, editable later via the branding editor's
    // `settings` JSON without touching the frontend at all.
    //
    // The logo is a dedicated 240x240 copy (infra/assets/login-logo.jpg, 13 KB), NOT the
    // 2048x2048 ui/public/logo.jpg (256 KB). Cognito inlines the bytes as base64 into THIS
    // template, once per colour mode, so the full-size logo made this one resource 685 KB of a
    // template capped at 1 MB (staging stood at 95.6%). The sign-in form renders it at 60x60 CSS
    // pixels; 240 px covers a 4x display, and against the original it differs by ~1/255 mean
    // at 1x-3x. Regenerate it from the UI logo with:
    //   sips -s format jpeg -s formatOptions 90 -Z 240 ui/public/logo.jpg --out infra/assets/login-logo.jpg
    const logoBase64 = require("fs").readFileSync(
      path.join(__dirname, "../assets/login-logo.jpg"),
    ).toString("base64");
    new cognito.CfnManagedLoginBranding(this, "UserPoolManagedLoginBranding", {
      userPoolId: userPool.userPoolId,
      clientId: userPoolClient.userPoolClientId,
      // `useCognitoProvidedValues` and `settings` are mutually exclusive —
      // Cognito rejects the request if both (or neither) are given.
      // Uploading a FORM_LOGO asset alone does not display it — Cognito's
      // default style has `components.form.logo.enabled: false`, so that
      // flag must be flipped on explicitly. Everything else here is left at
      // Cognito's own defaults (confirmed via `describe-managed-login-
      // branding-by-client --return-merged-resources`).
      settings: {
        components: {
          form: {
            logo: { enabled: true },
          },
        },
      },
      // A separate asset is required per color mode — Cognito does not
      // apply a single DYNAMIC-mode asset to both light and dark; the same
      // bytes are registered twice, once per mode.
      assets: [
        {
          category: "FORM_LOGO",
          colorMode: "LIGHT",
          extension: "JPEG",
          bytes: logoBase64,
        },
        {
          category: "FORM_LOGO",
          colorMode: "DARK",
          extension: "JPEG",
          bytes: logoBase64,
        },
      ],
    });

    const userPoolIssuerUrl =
      `https://cognito-idp.${this.region}.amazonaws.com/${userPool.userPoolId}`;

    // Surface as stack props for downstream consumers (UI build env,
    // proxy JWT validator, runtime authorizer).
    this.userPoolId = userPool.userPoolId;
    this.userPoolClientId = userPoolClient.userPoolClientId;
    this.userPoolDomain = userPoolDomain.domainName;
    this.userPoolIssuer = userPoolIssuerUrl;

    // ---- gateway-admins Cognito group ----
    //
    // Backs the gateway-registry-admin spec (R3): membership in this
    // group is the runtime's admin gate for every `gateway_*` mutation
    // (`agent/src/main.py::_is_admin` checks `cognito:groups`), and the
    // UI hides the Settings → Gateways tab from non-members
    // (`useIsGatewayAdmin` in `ui/src/auth/isGatewayAdmin.ts`). The
    // group itself is empty on creation — the CDK declares the
    // container; an operator assigns users via the Cognito console or
    // `aws cognito-idp admin-add-user-to-group`. Group name is
    // hard-coded to match `GATEWAY_ADMIN_GROUP` in both the runtime
    // (`agent/src/main.py`) and the UI (`ui/src/auth/isGatewayAdmin.ts`).
    new cognito.CfnUserPoolGroup(this, "GatewayAdminsGroup", {
      userPoolId: userPool.userPoolId,
      groupName: "gateway-admins",
      description:
        "Members can register, edit, disable, delete, reload, and probe MCP gateways via the runtime gateway_* actions and the Settings → Gateways UI panel.",
    });

    // ---- #1: OAuth2 Credential Provider (AgentCore Identity) ----
    const credProviderName = `${projectName}-m2m-oauth2`;

    // Store machine client secret in Secrets Manager (avoids logging in Lambda)
    const m2mSecretStore = new cdk.aws_secretsmanager.Secret(this, "M2MClientSecretStore", {
      secretName: `/${projectName}/m2m-client-secret`,
      secretStringValue: cdk.SecretValue.unsafePlainText(m2mClientSecret),
      description: "M2M client secret for OAuth2 credential provider",
    });

    const credProviderFn = new lambda.Function(this, "CredProviderFn", {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: "index.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "cred-provider-handler")),
      timeout: cdk.Duration.minutes(5),
    });

    // Grant secret read access
    m2mSecretStore.grantRead(credProviderFn);

    // Grant AgentCore Identity permissions (matching reference implementation)
    credProviderFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "bedrock-agentcore:CreateOauth2CredentialProvider",
          "bedrock-agentcore:UpdateOauth2CredentialProvider",
          "bedrock-agentcore:DeleteOauth2CredentialProvider",
          "bedrock-agentcore:GetOauth2CredentialProvider",
        ],
        resources: [
          `arn:aws:bedrock-agentcore:${this.region}:${this.account}:token-vault/default`,
          `arn:aws:bedrock-agentcore:${this.region}:${this.account}:token-vault/default/oauth2credentialprovider/*`,
        ],
      })
    );
    credProviderFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "bedrock-agentcore:CreateTokenVault",
          "bedrock-agentcore:GetTokenVault",
        ],
        resources: [
          `arn:aws:bedrock-agentcore:${this.region}:${this.account}:token-vault/default`,
          `arn:aws:bedrock-agentcore:${this.region}:${this.account}:token-vault/default/*`,
        ],
      })
    );
    credProviderFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "secretsmanager:CreateSecret",
          "secretsmanager:DeleteSecret",
          "secretsmanager:PutSecretValue",
        ],
        resources: [
          `arn:aws:secretsmanager:${this.region}:${this.account}:secret:bedrock-agentcore-identity!default/oauth2/*`,
        ],
      })
    );

    const credProviderProvider = new cr.Provider(this, "CredProviderProvider", {
      onEventHandler: credProviderFn,
    });

    const credProviderResource = new cdk.CustomResource(this, "CredProviderCR", {
      serviceToken: credProviderProvider.serviceToken,
      properties: {
        ProviderName: credProviderName,
        ClientSecretArn: m2mSecretStore.secretArn,
        DiscoveryUrl: m2mDiscoveryUrl,
        ClientId: m2mClient.userPoolClientId,
      },
    });
    credProviderResource.node.addDependency(m2mClient);

    const credProviderArn = credProviderResource.getAttString("ProviderArn");

    // ---- #3: AWS MCP Server Runtime with JWT auth ----
    // Use the same zip-packager approach as the main agent to ensure deps are installed
    const mcpCodeBucket = new s3.Bucket(this, "McpCodeBucket", {
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
    });

    const mcpPackagerLambda = new lambda.Function(this, "McpZipPackagerLambda", {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: "index.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "zip-packager")),
      timeout: cdk.Duration.minutes(10),
      memorySize: 1024,
      ephemeralStorageSize: cdk.Size.gibibytes(2),
    });
    mcpCodeBucket.grantReadWrite(mcpPackagerLambda);

    // Read MCP server source
    const mcpSrcDir = path.join(__dirname, "../../agent/mcp-servers/aws-mcp");
    const mcpCode: Record<string, string> = {};
    for (const file of require("fs").readdirSync(mcpSrcDir)) {
      if (file.endsWith(".py") || file === "requirements.txt") {
        mcpCode[file] = require("fs").readFileSync(path.join(mcpSrcDir, file)).toString("base64");
      }
    }

    const mcpRequirements = require("fs").readFileSync(path.join(mcpSrcDir, "requirements.txt"), "utf-8")
      .split("\n")
      .map((l: string) => l.trim())
      .filter((l: string) => l && !l.startsWith("#"));

    const mcpContentHash = require("crypto").createHash("sha256")
      .update(JSON.stringify({ mcpRequirements, mcpCode, buildVersion: "v2-mcp-zip-rebuild" }))
      .digest("hex").slice(0, 16);

    const mcpZipObjectKey = `mcp_deployment_package_${mcpContentHash}.zip`;

    const mcpPackagerProvider = new cr.Provider(this, "McpZipPackagerProvider", {
      onEventHandler: mcpPackagerLambda,
    });

    const mcpZipPackager = new cdk.CustomResource(this, "McpZipPackager", {
      serviceToken: mcpPackagerProvider.serviceToken,
      properties: {
        BucketName: mcpCodeBucket.bucketName,
        ObjectKey: mcpZipObjectKey,
        Requirements: mcpRequirements,
        AgentCode: mcpCode,
        ContentHash: mcpContentHash,
      },
    });

    const mcpRuntime = new agentcore.Runtime(this, "AwsMcpRuntime", {
      runtimeName: `${projectName.replace(/-/g, "_")}_mcp_proxy`,
      agentRuntimeArtifact: agentcore.AgentRuntimeArtifact.fromS3(
        {
          bucketName: mcpCodeBucket.bucketName,
          objectKey: mcpZipObjectKey,
        },
        agentcore.AgentCoreRuntime.PYTHON_3_13,
        ["opentelemetry-instrument", "mcp_server.py"],
      ),
      protocolConfiguration: agentcore.ProtocolType.MCP,
      networkConfiguration: agentcore.RuntimeNetworkConfiguration.usingPublicNetwork(),
      environmentVariables: {
        AWS_REGION: this.region,
      },
      // #3: Configure JWT auth to accept tokens from the M2M pool
      authorizerConfiguration: agentcore.RuntimeAuthorizerConfiguration.usingJWT(
        m2mDiscoveryUrl,
        [m2mClient.userPoolClientId],  // allowed clients
        undefined,                      // allowed audiences
        [m2mScope],                     // allowed scopes
      ),
    });

    mcpRuntime.node.addDependency(mcpZipPackager);

    // #4: Grant the MCP runtime permissions
    mcpRuntime.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "aws-mcp:InvokeMcp",
          "aws-mcp:CallReadOnlyTool",
          "aws-mcp:CallReadWriteTool",
        ],
        resources: ["*"],
      })
    );

    // No skills-bucket grant. This runtime is the aws-mcp PROXY
    // (`agent/mcp-servers/aws-mcp/mcp_server.py`): it forwards tool calls to the upstream AWS MCP
    // service over SigV4 and never touches S3 — the file imports no boto3 and names no bucket. It
    // used to hold `skillsBucket.grantReadWrite(mcpRuntime)`, i.e. read+write on all of our media,
    // for an AWS-documentation search tool. Removed (asset-lineage Phase 3, least privilege); its
    // only remaining permissions are the `aws-mcp:*` actions granted above.

    // ---- Gateway interceptor (per-actor tool scoping) ----
    // Header carrying the end user's Cognito token from the proxy, through the
    // runtime, to the harness gateway. Declared once here because three places
    // must agree on it: the runtime's allowlist, the proxy that sets it, and
    // the agent that reads it from RequestContext.request_headers.
    //
    // The prefix is mandatory, not stylistic. The service docs say any
    // non-restricted header may be forwarded, but the CDK L2 validates against
    // /(Authorization|X-Amzn-Bedrock-AgentCore-Runtime-Custom-[a-zA-Z0-9-]+)/
    // and rejects anything else at synth time (with a misleading "letters,
    // numbers, and hyphens" message). `Authorization` is the only other
    // accepted value and it requires the runtime to use a customJWTAuthorizer,
    // which this runtime deliberately does not.
    const USER_TOKEN_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-UserToken";

    // OIDC discovery for the end-user pool, used as this gateway's inbound
    // authorizer so the interceptor can read a verified identity claim.
    const userDiscoveryUrl =
      `https://cognito-idp.${this.region}.amazonaws.com/${userPool.userPoolId}/.well-known/openid-configuration`;

    // One Lambda serves both interception points. It detects the phase from
    // the payload (REQUEST input carries no `gatewayResponse`) and does two
    // halves of one job: RESPONSE filters `tools/list` and search results so
    // an actor only sees their own registered MCP servers' tools, REQUEST
    // denies `tools/call` for a tool the actor does not own.
    //
    // Both halves are required. Filtering the listing alone is advisory — a
    // model can name a tool it was never shown — so without the REQUEST half
    // the scoping is cosmetic rather than a security boundary.
    // KMS asymmetric key for signed delegation grants (Wave 1 / option E).
    //
    // A plan node runs asynchronously (plan-chainer → Task Agent), long after
    // the user's JWT is gone, so the harness gateway interceptor has no verified
    // identity to scope tools by. The orchestrator (Main Agent) signs a short
    // grant over {actor_id, plan_id, exp} while it still holds the actor; the
    // interceptor verifies it and derives the actor FROM THE GRANT, never from
    // the asserted actor_id. Asymmetric so the private key never leaves KMS —
    // the orchestrator gets kms:Sign, the interceptor gets kms:Verify, and no
    // process (and no plan row) ever holds key material. Both grants are scoped
    // to this key ARN only (see grant() calls on each role). ECC P-256 keeps the
    // signature small enough to ride a request header.
    const delegationGrantKey = new kms.Key(this, "DelegationGrantKey", {
      description: `${projectName} signed delegation grant key (async plan actor delegation)`,
      keySpec: kms.KeySpec.ECC_NIST_P256,
      keyUsage: kms.KeyUsage.SIGN_VERIFY,
      alias: `${projectName}-delegation-grant`,
      // The key identity is referenced by persisted signatures' verification;
      // retain it across stack replacement so in-flight grants stay verifiable.
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    const gatewayInterceptorFn = new lambda.Function(this, "GatewayInterceptorFn", {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: "index.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "lambdas", "gateway-interceptor")),
      timeout: cdk.Duration.seconds(10),
      // On the hot path of EVERY call through the user-MCP gateway (REQUEST and
      // RESPONSE both invoke it), and at the 128 MB default one invocation took
      // 2.92s of a 4.9s gateway connect in staging. Lambda allots CPU in
      // proportion to memory, and the work here is CPU-bound (boto3 import, TLS,
      // kms:Verify, JSON filtering), so 1024 MB (~8x the CPU share) shortens
      // both the cold start and the run.
      memorySize: 1024,
      description:
        "AgentCore Gateway interceptor: per-actor scoping of user-registered MCP tools",
      environment: {
        // Verify-only: the interceptor calls kms:Verify with this key ARN to
        // authenticate a delegation grant before deriving the actor from it.
        DELEGATION_KEY_ARN: delegationGrantKey.keyArn,
      },
      // GATEWAY_REGISTRY_TABLE and the read grant are attached further down,
      // once the table exists: this function has to be constructed before the
      // gateway that references its ARN, and the table is created after both.
    });

    // Verify-only grant, scoped to the single grant key. The interceptor never
    // signs and holds no key material.
    delegationGrantKey.grant(gatewayInterceptorFn, "kms:Verify");

    // The gateway role is hoisted into a variable (rather than constructed
    // inline as `new iam.Role(...).roleArn`) so the interceptor can grant it
    // invoke permission. The L2 `LambdaInterceptor` construct would do this
    // automatically; with the L1 `CfnGateway` it is ours to do, and without
    // it every gateway call fails when the interceptor cannot be invoked.
    const gatewayRole = new iam.Role(this, "GatewayRole", {
      assumedBy: new iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
      inlinePolicies: {
        "gateway-identity": new iam.PolicyDocument({
          statements: [
            new iam.PolicyStatement({
              actions: [
                "bedrock-agentcore:GetWorkloadAccessToken",
              ],
              resources: [
                `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default`,
                `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default/workload-identity/*`,
              ],
            }),
            new iam.PolicyStatement({
              actions: [
                "bedrock-agentcore:GetResourceOauth2Token",
              ],
              resources: [
                `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default`,
                `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default/workload-identity/*`,
                `arn:aws:bedrock-agentcore:${this.region}:${this.account}:token-vault/default`,
                `arn:aws:bedrock-agentcore:${this.region}:${this.account}:token-vault/default/oauth2credentialprovider/*`,
              ],
            }),
            // The gateway fetches an API-key provider's key at invocation time.
            // Without this a target is created successfully and then fails on
            // every call -- surfacing only when a tool is used, not at
            // registration, which is a much harder failure to attribute.
            new iam.PolicyStatement({
              actions: ["bedrock-agentcore:GetResourceApiKey"],
              resources: [
                // Same four resource shapes GetResourceOauth2Token above lists.
                // Resolving a credential touches the workload identity AND the
                // token vault, and IAM is evaluated against BOTH: granting only
                // the vault gets past the first check and fails the second, with
                // an error identical apart from the ARN.
                `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default`,
                `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default/workload-identity/*`,
                `arn:aws:bedrock-agentcore:${this.region}:${this.account}:token-vault/default`,
                `arn:aws:bedrock-agentcore:${this.region}:${this.account}:token-vault/default/apikeycredentialprovider/*`,
              ],
            }),
            new iam.PolicyStatement({
              actions: ["secretsmanager:GetSecretValue"],
              resources: [
                `arn:aws:secretsmanager:${this.region}:${this.account}:secret:bedrock-agentcore*`,
              ],
            }),
          ],
        }),
      },
    });
    gatewayInterceptorFn.grantInvoke(gatewayRole);

    // ---- Platform Gateway (Wave 4: AWS_IAM trust plane) ----
    // Shared platform capabilities (AWS documentation search, managed Web
    // Search, approved promoted targets) belong here behind IAM inbound auth
    // plus (added separately) a default-deny AgentCore Policy Engine — NOT the
    // tenant ownership interceptor. Introduced additively: no targets are
    // attached and no runtime routes to it yet, so it is inert until targets
    // are moved (Wave 5). Separate least-privilege role from the Tenant gateway
    // (no interceptor invoke — this gateway has no interceptor).
    // ---- AgentCore Web Search (env-config `webSearch`; agent-harness-help-web-search R6-R8) ----
    // On: a `web-search` connector target on THIS gateway, `InvokeWebSearch` on its role, and a Cedar permit for
    // exactly the WebSearch action. Off: none of the three. Region-gated so an unsupported region fails synth
    // instead of shipping a target that never reaches READY.
    const webSearchCfg = props.envConfig?.webSearch?.enabled ? props.envConfig.webSearch : undefined;
    if (webSearchCfg && !cdk.Token.isUnresolved(this.region) && !WEB_SEARCH_SUPPORTED_REGIONS.includes(this.region)) {
      throw new Error(`webSearch.enabled is set but the AgentCore Web Search connector is not offered in ${this.region} ` +
        `(supported: ${WEB_SEARCH_SUPPORTED_REGIONS.join(", ")}). Turn webSearch off for this env.`);
    }
    const webSearchTargetName = `${projectName}-web-search`;
    /** The tool's gateway name: `<targetName>___WebSearch` (verified via tools/list, 2026-10-20). */
    const webSearchToolName = `${webSearchTargetName}___WebSearch`;
    const platformGatewayRole = new iam.Role(this, "PlatformGatewayRole", {
      assumedBy: new iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
      inlinePolicies: {
        "platform-gateway-identity": new iam.PolicyDocument({
          statements: [
            new iam.PolicyStatement({
              actions: ["bedrock-agentcore:GetWorkloadAccessToken"],
              resources: [
                `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default`,
                `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default/workload-identity/*`,
              ],
            }),
            new iam.PolicyStatement({
              actions: [
                "bedrock-agentcore:GetResourceOauth2Token",
                "bedrock-agentcore:GetResourceApiKey",
              ],
              resources: [
                `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default`,
                `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default/workload-identity/*`,
                `arn:aws:bedrock-agentcore:${this.region}:${this.account}:token-vault/default`,
                `arn:aws:bedrock-agentcore:${this.region}:${this.account}:token-vault/default/oauth2credentialprovider/*`,
                `arn:aws:bedrock-agentcore:${this.region}:${this.account}:token-vault/default/apikeycredentialprovider/*`,
              ],
            }),
            new iam.PolicyStatement({
              actions: ["secretsmanager:GetSecretValue"],
              resources: [
                `arn:aws:secretsmanager:${this.region}:${this.account}:secret:bedrock-agentcore*`,
              ],
            }),
            // Cedar policy engine (Wave 4): the EXACT permissions per
            // https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-permissions.html
            // — GetPolicyEngine on the engine; AuthorizeAction + PartiallyAuthorizeActions on
            // BOTH the engine and the gateway. Required to associate (validated up front by
            // GenesisPolicyEngineCheck) and to evaluate Cedar decisions at request time.
            new iam.PolicyStatement({
              actions: ["bedrock-agentcore:GetPolicyEngine"],
              resources: [
                `arn:aws:bedrock-agentcore:${this.region}:${this.account}:policy-engine/*`,
              ],
            }),
            new iam.PolicyStatement({
              actions: [
                "bedrock-agentcore:AuthorizeAction",
                "bedrock-agentcore:PartiallyAuthorizeActions",
              ],
              resources: [
                `arn:aws:bedrock-agentcore:${this.region}:${this.account}:policy-engine/*`,
                `arn:aws:bedrock-agentcore:${this.region}:${this.account}:gateway/*`,
              ],
            }),
            // The connector checks this per request against the SERVICE-owned tool ARN (account `aws`).
            ...(webSearchCfg
              ? [new iam.PolicyStatement({
                  actions: ["bedrock-agentcore:InvokeWebSearch"],
                  resources: [`arn:aws:bedrock-agentcore:${this.region}:aws:tool/web-search.v1`],
                })]
              : []),
          ],
        }),
      },
    });

    const platformGateway = new cdk.aws_bedrockagentcore.CfnGateway(this, "PlatformGateway", {
      name: `${projectName}-platform-gateway`,
      roleArn: platformGatewayRole.roleArn,
      protocolType: "MCP",
      protocolConfiguration: {
        mcp: {
          searchType: "SEMANTIC",
          supportedVersions: ["2025-03-26", "2026-07-28"],
        },
      },
      // AWS_IAM inbound: the calling runtime role is the identity. No
      // CUSTOM_JWT authorizer and NO ownership interceptor — shared platform
      // tools are governed by IAM plus (added separately) an AgentCore Policy
      // Engine, never per-user ownership. Authorizer configuration is omitted
      // (optional for AWS_IAM).
      authorizerType: "AWS_IAM",
    });

    new cdk.CfnOutput(this, "PlatformGatewayArn", { value: platformGateway.attrGatewayArn });
    new cdk.CfnOutput(this, "PlatformGatewayUrl", { value: platformGateway.attrGatewayUrl });
    new cdk.CfnOutput(this, "PlatformGatewayId", { value: platformGateway.attrGatewayIdentifier });

    // ---- Cedar policy engine for the shared Platform gateway (Wave 4 / Option B) ----
    // Governs the moved AWS-doc tools: permit the read-only doc/reference tools, default-deny
    // everything else (Cedar default-deny). Native L1 for engine + policy; the association is an
    // L1 property override because the pinned CfnGateway (2.244) predates the typed
    // policyEngineConfiguration prop — the CFN resource supports it; prop names Arn/Mode verified
    // from the CfnGateway.GatewayPolicyEngineConfigurationProperty docs. Action/resource shape +
    // the specific-gateway-ARN requirement validated live via create-policy FAIL_ON_ANY_FINDINGS.
    const platformPolicyEngine = new cdk.aws_bedrockagentcore.CfnPolicyEngine(this, "PlatformPolicyEngine", {
      name: `${projectName.replace(/-/g, "_")}_platform_policy`,
      description:
        "Cedar governance for the shared Platform gateway: permit the read-only AWS-doc tools; default-deny everything else.",
    });
    const platformCedarStatement = [
      "permit(",
      "  principal,",
      "  action in [",
      `    AgentCore::Action::"${projectName}-aws-mcp___aws___search_documentation",`,
      `    AgentCore::Action::"${projectName}-aws-mcp___aws___read_documentation",`,
      `    AgentCore::Action::"${projectName}-aws-mcp___aws___list_regions",`,
      `    AgentCore::Action::"${projectName}-aws-mcp___aws___get_regional_availability",`,
      `    AgentCore::Action::"${projectName}-aws-mcp___aws___retrieve_skill",`,
      `    AgentCore::Action::"${projectName}-aws-mcp___aws___get_tasks"`,
      "  ],",
      `  resource == AgentCore::Gateway::"${platformGateway.attrGatewayArn}"`,
      ");",
    ].join("\n");
    const platformPolicy = new cdk.aws_bedrockagentcore.CfnPolicy(this, "PlatformPolicyPermitDocTools", {
      name: `${projectName.replace(/-/g, "_")}_permit_aws_doc_tools`,
      policyEngineId: platformPolicyEngine.attrPolicyEngineId,
      definition: { cedar: { statement: platformCedarStatement } },
      // IGNORE_ALL_FINDINGS (not FAIL_ON_ANY_FINDINGS): Cedar validates a policy's actions against
      // the gateway's action SCHEMA, which is derived from its targets' tools — and the aws-mcp
      // target's tools sync ASYNCHRONOUSLY, lagging the target's CFN completion. On a FRESH deploy
      // (prod) the `<target>___aws___*` actions are not in the schema yet, so FAIL_ON_ANY_FINDINGS
      // rejected the create ("unrecognized action … unable to find an applicable action given the
      // policy scope") and rolled the whole stack back. Staging only passed because the target had
      // been synced by earlier incremental deploys. The action shapes are already live-validated on
      // staging, so we skip create-time validation and let the permits resolve once the target syncs
      // (a brief default-deny window under ENFORCE until then, which self-heals).
      validationMode: "IGNORE_ALL_FINDINGS",
    });
    platformPolicy.addDependency(platformPolicyEngine);
    // Associate engine -> gateway in ENFORCE. NOTE: the policy references the gateway ARN, so the
    // policy depends on the gateway; we deliberately do NOT add gateway.addDependency(policy) (that
    // would be a CFN cycle). The gateway role already carries the required Cedar permissions
    // (GetPolicyEngine + AuthorizeAction + PartiallyAuthorizeActions, per policy-permissions.html).
    platformGateway.addPropertyOverride("PolicyEngineConfiguration", {
      Arn: platformPolicyEngine.attrPolicyEngineArn,
      Mode: "ENFORCE",
    });

    // ---- Gateway (L1 for full control) ----
    const gateway = new cdk.aws_bedrockagentcore.CfnGateway(this, "Gateway", {
      name: `${projectName}-gateway`,
      roleArn: gatewayRole.roleArn,
      protocolType: "MCP",
      protocolConfiguration: {
        mcp: {
          searchType: "SEMANTIC",
          // Dual protocols during migration (Wave 4): legacy callers keep
          // 2025-03-26; reviewed MCP 2.x clients may negotiate 2026-07-28.
          supportedVersions: ["2025-03-26", "2026-07-28"],
        },
      },
      authorizerType: "CUSTOM_JWT",
      // Inbound auth is the END USER's pool, not the M2M pool.
      //
      // This gateway hosts user-registered MCP servers, so the interceptor has
      // to know which user is calling. A shared M2M identity cannot express
      // that -- the actor would have to be asserted in a header, and anything
      // holding the M2M secret could then claim to be anyone. With the user
      // pool as the authorizer the gateway validates the caller's token and
      // the interceptor reads a verified `sub` claim instead.
      //
      // A CUSTOM_JWT authorizer takes exactly one discoveryUrl, and M2MPool
      // and UserPool are separate pools, so this gateway cannot also accept
      // M2M tokens. That is fine: the media gateway (separate stack) keeps M2M
      // inbound, so the polling worker and plan chainer are unaffected. The
      // consequence to accept is that user-registered tools are reachable only
      // on an interactive turn -- an EventBridge-triggered poll has no user
      // token and never will.
      authorizerConfiguration: {
        customJwtAuthorizer: {
          // Two clients from THIS pool: the SPA (interactive, user tokens) and
          // the async-plan machine client (Wave 1 / option E). The machine
          // token has no user `sub`; that is fine — for grant-bearing async
          // traffic the interceptor derives the actor from the verified
          // delegation grant, not from the token.
          allowedClients: [
            userPoolClient.userPoolClientId,
            asyncPlanClient.userPoolClientId,
          ],
          discoveryUrl: userDiscoveryUrl,
        },
      },
      interceptorConfigurations: [
        {
          // Both phases, one function.
          interceptionPoints: ["REQUEST", "RESPONSE"],
          interceptor: {
            lambda: { arn: gatewayInterceptorFn.functionArn },
          },
          inputConfiguration: {
            // Mandatory for this design: without it the `headers` field is
            // omitted from the interceptor payload entirely, the actor
            // header never arrives, and the interceptor — which fails
            // closed — hides every user tool from everyone.
            passRequestHeaders: true,
          },
        },
      ],
    });

    // Same ordering argument for the interceptor's invoke grant, which lands in
    // the role's default policy. If the gateway went live before that existed it
    // could not invoke its interceptor, and every gateway call would fail for
    // the duration of the window. Guarded because the child id is a CDK
    // implementation detail rather than a contract.
    const gatewayRoleDefaultPolicy = gatewayRole.node.tryFindChild("DefaultPolicy");
    if (gatewayRoleDefaultPolicy instanceof iam.Policy) {
      gateway.addDependency(
        gatewayRoleDefaultPolicy.node.defaultChild as cdk.CfnResource,
      );
    }

    // ---- #2: Gateway MCP Server Target (custom resource with retry) ----
    const runtimeEndpoint = cdk.Fn.join("", [
      "https://bedrock-agentcore.",
      this.region,
      ".amazonaws.com/runtimes/",
      "arn%3Aaws%3Abedrock-agentcore%3A",
      this.region,
      "%3A",
      this.account,
      "%3Aruntime%2F",
      mcpRuntime.agentRuntimeId,
      "/invocations",
    ]);

    const targetFn = new lambda.Function(this, "GatewayTargetFn", {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: "index.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "gateway-target-handler")),
      timeout: cdk.Duration.minutes(10),
    });

    // Creating an MCP target with OAuth outbound auth makes this handler resolve
    // the credential provider's secret, so it needs to READ it. Without this the
    // target is created and then goes FAILED with:
    //
    //   Please check the OAuth setup. Access denied when retrieving secret
    //   'arn:...:secret:bedrock-agentcore-identity!default/oauth2/...'
    //   User: ...GatewayTargetFnServiceRole... is not authorized to perform:
    //   secretsmanager:GetSecretValue
    //
    // which is what happened to mediaworks-harness-aws-mcp: created and failed one
    // second apart, three weeks ago, and never retried. CredProviderFn can Create,
    // Put and Delete these secrets but not Get, and the read is needed by a
    // DIFFERENT function than the one that writes them -- so neither role having
    // it looked deliberate from either side.
    //
    // Scoped to the same oauth2 path CredProviderFn writes to, not to
    // `bedrock-agentcore*` generally.
    targetFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["secretsmanager:GetSecretValue"],
        resources: [
          `arn:aws:secretsmanager:${this.region}:${this.account}:secret:bedrock-agentcore-identity!default/oauth2/*`,
        ],
      })
    );
    targetFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "bedrock-agentcore:CreateGatewayTarget",
          "bedrock-agentcore:DeleteGatewayTarget",
          "bedrock-agentcore:GetGatewayTarget",
          "bedrock-agentcore:SynchronizeGatewayTargets",
          "bedrock-agentcore:GetAgentRuntime",
          // Target sync mints a workload-identity token from the gateway's
          // own workload-identity directory entry so it can authenticate
          // its `tools/list` call to the upstream MCP runtime. Without
          // this action the target lands in `Failed` status with a 403:
          // "no identity-based policy allows the
          //   bedrock-agentcore:GetWorkloadAccessToken action".
          "bedrock-agentcore:GetWorkloadAccessToken",
          // Target sync also needs to fetch an OAuth2 token from the
          // credential provider to authenticate the tools/list call.
          "bedrock-agentcore:GetResourceOauth2Token",
        ],
        resources: ["*"],
      })
    );

    const targetProvider = new cr.Provider(this, "GatewayTargetProvider", {
      onEventHandler: targetFn,
    });

    const gatewayTargetCR = new cdk.CustomResource(this, "AwsMcpTarget", {
      serviceToken: targetProvider.serviceToken,
      properties: {
        // Wave 4 / Option B: the AWS-doc (aws-mcp) shared tool lives on the PLATFORM gateway
        // (AWS_IAM, plane=platform), governed by IAM + a Cedar policy engine — not the tenant
        // ownership interceptor. VERIFIED via runtime logs (mediaworks_harness_main_agent) that the
        // main agent connects platform-gateway over SigV4 with all 8 aws-mcp tools; the task agent
        // now connects it too (load_platform_gateway_from_env wired into both, a0b0d417 + 4785f060).
        // The earlier revert was premature (test predated the task-agent fix propagating).
        GatewayId: platformGateway.attrGatewayIdentifier,
        RuntimeId: mcpRuntime.agentRuntimeId,
        TargetName: `${projectName}-aws-mcp`,
        Endpoint: runtimeEndpoint,
        CredProviderArn: credProviderArn,
        Scopes: JSON.stringify([m2mScope]),
        // Bumped 4->5 to force the tenant->platform move through the handler's identity-change
        // (delete-from-OLD-gateway + create-on-new) path.
        Revision: "5",
      },
    });
    gatewayTargetCR.node.addDependency(mcpRuntime);
    gatewayTargetCR.node.addDependency(credProviderResource);
    gatewayTargetCR.node.addDependency(platformGateway);
    // Create the Cedar permit AFTER the aws-mcp target is registered, so the gateway's action
    // schema is as populated as possible before the policy lands (the tool sync is still async, so
    // IGNORE_ALL_FINDINGS above is what actually guarantees the create; this just tightens ordering
    // and shrinks the default-deny window). No cycle: policy -> target -> gateway -> engine, and
    // policy -> engine — the gateway references the ENGINE, never the policy.
    platformPolicy.addDependency(gatewayTargetCR.node.defaultChild as cdk.CfnResource);

    if (webSearchCfg) {
      // A native GatewayTarget: CloudFormation supports `Mcp.Connector`, but the pinned CDK L1 (2.244) predates
      // its typed prop, so the connector block is a property override. `ConnectorSource` has no version field:
      // the target runs the connector's default version (1.3.0 when verified, with request-level filters).
      const domainFilter: Record<string, string[]> = {};
      if (webSearchCfg.includeDomains?.length) domainFilter.include = webSearchCfg.includeDomains;
      if (webSearchCfg.excludeDomains?.length) domainFilter.exclude = webSearchCfg.excludeDomains;
      const webSearchTarget = new cdk.aws_bedrockagentcore.CfnGatewayTarget(this, "PlatformWebSearchTarget", {
        gatewayIdentifier: platformGateway.attrGatewayIdentifier,
        name: webSearchTargetName,
        description: "AgentCore managed Web Search (shared, Cedar-governed). Bound per turn by the chat's toggle.",
        targetConfiguration: { mcp: {} } as any,
        credentialProviderConfigurations: [{ credentialProviderType: "GATEWAY_IAM_ROLE" }],
      });
      webSearchTarget.addPropertyOverride("TargetConfiguration.Mcp.Connector", {
        Source: { ConnectorId: "web-search" },
        Configurations: [{
          Name: "WebSearch",
          ParameterValues: Object.keys(domainFilter).length ? { domainFilter } : {},
        }],
      });
      webSearchTarget.addDependency(platformGateway);
      // Exactly the WebSearch action on exactly this gateway; Cedar's default-deny stays for everything else.
      // IGNORE_ALL_FINDINGS for the same reason as the doc-tool permit: the action enters the gateway's schema
      // only once the target's tools sync, which lags the target's CFN completion on a fresh deploy.
      const webSearchPolicy = new cdk.aws_bedrockagentcore.CfnPolicy(this, "PlatformPolicyPermitWebSearch", {
        name: `${projectName.replace(/-/g, "_")}_permit_web_search`,
        policyEngineId: platformPolicyEngine.attrPolicyEngineId,
        definition: {
          cedar: {
            statement: [
              "permit(",
              "  principal,",
              `  action == AgentCore::Action::"${webSearchToolName}",`,
              `  resource == AgentCore::Gateway::"${platformGateway.attrGatewayArn}"`,
              ");",
            ].join("\n"),
          },
        },
        validationMode: "IGNORE_ALL_FINDINGS",
      });
      webSearchPolicy.addDependency(platformPolicyEngine);
      webSearchPolicy.addDependency(webSearchTarget);
    }

    // ---- CloudFront for Artifacts ----
    // Declared here (before the M2C MCP runtimes) so the runtimes can be
    // handed the CDN domain via env — their results tools copy workflow
    // outputs back to the harness artifacts bucket and return CDN URLs.
    // CORS for the public artifacts CDN. The renderer's NLE timeline uses
    // <audio/video crossOrigin="anonymous"> so quiet TTS/stem outputs can be
    // routed through a Web Audio GainNode (mediaGain.ts) for peak normalization.
    // Without an Access-Control-Allow-Origin header the browser BLOCKS those
    // cross-origin loads (ERR_FAILED), and even removing crossOrigin would only
    // trade the error for tainted (silent) Web Audio. Artifacts are already
    // public, so a static ACAO:* is safe AND cache-friendly (no Vary:Origin, so
    // one cached object serves every viewer). Simple GET media loads are not
    // preflighted; this header on the response is all the browser needs.
    const artifactsCorsPolicy = new cloudfront.ResponseHeadersPolicy(this, "ArtifactsCorsPolicy", {
      responseHeadersPolicyName: `${projectName}-artifacts-cors`,
      corsBehavior: {
        accessControlAllowOrigins: ["*"],
        accessControlAllowHeaders: ["*"],
        accessControlAllowMethods: ["GET", "HEAD", "OPTIONS"],
        accessControlAllowCredentials: false,
        accessControlExposeHeaders: ["Content-Length", "Content-Range", "Accept-Ranges"],
        originOverride: true,
      },
    });
    // media-harness-project-ingestion §18 (Req 11.6-11.8): Project media is NEVER public. A
    // viewer-request CloudFront Function refuses `/projects/*` with 403 on the ArtifactsCdn — on both a
    // dedicated `projects/*` behaviour (what Req 11.6 names and the synth test asserts) AND the default
    // behaviour, because CloudFront matches patterns on the request URI as sent while S3 decodes
    // percent-encoding, so `/%70rojects/…` would miss the pattern yet fetch `projects/…`. Deployed in
    // every environment (prod included): it costs nothing and keeps a stray `projects/` object private.
    //
    // `artifactsCdnPrivate` (staging first): the SAME function, on the same two behaviours, denies
    // EVERY request instead — artifact URLs become identifiers resolved through presigned S3 GETs
    // (proxy `POST /api/artifact-urls`, agent/Slack by key). A viewer-request function runs before
    // the cache, so nothing cached is served either. Flag off → code byte-identical to before.
    const artifactsCdnPrivate = props.envConfig?.artifactsCdnPrivate === true;
    // asset-lineage Phase 3 (a), design §9.1. `project` writes new session outputs under
    // projects/<pid>/sessions/<sid>/output/. Only coherent where artifact URLs are identifiers resolved
    // through the proxy: a `projects/` CDN URL is always 403, so refuse it without the private CDN.
    const sessionOutputLayout = props.envConfig?.sessionOutputLayout ?? "artifacts";
    if (sessionOutputLayout === "project" && !artifactsCdnPrivate) {
      throw new Error(
        "sessionOutputLayout=project requires artifactsCdnPrivate: projects/<pid>/sessions/ outputs are "
        + "resolved through the proxy, never served by the artifacts CDN.");
    }
    // asset-lineage Phase 3 (b), design §9.2: per-(project, session) sandbox credentials.
    // ON by default (B2): the sandbox's own role holds no artifact/project S3 access, so without the
    // scoped role a cell could reach no session output at all. `false` is honoured only as an explicit
    // opt-out (a sandbox with no S3), never the default.
    const sandboxScopedCredentials = props.envConfig?.sandboxScopedCredentials !== false;
    const sandboxScopedRoleName = `${projectName}-sandbox-scoped-s3`;
    const sandboxScopedRoleArn = `arn:aws:iam::${this.account}:role/${sandboxScopedRoleName}`;
    const projectsDenyFn = new cloudfront.Function(this, "ProjectsDeny", {
      comment: artifactsCdnPrivate
        ? `${projectName} deny ALL on the artifacts CDN (artifactsCdnPrivate; projects/* included)`
        : `${projectName} refuse /projects/* on the artifacts CDN (project media is never public)`,
      code: cloudfront.FunctionCode.fromInline(
        (artifactsCdnPrivate
          ? [
              // Private: deny EVERYTHING — no public path at all. Consumers get presigned S3 URLs, and
              // the code-interpreter sandbox reads its ffmpeg (`tools/ffmpeg/`) with its own IAM role.
              "function handler(event) {",
              "  return deny();",
              "}",
            ]
          : [
              "function handler(event) {",
              "  var uri = event.request.uri, path;",
              "  try { path = decodeURIComponent(uri); } catch (e) { return deny(); }",
              "  path = path.replace(/\\/{2,}/g, '/');",
              "  if (/^\\/(\\.\\/)*projects(\\/|$)/.test(path)) return deny();",
              "  return event.request;",
              "}",
            ]
        ).concat([
          "function deny() { return { statusCode: 403, statusDescription: 'Forbidden' }; }",
        ]).join("\n"),
      ),
    });
    const artifactsOrigin = origins.S3BucketOrigin.withOriginAccessControl(skillsBucket);
    const artifactsDistribution = new cloudfront.Distribution(this, "ArtifactsCdn", {
      defaultBehavior: {
        origin: artifactsOrigin,
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
        responseHeadersPolicy: artifactsCorsPolicy,
        functionAssociations: [
          { function: projectsDenyFn, eventType: cloudfront.FunctionEventType.VIEWER_REQUEST },
        ],
      },
      additionalBehaviors: {
        // The named `projects/*` refusal behaviour (Req 11.6): same origin, caching disabled, deny fn.
        "projects/*": {
          origin: artifactsOrigin,
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
          cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
          responseHeadersPolicy: artifactsCorsPolicy,
          functionAssociations: [
            { function: projectsDenyFn, eventType: cloudfront.FunctionEventType.VIEWER_REQUEST },
          ],
        },
      },
      comment: `${projectName} artifacts CDN`,
    });

    // ---- M2C media tools (federated via the media-workflow-gateway) ----
    //
    // The harness's custom diarization + audiostem MCP runtimes (which wrapped
    // the M2C Step Functions state machines directly) were REMOVED in favor of
    // federating to the self-contained M2C `media-workflow-gateway` stood up by
    // the externally published M2C bundle (its agent-tools / gateway stack, per
    // the bundle contract). That gateway exposes the tools
    // process_media / diarize_speech / separate_stems / get_job_status behind
    // its own Cognito M2M auth, and the harness consumes them by registering it
    // in the gateway registry (no per-runtime wiring here). See
    // infra/gitlab/README.md for the deploy/federation runbook.

    // ---- Main Agent Runtime ----
    // Use zip-packager Lambda to build deployment package with ARM64 deps
    const agentCodeBucket = new s3.Bucket(this, "AgentCodeBucket", {
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
    });

    const packagerLambda = new lambda.Function(this, "ZipPackagerLambda", {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: "index.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "zip-packager")),
      timeout: cdk.Duration.minutes(10),
      memorySize: 1024,
      ephemeralStorageSize: cdk.Size.gibibytes(2),
    });
    agentCodeBucket.grantReadWrite(packagerLambda);

    // Read agent source files and encode as base64.
    //
    // Layout: top-level `agent/src/*.py` plus per-package
    // subdirectories (each with an `__init__.py`). The block below
    // walks the top-level files non-recursively, then auto-discovers
    // every package directory and bundles it recursively. Adding a
    // new package under `agent/src/` therefore requires no CDK
    // changes — the package is picked up on the next deploy as long
    // as it has an `__init__.py`.
    //
    // Replaces the previous per-package walkers (one for `lab/`,
    // one for `jobs/`) which were copy-paste duplicates and
    // routinely forgotten when adding new packages — the symptom
    // being a `ModuleNotFoundError` raised by the deployed runtime
    // 60–120 seconds after `cdk deploy` reported success. See
    // `docs/backlog.md` (2026-06-25 entry "generalize agent source
    // bundling") for the rationale.
    const fs = require("fs");
    const agentSrcDir = path.join(__dirname, "../../agent/src");
    const agentCode: Record<string, string> = {};

    // Directory-name predicate: skip dev/build artifacts that should
    // never ship to the runtime. The conditions cover:
    //   - `__pycache__`               — Python bytecode cache
    //   - any name starting with `.`  — `.venv`, `.pytest_cache`,
    //                                   `.hypothesis`, `.mypy_cache`,
    //                                   `.ruff_cache`, `.tox`, etc.
    //   - any name ending in `.egg-info` — pip editable-install
    //                                       metadata (e.g.
    //                                       `agent.egg-info`)
    const shouldSkipDir = (name: string): boolean =>
      name === "__pycache__"
      || name.startsWith(".")
      || name.endsWith(".egg-info");

    // Top-level files only — no recursion. Mirrors the previous
    // behaviour: `agent/src/*.py` and `requirements.txt` are flat
    // peers under the runtime, not nested in a package directory.
    for (const file of fs.readdirSync(agentSrcDir)) {
      if (file.endsWith(".py") || file === "requirements.txt") {
        const content = fs.readFileSync(path.join(agentSrcDir, file));
        agentCode[file] = content.toString("base64");
      }
    }

    // Recursive walker shared by every package directory. Pulls
    // `.py` (source), `.txt` (e.g. per-package requirements) and
    // `.json` (data files a module opens beside itself — e.g.
    // `project_search/withheld-tool-prefixes.json`, which the runtime
    // read and, absent from the zip, silently replaced with its
    // built-in defaults: the prompt-registry withhold never shipped).
    // Built-in skill resources live under `agent/skills/`.
    const bundleAgentPackage = (dir: string, prefix: string): void => {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        if (entry.isDirectory()) {
          if (shouldSkipDir(entry.name)) continue;
          bundleAgentPackage(
            path.join(dir, entry.name),
            path.join(prefix, entry.name),
          );
        } else if (
          entry.isFile()
          && (entry.name.endsWith(".py") || entry.name.endsWith(".txt") || entry.name.endsWith(".json"))
        ) {
          agentCode[path.join(prefix, entry.name)] =
            fs.readFileSync(path.join(dir, entry.name)).toString("base64");
        }
      }
    };

    // Auto-discover every Python package under `agent/src/` and
    // bundle it. Gating on `__init__.py` keeps the loop tight: a
    // stray utility directory without one (e.g. a future
    // `__experiments/` scratchpad) won't get shipped to the
    // runtime, but every legitimate package will.
    // Packages that are NOT runtime code: `ingestion/` ships only in the ingestion Lambda package (its own
    // zip below) and no runtime module imports it. Bundling it into the runtime zip made every ingestion-only
    // commit change the runtime content hash, so all five AgentCore runtimes updated (~4 min) and the ECS
    // services waited on them, for code no runtime runs.
    const lambdaOnlyPackages = new Set(["ingestion"]);
    for (const entry of fs.readdirSync(agentSrcDir, { withFileTypes: true })) {
      if (!entry.isDirectory()) continue;
      if (shouldSkipDir(entry.name)) continue;
      if (lambdaOnlyPackages.has(entry.name)) continue;
      const initPy = path.join(agentSrcDir, entry.name, "__init__.py");
      if (!fs.existsSync(initPy)) continue;
      bundleAgentPackage(
        path.join(agentSrcDir, entry.name),
        entry.name,
      );
    }

    // Read built-in skills
    const skillsDir = path.join(__dirname, "../../agent/skills");
    if (fs.existsSync(skillsDir)) {
      const readDirRecursive = (dir: string, prefix: string) => {
        for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
          const fullPath = path.join(dir, entry.name);
          const relPath = path.join(prefix, entry.name);
          if (entry.isDirectory() && entry.name !== "__pycache__") {
            readDirRecursive(fullPath, relPath);
          } else if (entry.isFile()) {
            agentCode[relPath] = fs.readFileSync(fullPath).toString("base64");
          }
        }
      };
      readDirRecursive(skillsDir, "skills");
    }

    // Read requirements
    const requirements = fs.readFileSync(path.join(agentSrcDir, "requirements.txt"), "utf-8")
      .split("\n")
      .map((l: string) => l.trim())
      .filter((l: string) => l && !l.startsWith("#"));

    const crypto = require("crypto");
    const contentHash = crypto.createHash("sha256")
      .update(JSON.stringify({ requirements, agentCode, buildVersion: "v64-default-add-http-request" }))
      .digest("hex").slice(0, 16);

    // Upload agent code to S3 as a JSON file (avoids Lambda 1MB event payload limit)
    const agentCodeKey = `agent_code_${contentHash}.json`;
    const agentCodeAsset = new s3deploy.BucketDeployment(this, "AgentCodeAsset", {
      sources: [s3deploy.Source.jsonData(agentCodeKey, agentCode)],
      destinationBucket: agentCodeBucket,
      destinationKeyPrefix: "",
      prune: false,
    });

    const packagerProvider = new cr.Provider(this, "ZipPackagerProvider", {
      onEventHandler: packagerLambda,
    });

    const zipObjectKey = `deployment_package_${contentHash}.zip`;

    const zipPackager = new cdk.CustomResource(this, "ZipPackagerV2", {
      serviceToken: packagerProvider.serviceToken,
      properties: {
        BucketName: agentCodeBucket.bucketName,
        ObjectKey: zipObjectKey,
        Requirements: requirements,
        AgentCodeBucket: agentCodeBucket.bucketName,
        AgentCodeKey: agentCodeKey,
        ContentHash: contentHash,
      },
    });
    zipPackager.node.addDependency(agentCodeAsset);

    // ---- Code Interpreter ----
    const codeInterpreter = new agentcore.CodeInterpreterCustom(this, "CodeInterpreter", {
      codeInterpreterCustomName: `${projectName.replace(/-/g, "_")}_code_interpreter`,
      description: "Sandboxed Python execution with S3 artifact access",
      networkConfiguration: agentcore.CodeInterpreterNetworkConfiguration.usingPublicNetwork(),
    });

    // The code interpreter's OWN execution role: the harness-staged ffmpeg build and nothing else (B2).
    //
    // This role is held by the sandbox microVM that runs MODEL-AUTHORED Python, and it is shared by every
    // actor, project and session. It used to read `artifacts/*` + `projects/*` and write `artifacts/*`
    // (+ `projects/*/sessions/*/output/*`) for EVERYONE, with the per-session scoped credentials
    // (`SandboxScopedS3Role`, below) installed only as a `~/.aws/credentials` file — which model code can
    // delete or ignore and fall straight back to this role. So the role now holds no artifact or project
    // access at all: every S3 read/write a cell makes goes through the scoped role's
    // `${aws:PrincipalTag/...}` prefixes, and the runtimes FAIL CLOSED when they cannot install those
    // credentials (`sandbox_credentials.install`, `NormalizedCodeInterpreterTool._inject_credentials`,
    // `crop_video._inject_scoped_credentials`, `media_actions._inject_credentials`) instead of running
    // the cell on this role. `tools/ffmpeg/*` stays: it is public tooling, not user data, and the
    // staged-ffmpeg download can precede the injection on a fresh sandbox.
    // `test:sandbox-role-grants` pins this (no artifacts/projects resource on this role, in every env).
    skillsBucket.grantRead(codeInterpreter, "tools/ffmpeg/*");

    // ---- DynamoDB Plan State Table ----
    const planStateTable = new dynamodb.Table(this, "PlanStateTable", {
      tableName: `${projectName}-plan-state`,
      partitionKey: { name: "session_id", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "node_id", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      timeToLiveAttribute: "ttl",
      // Stream enables the Plan Chainer Lambda to react to node state changes
      stream: dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
    });

    // ---- Session Metadata Table ----
    const sessionsTable = new dynamodb.Table(this, "SessionsTable", {
      tableName: `${projectName}-sessions`,
      partitionKey: { name: "sessionId", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      timeToLiveAttribute: "ttl",
    });

    sessionsTable.addGlobalSecondaryIndex({
      indexName: "active-lastActivity-index",
      partitionKey: { name: "actorId", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "lastActivityAt", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // ---- Gateway Registry Table ----
    //
    // Backs the gateway-registry-admin spec (R1): single-table store of
    // admin-managed MCP gateway descriptors that the Main Agent loads on
    // startup in place of the legacy `GATEWAYS` env var. RETAIN policy
    // because gateway definitions are operational config, not ephemeral
    // state — losing them on a `cdk destroy` would force a re-registration
    // by an admin. Read/write grants and the Secrets Manager policy for
    // gateway client secrets are wired in task 1.2.
    //
    // Schema (per design.md):
    //   PK  HASH    string   "GATEWAY#REGISTERED"
    //   SK  RANGE   string   <gateway_id>
    const gatewayRegistryTable = new dynamodb.Table(this, "GatewayRegistry", {
      tableName: `${projectName}-gateway-registry`,
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // ---- Built-in skill catalog (deploy-time projection) ----
    //
    // The Skills tab's built-in panel reads these rows, so it never wakes the agent
    // runtime to ask what ships. They are written HERE, by the deploy, rather than by the
    // runtime at startup — which matters: an AgentCore runtime starts on demand, so rows
    // it publishes "at startup" are absent on a fresh environment and describe the
    // PREVIOUS version until someone happens to invoke it. Writing them during
    // `cdk deploy` is deterministic, refreshes on every deploy, and fails loudly (a
    // failed CloudFormation resource) instead of leaving a silently short list.
    //
    // Read by `capability.sources.read_builtins("skill")` for the list and
    // `read_builtin_skill()` for one package. Sits under the same `GATEWAY#CATALOG` PK as
    // the builtin TOOL projection the runtime publishes (`main.py:_CATALOG_PK`, read by
    // `ui/server/toolCatalog.ts`). Skills are not gateway-derived — the shared PK is just
    // "builtin catalog projections", and one convention beats two.
    //
    // See `builtin-skills.ts` for why the frontmatter parsing is not just "line 2".
    const builtinSkills = readBuiltinSkills(
      path.join(__dirname, "../../agent/skills"),
    );
    assertSkillItemsFit(builtinSkills);

    // ONE ITEM PER SKILL (`SK=__skill__<slug>`), not one row holding all of them.
    //
    // The item carries the whole SKILL.md so the detail view can render without invoking
    // the agent runtime, and bodies are what make size a real constraint: the seven total
    // ~67 KB today, against a hard 400 KB per item. Per-skill items give each its own
    // budget, let a View `GetItem` exactly the one it needs, and let the LIST Query
    // project just name+description instead of dragging every body across.
    //
    // THE BODIES TRAVEL AS AN S3 ASSET, NOT IN THE TEMPLATE. Seven `AwsCustomResource`
    // putItems with the bodies inline was the obvious shape and it fails the deploy:
    //
    //   ValidationError: Template may not exceed 1000000 bytes in size.
    //
    // The template was already ~895 KB without them (685 KB was the Cognito
    // `ManagedLoginBranding` resource, which inlined a 342 KB base64 login logo once per
    // colour mode — since shrunk to a 240 px copy, ~36 KB for both), so 163 KB of skill bodies went over. An asset also decouples catalog
    // growth from a 1 MB budget shared with every other resource in the stack.
    //
    // The asset's S3 key is its CONTENT HASH, so a `SKILL.md` edit changes a property and
    // CloudFormation re-runs the handler. Without that it would see no diff, never invoke
    // it, and the rows would silently keep the old text — the failure this whole
    // projection exists to fix.
    const catalogAsset = new s3assets.Asset(this, "BuiltinSkillCatalogFile", {
      path: stageBuiltinSkillCatalog(
        builtinSkills,
        path.join(__dirname, "../.build"),
      ),
    });

    const catalogWriter = new lambda.Function(this, "BuiltinSkillCatalogFn", {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: "index.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "builtin-skill-catalog")),
      timeout: cdk.Duration.minutes(5),
      description: "Writes the built-in skill catalog rows read by the Skills tab",
    });
    catalogAsset.grantRead(catalogWriter);
    // Query + Delete as well as writes: the handler reconciles, so a skill deleted from
    // the repo loses its row instead of being offered by the panel forever.
    gatewayRegistryTable.grantReadWriteData(catalogWriter);

    const catalogResource = new cdk.CustomResource(this, "BuiltinSkillCatalog", {
      serviceToken: new cr.Provider(this, "BuiltinSkillCatalogProvider", {
        onEventHandler: catalogWriter,
      }).serviceToken,
      properties: {
        TableName: gatewayRegistryTable.tableName,
        CatalogBucket: catalogAsset.s3BucketName,
        CatalogKey: catalogAsset.s3ObjectKey,
      },
    });
    catalogResource.node.addDependency(gatewayRegistryTable);

    // ---- Job Registry Table ----
    //
    // Backs the async-job-tracking spec (R5.1, R5.3, R11.1): durable
    // store of Async_Job records written by the agent runtime on
    // start-tool dispatch and updated by the polling worker. The table
    // sits next to `SessionsTable` and `GatewayRegistry` so the same
    // CDK + IAM grant patterns apply.
    //
    // Schema (per design.md "Job_Registry" section):
    //   PK  jobId          string   (full SFN execution ARN, or other tool-supplied id)
    //
    // GSI actor-status-index — sidebar listing + reconnect resync
    //   PK  actorId        string   (Cognito sub from JWT)
    //   SK  updatedAt      string   (ISO 8601 — sorts lexically newest-first)
    //   Projection: ALL
    //
    // GSI poll-partition-index — polling worker active-job scan
    //   PK  pollPartition  string   ("ACTIVE" while in flight; REMOVEd on terminal)
    //   SK  updatedAt      string
    //   Projection: ALL
    //
    // Streams:
    //   NEW_AND_OLD_IMAGES so the ChangefeedFanout Lambda can emit SSE
    //   `job.update` and `job.terminal` frames with both pre- and
    //   post-images available for diff logic.
    //
    // TTL:
    //   The literal attribute name `ttl` (epoch seconds) — set only on
    //   terminal transition by `JobRegistry.set_terminal`, mirroring the
    //   `state_store.py` convention. Active jobs never carry a TTL and
    //   are never expired.
    //
    // Removal policy:
    //   DESTROY mirrors `SessionsTable` and `PlanStateTable` — these are
    //   user-scoped operational records (not admin-managed registry
    //   data like `gatewayRegistryTable`) so wiping them on `cdk destroy`
    //   is acceptable in dev environments. Production deployments should
    //   override via a per-stack policy if required.
    const jobRegistryTable = new dynamodb.Table(this, "JobRegistry", {
      tableName: `${projectName}-job-registry`,
      partitionKey: { name: "jobId", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      timeToLiveAttribute: "ttl",
      stream: dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
    });

    const jobRegistryActorStatusIndexName = "actor-status-index";
    jobRegistryTable.addGlobalSecondaryIndex({
      indexName: jobRegistryActorStatusIndexName,
      partitionKey: { name: "actorId", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "updatedAt", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    const jobRegistryPollPartitionIndexName = "poll-partition-index";
    jobRegistryTable.addGlobalSecondaryIndex({
      indexName: jobRegistryPollPartitionIndexName,
      partitionKey: { name: "pollPartition", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "updatedAt", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // ---- Improve store (Step 2 transport for the Observe→Evaluate loop) ----
    //
    // Single table, typed sort keys, PK `actorId` / SK `sk`. Two item kinds:
    //
    //   actorId="__system__", sk="SNAPSHOT"  -> { bundle:{suggestions,components,patterns},
    //                                             createdAt, judgeConfigHash, codeVersion }
    //   actorId=<cognito sub>, sk="SUGG#<id>" -> { status:"dismissed", reason, note?, dismissedAt }
    //
    // The mined bundle is platform/harness-code — identical for every user — so it is stored
    // ONCE under the reserved `__system__` partition (real Cognito subs are UUIDs, so there is
    // no collision), seeded offline by `improve.seed_snapshot`. Only the per-suggestion dismiss
    // overlay is per-actor. `GET /improve/data` reads the global SNAPSHOT + the caller's SUGG#
    // rows and merges bundle→overlay (never the reverse), so a stale overlay row is harmless.
    //
    // RETAIN + PITR because these rows are human decisions (the OI-5 label substrate) — losing
    // them to a stack teardown is not acceptable. No stream and no GSI: the read is a GetItem +
    // a single-partition Query, neither of which needs one. Name has no env suffix, per AGENTS.md.
    // (Caveat accepted: a pinned name + RETAIN means a destroy/replace would name-collide.)
    const improveTable = new dynamodb.Table(this, "ImproveStore", {
      tableName: `${projectName}-improve`,
      partitionKey: { name: "actorId", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "sk", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      pointInTimeRecovery: true,
    });

    // ---- Polling_Worker Lambda (async-job-tracking task 8.1) ----
    //
    // Server-side poller for active Async_Jobs. Triggered by an
    // EventBridge `rate(<pollIntervalMinutes>m)` schedule (default 1
    // minute, the floor for EventBridge `Rule` schedules; the design
    // doc's R4.1 mentions 15s but that's not achievable with this
    // construct). Each invocation queries the `poll-partition-index`
    // GSI for jobs with `pollPartition = "ACTIVE"`, dispatches the
    // trio's `status_tool` (SFN direct path or MCP gateway path), and
    // reconciles the response into the JobRegistry — see
    // `infra/lib/lambdas/polling-worker/handler.py` for the full
    // dispatch/reconcile contract.
    //
    // Packaging: `lambda.Code.fromAsset(...)` directly against the
    // source directory. The handler and its helpers are stdlib + boto3
    // only (see `infra/lib/lambdas/polling-worker/requirements.txt`),
    // so no `pip install` step is required and we sidestep the
    // `zip-packager` custom-resource pattern used for the agent
    // runtime. If a future change adds a non-stdlib dep, switch to the
    // `zip-packager` path the way the main agent and MCP runtime do.
    //
    // Runtime: PYTHON_3_13 — matches every other Python Lambda in the
    // stack (`CredProviderFn`, `GatewayTargetFn`, `ZipPackagerLambda`,
    // `McpZipPackagerLambda`) so the deployment image cache is shared.
    //
    // Timeout: 60s. The cycle dispatches sequentially across all
    // active jobs (handler.py "Concurrency" docstring) — at the
    // design's small-N scale (0–10 active per actor, handful of
    // actors) this fits comfortably. If a single status tool stalls,
    // the next EventBridge tick fires regardless; we lose one cycle,
    // not the whole pipeline.
    //
    // Memory: 512 MiB. The worker holds one HTTP connection at a time
    // and a few small DDB items; CPU is the gating factor for boto3
    // signing and TLS, not memory. Bumped above the 128 MiB default
    // because Lambda gives proportional CPU shares with memory.
    //
    // IAM grants (DDB read/write on the JobRegistry table + GSIs,
    // DDB read on the gateway-registry table, Secrets Manager on the
    // gateway client-secret prefix, and `states:DescribeExecution`)
    // are wired in task 8.2 immediately below — this construct
    // intentionally creates the Lambda with no extra inline policies
    // so the grant code in 8.2 can attach against the function's
    // auto-created role.
    const pollIntervalMinutes = props.pollIntervalMinutes ?? 1;
    if (
      !Number.isInteger(pollIntervalMinutes) ||
      pollIntervalMinutes < 1
    ) {
      throw new Error(
        `pollIntervalMinutes must be a positive integer; got ${pollIntervalMinutes}`,
      );
    }

    // ---- Shared result-resolution layer ----
    //
    // Turning a finished node's RAW recorded output into something displayable is
    // deterministic, so it does not belong in a model turn. Both the polling worker
    // (at job terminal) and the plan chainer (at plan settle) need it, and both are
    // packaged with `lambda.Code.fromAsset(<dir>)` with no bundling step -- so a
    // shared file would have to be DUPLICATED into each bundle. Duplication is
    // exactly how the fal queue endpoint rule came to be fixed three separate times
    // in three separate places, so this ships as a layer with one copy.
    //
    // The module name (`result_resolution`) is also deliberately unique: both
    // bundles already define `handler` and `gateway_invoker`, and a sys.path insert
    // lets whichever imports first shadow the other.
    const resultResolutionLayer = new lambda.LayerVersion(this, "ResultResolutionLayer", {
      code: lambda.Code.fromAsset(
        path.join(__dirname, "lambdas/_layers/result-resolution"),
        // Without this, running the layer's tests locally leaves a __pycache__ in
        // the asset directory -- which ships, and worse makes the asset hash vary
        // with whether anyone happened to run tests, so a no-op deploy churns out
        // a new layer version.
        { exclude: ["**/__pycache__", "**/*.pyc"] },
      ),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_13],
      description:
        "Deterministic resolution of node/job outputs into one typed result artifact",
    });

    const pollingWorker = new lambda.Function(this, "JobPollingWorker", {
      functionName: `${projectName}-job-polling-worker`,
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: "handler.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "lambdas/polling-worker")),
      layers: [resultResolutionLayer],
      timeout: cdk.Duration.seconds(60),
      memorySize: 512,
      environment: {
        JOB_REGISTRY_TABLE: jobRegistryTable.tableName,
        JOB_REGISTRY_POLL_INDEX: jobRegistryPollPartitionIndexName,
        // Where a lone job's resolved result artifact is written, mirroring the
        // plan chainer's aggregate so both retrieval paths read one schema.
        SKILLS_S3_BUCKET: skillsBucket.bucketName,
        // CloudFront domain so a lone job's aggregate can persist M2C proxy artifacts
        // into our bucket and return durable urls (matches the plan chainer).
        ARTIFACTS_DOMAIN: artifactsDistribution.distributionDomainName,
        GATEWAY_REGISTRY_TABLE: gatewayRegistryTable.tableName,
        // Machine client for polling a user_jwt gateway. The token gets the
        // worker through the front door; WHOSE job is being polled comes from
        // the job's signed delegation grant, never from this token (it has no
        // `sub`), so this cannot be used to poll another actor's job.
        ASYNC_PLAN_TOKEN_ENDPOINT: asyncPlanTokenEndpoint,
        ASYNC_PLAN_CLIENT_ID: asyncPlanClient.userPoolClientId,
        ASYNC_PLAN_CLIENT_SECRET: asyncPlanClient.userPoolClientSecret.unsafeUnwrap(),
        ASYNC_PLAN_SCOPE: asyncPlanScope,
        MAX_JOB_AGE_SECONDS: "3600",
        METRIC_NAMESPACE: "MediaworksHarness/Jobs",
        // asset-lineage Phase 3 (a): a job's imported outputs land under its producing project.
        ...(sessionOutputLayout === "project" ? { SESSION_OUTPUT_LAYOUT: "project" } : {}),
      },
      description:
        "Polls active Async_Jobs from JobRegistry and dispatches status tools (async-job-tracking R4.1)",
    });

    // ---- Polling_Worker IAM grants (async-job-tracking task 8.2) ----
    //
    // R11.3: the polling worker reads the JobRegistry GSI for active
    // jobs, writes status updates back, looks up gateway descriptors
    // + OAuth client secrets to call MCP `status_tool`s, and calls
    // `stepfunctions:DescribeExecution` for SFN-direct trios.
    //
    // What we DO grant:
    //   * DDB read/write on the JobRegistry table — covers Query on
    //     the base table AND every GSI (`grantReadWriteData`
    //     auto-includes `${tableArn}/index/*`, so both
    //     `poll-partition-index` and `actor-status-index` are
    //     covered), plus the `UpdateItem` paths `set_active_status`,
    //     `bump_error`, and `set_terminal` in
    //     `lambdas/polling-worker/registry.py`.
    //   * DDB read on the gateway-registry table —
    //     `_resolve_gateway_descriptor` in
    //     `lambdas/polling-worker/gateway_invoker.py` does one
    //     `GetItem` per dispatched MCP job to resolve the gateway URL
    //     and `auth_config`.
    //   * `secretsmanager:GetSecretValue` on the gateway client-secret
    //     prefix — the OAuth client_credentials handshake reads the
    //     gateway's client secret via Secrets Manager. Wildcard
    //     region/account mirror the Main + Task agent runtime grants
    //     elsewhere in this stack (gateway secrets are addressed by
    //     path, not partition).
    //   * `states:DescribeExecution` on every execution ARN —
    //     SFN-direct trios (`gatewayId == "__aws_sfn__"`,
    //     `_describe_sfn_execution` in `handler.py`) call
    //     DescribeExecution directly. Scoped to
    //     `arn:aws:states:*:*:execution:*:*` because a
    //     Job_Detector-tagged SFN trio can resolve to any state
    //     machine in the account/region.
    //
    // What we deliberately DO NOT grant:
    //   * `bedrock-agentcore:InvokeAgentRuntime` — that action is for
    //     invoking AgentCore *Runtimes* (SigV4-signed). The polling
    //     worker's MCP path talks to AgentCore *Gateways*, which are
    //     plain HTTPS endpoints fronted by an OAuth2/JWT IdP. The
    //     dispatch code (`gateway_invoker.invoke_status_tool`) uses
    //     `urllib.request` + `Authorization: Bearer <token>`, not
    //     SigV4 against a runtime ARN, so no `bedrock-agentcore:*`
    //     permission is required for this path. Revisit if a future
    //     trio routes the polling cycle through a runtime ARN
    //     instead of a gateway URL.
    jobRegistryTable.grantReadWriteData(pollingWorker);
    gatewayRegistryTable.grantReadData(pollingWorker);
    // Reads the analysis JSONs it inlines (proxy bucket is granted below) and
    // writes the job's resolved result artifact. Without the write the aggregate
    // silently degrades to "no artifact" and lone-job retrieval keeps fanning out.
    skillsBucket.grantReadWrite(pollingWorker);
    // Proxy-bucket READ so make_s3_importer (wired into _publish_job_result) can copy
    // an M2C proxy artifact into the skills bucket for CloudFront delivery when building
    // a lone/direct job's aggregate. WITHOUT this the copy fails AccessDenied; the
    // importer is best-effort and swallows the error, so the aggregate silently keeps
    // s3:// proxy refs -> the agent re-presigns via get_download_url (1h expiry) AND the
    // render hook receives an undisplayable s3:// url (blank player). This mirrors the
    // Plan Chainer's MediaWorkflowOutputReadForInjection grant, which is exactly why the
    // chainer's aggregates already carry cloudfront.net urls and the polling worker's did
    // not. The comment two lines up ("granted below") promised this grant; it was missing.
    if (props.envConfig?.media?.proxyBucket) {
      pollingWorker.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "MediaWorkflowProxyReadForImport",
          actions: ["s3:GetObject", "s3:ListBucket"],
          resources: [
            `arn:aws:s3:::${props.envConfig.media.proxyBucket}`,
            `arn:aws:s3:::${props.envConfig.media.proxyBucket}/*`,
          ],
        }),
      );
    }
    pollingWorker.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["secretsmanager:GetSecretValue"],
        resources: [
          `arn:aws:secretsmanager:*:*:secret:/${projectName}/gateway/*`,
        ],
      }),
    );
    // The M2C media-workflow-gateway credentials live outside /${projectName}/gateway/* (they belong
    // to the M2C stack). The worker calls that gateway's shared get_job_status for an SFN-polled M2C
    // job whose execution output names no file (enrichment / conversation-analysis), because only
    // the status tool declares the job's outputs.
    if (props.envConfig?.media?.credentialsSecretArn) {
      pollingWorker.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "MediaGatewaySecretRead",
          actions: ["secretsmanager:GetSecretValue"],
          resources: [props.envConfig.media.credentialsSecretArn],
        }),
      );
    }
    pollingWorker.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["states:DescribeExecution"],
        resources: ["arn:aws:states:*:*:execution:*:*"],
      }),
    );
    // Resilience P0: the worker publishes ActiveJobs / JobsTimedOutBySweep /
    // Poll*Errors custom metrics for alarms. PutMetricData can't be resource-
    // scoped; constrain to the harness namespace via a condition.
    pollingWorker.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["cloudwatch:PutMetricData"],
        resources: ["*"],
        conditions: {
          StringEquals: { "cloudwatch:namespace": "MediaworksHarness/Jobs" },
        },
      }),
    );

    const pollingSchedule = new events.Rule(this, "JobPollingSchedule", {
      ruleName: `${projectName}-job-polling-schedule`,
      description: `Fires the Polling_Worker Lambda every ${pollIntervalMinutes}m (async-job-tracking R4.1; minute-granularity floor of EventBridge Rule schedules)`,
      schedule: events.Schedule.rate(cdk.Duration.minutes(pollIntervalMinutes)),
    });
    pollingSchedule.addTarget(new eventsTargets.LambdaFunction(pollingWorker));

    // ---- Event-driven completion for Step Functions jobs ----
    //
    // The schedule above is minute-granular, so an M2C execution was detected
    // ~60s after it ended (staging: 59.5s / 58.7s), and every downstream step
    // (plan chainer, render, the UI) waited that long. Step Functions publishes
    // `Execution Status Change` to this account's default bus for free (standard
    // workflows), so the SAME worker is invoked with that event and settles the
    // one job it names through the same `_poll_cycle` (handler.py
    // `handle_execution_event`) — no second copy of the completion logic. It
    // re-reads DescribeExecution rather than trusting the event, and the
    // registry's terminal-write guard makes event + poll idempotent in either
    // order. The schedule stays: fal and other gateway-polled jobs have no event,
    // nor does an M2C running in another account (sandbox), and a dropped event
    // must still settle.
    //
    // Scoped to this account+region's state machines, optionally narrowed by name
    // (`media.jobEventStateMachinePrefix`). Unscoped executions (ingestion, purge,
    // deploy waiters) cost one GetItem and are ignored.
    const jobEventStateMachinePrefix = props.envConfig?.media?.jobEventStateMachinePrefix ?? "";
    const sfnCompletionRule = new events.Rule(this, "JobSfnCompletionRule", {
      ruleName: `${projectName}-job-sfn-completion`,
      description:
        "Settles an async job as soon as its Step Functions execution ends (the 1-minute poll stays as fallback)",
      eventPattern: {
        source: ["aws.states"],
        detailType: ["Step Functions Execution Status Change"],
        detail: {
          status: ["SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"],
          stateMachineArn: [
            {
              prefix: `arn:aws:states:${this.region}:${this.account}:stateMachine:${jobEventStateMachinePrefix}`,
            },
          ],
        },
      },
    });
    sfnCompletionRule.addTarget(
      new eventsTargets.LambdaFunction(pollingWorker, {
        // A completion older than this is already settled by the schedule, and
        // the handler never raises for an event it cannot use, so EventBridge's
        // 24h / 185-retry defaults would only replay noise.
        maxEventAge: cdk.Duration.minutes(5),
        retryAttempts: 2,
      }),
    );

    new cdk.CfnOutput(this, "JobPollingWorkerFunctionName", {
      value: pollingWorker.functionName,
      description:
        "Lambda function name for the async-job-tracking polling worker",
    });

    new cdk.CfnOutput(this, "JobPollingWorkerFunctionArn", {
      value: pollingWorker.functionArn,
      description:
        "Lambda function ARN for the async-job-tracking polling worker",
    });

    new cdk.CfnOutput(this, "JobPollingScheduleRuleName", {
      value: pollingSchedule.ruleName,
      description:
        "EventBridge rule name driving the async-job-tracking polling cycle",
    });

    // ---- ChangefeedFanout: SQS FIFO queue + Lambda + DDB Stream ----
    //
    // Wires the push-channel half of async-job-tracking (R8.1, R8.2):
    //
    //   JobRegistry (DDB)
    //       │  Stream (NEW_AND_OLD_IMAGES)
    //       ▼
    //   JobChangefeedFanout Lambda  ── SendMessageBatch ──▶  JobFanoutQueue (FIFO)
    //                                                              │
    //                                                              ▼
    //                                                    agentcore-proxy /jobs/stream
    //                                                    long-poll ReceiveMessage,
    //                                                    filtered by actorId
    //                                                    (task 10.2 / R8.1, R8.2).
    //
    // Single-queue strategy:
    //   The design (Push_Channel section) calls for "per-actor" fan-out.
    //   The handler at `infra/lib/lambdas/changefeed-fanout/handler.ts`
    //   implements that as ONE FIFO queue with `MessageGroupId = actorId`
    //   rather than one queue per actor. This gives FIFO ordering per
    //   Actor while keeping the infrastructure footprint to a single
    //   queue. The proxy filters subscribers by JWT-validated actorId at
    //   receive time; cross-actor leakage is impossible.
    //
    // Queue config:
    //   - FIFO (`.fifo` suffix) for per-actor in-order delivery.
    //   - Content-based dedup is NOT enabled — the handler computes a
    //     deterministic `MessageDeduplicationId` from
    //     `sha256(jobId|updatedAt|event)`, which is more discriminating
    //     than an SHA over the entire body.
    //   - Visibility timeout 30s: matches the proxy's 20s long-poll wait
    //     (R8.1) plus a buffer for the proxy's per-message handling
    //     before it acks via `DeleteMessage`.
    //   - Retention 4 days: the proxy is long-lived and shouldn't lose
    //     messages, but bound the queue rather than letting it grow to
    //     SQS's 14-day max.
    const jobFanoutDlq = new sqs.Queue(this, "JobFanoutDLQ", {
      queueName: `${projectName}-job-fanout-dlq.fifo`,
      fifo: true,
      contentBasedDeduplication: true,
      retentionPeriod: cdk.Duration.days(14),
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const jobFanoutQueue = new sqs.Queue(this, "JobFanoutQueue", {
      // FIFO queue names MUST end in `.fifo` (SQS constraint).
      queueName: `${projectName}-job-fanout.fifo`,
      fifo: true,
      contentBasedDeduplication: false,
      visibilityTimeout: cdk.Duration.seconds(30),
      retentionPeriod: cdk.Duration.days(4),
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      // Poison messages the SSE consumer repeatedly fails to process land in the
      // DLQ after 5 receives instead of cycling until retention (resilience P0).
      deadLetterQueue: { queue: jobFanoutDlq, maxReceiveCount: 5 },
    });

    // ChangefeedFanout Lambda — TypeScript handler bundled by esbuild
    // via `NodejsFunction`. The handler at
    // `infra/lib/lambdas/changefeed-fanout/handler.ts` consumes
    // `INSERT`/`MODIFY` records from the JobRegistry stream and emits
    // one `JobUpdateEvent` per relevant mutation onto JobFanoutQueue.
    //
    // Runtime is Node.js 22 to match the AWS SDK v3 modules
    // (`@aws-sdk/client-sqs`, `@aws-sdk/util-dynamodb`) that the
    // handler imports — those packages need a modern Node baseline,
    // and Node 22 is the current LTS at AWS Lambda.
    //
    // Bundling: `NodejsFunction` defaults to esbuild + AWS SDK
    // externalization (the v3 modules ship inside the Lambda runtime
    // image). We rely on the defaults; no custom `bundling` overrides.
    const jobChangefeedFanout = new NodejsFunction(this, "JobChangefeedFanout", {
      functionName: `${projectName}-job-changefeed-fanout`,
      runtime: lambda.Runtime.NODEJS_22_X,
      entry: path.join(__dirname, "lambdas/changefeed-fanout/handler.ts"),
      handler: "handler",
      timeout: cdk.Duration.seconds(60),
      memorySize: 256,
      environment: {
        JOB_FANOUT_QUEUE_URL: jobFanoutQueue.queueUrl,
        // Plan State bridge: when a gateway job with runId reaches terminal,
        // write the node completion to the Plan State table so the Plan Chainer fires.
        PLAN_STATE_TABLE: planStateTable.tableName,
        JOB_REGISTRY_TABLE: jobRegistryTable.tableName,
      },
      description:
        "Subscribes to JobRegistry DDB stream and fans out per-actor SSE frames to JobFanoutQueue (async-job-tracking R8.1, R8.2)",
    });

    // Subscribe the Lambda to the JobRegistry stream.
    //
    // - `LATEST` matches the design's "live changefeed" intent — we
    //   only care about mutations that happen after the Lambda is
    //   wired up; historical state is hydrated by the proxy's
    //   `GET /jobs` reconnect-resync (R8.3, task 10.1).
    // - Batch size 10: matches the SQS `SendMessageBatch` cap so each
    //   stream batch maps to at most one outbound batch — no
    //   intra-batch chunking, no partial-failure handling beyond what
    //   the handler already does.
    // - `bisectBatchOnError + retryAttempts: 3`: small bisect window
    //   keeps the polling worker's own MODIFY frequency from amplifying
    //   a single poison record into a stream-wide stall.
    // DLQ for the changefeed stream. Without an onFailure destination, a poison
    // record that exhausts retries + bisect is silently DROPPED — the job's
    // status transition never fans out (Jobs panel stuck until a manual
    // reconnect-resync). This captures the failed-batch metadata for
    // investigation/replay + drives a DLQ-depth alarm (resilience P0).
    const jobChangefeedDlq = new sqs.Queue(this, "JobChangefeedDLQ", {
      queueName: `${projectName}-job-changefeed-dlq`,
      retentionPeriod: cdk.Duration.days(14),
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    jobChangefeedFanout.addEventSource(
      new lambdaEventSources.DynamoEventSource(jobRegistryTable, {
        startingPosition: lambda.StartingPosition.LATEST,
        batchSize: 10,
        retryAttempts: 3,
        bisectBatchOnError: true,
        // Bound how long a poison batch blocks the shard, then route to the DLQ
        // rather than dropping it.
        maxRecordAge: cdk.Duration.hours(1),
        onFailure: new lambdaEventSources.SqsDlq(jobChangefeedDlq),
      }),
    );

    // Grant the Lambda permission to publish to the fanout queue.
    // `grantSendMessages` covers both `sqs:SendMessage` and
    // `sqs:SendMessageBatch` — the handler uses the batch form.
    // Stream-read permissions (`DescribeStream`, `GetRecords`,
    // `GetShardIterator`, `ListStreams`) are auto-granted by
    // `DynamoEventSource` against the registry's stream ARN, so we
    // don't restate them here.
    jobFanoutQueue.grantSendMessages(jobChangefeedFanout);
    // Bridge: changefeed writes plan state + job registry for plan-node completions
    planStateTable.grantReadWriteData(jobChangefeedFanout);
    jobRegistryTable.grantReadWriteData(jobChangefeedFanout);

    new cdk.CfnOutput(this, "JobFanoutQueueUrl", {
      value: jobFanoutQueue.queueUrl,
      description:
        "SQS FIFO queue URL consumed by the agentcore-proxy /jobs/stream SSE handler (async-job-tracking R8.1, R8.2)",
    });

    new cdk.CfnOutput(this, "JobFanoutQueueArn", {
      value: jobFanoutQueue.queueArn,
      description:
        "SQS FIFO queue ARN — used by task 10.3 to grant the proxy ReceiveMessage/DeleteMessage",
    });

    new cdk.CfnOutput(this, "JobFanoutDlqArn", {
      value: jobFanoutDlq.queueArn,
      description: "DLQ for poison fanout messages (resilience P0; alarm on ApproximateNumberOfMessagesVisible > 0)",
    });

    new cdk.CfnOutput(this, "JobChangefeedDlqArn", {
      value: jobChangefeedDlq.queueArn,
      description: "DLQ for dropped changefeed stream batches (resilience P0; alarm on depth > 0)",
    });

    new cdk.CfnOutput(this, "JobChangefeedFanoutFunctionArn", {
      value: jobChangefeedFanout.functionArn,
      description:
        "Lambda function ARN for the JobRegistry → SQS changefeed fanout (async-job-tracking R8.1)",
    });

    // ---- Resilience alarms + dashboard (resilience P0) ----
    //
    // Makes the async-job pipeline's silent failures loud: DLQ depth (dropped
    // status batches / poison fanout messages), Lambda errors/throttles, and
    // the polling worker's custom metrics (stuck-job sweep firing, elevated
    // non-transient poll errors). All alarms publish to an SNS topic — ops
    // subscribes an email/Slack endpoint (ResilienceAlarmTopicArn output).
    const alarmTopic = new sns.Topic(this, "ResilienceAlarmTopic", {
      topicName: `${projectName}-resilience-alarms`,
      displayName: "Mediaworks Harness — resilience alarms",
    });
    const alarmAction = new cloudwatchActions.SnsAction(alarmTopic);
    const period5 = cdk.Duration.minutes(5);
    const jobsMetric = (metricName: string, statistic = "Sum") =>
      new cloudwatch.Metric({
        namespace: "MediaworksHarness/Jobs",
        metricName,
        period: period5,
        statistic,
      });
    const mkAlarm = (
      id: string,
      metric: cloudwatch.IMetric,
      threshold: number,
      alarmDescription: string,
    ): cloudwatch.Alarm => {
      const alarm = new cloudwatch.Alarm(this, id, {
        metric,
        threshold,
        evaluationPeriods: 1,
        comparisonOperator:
          cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        alarmDescription,
      });
      alarm.addAlarmAction(alarmAction);
      return alarm;
    };

    const changefeedDlqDepth = jobChangefeedDlq.metricApproximateNumberOfMessagesVisible(
      { period: period5, statistic: "Maximum" });
    const fanoutDlqDepth = jobFanoutDlq.metricApproximateNumberOfMessagesVisible(
      { period: period5, statistic: "Maximum" });
    const changefeedErrors = jobChangefeedFanout.metricErrors({ period: period5, statistic: "Sum" });
    const pollErrors = pollingWorker.metricErrors({ period: period5, statistic: "Sum" });
    const pollThrottles = pollingWorker.metricThrottles({ period: period5, statistic: "Sum" });

    // Data-loss signals — any message in a DLQ is a page.
    mkAlarm("ChangefeedDlqDepthAlarm", changefeedDlqDepth, 0,
      "JobRegistry changefeed DLQ has messages — job-status batches were dropped after retries. Investigate/replay.");
    mkAlarm("FanoutDlqDepthAlarm", fanoutDlqDepth, 0,
      "Job fanout DLQ has poison messages the SSE consumer could not process.");
    // Compute failures.
    mkAlarm("ChangefeedErrorsAlarm", changefeedErrors, 0,
      "Changefeed fanout Lambda is erroring — real-time job push degraded.");
    mkAlarm("PollingWorkerErrorsAlarm", pollErrors, 0,
      "Polling worker Lambda is erroring — job status reconciliation degraded.");
    mkAlarm("PollingWorkerThrottlesAlarm", pollThrottles, 0,
      "Polling worker is being throttled.");
    // Job-health signals from the worker's custom metrics.
    mkAlarm("StuckJobsSweptAlarm", jobsMetric("JobsTimedOutBySweep"), 0,
      "Jobs are being force-timed-out by the stuck-job sweep — a workflow/gateway is hanging.");
    mkAlarm("PollNonTransientErrorsAlarm", jobsMetric("PollNonTransientErrors"), 5,
      "Elevated non-transient poll errors — gateway/tool likely failing.");

    new cdk.CfnOutput(this, "ResilienceAlarmTopicArn", {
      value: alarmTopic.topicArn,
      description: "Subscribe an email/Slack endpoint here to receive resilience alarms",
    });

    new cloudwatch.Dashboard(this, "ResilienceDashboard", {
      dashboardName: `${projectName}-resilience`,
      widgets: [
        [new cloudwatch.GraphWidget({
          title: "DLQ depth (data loss)",
          left: [changefeedDlqDepth, fanoutDlqDepth],
        })],
        [new cloudwatch.GraphWidget({
          title: "Lambda errors / throttles",
          left: [changefeedErrors, pollErrors, pollThrottles],
        })],
        [new cloudwatch.GraphWidget({
          title: "Jobs (active / swept / poll errors)",
          left: [
            jobsMetric("ActiveJobs", "Maximum"),
            jobsMetric("JobsTimedOutBySweep"),
            jobsMetric("PollTransientErrors"),
            jobsMetric("PollNonTransientErrors"),
          ],
        })],
      ],
    });

    // ---- AgentCore-proxy IAM policy (async-job-tracking task 10.3) ----
    //
    // Bundles the DDB + SQS permissions the agentcore-proxy needs to
    // serve `/jobs`, `/jobs/:id`, `/jobs?ids=...`, and `/jobs/stream`
    // (R11.1, R11.2). Required actions are derived directly from the
    // SDK calls in `ui/agentcore-proxy.ts`:
    //
    //   * `dynamodb:Query`  on the JobRegistry table AND the
    //     `actor-status-index` GSI — `GET /jobs` issues a
    //     `QueryCommand` against the GSI keyed by `actorId`, sorted
    //     newest-first by `updatedAt`.
    //   * `dynamodb:GetItem` on the JobRegistry table — both
    //     `GET /jobs/:id` (single-record lookup, cross-actor returns
    //     404) and the `GET /jobs?ids=a,b,c` reconnect-resync read
    //     issue parallel `GetCommand`s by `jobId`.
    //   * `sqs:ReceiveMessage` + `sqs:DeleteMessage` +
    //     `sqs:ChangeMessageVisibility` on the `JobFanoutQueue` ARN —
    //     the SSE handler at `/jobs/stream` runs a 20-second long-poll
    //     `ReceiveMessage` loop, `DeleteMessage`s frames it has
    //     forwarded to the matching actor's SSE connection, and uses
    //     `ChangeMessageVisibility(VisibilityTimeout=0)` to release
    //     cross-actor frames immediately back to the queue (see
    //     `ui/agentcore-proxy.ts`, `/jobs/stream` middleware).
    //
    // Why a `ManagedPolicy` and not a role:
    //   The agentcore-proxy currently runs as a Vite dev server (local
    //   Node.js process) with developer AWS credentials in dev — there
    //   is no formal proxy execution role in this stack today. The
    //   existing `LabRole` / `ChatRole` / `ProjectRuntimeRole`
    //   constructs are AgentCore Runtime session-tagged roles for
    //   Strands sub-process work, not for the Vite proxy that fronts
    //   them.
    //
    //   A standalone `iam.ManagedPolicy` is principal-agnostic and
    //   ARN-addressable: a developer can attach it to their IAM user
    //   for local testing, and a future deployment (Lambda, ECS,
    //   Fargate, EKS, …) can attach it to whatever execution role it
    //   ends up using — without forcing this stack to predict that
    //   future shape. The pattern mirrors `SkillsAccessPolicy` at the
    //   top of this stack.
    //
    // What it does NOT grant:
    //   * `dynamodb:PutItem` / `UpdateItem` / `DeleteItem` — the proxy
    //     is read-only against the JobRegistry. Writes happen only in
    //     the agent runtime (`agent/src/jobs/registry.py`) and the
    //     polling worker.
    //   * `sqs:SendMessage*` — the proxy is a consumer; only the
    //     ChangefeedFanout Lambda publishes (and that grant is wired
    //     above).
    const agentCoreProxyPolicy = new iam.ManagedPolicy(
      this,
      "AgentCoreProxyPolicy",
      {
        managedPolicyName: `${projectName}-agentcore-proxy-job-tracking`,
        description:
          "Permissions the agentcore-proxy needs to serve /jobs, /jobs/:id, " +
          "/jobs?ids=..., and /jobs/stream (async-job-tracking R11.1, R11.2). " +
          "Attach to whatever principal runs the proxy (developer IAM user " +
          "in dev, future Lambda/ECS execution role in production).",
        statements: [
          new iam.PolicyStatement({
            sid: "JobRegistryRead",
            effect: iam.Effect.ALLOW,
            actions: ["dynamodb:Query", "dynamodb:GetItem"],
            resources: [
              jobRegistryTable.tableArn,
              `${jobRegistryTable.tableArn}/index/${jobRegistryActorStatusIndexName}`,
            ],
          }),
          new iam.PolicyStatement({
            sid: "JobFanoutConsume",
            effect: iam.Effect.ALLOW,
            actions: [
              "sqs:ReceiveMessage",
              "sqs:DeleteMessage",
              "sqs:ChangeMessageVisibility",
            ],
            resources: [jobFanoutQueue.queueArn],
          }),
        ],
      },
    );

    new cdk.CfnOutput(this, "AgentCoreProxyPolicyArn", {
      value: agentCoreProxyPolicy.managedPolicyArn,
      description:
        "Managed policy ARN bundling the DDB+SQS permissions the agentcore-proxy " +
        "needs for /jobs and /jobs/stream (async-job-tracking task 10.3, R11.1, R11.2). " +
        "Attach to whatever principal runs the proxy.",
    });

    // ---- CloudFront for Artifacts ----
    // (artifactsDistribution is declared earlier, before the MCP runtimes,
    //  so the M2C results tools can be handed the CDN domain via env.)
    new cdk.CfnOutput(this, "ArtifactsDomain", {
      value: artifactsDistribution.distributionDomainName,
      description: "CloudFront domain for artifact URLs",
    });

    // ---- Render Agent Runtime ----
    // The name is a plain constant so a caller's InvokeAgentRuntime grant can be scoped by
    // NAME (`runtime/<name>-*`) instead of by `agentRuntimeArn` — a GetAtt there would make the
    // caller's execution-role DefaultPolicy, and so the caller runtime (which DependsOn it),
    // wait for this runtime to finish updating. See the runtime-ARN SSM parameters below.
    const renderAgentRuntimeName = `${projectName.replace(/-/g, "_")}_render_agent`;
    const renderAgentRuntime = new agentcore.Runtime(this, "RenderAgentRuntime", {
      runtimeName: renderAgentRuntimeName,
      agentRuntimeArtifact: agentcore.AgentRuntimeArtifact.fromS3(
        {
          bucketName: agentCodeBucket.bucketName,
          objectKey: zipObjectKey,
        },
        agentcore.AgentCoreRuntime.PYTHON_3_13,
        ["opentelemetry-instrument", "render_agent.py"],
      ),
      protocolConfiguration: agentcore.ProtocolType.HTTP,
      networkConfiguration: agentcore.RuntimeNetworkConfiguration.usingPublicNetwork(),
      environmentVariables: {
        AWS_REGION: this.region,
        SKILLS_S3_BUCKET: skillsBucket.bucketName,
        ARTIFACTS_PREFIX: "artifacts/",
        ARTIFACTS_DOMAIN: artifactsDistribution.distributionDomainName,
        // Gateway registry (same table the task agent reads) so the render
        // agent can load the media-outputs gateway tool and resolve
        // gateway-managed s3:// artifacts to http URLs via get_download_url.
        GATEWAY_REGISTRY_TABLE: gatewayRegistryTable.tableName,
      },
    });
    renderAgentRuntime.node.addDependency(zipPackager);

    // CANARY: opt the render runtime into AgentCore Runtime platform V2 (snapshot-restore)
    // — consistent ~2s cold starts regardless of image size (vs V1's ~5.4–30s). Set via an
    // L1 escape hatch because the alpha construct (2.244) exposes no typed prop yet; the
    // CloudFormation resource AWS::BedrockAgentCore::Runtime supports PlatformVersion today
    // (verified LIVE in us-west-2), and it is a NON-replacement (in-place) property, so the
    // runtime ARN is preserved — no proxy rewiring. Reversible: remove this to return to V1.
    // Render is the canary (async, non-user-blocking); fan out to the other runtimes after
    // validating cold-start delta + health. Snapshot-safety audited: per-request uuids/time,
    // KMS-signed delegation grants, no startup catalog/token/crypto-RNG.
    (renderAgentRuntime.node.defaultChild as cdk.CfnResource).addPropertyOverride(
      "PlatformVersion",
      "V2",
    );

    // Grant Bedrock model access for manifest generation
    renderAgentRuntime.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
        resources: [
          "arn:aws:bedrock:*::foundation-model/*",
          `arn:aws:bedrock:*:${this.account}:inference-profile/*`,
        ],
      })
    );

    // Grant S3 read/write scoped to the artifacts prefix on the skills bucket.
    renderAgentRuntime.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
        resources: [
          skillsBucket.bucketArn,
          `${skillsBucket.bucketArn}/artifacts/*`,
          // asset-lineage Phase 3 (a): session outputs under the project layout (output sub-tree only).
          `${skillsBucket.bucketArn}/projects/*/sessions/*/output/*`,
        ],
      })
    );
    skillsBucket.grantRead(renderAgentRuntime);

    // Gateway access (mirrors the task agent) so the render agent can load the
    // media-outputs gateway tool and resolve gateway-managed s3:// artifacts to
    // fetchable http URLs via `get_download_url` — no direct proxy-bucket S3 IAM
    // needed (the gateway is the controlled presigner).
    gatewayRegistryTable.grantReadData(renderAgentRuntime);
    renderAgentRuntime.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["secretsmanager:GetSecretValue"],
        resources: [`arn:aws:secretsmanager:*:*:secret:/${projectName}/gateway/*`],
      })
    );
    // Federation: the M2C media-workflow-gateway credentials live outside the
    // ${projectName}/gateway/* path; grant the render agent explicit read so it
    // can OAuth to the media gateway to call get_download_url.
    if (props.envConfig?.media?.credentialsSecretArn) {
      renderAgentRuntime.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "RenderMediaGatewaySecretRead",
          actions: ["secretsmanager:GetSecretValue"],
          resources: [props.envConfig.media.credentialsSecretArn],
        })
      );
    }

    // ---- Lab Asset_Index DDB + Artifacts S3 (declared early so the Task
    // and Main Agents can mount them as env vars; constructs themselves
    // stay below).
    const labAssetIndexTableName = `${projectName}-lab-asset-index`;
    const labArtifactsBucketNameEarly =
      `${projectName}-lab-artifacts-${this.account}-${this.region}`;

    // ---- AgentCore Memory ----
    //
    // Durable conversation history. The Main Agent binds it via ``MEMORY_ID``
    // env var; Strands' ``AgentCoreMemorySessionManager`` reads/writes events
    // keyed by ``(actor_id, session_id)`` so each chat session has isolated
    // history that survives runtime restarts. The UI also reads it directly
    // via the proxy ``/memory/*`` routes to repopulate the chat on session
    // switch. The Task Agent binds the SAME store for externally-invoked
    // Custom Agents, keyed on the segregated ``EXT#`` keyspace (custom-agents
    // Req 10) — declared here, above both runtimes, so each can reference it.
    //
    // Event expiry is 30 days; long enough for normal usage, short enough
    // that a forgotten session doesn't accrue indefinitely.
    const conversationMemory = new cdk.aws_bedrockagentcore.CfnMemory(
      this,
      "ConversationMemory",
      {
        name: `${projectName.replace(/-/g, "_")}_conversation_memory`,
        description: "Main Agent conversation history (per actor, per session)",
        eventExpiryDuration: 30,
        // Long-term memory strategies. These are BUILT-IN strategies (no
        // overrides), so they do NOT require a `memoryExecutionRoleArn` — the
        // AgentCore Memory service runs extraction with its own capacity
        // (built-in-WITH-overrides / custom strategies are the ones that need
        // an execution role).
        //
        // `namespaces` are PINNED here and MUST stay in lockstep with the
        // constants in agent/src/security/identity.py (SESSION_SUMMARY_NAMESPACE =
        // "/summaries/{actorId}/{sessionId}/", USER_PREFERENCE_NAMESPACE =
        // "/preferences/{actorId}/" — leading+trailing slashes per the AWS
        // Strands+AgentCore doc convention). The extraction side (this config)
        // routes records into these namespaces; the retrieval side (the Strands
        // session manager's retrieval_config) resolves the same templates. If
        // they drift, retrieval silently returns nothing. `{actorId}` is
        // actor-scoped → cross-session recall; `{sessionId}` scopes the summary
        // per session.
        //
        // DEPLOY GATE: changing strategies on the EXISTING memory resource must be
        // an in-place UPDATE, not a replacement — a replacement would change
        // MEMORY_ID and drop the 30-day short-term event store (all users' chat
        // history) plus every saved preference and summary. Confirm the change
        // set shows Modify (not Replace) for ConversationMemory before pushing to
        // origin/staging.
        memoryStrategies: [
          {
            // Rolling per-session summaries → cross-session summary context.
            summaryMemoryStrategy: {
              name: "conversation_summary",
              description:
                "Rolling per-session conversation summaries for cross-session summary context.",
              namespaces: ["/summaries/{actorId}/{sessionId}/"],
            },
          },
          // The `semantic_facts` strategy (`/facts/{actorId}/`) was REMOVED (2026-10): actor-wide,
          // never-expiring, project-less facts leaked file locations and media claims across
          // projects (staging session 07703a35); Projects now own files and content. Removing it
          // is an in-place update: the CloudFormation reference for AWS::BedrockAgentCore::Memory
          // lists `MemoryStrategies` (and each MemoryStrategy member) as "Update requires: No
          // interruption"; only `Name` and `EncryptionKeyArn` require replacement. The harness
          // stopped reading and writing /facts/ in the same change.
          {
            // Learned user preferences (how the user likes things), per actor.
            userPreferenceMemoryStrategy: {
              name: "user_preferences",
              description:
                "Durable per-actor user preferences learned from conversations.",
              namespaces: ["/preferences/{actorId}/"],
            },
          },
        ],
      },
    );

    // ---- Runtime config secret (V2 1536-byte env cap) ----
    // AgentCore Runtime V2 caps the env-var payload at 1536 bytes. task + main exceed it, so the
    // bulk of static config (and the two Cognito client secrets, out of plaintext env) lives in
    // ONE Secrets Manager secret the agent fetches at startup (runtime_config_bootstrap.py →
    // os.environ.setdefault). Only AWS_REGION/DEPLOY_ENV/BYPASS + the inter-runtime ARN cross-refs
    // + this secret's ARN stay in env. The secret references ONLY resources defined above (all
    // before this line), so there is no cycle with the runtimes that read its ARN. Inter-runtime
    // ARNs deliberately stay in env (putting them here would make the secret depend on the very
    // runtimes that depend on it).
    // ---- Projects / Project_Token / Search_Gateway nested stack (media-harness-projects-access) ----
    // Created here, before `runtimeConfig`, so its outputs can go into RuntimeConfigSecret. It is a
    // NestedStack (D1): the parent template grows only by the AWS::CloudFormation::Stack resource and
    // a few parameters/policies, keeping it under the 1,000,000-byte limit. The proxy and Main_Agent
    // runtime role ARNs it needs for the token functions' resource policies are passed lazily because
    // those roles are created further down; the Lazy producers resolve at synth, after construction.
    const projectsCfg = props.envConfig?.projects;
    let projectsStack: ProjectsStack | undefined;
    if (projectsCfg?.enabled) {
      projectsStack = new ProjectsStack(this, "ProjectsStack", {
        projectName,
        envName,
        projectsConfig: projectsCfg,
        userPoolId: userPool.userPoolId,
        userPoolClientId: userPoolClient.userPoolClientId,
        m2cGatewayUrl: props.envConfig?.media?.gatewayUrl ?? "",
        m2cCredentialsSecretArn: props.envConfig?.media?.credentialsSecretArn ?? "",
        m2cClientId: props.envConfig?.media?.clientId ?? "",
        m2cTokenEndpoint: props.envConfig?.media?.tokenEndpoint ?? "",
        m2cDiscoveryUrl: props.envConfig?.media?.oidcDiscoveryUrl,
        m2cScope: props.envConfig?.media?.scope,
        ingestBucketName: skillsBucket.bucketName,
        m2cProxyBucket: props.envConfig?.media?.proxyBucket,
        // Resolved at synth (post-construction): the roles are created later in this constructor.
        proxyTaskRoleArn: cdk.Lazy.string({ produce: () => uiProxyTaskRole.roleArn }),
        mainAgentRuntimeRoleArn: cdk.Lazy.string({ produce: () => agentRuntime.role.roleArn }),
        // Knowledge-graph tools for dispatched sub-agents (DelegatedTokenIssuerFn).
        delegationKeyArn: delegationGrantKey.keyArn,
        taskAgentRuntimeRoleArn: cdk.Lazy.string({ produce: () => taskAgentRuntime.role.roleArn }),
      });
      (this as { projectsStack?: ProjectsStack }).projectsStack = projectsStack;
    }

    // ---- media-harness-project-ingestion: nested IngestionStack (Asset_Service P0) ----
    // Created when `projects.ingestion.enabled`, which implies `projectsStack`. It is a NestedStack
    // (design §20): the parent template is already ~945 KB of the 1,000,000-byte limit, so the writer
    // Lambda and its role live in the nested template; the parent grows only by the stack resource,
    // the ArtifactsCdn deny (above, always-on), two secret keys, and two invoke grants.
    //
    // Packaging mirrors the agent runtime: the deploy-time zip-packager custom resource bundles
    // `agent/src/ingestion/`, `agent/src/projects/`, and the three token-issuer `.py` files (plus arm64
    // wheels) into a second zip in the shared agent code bucket, which the Asset_Service references.
    let ingestionStack: IngestionStack | undefined;
    let ingestionConfigJson = "";
    if (projectsStack && projectsCfg?.ingestion?.enabled) {
      const ingCfg = projectsCfg.ingestion;
      ingestionConfigJson = JSON.stringify({
        frameIntervalSec: ingCfg.frameIntervalSec ?? 1,
        frameWidth: ingCfg.frameWidth ?? 960,
        frameHeight: ingCfg.frameHeight ?? 540,
        imageModelChain: ingCfg.imageModelChain ?? [],
        imageThinking: ingCfg.imageThinking ?? "",
        s3Allowlist: ingCfg.s3Allowlist ?? [],
        rebuildConcurrency: ingCfg.rebuildConcurrency ?? 8,
        orphanSweep: ingCfg.orphanSweep ?? true,
        // projects.lineageEdges reaches every reader of INGESTION_CONFIG (the Main/Task runtimes via
        // RuntimeConfigSecret, the proxy, the Asset_Service, Generated_Media). The key is written ONLY when on,
        // so an off env's JSON — and every template that embeds it — is byte-identical.
        ...(projectsCfg.lineageEdges ? { lineageEdges: true } : {}),
        // Phase 1 entity-metadata workflows: like lineageEdges, the key is written ONLY when on, so an off
        // env's JSON — and every template embedding it — stays byte-identical.
        ...(ingCfg.entityMetadata ? { entityMetadata: true } : {}),
      });

      // Collect the ingestion Lambda sources. `agent/src/ingestion` and `agent/src/projects` are
      // bundled under their package names; the token-issuer modules are bundled at the package
      // ROOT so `import cognito` resolves (design §20 "Lambda packaging").
      const ingestionCode: Record<string, string> = {};
      const walkPy = (dir: string, prefix: string): void => {
        for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
          if (entry.name === "__pycache__" || entry.name.startsWith(".")) continue;
          const abs = path.join(dir, entry.name);
          if (entry.isDirectory()) {
            walkPy(abs, path.join(prefix, entry.name));
          } else if (entry.isFile() && (entry.name.endsWith(".py") || entry.name.endsWith(".txt"))) {
            ingestionCode[path.join(prefix, entry.name)] = fs.readFileSync(abs).toString("base64");
          }
        }
      };
      walkPy(path.join(agentSrcDir, "ingestion"), "ingestion");
      walkPy(path.join(agentSrcDir, "projects"), "projects");
      const tokenIssuerDir = path.join(__dirname, "lambdas", "token-issuer");
      // cognito.py verifies the User_Token; it and tokens.py both import jwslite (the stdlib JOSE
      // helpers that replaced cryptography/PyJWT), so jwslite.py MUST be bundled or `import cognito` fails.
      for (const f of ["access.py", "cognito.py", "tokens.py", "jwslite.py"]) {
        ingestionCode[f] = fs.readFileSync(path.join(tokenIssuerDir, f)).toString("base64");
      }

      // Third-party runtime deps: Pillow (IngestTick's image Search_Rendition, design §12) and pypdf
      // (its PDF text extraction, Requirement 16 / design §21). Both are pure-Python universal wheels
      // (pypdf has no native build), so the arm64/py3.13 packager cannot fail on them. The token-issuer
      // modules are stdlib-only (kms:Sign/Verify + jwslite).
      // These Lambdas decode USER uploads, so they take the advisory-free releases (pip-audit 2026-10):
      // pillow 13.0.0 (the runtime zip pins the same release in agent/src/requirements.txt since
      // strands-agents-tools, which held it below 13, was dropped) and pypdf 6.21.4 (also the dev-extra
      // pin in agent/pyproject.toml).
      const ingestionRequirements: string[] = ["pillow==13.0.0", "pypdf==6.21.4"];
      const ingestionHash = crypto
        .createHash("sha256")
        .update(JSON.stringify({ ingestionRequirements, ingestionCode, v: "ingestion-p0-v2" }))
        .digest("hex")
        .slice(0, 16);
      const ingestionCodeKey = `ingestion_code_${ingestionHash}.json`;
      const ingestionCodeAsset = new s3deploy.BucketDeployment(this, "IngestionCodeAsset", {
        sources: [s3deploy.Source.jsonData(ingestionCodeKey, ingestionCode)],
        destinationBucket: agentCodeBucket,
        destinationKeyPrefix: "",
        prune: false,
      });
      const ingestionZipKey = `ingestion_package_${ingestionHash}.zip`;
      const ingestionZip = new cdk.CustomResource(this, "IngestionZipPackager", {
        serviceToken: packagerProvider.serviceToken,
        properties: {
          BucketName: agentCodeBucket.bucketName,
          ObjectKey: ingestionZipKey,
          Requirements: ingestionRequirements,
          AgentCodeBucket: agentCodeBucket.bucketName,
          AgentCodeKey: ingestionCodeKey,
          ContentHash: ingestionHash,
        },
      });
      ingestionZip.node.addDependency(ingestionCodeAsset);

      // The HeroFrame Lambda's package: the same sources plus PyAV (FFmpeg as an abi3 manylinux_2_28 aarch64
      // wheel, ~95 MB unzipped). Its own zip so the other ingestion Lambdas do not carry a video decoder.
      const heroFrameRequirements: string[] = ["pillow==13.0.0", "av==19.0.1"];
      const heroFrameHash = crypto
        .createHash("sha256")
        .update(JSON.stringify({ heroFrameRequirements, ingestionCode, v: "hero-frame-v1" }))
        .digest("hex")
        .slice(0, 16);
      const heroFrameZipKey = `hero_frame_package_${heroFrameHash}.zip`;
      const heroFrameZip = new cdk.CustomResource(this, "HeroFrameZipPackager", {
        serviceToken: packagerProvider.serviceToken,
        properties: {
          BucketName: agentCodeBucket.bucketName,
          ObjectKey: heroFrameZipKey,
          Requirements: heroFrameRequirements,
          AgentCodeBucket: agentCodeBucket.bucketName,
          AgentCodeKey: ingestionCodeKey,
          ContentHash: heroFrameHash,
        },
      });
      heroFrameZip.node.addDependency(ingestionCodeAsset);

      ingestionStack = new IngestionStack(this, "IngestionStack", {
        projectName,
        envName,
        projectsTableName: projectsStack.projectsTable.tableName,
        projectsTableArn: projectsStack.projectsTable.tableArn,
        projectsTableStreamArn: projectsStack.projectsTable.tableStreamArn,
        ingestBucketName: skillsBucket.bucketName,
        userPoolId: userPool.userPoolId,
        userPoolClientId: userPoolClient.userPoolClientId,
        // P0 reads INGESTION_CONFIG from its own env; runtimeConfigSecretArn is intentionally omitted
        // so the nested stack does not reference the secret (which references ASSET_SERVICE_FN) — that
        // would be a resource cycle. The later pipeline wave threads it when those Lambdas need it.
        codeBucketName: agentCodeBucket.bucketName,
        codeObjectKey: ingestionZipKey,
        heroFrameCodeObjectKey: heroFrameZipKey,
        proxyTaskRoleArn: cdk.Lazy.string({ produce: () => uiProxyTaskRole.roleArn }),
        mainAgentRuntimeRoleArn: cdk.Lazy.string({ produce: () => agentRuntime.role.roleArn }),
        ingestionConfigJson,
        m2c: (() => {
          const m = props.envConfig?.media;
          return m?.gatewayUrl && m.clientId && m.tokenEndpoint && m.credentialsSecretArn && m.proxyBucket
            ? { gatewayUrl: m.gatewayUrl, clientId: m.clientId, tokenEndpoint: m.tokenEndpoint,
                credentialsSecretArn: m.credentialsSecretArn, scope: m.scope, proxyBucket: m.proxyBucket }
            : undefined;
        })(),
        generatedMedia: projectsCfg.ingestion.generatedMedia
          ? {
              jobRegistryTableName: jobRegistryTable.tableName,
              jobRegistryTableArn: jobRegistryTable.tableArn,
              jobRegistryStreamArn: jobRegistryTable.tableStreamArn!,
              sessionsTableName: sessionsTable.tableName,
              sessionsTableArn: sessionsTable.tableArn,
            }
          : undefined,
        personalSamples: projectsCfg.ingestion.personalSamples ? PERSONAL_SAMPLES : undefined,
      });
      // The Asset_Service references the deploy-time zip by (bucket, key); make the nested stack wait
      // for the packager so the object exists before the function is created.
      ingestionStack.node.addDependency(ingestionZip);
      ingestionStack.node.addDependency(heroFrameZip);
      (this as { ingestionStack?: IngestionStack }).ingestionStack = ingestionStack;

      // Personal samples: upload `infra/assets/samples/` (the demo tour's 0-demo.mp4 and its NOTICE) ONCE to the
      // Ingest bucket under `samples/`; the Asset_Service copies them into each user's Personal project. Only
      // these keys are touched: `prune: false` never deletes anything else under `samples/`, and the objects stay
      // on a stack delete (users' copies are their own files). The bucket stays private (no new grant to it).
      if (projectsCfg.ingestion.personalSamples) {
        new s3deploy.BucketDeployment(this, "PersonalSamplesAsset", {
          sources: [s3deploy.Source.asset(PERSONAL_SAMPLES_DIR)],
          destinationBucket: skillsBucket,
          destinationKeyPrefix: "samples/",
          prune: false,
          retainOnDelete: true,
          memoryLimit: 512,
        });
      }
    }

    // Append-only `asset_events` audit history (`AssetAuditStack`, asset-lineage Phase 3 / D6): a
    // second consumer of the EXISTING projects-table stream, writing one Iceberg commit per batch into
    // an S3 Tables table. Flagged OFF unless `envConfig.assetAuditTable` (staging today) and skipped
    // with a warning where there is no projects table to read. The appender consumes only when
    // `envConfig.assetAuditAppend` is also set (needs the out-of-band `s3tablescatalog` integration).
    let assetAuditStack: AssetAuditStack | undefined;
    if (props.envConfig?.assetAuditTable) {
      if (!projectsStack) {
        cdk.Annotations.of(this).addWarning(
          "assetAuditTable is set but projects is not enabled: AssetAuditStack skipped (no projects stream).");
      } else {
        assetAuditStack = new AssetAuditStack(this, "AssetAuditStack", {
          projectName,
          envName,
          projectsTableStreamArn: projectsStack.projectsTable.tableStreamArn!,
          appendEnabled: props.envConfig?.assetAuditAppend === true,
        });
        (this as { assetAuditStack?: AssetAuditStack }).assetAuditStack = assetAuditStack;
      }
    }

    // Per-Project knowledge graph (`GraphStack`): only where the env enables it. The image is the one CI
    // pushed (`-c graphLambdaImageTag=<sha>-<iid>`, out-of-band repo), or else the one HarnessAssets
    // built in CodeBuild for this deployment.
    let graphStack: GraphStack | undefined;
    const graphImage = graphImageTagCtx
      ? { repositoryName: graphRepositoryName(projectName), imageTag: graphImageTagCtx }
      : harnessAssets?.graphImage
        ? { repositoryName: harnessAssets.graphImage.repository.repositoryName, imageTag: harnessAssets.graphImage.tag }
        : undefined;
    if (projectsStack && ingestionStack && projectsCfg?.graph?.enabled) {
      if (!graphImage) {
        throw new Error("projects.graph.enabled but no graph image: neither -c graphLambdaImageTag nor an in-stack build");
      } else {
        graphStack = new GraphStack(this, "GraphStack", {
          projectName,
          projectsTableName: projectsStack.projectsTable.tableName,
          projectsTableArn: projectsStack.projectsTable.tableArn,
          projectsTableStreamArn: projectsStack.projectsTable.tableStreamArn!,
          ingestBucketName: skillsBucket.bucketName,
          proxyTaskRoleArn: cdk.Lazy.string({ produce: () => uiProxyTaskRole.roleArn }),
          repositoryName: graphImage.repositoryName,
          imageTag: graphImage.imageTag,
          // Agent graph tools on the project Search_Gateway — same Project_Token + Cedar privacy as search.
          search: {
            gatewayId: projectsStack.searchGateway.attrGatewayIdentifier,
            gatewayArn: projectsStack.searchGateway.attrGatewayArn,
            gatewayRoleArn: projectsStack.searchGatewayRole.roleArn,
            policyEngineId: projectsStack.searchPolicyEngine.attrPolicyEngineId,
          },
        });
        (this as { graphStack?: GraphStack }).graphStack = graphStack;
      }
    }

    // Inter-runtime ARNs live in SSM parameters under this path (one per sub-runtime, written
    // AFTER the runtimes exist — see "Runtime ARN parameters" below), NOT in any runtime's env.
    // An env var holding `<other>.agentRuntimeArn` is a GetAtt, which made task wait for render and
    // main wait for all the others, serialising the runtime updates (render → task → main, ~4 min each).
    // The path is a plain string, so neither this secret nor any runtime depends on a runtime.
    const runtimeArnParamPath = `/${projectName}-${envName}/runtime-arns`;
    const runtimeArnParamReadStatements = (): iam.PolicyStatement[] => [
      // Built from strings rather than `param.parameterArn` so the grant creates no edge to the
      // parameters (and through them to the runtimes whose ARNs they hold).
      new iam.PolicyStatement({
        sid: "RuntimeArnParamsByPath",
        actions: ["ssm:GetParametersByPath"],
        resources: [`arn:aws:ssm:${this.region}:${this.account}:parameter${runtimeArnParamPath}`],
      }),
      new iam.PolicyStatement({
        sid: "RuntimeArnParams",
        actions: ["ssm:GetParameter", "ssm:GetParameters"],
        resources: [`arn:aws:ssm:${this.region}:${this.account}:parameter${runtimeArnParamPath}/*`],
      }),
    ];
    const runtimeConfig: { [k: string]: string } = {
      APP_VERSION: appVersion,
      RUNTIME_ARN_PARAM_PATH: runtimeArnParamPath,
      SKILLS_S3_BUCKET: skillsBucket.bucketName,
      SKILLS_S3_PREFIX: "skills/",
      UPLOADS_S3_BUCKET: uploadsBucket.bucketName,
      ARTIFACTS_BUCKET: labArtifactsBucketNameEarly,
      ARTIFACTS_DOMAIN: artifactsDistribution.distributionDomainName,
      ARTIFACTS_PREFIX: "artifacts/",
      PLAN_STATE_TABLE: planStateTable.tableName,
      GATEWAY_REGISTRY_TABLE: gatewayRegistryTable.tableName,
      JOB_REGISTRY_TABLE: jobRegistryTable.tableName,
      ASSET_INDEX_TABLE: labAssetIndexTableName,
      CODE_INTERPRETER_ID: codeInterpreter.codeInterpreterId,
      MEMORY_ID: conversationMemory.attrMemoryId,
      PROJECT_NAME: projectName,
      // Reasoning-effort cap for the Opus orchestrator + Opus plan nodes (agent/src/model
      // /factory.py honors this; Sonnet/Haiku agents are gated out). Lower effort scales
      // the model's thinking — and latency — down on every call. `low` is the shipped
      // default: on Opus 5 it cut think-before-first-token (TTFT) from ~8.4s at medium to
      // ~2.7s (vs ~21s with no field), the dominant perceived-latency cost of the Opus 5
      // switch — opus-4-8's TTFT is ~0.8s, so low gets Opus 5 much closer. `medium`/`high`
      // trade speed for deeper multi-step reasoning; set in agent/src/model/models.json
      // (`reasoning_effort`). In the RuntimeConfigSecret (hydrated into
      // os.environ by runtime_config_bootstrap), not inline runtime env — respects the V2
      // 1536-byte env budget. factory.py sends ONLY output_config.effort (no thinking field):
      // an explicit thinking:{adaptive} front-loaded reasoning and spiked TTFT to 8-18s.
      BEDROCK_REASONING_EFFORT: modelDefaults.reasoning_effort,
      DYNAMIC_FANOUT_ENABLED: dynamicFanoutFlag,
      MEDIA_PROXY_BUCKET: props.envConfig?.media?.proxyBucket ?? "",
      HARNESS_GATEWAY_ID: gateway.attrGatewayIdentifier,
      GATEWAY_ENDPOINTS: gateway.attrGatewayUrl,
      HARNESS_PLATFORM_GATEWAY_ID: platformGateway.attrGatewayIdentifier,
      HARNESS_PLATFORM_GATEWAY_URL: platformGateway.attrGatewayUrl,
      // Web Search: deployed here or not. Whether a TURN may search is the chat's toggle (agent/src/web_search.py).
      WEB_SEARCH_ENABLED: webSearchCfg ? "true" : "false",
      DELEGATION_SIGNING_KEY_ARN: delegationGrantKey.keyArn,
      DELEGATION_GRANTS_ENABLED:
        (this.node.tryGetContext("enableDelegationGrants") === false ||
          this.node.tryGetContext("enableDelegationGrants") === "false")
          ? "false"
          : "true",
      ASYNC_PLAN_TOKEN_ENDPOINT: asyncPlanTokenEndpoint,
      ASYNC_PLAN_CLIENT_ID: asyncPlanClient.userPoolClientId,
      ASYNC_PLAN_CLIENT_SECRET: asyncPlanClient.userPoolClientSecret.unsafeUnwrap(),
      ASYNC_PLAN_SCOPE: asyncPlanScope,
      M2M_TOKEN_ENDPOINT: `https://${m2mDomain.domainName}.auth.${this.region}.amazoncognito.com/oauth2/token`,
      M2M_CLIENT_ID: m2mClient.userPoolClientId,
      M2M_CLIENT_SECRET: m2mClient.userPoolClientSecret.unsafeUnwrap(),
      M2M_SCOPE: m2mScope,
    };
    // Projects runtime config — present only where the nested stack exists (prod has none, so no
    // Search_Tool is bound and no ProjectTokenRef is created). SEARCH_TIMEOUT_SECONDS is the fixed 60 s.
    if (projectsStack) {
      Object.assign(runtimeConfig, {
        SEARCH_GATEWAY_URL: projectsStack.searchGatewayUrl,
        SEARCH_TOOL_NAME: projectsStack.searchToolName,
        PROJECTS_TABLE: projectsStack.projectsTable.tableName,
        PROJECT_TOKEN_ISSUER: projectsStack.projectTokenIssuerUrl,
        PROJECT_TOKEN_REFRESH_FN: projectsStack.tokenRefreshFnName,
        // Task Agent / plan nodes mint their own Project_Token for the graph tools (never a forwarded one).
        PROJECT_TOKEN_DELEGATED_FN: delegatedTokenIssuerFunctionName(projectName),
        SEARCH_TIMEOUT_SECONDS: "60",
      });
    }
    // media-harness-project-ingestion: the Main_Agent runtime reads ASSET_SERVICE_FN from here for the
    // Project_Tools (design §17; not a runtime env var, V2's 1536-byte budget). INGESTION_CONFIG rides
    // along for the later pipeline Lambdas (design §20). Both absent when ingestion is off (prod).
    if (ingestionStack) {
      Object.assign(runtimeConfig, {
        ASSET_SERVICE_FN: ingestionStack.assetServiceFnName,
        INGESTION_CONFIG: ingestionConfigJson,
      });
    }
    // artifactsCdnPrivate: the agent rewrites artifact URLs in tool inputs to presigned GETs (external
    // fetchers such as fal cannot read a private CDN). Secret, not runtime env (V2 1536-byte cap).
    if (artifactsCdnPrivate) runtimeConfig.ARTIFACTS_CDN_PRIVATE = "true";
    // asset-lineage Phase 3 (a): writers put NEW session outputs under projects/<pid>/sessions/<sid>/
    // output/ (readers accept both roots regardless). Secret, not runtime env (V2 1536-byte cap).
    if (sessionOutputLayout === "project") runtimeConfig.SESSION_OUTPUT_LAYOUT = "project";
    // asset-lineage Phase 3 (b): the scoped sandbox role, by NAME (it is created after the runtimes,
    // whose roles it trusts; a GetAtt here would be a cycle through this secret).
    if (sandboxScopedCredentials) runtimeConfig.SANDBOX_SCOPED_ROLE_ARN = sandboxScopedRoleArn;
    const runtimeConfigSecret = new cdk.aws_secretsmanager.Secret(this, "RuntimeConfigSecret", {
      secretName: `${projectName}-runtime-config-${envName}`,
      secretStringValue: cdk.SecretValue.unsafePlainText(JSON.stringify(runtimeConfig)),
    });

    // ---- Task Sub-Agent Runtime ----
    const taskAgentRuntime = new agentcore.Runtime(this, "TaskAgentRuntime", {
      runtimeName: `${projectName.replace(/-/g, "_")}_task_agent`,
      agentRuntimeArtifact: agentcore.AgentRuntimeArtifact.fromS3(
        {
          bucketName: agentCodeBucket.bucketName,
          objectKey: zipObjectKey,
        },
        agentcore.AgentCoreRuntime.PYTHON_3_13,
        ["opentelemetry-instrument", "task_agent.py"],
      ),
      protocolConfiguration: agentcore.ProtocolType.HTTP,
      networkConfiguration: agentcore.RuntimeNetworkConfiguration.usingPublicNetwork(),
      // Async plan execution (coordinator loop) can take 15+ minutes for
      // multi-step media pipelines. The default idle session timeout is
      // too short — extend it to match the Main Agent's configuration.
      lifecycleConfiguration: {
        idleRuntimeSessionTimeout: cdk.Duration.minutes(15),
        maxLifetime: cdk.Duration.hours(8),
      },
      environmentVariables: {
        AWS_REGION: this.region,
        DEPLOY_ENV: envName,
        BYPASS_TOOL_CONSENT: "true",
        // RENDER_AGENT_RUNTIME_ARN (render-on-results hook) is deliberately NOT here: it is an SSM
        // parameter under RUNTIME_ARN_PARAM_PATH, hydrated at startup and re-read lazily at call
        // time (agent/src/runtime_arns.py), so this runtime no longer waits for render to update.
        // All static config (buckets, tables, gateways, delegation signing key, async-plan + M2M
        // creds, memory, code-interpreter, project) is fetched at startup from RuntimeConfigSecret
        // via runtime_config_bootstrap.py (os.environ.setdefault) — keeps this env under V2's
        // 1536-byte cap and takes the two Cognito client secrets out of plaintext env.
        RUNTIME_CONFIG_SECRET_ARN: runtimeConfigSecret.secretArn,
      },
    });
    taskAgentRuntime.node.addDependency(zipPackager);
    // Read the externalized runtime config at startup (secretsmanager:GetSecretValue + kms:Decrypt).
    runtimeConfigSecret.grantRead(taskAgentRuntime);
    taskAgentRuntime.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
        resources: [
          "arn:aws:bedrock:*::foundation-model/*",
          `arn:aws:bedrock:*:${this.account}:inference-profile/*`,
        ],
      })
    );
    // Bucket-wide read+write, DELIBERATELY left that way (asset-lineage Phase 3 narrowed the sandbox,
    // the aws-mcp proxy and the planning runtime instead). The Task Agent hosts the Lab
    // service and the full tool set, so it legitimately writes `users/<actor>/{drafts,published}/*`
    // (every Lab CRUD), `artifacts/*` (the `artifact` tool, renders, imports), `projects/*` and
    // `plans/*`. Narrowing it would be a prefix list that is the whole bucket minus `skills/`, which
    // buys nothing and breaks the next writer someone adds. Per-actor scoping here needs AgentCore
    // runtime session tagging (deferred — see the LabRole note at the foot of this file), not a
    // prefix: application checks remain the authoritative gate.
    skillsBucket.grantReadWrite(taskAgentRuntime);
    uploadsBucket.grantReadWrite(taskAgentRuntime);
    // Durable conversation memory (MEMORY_ID is bound above at line ~1700). The
    // Task Agent binds AgentCoreMemorySessionManager for EXTERNAL invocations
    // (caller-scoped EXT# session) so follow-up prompts preserve context. Without
    // these event actions, ListEvents/CreateEvent 403 and the turn silently
    // degrades to stateless — the same grant the Main Agent + proxy already have.
    taskAgentRuntime.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "MemoryReadWrite",
        actions: [
          "bedrock-agentcore:CreateEvent",
          "bedrock-agentcore:ListEvents",
          "bedrock-agentcore:GetEvent",
          "bedrock-agentcore:ListSessions",
        ],
        resources: [
          conversationMemory.attrMemoryArn,
          `${conversationMemory.attrMemoryArn}/*`,
        ],
      })
    );
    // Invoke the Render Agent for the deterministic render-on-results hook. Scoped by runtime NAME
    // (AgentCore ARNs are `runtime/<name>-<suffix>`), not `renderAgentRuntime.agentRuntimeArn`: this
    // statement lands in the DefaultPolicy the task runtime DependsOn, so a GetAtt here would make
    // task wait for render. `-*` keeps it to this one runtime name; `/*` covers its endpoints.
    taskAgentRuntime.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["bedrock-agentcore:InvokeAgentRuntime"],
        resources: [
          `arn:aws:bedrock-agentcore:${this.region}:${this.account}:runtime/${renderAgentRuntimeName}-*`,
          `arn:aws:bedrock-agentcore:${this.region}:${this.account}:runtime/${renderAgentRuntimeName}-*/*`,
        ],
      })
    );
    // Read the inter-runtime ARN parameters (RENDER_AGENT_RUNTIME_ARN).
    for (const s of runtimeArnParamReadStatements()) taskAgentRuntime.addToRolePolicy(s);
    // async-job-tracking: Task Agent's interceptor needs the same DDB
    // write grant as the Main Agent. `register()` uses conditional
    // PutItem; the read side covers the results-tool status gate.
    jobRegistryTable.grantReadWriteData(taskAgentRuntime);
    planStateTable.grantReadWriteData(taskAgentRuntime);
    codeInterpreter.grantUse(taskAgentRuntime);

    // Gateway registry: read-only on the table (the Task Agent never
    // registers/updates gateways — only the Main Agent's admin actions
    // do that) and read-only on the Secrets Manager prefix that
    // `MCPManager.load_gateways_from_registry` resolves OAuth client
    // secrets from. Wildcard region/account match the Main Agent's
    // grants — gateway secrets are addressed by path, not partition.
    gatewayRegistryTable.grantReadData(taskAgentRuntime);
    // Wave 4: the Task Agent invokes shared tools on the AWS_IAM Platform
    // Gateway (scoped to its ARN, least privilege).
    taskAgentRuntime.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["bedrock-agentcore:InvokeGateway"],
        resources: [platformGateway.attrGatewayArn],
      }),
    );
    taskAgentRuntime.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["secretsmanager:GetSecretValue"],
        resources: [
          `arn:aws:secretsmanager:*:*:secret:/${projectName}/gateway/*`,
        ],
      })
    );

    // Reconcile-on-adopt (stuck-processing fix): when a plan node adopts an
    // async SFN execution, _execute_plan_node reads the execution's CURRENT
    // status so an already-terminal job (dedup to a prior run, or one that
    // finished before the first poll) goes straight to results_ready/failed
    // instead of hanging in "processing". Read-only, execution-scoped,
    // cross-region — same grant the polling worker and Plan Chainer carry.
    taskAgentRuntime.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["states:DescribeExecution"],
        resources: ["arn:aws:states:*:*:execution:*:*"],
      })
    );

    // Federation: the M2C media-workflow-gateway credentials live outside the
    // ${projectName}/gateway/* path (they belong to the M2C stack), and the
    // task agent is where the media gateway tools are loaded — so grant it
    // explicit read on that secret when this env federates.
    if (props.envConfig?.media?.credentialsSecretArn) {
      taskAgentRuntime.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "MediaGatewaySecretRead",
          actions: ["secretsmanager:GetSecretValue"],
          resources: [props.envConfig.media.credentialsSecretArn],
        })
      );
    }

    // Federation: the OMS Core M2M client secret lives cross-account /
    // cross-region in the OMS account (us-east-1); the OMS side attaches a
    // Secrets Manager resource policy allowing this account to read it. The
    // task agent loads the oms-core gateway tools, so grant it explicit read
    // on that secret when this env federates to OMS.
    if (props.envConfig?.oms?.credentialsSecretArn) {
      taskAgentRuntime.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "OmsCoreGatewaySecretRead",
          actions: ["secretsmanager:GetSecretValue"],
          resources: [props.envConfig.oms.credentialsSecretArn],
        })
      );
    }
    // Cross-account KMS decrypt for the OMS client secret (see the identical
    // grant on the main agent role): the caller needs kms:Decrypt on the OMS
    // CMK ARN for cross-account GetSecretValue to succeed at decrypt.
    if (props.envConfig?.oms?.kmsKeyArn) {
      taskAgentRuntime.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "OmsCoreGatewaySecretKmsDecrypt",
          actions: ["kms:Decrypt", "kms:DescribeKey"],
          resources: [props.envConfig.oms.kmsKeyArn],
        })
      );
    }

    // Harness gateway seed. load_gateways_from_registry is the ONLY loader the
    // agent has, so a gateway absent from this table is a gateway the agent
    // never opens a session to. Without this row, user-registered MCP servers
    // are created as targets on a gateway the agent has never heard of -- they
    // exist, the ownership record exists, and the agent still reports it has no
    // such tools, which is exactly what happened on the first live test.
    //
    // auth_type "user_jwt" is the variant that carries no stored credential:
    // the agent presents the CALLING USER's forwarded token, which is what lets
    // this gateway's authorizer validate a real identity and its interceptor
    // scope tools to a verified `sub`. Seeded rather than user-created because
    // it is platform configuration, not someone's server.
    //
    // Mirrors the OMS seed below: additive, one row, onUpdate identical to
    // onCreate so redeploys refresh it, and an admin can still disable it at
    // runtime through the registry admin path.
    const harnessSeedAction = {
      service: "DynamoDB",
      action: "putItem",
      parameters: {
        TableName: gatewayRegistryTable.tableName,
        Item: {
          PK: { S: "GATEWAY#REGISTERED" },
          SK: { S: "harness-user-mcp" },
          id: { S: "harness-user-mcp" },
          name: { S: "Your MCP servers" },
          description: {
            S: "MCP servers registered by users, scoped per owner by the gateway interceptor.",
          },
          url: { S: gateway.attrGatewayUrl },
          auth_type: { S: "user_jwt" },
          auth_config: { M: { auth_type: { S: "user_jwt" } } },
          enabled: { BOOL: true },
          created_by: { S: "infra-seed" },
          // Job ROLE declarations for servers that follow neither the
          // start_*/*_status/*_results naming convention nor MCP's _meta
          // job_role annotation. fal's submit_job / check_job / get_job_result
          // are a complete async trio the convention cannot see, so its jobs
          // were never registered, polled, or notified -- unlike every other
          // async job. The Job_Detector already treats _meta.job_role as
          // authoritative, so this feeds the SAME mechanism an annotating
          // server would use; nothing downstream is taught that fal exists.
          // A server-supplied annotation still wins over these.
          job_role_overrides: {
            M: {
              submit_job: {
                M: { job_role: { S: "start" }, job_group: { S: "fal|job" } },
              },
              check_job: {
                M: { job_role: { S: "status" }, job_group: { S: "fal|job" } },
              },
              get_job_result: {
                M: { job_role: { S: "results" }, job_group: { S: "fal|job" } },
              },
            },
          },
        },
      },
      physicalResourceId: cr.PhysicalResourceId.of("gateway-harness-user-mcp"),
    };
    const harnessSeed = new cr.AwsCustomResource(this, "SeedHarnessUserMcpGateway", {
      installLatestAwsSdk: true,
      onCreate: harnessSeedAction,
      onUpdate: harnessSeedAction,
      policy: cr.AwsCustomResourcePolicy.fromSdkCalls({
        resources: [gatewayRegistryTable.tableArn],
      }),
    });
    harnessSeed.node.addDependency(gatewayRegistryTable);
    harnessSeed.node.addDependency(gateway);

    // Federation seed: when this env federates to OMS Core, seed a single
    // runtime-managed registry row (`GATEWAY#REGISTERED` / `oms-core`) at deploy
    // time via a DynamoDB putItem custom resource. This is an additive,
    // env-gated infra seed of one row (only for envs that set `oms`); admins
    // can still edit/disable it at runtime through the gateway-registry admin
    // path. onUpdate mirrors onCreate so redeploys refresh the row.
    if (props.envConfig?.oms?.endpointUrl) {
      const oms = props.envConfig.oms;
      const omsSeedAction = {
        service: "DynamoDB",
        action: "putItem",
        parameters: {
          TableName: gatewayRegistryTable.tableName,
          Item: {
            PK: { S: "GATEWAY#REGISTERED" },
            SK: { S: "oms-core" },
            id: { S: "oms-core" },
            name: { S: "OMS Core (hosted MCP endpoint)" },
            description: {
              S: "OMS Core federation tools: list_integrations, get_integration, get_action_schema, validate_template, save_template, activate_template.",
            },
            url: { S: oms.endpointUrl! },
            auth_type: { S: "jwt" },
            auth_config: {
              M: {
                auth_type: { S: "jwt" },
                token_endpoint: { S: oms.tokenEndpoint! },
                client_id: { S: oms.clientId! },
                client_secret_arn: { S: oms.credentialsSecretArn! },
                scope: { S: oms.scope! },
              },
            },
            enabled: { BOOL: true },
            created_by: { S: "infra-seed" },
          },
        },
        physicalResourceId: cr.PhysicalResourceId.of("gateway-oms-core"),
      };
      const seed = new cr.AwsCustomResource(this, "SeedOmsCoreGateway", {
        installLatestAwsSdk: true,
        onCreate: omsSeedAction,
        onUpdate: omsSeedAction, // identical to onCreate so redeploys refresh the row
        policy: cr.AwsCustomResourcePolicy.fromSdkCalls({
          resources: [gatewayRegistryTable.tableArn],
        }),
      });
      seed.node.addDependency(gatewayRegistryTable);
    }

    // Media gateway seed: the agent only federates to a gateway it finds as a
    // GATEWAY#REGISTERED registry row. The toolkit's media-workflow-gateway is deployed
    // and owned independently (see env-config.ts's `media` block); every OAuth field
    // there is a plain literal string, so this is a direct DynamoDB putItem, same shape
    // as the OMS seed above. Without it, media tools never load → no dispatch → nothing
    // folds into the timeline.
    const media = props.envConfig?.media;
    if (media?.gatewayUrl && media.clientId && media.tokenEndpoint && media.credentialsSecretArn) {
      addMediaGatewaySeed(this, "SeedMediaWorkflowGateway", {
        registryTable: gatewayRegistryTable,
        gatewayUrl: media.gatewayUrl,
        clientId: media.clientId,
        tokenEndpoint: media.tokenEndpoint,
        credentialsSecretArn: media.credentialsSecretArn,
        scope: media.scope,
      });
    }

    // Read the M2C proxy/output bucket so the render pipeline (dispatch_render)
    // can copy media-workflow outputs into the CloudFront-backed artifacts
    // bucket for rendering.
    if (props.envConfig?.media?.proxyBucket) {
      // Every runtime that can DISPATCH a render needs this, not just the task
      // agent. dispatch_render turns s3:// artifacts into presigned GET urls, and a
      // presigned url carries only the SIGNER's authority -- so signing with a role
      // that cannot read the bucket yields a url that is well-formed, unexpired and
      // permanently 403. The browser then gets XML instead of video and shows a
      // black player stuck at 0:00.
      //
      // The render hook fires wherever the tool result lands, and a plan-results
      // retrieval lands on the MAIN agent. Its role had skills-store, uploads and
      // agent-code but not the M2C proxy bucket, so every analysis view that
      // referenced the proxy was dead on arrival. Granting the same read to the
      // runtimes that render keeps the presign approach (chosen to avoid a
      // 200-500ms copy per file) instead of trading it for mirroring.
      for (const runtime of [taskAgentRuntime, renderAgentRuntime]) {
        runtime.addToRolePolicy(
          new iam.PolicyStatement({
            sid: "MediaWorkflowOutputRead",
            actions: ["s3:GetObject", "s3:ListBucket"],
            resources: [
              `arn:aws:s3:::${props.envConfig.media.proxyBucket}`,
              `arn:aws:s3:::${props.envConfig.media.proxyBucket}/*`,
            ],
          })
        );
      }
    }

    // ---- Plan Chainer Lambda (Pattern D reactive orchestration) ----
    // Triggered by DDB Streams on the Plan State table. Reads the plan DAG,
    // finds nodes whose dependencies are satisfied, dispatches them to the
    // Task Agent (fire & forget), and marks the parent run complete when all
    // nodes are terminal.
    const planChainerLambda = new lambda.Function(this, "PlanChainerLambda", {
      functionName: `${projectName}-plan-chainer`,
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: "handler.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "lambdas/plan-chainer")),
      layers: [resultResolutionLayer],
      timeout: cdk.Duration.seconds(60),
      memorySize: 256,
      environment: {
        PLAN_STATE_TABLE: planStateTable.tableName,
        JOB_REGISTRY_TABLE: jobRegistryTable.tableName,
        TASK_AGENT_RUNTIME_ARN: taskAgentRuntime.agentRuntimeArn,
        SKILLS_S3_BUCKET: skillsBucket.bucketName,
        GATEWAY_REGISTRY_TABLE: gatewayRegistryTable.tableName,
        // The chainer auto-imports plan DELIVERABLES (M2C proxy-bucket result files) into the
        // CloudFront-backed artifacts bucket at settle, so get_plan_results returns downloadable
        // https urls instead of un-fetchable s3:// refs. Needs the CDN domain to form those urls.
        ARTIFACTS_DOMAIN: artifactsDistribution.distributionDomainName,
        // The results fetch reaches a user_jwt gateway as a MACHINE and proves whose
        // work it is with the plan's signed delegation grant, exactly as the polling
        // worker does. Without these the fetch failed with "unsupported gateway
        // auth_type 'user_jwt'" and the node stayed in results_ready forever.
        ASYNC_PLAN_TOKEN_ENDPOINT: asyncPlanTokenEndpoint,
        ASYNC_PLAN_CLIENT_ID: asyncPlanClient.userPoolClientId,
        ASYNC_PLAN_CLIENT_SECRET: asyncPlanClient.userPoolClientSecret.unsafeUnwrap(),
        ASYNC_PLAN_SCOPE: asyncPlanScope,
        DYNAMIC_FANOUT_ENABLED: dynamicFanoutFlag,
        // The watchdog finds a job a RUNNING node registered mid-turn through this GSI
        // (actorId, updatedAt), so a node waiting on its own fal/SFN job is never reaped.
        JOB_REGISTRY_ACTOR_INDEX: jobRegistryActorStatusIndexName,
        // asset-lineage Phase 3 (a): plan deliverables land under the plan's producing project, and
        // each node dispatch forwards that project so the node's own writes do too.
        ...(sessionOutputLayout === "project" ? { SESSION_OUTPUT_LAYOUT: "project" } : {}),
        // projects.lineageEdges: each node's job row records what it was handed (`inputRefs`) and what it
        // made (`outputs`), which Generated_Media turns into lineage edges. Absent (off) everywhere else.
        ...(props.envConfig?.projects?.lineageEdges ? { LINEAGE_EDGES: "true" } : {}),
      },
    });

    // Grants: read/write plan state, read/write job registry, invoke Task Agent
    planStateTable.grantReadWriteData(planChainerLambda);
    jobRegistryTable.grantReadWriteData(planChainerLambda);
    skillsBucket.grantReadWrite(planChainerLambda); // Read deps + write plan manifest
    gatewayRegistryTable.grantReadData(planChainerLambda); // Direct results-tool calls (no LLM)

    // Read the M2C proxy/output bucket so _fetch_text_artifact
    // (handler.py) can inline the actual transcript/text content a
    // gateway tool wrote there — not just its s3uri — into a downstream
    // text-synthesis node's prompt. Without this grant, dependency
    // injection for any node whose upstream output lives in this bucket
    // (diarization transcripts, stem-separation metrics, etc.) silently
    // falls back to pointer-only injection: the fetch raises AccessDenied,
    // _fetch_text_artifact catches it and returns None, and the downstream
    // node is left with an unreadable s3uri — exactly the t3_translate
    // no-op-disguised-as-SUCCEEDED behavior this fix targets. Mirrors the
    // MediaWorkflowOutputRead grant already on taskAgentRuntime above.
    if (props.envConfig?.media?.proxyBucket) {
      planChainerLambda.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "MediaWorkflowOutputReadForInjection",
          // GetObject fetches a named transcript file; ListBucket lets the chainer DISCOVER the
          // transcript under an async job's output PREFIX. Diarization (and other media jobs)
          // report only the output prefix, not the filename (the transcript lives at
          // <prefix>/transcripts/final_transcript.json). Without ListBucket,
          // _list_prefix_inlinable_keys raises AccessDenied, inlining falls back to the bare
          // pointer, and the downstream translate node is left with an unreadable s3uri — the
          // t3_translate no-op-disguised-as-SUCCEEDED this targets (plan 87aef838, verified: the
          // chainer logged "Failed to list prefix ... for inlining"). Both the bucket (for List)
          // and its objects (for Get) are in scope; mismatched action/resource pairs are no-ops.
          actions: ["s3:GetObject", "s3:ListBucket"],
          resources: [
            `arn:aws:s3:::${props.envConfig.media.proxyBucket}`,
            `arn:aws:s3:::${props.envConfig.media.proxyBucket}/*`,
          ],
        })
      );
    }

    planChainerLambda.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["secretsmanager:GetSecretValue"],
        resources: [
          `arn:aws:secretsmanager:*:*:secret:/${projectName}/gateway/*`,
        ],
      }),
    );
    // Same M2C credentials, for the same reason: the chainer folds the status tool's declared
    // outputs into a node whose SFN execution output is only {uuid, total_estimated_cost}.
    if (props.envConfig?.media?.credentialsSecretArn) {
      planChainerLambda.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "MediaGatewaySecretRead",
          actions: ["secretsmanager:GetSecretValue"],
          resources: [props.envConfig.media.credentialsSecretArn],
        }),
      );
    }
    planChainerLambda.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["bedrock-agentcore:InvokeAgentRuntime"],
        resources: [
          taskAgentRuntime.agentRuntimeArn,
          `${taskAgentRuntime.agentRuntimeArn}/*`,
        ],
      }),
    );
    // The M2C workflows are all "shared-status" trios (a submit tool + a
    // shared get_job_status, with NO dedicated *_results tool), so a
    // completed node's result IS its Step Functions execution output. The
    // chainer reads it via DescribeExecution in _dispatch_sfn_output_as_result;
    // without this grant that call is AccessDenied, the node never leaves
    // results_ready, and the entire fan-out stalls after the first node.
    // Read-only, execution-scoped, cross-region — identical to the grant the
    // polling worker already carries (both call DescribeExecution on the same
    // executions), so the two stay in lock-step.
    planChainerLambda.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["states:DescribeExecution"],
        resources: ["arn:aws:states:*:*:execution:*:*"],
      }),
    );

    // Wire DDB Stream from Plan State table to the Plan Chainer Lambda
    planChainerLambda.addEventSource(
      new lambdaEventSources.DynamoEventSource(planStateTable, {
        startingPosition: lambda.StartingPosition.LATEST,
        batchSize: 10,
        maxBatchingWindow: cdk.Duration.seconds(5),
        retryAttempts: 3,
      }),
    );

    // The plan watchdog, once a minute. The stall check used to run only when a stream event
    // happened to fire the chainer, so a node that died with its microVM and never wrote a
    // terminal status (plan 81d9198e) was noticed 3h18m later, by an unrelated event. The sweep
    // reads ONE small partition of the plan-state table (WATCH#plans, a row per in-flight plan),
    // reaps RUNNING nodes quiet for > PLAN_NODE_STALL_SECONDS (900s = the node's 600s exec
    // timeout + margin), and does nothing else: the reaped node's failed write is a stream record,
    // and that invocation runs the ordinary retry/skip/settle pass.
    const planWatchdogSchedule = new events.Rule(this, "PlanChainerWatchdogSchedule", {
      ruleName: `${projectName}-plan-watchdog-schedule`,
      description: "Fires the Plan Chainer's stalled-node watchdog sweep every minute",
      schedule: events.Schedule.rate(cdk.Duration.minutes(1)),
    });
    planWatchdogSchedule.addTarget(new eventsTargets.LambdaFunction(planChainerLambda, {
      event: events.RuleTargetInput.fromObject({ watchdog: true }),
      retryAttempts: 0, // the next minute's sweep is the retry
    }));

    // ---- Planning Agent Runtime ----
    const planningAgentRuntime = new agentcore.Runtime(this, "PlanningAgentRuntime", {
      runtimeName: `${projectName.replace(/-/g, "_")}_planning_agent`,
      agentRuntimeArtifact: agentcore.AgentRuntimeArtifact.fromS3(
        {
          bucketName: agentCodeBucket.bucketName,
          objectKey: zipObjectKey,
        },
        agentcore.AgentCoreRuntime.PYTHON_3_13,
        ["opentelemetry-instrument", "planning_agent.py"],
      ),
      protocolConfiguration: agentcore.ProtocolType.HTTP,
      networkConfiguration: agentcore.RuntimeNetworkConfiguration.usingPublicNetwork(),
      environmentVariables: {
        AWS_REGION: this.region,
        SKILLS_S3_BUCKET: skillsBucket.bucketName,
        PLAN_STATE_TABLE: planStateTable.tableName,
        DYNAMIC_FANOUT_ENABLED: dynamicFanoutFlag,
      },
    });
    planningAgentRuntime.node.addDependency(zipPackager);
    planningAgentRuntime.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
        resources: [
          "arn:aws:bedrock:*::foundation-model/*",
          `arn:aws:bedrock:*:${this.account}:inference-profile/*`,
        ],
      })
    );
    // The Planning Agent runs with NO tools and writes exactly one object per invocation:
    // `plans/<session>/planning/output.json` (`planning_agent.py`). So it gets read on the bucket
    // (skills/config loads) and put ONLY under `plans/*` — not bucket-wide write, which let a
    // tool-less planner overwrite any artifact or Lab asset (asset-lineage Phase 3, least privilege).
    skillsBucket.grantRead(planningAgentRuntime);
    skillsBucket.grantPut(planningAgentRuntime, "plans/*");
    planStateTable.grantReadWriteData(planningAgentRuntime);

    // ---- Render Agent Runtime ----
    //
    // Dedicated runtime for artifact rendering (artifact-render-agent spec,
    // Requirements 7.1–7.3). The Render Agent analyzes media/document/code/
    // data artifacts and produces validated Render_Manifest JSON documents.
    // It uses a skill-based dispatch pattern: the appropriate render skill
    // is selected based on artifact MIME type, and the manifest is written
    // to S3 under the `artifacts/` prefix.
    //
    // Separate from the Task Agent for isolation (CPU-intensive media
    // analysis), specialized tooling (media_probe, peak_extractor,
    // caption_parser), and independent scaling of bursty render workloads.

    // ---- Main Agent Runtime ----
    //
    // The Main Agent absorbs Lab CRUD capability — the 20 Lab actions are
    // exposed as Strands tools and structured `{action: ...}` payloads are
    // dispatched directly through `LabService.handle` (bypass route used by
    // the Lab UI). The previously-separate `LabServiceRuntime` is gone; the
    // Main Agent owns the Asset_Index table and Lab artifacts bucket.
    //
    // Authorizer switch (user-auth task 7.1, Requirements R4 + R5):
    // `authorizerConfiguration` updates in place on AgentCore Runtime — the
    // `agentRuntimeArn` / `agentRuntimeId` are stable across an IAM ↔ JWT
    // switch. No CFN replacement, no maintenance window. Verified empirically:
    // flipping from default IAM to JWT against the user pool kept the ARN.
    //
    // The actor-id fallback chain in `agent/src/main.py::_resolve_actor_id`
    // (JWT `sub` → `X-Actor-Id` header → env) kept stage-1 traffic working
    // until task 10 removed the env fallback. See user-auth `requirements.md` R5.
    const agentRuntime = new agentcore.Runtime(this, "AgentRuntime", {
      runtimeName: `${projectName.replace(/-/g, "_")}_main_agent`,
      agentRuntimeArtifact: agentcore.AgentRuntimeArtifact.fromS3(
        {
          bucketName: agentCodeBucket.bucketName,
          objectKey: zipObjectKey,
        },
        agentcore.AgentCoreRuntime.PYTHON_3_13,
        ["opentelemetry-instrument", "main.py"],
      ),
      protocolConfiguration: agentcore.ProtocolType.HTTP,
      networkConfiguration: agentcore.RuntimeNetworkConfiguration.usingPublicNetwork(),
      // Switch from default IAM auth to JWT against the human-auth user
      // pool created above. The Cognito user pool client ID is registered
      // as the allowed audience — only access tokens minted for the Lab UI
      // client will be accepted. Triggers a CFN replacement on this
      // runtime; see the warning block above.
      // NOTE: authorizerConfiguration commented out for dev — enables SigV4
      // auth from the local proxy. Re-enable for production with real users.
      // authorizerConfiguration: agentcore.RuntimeAuthorizerConfiguration.usingJWT(
      //   `${userPoolIssuerUrl}/.well-known/openid-configuration`,
      //   [userPoolClient.userPoolClientId],
      // ),
      lifecycleConfiguration: {
        idleRuntimeSessionTimeout: cdk.Duration.minutes(15),
        maxLifetime: cdk.Duration.hours(8),
      },
      // Forward the end user's Cognito token into the runtime so it can
      // present it as inbound auth to the harness gateway, whose authorizer is
      // the user pool. Without this the runtime has an actor *id* (stamped in
      // the request body) but no user *token*, and the gateway cannot verify
      // who is calling.
      //
      // A custom header rather than `Authorization`: forwarding
      // `Authorization` requires the runtime itself to use a
      // customJWTAuthorizer, and this runtime deliberately stays on SigV4 so
      // the proxy can sign requests (see the commented-out
      // authorizerConfiguration above). Headers must be allowlisted here or
      // they are silently dropped.
      requestHeaderConfiguration: {
        // USER_TOKEN_HEADER forwards the Cognito token to the harness gateway. The ProjectToken
        // header carries the per-turn Project_Token the proxy minted (media-harness-projects-access,
        // Component 2 step 5, D6); the Main_Agent reads it next to the user token in main.py.
        allowlistedHeaders: [
          USER_TOKEN_HEADER,
          "X-Amzn-Bedrock-AgentCore-Runtime-Custom-ProjectToken",
        ],
      },
      environmentVariables: {
        AWS_REGION: this.region,
        DEPLOY_ENV: envName,
        BYPASS_TOOL_CONSENT: "true",
        // The sub-agent ARNs (TASK_/PLANNING_/RENDER_AGENT_RUNTIME_ARN) are deliberately
        // NOT here. As env they were GetAtts that made this runtime wait for all three sub-runtimes
        // to update first; they are SSM parameters under RUNTIME_ARN_PARAM_PATH instead (written
        // after the runtimes, below), hydrated by runtime_config_bootstrap.py at startup and
        // re-read lazily at call time by agent/src/runtime_arns.py.
        // All other static config (buckets, tables, gateways incl. HARNESS_GATEWAY_ID /
        // GATEWAY_ENDPOINTS, delegation signing key, async-plan + M2M creds, memory,
        // code-interpreter, model, media-proxy bucket, project) is fetched at startup from
        // RuntimeConfigSecret via runtime_config_bootstrap.py (os.environ.setdefault) — keeps this
        // env under V2's 1536-byte cap and removes the two Cognito client secrets from plaintext env.
        RUNTIME_CONFIG_SECRET_ARN: runtimeConfigSecret.secretArn,
      },
    });

    // Mount session storage at /mnt/workspace for the Lab test harness — the
    // harness writes intermediate run artifacts here. Inherited from the
    // retired Lab_Service runtime.
    const cfnAgentRuntime = agentRuntime.node.defaultChild as cdk.CfnResource;
    cfnAgentRuntime.addPropertyOverride("FilesystemConfigurations", [
      { SessionStorage: { MountPath: "/mnt/workspace" } },
    ]);

    agentRuntime.node.addDependency(zipPackager);
    // Read the externalized runtime config at startup (secretsmanager:GetSecretValue + kms:Decrypt).
    runtimeConfigSecret.grantRead(agentRuntime);
    // Read the inter-runtime ARN parameters (all sub-agents).
    for (const s of runtimeArnParamReadStatements()) agentRuntime.addToRolePolicy(s);

    // ---- Runtime ARN parameters ----
    // One SSM parameter per sub-runtime ARN, named for the env var the agent reads
    // (`<path>/TASK_AGENT_RUNTIME_ARN`, ...). These depend on the runtimes; NO runtime depends on
    // them (readers get only the string path via RuntimeConfigSecret and a string-ARN grant), so
    // all sub-runtimes update in parallel instead of render → task → main. Consequence: on a
    // brand-new environment a runtime can start before these exist, so the agent resolves them
    // lazily at call time as well as at startup (agent/src/runtime_arns.py).
    // `infra/test/runtime-parallel.synth.test.ts` fails if a runtime → runtime edge comes back.
    const runtimeArnParams: Array<[string, agentcore.Runtime]> = [
      // The main runtime's ARN is for the UI proxy (no runtime reads it): the proxy task definition
      // must not GetAtt a runtime, or ECS waits for the runtime updates (docs/ci-speed-proposal.md P1).
      ["AGENT_RUNTIME_ARN", agentRuntime],
      ["TASK_AGENT_RUNTIME_ARN", taskAgentRuntime],
      ["PLANNING_AGENT_RUNTIME_ARN", planningAgentRuntime],
      ["RENDER_AGENT_RUNTIME_ARN", renderAgentRuntime],
    ];
    for (const [envVarName, rt] of runtimeArnParams) {
      new ssm.StringParameter(this, `RuntimeArnParam${envVarName}`, {
        parameterName: `${runtimeArnParamPath}/${envVarName}`,
        stringValue: rt.agentRuntimeArn,
        description: `AgentCore runtime ARN read by the agent as ${envVarName}`,
      });
    }

    // AgentCore Runtime V2 (snapshot-restore) on every runtime — DECLARED, not inherited. V2 caps
    // the env-var payload at 1536 bytes; the first fan-out failed on task/main for exactly that
    // reason. Their bulk config now comes from RuntimeConfigSecret (see above), so every env fits
    // (main ~741B, task ~348B). Measured in staging: platform cold start main V1 ≈8.9s vs task V2
    // ≈0.9s, planning V1 6.5–7.8s → V2 ≈2.0s. Declared explicitly because task/planning/mcp
    // were left on V2 by the failed fan-out while the template said nothing — drift a fresh env
    // (prod) would not reproduce. PlatformVersion is a non-replacement property: in-place, ARNs
    // preserved, reversible. Render carries its own override above.
    for (const rt of [mcpRuntime, taskAgentRuntime, planningAgentRuntime, agentRuntime]) {
      (rt.node.defaultChild as cdk.CfnResource).addPropertyOverride("PlatformVersion", "V2");
    }

    // Sign-only grant for delegation grants (option E), scoped to the single
    // grant key. The Main Agent (orchestrator) is the ONLY signer; it never
    // gets kms:Verify and no other role gets kms:Sign on this key.
    delegationGrantKey.grant(agentRuntime, "kms:Sign");
    // The Main_Agent is the ONLY caller of TokenRefreshFn (Req 4.27). Identity policy here; the
    // function's resource policy (added in the nested stack) names this role's ARN.
    if (projectsStack) {
      agentRuntime.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "InvokeProjectTokenRefresh",
          actions: ["lambda:InvokeFunction"],
          resources: [projectsStack.tokenRefreshFn.functionArn],
        }),
      );
    }
    // The Main_Agent invokes Asset_Service for the Project_Tools (design §17); it is the only runtime
    // caller. Identity grant here; the function's resource policy (nested stack) names this role's ARN.
    if (ingestionStack) {
      agentRuntime.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "InvokeAssetService",
          actions: ["lambda:InvokeFunction"],
          resources: [`arn:aws:lambda:${this.region}:${this.account}:function:${assetServiceFunctionName(projectName)}`],
        }),
      );
    }
    // Same reason as its env above: the Task Agent mints job-scoped grants.
    delegationGrantKey.grant(taskAgentRuntime, "kms:Sign");
    // The Task Agent mints its own Project_Token (from its delegation grant) for the knowledge-graph tools.
    if (projectsStack) {
      taskAgentRuntime.addToRolePolicy(new iam.PolicyStatement({
        sid: "InvokeDelegatedProjectTokenIssuer",
        actions: ["lambda:InvokeFunction"],
        resources: [`arn:aws:lambda:${this.region}:${this.account}:function:${delegatedTokenIssuerFunctionName(projectName)}`],
      }));
    }

    // ---- Continuous online evaluation + monitoring (AgentCore Evaluations) ----
    // Continuously samples live main-agent traffic and scores it with built-in
    // evaluators (GoalSuccessRate/Helpfulness/Correctness). Results land in a
    // dedicated CloudWatch log group + EMF metrics under the
    // `Bedrock-AgentCore/Evaluations` namespace (build dashboards/alarms there).
    // Native CFN type (no L1/L2 in aws-cdk-lib yet) → CfnResource.
    // NOTE: sessions that dispatch to the render/task sub-agents return
    // LogEventMissingException unless those runtimes' log groups are in the
    // data source too; `onlineEvalExpanded` (env-config) adds them, plus a
    // second tool/skill/instruction config.
    const mainRuntimeId = agentRuntime.agentRuntimeId;

    // --- B3: cold-start ImportError alarm across all agent runtimes ----------
    // The 2026 prod outage was an unpinned-dependency ImportError at container
    // cold start (mcp SDK) that NO alarm caught — it surfaced only as failed
    // user invocations. This turns any import-time failure logged by a runtime
    // into a paged CloudWatch alarm on the existing resilience SNS topic.
    //
    // Every runtime publishes to ONE namespace+metric (no dimensions), so the
    // filters aggregate and a single alarm covers all five runtimes.
    const importErrorRuntimes: { id: string; runtime: agentcore.Runtime }[] = [
      { id: "Main", runtime: agentRuntime },
      { id: "Task", runtime: taskAgentRuntime },
      { id: "Planning", runtime: planningAgentRuntime },
      { id: "Render", runtime: renderAgentRuntime },
      { id: "Mcp", runtime: mcpRuntime },
    ];
    const IMPORT_ERROR_NS = "MediaworksHarness/Runtime";
    const IMPORT_ERROR_METRIC = "ImportErrors";
    for (const { id, runtime } of importErrorRuntimes) {
      const lg = logs.LogGroup.fromLogGroupName(
        this,
        `RuntimeLogGroup${id}`,
        `/aws/bedrock-agentcore/runtimes/${runtime.agentRuntimeId}-DEFAULT`,
      );
      new logs.MetricFilter(this, `ImportErrorFilter${id}`, {
        logGroup: lg,
        metricNamespace: IMPORT_ERROR_NS,
        metricName: IMPORT_ERROR_METRIC,
        // Unstructured-log OR match: any Python import-time failure signature.
        filterPattern: logs.FilterPattern.anyTerm(
          "ImportError",
          "ModuleNotFoundError",
          "cannot import name",
        ),
        metricValue: "1",
        defaultValue: 0,
      });
    }
    mkAlarm(
      "RuntimeImportErrorAlarm",
      new cloudwatch.Metric({
        namespace: IMPORT_ERROR_NS,
        metricName: IMPORT_ERROR_METRIC,
        period: period5,
        statistic: "Sum",
      }),
      0,
      "An agent runtime logged ImportError/ModuleNotFoundError — a cold-start dependency failure " +
        "(the 2026 outage class). Check the runtime's requirements pin (agent/src/requirements.txt) and redeploy.",
    );

    // Demonstration-only Slack ingress — ADDITIVE, created only when the env
    // config opts in (staging). Other envs omit `slack`, so this block is a
    // no-op and the synthesized stack is byte-identical to before. Uses the
    // existing Main Agent runtime; touches no existing resource.
    if (props.envConfig?.slack?.enabled) {
      new SlackIntegration(this, "SlackIntegration", {
        projectName,
        envName,
        mainAgentRuntimeArn: agentRuntime.agentRuntimeArn,
        demoActorId: props.envConfig.slack.demoActorId,
        sessionSalt: props.envConfig.slack.sessionSalt ?? envName,
        rendererBaseUrl: props.envConfig.slack.rendererBaseUrl ?? "",
        artifactsDomain: artifactsDistribution.distributionDomainName,
        // The UI distribution is created further down this constructor; resolved at synth.
        uiDomain: cdk.Lazy.string({ produce: () => uiDistribution.distributionDomainName }),
        artifactsCdnPrivate,
        maxUploadBytes: props.envConfig.slack.maxUploadBytes ?? 52428800,
        jobRegistryTable,
        artifactsBucket: skillsBucket,
        maxFiles: props.envConfig.slack.maxFiles ?? 5,
        maxFileBytes: props.envConfig.slack.maxFileBytes ?? 1073741824,
        maxAggregateBytes: props.envConfig.slack.maxAggregateBytes ?? 2147483648,
      });
    }
    // The OTel `service.name` the runtime stamps on its spans is the runtime
    // NAME + ".DEFAULT" (no generated id suffix) — e.g.
    // "mediaworks_harness_main_agent.DEFAULT" — whereas the log group carries
    // the full runtime ID ("..._main_agent-<suffix>-DEFAULT"). Online eval
    // filters sampled spans by ServiceNames, so this MUST be the runtime name,
    // not mainRuntimeId, or it matches zero spans and never scores anything.
    const mainRuntimeName = `${projectName.replace(/-/g, "_")}_main_agent`;
    const onlineEvalRole = new iam.Role(this, "OnlineEvaluationRole", {
      assumedBy: new iam.ServicePrincipal("bedrock-agentcore.amazonaws.com", {
        conditions: {
          StringEquals: {
            "aws:ResourceAccount": this.account,
            "aws:SourceAccount": this.account,
          },
          ArnLike: {
            "aws:SourceArn": [
              `arn:aws:bedrock-agentcore:${this.region}:${this.account}:evaluator/*`,
              `arn:aws:bedrock-agentcore:${this.region}:${this.account}:online-evaluation-config/*`,
            ],
          },
        },
      }),
      description: "Execution role for AgentCore continuous (online) evaluation of the main agent.",
    });
    onlineEvalRole.addToPolicy(new iam.PolicyStatement({
      sid: "CloudWatchLogRead",
      actions: [
        "logs:DescribeLogGroups", "logs:DescribeLogStreams",
        "logs:GetQueryResults", "logs:StartQuery",
        "cloudwatch:GenerateQuery", "cloudwatch:GenerateQueryResultsSummary",
      ],
      resources: ["*"],
    }));
    onlineEvalRole.addToPolicy(new iam.PolicyStatement({
      sid: "CloudWatchLogWrite",
      actions: ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:GetLogEvents"],
      resources: [`arn:aws:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/evaluations/*`],
    }));
    onlineEvalRole.addToPolicy(new iam.PolicyStatement({
      sid: "CloudWatchIndexPolicy",
      actions: ["logs:DescribeIndexPolicies", "logs:PutIndexPolicy"],
      resources: [
        `arn:aws:logs:${this.region}:${this.account}:log-group:aws/spans`,
        `arn:aws:logs:${this.region}:${this.account}:log-group:aws/spans:*`,
      ],
    }));
    onlineEvalRole.addToPolicy(new iam.PolicyStatement({
      sid: "BedrockInvoke",
      actions: ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
      resources: ["*"],
    }));

    // Continuous online evaluation. AgentCore auto-creates the runtime's log
    // group, but only once the runtime has emitted logs — so on a brand-new
    // env's FIRST deploy (runtime has never logged) the OnlineEvaluationConfig
    // create-time validation ("log group exists") can fail. dev/staging/prod
    // all have live, logging runtimes, so online-eval defaults ON for every
    // env. A genuinely fresh env whose runtime hasn't logged yet can opt out of
    // this one deploy with `-c enableOnlineEval=false`, then re-enable on the
    // next deploy once the runtime has logged at least once.
    const _oeCtx = this.node.tryGetContext("enableOnlineEval");
    const enableOnlineEval =
      _oeCtx === undefined
        ? true
        : _oeCtx === true || _oeCtx === "true";
    // `onlineEvalExpanded` (env-config; staging) widens what is scored — see its doc there.
    const onlineEvalExpanded = props.envConfig?.onlineEvalExpanded === true;
    const runtimeLogGroup = (runtimeId: string) => `/aws/bedrock-agentcore/runtimes/${runtimeId}-DEFAULT`;
    // The sub-agent groups let a main session that dispatched to Task/Render resolve the log events
    // its spans point at (LogEventMissingException otherwise); LogGroupNames allows 10.
    // `ServiceNames` stays the main runtime's ONLY: the CFN schema caps it at one item, so the
    // sub-agents' own turns are not sampled as sessions of their own.
    const onlineEvalDataSource = {
      CloudWatchLogs: {
        LogGroupNames: onlineEvalExpanded
          ? [mainRuntimeId, taskAgentRuntime.agentRuntimeId, renderAgentRuntime.agentRuntimeId].map(runtimeLogGroup)
          : [runtimeLogGroup(mainRuntimeId)],
        ServiceNames: [`${mainRuntimeName}.DEFAULT`],
      },
    };
    if (enableOnlineEval) {
    const onlineEval = new cdk.CfnResource(this, "OnlineEvaluationConfig", {
      type: "AWS::BedrockAgentCore::OnlineEvaluationConfig",
      properties: {
        OnlineEvaluationConfigName: `${projectName.replace(/-/g, "_")}_main_online_eval`,
        Description: "Continuous monitoring of the main agent (GoalSuccessRate/Helpfulness/Correctness).",
        DataSourceConfig: onlineEvalDataSource,
        EvaluationExecutionRoleArn: onlineEvalRole.roleArn,
        Evaluators: [
          { EvaluatorId: "Builtin.GoalSuccessRate" },
          { EvaluatorId: "Builtin.Helpfulness" },
          { EvaluatorId: "Builtin.Correctness" },
        ],
        Rule: { SamplingConfig: { SamplingPercentage: 100 } },
        ExecutionStatus: "ENABLED",
      },
    });
    onlineEval.node.addDependency(agentRuntime);
    // The create-time handler validates that the execution role can read the
    // log groups, so the config must be created AFTER the role's inline policy
    // is attached (referencing roleArn alone does not order after the policy).
    onlineEval.node.addDependency(onlineEvalRole);
    if (onlineEvalExpanded) onlineEval.node.addDependency(taskAgentRuntime, renderAgentRuntime);

    new cdk.CfnOutput(this, "OnlineEvaluationConfigArnOutput", {
      value: onlineEval.getAtt("OnlineEvaluationConfigArn").toString(),
      description: "AgentCore continuous online-evaluation config ARN",
    });

    // Tool/skill/instruction evaluators at 25%. ONE config holds all five: evaluation levels may
    // mix in a config (the main one already mixes SESSION and TRACE) — these are TOOL_CALL x4 +
    // TRACE x1, far under the 25-evaluator cap. Separate from the main config only for its own
    // sampling rate: a TOOL_CALL evaluator judges every tool call, so 100% would multiply judge
    // spend. Results land in `/aws/bedrock-agentcore/evaluations/results/<name>-<id>`, which the
    // Improve harvest finds by prefix (IMPROVE_EVAL_RESULTS_PREFIX).
    if (onlineEvalExpanded) {
      const componentEval = new cdk.CfnResource(this, "ComponentOnlineEvaluationConfig", {
        type: "AWS::BedrockAgentCore::OnlineEvaluationConfig",
        properties: {
          OnlineEvaluationConfigName: `${projectName.replace(/-/g, "_")}_component_online_eval`,
          Description: "Main agent tool, skill and instruction following (25% sample).",
          DataSourceConfig: onlineEvalDataSource,
          EvaluationExecutionRoleArn: onlineEvalRole.roleArn,
          Evaluators: [
            { EvaluatorId: "Builtin.ToolSelectionAccuracy" },
            { EvaluatorId: "Builtin.ToolParameterAccuracy" },
            { EvaluatorId: "Builtin.SkillSelectionAccuracy" },
            { EvaluatorId: "Builtin.SkillInstructionFollowing" },
            { EvaluatorId: "Builtin.InstructionFollowing" },
          ],
          Rule: { SamplingConfig: { SamplingPercentage: 25 } },
          ExecutionStatus: "ENABLED",
        },
      });
      componentEval.node.addDependency(agentRuntime, taskAgentRuntime, renderAgentRuntime, onlineEvalRole);
    }
    }

    // Grant Bedrock model access
    // The MAIN agent renders too, and needs the same M2C proxy read as the task and
    // render agents. dispatch_render presigns s3:// artifacts, and a presigned url
    // carries only the SIGNER's authority -- so signing with a role that cannot read
    // the bucket produces a url that is well-formed, unexpired and permanently 403.
    // A plan-results retrieval lands on THIS runtime, so every analysis view that
    // referenced the proxy bucket rendered a black player stuck at 0:00 while the
    // task agent's identical renders worked.
    if (props.envConfig?.media?.proxyBucket) {
      agentRuntime.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "MediaWorkflowOutputRead",
          actions: ["s3:GetObject", "s3:ListBucket"],
          resources: [
            `arn:aws:s3:::${props.envConfig.media.proxyBucket}`,
            `arn:aws:s3:::${props.envConfig.media.proxyBucket}/*`,
          ],
        })
      );
    }

    agentRuntime.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:ListInferenceProfiles"],
        resources: ["*"],
      })
    );

    // Federation: when this env federates to the M2C media-workflow-gateway,
    // the main agent resolves that gateway's OAuth client secret from Secrets
    // Manager at connect time (see mcp_manager._fetch_secret_string, which
    // tolerates the M2C JSON credential bundle). Grant read on that secret.
    if (props.envConfig?.media?.credentialsSecretArn) {
      agentRuntime.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "MediaGatewaySecretRead",
          actions: ["secretsmanager:GetSecretValue"],
          resources: [props.envConfig.media.credentialsSecretArn],
        })
      );
    }

    // Federation: when this env federates to the OMS Core hosted MCP endpoint,
    // the main agent resolves the OMS M2M client secret from Secrets Manager at
    // connect time. That secret lives cross-account/cross-region in the OMS
    // account (us-east-1) with a resource policy allowing this account to read
    // it. Grant read on that secret.
    if (props.envConfig?.oms?.credentialsSecretArn) {
      agentRuntime.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "OmsCoreGatewaySecretRead",
          actions: ["secretsmanager:GetSecretValue"],
          resources: [props.envConfig.oms.credentialsSecretArn],
        })
      );
    }
    // Cross-account KMS decrypt for the OMS client secret: the secret is
    // encrypted with an OMS customer-managed CMK. The CMK key policy grants
    // this account; the caller's identity policy must also allow kms:Decrypt on
    // that key ARN, or the cross-account GetSecretValue fails at decrypt.
    if (props.envConfig?.oms?.kmsKeyArn) {
      agentRuntime.addToRolePolicy(
        new iam.PolicyStatement({
          sid: "OmsCoreGatewaySecretKmsDecrypt",
          actions: ["kms:Decrypt", "kms:DescribeKey"],
          resources: [props.envConfig.oms.kmsKeyArn],
        })
      );
    }

    // Grant Memory read/write — Strands' session manager calls these
    // operations, plus the UI reads via the proxy. BatchCreate/BatchDelete are the
    // `remember` / `forget` tools (ltm.py): without BatchCreate every `remember`
    // since launch failed with AccessDeniedException, which the tool reported as
    // a transient "Could not save" (session 2f376186).
    agentRuntime.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "bedrock-agentcore:CreateEvent",
          "bedrock-agentcore:ListEvents",
          "bedrock-agentcore:GetEvent",
          "bedrock-agentcore:ListSessions",
          "bedrock-agentcore:GetMemoryRecord",
          "bedrock-agentcore:ListMemoryRecords",
          "bedrock-agentcore:RetrieveMemoryRecords",
          "bedrock-agentcore:BatchCreateMemoryRecords",
          "bedrock-agentcore:BatchDeleteMemoryRecords",
        ],
        resources: [
          conversationMemory.attrMemoryArn,
          `${conversationMemory.attrMemoryArn}/*`,
        ],
      })
    );

    // Grant S3 skills access. Bucket-wide read+write, deliberately left — the orchestrator hosts the
    // Lab service and the same tool set as the Task Agent (see the note on its grant above).
    skillsBucket.grantReadWrite(agentRuntime);
    uploadsBucket.grantReadWrite(agentRuntime);
    codeInterpreter.grantUse(agentRuntime);

    // ---- SandboxScopedS3Role (asset-lineage Phase 3 (b), design §9.2) ----
    // Short-lived, per-(project, session) S3 credentials for the code sandbox, which runs
    // MODEL-AUTHORED code. ABAC: the Main/Task runtime assumes this role with STS session tags
    // `project_id` / `session_id` (agent/src/sandbox_credentials.py) and injects the result into the
    // sandbox out of band; the policy below resolves every path from `${aws:PrincipalTag/...}`. The
    // sandbox itself is NEVER granted sts:AssumeRole (model code could mint itself any tags). A
    // session with no trusted project is tagged `project_id=none`, so it can read no project and
    // write only its legacy `artifacts/<sid>/`. Name-based ARN in RuntimeConfigSecret (see above).
    if (sandboxScopedCredentials) {
      const tagConditions = {
        Null: { "aws:RequestTag/project_id": "false", "aws:RequestTag/session_id": "false" },
        "ForAllValues:StringEquals": { "aws:TagKeys": ["project_id", "session_id"] },
      };
      const sandboxScopedRole = new iam.Role(this, "SandboxScopedS3Role", {
        roleName: sandboxScopedRoleName,
        description:
          "Per-(project, session) S3 access for the code sandbox (asset-lineage Phase 3 (b)). "
          + "Assumed only by the Main and Task runtimes, with project_id/session_id session tags.",
        assumedBy: new iam.ArnPrincipal(agentRuntime.role.roleArn).withConditions(tagConditions).withSessionTags(),
        maxSessionDuration: cdk.Duration.hours(1),
      });
      sandboxScopedRole.assumeRolePolicy?.addStatements(new iam.PolicyStatement({
        actions: ["sts:AssumeRole", "sts:TagSession"],
        principals: [new iam.ArnPrincipal(taskAgentRuntime.role.roleArn)],
        conditions: tagConditions,
      }));
      const pidTag = "${aws:PrincipalTag/project_id}";
      const sidTag = "${aws:PrincipalTag/session_id}";
      const writePrefixes = [`projects/${pidTag}/sessions/${sidTag}/*`, `artifacts/${sidTag}/*`];
      // + the staged ffmpeg build: once these credentials are injected they are the cell's ONLY S3 access, and the
      // crop / reel / timeline-action paths download ffmpeg from `tools/ffmpeg/` with them (no public link serves it).
      const readPrefixes = [`projects/${pidTag}/*`, "artifacts/*", "tools/ffmpeg/*"];
      sandboxScopedRole.addToPolicy(new iam.PolicyStatement({
        sid: "ReadProjectAndArtifacts",
        actions: ["s3:GetObject", "s3:GetObjectVersion"],
        resources: readPrefixes.map((p) => `${skillsBucket.bucketArn}/${p}`),
      }));
      sandboxScopedRole.addToPolicy(new iam.PolicyStatement({
        sid: "WriteOwnSession",
        actions: ["s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"],
        resources: writePrefixes.map((p) => `${skillsBucket.bucketArn}/${p}`),
      }));
      sandboxScopedRole.addToPolicy(new iam.PolicyStatement({
        sid: "ListReadablePrefixes",
        actions: ["s3:ListBucket"],
        resources: [skillsBucket.bucketArn],
        conditions: { StringLike: { "s3:prefix": [...readPrefixes, `projects/${pidTag}/sessions/${sidTag}/`] } },
      }));
      for (const runtime of [agentRuntime, taskAgentRuntime]) {
        runtime.addToRolePolicy(new iam.PolicyStatement({
          sid: "AssumeSandboxScopedS3Role",
          actions: ["sts:AssumeRole", "sts:TagSession"],
          resources: [sandboxScopedRoleArn],
        }));
      }
    }
    // Wave 4: the Main Agent invokes shared tools on the AWS_IAM Platform
    // Gateway. Scoped to that gateway's ARN (least privilege).
    agentRuntime.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["bedrock-agentcore:InvokeGateway"],
        resources: [platformGateway.attrGatewayArn],
      }),
    );

    // Grant main agent permission to invoke the task agent
    agentRuntime.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["bedrock-agentcore:InvokeAgentRuntime"],
        resources: ["*"], // Will be scoped after task agent is created
      })
    );

    // Grant main agent DynamoDB access for plan state
    planStateTable.grantReadWriteData(agentRuntime);

    // ---- async-job-tracking JobRegistry grant (R3.1, R3.4, R10.2) ----
    // The per-tool job-tracking wrap in `agent/src/main.py` (see
    // `jobs.strands_tool_wrap.JobTrackingMCPAgentTool`) uses
    // conditional PutItem to register jobs spawned by `start_*`
    // tools, and reads job records back to gate `*_results` calls on
    // terminal status. Both paths need read+write on the registry
    // table; the JOB_REGISTRY_TABLE env var binds the table name
    // above so the runtime knows which table to talk to.
    jobRegistryTable.grantReadWriteData(agentRuntime);

    // Grant main agent DynamoDB + Secrets Manager access for the gateway
    // registry (gateway-registry-admin task 1.2, R2). Read/write on the
    // registry table backs `GatewayRegistry.list_enabled / register / update /
    // disable`; the Secrets Manager statement scopes secret CRUD to the
    // `/${projectName}/gateway/*` prefix that `GatewayRegistry.register`
    // writes into when an admin pastes a gateway client_secret. Wildcard
    // region/account match the task spec — gateway secrets are addressed by
    // path, not by partition, so we don't constrain to this stack's region.
    // The interceptor resolves target ownership from this table -- the single
    // source of truth the agent writes at registration time and the proxy's
    // catalog reader also consults, so the enforcement and display surfaces
    // cannot disagree about who owns a target. Attached here rather than at
    // construction because the function must exist before the gateway that
    // references its ARN, and the table is created after both.
    gatewayInterceptorFn.addEnvironment(
      "GATEWAY_REGISTRY_TABLE", gatewayRegistryTable.tableName,
    );
    gatewayRegistryTable.grantReadData(gatewayInterceptorFn);

    gatewayRegistryTable.grantReadWriteData(agentRuntime);
    agentRuntime.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "secretsmanager:GetSecretValue",
          "secretsmanager:CreateSecret",
          "secretsmanager:UpdateSecret",
          "secretsmanager:DeleteSecret",
        ],
        resources: [
          `arn:aws:secretsmanager:*:*:secret:/${projectName}/gateway/*`,
        ],
      })
    );

    // Registering a user's own MCP server happens at REQUEST time, not deploy
    // time, so these permissions belong to the runtime rather than to a CDK
    // custom resource. The existing cred-provider-handler stays deploy-time and
    // platform-scoped; this is the per-user path.
    //
    // API-key credential providers let AgentCore Identity hold the user's
    // third-party token so it never passes through our code or our Secrets
    // Manager -- the credential-custodianship problem is delegated rather than
    // solved by us.
    agentRuntime.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "bedrock-agentcore:CreateApiKeyCredentialProvider",
          "bedrock-agentcore:GetApiKeyCredentialProvider",
          "bedrock-agentcore:UpdateApiKeyCredentialProvider",
          "bedrock-agentcore:DeleteApiKeyCredentialProvider",
          "bedrock-agentcore:ListApiKeyCredentialProviders",
          // CreateApiKeyCredentialProvider idempotently ensures the default
          // token vault exists and needs these even when it is a no-op: the
          // first live registration failed with "not authorized to perform
          // bedrock-agentcore:CreateTokenVault" although the vault already
          // existed. Identified from CredProviderFn, which carries the pair.
          "bedrock-agentcore:CreateTokenVault",
          "bedrock-agentcore:GetTokenVault",
        ],
        resources: [
          `arn:aws:bedrock-agentcore:${this.region}:${this.account}:token-vault/default`,
          `arn:aws:bedrock-agentcore:${this.region}:${this.account}:token-vault/default/*`,
        ],
      })
    );

    // AgentCore Identity keeps the API key in a Secrets Manager secret it owns.
    // CredProviderFn needs the equivalent for the OAuth path, so the caller --
    // not only the service -- has to be able to write it.
    agentRuntime.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "secretsmanager:CreateSecret",
          "secretsmanager:PutSecretValue",
          "secretsmanager:DeleteSecret",
          "secretsmanager:GetSecretValue",
        ],
        resources: [
          `arn:aws:secretsmanager:${this.region}:${this.account}:secret:bedrock-agentcore-identity!default/*`,
        ],
      })
    );

    // Retrieving an API-key credential happens under the RUNTIME's identity, not
    // the gateway's. This was granted to the gateway role first, from the
    // outbound-auth docs, and the live failure named the caller:
    //
    //   Failed to get API key from credential provider - User:
    //   ...AgentRuntimeExecutionRole... is not authorized to perform
    //   bedrock-agentcore:GetResourceApiKey
    //
    // So the target registered fine and then failed on every call. The grant on
    // the gateway role is left in place -- harmless, read-only, and the docs do
    // list it for gateway service roles.
    agentRuntime.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["bedrock-agentcore:GetResourceApiKey"],
        resources: [
          // Four shapes, matching GetResourceOauth2Token on the gateway role.
          // Two live failures, same action and principal, DIFFERENT resource:
          // first token-vault/.../apikeycredentialprovider/usr-..., then
          // workload-identity-directory/.../mediaworks-harness-gateway-...
          // Credential resolution is authorized against both.
          `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default`,
          `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default/workload-identity/*`,
          `arn:aws:bedrock-agentcore:${this.region}:${this.account}:token-vault/default`,
          `arn:aws:bedrock-agentcore:${this.region}:${this.account}:token-vault/default/apikeycredentialprovider/*`,
        ],
      })
    );

    // Target lifecycle on the harness gateway only. Deliberately scoped to this
    // one gateway rather than a wildcard: a user registering a server must not
    // be able to reach the media gateway's targets.
    agentRuntime.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "bedrock-agentcore:CreateGatewayTarget",
          "bedrock-agentcore:UpdateGatewayTarget",
          "bedrock-agentcore:DeleteGatewayTarget",
          "bedrock-agentcore:GetGatewayTarget",
          "bedrock-agentcore:ListGatewayTargets",
          // register_user_mcp_server calls the synchronize endpoint after
          // creating a target, so without this it fails one step later. Named
          // from the deploy-time GatewayTargetFn, which does the same thing.
          "bedrock-agentcore:SynchronizeGatewayTargets",
        ],
        resources: [
          gateway.attrGatewayArn,
          `${gateway.attrGatewayArn}/target/*`,
        ],
      })
    );

    // ---- Lab Infrastructure ----

    // Lab Asset_Index DynamoDB table — unified-mediaworks-platform single-table.
    //
    // Schema (per agent/src/lab/asset_index.py):
    //   PK    HASH    string   one of:
    //                            USER#<u>#DRAFTS
    //                            USER#<u>#PUBLISHED
    //                            GROUP#<g>#PUBLISHED
    //                            ORG#<o>#PUBLISHED
    //                            USER#<u>#TOOL_COLLECTIONS  (flat — no version)
    //                            MIGRATION#MARKER           (one-shot migration markers)
    //   SK    RANGE   string   <category>#<draftId>#draft
    //                          <category>#<publishedId>#v<NNNNNNNNNN>
    //                          <category>#<id>             (tool-collection flat)
    //                          <marker_key>                (migration markers)
    //
    // GSI byPublishedId (sparse, only on recordType=published versioned rows):
    //   GSI1PK  HASH    "PUB#<publishedId>"
    //   GSI1SK  RANGE   "v<NNNNNNNNNN>"
    //   Projection: ALL — query_lineage decodes full asset rows from this index.
    const labAssetIndexTable = new dynamodb.Table(this, "LabAssetIndexTable", {
      tableName: `${projectName}-lab-asset-index`,
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      // Feeds the Registry projection worker. Lifecycle transactions append an
      // `OUTBOX#...` row (agent/src/lab/outbox.py) in the SAME
      // TransactWriteItems as the canonical mutation, and the projector's event
      // source filters the stream on `recordType = "outbox"` so asset-row
      // writes never invoke it.
      //
      // NEW_IMAGE, not NEW_AND_OLD_IMAGES: an outbox row is only ever inserted,
      // never updated, so the old image would always be empty — and halving the
      // stream payload keeps the 1 MB per-record limit comfortable.
      //
      // Enabling a stream on an existing table is an in-place, non-destructive
      // update (no replacement), which matters because this table is RETAIN and
      // holds every user's authored asset metadata.
      stream: dynamodb.StreamViewType.NEW_IMAGE,
    });

    // GSI byPublishedId — used by query_lineage to enumerate every version
    // of a published lineage in one query. Sparse: writes only emit GSI1PK /
    // GSI1SK on rows with recordType=published and a versioned category, so
    // drafts and flat tool-collection rows are not projected.
    labAssetIndexTable.addGlobalSecondaryIndex({
      indexName: "byPublishedId",
      partitionKey: { name: "GSI1PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "GSI1SK", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // ---- Lab Artifacts S3 Bucket ----
    //
    // Holds the Lab service's draft + published asset content under
    // `users/<actor>/drafts/...`, `users/<actor>/published/...`, and
    // post-migration `org/<o>/published/...` plus run records under
    // `users/<actor>/lab-runs/...`. Imported by default; pass
    // `-c createLabArtifactsBucket=true` to create.
    const labArtifactsBucketName =
      `${projectName}-lab-artifacts-${this.account}-${this.region}`;
    const labArtifactsBucket: s3.IBucket = shouldCreate("createLabArtifactsBucket")
      ? new s3.Bucket(this, "LabArtifactsBucket", {
          bucketName: labArtifactsBucketName,
          removalPolicy: cdk.RemovalPolicy.RETAIN,
          versioned: false, // Lifecycle on the row, not the object
          encryption: s3.BucketEncryption.S3_MANAGED,
          blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
        })
      : s3.Bucket.fromBucketName(
          this,
          "LabArtifactsBucket",
          labArtifactsBucketName,
        );

    // Lab_Role: prefix-scoped IAM role assumed by Lab_Mode requests (Task 6.1).
    // Statically expands per-group statements at synthesis time. Group membership
    // changes require a CDK redeploy. Production runtime session tagging
    // (userId, orgId, groups) is deferred to the AgentCore-proxy follow-on; this
    // construct is forward-compatible with that work.
    const labRole = new LabRole(this, "LabRole", {
      artifactsBucket: labArtifactsBucket,
      groupIds: [], // Populated by future deployments once group membership is known
    });

    // ---- Static-federation M2C read grants on auxiliary source buckets ----
    // Separately deployed M2C roles may need compatibility access to these Harness buckets.
    // Nested source/manifest mode uses SkillsBucket as IngestBucket and leaves this path disabled.
    if (m2cServiceRoleArns.length > 0) {
      const m2cBucketsToGrant = [uploadsBucket, labArtifactsBucket];
      for (const bucket of m2cBucketsToGrant) {
        new cr.AwsCustomResource(this, `M2CReadGrant-${bucket.node.id}`, {
          onCreate: {
            service: "S3",
            action: "putBucketPolicy",
            parameters: {
              Bucket: bucket.bucketName,
              Policy: JSON.stringify({
                Version: "2012-10-17",
                Statement: [
                  {
                    Sid: "AllowM2CServiceRolesListBucket",
                    Effect: "Allow",
                    Principal: { AWS: m2cServiceRoleArns },
                    Action: "s3:ListBucket",
                    Resource: bucket.bucketArn,
                  },
                  {
                    Sid: "AllowM2CServiceRolesGetObject",
                    Effect: "Allow",
                    Principal: { AWS: m2cServiceRoleArns },
                    Action: "s3:GetObject",
                    Resource: `${bucket.bucketArn}/*`,
                  },
                ],
              }),
            },
            physicalResourceId: cr.PhysicalResourceId.of(`m2c-bucket-policy-${bucket.bucketName}`),
          },
          onUpdate: {
            service: "S3",
            action: "putBucketPolicy",
            parameters: {
              Bucket: bucket.bucketName,
              Policy: JSON.stringify({
                Version: "2012-10-17",
                Statement: [
                  {
                    Sid: "AllowM2CServiceRolesListBucket",
                    Effect: "Allow",
                    Principal: { AWS: m2cServiceRoleArns },
                    Action: "s3:ListBucket",
                    Resource: bucket.bucketArn,
                  },
                  {
                    Sid: "AllowM2CServiceRolesGetObject",
                    Effect: "Allow",
                    Principal: { AWS: m2cServiceRoleArns },
                    Action: "s3:GetObject",
                    Resource: `${bucket.bucketArn}/*`,
                  },
                ],
              }),
            },
            physicalResourceId: cr.PhysicalResourceId.of(`m2c-bucket-policy-${bucket.bucketName}`),
          },
          onDelete: {
            service: "S3",
            action: "deleteBucketPolicy",
            parameters: { Bucket: bucket.bucketName },
          },
          policy: cr.AwsCustomResourcePolicy.fromStatements([
            new iam.PolicyStatement({
              actions: ["s3:PutBucketPolicy", "s3:GetBucketPolicy", "s3:DeleteBucketPolicy"],
              resources: [bucket.bucketArn],
            }),
          ]),
        });
      }
    }

    // Chat_Role: read-only prefix-scoped IAM role assumed by Chat_Mode requests
    // (Task 6.2, Requirement 10.3). Identical to Lab_Role minus all `drafts/*`
    // access and all writes on `users/<u>/published/*`. Result is read-only
    // across `users/${userId}/published/*`, `groups/<g>/published/*`, and
    // `org/${orgId}/published/*`. Same static-expansion model for groups as
    // Lab_Role; same forward-compatibility with the AgentCore-proxy session
    // tagging follow-on.
    const chatRole = new ChatRole(this, "ChatRole", {
      artifactsBucket: labArtifactsBucket,
      groupIds: [], // Populated by future deployments once group membership is known
    });

    // Project_Runtime_Role: read-only prefix-scoped IAM role assumed by
    // Project_Mode runs (Task 6.3, Requirement 10.4). Group bindings are
    // project-specific — this construct is meant to be instantiated per
    // project, statically emitting the project's bound group ARNs at
    // synthesis time. Differs from Chat_Role in that there is no `users/*`
    // access at all (no private published, no drafts) — the project runtime
    // can only see assets that have been explicitly shared to one of the
    // project's bound groups or to the org. Same forward-compatibility with
    // the AgentCore-proxy session tagging follow-on as Lab_Role / Chat_Role.
    const projectRuntimeRole = new ProjectRuntimeRole(this, "ProjectRuntimeRole", {
      artifactsBucket: labArtifactsBucket,
      groupIds: [], // Populated per project once project bindings are known
    });

    // ---- Lab_Service runtime: REMOVED ----
    //
    // The Lab_Service capability has been merged into the Main Agent runtime
    // (see `agent/src/main.py` and `agent/src/main_lab_tools.py`). The Main
    // Agent now owns the Asset_Index DDB table and Lab artifacts bucket; the
    // 20 Lab CRUD operations are exposed as Strands tools, and structured
    // `{action: ...}` payloads route through `LabService.handle` directly.
    //
    // Grant the Main Agent the same Lab data permissions the retired runtime
    // had (DDB + S3 + InvokeAgentRuntime for the Task Agent harness path).
    labAssetIndexTable.grantReadWriteData(agentRuntime);
    labArtifactsBucket.grantReadWrite(agentRuntime);
    // Task agent reads the asset index to load the actor's enabled hooks.
    labAssetIndexTable.grantReadData(taskAgentRuntime);
    // Task agent reads published Custom Agent personas + SKILL.md files for
    // external invoke config resolution (custom-agents seam fix). Read-only:
    // the runtime never writes lab assets.
    labArtifactsBucket.grantRead(taskAgentRuntime);

    // ---- Deploy-time preload (design docs/design-2026-10-27-admin-preload-and-preanalyzed-ingest.md) ----
    //
    // Gated: only created when `envConfig.preload` is set (operator passed `-c ownerEmail=…` and/or
    // `-c preloadPath=…`) AND projects/ingestion exist (the preload seeds Project rows and queues assets
    // the ingestion pipeline then analyzes). With no preload context the construct is not instantiated at
    // all, so a normal deploy is byte-identical and nothing extra runs. When configured but no package is
    // present in the Ingest bucket, the Lambda itself no-ops (SUCCESS); a package present without an
    // ownerEmail fails the deploy loudly (both decided in `preload.handler.locate_package`). The Ingest
    // bucket is the shared `skillsBucket`; skills publish to the Lab asset-index table + artifacts bucket
    // exactly as a user's own private Lab publish would.
    if (props.envConfig?.preload && projectsStack && ingestionStack) {
      const preload = addPreloadResource(this, "PreloadFn", {
        projectName,
        envName,
        ingestBucket: skillsBucket,
        projectsTable: projectsStack.projectsTable,
        assetIndexTable: labAssetIndexTable,
        artifactsBucket: labArtifactsBucket,
        userPool,
        ownerEmail: props.envConfig.preload.ownerEmail,
        preloadPath: props.envConfig.preload.preloadPath,
        // Re-run trigger: an operator nonce wins (manual same-commit redeploy after uploading a package),
        // else the app version, which CI changes every build. Without either, the handler runs on first
        // create only. This is the fix for the former constant salt that never re-ran on redeploy.
        reinvokeSalt: props.envConfig.preload.preloadNonce ?? props.appVersion,
        // The no-Docker zip packager adds PyYAML (the Lab skill validator needs it; the managed runtime lacks it).
        packaging: {
          codeBucket: agentCodeBucket,
          packagerServiceToken: packagerProvider.serviceToken,
          baseCode: agentCode,
        },
      });
      // Run only once the ingestion pipeline (sweep + tick) can ingest what the preload queues.
      preload.node.addDependency(ingestionStack);
    }

    // ================================================================
    // Capability control plane — AWS Agent Registry projection
    // ================================================================
    //
    // Deterministic capability inventory used to travel through the Main Agent
    // AgentCore Runtime, so listing skills could cold-start Strands, a model
    // client, OAuth, MCP sessions, and skill packages to answer a question that
    // needs none of them. Two pieces replace that:
    //
    //  1. an ASYNCHRONOUS projector (below) that mirrors org-scoped published
    //     versions into the environment's AWS Agent Registry, and
    //  2. an ALWAYS-ON Capability API (further down, next to the UI proxy) that
    //     serves merged canonical + Registry + Gateway data over
    //     `/control-plane/*`.
    //
    // DynamoDB/S3 stay canonical throughout; the Registry is an eventually
    // consistent discovery projection and never a package store. Namespace is
    // `agent-registry` / `agent-registry-control` exclusively — the deprecated
    // Registry surface under `bedrock-agentcore` is not used (identity and
    // runtime resources keep their `bedrock-agentcore` names).
    // `envName` is declared at the top of the constructor — the agent runtimes,
    // defined earlier in this file, also stamp it as DEPLOY_ENV.
    const registryConfig = props.envConfig?.agentRegistry;
    // The Agent Registry has no CloudFormation resource type. An env that names a registry but pins
    // no id (`customer`) gets one created-or-reused by name at deploy time; the pinned envs keep
    // their literal id, untouched.
    const registryId = !registryConfig?.registryId && registryConfig?.registryName
      ? new AgentRegistryProvider(this, "AgentRegistry", { registryName: registryConfig.registryName }).registryId
      : registryConfig?.registryId ?? "";
    const registryName = registryConfig?.registryName ?? "";
    const projectionEnabled = Boolean(
      registryConfig?.projectionEnabled && registryId,
    );

    if (registryName && !registryName.endsWith(`-${envName}`)) {
      // Synthesis-time guard mirroring `capability/config.py`. A registry name
      // from another environment is a copy-paste error that would otherwise be
      // discovered only after a projector wrote into the wrong catalog.
      throw new Error(
        `agentRegistry.registryName '${registryName}' does not belong to env ` +
          `'${envName}' (expected suffix '-${envName}')`,
      );
    }

    // Registry ARN, built rather than configured: `create-registry` returns the
    // ARN, but keeping only the id in env config means one value to paste and
    // no chance of an id/ARN pair that disagree. Empty when unprovisioned, in
    // which case the policies below are not attached at all.
    const registryArn = registryId
      ? `arn:aws:agent-registry:${this.region}:${this.account}:registry/${registryId}`
      : "";

    // ---- Projection state ----
    //
    // One row per (environment, category, assetId, version) recording what the
    // Registry currently reflects: canonical revision, descriptor digest,
    // record id/ARN, status, attempt count, redacted last error. A projection
    // cache, not a source of truth — losing it costs a reconciliation pass, not
    // data. RETAIN anyway, because rebuilding it is a deliberate operation.
    const projectionStateTable = new dynamodb.Table(this, "RegistryProjectionState", {
      tableName: `${projectName}-registry-projection-state`,
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING }, // env#category#assetId
      sortKey: { name: "SK", type: dynamodb.AttributeType.STRING }, // v<version>
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // ---- Encrypted DLQ ----
    //
    // Terminal destination for a batch the projector could not process — a
    // poison descriptor (prohibited internal tool, leaked credential field,
    // oversize payload) or a source that stayed unavailable through every
    // retry. Encrypted because an event names assets and versions, and 14 days
    // so a Friday failure is still replayable on Monday.
    const projectorDlq = new sqs.Queue(this, "RegistryProjectorDlq", {
      queueName: `${projectName}-registry-projector-dlq`,
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      retentionPeriod: cdk.Duration.days(14),
    });

    // ---- Projection role (least privilege, one registry) ----
    //
    // Record-level writes on exactly one registry ARN. Deliberately NOT
    // `AgentRegistryFullAccess`, and deliberately no `DeleteRegistryRecord` or
    // `DeleteRegistry`: the projector's only removal mechanism is deprecating a
    // revision, so an over-broad policy is the difference between a bug that
    // hides a record and one that destroys history (R9/R16).
    const projectorRole = new iam.Role(this, "RegistryProjectorRole", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      description:
        "Registry projection worker: record create/update/status/submit on one registry",
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          "service-role/AWSLambdaBasicExecutionRole",
        ),
      ],
    });
    if (registryArn) {
      projectorRole.addToPolicy(
        new iam.PolicyStatement({
          sid: "ProjectRegistryRecords",
          actions: [
            "agent-registry:CreateRegistryRecord",
            "agent-registry:GetRegistryRecord",
            "agent-registry:ListRegistryRecords",
            "agent-registry:UpdateRegistryRecord",
            "agent-registry:UpdateRegistryRecordStatus",
            "agent-registry:SubmitRegistryRecordForApproval",
          ],
          resources: [registryArn, `${registryArn}/*`],
        }),
      );
    }
    // Canonical reads only. The projector reloads the row it is about to
    // project (never trusting the event payload) and reads SKILL.md for
    // discovery text; it must not be able to mutate either.
    labAssetIndexTable.grantReadData(projectorRole);
    labArtifactsBucket.grantRead(projectorRole);
    projectionStateTable.grantReadWriteData(projectorRole);
    labAssetIndexTable.grantStreamRead(projectorRole);
    projectorDlq.grantSendMessages(projectorRole);

    // ---- Projector Lambda ----
    //
    // The deployment asset is `agent/src` (handler `capability.projector.handler`)
    // rather than a self-contained directory, so the projector imports the SAME
    // descriptor builders, naming rules, and AssetIndex schema constants as the
    // Capability API and the backfill script. A duplicated pad width or
    // discriminator string is exactly the drift that stops projection silently.
    //
    // Plain `Code.fromAsset` with no bundling: the GitLab deploy job runs on
    // `node:20` with no Docker daemon, so CDK asset bundling and
    // `DockerImageFunction` both fail there. That also means no pip install —
    // `agent/src/capability/botocore_models/` vendors the ~15 KB of
    // `agent-registry` service models and `capability/registry.py` puts them on
    // `AWS_DATA_PATH`, because the managed runtime's bundled botocore predates
    // the namespace (see that directory's README).
    const registryProjector = new lambda.Function(this, "RegistryProjector", {
      functionName: `${projectName}-registry-projector`,
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: "capability.projector.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "../../agent/src"), {
        // __pycache__ would ship AND make the asset hash depend on whether
        // anyone happened to run the tests, churning a new version on no-op
        // deploys (same reason as ResultResolutionLayer above).
        exclude: [
          "**/__pycache__",
          "**/*.pyc",
          "agent.egg-info",
          "**/*.md",
        ],
      }),
      role: projectorRole,
      timeout: cdk.Duration.seconds(60),
      memorySize: 512,
      environment: {
        ASSET_INDEX_TABLE: labAssetIndexTable.tableName,
        PROJECTION_STATE_TABLE: projectionStateTable.tableName,
        ARTIFACTS_BUCKET: labArtifactsBucket.bucketName,
        REGISTRY_ID: registryId,
        REGISTRY_NAME: registryName,
        REGISTRY_PROJECTION_ENABLED: projectionEnabled ? "true" : "false",
        DEPLOY_ENV: envName,
      },
      description:
        "Projects org-scoped published versions into the harness-shared AWS Agent Registry",
    });

    // The event source is created regardless of `projectionEnabled` so the
    // wiring is identical in every environment and enabling projection is a
    // config change rather than a topology change. With projection off the
    // handler raises a ConfigError on the first event — visible, and it keeps
    // the outbox rows on the stream rather than silently draining them.
    registryProjector.addEventSource(
      new lambdaEventSources.DynamoEventSource(labAssetIndexTable, {
        startingPosition: lambda.StartingPosition.LATEST,
        batchSize: 10,
        // Bisect on error so one poison event does not send its nine innocent
        // batch-mates to the DLQ with it.
        bisectBatchOnError: true,
        retryAttempts: 8,
        maxRecordAge: cdk.Duration.hours(24),
        reportBatchItemFailures: true,
        onFailure: new lambdaEventSources.SqsDlq(projectorDlq),
        // Server-side filter: only outbox rows reach the projector. Without it
        // every asset write would invoke the Lambda just to be discarded. The
        // literal must match `OUTBOX_RECORD_TYPE` in agent/src/lab/outbox.py.
        filters: [
          lambda.FilterCriteria.filter({
            eventName: lambda.FilterRule.isEqual("INSERT"),
            dynamodb: {
              NewImage: { recordType: { S: lambda.FilterRule.isEqual("outbox") } },
            },
          }),
        ],
      }),
    );

    // ---- Reconciliation role ----
    //
    // Read-only across canonical metadata, the artifacts needed to rebuild a
    // descriptor, Registry records, and projection state. Separate from the
    // projector so a reconciliation run cannot mutate what it is auditing;
    // replay is a deliberate re-invocation of the projector, not a capability
    // of this role (design "AWS roles").
    const reconciliationRole = new iam.Role(this, "RegistryReconciliationRole", {
      assumedBy: new iam.AccountPrincipal(this.account),
      description:
        "Read-only reconciliation of canonical state against the Registry projection",
    });
    labAssetIndexTable.grantReadData(reconciliationRole);
    labArtifactsBucket.grantRead(reconciliationRole);
    projectionStateTable.grantReadData(reconciliationRole);
    if (registryArn) {
      reconciliationRole.addToPolicy(
        new iam.PolicyStatement({
          sid: "ReadRegistryForReconciliation",
          actions: [
            "agent-registry:GetRegistry",
            "agent-registry:GetRegistryRecord",
            "agent-registry:ListRegistries",
            "agent-registry:ListRegistryRecords",
            "agent-registry:ListDiscoverableRegistryRecords",
            "agent-registry:SearchDiscoverableRegistryRecords",
            "agent-registry:BatchGetDiscoverableRegistryRecord",
          ],
          resources: [registryArn, `${registryArn}/*`],
        }),
      );
    }

    // ---- Improve producer: self-refreshing SNAPSHOT (scheduled) ----
    //
    // Runs the whole Observe->Evaluate->Mine->Propose producer on a daily
    // schedule and writes the ONE global `(__system__, "SNAPSHOT")` item the
    // Capability API serves. This REPLACES the manual `mine_local --emit-wire` +
    // `improve.seed_snapshot` two-step with an in-cloud job — the SNAPSHOT
    // refreshes on its own, browser-only, with no local checkout in the loop.
    //
    // Same no-Docker packaging mold as RegistryProjector above:
    // `Code.fromAsset("../../agent/src")`, handler is a module path into the
    // bundled `improve` package. The GitLab deploy job runs on `node:20` with no
    // Docker daemon and no pip install, so the producer must run on the managed
    // `python3.13` runtime as-is. That runtime bundles boto3 but NOT PyYAML (nor
    // strands/agentcore) — so the producer ships its config + Sigma registry as
    // JSON (`improve/data/*.json`) and never imports yaml at module scope
    // (guarded by tests/test_capability_api_isolation.py). boto3 covers the live
    // judge (`bedrock-runtime.converse`), the memory reader, the telemetry
    // harvest (Logs Insights + DynamoDB scans), and the SNAPSHOT write.
    //
    // Identical `exclude` to RegistryProjector, so both point at the SAME asset
    // hash (one S3 upload, two functions) rather than churning a second zip.
    const improveWorker = new lambda.Function(this, "ImproveWorker", {
      functionName: `${projectName}-improve-worker`,
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: "improve.aws_lambda.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "../../agent/src"), {
        exclude: ["**/__pycache__", "**/*.pyc", "agent.egg-info", "**/*.md"],
      }),
      // Live scoring re-judges the whole selected population each run, so the
      // 15-min ceiling is the hard bound; the harvest window + population are
      // capped by env below to stay well under it. Memory holds the harvest JSON
      // plus the judge's in-flight request/response payloads.
      timeout: cdk.Duration.minutes(15),
      memorySize: 1024,
      environment: {
        // No default table name in the handler on purpose — a misconfigured
        // deploy fails loud rather than writing to a guessed table.
        IMPROVE_TABLE: improveTable.tableName,
        IMPROVE_HARVEST_DIR: "/tmp/improve-harvest",
        // The deployed memory wins over the id baked into loop.config.json, so
        // the reader can never point at a stale memory the JSON happened to name.
        IMPROVE_MEMORY_ID: conversationMemory.attrMemoryId,
        // harvest_live scans these by name (its hardcoded defaults omit the env
        // suffix); bind the real tables so the worker is correct in any env.
        IMPROVE_PLAN_STATE_TABLE: planStateTable.tableName,
        IMPROVE_JOB_REGISTRY_TABLE: jobRegistryTable.tableName,
        // A fan-out plan's nodes.json is offloaded to S3 above ~300 KB; without
        // this its edges read as unavailable (loud) instead of being fetched.
        SKILLS_S3_BUCKET: skillsBucket.bucketName,
        // Time-budget knobs for the 15-min ceiling. runs=1 is the cheapest judge
        // setting (loop.config.json ships 3); newest IMPROVE_LIMIT sessions only.
        IMPROVE_HARVEST_HOURS: "24",
        // Sized to CLEAR a day, not to sample one. Measured on the deployed 24h window: 36
        // sessions carry enough spans to be worth scoring (2026-09-03), 37 on an earlier
        // window, and the ten scheduled runs before it admitted 9..25 — so 60 is ~1.6x the
        // busiest day observed. At 25 roughly a third of the day was never counted, and because
        // the population is ordered NEWEST FIRST a plan's sub-sessions sort ABOVE the request
        // that spawned them: the cap kept children and dropped parents, which is why the surface
        // has a state for a sub-session whose request is absent. Replaying that same window at
        // 25 admitted 11 sub-sessions, 1 with its parent outside; at 60 nothing is dropped and
        // the state is empty.
        //
        // Bounded rather than unlimited because the ceiling is 900s and the population is not
        // ours to bound — one unusually busy day is the failure this cap was written for. Real
        // cost from this function's own log group: 2026-09-03 08:01 scored 25 in 74.6s, of
        // which 11.0s was the harvest (that half tracks the window, not the population), and a
        // manual 168h run scored 174 in 287s. Worst per-run rate over ten scheduled runs is
        // 3.1s, so a FULL 60 lands near 200s — under a quarter of the ceiling, with the
        // 174-run run already proving the shape well past it. Memory peaked at 151 MB of 1024.
        //
        // Re-measure when a scheduled run reports population_size equal to this number: that
        // is the cap binding again, i.e. a day that no longer fits. Take three figures — the
        // discoverable session count, the duration, and how many of the population were
        // CONVERSATIONAL, because only those cost a Bedrock judge call (measured 14 of 25),
        // so the judge bill tracks that share and never the cap itself.
        IMPROVE_LIMIT: "60",
        IMPROVE_JUDGE_RUNS: "1",
        // AgentCore online-eval result groups are `<prefix><config name>-<id>`, the id generated —
        // so the harvest DISCOVERS them by this prefix (every config this project owns) rather
        // than naming one. Read with the ImproveInsightsRead grant below; no new permission.
        IMPROVE_EVAL_RESULTS_PREFIX: `/aws/bedrock-agentcore/evaluations/results/${projectName.replace(/-/g, "_")}_`,
      },
      description:
        "Self-refreshing Improve producer: harvest telemetry, re-judge, mine, write the global SNAPSHOT",
    });

    // Store: read+write — the cross-run SUGGESTION_STORE item AND the SNAPSHOT
    // write both live in this table.
    improveTable.grantReadWriteData(improveWorker);
    // Evidence: bounded Scan on plan-state + job-registry (grantReadData includes Scan).
    planStateTable.grantReadData(improveWorker);
    jobRegistryTable.grantReadData(improveWorker);
    // Offloaded nodes.json fallback (S3 GetObject in improve/observe/build_run.py).
    skillsBucket.grantRead(improveWorker);
    // Conversation memory — the reader (agent/src/improve/observe/memory_reader.py) makes
    // THREE calls: `list_sessions` to map every session to its owner in one sweep,
    // `list_actors` to page the actor list, and `list_events` for that actor's turns.
    // `list_sessions` is what makes owner resolution linear in (actors + sessions) instead of
    // their product; without this grant the reader still resolves every owner correctly, but by
    // probing each actor per session — so the cost of omitting it is one denied call plus a
    // quadratic scan, not a wrong answer. Granting it is therefore a performance decision, and
    // the least-privilege argument that excluded it no longer describes the code.
    // Still read-only (no CreateEvent/GetEvent), mirroring the task-runtime grant's resource
    // scope above. ListActors stays load-bearing: an earlier draft omitted it, and that
    // unprotected call would AccessDeny and crash the whole run on its first session.
    improveWorker.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "ImproveMemoryRead",
        actions: [
          "bedrock-agentcore:ListActors",
          "bedrock-agentcore:ListEvents",
          "bedrock-agentcore:ListSessions",
        ],
        resources: [
          conversationMemory.attrMemoryArn,
          `${conversationMemory.attrMemoryArn}/*`,
        ],
      }),
    );
    // Logs Insights over `aws/spans` for the telemetry harvest. StartQuery
    // cannot be scoped to a single log group, so `*` mirrors OnlineEvaluationRole.
    improveWorker.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "ImproveInsightsRead",
        actions: [
          "logs:StartQuery",
          "logs:GetQueryResults",
          "logs:DescribeLogGroups",
          "logs:DescribeLogStreams",
        ],
        resources: ["*"],
      }),
    );
    // Ambient platform-health read (harvest_alarms). Describe* are not resource-scopable.
    improveWorker.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "ImproveAlarmsRead",
        actions: ["cloudwatch:DescribeAlarms", "cloudwatch:DescribeAlarmHistory"],
        resources: ["*"],
      }),
    );
    // The live judge (`bedrock-runtime.converse`) — mirrors OnlineEvaluationRole's BedrockInvoke.
    improveWorker.addToRolePolicy(
      new iam.PolicyStatement({
        sid: "ImproveBedrockInvoke",
        actions: ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
        resources: ["*"],
      }),
    );

    // Daily, off-peak. Same Rule + LambdaFunction target mold as the polling worker.
    const improveSchedule = new events.Rule(this, "ImproveWorkerSchedule", {
      ruleName: `${projectName}-improve-worker-schedule`,
      description: "Fires the Improve producer once a day to refresh the SNAPSHOT",
      schedule: events.Schedule.cron({ minute: "0", hour: "8" }), // 08:00 UTC
    });
    improveSchedule.addTarget(new eventsTargets.LambdaFunction(improveWorker));

    // ---- UI hosting: agentcore-proxy backend (ECS Fargate + ALB) ----
    //
    // The Lab UI ("Media2Cloud") was local-dev-only: `ui/agentcore-proxy.ts`
    // ran as Vite dev-middleware, never as a standalone server. That logic
    // now lives in `ui/server/app.ts` (an Express app — see that file's
    // header comment for the exact route list and required env vars) with
    // a standalone entry point at `ui/server/index.ts`, containerized by
    // `ui/proxy.Dockerfile`.
    //
    // This is the first VPC-attached resource in this stack — everything
    // else here is serverless (Lambda, DynamoDB, SQS, Bedrock AgentCore).
    // The proxy needs a long-lived process for `/jobs/stream`'s indefinite
    // SSE long-poll loop, which doesn't fit Lambda's execution-time model.
    //
    // No NAT gateway: tasks run in PUBLIC subnets with a public IP. Every
    // AWS API the proxy calls (Bedrock, Bedrock AgentCore, DynamoDB, SQS,
    // S3) is a public endpoint reachable directly — a NAT gateway would
    // only be needed for private-subnet egress, which this workload
    // doesn't require.
    // Import the account's default VPC rather than creating a new one —
    // both staging (100000000002) and prod (100000000003) are shared
    // accounts already at (or near) the default 5-VPCs-per-region quota
    // from other, unrelated stacks. The default VPC's public subnets (one per AZ,
    // `MapPublicIpOnLaunch: true`) are exactly what this no-NAT, public-only
    // Fargate setup needs — no new VPC, IGW, or route tables required.
    //
    // A fresh customer account may have no default VPC, so `envConfig.createVpc` builds a 2-AZ
    // public VPC (no NAT — the tasks reach ECR and AWS APIs through the IGW with public IPs), and
    // `vpcId` uses an existing one, which needs public subnets in at least two AZs (the ALB does).
    const uiVpc = props.envConfig?.vpcId
      ? ec2.Vpc.fromLookup(this, "UiVpc", { vpcId: props.envConfig.vpcId })
      : props.envConfig?.createVpc
        ? new ec2.Vpc(this, "UiVpc", {
            maxAzs: 2, natGateways: 0,
            subnetConfiguration: [{ name: "public", subnetType: ec2.SubnetType.PUBLIC }],
          })
        : ec2.Vpc.fromLookup(this, "UiVpc", { isDefault: true });

    // The proxy's container image is built and pushed by a kaniko job in
    // `.gitlab-ci.yml` (GitLab CI runners have no Docker daemon, so
    // `ecs.ContainerImage.fromAsset`'s local `docker build` doesn't work
    // there — kaniko builds rootless, matching the pattern already used by
    // other projects on these same runners). CDK only references the
    // already-pushed tag; it never builds the image itself.
    //
    // The repo itself is imported, not created here, to avoid a bootstrap
    // ordering loop: kaniko's `build` stage must push an image before this
    // `cdk deploy` runs (the ECS service would otherwise fail to pull a
    // nonexistent image and get stuck, same as the earlier arch-mismatch
    // incident — no deployment circuit breaker is configured), but ECR
    // requires the repository to exist before any push. If CDK created the
    // repo, that creation would only happen inside this same `cdk deploy` —
    // too late for kaniko's prior push. So the repo is created once,
    // out-of-band (see infra/gitlab/README.md, same pattern as the GitLab
    // IAM roles), and CDK only ever references it by name.
    const uiProxyRepo = ecr.Repository.fromRepositoryName(
      this,
      "UiProxyRepo",
      `${projectName}-ui-proxy`,
    );
    // `-c uiProxyImageTag=<sha>-<pipeline-iid>` from CI selects the pushed image in that repo.
    // Without it, HarnessAssets built the image in CodeBuild for this deployment (top of the
    // constructor) — never `latest`, which no build pushes.
    const uiProxyImage = uiProxyImageTagCtx
      ? ecs.ContainerImage.fromEcrRepository(uiProxyRepo, uiProxyImageTagCtx)
      : harnessAssets!.uiImage!;

    const uiProxyTaskRole = new iam.Role(this, "UiProxyTaskRole", {
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
    });

    // Same three runtime-invoke + Memory + Bedrock grants the AgentCore
    // runtimes themselves get (see `agentRuntime.addToRolePolicy` above) —
    // the proxy is a client of these runtimes, not a runtime itself, so it
    // needs InvokeAgentRuntime rather than the runtimes' own execution
    // grants.
    //
    // Scoped by runtime NAME (`runtime/<name>-*`), never by `agentRuntimeArn`: this statement lands in the
    // DefaultPolicy the proxy's ECS service DependsOn, so a GetAtt made the service wait for every runtime
    // update before it could start its own (~4 min of runtime + ~3.5 min of service in SERIES on each agent
    // deploy; staging 2026-10-20). Same pattern as the task agent's grant on the render agent.
    const runtimeNameArn = (name: string) =>
      `arn:aws:bedrock-agentcore:${this.region}:${this.account}:runtime/${name}-*`;
    const proxyInvokedRuntimes = [
      `${projectName.replace(/-/g, "_")}_main_agent`,
      `${projectName.replace(/-/g, "_")}_task_agent`,
      renderAgentRuntimeName,
    ];
    uiProxyTaskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "InvokeAgentRuntimes",
        actions: ["bedrock-agentcore:InvokeAgentRuntime"],
        resources: proxyInvokedRuntimes.flatMap((n) => [runtimeNameArn(n), `${runtimeNameArn(n)}/*`]),
      })
    );
    uiProxyTaskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "MemoryReadWrite",
        actions: [
          "bedrock-agentcore:CreateEvent",
          "bedrock-agentcore:ListEvents",
          "bedrock-agentcore:GetEvent",
          "bedrock-agentcore:ListSessions",
        ],
        resources: [
          conversationMemory.attrMemoryArn,
          `${conversationMemory.attrMemoryArn}/*`,
        ],
      })
    );
    uiProxyTaskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "ListInferenceProfiles",
        actions: ["bedrock:ListInferenceProfiles"],
        resources: ["*"],
      })
    );
    // Runtime ARNs for the proxy, read from SSM at startup (no task-definition → runtime edge).
    for (const s of runtimeArnParamReadStatements()) uiProxyTaskRole.addToPolicy(s);
    // Generated_Media (ingestion Req 9): the transcript finalize sends one message per Projects turn.
    // ARN from the fixed queue name, so the parent takes no token from the nested stack for it.
    const generatedMediaQueueUrl = ingestionStack?.generatedMediaQueue
      ? `https://sqs.${this.region}.amazonaws.com/${this.account}/${generatedMediaQueueName(projectName)}`
      : "";
    if (generatedMediaQueueUrl) {
      uiProxyTaskRole.addToPolicy(
        new iam.PolicyStatement({
          sid: "GeneratedMediaSend",
          actions: ["sqs:SendMessage"],
          resources: [`arn:aws:sqs:${this.region}:${this.account}:${generatedMediaQueueName(projectName)}`],
        })
      );
    }
    // Admin (gateway-admins) user management surface (`/admin/*` in ui/server/app.ts):
    // read the user directory and grant/revoke the gateway-admins group. Scoped to
    // THIS user pool; least-privilege — exactly the three verbs the proxy calls.
    uiProxyTaskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "AdminCognitoUserManagement",
        actions: [
          "cognito-idp:ListUsers",
          "cognito-idp:ListUsersInGroup",
          "cognito-idp:AdminAddUserToGroup",
          "cognito-idp:AdminRemoveUserFromGroup",
          "cognito-idp:AdminResetUserPassword",
          "cognito-idp:AdminDeleteUser",
          "cognito-idp:AdminCreateUser",
          "cognito-idp:AdminDisableUser",
          "cognito-idp:AdminEnableUser",
        ],
        resources: [userPool.userPoolArn],
      })
    );
    sessionsTable.grantReadWriteData(uiProxyTaskRole);
    // Read (list/single/resync) + delete (Activity Panel run-cleanup, task
    // "Job/run management") — the proxy never writes/updates job records,
    // only reads and removes them, so we grant exactly those two verbs
    // rather than the broader grantReadWriteData used by the actual job
    // writers (agent runtimes, plan chainer, polling worker).
    jobRegistryTable.grantReadData(uiProxyTaskRole);
    jobRegistryTable.grantWriteData(uiProxyTaskRole);
    // Improve store: read for `GET /improve/data` (global SNAPSHOT + the caller's SUGG#
    // overlay) and write for the dismiss/undo overlay rows (`POST`/`DELETE
    // /improve/suggestions/:id/dismiss`). The proxy writes ONLY per-actor SUGG# rows; the
    // `__system__` SNAPSHOT is written offline by the seeder (and, later, the worker), never
    // by the proxy — GetItem/Query/PutItem/DeleteItem is all these two verbs cover.
    improveTable.grantReadData(uiProxyTaskRole);
    improveTable.grantWriteData(uiProxyTaskRole);
    jobFanoutQueue.grantConsumeMessages(uiProxyTaskRole);
    skillsBucket.grantReadWrite(uiProxyTaskRole);
    // `GET /invoke/:agentId/download?ref=` presigns media-workflow OUTPUTS,
    // which live in the M2C proxy bucket — not skillsBucket. A presigned URL
    // carries the signer's own permissions, so without this the route happily
    // mints a 12h URL that S3 then rejects with AccessDenied on the task role.
    // That failure is invisible to the route (presigning is a local signature,
    // never an API call) and invisible in the browser, where the S3 error is
    // masked as an opaque CORS failure — the link just reads "(failed)".
    // Read-only: the proxy signs GETs, it never writes outputs.
    if (props.envConfig?.media?.proxyBucket) {
      uiProxyTaskRole.addToPolicy(
        new iam.PolicyStatement({
          sid: "MediaWorkflowOutputReadForDownloadRoute",
          actions: ["s3:GetObject"],
          resources: [
            `arn:aws:s3:::${props.envConfig.media.proxyBucket}/*`,
          ],
        })
      );
    }
    // External invoke (`POST /invoke/:agentId`): the proxy queries the
    // byPublishedId GSI to resolve the agent's current published version and
    // its integration_config / external-access gate. Read-only — publishing
    // and revocation are Lab-service writes, never the proxy's.
    labAssetIndexTable.grantReadData(uiProxyTaskRole);
    // /catalog serves the tool list without invoking the agent runtime: gateway
    // tools come from the AgentCore control plane (authoritative — Lambda targets
    // carry their schemas inline) and builtin specs from the projection the
    // runtime publishes into the gateway registry. Read-only on both.
    gatewayRegistryTable.grantReadData(uiProxyTaskRole);
    uiProxyTaskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: [
          "bedrock-agentcore:GetGateway",
          "bedrock-agentcore:ListGatewayTargets",
          "bedrock-agentcore:GetGatewayTarget",
        ],
        resources: ["*"],
      })
    );
    // External jobs read (`GET /invoke/:agentId/jobs`): the proxy reads a
    // completed SFN job's outputs directly (no agent turn) to surface artifact
    // URIs to the external console. Scoped to execution ARNs (same scoping the
    // polling worker uses); a job's existence is still gated by the caller's
    // derived-session filter, so this only ever describes the caller's jobs.
    uiProxyTaskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "DescribeJobExecutions",
        actions: ["states:DescribeExecution"],
        resources: ["arn:aws:states:*:*:execution:*:*"],
      }),
    );

    // Projects (media-harness-projects-access, task 8.10). The proxy owns the Project_Registry and the
    // turn gate: it reads/writes the projects table directly and is the ONLY caller of TokenIssuerFn.
    // It also reads RuntimeConfigSecret to stamp __turn_context. All gated on the nested stack existing
    // (prod leaves PROJECTS_TABLE unset so /projects returns 404 projects_not_enabled).
    if (projectsStack) {
      projectsStack.projectsTable.grantReadWriteData(uiProxyTaskRole);
      uiProxyTaskRole.addToPolicy(
        new iam.PolicyStatement({
          sid: "InvokeProjectTokenIssuer",
          actions: ["lambda:InvokeFunction"],
          resources: [projectsStack.tokenIssuerFn.functionArn],
        }),
      );
      runtimeConfigSecret.grantRead(uiProxyTaskRole);
    }
    // The proxy invokes Asset_Service for the ingestion routes (uploads/assets/s3-locations/urls,
    // design §3). Identity grant here; the resource policy (nested stack) names the proxy role ARN.
    // The proxy is the only caller of the graph reader (`/projects/:pid/graph*`, after checkAccess).
    if (graphStack) {
      uiProxyTaskRole.addToPolicy(
        new iam.PolicyStatement({
          sid: "InvokeGraphReader",
          actions: ["lambda:InvokeFunction"],
          resources: [`arn:aws:lambda:${this.region}:${this.account}:function:${graphReaderFunctionName(projectName)}`],
        }),
      );
    }
    if (ingestionStack) {
      uiProxyTaskRole.addToPolicy(
        new iam.PolicyStatement({
          sid: "InvokeAssetService",
          actions: ["lambda:InvokeFunction"],
          resources: [`arn:aws:lambda:${this.region}:${this.account}:function:${assetServiceFunctionName(projectName)}`],
        }),
      );
    }

    // HMAC key for document VIEW TICKETS (ui/server/artifacts/viewTicket.ts). An agent-written
    // HTML page has to load through /api/artifact (which re-signs the links inside it), and an
    // iframe or a new tab cannot send a header — so the page used to carry the user's Cognito
    // access token in its URL, readable by any script in the page. A ticket grants read of that
    // ONE document for one hour instead. One shared key so every proxy task verifies every ticket
    // and a deploy does not void open documents.
    const artifactViewSecret = new cdk.aws_secretsmanager.Secret(this, "ArtifactViewTicketSecret", {
      description: "HMAC key for /api/artifact document view tickets (proxy only)",
      generateSecretString: { passwordLength: 64, excludePunctuation: true },
    });

    // ORIGIN LOCK for the UiProxy ALB (security finding #2, interim). The ALB is internet-facing on
    // plain HTTP:80 because CloudFront is its only intended client and an ALB on `*.elb.amazonaws.com`
    // cannot present a certificate CloudFront would accept. Two locks keep anything else out:
    //   1. its security group admits ONLY CloudFront's origin-facing managed prefix list (no 0.0.0.0/0);
    //   2. CloudFront adds `X-Origin-Verify: <this secret>` to every origin request, and BOTH services
    //      behind the ALB (the proxy and the Capability API) refuse any non-health request without it
    //      — the prefix list admits every CloudFront distribution, not just ours.
    // The value reaches CloudFront as a `{{resolve:secretsmanager}}` dynamic reference and the tasks as
    // an ECS secret, so it is in neither the template nor any task definition's plaintext env.
    // Follow-up (the real fix): a CloudFront VPC origin on an INTERNAL ALB, which also removes the
    // cleartext CloudFront->ALB hop. VPC origins need the ALB in PRIVATE subnets, and staging/prod run in
    // the account's shared default VPC, which has only public subnets — see AGENTS.md "ALB origin lock".
    const originVerifySecret = new cdk.aws_secretsmanager.Secret(this, "UiOriginVerifySecret", {
      description: "Shared secret CloudFront sends as X-Origin-Verify to the UI proxy ALB (proxy + Capability API)",
      generateSecretString: { passwordLength: 48, excludePunctuation: true },
    });

    const uiProxyService = new ecsPatterns.ApplicationLoadBalancedFargateService(
      this,
      "UiProxyService",
      {
        vpc: uiVpc,
        // No 0.0.0.0/0 ingress: the listener is opened to CloudFront's prefix list below instead.
        openListener: false,
        assignPublicIp: true,
        taskSubnets: { subnetType: ec2.SubnetType.PUBLIC },
        cpu: 512,
        memoryLimitMiB: 1024,
        desiredCount: 1,
        // Rolling deploys spin up a fresh task before tearing down the old
        // one instead of dipping to 0 healthy tasks (CDK's ECS default
        // minHealthyPercent is 50%, which would take the single desired
        // task offline mid-deploy).
        minHealthyPercent: 100,
        maxHealthyPercent: 200,
        // Explicit X86_64 to match GitLab CI's runners (`.gitlab-ci.yml`
        // tags every job `arch:amd64`) and kaniko's build target — the
        // kaniko job never cross-compiles, so a mismatch here would only
        // surface as a silent "exec format error" (exit code 255) task
        // crash with no useful CloudFormation-visible error, which is what
        // happened once already when this was left unset and the image was
        // built locally on an Apple Silicon machine instead.
        runtimePlatform: {
          cpuArchitecture: ecs.CpuArchitecture.X86_64,
          operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
        },
        taskImageOptions: {
          image: uiProxyImage,
          containerPort: 8080,
          taskRole: uiProxyTaskRole,
          // Injected at task start; the execution role gets read on exactly this secret.
          secrets: {
            ARTIFACT_VIEW_SECRET: ecs.Secret.fromSecretsManager(artifactViewSecret),
            // Required on every non-health request (ui/server/originVerify.ts).
            ORIGIN_VERIFY_SECRET: ecs.Secret.fromSecretsManager(originVerifySecret),
          },
          environment: {
            AWS_REGION: this.region,
            APP_VERSION: appVersion,
            // Runtime ARNs come from the SSM parameters under this path, read at startup
            // (`ui/server/runtimeArns.ts`). As GetAtt env they made this task definition wait
            // for the main/task/render runtime updates (~4 min) on every deploy.
            RUNTIME_ARN_PARAM_PATH: runtimeArnParamPath,
            MEMORY_ID: conversationMemory.attrMemoryId,
            SESSIONS_TABLE: sessionsTable.tableName,
            UPLOAD_BUCKET: skillsBucket.bucketName,
            // M2C media-workflow output bucket. The external
            // `GET /invoke/:agentId/download?ref=` route mints a short-lived
            // presigned GET **on demand** for objects here, so replies carry
            // ~196-char refs instead of ~1,700-char presigned URLs. Absent this
            // the bucket allowlist collapses to UPLOAD_BUCKET alone and every
            // media-output ref 403s with `bucket_not_allowed`.
            MEDIA_PROXY_BUCKET: props.envConfig?.media?.proxyBucket ?? "",
            JOB_REGISTRY_TABLE: jobRegistryTable.tableName,
            // Improve store — `GET /improve/data` reads the global SNAPSHOT + the caller's
            // dismiss overlay, and the dismiss/undo writes go here. When this env var is
            // unset the /improve routes return 500 "IMPROVE_TABLE not configured" (the proxy
            // logs it at startup), exactly like /jobs on JOB_REGISTRY_TABLE.
            IMPROVE_TABLE: improveTable.tableName,
            // Lets /catalog read the published builtin projection and the
            // gateway descriptors directly, instead of invoking the runtime.
            GATEWAY_REGISTRY_TABLE: gatewayRegistryTable.tableName,
            JOB_REGISTRY_ACTOR_STATUS_INDEX: jobRegistryActorStatusIndexName,
            JOB_FANOUT_QUEUE_URL: jobFanoutQueue.queueUrl,
            // Lab asset index — `POST /invoke/:agentId` resolves a Custom
            // Agent's current published version via the `byPublishedId` GSI
            // (ui/server/app.ts `resolveCurrentPublishedAgent`). Without
            // this the route fails closed: every external invoke returns
            // 500 internal_error (the proxy logs the misconfiguration at
            // startup). The GSI name is app.ts's default ("byPublishedId"),
            // matching the table definition above.
            ASSET_INDEX_TABLE: labAssetIndexTable.tableName,
            ARTIFACTS_DOMAIN: artifactsDistribution.distributionDomainName,
            // artifactsCdnPrivate: `/api/artifact` stops being public (auth + session-ownership
            // policy), since the artifacts CDN it used to mirror is locked. Absent otherwise.
            ...(artifactsCdnPrivate ? { ARTIFACTS_REQUIRE_AUTH: "true" } : {}),
            // The harness's OWN gateway, so `GET /mcp/servers?live=1` can tell a
            // platform target from a federated gateway's target. Same value the
            // agent reads as HARNESS_GATEWAY_ID; the two paths must agree or the
            // Tools panel's contents change with the read path — M2C's five
            // workflow targets would list as if the user had five MCP servers.
            // Absent, the proxy reports platform rows as unreadable rather than
            // dropping them (absent ≠ empty).
            HARNESS_GATEWAY_ID: gateway.attrGatewayIdentifier,
            // Platform (AWS_IAM) gateway id — plane-aware catalog reporting (Wave 4).
            HARNESS_PLATFORM_GATEWAY_ID: platformGateway.attrGatewayIdentifier,
        HARNESS_PLATFORM_GATEWAY_URL: platformGateway.attrGatewayUrl,
            // JWT auth — required by `auth/validateJwt.ts` when
            // DEV_BYPASS_AUTH is not set (which is correct for staging/prod).
            USER_POOL_ISSUER: userPoolIssuerUrl,
            USER_POOL_CLIENT_ID: userPoolClient.userPoolClientId,
            // Reported by GET /models so the SPA adopts the platform default
            // rather than guessing from list order.
            DEFAULT_MODEL_ID: defaultModelId,
            // Surfaced to the frontend via GET /config so oidc-client-ts's
            // UserManager can be built from a runtime fetch instead of a
            // Vite build-time env var — the same ui/dist artifact then works
            // against any environment's User Pool.
            USER_POOL_ID: userPool.userPoolId,
            // Cognito's hosted-UI /logout endpoint takes proprietary
            // client_id/logout_uri params, not the OIDC-standard
            // id_token_hint/post_logout_redirect_uri pair oidc-client-ts's
            // signoutRedirect() sends — so the frontend builds that URL by
            // hand (see authManager.ts) using this domain instead.
            USER_POOL_DOMAIN: userPoolDomain.baseUrl(),
            // Branding — consumed by GET /config and forwarded to the SPA
            // at runtime so the same build artifact works across envs.
            // Set LOGO_KEY to "random" for rotating logos on each page load.
            LOGO_KEY: "random",
            BRAND_NAME: "Media Agent Harness",
            // Projects turn gate (media-harness-projects-access task 8.10): only where the nested
            // stack exists. Absent in prod, so `/projects` returns 404 projects_not_enabled and the
            // turn gate is skipped. The 1536-byte V2 cap does not apply to the proxy (ECS), so these
            // are plain env vars.
            ...(projectsStack
              ? {
                  PROJECTS_TABLE: projectsStack.projectsTable.tableName,
                  TOKEN_ISSUER_FN: projectsStack.tokenIssuerFn.functionName,
                  RUNTIME_CONFIG_SECRET_ARN: runtimeConfigSecret.secretArn,
                  // The Moments tab's own search (`POST /projects/:pid/moments/search`) calls the Search_Gateway
                  // directly with a Project_Token, like the Main_Agent's Search_Tool.
                  SEARCH_GATEWAY_URL: projectsStack.searchGatewayUrl,
                  SEARCH_TOOL_NAME: projectsStack.searchToolName,
                }
              : {}),
            // media-harness-project-ingestion: the proxy's ingestion routes invoke Asset_Service and
            // enforce per-kind upload limits. ECS env has no V2 1536-byte cap, so these are plain vars.
            ...(ingestionStack
              ? {
                  ASSET_SERVICE_FN: ingestionStack.assetServiceFnName,
                  INGESTION_CONFIG: ingestionConfigJson,
                  ...(generatedMediaQueueUrl ? { GENERATED_MEDIA_QUEUE_URL: generatedMediaQueueUrl } : {}),
                }
              : {}),
            // Knowledge graph: the reader's fixed name (no token from the nested stack) + the feature flag
            // that makes the registry write `graph` and `/config` report `graphEnabled`.
            ...(graphStack
              ? { GRAPH_FN: graphReaderFunctionName(projectName), GRAPH_ENABLED: "true" }
              : {}),
            // Web Search deployed: `/config.webSearchAvailable` and the turn gate (a turn's web_search is forced
            // off when this is absent).
            ...(webSearchCfg ? { WEB_SEARCH_ENABLED: "true" } : {}),
          },
        },
      }
    );
    // ALB target group health check — hits the unauthenticated /health
    // route added to `ui/server/app.ts` for exactly this purpose.
    uiProxyService.targetGroup.configureHealthCheck({ path: "/health" });

    // The ALB admits CloudFront only. The managed prefix list's id differs per region (and partition),
    // so it is resolved at deploy time rather than hard-coded or context-looked-up (a fresh customer
    // account has no cdk.context.json entry). Weight ~55 of the default 60 inbound rules per SG.
    const cloudFrontPrefixList = new cr.AwsCustomResource(this, "CloudFrontOriginFacingPrefixList", {
      onUpdate: {
        service: "EC2",
        action: "describeManagedPrefixLists",
        parameters: {
          Filters: [{ Name: "prefix-list-name", Values: ["com.amazonaws.global.cloudfront.origin-facing"] }],
        },
        physicalResourceId: cr.PhysicalResourceId.of("cloudfront-origin-facing-prefix-list"),
        outputPaths: ["PrefixLists.0.PrefixListId"],
      },
      policy: cr.AwsCustomResourcePolicy.fromSdkCalls({ resources: cr.AwsCustomResourcePolicy.ANY_RESOURCE }),
    });
    uiProxyService.loadBalancer.connections.allowFrom(
      ec2.Peer.prefixList(cloudFrontPrefixList.getResponseField("PrefixLists.0.PrefixListId")),
      ec2.Port.tcp(80),
      "CloudFront origin-facing only (origin lock)",
    );

    // ================================================================
    // Capability API — always-on control plane (`/control-plane/*`)
    // ================================================================
    //
    // A SECOND Fargate service on the SAME cluster, VPC, and ALB listener as
    // the UI proxy, with its own target group and a path rule for
    // `/control-plane/*`. Routing straight to this target group — rather than
    // proxying through `ui/server` — removes an application hop from every
    // read and every CRUD call (R6).
    //
    // Why a service and not a Lambda: R6/R13 require desiredCount >= 1 and no
    // scale-to-zero, so no user request ever waits for container provisioning.
    // The measured alternative was the old path, where a cold Main Runtime
    // invocation cost ~8.6s to list tools.
    //
    // Same image-delivery pattern as the UI proxy: CI (kaniko) pushes to an
    // out-of-band repo and passes `-c capabilityApiImageTag`; without the tag,
    // HarnessAssets built it in CodeBuild for this deployment.
    const capabilityApiRepo = ecr.Repository.fromRepositoryName(
      this,
      "CapabilityApiRepo",
      `${projectName}-capability-api`,
    );
    const capabilityApiImage = capabilityApiImageTagCtx
      ? ecs.ContainerImage.fromEcrRepository(capabilityApiRepo, capabilityApiImageTagCtx)
      : harnessAssets!.capabilityImage!;

    // TEMPORARY unblock: building/pushing the capability-api image needs a one-time bootstrap
    // (the ECR repo `${projectName}-capability-api` must exist AND the GitLab CI role must be
    // granted ecr push on it — its GitLabDeploy policy currently hardcodes only the ui-proxy
    // repo). Until that lands, deploying this service at desiredCount 1 + minHealthyPercent 100
    // hangs `cdk deploy` waiting for ECS to pull an image that was never pushed. Passing
    // `-c capabilityApiEnabled=false` deploys the service at desiredCount 0 — the task def
    // registers, no task launches, so ECS stabilizes immediately and the rest of the stack
    // (including the UI) deploys and is testable. Re-enable by dropping the flag once the ECR
    // repo + push IAM are in place. Default is ENABLED so prod and local synth are unchanged.
    const capabilityApiEnabled =
      this.node.tryGetContext("capabilityApiEnabled") !== "false";

    // ---- Capability query role (reads, plus purge-only deletes) ----
    const capabilityApiTaskRole = new iam.Role(this, "CapabilityApiTaskRole", {
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
      description:
        "Capability API: canonical reads + Registry discovery reads, plus the " +
        "deletes the purge control-plane operation needs. No InvokeAgentRuntime, " +
        "no Registry writes.",
    });
    // Deliberately NO `bedrock-agentcore:InvokeAgentRuntime`. The absence is
    // load-bearing: R1 and Property 5 require that no inventory path can reach
    // the Runtime, and an IAM role that cannot call it is a stronger guarantee
    // than code that currently does not.
    labAssetIndexTable.grantReadData(capabilityApiTaskRole);
    // Built-in Agents panel: the sub-runtime ARNs, read from SSM (string-ARN grant, no runtime edge).
    for (const s of runtimeArnParamReadStatements()) capabilityApiTaskRole.addToPolicy(s);
    labArtifactsBucket.grantRead(capabilityApiTaskRole);
    projectionStateTable.grantReadData(capabilityApiTaskRole);
    gatewayRegistryTable.grantReadData(capabilityApiTaskRole);
    // Authored writes. This process now serves the 35 control-plane lab actions
    // (`capability_api/lab_actions.py`) as well as purge, so it creates, updates
    // and publishes assets — the same `agent/src/lab/` code the Runtime runs,
    // hosted where there is no cold start to pay.
    //
    // Still hand-rolled rather than `grantReadWriteData`: that adds BatchWriteItem,
    // which nothing here issues. ConditionCheckItem and Query already come from
    // grantReadData, and there is no `dynamodb:TransactWriteItems` action —
    // DynamoDB authorizes a transaction against the underlying per-item actions,
    // so DeleteItem + PutItem covers purge's version rows and the outbox row that
    // rides in the same transaction.
    //
    // `UpdateItem` is new here, and it was previously withheld ON PURPOSE, on the
    // grounds that it "would let a read-path bug mutate a published version in
    // place". That reasoning held while this process only ever read and purged; it
    // cannot hold for a host that serves `UpdateSkillMd` and `PublishAsset`.
    // Published-version immutability now rests where it already actually rested
    // for the Runtime — on `lab/_canonical_ops`, which writes versions as new rows
    // under a distinct sort key and never updates one in place — not on the
    // absence of an IAM action in one of the two processes running that code.
    capabilityApiTaskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "WriteLabAssetRows",
        actions: [
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
        ],
        resources: [labAssetIndexTable.tableArn],
      }),
    );
    capabilityApiTaskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "WriteLabArtifacts",
        // s3:ListBucket for the prefix walk already comes from grantRead.
        // PutObject covers authored content — SKILL.md, supporting files, the
        // config JSON a publish copies forward.
        actions: ["s3:PutObject", "s3:DeleteObject"],
        resources: [`${labArtifactsBucket.bucketArn}/*`],
      }),
    );
    capabilityApiTaskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "ListInferenceProfiles",
        // `specialty_crud._validate_model` checks a draft's model against the live
        // catalog (`model_catalog.discover_models`), and Create/UpdateSpecialtyAgentDraft
        // are control-plane actions — so this service performs that check now.
        //
        // Without it `discover_models` degrades to `[]`, which `_validate_model`
        // correctly refuses to read as "no such model" and instead fails closed with
        // "Model validation is temporarily unavailable". So saving an agent with an
        // EXPLICIT model returned that on every attempt, while "Platform default"
        // (model=None, validation skipped) worked — which is why it went unnoticed.
        //
        // Same grant the ui-proxy task role and the agent runtimes already carry;
        // resource `*` because ListInferenceProfiles is not resource-scoped.
        actions: ["bedrock:ListInferenceProfiles"],
        resources: ["*"],
      }),
    );
    capabilityApiTaskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "ReadGatewayHierarchy",
        // Gateway → Target → Tool comes from the control plane, which is
        // authoritative: Lambda targets carry their tool schemas inline, so
        // there is no projection to keep in sync. Resource `*` because gateway
        // ARNs are discovered at request time from the gateway registry rows.
        actions: [
          "bedrock-agentcore:GetGateway",
          "bedrock-agentcore:ListGatewayTargets",
          "bedrock-agentcore:GetGatewayTarget",
        ],
        resources: ["*"],
      }),
    );
    if (registryArn) {
      capabilityApiTaskRole.addToPolicy(
        new iam.PolicyStatement({
          sid: "ReadDiscoverableRegistryRecords",
          // Discovery reads ONLY. No control-plane action, so a bug in the read
          // path cannot create, approve, or deprecate a record.
          actions: [
            "agent-registry:ListDiscoverableRegistryRecords",
            "agent-registry:SearchDiscoverableRegistryRecords",
            "agent-registry:BatchGetDiscoverableRegistryRecord",
          ],
          resources: [registryArn, `${registryArn}/*`],
        }),
      );
    }
    // Improve store — GET /control-plane/improve/data reads the global SNAPSHOT
    // (GetItem) + this actor's dismiss overlay (Query); the dismiss/undo routes
    // write and delete the per-actor SUGG# row (PutItem / DeleteItem). Hand-rolled
    // to exactly these four, matching the least-privilege style above: no UpdateItem
    // (a dismissal is a full-row PutItem, never an in-place mutation) and no index
    // (the table has no GSI). The table itself is defined above (RETAIN + PITR) and
    // is already read/written by the UI proxy.
    capabilityApiTaskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "ImproveStoreReadAndOverlayWrite",
        actions: [
          "dynamodb:GetItem",
          "dynamodb:Query",
          "dynamodb:PutItem",
          "dynamodb:DeleteItem",
        ],
        resources: [improveTable.tableArn],
      }),
    );

    const capabilityApiTaskDef = new ecs.FargateTaskDefinition(
      this,
      "CapabilityApiTaskDef",
      {
        cpu: 512,
        memoryLimitMiB: 1024,
        taskRole: capabilityApiTaskRole,
        // X86_64 for the same reason as the UI proxy: CI builds amd64, and a
        // mismatch surfaces only as an opaque "exec format error" crash loop.
        runtimePlatform: {
          cpuArchitecture: ecs.CpuArchitecture.X86_64,
          operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
        },
      },
    );
    capabilityApiTaskDef.addContainer("api", {
      image: capabilityApiImage,
      portMappings: [{ containerPort: 8080 }],
      // Same origin lock as the proxy: `/control-plane/*` is split off at the ALB, so this service
      // must enforce X-Origin-Verify itself (agent/src/capability_api/origin_verify.py).
      secrets: { ORIGIN_VERIFY_SECRET: ecs.Secret.fromSecretsManager(originVerifySecret) },
      logging: ecs.LogDrivers.awsLogs({ streamPrefix: "capability-api" }),
      environment: {
        AWS_REGION: this.region,
        DEPLOY_ENV: envName,
        APP_VERSION: appVersion,
        ASSET_INDEX_TABLE: labAssetIndexTable.tableName,
        ARTIFACTS_BUCKET: labArtifactsBucket.bucketName,
        GATEWAY_REGISTRY_TABLE: gatewayRegistryTable.tableName,
        PROJECTION_STATE_TABLE: projectionStateTable.tableName,
        // Improve store. Optional: when unset the /control-plane/improve/* routes
        // return 503 (deployment incomplete), never a 500 or a startup ConfigError.
        IMPROVE_TABLE: improveTable.tableName,
        REGISTRY_ID: registryId,
        REGISTRY_NAME: registryName,
        REGISTRY_PROJECTION_ENABLED: projectionEnabled ? "true" : "false",
        REGISTRY_SHADOW_SAMPLE_PERCENT: String(
          registryConfig?.shadowSamplePercent ?? 0,
        ),
        // Same pool and client the UI proxy validates against, so the two front
        // doors cannot disagree about who a caller is.
        USER_POOL_ISSUER: userPoolIssuerUrl,
        USER_POOL_CLIENT_ID: userPoolClient.userPoolClientId,
        // Sub-agent runtime ARNs, so the built-in Agents panel can be served from
        // here instead of waking the Runtime to be told a constant. Only the ARN's
        // last segment is ever surfaced (`runtimeId`); the panel's `configured`
        // badge is `bool(arn)`.
        //
        // These are READ, not invoked — this task deliberately has no
        // `bedrock-agentcore:InvokeAgentRuntime` permission, and that must stay
        // true. `capability.sources` resolves them per call through
        // `runtime_arns.get`, which reads the SSM parameters under this path. They
        // are NOT task env: a GetAtt of a runtime ARN made this task definition wait
        // for four runtime updates (~4 min) on every deploy (docs/ci-speed-proposal.md P1).
        RUNTIME_ARN_PARAM_PATH: runtimeArnParamPath,
        // Per-category cutover flags. Absent entries default to `legacy`, so
        // deploying this service changes no UI behaviour on its own (R14).
        ...capabilitySourceEnv(registryConfig?.capabilitySources),
      },
    });

    const capabilityApiService = new ecs.FargateService(
      this,
      "CapabilityApiService",
      {
        cluster: uiProxyService.cluster,
        taskDefinition: capabilityApiTaskDef,
        // R6/R13: at least one healthy task at all times, and never zero —
        // including mid-deploy, hence minHealthyPercent 100 (CDK's default of
        // 50% would take the single task offline during a rolling update).
        // desiredCount is gated by capabilityApiEnabled: 0 while the image
        // bootstrap is pending (see the flag above) so the deploy doesn't hang.
        desiredCount: capabilityApiEnabled ? 1 : 0,
        minHealthyPercent: 100,
        maxHealthyPercent: 200,
        assignPublicIp: true,
        vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
        healthCheckGracePeriod: cdk.Duration.seconds(60),
      },
    );

    const capabilityApiTargetGroup = new elbv2.ApplicationTargetGroup(
      this,
      "CapabilityApiTargetGroup",
      {
        vpc: uiVpc,
        port: 8080,
        protocol: elbv2.ApplicationProtocol.HTTP,
        targets: [capabilityApiService],
        healthCheck: {
          path: "/health",
          interval: cdk.Duration.seconds(30),
          healthyThresholdCount: 2,
        },
        deregistrationDelay: cdk.Duration.seconds(15),
      },
    );

    // Path rule ahead of the listener's default action (which forwards to the
    // UI proxy). Priority 10 leaves room below for future rules.
    new elbv2.ApplicationListenerRule(this, "ControlPlaneListenerRule", {
      listener: uiProxyService.listener,
      priority: 10,
      conditions: [
        elbv2.ListenerCondition.pathPatterns([
          "/control-plane",
          "/control-plane/*",
        ]),
      ],
      action: elbv2.ListenerAction.forward([capabilityApiTargetGroup]),
    });

    // ---- UI hosting: static SPA (S3 + CloudFront) ----
    //
    // Mirrors the `ArtifactsCdn` pattern above (S3BucketOrigin +
    // OriginAccessControl, default `*.cloudfront.net` domain — no ACM
    // certificate / Route53 / custom domain in this pass): the bucket is
    // fully private (`BLOCK_ALL`); CloudFront is the only path to its
    // contents. A second cache behavior routes every backend path to the
    // `UiProxyService` ALB so the SPA and its API calls are same-origin —
    // the frontend already calls every backend route
    // (`/api`, `/lab`, `/jobs`, `/sessions`, `/memory`, `/catalog`,
    // `/models`, `/render`, `/upload`, `/config`,
    // `/api/artifact`) as relative paths.
    const uiBucket = new s3.Bucket(this, "UiBucket", {
      bucketName: `${projectName}-ui-${this.account}-${this.region}`,
      // Unlike SkillsBucket (irreplaceable user-generated artifacts),
      // this bucket only ever holds `ui/dist` — fully reproducible by
      // `npm run build` + the `UiBucketDeployment` below on every deploy.
      // DESTROY + autoDeleteObjects so a stack rollback/replacement
      // doesn't leave an orphaned bucket blocking the next deploy with
      // an AlreadyExists error (versioning is likewise unnecessary for
      // the same reason — nothing here is worth keeping history of).
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
    });

    const uiApiOrigin = new origins.LoadBalancerV2Origin(uiProxyService.loadBalancer, {
      protocolPolicy: cloudfront.OriginProtocolPolicy.HTTP_ONLY,
      // The origin lock (see UiOriginVerifySecret). CloudFront OVERWRITES a viewer-sent header of the
      // same name, so a client cannot supply its own. A dynamic reference: never plaintext in the template.
      customHeaders: { "X-Origin-Verify": originVerifySecret.secretValue.unsafeUnwrap() },
      // `/jobs/stream` is a long-lived SSE connection. CloudFront's default
      // 30s origin-response timeout would cut an idle stream; the proxy's
      // 20s heartbeat (see the /jobs/stream handler) already keeps bytes
      // flowing under that, but raise the ceiling to CloudFront's 60s max
      // for extra margin. (60s is the cap without an account quota
      // increase.)
      readTimeout: cdk.Duration.seconds(60),
    });
    const uiApiBehavior: cloudfront.BehaviorOptions = {
      origin: uiApiOrigin,
      viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
      cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
      originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
      allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
    };

    // SPA deep-link routing is done with a CloudFront Function on the DEFAULT
    // (S3) behavior ONLY, instead of a distribution-wide 403/404 -> /index.html
    // error rewrite. The old `errorResponses` applied to EVERY behavior, so a
    // legitimate JSON 4xx from an API path (e.g. `DELETE /jobs?runId=` returning
    // 404/409) was rewritten to `200 index.html` AND cached (ErrorCachingMinTTL
    // 300s), which the client saw as "non-JSON response (status 200); the delete
    // route may be unavailable" — and a retry got the cached HTML. Rewriting only
    // extensionless (client-route) URIs to the app shell, on the S3 behavior,
    // preserves deep links while API behaviors return their real status/JSON.
    const spaRewriteFn = new cloudfront.Function(this, "SpaHistoryRewrite", {
      comment: `${projectName} SPA history fallback (extensionless -> /index.html)`,
      code: cloudfront.FunctionCode.fromInline(
        [
          "function handler(event) {",
          "  var request = event.request;",
          "  var uri = request.uri;",
          "  var last = uri.substring(uri.lastIndexOf('/') + 1);",
          "  // No file extension => a client-side route; serve the SPA shell.",
          "  if (last.indexOf('.') === -1) { request.uri = '/index.html'; }",
          "  return request;",
          "}",
        ].join("\n"),
      ),
    });

    // The app SHELL (index.html, and every extensionless path the rewrite serves it for) must revalidate on each
    // load, or a browser keeps a shell that names an old bundle for a year. The S3 object's own header could not be
    // trusted for this: the second BucketDeployment meant to re-upload index.html with `no-cache` is an `s3 sync`,
    // which skips a file whose size and (zip-preserved) timestamp look unchanged, so index.html kept the assets'
    // `immutable` header (staging 2026-10-16: deployed fixes invisible until a hard refresh). The browser-facing
    // header is therefore set HERE, at the edge; `/assets/*` (content-hashed) keeps the immutable one from S3.
    const uiShellHeaders = new cloudfront.ResponseHeadersPolicy(this, "UiShellNoCache", {
      comment: `${projectName} SPA shell: always revalidate`,
      customHeadersBehavior: {
        customHeaders: [{ header: "Cache-Control", value: "no-cache, max-age=0, must-revalidate", override: true }],
      },
    });
    const uiBucketOrigin = origins.S3BucketOrigin.withOriginAccessControl(uiBucket);
    const uiDistribution = new cloudfront.Distribution(this, "UiDistribution", {
      defaultBehavior: {
        origin: uiBucketOrigin,
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
        responseHeadersPolicy: uiShellHeaders,
        functionAssociations: [
          { function: spaRewriteFn, eventType: cloudfront.FunctionEventType.VIEWER_REQUEST },
        ],
      },
      // CloudFront path patterns are exact: `/lab/*` matches `/lab/x` but NOT
      // the bare `/lab`, so every API path the frontend calls without a
      // trailing segment needs its own bare entry alongside the `/*` one, or
      // the request falls through to the S3/SPA default behavior and returns
      // index.html (surfacing as "Unexpected token '<' ... is not valid
      // JSON"). The Skills/Agents/Hooks tabs POST to the bare `/lab`
      // (labAction in ui/src/lab-api.ts), so it MUST be listed explicitly.
      additionalBehaviors: {
        // Content-hashed bundles: cached hard (their S3 header is `immutable`), without the shell header.
        "/assets/*": {
          origin: uiBucketOrigin,
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
          cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
        },
        "/api/*": uiApiBehavior,
        "/lab": uiApiBehavior,
        "/lab/*": uiApiBehavior,
        "/jobs": uiApiBehavior,
        "/jobs/*": uiApiBehavior,
        "/sessions": uiApiBehavior,
        "/sessions/*": uiApiBehavior,
        // Project_Registry routes (ui/server/projects/routes.ts). Bare entry
        // for `GET|POST /projects`, wildcard for `/projects/:pid/...`.
        "/projects": uiApiBehavior,
        "/projects/*": uiApiBehavior,
        "/admin": uiApiBehavior,
        "/admin/*": uiApiBehavior,
        "/memory": uiApiBehavior,
        "/memory/*": uiApiBehavior,
        "/catalog": uiApiBehavior,
        "/models": uiApiBehavior,
        "/render": uiApiBehavior,
        "/upload": uiApiBehavior,
        "/config": uiApiBehavior,
        // Direct timeline actions (`POST /media/actions`: Frame / Clip / Crop with no model turn,
        // ui/server/mediaActions.ts). Unlisted, it would get index.html from the SPA bucket.
        "/media/*": uiApiBehavior,
        // MCP server listing for the Tools panel. Same reason as every entry
        // here: without an explicit behavior this path falls to the default
        // behavior, is served index.html from the SPA bucket with a 200, and
        // the caller sees valid-looking HTML instead of JSON.
        "/mcp": uiApiBehavior,
        "/mcp/*": uiApiBehavior,
        // Owner-side Custom Agent test invoke. Needs an explicit behavior like
        // every other API path: the default behavior serves the SPA bucket, so
        // an unlisted path returns index.html instead of reaching the proxy —
        // which looks like the agent replying with raw HTML.
        "/agent-test": uiApiBehavior,
        // Improve surface read (`GET /improve/data`). Proxy-served through
        // ui/server like /jobs — NOT split off at the ALB the way
        // /control-plane is. Two EXACT patterns, never a `/improve*` glob: a
        // glob would also swallow the SPA's own `/improve-data.json` (the old
        // static file), whereas `/improve/*` does not match `/improve-data.json`
        // because that path starts with `/improve-`, not `/improve/`. Without
        // these the path falls to the default behavior and returns index.html
        // from the SPA bucket with a 200 — HTML where the SPA expects JSON.
        "/improve": uiApiBehavior,
        "/improve/*": uiApiBehavior,
        // External Custom Agent invoke (`POST /invoke/{agentId}`) — the only
        // path external systems call. Routed through CloudFront so callers
        // get HTTPS at the edge (the ALB listener itself is plain HTTP and
        // must not carry bearer tokens directly). No bare `/invoke` entry:
        // the route always carries an agentId path segment.
        "/invoke/*": uiApiBehavior,
        // Capability API. Same ALB origin as every other API path — the ALB's
        // own listener rule (priority 10) is what splits `/control-plane/*` off
        // to the Capability API target group, so CloudFront needs no separate
        // origin. Without these two entries the path falls through to the
        // default behavior, is served index.html from the SPA bucket with a
        // 200, and the SPA sees valid-looking HTML instead of JSON.
        "/control-plane": uiApiBehavior,
        "/control-plane/*": uiApiBehavior,
      },
      // SPA client-side routing is handled by `spaRewriteFn` (a CloudFront
      // Function on the DEFAULT behavior that rewrites extensionless URIs to
      // /index.html), NOT by distribution-wide errorResponses. errorResponses
      // apply to the API behaviors too, so a 403/404 from the proxy (a real
      // JSON API error) would be rewritten to `200 index.html` and cached —
      // exactly what broke DELETE /jobs?runId= and surfaced HTML where the SPA
      // expected JSON. With the function, deep links serve the shell and API
      // paths return their real status untouched.
      defaultRootObject: "index.html",
      comment: `${projectName} UI CDN`,
    });

    // Cutover ordering for the origin lock: CloudFormation finishes a Distribution update only once it
    // is Deployed at every edge, so making both services depend on it means a task that ENFORCES
    // X-Origin-Verify never starts while some edge still forwards without it. (Costs a wait only on a
    // deploy that changes the distribution.)
    uiProxyService.service.node.addDependency(uiDistribution);
    capabilityApiService.node.addDependency(uiDistribution);

    // The SPA: built by HarnessAssets' UI CodeBuild project when it built the proxy image, else the
    // prebuilt `ui/dist` (CI's `build` job, or `cd ui && npm run build` by hand).
    const spaSource = harnessAssets?.uiSource ?? s3deploy.Source.asset(path.join(__dirname, "../../ui/dist"));

    const uiAssetsDeployment = new s3deploy.BucketDeployment(this, "UiBucketDeployment", {
      sources: [spaSource],
      destinationBucket: uiBucket,
      distribution: uiDistribution,
      distributionPaths: ["/*"],
      // Hashed JS/CSS are content-addressed (Vite emits `assets/index-<hash>.js`),
      // so a name never changes contents — safe to cache hard and skip revalidation.
      cacheControl: [s3deploy.CacheControl.fromString("public, max-age=31536000, immutable")],
    });

    // index.html is the app SHELL: it is NOT content-hashed and it references the
    // current hashed bundle by name, so it MUST revalidate on every load. Before
    // this it inherited NO Cache-Control, so the browser (and CloudFront) served a
    // stale shell pointing at an old, since-purged bundle — the recurring
    // "my UI fix didn't deploy" / stale-bundle bug (reported for the sidebar
    // indent, then the divider, twice). Re-uploaded AFTER the assets deployment
    // (addDependency) with prune:false so this no-cache header wins on index.html
    // while the immutable header stays on the hashed assets.
    const uiIndexDeployment = new s3deploy.BucketDeployment(this, "UiIndexNoCache", {
      sources: [spaSource],
      destinationBucket: uiBucket,
      prune: false,
      exclude: ["*"],
      include: ["index.html"],
      cacheControl: [s3deploy.CacheControl.fromString("no-cache, max-age=0, must-revalidate")],
    });
    uiIndexDeployment.node.addDependency(uiAssetsDeployment);

    // ---- Wire Cognito callback/logout URLs to the now-known CloudFront
    // domain (staging/prod only — dev keeps its localhost defaults) ----
    //
    // `userPoolCallbackUrls`/`userPoolLogoutUrls` default to
    // `http://localhost:3000/...` (see the constructor prop defaults near
    // `UserPool`/`LabUIClient` above) because that's the only URL known at
    // the time those constructs are created — `UiDistribution` (this
    // stack's actual hosted origin) doesn't exist yet within the same
    // linear construction order. Rather than reorder the whole stack
    // (`UserPool` feeds `USER_POOL_ISSUER`/`USER_POOL_CLIENT_ID` into the
    // ECS task env vars created earlier), override the underlying
    // `AWS::Cognito::UserPoolClient`'s `CallbackURLs`/`LogoutURLs`
    // properties directly via the L1 escape hatch once the real domain is
    // known. This only takes effect for staging/prod — `app.ts` never
    // passes `userPoolCallbackUrls`/`userPoolLogoutUrls` today, so every
    // environment hits this override; if a future custom domain is added,
    // swap `uiDistribution.distributionDomainName` for that domain here.
    const uiOrigin = `https://${uiDistribution.distributionDomainName}`;
    const cfnUserPoolClient = userPoolClient.node.defaultChild as cognito.CfnUserPoolClient;
    cfnUserPoolClient.callbackUrLs = [`${uiOrigin}/auth/callback`];
    cfnUserPoolClient.logoutUrLs = [`${uiOrigin}/`];

    // ---- Outputs ----
    new cdk.CfnOutput(this, "UiDistributionId", {
      value: uiDistribution.distributionId,
      description:
        "CloudFront distribution ID for the hosted Lab UI — the " +
        "UiBucketDeployment CDK construct invalidates it automatically on " +
        "every `cdk deploy`; this output is for manual debugging only",
    });

    new cdk.CfnOutput(this, "UiDistributionDomainName", {
      value: uiDistribution.distributionDomainName,
      description: "CloudFront domain serving the hosted Lab UI",
    });

    // The Main Agent's project tools (list/switch/create) call the proxy's own `/projects` API with the
    // turn's forwarded user token, through CloudFront because the ALB is origin-locked
    // (agent/src/project_search/registry_client.py). An SSM parameter under the runtime-ARN path, read
    // lazily (runtime_arns.get), rather than runtime env or RuntimeConfigSecret, so this value adds no
    // deploy-ordering edge to any runtime.
    new ssm.StringParameter(this, "UiPublicOriginParam", {
      parameterName: `${runtimeArnParamPath}/UI_PUBLIC_ORIGIN`,
      stringValue: `https://${uiDistribution.distributionDomainName}`,
      description: "Public https origin of the hosted UI, read by the agent as UI_PUBLIC_ORIGIN",
    });

    // ---- The platform's Bedrock guardrail (docs/superpowers/specs/2026-11-09-bedrock-guardrails-design.md) ----
    // ONE guardrail for the whole platform. An admin picks it in Settings -> Safety (the proxy writes
    // GUARDRAIL_SELECTED, which this stack never manages, so a deploy never overwrites the choice); until then the
    // default below applies. The Main and Task agents read both at most once a minute (agent/src/model/guardrail.py).
    const guardrailSelectedParam = `${runtimeArnParamPath}/GUARDRAIL_SELECTED`;
    const guardrailDefaultParam = `${runtimeArnParamPath}/GUARDRAIL_DEFAULT`;
    if (props.envConfig?.defaultGuardrail) {
      // Light, for a media harness whose film analysis and moderation reports describe violent or adult scenes:
      // prompt attacks blocked at HIGH on input; other categories only at LOW (clearly harmful). No topic/PII filters.
      const contentFilters = ["SEXUAL", "VIOLENCE", "HATE", "INSULTS", "MISCONDUCT"].map((type) => ({
        type, inputStrength: "LOW", outputStrength: "LOW",
      }));
      const contentPolicyConfig = {
        filtersConfig: [...contentFilters, { type: "PROMPT_ATTACK", inputStrength: "HIGH", outputStrength: "NONE" }],
      };
      const guardrail = new bedrock.CfnGuardrail(this, "DefaultGuardrail", {
        name: `${projectName}-${envName}-default`,
        description: "Media Agent Harness default guardrail: blocks prompt attacks and clearly harmful content",
        blockedInputMessaging: "This request was blocked by the platform's content guardrail.",
        blockedOutputsMessaging: "This response was blocked by the platform's content guardrail.",
        contentPolicyConfig,
      });
      // A version is immutable, so a policy change publishes a NEW version: the logical id follows the policy.
      const policyHash = require("crypto").createHash("sha256")
        .update(JSON.stringify(contentPolicyConfig)).digest("hex").slice(0, 8);
      const guardrailVersion = new bedrock.CfnGuardrailVersion(this, `DefaultGuardrailVersion${policyHash}`, {
        guardrailIdentifier: guardrail.attrGuardrailId,
        description: `policy ${policyHash}`,
      });
      new ssm.StringParameter(this, "GuardrailDefaultParam", {
        parameterName: guardrailDefaultParam,
        stringValue: cdk.Stack.of(this).toJsonString({
          id: guardrail.attrGuardrailId,
          version: guardrailVersion.attrVersion,
          name: `${projectName}-${envName}-default`,
        }),
        description: "The platform's default Bedrock guardrail (JSON {id, version, name}), read as GUARDRAIL_DEFAULT",
      });
    }
    // Applying a guardrail to Converse needs bedrock:ApplyGuardrail. Any guardrail in this account (an admin may pick
    // one made in the console), on the two runtimes that bind it. Reading the setting is the runtime-ARN path grant.
    for (const rt of [agentRuntime, taskAgentRuntime]) {
      rt.addToRolePolicy(new iam.PolicyStatement({
        sid: "ApplyPlatformGuardrail",
        actions: ["bedrock:ApplyGuardrail"],
        resources: [`arn:aws:bedrock:${this.region}:${this.account}:guardrail/*`],
      }));
    }
    // The proxy lists the account's guardrails for the admin dropdown and writes the admin's pick.
    uiProxyTaskRole.addToPolicy(new iam.PolicyStatement({
      sid: "ListGuardrails",
      actions: ["bedrock:ListGuardrails", "bedrock:GetGuardrail"],
      resources: ["*"],
    }));
    uiProxyTaskRole.addToPolicy(new iam.PolicyStatement({
      sid: "GuardrailSetting",
      actions: ["ssm:GetParameters", "ssm:GetParameter", "ssm:PutParameter"],
      resources: [
        `arn:aws:ssm:${this.region}:${this.account}:parameter${guardrailSelectedParam}`,
        `arn:aws:ssm:${this.region}:${this.account}:parameter${guardrailDefaultParam}`,
      ],
    }));

    new cdk.CfnOutput(this, "UiBucketName", {
      value: uiBucket.bucketName,
      description: "S3 bucket holding the built ui/dist/ SPA assets",
    });

    new cdk.CfnOutput(this, "UiProxyServiceUrl", {
      value: `http://${uiProxyService.loadBalancer.loadBalancerDnsName}`,
      description:
        "UI proxy ALB (origin-locked: CloudFront prefix list + X-Origin-Verify) — NOT reachable " +
        "directly; use UiDistributionDomainName for every request, health checks included",
    });

    new cdk.CfnOutput(this, "UiProxyRepoUri", {
      value: uiProxyRepo.repositoryUri,
      description:
        "ECR repository for the agentcore-proxy container image — the " +
        "build:proxy-image CI job (kaniko) pushes here; pass the pushed " +
        "tag to `cdk deploy` via `-c uiProxyImageTag=<tag>`",
    });

    new cdk.CfnOutput(this, "SkillsBucketName", {
      value: skillsBucket.bucketName,
    });

    new cdk.CfnOutput(this, "UploadsBucketName", {
      value: uploadsBucket.bucketName,
      description: "Legacy auxiliary uploads bucket retained for runtime compatibility",
    });

    new cdk.CfnOutput(this, "SkillsAccessPolicyArn", {
      value: skillsAccessPolicy.managedPolicyArn,
    });

    new cdk.CfnOutput(this, "McpRuntimeArn", {
      value: mcpRuntime.agentRuntimeArn,
    });

    new cdk.CfnOutput(this, "GatewayUrl", {
      value: gateway.attrGatewayUrl,
    });

    new cdk.CfnOutput(this, "GatewayArn", {
      value: gateway.attrGatewayArn,
    });

    new cdk.CfnOutput(this, "M2MPoolId", {
      value: m2mPool.userPoolId,
    });

    new cdk.CfnOutput(this, "M2MClientId", {
      value: m2mClient.userPoolClientId,
    });

    // ---- User pool (Lab UI human auth) outputs ----
    new cdk.CfnOutput(this, "UserPoolId", {
      value: userPool.userPoolId,
      description: "Cognito user pool ID for Lab UI human auth",
    });

    new cdk.CfnOutput(this, "UserPoolClientId", {
      value: userPoolClient.userPoolClientId,
      description: "Cognito user pool app client ID (Lab UI hosted sign-in)",
    });

    new cdk.CfnOutput(this, "AsyncPlanClientId", {
      value: asyncPlanClient.userPoolClientId,
      description:
        "Cognito user pool client_credentials client ID for async plan " +
        "delegated gateway access (Wave 1 / option E). In the gateway's " +
        "allowedClients; the Task Agent mints a token with it.",
    });

    new cdk.CfnOutput(this, "AsyncPlanTokenEndpoint", {
      value: asyncPlanTokenEndpoint,
      description:
        "OAuth2 token endpoint the Task Agent uses to mint the async-plan " +
        "machine token (client_credentials).",
    });

    new cdk.CfnOutput(this, "UserPoolDomain", {
      value: userPoolDomain.domainName,
      description:
        "Cognito hosted UI domain prefix (full URL is " +
        "https://<prefix>.auth.<region>.amazoncognito.com)",
    });

    new cdk.CfnOutput(this, "UserPoolIssuer", {
      value: userPoolIssuerUrl,
      description:
        "Cognito user pool issuer URL (used for JWT validation; OpenID " +
        "discovery doc is at <issuer>/.well-known/openid-configuration)",
    });

    new cdk.CfnOutput(this, "M2MTokenEndpoint", {
      value: `https://${m2mDomain.domainName}.auth.${this.region}.amazoncognito.com/oauth2/token`,
    });

    new cdk.CfnOutput(this, "CredentialProviderArn", {
      value: credProviderArn,
    });

    new cdk.CfnOutput(this, "M2MScope", {
      value: m2mScope,
    });

    new cdk.CfnOutput(this, "CredProviderName", {
      value: credProviderName,
    });

    new cdk.CfnOutput(this, "GatewayId", {
      value: gateway.attrGatewayIdentifier,
    });

    new cdk.CfnOutput(this, "AgentRuntimeArn", {
      value: agentRuntime.agentRuntimeArn,
      description: "Main agent runtime ARN",
    });

    new cdk.CfnOutput(this, "AgentRuntimeId", {
      value: agentRuntime.agentRuntimeId,
    });

    new cdk.CfnOutput(this, "TaskAgentRuntimeArn", {
      value: taskAgentRuntime.agentRuntimeArn,
      description: "Task sub-agent runtime ARN",
    });

    new cdk.CfnOutput(this, "CodeInterpreterId", {
      value: codeInterpreter.codeInterpreterId,
      description: "Code interpreter ID",
    });

    new cdk.CfnOutput(this, "PlanningAgentRuntimeArn", {
      value: planningAgentRuntime.agentRuntimeArn,
      description: "Planning agent runtime ARN",
    });

    new cdk.CfnOutput(this, "RenderAgentRuntimeArn", {
      value: renderAgentRuntime.agentRuntimeArn,
      description: "Render agent runtime ARN (artifact-render-agent)",
    });

    new cdk.CfnOutput(this, "PlanStateTableName", {
      value: planStateTable.tableName,
      description: "DynamoDB plan state table name",
    });

    new cdk.CfnOutput(this, "SessionsTableName", {
      value: sessionsTable.tableName,
      description: "DynamoDB sessions metadata table name",
    });

    // ---- Job Registry outputs ----
    //
    // Surfaced for downstream tasks in the async-job-tracking spec:
    //   * Polling_Worker Lambda — needs the table ARN and the
    //     `poll-partition-index` name to query active jobs.
    //   * ChangefeedFanout Lambda — needs the stream ARN to subscribe
    //     to `INSERT` / `MODIFY` events.
    //   * agentcore-proxy `/jobs` and `/jobs/stream` handlers — need
    //     the table ARN and `actor-status-index` name for the
    //     listing/single-record query paths.
    new cdk.CfnOutput(this, "JobRegistryTableName", {
      value: jobRegistryTable.tableName,
      description: "DynamoDB job registry table name (async-job-tracking)",
    });

    new cdk.CfnOutput(this, "JobRegistryTableArn", {
      value: jobRegistryTable.tableArn,
      description: "DynamoDB job registry table ARN (async-job-tracking)",
    });

    new cdk.CfnOutput(this, "JobRegistryStreamArn", {
      // Streams are enabled (NEW_AND_OLD_IMAGES) so this is non-null.
      value: jobRegistryTable.tableStreamArn ?? "",
      description:
        "DynamoDB job registry stream ARN — consumed by ChangefeedFanout Lambda",
    });

    new cdk.CfnOutput(this, "JobRegistryActorStatusIndexName", {
      value: jobRegistryActorStatusIndexName,
      description:
        "GSI name for actor-scoped job listing (PK actorId, SK updatedAt)",
    });

    new cdk.CfnOutput(this, "JobRegistryPollPartitionIndexName", {
      value: jobRegistryPollPartitionIndexName,
      description:
        "GSI name for polling-worker active-job scan (PK pollPartition, SK updatedAt)",
    });

    new cdk.CfnOutput(this, "MemoryId", {
      value: conversationMemory.attrMemoryId,
      description: "AgentCore Memory ID for Main Agent conversation history",
    });

    new cdk.CfnOutput(this, "LabAssetIndexTableName", {
      value: labAssetIndexTable.tableName,
      description: "Lab Asset_Index DynamoDB table name",
    });

    new cdk.CfnOutput(this, "LabArtifactsBucketName", {
      value: labArtifactsBucket.bucketName,
      description: "Lab Artifacts_Bucket S3 bucket name",
    });

    // ---- Capability control plane ----

    new cdk.CfnOutput(this, "CapabilityApiRepositoryUri", {
      value: capabilityApiRepo.repositoryUri,
      description:
        "ECR repo for the Capability API image. Build with agent/capability.Dockerfile " +
        "and pass the tag via `-c capabilityApiImageTag=<tag>`",
    });

    new cdk.CfnOutput(this, "CapabilityApiServiceName", {
      value: capabilityApiService.serviceName,
      description:
        "Always-on Capability API ECS service (serves /control-plane/*)",
    });

    new cdk.CfnOutput(this, "RegistryProjectionStateTableName", {
      value: projectionStateTable.tableName,
      description: "Registry projection-state DynamoDB table name",
    });

    new cdk.CfnOutput(this, "RegistryProjectorDlqUrl", {
      value: projectorDlq.queueUrl,
      description:
        "Registry projector DLQ. Non-zero depth means projection is failing; " +
        "canonical publishes are unaffected",
    });

    new cdk.CfnOutput(this, "RegistryProjectorFunctionName", {
      value: registryProjector.functionName,
      description: "Registry projection worker Lambda",
    });

    new cdk.CfnOutput(this, "AgentRegistryId", {
      value: registryId || "(not provisioned)",
      description:
        "Harness-shared AWS Agent Registry id. Provision with " +
        "`agent/scripts/registry_provision.py --env <env> --execute`, then paste " +
        "into infra/lib/env-config.ts",
    });

    new cdk.CfnOutput(this, "RegistryReconciliationRoleArn", {
      value: reconciliationRole.roleArn,
      description:
        "Read-only role for reconciling canonical state against the Registry projection",
    });

    new cdk.CfnOutput(this, "LabRoleArn", {
      value: labRole.role.roleArn,
      description:
        "Lab_Role ARN — prefix-scoped IAM role for Lab_Mode requests (forward-compatible with AgentCore-proxy session tagging)",
    });

    new cdk.CfnOutput(this, "ChatRoleArn", {
      value: chatRole.role.roleArn,
      description:
        "Chat_Role ARN — read-only prefix-scoped IAM role for Chat_Mode requests (no drafts access, no writes; forward-compatible with AgentCore-proxy session tagging)",
    });

    new cdk.CfnOutput(this, "ProjectRuntimeRoleArn", {
      value: projectRuntimeRole.role.roleArn,
      description:
        "Project_Runtime_Role ARN — read-only prefix-scoped IAM role for Project_Mode runs (no users/* access at all; group bindings are project-specific and statically emitted at synthesis time; forward-compatible with AgentCore-proxy session tagging)",
    });
  }
}

// ============================================================================
// Lab_Role construct
// ============================================================================
//
// Per design.md (IAM section) and Requirements 10.1, 10.2 of the
// unified-mediaworks-platform spec, Lab_Role is the prefix-scoped IAM role
// assumed by Lab_Mode requests. It binds S3 permissions to the actor's
// `userId`, `orgId`, and `groups` via PrincipalTag-templated resource ARNs.
//
// Group access:
//   IAM does not natively support `Resource` patterns parameterized over a
//   multi-value session tag, so this construct STATICALLY EXPANDS group access
//   into one `groups/<g>/published/*` Resource statement per group ID known at
//   role-attachment (synthesis) time. The illustrative `s3:prefix-segment-2`
//   condition described in the design is intentionally NOT used here.
//   Group membership changes therefore require a CDK redeploy.
//
// Runtime tagging note:
//   Production runtime session tagging — i.e. injecting `userId`, `orgId`, and
//   `groups` at AssumeRole time from JWT claims — is deferred to the
//   AgentCore-proxy follow-on. This construct is forward-compatible with that
//   work: the policy statements already reference `${aws:PrincipalTag/userId}`
//   and `${aws:PrincipalTag/orgId}`, and the role's trust policy can be
//   tightened in the follow-on without altering these grants. Until the proxy
//   work lands, application-level checks in `list_visible_assets` are the
//   authoritative gate; IAM is defense-in-depth.

export interface LabRoleProps {
  /** The Lab artifacts bucket whose ARN scopes all S3 statements. */
  readonly artifactsBucket: s3.IBucket;
  /**
   * Group IDs to grant `groups/<g>/published/*` read access to. Statically
   * expanded into one Resource statement per ID at synthesis time. Defaults
   * to an empty list (no group access).
   */
  readonly groupIds?: string[];
  /**
   * Optional override for the role's trust principal. Defaults to the
   * deploying account's root, which is a permissive placeholder suitable
   * pre-AgentCore-proxy. The proxy follow-on will tighten this.
   */
  readonly assumedBy?: iam.IPrincipal;
}

export class LabRole extends Construct {
  public readonly role: iam.Role;

  constructor(scope: Construct, id: string, props: LabRoleProps) {
    super(scope, id);

    const groupIds = props.groupIds ?? [];
    const bucketArn = props.artifactsBucket.bucketArn;

    // PrincipalTag placeholders are written as literal strings in the policy
    // document; CloudFormation/IAM resolves them at request time. Note the
    // backslash escape to prevent TS template-literal interpolation.
    const userIdTag = "${aws:PrincipalTag/userId}";
    const orgIdTag = "${aws:PrincipalTag/orgId}";

    this.role = new iam.Role(this, "Role", {
      // Permissive placeholder trust — tightened by the AgentCore-proxy
      // follow-on. AccountRootPrincipal allows STS delegation within the
      // account; production traffic will not exercise this role until the
      // proxy injects session tags via AssumeRole.
      assumedBy: props.assumedBy ?? new iam.AccountRootPrincipal(),
      description:
        "Lab_Mode prefix-scoped role (forward-compatible with AgentCore-proxy " +
        "session tagging; group access is statically expanded at synth time)",
    });

    // OwnDraftsRW — the actor's own draft prefix
    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: "OwnDraftsRW",
        effect: iam.Effect.ALLOW,
        actions: ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
        resources: [`${bucketArn}/users/${userIdTag}/drafts/*`],
      })
    );

    // OwnPrivatePublishedRW — the actor's own private-scoped published prefix
    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: "OwnPrivatePublishedRW",
        effect: iam.Effect.ALLOW,
        actions: ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
        resources: [`${bucketArn}/users/${userIdTag}/published/*`],
      })
    );

    // OrgPublishedRead — read of the actor's org-scoped published prefix
    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: "OrgPublishedRead",
        effect: iam.Effect.ALLOW,
        actions: ["s3:GetObject"],
        resources: [`${bucketArn}/org/${orgIdTag}/published/*`],
      })
    );

    // GroupPublishedRead — one Resource statement per known group ID.
    // Static expansion: IAM cannot parameterize Resource over a multi-value
    // session tag, so we emit explicit ARNs at synthesis time.
    if (groupIds.length > 0) {
      this.role.addToPolicy(
        new iam.PolicyStatement({
          sid: "GroupPublishedRead",
          effect: iam.Effect.ALLOW,
          actions: ["s3:GetObject"],
          resources: groupIds.map(
            (g) => `${bucketArn}/groups/${g}/published/*`
          ),
        })
      );
    }

    // ListBucketScoped — the actor can list only the prefixes they have
    // object-level access to. Group prefixes are statically expanded; the
    // own/org prefixes use PrincipalTag templating.
    const listPrefixes: string[] = [
      `users/${userIdTag}/*`,
      `org/${orgIdTag}/published/*`,
      ...groupIds.map((g) => `groups/${g}/published/*`),
    ];

    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: "ListBucketScoped",
        effect: iam.Effect.ALLOW,
        actions: ["s3:ListBucket"],
        resources: [bucketArn],
        conditions: {
          StringLike: {
            "s3:prefix": listPrefixes,
          },
        },
      })
    );
  }
}

// ============================================================================
// Chat_Role construct
// ============================================================================
//
// Per design.md (IAM section / "Chat_Role") and Requirement 10.3 of the
// unified-mediaworks-platform spec, Chat_Role is the read-only prefix-scoped
// IAM role assumed by Chat_Mode requests. It is identical to Lab_Role except:
//
//   * Every `drafts/*` statement is removed.
//   * `PutObject` and `DeleteObject` on `users/<u>/published/*` are removed.
//
// Result: read-only `s3:GetObject` across
//   - `users/${aws:PrincipalTag/userId}/published/*`
//   - `groups/<g>/published/*`           (statically expanded per group ID)
//   - `org/${aws:PrincipalTag/orgId}/published/*`
//
// plus a matching `s3:ListBucket` grant constrained by `s3:prefix` to the
// same set. There is no `drafts/*` resource pattern anywhere in this policy.
//
// Group access:
//   IAM does not natively support `Resource` patterns parameterized over a
//   multi-value session tag, so this construct STATICALLY EXPANDS group access
//   into one `groups/<g>/published/*` Resource statement per group ID known at
//   role-attachment (synthesis) time. Group membership changes therefore
//   require a CDK redeploy.
//
// Runtime tagging note:
//   Production runtime session tagging — i.e. injecting `userId`, `orgId`, and
//   `groups` at AssumeRole time from JWT claims — is deferred to the
//   AgentCore-proxy follow-on. This construct is forward-compatible with that
//   work: the policy statements already reference `${aws:PrincipalTag/userId}`
//   and `${aws:PrincipalTag/orgId}`, and the role's trust policy can be
//   tightened in the follow-on without altering these grants. Until the proxy
//   work lands, application-level checks in `list_visible_assets` are the
//   authoritative gate; IAM is defense-in-depth.

export interface ChatRoleProps {
  /** The Lab artifacts bucket whose ARN scopes all S3 statements. */
  readonly artifactsBucket: s3.IBucket;
  /**
   * Group IDs to grant `groups/<g>/published/*` read access to. Statically
   * expanded into one Resource statement per ID at synthesis time. Defaults
   * to an empty list (no group access).
   */
  readonly groupIds?: string[];
  /**
   * Optional override for the role's trust principal. Defaults to the
   * deploying account's root, which is a permissive placeholder suitable
   * pre-AgentCore-proxy. The proxy follow-on will tighten this.
   */
  readonly assumedBy?: iam.IPrincipal;
}

export class ChatRole extends Construct {
  public readonly role: iam.Role;

  constructor(scope: Construct, id: string, props: ChatRoleProps) {
    super(scope, id);

    const groupIds = props.groupIds ?? [];
    const bucketArn = props.artifactsBucket.bucketArn;

    // PrincipalTag placeholders are written as literal strings in the policy
    // document; CloudFormation/IAM resolves them at request time. Note the
    // backslash escape to prevent TS template-literal interpolation.
    const userIdTag = "${aws:PrincipalTag/userId}";
    const orgIdTag = "${aws:PrincipalTag/orgId}";

    this.role = new iam.Role(this, "Role", {
      // Permissive placeholder trust — tightened by the AgentCore-proxy
      // follow-on. AccountRootPrincipal allows STS delegation within the
      // account; production traffic will not exercise this role until the
      // proxy injects session tags via AssumeRole.
      assumedBy: props.assumedBy ?? new iam.AccountRootPrincipal(),
      description:
        "Chat_Mode read-only prefix-scoped role (no drafts access, no writes; " +
        "forward-compatible with AgentCore-proxy session tagging; group " +
        "access is statically expanded at synth time)",
    });

    // OwnPublishedRead — read of the actor's own private-scoped published prefix.
    // Note: no drafts access, no writes anywhere — this is the key delta from Lab_Role.
    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: "OwnPublishedRead",
        effect: iam.Effect.ALLOW,
        actions: ["s3:GetObject"],
        resources: [`${bucketArn}/users/${userIdTag}/published/*`],
      })
    );

    // OrgPublishedRead — read of the actor's org-scoped published prefix
    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: "OrgPublishedRead",
        effect: iam.Effect.ALLOW,
        actions: ["s3:GetObject"],
        resources: [`${bucketArn}/org/${orgIdTag}/published/*`],
      })
    );

    // GroupPublishedRead — one Resource statement per known group ID.
    // Static expansion: IAM cannot parameterize Resource over a multi-value
    // session tag, so we emit explicit ARNs at synthesis time.
    if (groupIds.length > 0) {
      this.role.addToPolicy(
        new iam.PolicyStatement({
          sid: "GroupPublishedRead",
          effect: iam.Effect.ALLOW,
          actions: ["s3:GetObject"],
          resources: groupIds.map(
            (g) => `${bucketArn}/groups/${g}/published/*`
          ),
        })
      );
    }

    // ListBucketScoped — read-only listing constrained to exactly the published
    // prefixes this role can GetObject on. No `users/<u>/drafts/*` and no
    // `users/<u>/*` (which would leak draft existence via listing).
    const listPrefixes: string[] = [
      `users/${userIdTag}/published/*`,
      `org/${orgIdTag}/published/*`,
      ...groupIds.map((g) => `groups/${g}/published/*`),
    ];

    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: "ListBucketScoped",
        effect: iam.Effect.ALLOW,
        actions: ["s3:ListBucket"],
        resources: [bucketArn],
        conditions: {
          StringLike: {
            "s3:prefix": listPrefixes,
          },
        },
      })
    );
  }
}

// ============================================================================
// Project_Runtime_Role construct
// ============================================================================
//
// Per design.md (IAM section / "Project_Runtime_Role") and Requirement 10.4 of
// the unified-mediaworks-platform spec, Project_Runtime_Role is the read-only
// prefix-scoped IAM role assumed by Project_Mode runs. It differs from
// Chat_Role in one critical way: there is NO `users/*` access at all — no
// private published, no drafts. The project runtime can only see assets that
// have been explicitly shared to one of the project's bound groups or to the
// org.
//
// Resulting grants: read-only `s3:GetObject` across
//   - `groups/<g>/published/*`           (statically expanded per bound group)
//   - `org/${aws:PrincipalTag/orgId}/published/*`
//
// plus a matching `s3:ListBucket` grant constrained by `s3:prefix` to exactly
// those prefixes. There is no `users/*` resource pattern anywhere in this
// policy — listing or fetching any object under `users/` returns AccessDenied.
//
// Per-project instantiation:
//   Group bindings are project-specific. This construct is meant to be
//   instantiated per project, statically emitting the project's bound group
//   ARNs at synthesis time. Re-binding a project to a different set of groups
//   requires a CDK redeploy.
//
// Group access:
//   IAM does not natively support `Resource` patterns parameterized over a
//   multi-value session tag, so this construct STATICALLY EXPANDS group access
//   into one `groups/<g>/published/*` Resource statement per bound group ID
//   known at synthesis time. The illustrative `s3:prefix-segment-2` condition
//   described in the design is intentionally NOT used here.
//
// Runtime tagging note:
//   Production runtime session tagging — i.e. injecting `orgId` (and optionally
//   `userId` / `groups`) at AssumeRole time from JWT claims — is deferred to
//   the AgentCore-proxy follow-on. This construct is forward-compatible with
//   that work: the policy statements already reference
//   `${aws:PrincipalTag/orgId}`, and the role's trust policy can be tightened
//   in the follow-on without altering these grants. Until the proxy work
//   lands, application-level checks in `list_visible_assets` (in `project`
//   mode) are the authoritative gate; IAM is defense-in-depth.

export interface ProjectRuntimeRoleProps {
  /** The Lab artifacts bucket whose ARN scopes all S3 statements. */
  readonly artifactsBucket: s3.IBucket;
  /**
   * The project's bound group IDs. Statically expanded into one
   * `groups/<g>/published/*` Resource statement per ID at synthesis time.
   * Defaults to an empty list (no group access — only org-published reads).
   */
  readonly groupIds?: string[];
  /**
   * Optional override for the role's trust principal. Defaults to the
   * deploying account's root, which is a permissive placeholder suitable
   * pre-AgentCore-proxy. The proxy follow-on will tighten this.
   */
  readonly assumedBy?: iam.IPrincipal;
}

export class ProjectRuntimeRole extends Construct {
  public readonly role: iam.Role;

  constructor(scope: Construct, id: string, props: ProjectRuntimeRoleProps) {
    super(scope, id);

    const groupIds = props.groupIds ?? [];
    const bucketArn = props.artifactsBucket.bucketArn;

    // PrincipalTag placeholder is written as a literal string in the policy
    // document; CloudFormation/IAM resolves it at request time. Note the
    // backslash escape to prevent TS template-literal interpolation.
    const orgIdTag = "${aws:PrincipalTag/orgId}";

    this.role = new iam.Role(this, "Role", {
      // Permissive placeholder trust — tightened by the AgentCore-proxy
      // follow-on. AccountRootPrincipal allows STS delegation within the
      // account; production traffic will not exercise this role until the
      // proxy injects session tags via AssumeRole.
      assumedBy: props.assumedBy ?? new iam.AccountRootPrincipal(),
      description:
        "Project_Mode read-only prefix-scoped role (no users/* access at all; " +
        "group bindings are project-specific and statically expanded at synth " +
        "time; forward-compatible with AgentCore-proxy session tagging)",
    });

    // OrgPublishedRead — read of the actor's org-scoped published prefix
    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: "OrgPublishedRead",
        effect: iam.Effect.ALLOW,
        actions: ["s3:GetObject"],
        resources: [`${bucketArn}/org/${orgIdTag}/published/*`],
      })
    );

    // GroupPublishedRead — one Resource statement per bound group ID.
    // Static expansion: IAM cannot parameterize Resource over a multi-value
    // session tag, so we emit explicit ARNs at synthesis time. Emitted only
    // when the project has at least one bound group.
    if (groupIds.length > 0) {
      this.role.addToPolicy(
        new iam.PolicyStatement({
          sid: "GroupPublishedRead",
          effect: iam.Effect.ALLOW,
          actions: ["s3:GetObject"],
          resources: groupIds.map(
            (g) => `${bucketArn}/groups/${g}/published/*`
          ),
        })
      );
    }

    // ListBucketScoped — read-only listing constrained to exactly the
    // published prefixes this role can GetObject on. Crucially, NO
    // `users/*` prefix appears here — listing under `users/` is denied,
    // which prevents the project runtime from enumerating any user's
    // private or published artifacts.
    const listPrefixes: string[] = [
      `org/${orgIdTag}/published/*`,
      ...groupIds.map((g) => `groups/${g}/published/*`),
    ];

    this.role.addToPolicy(
      new iam.PolicyStatement({
        sid: "ListBucketScoped",
        effect: iam.Effect.ALLOW,
        actions: ["s3:ListBucket"],
        resources: [bucketArn],
        conditions: {
          StringLike: {
            "s3:prefix": listPrefixes,
          },
        },
      })
    );
  }
}
