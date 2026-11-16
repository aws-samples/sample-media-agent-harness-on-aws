/**
 * composeTimeline — merge sibling render manifests that describe the SAME source
 * media into one composite timeline, so an analysis (stems, shots, faces,
 * diarization) shows up as lanes on the source's timeline instead of a detached
 * tab. Pure + deterministic.
 *
 * SHARED between the React UI and the proxy server (the `/sessions/:id/renders`
 * endpoint composes per-source composites SERVER-SIDE, so the UI renders one
 * finished composite per source and never merges client-side). The only import
 * is `import type` (erased by esbuild), so this module has NO runtime deps and
 * bundles cleanly into the proxy.
 *
 * Merge rules (union, video-first, dedup by id):
 *  - tracks: union by id, ordered video → audio → overlay → subtitle so the
 *    editor's primary media stays the video.
 *  - peaks: object-merged (per-track).
 *  - segments / markers / clips: concatenated, de-duped by id.
 *  - annotations: unioned by id (face boxes survive onto the shared video).
 *  - captions: concatenated, de-duped by (start, speaker, text) — they have no id.
 *  - duration: max across inputs (a longer analysis must not truncate the ruler).
 */
import type { RenderManifest, ManifestTrack } from '../components/nle/manifestParser';

/** Group manifests by their `sourceKey`; manifests without one stand alone (keyed by id). */
export function groupBySourceKey(manifests: RenderManifest[]): Map<string, RenderManifest[]> {
  const groups = new Map<string, RenderManifest[]>();
  for (const m of manifests) {
    const raw = (m as { sourceKey?: unknown }).sourceKey;
    const key = typeof raw === 'string' && raw ? raw : `__solo__:${m.id ?? Math.random().toString(36)}`;
    const arr = groups.get(key) ?? [];
    arr.push(m);
    groups.set(key, arr);
  }
  return groups;
}

const TRACK_ORDER: Record<string, number> = { video: 0, audio: 1, overlay: 2, subtitle: 3 };

/**
 * Merge manifests sharing a source into one composite. A single manifest is
 * returned unchanged (no-op), so callers can always route through this.
 */
