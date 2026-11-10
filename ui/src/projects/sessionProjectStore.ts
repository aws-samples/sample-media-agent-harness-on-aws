/**
 * Session project state (media-harness-projects-access task 13.1; design Component 4 "Session project
 * state", Data Models "SPA state", D18).
 *
 * An in-memory `Map<sessionId, {activeProjectId, activeProjectName, webSearch}>` shared by the composer's
 * Plus_Menu Project section and status line, and the Projects_Surface's `Use in this chat`, so a selection made on the
 * surface shows on the composer without `ChatCanvas` owning surface state (D18). Same module-store shape as
 * `services/notifications.ts`: immutable entries, a listener set, `useSyncExternalStore`.
 *
 *   * `seedSessionProject` — fill an entry from the session row when a session opens (Req 3.6). It never
 *     overwrites an entry this tab already holds, because a row read earlier may predate a selection.
 *     A session with no row yet is seeded `persisted: false`: until `markSessionPersisted`, changes stay in
 *     the tab (no PATCH, which would upsert an ownerless row) and reach the server with the first turn.
 *   * `selectActiveProject` — `PATCH /sessions/:id {activeProjectId}` (the proxy runs `checkAccess`) and
 *     update the entry ONLY on 200 (Req 3.3, 10.8). A not-found leaves the entry unchanged.
 *   * `resetToPersonal` — switch the entry to the Personal_Project and attach the lost-access notice
 *     (Req 3.11, 3.12). It also writes Personal to the session row, unless the caller says the server
 *     already did (the turn gate's 409).
 *   * `reconcileWithProjectList` — the open-time check the Plus_Menu and the Projects_Surface share: given a
 *     fresh `GET /projects`, fill the active name, or reset to Personal when the Active_Project is gone
 *     (Req 3.12).
 *   * `snapshot` — a frozen copy for submit time. ChatCanvas holds it for the life of the request, so a
 *     change mid-turn applies only to the next turn (Req 3.9).
 *   * `webSearch` is in every entry and `setWebSearch` persists it, for media-harness-web-search-toggle.
 *   * Session binding (asset-lineage D1, R2): once a session has a turn its project is fixed. An entry learns that
 *     from a `hasTurns` seed, {@link markSessionProjectFixed}, or the proxy's `409 session_project_fixed`, and then
 *     carries `projectFixed: true`; `selectActiveProject` of another project is refused without a request, and
 *     the caller offers a new chat in that project instead.
 *
 * Inert when `/config.projectsEnabled` is not `true`: no entry is ever created, every mutator returns
 * without a request, and `useSessionProject` / `snapshot` return `null`.
 */
import { useSyncExternalStore } from 'react';

import { peekRuntimeConfig } from '../config/runtimeConfig';
import { isSessionProjectFixed, patchSessionProject, projectsEnabled, SESSION_PROJECT_FIXED_CODE } from './api';
import type { PersonalProjectRef, ProjectSummary, ProjectsResult } from './api';

export interface SessionProjectState {
  /** `null` until known: the row predates Projects, or no list has been read yet. */
  activeProjectId: string | null;
  activeProjectName: string | null;
  webSearch: boolean;
}

export interface SessionProjectView extends SessionProjectState {
  /** The lost-access notice to show, or `null`. Cleared by `dismissNotice`. */
  notice: string | null;
  /** Present (and `true`) once the session's project is fixed by a turn (D1); absent while it may still switch. */
  projectFixed?: true;
}

/** What the session row carries. Both fields are optional: rows written before Projects have neither. */
export interface SessionProjectSeed {
  activeProjectId?: string | null;
  activeProjectName?: string | null;
  webSearch?: boolean | null;
  /**
   * `false` for a session with no row yet (rows are created lazily on the first input). Until
   * {@link markSessionPersisted}, changes stay in this tab and send no `PATCH /sessions/:id`: that route is an
   * upsert, so a PATCH first would write an ownerless row the later create could not replace. The first turn
   * carries the values as `active_project_id` / `web_search`, and the turn gate checks and persists them.
   * Defaults to `true` (seeded from a row).
   */
  persisted?: boolean;
  /** The session already has a turn, so its project is fixed (D1). */
  hasTurns?: boolean;
}

