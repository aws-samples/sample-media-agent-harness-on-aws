/**
 * @vitest-environment jsdom
 *
 * Projects_Surface: the selected Project full width under the shared surface header — its own header
 * (name, role, lock on Personal, `Use in this chat`, the per-role overflow menu), the inline create form
 * opened by the sidebar's `+ New project`, the Assets section (registered slot, or its toolbar and the empty
 * state; the grid itself is covered in `ProjectAssetsSection.test.tsx`), and
 * the Members section hidden behind `SHOW_PROJECT_MEMBERS`.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import '@testing-library/jest-dom';
import React from 'react';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';

const mocks = vi.hoisted(() => ({
  projectsEnabled: vi.fn(),
  listProjects: vi.fn(),
  createProject: vi.fn(),
  renameProject: vi.fn(),
  deleteProject: vi.fn(),
  leaveProject: vi.fn(),
  getProject: vi.fn(),
  patchSessionProject: vi.fn(),
  listAssets: vi.fn(),
  peekGraphEnabled: vi.fn(() => false),
  peekLineageEnabled: vi.fn(() => false),
}));
vi.mock('./lineage/LineageTab', () => ({
  default: ({ project, initialFocus }: { project: { id: string }; initialFocus?: string | null }) => (
    <div data-testid="lineage-tab-mock" data-focus={initialFocus ?? ''}>{project.id}</div>
  ),
}));
vi.mock('./graph/GraphTab', () => ({
  default: ({ project, focusIds }: { project: { id: string }; focusIds?: string[] }) => (
    <div data-testid="graph-tab-mock" data-focus={(focusIds ?? []).join(',')}>{project.id}</div>
  ),
}));

vi.mock('../../projects/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../projects/api')>()),
  ...mocks,
}));

import ProjectsSurface, { SHOW_PROJECT_MEMBERS } from './ProjectsSurface';
import { PROJECT_ACTIONS_LABEL } from './ProjectHeader';
import { ASSETS_EMPTY_BODY, ASSETS_EMPTY_TITLE, UPLOAD_BUTTON_NAME } from './ProjectAssetsSection';
import { __resetProjectTabsForTests, registerProjectTab, type ProjectTabProps } from './tabSlots';
import { __resetProjectsListForTests } from '../../projects/useProjectsList';
import { __resetSessionProjectStoreForTests, getSessionProject, seedSessionProject } from '../../projects/sessionProjectStore';
import { EMOJI_OR_GLYPH } from '../../test-utils/glyphs';

const PERSONAL = { projectId: 'pP', name: 'Personal', role: 'owner' as const, personal: true };
const OWNED = { projectId: 'pO', name: 'Launch Q3', role: 'owner' as const, personal: false, activeAssetCount: 3 };
const SHARED = { projectId: 'pM', name: 'Brand archive', role: 'member' as const, personal: false };
const LIST = [OWNED, PERSONAL, SHARED];

async function flush() {
  await act(async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); });
}

type Props = React.ComponentProps<typeof ProjectsSurface>;
async function renderSurface(props: Partial<Props> = {}) {
  const onSelectedChange = vi.fn();
  const utils = render(<ProjectsSurface onSelectedChange={onSelectedChange} {...props} />);
  await flush();
  return { ...utils, onSelectedChange };
}

function openActions() {
  fireEvent.click(screen.getByRole('button', { name: PROJECT_ACTIONS_LABEL }));
  return screen.getByRole('menu', { name: PROJECT_ACTIONS_LABEL });
}
const actionNames = (menu: HTMLElement) => within(menu).getAllByRole('menuitem').map((el) => el.textContent);

beforeEach(() => {
  vi.clearAllMocks();
  __resetProjectsListForTests();
  __resetProjectTabsForTests();
  __resetSessionProjectStoreForTests();
  mocks.projectsEnabled.mockResolvedValue(true);
  mocks.listProjects.mockResolvedValue({ ok: true, value: LIST });
  mocks.patchSessionProject.mockResolvedValue({ ok: true, value: undefined });
  mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [] } });
  mocks.peekGraphEnabled.mockReturnValue(false);
  mocks.peekLineageEnabled.mockReturnValue(false);
});
afterEach(cleanup);

describe('ProjectsSurface settings page', () => {
  it('Settings replaces the asset list with the settings page, and pressing it again goes back', async () => {
    mocks.getProject.mockResolvedValue({ ok: true, value: {
      project: { projectId: OWNED.projectId, name: OWNED.name, personal: false, description: 'Launch', tags: [], indexing: false },
      role: 'owner', owner: {}, members: [] } });
    await renderSurface({ selectedId: OWNED.projectId });
    expect(screen.queryByTestId('project-settings')).toBeNull();
    fireEvent.click(screen.getByTestId('project-settings-button'));
    await flush();
    expect(screen.getByTestId('project-settings')).toBeInTheDocument();
    expect(screen.getByTestId('project-settings-button')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.queryByText(ASSETS_EMPTY_TITLE)).toBeNull(); // the asset list is not shown under it
    expect(mocks.getProject).toHaveBeenCalledWith(OWNED.projectId);
    fireEvent.click(screen.getByTestId('project-settings-button'));
    await flush();
    expect(screen.queryByTestId('project-settings')).toBeNull();
  });
});

describe('ProjectsSurface layout', () => {
  it('has no inner list column and no create button of its own', async () => {
    await renderSurface({ selectedId: OWNED.projectId });
    expect(screen.queryByTestId('project-list')).toBeNull();
    expect(screen.queryByRole('button', { name: /new project/i })).toBeNull();
    expect(screen.queryByRole('tablist')).toBeNull();
  });

  it('renders the selected Project in the shared header style: name, one badge, and Personal lock — no notice line', async () => {
    await renderSurface({ selectedId: PERSONAL.projectId });
    const header = screen.getByTestId('project-header');
    expect(within(header).getByRole('heading', { name: 'Personal' })).toBeInTheDocument();
    // Personal says "private" (it is always yours); what that means is the badge's tooltip, not a sentence.
    expect(header).not.toHaveTextContent('owner');
    expect(within(header).getByText('private')).toHaveAttribute('title', expect.stringMatching(/cannot be shared/));
    expect(header).not.toHaveTextContent('cannot be shared');
    expect(header.querySelector('svg[aria-hidden="true"]')).not.toBeNull();
    expect(screen.getByRole('heading', { name: 'Projects' })).toBeInTheDocument();
    expect(screen.getByTestId('projects-surface').textContent ?? '').not.toMatch(EMOJI_OR_GLYPH);
  });

  it('falls back to Personal when nothing is selected and reports it to the host', async () => {
    const { onSelectedChange } = await renderSurface();
    expect(within(screen.getByTestId('project-header')).getByRole('heading', { name: 'Personal' })).toBeInTheDocument();
    expect(onSelectedChange).toHaveBeenCalledWith(PERSONAL.projectId);
  });

  it('follows the host selection (the sidebar row) without touching the chat', async () => {
    await seedSessionProject('S1', { activeProjectId: PERSONAL.projectId, activeProjectName: 'Personal' });
    const { rerender, onSelectedChange } = await renderSurface({ selectedId: PERSONAL.projectId, activeSessionId: 'S1' });
    rerender(<ProjectsSurface selectedId={SHARED.projectId} activeSessionId="S1" onSelectedChange={onSelectedChange} />);
    await flush();
    expect(within(screen.getByTestId('project-header')).getByRole('heading', { name: 'Brand archive' })).toBeInTheDocument();
    expect(getSessionProject('S1')?.activeProjectId).toBe(PERSONAL.projectId);
    expect(mocks.patchSessionProject).not.toHaveBeenCalled();
  });
});

describe('ProjectsSurface overflow menu', () => {
  it('offers Rename and Delete project to the Owner of a non-Personal Project', async () => {
    await renderSurface({ selectedId: OWNED.projectId });
    expect(actionNames(openActions())).toEqual(['Rename', 'Delete project']);
  });

  it('offers only Leave project to a Member', async () => {
    await renderSurface({ selectedId: SHARED.projectId });
    expect(actionNames(openActions())).toEqual(['Leave project']);
  });

  it('has no menu at all for Personal', async () => {
    await renderSurface({ selectedId: PERSONAL.projectId });
    expect(screen.queryByRole('button', { name: PROJECT_ACTIONS_LABEL })).toBeNull();
  });

  it('Rename edits inline, PATCHes the new name, and re-reads the list', async () => {
    mocks.renameProject.mockResolvedValue({ ok: true, value: { projectId: OWNED.projectId, name: 'Launch Q4', personal: false } });
    await renderSurface({ selectedId: OWNED.projectId });
    fireEvent.click(within(openActions()).getByRole('menuitem', { name: 'Rename' }));
    const field = screen.getByRole('textbox', { name: 'Project name' });
    expect(field).toHaveValue('Launch Q3');
    mocks.listProjects.mockResolvedValue({ ok: true, value: [{ ...OWNED, name: 'Launch Q4' }, PERSONAL, SHARED] });
    fireEvent.change(field, { target: { value: '  Launch Q4 ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await flush();
    expect(mocks.renameProject).toHaveBeenCalledWith(OWNED.projectId, 'Launch Q4');
    expect(within(screen.getByTestId('project-header')).getByRole('heading', { name: 'Launch Q4' })).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: 'Project name' })).toBeNull();
  });

  it('Delete project needs the exact name typed, then deletes and falls back to Personal', async () => {
    mocks.deleteProject.mockResolvedValue({ ok: true, value: { projectId: OWNED.projectId, name: OWNED.name, personal: false } });
    const { onSelectedChange } = await renderSurface({ selectedId: OWNED.projectId });
    fireEvent.click(within(openActions()).getByRole('menuitem', { name: 'Delete project' }));
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveTextContent('Delete "Launch Q3"? This cannot be undone. It currently has 3 active assets.');
    const confirm = within(dialog).getByRole('button', { name: 'Delete project' });
    expect(confirm).toBeDisabled();
    fireEvent.change(within(dialog).getByLabelText('Type Launch Q3 to confirm'), { target: { value: 'Launch Q3' } });
    expect(confirm).toBeEnabled();
    mocks.listProjects.mockResolvedValue({ ok: true, value: [PERSONAL, SHARED] });
    fireEvent.click(confirm);
    await flush();
    expect(mocks.deleteProject).toHaveBeenCalledWith(OWNED.projectId, 'Launch Q3');
    expect(onSelectedChange).toHaveBeenLastCalledWith(PERSONAL.projectId);
    expect(within(screen.getByTestId('project-header')).getByRole('heading', { name: 'Personal' })).toBeInTheDocument();
  });

  it('Leave project confirms, leaves, and falls back to Personal with the lost-access notice', async () => {
    mocks.leaveProject.mockResolvedValue({ ok: true, value: { sub: 'me' } });
    await renderSurface({ selectedId: SHARED.projectId });
    fireEvent.click(within(openActions()).getByRole('menuitem', { name: 'Leave project' }));
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveTextContent('Leave "Brand archive"? You will lose access until you are invited again.');
    mocks.listProjects.mockResolvedValue({ ok: true, value: [OWNED, PERSONAL] });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Leave' }));
    await flush();
    expect(mocks.leaveProject).toHaveBeenCalledWith(SHARED.projectId);
    expect(within(screen.getByTestId('project-header')).getByRole('heading', { name: 'Personal' })).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('You no longer have access to Brand archive');
  });
});

describe('ProjectsSurface create form', () => {
  it('opens only on a createRequest bump, and selects the created Project', async () => {
    mocks.createProject.mockResolvedValue({ ok: true, value: { projectId: 'pNew', name: 'Trailer cuts', personal: false } });
    const { rerender, onSelectedChange } = await renderSurface({ createRequest: 0, selectedId: PERSONAL.projectId });
    expect(screen.queryByTestId('project-create-form')).toBeNull();

    rerender(<ProjectsSurface createRequest={1} selectedId={PERSONAL.projectId} onSelectedChange={onSelectedChange} />);
    const form = screen.getByTestId('project-create-form');
    // A full page: the selected project's header and assets are not shown behind it.
    expect(screen.queryByTestId('project-header')).toBeNull();
    expect(screen.queryByTestId('project-assets')).toBeNull();
    const create = within(form).getByRole('button', { name: 'Create project' });
    expect(create).toBeDisabled();
    fireEvent.change(within(form).getByLabelText('New project name'), { target: { value: '   ' } });
    expect(create).toBeDisabled();

    mocks.listProjects.mockResolvedValue({
      ok: true,
      value: [...LIST, { projectId: 'pNew', name: 'Trailer cuts', role: 'owner', personal: false }],
    });
    fireEvent.change(within(form).getByLabelText('New project name'), { target: { value: ' Trailer cuts ' } });
    fireEvent.click(create);
    await flush();
    // Indexing defaults OFF; no description or tags were typed.
    expect(mocks.createProject).toHaveBeenCalledWith('Trailer cuts', { indexing: false });
    expect(onSelectedChange).toHaveBeenLastCalledWith('pNew');
    expect(screen.queryByTestId('project-create-form')).toBeNull();
    expect(within(screen.getByTestId('project-header')).getByRole('heading', { name: 'Trailer cuts' })).toBeInTheDocument();
  });

  it('keeps the form open with the error inline when create fails', async () => {
    mocks.createProject.mockResolvedValue({ ok: false, kind: 'error', status: 409, code: 'name_taken', message: 'That name is taken' });
    const { rerender, onSelectedChange } = await renderSurface({ createRequest: 0 });
    rerender(<ProjectsSurface createRequest={1} onSelectedChange={onSelectedChange} />);
    fireEvent.change(screen.getByLabelText('New project name'), { target: { value: 'Launch Q3' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create project' }));
    await flush();
    expect(screen.getByRole('alert')).toHaveTextContent('That name is taken');
    expect(screen.getByTestId('project-create-form')).toBeInTheDocument();
  });

  it('Back to Projects returns to the project that was showing, creating nothing', async () => {
    const { rerender, onSelectedChange } = await renderSurface({ createRequest: 0, selectedId: OWNED.projectId });
    rerender(<ProjectsSurface createRequest={1} selectedId={OWNED.projectId} onSelectedChange={onSelectedChange} />);
    expect(screen.getByTestId('project-create-page')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '← Back to Projects' }));
    expect(screen.queryByTestId('project-create-page')).toBeNull();
    expect(within(screen.getByTestId('project-header')).getByRole('heading', { name: OWNED.name })).toBeInTheDocument();
    expect(mocks.createProject).not.toHaveBeenCalled();
  });
});

describe('ProjectsSurface body', () => {
  it('shows the Assets empty state, and never "Not available in this build"', async () => {
    await renderSurface({ selectedId: OWNED.projectId });
    const empty = screen.getByTestId('project-assets-empty');
    expect(empty).toHaveTextContent(ASSETS_EMPTY_TITLE);
    expect(empty).toHaveTextContent(ASSETS_EMPTY_BODY);
    expect(ASSETS_EMPTY_TITLE).toBe('No assets yet');
    expect(empty.querySelector('svg[aria-hidden="true"]')).not.toBeNull();
    expect(screen.queryByText(/Not available in this build/)).toBeNull();
    expect(mocks.listAssets).toHaveBeenCalledWith(OWNED.projectId);
    // The Assets section's own toolbar; no header slot buttons without registered slots.
    const section = screen.getByTestId('project-assets');
    expect(within(section).getByRole('button', { name: UPLOAD_BUTTON_NAME })).toBeInTheDocument();
    expect(within(section).queryByRole('button', { name: /S3 location/ })).toBeNull();
    expect(within(screen.getByTestId('project-header')).queryByRole('button', { name: /^Upload/ })).toBeNull();
    expect(screen.queryByRole('button', { name: 'S3 location' })).toBeNull();
  });

  it('renders a registered assets slot instead of the empty state, with the Selected_Project', async () => {
    registerProjectTab('assets', ({ project, role }: ProjectTabProps) => (
      <div data-testid="assets-slot">{`${project.id}:${role}`}</div>
    ));
    await renderSurface({ selectedId: OWNED.projectId });
    expect(screen.getByTestId('assets-slot')).toHaveTextContent('pO:owner');
    expect(screen.queryByTestId('project-assets-empty')).toBeNull();
  });

  it('shows Upload and S3 location toolbar buttons only for registered slots, opening their panels', async () => {
    registerProjectTab('upload', () => <div data-testid="upload-slot" />);
    await renderSurface({ selectedId: OWNED.projectId });
    expect(screen.queryByRole('button', { name: 'S3 location' })).toBeNull();
    const upload = screen.getByRole('button', { name: 'Upload' });
    fireEvent.click(upload);
    expect(screen.getByTestId('upload-slot')).toBeInTheDocument();
    expect(upload).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(upload);
    expect(screen.queryByTestId('upload-slot')).toBeNull();
  });

  it('hides the Members section and has no Settings tab', async () => {
    expect(SHOW_PROJECT_MEMBERS).toBe(false);
    await renderSurface({ selectedId: OWNED.projectId });
    expect(screen.queryByTestId('members-tab')).toBeNull();
    expect(screen.queryByTestId('settings-tab')).toBeNull();
    expect(screen.queryByRole('heading', { name: 'Members' })).toBeNull();
    expect(mocks.getProject).not.toHaveBeenCalled();
  });

  it('New chat shows on every Project and asks the host for a new session in it, leaving the open chat alone', async () => {
    await seedSessionProject('S1', { activeProjectId: PERSONAL.projectId, activeProjectName: 'Personal' });
    const onNewChatInProject = vi.fn();
    // Also on the Project the open chat already uses: a new chat is still a new chat.
    const { rerender, onSelectedChange } = await renderSurface({
      selectedId: PERSONAL.projectId, activeSessionId: 'S1', onNewChatInProject,
    });
    expect(screen.getByRole('button', { name: `Chat in ${PERSONAL.name}` })).toBeInTheDocument();
    rerender(<ProjectsSurface selectedId={OWNED.projectId} activeSessionId="S1"
      onNewChatInProject={onNewChatInProject} onSelectedChange={onSelectedChange} />);
    await flush();
    fireEvent.click(screen.getByRole('button', { name: `Chat in ${OWNED.name}` }));
    expect(onNewChatInProject).toHaveBeenCalledWith(OWNED.projectId, OWNED.name);
    expect(mocks.patchSessionProject).not.toHaveBeenCalled();
    expect(getSessionProject('S1')?.activeProjectId).toBe(PERSONAL.projectId);
  });

  it('shows no New chat button when the host offers no way to start one', async () => {
    await renderSurface({ selectedId: OWNED.projectId });
    expect(screen.queryByTestId('project-new-chat')).toBeNull();
  });
});

describe('ProjectsSurface Graph tab', () => {
  it('appears only where the graph is deployed', async () => {
    await renderSurface({ selectedId: OWNED.projectId });
    expect(screen.queryByRole('tab', { name: 'Graph' })).toBeNull();
  });

  it('switches between Assets and Graph, and a project change goes back to Assets', async () => {
    mocks.peekGraphEnabled.mockReturnValue(true);
    const { rerender, onSelectedChange } = await renderSurface({ selectedId: OWNED.projectId });
    expect(screen.getByRole('tab', { name: 'Assets' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByText(ASSETS_EMPTY_TITLE)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab', { name: 'Graph' }));
    expect(screen.getByTestId('graph-tab-mock')).toHaveTextContent(OWNED.projectId);
    expect(screen.queryByText(ASSETS_EMPTY_TITLE)).toBeNull();
    rerender(<ProjectsSurface onSelectedChange={onSelectedChange} selectedId={PERSONAL.projectId} />);
    await flush();
    expect(screen.queryByTestId('graph-tab-mock')).toBeNull();
    expect(screen.getByRole('tab', { name: 'Assets' })).toHaveAttribute('aria-selected', 'true');
  });
});

describe('ProjectsSurface graph focus (from a chat graph result)', () => {
  it('opens the Graph view of the requested project with the nodes to highlight', async () => {
    mocks.peekGraphEnabled.mockReturnValue(true);
    await renderSurface({ selectedId: OWNED.projectId,
      graphFocus: { projectId: OWNED.projectId, nodeIds: ['person:uma', 'asset:x'], nonce: 1 } });
    const tab = screen.getByTestId('graph-tab-mock');
    expect(tab).toHaveTextContent(OWNED.projectId);
    expect(tab).toHaveAttribute('data-focus', 'person:uma,asset:x');
    expect(screen.getByRole('tab', { name: 'Graph' })).toHaveAttribute('aria-selected', 'true');
  });

  it('ignores a focus request for a different project', async () => {
    mocks.peekGraphEnabled.mockReturnValue(true);
    await renderSurface({ selectedId: OWNED.projectId,
      graphFocus: { projectId: SHARED.projectId, nodeIds: ['x'], nonce: 1 } });
    expect(screen.queryByTestId('graph-tab-mock')).toBeNull();
  });
});

describe('ProjectsSurface Lineage tab (/config.lineageEnabled)', () => {
  const ASSET = { assetId: 'video:00000000-0000-4000-8000-000000000001', displayName: 'final.mp4', kind: 'video',
    status: 'ready', sourceSize: 1024 };

  it('appears only where lineage is on, and the asset menu offers Lineage only then', async () => {
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [ASSET] } });
    await renderSurface({ selectedId: OWNED.projectId });
    expect(screen.queryByRole('tab', { name: 'Lineage (beta)' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'More for final.mp4' }));
    expect(screen.queryByRole('menuitem', { name: 'Lineage' })).toBeNull();
  });

  it('switches to the whole-project Lineage view', async () => {
    mocks.peekLineageEnabled.mockReturnValue(true);
    await renderSurface({ selectedId: OWNED.projectId });
    expect(screen.getByRole('tab', { name: 'Assets' })).toHaveAttribute('aria-selected', 'true');
    fireEvent.click(screen.getByRole('tab', { name: 'Lineage (beta)' }));
    const tab = screen.getByTestId('lineage-tab-mock');
    expect(tab).toHaveTextContent(OWNED.projectId);
    expect(tab).toHaveAttribute('data-focus', '');
    expect(screen.queryByText(ASSETS_EMPTY_TITLE)).toBeNull();
  });

  it("an asset's ⋯ → Lineage opens the view focused on that asset", async () => {
    mocks.peekLineageEnabled.mockReturnValue(true);
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [ASSET] } });
    await renderSurface({ selectedId: OWNED.projectId });
    fireEvent.click(screen.getByRole('button', { name: 'More for final.mp4' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Lineage' }));
    expect(screen.getByRole('tab', { name: 'Lineage (beta)' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByTestId('lineage-tab-mock')).toHaveAttribute('data-focus', ASSET.assetId);
  });
});
