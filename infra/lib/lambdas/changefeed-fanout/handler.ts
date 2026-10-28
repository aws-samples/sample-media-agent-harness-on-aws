/**
 * Changefeed fanout Lambda — async-job-tracking task 9.1.
 *
 * Subscribes to the JobRegistry DynamoDB stream and writes one
 * `JobUpdateEvent`-shaped message per relevant mutation onto a single
 * SQS FIFO queue. The agentcore-proxy long-polls that queue and
 * re-emits each message as an SSE frame on `/jobs/stream` (task 10.2).
 *
 * Wire format (see `.kiro/specs/async-job-tracking/design.md` →
 * "Push_Channel" → "Wire format" and `ui/src/types/jobs.ts`):
 *
 *     event: job.update
 *     data:  <Job projection JSON>
 *
 *     event: job.terminal
 *     data:  <Job projection JSON>
 *
 * The `event` and `data` fields together form the `JobUpdateEvent`
 * envelope; the proxy translates that into the SSE wire syntax above.
 *
 * Per-actor delivery: the design calls for "per-actor" fan-out. We
 * implement this as a SINGLE FIFO queue with `MessageGroupId = actorId`
 * rather than provisioning one queue per actor. This gives FIFO
 * ordering per Actor while keeping the infrastructure footprint to one
 * queue. The agentcore-proxy filters subscribers by the JWT-validated
 * actorId at receive time; cross-actor leakage is impossible because
 * each subscriber polls with its own `MessageGroupId` filter pattern.
 *
 * CDK wiring (event source mapping, env vars, IAM) lives in task 9.2.
 */

import { createHash } from "crypto";

import type { DynamoDBStreamEvent, DynamoDBRecord } from "aws-lambda";
import { unmarshall } from "@aws-sdk/util-dynamodb";
import {
  SQSClient,
  SendMessageBatchCommand,
  type SendMessageBatchRequestEntry,
} from "@aws-sdk/client-sqs";
import {
  DynamoDBClient,
  PutItemCommand,
  QueryCommand,
  UpdateItemCommand,
  type AttributeValue,
} from "@aws-sdk/client-dynamodb";
import { matchPlanNode, nodeIdOfKey, type PlanStateRow } from "./planNodeMatch";

// ---------------------------------------------------------------------------
// Types — duplicated locally to keep this Lambda self-contained.
//
// The canonical definitions live at `ui/src/types/jobs.ts`. They are
// duplicated here (rather than imported across packages) because the
// infra package compiles in isolation and does not have a path into
// `ui/src/`. Field set MUST stay in sync with the ui types — the SSE
// consumer parses each `data` payload back into a `Job`.
// ---------------------------------------------------------------------------

type JobStatus =
  | "PENDING"
  | "RUNNING"
  | "SUCCEEDED"
  | "FAILED"
  | "TIMED_OUT"
  | "ABORTED";

interface Job {
  jobId: string;
  actorId: string;
  sessionId: string;
  trioId: string;
  gatewayId: string;
  status: JobStatus;
  startAt: string;
  updatedAt: string;
  terminalAt: string | null;
  hasRenderableArtifacts: boolean;
  lastError: string | null;
  runId: string | null;
  nodeId: string | null;
  /** fal queue endpoint (queue jobs only): the chainer's results fetch needs it with the id. */
  endpointId: string | null;
  /** The plan node that submitted this gateway job (job-tracking wrap inside a node). */
  planNodeId: string | null;
  dependsOn: string[];
  artifactUrls: string[];
  /** What a completed plan node made (recorded by the plan chainer); omitted until then. */
  outputs?: { uri: string; key: string; kind: string }[];
}

interface JobUpdateEvent {
  event: "job.update" | "job.terminal";
  data: Job;
}

// ---------------------------------------------------------------------------
// Module-level state — clients are reused across warm invocations.
// ---------------------------------------------------------------------------

const QUEUE_URL_ENV = "JOB_FANOUT_QUEUE_URL";

const sqs = new SQSClient({
  region: process.env.AWS_REGION ?? process.env.AWS_DEFAULT_REGION,
});