export interface LostAccessNotice {
  personalProject: PersonalProjectRef;
  /** Name of the Project access was lost to; defaults to the entry's current name. */
  lostProjectName?: string | null;
  /** True when the server already wrote Personal to the session row (the turn gate's 409). */
  alreadyPersisted?: boolean;
}

export type ReconcileOutcome = 'ok' | 'lost' | 'skipped';

/** The notice of Req 3.11. */
export function lostAccessMessage(lostProjectName?: string | null): string {
  const what = lostProjectName ? `"${lostProjectName}"` : 'the previously selected Project';
  return `You no longer have access to ${what}. This session has switched to your Personal project.`;
}

// ------------------------------------------------------------------------------------------ state

const entries = new Map<string, SessionProjectView>();
const listeners = new Set<() => void>();
/** Latest write per session, so an older PATCH answering late cannot overwrite a newer choice. */
const writeSeq = new Map<string, number>();
/** Sessions seeded with `persisted: false` whose row has not been created yet. */
const unpersisted = new Set<string>();
/** Sessions whose project a turn has fixed (D1). Kept apart from the entries so every write carries it. */
const fixed = new Set<string>();
let enabledPromise: Promise<boolean> | null = null;
let enabledKnown: boolean | null = null;

function isEnabled(): Promise<boolean> {
  if (!enabledPromise) {
    enabledPromise = projectsEnabled().then(
      (on) => {
        enabledKnown = on;
        return on;
      },
      () => {
        enabledKnown = false;
        return false;
      },
    );
  }
  return enabledPromise;
}

function emit(): void {
  for (const l of listeners) l();
}

function put(sessionId: string, next: SessionProjectView): void {
  const { projectFixed: _ignored, ...rest } = next;
  entries.set(sessionId, Object.freeze(fixed.has(sessionId) ? { ...rest, projectFixed: true as const } : rest));
  emit();
}

function bumpSeq(sessionId: string): number {
  const n = (writeSeq.get(sessionId) ?? 0) + 1;
  writeSeq.set(sessionId, n);
  return n;
}

const DISABLED = { ok: false, kind: 'disabled' } as const;
const PROJECT_FIXED: ProjectsResult<void> = Object.freeze({
  ok: false as const,
  kind: 'error' as const,
  status: 409,
  code: SESSION_PROJECT_FIXED_CODE,
  message: 'This chat already has turns in its project. Start a new chat to work in another one.',
});

// ------------------------------------------------------------------------------------------ reads

/** Current state for a session, or `null` (unknown session, or Projects disabled). */
export function getSessionProject(sessionId: string | null | undefined): SessionProjectView | null {
  if (!sessionId) return null;
  return entries.get(sessionId) ?? null;
}

