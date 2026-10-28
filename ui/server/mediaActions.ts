// POST /media/actions — direct timeline actions (Frame / Clip / Crop) with NO model turn
// (docs/design-2026-10-18-direct-media-actions.md §1, re-assessed).
//
// The Renderer's buttons used to send a chat turn ("Clip the video from 0:23 to 0:30. [source: <url>]") for the
// model to turn back into a tool call. Here the proxy does the deterministic part itself:
//   1. the chat session must be the actor's and have an active project the actor can access;
//   2. the source must be an asset OF THAT PROJECT — by id, or by the URL the tab plays, resolved through the
//      Asset_Service's `resolve_locations` (never a guess from the URL) — and is presigned with the same bucket
//      rules as `assets/urls`;
//   3. the MAIN runtime is invoked with `{__media_action}` under the chat's runtime session id (its warm microVM
//      and warm code-interpreter sandbox), and answers with one `media_action_result` frame;
//   4. the outputs are registered as project assets through the same GeneratedMedia turn message a chat turn
//      sends (lineage: output ← the source), and the action is recorded in the transcript as an `action` line.
//
// Any refusal is a JSON `{ok:false, error}` — the UI then falls back to the old chat path, so nothing is lost.

import type { AccessResult } from "./projects/access";
import { isProjectId } from "./projects/ids";
import type { TurnEnd } from "./projects/generatedMedia";
import type { StoredMessage } from "./transcriptStore";

export const MEDIA_ACTION_OPS = ["frame", "clip", "crop"] as const;
export type MediaActionOp = (typeof MEDIA_ACTION_OPS)[number];
const MAX_BODY_BYTES = 32 * 1024;
const ASSET_ID_RE = /^(video|image|audio|document|data):[0-9a-f-]{36}$/;

export interface MediaActionRequest {
  op: MediaActionOp;
  source: { assetId?: string; location?: string };
  points?: number[];
  ranges?: { inMs: number; outMs: number }[];
  box?: { x: number; y: number; w: number; h: number };
  /** The crop's aspect when it matches a preset ("9:16"), as the timeline measured it: display only. */
  aspect?: string;
  sessionId: string;
  /** Client-chosen id of the chat's action line (upserted by id); generated when absent. */
  actionId?: string;
  /**
   * One item of a multi-clip batch (the gallery's "Crop all" / "Clip all marked" / "Extract frame"): the client
   * sends the items one request at a time (each well under the 60 s edge timeout) and every item lands in ONE
   * action line, `<batch.id>-x`, at `items[index]` (so a retry replaces, never duplicates).
   */
  batch?: { id: string; index: number; total: number; label?: string };
}

/** One item of a batch action line. */
export interface ActionLineItem {
  label?: string;
  sourceName: string;
  sourceAssetId?: string;
  points?: number[];
  ranges?: { inMs: number; outMs: number }[];
  box?: { x: number; y: number; w: number; h: number };
  outputs?: MediaActionOutput[];
  error?: string;
  elapsedMs?: number;
}

export const MAX_BATCH_ITEMS = 50;

export interface MediaActionOutput {
  kind: "image" | "video";
  name: string;
  url: string;
  key: string;
  s3uri: string;
  /** The SOURCE time range (seconds) this output was cut from: a frame's instant, a clip's in/out. */
  range?: { start: number; end: number };
}

/**
 * One timeline output's lineage entry: made from the source, with the SOURCE time range it was cut from and the
 * op's settings (a crop's box). The registrar records range and settings on the lineage edge only where
 * `projects.lineageEdges` is on; elsewhere it reads the entry exactly as before (output, inputs, tool).
 */
