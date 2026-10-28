/**
 * `/projects` — Project_Registry routes (media-harness-projects-access task 6.9; design Component 1 route
 * table, Component 9 "Proxy routes", D7).
 *
 * | Route                                   | Registry call                   | Success                 |
 * |-----------------------------------------|---------------------------------|-------------------------|
 * | `GET    /projects`                      | `ensurePersonalProject`, `list` | 200 `{projects}`        |
 * | `POST   /projects {name}`               | `create`                        | 201 `{project}`         |
 * | `GET    /projects/:pid`                 | `detail`                        | 200 `{project, role, owner, members}` |
 * | `PATCH  /projects/:pid {name}`          | `rename`                        | 200 `{project}`         |
 * | `DELETE /projects/:pid {confirmName}`   | `delete`                        | 200 `{project}`         |
 * | `POST   /projects/:pid/members {invite}`| `resolveInvitee`, `invite`      | 201 `{member}`          |
 * | `DELETE /projects/:pid/members/:sub`    | `remove`                        | 200 `{removed: {sub}}`  |
 * | `POST   /projects/:pid/leave`           | `leave`                         | 200 `{left: {sub}}`     |
 * | `*      /projects/:pid/graph*`          | `graphRoutes.ts`                | see that module         |
 *
 * Every failure is `{"error":{"code", "message", ...}}` with the status from `registryErrorStatus`.
 *
 * Identity: the Actor is `req.actorId`, set by the Cognito `authMiddleware` (`/projects` is in `authPaths`).
 * Bodies are allowlisted — only `name`, `confirmName`, and `invite` are read — so an Actor id in the body
 * (`actorId`, `actor_id`, `ownerSub`, ...) is never consulted. A request with no `req.actorId` fails closed
 * with 401 before any registry read (the middleware should already have answered it).
 *
 * Disabled: when no projects store is configured (`PROJECTS_TABLE` unset, as in prod; `access.ts`), every
 * `/projects*` request answers `404 {"error":{"code":"projects_not_enabled"}}`, following the
 * `SESSIONS_TABLE` pattern. The check is per request, so the store is resolved lazily, never at mount.
 *
 * Invite resolution (task 6.7) is injected as `resolveInvitee(value)`. The default is
 * `defaultInviteResolver` from `./invite` (Cognito `ListUsers` + `AdminGetUser`), statically imported so
 * esbuild bundles it. It reads `USER_POOL_ID` lazily: with a pool configured it resolves an invite value
 * to exactly one enabled Harness user, and with no pool (local dev, or an environment without Projects) it
 * refuses every value with the "no single Harness user matches" error, exactly as the pre-6.7 stub did.
 * A caller may still override it (`createProjectsHandler({ resolveInvitee })`), which the route tests use
 * to drive a fake directory. `resolveInviteeUnavailable` is retained as that always-refusing resolver.
 *
 * Before resolving an invite value, the route confirms with `checkAccess` that the caller is the Owner of a
 * non-Personal Project. So a non-member gets the uniform `project_not_found` (Req 1.17) and a Member gets
 * `owner_only`, and neither can use the route to ask the user directory whether an email exists
 * (requirements open question 6 accepts that disclosure for the Owner only). `invite` re-checks all of this
 * in its own transaction, so the pre-check adds no race.
 */
import { readCappedBody } from "../agentTestRoute";
import {
  ProjectRegistryError,
  ProjectsDisabledError,
  checkAccess,
  ensurePersonalProject,
  projectRegistry,
  projectsEnabled,
} from "./access";
import { REGISTRY_MESSAGES, registryErrorStatus } from "./registry";
import type { Invitee, RegistryError, RegistryResult } from "./registry";
// Default invite resolver (task 6.7). Static import so esbuild bundles it; it reads `USER_POOL_ID`
// lazily and refuses every value when no pool is configured, so this is safe in every environment.
import { defaultInviteResolver } from "./invite";
// Real ingestion asset read/URL routes (`/projects/:pid/assets*`), replacing the dev stand-in.
import { dispatchAssetRoute } from "./ingestionRoutes";
// Ingestion write routes (uploads, registration, s3-locations, remove, chat-copy, capabilities).
import { dispatchIngestionWriteRoute } from "./uploadRoutes";
import { bearerToken } from "./turnGate";
import { ensurePersonalSamples } from "./personalSamples";
// Knowledge-graph routes (`/projects/:pid/graph*`), backed by the x86 graph-reader Lambda.
import { dispatchGraphRoute } from "./graphRoutes";
import { dispatchMomentsSearch } from "./momentsSearch";

