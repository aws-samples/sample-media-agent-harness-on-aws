/**
 * Artifact URL resolution for the browser.
 *
 * An artifact reference is a STABLE IDENTIFIER, never a fetchable URL in its own right:
 *   - `https://<ARTIFACTS_DOMAIN>/artifacts/<key>` (the artifacts CloudFront distribution),
 *   - `/api/artifact?key=artifacts/<key>` (relative, or absolute on our own origin),
 *   - a bare `artifacts/<key>`.
 * Nothing stores an expiring URL. Every consumer resolves the identifier at the moment it uses it,
 * through the authenticated proxy route `POST /api/artifact-urls`, which answers short-lived
 * presigned S3 GETs. When `artifactsCdnPrivate` is on for an environment, the public CDN and the
 * public `/api/artifact` stop serving, so an unresolved identifier no longer loads; with the flag
 * off both still work, so resolution failing is never worse than today.
 *
 * `artifacts/m2c/<bucket>/<key>` is an M2C_REFERENCE: an object M2C wrote, referenced where it
 * wrote it and never copied (asset-lineage D4/R4). It is an artifact key like any other here — the
 * batched resolver asks for it and the proxy presigns it in the M2C proxy bucket for an actor who
 * can read the project that references it. Its ONLY canonical form is the bare key: there is no
 * object at the matching CDN path, so `artifactIdentifierFor` never builds one.
 *
 * Failure policy: nothing here throws to a caller. A key that cannot be resolved (network error,
 * non-200, per-key error) keeps its ORIGINAL string, so a page degrades to the pre-change
 * behaviour instead of losing links.
 *
 * The reverse maps (`resolved url -> key`, `resolved url -> original identifier`) exist for two
 * consumers: the media re-sign hook, which only sees an element's current (presigned) `src`, and
 * `unresolveArtifactUrls`, which turns presigned URLs in a template's interaction payload back into
 * identifiers before they reach chat history or an agent prompt.
 */

import { isProjectSessionOutputKey } from '../shared/sessionLayout';
import { authFetch } from '../auth/authFetch';
import { getConfig } from '../lab-api';
import { deriveArtifactKey } from '../components/chat/artifactUrl';

export interface ResolvedArtifactUrl {
  url: string;
  /** Epoch SECONDS at which the presigned URL stops working. */
  expiresAt: number;
}

export interface ResolveOptions {
  /** Presign with `Content-Disposition: attachment` (a separate cache namespace). */
  download?: boolean;
  /**
   * The URL will be loaded by a FRAME or a top-level navigation (an `<iframe>`, an "Open" link),
   * which cannot send an Authorization header. Every key then resolves to a same-origin
   * `/api/artifact?key=…&view=<ticket>` URL: a view ticket (valid for that one key, read-only) is
   * the only credential it carries — never the user's access token — and the proxy serves it
   * sniff-proof, sandboxed and with a policed content type. A separate cache namespace.
   */
  frame?: boolean;
}

type Mode = 'view' | 'download' | 'frame';
const MODES: readonly Mode[] = ['view', 'download', 'frame'];

function modeOf(opts?: ResolveOptions): Mode {
  return opts?.download ? 'download' : opts?.frame ? 'frame' : 'view';
}

export const ARTIFACT_URLS_ENDPOINT = '/api/artifact-urls';
/** The route's per-request key limit. */
export const MAX_KEYS_PER_REQUEST = 100;
/** A cached URL is dropped this many seconds before its `expiresAt`. */
export const EXPIRY_SKEW_SECONDS = 60;
/** A key the server refused (`not_found`/`not_resolvable`) is not re-asked for this long. */
const NEGATIVE_TTL_MS = 30_000;

const ARTIFACTS_PREFIX = 'artifacts/';
/** The project root; only its `sessions/<sid>/output/` sub-tree is an artifact (`isProjectSessionOutputKey`). */
const PROJECTS_PREFIX = 'projects/';
/** The M2C_REFERENCE namespace: `artifacts/m2c/<bucket>/<key>`, resolvable only via the proxy. */
const M2C_PREFIX = 'artifacts/m2c/';

// ---------------------------------------------------------------------------
// Artifacts domain (from the cached /config)
// ---------------------------------------------------------------------------

let artifactsDomain = '';
let domainPromise: Promise<void> | null = null;

/**
 * Load the configured ARTIFACTS_DOMAIN from `/config` (cached by `getConfig`). `artifactKeyOf`
 * already trusts `*.cloudfront.net`, so this only matters for a custom artifacts domain; the async
 * resolvers await it, sync callers use whatever is known. Never rejects.
 */
