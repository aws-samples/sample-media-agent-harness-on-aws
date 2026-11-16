// The Moments tab's own search (2026-10-11): `POST /projects/:pid/moments/search {query, kinds?, k?}`.
//
// A user refining a search used to need a whole agent turn (10–25 s, model tokens). The tab now asks the
// Search_Gateway directly — no LLM — and gets back cards in the SAME shape the Moments manifest carries
// (`render/moments.py` build_moments_manifest), so `ClipGallery` renders them unchanged.
//
// Flow: Actor membership (`checkAccess`, uniform 404), a Project_Token from the Token_Issuer for the caller's own
// User_Token (as the turn gate mints one), one MCP `tools/call` of `hybrid-search___search_content` with
// `{collection_id: pid, search_term: query, k}` (the argument shape the agent's Search_Tool sends; scope tags
// `@Display_Name`, `#topic`, `#utterance` travel inside `search_term`, exactly as the media-search-qa skill
// writes them), then: the harness-side kind filter, the Hit shape check (`verify.is_hit`), the registry
// (Display_Name, kind, status — a `removed` asset is never a card), and one card per (asset, start, end).
//
// The agent is not told about these searches (v1). Nothing here is persisted: the tab keeps its own search
// history in the manifest's `viewState.search` (queries only) and re-runs a query when it is reopened.
import { companionTerm as companionTerm_, mergeDocumentHits } from "./documentRecall";
import { checkAccess as realCheckAccess, PROJECT_NOT_FOUND_BODY } from "./access";
import type { AccessResult } from "./access";
import { readCappedBody } from "../agentTestRoute";
import { issueProjectToken as realIssue } from "./tokenIssuerClient";
import type { IssueResult } from "./tokenIssuerClient";
import { dynamoTransactStoreFromEnv } from "./store";
import type { TransactStore } from "./store";
import { assetKey, isAssetId, projectPk } from "./schema";
import { bearerToken, isValidFrameKey } from "./turnGate";
import { invokeAssetService } from "./assetServiceClient";

export const MOMENTS_SEARCH_QUERY_MAX = 200;
export const MOMENTS_SEARCH_K_MAX = 50;
export const MOMENT_TEXT_MAX = 500;
const HIGHLIGHT_TERMS_MAX = 20;
const HIGHLIGHT_TERM_MAX_CHARS = 100;
const BODY_MAX_BYTES = 4 * 1024;
const SEARCH_TIMEOUT_MS = 30_000;
// Every searchable kind, pinned to infra/lib/projects/fixtures/search-kinds.json (agent: compact.ASSET_KINDS). A copy
// without `document` dropped every document hit here after PDFs became indexed.
export const KINDS = ["video", "image", "audio", "document"] as const;
type Kind = (typeof KINDS)[number];

/** The trim set shared with `project_search/citations.py` TRIM_CHARS (Python and JS defaults disagree). */
const TRIM = new Set([..."\u0020\u0009\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000\ufeff"]);

export interface MomentCard {
  id: string;
  ref: { project_id: string; asset_id: string; frame_s3_key: string | null };
  kind: Kind;
  display_name: string;
  start_ms: number;
  end_ms: number;
  source_index: string | null;
  final_score: number;
  text: string;
  text_truncated: boolean;
  highlight_terms: string[];
  citation: null;
  /** An image search: the indexes of the query images that found this moment ("match any"). */
  matched_by?: number[];
  found_by?: string[];
}

/** One query image of the tab's search (design 2026-10-18 §2): an image asset, or a frame of a project video. */
export type QueryImage = { assetId: string } | { frameKey: string; sourceAssetId: string };
export const QUERY_IMAGES_MAX = 3;

/** `query` for a text search, or `images` (1..3, "match any") for an image search — exactly one of them. */
export interface MomentsSearchRequest {
  query: string; images: QueryImage[] | null; kinds: Kind[] | null; k: number;
  /** The chat the tab belongs to: an image query's rendition is made on demand within its rules (optional). */
  sessionId?: string;
}

