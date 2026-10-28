/**
 * Deploy-time preload custom resource (design docs/design-2026-10-27-admin-preload-and-preanalyzed-ingest.md,
 * Task outline 1 + 9).
 *
 * `PreloadFn` runs on stack Create/Update AFTER the user pool, projects table, Ingest bucket, skills store and
 * ingestion pipeline exist. It finds a package at `preloadPath` (a folder, or a `.zip` it unpacks), no-ops when
 * absent, fails the deploy when a package is present but `ownerEmail` is empty, and otherwise creates the
 * owner, seeds the project(s), places the assets and validates the paired skill(s). The Python lives in
 * `agent/src/preload/` (handler `preload.handler.handler`). Its package is built by the stack's zip packager
 * from the agent sources plus `ingestion` and PyYAML (see `PreloadResourceProps.packaging`), so the handler
 * can import `preload`, `ingestion`, `projects` and `lab`, and the skill validator can parse `SKILL.md`.
 *
 * This is a self-contained helper: the stack calls {@link addPreloadResource} with the already-constructed
 * Ingest bucket, projects table, Lab asset-index table, Lab artifacts bucket and user pool once `cfg.preload`
 * is set. Keeping it here (rather than inline in the 4,700-line stack) keeps the IAM surface — Cognito admin
 * verbs, the projects + asset-index tables, the Ingest bucket asset/import prefixes and the artifacts bucket
 * skills prefix — reviewable in one place (design Part 2 "IAM").
 */
import * as crypto from "crypto";
import * as fs from "fs";
import * as path from "path";

import * as cdk from "aws-cdk-lib";
import * as cognito from "aws-cdk-lib/aws-cognito";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as s3deploy from "aws-cdk-lib/aws-s3-deployment";
import * as cr from "aws-cdk-lib/custom-resources";
import { Construct } from "constructs";

export interface PreloadResourceProps {
  /** `${projectName}` — the resource-name prefix (e.g. `mediaworks-harness`). */
  readonly projectName: string;
  /** The deploy env name (`staging` | `prod` | …), surfaced to the handler as `DEPLOY_ENV`. */
  readonly envName: string;
  /** The shared Ingest/Skills bucket the package lives in and the pipeline reads. */
  readonly ingestBucket: s3.IBucket;
  /** The projects single table (`PROJECT#…` / `USER#…` rows). */
  readonly projectsTable: dynamodb.ITable;
  /** The Lab asset-index single table the published skill row is written to (projector source). */
  readonly assetIndexTable: dynamodb.ITable;
  /** The Lab artifacts bucket the published skill package bytes live in (`users/<sub>/published/…`). */
  readonly artifactsBucket: s3.IBucket;
  /** The end-user Cognito pool the owner is created in. */
  readonly userPool: cognito.IUserPool;
  /** The admin group the owner is added to (default `gateway-admins`). */
  readonly adminGroup?: string;
  /** The one deploy input; a package present without it fails the deploy (handled in the Lambda). */
  readonly ownerEmail?: string;
  /** Where the package lives in the bucket (default `preload/`). */
  readonly preloadPath?: string;
  /**
   * The value stamped into the custom resource's `Version` property. CloudFormation re-invokes a custom
   * resource on Update ONLY when a property changes, so a CONSTANT here means an upload-then-redeploy with
   * the same `ownerEmail`/`preloadPath` never re-runs the (idempotent) handler and a newly uploaded package
   * is never picked up. Pass a value that changes when a re-run is wanted — the app version (every CI build)
   * and/or an operator `preloadNonce`. Absent → `"1"` (first create still runs; redeploys will not).
   */
  readonly reinvokeSalt?: string;
  /**
   * How the handler's code is built. The managed `python3.13` runtime has boto3 but NOT PyYAML, which the Lab
   * skill-package validator imports to read `SKILL.md` frontmatter — so a plain `Code.fromAsset(agent/src)`
   * seeded projects and assets but failed every skill with `No module named 'yaml'` (staging, 2026-10-28).
   * The stack's no-Docker zip packager (the same one that builds the ingestion Lambdas) writes `baseCode`
   * plus the `ingestion` package and pip-installs {@link PRELOAD_REQUIREMENTS} as aarch64 wheels, so the
   * function runs on ARM_64.
   */
  readonly packaging: {
    /** Bucket the packager reads the code JSON from and writes the zip to. */
    readonly codeBucket: s3.IBucket;
    /** Service token of the zip-packager provider. */
    readonly packagerServiceToken: string;
    /** `relative path -> base64` source map (the agent runtime's map; `ingestion` is added here). */
    readonly baseCode: Record<string, string>;
  };
}