export function composeTimeline(manifests: RenderManifest[]): RenderManifest {
  if (manifests.length === 0) throw new Error('composeTimeline: no manifests');
  if (manifests.length === 1) return manifests[0];

  const byTrackId = new Map<string, ManifestTrack>();
  const byMarkerId = new Map<string, RenderManifest['markers'] extends (infer T)[] | undefined ? T : never>();
  const bySegmentId = new Map<string, NonNullable<RenderManifest['segments']>[number]>();
  const byClipId = new Map<string, RenderManifest['clips'][number]>();
  const byAnnotationId = new Map<string, Record<string, unknown>>();
  const annotationsSansId: Record<string, unknown>[] = [];
  const captionSeen = new Set<string>();
  const captions: NonNullable<RenderManifest['captions']> = [];
  let peaks: Record<string, number[]> = {};
  let duration = 0;
  let createdAt = '';
  let sourceKey: string | undefined;
  // The source identities the server folded on (sourceAssetId > sourceLocation > sourceKey) ride
  // onto the composite, so a composite still names the asset its lanes describe.
  let sourceAssetId: string | undefined;
  let sourceLocation: string | undefined;
  const metadata: Record<string, string | number | boolean> = {};

  // A LANE SET ("speakers", "people", "scenes") is shown as its NEWEST manifest drew it: a re-run or an agent
  // correction (update_timeline) replaces that set instead of adding beside it, which first-wins never let through,
  // and every other set stays. A segment names its set (`laneSet`, stamped by its render declaration), else its
  // manifest does; an untagged segment (a view rendered before sets existed) is never replaced. The captions are
  // the "speakers" set's.
  const setOf = (m: RenderManifest, s?: { laneSet?: unknown }): string => {
    const own = s?.laneSet ?? (m as { laneSet?: unknown }).laneSet;
    return typeof own === 'string' ? own : '';
  };
  const setsOf = (m: RenderManifest): Set<string> => new Set([setOf(m),
    ...(m.segments ?? []).map((s) => setOf(m, s as { laneSet?: unknown })),
    ...(m.markers ?? []).map((mk) => setOf(m, mk as { laneSet?: unknown }))].filter(Boolean));
  const laneOwner = new Map<string, RenderManifest>();
  for (const m of manifests) {
    for (const set of setsOf(m)) {
      const cur = laneOwner.get(set);
      if (!cur || (m.createdAt ?? '') >= (cur.createdAt ?? '')) laneOwner.set(set, m);
    }
  }
  const draws = (m: RenderManifest, set: string) => !set || laneOwner.get(set) === m;

  for (const m of manifests) {
    const drawsCaptions = !setsOf(m).has('speakers') || draws(m, 'speakers');
    duration = Math.max(duration, typeof m.duration === 'number' ? m.duration : 0);
    if (m.createdAt && m.createdAt > createdAt) createdAt = m.createdAt;
    const sk = (m as { sourceKey?: unknown }).sourceKey;
    if (typeof sk === 'string' && sk) sourceKey = sk;
    const said = (m as { sourceAssetId?: unknown }).sourceAssetId;
    if (!sourceAssetId && typeof said === 'string' && said) sourceAssetId = said;
    const sloc = (m as { sourceLocation?: unknown }).sourceLocation;
    if (!sourceLocation && typeof sloc === 'string' && sloc) sourceLocation = sloc;
    for (const t of m.tracks ?? []) if (t && t.id && !byTrackId.has(t.id)) byTrackId.set(t.id, t);
    if (m.peaks) peaks = { ...peaks, ...m.peaks };
    for (const mk of m.markers ?? []) {
      if (mk && mk.id && !byMarkerId.has(mk.id) && draws(m, setOf(m, mk as { laneSet?: unknown }))) byMarkerId.set(mk.id, mk);
    }
    for (const s of m.segments ?? []) {
      if (s && s.id && !bySegmentId.has(s.id) && draws(m, setOf(m, s as { laneSet?: unknown }))) bySegmentId.set(s.id, s);
    }
    for (const c of m.clips ?? []) if (c && c.id && !byClipId.has(c.id)) byClipId.set(c.id, c);
    // annotations (face boxes / overlay projections) are an untyped pass-through field — union them
    // by id so a merged composite still overlays every analysis's boxes on the shared video.
    const anns = (m as { annotations?: unknown }).annotations;
    if (Array.isArray(anns)) {
      for (const a of anns) {
        if (a && typeof a === 'object') {
          const id = (a as { id?: unknown }).id;
          if (typeof id === 'string' && id) { if (!byAnnotationId.has(id)) byAnnotationId.set(id, a as Record<string, unknown>); }
          else annotationsSansId.push(a as Record<string, unknown>);
        }
      }
    }
    if (drawsCaptions) for (const cue of m.captions ?? []) {
      const k = `${cue.start}|${cue.speaker}|${cue.text}`;
      if (!captionSeen.has(k)) { captionSeen.add(k); captions.push(cue); }
    }
    if (m.metadata) Object.assign(metadata, m.metadata);
  }

  const tracks = [...byTrackId.values()].sort(
    (a, b) => (TRACK_ORDER[a.type] ?? 9) - (TRACK_ORDER[b.type] ?? 9),
  );
  const markers = [...byMarkerId.values()];
  const segments = [...bySegmentId.values()];
  const clips = [...byClipId.values()];

  const out: RenderManifest = {
    id: manifests[0].id,
    template: 'composite-timeline',
    duration,
    tracks,
    clips,
    createdAt: createdAt || new Date().toISOString(),
  };
  if (captions.length) out.captions = captions;
  if (Object.keys(peaks).length) out.peaks = peaks;
  if (markers.length) out.markers = markers;
  if (segments.length) out.segments = segments;
  if (Object.keys(metadata).length) out.metadata = metadata;
  const annotations = [...byAnnotationId.values(), ...annotationsSansId];
  if (annotations.length) (out as { annotations?: unknown[] }).annotations = annotations;
  if (sourceKey) (out as { sourceKey?: string }).sourceKey = sourceKey;
  if (sourceAssetId) (out as { sourceAssetId?: string }).sourceAssetId = sourceAssetId;
  if (sourceLocation) (out as { sourceLocation?: string }).sourceLocation = sourceLocation;
  return out;
}
