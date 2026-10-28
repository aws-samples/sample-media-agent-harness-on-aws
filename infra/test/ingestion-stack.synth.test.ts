/**
 * Synth assertions over the IngestionStack + ArtifactsCdn refusal (media-harness-project-ingestion
 * design §2, §18, §20). Run: `npx ts-node test/ingestion-stack.synth.test.ts` (node:assert, no
 * framework — matches the other infra synth tests).
 *
 * It instantiates the real `AgentInfraStack` for staging, sandbox, and prod and asserts over the
 * synthesized nested + parent templates:
 *  - nested IngestionStack exists only where `projects.ingestion.enabled` (sandbox, staging), with the
 *    Asset_Service Lambda (arm64, correct handler/role) and its two invoke resource-policies;
 *  - the parent RuntimeConfigSecret carries ASSET_SERVICE_FN + INGESTION_CONFIG where ingestion is on,
 *    and omits ASSET_SERVICE_FN in prod;
 *  - the ArtifactsCdn `projects/*` 403 refusal (CloudFront Function + behaviour) is present in EVERY
 *    environment, prod included (Req 11.6-11.8).
 */
import * as assert from "node:assert/strict";
import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { AgentInfraStack } from "../lib/agent-infra-stack";
import { resolveEnvConfig, EnvName } from "../lib/env-config";
import { PERSONAL_SAMPLES } from "../lib/personal-samples";

const PROJECT = "mediaworks-harness";

interface Built {
  stack: AgentInfraStack;
  nested: Template | undefined;
  parent: Template;
}

function build(env: EnvName): Built {
  // createBuckets mirrors cdk.json, so the synth matches what the deploy creates (bucket policies included).
  const app = new cdk.App({ context: { env, projectName: PROJECT, createBuckets: true } });
  const envConfig = resolveEnvConfig(app);
  const stackName = env === "dev" ? `${PROJECT}-infra` : `${PROJECT}-infra-${env}`;
  const stack = new AgentInfraStack(app, stackName, {
    projectName: PROJECT,
    envConfig,
    env: { account: envConfig.account, region: envConfig.region },
  });
  return {
    stack,
    nested: stack.ingestionStack ? Template.fromStack(stack.ingestionStack) : undefined,
    parent: Template.fromStack(stack),
  };
}

function resourcesOf(t: Template, type: string): any[] {
  return Object.values(t.findResources(type)).map((r: any) => r.Properties ?? {});
}

/** Assert the ArtifactsCdn carries the ProjectsDeny CloudFront Function + a `projects/*` behaviour. */
function assertArtifactsCdnDeny(parent: Template, env: string): void {
  const fns = resourcesOf(parent, "AWS::CloudFront::Function");
  const deny = fns.find((f) => JSON.stringify(f).includes("projects"));
  assert.ok(deny, `${env}: a ProjectsDeny CloudFront Function exists`);
  const code = JSON.stringify(deny.FunctionCode ?? deny);
  assert.ok(code.includes("403"), `${env}: ProjectsDeny returns 403`);

  const dists = resourcesOf(parent, "AWS::CloudFront::Distribution");
  const artifacts = dists.find((d) => {
    const behaviors = d.DistributionConfig?.CacheBehaviors ?? [];
    return behaviors.some((b: any) => b.PathPattern === "projects/*");
  });
  assert.ok(artifacts, `${env}: the ArtifactsCdn has a projects/* behaviour (Req 11.6)`);
  const cfg = artifacts.DistributionConfig;
  // Both the projects/* behaviour AND the default behaviour associate a function (Req 11.7).
  const projectsBehavior = cfg.CacheBehaviors.find((b: any) => b.PathPattern === "projects/*");
  assert.ok(
    (projectsBehavior.FunctionAssociations ?? []).length >= 1,
    `${env}: projects/* behaviour associates the deny function`,
  );
  assert.ok(
    (cfg.DefaultCacheBehavior?.FunctionAssociations ?? []).length >= 1,
    `${env}: the default behaviour also associates the deny function (percent-encoding bypass)`,
  );
}