export function ensureArtifactsDomain(): Promise<void> {
  if (!domainPromise) {
    domainPromise = (async () => {
      try {
        const cfg = await getConfig();
        const d = cfg && typeof cfg.artifactsDomain === 'string' ? cfg.artifactsDomain : '';
        if (d && !artifactsDomain) artifactsDomain = d.toLowerCase();
      } catch {
        // No config (or a test mock without getConfig): *.cloudfront.net is still trusted.
      }
    })();
  }
  return domainPromise;
}

/** Set the artifacts domain directly (tests; or a caller that already holds the config). */
export function setArtifactsDomain(domain: string | undefined): void {
  artifactsDomain = (domain || '').toLowerCase();
}

// ---------------------------------------------------------------------------
// Identifier -> key
// ---------------------------------------------------------------------------

/**
 * The authorization-policy shape check the server applies, mirrored so we never ask for junk: an
 * `artifacts/` key, or a projects-session OUTPUT key (`projects/<pid>/sessions/<sid>/output/…`,
 * asset-lineage Phase 3 (a)). Project ASSET keys are not artifact keys (they resolve by asset id).
 */
export function isArtifactKey(key: string): boolean {
  if (isProjectSessionOutputKey(key)) return true;
  return (
    typeof key === 'string' &&
    key.startsWith(ARTIFACTS_PREFIX) &&
    key.length > ARTIFACTS_PREFIX.length &&
    !key.includes('..') &&
    !/[\\\r\n]/.test(key)
  );
}

// A project FILE's bytes: any `projects/<pid>/…` key that is not session output. Not an artifact key —
// the proxy's `/api/artifact*` routes never serve it — but a manifest names one whenever its source is a
// project file (the agent's stable-ref rewrite turns the presigned source URL into its artifacts-CDN
// path). With the CDN private that path 403s, so it resolves through the asset-id-gated Asset_URL_Route.
// The canonical layout `projects/<pid>/<kind>:<uuid>/<file>` names its asset; a file registered in
// place (the import inbox, `projects/<pid>/import/<name>`) does not, so `requestChunk` looks it up first.
const PROJECT_FILE_KEY_RE = /^projects\/(p[0-9a-f]{32})\/(?:((?:video|image|audio|document|data):[0-9a-f-]{36})\/[^/\\\r\n]+$|[^\\\r\n]+$)/;

/** The project (and asset, when the key names it) a project FILE key belongs to, or null. */
export function projectAssetOfKey(key: unknown): { projectId: string; assetId: string | null } | null {
  if (typeof key !== 'string' || key.includes('..') || isArtifactKey(key)) return null;
  const m = PROJECT_FILE_KEY_RE.exec(key);
  return m ? { projectId: m[1], assetId: m[2] ?? null } : null;
}

/** An artifact key, or a project ASSET key (resolved through the Asset_URL_Route). */
function isResolvableKey(key: string): boolean {
  return isArtifactKey(key) || projectAssetOfKey(key) !== null;
}

/**
 * The project ASSET key an identifier names — bare, or its artifacts-CDN URL — or null. Kept apart
 * from `artifactKeyOf` (which must stay null for these: no `/api/artifact*` route serves them).
 */
