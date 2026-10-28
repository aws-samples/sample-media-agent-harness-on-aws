// POST /media/actions: the chat's project gates the source, the main runtime runs the action under the chat's
// runtime session, the outputs are registered (GeneratedMedia lineage) and the action line is recorded.
import { describe, it, expect, vi } from "vitest";
import { Readable } from "node:stream";
import {
  handleMediaActions, parseMediaActionRequest, resultFrameIn, timelineLineage, type MediaActionsDeps,
} from "./mediaActions";

describe("timelineLineage (lineage edges)", () => {
  const o = { kind: "image" as const, name: "f.jpg", url: "u", key: "k/f.jpg", s3uri: "s3://skills/k/f.jpg" };
  it("carries the source time range and the op's settings", () => {
    expect(timelineLineage({ ...o, range: { start: 12.5, end: 12.5 } }, "s3://ingest/src.mp4", { op: "frame" })).toEqual({
      output: "s3://skills/k/f.jpg", inputs: ["s3://ingest/src.mp4"], tool: "timeline_frame", via: "timeline",
      ranges: [{ input: "s3://ingest/src.mp4", start: 12.5, end: 12.5, unit: "s" }], settings: { op: "frame" },
    });
    const crop = timelineLineage({ ...o, range: { start: 1, end: Number.NaN } }, "s3://ingest/src.mp4",
      { op: "crop", box: { x: 0.1, y: 0, w: 0.5, h: 1 } });
    expect(crop.ranges).toBeUndefined();
    expect(crop.settings).toEqual({ op: "crop", box: [0.1, 0, 0.5, 1] });
  });
});

const PID = "p" + "a".repeat(32);
const SID = "11111111-2222-4333-8444-555555555555";
const ASSET = "video:6865ef88-8411-4629-898f-618ee72d080f";
const LOC = "https://d12.cloudfront.net/projects/" + PID + "/sessions/s/output/x/clip.mp4";

function req(body: unknown, actorId: string | null = "u1") {
  const r: any = Readable.from([Buffer.from(JSON.stringify(body))]);
  r.method = "POST";
  r.actorId = actorId ?? undefined;
  r.userToken = "tok";
  return r;
}

function res() {
  const out: { status?: number; body?: any } = {};
  const r: any = {
    headersSent: false,
    writeHead(s: number) { out.status = s; this.headersSent = true; },
    end(b?: string) { out.body = b ? JSON.parse(b) : undefined; },
  };
  return { r, out };
}

const okFrame = (outputs: unknown[]) =>
  `data: ${JSON.stringify(JSON.stringify({ type: "media_action_result", ok: true, outputs, elapsedMs: 1400 }))}\n\n`;

function deps(over: Partial<MediaActionsDeps> = {}): MediaActionsDeps & { calls: any } {
  const calls: any = { invoke: [], record: [], notify: [] };
  return {
    calls,
    readSession: async (sid, actor) => (sid === SID && actor === "u1" ? { activeProjectId: PID } : null),
    checkAccess: async () => ({ ok: true }) as any,
    resolveLocation: async (_p, _a, _t, loc) => (loc === LOC ? ASSET : null),
    readAssetSource: async (_p, id) => (id === ASSET
      ? { row: { kind: "video", displayName: "trailer.mp4" }, sourceUri: "s3://ingest/projects/x/trailer.mp4", url: "https://signed/x" }
      : null),
    invokeRuntime: async (sid, payload) => {
      calls.invoke.push({ sid, payload });
      return { status: 200, body: okFrame([{ kind: "video", name: "trailer_clip.mp4", key: "k/trailer_clip.mp4", s3uri: "s3://skills/k/trailer_clip.mp4", url: "https://cdn/k/trailer_clip.mp4" }]) };
    },
    recordAction: async (sid, m) => { calls.record.push({ sid, m }); },
    notifyGeneratedMedia: (t) => { calls.notify.push(t); },
    now: () => 1000,
    newId: () => "act-1",
    ...over,
  };
}

const clip = { op: "clip", source: { location: LOC }, ranges: [{ inMs: 23000, outMs: 30000 }], sessionId: SID };

