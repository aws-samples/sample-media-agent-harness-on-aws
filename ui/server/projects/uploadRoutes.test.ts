// Unit tests for the proxy ingestion WRITE routes (`uploadRoutes.ts`): capabilities, uploads (single +
// multipart), complete, abort, register, s3-locations, remove, and the chat attachment reference. Everything external is a
// fake: a stub `checkAccess`, an in-memory S3 (`FakeS3`), a scripted Asset_Service, a seeded
// `InMemoryTransactStore`, and a session-owner map. One test runs the REAL presigner (static
// credentials, no network) to prove Content-Type is signed into a single PUT and no empty-body checksum
// is baked into the URLs.
import { Readable } from "node:stream";
import { S3Client } from "@aws-sdk/client-s3";
import { describe, expect, it } from "vitest";
import { ACCESS_DENIED } from "./access";
import type { AccessResult } from "./access";
import type { AssetServiceCall, AssetServiceReply } from "./assetServiceClient";
import { InMemoryTransactStore } from "./inMemoryStore";
import { ASSET_LIST_PK_ATTR, ASSET_LIST_SK_ATTR, assetKey, projectPk } from "./schema";
import type { Item } from "./store";
import { UPLOAD_URL_TTL_S, dispatchIngestionWriteRoute, s3UploadOps, turnIdsOf } from "./uploadRoutes";
import type { IngestionWriteDeps, UploadS3 } from "./uploadRoutes";

const PID = "p0123456789abcdef0123456789abcdef";
const ACTOR = "sub-alice";
const BUCKET = "ingest-bucket";
const MIB = 1024 * 1024;
const NOW = 1_700_000_000_000;

const allow = async () => ({ ok: true } as AccessResult);
const deny = async () => ACCESS_DENIED;

let uuidSeq = 0;
const fixedUuid = () => `00000000-0000-4000-8000-${String(++uuidSeq).padStart(12, "0")}`;

class FakeS3 implements UploadS3 {
  calls: { op: string; args: unknown[] }[] = [];
  objects = new Map<string, string>(); // `${bucket}/${key}` → body tag
  uploads: { bucket: string; key: string; uploadId: string }[] = [];
  private seq = 0;
  private rec(op: string, ...args: unknown[]) {
    this.calls.push({ op, args });
  }
  ops(op: string) {
    return this.calls.filter((c) => c.op === op);
  }
  async presignPut(bucket: string, key: string, contentType: string, expiresIn: number) {
    this.rec("presignPut", bucket, key, contentType, expiresIn);
    return `https://s3.example/${bucket}/${key}?put&ct=${encodeURIComponent(contentType)}&ttl=${expiresIn}`;
  }
  async createMultipartUpload(bucket: string, key: string, contentType: string) {
    this.rec("createMultipartUpload", bucket, key, contentType);
    const uploadId = `up-${++this.seq}`;
    this.uploads.push({ bucket, key, uploadId });
    return uploadId;
  }
  async presignUploadPart(bucket: string, key: string, uploadId: string, partNumber: number, expiresIn: number) {
    this.rec("presignUploadPart", bucket, key, uploadId, partNumber, expiresIn);
    return `https://s3.example/${bucket}/${key}?uploadId=${uploadId}&partNumber=${partNumber}&ttl=${expiresIn}`;
  }
  async listMultipartUploads(bucket: string, prefix: string) {
    this.rec("listMultipartUploads", bucket, prefix);
    return this.uploads.filter((u) => u.bucket === bucket && u.key.startsWith(prefix)).map(({ key, uploadId }) => ({ key, uploadId }));
  }
  async completeMultipartUpload(bucket: string, key: string, uploadId: string, parts: { partNumber: number; etag: string }[]) {
    this.rec("completeMultipartUpload", bucket, key, uploadId, parts);
    this.uploads = this.uploads.filter((u) => u.uploadId !== uploadId);
    this.objects.set(`${bucket}/${key}`, "completed");
  }
  async abortMultipartUpload(bucket: string, key: string, uploadId: string) {
    this.rec("abortMultipartUpload", bucket, key, uploadId);
    this.uploads = this.uploads.filter((u) => u.uploadId !== uploadId);
  }
  /** Not part of `UploadS3` any more (Phase 2 copies no bytes); kept so a test can prove nothing copies. */
  async copyObject(srcBucket: string, srcKey: string, dstBucket: string, dstKey: string) {
    this.rec("copyObject", srcBucket, srcKey, dstBucket, dstKey);
    this.objects.set(`${dstBucket}/${dstKey}`, this.objects.get(`${srcBucket}/${srcKey}`) ?? "copied");
  }
  async deleteObject(bucket: string, key: string) {
    this.rec("deleteObject", bucket, key);
    this.objects.delete(`${bucket}/${key}`);
  }
  async presignGet(bucket: string, key: string, expiresIn: number) {
    this.rec("presignGet", bucket, key, expiresIn);
    return `https://s3.example/${bucket}/${key}?get&ttl=${expiresIn}`;
  }
}

/** A scripted Asset_Service: records each call, answers from `handlers[command]`. */
function fakeAssetService(handlers: Record<string, (call: AssetServiceCall) => AssetServiceReply | Promise<AssetServiceReply>>) {
  const calls: AssetServiceCall[] = [];
  const invoke = async (call: AssetServiceCall): Promise<AssetServiceReply> => {
    calls.push(call);
    const h = handlers[call.command];
    if (!h) return { ok: false, status: 400, error: { code: "unknown_command", message: call.command } };
    return h(call);
  };
  return { invoke, calls };
}

