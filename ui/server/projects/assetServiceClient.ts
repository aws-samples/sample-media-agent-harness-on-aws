// Asset_Service client (media-harness-project-ingestion design §2 "Asset_Service Lambda"; Projects
// Phase B/C write routes).
//
// The proxy invokes the Asset_Service for every Project_Asset write (register_upload,
// register_s3_location, remove, record_chat_copy). The event is the Asset_Service contract
// (`agent/src/ingestion/handlers/asset_service.py`):
//
//   { userToken, projectId, command, args, caller: "proxy", sessionId? }
//   → { ok: true, value } | { ok: false, status, error: { code, message } }
//
// IAM limits who may invoke the function (the proxy task role is in its resource policy) and the
// `userToken` — the caller's Cognito access token, which the Asset_Service verifies — fixes the Actor,
// exactly like the Token_Issuer (projects-access D2). The proxy never asserts an actor id itself.
//
// Deployed: `ASSET_SERVICE_FN` (name or ARN), a SigV4-signed Lambda Invoke (the proxy has no
// @aws-sdk/client-lambda dependency, so this signs the REST call like `tokenIssuerClient.ts`).
//
// DEV ONLY: `LOCAL_ASSET_SERVICE_URL` (e.g. `http://127.0.0.1:8099`, served by
// `agent/scripts/local_asset_service.py`) receives the same event as JSON at `${url}/invoke`, unsigned.
// It is refused (with a warning) when `NODE_ENV=production`, like `LOCAL_TOKEN_ISSUER_URL`; the client
// then uses `ASSET_SERVICE_FN`. Only on that local target may an event carry `devBypassActor` (the
// proxy's `DEV_BYPASS_AUTH` actor, for a request with no bearer token); the local script honours it only
// with `--dev-bypass`, and it is stripped before any Lambda invoke.
//
// Every failure to reach the service (no configuration, timeout, non-2xx, a Lambda function error,
// a malformed body) is the reply `{ok:false, status:503, error:{code:"asset_service_unavailable"}}`, so a
// route maps it like any other refusal. Tokens and bodies are never logged.
import http from "node:http";
import https from "node:https";
import { HttpRequest } from "@smithy/protocol-http";
import { SignatureV4 } from "@smithy/signature-v4";
import { Sha256 } from "@aws-crypto/sha256-js";
import { defaultProvider } from "@aws-sdk/credential-provider-node";
import type { IssuerHttpRequest, IssuerHttpResponse, IssuerTransport, LambdaSigner } from "./tokenIssuerClient";

export const ASSET_SERVICE_FN_ENV = "ASSET_SERVICE_FN";
export const LOCAL_ASSET_SERVICE_URL_ENV = "LOCAL_ASSET_SERVICE_URL";
/** The Lambda runs for at most 90 s; an s3-location registration of 200 objects is the slow case. */
export const ASSET_SERVICE_BUDGET_MS = 85_000;
/** A RequestResponse payload is at most 6 MB; a 200-object s3-location reply is well under this. */
const MAX_RESPONSE_BYTES = 6 * 1024 * 1024;

export type AssetServiceCommand =
  | "register_upload"
  | "register_s3_location"
  | "remove"
  | "record_chat_session"
  | "remove_session_assets"
  /** Legacy: Phase 2 attaches by reference, so no new chat copy is ever recorded (asset-lineage R12). */
  | "record_chat_copy"
  | "resolve_assets"
  | "capabilities"
  | "enable_indexing"
  | "move_assets"
  | "rename_folder"
  | "create_folder"
  | "resolve_locations"
  | "search_images"
  | "create_bookmarks"
  | "remove_bookmarks"
  | "import_status"
  | "rescan_import"
  /** Copy the configured samples (the demo tour's 0-demo.mp4) into the caller's own Personal project, pinned. */
  | "seed_personal_samples"
  /** The read-only lineage graph (projects.lineageEdges): ancestors / descendants / the whole Project. */
  | "lineage";

export interface AssetServiceCall {
  /** The caller's Cognito access token (the Actor). Absent only under `DEV_BYPASS_AUTH`. */
  userToken?: string;
  /** DEV ONLY: the dev-bypass actor; sent to the LOCAL target only (see header). */
  devBypassActor?: string;
  projectId: string;
  command: AssetServiceCommand;
  args: Record<string, unknown>;
  sessionId?: string;
}

export interface AssetServiceError {
  code: string;
  message: string;
  [extra: string]: unknown;
}

