/**
 * Asset-URL request batcher (Component 7, "URLs at view time"; Req 3.26, 3.27).
 *
 * The Asset_URL_Route (`getAssetUrls`, Task 8.1) mints a view-time presigned URL per asset id and
 * accepts at most 100 ids per call. A Moments_Gallery mounting 50 cards would otherwise fire 50
 * concurrent requests; this module coalesces the concurrent `getAssetUrls` calls of a single Project
 * into one request per microtask (chunked to 100 ids), so a gallery of up to 50 cards costs one
 * request on open. Refreshes triggered close together (Play all resuming several stale cards, or a
 * 403 burst after the signer's credentials expire) that fall in the same microtask are merged the
 * same way.
 *
 * Pure of React and the DOM. It keeps one module-level map of pending (not-yet-flushed) batches keyed
 * by `projectId`; each flush is scheduled on the next microtask. `request` resolves with a per-id
 * result map drawn from the batched {@link AssetUrlsResponse} — never throws, because `getAssetUrls`
 * never does.
 */
import {
  getAssetUrls,
  type FrameAsk,
  type AssetUrlEntry,
  type AssetUrlsResponse,
  type ProjectsApiErrorBody,
  type ProjectsResult,
} from './api';
import { noteAssetFolders, setAssetFolderFetcher } from './assetFolders';

/** The route's per-call id ceiling (Asset_URL_Route; see `getAssetUrls`). */
export const MAX_IDS_PER_CALL = 100;

/**
 * The outcome for one requested asset id, distilled from the batched response so a caller never has to
 * join `urls` and `errors` itself:
 *   * `ok`        — the id's {@link AssetUrlEntry} (source bytes, plus a `rendition` for an image).
 *   * `not_found` — the Actor cannot see this asset: a per-id `project_not_found`, or a whole-route
 *                   denial (`404` / access lost). Maps to a card's `no_access` phase (Req 3.28).
 *   * `error`     — any other failure (Projects disabled, network, an unexpected per-id code, or an id
 *                   the route returned in neither map). Maps to a card's `load_failed` phase (Req 3.15).
 */
export type AssetUrlOutcome =
  | { ok: true; entry: AssetUrlEntry }
  | { ok: false; kind: 'not_found' }
  | { ok: false; kind: 'error'; code: string; message: string };

interface BatchResult { assets: Map<string, AssetUrlOutcome>; frames: Map<string, string> }

interface PendingBatch {
  /** Distinct ids collected this microtask (dedupes in-flight ids within the batch). */
  ids: Set<string>;
  /** Moment pictures asked for this microtask, by `"<asset_id>@<at_ms>"` (one request carries both). */
  frames: Map<string, FrameAsk>;
  /** Shared promise every waiter of this batch awaits, resolving to the combined results. */
  promise: Promise<BatchResult>;
  resolve: (results: BatchResult) => void;
}

/** One pending batch per Project, replaced the moment it flushes. */
const pending = new Map<string, PendingBatch>();

const NOT_FOUND: AssetUrlOutcome = { ok: false, kind: 'not_found' };

function scheduleMicrotask(fn: () => void): void {
  if (typeof queueMicrotask === 'function') queueMicrotask(fn);
  else void Promise.resolve().then(fn);
}

/** Map a whole-route failure onto a single id's outcome. */
function outcomeFromFailure(failure: Exclude<ProjectsResult<never>, { ok: true }>): AssetUrlOutcome {
  switch (failure.kind) {
    case 'not_found':
    case 'access_lost':
      // Both mean the Actor no longer has access to the Project (Req 3.28).
      return NOT_FOUND;
    case 'disabled':
      return { ok: false, kind: 'error', code: 'projects_disabled', message: 'Projects are not enabled.' };
    case 'error':
      return { ok: false, kind: 'error', code: failure.code, message: failure.message };
  }
}

/** Map one per-id `errors` entry onto its outcome. */
function outcomeFromPerIdError(err: ProjectsApiErrorBody): AssetUrlOutcome {
  if (err.code === 'project_not_found') return NOT_FOUND;
  return { ok: false, kind: 'error', code: err.code, message: err.message };
}

/** Distribute one chunk's {@link ProjectsResult} across the chunk's ids into `into`. */
function spreadChunkResult(
  chunkIds: string[],
  result: ProjectsResult<AssetUrlsResponse>,
  into: Map<string, AssetUrlOutcome>,
): void {
  if (!result.ok) {
    const outcome = outcomeFromFailure(result);
    for (const id of chunkIds) into.set(id, outcome);
    return;
  }
  const { urls, errors } = result.value;
  for (const id of chunkIds) {
    const entry = urls[id];
    if (entry) {
      into.set(id, { ok: true, entry });
      continue;
    }
    const err = errors[id];
    if (err) {
      into.set(id, outcomeFromPerIdError(err));
      continue;
    }
    // The route promises each id lands in exactly one map; a missing id is an unexpected reply.
    into.set(id, {
      ok: false,
      kind: 'error',
      code: 'url_missing',
      message: `No URL or error returned for asset ${id}.`,
    });
  }
}

