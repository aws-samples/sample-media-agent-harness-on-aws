/** @vitest-environment jsdom */
/**
 * The tour against what workshop testers hit (IND403 dry run, 2026-11):
 *  - the counter said "1 / 17" while the welcome listed 13 stops;
 *  - a failed agent turn ("This turn failed: … ServiceUnavailableException") advanced the tour as if it had worked;
 *  - "Shot segmentation is running" never advanced although the job was Completed (it counted Jobs-panel rows, which
 *    exist only while the Jobs tab is open, and missed a job that finished before the step opened);
 *  - "Do it for me" on Clip, with no range marked, sent "Clip the video." to the agent and no job started;
 *  - "Do it for me" on Bring a video did nothing, silently, when no video was there;
 *  - Skip tour left the + menu open.
 */
import '@testing-library/jest-dom';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';

const jobs = vi.hoisted(() => ({ byId: new Map<string, { jobId: string; status: string; startAt: string; trioId?: string }>() }));
vi.mock('../../hooks/useJobs', () => ({ jobsStore: { getSnapshot: () => ({ jobsById: jobs.byId }) } }));

import HarnessTour, { attachDemoVideo, stopNumber, STOP_COUNT } from './HarnessTour';
import { isFailedTurn } from '../ChatCanvas';

const STEP_KEY = 'harness-tour-v1-step';
const realRect = HTMLElement.prototype.getBoundingClientRect;
const tick = async (ms: number) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); };
const step = () => screen.getByTestId('tour-step');

beforeEach(() => {
  localStorage.clear();
  jobs.byId.clear();
  // The tour reads only what is visible; jsdom lays nothing out, so give every element a box.
  HTMLElement.prototype.getBoundingClientRect = () => ({ x: 0, y: 0, top: 0, left: 0, right: 10, bottom: 10,
    width: 10, height: 10, toJSON: () => ({}) }) as DOMRect;
  document.body.innerHTML = '<div data-tour="center" id="center"></div><div data-tour="right-panel"></div>';
  vi.useFakeTimers();
});
afterEach(() => { vi.useRealTimers(); HTMLElement.prototype.getBoundingClientRect = realRect; });

/** An assistant reply as ChatCanvas renders it: the ⏱ summary marks it finished. */
function reply(text: string, failed = false) {
  const row = document.createElement('div');
  row.setAttribute('data-role', 'assistant');
  if (failed) row.setAttribute('data-turn-failed', 'true');
  row.innerText = `${text}\n⏱ 1.2s`;
  row.textContent = row.innerText;
  document.getElementById('center')!.appendChild(row);
}

describe('the counter numbers stops, as the welcome list does', () => {
  it('13 stops; a wait step shows the stop it belongs to', () => {
    expect(STOP_COUNT).toBe(13);
    expect(stopNumber(0)).toBe(1);
    expect(stopNumber(3)).toBe(3); // "The agent is answering" belongs to stop 3, Ask about it
    expect(stopNumber(16)).toBe(13);
  });
  it('the card reads "1 / 13", never "1 / 17"', () => {
    render(<HarnessTour firstVisit />);
    fireEvent.click(screen.getByTestId('tour-start'));
    expect(step()).toHaveTextContent('1 / 13');
    expect(step()).not.toHaveTextContent('/ 17');
  });
});

describe('a failed agent turn never advances the tour', () => {
  it('goes back to the step that sent it and says why', async () => {
    localStorage.setItem(STEP_KEY, '3'); // "The agent is answering"
    render(<HarnessTour />);
    await tick(300);
    reply('This turn failed: An error occurred (ServiceUnavailableException) when calling the ConverseStream operation: Bedrock is unable to process your request.', true);
    await tick(900);
    expect(step()).toHaveAttribute('data-step', '2'); // back to "Ask about it"
    const note = screen.getByTestId('tour-note');
    expect(note).toHaveTextContent("That didn't work");
    expect(note).toHaveTextContent('ServiceUnavailableException');
    expect(note).toHaveTextContent('Settings → Model');
  });
  it('a turn that worked advances to Read the answer, which highlights the reply', async () => {
    localStorage.setItem(STEP_KEY, '3');
    render(<HarnessTour />);
    await tick(300);
    reply('The video is 1280×826, 15 fps.');
    await tick(2000);
    expect(step()).toHaveAttribute('data-step', '4');
    expect(step()).toHaveTextContent('See how it answered');
    expect(step()).toHaveTextContent('calling tools');
    expect(screen.queryByTestId('tour-note')).toBeNull();
  });
  it('isFailedTurn: the agent error text or a failed "Turn failed" step', () => {
    expect(isFailedTurn({ content: '\n\n**This turn failed:** boom\n' })).toBe(true);
    expect(isFailedTurn({ content: 'ok', steps: [{ status: 'failed', label: 'Turn failed' }] })).toBe(true);
    expect(isFailedTurn({ content: 'All done.', steps: [{ status: 'done', label: 'Ready' }] })).toBe(false);
  });
});

