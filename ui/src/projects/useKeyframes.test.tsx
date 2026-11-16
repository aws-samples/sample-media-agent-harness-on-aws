// @vitest-environment jsdom
// A timeline snaps a drag onto its file's keyframes, however the timeline names that file: an asset player names its
// asset; an agent-made timeline names only its location (staging: the Meridian timeline, whose drags never snapped,
// so two of three clips were re-encoded instead of copied).
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';

const h = vi.hoisted(() => ({ getKeyframes: vi.fn(), resolveLocations: vi.fn() }));
vi.mock('./api', () => ({ getKeyframes: h.getKeyframes, resolveLocations: h.resolveLocations }));

import { useTimelineKeyframes, _resetKeyframes } from './useKeyframes';
import { _resetLocationAssets } from './useLocationAssets';

beforeEach(() => {
  _resetKeyframes(); _resetLocationAssets();
  h.getKeyframes.mockReset().mockResolvedValue({ ok: true, value: [0, 259.193, 472.206] });
  h.resolveLocations.mockReset().mockResolvedValue({ ok: true, value: { 's3://b/projects/p1/video:9/meridian.mp4': 'video:9' } });
});

describe('useTimelineKeyframes', () => {
  it('by the asset the manifest names', async () => {
    const { result } = renderHook(() => useTimelineKeyframes('p1', { sourceAssetId: 'video:1' }));
    await waitFor(() => expect(result.current).toEqual([0, 259.193, 472.206]));
    expect(h.getKeyframes).toHaveBeenCalledWith('p1', 'video:1');
    expect(h.resolveLocations).not.toHaveBeenCalled();
  });
  it('by its location, when that is all an agent-made timeline names', async () => {
    const { result } = renderHook(() => useTimelineKeyframes('p1', { sourceLocation: 's3://b/projects/p1/video:9/meridian.mp4' }));
    await waitFor(() => expect(result.current).toEqual([0, 259.193, 472.206]));
    expect(h.getKeyframes).toHaveBeenCalledWith('p1', 'video:9');
  });
  it('no project, or no file named: nothing asked, nothing to snap to', () => {
    expect(renderHook(() => useTimelineKeyframes(null, { sourceAssetId: 'video:1' })).result.current).toEqual([]);
    expect(renderHook(() => useTimelineKeyframes('p1', { template: 'nle-timeline' })).result.current).toEqual([]);
    expect(h.getKeyframes).not.toHaveBeenCalled();
  });
});
