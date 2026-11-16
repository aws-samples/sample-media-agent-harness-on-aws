// @vitest-environment jsdom
//
// Unit tests for ClipGallery's Moments mode and the `readMoments` validator (Component 7,
// media-harness-search-qa-rendering; Req 3.8, 3.9, 3.12, 3.13, 3.14, 3.16, 3.20, 3.21, 3.26).
//
// The legacy clip path is covered unchanged by ClipGallery.test.tsx; this file exercises the Moments
// branch. The URL hook, MomentCard, and the session/project seams are mocked so the test drives the
// gallery's bulk-action membership, URL minting at action time, and the audio "Clip all marked" verb
// without the real card, batcher, or network.
import { describe, it, expect, vi, beforeEach } from 'vitest';
import '@testing-library/jest-dom';
import { render, screen, fireEvent } from '@testing-library/react';
import React from 'react';

import ClipGallery, { readMoments, MOMENTS_MAX, MOMENTS_PAGE } from './ClipGallery';
import { theme } from '../../../styles/tokens';
import type { MomentItem, MomentsBlock, ClipGalleryData } from '../types';
import { pendingMomentClips, __resetMomentClipAutoOpenForTests } from '../../../services/momentClipAutoOpen';

// --- hoisted mock state the factories below close over ---------------------------------------------
const h = vi.hoisted(() => ({
  state: vi.fn(),
  freshUrlFor: vi.fn(),
  findSimilar: vi.fn(),
  searchByImages: vi.fn(async () => ({ ok: true, value: { query: '', kinds: null, items: [], total: 0 } })),
  blocks: [] as { items: { id: string }[] }[],
}));

vi.mock('../../../projects/api', async (orig) => ({ ...(await orig<typeof import('../../../projects/api')>()),
  searchMomentsByImages: h.searchByImages }));
vi.mock('../../../projects/assetUrlBatcher', () => ({ request: vi.fn(async () => new Map()), requestFrames: vi.fn(async () => new Map()) }));

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
    onClip?: (m: MomentItem, range: { in: number; out: number }) => void;
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
        {props.onClip && (
          <button data-testid={`clip-${props.moment.id}`} onClick={() => props.onClip!(props.moment, { in: 56, out: 60 })}>
            clip
          </button>
        )}
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

function momentsData(items: MomentItem[]): ClipGalleryData {
  const galleryMoments: MomentsBlock = {
    v: 1,
    project_id: 'p1',
    turn_id: 't1',
    basis: 'cited',
    truncated: false,
    items,
  };
  return { galleryMoments };
}

beforeEach(() => {
  vi.clearAllMocks();
  h.state.mockReturnValue({ phase: 'ready', url: 'https://x', obtainedAt: 0 });
  h.freshUrlFor.mockImplementation(async (id: string) => `https://fresh/${id}`);
});

describe('readMoments', () => {
  it('returns null for a legacy manifest (no galleryMoments) so the legacy path runs (Req 3.16)', () => {
    expect(readMoments({ galleryClips: [{ id: 'c1', label: 'C', src: 'https://x/c.mp4', kind: 'video' }] })).toBeNull();
    expect(readMoments({})).toBeNull();
  });

  it('keeps only well-formed items and drops the rest (Req 3.9)', () => {
    const good = item('ok1', 'video');
    const data = momentsData([
      good,
      item('badproj', 'video', { ref: { project_id: 'other', asset_id: 'a', frame_s3_key: null } }),
      { ...item('badkind', 'video'), kind: 'doc' as unknown as MomentItem['kind'] },
      { ...item('badspan', 'video'), start_ms: 10, end_ms: 10 },
      { ...item('badname', 'video'), display_name: '' },
    ]);
    const block = readMoments(data)!;
    expect(block).not.toBeNull();
    expect(block.items.map((m) => m.id)).toEqual(['ok1']);
  });

  it('caps the kept items at MOMENTS_MAX (200)', () => {
    expect(MOMENTS_MAX).toBe(200);
    const many = Array.from({ length: MOMENTS_MAX + 10 }, (_, i) => item(`m${i}`, 'video'));
    const block = readMoments(momentsData(many))!;
    expect(block.items).toHaveLength(MOMENTS_MAX);
  });
});