export function projectAssetKeyOf(input: string, artifactsDomainOverride?: string): string | null {
  if (typeof input !== 'string') return null;
  const raw = input.trim();
  if (!raw || isMalformedIdentifier(raw)) return null;
  if (raw.startsWith(PROJECTS_PREFIX)) return projectAssetOfKey(raw) ? raw : null;
  if (!/^https?:\/\//i.test(raw)) return null;
  let u: URL;
  try {
    u = new URL(raw);
  } catch {
    return null;
  }
  if (!isTrustedArtifactHost(u.hostname, artifactsDomainOverride)) return null;
  const key = safeDecode(u.pathname.slice(1));
  return key && projectAssetOfKey(key) ? key : null;
}

/** An S3 REST endpoint host (`<bucket>.s3[.<region>].amazonaws.com` or `s3[.<region>].amazonaws.com`). */
function isAmazonS3Host(host: string): boolean {
  const h = host.toLowerCase();
  if (!h.endsWith('.amazonaws.com')) return false;
  return h.includes('.s3.') || h.includes('.s3-') || h.startsWith('s3.') || h.startsWith('s3-');
}

/** The key candidate(s) a decoded S3 REST path yields: the whole path (virtual-hosted), and — when
 *  there is a leading segment — the path minus its first segment (path-style, dropping `<bucket>`). */
function s3RestKeyCandidates(u: URL): string[] {
  const path = safeDecode(u.pathname.replace(/^\/+/, ''));
  if (!path) return [];
  const out = [path];
  const i = path.indexOf('/');
  if (i > 0) out.push(path.slice(i + 1));
  return out;
}

/**
 * The artifact / project-asset S3 key a RAW presigned S3 REST URL of our OWN content names, or null.
 *
 * The render pipeline bakes a presigned S3 URL into a stored manifest in two cases — a `render-video`
 * SOURCE and a generated-image gallery whose source is a project asset
 * (`https://<bucket>.s3.<region>.amazonaws.com/projects/<pid>/<kind>:<uuid>/<file>?X-Amz-…`). Every
 * OTHER render instead stores the artifacts-CDN identifier, which `artifactKeyOf` / `projectAssetKeyOf`
 * already resolve. A presigned token expires in an hour, so a baked one is a dead link — but it names
 * the SAME object as its stable identifier, so we recover the key (dropping the signature query) and
 * let the normal, server-gated routes re-resolve and re-sign it at view time (deep resolver + the
 * media re-sign hook + the copy button).
 *
 * The token is never trusted — only the path — and resolution re-authorizes the key / asset id
 * (`/api/artifact-urls`, `/projects/:pid/assets/urls`), so a foreign host or an inaccessible object
 * simply stays unresolved (the original string is kept). `artifactKeyOf` deliberately keeps returning
 * null for a presigned URL (it treats a presigned link as opaque); this is a separate, narrower
 * recovery used only where re-resolution happens, so that contract is unchanged.
 */
export function s3RestArtifactKeyOf(input: string): string | null {
  if (typeof input !== 'string') return null;
  const raw = input.trim();
  if (!raw || isMalformedIdentifier(raw) || !/^https?:\/\//i.test(raw)) return null;
  let u: URL;
  try {
    u = new URL(raw);
  } catch {
    return null;
  }
  if (!isAmazonS3Host(u.hostname)) return null;
  for (const cand of s3RestKeyCandidates(u)) {
    if (isArtifactKey(cand) || projectAssetOfKey(cand)) return cand;
  }
  return null;
}

/**
 * A string that cannot name ONE artifact: a backslash, a CR/LF, or a second `http(s)://` after its
 * start. This is what an agent produces when it writes two links into one field (session 865950fd:
 * `.../compliance_report.json\nhttps://.../compliance_report.pdf`, with a literal `\n`). `new URL`
 * would silently strip a raw newline and name a path nobody wrote.
 */
function isMalformedIdentifier(raw: string): boolean {
  return raw.includes('..') || /[\\\r\n]/.test(raw) || /https?:\/\//i.test(raw.slice(1));
}

function spaOrigin(): string {
  return typeof window !== 'undefined' && window.location ? window.location.origin : 'http://localhost';
}

function isTrustedArtifactHost(host: string, domainOverride?: string): boolean {
  const h = host.toLowerCase();
  const dom = (domainOverride || artifactsDomain || '').toLowerCase();
  return h.endsWith('.cloudfront.net') || (!!dom && h === dom);
}

function safeDecode(s: string): string | null {
  try {
    return decodeURIComponent(s);
  } catch {
    return null;
  }
}

/**
 * The `artifacts/...` S3 key an artifact identifier names, or null when `input` is not one of our
 * artifact identifiers. Null for presigned `*.amazonaws.com` URLs, fal/third-party hosts, `data:` /
 * `blob:` URLs, anything containing `..`, and malformed strings (`isMalformedIdentifier`).
 *
 * `artifactsDomain` overrides the cached `/config` value (ArtifactLink receives it as a prop).
 */
export function artifactKeyOf(input: string, artifactsDomainOverride?: string): string | null {
  if (typeof input !== 'string') return null;
  const raw = input.trim();
  if (!raw || isMalformedIdentifier(raw)) return null;

  // (c) bare key
  if (raw.startsWith(ARTIFACTS_PREFIX) || raw.startsWith(PROJECTS_PREFIX)) return isArtifactKey(raw) ? raw : null;

  const isAbsolute = /^https?:\/\//i.test(raw);
  const isRootRelative = raw.startsWith('/') && !raw.startsWith('//');
  if (!isAbsolute && !isRootRelative) return null; // data:, blob:, javascript:, relative paths

  let u: URL;
  try {
    u = new URL(raw, spaOrigin());
  } catch {
    return null;
  }

  // (b) the /api/artifact proxy form, relative or absolute on our own (or the artifacts) host
  if (u.pathname === '/api/artifact') {
    if (isAbsolute && u.origin !== spaOrigin() && !isTrustedArtifactHost(u.hostname, artifactsDomainOverride)) {
      return null;
    }
    let key = deriveArtifactKey(`${u.pathname}${u.search}`);
    if (!key) return null;
    // Same normalisation the proxy applies: the agent runtime sometimes doubles the prefix.
    if (key.startsWith('artifacts/artifacts/')) key = key.slice(ARTIFACTS_PREFIX.length);
    return isArtifactKey(key) ? key : null;
  }

  // (a) the artifacts CDN
  if (!isAbsolute) return null;
  if (!isTrustedArtifactHost(u.hostname, artifactsDomainOverride)) return null;
  if (!u.pathname.startsWith(`/${ARTIFACTS_PREFIX}`) && !u.pathname.startsWith(`/${PROJECTS_PREFIX}`)) return null;
  const encoded = deriveArtifactKey(`${u.origin}${u.pathname}`, artifactsDomainOverride || artifactsDomain);
  if (!encoded) return null;
  const key = safeDecode(encoded);
  return key && isArtifactKey(key) ? key : null;
}

/**
 * The canonical identifier for a key: the CDN URL when the domain is known, else the bare key.
 * An M2C_REFERENCE always stays the bare key — no object exists at its CDN path, so a URL there
 * would be a link that looks fetchable and 403s.
 */
export function artifactIdentifierFor(key: string): string {
  if (key.startsWith(M2C_PREFIX)) return key;
  return artifactsDomain ? `https://${artifactsDomain}/${key}` : key;
}

// ---------------------------------------------------------------------------
// Batched resolver
// ---------------------------------------------------------------------------

type Waiter = (value: ResolvedArtifactUrl | null) => void;

const cache = new Map<string, ResolvedArtifactUrl>();
const negative = new Map<string, number>();
const inflight = new Map<string, Promise<ResolvedArtifactUrl | null>>();
const keyByUrl = new Map<string, string>();
const originalByUrl = new Map<string, string>();
const queued: Record<Mode, Map<string, Waiter>> = { view: new Map(), download: new Map(), frame: new Map() };
const flushScheduled: Record<Mode, boolean> = { view: false, download: false, frame: false };

function ns(key: string, mode: Mode): string {
  return `${mode}:${key}`;
}

function isFresh(e: ResolvedArtifactUrl | undefined, nowMs: number): e is ResolvedArtifactUrl {
  return !!e && nowMs / 1000 < e.expiresAt - EXPIRY_SKEW_SECONDS;
}

function normalizedHref(url: string): string | null {
  try {
    return new URL(url, spaOrigin()).href;
  } catch {
    return null;
  }
}

function rememberUrl(url: string, key: string): void {
  keyByUrl.set(url, key);
  const href = normalizedHref(url);
  if (href && href !== url) keyByUrl.set(href, key);
}

/** Record which identifier a resolved URL stood in for (used by `unresolveArtifactUrls`). */
export function rememberOriginal(resolvedUrl: string, original: string): void {
  if (resolvedUrl && original && resolvedUrl !== original) originalByUrl.set(resolvedUrl, original);
}

/** Sync cache lookup; returns a URL only while it is still comfortably valid. */
export function peekArtifactUrl(key: string, opts?: ResolveOptions): ResolvedArtifactUrl | null {
  const e = cache.get(ns(key, modeOf(opts)));
  return isFresh(e, Date.now()) ? e : null;
}

/** The artifact key behind a URL: a resolved (presigned) URL via the reverse map, else an identifier. */
export function keyForUrl(url: string): string | null {
  if (!url) return null;
  const direct = keyByUrl.get(url);
  if (direct) return direct;
  const href = normalizedHref(url);
  if (href && keyByUrl.has(href)) return keyByUrl.get(href)!;
  return artifactKeyOf(url) ?? projectAssetKeyOf(url) ?? s3RestArtifactKeyOf(url);
}

async function requestChunk(keys: string[], mode: Mode): Promise<Map<string, ResolvedArtifactUrl>> {
  const out = new Map<string, ResolvedArtifactUrl>();
  const assetKeys = keys.filter((k) => projectAssetOfKey(k));
  if (assetKeys.length) {
    // A project ASSET (a manifest's source video) resolves through the asset-id-gated Asset_URL_Route,
    // per project. Its URLs are view URLs; a download asks the same route (no attachment variant).
    const byProject = new Map<string, Map<string, string>>();
    const unnamed = new Map<string, string[]>();
    for (const k of assetKeys) {
      const a = projectAssetOfKey(k)!;
      if (!byProject.has(a.projectId)) byProject.set(a.projectId, new Map());
      if (a.assetId) byProject.get(a.projectId)!.set(a.assetId, k);
      else unnamed.set(a.projectId, [...(unnamed.get(a.projectId) ?? []), k]);
    }
    await Promise.all([...byProject].map(async ([pid, ids]) => {
      try {
        const named = unnamed.get(pid);
        if (named) {
          // A file registered in place: the asset registered at its key (SRC guard), same route as chat links.
          const r = await authFetch(`/projects/${encodeURIComponent(pid)}/assets/resolve-locations`, {
            method: 'POST',
            headers: { 'content-type': 'application/json', accept: 'application/json' },
            body: JSON.stringify({ locations: named }),
          });
          const found = r && r.ok ? ((await r.json()) as { locations?: Record<string, unknown> } | null)?.locations ?? {} : {};
          for (const k of named) {
            const id = found[k];
            if (typeof id === 'string' && id) ids.set(id, k);
            else negative.set(ns(k, mode), Date.now() + NEGATIVE_TTL_MS);
          }
          if (!ids.size) return;
        }
        const res = await authFetch(`/projects/${encodeURIComponent(pid)}/assets/urls`, {
          method: 'POST',
          headers: { 'content-type': 'application/json', accept: 'application/json' },
          body: JSON.stringify({ asset_ids: [...ids.keys()] }),
        });
        const body = res && res.ok ? ((await res.json()) as { urls?: Record<string, { source?: { url?: unknown; expiresAt?: unknown } }> } | null) : null;
        const urls = body && body.urls && typeof body.urls === 'object' ? body.urls : {};
        const now = Date.now();
        for (const [assetId, key] of ids) {
          const src = Object.prototype.hasOwnProperty.call(urls, assetId) ? urls[assetId]?.source : undefined;
          const url = src && typeof src.url === 'string' ? src.url : '';
          const expiresAt = src && typeof src.expiresAt === 'number' && Number.isFinite(src.expiresAt) ? src.expiresAt : NaN;
          if (url && Number.isFinite(expiresAt)) {
            const entry = { url, expiresAt };
            cache.set(ns(key, mode), entry);
            negative.delete(ns(key, mode));
            rememberUrl(url, key);
            out.set(key, entry);
          } else {
            negative.set(ns(key, mode), now + NEGATIVE_TTL_MS);
          }
        }
      } catch {
        // Network / parse failure: these keys keep their original URL.
      }
    }));
    keys = keys.filter((k) => !projectAssetOfKey(k));
    if (!keys.length) return out;
  }
  try {
    const res = await authFetch(ARTIFACT_URLS_ENDPOINT, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(mode === 'download' ? { keys, download: true } : mode === 'frame' ? { keys, frame: true } : { keys }),
    });
    if (!res || !res.ok) return out;
    const body = (await res.json()) as {
      urls?: Record<string, { url?: unknown; expiresAt?: unknown }>;
      errors?: Record<string, unknown>;
    } | null;
    const urls = body && typeof body.urls === 'object' && body.urls ? body.urls : {};
    const errors = body && typeof body.errors === 'object' && body.errors ? body.errors : {};
    const now = Date.now();
    for (const key of keys) {
      const e = Object.prototype.hasOwnProperty.call(urls, key) ? urls[key] : undefined;
      const url = e && typeof e.url === 'string' ? e.url : '';
      const expiresAt = e && typeof e.expiresAt === 'number' && Number.isFinite(e.expiresAt) ? e.expiresAt : NaN;
      if (url && Number.isFinite(expiresAt)) {
        const entry = { url, expiresAt };
        cache.set(ns(key, mode), entry);
        negative.delete(ns(key, mode));
        rememberUrl(url, key);
        out.set(key, entry);
      } else if (Object.prototype.hasOwnProperty.call(errors, key)) {
        negative.set(ns(key, mode), now + NEGATIVE_TTL_MS);
      }
    }
  } catch {
    // Network / parse failure: every key in the chunk keeps its original URL.
  }
  return out;
}

function flush(mode: Mode): void {
  flushScheduled[mode] = false;
  const bucket = queued[mode];
  if (bucket.size === 0) return;
  const entries = [...bucket.entries()];
  bucket.clear();
  for (let i = 0; i < entries.length; i += MAX_KEYS_PER_REQUEST) {
    const chunk = entries.slice(i, i + MAX_KEYS_PER_REQUEST);
    void requestChunk(chunk.map(([k]) => k), mode).then((result) => {
      for (const [key, waiter] of chunk) {
        inflight.delete(ns(key, mode));
        waiter(result.get(key) ?? null);
      }
    });
  }
}

function enqueue(key: string, mode: Mode): Promise<ResolvedArtifactUrl | null> {
  const id = ns(key, mode);
  const existing = inflight.get(id);
  if (existing) return existing;
  const p = new Promise<ResolvedArtifactUrl | null>((resolve) => {
    queued[mode].set(key, resolve);
  });
  inflight.set(id, p);
  if (!flushScheduled[mode]) {
    flushScheduled[mode] = true;
    // Coalesce every key requested in the same tick (e.g. all the effects of one commit) into
    // one POST.
    queueMicrotask(() => flush(mode));
  }
  return p;
}

/**
 * Resolve artifact keys to presigned URLs. Batched (calls in the same tick share a request),
 * chunked to 100 keys, de-duplicated against in-flight requests, cached until 60 s before expiry.
 * Keys that fail are simply absent from the result; this never rejects.
 */
export async function resolveArtifactUrls(
  keys: Iterable<string>,
  opts?: ResolveOptions,
): Promise<Map<string, ResolvedArtifactUrl>> {
  const mode = modeOf(opts);
  const result = new Map<string, ResolvedArtifactUrl>();
  const now = Date.now();
  const waits: Promise<void>[] = [];
  for (const key of new Set(keys)) {
    if (!isResolvableKey(key)) continue;
    const id = ns(key, mode);
    const hit = cache.get(id);
    if (isFresh(hit, now)) {
      result.set(key, hit);
      continue;
    }
    const until = negative.get(id);
    if (until !== undefined && until > now) continue;
    waits.push(enqueue(key, mode).then((v) => { if (v) result.set(key, v); }));
  }
  if (waits.length) await Promise.all(waits);
  return result;
}

/**
 * Drop the cached URL for a key (or for the key behind a resolved/identifier URL) so the next
 * resolve asks the server again. The old URL stays in the reverse map, so an element still showing
 * it can be mapped back to its key.
 */
export function invalidateArtifactUrl(keyOrResolvedUrl: string): void {
  const key = isArtifactKey(keyOrResolvedUrl) ? keyOrResolvedUrl : keyForUrl(keyOrResolvedUrl);
  if (!key) return;
  for (const mode of MODES) {
    cache.delete(ns(key, mode));
    negative.delete(ns(key, mode));
  }
}

/** Test hook: forget every cached URL, reverse mapping and the cached domain. */
export function __resetArtifactUrlsForTests(): void {
  cache.clear();
  negative.clear();
  inflight.clear();
  keyByUrl.clear();
  originalByUrl.clear();
  for (const mode of MODES) {
    queued[mode].clear();
    flushScheduled[mode] = false;
  }
  artifactsDomain = '';
  domainPromise = null;
}

// ---------------------------------------------------------------------------
// Deep (JSON) resolution
// ---------------------------------------------------------------------------

export type JsonPath = ReadonlyArray<string | number>;

export interface DeepResolveOptions {
  /** Return true to leave the string at `path` untouched. Combined with the default skip. */
  skip?: (path: JsonPath) => boolean;
}

/**
 * Properties that hold an IDENTITY rather than something to load: `key`, `sourceKey`,
 * `frame_s3_key`, ... (persisted edits and server de-duplication key off them), and `manifestUrl`
 * (fetched by ManifestRouter via authFetch, and part of a tab's persisted identity).
 */
function defaultSkip(path: JsonPath): boolean {
  const last = path[path.length - 1];
  return typeof last === 'string' && (/key$/i.test(last) || last === 'manifestUrl');
}

/**
 * `document.previewUrl` is framed by DocumentViewer through `/api/artifact?key=…&view=<ticket>`
 * (`resolveArtifactUrls(…, {frame: true})`), which serves a guessed content type for octet-stream
 * objects (a presigned GET would download a PDF instead of displaying it). Render hosts pass this
 * to keep that path.
 */
export function preserveDocumentPreview(path: JsonPath): boolean {
  const n = path.length;
  if (n < 2 || path[n - 1] !== 'previewUrl') return false;
  // `document.previewUrl`, and each `documents[i].previewUrl` of a multi-document manifest.
  return path[n - 2] === 'document' || (n >= 3 && typeof path[n - 2] === 'number' && path[n - 3] === 'documents');
}

function walk(value: unknown, path: (string | number)[], skip: (p: JsonPath) => boolean, visit: (s: string) => void, seen: WeakSet<object>): void {
  if (typeof value === 'string') {
    if (!skip(path)) visit(value);
    return;
  }
  if (!value || typeof value !== 'object') return;
  if (seen.has(value as object)) return;
  seen.add(value as object);
  if (Array.isArray(value)) {
    value.forEach((v, i) => { path.push(i); walk(v, path, skip, visit, seen); path.pop(); });
    return;
  }
  for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
    path.push(k); walk(v, path, skip, visit, seen); path.pop();
  }
}

