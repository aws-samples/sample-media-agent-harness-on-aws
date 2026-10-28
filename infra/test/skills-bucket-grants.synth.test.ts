/**
 * Synth assertions for who may write the skills/artifacts bucket (asset-lineage Phase 3, least
 * privilege). Run: `npm run test:skills-bucket-grants` (node:assert, no framework — like the other
 * infra synth tests).
 *
 * The bucket holds session artifacts (`artifacts/*`), project media (`projects/*`), Lab drafts and
 * published assets (`users/*`), the skill packages (`skills/*`) and plan outputs (`plans/*`). Four
 * principals used to hold read+write on ALL of it; this pins what each one may do now:
 *
 *  - the code-interpreter sandbox's OWN role (model-authored Python, shared by every actor): read
 *    `tools/ffmpeg/*` and NOTHING else (B2) — no artifacts, no projects, no writes. Model code that
 *    deletes the injected credentials file falls back to a role with no user data.
 *  - `SandboxScopedS3Role` (Phase 3 (b), every env since B2): trusted ONLY by the Main and Task runtime roles
 *    with `sts:TagSession` and required `project_id`/`session_id` tags; every resource resolves from
 *    `${aws:PrincipalTag/...}`; no DELETE; its ARN is in the RuntimeConfigSecret, not runtime env.
 *  - the aws-mcp proxy runtime: no skills-bucket access at all (it never touches S3).
 *  - the Planning runtime (no tools, one output): bucket-wide read, PUT `plans/*`.
 *  - the Main and Task Agent runtimes: still bucket-wide read+write, DELIBERATELY — they host the
 *    Lab service and write every prefix. Asserted so a narrowing there is a conscious edit.
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
    projectName: PROJECT,
    envConfig,
    env: { account: envConfig.account, region: envConfig.region },
  });
  return Template.fromStack(stack);
}

type Statement = { Action: string | string[]; Resource?: unknown; Effect?: string };

/** Every statement of every IAM policy attached to the role whose logical id starts with `prefix`. */
function statementsFor(t: Template, prefix: string): Statement[] {
  const roleIds = Object.keys(t.findResources("AWS::IAM::Role")).filter((id) => id.startsWith(prefix));
  assert.ok(roleIds.length > 0, `role ${prefix}* exists`);
  const out: Statement[] = [];
  for (const r of Object.values(t.findResources("AWS::IAM::Policy")) as any[]) {
    const roles = JSON.stringify(r.Properties.Roles ?? []);
    if (roleIds.some((id) => roles.includes(`"${id}"`))) out.push(...r.Properties.PolicyDocument.Statement);
  }
  return out;
}

const actions = (s: Statement): string[] => (Array.isArray(s.Action) ? s.Action : [s.Action]);

/** Flatten a CloudFormation resource value to a comparable string (literal ARN or Fn::Join parts). */
function flat(x: unknown): string {
  if (typeof x === "string") return x;
  if (Array.isArray(x)) return x.map(flat).join("");
  if (x && typeof x === "object") return Object.values(x as Record<string, unknown>).map(flat).join("");
  return "";
}
const resources = (s: Statement): string[] => {
  const r = s.Resource;
  return (Array.isArray(r) ? r : [r]).map(flat);
};
/**
 * The skills bucket is a `GetAtt` where this stack creates it and a literal ARN where it is imported
 * by name (staging/prod import `…-skills-store-<account>-<region>`), so both shapes count.
 */
const namesSkillsBucket = (r: string): boolean => r.includes("SkillsBucket") || r.includes("skills-store");
/** `…/artifacts/*` — an object-level pattern under one prefix, not the whole bucket. */
const isObjectPattern = (r: string): boolean => /\/[A-Za-z0-9_.-]+\/\*$/.test(r) || r.endsWith(SESSION_OUTPUTS);
/** The session-output sub-tree of the project layout (asset-lineage Phase 3 (a)). */
const SESSION_OUTPUTS = "/projects/*/sessions/*/output/*";
/** `…<bucket>/*` — every object in the bucket (literal ARN or `<GetAtt>Arn` + "/*"). */
const isBucketWideObjects = (r: string): boolean =>
  /(Arn|skills-store[A-Za-z0-9-]*)\/\*$/.test(r);

