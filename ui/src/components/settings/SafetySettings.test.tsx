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

const DEFAULT = { id: 'dflt1', version: '1', name: 'harness-default' };
const VIEW = {
  guardrails: [{ id: 'abc123', name: 'Strict', description: 'brand safety', versions: ['1', '2'] }],
  selected: null, default: DEFAULT, effective: DEFAULT,
};
const flush = () => act(async () => { for (let i = 0; i < 5; i++) await Promise.resolve(); });

afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe('Settings -> Safety (admin)', () => {
  it('shows what is in use and offers the default, each published version (newest first) and None', async () => {
    m.getGuardrails.mockResolvedValue(VIEW);
    render(<SafetySettings />);
    await flush();
    expect(screen.getByTestId('guardrail-effective')).toHaveTextContent('harness-default (v1)');
    const opts = Array.from((screen.getByLabelText('Content guardrail') as HTMLSelectElement).options).map((o) => o.textContent);
    expect(opts).toEqual(['harness-default (v1) · platform default', 'Strict (v2)', 'Strict (v1)', 'None (no guardrail)']);
  });

  it('saves a specific version, None, or the default', async () => {
    m.getGuardrails.mockResolvedValue(VIEW);
    m.setGuardrail.mockImplementation(async (pick: any) => (pick ? { ...pick, name: 'x' } : null));
    render(<SafetySettings />);
    await flush();
    const select = screen.getByLabelText('Content guardrail');
    fireEvent.change(select, { target: { value: 'abc123:2' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await flush();
    expect(m.setGuardrail).toHaveBeenLastCalledWith({ id: 'abc123', version: '2' });
    expect(screen.getByRole('status')).toHaveTextContent('within a minute');
    fireEvent.change(select, { target: { value: '__none__' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await flush();
    expect(m.setGuardrail).toHaveBeenLastCalledWith(null);
    expect(screen.getByTestId('guardrail-effective')).toHaveTextContent('None');
    fireEvent.change(select, { target: { value: '__default__' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await flush();
    expect(m.setGuardrail).toHaveBeenLastCalledWith('default');
  });

  it('lists the default guardrail once, and shows a pin of its version as the default', async () => {
    m.getGuardrails.mockResolvedValue({
      guardrails: [{ id: 'dflt1', name: 'harness-default', description: 'd', versions: ['1'] }],
      selected: { id: 'dflt1', version: '1', name: 'harness-default' }, default: DEFAULT, effective: DEFAULT,
    });
    render(<SafetySettings />);
    await flush();
    const sel = screen.getByLabelText('Content guardrail') as HTMLSelectElement;
    expect(Array.from(sel.options).map((o) => o.textContent)).toEqual(['harness-default (v1) · platform default', 'None (no guardrail)']);
    expect(sel.value).toBe('__default__');
  });

  it('shows a load or save failure', async () => {
    m.getGuardrails.mockRejectedValueOnce(new Error('403'));
    render(<SafetySettings />);
    await flush();
    expect(screen.getByRole('alert')).toHaveTextContent("Couldn't load guardrails.");
    cleanup();
    m.getGuardrails.mockResolvedValue(VIEW);
    m.setGuardrail.mockRejectedValue(new Error('No published version of that guardrail in this account'));
    render(<SafetySettings />);
    await flush();
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await flush();
    expect(screen.getByRole('alert')).toHaveTextContent('No published version');
  });
});
