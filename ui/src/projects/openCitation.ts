/**
 * Opening a Verified_Citation and a Moment's Expand (design Component 5, "Opening a Verified_Citation";
 * Component 7, "Expand"; Req 2.8, 2.9, 2.11, 2.12).
 *
 * Both a verified citation link (Component 5) and a Moment card's Expand (Component 7) open the same
 * asset-player tab. The shared path is {@link openAssetAt}; {@link openCitation} is the thin
 * citation-record wrapper over it.
 *
 * The path, start to finish:
 *   1. Read the asset's status (`getAsset`) and mint a view-time URL (`getAssetUrls`) in PARALLEL. The
 *      status read is required because the Asset_URL_Route signs a URL for an asset in ANY status —
 *      `removed` included — so a URL alone cannot detect a removal.
 *   2. Map every refusal of Req 2.9 to its exact inline message, naming the Display_Name, and open no tab.
 *   3. Preflight-load the media in a detached element (video/audio `preload=metadata`, or an `Image`)
 *      BEFORE a tab exists, so Req 2.9 can refuse an asset that fails to load. One `error` or 20 s with
 *      no load counts as a failure, which triggers exactly ONE fresh `getAssetUrls` and one more try.
 *   4. On success, cache the minted URL in the in-memory {@link assetUrlCache} (never persisted) and
 *      dispatch `lab:open-asset`. Nothing here dispatches `chat:send`, calls `renderTab`, or reaches
 *      `/api/`, so no agent turn and no render-agent invocation starts (Req 2.8).
 *
 * A DOCUMENT (a PDF page hit) takes the same path minus the media preflight (a PDF has no metadata event to
 * wait for): its `lab:open-asset` tab is an AssetPlayer that embeds the PDF at the hit's page (`#page=N`,
 * decoded from `startMs` by `shared/pageLocator.ts`), inside the harness like every other open.
 */
import {
  getAsset as apiGetAsset,
  getAssetUrls as apiGetAssetUrls,
  type AssetUrlEntry,
  type ProjectsResult,
  type AssetDetail,
  type AssetUrlsResponse,
} from './api';
import type { CitationRecordWire } from '../api';
import type { MintedUrl } from './assetUrl';

// ----------------------------------------------------------------------------------- shared types

/** The asset kinds that open: the three the players handle, and a document (the browser's PDF viewer). */
export type AssetKind = 'video' | 'image' | 'audio' | 'document';

/** An Asset_Reference as carried on a verified citation record and a Moment. No URL. */
export interface AssetRef {
  project_id: string;
  asset_id: string;
  /** `null` for a time-range (non-frame) citation; a frame key for a `shot` Hit. */
  frame_s3_key: string | null;
}

/** The minimum a caller needs to open an asset at a position — a citation record or a Moment card. */
export interface OpenAssetTarget {
  ref: AssetRef;
  kind: AssetKind;
  displayName: string;
  /** Where the player opens, paused: the Citation's start (or the Moment's `start_ms`). */
  startMs: number;
  /** The span to SELECT on the full asset's timeline, in seconds of the media's own clock (a Moment's
   *  current In/Out). Absent for a Citation: nothing is selected. */
  range?: { start: number; end: number };
  /** The asset's canonical `s3://` location, when the opener knows it (a media action's own output). The tab
   *  carries it as `sourceLocation`, so a later analysis of the same file (its shots, its speakers) folds into
   *  THIS tab instead of opening a second one on the same video. */
  sourceLocation?: string;
}

/**
 * A citation record known to be verified. The verified-only fields of {@link CitationRecordWire} are
 * present (the SPA only ever calls {@link openCitation} for a record it rendered as a verified link).
 */
export interface VerifiedCitationRecord extends CitationRecordWire {
  verified: true;
  ref: AssetRef;
  kind: AssetKind;
  asset_display_name: string;
}

export type OpenCitationResult =
  | { ok: true }
  | { ok: false; message: string }; // always names the Display_Name (Req 2.9)

/** The `window` event name an open emits; `WorkspaceView` listens for it and appends an asset-player tab. */
export const OPEN_ASSET_EVENT = 'lab:open-asset';

/**
 * `lab:open-asset` detail. Carries the Asset_Reference, kind, Display_Name and start, the active session,
 * and the just-resolved URL (also seeded into {@link assetUrlCache}). The URL is transient — the listener
 * stores an Asset_Reference ONLY on the render-history block, never the URL (Req 2.12).
 */
export interface OpenAssetEventDetail {
  ref: AssetRef;
  kind: AssetKind;
  displayName: string;
  startMs: number;
  /** See {@link OpenAssetTarget.range}; present only when the opener passed one. */
  range?: { start: number; end: number };
  /** See {@link OpenAssetTarget.sourceLocation}. */
  sourceLocation?: string;
  sessionId: string;
  url: string;
}

