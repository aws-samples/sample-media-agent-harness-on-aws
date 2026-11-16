/**
 * A file made a moment ago (a clip, a crop, a generated image) is registered as an asset, and gets its still, a few
 * seconds AFTER its URL is first shown. Anything that looks a fresh file up asks again on this one backoff instead of
 * taking the first miss as final — a fresh clip's card stayed grey until a reload (staging 2026-11-15).
 */

/** When to ask again (ms after the previous ask). About a minute in all. */
export const FRESH_FILE_RETRY_MS = [2_000, 4_000, 8_000, 15_000, 30_000];

/** Run `ask` until `done(answer)` says there is nothing left to wait for, on {@link FRESH_FILE_RETRY_MS}.
 *  Returns the cancel: after it, nothing more is asked and `done` is not called again. */
export function askUntil<T>(ask: () => Promise<T>, done: (answer: T) => boolean): () => void {
  let live = true;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const go = (attempt: number) => {
    ask().then((answer) => {
      if (!live) return;
      if (!done(answer) && attempt < FRESH_FILE_RETRY_MS.length) timer = setTimeout(() => go(attempt + 1), FRESH_FILE_RETRY_MS[attempt]);
    }).catch(() => { /* a failed ask is asked again next time the card shows */ });
  };
  go(0);
  return () => { live = false; if (timer) clearTimeout(timer); };
}
