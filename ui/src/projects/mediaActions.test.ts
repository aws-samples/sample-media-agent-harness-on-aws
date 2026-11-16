// @vitest-environment jsdom
// Direct timeline actions, client half: which timeline actions go direct, the running → done / failed flow with
// its fallback to the chat path, and the hidden context the next turn carries.
import { describe, it, expect, vi, beforeEach } from 'vitest';

const h = vi.hoisted(() => ({ authFetch: vi.fn() }));
vi.mock('../auth/authFetch', () => ({ authFetch: h.authFetch }));

import {
  __resetDirectActionsForTests, actionContextBlock, actionLineMessage, CHAT_ACTION_EVENT, directMediaRequest,
  retryDirectAction, routeTimelineAction, runDirectTimelineAction, withActionContext, type ChatActionDetail, type MediaActionBody,
} from './mediaActions';
import type { ChatMessage } from '../lab-api';

const SID = 's-1';
const SRC = 'https://cdn.example/projects/p/video:1/trailer.mp4';

function json(status: number, body: unknown) {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as unknown as Response;
}

function captureLines() {
  const seen: ChatActionDetail[] = [];
  const on = (e: Event) => seen.push((e as CustomEvent<ChatActionDetail>).detail);
  window.addEventListener(CHAT_ACTION_EVENT, on);
  return { seen, stop: () => window.removeEventListener(CHAT_ACTION_EVENT, on) };
}

beforeEach(() => { h.authFetch.mockReset(); __resetDirectActionsForTests(); });

describe('directMediaRequest', () => {
  it('maps Frame / Clip / Crop on one source to the route body (seconds → ms)', () => {
    expect(directMediaRequest({ verb: 'frame', src: SRC, points: [23, 26.5, 23] }, SID, 'a1'))
      .toEqual({ op: 'frame', points: [23000, 26500], source: { location: SRC }, sessionId: SID, actionId: 'a1' });
    expect(directMediaRequest({ verb: 'clip', src: SRC, ranges: [{ in: 23, out: 30 }] }, SID, 'a1'))
      .toMatchObject({ op: 'clip', ranges: [{ inMs: 23000, outMs: 30000 }] });
    expect(directMediaRequest({ verb: 'crop', src: SRC, box: { x: 0.2, y: 0, w: 0.5, h: 1 } }, SID, 'a1'))
      .toMatchObject({ op: 'crop', box: { x: 0.2, y: 0, w: 0.5, h: 1 } });
  });

  it('leaves to the agent: Analyze, a batch, a whole-video clip, no source', () => {
    expect(directMediaRequest({ verb: 'shots', src: SRC }, SID, 'a')).toBeNull();
    expect(directMediaRequest({ verb: 'clip', src: SRC, ranges: [] }, SID, 'a')).toBeNull();
    expect(directMediaRequest({ verb: 'frame', src: SRC, points: [1], clips: [{}] }, SID, 'a')).toBeNull();
    expect(directMediaRequest({ verb: 'frame', points: [1] }, SID, 'a')).toBeNull();
  });
});

