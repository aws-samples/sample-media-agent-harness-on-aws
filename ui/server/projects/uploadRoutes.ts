// Proxy ingestion WRITE routes (Projects Phase B/C; media-harness-project-ingestion design §3 "Proxy
// ingestion routes", D9). The read routes (list, detail, `assets/urls`) stay in `ingestionRoutes.ts`.
//
// Every route is under `/projects/:pid`, behind the existing `/projects` auth, and runs `checkAccess`
// first: a denial is the uniform `404 {"error":{"code":"project_not_found"}}` before any body is
// interpreted or any S3/Asset_Service call is made.
//
//   GET  /projects/:pid/ingestion/capabilities           → limits, batch size, multipart plan (config-driven)
//   POST /projects/:pid/uploads {files:[{name,size,contentType,folder?}]}  (1..20)
//        → {uploads:[single|multipart grant], errors:{"<index>":{code}}}. Each file gets an asset id
//          `<kind>:<uuid4>` and the key `projects/<pid>/<assetId>/<sanitize(name)>` (the same derivation
//          `register_upload` re-does). Under 100 MiB: one presigned PUT with Content-Type SIGNED. At or
//          above: CreateMultipartUpload + one presigned UploadPart URL per 64 MiB part. URLs live 3600 s.
//          `folder` (design D20) is normalized (`folders.ts`); ANY invalid folder is `400 invalid_folder`
//          for the whole request, before anything is presigned. The key does not change: the folder rides
//          on the object as user metadata `x-amz-meta-harness-folder` (percent-encoded) — signed into a
//          single PUT as a header the grant's `headers` carries (the browser must send every grant header),
//          set server-side on CreateMultipartUpload — and `register_upload` reads it from its HEAD. A grant
//          echoes the normalized `folder` when there is one.
//   POST /projects/:pid/uploads/:assetId/complete {uploadId, parts:[{partNumber, etag}]} → {ok:true}
//   POST /projects/:pid/uploads/:assetId/abort {uploadId}                               → {ok:true}
//        Stateless binding (D9): the proxy keeps no upload table. The upload is found by listing the
//        multipart uploads under `projects/<pid>/<assetId>/` (the key is a function of pid + assetId), so
//        an uploadId of another Project or asset is simply not found.
//   POST /projects/:pid/assets {assetId, fileName, contentType, size, folder?, visibility?, sessionId?} →
//        Asset_Service `register_upload` → the asset view (the shape GET …/assets/:assetId returns); 409
//        object_missing / size_mismatch. An explicit `folder` (400 invalid_folder) overrides the one the upload
//        carries. `visibility: "session"` + `sessionId` is a "this chat only" upload (asset-lineage D2): a
//        hidden asset of that session, filed in its `Sessions/…` folder and never indexed. The session must be
//        the caller's own (404 session_not_found), as for the chat attachment below.
//   POST /projects/:pid/s3-locations {uri} → Asset_Service `register_s3_location`
//        → {registered:[assetView], skipped:[{key, reason}], errors:[{key, code}]}
//   POST /projects/:pid/assets/:assetId/remove → Asset_Service `remove` → {ok:true}
//   POST /projects/:pid/assets/:assetId/retry → Asset_Service `retry` → the asset view (409 not_failed)
//   POST /projects/:pid/ingestion/rebuild → Asset_Service `rebuild_retry` (409 not_rebuild_failed)
//   POST /projects/:pid/ingestion/indexing → Asset_Service `enable_indexing` (owner only; OFF→ON, queues stored files)
//        (the Asset_Service deletes uploaded bytes + the chat copies legacy rows recorded, never an
//        s3-location source).
//   POST /projects/:pid/assets/move {assetIds, folder?} → Asset_Service `move_assets` → {folder, moved, failed}
//        (folder absent/"" = the project root; metadata only — no bytes move, nothing is re-indexed)
//   POST /projects/:pid/assets/create-folder {folder} → Asset_Service `create_folder` → {folder} (an empty folder
//     every member sees; renaming or deleting a folder re-keys these rows)
//   POST /projects/:pid/assets/rename-folder {from, to} → Asset_Service `rename_folder` →
//        {from, to, moved, failed, remaining} (call again while remaining > 0)
//   POST /projects/:pid/assets/resolve-locations {locations} → Asset_Service `resolve_locations` →
//        {locations: {<url>: assetId | null}} (a chat or Renderer URL to the asset registered there)
//   POST /projects/:pid/assets/bookmarks {items: [{assetId, startMs, endMs?, label?}], folder?, sessionId?} →
//        Asset_Service `create_bookmarks` → {folder, created, failed} (a saved moment: no bytes, never indexed)
//   POST /projects/:pid/assets/remove-bookmarks {bookmarkIds} → Asset_Service `remove_bookmarks`
//   POST /projects/:pid/assets/import-status → Asset_Service `import_status` → {bucket, prefix, counts, attention,
//        statusCounts, indexing}: where to copy files for the import inbox and what it did with them
//   POST /projects/:pid/assets/rescan-import → Asset_Service `rescan_import` (sweep the inbox now, every object)
//   POST /projects/:pid/assets/lineage {assetId?, direction?: ancestors|descendants|both|project, depth?, maxNodes?,
//        sessionId?} → Asset_Service `lineage` (read-only; projects.lineageEdges) → {nodes, edges, gaps, stats,
//        truncated, focus?}. Under `/projects`, so it is covered by `authPaths`, `PROTECTED_PREFIXES` and the
//        CloudFront `/projects/*` behavior already: no new path registration.
//   POST /projects/:pid/assets/:assetId/chat-copy {sessionId}
//        → the chat ATTACHMENT REFERENCE for that asset (asset-lineage Phase 2; the path is unchanged):
//          {assetId, projectId, displayName, kind, sizeBytes, url, expiresAt, sourceUri, bucket, key}
//          (+ `name`, `size`, `contentType`, `filename` — the `POST /upload`-shaped aliases the attachment
//          code has always read). NOTHING is copied: `url` is a short-lived presigned GET of the asset's own
//          object, for in-browser preview, and `sourceUri` is what the agent's tools read. The session
//          association is recorded on the ASSET row first (Asset_Service `record_chat_session`, conditioned
//          on the asset not being removed). Refused (404) when the session row exists with another actorId
//          (a missing row is a new chat, allowed).
//
// Identity for the Asset_Service: the caller's Cognito access token (`req.userToken` from the auth
// middleware, else the bearer header). Under `DEV_BYPASS_AUTH` there is none, and the dev-bypass actor
// is passed for the LOCAL Asset_Service only (`assetServiceClient.ts`).
import {
  AbortMultipartUploadCommand,
  CompleteMultipartUploadCommand,
  CreateMultipartUploadCommand,
  DeleteObjectCommand,
  GetObjectCommand,
  ListMultipartUploadsCommand,
  PutObjectCommand,
  S3Client,
  UploadPartCommand,
} from "@aws-sdk/client-s3";
import { getSignedUrl } from "@aws-sdk/s3-request-presigner";
import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import { DynamoDBDocumentClient, GetCommand } from "@aws-sdk/lib-dynamodb";
import { randomUUID } from "node:crypto";
import { readCappedBody } from "../agentTestRoute";
import { PROJECT_NOT_FOUND_BODY, checkAccess as realCheckAccess } from "./access";
import type { AccessResult } from "./access";
import { invokeAssetService as realInvokeAssetService } from "./assetServiceClient";
import type { AssetServiceReply, InvokeAssetService } from "./assetServiceClient";
import {
  ASSET_URL_TTL_S,
  defaultAllowlist,
  defaultIngestBucket,
  defaultM2cBucket,
  defaultStore,
  parseS3Uri,
  publicAssetView,
} from "./ingestionRoutes";
import type { AssetRow } from "./ingestionRoutes";
import {
  MAX_FILES_PER_BATCH,
  MULTIPART_THRESHOLD_BYTES,
  PART_SIZE_BYTES,
  ingestionCapabilities,
  kindFor,
  uploadLimitsFromEnv,
  uploadObjectKey,
} from "./ingestionNames";
import { FOLDER_METADATA_KEY, encodeFolderMetadata, isFolderError, normalizeFolder } from "./folders";
import { ASSET_SK_PREFIX, assetKey, isAssetId, kindFromAssetId, projectPrefix } from "./schema";
import type { TransactStore } from "./store";

