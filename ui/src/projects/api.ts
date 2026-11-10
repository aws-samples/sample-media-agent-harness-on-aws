/**
 * `/projects` client (media-harness-projects-access task 13.1; design Component 1 route table,
 * Component 4 "Session project state", D18).
 *
 * One function per Project_Registry route (`ui/server/projects/routes.ts`) plus the Projects half of
 * `PATCH /sessions/:id {activeProjectId, webSearch}`. All calls go through `authFetch` (`/projects` and
 * `/sessions` are in `PROTECTED_PREFIXES`).
 *
 * Nothing here throws for an HTTP outcome. Every call resolves to a {@link ProjectsResult}:
 *   * `ok`          — the route's success body, unwrapped.
 *   * `disabled`    — `/config.projectsEnabled` is not `true`. NO request is made, so a build without
 *                     Projects (prod, D7) sends exactly the traffic it sent before this feature. A server
 *                     answer of `404 projects_not_enabled` maps here too.
 *   * `not_found`   — the uniform `404 project_not_found` (Req 1.17). The registry and `PATCH /sessions/:id`
 *                     answer every access denial with it, so callers cannot and need not tell "deleted" from
 *                     "removed" from "never existed".
 *   * `access_lost` — the `/api` turn gate's `409 project_access_lost` (Req 3.11), parsed by
 *                     {@link parseProjectAccessLost}. The gate has already moved the session row to
 *                     `personalProject`.
 *   * `error`       — anything else, with the server's `code` and `message` (and the raw error object, which
 *                     carries extra fields such as `limit` or `rule`). A network failure is `status: 0`.
 */
import { authFetch } from '../auth/authFetch';
import { noteFolderRenamed, noteMoved } from './assetFolders';
import { getConfig } from '../lab-api';
import { peekRuntimeConfig } from '../config/runtimeConfig';

// ------------------------------------------------------------------------------------------ wire types

import type { SearchKind } from '../shared/searchKinds';
export type ProjectRole = 'owner' | 'member';

/** One row of `GET /projects`. */
export interface ProjectSummary {
  projectId: string;
  name: string;
  role: ProjectRole;
  personal: boolean;
  /** Owned by the ingestion spec; absent means unknown, not zero. */
  activeAssetCount?: number;
  /** Search indexing ON: files are analyzed and searchable. Absent (an older proxy) = unknown, shown as off. */
  indexing?: boolean;
}

/** A Project's META view, as returned by create, rename, delete, and detail. */
export interface ProjectMeta {
  projectId: string;
  name: string;
  personal: boolean;
  status?: string;
  ownerSub?: string;
  memberCount?: number;
  createdAt?: string;
  deletedAt?: string;
  activeAssetCount?: number;
  collectionState?: string;
  /** Project settings. Absent on an older proxy: treat description as "", tags as [], indexing as ON. */
  description?: string;
  tags?: string[];
  /** OFF: files are stored and findable by name, not analyzed. The owner turns it ON once; never OFF. */
  indexing?: boolean;
  /** Files kept but not analyzed; what turning indexing on would queue. */
  storedCount?: number;
  /** Whether the Project has a knowledge graph. Absent (older proxy, or the feature is off) = no graph. */
  graph?: boolean;
}

/** Settings sent on create. */
export interface NewProjectSettings {
  description?: string;
  tags?: string[];
  indexing?: boolean;
  /** Build a knowledge graph for the Project. Sent only where `/config.graphEnabled` is true. */
  graph?: boolean;
}

/** An Owner or Member, shown by email or username (Req 1.20). */
export interface ProjectMember {
  sub: string;
  display: string;
  role: ProjectRole;
  addedAt: string;
}

/** `GET /projects/:pid`. `role` is the caller's. */
export interface ProjectDetail {
  project: ProjectMeta;
  role: ProjectRole;
  owner: ProjectMember;
  members: ProjectMember[];
}

/** The Personal_Project the turn gate switched the session to. */
export interface PersonalProjectRef {
  id: string;
  name: string;
}

/** The `{"error": {...}}` object every `/projects` failure carries. */
export interface ProjectsApiErrorBody {
  code: string;
  message: string;
  [extra: string]: unknown;
}

export type ProjectsResult<T> =
  | { ok: true; value: T }
  | { ok: false; kind: 'disabled' }
  | { ok: false; kind: 'not_found' }
  | { ok: false; kind: 'access_lost'; personalProject: PersonalProjectRef }
  | { ok: false; kind: 'error'; status: number; code: string; message: string; error?: ProjectsApiErrorBody };

export type ProjectsFailure = Exclude<ProjectsResult<never>, { ok: true }>;

/** The two Projects fields `PATCH /sessions/:id` accepts. Omitted keys are left unchanged. */
export interface SessionProjectPatch {
  activeProjectId?: string;
  webSearch?: boolean;
}

// --------------------------------------------------------------------- asset wire types (ingestion-owned routes)

/**
 * A Project_Asset's Ingestion_Status (media-harness-project-ingestion). Only `removed` is acted on by the
 * citation players — the Asset_URL_Route signs a URL for an asset in any status, so a status read is the only way
 * to detect a removal (design Component 5, "Opening a Verified_Citation"). The others are carried through verbatim.
 */
export type AssetIngestionStatus =
  | 'uploading'
  | 'queued'
  | 'analyzing'
  | 'indexing'
  | 'indexed'
  | 'ready'
  | 'failed'
  | 'removed'
  /** Indexing is OFF for the Project: kept and findable by name, not analyzed. */
  | 'stored';

/**
 * The four Project_Asset kinds. `document` is anything that is not video, image, or audio; it is registered and
 * listed now, and waits in `queued` until document indexing ships.
 */
export type ProjectAssetKind = 'video' | 'image' | 'audio' | 'document';

/**
 * Every kind an asset row can carry: the upload kinds plus `data` (JSON/CSV/text analysis files a session or an
 * M2C job produced; registered by the harness, never uploaded). A `data` asset has no thumbnail or player.
 */
export type AssetKind = ProjectAssetKind | 'data';

/** What an asset is for (asset-lineage design §2). Absent on a row means `source` (rows written before roles). */
export type AssetRole = 'source' | 'deliverable' | 'analysis' | 'intermediate';

/** An asset's role; a row with none (or an unknown value) is a `source`. */
export function assetRole(asset: { role?: unknown }): AssetRole {
  const r = asset.role;
  return r === 'deliverable' || r === 'analysis' || r === 'intermediate' ? r : 'source';
}

