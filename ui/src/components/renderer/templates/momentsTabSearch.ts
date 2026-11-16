/**
 * The Moments tab's own search (2026-10-11; image queries 2026-10-18; versions 2026-11-15): edit the query and
 * search again — the results open as the NEXT VERSION of this Moments tab.
 *
 * - A query is TEXT (`{q}`) or ONE IMAGE (`{images}`). The search bar shows an image as a chip; typing over it asks
 *   first, because M2C cannot combine words and an image in one search.
 * - A search's results are a new view of the tab (`momentsVersionManifest`), opened through the panel's one
 *   client-view path: it joins this tab's versions (‹ vN of M ›, each named by its query), is saved with the chat,
 *   and reopens as it was. The tab itself never changes under you. (Until 2026-11 a tab kept its own search
 *   history behind ‹ › arrows and a clock menu — a second "previous results" control beside the version switcher.)
 * - The type chips FILTER the cards on screen; a search fetches every kind and its version opens with the same
 *   chips pressed.
 * - Each fetched result set gets its own card-id prefix, so per-card URL/mark state can never carry over.
 * - An image whose search rendition is still being made (a just-dropped picture) is retried until it is ready.
 * - The agent is not told about these searches (v1).
 */
import { SEARCH_KINDS as SHARED_SEARCH_KINDS, isSearchKind } from '../../../shared/searchKinds';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ClipGalleryData, MomentItem, MomentsBlock, MomentsQueryImage } from '../types';
import { searchMoments, searchMomentsByImages, type MomentsSearchImage } from '../../../projects/api';
import { getActiveSessionId } from '../../../services/activeSession';
import { viewSubjectIds } from '../../../shared/renderSubject';
import {
  VIEW_SEARCH_IMAGE_LABEL_MAX, VIEW_SEARCH_QUERY_MAX,
  type RenderViewQueryImage, type RenderViewSearchEntry, type RenderViewSearchKind,
} from '../../../shared/renderViewState';

export const SEARCH_KINDS: RenderViewSearchKind[] = [...SHARED_SEARCH_KINDS];
// ONE image per search in the UI: M2C searches one image per call, and "match any" across several read as a
// different feature than the one-picture search people expect.
export const QUERY_IMAGES_MAX = 1;
/** How long a not-yet-searchable image (409 image_not_ready) is retried, and how often. */
export const NOT_READY_RETRY_MS = 5_000;
export const NOT_READY_RETRIES = 24;

/** A search on its way to becoming a version: running, waiting for a new picture, or failed. */
export type PendingSearch =
  | { status: 'loading' }
  | { status: 'preparing' }
  | { status: 'error'; message: string };

export interface MomentsTabSearch {
  /** The search box text. */
  query: string;
  setQuery: (q: string) => void;
  /** The query images in the bar (chips), in order. */
  images: MomentsQueryImage[];
  /** Selected kind chips; empty = every kind. */
  kinds: RenderViewSearchKind[];
  toggleKind: (k: RenderViewSearchKind) => void;
  clearKinds: () => void;
  /** Search the box text (words mode: the bar holds words OR an image, never both). */
  submit: () => void;
  /** Leave image mode for words: drop the image, empty box. */
  clearImages: () => void;
  /** Search by exactly these images (a new search). */
  searchImages: (images: MomentsQueryImage[]) => void;
  /** Add an image to the image search; at most {@link QUERY_IMAGES_MAX}. */
  addImage: (image: MomentsQueryImage) => void;
  replaceImage: (index: number, image: MomentsQueryImage) => void;
  removeImage: (index: number) => void;
  /** Find similar on a card: a new search by that card's image — here, not in a chat turn. Null if it has none. */
  imageOfMoment: (m: MomentItem) => MomentsQueryImage | null;
  /** The tab's own image query (what found its cards), for the cards' "matched by" badges. */
  queryImages: MomentsQueryImage[];
  /** This tab's cards, filtered by the kind chips. */
  items: MomentItem[];
  /** A search not yet opened as a version (null when none is running or failed). */
  pending: PendingSearch | null;
  retry: () => void;
}

const mmss = (ms: number) => {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
};

const cut = (label: string) => [...label].slice(0, VIEW_SEARCH_IMAGE_LABEL_MAX).join('');

/** A card's image as a query: an image moment by its asset, a video moment by its frame. Audio/documents: none. */
export function imageOfMoment(m: MomentItem): MomentsQueryImage | null {
  if (m.kind === 'image') return { assetId: m.ref.asset_id, label: cut(m.display_name) };
  if (m.kind === 'video' && m.ref.frame_s3_key) {
    return { frameKey: m.ref.frame_s3_key, sourceAssetId: m.ref.asset_id, atMs: m.start_ms,
      label: cut(`Frame ${mmss(m.start_ms)} · ${m.display_name}`) };
  }
  return null;
}

