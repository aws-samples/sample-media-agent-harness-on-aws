// Transaction store for the Project_Registry (media-harness-projects-access design Data Models
// "Projects table", D5).
//
// Every registry write is ONE `TransactWriteItems` whose conditions encode the invariants, and every read is
// consistent. `TransactStore` is that narrow surface: `transactWrite`, a consistent `batchGet`, and a
// consistent `query`. Conditions and updates are a small structured form rather than raw expression
// strings, so the same op runs against DynamoDB (compiled here) and against `InMemoryTransactStore`
// (evaluated in `inMemoryStore.ts`) with one meaning. Only what the registry's transaction table needs is
// expressible: attribute_exists / attribute_not_exists, `= < <= > >=` against a scalar, AND, OR; and
// SET / ADD / REMOVE on top-level attributes.
//
// A cancelled transaction surfaces as `TransactionCancelledError` with one reason per op, in op order, so
// the caller knows WHICH condition failed (`ConditionalCheckFailed`) or that DynamoDB cancelled it for a
// concurrent writer (`TransactionConflict`). The AWS SDK does not retry `TransactionCanceledException`;
// the registry's retry policy (task 6.2) does.
//
// Env: `PROJECTS_TABLE` names the table; `PROJECTS_DDB_ENDPOINT`, when set, overrides the DynamoDB
// endpoint (local dev points it at a moto server, which accepts any static credentials, e.g.
// AWS_ACCESS_KEY_ID=testing).
import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import {
  BatchGetCommand,
  DynamoDBDocumentClient,
  QueryCommand,
  TransactWriteCommand,
} from "@aws-sdk/lib-dynamodb";
import type { TransactWriteCommandInput } from "@aws-sdk/lib-dynamodb";
import type { Key } from "./schema";

export type { Key } from "./schema";

/** A stored item. Always carries its string `PK` and `SK`. */
export type Item = Key & Record<string, unknown>;

export type Scalar = string | number | boolean;

export type Condition =
  | { kind: "exists"; attr: string }
  | { kind: "notExists"; attr: string }
  | { kind: "cmp"; attr: string; op: "="; value: Scalar }
  | { kind: "cmp"; attr: string; op: "<" | "<=" | ">" | ">="; value: string | number }
  | { kind: "and"; of: Condition[] }
  | { kind: "or"; of: Condition[] };

/** Condition builders. `cond.notExists("PK")` is "the item does not exist". */
export const cond = {
  exists: (attr: string): Condition => ({ kind: "exists", attr }),
  notExists: (attr: string): Condition => ({ kind: "notExists", attr }),
  eq: (attr: string, value: Scalar): Condition => ({ kind: "cmp", attr, op: "=", value }),
  lt: (attr: string, value: string | number): Condition => ({ kind: "cmp", attr, op: "<", value }),
  le: (attr: string, value: string | number): Condition => ({ kind: "cmp", attr, op: "<=", value }),
  gt: (attr: string, value: string | number): Condition => ({ kind: "cmp", attr, op: ">", value }),
  ge: (attr: string, value: string | number): Condition => ({ kind: "cmp", attr, op: ">=", value }),
  and: (...of: Condition[]): Condition => ({ kind: "and", of }),
  or: (...of: Condition[]): Condition => ({ kind: "or", of }),
};

export type TransactOp =
  | { kind: "put"; item: Item; condition?: Condition }
  | { kind: "delete"; key: Key; condition?: Condition }
  | {
      kind: "update";
      key: Key;
      /** SET attr = value. */
      set?: Record<string, unknown>;
      /** ADD attr n (an absent attribute counts as 0; an absent item is created). */
      add?: Record<string, number>;
      /** REMOVE attr. */
      remove?: string[];
      condition?: Condition;
    }
  | { kind: "conditionCheck"; key: Key; condition: Condition };

/** DynamoDB's per-transaction item limit. */
export const MAX_TRANSACT_ITEMS = 100;
/** DynamoDB's per-request `BatchGetItem` key limit. */
export const MAX_BATCH_GET_KEYS = 100;

export type CancellationCode =
  | "None"
  | "ConditionalCheckFailed"
  | "TransactionConflict"
  // Other codes DynamoDB can report (throttling, item collection size, validation).
  | (string & {});