// SQS SendMessageBatch caps at 10 entries per call.
const SQS_BATCH_SIZE = 10;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Project a raw DDB image (already `unmarshall`ed) into the `Job`
 * shape carried on the wire. Returns `null` when the image is missing
 * fields that make it impossible to address an Actor — those rows are
 * dropped silently because the SSE consumer cannot route them.
 */
function projectJob(image: Record<string, unknown>): Job | null {
  const jobId = image.jobId;
  const actorId = image.actorId;
  const status = image.status;
  if (
    typeof jobId !== "string" ||
    typeof actorId !== "string" ||
    typeof status !== "string"
  ) {
    return null;
  }
  return {
    jobId,
    actorId,
    sessionId: typeof image.sessionId === "string" ? image.sessionId : "",
    trioId: typeof image.trioId === "string" ? image.trioId : "",
    gatewayId: typeof image.gatewayId === "string" ? image.gatewayId : "",
    status: status as JobStatus,
    startAt: typeof image.startAt === "string" ? image.startAt : "",
    updatedAt: typeof image.updatedAt === "string" ? image.updatedAt : "",
    terminalAt:
      typeof image.terminalAt === "string" ? image.terminalAt : null,
    hasRenderableArtifacts: image.hasRenderableArtifacts === true,
    lastError: typeof image.lastError === "string" ? image.lastError : null,
    runId: typeof image.runId === "string" ? image.runId : null,
    nodeId: typeof image.nodeId === "string" ? image.nodeId : null,
    endpointId: typeof image.endpointId === "string" ? image.endpointId : null,
    planNodeId: typeof image.planNodeId === "string" ? image.planNodeId : null,
    dependsOn: Array.isArray(image.dependsOn)
      ? image.dependsOn.filter((x): x is string => typeof x === "string")
      : [],
    artifactUrls: Array.isArray(image.artifactUrls)
      ? image.artifactUrls.filter((x): x is string => typeof x === "string")
      : [],
    // Same as `GET /jobs` (ui/server/jobsProjection.ts projectOutputs): only well-formed media entries.
    ...(Array.isArray(image.outputs) ? { outputs: projectOutputs(image.outputs) } : {}),
  };
}

const OUTPUT_KINDS = new Set(["video", "image", "audio"]);

function projectOutputs(raw: unknown[]): { uri: string; key: string; kind: string }[] {
  const out: { uri: string; key: string; kind: string }[] = [];
  for (const o of raw) {
    if (!o || typeof o !== "object") continue;
    const { uri, key, kind } = o as Record<string, unknown>;
    if (typeof uri === "string" && typeof key === "string" && typeof kind === "string" && OUTPUT_KINDS.has(kind)) {
      out.push({ uri, key, kind });
    }
  }
  return out;
}

/**
 * Decide whether a stream record should produce an SQS frame, and if
 * so, what kind. Returns `null` for events that should be skipped:
 *
 *   - `REMOVE` events (TTL deletes on already-terminal records — the
 *     consumer already saw `job.terminal` for that job).
 *   - `MODIFY` events that change attributes other than `status` or
 *     `terminalAt` (e.g. a `consecutiveErrors` bump from the polling
 *     worker on a transient failure). Emitting these as `job.update`
 *     would be noise on the wire.
 *
 * The SSE event name is decided as follows:
 *
 *   - `job.terminal` when `terminalAt` is freshly stamped on the new
 *     image (the registry stamps this attribute only on terminal
 *     transition — see `JobRegistry.set_terminal`).
 *   - `job.update` otherwise.
 */
interface ProjectedFrame {
  payload: JobUpdateEvent;
  groupId: string;
  dedupId: string;
  /**
   * The row just reached terminal (fresh `terminalAt`). Kept apart from `payload.event` because a
   * plan-node FAILURE is downgraded to `job.update` for the UI, and the plan-state bridge must still
   * see it: a gateway job registered with `nodeId` (the task agent's SFN/queue registration does
   * this) is downgraded too, and keying the bridge on the event name left that node waiting forever.
   */
  terminalTransition: boolean;
}