/** Calls one Search_Gateway tool with a Project_Token; resolves the tool's text result or throws. */
export type SearchToolCaller = (token: string, toolName: string, args: Record<string, unknown>) => Promise<string>;

export interface MomentsSearchDeps {
  checkAccess?: (actorSub: string, projectId: string) => Promise<AccessResult>;
  issueProjectToken?: (userToken: string, projectId: string) => Promise<IssueResult>;
  store?: (Pick<TransactStore, "batchGet"> & Partial<Pick<TransactStore, "query">>) | null;
  /** Buckets a query image's key is read from (default env `SKILLS_S3_BUCKET` / `MEDIA_PROXY_BUCKET`). */
  buckets?: { skills: string; mediaProxy: string };
  callTool?: SearchToolCaller;
  toolName?: string;
  /** An image query whose asset has no Search_Rendition yet: Asset_Service `search_images` makes one on demand
   *  (the agent's search does the same), with its ownership and this-chat-only rules. Default: invoke it. */
  searchImages?: (pid: string, userToken: string, assetIds: string[], sessionId: string) =>
    Promise<Record<string, { uri?: string; reason?: string }>>;
}

/** Asset_Service `search_images`: each image asset's Search_Rendition uri, made on demand, or why not. */
async function defaultSearchImages(pid: string, userToken: string, assetIds: string[], sessionId: string):
  Promise<Record<string, { uri?: string; reason?: string }>> {
  const r = await invokeAssetService({ userToken, projectId: pid, command: "search_images", args: { assetIds, sessionId }, sessionId });
  const images = r.ok ? ((r.value as { images?: Record<string, { uri?: string; reason?: string }> }).images ?? {}) : {};
  return images;
}

// ------------------------------------------------------------------------------------------ request

/** Validate the body; a string error message for a 400. */
export function parseMomentsSearchRequest(body: unknown): MomentsSearchRequest | string {
  if (!body || typeof body !== "object" || Array.isArray(body)) return "Body must be a JSON object";
  const b = body as Record<string, unknown>;
  const query = typeof b.query === "string" ? b.query.trim() : "";
  let images: QueryImage[] | null = null;
  if (b.images !== undefined && b.images !== null) {
    if (query) return "pass either query or images, not both";
    const parsedImages = parseQueryImages(b.images);
    if (typeof parsedImages === "string") return parsedImages;
    if (b.match !== undefined && b.match !== "any") return 'match must be "any"';
    images = parsedImages;
  } else {
    if (!query) return "query must be a non-empty string";
    if ([...query].length > MOMENTS_SEARCH_QUERY_MAX) return `query must be at most ${MOMENTS_SEARCH_QUERY_MAX} characters`;
  }
  let kinds: Kind[] | null = null;
  if (b.kinds !== undefined && b.kinds !== null) {
    if (!Array.isArray(b.kinds) || b.kinds.some((k) => !KINDS.includes(k as Kind))) {
      return "kinds must be a list of video, image, audio";
    }
    kinds = [...new Set(b.kinds as Kind[])];
    if (kinds.length === 0 || kinds.length === KINDS.length) kinds = null; // none or all = no filter
  }
  let k = MOMENTS_SEARCH_K_MAX;
  if (b.k !== undefined && b.k !== null) {
    if (!Number.isInteger(b.k) || (b.k as number) < 1 || (b.k as number) > MOMENTS_SEARCH_K_MAX) {
      return `k must be an integer from 1 to ${MOMENTS_SEARCH_K_MAX}`;
    }
    k = b.k as number;
  }
  const sessionId = typeof b.sessionId === "string" && /^[A-Za-z0-9._-]{1,128}$/.test(b.sessionId) ? b.sessionId : undefined;
  return { query, images, kinds, k, ...(sessionId ? { sessionId } : {}) };
}