export interface CancellationReason {
  code: CancellationCode;
  message?: string;
}

/** The transaction was cancelled and changed nothing. `reasons[i]` belongs to `ops[i]`. */
export class TransactionCancelledError extends Error {
  readonly reasons: readonly CancellationReason[];

  constructor(reasons: readonly CancellationReason[], message?: string) {
    super(message ?? `Transaction cancelled, reasons [${reasons.map((r) => r.code).join(", ")}]`);
    this.name = "TransactionCancelledError";
    this.reasons = reasons;
  }

  /** Indices of the ops whose condition failed. */
  get failedConditions(): number[] {
    return indicesWith(this.reasons, "ConditionalCheckFailed");
  }

  /** Whether DynamoDB cancelled it for a concurrent writer (retryable under the registry policy). */
  get hasConflict(): boolean {
    return this.reasons.some((r) => r.code === "TransactionConflict");
  }
}

/** The request was malformed (DynamoDB `ValidationException`), e.g. one transaction naming an item twice. */
export class StoreValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "StoreValidationError";
  }
}

export interface TransactStore {
  /** Apply every op atomically, or none (throws `TransactionCancelledError` / `StoreValidationError`). */
  transactWrite(ops: readonly TransactOp[]): Promise<void>;
  /** Consistent read of any number of distinct keys; absent items are omitted; order is unspecified. */
  batchGet(keys: readonly Key[]): Promise<Item[]>;
  /** Consistent read of every item under `pk` whose SK begins with `skPrefix`, in ascending SK order. */
  query(pk: string, skPrefix?: string): Promise<Item[]>;
}

/** One page of a GSI query (ingestion `assetsByRegistered`). GSIs are eventually consistent. */
export interface IndexQueryOptions {
  /** The GSI name, e.g. `assetsByRegistered`. */
  indexName: string;
  /** The GSI partition attribute and the value to match (e.g. `assetListPk = "PROJECT#<pid>"`). */
  partitionAttr: string;
  partitionValue: string;
  /** The GSI sort attribute (used only by the in-memory twin to order a page; DynamoDB orders by the GSI). */
  sortAttr?: string;
  /** Ascending by the GSI sort key; false for newest-first. Defaults to true. */
  ascending?: boolean;
  /** Page size. */
  limit?: number;
  /** The `LastEvaluatedKey` of the previous page, to continue. */
  exclusiveStartKey?: Record<string, unknown>;
  /**
   * Only these attributes of each item (a `ProjectionExpression`). The GSI projects ALL, and an ASSET row carries its
   * ingestion `steps` (~20 KB each), so a whole-project scan that needs four fields read ~30x what it used.
   */
  projection?: readonly string[];
}

export interface IndexQueryResult {
  items: Item[];
  /** Present only when a further page exists. */
  lastEvaluatedKey?: Record<string, unknown>;
}

/** A store that can also read a GSI page. The two concrete stores implement it; the test fakes need not. */
export interface IndexQueryStore {
  queryIndex(opts: IndexQueryOptions): Promise<IndexQueryResult>;
}

/** A stable string id for a key, for maps and duplicate detection. */
export function keyId(key: Key): string {
  return JSON.stringify([key.PK, key.SK]);
}

/** The key of the item an op names. */
export function opKey(op: TransactOp): Key {
  return op.kind === "put" ? { PK: op.item.PK, SK: op.item.SK } : op.key;
}

// ------------------------------------------------------------------------------- shared validation

/**
 * Request-shape checks DynamoDB performs before executing anything (its `ValidationException`s). Both
 * stores run them, so a request the in-memory store accepts is one DynamoDB would accept.
 */
export function validateTransaction(ops: readonly TransactOp[]): void {
  if (ops.length === 0) throw new StoreValidationError("A transaction must contain at least one op");
  if (ops.length > MAX_TRANSACT_ITEMS) {
    throw new StoreValidationError(`A transaction cannot contain more than ${MAX_TRANSACT_ITEMS} ops`);
  }
  const seen = new Set<string>();
  for (const op of ops) {
    const key = opKey(op);
    validateKey(key);
    const id = keyId(key);
    if (seen.has(id)) {
      throw new StoreValidationError("Transaction request cannot include multiple operations on one item");
    }
    seen.add(id);
    if (op.condition) validateCondition(op.condition);
    if (op.kind === "put") {
      for (const [attr, v] of Object.entries(op.item)) {
        if (v === undefined) throw new StoreValidationError(`Put attribute ${attr} is undefined`);
      }
    } else if (op.kind === "update") {
      validateUpdate(op);
    }
  }
}

