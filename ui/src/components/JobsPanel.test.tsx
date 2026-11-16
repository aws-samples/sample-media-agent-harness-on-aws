// @vitest-environment jsdom
import React from 'react';
import { cleanup, render, screen, fireEvent } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Job } from '../types/jobs';

// Controlled jobs store — JobsPanel reads the actor-wide list via useJobs();
// the panel is responsible for narrowing it to the active session.
const jobsMock = vi.fn((_windowHours?: number) => ({ jobs: [] as Job[] }));
vi.mock('../hooks/useJobs', () => ({ useJobs: (w?: number) => jobsMock(w), loadSession: () => Promise.resolve() }));

import JobsPanel, { jobsForSession, rollUpRuns } from './JobsPanel';

function job(overrides: Partial<Job>): Job {
  return {
    jobId: 'arn:job:' + Math.random().toString(36).slice(2),
    actorId: 'actor-1',
    sessionId: 'session-A',
    trioId: 'face-recognition|recognize_faces',
    gatewayId: '__aws_sfn__',
    status: 'SUCCEEDED',
    startAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
    terminalAt: new Date().toISOString(),
    hasRenderableArtifacts: true,
    lastError: null,
    runId: null,
    nodeId: null,
    dependsOn: [],
    ...overrides,
  };
}

describe('jobsForSession', () => {
  const jobs = [
    job({ jobId: 'a', sessionId: 'session-A' }),
    job({ jobId: 'b', sessionId: 'session-B' }),
    job({ jobId: 'c', sessionId: 'session-A' }),
    job({ jobId: 'd', sessionId: '' }),
  ];

  it('returns only jobs from the active session', () => {
    expect(jobsForSession(jobs, 'session-A').map(j => j.jobId)).toEqual(['a', 'c']);
  });

  it('excludes other sessions entirely', () => {
    expect(jobsForSession(jobs, 'session-B').map(j => j.jobId)).toEqual(['b']);
  });

  it('never leaks the actor-wide list when the active session id is empty', () => {
    expect(jobsForSession(jobs, '')).toEqual([]);
  });

  it('does not match jobs with an empty sessionId to a real session', () => {
    expect(jobsForSession(jobs, 'session-A').some(j => j.jobId === 'd')).toBe(false);
  });
});

describe('JobsPanel (session-scoped presentation)', () => {
  afterEach(() => { cleanup(); jobsMock.mockReset(); });

  it('renders only the active session\'s jobs even though the store is actor-wide', () => {
    jobsMock.mockReturnValue({ jobs: [
      job({ jobId: 'mine-1', sessionId: 'session-A', trioId: 'face-recognition|recognize_faces' }),
      job({ jobId: 'other-1', sessionId: 'session-B', trioId: 'audio-stem|separate_audio_stems' }),
      job({ jobId: 'mine-2', sessionId: 'session-A', trioId: 'speech-diarization|diarize_speech' }),
    ] });

    render(<JobsPanel activeSessionId="session-A" />);

    expect(document.querySelector('[data-job-id="mine-1"]')).toBeTruthy();
    expect(document.querySelector('[data-job-id="mine-2"]')).toBeTruthy();
    expect(document.querySelector('[data-job-id="other-1"]')).toBeNull();
    // The cross-session "open spawning session" affordance is gone — every
    // visible row already belongs to the active session.
    expect(screen.queryByLabelText('Open spawning session')).toBeNull();
  });

  it('shows a session-scoped empty state when this session has no jobs', () => {
    jobsMock.mockReturnValue({ jobs: [job({ jobId: 'other', sessionId: 'session-B' })] });
    render(<JobsPanel activeSessionId="session-A" />);
    expect(screen.getByText(/No jobs in this session/i)).toBeTruthy();
    expect(document.querySelector('[data-job-id]')).toBeNull();
  });

  // Regression: opening a session older than JOBS_RECENCY_WINDOW_HOURS (24h) left
  // the panel empty because JobsPanel used the default finite window, so its own
  // terminal jobs (already fetched by loadSession) were age-filtered out. The
  // panel is session-scoped, so it must request the UN-windowed set (Infinity)
  // and let jobsForSession() do the bounding.
  it("requests the un-windowed job set so an old session's jobs are not age-filtered", () => {
    jobsMock.mockReturnValue({ jobs: [] });
    render(<JobsPanel activeSessionId="session-A" />);
    expect(jobsMock).toHaveBeenCalledWith(Infinity);
    expect(jobsMock).not.toHaveBeenCalledWith(undefined);
  });

  it("renders an active session's terminal jobs even when they are days old", () => {
    const old = new Date(Date.now() - 3 * 24 * 60 * 60 * 1000).toISOString();
    jobsMock.mockReturnValue({ jobs: [
      job({ jobId: 'old-1', sessionId: 'session-A', status: 'SUCCEEDED', terminalAt: old, updatedAt: old }),
    ] });
    render(<JobsPanel activeSessionId="session-A" />);
    expect(document.querySelector('[data-job-id="old-1"]')).toBeTruthy();
  });
});

