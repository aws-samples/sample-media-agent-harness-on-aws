/**
 * Synth assertions for the security fixes that live in the template, in EVERY deployable env
 * (staging, prod, sandbox, customer). Run: `npm run test:security-synth` (node:assert, no framework).
 *
 *  #2 ALB origin lock (interim; the VPC-origin plan is in AGENTS.md "ALB origin lock"):
 *     - the UiProxy ALB security group has NO 0.0.0.0/0 (or ::/0) ingress, only CloudFront's
 *       origin-facing managed prefix list, resolved at deploy time;
 *     - CloudFront sends `X-Origin-Verify` as a Secrets Manager dynamic reference (never plaintext);
 *     - the proxy AND the Capability API task get the same secret as an ECS secret (not env);
 *     - both services wait for the distribution (no enforcing task before every edge sends the header).
 *  #3 `/dispatch` has no CloudFront behavior.
 *  #4 (B2) the code interpreter's own role reads only `tools/ffmpeg/*`; the scoped role exists.
 */
import * as assert from "node:assert/strict";
import * as cdk from "aws-cdk-lib";
import { buildApp } from "../lib/build-app";

process.env.CDK_DEFAULT_ACCOUNT = process.env.CDK_DEFAULT_ACCOUNT || "111111111111";
process.env.CDK_DEFAULT_REGION = process.env.CDK_DEFAULT_REGION || "us-west-2";

const PROJECT = "mediaworks-harness";
const res = (t: any, type: string): [string, any][] => Object.entries<any>(t.Resources).filter(([, r]) => r.Type === type);

