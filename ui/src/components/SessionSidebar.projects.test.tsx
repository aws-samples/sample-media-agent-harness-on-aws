/**
 * @vitest-environment jsdom
 *
 * The sidebar's Projects inventory, laid out like the Agents and Skills blocks: listed only in `projects`
 * mode, Personal first, a lock on Personal and a folder on the rest, the role on each row, the
 * Selected_Project highlighted, and `+ New project` as the one create entry.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import '@testing-library/jest-dom';
import React from 'react';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';

const mocks = vi.hoisted(() => ({
  listProjects: vi.fn(),
  projectsEnabled: vi.fn(),
}));

vi.mock('../auth/useCurrentUser', () => ({
  useCurrentUser: () => ({ mode: 'dev', displayName: 'Local Dev', initials: 'LD', sub: 'u', signOut: vi.fn() }),
}));
vi.mock('../auth/isGatewayAdmin', () => ({ useIsGatewayAdmin: () => false }));
vi.mock('../hooks/useJobs', () => ({ useJobs: () => ({ jobs: [] }) }));
vi.mock('../config/branding', () => ({
  BRAND_NAME: 'TestBrand',
  getActiveLogo: () => ({ src: 'logo.png', alt: 'logo', borderRadius: 0 }),
}));
vi.mock('../projects/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../projects/api')>()),
  listProjects: mocks.listProjects,
  projectsEnabled: mocks.projectsEnabled,
}));

import SessionSidebar from './SessionSidebar';
import { __resetProjectsListForTests, refreshProjectsList } from '../projects/useProjectsList';
import { EMOJI_OR_GLYPH } from '../test-utils/glyphs';

type SidebarProps = React.ComponentProps<typeof SessionSidebar>;

const LIST = [
  { projectId: 'pB', name: 'brand archive', role: 'member', personal: false },
  { projectId: 'pL', name: 'Launch Q3', role: 'owner', personal: false },
  { projectId: 'pP', name: 'Personal', role: 'owner', personal: true },
];

function makeProps(overrides: Partial<SidebarProps> = {}): SidebarProps {
  return {
    sessions: [],
    activeSessionId: '',
    collapsed: false,
    onToggleCollapse: vi.fn(),
    onSelectSession: vi.fn(),
    onNewSession: vi.fn(),
    onGoChat: vi.fn(),
    onShowLanding: vi.fn(),
    onOpenSettings: vi.fn(),
    onOpenWorkflows: vi.fn(),
    onOpenCustomAgents: vi.fn(),
    onOpenProjects: vi.fn(),
    onNewProject: vi.fn(),
    onSelectProject: vi.fn(),
    projectsEnabled: true,
    centerMode: 'projects',
    ...overrides,
  };
}

async function flush() {
  await act(async () => { for (let i = 0; i < 6; i++) await Promise.resolve(); });
}

const rows = () => Array.from(document.querySelectorAll<HTMLElement>('[data-project-row]'));

beforeEach(() => {
  vi.clearAllMocks();
  __resetProjectsListForTests();
  mocks.projectsEnabled.mockResolvedValue(true);
  mocks.listProjects.mockResolvedValue({ ok: true, value: LIST });
});
afterEach(cleanup);

describe('SessionSidebar — Projects inventory', () => {
  it('lists projects only in projects mode, and reads nothing in other modes', async () => {
    const { rerender } = render(<SessionSidebar {...makeProps({ centerMode: 'chat' })} />);
    await flush();
    expect(screen.queryByTestId('project-inventory')).toBeNull();
    expect(mocks.listProjects).not.toHaveBeenCalled();

    rerender(<SessionSidebar {...makeProps({ centerMode: 'projects' })} />);
    await flush();
    expect(screen.getByTestId('project-inventory')).toBeInTheDocument();
    expect(mocks.listProjects).toHaveBeenCalledTimes(1);

    rerender(<SessionSidebar {...makeProps({ centerMode: 'skills' })} />);
    expect(screen.queryByTestId('project-inventory')).toBeNull();
  });

  it('orders Personal first then by name, with a lock on Personal, a folder elsewhere, and each role', async () => {
    render(<SessionSidebar {...makeProps()} />);
    await flush();
    expect(rows().map((r) => r.getAttribute('aria-label'))).toEqual([
      'Personal (private), owner',
      'brand archive, member',
      'Launch Q3, owner',
    ]);
    for (const r of rows()) {
      expect(r.querySelector('svg')).not.toBeNull();
      expect(r.textContent ?? '').not.toMatch(EMOJI_OR_GLYPH);
    }
    expect(rows()[1]).toHaveTextContent('member');
    // The category header counts them, as Agents and Skills do.
    expect(screen.getByRole('button', { name: /^Projects/ })).toHaveTextContent('3');
  });

  it('marks a project with search indexing on with the magnifier, and only that one', async () => {
    mocks.listProjects.mockResolvedValue({ ok: true, value: [
      { ...LIST[0], indexing: true }, { ...LIST[1], indexing: false }, LIST[2],
    ] });
    render(<SessionSidebar {...makeProps()} />);
    await flush();
    const marked = rows().filter((r) => r.querySelector('[data-testid="search-indexed-glyph"]'));
    expect(marked.map((r) => r.getAttribute('aria-label'))).toEqual(['brand archive, member']);
  });

  it('highlights the Selected_Project and reports a click or Enter as a selection', async () => {
    const onSelectProject = vi.fn();
    render(<SessionSidebar {...makeProps({ activeProjectId: 'pL', onSelectProject })} />);
    await flush();
    const [personal, , launch] = rows();
    expect(launch).toHaveAttribute('data-active', 'true');
    expect(launch).toHaveAttribute('aria-current', 'true');
    expect(personal).toHaveAttribute('data-active', 'false');
    fireEvent.click(personal);
    expect(onSelectProject).toHaveBeenCalledWith('pP');
    fireEvent.keyDown(rows()[1], { key: 'Enter' });
    expect(onSelectProject).toHaveBeenLastCalledWith('pB');
  });

  it('picks up a refresh made elsewhere (the surface after create, rename, delete, or leave)', async () => {
    render(<SessionSidebar {...makeProps()} />);
    await flush();
    expect(rows()).toHaveLength(3);
    mocks.listProjects.mockResolvedValue({
      ok: true,
      value: [...LIST, { projectId: 'pN', name: 'New one', role: 'owner', personal: false }],
    });
    await act(async () => { await refreshProjectsList(); });
    expect(rows().map((r) => r.getAttribute('title'))).toContain('New one');
  });

  it('has exactly one New project entry, the shared header action, and it calls onNewProject', async () => {
    const onNewProject = vi.fn();
    render(<SessionSidebar {...makeProps({ onNewProject })} />);
    await flush();
    const entries = screen.getAllByRole('button', { name: /new project/i });
    expect(entries).toHaveLength(1);
    fireEvent.click(entries[0]);
    expect(onNewProject).toHaveBeenCalledTimes(1);
    expect(within(screen.getByTestId('project-inventory')).queryByRole('button', { name: /new/i })).toBeNull();
  });
});

describe('Settings (IND403: "Capabilities holds the settings; why not call it Settings?")', () => {
  it('the lower menu heading is Settings, and nothing is labelled Capabilities', async () => {
    render(<SessionSidebar {...makeProps()} />);
    await flush();
    expect(screen.getByRole('button', { name: /^Settings$/ })).toBeInTheDocument();
    expect(screen.queryByText('Capabilities')).toBeNull();
  });
  it('the collapsed rail has ONE Settings button', async () => {
    render(<SessionSidebar {...makeProps({ collapsed: true })} />);
    await flush();
    expect(screen.getAllByRole('button', { name: 'Settings' })).toHaveLength(1);
    expect(screen.queryByRole('button', { name: 'Capabilities' })).toBeNull();
  });
});
