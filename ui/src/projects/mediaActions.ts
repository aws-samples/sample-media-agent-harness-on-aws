/**
 * Direct timeline actions (Frame / Clip / Crop) — the client half of `POST /media/actions`
 * (docs/design-2026-10-18-direct-media-actions.md §1; mock .playwright-mcp/mock/action-trail.html §1).
 *
 * A timeline button no longer sends a chat turn when its source is a file of the chat's project: the proxy
 * runs the action in the chat's warm sandbox with no model turn, and the chat gets an ACTION LINE — a quiet
 * event row (not a message) with live status and the result. Action lines are saved with the chat
 * (`role: "action"`, upserted by `<actionId>-x`) and never start a turn; the next turn the user sends carries
 * a hidden summary of them (`actionContextBlock`), so "now make that clip vertical" still works.
 */
import { authFetch } from '../auth/authFetch';
import type { ChatMessage } from '../lab-api';

export type MediaActionOp = 'frame' | 'clip' | 'crop';

export interface MediaActionOutput { kind: 'image' | 'video' | 'audio'; name: string; url: string; key: string; s3uri: string }

/** What an action line shows; `running` exists on screen only (the proxy records `done` / `failed`). */
export interface ActionLineRecord {
  op: MediaActionOp;
  status: 'running' | 'done' | 'failed';
  sourceName: string;
  sourceAssetId?: string;
  points?: number[];
  ranges?: { inMs: number; outMs: number }[];
  box?: { x: number; y: number; w: number; h: number };
  /** The crop's aspect when it matched a preset ("9:16"), measured by the timeline from the video's pixels. */
  aspect?: string;
  outputs?: MediaActionOutput[];
  error?: string;
  elapsedMs?: number;
  /** The action was then handed to the agent as a chat turn (the direct path could not run it). */
  handedToAgent?: boolean;
  /** A batch (the gallery's Crop all / Clip all marked / Extract frame): one entry per item, and the batch size. */
  items?: ActionLineItem[];
  batchTotal?: number;
}

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

export interface MediaActionBody {
  op: MediaActionOp;
  source: { assetId?: string; location?: string };
  points?: number[];
  ranges?: { inMs: number; outMs: number }[];
  box?: { x: number; y: number; w: number; h: number };
  aspect?: string;
  sessionId: string;
  actionId: string;
  batch?: { id: string; index: number; total: number; label?: string };
}

/** A batch, sent one item per request (each well under the 60 s edge timeout), recorded as one line. */
export interface MediaActionBatch {
  op: MediaActionOp;
  sessionId: string;
  actionId: string;
  items: (Omit<MediaActionBody, 'sessionId' | 'actionId' | 'op' | 'batch'> & { label?: string })[];
}

export interface TimelineActionLike {
  verb?: string;
  src?: string;
  ranges?: { in: number; out: number }[];
  points?: number[];
  box?: { x: number; y: number; w: number; h: number };
  aspect?: string;
  clips?: unknown[];
}

interface BatchEntry { label?: unknown; src?: unknown; in?: unknown; out?: unknown; at?: unknown; box?: unknown }

const isBox = (b: unknown): b is { x: number; y: number; w: number; h: number } =>
  !!b && typeof b === 'object' && ['x', 'y', 'w', 'h'].every((k) => Number.isFinite((b as Record<string, number>)[k]));

/**
 * The direct batch for a gallery action over several clips (Clip all marked / Extract frame / Crop all), or `null`
 * when it must go to the agent: another verb (an audio Trim), or any entry without a usable source and value.
 */
