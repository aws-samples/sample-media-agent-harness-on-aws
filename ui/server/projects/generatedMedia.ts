// Generated_Media turn message (media-harness-project-ingestion Req 9, design §16).
//
// At the end of a Projects turn the proxy enqueues one `{kind: "turn"}` message for GeneratedMediaFn, which
// registers the media the turn produced into the turn's Active_Project. The proxy is the producer because it
// alone holds both the turn's Active_Project (from the turn gate) and the verified Actor at turn end.
//
// `externalRefs` are the content-addressed `artifacts/external/<24 hex>[.ext]` keys (fal outputs copied by
// `agent/src/media/persist_media.py`) that the reply text, its steps, or its summary name. They are
// session-less, so a reference is the only way to know a turn produced them. Best effort: the Lambda also
// reads the render manifests the turn wrote, and session objects are found by time window, not by reference.
//
// Sending never blocks or fails the turn: an error is logged and dropped.

export const EXTERNAL_REF_RE = /artifacts\/external\/[0-9a-f]{24}(?:\.[A-Za-z0-9]{2,5})?(?![A-Za-z0-9])/g;
const MAX_REFS = 200;

export interface GeneratedMediaTurnMessage {
  kind: "turn";
  sessionId: string;
  turnId: string;
  projectId: string;
  actorSub: string;
  /** Epoch milliseconds. */
  startedAt: number;
  endedAt: number;
  externalRefs: string[];
  /** asset-lineage design §3: the turn's output → inputs ledger from the agent's execution_summary. */
  lineage?: LineageEntry[];
}

export interface LineageEntry {
  output: string;
  inputs: string[];
  tool?: string;
  // Lineage-edge fields (agent/src/projects/lineage_contract.py), passed through bounded. The registrar uses them
  // only where `projects.lineageEdges` is on.
  unresolved?: { ref: string; reason: string }[];
  origin?: string;
  ranges?: { input: string; start: number; end: number; unit: string }[];
  settings?: Record<string, unknown>;
  jobId?: string;
  via?: string;
  inferred?: boolean;
}

const MAX_LINEAGE = 100;
// The 20-input cap is lifted for lineage edges (a 30-clip assembly keeps all 30); the registrar caps what it
// writes to the ASSET row's `derivedFrom`. The whole message stays under SQS's 256 KB (`MAX_LINEAGE_BYTES`).
const MAX_LINEAGE_INPUTS = 500;
const MAX_URI = 2048;
const MAX_LINEAGE_BYTES = 180_000;
const MAX_SETTINGS_BYTES = 1024;

const isUri = (i: unknown): i is string => typeof i === "string" && i.length > 0 && i.length <= MAX_URI;
const finite = (n: unknown): n is number => typeof n === "number" && Number.isFinite(n);

