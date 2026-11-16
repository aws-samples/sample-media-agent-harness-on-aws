/**
 * @vitest-environment jsdom
 *
 * A direct timeline action reads as a normal chat turn: the request as the user's blue bubble, the result as a
 * reply (one card, or one strip for several frames, and the footer); running shows the chat's in-progress row, a
 * failure the reason + Retry. The agent is never called.
 */
import '@testing-library/jest-dom';
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

const h = vi.hoisted(() => ({ authFetch: vi.fn() }));
vi.mock('../../auth/authFetch', () => ({ authFetch: h.authFetch }));

import { ActionLine } from './ActionLine';
import { __resetDirectActionsForTests, runDirectTimelineAction, type ActionLineRecord } from '../../projects/mediaActions';

const frames: ActionLineRecord = {
  op: 'frame', status: 'done', sourceName: 'netflix_open_content_meridian.mp4', points: [23000, 26000, 29000], elapsedMs: 1400,
  outputs: [0, 1, 2].map((i) => ({ kind: 'image' as const, name: `f${i}.jpg`, url: `https://example.org/f${i}.jpg`, key: `f${i}.jpg`, s3uri: `s3://b/f${i}.jpg` })),
};

beforeEach(() => { h.authFetch.mockReset(); __resetDirectActionsForTests(); });

const clip: ActionLineRecord = {
  op: 'clip', status: 'done', sourceName: 'Netflix_Open_Content_Meridian.mp4', ranges: [{ inMs: 0, outMs: 94000 }], elapsedMs: 600,
  outputs: [{ kind: 'video', name: 'clip_0000-0134.mp4', url: 'https://example.org/clip.mp4', key: 'clip.mp4', s3uri: 's3://b/clip.mp4' }],
};

