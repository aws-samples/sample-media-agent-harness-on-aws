/**
 * ClipGallery — the generic surface for a set of INDEPENDENT clips (audio takes or
 * video takes), each individually playable and mark-in/out editable.
 *
 * This is the non-hack answer to "multiple timelines": clips are NOT put on one shared
 * axis (they are not time-aligned). Each clip owns its own ClipEditor. The gallery then
 * lets you mark keep-ranges across several clips and act on them in ONE request —
 * "Trim all marked (N)" — with each entry carrying its own clip id + src + range, so
 * the agent knows exactly which file to trim and to what.
 *
 * Media-aware verbs: an audio gallery trims; a video gallery clips. The action reaches the
 * agent as an InteractionEvent{type:'action'} carrying a composed prompt + the per-clip list.
 *
 * ─────────────────────────────────────────────────────────────────────────────────────────
 * MOMENTS MODE (Component 7, media-harness-search-qa-rendering; Req 3.8, 3.12–3.16, 3.20, 3.21).
 * A Moments manifest carries `data.galleryMoments` (a {@link MomentsBlock}). `readMoments` validates
 * it; when present the gallery renders {@link MomentCard}s instead of legacy ClipEditor cards, with
 * the SAME header, bulk-action bar and Play all, but driven by Asset_References (no URL) — each card
 * mints a short-lived presigned URL at view time through {@link useMomentUrls}. When `readMoments`
 * returns `null` (a legacy manifest, Req 3.16) the component runs today's `readClips` path UNCHANGED.
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import type {
  RendererProps, ClipGalleryData, ClipGalleryClip, BatchClipActionData, OpenTabActionData,
  CropBox, MomentsBlock, MomentItem, MomentsQueryImage,
} from '../types';
import ClipEditor, { type ClipMark } from '../../nle/ClipEditor';
import { formatTimecode } from '../../nle/formatTimecode';
import MomentCard from './MomentCard';
import { AssetPreviewModal } from '../../chat/AssetPreviewModal';
import { downloadFile } from '../../chat/artifactUrl';
import { openAssetAt, resolveCitationSource } from '../../../projects/openCitation';
import { pageOfSpan } from '../../../shared/pageLocator';
import { createBookmarks, listFolders } from '../../../projects/api';
import { buildFolderTree, flattenFolders } from '../../../projects/folderPaths';
import { readPendingFolders } from '../../../projects/pendingFolders';
import { FolderPicker } from '../../projects/FolderPicker';
import PlayIcon from '../../icons/PlayIcon';
import PauseIcon from '../../icons/PauseIcon';
import ScissorsIcon from '../../icons/ScissorsIcon';
import CropIcon from '../../icons/CropIcon';
import ImageIcon from '../../icons/ImageIcon';
import {
  momentsBatchEntries, type MomentBatchCard,
} from './momentText';
import { useMomentUrls } from '../../../projects/useMomentUrls';
import { useMomentFrames } from '../../../projects/useMomentFrames';
import { getActiveSessionId } from '../../../services/activeSession';
import { requestMomentClipAutoOpen } from '../../../services/momentClipAutoOpen';
import { getSessionProject } from '../../../projects/sessionProjectStore';
import SearchIcon from '../../icons/SearchIcon';
import { SEARCH_KINDS, momentsVersionManifest, useMomentsTabSearch, type MomentsTabSearch } from './momentsTabSearch';
import { ImageQueryValue, MatchedBadges, SearchByImageButton } from './MomentsQueryBar';
import { requestOpenView } from '../openView';
import DocumentMomentCard from './DocumentMomentCard';
import { isSearchKind, type PlayableKind } from '../../../shared/searchKinds';

const PALETTE = ['#7dd3fc', '#c4b5fd', '#f9a8d4', '#86efac', '#fde68a', '#fca5a5'];

function readClips(data: ClipGalleryData): ClipGalleryClip[] {
  const raw = data.galleryClips ?? data.clips ?? [];
  return raw.filter((c): c is ClipGalleryClip =>
    !!c && typeof c.id === 'string' && typeof c.src === 'string' &&
    /^https:\/\//.test(c.src) && (c.kind === 'audio' || c.kind === 'video'));
}

/**
 * Validate a Moments manifest's `galleryMoments` block (Component 7; Req 3.9, 3.16).
 *
 * Returns the block with only well-formed items, or `null` when `data.galleryMoments` is absent or
 * structurally invalid — in which case the gallery runs its legacy `galleryClips` path unchanged.
 * An item is kept only when its `ref.project_id` equals the block's `project_id`, `kind` is one of
 * `video`/`image`/`audio`, `start_ms`/`end_ms` are integers with `0 <= start_ms < end_ms`, and
 * `display_name` is a non-empty string; anything else is dropped and the list caps at
 * {@link MOMENTS_MAX}, so a hand-edited or corrupted manifest degrades to fewer cards rather than throwing.
 */
/** Most cards a Moments manifest may carry (mirrors `render/moments.py` MOMENTS_MAX). */
export const MOMENTS_MAX = 200;
/** Cards shown on open, and how many each "Load more" adds. URLs are minted only for shown cards. */
export const MOMENTS_PAGE = 24;