function mapStrings(value: unknown, path: (string | number)[], skip: (p: JsonPath) => boolean, fn: (s: string) => string, seen: WeakMap<object, unknown>): unknown {
  if (typeof value === 'string') return skip(path) ? value : fn(value);
  if (!value || typeof value !== 'object') return value;
  if (seen.has(value as object)) return seen.get(value as object);
  if (Array.isArray(value)) {
    const out: unknown[] = [];
    seen.set(value, out);
    value.forEach((v, i) => { path.push(i); out.push(mapStrings(v, path, skip, fn, seen)); path.pop(); });
    return out;
  }
  const proto = Object.getPrototypeOf(value);
  if (proto !== Object.prototype && proto !== null) return value; // Date, Map, class instances
  const out: Record<string, unknown> = {};
  seen.set(value as object, out);
  for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
    path.push(k); out[k] = mapStrings(v, path, skip, fn, seen); path.pop();
  }
  return out;
}

function combinedSkip(opts?: DeepResolveOptions): (p: JsonPath) => boolean {
  const extra = opts?.skip;
  return extra ? (p) => defaultSkip(p) || extra(p) : defaultSkip;
}

function collectRefs(value: unknown, opts?: DeepResolveOptions): Map<string, string> {
  const found = new Map<string, string>();
  walk(value, [], combinedSkip(opts), (s) => {
    if (found.has(s)) return;
    const k = artifactKeyOf(s) ?? projectAssetKeyOf(s) ?? s3RestArtifactKeyOf(s);
    if (k) found.set(s, k);
  }, new WeakSet());
  return found;
}