/** Injection seams so tests can stub the API, the preflight loader, the clock, and the dispatch. */
export interface OpenCitationDeps {
  getAsset?: (projectId: string, assetId: string) => Promise<ProjectsResult<AssetDetail>>;
  getAssetUrls?: (projectId: string, assetIds: string[]) => Promise<ProjectsResult<AssetUrlsResponse>>;
  /** Resolves `true` on `loadedmetadata`/image `load`, `false` on `error` or after `timeoutMs`. */
  preloadMedia?: (url: string, kind: AssetKind, timeoutMs: number) => Promise<boolean>;
  dispatch?: (event: Event) => void;
  now?: () => number;
  cache?: Map<string, MintedUrl>;
}

// ------------------------------------------------------------------------------------- URL cache

/**
 * In-memory, process-local map of the last minted URL per asset, keyed by {@link assetCacheKey}. NEVER
 * persisted. Shared with the Moment cards (Component 7) so a card and a citation player of the same asset
 * reuse one fresh URL. An AssetPlayer reads it on mount and re-mints only when the entry is stale.
 */
export const assetUrlCache = new Map<string, MintedUrl>();

export function assetCacheKey(projectId: string, assetId: string): string {
  return `${projectId}/${assetId}`;
}

/** 20 s with no load (nor error) counts as a load failure (design Component 5, step 3). */
export const PREFLIGHT_TIMEOUT_MS = 20_000;

// --------------------------------------------------------------------------------- Req 2.9 messages

function accessLostMessage(name: string): string {
  return `Can't open ${name}: you no longer have access to this project.`;
}
function removedMessage(name: string): string {
  return `Can't open ${name}: it was removed from the project.`;
}
function loadFailedMessage(name: string): string {
  return `Can't open ${name}: it could not be loaded.`;
}


// --------------------------------------------------------------------------------- preflight loader

/**
 * Default DOM preflight: a detached media element (`preload=metadata`, muted) or an `Image` is given
 * `url`. Resolves `true` on load, `false` on `error` or after `timeoutMs`. Always detaches its listeners
 * and clears `src` so a failed load cannot keep issuing range requests.
 */
function defaultPreloadMedia(url: string, kind: AssetKind, timeoutMs: number): Promise<boolean> {
  return new Promise((resolve) => {
    if (typeof document === 'undefined' && kind !== 'image') {
      resolve(false);
      return;
    }
    let settled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const finish = (ok: boolean, cleanup: () => void) => {
      if (settled) return;
      settled = true;
      if (timer !== undefined) clearTimeout(timer);
      cleanup();
      resolve(ok);
    };

    if (kind === 'image') {
      const img = new Image();
      const cleanup = () => {
        img.onload = null;
        img.onerror = null;
        img.src = '';
      };
      img.onload = () => finish(true, cleanup);
      img.onerror = () => finish(false, cleanup);
      timer = setTimeout(() => finish(false, cleanup), timeoutMs);
      img.src = url;
      return;
    }

    const el = document.createElement(kind) as HTMLMediaElement;
    el.preload = 'metadata';
    el.muted = true;
    const onLoaded = () => finish(true, cleanup);
    const onError = () => finish(false, cleanup);
    const cleanup = () => {
      el.removeEventListener('loadedmetadata', onLoaded);
      el.removeEventListener('error', onError);
      el.removeAttribute('src');
      try {
        el.load();
      } catch {
        /* detached element; nothing to abort */
      }
    };
    el.addEventListener('loadedmetadata', onLoaded);
    el.addEventListener('error', onError);
    timer = setTimeout(() => finish(false, cleanup), timeoutMs);
    el.src = url;
  });
}

// ------------------------------------------------------------------------------------- source URL

/** The bytes a player loads: the source for video/audio, and for an image too (preflight uses source). */
function sourceUrlOf(entry: AssetUrlEntry): string | undefined {
  return entry.source?.url;
}

// ------------------------------------------------------------------------------------- open paths

/**
 * Open an asset at a position (a verified citation link or a Moment's Expand). Steps 1–4 of design
 * Component 5. On success resolves `{ ok: true }` after dispatching `lab:open-asset`; on a refusal
 * resolves `{ ok: false, message }` with the exact Req 2.9 message and dispatches nothing.
 */