describe('rollUpRuns (side-panel consolidation)', () => {
  it('collapses a plan run into one top-level row and hides its node/SFN children', () => {
    const jobs = [
      job({ jobId: 'p1', trioId: 'plan|meridian-media-pipeline', runId: 'p1', nodeId: null, status: 'RUNNING' }),
      job({ jobId: 'p1#n1', trioId: 'node|sg1_trim', runId: 'p1', nodeId: 'sg1_trim', status: 'SUCCEEDED' }),
      job({ jobId: 'p1#n2', trioId: 'node|sg2_stem', runId: 'p1', nodeId: 'sg2_stem', status: 'RUNNING' }),
      job({ jobId: 'arn:sfn:1', trioId: 'media-processing|process_media', runId: 'p1', nodeId: null, status: 'SUCCEEDED' }),
      job({ jobId: 's1', trioId: 'face-recognition|recognize_faces', runId: null, nodeId: null, status: 'SUCCEEDED' }),
    ];
    const rows = rollUpRuns(jobs);
    const ids = rows.map(r => r.job.jobId).sort();
    // Only the plan parent + the standalone job are top-level.
    expect(ids).toEqual(['p1', 's1']);
    const plan = rows.find(r => r.job.jobId === 'p1')!;
    expect(plan.isPlan).toBe(true);
    expect(plan.nodeTotal).toBe(2);
    expect(plan.nodeDone).toBe(1);
    expect(plan.aggregateStatus).toBe('RUNNING'); // a node is still running
    // IND403 "Jobs only pulses": a running plan names the step it is on.
    expect(plan.runningStep).toBeTruthy();
    expect(plan.runningStep).not.toBe('Running');
    const standalone = rows.find(r => r.job.jobId === 's1')!;
    expect(standalone.isPlan).toBe(false);
    expect(standalone.aggregateStatus).toBe('SUCCEEDED');
  });

  it('aggregates a plan to SUCCEEDED only when every node succeeded', () => {
    const rows = rollUpRuns([
      job({ jobId: 'p2', trioId: 'plan|done-run', runId: 'p2', nodeId: null, status: 'RUNNING' }),
      job({ jobId: 'p2#a', trioId: 'node|a', runId: 'p2', nodeId: 'a', status: 'SUCCEEDED' }),
      job({ jobId: 'p2#b', trioId: 'node|b', runId: 'p2', nodeId: 'b', status: 'SUCCEEDED' }),
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0].aggregateStatus).toBe('SUCCEEDED');
    expect(rows[0].nodeDone).toBe(2);
    expect(rows[0].nodeTotal).toBe(2);
  });

  it('aggregates a plan to FAILED when a node failed and none are active', () => {
    const rows = rollUpRuns([
      job({ jobId: 'p3', trioId: 'plan|bad-run', runId: 'p3', nodeId: null, status: 'RUNNING' }),
      job({ jobId: 'p3#a', trioId: 'node|a', runId: 'p3', nodeId: 'a', status: 'SUCCEEDED' }),
      job({ jobId: 'p3#b', trioId: 'node|b', runId: 'p3', nodeId: 'b', status: 'FAILED', terminalAt: new Date().toISOString() }),
    ]);
    expect(rows[0].aggregateStatus).toBe('FAILED');
    expect(rows[0].nodeFailed).toBe(1);
  });
});