/**
 * `GET /projects/:pid/assets/:assetId` (ingestion Component 3) — the Project_Asset row. A denied or absent asset is
 * the uniform `404 project_not_found` ({@link ProjectsResult} `not_found`), so this value is only ever a found row.
 * Ingestion owns the full row; the players read `status` and `displayName`, and extra fields pass through.
 *
 * The same shape is the asset view returned by the list route, registration (`POST …/assets`), and an S3-location
 * submission. The optional fields are the server's pass-through fields; absent means unknown.
 */
export interface AssetDetail {
  assetId: string;
  displayName: string;
  kind: AssetKind;
  status: AssetIngestionStatus;
  contentType?: string;
  sourceSize?: number;
  registeredAt?: string;
  /** `upload`, `s3`, `generated` (legacy copies), `session` (a session output, in place), or `m2c` (by reference). */
  source?: string;
  /** Absent means `source` ({@link assetRole}). `analysis` and `intermediate` are hidden from the grid by default. */
  role?: AssetRole;
  /** `project` (absent) or `session`. */
  visibility?: 'project' | 'session';
  /** The session that produced (or owns) the asset, when it came from one. */
  sessionId?: string;
  /** A failed asset's reason: a plain string, or an object with a `message`/`reason`/`code`. */
  failure?: unknown;
  /**
   * Why a `stored` asset was deliberately left unindexed by an ingestion run (`no_speech`: audio with no speech,
   * e.g. a music or sound-effect stem). Not a failure. Absent on a file stored only because indexing is off.
   */
  indexSkippedReason?: string;
  /** A Personal sample (the demo tour's `0-demo.mp4`): the server lists it first, and the + menu keeps it first. */
  pinned?: boolean;
  /**
   * The folder the asset sits in, as a `/`-separated path (`Footage/Day 1`, `Sessions/<session title> · <date>`,
   * legacy `Generated/<session title> · <date>`). Absent (or empty) means the top level.
   */
  folder?: string;
  [extra: string]: unknown;
}

/** The asset view (list rows, registration result). Same as {@link AssetDetail}. */
export type AssetView = AssetDetail;

/** `GET /projects/:pid/assets?cursor=` — one newest-first page; `cursor` is absent on the last page. */
export interface AssetPage {
  assets: AssetView[];
  cursor?: string;
  /** The Project's search collection state: `absent` | `ready` | `rebuilding` | `rebuild_failed`. */
  collectionState?: string;
}

// ------------------------------------------------------------------------------------- upload wire types

/** `GET /projects/:pid/ingestion/capabilities` — what the Upload_Client validates against before any request. */
export interface IngestionCapabilities {
  maxFilesPerBatch: number;
  /** Largest accepted file per kind, in bytes. */
  limits: Record<ProjectAssetKind, number>;
  /** Files at or above this size go multipart. */
  multipartThresholdBytes: number;
  partSizeBytes: number;
}

/** One file of a `POST /projects/:pid/uploads` request. */
export interface UploadFileSpec {
  name: string;
  size: number;
  contentType: string;
  /**
   * The file's raw relative directory inside a dropped or picked folder (no file name), e.g. `Footage/Day 1`.
   * Omitted for a loose file. The server normalizes it and answers `400 invalid_folder` for one it refuses.
   */
  folder?: string;
}

/** A single presigned PUT for one file. The PUT must send exactly `headers` (the signed Content-Type). */
export interface SingleUploadGrant {
  index: number;
  assetId: string;
  key: string;
  mode: 'single';
  url: string;
  headers: Record<string, string>;
}

/** A multipart upload: one presigned UploadPart URL per part, `partSize` bytes each (the last holds the rest). */
export interface MultipartUploadGrant {
  index: number;
  assetId: string;
  key: string;
  mode: 'multipart';
  uploadId: string;
  partSize: number;
  parts: { partNumber: number; url: string }[];
}

export type UploadGrant = SingleUploadGrant | MultipartUploadGrant;

/**
 * `POST /projects/:pid/uploads` — a grant per accepted file, and a refusal per refused file keyed by the file's
 * index in the request. Each index is in exactly one of the two.
 */
export interface UploadGrantsResponse {
  uploads: UploadGrant[];
  errors: Record<string, { code: string; message?: string }>;
}

/** One finished part of a multipart upload: its number and the `ETag` response header of its PUT. */
export interface CompletedPart {
  partNumber: number;
  etag: string;
}

/** `POST /projects/:pid/s3-locations` — what one S3 location submission registered and skipped. */
export interface S3LocationResult {
  registered: AssetView[];
  skipped: { key: string; reason: string }[];
  errors: unknown[];
}

/**
 * `POST /projects/:pid/assets/:assetId/chat-copy` — the chat ATTACHMENT REFERENCE for a project asset
 * (asset-lineage Phase 2). Nothing is copied: the asset stays where it is and the chat references it by
 * `{projectId, assetId}`. `url` is a short-lived presigned GET of the asset's own object (in-browser preview,
 * and generators that can only fetch https); `sourceUri` is what the agent's tools read. `name`, `size`,
 * `contentType` and `key` are the `POST /upload`-shaped aliases, so a reference attaches exactly like an upload.
 */
export interface ChatCopyResult {
  key: string;
  url: string;
  name: string;
  size: number;
  contentType: string;
  /** Present when the server reports the bucket (as `POST /upload` does). */
  bucket?: string;
  /** The asset this attachment references (absent only from a pre-Phase-2 proxy). */
  assetId?: string;
  projectId?: string;
  displayName?: string;
  kind?: ProjectAssetKind;
  sizeBytes?: number;
  /** `s3://<bucket>/<key>` of the asset's own object. */
  sourceUri?: string;
  /** Epoch seconds at which `url` stops working. */
  expiresAt?: number;
}

/** One presigned GET minted by the Asset_URL_Route. `expiresAt` is epoch seconds (the signer's credential floor). */
export interface PresignedAssetUrl {
  url: string;
  expiresAt: number;
}

/** One `urls` entry of the Asset_URL_Route: the source bytes, plus a `rendition` for an image. */
export interface AssetUrlEntry {
  source: PresignedAssetUrl;
  rendition?: PresignedAssetUrl;
  /** Where the file is filed (`""` = the top level). Absent from an older proxy. */
  folder?: string;
}