describe('runDirectTimelineAction', () => {
  const body: MediaActionBody = { op: 'clip', source: { location: SRC }, ranges: [{ inMs: 23000, outMs: 30000 }], sessionId: SID, actionId: 'a1' };
  const out = { kind: 'video', name: 'trailer_clip.mp4', url: 'https://cdn/x.mp4', key: 'x.mp4', s3uri: 's3://b/x.mp4' };

  it('shows a running line at once, then done, and opens the result — no chat turn', async () => {
    h.authFetch.mockResolvedValue(json(200, { ok: true, outputs: [out], elapsedMs: 4100 }));
    const lines = captureLines();
    const fallback = vi.fn();
    const openOutputs = vi.fn();
    const r = await runDirectTimelineAction(body, { fallback, openOutputs, now: () => 5 });
    lines.stop();
    expect(r.ok).toBe(true);
    expect(h.authFetch).toHaveBeenCalledWith('/media/actions', expect.objectContaining({ method: 'POST' }));
    expect(lines.seen.map((d) => (d.message.action as { status: string }).status)).toEqual(['running', 'done']);
    expect(lines.seen[1].message).toMatchObject({ id: 'a1-x', role: 'action', timestamp: 5 });
    expect(openOutputs).toHaveBeenCalledWith([out], 'clip', 'trailer.mp4');
    expect(fallback).not.toHaveBeenCalled();
  });

  it('a second identical Clip while the first still runs joins it: one request, one result, one tab', async () => {
    // IND403 dry run: Clip took 10-16 s the first time, the button gave no sign, and a second click cut a copy.
    let finish: (r: Response) => void = () => undefined;
    h.authFetch.mockReturnValue(new Promise<Response>((r) => { finish = r; }));
    const openOutputs = vi.fn();
    const first = runDirectTimelineAction(body, { fallback: vi.fn(), openOutputs });
    const second = runDirectTimelineAction({ ...body, actionId: 'a2' }, { fallback: vi.fn(), openOutputs });
    finish(json(200, { ok: true, outputs: [out], elapsedMs: 12000 }));
    expect(await second).toBe(await first);
    expect(h.authFetch).toHaveBeenCalledTimes(1);
    expect(openOutputs).toHaveBeenCalledTimes(1);
  });

  it('a different range, or the same one after the first finished, runs on its own', async () => {
    h.authFetch.mockResolvedValue(json(200, { ok: true, outputs: [out], elapsedMs: 900 }));
    const opts = { fallback: vi.fn(), openOutputs: vi.fn() };
    await Promise.all([
      runDirectTimelineAction(body, opts),
      runDirectTimelineAction({ ...body, actionId: 'a3', ranges: [{ inMs: 40000, outMs: 50000 }] }, opts),
    ]);
    await runDirectTimelineAction({ ...body, actionId: 'a4' }, opts);
    expect(h.authFetch).toHaveBeenCalledTimes(3);
  });

  it('a refusal before running removes the line and takes the chat path', async () => {
    h.authFetch.mockResolvedValue(json(422, { ok: false, error: 'the source is not a file of this chat\'s project' }));
    const lines = captureLines();
    const fallback = vi.fn();
    await runDirectTimelineAction(body, { fallback, openOutputs: vi.fn() });
    lines.stop();
    expect(lines.seen[lines.seen.length - 1]?.remove).toBe(true);
    expect(fallback).toHaveBeenCalledTimes(1);
  });

  it('a network error is NOT a refusal: it may have run, so it is never handed to the agent (Retry instead)', async () => {
    h.authFetch.mockRejectedValue(new Error('offline'));
    const fallback = vi.fn();
    await runDirectTimelineAction(body, { fallback, openOutputs: vi.fn() });
    expect(fallback).not.toHaveBeenCalled();
  });

  it('a failed run keeps a failed line, hands the request to the agent, and can be retried in place', async () => {
    h.authFetch.mockResolvedValueOnce(json(502, { ok: false, error: 'moov atom not found' }));
    const lines = captureLines();
    const fallback = vi.fn();
    await runDirectTimelineAction(body, { fallback, openOutputs: vi.fn() });
    const failed = lines.seen[lines.seen.length - 1]!.message.action as { status: string; error: string; handedToAgent: boolean };
    expect(failed).toMatchObject({ status: 'failed', error: 'moov atom not found', handedToAgent: true });
    expect(fallback).toHaveBeenCalledTimes(1);
    h.authFetch.mockResolvedValueOnce(json(200, { ok: true, outputs: [out], elapsedMs: 3000 }));
    expect(retryDirectAction('a1')).toBe(true);
    await vi.waitFor(() => expect((lines.seen[lines.seen.length - 1]!.message.action as { status: string }).status).toBe('done'));
    lines.stop();
    expect(lines.seen[lines.seen.length - 1]!.message.id).toBe('a1-x');  // same line, updated in place
    expect(fallback).toHaveBeenCalledTimes(1);            // a retry never asks the agent twice
  });
});

