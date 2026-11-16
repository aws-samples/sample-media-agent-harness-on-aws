// Folders over the proxy routes (design D20): the uploads route threads `folder` into the object's signed
// metadata (and the grant), register passes an explicit folder, the asset views carry `folder`, `?q=`
// matches the folder path, and `POST …/assets/duplicates` finds already-uploaded files in one GSI pass.
import { Readable } from "node:stream";
import { S3Client } from "@aws-sdk/client-s3";
import { describe, expect, it } from "vitest";
import { ACCESS_DENIED } from "./access";
import type { AccessResult } from "./access";
import type { AssetServiceCall, AssetServiceReply } from "./assetServiceClient";
import { InMemoryTransactStore } from "./inMemoryStore";
import { DUPLICATES_MAX_FILES, PROJECT_SCAN_PAGE, dispatchAssetRoute, scanProjectAssets } from "./ingestionRoutes";
import { ASSET_LIST_PK_ATTR, ASSET_LIST_SK_ATTR, assetKey, projectPk } from "./schema";
import type { Item } from "./store";
import { dispatchIngestionWriteRoute, s3UploadOps } from "./uploadRoutes";
import type { IngestionWriteDeps, UploadS3 } from "./uploadRoutes";

const PID = "p0123456789abcdef0123456789abcdef";
const ACTOR = "sub-alice";
const BUCKET = "ingest-bucket";
const MIB = 1024 * 1024;

const allow = async () => ({ ok: true } as AccessResult);
const deny = async () => ACCESS_DENIED;

function makeReq(method: string, body?: unknown, url = "/"): any {
  const payload = body === undefined ? [] : [Buffer.from(typeof body === "string" ? body : JSON.stringify(body), "utf8")];
  const req: any = Readable.from(payload);
  req.method = method;
  req.url = url;
  return req;
}

function makeRes() {
  let resolve!: (c: { status?: number; body: any }) => void;
  const done = new Promise<{ status?: number; body: any }>((r) => (resolve = r));
  const res: any = {
    headersSent: false,
    writeHead(status: number) {
      this._status = status;
      this.headersSent = true;
    },
    end(body?: string) {
      resolve({ status: this._status, body: body ? JSON.parse(body) : undefined });
    },
  };
  return { res, done };
}

class RecordingS3 implements Partial<UploadS3> {
  puts: unknown[][] = [];
  creates: unknown[][] = [];
  async presignPut(...args: unknown[]) {
    this.puts.push(args);
    return "https://s3.example/put";
  }
  async createMultipartUpload(...args: unknown[]) {
    this.creates.push(args);
    return "up-1";
  }
  async presignUploadPart() {
    return "https://s3.example/part";
  }
}

let seq = 0;
async function write(parts: string[], body: unknown, deps: IngestionWriteDeps) {
  const { res, done } = makeRes();
  await dispatchIngestionWriteRoute(makeReq("POST", body), res, { actor: ACTOR, pid: PID, parts, method: "POST", userToken: "jwt" }, {
    ingestBucket: BUCKET, env: {}, newUuid: () => `00000000-0000-4000-8000-${String(++seq).padStart(12, "0")}`, ...deps,
  });
  return done;
}

async function read(method: string, parts: string[], store: unknown, opts: { body?: unknown; query?: string; checkAccess?: any } = {}) {
  const { res, done } = makeRes();
  const handled = await dispatchAssetRoute(makeReq(method, opts.body, `/${parts.join("/")}${opts.query ?? ""}`), res,
    { actor: ACTOR, pid: PID, parts, method }, { checkAccess: opts.checkAccess ?? allow, store: store as any });
  return handled ? done : { status: undefined, body: undefined };
}

let rowSeq = 0;
function row(displayName: string, over: Partial<Item> = {}): Item {
  const n = ++rowSeq;
  const id = `video:00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
  const registeredAt = `2026-10-01T00:00:${String(n % 60).padStart(2, "0")}.${String(n).padStart(3, "0")}Z`;
  return {
    ...assetKey(PID, id),
    assetId: id,
    kind: "video",
    source: "upload",
    sourceUri: `s3://${BUCKET}/projects/${PID}/${id}/x.mp4`,
    sourceSize: 100,
    displayName,
    status: "stored",
    registeredAt,
    [ASSET_LIST_PK_ATTR]: projectPk(PID),
    [ASSET_LIST_SK_ATTR]: `${registeredAt}#${id}`,
    ...over,
  } as Item;
}

