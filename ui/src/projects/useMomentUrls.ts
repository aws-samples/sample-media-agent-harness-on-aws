/**
 * `useMomentUrls` — view-time presigned URLs for a Moments_Gallery (Component 7,
 * "URLs at view time"; Req 3.26, 3.27, 3.28, 3.15).
 *
 * A {@link MomentItem} carries NO media URL by construction — only an Asset_Reference. The SPA mints
 * short-lived presigned URLs at the moment a card is shown, and never persists one. This hook owns that
 * minting for a whole {@link MomentsBlock}:
 *
 *   - On mount it collects the distinct `asset_id`s of the block's cards (all share the block's
 *     `project_id`) and requests them through {@link request | assetUrlBatcher.request} in ONE call (the
 *     batcher chunks to 100 ids). The gallery passes only its SHOWN page of cards, so opening costs one
 *     request for that page; when "Load more" grows the block, cards already seeded keep their machine
 *     state and only the new cards are seeded and minted (one more batched request).
 *   - Each card runs its own copy of Part 2's `assetUrl.ts` `step` machine, unchanged. The hook only
 *     routes element events (`onLoadStart` / `onLoadOk` / `onLoadError`) into that machine and executes
 *     the `mint` instruction it returns. So each load gets at most one refresh, a load never starts
 *     with a URL obtained more than {@link URL_MAX_AGE_MS | 50 minutes} earlier, and `load_failed` is
 *     absorbing (Req 3.27).
 *   - A whole-route `404` or a per-id `project_not_found` maps to the uniform `no_access` phase (Req
 *     3.28); any other mint failure, or a second error after the one refresh, maps to `load_failed`
 *     (Req 3.15).
 *   - `freshUrlFor` is the bulk-action entry point: it returns a URL no older than 50 minutes (minting
 *     one when needed, coalesced through the same batcher) or `null` for a card that cannot be served.
 *
 * Cards of the same asset share one minted URL through a module-level `assetUrlCache` (in memory, never
 * persisted), so a refresh by one card is reused by the next card's `load_start` and a never-opened
 * gallery tab mints nothing.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  isStale,
  step,
  URL_MAX_AGE_MS,
  type FailReason,
  type MintedUrl,
  type Resume,
  type UrlEvent,
  type UrlState,
} from './assetUrl';
import { request as batchRequest, type AssetUrlOutcome } from './assetUrlBatcher';
import type { MomentItem, MomentsBlock } from '../components/renderer/types';
import type { CardUrlPhase } from '../components/renderer/templates/momentText';

/**
 * The URL a Moment card shows, derived from its `assetUrl.ts` machine:
 *   - `minting`     — the first mint (on open) or a refresh is in flight; show a spinner.
 *   - `ready`       — a usable URL (video/audio: `source`; image: `rendition ?? source`) with the
 *                     epoch ms it was obtained, so a caller can judge staleness itself.
 *   - `no_access`   — the Actor lost access to the Project (uniform not-found, Req 3.28).
 *   - `load_failed` — any other failure, or a second error after the one refresh (Req 3.15).
 */
export type CardUrl =
  | { phase: 'minting' }
  | { phase: 'ready'; url: string; obtainedAt: number; poster?: string }
  | { phase: 'no_access' }
  | { phase: 'load_failed' };

// Compile-time guarantee that `CardUrl`'s phases stay exactly in step with momentText's `CardUrlPhase`
// (the gallery reads one through the other). Both directions, so neither can drift.
type _PhaseInSync = CardUrl['phase'] extends CardUrlPhase
  ? CardUrlPhase extends CardUrl['phase']
    ? true
    : never
  : never;
const _phaseInSync: _PhaseInSync = true;
void _phaseInSync;

export interface MomentUrls {
  /** The current {@link CardUrl} for a card; `minting` for an id not in this block. */
  state: (momentId: string) => CardUrl;
  /** A media `loadstart` / `play` / `seeking`, or the card mount: may trigger the load's one refresh. */
  onLoadStart: (momentId: string, resume: Resume) => void;
  /** A media `loadedmetadata` / image `load`: the (re)load landed. */
  onLoadOk: (momentId: string) => void;
  /** A media/image `error`: refreshes once, then moves the card to `load_failed`. */
  onLoadError: (momentId: string, resume: Resume) => void;
  /**
   * A URL for a bulk action, no older than 50 minutes (minted through the batcher when needed), or
   * `null` for a card that cannot be served. A not-found moves the card to `no_access` (Req 3.28).
   */
  freshUrlFor: (momentId: string) => Promise<string | null>;
}

/** Per-card static facts the machine needs: which asset to mint, and how to pick its URL. */
interface CardMeta {
  assetId: string;
  kind: MomentItem['kind'];
}

/**
 * In-memory, never-persisted cache of the last minted URL per `project_id`/`asset_id`. Shared across
 * every card and every mount so a refresh is reused and a remounted gallery reuses fresh entries while
 * re-minting stale ones. Keyed with a NUL separator (neither a Project nor an asset id contains one).
 */
const assetUrlCache = new Map<string, MintedUrl>();