/** The `parts[1]` segments the ingestion routes own (design §3). */
const INGESTION_SEGMENTS = new Set(["assets", "uploads", "s3-locations", "ingestion"]);

/** Max accepted request body. Bodies carry a name or an invite value; this is generous for both. */
export const PROJECTS_MAX_BODY_BYTES = 16 * 1024;

/** Longest accepted invite value (an email address is at most 320 characters). */
export const MAX_INVITE_VALUE_LENGTH = 320;

/** The invite value matched no enabled user, or more than one user (Req 1.10). One text for both cases. */
export interface NoSingleUserError {
  code: "no_single_user";
  message: string;
}

export const NO_SINGLE_USER: NoSingleUserError = Object.freeze({
  code: "no_single_user",
  message: "No single Harness user matches that email or username",
} as const);

export type InviteResolution = { ok: true; invitee: Invitee } | { ok: false; error: NoSingleUserError };

/** Resolve an invite value (email or username) to exactly one enabled Harness user (task 6.7). */
export type ResolveInvitee = (value: string) => Promise<InviteResolution>;

/** Always-refuses resolver (the pre-6.7 behaviour), kept for callers that want to disable invites. */
export const resolveInviteeUnavailable: ResolveInvitee = async () => ({ ok: false, error: NO_SINGLE_USER });

export interface ProjectsRouteDeps {
  /** Invite lookup; defaults to `defaultInviteResolver` (Cognito, env-driven; refuses with no pool). */
  resolveInvitee?: ResolveInvitee;
}

const PROJECTS_NOT_ENABLED = Object.freeze({
  code: "projects_not_enabled",
  message: "Projects are not enabled in this environment",
});

/** Every error body carries at least these; registry errors add their own fields (`limit`, `rule`, ...). */
type ErrorBody = { readonly code: string; readonly message: string };

function sendJson(res: any, status: number, body: unknown, extraHeaders: Record<string, string> = {}): void {
  if (res.headersSent) return;
  res.writeHead(status, { "content-type": "application/json", ...extraHeaders });
  res.end(JSON.stringify(body));
}

function sendError(res: any, status: number, error: ErrorBody, extraHeaders?: Record<string, string>): void {
  sendJson(res, status, { error }, extraHeaders);
}

function sendRegistryError(res: any, error: RegistryError): void {
  sendError(res, registryErrorStatus(error), error);
}

function sendResult<T>(res: any, r: RegistryResult<T>, status: number, wrap: (v: T) => unknown): void {
  if (r.ok) sendJson(res, status, wrap(r.value));
  else sendRegistryError(res, r.error);
}

function methodNotAllowed(res: any, allow: string[]): void {
  sendError(
    res,
    405,
    { code: "method_not_allowed", message: `Use ${allow.join(" or ")}` },
    { allow: allow.join(", ") },
  );
}

class BadRequest extends Error {
  readonly code: string;
  readonly status: number;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

/** The JSON object body, `{}` when empty. Throws `BadRequest` on oversize, invalid JSON, or a non-object. */
async function readJsonObject(req: any): Promise<Record<string, unknown>> {
  let raw: Buffer;
  try {
    raw = await readCappedBody(req, PROJECTS_MAX_BODY_BYTES);
  } catch (e: any) {
    if (e?.code === "payload_too_large") throw new BadRequest(413, "payload_too_large", "Request body too large");
    throw e;
  }
  const text = raw.toString("utf8").trim();
  if (!text) return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new BadRequest(400, "bad_request", "Body must be valid JSON");
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new BadRequest(400, "bad_request", "Body must be a JSON object");
  }
  return parsed as Record<string, unknown>;
}

/** A string field, or `""` when absent or not a string (the registry then refuses it after its access check). */
const stringField = (body: Record<string, unknown>, key: string): string =>
  typeof body[key] === "string" ? (body[key] as string) : "";