/** Fetch every id of a flushed batch, chunked to {@link MAX_IDS_PER_CALL}, into one combined map. */
async function runBatch(projectId: string, ids: string[], frameAsks: FrameAsk[] = []): Promise<BatchResult> {
  const combined = new Map<string, AssetUrlOutcome>();
  const frames = new Map<string, string>();
  const chunks: { ids: string[]; frames: FrameAsk[] }[] = [];
  const n = Math.max(Math.ceil(ids.length / MAX_IDS_PER_CALL), Math.ceil(frameAsks.length / MAX_IDS_PER_CALL));
  for (let c = 0; c < n; c++) {
    chunks.push({ ids: ids.slice(c * MAX_IDS_PER_CALL, (c + 1) * MAX_IDS_PER_CALL),
      frames: frameAsks.slice(c * MAX_IDS_PER_CALL, (c + 1) * MAX_IDS_PER_CALL) });
  }
  await Promise.all(
    chunks.map(async ({ ids: chunk, frames: chunkFrames }) => {
      let result: ProjectsResult<AssetUrlsResponse>;
      try {
        // Contractually never undefined; a stub that answers nothing must not break the batch.
        result = (await getAssetUrls(projectId, chunk, chunkFrames))
          ?? { ok: false, kind: 'error', status: 0, code: 'batch_error', message: 'no answer' };
      } catch (e) {
        // `getAssetUrls` is contractually non-throwing; treat a surprise as a per-chunk error.
        const message = e instanceof Error ? e.message : String(e);
        result = { ok: false, kind: 'error', status: 0, code: 'batch_error', message };
      }
      if (result.ok) {
        // Every URL answer also says where each file is filed: the folder store is fed for free.
        const folders: Record<string, string> = {};
        for (const [id, entry] of Object.entries(result.value.urls)) if (typeof entry?.folder === 'string') folders[id] = entry.folder;
        noteAssetFolders(projectId, folders);
      }
      spreadChunkResult(chunk, result, combined);
      if (result.ok) for (const [k, f] of Object.entries(result.value.frames ?? {})) if (f?.url) frames.set(k, f.url);
    }),
  );
  return { assets: combined, frames };
}

/**
 * Request view-time URLs for `assetIds` within `projectId`, coalesced with every other call made in
 * the same microtask for the same Project. Resolves with a per-id {@link AssetUrlOutcome} map holding
 * exactly the (non-empty, deduped) ids requested. Blank ids are dropped; an empty request resolves to
 * an empty map without any network call.
 */
/** The Project's batch collecting this microtask's asks, opened (and its flush scheduled) on first use. */
function batchFor(projectId: string): PendingBatch {
  let batch = pending.get(projectId);
  if (!batch) {
    let resolveFn!: (results: BatchResult) => void;
    const promise = new Promise<BatchResult>((res) => {
      resolveFn = res;
    });
    const opened: PendingBatch = { ids: new Set(), frames: new Map(), promise, resolve: resolveFn };
    batch = opened;
    pending.set(projectId, opened);
    scheduleMicrotask(() => {
      // Detach this batch so calls arriving after the flush open a fresh one.
      pending.delete(projectId);
      void runBatch(projectId, Array.from(opened.ids), Array.from(opened.frames.values())).then(opened.resolve);
    });
  }
  return batch;
}

export function request(projectId: string, assetIds: string[]): Promise<Map<string, AssetUrlOutcome>> {
  const requested = Array.from(new Set(assetIds.filter((id) => typeof id === 'string' && id.length > 0)));
  if (requested.length === 0) return Promise.resolve(new Map());
  const batch = batchFor(projectId);
  for (const id of requested) batch.ids.add(id);

  // Each waiter awaits the shared batch result, then projects out only the ids it asked for.
  return batch.promise.then(({ assets }) => {
    const mine = new Map<string, AssetUrlOutcome>();
    for (const id of requested) {
      const outcome = assets.get(id);
      if (outcome) mine.set(id, outcome);
    }
    return mine;
  });
}

/**
 * A moment's own picture per ask (`"<asset_id>@<at_ms>"` → URL), in the SAME request as the cards' asset URLs
 * (coalesced per microtask), so a gallery page still costs one call. An ask the route cannot serve is absent.
 */
export function requestFrames(projectId: string, asks: FrameAsk[]): Promise<Map<string, string>> {
  if (asks.length === 0) return Promise.resolve(new Map());
  const batch = batchFor(projectId);
  for (const a of asks) batch.frames.set(`${a.asset_id}@${a.at_ms}`, a);
  return batch.promise.then(({ frames }) => {
    const mine = new Map<string, string>();
    for (const a of asks) {
      const url = frames.get(`${a.asset_id}@${a.at_ms}`);
      if (url) mine.set(`${a.asset_id}@${a.at_ms}`, url);
    }
    return mine;
  });
}

// The folder store asks for an unknown id through this batcher, so its lookups coalesce with the cards' URL requests.
setAssetFolderFetcher((projectId, assetIds) => request(projectId, assetIds));