export function timelineLineage(
  o: MediaActionOutput, sourceUri: string, r: { op: string; box?: { x: number; y: number; w: number; h: number } },
): Record<string, unknown> {
  const range = o.range && Number.isFinite(o.range.start) && Number.isFinite(o.range.end) ? o.range : null;
  return {
    output: o.s3uri, inputs: [sourceUri], tool: `timeline_${r.op}`, via: "timeline",
    ...(range ? { ranges: [{ input: sourceUri, start: range.start, end: range.end, unit: "s" }] } : {}),
    settings: r.box ? { op: r.op, box: [r.box.x, r.box.y, r.box.w, r.box.h] } : { op: r.op },
  };
}

/** The transcript form of an action line (`role: "action"`), and what the client renders. */
export interface ActionLineRecord {
  op: MediaActionOp;
  status: "done" | "failed";
  sourceName: string;
  sourceAssetId?: string;
  points?: number[];
  ranges?: { inMs: number; outMs: number }[];
  box?: { x: number; y: number; w: number; h: number };
  aspect?: string;
  outputs?: MediaActionOutput[];
  error?: string;
  elapsedMs?: number;
  /** A batch line: one entry per item (in the order sent), and how many the batch has in all. */
  items?: ActionLineItem[];
  batchTotal?: number;
}

export interface MediaActionsDeps {
  readSession(sessionId: string, actorSub: string): Promise<{ activeProjectId?: string } | null>;
  checkAccess(actorSub: string, projectId: string): Promise<AccessResult>;
  /** Asset id at `location` in `projectId` (Asset_Service resolve_locations), or null. */
  resolveLocation(projectId: string, actorSub: string, userToken: string | undefined, location: string): Promise<string | null>;
  readAssetSource(projectId: string, assetId: string): Promise<{ row: Record<string, unknown>; sourceUri: string; url: string } | null>;
  /** POST the payload to the main runtime under `runtimeSessionId`; the raw response body. */
  invokeRuntime(runtimeSessionId: string, payload: Record<string, unknown>): Promise<{ status: number; body: string }>;
  recordAction?(sessionId: string, message: StoredMessage): Promise<void>;
  /** The chat's current action line `<id>-x`, if any (a batch merges into it). */
  readActionLine?(sessionId: string, messageId: string): Promise<StoredMessage | null>;
  notifyGeneratedMedia?(t: TurnEnd): void;
  now?(): number;
  newId?(): string;
}

type Parsed = { ok: true; req: MediaActionRequest } | { ok: false; error: string };

const isMs = (v: unknown) => typeof v === "number" && Number.isFinite(v) && v >= 0;
const isFrac = (v: unknown) => typeof v === "number" && Number.isFinite(v) && v >= 0 && v <= 1;