function extras(e: Record<string, unknown>): Partial<LineageEntry> {
  const out: Partial<LineageEntry> = {};
  if (Array.isArray(e.unresolved)) {
    const u = e.unresolved
      .filter((x): x is { ref: string; reason?: unknown } => !!x && typeof (x as { ref?: unknown }).ref === "string")
      .map((x) => ({ ref: x.ref.split("?")[0].slice(0, 512), reason: typeof x.reason === "string" ? x.reason.slice(0, 64) : "unresolved" }))
      .filter((x) => x.ref);
    if (u.length) out.unresolved = u.slice(0, MAX_LINEAGE_INPUTS);
  }
  if (typeof e.origin === "string" && /^https?:\/\//i.test(e.origin)) out.origin = e.origin.split("?")[0].slice(0, 512);
  if (Array.isArray(e.ranges)) {
    const r = e.ranges
      .filter((x): x is { input: string; start: number; end: number; unit?: unknown } =>
        !!x && isUri((x as { input?: unknown }).input) && finite((x as { start?: unknown }).start) && finite((x as { end?: unknown }).end))
      .map((x) => ({ input: x.input, start: x.start, end: x.end, unit: x.unit === "frame" ? "frame" : "s" }));
    if (r.length) out.ranges = r.slice(0, MAX_LINEAGE_INPUTS);
  }
  if (e.settings && typeof e.settings === "object" && !Array.isArray(e.settings)) {
    const json = JSON.stringify(e.settings);
    if (json.length <= MAX_SETTINGS_BYTES) out.settings = e.settings as Record<string, unknown>;
  }
  if (typeof e.jobId === "string" && e.jobId) out.jobId = e.jobId.slice(0, 512);
  if (typeof e.via === "string" && e.via) out.via = e.via.slice(0, 32);
  if (e.inferred === true) out.inferred = true;
  return out;
}

/** The well-formed `lineage` entries of an execution_summary (`summary.lineage`); malformed ones are dropped. */
export function lineageIn(summary: unknown): LineageEntry[] {
  const raw = (summary as { lineage?: unknown } | null)?.lineage;
  if (!Array.isArray(raw)) return [];
  const out: LineageEntry[] = [];
  for (const e of raw) {
    if (out.length >= MAX_LINEAGE) break;
    const o = (e as { output?: unknown })?.output;
    if (typeof o !== "string" || !o || o.length > MAX_URI) continue;
    const ins = (e as { inputs?: unknown }).inputs;
    const inputs = Array.isArray(ins) ? [...new Set(ins.filter(isUri))].slice(0, MAX_LINEAGE_INPUTS) : [];
    const tool = (e as { tool?: unknown }).tool;
    const base: LineageEntry = typeof tool === "string" && tool ? { output: o, inputs, tool: tool.slice(0, 64) } : { output: o, inputs };
    out.push({ ...base, ...extras(e as Record<string, unknown>) });
  }
  // Bound the message: halve the longest input lists until it fits (outputs are never dropped for size). The size
  // is kept arithmetically (each input costs its JSON length + a comma), so this is linear, not a re-stringify loop.
  let size = JSON.stringify(out).length;
  while (size > MAX_LINEAGE_BYTES) {
    const longest = out.reduce((a, b) => (b.inputs.length > a.inputs.length ? b : a), out[0]);
    if (!longest || longest.inputs.length <= 1) break;
    const keep = Math.ceil(longest.inputs.length / 2);
    for (const dropped of longest.inputs.slice(keep)) size -= JSON.stringify(dropped).length + 1;
    longest.inputs = longest.inputs.slice(0, keep);
    if (longest.ranges) {
      const kept = new Set(longest.inputs);
      const before = JSON.stringify(longest.ranges).length;
      longest.ranges = longest.ranges.filter((r) => kept.has(r.input));
      size -= before - JSON.stringify(longest.ranges).length;
    }
  }
  return out;
}

/** Every external key named in `texts`, sorted and unique (capped so a message stays far under 256 KB). */
export function externalRefsIn(...texts: string[]): string[] {
  const found = new Set<string>();
  for (const t of texts) {
    for (const m of (t ?? "").matchAll(EXTERNAL_REF_RE)) found.add(m[0]);
  }
  return [...found].sort().slice(0, MAX_REFS);
}

export interface TurnEnd {
  sessionId: string | undefined;
  turnId: string | undefined;
  projectId: string | undefined;
  actorSub: string | undefined;
  startedAt: number;
  endedAt: number;
  text: string;
  steps: unknown;
  summary: unknown;
}

/** The message for a finished turn, or `null` when it was not a Projects turn of a known Actor. */
export function buildTurnMessage(t: TurnEnd): GeneratedMediaTurnMessage | null {
  if (!t.sessionId || !t.turnId || !t.projectId || !t.actorSub) return null;
  if (!(t.startedAt > 0 && t.endedAt >= t.startedAt)) return null;
  const lineage = lineageIn(t.summary);
  return {
    kind: "turn",
    sessionId: t.sessionId,
    turnId: t.turnId,
    projectId: t.projectId,
    actorSub: t.actorSub,
    startedAt: t.startedAt,
    endedAt: t.endedAt,
    externalRefs: externalRefsIn(t.text, JSON.stringify(t.steps ?? []), JSON.stringify(t.summary ?? null)),
    ...(lineage.length ? { lineage } : {}),
  };
}

/** `send` posts one SQS message body; the notifier never throws and never awaits on the turn's path. */
export function makeGeneratedMediaNotifier(send: (body: string) => Promise<unknown>) {
  return (t: TurnEnd): void => {
    const msg = buildTurnMessage(t);
    if (!msg) return;
    void send(JSON.stringify(msg)).catch((e: any) =>
      console.error("[agentcore-proxy] generated-media enqueue failed:", e?.name ?? e?.message ?? e));
  };
}
