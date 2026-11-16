/**
 * #1 — Job-result fetch/focus decision (pure, unit-tested).
 *
 * Clicking a job-completion notification (or a job chip) should NOT blindly re-run an agent turn
 * every time. The decision, addressed by job id:
 *
 *   - FOCUS   — a render tab for this job already exists → just focus it. This is the only
 *               "success cache": we focus-instead-of-fetch ONLY when a real, rendered tab exists,
 *               so a failed/empty fetch is never mistaken for done.
 *   - SUPPRESS— a fetch for this job was dispatched very recently (in-flight window) → drop the
 *               repeat so rapid/double clicks don't fan out into duplicate turns/tabs.
 *   - FETCH   — first click, OR a retry after the in-flight window elapsed. Because a FAILED or
 *               empty fetch leaves NO tab, a later click naturally re-fetches — that is the
 *               recovery path. (The Jobs-panel "Results" button is an always-on manual re-fetch.)
 *
 * Keeping this pure means the failure/recovery edge cases are testable without React/AWS.
 */
export type ResultsAction =
  | { kind: 'focus'; index: number }
  | { kind: 'suppress' }
  | { kind: 'fetch' };

export interface ResultsDecisionInput {
  /** The completed job's id. */
  jobId: string;
  /**
   * Render tabs currently loaded for the job's session (each may carry `jobId`). Empty when the
   * job's session isn't the active one yet (tabs load async after navigation) — in that case no
   * tab matches and we fall through to the fetch/suppress logic, which is correct: the server
   * render list surfaces the tab after the switch and a later click then focuses it.
   */
  renderHistory: ReadonlyArray<{ jobId?: string }>;
  /** ms timestamp of the last fetch we dispatched for THIS job (undefined = never). */
  lastFetchAt?: number;
  /** now (ms). */
  now: number;
  /** How long after a dispatch to treat a fetch as still in flight. Default 4000ms. */
  inFlightWindowMs?: number;
}

export function decideResultsAction(input: ResultsDecisionInput): ResultsAction {
  const { jobId, renderHistory, lastFetchAt, now } = input;
  const window = input.inFlightWindowMs ?? 4000;

  // Ready → focus the existing tab. Never re-fetch a job whose result is already rendered.
  const index = renderHistory.findIndex((e) => !!e.jobId && e.jobId === jobId);
  if (index >= 0) return { kind: 'focus', index };

  // No tab yet. Dedup a burst of clicks while the first fetch is plausibly still running…
  if (lastFetchAt !== undefined && now - lastFetchAt < window) return { kind: 'suppress' };

  // …otherwise fetch. This one branch is both the FIRST fetch and the RETRY after a failed/empty
  // fetch (which left no tab and whose in-flight window has since elapsed).
  return { kind: 'fetch' };
}


/**
 * #2 — the turn to fire when a FAILED job notification is opened.
 *
 * Opening a failed-job notification used to do nothing (no `failure` branch), so the reason and
 * job id never surfaced. This hands the agent the same "why did this fail" request that works
 * when a user asks by hand — get_job → explain → (recover) — so the reason + id come back and a
 * recoverable failure gets retried. Pure so the wording is unit-testable without React.
 */
export function failedNotificationTurn(
  jobId: string,
  label?: string,
): { prompt: string; displayText: string } {
  return {
    prompt: `Can you check why this job failed: ${jobId}. Tell me the reason, and if it is `
      + `recoverable, correct it and retry.`,
    displayText: `Why did ${label || 'the job'} fail?`,
  };
}

/**
 * The text-only "summarize what just finished" turn for a completed job whose deliverable is
 * already rendered in the panel.
 *
 * The rendered result lives only in the UI — it is NOT in the agent's context — so the agent must
 * read it once. The earlier wording ("already resolved — do NOT re-fetch … just read the resolved
 * result") forbade the only way to read it: staging session 9936cbc3 refused to summarize a
 * SUCCEEDED plan because it had no result in context and was told not to fetch. Permit one cheap
 * read of the STORED result (get_plan_results for a plan, get_job for a job — neither regenerates
 * anything) and keep forbidding the expensive parts (presign, re-render, re-feature).
 */
export function summaryNotificationTurn(
  jobId: string,
  label?: string,
): { prompt: string; displayText: string; turnPurpose: 'results_summary' } {
  const name = label || 'completed';
  return {
    prompt: `Briefly summarize the results of the ${name} job (id ${jobId}). The panel shows its `
      + `artifacts by itself (on the file's own tab), but the result is not in this conversation, so read the `
      + `stored result ONCE: call get_plan_results("${jobId}") if it is a plan, otherwise `
      + `get_job("${jobId}"). That only reads the cached result. Do NOT presign, re-render, or `
      + `re-feature the artifacts — just give a short summary.`,
    displayText: `Summarize the ${label || 'job'} results`,
    // Enforced, not just asked: the agent withholds its gallery / clip / frame tools for this turn (main.py), so a
    // summary can never open a second tab of results the panel already shows.
    turnPurpose: 'results_summary',
  };
}