/** Presigned upload URLs live this long (contract: 3600 s). */
export const UPLOAD_URL_TTL_S = 3600;
/** A batch of 20 file descriptors is a few KB; anything this large is not a well-formed request. */
const WRITE_MAX_BODY_BYTES = 64 * 1024;
/** Longest accepted file name (bytes are what S3 keys count; the sanitized segment is ≤ 200 chars). */
const MAX_FILE_NAME_LENGTH = 1024;
const MAX_CONTENT_TYPE_LENGTH = 255;
/** S3's multipart limits. */
const MAX_PARTS = 10_000;
/** A chat session id becomes a key segment: the same safe class the Asset_Service's copy-key check uses. */
const SESSION_ID_RE = /^[A-Za-z0-9._-]{1,128}$/;
const DEFAULT_CONTENT_TYPE = "application/octet-stream";

// --------------------------------------------------------------------------------------------- deps

/** The S3 operations these routes perform (the default is the proxy's own `S3Client`). */
export interface UploadS3 {
  /** `metadata` (user metadata, e.g. the folder) is signed as `x-amz-meta-*` HEADERS the PUT must send. */
  presignPut(bucket: string, key: string, contentType: string, expiresIn: number, metadata?: Record<string, string>): Promise<string>;
  createMultipartUpload(bucket: string, key: string, contentType: string, metadata?: Record<string, string>): Promise<string>;
  presignUploadPart(bucket: string, key: string, uploadId: string, partNumber: number, expiresIn: number): Promise<string>;
  listMultipartUploads(bucket: string, prefix: string): Promise<{ key: string; uploadId: string }[]>;
  completeMultipartUpload(bucket: string, key: string, uploadId: string, parts: { partNumber: number; etag: string }[]): Promise<void>;
  abortMultipartUpload(bucket: string, key: string, uploadId: string): Promise<void>;
  deleteObject(bucket: string, key: string): Promise<void>;
  presignGet(bucket: string, key: string, expiresIn: number): Promise<string>;
}

/**
 * The session row: `null` when it cannot be read (fail closed), `{exists:false}` for a new chat. `title` and
 * `createdAt` name the session's `Sessions/…` folder for a "this chat only" upload; both may be absent.
 */
export type SessionOwnerLookup = (
  sessionId: string,
) => Promise<{ exists: boolean; actorId?: string; title?: string; createdAt?: unknown } | null>;

/** A chat's owner, title and its turn ids in order (the lineage view's "Made in chat <title> · turn N"). */
export type SessionTurnsLookup = (
  sessionId: string,
) => Promise<{ actorId?: string; title?: string; turnIds: string[] } | null>;

export interface IngestionWriteDeps {
  checkAccess?: (actorSub: string, projectId: string) => Promise<AccessResult>;
  /** Reader of a chat's owner, title and turn order (lineage chat provenance); defaults to the sessions table. */
  sessionTurns?: SessionTurnsLookup;
  /** Reader of ASSET rows (the chat attachment reference); defaults to the env store. */
  store?: Pick<TransactStore, "batchGet"> | null;
  s3?: UploadS3;
  invokeAssetService?: InvokeAssetService;
  /** The Ingest bucket: uploads and Project objects live here. */
  ingestBucket?: string;
  sourceBucketAllowlist?: string[];
  /** The M2C proxy bucket: an `m2c` asset's bytes stay there (D4) and are attached by reference. */
  m2cBucket?: string;
  sessionOwner?: SessionOwnerLookup;
  env?: NodeJS.ProcessEnv;
  now?: () => number;
  newUuid?: () => string;
}

export interface IngestionWriteContext {
  actor: string;
  pid: string;
  parts: string[];
  method: string;
  /** The caller's Cognito access token, when there is one. */
  userToken?: string;
  /** True under `DEV_BYPASS_AUTH` (no token; the actor is sent to a LOCAL Asset_Service only). */
  devBypass?: boolean;
}

let cachedS3: UploadS3 | undefined;
let cachedSessionOwner: SessionOwnerLookup | undefined;
let cachedSessionTurns: SessionTurnsLookup | undefined;

