/**
 * @vitest-environment node
 */
import { describe, it, expect } from 'vitest';
import { composeTimeline, groupBySourceKey } from './composeTimeline';
import type { RenderManifest } from './manifestParser';

const videoManifest = {
  id: 'vid', template: 'nle-timeline', duration: 131.766, createdAt: '2026-08-26T10:00:00Z',
  sourceKey: 'artifacts/s/clip.mp4',
  tracks: [{ id: 'video-transcoded', type: 'video', src: 'https://cdn/clip.mp4', start: 0, end: 131.766 }],
  clips: [],
} as unknown as RenderManifest;

const stemsManifest = {
  id: 'stem', template: 'nle-timeline', duration: 131.765, createdAt: '2026-08-26T10:05:00Z',
  sourceKey: 'artifacts/s/clip.mp4',
  tracks: [
    { id: 'audio-audio_speech', type: 'audio', src: 'https://cdn/speech.wav', start: 0, end: 131.765 },
    { id: 'audio-audio_music', type: 'audio', src: 'https://cdn/music.wav', start: 0, end: 131.765 },
    { id: 'audio-audio_sfx', type: 'audio', src: 'https://cdn/sfx.wav', start: 0, end: 131.765 },
  ],
  clips: [],
  peaks: { 'audio-audio_speech': [0.1, 0.2], 'audio-audio_music': [0.3], 'audio-audio_sfx': [0.4] },
} as unknown as RenderManifest;

const otherSource = { ...videoManifest, id: 'other', sourceKey: 'artifacts/s/original.mp4' } as unknown as RenderManifest;

describe('groupBySourceKey', () => {
  it('groups manifests that share a sourceKey; distinct keys stay separate', () => {
    const g = groupBySourceKey([videoManifest, stemsManifest, otherSource]);
    expect(g.get('artifacts/s/clip.mp4')).toHaveLength(2);
    expect(g.get('artifacts/s/original.mp4')).toHaveLength(1);
  });
  it('manifests without a sourceKey stand alone', () => {
    const a = { ...videoManifest, id: 'a' } as Record<string, unknown>; delete a.sourceKey;
    const b = { ...videoManifest, id: 'b' } as Record<string, unknown>; delete b.sourceKey;
    const g = groupBySourceKey([a as unknown as RenderManifest, b as unknown as RenderManifest]);
    // two solo groups (keyed by id), not merged
    expect(g.size).toBe(2);
  });
});

describe('composeTimeline', () => {
  it('returns a single manifest unchanged (no-op)', () => {
    expect(composeTimeline([videoManifest])).toBe(videoManifest);
  });

  it('merges video + stems into one composite: video-first tracks, merged peaks, max duration', () => {
    const c = composeTimeline([videoManifest, stemsManifest]);
    expect(c.template).toBe('composite-timeline');
    expect(c.tracks).toHaveLength(4);
    expect(c.tracks[0].type).toBe('video'); // video ordered first
    expect(c.tracks.filter(t => t.type === 'audio')).toHaveLength(3);
    expect(Object.keys(c.peaks ?? {})).toHaveLength(3);
    expect(c.duration).toBeCloseTo(131.766, 3); // max of the two
    expect((c as { sourceKey?: string }).sourceKey).toBe('artifacts/s/clip.mp4');
  });

  it('de-dupes tracks by id when the same manifest is merged twice', () => {
    const c = composeTimeline([stemsManifest, stemsManifest]);
    expect(c.tracks).toHaveLength(3);
  });

  it('adc293de shape: clip video + stems + shots + speakers + faces → ONE composite that KEEPS the video and gathers every lane', () => {
    // Post-sourceKey-fix, all analyses of the clip carry the CLIP's key, so they merge onto its
    // video instead of replacing it (the bug the user saw) or fragmenting into a tab each.
    const shots = {
      id: 'shots', template: 'video-markers', duration: 60, createdAt: '2026-08-26T10:06:00Z',
      sourceKey: 'artifacts/s/clip.mp4', tracks: [], clips: [],
      segments: Array.from({ length: 8 }, (_, i) => ({ id: `shot-${i}`, start: i * 7, end: i * 7 + 7, label: `shot ${i + 1}`, lane: 'Shots' })),
    } as unknown as RenderManifest;
    const speakers = {
      id: 'diar', template: 'composite-timeline', duration: 60, createdAt: '2026-08-26T10:07:00Z',
      sourceKey: 'artifacts/s/clip.mp4', tracks: [], clips: [],
      segments: [
        { id: 'sp0-a', start: 5, end: 12, label: 'Speaker 1', lane: 'Speaker 1' },
        { id: 'sp1-a', start: 12, end: 20, label: 'Speaker 2', lane: 'Speaker 2' },
      ],
    } as unknown as RenderManifest;
    const faces = {
      id: 'faces', template: 'video-annotations', duration: 60, createdAt: '2026-08-26T10:08:00Z',
      sourceKey: 'artifacts/s/clip.mp4', tracks: [{ id: 'video-transcoded', type: 'video', src: 'https://cdn/clip.mp4', start: 0, end: 60 }], clips: [],
      annotations: [{ id: 'reid', label: 'Reid Scott', box: [0.4, 0.2, 0.3, 0.4], start: 5, end: 15 }],
    } as unknown as RenderManifest;

    const c = composeTimeline([videoManifest, stemsManifest, shots, speakers, faces]);
    expect(c.template).toBe('composite-timeline');
    // the video is KEPT (deduped by id across videoManifest + faces) and ordered first
    expect(c.tracks[0].type).toBe('video');
    expect(c.tracks.filter(t => t.type === 'video')).toHaveLength(1);
    expect(c.tracks.filter(t => t.type === 'audio')).toHaveLength(3); // stems
    // shots + speaker turns gathered into segments (SegmentLanes groups them by lane)
    expect((c.segments ?? []).length).toBe(10);
    // face presence carried as annotations
    expect((((c as { annotations?: unknown[] }).annotations) ?? []).length).toBe(1);
    expect((c as { sourceKey?: string }).sourceKey).toBe('artifacts/s/clip.mp4');
  });
});

