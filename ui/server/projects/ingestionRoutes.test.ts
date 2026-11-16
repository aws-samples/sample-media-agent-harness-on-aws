// Tests for the real proxy ingestion asset routes (media-harness-project-ingestion §3, §18): the
// Asset_URL_Route (`POST …/assets/urls`), the asset-status route (`GET …/assets/:assetId`), and the
// asset-list route (`GET …/assets`). They drive `dispatchAssetRoute` with injected fakes — a stub
// `checkAccess`, a seeded `InMemoryTransactStore` (DynamoDB `BatchGetItem` + GSI semantics), and a
// recording presigner — so no network, S3, or DynamoDB is touched.
//
// Covered: the ≤100-id cap, per-id `project_not_found` for malformed/absent/unsignable ids, presign of
// the source plus an image's rendition, an allowlisted non-Ingest source bucket, whole-route access
// denial, the status route's view (no raw S3 location), and the newest-first list route with cursor
// plumbing.
import { randomUUID } from "node:crypto";
import { Readable } from "node:stream";
import { describe, expect, it } from "vitest";
import { ACCESS_DENIED } from "./access";
import type { AccessResult } from "./access";
import { InMemoryTransactStore } from "./inMemoryStore";
import {
  ASSET_LIST_INDEX,
  ASSET_LIST_PK_ATTR,
  ASSET_LIST_SK_ATTR,
  assetKey,
  projectPk,
} from "./schema";
import type { Item } from "./store";
import {
  ASSET_URL_MAX_IDS,
  ASSET_URL_TTL_S,
  dispatchAssetRoute,
} from "./ingestionRoutes";
import type { AssetRouteDeps } from "./ingestionRoutes";
import { resetMomentFrameCache } from "./momentFrames";

const PID = "p0123456789abcdef0123456789abcdef";
const ACTOR = "sub-alice";
const INGEST_BUCKET = "ingest-bucket";
const OTHER_BUCKET = "partner-bucket";
const NOW_MS = 1_700_000_000_000;
const EXPECTED_EXPIRES_AT = Math.floor(NOW_MS / 1000) + ASSET_URL_TTL_S;

const allow: AssetRouteDeps["checkAccess"] = async () => ({ ok: true } as AccessResult);
const deny: AssetRouteDeps["checkAccess"] = async () => ACCESS_DENIED;

/** A new well-formed asset id of the given kind (`<kind>:<lowercase UUID v4>`). */
function assetId(kind: "video" | "image" | "audio"): string {
  return `${kind}:${randomUUID()}`;
}

/** A seeded ASSET row, including the `assetsByRegistered` GSI pair, keyed under `PID`. */
function assetRow(
  id: string,
  opts: { kind: "video" | "image" | "audio"; sourceUri: string; status?: string; displayName?: string; registeredAt?: string; renditionKey?: string },
): Item {
  const registeredAt = opts.registeredAt ?? "2026-10-05T12:00:00.000Z";
  const row: Item = {
    ...assetKey(PID, id),
    assetId: id,
    kind: opts.kind,
    source: "upload",
    sourceUri: opts.sourceUri,
    contentType: "application/octet-stream",
    sourceSize: 1234,
    displayName: opts.displayName ?? `${opts.kind}.bin`,
    status: opts.status ?? "indexed",
    registeredAt,
    [ASSET_LIST_PK_ATTR]: projectPk(PID),
    [ASSET_LIST_SK_ATTR]: `${registeredAt}#${id}`,
  };
  if (opts.renditionKey) row.rendition = { key: opts.renditionKey, width: 1568, height: 1045, bytes: 412331 };
  return row;
}

/** A recording presigner: returns a deterministic URL and remembers every (bucket, key, ttl). */
function recordingPresign() {
  const calls: { bucket: string; key: string; ttl: number }[] = [];
  const presignGet = async (bucket: string, key: string, ttl: number) => {
    calls.push({ bucket, key, ttl });
    return `https://signed.example/${bucket}/${key}?ttl=${ttl}`;
  };
  return { presignGet, calls };
}

/** A fake request: a readable body (for POST) and a `url` carrying the query string (for the list route). */
function makeReq(method: string, url: string, body?: unknown): any {
  const payload =
    body === undefined ? [] : [Buffer.from(typeof body === "string" ? body : JSON.stringify(body), "utf8")];
  const req: any = Readable.from(payload);
  req.method = method;
  req.url = url;
  return req;
}

interface Captured {
  status?: number;
  body: any;
}