/** The turn ids of a transcript, in order, once each (`turnId`, else the `<turnId>-u|-a|-x` message id). */
export function turnIdsOf(transcript: unknown): string[] {
  const out: string[] = [];
  for (const m of Array.isArray(transcript) ? transcript : []) {
    const rec = (m ?? {}) as { turnId?: unknown; id?: unknown };
    const id = typeof rec.turnId === "string" ? rec.turnId
      : typeof rec.id === "string" ? rec.id.replace(/-(u|a|x)$/, "") : "";
    if (id && !out.includes(id)) out.push(id);
  }
  return out;
}

function defaultSessionTurns(env: NodeJS.ProcessEnv): SessionTurnsLookup {
  if (!cachedSessionTurns) {
    const table = env.SESSIONS_TABLE ?? "";
    const ddb = table ? DynamoDBDocumentClient.from(new DynamoDBClient({ region: env.AWS_REGION ?? "us-west-2" })) : null;
    cachedSessionTurns = async (sessionId) => {
      if (!ddb || !table) return null;
      try {
        const got = await ddb.send(new GetCommand({
          TableName: table, Key: { sessionId },
          ProjectionExpression: "actorId, #t, transcript", ExpressionAttributeNames: { "#t": "title" },
        }));
        if (!got.Item) return null;
        return {
          ...(typeof got.Item.actorId === "string" ? { actorId: got.Item.actorId } : {}),
          ...(typeof got.Item.title === "string" ? { title: got.Item.title } : {}),
          turnIds: turnIdsOf(got.Item.transcript),
        };
      } catch {
        return null;
      }
    };
  }
  return cachedSessionTurns;
}

/** The default S3 operations over one `S3Client` (the proxy task role), built on first use. */
export function s3UploadOps(client: S3Client): UploadS3 {
  return {
    presignPut: (bucket, key, contentType, expiresIn, metadata) => {
      // `signableHeaders` puts content-type into X-Amz-SignedHeaders: a PUT with any other type fails. User
      // metadata is signed the same way and kept a HEADER (`unhoistableHeaders`): the presigner would
      // otherwise hoist `x-amz-meta-*` into the query string.
      const metaHeaders = Object.keys(metadata ?? {}).map((k) => `x-amz-meta-${k.toLowerCase()}`);
      return getSignedUrl(
        client,
        new PutObjectCommand({ Bucket: bucket, Key: key, ContentType: contentType, ...(metadata ? { Metadata: metadata } : {}) }),
        { expiresIn, signableHeaders: new Set(["content-type", ...metaHeaders]), unhoistableHeaders: new Set(metaHeaders) },
      );
    },
    async createMultipartUpload(bucket, key, contentType, metadata) {
      const r = await client.send(
        new CreateMultipartUploadCommand({ Bucket: bucket, Key: key, ContentType: contentType, ...(metadata ? { Metadata: metadata } : {}) }),
      );
      if (!r.UploadId) throw new Error("CreateMultipartUpload returned no UploadId");
      return r.UploadId;
    },
    presignUploadPart: (bucket, key, uploadId, partNumber, expiresIn) =>
      getSignedUrl(client, new UploadPartCommand({ Bucket: bucket, Key: key, UploadId: uploadId, PartNumber: partNumber }), {
        expiresIn,
      }),
    async listMultipartUploads(bucket, prefix) {
      const out: { key: string; uploadId: string }[] = [];
      let keyMarker: string | undefined;
      let uploadIdMarker: string | undefined;
      for (let page = 0; page < 10; page++) {
        const r = await client.send(
          new ListMultipartUploadsCommand({ Bucket: bucket, Prefix: prefix, KeyMarker: keyMarker, UploadIdMarker: uploadIdMarker }),
        );
        for (const u of r.Uploads ?? []) if (u.Key && u.UploadId) out.push({ key: u.Key, uploadId: u.UploadId });
        if (!r.IsTruncated) break;
        keyMarker = r.NextKeyMarker;
        uploadIdMarker = r.NextUploadIdMarker;
      }
      return out;
    },
    async completeMultipartUpload(bucket, key, uploadId, parts) {
      await client.send(
        new CompleteMultipartUploadCommand({
          Bucket: bucket,
          Key: key,
          UploadId: uploadId,
          MultipartUpload: { Parts: parts.map((p) => ({ PartNumber: p.partNumber, ETag: p.etag })) },
        }),
      );
    },
    async abortMultipartUpload(bucket, key, uploadId) {
      await client.send(new AbortMultipartUploadCommand({ Bucket: bucket, Key: key, UploadId: uploadId }));
    },
    async deleteObject(bucket, key) {
      await client.send(new DeleteObjectCommand({ Bucket: bucket, Key: key }));
    },
    presignGet: (bucket, key, expiresIn) => getSignedUrl(client, new GetObjectCommand({ Bucket: bucket, Key: key }), { expiresIn }),
  };
}

function defaultS3(env: NodeJS.ProcessEnv): UploadS3 {
  if (!cachedS3) {
    // WHEN_REQUIRED: otherwise the SDK signs a CRC32 of an EMPTY body into every presigned PUT/UploadPart
    // URL (`x-amz-checksum-crc32`), and S3 rejects the browser's real bytes.
    const client = new S3Client({
      region: env.AWS_REGION ?? "us-west-2",
      requestChecksumCalculation: "WHEN_REQUIRED",
      responseChecksumValidation: "WHEN_REQUIRED",
    });
    cachedS3 = s3UploadOps(client);
  }
  return cachedS3;
}

/** The default session-owner lookup: a consistent `GetItem` of `SESSIONS_TABLE` (null when unset). */
function defaultSessionOwner(env: NodeJS.ProcessEnv): SessionOwnerLookup {
  if (!cachedSessionOwner) {
    const table = env.SESSIONS_TABLE ?? "";
    const ddb = table ? DynamoDBDocumentClient.from(new DynamoDBClient({ region: env.AWS_REGION ?? "us-west-2" })) : null;
    cachedSessionOwner = async (sessionId) => {
      if (!ddb || !table) return null;
      try {
        const got = await ddb.send(
          new GetCommand({
            TableName: table,
            Key: { sessionId },
            // `title` is how a session's folder is named; both it and `createdAt` are read here, never taken
            // from a request body.
            ProjectionExpression: "actorId, #t, #c",
            ExpressionAttributeNames: { "#t": "title", "#c": "createdAt" },
            ConsistentRead: true,
          }),
        );
        if (!got.Item) return { exists: false };
        return {
          exists: true,
          actorId: typeof got.Item.actorId === "string" ? got.Item.actorId : undefined,
          ...(typeof got.Item.title === "string" ? { title: got.Item.title } : {}),
          ...(got.Item.createdAt !== undefined ? { createdAt: got.Item.createdAt } : {}),
        };
      } catch {
        return null;
      }
    };
  }
  return cachedSessionOwner;
}

