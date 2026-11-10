/**
 * @vitest-environment jsdom
 */
import '@testing-library/jest-dom';
import React from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';

const m = vi.hoisted(() => ({ getGuardrails: vi.fn(), setGuardrail: vi.fn() }));
vi.mock('../../services/adminApi', () => ({ getGuardrails: m.getGuardrails, setGuardrail: m.setGuardrail }));

import SafetySettings from './SafetySettings';

const GUARDRAILS = [{ id: 'abc123', name: 'harness-light', description: 'd', versions: ['1', '2'] }];
const flush = () => act(async () => { for (let i = 0; i < 5; i++) await Promise.resolve(); });

afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe('Settings -> Safety (admin)', () => {
  it('is off until an admin picks a guardrail: None first, then each published version (newest first)', async () => {
    m.getGuardrails.mockResolvedValue({ guardrails: GUARDRAILS, effective: null });
    render(<SafetySettings />);
    await flush();
    expect(screen.getByTestId('guardrail-effective')).toHaveTextContent('In use: None');
    const sel = screen.getByLabelText('Content guardrail') as HTMLSelectElement;
    expect(Array.from(sel.options).map((o) => o.textContent)).toEqual(['None (no guardrail)', 'harness-light (v2)', 'harness-light (v1)']);
    expect(sel.value).toBe('__none__');
  });

  it('shows the pick in use, saves a version or None', async () => {
    m.getGuardrails.mockResolvedValue({ guardrails: GUARDRAILS, effective: { id: 'abc123', version: '1', name: 'harness-light' } });
    m.setGuardrail.mockImplementation(async (pick: any) => (pick ? { ...pick, name: 'harness-light' } : null));
    render(<SafetySettings />);
    await flush();
    const sel = screen.getByLabelText('Content guardrail') as HTMLSelectElement;
    expect(sel.value).toBe('abc123:1');
    fireEvent.change(sel, { target: { value: 'abc123:2' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await flush();
    expect(m.setGuardrail).toHaveBeenLastCalledWith({ id: 'abc123', version: '2' });
    expect(screen.getByTestId('guardrail-effective')).toHaveTextContent('harness-light (v2)');
    expect(screen.getByRole('status')).toHaveTextContent('within a minute');
    fireEvent.change(sel, { target: { value: '__none__' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await flush();
    expect(m.setGuardrail).toHaveBeenLastCalledWith(null);
    expect(screen.getByTestId('guardrail-effective')).toHaveTextContent('None');
  });

  it('shows a load or save failure', async () => {
    m.getGuardrails.mockRejectedValueOnce(new Error('403'));
    render(<SafetySettings />);
    await flush();
    expect(screen.getByRole('alert')).toHaveTextContent("Couldn't load guardrails.");
    cleanup();
    m.getGuardrails.mockResolvedValue({ guardrails: GUARDRAILS, effective: null });
    m.setGuardrail.mockRejectedValue(new Error('No published version of that guardrail in this account'));
    render(<SafetySettings />);
    await flush();
    fireEvent.change(screen.getByLabelText('Content guardrail'), { target: { value: 'abc123:2' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await flush();
    expect(screen.getByRole('alert')).toHaveTextContent('No published version');
  });
});
