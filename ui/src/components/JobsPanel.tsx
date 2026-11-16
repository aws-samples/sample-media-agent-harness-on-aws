import React, { forwardRef, useImperativeHandle, useRef, useEffect } from 'react';
import { theme } from '../styles/tokens';
import { useJobs, loadSession } from '../hooks/useJobs';
import { formatElapsed, isActive, statusLabel } from '../utils/jobStatus';
import { labelForTrio } from '../services/notifications';
import { summaryNotificationTurn } from '../services/resultsFetch';
import type { Job, JobStatus } from '../types/jobs';

/**
 * Imperative handle so the inline chat `JobReference` chip can scroll a
 * specific job into view and flash it — the same contract the sidebar
 * used to expose, now that Jobs lives in the right panel.
 */
export interface JobsPanelHandle {
  scrollToJob(jobId: string): void;
}

interface JobsPanelProps {
  /**
   * The session currently on screen. The panel shows only jobs spawned by
   * this session; the shared `useJobs` store stays actor-wide so background
   * auto-render (which must see other sessions' terminal frames) keeps working.
   */
  activeSessionId: string;
  /**
   * Jump to a job's flow in the Workflow (Activity) view. Given the run/job id
   * to focus; the caller switches to the Activity tab and expands that flow
   * (reusing ActivityPanel's `focusRunId` path). Optional so tests may omit it.
   */
  onOpenFlow?: (runId: string) => void;
}

/**
 * Filter the actor-wide job list down to a single session — pure, for tests.
 *
 * An empty `activeSessionId` yields no rows rather than the whole actor list,
 * so a transient empty session id can never leak every session's jobs.
 */
export function jobsForSession(jobs: readonly Job[], activeSessionId: string): Job[] {
  if (!activeSessionId) return [];
  return jobs.filter((job) => job.sessionId === activeSessionId);
}

/**
 * A consolidated top-level row for the Jobs side panel.
 *
 * The side panel shows ONE row per user-initiated action — a plan run or a
 * standalone dispatch — never the individual DAG nodes or their underlying
 * gateway/SFN jobs (those are still tracked in the store and shown in full
 * in the Workflow/Activity view). For a plan, `nodeDone/nodeTotal` +
 * `aggregateStatus` summarize the run so one glance answers "is it done?".
 */
export interface RunRow {
  /** The top-level job: a plan parent, or a standalone (non-plan) job. */
  job: Job;
  /** True when this row represents a multi-node plan run. */
  isPlan: boolean;
  /** Node-level children counts (only meaningful when `isPlan`). */
  nodeTotal: number;
  nodeDone: number;
  nodeFailed: number;
  /** A running plan's current step (its first active node's workflow), shown instead of a bare "Running". */
  runningStep?: string;
  /** Status to display for the row (derived from children for a plan). */
  aggregateStatus: JobStatus;
}

function isPlanParent(j: Job): boolean {
  return j.trioId.startsWith('plan|') || (j.runId != null && j.runId === j.jobId);
}
function isNodeVirtual(j: Job): boolean {
  return j.trioId.startsWith('node|');
}
function isRunChild(j: Job): boolean {
  // A node virtual-job or an underlying gateway/SFN job that belongs to a
  // plan run (runId points at the plan, and it isn't the plan record itself).
  return j.runId != null && j.runId !== j.jobId && !isPlanParent(j);
}
function isTerminalFailureStatus(s: JobStatus): boolean {
  return s === 'FAILED' || s === 'TIMED_OUT' || s === 'ABORTED';
}

/**
 * Collapse a session's flat job list into top-level run rows: one row per
 * plan (children rolled up) plus each standalone job. Pure + exported for
 * unit coverage. Sorted newest-first by `updatedAt` (ISO-8601 sorts
 * lexicographically), matching the store's ordering.
 */
