// IND403 dry run: a deleted chat came back on reload. Deleting soft-deletes the row (`active = 0` + a TTL), but
// the transcript and renders reads only checked the owner, and re-creating the id answered 200 "exists", so the
// SPA resumed it. A deleted chat is gone everywhere: its transcript and renders are not served, its id is not
// re-created, and no turn continues it.
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
import { closeServers, listenLoopback } from "./testing/loopback";

const ME = "local-dev-user";
const SID = "0b6c5d1e-2f3a-4b5c-8d7e-9f0a1b2c3d4e";
const servers: http.Server[] = [];
const saved: Record<string, string | undefined> = {};
const VARS = ["AGENT_RUNTIME_ARN", "UPLOAD_BUCKET", "DEV_BYPASS_AUTH", "SESSIONS_TABLE", "LAB_DEFAULT_ACTOR_ID"];

beforeEach(() => {
  for (const k of VARS) saved[k] = process.env[k];
  process.env.AGENT_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:000000000000:runtime/t-AAAAAAAAAA";
  process.env.SESSIONS_TABLE = "test-sessions";
  process.env.DEV_BYPASS_AUTH = "true";
  process.env.LAB_DEFAULT_ACTOR_ID = ME;
  delete process.env.UPLOAD_BUCKET; // the transcript falls back to the row's own tail
  sendMock.mockReset();
});
afterEach(async () => {
  await closeServers(servers);
  for (const k of VARS) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; }
});

const listen = () => listenLoopback(buildApp(), servers);

function rowIs(item: Record<string, unknown>) {
  sendMock.mockImplementation((cmd: any) => {
    if (cmd.__type === "Get") return Promise.resolve({ Item: item });
    if (cmd.__type === "Put") {
      const e: any = new Error("exists"); e.name = "ConditionalCheckFailedException";
      return Promise.reject(e);
    }
    return Promise.resolve({});
  });
}

const DELETED = { sessionId: SID, actorId: ME, active: 0, ttl: 1, transcript: [{ role: "user", content: "hi" }] };
const LIVE = { ...DELETED, active: 1 };

describe("a deleted chat is gone everywhere", () => {
  it("serves no transcript for a deleted chat, and the transcript of a live one", async () => {
    rowIs(DELETED);
    const base = await listen();
    expect((await (await fetch(`${base}/sessions/${SID}/transcript`)).json()).transcript).toEqual([]);
    rowIs(LIVE);
    expect((await (await fetch(`${base}/sessions/${SID}/transcript`)).json()).transcript).toHaveLength(1);
  });

  it("refuses to re-create a deleted chat's id (410), and still answers 200 for a live one", async () => {
    const base = await listen();
    const post = () => fetch(`${base}/sessions`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ sessionId: SID, title: "x" }),
    });
    rowIs(DELETED);
    const gone = await post();
    expect(gone.status).toBe(410);
    expect((await gone.json()).error).toBe("session_deleted");
    rowIs(LIVE);
    expect((await post()).status).toBe(200);
  });
});
