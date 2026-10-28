/**
 * IngestionStack — the nested stack for media-harness-project-ingestion (design §20 "IngestionStack").
 *
 * A CDK `NestedStack` created by `agent-infra-stack.ts` ONLY when `envConfig.projects.ingestion.enabled`
 * (sandbox, staging, source). It MUST be nested: the parent (staging) template is already ~945 KB of
 * CloudFormation's 1,000,000-byte limit, so every Lambda, role, state machine, queue, and schedule of
 * this spec lives here, and the parent grows only by the `AWS::CloudFormation::Stack` resource, a few
 * parameters, the ArtifactsCdn behaviour + CloudFront Function (which stay in the main stack), the
 * proxy/runtime policy statements, and two `RuntimeConfigSecret` keys (design §18, §20).
 *
 * P0 slice: the single `AssetServiceFn` — the only writer of Project_Asset rows outside the pipeline
 * (design §2, D3). The Dispatcher, IngestTick, CollectionRebuild, ProjectPurge, GeneratedMedia, and the
 * multipart/orphan janitors land in later waves and attach here; see the `TODO(ingestion)` markers.
 *
 * Lambda packaging mirrors the agent runtime (design §20 "Lambda packaging"): the GitLab deploy job has
 * no Docker and no pip, so the code package is built at deploy time by the existing zip-packager custom
 * resource in the PARENT (it bundles `agent/src/ingestion/`, `agent/src/projects/`, and the three
 * token-issuer `.py` files, plus arm64 wheels), and the (bucket, key) is passed in. All ingestion
 * Lambdas share that one package.
 */

import * as cdk from "aws-cdk-lib";
import * as events from "aws-cdk-lib/aws-events";
import * as targets from "aws-cdk-lib/aws-events-targets";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as sfn from "aws-cdk-lib/aws-stepfunctions";
import * as sqs from "aws-cdk-lib/aws-sqs";
import * as tasks from "aws-cdk-lib/aws-stepfunctions-tasks";
import { Construct } from "constructs";

export interface IngestionStackProps extends cdk.NestedStackProps {
  readonly projectName: string;
  /** Harness env name (`sandbox` | `staging` | `source`). */
  readonly envName: string;

  // ---- projects table (ProjectsStack outputs) ----
  readonly projectsTableName: string;
  readonly projectsTableArn: string;
  /** Stream ARN — consumed by the Dispatcher in a later wave; threaded now so props are stable. */
  readonly projectsTableStreamArn?: string;

  // ---- Ingest bucket (holds every Project_Prefix) ----
  readonly ingestBucketName: string;

  // ---- End-user Cognito pool (Asset_Service verifies the User_Token against it) ----
  readonly userPoolId: string;
  readonly userPoolClientId: string;

  // ---- RuntimeConfigSecret ARN (ingestion Lambdas read INGESTION_CONFIG from it in later waves) ----
  // Optional/empty for the P0 Asset_Service, which reads INGESTION_CONFIG from its own env instead, so
  // the nested stack does not reference the parent's secret (which references ASSET_SERVICE_FN) — that
  // would be a resource cycle. The grant + env are added only when a non-empty ARN is passed.
  readonly runtimeConfigSecretArn?: string;

  // ---- Deploy-time code package (zip-packager output in the parent's agent code bucket) ----
  readonly codeBucketName: string;
  readonly codeObjectKey: string;
  /**
   * The HeroFrame Lambda's own package (the ingestion sources + Pillow + PyAV). Separate because PyAV is ~95 MB
   * unzipped and only this one function decodes video. Absent → no HeroFrameFn, and the Dispatcher fans out nothing.
   */
  readonly heroFrameCodeObjectKey?: string;

  // ---- Caller ARNs for the Asset_Service resource policy (design §2: IAM limits who may invoke) ----
  readonly proxyTaskRoleArn: string;
  readonly mainAgentRuntimeRoleArn: string;
  /** Extra invoker role ARNs (the deployed live test), sandbox/staging only. */
  readonly assetServiceTestInvokerRoleArns?: string[];

  // ---- INGESTION_CONFIG JSON (also set directly on the Asset_Service env for P0) ----
  readonly ingestionConfigJson: string;
  /**
   * Personal samples (`infra/lib/personal-samples.ts`): the Asset_Service copies each into a user's Personal
   * project. Present → `PERSONAL_SAMPLES` env + read of exactly these `samples/` keys + write of exactly
   * `projects/*\/<fileName>`. Absent → neither.
   */
  readonly personalSamples?: ReadonlyArray<{ key: string; fileName: string; contentType: string; version: string }>;

  /**
   * The environment's M2C OAuth client and proxy (output) bucket (`envConfig.media`). When every field is
   * set, the pipeline is created: the Dispatcher (stream + 1-minute sweep), the `AssetIngestion` state
   * machine, and IngestTick (design §7, §8). Absent → the Asset_Service alone, as in P0.
   */
  readonly m2c?: {
    gatewayUrl: string;
    clientId: string;
    tokenEndpoint: string;
    credentialsSecretArn: string;
    scope?: string;
    proxyBucket: string;
  };

  /**
   * Generated_Media (Req 9, design §16) — present only when `ingestion.generatedMedia` is on. The job
   * registry's stream feeds async completions; the queue (fixed name `<projectName>-generated-media`, so
   * the parent can grant the proxy without a cross-stack token) carries the proxy's turn-end messages.
   */
  readonly generatedMedia?: {
    jobRegistryTableName: string;
    jobRegistryTableArn: string;
    jobRegistryStreamArn: string;
    /** The sessions table: GeneratedMediaFn reads a session's `title`/`createdAt` for its folder (design D20). */
    sessionsTableName: string;
    sessionsTableArn: string;
  };
}