function parseQueryImages(raw: unknown): QueryImage[] | string {
  if (!Array.isArray(raw) || raw.length < 1 || raw.length > QUERY_IMAGES_MAX) {
    return `images must be a list of 1 to ${QUERY_IMAGES_MAX} query images`;
  }
  const out: QueryImage[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object" || Array.isArray(item)) return "each image must be an object";
    const x = item as Record<string, unknown>;
    if (typeof x.assetId === "string" && x.frameKey === undefined) {
      if (!isAssetId(x.assetId) || !x.assetId.startsWith("image:")) return "an image's assetId must be an image asset id";
      out.push({ assetId: x.assetId });
    } else if (typeof x.frameKey === "string" && typeof x.sourceAssetId === "string" && x.assetId === undefined) {
      if (!isValidFrameKey(x.frameKey) || x.frameKey.length > 1024) return "frameKey is not a valid key";
      if (!isAssetId(x.sourceAssetId)) return "sourceAssetId must be an asset id";
      out.push({ frameKey: x.frameKey, sourceAssetId: x.sourceAssetId });
    } else {
      return "each image needs an assetId, or a frameKey with its sourceAssetId";
    }
  }
  return out;
}

// ------------------------------------------------------------------------------------------ query images

interface QueryAssetRow extends RegistryRow { rendition?: unknown; steps?: unknown }

/** The S3 key part of an `s3://bucket/key` prefix, or null. */
function keyOfS3Prefix(uri: unknown): string | null {
  if (typeof uri !== "string" || !uri.startsWith("s3://")) return null;
  const rest = uri.slice(5);
  const slash = rest.indexOf("/");
  const key = slash > 0 ? rest.slice(slash + 1) : "";
  return key ? (key.endsWith("/") ? key : `${key}/`) : null;
}

/** The output prefixes of an asset's own ingestion runs (`steps.*.outputPrefix`): where its hit frames live. */
function stepPrefixes(row: QueryAssetRow): string[] {
  const steps = row.steps;
  if (!steps || typeof steps !== "object" || Array.isArray(steps)) return [];
  return Object.values(steps as Record<string, unknown>)
    .map((s) => keyOfS3Prefix(s && typeof s === "object" ? (s as Record<string, unknown>).outputPrefix : null))
    .filter((p): p is string => !!p);
}

export type ResolvedQueryImages = { ok: true; uris: string[] } | { ok: false; status: number; code: string; message: string };

/**
 * Each query image as the `search_image_s3uri` M2C searches with, checked like the agent's `call_checks`:
 *   - an image ASSET of THIS project (not removed) → its Search_Rendition `s3://<skills>/<rendition.key>`; none yet → 409;
 *   - a FRAME → its source must be a video of this project (not removed), and the key must lie under that asset's own
 *     ingestion output prefixes (`steps.*.outputPrefix`), under `projects/<pid>/`, or under the `outputPrefix` of one
 *     of this project's JOB# records. Never an arbitrary key: another project's frame is refused like a missing one.
 */
