// @vitest-environment jsdom
//
// Image queries in the Moments tab (mock search-and-actions.html §1): the bar holds words OR an image, never both
// (M2C searches one kind per call). Image mode: the field becomes the image, with no text input; "Search by words"
// or ✕ goes back to an empty, focused box. An image search opens as the tab's next version, like a words search.
import { describe, it, expect, vi, beforeEach } from 'vitest';
import '@testing-library/jest-dom';
import { render, screen, fireEvent, within, waitFor, act } from '@testing-library/react';
import React from 'react';

import ClipGallery from './ClipGallery';
import { theme } from '../../../styles/tokens';
import type { MomentItem, MomentsBlock, MomentsQuery, ClipGalleryData } from '../types';

const h = vi.hoisted(() => ({
  searchByImages: vi.fn(),
  searchText: vi.fn(),
  upload: vi.fn(),
  searchAssets: vi.fn(),
}));
vi.mock('../../../projects/api', async (orig) => ({ ...(await orig<typeof import('../../../projects/api')>()),
  searchMomentsByImages: h.searchByImages, searchMoments: h.searchText, searchAssets: h.searchAssets }));
vi.mock('../../../projects/assetUrlBatcher', () => ({ request: vi.fn(async () => new Map()), requestFrames: vi.fn(async () => new Map()) }));
vi.mock('../../../projects/upload/queryImage', async (orig) => ({
  ...(await orig<typeof import('../../../projects/upload/queryImage')>()), uploadQueryImage: h.upload }));
vi.mock('../../../projects/useMomentUrls', () => ({
  useMomentUrls: () => ({ state: () => ({ phase: 'ready', url: 'https://x' }), onLoadStart: vi.fn(), onLoadOk: vi.fn(), onLoadError: vi.fn(), freshUrlFor: vi.fn() }),
  URL_MAX_AGE_MS: 3_000_000,
}));
vi.mock('../../../services/activeSession', () => ({ getActiveSessionId: () => 'sess-1' }));
vi.mock('../../../projects/sessionProjectStore', () => ({ getSessionProject: () => ({ activeProjectId: 'p1' }) }));
vi.mock('./MomentCard', () => ({
  default: (props: { moment: MomentItem; badges?: React.ReactNode; onFindSimilar?: (m: MomentItem) => void }) => (
    <div data-testid={`mc-${props.moment.id}`}>{props.badges}
      <button data-testid={`fs-${props.moment.id}`} onClick={() => props.onFindSimilar?.(props.moment)}>find similar</button>
    </div>
  ),
}));

const PID = 'p1';
const IMG = 'image:11111111-1111-4111-8111-111111111111';
const IMG2 = 'image:33333333-3333-4333-8333-333333333333';
const VID = 'video:22222222-2222-4222-8222-222222222222';

const item = (id: string, over: Partial<MomentItem> = {}): MomentItem => ({
  id, ref: { project_id: PID, asset_id: VID, frame_s3_key: null }, kind: 'video', display_name: id, start_ms: 0, end_ms: 4000,
  source_index: null, final_score: 0.5, text: '', text_truncated: false, highlight_terms: [], citation: null, ...over,
});

function data(block: Partial<MomentsBlock>, viewState?: unknown): ClipGalleryData {
  return { galleryMoments: { v: 1, project_id: PID, turn_id: 't', basis: 'searched', truncated: false, items: [item('m1')], ...block },
    ...(viewState ? { viewState, onViewStateChange: vi.fn() } : {}) } as unknown as ClipGalleryData;
}

const ok = (items: unknown[] = []) => ({ ok: true, value: { query: '', kinds: null, items, total: items.length } });

beforeEach(() => {
  h.searchByImages.mockReset().mockResolvedValue(ok());
  h.searchText.mockReset().mockResolvedValue(ok());
  h.searchAssets.mockReset().mockResolvedValue({ ok: true, value: { assets: [
    { assetId: IMG2, displayName: 'dock.jpg', kind: 'image', status: 'indexed' },
    { assetId: IMG, displayName: 'broken.jpg', kind: 'image', status: 'failed' },
  ], truncated: false } });
  h.upload.mockReset().mockResolvedValue({ ok: true, assetId: IMG2, name: 'dock.jpg' });
});