describe('ClipGallery Moments paging (2026-10-10: every search result, paged)', () => {
  beforeEach(() => { h.blocks.length = 0; h.state.mockReturnValue({ phase: 'minting' }); });

  it('shows one page, then Load more reveals the next page and counts what remains', () => {
    const many = Array.from({ length: 60 }, (_, i) => item(`m${i}`, 'video'));
    render(<ClipGallery data={momentsData(many)} title="" theme={theme} />);
    expect(screen.getAllByTestId(/^mc-/)).toHaveLength(MOMENTS_PAGE);
    expect(screen.getByTestId('moments-count')).toHaveTextContent(`Showing ${MOMENTS_PAGE} of 60`);
    const more = screen.getByTestId('moments-load-more');
    expect(more).toHaveTextContent(`${60 - MOMENTS_PAGE} remaining`);
    fireEvent.click(more);
    expect(screen.getAllByTestId(/^mc-/)).toHaveLength(MOMENTS_PAGE * 2);
    fireEvent.click(screen.getByTestId('moments-load-more'));
    expect(screen.getAllByTestId(/^mc-/)).toHaveLength(60);
    expect(screen.queryByTestId('moments-load-more')).toBeNull();
  });

  it('mints URLs only for the shown cards', () => {
    const many = Array.from({ length: 60 }, (_, i) => item(`m${i}`, 'video'));
    render(<ClipGallery data={momentsData(many)} title="" theme={theme} />);
    expect(h.blocks[h.blocks.length - 1].items.map((i: { id: string }) => i.id)).toEqual(many.slice(0, MOMENTS_PAGE).map(i => i.id));
    fireEvent.click(screen.getByTestId('moments-load-more'));
    expect(h.blocks[h.blocks.length - 1].items).toHaveLength(MOMENTS_PAGE * 2);
  });

  it('a single page has no pager and no count line', () => {
    render(<ClipGallery data={momentsData([item('m1', 'video'), item('m2', 'video')])} title="" theme={theme} />);
    expect(screen.queryByTestId('moments-load-more')).toBeNull();
    expect(screen.queryByTestId('moments-count')).toBeNull();
  });
});

describe('ClipGallery Moments mode', () => {
  it('renders a MomentCard per item under the Moments title', () => {
    render(<ClipGallery data={momentsData([item('m1', 'audio'), item('m2', 'audio')])} title="" theme={theme} />);
    expect(screen.getByTestId('clip-gallery')).toBeInTheDocument();
    expect(screen.getByText('Matched moments')).toBeInTheDocument();
    expect(screen.getByTestId('mc-m1')).toBeInTheDocument();
    expect(screen.getByTestId('mc-m2')).toBeInTheDocument();
  });

  it('shows an empty state when the block has no items', () => {
    render(<ClipGallery data={momentsData([])} title="" theme={theme} />);
    expect(screen.getByTestId('clip-gallery-empty')).toHaveTextContent('No moments to review');
  });

  it('shows ONLY Play all in sequence — the bulk clip/frame/crop actions are gone (ISSUE 4d)', () => {
    render(<ClipGallery data={momentsData([item('m1', 'audio')])} title="" theme={theme} />);
    expect(screen.getByTestId('play-all')).toHaveTextContent('Play all in sequence');
    expect(screen.queryByTestId('trim-all-marked')).toBeNull();
    expect(screen.queryByTestId('extract-frames')).toBeNull();
    expect(screen.queryByTestId('crop-all')).toBeNull();
  });

  it('keeps no bulk clip/frame/crop buttons even when a video moment is present (ISSUE 4d)', () => {
    render(<ClipGallery data={momentsData([item('v1', 'video')])} title="" theme={theme} />);
    expect(screen.getByTestId('play-all')).toBeInTheDocument();
    expect(screen.queryByTestId('trim-all-marked')).toBeNull();
    expect(screen.queryByTestId('extract-frames')).toBeNull();
    expect(screen.queryByTestId('crop-all')).toBeNull();
  });

  it('a card’s Find similar searches in this tab by its frame, never as a chat turn (design 2026-10-18 §2)', () => {
    const v = 'video:11111111-1111-4111-8111-111111111111';
    render(<ClipGallery data={momentsData([item('v1', 'video', { display_name: 'trailer.mp4', start_ms: 12_000, end_ms: 16_000,
      ref: { project_id: 'p1', asset_id: v, frame_s3_key: 'projects/f.jpg' } })])} title="" theme={theme} />);
    fireEvent.click(screen.getByTestId('fs-v1'));
    expect(h.findSimilar).not.toHaveBeenCalled();
    expect(h.searchByImages).toHaveBeenCalledWith('p1', [{ frameKey: 'projects/f.jpg', sourceAssetId: v }], 'sess-1');
    expect(screen.getByTestId('query-image-chip')).toHaveTextContent('Frame 00:12 · trailer.mp4');
  });
});