// ------------------------------------------------------------------------------------------------
// prod (since 2026-10): IngestionStack + ASSET_SERVICE_FN, and the ArtifactsCdn refusal.
// ------------------------------------------------------------------------------------------------
{
  const prod = build("prod");
  assert.ok(prod.stack.ingestionStack, "prod must have an IngestionStack");
  const prodJson = JSON.stringify(prod.parent.toJSON());
  assert.ok(prodJson.includes("ASSET_SERVICE_FN"), "prod secret must carry ASSET_SERVICE_FN");
  assertArtifactsCdnDeny(prod.parent, "prod");
  // The demo tour's 0-demo.mp4 reaches every prod user's Personal project too.
  const prodAsset = resourcesOf(Template.fromStack(prod.stack.ingestionStack!), "AWS::Lambda::Function")
    .find((f: any) => f.Handler === "ingestion.handlers.asset_service.handler");
  assert.deepEqual(JSON.parse(prodAsset.Environment.Variables.PERSONAL_SAMPLES), [...PERSONAL_SAMPLES],
    "prod: the Asset_Service gets PERSONAL_SAMPLES");
  assert.equal(Object.values(prod.parent.findResources("Custom::CDKBucketDeployment"))
    .filter((r: any) => r.Properties.DestinationBucketKeyPrefix === "samples/").length, 1, "prod: samples/ is uploaded");
  console.log("prod: ingestion stack, ASSET_SERVICE_FN, deny present — ok");
}

