/**
 * Personal samples (the demo tour's `0-demo.mp4`): `GET /projects` asks the Asset_Service to copy the configured
 * samples into the caller's own Personal project (`seed_personal_samples`, `agent/src/ingestion/personal_samples.py`).
 *
 * The Asset_Service holds all the rules (owner + Personal only, a marker per sample so a deleted sample never
 * comes back, a missing sample retried later, a no-op where nothing is configured). This side only decides WHEN:
 *
 *   * once per actor per proxy process — a success is remembered, so a project list costs one Lambda call per user
 *     per task, not one per page load. A failure is NOT remembered, so the next list retries;
 *   * bounded — the list waits at most {@link SEED_WAIT_MS} for the copy (so a brand-new user's first list, and the
 *     tour's first + menu, already show the demo), then answers without it; the seed carries on in the background;
 *   * never a failure of `GET /projects` — every error is logged and swallowed.
 *
 * It carries the caller's own User_Token: the Asset_Service verifies it and checks the caller owns the project.
 */
import { invokeAssetService, type AssetServiceCall, type AssetServiceReply } from "./assetServiceClient";

/** How long a project list waits for the first seed before answering without it. */
export const SEED_WAIT_MS = 5000;

type Invoke = (call: AssetServiceCall) => Promise<AssetServiceReply>;

const seeded = new Set<string>();
const inFlight = new Map<string, Promise<void>>();

/**
 * Seed `actor`'s Personal project `personalProjectId` once per process. Resolves when the seed finished or
 * {@link SEED_WAIT_MS} passed, whichever is first; never rejects.
 */
export async function ensurePersonalSamples(
  actor: string,
  personalProjectId: string,
  userToken: string | undefined,
  opts: { invoke?: Invoke; waitMs?: number } = {},
): Promise<void> {
  // No token (dev bypass) means the Asset_Service cannot verify the caller: skip rather than fail.
  if (!userToken || seeded.has(actor)) return;
  let run = inFlight.get(actor);
  if (!run) {
    const invoke = opts.invoke ?? invokeAssetService;
    run = invoke({ userToken, projectId: personalProjectId, command: "seed_personal_samples", args: {} })
      .then((reply) => {
        if (reply.ok) seeded.add(actor);
        else console.warn(`[projects] seed_personal_samples refused: ${reply.status} ${reply.error?.code ?? ""}`);
      })
      .catch((err: unknown) => {
        console.warn(`[projects] seed_personal_samples failed: ${err instanceof Error ? err.message : String(err)}`);
      })
      .finally(() => inFlight.delete(actor));
    inFlight.set(actor, run);
  }
  const waitMs = opts.waitMs ?? SEED_WAIT_MS;
  let timer: ReturnType<typeof setTimeout> | undefined;
  await Promise.race([run, new Promise<void>((resolve) => { timer = setTimeout(resolve, waitMs); })]);
  if (timer) clearTimeout(timer);
}

/** Test-only: forget which actors were seeded. */
export function __resetPersonalSamplesForTests(): void {
  seeded.clear();
  inFlight.clear();
}
