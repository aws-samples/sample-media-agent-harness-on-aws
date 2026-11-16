/**
 * AssetPlayer — the `asset-player` renderer template (design Component 5, "AssetPlayer template";
 * Req 2.8, 2.11, 2.12).
 *
 * A verified Citation (Component 5) or a Moment's Expand (Component 7) opens one of these tabs through
 * WorkspaceView's `lab:open-asset` listener. The block data is an Asset_Reference and NO URL —
 * `{ ref, kind, displayName, startMs, origin }` — so a persisted tab stays valid: the player mints a
 * fresh presigned URL for the ref every time it mounts, and never stores one.
 *
 * What it renders:
 *   - `video` / `audio` → `NleTimeline` with an inline single-track manifest, opened PAUSED and seeked
 *     to `startMs` via NleTimeline's `initialTime`.
 *   - `image`           → a plain `<img>`.
 *
 * URL lifetime (Req 2.12): a presigned URL is short-lived, so the player drives the shared, pure
 * `assetUrl.ts` state machine. On a load error (treated as a possible 403, since a media element exposes
 * no HTTP status) or a `play`/`seeking` with a URL older than 50 minutes, it re-mints ONCE and, on the
 * next load, resumes at the position and play state captured before the swap. The `initialTime` seek is
 * first-load only, so a refresh never jumps back to the Citation's start. The failure state is absorbing
 * — the player cannot loop on mints — and it never starts an agent turn or changes a stored citation.
 */
import { useRefreshedHtml } from '../../../projects/htmlEmbeds';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import type { RendererProps } from '../types';
import type { RenderManifest } from '../../nle/manifestParser';
import NleTimeline from './NleTimeline';
import { pageOfSpan, pdfUrlWithPage } from '../../../shared/pageLocator';
import type { RenderViewState } from '../../../shared/renderViewState';
import {
  assetCacheKey,
  assetUrlCache,
  type AssetKind,
  type AssetRef,
  type OpenCitationDeps,
} from '../../../projects/openCitation';
import {
  getAsset as apiGetAsset,
  getAssetUrls as apiGetAssetUrls,
} from '../../../projects/api';
import {
  isStale,
  step,
  type MintedUrl,
  type Resume,
  type UrlEvent,
  type UrlState,
} from '../../../projects/assetUrl';

/** The `asset-player` block data: an Asset_Reference plus display facts, and no URL (Req 2.12). */
export interface AssetPlayerData {
  ref: AssetRef;
  kind: AssetKind;
  displayName: string;
  /** Where the player opens, paused: the Citation's start (or the Moment's `start_ms`). */
  startMs: number;
  /** `citation` (verified link) or `moment` (Expand); informational only. */
  origin?: string;
  /** A persisted view's timeline state (range, frame marker, crop box), restored on mount (R8.2). */
  viewState?: RenderViewState;
  /** Set when the tab is a persisted view (ManifestRouter): reports each timeline-state change. */
  onViewStateChange?: (viewState: RenderViewState) => void;
}

/** The refusal message shown in place of the player when the asset cannot be served. */
function failureMessage(reason: 'not_found' | 'removed' | 'load_failed', name: string): string {
  if (reason === 'not_found') return `You no longer have access to this project.`;
  return `${name} could not be loaded.`;
}

