/**
 * Which project assets a list of shown URLs are (a Renderer image gallery, a chat card): one `resolve-locations`
 * call per set of URLs, cached per project + URL, so a card can say where its file is filed and show its still.
 * `undefined` = still asking; a URL that is no asset of the project maps to `null`. A miss is not final — a file
 * made a moment ago is registered seconds later — so it is asked again on the fresh-file backoff ({@link askUntil});
 * it used to be cached as "no asset" for the life of the page.
 */
import { useEffect, useMemo, useState } from 'react';
import { resolveLocations } from './api';
import { askUntil } from './freshFile';

const cache = new Map<string, string | null>();
const key = (projectId: string, url: string) => `${projectId}\u0000${url}`;

export function useLocationAssets(projectId: string | null | undefined, urls: readonly string[]): Record<string, string | null> | undefined {
  const sig = urls.join('\n');
  const [tick, bump] = useState(0);
  useEffect(() => {
    if (!projectId || urls.length === 0) return;
    const unresolved = () => urls.filter((u) => (cache.get(key(projectId, u)) ?? null) === null).slice(0, 100);
    return askUntil(async () => {
      const missing = unresolved();
      if (missing.length === 0) return true;
      const r = await resolveLocations(projectId, missing);
      if (!r.ok) return true; // not cached: asked again next time the card shows
      for (const u of missing) cache.set(key(projectId, u), r.value[u] ?? null);
      bump((n) => n + 1);
      return unresolved().length === 0;
    }, (done) => done);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, sig]);
  return useMemo(() => {
    if (!projectId) return undefined;
    const out: Record<string, string | null> = {};
    for (const u of urls) {
      const k = key(projectId, u);
      if (!cache.has(k)) return undefined;
      out[u] = cache.get(k)!;
    }
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, sig, tick, cache.size]);
}

/** Test seam. */
export function _resetLocationAssets(): void { cache.clear(); }