const toWire = (i: MomentsQueryImage): MomentsSearchImage | null =>
  i.assetId ? { assetId: i.assetId } : i.frameKey && i.sourceAssetId ? { frameKey: i.frameKey, sourceAssetId: i.sourceAssetId } : null;

const toEntryImage = (i: MomentsQueryImage): RenderViewQueryImage => {
  const out: RenderViewQueryImage = { label: cut(i.label || 'Image') };
  if (i.assetId) out.assetId = i.assetId;
  else { out.frameKey = i.frameKey; out.sourceAssetId = i.sourceAssetId; }
  if (typeof i.atMs === 'number') out.atMs = i.atMs;
  return out;
};

/** The cards of one search response, validated like a manifest's and given a per-search id prefix. */
export function cardsFromResponse(projectId: string, raw: unknown[], prefix: string): MomentItem[] {
  const out: MomentItem[] = [];
  for (const r of raw) {
    const m = r as Partial<MomentItem> | null;
    if (!m || typeof m !== 'object' || typeof m.id !== 'string') continue;
    const ref = m.ref;
    if (!ref || ref.project_id !== projectId || typeof ref.asset_id !== 'string' || !ref.asset_id) continue;
    if (!isSearchKind(m.kind)) continue;
    if (!Number.isInteger(m.start_ms) || !Number.isInteger(m.end_ms) || !((m.start_ms as number) >= 0 && (m.start_ms as number) < (m.end_ms as number))) continue;
    if (typeof m.display_name !== 'string' || !m.display_name) continue;
    out.push({
      id: `${prefix}-${m.id}`,
      ref: { project_id: ref.project_id, asset_id: ref.asset_id, frame_s3_key: typeof ref.frame_s3_key === 'string' ? ref.frame_s3_key : null },
      kind: m.kind,
      display_name: m.display_name,
      start_ms: m.start_ms as number,
      end_ms: m.end_ms as number,
      source_index: typeof m.source_index === 'string' ? m.source_index : null,
      final_score: typeof m.final_score === 'number' && Number.isFinite(m.final_score) ? m.final_score : 0,
      text: typeof m.text === 'string' ? m.text : '',
      text_truncated: m.text_truncated === true,
      highlight_terms: Array.isArray(m.highlight_terms) ? m.highlight_terms.filter((x): x is string => typeof x === 'string') : [],
      citation: null,
      ...(Array.isArray(m.matched_by) ? { matched_by: m.matched_by.filter((x): x is number => Number.isInteger(x)) } : {}),
      ...(Array.isArray(m.found_by) ? { found_by: m.found_by.filter((x): x is string => x === 'meaning' || x === 'keyword') } : {}),
    });
  }
  return out;
}

/** The turn's starting query from the manifest: its typed `queryRef`, else the old text `query`. */
function originalOf(moments: MomentsBlock): { q: string; images: MomentsQueryImage[] } {
  const ref = moments.queryRef;
  if (ref && ref.type === 'image' && Array.isArray(ref.images)) {
    const images = ref.images.filter((i) => i && typeof i.label === 'string' && (i.assetId || (i.frameKey && i.sourceAssetId)))
      .slice(0, QUERY_IMAGES_MAX);
    if (images.length) return { q: '', images };
  }
  if (ref && ref.type === 'text' && typeof ref.q === 'string') return { q: ref.q, images: [] };
  return { q: typeof moments.query === 'string' ? moments.query : '', images: [] };
}

/**
 * A search's results as the next version of the tab `data` shows: the same title and subject (so it joins that
 * tab's versions), the query that found them (the version's name, and the box's text when it is open), and the
 * chips that were pressed.
 */
export function momentsVersionManifest(
  data: ClipGalleryData, moments: MomentsBlock, entry: RenderViewSearchEntry, items: MomentItem[], id: string, createdAt: string,
): Record<string, unknown> & { id: string } {
  const galleryMoments: MomentsBlock = {
    v: 1, project_id: moments.project_id, turn_id: moments.turn_id, basis: 'searched', cited_count: 0, truncated: false, items,
    ...(entry.images ? { queryRef: { type: 'image', images: entry.images, match: 'any' } } : { query: entry.q, queryRef: { type: 'text', q: entry.q } }),
    ...(entry.kinds?.length ? { kinds: [...entry.kinds] } : {}),
  };
  return {
    id, template: 'clip-gallery', createdAt, title: data.title || 'Moments',
    subjectIds: viewSubjectIds(data), galleryMoments,
  };
}

