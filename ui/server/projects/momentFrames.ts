// A moment's own picture: the frame of the shot it starts in.
//
// Indexing writes each video's Document_Record (`projects/<pid>/<assetId>/documents/<sha256>.jsonl`, named on the
// asset row as `doc.recordKey`) with one `shot` line per shot carrying `start_ms`, `end_ms` and the shot's
// representative frame (`fields.frame_s3_key`, from the 1 fps frame capture). Every moment — a shot, scene, topic
// or spoken line — starts inside one of those shots, so that shot's frame is its picture. The record is content
// addressed (immutable per key), so its shot list is cached by key.

export interface ShotFrame { s: number; e: number; key: string }

/** The shot lines of a Document_Record, sorted by start. Malformed lines are skipped. */
export function parseShotFrames(ndjson: string): ShotFrame[] {
  const out: ShotFrame[] = [];
  for (const line of ndjson.split("\n")) {
    if (!line.includes('"shot"')) continue;
    try {
      const d = JSON.parse(line) as { doc_type?: unknown; start_ms?: unknown; end_ms?: unknown; fields?: { frame_s3_key?: unknown } };
      const key = d.fields?.frame_s3_key;
      if (d.doc_type === "shot" && typeof d.start_ms === "number" && typeof d.end_ms === "number"
        && typeof key === "string" && key && !key.includes("..")) out.push({ s: d.start_ms, e: d.end_ms, key });
    } catch { /* skip */ }
  }
  return out.sort((a, b) => a.s - b.s);
}

/** The frame of the shot `atMs` falls in; else the shot starting nearest it; `null` when there are none. */
export function frameAt(shots: readonly ShotFrame[], atMs: number): string | null {
  let best: ShotFrame | null = null;
  for (const sh of shots) {
    if (sh.s <= atMs && atMs < sh.e) return sh.key;
    if (!best || Math.abs(sh.s - atMs) < Math.abs(best.s - atMs)) best = sh;
  }
  return best ? best.key : null;
}

const CACHE_MAX = 200;
const cache = new Map<string, ShotFrame[]>();

/** The shot list of `recordKey` (read once per key; a read failure is not cached). */
export async function shotFramesOf(recordKey: string, read: (key: string) => Promise<string>): Promise<ShotFrame[]> {
  const hit = cache.get(recordKey);
  if (hit) return hit;
  const shots = parseShotFrames(await read(recordKey));
  if (cache.size >= CACHE_MAX) cache.delete(cache.keys().next().value as string);
  cache.set(recordKey, shots);
  return shots;
}

export function resetMomentFrameCache(): void {
  cache.clear();
}
