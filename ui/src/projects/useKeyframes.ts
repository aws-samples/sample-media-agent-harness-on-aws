/**
 * A video file's keyframe times, for the timeline to snap a dragged range onto: a cut that starts on a keyframe is a
 * stream copy (instant and exact), so a range placed by eye is as fast as a shot boundary, and the clip starts where
 * the range shows (media_actions.copy_start). Read once per file (an object's index never changes); `[]` until
 * known, and for a file whose index is not read — the timeline then simply does not snap to it.
 */
import { useEffect, useState } from 'react';
import { getKeyframes } from './api';
import { useLocationAssets } from './useLocationAssets';

const cache = new Map<string, Promise<number[]>>();

export function useKeyframes(projectId: string | null | undefined, assetId: string | null | undefined): number[] {
  const [times, setTimes] = useState<number[]>([]);
  useEffect(() => {
    setTimes([]);
    if (!projectId || !assetId) return;
    const k = `${projectId}\u0000${assetId}`;
    let p = cache.get(k);
    if (!p) {
      p = getKeyframes(projectId, assetId).then((r) => {
        if (!r.ok) { cache.delete(k); return []; } // a failure is asked again next time the file is shown
        return r.value ?? [];
      });
      cache.set(k, p);
    }
    let live = true;
    void p.then((t) => { if (live) setTimes(t); });
    return () => { live = false; };
  }, [projectId, assetId]);
  return times;
}

/**
 * The keyframes of the file a timeline manifest shows: by its asset id, else by its canonical location (an agent-made
 * timeline names its file only by `sourceLocation` — the Meridian timeline on staging, whose drags never snapped).
 */
export function useTimelineKeyframes(projectId: string | null | undefined, manifest: unknown): number[] {
  const m = (manifest ?? {}) as { sourceAssetId?: unknown; sourceLocation?: unknown };
  const assetId = typeof m.sourceAssetId === 'string' && m.sourceAssetId ? m.sourceAssetId : null;
  const loc = !assetId && typeof m.sourceLocation === 'string' && m.sourceLocation.startsWith('s3://') ? m.sourceLocation : null;
  const byLocation = useLocationAssets(projectId, loc ? [loc] : []);
  return useKeyframes(projectId, assetId ?? (loc ? byLocation?.[loc] ?? null : null));
}

/** Test seam. */
export function _resetKeyframes(): void { cache.clear(); }
