import { describe, it, expect } from 'vitest';
import { mergeViewState, normalizeViewState, sameViewState, viewStatePatch } from './renderViewState';

describe('renderViewState', () => {
  it('normalize keeps only valid fields', () => {
    expect(normalizeViewState({ range: { start: 1, end: 2 }, frameMs: -5, crop: { x: 0, y: 0, w: 2, h: 1 }, zoom: 3 }))
      .toEqual({ range: { start: 1, end: 2 } });
    expect(normalizeViewState({ frameMs: 'x' })).toBeUndefined();
    expect(normalizeViewState(null)).toBeUndefined();
    expect(normalizeViewState([])).toBeUndefined();
  });

  it('merge sets, clears (null) and keeps untouched fields', () => {
    const r = mergeViewState({ range: { start: 1, end: 2 }, frameMs: 500 }, { frameMs: null, crop: { x: 0.1, y: 0.2, w: 0.3, h: 0.4 } });
    expect(r).toEqual({ ok: true, viewState: { range: { start: 1, end: 2 }, crop: { x: 0.1, y: 0.2, w: 0.3, h: 0.4 } } });
  });

  it('merge rejects an invalid or unknown field whole', () => {
    expect(mergeViewState({}, { range: { start: 3, end: 3 } }).ok).toBe(false);
    expect(mergeViewState({}, { crop: { x: 0.9, y: 0, w: 0.2, h: 0.5 } }).ok).toBe(false);
    expect(mergeViewState({}, { frameMs: 1, zoom: 2 }).ok).toBe(false);
    expect(mergeViewState({}, 'nope').ok).toBe(false);
  });

  it('patch body names every field so an absent one is cleared', () => {
    expect(viewStatePatch({ frameMs: 10 })).toEqual({ range: null, frameMs: 10, crop: null, search: null });
  });

  it('sameViewState compares by value, absent ≡ empty', () => {
    expect(sameViewState(undefined, {})).toBe(true);
    expect(sameViewState({ range: { start: 1, end: 2 } }, { range: { start: 1, end: 2 } })).toBe(true);
    expect(sameViewState({ frameMs: 1 }, { frameMs: 2 })).toBe(false);
  });
});

describe('viewState.search (the Moments tab search history)', () => {
  it('keeps a valid history and position', () => {
    const search = { history: [{ q: 'night drive' }, { q: 'rain', kinds: ['video', 'video'] }], index: 2 };
    expect(normalizeViewState({ search })).toEqual({ search: { history: [{ q: 'night drive' }, { q: 'rain', kinds: ['video'] }], index: 2 } });
    const merged = mergeViewState({ range: { start: 1, end: 2 } }, { search: { history: [{ q: 'x' }], index: 0 } });
    expect(merged).toEqual({ ok: true, viewState: { range: { start: 1, end: 2 }, search: { history: [{ q: 'x' }], index: 0 } } });
    expect(mergeViewState({ search }, { search: null })).toEqual({ ok: true, viewState: {} });
  });

  it('rejects a malformed or oversized history', () => {
    const bad = [
      { history: [{ q: 'x' }], index: 2 },
      { history: [{ q: '' }], index: 0 },
      { history: [{ q: 'x'.repeat(201) }], index: 0 },
      { history: [{ q: 'x', kinds: ['pdf'] }], index: 1 },
      { history: Array.from({ length: 11 }, () => ({ q: 'x' })), index: 0 },
      { history: 'x', index: 0 },
    ];
    for (const search of bad) {
      expect(normalizeViewState({ search })).toBeUndefined();
      expect(mergeViewState(undefined, { search }).ok).toBe(false);
    }
  });

  it('a full history stays under the 4 KB PATCH cap', () => {
    const search = { history: Array.from({ length: 10 }, () => ({ q: 'q'.repeat(200), kinds: ['video', 'image'] as ('video' | 'image')[] })), index: 10 };
    expect(JSON.stringify({ viewState: viewStatePatch({ search }) }).length).toBeLessThan(4 * 1024);
  });
});

describe('renderViewState image searches (design 2026-10-18 §2)', () => {
  const IMG = 'image:11111111-1111-4111-8111-111111111111';
  const VID = 'video:22222222-2222-4222-8222-222222222222';
  it('keeps an image search of 1..3 images (asset or frame), beside text searches', () => {
    const search = { history: [{ q: 'dock' }, { images: [{ assetId: IMG, label: 'still.jpg' },
      { frameKey: 'micro-workflow/x/f.jpg', sourceAssetId: VID, atMs: 12000, label: 'Frame 00:12' }], kinds: ['video'] }], index: 2 };
    expect(normalizeViewState({ search })).toEqual({ search });
  });
  it('drops a malformed image search (and with it the whole search field)', () => {
    for (const bad of [
      { images: [] }, { images: [{ assetId: IMG }] }, { images: [{ assetId: 'nope', label: 'x' }] },
      { images: [{ frameKey: 'a?b', sourceAssetId: VID, label: 'x' }] }, { images: [{ frameKey: 'k', label: 'x' }] },
      { q: 'x', images: [{ assetId: IMG, label: 'x' }] },
      { images: Array.from({ length: 4 }, () => ({ assetId: IMG, label: 'x' })) },
    ]) {
      expect(normalizeViewState({ search: { history: [bad], index: 1 } })).toBeUndefined();
    }
  });
});
