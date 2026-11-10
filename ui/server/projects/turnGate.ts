// The proxy turn gate (media-harness-projects-access task 10.2; design Component 2, Key flows 1 and 3, D6,
// D7; Req 1.17, 1.27, 2.1, 3.6, 3.8, 3.9, 3.11, 4.1, 4.11, 4.15, 4.17, 7.8, 9.5, 9.9, 9.10).
//
// `app.ts`'s `/api` handler calls `runTurnGate` after the session-lock gate and acts on the outcome:
//   * `skip`    — Projects are disabled (D7) or the request is not a chat turn (`prompt` + `session_id`, no
//                 `action`). The handler forwards the request exactly as it did before this feature.
//   * `reject`  — start no turn; send `status` + `body`. `409 project_access_lost` when the Actor lost the
//                 Active_Project (the session row has already been moved to the Personal_Project).
//                 `409 session_project_fixed` when the turn names a different project than the one the
//                 session's first turn fixed (D1, R2; see `projectChangeRefused`).
//   * `proceed` — stamp `turnContext` as `__turn_context` (replacing any client copy; `null` means remove
//                 it) and send `projectToken` (when present) ONLY in `PROJECT_TOKEN_HEADER`.
//
// Steps (design Component 2):
//   1. `active_project_id` / `web_search` from the body; else the session row's `activeProjectId` /
//      `webSearch`; else the Personal_Project / `true` (Web Search on by default where deployed).
//   2. `ensurePersonalProject` (creates it on a first turn, Req 2.1).
//   3. The Token_Issuer (2 s budget, inside the client) and, in parallel, `checkAccess` for the Project's
//      name. `not_found` (or a registry denial) → 409. Any other failure, including `unauthenticated`
//      (authMiddleware already validated the caller), → `project_access: "unavailable"` with no token.
//   4. Persist `{activeProjectId, webSearch}` on the session row.
//
// Session rows are only read and written when they belong to the Actor: the `/api` path does not otherwise
// check session ownership, so a foreign `session_id` must not let one actor write another's row. The
// Project_Token is never logged, never put in a returned body, and never put in `turnContext`.
import { GetCommand, UpdateCommand } from "@aws-sdk/lib-dynamodb";
import type { DynamoDBDocumentClient } from "@aws-sdk/lib-dynamodb";
import {
  checkAccess as defaultCheckAccess,
  ensurePersonalProject as defaultEnsurePersonal,
  graphFeatureEnabled,
  projectsEnabled as defaultProjectsEnabled,
  isProjectId,
  PROJECT_NOT_FOUND_BODY,
  PROJECT_NOT_FOUND_STATUS,
  ProjectRegistryError,
} from "./access";
import type { AccessResult, ProjectMeta } from "./access";
import { webSearchFeatureEnabled } from "./access";
import { projectPk } from "./schema";
import { dynamoTransactStoreFromEnv } from "./store";
import { issueProjectToken as defaultIssue } from "./tokenIssuerClient";
import type { IssueResult } from "./tokenIssuerClient";
import { buildTurnContext } from "./turnContext";
import type { TurnContext, TurnContextFindSimilar } from "./turnContext";

export const PROJECT_ACCESS_LOST_STATUS = 409;

// ---------------------------------------------------------------------------------------- session store

/** The Projects fields of a session row the gate reads. */
export interface TurnGateSessionRow {
  activeProjectId?: string;
  webSearch?: boolean;
  /**
   * The session has at least one persisted turn: its row's `transcript` tail is non-empty. The proxy writes that
   * tail for every chat turn (`transcriptStore.finalizeTurn`, including the throttled provisional writes during a
   * long stream), and the legacy client PATCH wrote it before that, so it is the one signal every session with a
   * turn carries. `lastActivityAt` is not one: every PATCH (a project pick included) bumps it.
   */
  hasTurns?: boolean;
}

// ------------------------------------------------------------------------- session ↔ project binding (R2)

export const SESSION_PROJECT_FIXED_STATUS = 409;
export const SESSION_PROJECT_FIXED_CODE = "session_project_fixed";

/** The 409 body for a project change on a session whose project is fixed; `projectId` is the bound one. */
export function sessionProjectFixedBody(projectId: string) {
  return {
    error: {
      code: SESSION_PROJECT_FIXED_CODE,
      message: "This chat already has turns in another project. Start a new chat to work in that project.",
      projectId,
    },
  };
}

export const SESSION_PROJECT_GONE_CODE = "session_project_gone";

