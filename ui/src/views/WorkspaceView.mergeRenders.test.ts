import { describe, it, expect } from 'vitest';
import { mergeServerRenders } from './WorkspaceView';
import type { RenderHistoryEntry } from '../components/renderer/types';

// The session-switch hydration bug: switching a session loaded only the cached
// render history (which under-counts async renders), so tabs were missing until a
// hard refresh ran the S3 merge. mergeServerRenders is now shared by both paths.

const cachedEntry = (key: string, ts: number, label?: string): RenderHistoryEntry => ({
  turnIndex: 0,
  timestamp: ts,
  label,
  blocks: [{
    template: 'manifest-router',
    title: label || 'Render Output',
    data: { manifestUrl: `/api/artifact?key=${encodeURIComponent(key)}` },
  }],
});

describe('mergeServerRenders', () => {
  it('appends server renders that are not already cached (by manifest key)', () => {
    const cached = [cachedEntry('artifacts/s/render/a.json', 1000, 'A')];
    const server = [
      { key: 'artifacts/s/render/a.json', label: 'A', createdAt: '2026-08-13T21:00:00Z' }, // dup of cached
      { key: 'artifacts/s/render/b.json', label: 'B', createdAt: '2026-08-13T21:00:02Z' },
      { key: 'artifacts/s/render/c.json', label: 'C', createdAt: '2026-08-13T21:00:03Z' },
    ];
    const merged = mergeServerRenders(cached, server);
    const keys = merged.map(e => {
      const url = String((e.blocks[0].data as { manifestUrl?: string }).manifestUrl);
      return decodeURIComponent(url.match(/key=([^&]+)/)![1]);
    });
    expect(keys).toEqual([
      'artifacts/s/render/a.json',
      'artifacts/s/render/b.json',
      'artifacts/s/render/c.json',
    ]); // cached kept, b & c restored, a not duplicated
  });

  it('returns cached unchanged when the server has nothing new', () => {
    const cached = [cachedEntry('artifacts/s/render/a.json', 1000)];
    expect(mergeServerRenders(cached, [])).toHaveLength(1);
  });

  it('restores all server renders when nothing was cached (the "hard refresh finds them" case)', () => {
    const server = [
      { key: 'artifacts/s/render/a.json', createdAt: '2026-08-13T21:00:01Z' },
      { key: 'artifacts/s/render/b.json', createdAt: '2026-08-13T21:00:02Z' },
    ];
    expect(mergeServerRenders([], server)).toHaveLength(2);
  });

  it('sorts the merged result by timestamp', () => {
    const server = [
      { key: 'artifacts/s/render/late.json', createdAt: '2026-08-13T21:00:05Z' },
      { key: 'artifacts/s/render/early.json', createdAt: '1970-01-01T00:00:01Z' },
    ];
    const merged = mergeServerRenders([], server);
    expect(merged[0].timestamp).toBeLessThan(merged[1].timestamp);
    expect((merged[0].blocks[0].data as { manifestUrl?: string }).manifestUrl).toContain('early.json');
  });

  it('SERVER-AUTHORITATIVE: stale live tabs (mismatched keys) are replaced by the server composite', () => {
    // The exact bug from session 1dd5c2d8: the source video + extracted audio compose server-side,
    // but the browser also had LIVE tabs from the stream (keyed differently than the S3 manifests).
    // The server list is authoritative → those stale live tabs must NOT linger beside the composite.
    const SK = 'sk-1dd5';
    const liveVideo = cachedEntry('artifacts/s/render/live-video-hash.json', 1000, 'Video');
    const liveAudio = cachedEntry('artifacts/s/render/live-audio-hash.json', 1100, 'Audio');
    const merged = mergeServerRenders([liveVideo, liveAudio], [
      compositeEntry(SK, ['33f23152.json', 'arn_65e68cd2.json'],
        [{ id: 'video-x', type: 'video' }, { id: 'audio-s', type: 'audio' }]),
    ]);
    expect(merged).toHaveLength(1); // ONE composite tab — the stale live Video/Audio tabs are gone
    const m = (merged[0].blocks[0].data as { sourceKey?: string; manifest?: { tracks?: any[] } });
    expect(m.sourceKey).toBe(SK);
    expect((m.manifest?.tracks ?? []).map((t: any) => t.type)).toEqual(['video', 'audio']);
  });

  it('keeps a still-composing live placeholder while the server has other finished renders', () => {
    const composing: RenderHistoryEntry = { turnIndex: 0, timestamp: 9999, label: 'Composing', status: 'composing', blocks: [] };
    const merged = mergeServerRenders([composing], [soloServer('artifacts/s/render/v.json', 'Video')]);
    expect(merged).toHaveLength(2); // the finished server render + the in-flight placeholder
    expect(merged.some(e => e.status === 'composing')).toBe(true);
  });

  // --- Upload/result preview survival (the "original disappears when the proxy lands" bug) --------
  // renderUrlsInPanel makes a preview entry with `artifactUrls` + status:'composing'; when its
  // CLIENT compose resolves, blocks get populated but status stays 'composing'. A slow source-video
  // render can finish client-side BEFORE its S3 manifest is listed. An unrelated render landing
  // (e.g. a same-turn proxy) must not drop it. Stale STREAM tabs (manifestUrl, NO artifactUrls)
  // stay server-authoritative-dropped.
  const uploadPreview = (mkey: string, url: string, ts = 1000): RenderHistoryEntry => ({
    turnIndex: 0, timestamp: ts, label: 'Video', status: 'composing', artifactUrls: [url],
    blocks: [{ template: 'manifest-router', title: 'Video', data: { manifestUrl: `/api/artifact?key=${encodeURIComponent(mkey)}` } }],
  });

  it('preserves a finished upload preview (artifactUrls) when the server lists only an unrelated render', () => {
    const preview = uploadPreview('artifacts/s/render/original.json', 'https://cdn/artifacts/s/input/nba.mp4');
    const merged = mergeServerRenders([preview], [soloServer('artifacts/s/render/proxy.json', 'Video')]);
    expect(merged).toHaveLength(2); // proxy (server) + the upload preview survives
    const keys = merged.map(e => {
      const u = String((e.blocks?.[0]?.data as { manifestUrl?: string })?.manifestUrl ?? '');
      const m = u.match(/key=([^&]+)/); return m ? decodeURIComponent(m[1]) : '';
    });
    expect(keys).toContain('artifacts/s/render/original.json');
    expect(keys).toContain('artifacts/s/render/proxy.json');
  });

  it('supersedes the upload preview once the server lists its OWN manifest (no duplicate)', () => {
    const preview = uploadPreview('artifacts/s/render/original.json', 'https://cdn/artifacts/s/input/nba.mp4');
    const merged = mergeServerRenders([preview], [soloServer('artifacts/s/render/original.json', 'Video')]);
    expect(merged).toHaveLength(1); // server version wins — preview is not duplicated
  });

  it('supersedes the upload preview when a composite ABSORBS its manifest (member)', () => {
    const SK = 'artifacts/s/clip.mp4';
    const preview = uploadPreview('artifacts/s/render/original.json', 'https://cdn/artifacts/s/input/nba.mp4');
    const merged = mergeServerRenders([preview], [
      compositeEntry(SK, ['artifacts/s/render/original.json', 'artifacts/s/render/stems.json'],
        [{ id: 'v', type: 'video' }, { id: 'a', type: 'audio' }]),
    ]);
    expect(merged).toHaveLength(1); // absorbed into the composite, not left beside it
  });


  // --- Server-authoritative composites -------------------------------------------------------
  // The SERVER (/sessions/:id/renders) now composes per-source composites. It returns a composite
  // ENTRY (composite:true, key `composite:<sk>`, inline manifest, memberKeys). The UI just MAPS it
  // — no client composeTimeline. This is what keeps the tab set stable (no create-then-merge).
  const compositeEntry = (sk: string, memberKeys: string[], tracks: unknown[], extra: Record<string, unknown> = {}) => ({
    key: `composite:${sk}`, composite: true, sourceKey: sk, memberKeys,
    createdAt: '2026-08-26T10:00:00Z', label: 'Meridian',
    manifest: { id: 'c', template: 'composite-timeline', duration: 131, clips: [], sourceKey: sk, tracks, ...extra },
  });
  const soloServer = (key: string, label: string, ts = '2026-08-26T10:00:00Z') =>
    ({ key, label, createdAt: ts, manifest: { id: key, template: 'nle-timeline', duration: 60, tracks: [], clips: [] } });

  it('maps a server composite entry to ONE inline composite tab (no client merge)', () => {
    const SK = 'artifacts/s/clip.mp4';
    const merged = mergeServerRenders([], [
      compositeEntry(SK, ['artifacts/s/render/v.json', 'artifacts/s/render/stems.json'],
        [{ id: 'video-x', type: 'video' }, { id: 'audio-s', type: 'audio' }]),
    ]);
    expect(merged).toHaveLength(1);
    const m = (merged[0].blocks[0].data as { manifest?: { template?: string; tracks?: unknown[] }; sourceKey?: string });
    expect(m.sourceKey).toBe(SK);
    expect(m.manifest?.template).toBe('composite-timeline');
    expect(m.manifest?.tracks).toHaveLength(2); // video + audio lane, composed server-side
  });

  it('absorbs cached solo tabs the composite lists as members (no duplicate beside the composite)', () => {
    const SK = 'artifacts/s/clip.mp4';
    const cached = [
      cachedEntry('artifacts/s/render/v.json', 1000, 'Video'),
      cachedEntry('artifacts/s/render/stems.json', 1100, 'Audio'),
    ];
    const merged = mergeServerRenders(cached, [
      compositeEntry(SK, ['artifacts/s/render/v.json', 'artifacts/s/render/stems.json'],
        [{ id: 'video-x', type: 'video' }, { id: 'audio-s', type: 'audio' }]),
    ]);
    expect(merged).toHaveLength(1); // both cached solos absorbed → one composite tab
    expect((merged[0].blocks[0].data as { sourceKey?: string }).sourceKey).toBe(SK);
  });

  it('a single-source solo render stays a solo tab', () => {
    const merged = mergeServerRenders([], [soloServer('artifacts/s/render/v.json', 'Video')]);
    expect(merged).toHaveLength(1);
    expect((merged[0].blocks[0].data as { manifestUrl?: string }).manifestUrl).toContain('v.json');
  });

  it('preserves an UNRELATED cached tab when the server list is empty (no drop on transient empty)', () => {
    // e2e-render-sim relies on this: seeded renderHistory must survive a getSessionRenders that
    // returns nothing (artifacts not listed), so we never blank the panel.
    const cached = [cachedEntry('artifacts/other/render/z.json', 1000, 'Z')];
    const merged = mergeServerRenders(cached, []);
    expect(merged).toHaveLength(1);
    expect((merged[0].blocks[0].data as { manifestUrl?: string }).manifestUrl).toContain('z.json');
  });

  it('ANTI-CHURN: re-fetching an updated composite replaces the SAME tab in place (stable count/key)', () => {
    // The core stability guarantee: when a new analysis folds in server-side, the endpoint returns
    // the SAME composite key with more lanes. The UI must update that ONE tab, never add/reorder.
    const SK = 'sk-stable';
    const first = mergeServerRenders([], [
      compositeEntry(SK, ['a.json', 'b.json'], [{ id: 'video-x', type: 'video' }, { id: 'audio-s', type: 'audio' }]),
    ]);
    expect(first).toHaveLength(1);
    const second = mergeServerRenders(first, [
      compositeEntry(SK, ['a.json', 'b.json', 'shots.json'],
        [{ id: 'video-x', type: 'video' }, { id: 'audio-s', type: 'audio' }],
        { markers: [{ id: 'm0', time: 0 }, { id: 'm1', time: 5 }] }),
    ]);
    expect(second).toHaveLength(1); // SAME tab — not two, no churn
    const m = (second[0].blocks[0].data as { sourceKey?: string; manifest?: { markers?: unknown[] } });
    expect(m.sourceKey).toBe(SK);
    expect((m.manifest?.markers ?? []).length).toBe(2); // content updated in place (shots lane added)
  });
});