/** True when `value` contains at least one artifact identifier that a deep resolve would replace. */
export function hasArtifactRefs(value: unknown, opts?: DeepResolveOptions): boolean {
  return collectRefs(value, opts).size > 0;
}

function substitute<T>(value: T, refs: Map<string, string>, resolved: (key: string) => ResolvedArtifactUrl | null | undefined, opts?: DeepResolveOptions): T {
  return mapStrings(value, [], combinedSkip(opts), (s) => {
    const key = refs.get(s);
    const r = key ? resolved(key) : null;
    if (!r) return s;
    rememberOriginal(r.url, s);
    return r.url;
  }, new WeakMap()) as T;
}

/**
 * Deep-copy a JSON value with every artifact identifier replaced by a presigned URL, resolved in
 * one batch. Strings that fail to resolve (and skipped paths) stay unchanged. Never rejects.
 */
export async function resolveArtifactUrlsDeep<T>(value: T, opts?: DeepResolveOptions): Promise<T> {
  await ensureArtifactsDomain();
  const refs = collectRefs(value, opts);
  let resolved = new Map<string, ResolvedArtifactUrl>();
  if (refs.size) resolved = await resolveArtifactUrls(new Set(refs.values()));
  return substitute(value, refs, (k) => resolved.get(k), opts);
}