function decodeSegment(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s; // left encoded: matches no Project id or member sub, so it reads as not-found
  }
}

/**
 * Create the `/projects` handler. Mount with `app.use("/projects", createProjectsHandler())` AFTER the
 * `authPaths` middleware loop, so `req.actorId` is populated or the request was already answered 401.
 */
export function createProjectsHandler(deps: ProjectsRouteDeps = {}) {
  const resolveInvitee = deps.resolveInvitee ?? defaultInviteResolver;

  return async function projectsHandler(req: any, res: any): Promise<void> {
    const actor: string | undefined = req.actorId;
    if (typeof actor !== "string" || actor.length === 0) {
      sendError(res, 401, { code: "missing_actor", message: "No actor id resolved for request" }, {
        "www-authenticate": 'Bearer error="missing_actor"',
      });
      return;
    }
    if (!projectsEnabled()) {
      sendError(res, 404, PROJECTS_NOT_ENABLED);
      return;
    }

    const method = String(req.method || "GET").toUpperCase();
    const parts = new URL(String(req.url || "/"), "http://localhost").pathname.split("/").filter(Boolean);

    try {
      // /projects
      if (parts.length === 0) {
        if (method === "GET") {
          const personal = await ensurePersonalProject(actor);
          // The demo tour's sample (0-demo.mp4) in the caller's own Personal project: once per actor per
          // process, waited on briefly, never a failure of this list (personalSamples.ts). Not under dev bypass.
          if (process.env.DEV_BYPASS_AUTH !== "true") {
            const userToken =
              (typeof req.userToken === "string" && req.userToken) || bearerToken(req.headers?.authorization) || undefined;
            await ensurePersonalSamples(actor, personal.projectId, userToken);
          }
          const projects = await projectRegistry().list(actor);
          sendJson(res, 200, { projects });
          return;
        }
        if (method === "POST") {
          const body = await readJsonObject(req);
          // Settings ride along on create; the registry validates them (indexing defaults OFF).
          const r = await projectRegistry().create(actor, stringField(body, "name"), {
            description: body.description,
            tags: body.tags,
            indexing: body.indexing as boolean | undefined,
            graph: body.graph as boolean | undefined,
          });
          sendResult(res, r, 201, (project) => ({ project }));
          return;
        }
        methodNotAllowed(res, ["GET", "POST"]);
        return;
      }

      const pid = decodeSegment(parts[0]);

      // /projects/:pid/graph* — the knowledge graph (overview, stats, neighbourhood, enable).
      if (parts.length >= 2 && parts[1] === "graph" && (await dispatchGraphRoute(req, res, { actor, pid, parts, method }))) {
        return;
      }

      // /projects/:pid/moments/search — the Moments tab's own search (no agent turn, no LLM).
      if (parts.length >= 2 && parts[1] === "moments" && (await dispatchMomentsSearch(req, res, { actor, pid, parts, method }))) {
        return;
      }

      // /projects/:pid/{assets,uploads,s3-locations,ingestion}* — the ingestion routes. The read routes
      // (Asset_URL_Route, status, list) come first; then the write routes. Each returns false for a
      // path it does not own, so an unknown one falls through to the `404 "No such /projects route"`.
      if (parts.length >= 2 && INGESTION_SEGMENTS.has(parts[1])) {
        if (parts[1] === "assets" && (await dispatchAssetRoute(req, res, { actor, pid, parts, method }))) return;
        // Under DEV_BYPASS_AUTH the proxy's actor is the identity; it reaches a LOCAL Asset_Service only.
        const devBypass = process.env.DEV_BYPASS_AUTH === "true";
        const userToken = devBypass
          ? undefined
          : (typeof req.userToken === "string" && req.userToken) || bearerToken(req.headers?.authorization) || undefined;
        if (await dispatchIngestionWriteRoute(req, res, { actor, pid, parts, method, userToken, devBypass })) return;
      }

      // /projects/:pid
      if (parts.length === 1) {
        if (method === "GET") {
          sendResult(res, await projectRegistry().detail(actor, pid), 200, (detail) => detail);
          return;
        }
        if (method === "PATCH") {
          const body = await readJsonObject(req);
          // `name` renames; `description`/`tags` change settings; a body may carry both (rename first, so a
          // refused name changes nothing). Indexing is turned on via POST /projects/:pid/ingestion/indexing.
          const hasName = "name" in body;
          const hasSettings = "description" in body || "tags" in body;
          if (!hasName && !hasSettings) {
            sendError(res, 400, { code: "invalid_body", message: "Nothing to change: send name, description, or tags" });
            return;
          }
          let r = hasName ? await projectRegistry().rename(actor, pid, stringField(body, "name")) : null;
          if (hasSettings && (!r || r.ok)) {
            r = await projectRegistry().updateSettings(actor, pid, { description: body.description, tags: body.tags });
          }
          sendResult(res, r!, 200, (project) => ({ project }));
          return;
        }
        if (method === "DELETE") {
          const body = await readJsonObject(req);
          // Exact match, no trimming or case folding (Req 1.22): the registry compares to META `name`.
          const r = await projectRegistry().delete(actor, pid, stringField(body, "confirmName"));
          sendResult(res, r, 200, (project) => ({ project }));
          return;
        }
        methodNotAllowed(res, ["GET", "PATCH", "DELETE"]);
        return;
      }

      // /projects/:pid/members
      if (parts.length === 2 && parts[1] === "members") {
        if (method !== "POST") {
          methodNotAllowed(res, ["POST"]);
          return;
        }
        const body = await readJsonObject(req);
        await invite(res, actor, pid, body.invite, resolveInvitee);
        return;
      }

      // /projects/:pid/members/:sub
      if (parts.length === 3 && parts[1] === "members") {
        if (method !== "DELETE") {
          methodNotAllowed(res, ["DELETE"]);
          return;
        }
        const r = await projectRegistry().remove(actor, pid, decodeSegment(parts[2]));
        sendResult(res, r, 200, (removed) => ({ removed }));
        return;
      }

      // /projects/:pid/leave
      if (parts.length === 2 && parts[1] === "leave") {
        if (method !== "POST") {
          methodNotAllowed(res, ["POST"]);
          return;
        }
        sendResult(res, await projectRegistry().leave(actor, pid), 200, (left) => ({ left }));
        return;
      }

      sendError(res, 404, { code: "not_found", message: "No such /projects route" });
    } catch (e) {
      if (e instanceof BadRequest) {
        sendError(res, e.status, { code: e.code, message: e.message });
        return;
      }
      if (e instanceof ProjectRegistryError) {
        sendError(res, e.status, e.error);
        return;
      }
      if (e instanceof ProjectsDisabledError) {
        sendError(res, 404, PROJECTS_NOT_ENABLED);
        return;
      }
      console.error("[projects] route error:", (e as Error)?.message ?? e);
      sendError(res, 500, { code: "internal_error", message: "Internal error" });
    }
  };
}