/**
 * `POST /projects/:pid/assets/urls {asset_ids}` (the Asset_URL_Route, ingestion Req 11.10–11.12) — view-time
 * presigned URLs. A whole-route denial is the uniform `404` ({@link ProjectsResult} `not_found`). A per-id failure
 * (a malformed or absent id, or one the Actor cannot see) is the identical `project_not_found` entry in `errors`,
 * keyed by the asset id, and is never also present in `urls`. The caller finds each id in exactly one of the two
 * maps. The whole value is still `ok` because the route answered.
 */
export interface AssetUrlsResponse {
  urls: Record<string, AssetUrlEntry>;
  errors: Record<string, ProjectsApiErrorBody>;
}

// ------------------------------------------------------------------------------------------ enablement

/**
 * Whether Projects are on in this environment: `GET /config` `projectsEnabled === true`. Anything else
 * (absent, false, an unreadable `/config`) is off, so the client stays inert rather than calling routes
 * that would 404.
 */
export async function projectsEnabled(): Promise<boolean> {
  try {
    const cfg = (await getConfig()) as { projectsEnabled?: unknown };
    return cfg?.projectsEnabled === true;
  } catch {
    return false;
  }
}

/**
 * The Projects flag without waiting, or `null` before `/config` has loaded. Sign-in loads `/config` before
 * the workspace mounts, so seeding state with this renders the Projects tab in the same frame as the other
 * tabs instead of after a second round trip.
 */
export function peekProjectsEnabled(): boolean | null {
  const cfg = peekRuntimeConfig();
  return cfg ? cfg.projectsEnabled === true : null;
}

/** Whether the knowledge graph is deployed here (`/config.graphEnabled`); false until `/config` has loaded. */
export function peekGraphEnabled(): boolean {
  return peekRuntimeConfig()?.graphEnabled === true;
}

/** Awaiting form of {@link peekGraphEnabled}. */
export async function graphEnabled(): Promise<boolean> {
  try {
    const cfg = (await getConfig()) as { graphEnabled?: unknown };
    return cfg?.graphEnabled === true;
  } catch {
    return false;
  }
}

// ------------------------------------------------------------------------------------------ parsing

/**
 * Recognise the `/api` turn gate's `409 {"error":{"code":"project_access_lost","personalProject":{id,name}}}`.
 * Returns the Personal_Project the session now uses, or `null` for any other response.
 */
export function parseProjectAccessLost(status: number, body: unknown): PersonalProjectRef | null {
  if (status !== 409) return null;
  const err = (body as { error?: unknown } | null)?.error as
    | { code?: unknown; personalProject?: { id?: unknown; name?: unknown } }
    | undefined;
  if (!err || typeof err !== 'object' || err.code !== 'project_access_lost') return null;
  const p = err.personalProject;
  if (!p || typeof p.id !== 'string' || typeof p.name !== 'string') return null;
  return { id: p.id, name: p.name };
}

function failureFrom(status: number, body: unknown): ProjectsFailure {
  const lost = parseProjectAccessLost(status, body);
  if (lost) return { ok: false, kind: 'access_lost', personalProject: lost };

  const raw = (body as { error?: unknown } | null)?.error;
  if (raw && typeof raw === 'object') {
    const e = raw as Partial<ProjectsApiErrorBody>;
    const code = typeof e.code === 'string' ? e.code : 'http_error';
    if (status === 404 && code === 'project_not_found') return { ok: false, kind: 'not_found' };
    if (status === 404 && code === 'projects_not_enabled') return { ok: false, kind: 'disabled' };
    const message = typeof e.message === 'string' ? e.message : `Request failed (${status})`;
    return { ok: false, kind: 'error', status, code, message, error: { ...(e as object), code, message } };
  }
  // `/sessions` errors are `{"error": "<text>"}` (e.g. 423 "Session is locked").
  const message = typeof raw === 'string' ? raw : `Request failed (${status})`;
  const code = status === 423 ? 'session_locked' : 'http_error';
  return { ok: false, kind: 'error', status, code, message };
}

async function call<T>(
  path: string,
  init: { method?: string; body?: unknown },
  unwrap: (body: any) => T,
): Promise<ProjectsResult<T>> {
  if (!(await projectsEnabled())) return { ok: false, kind: 'disabled' };
  const headers: Record<string, string> = { accept: 'application/json' };
  const reqInit: RequestInit = { method: init.method ?? 'GET', headers };
  if (init.body !== undefined) {
    headers['content-type'] = 'application/json';
    reqInit.body = JSON.stringify(init.body);
  }
  let res: Response;
  try {
    res = await authFetch(path, reqInit);
  } catch (e) {
    const message = e instanceof Error ? e.message : String(e);
    return { ok: false, kind: 'error', status: 0, code: 'network_error', message };
  }
  const body = await res.json().catch(() => null);
  if (!res.ok) return failureFrom(res.status, body);
  return { ok: true, value: unwrap(body) };
}

const enc = encodeURIComponent;

// ------------------------------------------------------------------------------------------ routes

/** One `POST /projects/:pid/moments/search` result: cards in the Moments manifest's item shape. */
export interface MomentsSearchResult {
  query: string;
  kinds: SearchKind[] | null;
  items: unknown[];
  total: number;
}

/**
 * `POST /projects/:pid/moments/search {query, kinds?}` — the Moments tab's own search: the Search_Gateway
 * directly, no agent turn. `kinds` omitted (or every kind) means no filter.
 */
export function searchMoments(
  projectId: string,
  query: string,
  kinds?: SearchKind[],
): Promise<ProjectsResult<MomentsSearchResult>> {
  return call(`/projects/${enc(projectId)}/moments/search`, {
    method: 'POST',
    body: kinds && kinds.length ? { query, kinds } : { query },
  }, (b) => ({
    query: typeof b?.query === 'string' ? b.query : query,
    kinds: Array.isArray(b?.kinds) ? b.kinds : null,
    items: Array.isArray(b?.items) ? b.items : [],
    total: typeof b?.total === 'number' ? b.total : 0,
  }));
}

/** One query image as the moments search route takes it: an image asset, or a frame of a project video. */
export type MomentsSearchImage = { assetId: string } | { frameKey: string; sourceAssetId: string };

