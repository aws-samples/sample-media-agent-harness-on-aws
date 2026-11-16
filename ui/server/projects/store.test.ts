// DynamoTransactStore (task 6.1): expression compilation, error translation, and BatchGet chunking, against
// a fake `send`. The in-memory store's semantics are in inMemoryStore.test.ts.
import { describe, expect, it } from "vitest";
import { countersKey, memberKey, metaKey, projectPrefix } from "./schema";
import {
  DynamoTransactStore,
  StoreValidationError,
  TransactionCancelledError,
  compileTransactOp,
  cond,
  dynamoTransactStoreFromEnv,
} from "./store";
import type { DocumentSender, Key } from "./store";

const PID = "p" + "a".repeat(32);
const T = "projects-table";

function fakeSender(reply: (input: Record<string, unknown>, name: string) => unknown): DocumentSender & { inputs: Record<string, unknown>[] } {
  const inputs: Record<string, unknown>[] = [];
  return {
    inputs,
    async send(command) {
      const input = command.input as unknown as Record<string, unknown>;
      inputs.push(input);
      return reply(input, command.constructor.name);
    },
  };
}

describe("compileTransactOp", () => {
  it("compiles an update with SET, ADD, REMOVE, and a nested condition", () => {
    const out = compileTransactOp(
      {
        kind: "update",
        key: metaKey(PID),
        set: { status: "deleted" },
        add: { memberCount: -1 },
        remove: ["collectionState"],
        condition: cond.and(cond.eq("status", "active"), cond.or(cond.notExists("x"), cond.lt("memberCount", 25))),
      },
      T,
    );
    expect(out).toEqual({
      Update: {
        TableName: T,
        Key: metaKey(PID),
        ConditionExpression: "(#n0 = :v0 AND (attribute_not_exists(#n1) OR #n2 < :v1))",
        UpdateExpression: "SET #n0 = :v2 ADD #n2 :v3 REMOVE #n3",
        ExpressionAttributeNames: { "#n0": "status", "#n1": "x", "#n2": "memberCount", "#n3": "collectionState" },
        ExpressionAttributeValues: { ":v0": "active", ":v1": 25, ":v2": "deleted", ":v3": -1 },
      },
    });
  });

  it("omits empty expression maps, which DynamoDB rejects", () => {
    expect(compileTransactOp({ kind: "delete", key: memberKey(PID, "bob") }, T)).toEqual({
      Delete: { TableName: T, Key: memberKey(PID, "bob") },
    });
    expect(compileTransactOp({ kind: "put", item: { ...metaKey(PID), a: 1 }, condition: cond.notExists("PK") }, T)).toEqual({
      Put: { TableName: T, Item: { ...metaKey(PID), a: 1 }, ConditionExpression: "attribute_not_exists(#n0)", ExpressionAttributeNames: { "#n0": "PK" } },
    });
  });
});