export function rollUpRuns(jobs: readonly Job[]): RunRow[] {
  const rows: RunRow[] = [];

  // Standalone (non-plan, non-child) jobs → one row each, as-is.
  for (const j of jobs) {
    if (isPlanParent(j) || isRunChild(j)) continue;
    rows.push({ job: j, isPlan: false, nodeTotal: 0, nodeDone: 0, nodeFailed: 0, aggregateStatus: j.status });
  }

  // Plan runs → one row each, children rolled up into an aggregate.
  for (const plan of jobs) {
    if (!isPlanParent(plan)) continue;
    const nodes = jobs.filter((j) => j.runId === plan.jobId && j.jobId !== plan.jobId && isNodeVirtual(j));
    const nodeTotal = nodes.length;
    const nodeDone = nodes.filter((n) => n.status === 'SUCCEEDED').length;
    const nodeFailed = nodes.filter((n) => isTerminalFailureStatus(n.status)).length;
    const anyNodeActive = nodes.some((n) => n.status === 'PENDING' || n.status === 'RUNNING');
    let aggregateStatus: JobStatus;
    if (nodeTotal === 0) aggregateStatus = plan.status;         // no nodes yet → plan's own status
    else if (anyNodeActive) aggregateStatus = 'RUNNING';
    else if (nodeFailed > 0) aggregateStatus = 'FAILED';
    else if (nodeDone === nodeTotal) aggregateStatus = 'SUCCEEDED';
    else aggregateStatus = plan.status;
    const runningNode = nodes.find((n) => n.status === 'PENDING' || n.status === 'RUNNING');
    rows.push({ job: plan, isPlan: true, nodeTotal, nodeDone, nodeFailed, aggregateStatus,
      ...(runningNode ? { runningStep: labelForTrio(runningNode.trioId) } : {}) });
  }

  rows.sort((a, b) => {
    const timeA = a.job.startAt || a.job.updatedAt;
    const timeB = b.job.startAt || b.job.updatedAt;
    if (timeA !== timeB) {
      return timeB.localeCompare(timeA);
    }
    return b.job.updatedAt.localeCompare(a.job.updatedAt);
  });
  return rows;
}

/**
 * Async-job list rendered in the right panel's "Jobs" tab. Reads from
 * the shared `useJobs` store (seeded by `GET /jobs`, kept live by the
 * `/jobs/stream` SSE subscription mounted once at the app root), then
 * scopes the view to the active session at the presentation layer.
 */
