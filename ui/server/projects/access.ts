// Project access checks and the Personal_Project entry point (media-harness-projects-access task 6.3;
// design Component 1 "Exported functions", Interfaces provided to sibling specs; Req 1.17, 1.27, 2.1–2.3,
// 2.8).
//
// The interface the ingestion spec's `/upload` and registration routes, the turn gate, and
// `PATCH /sessions/:id` import:
//   * `checkAccess(actorSub, projectId)` — a malformed id is refused before any read; a well-formed id costs
//     ONE consistent `BatchGetItem` of META and `MEMBER#<actorSub>`. Every denial (malformed id, never
//     created, deleted, tombstoned, not a member) returns the same frozen `ACCESS_DENIED` object and writes
//     nothing, so a route answering `res.status(404).json({ error: r.error })` sends a byte-identical
//     `{"error":{"code":"project_not_found","message":"Project not found"}}` for all of them.
//   * `ensurePersonalProject(actorSub)` — `ProjectRegistry.ensurePersonal`: puts PERSONAL with
//     `attribute_not_exists`, and on a condition failure or a `TransactionConflict` re-reads PERSONAL
//     consistently and returns the winner (conflicts retry up to 3 times with full-jitter backoff). A
//     registry that stays busy, or cannot allocate an id, throws `ProjectRegistryError` (503 / 500).
//
// Store selection (resolved lazily on first use, never at import, so importing this module cannot throw):
//   * `PROJECTS_STORE=memory` — an in-process `InMemoryTransactStore`. DEV ONLY: local runs with no AWS.
//     State lives in this process and dies with it, and two proxy tasks would each hold a different
//     registry, so it is refused (with a warning) when `NODE_ENV=production`, as in the proxy image.
//   * otherwise `PROJECTS_TABLE` (+ optional `PROJECTS_DDB_ENDPOINT`) via `dynamoTransactStoreFromEnv`;
//   * neither → Projects are disabled: `projectsEnabled()` is false and `checkAccess` /
//     `ensurePersonalProject` throw `ProjectsDisabledError`. Callers check `projectsEnabled()` first
//     (`/projects` answers `404 projects_not_enabled`, the turn gate is skipped; design D7).
// Tests inject a store with `setProjectsStore(store, deps?)` and restore env resolution with
// `resetProjectsStore()`.
import { isProjectId } from "./ids";
import { InMemoryTransactStore } from "./inMemoryStore";
import { PROJECT_NOT_FOUND, ProjectRegistry, registryErrorStatus, toProjectMeta } from "./registry";
import type { ProjectMeta, ProjectRegistryDeps, RegistryError } from "./registry";
import { isTombstone, memberKey, metaKey } from "./schema";
import { dynamoTransactStoreFromEnv, keyId } from "./store";
import type { TransactStore } from "./store";

export { isProjectId } from "./ids";
export type { ProjectMeta } from "./registry";

/** The one not-found every denial carries (Req 1.17). */
export type ProjectNotFound = typeof PROJECT_NOT_FOUND;

export type AccessResult =
  | { ok: true; project: ProjectMeta; role: "owner" | "member" }
  | { ok: false; error: ProjectNotFound }; // one shape for every denial (Req 1.17)

/** The single denial result: every refusal returns this very object. */
export const ACCESS_DENIED: Extract<AccessResult, { ok: false }> = Object.freeze({
  ok: false as const,
  error: PROJECT_NOT_FOUND,
});

/** HTTP status and body of a denial: `404 {"error":{"code":"project_not_found","message":"Project not found"}}`. */
export const PROJECT_NOT_FOUND_STATUS = 404;
export const PROJECT_NOT_FOUND_BODY = Object.freeze({ error: PROJECT_NOT_FOUND });

/** Projects are not configured (no `PROJECTS_TABLE`, no dev `PROJECTS_STORE=memory`). */
export class ProjectsDisabledError extends Error {
  constructor() {
    super("Projects are not enabled (PROJECTS_TABLE is unset)");
    this.name = "ProjectsDisabledError";
  }
}

/** `ensurePersonalProject` could not complete: `registry_busy` (503, retryable) or `id_generation_failed` (500). */
export class ProjectRegistryError extends Error {
  readonly error: RegistryError;
  readonly status: number;
  constructor(error: RegistryError) {
    super(error.message);
    this.name = "ProjectRegistryError";
    this.error = error;
    this.status = registryErrorStatus(error);
  }
}

// ----------------------------------------------------------------------------------- store singleton

interface Configured {
  store: TransactStore;
  registry: ProjectRegistry;
}

/** `undefined` = not yet resolved from env; `null` = disabled. */
let configured: Configured | null | undefined;