export async function resolveQueryImages(
  pid: string, images: QueryImage[], store: Pick<TransactStore, "batchGet"> & Partial<Pick<TransactStore, "query">>,
  buckets: { skills: string; mediaProxy: string },
  onDemand?: (assetIds: string[]) => Promise<Record<string, { uri?: string; reason?: string }>>,
): Promise<ResolvedQueryImages> {
  const notFound = { ok: false as const, status: 404, code: "query_image_not_found",
    message: "That image is not part of this project any more." };
  const ids = [...new Set(images.map((i) => ("assetId" in i ? i.assetId : i.sourceAssetId)))];
  const rows = new Map<string, QueryAssetRow>();
  for (const row of (await store.batchGet(ids.map((id) => assetKey(pid, id)))) as QueryAssetRow[]) {
    if (typeof row.assetId === "string") rows.set(row.assetId, row);
  }
  let jobPrefixes: string[] | null = null;
  const uris: string[] = [];
  for (const image of images) {
    if ("assetId" in image) {
      const row = rows.get(image.assetId);
      if (!row || row.status === "removed" || row.kind !== "image") return notFound;
      const r = row.rendition as Record<string, unknown> | undefined;
      const key = r && typeof r === "object" && typeof r.key === "string" ? r.key : "";
      if (key && buckets.skills) {
        uris.push(`s3://${buckets.skills}/${key}`);
        continue;
      }
      // No rendition yet (an older image, or one ingestion never renders): make it now rather than wait forever.
      const made = onDemand ? (await onDemand([image.assetId]).catch(() => ({})))[image.assetId] : undefined;
      if (made?.uri) { uris.push(made.uri); continue; }
      if (made && made.reason && made.reason !== "not_ready") return notFound;
      return { ok: false, status: 409, code: "image_not_ready", message: "That image is still being prepared for search; try again in a minute." };
    }
    const row = rows.get(image.sourceAssetId);
    if (!row || row.status === "removed" || row.kind !== "video") return notFound;
    const key = image.frameKey;
    let owned = key.startsWith(`projects/${pid}/`) || stepPrefixes(row).some((p) => key.startsWith(p));
    if (!owned && store.query) {
      jobPrefixes ??= ((await store.query(projectPk(pid), "JOB#")) as Record<string, unknown>[])
        .map((j) => keyOfS3Prefix(j.outputPrefix)).filter((p): p is string => !!p);
      owned = jobPrefixes.some((p) => key.startsWith(p));
    }
    if (!owned) return notFound;
    const bucket = key.startsWith("projects/") ? buckets.skills : buckets.mediaProxy;
    if (!bucket) return { ok: false, status: 503, code: "search_unavailable", message: "Search by frame is not configured here." };
    uris.push(`s3://${bucket}/${key}`);
  }
  return { ok: true, uris };
}

/**
 * Merge one result list per query image ("match any"): one hit per (asset_id, start_ms), the best `final_score`
 * kept, `matched_by` = the indexes of the images that found it, best first.
 */
export function mergeImageResults(perImage: unknown[][]): (Record<string, unknown> & { matched_by: number[] })[] {
  const byKey = new Map<string, Record<string, unknown> & { matched_by: number[] }>();
  perImage.forEach((hits, index) => {
    for (const h of hits) {
      if (!isHit(h)) continue;
      const key = `${h.asset_id}|${h.start_ms}`;
      const prior = byKey.get(key);
      if (!prior) byKey.set(key, { ...h, matched_by: [index] });
      else {
        if (!prior.matched_by.includes(index)) prior.matched_by.push(index);
        if (h.final_score > (prior.final_score as number)) byKey.set(key, { ...h, matched_by: prior.matched_by });
      }
    }
  });
  return [...byKey.values()].sort((a, b) => (b.final_score as number) - (a.final_score as number));
}

// ------------------------------------------------------------------------------------------ shaping

const isInt = (v: unknown): v is number => typeof v === "number" && Number.isInteger(v);

/** `verify.is_hit`: str asset_id, int start_ms < int end_ms, numeric final_score. */
export function isHit(h: unknown): h is Record<string, unknown> & { asset_id: string; start_ms: number; end_ms: number; final_score: number } {
  if (!h || typeof h !== "object" || Array.isArray(h)) return false;
  const x = h as Record<string, unknown>;
  return typeof x.asset_id === "string" && isInt(x.start_ms) && isInt(x.end_ms) && x.start_ms < x.end_ms
    && typeof x.final_score === "number" && Number.isFinite(x.final_score);
}

/** The Hits of a search_content text result (`{"results": [...]}`), else []. */
export function hitsOfResult(text: string): unknown[] {
  try {
    const parsed = JSON.parse(text);
    return parsed && Array.isArray(parsed.results) ? parsed.results : [];
  } catch {
    return [];
  }
}

function trim(s: string): string {
  const chars = [...s];
  let a = 0;
  let b = chars.length;
  while (a < b && TRIM.has(chars[a])) a++;
  while (b > a && TRIM.has(chars[b - 1])) b--;
  return chars.slice(a, b).join("");
}