export function useMomentsTabSearch(
  moments: MomentsBlock,
  /** Called with a finished search (its entry and cards): the caller opens it as the tab's next version. */
  onResults: (entry: RenderViewSearchEntry, items: MomentItem[]) => void,
): MomentsTabSearch {
  const original = useMemo(() => originalOf(moments), [moments]);
  const [query, setQueryState] = useState<string>(original.q);
  const [images, setImages] = useState<MomentsQueryImage[]>(original.images);
  const [kinds, setKinds] = useState<RenderViewSearchKind[]>(() => (moments.kinds ?? []).filter(isSearchKind));
  const [pending, setPending] = useState<{ entry: RenderViewSearchEntry; state: PendingSearch } | null>(null);
  const seq = useRef(0);
  const live = useRef(0); // the search whose answer still counts (a newer one replaces it)
  const retryTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => { if (retryTimer.current) clearTimeout(retryTimer.current); }, []);
  const onResultsRef = useRef(onResults);
  onResultsRef.current = onResults;

  const run = useCallback((e: RenderViewSearchEntry, attempt = 0) => {
    if (retryTimer.current) { clearTimeout(retryTimer.current); retryTimer.current = null; }
    const mine = ++live.current;
    setPending({ entry: e, state: attempt > 0 ? { status: 'preparing' } : { status: 'loading' } });
    const prefix = `t${++seq.current}`;
    const wire = e.images ? e.images.map(toWire).filter((x): x is MomentsSearchImage => !!x) : null;
    const request = wire ? searchMomentsByImages(moments.project_id, wire, getActiveSessionId() || null) : searchMoments(moments.project_id, e.q ?? '');
    void request.then((r) => {
      if (mine !== live.current) return;
      if (!r.ok && r.kind === 'error' && r.code === 'image_not_ready' && attempt < NOT_READY_RETRIES) {
        // A just-dropped picture: its search rendition is still being made. Say so, and try again shortly.
        setPending({ entry: e, state: { status: 'preparing' } });
        retryTimer.current = setTimeout(() => run(e, attempt + 1), NOT_READY_RETRY_MS);
        return;
      }
      if (!r.ok) {
        setPending({ entry: e, state: { status: 'error', message: r.kind === 'error' ? r.message : 'This project is not available.' } });
        return;
      }
      setPending(null);
      onResultsRef.current(e, cardsFromResponse(moments.project_id, r.value.items, prefix));
    });
  }, [moments.project_id]);

  const withKinds = useCallback((e: RenderViewSearchEntry): RenderViewSearchEntry =>
    (kinds.length && kinds.length < SEARCH_KINDS.length ? { ...e, kinds: [...kinds] } : e), [kinds]);

  const searchText = useCallback((text: string) => {
    const q = [...text.trim()].slice(0, VIEW_SEARCH_QUERY_MAX).join('');
    if (!q) return;
    setImages([]);
    run(withKinds({ q }));
  }, [run, withKinds]);

  const searchImages = useCallback((next: MomentsQueryImage[]) => {
    const list = next.slice(0, QUERY_IMAGES_MAX);
    setImages(list);
    setQueryState('');
    if (list.length) run(withKinds({ images: list.map(toEntryImage) }));
  }, [run, withKinds]);

  const submit = useCallback(() => {
    const q = query.trim();
    if (q) searchText(q);
  }, [query, searchText]);

  // The kind chips FILTER the cards on screen.
  const items = useMemo(() => (kinds.length === 0 || kinds.length >= SEARCH_KINDS.length
    ? moments.items
    : moments.items.filter((m) => (kinds as string[]).includes(m.kind))), [moments.items, kinds]);

  const identity = (i: MomentsQueryImage) => i.assetId ?? `${i.sourceAssetId}|${i.frameKey}`;
  return {
    query,
    setQuery: (q: string) => setQueryState(q),
    images,
    kinds,
    toggleKind: (k) => setKinds((prev) => (prev.includes(k) ? prev.filter((x) => x !== k) : [...prev, k])),
    clearKinds: () => setKinds([]),
    submit,
    clearImages: () => { setImages([]); setQueryState(''); },
    searchImages,
    addImage: (image) => {
      if (images.some((i) => identity(i) === identity(image))) return;
      if (images.length >= QUERY_IMAGES_MAX) return;
      searchImages([...images, image]);
    },
    replaceImage: (i, image) => searchImages(images.map((x, j) => (j === i ? image : x))),
    removeImage: (i) => {
      const rest = images.filter((_, j) => j !== i);
      if (rest.length) searchImages(rest);
      else setImages([]);
    },
    imageOfMoment,
    queryImages: original.images,
    items,
    pending: pending?.state ?? null,
    retry: () => { if (pending) run(pending.entry); },
  };
}