/**
 * `POST /projects/:pid/moments/search {images, match: "any"}` — search by up to 3 images (one M2C search each,
 * merged; each card's `matched_by` names the images that found it). A 409 `image_not_ready` means an image's
 * search rendition is still being made.
 */
export function searchMomentsByImages(
  projectId: string,
  images: MomentsSearchImage[],
  sessionId?: string | null,
): Promise<ProjectsResult<MomentsSearchResult>> {
  return call(`/projects/${enc(projectId)}/moments/search`, {
    method: 'POST',
    // The chat id lets the proxy make a missing search rendition on demand (Asset_Service search_images).
    body: { images, match: 'any', ...(sessionId ? { sessionId } : {}) },
  }, (b) => ({
    query: '',
    kinds: Array.isArray(b?.kinds) ? b.kinds : null,
    items: Array.isArray(b?.items) ? b.items : [],
    total: typeof b?.total === 'number' ? b.total : 0,
  }));
}

/** `GET /projects` — Personal first (the server ensures it exists), then the caller's other Projects. */
export function listProjects(): Promise<ProjectsResult<ProjectSummary[]>> {
  return call('/projects', {}, (b) => (Array.isArray(b?.projects) ? (b.projects as ProjectSummary[]) : []));
}

/** `POST /projects {name, description?, tags?, indexing?}` — 201 `{project}`. Indexing defaults OFF. */
export function createProject(name: string, settings: NewProjectSettings = {}): Promise<ProjectsResult<ProjectMeta>> {
  return call('/projects', { method: 'POST', body: { name, ...settings } }, (b) => b.project as ProjectMeta);
}

/** `PATCH /projects/:pid {description?, tags?}` — change settings (Owner only). */
export function updateProjectSettings(
  projectId: string,
  settings: { description?: string; tags?: string[] },
): Promise<ProjectsResult<ProjectMeta>> {
  return call(`/projects/${enc(projectId)}`, { method: 'PATCH', body: settings }, (b) => b.project as ProjectMeta);
}

/** What turning indexing on did: `queued` stored files were sent for analysis. */
export interface EnableIndexingResult {
  indexing: true;
  changed: boolean;
  queued: number;
}

/**
 * `POST /projects/:pid/ingestion/indexing` — turn indexing ON (Owner only; it cannot be turned off) and queue
 * every stored file. Idempotent, so retrying after an error queues whatever is left.
 */
export function enableIndexing(projectId: string): Promise<ProjectsResult<EnableIndexingResult>> {
  return call(`/projects/${enc(projectId)}/ingestion/indexing`, { method: 'POST', body: {} },
    (b) => b as EnableIndexingResult);
}

/** `GET /projects/:pid` — detail with Owner, Members, and the caller's role. */
export function getProject(projectId: string): Promise<ProjectsResult<ProjectDetail>> {
  return call(`/projects/${enc(projectId)}`, {}, (b) => b as ProjectDetail);
}

/** `PATCH /projects/:pid {name}` — rename (Owner only). */
export function renameProject(projectId: string, name: string): Promise<ProjectsResult<ProjectMeta>> {
  return call(`/projects/${enc(projectId)}`, { method: 'PATCH', body: { name } }, (b) => b.project as ProjectMeta);
}

/** `DELETE /projects/:pid {confirmName}` — `confirmName` must equal the name exactly (Req 1.22). */
export function deleteProject(projectId: string, confirmName: string): Promise<ProjectsResult<ProjectMeta>> {
  return call(
    `/projects/${enc(projectId)}`,
    { method: 'DELETE', body: { confirmName } },
    (b) => b.project as ProjectMeta,
  );
}

/** `POST /projects/:pid/members {invite}` — invite by email or username. */
export function inviteMember(projectId: string, invite: string): Promise<ProjectsResult<ProjectMember>> {
  return call(
    `/projects/${enc(projectId)}/members`,
    { method: 'POST', body: { invite } },
    (b) => b.member as ProjectMember,
  );
}

/** `DELETE /projects/:pid/members/:sub` — remove a Member (Owner only). */
export function removeMember(projectId: string, sub: string): Promise<ProjectsResult<{ sub: string }>> {
  return call(
    `/projects/${enc(projectId)}/members/${enc(sub)}`,
    { method: 'DELETE' },
    (b) => b.removed as { sub: string },
  );
}

/** `POST /projects/:pid/leave` — leave as a Member. */
export function leaveProject(projectId: string): Promise<ProjectsResult<{ sub: string }>> {
  return call(`/projects/${enc(projectId)}/leave`, { method: 'POST' }, (b) => b.left as { sub: string });
}

/**
 * `PATCH /sessions/:id {activeProjectId, webSearch}`. The proxy runs `checkAccess` before writing; a denial is
 * the uniform 404 (`not_found`) and nothing is written. A locked session answers 423 (`error`,
 * `code: "session_locked"`).
 */
export function patchSessionProject(
  sessionId: string,
  patch: SessionProjectPatch,
): Promise<ProjectsResult<void>> {
  const body: SessionProjectPatch = {};
  if (patch.activeProjectId !== undefined) body.activeProjectId = patch.activeProjectId;
  if (patch.webSearch !== undefined) body.webSearch = patch.webSearch;
  return call(`/sessions/${enc(sessionId)}`, { method: 'PATCH', body }, () => undefined);
}

/**
 * The code of the proxy's `409` for a project change on a session whose first turn already fixed its project
 * (D1, R2): from `PATCH /sessions/:id {activeProjectId}` and from a chat turn naming another project.
 */
export const SESSION_PROJECT_FIXED_CODE = 'session_project_fixed';

/** The failure is the `409 session_project_fixed` refusal (structured `{error:{code}}` or a bare `{error}` string). */
export function isSessionProjectFixed(r: ProjectsResult<unknown>): boolean {
  return !r.ok && r.kind === 'error' && r.status === 409
    && (r.code === SESSION_PROJECT_FIXED_CODE || r.message === SESSION_PROJECT_FIXED_CODE);
}

/**
 * A chat turn refused by the turn gate because the session is bound to another project (asset-lineage R2.1):
 * `409 {error: {code: 'session_project_fixed', projectId}}` (or the bare `{error: 'session_project_fixed'}`).
 * Returns the bound project id ('' when the body names none), or null for any other response.
 */