/** The GeneratedMediaQueue name, shared with the parent's proxy env + grant. */
export function generatedMediaQueueName(projectName: string): string {
  return `${projectName}-generated-media`;
}

export class IngestionStack extends cdk.NestedStack {
  /** The Asset_Service Lambda — `RuntimeConfigSecret` key `ASSET_SERVICE_FN` + proxy env. */
  public readonly assetServiceFn: lambda.Function;
  /** Execution role for the Asset_Service (projects-table RW + Ingest-bucket read + secret read). */
  public readonly assetServiceRole: iam.Role;
  private readonly fixedAssetServiceName: string;

  constructor(scope: Construct, id: string, props: IngestionStackProps) {
    super(scope, id, props);

    const { projectName, envName } = props;
    const ingestArn = `arn:aws:s3:::${props.ingestBucketName}`;

    // ---------------------------------------------------------------------------------------------
    // Asset_Service execution role (design §2 "Privileged writes sit on roles no model drives", D3)
    // ---------------------------------------------------------------------------------------------
    this.assetServiceRole = new iam.Role(this, "AssetServiceRole", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      description:
        "Asset_Service: projects-table RW, Ingest-bucket read under projects/*, RuntimeConfigSecret " +
        "read. The ONLY writer of Project_Asset rows outside the pipeline; no model drives it.",
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName("service-role/AWSLambdaBasicExecutionRole"),
      ],
    });

    // Projects table read/write (base table + its GSIs). The Asset_Service makes the registration
    // transactions and the consistent reads of ASSET# rows.
    this.assetServiceRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "ProjectsTableReadWrite",
        actions: [
          "dynamodb:GetItem",
          "dynamodb:BatchGetItem",
          "dynamodb:Query",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:ConditionCheckItem",
          "dynamodb:TransactWriteItems",
          "dynamodb:TransactGetItems",
        ],
        resources: [props.projectsTableArn, `${props.projectsTableArn}/index/*`],
      }),
    );

    // Ingest bucket: HeadObject/GetObject on projects/* (register_upload verification; registrar reads),
    // and ListBucket scoped to the projects/ prefix (register_s3_location enumeration).
    this.assetServiceRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "IngestBucketReadProjects",
        actions: ["s3:GetObject"],
        resources: [`${ingestArn}/projects/*`],
      }),
    );
    // `search_images` renders a Search_Rendition on demand for an image ingestion never renders (a "this chat
    // only" upload, a stored asset), with the same writer as IngestTick. Write access is to rendition objects
    // ONLY, never to an asset's own bytes.
    this.assetServiceRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "IngestBucketWriteSearchRenditions",
        actions: ["s3:PutObject"],
        resources: [`${ingestArn}/projects/*/rendition.jpg`],
      }),
    );
    // `remove` (Projects Phase B) deletes bytes the Harness owns, and only those: an uploaded/generated
    // asset's object under projects/*, its chat copies under artifacts/<session>/input/*, and a `session`
    // asset's in-place source under artifacts/<session>/output/* (asset-lineage design §2). Nothing else in
    // the bucket (s3-location sources outside projects/, the shared artifacts/external/, skills) is
    // deletable, and M2C outputs (`m2c` assets) are never deleted (D4). The bucket is versioned, so a
    // delete writes a delete marker and the noncurrent-version lifecycle reclaims the bytes; no
    // s3:DeleteObjectVersion is granted.
    this.assetServiceRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "IngestBucketDeleteOwnedObjects",
        actions: ["s3:DeleteObject"],
        resources: [`${ingestArn}/projects/*`, `${ingestArn}/artifacts/*/input/*`, `${ingestArn}/artifacts/*/output/*`],
      }),
    );
    this.assetServiceRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "IngestBucketListProjects",
        actions: ["s3:ListBucket"],
        resources: [ingestArn],
        conditions: { StringLike: { "s3:prefix": ["projects/*"] } },
      }),
    );

    // RuntimeConfigSecret read (INGESTION_CONFIG; the AWS-managed secrets key needs no explicit grant).
    // Only when a non-empty ARN is passed (the later pipeline wave); P0 reads INGESTION_CONFIG from env.
    if (props.runtimeConfigSecretArn) {
      this.assetServiceRole.addToPolicy(
        new iam.PolicyStatement({
          sid: "RuntimeConfigSecretRead",
          actions: ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
          resources: [props.runtimeConfigSecretArn],
        }),
      );
    }

    // ---------------------------------------------------------------------------------------------
    // Asset_Service Lambda (design §2: Python 3.13, arm64, 1,024 MB, 90 s)
    // ---------------------------------------------------------------------------------------------
    const codeBucket = s3.Bucket.fromBucketName(this, "IngestionCodeBucket", props.codeBucketName);
    this.fixedAssetServiceName = assetServiceFunctionName(projectName);
    this.assetServiceFn = new lambda.Function(this, "AssetServiceFn", {
      functionName: this.fixedAssetServiceName,
      runtime: lambda.Runtime.PYTHON_3_13,
      architecture: lambda.Architecture.ARM_64,
      handler: "ingestion.handlers.asset_service.handler",
      code: lambda.Code.fromBucket(codeBucket, props.codeObjectKey),
      role: this.assetServiceRole,
      memorySize: 1024,
      timeout: cdk.Duration.seconds(90),
      description:
        "Asset_Service: the sole writer of Project_Asset rows outside the pipeline (resolve_assets, " +
        "register_upload, register_s3_location, remove, record_chat_copy). Verifies the User_Token + " +
        "membership per call.",
      environment: {
        // AWS_REGION is Lambda-reserved and injected automatically; do not set it here.
        PROJECTS_TABLE: props.projectsTableName,
        INGEST_BUCKET: props.ingestBucketName,
        USER_POOL_ID: props.userPoolId,
        USER_POOL_CLIENT_ID: props.userPoolClientId,
        DEPLOY_ENV: envName,
        // P0 reads INGESTION_CONFIG from its own env (Lambda env has no V2 1536-byte cap). It is ALSO
        // in RuntimeConfigSecret (design §20) for the later pipeline Lambdas.
        INGESTION_CONFIG: props.ingestionConfigJson,
        ...(props.personalSamples?.length ? { PERSONAL_SAMPLES: JSON.stringify(props.personalSamples) } : {}),
        ...(props.runtimeConfigSecretArn
          ? { RUNTIME_CONFIG_SECRET_ARN: props.runtimeConfigSecretArn }
          : {}),
      },
    });

    // Personal samples: the Asset_Service copies each shared `samples/` object into a user's Personal project
    // (CopyObject = GetObject on the source + PutObject on the destination). Both grants name exactly the
    // configured keys — never the whole bucket and never other project bytes.
    if (props.personalSamples?.length) {
      this.assetServiceRole.addToPolicy(
        new iam.PolicyStatement({
          sid: "IngestBucketReadPersonalSamples",
          actions: ["s3:GetObject"],
          resources: props.personalSamples.map((s) => `${ingestArn}/${s.key}`),
        }),
      );
      this.assetServiceRole.addToPolicy(
        new iam.PolicyStatement({
          sid: "IngestBucketWritePersonalSampleCopies",
          actions: ["s3:PutObject"],
          resources: props.personalSamples.map((s) => `${ingestArn}/projects/*/${s.fileName}`),
        }),
      );
    }

    // Resource policy: IAM limits who may invoke the writer (design §2). The identity grants on these
    // caller roles are added by the parent.
    this.assetServiceFn.addPermission("ProxyInvoke", {
      principal: new iam.ArnPrincipal(props.proxyTaskRoleArn),
      action: "lambda:InvokeFunction",
    });
    this.assetServiceFn.addPermission("MainAgentInvoke", {
      principal: new iam.ArnPrincipal(props.mainAgentRuntimeRoleArn),
      action: "lambda:InvokeFunction",
    });
    (props.assetServiceTestInvokerRoleArns ?? []).forEach((arn, i) => {
      this.assetServiceFn.addPermission(`TestInvoker${i}`, {
        principal: new iam.ArnPrincipal(arn),
        action: "lambda:InvokeFunction",
      });
    });

    if (props.m2c && props.projectsTableStreamArn) {
      // The Asset_Service checks the M2C catalog before registering, retrying, or re-adding (Req 8.15, 11.2).
      const m2cCfg = props.m2c;
      for (const [k, v] of Object.entries({
        M2C_GATEWAY_URL: m2cCfg.gatewayUrl, M2C_CLIENT_ID: m2cCfg.clientId, M2C_TOKEN_ENDPOINT: m2cCfg.tokenEndpoint,
        M2C_SECRET_ARN: m2cCfg.credentialsSecretArn, M2C_SCOPE: m2cCfg.scope ?? "media-workflows/invoke",
      })) {
        this.assetServiceFn.addEnvironment(k, v);
      }
      this.assetServiceRole.addToPolicy(new iam.PolicyStatement({
        sid: "M2cCredentialsRead", actions: ["secretsmanager:GetSecretValue"], resources: [m2cCfg.credentialsSecretArn],
      }));
      // `readd` of an `m2c` asset HEADs its source, which stays in the M2C proxy bucket (asset-lineage D4).
      this.assetServiceRole.addToPolicy(new iam.PolicyStatement({
        sid: "M2cOutputsHead", actions: ["s3:GetObject"], resources: [`arn:aws:s3:::${m2cCfg.proxyBucket}/*`],
      }));
      this.addPipeline(props, codeBucket, props.m2c, props.projectsTableStreamArn);
    }
    // TODO(ingestion): the multipart/orphan janitors attach to this stack in a later wave.
  }

  /** The Dispatcher, the `AssetIngestion` state machine, and IngestTick (design §7, §8). */
  public dispatcherFn?: lambda.Function;
  public heroFrameFn?: lambda.Function;
  public tickFn?: lambda.Function;
  public assetIngestion?: sfn.StateMachine;
  public rebuildTickFn?: lambda.Function;
  public collectionRebuild?: sfn.StateMachine;
  public purgeTickFn?: lambda.Function;
  public projectPurge?: sfn.StateMachine;

  private addPipeline(
    props: IngestionStackProps,
    codeBucket: s3.IBucket,
    m2c: NonNullable<IngestionStackProps["m2c"]>,
    streamArn: string,
  ): void {
    const ingestArn = `arn:aws:s3:::${props.ingestBucketName}`;
    // Built from a fixed name, not the construct's ARN: IngestTick starts runs in-process when it frees a
    // slot, and the state machine invokes IngestTick, so a token reference would be a cycle.
    const smName = `${props.projectName}-asset-ingestion`;
    const smArn = `arn:aws:states:${this.region}:${this.account}:stateMachine:${smName}`;
    const rebuildSmName = `${props.projectName}-collection-rebuild`;
    const rebuildSmArn = `arn:aws:states:${this.region}:${this.account}:stateMachine:${rebuildSmName}`;
    const purgeSmName = `${props.projectName}-project-purge`;
    const purgeSmArn = `arn:aws:states:${this.region}:${this.account}:stateMachine:${purgeSmName}`;
    const tableRw = new iam.PolicyStatement({
      sid: "ProjectsTableReadWrite",
      actions: ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:UpdateItem", "dynamodb:PutItem",
        "dynamodb:DeleteItem", "dynamodb:ConditionCheckItem", "dynamodb:TransactWriteItems"],
      resources: [props.projectsTableArn, `${props.projectsTableArn}/index/*`],
    });
    const startRuns = new iam.PolicyStatement({
      sid: "StartAssetIngestion", actions: ["states:StartExecution"], resources: [smArn],
    });
    const env = {
      PROJECTS_TABLE: props.projectsTableName,
      INGEST_BUCKET: props.ingestBucketName,
      ASSET_INGESTION_SFN_ARN: smArn,
      COLLECTION_REBUILD_SFN_ARN: rebuildSmArn,
      PROJECT_PURGE_SFN_ARN: purgeSmArn,
      DEPLOY_ENV: props.envName,
      INGESTION_CONFIG: props.ingestionConfigJson,
      M2C_GATEWAY_URL: m2c.gatewayUrl,
      M2C_CLIENT_ID: m2c.clientId,
      M2C_TOKEN_ENDPOINT: m2c.tokenEndpoint,
      M2C_SECRET_ARN: m2c.credentialsSecretArn,
      M2C_SCOPE: m2c.scope ?? "media-workflows/invoke",
    };
    const basic = iam.ManagedPolicy.fromAwsManagedPolicyName("service-role/AWSLambdaBasicExecutionRole");

    // ---- HeroFrame: a sampled thumbnail for every new video (ingestion/hero_frame.py). ----
    // The indexing poster only exists after the whole M2C chain, so a `stored` video (indexing off, session
    // uploads, M2C intermediates) never had a hero image. The Dispatcher async-invokes this per new
    // ASSET#video: row; it reads the source with ranged GETs and writes the same rendition.jpg the poster uses.
    let heroFrameFn: lambda.Function | undefined;
    if (props.heroFrameCodeObjectKey) {
      const heroRole = new iam.Role(this, "HeroFrameRole", {
        assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"), managedPolicies: [basic],
        description: "HeroFrame: read a video source, write its rendition.jpg, set ASSET.rendition if absent.",
      });
      heroRole.addToPolicy(new iam.PolicyStatement({
        sid: "AssetRowReadUpdate", actions: ["dynamodb:GetItem", "dynamodb:UpdateItem"],
        resources: [props.projectsTableArn],
      }));
      heroRole.addToPolicy(new iam.PolicyStatement({
        sid: "VideoSourcesRead", actions: ["s3:GetObject"],
        resources: [`${ingestArn}/projects/*`, `${ingestArn}/artifacts/*`, `arn:aws:s3:::${m2c.proxyBucket}/*`],
      }));
      heroRole.addToPolicy(new iam.PolicyStatement({
        sid: "RenditionWrite", actions: ["s3:PutObject"], resources: [`${ingestArn}/projects/*/rendition.jpg`],
      }));
      heroFrameFn = new lambda.Function(this, "HeroFrameFn", {
        runtime: lambda.Runtime.PYTHON_3_13, architecture: lambda.Architecture.ARM_64,
        handler: "ingestion.handlers.hero_frame.handler",
        code: lambda.Code.fromBucket(codeBucket, props.heroFrameCodeObjectKey), role: heroRole,
        memorySize: 1536, timeout: cdk.Duration.seconds(120), retryAttempts: 1,
        description: "HeroFrame: sample one frame of a new video as its rendition.jpg thumbnail.",
        environment: { PROJECTS_TABLE: props.projectsTableName, INGEST_BUCKET: props.ingestBucketName },
      });
      this.heroFrameFn = heroFrameFn;
    }

    // ---- Dispatcher: claims queued assets and starts runs. No S3, no M2C. ----
    const dispatcherRole = new iam.Role(this, "DispatcherRole", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"), managedPolicies: [basic],
      description: "Ingestion Dispatcher: projects-table claims + stream read, StartExecution of AssetIngestion.",
    });
    dispatcherRole.addToPolicy(tableRw);
    dispatcherRole.addToPolicy(startRuns);
    dispatcherRole.addToPolicy(new iam.PolicyStatement({
      sid: "ProjectsStreamRead",
      actions: ["dynamodb:DescribeStream", "dynamodb:GetRecords", "dynamodb:GetShardIterator", "dynamodb:ListStreams"],
      resources: [streamArn],
    }));
    this.dispatcherFn = new lambda.Function(this, "DispatcherFn", {
      runtime: lambda.Runtime.PYTHON_3_13, architecture: lambda.Architecture.ARM_64,
      handler: "ingestion.handlers.dispatcher.handler",
      code: lambda.Code.fromBucket(codeBucket, props.codeObjectKey), role: dispatcherRole,
      memorySize: 512, timeout: cdk.Duration.seconds(120),
      description: "Ingestion Dispatcher: start queued Project_Assets, at most 5 processing per Project (design §7).",
      environment: heroFrameFn ? { ...env, HERO_FRAME_FN: heroFrameFn.functionName } : env,
    });
    if (heroFrameFn) heroFrameFn.grantInvoke(dispatcherRole);
    // An asset entered `queued`: the registration (and later Retry/Re-add) transaction inserted QUEUE#. And, when
    // HeroFrame exists, a video ASSET row was inserted — riding this mapping keeps the projects stream at two
    // readers (DynamoDB Streams throttles above ~2 per shard) instead of adding HeroFrame as a third.
    const dispatcherFilters = [lambda.FilterCriteria.filter({
      eventName: lambda.FilterRule.isEqual("INSERT"),
      dynamodb: { NewImage: { SK: { S: lambda.FilterRule.beginsWith("QUEUE#") } } },
    })];
    if (heroFrameFn) {
      dispatcherFilters.push(lambda.FilterCriteria.filter({
        eventName: lambda.FilterRule.isEqual("INSERT"),
        dynamodb: { NewImage: { SK: { S: lambda.FilterRule.beginsWith("ASSET#video:") } } },
      }));
    }
    new lambda.EventSourceMapping(this, "DispatcherQueueStream", {
      target: this.dispatcherFn, eventSourceArn: streamArn,
      startingPosition: lambda.StartingPosition.LATEST, batchSize: 50,
      maxBatchingWindow: cdk.Duration.seconds(1), retryAttempts: 5, bisectBatchOnError: true,
      filters: dispatcherFilters,
    });
    new events.Rule(this, "DispatcherSweep", {
      schedule: events.Schedule.rate(cdk.Duration.minutes(1)),
      description: "Ingestion Dispatcher sweep: Projects with queued or processing assets (SCHED), stuck starts.",
      targets: [new targets.LambdaFunction(this.dispatcherFn, { retryAttempts: 0 })],
    });

    // ---- ImportSweep: the import inbox (projects/<pid>/import/), swept once a minute. ----
    // A sweep, not an S3 event: the ingest bucket is shared with M2C and imported (not created) in the deployed
    // envs, and turning its notifications on would REPLACE the bucket's whole notification configuration. Listing
    // the prefix needs no change to the bucket. It registers objects IN PLACE (no copy), so it needs list only.
    const importRole = new iam.Role(this, "ImportSweepRole", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"), managedPolicies: [basic],
      description: "ImportSweep: list the projects/ prefix of the Ingest bucket, register inbox objects in the projects table.",
    });
    importRole.addToPolicy(new iam.PolicyStatement({
      sid: "ProjectsTableImport",
      actions: ["dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:UpdateItem",
        "dynamodb:ConditionCheckItem", "dynamodb:TransactWriteItems"],
      resources: [props.projectsTableArn],
    }));
    importRole.addToPolicy(new iam.PolicyStatement({
      sid: "IngestBucketListProjects", actions: ["s3:ListBucket"], resources: [ingestArn],
      conditions: { StringLike: { "s3:prefix": ["projects/", "projects/*"] } },
    }));
    const importSweepFn = new lambda.Function(this, "ImportSweepFn", {
      runtime: lambda.Runtime.PYTHON_3_13, architecture: lambda.Architecture.ARM_64,
      handler: "ingestion.handlers.import_sweep.handler",
      code: lambda.Code.fromBucket(codeBucket, props.codeObjectKey), role: importRole,
      memorySize: 512, timeout: cdk.Duration.minutes(5),
      description: "Import inbox: registers files copied to projects/<pid>/import/ in place, flags what cannot be ingested.",
      environment: { PROJECTS_TABLE: props.projectsTableName, INGEST_BUCKET: props.ingestBucketName,
        INGESTION_CONFIG: props.ingestionConfigJson, DEPLOY_ENV: props.envName },
      reservedConcurrentExecutions: 1, // one sweep at a time: two would race the same watermark
    });
    new events.Rule(this, "ImportInboxSweep", {
      schedule: events.Schedule.rate(cdk.Duration.minutes(1)),
      description: "Import inbox sweep: projects/<pid>/import/ objects become project assets.",
      targets: [new targets.LambdaFunction(importSweepFn, { retryAttempts: 0 })],
    });

    // ---- IngestTick: one step of a run. Reads M2C outputs, writes Document_Records, calls M2C. ----
    const tickRole = new iam.Role(this, "IngestTickRole", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"), managedPolicies: [basic],
      description: "IngestTick: projects-table RW, M2C output read, Document_Record write, M2C credentials read.",
    });
    tickRole.addToPolicy(tableRw);
    tickRole.addToPolicy(startRuns);
    // A stale scope after a re-index starts a Collection_Rebuild (Req 6.9).
    const startRebuilds = new iam.PolicyStatement({
      sid: "StartCollectionRebuild", actions: ["states:StartExecution"], resources: [rebuildSmArn],
    });
    tickRole.addToPolicy(startRebuilds);
    tickRole.addToPolicy(new iam.PolicyStatement({
      sid: "M2cOutputsRead", actions: ["s3:GetObject"], resources: [`arn:aws:s3:::${m2c.proxyBucket}/*`],
    }));
    tickRole.addToPolicy(new iam.PolicyStatement({
      sid: "DocumentRecordsReadWrite", actions: ["s3:GetObject", "s3:PutObject"],
      resources: [`${ingestArn}/projects/*`],
    }));
    // A `session` asset is registered IN PLACE (asset-lineage WP1), so its sourceUri can be any key the
    // GeneratedMediaFn registers under artifacts/: fal outputs at artifacts/external/<hash>, legacy-layout
    // outputs at artifacts/<sid>/output/, a folder the model named. The image rendition reads those bytes;
    // without this grant every such image failed "the image cannot be read: AccessDenied" (staging
    // 2026-10-09, project p925d3f6, 20 fal images). Read-only: Document_Records still go to projects/* alone.
    tickRole.addToPolicy(new iam.PolicyStatement({
      sid: "SessionSourcesRead", actions: ["s3:GetObject"], resources: [`${ingestArn}/artifacts/*`],
    }));
    tickRole.addToPolicy(new iam.PolicyStatement({
      sid: "M2cCredentialsRead", actions: ["secretsmanager:GetSecretValue"], resources: [m2c.credentialsSecretArn],
    }));
    this.tickFn = new lambda.Function(this, "IngestTickFn", {
      runtime: lambda.Runtime.PYTHON_3_13, architecture: lambda.Architecture.ARM_64,
      handler: "ingestion.handlers.tick.handler",
      code: lambda.Code.fromBucket(codeBucket, props.codeObjectKey), role: tickRole,
      memorySize: 2048, timeout: cdk.Duration.seconds(300),
      description: "IngestTick: one step of an AssetIngestion run — poll, submit, build, index (design §8).",
      environment: env,
    });

    // ---- AssetIngestion: Tick → Choice(done?) → Wait(wait_s) → Tick (design §8). ----
    const tick = new tasks.LambdaInvoke(this, "Tick", {
      lambdaFunction: this.tickFn, payloadResponseOnly: true, retryOnServiceExceptions: true,
    });
    tick.addRetry({ errors: ["States.ALL"], interval: cdk.Duration.seconds(30), backoffRate: 2, maxAttempts: 3 });
    const abort = new tasks.LambdaInvoke(this, "AbortRun", {
      lambdaFunction: this.tickFn, payloadResponseOnly: true,
      payload: sfn.TaskInput.fromObject({
        "pid.$": "$$.Execution.Input.pid", "assetId.$": "$$.Execution.Input.assetId",
        "runSeq.$": "$$.Execution.Input.runSeq", abort: true, "error.$": "$.Cause",
      }),
    });
    const finished = new sfn.Succeed(this, "Finished");
    tick.addCatch(abort.next(finished), { errors: ["States.ALL"] });
    // The 30 s floor is what keeps two 6-hour waves + 3 index chunks inside 25,000 history events.
    const wait = new sfn.Wait(this, "WaitBeforeNextTick", { time: sfn.WaitTime.secondsPath("$.wait_s") });
    wait.next(tick);
    const definition = tick.next(
      new sfn.Choice(this, "RunDone").when(sfn.Condition.booleanEquals("$.done", true), finished).otherwise(wait),
    );
    this.assetIngestion = new sfn.StateMachine(this, "AssetIngestion", {
      stateMachineName: smName,
      definitionBody: sfn.DefinitionBody.fromChainable(definition),
      stateMachineType: sfn.StateMachineType.STANDARD,
      timeout: cdk.Duration.hours(24),
      logs: {
        destination: new logs.LogGroup(this, "AssetIngestionLogs", {
          retention: logs.RetentionDays.ONE_MONTH, removalPolicy: cdk.RemovalPolicy.DESTROY,
        }),
        level: sfn.LogLevel.ERROR,
      },
    });

    const codeOf = lambda.Code.fromBucket(codeBucket, props.codeObjectKey);
    const secretRead = new iam.PolicyStatement({
      sid: "M2cCredentialsRead", actions: ["secretsmanager:GetSecretValue"], resources: [m2c.credentialsSecretArn],
    });

    // ---- Collection_Rebuild (design §14): RebuildTick loop over META.rebuild phases. ----
    this.assetServiceFn.addEnvironment("COLLECTION_REBUILD_SFN_ARN", rebuildSmArn);
    this.assetServiceRole.addToPolicy(startRebuilds); // a removal of an accepted asset starts one (Req 10.5)
    const rebuildRole = new iam.Role(this, "RebuildTickRole", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"), managedPolicies: [basic],
      description: "RebuildTick: projects-table RW, Document_Record read, M2C credentials, start the follow-up rebuild.",
    });
    rebuildRole.addToPolicy(tableRw);
    rebuildRole.addToPolicy(startRebuilds);
    rebuildRole.addToPolicy(secretRead);
    rebuildRole.addToPolicy(new iam.PolicyStatement({
      sid: "DocumentRecordsRead", actions: ["s3:GetObject"], resources: [`${ingestArn}/projects/*`],
    }));
    this.rebuildTickFn = new lambda.Function(this, "RebuildTickFn", {
      runtime: lambda.Runtime.PYTHON_3_13, architecture: lambda.Architecture.ARM_64,
      handler: "ingestion.handlers.rebuild_tick.handler", code: codeOf, role: rebuildRole,
      memorySize: 1024, timeout: cdk.Duration.seconds(900), // delete_collection is a 600 s call
      description: "RebuildTick: one step of a Collection_Rebuild (wait, delete, create, resubmit) (design §14).",
      environment: env,
    });
    this.collectionRebuild = this.tickLoop("CollectionRebuild", this.rebuildTickFn, rebuildSmName,
      cdk.Duration.hours(12), ["pid", "seq"]);

    // ---- Project-deletion purge (design §15): the ONLY role with object-version deletes. ----
    const purgeRole = new iam.Role(this, "PurgeRole", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"), managedPolicies: [basic],
      description: "ProjectPurge: deletes a deleted Project's collection, every object version under projects/<pid>/, and its records.",
    });
    purgeRole.addToPolicy(new iam.PolicyStatement({
      sid: "ProjectsTablePurge",
      actions: ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:UpdateItem", "dynamodb:PutItem",
        "dynamodb:DeleteItem", "dynamodb:BatchWriteItem"],
      resources: [props.projectsTableArn],
    }));
    purgeRole.addToPolicy(secretRead);
    purgeRole.addToPolicy(new iam.PolicyStatement({
      sid: "IngestListProjectVersions", actions: ["s3:ListBucketVersions"], resources: [ingestArn],
      conditions: { StringLike: { "s3:prefix": ["projects/*"] } },
    }));
    purgeRole.addToPolicy(new iam.PolicyStatement({
      sid: "IngestListUploads", actions: ["s3:ListBucketMultipartUploads"], resources: [ingestArn],
    }));
    purgeRole.addToPolicy(new iam.PolicyStatement({
      sid: "IngestDeleteProjectVersions",
      actions: ["s3:DeleteObject", "s3:DeleteObjectVersion", "s3:AbortMultipartUpload"],
      resources: [`${ingestArn}/projects/*`],
    }));
    this.purgeTickFn = new lambda.Function(this, "PurgeTickFn", {
      runtime: lambda.Runtime.PYTHON_3_13, architecture: lambda.Architecture.ARM_64,
      handler: "ingestion.handlers.purge.handler", code: codeOf, role: purgeRole,
      memorySize: 1024, timeout: cdk.Duration.seconds(900),
      description: "PurgeTick: one step of a deleted Project's purge (design §15).",
      environment: env,
    });
    this.projectPurge = this.tickLoop("ProjectPurge", this.purgeTickFn, purgeSmName, cdk.Duration.hours(24), ["pid"]);

    const triggerRole = new iam.Role(this, "PurgeTriggerRole", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"), managedPolicies: [basic],
      description: "PurgeTrigger: start ProjectPurge on a deleted Project (stream + daily sweep).",
    });
    triggerRole.addToPolicy(new iam.PolicyStatement({
      sid: "ProjectsTableScan", actions: ["dynamodb:Scan"], resources: [props.projectsTableArn],
    }));
    triggerRole.addToPolicy(new iam.PolicyStatement({
      sid: "StartProjectPurge", actions: ["states:StartExecution"], resources: [purgeSmArn],
    }));
    triggerRole.addToPolicy(new iam.PolicyStatement({
      sid: "ProjectsStreamRead",
      actions: ["dynamodb:DescribeStream", "dynamodb:GetRecords", "dynamodb:GetShardIterator", "dynamodb:ListStreams"],
      resources: [streamArn],
    }));
    const triggerFn = new lambda.Function(this, "PurgeTriggerFn", {
      runtime: lambda.Runtime.PYTHON_3_13, architecture: lambda.Architecture.ARM_64,
      handler: "ingestion.handlers.purge.trigger_handler", code: codeOf, role: triggerRole,
      memorySize: 256, timeout: cdk.Duration.seconds(120),
      description: "PurgeTrigger: META active -> deleted starts purge-<pid>; a daily sweep catches missed records.",
      environment: env,
    });
    new lambda.EventSourceMapping(this, "PurgeTriggerStream", {
      target: triggerFn, eventSourceArn: streamArn, startingPosition: lambda.StartingPosition.LATEST,
      batchSize: 20, retryAttempts: 5, bisectBatchOnError: true,
      filters: [lambda.FilterCriteria.filter({
        eventName: lambda.FilterRule.isEqual("MODIFY"),
        dynamodb: {
          Keys: { SK: { S: lambda.FilterRule.isEqual("META") } },
          NewImage: { status: { S: lambda.FilterRule.isEqual("deleted") } },
          OldImage: { status: { S: lambda.FilterRule.isEqual("active") } },
        },
      })],
    });
    new events.Rule(this, "PurgeSweep", {
      schedule: events.Schedule.rate(cdk.Duration.days(1)),
      description: "ProjectPurge sweep: deleted Projects whose stream record was missed (records expire in 24 h).",
      targets: [new targets.LambdaFunction(triggerFn, { retryAttempts: 2 })],
    });

    if (props.generatedMedia) this.addGeneratedMedia(props, codeOf, env, props.generatedMedia, m2c.proxyBucket);
  }

  public generatedMediaFn?: lambda.Function;
  public generatedMediaQueue?: sqs.Queue;

  /**
   * GeneratedMediaFn (design §16; asset-lineage design §3, the session output registrar): SQS turn messages
   * from the proxy + job-registry terminal rows. It registers session/external media and the M2C outputs a
   * job reports IN PLACE — no byte is copied, so it writes no S3 object at all. It reads (HEAD with
   * checksums) `artifacts/*`, the `jobs/`/`plans/` result documents, and the M2C proxy bucket.
   */
  private addGeneratedMedia(props: IngestionStackProps, code: lambda.Code, env: Record<string, string>,
                            gm: NonNullable<IngestionStackProps["generatedMedia"]>, m2cProxyBucket: string): void {
    const ingestArn = `arn:aws:s3:::${props.ingestBucketName}`;
    const dlq = new sqs.Queue(this, "GeneratedMediaDlq", {
      queueName: `${generatedMediaQueueName(props.projectName)}-dlq`,
      retentionPeriod: cdk.Duration.days(14), encryption: sqs.QueueEncryption.SQS_MANAGED, enforceSSL: true,
    });
    // Visibility 120 s = the function timeout; a failed copy is retried on redelivery (Req 9.8).
    this.generatedMediaQueue = new sqs.Queue(this, "GeneratedMediaQueue", {
      queueName: generatedMediaQueueName(props.projectName),
      visibilityTimeout: cdk.Duration.seconds(120), encryption: sqs.QueueEncryption.SQS_MANAGED, enforceSSL: true,
      deadLetterQueue: { queue: dlq, maxReceiveCount: 4 },
    });
    const role = new iam.Role(this, "GeneratedMediaRole", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName("service-role/AWSLambdaBasicExecutionRole")],
      description: "GeneratedMedia: read session/external/M2C outputs + result docs, register them in place.",
    });
    role.addToPolicy(new iam.PolicyStatement({
      sid: "ProjectsTableRegister",
      actions: ["dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:Query", "dynamodb:PutItem",
        "dynamodb:UpdateItem", "dynamodb:ConditionCheckItem", "dynamodb:TransactWriteItems"],
      resources: [props.projectsTableArn],
    }));
    role.addToPolicy(new iam.PolicyStatement({
      sid: "JobRegistryPlanRowRead", actions: ["dynamodb:GetItem"], resources: [gm.jobRegistryTableArn],
    }));
    // The folder `Sessions/<session title> · <date>` (asset-lineage R3.3): one GetItem of the session row, plus
    // one per invented `artifacts/<uuid>/output/` folder to refuse another session's (R3.4). Read-only, base
    // table only — a failed title read just falls back to `Sessions/Session <id> · <date>`.
    role.addToPolicy(new iam.PolicyStatement({
      sid: "SessionTitleRead", actions: ["dynamodb:GetItem"], resources: [gm.sessionsTableArn],
    }));
    // GetObject covers HeadObject (incl. ChecksumMode=ENABLED, the HASH dedupe guard's checksum, R5).
    role.addToPolicy(new iam.PolicyStatement({
      sid: "SourcesAndResultsRead", actions: ["s3:GetObject", "s3:GetObjectVersion", "s3:GetObjectAttributes"],
      resources: [`${ingestArn}/artifacts/*`, `${ingestArn}/jobs/*`, `${ingestArn}/plans/*`,
        // asset-lineage Phase 3 (a): session outputs under the project layout, registered in place.
        `${ingestArn}/projects/*/sessions/*`],
    }));
    // M2C outputs are registered by reference where M2C wrote them (asset-lineage R4.1, D4): read-only.
    role.addToPolicy(new iam.PolicyStatement({
      sid: "M2cOutputsRead", actions: ["s3:GetObject", "s3:GetObjectAttributes"],
      resources: [`arn:aws:s3:::${m2cProxyBucket}/*`],
    }));
    role.addToPolicy(new iam.PolicyStatement({
      sid: "SessionList", actions: ["s3:ListBucket"], resources: [ingestArn],
      conditions: { StringLike: { "s3:prefix": ["artifacts/*", "projects/*/sessions/*"] } },
    }));
    this.generatedMediaFn = new lambda.Function(this, "GeneratedMediaFn", {
      runtime: lambda.Runtime.PYTHON_3_13, architecture: lambda.Architecture.ARM_64,
      handler: "ingestion.handlers.generated_media.handler", code, role,
      memorySize: 512, timeout: cdk.Duration.seconds(120),
      description: "GeneratedMedia: register the media a Projects turn produced into its Project (design §16).",
      environment: {
        ...env, JOB_REGISTRY_TABLE: gm.jobRegistryTableName, SESSIONS_TABLE: gm.sessionsTableName,
        MEDIA_PROXY_BUCKET: m2cProxyBucket,
      },
    });
    this.generatedMediaQueue.grantConsumeMessages(role);
    new lambda.EventSourceMapping(this, "GeneratedMediaTurns", {
      target: this.generatedMediaFn, eventSourceArn: this.generatedMediaQueue.queueArn,
      batchSize: 5, reportBatchItemFailures: true,
    });
    role.addToPolicy(new iam.PolicyStatement({
      sid: "JobRegistryStreamRead",
      actions: ["dynamodb:DescribeStream", "dynamodb:GetRecords", "dynamodb:GetShardIterator", "dynamodb:ListStreams"],
      resources: [gm.jobRegistryStreamArn],
    }));
    // A job or plan row reaching a terminal status with a result document (design §16 "async").
    new lambda.EventSourceMapping(this, "GeneratedMediaJobs", {
      target: this.generatedMediaFn, eventSourceArn: gm.jobRegistryStreamArn,
      startingPosition: lambda.StartingPosition.LATEST, batchSize: 10, retryAttempts: 3,
      bisectBatchOnError: true, reportBatchItemFailures: true, maxRecordAge: cdk.Duration.hours(6),
      filters: [lambda.FilterCriteria.filter({
        eventName: lambda.FilterRule.isEqual("MODIFY"),
        dynamodb: {
          NewImage: {
            status: { S: lambda.FilterRule.or("SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED") },
            resultKey: { S: lambda.FilterRule.exists() },
          },
        },
      })],
    });
  }

  /**
   * `Tick → Choice(done?) → Wait(wait_s) → Tick`, with a Catch that invokes the tick once more with
   * `{abort: true, error}` so a crashed loop records a failure instead of leaving its state stuck.
   */
  private tickLoop(id: string, fn: lambda.IFunction, name: string, timeout: cdk.Duration,
                   inputFields: string[]): sfn.StateMachine {
    const step = new tasks.LambdaInvoke(this, `${id}Tick`, {
      lambdaFunction: fn, payloadResponseOnly: true, retryOnServiceExceptions: true,
    });
    step.addRetry({ errors: ["States.ALL"], interval: cdk.Duration.seconds(30), backoffRate: 2, maxAttempts: 3 });
    const payload: Record<string, unknown> = { abort: true, "error.$": "$.Cause" };
    for (const f of inputFields) payload[`${f}.$`] = `$$.Execution.Input.${f}`;
    const finished = new sfn.Succeed(this, `${id}Finished`);
    step.addCatch(new tasks.LambdaInvoke(this, `${id}Abort`, {
      lambdaFunction: fn, payloadResponseOnly: true, payload: sfn.TaskInput.fromObject(payload),
    }).next(finished), { errors: ["States.ALL"] });
    const wait = new sfn.Wait(this, `${id}Wait`, { time: sfn.WaitTime.secondsPath("$.wait_s") });
    wait.next(step);
    return new sfn.StateMachine(this, id, {
      stateMachineName: name,
      definitionBody: sfn.DefinitionBody.fromChainable(step.next(
        new sfn.Choice(this, `${id}Done`).when(sfn.Condition.booleanEquals("$.done", true), finished).otherwise(wait))),
      stateMachineType: sfn.StateMachineType.STANDARD,
      timeout,
      logs: {
        destination: new logs.LogGroup(this, `${id}Logs`, {
          retention: logs.RetentionDays.ONE_MONTH, removalPolicy: cdk.RemovalPolicy.DESTROY,
        }),
        level: sfn.LogLevel.ERROR,
      },
    });
  }

  /**
   * Asset_Service function name — `RuntimeConfigSecret` key `ASSET_SERVICE_FN` + proxy env. A plain string
   * (the function has this fixed name), so the parent's secret and task definition take no token from
   * this nested stack and the main/task runtimes stop waiting for it to update (docs/ci-speed-proposal.md P2).
   */
  public get assetServiceFnName(): string {
    return this.fixedAssetServiceName;
  }
}

/** The Asset_Service Lambda's fixed name, shared with the parent's secret, proxy env, and invoke grants. */
export function assetServiceFunctionName(projectName: string): string {
  return `${projectName}-asset-service`;
}
