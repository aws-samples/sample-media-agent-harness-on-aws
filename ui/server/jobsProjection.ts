// Shared JobRegistry → client `Job` projection.
//
// Both the internal `/jobs*` routes (Cognito operator realm) and the external
// `GET /invoke/:agentId/jobs` route (OIDC caller realm) return the SAME `Job`
// shape the dev-portal client already consumes (`ui/src/types/jobs.ts`). This
// module is the single source of truth for that projection so the two realms
// can never drift.

import type { Job, JobStatus } from "../src/types/jobs";

/**
 * Project a raw DynamoDB JobRegistry item onto the client-facing `Job`. Coerces
 * every field to its declared type with safe defaults so a partially-written or
 * legacy row never yields `undefined` on the wire.
 */
export const projectJob = (item: Record<string, any>): Job => ({
  jobId: String(item.jobId ?? ""),
  actorId: String(item.actorId ?? ""),
  sessionId: String(item.sessionId ?? ""),
  trioId: String(item.trioId ?? ""),
  gatewayId: String(item.gatewayId ?? ""),
  status: (item.status ?? "PENDING") as JobStatus,
  startAt: String(item.startAt ?? ""),
  updatedAt: String(item.updatedAt ?? ""),
  terminalAt: item.terminalAt ?? null,
  hasRenderableArtifacts: item.hasRenderableArtifacts === true,
  lastError: item.lastError ?? null,
  runId: item.runId ?? null,
  turnId: item.turnId ?? null,
  nodeId: item.nodeId ?? null,
  dependsOn: Array.isArray(item.dependsOn) ? item.dependsOn.map(String) : [],
  artifactUrls: Array.isArray(item.artifactUrls) ? item.artifactUrls.map(String) : [],
  // What a completed plan node made (the plan chainer records it, projects.lineageEdges); absent until then.
  ...(Array.isArray(item.outputs) ? { outputs: projectOutputs(item.outputs) } : {}),
});

const OUTPUT_KINDS = new Set(["video", "image", "audio"]);

/** The chainer's `outputs` list, keeping only well-formed entries (a bad row never breaks the wire shape). */
export function projectOutputs(raw: unknown[]): NonNullable<Job["outputs"]> {
  const out: NonNullable<Job["outputs"]> = [];
  for (const o of raw) {
    if (!o || typeof o !== "object") continue;
    const { uri, key, kind } = o as Record<string, unknown>;
    if (typeof uri === "string" && typeof key === "string" && typeof kind === "string" && OUTPUT_KINDS.has(kind)) {
      out.push({ uri, key, kind: kind as "video" | "image" | "audio" });
    }
  }
  return out;
}