/**
 * Third-party packages the handler imports beyond the runtime's boto3. Keep in step with `agent/pyproject.toml`
 * (`PyYAML == 6.0.2`); `infra/test/preload.synth.test.ts` asserts they match.
 */
export const PRELOAD_REQUIREMENTS: readonly string[] = ["PyYAML==6.0.2"];

/** `agent/src/ingestion` as a `relative path -> base64` map (`.py`/`.txt`/`.json`), skipping caches. */
function ingestionSources(): Record<string, string> {
  const out: Record<string, string> = {};
  const walk = (dir: string, prefix: string): void => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      if (entry.name === "__pycache__" || entry.name.startsWith(".")) continue;
      const abs = path.join(dir, entry.name);
      const rel = path.posix.join(prefix, entry.name);
      if (entry.isDirectory()) walk(abs, rel);
      else if (entry.isFile() && /\.(py|txt|json)$/.test(entry.name)) {
        out[rel] = fs.readFileSync(abs).toString("base64");
      }
    }
  };
  walk(path.join(__dirname, "../../agent/src/ingestion"), "ingestion");
  return out;
}

/**
 * Wire the `PreloadFn` Lambda, its least-privilege role, and the CloudFormation custom resource that invokes it
 * on Create/Update. Returns the {@link cdk.CustomResource} so the caller can add further `node.addDependency(...)`
 * on the ingestion pipeline (so preload runs only once the sweep + tick can ingest what it queues).
 *
 * DEFERRED (design Task outline 9): an optional `BucketDeployment` that uploads a LOCAL preload package into
 * the Ingest bucket before this resource runs. Not implemented — today the operator uploads the package to
 * `preloadPath` out of band; wiring a local-folder upload is a follow-up.
 */