describe('ClipGallery Moments face-lift (Oct 9: uneven, busy cards)', () => {
  it('stretches every card to the row height', () => {
    render(<ClipGallery data={momentsData([item('a', 'video'), item('b', 'image'), item('c', 'audio')])} title="" theme={theme} />);
    expect(screen.getByTestId('moments-grid').style.alignItems).toBe('stretch');
  });

  it('a card\'s Preview opens the chat preview playing ONLY its range', () => {
    render(<ClipGallery data={momentsData([item('v1', 'video', { display_name: 'nba.mp4' })])} title="" theme={theme} />);
    fireEvent.click(screen.getByTestId('preview-v1'));
    expect(screen.getByRole('dialog', { name: 'Preview of nba.mp4' })).toBeInTheDocument();
    expect(screen.getByTestId('asset-preview-video')).toHaveAttribute('src', 'https://x');
    expect(screen.getByTestId('asset-preview-range')).toBeInTheDocument();
  });

  it('Play all in sequence is the preview over a queue: each range in turn, advancing when one ends; images are skipped', () => {
    render(<ClipGallery data={momentsData([item('a', 'video'), item('b', 'image'), item('c', 'audio')])} title="" theme={theme} />);
    fireEvent.click(screen.getByTestId('play-all'));
    expect(screen.getByTestId('asset-preview')).toHaveTextContent('Playing all · 1 of 2');
    expect(screen.getByTestId('preview-a')).toHaveAttribute('data-playing', 'true');
    expect(screen.getByTestId('play-all')).toHaveTextContent('Stop');
    fireEvent(screen.getByTestId('asset-preview-video'), new Event('ended'));
    expect(screen.getByTestId('asset-preview')).toHaveTextContent('Playing all · 2 of 2');
    expect(screen.getByTestId('asset-preview-audio')).toBeInTheDocument();
    fireEvent(screen.getByTestId('asset-preview-audio'), new Event('ended'));
    expect(screen.queryByTestId('asset-preview')).toBeNull();
    expect(screen.getByTestId('play-all')).toHaveTextContent('Play all in sequence');
  });

  it('draws the bulk bar with SVG icons, no glyph characters', () => {
    render(<ClipGallery data={momentsData([item('a', 'video')])} title="" theme={theme} />);
    for (const id of ['play-all']) {
      const btn = screen.getByTestId(id);
      expect(btn.querySelector('svg')).not.toBeNull();
      expect(btn.textContent).not.toMatch(/[\u25B6\u23F8\u2702\u25A2\u{1F39E}]/u);
    }
  });
});

describe('ClipGallery Moments: a card\'s Clip icon (2026-10-11)', () => {
  beforeEach(() => __resetMomentClipAutoOpenForTests());
  it('sends the SAME one-clip clip-batch action "Clip all marked" composes, with the card\'s ready URL', () => {
    const onInteraction = vi.fn();
    render(<ClipGallery data={momentsData([item('v1', 'video', { display_name: 'nba.mp4' })])} title="" theme={theme} onInteraction={onInteraction} />);
    fireEvent.click(screen.getByTestId('clip-v1'));
    expect(onInteraction).toHaveBeenCalledTimes(1);
    const ev = onInteraction.mock.calls[0][0];
    expect(ev).toMatchObject({ type: 'action', target: 'clip-batch' });
    expect(ev.data.verb).toBe('clip');
    expect(ev.data.prompt).toContain('Clip "nba.mp4"');
    expect(ev.data.prompt).toContain('00:56.000 to 01:00.000');
    expect(ev.data.clips).toEqual([{
      clipId: 'v1', label: 'nba.mp4', src: 'https://x', in: 56, out: 60,
      ref: { project_id: 'p1', asset_id: 'asset-v1', frame_s3_key: null }, kind: 'video',
    }]);    // ...and records the pending auto-open entry its job will consume.
    expect(pendingMomentClips()).toEqual([expect.objectContaining({ sessionId: 'sess-1', label: 'nba.mp4' })]);
  });

  it('uses the trim verb for audio', () => {
    const onInteraction = vi.fn();
    render(<ClipGallery data={momentsData([item('a1', 'audio')])} title="" theme={theme} onInteraction={onInteraction} />);
    fireEvent.click(screen.getByTestId('clip-a1'));
    expect(onInteraction.mock.calls[0][0]).toMatchObject({ target: 'trim-batch', data: { verb: 'trim' } });
  });

  it('sends nothing while the card\'s media is not ready, and offers no Clip without onInteraction', () => {
    h.state.mockReturnValue({ phase: 'minting' });
    const onInteraction = vi.fn();
    const { unmount } = render(<ClipGallery data={momentsData([item('v1', 'video')])} title="" theme={theme} onInteraction={onInteraction} />);
    fireEvent.click(screen.getByTestId('clip-v1'));
    expect(onInteraction).not.toHaveBeenCalled();
    expect(pendingMomentClips()).toHaveLength(0);
    unmount();
    render(<ClipGallery data={momentsData([item('v2', 'video')])} title="" theme={theme} />);
    expect(screen.queryByTestId('clip-v2')).toBeNull();
  });
});