/** Forget the memoized defaults; for tests that vary env. */
export function resetIngestionWriteDefaults(): void {
  cachedS3 = undefined;
  cachedSessionOwner = undefined;
}

interface Resolved {
  checkAccess: (actorSub: string, projectId: string) => Promise<AccessResult>;
  store: Pick<TransactStore, "batchGet"> | null;
  s3: () => UploadS3;
  invoke: InvokeAssetService;
  ingestBucket: string;
  allowlist: string[];
  m2cBucket: string;
  sessionOwner: SessionOwnerLookup;
  sessionTurns: SessionTurnsLookup;
  env: NodeJS.ProcessEnv;
  now: () => number;
  newUuid: () => string;
}

function resolve(deps: IngestionWriteDeps): Resolved {
  const env = deps.env ?? process.env;
  return {
    checkAccess: deps.checkAccess ?? realCheckAccess,
    store: deps.store !== undefined ? deps.store : defaultStore(env),
    s3: () => deps.s3 ?? defaultS3(env),
    invoke: deps.invokeAssetService ?? realInvokeAssetService,
    ingestBucket: deps.ingestBucket ?? defaultIngestBucket(env),
    allowlist: deps.sourceBucketAllowlist ?? defaultAllowlist(env),
    m2cBucket: deps.m2cBucket ?? defaultM2cBucket(env),
    sessionOwner: deps.sessionOwner ?? defaultSessionOwner(env),
    sessionTurns: deps.sessionTurns ?? defaultSessionTurns(env),
    env,
    now: deps.now ?? (() => Date.now()),
    newUuid: deps.newUuid ?? (() => randomUUID()),
  };
}

// ------------------------------------------------------------------------------------------ helpers

function sendJson(res: any, status: number, body: unknown): void {
  if (res.headersSent) return;
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify(body));
}

function sendError(res: any, status: number, code: string, message: string): void {
  sendJson(res, status, { error: { code, message } });
}

function sendNotFound(res: any): void {
  sendJson(res, 404, PROJECT_NOT_FOUND_BODY);
}

/** Map an Asset_Service refusal to the HTTP answer (a project denial keeps the uniform body). */
function sendServiceError(res: any, reply: Extract<AssetServiceReply, { ok: false }>): void {
  if (reply.status === 404 && reply.error.code === "project_not_found") {
    sendNotFound(res);
    return;
  }
  sendJson(res, reply.status, { error: { code: reply.error.code, message: reply.error.message } });
}

class BodyError extends Error {
  constructor(readonly status: number, readonly code: string, message: string) {
    super(message);
  }
}

/** The raw body text (read BEFORE `checkAccess`; parsed only after it passes). */
async function readRawBody(req: any): Promise<string> {
  try {
    return (await readCappedBody(req, WRITE_MAX_BODY_BYTES)).toString("utf8").trim();
  } catch (e: any) {
    if (e?.code === "payload_too_large") throw new BodyError(413, "payload_too_large", "Request body too large");
    throw new BodyError(400, "bad_request", "Could not read request body");
  }
}

function parseObject(text: string): Record<string, unknown> {
  if (!text) return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new BodyError(400, "bad_request", "Body must be valid JSON");
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new BodyError(400, "bad_request", "Body must be a JSON object");
  }
  return parsed as Record<string, unknown>;
}

function decodeSegment(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}

const isPositiveInt = (v: unknown): v is number => typeof v === "number" && Number.isSafeInteger(v) && v > 0;

// ------------------------------------------------------------------------------------------ dispatch

type Route =
  | { name: "capabilities" }
  | { name: "uploads" }
  | { name: "complete"; assetId: string }
  | { name: "abort"; assetId: string }
  | { name: "register" }
  | { name: "s3Locations" }
  | { name: "remove"; assetId: string }
  | { name: "retry"; assetId: string }
  | { name: "rebuild" }
  | { name: "indexing" }
  | { name: "chatAttach"; assetId: string }
  | { name: "organize"; command: OrganizeCommand };

type OrganizeCommand = "move_assets" | "rename_folder" | "create_folder" | "resolve_locations" | "create_bookmarks" | "remove_bookmarks"
  | "import_status" | "rescan_import" | "lineage";

// `POST /projects/:pid/assets/<verb>` → the Asset_Service organize command it forwards to. An asset id is
// `<kind>:<uuid>`, so none of these verbs can collide with `assets/:assetId`.
const ORGANIZE_VERBS: Record<string, OrganizeCommand> = {
  move: "move_assets",
  "rename-folder": "rename_folder",
  "create-folder": "create_folder",
  "resolve-locations": "resolve_locations",
  bookmarks: "create_bookmarks",
  "remove-bookmarks": "remove_bookmarks",
  "import-status": "import_status",
  "rescan-import": "rescan_import",
  // Read-only, but a POST like its siblings (a body carries the query). Off-flag the Asset_Service answers
  // 404 lineage_not_enabled.
  lineage: "lineage",
};

function matchRoute(parts: string[], method: string): Route | null {
  const [, a, b, c] = parts;
  const n = parts.length;
  if (method === "GET" && n === 3 && a === "ingestion" && b === "capabilities") return { name: "capabilities" };
  if (method !== "POST") return null;
  if (n === 2 && a === "uploads") return { name: "uploads" };
  if (n === 3 && a === "ingestion" && b === "rebuild") return { name: "rebuild" };
  if (n === 3 && a === "ingestion" && b === "indexing") return { name: "indexing" };
  if (n === 4 && a === "uploads" && c === "complete") return { name: "complete", assetId: decodeSegment(b) };
  if (n === 4 && a === "uploads" && c === "abort") return { name: "abort", assetId: decodeSegment(b) };
  if (n === 2 && a === "assets") return { name: "register" };
  if (n === 3 && a === "assets" && Object.prototype.hasOwnProperty.call(ORGANIZE_VERBS, b)) return { name: "organize", command: ORGANIZE_VERBS[b] };
  if (n === 2 && a === "s3-locations") return { name: "s3Locations" };
  if (n === 4 && a === "assets" && c === "remove") return { name: "remove", assetId: decodeSegment(b) };
  if (n === 4 && a === "assets" && c === "retry") return { name: "retry", assetId: decodeSegment(b) };
  if (n === 4 && a === "assets" && c === "chat-copy") return { name: "chatAttach", assetId: decodeSegment(b) };
  return null;
}

