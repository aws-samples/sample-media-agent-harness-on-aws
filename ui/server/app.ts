/**
 * Express app that proxies requests to AgentCore with SigV4 signing.
 *
 * Extracted from the original Vite dev-middleware plugin (`ui/agentcore-proxy.ts`)
 * so the same route logic can run as a standalone container (ECS Fargate) in
 * staging/prod, not just inside `vite dev`. The only structural change from
 * the original is the registration API: `server.middlewares.use(path, fn)`
 * (Vite's bundled `connect` instance) becomes `app.use(path, fn)` (Express).
 * Express's `.use(path, fn)` strips-and-restores the mount prefix from
 * `req.url` around each middleware call exactly like `connect` does, so the
 * `/jobs` → `next()` → `/jobs/stream` fallthrough below is unchanged.
 *
 * `ui/agentcore-proxy.ts` is now a thin Vite plugin that re-registers this
 * same route list against `server.middlewares` for local `npm run dev` —
 * see that file for the dev-only wiring.
 *
 * Routes:
 *   /api/*        → Agent runtime (invoke, SigV4)
 *   /lab/*        → Lab_Service runtime (invoke, SigV4) — skill CRUD, specialty CRUD,
 *                   tool collection CRUD, workflow CRUD, chat, harness, readiness,
 *                   gateway browse/refresh
 *   /catalog      → Gateway tools/list (direct, JWT Bearer token)
 *   /upload       → File upload → S3 (multipart form data)
 *   /jobs(/...)   → JobRegistry CRUD reads (GET /jobs, /jobs/:id, /jobs?ids=...)
 *   /jobs/stream  → SSE push channel sourced from the per-actor SQS FIFO queue
 *   /health       → Plain 200, no auth — ALB/ECS target group health check
 *
 * Env vars:
 *   AGENT_RUNTIME_ARN        (required) — AgentCore runtime ARN (Main Agent)
 *   LAB_SERVICE_RUNTIME_ARN  (optional) — AgentCore runtime ARN (Lab_Service)
 *   RENDER_AGENT_RUNTIME_ARN (optional) — AgentCore runtime ARN (Render Agent)
 *   TASK_AGENT_RUNTIME_ARN   (optional) — AgentCore runtime ARN (Task Agent)
 *   UPLOAD_BUCKET            (optional) — S3 bucket for file uploads (defaults to SKILLS_S3_BUCKET pattern)
 *   SESSIONS_TABLE           (optional) — DynamoDB sessions table; when unset, /sessions returns 500
 *   PROJECTS_TABLE           (optional) — Project_Registry table; when unset, /projects returns 404 projects_not_enabled
 *   JOB_REGISTRY_TABLE       (optional) — DynamoDB JobRegistry table name; when unset, /jobs returns 500
 *   IMPROVE_TABLE            (optional) — DynamoDB Improve store table name; when unset, /improve returns 500
 *   JOB_FANOUT_QUEUE_URL     (optional) — SQS FIFO queue URL fed by the changefeed-fanout Lambda;
 *                                          when unset, /jobs/stream returns 500
 *   MEMORY_ID                (optional) — AgentCore Memory id; when unset, /memory/* returns empty results
 *   ARTIFACTS_DOMAIN         (optional) — CloudFront domain surfaced via /config
 *   USER_POOL_ID             (optional) — Cognito User Pool id surfaced via /config, for the
 *                                          frontend's oidc-client-ts UserManager
 *   USER_POOL_DOMAIN         (optional) — Cognito hosted-UI base URL (e.g. https://<domain>.auth.
 *                                          <region>.amazoncognito.com), surfaced via /config for
 *                                          the frontend's hand-built logout URL (see authManager.ts)
 *   AWS_REGION               (optional) — defaults to us-west-2, also surfaced via /config
 */

import express, { type Express } from "express";
import { HttpRequest } from "@smithy/protocol-http";
import { SignatureV4 } from "@smithy/signature-v4";
import { Sha256 } from "@aws-crypto/sha256-js";
import crypto from "crypto";
import { defaultProvider } from "@aws-sdk/credential-provider-node";
import { BedrockClient, ListInferenceProfilesCommand } from "@aws-sdk/client-bedrock";
import { S3Client, PutObjectCommand, GetObjectCommand, ListObjectsV2Command, DeleteObjectCommand } from "@aws-sdk/client-s3";
import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import { TranscriptAccumulator } from "./transcriptAccumulator";
import { finalizeTurn, buildTurnMessages, readTranscriptFromS3 } from "./transcriptStore";
import type { StoredMessage } from "./transcriptStore";
import { handleMediaActions } from "./mediaActions";
import { parseRenderOutcome } from "./renderOutcome";
import { SFNClient, DescribeExecutionCommand } from "@aws-sdk/client-sfn";
import { DynamoDBDocumentClient, QueryCommand, PutCommand, UpdateCommand, GetCommand, DeleteCommand } from "@aws-sdk/lib-dynamodb";
import {
  SQSClient,
  ReceiveMessageCommand,
  DeleteMessageCommand,
  ChangeMessageVisibilityCommand,
  SendMessageCommand,
} from "@aws-sdk/client-sqs";
import type { JobUpdateEvent } from "../src/types/jobs";
import { supportsEffort } from "./reasoningEffort";
import { projectJob } from "./jobsProjection";
import { queryActorJobRows, selectRecentRuns, selectSessionJobs, encodeJobsCursor, decodeJobsCursor } from "./jobsList";
import { BedrockAgentCoreClient, CreateEventCommand, ListEventsCommand, ListSessionsCommand } from "@aws-sdk/client-bedrock-agentcore";
import { CognitoIdentityProviderClient, ListUsersCommand, ListUsersInGroupCommand, AdminAddUserToGroupCommand, AdminRemoveUserFromGroupCommand, AdminResetUserPasswordCommand, AdminDeleteUserCommand, AdminCreateUserCommand, AdminDisableUserCommand, AdminEnableUserCommand } from "@aws-sdk/client-cognito-identity-provider";
import { getSignedUrl } from "@aws-sdk/s3-request-presigner";
import { URL } from "url";
import https from "https";
import http from "http";
import { catalogWithoutRuntime } from "./toolCatalog";
import { mcpServersForActor, ownershipRowsForActor } from "./mcpServers";
import Busboy from "busboy";
import { validateJwt } from "../auth/validateJwt";
// Per-agent OIDC verifier seam for the external invoke path (POST /invoke/:agentId).
// The verifier itself is task 10.1 (agentOidc.ts is a placeholder stub until
// then); this route (task 9.1) is wired against its stable type contract.
import { verifyAgentToken } from "./auth/agentOidc";
// External invoke route handler factory (POST /invoke/:agentId), extracted
// from this file for isolated integration testing (task 9.4). The pure
// two-identity stamping seam (Property 8) lives under the same folder and is
// consumed internally by the handler.
import { createInvokeHandler } from "./invoke/invokeRoute";
// External jobs read route (GET /invoke/:agentId/jobs) — long-running workflow
// tracking on the OIDC caller realm. Same identity gate as invoke; scopes reads
// to the caller's (Owner actorId, derived external session).
import { createInvokeJobsHandler } from "./invoke/invokeJobsRoute";
// External media upload route (POST /invoke/:agentId/uploads) — issues a
// presigned S3 PUT so an external OIDC caller can upload media into the same
// artifacts/ location the agent reads from. Same identity gate as invoke.
import { createInvokeUploadHandler } from "./invoke/invokeUploadRoute";
// External render-manifest read route (GET /invoke/:agentId/manifest) — serves a
// render manifest JSON same-origin (CloudFront has no CORS for JSON) so the
// external console can render the media it describes. Same identity gate as invoke.
import { createInvokeManifestHandler } from "./invoke/invokeManifestRoute";
// External artifact download (short `?ref=` links so the agent never has to
// paste a ~1,700-char presigned URL into a reply).
import { createInvokeDownloadHandler } from "./invoke/invokeDownloadRoute";
import { labRuntimeSessionId } from "./labSession";
import {
  decideRenderSession,
  composeRenderTabs,
  isSafeSessionId,
  isSessionRenderKey,
  renderViewId,
  renderViewManifestError,
  RENDER_VIEW_MAX_BYTES,
  RENDER_VIEW_PATCH_MAX_BYTES,
} from "./renderSession";
import { mergeViewState, normalizeViewState } from "../src/shared/renderViewState";
import { uploadKeyFor } from "./uploadKey";
import { artifactOwnerTag } from "./artifactOwner";
import { streamUploadToS3, FileTooLargeError } from "./uploadStream";
// Per-agent / per-caller rate-limit & quota gate for the external invoke path
// (task 14.1, Req 14.1/14.2). In-memory (per proxy instance); a distributed
// store is a documented future enhancement.
import { createInvokeRateLimiter } from "./invoke/rateLimiter";
// Streaming transport reliability for the external invoke path (task 15.1,
// Req 15.1/15.3/15.4): per-instance concurrent-stream cap + keepalive/idle-
// timeout budget. Both env-configurable (INVOKE_MAX_CONCURRENT_STREAMS,
// INVOKE_SSE_KEEPALIVE_MS, INVOKE_*_IDLE_TIMEOUT_MS) with documented defaults.
import { streamGuardFromEnv, transportConfigFromEnv } from "./invoke/streamGuard";
// Observability, audit & kill switch for the external invoke path (task 17.1,
// Req 17). The kill switch (global + per-agent, admin-controlled, seeded from
// INVOKE_KILL_SWITCH_* env) is checked FIRST in the pipeline; the structured
// audit logger writes one entry per invocation (incl. denials) to stdout.
import { killSwitchFromEnv } from "./invoke/killSwitch";
import { createConsoleAuditLogger } from "./invoke/auditLog";
// Owner-side Custom Agent test invoke (internal realm; Cognito-authed). See
// `agentTestRoute.ts` for why this is a dedicated route rather than `/dispatch`.
import { createAgentTestHandler } from "./agentTestRoute";
// Project_Registry routes (`/projects*`, Cognito-authed). See `projects/routes.ts`.
import { createProjectsHandler } from "./projects/routes";
import { createAssetServiceClient, type InvokeAssetService } from "./projects/assetServiceClient";
import { checkAccess as checkProjectAccess, graphFeatureEnabled, lineageFeatureEnabled, projectsEnabled, webSearchFeatureEnabled } from "./projects/access";
import { readAssetSource } from "./projects/ingestionRoutes";
// Authenticated session-artifact ref → short-lived presigned URL resolver (the "no broken links"
// media backbone — the UI stores stable artifacts/<session>/… keys and re-mints URLs via this on
// load/error). See artifactUrlsRoute.ts.
import {
  handleArtifactUrls,
  defaultPresignGet,
  authorizeArtifactKey,
  normalizeArtifactKey,
  envAuthorizeM2cRef,
  envAuthorizeUnattributed,
  envCheckProjectMember,
  isM2cArtifactKey,
  parseM2cArtifactKey,
  ARTIFACT_URL_TTL_S,
  type AuthorizeM2cRef,
  type AuthorizeUnattributed,
  type CheckProjectMember,
  type SessionOwnership,
} from "./artifactUrlsRoute";
import { isProjectSessionOutputKey } from "../src/shared/sessionLayout";
// Re-sign artifact references inside a served HTML artifact (presigned URLs baked into a
// saved document expire; see the module header and session b42731df).
import {
  isRewritableHtml,
  resignHtmlArtifact,
} from "./artifacts/htmlArtifactRewrite";
import {
  documentViewUrl,
  isViewedDocumentKey,
  mintViewTicket,
  verifyViewTicket,
} from "./artifacts/viewTicket";
import { artifactServePolicy, isolationHeaders, safeDispositionFilename } from "./artifacts/servePolicy";
// Session-row Projects fields; both return "nothing to add" when Projects are disabled.
import { sessionCreateFields, sessionPatchFields } from "./projects/sessionProjects";
// Chat-turn gate: Project_Token, `__turn_context`. Skipped entirely when Projects are disabled.
import { dynamoSessionStore, runTurnGate } from "./projects/turnGate";
import { makeGeneratedMediaNotifier } from "./projects/generatedMedia";
import type { TurnGateOutcome } from "./projects/turnGate";
import { PROJECT_TOKEN_HEADER } from "./projects/tokenIssuerClient";
import { TURN_CONTEXT_BODY_KEY } from "./projects/turnContext";
import { createSerializer } from "./render/serializeBy";
import { decideApiSession, prepareRuntimeBody } from "./runtimeBody";
import { originVerifyMiddleware } from "./originVerify";

// Blank-line keep-alive for the streamed chat relay. 20s is well inside the
// ALB's 60s idle timeout, leaving room for a missed tick.
// Catalog fallback session id, derived per actor.
//
// This was previously a single pinned constant so the fallback reused a warm
// runtime (10.3s cold vs 0.2s warm). That is unsafe once the gateway
// interceptor filters tools per actor: AgentCore gives each session its own
// microVM, so one shared session id means one container serving every user,
// and the runtime's gateway connection — established once per container —
// would be bound to whichever actor happened to warm it. Every other user
// would then be shown that actor's filtered tool list.
//
// Deriving from the actor keeps the warm-reuse benefit per user while making
// cross-actor container sharing impossible. AgentCore requires >= 33 chars.
// Header carrying the end user's token into the runtime. MUST match
// USER_TOKEN_HEADER in infra/lib/agent-infra-stack.ts and the runtime's
// RequestHeaderAllowlist -- a non-allowlisted header is dropped silently, which
// would present as the gateway rejecting every call for no visible reason. The
// `X-Amzn-Bedrock-AgentCore-Runtime-Custom-` prefix is required by AgentCore.
const USER_TOKEN_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-UserToken";

const CATALOG_SESSION_PREFIX = "harness-catalog-";

function catalogSessionIdFor(actorId: string): string {
  const safe = (actorId || "anonymous").replace(/[^a-zA-Z0-9-]/g, "-");
  return `${CATALOG_SESSION_PREFIX}${safe}`.padEnd(40, "0");
}

const STREAM_HEARTBEAT_MS = 20_000;
const STREAM_HEARTBEAT_TICK_MS = 5_000;
// How often the proxy writes a PROVISIONAL transcript snapshot during a long streaming turn, so a
// mid-turn reload recovers the in-flight turn with its steps. Matches the client's former
// PROVISIONAL_PERSIST_MS. Upsert-by-turnId means the final write replaces it.
const TRANSCRIPT_PROVISIONAL_MS = 4_000;
// Gap before the single /render retry after a Render Agent 5xx/429.
const RENDER_RETRY_DELAY_MS = 1_500;

const REGION = process.env.AWS_REGION ?? "us-west-2";

const agentCoreSigner = new SignatureV4({
  service: "bedrock-agentcore",
  region: REGION,
  credentials: defaultProvider(),
  sha256: Sha256,
});

// ---- Auth middleware (R3, R5, R6, R7) ----
//
// Validates the bearer token on every protected request and attaches the
// resolved actor id (Cognito `sub`) to `req.actorId`. Downstream handlers read
// that field so each request is keyed on the caller's identity.
//
// When `DEV_BYPASS_AUTH=true` the middleware skips validation entirely and
// uses `LAB_DEFAULT_ACTOR_ID` (defaulting to `local-dev-user`) so local dev
// still works without a real Cognito user.
// `userToken` is the caller's validated bearer token, retained so it can be
// forwarded to the runtime for onward gateway calls. Everything else keys off
// `actorId`; this is a credential, not an identity.
type AuthedRequest = { actorId?: string; actorGroups?: string[]; userToken?: string } & {
  headers: Record<string, string | string[] | undefined>;
};

function isDevBypassEnabled(): boolean {
  return process.env.DEV_BYPASS_AUTH === "true";
}

function devBypassActorId(): string {
  return process.env.LAB_DEFAULT_ACTOR_ID ?? "local-dev-user";
}

function send401(res: any, code: string, detail: string): void {
  res.writeHead(401, {
    "content-type": "application/json",
    "access-control-allow-origin": "*",
    "www-authenticate": `Bearer error="${code}"`,
  });
  res.end(JSON.stringify({ error: { code, message: detail } }));
}

/**
 * Admin gate primitive: membership in the Cognito `gateway-admins` group — the
 * harness's authoritative admin signal, mirroring the agent's `_is_admin`
 * (agent/src/main.py). Do NOT introduce a separate `custom:role`. Exported so
 * the `/admin` route gate and its unit tests share one definition.
 */
export const ADMIN_GROUP = "gateway-admins";
export function hasAdminGroup(groups: readonly string[] | undefined | null): boolean {
  return Array.isArray(groups) && groups.includes(ADMIN_GROUP);
}

/** The 401 message when a protected route is called with no bearer token (header only; no query transport). */
export const MISSING_TOKEN_MESSAGE = "Authorization header is required";

async function authMiddleware(req: any, res: any, next: (err?: any) => void): Promise<void> {
  // Dev bypass short-circuit — no token required, no validation performed.
  if (isDevBypassEnabled()) {
    (req as AuthedRequest).actorId = devBypassActorId();
    // Dev mode grants full admin access for local development.
    (req as AuthedRequest).actorGroups = ["gateway-admins"];
    next();
    return;
  }

  // The bearer token travels ONLY in the ``Authorization: Bearer <jwt>`` header.
  //
  // A ``?access_token=<jwt>`` query transport used to be accepted here (for the `/jobs/stream`
  // EventSource, and the document viewer's iframe). A token in a URL is the user's whole identity
  // for up to an hour, and URLs leak: into the agent-written page that can read its own
  // `location`, into access logs, Referer headers and browser history. The SSE client now reads
  // the stream with `fetch` and a header (useJobsSubscription), and every frame or link a browser
  // must load without a header carries a VIEW TICKET instead (artifacts/viewTicket.ts) — valid for
  // one key, read-only. So a query token is now ignored, not validated: a request carrying only one
  // is unauthenticated.
  let token: string | null = null;
  const tokenSource = "header";

  const authHeader = req.headers["authorization"];
  if (typeof authHeader === "string" && authHeader.trim() !== "") {
    const match = /^Bearer\s+(.+)$/i.exec(authHeader.trim());
    if (!match) {
      send401(res, "missing_token", "Authorization header must be of the form 'Bearer <token>'");
      return;
    }
    const headerToken = match[1].trim();
    if (!headerToken) {
      send401(res, "missing_token", "Bearer token is empty");
      return;
    }
    token = headerToken;
  }

  if (!token) {
    send401(res, "missing_token", MISSING_TOKEN_MESSAGE);
    return;
  }

  try {
    const { sub, groups } = await validateJwt(token);
    (req as AuthedRequest).actorId = sub;
    (req as AuthedRequest).actorGroups = groups;
    (req as AuthedRequest).userToken = token;
    next();
  } catch (err: any) {
    console.error(
      `[agentcore-proxy] JWT validation failed (source=${tokenSource}):`,
      err?.message ?? err,
    );
    send401(res, "invalid_token", err?.message ?? "Token validation failed");
  }
}

// ---- PATCH /sessions/:id ownership (no orphan rows) ----

/** The row exists and belongs to the caller (`#actor` → actorId, `:actor` → the caller's id). */
const SESSION_OWNED_CONDITION = "attribute_exists(sessionId) AND #actor = :actor";
/** A soft-deleted session (`active = 0`) takes no more writes, so nothing keeps a deleted chat alive. Rows
 *  written before `active` existed have no attribute and count as live. Needs `:one` bound to 1. */
const SESSION_LIVE_CONDITION = "(attribute_not_exists(active) OR active = :one)";

/** A DynamoDB attribute from a `ReturnValuesOnConditionCheckFailure` image: marshalled or plain. */
function oldImageAttr(item: any, name: string): unknown {
  const v = item?.[name];
  if (v && typeof v === "object" && !Array.isArray(v)) {
    if ("S" in v) return v.S;
    if ("BOOL" in v) return v.BOOL;
  }
  return v;
}

/** True when a failed PATCH condition was the lock (the caller owns the row and it is locked). */
export function sessionFailureIsLocked(err: any, actorId: string): boolean {
  const item = err?.Item;
  if (!item) return false;
  return oldImageAttr(item, "actorId") === actorId && oldImageAttr(item, "locked") === true;
}

function sendSessionNotFound(res: any, sessionId: string): void {
  res.writeHead(404, { "content-type": "application/json", "access-control-allow-origin": "*" });
  res.end(JSON.stringify({ error: "Session not found", code: "session_not_found", sessionId }));
}

function guessMimeType(filename: string): string {
  const ext = filename.split(".").pop()?.toLowerCase() ?? "";
  const mimes: Record<string, string> = {
    pdf: "application/pdf", mp4: "video/mp4", webm: "video/webm", mov: "video/quicktime",
    mp3: "audio/mpeg", wav: "audio/wav", flac: "audio/flac",
    png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif",
    json: "application/json", csv: "text/csv", txt: "text/plain",
    py: "text/x-python", ts: "text/typescript", js: "application/javascript",
    html: "text/html", htm: "text/html", svg: "image/svg+xml", xml: "text/xml", md: "text/markdown",
    vtt: "text/vtt", srt: "application/x-subrip", webp: "image/webp", m4a: "audio/mp4",
  };
  return mimes[ext] ?? "application/octet-stream";
}

// ---- Shared SigV4 fetch ----

async function signedFetch(
  url: string,
  body: Buffer,
  extraHeaders?: Record<string, string>
): Promise<{ status: number; headers: Record<string, string>; body: Buffer }> {
  const parsed = new URL(url);

  const headers: Record<string, string> = {
    host: parsed.hostname,
    "content-type": "application/json",
    ...extraHeaders,
  };

  const request = new HttpRequest({
    method: "POST",
    protocol: parsed.protocol,
    hostname: parsed.hostname,
    port: parsed.port ? Number(parsed.port) : undefined,
    path: parsed.pathname,
    query: Object.fromEntries(parsed.searchParams),
    headers,
    body,
  });

  // Local E2E override (env-gated): an http:// (localhost) target is a locally-run agent runtime,
  // which needs NO SigV4 — POST directly with plain http. No-op for the deployed https endpoints.
  // Decide BEFORE signing: signing needs AWS credentials, which a local run (or CI) may not have.
  const isLocal = parsed.protocol === "http:";
  const transport = isLocal ? http : https;
  const outHeaders = (isLocal ? headers : (await agentCoreSigner.sign(request)).headers) as Record<string, string>;

  return new Promise((resolve, reject) => {
    const req = transport.request(
      url,
      {
        method: "POST",
        headers: outHeaders,
      },
      (res) => {
        const chunks: Buffer[] = [];
        res.on("data", (chunk) => chunks.push(chunk));
        res.on("end", () => {
          const respHeaders: Record<string, string> = {};
          for (const [k, v] of Object.entries(res.headers)) {
            if (typeof v === "string") respHeaders[k] = v;
          }
          resolve({
            status: res.statusCode ?? 500,
            headers: respHeaders,
            body: Buffer.concat(chunks),
          });
        });
      }
    );
    req.on("error", reject);
    req.write(body);
    req.end();
  });
}

// Streaming variant of signedFetch: signs the request with SigV4 and resolves
// with the upstream IncomingMessage BEFORE the body is consumed, so the caller
// can pipe chunks to the client as they arrive. This is what makes the agent's
// SSE steps/text reach the browser incrementally — signedFetch (above) buffers
// the entire response via Buffer.concat and only resolves on `end`, which
// collapsed live streaming into a single one-shot flush at turn completion.
function signedRequestStream(
  url: string,
  body: Buffer,
  extraHeaders?: Record<string, string>
): Promise<import("http").IncomingMessage> {
  const parsed = new URL(url);

  const headers: Record<string, string> = {
    host: parsed.hostname,
    "content-type": "application/json",
    ...extraHeaders,
  };

  const request = new HttpRequest({
    method: "POST",
    protocol: parsed.protocol,
    hostname: parsed.hostname,
    port: parsed.port ? Number(parsed.port) : undefined,
    path: parsed.pathname,
    query: Object.fromEntries(parsed.searchParams),
    headers,
    body,
  });

  // Local E2E override (env-gated): http:// target = locally-run runtime, no SigV4. Decided BEFORE
  // signing, which needs AWS credentials a local run (or CI) may not have.
  const isLocal = parsed.protocol === "http:";
  const signing = isLocal ? Promise.resolve(null) : agentCoreSigner.sign(request);
  return signing.then(
    (signed) =>
      new Promise<import("http").IncomingMessage>((resolve, reject) => {
        const transport = isLocal ? http : https;
        const outHeaders = (isLocal ? headers : signed!.headers) as Record<string, string>;
        const req = transport.request(
          url,
          { method: "POST", headers: outHeaders },
          (res) => resolve(res),
        );
        req.on("error", reject);
        req.write(body);
        req.end();
      }),
  );
}