/**
 * The 409 body for a turn in a chat whose OWN project is gone (deleted, or the Actor was removed from it). The chat is
 * fixed to that project, so it cannot continue anywhere: the SPA opens a new chat in `personalProject` instead. The
 * row is never re-pointed (that mixed two projects in one chat).
 */
export function sessionProjectGoneBody(projectId: string, personal: { projectId: string; name: string }) {
  return {
    error: {
      code: SESSION_PROJECT_GONE_CODE,
      message: "This chat's project no longer exists. Continue in a new chat.",
      projectId,
      personalProject: { id: personal.projectId, name: personal.name },
    },
  };
}

/**
 * A session's project is fixed by its first turn (D1): once the row records a turn AND a project, a request for
 * a different project is refused. A row with no turns yet (or a pre-Projects row with no project) may still
 * switch. `null` (absent, foreign, or unreadable row) never refuses: the caller fails open, as the gate does.
 */
export function projectChangeRefused(row: TurnGateSessionRow | null, requestedProjectId: string): boolean {
  return !!row && row.hasTurns === true && !!row.activeProjectId && row.activeProjectId !== requestedProjectId;
}

export interface TurnGatePersistFields {
  activeProjectId?: string;
  /** Epoch ms; written only when `activeProjectId` changes. */
  activeProjectSetAt?: number;
  webSearch?: boolean;
}

export interface TurnGateSessionStore {
  /** The Actor's row, or `null` when absent or owned by someone else. */
  read(sessionId: string, actorSub: string): Promise<TurnGateSessionRow | null>;
  /** Best-effort update of the Actor's own row; never creates a row. */
  persist(sessionId: string, actorSub: string, fields: TurnGatePersistFields): Promise<void>;
}

const isConditionFailure = (err: unknown) =>
  (err as { name?: string } | null)?.name === "ConditionalCheckFailedException";

/** The sessions-table implementation (`SESSIONS_TABLE`, key `sessionId`). */
export function dynamoSessionStore(ddb: DynamoDBDocumentClient, table: string): TurnGateSessionStore {
  async function readOwned(sessionId: string, actorSub: string, projection: string, names?: Record<string, string>) {
    const got = await ddb.send(new GetCommand({
      TableName: table,
      Key: { sessionId },
      ConsistentRead: true,
      ProjectionExpression: projection,
      ...(names ? { ExpressionAttributeNames: names } : {}),
    }));
    const item = got.Item as Record<string, unknown> | undefined;
    return item && item.actorId === actorSub ? item : null;
  }

  return {
    async read(sessionId, actorSub) {
      // Only the first transcript message's role: enough to tell "has a turn" without reading the whole tail.
      const item = await readOwned(
        sessionId, actorSub, "actorId, activeProjectId, webSearch, #tr[0].#role", { "#tr": "transcript", "#role": "role" },
      );
      if (!item) return null;
      const row: TurnGateSessionRow = {};
      if (typeof item.activeProjectId === "string" && item.activeProjectId) row.activeProjectId = item.activeProjectId;
      if (typeof item.webSearch === "boolean") row.webSearch = item.webSearch;
      if (Array.isArray(item.transcript) && item.transcript.length > 0) row.hasTurns = true;
      return row;
    },

    async persist(sessionId, actorSub, fields) {
      const sets: string[] = [];
      const values: Record<string, unknown> = { ":actor": actorSub };
      for (const [k, v] of Object.entries(fields)) {
        if (v === undefined) continue;
        sets.push(`${k} = :${k}`);
        values[`:${k}`] = v;
      }
      if (sets.length === 0) return;
      try {
        await ddb.send(new UpdateCommand({
          TableName: table,
          Key: { sessionId },
          UpdateExpression: `SET ${sets.join(", ")}`,
          // `actorId = :actor` also requires the row to exist, so this never creates a stub row.
          ConditionExpression: "actorId = :actor",
          ExpressionAttributeValues: values,
        }));
      } catch (err) {
        if (!isConditionFailure(err)) throw err;
      }
    },
  };
}

// ------------------------------------------------------------------------------------------------ gate

export interface TurnGateInput {
  /** The parsed request body (anything; non-objects are skipped). */
  body: unknown;
  actorSub: string | undefined;
  /** The raw `Authorization` header; its bearer token is the User_Token sent to the issuer. */
  authorization: string | undefined;
  sessionId: string;
  turnId: string;
}