export type AssetServiceReply =
  | { ok: true; value: Record<string, unknown> }
  | { ok: false; status: number; error: AssetServiceError };

export type InvokeAssetService = (call: AssetServiceCall) => Promise<AssetServiceReply>;

export interface AssetServiceClientOptions {
  env?: NodeJS.ProcessEnv;
  transport?: IssuerTransport;
  signer?: LambdaSigner;
  budgetMs?: number;
  warn?: (msg: string) => void;
}

type Target =
  | { kind: "local"; url: string }
  | { kind: "lambda"; region: string; hostname: string; path: string }
  | { kind: "none"; reason: string };

/** Which Asset_Service this env selects (exported for tests). */
export function resolveAssetServiceTarget(
  env: NodeJS.ProcessEnv,
  warn: (msg: string) => void = console.warn,
): Target {
  const local = (env[LOCAL_ASSET_SERVICE_URL_ENV] ?? "").trim();
  if (local) {
    if (env.NODE_ENV === "production") {
      warn(`[projects] ${LOCAL_ASSET_SERVICE_URL_ENV} is dev-only and is ignored when NODE_ENV=production`);
    } else {
      let parsed: URL | null = null;
      try {
        parsed = new URL(local);
      } catch {
        parsed = null;
      }
      if (!parsed || (parsed.protocol !== "http:" && parsed.protocol !== "https:")) {
        return { kind: "none", reason: `${LOCAL_ASSET_SERVICE_URL_ENV} is not an http(s) URL` };
      }
      return { kind: "local", url: `${local.replace(/\/+$/, "")}/invoke` };
    }
  }
  const fn = (env[ASSET_SERVICE_FN_ENV] ?? "").trim();
  if (!fn) return { kind: "none", reason: `${ASSET_SERVICE_FN_ENV} is not set` };
  const arnRegion = /^arn:aws[a-z-]*:lambda:([a-z0-9-]+):/.exec(fn)?.[1];
  const region = arnRegion || env.AWS_REGION || env.AWS_DEFAULT_REGION || "us-west-2";
  return {
    kind: "lambda",
    region,
    hostname: `lambda.${region}.amazonaws.com`,
    path: `/2015-03-31/functions/${encodeURIComponent(fn)}/invocations`,
  };
}

export function unavailable(reason: string): AssetServiceReply {
  return {
    ok: false,
    status: 503,
    error: { code: "asset_service_unavailable", message: "The asset service is unavailable; try again", reason },
  };
}

/** Map a parsed Asset_Service reply to the typed union; anything else is `unavailable`. */
export function mapAssetServicePayload(payload: unknown): AssetServiceReply {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return unavailable("malformed reply");
  const p = payload as Record<string, unknown>;
  if (p.ok === true) {
    const value = p.value;
    if (!value || typeof value !== "object" || Array.isArray(value)) return unavailable("malformed ok reply");
    return { ok: true, value: value as Record<string, unknown> };
  }
  if (p.ok === false) {
    const status = typeof p.status === "number" && Number.isInteger(p.status) ? p.status : 0;
    const err = p.error as { code?: unknown; message?: unknown } | undefined;
    if (status < 400 || status > 599 || !err || typeof err.code !== "string") return unavailable("malformed error reply");
    return {
      ok: false,
      status,
      error: { code: err.code, message: typeof err.message === "string" ? err.message : err.code },
    };
  }
  return unavailable("malformed reply");
}

function mapHttp(res: IssuerHttpResponse, kind: "local" | "lambda"): AssetServiceReply {
  if (res.status < 200 || res.status >= 300) return unavailable(`${kind} asset service returned ${res.status}`);
  if (kind === "lambda" && res.headers["x-amz-function-error"]) return unavailable("asset service function error");
  let payload: unknown;
  try {
    payload = JSON.parse(res.body);
  } catch {
    return unavailable("asset service reply is not JSON");
  }
  return mapAssetServicePayload(payload);
}

class BudgetExceeded extends Error {}

function withinBudget<T>(p: Promise<T>, signal: AbortSignal): Promise<T> {
  if (signal.aborted) return Promise.reject(new BudgetExceeded());
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(new BudgetExceeded());
    signal.addEventListener("abort", onAbort, { once: true });
    p.then(
      (v) => {
        signal.removeEventListener("abort", onAbort);
        resolve(v);
      },
      (e) => {
        signal.removeEventListener("abort", onAbort);
        reject(e);
      },
    );
  });
}