export default function AssetPlayer({ data, theme: t, onInteraction }: RendererProps<AssetPlayerData> & { deps?: OpenCitationDeps }) {
  const { ref, kind, displayName, startMs } = data;
  const assetId = ref.asset_id;
  const projectId = ref.project_id;

  // Test/injection seams; default to the real API, clock, and shared cache.
  const deps = (data as unknown as { deps?: OpenCitationDeps }).deps;
  const getAsset = deps?.getAsset ?? apiGetAsset;
  const getAssetUrls = deps?.getAssetUrls ?? apiGetAssetUrls;
  const now = deps?.now ?? Date.now;
  const cache = deps?.cache ?? assetUrlCache;

  // The pure URL machine is the source of truth; a render counter repaints on each transition. The
  // URL actually rendered is held separately so a refresh (state → `minting`) keeps the current URL
  // on screen until the new one lands — which keeps NleTimeline MOUNTED, so its first-load seek is
  // not re-applied.
  const stateRef = useRef<UrlState>({ phase: 'minting', resume: null });
  const [, force] = useState(0);
  const bump = useCallback(() => force((n) => n + 1), []);
  const [displayUrl, setDisplayUrl] = useState<string | null>(null);
  const mountedRef = useRef(true);

  // Latest playback position + play intent, captured BEFORE a swap (a media error can reset the
  // element's `paused`/`currentTime`, so we must not read them after the error).
  const lastResumeRef = useRef<Resume>({ time: startMs / 1000, playing: false });

  const key = assetCacheKey(projectId, assetId);
  const doMintRef = useRef<(resume: Resume | null) => void>(() => {});

  // Apply one event to the machine, repaint, and run any mint it requested.
  const apply = useCallback(
    (event: UrlEvent) => {
      const { state: next, mint } = step(stateRef.current, event);
      stateRef.current = next;
      if (next.phase === 'ready') setDisplayUrl(next.minted.url);
      bump();
      if (mint) doMintRef.current(next.phase === 'minting' ? next.resume : null);
    },
    [bump],
  );

  // Mint a URL for the ref and feed the result back into the machine. The initial mount (`resume ===
  // null`) reads the status too, so a removed/absent asset refuses before a tab shows; a refresh only
  // re-mints the URL.
  const doMint = useCallback(
    async (resume: Resume | null) => {
      const isRefresh = resume !== null;

      if (!isRefresh) {
        const cached = cache.get(key);
        if (cached && !isStale(cached, now())) {
          apply({ type: 'minted', minted: cached });
          return;
        }
        const [statusRes, urlsRes] = await Promise.all([
          getAsset(projectId, assetId),
          getAssetUrls(projectId, [assetId]),
        ]);
        if (!mountedRef.current) return;
        const urlNotFound =
          (!urlsRes.ok && urlsRes.kind === 'not_found') ||
          (urlsRes.ok && urlsRes.value.errors[assetId]?.code === 'project_not_found');
        if ((!statusRes.ok && statusRes.kind === 'not_found') || urlNotFound) {
          apply({ type: 'mint_failed', reason: 'not_found' });
          return;
        }
        if (statusRes.ok && statusRes.value.status === 'removed') {
          apply({ type: 'mint_failed', reason: 'removed' });
          return;
        }
        if (!urlsRes.ok) {
          apply({ type: 'mint_failed', reason: 'load_failed' });
          return;
        }
        const url = urlsRes.value.urls[assetId]?.source?.url;
        if (!url) {
          apply({ type: 'mint_failed', reason: 'load_failed' });
          return;
        }
        const minted: MintedUrl = { url, obtainedAt: now() };
        cache.set(key, minted);
        apply({ type: 'minted', minted });
        return;
      }

      // Refresh: the status is already known; only re-sign the URL.
      const urlsRes = await getAssetUrls(projectId, [assetId]);
      if (!mountedRef.current) return;
      const urlNotFound =
        (!urlsRes.ok && urlsRes.kind === 'not_found') ||
        (urlsRes.ok && urlsRes.value.errors[assetId]?.code === 'project_not_found');
      if (urlNotFound) {
        apply({ type: 'mint_failed', reason: 'not_found' });
        return;
      }
      if (!urlsRes.ok) {
        apply({ type: 'mint_failed', reason: 'load_failed' });
        return;
      }
      const url = urlsRes.value.urls[assetId]?.source?.url;
      if (!url) {
        apply({ type: 'mint_failed', reason: 'load_failed' });
        return;
      }
      const minted: MintedUrl = { url, obtainedAt: now() };
      cache.set(key, minted);
      apply({ type: 'minted', minted });
    },
    [apply, assetId, cache, getAsset, getAssetUrls, key, now, projectId],
  );
  useEffect(() => {
    doMintRef.current = (resume) => void doMint(resume);
  }, [doMint]);

  // Mount / ref change: start the one initial mint.
  useEffect(() => {
    mountedRef.current = true;
    stateRef.current = { phase: 'minting', resume: null };
    setDisplayUrl(null);
    void doMint(null);
    return () => {
      mountedRef.current = false;
    };
    // Re-run only when the asset the tab points at changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, assetId]);

  // Media element wiring (video/audio): route element events into the machine and apply the resume
  // after a refresh.
  const cleanupElRef = useRef<(() => void) | null>(null);
  const onMediaElement = useCallback(
    (el: HTMLMediaElement | null) => {
      if (cleanupElRef.current) {
        cleanupElRef.current();
        cleanupElRef.current = null;
      }
      if (!el) return;
      const onPlay = () => {
        lastResumeRef.current = { time: el.currentTime, playing: true };
        apply({ type: 'load_start', now: now(), resume: lastResumeRef.current });
      };
      const onPause = () => {
        lastResumeRef.current = { time: el.currentTime, playing: false };
      };
      const onTimeUpdate = () => {
        lastResumeRef.current = { time: el.currentTime, playing: !el.paused };
      };
      const onSeeking = () => {
        lastResumeRef.current = { time: el.currentTime, playing: !el.paused };
        apply({ type: 'load_start', now: now(), resume: lastResumeRef.current });
      };
      const onError = () => {
        apply({ type: 'load_error', resume: lastResumeRef.current });
      };
      const onLoadedMeta = () => {
        // Before clearing the carried resume (load_ok), restore the position/play state captured
        // before the swap. A null `resume` (first load) is left to NleTimeline's `initialTime` seek.
        const st = stateRef.current;
        if (st.phase === 'ready' && st.resume) {
          try {
            el.currentTime = st.resume.time;
          } catch {
            /* not seekable yet */
          }
          if (st.resume.playing) void el.play?.()?.catch?.(() => {});
        }
        apply({ type: 'load_ok' });
      };
      el.addEventListener('play', onPlay);
      el.addEventListener('pause', onPause);
      el.addEventListener('timeupdate', onTimeUpdate);
      el.addEventListener('seeking', onSeeking);
      el.addEventListener('error', onError);
      el.addEventListener('loadedmetadata', onLoadedMeta);
      cleanupElRef.current = () => {
        el.removeEventListener('play', onPlay);
        el.removeEventListener('pause', onPause);
        el.removeEventListener('timeupdate', onTimeUpdate);
        el.removeEventListener('seeking', onSeeking);
        el.removeEventListener('error', onError);
        el.removeEventListener('loadedmetadata', onLoadedMeta);
      };
    },
    [apply, now],
  );

  // The inline single-track manifest for a video/audio asset. `durationIsEstimate` lets the element's
  // measured length drive the TimelineEditor (the manifest carries 0). Rebuilt when the URL swaps.
  const manifest = useMemo<RenderManifest | null>(() => {
    if (!displayUrl || kind === 'image') return null;
    return {
      template: 'nle-timeline',
      id: `asset-${assetId}`,
      // The file it plays: the timeline snaps a dragged range onto this file's keyframes (useKeyframes).
      sourceAssetId: assetId,
      title: displayName,
      duration: 0,
      durationIsEstimate: true,
      tracks: [
        {
          id: 'src',
          type: kind,
          src: displayUrl,
          label: displayName,
          start: 0,
          end: 0,
        },
      ],
      clips: [],
      createdAt: new Date(0).toISOString(),
    } as unknown as RenderManifest;
  }, [displayUrl, kind, assetId, displayName]);

  const phase = stateRef.current.phase;

  // An HTML deliverable's embedded image links expire; re-link them at view time (sandboxed srcdoc below).
  const htmlDoc = useRefreshedHtml(kind === 'document' ? displayUrl : null, displayName, projectId);

  if (phase === 'failed') {
    const reason = (stateRef.current as { reason: 'not_found' | 'removed' | 'load_failed' }).reason;
    return (
      <div
        data-testid="asset-player-error"
        role="alert"
        style={{
          padding: 24,
          background: t.colors.surface,
          border: `1px solid ${t.colors.border}`,
          borderRadius: t.radius.panel,
          textAlign: 'center',
          color: t.colors.textSecondary,
          fontSize: 13,
        }}
      >
        {failureMessage(reason, displayName)}
      </div>
    );
  }

  if (!displayUrl) {
    return (
      <div
        data-testid="asset-player-loading"
        style={{ padding: 24, color: t.colors.textSecondary, fontSize: 12, textAlign: 'center' }}
      >
        Loading {displayName}…
      </div>
    );
  }

  if (kind === 'document') {
    // A PDF page hit: the browser's own PDF viewer, embedded, opened at the page the span encodes. The URL is
    // the asset's short-lived presigned GET (re-minted by this player like every kind); "Open in new tab" is
    // there for a reader who wants the whole window.
    const page = pageOfSpan(startMs);
    const pageUrl = pdfUrlWithPage(displayUrl, startMs);
    return (
      <div data-testid="asset-player-document"
        style={{ display: 'flex', flexDirection: 'column', background: t.colors.surface, border: `1px solid ${t.colors.border}`, borderRadius: t.radius.panel, overflow: 'hidden' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 10px', borderBottom: `1px solid ${t.colors.border}`, fontSize: t.typography.sizes.meta, color: t.colors.textSecondary }}>
          <span data-testid="asset-player-document-page">Page {page}</span>
          <span style={{ flex: 1 }} />
          <a href={pageUrl} target="_blank" rel="noopener noreferrer" data-testid="asset-player-document-newtab"
            style={{ color: t.colors.primary, textDecoration: 'none', fontWeight: t.typography.weights.semibold }}>
            Open in new tab ↗
          </a>
        </div>
        {/* A new element per mode (`key`): `sandbox` must be set when the frame is created, or the srcdoc page
            loads with this app's origin before the flags apply. */}
        <iframe
          key={htmlDoc !== null ? 'sandboxed' : 'plain'}
          title={`${displayName}, page ${page}`}
          {...(htmlDoc !== null ? { srcDoc: htmlDoc, sandbox: 'allow-scripts allow-popups' } : { src: pageUrl })}
          onLoad={() => apply({ type: 'load_ok' })}
          style={{ width: '100%', height: '75vh', border: 'none', display: 'block', background: '#fff' }}
        />
      </div>
    );
  }

  if (kind === 'image') {
    return (
      <img
        data-testid="asset-player-image"
        src={displayUrl}
        alt={displayName}
        onLoad={() => apply({ type: 'load_ok' })}
        onError={() => apply({ type: 'load_error', resume: lastResumeRef.current })}
        // Cap a single image at 62vh like the other previews (GenericPreview, ImageAnnotated); a
        // tall 9:16 still would otherwise fill the whole tab at full width. `contain` letterboxes
        // within the cap rather than cropping or stretching.
        style={{ width: '100%', height: 'auto', maxHeight: '62vh', objectFit: 'contain', display: 'block', background: '#000', borderRadius: t.radius.panel }}
      />
    );
  }

  return (
    <div data-testid="asset-player-timeline">
      <NleTimeline
        data={{
          manifestUrl: '',
          manifest: manifest ?? undefined,
          initialTime: startMs / 1000,
          onMediaElement,
          viewState: data.viewState,
          onViewStateChange: data.onViewStateChange,
          // The minted URL is a presigned GET of the asset's source bucket. The m2c proxy bucket (an
          // m2c-source deliverable, e.g. a per-speaker diarization .wav) has NO CORS config, so a
          // crossOrigin="anonymous" media element refuses to load it — 0:00/0:00 or "could not be
          // loaded". AssetPlayer's inline manifest carries no peaks, so nothing needs CORS here.
          mediaCrossOrigin: false,
        }}
        title={displayName}
        theme={t}
        // Clip / Frame / Analyze / Crop start an agent turn through this callback; without it every
        // timeline button is a silent no-op.
        onInteraction={onInteraction}
      />
    </div>
  );
}