describe('routeTimelineAction (the Renderer bridge)', () => {
  const clipData = { verb: 'clip', src: SRC, ranges: [{ in: 23, out: 30 }] };

  it('a project chat calls the route, not the agent', async () => {
    h.authFetch.mockResolvedValue(json(200, { ok: true, outputs: [], elapsedMs: 1 }));
    const sendToAgent = vi.fn();
    expect(routeTimelineAction(clipData, SID, { projectOf: () => 'p1', sendToAgent, openOutputs: vi.fn(), newActionId: () => 'a9' })).toBe('direct');
    await vi.waitFor(() => expect(h.authFetch).toHaveBeenCalledTimes(1));
    expect(JSON.parse(h.authFetch.mock.calls[0][1].body)).toMatchObject({ op: 'clip', actionId: 'a9', sessionId: SID });
    expect(sendToAgent).not.toHaveBeenCalled();
  });

  it('falls back to the agent when the route refuses', async () => {
    h.authFetch.mockResolvedValue(json(422, { ok: false, error: 'not a project file' }));
    const sendToAgent = vi.fn();
    routeTimelineAction(clipData, SID, { projectOf: () => 'p1', sendToAgent, openOutputs: vi.fn() });
    await vi.waitFor(() => expect(sendToAgent).toHaveBeenCalledTimes(1));
  });

  it('a brand-new chat is saved (with its project) BEFORE the action runs, so the route does not refuse it', async () => {
    // Staging 28391ced: the first Clip of a new chat reached the proxy before the chat's row existed,
    // got "409 the chat has no active project", and took the slow agent path.
    const order: string[] = [];
    h.authFetch.mockImplementation(async () => { order.push('action'); return json(200, { ok: true, outputs: [], elapsedMs: 1 }); });
    const prepare = vi.fn(async () => { order.push('prepare'); });
    routeTimelineAction(clipData, SID, { projectOf: () => 'p1', sendToAgent: vi.fn(), openOutputs: vi.fn(), prepare });
    await vi.waitFor(() => expect(order).toEqual(['prepare', 'action']));
    expect(prepare).toHaveBeenCalledWith(SID);
  });

  it('a failed prepare still runs the action (the route then decides)', async () => {
    h.authFetch.mockResolvedValue(json(200, { ok: true, outputs: [], elapsedMs: 1 }));
    const sendToAgent = vi.fn();
    routeTimelineAction(clipData, SID, { projectOf: () => 'p1', sendToAgent, openOutputs: vi.fn(),
      prepare: () => Promise.reject(new Error('offline')) });
    await vi.waitFor(() => expect(h.authFetch).toHaveBeenCalledTimes(1));
    expect(sendToAgent).not.toHaveBeenCalled();
  });

  it('a chat with no project, or Analyze, goes straight to the agent', () => {
    const sendToAgent = vi.fn();
    expect(routeTimelineAction(clipData, SID, { projectOf: () => null, sendToAgent, openOutputs: vi.fn() })).toBe('agent');
    expect(routeTimelineAction({ verb: 'faces', src: SRC }, SID, { projectOf: () => 'p1', sendToAgent, openOutputs: vi.fn() })).toBe('agent');
    expect(sendToAgent).toHaveBeenCalledTimes(2);
    expect(h.authFetch).not.toHaveBeenCalled();
  });
});