describe('"Watch it in Jobs" waits on the job itself — no Next to press', () => {
  it('advances on a job that already finished, with no Jobs panel rows on screen', async () => {
    jobs.byId.set('j1', { jobId: 'j1', status: 'SUCCEEDED', startAt: new Date().toISOString(), trioId: 'shot-segmentation|segment_shots' });
    localStorage.setItem(STEP_KEY, '9');
    render(<HarnessTour />);
    expect(document.querySelectorAll('[data-job-row]')).toHaveLength(0);
    await tick(2000);
    expect(step()).toHaveAttribute('data-step', '10'); // "Bring the results back"
  });
  it('waits while the job runs, then advances when it completes', async () => {
    jobs.byId.set('j1', { jobId: 'j1', status: 'RUNNING', startAt: new Date().toISOString(), trioId: 'shot-segmentation|segment_shots' });
    localStorage.setItem(STEP_KEY, '9');
    render(<HarnessTour />);
    await tick(3000);
    expect(step()).toHaveAttribute('data-step', '9');
    expect(screen.queryByTestId('tour-next')).toBeNull(); // it is a wait: nothing to press
    jobs.byId.set('j1', { jobId: 'j1', status: 'SUCCEEDED', startAt: new Date().toISOString(), trioId: 'shot-segmentation|segment_shots' });
    await tick(2000);
    expect(step()).toHaveAttribute('data-step', '10');
  });
  it('a probe job that succeeds first (process_media) does not move it on while the shot job is pending', async () => {
    jobs.byId.set('probe', { jobId: 'probe', status: 'SUCCEEDED', startAt: new Date().toISOString(), trioId: 'media-processing|process_media' });
    jobs.byId.set('shots', { jobId: 'shots', status: 'PENDING', startAt: new Date().toISOString(), trioId: 'shot-segmentation|segment_shots' });
    localStorage.setItem(STEP_KEY, '9');
    render(<HarnessTour />);
    await tick(3000);
    expect(step()).toHaveAttribute('data-step', '9');
    jobs.byId.set('shots', { jobId: 'shots', status: 'SUCCEEDED', startAt: new Date().toISOString(), trioId: 'shot-segmentation|segment_shots' });
    await tick(1500);
    expect(step()).toHaveAttribute('data-step', '10');
  });
  it('a failed job goes back to Find the shots and says so', async () => {
    jobs.byId.set('j1', { jobId: 'j1', status: 'FAILED', startAt: new Date().toISOString(), trioId: 'shot-segmentation|segment_shots' });
    localStorage.setItem(STEP_KEY, '9');
    render(<HarnessTour />);
    await tick(1000);
    expect(step()).toHaveAttribute('data-step', '7');
    expect(screen.getByTestId('tour-note')).toHaveTextContent('the job failed');
  });
});

describe('"Cut a clip" is one step: mark a range and Clip', () => {
  function timeline({ ruler }: { ruler: boolean }) {
    const clicks: string[] = [];
    const host = document.createElement('div');
    host.innerHTML = (ruler ? '<div data-testid="timeline-ruler" id="ruler"></div>' : '') + '<button data-testid="action-clip">Clip</button>';
    document.body.appendChild(host);
    host.querySelector('[data-testid="action-clip"]')!.addEventListener('click', () => clicks.push('clip'));
    // The ruler marks a range on pointerup, as TimelineEditor does.
    window.addEventListener('pointerup', () => {
      if (!document.querySelector('#ruler') || document.querySelector('[data-testid^="range-band-"]')) return;
      const band = document.createElement('div'); band.setAttribute('data-testid', 'range-band-0'); host.appendChild(band);
    });
    return clicks;
  }
  it('drags a range, then clicks Clip, so the clip is cut directly instead of asking the agent', async () => {
    const clicks = timeline({ ruler: true });
    localStorage.setItem(STEP_KEY, '5'); // "Cut a clip"
    render(<HarnessTour />);
    await tick(300);
    fireEvent.click(screen.getByTestId('tour-do-it'));
    await tick(800);
    expect(document.querySelector('[data-testid^="range-band-"]')).not.toBeNull();
    expect(clicks).toEqual(['clip']);
  });
  it('with no timeline to mark, says so instead of sending "Clip the video."', async () => {
    const clicks = timeline({ ruler: false });
    localStorage.setItem(STEP_KEY, '5');
    render(<HarnessTour />);
    await tick(300);
    fireEvent.click(screen.getByTestId('tour-do-it'));
    await tick(800);
    expect(clicks).toEqual([]);
    expect(screen.getByTestId('tour-note')).toHaveTextContent('Mark a range on the timeline first');
  });
});