describe('JobsPanel (consolidated run rendering)', () => {
  afterEach(() => { cleanup(); jobsMock.mockReset(); });

  it('renders ONE top-level row for a plan run (nodes + SFN jobs rolled up)', () => {
    jobsMock.mockReturnValue({ jobs: [
      job({ jobId: 'p1', sessionId: 'session-A', trioId: 'plan|meridian-media-pipeline', runId: 'p1', nodeId: null, status: 'RUNNING' }),
      job({ jobId: 'p1#n1', sessionId: 'session-A', trioId: 'node|sg1_trim', runId: 'p1', nodeId: 'sg1_trim', status: 'SUCCEEDED' }),
      job({ jobId: 'p1#n2', sessionId: 'session-A', trioId: 'node|sg2_stem', runId: 'p1', nodeId: 'sg2_stem', status: 'RUNNING' }),
      job({ jobId: 'arn:sfn:x', sessionId: 'session-A', trioId: 'media-processing|process_media', runId: 'p1', nodeId: null, status: 'SUCCEEDED' }),
    ] });

    render(<JobsPanel activeSessionId="session-A" />);

    // Exactly one visible row — the plan — not the 4 raw records.
    const rows = document.querySelectorAll('[data-job-id]');
    expect(rows.length).toBe(1);
    expect(document.querySelector('[data-job-id="p1"]')).toBeTruthy();
    expect(document.querySelector('[data-job-id="p1#n1"]')).toBeNull();
    expect(document.querySelector('[data-job-id="arn:sfn:x"]')).toBeNull();
    // Shows the plan name + aggregate progress (1 of 2 nodes done, still running).
    expect(screen.getByText('meridian-media-pipeline')).toBeTruthy();
    expect(screen.getByText(/1\/2/)).toBeTruthy();
  });
});


describe('JobsPanel open-flow quick-link', () => {
  afterEach(() => { cleanup(); jobsMock.mockReset(); });

  const LABEL = 'Open flow in Workflow view';

  it('shows an open-flow icon on every row and focuses the run/job id', () => {
    // A plan run (runId === jobId) and a standalone job (runId null).
    jobsMock.mockReturnValue({
      jobs: [
        job({ jobId: 'plan-1', trioId: 'plan|my-flow', runId: 'plan-1', sessionId: 'session-A' }),
        job({ jobId: 'job-2', trioId: 'face-recognition|recognize_faces', runId: null, sessionId: 'session-A' }),
      ],
    });
    const onOpenFlow = vi.fn();
    render(<JobsPanel activeSessionId="session-A" onOpenFlow={onOpenFlow} />);

    const buttons = screen.getAllByLabelText(LABEL);
    expect(buttons).toHaveLength(2); // every row, for consistency

    // Row order is sorted by rollUpRuns, so assert order-independently: each
    // row's button focuses its own run/job id (plan -> runId===jobId; standalone
    // -> runId null, falls back to jobId).
    buttons.forEach((b) => fireEvent.click(b));
    const focused = onOpenFlow.mock.calls.map((c) => c[0]);
    expect(new Set(focused)).toEqual(new Set(['plan-1', 'job-2']));
  });

  it('omits the open-flow icon when no onOpenFlow handler is provided', () => {
    jobsMock.mockReturnValue({
      jobs: [job({ jobId: 'plan-1', trioId: 'plan|my-flow', runId: 'plan-1', sessionId: 'session-A' })],
    });
    render(<JobsPanel activeSessionId="session-A" />);
    expect(screen.queryByLabelText(LABEL)).toBeNull();
  });
});

describe('JobsPanel row text (IND403 dry run)', () => {
  it('names the workflow, not the raw trio id, and a running job shows how long it has been going', () => {
    const started = new Date(Date.now() - 42_000).toISOString();
    jobsMock.mockReturnValue({ jobs: [job({ jobId: 'j-run', sessionId: 'session-A', trioId: 'shot-segmentation|segment_shots', status: 'RUNNING', startAt: started, updatedAt: started, terminalAt: null })] });
    render(<JobsPanel activeSessionId="session-A" />);
    expect(screen.getByText('Shot Segmentation')).toBeTruthy();
    expect(screen.queryByText(/shot-segmentation\|segment_shots/)).toBeNull();
    expect(screen.getByText(/Running · 4[12]s/)).toBeTruthy();
  });
});

describe('View results — the summary turn, never "feature the artifacts"', () => {
  afterEach(cleanup);
  it('sends a results_summary turn, so the agent cannot open a second gallery tab of the same shots', () => {
    jobsMock.mockReturnValue({ jobs: [job({ jobId: 'j-shots', trioId: 'shot-segmentation|segment_shots', sessionId: 'session-A' })] });
    const sent: Array<{ prompt: string; turnPurpose?: string }> = [];
    const onSend = (e: Event) => sent.push((e as CustomEvent).detail);
    window.addEventListener('chat:send', onSend);
    render(<JobsPanel activeSessionId="session-A" />);
    fireEvent.click(screen.getByRole('button', { name: 'View results' }));
    window.removeEventListener('chat:send', onSend);
    expect(sent).toHaveLength(1);
    expect(sent[0].turnPurpose).toBe('results_summary');
    expect(sent[0].prompt).toContain('get_job("j-shots")');
    expect(sent[0].prompt).not.toMatch(/(^|[^-])feature the artifacts/i);
  });
});