// ------------------------------------------------------------------------------------------- uploads

describe("POST /projects/:pid/uploads with folder", () => {
  it("signs the normalized folder as metadata (header for a PUT, server-side for multipart) and echoes it", async () => {
    const s3 = new RecordingS3();
    const r = await write([PID, "uploads"], {
      files: [
        { name: "a.mp4", size: 10, contentType: "video/mp4", folder: " Footage / Día 1/" },
        { name: "b.mp4", size: 200 * MIB, contentType: "video/mp4", folder: "Footage/Day 2" },
        { name: "c.mp4", size: 10, contentType: "video/mp4" },
      ],
    }, { checkAccess: allow, s3: s3 as unknown as UploadS3 });
    expect(r.status).toBe(200);
    const [a, b, c] = r.body.uploads;
    expect(a.folder).toBe("Footage/Día 1");
    expect(a.key).toBe(`projects/${PID}/${a.assetId}/a.mp4`); // the key layout does not change
    expect(a.headers).toEqual({ "Content-Type": "video/mp4", "x-amz-meta-harness-folder": encodeURIComponent("Footage/Día 1") });
    expect(s3.puts[0][4]).toEqual({ "harness-folder": encodeURIComponent("Footage/Día 1") });
    expect(b.folder).toBe("Footage/Day 2");
    expect(s3.creates[0][3]).toEqual({ "harness-folder": encodeURIComponent("Footage/Day 2") });
    expect(c.folder).toBeUndefined();
    expect(c.headers).toEqual({ "Content-Type": "video/mp4" });
    expect(s3.puts[1][4]).toBeUndefined();
  });

  it("refuses the whole request with 400 invalid_folder before presigning anything", async () => {
    const s3 = new RecordingS3();
    const r = await write([PID, "uploads"], {
      files: [{ name: "a.mp4", size: 10, contentType: "video/mp4" }, { name: "b.mp4", size: 10, folder: "x/../y" }],
    }, { checkAccess: allow, s3: s3 as unknown as UploadS3 });
    expect(r.status).toBe(400);
    expect(r.body.error).toMatchObject({ code: "invalid_folder", index: 1, reason: "dot_segment" });
    expect(s3.puts).toEqual([]);
    const notString = await write([PID, "uploads"], { files: [{ name: "a.mp4", size: 10, folder: 7 }] },
      { checkAccess: allow, s3: s3 as unknown as UploadS3 });
    expect(notString.body.error.code).toBe("invalid_folder");
  });

  it("the real presigner keeps the folder a signed header, not a query parameter", async () => {
    const client = new S3Client({ region: "us-west-2", credentials: { accessKeyId: "AKIDEXAMPLE", secretAccessKey: "s" },
      requestChecksumCalculation: "WHEN_REQUIRED" });
    const url = new URL(await s3UploadOps(client).presignPut(BUCKET, "k", "video/mp4", 3600, { "harness-folder": "Footage%2FDay%201" }));
    expect(url.searchParams.get("X-Amz-SignedHeaders")).toBe("content-type;host;x-amz-meta-harness-folder");
    expect([...url.searchParams.keys()].some((k) => k.toLowerCase().startsWith("x-amz-meta-"))).toBe(false);
  });
});