export function directBatchRequest(data: TimelineActionLike | undefined, sessionId: string, actionId: string): MediaActionBatch | null {
  const clips = Array.isArray(data?.clips) ? (data!.clips as BatchEntry[]) : [];
  if (!sessionId || clips.length === 0 || clips.length > MAX_BATCH_ITEMS) return null;
  // A trim is a clip of an audio file: the same direct action (the proxy and the sandbox take audio for a clip).
  const op: MediaActionOp | null = data!.verb === 'clip' || data!.verb === 'trim' ? 'clip' : data!.verb === 'extract' ? 'frame' : data!.verb === 'crop' ? 'crop' : null;
  if (!op) return null;
  const items: MediaActionBatch['items'] = [];
  for (const c of clips) {
    if (typeof c.src !== 'string' || !/^https?:\/\//.test(c.src)) return null;
    const label = typeof c.label === 'string' ? c.label : undefined;
    const base = { source: { location: c.src }, ...(label ? { label } : {}) };
    if (op === 'clip') {
      const a = Number(c.in), b = Number(c.out);
      if (!Number.isFinite(a) || !Number.isFinite(b) || b <= a) return null;
      items.push({ ...base, ranges: [{ inMs: ms(a), outMs: ms(b) }] });
    } else if (op === 'frame') {
      const at = Number(c.at ?? c.in);
      if (!Number.isFinite(at) || at < 0) return null;
      items.push({ ...base, points: [ms(at)] });
    } else {
      if (!isBox(c.box)) return null;
      items.push({ ...base, box: c.box });
    }
  }
  return { op, sessionId, actionId, items };
}

export const MAX_BATCH_ITEMS = 50;

const ms = (s: number) => Math.max(0, Math.round(s * 1000));

/**
 * The direct request for a timeline action, or `null` when it must go to the agent as before: another verb
 * (Analyze), a batch over several clips, no source, or a whole-video clip (no range marked).
 */
export function directMediaRequest(data: TimelineActionLike | undefined, sessionId: string, actionId: string): MediaActionBody | null {
  if (!data || !data.src || !/^https?:\/\//.test(data.src) || !sessionId) return null;
  if (Array.isArray(data.clips) && data.clips.length > 0) return null;
  const base = { source: { location: data.src }, sessionId, actionId };
  if (data.verb === 'frame') {
    const pts = (data.points ?? []).filter((p) => Number.isFinite(p) && p >= 0);
    return pts.length ? { ...base, op: 'frame', points: [...new Set(pts.map(ms))] } : null;
  }
  if (data.verb === 'clip') {
    const ranges = (data.ranges ?? []).filter((r) => Number.isFinite(r.in) && Number.isFinite(r.out) && r.out > r.in)
      .map((r) => ({ inMs: ms(r.in), outMs: ms(r.out) }));
    return ranges.length ? { ...base, op: 'clip', ranges } : null;
  }
  if (data.verb === 'crop' && data.box) {
    const r = (data.ranges ?? []).find((x) => Number.isFinite(x.in) && Number.isFinite(x.out) && x.out > x.in);
    return { ...base, op: 'crop', box: data.box, ...(data.aspect ? { aspect: data.aspect } : {}),
      ...(r ? { ranges: [{ inMs: ms(r.in), outMs: ms(r.out) }] } : {}) };
  }
  return null;
}

export type MediaActionReply =
  | { ok: true; outputs: MediaActionOutput[]; elapsedMs: number; record?: ActionLineRecord }
  /** `ran`: the action ran and failed (recorded as a failed line); otherwise it was refused before running.
   *  `unknown`: no answer came back (the connection dropped, an edge timeout's HTML page) — it may well have run,
   *  and finished. Never handed to the agent: that cut every clip a second time. */
  | { ok: false; ran: boolean; error: string; record?: ActionLineRecord; unknown?: boolean };

export async function runMediaAction(body: MediaActionBody): Promise<MediaActionReply> {
  try {
    const res = await authFetch('/media/actions', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    const json = await res.json().catch(() => null) as Record<string, unknown> | null;
    // Not the route's own JSON (CloudFront's 504 page, a proxy restart): whether it ran is unknown.
    if (!json) return { ok: false, ran: false, unknown: true, error: `no answer (HTTP ${res.status}) — it may still have finished` };
    if (res.ok && json.ok === true) {
      return { ok: true, outputs: (json.outputs as MediaActionOutput[]) ?? [], elapsedMs: Number(json.elapsedMs) || 0,
        record: json.record as ActionLineRecord | undefined };
    }
    // A long action's reply streams (200 + keep-alive), so the body — not the status — says whether it ran.
    return { ok: false, ran: typeof json.ran === 'boolean' ? json.ran : res.status === 502, error: String(json.error ?? `HTTP ${res.status}`),
      record: json.record as ActionLineRecord | undefined };
  } catch (e) {
    return { ok: false, ran: false, unknown: true, error: `${e instanceof Error ? e.message : String(e)} — it may still have finished` };
  }
}

export const actionMessageId = (actionId: string) => `${actionId}-x`;

export function actionLineMessage(actionId: string, startedAt: number, action: ActionLineRecord): ChatMessage {
  return { id: actionMessageId(actionId), role: 'action', content: '', timestamp: startedAt, action };
}

/** Upsert an action line into the chat of `sessionId` (ChatCanvas listens). `remove` drops it. */
export const CHAT_ACTION_EVENT = 'chat:action';
export interface ChatActionDetail { sessionId: string; message: ChatMessage; remove?: boolean }
export function publishActionLine(detail: ChatActionDetail): void {
  window.dispatchEvent(new CustomEvent<ChatActionDetail>(CHAT_ACTION_EVENT, { detail }));
}

// ------------------------------------------------------------------ the line's words (shared with the context block)

export function timecode(msValue: number): string {
  const s = Math.floor(msValue / 1000);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return (h ? `${h}:${String(m).padStart(2, '0')}` : String(m).padStart(2, '0')) + `:${String(sec).padStart(2, '0')}`;
}

const pct = (v: number) => `${Math.round(v * 100)}%`;

/** "3 frames from trailer.mp4" / "Clip 00:23–00:30" / "Crop 56% × 100%". */
export function actionTitle(a: ActionLineRecord): string {
  if (a.op === 'frame') {
    const n = a.points?.length ?? a.outputs?.length ?? 1;
    return n === 1 ? 'Frame' : `${n} frames`;
  }
  if (a.op === 'clip') {
    const r = a.ranges ?? [];
    return r.length === 1 ? `Clip ${timecode(r[0].inMs)}–${timecode(r[0].outMs)}` : `${r.length} clips`;
  }
  return a.box ? `Crop ${pct(a.box.w)} × ${pct(a.box.h)}` : 'Crop';
}

/** The line's detail: the times, then the elapsed time. */
export function actionDetail(a: ActionLineRecord): string {
  const parts: string[] = [];
  if (a.op === 'frame' && a.points?.length) parts.push(a.points.map(timecode).join(' · '));
  if (a.op === 'clip' && (a.ranges?.length ?? 0) > 1) parts.push(a.ranges!.map((r) => `${timecode(r.inMs)}–${timecode(r.outMs)}`).join(' · '));
  if (a.status === 'done' && typeof a.elapsedMs === 'number') parts.push(`${(a.elapsedMs / 1000).toFixed(1)} s`);
  return parts.join(' · ');
}

// ------------------------------------------------------------------ the action as a chat TURN (ActionLine)

/** The crop in words: its preset aspect when the timeline measured one ("9:16"), else the area the user drew. */
function boxText(box: ActionLineRecord['box'], aspect?: string): string {
  if (aspect) return aspect;
  return box ? `the selected area (${pct(box.w)} × ${pct(box.h)} of the frame)` : 'the selected area';
}

const CROP_PRESETS: readonly [string, number][] = [['16:9', 16 / 9], ['4:3', 4 / 3], ['1:1', 1], ['4:5', 4 / 5], ['9:16', 9 / 16]];

/** The preset a crop box matches on a `videoWidth × videoHeight` frame (within 2%), or undefined. */
export function cropAspectLabel(box: { w: number; h: number }, videoWidth: number, videoHeight: number): string | undefined {
  if (!(videoWidth > 0) || !(videoHeight > 0) || !(box.w > 0) || !(box.h > 0)) return undefined;
  const ratio = (box.w * videoWidth) / (box.h * videoHeight);
  const hit = CROP_PRESETS.find(([, r]) => Math.abs(ratio - r) / r < 0.02);
  return hit?.[0];
}

/** The user's bubble — the request in their words: "Clip 00:00–01:34 of x.mp4", "Extract 6 frames from x.mp4". */
export function actionRequestText(a: ActionLineRecord): string {
  // The user's words, as they would type them.
  if (a.batchTotal) {
    const n = a.batchTotal;
    return a.op === 'clip' ? `Clip ${n} marked clips` : a.op === 'crop' ? `Crop ${n} clips, each to its box`
      : `Extract a frame from each of ${n} clips`;
  }
  if (a.op === 'clip') {
    const r = a.ranges ?? [];
    return r.length === 1
      ? `Clip ${timecode(r[0].inMs)}–${timecode(r[0].outMs)} of ${a.sourceName}`
      : `Clip ${r.length} ranges of ${a.sourceName}`;
  }
  if (a.op === 'frame') {
    const p = a.points ?? [];
    return p.length === 1
      ? `Extract a frame at ${timecode(p[0])} from ${a.sourceName}`
      : `Extract ${p.length || a.outputs?.length || 0} frames from ${a.sourceName}`;
  }
  const span = a.ranges?.[0];
  return span ? `Crop ${timecode(span.inMs)}–${timecode(span.outMs)} of ${a.sourceName} to ${boxText(a.box, a.aspect)}`
    : `Crop ${a.sourceName} to ${boxText(a.box, a.aspect)}`;
}

/** The bubble's muted second line: the times when there are several, then where it came from. */
export function actionRequestMeta(a: ActionLineRecord): string {
  if (a.batchTotal) return a.sourceName && !/^\d+ videos$/.test(a.sourceName) ? `of ${a.sourceName} — from the gallery` : 'from the gallery';
  const times = a.op === 'frame' && (a.points?.length ?? 0) > 1 ? a.points!.map(timecode).join(' · ')
    : a.op === 'clip' && (a.ranges?.length ?? 0) > 1 ? a.ranges!.map((r) => `${timecode(r.inMs)}–${timecode(r.outMs)}`).join(' · ')
      : '';
  return times ? `${times} — from the timeline` : 'from the timeline';
}

/** The reply's step label: "Clip" / "Frames" / "Crop". */
export function actionStepLabel(a: ActionLineRecord): string {
  if (a.batchTotal) return a.op === 'clip' ? 'Clips' : a.op === 'crop' ? 'Crops' : 'Frames';
  if (a.op === 'clip') return 'Clip';
  if (a.op === 'crop') return 'Crop';
  return (a.points?.length ?? a.outputs?.length ?? 1) > 1 ? 'Frames' : 'Frame';
}

/** The in-progress label: "Clipping…" / "Extracting frames…" / "Cropping…". */
export function actionRunningLabel(a: ActionLineRecord): string {
  if (a.batchTotal) {
    const k = Math.min(a.batchTotal, (a.items?.filter((x) => x && (x.outputs || x.error)).length ?? 0) + 1);
    const verb = a.op === 'clip' ? 'Clipping' : a.op === 'crop' ? 'Cropping' : 'Extracting frame';
    return `${verb} ${k} of ${a.batchTotal}…`;
  }
  if (a.op === 'clip') return 'Clipping…';
  if (a.op === 'crop') return 'Cropping…';
  return (a.points?.length ?? 1) > 1 ? 'Extracting frames…' : 'Extracting a frame…';
}

/** A length as you would say it: "7-second" / "1-minute" / "1:34". */
function spanWords(msValue: number): string {
  const s = Math.max(0, Math.round(msValue / 1000));
  if (s < 60) return `${s}-second`;
  return s % 60 === 0 ? `${s / 60}-minute` : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

/** The reply's one line, in plain words: "Here's your 7-second clip." / "Here are your 3 clips." / "The video is cropped to 9:16." */
export function actionReplySummary(a: ActionLineRecord): string {
  if (a.batchTotal) {
    const n = a.outputs?.length ?? 0;
    const noun = (n === 1 ? { frame: 'frame', clip: 'clip', crop: 'crop' } : { frame: 'frames', clip: 'clips', crop: 'crops' })[a.op];
    const failed = batchFailures(a).length;
    return failed ? `Here are your ${n} ${noun} — ${failed} of ${a.batchTotal} could not be made.`
      : n === 1 ? `Here's your ${noun}.` : `Here are your ${n} ${noun}.`;
  }
  if (a.op === 'clip') {
    const r = a.ranges ?? [];
    return r.length === 1 ? `Here's your ${spanWords(r[0].outMs - r[0].inMs)} clip.` : `Here are your ${r.length} clips.`;
  }
  if (a.op === 'frame') {
    const n = a.outputs?.length ?? a.points?.length ?? 1;
    const p = a.points ?? [];
    return n === 1 ? `Here's your frame${p.length === 1 ? ` from ${timecode(p[0])}` : ''}.` : `Here are your ${n} frames.`;
  }
  return `The video is cropped to ${boxText(a.box, a.aspect)}.`;
}

/** A batch's failed items: label (or source name) and why. */
export function batchFailures(a: ActionLineRecord): { name: string; error: string }[] {
  return (a.items ?? []).flatMap((x, i) => (x && x.error && !x.outputs?.length
    ? [{ name: x.label || x.sourceName || `Item ${i + 1}`, error: x.error }] : []));
}

/** Several results of one action open as ONE gallery tab (WorkspaceView builds it in the browser, no agent). */
export const RESULT_GALLERY_EVENT = 'lab:open-result-gallery';
export interface ResultGalleryDetail {
  sessionId: string;
  label: string;
  kind: 'image' | 'video' | 'audio';
  items: { url: string; name: string; kind?: 'image' | 'video' | 'audio' }[];
  /** The gallery this one is the next version of (its subject ids): it opens in that gallery's tab. */
  subjectIds?: string[];
}

/** The gallery tab's manifest: frames as an `image-annotated` gallery, clips as a `clip-gallery`. */
export function resultGalleryManifest(d: ResultGalleryDetail, id: string, createdAt: string): Record<string, unknown> & { id: string } {
  return d.kind === 'image'
    ? { id, template: 'image-annotated', createdAt, title: d.label, src: d.items[0]?.url,
        images: d.items.map((i) => ({ src: i.url, label: i.name, annotations: [] })), annotations: [] }
    : { id, template: 'clip-gallery', createdAt, title: d.label, ...(d.subjectIds?.length ? { subjectIds: d.subjectIds } : {}),
        clips: d.items.map((i, n) => ({ id: `c${n + 1}`, label: i.name, src: i.url, kind: i.kind === 'audio' || (!i.kind && d.kind === 'audio') ? 'audio' : 'video' })) };
}

/**
 * Trimming some of a gallery's items returns the gallery's NEXT VERSION: the same set, in order, each trimmed item in
 * place of its original, declared to be about the gallery it revises — so it opens as a version in that gallery's
 * tab, not as a separate tab (staging 2026-11-15: one Trim all marked opened seven). `record.items[i]` is what the
 * batch's i-th item made; an item that failed keeps its original.
 */
export function setVersionDetail(
  v: { set: { clipId: string; label: string; src: string; kind?: 'video' | 'image' | 'audio' }[]; clips: { clipId: string }[]; origin: { subjectIds: string[]; label?: string } },
  record: ActionLineRecord | undefined, sessionId: string, verb: string,
): ResultGalleryDetail {
  const made = new Map<string, MediaActionOutput>();
  v.clips.forEach((c, i) => { const o = record?.items?.[i]?.outputs?.[0]; if (o) made.set(c.clipId, o); });
  const word = verb === 'trim' ? 'trimmed' : verb === 'crop' ? 'cropped' : 'clipped';
  const items = v.set.map((c) => {
    const o = made.get(c.clipId);
    return o ? { url: o.url, name: `${c.label} · ${word}`, kind: o.kind } : { url: c.src, name: c.label, ...(c.kind ? { kind: c.kind } : {}) };
  });
  const kind = items.every((i) => i.kind === 'audio') ? 'audio' : 'video';
  return { sessionId, kind, items, label: `${v.origin.label || 'Gallery'} · ${word}`, subjectIds: v.origin.subjectIds };
}

/** The gallery tab's name: "3 clips · trailer.mp4" / "8 frames · trailer.mp4". */
export function galleryLabel(op: MediaActionOp, n: number, sourceName?: string): string {
  const what = op === 'frame' ? `${n} frames` : op === 'crop' ? `${n} crops` : `${n} clips`;
  return sourceName ? `${what} · ${sourceName}` : what;
}

/** The reply's footer: "Opened in a new tab · 0.6 s"; several results: "Opened in a gallery tab · 1.1 s". */
export function actionReplyFooter(a: ActionLineRecord): string {
  const opened = (a.outputs?.length ?? 0) > 1 ? 'Opened in a gallery tab' : 'Opened in a new tab';
  return typeof a.elapsedMs === 'number' ? `${opened} · ${(a.elapsedMs / 1000).toFixed(1)} s` : opened;
}

const VERBS: Record<MediaActionOp, [string, string]> = {
  frame: ['extract', 'extracted'], clip: ['clip', 'clipped'], crop: ['crop', 'cropped'],
};

/**
 * The hidden context the NEXT turn carries: every action line since the last assistant reply, so the agent
 * knows about files the user made in the timeline. Empty when there are none.
 */
export function actionContextBlock(messages: readonly ChatMessage[]): string {
  let lastAssistant = -1;
  for (let i = messages.length - 1; i >= 0; i--) if (messages[i].role === 'assistant') { lastAssistant = i; break; }
  const lines: string[] = [];
  for (const m of messages.slice(lastAssistant + 1)) {
    const line = m.role === 'action' ? (m.action as ActionLineRecord | undefined) : undefined;
    if (!line || line.status === 'running') continue;
    // A batch is told item by item, exactly as if each had been its own action.
    const each: ActionLineRecord[] = line.batchTotal
      ? (line.items ?? []).filter((x) => x && (x.outputs?.length || x.error)).map((x) => ({
        op: line.op, status: x.outputs?.length ? 'done' : 'failed', sourceName: x.label ? `${x.sourceName} [${x.label}]` : x.sourceName,
        ...(x.sourceAssetId ? { sourceAssetId: x.sourceAssetId } : {}), ...(x.points ? { points: x.points } : {}),
        ...(x.ranges ? { ranges: x.ranges } : {}), ...(x.box ? { box: x.box } : {}),
        ...(x.outputs ? { outputs: x.outputs } : {}), ...(x.error ? { error: x.error } : {}) }))
      : [line];
    for (const a of each) {
    const src = `${a.sourceName}${a.sourceAssetId ? ` (${a.sourceAssetId})` : ''}`;
    const [verb, done] = VERBS[a.op];
    const object = a.op === 'frame' ? `${actionTitle(a).toLowerCase()} of ${src}${a.points?.length ? ` at ${a.points.map(timecode).join(', ')}` : ''}`
      : a.op === 'clip' ? `${src} ${(a.ranges ?? []).map((r) => `${timecode(r.inMs)}–${timecode(r.outMs)}`).join(', ')}`
        : `${src}${a.box ? ` to box x=${a.box.x.toFixed(3)} y=${a.box.y.toFixed(3)} w=${a.box.w.toFixed(3)} h=${a.box.h.toFixed(3)}` : ''}`;
    const what = `${done} ${object}`;
    if (a.status === 'failed') {
      lines.push(`- tried to ${verb} ${object} — failed: ${a.error ?? 'unknown error'}`);
    } else {
      const outs = (a.outputs ?? []).map((o) => `${o.name} (${o.url})`).join(', ');
      lines.push(`- ${what} → ${outs || 'no output'}`);
    }
    }
  }
  return lines.length ? `[Since your last reply, the user did, in the timeline:]\n${lines.join('\n')}` : '';
}

/** `prompt` with the action context in front (the user's own words stay last, as the turn's request). */
export function withActionContext(messages: readonly ChatMessage[], prompt: string): string {
  const block = actionContextBlock(messages);
  return block ? `${block}\n\n${prompt}` : prompt;
}

// ------------------------------------------------------------------ the flow (timeline button and Retry)

export interface DirectActionOptions {
  /** The chat path (the old `chat:send` prompt): used when the direct path refuses or the action fails. */
  fallback: () => void;
  /** Open the results (new Renderer tabs). */
  /** `record`: a batch's line, whose `items[i].outputs` say which item made what (a set's next version uses it). */
  openOutputs: (outputs: MediaActionOutput[], op: MediaActionOp, sourceName?: string, record?: ActionLineRecord) => void;
  now?: () => number;
}

const retries = new Map<string, { body: MediaActionBody; opts: DirectActionOptions; startedAt: number }>();

const nameFromLocation = (loc?: string) => {
  const last = (loc ?? '').split('?')[0].split('/').filter(Boolean).pop() ?? '';
  try { return decodeURIComponent(last) || 'video'; } catch { return last || 'video'; }
};

/**
 * Run one direct action end to end: a running line at once, then done (results opened) or failed. A refusal
 * before running (not a project file, no project, over the length cap) removes the line and takes the chat
 * path; a failed run keeps the failed line and ALSO hands the request to the agent, so it still gets done.
 */
/** What makes two actions the same: one session, one op, one file, the same ranges / points / box. */
const actionIdentity = (b: MediaActionBody) => JSON.stringify([b.sessionId, b.op, b.source.assetId ?? b.source.location ?? '',
  b.ranges ?? null, b.points ?? null, b.box ?? null, b.aspect ?? null]);
const inFlight = new Map<string, Promise<MediaActionReply>>();

/**
 * Run one direct action, at most once at a time per identity: Clip can take 10-16 s the first time (the sandbox
 * starts), the button gave no sign of it, and a second click cut a second identical clip (IND403 dry run). A repeat
 * while the same action runs joins it — one line, one result, one tab — instead of starting another.
 */
export function runDirectTimelineAction(body: MediaActionBody, opts: DirectActionOptions, startedAt?: number): Promise<MediaActionReply> {
  const key = actionIdentity(body);
  const running = inFlight.get(key);
  if (running) return running;
  const run = runDirectTimelineActionOnce(body, opts, startedAt).finally(() => inFlight.delete(key));
  inFlight.set(key, run);
  return run;
}

async function runDirectTimelineActionOnce(body: MediaActionBody, opts: DirectActionOptions, startedAt?: number): Promise<MediaActionReply> {
  const now = opts.now ?? (() => Date.now());
  const t0 = startedAt ?? now();
  retries.set(body.actionId, { body, opts, startedAt: t0 });
  const base: ActionLineRecord = {
    op: body.op, status: 'running', sourceName: nameFromLocation(body.source.location),
    ...(body.points ? { points: body.points } : {}), ...(body.ranges ? { ranges: body.ranges } : {}),
    ...(body.box ? { box: body.box } : {}), ...(body.aspect ? { aspect: body.aspect } : {}),
  };
  const line = (a: ActionLineRecord) => publishActionLine({ sessionId: body.sessionId, message: actionLineMessage(body.actionId, t0, a) });
  line(base);
  const reply = await runMediaAction(body);
  if (reply.ok) {
    line({ ...base, ...(reply.record ?? {}), status: 'done', outputs: reply.outputs, elapsedMs: reply.elapsedMs });
    opts.openOutputs(reply.outputs, body.op, base.sourceName);
  } else if (reply.unknown) {
    // No answer: it may have run, so it is NOT handed to the agent (that cut the clips a second time). The line
    // says so and offers Retry; the outputs of a run that did finish land in the project regardless.
    line({ ...base, status: 'failed', error: reply.error });
  } else if (!reply.ran) {
    publishActionLine({ sessionId: body.sessionId, message: actionLineMessage(body.actionId, t0, base), remove: true });
    retries.delete(body.actionId);
    opts.fallback();
  } else {
    line({ ...base, ...(reply.record ?? {}), status: 'failed', error: reply.error, handedToAgent: true });
    opts.fallback();
  }
  return reply;
}

const batchRetries = new Map<string, { batch: MediaActionBatch; opts: DirectActionOptions; startedAt: number; record: ActionLineRecord }>();

/**
 * Run a batch: ONE action line that counts up ("Cropping 3 of 12…"), its items sent one after another to the
 * chat's warm sandbox (`only` limits a retry to the items that failed), then ONE gallery tab with every result.
 * If the FIRST item is refused before running (not a file of the chat's project, …) nothing was made: the line
 * goes and the batch takes the chat path, as a single action does. Later failures stay on their items.
 */
export async function runDirectBatch(batch: MediaActionBatch, opts: DirectActionOptions, startedAt?: number,
  prior?: ActionLineRecord, only?: ReadonlySet<number>): Promise<ActionLineRecord> {
  const now = opts.now ?? (() => Date.now());
  const t0 = startedAt ?? now();
  const total = batch.items.length;
  const names = new Set(batch.items.map((i) => nameFromLocation(i.source.location)));
  let record: ActionLineRecord = prior ? { ...prior, status: 'running' } : {
    op: batch.op, status: 'running', sourceName: names.size === 1 ? [...names][0] : `${total} videos`,
    batchTotal: total, items: [], outputs: [],
  };
  const line = (a: ActionLineRecord) => publishActionLine({ sessionId: batch.sessionId, message: actionLineMessage(batch.actionId, t0, a) });
  line(record);
  for (let index = 0; index < total; index++) {
    if (only && !only.has(index)) continue;
    const { label, ...item } = batch.items[index];
    const reply = await runMediaAction({ ...item, op: batch.op, sessionId: batch.sessionId, actionId: batch.actionId,
      batch: { id: batch.actionId, index, total, ...(label ? { label } : {}) } });
    if (!reply.ok && !reply.ran && !reply.unknown && index === 0 && !prior) {
      publishActionLine({ sessionId: batch.sessionId, message: actionLineMessage(batch.actionId, t0, record), remove: true });
      opts.fallback();
      return record;
    }
    if (reply.record) {
      record = { ...reply.record, status: 'running' };
    } else {
      // Nothing came back (network, a refusal): mark the item here so the line still counts it.
      const items = [...(record.items ?? [])];
      items[index] = { ...(label ? { label } : {}), sourceName: nameFromLocation(item.source.location),
        error: reply.ok ? 'no result' : reply.error };
      record = { ...record, items };
    }
    line(record);
  }
  const outputs = record.outputs ?? [];
  record = { ...record, status: outputs.length ? 'done' : 'failed',
    ...(outputs.length ? {} : { error: record.error ?? batchFailures(record)[0]?.error ?? 'Nothing could be made.' }) };
  line(record);
  batchRetries.set(batch.actionId, { batch, opts, startedAt: t0, record });
  if (outputs.length) opts.openOutputs(outputs, batch.op, /^\d+ videos$/.test(record.sourceName) ? undefined : record.sourceName, record);
  return record;
}

/**
 * Where a timeline action goes (WorkspaceView's Renderer bridge): DIRECT when the chat has a project and the
 * action is one `directMediaRequest` can express, else to the agent (`sendToAgent`, the old chat turn).
 */
export function routeTimelineAction(
  data: TimelineActionLike | undefined,
  sessionId: string | null | undefined,
  deps: {
    projectOf: (sessionId: string) => string | null | undefined;
    sendToAgent: () => void;
    openOutputs: (outputs: MediaActionOutput[], sessionId: string, op?: MediaActionOp, sourceName?: string, record?: ActionLineRecord) => void;
    newActionId?: () => string;
    /** Make sure the chat's row (with its project) exists first: a brand-new chat has none until its first turn. */
    prepare?: (sessionId: string) => Promise<void>;
  },
): 'direct' | 'agent' {
  const sid = sessionId ?? '';
  const id = (deps.newActionId ?? (() => `act-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`))();
  const hasProject = !!sid && !!deps.projectOf(sid);
  const opts = { fallback: deps.sendToAgent, openOutputs: (outputs: MediaActionOutput[], op: MediaActionOp, name?: string, record?: ActionLineRecord) => deps.openOutputs(outputs, sid, op, name, record) };
  // A failed prepare still runs the action: the proxy then refuses it and the chat path takes over, as before.
  const ready = () => (deps.prepare ? deps.prepare(sid).catch(() => undefined) : Promise.resolve());
  const batch = hasProject ? directBatchRequest(data, sid, id) : null;
  if (batch) {
    void ready().then(() => runDirectBatch(batch, opts));
    return 'direct';
  }
  const direct = hasProject ? directMediaRequest(data, sid, id) : null;
  if (!direct) {
    deps.sendToAgent();
    return 'agent';
  }
  void ready().then(() => runDirectTimelineAction(direct, opts));
  return 'direct';
}

/** Retry a failed line in place (same id: the proxy upserts the transcript line by it). No second fallback. */
export function retryDirectAction(actionId: string): boolean {
  const b = batchRetries.get(actionId);
  if (b) {
    // Only the items that made nothing run again; the line keeps the rest.
    const failed = new Set((b.record.items ?? []).flatMap((x, i) => (x && x.outputs?.length ? [] : [i])));
    for (let i = (b.record.items?.length ?? 0); i < b.batch.items.length; i++) failed.add(i);
    void runDirectBatch(b.batch, { ...b.opts, fallback: () => {} }, b.startedAt, b.record, failed);
    return true;
  }
  const r = retries.get(actionId);
  if (!r) return false;
  void runDirectTimelineAction(r.body, { ...r.opts, fallback: () => {} }, r.startedAt);
  return true;
}

export function canRetryDirectAction(actionId: string): boolean {
  return retries.has(actionId) || batchRetries.has(actionId);
}

/** Test seam. */
export function __resetDirectActionsForTests(): void { retries.clear(); batchRetries.clear(); }
