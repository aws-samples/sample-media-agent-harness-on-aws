/**
 * Runtime-parallelism guard: no AgentCore runtime may wait on another runtime during a deploy.
 *
 * Run: `npm run test:runtime-parallel` (node:assert, no framework — matches the other synth tests).
 *
 * Why: CloudFormation updates a resource only after everything it references has finished. An
 * inline env var holding `<other>.agentRuntimeArn` (a GetAtt), or an InvokeAgentRuntime grant on
 * that GetAtt inside an execution-role DefaultPolicy the runtime DependsOn, serialised the runtime
 * updates into render → task → main (~4 min each). The sub-runtime ARNs now travel through SSM
 * parameters under RUNTIME_ARN_PARAM_PATH, read by the agent at startup and lazily at call time
 * (agent/src/runtime_arns.py), and grants are scoped by runtime NAME.
 *
 * Asserts, per env:
 *   1. No AWS::BedrockAgentCore::Runtime transitively depends on another Runtime (following Ref,
 *      Fn::GetAtt, Fn::Sub `${X}`/`${X.Attr}`, and DependsOn).
 *   (and 4: no ECS task definition OR service reaches a runtime — the service via its task role's policy.)
 *   2. None of the sub-runtime ARN env vars appears in any runtime's EnvironmentVariables.
 *   3. The `/<project>-<env>/runtime-arns/<VAR>` SSM parameters exist, and the path is in
 *      RuntimeConfigSecret.
 *   5. `UI_PUBLIC_ORIGIN` (the agent's project tools) exists there as an https origin.
 * Prints the runtime critical path (longest runtime → runtime chain; 1 = fully parallel).
 */
import * as assert from "node:assert/strict";
import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { AgentInfraStack } from "../lib/agent-infra-stack";
import { resolveEnvConfig, EnvName } from "../lib/env-config";

const PROJECT = "mediaworks-harness";
const RUNTIME_TYPE = "AWS::BedrockAgentCore::Runtime";
const ARN_VARS = [
  "TASK_AGENT_RUNTIME_ARN",
  "PLANNING_AGENT_RUNTIME_ARN",
  "RENDER_AGENT_RUNTIME_ARN",
];

type Resources = Record<string, { Type: string; Properties?: any; DependsOn?: string | string[] }>;

function synth(env: EnvName): Resources {
  const app = new cdk.App({ context: { env, projectName: PROJECT } });
  const envConfig = resolveEnvConfig(app);
  const stackName = env === "dev" ? `${PROJECT}-infra` : `${PROJECT}-infra-${env}`;
  const stack = new AgentInfraStack(app, stackName, {
    projectName: PROJECT,
    envConfig,
    env: { account: envConfig.account, region: envConfig.region },
  });
  return (Template.fromStack(stack).toJSON() as any).Resources as Resources;
}

/** Logical ids referenced by a CloudFormation value (Ref / GetAtt / Sub). */
function collectRefs(node: unknown, out: Set<string>): void {
  if (Array.isArray(node)) {
    for (const x of node) collectRefs(x, out);
    return;
  }
  if (!node || typeof node !== "object") return;
  for (const [k, v] of Object.entries(node as Record<string, unknown>)) {
    if (k === "Ref" && typeof v === "string") {
      out.add(v);
    } else if (k === "Fn::GetAtt") {
      out.add(Array.isArray(v) ? String(v[0]) : String(v).split(".")[0]);
    } else if (k === "Fn::Sub") {
      const tmpl = Array.isArray(v) ? String(v[0]) : String(v);
      for (const m of tmpl.matchAll(/\$\{([^}!][^}]*)\}/g)) out.add(m[1].split(".")[0]);
      if (Array.isArray(v)) collectRefs(v[1], out);
    } else {
      collectRefs(v, out);
    }
  }
}

function directDeps(resources: Resources): Map<string, Set<string>> {
  const deps = new Map<string, Set<string>>();
  for (const [id, r] of Object.entries(resources)) {
    const s = new Set<string>();
    const { DependsOn, ...rest } = r;
    collectRefs(rest, s);
    for (const d of DependsOn === undefined ? [] : Array.isArray(DependsOn) ? DependsOn : [DependsOn]) s.add(d);
    deps.set(id, new Set([...s].filter((x) => x in resources && x !== id)));
  }
  return deps;
}

/** For `from`, every other runtime it reaches, with one witness path. */
function reachableRuntimes(from: string, deps: Map<string, Set<string>>, runtimes: Set<string>) {
  const parent = new Map<string, string | null>([[from, null]]);
  const queue = [from];
  while (queue.length) {
    const n = queue.shift()!;
    for (const m of deps.get(n) ?? []) {
      if (!parent.has(m)) {
        parent.set(m, n);
        queue.push(m);
      }
    }
  }
  const found: Array<{ to: string; path: string[] }> = [];
  for (const rt of runtimes) {
    if (rt === from || !parent.has(rt)) continue;
    const path = [rt];
    while (parent.get(path[path.length - 1])) path.push(parent.get(path[path.length - 1])!);
    found.push({ to: rt, path: path.reverse() });
  }
  return found;
}

/** Longest runtime → runtime chain (in runtimes), memoised DFS over the reduced graph. */
function criticalPath(rtGraph: Map<string, string[]>): string[] {
  const memo = new Map<string, string[]>();
  const longest = (n: string): string[] => {
    if (memo.has(n)) return memo.get(n)!;
    let best: string[] = [];
    for (const m of rtGraph.get(n) ?? []) {
      const p = longest(m);
      if (p.length > best.length) best = p;
    }
    const res = [n, ...best];
    memo.set(n, res);
    return res;
  };
  let best: string[] = [];
  for (const n of rtGraph.keys()) {
    const p = longest(n);
    if (p.length > best.length) best = p;
  }
  return best;
}

