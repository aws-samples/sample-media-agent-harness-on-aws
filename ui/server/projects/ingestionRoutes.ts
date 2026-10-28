// Proxy ingestion asset read/URL routes (media-harness-project-ingestion design §3 "Proxy ingestion
// routes" and §18 "ArtifactsCdn refusal and Asset_URL_Route"). These are the REAL routes that replace the
// dev-only stand-in (`devAssetRoutes.ts`, now deleted): they run in BOTH a deployed environment and a
// local moto stack, so they carry no registration guard.
//
// Dispatched from `routes.ts` for `/projects/:pid/assets*` (P0 slice):
//
//   POST /projects/:pid/assets/urls {asset_ids}   (the Asset_URL_Route, Req 11.10–11.12; at most 100 ids)
//     `checkAccess(actor, pid)` (uniform 404 on denial), a consistent `BatchGetItem` of the `ASSET#<id>`
//     rows for the well-formed ids, and for each found row (any status) a presigned GET of its `sourceUri`
//     for 3,600 s — and, for an image, of its `rendition.key` too. A source whose bucket is the Ingest
//     bucket or an allowlisted bucket is signed, and so is an `m2c` asset's source in the M2C proxy bucket
//     (`MEDIA_PROXY_BUCKET`; asset-lineage R4.1: M2C outputs are referenced where M2C wrote them); any other
//     source, and every malformed or absent id, gets the per-id `project_not_found` error.
//     Response: `{urls: {assetId: {source, rendition?, folder}}, errors}` (`folder` "" = the top level).
//
//   GET /projects/:pid/assets/:assetId   (asset status, Req 3.3, 3.11)
//     `checkAccess`, then a consistent read of `ASSET#<assetId>`; a denied, malformed, or absent asset is
//     the uniform 404. Returns the asset's client-facing view (`assetId`, `kind`, `displayName`, `status`,
//     and pass-through fields), never the raw S3 location.
//
//   GET /projects/:pid/assets?cursor=   (asset list, Req 3.15, 7.1)
//     `checkAccess`, then a page of the `assetsByRegistered` GSI (newest first, 50 per page). Returns
//     `{assets, cursor?}`; `cursor` is an opaque continuation token. Each view carries `folder` when the
//     asset is in one (design D20); `?q=` matches the folded `folder + "/" + displayName`, RANKED by
//     relevance (exact Display_Name > prefix > substring > folder, newest-first within a tier) and PAGED by
//     an opaque `cursor` so every match is reachable; `total` is the ranked match count, `truncated` means
//     the project is larger than the scan bound.
//
//   POST /projects/:pid/assets/duplicates {files:[{name, size}]}   (1..1000 entries; design D20)
//     `checkAccess` (uniform 404), then ONE newest-first pass over the same GSI. For each file: the
//     Display_Name base the registry would derive (`displayNames.ts`, twin of `derive_display_name`), and
//     a live asset whose Display_Name — as is, or without a trailing ` (n)` — equals it case-insensitively
//     AND whose `sourceSize` equals `size`; the unsuffixed original is preferred over a ` (n)` copy.
//     → `{duplicates:[{index, asset}], truncated?}` (`truncated` only when the pass stopped at its cap).
//
// Local/deployed parity: the default store comes from `dynamoTransactStoreFromEnv`, which honors
// `PROJECTS_DDB_ENDPOINT` (loopback → moto), and URLs are signed with the proxy's own AWS credentials
// (the same `S3Client` the `/upload` route uses) — exactly as the stand-in did, so local citation/Moment
// testing keeps working after the stand-in is removed. The GSI is eventually consistent, which is safe
// because `checkAccess` runs first on the base table and no access decision reads the index (design §2).
import { GetObjectCommand, S3Client } from "@aws-sdk/client-s3";
import { getSignedUrl } from "@aws-sdk/s3-request-presigner";
import { readCappedBody } from "../agentTestRoute";
import { PROJECT_NOT_FOUND_BODY, checkAccess as realCheckAccess } from "./access";
import type { AccessResult } from "./access";
import { PROJECT_NOT_FOUND } from "./registry";
import {
  ASSET_LIST_INDEX,
  ASSET_LIST_PK_ATTR,
  ASSET_LIST_SK_ATTR,
  ASSET_SK_PREFIX,
  assetKey,
  isAssetId,
  projectPk,
} from "./schema";
import { displayBase, deriveDisplayName, withoutCopySuffix } from "./displayNames";
import { folderPathText, isFolderError, normalizeFolder } from "./folders";
import { kindFor } from "./ingestionNames";
import { dynamoTransactStoreFromEnv } from "./store";
import type { IndexQueryStore, Item, TransactStore } from "./store";

/** The presigned-GET lifetime, in seconds (ingestion Req 11.10). */
export const ASSET_URL_TTL_S = 3600;

/** The most ids one `POST …/assets/urls` call accepts (the client's `assetUrlBatcher` chunks to this). */
export const ASSET_URL_MAX_IDS = 100;

/** The Assets-tab page size for the list route (design §3 `Limit 50`). */
export const ASSET_LIST_PAGE = 50;

/** Longest accepted `assets/urls` body: 100 ids of ~64 chars plus JSON overhead is well under this. */
const ASSET_URLS_MAX_BODY_BYTES = 64 * 1024;

/** Sign one GET; the proxy's credentials are used (injected `presignGet`, default builds an `S3Client`). */
export type PresignGet = (bucket: string, key: string, expiresIn: number) => Promise<string>;