/** A fake response that resolves once `end` is called, with the parsed JSON body. */
function makeRes(): { res: any; done: Promise<Captured> } {
  let resolve!: (c: Captured) => void;
  const done = new Promise<Captured>((r) => (resolve = r));
  const res: any = {
    headersSent: false,
    _status: undefined as number | undefined,
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

/** Dispatch one asset request and return the captured response plus whether the route handled it. */
async function run(
  method: string,
  parts: string[],
  deps: AssetRouteDeps,
  opts: { body?: unknown; query?: string } = {},
): Promise<Captured & { handled: boolean }> {
  const url = `/${parts.join("/")}${opts.query ?? ""}`;
  const req = makeReq(method, url, opts.body);
  const { res, done } = makeRes();
  const handled = await dispatchAssetRoute(req, res, { actor: ACTOR, pid: PID, parts, method }, deps);
  if (!handled) return { handled, body: undefined };
  const captured = await done;
  return { handled, ...captured };
}

function urlsParts(): string[] {
  return [PID, "assets", "urls"];
}

describe("Asset_URL_Route frames — each moment's own picture (IND403: every card showed one hero still)", () => {
  it("signs the frame of the shot each time falls in, from the video's own record only", async () => {
    resetMomentFrameCache();
    const store = new InMemoryTransactStore();
    const video = assetId("video");
    const other = assetId("video");
    const recordKey = `projects/${PID}/${video}/documents/abc.jsonl`;
    const shot = (s: number, e: number, key: string) => JSON.stringify({ doc_type: "shot", start_ms: s, end_ms: e, fields: { frame_s3_key: key } });
    const reads: string[] = [];
    const readObject = async (bucket: string, key: string) => {
      reads.push(`${bucket}/${key}`);
      return [shot(0, 5000, "m2c/run/frames/f_000060.jpg"), JSON.stringify({ doc_type: "scene", start_ms: 0, end_ms: 9000 }),
        shot(5000, 9000, `projects/${PID}/${video}/frames/f_000210.jpg`)].join("\n");
    };
    store.seed(
      { ...assetRow(video, { kind: "video", sourceUri: `s3://${INGEST_BUCKET}/projects/${PID}/import/a.mp4` }), doc: { recordKey } },
      // A row naming ANOTHER asset's record is never read.
      { ...assetRow(other, { kind: "video", sourceUri: `s3://${INGEST_BUCKET}/projects/${PID}/import/b.mp4` }), doc: { recordKey } },
    );
    const { presignGet, calls } = recordingPresign();
    const r = await run("POST", urlsParts(),
      { checkAccess: allow, store, presignGet, readObject, ingestBucket: INGEST_BUCKET, m2cBucket: "m2c-bucket", now: () => NOW_MS } as AssetRouteDeps,
      { body: { asset_ids: [], frames: [{ asset_id: video, at_ms: 1200 }, { asset_id: video, at_ms: 7000 }, { asset_id: other, at_ms: 1200 }] } });
    expect(r.status).toBe(200);
    expect(r.body.frames[`${video}@1200`].url).toContain("m2c-bucket/m2c/run/frames/f_000060.jpg");
    expect(r.body.frames[`${video}@7000`].url).toContain(`${INGEST_BUCKET}/projects/${PID}/${video}/frames/f_000210.jpg`);
    expect(r.body.frames[`${other}@1200`]).toBeUndefined();
    expect(reads).toEqual([`${INGEST_BUCKET}/${recordKey}`]); // read once, cached
    expect(calls).toHaveLength(2);
  });

  it("answers the uniform 404 when access is denied", async () => {
    const r = await run("POST", urlsParts(), { checkAccess: deny, store: new InMemoryTransactStore(), ingestBucket: INGEST_BUCKET, now: () => NOW_MS },
      { body: { asset_ids: [], frames: [{ asset_id: assetId("video"), at_ms: 0 }] } });
    expect(r.status).toBe(404);
  });
});

describe("Asset_URL_Route (POST /projects/:pid/assets/urls)", () => {
  it("rejects more than 100 ids with 400 too_many_ids", async () => {
    const store = new InMemoryTransactStore();
    const asset_ids = Array.from({ length: ASSET_URL_MAX_IDS + 1 }, (_, i) => `video:${i}`);
    const r = await run("POST", urlsParts(), { checkAccess: allow, store, ingestBucket: INGEST_BUCKET, now: () => NOW_MS }, { body: { asset_ids } });
    expect(r.status).toBe(400);
    expect(r.body.error.code).toBe("too_many_ids");
  });

  it("answers the uniform 404 when access is denied, and reads nothing", async () => {
    const store = new InMemoryTransactStore();
    let read = 0;
    const spied = { batchGet: async (...a: any[]) => (read++, (store.batchGet as any)(...a)), queryIndex: store.queryIndex.bind(store) };
    const r = await run("POST", urlsParts(), { checkAccess: deny, store: spied as any, ingestBucket: INGEST_BUCKET, now: () => NOW_MS }, { body: { asset_ids: [assetId("video")] } });
    expect(r.status).toBe(404);
    expect(r.body.error.code).toBe("project_not_found");
    expect(read).toBe(0);
  });

  it("presigns the source and, for an image, the rendition; errors unsignable/absent/malformed ids", async () => {
    const store = new InMemoryTransactStore();
    const image = assetId("image");
    const video = assetId("video");
    const partner = assetId("video");
    const evil = assetId("audio");
    const missing = assetId("video");
    const malformed = "video:not-a-uuid";
    store.seed(
      assetRow(image, {
        kind: "image",
        sourceUri: `s3://${INGEST_BUCKET}/projects/${PID}/${image}/pic.jpg`,
        renditionKey: `projects/${PID}/${image}/rendition.jpg`,
      }),
      assetRow(video, { kind: "video", sourceUri: `s3://${INGEST_BUCKET}/projects/${PID}/${video}/clip.mp4`, status: "removed" }),
      assetRow(partner, { kind: "video", sourceUri: `s3://${OTHER_BUCKET}/incoming/a.mp4` }),
      assetRow(evil, { kind: "audio", sourceUri: `s3://not-allowed-bucket/x/y.wav` }),
    );
    const { presignGet, calls } = recordingPresign();

    const r = await run(
      "POST",
      urlsParts(),
      { checkAccess: allow, store, presignGet, ingestBucket: INGEST_BUCKET, sourceBucketAllowlist: [OTHER_BUCKET], now: () => NOW_MS },
      { body: { asset_ids: [image, video, partner, evil, missing, malformed] } },
    );

    expect(r.status).toBe(200);
    // Image: source + rendition, both at the credential floor.
    expect(r.body.urls[image].source).toEqual({
      url: `https://signed.example/${INGEST_BUCKET}/projects/${PID}/${image}/pic.jpg?ttl=${ASSET_URL_TTL_S}`,
      expiresAt: EXPECTED_EXPIRES_AT,
    });
    expect(r.body.urls[image].rendition).toEqual({
      url: `https://signed.example/${INGEST_BUCKET}/projects/${PID}/${image}/rendition.jpg?ttl=${ASSET_URL_TTL_S}`,
      expiresAt: EXPECTED_EXPIRES_AT,
    });
    // Video (even `removed`): source only, no rendition.
    expect(r.body.urls[video].source.url).toContain(`${INGEST_BUCKET}/projects/${PID}/${video}/clip.mp4`);
    expect(r.body.urls[video].rendition).toBeUndefined();
    // Allowlisted non-Ingest bucket: signed the same way.
    expect(r.body.urls[partner].source.url).toContain(`${OTHER_BUCKET}/incoming/a.mp4`);
    // Unsignable (bucket not allowed), absent, and malformed: per-id project_not_found, never in urls.
    for (const bad of [evil, missing, malformed]) {
      expect(r.body.urls[bad]).toBeUndefined();
      expect(r.body.errors[bad].code).toBe("project_not_found");
    }
    // The rendition is signed against the Ingest bucket; the disallowed/absent/malformed ids are never signed.
    expect(calls.some((c) => c.bucket === "not-allowed-bucket")).toBe(false);
    expect(calls.every((c) => c.ttl === ASSET_URL_TTL_S)).toBe(true);
  });

  it("says where each file is filed (\"\" = the top level), so every view can show its location", async () => {
    const store = new InMemoryTransactStore();
    const filed = assetId("video");
    const loose = assetId("image");
    const filedRow = assetRow(filed, { kind: "video", sourceUri: `s3://${INGEST_BUCKET}/projects/${PID}/${filed}/a.mp4` });
    filedRow.folder = "Footage/Trailers";
    store.seed(filedRow, assetRow(loose, { kind: "image", sourceUri: `s3://${INGEST_BUCKET}/projects/${PID}/${loose}/b.png` }));
    const { presignGet } = recordingPresign();
    const r = await run("POST", urlsParts(), { checkAccess: allow, store, presignGet, ingestBucket: INGEST_BUCKET, now: () => NOW_MS },
      { body: { asset_ids: [filed, loose] } });
    expect(r.status).toBe(200);
    expect(r.body.urls[filed].folder).toBe("Footage/Trailers");
    expect(r.body.urls[loose].folder).toBe("");
  });

  it("signs a video's hero image, and never a rendition key outside the asset's own prefix", async () => {
    const store = new InMemoryTransactStore();
    const video = assetId("video");
    const sneaky = assetId("image");
    store.seed(
      assetRow(video, { kind: "video", sourceUri: `s3://${INGEST_BUCKET}/projects/${PID}/${video}/clip.mp4`,
        renditionKey: `projects/${PID}/${video}/rendition.jpg` }),
      assetRow(sneaky, { kind: "image", sourceUri: `s3://${INGEST_BUCKET}/projects/${PID}/${sneaky}/a.png`,
        renditionKey: `projects/${PID}/other-asset/secret.jpg` }),
    );
    const { presignGet } = recordingPresign();
    const r = await run("POST", urlsParts(),
      { checkAccess: allow, store, presignGet, ingestBucket: INGEST_BUCKET, now: () => NOW_MS },
      { body: { asset_ids: [video, sneaky] } });
    expect(r.body.urls[video].rendition.url).toContain(`projects/${PID}/${video}/rendition.jpg`);
    expect(r.body.urls[sneaky].rendition).toBeUndefined();
  });

  it("signs an m2c asset's source in the M2C proxy bucket, and only an m2c asset's", async () => {
    const M2C = "m2c-proxy-bucket";
    const store = new InMemoryTransactStore();
    const stem = assetId("audio");
    const notM2c = assetId("audio");
    store.seed(
      { ...assetRow(stem, { kind: "audio", sourceUri: `s3://${M2C}/micro-workflow/stems/x/vocals.wav` }), source: "m2c" },
      // A non-m2c row naming the M2C bucket is refused: the bucket is allowlisted for m2c references only.
      assetRow(notM2c, { kind: "audio", sourceUri: `s3://${M2C}/micro-workflow/stems/x/drums.wav` }),
    );
    const { presignGet, calls } = recordingPresign();
    const r = await run("POST", urlsParts(),
      { checkAccess: allow, store, presignGet, ingestBucket: INGEST_BUCKET, m2cBucket: M2C, now: () => NOW_MS },
      { body: { asset_ids: [stem, notM2c] } });
    expect(r.status).toBe(200);
    expect(r.body.urls[stem].source).toEqual({
      url: `https://signed.example/${M2C}/micro-workflow/stems/x/vocals.wav?ttl=${ASSET_URL_TTL_S}`,
      expiresAt: EXPECTED_EXPIRES_AT,
    });
    expect(r.body.urls[notM2c]).toBeUndefined();
    expect(r.body.errors[notM2c].code).toBe("project_not_found");
    expect(calls.map((c) => c.key)).toEqual(["micro-workflow/stems/x/vocals.wav"]);
  });

  it("takes the M2C proxy bucket from MEDIA_PROXY_BUCKET, and signs no m2c source without one", async () => {
    const M2C = "env-m2c-bucket";
    const store = new InMemoryTransactStore();
    const stem = assetId("audio");
    store.seed({ ...assetRow(stem, { kind: "audio", sourceUri: `s3://${M2C}/out/vocals.wav` }), source: "m2c" });
    const prev = process.env.MEDIA_PROXY_BUCKET;
    try {
      process.env.MEDIA_PROXY_BUCKET = M2C;
      const { presignGet } = recordingPresign();
      const deps = { checkAccess: allow, store, presignGet, ingestBucket: INGEST_BUCKET, now: () => NOW_MS };
      const signed = await run("POST", urlsParts(), deps, { body: { asset_ids: [stem] } });
      expect(signed.body.urls[stem].source.url).toContain(`${M2C}/out/vocals.wav`);
      process.env.MEDIA_PROXY_BUCKET = "";
      const refused = await run("POST", urlsParts(), deps, { body: { asset_ids: [stem] } });
      expect(refused.body.errors[stem].code).toBe("project_not_found");
    } finally {
      if (prev === undefined) delete process.env.MEDIA_PROXY_BUCKET;
      else process.env.MEDIA_PROXY_BUCKET = prev;
    }
  });
});

describe("asset status (GET /projects/:pid/assets/:assetId)", () => {
  it("returns the client view of a found asset and never the raw S3 location", async () => {
    const store = new InMemoryTransactStore();
    const id = assetId("image");
    store.seed(
      assetRow(id, {
        kind: "image",
        displayName: "pic.jpg",
        status: "indexed",
        sourceUri: `s3://${INGEST_BUCKET}/projects/${PID}/${id}/pic.jpg`,
        renditionKey: `projects/${PID}/${id}/rendition.jpg`,
      }),
    );
    const r = await run("GET", [PID, "assets", id], { checkAccess: allow, store });
    expect(r.status).toBe(200);
    expect(r.body).toMatchObject({ assetId: id, kind: "image", displayName: "pic.jpg", status: "indexed" });
    expect(r.body.sourceUri).toBeUndefined();
    expect(r.body.PK).toBeUndefined();
    // The rendition dimensions pass through, but not its S3 key.
    expect(r.body.rendition).toEqual({ width: 1568, height: 1045, bytes: 412331 });
  });

  it("carries indexSkippedReason, so a no-speech audio stem shows Not indexed rather than nothing", async () => {
    // Staging p925d3f6 audio:5ea2c1aa (audio_sfx.wav): the row was stored+no_speech, the view dropped the field,
    // and the card drew no badge at all.
    const store = new InMemoryTransactStore();
    const id = assetId("audio");
    store.seed({ ...assetRow(id, {
      kind: "audio", displayName: "audio_sfx.wav", status: "stored",
      sourceUri: `s3://${INGEST_BUCKET}/projects/${PID}/${id}/a.wav`,
    }), indexSkippedReason: "no_speech", indexSkippedStep: "speech-diarization" });
    const r = await run("GET", [PID, "assets", id], { checkAccess: allow, store });
    expect(r.status).toBe(200);
    expect(r.body.indexSkippedReason).toBe("no_speech");
  });

  it("is the uniform 404 for an absent or malformed asset, and on access denial", async () => {
    const store = new InMemoryTransactStore();
    const absent = await run("GET", [PID, "assets", assetId("video")], { checkAccess: allow, store });
    expect(absent.status).toBe(404);
    expect(absent.body.error.code).toBe("project_not_found");

    const malformed = await run("GET", [PID, "assets", "video:bad"], { checkAccess: allow, store });
    expect(malformed.status).toBe(404);

    const id = assetId("video");
    store.seed(assetRow(id, { kind: "video", sourceUri: `s3://${INGEST_BUCKET}/projects/${PID}/${id}/a.mp4` }));
    const denied = await run("GET", [PID, "assets", id], { checkAccess: deny, store });
    expect(denied.status).toBe(404);
  });
});

describe("asset list (GET /projects/:pid/assets)", () => {
  it("returns assets newest-first via the GSI, with client views only", async () => {
    const store = new InMemoryTransactStore();
    const older = assetId("video");
    const newer = assetId("image");
    store.seed(
      assetRow(older, { kind: "video", registeredAt: "2026-10-01T00:00:00.000Z", sourceUri: `s3://${INGEST_BUCKET}/projects/${PID}/${older}/a.mp4` }),
      assetRow(newer, { kind: "image", registeredAt: "2026-10-09T00:00:00.000Z", sourceUri: `s3://${INGEST_BUCKET}/projects/${PID}/${newer}/b.jpg` }),
    );
    const r = await run("GET", [PID, "assets"], { checkAccess: allow, store });
    expect(r.status).toBe(200);
    expect(r.body.assets.map((a: any) => a.assetId)).toEqual([newer, older]); // newest first
    expect(r.body.assets[0].sourceUri).toBeUndefined();
    expect(r.body.cursor).toBeUndefined(); // only two rows, under one page
    expect(r.body.collectionState).toBeUndefined(); // the stub's META carries none
    const withState: AssetRouteDeps["checkAccess"] = async () =>
      ({ ok: true, project: { projectId: PID, collectionState: "rebuilding" } } as unknown as AccessResult);
    const r2 = await run("GET", [PID, "assets"], { checkAccess: withState, store });
    expect(r2.body.collectionState).toBe("rebuilding");
  });

  it("is the uniform 404 on access denial", async () => {
    const store = new InMemoryTransactStore();
    const r = await run("GET", [PID, "assets"], { checkAccess: deny, store });
    expect(r.status).toBe(404);
    expect(r.body.error.code).toBe("project_not_found");
  });

  it("?q= finds by name (case-insensitive substring), ?kind= filters, removed never shows", async () => {
    const store = new InMemoryTransactStore();
    const ids = [assetId("video"), assetId("video"), assetId("image"), assetId("video")];
    const rows: [string, string, string, string][] = [
      [ids[0], "video", "Meridian_Trailer.mp4", "indexed"],
      [ids[1], "video", "tidal_line_trailer.mp4", "stored"],
      [ids[2], "image", "meridian poster.jpg", "stored"],
      [ids[3], "video", "Meridian_cut.mp4", "removed"],
    ];
    rows.forEach(([id, kind, displayName, status], i) => store.seed(assetRow(id, {
      kind, displayName, status, registeredAt: `2026-10-0${i + 1}T00:00:00.000Z`,
      sourceUri: `s3://${INGEST_BUCKET}/projects/${PID}/${id}/x`,
    })));
    const byName = await run("GET", [PID, "assets"], { checkAccess: allow, store }, { query: "?q=MERIDIAN" });
    expect(byName.status).toBe(200);
    expect(byName.body.assets.map((a: any) => a.displayName)).toEqual(["meridian poster.jpg", "Meridian_Trailer.mp4"]);
    const videos = await run("GET", [PID, "assets"], { checkAccess: allow, store }, { query: "?q=trailer&kind=video" });
    expect(videos.body.assets.map((a: any) => a.displayName)).toEqual(["tidal_line_trailer.mp4", "Meridian_Trailer.mp4"]);
    const bad = await run("GET", [PID, "assets"], { checkAccess: allow, store }, { query: "?kind=pdf" });
    expect(bad.status).toBe(400);
    const blank = await run("GET", [PID, "assets"], { checkAccess: allow, store }, { query: "?q=%20" });
    expect(blank.status).toBe(400);
  });

  it("round-trips the continuation cursor and rejects a malformed one", async () => {
    const last = { PK: projectPk(PID), SK: "ASSET#video:x", [ASSET_LIST_PK_ATTR]: projectPk(PID), [ASSET_LIST_SK_ATTR]: "2026#video:x" };
    const seen: any[] = [];
    const fakeStore = {
      batchGet: async () => [],
      queryIndex: async (opts: any) => {
        seen.push(opts);
        // First call (no cursor) returns a page with more to come; a cursored call returns the tail.
        return opts.exclusiveStartKey ? { items: [] } : { items: [], lastEvaluatedKey: last };
      },
    };

    const first = await run("GET", [PID, "assets"], { checkAccess: allow, store: fakeStore as any });
    expect(first.status).toBe(200);
    expect(typeof first.body.cursor).toBe("string");
    // The route asks the GSI newest-first, 50 per page, on the right index/partition.
    expect(seen[0]).toMatchObject({
      indexName: ASSET_LIST_INDEX,
      partitionAttr: ASSET_LIST_PK_ATTR,
      partitionValue: projectPk(PID),
      sortAttr: ASSET_LIST_SK_ATTR,
      ascending: false,
      limit: 50,
    });

    const second = await run("GET", [PID, "assets"], { checkAccess: allow, store: fakeStore as any }, { query: `?cursor=${encodeURIComponent(first.body.cursor)}` });
    expect(second.status).toBe(200);
    expect(seen[1].exclusiveStartKey).toEqual(last); // decoded back to the exact LastEvaluatedKey

    const bad = await run("GET", [PID, "assets"], { checkAccess: allow, store: fakeStore as any }, { query: "?cursor=%00notjson" });
    expect(bad.status).toBe(400);
    expect(bad.body.error.code).toBe("bad_request");
  });
});

describe("asset name search — relevance ranking + pagination", () => {
  /** Seed one matching row with a given name/time; returns its id. */
  function seedNamed(store: InMemoryTransactStore, displayName: string, registeredAt: string): string {
    const id = assetId("video");
    store.seed(assetRow(id, {
      kind: "video", displayName, status: "stored", registeredAt,
      sourceUri: `s3://${INGEST_BUCKET}/projects/${PID}/${id}/x.mp4`,
    }));
    return id;
  }

  it("surfaces an OLDER exact-named asset even when >50 NEWER assets also match", async () => {
    const store = new InMemoryTransactStore();
    // The original upload is OLDER than every derivative.
    const original = seedNamed(store, "Netflix_Open_Content_Meridian.mp4", "2026-01-01T00:00:00.000Z");
    // 60 NEWER crop derivatives that CONTAIN the full name as a substring (so they match the query too).
    for (let i = 0; i < 60; i++) {
      seedNamed(store, `crop_${i}_Netflix_Open_Content_Meridian.mp4`, `2026-02-${String((i % 27) + 1).padStart(2, "0")}T00:00:00.000Z`);
    }
    const r = await run("GET", [PID, "assets"], { checkAccess: allow, store },
      { query: `?q=${encodeURIComponent("Netflix_Open_Content_Meridian.mp4")}` });
    expect(r.status).toBe(200);
    // Exact Display_Name match ranks first despite 60 newer substring matches — the old 50-cap hid it.
    expect(r.body.assets[0].assetId).toBe(original);
    expect(r.body.assets[0].displayName).toBe("Netflix_Open_Content_Meridian.mp4");
    expect(r.body.total).toBe(61);
  });

  it("a name typed without its extension is exact: the original ranks ahead of newer _clip_ derivatives", async () => {
    const store = new InMemoryTransactStore();
    const original = seedNamed(store, "Netflix_Open_Content_Meridian.mp4", "2026-01-01T00:00:00.000Z");
    for (let i = 0; i < 30; i++) {
      seedNamed(store, `Netflix_Open_Content_Meridian_clip_00${i}.mp4`, `2026-02-${String((i % 27) + 1).padStart(2, "0")}T00:00:00.000Z`);
    }
    const r = await run("GET", [PID, "assets"], { checkAccess: allow, store },
      { query: `?q=${encodeURIComponent("Netflix_Open_Content_Meridian")}` });
    expect(r.status).toBe(200);
    expect(r.body.assets[0].assetId).toBe(original);
  });

  it("ranks exact > prefix > substring, overriding recency", async () => {
    const store = new InMemoryTransactStore();
    // Oldest is the exact match; newest is the mere substring. Recency alone would invert the order.
    seedNamed(store, "meridian", "2026-01-01T00:00:00.000Z");                 // exact
    seedNamed(store, "meridian_trailer.mp4", "2026-01-02T00:00:00.000Z");     // prefix
    seedNamed(store, "tidal_meridian.mp4", "2026-01-03T00:00:00.000Z");       // substring
    const r = await run("GET", [PID, "assets"], { checkAccess: allow, store }, { query: "?q=meridian" });
    expect(r.status).toBe(200);
    expect(r.body.assets.map((a: any) => a.displayName)).toEqual([
      "meridian", "meridian_trailer.mp4", "tidal_meridian.mp4",
    ]);
  });

  it("pages past the first 50 via the cursor and reaches ALL matches", async () => {
    const store = new InMemoryTransactStore();
    const want = new Set<string>();
    for (let i = 0; i < 120; i++) {
      want.add(seedNamed(store, `clip_${String(i).padStart(3, "0")}.mp4`, `2026-03-01T00:00:${String(i % 60).padStart(2, "0")}.000Z`));
    }
    const seen = new Set<string>();
    let cursor: string | undefined;
    let pages = 0;
    do {
      const q = `?q=clip${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`;
      const r = await run("GET", [PID, "assets"], { checkAccess: allow, store }, { query: q });
      expect(r.status).toBe(200);
      expect(r.body.total).toBe(120);
      for (const a of r.body.assets) seen.add(a.assetId);
      cursor = r.body.cursor;
      pages += 1;
      expect(pages).toBeLessThanOrEqual(4);
    } while (cursor);
    expect(pages).toBe(3); // 50 + 50 + 20
    expect(seen.size).toBe(120);
    expect([...want].every((id) => seen.has(id))).toBe(true);
  });

  it("refuses a cursor minted for a different query", async () => {
    const store = new InMemoryTransactStore();
    for (let i = 0; i < 60; i++) seedNamed(store, `clip_${i}.mp4`, `2026-03-0${(i % 9) + 1}T00:00:00.000Z`);
    const first = await run("GET", [PID, "assets"], { checkAccess: allow, store }, { query: "?q=clip" });
    expect(typeof first.body.cursor).toBe("string");
    // Same cursor, different q → 400 (the cursor pins its own q+kind).
    const bad = await run("GET", [PID, "assets"], { checkAccess: allow, store },
      { query: `?q=other&cursor=${encodeURIComponent(first.body.cursor)}` });
    expect(bad.status).toBe(400);
    expect(bad.body.error.code).toBe("bad_request");
  });
});

describe("dispatchAssetRoute fall-through", () => {
  it("does not handle an asset subroute outside the P0 slice (e.g. POST …/assets)", async () => {
    const store = new InMemoryTransactStore();
    const r = await run("POST", [PID, "assets"], { checkAccess: allow, store }, { body: {} });
    expect(r.handled).toBe(false);
  });
});

describe("folders — GET …/assets?folder= and GET …/assets/folders", () => {
  function seedFolders(store: InMemoryTransactStore) {
    const rows: [string, string | undefined, string, string?][] = [
      [assetId("video"), "Footage/Day 1", "indexed"],
      [assetId("video"), "Footage/Day 1", "indexed"],
      [assetId("video"), "Footage/Day 1/Drone", "stored"],
      [assetId("image"), undefined, "indexed"],
      [assetId("image"), "Footage/Day 1", "removed"],
      [assetId("image"), "Chats/Promo · 2026-10-01", "stored", "session"],
    ];
    rows.forEach(([id, folder, status, visibility], i) => {
      const row = assetRow(id, { kind: id.startsWith("image") ? "image" : "video", status,
        registeredAt: `2026-10-0${i + 1}T00:00:00.000Z`, sourceUri: `s3://${INGEST_BUCKET}/projects/${PID}/${id}/x` });
      if (folder) row.folder = folder;
      if (visibility) row.visibility = visibility;
      store.seed(row);
    });
    return rows.map(([id]) => id);
  }

  it("?folder= lists the files filed directly in it, newest first; folder= alone is the root", async () => {
    const store = new InMemoryTransactStore();
    const ids = seedFolders(store);
    const day1 = await run("GET", [PID, "assets"], { checkAccess: allow, store }, { query: "?folder=Footage%2FDay%201" });
    expect(day1.status).toBe(200);
    expect(day1.body.assets.map((a: any) => a.assetId)).toEqual([ids[1], ids[0]]);   // not the subfolder, not removed
    const root = await run("GET", [PID, "assets"], { checkAccess: allow, store }, { query: "?folder=" });
    expect(root.body.assets.map((a: any) => a.assetId)).toEqual([ids[3]]);
    const bad = await run("GET", [PID, "assets"], { checkAccess: allow, store }, { query: "?folder=a%2F..%2Fb" });
    expect(bad.status).toBe(400);
  });

  it("a cursor minted for one folder is refused for another", async () => {
    const store = new InMemoryTransactStore();
    for (let i = 0; i < 55; i++) {
      const id = assetId("video");
      const row = assetRow(id, { kind: "video", registeredAt: `2026-10-01T00:00:${String(i).padStart(2, "0")}.000Z`,
        sourceUri: `s3://${INGEST_BUCKET}/projects/${PID}/${id}/x` });
      row.folder = "Big";
      store.seed(row);
    }
    const first = await run("GET", [PID, "assets"], { checkAccess: allow, store }, { query: "?folder=Big" });
    expect(first.body.assets).toHaveLength(50);
    const cursor = encodeURIComponent(first.body.cursor);
    const next = await run("GET", [PID, "assets"], { checkAccess: allow, store }, { query: `?folder=Big&cursor=${cursor}` });
    expect(next.body.assets).toHaveLength(5);
    const wrong = await run("GET", [PID, "assets"], { checkAccess: allow, store }, { query: `?folder=Other&cursor=${cursor}` });
    expect(wrong.status).toBe(400);
  });

  it("assets/folders lists every folder with its direct count; hidden chat uploads and removed files don't count", async () => {
    const store = new InMemoryTransactStore();
    seedFolders(store);
    const r = await run("GET", [PID, "assets", "folders"], { checkAccess: allow, store });
    expect(r.status).toBe(200);
    // `rootCount`: what sits in no folder (the tree's Top level row).
    expect(r.body).toEqual({ folders: [{ folder: "Footage/Day 1", count: 2 }, { folder: "Footage/Day 1/Drone", count: 1 }], rootCount: 1 });
    // A folder made empty ("New folder") is a recorded row: listed with 0 until something is filed in it.
    store.seed({ PK: `PROJECT#${PID}`, SK: "FOLDER#Boards/Board 07", folder: "Boards/Board 07" });
    store.seed({ PK: `PROJECT#${PID}`, SK: "FOLDER#Footage/Day 1", folder: "Footage/Day 1" });
    const withEmpty = await run("GET", [PID, "assets", "folders"], { checkAccess: allow, store });
    expect(withEmpty.body.folders).toEqual([{ folder: "Boards/Board 07", count: 0 }, { folder: "Footage/Day 1", count: 2 },
      { folder: "Footage/Day 1/Drone", count: 1 }]);
    const denied = await run("GET", [PID, "assets", "folders"], { checkAccess: deny, store });
    expect(denied.status).toBe(404);
  });
});

describe("bookmarks — GET …/assets/bookmarks and folder counts", () => {
  it("lists a folder's saved moments (oldest first) and counts them in the folder list", async () => {
    const store = new InMemoryTransactStore();
    const aid = assetId("video");
    store.seed(assetRow(aid, { kind: "video", sourceUri: `s3://${INGEST_BUCKET}/projects/${PID}/${aid}/a.mp4` }));
    const bm = (n: number, folder?: string): Item => ({ PK: projectPk(PID), SK: `BOOKMARK#bookmark:${n}`, bookmarkId: `bookmark:${n}`,
      assetId: aid, startMs: n * 1000, label: `m${n}`, createdAt: `2026-10-16T00:00:0${n}.000Z`, ...(folder ? { folder } : {}) });
    store.seed(bm(2, "Boards/Board 07"), bm(1, "Boards/Board 07"), bm(3));
    const r = await run("GET", [PID, "assets", "bookmarks"], { checkAccess: allow, store }, { query: "?folder=Boards%2FBoard%2007" });
    expect(r.status).toBe(200);
    expect(r.body.bookmarks.map((b: any) => b.bookmarkId)).toEqual(["bookmark:1", "bookmark:2"]);
    expect(r.body.bookmarks[0]).toMatchObject({ assetId: aid, startMs: 1000, label: "m1", folder: "Boards/Board 07" });
    const root = await run("GET", [PID, "assets", "bookmarks"], { checkAccess: allow, store }, { query: "?folder=" });
    expect(root.body.bookmarks.map((b: any) => b.bookmarkId)).toEqual(["bookmark:3"]);
    const folders = await run("GET", [PID, "assets", "folders"], { checkAccess: allow, store });
    expect(folders.body.folders).toEqual([{ folder: "Boards/Board 07", count: 2 }]);
    // A bookmark is never an asset: the list does not return it.
    const list = await run("GET", [PID, "assets"], { checkAccess: allow, store });
    expect(list.body.assets.map((a: any) => a.assetId)).toEqual([aid]);
    expect((await run("GET", [PID, "assets", "bookmarks"], { checkAccess: deny, store })).status).toBe(404);
  });
});

describe("keyframes (GET /projects/:pid/assets/:assetId/keyframes) — what the timeline snaps a dragged range onto", () => {
  // The Asset_Service reads the index (agent projects.keyframes, the ONE reader the clip's copy decision uses too).
  const service = (reply: any) => {
    const calls: any[] = [];
    return { calls, invokeAssetService: async (call: any) => { calls.push(call); return reply; } };
  };
  it("asks the Asset_Service for the asset's keyframes, as the caller", async () => {
    const video = assetId("video");
    const s = service({ ok: true, value: { keyframes: [0, 0.8, 1.6, "x", 2.4] } });
    const req = makeReq("GET", `/${PID}/assets/${video}/keyframes`);
    req.userToken = "tok-1";
    const { res, done } = makeRes();
    await dispatchAssetRoute(req, res, { actor: ACTOR, pid: PID, parts: [PID, "assets", video, "keyframes"], method: "GET" },
      { store: new InMemoryTransactStore(), checkAccess: allow, invokeAssetService: s.invokeAssetService });
    expect(await done).toMatchObject({ status: 200, body: { keyframes: [0, 0.8, 1.6, 2.4] } });
    expect(s.calls).toEqual([{ projectId: PID, command: "keyframes", args: { asset_id: video }, userToken: "tok-1" }]);
  });
  it("no access or a malformed id is the uniform 404 without asking; a 404 from the service too; anything else: no snap", async () => {
    const video = assetId("video");
    const base = { store: new InMemoryTransactStore() };
    const s = service({ ok: true, value: { keyframes: [1] } });
    expect((await run("GET", [PID, "assets", video, "keyframes"], { ...base, checkAccess: deny, invokeAssetService: s.invokeAssetService })).status).toBe(404);
    expect((await run("GET", [PID, "assets", "video:bad", "keyframes"], { ...base, checkAccess: allow, invokeAssetService: s.invokeAssetService })).status).toBe(404);
    expect(s.calls).toHaveLength(0);
    const gone = service({ ok: false, status: 404, error: { code: "not_found", message: "" } });
    expect((await run("GET", [PID, "assets", video, "keyframes"], { ...base, checkAccess: allow, invokeAssetService: gone.invokeAssetService })).status).toBe(404);
    const down = service({ ok: false, status: 503, error: { code: "unavailable", message: "" } });
    expect((await run("GET", [PID, "assets", video, "keyframes"], { ...base, checkAccess: allow, invokeAssetService: down.invokeAssetService })).body).toEqual({ keyframes: null });
  });
});