export function classify(record: DynamoDBRecord): ProjectedFrame | null {
  if (record.eventName !== "INSERT" && record.eventName !== "MODIFY") {
    return null;
  }
  const newImageRaw = record.dynamodb?.NewImage;
  if (!newImageRaw) {
    return null;
  }

  // `unmarshall` from `@aws-sdk/util-dynamodb` expects the SDK's
  // `AttributeValue` shape; `aws-lambda`'s stream record uses a
  // structurally-identical but nominally-different type. Cast through
  // `any` — the wire shape is the same JSON in both cases.
  const newImage = unmarshall(newImageRaw as any) as Record<string, unknown>;
  const newJob = projectJob(newImage);
  if (!newJob) {
    return null;
  }

  const oldImage = record.dynamodb?.OldImage
    ? (unmarshall(record.dynamodb.OldImage as any) as Record<string, unknown>)
    : undefined;

  const newTerminal = typeof newImage.terminalAt === "string";
  const oldTerminal = oldImage
    ? typeof oldImage.terminalAt === "string"
    : false;

  let event: "job.update" | "job.terminal";
  if (newTerminal && !oldTerminal) {
    // Fresh terminal transition (works for both INSERT and MODIFY,
    // though INSERT-with-terminal is unexpected in practice).
    event = "job.terminal";
  } else if (record.eventName === "INSERT") {
    // Registration → emit a single `job.update` so the sidebar can
    // surface the new row immediately.
    event = "job.update";
  } else {
    // MODIFY without a fresh terminalAt: emit only when the status
    // actually changed. This filters out side-effect-only writes such
    // as `consecutiveErrors` bumps.
    if (oldImage?.status === newJob.status) {
      return null;
    }
    event = "job.update";
  }

  // A plan NODE's terminal FAILURE is retry-eligible (the chainer re-dispatches a failed node
  // with its error injected) and rolls up into the PLAN's own terminal event. Emitting a node
  // failure as `job.terminal` fired a user-facing "failed" toast for a node the chainer then
  // retried to SUCCESS (observed on 69eb360a#t2: first attempt FAILED → failed notification →
  // retry → SUCCEEDED). Downgrade a plan-node's terminal failure to a `job.update` so the
  // sidebar still reflects the status, but only the PLAN's job.terminal is the user-facing
  // outcome. Node SUCCESS terminals and all plan/lone-job terminals are unchanged.
  const isPlanNode =
    Boolean(newJob.nodeId) ||
    (typeof newJob.trioId === "string" && newJob.trioId.startsWith("node|")) ||
    newJob.jobId.includes("#");
  const failedStatus =
    newJob.status === "FAILED" ||
    newJob.status === "TIMED_OUT" ||
    newJob.status === "ABORTED";
  if (event === "job.terminal" && isPlanNode && failedStatus) {
    event = "job.update";
  }

  // MessageDeduplicationId: deterministic over (jobId, updatedAt, event).
  // Hashing keeps the value within SQS's 128-character limit regardless
  // of jobId length (SFN execution ARNs can be long) and avoids any
  // character-class issues with FIFO dedup id constraints. The `event`
  // bit guarantees that a single (jobId, updatedAt) pair which somehow
  // produced both an `update` and a `terminal` frame is not collapsed.
  const dedupId = createHash("sha256")
    .update(`${newJob.jobId}|${newJob.updatedAt}|${event}`)
    .digest("hex");

  return {
    payload: { event, data: newJob },
    terminalTransition: newTerminal && !oldTerminal,
    // Per-actor FIFO ordering — see the file header for why one queue
    // is enough rather than provisioning per-actor queues.
    groupId: newJob.actorId,
    dedupId,
  };
}

// ---------------------------------------------------------------------------
// Plan State Bridge — when an async job with a runId reaches terminal,
// write the corresponding node status to the Plan State table so the
// Plan Chainer Lambda can react and dispatch the next node.
// ---------------------------------------------------------------------------

const PLAN_STATE_TABLE = process.env.PLAN_STATE_TABLE || "";
const planStateDdb = PLAN_STATE_TABLE
  ? new DynamoDBClient({ region: process.env.AWS_REGION || "us-west-2" })
  : null;