/**
 * Handle a `/projects/:pid/{ingestion,uploads,assets,s3-locations}` write route, or return `false` so
 * `routes.ts` falls through. Returns `true` once a response has been sent.
 */
export async function dispatchIngestionWriteRoute(
  req: any,
  res: any,
  ctx: IngestionWriteContext,
  deps: IngestionWriteDeps = {},
): Promise<boolean> {
  const route = matchRoute(ctx.parts, ctx.method);
  if (!route) return false;
  const d = resolve(deps);
  try {
    const raw = route.name === "capabilities" ? "" : await readRawBody(req);
    const access = await d.checkAccess(ctx.actor, ctx.pid);
    if (!access.ok) {
      sendNotFound(res);
      return true;
    }
    const body = parseObject(raw);
    switch (route.name) {
      case "capabilities":
        sendJson(res, 200, ingestionCapabilities(d.env));
        break;
      case "uploads":
        await handleUploads(res, ctx, d, body);
        break;
      case "complete":
        await handleComplete(res, ctx, d, route.assetId, body);
        break;
      case "abort":
        await handleAbort(res, ctx, d, route.assetId, body);
        break;
      case "register":
        await handleRegister(res, ctx, d, body);
        break;
      case "s3Locations":
        await handleS3Locations(res, ctx, d, body);
        break;
      case "remove":
        await handleRemove(res, ctx, d, route.assetId);
        break;
      case "retry":
        await handleRetry(res, ctx, d, route.assetId);
        break;
      case "rebuild":
        await handleRebuildRetry(res, ctx, d);
        break;
      case "indexing":
        await handleEnableIndexing(res, ctx, d);
        break;
      case "chatAttach":
        await handleChatAttach(res, ctx, d, route.assetId, body);
        break;
      case "organize":
        await handleOrganize(res, ctx, d, route.command, body);
        break;
    }
  } catch (e) {
    if (e instanceof BodyError) {
      sendError(res, e.status, e.code, e.message);
      return true;
    }
    console.error("[projects] ingestion write route error:", (e as Error)?.name ?? "", (e as Error)?.message ?? e);
    sendError(res, 500, "internal_error", "Internal error");
  }
  return true;
}

function serviceCall(ctx: IngestionWriteContext) {
  return {
    projectId: ctx.pid,
    ...(ctx.userToken ? { userToken: ctx.userToken } : {}),
    ...(!ctx.userToken && ctx.devBypass ? { devBypassActor: ctx.actor } : {}),
  };
}

// ------------------------------------------------------------------------------------------- uploads

type UploadGrant =
  | { index: number; assetId: string; key: string; folder?: string; mode: "single"; url: string; headers: Record<string, string> }
  | {
      index: number;
      assetId: string;
      key: string;
      folder?: string;
      mode: "multipart";
      uploadId: string;
      partSize: number;
      parts: { partNumber: number; url: string }[];
    };

async function handleUploads(res: any, ctx: IngestionWriteContext, d: Resolved, body: Record<string, unknown>): Promise<void> {
  const files = body.files;
  if (!Array.isArray(files) || files.length === 0) {
    sendError(res, 400, "bad_request", `files must be an array of 1 to ${MAX_FILES_PER_BATCH} file descriptors`);
    return;
  }
  if (files.length > MAX_FILES_PER_BATCH) {
    sendError(res, 400, "too_many_files", `At most ${MAX_FILES_PER_BATCH} files per batch`);
    return;
  }
  if (!d.ingestBucket) {
    sendError(res, 503, "uploads_not_configured", "Uploads are not configured in this environment");
    return;
  }
  // Folders first: one invalid folder refuses the whole request, before anything is presigned.
  const folders: (string | null)[] = [];
  for (let index = 0; index < files.length; index++) {
    const f = files[index] as { folder?: unknown } | null;
    const folder = normalizeFolder(f && typeof f === "object" ? f.folder : undefined);
    if (isFolderError(folder)) {
      sendJson(res, 400, { error: { code: "invalid_folder", message: `files[${index}].folder is not a valid folder (${folder.error})`, index, reason: folder.error } });
      return;
    }
    folders.push(folder);
  }
  const limits = uploadLimitsFromEnv(d.env);
  const uploads: UploadGrant[] = [];
  const errors: Record<string, { code: string }> = {};
  const s3 = d.s3();

  for (let index = 0; index < files.length; index++) {
    const f = files[index] as { name?: unknown; size?: unknown; contentType?: unknown } | null;
    const name = f && typeof f === "object" ? f.name : undefined;
    const size = f && typeof f === "object" ? f.size : undefined;
    const rawType = f && typeof f === "object" ? f.contentType : undefined;
    if (
      typeof name !== "string" ||
      !name.trim() ||
      name.length > MAX_FILE_NAME_LENGTH ||
      !isPositiveInt(size) ||
      (rawType !== undefined && typeof rawType !== "string") ||
      (typeof rawType === "string" && rawType.length > MAX_CONTENT_TYPE_LENGTH)
    ) {
      errors[String(index)] = { code: "bad_request" };
      continue;
    }
    const contentType = typeof rawType === "string" && rawType.trim() ? rawType.trim() : DEFAULT_CONTENT_TYPE;
    const kind = kindFor(contentType, name);
    if (size > limits[kind]) {
      errors[String(index)] = { code: "too_large" };
      continue;
    }
    const assetId = `${kind}:${d.newUuid()}`;
    const key = uploadObjectKey(ctx.pid, assetId, name);
    const folder = folders[index];
    const metadata = folder ? { [FOLDER_METADATA_KEY]: encodeFolderMetadata(folder) } : undefined;
    const folderField = folder ? { folder } : {};
    if (size < MULTIPART_THRESHOLD_BYTES) {
      const url = await s3.presignPut(d.ingestBucket, key, contentType, UPLOAD_URL_TTL_S, metadata);
      const headers: Record<string, string> = { "Content-Type": contentType };
      if (metadata) headers[`x-amz-meta-${FOLDER_METADATA_KEY}`] = metadata[FOLDER_METADATA_KEY];
      uploads.push({ index, assetId, key, ...folderField, mode: "single", url, headers });
      continue;
    }
    const partCount = Math.ceil(size / PART_SIZE_BYTES);
    if (partCount > MAX_PARTS) {
      errors[String(index)] = { code: "too_large" };
      continue;
    }
    const uploadId = await s3.createMultipartUpload(d.ingestBucket, key, contentType, metadata);
    const parts: { partNumber: number; url: string }[] = [];
    for (let partNumber = 1; partNumber <= partCount; partNumber++) {
      parts.push({ partNumber, url: await s3.presignUploadPart(d.ingestBucket, key, uploadId, partNumber, UPLOAD_URL_TTL_S) });
    }
    uploads.push({ index, assetId, key, ...folderField, mode: "multipart", uploadId, partSize: PART_SIZE_BYTES, parts });
  }
  sendJson(res, 200, { uploads, errors });
}