export function readMoments(data: ClipGalleryData): MomentsBlock | null {
  const block = data.galleryMoments as MomentsBlock | undefined;
  if (!block || typeof block !== 'object') return null;
  const projectId = block.project_id;
  if (typeof projectId !== 'string' || projectId === '') return null;
  if (!Array.isArray(block.items)) return null;

  const items: MomentItem[] = [];
  for (const raw of block.items) {
    if (items.length >= MOMENTS_MAX) break;
    if (!raw || typeof raw !== 'object') continue;
    const m = raw as MomentItem;
    if (typeof m.id !== 'string' || m.id === '') continue;
    const ref = m.ref as MomentItem['ref'] | undefined;
    if (!ref || typeof ref !== 'object') continue;
    if (ref.project_id !== projectId) continue;
    if (typeof ref.asset_id !== 'string' || ref.asset_id === '') continue;
    if (!(ref.frame_s3_key === null || typeof ref.frame_s3_key === 'string')) continue;
    if (!isSearchKind(m.kind)) continue;
    if (!Number.isInteger(m.start_ms) || !Number.isInteger(m.end_ms)) continue;
    if (!(m.start_ms >= 0 && m.start_ms < m.end_ms)) continue;
    if (typeof m.display_name !== 'string' || m.display_name === '') continue;
    items.push({
      id: m.id,
      ref: { project_id: ref.project_id, asset_id: ref.asset_id, frame_s3_key: ref.frame_s3_key ?? null },
      kind: m.kind,
      display_name: m.display_name,
      start_ms: m.start_ms,
      end_ms: m.end_ms,
      source_index: typeof m.source_index === 'string' ? m.source_index : null,
      final_score: typeof m.final_score === 'number' && Number.isFinite(m.final_score) ? m.final_score : 0,
      text: typeof m.text === 'string' ? m.text : '',
      text_truncated: m.text_truncated === true,
      highlight_terms: Array.isArray(m.highlight_terms)
        ? m.highlight_terms.filter((x): x is string => typeof x === 'string')
        : [],
      citation: typeof m.citation === 'string' ? m.citation : null,
      ...(Array.isArray(m.matched_by) ? { matched_by: m.matched_by.filter((x): x is number => Number.isInteger(x)) } : {}),
      ...(Array.isArray(m.found_by) ? { found_by: m.found_by.filter((x): x is string => x === 'meaning' || x === 'keyword') } : {}),
    });
  }
  return { ...block, project_id: projectId, items };
}

/** Compose the natural-language turn the agent will run. Names each clip + its keep-range;
 *  the src is threaded in `clips[].src` (not the prompt text) so URLs never hit the transcript.
 *
 *  When every marked entry comes from the SAME source file (the usual case: marking several
 *  shots of one video), the prompt says so explicitly. Without that, "Clip these 7 clips to
 *  their marked ranges" reads as 7 independent targets, and the planner's fan-out rule turns it
 *  into a 7-node plan where each node re-reads the whole source — 7m39s for cutting that one
 *  ffmpeg pass does in about a minute (session 27c33c57). */
export function buildBatchTrimPrompt(
  verb: 'trim' | 'clip',
  marked: { label: string; in: number; out: number; src?: string }[],
): string {
  const tc = formatTimecode;
  const noun = verb === 'trim' ? 'clip' : 'video';
  const action = verb === 'trim' ? 'Trim' : 'Clip';
  if (marked.length === 0) return `${action} the ${noun}.`;
  if (marked.length === 1) {
    const m = marked[0];
    return `${action} "${m.label}" — keep ${tc(m.in)} to ${tc(m.out)}.`;
  }
  const parts = marked.map(m => `"${m.label}" keep ${tc(m.in)}–${tc(m.out)}`).join('; ');
  const srcs = marked.map(m => m.src).filter(Boolean);
  const oneSource = srcs.length === marked.length && new Set(srcs).size === 1;
  const suffix = oneSource
    ? ' These are all ranges of the SAME source file: cut them in ONE pass (one step, not one job'
      + ' or plan node per clip), then show every clip in one combined gallery.'
    : '';
  return `${action} these ${marked.length} clips to their marked ranges: ${parts}.${suffix}`;
}

/** Compose the natural-language turn for a multi-video frame extraction. Names each clip +
 *  its capture point; the src is threaded in `clips[].src` (not the prompt text). */
export function buildFrameExtractPrompt(marked: { label: string; at: number }[]): string {
  const tc = formatTimecode;
  if (marked.length === 0) return 'Extract a frame.';
  if (marked.length === 1) return `Extract a still frame from "${marked[0].label}" at ${tc(marked[0].at)}.`;
  const parts = marked.map(m => `"${m.label}" at ${tc(m.at)}`).join('; ');
  return `Extract a still frame from each of these ${marked.length} clips: ${parts}.`;
}

/** Compose the natural-language turn for a multi-video CROP. Names each clip + its normalized
 *  box (fractions of the frame); the src is threaded in `clips[].src`. The agent runs
 *  crop_video.py per clip with the box. */
export function buildBatchCropPrompt(marked: { label: string; box: CropBox }[]): string {
  const f = (n: number) => Math.max(0, Math.min(1, n)).toFixed(3);
  const arg = (b: CropBox) => `${f(b.x)},${f(b.y)},${f(b.w)},${f(b.h)}`;
  // The box value IS the final crop (aspect baked into w/h); the SKILL routes it to crop_video.
  if (marked.length === 0) return 'Crop the clip.';
  if (marked.length === 1) {
    const m = marked[0];
    return `Crop "${m.label}" to box="${arg(m.box)}" (x, y, w, h as fractions of the frame).`;
  }
  const parts = marked.map(m => `"${m.label}" box="${arg(m.box)}"`).join('; ');
  return `Crop these ${marked.length} clips, each to its box (x, y, w, h as fractions of the frame): ${parts}.`;
}

