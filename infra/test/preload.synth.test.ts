/**
 * Synth assertions over the preload wiring (design Task outline 1 + 9). Run:
 * `npx ts-node test/preload.synth.test.ts` (node:assert, no framework — matches the other infra tests).
 *
 * Two layers:
 *  1. The `addPreloadResource` construct in isolation — the Lambda, the Cognito owner policy, and the
 *     custom-resource properties (incl. the Lab asset-index table + artifacts bucket for the skill publish).
 *  2. The gate in `AgentInfraStack`: the `mediaworks-harness-preload` Lambda is present when the deploy is
 *     configured with preload context (`-c ownerEmail=…`) and ABSENT otherwise, so a normal deploy is
 *     byte-identical and nothing extra runs.
 */
import * as assert from "node:assert/strict";

import * as cdk from "aws-cdk-lib";
import { Template, Match } from "aws-cdk-lib/assertions";
import * as cognito from "aws-cdk-lib/aws-cognito";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as fs from "node:fs";
import * as path from "node:path";

import { buildApp } from "../lib/build-app";
import { addPreloadResource, PRELOAD_REQUIREMENTS } from "../lib/preload";

process.env.CDK_DEFAULT_ACCOUNT = process.env.CDK_DEFAULT_ACCOUNT || "111111111111";
process.env.CDK_DEFAULT_REGION = process.env.CDK_DEFAULT_REGION || "us-west-2";

const PROJECT = "mediaworks-harness";

/** The stack hands the preload construct its zip packager; a fake token + bucket is enough to synthesize. */
function packaging(stack: cdk.Stack, suffix = "") {
  return {
    codeBucket: new s3.Bucket(stack, `Code${suffix}`),
    packagerServiceToken: "arn:aws:lambda:us-west-2:111111111111:function:packager",
    baseCode: { "main.py": Buffer.from("# main").toString("base64") },
  };
}

function build(ownerEmail?: string, preloadPath = "preload/"): Template {
  const app = new cdk.App();
  const stack = new cdk.Stack(app, "PreloadTestStack", { env: { account: "111111111111", region: "us-west-2" } });
  const ingestBucket = new s3.Bucket(stack, "Ingest");
  const artifactsBucket = new s3.Bucket(stack, "Artifacts");
  const projectsTable = new dynamodb.Table(stack, "Projects", {
    partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
    sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
  });
  const assetIndexTable = new dynamodb.Table(stack, "AssetIndex", {
    partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
    sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
  });
  const userPool = new cognito.UserPool(stack, "Pool");
  addPreloadResource(stack, "PreloadFn", {
    projectName: PROJECT,
    envName: "staging",
    ingestBucket,
    projectsTable,
    assetIndexTable,
    artifactsBucket,
    userPool,
    ownerEmail,
    preloadPath,
    packaging: packaging(stack),
  });
  return Template.fromStack(stack);
}

// ---- Layer 1: the construct in isolation ----------------------------------------------------------
function constructInIsolation(): void {
  const t = build("owner@example.com");

  // The PreloadFn Lambda bundling agent/src with the Python handler.
  t.hasResourceProperties("AWS::Lambda::Function", Match.objectLike({
    Handler: "preload.handler.handler",
    FunctionName: "mediaworks-harness-preload",
    Runtime: "python3.13",
    Timeout: 900,
    // Built by the zip packager (aarch64 wheels), so ARM_64 and code from the bucket, not a local asset.
    Architectures: ["arm64"],
    Code: Match.objectLike({ S3Key: Match.stringLikeRegexp("^preload_package_[0-9a-f]{16}\\.zip$") }),
  }));

  // The packager installs PyYAML: the Lab skill validator imports it and the managed runtime lacks it, so
  // without it every skill failed with `No module named 'yaml'` (staging, 2026-10-28).
  const packagers = Object.values(t.findResources("AWS::CloudFormation::CustomResource"))
    .filter((r: any) => typeof r.Properties?.ObjectKey === "string" && r.Properties.ObjectKey.startsWith("preload_package_"));
  assert.equal(packagers.length, 1, "one zip packager builds the preload package");
  assert.deepEqual((packagers[0] as any).Properties.Requirements, [...PRELOAD_REQUIREMENTS]);

  // The pin matches agent/pyproject.toml, so the Lambda validates skills with the same PyYAML as the runtime.
  const pyproject = fs.readFileSync(path.join(__dirname, "../../agent/pyproject.toml"), "utf-8");
  const pinned = /"PyYAML\s*==\s*([0-9.]+)"/.exec(pyproject)?.[1];
  assert.ok(pinned, "agent/pyproject.toml pins PyYAML");
  assert.ok(PRELOAD_REQUIREMENTS.includes(`PyYAML==${pinned}`), `preload PyYAML pin matches pyproject (${pinned})`);

  // The Cognito owner-resolution policy (admin verbs, scoped to the one pool).
  t.hasResourceProperties("AWS::IAM::Policy", Match.objectLike({
    PolicyDocument: Match.objectLike({
      Statement: Match.arrayWith([
        Match.objectLike({
          Action: Match.arrayWith([
            "cognito-idp:ListUsers",
            "cognito-idp:AdminCreateUser",
            "cognito-idp:AdminAddUserToGroup",
          ]),
        }),
      ]),
    }),
  }));

  // The CloudFormation custom-resource properties the handler reads — including the skill-publish targets.
  const asJson = JSON.stringify(t.toJSON());
  for (const key of ["PreloadPath", "UserPoolId", "AssetIndexTable", "ArtifactsBucket", "ProjectsTable"]) {
    assert.ok(asJson.includes(key), `custom resource must pass ${key} to the handler`);
  }

  const fns = t.findResources("AWS::Lambda::Function");
  assert.ok(Object.keys(fns).length >= 1, "at least the PreloadFn Lambda");
  console.log("preload.synth.test: construct-in-isolation OK");
}