describe("parseMediaActionRequest", () => {
  it("accepts the three ops and refuses malformed bodies", () => {
    expect(parseMediaActionRequest(clip).ok).toBe(true);
    expect(parseMediaActionRequest({ ...clip, op: "frame", points: [1000] }).ok).toBe(true);
    expect(parseMediaActionRequest({ ...clip, op: "crop", box: { x: 0, y: 0, w: 0.5, h: 1 } }).ok).toBe(true);
    expect(parseMediaActionRequest({ ...clip, op: "trim" })).toMatchObject({ ok: false });
    expect(parseMediaActionRequest({ ...clip, ranges: undefined })).toMatchObject({ ok: false, error: "clip needs ranges" });
    expect(parseMediaActionRequest({ ...clip, op: "crop", box: { x: 0, y: 0, w: 2, h: 1 } })).toMatchObject({ ok: false });
    expect(parseMediaActionRequest({ ...clip, source: { location: "javascript:alert(1)" } })).toMatchObject({ ok: false });
    expect(parseMediaActionRequest({ ...clip, source: { assetId: "nope" } })).toMatchObject({ ok: false });
  });
});

describe("resultFrameIn", () => {
  it("finds the frame in SSE (double-encoded) and in plain JSON", () => {
    expect(resultFrameIn(okFrame([]))?.ok).toBe(true);
    expect(resultFrameIn(JSON.stringify({ type: "media_action_result", ok: false, error: "x" }))?.error).toBe("x");
    expect(resultFrameIn("data: {\"type\":\"step\"}\n\n")).toBeNull();
  });
});

describe("POST /media/actions", () => {
  it("resolves the source by its URL, invokes the runtime under the chat session, registers and records", async () => {
    const d = deps();
    const { r, out } = res();
    await handleMediaActions(req(clip), r, d);
    expect(out.status).toBe(200);
    expect(out.body.outputs[0].name).toBe("trailer_clip.mp4");
    // the runtime: the chat's runtime session, a resolved source (never the client URL), no prompt
    expect(d.calls.invoke).toHaveLength(1);
    const { sid, payload } = d.calls.invoke[0];
    expect(sid).toBe(SID);
    expect(payload.__media_action).toMatchObject({
      action_id: "act-1", op: "clip", ranges: [{ inMs: 23000, outMs: 30000 }],
      source: { s3uri: "s3://ingest/projects/x/trailer.mp4", url: "https://signed/x", asset_id: ASSET, name: "trailer.mp4" },
    });
    expect(payload).toMatchObject({ session_id: SID, active_project_id: PID, __actor_id: "u1" });
    expect(payload.prompt).toBeUndefined();
    // registration: lineage output ← source
    expect(d.calls.notify[0]).toMatchObject({ sessionId: SID, turnId: "act-1", projectId: PID, actorSub: "u1" });
    expect(d.calls.notify[0].summary.lineage).toEqual([
      { output: "s3://skills/k/trailer_clip.mp4", inputs: ["s3://ingest/projects/x/trailer.mp4"], tool: "timeline_clip",
        via: "timeline", settings: { op: "clip" } },
    ]);
    // the action line
    expect(d.calls.record[0].m).toMatchObject({ id: "act-1-x", role: "action", turnId: "act-1",
      action: { op: "clip", status: "done", sourceName: "trailer.mp4", sourceAssetId: ASSET } });
  });

  it("refuses without an actor, a session of this actor, an active project or access", async () => {
    let x = res(); await handleMediaActions(req(clip, null), x.r, deps()); expect(x.out.status).toBe(401);
    x = res(); await handleMediaActions(req({ ...clip, sessionId: "other" }), x.r, deps()); expect(x.out.status).toBe(409);
    x = res(); await handleMediaActions(req(clip), x.r, deps({ readSession: async () => ({}) })); expect(x.out.status).toBe(409);
    x = res(); await handleMediaActions(req(clip), x.r, deps({ checkAccess: async () => ({ ok: false }) as any }));
    expect(x.out.status).toBe(404);
  });

  it("refuses a source that is not a video asset of the chat's project, and never invokes the runtime", async () => {
    const d = deps();
    let x = res();
    await handleMediaActions(req({ ...clip, source: { location: "https://elsewhere/x.mp4" } }), x.r, d);
    expect(x.out).toMatchObject({ status: 422, body: { ok: false } });
    x = res();
    await handleMediaActions(req(clip), x.r, deps({
      readAssetSource: async () => ({ row: { kind: "image" }, sourceUri: "s3://b/k.png", url: "https://s/k" }),
    }));
    expect(x.out.status).toBe(422);
    expect(d.calls.invoke).toHaveLength(0);
  });

  it("a failed action is a 502 the UI falls back on, still recorded as a failed line, never registered", async () => {
    const d = deps({
      invokeRuntime: async () => ({ status: 200, body: JSON.stringify({ type: "media_action_result", ok: false, error: "moov atom not found" }) }),
    });
    const { r, out } = res();
    await handleMediaActions(req(clip), r, d);
    expect(out).toMatchObject({ status: 502, body: { ok: false, error: "moov atom not found" } });
    expect(d.calls.notify).toHaveLength(0);
    expect(d.calls.record[0].m.action).toMatchObject({ status: "failed", error: "moov atom not found" });
  });

  it("an unreachable runtime is a failure, not a crash", async () => {
    const { r, out } = res();
    await handleMediaActions(req(clip), r, deps({ invokeRuntime: vi.fn().mockRejectedValue(new Error("ECONNRESET")) }));
    expect(out.status).toBe(502);
    expect(out.body.error).toMatch(/ECONNRESET/);
  });
});

