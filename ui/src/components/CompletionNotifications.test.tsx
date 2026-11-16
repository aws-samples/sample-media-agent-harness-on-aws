// @vitest-environment jsdom
import React from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import CompletionNotifications from './CompletionNotifications';
import { notificationsStore, __resetNotificationsForTests } from '../services/notifications';
import type { Job } from '../types/jobs';

function job(overrides: Partial<Job>): Job {
  return {
    jobId: 'j-' + Math.random().toString(36).slice(2), actorId: 'a', sessionId: 'sess-A',
    trioId: 'face-recognition|recognize_faces', gatewayId: '__aws_sfn__', status: 'SUCCEEDED',
    startAt: new Date().toISOString(), updatedAt: new Date().toISOString(),
    terminalAt: new Date().toISOString(), hasRenderableArtifacts: true, lastError: null, runId: null, nodeId: null, dependsOn: [],
    ...overrides,
  };
}

describe('CompletionNotifications', () => {
  beforeEach(() => __resetNotificationsForTests());
  afterEach(() => { cleanup(); __resetNotificationsForTests(); vi.useRealTimers(); });

  it('flashes a banner on a new completion and opens the session on click', () => {
    const onOpen = vi.fn();
    render(<CompletionNotifications onOpen={onOpen} />);
    act(() => { notificationsStore.pushFromTerminal(job({ jobId: 'jx', sessionId: 'sess-A' })); });

    expect(screen.getByRole('status').textContent).toMatch(/Face Recognition finished/);
    fireEvent.click(screen.getByText(/Face Recognition finished/));
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(onOpen.mock.calls[0][0]).toMatchObject({ jobId: 'jx', sessionId: 'sess-A', renderable: true });
  });

  it('auto-dismisses the banner after the TTL but keeps it in the bell', () => {
    vi.useFakeTimers();
    render(<CompletionNotifications onOpen={vi.fn()} />);
    act(() => { notificationsStore.pushFromTerminal(job({ jobId: 'jy' })); });
    expect(screen.queryByRole('status')).not.toBeNull();
    act(() => { vi.advanceTimersByTime(12100); });
    expect(screen.queryByRole('status')).toBeNull();
    // Bell still shows the unread count.
    expect(screen.getByLabelText(/Notifications \(1 unread\)/)).toBeTruthy();
  });

  it('X dismisses the banner without marking it read', () => {
    render(<CompletionNotifications onOpen={vi.fn()} />);
    act(() => { notificationsStore.pushFromTerminal(job({ jobId: 'jz' })); });
    fireEvent.click(screen.getByLabelText('Dismiss notification'));
    expect(screen.queryByRole('status')).toBeNull();
    expect(notificationsStore.getSnapshot().unseen).toBe(1);
  });

  it('bell dropdown lists successes and failures and opens on click', () => {
    const onOpen = vi.fn();
    render(<CompletionNotifications onOpen={onOpen} />);
    act(() => {
      notificationsStore.pushFromTerminal(job({ jobId: 'ok', status: 'SUCCEEDED' }));
      notificationsStore.pushFromTerminal(job({ jobId: 'bad', status: 'FAILED', lastError: 'nope', hasRenderableArtifacts: false }));
    });
    fireEvent.click(screen.getByLabelText(/Notifications \(2 unread\)/));
    const items = document.querySelectorAll('[data-notification-id]');
    expect(items.length).toBe(2);
    fireEvent.click(document.querySelector('[data-notification-id="bad"]')!);
    expect(onOpen.mock.calls[0][0]).toMatchObject({ jobId: 'bad', kind: 'failure' });
  });

  it('drops the banner beneath the bell instead of over the top bar, and opening the bell replaces it', () => {
    render(<CompletionNotifications onOpen={vi.fn()} />);
    const bell = screen.getByRole('button', { name: /^Notifications/ });
    bell.parentElement!.getBoundingClientRect = () => ({ top: 10, bottom: 40, left: 900, right: 930, width: 30, height: 30, x: 900, y: 10, toJSON: () => ({}) });
    act(() => { notificationsStore.pushFromTerminal(job({ jobId: 'jb' })); });
    const banner = screen.getByRole('status');
    expect(banner.style.top).toBe('48px');                       // below the bell, never over it
    expect(banner.style.right).toBe(`${window.innerWidth - 930}px`);
    expect(banner.style.left).toBe('');                           // not a full-width strip
    fireEvent.click(screen.getByRole('button', { name: /^Notifications/ }));
    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.getByRole('menu').textContent).toMatch(/finished/);
  });

  it('spans the chat column just under the top bar when the bell sits in it', () => {
    const rect = (left: number, top: number, width: number, height: number) => () =>
      ({ top, bottom: top + height, left, right: left + width, width, height, x: left, y: top, toJSON: () => ({}) });
    const column = document.createElement('div');
    column.setAttribute('data-tour', 'center');
    column.getBoundingClientRect = rect(300, 0, 700, 900);
    document.body.appendChild(column);
    render(<CompletionNotifications onOpen={vi.fn()} />, { container: column });
    const bell = screen.getByRole('button', { name: /^Notifications/ });
    bell.parentElement!.getBoundingClientRect = rect(950, 10, 30, 30);
    act(() => { notificationsStore.pushFromTerminal(job({ jobId: 'jc' })); });
    const banner = screen.getByRole('status');
    expect(banner.style.top).toBe('46px');      // under the top bar: the bell stays clickable
    expect(banner.style.left).toBe('308px');    // the column, inset 8px
    expect(banner.style.width).toBe('684px');   // across the whole column
    expect(banner.style.right).toBe('');
    column.remove();
  });

  it('shows no banner and no unread badge when there are no notifications', () => {
    render(<CompletionNotifications onOpen={vi.fn()} />);
    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.getByLabelText('Notifications')).toBeTruthy();
  });
});

describe('notifiedAgo — bell rows of the same job type tell apart by when they arrived', () => {
  it('just now, minutes, hours, days', async () => {
    const { notifiedAgo } = await import('./CompletionNotifications');
    const now = 1_000_000_000_000;
    expect(notifiedAgo(now - 5_000, now)).toBe('just now');
    expect(notifiedAgo(now - 4 * 60_000, now)).toBe('4m ago');
    expect(notifiedAgo(now - 3 * 3_600_000, now)).toBe('3h ago');
    expect(notifiedAgo(now - 2 * 86_400_000, now)).toBe('2d ago');
  });
});