/** The store the asset read routes need: a consistent `batchGet` and a GSI `queryIndex`. */
export type AssetReadStore = Pick<TransactStore, "batchGet" | "query"> & IndexQueryStore;

export interface AssetRouteDeps {
  /** Access check; defaults to the real `checkAccess` over the configured store. */
  checkAccess?: (actorSub: string, projectId: string) => Promise<AccessResult>;
  /** Reader of ASSET rows and the GSI; defaults to a store built from env (honors `PROJECTS_DDB_ENDPOINT`). */
  store?: AssetReadStore | null;
  /** Presigner; defaults to the proxy's own `S3Client` GET presign. */
  presignGet?: PresignGet;
  /** The Ingest bucket (renditions live here); defaults to `UPLOAD_BUCKET` or `SKILLS_S3_BUCKET`. */
  ingestBucket?: string;
  /** Extra buckets a `sourceUri` may live in (s3-location assets); defaults to env, else none. */
  sourceBucketAllowlist?: string[];
  /** The M2C proxy bucket an `m2c` asset's source lives in; defaults to `MEDIA_PROXY_BUCKET`, else none. */
  m2cBucket?: string;
  /** Clock, in epoch ms (injectable for tests). */
  now?: () => number;
}

/** The ingestion asset-row fields these routes read. Extra fields pass through the client views. */
export interface AssetRow {
  assetId?: unknown;
  kind?: unknown;
  status?: unknown;
  displayName?: unknown;
  sourceUri?: unknown;
  rendition?: unknown;
  [extra: string]: unknown;
}

/** One presigned GET: `expiresAt` is epoch seconds (the signer's credential floor). */
interface PresignedUrl {
  url: string;
  expiresAt: number;
}

/** One `urls` entry of the Asset_URL_Route: the source bytes, plus a `rendition` for an image. */
interface AssetUrlEntry {
  source: PresignedUrl;
  rendition?: PresignedUrl;
  /** Where the file is filed (`""` = the top level), so every view that shows it can say where it lives. */
  folder: string;
}

/** Parse `s3://bucket/key` into its parts, or `null` when it is not a well-formed s3 URI. */
export function parseS3Uri(uri: unknown): { bucket: string; key: string } | null {
  if (typeof uri !== "string" || !uri.startsWith("s3://")) return null;
  const rest = uri.slice("s3://".length);
  const slash = rest.indexOf("/");
  if (slash <= 0) return null;
  const bucket = rest.slice(0, slash);
  const key = rest.slice(slash + 1);
  if (!bucket || !key) return null;
  return { bucket, key };
}

/** `expiresAt` the client expects: epoch SECONDS at the end of the signing window. */
function expiresAtSeconds(nowMs: number): number {
  return Math.floor(nowMs / 1000) + ASSET_URL_TTL_S;
}

function sendJson(res: any, status: number, body: unknown): void {
  if (res.headersSent) return;
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify(body));
}

/** The uniform `404 {"error":{"code":"project_not_found",…}}` every whole-route denial returns (Req 1.17). */
function sendNotFound(res: any): void {
  sendJson(res, 404, PROJECT_NOT_FOUND_BODY);
}

function decodeSegment(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s; // left encoded: matches no asset id, so it reads as not-found
  }
}

// ------------------------------------------------------------------------------- default dependencies

let cachedPresignGet: PresignGet | undefined;
let cachedStore: AssetReadStore | null | undefined;

/** The default presigner: the proxy's own `S3Client` (its AWS credentials), built once. */
function defaultPresignGet(env: NodeJS.ProcessEnv): PresignGet {
  if (!cachedPresignGet) {
    const client = new S3Client({ region: env.AWS_REGION ?? "us-west-2" });
    cachedPresignGet = (bucket, key, expiresIn) =>
      getSignedUrl(client, new GetObjectCommand({ Bucket: bucket, Key: key }), { expiresIn });
  }
  return cachedPresignGet;
}

/** The default store from env (same table/endpoint as the registry), built once. */
export function defaultStore(env: NodeJS.ProcessEnv = process.env): AssetReadStore | null {
  if (cachedStore === undefined) cachedStore = dynamoTransactStoreFromEnv(env);
  return cachedStore;
}

/** The Ingest bucket: the shared Skills/Ingest bucket the `/upload` route uses. */
export function defaultIngestBucket(env: NodeJS.ProcessEnv = process.env): string {
  return env.UPLOAD_BUCKET || env.SKILLS_S3_BUCKET || "";
}