const JobsPanel = forwardRef<JobsPanelHandle, JobsPanelProps>(function JobsPanel({ activeSessionId, onOpenFlow }, ref) {
  // Infinity (no recency window) is deliberate: this panel is SESSION-scoped
  // via jobsForSession() below, so the 24h JOBS_RECENCY_WINDOW_HOURS default is
  // the wrong bound here — it would hide an opened session's own terminal jobs
  // once they age past 24h, leaving the panel empty for any session older than a
  // day (even after loadSession() fetched them into the store). Opening a session
  // should show ALL of its jobs regardless of age; session scope is the bound.
  // The finite window is only appropriate for a global/actor-wide list.
  const { jobs } = useJobs(Infinity);
  // Load THIS session's jobs on open. The actor-wide seed only holds the newest
  // ~50 runs, so a previously-viewed session's jobs may not be in the store; this
  // session-scoped fetch merges them in so the panel isn't empty for old sessions.
  useEffect(() => {
    if (activeSessionId) void loadSession(activeSessionId);
  }, [activeSessionId]);
  const sessionJobs = jobsForSession(jobs, activeSessionId);
  const runRows = rollUpRuns(sessionJobs);
  const now = Date.now();
  // Re-render once a second while anything runs, so its elapsed time ticks.
  const anyActive = sessionJobs.some((j) => isActive(j.status));
  const [, setTick] = React.useState(0);
  useEffect(() => {
    if (!anyActive) return;
    const t = window.setInterval(() => setTick((n) => n + 1), 1000);
    return () => window.clearInterval(t);
  }, [anyActive]);
  const containerRef = useRef<HTMLDivElement>(null);

  useImperativeHandle(ref, () => ({
    scrollToJob(jobId: string): void {
      if (typeof document === 'undefined' || !jobId) return;
      const escaped = (typeof CSS !== 'undefined' && typeof CSS.escape === 'function')
        ? CSS.escape(jobId)
        : jobId.replace(/"/g, '\\"');
      const root: ParentNode = containerRef.current ?? document;
      const row = root.querySelector<HTMLElement>(`[data-job-id="${escaped}"]`);
      if (!row) return;
      row.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      row.classList.add('job-row-highlight');
      window.setTimeout(() => row.classList.remove('job-row-highlight'), 1500);
    },
  }), []);

  const handleCopyJobId = (jobId: string): void => {
    if (typeof navigator === 'undefined' || !navigator.clipboard) return;
    navigator.clipboard.writeText(jobId).catch((err) => console.error('JobsPanel: copy job id failed:', err));
  };

  const handleGetResults = (job: Job): void => {
    // Push the fetch-results request straight into the chat as a live turn
    // (not just pre-fill the input), scoped to the job's own session.
    // The same results-summary turn as the bell: the render hook puts the outputs on the file's own tab, and the
    // agent may not open a gallery of them too ("feature the artifacts" did — a second tab of the same shots).
    // displayText keeps the raw jobId — an execution ARN embedding the account id, for standalone Step Functions
    // jobs — out of the visible/stored chat bubble; the agent still receives it via `prompt`.
    const { prompt, turnPurpose } = summaryNotificationTurn(job.jobId, labelForTrio(job.trioId));
    const displayText = `Fetch results for ${job.nodeId ?? 'this job'}`;
    window.dispatchEvent(new CustomEvent('chat:send', { detail: { prompt, displayText, turnPurpose, sessionId: job.sessionId } }));
  };

  return (
    <div ref={containerRef} style={containerStyle}>
      <style>{`
        [data-job-id].job-row-highlight { animation: jobs-panel-flash 1.5s ease; }
        @keyframes jobs-panel-flash {
          0% { background: ${theme.colors.activeBg}; }
          70% { background: ${theme.colors.activeBg}; }
          100% { background: transparent; }
        }
        @keyframes jobs-dot-pulse { 50% { opacity: 0.35; } }
      `}</style>
      {runRows.length === 0 ? (
        <div style={emptyStyle}>No jobs in this session yet</div>
      ) : (
        runRows.map(row => {
          const job = row.job;
          // The workflow's name ("Shot Segmentation"), as the notification says it — not the raw trio id.
          const label = row.isPlan ? planDisplayName(job.trioId) : labelForTrio(job.trioId);
          // A running plan says which step it is on and how long it has run (IND403: "Jobs only pulses").
          const meta = row.isPlan
            ? isActive(row.aggregateStatus)
              ? `${row.nodeDone}/${row.nodeTotal} · ${row.runningStep ?? statusLabel(row.aggregateStatus)} · ${formatElapsed(job.startAt, job.updatedAt, row.aggregateStatus)}`
              : `${row.nodeDone}/${row.nodeTotal} · ${statusLabel(row.aggregateStatus)} · ${formatRelativeTime(job.startAt, now)}`
            // Running: how long it has been going, ticking (a pulsing dot alone read as stuck). Done: when.
            : isActive(job.status)
              ? `${statusLabel(job.status)} · ${formatElapsed(job.startAt, job.updatedAt, job.status)}`
              : `${statusLabel(job.status)} · ${formatRelativeTime(job.startAt, now)}`;
          const dotColor = statusColor(row.aggregateStatus);
          return (
            <div key={job.jobId} data-job-id={job.jobId} data-job-row="" style={rowStyle} title={`${job.trioId} · ${row.aggregateStatus}`}>
              <span style={{ ...dotStyle, background: dotColor, ...(isActive(row.aggregateStatus) ? pulseStyle : null) }} aria-hidden="true" />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={trioStyle}>{label}</div>
                <div style={metaStyle}>{meta}</div>
              </div>
              <div data-row-actions="" style={actionsStyle}>
                {onOpenFlow && (
                  <button type="button" onClick={() => onOpenFlow(job.runId || job.jobId)} style={iconBtnStyle} title="Open this flow in the Workflow view" aria-label="Open flow in Workflow view"><FlowIcon /></button>
                )}
                {row.aggregateStatus === 'SUCCEEDED' && (
                  <button type="button" onClick={() => handleGetResults(job)} style={resultsIconBtnStyle} title="Fetch this job's results into chat" aria-label="View results"><ResultsIcon /></button>
                )}
                <button type="button" onClick={() => handleCopyJobId(job.jobId)} style={iconBtnStyle} title="Copy job ID" aria-label="Copy job ID"><CopyIcon /></button>
              </div>
            </div>
          );
        })
      )}
    </div>
  );
});

export default JobsPanel;


/** "plan|meridian-media-pipeline" -> "meridian-media-pipeline". */
function planDisplayName(trioId: string): string {
  return trioId.startsWith('plan|') ? trioId.slice('plan|'.length) : trioId;
}

function statusColor(s: JobStatus): string {
  switch (s) {
    case 'SUCCEEDED': return theme.colors.success;
    case 'FAILED':
    case 'TIMED_OUT':
    case 'ABORTED': return theme.colors.error;
    default: return theme.colors.accent; // PENDING / RUNNING
  }
}

function formatRelativeTime(iso: string, now: number): string {
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return iso;
  const diffSec = Math.floor((now - ms) / 1000);
  if (diffSec < 5) return 'now';
  if (diffSec < 60) return `${diffSec}s ago`;
  const min = Math.floor(diffSec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  return `${Math.floor(hr / 24)}d ago`;
}

const containerStyle: React.CSSProperties = { flex: 1, overflowY: 'auto', padding: 12 };
const emptyStyle: React.CSSProperties = { padding: '24px 16px', textAlign: 'center', color: theme.colors.textSecondary, fontSize: theme.typography.sizes.body };
const rowStyle: React.CSSProperties = { position: 'relative', display: 'flex', alignItems: 'center', gap: 10, padding: '12px 14px', border: `1px solid ${theme.colors.border}`, borderRadius: theme.radius.panel, marginBottom: 10, fontSize: 'var(--fs-md)' };
const dotStyle: React.CSSProperties = { width: 9, height: 9, borderRadius: '50%', flexShrink: 0 };
const pulseStyle: React.CSSProperties = { animation: 'jobs-dot-pulse 1.3s infinite' };
const trioStyle: React.CSSProperties = { color: theme.colors.textPrimary, fontWeight: theme.typography.weights.semibold, fontSize: 'var(--fs-md)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' };
const metaStyle: React.CSSProperties = { color: theme.colors.textSecondary, fontSize: 'var(--fs-xs)', marginTop: 3 };
const actionsStyle: React.CSSProperties = { display: 'flex', gap: 6, alignItems: 'center', flexShrink: 0 };
const iconBtnStyle: React.CSSProperties = { width: 26, height: 26, borderRadius: theme.radius.button, border: 'none', background: 'none', color: theme.colors.textSecondary, cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center' };
// Always-visible, labelled "Results" action so a user who missed the completion
// notification can pull a job's output into chat — obvious even with several
// jobs in the list. Primary-blue to match link/download affordances.
// Copy and Results are the SAME icon-button shape/size and both always visible
// (no hover-gate) — consistent per feedback. Results is tinted primary so it
// still reads as the main action; Copy stays secondary-grey.
const resultsIconBtnStyle: React.CSSProperties = { ...iconBtnStyle, color: theme.colors.primary };

function CopyIcon() {
  return (<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><rect x="4" y="4" width="8" height="8" rx="1.5" /><path d="M2 10V3a1 1 0 0 1 1-1h7" /></svg>);
}
function ResultsIcon() {
  return (<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M7 2v7M4 6.5L7 9.5l3-3" /><path d="M2.5 11h9" /></svg>);
}
function FlowIcon() {
  // A mini DAG: one node branching to two — signals "open this flow".
  return (<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><circle cx="3" cy="7" r="1.6" /><circle cx="11" cy="3.5" r="1.6" /><circle cx="11" cy="10.5" r="1.6" /><path d="M4.5 6.2 9.4 4.1M4.5 7.8 9.4 9.9" /></svg>);
}