/** The multipart upload `uploadId` under `projects/<pid>/<assetId>/`, or null (another asset's, or gone). */
async function findUpload(d: Resolved, pid: string, assetId: string, uploadId: string): Promise<{ key: string } | null> {
  const prefix = `${projectPrefix(pid)}${assetId}/`;
  const uploads = await d.s3().listMultipartUploads(d.ingestBucket, prefix);
  const hit = uploads.find((u) => u.uploadId === uploadId && u.key.startsWith(prefix));
  return hit ? { key: hit.key } : null;
}

function readUploadId(body: Record<string, unknown>): string | null {
  const id = body.uploadId;
  return typeof id === "string" && id.length > 0 && id.length <= 1024 ? id : null;
}

async function handleComplete(
  res: any,
  ctx: IngestionWriteContext,
  d: Resolved,
  assetId: string,
  body: Record<string, unknown>,
): Promise<void> {
  if (!isAssetId(assetId)) {
    sendError(res, 404, "upload_not_found", "No such upload");
    return;
  }
  const uploadId = readUploadId(body);
  const rawParts = body.parts;
  if (!uploadId || !Array.isArray(rawParts) || rawParts.length === 0 || rawParts.length > MAX_PARTS) {
    sendError(res, 400, "bad_request", "uploadId and 1 to 10000 parts are required");
    return;
  }
  const parts: { partNumber: number; etag: string }[] = [];
  const seen = new Set<number>();
  for (const p of rawParts) {
    const partNumber = (p as { partNumber?: unknown } | null)?.partNumber;
    const etag = (p as { etag?: unknown } | null)?.etag;
    if (!isPositiveInt(partNumber) || partNumber > MAX_PARTS || seen.has(partNumber) || typeof etag !== "string" || !etag || etag.length > 256) {
      sendError(res, 400, "bad_request", "Each part needs a unique partNumber (1..10000) and an etag");
      return;
    }
    seen.add(partNumber);
    parts.push({ partNumber, etag });
  }
  parts.sort((a, b) => a.partNumber - b.partNumber);
  const upload = await findUpload(d, ctx.pid, assetId, uploadId);
  if (!upload) {
    sendError(res, 404, "upload_not_found", "No such upload");
    return;
  }
  try {
    await d.s3().completeMultipartUpload(d.ingestBucket, upload.key, uploadId, parts);
  } catch (e) {
    // InvalidPart / InvalidPartOrder / EntityTooSmall: the client's parts list is wrong; say which.
    sendError(res, 400, "complete_failed", String((e as Error)?.name || "CompleteMultipartUpload failed"));
    return;
  }
  sendJson(res, 200, { ok: true });
}

async function handleAbort(
  res: any,
  ctx: IngestionWriteContext,
  d: Resolved,
  assetId: string,
  body: Record<string, unknown>,
): Promise<void> {
  const uploadId = readUploadId(body);
  if (!uploadId) {
    sendError(res, 400, "bad_request", "uploadId is required");
    return;
  }
  // Idempotent: an upload that is not (or no longer) there is already aborted.
  if (!isAssetId(assetId)) {
    sendJson(res, 200, { ok: true });
    return;
  }
  const upload = await findUpload(d, ctx.pid, assetId, uploadId);
  if (upload) {
    try {
      await d.s3().abortMultipartUpload(d.ingestBucket, upload.key, uploadId);
    } catch (e) {
      if ((e as Error)?.name !== "NoSuchUpload") throw e;
    }
  }
  sendJson(res, 200, { ok: true });
}

// ------------------------------------------------------------------------- Asset_Service-backed routes

async function handleRegister(res: any, ctx: IngestionWriteContext, d: Resolved, body: Record<string, unknown>): Promise<void> {
  const { assetId, fileName, contentType, size } = body;
  if (typeof assetId !== "string" || typeof fileName !== "string" || typeof contentType !== "string" || !isPositiveInt(size)) {
    sendError(res, 400, "bad_request", "assetId, fileName, contentType and a positive size are required");
    return;
  }
  // An explicit folder overrides the one the upload carries as metadata (absent → the Asset_Service reads it).
  const folder = normalizeFolder(body.folder);
  if (isFolderError(folder)) {
    sendJson(res, 400, { error: { code: "invalid_folder", message: `folder is not a valid folder (${folder.error})`, reason: folder.error } });
    return;
  }
  // "This chat only" (asset-lineage D2): a hidden asset of the caller's own session. The session's title and
  // creation day come from the session ROW, never from the body, so the folder cannot be dictated by a caller.
  let session: { sessionId: string; sessionTitle?: string; sessionCreatedAt?: unknown } | null = null;
  if (body.visibility !== undefined || body.sessionId !== undefined) {
    const sessionId = body.sessionId;
    if (body.visibility !== "session") {
      sendError(res, 400, "bad_request", 'visibility must be "session"');
      return;
    }
    if (typeof sessionId !== "string" || !SESSION_ID_RE.test(sessionId) || sessionId.includes("..")) {
      sendError(res, 400, "bad_request", "sessionId must be 1-128 of [A-Za-z0-9._-]");
      return;
    }
    const owner = await d.sessionOwner(sessionId);
    if (owner === null || (owner.exists && owner.actorId !== ctx.actor)) {
      sendError(res, 404, "session_not_found", "Session not found");
      return;
    }
    session = {
      sessionId,
      ...(owner.title !== undefined ? { sessionTitle: owner.title } : {}),
      ...(owner.createdAt !== undefined ? { sessionCreatedAt: owner.createdAt } : {}),
    };
  }
  // Only these fields reach the Asset_Service; the key is re-derived there, never taken from here.
  const args: Record<string, unknown> = {
    assetId,
    fileName,
    contentType,
    size,
    ...(folder ? { folder } : {}),
    ...(session ? { visibility: "session", ...session } : {}),
  };
  const reply = await d.invoke({
    ...serviceCall(ctx),
    command: "register_upload",
    args,
    ...(session ? { sessionId: session.sessionId } : {}),
  });
  if (!reply.ok) {
    sendServiceError(res, reply);
    return;
  }
  const asset = reply.value.asset as AssetRow | undefined;
  if (!asset || typeof asset !== "object") {
    sendError(res, 502, "asset_service_bad_reply", "The asset service returned no asset");
    return;
  }
  sendJson(res, 200, publicAssetView(asset, assetId));
}