/**
 * Sync variant for a warm cache: the resolved deep copy when every identifier in `value` is already
 * cached (or there are none), else null. Lets a remount render immediately without a loading flash.
 */
export function resolveArtifactUrlsDeepSync<T>(value: T, opts?: DeepResolveOptions): T | null {
  const refs = collectRefs(value, opts);
  for (const key of refs.values()) if (!peekArtifactUrl(key)) return null;
  return substitute(value, refs, (k) => peekArtifactUrl(k), opts);
}

// ---------------------------------------------------------------------------
// Reverse: presigned URL -> identifier
// ---------------------------------------------------------------------------

function unresolveString(s: string): string {
  const exact = originalByUrl.get(s);
  if (exact) return exact;
  const exactKey = keyByUrl.get(s);
  if (exactKey) return artifactIdentifierFor(exactKey);
  // Embedded in prose, e.g. an action prompt that quotes the clip's src.
  if (s.length < 24 || !s.includes('://')) return s;
  let out = s;
  for (const [url, original] of originalByUrl) {
    if (out.includes(url)) out = out.split(url).join(original);
  }
  for (const [url, key] of keyByUrl) {
    if (out.includes(url)) out = out.split(url).join(artifactIdentifierFor(key));
  }
  return out;
}

/**
 * Replace every presigned URL this module produced with the identifier it stands for. Applied to
 * template interaction payloads so an expiring URL never reaches chat history, a render tab, or an
 * agent prompt.
 */