// Button styles shared by both modes.
function mkPrimaryStyle(t: RendererProps['theme'], enabled: boolean): React.CSSProperties {
  return {
    padding: '7px 14px', fontSize: t.typography.sizes.meta, borderRadius: t.radius.button,
    border: 'none', background: enabled ? t.colors.primary : t.colors.border,
    color: enabled ? t.colors.onPrimary : t.colors.textTertiary, fontWeight: t.typography.weights.semibold,
    cursor: enabled ? 'pointer' : 'not-allowed', fontFamily: 'inherit', whiteSpace: 'nowrap',
  };
}
function secondaryStyle(t: RendererProps['theme'], active: boolean): React.CSSProperties {
  return {
    padding: '7px 14px', fontSize: t.typography.sizes.meta, borderRadius: t.radius.button,
    border: `1px solid ${t.colors.borderStrong}`, background: active ? t.colors.accentSubtle : t.colors.surface,
    color: t.colors.textPrimary, fontWeight: t.typography.weights.semibold,
    cursor: 'pointer', fontFamily: 'inherit', whiteSpace: 'nowrap',
  };
}

/** Entry point: branch to Moments mode when the manifest carries a valid Moments block,
 *  else the legacy clip gallery (Req 3.16). Split so each mode's hooks run unconditionally. */
export default function ClipGallery(props: RendererProps<ClipGalleryData>) {
  // Memoized on the block, so a parent re-render keeps the same cards (the tab's search resets its page and
  // marks only when the result set really changes).
  const block = props.data.galleryMoments;
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const moments = React.useMemo(() => readMoments(props.data), [block]);
  return moments ? <MomentsGallery {...props} moments={moments} /> : <LegacyClipGallery {...props} />;
}

// ───────────────────────────────────────────────────────────────────────── Moments mode