async function handleS3Locations(res: any, ctx: IngestionWriteContext, d: Resolved, body: Record<string, unknown>): Promise<void> {
  const uri = body.uri;
  if (typeof uri !== "string" || !uri.trim() || uri.length > 2048) {
    sendError(res, 400, "bad_request", "uri must be an s3:// location");
    return;
  }
  const reply = await d.invoke({ ...serviceCall(ctx), command: "register_s3_location", args: { location: uri.trim() } });
  if (!reply.ok) {
    sendServiceError(res, reply);
    return;
  }
  const v = reply.value as { registered?: unknown; skipped?: unknown; errors?: unknown };
  const registered = Array.isArray(v.registered) ? (v.registered as AssetRow[]).map((r) => publicAssetView(r)) : [];
  sendJson(res, 200, {
    registered,
    skipped: Array.isArray(v.skipped) ? v.skipped : [],
    errors: Array.isArray(v.errors) ? v.errors : [],
  });
}

async function handleRemove(res: any, ctx: IngestionWriteContext, d: Resolved, assetId: string): Promise<void> {
  if (!isAssetId(assetId)) {
    sendNotFound(res);
    return;
  }
  const reply = await d.invoke({ ...serviceCall(ctx), command: "remove", args: { assetId } });
  if (!reply.ok) {
    if (reply.status === 404) {
      sendNotFound(res); // asset_not_found and project_not_found read the same to the caller
      return;
    }
    sendServiceError(res, reply);
    return;
  }
  const failed = (reply.value as { failed?: unknown }).failed;
  if (Array.isArray(failed) && failed.length > 0) {
    // The asset IS removed; some bytes are not yet deleted. Removing again (idempotent) retries them.
    sendError(res, 502, "cleanup_incomplete", "The asset was removed but some files could not be deleted; try again");
    return;
  }
  sendJson(res, 200, { ok: true });
}

/**
 * `POST /projects/:pid/assets/{move,rename-folder,resolve-locations}` → the matching Asset_Service command. The
 * body is forwarded as the command's args (only its known fields); the Asset_Service validates and normalizes
 * them (ids, folder paths, batch limits), so its 400s reach the caller as they are.
 */
const ORGANIZE_FIELDS: Record<OrganizeCommand, readonly string[]> = {
  move_assets: ["assetIds", "folder"],
  rename_folder: ["from", "to"],
  create_folder: ["folder"],
  resolve_locations: ["locations"],
  create_bookmarks: ["items", "folder", "sessionId"],
  remove_bookmarks: ["bookmarkIds"],
  import_status: [],
  rescan_import: [],
  lineage: ["assetId", "direction", "depth", "maxNodes", "sessionId"],
};

async function handleOrganize(
  res: any,
  ctx: IngestionWriteContext,
  d: Resolved,
  command: OrganizeCommand,
  body: Record<string, unknown>,
): Promise<void> {
  const args: Record<string, unknown> = {};
  for (const f of ORGANIZE_FIELDS[command]) if (body[f] !== undefined) args[f] = body[f];
  const reply = await d.invoke({ ...serviceCall(ctx), command, args });
  if (!reply.ok) {
    sendServiceError(res, reply);
    return;
  }
  if (command === "lineage") {
    sendJson(res, 200, await withChats(reply.value as Record<string, unknown>, ctx.actor, d.sessionTurns));
    return;
  }
  sendJson(res, 200, reply.value);
}

const MAX_LINEAGE_CHATS = 25;

/**
 * Chat provenance for the lineage view: `chats[sessionId] = {title, turns: {turnId: N}}` for the chats THIS actor
 * owns (a link back to the turn that made a file); another member's chat is `{mine: false}` — its title and turns
 * are theirs. Best effort: an unreadable session is simply absent.
 */
export async function withChats(
  graph: Record<string, unknown>, actor: string, lookup: SessionTurnsLookup,
): Promise<Record<string, unknown>> {
  const ids = new Set<string>();
  for (const list of [graph.nodes, graph.edges]) {
    for (const x of Array.isArray(list) ? list : []) {
      const sid = (x as { sessionId?: unknown })?.sessionId;
      if (typeof sid === "string" && sid) ids.add(sid);
    }
  }
  const chats: Record<string, { mine: boolean; title?: string; turns?: Record<string, number> }> = {};
  await Promise.all([...ids].slice(0, MAX_LINEAGE_CHATS).map(async (sid) => {
    const s = await lookup(sid).catch(() => null);
    if (!s) return;
    if (s.actorId !== actor) { chats[sid] = { mine: false }; return; }
    chats[sid] = { mine: true, ...(s.title ? { title: s.title } : {}),
      turns: Object.fromEntries(s.turnIds.map((t, i) => [t, i + 1])) };
  }));
  return { ...graph, chats };
}

/**
 * `POST /projects/:pid/assets/:assetId/retry` → Asset_Service `retry`: a `failed` asset goes back to `queued`
 * and the pipeline reruns only what failed (Req 7.10-7.13). 409 `not_failed` names the current status.
 */