describe('the next turn knows what the user did', () => {
  const msg = (id: string, role: ChatMessage['role'], extra: Partial<ChatMessage> = {}): ChatMessage =>
    ({ id, role, content: 'x', timestamp: 1, ...extra });

  it('summarizes action lines since the last reply only, then the prompt', () => {
    const msgs: ChatMessage[] = [
      actionLineMessage('old', 1, { op: 'frame', status: 'done', sourceName: 'old.mp4', points: [1000], outputs: [] }),
      msg('a', 'assistant'),
      actionLineMessage('f', 2, {
        op: 'frame', status: 'done', sourceName: 'meridian.mp4', sourceAssetId: 'video:1', points: [23000, 26000],
        outputs: [{ kind: 'image', name: 'f1.jpg', url: 'https://cdn/f1.jpg', key: 'f1.jpg', s3uri: 's3://b/f1.jpg' }],
      }),
      actionLineMessage('c', 3, { op: 'clip', status: 'done', sourceName: 'meridian.mp4', ranges: [{ inMs: 23000, outMs: 30000 }],
        outputs: [{ kind: 'video', name: 'clip.mp4', url: 'https://cdn/clip.mp4', key: 'clip.mp4', s3uri: 's3://b/clip.mp4' }] }),
      actionLineMessage('k', 4, { op: 'crop', status: 'failed', sourceName: 'clip.mp4', error: 'box outside the frame' }),
      actionLineMessage('r', 5, { op: 'frame', status: 'running', sourceName: 'x.mp4', points: [0] }),
      msg('u', 'user'),
    ];
    const block = actionContextBlock(msgs);
    expect(block.split('\n')).toEqual([
      '[Since your last reply, the user did, in the timeline:]',
      '- extracted 2 frames of meridian.mp4 (video:1) at 00:23, 00:26 → f1.jpg (https://cdn/f1.jpg)',
      '- clipped meridian.mp4 00:23–00:30 → clip.mp4 (https://cdn/clip.mp4)',
      '- tried to crop clip.mp4 — failed: box outside the frame',
    ]);
    expect(withActionContext(msgs, 'now make it vertical')).toBe(`${block}\n\nnow make it vertical`);
    expect(withActionContext([msg('a', 'assistant'), msg('u', 'user')], 'hi')).toBe('hi');
  });
});

describe('crop wording names the aspect the timeline measured', () => {
  it('matches a preset on the video pixels, else reads the share of the frame', async () => {
    const m = await import('./mediaActions');
    // 9:16 on a 1280x720 frame: w = (9/16)*(720/1280) = 0.316 of the width, full height.
    expect(m.cropAspectLabel({ w: 0.3164, h: 1 }, 1280, 720)).toBe('9:16');
    expect(m.cropAspectLabel({ w: 1, h: 1 }, 1280, 720)).toBe('16:9');
    expect(m.cropAspectLabel({ w: 0.3, h: 0.7 }, 1280, 720)).toBeUndefined();
    const crop = { op: 'crop' as const, status: 'done' as const, sourceName: 'clip.mp4', box: { x: 0.34, y: 0, w: 0.3164, h: 1 } };
    expect(m.actionRequestText({ ...crop, aspect: '9:16' })).toBe('Crop clip.mp4 to 9:16');
    expect(m.actionReplySummary({ ...crop, aspect: '9:16' })).toBe('The video is cropped to 9:16.');
    expect(m.actionReplySummary(crop)).toBe('The video is cropped to the selected area (32% × 100% of the frame).');
    expect(m.actionRequestText(crop)).toBe('Crop clip.mp4 to the selected area (32% × 100% of the frame)');
  });
});

describe('result gallery', () => {
  it('frames build an image gallery, clips a clip gallery, each holding every result', async () => {
    const m = await import('./mediaActions');
    const items = [{ url: 'https://cdn/a.jpg', name: 'a.jpg' }, { url: 'https://cdn/b.jpg', name: 'b.jpg' }];
    const img = m.resultGalleryManifest({ sessionId: 's', label: '2 frames · x.mp4', kind: 'image', items }, 'view-1', 't');
    expect(img).toMatchObject({ id: 'view-1', template: 'image-annotated', src: 'https://cdn/a.jpg', title: '2 frames · x.mp4' });
    expect((img.images as unknown[]).length).toBe(2);
    const vid = m.resultGalleryManifest({ sessionId: 's', label: '2 clips', kind: 'video', items }, 'view-2', 't');
    expect(vid).toMatchObject({ template: 'clip-gallery', clips: [{ id: 'c1', src: 'https://cdn/a.jpg', kind: 'video' }, { id: 'c2' }] });
    expect(m.galleryLabel('clip', 3, 'x.mp4')).toBe('3 clips · x.mp4');
    expect(m.galleryLabel('frame', 8)).toBe('8 frames');
  });
});