export function parseSessionProjectFixed(status: number, body: unknown): string | null {
  if (status !== 409) return null;
  const err = (body as { error?: unknown } | null)?.error;
  if (err === SESSION_PROJECT_FIXED_CODE) return '';
  if (!err || typeof err !== 'object') return null;
  const e = err as { code?: unknown; projectId?: unknown };
  if (e.code !== SESSION_PROJECT_FIXED_CODE) return null;
  return typeof e.projectId === 'string' ? e.projectId : '';
}

export const SESSION_PROJECT_GONE_CODE = 'session_project_gone';

/** `409 session_project_gone`: this chat's own project is gone, so it cannot continue. The Personal project to move to. */
export function parseSessionProjectGone(status: number, body: unknown): { id: string; name: string } | null {
  if (status !== 409) return null;
  const err = (body as { error?: unknown } | null)?.error;
  if (!err || typeof err !== 'object') return null;
  const e = err as { code?: unknown; personalProject?: { id?: unknown; name?: unknown } };
  if (e.code !== SESSION_PROJECT_GONE_CODE) return null;
  const p = e.personalProject;
  return p && typeof p.id === 'string' && p.id ? { id: p.id, name: typeof p.name === 'string' ? p.name : 'Personal' } : null;
}

// ------------------------------------------------------------------------------------------ asset routes

/**
 * `GET /projects/:pid/assets/:assetId` — the Project_Asset's current status and Display_Name (ingestion
 * Component 3). Read before opening a Citation or Expanding a Moment, because the Asset_URL_Route signs a URL for
 * an asset in any status (`removed` included), so a URL alone cannot detect a removal. A denied or absent asset is
 * the uniform `404` → `not_found`.
 *
 * Cross-spec: the backing route is owned by media-harness-project-ingestion; locally it is served by the dev
 * stand-in (Task 10.2). Only the client is added here.
 */
export function getAsset(projectId: string, assetId: string): Promise<ProjectsResult<AssetDetail>> {
  return call(`/projects/${enc(projectId)}/assets/${enc(assetId)}`, {}, (b) => b as AssetDetail);
}

/**
 * `POST /projects/:pid/assets/urls {asset_ids}` — the Asset_URL_Route. Mints a view-time presigned URL per id (and
 * a `rendition` for an image). A whole-route denial is the uniform `404` → `not_found`; per-id failures come back
 * in the result's `errors` map (`project_not_found`), keyed by asset id, with the value still `ok`.
 *
 * The route accepts at most 100 ids; coalescing concurrent requests and chunking to 100 is the caller's job
 * (`assetUrlBatcher`, Task 9.6). Cross-spec: owned by media-harness-project-ingestion; dev stand-in in Task 10.2.
 */
export function getAssetUrls(
  projectId: string,
  assetIds: string[],
): Promise<ProjectsResult<AssetUrlsResponse>> {
  return call(
    `/projects/${enc(projectId)}/assets/urls`,
    { method: 'POST', body: { asset_ids: assetIds } },
    (b) => ({
      urls: (b?.urls ?? {}) as Record<string, AssetUrlEntry>,
      errors: (b?.errors ?? {}) as Record<string, ProjectsApiErrorBody>,
    }),
  );
}

/** `GET /projects/:pid/assets?cursor=` — one newest-first page of the Project's assets (removed ones excluded). */
export function listAssets(projectId: string, cursor?: string | null): Promise<ProjectsResult<AssetPage>> {
  const qs = cursor ? `?cursor=${enc(cursor)}` : '';
  return call(`/projects/${enc(projectId)}/assets${qs}`, {}, (b) => ({
    assets: Array.isArray(b?.assets) ? (b.assets as AssetView[]) : [],
    ...(typeof b?.cursor === 'string' && b.cursor ? { cursor: b.cursor as string } : {}),
    ...(typeof b?.collectionState === 'string' ? { collectionState: b.collectionState as string } : {}),
  }));
}

/** `GET /projects/:pid/assets?q=&kind=&cursor=` — filename search, RANKED by relevance (exact Display_Name >
 *  prefix > substring, also matching the folder path at lowest weight) and PAGED by an opaque `cursor` so
 *  every match is reachable. Works whether indexing is on or off. `total` is the ranked match count;
 *  `cursor` is absent on the last page; `truncated` means the project is larger than the search scan bound. */
export function searchAssets(
  projectId: string,
  query: { q?: string; kind?: ProjectAssetKind; folder?: string },
  cursor?: string | null,
): Promise<ProjectsResult<{ assets: AssetView[]; truncated: boolean; total?: number; cursor?: string }>> {
  const params = new URLSearchParams();
  if (query.q && query.q.trim()) params.set('q', query.q.trim());
  if (query.kind) params.set('kind', query.kind);
  // `folder: ''` is the project root; `undefined` is "every folder".
  if (query.folder !== undefined) params.set('folder', query.folder);
  if (cursor) params.set('cursor', cursor);
  return call(`/projects/${enc(projectId)}/assets?${params.toString()}`, {}, (b) => ({
    assets: Array.isArray(b?.assets) ? (b.assets as AssetView[]) : [],
    truncated: b?.truncated === true,
    ...(typeof b?.total === 'number' ? { total: b.total as number } : {}),
    ...(typeof b?.cursor === 'string' && b.cursor ? { cursor: b.cursor as string } : {}),
  }));
}

/** One folder of `GET /projects/:pid/assets/folders`: its path and how many shown files it holds DIRECTLY. */
export interface FolderCount {
  folder: string;
  count: number;
}

/**
 * `GET /projects/:pid/assets/folders` — every folder holding a file the grid shows, complete (not just the
 * loaded pages). A hidden "this chat only" upload is not counted. `truncated`: the project is larger than the
 * scan bound. `rootCount`: what sits in no folder (the Top level row).
 */
export function listFolders(projectId: string): Promise<ProjectsResult<{ folders: FolderCount[]; truncated: boolean; rootCount?: number }>> {
  return call(`/projects/${enc(projectId)}/assets/folders`, {}, (b) => ({
    folders: Array.isArray(b?.folders) ? (b.folders as FolderCount[]) : [],
    truncated: b?.truncated === true,
    // Absent from an older proxy: unknown, so the Top level row shows no count rather than a wrong 0.
    ...(typeof b?.rootCount === 'number' ? { rootCount: b.rootCount } : {}),
  }));
}

/** One match of `POST /projects/:pid/assets/duplicates`: the request file at `index` is already `asset`. */
export interface DuplicateMatch {
  index: number;
  asset: AssetView;
}