function cacheKeyFor(projectId: string, assetId: string): string {
  return `${projectId}\u0000${assetId}`;
}

/** Distil a batched {@link AssetUrlOutcome} into either a minted URL or a terminal failure reason. */
function outcomeToMint(
  outcome: AssetUrlOutcome | undefined,
  kind: MomentItem['kind'],
  now: number,
): { minted: MintedUrl } | { fail: FailReason } {
  if (!outcome) return { fail: 'load_failed' };
  if (!outcome.ok) return { fail: outcome.kind === 'not_found' ? 'not_found' : 'load_failed' };
  // Image cards prefer the rendition (a lighter still) and fall back to the source bytes. A video card shows
  // its rendition as the picture and keeps the source for Preview / Play all, so a gallery mounts no players
  // (24 at once held every connection to S3, and Firefox's ~6 per host stalled the one you clicked).
  const picked = kind === 'image' ? outcome.entry.rendition ?? outcome.entry.source : outcome.entry.source;
  const poster = kind === 'video' ? outcome.entry.rendition?.url : undefined;
  return { minted: { url: picked.url, obtainedAt: now, expiresAt: picked.expiresAt, ...(poster ? { poster } : {}) } };
}

/** Map a card's `assetUrl.ts` {@link UrlState} to the {@link CardUrl} the gallery renders. */
function toCardUrl(s: UrlState | undefined): CardUrl {
  if (!s) return { phase: 'minting' };
  switch (s.phase) {
    case 'minting':
      return { phase: 'minting' };
    case 'ready':
      return { phase: 'ready', url: s.minted.url, obtainedAt: s.minted.obtainedAt,
        ...(s.minted.poster ? { poster: s.minted.poster } : {}) };
    case 'failed':
      // `load_failed` is the one failure the Actor may retry past; `not_found` / `removed` mean the
      // asset is gone for this Actor and present uniformly as `no_access` (Req 3.28, 3.15).
      return s.reason === 'load_failed' ? { phase: 'load_failed' } : { phase: 'no_access' };
  }
}