describe("DynamoTransactStore", () => {
  it("translates TransactionCanceledException into per-op reasons", async () => {
    const client = fakeSender(() => {
      throw Object.assign(new Error("cancelled"), {
        name: "TransactionCanceledException",
        CancellationReasons: [{ Code: "None" }, { Code: "ConditionalCheckFailed", Message: "The conditional request failed" }],
      });
    });
    const store = new DynamoTransactStore({ tableName: T, client });
    const e = await store
      .transactWrite([
        { kind: "put", item: { ...metaKey(PID) } },
        { kind: "update", key: countersKey("alice"), add: { ownedCount: 1 } },
      ])
      .catch((err: unknown) => err);
    expect(e).toBeInstanceOf(TransactionCancelledError);
    expect((e as TransactionCancelledError).failedConditions).toEqual([1]);
  });

  it("translates ValidationException and validates locally before sending", async () => {
    const client = fakeSender(() => {
      throw Object.assign(new Error("bad"), { name: "ValidationException" });
    });
    const store = new DynamoTransactStore({ tableName: T, client });
    await expect(store.transactWrite([{ kind: "put", item: { ...metaKey(PID) } }])).rejects.toThrow(StoreValidationError);
    const put = { kind: "put" as const, item: { ...metaKey(PID) } };
    await expect(store.transactWrite([put, put])).rejects.toThrow(StoreValidationError);
    expect(client.inputs).toHaveLength(1);
  });

  it("batch-gets in chunks of 100 with ConsistentRead, re-requesting UnprocessedKeys", async () => {
    const keys: Key[] = Array.from({ length: 150 }, (_, i) => ({ PK: `PROJECT#${i}`, SK: "META" }));
    let first = true;
    const client = fakeSender((input) => {
      const req = (input.RequestItems as Record<string, { Keys: Key[]; ConsistentRead: boolean }>)[T]!;
      expect(req.ConsistentRead).toBe(true);
      expect(req.Keys.length).toBeLessThanOrEqual(100);
      if (first) {
        first = false;
        return { Responses: { [T]: req.Keys.slice(10) }, UnprocessedKeys: { [T]: { Keys: req.Keys.slice(0, 10) } } };
      }
      return { Responses: { [T]: req.Keys } };
    });
    const sleeps: number[] = [];
    const store = new DynamoTransactStore({ tableName: T, client, sleep: async (ms) => void sleeps.push(ms) });
    const got = await store.batchGet(keys);
    expect(new Set(got.map((k) => k.PK))).toEqual(new Set(keys.map((k) => k.PK)));
    expect(client.inputs).toHaveLength(3);
    expect(sleeps).toHaveLength(1);
  });

  it("queries consistently by prefix and follows LastEvaluatedKey", async () => {
    const client = fakeSender((input) =>
      input.ExclusiveStartKey ? { Items: [{ PK: "P", SK: "MEMBER#b" }] } : { Items: [{ PK: "P", SK: "MEMBER#a" }], LastEvaluatedKey: { PK: "P", SK: "MEMBER#a" } },
    );
    const store = new DynamoTransactStore({ tableName: T, client });
    expect((await store.query("P", "MEMBER#")).map((i) => i.SK)).toEqual(["MEMBER#a", "MEMBER#b"]);
    expect(client.inputs[0]).toMatchObject({
      TableName: T,
      ConsistentRead: true,
      KeyConditionExpression: "#n0 = :v0 AND begins_with(#n1, :v1)",
      ExpressionAttributeValues: { ":v0": "P", ":v1": "MEMBER#" },
    });
  });

  it("is configured from PROJECTS_TABLE and PROJECTS_DDB_ENDPOINT", async () => {
    expect(dynamoTransactStoreFromEnv({})).toBeNull();
    const store = dynamoTransactStoreFromEnv({ PROJECTS_TABLE: T, PROJECTS_DDB_ENDPOINT: "http://localhost:5000" });
    expect(store).toBeInstanceOf(DynamoTransactStore);
    const config = (store as unknown as { client: { config: { endpoint?: () => Promise<{ hostname: string; port?: number }> } } })
      .client.config;
    expect(await config.endpoint?.()).toMatchObject({ hostname: "localhost", port: 5000 });
  });

  it("compiles a condition check from its required condition", () => {
    expect(compileTransactOp({ kind: "conditionCheck", key: metaKey(PID), condition: cond.eq("status", "active") }, T)).toEqual({
      ConditionCheck: {
        TableName: T,
        Key: metaKey(PID),
        ConditionExpression: "#n0 = :v0",
        ExpressionAttributeNames: { "#n0": "status" },
        ExpressionAttributeValues: { ":v0": "active" },
      },
    });
  });
});

describe("schema", () => {
  it("builds keys and refuses malformed ids and empty subs", () => {
    expect(metaKey(PID)).toEqual({ PK: `PROJECT#${PID}`, SK: "META" });
    expect(projectPrefix(PID)).toBe(`projects/${PID}/`);
    expect(() => metaKey("default")).toThrow();
    expect(() => countersKey("")).toThrow();
  });
});

describe("DynamoTransactStore.queryIndex projection", () => {
  it("sends a ProjectionExpression over name placeholders, and none without a projection", async () => {
    const client = fakeSender(() => ({ Items: [] }));
    const store = new DynamoTransactStore({ tableName: T, client });
    await store.queryIndex({ indexName: "assetsByRegistered", partitionAttr: "assetListPk", partitionValue: "PROJECT#x",
      projection: ["status", "role", "folder"] });
    const input = client.inputs[0] as any;
    const names = input.ExpressionAttributeNames as Record<string, string>;
    expect(input.ProjectionExpression.split(", ").map((id: string) => names[id])).toEqual(["status", "role", "folder"]);
    await store.queryIndex({ indexName: "assetsByRegistered", partitionAttr: "assetListPk", partitionValue: "PROJECT#x" });
    expect((client.inputs[1] as any).ProjectionExpression).toBeUndefined();
  });
});