/** Extra source buckets (s3-location assets) from `PROJECT_ASSET_SOURCE_BUCKETS` (comma-separated). */
export function defaultAllowlist(env: NodeJS.ProcessEnv = process.env): string[] {
  return (env.PROJECT_ASSET_SOURCE_BUCKETS ?? "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

/** The M2C proxy bucket (`MEDIA_PROXY_BUCKET`), where an `m2c` asset's source stays (asset-lineage D4). */
export function defaultM2cBucket(env: NodeJS.ProcessEnv = process.env): string {
  return (env.MEDIA_PROXY_BUCKET ?? "").trim();
}

/** Forget the memoized default presigner/store; for tests that vary env. */
export function resetAssetRouteDefaults(): void {
  cachedPresignGet = undefined;
  cachedStore = undefined;
}

interface ResolvedDeps {
  checkAccess: (actorSub: string, projectId: string) => Promise<AccessResult>;
  store: AssetReadStore | null;
  presignGet: PresignGet;
  ingestBucket: string;
  allowlist: string[];
  m2cBucket: string;
  now: () => number;
}

function resolveDeps(deps: AssetRouteDeps): ResolvedDeps {
  const env = process.env;
  return {
    checkAccess: deps.checkAccess ?? realCheckAccess,
    store: deps.store !== undefined ? deps.store : defaultStore(env),
    presignGet: deps.presignGet ?? defaultPresignGet(env),
    ingestBucket: deps.ingestBucket ?? defaultIngestBucket(env),
    allowlist: deps.sourceBucketAllowlist ?? defaultAllowlist(env),
    m2cBucket: deps.m2cBucket ?? defaultM2cBucket(env),
    now: deps.now ?? (() => Date.now()),
  };
}

// ------------------------------------------------------------------------------------------ dispatch

/** The parsed `/projects`-relative request the dispatcher acts on. `parts[1]` is already `"assets"`. */
export interface AssetRouteContext {
  actor: string;
  pid: string;
  parts: string[];
  method: string;
}

/**
 * Handle a `/projects/:pid/assets*` request, or return `false` to let `routes.ts` fall through (an
 * asset subroute this P0 slice does not implement, e.g. `POST …/assets` registration). Returns `true`
 * once it has sent a response.
 */
export async function dispatchAssetRoute(
  req: any,
  res: any,
  ctx: AssetRouteContext,
  deps: AssetRouteDeps = {},
): Promise<boolean> {
  const { parts, method, actor, pid } = ctx;
  if (parts[1] !== "assets") return false;

  const isUrls = method === "POST" && parts.length === 3 && parts[2] === "urls";
  // `assets/folders` is the folder list; an asset id is `<kind>:<uuid>`, so it can never be named `folders`.
  const isFolders = method === "GET" && parts.length === 3 && parts[2] === "folders";
  const isBookmarks = method === "GET" && parts.length === 3 && parts[2] === "bookmarks";
  const isDetail = method === "GET" && parts.length === 3 && parts[2] !== "urls" && !isFolders && !isBookmarks;
  const isList = method === "GET" && parts.length === 2;
  const isDuplicates = method === "POST" && parts.length === 3 && parts[2] === "duplicates";
  if (!isUrls && !isDetail && !isList && !isDuplicates && !isFolders && !isBookmarks) return false;

  const d = resolveDeps(deps);
  try {
    if (!d.store) {
      // No store reachable (PROJECTS_TABLE unset): behave as a whole-route denial.
      sendNotFound(res);
      return true;
    }
    const store = d.store; // narrowed non-null for the handlers
    if (isDuplicates) {
      await handleDuplicates(req, res, { actor, pid, store, checkAccess: d.checkAccess });
    } else if (isUrls) {
      await handleAssetUrls(req, res, {
        actor,
        pid,
        store,
        presignGet: d.presignGet,
        ingestBucket: d.ingestBucket,
        allowlist: d.allowlist,
        m2cBucket: d.m2cBucket,
        now: d.now,
        checkAccess: d.checkAccess,
      });
    } else if (isFolders) {
      await handleFolderList(res, { actor, pid, store, checkAccess: d.checkAccess });
    } else if (isBookmarks) {
      await handleBookmarkList(req, res, { actor, pid, store, checkAccess: d.checkAccess });
    } else if (isDetail) {
      await handleAssetDetail(res, { actor, pid, assetId: decodeSegment(parts[2]), store, checkAccess: d.checkAccess });
    } else {
      await handleAssetList(req, res, { actor, pid, store, checkAccess: d.checkAccess });
    }
  } catch (e) {
    console.error("[projects] asset route error:", (e as Error)?.message ?? e);
    sendJson(res, 500, { error: { code: "internal_error", message: "Internal error" } });
  }
  return true;
}

/**
 * One project asset's row, its canonical `s3://` source and a presigned GET of it — the same bucket rules as
 * `assets/urls` (`signAsset`), so a caller can never be handed a URL that route would refuse. `null` for a
 * missing, removed or unsignable asset. The CALLER checks the actor's access to `pid` first.
 */
export async function readAssetSource(
  pid: string,
  assetId: string,
  deps: AssetRouteDeps = {},
): Promise<{ row: AssetRow; sourceUri: string; url: string } | null> {
  if (!isAssetId(assetId)) return null;
  const d = resolveDeps(deps);
  if (!d.store) return null;
  const [row] = (await d.store.batchGet([assetKey(pid, assetId)])) as (AssetRow | undefined)[];
  if (!row || row.status === "removed" || typeof row.sourceUri !== "string") return null;
  const entry = await signAsset(row, {
    actor: "", pid, store: d.store, presignGet: d.presignGet, ingestBucket: d.ingestBucket, allowlist: d.allowlist,
    m2cBucket: d.m2cBucket, now: d.now, checkAccess: d.checkAccess,
  });
  return entry ? { row, sourceUri: row.sourceUri, url: entry.source.url } : null;
}

// ---------------------------------------------------------------------------------- Asset_URL_Route

interface UrlsCtx {
  actor: string;
  pid: string;
  store: AssetReadStore;
  presignGet: PresignGet;
  ingestBucket: string;
  allowlist: string[];
  m2cBucket: string;
  now: () => number;
  checkAccess: (actorSub: string, projectId: string) => Promise<AccessResult>;
}

/**
 * Whether a row's `sourceUri` bucket may be signed: the Ingest bucket, an allowlisted one, or — for an
 * `m2c` asset only — the M2C proxy bucket. No other row can name that bucket and get a signed URL.
 */
function bucketAllowed(bucket: string, row: AssetRow, ctx: UrlsCtx): boolean {
  if (ctx.ingestBucket && bucket === ctx.ingestBucket) return true;
  if (ctx.m2cBucket && bucket === ctx.m2cBucket && row.source === "m2c") return true;
  return ctx.allowlist.includes(bucket);
}

/** Presign the source (and, for an image or a video, its rendition / hero image), or `null` to refuse. */
async function signAsset(row: AssetRow, ctx: UrlsCtx): Promise<AssetUrlEntry | null> {
  const src = parseS3Uri(row.sourceUri);
  if (!src || !bucketAllowed(src.bucket, row, ctx)) return null;
  const expiresAt = expiresAtSeconds(ctx.now());
  const url = await ctx.presignGet(src.bucket, src.key, ASSET_URL_TTL_S);
  const entry: AssetUrlEntry = { source: { url, expiresAt }, folder: typeof row.folder === "string" ? row.folder.trim() : "" };
  const rendition = row.rendition as { key?: unknown } | undefined;
  // A video's hero image is a captured frame the pipeline copied to the same rendition key an image uses.
  // The key must sit under this asset's own prefix: only the pipeline writes it, but never sign anything else.
  const rKey = rendition && typeof rendition.key === "string" ? rendition.key : "";
  if ((row.kind === "image" || row.kind === "video") && ctx.ingestBucket && rKey
      && rKey.startsWith("projects/") && rKey.endsWith(`/${String(row.assetId)}/rendition.jpg`)) {
    const rUrl = await ctx.presignGet(ctx.ingestBucket, rKey, ASSET_URL_TTL_S);
    entry.rendition = { url: rUrl, expiresAt };
  }
  return entry;
}

/** `POST /projects/:pid/assets/urls` — access check, consistent read, per-id presign or `project_not_found`. */
async function handleAssetUrls(req: any, res: any, ctx: UrlsCtx): Promise<void> {
  const ids = await readAssetIds(req, res);
  if (ids === null) return; // a 4xx was already sent

  const access = await ctx.checkAccess(ctx.actor, ctx.pid);
  if (!access.ok) {
    sendNotFound(res);
    return;
  }

  // De-duplicate for the batch read; the response is keyed by id, so duplicates collapse naturally.
  const uniqueIds = [...new Set(ids)];
  const wellFormed = uniqueIds.filter((id) => isAssetId(id));
  const rows = wellFormed.length ? await ctx.store.batchGet(wellFormed.map((id) => assetKey(ctx.pid, id))) : [];
  const bySk = new Map<string, Item>(rows.map((r) => [String(r.SK), r]));

  const urls: Record<string, AssetUrlEntry> = {};
  const errors: Record<string, typeof PROJECT_NOT_FOUND> = {};

  for (const id of uniqueIds) {
    const row = isAssetId(id) ? (bySk.get(ASSET_SK_PREFIX + id) as AssetRow | undefined) : undefined;
    const entry = row ? await signAsset(row, ctx) : null;
    if (!entry) {
      // A per-id failure is the identical uniform not-found, keyed by the id, never also in `urls`.
      errors[id] = PROJECT_NOT_FOUND;
      continue;
    }
    urls[id] = entry;
  }

  sendJson(res, 200, { urls, errors });
}

/**
 * Read and validate the `{asset_ids}` body: a JSON object with an array of up to 100 strings. Sends a
 * 400 and returns `null` on anything malformed; returns the id list otherwise.
 */
async function readAssetIds(req: any, res: any): Promise<string[] | null> {
  let raw: Buffer;
  try {
    raw = await readCappedBody(req, ASSET_URLS_MAX_BODY_BYTES);
  } catch (e: any) {
    const code = e?.code === "payload_too_large" ? 413 : 400;
    sendJson(res, code, { error: { code: "bad_request", message: "Could not read request body" } });
    return null;
  }
  const text = raw.toString("utf8").trim();
  let parsed: unknown;
  try {
    parsed = text ? JSON.parse(text) : {};
  } catch {
    sendJson(res, 400, { error: { code: "bad_request", message: "Body must be valid JSON" } });
    return null;
  }
  const assetIds = (parsed as { asset_ids?: unknown } | null)?.asset_ids;
  if (!Array.isArray(assetIds) || assetIds.some((id) => typeof id !== "string")) {
    sendJson(res, 400, { error: { code: "bad_request", message: "asset_ids must be an array of strings" } });
    return null;
  }
  if (assetIds.length > ASSET_URL_MAX_IDS) {
    sendJson(res, 400, {
      error: { code: "too_many_ids", message: `At most ${ASSET_URL_MAX_IDS} asset ids per request` },
    });
    return null;
  }
  return assetIds as string[];
}

// --------------------------------------------------------------------------------------- status route

interface DetailCtx {
  actor: string;
  pid: string;
  assetId: string;
  store: Pick<TransactStore, "batchGet">;
  checkAccess: (actorSub: string, projectId: string) => Promise<AccessResult>;
}

/** `GET /projects/:pid/assets/:assetId` — access check, consistent read, the row's client view. */
async function handleAssetDetail(res: any, ctx: DetailCtx): Promise<void> {
  const access = await ctx.checkAccess(ctx.actor, ctx.pid);
  if (!access.ok) {
    sendNotFound(res);
    return;
  }
  if (!isAssetId(ctx.assetId)) {
    // A malformed id names no ASSET row: the uniform not-found (Req 3.11).
    sendNotFound(res);
    return;
  }
  const [row] = (await ctx.store.batchGet([assetKey(ctx.pid, ctx.assetId)])) as (AssetRow | undefined)[];
  if (!row) {
    sendNotFound(res);
    return;
  }
  sendJson(res, 200, publicAssetView(row, ctx.assetId));
}

// ----------------------------------------------------------------------------------------- list route

interface ListCtx {
  actor: string;
  pid: string;
  store: AssetReadStore;
  checkAccess: (actorSub: string, projectId: string) => Promise<AccessResult>;
}

/** `GET /projects/:pid/assets?cursor=` — access check, then a newest-first GSI page.
 *  `?q=<name part>&kind=<video|image|audio|document|data>` — filename search instead (see `searchAssets`). */
async function handleAssetList(req: any, res: any, ctx: ListCtx): Promise<void> {
  const access = await ctx.checkAccess(ctx.actor, ctx.pid);
  if (!access.ok) {
    sendNotFound(res);
    return;
  }
  const params = new URL(String(req.url || "/"), "http://localhost").searchParams;
  const q = params.get("q");
  const kind = params.get("kind");
  // `?folder=<path>`: the files filed directly in that folder (`folder=` alone is the project root). Its
  // subfolders are listed by `GET …/assets/folders`; like a file browser, a folder shows its own files.
  const rawFolder = params.get("folder");
  const folder = rawFolder === null ? undefined : normalizeFolder(rawFolder);
  if (isFolderError(folder)) {
    sendJson(res, 400, { error: { code: "bad_request", message: `folder: ${folder.error}` } });
    return;
  }
  const folderKey = folder === undefined ? undefined : (folder ?? "");
  if (q !== null || kind !== null || folderKey !== undefined) {
    if ((q !== null && (q.trim().length === 0 || q.length > 255)) || (kind !== null && !ASSET_KINDS.has(kind))) {
      sendJson(res, 400, { error: { code: "bad_request", message: "q must be 1-255 characters; kind one of video, image, audio, document, data" } });
      return;
    }
    // Paging offset for a search comes from an opaque cursor that also pins the exact (q, kind) it was
    // minted for — a client sending a cursor from a different query is a bug, so it is refused rather
    // than silently paging the wrong result set.
    const rawSearchCursor = params.get("cursor");
    let offset = 0;
    if (rawSearchCursor) {
      const decoded = decodeSearchCursor(rawSearchCursor);
      if (!decoded || decoded.q !== (q ?? "") || decoded.kind !== (kind ?? "") || decoded.folder !== folderKey) {
        sendJson(res, 400, { error: { code: "bad_request", message: "Invalid cursor" } });
        return;
      }
      offset = decoded.offset;
    }
    const found = await searchAssets(ctx, q, kind, folderKey);
    const page = found.ranked.slice(offset, offset + SEARCH_PAGE);
    const nextOffset = offset + page.length;
    const hasMore = nextOffset < found.ranked.length;
    sendJson(res, 200, {
      assets: page.map((r) => publicAssetView(r as AssetRow)),
      total: found.ranked.length,
      ...(hasMore ? { cursor: encodeSearchCursor({ q: q ?? "", kind: kind ?? "", ...(folderKey !== undefined ? { folder: folderKey } : {}), offset: nextOffset }) } : {}),
      // `truncated` now means the project is larger than the scan bound, so some matches may be unseen —
      // not that the page was capped (every ranked match within the scan is reachable by paging).
      ...(found.scanTruncated ? { truncated: true } : {}),
      ...(typeof access.project?.collectionState === "string" ? { collectionState: access.project.collectionState } : {}),
    });
    return;
  }
  const rawCursor = params.get("cursor");
  let exclusiveStartKey: Record<string, unknown> | undefined;
  if (rawCursor) {
    const decoded = decodeCursor(rawCursor);
    if (!decoded) {
      sendJson(res, 400, { error: { code: "bad_request", message: "Invalid cursor" } });
      return;
    }
    exclusiveStartKey = decoded;
  }

  const page = await ctx.store.queryIndex({
    indexName: ASSET_LIST_INDEX,
    partitionAttr: ASSET_LIST_PK_ATTR,
    partitionValue: projectPk(ctx.pid), // access.ok guarantees a well-formed pid
    sortAttr: ASSET_LIST_SK_ATTR,
    ascending: false, // newest first
    limit: ASSET_LIST_PAGE,
    ...(exclusiveStartKey ? { exclusiveStartKey } : {}),
  });

  // A removed asset loses its GSI attributes in the removal transaction, so it leaves the index; the GSI
  // is eventually consistent, so the filter also hides one that is still propagating out.
  sendJson(res, 200, {
    assets: page.items.filter((r) => r.status !== "removed").map((r) => publicAssetView(r as AssetRow)),
    ...(page.lastEvaluatedKey ? { cursor: encodeCursor(page.lastEvaluatedKey) } : {}),
    // The search collection's state, from the META the access check read (Req 10.12, 10.18).
    ...(typeof access.project?.collectionState === "string" ? { collectionState: access.project.collectionState } : {}),
  });
}

/**
 * `GET /projects/:pid/assets/folders` → `{folders: [{folder, count}], rootCount, truncated?}`: every folder that holds a
 * file the grid shows, with how many it holds directly (the client sums a folder's subtree). A complete list —
 * unlike one derived from loaded pages — so the folder tree never misses a folder whose files are older than
 * the first page. A hidden "this chat only" upload (`visibility: session`) is not counted. `rootCount` is what
 * sits in no folder (files and saved moments): the tree's Top level row.
 * Reads the same newest-first GSI as the list, bounded like search.
 */
async function handleFolderList(res: any, ctx: ListCtx): Promise<void> {
  const access = await ctx.checkAccess(ctx.actor, ctx.pid);
  if (!access.ok) {
    sendNotFound(res);
    return;
  }
  const counts = new Map<string, number>();
  let rootCount = 0;
  let truncated = true;
  let start: Record<string, unknown> | undefined;
  for (let page = 0; page < SEARCH_MAX_PAGES; page++) {
    const got = await ctx.store.queryIndex({
      indexName: ASSET_LIST_INDEX,
      partitionAttr: ASSET_LIST_PK_ATTR,
      partitionValue: projectPk(ctx.pid),
      sortAttr: ASSET_LIST_SK_ATTR,
      ascending: false,
      limit: ASSET_LIST_PAGE,
      ...(start ? { exclusiveStartKey: start } : {}),
    });
    for (const r of got.items) {
      if (r.status === "removed" || r.visibility === "session") continue;
      const f = typeof r.folder === "string" ? r.folder : "";
      if (f) counts.set(f, (counts.get(f) ?? 0) + 1);
      else rootCount++;
    }
    if (!got.lastEvaluatedKey) {
      truncated = false;
      break;
    }
    start = got.lastEvaluatedKey;
  }
  // Bookmarks file like files, so a folder holding only saved moments is a folder too.
  for (const b of await ctx.store.query(projectPk(ctx.pid), BOOKMARK_SK_PREFIX)) {
    const f = typeof b.folder === "string" ? b.folder : "";
    if (f) counts.set(f, (counts.get(f) ?? 0) + 1);
    else rootCount++;
  }
  // Folders made empty ("New folder") are recorded rows: listed with what they hold (0 until something is filed).
  for (const r of await ctx.store.query(projectPk(ctx.pid), FOLDER_SK_PREFIX)) {
    const f = typeof r.folder === "string" ? r.folder : "";
    if (f && !counts.has(f)) counts.set(f, 0);
  }
  const folders = [...counts.entries()]
    .sort(([a], [b]) => a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" }))
    .map(([folder, count]) => ({ folder, count }));
  sendJson(res, 200, { folders, rootCount, ...(truncated ? { truncated: true } : {}) });
}

const BOOKMARK_SK_PREFIX = "BOOKMARK#";
const FOLDER_SK_PREFIX = "FOLDER#";

/** The client view of a bookmark row (twin of `asset_service.bookmark_view`). */
export function bookmarkView(r: Item): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const k of ["bookmarkId", "assetId", "startMs", "endMs", "label", "folder", "assetKind", "assetDisplayName",
    "createdAt", "sessionId"]) {
    if (r[k] !== undefined && r[k] !== null && r[k] !== "") out[k] = r[k];
  }
  return out;
}

/**
 * `GET /projects/:pid/assets/bookmarks?folder=` → `{bookmarks}`: the saved moments filed in that folder (`folder=`
 * alone is the root; no `folder` = every bookmark), oldest first. A bookmark is its own row type (`BOOKMARK#`),
 * so the asset list and search never return one; the Assets grid shows a folder's bookmarks after its files.
 */
async function handleBookmarkList(req: any, res: any, ctx: ListCtx): Promise<void> {
  const access = await ctx.checkAccess(ctx.actor, ctx.pid);
  if (!access.ok) {
    sendNotFound(res);
    return;
  }
  const raw = new URL(String(req.url || "/"), "http://localhost").searchParams.get("folder");
  const folder = raw === null ? undefined : normalizeFolder(raw);
  if (isFolderError(folder)) {
    sendJson(res, 400, { error: { code: "bad_request", message: `folder: ${folder.error}` } });
    return;
  }
  const want = folder === undefined ? undefined : (folder ?? "");
  const rows = (await ctx.store.query(projectPk(ctx.pid), BOOKMARK_SK_PREFIX))
    .filter((r) => want === undefined || (typeof r.folder === "string" ? r.folder : "") === want)
    .sort((a, b) => String(a.createdAt ?? "").localeCompare(String(b.createdAt ?? "")));
  sendJson(res, 200, { bookmarks: rows.map(bookmarkView) });
}

// ------------------------------------------------------------------------------------- shared helpers

const ASSET_KINDS = new Set(["video", "image", "audio", "document", "data"]);
/** Filename search reads at most this many GSI pages (50 rows each): 5,000 assets, newest first. */
const SEARCH_MAX_PAGES = 100;
/** One page of ranked search results. */
const SEARCH_PAGE = 50;

/** NFC + lower case — the proxy's `foldName` rule (the agent's `fold_name` uses casefold, which differs only
 *  for a few letters such as ß). */
function fold(s: string): string {
  return s.normalize("NFC").toLowerCase();
}

/**
 * Relevance of a row to a folded name query, lower is better: 0 exact Display_Name, 1 prefix, 2 substring,
 * 3 matched only via its folder path. A JS stable sort by this score keeps the scan's newest-first order
 * within each tier, so the tiebreak is recency without a second key. The best name match therefore always
 * sorts ahead of newer rows that merely contain the query — the fix for an older exact-named upload being
 * buried under 50 newer `crop_*` derivatives.
 */
function nameScore(row: Item, needle: string): number {
  const name = fold(typeof row.displayName === "string" ? row.displayName : "");
  if (name === needle) return 0;
  if (name.startsWith(needle)) return 1;
  if (name.includes(needle)) return 2;
  return 3; // the query matched the folder path, not the name
}

/** A search continuation: the exact query it was minted for plus the offset into the ranked result set. */
interface SearchCursor {
  q: string;
  kind: string;
  /** The `?folder=` the cursor was minted for (`""` = the root); absent when none was given. */
  folder?: string;
  offset: number;
}

function encodeSearchCursor(c: SearchCursor): string {
  return Buffer.from(JSON.stringify(c), "utf8").toString("base64url");
}

function decodeSearchCursor(token: string): SearchCursor | null {
  try {
    const p = JSON.parse(Buffer.from(token, "base64url").toString("utf8"));
    if (p && typeof p === "object" && typeof p.q === "string" && typeof p.kind === "string"
        && Number.isSafeInteger(p.offset) && p.offset >= 0) {
      if (p.folder !== undefined && typeof p.folder !== "string") return null;
      return { q: p.q, kind: p.kind, ...(typeof p.folder === "string" ? { folder: p.folder } : {}), offset: p.offset };
    }
  } catch {
    // fall through
  }
  return null;
}

/**
 * Filename search over a Project's live assets, RANKED and PAGEABLE. Ranking needs the whole match set, so
 * this scans the newest-first GSI (no new index) collecting EVERY match up to a safety bound, then ranks a
 * name query by relevance (exact > prefix > substring > folder, newest-first within a tier). The caller
 * slices one `SEARCH_PAGE` window out of `ranked`, so every match within the scan is reachable by paging —
 * unlike the old 50-cap, which hid an older exact-named asset behind 50 newer substring matches. Works
 * whether indexing is on or off (it reads the asset list, not the search index). `scanTruncated` says the
 * project is larger than the scan bound, so matches beyond it were not seen.
 */
async function searchAssets(ctx: ListCtx, q: string | null, kind: string | null,
  folder?: string): Promise<{ ranked: Item[]; scanTruncated: boolean }> {
  const needle = q === null ? null : fold(q.trim());
  const matches: Item[] = [];
  let scanTruncated = true;
  let start: Record<string, unknown> | undefined;
  for (let page = 0; page < SEARCH_MAX_PAGES; page++) {
    const got = await ctx.store.queryIndex({
      indexName: ASSET_LIST_INDEX,
      partitionAttr: ASSET_LIST_PK_ATTR,
      partitionValue: projectPk(ctx.pid),
      sortAttr: ASSET_LIST_SK_ATTR,
      ascending: false,
      limit: ASSET_LIST_PAGE,
      ...(start ? { exclusiveStartKey: start } : {}),
    });
    for (const r of got.items) {
      if (r.status === "removed") continue;
      if (kind !== null && r.kind !== kind) continue;
      if (folder !== undefined && (typeof r.folder === "string" ? r.folder : "") !== folder) continue;
      if (needle !== null && !fold(folderPathText(r)).includes(needle)) continue;
      matches.push(r);
    }
    if (!got.lastEvaluatedKey) {
      scanTruncated = false;
      break;
    }
    start = got.lastEvaluatedKey;
  }
  // The scan yields newest-first; a stable sort by relevance keeps that recency order within each tier.
  const ranked = needle === null ? matches : matches.slice().sort((a, b) => nameScore(a, needle) - nameScore(b, needle));
  return { ranked, scanTruncated };
}

/** Fields of an ASSET row carried verbatim into the client view (the raw S3 location is never one). */
const PASSTHROUGH_FIELDS = [
  "source",
  "contentType",
  "sourceSize",
  "registeredAt",
  "queuedAt",
  "statusChangedAt",
  "failure",
  "folder",
  // asset-lineage design §2: where a file sits in the session structure and what it came from.
  "role",
  "visibility",
  "sessionId",
  "derivedFrom",
  "jobId",
  // A stored file a run deliberately left unindexed (`no_speech` audio): the card's neutral "Not indexed" badge.
  "indexSkippedReason",
  // A Personal sample (the demo tour's 0-demo.mp4): listed first, and kept first by the + menu's re-sort.
  "pinned",
] as const;

/**
 * The client-facing view of an ASSET row: the four fields the SPA reads (`assetId`, `kind`, `displayName`,
 * `status`) plus a few pass-through fields and the rendition's dimensions — never `PK`/`SK`, the raw
 * `sourceUri`, or the rendition's S3 key.
 */
export function publicAssetView(row: AssetRow, fallbackAssetId?: string): Record<string, unknown> {
  const view: Record<string, unknown> = {
    assetId: typeof row.assetId === "string" ? row.assetId : fallbackAssetId,
    kind: row.kind,
    displayName: row.displayName,
    status: row.status,
  };
  for (const k of PASSTHROUGH_FIELDS) {
    if (row[k] !== undefined) view[k] = row[k];
  }
  const rendition = row.rendition as { width?: unknown; height?: unknown; bytes?: unknown } | undefined;
  if (rendition && typeof rendition === "object") {
    view.rendition = { width: rendition.width, height: rendition.height, bytes: rendition.bytes };
  }
  return view;
}

/** Encode a `LastEvaluatedKey` as an opaque continuation token. */
function encodeCursor(key: Record<string, unknown>): string {
  return Buffer.from(JSON.stringify(key), "utf8").toString("base64url");
}

/** Decode a continuation token back to a `LastEvaluatedKey`, or `null` when it is malformed. */
function decodeCursor(token: string): Record<string, unknown> | null {
  try {
    const parsed = JSON.parse(Buffer.from(token, "base64url").toString("utf8"));
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed as Record<string, unknown>;
  } catch {
    // fall through
  }
  return null;
}

// ------------------------------------------------------------------------------------ duplicates route

/** The most file descriptors one `POST …/assets/duplicates` accepts (the SPA chunks a bigger drop). */
export const DUPLICATES_MAX_FILES = 1000;
/** 1,000 descriptors of a long name are well under this; a larger body is refused with 413. */
const DUPLICATES_MAX_BODY_BYTES = 1024 * 1024;
const DUPLICATES_MAX_NAME_LENGTH = 1024;
/** The pass reads at most this many GSI pages (50 rows each, 20,000 assets), newest first. */
const DUPLICATES_MAX_PAGES = 400;

interface DuplicateFile {
  index: number;
  size: number;
}

interface DuplicateHit {
  row: Item;
  exact: boolean;
}

/**
 * `POST /projects/:pid/assets/duplicates` — "these files are already in the Project" in one pass. The body is
 * read (capped) before the access check and interpreted only after it passes, like every other route here.
 */
async function handleDuplicates(req: any, res: any, ctx: ListCtx): Promise<void> {
  let raw: Buffer;
  try {
    raw = await readCappedBody(req, DUPLICATES_MAX_BODY_BYTES);
  } catch (e: any) {
    const tooLarge = e?.code === "payload_too_large";
    sendJson(res, tooLarge ? 413 : 400, {
      error: { code: tooLarge ? "payload_too_large" : "bad_request", message: "Could not read request body" },
    });
    return;
  }
  const access = await ctx.checkAccess(ctx.actor, ctx.pid);
  if (!access.ok) {
    sendNotFound(res);
    return;
  }
  let parsed: unknown;
  try {
    const text = raw.toString("utf8").trim();
    parsed = text ? JSON.parse(text) : {};
  } catch {
    sendJson(res, 400, { error: { code: "bad_request", message: "Body must be valid JSON" } });
    return;
  }
  const files = (parsed as { files?: unknown } | null)?.files;
  if (!Array.isArray(files) || files.length === 0) {
    sendJson(res, 400, { error: { code: "bad_request", message: `files must be an array of 1 to ${DUPLICATES_MAX_FILES} {name, size}` } });
    return;
  }
  if (files.length > DUPLICATES_MAX_FILES) {
    sendJson(res, 400, { error: { code: "too_many_files", message: `At most ${DUPLICATES_MAX_FILES} files per request` } });
    return;
  }
  // folded base → the files that would get it.
  const wanted = new Map<string, DuplicateFile[]>();
  for (let index = 0; index < files.length; index++) {
    const f = files[index] as { name?: unknown; size?: unknown } | null;
    const name = f && typeof f === "object" ? f.name : undefined;
    const size = f && typeof f === "object" ? f.size : undefined;
    if (typeof name !== "string" || name.length > DUPLICATES_MAX_NAME_LENGTH
        || typeof size !== "number" || !Number.isSafeInteger(size) || size < 0) {
      sendJson(res, 400, { error: { code: "bad_request", message: `files[${index}] needs a name and a non-negative integer size` } });
      return;
    }
    // A name with nothing left after the registry's stripping would be `Untitled <kind>`: never a duplicate.
    if (!displayBase(name)) continue;
    const base = fold(deriveDisplayName(name, kindFor("", name)));
    wanted.set(base, [...(wanted.get(base) ?? []), { index, size }]);
  }

  const best = new Map<number, DuplicateHit>();
  const pending = (): boolean => {
    for (const group of wanted.values()) for (const f of group) if (!best.get(f.index)?.exact) return true;
    return false;
  };
  let truncated = false;
  let start: Record<string, unknown> | undefined;
  if (wanted.size > 0) {
    for (let page = 0; ; page++) {
      if (page >= DUPLICATES_MAX_PAGES) {
        truncated = true;
        break;
      }
      const got = await ctx.store.queryIndex({
        indexName: ASSET_LIST_INDEX,
        partitionAttr: ASSET_LIST_PK_ATTR,
        partitionValue: projectPk(ctx.pid),
        sortAttr: ASSET_LIST_SK_ATTR,
        ascending: false,
        limit: ASSET_LIST_PAGE,
        ...(start ? { exclusiveStartKey: start } : {}),
      });
      for (const r of got.items) {
        if (r.status === "removed" || typeof r.displayName !== "string") continue;
        const size = typeof r.sourceSize === "number" ? r.sourceSize : Number(r.sourceSize);
        if (!Number.isFinite(size)) continue;
        const name = fold(r.displayName);
        const unsuffixed = fold(withoutCopySuffix(r.displayName));
        for (const key of name === unsuffixed ? [name] : [name, unsuffixed]) {
          for (const f of wanted.get(key) ?? []) {
            if (f.size !== size) continue;
            const exact = key === name;
            const prior = best.get(f.index);
            // Newest first: keep the first hit, unless a later one is the unsuffixed original.
            if (!prior || (exact && !prior.exact)) best.set(f.index, { row: r, exact });
          }
        }
      }
      if (!got.lastEvaluatedKey || !pending()) break; // done, or every file already has its original
      start = got.lastEvaluatedKey;
    }
  }
  const duplicates = [...best.entries()]
    .sort(([a], [b]) => a - b)
    .map(([index, hit]) => ({ index, asset: publicAssetView(hit.row as AssetRow) }));
  sendJson(res, 200, { duplicates, ...(truncated ? { truncated: true } : {}) });
}