describe('mergeServerRenders — stable tab identity (lineage spec R8)', () => {
  const KEY = 'artifacts/s/render/view-1.json';
  const url = `/api/artifact?key=${encodeURIComponent(KEY)}`;

  it('every server tab carries its key; a composite also keeps compositeKey + memberKeys', () => {
    const merged = mergeServerRenders([], [
      { key: 'artifacts/s/render/a.json', createdAt: '2026-08-13T21:00:01Z' },
      { key: 'composite:asset:v1', createdAt: '2026-08-13T21:00:02Z', composite: true, memberKeys: ['x.json', 'y.json'], manifest: { tracks: [] } },
    ]);
    expect(merged.map(e => e.key)).toEqual(['artifacts/s/render/a.json', 'composite:asset:v1']);
    expect(merged[1].blocks[0].data).toMatchObject({ compositeKey: 'composite:asset:v1', memberKeys: ['x.json', 'y.json'] });
  });

  it('keeps a client-only Expand view the server has not listed, and replaces it once listed', () => {
    const view: RenderHistoryEntry = {
      turnIndex: 0, timestamp: Date.parse('2026-08-13T21:00:05Z'), label: 'nba.mp4', key: KEY, clientOnly: true, status: 'ready',
      blocks: [{ template: 'manifest-router', title: 'nba.mp4', data: { manifest: { template: 'asset-player' }, manifestUrl: url } }],
    };
    const other = { key: 'artifacts/s/render/shots.json', createdAt: '2026-08-13T21:00:01Z' };
    const kept = mergeServerRenders([view], [other]);
    expect(kept.map(e => e.key)).toEqual(['artifacts/s/render/shots.json', KEY]);

    const listed = mergeServerRenders(kept, [other, { key: KEY, label: 'nba.mp4', createdAt: '2026-08-13T21:00:05Z' }]);
    expect(listed).toHaveLength(2);
    const v = listed.find(e => e.key === KEY)!;
    expect(v.clientOnly).toBeUndefined();
    // Same block object: the mounted player keeps its inline manifest and is not reloaded.
    expect(v.blocks).toBe(view.blocks);
  });

  it('drops a saved-and-listed tab the server no longer lists (server stays authoritative)', () => {
    const saved = cachedEntry(KEY, 1000, 'V');
    const merged = mergeServerRenders([saved], [{ key: 'artifacts/s/render/b.json', createdAt: '2026-08-13T21:00:01Z' }]);
    expect(merged.map(e => e.key)).toEqual(['artifacts/s/render/b.json']);
  });
});

describe('mergeServerRenders — every tab keeps what it is about (design A: one tab per subject)', () => {
  it("uses the server's subjectIds, else derives them from the inline manifest", () => {
    const merged = mergeServerRenders([], [
      { key: 'composite:loc:x', createdAt: '2026-11-14T00:00:01Z', composite: true, subjectIds: ['loc:s3://b/x.mp4'], manifest: { template: 'nle-timeline' } },
      { key: 'artifacts/s/render/v.json', createdAt: '2026-11-14T00:00:02Z', manifest: { template: 'asset-player', ref: { asset_id: 'video:1' } } },
    ]);
    expect(merged.map((e) => e.subjectIds)).toEqual([['loc:s3://b/x.mp4'], ['asset:video:1']]);
  });
});