export function validateKeys(keys: readonly Key[]): void {
  const seen = new Set<string>();
  for (const key of keys) {
    validateKey(key);
    const id = keyId(key);
    if (seen.has(id)) throw new StoreValidationError("Provided list of item keys contains duplicates");
    seen.add(id);
  }
}

function validateKey(key: Key): void {
  if (typeof key.PK !== "string" || key.PK === "" || typeof key.SK !== "string" || key.SK === "") {
    throw new StoreValidationError("Key attributes PK and SK must be non-empty strings");
  }
}

function validateCondition(c: Condition): void {
  if ((c.kind === "and" || c.kind === "or") && c.of.length === 0) {
    throw new StoreValidationError(`Empty ${c.kind.toUpperCase()} condition`);
  }
  if (c.kind === "and" || c.kind === "or") c.of.forEach(validateCondition);
}

function validateUpdate(op: Extract<TransactOp, { kind: "update" }>): void {
  const paths = [
    ...Object.entries(op.set ?? {}).map(([attr, v]) => {
      if (v === undefined) throw new StoreValidationError(`SET attribute ${attr} is undefined`);
      return attr;
    }),
    ...Object.entries(op.add ?? {}).map(([attr, n]) => {
      if (typeof n !== "number" || !Number.isFinite(n)) {
        throw new StoreValidationError(`ADD ${attr} needs a finite number`);
      }
      return attr;
    }),
    ...(op.remove ?? []),
  ];
  if (paths.length === 0) throw new StoreValidationError("An update must SET, ADD, or REMOVE something");
  const seen = new Set<string>();
  for (const p of paths) {
    if (p === "PK" || p === "SK") throw new StoreValidationError("Cannot update attribute PK or SK: part of the key");
    if (seen.has(p)) throw new StoreValidationError(`Two document paths overlap: ${p}`);
    seen.add(p);
  }
}

// ---------------------------------------------------------------------------------- DynamoDB impl

/** Expression attribute name/value placeholders shared by one op's condition and update expressions. */
class ExprBuilder {
  readonly names: Record<string, string> = {};
  readonly values: Record<string, unknown> = {};
  private nameIds = new Map<string, string>();
  private valueCount = 0;

  name(attr: string): string {
    let id = this.nameIds.get(attr);
    if (!id) {
      id = `#n${this.nameIds.size}`;
      this.nameIds.set(attr, id);
      this.names[id] = attr;
    }
    return id;
  }

  value(v: unknown): string {
    const id = `:v${this.valueCount++}`;
    this.values[id] = v;
    return id;
  }

  condition(c: Condition): string {
    switch (c.kind) {
      case "exists":
        return `attribute_exists(${this.name(c.attr)})`;
      case "notExists":
        return `attribute_not_exists(${this.name(c.attr)})`;
      case "cmp":
        return `${this.name(c.attr)} ${c.op} ${this.value(c.value)}`;
      case "and":
      case "or":
        return `(${c.of.map((x) => this.condition(x)).join(c.kind === "and" ? " AND " : " OR ")})`;
    }
  }

  /** Spread into a request: omits empty maps, which DynamoDB rejects. */
  attrs(): { ExpressionAttributeNames?: Record<string, string>; ExpressionAttributeValues?: Record<string, unknown> } {
    return {
      ...(Object.keys(this.names).length ? { ExpressionAttributeNames: this.names } : {}),
      ...(Object.keys(this.values).length ? { ExpressionAttributeValues: this.values } : {}),
    };
  }
}

export type CompiledTransactItem = NonNullable<TransactWriteCommandInput["TransactItems"]>[number];

