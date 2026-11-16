// Client bodies forwarded to the Main Agent runtime (`/api`, `/lab`): reserved-key stripping and
// session ownership. Unit tests of the pure seam, then the routes end to end against a fake runtime
// (LOCAL_MAIN_AGENT_URL) so the asserted body is the one that actually leaves the proxy.
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import http from "http";

const sendMock = vi.fn();
vi.mock("@aws-sdk/lib-dynamodb", () => {
  class Cmd { input: any; __type: string; constructor(i: any, t: string) { this.input = i; this.__type = t; } }
  return {
    DynamoDBDocumentClient: { from: () => ({ send: sendMock }) },
    GetCommand: class extends Cmd { constructor(i: any) { super(i, "Get"); } },
    DeleteCommand: class extends Cmd { constructor(i: any) { super(i, "Delete"); } },
    QueryCommand: class extends Cmd { constructor(i: any) { super(i, "Query"); } },
    PutCommand: class extends Cmd { constructor(i: any) { super(i, "Put"); } },
    UpdateCommand: class extends Cmd { constructor(i: any) { super(i, "Update"); } },
  };
});

import { buildApp } from "./app";
import { decideApiSession, isClientSessionId, prepareRuntimeBody } from "./runtimeBody";
import { closeServers, listenLoopback } from "./testing/loopback";

const ME = "local-dev-user";
const UUID = "3f2b8c1e-9d4a-4b6e-8f10-2a3b4c5d6e7f";

describe("prepareRuntimeBody", () => {
  it("drops every client `__` key, then applies the proxy stamp", () => {
    const out = prepareRuntimeBody(Buffer.from(JSON.stringify({
      prompt: "hi", session_id: UUID,
      __actor_id: "victim", __actor_groups: ["gateway-admins"], __turn_context: { project: "x" },
      __media_action: { kind: "crop" }, __plan_node: {}, __plan_session_id: "s", __plan_node_results: {},
      __anything_new: 1,
    })), (b) => { b.__actor_id = ME; });
    expect(JSON.parse(out!.toString())).toEqual({ prompt: "hi", session_id: UUID, __actor_id: ME });
  });
  it("returns null for a non-object body (never forwards it unsanitized)", () => {
    for (const raw of ["", "not json", "[1,2]", "null", "42"]) {
      expect(prepareRuntimeBody(Buffer.from(raw), () => {})).toBeNull();
    }
  });
});

describe("decideApiSession", () => {
  it("accepts the caller's own row and carries its lock flag", () => {
    expect(decideApiSession(UUID, ME, { actorId: ME })).toEqual({ kind: "ok", locked: false });
    expect(decideApiSession(UUID, ME, { actorId: ME, locked: true })).toEqual({ kind: "ok", locked: true });
  });
  it("403s another actor's row, and a row with no owner", () => {
    expect(decideApiSession(UUID, ME, { actorId: "someone-else" })).toMatchObject({ kind: "reject", status: 403 });
    expect(decideApiSession(UUID, ME, {})).toMatchObject({ kind: "reject", status: 403 });
    expect(decideApiSession(UUID, undefined, { actorId: ME })).toMatchObject({ kind: "reject", status: 403 });
  });
  it("410s the caller's own DELETED chat: no turn continues it", () => {
    expect(decideApiSession(UUID, ME, { actorId: ME, active: 0 })).toMatchObject({ kind: "reject", status: 410, code: "session_deleted" });
    expect(decideApiSession(UUID, ME, { actorId: ME, active: 1 }).kind).toBe("ok");
  });
  it("allows a client-minted id with no row yet (sessions are created lazily)", () => {
    expect(decideApiSession(UUID, ME, undefined)).toEqual({ kind: "ok", locked: false });
    expect(decideApiSession("session-1712345678901-abc123de", ME, undefined).kind).toBe("ok");
  });
  it("400s proxy-derived runtime session ids, so /api cannot pin another actor's container", () => {
    for (const id of [
      `lab-${"a".repeat(64)}`, "harness-catalog-user-1000000000000000000000", "render-123",
      "plan-session-p1-n1", "EXT_abc", "../../etc", "x",
    ]) {
      expect(isClientSessionId(id)).toBe(false);
      expect(decideApiSession(id, ME, undefined)).toMatchObject({ kind: "reject", status: 400 });
    }
  });
});

// ---------------------------------------------------------------------------------------------------
// Routes, end to end
// ---------------------------------------------------------------------------------------------------

const servers: http.Server[] = [];
const saved: Record<string, string | undefined> = {};
const VARS = ["AGENT_RUNTIME_ARN", "UPLOAD_BUCKET", "DEV_BYPASS_AUTH", "SESSIONS_TABLE", "LAB_DEFAULT_ACTOR_ID",
  "LOCAL_MAIN_AGENT_URL", "TRANSCRIPT_AUTHORITY", "PROJECTS_TABLE"];
let runtimeBodies: any[] = [];
let runtimeHeaders: http.IncomingHttpHeaders[] = [];