describe('ActionLine — a direct action as a chat turn', () => {
  it('a clip: the request as the user bubble ("from the timeline"), the result as a reply with one card and the footer', () => {
    render(<ActionLine actionId="a1" action={clip} sessionId="s1" />);
    expect(screen.getByTestId('action-line')).toHaveAttribute('data-status', 'done');
    const request = screen.getByTestId('action-request');
    expect(request).toHaveTextContent('Clip 00:00–01:34 of Netflix_Open_Content_Meridian.mp4');
    expect(request).toHaveTextContent('from the timeline');
    const reply = screen.getByTestId('action-reply');
    expect(reply).toHaveTextContent('Clip');
    expect(screen.getByTestId('action-summary')).toHaveTextContent("Here's your 1:34 clip.");
    expect(screen.getByTestId('action-cards').children).toHaveLength(1); // the standard chat media card
    expect(screen.getByTestId('action-footer')).toHaveTextContent('Opened in a new tab · 0.6 s');
    expect(screen.queryByRole('button', { name: 'Open' })).toBeNull();
  });

  it('several frames: the times in the bubble, each frame the standard chat image card, "Opened in a gallery tab"', () => {
    render(<ActionLine actionId="a1" action={frames} sessionId="s1" />);
    const request = screen.getByTestId('action-request');
    expect(request).toHaveTextContent('Extract 3 frames from netflix_open_content_meridian.mp4');
    expect(request).toHaveTextContent('00:23 · 00:26 · 00:29 — from the timeline');
    expect(screen.getByTestId('action-summary')).toHaveTextContent('Here are your 3 frames.');
    // As big as any chat image: one standard card per frame (a 92 px strip read as broken).
    expect(screen.getByTestId('action-cards').children).toHaveLength(3);
    expect(screen.getByTestId('action-footer')).toHaveTextContent('Opened in a gallery tab · 1.4 s');
  });

  it('a Clip of several ranges shows every clip, not just the first', () => {
    const clips = { op: 'clip' as const, status: 'done' as const, sourceName: 'x.mp4', elapsedMs: 900,
      ranges: [{ inMs: 4771, outMs: 25626 }, { inMs: 35202, outMs: 53520 }, { inMs: 71839, outMs: 91792 }],
      outputs: [0, 1, 2].map((i) => ({ kind: 'video' as const, name: `c${i}.mp4`, url: `https://example.org/c${i}.mp4`, key: `c${i}.mp4`, s3uri: `s3://b/c${i}.mp4` })) };
    render(<ActionLine actionId="a2" action={clips} sessionId="s1" />);
    expect(screen.getByTestId('action-cards').children).toHaveLength(3);
    expect(screen.getByTestId('action-summary')).toHaveTextContent('Here are your 3 clips.');
    expect(screen.getByTestId('action-footer')).toHaveTextContent('Opened in a gallery tab · 0.9 s');
  });

  it('several results open as ONE gallery tab, at once (no tab per result)', async () => {
    const { openActionOutputs } = await import('./ActionLine');
    const { RESULT_GALLERY_EVENT } = await import('../../projects/mediaActions');
    const outs = [0, 1, 2].map((i) => ({ kind: 'video' as const, name: `c${i}.mp4`, url: `https://e/c${i}.mp4`, key: `c${i}`, s3uri: `s3://b/c${i}.mp4` }));
    const seen: unknown[] = [];
    const on = (e: Event) => seen.push((e as CustomEvent).detail);
    window.addEventListener(RESULT_GALLERY_EVENT, on);
    const resolve = vi.fn(); const open = vi.fn();
    await openActionOutputs(outs, 's1', 'clip', 'x.mp4', { resolve, open, sleep: async () => {} });
    await openActionOutputs(frames.outputs!, 's1', 'frame', 'x.mp4', { resolve, open, sleep: async () => {} });
    window.removeEventListener(RESULT_GALLERY_EVENT, on);
    expect(seen).toEqual([
      { sessionId: 's1', kind: 'video', label: '3 clips · x.mp4', items: outs.map((o) => ({ url: o.url, name: o.name, kind: o.kind })) },
      { sessionId: 's1', kind: 'image', label: '3 frames · x.mp4', items: frames.outputs!.map((o) => ({ url: o.url, name: o.name, kind: o.kind })) },
    ]);
    expect(resolve).not.toHaveBeenCalled();
    expect(open).not.toHaveBeenCalled();
  });

  it('a single frame and a crop read as the user would type them', () => {
    const { unmount } = render(<ActionLine actionId="a1" sessionId="s1"
      action={{ ...frames, points: [23000], outputs: frames.outputs!.slice(0, 1) }} />);
    expect(screen.getByTestId('action-request')).toHaveTextContent('Extract a frame at 00:23 from netflix_open_content_meridian.mp4');
    expect(screen.getByTestId('action-summary')).toHaveTextContent("Here's your frame from 00:23.");
    unmount();
    render(<ActionLine actionId="a2" sessionId="s1" action={{ op: 'crop', status: 'done', sourceName: 'clip.mp4',
      box: { x: 0.35, y: 0, w: 0.32, h: 1 }, elapsedMs: 4800, outputs: [{ ...clip.outputs![0], name: 'clip_crop.mp4' }] }} />);
    expect(screen.getByTestId('action-request')).toHaveTextContent('Crop clip.mp4 to the selected area (32% × 100% of the frame)');
    expect(screen.getByTestId('action-summary')).toHaveTextContent('The video is cropped to the selected area (32% × 100% of the frame).');
  });

  it('running: the bubble at once, and the chat\'s in-progress row in the reply', () => {
    render(<ActionLine actionId="a1" action={{ op: 'clip', status: 'running', sourceName: 'trailer.mp4', ranges: [{ inMs: 23000, outMs: 30000 }] }} sessionId="s1" />);
    expect(screen.getByTestId('action-request')).toHaveTextContent('Clip 00:23–00:30 of trailer.mp4');
    expect(screen.getByTestId('action-working')).toHaveTextContent('Clipping…');
    expect(screen.queryByTestId('action-summary')).toBeNull();
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('failed: the reason in the chat\'s error style, that it went to the agent, and Retry when it can be re-run', async () => {
    h.authFetch.mockResolvedValue({ ok: false, status: 502, json: async () => ({ ok: false, error: 'box outside the frame' }) });
    await runDirectTimelineAction(
      { op: 'crop', source: { location: 'https://x/clip.mp4' }, box: { x: 0, y: 0, w: 1, h: 1 }, sessionId: 's1', actionId: 'k1' },
      { fallback: () => {}, openOutputs: () => {} },
    );
    render(<ActionLine actionId="k1" sessionId="s1"
      action={{ op: 'crop', status: 'failed', sourceName: 'clip.mp4', error: 'box outside the frame', handedToAgent: true }} />);
    expect(screen.getByTestId('action-error')).toHaveTextContent('box outside the frame');
    expect(screen.getByTestId('action-reply')).toHaveTextContent('Sent to the agent instead');
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
  });

  it('a failed line restored from the transcript (nothing to re-run here) offers no Retry', () => {
    render(<ActionLine actionId="old" sessionId="s1" action={{ op: 'frame', status: 'failed', sourceName: 'x.mp4', error: 'e' }} />);
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
  });
});

describe('openActionOutputs', () => {
  it('opens a single result as an asset tab (no Render Agent), retrying until it is registered', async () => {
    const { openActionOutputs } = await import('./ActionLine');
    const store = await import('../../projects/sessionProjectStore');
    vi.spyOn(store, 'getSessionProject').mockReturnValue({ activeProjectId: 'p1' } as never);
    const resolve = vi.fn()
      .mockResolvedValueOnce({ ok: true, value: { 's3://b/f0.jpg': null } })            // not registered yet
      .mockResolvedValueOnce({ ok: true, value: { 's3://b/f0.jpg': 'image:abc' } });
    const open = vi.fn().mockResolvedValue({ ok: true });
    const composed: unknown[] = [];
    const onUrls = (e: Event) => composed.push((e as CustomEvent).detail);
    window.addEventListener('lab:render-urls', onUrls);
    await openActionOutputs(frames.outputs!.slice(0, 1), 's1', 'frame', undefined, { resolve, open, sleep: async () => {} });
    window.removeEventListener('lab:render-urls', onUrls);
    expect(resolve).toHaveBeenCalledWith('p1', ['s3://b/f0.jpg']);
    expect(open).toHaveBeenCalledWith(expect.objectContaining({ ref: { project_id: 'p1', asset_id: 'image:abc', frame_s3_key: null }, kind: 'image' }), 's1');
    expect(composed).toEqual([]); // never the composed /render path
  });

  it('falls back to the composed tab only when the result never registers', async () => {
    const { openActionOutputs } = await import('./ActionLine');
    const store = await import('../../projects/sessionProjectStore');
    vi.spyOn(store, 'getSessionProject').mockReturnValue({ activeProjectId: 'p1' } as never);
    const resolve = vi.fn().mockResolvedValue({ ok: true, value: {} });
    const composed: unknown[] = [];
    const onUrls = (e: Event) => composed.push((e as CustomEvent).detail);
    window.addEventListener('lab:render-urls', onUrls);
    await openActionOutputs(frames.outputs!.slice(0, 1), 's1', 'frame', undefined, { resolve, open: vi.fn(), sleep: async () => {} });
    window.removeEventListener('lab:render-urls', onUrls);
    expect(resolve).toHaveBeenCalledTimes(12);
    expect(composed).toHaveLength(1);
  });

  it('a big batch shows the first 4 results, "Show all" opens the gallery, and failures are named with Retry', async () => {
    const { RESULT_GALLERY_EVENT } = await import('../../projects/mediaActions');
    const outs = Array.from({ length: 6 }, (_, i) => ({ kind: 'video' as const, name: `c${i}.mp4`, url: `https://e/c${i}.mp4`, key: `c${i}`, s3uri: `s3://b/c${i}` }));
    const rec = { op: 'crop' as const, status: 'done' as const, sourceName: 'x.mp4', batchTotal: 7, elapsedMs: 9000, outputs: outs,
      items: [...outs.map((o) => ({ sourceName: 'x.mp4', outputs: [o] })), { sourceName: 'x.mp4', label: 'Segment 7', error: 'ffmpeg failed' }] };
    const seen: unknown[] = [];
    const on = (e: Event) => seen.push((e as CustomEvent).detail);
    window.addEventListener(RESULT_GALLERY_EVENT, on);
    render(<ActionLine actionId="b9" action={rec} sessionId="s1" />);
    expect(screen.getByTestId('action-cards').children).toHaveLength(4);
    fireEvent.click(screen.getByTestId('action-show-all'));
    window.removeEventListener(RESULT_GALLERY_EVENT, on);
    expect(screen.getByTestId('action-show-all')).toHaveTextContent('Show all 6');
    expect(seen).toHaveLength(1);
    expect(screen.getByTestId('action-batch-failures')).toHaveTextContent('Segment 7: ffmpeg failed');
    expect(screen.getByTestId('action-summary')).toHaveTextContent('Here are your 6 crops — 1 of 7 could not be made.');
  });
});