function unescapeHtml(s: string): string {
  return s
    .replace(/&#(\d+);/g, (_, d) => String.fromCodePoint(Number(d)))
    .replace(/&#x([0-9a-f]+);/gi, (_, h) => String.fromCodePoint(parseInt(h, 16)))
    .replace(/&quot;/g, '"').replace(/&#39;|&apos;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">")
    .replace(/&nbsp;/g, " ").replace(/&amp;/g, "&");
}

function strLeaves(node: unknown, depth = 1): string[] {
  if (typeof node === "string") return [node];
  if (depth > 3 || !node || typeof node !== "object") return [];
  const values = Array.isArray(node) ? node : Object.values(node as Record<string, unknown>);
  return values.flatMap((v) => strLeaves(v, depth + 1));
}

/** `render/moments.highlight_terms`: the trimmed, tag-stripped, unescaped inner text of every `<em>`. */
export function highlightTerms(highlights: unknown): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const leaf of strLeaves(highlights)) {
    for (const m of leaf.matchAll(/<em>([\s\S]*?)<\/em>/gi)) {
      const inner = trim(unescapeHtml(m[1].replace(/<[^>]*>/g, "")));
      if (!inner || [...inner].length > HIGHLIGHT_TERM_MAX_CHARS) continue;
      const fold = inner.toLocaleLowerCase();
      if (seen.has(fold)) continue;
      seen.add(fold);
      out.push(inner);
      if (out.length >= HIGHLIGHT_TERMS_MAX) return out;
    }
  }
  return out;
}

/** `render/moments.matched_text`: "" for a shot Hit, else its text (or named entities), cut to 500. */
export function matchedText(hit: Record<string, unknown>): [string, boolean] {
  let raw = "";
  if (hit.source_index !== "shot") {
    if (typeof hit.text === "string" && hit.text) raw = hit.text;
    else if (hit.named_entities !== undefined && hit.named_entities !== null) {
      raw = typeof hit.named_entities === "string" ? hit.named_entities : JSON.stringify(hit.named_entities);
    }
  }
  const chars = [...raw];
  return chars.length > MOMENT_TEXT_MAX ? [chars.slice(0, MOMENT_TEXT_MAX).join("") + "…", true] : [raw, false];
}

interface RegistryRow { assetId?: unknown; kind?: unknown; displayName?: unknown; status?: unknown }

/** Which of hybrid search's searches matched a hit (`ann:*` = by meaning, `fts:*` = by keyword); the agent's
 * Moments (`render/moments.found_by`) apply the same rule. */
export function foundBy(sources: unknown): { found_by?: string[] } {
  // M2C sends `{"ann:vector": 0.0117, "fts:named_entities": ...}` (source → score); a list of names is accepted too.
  const names = Array.isArray(sources) ? sources : sources && typeof sources === "object" ? Object.keys(sources) : [];
  const s = names.filter((x): x is string => typeof x === "string").map((x) => x.toLowerCase());
  const out = [
    ...(s.some((x) => /^(ann|knn|vector)/.test(x)) ? ["meaning"] : []),
    ...(s.some((x) => /^(fts|bm25|keyword|text)/.test(x)) ? ["keyword"] : []),
  ];
  return out.length ? { found_by: out } : {};
}

/** Cards for `hits` in rank order: kind filter, Hit shape, registry (removed / unknown dropped), one per key. */
export function shapeMomentCards(
  pid: string, hits: unknown[], rows: Map<string, RegistryRow>, kinds: Kind[] | null,
): MomentCard[] {
  const out: MomentCard[] = [];
  const seen = new Set<string>();
  for (const h of hits) {
    if (!isHit(h)) continue;
    const kindOfId = h.asset_id.split(":", 1)[0] as Kind;
    if (kinds && !kinds.includes(kindOfId)) continue;
    const row = rows.get(h.asset_id);
    if (!row || row.status === "removed") continue;
    const kind = row.kind as Kind;
    const display = typeof row.displayName === "string" ? row.displayName : "";
    if (!KINDS.includes(kind) || !display) continue;
    const key = `${h.asset_id}|${h.start_ms}|${h.end_ms}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const [text, truncated] = matchedText(h);
    const frame = h.source_index === "shot" && typeof h.frame_s3_key === "string" && h.frame_s3_key ? h.frame_s3_key : null;
    out.push({
      id: `s${out.length + 1}`,
      ref: { project_id: pid, asset_id: h.asset_id, frame_s3_key: frame },
      kind,
      display_name: display,
      start_ms: h.start_ms,
      end_ms: h.end_ms,
      source_index: typeof h.source_index === "string" ? h.source_index : null,
      final_score: h.final_score,
      text,
      text_truncated: truncated,
      highlight_terms: highlightTerms(h.highlights),
      citation: null,
      ...(Array.isArray(h.matched_by) ? { matched_by: (h.matched_by as number[]).slice() } : {}),
      ...foundBy(h.match_sources),
    });
  }
  return out;
}

// ------------------------------------------------------------------------------------------ MCP client

let rpcId = 0;

async function readRpc(res: Response, id: number): Promise<any> {
  const text = await res.text();
  const type = (res.headers.get("content-type") ?? "").toLowerCase();
  if (type.includes("text/event-stream")) {
    let found: any = null;
    for (const line of text.split(/\r?\n/)) {
      if (!line.startsWith("data:")) continue;
      try {
        const msg = JSON.parse(line.slice(5).trim());
        if (msg && msg.id === id) found = msg;
      } catch { /* a non-JSON keep-alive line */ }
    }
    return found;
  }
  return text ? JSON.parse(text) : null;
}

/**
 * A minimal MCP streamable-HTTP client: `initialize`, `notifications/initialized`, then `tools/call`. The only
 * credential is the Project_Token as Bearer (the Search_Gateway's Cedar policy reads its `project_id` tag).
 */
export function mcpToolCaller(url: string, fetchImpl: typeof fetch = fetch, timeoutMs = SEARCH_TIMEOUT_MS): SearchToolCaller {
  return async (token, toolName, args) => {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const base: Record<string, string> = {
        authorization: `Bearer ${token}`,
        "content-type": "application/json",
        accept: "application/json, text/event-stream",
      };
      const post = (headers: Record<string, string>, body: unknown) =>
        fetchImpl(url, { method: "POST", headers, body: JSON.stringify(body), signal: ctrl.signal });
      const initId = ++rpcId;
      const init = await post(base, {
        jsonrpc: "2.0", id: initId, method: "initialize",
        params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "harness-moments-search", version: "1" } },
      });
      if (!init.ok) throw new SearchCallError(`initialize HTTP ${init.status}`, init.status);
      const initMsg = await readRpc(init, initId);
      if (!initMsg || initMsg.error) throw new SearchCallError(initMsg?.error?.message ?? "initialize failed");
      const headers: Record<string, string> = { ...base };
      const session = init.headers.get("mcp-session-id");
      if (session) headers["mcp-session-id"] = session;
      // Deliberately NO `mcp-protocol-version` header. The AgentCore gateway answers `initialize` with its newest
      // revision (2026-07-28) whatever we ask for, and once a request DECLARES that revision every call must
      // also carry `_meta` (`io.modelcontextprotocol/protocolVersion`, `/clientInfo`, ...) plus `Mcp-Method`
      // headers -- this minimal client speaks neither, so echoing the negotiated version made every tools/call
      // a 400 "Missing required '_meta'" (staging 2026-10-11). Without the header the gateway applies its base
      // streamable-HTTP handling, which serves tools/call (verified live). Revisit only with a full MCP client.
      await post(headers, { jsonrpc: "2.0", method: "notifications/initialized" }).then((r) => r.text()).catch(() => undefined);
      const callId = ++rpcId;
      const call = await post(headers, { jsonrpc: "2.0", id: callId, method: "tools/call", params: { name: toolName, arguments: args } });
      if (!call.ok) throw new SearchCallError(`tools/call HTTP ${call.status}`, call.status);
      const msg = await readRpc(call, callId);
      if (!msg) throw new SearchCallError("no tools/call response");
      if (msg.error) throw new SearchCallError(String(msg.error.message ?? "search failed"));
      const result = msg.result ?? {};
      const text = (Array.isArray(result.content) ? result.content : [])
        .map((c: any) => (typeof c?.text === "string" ? c.text : "")).join("\n");
      if (result.isError) throw new SearchCallError(text || "search failed");
      return text;
    } catch (e) {
      if ((e as Error)?.name === "AbortError") throw new SearchCallError("search timed out");
      throw e;
    } finally {
      clearTimeout(timer);
    }
  };
}

export class SearchCallError extends Error {
  constructor(message: string, readonly status?: number) { super(message); }
}

// ------------------------------------------------------------------------------------------ route

function sendJson(res: any, status: number, body: unknown): void {
  if (res.headersSent) return;
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify(body));
}

const badRequest = (res: any, message: string) => sendJson(res, 400, { error: { code: "bad_request", message } });

let defaultCaller: SearchToolCaller | null = null;
let defaultStore: (Pick<TransactStore, "batchGet"> & Partial<Pick<TransactStore, "query">>) | null | undefined;

/** The registry rows of the hits' assets (Display_Name, kind, status), one batch read. */
async function registryRows(store: Pick<TransactStore, "batchGet">, pid: string, hits: unknown[]): Promise<Map<string, RegistryRow>> {
  const ids = [...new Set(hits.filter(isHit).map((h) => h.asset_id).filter((id) => isAssetId(id)))];
  const rows = new Map<string, RegistryRow>();
  if (ids.length) {
    for (const row of (await store.batchGet(ids.map((id) => assetKey(pid, id)))) as RegistryRow[]) {
      if (typeof row.assetId === "string") rows.set(row.assetId, row);
    }
  }
  return rows;
}

/** Handle `POST /projects/:pid/moments/search`, or `false` for any other path. */
export async function dispatchMomentsSearch(
  req: any, res: any, ctx: { actor: string; pid: string; parts: string[]; method: string },
  deps: MomentsSearchDeps = {},
): Promise<boolean> {
  const { parts, method } = ctx;
  if (!(parts.length === 3 && parts[1] === "moments" && parts[2] === "search")) return false;
  if (method !== "POST") {
    res.writeHead(405, { "content-type": "application/json", allow: "POST" });
    res.end(JSON.stringify({ error: { code: "method_not_allowed", message: "Use POST" } }));
    return true;
  }
  let body: unknown;
  try {
    const raw = await readCappedBody(req, BODY_MAX_BYTES);
    body = JSON.parse(raw.toString("utf8") || "{}");
  } catch {
    badRequest(res, "Body must be valid JSON of at most 4 KB");
    return true;
  }
  const parsed = parseMomentsSearchRequest(body);
  if (typeof parsed === "string") {
    badRequest(res, parsed);
    return true;
  }

  const env = process.env;
  const toolName = deps.toolName ?? env.SEARCH_TOOL_NAME ?? "";
  const url = env.SEARCH_GATEWAY_URL ?? "";
  const callTool = deps.callTool ?? (url ? (defaultCaller ??= mcpToolCaller(url)) : null);
  const store = deps.store !== undefined ? deps.store : (defaultStore ??= dynamoTransactStoreFromEnv(env));
  if (!toolName || !callTool || !store) {
    sendJson(res, 503, { error: { code: "search_unavailable", message: "Search is not configured in this environment" } });
    return true;
  }

  try {
    const access = await (deps.checkAccess ?? realCheckAccess)(ctx.actor, ctx.pid);
    if (!access.ok) {
      sendJson(res, 404, PROJECT_NOT_FOUND_BODY);
      return true;
    }
    const userToken = (typeof req.userToken === "string" && req.userToken) || bearerToken(req.headers?.authorization) || "";
    const issued = await (deps.issueProjectToken ?? realIssue)(userToken, ctx.pid);
    if (issued.status === "not_found") {
      sendJson(res, 404, PROJECT_NOT_FOUND_BODY);
      return true;
    }
    if (issued.status !== "ok") {
      sendJson(res, 503, { error: { code: "search_unavailable", message: "Could not authorize a search right now" } });
      return true;
    }
    if (parsed.images) {
      // Search by image: one M2C call per query image (it takes one per call), merged as "match any". No LLM.
      const searchImages = deps.searchImages ?? defaultSearchImages;
      const resolved = await resolveQueryImages(ctx.pid, parsed.images, store, deps.buckets ?? {
        skills: env.SKILLS_S3_BUCKET ?? "", mediaProxy: env.MEDIA_PROXY_BUCKET ?? "",
      }, parsed.sessionId ? (ids) => searchImages(ctx.pid, userToken, ids, parsed.sessionId!) : undefined);
      if (!resolved.ok) {
        sendJson(res, resolved.status, { error: { code: resolved.code, message: resolved.message } });
        return true;
      }
      const results = await Promise.allSettled(resolved.uris.map((uri) =>
        callTool(issued.token, toolName, { collection_id: ctx.pid, search_image_s3uri: uri, k: parsed.k })));
      if (results.every((r) => r.status === "rejected")) {
        console.warn("[moments-search] image search failed:", ((results[0] as PromiseRejectedResult).reason as Error)?.message);
        sendJson(res, 502, { error: { code: "search_failed", message: "The search did not complete. Try again." } });
        return true;
      }
      const merged = mergeImageResults(results.map((r) => (r.status === "fulfilled" ? hitsOfResult(r.value) : [])));
      const rows = await registryRows(store, ctx.pid, merged);
      const items = shapeMomentCards(ctx.pid, merged, rows, parsed.kinds);
      sendJson(res, 200, { images: parsed.images, match: "any", kinds: parsed.kinds, items, total: items.length,
        ...(results.some((r) => r.status === "rejected") ? { partial: true } : {}) });
      return true;
    }
    let text: string;
    // Document recall (documentRecall.ts, the agent's project_search/document_recall.py twin): a plain query
    // also asks the document scope, in parallel -- video shots otherwise outrank every PDF page.
    const companionTerm = companionTerm_(parsed.query, parsed.kinds);
    const args = { collection_id: ctx.pid, search_term: parsed.query, k: parsed.k };
    const [primary, companion] = await Promise.allSettled([
      callTool(issued.token, toolName, args),
      companionTerm ? callTool(issued.token, toolName, { ...args, search_term: companionTerm }) : Promise.resolve(null),
    ]);
    if (primary.status === "rejected") {
      const e = primary.reason;
      console.warn("[moments-search] search call failed:", (e as Error)?.message ?? e);
      sendJson(res, 502, { error: { code: "search_failed", message: "The search did not complete. Try again." } });
      return true;
    }
    text = primary.value;
    if (companion.status === "rejected") {
      console.warn("[moments-search] document companion failed; plain results only:", (companion.reason as Error)?.message ?? companion.reason);
    }
    const companionHits = companion.status === "fulfilled" && typeof companion.value === "string" ? hitsOfResult(companion.value) : [];
    const hits = mergeDocumentHits(hitsOfResult(text), companionHits);    const rows = await registryRows(store, ctx.pid, hits);
    const items = shapeMomentCards(ctx.pid, hits, rows, parsed.kinds);
    sendJson(res, 200, { query: parsed.query, kinds: parsed.kinds, items, total: items.length });
  } catch (e) {
    console.error("[moments-search] error:", (e as Error)?.message ?? e);
    sendJson(res, 500, { error: { code: "internal_error", message: "Internal error" } });
  }
  return true;
}