/**
 * The store selected by env: dev `PROJECTS_STORE=memory` (refused under `NODE_ENV=production`), else
 * `PROJECTS_TABLE`, else null (disabled).
 */
export function projectsStoreFromEnv(env: NodeJS.ProcessEnv = process.env): TransactStore | null {
  if ((env.PROJECTS_STORE ?? "").trim().toLowerCase() === "memory") {
    if (env.NODE_ENV === "production") {
      console.warn("[projects] PROJECTS_STORE=memory is dev-only and is ignored when NODE_ENV=production");
    } else {
      console.warn("[projects] using the in-process InMemoryTransactStore (PROJECTS_STORE=memory, dev only)");
      return new InMemoryTransactStore();
    }
  }
  return dynamoTransactStoreFromEnv(env);
}

/**
 * AgentCore Web Search is deployed here (`WEB_SEARCH_ENABLED=true`, set by CDK only where env-config
 * `webSearch.enabled`): `/config.webSearchAvailable`, and the turn gate forces a turn's `web_search` off without it.
 */
export function webSearchFeatureEnabled(env: NodeJS.ProcessEnv = process.env): boolean {
  return (env.WEB_SEARCH_ENABLED ?? "").trim().toLowerCase() === "true";
}

/**
 * Asset lineage edges are on here (env-config `projects.lineageEdges`): the proxy task's INGESTION_CONFIG carries
 * `"lineageEdges": true` only where the flag is on. `/config.lineageEnabled` shows the Projects Lineage tab.
 */
export function lineageFeatureEnabled(env: NodeJS.ProcessEnv = process.env): boolean {
  const raw = env.INGESTION_CONFIG ?? "";
  if (!raw.includes("lineageEdges")) return false;
  try {
    return (JSON.parse(raw) as { lineageEdges?: unknown })?.lineageEdges === true;
  } catch {
    return false;
  }
}

/** The knowledge-graph feature flag (`GRAPH_ENABLED=true`, set by CDK only where the graph stack exists). */
export function graphFeatureEnabled(env: NodeJS.ProcessEnv = process.env): boolean {
  return (env.GRAPH_ENABLED ?? "").trim().toLowerCase() === "true";
}

function current(): Configured | null {
  if (configured === undefined) {
    const store = projectsStoreFromEnv();
    configured = store ? { store, registry: new ProjectRegistry({ store, graphFeature: graphFeatureEnabled() }) } : null;
  }
  return configured;
}

function configuredOrThrow(): Configured {
  const c = current();
  if (!c) throw new ProjectsDisabledError();
  return c;
}

/** Whether a projects store is configured. */
export function projectsEnabled(): boolean {
  return current() !== null;
}

/** Use `store` (null disables Projects) instead of env, with optional registry deps. For tests. */
export function setProjectsStore(store: TransactStore | null, deps: Omit<ProjectRegistryDeps, "store"> = {}): void {
  configured = store ? { store, registry: new ProjectRegistry({ ...deps, store }) } : null;
}

/** Forget any injected store; the next call resolves from env again. */
export function resetProjectsStore(): void {
  configured = undefined;
}

/** The configured registry, for the `/projects` routes (throws `ProjectsDisabledError` when disabled). */
export function projectRegistry(): ProjectRegistry {
  return configuredOrThrow().registry;
}

// ----------------------------------------------------------------------------------- exported functions

/**
 * Whether `actorSub` may use `projectId`: META exists, is not a tombstone, is active, and the Actor's
 * MEMBER item exists. One consistent BatchGet; never writes. Every denial is `ACCESS_DENIED`.
 */
export async function checkAccess(actorSub: string, projectId: string): Promise<AccessResult> {
  const { store } = configuredOrThrow();
  if (!isProjectId(projectId) || typeof actorSub !== "string" || actorSub.length === 0) return ACCESS_DENIED;
  const mk = metaKey(projectId);
  const ak = memberKey(projectId, actorSub);
  const got = new Map((await store.batchGet([mk, ak])).map((i) => [keyId(i), i]));
  const meta = got.get(keyId(mk));
  const me = got.get(keyId(ak));
  if (!meta || isTombstone(meta) || meta.status !== "active" || !me) return ACCESS_DENIED;
  return { ok: true, project: toProjectMeta(meta), role: me.role === "owner" ? "owner" : "member" };
}

/** The Actor's one Personal_Project, created on first use; concurrent first calls all get the winner (Req 2.1–2.3). */
export async function ensurePersonalProject(actorSub: string): Promise<ProjectMeta> {
  const r = await configuredOrThrow().registry.ensurePersonal(actorSub);
  if (!r.ok) throw new ProjectRegistryError(r.error);
  return r.value;
}