describe('the rest', () => {
  it('Bring a video says why when there is no video to attach', async () => {
    document.body.innerHTML = '<button aria-label="Attach and tools" id="plus"></button><div id="menu"></div>';
    document.getElementById('plus')!.addEventListener('click', () => {
      document.getElementById('menu')!.innerHTML = '<input data-testid="plus-menu-asset-search" />';
    });
    const run = attachDemoVideo();
    await vi.runAllTimersAsync();
    expect(await run).toMatch(/no video in this project yet.*0-demo\.mp4/);
  });
  it('Skip tour closes the + menu it opened', async () => {
    let open = true;
    const plus = document.createElement('button');
    plus.setAttribute('aria-label', 'Attach and tools');
    document.body.appendChild(plus);
    const menu = document.createElement('div');
    menu.innerHTML = '<input data-testid="plus-menu-asset-search" />';
    document.body.appendChild(menu);
    plus.addEventListener('click', () => { open = !open; menu.innerHTML = open ? '<input data-testid="plus-menu-asset-search" />' : ''; });
    localStorage.setItem(STEP_KEY, '1'); // "Bring a video"
    render(<HarnessTour />);
    await tick(300);
    fireEvent.click(screen.getByRole('button', { name: 'Skip tour' }));
    expect(open).toBe(false);
    expect(document.querySelector('[data-testid="plus-menu-asset-search"]')).toBeNull();
  });
});

describe('Ask about it: "Do it for me" waits until Send can send', () => {
  it('a Send still disabled (the attachment resolving) is not clicked into nothing', async () => {
    const send = document.createElement('button');
    send.setAttribute('aria-label', 'Send message');
    send.disabled = true;
    let sent = 0;
    send.addEventListener('click', () => {
      sent++;
      const stop = document.createElement('button'); stop.setAttribute('aria-label', 'Stop agent'); document.body.appendChild(stop);
    });
    document.body.appendChild(send);
    localStorage.setItem(STEP_KEY, '2'); // "Ask about it"
    render(<HarnessTour />);
    await tick(300);
    fireEvent.click(screen.getByTestId('tour-do-it'));
    await tick(2000);
    expect(sent).toBe(0); // still disabled: nothing pressed yet
    send.disabled = false;
    await tick(1500);
    expect(sent).toBe(1);
    expect(step()).toHaveAttribute('data-step', '3'); // the turn started: on to "The agent is answering"
  });
});

describe('Jump to a shot: done when the video is at the clicked shot', () => {
  it('clicking a shot that the player seeks to completes the step', async () => {
    const panel = document.querySelector('[data-tour="right-panel"]')!;
    panel.innerHTML = '<video></video><div data-testid="lane-shots">'
      + '<button title="Shot 0 @ 0.00s"></button><button title="Shot 1 @ 4.20s"></button><button title="Shot 2 @ 9.50s"></button></div>';
    const video = panel.querySelector('video')!;
    Object.defineProperty(video, 'currentTime', { value: 0, writable: true });
    panel.querySelectorAll('[data-testid="lane-shots"] button').forEach((b) => b.addEventListener('click', () => {
      video.currentTime = Number(/@ ([\d.]+)s/.exec(b.getAttribute('title')!)![1]);
    }));
    localStorage.setItem(STEP_KEY, '12'); // "Jump to a shot"
    render(<HarnessTour />);
    await tick(300);
    expect(step()).toHaveTextContent('Jump to a shot');
    fireEvent.click(screen.getByTestId('tour-do-it'));
    await tick(1500);
    expect(video.currentTime).toBe(9.5);
    // It does not move on by itself: the user tries a few shots, then presses Next when ready.
    await tick(3000);
    expect(step()).toHaveAttribute('data-step', '12');
    expect(step()).toHaveTextContent('✓ Jump to a shot');
    fireEvent.click(panel.querySelectorAll<HTMLElement>('[data-testid="lane-shots"] button')[1]);
    await tick(1000);
    expect(video.currentTime).toBe(4.2);
    expect(step()).toHaveAttribute('data-step', '12');
    fireEvent.click(screen.getByTestId('tour-next'));
    await tick(300);
    expect(step()).toHaveAttribute('data-step', '13'); // on to Projects
  });
});