// ------------------------------------------------------------------------------------------------
// staging + sandbox: IngestionStack with the Asset_Service Lambda; parent secret + proxy wiring.
// ------------------------------------------------------------------------------------------------
for (const env of ["staging", "sandbox"] as const) {
  const { stack, nested, parent } = build(env);
  assert.ok(nested, `${env} must have an IngestionStack`);
  const t = nested!;

  // --- Asset_Service Lambda: arm64, the resolve/register handler, on the AssetServiceRole ---
  const fns = t.findResources("AWS::Lambda::Function");
  const assetFn = Object.entries(fns).find(
    ([, r]) => (r as any).Properties?.Handler === "ingestion.handlers.asset_service.handler",
  );
  assert.ok(assetFn, `${env}: the Asset_Service Lambda exists`);
  const props = (assetFn![1] as any).Properties;
  assert.deepEqual(props.Architectures, ["arm64"], `${env}: Asset_Service is arm64 (design §2)`);
  assert.equal(props.Timeout, 90, `${env}: Asset_Service timeout 90 s`);
  assert.equal(props.MemorySize, 1024, `${env}: Asset_Service 1,024 MB`);
  assert.ok(JSON.stringify(props.Role).includes("AssetServiceRole"), `${env}: on the AssetServiceRole`);
  assert.ok(props.Environment?.Variables?.PROJECTS_TABLE, `${env}: PROJECTS_TABLE env`);
  assert.ok(props.Environment?.Variables?.INGEST_BUCKET, `${env}: INGEST_BUCKET env`);
  assert.ok(props.Environment?.Variables?.INGESTION_CONFIG, `${env}: INGESTION_CONFIG env`);

  // --- Resource policy: at least the proxy + main-agent invoke permissions ---
  const perms = resourcesOf(t, "AWS::Lambda::Permission");
  assert.ok(perms.length >= 2, `${env}: Asset_Service has >= 2 invoke permissions (proxy + main-agent)`);
  for (const p of perms) {
    assert.equal(p.Action, "lambda:InvokeFunction", `${env}: invoke permission action`);
  }

  // --- AssetServiceRole: projects-table RW (incl. transactions) + Ingest-bucket read ---
  const roleJson = JSON.stringify(resourcesOf(t, "AWS::IAM::Role").concat(resourcesOf(t, "AWS::IAM::Policy")));
  assert.ok(roleJson.includes("dynamodb:TransactWriteItems"), `${env}: role can run the registration txn`);
  assert.ok(roleJson.includes("s3:GetObject"), `${env}: role can HeadObject/GetObject in the Ingest bucket`);
  assert.ok(roleJson.includes("s3:ListBucket"), `${env}: role can list the projects/ prefix`);
  // `remove` may delete ONLY owned bytes: projects/*, chat copies under artifacts/*/input/*, and a `session`
  // asset's in-place source under artifacts/*/output/* (asset-lineage design §2) — never artifacts/external/.
  const policies = Object.values(t.findResources("AWS::IAM::Policy"))
    .filter((p: any) => JSON.stringify(p.Properties?.Roles ?? []).includes("AssetServiceRole"))
    .map((p: any) => p.Properties);
  const deletes = policies.flatMap((p: any) =>
    (p.PolicyDocument?.Statement ?? []).filter((s: any) =>
      ([] as string[]).concat(s.Action).some((a) => a.startsWith("s3:Delete")),
    ),
  );
  assert.equal(deletes.length, 1, `${env}: exactly one s3:Delete* statement on the AssetServiceRole`);
  assert.deepEqual([].concat(deletes[0].Action), ["s3:DeleteObject"], `${env}: DeleteObject only (no version deletes)`);
  const deleteResources = JSON.stringify(deletes[0].Resource);
  assert.ok(deleteResources.includes("/projects/*"), `${env}: DeleteObject on projects/*`);
  assert.ok(deleteResources.includes("/artifacts/*/input/*"), `${env}: DeleteObject on artifacts/*/input/*`);
  assert.ok(deleteResources.includes("/artifacts/*/output/*"), `${env}: DeleteObject on artifacts/*/output/*`);
  assert.ok(!deleteResources.includes("/artifacts/*\"") && !deleteResources.includes("external"),
    `${env}: no DeleteObject on all of artifacts/* or the shared external/ namespace`);
  assert.equal(
    [].concat(deletes[0].Resource).length,
    3,
    `${env}: DeleteObject on exactly those three prefixes`,
  );

  // --- Pipeline (design §7, §8): Dispatcher on the stream + sweep, AssetIngestion tick loop ---
  const handlers = Object.values(fns).map((r: any) => r.Properties?.Handler);
  assert.ok(handlers.includes("ingestion.handlers.dispatcher.handler"), `${env}: Dispatcher Lambda`);
  assert.ok(handlers.includes("ingestion.handlers.tick.handler"), `${env}: IngestTick Lambda`);
  const esm = resourcesOf(t, "AWS::Lambda::EventSourceMapping");
  const genMedia = env === "staging"; // `ingestion.generatedMedia` is on in staging only
  assert.equal(esm.length, genMedia ? 4 : 2,
    `${env}: dispatcher + purge-trigger stream mappings${genMedia ? " + Generated_Media queue and job stream" : ""}`);
  const filters = esm.map((m) => JSON.stringify(m.FilterCriteria ?? {}));
  assert.ok(filters.some((f) => f.includes("INSERT") && f.includes("QUEUE#")), `${env}: dispatcher on QUEUE# inserts`);
  assert.ok(filters.some((f) => f.includes("MODIFY") && f.includes("deleted") && f.includes("active")),
    `${env}: purge trigger on META active -> deleted`);
  // HeroFrame (sampled video thumbnail): rides the Dispatcher's mapping — no third projects-stream reader.
  const heroFn = Object.entries(fns).find(
    ([, r]) => (r as any).Properties?.Handler === "ingestion.handlers.hero_frame.handler");
  assert.ok(heroFn, `${env}: HeroFrameFn exists`);
  const heroProps = (heroFn![1] as any).Properties;
  assert.equal(heroProps.Architectures?.[0], "arm64", `${env}: HeroFrameFn is arm64 (the av wheel is aarch64)`);
  assert.ok(JSON.stringify(heroProps.Code).includes("hero_frame_package_"), `${env}: HeroFrameFn uses its own package`);
  const dispFn = Object.values(fns).find((r: any) => r.Properties?.Handler === "ingestion.handlers.dispatcher.handler") as any;
  assert.ok(dispFn.Properties.Environment.Variables.HERO_FRAME_FN, `${env}: Dispatcher env HERO_FRAME_FN`);
  const dispMapping = esm.find((m) => JSON.stringify(m.FilterCriteria ?? {}).includes("QUEUE#"));
  assert.ok(JSON.stringify(dispMapping.FilterCriteria).includes("ASSET#video:"),
    `${env}: the Dispatcher mapping also matches ASSET#video: inserts`);
  // Stream mappings (StartingPosition set) other than Generated_Media's job-registry one read the projects stream.
  const streamReaders = esm.filter((m) => m.StartingPosition
    && !JSON.stringify(m.FilterCriteria ?? {}).includes("resultKey")).length;
  assert.equal(streamReaders, 2, `${env}: exactly two projects-stream readers (Dispatcher + PurgeTrigger)`);
  const heroPolicies = Object.values(t.findResources("AWS::IAM::Policy"))
    .filter((p: any) => JSON.stringify(p.Properties?.Roles ?? []).includes("HeroFrameRole"))
    .flatMap((p: any) => p.Properties.PolicyDocument.Statement);
  const heroPuts = heroPolicies.filter((s: any) => ([] as string[]).concat(s.Action).includes("s3:PutObject"));
  assert.equal(heroPuts.length, 1, `${env}: one PutObject statement`);
  assert.ok(JSON.stringify(heroPuts[0].Resource).includes("/projects/*/rendition.jpg"),
    `${env}: HeroFrame writes rendition.jpg only`);
  const dispPolicies = Object.values(t.findResources("AWS::IAM::Policy"))
    .filter((p: any) => JSON.stringify(p.Properties?.Roles ?? []).includes("DispatcherRole"))
    .flatMap((p: any) => p.Properties.PolicyDocument.Statement);
  assert.ok(dispPolicies.some((s: any) => ([] as string[]).concat(s.Action).includes("lambda:InvokeFunction")),
    `${env}: Dispatcher may invoke HeroFrame`);
  // Generated_Media (design §16; asset-lineage §3): queue + job-registry terminal rows; registers in place,
  // so it writes no S3 object, and it reads the M2C proxy bucket.
  const gmFn = Object.entries(fns).find(
    ([, r]) => (r as any).Properties?.Handler === "ingestion.handlers.generated_media.handler");
  assert.equal(!!gmFn, genMedia, `${env}: GeneratedMediaFn iff generatedMedia`);
  const parentEnvJson = JSON.stringify(parent.toJSON());
  assert.equal(parentEnvJson.includes("GENERATED_MEDIA_QUEUE_URL"), genMedia,
    `${env}: proxy GENERATED_MEDIA_QUEUE_URL iff generatedMedia`);
  if (genMedia) {
    assert.ok(filters.some((f) => f.includes("resultKey") && f.includes("SUCCEEDED")),
      `${env}: job stream filtered to terminal rows with a resultKey`);
    const queues = resourcesOf(t, "AWS::SQS::Queue");
    const q = queues.find((x) => x.QueueName === `${PROJECT}-generated-media`);
    assert.ok(q && q.VisibilityTimeout === 120 && q.RedrivePolicy?.maxReceiveCount === 4,
      `${env}: GeneratedMediaQueue visibility 120 s, 4 receives to the DLQ`);
    const gmPolicies = Object.values(t.findResources("AWS::IAM::Policy"))
      .filter((p: any) => JSON.stringify(p.Properties?.Roles ?? []).includes("GeneratedMediaRole"))
      .flatMap((p: any) => p.Properties.PolicyDocument.Statement);
    const writes = gmPolicies.filter((s: any) => [].concat(s.Action).some((a: string) => /s3:(Put|Delete)/.test(a)));
    assert.equal(writes.length, 0, `${env}: GeneratedMedia has no S3 write (in-place registration, no CopyObject)`);
    const m2cRead = gmPolicies.find((s: any) => s.Sid === "M2cOutputsRead");
    assert.ok(m2cRead, `${env}: GeneratedMedia may read the M2C proxy bucket`);
    assert.deepEqual(([] as string[]).concat(m2cRead.Action).sort(), ["s3:GetObject", "s3:GetObjectAttributes"],
      `${env}: M2C read is GetObject + GetObjectAttributes only`);
    const srcRead = gmPolicies.find((s: any) => s.Sid === "SourcesAndResultsRead");
    assert.ok(([] as string[]).concat(srcRead.Action).includes("s3:GetObjectAttributes"), `${env}: GetObjectAttributes on artifacts/*`);
    // asset-lineage Phase 3 (a): it reads + lists the project layout's session roots — and only those.
    const srcRes = JSON.stringify(srcRead.Resource);
    assert.ok(srcRes.includes("/projects/*/sessions/*"), `${env}: GeneratedMedia reads projects/*/sessions/*`);
    assert.ok(!/\/projects\/\*"/.test(srcRes), `${env}: never all of projects/*`);
    const list = gmPolicies.find((s: any) => s.Sid === "SessionList");
    assert.deepEqual(list.Condition.StringLike["s3:prefix"], ["artifacts/*", "projects/*/sessions/*"],
      `${env}: GeneratedMedia lists both session roots`);
    assert.ok(parentEnvJson.includes("GeneratedMediaSend"), `${env}: proxy may SendMessage to the queue`);
    // Folders (design D20): the session title comes from one GetItem of the sessions table, read-only.
    const gmEnv = (gmFn![1] as any).Properties.Environment.Variables;
    assert.ok(gmEnv.SESSIONS_TABLE, `${env}: GeneratedMediaFn env SESSIONS_TABLE`);
    assert.ok(gmEnv.MEDIA_PROXY_BUCKET, `${env}: GeneratedMediaFn env MEDIA_PROXY_BUCKET`);
    const titleRead = gmPolicies.filter((s: any) => s.Sid === "SessionTitleRead");
    assert.equal(titleRead.length, 1, `${env}: one SessionTitleRead statement`);
    assert.deepEqual([].concat(titleRead[0].Action), ["dynamodb:GetItem"], `${env}: session title read is GetItem only`);
  }
  // Only the PurgeRole may delete object versions (Req 10.21).
  const versionDeleters = Object.entries(t.findResources("AWS::IAM::Policy")).filter(([, p]: any) =>
    JSON.stringify(p.Properties.PolicyDocument).includes("s3:DeleteObjectVersion"));
  assert.equal(versionDeleters.length, 1, `${env}: exactly one policy grants s3:DeleteObjectVersion`);
  assert.ok(JSON.stringify(versionDeleters[0][1]).includes("PurgeRole"), `${env}: and it is the PurgeRole's`);
  const rules = resourcesOf(t, "AWS::Events::Rule");
  assert.ok(rules.some((r) => r.ScheduleExpression === "rate(1 minute)"), `${env}: 1-minute sweep`);
  const sms = resourcesOf(t, "AWS::StepFunctions::StateMachine");
  assert.deepEqual(sms.map((m) => m.StateMachineName).sort(),
    [`${PROJECT}-asset-ingestion`, `${PROJECT}-collection-rebuild`, `${PROJECT}-project-purge`],
    `${env}: AssetIngestion, CollectionRebuild, ProjectPurge`);
  const ingestionSm = sms.find((m) => m.StateMachineName === `${PROJECT}-asset-ingestion`);
  const def = JSON.stringify(ingestionSm.DefinitionString);
  assert.ok(def.includes("SecondsPath") && def.includes("$.wait_s"), `${env}: Wait reads the tick's wait_s`);
  assert.ok(def.includes("TimeoutSeconds") && def.includes("86400"), `${env}: 24 h execution backstop`);
  const tickEnv = (Object.values(fns).find((r: any) => r.Properties?.Handler === "ingestion.handlers.tick.handler") as any)
    .Properties.Environment.Variables;
  for (const k of ["M2C_GATEWAY_URL", "M2C_CLIENT_ID", "M2C_TOKEN_ENDPOINT", "M2C_SECRET_ARN", "ASSET_INGESTION_SFN_ARN"]) {
    assert.ok(tickEnv[k], `${env}: IngestTick env ${k}`);
  }

  // --- IngestTickRole: reads every place a `session` asset's in-place source can live (asset-lineage WP1) ---
  // The image rendition GETs row.sourceUri. Staging 2026-10-09: 20 fal images under artifacts/external/
  // failed "the image cannot be read: AccessDenied" because the role read projects/* only.
  const tickStatements = Object.values(t.findResources("AWS::IAM::Policy"))
    .filter((p: any) => JSON.stringify(p.Properties?.Roles ?? []).includes("IngestTickRole"))
    .flatMap((p: any) => p.Properties.PolicyDocument.Statement);
  const tickGetRes = JSON.stringify(tickStatements
    .filter((s: any) => [].concat(s.Action).includes("s3:GetObject" as never)).map((s: any) => s.Resource));
  for (const prefix of ["/artifacts/*", "/projects/*"]) {
    assert.ok(tickGetRes.includes(`${prefix}"`), `${env}: IngestTick may GetObject ${prefix} (in-place session sources)`);
  }
  const tickPuts = tickStatements.filter((s: any) => [].concat(s.Action).some((a: string) => /^s3:(Put|Delete)/.test(a)));
  assert.equal(tickPuts.length, 1, `${env}: one IngestTick S3 write statement`);
  assert.deepEqual(JSON.stringify(tickPuts[0].Resource).match(/\/[a-z]+\/\*/g), ["/projects/*"],
    `${env}: IngestTick writes Document_Records under projects/* only`);

  // --- ImportSweep: the import inbox only LISTS the bucket and never touches its notification config ---
  const sweepFns = resourcesOf(t, "AWS::Lambda::Function")
    .filter((f: any) => f.Handler === "ingestion.handlers.import_sweep.handler");
  assert.equal(sweepFns.length, 1, `${env}: one ImportSweep function`);
  const sweepStatements = resourcesOf(t, "AWS::IAM::Policy")
    .filter((p: any) => JSON.stringify(p.Roles ?? []).includes("ImportSweepRole"))
    .flatMap((p: any) => p.PolicyDocument.Statement);
  const sweepS3 = sweepStatements.flatMap((st: any) => [].concat(st.Action)).filter((a: string) => a.startsWith("s3:"));
  assert.deepEqual(sweepS3, ["s3:ListBucket"], `${env}: ImportSweep may only list the bucket (registers in place)`);
  const nestedJson = JSON.stringify(t.toJSON());
  assert.ok(!/PutBucketNotification|BucketNotifications/.test(nestedJson),
    `${env}: nothing rewrites the shared ingest bucket's notification configuration`);

  // --- Parent: secret carries ASSET_SERVICE_FN + INGESTION_CONFIG; ArtifactsCdn deny present ---
  const parentJson = JSON.stringify(parent.toJSON());
  assert.ok(parentJson.includes("ASSET_SERVICE_FN"), `${env}: parent secret has ASSET_SERVICE_FN`);
  assert.ok(parentJson.includes("INGESTION_CONFIG"), `${env}: parent secret has INGESTION_CONFIG`);
  assertArtifactsCdnDeny(parent, env);
  if (env === "staging") {
    const policies = resourcesOf(parent, "AWS::S3::BucketPolicy");
    const st = policies.flatMap((p: any) => p.PolicyDocument?.Statement ?? [])
      .find((s: any) => s.Sid === "AllowM2CHybridSearchReadRenditions");
    assert.ok(st, "staging: hybrid-search may read Search_Renditions");
    assert.deepEqual([].concat(st.Action), ["s3:GetObject"], "staging: rendition read is GetObject only");
    assert.ok(JSON.stringify(st.Resource).includes("/projects/*/rendition.jpg"), "staging: renditions only");
  }

  // --- Personal samples (the demo tour's 0-demo.mp4): env, exact-key IAM, one upload to samples/ ---
  // On in staging (and prod + customer); sandbox leaves it off, so its Asset_Service has no PERSONAL_SAMPLES.
  if (env === "sandbox") {
    const asset = resourcesOf(t, "AWS::Lambda::Function")
      .find((f: any) => f.Handler === "ingestion.handlers.asset_service.handler");
    assert.equal(asset.Environment.Variables.PERSONAL_SAMPLES, undefined, "sandbox: no personal samples");
  } else {
    const asset = resourcesOf(t, "AWS::Lambda::Function")
      .find((f: any) => f.Handler === "ingestion.handlers.asset_service.handler");
    const samples = JSON.parse(asset.Environment.Variables.PERSONAL_SAMPLES ?? "[]");
    assert.deepEqual(samples, [...PERSONAL_SAMPLES], `${env}: the Asset_Service gets PERSONAL_SAMPLES`);
    const stmts = resourcesOf(t, "AWS::IAM::Policy")
      .filter((p: any) => JSON.stringify(p.Roles ?? []).includes("AssetServiceRole"))
      .flatMap((p: any) => p.PolicyDocument.Statement);
    const read = stmts.find((s: any) => s.Sid === "IngestBucketReadPersonalSamples");
    const write = stmts.find((s: any) => s.Sid === "IngestBucketWritePersonalSampleCopies");
    assert.ok(read && write, `${env}: the sample read + copy grants exist`);
    assert.deepEqual([].concat(read.Action), ["s3:GetObject"]);
    assert.deepEqual([].concat(write.Action), ["s3:PutObject"]);
    assert.ok(JSON.stringify(read.Resource).includes("/samples/0-demo.mp4") && !JSON.stringify(read.Resource).includes("samples/*"),
      `${env}: the read names the sample key exactly`);
    assert.ok(JSON.stringify(write.Resource).includes("/projects/*/0-demo.mp4"),
      `${env}: the copy may write only <project>/<asset>/0-demo.mp4`);
    const deploys = Object.values(parent.findResources("Custom::CDKBucketDeployment")).map((r: any) => r.Properties)
      .filter((p: any) => p.DestinationBucketKeyPrefix === "samples/");
    assert.equal(deploys.length, 1, `${env}: one BucketDeployment uploads samples/`);
    assert.equal(deploys[0].Prune, false, `${env}: the samples upload never deletes other samples/ objects`);
    assert.equal(deploys[0].RetainOnDelete, true, `${env}: users' samples survive a stack delete`);
  }

  console.log(`${env}: all IngestionStack synth assertions passed`);
}

console.log("ingestion-stack.synth: all assertions passed");