function makeReq(method: string, body?: unknown): any {
  const payload = body === undefined ? [] : [Buffer.from(typeof body === "string" ? body : JSON.stringify(body), "utf8")];
  const req: any = Readable.from(payload);
  req.method = method;
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

interface RunOpts {
  body?: unknown;
  userToken?: string;
  devBypass?: boolean;
}

async function run(method: string, parts: string[], deps: IngestionWriteDeps, opts: RunOpts = {}) {
  const { res, done } = makeRes();
  const handled = await dispatchIngestionWriteRoute(
    makeReq(method, opts.body),
    res,
    { actor: ACTOR, pid: PID, parts, method, userToken: opts.userToken ?? "user-jwt", devBypass: opts.devBypass },
    { now: () => NOW, newUuid: fixedUuid, ingestBucket: BUCKET, env: {}, ...deps },
  );
  if (!handled) return { handled, status: undefined, body: undefined };
  return { handled, ...(await done) };
}

function assetRow(id: string, over: Partial<Item> = {}): Item {
  return {
    ...assetKey(PID, id),
    assetId: id,
    kind: id.split(":")[0],
    source: "upload",
    sourceUri: `s3://${BUCKET}/projects/${PID}/${id}/clip.mp4`,
    sourceSize: 2048,
    contentType: "video/mp4",
    displayName: "clip.mp4",
    status: "queued",
    registeredAt: "2026-10-05T12:00:00.000Z",
    [ASSET_LIST_PK_ATTR]: projectPk(PID),
    [ASSET_LIST_SK_ATTR]: `2026-10-05T12:00:00.000Z#${id}`,
    ...over,
  } as Item;
}

// ------------------------------------------------------------------------------------- access & routing

describe("access and routing", () => {
  it("every route is the uniform 404 on access denial, before any S3 or Asset_Service call", async () => {
    const s3 = new FakeS3();
    const svc = fakeAssetService({});
    const aid = "video:6f1c0a2b-1111-4222-8333-444455556666";
    const routes: [string, string[], unknown][] = [
      ["GET", [PID, "ingestion", "capabilities"], undefined],
      ["POST", [PID, "uploads"], { files: [{ name: "a.mp4", size: 10, contentType: "video/mp4" }] }],
      ["POST", [PID, "uploads", aid, "complete"], { uploadId: "u", parts: [{ partNumber: 1, etag: "e" }] }],
      ["POST", [PID, "uploads", aid, "abort"], { uploadId: "u" }],
      ["POST", [PID, "assets"], { assetId: aid, fileName: "a.mp4", contentType: "video/mp4", size: 10 }],
      ["POST", [PID, "s3-locations"], { uri: `s3://${BUCKET}/projects/${PID}/` }],
      ["POST", [PID, "assets", aid, "remove"], undefined],
      ["POST", [PID, "assets", aid, "chat-copy"], { sessionId: "s1" }],
    ];
    for (const [method, parts, body] of routes) {
      const r = await run(method, parts, { checkAccess: deny, s3, invokeAssetService: svc.invoke, store: new InMemoryTransactStore() }, { body });
      expect(r.status, parts.join("/")).toBe(404);
      expect(r.body).toEqual({ error: { code: "project_not_found", message: expect.any(String) } });
    }
    expect(s3.calls).toEqual([]);
    expect(svc.calls).toEqual([]);
  });

  it("does not claim paths it does not own (read routes, unknown subroutes)", async () => {
    for (const [m, parts] of [
      ["GET", [PID, "assets"]],
      ["POST", [PID, "assets", "urls"]],
      ["GET", [PID, "uploads"]],
      ["POST", [PID, "assets", "video:x", "rebuild"]],
    ] as [string, string[]][]) {
      expect((await run(m, parts, { checkAccess: allow })).handled).toBe(false);
    }
  });

  it("rejects a malformed JSON body with 400 after the access check", async () => {
    const r = await run("POST", [PID, "uploads"], { checkAccess: allow, s3: new FakeS3() }, { body: "{nope" });
    expect(r.status).toBe(400);
    expect(r.body.error.code).toBe("bad_request");
  });
});

// ------------------------------------------------------------------------------------------ capabilities

describe("GET /projects/:pid/ingestion/capabilities", () => {
  it("returns the config-driven contract body", async () => {
    const r = await run("GET", [PID, "ingestion", "capabilities"], {
      checkAccess: allow,
      env: { INGESTION_CONFIG: JSON.stringify({ uploadLimits: { image: 1234 } }) },
    });
    expect(r.status).toBe(200);
    expect(r.body).toEqual({
      maxFilesPerBatch: 20,
      limits: { video: 5368709120, audio: 5368709120, image: 1234, document: 524288000 },
      multipartThresholdBytes: 104857600,
      partSizeBytes: 67108864,
    });
  });
});

// ----------------------------------------------------------------------------------------------- uploads

describe("POST /projects/:pid/uploads", () => {
  it("grants a signed single PUT under 100 MiB and multipart at or above, keyed exactly like register_upload", async () => {
    const s3 = new FakeS3();
    uuidSeq = 0;
    const r = await run("POST", [PID, "uploads"], { checkAccess: allow, s3 }, {
      body: {
        files: [
          { name: "Holiday clip (1).MOV", size: 5 * MIB, contentType: "video/quicktime" },
          { name: "notes.txt", size: 12, contentType: "text/plain" },
          { name: "big.mp4", size: 100 * MIB, contentType: "video/mp4" },
          { name: "huge-archive.zip", size: 130 * MIB, contentType: "" },
        ],
      },
    });
    expect(r.status).toBe(200);
    expect(r.body.errors).toEqual({});
    const [a, b, c, d] = r.body.uploads;

    expect(a).toEqual({
      index: 0,
      assetId: "video:00000000-0000-4000-8000-000000000001",
      key: `projects/${PID}/video:00000000-0000-4000-8000-000000000001/Holiday_clip__1_.MOV`,
      mode: "single",
      url: expect.stringContaining("ct=video%2Fquicktime"),
      headers: { "Content-Type": "video/quicktime" },
    });
    expect(b.assetId).toMatch(/^document:/);
    expect(b.mode).toBe("single");
    expect(b.headers).toEqual({ "Content-Type": "text/plain" });

    // Exactly 100 MiB is multipart (threshold is "at or above").
    expect(c.mode).toBe("multipart");
    expect(c.partSize).toBe(64 * MIB);
    expect(c.parts.map((p: any) => p.partNumber)).toEqual([1, 2]);
    expect(c.uploadId).toBe("up-1");
    // An unknown type with no media extension is a document; 130 MiB → 3 parts; empty type → octet-stream.
    expect(d.assetId).toMatch(/^document:/);
    expect(d.mode).toBe("multipart");
    expect(d.parts).toHaveLength(3);
    expect(s3.ops("createMultipartUpload").map((x) => x.args[2])).toEqual(["video/mp4", "application/octet-stream"]);
    // Every URL is signed for 3600 s against the Ingest bucket only.
    for (const call of [...s3.ops("presignPut"), ...s3.ops("presignUploadPart")]) {
      expect(call.args[0]).toBe(BUCKET);
    }
    expect(s3.ops("presignPut").every((x) => x.args[3] === UPLOAD_URL_TTL_S)).toBe(true);
    expect(s3.ops("presignUploadPart").every((x) => x.args[4] === UPLOAD_URL_TTL_S)).toBe(true);
  });

  it("reports per-file too_large and bad_request without failing the batch", async () => {
    const s3 = new FakeS3();
    const r = await run("POST", [PID, "uploads"], { checkAccess: allow, s3 }, {
      body: {
        files: [
          { name: "photo.png", size: 50 * MIB + 1, contentType: "image/png" }, // over the 50 MiB image limit
          { name: "", size: 10, contentType: "text/plain" },
          { name: "x.bin", size: 0 },
          { name: "y.bin", size: 1.5 },
          { name: "z.bin", size: 10, contentType: 7 },
          "not-an-object",
          { name: "ok.pdf", size: 1000, contentType: "application/pdf" },
          { name: "deck.key", size: 500 * MIB + 1 }, // a document over 500 MiB
        ],
      },
    });
    expect(r.status).toBe(200);
    expect(r.body.errors).toEqual({
      "0": { code: "too_large" },
      "1": { code: "bad_request" },
      "2": { code: "bad_request" },
      "3": { code: "bad_request" },
      "4": { code: "bad_request" },
      "5": { code: "bad_request" },
      "7": { code: "too_large" },
    });
    expect(r.body.uploads.map((u: any) => u.index)).toEqual([6]);
  });

  it("validates the batch size 1..20", async () => {
    const s3 = new FakeS3();
    const none = await run("POST", [PID, "uploads"], { checkAccess: allow, s3 }, { body: { files: [] } });
    expect(none.status).toBe(400);
    const files = Array.from({ length: 21 }, (_, i) => ({ name: `f${i}.txt`, size: 1, contentType: "text/plain" }));
    const many = await run("POST", [PID, "uploads"], { checkAccess: allow, s3 }, { body: { files } });
    expect(many.status).toBe(400);
    expect(many.body.error.code).toBe("too_many_files");
    const twenty = await run("POST", [PID, "uploads"], { checkAccess: allow, s3 }, { body: { files: files.slice(0, 20) } });
    expect(twenty.status).toBe(200);
    expect(twenty.body.uploads).toHaveLength(20);
  });
});

describe("real presigner (static credentials, no network)", () => {
  it("signs Content-Type into a single PUT and bakes no empty-body checksum into PUT/UploadPart URLs", async () => {
    const client = new S3Client({
      region: "us-west-2",
      credentials: { accessKeyId: "AKIDEXAMPLE", secretAccessKey: "secret" },
      requestChecksumCalculation: "WHEN_REQUIRED",
    });
    const ops = s3UploadOps(client);
    const put = new URL(await ops.presignPut(BUCKET, `projects/${PID}/a/x.png`, "image/png", 3600));
    expect(put.searchParams.get("X-Amz-SignedHeaders")).toBe("content-type;host");
    expect(put.searchParams.get("X-Amz-Expires")).toBe("3600");
    expect(put.searchParams.has("x-amz-checksum-crc32")).toBe(false);
    const part = new URL(await ops.presignUploadPart(BUCKET, "k", "up-1", 3, 3600));
    expect(part.searchParams.get("partNumber")).toBe("3");
    expect(part.searchParams.get("uploadId")).toBe("up-1");
    expect(part.searchParams.has("x-amz-checksum-crc32")).toBe(false);
  });
});

// ------------------------------------------------------------------------------------- complete / abort

describe("multipart complete and abort", () => {
  const AID = "video:00000000-0000-4000-8000-0000000000aa";
  const KEY = `projects/${PID}/${AID}/big.mp4`;

  it("completes with parts sorted ascending, bound to this Project+asset by listing S3", async () => {
    const s3 = new FakeS3();
    s3.uploads.push({ bucket: BUCKET, key: KEY, uploadId: "up-9" });
    const r = await run("POST", [PID, "uploads", AID, "complete"], { checkAccess: allow, s3 }, {
      body: { uploadId: "up-9", parts: [{ partNumber: 2, etag: '"b"' }, { partNumber: 1, etag: '"a"' }] },
    });
    expect(r.status).toBe(200);
    expect(r.body).toEqual({ ok: true });
    expect(s3.ops("listMultipartUploads")[0].args).toEqual([BUCKET, `projects/${PID}/${AID}/`]);
    expect(s3.ops("completeMultipartUpload")[0].args).toEqual([
      BUCKET, KEY, "up-9", [{ partNumber: 1, etag: '"a"' }, { partNumber: 2, etag: '"b"' }],
    ]);
  });

  it("refuses an uploadId of another asset (or Project) as upload_not_found", async () => {
    const s3 = new FakeS3();
    s3.uploads.push({ bucket: BUCKET, key: `projects/${PID}/video:other/x.mp4`, uploadId: "up-9" });
    const r = await run("POST", [PID, "uploads", AID, "complete"], { checkAccess: allow, s3 }, {
      body: { uploadId: "up-9", parts: [{ partNumber: 1, etag: "e" }] },
    });
    expect(r.status).toBe(404);
    expect(r.body.error.code).toBe("upload_not_found");
    expect(s3.ops("completeMultipartUpload")).toHaveLength(0);
  });

  it("validates the parts list", async () => {
    const s3 = new FakeS3();
    for (const parts of [[], [{ partNumber: 0, etag: "e" }], [{ partNumber: 1, etag: "" }], [{ partNumber: 1, etag: "a" }, { partNumber: 1, etag: "b" }]]) {
      const r = await run("POST", [PID, "uploads", AID, "complete"], { checkAccess: allow, s3 }, { body: { uploadId: "u", parts } });
      expect(r.status).toBe(400);
    }
  });

  it("maps an S3 completion error to 400 complete_failed", async () => {
    const s3 = new FakeS3();
    s3.uploads.push({ bucket: BUCKET, key: KEY, uploadId: "up-9" });
    s3.completeMultipartUpload = async () => {
      throw Object.assign(new Error("bad part"), { name: "InvalidPart" });
    };
    const r = await run("POST", [PID, "uploads", AID, "complete"], { checkAccess: allow, s3 }, {
      body: { uploadId: "up-9", parts: [{ partNumber: 1, etag: "e" }] },
    });
    expect(r.status).toBe(400);
    expect(r.body.error).toEqual({ code: "complete_failed", message: "InvalidPart" });
  });

  it("aborts this asset's upload and is idempotent for one that is gone", async () => {
    const s3 = new FakeS3();
    s3.uploads.push({ bucket: BUCKET, key: KEY, uploadId: "up-9" });
    const first = await run("POST", [PID, "uploads", AID, "abort"], { checkAccess: allow, s3 }, { body: { uploadId: "up-9" } });
    expect(first.body).toEqual({ ok: true });
    expect(s3.ops("abortMultipartUpload")[0].args).toEqual([BUCKET, KEY, "up-9"]);
    const again = await run("POST", [PID, "uploads", AID, "abort"], { checkAccess: allow, s3 }, { body: { uploadId: "up-9" } });
    expect(again.body).toEqual({ ok: true });
    expect(s3.ops("abortMultipartUpload")).toHaveLength(1);
    const noId = await run("POST", [PID, "uploads", AID, "abort"], { checkAccess: allow, s3 }, { body: {} });
    expect(noId.status).toBe(400);
  });
});

// ------------------------------------------------------------------------------- Asset_Service routes

describe("POST /projects/:pid/assets (register_upload)", () => {
  const AID = "document:00000000-0000-4000-8000-0000000000bb";

  it("sends only the four allowlisted fields with the caller's token and returns the asset view", async () => {
    const svc = fakeAssetService({
      register_upload: (call) => ({
        ok: true,
        value: { asset: { ...assetRow(AID, { kind: "document", displayName: "a.txt" }), PK: undefined, SK: undefined } },
      }),
    });
    const r = await run("POST", [PID, "assets"], { checkAccess: allow, invokeAssetService: svc.invoke }, {
      body: { assetId: AID, fileName: "a.txt", contentType: "text/plain", size: 12, actorId: "evil", key: "x" },
    });
    expect(r.status).toBe(200);
    expect(r.body).toMatchObject({ assetId: AID, kind: "document", displayName: "a.txt", status: "queued", sourceSize: 2048 });
    expect(r.body.sourceUri).toBeUndefined();
    expect(svc.calls).toEqual([
      {
        projectId: PID,
        userToken: "user-jwt",
        command: "register_upload",
        args: { assetId: AID, fileName: "a.txt", contentType: "text/plain", size: 12 },
      },
    ]);
  });

  it("passes 409 object_missing / size_mismatch through", async () => {
    for (const code of ["object_missing", "size_mismatch"]) {
      const svc = fakeAssetService({ register_upload: () => ({ ok: false, status: 409, error: { code, message: "m" } }) });
      const r = await run("POST", [PID, "assets"], { checkAccess: allow, invokeAssetService: svc.invoke }, {
        body: { assetId: AID, fileName: "a.txt", contentType: "text/plain", size: 12 },
      });
      expect(r.status).toBe(409);
      expect(r.body).toEqual({ error: { code, message: "m" } });
    }
  });

  it("under DEV_BYPASS_AUTH sends the dev actor instead of a token", async () => {
    const svc = fakeAssetService({ register_upload: () => ({ ok: true, value: { asset: assetRow(AID) } }) });
    await run("POST", [PID, "assets"], { checkAccess: allow, invokeAssetService: svc.invoke }, {
      body: { assetId: AID, fileName: "a.txt", contentType: "text/plain", size: 12 },
      userToken: "",
      devBypass: true,
    });
    expect(svc.calls[0].userToken).toBeUndefined();
    expect(svc.calls[0].devBypassActor).toBe(ACTOR);
  });

  it("is 503 when the Asset_Service is unreachable, 400 on a malformed body", async () => {
    const svc = fakeAssetService({
      register_upload: () => ({ ok: false, status: 503, error: { code: "asset_service_unavailable", message: "m" } }),
    });
    const down = await run("POST", [PID, "assets"], { checkAccess: allow, invokeAssetService: svc.invoke }, {
      body: { assetId: AID, fileName: "a.txt", contentType: "text/plain", size: 12 },
    });
    expect(down.status).toBe(503);
    const bad = await run("POST", [PID, "assets"], { checkAccess: allow, invokeAssetService: svc.invoke }, {
      body: { assetId: AID, fileName: "a.txt", size: "12" },
    });
    expect(bad.status).toBe(400);
  });

  // ------------------------------------------------------ "this chat only" (asset-lineage D2)
  describe('visibility "session"', () => {
    const base = { assetId: AID, fileName: "a.txt", contentType: "text/plain", size: 12 };

    function withSession(sessions: Record<string, { actorId: string | null; title?: string; createdAt?: unknown }>) {
      const svc = fakeAssetService({ register_upload: () => ({ ok: true, value: { asset: assetRow(AID) } }) });
      const sessionOwner = async (sid: string) => {
        const row = sessions[sid];
        if (row === undefined) return { exists: false };
        if (row.actorId === null) return null;
        return { exists: true, actorId: row.actorId, ...(row.title !== undefined ? { title: row.title } : {}),
                 ...(row.createdAt !== undefined ? { createdAt: row.createdAt } : {}) };
      };
      return { svc, deps: { checkAccess: allow, invokeAssetService: svc.invoke, sessionOwner } as IngestionWriteDeps };
    }

    it("forwards the session visibility with the title and day read from the session ROW", async () => {
      const { svc, deps } = withSession({ "sess-1": { actorId: ACTOR, title: "Trailer cut", createdAt: "2026-10-09T08:00:00.000Z" } });
      const r = await run("POST", [PID, "assets"], deps, {
        body: { ...base, visibility: "session", sessionId: "sess-1", sessionTitle: "spoofed" },
      });
      expect(r.status).toBe(200);
      expect(svc.calls[0]).toMatchObject({
        command: "register_upload",
        sessionId: "sess-1",
        args: {
          ...base,
          visibility: "session",
          sessionId: "sess-1",
          sessionTitle: "Trailer cut",
          sessionCreatedAt: "2026-10-09T08:00:00.000Z",
        },
      });
    });

    it("allows a session with no row yet, carrying no title", async () => {
      const { svc, deps } = withSession({});
      const r = await run("POST", [PID, "assets"], deps, { body: { ...base, visibility: "session", sessionId: "brand-new" } });
      expect(r.status).toBe(200);
      expect(svc.calls[0].args).toEqual({ ...base, visibility: "session", sessionId: "brand-new" });
    });

    it("refuses another actor's session, an unreadable one, a bad id, and any other visibility", async () => {
      const foreign = withSession({ "sess-x": { actorId: "sub-mallory" }, "sess-u": { actorId: null } });
      for (const sessionId of ["sess-x", "sess-u"]) {
        const r = await run("POST", [PID, "assets"], foreign.deps, { body: { ...base, visibility: "session", sessionId } });
        expect(r.status).toBe(404);
        expect(r.body.error.code).toBe("session_not_found");
      }
      const bad = withSession({});
      for (const body of [
        { ...base, visibility: "session", sessionId: "a/b" },
        { ...base, visibility: "session" },
        { ...base, visibility: "project", sessionId: "s" },
        { ...base, sessionId: "s" },
      ]) {
        const r = await run("POST", [PID, "assets"], bad.deps, { body });
        expect(r.status).toBe(400);
      }
      expect(foreign.svc.calls).toHaveLength(0);
      expect(bad.svc.calls).toHaveLength(0);
    });
  });
});

describe("POST /projects/:pid/s3-locations", () => {
  it("maps register_s3_location to {registered:[view], skipped, errors}", async () => {
    const id = "video:00000000-0000-4000-8000-0000000000cc";
    const svc = fakeAssetService({
      register_s3_location: (call) => ({
        ok: true,
        value: {
          registered: [assetRow(id, { source: "s3" })],
          skipped: [{ key: "k1", reason: "already_registered" }],
          errors: [{ key: "k2", code: "not_found" }],
          scanned: 3,
        },
      }),
    });
    const uri = `s3://${BUCKET}/projects/${PID}/incoming/`;
    const r = await run("POST", [PID, "s3-locations"], { checkAccess: allow, invokeAssetService: svc.invoke }, { body: { uri } });
    expect(r.status).toBe(200);
    expect(r.body.registered).toHaveLength(1);
    expect(r.body.registered[0]).toMatchObject({ assetId: id, source: "s3", status: "queued" });
    expect(r.body.registered[0].sourceUri).toBeUndefined();
    expect(r.body.skipped).toEqual([{ key: "k1", reason: "already_registered" }]);
    expect(r.body.errors).toEqual([{ key: "k2", code: "not_found" }]);
    expect(svc.calls[0].args).toEqual({ location: uri });
  });

  it("passes a location refusal through", async () => {
    const svc = fakeAssetService({
      register_s3_location: () => ({ ok: false, status: 400, error: { code: "outside_project_prefix", message: "m" } }),
    });
    const r = await run("POST", [PID, "s3-locations"], { checkAccess: allow, invokeAssetService: svc.invoke }, { body: { uri: "s3://b/x" } });
    expect(r.status).toBe(400);
    expect(r.body.error.code).toBe("outside_project_prefix");
  });
});

describe("POST /projects/:pid/assets/:assetId/remove", () => {
  const AID = "image:00000000-0000-4000-8000-0000000000dd";

  it("answers {ok:true} when the asset is removed and its bytes deleted", async () => {
    const svc = fakeAssetService({ remove: () => ({ ok: true, value: { asset: {}, changed: true, deleted: ["k"], failed: [] } }) });
    const r = await run("POST", [PID, "assets", AID, "remove"], { checkAccess: allow, invokeAssetService: svc.invoke });
    expect(r.status).toBe(200);
    expect(r.body).toEqual({ ok: true });
    expect(svc.calls[0]).toMatchObject({ command: "remove", args: { assetId: AID } });
  });

  it("is 502 cleanup_incomplete when some bytes could not be deleted (a retry is safe)", async () => {
    const svc = fakeAssetService({ remove: () => ({ ok: true, value: { asset: {}, changed: false, deleted: [], failed: ["k"] } }) });
    const r = await run("POST", [PID, "assets", AID, "remove"], { checkAccess: allow, invokeAssetService: svc.invoke });
    expect(r.status).toBe(502);
    expect(r.body.error.code).toBe("cleanup_incomplete");
  });

  it("is the uniform 404 for a malformed or unknown asset", async () => {
    const svc = fakeAssetService({ remove: () => ({ ok: false, status: 404, error: { code: "asset_not_found", message: "m" } }) });
    const unknown = await run("POST", [PID, "assets", AID, "remove"], { checkAccess: allow, invokeAssetService: svc.invoke });
    expect(unknown.status).toBe(404);
    expect(unknown.body.error.code).toBe("project_not_found");
    const malformed = await run("POST", [PID, "assets", "image:nope", "remove"], { checkAccess: allow, invokeAssetService: svc.invoke });
    expect(malformed.status).toBe(404);
    expect(svc.calls).toHaveLength(1);
  });
});

describe("POST /projects/:pid/assets/:assetId/retry", () => {
  const AID = "video:00000000-0000-4000-8000-0000000000ab";

  it("forwards to Asset_Service retry and answers the asset view", async () => {
    const svc = fakeAssetService({ retry: () => ({ ok: true, value: { asset: { assetId: AID, kind: "video",
      displayName: "a.mp4", status: "queued" } } }) });
    const r = await run("POST", [PID, "assets", AID, "retry"], { checkAccess: allow, invokeAssetService: svc.invoke });
    expect(r.status).toBe(200);
    expect(r.body).toMatchObject({ assetId: AID, status: "queued" });
    expect(svc.calls[0]).toMatchObject({ command: "retry", args: { assetId: AID } });
  });

  it("passes a refusal through (409 not_failed) and is the uniform 404 for an unknown asset", async () => {
    const refused = fakeAssetService({ retry: () => ({ ok: false, status: 409, error: { code: "not_failed",
      message: "Only a failed asset can be retried; it is indexed" } }) });
    const r = await run("POST", [PID, "assets", AID, "retry"], { checkAccess: allow, invokeAssetService: refused.invoke });
    expect(r.status).toBe(409);
    expect(r.body.error.code).toBe("not_failed");
    const missing = fakeAssetService({ retry: () => ({ ok: false, status: 404, error: { code: "asset_not_found", message: "m" } }) });
    const m = await run("POST", [PID, "assets", AID, "retry"], { checkAccess: allow, invokeAssetService: missing.invoke });
    expect(m.status).toBe(404);
  });
});

// ------------------------------------------------------------------------------- chat attachment reference

describe("POST /projects/:pid/assets/:assetId/chat-copy", () => {
  const AID = "video:00000000-0000-4000-8000-0000000000ee";
  const SRC_KEY = `projects/${PID}/${AID}/clip.mp4`;

  function setup(over: { sessions?: Record<string, string | null>; row?: Partial<Item>; m2cBucket?: string } = {}) {
    const store = new InMemoryTransactStore();
    store.seed(assetRow(AID, over.row));
    const s3 = new FakeS3();
    s3.objects.set(`${BUCKET}/${SRC_KEY}`, "bytes");
    const svc = fakeAssetService({ record_chat_session: () => ({ ok: true, value: { asset: {} } }) });
    const sessions = over.sessions ?? {};
    const sessionOwner = async (sid: string) =>
      sid in sessions ? (sessions[sid] === null ? null : { exists: true, actorId: sessions[sid] as string }) : { exists: false };
    return {
      store,
      s3,
      svc,
      deps: {
        checkAccess: allow,
        store,
        s3,
        invokeAssetService: svc.invoke,
        sessionOwner,
        ...(over.m2cBucket !== undefined ? { m2cBucket: over.m2cBucket } : {}),
      } as IngestionWriteDeps,
    };
  }

  it("records the session on the asset, answers the reference, and copies NO bytes", async () => {
    const { s3, svc, deps } = setup({ sessions: { "sess-1": ACTOR } });
    const r = await run("POST", [PID, "assets", AID, "chat-copy"], deps, { body: { sessionId: "sess-1" } });
    expect(r.status).toBe(200);
    expect(r.body).toEqual({
      assetId: AID,
      projectId: PID,
      displayName: "clip.mp4",
      kind: "video",
      sizeBytes: 2048,
      url: `https://s3.example/${BUCKET}/${SRC_KEY}?get&ttl=3600`,
      expiresAt: Math.floor(NOW / 1000) + 3600,
      sourceUri: `s3://${BUCKET}/${SRC_KEY}`,
      bucket: BUCKET,
      key: SRC_KEY,
      name: "clip.mp4",
      size: 2048,
      contentType: "video/mp4",
      filename: "clip.mp4",
    });
    expect(svc.calls).toEqual([
      expect.objectContaining({
        command: "record_chat_session",
        args: { assetId: AID, sessionId: "sess-1" },
        sessionId: "sess-1",
      }),
    ]);
    // Nothing is copied any more (asset-lineage Phase 2): the session prefix gains no object at all.
    expect(s3.ops("copyObject")).toHaveLength(0);
    expect([...s3.objects.keys()]).toEqual([`${BUCKET}/${SRC_KEY}`]);
    expect(r.body.key).not.toContain("artifacts/");
  });

  it("allows a new chat (no session row yet)", async () => {
    const { deps } = setup();
    const r = await run("POST", [PID, "assets", AID, "chat-copy"], deps, { body: { sessionId: "brand-new" } });
    expect(r.status).toBe(200);
  });

  it("refuses a session owned by another actor, or one whose owner cannot be read (nothing recorded)", async () => {
    for (const owner of ["sub-mallory", null]) {
      const { s3, svc, deps } = setup({ sessions: { "sess-x": owner } });
      const r = await run("POST", [PID, "assets", AID, "chat-copy"], deps, { body: { sessionId: "sess-x" } });
      expect(r.status).toBe(404);
      expect(r.body.error.code).toBe("session_not_found");
      expect(s3.calls).toEqual([]);
      expect(svc.calls).toHaveLength(0);
    }
  });

  it("is the uniform 404 for a removed or absent asset", async () => {
    const removed = setup({ row: { status: "removed" } });
    const r = await run("POST", [PID, "assets", AID, "chat-copy"], removed.deps, { body: { sessionId: "s" } });
    expect(r.status).toBe(404);
    expect(r.body.error.code).toBe("project_not_found");
    const absent = setup();
    const r2 = await run("POST", [PID, "assets", "video:00000000-0000-4000-8000-000000000999", "chat-copy"], absent.deps, {
      body: { sessionId: "s" },
    });
    expect(r2.status).toBe(404);
  });

  it("validates the session id", async () => {
    const { deps } = setup();
    for (const sessionId of ["", "../x", "a/b", "x".repeat(129), 5]) {
      const r = await run("POST", [PID, "assets", AID, "chat-copy"], deps, { body: { sessionId } });
      expect(r.status).toBe(400);
    }
  });

  it("signs an m2c asset where M2C wrote it, and refuses that bucket for any other source", async () => {
    const m2cKey = "runs/job-1/out.mp4";
    const attachable = setup({
      m2cBucket: "m2c-proxy",
      row: { source: "m2c", sourceUri: `s3://m2c-proxy/${m2cKey}` },
    });
    const ok = await run("POST", [PID, "assets", AID, "chat-copy"], attachable.deps, { body: { sessionId: "s" } });
    expect(ok.status).toBe(200);
    expect(ok.body.sourceUri).toBe(`s3://m2c-proxy/${m2cKey}`);
    const spoofed = setup({ m2cBucket: "m2c-proxy", row: { source: "upload", sourceUri: `s3://m2c-proxy/${m2cKey}` } });
    const no = await run("POST", [PID, "assets", AID, "chat-copy"], spoofed.deps, { body: { sessionId: "s" } });
    expect(no.status).toBe(404);
  });

  it('a "this chat only" asset is attachable in its own chat and in no other (D2)', async () => {
    const own = setup({ row: { visibility: "session", sessionId: "sess-1" } });
    const mine = await run("POST", [PID, "assets", AID, "chat-copy"], own.deps, { body: { sessionId: "sess-1" } });
    expect(mine.status).toBe(200);
    const other = setup({ row: { visibility: "session", sessionId: "sess-1" } });
    const theirs = await run("POST", [PID, "assets", AID, "chat-copy"], other.deps, { body: { sessionId: "sess-2" } });
    expect(theirs.status).toBe(404);
    expect(other.svc.calls).toHaveLength(0);
  });

  it("passes a record refusal through without handing out a URL", async () => {
    const refused = setup();
    refused.deps.invokeAssetService = fakeAssetService({
      record_chat_session: () => ({ ok: false, status: 404, error: { code: "asset_not_found", message: "m" } }),
    }).invoke;
    const r = await run("POST", [PID, "assets", AID, "chat-copy"], refused.deps, { body: { sessionId: "s" } });
    expect(r.status).toBe(404);
    expect(refused.s3.calls).toEqual([]);
  });
});

describe("POST /projects/:pid/ingestion/rebuild", () => {
  it("forwards rebuild_retry and passes its refusal through", async () => {
    const ok = fakeAssetService({ rebuild_retry: () => ({ ok: true, value: { collectionState: "rebuilding", rebuildSeq: 3 } }) });
    const r = await run("POST", [PID, "ingestion", "rebuild"], { checkAccess: allow, invokeAssetService: ok.invoke });
    expect(r.status).toBe(200);
    expect(r.body).toEqual({ collectionState: "rebuilding", rebuildSeq: 3 });
    expect(ok.calls[0]).toMatchObject({ command: "rebuild_retry", args: {} });
    const no = fakeAssetService({ rebuild_retry: () => ({ ok: false, status: 409, error: { code: "not_rebuild_failed",
      message: "Only a failed rebuild can be retried; the collection is ready" } }) });
    const r2 = await run("POST", [PID, "ingestion", "rebuild"], { checkAccess: allow, invokeAssetService: no.invoke });
    expect(r2.status).toBe(409);
    expect(r2.body.error.code).toBe("not_rebuild_failed");
  });
});

describe("POST /projects/:pid/ingestion/indexing", () => {
  it("forwards enable_indexing and passes owner_only / catalog refusals through", async () => {
    const ok = fakeAssetService({ enable_indexing: () => ({ ok: true, value: { indexing: true, changed: true, queued: 4 } }) });
    const r = await run("POST", [PID, "ingestion", "indexing"], { checkAccess: allow, invokeAssetService: ok.invoke });
    expect(r.status).toBe(200);
    expect(r.body).toEqual({ indexing: true, changed: true, queued: 4 });
    expect(ok.calls[0]).toMatchObject({ command: "enable_indexing", args: {} });
    for (const [status, code] of [[403, "owner_only"], [503, "catalog_missing"]] as const) {
      const no = fakeAssetService({ enable_indexing: () => ({ ok: false, status, error: { code, message: "no" } }) });
      const r2 = await run("POST", [PID, "ingestion", "indexing"], { checkAccess: allow, invokeAssetService: no.invoke });
      expect([r2.status, r2.body.error.code]).toEqual([status, code]);
    }
  });
});

describe("POST /projects/:pid/assets/{move,rename-folder,resolve-locations} (organize)", () => {
  const AID = "video:00000000-0000-4000-8000-0000000000aa";

  it("forwards each verb to its Asset_Service command with only the command's fields", async () => {
    const svc = fakeAssetService({
      move_assets: () => ({ ok: true, value: { folder: "Boards/Board 07", moved: [AID], failed: [] } }),
      rename_folder: () => ({ ok: true, value: { from: "A", to: "B", moved: [AID], failed: [], remaining: 0 } }),
      resolve_locations: () => ({ ok: true, value: { locations: { u: AID } } }),
    });
    const deps = { checkAccess: allow, invokeAssetService: svc.invoke };
    const move = await run("POST", [PID, "assets", "move"], deps, {
      body: { assetIds: [AID], folder: "Boards/Board 07", extra: "dropped" } });
    expect(move.status).toBe(200);
    expect(move.body).toEqual({ folder: "Boards/Board 07", moved: [AID], failed: [] });
    expect(svc.calls[0]).toMatchObject({ command: "move_assets", projectId: PID,
      args: { assetIds: [AID], folder: "Boards/Board 07" } });
    expect(svc.calls[0].args).not.toHaveProperty("extra");
    const rename = await run("POST", [PID, "assets", "rename-folder"], deps, { body: { from: "A", to: "B" } });
    expect(rename.body.remaining).toBe(0);
    expect(svc.calls[1]).toMatchObject({ command: "rename_folder", args: { from: "A", to: "B" } });
    const resolve = await run("POST", [PID, "assets", "resolve-locations"], deps, { body: { locations: ["u"] } });
    expect(resolve.body).toEqual({ locations: { u: AID } });
  });

  it("passes the Asset_Service's validation errors through and is the uniform 404 without access", async () => {
    const svc = fakeAssetService({ move_assets: () => ({ ok: false, status: 400,
      error: { code: "invalid_folder", message: "folder: dot_segment" } }) });
    const bad = await run("POST", [PID, "assets", "move"], { checkAccess: allow, invokeAssetService: svc.invoke }, {
      body: { assetIds: [AID], folder: "a/../b" } });
    expect(bad.status).toBe(400);
    expect(bad.body.error.code).toBe("invalid_folder");
    const denied = await run("POST", [PID, "assets", "move"], { checkAccess: deny, invokeAssetService: svc.invoke }, {
      body: { assetIds: [AID] } });
    expect(denied.status).toBe(404);
    expect(svc.calls).toHaveLength(1);
  });

  it("forwards the import inbox verbs with no args", async () => {
    const svc = fakeAssetService({
      import_status: () => ({ ok: true, value: { bucket: "b", prefix: `projects/${PID}/import/`, counts: {}, attention: [] } }),
      rescan_import: () => ({ ok: true, value: { ingested: 1 } }),
    });
    const deps = { checkAccess: allow, invokeAssetService: svc.invoke };
    expect((await run("POST", [PID, "assets", "import-status"], deps, { body: { junk: 1 } })).body.prefix).toBe(`projects/${PID}/import/`);
    expect((await run("POST", [PID, "assets", "rescan-import"], deps)).body).toEqual({ ingested: 1 });
    expect(svc.calls.map((c) => [c.command, c.args])).toEqual([["import_status", {}], ["rescan_import", {}]]);
  });

  it("forwards the lineage read (allowlisted args only) and passes 404 lineage_not_enabled through", async () => {
    const graph = { nodes: [{ id: AID, stage: "final" }], edges: [], gaps: [], stats: { nodes: 1 }, truncated: false };
    const svc = fakeAssetService({ lineage: () => ({ ok: true, value: graph }) });
    const deps = { checkAccess: allow, invokeAssetService: svc.invoke };
    const ok = await run("POST", [PID, "assets", "lineage"], deps, {
      body: { assetId: AID, direction: "ancestors", depth: 3, maxNodes: 50, sessionId: "s1", userId: "spoof" } });
    expect(ok.status).toBe(200);
    expect(ok.body).toEqual({ ...graph, chats: {} });
    expect(svc.calls[0].command).toBe("lineage");
    expect(svc.calls[0].args).toEqual({ assetId: AID, direction: "ancestors", depth: 3, maxNodes: 50, sessionId: "s1" });
    const off = fakeAssetService({ lineage: () => ({ ok: false, status: 404,
      error: { code: "lineage_not_enabled", message: "Lineage is not enabled in this environment" } }) });
    const r = await run("POST", [PID, "assets", "lineage"], { checkAccess: allow, invokeAssetService: off.invoke }, {
      body: { direction: "project" } });
    expect(r.status).toBe(404);
    const denied = await run("POST", [PID, "assets", "lineage"], { checkAccess: deny, invokeAssetService: svc.invoke }, {
      body: { direction: "project" } });
    expect(denied.status).toBe(404);
    expect(svc.calls).toHaveLength(1);
  });

  it("adds chat provenance: title + turn number for the actor's own chats, nothing of another member's", async () => {
    const graph = { nodes: [{ id: AID, sessionId: "s-mine", turnId: "t-2" }, { id: "x", sessionId: "s-theirs" }],
      edges: [{ from: "x", to: AID, sessionId: "s-mine", turnId: "t-2" }] };
    const svc = fakeAssetService({ lineage: () => ({ ok: true, value: graph }) });
    const sessionTurns = vi.fn(async (sid: string) => sid === "s-mine"
      ? { actorId: ACTOR, title: "Watercolor chain", turnIds: ["t-1", "t-2", "t-3"] }
      : { actorId: "someone-else", title: "Their secret chat", turnIds: ["t-9"] });
    const r = await run("POST", [PID, "assets", "lineage"], { checkAccess: allow, invokeAssetService: svc.invoke, sessionTurns }, {
      body: { direction: "project" } });
    expect(r.body.chats).toEqual({
      "s-mine": { mine: true, title: "Watercolor chain", turns: { "t-1": 1, "t-2": 2, "t-3": 3 } },
      "s-theirs": { mine: false },
    });
    expect(JSON.stringify(r.body)).not.toContain("Their secret chat");
    expect(sessionTurns).toHaveBeenCalledTimes(2);
  });

  it("turnIdsOf reads turn ids in order, once each", () => {
    expect(turnIdsOf([{ id: "t-1-u", turnId: "t-1" }, { id: "t-1-a" }, { id: "t-2-u" }, { id: "t-2-x", role: "action" }, {}]))
      .toEqual(["t-1", "t-2"]);
  });

  it("does not claim an unknown verb or a GET", async () => {
    const svc = fakeAssetService({});
    expect((await run("POST", [PID, "assets", "teleport"], { checkAccess: allow, invokeAssetService: svc.invoke })).handled)
      .toBe(false);
    expect((await run("GET", [PID, "assets", "move"], { checkAccess: allow, invokeAssetService: svc.invoke })).handled)
      .toBe(false);
  });
});