describe("POST /projects/:pid/assets (register) with folder", () => {
  const AID = "video:00000000-0000-4000-8000-0000000000aa";
  function service() {
    const calls: AssetServiceCall[] = [];
    const invoke = async (call: AssetServiceCall): Promise<AssetServiceReply> => {
      calls.push(call);
      return { ok: true, value: { asset: { assetId: AID, kind: "video", displayName: "a.mp4", status: "queued", folder: call.args.folder } } };
    };
    return { calls, invoke };
  }

  it("passes a normalized explicit folder, omits an absent one, refuses an invalid one", async () => {
    const svc = service();
    const base = { assetId: AID, fileName: "a.mp4", contentType: "video/mp4", size: 10 };
    const r = await write([PID, "assets"], { ...base, folder: "Footage//Day 1/" }, { checkAccess: allow, invokeAssetService: svc.invoke });
    expect(r.status).toBe(200);
    expect(svc.calls[0].args).toEqual({ ...base, folder: "Footage/Day 1" });
    expect(r.body.folder).toBe("Footage/Day 1"); // the asset view carries it
    await write([PID, "assets"], base, { checkAccess: allow, invokeAssetService: svc.invoke });
    expect(svc.calls[1].args).toEqual(base);
    const bad = await write([PID, "assets"], { ...base, folder: "a[1]" }, { checkAccess: allow, invokeAssetService: svc.invoke });
    expect(bad.status).toBe(400);
    expect(bad.body.error.code).toBe("invalid_folder");
    expect(svc.calls).toHaveLength(2);
  });
});

// ------------------------------------------------------------------------------------ list and search

describe("asset list/search with folders", () => {
  it("list and detail views carry folder; ?q= matches the folder path", async () => {
    const store = new InMemoryTransactStore();
    const inDay1 = row("a.mp4", { folder: "Footage/Day 1" });
    const inDay2 = row("b.mp4", { folder: "Footage/Day 2" });
    const atRoot = row("day 1 notes.mp4");
    store.seed(inDay1, inDay2, atRoot);
    const list = await read("GET", [PID, "assets"], store);
    const byName = Object.fromEntries(list.body.assets.map((a: any) => [a.displayName, a]));
    expect(byName["a.mp4"].folder).toBe("Footage/Day 1");
    expect("folder" in byName["day 1 notes.mp4"]).toBe(false);
    const detail = await read("GET", [PID, "assets", String(inDay2.assetId)], store);
    expect(detail.body.folder).toBe("Footage/Day 2");

    const q = async (s: string) => (await read("GET", [PID, "assets"], store, { query: `?q=${encodeURIComponent(s)}` })).body.assets
      .map((a: any) => a.displayName).sort();
    expect(await q("DAY 1")).toEqual(["a.mp4", "day 1 notes.mp4"]);
    expect(await q("day 1/a")).toEqual(["a.mp4"]);
    expect(await q("footage")).toEqual(["a.mp4", "b.mp4"]);
  });
});

// ------------------------------------------------------------------------------------------ duplicates