export function unresolveArtifactUrls<T>(value: T): T {
  if (originalByUrl.size === 0 && keyByUrl.size === 0) return value;
  return mapStrings(value, [], () => false, unresolveString, new WeakMap()) as T;
}

// ---------------------------------------------------------------------------
// Agent HTML
// ---------------------------------------------------------------------------

// A candidate artifact URL inside an attribute value or a CSS url(): the CDN form or the proxy form.
// (A projects-session OUTPUT path counts too, asset-lineage Phase 3 (a); `artifactKeyOf` validates it.)
const CANDIDATE_RE = /(?:https?:\/\/[^\s"'<>()\\]+)?\/(?:api\/artifact\?key=|artifacts\/|projects\/p[0-9a-f]{32}\/sessions\/)[^\s"'<>()\\]*/gi;
const BARE_CANDIDATE_RE = /^\s*((?:artifacts\/|projects\/p[0-9a-f]{32}\/sessions\/)[^\s"'<>()\\]+)\s*$/;
const HAS_REF_RE = /artifacts\/|api\/artifact|projects\/p[0-9a-f]{32}\//;
const TOKEN_RE = /<style\b[^>]*>[\s\S]*?<\/style\s*>|<script\b[^>]*>[\s\S]*?<\/script\s*>|<!--[\s\S]*?-->|<[a-zA-Z][^>]*>/gi;
const ATTR_RE = /([\s/][^\s"'=<>/`]+\s*=\s*)(?:"([^"]*)"|'([^']*)')/g;
const CSS_URL_RE = /url\(\s*(["']?)([^"')]*?)\1\s*\)/gi;

function decodeEntities(s: string): string {
  return s.replace(/&(#x[0-9a-f]+|#[0-9]+|amp|quot|apos|lt|gt|#39);/gi, (m, e: string) => {
    const lower = e.toLowerCase();
    if (lower === 'amp') return '&';
    if (lower === 'quot') return '"';
    if (lower === 'apos' || lower === '#39') return "'";
    if (lower === 'lt') return '<';
    if (lower === 'gt') return '>';
    const code = lower.startsWith('#x') ? parseInt(lower.slice(2), 16) : parseInt(lower.slice(1), 10);
    return Number.isFinite(code) && code > 0 && code < 0x110000 ? String.fromCodePoint(code) : m;
  });
}

function encodeAttr(s: string, quote: '"' | "'"): string {
  const out = s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  return quote === '"' ? out.replace(/"/g, '&quot;') : out.replace(/'/g, '&#39;');
}

type UrlFn = (candidate: string) => string;

/** Replace candidates inside a DECODED attribute value / CSS text. */
function replaceCandidates(text: string, fn: UrlFn, allowBare: boolean): string {
  const bare = allowBare ? text.match(BARE_CANDIDATE_RE) : null;
  if (bare) return text.replace(bare[1], fn(bare[1]));
  return text.replace(CANDIDATE_RE, (m) => fn(m));
}

function transformCss(css: string, fn: UrlFn): string {
  return css.replace(CSS_URL_RE, (whole, q: string, url: string) => {
    const next = fn(url);
    // A raw-text <style> body is not entity-decoded, so the value must be URL-safe as is.
    if (next === url || /["'()\s\\<>]/.test(next)) return whole;
    return `url(${q}${next}${q})`;
  });
}

function transformTag(tag: string, fn: UrlFn): string {
  return tag.replace(ATTR_RE, (whole, lead: string, dq: string | undefined, sq: string | undefined) => {
    const quote: '"' | "'" = dq !== undefined ? '"' : "'";
    const rawValue = (dq ?? sq) as string;
    const decoded = decodeEntities(rawValue);
    const next = replaceCandidates(decoded, fn, true);
    if (next === decoded) return whole;
    return `${lead}${quote}${encodeAttr(next, quote)}${quote}`;
  });
}

function transformHtml(html: string, fn: UrlFn): string {
  return html.replace(TOKEN_RE, (token) => {
    if (token.startsWith('<!--')) return token;
    const lower = token.slice(0, 7).toLowerCase();
    if (lower.startsWith('<style')) {
      const close = token.indexOf('>') + 1;
      const end = token.toLowerCase().lastIndexOf('</style');
      return transformTag(token.slice(0, close), fn) + transformCss(token.slice(close, end), fn) + token.slice(end);
    }
    if (lower.startsWith('<script')) {
      const close = token.indexOf('>') + 1;
      // Only the opening tag's attributes (<script src>); script bodies are code, left alone.
      return transformTag(token.slice(0, close), fn) + token.slice(close);
    }
    return transformTag(token, fn);
  });
}

/**
 * The key an HTML attribute value names: an artifact identifier, a project-asset identifier, or a signed S3
 * URL of our own content baked into the page (the same chain as the deep resolver). Prod session d52d544e: a
 * sandbox-built page carried signed S3 URLs, which die when the signature expires (6 h) -- re-signing them at
 * view time keeps a deliverable working. Only the path is used; the server re-authorizes the key.
 */
function htmlRefKeyOf(c: string): string | null {
  return artifactKeyOf(c) ?? projectAssetKeyOf(c) ?? s3RestArtifactKeyOf(c);
}

/** True when agent HTML references at least one artifact URL that `resolveArtifactUrlsInHtml` would replace. */
export function htmlHasArtifactUrls(html: string): boolean {
  if (!html || !HAS_REF_RE.test(html)) return false;
  let found = false;
  transformHtml(html, (c) => { if (!found && htmlRefKeyOf(c)) found = true; return c; });
  return found;
}

/**
 * Substitute presigned URLs for artifact URLs in agent-authored HTML: attribute values (src, href,
 * poster, srcset, style="…url()…", …) and CSS `url()` in <style> blocks. Attribute values are
 * entity-decoded before matching and re-escaped after; text content and script bodies are left
 * alone. Unresolvable URLs stay as they were. Never rejects.
 */
export async function resolveArtifactUrlsInHtml(html: string): Promise<string> {
  if (!html || !HAS_REF_RE.test(html)) return html;
  await ensureArtifactsDomain();
  const refs = new Map<string, string>();
  transformHtml(html, (c) => {
    const k = htmlRefKeyOf(c);
    if (k) refs.set(c, k);
    return c;
  });
  if (!refs.size) return html;
  const resolved = await resolveArtifactUrls(new Set(refs.values()));
  return transformHtml(html, (c) => {
    const k = refs.get(c);
    const r = k ? resolved.get(k) : undefined;
    if (!r) return c;
    rememberOriginal(r.url, c);
    return r.url;
  });
}