// ---- App ----

/**
 * Strip inline render manifests from a renderHistory before persisting it to the session row.
 *
 * A COMPOSITE render block inlines the FULL manifest object into `block.data.manifest` (observed
 * at ~471 KB for one timeline). renderHistory shares the session row with the chat `transcript`,
 * so a single inlined manifest pushed the row past DynamoDB's 400 KB item limit — after which
 * EVERY write to that row (transcript, renderHistory, lastActivityAt) was rejected, silently
 * dropping chat turns (they only survived in localStorage and vanished on refresh).
 *
 * The S3 render manifests (`GET /sessions/:id/renders`) are authoritative and re-derive every
 * render on load, so the row only needs lean REFERENCES: keep `manifestUrl` (solo) and
 * `sourceKey`/`memberKeys` (composite) plus labels/timestamps, and drop the inlined `manifest`.
 * Each entry then costs ~0.2 KB instead of hundreds of KB, keeping the row well under the limit.
 */
export function leanRenderHistory(history: unknown[]): unknown[] {
  if (!Array.isArray(history)) return [];
  return history.map((entry) => {
    if (!entry || typeof entry !== "object") return entry;
    const e = entry as Record<string, unknown>;
    if (!Array.isArray(e.blocks)) return e;
    return {
      ...e,
      blocks: (e.blocks as unknown[]).map((block) => {
        if (!block || typeof block !== "object") return block;
        const b = block as Record<string, unknown>;
        const data = b.data;
        if (!data || typeof data !== "object" || !("manifest" in (data as Record<string, unknown>))) return b;
        const { manifest, ...leanData } = data as Record<string, unknown>;
        void manifest; // dropped: re-derived from the authoritative S3 manifest on load
        return { ...b, data: leanData };
      }),
    };
  });
}

/**
 * Read a request body, keeping at most `maxBytes`. Resolves `null` when the body is larger (the rest
 * is drained and discarded so the response can still be written on the same connection).
 */
export async function readBodyCapped(req: any, maxBytes: number): Promise<Buffer | null> {
  const chunks: Buffer[] = [];
  let size = 0;
  let over = false;
  req.on("data", (c: Buffer) => {
    size += c.length;
    if (size > maxBytes) { over = true; chunks.length = 0; return; }
    if (!over) chunks.push(c);
  });
  await new Promise<void>((resolve, reject) => { req.on("end", resolve); req.on("error", reject); });
  return over ? null : Buffer.concat(chunks);
}

/**
 * Cap the persisted chat transcript by BYTES (not just message count) so the transcript alone can
 * never push the session row toward DynamoDB's 400 KB item limit. Keeps the most recent messages,
 * dropping oldest first until the serialized transcript fits the budget. Paired with
 * leanRenderHistory, this keeps the whole row comfortably under the limit.
 */
export function byteCapTranscript(transcript: unknown, maxBytes = 300_000): unknown[] {
  let out = Array.isArray(transcript) ? transcript.slice(-100) : [];
  while (out.length > 1 && Buffer.byteLength(JSON.stringify(out), "utf8") > maxBytes) {
    out = out.slice(1);
  }
  return out;
}

/**
 * The Asset_Service client used by `DELETE /sessions/:id` to drop that session's hidden
 * "this chat only" assets (asset-lineage D2). Built lazily and once: with no `ASSET_SERVICE_FN` the
 * client itself answers `503 asset_service_unavailable`, so the delete still succeeds.
 */
let _sessionAssetRemover: { invoke: InvokeAssetService } | null = null;
function sessionAssetRemover(): { invoke: InvokeAssetService } {
  if (!_sessionAssetRemover) _sessionAssetRemover = createAssetServiceClient();
  return _sessionAssetRemover;
}

/** Test seam: forget the memoised client. */
export function __resetSessionAssetRemoverForTests(): void {
  _sessionAssetRemover = null;
}

