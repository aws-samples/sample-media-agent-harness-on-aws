/**
 * The URL-refresh state machine shared by the AssetPlayer (Component 5) and the
 * Moment cards (Component 7). A presigned asset URL is short-lived, so a tab or
 * card refreshes it when a load would otherwise use a URL that is too old or has
 * already failed. This module is pure: no React, no `fetch`, no DOM. Callers route
 * media-element events into `step` and execute the returned `mint` instruction.
 *
 * Design Property 20 ("Asset URL refresh state machine", Req 2.12, 3.27). For any
 * sequence of events with non-decreasing `now`, driving `step` from
 * `{ phase: 'minting' }`, the machine guarantees:
 *
 *   1. At most one refresh (`mint: true`) per load — a load being the span from a
 *      `load_start` to the next `load_start`.
 *   2. A `load_start` that leaves the state `ready` without a mint always had a URL
 *      obtained no more than `URL_MAX_AGE_MS` ago.
 *   3. `failed` is absorbing: no event leaves it, and it never returns `mint: true`,
 *      so a player cannot loop on mints.
 *   4. The `resume` (playback position + play intent) carried into `minting` equals
 *      the triggering event's `resume`, and is kept until the next `load_ok`.
 */

/** A minted URL is refreshed once it is older than 50 minutes (Req 2.12). */
export const URL_MAX_AGE_MS = 50 * 60 * 1000;

export interface MintedUrl {
  url: string;
  /** `Date.now()` at the moment the URL was obtained. */
  obtainedAt: number;
  /** Optional signer-reported expiry; the age check uses `obtainedAt`. */
  expiresAt?: number;
  /** A video's hero still (`rendition.jpg`), shown on its card so no card mounts a player. */
  poster?: string;
}

/** The playback position and play intent captured before a `src` swap. */
export interface Resume {
  time: number;
  playing: boolean;
}

export type FailReason = "not_found" | "removed" | "load_failed";

export type UrlState =
  | { phase: "minting"; resume: Resume | null }
  | { phase: "ready"; minted: MintedUrl; refreshUsed: boolean; resume: Resume | null }
  | { phase: "failed"; reason: FailReason };

export type UrlEvent =
  | { type: "minted"; minted: MintedUrl }
  | { type: "mint_failed"; reason: FailReason }
  | { type: "load_start"; now: number; resume: Resume } // mount, play, or seek
  | { type: "load_ok" } // loadedmetadata / image load
  | { type: "load_error"; resume: Resume }; // media or image error event

/** A minted URL is stale once it is older than `URL_MAX_AGE_MS`. */
export function isStale(m: MintedUrl, now: number): boolean {
  return now - m.obtainedAt > URL_MAX_AGE_MS;
}

/**
 * Advance the machine by one event. Returns the next state and whether the caller
 * must mint a fresh URL (`mint: true`). A mint is only ever requested from `ready`:
 * on a stale `load_start` or on the first `load_error` of a load.
 */
export function step(s: UrlState, e: UrlEvent): { state: UrlState; mint: boolean } {
  switch (s.phase) {
    case "failed":
      // Absorbing (invariant 3): nothing leaves `failed`, nothing mints.
      return { state: s, mint: false };

    case "minting":
      switch (e.type) {
        case "minted":
          // A refresh mint always carries a resume (every `load_start` and
          // `load_error` does); only the initial mount mint has `resume === null`.
          // That distinction sets `refreshUsed`, spending this load's one refresh.
          return {
            state: {
              phase: "ready",
              minted: e.minted,
              refreshUsed: s.resume !== null,
              resume: s.resume,
            },
            mint: false,
          };
        case "mint_failed":
          return { state: { phase: "failed", reason: e.reason }, mint: false };
        default:
          // A load_start / load_ok / load_error arriving while a mint is in flight
          // keeps the pending resume (invariant 4: kept until the next load_ok) and
          // never triggers a second mint.
          return { state: s, mint: false };
      }

    case "ready":
      switch (e.type) {
        case "load_start":
          if (isStale(s.minted, e.now)) {
            // This load's one refresh (invariant 1).
            return { state: { phase: "minting", resume: e.resume }, mint: true };
          }
          // Fresh URL (invariant 2): a new load restores the one-refresh budget.
          return {
            state: { phase: "ready", minted: s.minted, refreshUsed: false, resume: s.resume },
            mint: false,
          };
        case "load_error":
          if (!s.refreshUsed) {
            // First error of this load: refresh once.
            return { state: { phase: "minting", resume: e.resume }, mint: true };
          }
          // The load's refresh was already spent: give up (invariant 1 keeps us
          // from looping on mints).
          return { state: { phase: "failed", reason: "load_failed" }, mint: false };
        case "load_ok":
          // The (re)load landed: clear the carried resume (invariant 4). Keep
          // `refreshUsed` so a later error in the same load cannot mint again; the
          // budget is restored only by the next `load_start`.
          return {
            state: { phase: "ready", minted: s.minted, refreshUsed: s.refreshUsed, resume: null },
            mint: false,
          };
        default:
          // minted / mint_failed with a URL already in hand: ignore.
          return { state: s, mint: false };
      }
  }
}