for (const env of ["staging", "prod", "sandbox", "customer"]) {
  const app = new cdk.App({ context: { env, projectName: PROJECT } });
  const { harness } = buildApp(app);
  const t: any = app.synth().getStackArtifact(harness.artifactId).template;
  const json = JSON.stringify(t);

  // ---- #2: the ALB security group admits CloudFront only --------------------------------------
  const albs = res(t, "AWS::ElasticLoadBalancingV2::LoadBalancer");
  assert.equal(albs.length, 1, `${env}: one ALB`);
  const albSgRefs = JSON.stringify(albs[0][1].Properties.SecurityGroups);
  const albSgs = res(t, "AWS::EC2::SecurityGroup").filter(([id]) => albSgRefs.includes(id));
  assert.equal(albSgs.length, 1, `${env}: the ALB's security group is in this template`);
  const [albSgId, albSg] = albSgs[0];
  const inline = albSg.Properties.SecurityGroupIngress ?? [];
  const separate = res(t, "AWS::EC2::SecurityGroupIngress")
    .filter(([, r]) => JSON.stringify(r.Properties.GroupId).includes(albSgId))
    .map(([, r]) => r.Properties);
  const ingress = [...inline, ...separate];
  assert.ok(ingress.length > 0, `${env}: the ALB has an ingress rule`);
  for (const rule of ingress) {
    assert.ok(!rule.CidrIp && !rule.CidrIpv6, `${env}: no CIDR ingress on the ALB (got ${JSON.stringify(rule)})`);
    assert.ok(rule.SourcePrefixListId, `${env}: ALB ingress is from a prefix list only (got ${JSON.stringify(rule)})`);
    assert.equal(rule.FromPort, 80);
    assert.equal(rule.ToPort, 80);
  }
  const plLookup = res(t, "Custom::AWS").filter(([id]) => id.startsWith("CloudFrontOriginFacingPrefixList"));
  assert.equal(plLookup.length, 1, `${env}: the prefix list id is resolved at deploy time`);
  assert.match(JSON.stringify(plLookup[0][1].Properties), /com\.amazonaws\.global\.cloudfront\.origin-facing/);
  assert.ok(JSON.stringify(ingress).includes(plLookup[0][0]), `${env}: ingress names the looked-up prefix list`);

  // ---- #2: CloudFront sends the origin-verify header as a dynamic reference --------------------
  const [[, dist]] = res(t, "AWS::CloudFront::Distribution").filter(([id]) => id.startsWith("UiDistribution"));
  const albOrigins = (dist.Properties.DistributionConfig.Origins as any[]).filter((o) => o.CustomOriginConfig);
  assert.ok(albOrigins.length >= 1, `${env}: the ALB is a custom origin`);
  const secretIds = res(t, "AWS::SecretsManager::Secret").filter(([id]) => id.startsWith("UiOriginVerifySecret"));
  assert.equal(secretIds.length, 1, `${env}: one origin-verify secret`);
  const secretId = secretIds[0][0];
  for (const o of albOrigins) {
    const h = (o.OriginCustomHeaders as any[] ?? []).find((x) => x.HeaderName === "X-Origin-Verify");
    assert.ok(h, `${env}: ALB origin sends X-Origin-Verify`);
    const v = JSON.stringify(h.HeaderValue);
    assert.ok(v.includes("{{resolve:secretsmanager:") && v.includes(secretId),
      `${env}: header value is a Secrets Manager dynamic reference, got ${v}`);
  }

  // ---- #2: both services behind the ALB get the secret (as a secret) and wait for CloudFront ---
  const taskDefs = res(t, "AWS::ECS::TaskDefinition");
  const withSecret = taskDefs.filter(([, td]) => (td.Properties.ContainerDefinitions as any[]).some((c) =>
    (c.Secrets ?? []).some((s: any) => s.Name === "ORIGIN_VERIFY_SECRET" && JSON.stringify(s.ValueFrom).includes(secretId))));
  assert.equal(withSecret.length, 2, `${env}: proxy + Capability API task defs carry ORIGIN_VERIFY_SECRET`);
  for (const [, td] of taskDefs) {
    for (const c of td.Properties.ContainerDefinitions as any[]) {
      assert.ok(!(c.Environment ?? []).some((e: any) => e.Name === "ORIGIN_VERIFY_SECRET"), `${env}: never plaintext env`);
    }
  }
  const distId = res(t, "AWS::CloudFront::Distribution").find(([id]) => id.startsWith("UiDistribution"))![0];
  const services = res(t, "AWS::ECS::Service");
  assert.equal(services.length, 2, `${env}: two ECS services`);
  for (const [id, svc] of services) {
    const deps = ([] as string[]).concat(svc.DependsOn ?? []);
    assert.ok(deps.includes(distId), `${env}: ${id} depends on the distribution (cutover ordering)`);
  }

  // ---- #3: no /dispatch behavior ---------------------------------------------------------------
  const patterns = (dist.Properties.DistributionConfig.CacheBehaviors as any[]).map((b) => b.PathPattern);
  assert.ok(!patterns.includes("/dispatch") && !patterns.some((p: string) => p.startsWith("/dispatch")),
    `${env}: no /dispatch CloudFront behavior`);
  assert.ok(patterns.includes("/control-plane/*") && patterns.includes("/api/*"), `${env}: API behaviors intact`);

  // ---- #4: the sandbox's own role reads only the staged ffmpeg; the scoped role exists ----------
  const ciRoleIds = res(t, "AWS::IAM::Role").filter(([id]) => id.startsWith("CodeInterpreterServiceRole")).map(([id]) => id);
  assert.ok(ciRoleIds.length > 0, `${env}: code interpreter role`);
  for (const [, pol] of res(t, "AWS::IAM::Policy")) {
    if (!ciRoleIds.some((id) => JSON.stringify(pol.Properties.Roles).includes(id))) continue;
    for (const st of pol.Properties.PolicyDocument.Statement as any[]) {
      const acts = ([] as string[]).concat(st.Action);
      if (!acts.some((a) => a.startsWith("s3:"))) continue;
      for (const r of ([] as any[]).concat(st.Resource)) {
        const flat = JSON.stringify(r);
        if (flat.includes("/")) assert.match(flat, /tools\/ffmpeg\/\*/, `${env}: sandbox role object grant ${flat}`);
      }
      assert.ok(acts.every((a) => !/^s3:(Put|Delete|Abort)/.test(a)), `${env}: sandbox role writes nothing: ${acts}`);
    }
  }
  assert.equal(res(t, "AWS::IAM::Role").filter(([id]) => id.startsWith("SandboxScopedS3Role")).length, 1,
    `${env}: the scoped sandbox role exists (on by default)`);
  assert.ok(json.includes("SANDBOX_SCOPED_ROLE_ARN"), `${env}: its ARN reaches the runtimes`);

  // ---- local dev login: loopback callbacks on staging only, never prod or a customer install ----------------------
  const clients = res(t, "AWS::Cognito::UserPoolClient").filter(([, r]) => r.Properties.CallbackURLs);
  assert.ok(clients.length >= 1, `${env}: the UI app client has callback URLs`);
  const urls = clients.flatMap(([, r]) => [...r.Properties.CallbackURLs, ...(r.Properties.LogoutURLs || [])].map((u: any) => JSON.stringify(u)));
  const loopback = urls.filter((u) => /localhost|127\.0\.0\.1/.test(u));
  if (env === "staging") {
    assert.ok(loopback.some((u) => u.includes("http://127.0.0.1:3000/auth/callback")), `${env}: local dev login is on`);
    assert.ok(loopback.every((u) => /^"http:\/\/(127\.0\.0\.1|localhost):3000\/(auth\/callback)?"$/.test(u)), `${env}: loopback port 3000 only: ${loopback}`);
  } else if (env === "prod" || env === "customer") {
    assert.deepEqual(loopback, [], `${env}: no loopback callback or logout URL`);
  }

  console.log(`${env}: origin lock + /dispatch gone + sandbox role B2 + login callbacks — ok`);
}
console.log("security-origin-lock.synth: all assertions passed");