function MomentsGallery({ data, theme: t, onInteraction, moments }: RendererProps<ClipGalleryData> & { moments: MomentsBlock }) {
  // Session + project context the cards' Expand and Find_Similar are scoped to. The renderer props
  // carry no session id, so read the on-screen session the same way the auto-render does.
  const sessionId = getActiveSessionId();

  // The tab's own search: its results open as this tab's next version (named by the query), never in place.
  const search = useMomentsTabSearch(moments, (entry, found) => {
    const label = data.title || 'Moments';
    requestOpenView({ sessionId, label,
      manifest: momentsVersionManifest(data, moments, entry, found, `view-${crypto.randomUUID?.() ?? Date.now().toString(36)}`, new Date().toISOString()) });
  });

  // Paged: a turn can carry up to 200 Moments, so the grid opens with one page and "Load more" adds the
  // next. Only shown cards exist, so only they mint URLs and only they join Play all.
  const [shown, setShown] = useState(MOMENTS_PAGE);
  const allItems = search.items;
  const items = allItems.slice(0, shown);
  const remaining = allItems.length - items.length;
  const visibleBlock = React.useMemo(
    () => ({ ...moments, items }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [moments, shown, allItems],
  );
  const activeProjectId = getSessionProject(sessionId)?.activeProjectId ?? null;

  // View-time presigned URLs for every card (minted on mount, refreshed per card; Req 3.26–3.28).
  const urls = useMomentUrls(visibleBlock);
  // Each video card's own picture: the frame its moment starts in (else the file's hero still).
  const frames = useMomentFrames(items);

  // Save to folder: pick moments, then file each as a bookmark (its asset + its range) in a project folder.
  const [picked, setPicked] = useState<ReadonlySet<string>>(() => new Set());
  const [saveOpen, setSaveOpen] = useState(false);
  const [saveFolders, setSaveFolders] = useState<string[]>([]);
  const [saveStatus, setSaveStatus] = useState<string | null>(null);

  // The membership snapshot `momentsBatchEntries` reads (Property 19; Req 3.12/3.14/3.15): each card's range is
  // the Moment's own span — a card has no marks; adjusting a range happens in the side panel.
  // A document (PDF page) has no media to play: it is never a batch member.
  const cards: MomentBatchCard[] = items.filter(isPlayableMoment).map((m) => ({
    id: m.id,
    label: m.display_name,
    kind: m.kind,
    ref: m.ref,
    mark: m.end_ms > m.start_ms ? { in: m.start_ms / 1000, out: m.end_ms / 1000 } : null,
    box: null,
    end: m.end_ms / 1000,
    phase: urls.state(m.id).phase,
  }));

  // --- Preview: the chat's modal, playing the moment's range once. Play all in sequence is the same modal over
  // a queue — each range in turn, advancing when one ends — so it needs no players on the cards.
  type Preview = { id: string; src: string | null; error: string | null; queue?: { ids: string[]; at: number } };
  const [preview, setPreview] = useState<Preview | null>(null);
  const previewSeq = useRef(0);
  const showPreview = useCallback((id: string, queue?: Preview['queue']) => {
    const m = allItems.find((x) => x.id === id);
    if (!m) { setPreview(null); return; }
    const seq = ++previewSeq.current;
    const card = urls.state(id);
    if (card.phase === 'ready') { setPreview({ id, src: card.url, error: null, queue }); return; }
    setPreview({ id, src: null, error: null, queue });
    void resolveCitationSource(m.ref, m.display_name).then((r) => {
      if (seq === previewSeq.current) setPreview({ id, src: r.ok ? r.url : null, error: r.ok ? null : r.message, queue });
    });
  }, [allItems, urls]);
  const playingId = preview?.queue ? preview.id : null;
  const stopAll = useCallback(() => { previewSeq.current++; setPreview(null); }, []);
  const playAll = useCallback(() => {
    const ids = momentsBatchEntries(cards, 'play').map((e) => e.clipId);
    if (ids.length > 0) showPreview(ids[0], { ids, at: 0 });
  }, [cards, showPreview]);
  const advance = useCallback(() => {
    const q = preview?.queue;
    if (!q) return;
    if (q.at + 1 >= q.ids.length) { stopAll(); return; }
    showPreview(q.ids[q.at + 1], { ids: q.ids, at: q.at + 1 });
  }, [preview, showPreview, stopAll]);
  const previewMoment = preview ? allItems.find((x) => x.id === preview.id) ?? null : null;

  // Find similar searches from this tab (its results are the tab's next version), not in a chat turn.
  const onFindSimilar = useCallback((moment: MomentItem) => {
    if (!isPlayableMoment(moment)) return;
    const image = search.imageOfMoment(moment);
    if (image) search.searchImages([image]);
  }, [search]);

  // A card's Clip icon: the SAME one-clip request "Clip all marked" composes (buildBatchTrimPrompt + the
  // clip-batch action), so the agent runs the existing media-processing clip path and the result arrives
  // as a timeline tab through the normal job-result render, opened without the bell. The source is the card's own media URL, which
  // WorkspaceView threads into the agent prompt (not the chat bubble).
  const onClip = useCallback((m: MomentItem, range: { in: number; out: number }) => {
    if (!isPlayableMoment(m)) return;
    const card = urls.state(m.id);
    const src = card.phase === 'ready' ? card.url : undefined;
    if (!src) return; // the card offers Clip only once its media is ready
    const verb: 'trim' | 'clip' = m.kind === 'audio' ? 'trim' : 'clip';
    const entry = { clipId: m.id, label: m.display_name, src, in: range.in, out: range.out, ref: m.ref, kind: m.kind };
    const prompt = buildBatchTrimPrompt(verb, [{ label: entry.label, in: entry.in, out: entry.out, src }]);
    const payload: BatchClipActionData = { verb, prompt, clips: [entry] };
    // The clip job this turn starts opens as a tab by itself (services/momentClipAutoOpen); recorded first
    // so even an instantly-registered job finds its entry.
    requestMomentClipAutoOpen(getActiveSessionId(), m.display_name);
    onInteraction?.({ type: 'action', target: `${verb}-batch`, data: payload, timestamp: Date.now() });
  }, [urls, onInteraction]);

  // A new result set starts on its first page, with nothing playing and nothing picked.
  React.useEffect(() => {
    setShown(MOMENTS_PAGE);
    stopAll();
    setPicked(new Set());
  }, [allItems, stopAll]);

  const togglePick = (id: string) => setPicked((cur) => {
    const next = new Set(cur);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    return next;
  });
  const pickedMoments = allItems.filter((m) => picked.has(m.id) && m.ref?.asset_id);
  const saveProjectId = pickedMoments[0]?.ref?.project_id ?? activeProjectId;
  const openSave = async (projectId: string | null = saveProjectId) => {
    setSaveStatus(null);
    if (!projectId) return;
    const r = await listFolders(projectId);
    setSaveFolders(flattenFolders(buildFolderTree(r.ok ? r.value.folders : [], readPendingFolders(saveProjectId))));
    setSaveOpen(true);
  };
  const saveTo = async (folder: string) => {
    setSaveOpen(false);
    if (!saveProjectId || pickedMoments.length === 0) return;
    const items = pickedMoments.map((m) => {
      const startMs = m.start_ms;
      const endMs = m.end_ms;
      return { assetId: m.ref.asset_id, startMs, ...(endMs > startMs ? { endMs } : {}),
        label: `${m.display_name} · ${formatTimecode(startMs / 1000)}–${formatTimecode(endMs / 1000)}` };
    });
    const r = await createBookmarks(saveProjectId, items, folder, sessionId);
    if (r.ok) {
      const n = r.value.created.length;
      setSaveStatus(`Saved ${n} moment${n === 1 ? '' : 's'} to ${folder || 'Top level'}`
        + (r.value.failed.length ? ` · ${r.value.failed.length} couldn't be saved` : ''));
      setPicked(new Set());
    } else setSaveStatus("Couldn't save the moments; try again.");
  };

  const title = data.title || 'Matched moments';
  // One line under the title: the agent's own subtitle, else just how many (and how many are showing).
  const howMany = allItems.length > MOMENTS_PAGE
    ? `Showing ${items.length} of ${allItems.length}${moments.truncated ? ' (top results)' : ''}`
    : `${allItems.length} ${allItems.length === 1 ? 'moment' : 'moments'}`;
  const subtitle = data.subtitle || howMany;
  const columns = Math.max(1, Math.min(3, Math.max(1, items.length)));

  return (
    <div data-testid="clip-gallery" style={{ background: t.colors.surface, border: `1px solid ${t.colors.border}`, borderRadius: t.radius.panel, padding: 16 }}>
      {/* Wraps in a narrow panel: the buttons drop below the title rather than squeezing it to one word a line. */}
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12, marginBottom: 16, flexWrap: 'wrap' }}>
        <div style={{ flex: '1 0 220px', minWidth: 0 }}>
          <h3 style={{ margin: 0, fontSize: 16, color: t.colors.textPrimary }}>{title}</h3>
          <p style={{ margin: '4px 0 0', fontSize: 12, color: t.colors.textSecondary }}
            data-testid={!data.subtitle && allItems.length > MOMENTS_PAGE ? 'moments-count' : undefined}>{subtitle}</p>
          {data.subtitle && allItems.length > MOMENTS_PAGE && (
            <p data-testid="moments-count" style={{ margin: '2px 0 0', fontSize: 11, color: t.colors.textTertiary }}>{howMany}</p>
          )}
        </div>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
          <button
            data-testid="play-all"
            onClick={playingId ? stopAll : playAll}
            style={secondaryStyle(t, !!playingId)}
            title={playingId ? 'Stop sequential playback' : 'Play every moment back-to-back'}
          >
            {playingId ? <><PauseIcon size={14} style={{ verticalAlign: '-2px', marginRight: 6 }} />Stop</> : <><PlayIcon size={14} style={{ verticalAlign: '-2px', marginRight: 6 }} />Play all in sequence</>}
          </button>
        </div>
      </div>
      <MomentsSearchBar search={search} theme={t} projectId={moments.project_id} sessionId={sessionId}
        selectedImage={pickedMoments.map((m) => search.imageOfMoment(m)).find((x) => !!x) ?? null} />
      {(picked.size > 0 || saveStatus) && (
        <div data-testid="moments-selection-bar" style={{ display: 'flex', alignItems: 'center', gap: 10, margin: '0 0 12px', padding: '8px 12px',
          borderRadius: t.radius.panel, background: t.colors.surfaceSubtle, border: `1px solid ${t.colors.border}`, fontSize: 13 }}>
          {picked.size > 0 ? (
            <>
              <b>{picked.size} selected</b>
              <span style={{ flex: 1 }} />
              <span style={{ position: 'relative', display: 'inline-flex' }}>
                <button type="button" data-testid="moments-save-to-folder" onClick={() => void openSave()}
                  disabled={!saveProjectId} style={mkPrimaryStyle(t, !!saveProjectId)}>Save to folder…</button>
                {saveOpen && (
                  <FolderPicker folders={saveFolders} title={`Save ${picked.size} moment${picked.size === 1 ? '' : 's'} to`}
                    verb="save" align="right" note="Saved as bookmarks: the files stay where they are."
                    onPick={(f) => void saveTo(f)} onClose={() => setSaveOpen(false)} />
                )}
              </span>
              <button type="button" onClick={() => setPicked(new Set())} style={linkButtonStyle(t)}>Clear</button>
            </>
          ) : (
            <>
              <span role="status" data-testid="moments-save-status">{saveStatus}</span>
              <span style={{ flex: 1 }} />
              <button type="button" onClick={() => setSaveStatus(null)} style={linkButtonStyle(t)}>Dismiss</button>
            </>
          )}
        </div>
      )}
      {/* A search on its way to becoming the next version: said here, above this tab's own cards. */}
      {search.pending && (
        <div data-testid={search.pending.status === 'error' ? 'moments-search-error' : 'moments-search-loading'}
          role="status" style={{ margin: '0 0 12px', color: t.colors.textSecondary, fontSize: 12 }}>
          {search.pending.status === 'preparing' ? 'Preparing the image for search… this takes a moment for a new picture.'
            : search.pending.status === 'loading' ? 'Searching the project…'
              : <>{search.pending.message}{' '}<button type="button" onClick={search.retry} style={linkButtonStyle(t)}>Try again</button></>}
        </div>
      )}
      {items.length === 0 ? (
        <div data-testid="clip-gallery-empty" style={{ padding: 24, textAlign: 'center', color: t.colors.textSecondary, fontSize: 12 }}>
          {moments.basis === 'searched' && search.queryImages.length
            ? <>No moments look like this image. Try another picture{search.kinds.length ? ', or clear the type filter' : ''}.</>
            : moments.basis === 'searched' && moments.cited_count === 0 && moments.query
              ? <>No moments match <span style={{ color: t.colors.textPrimary }}>“{moments.query}”</span>. Try other words{search.kinds.length ? ', or clear the type filter' : ''}.</>
              : 'No moments to review.'}
        </div>
      ) : (
        <ul data-testid="moments-grid" style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid',
          gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))`, gap: 14, alignItems: 'stretch' }}>
          {items.map((m) => m.kind === 'document' ? (
            <DocumentMomentCard
              key={m.id}
              moment={m}
              theme={t}
              sessionId={sessionId}
              cardUrl={urls.state(m.id)}
              onPreview={(x) => showPreview(x.id)}
            />
          ) : (
            <MomentCard
              key={m.id}
              moment={m}
              theme={t}
              sessionId={sessionId}
              cardUrl={urls.state(m.id)}
              frameUrl={frames[m.id] ?? null}
              onLoadStart={(resume) => urls.onLoadStart(m.id, resume)}
              onLoadOk={() => urls.onLoadOk(m.id)}
              onLoadError={(resume) => urls.onLoadError(m.id, resume)}
              onPreview={(x) => showPreview(x.id)}
              playing={playingId === m.id}
              // Find similar runs in this tab (no chat turn), so a reply in progress or another active project
              // no longer has to hold it back.
              turnInProgress={false}
              activeProjectId={null}
              onFindSimilar={onFindSimilar}
              badges={search.queryImages.length
                ? <MatchedBadges images={search.queryImages} matchedBy={m.matched_by} projectId={moments.project_id} /> : undefined}
              onClip={onInteraction ? onClip : undefined}
              onSaveToFolder={m.ref?.asset_id ? (x) => { setPicked(new Set([x.id])); void openSave(x.ref.project_id); } : undefined}
              selected={picked.has(m.id)}
              onSelectedChange={m.ref?.asset_id ? () => togglePick(m.id) : undefined}
              selecting={picked.size > 0}
            />
          ))}
        </ul>
      )}
      {remaining > 0 && (
        <button
          type="button"
          data-testid="moments-load-more"
          onClick={() => setShown(n => n + MOMENTS_PAGE)}
          style={loadMoreStyle(t)}
        >
          Load more <span style={{ color: t.colors.textSecondary, fontWeight: t.typography.weights.regular }}>· {remaining} remaining</span>
        </button>
      )}
      {preview && previewMoment && (
        <AssetPreviewModal
          key={`${preview.id}-${preview.queue?.at ?? ''}`}
          kind={previewMoment.kind}
          src={preview.src}
          name={previewMoment.display_name}
          sub={preview.queue ? `Playing all · ${preview.queue.at + 1} of ${preview.queue.ids.length}` : undefined}
          range={previewMoment.kind === 'document'
            ? { page: pageOfSpan(previewMoment.start_ms) }
            : previewMoment.kind === 'image' ? undefined
              : { startMs: previewMoment.start_ms, ...(previewMoment.end_ms > previewMoment.start_ms ? { endMs: previewMoment.end_ms } : {}) }}
          error={preview.error}
          onClose={stopAll}
          moveTarget={{ projectId: previewMoment.ref.project_id, assetId: previewMoment.ref.asset_id }}
          onRangeEnd={preview.queue ? advance : undefined}
          onDownload={preview.src ? () => void downloadFile(preview.src!, previewMoment.display_name) : undefined}
          onOpenInPanel={() => void openAssetAt({ ref: previewMoment.ref, kind: previewMoment.kind, displayName: previewMoment.display_name,
            startMs: previewMoment.start_ms,
            ...(previewMoment.kind !== 'image' && previewMoment.kind !== 'document' && previewMoment.end_ms > previewMoment.start_ms
              ? { range: { start: previewMoment.start_ms / 1000, end: previewMoment.end_ms / 1000 } } : {}) }, sessionId)}
        />
      )}
    </div>
  );
}


const KIND_LABEL: Record<string, string> = { video: 'Video', image: 'Image', audio: 'Audio', document: 'Document' };

function isPlayableMoment(m: MomentItem): m is MomentItem & { kind: PlayableKind } {
  return m.kind !== 'document';
}

/**
 * The tab's search row: the query box (pre-filled with the query that found this tab's cards), Search, and the
 * type chips. Quiet by design — one raised field, everything else muted until hovered or active. Earlier searches
 * are the tab's versions (the panel's ‹ vN of M ›), so the row has no history control of its own.
 */
function MomentsSearchBar({ search, theme: t, projectId, sessionId, selectedImage }: {
  search: MomentsTabSearch; theme: RendererProps['theme']; projectId: string; sessionId: string; selectedImage: MomentsQueryImage | null;
}) {
  const imageMode = search.images.length > 0;
  const inputRef = useRef<HTMLInputElement>(null);
  const focusWords = useRef(false);
  // "Search by words" / ✕: back to an empty, focused box (focused once it has rendered again).
  const toWords = () => { focusWords.current = true; search.clearImages(); };
  useEffect(() => {
    if (!imageMode && focusWords.current) { focusWords.current = false; inputRef.current?.focus(); }
  }, [imageMode]);
  return (
    <div data-testid="moments-search" style={{ marginBottom: 14 }}>
      <form
        role="search"
        aria-label="Search moments"
        onSubmit={(e) => { e.preventDefault(); search.submit(); }}
        style={{ display: 'flex', alignItems: 'center', gap: 8 }}
      >
        {/* Words OR an image, never both: M2C searches one kind per call, so the field holds exactly one
            (mock search-and-actions.html §1). Image mode has no text input to type into. */}
        <div style={{ ...searchFieldStyle(t), gap: 6, padding: '3px 6px 3px 10px' }} data-testid="moments-search-field"
          data-mode={imageMode ? 'image' : 'words'} title={imageMode ? undefined : 'Narrow with @File.mp4, #topic or #utterance'}>
          {imageMode ? (
            <ImageQueryValue search={search} projectId={projectId} sessionId={sessionId} selectedImage={selectedImage} theme={t}
              onWords={toWords} />
          ) : (
            <>
              <SearchIcon size={14} style={{ color: t.colors.textTertiary, flexShrink: 0 }} />
              <input
                ref={inputRef}
                type="search"
                aria-label="Search query"
                data-testid="moments-search-input"
                value={search.query}
                maxLength={200}
                onChange={(e) => search.setQuery(e.target.value)}
                placeholder="Search this project"
                style={{
                  flex: 1, minWidth: 90, background: 'transparent', border: 'none', outline: 'none',
                  color: t.colors.textPrimary, fontSize: t.typography.sizes.meta, fontFamily: 'inherit',
                }}
              />
              <SearchByImageButton search={search} projectId={projectId} sessionId={sessionId} selectedImage={selectedImage} theme={t} />
            </>
          )}
        </div>
        {/* Enter searches; the button stays for assistive tech, not on screen. */}
        <button type="submit" data-testid="moments-search-submit" disabled={!search.query.trim()} style={SR_ONLY}>
          Search
        </button>
      </form>
      <div role="group" aria-label="Filter by type" style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 8 }}>
        {/* "All" only matters once a type is picked: it is the way back. */}
        {search.kinds.length > 0 && (
          <button type="button" aria-pressed={false} onClick={search.clearKinds} style={chipStyle(t, false)}>All</button>
        )}
        {SEARCH_KINDS.map((k) => (
          <button key={k} type="button" data-testid={`moments-kind-${k}`} aria-pressed={search.kinds.includes(k)}
            onClick={() => search.toggleKind(k)} style={chipStyle(t, search.kinds.includes(k))}>
            {KIND_LABEL[k]}
          </button>
        ))}
      </div>
    </div>
  );
}

const SR_ONLY: React.CSSProperties = {
  position: 'absolute', width: 1, height: 1, padding: 0, margin: -1, overflow: 'hidden', clip: 'rect(0 0 0 0)',
  whiteSpace: 'nowrap', border: 0,
};

function searchFieldStyle(t: RendererProps['theme']): React.CSSProperties {
  return {
    flex: 1, display: 'flex', alignItems: 'center', gap: 8, minWidth: 0, padding: '7px 10px',
    background: t.colors.background, border: `1px solid ${t.colors.border}`, borderRadius: t.radius.button,
    cursor: 'text',
  };
}

function chipStyle(t: RendererProps['theme'], active: boolean): React.CSSProperties {
  return {
    padding: '3px 10px', fontSize: 11, fontFamily: 'inherit', cursor: 'pointer',
    borderRadius: t.radius.pill ?? 999,
    border: `1px solid ${active ? t.colors.primary : t.colors.border}`,
    background: active ? t.colors.accentSubtle ?? 'transparent' : 'transparent',
    color: active ? t.colors.textPrimary : t.colors.textSecondary,
    fontWeight: active ? t.typography.weights.semibold : t.typography.weights.regular,
  };
}

function linkButtonStyle(t: RendererProps['theme']): React.CSSProperties {
  return {
    background: 'none', border: 'none', padding: 0, cursor: 'pointer', fontFamily: 'inherit',
    fontSize: 12, color: t.colors.primary, textDecoration: 'underline',
  };
}

/** The quiet full-width pager under the grid: a muted row, not a primary button. */
function loadMoreStyle(t: RendererProps['theme']): React.CSSProperties {
  return {
    display: 'block', width: '100%', marginTop: 14, padding: '9px 12px',
    fontSize: t.typography.sizes.meta, fontWeight: t.typography.weights.semibold, fontFamily: 'inherit',
    color: t.colors.textPrimary, background: 'transparent', cursor: 'pointer',
    border: `1px dashed ${t.colors.border}`, borderRadius: t.radius.button, textAlign: 'center',
  };
}

// ───────────────────────────────────────────────────────────────────────── Legacy mode

function LegacyClipGallery({ data, theme: t, onInteraction }: RendererProps<ClipGalleryData>) {
  const clips = readClips(data);
  const [marks, setMarks] = useState<Record<string, ClipMark>>({});
  const [crops, setCrops] = useState<Record<string, CropBox>>({});

  const onMarkChange = useCallback((clipId: string, mark: ClipMark | null) => {
    setMarks(prev => {
      if (mark) return { ...prev, [clipId]: mark };
      if (!(clipId in prev)) return prev;
      const next = { ...prev }; delete next[clipId]; return next;
    });
  }, []);

  const onCropChange = useCallback((clipId: string, box: CropBox | null) => {
    setCrops(prev => {
      if (box) return { ...prev, [clipId]: box };
      if (!(clipId in prev)) return prev;
      const next = { ...prev }; delete next[clipId]; return next;
    });
  }, []);

  // --- Play all in sequence: drive each clip's media element back-to-back, advancing on
  // 'ended', so the reviewer previews the reel's flow before stitching (the "Play all" from
  // the static review deck). Event-driven so it stops cleanly and needs no timers.
  const mediaRefs = useRef<Map<string, HTMLMediaElement>>(new Map());
  const seqRef = useRef<{ stop: boolean }>({ stop: false });
  const cleanupRef = useRef<null | (() => void)>(null);
  const [playingId, setPlayingId] = useState<string | null>(null);

  const registerMedia = useCallback((clipId: string, el: HTMLMediaElement | null) => {
    if (el) mediaRefs.current.set(clipId, el);
    else mediaRefs.current.delete(clipId);
  }, []);

  const orderedIds = clips.map(c => c.id);

  const advance = useCallback((idx: number) => {
    cleanupRef.current?.(); cleanupRef.current = null;
    if (seqRef.current.stop || idx >= orderedIds.length) { setPlayingId(null); return; }
    const id = orderedIds[idx];
    const el = mediaRefs.current.get(id);
    if (!el) { advance(idx + 1); return; }
    setPlayingId(id);
    const onEnded = () => advance(idx + 1);
    el.addEventListener('ended', onEnded);
    cleanupRef.current = () => el.removeEventListener('ended', onEnded);
    try {
      el.currentTime = 0;
      const p = el.play();
      if (p && typeof (p as Promise<void>).catch === 'function') (p as Promise<void>).catch(() => { /* autoplay/interrupt */ });
    } catch { /* jsdom / blocked playback */ }
  }, [orderedIds]);

  const stopAll = useCallback(() => {
    seqRef.current.stop = true;
    cleanupRef.current?.(); cleanupRef.current = null;
    mediaRefs.current.forEach(el => { try { el.pause(); } catch { /* noop */ } });
    setPlayingId(null);
  }, []);

  const playAll = useCallback(() => {
    seqRef.current = { stop: false };
    advance(0);
  }, [advance]);

  if (clips.length === 0) {
    return <div data-testid="clip-gallery-empty" style={{ padding: 24, textAlign: 'center', color: t.colors.textSecondary, fontSize: 12 }}>No clips to review.</div>;
  }

  const clipById = new Map(clips.map(c => [c.id, c]));
  const anyVideo = clips.some(c => c.kind === 'video');
  const verb: 'trim' | 'clip' = anyVideo ? 'clip' : 'trim';
  const markedIds = clips.map(c => c.id).filter(id => marks[id]); // preserve gallery order
  const nMarked = markedIds.length;
  // Frame extraction applies to marked VIDEO clips only; capture point = each clip's mark-in.
  const videoMarkedIds = markedIds.filter(id => clipById.get(id)?.kind === 'video');
  const nVideoMarked = videoMarkedIds.length;
  // Crop applies to VIDEO clips that have a crop region set (independent of the trim marks).
  const croppedIds = clips.map(c => c.id).filter(id => crops[id] && clipById.get(id)?.kind === 'video');
  const nCropped = croppedIds.length;

  // The whole set, so a Trim / Clip / Crop of some of it comes back as this gallery's next version (not a separate tab).
  const wholeSet = clips.map(c => ({ clipId: c.id, label: c.label, src: c.src, kind: c.kind }));
  const emitBatch = () => {
    if (nMarked === 0) return;
    const entries = markedIds.map(id => {
      const c = clipById.get(id)!; const m = marks[id];
      return { clipId: id, label: c.label, src: c.src, in: m.in, out: m.out };
    });
    const prompt = buildBatchTrimPrompt(verb, entries.map(e => ({ label: e.label, in: e.in, out: e.out, src: e.src })));
    const payload: BatchClipActionData = { verb, prompt, clips: entries, set: wholeSet };
    onInteraction?.({ type: 'action', target: `${verb}-batch`, data: payload, timestamp: Date.now() });
  };

  // Multi-video frame extraction: one still per marked video at its mark-in, in ONE request.
  const emitExtract = () => {
    if (nVideoMarked === 0) return;
    const entries = videoMarkedIds.map(id => {
      const c = clipById.get(id)!; const m = marks[id];
      return { clipId: id, label: c.label, src: c.src, in: m.in, out: m.out, at: m.in };
    });
    const prompt = buildFrameExtractPrompt(entries.map(e => ({ label: e.label, at: e.at })));
    const payload: BatchClipActionData = { verb: 'extract', prompt, clips: entries };
    onInteraction?.({ type: 'action', target: 'extract-frames', data: payload, timestamp: Date.now() });
  };

  // Multi-video crop: crop each boxed clip to its own region in ONE request. The user boxes
  // each clip individually (per-card Crop), then this single "Crop all" button acts on every
  // boxed clip at once — the same pattern as "Clip all marked" / "Extract frame". There is no
  // per-clip crop action: a card only SETS a box, it never crops on its own.
  const emitCropBatch = () => {
    if (nCropped === 0) return;
    const entries = croppedIds.map(id => {
      const c = clipById.get(id)!;
      return { clipId: id, label: c.label, src: c.src, box: crops[id] };
    });
    const prompt = buildBatchCropPrompt(entries.map(e => ({ label: e.label, box: e.box })));
    const payload: BatchClipActionData = { verb: 'crop', prompt, clips: entries, set: wholeSet };
    onInteraction?.({ type: 'action', target: 'crop-batch', data: payload, timestamp: Date.now() });
  };

  // Expand a video clip into its own Renderer timeline tab (client-side, no agent turn).
  const onExpand = (clip: ClipGalleryClip) => {
    const payload: OpenTabActionData = { url: clip.src, label: clip.label };
    onInteraction?.({ type: 'open-tab', target: 'expand-timeline', data: payload, timestamp: Date.now() });
  };

  const title = data.title || (verb === 'trim' ? 'Review & trim clips' : 'Review & clip takes');
  const subtitle = data.subtitle || 'Mark a keep-range on each clip, then act on them together.';
  const columns = Math.max(1, Math.min(3, clips.length));

  const mkPrimary = (enabled: boolean): React.CSSProperties => mkPrimaryStyle(t, enabled);
  const secondaryBtn: React.CSSProperties = secondaryStyle(t, !!playingId);

  return (
    <div data-testid="clip-gallery" style={{ background: t.colors.surface, border: `1px solid ${t.colors.border}`, borderRadius: t.radius.panel, padding: 16 }}>
      {/* Wraps in a narrow panel: the buttons drop below the title rather than squeezing it to one word a line. */}
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12, marginBottom: 16, flexWrap: 'wrap' }}>
        <div style={{ flex: '1 0 220px', minWidth: 0 }}>
          <h3 style={{ margin: 0, fontSize: 16, color: t.colors.textPrimary }}>{title}</h3>
          <p style={{ margin: '4px 0 0', fontSize: 12, color: t.colors.textSecondary }}>{subtitle}</p>
        </div>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
          <button
            data-testid="play-all"
            onClick={playingId ? stopAll : playAll}
            style={secondaryBtn}
            title={playingId ? 'Stop sequential playback' : 'Play every clip back-to-back (preview the reel flow)'}
          >
            {playingId ? <><PauseIcon size={14} style={{ verticalAlign: '-2px', marginRight: 6 }} />Stop</> : <><PlayIcon size={14} style={{ verticalAlign: '-2px', marginRight: 6 }} />Play all in sequence</>}
          </button>
          <button
            data-testid="trim-all-marked"
            onClick={emitBatch}
            disabled={nMarked === 0}
            style={mkPrimary(nMarked > 0)}
            title={nMarked ? `Send one request to ${verb} all marked clips` : 'Mark a keep-range on at least one clip'}
          >
            <ScissorsIcon size={14} style={{ verticalAlign: '-2px', marginRight: 6 }} />{verb === 'trim' ? 'Trim' : 'Clip'} all marked ({nMarked})
          </button>
          {anyVideo && (
            <button
              data-testid="extract-frames"
              onClick={emitExtract}
              disabled={nVideoMarked === 0}
              style={mkPrimary(nVideoMarked > 0)}
              title={nVideoMarked ? 'Extract a still from each marked video at its mark-in' : 'Mark a keep-range on at least one video'}
            >
              <ImageIcon size={14} style={{ verticalAlign: '-2px', marginRight: 6 }} />Extract frame ({nVideoMarked})
            </button>
          )}
          {anyVideo && (
            <button
              data-testid="crop-all"
              onClick={emitCropBatch}
              disabled={nCropped === 0}
              style={mkPrimary(nCropped > 0)}
              title={nCropped ? 'Crop every boxed clip to its region, in one request' : 'Set a crop box (Crop) on at least one clip first'}
            >
              <CropIcon size={14} style={{ verticalAlign: '-2px', marginRight: 6 }} />Crop all ({nCropped})
            </button>
          )}
        </div>
      </div>
      {/* As many columns as FIT (each card at least 240 px), up to one per clip: a fixed 3 made ~100 px cards in the side panel. */}
      <div data-testid="clip-gallery-grid" style={{ display: 'grid', gridTemplateColumns: `repeat(auto-fill, minmax(min(100%, ${columns === 1 ? '100%' : '240px'}), 1fr))`, gap: 14, alignItems: 'start' }}>
        {clips.map((clip, i) => (
          <ClipEditor key={clip.id} clip={clip} theme={t} onMarkChange={onMarkChange} accent={PALETTE[i % PALETTE.length]} registerMedia={registerMedia} playing={playingId === clip.id} onExpand={onExpand} onCropChange={onCropChange} cropBox={crops[clip.id] ?? null} />
        ))}
      </div>
    </div>
  );
}