/** Validate the body shape (the runtime re-validates the semantics: caps, box sizes). */
export function parseMediaActionRequest(raw: unknown): Parsed {
  const b = raw as Partial<MediaActionRequest> | null;
  if (!b || typeof b !== "object") return { ok: false, error: "body must be a JSON object" };
  if (!MEDIA_ACTION_OPS.includes(b.op as MediaActionOp)) return { ok: false, error: "op must be frame, clip or crop" };
  if (typeof b.sessionId !== "string" || !b.sessionId) return { ok: false, error: "sessionId is required" };
  const src = b.source ?? {};
  const assetId = typeof src.assetId === "string" ? src.assetId : undefined;
  const location = typeof src.location === "string" ? src.location : undefined;
  if (!assetId && !location) return { ok: false, error: "source.assetId or source.location is required" };
  if (assetId && !ASSET_ID_RE.test(assetId)) return { ok: false, error: "source.assetId is not an asset id" };
  if (location && (location.length > 4096 || !/^(https?:\/\/|s3:\/\/)/.test(location))) {
    return { ok: false, error: "source.location must be an http(s) or s3 URL" };
  }
  const req: MediaActionRequest = { op: b.op as MediaActionOp, source: { assetId, location }, sessionId: b.sessionId };
  if (typeof b.actionId === "string" && /^[A-Za-z0-9._-]{1,80}$/.test(b.actionId)) req.actionId = b.actionId;
  if (b.points !== undefined) {
    if (!Array.isArray(b.points) || !b.points.every(isMs)) return { ok: false, error: "points must be milliseconds" };
    req.points = b.points.map((p) => Math.round(p));
  }
  if (b.ranges !== undefined) {
    if (!Array.isArray(b.ranges) || !b.ranges.every((r) => r && isMs(r.inMs) && isMs(r.outMs))) {
      return { ok: false, error: "ranges must be [{inMs, outMs}]" };
    }
    req.ranges = b.ranges.map((r) => ({ inMs: Math.round(r.inMs), outMs: Math.round(r.outMs) }));
  }
  if (b.box !== undefined) {
    const x = b.box as Record<string, unknown>;
    if (!x || !["x", "y", "w", "h"].every((k) => isFrac(x[k]))) return { ok: false, error: "box must be {x,y,w,h} fractions" };
    req.box = { x: x.x as number, y: x.y as number, w: x.w as number, h: x.h as number };
  }
  if (typeof b.aspect === "string" && /^\d{1,2}:\d{1,2}$/.test(b.aspect)) req.aspect = b.aspect;
  if (b.batch !== undefined) {
    const x = b.batch as Record<string, unknown>;
    const total = x?.total, index = x?.index;
    if (!x || typeof x.id !== "string" || !/^[A-Za-z0-9._-]{1,80}$/.test(x.id)
        || !Number.isInteger(total) || (total as number) < 1 || (total as number) > MAX_BATCH_ITEMS
        || !Number.isInteger(index) || (index as number) < 0 || (index as number) >= (total as number)) {
      return { ok: false, error: `batch must be {id, index, total} with total <= ${MAX_BATCH_ITEMS}` };
    }
    req.batch = { id: x.id, index: index as number, total: total as number,
      ...(typeof x.label === "string" ? { label: x.label.slice(0, 200) } : {}) };
  }
  if (req.op === "frame" && !req.points?.length) return { ok: false, error: "frame needs points" };
  if (req.op === "clip" && !req.ranges?.length) return { ok: false, error: "clip needs ranges" };
  if (req.op === "crop" && !req.box) return { ok: false, error: "crop needs a box" };
  return { ok: true, req };
}

/**
 * A batch line with `item` at `r.batch.index`: the items so far, every output in item order, the summed time, and
 * `failed` only when no item has produced anything (a partly failed batch is `done`, its failures on their items).
 */
export function mergeBatchItem(prior: ActionLineRecord | undefined, r: MediaActionRequest, item: ActionLineItem): ActionLineRecord {
  const total = r.batch!.total;
  const items: (ActionLineItem | undefined)[] = Array.from({ length: total }, (_, i) =>
    prior?.op === r.op && Array.isArray(prior.items) ? prior.items[i] ?? undefined : undefined);
  items[r.batch!.index] = item;
  const done = items.filter((x): x is ActionLineItem => !!x);
  const outputs = done.flatMap((x) => x.outputs ?? []);
  const names = new Set(done.map((x) => x.sourceName));
  return {
    op: r.op,
    status: outputs.length ? "done" : "failed",
    sourceName: names.size === 1 ? [...names][0] : `${total} videos`,
    batchTotal: total,
    items: items.map((x) => x ?? { sourceName: "" }),
    outputs,
    elapsedMs: done.reduce((n, x) => n + (x.elapsedMs ?? 0), 0),
    ...(outputs.length ? {} : { error: done.find((x) => x.error)?.error ?? "the action failed" }),
  };
}

/** The `media_action_result` frame out of the runtime's SSE (or plain JSON) response body. */
export function resultFrameIn(body: string): Record<string, unknown> | null {
  const candidates: string[] = [];
  for (const line of body.split(/\r?\n/)) {
    const t = line.trim();
    if (!t) continue;
    candidates.push(t.startsWith("data:") ? t.slice(5).trim() : t);
  }
  for (const c of candidates) {
    let v: unknown;
    try { v = JSON.parse(c); } catch { continue; }
    if (typeof v === "string") { try { v = JSON.parse(v); } catch { continue; } }
    if (v && typeof v === "object" && (v as { type?: unknown }).type === "media_action_result") return v as Record<string, unknown>;
  }
  return null;
}

