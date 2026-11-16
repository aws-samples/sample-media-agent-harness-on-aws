// @vitest-environment jsdom
//
// The Moments tab's own search (2026-10-11; versions 2026-11-15): the box is pre-filled with the query that found
// the cards, and a search's results open as the tab's next version (the panel's ‹ vN of M ›), never in place.
// Same mocks as ClipGallery.moments.test.tsx, plus the search API.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import '@testing-library/jest-dom';
import { cleanup, within, render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import React from 'react';

import ClipGallery from './ClipGallery';
import { theme } from '../../../styles/tokens';
import type { MomentItem, MomentsBlock, ClipGalleryData } from '../types';

// --- hoisted mock state the factories below close over ---------------------------------------------
const h = vi.hoisted(() => ({
  state: vi.fn(),
  freshUrlFor: vi.fn(),
  findSimilar: vi.fn(),
  blocks: [] as { items: { id: string }[] }[],
  searchMoments: vi.fn(),
  openAssetAt: vi.fn(),
}));

vi.mock('../../../projects/api', () => ({ searchMoments: h.searchMoments, getAsset: vi.fn(), getAssetUrls: vi.fn() }));
vi.mock('../../../projects/openCitation', () => ({ openAssetAt: h.openAssetAt }));

vi.mock('../../../projects/useMomentUrls', () => ({
  useMomentUrls: (block: { items: { id: string }[] }) => (h.blocks.push(block), {
    state: h.state,
    onLoadStart: vi.fn(),
    onLoadOk: vi.fn(),
    onLoadError: vi.fn(),
    freshUrlFor: h.freshUrlFor,
  }),
  URL_MAX_AGE_MS: 3_000_000,
}));
vi.mock('../../../projects/findSimilar', () => ({ findSimilar: h.findSimilar }));
vi.mock('../../../projects/turnState', () => ({ useSessionTurnInProgress: () => false }));
vi.mock('../../../services/activeSession', () => ({ getActiveSessionId: () => 'sess-1' }));
vi.mock('../../../projects/sessionProjectStore', () => ({ getSessionProject: () => ({ activeProjectId: 'p1' }) }));

// A light MomentCard stub: it registers a fake media element and exposes a button that sets a mark on
// the card, so the gallery's membership counts and dispatched entries can be driven deterministically.
vi.mock('./MomentCard', () => ({
  default: (props: {
    moment: MomentItem;
    onPreview?: (m: MomentItem) => void;
    playing?: boolean;
    onFindSimilar?: (m: MomentItem) => void;
  }) => {
    return (
      <div data-testid={`mc-${props.moment.id}`}>
        <button data-testid={`preview-${props.moment.id}`} data-playing={props.playing ? 'true' : undefined}
          onClick={() => props.onPreview?.(props.moment)}>
          preview
        </button>
        <button data-testid={`fs-${props.moment.id}`} onClick={() => props.onFindSimilar?.(props.moment)}>
          find similar
        </button>
      </div>
    );
  },
}));

function item(id: string, kind: MomentItem['kind'], over: Partial<MomentItem> = {}): MomentItem {
  return {
    id,
    ref: { project_id: 'p1', asset_id: `asset-${id}`, frame_s3_key: null },
    kind,
    display_name: id,
    start_ms: 0,
    end_ms: 4000,
    source_index: null,
    final_score: 0.5,
    text: '',
    text_truncated: false,
    highlight_terms: [],
    citation: null,
    ...over,
  };
}

function momentsData(items: MomentItem[], extra: Record<string, unknown> = {}): ClipGalleryData {
  const galleryMoments = { v: 1, project_id: 'p1', turn_id: 't1', basis: 'searched', truncated: false, items, query: 'night drive' };
  return { title: 'Moments', subtitle: 'Matched by this turn\'s searches', galleryMoments, ...extra } as unknown as ClipGalleryData;
}

const wire = (id: string) => ({ ...item(id, 'video'), id });
const ok = (items: unknown[]) => ({ ok: true, value: { query: 'q', kinds: null, items, total: items.length } });

function renderGallery(data: ClipGalleryData) {
  return render(<ClipGallery data={data} title="Moments" theme={theme as never} />);
}

const cardIds = () => screen.queryAllByTestId(/^mc-/).map((el) => el.getAttribute('data-testid'));

beforeEach(() => {
  h.state.mockReset().mockReturnValue({ phase: 'ready', url: 'https://x', obtainedAt: 0 });
  h.searchMoments.mockReset();
  h.blocks.length = 0;
});

describe('Moments tab search', () => {
  // Each search's results open as the next VERSION of this tab (IND403: the tab's own ‹ › history and clock menu were a
  // second "previous results" control beside the version switcher).
  const opened: Array<{ sessionId: string; label: string; manifest: Record<string, any> }> = [];
  const onOpen = (e: Event) => opened.push((e as CustomEvent).detail);
  beforeEach(() => { opened.length = 0; window.addEventListener('lab:open-view', onOpen); });
  afterEach(() => window.removeEventListener('lab:open-view', onOpen));

  it('pre-fills the box with the query that found the cards; no history control of its own', () => {
    renderGallery(momentsData([item('m1', 'video'), item('m2', 'video')]));
    expect(screen.getByTestId('moments-search-input')).toHaveValue('night drive');
    expect(cardIds()).toEqual(['mc-m1', 'mc-m2']);
    expect(screen.queryByTestId('moments-search-back')).toBeNull();
    expect(screen.queryByTestId('moments-search-forward')).toBeNull();
    expect(screen.queryByTestId('moments-recent')).toBeNull();
  });

  it("a search opens its results as the tab's next version, named by its query; the tab itself is unchanged", async () => {
    h.searchMoments.mockResolvedValue(ok([wire('s1'), wire('s2'), wire('s3')]));
    renderGallery(momentsData([item('m1', 'video')]));
    fireEvent.change(screen.getByTestId('moments-search-input'), { target: { value: 'rain on windshield' } });
    fireEvent.click(screen.getByTestId('moments-search-submit'));
    expect(screen.getByTestId('moments-search-loading')).toBeInTheDocument();
    await waitFor(() => expect(opened).toHaveLength(1));
    expect(h.searchMoments).toHaveBeenCalledWith('p1', 'rain on windshield');
    const { manifest, label, sessionId } = opened[0];
    expect(sessionId).toBe('sess-1');
    expect(label).toBe('Moments');
    expect(manifest).toMatchObject({ template: 'clip-gallery', title: 'Moments', subjectIds: ['title:Moments'] });
    expect(manifest.galleryMoments).toMatchObject({ project_id: 'p1', basis: 'searched', query: 'rain on windshield',
      queryRef: { type: 'text', q: 'rain on windshield' } });
    expect(manifest.galleryMoments.items.map((m: MomentItem) => m.id).every((id: string) => /^t\d+-s\d$/.test(id))).toBe(true);
    expect(cardIds()).toEqual(['mc-m1']);
    expect(screen.queryByTestId('moments-search-loading')).toBeNull();
  });

  // Staging 2026-10-11: clicking a chip changed nothing -- the chips were only sent with the NEXT search, and never
  // applied to the agent's own results. They now filter the cards on screen, instantly and without a re-fetch.
  it('kind chips filter the cards on screen without re-searching', async () => {
    renderGallery(momentsData([item('m1', 'video'), item('m2', 'image'), item('m3', 'audio')]));
    expect(cardIds()).toHaveLength(3);
    fireEvent.click(screen.getByTestId('moments-kind-image'));
    expect(screen.getByTestId('moments-kind-image')).toHaveAttribute('aria-pressed', 'true');
    await waitFor(() => expect(cardIds()).toHaveLength(1));
    fireEvent.click(screen.getByTestId('moments-kind-audio'));
    await waitFor(() => expect(cardIds()).toHaveLength(2));
    fireEvent.click(screen.getByTestId('moments-kind-image'));
    fireEvent.click(screen.getByTestId('moments-kind-audio'));
    await waitFor(() => expect(cardIds()).toHaveLength(3));
    expect(h.searchMoments).not.toHaveBeenCalled();
  });

  it('a search fetches every kind, and its version opens with the same chips pressed', async () => {
    h.searchMoments.mockResolvedValue(ok([{ ...item('s1', 'video'), id: 's1' }, { ...item('s2', 'image'), id: 's2' }]));
    renderGallery(momentsData([item('m1', 'video')]));
    fireEvent.click(screen.getByTestId('moments-kind-image'));
    fireEvent.click(screen.getByTestId('moments-search-submit'));
    await waitFor(() => expect(opened).toHaveLength(1));
    expect(h.searchMoments).toHaveBeenCalledWith('p1', 'night drive');
    expect(opened[0].manifest.galleryMoments.items).toHaveLength(2);
    expect(opened[0].manifest.galleryMoments.kinds).toEqual(['image']);
    cleanup();
    renderGallery(opened[0].manifest as unknown as ClipGalleryData);
    expect(screen.getByTestId('moments-search-input')).toHaveValue('night drive');
    expect(screen.getByTestId('moments-kind-image')).toHaveAttribute('aria-pressed', 'true');
    expect(cardIds()).toHaveLength(1);
  });

  it('a version that found nothing says so, by its query', () => {
    const data = momentsData([], {});
    (data.galleryMoments as MomentsBlock).cited_count = 0;
    (data.galleryMoments as MomentsBlock).query = 'kawhi dunk';
    renderGallery(data);
    expect(screen.getByTestId('clip-gallery-empty')).toHaveTextContent('No moments match “kawhi dunk”');
  });

  it('shows a failed search above the cards, and retries it', async () => {
    h.searchMoments.mockResolvedValueOnce({ ok: false, kind: 'error', status: 502, code: 'search_failed', message: 'The search did not complete. Try again.' });
    renderGallery(momentsData([item('m1', 'video')]));
    fireEvent.click(screen.getByTestId('moments-search-submit'));
    await waitFor(() => expect(screen.getByTestId('moments-search-error')).toHaveTextContent('The search did not complete.'));
    expect(cardIds()).toEqual(['mc-m1']);
    h.searchMoments.mockResolvedValueOnce(ok([wire('s1')]));
    await act(async () => { fireEvent.click(screen.getByText('Try again')); });
    await waitFor(() => expect(opened).toHaveLength(1));
    expect(screen.queryByTestId('moments-search-error')).toBeNull();
  });
});

// Staging 2026-10-11: PDFs were indexed, but a document hit never reached a card -- the gallery's kind check
// dropped it, there was no Document chip, and no card could show a page.
describe('document (PDF page) moments', () => {
  const doc = (id: string, page: number, over: Partial<MomentItem> = {}) =>
    item(id, 'document', { start_ms: (page - 1) * 10_000 + 2, end_ms: (page - 1) * 10_000 + 3, display_name: 'report.pdf',
      text: 'Rated PG-13 for violence', highlight_terms: ['violence'], source_index: 'scene', ...over });

  it('render as a document card at their page, next to media cards', () => {
    renderGallery(momentsData([item('m1', 'video'), doc('d1', 3)]));
    expect(cardIds()).toEqual(['mc-m1']); // the media card; the document is not a MomentCard
    const card = screen.getByTestId('moment-card-d1');
    expect(card).toHaveAttribute('data-moment-kind', 'document');
    expect(within(card).getByTestId('card-badge-left')).toHaveTextContent('p. 3');
    expect(screen.getByTestId('moment-text-d1').querySelector('mark')).toHaveTextContent('violence');
    // The one card's icons: Preview · Download · Open in side panel.
    expect(within(card).getByRole('button', { name: 'Preview report.pdf' })).toBeInTheDocument();
    expect(within(card).getByRole('button', { name: 'Download report.pdf' })).toBeInTheDocument();
  });

  it('open the PDF at the page through the shared open path', () => {
    h.openAssetAt.mockReset().mockResolvedValue({ ok: true });
    renderGallery(momentsData([doc('d1', 7)]));
    fireEvent.click(screen.getByRole('button', { name: 'Open report.pdf in the side panel' }));
    expect(h.openAssetAt).toHaveBeenCalledWith(
      { ref: { project_id: 'p1', asset_id: 'asset-d1', frame_s3_key: null }, kind: 'document', displayName: 'report.pdf', startMs: 60_002 },
      expect.any(String),
    );
  });

  it('show an open refusal in the card', async () => {
    h.openAssetAt.mockReset().mockResolvedValue({ ok: false, message: "Can't open report.pdf: it was removed from the project." });
    renderGallery(momentsData([doc('d1', 1)]));
    fireEvent.click(screen.getByRole('button', { name: 'Open report.pdf in the side panel' }));
    expect(await screen.findByTestId('moment-refusal-d1')).toHaveTextContent('removed from the project');
  });

  it('Preview opens the PDF at the page in the chat preview', () => {
    renderGallery(momentsData([doc('d1', 4)]));
    fireEvent.click(screen.getByRole('button', { name: 'Preview report.pdf' }));
    expect(screen.getByTestId('asset-preview-document').getAttribute('src')).toBe('https://x#page=4');
    expect(screen.getByTestId('asset-preview-range')).toHaveTextContent('Page 4');
  });

  it('have a Document chip that filters to them', async () => {
    renderGallery(momentsData([item('m1', 'video'), doc('d1', 2)]));
    fireEvent.click(screen.getByTestId('moments-kind-document'));
    expect(screen.getByTestId('moments-kind-document')).toHaveTextContent('Document');
    await waitFor(() => expect(cardIds()).toEqual([]));
    expect(screen.getByTestId('moment-card-d1')).toBeInTheDocument();
  });

  it('hide their text when access is lost', () => {
    h.state.mockReturnValue({ phase: 'no_access' });
    renderGallery(momentsData([doc('d1', 2)]));
    expect(screen.getByTestId('moment-no-access-d1')).toBeInTheDocument();
    expect(screen.queryByTestId('moment-text-d1')).toBeNull();
  });
});
