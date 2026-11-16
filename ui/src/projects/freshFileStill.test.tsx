// @vitest-environment jsdom
// Staging 2026-11-15: three clips cut a moment ago showed grey cards until the page was reloaded. Their file was not
// registered yet when the card first asked (the location → asset lookup cached that miss for the life of the page),
// and their still did not exist yet (asked once). Both are now asked again on a short backoff.
import '@testing-library/jest-dom';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';

const h = vi.hoisted(() => ({ request: vi.fn(), resolveLocations: vi.fn() }));
vi.mock('./assetUrlBatcher', () => ({ request: h.request }));
vi.mock('./api', () => ({ resolveLocations: h.resolveLocations }));

import { useHeroStill } from './heroStill';
import { FRESH_FILE_RETRY_MS as STILL_RETRY_MS } from './freshFile';
import { useLocationAssets, _resetLocationAssets } from './useLocationAssets';

const tick = async (ms: number) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); };
const entry = (rendition?: string) => new Map([['video:1', { ok: true, entry: { source: { url: 'https://s/v.mp4' }, ...(rendition ? { rendition: { url: rendition } } : {}) } }]]);

beforeEach(() => { vi.useFakeTimers(); h.request.mockReset(); h.resolveLocations.mockReset(); _resetLocationAssets(); });
afterEach(() => vi.useRealTimers());

describe('useHeroStill — a video made a moment ago', () => {
  it('asks again until its still exists, then stops', async () => {
    h.request.mockResolvedValueOnce(entry()).mockResolvedValueOnce(entry()).mockResolvedValue(entry('https://s/rendition.jpg'));
    const { result } = renderHook(() => useHeroStill('p1', 'video:1', 'video'));
    await tick(0);
    expect(result.current.still).toBeNull();
    await tick(STILL_RETRY_MS[0] + STILL_RETRY_MS[1]);
    expect(result.current.still).toBe('https://s/rendition.jpg');
    const calls = h.request.mock.calls.length;
    await tick(120_000);
    expect(h.request.mock.calls.length).toBe(calls); // found: no more asking
  });
  it('never shows a video\'s own bytes, and gives up after the backoff', async () => {
    h.request.mockResolvedValue(entry());
    const { result } = renderHook(() => useHeroStill('p1', 'video:1', 'video'));
    await tick(120_000);
    expect(result.current.still).toBeNull();
    expect(h.request).toHaveBeenCalledTimes(STILL_RETRY_MS.length + 1);
  });
  it('a refusal (gone, no access) is final', async () => {
    h.request.mockResolvedValue(new Map([['video:1', { ok: false, kind: 'not_found' }]]));
    renderHook(() => useHeroStill('p1', 'video:1', 'video'));
    await tick(120_000);
    expect(h.request).toHaveBeenCalledTimes(1);
  });
});

describe('useLocationAssets — a file registered a moment after its URL is shown', () => {
  const URL = 'https://cdn/projects/p1/sessions/s/output/clip.mp4';
  it('a miss is asked again, and the card gets the asset once it is registered', async () => {
    h.resolveLocations.mockResolvedValueOnce({ ok: true, value: {} }).mockResolvedValue({ ok: true, value: { [URL]: 'video:9' } });
    const { result } = renderHook(() => useLocationAssets('p1', [URL]));
    await tick(0);
    expect(result.current).toEqual({ [URL]: null });
    await tick(MISS_RETRY_FIRST);
    expect(result.current).toEqual({ [URL]: 'video:9' });
    await tick(120_000);
    expect(h.resolveLocations).toHaveBeenCalledTimes(2);
  });
});
const MISS_RETRY_FIRST = 2_000;
