// A drag lands on a snap target (a shot boundary, a keyframe) within a few PIXELS, as in any editor. A fixed 0.4 s
// was under a pixel on a 9-minute timeline, so nothing ever snapped at the zoom people work at.
import { describe, it, expect } from 'vitest';
import { snapTime } from './TimelineEditor';

describe('snapTime', () => {
  const keyframes = [0, 3.3, 6.6, 228.762095];
  it('zoomed out (9 min across 600 px): lands on a keyframe a second or more away', () => {
    expect(snapTime(230.0, keyframes, 600, 561)).toBe(228.762095); // 8 px ≈ 7.5 s here
  });
  it('zoomed in (9 min across 60,000 px): only a frame or two away snaps; elsewhere the mark stays put', () => {
    expect(snapTime(228.80, keyframes, 60_000, 561)).toBe(228.762095); // 8 px ≈ 0.075 s
    expect(snapTime(229.0, keyframes, 60_000, 561)).toBe(229.0);
  });
  it('nothing to snap to, or nothing measured: the time as is', () => {
    expect(snapTime(5, [], 600, 561)).toBe(5);
    expect(snapTime(5, undefined, 600, 561)).toBe(5);
    expect(snapTime(5, keyframes, 0, 561)).toBe(5);
  });
});