// ---- Layer 1a: a `.zip` package may write (and clear) its unpack prefix, nothing else -----------------
/** Every `s3:*` action the role's policies grant on a resource whose ARN string contains `needle`. */
function s3ActionsOn(t: Template, needle: string): string[] {
  const out: string[] = [];
  for (const pol of Object.values(t.findResources("AWS::IAM::Policy")) as any[]) {
    for (const st of pol.Properties.PolicyDocument.Statement) {
      const res = JSON.stringify(st.Resource ?? "");
      if (!res.includes(needle)) continue;
      out.push(...([] as string[]).concat(st.Action).filter((a) => a.startsWith("s3:")));
    }
  }
  return out;
}

function zipUnpackGrant(): void {
  // `preload.handler._unpack_zip` writes `<path>-unpacked/` and clears it first (so a file dropped from the zip
  // does not linger and get re-installed); without Put + Delete there the zip path fails AccessDenied.
  const zip = s3ActionsOn(build("owner@example.com", "pkg/example.zip"), "pkg/example-unpacked/*");
  assert.ok(zip.some((a) => a.startsWith("s3:PutObject")), "zip package: PutObject on the unpack prefix");
  assert.ok(zip.some((a) => a.startsWith("s3:DeleteObject")), "zip package: DeleteObject on the unpack prefix");
  // A folder package gets no write anywhere under the package path.
  const folder = s3ActionsOn(build("owner@example.com", "preload/"), "-unpacked");
  assert.equal(folder.length, 0, "folder package: no unpack-prefix grant");
  console.log("preload.synth.test: zip-unpack grant OK");
}

// ---- Layer 1b: the re-invoke salt reaches the custom resource's Version --------------------------
function reinvokeSalt(): void {
  // A passed salt is stamped into Version, so a changed salt (operator nonce / new app version) forces
  // CloudFormation to re-invoke the idempotent handler on redeploy — the fix for the former constant "1".
  const app = new cdk.App();
  const stack = new cdk.Stack(app, "SaltStack", { env: { account: "111111111111", region: "us-west-2" } });
  const mk = () => ({
    ingestBucket: new s3.Bucket(stack, `Ingest${Math.random().toString(36).slice(2)}`),
    artifactsBucket: new s3.Bucket(stack, `Artifacts${Math.random().toString(36).slice(2)}`),
    projectsTable: new dynamodb.Table(stack, `Projects${Math.random().toString(36).slice(2)}`, {
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
    }),
    assetIndexTable: new dynamodb.Table(stack, `AssetIndex${Math.random().toString(36).slice(2)}`, {
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
    }),
    userPool: new cognito.UserPool(stack, `Pool${Math.random().toString(36).slice(2)}`),
  });
  addPreloadResource(stack, "SaltFn", {
    projectName: PROJECT, envName: "staging", ...mk(),
    ownerEmail: "owner@example.com", preloadPath: "preload/", reinvokeSalt: "2026.10.28-rc.1234",
    packaging: packaging(stack, "Salt"),
  });
  const json = JSON.stringify(Template.fromStack(stack).toJSON());
  assert.ok(json.includes("2026.10.28-rc.1234"), "the reinvokeSalt is stamped into the custom resource Version");
  assert.ok(!json.includes('"Version":"1"') && !json.includes('"Version": "1"'),
    "a non-default salt replaces the constant default");
  console.log("preload.synth.test: reinvoke-salt OK");
}

// ---- Layer 2: the AgentInfraStack gate ------------------------------------------------------------
function preloadLambdaCount(t: Template): number {
  return Object.values(t.findResources("AWS::Lambda::Function")).filter(
    (r: any) => r.Properties?.FunctionName === `${PROJECT}-preload`,
  ).length;
}

function stackGate(): void {
  // Configured (operator passed `-c ownerEmail=…`) → the preload Lambda is wired into staging.
  const onApp = new cdk.App({ context: { env: "staging", projectName: PROJECT, ownerEmail: "owner@example.com" } });
  const on = Template.fromStack(buildApp(onApp).harness);
  assert.equal(preloadLambdaCount(on), 1, "preload Lambda present when ownerEmail is configured");

  // Not configured → the construct is not instantiated at all (no extra resource, no deploy slowdown).
  const offApp = new cdk.App({ context: { env: "staging", projectName: PROJECT } });
  const off = Template.fromStack(buildApp(offApp).harness);
  assert.equal(preloadLambdaCount(off), 0, "no preload Lambda when preload is not configured");

  console.log("preload.synth.test: AgentInfraStack gate OK (present when configured, absent otherwise)");
}

constructInIsolation();
zipUnpackGrant();
reinvokeSalt();
stackGate();
console.log("preload.synth.test: OK");