/** The most files one `POST /projects/:pid/assets/duplicates` request accepts. */
export const MAX_DUPLICATE_CHECK_FILES = 1000;

/**
 * `POST /projects/:pid/assets/duplicates {files: [{name, size}]}` — which of up to 1000 files are already in the
 * Project (same derived name, ignoring the ` (n)` suffix, and same size). The server prefers the unsuffixed
 * original when several match. Files without a match are absent from `duplicates`.
 */
export function findDuplicatesBatch(
  projectId: string,
  files: { name: string; size: number }[],
): Promise<ProjectsResult<{ duplicates: DuplicateMatch[] }>> {
  return call(`/projects/${enc(projectId)}/assets/duplicates`, { method: 'POST', body: { files } }, (b) => ({
    duplicates: Array.isArray(b?.duplicates) ? (b.duplicates as DuplicateMatch[]) : [],
  }));
}

// ------------------------------------------------------------------------------------------ ingestion routes

/** `GET /projects/:pid/ingestion/capabilities` — batch size, per-kind limits, and the multipart threshold. */
export function getIngestionCapabilities(projectId: string): Promise<ProjectsResult<IngestionCapabilities>> {
  return call(`/projects/${enc(projectId)}/ingestion/capabilities`, {}, (b) => b as IngestionCapabilities);
}

/**
 * `POST /projects/:pid/uploads {files}` — 1 to 20 files; a presigned grant (or a refusal) per file. The upload
 * queue chunks larger batches into requests of at most 20.
 */
export function createUploads(
  projectId: string,
  files: UploadFileSpec[],
): Promise<ProjectsResult<UploadGrantsResponse>> {
  return call(`/projects/${enc(projectId)}/uploads`, { method: 'POST', body: { files } }, (b) => ({
    uploads: Array.isArray(b?.uploads) ? (b.uploads as UploadGrant[]) : [],
    errors: (b?.errors ?? {}) as UploadGrantsResponse['errors'],
  }));
}

/** `POST /projects/:pid/uploads/:assetId/complete {uploadId, parts}` — finish a multipart upload. */
export function completeUpload(
  projectId: string,
  assetId: string,
  uploadId: string,
  parts: CompletedPart[],
): Promise<ProjectsResult<void>> {
  return call(
    `/projects/${enc(projectId)}/uploads/${enc(assetId)}/complete`,
    { method: 'POST', body: { uploadId, parts } },
    () => undefined,
  );
}

/** `POST /projects/:pid/uploads/:assetId/abort {uploadId}` — abandon a multipart upload. */
export function abortUpload(projectId: string, assetId: string, uploadId: string): Promise<ProjectsResult<void>> {
  return call(
    `/projects/${enc(projectId)}/uploads/${enc(assetId)}/abort`,
    { method: 'POST', body: { uploadId } },
    () => undefined,
  );
}

/**
 * `POST /projects/:pid/assets {assetId, fileName, contentType, size}` — register an uploaded object as a
 * Project_Asset. `409 object_missing` / `size_mismatch` arrive as `error` with that `code`.
 *
 * `visibility: 'session'` + `sessionId` is a "this chat only" upload (asset-lineage D2): the asset belongs to
 * the project but is hidden from the Assets grid, filed in the session's `Sessions/…` folder, never indexed,
 * and deleted with the session.
 */
export function registerAsset(
  projectId: string,
  file: {
    assetId: string;
    fileName: string;
    contentType: string;
    size: number;
    visibility?: 'session';
    sessionId?: string;
  },
): Promise<ProjectsResult<AssetView>> {
  return call(`/projects/${enc(projectId)}/assets`, { method: 'POST', body: file }, (b) => b as AssetView);
}

/** `POST /projects/:pid/s3-locations {uri}` — register the objects at an `s3://bucket/key-or-prefix`. */
export function registerS3Location(projectId: string, uri: string): Promise<ProjectsResult<S3LocationResult>> {
  return call(`/projects/${enc(projectId)}/s3-locations`, { method: 'POST', body: { uri } }, (b) => ({
    registered: Array.isArray(b?.registered) ? (b.registered as AssetView[]) : [],
    skipped: Array.isArray(b?.skipped) ? (b.skipped as S3LocationResult['skipped']) : [],
    errors: Array.isArray(b?.errors) ? (b.errors as unknown[]) : [],
  }));
}

/**
 * `POST /projects/:pid/assets/:assetId/remove` — the asset becomes `removed`. An uploaded asset's object and every
 * chat copy are deleted; an S3-location asset is only unregistered.
 */
export function removeAsset(projectId: string, assetId: string): Promise<ProjectsResult<void>> {
  return call(`/projects/${enc(projectId)}/assets/${enc(assetId)}/remove`, { method: 'POST' }, () => undefined);
}

/** One asset an organize call could not file (`asset_not_found`: missing or removed; `invalid_folder`). */
export interface OrganizeFailure {
  assetId: string;
  code: string;
}

export interface MoveAssetsResult {
  /** The normalized destination (`""` = the project root). */
  folder: string;
  moved: string[];
  failed: OrganizeFailure[];
}

/** The most asset ids one move call takes (the Asset_Service's `_MOVE_MAX`). */
export const MOVE_ASSETS_MAX = 100;

/**
 * `POST /projects/:pid/assets/move` — file assets in `folder` (`""` = the project root). Metadata only: no bytes
 * move and nothing is re-indexed. At most {@link MOVE_ASSETS_MAX} ids per call.
 */
export function moveAssets(projectId: string, assetIds: string[], folder: string): Promise<ProjectsResult<MoveAssetsResult>> {
  return call(`/projects/${enc(projectId)}/assets/move`, { method: 'POST', body: { assetIds, folder } },
    (b) => b as MoveAssetsResult).then((r) => {
    // Every view showing these files says where they now live, without re-asking.
    if (r.ok && Array.isArray(r.value?.moved)) noteMoved(projectId, r.value.moved, r.value.folder ?? folder);
    return r;
  });
}

export interface RenameFolderResult {
  from: string;
  to: string;
  moved: string[];
  failed: OrganizeFailure[];
  /** Rows still to rename: call again with the same arguments while > 0. */
  remaining: number;
  /** Each item's old and new folder (absent from an older Asset_Service): what Undo puts back. */
  moves?: { assetId: string; from: string; to: string }[];
}