/** Submit-time copy of the values a turn carries. Later store changes never mutate it (Req 3.9). */
export function snapshot(sessionId: string | null | undefined): Readonly<SessionProjectState> | null {
  const e = getSessionProject(sessionId);
  if (!e) return null;
  return Object.freeze({
    activeProjectId: e.activeProjectId,
    activeProjectName: e.activeProjectName,
    webSearch: e.webSearch,
  });
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** React binding. Re-renders when this session's entry changes. */
export function useSessionProject(sessionId: string | null | undefined): SessionProjectView | null {
  const get = () => getSessionProject(sessionId);
  return useSyncExternalStore(subscribe, get, get);
}

// ------------------------------------------------------------------------------------------ writes

/**
 * Seed a session's entry from its row when the session opens (Req 3.6). A no-op when Projects are disabled
 * or the entry already exists. Applied synchronously once enablement is known.
 */
export async function seedSessionProject(sessionId: string, seed: SessionProjectSeed = {}): Promise<void> {
  const apply = () => {
    if (!sessionId || entries.has(sessionId)) return;
    if (seed.persisted === false) unpersisted.add(sessionId);
    if (seed.hasTurns === true) fixed.add(sessionId);
    put(sessionId, {
      activeProjectId: seed.activeProjectId ?? null,
      activeProjectName: seed.activeProjectId ? (seed.activeProjectName ?? null) : null,
      // A chat that recorded a choice keeps it. A NEW chat (no row yet) starts with Web Search ON wherever it is
      // deployed (user decision 2026-10-20); the + menu turns it off per chat.
      webSearch: typeof seed.webSearch === 'boolean'
        ? seed.webSearch
        : seed.persisted === false && peekRuntimeConfig()?.webSearchAvailable === true,
      notice: null,
    });
  };
  if (enabledKnown === true) {
    apply();
    return;
  }
  if (await isEnabled()) apply();
}

/**
 * Make `projectId` the session's Active_Project. Sends `PATCH /sessions/:id {activeProjectId}`; the proxy
 * confirms access first. The entry changes only on 200, and only if no newer write for the session started
 * meanwhile. `projectName` labels the composer; pass it from the row the Actor picked.
 */
export async function selectActiveProject(
  sessionId: string,
  projectId: string,
  projectName?: string | null,
): Promise<ProjectsResult<void>> {
  if (!(await isEnabled())) return DISABLED;
  // A turn fixed this session's project (D1): another project is a new chat, never a switch.
  if (fixed.has(sessionId) && entries.get(sessionId)?.activeProjectId !== projectId) return PROJECT_FIXED;
  const seq = bumpSeq(sessionId);
  // No row yet: keep the choice in this tab; the first turn sends it and the turn gate checks access.
  const r: ProjectsResult<void> = unpersisted.has(sessionId)
    ? { ok: true, value: undefined }
    : await patchSessionProject(sessionId, { activeProjectId: projectId });
  // The proxy knows of a turn this tab did not (another tab, a reopened session): remember it.
  if (isSessionProjectFixed(r)) markSessionProjectFixed(sessionId);
  if (r.ok && writeSeq.get(sessionId) === seq) {
    const prev = entries.get(sessionId);
    put(sessionId, {
      activeProjectId: projectId,
      activeProjectName: projectName ?? (prev?.activeProjectId === projectId ? prev.activeProjectName : null),
      webSearch: prev?.webSearch ?? false,
      notice: null,
    });
  }
  return r;
}

/** Persist the Web_Search_Toggle value (`PATCH /sessions/:id {webSearch}`); the entry changes only on 200. */
export async function setWebSearch(sessionId: string, webSearch: boolean): Promise<ProjectsResult<void>> {
  if (!(await isEnabled())) return DISABLED;
  const r: ProjectsResult<void> = unpersisted.has(sessionId)
    ? { ok: true, value: undefined }
    : await patchSessionProject(sessionId, { webSearch });
  if (r.ok) {
    const prev = entries.get(sessionId);
    put(sessionId, {
      activeProjectId: prev?.activeProjectId ?? null,
      activeProjectName: prev?.activeProjectName ?? null,
      webSearch,
      notice: prev?.notice ?? null,
    });
  }
  return r;
}

/**
 * Switch the session to its Personal_Project and show the lost-access notice (Req 3.11, 3.12). The entry
 * changes immediately. Unless `notice.alreadyPersisted`, Personal is also written to the session row so a
 * reopen in another browser restores it (Req 3.6); that write is best-effort.
 */
export async function resetToPersonal(sessionId: string, notice: LostAccessNotice): Promise<void> {
  if (!(await isEnabled())) return;
  const prev = entries.get(sessionId);
  const lostName = notice.lostProjectName !== undefined ? notice.lostProjectName : (prev?.activeProjectName ?? null);
  bumpSeq(sessionId); // an in-flight selection must not land on top of the reset
  put(sessionId, {
    activeProjectId: notice.personalProject.id,
    activeProjectName: notice.personalProject.name,
    webSearch: prev?.webSearch ?? false,
    notice: lostAccessMessage(lostName),
  });
  if (!notice.alreadyPersisted && !unpersisted.has(sessionId)) {
    await patchSessionProject(sessionId, { activeProjectId: notice.personalProject.id });
  }
}

/**
 * The session has a turn, so its project is fixed (D1): later picks of another project are refused locally. An
 * unseeded session keeps the flag for its seed.
 */
export function markSessionProjectFixed(sessionId: string): void {
  if (!sessionId || fixed.has(sessionId)) return;
  fixed.add(sessionId);
  const prev = entries.get(sessionId);
  if (prev) put(sessionId, prev);
}

/** The session's row now exists (its create returned): later changes are written with `PATCH /sessions/:id`. */
export function markSessionPersisted(sessionId: string): void {
  unpersisted.delete(sessionId);
}

/**
 * Create a not-yet-saved session's row and write its Active_Project, for a server route that reads the row before
 * any turn has: a direct media action in a brand-new chat was refused `409 the chat has no active project` and
 * fell back to a model turn, so every first Clip / Crop / Frame of a chat ran the slow way. No-op once persisted.
 * `create` is `lab-api.createSession` (injected so this module stays free of it); it answers 200 for a row that
 * already exists, so a race with the first turn's own create is harmless.
 */
export async function persistSessionForAction(sessionId: string, create: (sessionId: string) => Promise<unknown>): Promise<void> {
  if (!sessionId || !unpersisted.has(sessionId)) return;
  await create(sessionId);
  markSessionPersisted(sessionId);
  const projectId = entries.get(sessionId)?.activeProjectId;
  if (projectId) await patchSessionProject(sessionId, { activeProjectId: projectId });
}

/**
 * The open-time check (Plus_Menu, Projects_Surface): given a fresh `GET /projects`, label the Active_Project,
 * or reset to Personal with the notice when it is missing from the list (Req 3.12). An entry with no
 * Active_Project yet takes Personal silently, matching the turn gate's fallback. Returns `skipped` when there
 * is no entry (unknown session, or Projects disabled) or the list has no Personal row.
 */
export async function reconcileWithProjectList(
  sessionId: string,
  projects: readonly ProjectSummary[],
): Promise<ReconcileOutcome> {
  const prev = entries.get(sessionId);
  const personal = projects.find((p) => p.personal);
  if (!prev || !personal) return 'skipped';
  const personalRef = { id: personal.projectId, name: personal.name };

  if (prev.activeProjectId === null) {
    put(sessionId, { ...prev, activeProjectId: personalRef.id, activeProjectName: personalRef.name });
    return 'ok';
  }
  const active = projects.find((p) => p.projectId === prev.activeProjectId);
  if (active) {
    if (active.name !== prev.activeProjectName) put(sessionId, { ...prev, activeProjectName: active.name });
    return 'ok';
  }
  // A chat that already has a row has had a turn, so its project is fixed: it ends with its project rather than
  // moving to Personal (the next message opens a new chat; the proxy answers `session_project_gone`).
  if (!unpersisted.has(sessionId)) {
    put(sessionId, { ...prev, notice: projectGoneMessage(prev.activeProjectName) });
    return 'lost';
  }
  await resetToPersonal(sessionId, { personalProject: personalRef });
  return 'lost';
}

/** The chat's own project is gone: what the old chat says, and what the new chat it opens says. */
export function projectGoneMessage(projectName?: string | null): string {
  const what = projectName ? `"${projectName}"` : 'This chat\'s project';
  return `${what} was deleted, so this chat can't continue. Your next message starts a new chat in Personal.`;
}

export function projectGoneNewChatMessage(projectName?: string | null): string {
  const what = projectName ? `"${projectName}"` : 'The previous chat\'s project';
  return `${what} was deleted, so this is a new chat in Personal.`;
}

/** Show `notice` on the session's composer (e.g. a chat opened because its project was deleted). */
export function setSessionNotice(sessionId: string, notice: string): void {
  const prev = entries.get(sessionId);
  if (prev) put(sessionId, { ...prev, notice });
}

/** Clear the lost-access notice once shown. */
export function dismissNotice(sessionId: string): void {
  const prev = entries.get(sessionId);
  if (prev?.notice) put(sessionId, { ...prev, notice: null });
}

/** Drop a session's entry (e.g. on delete). */
export function forgetSessionProject(sessionId: string): void {
  writeSeq.delete(sessionId);
  unpersisted.delete(sessionId);
  fixed.delete(sessionId);
  if (entries.delete(sessionId)) emit();
}

export const sessionProjectStore = {
  subscribe,
  get: getSessionProject,
  snapshot,
  seed: seedSessionProject,
  markPersisted: markSessionPersisted,
  markProjectFixed: markSessionProjectFixed,
  selectActiveProject,
  setWebSearch,
  resetToPersonal,
  reconcileWithProjectList,
  dismissNotice,
  forget: forgetSessionProject,
};

/** Test-only reset, following the `__resetXForTests` convention. */
export function __resetSessionProjectStoreForTests(): void {
  entries.clear();
  writeSeq.clear();
  unpersisted.clear();
  fixed.clear();
  enabledPromise = null;
  enabledKnown = null;
  emit();
}
