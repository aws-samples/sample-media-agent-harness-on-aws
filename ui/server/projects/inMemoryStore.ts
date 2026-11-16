// In-memory `TransactStore` with DynamoDB `TransactWriteItems` semantics, for the registry property tests
// (tasks 6.4, 6.5). Reference model: docs/prototypes/media-harness-projects/validation/test_v4_registry_model.py.
//
// What it reproduces, because a fake looser than DynamoDB hides real breakage (AGENTS.md):
//   * all-or-nothing: every condition is evaluated against the pre-transaction state, and nothing is
//     written unless all hold;
//   * one reason per op, in op order (`None` | `ConditionalCheckFailed` | `TransactionConflict`), so the
//     caller learns which condition failed;
//   * the request validation DynamoDB runs first (`StoreValidationError`): one item named twice, more than
//     100 ops, no ops, an update touching a key attribute or naming one path twice;
//   * condition semantics: a comparison against an absent attribute or a value of another type is false,
//     strings compare by UTF-8 bytes, and an absent item has no attributes;
//   * update semantics: ADD on an absent attribute counts from 0, and an update of an absent item creates
//     it (with its key);
//   * `TransactionConflict` cancellation on demand (`injectConflicts`, or a `conflictOracle`), applied after
//     validation and before conditions, like a concurrent writer DynamoDB detected.
//
// Every read and write deep-copies, so callers cannot mutate stored state through a returned item.
import {
  StoreValidationError,
  TransactionCancelledError,
  keyId,
  opKey,
  validateKeys,
  validateTransaction,
} from "./store";
import type {
  CancellationReason,
  Condition,
  IndexQueryOptions,
  IndexQueryResult,
  IndexQueryStore,
  Item,
  Key,
  TransactOp,
  TransactStore,
} from "./store";

/**
 * Decides whether a transaction is cancelled for a conflict: `true` marks every op `TransactionConflict`,
 * a list of indices marks just those (the rest `None`), `false` or `[]` lets it proceed.
 */
export type ConflictOracle = (ops: readonly TransactOp[]) => boolean | readonly number[];

export interface InMemoryTransactStoreOptions {
  conflictOracle?: ConflictOracle;
}

export class InMemoryTransactStore implements TransactStore, IndexQueryStore {
  private readonly items = new Map<string, Item>();
  private conflictOracle: ConflictOracle | undefined;
  private pendingConflicts = 0;
  /** Committed transactions (not counting cancelled or rejected ones). */
  committed = 0;

  constructor(opts: InMemoryTransactStoreOptions = {}) {
    this.conflictOracle = opts.conflictOracle;
  }

  /** Cancel the next `count` otherwise-valid transactions with `TransactionConflict` on every op. */
  injectConflicts(count = 1): void {
    this.pendingConflicts += count;
  }

  setConflictOracle(oracle: ConflictOracle | undefined): void {
    this.conflictOracle = oracle;
  }

  async transactWrite(ops: readonly TransactOp[]): Promise<void> {
    validateTransaction(ops);

    const conflicts = this.takeConflicts(ops);
    if (conflicts.length > 0) {
      const marked = new Set(conflicts);
      throw new TransactionCancelledError(
        ops.map((_, i) => ({ code: marked.has(i) ? "TransactionConflict" : "None" })),
      );
    }

    const reasons: CancellationReason[] = ops.map((op) => {
      const current = this.items.get(keyId(opKey(op)));
      return { code: !op.condition || evaluate(op.condition, current) ? "None" : "ConditionalCheckFailed" };
    });
    if (reasons.some((r) => r.code !== "None")) throw new TransactionCancelledError(reasons);

    // Compute every new item before writing any, so a runtime type error (ADD to a non-number) leaves the
    // store untouched, as DynamoDB would.
    const writes: [string, Item | null][] = ops
      .filter((op) => op.kind !== "conditionCheck")
      .map((op) => {
        const id = keyId(opKey(op));
        return [id, applyOp(op, this.items.get(id))];
      });
    for (const [id, item] of writes) {
      if (item) this.items.set(id, item);
      else this.items.delete(id);
    }
    this.committed++;
  }

  async batchGet(keys: readonly Key[]): Promise<Item[]> {
    validateKeys(keys);
    return keys.flatMap((k) => {
      const item = this.items.get(keyId(k));
      return item ? [structuredClone(item)] : [];
    });
  }

  async query(pk: string, skPrefix = ""): Promise<Item[]> {
    return [...this.items.values()]
      .filter((i) => i.PK === pk && i.SK.startsWith(skPrefix))
      .sort((a, b) => utf8Compare(a.SK, b.SK))
      .map((i) => structuredClone(i));
  }