/**
 * `POST /projects/:pid/assets/rename-folder` — every asset in `from` or below it gets the `to` prefix instead
 * (`to: ""` lifts its contents to the root). Pages: call again while `remaining > 0`.
 */
export function renameFolder(projectId: string, from: string, to: string): Promise<ProjectsResult<RenameFolderResult>> {
  return call(`/projects/${enc(projectId)}/assets/rename-folder`, { method: 'POST', body: { from, to } },
    (b) => b as RenameFolderResult).then((r) => {
    if (r.ok) noteFolderRenamed(projectId, from, to);
    return r;
  });
}

/**
 * `POST /projects/:pid/assets/create-folder` — record an empty folder so every member, on every device, sees it
 * before anything is filed in it. Idempotent.
 */
export function createFolder(projectId: string, folder: string): Promise<ProjectsResult<{ folder: string }>> {
  return call(`/projects/${enc(projectId)}/assets/create-folder`, { method: 'POST', body: { folder } },
    (b) => b as { folder: string });
}

/**
 * `POST /projects/:pid/assets/resolve-locations` — chat or Renderer URLs (CDN, `s3://`, presigned, M2C
 * references) to the asset registered at each, `null` where none is (or it is another project's).
 */
export function resolveLocations(projectId: string, locations: string[]): Promise<ProjectsResult<Record<string, string | null>>> {
  return call(`/projects/${enc(projectId)}/assets/resolve-locations`, { method: 'POST', body: { locations } },
    (b) => ((b as { locations?: Record<string, string | null> }).locations ?? {}));
}

/**
 * `POST /projects/:pid/assets/:assetId/retry` — a `failed` asset goes back to `queued`; the pipeline reruns only
 * the steps that failed (or whose outputs are gone). 409 `not_failed` when it is no longer failed.
 */
export function retryAsset(projectId: string, assetId: string): Promise<ProjectsResult<void>> {
  return call(`/projects/${enc(projectId)}/assets/${enc(assetId)}/retry`, { method: 'POST' }, () => undefined);
}

/** `POST /projects/:pid/ingestion/rebuild` — retry a failed search-collection rebuild (Req 10.12, 10.13). */
export function retryRebuild(projectId: string): Promise<ProjectsResult<void>> {
  return call(`/projects/${enc(projectId)}/ingestion/rebuild`, { method: 'POST' }, () => undefined);
}

/** `POST /projects/:pid/assets/:assetId/chat-copy {sessionId}` — the attachment reference (no bytes are copied). */
export function chatCopyAsset(
  projectId: string,
  assetId: string,
  sessionId: string,
): Promise<ProjectsResult<ChatCopyResult>> {
  return call(
    `/projects/${enc(projectId)}/assets/${enc(assetId)}/chat-copy`,
    { method: 'POST', body: { sessionId } },
    (b) => b as ChatCopyResult,
  );
}

// ------------------------------------------------------------------------------------------ knowledge graph

/**
 * What the graph routes answer. `ready` carries data; `off` = the Project has no graph (the owner may enable
 * it); `seeding` = the first build is running (poll `stats`).
 */
export type GraphState = 'ready' | 'off' | 'seeding';

/**
 * `GET /projects/:pid/graph` — the whole graph in compact form: `nodes[i] = [id, typeIdx, name, sample01,
 * degree]`, `edges[j] = [srcIdx, dstIdx, typeIdx]` (indices into `nodes` / `types`).
 */
export interface GraphOverview {
  state: GraphState;
  generation?: number;
  updatedAt?: string;
  truncated?: boolean;
  totals?: { nodes: number; edges: number };
  types?: { nodes: string[]; edges: string[] };
  nodes?: [string, number, string, 0 | 1, number][];
  edges?: [number, number, number][];
}

export interface GraphStats {
  state: GraphState;
  generation?: number;
  updatedAt?: string;
  counts?: { nodes: number; edges: number };
}

export interface GraphNode {
  id: string;
  type: string;
  name: string;
  sample: boolean;
  kind?: string | null;
  status?: string | null;
  props: Record<string, unknown>;
}

export interface GraphNeighbourhood {
  state: GraphState;
  hops?: number;
  truncated?: boolean;
  node?: GraphNode;
  nodes?: GraphNode[];
  /** `[srcId, dstId, relationship]`. */
  edges?: [string, string, string][];
}

export function getProjectGraph(projectId: string): Promise<ProjectsResult<GraphOverview>> {
  return call(`/projects/${enc(projectId)}/graph`, {}, (b) => b as GraphOverview);
}

export function getProjectGraphStats(projectId: string): Promise<ProjectsResult<GraphStats>> {
  return call(`/projects/${enc(projectId)}/graph/stats`, {}, (b) => b as GraphStats);
}

export function getGraphNeighbourhood(projectId: string, nodeId: string, hops: 1 | 2 = 1):
  Promise<ProjectsResult<GraphNeighbourhood>> {
  return call(`/projects/${enc(projectId)}/graph/nodes/${enc(nodeId)}?hops=${hops}`, {}, (b) => b as GraphNeighbourhood);
}

/** `POST /projects/:pid/graph/enable` — Owner only, one way; the first build starts right after. */
export function enableProjectGraph(projectId: string): Promise<ProjectsResult<ProjectMeta>> {
  return call(`/projects/${enc(projectId)}/graph/enable`, { method: 'POST', body: {} }, (b) => b.project as ProjectMeta);
}

/** A saved moment: a range of a project asset, filed in a folder like a file (no bytes, never indexed). */
export interface Bookmark {
  bookmarkId: string;
  assetId: string;
  startMs: number;
  endMs?: number;
  label: string;
  folder?: string;
  assetKind?: string;
  assetDisplayName?: string;
  createdAt?: string;
}

export interface BookmarkInput {
  assetId: string;
  startMs: number;
  endMs?: number;
  label?: string;
}

/** `POST /projects/:pid/assets/bookmarks` — save moments in `folder` (`""` = the root), up to 100 per call. */
export function createBookmarks(projectId: string, items: BookmarkInput[], folder: string, sessionId?: string | null):
  Promise<ProjectsResult<{ folder: string; created: Bookmark[]; failed: OrganizeFailure[] }>> {
  return call(`/projects/${enc(projectId)}/assets/bookmarks`,
    { method: 'POST', body: { items, folder, ...(sessionId ? { sessionId } : {}) } },
    (b) => b as { folder: string; created: Bookmark[]; failed: OrganizeFailure[] });
}