describe("POST /projects/:pid/assets/duplicates", () => {
  const dup = (store: unknown, body: unknown, checkAccess: any = allow) =>
    read("POST", [PID, "assets", "duplicates"], store, { body, checkAccess });

  it("matches name (registry-derived, case-insensitive, any ` (n)`) AND size; prefers the unsuffixed original", async () => {
    const store = new InMemoryTransactStore();
    const original = row("Clip.mp4", { folder: "Footage/Day 1" });
    const copy = row("clip.mp4 (2)"); // newer than the original
    const otherSize = row("notes.pdf", { sourceSize: 5 });
    const onlyCopy = row("take.mov (3)", { sourceSize: 7 });
    const removed = row("gone.wav", { status: "removed" });
    const literal = row("draft (2)", { sourceSize: 9 }); // a file whose own name ends in " (2)"
    store.seed(original, copy, otherSize, onlyCopy, removed, literal);
    const r = await dup(store, {
      files: [
        { name: "  [CLIP].mp4", size: 100 }, // derives to "CLIP.mp4"
        { name: "notes.pdf", size: 6 }, // same name, other size → not a duplicate
        { name: "take.mov", size: 7 }, // only a ` (n)` copy exists → that copy
        { name: "gone.wav", size: 100 }, // removed → not a duplicate
        { name: "new.mp4", size: 100 },
        { name: "draft (2)", size: 9 },
        { name: "[]@#", size: 100 }, // nothing left → never a duplicate
      ],
    });
    expect(r.status).toBe(200);
    expect(r.body.duplicates.map((d: any) => [d.index, d.asset.assetId])).toEqual([
      [0, original.assetId],
      [2, onlyCopy.assetId],
      [5, literal.assetId],
    ]);
    expect(r.body.duplicates[0].asset).toMatchObject({ displayName: "Clip.mp4", folder: "Footage/Day 1", sourceSize: 100 });
    expect(r.body.duplicates[0].asset.sourceUri).toBeUndefined();
    expect(r.body.truncated).toBeUndefined();
  });

  it("is one projected pass over the newest-first GSI, stopping once every file has its original", async () => {
    const full = [row("a.mp4"), row("b.mp4"), row("c.mp4")];
    // The scan sees only the projected fields, as DynamoDB returns them; the answer is re-read whole.
    const slim = (r: Item) => ({ assetId: r.assetId, status: r.status, displayName: r.displayName, sourceSize: r.sourceSize });
    const pages = [
      { items: [slim(full[0])], lastEvaluatedKey: { k: 1 } },
      { items: [slim(full[1])], lastEvaluatedKey: { k: 2 } },
      { items: [slim(full[2])] },
    ];
    const seen: any[] = [];
    const store = {
      batchGet: async (keys: any[]) => full.filter((r) => keys.some((k) => k.SK === r.SK)),
      queryIndex: async (o: any) => (seen.push(o), pages[seen.length - 1]),
    };
    const r = await dup(store, { files: [{ name: "a.mp4", size: 100 }, { name: "b.mp4", size: 100 }] });
    expect(r.body.duplicates.map((d: any) => [d.index, d.asset.assetId])).toEqual([[0, full[0].assetId], [1, full[1].assetId]]);
    expect(r.body.duplicates[0].asset).toMatchObject({ displayName: "a.mp4", kind: "video", sourceSize: 100 });
    expect(seen).toHaveLength(2); // never read page 3
    expect(seen[0]).toMatchObject({ indexName: "assetsByRegistered", ascending: false, limit: PROJECT_SCAN_PAGE,
      projection: ["assetId", "status", "displayName", "sourceSize"] });
    expect(seen[1].exclusiveStartKey).toEqual({ k: 1 });
  });

  it("is the uniform 404 on denial (reading nothing) and validates the body", async () => {
    let reads = 0;
    const store = { batchGet: async () => [], queryIndex: async () => (reads++, { items: [] }) };
    const denied = await dup(store, { files: [{ name: "a.mp4", size: 1 }] }, deny);
    expect(denied.status).toBe(404);
    expect(denied.body.error.code).toBe("project_not_found");
    expect(reads).toBe(0);
    expect((await dup(store, { files: [] })).status).toBe(400);
    expect((await dup(store, { files: [{ name: "a", size: -1 }] })).status).toBe(400);
    expect((await dup(store, "{nope")).status).toBe(400);
    const many = Array.from({ length: DUPLICATES_MAX_FILES + 1 }, (_, i) => ({ name: `f${i}`, size: 1 }));
    const tooMany = await dup(store, { files: many });
    expect(tooMany.status).toBe(400);
    expect(tooMany.body.error.code).toBe("too_many_files");
    const ok = await dup(store, { files: many.slice(0, DUPLICATES_MAX_FILES) });
    expect(ok.status).toBe(200);
    const huge = await dup(store, { files: [{ name: "x".repeat(2 * 1024 * 1024), size: 1 }] });
    expect(huge.status).toBe(413);
  });

  it("GET on the path is not the duplicates route", async () => {
    const r = await read("GET", [PID, "assets", "duplicates"], new InMemoryTransactStore());
    expect(r.status).toBe(404); // a detail lookup of a malformed id
  });
});

// ------------------------------------------------------------------------------------ whole-project scans

/** An in-memory store that records every GSI page it was asked for. */
function countingStore(rows: Item[]) {
  const inner = new InMemoryTransactStore();
  inner.seed(...rows);
  const pages: any[] = [];
  const store = {
    batchGet: (keys: any) => inner.batchGet(keys),
    query: (pk: string, sk?: string) => inner.query(pk, sk),
    queryIndex: async (o: any) => { pages.push(o); return inner.queryIndex(o); },
  };
  return { store, pages };
}