/** A session's plan-state rows, optionally only one plan's `<runId>#` rows. Paginated. */
async function queryPlanState(
  client: Pick<DynamoDBClient, "send">, table: string, sessionId: string, runId?: string,
): Promise<PlanStateRow[]> {
  const rows: PlanStateRow[] = [];
  let start: Record<string, AttributeValue> | undefined;
  do {
    const result: any = await client.send(new QueryCommand({
      TableName: table,
      KeyConditionExpression: runId
        ? "session_id = :sid AND begins_with(node_id, :prefix)"
        : "session_id = :sid",
      ExpressionAttributeValues: {
        ":sid": { S: sessionId },
        ...(runId ? { ":prefix": { S: `${runId}#` } } : {}),
      },
      ExclusiveStartKey: start,
    }));
    for (const item of result.Items ?? []) {
      rows.push({
        nodeKey: item.node_id?.S ?? "",
        status: item.status?.S ?? "",
        jobId: item.job_id?.S ?? "",
        planId: item.plan_id?.S ?? "",
      });
    }
    start = result.LastEvaluatedKey;
  } while (start);
  return rows;
}

/**
 * Re-fire the plan chainer for a node that finished before its gateway job settled.
 *
 * The chainer listens to plan-state MODIFY events; a node already `completed`/`skipped` must not be
 * reopened, so this bumps `updated_at` (and records the settled job) with the status untouched,
 * conditioned on the row still being finished. Best effort: a failure only costs the re-fire.
 */
export async function rearmFinishedNode(
  client: Pick<DynamoDBClient, "send">, table: string, sessionId: string, nodeKey: string,
  jobId: string, now: string,
): Promise<void> {
  try {
    await client.send(new UpdateItemCommand({
      TableName: table,
      Key: { session_id: { S: sessionId }, node_id: { S: nodeKey } },
      UpdateExpression: "SET updated_at = :now, settled_job_id = :jid",
      ConditionExpression: "attribute_exists(node_id) AND #st IN (:done, :skipped)",
      ExpressionAttributeNames: { "#st": "status" },
      ExpressionAttributeValues: {
        ":now": { S: now },
        ":jid": { S: jobId },
        ":done": { S: "completed" },
        ":skipped": { S: "skipped" },
      },
    }));
    console.log(`[bridge] Re-armed finished node ${nodeKey} so the chainer re-checks its plan`);
  } catch (e: any) {
    if (e?.name !== "ConditionalCheckFailedException") {
      console.warn(`[bridge] Could not re-arm finished node ${nodeKey}:`, e);
    }
  }
}

const FAILURE_REASON_MAX_CHARS = 2000;

/**
 * The human reason for a failed gateway job, from its registry `lastError`.
 *
 * An SFN-polled M2C job's `lastError` is the execution `cause`: a Lambda error JSON whose
 * `errorMessage` is the reason and whose `stackTrace` is noise (`{"errorMessage":"No VAD turns found
 * after flattening — check VAD output","errorType":"RuntimeError","stackTrace":[…]}`). Keep the message
 * (and its type); anything else is passed through, bounded. Never invents a reason: no `lastError`
 * gives the bare status so the row still says what is known.
 */
export function jobFailureReason(lastError: string | null | undefined, status: string): string {
  const raw = typeof lastError === "string" ? lastError.trim() : "";
  if (!raw) return `job ${status}`;
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object" && typeof parsed.errorMessage === "string" && parsed.errorMessage) {
      const type = typeof parsed.errorType === "string" && parsed.errorType ? ` (${parsed.errorType})` : "";
      return `${parsed.errorMessage}${type}`.slice(0, FAILURE_REASON_MAX_CHARS);
    }
  } catch {
    // not JSON (or a cause truncated mid-stack-trace): take the errorMessage field if it is there
    const m = raw.match(/"errorMessage"\s*:\s*"((?:[^"\\]|\\.)*)"/);
    if (m) {
      let msg = m[1];
      try { msg = JSON.parse(`"${m[1]}"`); } catch { /* keep the escaped text */ }
      const t = raw.match(/"errorType"\s*:\s*"([^"\\]*)"/);
      return `${msg}${t && t[1] ? ` (${t[1]})` : ""}`.slice(0, FAILURE_REASON_MAX_CHARS);
    }
  }
  return raw.slice(0, FAILURE_REASON_MAX_CHARS);
}

