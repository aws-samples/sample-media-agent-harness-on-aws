/**
 * Synth assertions for `projects.lineageEdges` (env-config). Run:
 * `npx ts-node test/lineage-edges.synth.test.ts` (node:assert, no framework — matches the other synth tests).
 *
 * ON in staging only:
 *  - INGESTION_CONFIG carries `"lineageEdges":true` everywhere it is embedded: RuntimeConfigSecret (the Main/Task
 *    runtimes), the UI proxy task, and — in the nested IngestionStack — the Asset_Service and GeneratedMediaFn;
 *  - the plan chainer has LINEAGE_EDGES=true;
 *  - no new resource type, no GSI: edges are items in the Project's existing partition.
 * OFF in prod, sandbox and customer: neither `lineageEdges` nor `LINEAGE_EDGES` appears in the parent template or
 * any nested one (the flag adds nothing — the template is the one it was before the flag existed).
 * And `lineageEdges` without `ingestion.generatedMedia` (the edge writer) refuses to synthesize.
 */
import * as assert from "node:assert/strict";
import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { buildApp } from "../lib/build-app";
import { resolveEnvConfig, validateProjectsConfig, EnvName } from "../lib/env-config";

process.env.CDK_DEFAULT_ACCOUNT = process.env.CDK_DEFAULT_ACCOUNT || "111111111111";
process.env.CDK_DEFAULT_REGION = process.env.CDK_DEFAULT_REGION || "us-west-2";

const PROJECT = "mediaworks-harness";

function synth(env: EnvName): { parent: Template; ingestion?: Template } {
  const app = new cdk.App({ context: { env, projectName: PROJECT } });
  const { harness } = buildApp(app);
  const parent = Template.fromStack(harness);
  const ingestion = harness.ingestionStack ? Template.fromStack(harness.ingestionStack) : undefined;
  return { parent, ingestion };
}

function json(t: Template): string {
  return JSON.stringify(t.toJSON());
}

function lambdaEnv(t: Template, idPrefix: string): Record<string, unknown> {
  const fns = Object.entries(t.findResources("AWS::Lambda::Function")).filter(([id]) => id.startsWith(idPrefix));
  assert.equal(fns.length, 1, `exactly one ${idPrefix}* Lambda`);
  return (fns[0][1] as any).Properties.Environment?.Variables ?? {};
}

function ingestionConfigOf(v: unknown): Record<string, unknown> {
  assert.equal(typeof v, "string", "INGESTION_CONFIG is a JSON string");
  return JSON.parse(v as string);
}

// ---------------------------------------------------------------------------------------------- staging: ON
{
  const { parent, ingestion } = synth("staging");
  assert.ok(ingestion, "staging has the IngestionStack");
  const p = json(parent);
  assert.ok(p.includes('\\"lineageEdges\\":true'), "RuntimeConfigSecret / proxy INGESTION_CONFIG carry lineageEdges");

  const chainer = lambdaEnv(parent, "PlanChainerLambda");
  assert.equal(chainer.LINEAGE_EDGES, "true", "the plan chainer records node inputs/outputs");

  for (const fn of ["AssetServiceFn", "GeneratedMediaFn"]) {
    const env = lambdaEnv(ingestion!, fn);
    assert.equal(ingestionConfigOf(env.INGESTION_CONFIG).lineageEdges, true, `${fn} sees lineageEdges`);
  }
  // Edges are items in the Project partition: no new table, no new GSI on the projects table.
  const tables = parent.findResources("AWS::DynamoDB::Table");
  for (const [, t] of Object.entries(tables)) {
    const gsis = ((t as any).Properties.GlobalSecondaryIndexes ?? []).map((g: any) => g.IndexName);
    assert.ok(!gsis.some((n: string) => /lineage|derived/i.test(n)), "no lineage GSI");
  }
  console.log("staging: lineageEdges on in INGESTION_CONFIG (runtimes, proxy, Asset_Service, GeneratedMediaFn) + chainer — ok");
}

// ---------------------------------------------------------------------------------------------- off envs
for (const env of ["prod", "sandbox", "customer"] as EnvName[]) {
  const { parent, ingestion } = synth(env);
  for (const [name, t] of [["parent", parent], ["ingestion", ingestion]] as const) {
    if (!t) continue;
    const s = json(t);
    assert.ok(!s.includes("lineageEdges"), `${env} ${name}: no lineageEdges`);
    assert.ok(!s.includes("LINEAGE_EDGES"), `${env} ${name}: no LINEAGE_EDGES`);
  }
  assert.equal(lambdaEnv(parent, "PlanChainerLambda").LINEAGE_EDGES, undefined, `${env}: chainer unchanged`);
  console.log(`${env}: lineageEdges absent from every template — ok`);
}

// ---------------------------------------------------------------------------------------------- validation
{
  const app = new cdk.App({ context: { env: "staging", projectName: PROJECT } });
  const cfg = resolveEnvConfig(app);
  assert.throws(
    () => validateProjectsConfig({
      ...cfg, projects: { ...cfg.projects!, ingestion: { ...cfg.projects!.ingestion!, generatedMedia: false } },
    }),
    /lineageEdges requires/,
  );
  console.log("lineageEdges without generatedMedia: refused — ok");
}

console.log("lineage-edges.synth: all assertions passed");
