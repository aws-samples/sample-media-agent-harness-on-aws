/**
 * The ONE picture of a project file on a card: its rendition (`rendition.jpg`, the still indexing or the HeroFrame
 * Lambda writes for every image and video), else an image's own bytes. Never a video's source — that is the whole
 * file, and a card that mounts a player per file starves the browser's connections (Firefox, ~6 per host).
 * Batched with every other card's request through {@link request}. A still that does not exist YET (a video made a
 * moment ago) is asked for again on the fresh-file backoff ({@link askUntil}).
 */
import { useCallback, useEffect, useState } from 'react';
import { request } from './assetUrlBatcher';
import { askUntil } from './freshFile';

export function useHeroStill(
  projectId: string | undefined, assetId: string | undefined, kind: string, refreshKey?: unknown,
): { still: string | null; reload: () => void } {
  const [still, setStill] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    if (!projectId || !assetId || (kind !== 'image' && kind !== 'video')) { setStill(null); return; }
    return askUntil(() => request(projectId, [assetId]), (results) => {
      const out = results.get(assetId);
      const url = out?.ok ? out.entry.rendition?.url ?? (kind === 'image' ? out.entry.source?.url : undefined) ?? null : null;
      setStill(url);
      return !!url || !out?.ok; // found, or refused (gone, no access): nothing to wait for
    });
  }, [projectId, assetId, kind, nonce, refreshKey]);
  return { still, reload: useCallback(() => setNonce((n) => n + 1), []) };
}