export async function bridgeToPlanState(
  frames: ProjectedFrame[],
  client: Pick<DynamoDBClient, "send"> | null = planStateDdb,
  table: string = PLAN_STATE_TABLE,
): Promise<void> {
  if (!client || !table) return;

  for (const frame of frames) {
    if (!frame.terminalTransition) continue;
    const job = frame.payload.data;
    if (!job.runId) continue;

    // Skip plan-node entries (gateway = __coordinator__) — those are managed
    // by the Task Agent and Plan Chainer directly, not via this bridge.
    if (job.gatewayId === "__coordinator__") continue;

    // This is a real gateway job (e.g. SFN stem separation) that belongs to a plan.
    // Find the plan session by querying the plan state table for the PLAN record
    // that has plan_id = job.runId.
    // Convention: the session_id used for plan state is stored in job.sessionId.
    const sessionId = job.sessionId;
    if (!sessionId) continue;

    // Find which node this gateway job belongs to by matching the job_id
    // attribute stored on the plan state node record when the Task Agent marked
    // it as "running". This is more reliable than status-only matching because
    // it survives race conditions where a node's status was prematurely changed.
    // Fallback: if no node has a matching job_id attribute, fall back to the
    // first node with "running" status (legacy behavior for older plan runs).
    try {
      // The job's own plan first (`<runId>#<node>` rows); a plan written before the qualified
      // keys has none, so fall back to the whole session's (bare) rows.
      let rows = await queryPlanState(client, table, sessionId, job.runId);
      if (!rows.length) rows = await queryPlanState(client, table, sessionId);
      const match = matchPlanNode(rows, { jobId: job.jobId, runId: job.runId, nodeId: job.planNodeId });
      if (!match) {
        console.log(`[bridge] No matching node found for session ${sessionId} (job ${job.jobId})`);
        continue;
      }
      const targetNodeId = match.nodeKey;

      // Mark the node as results_ready in Plan State — the async job finished
      // but results haven't been fetched yet. The Plan Chainer will dispatch
      // a results-fetch agent when it sees this state.
      //
      // The job id (and the queue endpoint) are written WITH the status: the chainer's results
      // fetch reads them from this row, and a node still running has not written them yet. Without
      // them the fetch failed "missing job_id or gateway registry" (session c3447913). A row
      // already completed/skipped is left alone, so a late event cannot reopen a finished node.
      const now = new Date().toISOString();
      const terminalStatus = job.status === "SUCCEEDED" ? "results_ready" : "failed";
      // A failed job's REAL reason travels with the failure. Without it the node row had no `error`,
      // the plan aggregate said only "node failed", and the orchestrator invented a cause: staging
      // plan-shotdiar-render, where M2C reported "No VAD turns found" (no speech) and the agent told
      // the user the 5 s clip was "too short to diarize".
      const failureReason = terminalStatus === "failed" ? jobFailureReason(job.lastError, job.status) : "";
      try {
        await client.send(new UpdateItemCommand({
          TableName: table,
          Key: {
            session_id: { S: sessionId },
            node_id: { S: targetNodeId },
          },
          UpdateExpression:
            "SET #st = :status, updated_at = :now, #ttl = :ttl, gateway_status = :gs, job_id = :jid" +
            (job.endpointId ? ", endpoint_id = :ep" : "") +
            (failureReason ? ", #err = :err" : ""),
          ConditionExpression: "attribute_exists(node_id) AND NOT (#st IN (:done, :skipped))",
          ExpressionAttributeNames: {
            "#st": "status", "#ttl": "ttl", ...(failureReason ? { "#err": "error" } : {}),
          },
          ExpressionAttributeValues: {
            ...(failureReason ? { ":err": { S: failureReason } } : {}),
            ":status": { S: terminalStatus },
            ":now": { S: now },
            ":ttl": { N: String(Math.floor(Date.now() / 1000) + 86400) },
            ":gs": { S: job.status },  // Store the raw gateway status (SUCCEEDED/FAILED)
            ":jid": { S: job.jobId },
            ":done": { S: "completed" },
            ":skipped": { S: "skipped" },
            ...(job.endpointId ? { ":ep": { S: job.endpointId } } : {}),
          },
        }));
      } catch (e: any) {
        if (e?.name === "ConditionalCheckFailedException") {
          console.log(`[bridge] Node ${targetNodeId} already finished; job ${job.jobId} not applied`);
          // The chainer may have finished this node itself (it used the SFN output as the result)
          // and then DEFERRED the plan's completion because this very job was still running (F2):
          // it relies on this bridge to re-fire it once the job settles. Not applying the result
          // must still re-fire it, or the plan stays pending forever (staging 2026-10-09,
          // plan-shotspeaker01). Touch the finished row WITHOUT changing its status: the MODIFY
          // re-runs the chainer, which now sees the job terminal and settles the plan.
          await rearmFinishedNode(client, table, sessionId, targetNodeId, job.jobId, now);
          continue;
        }
        throw e;
      }

      console.log(
        `[bridge] Marked node ${targetNodeId} as ${terminalStatus} ` +
        `for plan ${job.runId} (triggered by gateway job ${job.jobId.slice(-20)}) [matched by ${match.by}]`
      );

      // Also update the Job Registry node entry for Activity panel.
      // Don't terminal it yet — the results-fetch agent will do that when it
      // writes "completed". Just update the status text for UI visibility.
      const jobRegistryTable = process.env.JOB_REGISTRY_TABLE;
      if (jobRegistryTable && terminalStatus === "failed") {
        // Only terminal the job registry entry on failure (no results to fetch)
        try {
          await client.send(new UpdateItemCommand({
            TableName: jobRegistryTable,
            Key: { jobId: { S: `${job.runId}#${nodeIdOfKey(targetNodeId)}` } },
            // Stamp runId + nodeId so the emitted SSE frame is self-describing:
            // consumers suppress plan members by runId/nodeId, and this write can
            // CREATE the row (UpdateItem upserts) — without these it would emit a
            // terminal frame lacking both, raising a premature per-node banner.
            UpdateExpression: "SET #st = :s, terminalAt = :t, updatedAt = :now, runId = :rid, nodeId = :nid" +
              (failureReason ? ", lastError = :err" : "") + " REMOVE pollPartition",
            ExpressionAttributeNames: { "#st": "status" },
            ExpressionAttributeValues: {
              ...(failureReason ? { ":err": { S: failureReason } } : {}),
              ":s": { S: "FAILED" },
              ":t": { S: now },
              ":now": { S: now },
              ":rid": { S: job.runId! },
              ":nid": { S: nodeIdOfKey(targetNodeId) },
            },
          }));
        } catch (e) {
          console.warn(`[bridge] Job registry update failed for ${targetNodeId}:`, e);
        }
      }
    } catch (err) {
      console.error(`[bridge] Failed to process plan state update for job ${job.jobId}:`, err);
    }
  }
}