/** The action line's transcript message (`role: "action"`, id `<actionId>-x`, upserted by turnId = actionId). */
export function actionMessage(actionId: string, startedAt: number, rec: ActionLineRecord): StoredMessage {
  return { id: `${actionId}-x`, role: "action", content: "", timestamp: startedAt, turnId: actionId, action: rec } as StoredMessage;
}

function send(res: any, status: number, body: unknown) {
  res.writeHead(status, { "content-type": "application/json", "access-control-allow-origin": "*", "cache-control": "no-store" });
  res.end(JSON.stringify(body));
}

async function readBody(req: any): Promise<string | null> {
  if (typeof req.body === "string") return req.body;
  if (req.body && typeof req.body === "object" && !Buffer.isBuffer(req.body)) return JSON.stringify(req.body);
  return new Promise((resolve) => {
    const chunks: Buffer[] = [];
    let size = 0;
    req.on("data", (c: Buffer) => {
      size += c.length;
      if (size > MAX_BODY_BYTES) { resolve(null); return; }
      chunks.push(c);
    });
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", () => resolve(null));
  });
}

/** Handle `POST /media/actions` (the auth middleware has set `req.actorId` / `req.userToken`). */
export async function handleMediaActions(req: any, res: any, deps: MediaActionsDeps): Promise<void> {
  if (req.method !== "POST") { send(res, 405, { ok: false, error: "POST only" }); return; }
  const actor: string | undefined = req.actorId;
  if (!actor) { send(res, 401, { ok: false, error: "missing_token" }); return; }
  const text = await readBody(req);
  if (text === null) { send(res, 413, { ok: false, error: "body too large" }); return; }
  let raw: unknown;
  try { raw = JSON.parse(text || "{}"); } catch { send(res, 400, { ok: false, error: "body must be JSON" }); return; }
  const parsed = parseMediaActionRequest(raw);
  if (!parsed.ok) { send(res, 400, { ok: false, error: parsed.error }); return; }
  const r = parsed.req;
  const now = deps.now ?? (() => Date.now());
  const startedAt = now();
  const actionId = r.batch ? `${r.batch.id}-${r.batch.index}`
    : r.actionId ?? deps.newId?.() ?? `a-${startedAt}-${Math.random().toString(36).slice(2, 8)}`;

  // 1. The chat session is the actor's and has a project the actor can still access.
  const session = await deps.readSession(r.sessionId, actor).catch(() => null);
  const pid = session?.activeProjectId;
  if (!session || !isProjectId(pid)) { send(res, 409, { ok: false, error: "the chat has no active project" }); return; }
  const access = await deps.checkAccess(actor, pid).catch(() => null);
  if (!access || !access.ok) { send(res, 404, { ok: false, error: "project not found" }); return; }

  // 2. The source is an asset of THAT project, with its real location (never inferred from the URL).
  let assetId = r.source.assetId ?? null;
  if (!assetId && r.source.location) assetId = await deps.resolveLocation(pid, actor, req.userToken, r.source.location).catch(() => null);
  const source = assetId ? await deps.readAssetSource(pid, assetId).catch(() => null) : null;
  if (!assetId || !source) { send(res, 422, { ok: false, error: "the source is not a file of this chat's project" }); return; }
  const kind = String(source.row.kind ?? "");
  if (kind !== "video") { send(res, 422, { ok: false, error: `${r.op} needs a video (this is ${kind || "unknown"})` }); return; }
  const sourceName = typeof source.row.displayName === "string" ? source.row.displayName : source.sourceUri.split("/").pop() ?? "video";

  // 3. The main runtime, under the chat's runtime session (its warm microVM and sandbox). No model turn.
  const payload: Record<string, unknown> = {
    __media_action: {
      action_id: actionId, op: r.op,
      source: { url: source.url, s3uri: source.sourceUri, name: sourceName, asset_id: assetId },
      ...(r.points ? { points: r.points } : {}),
      ...(r.ranges ? { ranges: r.ranges } : {}),
      ...(r.box ? { box: r.box } : {}),
    },
    session_id: r.sessionId,
    active_project_id: pid,
    __actor_id: actor,
  };
  let frame: Record<string, unknown> | null = null;
  let error = "";
  try {
    const out = await deps.invokeRuntime(r.sessionId, payload);
    frame = out.status >= 200 && out.status < 300 ? resultFrameIn(out.body) : null;
    if (!frame) error = `the runtime answered ${out.status} without a result`;
  } catch (e: any) {
    error = `the runtime could not be reached: ${e?.message ?? e}`;
  }
  const endedAt = now();
  const ok = !!frame && frame.ok === true;
  const outputs = ok && Array.isArray(frame!.outputs) ? (frame!.outputs as MediaActionOutput[]) : [];
  if (!ok && !error) error = typeof frame?.error === "string" ? frame.error : "the action failed";
  const elapsedMs = typeof frame?.elapsedMs === "number" ? frame.elapsedMs : endedAt - startedAt;

  // 4. Register the outputs (same GeneratedMedia turn message a chat turn sends) and record the action line.
  if (ok && outputs.length && deps.notifyGeneratedMedia) {
    deps.notifyGeneratedMedia({
      sessionId: r.sessionId, turnId: actionId, projectId: pid, actorSub: actor, startedAt, endedAt, text: "", steps: [],
      summary: { lineage: outputs.map((o) => timelineLineage(o, source.sourceUri, r)) },
    });
  }
  const record: ActionLineRecord = {
    op: r.op, status: ok ? "done" : "failed", sourceName, sourceAssetId: assetId,
    ...(r.points ? { points: r.points } : {}), ...(r.ranges ? { ranges: r.ranges } : {}), ...(r.box ? { box: r.box } : {}),
    ...(r.aspect ? { aspect: r.aspect } : {}), ...(ok ? { outputs } : { error }), elapsedMs,
  };
  if (r.batch) {
    // Merge this item into the batch's one line (read it, set items[index], write it back). The client sends the
    // items one after another, so there is no concurrent writer to race.
    const lineId = `${r.batch.id}-x`;
    const prior = deps.readActionLine ? await deps.readActionLine(r.sessionId, lineId).catch(() => null) : null;
    const merged = mergeBatchItem(prior?.action as ActionLineRecord | undefined, r, {
      ...(r.batch.label ? { label: r.batch.label } : {}), sourceName, sourceAssetId: assetId,
      ...(r.points ? { points: r.points } : {}), ...(r.ranges ? { ranges: r.ranges } : {}), ...(r.box ? { box: r.box } : {}),
      ...(ok ? { outputs } : { error }), elapsedMs,
    });
    const at = typeof prior?.timestamp === "number" ? prior.timestamp : startedAt;
    if (deps.recordAction) {
      await deps.recordAction(r.sessionId, actionMessage(r.batch.id, at, merged))
        .catch((e: any) => console.error("[media-actions] transcript write failed:", e?.message ?? e));
    }
    if (!ok) { send(res, 502, { ok: false, error, actionId, record: merged }); return; }
    send(res, 200, { ok: true, actionId, outputs, elapsedMs, record: merged });
    return;
  }
  if (deps.recordAction) {
    await deps.recordAction(r.sessionId, actionMessage(actionId, startedAt, record))
      .catch((e: any) => console.error("[media-actions] transcript write failed:", e?.message ?? e));
  }
  if (!ok) { send(res, 502, { ok: false, error, actionId, record }); return; }
  send(res, 200, { ok: true, actionId, outputs, elapsedMs, record });
}