async function handleRetry(res: any, ctx: IngestionWriteContext, d: Resolved, assetId: string): Promise<void> {
  if (!isAssetId(assetId)) {
    sendNotFound(res);
    return;
  }
  const reply = await d.invoke({ ...serviceCall(ctx), command: "retry", args: { assetId } });
  if (!reply.ok) {
    if (reply.status === 404) {
      sendNotFound(res);
      return;
    }
    sendServiceError(res, reply);
    return;
  }
  const asset = (reply.value as { asset?: AssetRow }).asset;
  sendJson(res, 200, asset ? publicAssetView(asset) : { ok: true });
}

/**
 * `POST /projects/:pid/ingestion/rebuild` → Asset_Service `rebuild_retry`: only from `rebuild_failed`, a new
 * Collection_Rebuild from step (a) (Req 10.12, 10.13). 409 `not_rebuild_failed` names the current state.
 */
async function handleRebuildRetry(res: any, ctx: IngestionWriteContext, d: Resolved): Promise<void> {
  const reply = await d.invoke({ ...serviceCall(ctx), command: "rebuild_retry", args: {} });
  if (!reply.ok) {
    if (reply.status === 404) {
      sendNotFound(res);
      return;
    }
    sendServiceError(res, reply);
    return;
  }
  sendJson(res, 200, reply.value);
}

/**
 * `POST /projects/:pid/ingestion/indexing` → Asset_Service `enable_indexing`: turn the Project's indexing ON
 * (owner only; it can never be turned off) and queue every stored file. Idempotent: on a Project already ON
 * it only queues stored files that were left, so the UI may simply retry. 403 `owner_only`; 503
 * `catalog_missing` when M2C lacks a workflow the stored files need (nothing is changed then).
 */
async function handleEnableIndexing(res: any, ctx: IngestionWriteContext, d: Resolved): Promise<void> {
  const reply = await d.invoke({ ...serviceCall(ctx), command: "enable_indexing", args: {} });
  if (!reply.ok) {
    if (reply.status === 404) {
      sendNotFound(res);
      return;
    }
    sendServiceError(res, reply);
    return;
  }
  sendJson(res, 200, reply.value);
}

// ------------------------------------------------------------------------------- chat attachment reference

async function readAsset(d: Resolved, pid: string, assetId: string): Promise<AssetRow | null> {
  if (!d.store) return null;
  const rows = await d.store.batchGet([assetKey(pid, assetId)]);
  const row = rows.find((r) => r.SK === ASSET_SK_PREFIX + assetId);
  return (row as AssetRow | undefined) ?? null;
}

/**
 * Whether an asset's `sourceUri` bucket may be presigned for a chat attachment: the Ingest bucket, an
 * allowlisted source bucket, or — for an `m2c` asset only — the M2C proxy bucket, whose bytes stay where M2C
 * wrote them (asset-lineage D4) and so can only ever be attached by reference. Mirrors `bucketAllowed` in
 * `ingestionRoutes.ts`, which guards the same signing for the Asset_URL_Route.
 */
function attachableBucket(bucket: string, row: AssetRow, d: Resolved): boolean {
  if (d.ingestBucket && bucket === d.ingestBucket) return true;
  if (d.m2cBucket && bucket === d.m2cBucket && row.source === "m2c") return true;
  return d.allowlist.includes(bucket);
}

async function handleChatAttach(
  res: any,
  ctx: IngestionWriteContext,
  d: Resolved,
  assetId: string,
  body: Record<string, unknown>,
): Promise<void> {
  const sessionId = body.sessionId;
  if (typeof sessionId !== "string" || !SESSION_ID_RE.test(sessionId) || sessionId.includes("..")) {
    sendError(res, 400, "bad_request", "sessionId must be 1-128 of [A-Za-z0-9._-]");
    return;
  }
  if (!isAssetId(assetId) || !kindFromAssetId(assetId) || !d.ingestBucket) {
    sendNotFound(res);
    return;
  }
  const row = await readAsset(d, ctx.pid, assetId);
  const src = row ? parseS3Uri(row.sourceUri) : null;
  if (!row || row.status === "removed" || !src) {
    sendNotFound(res);
    return;
  }
  if (!attachableBucket(src.bucket, row, d)) {
    sendNotFound(res);
    return;
  }
  // A "this chat only" asset belongs to ONE chat (asset-lineage D2): it is never attachable in another.
  if (row.visibility === "session" && row.sessionId !== sessionId) {
    sendNotFound(res);
    return;
  }
  const owner = await d.sessionOwner(sessionId);
  if (owner === null || (owner.exists && owner.actorId !== ctx.actor)) {
    // Unreadable (fail closed) or another actor's session: never hand out a URL for someone else's chat.
    sendError(res, 404, "session_not_found", "Session not found");
    return;
  }

  // Record the association BEFORE the URL is handed out, so an asset always knows which chats reference it
  // (and the record is conditioned on the asset not being removed, which is the authorization to read it).
  const recorded = await d.invoke({
    ...serviceCall(ctx),
    command: "record_chat_session",
    args: { assetId, sessionId },
    sessionId,
  });
  if (!recorded.ok) {
    if (recorded.status === 404) {
      sendNotFound(res);
      return;
    }
    sendServiceError(res, recorded);
    return;
  }

  const url = await d.s3().presignGet(src.bucket, src.key, ASSET_URL_TTL_S);
  const name = typeof row.displayName === "string" ? row.displayName : src.key.slice(src.key.lastIndexOf("/") + 1);
  const sizeBytes = typeof row.sourceSize === "number" ? row.sourceSize : Number(row.sourceSize ?? 0);
  const contentType = typeof row.contentType === "string" ? row.contentType : DEFAULT_CONTENT_TYPE;
  sendJson(res, 200, {
    // The identity the chat turn carries (asset-lineage R1.1): the asset, never a URL.
    assetId,
    projectId: ctx.pid,
    displayName: name,
    kind: kindFromAssetId(assetId),
    sizeBytes,
    // Short-lived presigned GET of the asset's own object: in-browser preview and generators that need https.
    url,
    expiresAt: Math.floor(d.now() / 1000) + ASSET_URL_TTL_S,
    // The asset's real location, for the agent's tools (an s3:// URI it can read directly).
    sourceUri: `s3://${src.bucket}/${src.key}`,
    bucket: src.bucket,
    key: src.key,
    // The `POST /upload`-shaped aliases the attachment code has always read.
    name,
    size: sizeBytes,
    contentType,
    filename: name,
  });
}
