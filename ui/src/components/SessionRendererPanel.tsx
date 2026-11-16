import React from 'react';
import { getAssetUrls } from '../projects/api';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { theme, tint } from '../styles/tokens';
import { authFetch } from '../auth/authFetch';
import {
  invalidateArtifactUrl,
  keyForUrl,
  peekArtifactUrl,
  preserveDocumentPreview,
  resolveArtifactUrls,
  unresolveArtifactUrls,
  type ResolvedArtifactUrl,
} from '../artifacts/artifactUrls';
import { useArtifactMediaResign, useResolvedArtifactData, useResolvedArtifactUrl } from '../artifacts/useArtifactMedia';
import { lookupRenderer } from './renderer/RendererRegistry';
import SandboxFrame from './renderer/SandboxFrame';
import RenderingAnimation from './renderer/RenderingAnimation';
import { frameSandboxFor, isMalformedReference } from './renderer/templates/artifactSrc';
import type { RenderBlock, RenderHistoryEntry, InteractionEvent, RendererProps } from './renderer/types';
import { renderTabKey } from './renderer/renderTabKey';
import { groupBySubject, viewSubjectIds } from '../shared/renderSubject';
import { useMoveToFolder, type MoveTarget } from './chat/MoveToFolderButton';
import { FileLocation } from './projects/FileLocation';
import { ChatAssetContext } from './chat/InlineAssetImage';
import { AssetIcon } from './chat/AssetIcons';
import { useSessionProject } from '../projects/sessionProjectStore';
import { RenderFailure, RenderFailureContext, TabRenderBoundary } from './renderer/renderFailure';

interface SessionRendererPanelProps {
  history: RenderHistoryEntry[];
  currentIndex: number;
  onNavigate: (index: number) => void;
  onClose: () => void;
  onInteraction?: (event: InteractionEvent) => void;
  /** True while the Render Agent is composing a view (shows the animation). */
  pending?: boolean;
  /**
   * Open a tab whose manifest has not been composed yet. Tabs for a finished
   * DAG are created immediately from job metadata and rendered on first click,
   * so we never spend Render Agent time on results nobody opened.
   */
  onRenderTab?: (tabKey: string) => void;
  /**
   * Remove a render tab from history (each generation adds one; let users prune). Both callbacks
   * name the tab by its stable key (`renderTabKey`), never its index: the list can reorder between
   * the click and the handler (R8.3).
   */
  onRemoveTab?: (tabKey: string) => void;
  /** Active session — used to show an async progress/failed strip for its jobs. */
  activeSessionId?: string;
  /**
   * When true the active session is frozen (demo lock): render-tab MUTATIONS and
   * WORK-triggering controls are disabled — remove-tab, refresh (re-invokes), and
   * clicking an un-rendered tab (which dispatches a render). Navigating to an
   * already-rendered tab and viewing/copying stay enabled so a frozen session can
   * still be shown. The server also 423s render-history mutations for a locked
   * session, so this is the UX mirror of that gate.
   */
  locked?: boolean;
}