// ---------------------------------------------------------------------------
// Lambda entry point
// ---------------------------------------------------------------------------

export const handler = async (
  event: DynamoDBStreamEvent,
): Promise<void> => {
  const queueUrl = process.env[QUEUE_URL_ENV];
  if (!queueUrl) {
    throw new Error(`${QUEUE_URL_ENV} is not set`);
  }

  const frames: ProjectedFrame[] = [];
  for (const record of event.Records ?? []) {
    const frame = classify(record);
    if (frame) {
      frames.push(frame);
    }
  }
  if (frames.length === 0) {
    return;
  }

  for (let i = 0; i < frames.length; i += SQS_BATCH_SIZE) {
    const slice = frames.slice(i, i + SQS_BATCH_SIZE);
    const entries: SendMessageBatchRequestEntry[] = slice.map((f, idx) => ({
      Id: String(idx),
      MessageBody: JSON.stringify(f.payload),
      MessageGroupId: f.groupId,
      MessageDeduplicationId: f.dedupId,
    }));
    const result = await sqs.send(
      new SendMessageBatchCommand({
        QueueUrl: queueUrl,
        Entries: entries,
      }),
    );
    if (result.Failed && result.Failed.length > 0) {
      // Surface failures to Lambda. The DynamoDB Streams event source
      // mapping (configured in task 9.2) handles retry; partial-batch
      // reporting can be wired there if needed.
      const ids = result.Failed.map((f) => f.Id ?? "?").join(",");
      throw new Error(
        `SQS SendMessageBatch reported failed entries: ${ids}`,
      );
    }
  }

  // Bridge: propagate terminal plan-node job completions to the Plan State
  // table so the Plan Chainer Lambda can react.
  await bridgeToPlanState(frames);
};