export function buildApp(): Express {
  const app = express();

  const arn = process.env.AGENT_RUNTIME_ARN;
  if (!arn) {
    throw new Error(
      "[agentcore-proxy] AGENT_RUNTIME_ARN env var is required."
    );
  }

  const encoded = encodeURIComponent(arn);
  // Local E2E override (env-gated, no-op in prod): LOCAL_MAIN_AGENT_URL points at a locally-run
  // main runtime (e.g. http://localhost:8080/invocations) so Playwright can drive the uncommitted
  // agent code end-to-end with no deploy. signedFetch/signedRequestStream skip SigV4 for http://.
  const invokeUrl = process.env.LOCAL_MAIN_AGENT_URL
    || `https://bedrock-agentcore.${REGION}.amazonaws.com/runtimes/${encoded}/invocations?qualifier=DEFAULT`;

  // Render Agent runtime — dedicated media/artifact rendering. The
  // /render endpoint invokes it to produce an NLE manifest.
  const renderArn = process.env.RENDER_AGENT_RUNTIME_ARN ?? "";
  const renderInvokeUrl = process.env.LOCAL_RENDER_AGENT_URL
    || (renderArn
      ? `https://bedrock-agentcore.${REGION}.amazonaws.com/runtimes/${encodeURIComponent(renderArn)}/invocations?qualifier=DEFAULT`
      : "");

  console.log(`[agentcore-proxy] Runtime: ${arn}`);
  console.log(`[agentcore-proxy] Render Agent: ${renderArn || "(not configured)"}`);

  // Origin lock (originVerify.ts): FIRST, before any route — a request that did not come through our
  // CloudFront distribution is refused whatever its path (only /health is exempt).
  app.use(originVerifyMiddleware(process.env.ORIGIN_VERIFY_SECRET));

  // /health — plain 200, no auth. ALB/ECS target group health check.
  app.use("/health", (_req, res) => {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ status: "ok" }));
  });

  // Mount the auth middleware on every protected path BEFORE the handlers
  // that follow. Express's middleware stack is connect-style, so each `use`
  // runs in order: this guarantees that downstream `/sessions`, `/memory`,
  // `/lab`, and `/api` handlers see `req.actorId` populated (or have
  // already been short-circuited with a 401).
  const authPaths = ["/api", "/lab", "/sessions", "/projects", "/memory", "/catalog", "/mcp", "/jobs", "/render", "/agent-test", "/admin", "/upload", "/models", "/media"];
  // Public read-through(s): artifact content under `artifacts/` is already public (served by the
  // artifacts CloudFront). `/api/artifact` only adds correct content-type + streaming, so it must
  // be reachable by a plain browser navigation (Open/Download links) — which cannot carry an
  // Authorization header. It is NOT auth-gated; instead the handler validates the key is under
  // `artifacts/` so it can never read anything but public artifact content.
  //
  // EXCEPT when `ARTIFACTS_REQUIRE_AUTH=true` (set per env by `artifactsCdnPrivate`, staging first):
  // the artifacts CDN is then locked, so this read-through must not become the new public door.
  // `/api/artifact` is dropped from the exemption, the auth middleware runs (Bearer header or
  // a view ticket, which a plain navigation CAN carry), and the handler applies the shared
  // artifact-key policy (`authorizeArtifactKey`). Unset → byte-for-byte the old public behavior.
  const artifactsRequireAuth = process.env.ARTIFACTS_REQUIRE_AUTH === "true";
  // HMAC key for view tickets (artifacts/viewTicket.ts). Deployed envs inject a stable one from
  // Secrets Manager so every proxy task agrees and a restart does not void open documents. Absent
  // (local dev), a per-process random key keeps tickets working for this process only.
  const artifactViewSecret = process.env.ARTIFACT_VIEW_SECRET || crypto.randomBytes(32).toString("hex");
  if (!process.env.ARTIFACT_VIEW_SECRET && artifactsRequireAuth) {
    console.warn("[agentcore-proxy] ARTIFACT_VIEW_SECRET unset: document view links are valid for this task only");
  }
  const PUBLIC_PATHS = artifactsRequireAuth ? [] : ["/api/artifact"];
  // Matched at a PATH BOUNDARY, not by bare prefix. A `startsWith` exemption silently covers
  // every path merely beginning with the string — `/api/artifacts`, `/api/artifact-registry` —
  // so registering any such route later would publish it, with nothing in the diff to suggest
  // auth was involved. (Those paths do 401 today, but only from the `/api` catch-all's own
  // defensive header check further down; the auth middleware had already waved them through.
  // Relying on that is relying on an unrelated handler that a new route would sit in front of.)
  const isPublicPath = (req: any) => {
    const [path, query = ""] = String(req.originalUrl || req.url || "").split("?");
    const params = new URLSearchParams(query);
    // An M2C_REFERENCE (`artifacts/m2c/<bucket>/<key>`) is never public, in either mode: it is
    // served only to an actor who can read a project that references it, so the auth middleware
    // must run to establish WHO is asking (Bearer header), unless a view ticket names the viewer.
    if (path === "/api/artifact" && isM2cArtifactKey(normalizeArtifactKey(params.get("key") ?? "")) && !params.has("view")) {
      return false;
    }
    // A projects-session OUTPUT key (asset-lineage Phase 3 (a)) is never public either: it is served
    // only to the session owner or a project member, so the caller must be known.
    if (path === "/api/artifact" && isProjectSessionOutputKey(params.get("key") ?? "") && !params.has("view")) {
      return false;
    }
    // A document read carrying a view ticket authenticates WITH the ticket, which the
    // `/api/artifact` handler verifies (and fails closed on). Exactly that path, nothing under it.
    if (path === "/api/artifact" && params.has("view")) return true;
    return PUBLIC_PATHS.some((pub) => path === pub || path.startsWith(`${pub}/`));
  };
  // EVERY `/api/artifact` response -- including the 400/401/410 JSON refusals the auth middleware
  // and the handler write -- is served sniff-proof and sandboxed (artifacts/servePolicy.ts). The
  // handler drops the CSP only for a PDF it streams. Registered before auth so a refusal has them.
  app.use("/api/artifact", (_req: any, res: any, next: any) => {
    for (const [h, v] of Object.entries(isolationHeaders(null))) res.setHeader(h, v);
    next();
  });
  for (const p of authPaths) {
    app.use(p, (req: any, res: any, next: any) => {
      if (isPublicPath(req)) return next();
      authMiddleware(req, res, next).catch((err) => {
        console.error("[agentcore-proxy] Auth middleware error:", err?.message ?? err);
        if (!res.headersSent) {
          send401(res, "auth_error", "Internal auth error");
        }
      });
    });
  }
  if (isDevBypassEnabled()) {
    console.log(
      `[agentcore-proxy] DEV_BYPASS_AUTH=true — JWT validation disabled, ` +
      `actorId=${devBypassActorId()}`
    );
  }

  // ---------------------------------------------------------------------------
  // /admin — Admin (gateway-admins) surface: user directory + role management.
  // Reuses the existing `gateway-admins` group + `actorGroups` (no new claim).
  // `/admin` is in authPaths, so authMiddleware has already populated
  // `actorGroups` here; `requireAdmin` then enforces group membership and
  // returns a real 403 (the distribution-wide CloudFront 403->index.html rewrite
  // was removed; API behaviors now surface true status codes). Staging/prod also
  // need a CloudFront behavior routing `/admin/*` -> the proxy ALB; local dev
  // hits the proxy directly so this is exercisable end-to-end without CloudFront.
  // ---------------------------------------------------------------------------
  const cognitoAdminClient = new CognitoIdentityProviderClient({ region: process.env.AWS_REGION || "us-west-2" });
  const adminUserPoolId = process.env.USER_POOL_ID || "";
  function requireAdmin(req: any, res: any): boolean {
    if (hasAdminGroup((req as AuthedRequest).actorGroups)) return true;
    res.writeHead(403, { "content-type": "application/json", "access-control-allow-origin": "*" });
    res.end(JSON.stringify({ error: { code: "forbidden", message: "Admin (gateway-admins) group required" } }));
    return false;
  }
  app.use("/admin", async (req: any, res: any) => {
    if (!requireAdmin(req, res)) return;
    const full = String(req.originalUrl || req.url || "");
    const sub = (full.split("?")[0].replace(/^\/admin/, "")) || "/";
    const url = new URL(full, "http://localhost");
    const method = String(req.method || "GET").toUpperCase();
    const json = (status: number, body: unknown) => {
      res.writeHead(status, { "content-type": "application/json", "access-control-allow-origin": "*" });
      res.end(JSON.stringify(body));
    };
    if (!adminUserPoolId) {
      json(200, { users: [], warning: "USER_POOL_ID not configured" });
      return;
    }
    try {
      // GET /admin/users — read-only user directory (D3).
      if (method === "GET" && (sub === "/users" || sub === "/users/")) {
        const limit = Math.min(Math.max(parseInt(url.searchParams.get("limit") ?? "60", 10) || 60, 1), 60);
        const [listed, adminMembers] = await Promise.all([
          cognitoAdminClient.send(new ListUsersCommand({ UserPoolId: adminUserPoolId, Limit: limit })),
          cognitoAdminClient.send(new ListUsersInGroupCommand({ UserPoolId: adminUserPoolId, GroupName: ADMIN_GROUP, Limit: 60 })),
        ]);
        const adminUsernames = new Set((adminMembers.Users ?? []).map((u: any) => u.Username));
        const users = (listed.Users ?? []).map((u: any) => {
          const attrs: Record<string, string | undefined> = Object.fromEntries(
            (u.Attributes ?? []).map((a: any) => [a.Name, a.Value]),
          );
          return {
            username: u.Username,
            sub: attrs.sub,
            email: attrs.email,
            status: u.UserStatus,
            enabled: u.Enabled,
            createdAt: u.UserCreateDate,
            admin: adminUsernames.has(u.Username),
          };
        });
        json(200, { users });
        return;
      }
      // POST /admin/users — create a user (Cognito emails an invite + temp password).
      if (method === "POST" && (sub === "/users" || sub === "/users/")) {
        const raw = await new Promise<string>((resolve) => {
          const chunks: Buffer[] = [];
          req.on("data", (c: Buffer) => chunks.push(c));
          req.on("end", () => resolve(Buffer.concat(chunks).toString() || "{}"));
          req.on("error", () => resolve("{}"));
        });
        let email = "";
        try { email = String(JSON.parse(raw)?.email ?? "").trim(); } catch { /* invalid JSON → empty */ }
        if (!email || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
          json(400, { error: { code: "bad_request", message: "a valid email is required" } });
          return;
        }
        let firstName = "", lastName = "";
        try {
          const parsed = JSON.parse(raw);
          firstName = String(parsed?.firstName ?? "").trim();
          lastName = String(parsed?.lastName ?? "").trim();
        } catch { /* already handled above */ }
        const attrs: { Name: string; Value: string }[] = [
          { Name: "email", Value: email },
          { Name: "email_verified", Value: "true" },
        ];
        if (firstName) attrs.push({ Name: "given_name", Value: firstName });
        if (lastName) attrs.push({ Name: "family_name", Value: lastName });
        const actor = (req as AuthedRequest).actorId ?? "unknown";
        await cognitoAdminClient.send(new AdminCreateUserCommand({
          UserPoolId: adminUserPoolId,
          Username: email,
          UserAttributes: attrs,
          DesiredDeliveryMediums: ["EMAIL"],
        }));
        console.log(
          `[admin-audit] actor=${actor} action=create_user target=${email} ts=${new Date().toISOString()}`,
        );
        json(200, { ok: true, username: email, created: true });
        return;
      }
      // POST|DELETE /admin/users/:username/admin — grant/revoke admin (D1).
      const m = sub.match(/^\/users\/([^/]+)\/admin\/?$/);
      if (m && (method === "POST" || method === "DELETE")) {
        const username = decodeURIComponent(m[1]);
        const actor = (req as AuthedRequest).actorId ?? "unknown";
        if (method === "POST") {
          await cognitoAdminClient.send(
            new AdminAddUserToGroupCommand({ UserPoolId: adminUserPoolId, Username: username, GroupName: ADMIN_GROUP }),
          );
        } else {
          await cognitoAdminClient.send(
            new AdminRemoveUserFromGroupCommand({ UserPoolId: adminUserPoolId, Username: username, GroupName: ADMIN_GROUP }),
          );
        }
        // Audit: every permission mutation is logged with actor + action + target.
        console.log(
          `[admin-audit] actor=${actor} action=${method === "POST" ? "grant_admin" : "revoke_admin"} ` +
          `target=${username} ts=${new Date().toISOString()}`,
        );
        json(200, { ok: true, username, admin: method === "POST" });
        return;
      }
      // POST /admin/users/:username/reset-password — trigger a Cognito password reset
      // (user must set a new password next sign-in). Useful for FORCE_CHANGE_PASSWORD /
      // locked-out users.
      const rp = sub.match(/^\/users\/([^/]+)\/reset-password\/?$/);
      if (rp && method === "POST") {
        const username = decodeURIComponent(rp[1]);
        const actor = (req as AuthedRequest).actorId ?? "unknown";
        await cognitoAdminClient.send(
          new AdminResetUserPasswordCommand({ UserPoolId: adminUserPoolId, Username: username }),
        );
        console.log(
          `[admin-audit] actor=${actor} action=reset_password target=${username} ts=${new Date().toISOString()}`,
        );
        json(200, { ok: true, username, reset: true });
        return;
      }
      // POST /admin/users/:username/disable | /enable — soft delete + undo (reversible;
      // preferred over hard delete). Cognito AdminDisableUser blocks sign-in;
      // AdminEnableUser restores it.
      const de = sub.match(/^\/users\/([^/]+)\/(disable|enable)\/?$/);
      if (de && method === "POST") {
        const username = decodeURIComponent(de[1]);
        const enable = de[2] === "enable";
        const actor = (req as AuthedRequest).actorId ?? "unknown";
        await cognitoAdminClient.send(enable
          ? new AdminEnableUserCommand({ UserPoolId: adminUserPoolId, Username: username })
          : new AdminDisableUserCommand({ UserPoolId: adminUserPoolId, Username: username }));
        console.log(
          `[admin-audit] actor=${actor} action=${enable ? "enable_user" : "disable_user"} ` +
          `target=${username} ts=${new Date().toISOString()}`,
        );
        json(200, { ok: true, username, enabled: enable });
        return;
      }
      // DELETE /admin/users/:username — permanently delete a Cognito user (destructive).
      // Matched AFTER /users/:u/admin so revoke isn't shadowed; the `[^/]+` stops at a
      // slash so this only matches the bare user path.
      const du = sub.match(/^\/users\/([^/]+)\/?$/);
      if (du && method === "DELETE") {
        const username = decodeURIComponent(du[1]);
        const actor = (req as AuthedRequest).actorId ?? "unknown";
        await cognitoAdminClient.send(
          new AdminDeleteUserCommand({ UserPoolId: adminUserPoolId, Username: username }),
        );
        console.log(
          `[admin-audit] actor=${actor} action=delete_user target=${username} ts=${new Date().toISOString()}`,
        );
        json(200, { ok: true, username, deleted: true });
        return;
      }
      json(404, { error: { code: "not_found", message: `unknown admin route: ${method} ${sub}` } });
    } catch (err: any) {
      console.error("[agentcore-proxy] /admin error:", err?.message ?? err);
      json(500, { error: { code: "admin_error", message: err?.message ?? "admin operation failed" } });
    }
  });

  // /upload — multipart file upload → S3
  const uploadBucket = process.env.UPLOAD_BUCKET ?? "";
  // Hard cap for browser uploads. The route streams to S3 (bounded memory), so this
  // protects the bucket/UX rather than the proxy's RAM; surfaced to the SPA via
  // /config so the client can reject an oversized file before starting. Default 2 GiB.
  const uploadMaxBytes = Number(process.env.UPLOAD_MAX_BYTES) || 2 * 1024 * 1024 * 1024;
  // M2C media-workflow output bucket, the second entry in the external download
  // route's bucket allowlist (`GET /invoke/:agentId/download?ref=`). Declared
  // here rather than inline at the mount site so the module still loads when the
  // var is unset — the allowlist just narrows to `uploadBucket`.
  const mediaProxyBucket = process.env.MEDIA_PROXY_BUCKET ?? "";
  const s3Client = new S3Client({ region: REGION });

  // Sessions DynamoDB
  const sessionsTable = process.env.SESSIONS_TABLE ?? "";
  const ddbClient = sessionsTable
    ? DynamoDBDocumentClient.from(new DynamoDBClient({ region: REGION }))
    : null;
  if (sessionsTable) {
    console.log(`[agentcore-proxy] Sessions table: ${sessionsTable}`);
  }

  if (uploadBucket) {
    console.log(`[agentcore-proxy] Upload bucket: ${uploadBucket}`);

    app.use("/upload", async (req, res) => {
      if (req.method !== "POST") {
        res.writeHead(405, { "content-type": "application/json" });
        res.end(JSON.stringify({ error: "Method not allowed" }));
        return;
      }

      try {
        const contentType = req.headers["content-type"] ?? "";
        if (!contentType.includes("multipart/form-data")) {
          res.writeHead(400, { "content-type": "application/json" });
          res.end(JSON.stringify({ error: "Content-Type must be multipart/form-data" }));
          return;
        }

        const { filename, key, size } = await new Promise<{
          filename: string; key: string; size: number;
        }>((resolve, reject) => {
          const bb = Busboy({ headers: req.headers as Record<string, string> });
          let sid = "";
          let sawFile = false;
          // Resolves to the session id ONLY if the authenticated actor owns it, else "".
          // Kicked off on the `field` event (which the SPA sends before the file part) so the
          // DynamoDB read overlaps the file transfer instead of adding latency.
          let ownedSidCheck: Promise<string> | null = null;

          const resolveOwnedSession = async (candidate: string): Promise<string> => {
            const actor = (req as { actorId?: string }).actorId;
            if (!candidate || !actor || !ddbClient || !sessionsTable) return "";
            try {
              const got = await ddbClient.send(new GetCommand({
                TableName: sessionsTable,
                Key: { sessionId: candidate },
                ProjectionExpression: "actorId",
              }));
              // Unknown session (not yet persisted — sessions are created lazily) or a session
              // owned by SOMEONE ELSE both fall back to the session-less prefix. Fail closed:
              // never place an object inside another actor's session prefix on their say-so.
              return got.Item?.actorId === actor ? candidate : "";
            } catch {
              return "";
            }
          };

          // The SPA appends `session_id` BEFORE the file part, so this field is set by
          // the time the file handler runs and can scope the object's key. Accept either
          // spelling — `session_id` (matches the agent field) or `sessionId`.
          bb.on("field", (name: string, value: string) => {
            if (name === "session_id" || name === "sessionId") {
              sid = value || "";
              ownedSidCheck = resolveOwnedSession(sid);
            }
          });

          bb.on("file", (_fieldname, file, info: { filename: string }) => {
            sawFile = true;
            const fname = info.filename || "upload";
            // `artifacts/<session>/input/<ts>-<name>` — see uploadKey.ts for why the path
            // (not the `s3://` scheme) is what marks an artifact as a source.
            //
            // SECURITY: `sid` arrives in the request body, so it is ATTACKER-CONTROLLED. It is
            // only honoured once the ownership check above confirms the authenticated actor owns
            // that session; otherwise it is discarded and the object lands under the session-less
            // prefix. Without this, any caller could place content inside another user's session
            // prefix (and before the authPaths fix, without even being signed in).
            //
            // Awaiting here is safe: nothing has read `file` yet, so it is still paused and
            // busboy back-pressures the request socket — no chunk can be dropped.
            void (async () => {
              let ownedSid = "";
              try {
                ownedSid = ownedSidCheck ? await ownedSidCheck : "";
              } catch {
                ownedSid = "";
              }
              // A session-less upload is tagged with its uploader, so only they can read it back.
              const uploader = (req as { actorId?: string }).actorId;
              const key = uploadKeyFor(ownedSid, fname, Date.now(), uploader ? artifactOwnerTag(uploader) : undefined);
              // Stream straight to S3 (bounded memory) rather than buffering the whole file.
              streamUploadToS3(s3Client, file as unknown as import("stream").Readable, {
                bucket: uploadBucket,
                key,
                contentType: guessMimeType(fname),
                maxBytes: uploadMaxBytes,
              })
                .then((size) => resolve({ filename: fname, key, size }))
                .catch((err) => {
                  // Stop pulling the rest of the (possibly huge) request body once we've
                  // decided to fail, and drain what's buffered so the socket closes cleanly.
                  try { req.unpipe(bb); } catch { /* noop */ }
                  try { req.resume(); } catch { /* noop */ }
                  reject(err);
                });
            })();
          });

          bb.on("error", reject);
          bb.on("finish", () => { if (!sawFile) reject(new Error("No file received")); });
          req.pipe(bb);
        });

        // Permanent public URL: uploads land under artifacts/, which is served by the
        // ArtifactsDomain CloudFront distribution (OAC). This is the form external
        // generators (e.g. fal.ai image_url) need — they cannot fetch s3:// and a
        // presigned URL expires in an hour. Surfaced so the agent gets a directly-usable
        // reference for a user-uploaded image instead of an s3:// it cannot read.
        const artifactsDomain = process.env.ARTIFACTS_DOMAIN || "";
        const cloudfrontUrl = artifactsDomain ? `https://${artifactsDomain}/${key}` : "";

        // Presign ONLY when there is no CloudFront domain to serve the object (local dev).
        // For a CloudFront-public artifact a presigned URL is strictly worse: it expires,
        // bypasses the CDN, and its query params (`X-Amz-Signature`, `X-Amz-Credential`)
        // match redaction's credential-SHAPE patterns, so external output can corrupt it
        // into an S3 `AuthorizationQueryParametersError`. The caller already prefers
        // `cloudfrontUrl`, so in deployed envs this presign was pure redundant surface.
        const presignedUrl = cloudfrontUrl
          ? ""
          : await getSignedUrl(s3Client, new GetObjectCommand({
              Bucket: uploadBucket,
              Key: key,
            }), { expiresIn: 3600 });

        res.writeHead(200, { "content-type": "application/json", "access-control-allow-origin": "*" });
        res.end(JSON.stringify({
          key,
          size,
          bucket: uploadBucket,
          url: presignedUrl,
          cloudfrontUrl,
          filename,
        }));
      } catch (err: any) {
        // Oversized upload: the stream aborted the multipart (no partial object), so
        // answer a clean 413 the client can surface inline rather than a generic 500.
        if (err instanceof FileTooLargeError || err?.code === "FILE_TOO_LARGE") {
          const maxBytes = err?.maxBytes ?? uploadMaxBytes;
          res.writeHead(413, { "content-type": "application/json", "access-control-allow-origin": "*" });
          res.end(JSON.stringify({
            error: `File exceeds the maximum upload size of ${(maxBytes / (1024 * 1024 * 1024)).toFixed(2)} GB`,
            code: "FILE_TOO_LARGE",
            maxBytes,
          }));
          return;
        }
        console.error("[agentcore-proxy] Upload error:", err.message);
        res.writeHead(500, { "content-type": "application/json" });
        res.end(JSON.stringify({ error: err.message }));
      }
    });
  } else {
    console.warn("[agentcore-proxy] UPLOAD_BUCKET not set — /upload disabled");
    app.use("/upload", (_req: any, res: any) => {
      res.writeHead(500, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: "UPLOAD_BUCKET not configured" }));
    });
  }

  // Session-row ownership for the artifact-key policy (shared by `/api/artifact-urls` and the gated
  // `/api/artifact`). Tri-state: a lookup ERROR propagates (the policy fails it closed as
  // `not_found`); no sessions table configured (local dev) means no row can exist → `unknown`.
  const lookupSessionOwnership = async (sessionId: string, actorId: string): Promise<SessionOwnership> => {
    if (!ddbClient || !sessionsTable) return "unknown";
    try {
      const got = await ddbClient.send(new GetCommand({ TableName: sessionsTable, Key: { sessionId } }));
      const owner = got.Item?.actorId;
      if (typeof owner !== "string" || !owner) return "unknown";
      return owner === actorId ? "owned" : "other-owner";
    } catch (e) {
      console.error("[artifact-urls] ownership lookup failed:", (e as Error)?.message ?? e);
      throw e;
    }
  };

  // Authorization for an M2C_REFERENCE (`artifacts/m2c/<bucket>/<key>`, asset-lineage D4/R4): the
  // location must be registered as an asset of a project this actor can read. Built once; reads
  // the projects table and applies the same `checkAccess` as `POST /projects/:pid/assets/urls`.
  const authorizeM2cRef: AuthorizeM2cRef = envAuthorizeM2cRef();
  // An UNATTRIBUTED artifacts key (flat, legacy untagged upload, a session with no owner row) is
  // readable only when a project the actor can read registered that location (artifactUrlsRoute.ts).
  const authorizeUnattributed: AuthorizeUnattributed = envAuthorizeUnattributed(uploadBucket);
  // Project membership for a projects-session OUTPUT key (asset-lineage Phase 3 (a), D3): the same
  // `checkAccess` as `POST /projects/:pid/assets/urls`.
  const checkProjectMember: CheckProjectMember = envCheckProjectMember();

  // POST /api/artifact-urls — authenticated resolve of artifact KEY refs to short-lived presigned
  // URLs (the "no broken links" media backbone). Behind the `/api` auth middleware (NOT a
  // PUBLIC_PATH — the boundary match exempts only `/api/artifact`), so `req.actorId` is set here.
  // Registered BEFORE `/api/artifact`; Express won't let that mount capture this (`-` is not a path
  // separator). Each key is authorized by `authorizeArtifactKey` (artifactUrlsRoute.ts).
  app.use("/api/artifact-urls", async (req: any, res: any) => {
    if (req.method !== "POST") {
      res.writeHead(405, { "content-type": "application/json", "access-control-allow-origin": "*" });
      res.end(JSON.stringify({ error: "method not allowed" }));
      return;
    }
    await handleArtifactUrls(req, res, {
      bucket: uploadBucket,
      presignGet: defaultPresignGet(REGION),
      lookupSessionOwnership,
      viewSecret: artifactViewSecret,
      m2cBucket: mediaProxyBucket,
      authorizeM2c: authorizeM2cRef,
      checkProjectMember,
      authorizeUnattributed,
    });
  });

  // /api/artifact — proxy S3 artifact content with proper content-type
  // ---------------------------------------------------------------------------
  app.use("/api/artifact", async (req: any, res: any) => {
    const url = new URL(req.url || "/", "http://localhost");
    let key = url.searchParams.get("key");
    if (!key || !uploadBucket) {
      res.writeHead(400, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: "Missing key parameter or bucket not configured" }));
      return;
    }
    // Agent runtime sometimes doubles the artifacts/ prefix in presigned URLs
    if (key.startsWith("artifacts/artifacts/")) {
      key = key.replace("artifacts/artifacts/", "artifacts/");
    }
    // Restrict strictly to artifact content so this can never be used to read arbitrary bucket keys.
    // Public (no auth) unless ARTIFACTS_REQUIRE_AUTH=true; see the ownership check below.
    // A projects-session OUTPUT key (asset-lineage Phase 3 (a)) is the one other servable form.
    const projectSessionKey = isProjectSessionOutputKey(key);
    if (!key.startsWith("artifacts/") && !projectSessionKey) {
      // 400, NOT 403. The UI CloudFront distribution maps 403 -> 200 /index.html and
      // 404 -> 200 /index.html for EVERY behaviour (CustomErrorResponses is distribution-level,
      // not per-behaviour) and caches it 300s. A 403 here therefore reaches the caller as a 200
      // serving the SPA's own HTML -- verified live on staging. An <iframe> pointed at it renders
      // the harness inside itself, which is exactly the reported "the document link is wrong and
      // the render ends up rendering the harness UI" symptom (session ee8b405b). Client-side
      // hardening cannot catch it: the URL is well-formed, it is the RESPONSE that lies.
      // 400 is both semantically right (we refuse the request) and untouched by the error map.
      res.writeHead(400, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: "key must be under artifacts/" }));
      return;
    }
    // An M2C_REFERENCE (`artifacts/m2c/<bucket>/<key>`) names an object in the M2C proxy bucket
    // that was never copied here (asset-lineage D4/R4), so it is read from THAT bucket and is
    // ALWAYS authenticated: `isPublicPath` keeps it out of the public exemption, so the auth
    // middleware has run and `req.actorId` is the verified caller. A refusal is the same 400 as
    // below (never 403/404 — the CloudFront error map rewrites those).
    const m2cRef = parseM2cArtifactKey(key);
    let viewer: string | undefined = req.actorId;
    if (m2cRef) {
      // A framed M2C document (the document viewer) carries a view ticket for its one key.
      if (!viewer && url.searchParams.has("view")) {
        viewer = verifyViewTicket(url.searchParams.get("view"), normalizeArtifactKey(key), artifactViewSecret) ?? undefined;
        if (!viewer) {
          send401(res, "invalid_view_ticket", "This document link has expired. Reopen it from the app.");
          return;
        }
      }
      if (!viewer) {
        send401(res, "missing_token", MISSING_TOKEN_MESSAGE);
        return;
      }
      if (!mediaProxyBucket || m2cRef.bucket !== mediaProxyBucket
          || (await authorizeM2cRef(m2cRef, viewer)) !== "allow") {
        res.writeHead(400, { "content-type": "application/json" });
        res.end(JSON.stringify({ error: "artifact key is not readable by this caller" }));
        return;
      }
    } else if (artifactsRequireAuth || projectSessionKey) {
      // Gated mode (ARTIFACTS_REQUIRE_AUTH=true): the caller is the signed-in user (`req.actorId`,
      // set by the auth middleware) OR the viewer a valid view ticket was minted for — a ticket
      // counts for its one key only. Then the shared artifact-key policy applies either way. A
      // refusal is 400 (never 403/404 — the same CloudFront error-map reason as above), and
      // `not_found` and `not_resolvable` answer identically so a non-owner cannot tell whether
      // another actor's session or object exists.
      if (!viewer && url.searchParams.has("view")) {
        viewer = verifyViewTicket(url.searchParams.get("view"), normalizeArtifactKey(key), artifactViewSecret) ?? undefined;
        if (!viewer) {
          send401(res, "invalid_view_ticket", "This document link has expired. Reopen it from the app.");
          return;
        }
      }
      if (!viewer) {
        send401(res, "missing_token", MISSING_TOKEN_MESSAGE);
        return;
      }
      const access = await authorizeArtifactKey(
        key, viewer, lookupSessionOwnership, undefined, undefined, checkProjectMember, authorizeUnattributed);
      if (access !== "allow") {
        res.writeHead(400, { "content-type": "application/json" });
        res.end(JSON.stringify({ error: "artifact key is not readable by this caller" }));
        return;
      }
    }
    // The object this request actually reads: the M2C proxy bucket for a reference, ours otherwise.
    const readBucket = m2cRef ? m2cRef.bucket : uploadBucket;
    const readKey = m2cRef ? m2cRef.key : key;
    const asDownload = url.searchParams.get("download") === "1";
    const rangeHeader: string | undefined = req.headers["range"];
    try {
      const obj = await s3Client.send(new GetObjectCommand({
        Bucket: readBucket,
        Key: readKey,
        // Forward a browser byte-range request straight to S3 (same `bytes=...`
        // grammar). Without this the proxy always returned the whole object (200),
        // so a media player could not seek/scrub and — for a non-faststart MP4
        // whose `moov` atom sits at the END — could not fetch that trailing index
        // to start playback at all. See the faststart discussion (session f485d134).
        ...(rangeHeader ? { Range: rangeHeader } : {}),
      }));
      const s3Type = obj.ContentType;
      // The stored type is WRITER-controlled, so it is an input to a policy, not a header to echo:
      // see artifacts/servePolicy.ts (nosniff + sandbox CSP on everything but PDF, executable and
      // malformed types downgraded to an octet-stream attachment, unpreviewed types attached).
      const policy = artifactServePolicy(
        (s3Type && !s3Type.includes('octet-stream')) ? s3Type : guessMimeType(readKey), asDownload);
      const contentType = policy.contentType;
      if (!policy.headers["content-security-policy"]) res.removeHeader("content-security-policy");
      const baseHeaders: Record<string, string> = {
        "content-type": contentType,
        "content-disposition": `${policy.disposition}; filename="${safeDispositionFilename(readKey)}"`,
        "access-control-allow-origin": "*",
        // Advertise range support so browsers issue byte-range requests for media
        // (seeking, and fetching a non-faststart MP4's trailing moov atom).
        "accept-ranges": "bytes",
        // Revalidate on every use: artifacts can be MUTATED in place (e.g.
        // "add a cover page" rewrites the same S3 key). With a long max-age
        // the browser served the stale copy after a re-render. `no-cache`
        // forces a re-fetch; since we don't emit an ETag it always returns
        // the current object.
        "cache-control": "no-cache, max-age=0",
        // Agent-written documents are served from THIS origin, so without isolation their scripts
        // would run as the app: read the signed-in user's tokens, call every API. `sandbox` gives
        // them an opaque origin even when opened in their own tab; `nosniff` pins the type.
        ...policy.headers,
      };
      const contentLength = obj.ContentLength != null ? { "content-length": String(obj.ContentLength) } : {};
      // An HTML artifact is RE-SIGNED before it is sent, so it must be handled before any
      // header goes out (see artifacts/htmlArtifactRewrite.ts). A saved deliverable (comic
      // storyboard, report) has presigned URLs BAKED IN by the code_interpreter input
      // presign, so every image 403s once they expire ~6h later (session b42731df). The
      // browser fetches those nested URLs itself, so the SPA's re-sign-on-error path never
      // sees them — the only place to fix it is the bytes we hand out. Re-signing per view
      // is also what makes "reopen/refresh the tab" the cure.
      // Skipped for a range request (a partial document cannot be rewritten), for
      // ?download=1 (a download should be the stored bytes), and for an M2C_REFERENCE (an
      // M2C output holds no artifact keys, and the rewrite signs OUR bucket).
      if (obj.Body && !m2cRef && !rangeHeader && !asDownload && isRewritableHtml(key, s3Type, obj.ContentLength)) {
        const raw = Buffer.from(await (obj.Body as any).transformToByteArray()).toString("utf8");
        const presign = defaultPresignGet(REGION);
        // Gated mode: each embedded key is authorized for THIS caller exactly as a direct read
        // would be (one ownership lookup per session, shared across the document's keys).
        // Ungated (artifactsRequireAuth off) every artifact is already public on the CDN, so
        // re-signing grants nothing a viewer could not already fetch.
        const ownershipCache = new Map<string, Awaited<ReturnType<typeof authorizeArtifactKey>>>();
        const html = await resignHtmlArtifact(raw, uploadBucket, {
          // A page linking to another page gets a view ticket for it, like the SPA would; a raw
          // presigned URL would serve that page's stored, expiring links and skip the sandbox.
          presign: async (k) =>
            viewer && isViewedDocumentKey(k)
              ? documentViewUrl(k, mintViewTicket(k, viewer, artifactViewSecret).ticket)
              : presign(uploadBucket, k, ARTIFACT_URL_TTL_S),
          authorize: async (k) =>
            (!artifactsRequireAuth && !isProjectSessionOutputKey(k)) ||
            (!!viewer && (await authorizeArtifactKey(
              k, viewer, lookupSessionOwnership, ownershipCache, authorizeM2cRef, checkProjectMember,
              authorizeUnattributed)) === "allow"),
          warn: (m) => console.warn(`[agentcore-proxy] ${m}`),
        });
        const htmlBody = Buffer.from(html, "utf8");
        res.writeHead(200, { ...baseHeaders, "content-length": String(htmlBody.length) });
        res.end(htmlBody);
        return;
      }
      // When the client asked for a byte range, S3 answers with the partial slice
      // plus a ContentRange; mirror that as `206 Partial Content` so the browser's
      // media stack treats the stream as seekable.
      if (rangeHeader && obj.ContentRange) {
        res.writeHead(206, { ...baseHeaders, ...contentLength, "content-range": obj.ContentRange });
      } else {
        res.writeHead(200, { ...baseHeaders, ...contentLength });
      }
      // Stream body
      if (obj.Body) {
        const stream = obj.Body as any;
        if (typeof stream.pipe === 'function') {
          stream.pipe(res);
        } else {
          const bytes = await obj.Body.transformToByteArray();
          res.end(Buffer.from(bytes));
        }
      } else {
        res.end();
      }
    } catch (err: any) {
      // Unsatisfiable range → 416 (semantically correct; the media stack retries
      // with a valid range rather than treating it as a hard failure).
      if (err?.name === "InvalidRange" || err?.$metadata?.httpStatusCode === 416) {
        res.writeHead(416, { "content-type": "application/json", "accept-ranges": "bytes" });
        res.end(JSON.stringify({ error: "Requested range not satisfiable" }));
        return;
      }
      console.error("[agentcore-proxy] Artifact download error:", err.message);
      if (err.name === "NoSuchKey" || err.$metadata?.httpStatusCode === 404) {
        // 410, NOT 404 -- same CloudFront error-map reason as the 400 above. A missing artifact
        // returned as 404 reaches the browser as `200 text/html` (the SPA), so a document viewer
        // silently embeds the harness instead of reporting the miss.
        res.writeHead(410, { "content-type": "application/json" });
        res.end(JSON.stringify({ error: "Artifact not found" }));
      } else {
        res.writeHead(500, { "content-type": "application/json" });
        res.end(JSON.stringify({ error: err.message }));
      }
    }
  });

  // NOTE: S3-based /sessions endpoint removed — session persistence now uses AgentCore Memory via /memory/*

  // /config — expose runtime configuration to the frontend. Cognito settings
  // are surfaced here (rather than baked into the Vite build as VITE_* vars)
  // so the same ui/dist artifact works against any environment's User
  // Pool — the SPA fetches its own auth config at load time instead of the
  // CI pipeline needing to hardcode or inject per-environment IDs.
  app.use("/config", (_req: any, res: any) => {
    res.writeHead(200, { "content-type": "application/json", "access-control-allow-origin": "*" });
    res.end(JSON.stringify({
      artifactsDomain: process.env.ARTIFACTS_DOMAIN || "",
      region: REGION,
      userPoolId: process.env.USER_POOL_ID || "",
      userPoolClientId: process.env.USER_POOL_CLIENT_ID || "",
      userPoolDomain: process.env.USER_POOL_DOMAIN || "",
      logoKey: process.env.LOGO_KEY || "default",
      brandName: process.env.BRAND_NAME || "",
      maxUploadBytes: uploadMaxBytes,
      // False hides the Plus_Menu and the `Projects` nav entry (projects-access D7).
      projectsEnabled: projectsEnabled(),
      // True where the knowledge-graph stack is deployed: shows the create-page switch and the Graph tab.
      graphEnabled: projectsEnabled() && graphFeatureEnabled(),
      // True where AgentCore Web Search is deployed (env-config `webSearch.enabled`): the + menu offers the
      // per-chat Web search toggle; elsewhere it is shown unavailable and every turn runs without it.
      webSearchAvailable: projectsEnabled() && webSearchFeatureEnabled(),
      // True where asset lineage edges are on (env-config `projects.lineageEdges`): the Projects Lineage tab and the
      // asset's Lineage action, both reading `POST /projects/:pid/assets/lineage`.
      lineageEnabled: projectsEnabled() && lineageFeatureEnabled(),
      // True where the artifacts CDN is private (`artifactsCdnPrivate`): `/api/artifact` needs a
      // token, so login-free surfaces (the /render-view deep link) must sign in first.
      artifactsRequireAuth: process.env.ARTIFACTS_REQUIRE_AUTH === "true",
      // The release this env runs (CDK stamps APP_VERSION from VERSION; scripts/release/version.sh).
      appVersion: process.env.APP_VERSION || "dev",
    }));
  });

  // /sessions — DynamoDB session metadata CRUD.
  //
  // The actor id is sourced per-request from the auth middleware
  // (`req.actorId`), which derives it from the validated JWT `sub` (or,
  // when `DEV_BYPASS_AUTH=true`, from `LAB_DEFAULT_ACTOR_ID`). The sidebar
  // list, the AgentCore Memory store, and the runtime's resolved actor_id
  // all key on the caller's identity.
  if (ddbClient && sessionsTable) {
    app.use("/sessions", async (req: any, res: any) => {
      const sessionsActorId: string | undefined = req.actorId;
      if (!sessionsActorId) {
        // Defense in depth — auth middleware should have already returned 401.
        send401(res, "missing_actor", "No actor id resolved for request");
        return;
      }
      const url = new URL(req.url || "/", "http://localhost");
      const pathParts = url.pathname.split("/").filter(Boolean);
      const sessionIdParam = pathParts[0];

      try {
        // GET /sessions — list active sessions for actor
        if (req.method === "GET" && !sessionIdParam) {
          // Session rows are large (renderHistory + transcript can push a row near the 1 MB
          // item ceiling), and the GSI projects ALL attributes. A single Query returns at most
          // 1 MB PER PAGE — applied BEFORE the `active=1` filter — so an un-paginated query read
          // only ~1-3 rows and then filtered, making the sidebar show a near-random handful of
          // active sessions (and a different count on each refresh as lastActivityAt reordered
          // the index). Two fixes together:
          //   1. Paginate over LastEvaluatedKey so EVERY active session is collected.
          //   2. Project ONLY the lean list fields (not renderHistory/transcript) so each row
          //      read is small, far more rows fit per page, and the response payload is tiny.
          // The client uses list renderHistory only as a cache; the S3 render manifests
          // (GET /sessions/:id/renders) remain authoritative, so dropping it here is safe.
          const sessions: Record<string, unknown>[] = [];
          let startKey: Record<string, unknown> | undefined = undefined;
          let pages = 0;
          do {
            const result = await ddbClient.send(new QueryCommand({
              TableName: sessionsTable,
              IndexName: "active-lastActivity-index",
              KeyConditionExpression: "actorId = :actor",
              FilterExpression: "active = :active",
              // `activeProjectId` / `webSearch` (Projects) let a reopened session restore both values
              // (Req 3.6). DynamoDB omits an attribute the row lacks, so pre-Projects rows are unchanged.
              ProjectionExpression: "sessionId, title, active, createdAt, lastActivityAt, #locked, activeProjectId, webSearch",
              ExpressionAttributeNames: { "#locked": "locked" },
              ExpressionAttributeValues: { ":actor": sessionsActorId, ":active": 1 },
              ScanIndexForward: false,
              ExclusiveStartKey: startKey,
            }));
            for (const it of result.Items ?? []) sessions.push(it);
            startKey = result.LastEvaluatedKey as Record<string, unknown> | undefined;
            pages += 1;
          } while (startKey && pages < 500);
          res.writeHead(200, { "content-type": "application/json", "access-control-allow-origin": "*" });
          res.end(JSON.stringify({ sessions }));
          return;
        }

        // POST /sessions — create session row (called on first message)
        if (req.method === "POST" && !sessionIdParam) {
          const chunks: Buffer[] = [];
          req.on("data", (c: Buffer) => chunks.push(c));
          await new Promise<void>((resolve) => req.on("end", resolve));
          const body = JSON.parse(Buffer.concat(chunks).toString());
          const { sessionId, title } = body;
          if (!sessionId) {
            res.writeHead(400, { "content-type": "application/json" });
            res.end(JSON.stringify({ error: "Missing sessionId" }));
            return;
          }
          const nowDate = new Date();
          const now = nowDate.toISOString();
          // Projects (when enabled): the Personal_Project as Active_Project, written in this one
          // conditional PutItem. `null` when Projects are disabled, leaving the row exactly as before.
          const projectFields = await sessionCreateFields(sessionsActorId, nowDate.getTime());
          await ddbClient.send(new PutCommand({
            TableName: sessionsTable,
            Item: {
              sessionId,
              actorId: sessionsActorId,
              title: title || "New Session",
              active: 1,
              createdAt: now,
              lastActivityAt: now,
              ...(projectFields ?? {}),
            },
            ConditionExpression: "attribute_not_exists(sessionId)",
          }));
          res.writeHead(201, { "content-type": "application/json", "access-control-allow-origin": "*" });
          res.end(JSON.stringify({ sessionId, createdAt: now }));
          return;
        }

        // GET /sessions/:id/renders — the AUTHORITATIVE list of renders for a session.
        //
        // Renders were only ever visible if a browser happened to be connected when one
        // completed: the Render Agent wrote its manifest to S3, pushed the key onto the
        // streaming channel, and the UI turned that into a `renderHistory` entry it wrote
        // back. So `renderHistory` was a client-side cache masquerading as the record, and
        // every render that finished AFTER a turn's stream closed was computed correctly and
        // then dropped. That is the normal case for this system, because media jobs settle
        // asynchronously minutes later -- one live session had 4 manifests in S3 and 2 in
        // renderHistory, another had 3 and 0.
        //
        // The manifests in S3 are already the source of truth. This endpoint reads them, so
        // the UI can stop maintaining a duplicate store and background renders appear on
        // their own. The stream path stays as a latency optimisation, not the only delivery.
        if (req.method === "GET" && sessionIdParam && pathParts[1] === "renders") {
          if (!uploadBucket) {
            res.writeHead(200, { "content-type": "application/json", "access-control-allow-origin": "*" });
            res.end(JSON.stringify({ renders: [] }));
            return;
          }
          // Ownership gate (parity with /transcript below). Without it, ANY authenticated actor
          // could list another actor's render manifests by guessing a session id — and those
          // manifests embed the session's media URLs/keys. Read the session row and require
          // `actorId === sessionsActorId`; a non-owner (or an unknown/orphaned row with no actorId)
          // gets an empty list, exactly as /transcript returns an empty transcript. Best-effort: a
          // row lookup failure must not 500 the list, but it also must not serve — treat it as not
          // owned.
          let rendersOwned = false;
          try {
            const ownRow = await ddbClient.send(new GetCommand({
              TableName: sessionsTable,
              Key: { sessionId: sessionIdParam },
            }));
            rendersOwned = !!ownRow.Item && ownRow.Item.actorId === sessionsActorId;
          } catch (e) {
            console.error(`[agentcore-proxy] /sessions/${sessionIdParam}/renders ownership lookup failed:`, e);
            rendersOwned = false;
          }
          if (!rendersOwned) {
            res.writeHead(200, { "content-type": "application/json", "access-control-allow-origin": "*" });
            res.end(JSON.stringify({ sessionId: sessionIdParam, renders: [] }));
            return;
          }
          const prefix = `artifacts/${sessionIdParam}/render/`;
          const listed = await s3Client.send(new ListObjectsV2Command({
            Bucket: uploadBucket,
            Prefix: prefix,
            MaxKeys: 200,
          }));
          const objects = (listed.Contents ?? [])
            // `render/_errors/` holds failure records kept for debugging; they are never tabs.
            .filter((o) => (o.Key ?? "").endsWith(".json") && !(o.Key ?? "").includes("/render/_errors/"))
            .sort((a, b) => (a.LastModified?.getTime() ?? 0) - (b.LastModified?.getTime() ?? 0));
          // Bodies are returned inline: a manifest is small, and one round trip per tab
          // would reintroduce a partial-load failure mode of its own. Fetched in PARALLEL — a
          // serial loop made the results->tab step scale with the render count (N sequential S3
          // GETs), which is exactly the "why does the tab take so long to appear" latency.
          const settled = await Promise.all(objects.map(async (o) => {
            const key = o.Key as string;
            try {
              const obj = await s3Client.send(new GetObjectCommand({ Bucket: uploadBucket, Key: key }));
              const body = await obj.Body?.transformToString();
              const manifest = body ? JSON.parse(body) : null;
              if (!manifest) return null;
              // A manifest carries `renderSkill` but no tab label -- the label was only
              // ever passed through the stream. Derive one so a restored tab is named after
              // the work ("Shot Segmentation") instead of falling back to "Render Output".
              const skill = String(manifest.renderSkill ?? "").split(",")[0].trim();
              const fromSkill = skill
                .replace(/^render[-_]/, "")
                .split(/[-_\s]+/)
                .filter(Boolean)
                .map((w: string) => w.charAt(0).toUpperCase() + w.slice(1))
                .join(" ");
              // A manifest that names itself (an `asset-player` tab from show_asset is titled with the file's
              // Display_Name) keeps that name; it carries no renderSkill, so it was restored as "Render Output".
              const label = fromSkill || (typeof manifest.title === "string" ? manifest.title.trim() : "");
              // Order by the manifest's OWN creation time when it has one (never later than the
              // object's LastModified): a viewState PATCH rewrites the object (new LastModified), and
              // a tab must not jump to the end of the strip because the user moved its range.
              const lastModified = (o.LastModified ?? new Date()).getTime();
              const ownCreated = typeof manifest.createdAt === "string" ? Date.parse(manifest.createdAt) : NaN;
              const createdAt = new Date(
                Number.isFinite(ownCreated) && ownCreated > 0 ? Math.min(ownCreated, lastModified) : lastModified,
              ).toISOString();
              return {
                key,
                manifestId: key.split("/").pop()?.replace(/\.json$/, "") ?? key,
                createdAt,
                label: label || undefined,
                manifest,
              };
            } catch {
              // A manifest that cannot be read must not hide the ones that can.
              return null;
            }
          }));
          const renders = settled
            .filter((r): r is NonNullable<typeof r> => r !== null)
            // Oldest first by each manifest's own time (see above), so a PATCHed view keeps its place
            // in a composite's member order as well as in the tab strip.
            .sort((a, b) => (a.createdAt < b.createdAt ? -1 : a.createdAt > b.createdAt ? 1 : 0));
          // Compose composites SERVER-SIDE so the UI renders finished composites and NEVER merges
          // client-side. Two collapses (see composeRenderTabs): (1) timeline manifests sharing a
          // sourceKey → ONE per-source composite (video + its analyses as lanes); (2) all pure
          // generated images in the session → ONE "Generated Images" gallery (so an N-panel
          // storyboard is one tab, not N). Analysis frame galleries (face/shot: boxes and/or a
          // non-render-image skill) stay solo — session 7cf83ef3, where image galleries sharing a
          // sourceKey were wrongly composited into an empty timeline. Server is the SOLE author of tabs.
          const composed = composeRenderTabs(renders);
          res.writeHead(200, { "content-type": "application/json", "access-control-allow-origin": "*" });
          res.end(JSON.stringify({ sessionId: sessionIdParam, renders: composed }));
          return;
        }

        // GET /sessions/:id/transcript — the durable rich chat transcript (per-message
        // steps + execution summary). Proxy-authoritative (docs/design-proxy-authoritative-transcript.md):
        // the FULL record lives in S3 (artifacts/<id>/transcript.json), so read it FIRST and fall
        // back to the row's capped tail only for sessions written before the proxy became the
        // writer. Actor-scoped either way: the ownership check on the row still gates the read, so a
        // caller only ever reads their own session's transcript.
        if (req.method === "GET" && sessionIdParam && pathParts[1] === "transcript") {
          const got = await ddbClient.send(new GetCommand({
            TableName: sessionsTable,
            Key: { sessionId: sessionIdParam },
          }));
          const item = got.Item;
          const owned = !!item && item.actorId === sessionsActorId;
          let transcript: unknown[] = [];
          if (owned) {
            const fromS3 = uploadBucket
              ? await readTranscriptFromS3(s3Client, uploadBucket, sessionIdParam)
              : null;
            transcript = fromS3 ?? (Array.isArray(item.transcript) ? item.transcript : []);
          }
          res.writeHead(200, { "content-type": "application/json", "access-control-allow-origin": "*" });
          res.end(JSON.stringify({ sessionId: sessionIdParam, transcript }));
          return;
        }

        // ---- Render-store writes: POST / PATCH / DELETE /sessions/:id/renders ----
        //
        // The S3 manifests under artifacts/<id>/render/ are the AUTHORITATIVE render list (the GET
        // above lists them), so every tab mutation is a write to that prefix. All three share one
        // gate: the session row must exist, be live, and belong to the caller (a lookup failure
        // fails CLOSED — the uniform 404), and a locked session (full freeze) answers 423. Keys are
        // constrained to THIS session's render prefix (artifacts/<id>/render/<name>.json, no
        // traversal) so a caller can never touch arbitrary objects.
        if (sessionIdParam && pathParts[1] === "renders" && pathParts.length === 2
            && (req.method === "POST" || req.method === "PATCH" || req.method === "DELETE")) {
          const sendJson = (status: number, payload: unknown) => {
            res.writeHead(status, { "content-type": "application/json", "access-control-allow-origin": "*" });
            res.end(JSON.stringify(payload));
          };
          if (!isSafeSessionId(sessionIdParam)) {
            sendJson(400, { error: "invalid session id" });
            return;
          }
          const renderStoreGate = async (): Promise<"ok" | "not_found" | "locked"> => {
            try {
              const snap = await ddbClient.send(new GetCommand({
                TableName: sessionsTable,
                Key: { sessionId: sessionIdParam },
                ProjectionExpression: "#actor, #locked, #active",
                ExpressionAttributeNames: { "#actor": "actorId", "#locked": "locked", "#active": "active" },
              }));
              const item = snap.Item;
              if (!item || item.actorId !== sessionsActorId || item.active === 0) return "not_found";
              return item.locked ? "locked" : "ok";
            } catch (e) {
              console.error(`[agentcore-proxy] /sessions/${sessionIdParam}/renders gate lookup failed:`, e);
              return "not_found";
            }
          };
          const passGate = async (): Promise<boolean> => {
            const gate = await renderStoreGate();
            if (gate === "not_found") { sendSessionNotFound(res, sessionIdParam); return false; }
            if (gate === "locked") {
              sendJson(423, { error: "Session is locked", locked: true, sessionId: sessionIdParam });
              return false;
            }
            return true;
          };

          // POST /sessions/:id/renders {manifest} — persist a client-authored view (an Expand
          // asset-player tab, or a timeline) as a render manifest, so it is listed like any agent
          // render and survives refresh and later renders (R8.1). The key is derived from the
          // manifest's own id (sanitised), else a hash of the body, and returned.
          if (req.method === "POST") {
            const raw = await readBodyCapped(req, RENDER_VIEW_MAX_BYTES);
            if (!raw) { sendJson(413, { error: `manifest exceeds ${RENDER_VIEW_MAX_BYTES} bytes` }); return; }
            let body: any;
            try { body = JSON.parse(raw.toString("utf8")); } catch { sendJson(400, { error: "invalid JSON" }); return; }
            const manifest = body?.manifest;
            const invalid = renderViewManifestError(manifest);
            if (invalid) { sendJson(400, { error: invalid }); return; }
            if (!(await passGate())) return;
            if (!uploadBucket) { sendJson(503, { error: "render store not configured" }); return; }
            const hash = crypto.createHash("sha256").update(raw).digest("hex");
            const key = `artifacts/${sessionIdParam}/render/${renderViewId(manifest, hash)}.json`;
            if (!isSessionRenderKey(sessionIdParam, key)) { sendJson(400, { error: "manifest id is not a valid render name" }); return; }
            const stored: Record<string, unknown> = { ...manifest };
            if (typeof stored.createdAt !== "string" || !Number.isFinite(Date.parse(stored.createdAt))) {
              stored.createdAt = new Date().toISOString();
            }
            const viewState = normalizeViewState(stored.viewState);
            if (viewState) stored.viewState = viewState; else delete stored.viewState;
            await s3Client.send(new PutObjectCommand({
              Bucket: uploadBucket,
              Key: key,
              Body: JSON.stringify(stored),
              ContentType: "application/json",
            }));
            sendJson(200, { sessionId: sessionIdParam, key });
            return;
          }

          const key = String(url.searchParams.get("key") ?? "");
          if (!isSessionRenderKey(sessionIdParam, key)) {
            sendJson(400, { error: "key must be a render manifest under this session" });
            return;
          }

          // PATCH /sessions/:id/renders?key=<manifest> {viewState} — merge the view's timeline state
          // (selected range, frame marker, crop box; R8.2) into that manifest. Read-modify-write of
          // ONE field: everything else in the manifest is kept byte-for-byte as stored.
          if (req.method === "PATCH") {
            const raw = await readBodyCapped(req, RENDER_VIEW_PATCH_MAX_BYTES);
            if (!raw) { sendJson(413, { error: `body exceeds ${RENDER_VIEW_PATCH_MAX_BYTES} bytes` }); return; }
            let body: any;
            try { body = JSON.parse(raw.toString("utf8")); } catch { sendJson(400, { error: "invalid JSON" }); return; }
            if (!(await passGate())) return;
            if (!uploadBucket) { sendJson(503, { error: "render store not configured" }); return; }
            let current: Record<string, unknown>;
            try {
              const obj = await s3Client.send(new GetObjectCommand({ Bucket: uploadBucket, Key: key }));
              const text = await obj.Body?.transformToString();
              const parsed = text ? JSON.parse(text) : null;
              if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("not a manifest");
              current = parsed;
            } catch {
              sendJson(404, { error: "render not found", key });
              return;
            }
            const merged = mergeViewState(current.viewState, body?.viewState);
            if (!merged.ok) { sendJson(400, { error: merged.error }); return; }
            const next: Record<string, unknown> = { ...current };
            if (Object.keys(merged.viewState).length) next.viewState = merged.viewState; else delete next.viewState;
            await s3Client.send(new PutObjectCommand({
              Bucket: uploadBucket,
              Key: key,
              Body: JSON.stringify(next),
              ContentType: "application/json",
            }));
            sendJson(200, { sessionId: sessionIdParam, key, viewState: merged.viewState });
            return;
          }

          // DELETE /sessions/:id/renders?key=<manifest key> — remove a render permanently. A tab the
          // user closes must be deleted at the source or the next load re-lists it and the tab "comes
          // back". Deleting the manifest is the clean delete: the tab is derived from it, and there is
          // no second store to keep in sync.
          if (!(await passGate())) return;
          if (uploadBucket) {
            try {
              await s3Client.send(new DeleteObjectCommand({ Bucket: uploadBucket, Key: key }));
            } catch {
              // Best-effort: an already-absent object is already "deleted".
            }
          }
          sendJson(200, { sessionId: sessionIdParam, deleted: key });
          return;
        }

        // PATCH /sessions/:id — update lastActivityAt (and optionally title, renderHistory)
        if (req.method === "PATCH" && sessionIdParam) {
          const chunks: Buffer[] = [];
          req.on("data", (c: Buffer) => chunks.push(c));
          await new Promise<void>((resolve) => req.on("end", resolve));
          const body = JSON.parse(Buffer.concat(chunks).toString());
          const now = new Date().toISOString();

          // Lock toggle — the ONLY mutation permitted on a locked session (so a
          // frozen session can always be unlocked). Handled before the freeze
          // guard below, and never carries other field changes.
          if (body.locked !== undefined) {
            try {
              await ddbClient.send(new UpdateCommand({
                TableName: sessionsTable,
                Key: { sessionId: sessionIdParam },
                UpdateExpression: "SET #locked = :locked",
                // Ownership only (NO lock guard, so a locked session can always be unlocked): the row
                // must exist and belong to the caller. An unconditional update would CREATE an orphan
                // row (no actorId) for an unknown id, and would let any actor lock anyone's session.
                ConditionExpression: SESSION_OWNED_CONDITION,
                ExpressionAttributeNames: { "#locked": "locked", "#actor": "actorId" },
                ExpressionAttributeValues: { ":locked": !!body.locked, ":actor": sessionsActorId },
              }));
            } catch (lockErr: any) {
              if (lockErr?.name === "ConditionalCheckFailedException") {
                sendSessionNotFound(res, sessionIdParam);
                return;
              }
              throw lockErr;
            }
            res.writeHead(200, { "content-type": "application/json", "access-control-allow-origin": "*" });
            res.end(JSON.stringify({ sessionId: sessionIdParam, locked: !!body.locked }));
            return;
          }

          let updateExpr = "SET lastActivityAt = :now";
          const exprValues: Record<string, any> = { ":now": now };
          // Projects (when enabled): `{activeProjectId, webSearch}`. `checkAccess` runs BEFORE any write and
          // a denial is the uniform 404. The helper only ever yields activeProjectId / activeProjectSetAt /
          // webSearch. With Projects disabled it yields nothing and both keys are ignored, as before.
          const projectPatch = await sessionPatchFields(sessionsActorId, body, Date.parse(now),
            { sessionId: sessionIdParam, sessions: dynamoSessionStore(ddbClient, sessionsTable) });
          if (!projectPatch.ok) {
            res.writeHead(projectPatch.status, { "content-type": "application/json", "access-control-allow-origin": "*" });
            res.end(JSON.stringify(projectPatch.body));
            return;
          }
          for (const [field, value] of Object.entries(projectPatch.set)) {
            updateExpr += `, ${field} = :${field}`;
            exprValues[`:${field}`] = value;
          }
          if (body.title) {
            updateExpr += ", title = :title";
            exprValues[":title"] = body.title;
          }
          if (body.renderHistory !== undefined) {
            // Persist LEAN refs only — strip inlined manifests (see leanRenderHistory) so the row
            // can never exceed DynamoDB's 400 KB limit and block transcript/turn persistence.
            const history = leanRenderHistory(Array.isArray(body.renderHistory) ? body.renderHistory : []).slice(-50);
            updateExpr += ", renderHistory = :rh";
            exprValues[":rh"] = history;
          }
          if (body.transcript !== undefined) {
            // Durable copy of the rich chat transcript (per-message steps +
            // execution summary). Capped like renderHistory so the session row stays
            // well under DynamoDB's 400 KB item limit. localStorage remains the
            // fresher within-browser copy; this is the cross-browser / cache-miss
            // restore that lets a reloaded session render the same collapsed step
            // groups + duration it showed live, instead of N flat text bubbles
            // rebuilt from text-only AgentCore Memory.
            const transcript = byteCapTranscript(body.transcript);
            updateExpr += ", transcript = :tr";
            exprValues[":tr"] = transcript;
          }
          // Ownership + full-freeze guard. The row must EXIST and belong to the caller — PATCH never
          // creates a row (rows are created by `POST /sessions` on the first message; the SPA only
          // PATCHes a session it has persisted) — and a locked session rejects every non-unlock edit
          // (rename + the automatic renderHistory/transcript/activity writes). On a failed condition
          // the old image (`ALL_OLD`) tells the two apart: an owned, locked row is 423; a missing row
          // or another actor's row is the same 404.
          try {
            await ddbClient.send(new UpdateCommand({
              TableName: sessionsTable,
              Key: { sessionId: sessionIdParam },
              UpdateExpression: updateExpr,
              ConditionExpression: `${SESSION_OWNED_CONDITION} AND ${SESSION_LIVE_CONDITION} AND (attribute_not_exists(#locked) OR #locked = :unlocked)`,
              ExpressionAttributeNames: { "#locked": "locked", "#actor": "actorId" },
              ExpressionAttributeValues: { ...exprValues, ":unlocked": false, ":actor": sessionsActorId, ":one": 1 },
              ReturnValuesOnConditionCheckFailure: "ALL_OLD",
            }));
          } catch (patchErr: any) {
            if (patchErr?.name === "ConditionalCheckFailedException") {
              if (sessionFailureIsLocked(patchErr, sessionsActorId)) {
                res.writeHead(423, { "content-type": "application/json", "access-control-allow-origin": "*" });
                res.end(JSON.stringify({ error: "Session is locked", locked: true, sessionId: sessionIdParam }));
                return;
              }
              sendSessionNotFound(res, sessionIdParam);
              return;
            }
            throw patchErr;
          }
          res.writeHead(200, { "content-type": "application/json", "access-control-allow-origin": "*" });
          res.end(JSON.stringify({ sessionId: sessionIdParam, lastActivityAt: now }));
          return;
        }

        // DELETE /sessions/:id — soft delete (set active=0, add TTL). Only the owner's existing row (an
        // unknown id used to create an orphan `{sessionId, active, ttl}` row, and any actor could delete any
        // session), and never a locked one (full-freeze). The old image tells the two refusals apart: an
        // owned, locked row is 423; a missing or foreign row is 404.
        if (req.method === "DELETE" && sessionIdParam) {
          const ttl = Math.floor(Date.now() / 1000) + 30 * 24 * 60 * 60;
          let deletedRow: Record<string, unknown> | undefined;
          try {
            const del = await ddbClient.send(new UpdateCommand({
              TableName: sessionsTable,
              Key: { sessionId: sessionIdParam },
              UpdateExpression: "SET active = :inactive, #ttl = :ttl",
              ConditionExpression: `${SESSION_OWNED_CONDITION} AND (attribute_not_exists(#locked) OR #locked = :unlocked)`,
              ExpressionAttributeNames: { "#ttl": "ttl", "#locked": "locked", "#actor": "actorId" },
              ExpressionAttributeValues: { ":inactive": 0, ":ttl": ttl, ":unlocked": false, ":actor": sessionsActorId },
              ReturnValuesOnConditionCheckFailure: "ALL_OLD",
              ReturnValues: "ALL_OLD",
            }));
            deletedRow = del.Attributes;
          } catch (delErr: any) {
            if (delErr?.name === "ConditionalCheckFailedException") {
              if (!sessionFailureIsLocked(delErr, sessionsActorId)) {
                sendSessionNotFound(res, sessionIdParam);
                return;
              }
              res.writeHead(423, { "content-type": "application/json", "access-control-allow-origin": "*" });
              res.end(JSON.stringify({ error: "Session is locked", locked: true, sessionId: sessionIdParam }));
              return;
            }
            throw delErr;
          }
          // asset-lineage D2: a deleted session takes its hidden "this chat only" files with it.
          // Best effort — `remove_session_assets` is idempotent, so a retry finishes the job, and a
          // failure here must never fail the delete the user asked for.
          const deletedFrom = deletedRow?.activeProjectId;
          if (typeof deletedFrom === "string" && deletedFrom) {
            const userToken = (req as { userToken?: string }).userToken;
            void sessionAssetRemover().invoke({
              projectId: deletedFrom,
              command: "remove_session_assets",
              args: { sessionId: sessionIdParam },
              ...(userToken ? { userToken } : {}),
              ...(sessionsActorId ? { devBypassActor: sessionsActorId } : {}),
            }).catch((e: any) => console.error(
              "[agentcore-proxy] remove_session_assets failed:", e?.message ?? e));
          }
          res.writeHead(200, { "content-type": "application/json", "access-control-allow-origin": "*" });
          res.end(JSON.stringify({ deleted: true, sessionId: sessionIdParam }));
          return;
        }

        res.writeHead(405, { "content-type": "application/json" });
        res.end(JSON.stringify({ error: "Method not allowed" }));
      } catch (err: any) {
        if (err.name === "ConditionalCheckFailedException") {
          res.writeHead(200, { "content-type": "application/json", "access-control-allow-origin": "*" });
          res.end(JSON.stringify({ sessionId: sessionIdParam || "exists" }));
          return;
        }
        console.error("[agentcore-proxy] Sessions error:", err.message);
        res.writeHead(500, { "content-type": "application/json" });
        res.end(JSON.stringify({ error: err.message }));
      }
    });
  } else {
    console.warn("[agentcore-proxy] SESSIONS_TABLE not set — /sessions disabled");
    app.use("/sessions", (_req: any, res: any) => {
      res.writeHead(500, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: "SESSIONS_TABLE not configured" }));
    });
  }

  // /projects — Project_Registry CRUD and membership (`projects/routes.ts`).
  //
  // `/projects` is in `authPaths`, so `req.actorId` is the verified JWT `sub` here; the handler never
  // reads an actor id from the body. Mounted unconditionally: the handler checks `projectsEnabled()` per
  // request and answers `404 projects_not_enabled` when `PROJECTS_TABLE` is unset (prod). Invite lookup
  // defaults to `defaultInviteResolver` (Cognito `ListUsers` + `AdminGetUser`, task 6.7); with no
  // `USER_POOL_ID` it refuses every value, so the mount needs no explicit resolver here.
  //
  // The `/projects/:pid/assets*` ingestion routes (Asset_URL_Route, asset status, asset list) are the
  // real routes, dispatched from inside `createProjectsHandler` (`projects/ingestionRoutes.ts`). They
  // presign with the proxy's own credentials and run in both deployed and local (moto) environments,
  // so no separate registration — and no dev-only stand-in — is needed here.
  app.use("/projects", createProjectsHandler());

  // /jobs — async-job-tracking read endpoints (R5.2, R6.1, R8.3, R11.1, R11.2).
  //
  //   GET /jobs              — list active+recent jobs for the requesting
  //                            actor, newest-first. Backed by the
  //                            `actor-status-index` GSI on the JobRegistry
  //                            table (PK actorId, SK updatedAt).
  //   GET /jobs?ids=a,b,c    — reconnect-resync read used by the React
  //                            client when the EventSource drops (R8.3).
  //                            Parallel GetItem on each id, silently
  //                            filtered to records owned by the actor.
  //   GET /jobs/:jobId       — single record. Cross-actor returns 404 with
  //                            the same shape as a missing record so we
  //                            don't leak existence (R11.2).
  //
  // JWT auth is enforced by the shared `authMiddleware` mounted above
  // (/jobs is in the `authPaths` array). Every read filters on
  // `req.actorId`; query params never carry identity.
  //
  // /jobs/stream (the SSE push channel from task 10.2) is registered as
  // a separate, more-specific middleware below — this handler simply
  // calls `next()` for it so the request falls through.
  const jobRegistryTable = process.env.JOB_REGISTRY_TABLE ?? "";
  const jobRegistryActorStatusIndex =
    process.env.JOB_REGISTRY_ACTOR_STATUS_INDEX ?? "actor-status-index";
  const jobsDdbClient = jobRegistryTable
    ? DynamoDBDocumentClient.from(new DynamoDBClient({ region: REGION }))
    : null;
  if (jobRegistryTable) {
    console.log(
      `[agentcore-proxy] Job registry: ${jobRegistryTable} ` +
      `(GSI ${jobRegistryActorStatusIndex})`,
    );
  }

  // Project a raw DDB row onto the wire-format `Job` shape consumed by the
  // React client and shared with `/jobs/stream` AND the external
  // `/invoke/:agentId/jobs` route. Defined once in `./jobsProjection` so the
  // internal (Cognito) and external (OIDC) realms can never drift — staging's
  // added fields (hasRenderableArtifacts / runId / nodeId / dependsOn /
  // artifactUrls) live there rather than in a second inline copy here.

  if (jobsDdbClient && jobRegistryTable) {
    app.use("/jobs", async (req: any, res: any, next: any) => {
      const jobsActorId: string | undefined = req.actorId;
      if (!jobsActorId) {
        // Defense in depth — auth middleware should have already 401'd.
        send401(res, "missing_actor", "No actor id resolved for request");
        return;
      }

      const url = new URL(req.url || "/", "http://localhost");
      const pathParts = url.pathname.split("/").filter(Boolean);
      // Node job ids embed a '#' (e.g. "<runId>#frame_05", "<runId>#minimax_submit"),
      // which the client percent-encodes to "%23". `url.pathname` keeps it encoded, so
      // without decoding here the DynamoDB key never matches and every GET/DELETE of a node
      // job 404s — and on an older build with no DELETE branch it falls through to the SPA,
      // surfacing in the client as `Unexpected token '<', "<!doctype "... is not valid JSON`.
      let jobIdParam = pathParts[0];
      if (jobIdParam !== undefined) {
        try {
          jobIdParam = decodeURIComponent(jobIdParam);
        } catch {
          /* malformed escape sequence — fall back to the raw segment */
        }
      }

      // Pass /jobs/stream through to the SSE handler registered below
      // (task 10.2) regardless of middleware registration order.
      if (jobIdParam === "stream") {
        next();
        return;
      }

      if (req.method !== "GET" && req.method !== "DELETE") {
        res.writeHead(405, {
          "content-type": "application/json",
          "access-control-allow-origin": "*",
        });
        res.end(JSON.stringify({ error: "Method not allowed" }));
        return;
      }

      // --- DELETE /jobs/:jobId and DELETE /jobs?runId=... ---
      //
      // Activity Panel "clean up old runs" management (task: job/run
      // management). Two shapes:
      //   DELETE /jobs/:jobId       — remove one record.
      //   DELETE /jobs?runId=<id>   — remove every record sharing that
      //                               runId (the plan-parent row plus all
      //                               node/raw-gateway-job rows for that
      //                               run) in one call, so the panel's
      //                               "delete run" action doesn't need N
      //                               round trips from the client.
      //
      // Ownership (actorId match) is enforced before every delete — same
      // not-found-shape-on-cross-actor convention as the GET paths (R11.2).
      // Active jobs (PENDING/RUNNING) are refused unless `force=true` is
      // passed, so a client can't silently kill an in-flight job by
      // deleting its tracking row out from under the polling
      // worker/coordinator — the row disappearing does not stop the
      // underlying job, it just orphans the UI's view of it.
      if (req.method === "DELETE") {
        try {
          const force = url.searchParams.get("force") === "true";

          if (jobIdParam) {
            const result = await jobsDdbClient.send(new GetCommand({
              TableName: jobRegistryTable,
              Key: { jobId: jobIdParam },
            }));
            const item = result.Item;
            if (!item || item.actorId !== jobsActorId) {
              res.writeHead(404, {
                "content-type": "application/json",
                "access-control-allow-origin": "*",
              });
              res.end(JSON.stringify({ error: "Not found" }));
              return;
            }
            if (!force && (item.status === "PENDING" || item.status === "RUNNING")) {
              res.writeHead(409, {
                "content-type": "application/json",
                "access-control-allow-origin": "*",
              });
              res.end(JSON.stringify({
                error: "job_active",
                message: "Job is still active. Pass ?force=true to delete anyway.",
              }));
              return;
            }
            await jobsDdbClient.send(new DeleteCommand({
              TableName: jobRegistryTable,
              Key: { jobId: jobIdParam },
            }));
            res.writeHead(200, {
              "content-type": "application/json",
              "access-control-allow-origin": "*",
            });
            res.end(JSON.stringify({ deleted: [jobIdParam] }));
            return;
          }

          const runIdParam = url.searchParams.get("runId");
          if (runIdParam !== null && runIdParam.trim() !== "") {
            // No GSI on runId — scan the actor's own jobs (bounded to this
            // actor via the actor-status-index, same as the list path)
            // and filter client-side. Actor-scoped job counts are small
            // (dozens, not thousands), so this is cheap in practice and
            // avoids provisioning a new GSI for an admin/cleanup action.
            const queryResult = await jobsDdbClient.send(new QueryCommand({
              TableName: jobRegistryTable,
              IndexName: jobRegistryActorStatusIndex,
              KeyConditionExpression: "actorId = :actor",
              ExpressionAttributeValues: { ":actor": jobsActorId },
            }));
            const runItems = (queryResult.Items ?? []).filter(
              (item) => item.runId === runIdParam,
            );
            if (runItems.length === 0) {
              res.writeHead(404, {
                "content-type": "application/json",
                "access-control-allow-origin": "*",
              });
              res.end(JSON.stringify({ error: "Not found" }));
              return;
            }
            const activeItems = runItems.filter(
              (item) => item.status === "PENDING" || item.status === "RUNNING",
            );
            if (!force && activeItems.length > 0) {
              res.writeHead(409, {
                "content-type": "application/json",
                "access-control-allow-origin": "*",
              });
              res.end(JSON.stringify({
                error: "run_active",
                message: `${activeItems.length} job(s) in this run are still active. Pass ?force=true to delete anyway.`,
              }));
              return;
            }
            const deleted: string[] = [];
            for (const item of runItems) {
              try {
                await jobsDdbClient.send(new DeleteCommand({
                  TableName: jobRegistryTable,
                  Key: { jobId: item.jobId },
                }));
                deleted.push(item.jobId as string);
              } catch (err: any) {
                console.error(
                  `[agentcore-proxy] DELETE /jobs?runId= failed for jobId=${item.jobId}:`,
                  err?.message ?? err,
                );
              }
            }
            res.writeHead(200, {
              "content-type": "application/json",
              "access-control-allow-origin": "*",
            });
            res.end(JSON.stringify({ deleted }));
            return;
          }

          res.writeHead(400, {
            "content-type": "application/json",
            "access-control-allow-origin": "*",
          });
          res.end(JSON.stringify({ error: "Must specify a jobId path segment or ?runId= query param" }));
          return;
        } catch (err: any) {
          console.error("[agentcore-proxy] Jobs delete error:", err?.message ?? err);
          if (!res.headersSent) {
            res.writeHead(500, {
              "content-type": "application/json",
              "access-control-allow-origin": "*",
            });
          }
          res.end(JSON.stringify({ error: "Failed to delete job(s)" }));
          return;
        }
      }

      try {
        // GET /jobs/:jobId — single record.
        if (jobIdParam) {
          const result = await jobsDdbClient.send(new GetCommand({
            TableName: jobRegistryTable,
            Key: { jobId: jobIdParam },
          }));
          const item = result.Item;
          // R11.2: cross-actor returns the same not-found shape as a
          // missing record so existence is not leaked.
          if (!item || item.actorId !== jobsActorId) {
            res.writeHead(404, {
              "content-type": "application/json",
              "access-control-allow-origin": "*",
            });
            res.end(JSON.stringify({ error: "Not found" }));
            return;
          }
          res.writeHead(200, {
            "content-type": "application/json",
            "access-control-allow-origin": "*",
          });
          res.end(JSON.stringify(projectJob(item)));
          return;
        }

        // GET /jobs?ids=a,b,c — reconnect-resync read (R8.3).
        const idsParam = url.searchParams.get("ids");
        if (idsParam !== null && idsParam.trim() !== "") {
          const ids = Array.from(
            new Set(
              idsParam
                .split(",")
                .map((s) => s.trim())
                .filter(Boolean),
            ),
          );
          if (ids.length === 0) {
            res.writeHead(200, {
              "content-type": "application/json",
              "access-control-allow-origin": "*",
            });
            res.end(JSON.stringify({ jobs: [] }));
            return;
          }
          const fetched = await Promise.all(
            ids.map((id) =>
              jobsDdbClient
                .send(new GetCommand({
                  TableName: jobRegistryTable,
                  Key: { jobId: id },
                }))
                .then((r) => r.Item)
                .catch((err) => {
                  // Per-id failures shouldn't fail the whole resync.
                  console.error(
                    `[agentcore-proxy] /jobs?ids= GetItem failed for ${id}:`,
                    err?.message ?? err,
                  );
                  return undefined;
                }),
            ),
          );
          const jobs = fetched
            .filter(
              (item): item is Record<string, any> =>
                !!item && item.actorId === jobsActorId,
            )
            .map(projectJob);
          res.writeHead(200, {
            "content-type": "application/json",
            "access-control-allow-origin": "*",
          });
          res.end(JSON.stringify({ jobs }));
          return;
        }

        // GET /jobs?sessionId=X — every job for ONE session, regardless of recency.
        // The default (recent-runs) response is actor-wide and bounded to the newest
        // ~50 runs, so opening an OLD session showed an EMPTY right panel even though
        // its jobs are still in the registry (nothing was deleted). There is no
        // session GSI, so query the actor's rows (newest-first, safety-capped by
        // queryActorJobRows) and filter to this session. A session's rows are a small
        // subset, so no run-paging is needed here.
        const sessionIdParam = url.searchParams.get("sessionId");
        if (sessionIdParam !== null && sessionIdParam.trim() !== "") {
          const sid = sessionIdParam.trim();
          const { items, truncated } = await queryActorJobRows(jobsDdbClient, {
            table: jobRegistryTable,
            index: jobRegistryActorStatusIndex,
            actorId: jobsActorId,
          });
          const jobs = selectSessionJobs(items, sid).map(projectJob);
          res.writeHead(200, {
            "content-type": "application/json",
            "access-control-allow-origin": "*",
          });
          res.end(JSON.stringify({ jobs, scanTruncated: truncated }));
          return;
        }

        // GET /jobs — newest COMPLETE runs first. A workflow spans many rows, so
        // the old single `Limit: 100` page (ordered by the index's `updatedAt`
        // sort key) showed only ~7 of an actor's 82 runs and reshuffled as rows
        // updated — "workflows disappearing". We now page the actor's rows fully
        // (bounded by a safety cap), group them into display runs, and return the
        // newest `runs` groups from `cursor`, each complete. Extra fields are
        // additive; the client reads `.jobs`.
        const runsRaw = parseInt(
          url.searchParams.get("runs") ?? url.searchParams.get("limit") ?? "50", 10);
        const runsLimit =
          Number.isFinite(runsRaw) && runsRaw > 0 ? Math.min(runsRaw, 200) : 50;
        const offset = decodeJobsCursor(url.searchParams.get("cursor"));
        const { items, truncated } = await queryActorJobRows(jobsDdbClient, {
          table: jobRegistryTable,
          index: jobRegistryActorStatusIndex,
          actorId: jobsActorId,
        });
        const { rows, totalRuns, nextOffset } = selectRecentRuns(items, {
          limit: runsLimit,
          offset,
        });
        const jobs = rows.map(projectJob);
        res.writeHead(200, {
          "content-type": "application/json",
          "access-control-allow-origin": "*",
        });
        res.end(JSON.stringify({
          jobs,
          totalRuns,
          scanTruncated: truncated,
          nextCursor: encodeJobsCursor(nextOffset),
        }));
      } catch (err: any) {
        console.error("[agentcore-proxy] Jobs error:", err?.message ?? err);
        if (!res.headersSent) {
          res.writeHead(500, {
            "content-type": "application/json",
            "access-control-allow-origin": "*",
          });
        }
        res.end(JSON.stringify({ error: "Failed to read jobs" }));
      }
    });
  } else {
    console.warn("[agentcore-proxy] JOB_REGISTRY_TABLE not set — /jobs disabled");
    app.use("/jobs", (_req: any, res: any) => {
      res.writeHead(500, {
        "content-type": "application/json",
        "access-control-allow-origin": "*",
      });
      res.end(JSON.stringify({ error: "JOB_REGISTRY_TABLE not configured" }));
    });
  }

  // --- /improve — the Improve surface's read + dismiss transport ------------
  //
  // DEAD as of Step 3: the SPA now calls the Capability API at
  // `/control-plane/improve/*` (`ui/src/improve-data.ts`), and `/improve` has been
  // removed from `authPaths` — so `req.actorId` is never populated here and every
  // request 401s at the guard below. Kept only to keep this migration commit small;
  // it can be deleted wholesale (along with the `IMPROVE_TABLE` env note above) once
  // the Capability API path is confirmed live in staging. Nothing reaches it.
  //
  // What it did (Step 2): moved the Improve bundle off the old public, unauthenticated
  // `improve-data.json` (SPA/S3 origin) onto an authenticated, actor-scoped proxy
  // route. The actor was read from `req.actorId`, never from the request body, so a
  // client could not widen its own visibility.
  //
  // Storage (DynamoDB Improve store, PK `actorId` / SK `sk`) has two item kinds:
  //   * ONE global bundle at (actorId="__system__", sk="SNAPSHOT"). The suggestions
  //     are platform/harness-code — identical for every user — so the bundle is
  //     stored once and seeded offline by `improve.seed_snapshot`; the proxy never
  //     writes it. Real Cognito subs are UUIDs, so "__system__" cannot collide.
  //   * Per-actor dismiss overlay rows at (actorId=<sub>, sk="SUGG#<id>"), written
  //     by the dismiss/undo routes below — the OI-5 human-label substrate.
  //
  // Routes (Express strips the `/improve` mount, so `req.url` is the sub-path):
  //   GET    /data                        -> global SNAPSHOT + caller's overlay, merged
  //   POST   /suggestions/:id/dismiss     -> upsert SUGG#<id> {status:"dismissed", ...}
  //   DELETE /suggestions/:id/dismiss     -> remove SUGG#<id> (undo)
  // Anything else 404s in the same shape as a missing row, so nothing leaks.
  //
  // Gated on IMPROVE_TABLE exactly like /jobs on JOB_REGISTRY_TABLE — 500 "not
  // configured" until the table exists.
  const improveTable = process.env.IMPROVE_TABLE ?? "";
  const improveDdbClient = improveTable
    ? DynamoDBDocumentClient.from(new DynamoDBClient({ region: REGION }))
    : null;
  // The reserved global partition. MUST match `seed_snapshot.SYSTEM_ACTOR`.
  const IMPROVE_SYSTEM_ACTOR = "__system__";
  if (improveTable) {
    console.log(`[agentcore-proxy] Improve store: ${improveTable}`);
  }
  const improveJson = (res: any, status: number, body: unknown) => {
    if (!res.headersSent) {
      res.writeHead(status, {
        "content-type": "application/json",
        "access-control-allow-origin": "*",
      });
    }
    res.end(JSON.stringify(body));
  };
  if (improveDdbClient && improveTable) {
    app.use("/improve", async (req: any, res: any) => {
      const improveActorId: string | undefined = req.actorId;
      if (!improveActorId) {
        send401(res, "missing_actor", "No actor id resolved for request");
        return;
      }
      const url = new URL(req.url || "/", "http://localhost");
      const pathParts = url.pathname.split("/").filter(Boolean);
      const method = req.method;

      const isData = pathParts.length === 1 && pathParts[0] === "data";
      // /suggestions/:id/dismiss — id is a content hash (12 hex chars), but decode
      // anyway so the shape survives an encoded id without a surprise mismatch.
      const isDismiss = pathParts.length === 3
        && pathParts[0] === "suggestions" && pathParts[2] === "dismiss";
      const suggestionId = isDismiss ? decodeURIComponent(pathParts[1]) : "";

      try {
        // ---- GET /improve/data : global bundle + caller's dismiss overlay -----
        if (method === "GET" && isData) {
          const [snapshot, overlay] = await Promise.all([
            improveDdbClient.send(new GetCommand({
              TableName: improveTable,
              Key: { actorId: IMPROVE_SYSTEM_ACTOR, sk: "SNAPSHOT" },
            })),
            improveDdbClient.send(new QueryCommand({
              TableName: improveTable,
              KeyConditionExpression: "actorId = :a AND begins_with(sk, :p)",
              ExpressionAttributeValues: { ":a": improveActorId, ":p": "SUGG#" },
            })),
          ]);
          const item = snapshot.Item;
          // Absent ≠ empty: no SNAPSHOT at all reads as 404 ("no data yet"),
          // distinct from a populated bundle whose lists happen to be empty.
          if (!item || !item.bundle) {
            improveJson(res, 404, { error: "Not found" });
            return;
          }
          const bundle = item.bundle as { suggestions?: any[] };
          // Overlay keyed by suggestionId. The merge is strictly bundle→overlay:
          // iterate the bundle's suggestions and look each up, NEVER fabricate a
          // suggestion from an overlay-only id. That makes a stale SUGG# row (one
          // whose suggestion is no longer mined) harmless — it simply matches
          // nothing.
          const overlayById: Record<string, any> = {};
          for (const row of overlay.Items ?? []) {
            const sk = String(row.sk ?? "");
            if (sk.startsWith("SUGG#")) overlayById[sk.slice("SUGG#".length)] = row;
          }
          const suggestions = Array.isArray(bundle.suggestions) ? bundle.suggestions : [];
          const merged = suggestions.map((s: any) => {
            const ov = overlayById[s?.suggestionId];
            return ov && ov.status === "dismissed" ? { ...s, status: "dismissed" } : s;
          });
          improveJson(res, 200, { ...bundle, suggestions: merged });
          return;
        }

        // ---- POST /improve/suggestions/:id/dismiss : record a dismissal -------
        if (method === "POST" && isDismiss) {
          const chunks: Buffer[] = [];
          req.on("data", (c: Buffer) => chunks.push(c));
          await new Promise<void>((resolve) => req.on("end", resolve));
          let parsed: any = {};
          try {
            parsed = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
          } catch {
            improveJson(res, 400, { error: "Invalid JSON body" });
            return;
          }
          // Absent ≠ empty: only carry reason/note when the client actually sent
          // one, so a blank dismissal does not masquerade as a reasoned label.
          const overlayRow: Record<string, unknown> = {
            actorId: improveActorId,
            sk: `SUGG#${suggestionId}`,
            status: "dismissed",
            dismissedAt: new Date().toISOString(),
          };
          if (typeof parsed.reason === "string" && parsed.reason) overlayRow.reason = parsed.reason;
          if (typeof parsed.note === "string" && parsed.note) overlayRow.note = parsed.note;
          await improveDdbClient.send(new PutCommand({
            TableName: improveTable,
            Item: overlayRow,
          }));
          improveJson(res, 200, { ok: true, suggestionId, status: "dismissed" });
          return;
        }

        // ---- DELETE /improve/suggestions/:id/dismiss : undo a dismissal -------
        if (method === "DELETE" && isDismiss) {
          await improveDdbClient.send(new DeleteCommand({
            TableName: improveTable,
            Key: { actorId: improveActorId, sk: `SUGG#${suggestionId}` },
          }));
          improveJson(res, 200, { ok: true, suggestionId, status: "open" });
          return;
        }

        // Unknown method+path: same 404 shape as a missing row.
        improveJson(res, 404, { error: "Not found" });
      } catch (err: any) {
        console.error("[agentcore-proxy] Improve error:", err?.message ?? err);
        improveJson(res, 500, { error: "Improve store request failed" });
      }
    });
  } else {
    console.warn("[agentcore-proxy] IMPROVE_TABLE not set — /improve disabled");
    app.use("/improve", (_req: any, res: any) => {
      improveJson(res, 500, { error: "IMPROVE_TABLE not configured" });
    });
  }

  // /jobs/stream — Server-Sent Events push channel (R6.3, R8.1, R8.2,
  // R8.3, R9.1).
  //
  // The DynamoDB stream → changefeed-fanout Lambda (`infra/lib/lambdas/
  // changefeed-fanout/handler.ts`, task 9.1) projects each relevant
  // JobRegistry mutation onto a `JobUpdateEvent` envelope and writes it
  // to a SINGLE SQS FIFO queue with `MessageGroupId = actorId`. This
  // handler is the SSE consumer for that queue.
  //
  // Wire format (see design.md → "Push_Channel" → "Wire format"):
  //
  //     event: job.update
  //     data:  <Job projection JSON>
  //
  //     event: job.terminal
  //     data:  <Job projection JSON>
  //
  // The `data:` line carries the bare `Job`, NOT the `{event, data}`
  // envelope that is on the queue.
  //
  // Multi-actor caveat (deployment model assumption):
  //
  //   The design uses one shared FIFO queue for ALL actors and relies on
  //   per-message filtering at the consumer. With multiple SSE
  //   connections (different actors) sharing this single proxy process,
  //   each `ReceiveMessage` returns an interleaved batch from every
  //   actor; we forward only frames whose `data.actorId` matches the
  //   connection's `req.actorId`, and short-circuit cross-actor messages
  //   with `ChangeMessageVisibility(VisibilityTimeout=0)` so another
  //   poll iteration (potentially on the SSE connection that owns the
  //   actor) can pick them up immediately rather than waiting for the
  //   queue's default visibility timeout to expire.
  //
  //   This is correct under our deployment model (one process instance —
  //   or N instances, each handling its own subset of SSE connections —
  //   with every instance polling the SAME shared FIFO queue) and
  //   inherently inefficient once N grows large: instances that don't own
  //   the message that arrives still burn a ReceiveMessage/
  //   ChangeMessageVisibility round-trip on it. When that inefficiency
  //   matters, prefer one of:
  //
  //     • per-actor SQS queues provisioned on demand
  //     • SNS topic with one subscription per actor connection
  //     • a Redis pub/sub layer in front of the proxy
  //
  //   Until that day, the comments in `changefeed-fanout/handler.ts` and
  //   this block are the canonical "how this scales" record.
  //
  // Reconnect resync (R8.3) is handled client-side by re-issuing
  // `GET /jobs?ids=...` on every EventSource open. We do not need to
  // replay historical messages here; the queue's role is push-only for
  // currently-connected clients.
  const jobFanoutQueueUrl = process.env.JOB_FANOUT_QUEUE_URL ?? "";
  const sqsClient = jobFanoutQueueUrl ? new SQSClient({ region: REGION }) : null;
  // Generated_Media (ingestion Req 9): one message per finished Projects turn; absent → nothing is sent.
  const generatedMediaQueueUrl = process.env.GENERATED_MEDIA_QUEUE_URL ?? "";
  const notifyGeneratedMedia = generatedMediaQueueUrl
    ? (() => {
        const client = sqsClient ?? new SQSClient({ region: REGION });
        return makeGeneratedMediaNotifier((body) =>
          client.send(new SendMessageCommand({ QueueUrl: generatedMediaQueueUrl, MessageBody: body })));
      })()
    : null;
  if (jobFanoutQueueUrl) {
    console.log(`[agentcore-proxy] Job fanout queue: ${jobFanoutQueueUrl}`);
  }

  // POST /media/actions — direct timeline actions (Frame / Clip / Crop), NO model turn (mediaActions.ts). The
  // chat's project gates the source; the main runtime runs ffmpeg in the chat's warm sandbox; the outputs are
  // registered through the GeneratedMedia turn message and the action is recorded as a transcript `action` line.
  if (ddbClient && sessionsTable) {
    const mediaSessions = dynamoSessionStore(ddbClient, sessionsTable);
    const mediaAssets = createAssetServiceClient();
    app.use("/media/actions", (req: any, res: any) => {
      if (!projectsEnabled()) {
        res.writeHead(404, { "content-type": "application/json", "access-control-allow-origin": "*" });
        res.end(JSON.stringify({ ok: false, error: "projects are not enabled" }));
        return;
      }
      void handleMediaActions(req, res, {
        readSession: (sid, actor) => mediaSessions.read(sid, actor),
        checkAccess: (actor, pid) => checkProjectAccess(actor, pid),
        resolveLocation: async (pid, actor, token, location) => {
          const reply = await mediaAssets.invoke({
            projectId: pid, command: "resolve_locations", args: { locations: [location] },
            ...(token ? { userToken: token } : isDevBypassEnabled() ? { devBypassActor: actor } : {}),
          });
          const id = reply.ok ? (reply.value as { locations?: Record<string, unknown> })?.locations?.[location] : null;
          return typeof id === "string" ? id : null;
        },
        readAssetSource: (pid, assetId) => readAssetSource(pid, assetId),
        invokeRuntime: async (sid, payload) => {
          const out = await signedFetch(invokeUrl, Buffer.from(JSON.stringify(payload), "utf8"), {
            "x-amzn-bedrock-agentcore-runtime-session-id": sid,
          });
          return { status: out.status, body: out.body.toString("utf8") };
        },
        ...(uploadBucket ? {
          recordAction: (sid: string, message: StoredMessage) => finalizeTurn({
            s3: s3Client, ddb: ddbClient, uploadBucket, sessionsTable, sessionId: sid, turnId: message.turnId ?? message.id,
            userTurn: message, byteCap: byteCapTranscript,
          }),
          readActionLine: async (sid: string, messageId: string) =>
            ((await readTranscriptFromS3(s3Client, uploadBucket, sid)) ?? []).find((m) => m.id === messageId) ?? null,
        } : {}),
        ...(notifyGeneratedMedia ? { notifyGeneratedMedia } : {}),
      }).catch((e: any) => {
        console.error("[media-actions] failed:", e?.message ?? e);
        if (!res.headersSent) {
          res.writeHead(500, { "content-type": "application/json", "access-control-allow-origin": "*" });
          res.end(JSON.stringify({ ok: false, error: "internal error" }));
        }
      });
    });
  }

  if (sqsClient && jobFanoutQueueUrl) {
    app.use("/jobs/stream", async (req: any, res: any) => {
      const streamActorId: string | undefined = req.actorId;
      if (!streamActorId) {
        // Defense in depth — auth middleware mounted at /jobs should
        // have already 401'd cross-actor / unauthenticated requests.
        send401(res, "missing_actor", "No actor id resolved for request");
        return;
      }
      if (req.method !== "GET") {
        res.writeHead(405, {
          "content-type": "application/json",
          "access-control-allow-origin": "*",
        });
        res.end(JSON.stringify({ error: "Method not allowed" }));
        return;
      }

      // SSE response setup. `x-accel-buffering: no` disables proxy
      // buffering (e.g. nginx); `cache-control: no-cache` and
      // `connection: keep-alive` are the standard pair for EventSource.
      res.writeHead(200, {
        "content-type": "text/event-stream",
        "cache-control": "no-cache",
        "connection": "keep-alive",
        "x-accel-buffering": "no",
        "access-control-allow-origin": "*",
      });
      // Initial comment so the client can confirm the stream is open
      // before the first real frame arrives. EventSource treats any
      // line starting with `:` as a comment.
      res.write(": ok\n\n");

      let closed = false;
      const onClose = (): void => {
        closed = true;
      };
      req.on("close", onClose);
      res.on("close", onClose);

      // Long-poll loop. Each `ReceiveMessage` waits up to 20s for a
      // message; if none arrives, the call returns empty and we loop
      // immediately to keep the connection alive. The loop terminates
      // when the client disconnects (`req.close`) or the response is
      // already ended.
      //
      // Heartbeat: when the actor has no jobs, `ReceiveMessage` returns
      // empty every 20s and the loop would otherwise write NOTHING to the
      // socket indefinitely. Any HTTP proxy in front of the origin
      // (CloudFront's origin-response timeout defaults to 30s; ALB idle
      // timeout 60s) cuts a connection that produces no bytes for its
      // window — which surfaced in the browser as a repeating
      // `ERR_HTTP2_PROTOCOL_ERROR` and a reconnect storm every ~30s. An SSE
      // comment (`:\n\n`, ignored by EventSource) on every empty cycle
      // keeps the connection provably alive well under those windows.
      while (!closed && !res.writableEnded) {
        try {
          const recv = await sqsClient.send(new ReceiveMessageCommand({
            QueueUrl: jobFanoutQueueUrl,
            WaitTimeSeconds: 20,
            MaxNumberOfMessages: 10,
            MessageAttributeNames: ["All"],
          }));
          if (closed || res.writableEnded) break;

          const messages = recv.Messages ?? [];
          if (messages.length === 0) {
            // Empty long-poll cycle — emit a heartbeat comment so the
            // connection never goes >20s without bytes.
            res.write(": heartbeat\n\n");
            continue;
          }
          for (const msg of messages) {
            if (closed || res.writableEnded) break;
            if (!msg.Body || !msg.ReceiptHandle) continue;

            let envelope: JobUpdateEvent | null = null;
            try {
              envelope = JSON.parse(msg.Body) as JobUpdateEvent;
            } catch (parseErr: any) {
              console.error(
                "[agentcore-proxy] /jobs/stream malformed message body:",
                parseErr?.message ?? parseErr,
              );
            }

            if (!envelope || !envelope.data || typeof envelope.data.actorId !== "string") {
              // Unparseable or missing actor id — drop it so it doesn't
              // come back. Logging above is enough.
              try {
                await sqsClient.send(new DeleteMessageCommand({
                  QueueUrl: jobFanoutQueueUrl,
                  ReceiptHandle: msg.ReceiptHandle,
                }));
              } catch (delErr: any) {
                console.error(
                  "[agentcore-proxy] /jobs/stream DeleteMessage (malformed) failed:",
                  delErr?.message ?? delErr,
                );
              }
              continue;
            }

            if (envelope.data.actorId === streamActorId) {
              // Owned by this connection. Per the design wire format,
              // the SSE `data:` line carries the bare `Job` payload —
              // not the {event, data} envelope.
              const eventName = envelope.event ?? "job.update";
              const dataJson = JSON.stringify(envelope.data);
              res.write(`event: ${eventName}\n`);
              res.write(`data: ${dataJson}\n\n`);
              try {
                await sqsClient.send(new DeleteMessageCommand({
                  QueueUrl: jobFanoutQueueUrl,
                  ReceiptHandle: msg.ReceiptHandle,
                }));
              } catch (delErr: any) {
                console.error(
                  "[agentcore-proxy] /jobs/stream DeleteMessage failed:",
                  delErr?.message ?? delErr,
                );
              }
            } else {
              // Belongs to a different actor — release the message
              // immediately so another iteration (possibly on a
              // different SSE connection, in this or another instance)
              // can pick it up without waiting for the default
              // visibility timeout. See the multi-actor caveat in the
              // block header above.
              try {
                await sqsClient.send(new ChangeMessageVisibilityCommand({
                  QueueUrl: jobFanoutQueueUrl,
                  ReceiptHandle: msg.ReceiptHandle,
                  VisibilityTimeout: 0,
                }));
              } catch (visErr: any) {
                console.error(
                  "[agentcore-proxy] /jobs/stream ChangeMessageVisibility failed:",
                  visErr?.message ?? visErr,
                );
              }
            }
          }
        } catch (err: any) {
          // Surface the error as an SSE comment so `EventSource.onerror`
          // observers see something, but keep the loop running — the
          // browser would auto-reconnect anyway, but a transient SQS
          // hiccup shouldn't drop the connection. Brief backoff so we
          // don't tight-loop on persistent failures.
          console.error(
            "[agentcore-proxy] /jobs/stream ReceiveMessage failed:",
            err?.message ?? err,
          );
          if (!closed && !res.writableEnded) {
            try {
              const safeMsg = String(err?.message ?? err).replace(/[\r\n]+/g, " ");
              res.write(`: error ${safeMsg}\n\n`);
            } catch {
              /* ignore write failure on a likely-closed socket */
            }
          }
          await new Promise((resolve) => setTimeout(resolve, 1000));
        }
      }

      try {
        res.end();
      } catch {
        /* socket already closed */
      }
    });
  } else {
    console.warn(
      "[agentcore-proxy] JOB_FANOUT_QUEUE_URL not set — /jobs/stream disabled",
    );
    app.use("/jobs/stream", (_req: any, res: any) => {
      res.writeHead(500, {
        "content-type": "application/json",
        "access-control-allow-origin": "*",
      });
      res.end(JSON.stringify({ error: "JOB_FANOUT_QUEUE_URL not configured" }));
    });
  }

  // /memory — AgentCore Memory proxy (uses @aws-sdk/client-bedrock-agentcore)
  // GET  /memory/sessions          → list sessions for actor
  // GET  /memory/events?sid=xxx    → list events for a session (with payloads)
  // POST /memory/events            → create event (write a turn)
  // MEMORY_ID is set by the CDK stack output; fail loud if it's missing
  // so we don't silently read/write a non-existent memory resource.
  const MEMORY_ID = process.env.MEMORY_ID ?? "";
  // The memory actor is per-request (sourced from `req.actorId`, which is
  // set by the auth middleware from the JWT `sub` claim — or, in dev
  // bypass mode, from LAB_DEFAULT_ACTOR_ID).
  if (!MEMORY_ID) {
    console.warn("[agentcore-proxy] MEMORY_ID not set — /memory/* will return empty results");
  } else {
    console.log(`[agentcore-proxy] Memory: ${MEMORY_ID} (actor per-request from JWT)`);
  }
  const memoryClient = new BedrockAgentCoreClient({ region: REGION });

  app.use("/memory", async (req: any, res: any) => {
    const memoryActor: string | undefined = req.actorId;
    if (!memoryActor) {
      send401(res, "missing_actor", "No actor id resolved for request");
      return;
    }
    const url = new URL(req.url || "/", "http://localhost");
    const pathname = url.pathname;

    try {
      // GET /memory/sessions — list sessions for actor
      if (req.method === "GET" && pathname === "/sessions") {
        const maxResults = parseInt(url.searchParams.get("max") || "50", 10);
        try {
          const result = await memoryClient.send(new ListSessionsCommand({
            memoryId: MEMORY_ID,
            actorId: memoryActor,
            maxResults,
          }));
          res.writeHead(200, { "content-type": "application/json", "access-control-allow-origin": "*" });
          res.end(JSON.stringify({
            sessions: result.sessionSummaries ?? [],
            nextToken: result.nextToken,
          }));
        } catch (e: any) {
          if (e.name === "ResourceNotFoundException") {
            res.writeHead(200, { "content-type": "application/json", "access-control-allow-origin": "*" });
            res.end(JSON.stringify({ sessions: [] }));
          } else {
            throw e;
          }
        }
        return;
      }

      // GET /memory/events?sid=xxx — list events for a session (with payloads)
      if (req.method === "GET" && pathname === "/events") {
        const sessionId = url.searchParams.get("sid");
        if (!sessionId) {
          res.writeHead(400, { "content-type": "application/json" });
          res.end(JSON.stringify({ error: "Missing sid param" }));
          return;
        }
        const maxResults = parseInt(url.searchParams.get("max") || "100", 10);
        try {
          const result = await memoryClient.send(new ListEventsCommand({
            memoryId: MEMORY_ID,
            actorId: memoryActor,
            sessionId,
            includePayloads: true,
            maxResults,
          }));
          res.writeHead(200, { "content-type": "application/json", "access-control-allow-origin": "*" });
          res.end(JSON.stringify({
            events: result.events ?? [],
            nextToken: result.nextToken,
          }));
        } catch (e: any) {
          if (e.name === "ResourceNotFoundException") {
            res.writeHead(200, { "content-type": "application/json", "access-control-allow-origin": "*" });
            res.end(JSON.stringify({ events: [] }));
          } else {
            throw e;
          }
        }
        return;
      }

      // POST /memory/events — create event (write a conversation turn)
      if (req.method === "POST" && pathname === "/events") {
        const chunks: Buffer[] = [];
        req.on("data", (c: Buffer) => chunks.push(c));
        await new Promise<void>((resolve) => req.on("end", resolve));
        const body = JSON.parse(Buffer.concat(chunks).toString());
        const { sessionId, messages } = body;
        if (!sessionId || !messages || !Array.isArray(messages)) {
          res.writeHead(400, { "content-type": "application/json" });
          res.end(JSON.stringify({ error: "Missing sessionId or messages" }));
          return;
        }

        const payload = messages.map((m: { role: string; content: string }) => ({
          conversational: {
            content: { text: m.content },
            role: m.role.toUpperCase(),
          },
        }));

        const result = await memoryClient.send(new CreateEventCommand({
          memoryId: MEMORY_ID,
          actorId: memoryActor,
          sessionId,
          eventTimestamp: new Date(),
          payload,
        }));

        res.writeHead(200, { "content-type": "application/json", "access-control-allow-origin": "*" });
        res.end(JSON.stringify({ eventId: result.event?.eventId }));
        return;
      }

      res.writeHead(405, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: "Method not allowed" }));
    } catch (err: any) {
      console.error("[agentcore-proxy] Memory error:", err.message);
      res.writeHead(500, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: err.message }));
    }
  });

  // /catalog — aggregated tool catalog (skills + built-ins + every
  // registered gateway). Single source of truth is the agent runtime's
  // `mcp_list_available` action, which already enumerates every gateway
  // loaded from the registry table. We forward the caller's JWT to that
  // action and reshape the response into the `{ sections: [...] }`
  // contract that TaskPanel/ToolsPanel/CapabilitiesSettings expect.
  //
  // Per-gateway sections are labelled with the gateway's display name
  // (`Gateway: <name>`) so the Capabilities tab can show a distinct
  // badge per gateway instead of bucketing everything into a single
  // "Main Gateway" pill.
  type CatalogSection = { label: string; tools: any[] };
  /**
   * A catalog plus the gateways this read could not reach.
   *
   * `unreadable` is part of the contract, not diagnostics: the panel paints what
   * arrived and then asks the data plane for the rest. Dropping it would turn
   * "we have not read this gateway yet" into "this gateway has no tools".
   */
  type CatalogResult = { sections: CatalogSection[]; unreadable: string[] };
  const CATALOG_CACHE_TTL_MS = 60_000;
  // Keyed per actor. A single shared cache would defeat the per-actor session
  // id above: one user's catalog would be served to the next caller straight
  // out of the cache, which is the same cross-actor leak by a different route.
  const catalogCacheByActor = new Map<string, { result: CatalogResult; expiresAt: number }>();
  const catalogInFlightByActor = new Map<string, Promise<CatalogResult>>();

  const gatewayRegistryTableName = process.env.GATEWAY_REGISTRY_TABLE ?? "";
  const catalogDdb = gatewayRegistryTableName
    ? DynamoDBDocumentClient.from(new DynamoDBClient({ region: REGION }))
    : null;

  const fetchCatalog = async (actorId: string, live = false): Promise<CatalogResult> => {
    // Preferred path: control plane for gateway tools + the registry item the
    // runtime publishes for builtins. No runtime invocation, so no cold start —
    // the measured difference is 8.6s versus ~0.5s.
    //
    // `live` skips it deliberately. A gateway the control plane cannot describe
    // (cross-account, or an MCP-server target whose capabilities live only in the
    // gateway's synchronized catalog) is visible ONLY from the data plane, so the
    // panel asks for one live pass in the background to complete what the fast
    // path reported as `unreadable`. Skipped rather than merged: the runtime
    // enumerates every gateway anyway, so its answer is the superset.
    if (!live) {
      try {
        const direct = await catalogWithoutRuntime(REGION, catalogDdb, gatewayRegistryTableName, actorId);
        if (direct) return direct;
      } catch (err: any) {
        console.warn("[agentcore-proxy] Catalog direct read failed, falling back:", err?.message);
      }
    }

    // Fallback: ask the runtime. Reached when the registry has no published
    // builtins yet (fresh deploy), a gateway is not an AgentCore gateway, or a
    // control-plane read failed. The session id is derived from the actor so
    // this path stays warm per user without sharing one container — and hence
    // one actor-bound gateway connection — across users.
    const outboundBody = Buffer.from(JSON.stringify({ action: "mcp_list_available" }));
    const upstream = await signedFetch(invokeUrl, outboundBody, {
      "x-amzn-bedrock-agentcore-runtime-session-id": catalogSessionIdFor(actorId),
    });
    if (upstream.status !== 200) {
      const error = new Error(`Catalog runtime returned ${upstream.status}`) as Error & {
        status?: number;
        body?: Buffer;
      };
      error.status = upstream.status;
      error.body = upstream.body;
      throw error;
    }

    const rawText = upstream.body.toString("utf8");
    let envelope: any = null;
    for (const line of rawText.split("\n")) {
      if (!line.startsWith("data: ")) continue;
      try {
        let value: any = JSON.parse(line.slice(6));
        if (typeof value === "string") {
          try { value = JSON.parse(value); } catch { /* single-encoded */ }
        }
        if (value && typeof value === "object") envelope = value;
      } catch { /* skip malformed line */ }
    }
    if (!envelope) {
      try { envelope = JSON.parse(rawText); } catch { envelope = null; }
    }
    if (!envelope) throw new Error("Unparseable runtime response");

    const gateways = Array.isArray(envelope.gateways) ? envelope.gateways : [];
    const builtinTools = Array.isArray(envelope.builtin_tools) ? envelope.builtin_tools : [];
    const sections: CatalogSection[] = [{ label: "Native Tools", tools: builtinTools }];
    for (const gateway of gateways) {
      sections.push({
        label: `Gateway: ${gateway?.name ?? gateway?.id ?? "Unnamed"}`,
        tools: Array.isArray(gateway?.tools) ? gateway.tools : [],
      });
    }
    // The runtime holds a live MCP session to every registered gateway, so it
    // saw everything there was to see. Nothing is outstanding after this.
    return { sections, unreadable: [] };
  };

  app.use("/catalog", async (req: any, res) => {
    try {
      const authHeader = req.headers["authorization"];
      if (!authHeader || typeof authHeader !== "string") {
        send401(res, "missing_token", "Authorization header is required");
        return;
      }

      const requestUrl = new URL(req.url || "/", "http://localhost");
      // `live=1` is the panel's second phase: go to the data plane for the
      // gateways the fast path reported as unreadable. It implies a cache bypass —
      // serving a cached fast-path result here would answer a request for the
      // complete list with the incomplete one that prompted it.
      const live = requestUrl.searchParams.get("live") === "1";
      const forceRefresh = requestUrl.searchParams.get("refresh") === "1" || live;
      const catalogActor = typeof req.actorId === "string" && req.actorId
        ? req.actorId
        : "anonymous";
      const cached = catalogCacheByActor.get(catalogActor);
      if (!forceRefresh && cached && Date.now() < cached.expiresAt) {
        res.writeHead(200, {
          "content-type": "application/json",
          "access-control-allow-origin": "*",
          "cache-control": "private, max-age=30",
          "x-capability-cache": "HIT",
        });
        res.end(JSON.stringify(cached.result));
        return;
      }

      let result: CatalogResult;
      let cacheResult = "MISS";
      const inFlight = catalogInFlightByActor.get(catalogActor);
      if (!forceRefresh && inFlight) {
        result = await inFlight;
        cacheResult = "HIT";
      } else {
        const request = fetchCatalog(catalogActor, live);
        if (!forceRefresh) catalogInFlightByActor.set(catalogActor, request);
        try {
          result = await request;
        } finally {
          if (!forceRefresh && catalogInFlightByActor.get(catalogActor) === request) {
            catalogInFlightByActor.delete(catalogActor);
          }
        }
        // A live pass IS cached: it is the completed catalog, so the next reader
        // gets the full list without a second runtime invocation.
        catalogCacheByActor.set(catalogActor, {
          result,
          expiresAt: Date.now() + CATALOG_CACHE_TTL_MS,
        });
      }

      res.writeHead(200, {
        "content-type": "application/json",
        "access-control-allow-origin": "*",
        "cache-control": "private, max-age=30",
        "x-capability-cache": cacheResult,
      });
      res.end(JSON.stringify(result));
    } catch (err: any) {
      console.error("[agentcore-proxy] Catalog error:", err.message);
      const status = typeof err.status === "number" ? err.status : 502;
      res.writeHead(status, { "content-type": "application/json" });
      res.end(err.body instanceof Buffer ? err.body : JSON.stringify({ error: err.message }));
    }
  });

  // /mcp/servers — the actor's visible MCP servers, read control-plane-side so
  // it does NOT wake the agent runtime (backlog 2.1 + 2.2). Two phases:
  //
  //   GET /mcp/servers          → ownership rows only (one DynamoDB Query,
  //                               instant paint — the caller's own servers)
  //   GET /mcp/servers?live=1   → merged with live gateway targets from the
  //                               control plane (surfaces platform servers such
  //                               as aws-mcp, plus live status)
  //
  // *** FAIL CLOSED. *** An unresolved actor gets empty + error, never a list —
  // the gateway's `usr-` targets belong to other people. `mcpServersForActor`
  // enforces this before touching DynamoDB or the control plane; the route adds
  // the same guard so a misconfigured registry can't turn into a leak either.
  app.use("/mcp/servers", async (req: any, res: any) => {
    const jsonHead = () =>
      res.writeHead(200, {
        "content-type": "application/json",
        "access-control-allow-origin": "*",
        "cache-control": "private, max-age=0, no-store",
      });
    try {
      const actorId = typeof req.actorId === "string" ? req.actorId : "";
      if (!actorId) {
        // Fail closed: never fall through to an unfiltered read.
        jsonHead();
        res.end(JSON.stringify({ servers: [], unreadable: [], error: "actor_unresolved" }));
        return;
      }
      if (!catalogDdb || !gatewayRegistryTableName) {
        jsonHead();
        res.end(JSON.stringify({ servers: [], unreadable: [], error: "registry_unavailable" }));
        return;
      }

      const requestUrl = new URL(req.url || "/", "http://localhost");
      const live = requestUrl.searchParams.get("live") === "1";

      const result = live
        ? await mcpServersForActor(
            REGION,
            catalogDdb,
            gatewayRegistryTableName,
            actorId,
            undefined,
            // Platform rows come from the harness's own gateway only, matching
            // `user_mcp.list_platform_mcp_servers`. Absent → reported as
            // unreadable, never silently dropped.
            process.env.HARNESS_GATEWAY_ID ?? "",
          )
        : {
            servers: await ownershipRowsForActor(catalogDdb, gatewayRegistryTableName, actorId),
            // EMPTY, not "Platform gateway pending".
            //
            // `unreadable` is rendered as a warning — "Couldn't read one gateway, so
            // this list may be incomplete" — so naming the platform gateway here to
            // mean "not fetched yet" asserted a failure that had not happened. It
            // flashed on every open. Pendency and failure need different words, and
            // the live phase now lands ~50ms later (measured 145ms then 197ms), so
            // there is no gap left worth narrating.
            unreadable: [],
          };

      // Diagnostic, values-free. The panel rendered empty with NO error, which
      // is a state the code should not be able to reach: both functions return
      // the caller's owned rows when run against the live table. Log the actor
      // PREFIX (never the full id) and the counts so the next occurrence says
      // which layer produced the empty list instead of requiring inference.
      console.log(
        `[agentcore-proxy] /mcp/servers live=${live} actor=${actorId.slice(0, 8)}... ` +
        `servers=${result.servers.length} error=${(result as any).error ?? "none"}`,
      );

      jsonHead();
      res.end(JSON.stringify(result));
    } catch (err: any) {
      console.error("[agentcore-proxy] MCP servers error:", err?.message ?? err);
      // Fail safe: an empty list with an error, never a partial/unfiltered one.
      jsonHead();
      res.end(JSON.stringify({ servers: [], unreadable: [], error: "mcp_list_failed" }));
    }
  });

  // /models — list available Bedrock inference profiles
  //
  // Also reports the platform default so the CLIENT never has to guess one. It
  // used to pick `models[0]`, and `ListInferenceProfiles` happens to return
  // Claude 3 Haiku first — a 4096-output model. Merely opening Settings -> Model
  // on a fresh browser therefore pinned the orchestrator to it and every chat
  // turn died with `ValidationException ... exceeds the model limit of 4096`.
  // Same value the agent runtimes read, set from CDK so the two cannot disagree;
  // the literal here is only for local dev.
  const DEFAULT_MODEL_ID =
    process.env.DEFAULT_MODEL_ID ?? "global.anthropic.claude-opus-5";
  const bedrockClient = new BedrockClient({ region: REGION });
  let modelsCache: { data: any[]; ts: number } = { data: [], ts: 0 };

  app.use("/models", async (_req, res) => {
    try {
      // Cache for 5 minutes
      if (Date.now() - modelsCache.ts < 300_000 && modelsCache.data.length > 0) {
        res.writeHead(200, { "content-type": "application/json", "access-control-allow-origin": "*" });
        res.end(JSON.stringify({ models: modelsCache.data, defaultModelId: DEFAULT_MODEL_ID }));
        return;
      }

      const profiles: any[] = [];
      let nextToken: string | undefined;
      do {
        const cmd = new ListInferenceProfilesCommand({
          typeEquals: "SYSTEM_DEFINED",
          maxResults: 100,
          ...(nextToken ? { nextToken } : {}),
        });
        const resp = await bedrockClient.send(cmd);
        for (const p of resp.inferenceProfileSummaries ?? []) {
          if (p.status !== "ACTIVE") continue;
          // Filter out image/embedding/video models
          const name = (p.inferenceProfileName ?? "").toLowerCase();
          if (name.includes("stable") || name.includes("embed") || name.includes("pegasus") ||
              name.includes("upscale") || name.includes("outpaint") || name.includes("inpaint") ||
              name.includes("background") || name.includes("recolor") || name.includes("replace") ||
              name.includes("style") || name.includes("sketch") || name.includes("structure") ||
              name.includes("erase")) continue;
          profiles.push({
            id: p.inferenceProfileId,
            name: p.inferenceProfileName,
            // Whether this model accepts the reasoning-effort cap. The UI shows
            // the effort slider only when true; sending the field otherwise 400s.
            supportsEffort: supportsEffort(p.inferenceProfileId),
          });
        }
        nextToken = resp.nextToken;
      } while (nextToken);

      modelsCache = { data: profiles, ts: Date.now() };
      res.writeHead(200, { "content-type": "application/json", "access-control-allow-origin": "*" });
      res.end(JSON.stringify({ models: profiles, defaultModelId: DEFAULT_MODEL_ID }));
    } catch (err: any) {
      console.error("[agentcore-proxy] Models error:", err.message);
      res.writeHead(502, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: err.message }));
    }
  });

  // There is deliberately NO `/dispatch` route. It forwarded the caller's raw body to the Task
  // Agent signed with the proxy role, so a signed-in user could set `actor_id`, `agent_id`,
  // `agent_test`, `tools`, `s3_bucket`, `ddb_table`, `plan_id`... — every field the Task Agent
  // trusts — and act as anyone. Sub-agents are reached only through the Main Agent's dispatch tool
  // and the plan chainer; the owner-side test console is `/agent-test` (stamped, allowlisted).
  // `unauthRoutes.security.test.ts` pins that `/dispatch` reaches nothing.

  // ===================================================================
  // POST /invoke/:agentId — EXTERNAL invoke surface (Req 5, 6, 8.8, 19).
  //
  // This is the one genuinely new external-facing surface. Unlike the
  // internal Cognito routes above, it is INDEPENDENT of `authMiddleware`
  // (`/invoke` is intentionally NOT in `authPaths`) and instead uses
  // per-agent, bring-your-own OIDC verification resolved from the agent's
  // current published version's `integration_config`. It then SigV4-invokes
  // the SHARED Task Agent runtime with the Owner stamped as the resource
  // actor and the issuer-qualified Caller stamped as a separate Principal.
  //
  // The NFR gates are composed inside `createInvokeHandler` in the fixed
  // middleware order (task 19.1, design §Component 4):
  //   0. kill-switch gate (Req 17) — checked FIRST
  //   1. rate-limit / quota — per-agent pre-check hoisted here (Req 14)
  //   2. resolve agent + current published version + effective external-access
  //      gate (Req 16)
  //   3. SSRF-hardened OIDC verify (Req 12, 13)
  //   4. derive external session/memory key (Req 10) + authoritative per-caller
  //      rate-limit consumption (Req 14)
  //   5. SigV4 dispatch with external opt-in tools (Req 11)
  //   6. SSE stream w/ keepalive (Req 15) + audit (Req 17)
  // The per-agent OIDC verifier is task 10.1 (called via the `verifyAgentToken`
  // seam); the canonical session-key derivation is task 12.3. Every dependency
  // below is injected into that handler in this exact composition.
  // ===================================================================
  const invokeTaskArn = process.env.TASK_AGENT_RUNTIME_ARN ?? "";
  const invokeTaskUrl = invokeTaskArn
    ? `https://bedrock-agentcore.${REGION}.amazonaws.com/runtimes/${encodeURIComponent(invokeTaskArn)}/invocations?qualifier=DEFAULT`
    : "";

  // ---- POST /agent-test — owner-side Custom Agent test invoke ----
  // Internal realm: `/agent-test` IS in `authPaths`, so the Cognito middleware
  // has already populated `req.actorId` by the time this runs. Registered with
  // `app.post` (not `app.use`) so only POST reaches it.
  app.post(
    "/agent-test",
    createAgentTestHandler({ invokeTaskUrl, signedRequestStream }),
  );

  // AssetIndex (single-table) — used to resolve a Custom Agent's current
  // published version by agentId via the `byPublishedId` GSI. The GSI
  // projects ALL attributes (the agent-side `AssetIndex.query_lineage`
  // decodes full rows off it), so the query result carries
  // `integrationConfig`, `ownerUserId`, `model`, etc. directly.
  const assetIndexTable = process.env.ASSET_INDEX_TABLE ?? "";
  const assetIndexPublishedIdIndex =
    process.env.ASSET_INDEX_PUBLISHED_ID_INDEX ?? "byPublishedId";
  const assetIndexDdb = assetIndexTable
    ? DynamoDBDocumentClient.from(new DynamoDBClient({ region: REGION }))
    : null;
  if (assetIndexTable) {
    console.log(
      `[agentcore-proxy] Asset index: ${assetIndexTable} ` +
        `(GSI ${assetIndexPublishedIdIndex}) — external invoke enabled`,
    );
  } else {
    console.warn(
      "[agentcore-proxy] ASSET_INDEX_TABLE not set — POST /invoke/:agentId " +
        "returns 500 internal_error",
    );
  }

  // Resolve a Custom Agent's CURRENT published version by agentId (Req 5.2).
  // Queries the byPublishedId GSI for the whole lineage and returns the single
  // row flagged `current` that is a published, non-deprecated specialty-agent.
  // Returns null when the agent is unknown or has no current published version.
  const resolveCurrentPublishedAgent = async (
    agentId: string,
  ): Promise<Record<string, any> | null> => {
    if (!assetIndexDdb || !assetIndexTable) return null;
    const resp = await assetIndexDdb.send(
      new QueryCommand({
        TableName: assetIndexTable,
        IndexName: assetIndexPublishedIdIndex,
        KeyConditionExpression: "GSI1PK = :pk",
        ExpressionAttributeValues: { ":pk": `PUB#${agentId}` },
      }),
    );
    const items = (resp.Items ?? []) as Record<string, any>[];
    const current = items.find(
      (it) =>
        it.current === true &&
        it.category === "specialty-agent" &&
        it.recordType === "published" &&
        it.deprecated !== true,
    );
    return current ?? null;
  };

  // The route handler itself lives in `./invoke/invokeRoute` as a
  // dependency-injected factory (extracted for isolated integration testing —
  // task 9.4). The AssetIndex/DynamoDB resolver, the per-agent OIDC verifier,
  // and the SigV4 SSE dispatcher are wired in here; behavior is identical to
  // the previously-inline handler.
  // Per-agent / per-caller rate-limit & quota gate (task 14.1, Req 14.1/14.2).
  // In-memory token-bucket + fixed-window quota, per proxy instance; tuned via
  // INVOKE_RL_* env vars (sane defaults otherwise).
  const invokeRateLimiter = createInvokeRateLimiter(process.env);

  // Streaming transport reliability (task 15.1, Req 15). One shared
  // per-instance concurrent-stream guard bounds simultaneous SSE streams
  // (excess → 503); the transport config carries the keepalive cadence and the
  // idle-timeout budget, pre-normalized so edge ≥ proxy ≥ runtime and
  // keepalive < proxy always hold. Both are env-tunable with sane defaults.
  const invokeStreamGuard = streamGuardFromEnv(process.env);
  const invokeTransport = transportConfigFromEnv(process.env);
  console.log(
    `[agentcore-proxy] /invoke transport: keepalive=${invokeTransport.keepaliveMs}ms ` +
      `proxyIdle=${invokeTransport.proxyIdleTimeoutMs}ms edgeIdle=${invokeTransport.edgeIdleTimeoutMs}ms ` +
      `runtime=${invokeTransport.runtimeStreamTimeoutMs}ms maxConcurrentStreams=${invokeStreamGuard.cap}`,
  );

  // Kill switch (task 17.1, Req 17.2/17.3/17.4): checked FIRST in the invoke
  // pipeline. Admin-controlled + independent of Owner action; seeded from
  // INVOKE_KILL_SWITCH_GLOBAL / INVOKE_KILL_SWITCH_AGENTS and mutable at runtime
  // (setGlobal/setAgent) by a future admin control plane.
  const invokeKillSwitch = killSwitchFromEnv(process.env);
  if (invokeKillSwitch.isGloballyDisabled()) {
    console.warn(
      "[agentcore-proxy] /invoke GLOBAL kill switch is ACTIVE — all external " +
        "invocations will be rejected with 503",
    );
  }
  const initiallyDisabledAgents = invokeKillSwitch.disabledAgents();
  if (initiallyDisabledAgents.length > 0) {
    console.warn(
      `[agentcore-proxy] /invoke per-agent kill switch active for: ${initiallyDisabledAgents.join(", ")}`,
    );
  }

  // Structured audit sink (task 17.1, Req 17.1): one JSON line per external
  // invocation (success or denial) under the [agentcore-audit] prefix for
  // log-based ingestion. Records agentId, resolved version, Owner, Caller sub,
  // and outcome — never tokens/credentials (Req 18.2).
  const invokeAuditLogger = createConsoleAuditLogger();

  // CORS preflight for the external invoke surface. Browser clients that send
  // `Authorization` + `Content-Type: application/json` trigger an OPTIONS
  // preflight; Express's default OPTIONS handler answers it WITHOUT CORS
  // headers, so the browser blocks the real POST. Answer it explicitly here so
  // third-party browser apps (bring-your-own-OIDC consoles) can call /invoke.
  // The POST responses already send `access-control-allow-origin: *`.
  app.options("/invoke/:agentId", (_req: any, res: any) => {
    res.writeHead(204, {
      "access-control-allow-origin": "*",
      "access-control-allow-methods": "POST, OPTIONS",
      "access-control-allow-headers": "authorization, content-type",
      "access-control-max-age": "600",
    });
    res.end();
  });

  app.post(
    "/invoke/:agentId",
    createInvokeHandler({
      invokeTaskUrl,
      assetIndexReady: Boolean(assetIndexDdb && assetIndexTable),
      resolveCurrentPublishedAgent,
      verifyAgentToken,
      signedRequestStream,
      rateLimiter: invokeRateLimiter,
      streamGuard: invokeStreamGuard,
      transport: invokeTransport,
      killSwitch: invokeKillSwitch,
      auditLogger: invokeAuditLogger,
    }),
  );

  // ===================================================================
  // GET /invoke/:agentId/jobs — EXTERNAL long-running-workflow tracking.
  //
  // Same OIDC realm as POST /invoke/:agentId (NOT in `authPaths`, no Cognito
  // middleware). Reuses the invoke identity gate and returns ONLY the caller's
  // own jobs, scoped to (Owner actorId, deriveExternalSessionId(agentId, issuer,
  // sub)). Distinct method+path from the invoke POST, so registration order
  // relative to it is irrelevant. The console polls it every few seconds to
  // watch a job to completion.
  // ===================================================================
  app.options("/invoke/:agentId/jobs", (_req: any, res: any) => {
    res.writeHead(204, {
      "access-control-allow-origin": "*",
      "access-control-allow-methods": "GET, OPTIONS",
      "access-control-allow-headers": "authorization, content-type",
      "access-control-max-age": "600",
    });
    res.end();
  });

  // Step Functions client for reading a completed job's outputs directly (no
  // agent turn). Execution ARNs may be cross-region in theory; DescribeExecution
  // is region-scoped, so we parse the ARN's region per-call and cache a client
  // per region. In practice all our trios run in REGION.
  const sfnClients = new Map<string, SFNClient>();
  const sfnClientFor = (region: string): SFNClient => {
    let c = sfnClients.get(region);
    if (!c) { c = new SFNClient({ region }); sfnClients.set(region, c); }
    return c;
  };
  const describeExecution = async (executionArn: string) => {
    // arn:aws:states:<region>:<acct>:execution:<machine>:<name>
    const region = executionArn.split(":")[3] || REGION;
    const resp = await sfnClientFor(region).send(
      new DescribeExecutionCommand({ executionArn }),
    );
    return { status: resp.status, output: resp.output };
  };

  // The Owner's jobs newest-first via the actor-status-index GSI. Shared by `/invoke/:agentId/jobs`
  // (lists the caller's jobs) and `/invoke/:agentId/download` (recognises the caller's own M2C
  // outputs); both narrow the rows to the caller's derived session themselves.
  const queryOwnerJobs = async (actorId: string, limit: number) => {
    if (!jobsDdbClient || !jobRegistryTable) return [];
    const result = await jobsDdbClient.send(
      new QueryCommand({
        TableName: jobRegistryTable,
        IndexName: jobRegistryActorStatusIndex,
        KeyConditionExpression: "actorId = :actor",
        ExpressionAttributeValues: { ":actor": actorId },
        ScanIndexForward: false,
        Limit: limit,
      }),
    );
    return (result.Items ?? []) as Record<string, any>[];
  };

  app.get(
    "/invoke/:agentId/jobs",
    createInvokeJobsHandler({
      assetIndexReady: Boolean(assetIndexDdb && assetIndexTable),
      jobRegistryReady: Boolean(jobsDdbClient && jobRegistryTable),
      resolveCurrentPublishedAgent,
      verifyAgentToken,
      queryActorJobs: queryOwnerJobs,
      // Read SUCCEEDED SFN jobs' outputs straight from Step Functions.
      describeExecution,
    }),
  );

  // ===================================================================
  // POST /invoke/:agentId/uploads — EXTERNAL media upload (presigned PUT).
  //
  // Same OIDC realm as POST /invoke/:agentId (NOT in `authPaths`). Verifies the
  // caller with the identical per-agent OIDC gate, then returns a short-lived
  // presigned S3 PUT scoped to a single `artifacts/<ts>-<name>` key in the
  // upload bucket (the same bucket+prefix the agent reads from). The browser
  // PUTs bytes directly to S3 — they never transit the proxy — then references
  // the returned s3:// URI in its next prompt.
  // ===================================================================
  app.options("/invoke/:agentId/uploads", (_req: any, res: any) => {
    res.writeHead(204, {
      "access-control-allow-origin": "*",
      "access-control-allow-methods": "POST, OPTIONS",
      "access-control-allow-headers": "authorization, content-type",
      "access-control-max-age": "600",
    });
    res.end();
  });

  app.post(
    "/invoke/:agentId/uploads",
    createInvokeUploadHandler({
      assetIndexReady: Boolean(assetIndexDdb && assetIndexTable),
      uploadReady: Boolean(uploadBucket),
      uploadBucket,
      resolveCurrentPublishedAgent,
      verifyAgentToken,
      // Presign a PUT scoped to exactly this key + content-type.
      presignPut: (key: string, contentType: string) =>
        getSignedUrl(
          s3Client,
          new PutObjectCommand({
            Bucket: uploadBucket,
            Key: key,
            ContentType: contentType,
          }),
          { expiresIn: 900 },
        ),
    }),
  );

  // ===================================================================
  // GET /invoke/:agentId/manifest — EXTERNAL render-manifest fetch.
  //
  // Same OIDC realm as POST /invoke/:agentId (NOT in `authPaths`). The external
  // invoke stream (Task Agent) emits `output_ref` frames carrying a render
  // manifest s3_key; the console fetches that manifest here to render the media
  // it describes. Only the manifest JSON needs this route — CloudFront serves no
  // CORS for JSON — so it is streamed same-origin from the artifact bucket. The
  // media `track.src` URLs INSIDE the manifest stay CloudFront (media elements
  // don't enforce CORS for playback). The key is constrained to
  // artifacts/**/render/*.json so a caller can never read arbitrary objects.
  // ===================================================================
  app.options("/invoke/:agentId/manifest", (_req: any, res: any) => {
    res.writeHead(204, {
      "access-control-allow-origin": "*",
      "access-control-allow-methods": "GET, OPTIONS",
      "access-control-allow-headers": "authorization, content-type",
      "access-control-max-age": "600",
    });
    res.end();
  });

  app.get(
    "/invoke/:agentId/manifest",
    createInvokeManifestHandler({
      assetIndexReady: Boolean(assetIndexDdb && assetIndexTable),
      uploadReady: Boolean(uploadBucket),
      resolveCurrentPublishedAgent,
      verifyAgentToken,
      // Load the manifest JSON from the artifact bucket; the handler presigns every artifact URL
      // inside it (so the external console renders media while the artifacts CDN is private) and
      // answers same-origin. The key is already validated to be a render manifest under
      // artifacts/**/render/.
      readObjectText: async (key: string) => {
        const obj = await s3Client.send(
          new GetObjectCommand({ Bucket: uploadBucket, Key: key }),
        );
        return obj.Body ? await obj.Body.transformToString("utf-8") : "";
      },
      presignArtifact: (key: string, expiresIn: number) =>
        defaultPresignGet(REGION)(uploadBucket, key, expiresIn),
      artifactsDomain: process.env.ARTIFACTS_DOMAIN || "",
    }),
  );

  // ===================================================================
  // GET /invoke/:agentId/download — EXTERNAL artifact download (short link).
  //
  // External twin of the Cognito-gated `/download` above. Exists so the agent
  // can hand back ~196-char `?ref=` links instead of ~1,700-char presigned
  // URLs, which blew the per-response output budget and truncated the reply
  // (leaving a SUCCEEDED job with no usable links). Same OIDC realm as
  // `POST /invoke/:agentId`; 302s to a freshly minted presigned URL so bytes
  // never transit this task.
  // ===================================================================
  app.options("/invoke/:agentId/download", (_req: any, res: any) => {
    res.writeHead(204, {
      "access-control-allow-origin": "*",
      "access-control-allow-methods": "GET, OPTIONS",
      "access-control-allow-headers": "authorization, content-type",
      "access-control-max-age": "600",
    });
    res.end();
  });

  app.get(
    "/invoke/:agentId/download",
    createInvokeDownloadHandler({
      assetIndexReady: Boolean(assetIndexDdb && assetIndexTable),
      // Same allowlist as the Cognito `/download`: this app's own
      // uploads/artifacts bucket plus the M2C media-workflow output bucket.
      allowedBuckets: [uploadBucket, mediaProxyBucket].filter(Boolean),
      defaultBucket: uploadBucket,
      mediaBucket: mediaProxyBucket,
      queryActorJobs: queryOwnerJobs,
      resolveCurrentPublishedAgent,
      verifyAgentToken,
      presignGet: (bucket: string, key: string, attach: boolean) =>
        getSignedUrl(
          s3Client,
          new GetObjectCommand({
            Bucket: bucket,
            Key: key,
            ...(attach
              ? {
                  ResponseContentDisposition: `attachment; filename="${
                    key.split("/").pop() || "download"
                  }"`,
                }
              : {}),
          }),
          // Generous window so a long playback can't outlive the credential
          // mid-stream; a new one is minted on the next request.
          { expiresIn: 12 * 60 * 60 },
        ),
    }),
  );

  // /render — invoke the Render Agent to produce an NLE manifest for a
  // media/artifact URL. The frontend calls this when it detects a
  // renderable artifact; the Render Agent probes the media, builds a
  // manifest, writes it to S3, and returns its s3_key. We reshape that
  // into { manifestUrl } (CloudFront) for the NLE timeline renderer.
  // Uses SigV4 (the runtime uses IAM auth).
  // Calls are queued per render session: the Render Agent fails a second invocation on a session
  // that is still busy (a two-video upload composes both tabs at once — session c70967c9).
  const serializeRender = createSerializer();
  app.use("/render", async (req: any, res: any) => {
    if (!renderInvokeUrl) {
      res.writeHead(500, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: "RENDER_AGENT_RUNTIME_ARN not configured" }));
      return;
    }
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => chunks.push(c));
    req.on("end", async () => {
      try {
        const bodyRaw = Buffer.concat(chunks).toString("utf8") || "{}";
        const parsedBody = JSON.parse(bodyRaw);
        // Accept a single `url`, or `urls` for the artifacts of ONE plan node
        // (e.g. a stem-separation node emits 4 wavs that belong on one
        // timeline). Deliberately no `sourceUrl`: supplying one puts the Render
        // Agent on its annotation branch, which renders only that source and
        // drops every other track — that is what previously collapsed a
        // multi-asset render to a single track and broke face bounding boxes.
        const urls: string[] = Array.isArray(parsedBody.urls)
          ? parsedBody.urls.filter((u: unknown): u is string => typeof u === "string" && !!u)
          : (parsedBody.url ? [String(parsedBody.url)] : []);
        const artifactUrl: string = urls[0] ?? "";
        if (!artifactUrl) {
          res.writeHead(400, { "content-type": "application/json" });
          res.end(JSON.stringify({ error: "url or urls is required" }));
          return;
        }
        // Scope the render to the artifact's OWN session so its manifest lands under
        // artifacts/<sid>/render/ (what GET /sessions/:id/renders lists) and can merge — rather
        // than an orphan render-<ts> dir. Prefers a caller session_id, else derives it from the
        // artifact URL (robust to the browser firing the source render before activeSession is set).
        //
        // Both inputs are caller-controlled, so the session must be one the caller OWNS — the rule
        // `/api` applies (`decideRenderSession` → `decideApiSession`): another user's session is a
        // 403, a non-chat id a 400, a locked one 423, and anything not provably the caller's renders
        // into a fresh owner-tagged `render-<tag>-…` session instead. A lookup failure fails CLOSED.
        let renderDecision: Awaited<ReturnType<typeof decideRenderSession>>;
        try {
          renderDecision = await decideRenderSession(
            parsedBody.session_id,
            artifactUrl,
            (req as { actorId?: string }).actorId,
            ddbClient && sessionsTable
              ? async (sid: string) => (await ddbClient.send(new GetCommand({
                  TableName: sessionsTable,
                  Key: { sessionId: sid },
                  ProjectionExpression: "#actor, #locked",
                  ExpressionAttributeNames: { "#actor": "actorId", "#locked": "locked" },
                }))).Item as { actorId?: unknown; locked?: unknown } | undefined
              : null,
          );
        } catch (lookupErr: any) {
          console.error("[agentcore-proxy] /render session ownership lookup failed:", lookupErr?.message);
          res.writeHead(503, { "content-type": "application/json" });
          res.end(JSON.stringify({ error: { code: "session_lookup_failed", message: "Could not verify the session; retry" } }));
          return;
        }
        if (renderDecision.kind === "reject") {
          res.writeHead(renderDecision.status, { "content-type": "application/json" });
          res.end(JSON.stringify({ error: { code: renderDecision.code, message: renderDecision.message } }));
          return;
        }
        const renderSession = renderDecision.sessionId;

        const outbound = Buffer.from(JSON.stringify({
          prompt: urls.length === 1
            ? `Render this artifact: ${artifactUrl}`
            : `Render these artifacts as one unified timeline:\n${urls.join("\n")}`,
          session_id: renderSession,
          render_options: parsedBody.render_options ?? {},
        }));

        const upstream = await serializeRender(renderSession, async () => {
          const invoke = () => signedFetch(renderInvokeUrl, outbound, {
            "x-amzn-bedrock-agentcore-runtime-session-id": renderSession,
          });
          const first = await invoke();
          // 409 = AgentCore RetryableConflictException ("Session operation in progress, please retry"):
          // another caller (not this queue) is using the session. 429/5xx: cold start or throttling.
          if (first.status !== 409 && first.status !== 429 && first.status < 500) return first;
          console.warn(`[agentcore-proxy] /render upstream ${first.status}; retrying once`);
          await new Promise((r) => setTimeout(r, RENDER_RETRY_DELAY_MS));
          return invoke();
        });

        // One outcome from the stream: a manifest, a deliberate "no view", an error record, or nothing.
        const outcome = parseRenderOutcome(upstream.body.toString("utf8"));
        if (outcome.kind === "no_view") {
          // Nothing to show, by design (e.g. data-only JSON). Not a failure: the client drops the placeholder.
          res.writeHead(200, { "content-type": "application/json" });
          res.end(JSON.stringify({ noView: true, reason: outcome.reason }));
          return;
        }
        if (outcome.kind !== "manifest") {
          // An error record is never handed to the client as a manifest (it used to fill the tab with one).
          // 422 = a definitive "this render has no view" (the client drops the tab); 502 stays transport failure.
          res.writeHead(422, { "content-type": "application/json" });
          res.end(JSON.stringify({ error: outcome.kind === "error" ? outcome.code : "Render Agent did not return a manifest" }));
          return;
        }
        const s3Key = outcome.s3Key;
        const manifestError: string | null = null;

        const domain = process.env.ARTIFACTS_DOMAIN ?? "";
        // Return a SAME-ORIGIN proxy URL for the manifest JSON. CloudFront
        // does not send CORS headers, so a browser fetch of the JSON from
        // the app's origin would be blocked. The /api/artifact proxy serves
        // it same-origin with the right content-type. (Media track `src`
        // URLs inside the manifest stay CloudFront — <video>/<audio>
        // elements don't enforce CORS for playback.)
        const manifestUrl = `/api/artifact?key=${encodeURIComponent(s3Key)}`;
        const cloudfrontUrl = domain ? `https://${domain}/${s3Key}` : null;

        res.writeHead(200, { "content-type": "application/json", "access-control-allow-origin": "*" });
        res.end(JSON.stringify({ manifestUrl, cloudfrontUrl, s3Key, error: manifestError }));
      } catch (err: any) {
        console.error("[agentcore-proxy] /render error:", err?.message ?? err);
        res.writeHead(502, { "content-type": "application/json" });
        res.end(JSON.stringify({ error: err instanceof Error ? err.message : "render failed" }));
      }
    });
  });

  // /lab/* — Lab CRUD route. Two modes:
  //   1. Legacy: LAB_SERVICE_RUNTIME_ARN set → proxy to Lab_Service runtime (old).
  //   2. Merged: LAB_SERVICE_RUNTIME_ARN unset → forward the body unchanged
  //      to the Main Agent invoke URL. The Main Agent detects structured
  //      `{action: ...}` payloads and dispatches them through `LabService.handle`
  //      directly (zero-LLM bypass).
  // JWT is passed through unchanged — AgentCore Runtime handles validation.
  const labArn = process.env.LAB_SERVICE_RUNTIME_ARN ?? "";
  const labInvokeUrl = labArn
    ? `https://bedrock-agentcore.${REGION}.amazonaws.com/runtimes/${encodeURIComponent(labArn)}/invocations?qualifier=DEFAULT`
    : invokeUrl; // fall back to Main Agent
  if (labArn) {
    console.log(`[agentcore-proxy] Lab route: legacy → ${labArn}`);
  } else {
    console.log(`[agentcore-proxy] Lab route: merged → Main Agent (${invokeUrl})`);
  }

  app.use("/lab", async (req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => chunks.push(c));
    req.on("end", async () => {
      try {
        const body = Buffer.concat(chunks);

        const authHeader = req.headers["authorization"];
        if (!authHeader || typeof authHeader !== "string") {
          send401(res, "missing_token", "Authorization header is required");
          return;
        }
        const labActorId: string | undefined = (req as any).actorId;
        const labActorGroups: string[] = (req as any).actorGroups ?? [];

        // Stamp the actor id (and group memberships) into the
        // request body. The cloud-managed AgentCore runtime strips
        // every header except `Authorization` and `Baggage` plus
        // the workload-identity token before the SDK gets a chance
        // to allowlist them, so the
        // `X-Amzn-Bedrock-AgentCore-Runtime-Custom-*` path
        // documented in the SDK source doesn't actually apply
        // here. We instead inject `__actor_id` and `__actor_groups`
        // into the JSON payload — the proxy is the only thing that
        // ever writes these fields (after JWT validation), and the
        // body always passes through the proxy, so the trust
        // boundary is preserved. The handler reads them on the
        // agent side via `_resolve_actor_id` (for actor id) and
        // `_is_admin` (for group membership).
        // Every client `__` key is dropped first (see runtimeBody.ts), then identity is stamped.
        const outboundBody: Buffer = prepareRuntimeBody(body, (parsedBody) => {
          if (labActorId) {
            parsedBody.__actor_id = labActorId;
            parsedBody.__actor_groups = labActorGroups;
          }
        }) ?? Buffer.from("{}", "utf8");

        // Session affinity: pin the runtime session PER ACTOR, derived from the
        // JWT-validated `actorId` rather than anything client-supplied.
        //
        // Without a session id AgentCore gives every call a fresh microVM, and
        // the entrypoint runs `_init_agent_subsystems()` +
        // `_ensure_gateway_connected()` *before* reaching the Lab bypass — so a
        // pure DynamoDB list still built the Bedrock model and completed an MCP
        // handshake. Measured on sandbox: ~8.5s cold vs ~190ms on a reused
        // session. Every Capabilities tab paid the former.
        //
        // Derived from the actor, never global or per-process, because
        // `agent/src/main.py` binds the gateway connection's `actor_id` once per
        // container ("a session belongs to one user, so this container serves
        // one actor for its lifetime"). Sharing a session across actors would
        // hand one user a container whose gateway asserts a different actor —
        // the cross-actor tool leak the interceptor exists to prevent. Note the
        // request body's `session_id` is deliberately NOT used: it is
        // client-controlled, so trusting it would let a caller select another
        // actor's container.
        const headers: Record<string, string> = {};
        const labSessionId = labRuntimeSessionId(labActorId);
        if (labSessionId) {
          headers["x-amzn-bedrock-agentcore-runtime-session-id"] = labSessionId;
        }

        // Use SigV4 for the runtime invoke — runtime now uses IAM auth.
        const labResult = await signedFetch(labInvokeUrl, outboundBody, headers);

        res.writeHead(labResult.status, {
          "content-type": labResult.headers["content-type"] ?? "application/json",
          "access-control-allow-origin": "*",
          "cache-control": "no-cache",
          "x-accel-buffering": "no",
        });
        res.end(labResult.body);
      } catch (err: any) {
        console.error("[agentcore-proxy] Lab_Service error:", err.message);
        if (!res.headersSent) {
          res.writeHead(502, { "content-type": "application/json" });
        }
        res.end(JSON.stringify({ error: err.message }));
      }
    });
  });

  // /api/* — proxy to agent runtime (with session affinity, streaming).
  //
  // After user-auth task 7.1 the main agent runtime uses JWT auth, so
  // we forward the caller's Bearer token unchanged — no SigV4 signing.
  // (SigV4 would overwrite the `Authorization` header with its own
  // signature, and the JWT-configured runtime would reject the result
  // with 403.)
  //
  // Session affinity is entirely client-driven: the frontend always
  // sends `session_id` in the request body (see `ChatCanvas.tsx`'s
  // `invokeAgentStreaming({ prompt, session_id: sessionId }, ...)`), so
  // this handler only ever forwards that value. There is intentionally
  // no per-process fallback variable — a same-process fallback would be
  // silently wrong the moment more than one Fargate task (or more than
  // one concurrent user within a single process) exists, since it would
  // leak one user's runtime session id onto another user's request.
  app.use("/api", async (req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => chunks.push(c));
    req.on("end", async () => {
      try {
        const body = Buffer.concat(chunks);
        const parsed = new URL(invokeUrl);

        let effectiveSessionId = "";
        // Proxy-authoritative transcript (docs/design-proxy-authoritative-transcript.md), dark
        // behind TRANSCRIPT_AUTHORITY=proxy: capture the fields needed to persist a clean turn.
        // display_text / attachment / turn_id are additive and absent today — the finalizer falls
        // back to a server-side normalize of the prompt and a generated turn id.
        let turnPrompt = "";
        let turnDisplayText: string | undefined;
        let turnAttachment: { url: string; name: string } | undefined;
        let turnAttachments: { url: string; name: string }[] | undefined;
        let turnId = "";
        try {
          const requestBody = JSON.parse(body.toString());
          if (requestBody.session_id) {
            effectiveSessionId = requestBody.session_id;
          }
          if (typeof requestBody.prompt === "string") turnPrompt = requestBody.prompt;
          if (typeof requestBody.display_text === "string") turnDisplayText = requestBody.display_text;
          if (requestBody.attachment && typeof requestBody.attachment === "object"
              && typeof requestBody.attachment.url === "string" && typeof requestBody.attachment.name === "string") {
            turnAttachment = { url: requestBody.attachment.url, name: requestBody.attachment.name };
          }
          // Multi-file turns: every attachment, in order (capped; url/name strings only).
          if (Array.isArray(requestBody.attachments)) {
            const refs = requestBody.attachments
              .filter((a: any) => a && typeof a.url === "string" && typeof a.name === "string")
              .slice(0, 20)
              .map((a: any) => ({ url: a.url, name: a.name }));
            if (refs.length > 0) {
              turnAttachments = refs;
              turnAttachment = turnAttachment ?? refs[0];
            }
          }
          if (typeof requestBody.turn_id === "string") turnId = requestBody.turn_id;
        } catch { /* not JSON */ }
        if (!turnId) turnId = `t-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
        // Proxy-authoritative transcript is ON BY DEFAULT: the client no longer writes the durable
        // transcript, so the proxy MUST persist every turn or the conversation is lost. The env var
        // is a DEBUG/disable switch only (TRANSCRIPT_AUTHORITY=client turns the writer off); a real
        // rollback is reverting the commit that removed the client writer. Still gated on having the
        // stores configured.
        const finalizeEnabled = process.env.TRANSCRIPT_AUTHORITY !== "client"
          && !!effectiveSessionId && !!ddbClient && !!sessionsTable && !!uploadBucket;

        const apiAuthHeader = req.headers["authorization"];
        if (!apiAuthHeader || typeof apiAuthHeader !== "string") {
          // Auth middleware should have already 401'd, but be defensive.
          send401(res, "missing_token", "Authorization header is required");
          return;
        }
        const apiActorId: string | undefined = (req as any).actorId;
        const userToken: string | undefined = (req as AuthedRequest).userToken;
        const apiActorGroups: string[] = (req as any).actorGroups ?? [];

        // Session ownership + full-freeze gate, one GetItem. `session_id` is client-supplied and it
        // selects BOTH the runtime session (a warm microVM bound to one actor's gateway connection)
        // and the transcript row the proxy appends this turn to — so it must be the caller's own
        // session (or a fresh client-minted id with no row yet). `decideApiSession` holds the rule.
        // A lookup failure fails CLOSED (503, retryable): letting the turn through on a DynamoDB blip
        // would be letting it through unchecked.
        if (effectiveSessionId) {
          let sessionRow: { actorId?: unknown; locked?: unknown } | undefined;
          if (ddbClient && sessionsTable) {
            try {
              const snap = await ddbClient.send(new GetCommand({
                TableName: sessionsTable,
                Key: { sessionId: effectiveSessionId },
                ProjectionExpression: "#actor, #locked",
                ExpressionAttributeNames: { "#actor": "actorId", "#locked": "locked" },
              }));
              sessionRow = snap.Item as typeof sessionRow;
            } catch (lookupErr: any) {
              console.error("[agentcore-proxy] session ownership lookup failed:", lookupErr?.message);
              res.writeHead(503, { "content-type": "application/json", "access-control-allow-origin": "*" });
              res.end(JSON.stringify({ error: { code: "session_lookup_failed", message: "Could not verify the session; retry" } }));
              return;
            }
          }
          const decision = decideApiSession(effectiveSessionId, apiActorId, sessionRow);
          if (decision.kind === "reject") {
            res.writeHead(decision.status, { "content-type": "application/json", "access-control-allow-origin": "*" });
            res.end(JSON.stringify({ error: { code: decision.code, message: decision.message } }));
            return;
          }
          if (decision.locked) {
            res.writeHead(423, { "content-type": "application/json", "access-control-allow-origin": "*" });
            res.end(JSON.stringify({ error: "Session is locked", locked: true }));
            return;
          }
        }

        // Projects turn gate (media-harness-projects-access design Component 2). Chat turns only
        // (`prompt` + `session_id`, no `action`); skipped entirely when Projects are disabled, so the
        // outbound request below is byte-identical to the pre-Projects one (D7). A `reject` starts no
        // turn: `409 project_access_lost` after the gate has moved the session to the Personal_Project.
        let turnGate: TurnGateOutcome = { kind: "skip" };
        if (projectsEnabled()) {
          let gateBody: unknown = null;
          try { gateBody = JSON.parse(body.toString("utf8")); } catch { /* not JSON → skip */ }
          turnGate = await runTurnGate(
            { body: gateBody, actorSub: apiActorId, authorization: apiAuthHeader, sessionId: effectiveSessionId, turnId },
            { sessions: ddbClient && sessionsTable ? dynamoSessionStore(ddbClient, sessionsTable) : null },
          );
          if (turnGate.kind === "reject") {
            res.writeHead(turnGate.status, { "content-type": "application/json", "access-control-allow-origin": "*" });
            res.end(JSON.stringify(turnGate.body));
            return;
          }
        }

        // Stamp actor id and group memberships into the body — see
        // the /lab block above for the rationale (AgentCore strips
        // custom headers despite the SDK's allowlist comment). Both
        // the chat path and the lab bypass path read these from the
        // payload as the primary identity source. `prepareRuntimeBody` first drops EVERY
        // client-sent `__` key (`__media_action`, `__plan_node`, `__turn_context`, ...): the runtime
        // trusts that namespace as proxy-written.
        const outboundBody: Buffer = prepareRuntimeBody(body, (parsedBody) => {
          if (apiActorId) {
            parsedBody.__actor_id = apiActorId;
            parsedBody.__actor_groups = apiActorGroups;
          }
          // Proxy-authored turn context (any client-supplied copy was dropped above). The
          // Project_Token is NOT in it: it travels only in the header.
          if (turnGate.kind === "proceed" && turnGate.turnContext) {
            parsedBody[TURN_CONTEXT_BODY_KEY] = turnGate.turnContext;
          }
        }) ?? Buffer.from("{}", "utf8");

        const headers: Record<string, string> = {
          host: parsed.hostname,
          "content-type": "application/json",
          "content-length": String(outboundBody.length),
        };
        if (effectiveSessionId) {
          headers["x-amzn-bedrock-agentcore-runtime-session-id"] = effectiveSessionId;
        }

        // Stream the agent invoke — pipe upstream chunks to the client as
        // they arrive so SSE steps/text render live. (Previously used the
        // buffering signedFetch, which flushed the whole turn at once.)
        const upstream = await signedRequestStream(invokeUrl, outboundBody, {
          ...(effectiveSessionId ? { "x-amzn-bedrock-agentcore-runtime-session-id": effectiveSessionId } : {}),
          // Forward the end user's validated token so the runtime can present
          // it as inbound auth to the harness gateway, whose authorizer is the
          // user pool. The runtime otherwise has only an actor id stamped in
          // the body, which the gateway cannot verify.
          //
          // Must match the runtime's RequestHeaderAllowlist or it is dropped
          // silently; the prefix is required by AgentCore (see the CDK
          // USER_TOKEN_HEADER comment). The body-stamped actor_id remains the
          // identity for everything else -- this header is only the credential
          // for onward gateway calls.
          ...(userToken ? { [USER_TOKEN_HEADER]: userToken } : {}),
          // The turn's Project_Token (Projects only), in its own allowlisted header and never in the body,
          // so neither the transcript accumulator nor body logging sees it (Req 4.15). Sent on the
          // LOCAL_MAIN_AGENT_URL path too: signedRequestStream forwards these headers unsigned there.
          ...(turnGate.kind === "proceed" && turnGate.projectToken
            ? { [PROJECT_TOKEN_HEADER]: turnGate.projectToken }
            : {}),
        });

        res.writeHead(upstream.statusCode ?? 200, {
          "content-type": (upstream.headers["content-type"] as string) ?? "application/json",
          "access-control-allow-origin": "*",
          "cache-control": "no-cache",
          "x-accel-buffering": "no",
        });

        // Forward chunks live. Stop if the client disconnects mid-stream.
        let clientGone = false;
        // Proxy-authoritative transcript: when the finalizer is enabled, the upstream must DRAIN to
        // completion even if the browser navigates away, so the turn is still persisted (today a
        // client disconnect aborts the upstream — see the else branch). When disabled, preserve the
        // legacy abort-on-disconnect behavior exactly.
        const acc = finalizeEnabled ? new TranscriptAccumulator() : null;
        const turnStartedAt = Date.now();
        let finalized = false;
        // Throttled PROVISIONAL persistence during a long stream, so a mid-turn reload recovers the
        // in-flight turn WITH its steps (the behavior the client's persistProvisional used to give;
        // now server-side). Upsert-by-turnId means the final write REPLACES the provisional — no
        // duplicate. A single in-flight guard + a tracked promise serialize writes so the final one
        // lands LAST (never overwritten by a late provisional).
        let lastProvisionalAt = 0;
        let provisionalInFlight = false;
        let provisionalPromise: Promise<void> = Promise.resolve();
        const writeTurn = (error?: string): Promise<void> => {
          const { text, steps, summary } = acc!.result();
          const { userTurn, assistantTurn } = buildTurnMessages({
            turnId, prompt: turnPrompt, displayText: turnDisplayText, attachment: turnAttachment,
            attachments: turnAttachments,
            text, steps, summary, error, startedAt: turnStartedAt,
          });
          return finalizeTurn({
            s3: s3Client, ddb: ddbClient!, uploadBucket, sessionsTable,
            sessionId: effectiveSessionId, turnId, userTurn, assistantTurn, byteCap: byteCapTranscript,
          });
        };
        const maybeProvisional = () => {
          if (!finalizeEnabled || finalized || provisionalInFlight) return;
          if (Date.now() - lastProvisionalAt < TRANSCRIPT_PROVISIONAL_MS) return;
          const snap = acc!.result();
          if (!snap.text && snap.steps.length === 0) return; // nothing worth persisting yet
          lastProvisionalAt = Date.now();
          provisionalInFlight = true;
          provisionalPromise = writeTurn()
            .catch((e: any) => console.error("[agentcore-proxy] provisional transcript failed:", e?.message))
            .finally(() => { provisionalInFlight = false; });
        };
        const runFinalize = (error?: string) => {
          if (!finalizeEnabled || finalized) return;
          finalized = true; // blocks any new provisional; the final write lands last
          try {
            acc!.finish();
          } catch (e: any) {
            console.error("[agentcore-proxy] transcript accumulate failed:", e?.message);
            return;
          }
          if (notifyGeneratedMedia && turnGate.kind === "proceed" && turnGate.turnContext) {
            const { text, steps, summary } = acc!.result();
            notifyGeneratedMedia({
              sessionId: effectiveSessionId, turnId, projectId: turnGate.turnContext.active_project.id,
              actorSub: apiActorId, startedAt: turnStartedAt, endedAt: Date.now(), text, steps, summary,
            });
          }
          // Wait for any in-flight provisional to settle, then write the authoritative final.
          void provisionalPromise
            .catch(() => {})
            .then(() => writeTurn(error))
            .catch((e: any) => console.error("[agentcore-proxy] transcript finalize failed:", e?.message));
        };
        const onClientClose = () => {
          clientGone = true;
          // Legacy path: abort the upstream read on disconnect. With the finalizer on we keep
          // reading so the turn completes and persists; res.write is guarded by clientGone.
          if (!finalizeEnabled) upstream.destroy();
        };
        req.on("close", onClientClose);

        // Keep-alive. An agentic turn can go minutes between bytes — a single
        // sub-agent dispatch is capped at 300s and routinely runs for several —
        // while the ALB in front of this proxy closes an idle connection after
        // 60s. The turn then completes server-side and the client reports a
        // network error, silently discarding a finished answer. /jobs/stream
        // already guards its own SSE this way.
        //
        // A blank line is the only no-op on the wire: the client splits on \n,
        // skips empty lines, and would otherwise render an unrecognised line as
        // assistant TEXT (see extractText in ui/src/api.ts) — so ": heartbeat"
        // would print into the chat.
        //
        // Only ever written at a record boundary. Injecting a newline while a
        // JSON line is half-transmitted would split it into two unparseable
        // fragments. Not being at a boundary also means bytes just flowed, so
        // the idle timer is already reset and no heartbeat is needed.
        let atLineBoundary = true;
        let lastByteAt = Date.now();
        const heartbeat = setInterval(() => {
          if (clientGone || res.writableEnded) return;
          if (Date.now() - lastByteAt < STREAM_HEARTBEAT_MS) return;
          if (!atLineBoundary) return;
          res.write("\n");
          lastByteAt = Date.now();
        }, STREAM_HEARTBEAT_TICK_MS);
        const stopHeartbeat = () => clearInterval(heartbeat);

        upstream.on("data", (chunk: Buffer) => {
          if (!clientGone && !res.writableEnded) res.write(chunk);
          if (acc) { acc.push(chunk); maybeProvisional(); }
          lastByteAt = Date.now();
          if (chunk.length > 0) atLineBoundary = chunk[chunk.length - 1] === 0x0a;
        });
        upstream.on("end", () => {
          stopHeartbeat();
          req.off("close", onClientClose);
          if (!res.writableEnded) res.end();
          runFinalize();
        });
        upstream.on("error", (streamErr: Error) => {
          stopHeartbeat();
          req.off("close", onClientClose);
          console.error("[agentcore-proxy] Upstream stream error:", streamErr.message);
          if (!res.writableEnded) res.end();
          runFinalize(streamErr.message || "Connection error");
        });
        req.on("close", stopHeartbeat);
      } catch (err: any) {
        console.error("[agentcore-proxy] Error:", err.message);
        if (!res.headersSent) {
          res.writeHead(502, { "content-type": "application/json" });
        }
        res.end(JSON.stringify({ error: err.message }));
      }
    });
  });

  return app;
}
