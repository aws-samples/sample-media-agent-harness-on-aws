/**
 * Each shown video moment's OWN picture: the frame of the shot it starts in. Asked through the asset-URL batcher
 * ({@link requestFrames}), so it rides the same request as the cards' URLs (`useMomentUrls`). A card the route
 * cannot serve keeps its file's hero still.
 */
import { useEffect, useMemo, useState } from 'react';
import { requestFrames } from './assetUrlBatcher';
import type { MomentItem } from '../components/renderer/types';

export function useMomentFrames(items: readonly MomentItem[]): Record<string, string> {
  const asks = useMemo(
    () => items.filter((m) => m.kind === 'video' && m.ref?.project_id && m.ref?.asset_id)
      .map((m) => ({ id: m.id, pid: m.ref.project_id, asset_id: m.ref.asset_id, at_ms: Math.max(0, Math.round(m.start_ms)) })),
    [items],
  );
  const sig = asks.map((a) => `${a.pid}|${a.asset_id}@${a.at_ms}`).join(',');
  const [frames, setFrames] = useState<Record<string, string>>({});
  useEffect(() => {
    if (asks.length === 0) return;
    let live = true;
    const byProject = new Map<string, typeof asks>();
    for (const a of asks) byProject.set(a.pid, [...(byProject.get(a.pid) ?? []), a]);
    void Promise.all([...byProject].map(async ([pid, list]) => {
      const got = await requestFrames(pid, list.map(({ asset_id, at_ms }) => ({ asset_id, at_ms })));
      const out: Record<string, string> = {};
      for (const a of list) {
        const url = got.get(`${a.asset_id}@${a.at_ms}`);
        if (url) out[a.id] = url;
      }
      return out;
    })).then((parts) => { if (live) setFrames(Object.assign({}, ...parts)); });
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sig]);
  return frames;
}