  /**
   * One page of a GSI query, reproducing DynamoDB's ordering (by the GSI sort key, in UTF-8 byte order)
   * and `ExclusiveStartKey`/`LastEvaluatedKey` paging over the in-memory items. Only items carrying the
   * partition attribute equal to `partitionValue` participate (a sparse GSI).
   */
  async queryIndex(opts: IndexQueryOptions): Promise<IndexQueryResult> {
    const ascending = opts.ascending ?? true;
    const sortAttr = opts.sortAttr ?? "SK";
    let rows = [...this.items.values()].filter((i) => i[opts.partitionAttr] === opts.partitionValue);
    rows.sort((a, b) => {
      const c = utf8Compare(String(a[sortAttr] ?? ""), String(b[sortAttr] ?? ""));
      return ascending ? c : -c;
    });
    const start = opts.exclusiveStartKey;
    if (start) {
      const idx = rows.findIndex((i) => i.PK === start.PK && i.SK === start.SK);
      if (idx >= 0) rows = rows.slice(idx + 1);
    }
    let lastEvaluatedKey: Record<string, unknown> | undefined;
    if (typeof opts.limit === "number" && opts.limit >= 0 && rows.length > opts.limit) {
      rows = rows.slice(0, opts.limit);
      const last = rows[rows.length - 1];
      if (last) {
        lastEvaluatedKey = {
          PK: last.PK,
          SK: last.SK,
          [opts.partitionAttr]: opts.partitionValue,
          [sortAttr]: last[sortAttr],
        };
      }
    }
    // A projection returns ONLY the named attributes, as DynamoDB does (no keys unless named).
    const pick = (i: Item): Item => (opts.projection?.length
      ? Object.fromEntries(opts.projection.filter((a) => a in i).map((a) => [a, structuredClone(i[a])])) as Item
      : structuredClone(i));
    return { items: rows.map(pick), ...(lastEvaluatedKey ? { lastEvaluatedKey } : {}) };
  }

  // ------------------------------------------------------------------------------- test helpers

  /** The item at `key`, or null. */
  get(key: Key): Item | null {
    const item = this.items.get(keyId(key));
    return item ? structuredClone(item) : null;
  }

  /** Write items directly, outside any transaction (fixtures). */
  seed(...items: Item[]): void {
    for (const item of items) this.items.set(keyId(item), structuredClone(item));
  }

  /** Every stored item, ordered by PK then SK. */
  snapshot(): Item[] {
    return [...this.items.values()]
      .sort((a, b) => utf8Compare(a.PK, b.PK) || utf8Compare(a.SK, b.SK))
      .map((i) => structuredClone(i));
  }

  private takeConflicts(ops: readonly TransactOp[]): number[] {
    const all = ops.map((_, i) => i);
    if (this.pendingConflicts > 0) {
      this.pendingConflicts--;
      return all;
    }
    const decision = this.conflictOracle?.(ops) ?? false;
    if (decision === true) return all;
    if (decision === false) return [];
    return decision.filter((i) => Number.isInteger(i) && i >= 0 && i < ops.length);
  }
}

function applyOp(op: Exclude<TransactOp, { kind: "conditionCheck" }>, current: Item | undefined): Item | null {
  switch (op.kind) {
    case "put":
      return structuredClone(op.item);
    case "delete":
      return null;
    case "update": {
      const next: Item = current ? structuredClone(current) : { PK: op.key.PK, SK: op.key.SK };
      for (const [attr, v] of Object.entries(op.set ?? {})) next[attr] = structuredClone(v);
      for (const [attr, n] of Object.entries(op.add ?? {})) {
        const prev = next[attr];
        if (prev !== undefined && typeof prev !== "number") {
          throw new StoreValidationError(`An operand in the update expression has an incorrect data type: ${attr}`);
        }
        next[attr] = (prev ?? 0) + n;
      }
      for (const attr of op.remove ?? []) delete next[attr];
      return next;
    }
  }
}

/** DynamoDB condition semantics over the item's pre-transaction state (`undefined` = absent item). */
export function evaluate(c: Condition, item: Item | undefined): boolean {
  switch (c.kind) {
    case "exists":
      return item !== undefined && item[c.attr] !== undefined;
    case "notExists":
      return item === undefined || item[c.attr] === undefined;
    case "and":
      return c.of.every((x) => evaluate(x, item));
    case "or":
      return c.of.some((x) => evaluate(x, item));
    case "cmp": {
      const actual = item?.[c.attr];
      if (actual === undefined || typeof actual !== typeof c.value) return false;
      if (c.op === "=") return actual === c.value;
      const order =
        typeof actual === "string" ? utf8Compare(actual, c.value as string) : (actual as number) - (c.value as number);
      switch (c.op) {
        case "<":
          return order < 0;
        case "<=":
          return order <= 0;
        case ">":
          return order > 0;
        case ">=":
          return order >= 0;
      }
    }
  }
}

/** DynamoDB orders strings by their UTF-8 bytes, which differs from UTF-16 code-unit order. */
function utf8Compare(a: string, b: string): number {
  return Buffer.compare(Buffer.from(a, "utf8"), Buffer.from(b, "utf8"));
}
