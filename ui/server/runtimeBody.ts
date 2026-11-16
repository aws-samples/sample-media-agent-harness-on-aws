// runtimeBody.ts — the ONE place a browser-supplied body is turned into a Main Agent runtime payload.
//
// The runtime trusts a family of double-underscore keys because only the proxy is supposed to write
// them: `__actor_id` / `__actor_groups` (identity), `__turn_context` (project scope),
// `__media_action` (runs a timeline action with NO model turn and none of `/media/actions`' checks),
// `__plan_node` / `__plan_session_id` / `__plan_node_results` (plan-chainer context). Stamping the two
// identity keys over a client body is not enough — every OTHER reserved key the client sent rode
// through untouched. So the rule is namespace-wide: a client can never send a `__`-prefixed key; the
// proxy strips them all, then writes the ones it owns.
//
// `/api` and `/lab` both go through `prepareRuntimeBody` (`runtimeBody.test.ts`).

/** True for a key in the proxy-owned namespace. */
export function isReservedRuntimeKey(key: string): boolean {
  return key.startsWith("__");
}

/**
 * Parse a client body, drop every proxy-reserved key, then apply `stamp` (the proxy's own fields).
 * Returns `null` for a body that is not a JSON object — callers forward nothing in that case rather
 * than an unsanitized buffer.
 */
export function prepareRuntimeBody(
  raw: Buffer,
  stamp: (body: Record<string, unknown>) => void,
): Buffer | null {
  if (raw.length === 0) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw.toString("utf8"));
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
  const body = parsed as Record<string, unknown>;
  for (const key of Object.keys(body)) {
    if (isReservedRuntimeKey(key)) delete body[key];
  }
  stamp(body);
  return Buffer.from(JSON.stringify(body), "utf8");
}

// A chat session id as the SPA mints it (`generateSessionId` in ui/src/LabContext.tsx): a UUID, or
// the `session-<ms>-<base36>` fallback for a browser without `crypto.randomUUID`.
const CLIENT_SESSION_ID = /^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|session-\d{1,16}-[a-z0-9]{1,16})$/i;

/**
 * Whether `id` is shaped like a session id a browser may name before its row exists. Proxy-derived
 * runtime sessions (`lab-<sha256(actor)>`, `harness-catalog-<actor>`, `render-…`, `plan-session-…`,
 * `EXT_…`) never match, so `/api` cannot be pointed at another actor's warm container — the actor-
 * derived ids are computable from a user id, and their containers hold that actor's gateway session.
 */
export function isClientSessionId(id: string): boolean {
  return CLIENT_SESSION_ID.test(id);
}

export type ApiSessionDecision =
  | { kind: "ok"; locked: boolean }
  | { kind: "reject"; status: number; code: string; message: string };

/**
 * Who may run a turn on `sessionId`. `row` is the session row (`undefined` when there is none).
 *   - a row owned by `actorId` → ok (carrying its lock flag);
 *   - a row owned by anyone else → 403 (no reading/continuing/pinning another user's session);
 *   - no row → ok only for a client-shaped id (sessions are created lazily, on the first message).
 */
export function decideApiSession(
  sessionId: string,
  actorId: string | undefined,
  row: { actorId?: unknown; locked?: unknown; active?: unknown } | undefined,
): ApiSessionDecision {
  if (row) {
    const owner = typeof row.actorId === "string" ? row.actorId : "";
    if (!actorId || owner !== actorId) {
      return { kind: "reject", status: 403, code: "session_not_owned", message: "Session belongs to another user" };
    }
    // A deleted chat is gone everywhere: its row is soft-deleted (`active = 0`, a TTL), and no turn continues it.
    if (row.active === 0) {
      return { kind: "reject", status: 410, code: "session_deleted", message: "This chat was deleted" };
    }
    return { kind: "ok", locked: !!row.locked };
  }
  if (!isClientSessionId(sessionId)) {
    return { kind: "reject", status: 400, code: "invalid_session_id", message: "session_id is not a chat session id" };
  }
  return { kind: "ok", locked: false };
}
