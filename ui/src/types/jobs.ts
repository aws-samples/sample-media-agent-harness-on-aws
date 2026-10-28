/**
 * Canonical TypeScript types for the async-job-tracking feature.
 *
 * Imported by both the React app under `ui/src/` and the Vite middleware
 * proxy at `ui/agentcore-proxy.ts`. Both sides talk about the same Job
 * records — the proxy projects DynamoDB rows from the JobRegistry table
 * onto `GET /jobs` / `GET /jobs/:id`, and emits `JobUpdateEvent` frames
 * over `/jobs/stream` (SSE).
 *
 * Field names mirror the DynamoDB attributes defined in the design
 * (.kiro/specs/async-job-tracking/design.md, "Job_Registry" section)
 * and the SSE wire format under "Push_Channel". camelCase throughout.
 *
 * Import paths:
 *   from `ui/src/...`            →  `./types/jobs` / `../types/jobs`
 *   from `ui/agentcore-proxy.ts` →  `./src/types/jobs`
 */

/**
 * Canonical job status set. Matches the `Literal[...]` type on the
 * Python `JobRecord.status` field and the DDB `status` attribute.
 *
 * - `PENDING` / `RUNNING` are active statuses; jobs in these states are
 *   polled by the Polling_Worker.
 * - `SUCCEEDED` / `FAILED` / `TIMED_OUT` / `ABORTED` are terminal; the
 *   registry stops polling and stamps `terminalAt` + `ttl`.
 */
export type JobStatus =
  | "PENDING"
  | "RUNNING"
  | "SUCCEEDED"
  | "FAILED"
  | "TIMED_OUT"
  | "ABORTED";

/**
 * An Async_Job record as projected by the agentcore-proxy onto
 * `GET /jobs` and `GET /jobs/:id`, and as carried in the `data` payload
 * of every `/jobs/stream` SSE frame.
 *
 * Mirrors the DDB attribute shape written by `JobRegistry.register` /
 * `set_active_status` / `set_terminal`. While a job is active,
 * `terminalAt` and `lastError` are `null`; on terminal transition the
 * registry stamps `terminalAt` (and `lastError` when the terminal
 * status is non-success).
 */
export interface Job {
  /** Job identifier (e.g. an SFN execution ARN) — DDB partition key. */
  jobId: string;

  /** Authenticated actor that spawned the job (Cognito `sub`). */
  actorId: string;

  /**
   * Chat session that spawned the job. Informational only — the
   * sidebar does NOT filter by sessionId; the inline chip does.
   */
  sessionId: string;

  /**
   * Job_Trio identifier — the group key shared by the start, status,
   * and results tools (see Job_Detector).
   */
  trioId: string;

  /**
   * Gateway hosting the trio. The sentinel `"__aws_sfn__"` marks the
   * SFN DescribeExecution direct-API path used by the polling worker.
   */
  gatewayId: string;

  /** Current job status. */
  status: JobStatus;

  /** ISO 8601 timestamp of registration. */
  startAt: string;

  /** ISO 8601 timestamp of the most recent status update. */
  updatedAt: string;

  /**
   * ISO 8601 timestamp of the terminal transition. `null` while the
   * job is still active.
   */
  terminalAt: string | null;

  /**
   * True when the polling worker found at least one user-facing artifact in
   * the completed status payload. Drives automatic rendering without any
   * gateway or tool-name allowlist.
   */
  hasRenderableArtifacts: boolean;

  /**
   * Most recent non-transient error message recorded by the polling
   * worker. `null` when no error has been recorded.
   */
  lastError: string | null;

  /**
   * Parent run identifier. When a job was spawned as part of a
   * plan_and_execute DAG, this contains the plan_id so the Activity
   * Panel can group child jobs under their parent run. `null` for
   * standalone jobs dispatched outside a plan.
   */
  runId: string | null;
  /**
   * The user request that caused this job. Groups everything one request
   * dispatched — including standalone jobs, which have no `runId` because
   * that identifies a plan execution. Null on rows written before it
   * existed; callers must fall back rather than assume presence.
   */
  turnId?: string | null;

  /**
   * DAG node id (`DAGNode.id`) this job represents, when it was spawned
   * as part of a `plan_and_execute` DAG. `null` for the parent "plan"
   * record and for standalone (non-plan) jobs. Lets the Activity Panel
   * identify which DAG vertex a job corresponds to.
   */
  nodeId: string | null;

  /**
   * DAG edges: node ids this job's node depends on (`DAGNode.
   * depends_on`), translated into their sibling job ids' `nodeId`
   * values by the client. Empty for the parent "plan" record and for
   * standalone jobs. Drives the dependency-graph rendering in the
   * Activity Panel — without this, a run's jobs are just a
   * timestamp-ordered list disconnected from the actual execution DAG.
   */
  dependsOn: string[];

  /**
   * Concrete renderable output URL(s) for a completed job, extracted by the
   * polling worker from the terminal status payload (empty until then / for
   * non-renderable jobs). Lets the client render a finished job deterministically
   * — POST the first URL to `/render` (Render Agent) — with no orchestrator LLM
   * turn. Powers the in-chat JobCard's "View results" action and the bell.
   * Optional on the type (older records / in-flight fixtures omit it); the
   * `/jobs` + SSE projections always supply `[]` when absent.
   */
  artifactUrls?: string[];

  /**
   * The media a COMPLETED plan node made (`node|…` rows): recorded by the plan chainer from the node's stored
   * result on its transition to completed (`projects.lineageEdges`), for any node type — including work that ran
   * inside the node's own turn and registered no job (an ffmpeg assemble). Absent until recorded.
   */
  outputs?: JobOutput[];
}

/** One file a plan node made: canonical `s3://` uri, its key, and the kind of media. */
export interface JobOutput {
  uri: string;
  key: string;
  kind: 'video' | 'image' | 'audio';
}

/**
 * Wire-format envelope for the SSE frames emitted by `/jobs/stream`.
 *
 * Each registry mutation produces exactly one frame:
 *
 *     event: job.update
 *     data:  {"jobId": "...", "status": "RUNNING", ...}
 *
 *     event: job.terminal
 *     data:  {"jobId": "...", "status": "SUCCEEDED", "terminalAt": "...", ...}
 *
 * `job.update` covers active→active transitions; `job.terminal` covers
 * any transition into a terminal status.
 */
export interface JobUpdateEvent {
  /** SSE event name. */
  event: "job.update" | "job.terminal";

  /** Full Job record after the mutation. */
  data: Job;
}