export function addPreloadResource(scope: Construct, id: string, props: PreloadResourceProps): cdk.CustomResource {
  const adminGroup = props.adminGroup ?? "gateway-admins";
  const preloadPath = props.preloadPath ?? "preload/";

  const role = new iam.Role(scope, `${id}Role`, {
    assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
    managedPolicies: [
      iam.ManagedPolicy.fromAwsManagedPolicyName("service-role/AWSLambdaBasicExecutionRole"),
    ],
    description: "Deploy-time preload: create owner, seed projects, place assets, validate skills",
  });

  // Cognito admin verbs on the one pool (owner resolution / creation / admin-group membership).
  role.addToPolicy(
    new iam.PolicyStatement({
      sid: "PreloadCognitoOwner",
      actions: [
        "cognito-idp:ListUsers",
        "cognito-idp:AdminCreateUser",
        "cognito-idp:AdminAddUserToGroup",
        "cognito-idp:AdminGetUser",
      ],
      resources: [props.userPool.userPoolArn],
    }),
  );

  // The projects table: seed META + MEMBER + mirror + name-guard and register assets.
  props.projectsTable.grantReadWriteData(role);

  // The Lab asset-index table: write the published skill row (and read it for idempotency).
  props.assetIndexTable.grantReadWriteData(role);

  // The Ingest bucket: read the package, write project assets + the import inbox.
  props.ingestBucket.grantRead(role, `${preloadPath.replace(/\/$/, "")}*`);
  props.ingestBucket.grantReadWrite(role, "projects/*");
  props.ingestBucket.grantRead(role);
  // A `.zip` package is unpacked into `<path without .zip>-unpacked/` (`preload.handler._unpack_zip`), which
  // first clears that scratch prefix so a file dropped from the zip does not linger. Write + delete there only.
  if (preloadPath.endsWith(".zip")) {
    const unpacked = `${preloadPath.slice(0, -".zip".length).replace(/\/$/, "")}-unpacked/*`;
    props.ingestBucket.grantReadWrite(role, unpacked);
  }

  // The Lab artifacts bucket: write the published skill package under the owner's prefix.
  props.artifactsBucket.grantReadWrite(role, "users/*");

  // The handler's package: the agent sources + `ingestion` + PyYAML, built by the zip packager (see `packaging`).
  const code: Record<string, string> = { ...props.packaging.baseCode, ...ingestionSources() };
  const codeHash = crypto
    .createHash("sha256")
    .update(JSON.stringify({ requirements: PRELOAD_REQUIREMENTS, code, v: "preload-v1" }))
    .digest("hex")
    .slice(0, 16);
  const codeKey = `preload_code_${codeHash}.json`;
  const zipKey = `preload_package_${codeHash}.zip`;
  const codeUpload = new s3deploy.BucketDeployment(scope, `${id}CodeAsset`, {
    sources: [s3deploy.Source.jsonData(codeKey, code)],
    destinationBucket: props.packaging.codeBucket,
    destinationKeyPrefix: "",
    prune: false,
  });
  const zip = new cdk.CustomResource(scope, `${id}ZipPackager`, {
    serviceToken: props.packaging.packagerServiceToken,
    properties: {
      BucketName: props.packaging.codeBucket.bucketName,
      ObjectKey: zipKey,
      Requirements: [...PRELOAD_REQUIREMENTS],
      AgentCodeBucket: props.packaging.codeBucket.bucketName,
      AgentCodeKey: codeKey,
      ContentHash: codeHash,
    },
  });
  zip.node.addDependency(codeUpload);

  const fn = new lambda.Function(scope, id, {
    functionName: `${props.projectName}-preload`,
    runtime: lambda.Runtime.PYTHON_3_13,
    // The packager installs aarch64 wheels (PyYAML has a native extension), so the function must be ARM_64.
    architecture: lambda.Architecture.ARM_64,
    handler: "preload.handler.handler",
    code: lambda.Code.fromBucket(props.packaging.codeBucket, zipKey),
    role,
    timeout: cdk.Duration.minutes(15),
    memorySize: 1024,
    environment: {
      DEPLOY_ENV: props.envName,
    },
    description: "Deploy-time preload custom resource backend",
  });
  fn.node.addDependency(zip);

  const provider = new cr.Provider(scope, `${id}Provider`, {
    onEventHandler: fn,
  });

  // The properties the handler reads; changing any of them (or `preloadPath`) re-runs on Update. The owner
  // email is NOT a secret, so it is fine as a plain property.
  return new cdk.CustomResource(scope, `${id}Resource`, {
    serviceToken: provider.serviceToken,
    properties: {
      IngestBucket: props.ingestBucket.bucketName,
      ProjectsTable: props.projectsTable.tableName,
      AssetIndexTable: props.assetIndexTable.tableName,
      ArtifactsBucket: props.artifactsBucket.bucketName,
      UserPoolId: props.userPool.userPoolId,
      AdminGroup: adminGroup,
      OwnerEmail: props.ownerEmail ?? "",
      PreloadPath: preloadPath,
      // Re-run trigger. CloudFormation only sends an Update to a custom resource when a property changes,
      // so this must change whenever a re-run is wanted (the handler is idempotent and no-ops when absent).
      // Keyed off the app version (every CI build) and/or an operator nonce — see `reinvokeSalt`. A constant
      // here was a bug: upload-then-redeploy with the same inputs never re-ran, so a freshly uploaded package
      // was ignored until an unrelated property changed.
      Version: props.reinvokeSalt ?? "1",
    },
  });
}