/** `POST /projects/:pid/members {invite}`: Owner pre-check, then resolve, then the registry transaction. */
async function invite(
  res: any,
  actor: string,
  pid: string,
  rawValue: unknown,
  resolveInvitee: ResolveInvitee,
): Promise<void> {
  // Same refusal order as `ProjectRegistry.invite`: not found, then the Personal rule, then Owner only.
  const access = await checkAccess(actor, pid);
  if (!access.ok) {
    sendRegistryError(res, access.error);
    return;
  }
  if (access.project.personal) {
    sendRegistryError(res, { code: "personal_project", message: REGISTRY_MESSAGES.personal });
    return;
  }
  if (access.role !== "owner") {
    sendRegistryError(res, {
      code: "owner_only",
      action: "invite",
      message: "Only the Project owner can invite Members to this Project",
    });
    return;
  }
  const value = typeof rawValue === "string" ? rawValue.trim() : "";
  if (!value || value.length > MAX_INVITE_VALUE_LENGTH) {
    sendError(res, 400, {
      code: "invalid_invite",
      message: `invite must be an email address or username of 1 to ${MAX_INVITE_VALUE_LENGTH} characters`,
    });
    return;
  }
  const resolved = await resolveInvitee(value);
  if (!resolved.ok) {
    sendError(res, 400, resolved.error);
    return;
  }
  const r = await projectRegistry().invite(actor, pid, resolved.invitee);
  sendResult(res, r, 201, (member) => ({ member }));
}