describe('a gallery batch runs directly (Crop all / Clip all marked / Extract frame)', () => {
  const crops = { verb: 'crop', prompt: 'Crop these', clips: [
    { label: 'Segment 1', src: SRC, box: { x: 0.2, y: 0, w: 0.3, h: 1 } },
    { label: 'Segment 2', src: SRC, box: { x: 0.4, y: 0, w: 0.3, h: 1 } },
    { label: 'Segment 3', src: SRC, box: { x: 0.1, y: 0, w: 0.3, h: 1 } },
  ] };

  it('maps each verb to a batch (an audio trim is a clip); refuses a bad entry', async () => {
    const m = await import('./mediaActions');
    expect(m.directBatchRequest(crops, SID, 'b1')).toMatchObject({ op: 'crop', actionId: 'b1',
      items: [{ label: 'Segment 1', source: { location: SRC }, box: { x: 0.2 } }, {}, {}] });
    expect(m.directBatchRequest({ verb: 'clip', clips: [{ src: SRC, in: 1.5, out: 4 }] }, SID, 'b')!.items[0].ranges)
      .toEqual([{ inMs: 1500, outMs: 4000 }]);
    expect(m.directBatchRequest({ verb: 'extract', clips: [{ src: SRC, in: 2, at: 2.5 }] }, SID, 'b')!.items[0].points).toEqual([2500]);
    expect(m.directBatchRequest({ verb: 'trim', clips: [{ src: SRC, in: 1, out: 2 }] }, SID, 'b')).toMatchObject({ op: 'clip', items: [{ ranges: [{ inMs: 1000, outMs: 2000 }] }] });
    expect(m.directBatchRequest({ verb: 'crop', clips: [{ src: SRC }] }, SID, 'b')).toBeNull();
  });

  it('one line counts up, items go one per request into ONE line, then ONE gallery opens', async () => {
    const lines = captureLines();
    let n = 0;
    h.authFetch.mockImplementation(async (_u: string, init: RequestInit) => {
      const b = JSON.parse(String(init.body));
      n += 1;
      const outputs = Array.from({ length: n }, (_, i) => ({ kind: 'video', name: `c${i}.mp4`, url: `https://cdn/c${i}.mp4`, key: `c${i}`, s3uri: `s3://b/c${i}` }));
      return json(200, { ok: true, outputs: [outputs[n - 1]], elapsedMs: 1000,
        record: { op: 'crop', status: 'done', sourceName: 'trailer.mp4', batchTotal: 3, outputs,
          items: Array.from({ length: 3 }, (_, i) => (i <= b.batch.index ? { sourceName: 'trailer.mp4', outputs: [outputs[i]] } : { sourceName: '' })) } });
    });
    const openOutputs = vi.fn();
    const route = routeTimelineAction(crops as never, SID, { projectOf: () => 'p1', sendToAgent: vi.fn(), openOutputs, newActionId: () => 'b1' });
    expect(route).toBe('direct');
    await vi.waitFor(() => expect(openOutputs).toHaveBeenCalledTimes(1));
    lines.stop();
    const bodies = h.authFetch.mock.calls.map((c) => JSON.parse(c[1].body));
    expect(bodies.map((b) => b.batch)).toEqual([0, 1, 2].map((index) => ({ id: 'b1', index, total: 3, label: `Segment ${index + 1}` })));
    expect(new Set(lines.seen.map((d) => d.message.id))).toEqual(new Set(['b1-x'])); // one line throughout
    const last = lines.seen[lines.seen.length - 1].message.action as { status: string; outputs: unknown[] };
    expect(last.status).toBe('done');
    expect(openOutputs.mock.calls[0][0]).toHaveLength(3);
    expect(openOutputs.mock.calls[0][1]).toBe(SID);
  });

  it('a first item refused before running hands the whole batch to the agent; nothing is left in the chat', async () => {
    const lines = captureLines();
    h.authFetch.mockResolvedValue(json(422, { ok: false, error: "the source is not a file of this chat's project" }));
    const sendToAgent = vi.fn();
    routeTimelineAction(crops as never, SID, { projectOf: () => 'p1', sendToAgent, openOutputs: vi.fn(), newActionId: () => 'b2' });
    await vi.waitFor(() => expect(sendToAgent).toHaveBeenCalledTimes(1));
    lines.stop();
    expect(h.authFetch).toHaveBeenCalledTimes(1);
    expect(lines.seen[lines.seen.length - 1].remove).toBe(true);
  });

  it('reads as a chat turn, counts up while running, and names the items that failed', async () => {
    const m = await import('./mediaActions');
    const rec = { op: 'crop' as const, status: 'done' as const, sourceName: 'trailer.mp4', batchTotal: 3,
      items: [{ sourceName: 'trailer.mp4', outputs: [{} as never] }, { sourceName: 'trailer.mp4', label: 'Segment 2', error: 'ffmpeg failed' },
        { sourceName: 'trailer.mp4', outputs: [{} as never] }], outputs: [{} as never, {} as never] };
    expect(m.actionRequestText(rec)).toBe('Crop 3 clips, each to its box');
    expect(m.actionRequestMeta(rec)).toBe('of trailer.mp4 — from the gallery');
    expect(m.actionReplySummary(rec)).toBe('Here are your 2 crops — 1 of 3 could not be made.');
    expect(m.batchFailures(rec)).toEqual([{ name: 'Segment 2', error: 'ffmpeg failed' }]);
    expect(m.actionRunningLabel({ ...rec, status: 'running', items: rec.items.slice(0, 1) })).toBe('Cropping 2 of 3…');
    // the agent hears about each item
    const block = actionContextBlock([{ id: 'b-x', role: 'action', content: '', timestamp: 1, action: rec } as ChatMessage]);
    expect(block.split('\n').filter((l) => l.startsWith('- '))).toHaveLength(3);
  });
});


