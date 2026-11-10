// Runtime ARNs for the proxy, read from SSM at startup (docs/ci-speed-proposal.md P1).
//
// The deploy writes one SSM parameter per AgentCore runtime under RUNTIME_ARN_PARAM_PATH
// (`<path>/AGENT_RUNTIME_ARN`, `<path>/TASK_AGENT_RUNTIME_ARN`, ...). The ECS task definition carries
// only that path: an env var holding a runtime's GetAtt ARN made CloudFormation wait for the runtime
// updates (~4 min) before it could even register the new task definition.
//
// `hydrateRuntimeArns` fills any of RUNTIME_ARN_NAMES that the env does not already set (env wins, so
// local dev and tests are unchanged), then returns. On a brand-new environment the parameters are written
// after the runtimes exist, so it retries until all are present or the wait runs out; the app then starts
// with whatever it has (buildApp throws if AGENT_RUNTIME_ARN is still missing, and ECS restarts the task).

import https from "https";
import { HttpRequest } from "@smithy/protocol-http";
import { SignatureV4 } from "@smithy/signature-v4";
import { Sha256 } from "@aws-crypto/sha256-js";
import { defaultProvider } from "@aws-sdk/credential-provider-node";

export const RUNTIME_ARN_NAMES = ["AGENT_RUNTIME_ARN", "TASK_AGENT_RUNTIME_ARN", "RENDER_AGENT_RUNTIME_ARN"] as const;

/** `{last path segment: value}` for every parameter directly under `path`. */
export type FetchByPath = (path: string) => Promise<Record<string, string>>;

/** One signed SSM JSON-protocol call (`target` = `GetParametersByPath`, `GetParameters`, `PutParameter`, ...). */
export type SsmCall = (target: string, body: Record<string, unknown>) => Promise<any>;

export function ssmCall(region: string): SsmCall {
  const signer = new SignatureV4({ service: "ssm", region, credentials: defaultProvider(), sha256: Sha256 });
  const hostname = `ssm.${region}.amazonaws.com`;
  return async (target, body) => {
    const request = new HttpRequest({
      method: "POST", protocol: "https:", hostname, path: "/",
      headers: {
        host: hostname,
        "content-type": "application/x-amz-json-1.1",
        "x-amz-target": `AmazonSSM.${target}`,
      },
      body: JSON.stringify(body),
    });
    const signed = await signer.sign(request);
    return new Promise((resolve, reject) => {
      const req = https.request(`https://${hostname}/`, { method: "POST", headers: signed.headers as any }, (res) => {
        const chunks: Buffer[] = [];
        res.on("data", (c) => chunks.push(c));
        res.on("end", () => {
          const text = Buffer.concat(chunks).toString("utf8");
          if ((res.statusCode ?? 500) >= 300) {
            reject(new Error(`SSM ${target} ${res.statusCode}: ${text.slice(0, 200)}`));
            return;
          }
          try { resolve(JSON.parse(text)); } catch (e) { reject(e); }
        });
      });
      req.on("error", reject);
      req.end(signed.body);
    });
  };
}

export function ssmFetchByPath(region: string): FetchByPath {
  const call = ssmCall(region);
  return async (path) => {
    const out: Record<string, string> = {};
    let token: string | undefined;
    do {
      const resp = await call("GetParametersByPath", { Path: path.replace(/\/+$/, ""), Recursive: false, ...(token ? { NextToken: token } : {}) });
      for (const p of resp?.Parameters ?? []) {
        const name = typeof p?.Name === "string" ? p.Name : "";
        if (name && typeof p.Value === "string" && p.Value) out[name.split("/").pop()!] = p.Value;
      }
      token = typeof resp?.NextToken === "string" ? resp.NextToken : undefined;
    } while (token);
    return out;
  };
}

export interface HydrateOptions {
  env?: NodeJS.ProcessEnv;
  fetchByPath?: FetchByPath;
  sleep?: (ms: number) => Promise<void>;
  maxWaitMs?: number;
  retryMs?: number;
  log?: (msg: string) => void;
}

/** Fill the runtime ARNs the env does not set from SSM. Never throws. */
export async function hydrateRuntimeArns(opts: HydrateOptions = {}): Promise<void> {
  const env = opts.env ?? process.env;
  const path = (env.RUNTIME_ARN_PARAM_PATH ?? "").trim();
  if (!path) return;
  const fetchByPath = opts.fetchByPath ?? ssmFetchByPath(env.AWS_REGION ?? "us-west-2");
  const sleep = opts.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
  const log = opts.log ?? ((m: string) => console.log(m));
  const retryMs = opts.retryMs ?? 10_000;
  let waited = 0;
  const maxWait = opts.maxWaitMs ?? 15 * 60_000;
  for (;;) {
    const missing = RUNTIME_ARN_NAMES.filter((n) => !env[n]);
    if (missing.length === 0) return;
    try {
      const found = await fetchByPath(path);
      for (const n of missing) if (found[n]) env[n] = found[n];
    } catch (e: any) {
      log(`[agentcore-proxy] runtime ARN read from ${path} failed: ${e?.message ?? e}`);
    }
    const still = RUNTIME_ARN_NAMES.filter((n) => !env[n]);
    if (still.length === 0) {
      log(`[agentcore-proxy] runtime ARNs loaded from ${path}`);
      return;
    }
    if (waited >= maxWait) {
      log(`[agentcore-proxy] runtime ARNs still missing after ${Math.round(waited / 1000)}s: ${still.join(", ")}`);
      return;
    }
    await sleep(retryMs);
    waited += retryMs;
  }
}