describe("parseMediaActionRequest: aspect", () => {
  it("keeps a preset aspect for the chat's wording, and drops anything else", () => {
    const base = { op: "crop", source: { assetId: "video:11111111-1111-4111-8111-111111111111" }, box: { x: 0.3, y: 0, w: 0.3, h: 1 }, sessionId: "s-1" };
    const ok = parseMediaActionRequest({ ...base, aspect: "9:16" }) as { ok: true; req: { aspect?: string } };
    expect(ok.req.aspect).toBe("9:16");
    const junk = parseMediaActionRequest({ ...base, aspect: "<script>" }) as { ok: true; req: { aspect?: string } };
    expect(junk.req.aspect).toBeUndefined();
  });
});

describe("a batch (the gallery's Crop all / Clip all marked / Extract frame)", () => {
  it("each item runs under its own action id and merges into ONE line at its index; a retry replaces it", async () => {
    let line: any = null;
    const d = deps({
      recordAction: async (_sid, m) => { line = m; },
      readActionLine: async (_sid, id) => (line && line.id === id ? line : null),
      now: (() => { let t = 5000; return () => (t += 10); })(),
    });
    const item = (index: number) => ({ ...clip, op: "crop", box: { x: 0.2, y: 0, w: 0.32, h: 1 }, ranges: undefined,
      batch: { id: "bat-1", index, total: 3, label: `Segment ${index + 1}` } });
    for (const i of [0, 1, 2]) {
      const { r, out } = res();
      await handleMediaActions(req(item(i)), r, d);
      expect(out.status).toBe(200);
    }
    expect(d.calls.invoke.map((c: any) => c.payload.__media_action.action_id)).toEqual(["bat-1-0", "bat-1-1", "bat-1-2"]);
    expect(line).toMatchObject({ id: "bat-1-x", role: "action", turnId: "bat-1",
      action: { op: "crop", status: "done", sourceName: "trailer.mp4", batchTotal: 3 } });
    expect(line.action.items.map((x: any) => x.label)).toEqual(["Segment 1", "Segment 2", "Segment 3"]);
    expect(line.action.outputs).toHaveLength(3);
    const firstAt = line.timestamp;
    // a retry of item 1 replaces it rather than adding a fourth
    const { r, out } = res();
    await handleMediaActions(req(item(1)), r, d);
    expect(out.status).toBe(200);
    expect(line.action.items).toHaveLength(3);
    expect(line.action.outputs).toHaveLength(3);
    expect(line.timestamp).toBe(firstAt); // the line keeps its place in the chat
  });

  it("a failed item is kept on its item; the line is done while anything succeeded", async () => {
    let line: any = null;
    let n = 0;
    const d = deps({
      recordAction: async (_sid, m) => { line = m; },
      readActionLine: async () => line,
      invokeRuntime: async () => (n++ === 1
        ? { status: 200, body: JSON.stringify({ type: "media_action_result", ok: false, error: "ffmpeg failed" }) }
        : { status: 200, body: okFrame([{ kind: "video", name: `o${n}.mp4`, key: "k", s3uri: `s3://b/o${n}.mp4`, url: "https://cdn/o.mp4" }]) }),
    });
    for (const i of [0, 1]) await handleMediaActions(req({ ...clip, batch: { id: "b2", index: i, total: 2 } }), res().r, d);
    expect(line.action.status).toBe("done");
    expect(line.action.items[1]).toMatchObject({ error: "ffmpeg failed" });
    expect(line.action.outputs).toHaveLength(1);
  });

  it("refuses a malformed batch", () => {
    expect(parseMediaActionRequest({ ...clip, batch: { id: "b", index: 3, total: 3 } })).toMatchObject({ ok: false });
    expect(parseMediaActionRequest({ ...clip, batch: { id: "b", index: 0, total: 99 } })).toMatchObject({ ok: false });
    expect(parseMediaActionRequest({ ...clip, batch: { id: "b c", index: 0, total: 2 } })).toMatchObject({ ok: false });
  });
});