describe('crop of a marked range (QA 2026-10-20: a 0:40-0:50 range was ignored and the full clip was cropped)', () => {
  it('sends the first marked range with the crop and names it in the bubble', async () => {
    const m = await import('./mediaActions');
    const body = m.directMediaRequest({ verb: 'crop', src: SRC, box: { x: 0.2, y: 0, w: 0.5, h: 1 }, aspect: '1:1',
      ranges: [{ in: 40, out: 50 }, { in: 60, out: 70 }] } as never, SID, 'c1');
    expect(body).toMatchObject({ op: 'crop', ranges: [{ inMs: 40000, outMs: 50000 }] });
    expect(m.actionRequestText({ op: 'crop', status: 'done', sourceName: 'x.mp4', aspect: '1:1', ranges: [{ inMs: 40000, outMs: 50000 }] }))
      .toBe('Crop 00:40–00:50 of x.mp4 to 1:1');
    expect(m.directMediaRequest({ verb: 'crop', src: SRC, box: { x: 0, y: 0, w: 1, h: 1 } }, SID, 'c2')).not.toHaveProperty('ranges');
  });
});

describe('an action whose answer never came back is not cut a second time (staging 2026-11-15)', () => {
  const body: MediaActionBody = { op: 'clip', ranges: [{ inMs: 0, outMs: 140941 }], source: { location: SRC }, sessionId: SID, actionId: 'slow-1' };
  it("an edge timeout's HTML page: the line fails with Retry, and the agent is NOT asked to do it again", async () => {
    h.authFetch.mockResolvedValue({ ok: false, status: 504, json: async () => { throw new SyntaxError('Unexpected token <'); } } as unknown as Response);
    const lines = captureLines();
    const fallback = vi.fn();
    const r = await runDirectTimelineAction(body, { fallback, openOutputs: vi.fn() });
    lines.stop();
    expect(fallback).not.toHaveBeenCalled();
    expect(r).toMatchObject({ ok: false, unknown: true });
    expect(lines.seen.slice(-1)[0]?.message.action).toMatchObject({ status: 'failed' });
    expect(String((lines.seen.slice(-1)[0]?.message.action as { error?: string } | undefined)?.error)).toMatch(/may still have finished/);
  });
  it('a dropped connection: the same', async () => {
    h.authFetch.mockRejectedValue(new TypeError('network error'));
    const fallback = vi.fn();
    expect(await runDirectTimelineAction({ ...body, actionId: 'slow-2' }, { fallback, openOutputs: vi.fn() })).toMatchObject({ unknown: true });
    expect(fallback).not.toHaveBeenCalled();
  });
  it("a long action's streamed reply (200) that failed still says it ran, from its body", async () => {
    h.authFetch.mockResolvedValue(json(200, { ok: false, ran: true, error: 'moov atom not found' }));
    const fallback = vi.fn();
    expect(await runDirectTimelineAction({ ...body, actionId: 'slow-3' }, { fallback, openOutputs: vi.fn() })).toMatchObject({ ok: false, ran: true });
    expect(fallback).toHaveBeenCalledTimes(1); // ran and failed: the agent may still fix it, as before
  });
});