export interface TurnGateDeps {
  sessions?: TurnGateSessionStore | null;
  projectsEnabled?: () => boolean;
  ensurePersonalProject?: (actorSub: string) => Promise<ProjectMeta>;
  checkAccess?: (actorSub: string, projectId: string) => Promise<AccessResult>;
  issueProjectToken?: (userToken: string, projectId: string) => Promise<IssueResult>;
  now?: () => number;
  warn?: (msg: string) => void;
  /**
   * Consistent reader of a Project's `ASSET#<id>` row, for resolving a Find_Similar reference (Req 3.23).
   * Defaults to a `BatchGetItem` over the Projects table from env. Returns `null` when absent or unreadable.
   */
  readProjectAsset?: (projectId: string, assetId: string) => Promise<FindSimilarAssetRow | null>;
  /** The Ingest/Skills bucket an image rendition and a `projects/`-prefixed frame live in (`SKILLS_S3_BUCKET`). */
  skillsBucket?: string;
  /** The M2C proxy bucket a non-`projects/` frame lives in (`MEDIA_PROXY_BUCKET`, MSQ-I2). */
  mediaProxyBucket?: string;
}

export type TurnGateOutcome =
  | { kind: "skip" }
  | { kind: "reject"; status: number; body: unknown }
  | {
      kind: "proceed";
      /** Stamp as `__turn_context`; `null` = remove any client copy and send none. */
      turnContext: TurnContext | null;
      /** Send in `PROJECT_TOKEN_HEADER` only. */
      projectToken: string | null;
    };

const SKIP: TurnGateOutcome = Object.freeze({ kind: "skip" as const });

/** A chat turn: a JSON object with a string `prompt`, a non-empty `session_id`, and no `action`. */
export function isChatTurn(body: unknown): body is Record<string, unknown> {
  if (!body || typeof body !== "object" || Array.isArray(body)) return false;
  const b = body as Record<string, unknown>;
  return typeof b.prompt === "string" && typeof b.session_id === "string" && b.session_id !== "" && b.action === undefined;
}

/** The token of an `Authorization: Bearer <token>` header, or "". */
export function bearerToken(authorization: string | undefined): string {
  if (typeof authorization !== "string") return "";
  const m = /^Bearer\s+(.+)$/i.exec(authorization.trim());
  return m ? m[1].trim() : "";
}

function describe(err: unknown): string {
  return (err instanceof Error ? err.message : String(err)).slice(0, 200);
}

// -------------------------------------------------------------------------------------- Find_Similar

/** The client-supplied `find_similar_ref` on a Find_Similar turn body (Req 3.23). */
export interface FindSimilarRef {
  project_id: string;
  asset_id: string;
  /** A `shot` Hit's frame key for a video; `null`/absent for an image. */
  frame_s3_key: string | null;
  kind: string;
  /** Advisory; the gate overrides Display_Name from the registry row. */
  display_name: string;
  /** The Citation timecode span; validated by the gate, dropped when it does not match. */
  timecode: string;
}

/** The `ASSET#<id>` row fields the Find_Similar resolver reads. Extra fields are ignored. */
export interface FindSimilarAssetRow {
  kind?: unknown;
  status?: unknown;
  displayName?: unknown;
  /** The image's searchable rendition; its `key` is the S3 object key under the Ingest bucket. */
  rendition?: unknown;
}

/** `find_similar.timecode` grammar (Citation span): `mm:ss`/`h:mm:ss` on each side, en-dash separated. */
const FIND_SIMILAR_TIMECODE = /^(?:\d{1,2}:)?[0-5]\d:[0-5]\d\u2013(?:\d{1,2}:)?[0-5]\d:[0-5]\d$/;

/** The 409 a Find_Similar on an image with no searchable rendition yet returns (design Component 7). */
export const FIND_SIMILAR_UNAVAILABLE_STATUS = 409;
export const FIND_SIMILAR_UNAVAILABLE_BODY = Object.freeze({
  error: { code: "find_similar_unavailable", message: "This image has no searchable rendition yet" },
});

/** The outcome of resolving a `find_similar_ref`: the stamped object, or a rejection to send instead. */
export type FindSimilarOutcome =
  | { ok: true; findSimilar: TurnContextFindSimilar }
  | { ok: false; status: number; body: unknown };

const FIND_SIMILAR_NOT_FOUND: FindSimilarOutcome = Object.freeze({
  ok: false as const,
  status: PROJECT_NOT_FOUND_STATUS,
  body: PROJECT_NOT_FOUND_BODY,
});