const skillsBucketStatements = (ss: Statement[]): Statement[] =>
  ss.filter((s) => resources(s).some(namesSkillsBucket));
const isWrite = (a: string): boolean => /^s3:(Put|Delete|Abort|Restore|Replicate|Bypass)/.test(a);

for (const env of ["staging", "prod", "sandbox"] as EnvName[]) {
  const t = build(env);

  // ---- code interpreter's own role: the staged ffmpeg only (B2) -----------------------------------
  {
    const ss = skillsBucketStatements(statementsFor(t, "CodeInterpreterServiceRole"));
    const objectRefs = ss.flatMap(resources).filter((r) => r.includes("/"));
    assert.ok(objectRefs.length > 0, `${env}: the sandbox can still read the staged ffmpeg`);
    for (const r of objectRefs) {
      assert.ok(r.endsWith("/tools/ffmpeg/*"), `${env}: the sandbox role's only object grant is tools/ffmpeg/*, got ${r}`);
    }
    for (const s of ss) {
      for (const a of actions(s)) assert.ok(!isWrite(a), `${env}: the sandbox role writes nothing (${a})`);
    }
    // No other bucket either (uploads, lab artifacts, ...): every S3 statement names the skills bucket.
    for (const st of statementsFor(t, "CodeInterpreterServiceRole")) {
      if (actions(st).some((a) => a.startsWith("s3:"))) {
        assert.ok(resources(st).every(namesSkillsBucket), `${env}: sandbox S3 grant outside the ffmpeg prefix: ${resources(st)}`);
      }
    }
  }

  // ---- SandboxScopedS3Role: every env (B2), ABAC, assumable only by the two orchestrator runtimes ----
  {
    const roles = Object.entries(t.findResources("AWS::IAM::Role")).filter(([id]) => id.startsWith("SandboxScopedS3Role"));
    {
      assert.equal(roles.length, 1, `${env}: one scoped sandbox role`);
      assert.ok(JSON.stringify(t.toJSON()).includes("SANDBOX_SCOPED_ROLE_ARN"), `${env}: scoped role ARN in RuntimeConfigSecret`);
      const role = roles[0][1].Properties;
      assert.equal(role.RoleName, `${PROJECT}-sandbox-scoped-s3`);
      assert.equal(role.MaxSessionDuration, 3600);
      const trust = role.AssumeRolePolicyDocument.Statement as any[];
      assert.equal(trust.length, 2, "one trust statement per orchestrator runtime");
      const principals = trust.map((st) => flat(st.Principal.AWS));
      assert.ok(principals.some((p) => p.startsWith("AgentRuntimeExecutionRole")), `main runtime trusted: ${principals}`);
      assert.ok(principals.some((p) => p.startsWith("TaskAgentRuntimeExecutionRole")), `task runtime trusted: ${principals}`);
      for (const st of trust) {
        assert.deepEqual([...actions(st)].sort(), ["sts:AssumeRole", "sts:TagSession"], "assume + TagSession");
        assert.deepEqual(st.Condition.Null, { "aws:RequestTag/project_id": "false", "aws:RequestTag/session_id": "false" });
        assert.deepEqual(st.Condition["ForAllValues:StringEquals"], { "aws:TagKeys": ["project_id", "session_id"] });
      }
      const ss = statementsFor(t, "SandboxScopedS3Role");
      for (const st of ss) {
        for (const a of actions(st)) assert.ok(!a.startsWith("s3:Delete"), `scoped role never deletes (${a})`);
        for (const r of resources(st).filter((r) => r.includes("/"))) {
          const objectPart = r.replace(/^.*skills-store[A-Za-z0-9-]*/, "");
          // `/tools/ffmpeg/*`: the staged ffmpeg build, which every cell downloads with these credentials (READ only,
          // checked just below). Everything else resolves from the session's tags.
          assert.ok(objectPart === "" || objectPart === "/artifacts/*" || objectPart === "/tools/ffmpeg/*"
            || objectPart.includes("${aws:PrincipalTag/project_id}")
            || objectPart.includes("${aws:PrincipalTag/session_id}"), `scoped path resolves from PrincipalTag: ${r}`);
        }
      }
      const ffmpegGrants = ss.filter((st) => resources(st).some((r) => r.endsWith("/tools/ffmpeg/*")));
      assert.ok(ffmpegGrants.length > 0, "the scoped role can read the staged ffmpeg build");
      for (const st of ffmpegGrants) {
        assert.ok([...actions(st)].every((a) => !isWrite(a)), `tools/ffmpeg is read-only for the sandbox: ${[...actions(st)]}`);
      }
      const writes = ss.filter((st) => actions(st).some(isWrite)).flatMap(resources);
      assert.ok(writes.length > 0 && writes.every((r) => r.includes("${aws:PrincipalTag/session_id}")),
        `every write is under the tagged session: ${writes}`);
      assert.ok(writes.some((r) => r.endsWith("/projects/${aws:PrincipalTag/project_id}/sessions/${aws:PrincipalTag/session_id}/*")));
      // The orchestrators may assume it; the sandbox may not.
      for (const r of ["AgentRuntimeExecutionRole", "TaskAgentRuntimeExecutionRole"]) {
        assert.ok(statementsFor(t, r).some((st) => actions(st).includes("sts:AssumeRole")
          && resources(st).some((x) => x.endsWith(`:role/${PROJECT}-sandbox-scoped-s3`))), `${r} may assume it`);
      }
      assert.ok(!statementsFor(t, "CodeInterpreterServiceRole").some((st) => actions(st).some((a) => a.startsWith("sts:"))),
        "the sandbox can never assume a role");
    }
  }

  // ---- aws-mcp proxy runtime: no skills-bucket access ------------------------------------------
  {
    const ss = skillsBucketStatements(statementsFor(t, "AwsMcpRuntimeExecutionRole"));
    assert.deepEqual(ss, [], `${env}: the aws-mcp proxy runtime has no skills-bucket grant`);
  }

  // ---- planning: read anywhere, put only plans/* ----------------------------------------------
  for (const role of ["PlanningAgentRuntimeExecutionRole"]) {
    const ss = skillsBucketStatements(statementsFor(t, role));
    const writes = ss.filter((s) => actions(s).some(isWrite));
    assert.ok(writes.length > 0, `${env}: ${role} can write its plan output`);
    for (const s of writes) {
      for (const r of resources(s)) {
        assert.ok(r.endsWith("/plans/*"), `${env}: ${role} writes only plans/*, got ${r}`);
      }
      for (const a of actions(s)) {
        assert.ok(!a.startsWith("s3:Delete"), `${env}: ${role} must not delete objects (${a})`);
      }
    }
    assert.ok(
      ss.some((s) => actions(s).includes("s3:GetObject*")),
      `${env}: ${role} keeps bucket-wide read (skills + config loads)`,
    );
  }

  // ---- main + task agent: bucket-wide read+write, deliberately ---------------------------------
  for (const role of ["AgentRuntimeExecutionRole", "TaskAgentRuntimeExecutionRole"]) {
    const ss = skillsBucketStatements(statementsFor(t, role));
    assert.ok(
      ss.some((s) => actions(s).includes("s3:PutObject") && resources(s).some(isBucketWideObjects)),
      `${env}: ${role} keeps bucket-wide write (hosts the Lab service — see the grant's comment)`,
    );
  }

  console.log(`${env}: skills-bucket grants — sandbox (ffmpeg only)/aws-mcp/planning narrowed, orchestrators intact`);

  // ---- M2C media-gateway credentials: the chainer and the poller call its shared get_job_status ---
  // (declared outputs of SFN-polled M2C jobs whose execution output names no file). The secret
  // lives outside /<project>/gateway/*, so without this grant the status call is AccessDenied and
  // the node keeps storing a bare {uuid} (staging session b8a59e30).
  {
    const secretArn = resolveEnvConfig(new cdk.App({ context: { env, projectName: PROJECT } })).media?.credentialsSecretArn;
    if (secretArn) {
      for (const role of ["PlanChainerLambdaServiceRole", "JobPollingWorkerServiceRole"]) {
        assert.ok(
          statementsFor(t, role).some((s) => actions(s).includes("secretsmanager:GetSecretValue")
            && resources(s).some((r) => r === secretArn)),
          `${env}: ${role} may read the M2C media-gateway credentials`,
        );
      }
      console.log(`${env}: chainer + poller read the M2C media-gateway credentials`);
    }
  }
}

console.log("skills-bucket-grants.synth: all assertions passed");