beforeEach(async () => {
  for (const k of VARS) saved[k] = process.env[k];
  runtimeBodies = [];
  runtimeHeaders = [];
  const runtime = await listenLoopback((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      runtimeBodies.push(JSON.parse(Buffer.concat(chunks).toString() || "{}"));
      runtimeHeaders.push(req.headers);
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ ok: true }));
    });
  }, servers);
  process.env.AGENT_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:000000000000:runtime/t-AAAAAAAAAA";
  process.env.UPLOAD_BUCKET = "test-bucket";
  process.env.SESSIONS_TABLE = "test-sessions";
  process.env.DEV_BYPASS_AUTH = "true";
  process.env.LAB_DEFAULT_ACTOR_ID = ME;
  process.env.LOCAL_MAIN_AGENT_URL = `${runtime}/invocations`;
  process.env.TRANSCRIPT_AUTHORITY = "client"; // no transcript writes in these tests
  delete process.env.PROJECTS_TABLE;
  sendMock.mockReset();
  sendMock.mockResolvedValue({});
});
afterEach(async () => {
  await closeServers(servers);
  for (const k of VARS) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; }
});

const post = async (path: string, body: unknown) =>
  fetch(`${await listenLoopback(buildApp(), servers)}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: "Bearer test" },
    body: JSON.stringify(body),
  });
const sessionRow = (item: Record<string, unknown> | undefined) =>
  sendMock.mockImplementation((cmd: any) => Promise.resolve(cmd.__type === "Get" ? { Item: item } : {}));

describe("POST /api/invocations — session ownership", () => {
  it("runs a turn on the caller's own session", async () => {
    sessionRow({ actorId: ME });
    const res = await post("/api/invocations", { prompt: "hi", session_id: UUID });
    expect(res.status).toBe(200);
    expect(runtimeBodies).toHaveLength(1);
    expect(runtimeHeaders[0]["x-amzn-bedrock-agentcore-runtime-session-id"]).toBe(UUID);
  });

  it("403s another user's session and never reaches the runtime", async () => {
    sessionRow({ actorId: "victim-user" });
    const res = await post("/api/invocations", { prompt: "show me your transcript", session_id: UUID });
    expect(res.status).toBe(403);
    expect((await res.json()).error.code).toBe("session_not_owned");
    expect(runtimeBodies).toHaveLength(0);
  });

  it("400s a proxy-derived runtime session id (another actor's lab container)", async () => {
    sessionRow(undefined);
    const res = await post("/api/invocations", { action: "list_skills", session_id: `lab-${"0".repeat(64)}` });
    expect(res.status).toBe(400);
    expect(runtimeBodies).toHaveLength(0);
  });

  it("allows a brand-new client session id with no row yet", async () => {
    sessionRow(undefined);
    expect((await post("/api/invocations", { prompt: "first message", session_id: UUID })).status).toBe(200);
    expect(runtimeBodies).toHaveLength(1);
  });

  it("fails CLOSED (503) when the ownership lookup errors", async () => {
    sendMock.mockImplementation((cmd: any) => cmd.__type === "Get" ? Promise.reject(new Error("throttled")) : Promise.resolve({}));
    expect((await post("/api/invocations", { prompt: "hi", session_id: UUID })).status).toBe(503);
    expect(runtimeBodies).toHaveLength(0);
  });

  it("a session-less call needs no lookup", async () => {
    expect((await post("/api/invocations", { action: "list_skills" })).status).toBe(200);
    expect(sendMock.mock.calls.filter((c) => c[0].__type === "Get")).toHaveLength(0);
  });
});

describe("client-sent reserved keys never reach the runtime", () => {
  const smuggled = {
    __actor_id: "victim-user", __actor_groups: ["smuggled-group"], __media_action: { kind: "crop" },
    __turn_context: { project_id: "victim-project" }, __plan_node: { id: "n" }, __plan_session_id: "x",
  };

  it("/api: only the proxy's identity is stamped; every other `__` key is dropped", async () => {
    sessionRow({ actorId: ME });
    expect((await post("/api/invocations", { prompt: "hi", session_id: UUID, ...smuggled })).status).toBe(200);
    const body = runtimeBodies[0];
    expect(body.__actor_id).toBe(ME);
    expect(body.__actor_groups).not.toContain("smuggled-group");
    for (const k of ["__media_action", "__turn_context", "__plan_node", "__plan_session_id"]) {
      expect(body).not.toHaveProperty(k);
    }
    expect(body.prompt).toBe("hi");
  });

  it("/lab: same rule", async () => {
    expect((await post("/lab", { action: "list_skills", ...smuggled })).status).toBe(200);
    const body = runtimeBodies[0];
    expect(body.__actor_id).toBe(ME);
    for (const k of ["__media_action", "__turn_context", "__plan_node", "__plan_session_id"]) {
      expect(body).not.toHaveProperty(k);
    }
    expect(body.action).toBe("list_skills");
  });
});