const imageRef: MomentsQuery = { type: 'image', match: 'any', images: [{ assetId: IMG, label: 'still.jpg' }] };

async function pickFile() {
  const input = screen.getByTestId('search-by-image-file') as HTMLInputElement;
  await act(async () => { fireEvent.change(input, { target: { files: [new File(['x'], 'dock.jpg', { type: 'image/jpeg' })] } }); });
}

describe('image query chips', () => {
  it("image mode: the chat's image search arrives as the field itself, with no text input to type into", () => {
    render(<ClipGallery data={data({ queryRef: imageRef })} title="" theme={theme} />);
    expect(screen.getByTestId('moments-search-field')).toHaveAttribute('data-mode', 'image');
    expect(screen.getByTestId('query-image-chip')).toHaveTextContent('still.jpg');
    expect(screen.getByTestId('query-image-chip')).toHaveTextContent('similar moments');
    expect(screen.queryByTestId('moments-search-input')).toBeNull();
    expect(screen.queryByTestId('query-match-any')).toBeNull();
  });

  it('"Search by words" and ✕ go back to an empty, focused box', () => {
    render(<ClipGallery data={data({ queryRef: imageRef })} title="" theme={theme} />);
    fireEvent.click(screen.getByTestId('query-search-words'));
    const input = screen.getByTestId('moments-search-input') as HTMLInputElement;
    expect(screen.getByTestId('moments-search-field')).toHaveAttribute('data-mode', 'words');
    expect(input.value).toBe('');
    expect(document.activeElement).toBe(input);
    expect(screen.queryByTestId('query-image-chip')).toBeNull();
  });

  it('one image per search: the chip menu offers a different image, never "Add"', () => {
    render(<ClipGallery data={data({ queryRef: imageRef })} title="" theme={theme} />);
    fireEvent.click(screen.getByTestId('query-image-chip'));
    const menu = screen.getByTestId('query-image-menu');
    expect(within(menu).queryByText(/Add/)).toBeNull();
    expect(within(menu).getByText(/Use a different image/)).toBeInTheDocument();
  });

  it('Use a different image… is a new search by the new image alone', async () => {
    render(<ClipGallery data={data({ queryRef: imageRef })} title="" theme={theme} />);
    fireEvent.click(screen.getByTestId('query-image-chip'));
    fireEvent.click(within(screen.getByTestId('query-image-menu')).getByText(/Use a different image/));
    await pickFile();
    expect(h.upload).toHaveBeenCalledWith(PID, 'sess-1', expect.any(File));
    expect(h.searchByImages).toHaveBeenLastCalledWith(PID, [{ assetId: IMG2 }], 'sess-1'); // the chat id: a missing rendition is made on demand
    expect(screen.getAllByTestId('query-image-chip')).toHaveLength(1);
    expect(screen.getByTestId('query-image-chip')).toHaveTextContent('dock.jpg');
  });

  it('words mode: the search-by-image button (a themed icon, not an emoji) switches the bar to that image', async () => {
    render(<ClipGallery data={data({ queryRef: imageRef })} title="" theme={theme} />);
    fireEvent.click(screen.getByTestId('query-image-remove')); // to words
    const btn = screen.getByTestId('search-by-image');
    expect(btn.textContent).not.toMatch(/📷/);
    expect(btn.querySelector('svg')).not.toBeNull();
    fireEvent.click(btn);
    await pickFile();
    expect(h.searchByImages).toHaveBeenLastCalledWith(PID, [{ assetId: IMG2 }], 'sess-1');
    expect(screen.getByTestId('moments-search-field')).toHaveAttribute('data-mode', 'image');
    expect(screen.getByTestId('query-image-chip')).toHaveTextContent('dock.jpg');
  });

  it("the popover is the + menu's layout: upload, then Recent images in the project; picking one searches with it", async () => {
    render(<ClipGallery data={data({})} title="" theme={theme} />);
    fireEvent.click(screen.getByTestId('search-by-image'));
    expect(screen.getByTestId('search-by-image-drop')).toHaveTextContent('Upload an image');
    const items = await screen.findAllByTestId('plus-menu-recent-asset');
    expect(items).toHaveLength(1); // a failed image is not offered
    expect(items[0]).toHaveTextContent('dock.jpg');
    expect(screen.getByText(/^Recent images in /)).toBeInTheDocument();
    expect(screen.queryByText('View all')).toBeNull(); // no Projects_Surface to open from here
    expect(h.searchAssets).toHaveBeenLastCalledWith(PID, { kind: 'image' });
    fireEvent.click(items[0]);
    expect(h.upload).not.toHaveBeenCalled();
    expect(h.searchByImages).toHaveBeenLastCalledWith(PID, [{ assetId: IMG2 }], 'sess-1');
    expect(screen.getByTestId('query-image-chip')).toHaveTextContent('dock.jpg');
  });

  it('the project images filter by name', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      render(<ClipGallery data={data({})} title="" theme={theme} />);
      fireEvent.click(screen.getByTestId('search-by-image'));
      fireEvent.change(await screen.findByTestId('plus-menu-asset-search'), { target: { value: 'dock' } });
      await act(async () => { vi.advanceTimersByTime(300); });
      await waitFor(() => expect(h.searchAssets).toHaveBeenLastCalledWith(PID, { kind: 'image', q: 'dock' }));
    } finally { vi.useRealTimers(); }
  });

  it('typing then searching by words is a plain text search — nothing asks to replace an image', () => {
    render(<ClipGallery data={data({ queryRef: imageRef })} title="" theme={theme} />);
    fireEvent.click(screen.getByTestId('query-search-words'));
    fireEvent.change(screen.getByTestId('moments-search-input'), { target: { value: 'dock at dawn' } });
    fireEvent.submit(screen.getByRole('search'));
    expect(screen.queryByTestId('moments-confirm-text')).toBeNull();
    expect(h.searchText).toHaveBeenCalledWith(PID, 'dock at dawn');
  });

  it('an image search opens as the next version, named by the image; there is no recent-searches clock', async () => {
    const opened: Array<{ manifest: Record<string, any> }> = [];
    const onOpen = (e: Event) => opened.push((e as CustomEvent).detail);
    window.addEventListener('lab:open-view', onOpen);
    render(<ClipGallery data={data({ queryRef: imageRef })} title="" theme={theme} />);
    expect(screen.queryByTestId('moments-recent')).toBeNull();
    fireEvent.click(screen.getByTestId('query-image-chip'));
    fireEvent.click(within(screen.getByTestId('query-image-menu')).getByText(/Use a different image/));
    await pickFile();
    await waitFor(() => expect(opened).toHaveLength(1));
    window.removeEventListener('lab:open-view', onOpen);
    expect(opened[0].manifest.galleryMoments.queryRef).toEqual({ type: 'image', match: 'any', images: [{ assetId: IMG2, label: 'dock.jpg' }] });
    const { viewVersionNote } = await import('../../../shared/renderSubject');
    expect(viewVersionNote(opened[0].manifest)).toBe('dock.jpg');
  });
});

describe('back-compat', () => {
  it("a tab saved with the old in-tab search history shows its own cards and searches nothing", () => {
    render(<ClipGallery data={data({ query: 'title card' }, { search: { history: [{ q: 'dock' }], index: 1 } })} title="" theme={theme} />);
    expect(h.searchText).not.toHaveBeenCalled();
    expect(screen.getByTestId('moments-search-input')).toHaveValue('title card');
    expect(screen.getByTestId('mc-m1')).toBeInTheDocument();
  });

  it('a manifest with only the old text query still pre-fills the box', () => {
    render(<ClipGallery data={data({ query: 'title card' })} title="" theme={theme} />);
    expect(screen.getByTestId('moments-search-input')).toHaveValue('title card');
  });
});