// IND403 Lab 1: a speaker relabel the agent wrote never replaced the diarization's UNKNOWN lane (first copy won).
describe('composeTimeline — the newest manifest of a lane set draws it', () => {
  const seg = (id: string, group: string, start: number, laneSet?: string) =>
    ({ id, name: 'line', start, end: start + 1, group, ...(laneSet ? { laneSet } : {}) });
  const at = (id: string, createdAt: string, extra: Record<string, unknown>) => ({
    id, template: 'nle-timeline', duration: 10, createdAt, sourceKey: 'artifacts/s/clip.mp4', tracks: [], clips: [], ...extra,
  }) as unknown as RenderManifest;
  const diarization = at('dia', '2026-11-14T10:00:00Z', {
    segments: [seg('speakers-0', 'SPEAKER_00', 1, 'speakers'), seg('speakers-1', 'UNKNOWN', 5, 'speakers')],
    captions: [{ start: 5, end: 6, speaker: 'UNKNOWN', text: 'Officer, please come in' }],
  });
  const faces = at('faces', '2026-11-14T10:01:00Z', { segments: [seg('people-0-0', 'Werner Vogels', 2, 'people')] });
  const relabel = at('lanes-speakers', '2026-11-14T10:05:00Z', {
    laneSet: 'speakers', segments: [seg('speakers-0', 'SPEAKER_00', 1, 'speakers'), seg('speakers-1', 'SPEAKER_01', 5, 'speakers')],
    captions: [{ start: 5, end: 6, speaker: 'SPEAKER_01', text: 'Officer, please come in' }],
  });
  const scenes = at('lanes-scenes', '2026-11-14T10:06:00Z', { laneSet: 'scenes', segments: [seg('scenes-0', 'Scenes', 0, 'scenes')] });

  it('a newer speakers set replaces the lanes and captions; faces and scenes keep their own rows', () => {
    const out = composeTimeline([videoManifest, diarization, faces, relabel, scenes]);
    expect(out.segments!.map((s) => s.group).sort()).toEqual(['SPEAKER_00', 'SPEAKER_01', 'Scenes', 'Werner Vogels']);
    expect(out.captions).toEqual([{ start: 5, end: 6, speaker: 'SPEAKER_01', text: 'Officer, please come in' }]);
    expect(out.tracks.map((t) => t.id)).toEqual(['video-transcoded']); // the video still comes from its own manifest
  });

  it('without a newer set the analysis lanes stay as they were', () => {
    const out = composeTimeline([videoManifest, diarization, faces]);
    expect(out.segments!.map((s) => s.group)).toEqual(['SPEAKER_00', 'UNKNOWN', 'Werner Vogels']);
  });

  it('an untagged segment (a view from before lane sets) is never replaced', () => {
    const legacy = at('old', '2026-11-01T10:00:00Z', { segments: [seg('s-0', 'UNKNOWN', 5)] });
    const out = composeTimeline([videoManifest, legacy, relabel]);
    expect(out.segments!.map((s) => s.group).sort()).toEqual(['SPEAKER_00', 'SPEAKER_01', 'UNKNOWN']);
  });

  it('a newer scenes BAND (markers) replaces the older one; the shots band is untouched', () => {
    const shots = at('shots', '2026-11-14T10:00:00Z', { markers: [{ id: 'm-0', time: 0, endTime: 5, label: '0' }] });
    const oldScenes = at('sc1', '2026-11-14T10:01:00Z', { markers: [{ id: 'scenes-0', time: 0, endTime: 10, label: '0', laneSet: 'scenes' }] });
    const newScenes = at('sc2', '2026-11-14T10:02:00Z', { markers: [
      { id: 'scenes-0', time: 0, endTime: 4, label: '1', laneSet: 'scenes' }, { id: 'scenes-1', time: 4, endTime: 10, label: '2', laneSet: 'scenes' }] });
    const out = composeTimeline([videoManifest, shots, oldScenes, newScenes]);
    expect(out.markers!.map((m) => `${(m as { laneSet?: string }).laneSet ?? 'shots'}:${m.label}`)).toEqual(['shots:0', 'scenes:1', 'scenes:2']);
  });
});