/** The `TransactItems` entry for one op (exported for tests of the compiled expressions). */
export function compileTransactOp(op: TransactOp, tableName: string): CompiledTransactItem {
  const b = new ExprBuilder();
  const condition =
    op.kind !== "conditionCheck" && op.condition ? { ConditionExpression: b.condition(op.condition) } : {};
  switch (op.kind) {
    case "put":
      return { Put: { TableName: tableName, Item: op.item, ...condition, ...b.attrs() } };
    case "delete":
      return { Delete: { TableName: tableName, Key: { PK: op.key.PK, SK: op.key.SK }, ...condition, ...b.attrs() } };
    case "conditionCheck": {
      const ConditionExpression = b.condition(op.condition);
      return { ConditionCheck: { TableName: tableName, Key: { PK: op.key.PK, SK: op.key.SK }, ConditionExpression, ...b.attrs() } };
    }
    case "update": {
      const clauses: string[] = [];
      const set = Object.entries(op.set ?? {}).map(([a, v]) => `${b.name(a)} = ${b.value(v)}`);
      if (set.length) clauses.push(`SET ${set.join(", ")}`);
      const add = Object.entries(op.add ?? {}).map(([a, n]) => `${b.name(a)} ${b.value(n)}`);
      if (add.length) clauses.push(`ADD ${add.join(", ")}`);
      const remove = (op.remove ?? []).map((a) => b.name(a));
      if (remove.length) clauses.push(`REMOVE ${remove.join(", ")}`);
      return {
        Update: {
          TableName: tableName,
          Key: { PK: op.key.PK, SK: op.key.SK },
          UpdateExpression: clauses.join(" "),
          ...condition,
          ...b.attrs(),
        },
      };
    }
  }
}

/** The subset of `DynamoDBDocumentClient` the store calls (injectable for tests). */
export interface DocumentSender {
  send(command: TransactWriteCommand | BatchGetCommand | QueryCommand): Promise<unknown>;
}

export interface DynamoTransactStoreOptions {
  tableName: string;
  client: DocumentSender;
  /** Backoff sleep for `UnprocessedKeys` re-requests (injectable for tests). */
  sleep?: (ms: number) => Promise<void>;
  /** Re-request rounds for `UnprocessedKeys` before giving up. */
  maxUnprocessedRounds?: number;
}

const UNPROCESSED_BASE_MS = 50;
const UNPROCESSED_CAP_MS = 2_000;
const DEFAULT_UNPROCESSED_ROUNDS = 10;

export class DynamoTransactStore implements TransactStore, IndexQueryStore {
  private readonly tableName: string;
  private readonly client: DocumentSender;
  private readonly sleep: (ms: number) => Promise<void>;
  private readonly maxUnprocessedRounds: number;

  constructor(opts: DynamoTransactStoreOptions) {
    if (!opts.tableName) throw new Error("DynamoTransactStore needs a table name");
    this.tableName = opts.tableName;
    this.client = opts.client;
    this.sleep = opts.sleep ?? ((ms) => new Promise((r) => setTimeout(r, ms)));
    this.maxUnprocessedRounds = opts.maxUnprocessedRounds ?? DEFAULT_UNPROCESSED_ROUNDS;
  }

  async transactWrite(ops: readonly TransactOp[]): Promise<void> {
    validateTransaction(ops);
    const TransactItems = ops.map((op) => compileTransactOp(op, this.tableName));
    try {
      await this.client.send(new TransactWriteCommand({ TransactItems }));
    } catch (e) {
      throw translateWriteError(e, ops.length);
    }
  }

  async batchGet(keys: readonly Key[]): Promise<Item[]> {
    validateKeys(keys);
    const out: Item[] = [];
    for (let i = 0; i < keys.length; i += MAX_BATCH_GET_KEYS) {
      let pending: Key[] = keys.slice(i, i + MAX_BATCH_GET_KEYS).map((k) => ({ PK: k.PK, SK: k.SK }));
      for (let round = 0; pending.length > 0; round++) {
        if (round > this.maxUnprocessedRounds) {
          throw new Error(`BatchGetItem left ${pending.length} keys unprocessed after ${this.maxUnprocessedRounds} retries`);
        }
        if (round > 0) await this.sleep(fullJitter(round));
        const res = (await this.client.send(
          new BatchGetCommand({
            RequestItems: { [this.tableName]: { Keys: pending, ConsistentRead: true } },
          }),
        )) as {
          Responses?: Record<string, Item[]>;
          UnprocessedKeys?: Record<string, { Keys?: Key[] }>;
        };
        out.push(...(res.Responses?.[this.tableName] ?? []));
        pending = res.UnprocessedKeys?.[this.tableName]?.Keys ?? [];
      }
    }
    return out;
  }