/** Parse a body's `find_similar_ref`; `null` when absent or malformed (the turn then runs without one). */
export function parseFindSimilarRef(raw: unknown): FindSimilarRef | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const r = raw as Record<string, unknown>;
  if (typeof r.project_id !== "string" || r.project_id === "") return null;
  if (typeof r.asset_id !== "string" || r.asset_id === "") return null;
  if (typeof r.kind !== "string" || r.kind === "") return null;
  return {
    project_id: r.project_id,
    asset_id: r.asset_id,
    frame_s3_key: typeof r.frame_s3_key === "string" ? r.frame_s3_key : null,
    kind: r.kind,
    display_name: typeof r.display_name === "string" ? r.display_name : "",
    timecode: typeof r.timecode === "string" ? r.timecode : "",
  };
}

/** A frame key the proxy will build a URI from: non-empty, no empty or `..` segment, no `%`, `?` or `#`. */
export function isValidFrameKey(key: unknown): key is string {
  if (typeof key !== "string" || key === "") return false;
  if (/[%?#]/.test(key)) return false;
  return key.split("/").every((seg) => seg !== "" && seg !== "..");
}

/** The searchable rendition's S3 key on an image row, or `null` when the row has none yet. */
function renditionKey(row: FindSimilarAssetRow): string | null {
  const r = row.rendition;
  if (r && typeof r === "object" && !Array.isArray(r)) {
    const key = (r as Record<string, unknown>).key;
    if (typeof key === "string" && key !== "") return key;
  }
  return null;
}

/** The default `ASSET#<id>` reader: one consistent `BatchGetItem` over the Projects table from env. */
async function defaultReadProjectAsset(pid: string, assetId: string): Promise<FindSimilarAssetRow | null> {
  if (!isProjectId(pid)) return null;
  const store = dynamoTransactStoreFromEnv();
  if (!store) return null;
  const rows = await store.batchGet([{ PK: projectPk(pid), SK: `ASSET#${assetId}` }]);
  return (rows[0] as FindSimilarAssetRow | undefined) ?? null;
}

/**
 * Resolve a `find_similar_ref` into the turn context's `find_similar`, against the turn's Active_Project
 * `pid` (design Component 7, "Starting the turn"):
 *   1. `ref.project_id` must equal `pid`, or the uniform 404.
 *   2. A consistent read of `ASSET#<asset_id>`; absent, `removed`, or a kind unlike `ref.kind` → the same 404.
 *   3. Image: `s3://<skills>/<rendition.key>`; no rendition yet → 409 `find_similar_unavailable`.
 *   4. Video: a valid `frame_s3_key` → `s3://<skills>/<key>` under `projects/`, else `s3://<proxy>/<key>` (MSQ-I2).
 *   5. `display_name` is the registry row's; `timecode` is kept only when it matches the Citation grammar.
 * A read failure fails closed as the uniform 404 (the card shows the message; no turn starts).
 */
export async function resolveFindSimilar(
  ref: FindSimilarRef,
  pid: string,
  deps: {
    readProjectAsset: (projectId: string, assetId: string) => Promise<FindSimilarAssetRow | null>;
    skillsBucket: string;
    mediaProxyBucket: string;
    warn: (msg: string) => void;
  },
): Promise<FindSimilarOutcome> {
  // Step 1: a Find_Similar search runs against the turn's Active_Project only.
  if (!pid || ref.project_id !== pid) return FIND_SIMILAR_NOT_FOUND;

  // Step 2: consistent read; absent / removed / kind mismatch is the uniform not-found.
  let row: FindSimilarAssetRow | null;
  try {
    row = await deps.readProjectAsset(pid, ref.asset_id);
  } catch (err) {
    deps.warn(`[projects] Find_Similar asset read failed: ${describe(err)}`);
    return FIND_SIMILAR_NOT_FOUND;
  }
  if (!row) return FIND_SIMILAR_NOT_FOUND;
  const kind = typeof row.kind === "string" ? row.kind : "";
  const status = typeof row.status === "string" ? row.status : "";
  if (status === "removed" || kind !== ref.kind) return FIND_SIMILAR_NOT_FOUND;

  const display_name = typeof row.displayName === "string" ? row.displayName : "";
  const timecode = FIND_SIMILAR_TIMECODE.test(ref.timecode) ? ref.timecode : "";

  if (kind === "image") {
    const key = renditionKey(row);
    if (!key) return { ok: false, status: FIND_SIMILAR_UNAVAILABLE_STATUS, body: FIND_SIMILAR_UNAVAILABLE_BODY };
    return { ok: true, findSimilar: { image_uri: `s3://${deps.skillsBucket}/${key}`, display_name, timecode } };
  }
  if (kind === "video") {
    const key = ref.frame_s3_key;
    if (!isValidFrameKey(key)) return FIND_SIMILAR_NOT_FOUND;
    const image_uri = key.startsWith("projects/")
      ? `s3://${deps.skillsBucket}/${key}`
      : `s3://${deps.mediaProxyBucket}/${key}`;
    return { ok: true, findSimilar: { image_uri, display_name, timecode } };
  }
  // Audio (or any other kind) has no searchable image to find similar against.
  return FIND_SIMILAR_NOT_FOUND;
}

export async function runTurnGate(input: TurnGateInput, deps: TurnGateDeps = {}): Promise<TurnGateOutcome> {
  const enabled = deps.projectsEnabled ?? defaultProjectsEnabled;
  if (!enabled()) return SKIP;
  const { body, actorSub, sessionId, turnId } = input;
  if (!isChatTurn(body) || !actorSub) return SKIP;

  const sessions = deps.sessions ?? null;
  const ensurePersonal = deps.ensurePersonalProject ?? defaultEnsurePersonal;
  const checkAccess = deps.checkAccess ?? defaultCheckAccess;
  const issue = deps.issueProjectToken ?? defaultIssue;
  const now = deps.now ?? Date.now;
  const warn = deps.warn ?? ((m: string) => console.warn(m));

  // A card's Find_Similar reference, resolved below once the Active_Project is known (Req 3.23). Absent or
  // malformed → the turn runs as a normal chat turn with no `find_similar`.
  const fsRef = parseFindSimilarRef(body.find_similar_ref);
  const resolveFs = (activeId: string) =>
    resolveFindSimilar(fsRef!, activeId, {
      readProjectAsset: deps.readProjectAsset ?? defaultReadProjectAsset,
      skillsBucket: deps.skillsBucket ?? process.env.SKILLS_S3_BUCKET ?? "",
      mediaProxyBucket: deps.mediaProxyBucket ?? process.env.MEDIA_PROXY_BUCKET ?? "",
      warn,
    });

  // Steps 1–2: the row and the Personal_Project, in parallel. Neither failure blocks chat.
  let personalError: unknown = null;
  const [row, personal] = await Promise.all([
    sessions
      ? sessions.read(sessionId, actorSub).catch((err) => {
          warn(`[projects] turn gate could not read the session row: ${describe(err)}`);
          return null;
        })
      : Promise.resolve(null),
    ensurePersonal(actorSub).catch((err) => {
      personalError = err;
      warn(`[projects] turn gate could not resolve the Personal_Project: ${describe(err)}`);
      return null;
    }),
  ]);

  const persist = async (fields: TurnGatePersistFields) => {
    if (!sessions || !row) return;
    try {
      await sessions.persist(sessionId, actorSub, fields);
    } catch (err) {
      warn(`[projects] turn gate could not update the session row: ${describe(err)}`);
    }
  };

  const requested = body.active_project_id;
  // D1: a session's project is fixed by its first turn. A turn naming a different project starts nothing; the
  // SPA offers a new chat in that project instead (R2.1). Checked before the issuer, so a refusal costs no token.
  // A chat whose fixed project is gone cannot continue at all, whatever project this turn names (session_project_gone).
  const goneOutcome = (): TurnGateOutcome | null => {
    if (!personal) return null;
    return { kind: "reject", status: SESSION_PROJECT_FIXED_STATUS, body: sessionProjectGoneBody(row!.activeProjectId!, personal) };
  };
  if (typeof requested === "string" && projectChangeRefused(row, requested)) {
    const bound = row!.activeProjectId!;
    if (personal && bound !== personal.projectId) {
      const still = await checkAccess(actorSub, bound).catch(() => null);
      if (still !== null && !still.ok) return goneOutcome()!;
    }
    return { kind: "reject", status: SESSION_PROJECT_FIXED_STATUS, body: sessionProjectFixedBody(bound) };
  }
  // A provided but non-string id is not a Project the Actor can use: it maps to not_found below.
  const pid: string =
    requested !== undefined && requested !== null
      ? (typeof requested === "string" ? requested : "")
      : (row?.activeProjectId ?? personal?.projectId ?? "");
  // Off unless Web Search is deployed here: a stale toggle or a hand-built body cannot arm it (the runtime gates
  // on its own flag too). The toggle is the chat's choice; this is the deployment's.
  // A chat with no recorded choice starts ON (user decision 2026-10-20): a row records `webSearch` on its first turn,
  // so only a brand-new chat (or a client that sends nothing) lands on the default.
  const webSearch: boolean = webSearchFeatureEnabled() &&
    (typeof body.web_search === "boolean" ? body.web_search : (row?.webSearch ?? true));

  if (!pid && (requested === undefined || requested === null)) {
    // No Project could be resolved (the registry is unavailable and nothing names one): run the turn
    // without a context, so the runtime binds no search. Chat must not depend on the registry.
    await persist({ webSearch });
    if (fsRef) {
      // A Find_Similar turn with no Active_Project cannot match the reference's Project: uniform 404.
      const fs = await resolveFs("");
      if (!fs.ok) return { kind: "reject", status: fs.status, body: fs.body };
    }
    return { kind: "proceed", turnContext: null, projectToken: null };
  }

  const isPersonal = !!personal && pid === personal.projectId;
  // Step 3: issuer + name lookup in parallel. The Personal_Project's META is already in hand.
  const [issued, access] = await Promise.all([
    issue(bearerToken(input.authorization), pid),
    isPersonal
      ? Promise.resolve<AccessResult>({ ok: true, project: personal!, role: "owner" })
      : checkAccess(actorSub, pid).catch((err): AccessResult | null => {
          warn(`[projects] turn gate access lookup failed: ${describe(err)}`);
          return null;
        }),
  ]);

  // A denial from either the issuer or the registry means access is gone. The Personal_Project is exempt:
  // moving a session "to Personal" because Personal was refused would loop, so treat that as unavailable.
  const lost = !isPersonal && (issued.status === "not_found" || (access !== null && !access.ok));
  if (lost) {
    if (!personal) {
      const status = personalError instanceof ProjectRegistryError ? personalError.status : 503;
      const error = personalError instanceof ProjectRegistryError
        ? personalError.error
        : { code: "registry_busy", message: "Project registry unavailable", retryable: true };
      return { kind: "reject", status, body: { error } };
    }
    // A chat with turns is fixed to its project: it ends rather than moving to Personal.
    if (row?.hasTurns === true && row.activeProjectId === pid) return goneOutcome()!;
    await persist({ activeProjectId: personal.projectId, activeProjectSetAt: now() });
    return {
      kind: "reject",
      status: PROJECT_ACCESS_LOST_STATUS,
      body: {
        error: {
          code: "project_access_lost",
          personalProject: { id: personal.projectId, name: personal.name },
        },
      },
    };
  }

  const ok = issued.status === "ok";
  if (!ok) {
    const reason = issued.status === "unavailable" ? issued.reason : issued.status;
    warn(`[projects] Project_Token unavailable for this turn (${reason}); continuing without search`);
  }
  const meta = access && access.ok ? access.project : null;
  // Persist the Active_Project only once something has verified it (the registry, or an issued token).
  const verified = !!meta || ok;
  await persist({
    ...(verified ? { activeProjectId: pid } : {}),
    ...(verified && pid !== row?.activeProjectId ? { activeProjectSetAt: now() } : {}),
    webSearch,
  });

  const turnContext = buildTurnContext({
    turnId,
    activeProject: {
      id: pid, name: meta?.name ?? "", personal: meta ? meta.personal === true : isPersonal,
      // Project settings, from the META the access check already read (no extra request). `graph` also needs
      // the env feature: with it off, an old META.graph=true row would only make the agent mint a token and
      // list graph tools that no longer exist.
      ...(meta ? { indexing: meta.indexing !== false, description: meta.description || undefined,
                   graph: meta.graph === true && graphFeatureEnabled() } : {}),
    },
    webSearch,
    projectAccess: ok ? "ok" : "unavailable",
  });
  if (fsRef) {
    // Resolve the Find_Similar reference against the Active_Project before stamping. A rejection starts no
    // turn (the SPA removes the optimistic bubble and shows the message under the button).
    const fs = await resolveFs(pid);
    if (!fs.ok) return { kind: "reject", status: fs.status, body: fs.body };
    turnContext.find_similar = fs.findSimilar;
  }
  return { kind: "proceed", turnContext, projectToken: ok ? (issued as { token: string }).token : null };
}