export default function SessionRendererPanel({ history, currentIndex, onNavigate, onInteraction, pending, onRenderTab, onRemoveTab, activeSessionId, locked = false }: SessionRendererPanelProps) {
  // The session's project: a tab that shows ONE of its files can file that file in a folder (Save to folder).
  const sessionProjectId = useSessionProject(activeSessionId)?.activeProjectId ?? null;
  // "If it can't render, it shouldn't show" (renderer/renderFailure.tsx): a tab whose renderer threw or whose
  // manifest failed for good is dropped from the strip, and the panel moves to a neighbour. Remembered per tab
  // CONTENT (key + what it points at) for the browser session, so a re-rendered tab gets a fresh chance.
  const [failed, setFailed] = useState<Record<string, string>>(loadFailedTabs);
  const visibleIdx = useMemo(
    () => history.map((e, i) => (failed[tabSignature(e)] ? -1 : i)).filter((i) => i >= 0),
    [history, failed],
  );
  const currentFailed = !!history[currentIndex] && !!failed[tabSignature(history[currentIndex])];
  // Show the neighbour at once (no blank frame while the parent catches up), and move the selection to it.
  const neighbour = visibleIdx.find((i) => i > currentIndex) ?? visibleIdx[visibleIdx.length - 1];
  const shownIndex = currentFailed ? neighbour ?? -1 : currentIndex;
  useEffect(() => {
    if (currentFailed && neighbour !== undefined) onNavigate(neighbour);
  }, [currentFailed, neighbour, onNavigate]);
  const current = shownIndex >= 0 ? history[shownIndex] : undefined;
  const currentSig = current ? tabSignature(current) : '';
  // ONE TAB PER SUBJECT (design A, new-ui-design/right-panel-tabs.html): every view of the same file — its player,
  // its timeline, a re-run gallery — is a VERSION of one tab, oldest first. The views themselves stay a flat list,
  // so every producer, focus and persistence rule keeps working on it unchanged; only the strip groups them.
  const tabs = useMemo(
    () => groupBySubject(visibleIdx, (i) => entrySubjectIds(history[i])).map((g) => g.map((n) => visibleIdx[n])),
    [visibleIdx, history],
  );
  const currentTab = tabs.findIndex((t) => t.includes(shownIndex));
  const versions = currentTab >= 0 ? tabs[currentTab] : [];
  const versionPos = versions.indexOf(shownIndex);
  // The version each tab last showed, so going back to a tab returns to it (default: its newest).
  const lastShown = useRef(new Map<string, string>());
  if (current && versions.length > 1) lastShown.current.set(renderTabKey(history[versions[0]]), renderTabKey(current));
  const open = (i: number) => {
    onNavigate(i);
    const e = history[i];
    // Navigation to a rendered view is always allowed; dispatching a render for an un-rendered one is WORK —
    // blocked on a locked (frozen) session.
    if (e && e.blocks.length === 0 && e.status !== 'composing' && !locked) onRenderTab?.(renderTabKey(e));
  };
  const reportFailure = useCallback((reason: string) => {
    if (!currentSig) return;
    // eslint-disable-next-line no-console
    console.warn('[renderer] hiding a tab that cannot render:', currentSig, '—', reason);
    setFailed((m) => {
      if (m[currentSig]) return m;
      const next = { ...m, [currentSig]: reason };
      saveFailedTabs(next);
      return next;
    });
  }, [currentSig]);
  // Resolve the CURRENT tab's source URL (sync from artifactUrls/inline tracks, or by
  // fetching its manifest-router manifest) so the footer copy button works for every
  // tab — including agent-rendered clip/analysis tabs whose block is only a manifest
  // pointer. Pre-resolved on tab change so the copy stays inside the click gesture.
  // `expiresIn` is set when a presigned URL (not the identifier) went to the clipboard.
  const [copied, setCopied] = useState<false | { expiresIn?: string }>(false);
  // Per-view reload counter for the ⟳ refresh control. Bumping it re-keys the
  // rendered block subtree so it REMOUNTS: ManifestRouter re-fetches its manifest
  // via authFetch (which refreshes an expired session token) and the embed
  // (iframe/media) reloads with a fresh src — the recovery path for a stale render
  // (expired session, transient load failure, or an in-place artifact mutation).
  const [reloadTick, setReloadTick] = useState(0);
  // Copy-source FLICKER fix: derive the source URL synchronously (server composites + artifactUrls
  // resolve without a fetch), so the button stays put when the reconcile re-lists renders as NEW
  // object refs each poll. The old effect set currentSrc=undefined then re-resolved on every `current`
  // reference change, so the button unmounted→remounted repeatedly — the reported flicker. Only fall
  // back to an async fetch when the sync path can't find a URL, and never flip to undefined for a mere
  // reference change (asyncSrc keeps its last value until the next resolve).
  const syncSrc = current ? tabSourceUrl(current) : undefined;
  const [asyncSrc, setAsyncSrc] = useState<string | undefined>(undefined);
  useEffect(() => {
    setCopied(false);
    if (!current || syncSrc) { setAsyncSrc(undefined); return; }
    let alive = true;
    void resolveSourceUrl(current).then((u) => { if (alive) setAsyncSrc(u); });
    return () => { alive = false; };
  }, [current, syncSrc]);
  const currentSrc = syncSrc ?? asyncSrc;
  // Gallery-aware copy: ImageAnnotated emits a 'select' event carrying the VISIBLE image's url when
  // the user clicks a thumbnail. Prefer that over the tab's default source (tabSourceUrl =
  // artifactUrls[0], the FIRST image) so "Copy source URL" follows the selection. Reset on tab
  // change (keyed to currentIndex, NOT `current` — which gets a new object ref each poll) so a
  // gallery selection never leaks into a different (e.g. video) tab's copy. Only absolute http(s)
  // urls are captured, so a relative/proxy src falls back to the resolved tab source.
  const [activeGallerySrc, setActiveGallerySrc] = useState<string | undefined>(undefined);
  useEffect(() => { setActiveGallerySrc(undefined); }, [shownIndex]);
  // ONE re-sign listener for every media element any block in this panel renders (see
  // useArtifactMediaResign). The content div is the host; the hook re-attaches if it remounts.
  const contentRef = useRef<HTMLDivElement>(null);
  useArtifactMediaResign(contentRef);
  const handleInteraction = (raw: InteractionEvent) => {
    // Templates were handed presigned URLs; map them back to stable identifiers so an expiring URL
    // never reaches the copy button, chat history, a new tab, or an agent prompt.
    let e = unresolveArtifactUrls(raw);
    // An action names the tab it came from, so what it makes can come back as that tab's next version.
    if (e.type === 'action' && current) {
      e = { ...e, data: { ...(e.data as object), origin: { subjectIds: entrySubjectIds(current), label: prettyTabLabel(current.label || current.blocks[0]?.title, shownIndex) } } };
    }
    if (e.type === 'select') {
      const url = (e.data as { url?: unknown } | undefined)?.url;
      if (typeof url === 'string' && /^https?:\/\//.test(url)) setActiveGallerySrc(url);
    }
    onInteraction?.(e);  // always forward — annotation clicks, timeline actions, etc.
  };
  const copyUrl = activeGallerySrc ?? currentSrc;
  // Save to folder: the tab's ONE file, by the same source URL Copy uses (an asset-player tab's is a presigned URL
  // of the asset's own key). Resolve-locations maps it to the asset registered there, or to nothing, and then the
  // button does not show. A Moments tab saves its picks as bookmarks itself; a tab-level Save there would move a
  // whole source file.
  const isMomentsTab = !!current?.blocks.some((b) => b.template === 'clip-gallery'
    && Array.isArray((b.data as { galleryMoments?: unknown })?.galleryMoments));
  const rendererAssetContext = useMemo(() => ({ sessionId: activeSessionId ?? '', projectId: sessionProjectId }), [activeSessionId, sessionProjectId]);
  const saveTarget = sessionProjectId && !isMomentsTab && copyUrl ? { projectId: sessionProjectId, location: copyUrl } : null;
  // The copy button's url is a stable IDENTIFIER. With the artifacts CDN private it 403s when
  // opened, so for one of OUR artifacts the clipboard gets a presigned URL instead. `keyForUrl`
  // (not `artifactKeyOf`) is used so a project-asset source (`projects/<pid>/<kind>:<uuid>/…`) and a
  // raw presigned own-bucket URL that a render manifest baked both map to their key and re-sign
  // through the resolver — otherwise Copy wrote the expiring/expired URL verbatim. Pre-resolve it
  // whenever the copied identifier changes, so the click can write it synchronously: some browsers
  // reject a clipboard write that happens after an await (the user activation has lapsed).
  const copyKey = copyUrl ? keyForUrl(copyUrl) : null;
  useEffect(() => {
    if (copyKey) void presignedForCopy(copyKey);
  }, [copyKey]);
  const copySource = () => {
    if (!copyUrl) return;
    const write = (text: string, expiresAt?: number) => {
      void navigator.clipboard?.writeText(absoluteCopyUrl(text))
        .then(() => {
          setCopied({ expiresIn: expiresAt === undefined ? undefined : expiryNote(expiresAt) });
          setTimeout(() => setCopied(false), expiresAt === undefined ? 1400 : 2600);
        })
        .catch(() => { /* clipboard blocked — title still shows the url */ });
    };
    if (!copyKey) { write(copyUrl); return; }
    const warm = peekCopyUrl(copyKey);
    if (warm) { write(warm.url, warm.expiresAt); return; }
    // Not resolved yet (or nearly expired): resolve, then copy. Falls back to the identifier.
    void presignedForCopy(copyKey).then((r) => (r ? write(r.url, r.expiresAt) : write(copyUrl)));
  };

  // In-progress jobs are now indicated by a pulsing dot on the Jobs tab (no in-panel strip) — the
  // old "N analysis jobs in progress" ribbon pushed the whole timeline down each time it appeared /
  // disappeared (layout shift), which read worse than the work it described.

  // The render animation is now PER-TAB (an entry with status 'composing' shows
  // it in its own content area below), so a new render never blanks the whole
  // panel or hides a result you're viewing. The old window-level showAnim latch
  // is gone — a composing tab exists from dispatch → manifest, so there is no
  // flash/no-show window to paper over.

  if (!current) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', height: '100%', alignItems: 'center', justifyContent: 'center', color: theme.colors.textSecondary, fontSize: theme.typography.sizes.body }}>
        No rendered content yet
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {/* Tab strip — one tab per result. A finished DAG surfaces every node's
          output at once so the user can click through them. */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 10px 0 10px', borderBottom: `1px solid ${theme.colors.border}`, flexShrink: 0 }}>
        <style>{TAB_MOTION_CSS}</style>
        <div style={{ display: 'flex', gap: 4, overflowX: 'auto', flex: 1 }} role="tablist">
          {tabs.map((members, t) => {
            const first = history[members[0]];
            const isActive = t === currentTab;
            const picked = lastShown.current.get(renderTabKey(first));
            const target = isActive ? shownIndex
              : members.find((i) => renderTabKey(history[i]) === picked) ?? members[members.length - 1];
            const entry = history[target];
            const label = prettyTabLabel(first.label || first.blocks[0]?.title, members[0]);
            const composing = entry.status === 'composing';
            const unrendered = entry.blocks.length === 0;
            return (
              <div
                key={`${renderTabKey(first)}-${members[0]}`}
                className="mh-render-tab"
                style={{
                  display: 'inline-flex', alignItems: 'center', flexShrink: 0,
                  borderRadius: theme.radius.button,
                  background: isActive ? theme.colors.activeBg : 'transparent',
                }}
              >
                <button
                  role="tab"
                  aria-selected={isActive}
                  aria-label={label}
                  title={composing ? `${label} — composing…` : unrendered ? (locked ? `${label} — session locked` : `${label} — click to render`) : members.length > 1 ? `${label} — ${members.length} versions` : label}
                  onClick={() => open(target)}
                  style={{
                    padding: '5px 6px 5px 12px',
                    border: 'none',
                    background: 'transparent',
                    color: isActive ? theme.colors.primary : theme.colors.textSecondary,
                    fontSize: theme.typography.sizes.meta,
                    fontWeight: isActive ? theme.typography.weights.semibold : theme.typography.weights.regular,
                    cursor: 'pointer',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {shortTabLabel(label)}
                  {members.length > 1 && <span data-testid="tab-version-count" aria-hidden="true" style={versionCountStyle}>{members.length}</span>}
                  {composing
                    ? <span aria-label="composing" title="composing" style={{ marginLeft: 6, color: theme.colors.primary, fontSize: 'var(--fs-3xs)' }}>●</span>
                    : unrendered && <span aria-hidden="true" style={{ marginLeft: 5, opacity: 0.5 }}>○</span>}
                </button>
                {onRemoveTab && (
                  <button
                    // Closing a tab closes the subject: every version of it.
                    onClick={(e) => { e.stopPropagation(); if (!locked) for (const i of members) onRemoveTab(renderTabKey(history[i])); }}
                    disabled={locked}
                    title={locked ? 'Session locked' : members.length > 1 ? `Remove this tab and its ${members.length} versions` : 'Remove this tab'}
                    aria-label={`Remove ${label} tab`}
                    style={{
                      ...tabCloseStyle,
                      ...(locked ? { opacity: 0.35, cursor: 'not-allowed' } : null),
                      color: isActive ? theme.colors.primary : theme.colors.textSecondary,
                    }}
                  >
                    ×
                  </button>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {/* Content */}
      <div ref={contentRef} data-testid="renderer-content" style={{ flex: 1, minHeight: 0, overflowY: 'auto' }}>
        {/* Inner wrapper grows to natural content height so a tall timeline SCROLLS. The outer is a
            plain block (not a flex column that would shrink its children and let NleTimeline's
            overflow:hidden clip the lanes below); minHeight:100% still lets the composing animation
            fill the panel when content is short. */}
        <div style={{ padding: 14, display: 'flex', flexDirection: 'column', gap: 14, minHeight: '100%', boxSizing: 'border-box' }}>
        {(versions.length > 1 || current.blocks.length > 0) && (
        <div data-testid="tab-toolbar" style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0, flexWrap: 'wrap', marginBottom: -4 }}>
        {versions.length > 1 && (
          <div data-testid="version-switcher" style={{ display: 'flex', alignItems: 'center', gap: 6, minWidth: 0, fontSize: theme.typography.sizes.meta, color: theme.colors.textSecondary }}>
            <button type="button" aria-label="Previous version" disabled={versionPos <= 0} onClick={() => open(versions[versionPos - 1])} style={versionStepStyle}>‹</button>
            <select aria-label="Version" value={shownIndex} onChange={(e) => open(Number(e.target.value))} style={versionSelectStyle}>
              {versions.map((i, n) => (
                <option key={renderTabKey(history[i])} value={i}>{`v${n + 1} · ${versionLabel(history[i], i)}`}</option>
              ))}
            </select>
            <span>of {versions.length}</span>
            <button type="button" aria-label="Next version" disabled={versionPos >= versions.length - 1} onClick={() => open(versions[versionPos + 1])} style={versionStepStyle}>›</button>
          </div>
        )}
        {current.blocks.length > 0 && (
          <div style={{ display: 'flex', justifyContent: 'flex-end', alignItems: 'center', gap: 8, marginLeft: 'auto' }}>
            <button
              data-testid="refresh-render"
              onClick={() => { if (!locked) setReloadTick((t) => t + 1); }}
              disabled={locked}
              title={locked ? 'Session locked' : 'Refresh — reload this view with a fresh session token (fixes an expired/stale render)'}
              aria-label="Refresh this render"
              style={locked ? { ...refreshBtnStyle, opacity: 0.35, cursor: 'not-allowed' } : refreshBtnStyle}
            >
              ⟳
            </button>
            {saveTarget && (
              <TabFileLocation key={saveTarget.location} target={saveTarget} name={current?.blocks[0]?.title || 'this file'} />
            )}
            {copyUrl && (
              <button
                data-testid="copy-source-url"
                onClick={copySource}
                title={copied ? 'Copied source URL' : `Copy this tab's source URL — paste into chat to run more on this exact clip\n${copyUrl}`}
                style={{
                  display: 'inline-flex', alignItems: 'center', gap: 6,
                  padding: '4px 10px', fontSize: theme.typography.sizes.meta,
                  borderRadius: theme.radius.button,
                  border: `1px solid ${copied ? theme.colors.primary : theme.colors.borderStrong}`,
                  background: copied ? theme.colors.accentSubtle : theme.colors.surface,
                  color: theme.colors.primary, cursor: 'pointer',
                  fontWeight: theme.typography.weights.semibold, whiteSpace: 'nowrap',
                }}
              >
                {copied ? '✓ Copied source URL' : '⧉ Copy source URL'}
                {copied && copied.expiresIn && (
                  <span data-testid="copy-source-expiry" style={{ fontWeight: theme.typography.weights.regular, color: theme.colors.textSecondary }}>
                    · link expires in {copied.expiresIn}
                  </span>
                )}
              </button>
            )}
          </div>
        )}
        </div>
        )}
        {current.blocks.length === 0 ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10, flex: 1, minHeight: 0 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <div style={{ width: 7, height: 7, borderRadius: '50%', background: theme.colors.primary }} />
              <span style={{ fontWeight: theme.typography.weights.semibold, color: theme.colors.textPrimary, fontSize: theme.typography.sizes.body }}>
                {current.status === 'composing' ? 'Composing this result…' : 'Rendering…'}
              </span>
            </div>
            <RenderingAnimation />
          </div>
        ) : (
          // The chat's project reaches the templates (an image gallery says where its images are filed).
          <ChatAssetContext.Provider value={rendererAssetContext}>
          <RenderFailureContext.Provider value={reportFailure}>
            <TabRenderBoundary key={`${currentSig}-${reloadTick}`} onError={(e) => reportFailure(`threw: ${e.message}`)}>
              {current.blocks.map((block, idx) => (
                // Keyed by the tab's identity, not its position: when the list reorders under the viewed
                // index, a different tab must remount rather than inherit this one's timeline state.
                <RenderBlockView key={`${renderTabKey(current)}-${reloadTick}-${idx}`} block={block} onInteraction={handleInteraction} />
              ))}
            </TabRenderBoundary>
          </RenderFailureContext.Provider>
          </ChatAssetContext.Provider>
        )}
        </div>
      </div>

      {/* Footer */}
      <div style={{ padding: '8px 14px', borderTop: `1px solid ${theme.colors.border}`, display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexShrink: 0, gap: 10 }}>
        <span style={{ color: theme.colors.textSecondary, fontSize: theme.typography.sizes.meta }}>
          Tab · {currentTab + 1} of {tabs.length}
        </span>
      </div>
    </div>
  );
}

/**
 * A copied presigned URL should still work for a while after it lands in the clipboard, so a cached
 * one with less than this left is replaced rather than copied (the resolver's own cache keeps URLs
 * until 60 s before expiry, which is fine for an <img> that re-signs on error but not for a link a
 * user pastes somewhere).
 */
const COPY_MIN_REMAINING_S = 15 * 60;

/** A cached presigned URL for `key` with at least COPY_MIN_REMAINING_S left, else null. Sync. */
function peekCopyUrl(key: string): ResolvedArtifactUrl | null {
  const hit = peekArtifactUrl(key);
  return hit && hit.expiresAt - Date.now() / 1000 >= COPY_MIN_REMAINING_S ? hit : null;
}

/** Resolve `key` for the copy button (fresh when the cached URL is nearly spent). Never rejects. */
async function presignedForCopy(key: string): Promise<ResolvedArtifactUrl | null> {
  const warm = peekCopyUrl(key);
  if (warm) return warm;
  if (peekArtifactUrl(key)) invalidateArtifactUrl(key);
  return (await resolveArtifactUrls([key])).get(key) ?? null;
}

/**
 * The clipboard form of a resolved URL: absolute. An HTML/SVG artifact resolves to a same-origin view-ticket
 * URL (`/api/artifact?key=…&view=…`, so the proxy re-signs the links inside the page) -- RELATIVE, which pasted
 * anywhere is not a link at all (prod session d52d544e: "Copy source URL" of the picture-book page was broken).
 */
export function absoluteCopyUrl(text: string, origin: string = window.location.origin): string {
  if (!text.startsWith('/') || text.startsWith('//')) return text;
  try {
    return new URL(text, origin).href;
  } catch {
    return text;
  }
}

/** "1 hour" for a fresh URL (the route mints 3600 s), else whole minutes left. */
export function expiryNote(expiresAt: number, nowMs: number = Date.now()): string {
  const minutes = Math.floor((expiresAt - nowMs / 1000) / 60);
  return minutes >= 55 ? '1 hour' : `${Math.max(1, minutes)} min`;
}

/** The pre-resolution `/api/artifact` form of a custom block's `data.src` (also the fallback). */
export function legacyCustomFrameSrc(raw: string): string {
  const key = raw.includes('cloudfront.net/')
    ? raw.replace(/https:\/\/[\w\-]+\.cloudfront\.net\//, '')
    : raw.includes('/api/artifact') ? '' : raw;
  return key ? `/api/artifact?key=${encodeURIComponent(key)}` : raw;
}

/**
 * A `custom` block with `data.src` (PDF/HTML document). An artifact identifier is resolved to a
 * presigned URL so the frame does not depend on a public `/api/artifact`; anything else (or a
 * failed resolution) keeps the legacy proxy form.
 */
function CustomSrcFrame({ raw, title }: { raw: string; title: string }) {
  const { src: resolved, key, pending } = useResolvedArtifactUrl(raw);
  // Same guard as DocumentViewer: two links in one field (session 865950fd) would otherwise become
  // `/api/artifact?key=<garbage>` and frame the proxy's raw JSON 400 body as the document.
  if (isMalformedReference(raw.trim())) {
    return (
      <div role="status" data-testid="custom-src-unavailable" style={{ padding: 24, textAlign: 'center', color: theme.colors.textSecondary, background: theme.colors.surfaceSubtle, border: `1px dashed ${theme.colors.borderStrong}`, borderRadius: theme.radius.panel }}>
        <div style={{ fontWeight: theme.typography.weights.semibold, color: theme.colors.textPrimary }}>Preview unavailable</div>
        <div style={{ marginTop: 6, fontSize: theme.typography.sizes.body }}>This document’s link isn’t a valid artifact URL, so it can’t be displayed here.</div>
      </div>
    );
  }
  const src = key ? (resolved && resolved !== raw ? resolved : legacyCustomFrameSrc(raw)) : legacyCustomFrameSrc(raw);
  // Sandboxed (never same-origin) unless it is a PDF, which Chromium will not render sandboxed.
  const sandbox = frameSandboxFor(src);
  return (
    <iframe
      key={sandbox ? 'sandboxed' : 'pdf'}
      sandbox={sandbox}
      src={pending ? undefined : src}
      title={title}
      data-testid="custom-src-frame"
      style={{ width: '100%', height: 500, border: `1px solid ${theme.colors.border}`, borderRadius: theme.radius.panel, background: theme.colors.surface }}
    />
  );
}

/**
 * A RendererRegistry template, handed `block.data` with every artifact identifier resolved to a
 * presigned URL (`document.previewUrl` kept for DocumentViewer). Data without identifiers passes
 * through untouched and synchronously.
 */
function ResolvedRegistryBlock({ Renderer, block, onInteraction }: {
  Renderer: React.ComponentType<RendererProps<any>>;
  block: RenderBlock;
  onInteraction?: (e: InteractionEvent) => void;
}) {
  const data = useResolvedArtifactData(block.data, RESOLVE_BLOCK_OPTS);
  if (!data) {
    return (
      <div data-testid="render-block-resolving" style={{ padding: 24, color: theme.colors.textSecondary, fontSize: 12, textAlign: 'center' }}>
        Loading render…
      </div>
    );
  }
  return <Renderer data={data} title={block.title} theme={theme} onInteraction={onInteraction} />;
}

const RESOLVE_BLOCK_OPTS = { skip: preserveDocumentPreview };

function RenderBlockView({ block, onInteraction }: { block: RenderBlock; onInteraction?: (e: InteractionEvent) => void }) {
  // PDF/document blocks — an iframe on the resolved artifact URL (legacy /api/artifact fallback).
  if (block.template === 'custom' && block.data?.src) {
    return <CustomSrcFrame raw={String(block.data.src)} title={block.title} />;
  }

  if (block.template === 'custom' && block.html) {
    return <SandboxFrame html={block.html} title={block.title} onInteraction={onInteraction as any} />;
  }

  const Renderer = lookupRenderer(block.template);
  if (Renderer) {
    return <ResolvedRegistryBlock Renderer={Renderer} block={block} onInteraction={onInteraction} />;
  }

  // Unknown template — try sandbox if html present, otherwise show error
  if (block.html) {
    return <SandboxFrame html={block.html} title={block.title} onInteraction={onInteraction as any} />;
  }

  return (
    <RenderFailure
      reason={`unknown template: ${block.template}`}
      fallback={(
        <div style={{ padding: 16, background: tint(theme.colors.warning), borderRadius: theme.radius.panel, border: `1px solid ${theme.colors.accent}`, fontSize: theme.typography.sizes.body, color: theme.colors.textPrimary }}>
          Unknown template: <code>{block.template}</code>
        </div>
      )}
    />
  );
}

/** A tab's identity AND what it points at, so a re-rendered tab (new manifest) is not hidden by an old failure. */
/** What a view is about: the ids the server stamped, else (a view this browser authored) its inline manifest's. */
export function entrySubjectIds(entry: RenderHistoryEntry): string[] {
  const ids = entry.subjectIds ?? viewSubjectIds((entry.blocks?.[0]?.data as { manifest?: unknown } | undefined)?.manifest);
  // Its own identity too: a view about no file (an untitled gallery) can still have a next version.
  return [...ids, `view:${renderTabKey(entry)}`];
}

/** A version's line in the switcher: what tells it apart (a search's query), else its own name; and when it was made. */
function versionLabel(entry: RenderHistoryEntry, idx: number): string {
  const name = entry.note || prettyTabLabel(entry.label || entry.blocks[0]?.title, idx);
  const at = new Date(entry.timestamp);
  return Number.isFinite(at.getTime()) ? `${name} · ${at.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}` : name;
}

const versionCountStyle: React.CSSProperties = {
  marginLeft: 6, padding: '0 5px', borderRadius: 8, fontSize: 'var(--fs-3xs)',
  background: theme.colors.activeBg, color: theme.colors.textSecondary, fontWeight: theme.typography.weights.semibold,
};
const versionStepStyle: React.CSSProperties = {
  display: 'inline-flex', alignItems: 'center', justifyContent: 'center', width: 26, height: 26, padding: 0,
  border: `1px solid ${theme.colors.borderStrong}`, background: theme.colors.surface, color: theme.colors.textSecondary,
  borderRadius: theme.radius.button, cursor: 'pointer', fontSize: 'var(--fs-md)', lineHeight: 1,
};
const versionSelectStyle: React.CSSProperties = {
  height: 26, border: `1px solid ${theme.colors.borderStrong}`, background: theme.colors.surface, color: theme.colors.textPrimary,
  borderRadius: theme.radius.button, padding: '0 6px', fontSize: theme.typography.sizes.meta, minWidth: 0, maxWidth: 280,
  textOverflow: 'ellipsis',
};

export function tabSignature(entry: RenderHistoryEntry): string {
  const targets = entry.blocks.map((b) => {
    const d = (b.data ?? {}) as { manifestUrl?: unknown; src?: unknown };
    return String(d.manifestUrl ?? d.src ?? b.template ?? '');
  });
  return `${renderTabKey(entry)}|${targets.join(',')}`;
}

const FAILED_TABS_KEY = 'lab-render-failed-tabs';
const FAILED_TABS_MAX = 300;

function loadFailedTabs(): Record<string, string> {
  try {
    const raw = sessionStorage.getItem(FAILED_TABS_KEY);
    const v = raw ? JSON.parse(raw) : {};
    return v && typeof v === 'object' ? v as Record<string, string> : {};
  } catch {
    return {};
  }
}

function saveFailedTabs(map: Record<string, string>): void {
  try {
    const entries = Object.entries(map).slice(-FAILED_TABS_MAX);
    sessionStorage.setItem(FAILED_TABS_KEY, JSON.stringify(Object.fromEntries(entries)));
  } catch { /* storage full or blocked: the in-memory set still hides it for this mount */ }
}

/**
 * A render entry's label can be a raw content hash (fal batch images arrive as
 * separate entries keyed by their image id). Never show a hash as a tab — fall
 * back to a friendly ordinal so the strip reads cleanly.
 */
/**
 * The primary source URL for a render tab — used by the per-tab copy button so a
 * user can grab a clip's URL and paste it into chat to reference THAT clip.
 * Prefers the canonical artifactUrls[0]; else digs the first media src out of the
 * composed block manifest. Exported for tests.
 */
export function tabSourceUrl(entry: RenderHistoryEntry): string | undefined {
  const u0 = entry.artifactUrls?.find(u => typeof u === 'string' && /^https?:\/\//.test(u));
  if (u0) return u0;
  const data = entry.blocks?.[0]?.data as Record<string, unknown> | undefined;
  if (!data) return undefined;
  // A server composite stores the timeline under `data.manifest` (inline), not directly on `data`;
  // fall back to it so the per-tab copy-source URL resolves for composites too (regression fix).
  const manifest = (data.manifest as Record<string, unknown> | undefined) ?? undefined;
  const trackSrc = (obj: Record<string, unknown> | undefined): unknown => {
    const tracks = obj && Array.isArray(obj.tracks) ? (obj.tracks as Array<Record<string, unknown>>) : [];
    // Prefer a video track's src (the source clip); fall back to the first track with an src.
    return tracks.find(tk => tk?.type === 'video' && typeof tk?.src === 'string')?.src
      ?? tracks.find(tk => typeof tk?.src === 'string')?.src;
  };
  const media = (data.media as Record<string, unknown> | undefined) ?? undefined;
  const cands: unknown[] = [
    data.sourceUrl, data.src, data.url,
    media?.src, media?.sourceUrl,
    trackSrc(data),
    manifest?.sourceUrl, manifest?.src,
    trackSrc(manifest),
  ];
  return cands.find((u): u is string => typeof u === 'string' && /^https?:\/\//.test(u));
}

/** The manifest-router URL for a tab whose block is only a pointer (agent renders). */
export function tabManifestUrl(entry: RenderHistoryEntry): string | undefined {
  const mu = (entry.blocks?.[0]?.data as { manifestUrl?: string } | undefined)?.manifestUrl;
  return typeof mu === 'string' && mu ? mu : undefined;
}

/**
 * Resolve a tab's primary source URL for the copy button: sync from artifactUrls /
 * inline tracks when present, else fetch the manifest-router manifest and pull
 * tracks[0].src / sourceUrl. Never throws. Exported for tests.
 */
/**
 * The Asset_Reference of an `asset-player` tab (a file opened from the project browser, a citation, a Moment's
 * Expand, or the agent's show_asset), or null. Such a tab stores the REFERENCE, never a URL (lineage R2.12), so
 * the copy button has nothing to find by URL; its source is minted from the reference instead.
 */
function assetPlayerRef(m: Record<string, unknown> | undefined): { project_id: string; asset_id: string } | null {
  if (!m || m.template !== 'asset-player') return null;
  const ref = m.ref as { project_id?: unknown; asset_id?: unknown } | undefined;
  return ref && typeof ref.project_id === 'string' && typeof ref.asset_id === 'string'
    ? { project_id: ref.project_id, asset_id: ref.asset_id } : null;
}

/** The asset's source URL from the asset-id-gated route (same access check as the player), or undefined. */
async function assetSourceUrl(ref: { project_id: string; asset_id: string }): Promise<string | undefined> {
  const r = await getAssetUrls(ref.project_id, [ref.asset_id]).catch(() => null);
  const url = r && r.ok ? r.value.urls[ref.asset_id]?.source?.url : undefined;
  return typeof url === 'string' && /^https?:\/\//.test(url) ? url : undefined;
}

export async function resolveSourceUrl(entry: RenderHistoryEntry): Promise<string | undefined> {
  const sync = tabSourceUrl(entry);
  if (sync) return sync;
  const inline = (entry.blocks?.[0]?.data as { manifest?: Record<string, unknown> } | undefined)?.manifest;
  const inlineRef = assetPlayerRef(inline);
  if (inlineRef) return assetSourceUrl(inlineRef);
  const mu = tabManifestUrl(entry);
  if (!mu) return undefined;
  try {
    // authFetch: the manifest is served by `/api/artifact`, which is authenticated when the
    // environment's artifacts are private.
    const r = await authFetch(mu);
    if (!r.ok) return undefined;
    const m = (await r.json()) as Record<string, unknown>;
    const fetchedRef = assetPlayerRef(m);
    if (fetchedRef) return assetSourceUrl(fetchedRef);
    const tracks = Array.isArray(m.tracks) ? (m.tracks as Array<Record<string, unknown>>) : [];
    const cand = (typeof m.sourceUrl === 'string' && m.sourceUrl)
      || (typeof m.src === 'string' && m.src)
      || tracks.find(t => typeof t?.src === 'string')?.src;
    return typeof cand === 'string' && /^https?:\/\//.test(cand) ? cand : undefined;
  } catch {
    return undefined;
  }
}

/** A new tab eases in (fade + small slide) instead of popping; off for reduced motion. */
const TAB_MOTION_CSS = `@keyframes mh-tab-in { from { opacity: 0; transform: translateY(3px); } to { opacity: 1; transform: none; } }
.mh-render-tab { animation: mh-tab-in 180ms ease-out; }
@media (prefers-reduced-motion: reduce) { .mh-render-tab { animation: none; } }`;

/** Longest tab label shown; a longer one is cut in the MIDDLE (its title keeps the full name). */
export const TAB_LABEL_MAX = 28;

/**
 * A tab's on-strip label: long names cut in the middle, because related outputs share a long prefix
 * (`Netflix_Open_Content_Meridian_clip_…`) and differ at the end, where the extension is too.
 */
export function shortTabLabel(label: string, max = TAB_LABEL_MAX): string {
  const chars = [...label];
  if (chars.length <= max) return label;
  const tail = Math.ceil((max - 1) * 0.55);
  return `${chars.slice(0, max - 1 - tail).join('')}…${chars.slice(chars.length - tail).join('')}`;
}

export function prettyTabLabel(raw: string | undefined, idx: number): string {
  const s = (raw ?? '').trim();
  // None of these are meaningful tab labels: an empty string, a bare hash, a
  // gateway tool id like "usr-8ddf…" (or its hyphen-title-cased form
  // "Usr 8ddf…"), or any label dominated by a long hex run. Fall back to a
  // friendly ordinal so the strip stays readable.
  const compact = s.replace(/[\s_-]/g, '');
  if (!s
      || /^[0-9a-f]{12,}$/i.test(s)
      || /^usr[\s_-]/i.test(s)
      || /[0-9a-f]{24,}/i.test(compact)) {
    return `Result ${idx + 1}`;
  }
  return s;
}

const tabCloseStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', justifyContent: 'center',
  width: 18, height: 18, marginRight: 4, padding: 0,
  border: 'none', background: 'none', borderRadius: theme.radius.badge,
  cursor: 'pointer', fontSize: 'var(--fs-md)', lineHeight: 1, opacity: 0.55,
};

// Subtle, square icon button that blends with the tab toolbar — matches the copy
// button's border/surface but stays visually quiet (it's a recovery affordance).
const refreshBtnStyle: React.CSSProperties = {
  display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
  width: 26, height: 26, padding: 0,
  borderRadius: theme.radius.button,
  border: `1px solid ${theme.colors.borderStrong}`,
  background: theme.colors.surface,
  color: theme.colors.textSecondary, cursor: 'pointer',
  fontSize: 'var(--fs-md)', lineHeight: 1,
};

/**
 * A single-file tab (a timeline, an image): where the file is filed, at the toolbar's left, and Move beside the
 * Refresh button — so you see where it lives before you move it. Nothing until the file resolves to a project asset.
 */
function TabFileLocation({ target, name }: { target: MoveTarget; name: string }) {
  const m = useMoveToFolder(target, name, true);
  if (!m.available) return null;
  return (
    <>
      {m.folder !== undefined && (
        <span style={{ order: -1, marginRight: 'auto', minWidth: 0, display: 'flex' }} data-testid="tab-file-location">
          <FileLocation variant="crumb" projectId={target.projectId} folder={m.folder} />
        </span>
      )}
      <span style={{ position: 'relative', display: 'inline-flex', alignItems: 'center', gap: 10 }}>
        <button type="button" className="cap-btn-light" onClick={() => (m.open ? m.close() : m.openPicker())}
          aria-haspopup="dialog" aria-expanded={m.open} data-testid="move-to-folder" title="Move to another folder">
          <AssetIcon kind="folder" size={15} /> Move
        </button>
        {m.ui}
      </span>
    </>
  );
}