export function useMomentUrls(block: MomentsBlock): MomentUrls {
  const projectId = block.project_id;
  const items = block.items;

  // Static per-card facts, rebuilt only when the item set changes.
  const cardMeta = useMemo(() => {
    const m = new Map<string, CardMeta>();
    for (const it of items) m.set(it.id, { assetId: it.ref.asset_id, kind: it.kind });
    return m;
  }, [items]);

  // A stable signature of the mounted cards; re-initializes the machines when the block's cards change.
  const itemsKey = useMemo(() => items.map((i) => i.id).join('\u0000'), [items]);

  // Latest values read by the (stable) callbacks below without re-subscribing children.
  const projectIdRef = useRef(projectId);
  projectIdRef.current = projectId;
  const cardMetaRef = useRef(cardMeta);
  cardMetaRef.current = cardMeta;

  // The source of truth: one `assetUrl.ts` state per card. Held in a ref (callbacks read the latest)
  // with a render counter so the component repaints on every transition.
  const statesRef = useRef<Map<string, UrlState>>(new Map());
  const [, forceRender] = useState(0);
  const bump = useCallback(() => forceRender((n) => n + 1), []);

  const mountedRef = useRef(true);
  // `doMint` and `applyEvent` are mutually recursive; break the cycle through a ref.
  const doMintRef = useRef<(momentId: string) => void>(() => {});

  // Apply one event to a card's machine, repaint, and run any mint the machine asked for.
  const applyEvent = useCallback(
    (momentId: string, event: UrlEvent) => {
      const prev = statesRef.current.get(momentId);
      if (!prev) return;
      const { state: next, mint } = step(prev, event);
      statesRef.current.set(momentId, next);
      bump();
      if (mint) doMintRef.current(momentId);
    },
    [bump],
  );

  // Directly drop a card to a terminal phase, outside the per-load machine — used only by
  // `freshUrlFor` when a bulk-action mint discovers the asset is no longer accessible (Req 3.28).
  const markFailed = useCallback(
    (momentId: string, reason: FailReason) => {
      const prev = statesRef.current.get(momentId);
      if (!prev || prev.phase === 'failed') return;
      statesRef.current.set(momentId, { phase: 'failed', reason });
      bump();
    },
    [bump],
  );

  // Mint a fresh URL for one card and feed the result back into its machine. Reuses a cache entry that
  // is still fresh; otherwise one batched request (coalesced with any sibling refresh this microtask).
  const doMint = useCallback(
    async (momentId: string) => {
      const meta = cardMetaRef.current.get(momentId);
      if (!meta) return;
      const pid = projectIdRef.current;
      const key = cacheKeyFor(pid, meta.assetId);

      const cached = assetUrlCache.get(key);
      if (cached && !isStale(cached, Date.now())) {
        applyEvent(momentId, { type: 'minted', minted: cached });
        return;
      }

      const results = await batchRequest(pid, [meta.assetId]);
      if (!mountedRef.current) return;
      const conv = outcomeToMint(results.get(meta.assetId), meta.kind, Date.now());
      if ('minted' in conv) {
        assetUrlCache.set(key, conv.minted);
        applyEvent(momentId, { type: 'minted', minted: conv.minted });
      } else {
        applyEvent(momentId, { type: 'mint_failed', reason: conv.fail });
      }
    },
    [applyEvent],
  );
  useEffect(() => {
    doMintRef.current = (momentId) => void doMint(momentId);
  }, [doMint]);

  // Mount / block-change: seed every NEW card's machine (reusing fresh cache entries) and fire the single
  // batch request for the distinct asset ids still needing a URL. A card already seeded under the same
  // Project keeps its state, so paging in more cards never resets or re-mints the ones on screen.
  const seededProjectRef = useRef<string | null>(null);
  useEffect(() => {
    mountedRef.current = true;
    const pid = projectIdRef.current;
    const meta = cardMetaRef.current;
    const previous = seededProjectRef.current === pid ? statesRef.current : new Map<string, UrlState>();
    seededProjectRef.current = pid;

    const seeded = new Map<string, UrlState>();
    const idsToMint = new Set<string>();
    for (const [momentId, m] of meta) {
      const kept = previous.get(momentId);
      if (kept) {
        seeded.set(momentId, kept);
        continue;
      }
      const cached = assetUrlCache.get(cacheKeyFor(pid, m.assetId));
      if (cached && !isStale(cached, Date.now())) {
        // A fresh cache entry means this card is immediately ready — no mint, full refresh budget.
        seeded.set(momentId, { phase: 'ready', minted: cached, refreshUsed: false, resume: null });
      } else {
        seeded.set(momentId, { phase: 'minting', resume: null });
        idsToMint.add(m.assetId);
      }
    }
    statesRef.current = seeded;
    bump();

    if (idsToMint.size > 0) {
      void batchRequest(pid, Array.from(idsToMint)).then((results) => {
        if (!mountedRef.current) return;
        const now = Date.now();
        for (const [momentId, m] of cardMetaRef.current) {
          if (!idsToMint.has(m.assetId)) continue; // not part of this batch (kept or cache-served)
          const cur = statesRef.current.get(momentId);
          if (!cur || cur.phase !== 'minting') continue; // cards served from cache aren't in the batch
          const outcome = results.get(m.assetId);
          if (!outcome) continue;
          const conv = outcomeToMint(outcome, m.kind, now);
          if ('minted' in conv) {
            assetUrlCache.set(cacheKeyFor(pid, m.assetId), conv.minted);
            applyEvent(momentId, { type: 'minted', minted: conv.minted });
          } else {
            applyEvent(momentId, { type: 'mint_failed', reason: conv.fail });
          }
        }
      });
    }

    return () => {
      mountedRef.current = false;
    };
    // `applyEvent` is stable; the identity deps capture a block whose cards or Project changed.
  }, [itemsKey, projectId, applyEvent, bump]);

  const state = useCallback((momentId: string): CardUrl => toCardUrl(statesRef.current.get(momentId)), []);

  const onLoadStart = useCallback(
    (momentId: string, resume: Resume) =>
      applyEvent(momentId, { type: 'load_start', now: Date.now(), resume }),
    [applyEvent],
  );
  const onLoadOk = useCallback((momentId: string) => applyEvent(momentId, { type: 'load_ok' }), [applyEvent]);
  const onLoadError = useCallback(
    (momentId: string, resume: Resume) => applyEvent(momentId, { type: 'load_error', resume }),
    [applyEvent],
  );

  const freshUrlFor = useCallback(
    async (momentId: string): Promise<string | null> => {
      const meta = cardMetaRef.current.get(momentId);
      if (!meta) return null;
      const pid = projectIdRef.current;
      const now = Date.now();

      const cur = statesRef.current.get(momentId);
      // A `no_access` / `load_failed` card is excluded from every bulk action.
      if (cur && cur.phase === 'failed') return null;
      // The card's own in-hand URL, if still fresh.
      if (cur && cur.phase === 'ready' && !isStale(cur.minted, now)) return cur.minted.url;
      // A sibling card of the same asset may already hold a fresh URL.
      const cached = assetUrlCache.get(cacheKeyFor(pid, meta.assetId));
      if (cached && !isStale(cached, now)) return cached.url;

      const results = await batchRequest(pid, [meta.assetId]);
      const conv = outcomeToMint(results.get(meta.assetId), meta.kind, Date.now());
      if ('minted' in conv) {
        assetUrlCache.set(cacheKeyFor(pid, meta.assetId), conv.minted);
        return conv.minted.url;
      }
      // A not-found drops the card to `no_access` for good (Req 3.28); a transient error just drops
      // the card out of this one request without changing its displayed phase.
      if (mountedRef.current && conv.fail !== 'load_failed') markFailed(momentId, conv.fail);
      return null;
    },
    [markFailed],
  );

  return { state, onLoadStart, onLoadOk, onLoadError, freshUrlFor };
}

// Re-exported so callers can reason about the freshness bound without importing `assetUrl.ts` directly.
export { URL_MAX_AGE_MS };