let failures = 0;
for (const env of ["staging", "sandbox", "prod"] as const) {
  const resources = synth(env);
  const deps = directDeps(resources);
  const runtimes = new Set(
    Object.entries(resources).filter(([, r]) => r.Type === RUNTIME_TYPE).map(([id]) => id),
  );
  assert.ok(runtimes.size >= 5, `${env}: expected >= 5 runtimes, found ${runtimes.size}`);

  // 1. No runtime → runtime edge.
  const rtGraph = new Map<string, string[]>();
  const edges: string[] = [];
  for (const rt of runtimes) {
    const reach = reachableRuntimes(rt, deps, runtimes);
    rtGraph.set(rt, reach.map((r) => r.to));
    for (const r of reach) edges.push(`${rt} waits on ${r.to} via ${r.path.join(" -> ")}`);
  }
  const chain = criticalPath(rtGraph);
  console.log(`${env}: ${runtimes.size} runtimes, critical path = ${chain.length} (${chain.join(" -> ")})`);
  if (edges.length) {
    failures++;
    console.error(`${env}: runtime -> runtime dependencies found:\n  ${edges.join("\n  ")}`);
  }

  // 2. No ARN env var on any runtime.
  for (const rt of runtimes) {
    const envVars = resources[rt].Properties?.EnvironmentVariables ?? {};
    for (const v of [...ARN_VARS, "AGENT_RUNTIME_ARN"]) {
      assert.ok(!(v in envVars), `${env}: ${rt} still carries ${v} in EnvironmentVariables`);
    }
  }

  // 3. The four SSM parameters, and the path in the secret.
  const path = `/${PROJECT}-${env}/runtime-arns`;
  const params = Object.values(resources)
    .filter((r) => r.Type === "AWS::SSM::Parameter")
    .map((r) => r.Properties ?? {});
  for (const v of ARN_VARS) {
    const p = params.find((pp) => pp.Name === `${path}/${v}`);
    assert.ok(p, `${env}: missing SSM parameter ${path}/${v}`);
    const refs = new Set<string>();
    collectRefs(p!.Value, refs);
    assert.ok([...refs].some((r) => runtimes.has(r)), `${env}: ${path}/${v} must hold a runtime ARN`);
  }
  const secret = Object.values(resources).find(
    (r) => r.Type === "AWS::SecretsManager::Secret" &&
      JSON.stringify(r.Properties?.SecretString ?? "").includes("RUNTIME_ARN_PARAM_PATH"),
  );
  assert.ok(secret, `${env}: RuntimeConfigSecret must carry RUNTIME_ARN_PARAM_PATH`);
  assert.ok(JSON.stringify(secret!.Properties.SecretString).includes(path), `${env}: secret path must be ${path}`);

  // 4. Deploy-speed edges (docs/ci-speed-proposal.md P1, P2). The ECS task definitions must not wait on any
  //    runtime (the proxy reads AGENT_RUNTIME_ARN & co. from SSM), and no runtime may wait on the ingestion
  //    nested stack, which updates on every ingestion change.
  const mainParam = params.find((pp) => pp.Name === `${path}/AGENT_RUNTIME_ARN`);
  assert.ok(mainParam, `${env}: missing SSM parameter ${path}/AGENT_RUNTIME_ARN (read by the UI proxy)`);
  // The SERVICE too, not just its task definition: CDK makes the service DependsOn its task role's
  // DefaultPolicy, so an InvokeAgentRuntime grant on `agentRuntimeArn` (a GetAtt) there made the proxy
  // service wait for every runtime update — ~3.5 min of serial deploy time (staging, 2026-10-20).
  for (const [id, r] of Object.entries(resources)) {
    if (r.Type !== "AWS::ECS::TaskDefinition" && r.Type !== "AWS::ECS::Service") continue;
    const reach = reachableRuntimes(id, deps, runtimes);
    assert.equal(reach.length, 0, `${env}: ${r.Type} ${id} waits on a runtime: ${
      reach.map((x) => x.path.join(" -> ")).join("; ")}`);
  }
  // 5. The agent's project tools reach the proxy through CloudFront (agent/src/project_search/registry_client.py):
  //    the https origin is an SSM parameter under the same path, read lazily, never runtime env.
  const originParam = params.find((pp) => pp.Name === `${path}/UI_PUBLIC_ORIGIN`);
  assert.ok(originParam, `${env}: missing SSM parameter ${path}/UI_PUBLIC_ORIGIN (read by the agent's project tools)`);
  assert.ok(JSON.stringify(originParam!.Value).includes("https://"), `${env}: UI_PUBLIC_ORIGIN must be an https origin`);
  const ingestionStacks = new Set(Object.entries(resources)
    .filter(([id, r]) => r.Type === "AWS::CloudFormation::Stack" && id.startsWith("IngestionStack")).map(([id]) => id));
  for (const rt of runtimes) {
    const reach = reachableRuntimes(rt, deps, ingestionStacks);
    assert.equal(reach.length, 0, `${env}: runtime ${rt} waits on the IngestionStack: ${
      reach.map((x) => x.path.join(" -> ")).join("; ")}`);
  }
}

if (failures) {
  console.error(`runtime-parallel: FAILED in ${failures} env(s)`);
  process.exit(1);
}
console.log("runtime-parallel: ok — no runtime waits on another runtime");