export async function openAssetAt(
  target: OpenAssetTarget,
  sessionId: string,
  deps?: OpenCitationDeps,
): Promise<OpenCitationResult> {
  const getAsset = deps?.getAsset ?? apiGetAsset;
  const getAssetUrls = deps?.getAssetUrls ?? apiGetAssetUrls;
  const preloadMedia = deps?.preloadMedia ?? defaultPreloadMedia;
  const dispatch = deps?.dispatch ?? ((e: Event) => window.dispatchEvent(e));
  const now = deps?.now ?? Date.now;
  const cache = deps?.cache ?? assetUrlCache;

  const { ref, kind, displayName } = target;
  const name = displayName;

  // Steps 1–2: status and URL in parallel, then the Req 2.9 refusals.
  const source = await resolveSource(ref, name, getAsset, getAssetUrls);
  if (!source.ok) return source;
  const firstUrl = source.url;

  // Step 3: preflight load, with one fresh getAssetUrls retry on failure.
  let url = firstUrl;
  // A document has nothing to preflight (no metadata event); the embedded viewer reports its own load.
  let loaded = kind === 'document' ? true : await preloadMedia(url, kind, PREFLIGHT_TIMEOUT_MS);
  if (!loaded) {
    const retryRes = await getAssetUrls(ref.project_id, [ref.asset_id]);
    const retryUrl =
      retryRes.ok ? sourceUrlOf(retryRes.value.urls[ref.asset_id] ?? ({} as AssetUrlEntry)) : undefined;
    if (!retryUrl) {
      return { ok: false, message: loadFailedMessage(name) };
    }
    url = retryUrl;
    loaded = await preloadMedia(url, kind, PREFLIGHT_TIMEOUT_MS);
    if (!loaded) {
      return { ok: false, message: loadFailedMessage(name) };
    }
  }

  // Step 4: cache the URL (never persisted) and announce the open. No chat:send, no renderTab, no /api.
  cache.set(assetCacheKey(ref.project_id, ref.asset_id), { url, obtainedAt: now() });
  const detail: OpenAssetEventDetail = {
    ref,
    kind,
    displayName: name,
    startMs: target.startMs,
    ...(target.range ? { range: { start: target.range.start, end: target.range.end } } : {}),
    ...(target.sourceLocation ? { sourceLocation: target.sourceLocation } : {}),
    sessionId,
    url,
  };
  dispatch(new CustomEvent(OPEN_ASSET_EVENT, { detail }));
  return { ok: true };
}

/**
 * Steps 1–2 of the open path: read status and mint a URL in parallel, then map every refusal of Req 2.9,
 * in the design's precedence order, to its message. Shared by the player and document paths.
 */
async function resolveSource(
  ref: AssetRef,
  name: string,
  getAsset: NonNullable<OpenCitationDeps['getAsset']>,
  getAssetUrls: NonNullable<OpenCitationDeps['getAssetUrls']>,
): Promise<{ ok: true; url: string } | { ok: false; message: string }> {
  const [statusRes, urlsRes] = await Promise.all([
    getAsset(ref.project_id, ref.asset_id),
    getAssetUrls(ref.project_id, [ref.asset_id]),
  ]);
  // (a) not_found from EITHER call — the route-level 404 or the per-id error entry.
  const urlNotFound =
    (!urlsRes.ok && urlsRes.kind === 'not_found') ||
    (urlsRes.ok && urlsRes.value.errors[ref.asset_id]?.code === 'project_not_found');
  if ((!statusRes.ok && statusRes.kind === 'not_found') || urlNotFound) {
    return { ok: false, message: accessLostMessage(name) };
  }
  // (b) removed.
  if (statusRes.ok && statusRes.value.status === 'removed') {
    return { ok: false, message: removedMessage(name) };
  }
  // (c) any other failure to obtain a URL.
  if (!urlsRes.ok) return { ok: false, message: loadFailedMessage(name) };
  const entry = urlsRes.value.urls[ref.asset_id];
  const url = entry ? sourceUrlOf(entry) : undefined;
  return url ? { ok: true, url } : { ok: false, message: loadFailedMessage(name) };
}

/**
 * Open a verified Citation's asset at the Citation's start. Thin wrapper over {@link openAssetAt} using
 * the record's Asset_Reference, kind, Display_Name and `start_ms`.
 */
/** The asset's loadable source URL — the same lookup (and the same access / removed / failed messages) the
 *  side-panel open uses — for the chat's preview of a citation, which does not touch the Renderer. */
export function resolveCitationSource(
  ref: AssetRef,
  name: string,
  deps?: Pick<OpenCitationDeps, 'getAsset' | 'getAssetUrls'>,
): Promise<{ ok: true; url: string } | { ok: false; message: string }> {
  return resolveSource(ref, name, deps?.getAsset ?? apiGetAsset, deps?.getAssetUrls ?? apiGetAssetUrls);
}

export function openCitation(
  record: VerifiedCitationRecord,
  sessionId: string,
  deps?: OpenCitationDeps,
): Promise<OpenCitationResult> {
  return openAssetAt(
    {
      ref: record.ref,
      kind: record.kind,
      displayName: record.asset_display_name,
      startMs: record.start_ms,
    },
    sessionId,
    deps,
  );
}
