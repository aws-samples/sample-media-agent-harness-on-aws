/**
 * Synth assertions for the platform's Bedrock guardrail (docs/superpowers/specs/2026-11-09-bedrock-guardrails-design.md).
 * Run: `npm run test:guardrail-synth` (node:assert, no framework).
 *
 * Where `defaultGuardrail` (staging, prod): one AWS::Bedrock::Guardrail, light (PROMPT_ATTACK HIGH on input, the
 * other content categories LOW), a published AWS::Bedrock::GuardrailVersion of it, and the GUARDRAIL_DEFAULT
 * parameter under the runtime-ARN path. Nowhere: a CloudFormation-managed GUARDRAIL_SELECTED (the admin's pick must
 * survive deploys). Everywhere: the main and task runtime roles may bedrock:ApplyGuardrail in this account, the
 * planning and render roles may not, and the proxy may list guardrails and write only GUARDRAIL_SELECTED.
 */
import * as assert from "node:assert/strict";
import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { AgentInfraStack } from "../lib/agent-infra-stack";
import { resolveEnvConfig, EnvName } from "../lib/env-config";

const PROJECT = "mediaworks-harness";

function build(env: EnvName): Template {
  const app = new cdk.App({ context: { env, projectName: PROJECT } });
  const envConfig = resolveEnvConfig(app);
  const stack = new AgentInfraStack(app, `${PROJECT}-infra-${env}`, {
    projectName: PROJECT, envConfig, env: { account: envConfig.account, region: envConfig.region },
  });
  return Template.fromStack(stack);
}

const json = (x: unknown) => JSON.stringify(x);

/** Every IAM statement attached to a role, keyed by the role's logical id (inline, AWS::IAM::Policy, ManagedPolicy). */
function statementsByRole(t: Template): Map<string, any[]> {
  const out = new Map<string, any[]>();
  const add = (role: string, s: any) => out.set(role, [...(out.get(role) ?? []), s]);
  for (const [id, r] of Object.entries(t.findResources("AWS::IAM::Role")) as [string, any][]) {
    for (const p of r.Properties?.Policies ?? []) for (const s of p.PolicyDocument?.Statement ?? []) add(id, s);
  }
  // AWS::IAM::ManagedPolicy too: CDK moves a large role's statements into "OverflowPolicy" managed policies.
  for (const r of [...Object.values(t.findResources("AWS::IAM::Policy")), ...Object.values(t.findResources("AWS::IAM::ManagedPolicy"))] as any[]) {
    for (const role of r.Properties?.Roles ?? []) {
      const id = role?.Ref;
      if (typeof id === "string") for (const s of r.Properties?.PolicyDocument?.Statement ?? []) add(id, s);
    }
  }
  return out;
}

const actionsOf = (s: any): string[] => ([] as string[]).concat(s.Action ?? []);

function runtimeRole(t: Template, prefix: string): string {
  const [id, rt] = Object.entries(t.findResources("AWS::BedrockAgentCore::Runtime")).find(([rid]) => rid.startsWith(prefix))!;
  const arn = (rt as any).Properties?.RoleArn;
  const ref = arn?.["Fn::GetAtt"]?.[0];
  assert.ok(typeof ref === "string", `${id}: role ref`);
  return ref;
}

let failures = 0;
for (const env of ["staging", "prod", "sandbox"] as const) {
  try {
    const t = build(env);
    const guardrails = Object.values(t.findResources("AWS::Bedrock::Guardrail")) as any[];
    const versions = Object.values(t.findResources("AWS::Bedrock::GuardrailVersion"));
    const params = (Object.values(t.findResources("AWS::SSM::Parameter")) as any[]).map((p) => p.Properties);
    const path = `/${PROJECT}-${env}/runtime-arns`;
    if (env === "sandbox") {
      assert.equal(guardrails.length, 0, "sandbox creates no guardrail");
      assert.ok(!params.some((p) => p.Name === `${path}/GUARDRAIL_DEFAULT`), "sandbox has no default");
    } else {
      assert.equal(guardrails.length, 1, `${env}: one default guardrail`);
      const filters = guardrails[0].Properties.ContentPolicyConfig.FiltersConfig as any[];
      const byType = Object.fromEntries(filters.map((f) => [f.Type, f]));
      assert.deepEqual(byType.PROMPT_ATTACK, { Type: "PROMPT_ATTACK", InputStrength: "HIGH", OutputStrength: "NONE" });
      for (const type of ["SEXUAL", "VIOLENCE", "HATE", "INSULTS", "MISCONDUCT"]) {
        assert.deepEqual(byType[type], { Type: type, InputStrength: "LOW", OutputStrength: "LOW" }, `${env}: ${type} is light`);
      }
      assert.equal(versions.length, 1, `${env}: one published version`);
      assert.ok(params.some((p) => p.Name === `${path}/GUARDRAIL_DEFAULT`), `${env}: GUARDRAIL_DEFAULT parameter`);
    }
    assert.ok(!params.some((p) => p.Name === `${path}/GUARDRAIL_SELECTED`), `${env}: the admin's pick is never CFN-managed`);

    const roles = statementsByRole(t);
    const applies = (role: string) => (roles.get(role) ?? []).some((s) => actionsOf(s).includes("bedrock:ApplyGuardrail"));
    assert.ok(applies(runtimeRole(t, "AgentRuntime")), `${env}: main may ApplyGuardrail`);
    assert.ok(applies(runtimeRole(t, "TaskAgentRuntime")), `${env}: task may ApplyGuardrail`);
    assert.ok(!applies(runtimeRole(t, "PlanningAgentRuntime")), `${env}: planning never applies one`);
    assert.ok(!applies(runtimeRole(t, "RenderAgentRuntime")), `${env}: render never applies one`);

    const proxy = [...roles.entries()].find(([id]) => id.startsWith("UiProxyTaskRole"))![1];
    assert.ok(proxy.some((s) => actionsOf(s).includes("bedrock:ListGuardrails")), `${env}: proxy lists guardrails`);
    const puts = proxy.filter((s) => actionsOf(s).includes("ssm:PutParameter"));
    assert.equal(puts.length, 1, `${env}: one PutParameter grant`);
    assert.ok(json(puts[0].Resource).includes("GUARDRAIL_SELECTED") && !json(puts[0].Resource).includes("*"),
      `${env}: the proxy writes only GUARDRAIL_SELECTED`);
    console.log(`guardrail-synth ${env}: ok`);
  } catch (e) {
    failures++;
    console.error(`guardrail-synth ${env}: FAILED`, e);
  }
}
if (failures) process.exit(1);