/** `POST /projects/:pid/assets/remove-bookmarks` — delete saved moments (their assets are untouched). */
export function removeBookmarks(projectId: string, bookmarkIds: string[]): Promise<ProjectsResult<{ removed: string[]; missing: string[] }>> {
  return call(`/projects/${enc(projectId)}/assets/remove-bookmarks`, { method: 'POST', body: { bookmarkIds } },
    (b) => b as { removed: string[]; missing: string[] });
}

/** `GET /projects/:pid/assets/bookmarks?folder=` — the saved moments filed in `folder` (`""` = the root). */
export function listBookmarks(projectId: string, folder: string): Promise<ProjectsResult<Bookmark[]>> {
  return call(`/projects/${enc(projectId)}/assets/bookmarks?folder=${enc(folder)}`, {},
    (b) => (Array.isArray(b?.bookmarks) ? (b.bookmarks as Bookmark[]) : []));
}

/** What the import inbox (`projects/<pid>/import/`) did with the files copied there, and where to copy them. */
export interface ImportStatus {
  bucket: string;
  prefix: string;
  counts: { registered: number; kept: number; attention: number; ignored: number };
  attention: { key: string; reason: string; size: number }[];
  statusCounts: Record<string, number>;
  indexing: boolean;
}

/** `POST /projects/:pid/assets/import-status`. */
export function getImportStatus(projectId: string): Promise<ProjectsResult<ImportStatus>> {
  return call(`/projects/${enc(projectId)}/assets/import-status`, { method: 'POST', body: {} }, (b) => b as ImportStatus);
}

/** `POST /projects/:pid/assets/rescan-import` — sweep the inbox now (it is also swept once a minute). */
export function rescanImport(projectId: string): Promise<ProjectsResult<Record<string, number>>> {
  return call(`/projects/${enc(projectId)}/assets/rescan-import`, { method: 'POST', body: {} }, (b) => b as Record<string, number>);
}

// --------------------------------------------------------------------- asset lineage (projects.lineageEdges)
// Served by the Asset_Service `lineage` command (agent/src/ingestion/lineage.py) over the lineage edge records.

/** Whether lineage is on here (`/config.lineageEnabled`); false until `/config` has loaded. */
export function peekLineageEnabled(): boolean {
  return (peekRuntimeConfig() as { lineageEnabled?: unknown } | null)?.lineageEnabled === true;
}

/** Where an asset sits in its chain, from its edges alone (the stored `role` is by file type at registration). */
export type LineageStage = 'source' | 'intermediate' | 'final' | 'standalone';

export interface LineageNode {
  id: string;
  stage: LineageStage;
  parents: number;
  children: number;
  kind: string | null;
  displayName: string;
  role?: string | null;
  status?: string | null;
  source?: string | null;
  folder?: string;
  registeredAt?: string | null;
  sessionId?: string;
  tool?: string;
  jobId?: string;
  turnId?: string;
  visibility?: 'session';
  /** Hops from the focus asset: negative = an ancestor. Present only on a focused graph. */
  distance?: number;
  removed?: boolean;
  missing?: boolean;
  /**
   * Where the file's lineage begins: `upload` (uploaded by `addedByDisplay`), `import` (from `importedFrom`),
   * `external` (made only from media downloaded from outside), `derived`, `scratch` (its step read no media) or
   * `unrecorded` (generated, inputs not recorded — the one genuine gap), or `external_undecided` (downloaded from
   * outside, but from which of `candidates` could not be decided: no parent is claimed).
   */
  origin?: 'upload' | 'import' | 'external' | 'derived' | 'scratch' | 'unrecorded' | 'external_undecided';
  /** `external_undecided`: the URLs the download step named (credential-free). */
  candidates?: string[];
  addedByDisplay?: string;
  importedFrom?: string;
  /** An external source node (`ext:<hash>`): media that entered the Project from outside. */
  external?: boolean;
  url?: string;
  host?: string;
  license?: string;
  firstSeenAt?: string;
}

/** The parent's time range a child was made from: seconds, or frames. */
export interface LineageRange { start: number; end: number; unit: 's' | 'frame' }

/** An edge from an INPUT (`from`) to the asset made from it (`to`). */
export interface LineageEdge {
  from: string;
  to: string;
  tool?: string;
  jobId?: string;
  turnId?: string;
  sessionId?: string;
  via?: string;
  ranges?: LineageRange[];
  settings?: Record<string, unknown>;
  /** Inferred from a plan's dependencies / a turn's fetches, not observed. */
  inferred?: boolean;
  /** Stands for a path through files the viewer may not see (another member's "this chat only" file). */
  contracted?: boolean;
  /** A "group by chat" link: the chat that made the file (view-only, never stored). */
  chat?: boolean;
  /** `derivedFrom` when the edge is known only from the ASSET row (not yet backfilled). */
  recorded?: 'edge' | 'derivedFrom';
  createdAt?: string;
}

export interface LineageGap { child: string; ref: string; reason: string; tool?: string; jobId?: string; at?: string }

export interface LineageGraph {
  nodes: LineageNode[];
  edges: LineageEdge[];
  gaps: LineageGap[];
  stats: {
    nodes: number; edges: number; droppedEdges: number;
    assets?: number; generated?: number;
    /** Genuine gaps only: generated files whose inputs were not recorded. */
    generatedWithoutLineage?: number;
    origins?: { uploaded: number; imported: number; external: number; scratch: number; unrecorded: number;
      externalUndecided?: number };
    externalSources?: number;
  };
  /** Chat provenance (the proxy): the actor's own chats with their title and turn numbers. */
  chats?: Record<string, { mine: boolean; title?: string; turns?: Record<string, number> }>;
  truncated: boolean;
  direction: string;
  focus?: string;
}

export type LineageDirection = 'ancestors' | 'descendants' | 'both' | 'project';

export interface LineageQuery {
  assetId?: string;
  direction?: LineageDirection;
  depth?: number;
  maxNodes?: number;
  sessionId?: string;
}

/** `POST /projects/:pid/assets/lineage` — one asset's ancestors / descendants, or the whole Project. */
export function getProjectLineage(projectId: string, q: LineageQuery = {}): Promise<ProjectsResult<LineageGraph>> {
  return call(`/projects/${enc(projectId)}/assets/lineage`, { method: 'POST', body: q }, (b) => b as LineageGraph);
}
