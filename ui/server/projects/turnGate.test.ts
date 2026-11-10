// Turn-gate tests (media-harness-projects-access task 10.3; Req 3.8, 3.11, 4.15, 4.17, 9.5, 9.9).
//
// Two layers:
//   * `runTurnGate` directly, with the sessions table on a small conditional-write fake (below) and the
//     Project_Registry on a real `ProjectRegistry` over `InMemoryTransactStore`.
//   * The real `buildApp()` `/api` handler, with the Main_Agent runtime and the Token_Issuer as local HTTP
//     stubs (`LOCAL_MAIN_AGENT_URL`, `LOCAL_TOKEN_ISSUER_URL`), so the test sees the exact bytes and headers
//     the proxy sends. The S3 transcript writer is captured through a fake `S3Client`.
//
// The "Projects disabled" block is the regression guard (D7): the outbound runtime request must be the
// pre-Projects request byte for byte.
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import http from "http";

// These tests pass `web_search` through as the deployed case: AgentCore Web Search is on where CDK sets
// WEB_SEARCH_ENABLED. The not-deployed case (forced off) has its own test at the end.
process.env.WEB_SEARCH_ENABLED = "true";

// ---- sessions table fake (conditional writes honoured; unsupported expressions throw) -------------

type Item = Record<string, any>;
const ddbTable = new Map<string, Item>();
const tick = () => new Promise<void>((r) => setImmediate(r));