  async query(pk: string, skPrefix = ""): Promise<Item[]> {
    const b = new ExprBuilder();
    const keyCondition =
      `${b.name("PK")} = ${b.value(pk)}` + (skPrefix ? ` AND begins_with(${b.name("SK")}, ${b.value(skPrefix)})` : "");
    const out: Item[] = [];
    let ExclusiveStartKey: Record<string, unknown> | undefined;
    do {
      const res = (await this.client.send(
        new QueryCommand({
          TableName: this.tableName,
          KeyConditionExpression: keyCondition,
          ConsistentRead: true,
          ...b.attrs(),
          ...(ExclusiveStartKey ? { ExclusiveStartKey } : {}),
        }),
      )) as { Items?: Item[]; LastEvaluatedKey?: Record<string, unknown> };
      out.push(...(res.Items ?? []));
      ExclusiveStartKey = res.LastEvaluatedKey;
    } while (ExclusiveStartKey);
    return out;
  }

  /**
   * One page of a GSI query. A GSI cannot be read consistently, so this omits `ConsistentRead`; no
   * access decision reads the index (`checkAccess` runs first on the base table; ingestion §2 Data
   * Models), so the eventual consistency is acceptable.
   */
  async queryIndex(opts: IndexQueryOptions): Promise<IndexQueryResult> {
    const b = new ExprBuilder();
    const keyCondition = `${b.name(opts.partitionAttr)} = ${b.value(opts.partitionValue)}`;
    const res = (await this.client.send(
      new QueryCommand({
        TableName: this.tableName,
        IndexName: opts.indexName,
        KeyConditionExpression: keyCondition,
        ScanIndexForward: opts.ascending ?? true,
        ...(opts.limit !== undefined ? { Limit: opts.limit } : {}),
        ...(opts.exclusiveStartKey ? { ExclusiveStartKey: opts.exclusiveStartKey } : {}),
        ...(opts.projection?.length ? { ProjectionExpression: opts.projection.map((a) => b.name(a)).join(", ") } : {}),
        ...b.attrs(),
      }),
    )) as { Items?: Item[]; LastEvaluatedKey?: Record<string, unknown> };
    return {
      items: res.Items ?? [],
      ...(res.LastEvaluatedKey ? { lastEvaluatedKey: res.LastEvaluatedKey } : {}),
    };
  }
}

function fullJitter(round: number): number {
  return Math.random() * Math.min(UNPROCESSED_CAP_MS, UNPROCESSED_BASE_MS * 2 ** round);
}

function translateWriteError(e: unknown, opCount: number): unknown {
  const err = e as { name?: string; message?: string; CancellationReasons?: { Code?: string; Message?: string }[] };
  if (err?.name === "TransactionCanceledException") {
    const raw = err.CancellationReasons ?? [];
    const reasons: CancellationReason[] = Array.from({ length: opCount }, (_, i) => ({
      code: raw[i]?.Code ?? "None",
      ...(raw[i]?.Message ? { message: raw[i]!.Message } : {}),
    }));
    return new TransactionCancelledError(reasons, err.message);
  }
  if (err?.name === "ValidationException") return new StoreValidationError(err.message ?? "ValidationException");
  return e;
}

/**
 * The projects store from env: `PROJECTS_TABLE` (null when unset, like the proxy's other optional tables)
 * and the optional `PROJECTS_DDB_ENDPOINT` override.
 */
export function dynamoTransactStoreFromEnv(env: NodeJS.ProcessEnv = process.env): DynamoTransactStore | null {
  const tableName = env.PROJECTS_TABLE ?? "";
  if (!tableName) return null;
  const endpoint = env.PROJECTS_DDB_ENDPOINT || undefined;
  const client = DynamoDBDocumentClient.from(
    new DynamoDBClient({ region: env.AWS_REGION ?? "us-west-2", ...(endpoint ? { endpoint } : {}) }),
  );
  return new DynamoTransactStore({ tableName, client });
}

function indicesWith(reasons: readonly CancellationReason[], code: CancellationCode): number[] {
  return reasons.flatMap((r, i) => (r.code === code ? [i] : []));
}