describe("trimming some of a gallery returns the gallery's next version (staging 2026-11-15: one Trim opened seven tabs)", () => {
  const set = [1, 2, 3].map((n) => ({ clipId: `c${n}`, label: `Chunk ${n}`, src: `https://cdn/chunk_${n}.wav`, kind: 'audio' as const }));
  const origin = { subjectIds: ['loc:s3://b/chunk_1.wav', 'view:artifacts/s/render/g1.json'], label: 'Olive chunks' };
  it('the same set in order, each trimmed item in place of its original, about the gallery it revises', async () => {
    const m = await import('./mediaActions');
    const record = { op: 'clip' as const, status: 'done' as const, sourceName: '2 files', items: [
      { outputs: [{ kind: 'audio' as const, name: 'chunk_1_trim.wav', url: 'https://cdn/chunk_1_trim.wav', key: 'k1', s3uri: 's3://b/k1' }] },
      { error: 'ffmpeg failed' }, // chunk 3's trim failed: it keeps its original
    ] };
    const d = m.setVersionDetail({ set, clips: [{ clipId: 'c1' }, { clipId: 'c3' }], origin }, record as never, SID, 'trim');
    expect(d).toMatchObject({ kind: 'audio', label: 'Olive chunks · trimmed', subjectIds: origin.subjectIds });
    expect(d.items.map((i) => [i.name, i.url])).toEqual([
      ['Chunk 1 · trimmed', 'https://cdn/chunk_1_trim.wav'], ['Chunk 2', 'https://cdn/chunk_2.wav'], ['Chunk 3', 'https://cdn/chunk_3.wav']]);
    const manifest = m.resultGalleryManifest(d, 'view-x', '2026-11-15T00:00:00Z') as { subjectIds?: string[]; clips?: { kind: string }[] };
    expect(manifest.subjectIds).toEqual(origin.subjectIds);
    expect(manifest.clips?.every((c) => c.kind === 'audio')).toBe(true);
  });
  it('a Crop all of some of a video gallery is its next version too, named "cropped"', async () => {
    const m = await import('./mediaActions');
    const vids = [1, 2].map((n) => ({ clipId: `v${n}`, label: `Shot ${n}`, src: `https://cdn/shot_${n}.mp4`, kind: 'video' as const }));
    const record = { op: 'crop' as const, status: 'done' as const, sourceName: 'x', items: [
      { outputs: [{ kind: 'video' as const, name: 'shot_2_crop.mp4', url: 'https://cdn/shot_2_crop.mp4', key: 'k', s3uri: 's3://b/k' }] }] };
    const d = m.setVersionDetail({ set: vids, clips: [{ clipId: 'v2' }], origin: { subjectIds: ['view:g'], label: 'Shots' } }, record as never, SID, 'crop');
    expect(d.label).toBe('Shots · cropped');
    expect(d.items.map((i) => i.name)).toEqual(['Shot 1', 'Shot 2 · cropped']);
  });
});