function ccf(): Error {
  const e = new Error("The conditional request failed");
  e.name = "ConditionalCheckFailedException";
  return e;
}
const attr = (n: string, names?: Record<string, string>) => (n.startsWith("#") ? names?.[n] ?? n : n);
function condHolds(item: Item | undefined, cond: string | undefined, names?: Record<string, string>, values?: Item): boolean {
  if (!cond) return true;
  return cond.split(/\s+AND\s+/).every((raw) => {
    const clause = raw.trim();
    let m = /^attribute_not_exists\(([#\w]+)\)$/.exec(clause);
    if (m) return !item || item[attr(m[1], names)] === undefined;
    m = /^([#\w]+)\s*=\s*(:\w+)$/.exec(clause);
    if (m) return !!item && item[attr(m[1], names)] === values?.[m[2]];
    throw new Error(`fake ddb: unsupported condition clause ${clause}`);
  });
}

async function fakeDdb(cmd: any): Promise<any> {
  await tick(); // let concurrent callers interleave between read and conditional write
  const { input } = cmd;
  const key = input.Key?.sessionId ?? input.Item?.sessionId;
  const cur = ddbTable.get(key);
  switch (cmd.__type) {
    case "Get":
      return { Item: cur ? structuredClone(cur) : undefined };
    case "Put":
      if (!condHolds(cur, input.ConditionExpression, input.ExpressionAttributeNames, input.ExpressionAttributeValues)) throw ccf();
      ddbTable.set(key, structuredClone(input.Item));
      return {};
    case "Update": {
      if (!condHolds(cur, input.ConditionExpression, input.ExpressionAttributeNames, input.ExpressionAttributeValues)) throw ccf();
      const m = /^SET (.+)$/.exec(input.UpdateExpression);
      if (!m) throw new Error(`fake ddb: unsupported update ${input.UpdateExpression}`);
      const next = { ...(cur ?? { sessionId: key }) };
      for (const pair of m[1].split(/,\s*/)) {
        const [lhs, rhs] = pair.split(/\s*=\s*/);
        next[attr(lhs, input.ExpressionAttributeNames)] = structuredClone(input.ExpressionAttributeValues[rhs]);
      }
      ddbTable.set(key, next);
      return {};
    }
    default:
      return {};
  }
}

const sendMock = vi.fn(fakeDdb);
vi.mock("@aws-sdk/lib-dynamodb", () => {
  class Cmd { input: any; __type: string; constructor(i: any, t: string) { this.input = i; this.__type = t; } }
  return {
    DynamoDBDocumentClient: { from: () => ({ send: sendMock }) },
    GetCommand: class extends Cmd { constructor(i: any) { super(i, "Get"); } },
    DeleteCommand: class extends Cmd { constructor(i: any) { super(i, "Delete"); } },
    QueryCommand: class extends Cmd { constructor(i: any) { super(i, "Query"); } },
    PutCommand: class extends Cmd { constructor(i: any) { super(i, "Put"); } },
    UpdateCommand: class extends Cmd { constructor(i: any) { super(i, "Update"); } },
    BatchGetCommand: class extends Cmd { constructor(i: any) { super(i, "BatchGet"); } },
    TransactWriteCommand: class extends Cmd { constructor(i: any) { super(i, "TransactWrite"); } },
  };
});

// ---- S3: capture transcript writes -------------------------------------------------------------------
const s3Puts: { Key: string; Body: string }[] = [];
vi.mock("@aws-sdk/client-s3", async (importOriginal) => {
  const orig = await importOriginal<typeof import("@aws-sdk/client-s3")>();
  class S3Client {
    async send(cmd: any) {
      if (cmd instanceof orig.PutObjectCommand) {
        s3Puts.push({ Key: cmd.input.Key as string, Body: String(cmd.input.Body) });
        return {};
      }
      const e = new Error("NoSuchKey");
      e.name = "NoSuchKey";
      throw e;
    }
  }
  return { ...orig, S3Client };
});

import { buildApp } from "../app";
import { closeServers, listenLoopback } from "../testing/loopback";
import { ensurePersonalProject, projectRegistry, resetProjectsStore, setProjectsStore } from "./access";
import { InMemoryTransactStore } from "./inMemoryStore";
import { createTokenIssuerClient, PROJECT_TOKEN_HEADER } from "./tokenIssuerClient";
import type { IssueResult } from "./tokenIssuerClient";
import {
  bearerToken,
  dynamoSessionStore,
  isChatTurn,
  isValidFrameKey,
  parseFindSimilarRef,
  resolveFindSimilar,
  runTurnGate,
  FIND_SIMILAR_UNAVAILABLE_STATUS,
  FIND_SIMILAR_UNAVAILABLE_BODY,
} from "./turnGate";
import type { FindSimilarAssetRow } from "./turnGate";
import { PROJECT_NOT_FOUND_BODY, PROJECT_NOT_FOUND_STATUS } from "./access";
import type { DynamoDBDocumentClient } from "@aws-sdk/lib-dynamodb";

const ACTOR = "local-dev-user";
const OTHER = "sub-other";
const TABLE = "test-sessions";
const TOKEN = "eyJhbGciOiJSUzI1NiJ9.PROJECT-TOKEN-SECRET.c2ln";
const USER_BEARER = "dev-login-hs256-token";

const sessions = () => dynamoSessionStore({ send: sendMock } as unknown as DynamoDBDocumentClient, TABLE);
const okIssuer = () => vi.fn(async (): Promise<IssueResult> => ({ status: "ok", token: TOKEN, exp: 1_900_000_000 }));

let store: InMemoryTransactStore;
const quiet = { warn: () => {} };

async function ownedProject(owner: string, name: string): Promise<string> {
  const r = await projectRegistry().create(owner, name);
  if (!r.ok) throw new Error(`create failed: ${JSON.stringify(r.error)}`);
  return r.value.projectId;
}
function seedSession(id: string, extra: Item = {}) {
  ddbTable.set(id, { sessionId: id, actorId: ACTOR, title: "t", active: 1, ...extra });
}
const chat = (extra: Item = {}) => ({ prompt: "hi", session_id: "sess-1", ...extra });

beforeEach(() => {
  ddbTable.clear();
  s3Puts.length = 0;
  sendMock.mockClear();
  store = new InMemoryTransactStore();
  setProjectsStore(store, { sleep: async () => {} });
});
afterEach(() => {
  resetProjectsStore();
});

// =======================================================================================================
describe("runTurnGate", () => {
  it("recognises chat turns only", () => {
    expect(isChatTurn(chat())).toBe(true);
    expect(isChatTurn({ ...chat(), action: "list_skills" })).toBe(false);
    expect(isChatTurn({ prompt: "hi" })).toBe(false);
    expect(isChatTurn({ session_id: "s" })).toBe(false);
    expect(isChatTurn([chat()])).toBe(false);
    expect(bearerToken(`Bearer ${USER_BEARER}`)).toBe(USER_BEARER);
    expect(bearerToken(undefined)).toBe("");
  });

  it("is skipped when Projects are disabled, and for session-less actions", async () => {
    const issue = okIssuer();
    const base = { actorSub: ACTOR, authorization: `Bearer ${USER_BEARER}`, sessionId: "sess-1", turnId: "t-1" };
    expect(await runTurnGate({ ...base, body: chat() }, { projectsEnabled: () => false, issueProjectToken: issue })).toEqual({ kind: "skip" });
    expect(await runTurnGate({ ...base, body: { action: "list_skills" } }, { issueProjectToken: issue })).toEqual({ kind: "skip" });
    expect(issue).not.toHaveBeenCalled();
    expect(sendMock).not.toHaveBeenCalled();
  });

  it("on not_found starts no turn, answers 409 with the Personal_Project, and moves the session there", async () => {
    const pid = await ownedProject(ACTOR, "Alpha");
    seedSession("sess-1", { activeProjectId: pid });
    const issue = vi.fn(async (): Promise<IssueResult> => ({ status: "not_found" }));
    const out = await runTurnGate(
      { body: chat({ active_project_id: pid, web_search: true }), actorSub: ACTOR, authorization: `Bearer ${USER_BEARER}`, sessionId: "sess-1", turnId: "t-1" },
      { sessions: sessions(), issueProjectToken: issue, ...quiet },
    );
    const personal = await ensurePersonalProject(ACTOR);
    expect(out).toEqual({
      kind: "reject",
      status: 409,
      body: { error: { code: "project_access_lost", personalProject: { id: personal.projectId, name: "Personal" } } },
    });
    expect(issue).toHaveBeenCalledWith(USER_BEARER, pid);
    const row = ddbTable.get("sess-1")!;
    expect(row.activeProjectId).toBe(personal.projectId);
    expect(typeof row.activeProjectSetAt).toBe("number");
    expect(row.webSearch).toBeUndefined(); // the turn was not accepted
  });

  it("answers 409 for a Project the Actor is not a member of, even if the issuer is down", async () => {
    const foreign = await ownedProject(OTHER, "Theirs");
    seedSession("sess-1");
    const out = await runTurnGate(
      { body: chat({ active_project_id: foreign }), actorSub: ACTOR, authorization: "Bearer x", sessionId: "sess-1", turnId: "t-1" },
      { sessions: sessions(), issueProjectToken: async () => ({ status: "unavailable", reason: "timeout" }), ...quiet },
    );
    expect(out).toMatchObject({ kind: "reject", status: 409 });
  });

  it("continues as unavailable, with no token, when the issuer exceeds its budget", async () => {
    const pid = await ownedProject(ACTOR, "Alpha");
    seedSession("sess-1");
    const slow = createTokenIssuerClient({
      env: { LOCAL_TOKEN_ISSUER_URL: "http://127.0.0.1:1" },
      budgetMs: 100,
      transport: () => new Promise(() => {}), // never answers
    });
    const t0 = Date.now();
    const out = await runTurnGate(
      { body: chat({ active_project_id: pid }), actorSub: ACTOR, authorization: "Bearer x", sessionId: "sess-1", turnId: "t-1" },
      { sessions: sessions(), issueProjectToken: slow.issueProjectToken, ...quiet },
    );
    expect(Date.now() - t0).toBeLessThan(1_500);
    expect(out).toEqual({
      kind: "proceed",
      projectToken: null,
      turnContext: {
        turn_id: "t-1",
        // A new Project's settings ride along: indexing OFF (the default), no description.
        active_project: { id: pid, name: "Alpha", collection_id: pid, personal: false, indexing: false },
        web_search: true, // no recorded choice: the default (on),
        project_access: "unavailable",
      },
    });
  });

  it("stamps graph only while the env graph feature is on, whatever an old META.graph says", async () => {
    setProjectsStore(store, { sleep: async () => {}, graphFeature: true }); // created while the feature was on
    const pid = await ownedProject(ACTOR, "Graphed");
    seedSession("sess-1");
    const gate = () => runTurnGate(
      { body: chat({ active_project_id: pid }), actorSub: ACTOR, authorization: `Bearer ${USER_BEARER}`, sessionId: "sess-1", turnId: "t-1" },
      { sessions: sessions(), issueProjectToken: okIssuer(), ...quiet },
    );
    try {
      vi.stubEnv("GRAPH_ENABLED", "");
      const off = await gate();
      expect(off).toMatchObject({ kind: "proceed", turnContext: { active_project: { id: pid } } });
      expect((off as { turnContext: { active_project: object } }).turnContext.active_project).not.toHaveProperty("graph");
      vi.stubEnv("GRAPH_ENABLED", "true");
      expect(await gate()).toMatchObject({ kind: "proceed", turnContext: { active_project: { id: pid, graph: true } } });
    } finally {
      vi.unstubAllEnvs();
    }
  });

  it("treats unauthenticated like unavailable", async () => {
    seedSession("sess-1");
    const out = await runTurnGate(
      { body: chat(), actorSub: ACTOR, authorization: "Bearer x", sessionId: "sess-1", turnId: "t-1" },
      { sessions: sessions(), issueProjectToken: async () => ({ status: "unauthenticated" }), ...quiet },
    );
    expect(out).toMatchObject({ kind: "proceed", projectToken: null, turnContext: { project_access: "unavailable" } });
  });

  it("falls back to the row, then the Personal_Project, and persists {activeProjectId, webSearch}", async () => {
    const pid = await ownedProject(ACTOR, "Alpha");
    seedSession("sess-1", { activeProjectId: pid, webSearch: true });
    const issue = okIssuer();
    const out = await runTurnGate(
      { body: chat(), actorSub: ACTOR, authorization: "Bearer x", sessionId: "sess-1", turnId: "t-1" },
      { sessions: sessions(), issueProjectToken: issue },
    );
    expect(issue).toHaveBeenCalledWith("x", pid);
    expect(out).toMatchObject({ kind: "proceed", projectToken: TOKEN, turnContext: { active_project: { id: pid }, web_search: true, project_access: "ok" } });

    seedSession("sess-2");
    const personal = await ensurePersonalProject(ACTOR);
    const out2 = await runTurnGate(
      { body: chat({ session_id: "sess-2", web_search: true }), actorSub: ACTOR, authorization: "Bearer x", sessionId: "sess-2", turnId: "t-2" },
      { sessions: sessions(), issueProjectToken: okIssuer() },
    );
    expect(out2).toMatchObject({ turnContext: { active_project: { id: personal.projectId, name: "Personal", personal: true } } });
    expect(ddbTable.get("sess-2")).toMatchObject({ activeProjectId: personal.projectId, webSearch: true });
  });

  it("a chat with no recorded choice starts with Web Search ON; a recorded off stays off", async () => {
    const pid = await ownedProject(ACTOR, "Alpha");
    seedSession("sess-new", { activeProjectId: pid });
    const on = await runTurnGate(
      { body: chat({ session_id: "sess-new" }), actorSub: ACTOR, authorization: "Bearer x", sessionId: "sess-new", turnId: "t-1" },
      { sessions: sessions(), issueProjectToken: okIssuer() },
    );
    expect(on).toMatchObject({ kind: "proceed", turnContext: { web_search: true } });
    seedSession("sess-off", { activeProjectId: pid, webSearch: false });
    const off = await runTurnGate(
      { body: chat({ session_id: "sess-off" }), actorSub: ACTOR, authorization: "Bearer x", sessionId: "sess-off", turnId: "t-2" },
      { sessions: sessions(), issueProjectToken: okIssuer() },
    );
    expect(off).toMatchObject({ kind: "proceed", turnContext: { web_search: false } });
  });

  it("never reads or writes another actor's session row", async () => {
    ddbTable.set("sess-x", { sessionId: "sess-x", actorId: OTHER, activeProjectId: "pffffffffffffffffffffffffffffffff" });
    const before = structuredClone(ddbTable.get("sess-x"));
    const personal = await ensurePersonalProject(ACTOR);
    const out = await runTurnGate(
      { body: chat({ session_id: "sess-x" }), actorSub: ACTOR, authorization: "Bearer x", sessionId: "sess-x", turnId: "t-1" },
      { sessions: sessions(), issueProjectToken: okIssuer(), ...quiet },
    );
    expect(out).toMatchObject({ kind: "proceed", turnContext: { active_project: { id: personal.projectId } } });
    expect(ddbTable.get("sess-x")).toEqual(before);
  });
});

// =======================================================================================================
describe("session ↔ project binding (D1, R2)", () => {
  const TURN = [{ id: "t-0-u", role: "user", content: "earlier", timestamp: 1, turnId: "t-0" }];
  const gate = (pid: string | undefined, issue = okIssuer()) => runTurnGate(
    { body: chat(pid === undefined ? {} : { active_project_id: pid }), actorSub: ACTOR, authorization: "Bearer x", sessionId: "sess-1", turnId: "t-1" },
    { sessions: sessions(), issueProjectToken: issue, ...quiet },
  );

  it("reads only the first transcript message to tell whether the session has a turn", async () => {
    seedSession("sess-1", { activeProjectId: "pa", transcript: TURN });
    seedSession("sess-2", { activeProjectId: "pa", transcript: [] });
    seedSession("sess-3", { activeProjectId: "pa" });
    expect(await sessions().read("sess-1", ACTOR)).toEqual({ activeProjectId: "pa", hasTurns: true });
    expect(await sessions().read("sess-2", ACTOR)).toEqual({ activeProjectId: "pa" });
    expect(await sessions().read("sess-3", ACTOR)).toEqual({ activeProjectId: "pa" });
    const get = sendMock.mock.calls.map((c) => c[0] as any).find((c) => c.__type === "Get");
    expect(get.input.ProjectionExpression).toBe("actorId, activeProjectId, webSearch, #tr[0].#role");
    expect(get.input.ExpressionAttributeNames).toEqual({ "#tr": "transcript", "#role": "role" });
  });

  it("refuses a different project once the session has a turn: 409 session_project_fixed, no token, no write", async () => {
    const bound = await ownedProject(ACTOR, "Alpha");
    const other = await ownedProject(ACTOR, "Beta");
    seedSession("sess-1", { activeProjectId: bound, transcript: TURN });
    const before = structuredClone(ddbTable.get("sess-1"));
    const issue = okIssuer();
    expect(await gate(other, issue)).toEqual({
      kind: "reject",
      status: 409,
      body: { error: expect.objectContaining({ code: "session_project_fixed", projectId: bound }) },
    });
    expect(issue).not.toHaveBeenCalled();
    expect(ddbTable.get("sess-1")).toEqual(before);
  });

  it("ends a chat whose own project was deleted: 409 session_project_gone, whatever project the turn names, no write", async () => {
    const personal = await ensurePersonalProject(ACTOR);
    const bound = await ownedProject(ACTOR, "Alpha");
    seedSession("sess-1", { activeProjectId: bound, transcript: TURN });
    expect((await projectRegistry().delete(ACTOR, bound, "Alpha")).ok).toBe(true);
    const before = structuredClone(ddbTable.get("sess-1"));
    const gone = {
      kind: "reject",
      status: 409,
      body: { error: expect.objectContaining({
        code: "session_project_gone", projectId: bound, personalProject: { id: personal.projectId, name: "Personal" },
      }) },
    };
    // The chat's composer moved to Personal (the SPA's reconcile), or still names the deleted project, or names none.
    expect(await gate(personal.projectId)).toEqual(gone);
    expect(await gate(bound, vi.fn(async (): Promise<IssueResult> => ({ status: "not_found" })))).toEqual(gone);
    expect(await gate(undefined, vi.fn(async (): Promise<IssueResult> => ({ status: "not_found" })))).toEqual(gone);
    expect(ddbTable.get("sess-1")).toEqual(before); // never re-pointed to Personal
  });

  it("a chat with no turn yet whose project was deleted still moves to Personal", async () => {
    const personal = await ensurePersonalProject(ACTOR);
    const pid = await ownedProject(ACTOR, "Alpha");
    seedSession("sess-1", { activeProjectId: pid });
    await projectRegistry().delete(ACTOR, pid, "Alpha");
    expect(await gate(pid, vi.fn(async (): Promise<IssueResult> => ({ status: "not_found" })))).toMatchObject({
      status: 409, body: { error: { code: "project_access_lost" } },
    });
    expect(ddbTable.get("sess-1")!.activeProjectId).toBe(personal.projectId);
  });

  it("lets a turn in the bound project (named or implied) proceed", async () => {
    const bound = await ownedProject(ACTOR, "Alpha");
    seedSession("sess-1", { activeProjectId: bound, transcript: TURN });
    expect(await gate(bound)).toMatchObject({ kind: "proceed", turnContext: { active_project: { id: bound } } });
    expect(await gate(undefined)).toMatchObject({ kind: "proceed", turnContext: { active_project: { id: bound } } });
  });

  it("still allows a switch on a session with no turn yet, and on a pre-Projects row with turns", async () => {
    const personal = await ensurePersonalProject(ACTOR);
    const pid = await ownedProject(ACTOR, "Alpha");
    seedSession("sess-1", { activeProjectId: personal.projectId });
    expect(await gate(pid)).toMatchObject({ kind: "proceed", turnContext: { active_project: { id: pid } } });
    expect(ddbTable.get("sess-1")!.activeProjectId).toBe(pid);

    // A row written before Projects has turns but no project: its first project pick binds it.
    seedSession("sess-1", { transcript: TURN });
    expect(await gate(pid)).toMatchObject({ kind: "proceed" });
  });
});

// =======================================================================================================
// Through buildApp(): the runtime and issuer are local HTTP stubs.

interface Captured { headers: http.IncomingHttpHeaders; body: Buffer }
let runtimeCalls: Captured[] = [];
let issuerCalls: unknown[] = [];
let issuerReply: unknown = { status: "ok", token: TOKEN, exp: 1_900_000_000, jti: "j-1" };
let runtimeServer: http.Server;
let issuerServer: http.Server;
let runtimeUrl = "";
let issuerUrl = "";

function readAll(req: http.IncomingMessage): Promise<Buffer> {
  return new Promise((resolve) => {
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => chunks.push(c));
    req.on("end", () => resolve(Buffer.concat(chunks)));
  });
}

beforeAll(async () => {
  runtimeServer = http.createServer(async (req, res) => {
    runtimeCalls.push({ headers: req.headers, body: await readAll(req) });
    res.writeHead(200, { "content-type": "text/event-stream" });
    res.end(`data: ${JSON.stringify("Hello from the agent")}\n\n`);
  });
  issuerServer = http.createServer(async (req, res) => {
    issuerCalls.push(JSON.parse((await readAll(req)).toString("utf8")));
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify(issuerReply));
  });
  const [runtimeBase, issuerBase] = await Promise.all([listenLoopback(runtimeServer), listenLoopback(issuerServer)]);
  runtimeUrl = `${runtimeBase}/invocations`;
  issuerUrl = issuerBase;
});
afterAll(async () => {
  await closeServers([runtimeServer, issuerServer]);
});

describe("/api through buildApp()", () => {
  const VARS = [
    "AGENT_RUNTIME_ARN", "UPLOAD_BUCKET", "DEV_BYPASS_AUTH", "SESSIONS_TABLE", "LAB_DEFAULT_ACTOR_ID",
    "PROJECTS_TABLE", "PROJECTS_STORE", "LOCAL_MAIN_AGENT_URL", "LOCAL_TOKEN_ISSUER_URL",
    "TOKEN_ISSUER_FN", "TRANSCRIPT_AUTHORITY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE",
  ];
  const saved: Record<string, string | undefined> = {};
  const servers: http.Server[] = [];

  beforeEach(() => {
    for (const k of VARS) saved[k] = process.env[k];
    process.env.AGENT_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:000000000000:runtime/t-AAAAAAAAAA";
    process.env.UPLOAD_BUCKET = "test-bucket";
    process.env.SESSIONS_TABLE = TABLE;
    process.env.DEV_BYPASS_AUTH = "true";
    process.env.LAB_DEFAULT_ACTOR_ID = ACTOR;
    process.env.LOCAL_MAIN_AGENT_URL = runtimeUrl;
    process.env.LOCAL_TOKEN_ISSUER_URL = issuerUrl;
    // The local (http://) runtime path still runs the SigV4 signer before discarding its output.
    process.env.AWS_ACCESS_KEY_ID = "AKIDTESTONLY";
    process.env.AWS_SECRET_ACCESS_KEY = "test-secret";
    for (const k of ["PROJECTS_TABLE", "PROJECTS_STORE", "TOKEN_ISSUER_FN", "TRANSCRIPT_AUTHORITY", "AWS_SESSION_TOKEN", "AWS_PROFILE"]) delete process.env[k];
    runtimeCalls = [];
    issuerCalls = [];
    issuerReply = { status: "ok", token: TOKEN, exp: 1_900_000_000, jti: "j-1" };
  });
  afterEach(async () => {
    await closeServers(servers);
    for (const k of VARS) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; }
  });

  function listen(): Promise<string> {
    return listenLoopback(buildApp(), servers);
  }
  const invoke = (base: string, body: unknown) =>
    fetch(`${base}/api/invocations`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${USER_BEARER}` },
      body: JSON.stringify(body),
    });
  const tokenHeader = PROJECT_TOKEN_HEADER.toLowerCase();

  describe("Projects disabled (regression: unchanged outbound request)", () => {
    beforeEach(() => setProjectsStore(null));

    it("sends the pre-Projects bytes and headers, with no token header and no issuer call", async () => {
      seedSession("sess-1");
      const clientBody = {
        prompt: "hi",
        session_id: "sess-1",
        active_project_id: "p0123456789abcdef0123456789abcdef",
        web_search: true,
        __turn_context: { forged: true },
      };
      const res = await invoke(await listen(), clientBody);
      expect(res.status).toBe(200);
      await res.text();
      expect(runtimeCalls).toHaveLength(1);
      const [call] = runtimeCalls;
      // Pre-feature body: the client body plus the actor stamp — minus every client `__` key, which
      // the proxy never forwards (runtimeBody.ts): a forged `__turn_context` is dropped even here.
      const { __turn_context: _forged, ...clientFields } = clientBody;
      const expected = Buffer.from(
        JSON.stringify({ ...clientFields, __actor_id: ACTOR, __actor_groups: ["gateway-admins"] }),
        "utf8",
      );
      expect(call.body.equals(expected)).toBe(true);
      expect(Object.keys(call.headers).sort()).toEqual(
        ["connection", "content-type", "host", "transfer-encoding", "x-amzn-bedrock-agentcore-runtime-session-id"],
      );
      expect(call.headers[tokenHeader]).toBeUndefined();
      expect(issuerCalls).toHaveLength(0);
      // No gate traffic on the sessions table: no consistent row read, no Projects write.
      const cmds = sendMock.mock.calls.map((c) => c[0] as any);
      expect(cmds.filter((c) => c.input.ConsistentRead)).toHaveLength(0);
      expect(cmds.filter((c) => /activeProjectId|webSearch/.test(c.input.UpdateExpression ?? ""))).toHaveLength(0);
    });
  });

  describe("Projects enabled", () => {
    it("stamps the proxy-authored context and sends the token only in its header", async () => {
      const base = await listen();
      // POST /sessions sets the Personal_Project.
      expect((await fetch(`${base}/sessions`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ sessionId: "sess-new" }) })).status).toBe(201);
      const pid = await ownedProject(ACTOR, "Trailer cuts");
      const res = await invoke(base, {
        prompt: "find the chase",
        session_id: "sess-new",
        turn_id: "t-abc",
        active_project_id: pid,
        web_search: true,
        __turn_context: { turn_id: "evil", active_project: { id: "pffffffffffffffffffffffffffffffff" }, project_access: "ok" },
      });
      expect(res.status).toBe(200);
      expect(await res.text()).toContain("Hello from the agent");

      expect(issuerCalls).toEqual([{ userToken: USER_BEARER, projectId: pid }]);
      expect(runtimeCalls).toHaveLength(1);
      const [call] = runtimeCalls;
      expect(call.headers[tokenHeader]).toBe(TOKEN);
      const raw = call.body.toString("utf8");
      expect(raw).not.toContain(TOKEN);
      const sent = JSON.parse(raw);
      expect(sent.__turn_context).toEqual({
        turn_id: "t-abc",
        active_project: { id: pid, name: "Trailer cuts", collection_id: pid, personal: false, indexing: false },
        web_search: true,
        project_access: "ok",
      });
      expect(ddbTable.get("sess-new")).toMatchObject({ activeProjectId: pid, webSearch: true });

      // The transcript turn items never carry the token.
      await vi.waitFor(() => expect(s3Puts.length).toBeGreaterThan(0));
      const doc = JSON.parse(s3Puts[s3Puts.length - 1].Body);
      expect(doc.messages.map((m: any) => [m.role, m.turnId])).toEqual([
        ["user", "t-abc"],
        ["assistant", "t-abc"],
      ]);
      expect(JSON.stringify(doc)).not.toContain(TOKEN);
    });

    it("answers 409 project_access_lost without invoking the runtime", async () => {
      seedSession("sess-1");
      const foreign = await ownedProject(OTHER, "Theirs");
      issuerReply = { status: "not_found" };
      const res = await invoke(await listen(), { prompt: "hi", session_id: "sess-1", active_project_id: foreign });
      expect(res.status).toBe(409);
      const personal = await ensurePersonalProject(ACTOR);
      expect(await res.json()).toEqual({
        error: { code: "project_access_lost", personalProject: { id: personal.projectId, name: "Personal" } },
      });
      expect(runtimeCalls).toHaveLength(0);
      expect(ddbTable.get("sess-1")!.activeProjectId).toBe(personal.projectId);
    });

    it("answers 409 session_project_fixed for a turn in another project once the session has a turn", async () => {
      const bound = await ownedProject(ACTOR, "Alpha");
      const other = await ownedProject(ACTOR, "Beta");
      seedSession("sess-1", { activeProjectId: bound, transcript: [{ id: "t-0-u", role: "user", content: "x", timestamp: 1 }] });
      const res = await invoke(await listen(), { prompt: "hi", session_id: "sess-1", active_project_id: other });
      expect(res.status).toBe(409);
      expect((await res.json()).error).toMatchObject({ code: "session_project_fixed", projectId: bound });
      expect(runtimeCalls).toHaveLength(0);
      expect(issuerCalls).toHaveLength(0);
      expect(ddbTable.get("sess-1")!.activeProjectId).toBe(bound);
    });

    it("leaves session-less actions untouched", async () => {
      const body = { action: "list_skills" };
      const res = await invoke(await listen(), body);
      await res.text();
      expect(runtimeCalls).toHaveLength(1);
      const [call] = runtimeCalls;
      expect(call.body.equals(Buffer.from(JSON.stringify({ ...body, __actor_id: ACTOR, __actor_groups: ["gateway-admins"] })))).toBe(true);
      expect(call.headers[tokenHeader]).toBeUndefined();
      expect(issuerCalls).toHaveLength(0);
    });
  });
});

// =======================================================================================================
// Find_Similar reference resolution (Component 7, media-harness-search-qa-rendering; Req 3.23, 3.24).

describe("parseFindSimilarRef", () => {
  const base = { project_id: "p1", asset_id: "a1", kind: "video" };

  it("parses a well-formed reference, defaulting the advisory fields", () => {
    expect(parseFindSimilarRef(base)).toEqual({
      project_id: "p1",
      asset_id: "a1",
      kind: "video",
      frame_s3_key: null,
      display_name: "",
      timecode: "",
    });
    expect(parseFindSimilarRef({ ...base, frame_s3_key: "projects/f.jpg", display_name: "x", timecode: "01:14\u201301:36" })).toMatchObject({
      frame_s3_key: "projects/f.jpg",
      display_name: "x",
      timecode: "01:14\u201301:36",
    });
  });

  it("returns null for a missing required field or a non-object", () => {
    expect(parseFindSimilarRef({ asset_id: "a1", kind: "video" })).toBeNull();
    expect(parseFindSimilarRef({ project_id: "p1", kind: "video" })).toBeNull();
    expect(parseFindSimilarRef({ project_id: "p1", asset_id: "a1" })).toBeNull();
    expect(parseFindSimilarRef({ ...base, project_id: "" })).toBeNull();
    expect(parseFindSimilarRef(null)).toBeNull();
    expect(parseFindSimilarRef([base])).toBeNull();
  });
});

describe("isValidFrameKey", () => {
  it("accepts a clean key and rejects empty, traversal, or URL-special segments", () => {
    expect(isValidFrameKey("projects/p1/frames/f.jpg")).toBe(true);
    expect(isValidFrameKey("")).toBe(false);
    expect(isValidFrameKey("a//b")).toBe(false);
    expect(isValidFrameKey("a/../b")).toBe(false);
    expect(isValidFrameKey("a/b%2e")).toBe(false);
    expect(isValidFrameKey("a/b?x")).toBe(false);
    expect(isValidFrameKey("a/b#x")).toBe(false);
    expect(isValidFrameKey(42)).toBe(false);
  });
});

describe("resolveFindSimilar", () => {
  const deps = (row: FindSimilarAssetRow | null, over: Partial<Parameters<typeof resolveFindSimilar>[2]> = {}) => ({
    readProjectAsset: vi.fn(async () => row),
    skillsBucket: "skills",
    mediaProxyBucket: "proxy",
    warn: () => {},
    ...over,
  });
  const ref = (over: Record<string, unknown> = {}) => ({
    project_id: "p1",
    asset_id: "a1",
    frame_s3_key: null as string | null,
    kind: "video",
    display_name: "x",
    timecode: "01:14\u201301:36",
    ...over,
  });

  it("rejects with the uniform not-found when the reference Project is not the Active_Project", async () => {
    const out = await resolveFindSimilar(ref({ project_id: "other" }), "p1", deps(null));
    expect(out).toEqual({ ok: false, status: PROJECT_NOT_FOUND_STATUS, body: PROJECT_NOT_FOUND_BODY });
  });

  it("rejects with not-found for an absent, removed, or kind-mismatched asset", async () => {
    expect(await resolveFindSimilar(ref(), "p1", deps(null))).toMatchObject({ ok: false, status: PROJECT_NOT_FOUND_STATUS });
    expect(await resolveFindSimilar(ref(), "p1", deps({ kind: "video", status: "removed" }))).toMatchObject({ ok: false });
    expect(await resolveFindSimilar(ref({ kind: "image" }), "p1", deps({ kind: "video", status: "indexed" }))).toMatchObject({ ok: false });
  });

  it("resolves an image to its rendition key under the skills bucket, keeping a valid timecode", async () => {
    const out = await resolveFindSimilar(
      ref({ kind: "image" }),
      "p1",
      deps({ kind: "image", status: "indexed", displayName: "poster.jpg", rendition: { key: "projects/p1/rend.jpg" } }),
    );
    expect(out).toEqual({
      ok: true,
      findSimilar: { image_uri: "s3://skills/projects/p1/rend.jpg", display_name: "poster.jpg", timecode: "01:14\u201301:36" },
    });
  });

  it("returns find_similar_unavailable for an image with no searchable rendition yet", async () => {
    const out = await resolveFindSimilar(ref({ kind: "image" }), "p1", deps({ kind: "image", status: "indexed" }));
    expect(out).toEqual({ ok: false, status: FIND_SIMILAR_UNAVAILABLE_STATUS, body: FIND_SIMILAR_UNAVAILABLE_BODY });
  });

  it("resolves a video frame to the skills bucket under projects/ and the proxy bucket otherwise (MSQ-I2)", async () => {
    const row = { kind: "video", status: "indexed", displayName: "nba.mp4" };
    const inProjects = await resolveFindSimilar(ref({ frame_s3_key: "projects/p1/f.jpg" }), "p1", deps(row));
    expect(inProjects).toMatchObject({ ok: true, findSimilar: { image_uri: "s3://skills/projects/p1/f.jpg", display_name: "nba.mp4" } });
    const inProxy = await resolveFindSimilar(ref({ frame_s3_key: "media/f.jpg" }), "p1", deps(row));
    expect(inProxy).toMatchObject({ ok: true, findSimilar: { image_uri: "s3://proxy/media/f.jpg" } });
  });

  it("drops a timecode that does not match the Citation grammar", async () => {
    const out = await resolveFindSimilar(
      ref({ frame_s3_key: "projects/f.jpg", timecode: "not-a-timecode" }),
      "p1",
      deps({ kind: "video", status: "indexed", displayName: "nba.mp4" }),
    );
    expect(out).toMatchObject({ ok: true, findSimilar: { timecode: "" } });
  });

  it("rejects a video with an invalid frame key, and any non-image/non-video kind", async () => {
    const row = { kind: "video", status: "indexed", displayName: "nba.mp4" };
    expect(await resolveFindSimilar(ref({ frame_s3_key: "a/../b" }), "p1", deps(row))).toMatchObject({ ok: false });
    expect(await resolveFindSimilar(ref({ kind: "audio" }), "p1", deps({ kind: "audio", status: "indexed" }))).toMatchObject({ ok: false });
  });

  it("fails closed as not-found when the asset read throws", async () => {
    const out = await resolveFindSimilar(ref({ frame_s3_key: "projects/f.jpg" }), "p1", {
      readProjectAsset: vi.fn(async () => { throw new Error("ddb down"); }),
      skillsBucket: "skills",
      mediaProxyBucket: "proxy",
      warn: () => {},
    });
    expect(out).toMatchObject({ ok: false, status: PROJECT_NOT_FOUND_STATUS });
  });
});

describe("runTurnGate with a find_similar_ref", () => {
  it("stamps turnContext.find_similar when the reference resolves against the Active_Project", async () => {
    const pid = await ownedProject(ACTOR, "Alpha");
    seedSession("sess-1", { activeProjectId: pid });
    const readProjectAsset = vi.fn(async () => ({ kind: "video", status: "indexed", displayName: "nba.mp4" }));
    const out = await runTurnGate(
      {
        body: chat({
          active_project_id: pid,
          find_similar_ref: { project_id: pid, asset_id: "a1", frame_s3_key: "projects/p1/f.jpg", kind: "video", display_name: "x", timecode: "01:14\u201301:36" },
        }),
        actorSub: ACTOR,
        authorization: `Bearer ${USER_BEARER}`,
        sessionId: "sess-1",
        turnId: "t-1",
      },
      { sessions: sessions(), issueProjectToken: okIssuer(), readProjectAsset, skillsBucket: "skills", mediaProxyBucket: "proxy", ...quiet },
    );
    expect(out).toMatchObject({
      kind: "proceed",
      turnContext: { find_similar: { image_uri: "s3://skills/projects/p1/f.jpg", display_name: "nba.mp4", timecode: "01:14\u201301:36" } },
    });
    expect(readProjectAsset).toHaveBeenCalledWith(pid, "a1");
  });

  it("rejects the turn (no runtime) when the find_similar_ref Project does not match the Active_Project", async () => {
    const pid = await ownedProject(ACTOR, "Alpha");
    seedSession("sess-1", { activeProjectId: pid });
    const readProjectAsset = vi.fn(async () => ({ kind: "video", status: "indexed", displayName: "nba.mp4" }));
    const out = await runTurnGate(
      {
        body: chat({
          active_project_id: pid,
          find_similar_ref: { project_id: "pffffffffffffffffffffffffffffffff", asset_id: "a1", frame_s3_key: "projects/f.jpg", kind: "video", display_name: "x", timecode: "01:14\u201301:36" },
        }),
        actorSub: ACTOR,
        authorization: `Bearer ${USER_BEARER}`,
        sessionId: "sess-1",
        turnId: "t-1",
      },
      { sessions: sessions(), issueProjectToken: okIssuer(), readProjectAsset, skillsBucket: "skills", mediaProxyBucket: "proxy", ...quiet },
    );
    expect(out).toMatchObject({ kind: "reject", status: PROJECT_NOT_FOUND_STATUS });
    expect(readProjectAsset).not.toHaveBeenCalled();
  });
});

describe("web_search where Web Search is not deployed", () => {
  it("forces a turn's web_search off, whatever the body or the session row say", async () => {
    const prev = process.env.WEB_SEARCH_ENABLED;
    delete process.env.WEB_SEARCH_ENABLED;
    try {
      const pid = (await ensurePersonalProject(ACTOR)).projectId;
      seedSession("sess-ws", { activeProjectId: pid, webSearch: true });
      const out = await runTurnGate(
        { body: chat({ session_id: "sess-ws", web_search: true }), actorSub: ACTOR, authorization: "Bearer x", sessionId: "sess-ws", turnId: "t-1" },
        { sessions: sessions(), issueProjectToken: okIssuer() },
      );
      expect(out).toMatchObject({ kind: "proceed", turnContext: { web_search: false } });
    } finally {
      process.env.WEB_SEARCH_ENABLED = prev;
    }
  });
});