describe("whole-project scans", () => {
  const many = (n: number, over: (i: number) => Partial<Item> = () => ({})) =>
    Array.from({ length: n }, (_, i) => row(`clip ${i}.mp4`, { steps: { probe: { log: "x".repeat(200) } }, ...over(i) }));

  it("reads in pages of PROJECT_SCAN_PAGE up to the row bound, and says when the bound ended it", async () => {
    const { store, pages } = countingStore(many(2500));
    const all: Item[] = [];
    expect(await scanProjectAssets(store, PID, { maxRows: 5000 }, (r) => void all.push(r))).toEqual({ truncated: false });
    expect(all).toHaveLength(2500);
    expect(pages.map((p) => p.limit)).toEqual([PROJECT_SCAN_PAGE, PROJECT_SCAN_PAGE, PROJECT_SCAN_PAGE]);

    pages.length = 0;
    let seen = 0;
    expect(await scanProjectAssets(store, PID, { maxRows: 1200 }, () => void seen++)).toEqual({ truncated: true });
    expect(seen).toBe(1200); // exactly the bound: the second page asks for the 200 left
    expect(pages.map((p) => p.limit)).toEqual([PROJECT_SCAN_PAGE, 200]);
  });

  it("a visitor's stop ends the scan without calling it truncated", async () => {
    const { store, pages } = countingStore(many(2500));
    let seen = 0;
    expect(await scanProjectAssets(store, PID, { maxRows: 5000 }, () => (++seen < 10 ? undefined : false))).toEqual({ truncated: false });
    expect(seen).toBe(10);
    expect(pages).toHaveLength(1);
  });

  it("the folder list is one projected read of a 120-file project (it was three 50-row pages of whole rows)", async () => {
    const { store, pages } = countingStore(many(120, (i) => ({ folder: i % 2 ? "Footage/Day 1" : "Footage/Day 2" })));
    const r = await read("GET", [PID, "assets", "folders"], store);
    expect(r.status).toBe(200);
    expect(r.body.folders).toEqual([{ folder: "Footage/Day 1", count: 60 }, { folder: "Footage/Day 2", count: 60 }]);
    expect(pages).toHaveLength(1);
    expect(pages[0].projection).toEqual(["status", "visibility", "folder"]);
  });

  it("a name search and a folder's files scan projected rows but answer with whole asset views", async () => {
    const rows = many(120, (i) => ({ folder: i < 60 ? "A" : "B", contentType: "video/mp4" }));
    const { store, pages } = countingStore(rows);
    const byName = await read("GET", [PID, "assets"], store, { query: `?q=${encodeURIComponent("clip 7")}` });
    expect(byName.status).toBe(200);
    // Every match comes back as a whole view, though the scan read only the search fields.
    expect(byName.body.assets.find((a: any) => a.displayName === "clip 7.mp4"))
      .toMatchObject({ contentType: "video/mp4", sourceSize: 100, folder: "A" });
    expect(byName.body.total).toBe(11); // 7, 70–79
    expect(pages[0].projection).toEqual(expect.arrayContaining(["assetId", "status", "kind", "folder", "displayName"]));
    expect(pages[0].projection).not.toContain("steps");

    const inB = await read("GET", [PID, "assets"], store, { query: "?folder=B" });
    expect(inB.body.total).toBe(60);
    expect(inB.body.assets).toHaveLength(50); // one SEARCH_PAGE, each whole
    expect(inB.body.assets.every((a: any) => a.folder === "B" && a.contentType === "video/mp4")).toBe(true);
  });

  it("a removed asset never reaches a search answer, even when the projected scan still listed it", async () => {
    const live = row("keep.mp4");
    const gone = row("keep too.mp4");
    const { store } = countingStore([live, gone]);
    // The GSI trails the base table: the scan sees `gone` as live, the consistent re-read sees it removed.
    const stale = { ...store, batchGet: async (keys: any) => (await store.batchGet(keys)).map((r: Item) => (r.assetId === gone.assetId ? { ...r, status: "removed" } : r)) };
    const r = await read("GET", [PID, "assets"], stale, { query: "?q=keep" });
    expect(r.body.assets.map((a: any) => a.assetId)).toEqual([live.assetId]);
  });
});
