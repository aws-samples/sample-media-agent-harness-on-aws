/**
 * What a render view is ABOUT — its subject — and the grouping of views into tabs: one tab per subject, every view
 * of that subject a version of the tab (design A, new-ui-design/right-panel-tabs.html).
 *
 * Shared by the proxy (`composeRenderTabs` stamps each view's ids) and the SPA (the panel groups the views it
 * holds, including the ones it authored and the server has not listed yet), so both sides agree on one rule.
 *
 * A view's ids come only from fields views already carry, never from template names:
 *   - the file it shows: `asset:<id>` (`sourceAssetId`, or an asset-player's `ref.asset_id`) and `loc:<s3 uri>`
 *     (`sourceLocation`); a path-shaped `sourceKey` too (`sk:`), which is how older and Render Agent timelines
 *     name their file. A bare file name is not an identity: two different uploads can share one;
 *   - else, for a view that shows no single file (a gallery), its title: `title:<title>`. A re-run of the same
 *     gallery becomes its next version rather than a second tab;
 *   - else nothing: the view is its own subject.
 */

const nonEmpty = (v: unknown): string => (typeof v === 'string' ? v.trim() : '');

export function viewSubjectIds(manifest: unknown): string[] {
  const m = (manifest ?? {}) as {
    sourceAssetId?: unknown; sourceLocation?: unknown; sourceKey?: unknown; title?: unknown; ref?: { asset_id?: unknown };
    subjectIds?: unknown;
  };
  // A view may say outright what it is about: the next version of a gallery names the gallery it revises.
  if (Array.isArray(m.subjectIds)) {
    const declared = m.subjectIds.filter((x): x is string => typeof x === 'string' && x.length > 0);
    if (declared.length) return declared;
  }
  const ids: string[] = [];
  const asset = nonEmpty(m.sourceAssetId) || nonEmpty(m.ref?.asset_id);
  if (asset) ids.push(`asset:${asset}`);
  const loc = nonEmpty(m.sourceLocation);
  if (loc) ids.push(`loc:${loc}`);
  const sk = nonEmpty(m.sourceKey);
  if (sk.includes('/')) ids.push(`sk:${sk}`);
  if (ids.length === 0) {
    const title = nonEmpty(m.title);
    if (title) ids.push(`title:${title}`);
  }
  return ids;
}

/**
 * What tells a view apart from the other versions of its tab, when it says: the query of a Moments search (its
 * words, or the image's name). Undefined for anything else — the switcher then names a version by its title.
 */
export function viewVersionNote(manifest: unknown): string | undefined {
  const g = (manifest as { galleryMoments?: { query?: unknown; queryRef?: { type?: unknown; q?: unknown; images?: unknown } } } | null)
    ?.galleryMoments;
  if (!g || typeof g !== 'object') return undefined;
  const ref = g.queryRef;
  if (ref?.type === 'image' && Array.isArray(ref.images)) {
    const names = ref.images.map((i) => (i as { label?: unknown })?.label).filter((x): x is string => typeof x === 'string' && !!x.trim());
    if (names.length) return names.join(' · ');
  }
  const q = ref?.type === 'text' && typeof ref.q === 'string' ? ref.q : typeof g.query === 'string' ? g.query : '';
  return q.trim() ? `“${q.trim()}”` : undefined;
}

/**
 * Group items whose ids overlap (transitively: a view naming a file's location joins a view naming the same file's
 * asset when a third names both). Returns the groups as lists of item indexes, each in input order, the groups
 * ordered by their first item. An item with no ids is a group of its own.
 */
export function groupBySubject<T>(items: readonly T[], idsOf: (item: T) => readonly string[]): number[][] {
  const parent = items.map((_, i) => i);
  const find = (i: number): number => {
    while (parent[i] !== i) { parent[i] = parent[parent[i]]; i = parent[i]; }
    return i;
  };
  const owner = new Map<string, number>();
  items.forEach((item, i) => {
    for (const id of idsOf(item)) {
      const j = owner.get(id);
      if (j === undefined) { owner.set(id, i); continue; }
      const a = find(i), b = find(j);
      if (a !== b) parent[Math.max(a, b)] = Math.min(a, b);
    }
  });
  const groups = new Map<number, number[]>();
  items.forEach((_, i) => {
    const r = find(i);
    const g = groups.get(r);
    if (g) g.push(i); else groups.set(r, [i]);
  });
  return [...groups.values()].sort((a, b) => a[0] - b[0]);
}