export function createAssetServiceClient(opts: AssetServiceClientOptions = {}): { invoke: InvokeAssetService } {
  const env = opts.env ?? process.env;
  const transport = opts.transport ?? nodeTransport;
  const signer = opts.signer ?? sigV4LambdaSigner;
  const budgetMs = opts.budgetMs ?? ASSET_SERVICE_BUDGET_MS;
  const sink = opts.warn ?? ((m: string) => console.warn(m));
  let warned = false;
  const warn = (m: string) => {
    if (!warned) {
      warned = true;
      sink(m);
    }
  };

  async function invoke(call: AssetServiceCall): Promise<AssetServiceReply> {
    const target = resolveAssetServiceTarget(env, warn);
    if (target.kind === "none") return unavailable(target.reason);
    const event: Record<string, unknown> = {
      userToken: call.userToken ?? "",
      projectId: call.projectId,
      command: call.command,
      args: call.args,
      caller: "proxy",
      ...(call.sessionId ? { sessionId: call.sessionId } : {}),
    };
    // DEV ONLY: the dev-bypass actor never leaves for a deployed function.
    if (target.kind === "local" && !call.userToken && call.devBypassActor) event.devBypassActor = call.devBypassActor;
    const body = JSON.stringify(event);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), budgetMs);
    try {
      if (target.kind === "local") {
        const res = await withinBudget(
          transport({ url: target.url, method: "POST", headers: { "content-type": "application/json" }, body, signal: controller.signal }),
          controller.signal,
        );
        return mapHttp(res, "local");
      }
      const baseHeaders = {
        host: target.hostname,
        "content-type": "application/json",
        "x-amz-invocation-type": "RequestResponse",
      };
      const headers = await withinBudget(
        signer({ region: target.region, hostname: target.hostname, path: target.path, headers: baseHeaders, body }),
        controller.signal,
      );
      const res = await withinBudget(
        transport({ url: `https://${target.hostname}${target.path}`, method: "POST", headers, body, signal: controller.signal }),
        controller.signal,
      );
      return mapHttp(res, "lambda");
    } catch (err) {
      if (err instanceof BudgetExceeded || controller.signal.aborted) return unavailable("timeout");
      return unavailable(`request failed: ${(err instanceof Error ? err.message : String(err)).slice(0, 200)}`);
    } finally {
      clearTimeout(timer);
    }
  }

  return { invoke };
}

// ---- default pieces ------------------------------------------------------------------------------

const lambdaSigners = new Map<string, SignatureV4>();

export const sigV4LambdaSigner: LambdaSigner = async ({ region, hostname, path, headers, body }) => {
  let s = lambdaSigners.get(region);
  if (!s) {
    s = new SignatureV4({ service: "lambda", region, credentials: defaultProvider(), sha256: Sha256 });
    lambdaSigners.set(region, s);
  }
  const signed = await s.sign(new HttpRequest({ method: "POST", protocol: "https:", hostname, path, headers, body }));
  return signed.headers as Record<string, string>;
};

export const nodeTransport: IssuerTransport = (req: IssuerHttpRequest) =>
  new Promise<IssuerHttpResponse>((resolve, reject) => {
    const lib = req.url.startsWith("https:") ? https : http;
    const r = lib.request(
      req.url,
      { method: req.method, headers: { ...req.headers, "content-length": String(Buffer.byteLength(req.body)) }, signal: req.signal },
      (res) => {
        const chunks: Buffer[] = [];
        let size = 0;
        res.on("data", (c: Buffer) => {
          size += c.length;
          if (size > MAX_RESPONSE_BYTES) {
            res.destroy(new Error("asset service response too large"));
            return;
          }
          chunks.push(c);
        });
        res.on("error", (e) => reject(e));
        res.on("end", () => {
          const headers: Record<string, string> = {};
          for (const [k, v] of Object.entries(res.headers)) {
            if (v !== undefined) headers[k.toLowerCase()] = Array.isArray(v) ? v.join(", ") : v;
          }
          resolve({ status: res.statusCode ?? 500, headers, body: Buffer.concat(chunks).toString("utf8") });
        });
      },
    );
    r.on("error", (e) => reject(e));
    r.end(req.body);
  });

let defaultClient: { invoke: InvokeAssetService } | undefined;

/** Invoke the configured Asset_Service (env read per call). Never throws. */
export function invokeAssetService(call: AssetServiceCall): Promise<AssetServiceReply> {
  if (!defaultClient) defaultClient = createAssetServiceClient();
  return defaultClient.invoke(call);
}
