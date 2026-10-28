/**
 * @vitest-environment jsdom
 *
 * The Plus_Menu: `Attach files`, `Recent in <Active_Project>`, the web-search slot, and the `Project` section
 * (the project radios, open-time access read, not-found reset, and `Manage projects` that used to live on the
 * status-line Project chip, ported with the same behaviour assertions).
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import PlusMenu, { PLUS_MENU_LABEL, SESSION_LOCKED_REASON, type PlusMenuProjects, type PlusMenuProps } from './PlusMenu';
import { lostAccessNotice, type PickerListResult, type PickerProject } from './ProjectPicker';
import { RECENT_ASSET_LIMIT, pickRecentAssets, type RecentAssetsResult } from './RecentAssets';
import {
  UploadSheetMount,
  registerUploadSheet,
  registerWebSearchToggleSlot,
  type UploadSheetProps,
  type WebSearchToggleSlotProps,
} from './composerSlots';
import type { AssetView } from '../../projects/api';
import { onNewChatInProjectRequest } from '../../projects/newChatInProject';
import { EMOJI_OR_GLYPH } from '../../test-utils/glyphs';

const PERSONAL: PickerProject = { projectId: 'p0', name: 'Personal', role: 'owner', personal: true };
const PROJECTS: PickerProject[] = [
  { projectId: 'p2', name: 'trailer cuts', role: 'owner', personal: false },
  PERSONAL,
  { projectId: 'p1', name: 'Launch Q3', role: 'member', personal: false },
  { projectId: 'p3', name: 'Brand archive', role: 'member', personal: false },
];

function asset(n: number, over: Partial<AssetView> = {}): AssetView {
  return {
    assetId: `video:${n}`,
    displayName: `clip${n}.mp4`,
    kind: 'video',
    status: 'ready',
    sourceSize: 2 * 1024 * 1024,
    registeredAt: new Date(Date.UTC(2026, 0, 1, 0, n)).toISOString(),
    ...over,
  };
}

function okProjects(list: PickerProject[] = PROJECTS): () => Promise<PickerListResult> {
  return () => Promise.resolve({ ok: true, value: list });
}
function okAssets(list: AssetView[]): (pid: string) => Promise<RecentAssetsResult> {
  return () => Promise.resolve({ ok: true, value: { assets: list } });
}

function projectProps(overrides: Partial<PlusMenuProjects> = {}): PlusMenuProjects {
  return {
    activeProjectId: 'p1',
    activeProjectName: 'Launch Q3',
    loadProjects: okProjects(),
    onSelectProject: vi.fn(),
    onActiveProjectNotFound: vi.fn(),
    onManageProjects: vi.fn(),
    loadRecentAssets: okAssets([asset(1), asset(2)]),
    onAttachRecentAsset: vi.fn(),
    ...overrides,
  };
}

function setup(overrides: Partial<PlusMenuProps> = {}) {
  const props: PlusMenuProps = {
    sessionId: 's1',
    locked: false,
    onAttachToChat: vi.fn(),
    ...overrides,
  };
  const utils = render(<PlusMenu {...props} />);
  return { ...utils, props };
}
function setupWithProjects(projects: Partial<PlusMenuProjects> = {}, overrides: Partial<PlusMenuProps> = {}) {
  const p = projectProps(projects);
  const utils = setup({ ...overrides, projects: p });
  return { ...utils, projects: p };
}

const plusButton = () => screen.getByRole('button', { name: PLUS_MENU_LABEL });

function openMenu() {
  fireEvent.click(plusButton());
  return screen.getByRole('menu', { name: PLUS_MENU_LABEL });
}
/** Open, and wait for the open-time reads to answer. */
async function openLoaded() {
  const menu = openMenu();
  await within(menu).findAllByRole('menuitemradio');
  await flush();
  return menu;
}
async function flush() {
  await act(async () => {
    for (let i = 0; i < 6; i++) await Promise.resolve();
  });
}

/** Every item in roving-focus order, as the keyboard sees them. */
function items(menu: HTMLElement): HTMLElement[] {
  return Array.from(menu.querySelectorAll<HTMLElement>('[role^="menuitem"], [role="switch"]'));
}
function nameOf(el: HTMLElement): string {
  const label = el.getAttribute('aria-label');
  if (label) return label;
  const copy = el.cloneNode(true) as HTMLElement;
  copy.querySelectorAll('[aria-hidden="true"]').forEach((n) => n.remove());
  return copy.textContent ?? '';
}
function itemNames(menu: HTMLElement): string[] {
  return items(menu).map(nameOf);
}
const recentGroup = (menu: HTMLElement) => within(menu).getByRole('group', { name: /^Recent in / });
const projectGroup = (menu: HTMLElement) => within(menu).getByRole('group', { name: 'Project' });
const recentRows = (menu: HTMLElement) => within(menu).queryAllByTestId('plus-menu-recent-asset');

function WebSearchStub({ locked }: WebSearchToggleSlotProps) {
  return <button type="button" role="switch" aria-checked="false" aria-label="Web search" data-locked={String(locked)} />;
}

let unregister: Array<() => void> = [];
afterEach(() => {
  cleanup();
  unregister.forEach((u) => u());
  unregister = [];
});

describe('PlusMenu', () => {
  it('renders a + button that opens and closes the menu', () => {
    setup();
    const btn = plusButton();
    expect(btn).toHaveTextContent('+');
    expect(btn).toHaveAttribute('aria-haspopup', 'menu');
    expect(btn).toHaveAttribute('aria-expanded', 'false');
    openMenu();
    expect(btn).toHaveAttribute('aria-expanded', 'true');
    fireEvent.click(btn);
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('without Projects has Attach files then Web search, and nothing project-related', () => {
    unregister.push(registerWebSearchToggleSlot(WebSearchStub));
    setup();
    const menu = openMenu();
    expect(itemNames(menu)).toEqual(['Attach files', 'Web search']);
    // Each sits beside its icon: an aria-hidden svg, not a text glyph.
    expect(menu.querySelectorAll('svg[aria-hidden="true"]')).toHaveLength(2);
    expect(within(menu).queryAllByRole('menuitemradio')).toHaveLength(0);
    expect(within(menu).queryAllByRole('group')).toHaveLength(0);
    expect(menu).not.toHaveTextContent(/recent|manage projects|project/i);
  });

  it('shows only Attach files while no web-search toggle is registered and Projects are off', () => {
    setup();
    expect(itemNames(openMenu())).toEqual(['Attach files']);
  });

  it('orders the sections Attach files, Recent in <project>, Web search, Project', async () => {
    unregister.push(registerWebSearchToggleSlot(WebSearchStub));
    setupWithProjects();
    const menu = await openLoaded();
    expect(itemNames(menu)).toEqual([
      'Attach files',
      'clip2.mp4, 2 MB',
      'clip1.mp4, 2 MB',
      'View all',
      'Web search',
      'Personal, private, owner',
      'Brand archive, member',
      'Launch Q3, member',
      'trailer cuts, owner',
      'Manage projects',
    ]);
    // Section headings name the groups.
    const groups = within(menu).getAllByRole('group');
    expect(groups.map((g) => g.getAttribute('aria-labelledby') && document.getElementById(g.getAttribute('aria-labelledby')!)?.textContent))
      .toEqual(['Recent in Launch Q3', 'Project']);
    const attach = within(menu).getByRole('menuitem', { name: 'Attach files' });
    const recent = recentGroup(menu);
    const toggle = within(menu).getByRole('switch', { name: 'Web search' });
    const project = projectGroup(menu);
    const follows = (a: Node, b: Node) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
    expect(follows(attach, recent)).toBe(true);
    expect(follows(recent, toggle)).toBe(true);
    expect(follows(toggle, project)).toBe(true);
  });

  it('renders no emoji or unicode glyphs', async () => {
    unregister.push(registerWebSearchToggleSlot(WebSearchStub));
    setupWithProjects({ loadRecentAssets: okAssets([asset(1, { status: 'failed' }), asset(2, { kind: 'image' })]) }, { locked: true });
    const menu = await openLoaded();
    expect(menu.textContent ?? '').not.toMatch(EMOJI_OR_GLYPH);
  });

  it('moves focus across every section with Down/Up and Escape refocuses the button', async () => {
    unregister.push(registerWebSearchToggleSlot(WebSearchStub));
    setupWithProjects();
    const menu = await openLoaded();
    const order = items(menu);
    expect(order).toHaveLength(10);
    expect(document.activeElement).toBe(order[0]);
    for (let i = 1; i < order.length; i++) {
      fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' });
      expect(document.activeElement).toBe(order[i]);
      expect(order.filter((el) => el.tabIndex === 0)).toEqual([order[i]]);
    }
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' });
    expect(document.activeElement).toBe(order[0]);
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowUp' });
    expect(document.activeElement).toBe(order[order.length - 1]);
    fireEvent.keyDown(document.activeElement!, { key: 'Home' });
    expect(document.activeElement).toBe(order[0]);
    fireEvent.keyDown(document.activeElement!, { key: 'End' });
    expect(document.activeElement).toBe(order[order.length - 1]);
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(document.activeElement).toBe(plusButton());
  });

  it('closes the menu before calling onAttachToChat', () => {
    const { props } = setup();
    const menu = openMenu();
    fireEvent.click(within(menu).getByRole('menuitem', { name: 'Attach files' }));
    expect(props.onAttachToChat).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('in a locked session shows Attach files aria-disabled with "Session locked", focusable but inert', () => {
    unregister.push(registerWebSearchToggleSlot(WebSearchStub));
    const { props } = setup({ locked: true });
    const menu = openMenu();
    const attach = within(menu).getByRole('menuitem', { name: /^Attach files/ });
    expect(attach).toHaveAttribute('aria-disabled', 'true');
    expect(attach).toHaveTextContent(SESSION_LOCKED_REASON);
    fireEvent.click(attach);
    expect(props.onAttachToChat).not.toHaveBeenCalled();
    expect(screen.getByRole('menu')).toBeInTheDocument();
    expect(document.activeElement).toBe(attach);
    // The toggle slot is told the session is locked.
    expect(within(menu).getByRole('switch', { name: 'Web search' })).toHaveAttribute('data-locked', 'true');
  });
});

describe('PlusMenu: Recent in <Active_Project>', () => {
  it('lists the 5 newest assets, excluding removed ones, with kind icon, name, size, and a badge only when not ready/queued', async () => {
    const list = [
      asset(1),
      asset(7, { status: 'removed' }),
      asset(3, { status: 'queued', kind: 'document', displayName: 'notes.pdf', sourceSize: 2048 }),
      asset(6, { status: 'analyzing', kind: 'audio', displayName: 'vo.wav' }),
      asset(2),
      asset(5, { status: 'failed', kind: 'image', displayName: 'still.png' }),
      asset(4, { status: 'indexed' }),
    ];
    setupWithProjects({ loadRecentAssets: okAssets(list) });
    const menu = await openLoaded();
    const rows = recentRows(menu);
    expect(rows).toHaveLength(RECENT_ASSET_LIMIT);
    expect(rows.map((r) => r.querySelector('span:nth-child(2)')?.textContent)).toEqual([
      'vo.wav',
      'still.png',
      'clip4.mp4',
      'notes.pdf',
      'clip2.mp4',
    ]);
    expect(menu).not.toHaveTextContent('clip7.mp4');
    expect(rows[0]).toHaveTextContent('Analyzing');
    expect(rows[0]).toHaveAccessibleName('vo.wav, 2 MB, Analyzing');
    expect(rows[1]).toHaveTextContent('Failed');
    expect(rows[2]).not.toHaveTextContent('Ready');
    expect(rows[3]).toHaveTextContent('2 KB');
    expect(rows[3]).not.toHaveTextContent('Queued');
    for (const r of rows) expect(r.querySelector('svg[aria-hidden="true"]')).not.toBeNull();
  });

  it('pickRecentAssets keeps the route order where rows carry no registeredAt', () => {
    const rows = [asset(1), asset(2), asset(3)].map((a) => ({ ...a, registeredAt: undefined }));
    expect(pickRecentAssets(rows).map((a) => a.assetId)).toEqual(['video:1', 'video:2', 'video:3']);
  });

  it('pickRecentAssets keeps a pinned sample (0-demo.mp4) first, ahead of newer files', () => {
    const rows = [
      { ...asset(1), registeredAt: '2026-10-28T09:00:00.000Z' },
      { ...asset(2), registeredAt: '2026-10-28T08:00:00.000Z' },
      { ...asset(3), registeredAt: '2026-01-01T00:00:00.000Z', pinned: true },
    ];
    expect(pickRecentAssets(rows).map((a) => a.assetId)).toEqual(['video:3', 'video:1', 'video:2']);
  });

  it('never offers a "this chat only" file: a hidden session asset belongs to its own chat (D2)', () => {
    const rows = [
      { ...asset(1), visibility: 'session' as const, sessionId: 'S9' },
      { ...asset(2), visibility: 'project' as const },
      asset(3),
    ];
    expect(pickRecentAssets(rows).map((a) => a.assetId).sort()).toEqual(['video:2', 'video:3']);
  });

  it('shows a row’s folder path as secondary text when it has one', async () => {
    const list = [
      asset(2, { folder: 'Footage/Day 1' }),
      asset(1),
    ].map((a) => ({ ...a, registeredAt: undefined }));
    setupWithProjects({ loadRecentAssets: okAssets(list) });
    const menu = await openLoaded();
    const [inFolder, loose] = recentRows(menu);
    expect(within(inFolder).getByTestId('plus-menu-asset-folder')).toHaveTextContent('Footage/Day 1');
    expect(inFolder.querySelector('span:nth-child(2)')?.textContent).toBe('clip2.mp4'); // the name stays first
    expect(inFolder).toHaveAccessibleName('clip2.mp4, in Footage/Day 1, 2 MB');
    expect(inFolder).toHaveAttribute('title', 'Footage/Day 1/clip2.mp4');
    expect(within(loose).queryByTestId('plus-menu-asset-folder')).toBeNull();
  });

  it('reads the first page of the Active_Project when the menu opens, never while closed', async () => {
    const loadRecentAssets = vi.fn(okAssets([asset(1)]));
    setupWithProjects({ loadRecentAssets });
    await flush();
    expect(loadRecentAssets).not.toHaveBeenCalled();
    await openLoaded();
    expect(loadRecentAssets).toHaveBeenCalledTimes(1);
    expect(loadRecentAssets).toHaveBeenCalledWith('p1');
  });

  it('shows the cached rows at once on reopen while the read refreshes them', async () => {
    let calls = 0;
    let release: (r: RecentAssetsResult) => void = () => {};
    const loadRecentAssets = vi.fn((): Promise<RecentAssetsResult> => {
      calls += 1;
      if (calls === 1) return Promise.resolve({ ok: true, value: { assets: [asset(1)] } });
      return new Promise((r) => { release = r; });
    });
    setupWithProjects({ loadRecentAssets });
    let menu = await openLoaded();
    expect(recentRows(menu)).toHaveLength(1);
    fireEvent.click(plusButton());
    menu = openMenu();
    // The second read is still pending: the cached row is shown, not a loading note.
    expect(recentRows(menu)).toHaveLength(1);
    expect(within(recentGroup(menu)).queryByText('Loading files…')).toBeNull();
    await act(async () => release({ ok: true, value: { assets: [asset(2), asset(1)] } }));
    expect(recentRows(menu)).toHaveLength(2);
    expect(loadRecentAssets).toHaveBeenCalledTimes(2);
  });

  it('shows a loading note, then "No files yet in <project>" for an empty project', async () => {
    let release: (r: RecentAssetsResult) => void = () => {};
    setupWithProjects({ loadRecentAssets: () => new Promise((r) => { release = r; }) });
    const menu = openMenu();
    expect(within(recentGroup(menu)).getByText('Loading files…')).toBeInTheDocument();
    await act(async () => release({ ok: true, value: { assets: [asset(1, { status: 'removed' })] } }));
    expect(within(recentGroup(menu)).getByText('No files yet in Launch Q3')).toBeInTheDocument();
    expect(recentRows(menu)).toHaveLength(0);
    expect(within(recentGroup(menu)).getByRole('menuitem', { name: 'View all' })).toBeInTheDocument();
  });

  it('shows an error note when the read fails, and View all still opens the Projects surface on the project', async () => {
    const { projects } = setupWithProjects({ loadRecentAssets: () => Promise.resolve({ ok: false }) });
    const menu = await openLoaded();
    expect(within(recentGroup(menu)).getByText('Files could not be loaded.')).toBeInTheDocument();
    fireEvent.click(within(recentGroup(menu)).getByRole('menuitem', { name: 'View all' }));
    expect(projects.onManageProjects).toHaveBeenCalledWith('p1');
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('a rejected read is the error note too', async () => {
    setupWithProjects({ loadRecentAssets: () => Promise.reject(new Error('down')) });
    const menu = await openLoaded();
    expect(within(recentGroup(menu)).getByText('Files could not be loaded.')).toBeInTheDocument();
  });

  it('clicking a row closes the menu and attaches that asset to the Active_Project', async () => {
    const { projects } = setupWithProjects();
    const menu = await openLoaded();
    fireEvent.click(recentRows(menu)[0]);
    expect(projects.onAttachRecentAsset).toHaveBeenCalledTimes(1);
    expect(projects.onAttachRecentAsset).toHaveBeenCalledWith(
      expect.objectContaining({ assetId: 'video:2', displayName: 'clip2.mp4' }),
      { id: 'p1', name: 'Launch Q3' },
    );
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(document.activeElement).toBe(plusButton());
  });

  it('is disabled while the session is locked: rows are aria-disabled and inert, with the reason shown', async () => {
    const { projects } = setupWithProjects({}, { locked: true });
    const menu = await openLoaded();
    const rows = recentRows(menu);
    expect(rows).toHaveLength(2);
    for (const r of rows) expect(r).toHaveAttribute('aria-disabled', 'true');
    expect(rows[0]).toHaveAccessibleName('clip2.mp4, 2 MB, Session locked');
    expect(recentGroup(menu)).toHaveTextContent(SESSION_LOCKED_REASON);
    fireEvent.click(rows[0]);
    expect(projects.onAttachRecentAsset).not.toHaveBeenCalled();
    expect(screen.getByRole('menu')).toBeInTheDocument();
  });

  it('an asset still uploading is aria-disabled', async () => {
    const { projects } = setupWithProjects({ loadRecentAssets: okAssets([asset(1, { status: 'uploading' })]) });
    const menu = await openLoaded();
    const row = recentRows(menu)[0];
    expect(row).toHaveAttribute('aria-disabled', 'true');
    expect(row).toHaveTextContent('Uploading');
    fireEvent.click(row);
    expect(projects.onAttachRecentAsset).not.toHaveBeenCalled();
  });
});

describe('PlusMenu: Project section (moved from the Project chip)', () => {
  it('lists Projects Personal first then by name, then Manage projects', async () => {
    setupWithProjects();
    const menu = await openLoaded();
    expect(Array.from(projectGroup(menu).querySelectorAll<HTMLElement>('[role^="menuitem"]')).map(nameOf)).toEqual([
      'Personal, private, owner',
      'Brand archive, member',
      'Launch Q3, member',
      'trailer cuts, owner',
      'Manage projects',
    ]);
  });

  it('marks the Active_Project checked, shows each role badge, and draws a lock (not an emoji) on Personal', async () => {
    setupWithProjects();
    const menu = await openLoaded();
    const personal = within(menu).getByRole('menuitemradio', { name: /^Personal, private/ });
    expect(personal).toHaveTextContent('owner');
    expect(personal).toHaveAttribute('aria-checked', 'false');
    expect(personal.querySelector('svg[aria-hidden="true"]')).not.toBeNull();
    const active = within(menu).getByRole('menuitemradio', { name: 'Launch Q3, member' });
    expect(active).toHaveAttribute('aria-checked', 'true');
    expect(active).toHaveTextContent('member');
    expect(projectGroup(menu).textContent ?? '').not.toMatch(EMOJI_OR_GLYPH);
  });

  it('selects a Project through onSelectProject, closes, and ignores the already-active one', async () => {
    const { projects } = setupWithProjects();
    let menu = await openLoaded();
    fireEvent.click(within(menu).getByRole('menuitemradio', { name: 'Brand archive, member' }));
    expect(projects.onSelectProject).toHaveBeenCalledTimes(1);
    expect(projects.onSelectProject).toHaveBeenCalledWith('p3');
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(document.activeElement).toBe(plusButton());

    menu = await openLoaded();
    fireEvent.click(within(menu).getByRole('menuitemradio', { name: 'Launch Q3, member' }));
    expect(projects.onSelectProject).toHaveBeenCalledTimes(1);
  });

  it('on a session with a turn, other projects start a new chat in them instead of switching (D1)', async () => {
    const onStartNewChatInProject = vi.fn();
    const { projects } = setupWithProjects({ projectFixed: true, onStartNewChatInProject });
    let menu = await openLoaded();
    const group = projectGroup(menu);
    expect(within(group).getByTestId('project-fixed-note')).toHaveTextContent('This chat stays in Launch Q3.');
    // The active project stays the one checked radio; every other row is a "new chat" action.
    expect(within(group).getAllByRole('menuitemradio').map(nameOf)).toEqual(['Launch Q3, member']);
    expect(within(group).getByRole('menuitemradio', { name: 'Launch Q3, member' })).toHaveAttribute('aria-checked', 'true');
    const row = within(group).getByRole('menuitem', { name: 'Start a new chat in Brand archive' });
    expect(row).toHaveTextContent('New chat');
    fireEvent.click(row);
    expect(onStartNewChatInProject).toHaveBeenCalledWith('p3', 'Brand archive');
    expect(projects.onSelectProject).not.toHaveBeenCalled();
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();

    menu = await openLoaded();
    fireEvent.click(within(menu).getByRole('menuitemradio', { name: 'Launch Q3, member' }));
    expect(onStartNewChatInProject).toHaveBeenCalledTimes(1);
    expect(projects.onSelectProject).not.toHaveBeenCalled();
  });

  it('by default asks the host for the new chat through the projects:new-chat event', async () => {
    const seen: unknown[] = [];
    const off = onNewChatInProjectRequest((d) => seen.push(d));
    try {
      setupWithProjects({ projectFixed: true });
      const menu = await openLoaded();
      fireEvent.click(within(menu).getByRole('menuitem', { name: 'Start a new chat in Brand archive' }));
      expect(seen).toEqual([{ projectId: 'p3', projectName: 'Brand archive' }]);
    } finally {
      off();
    }
  });

  it('without a turn shows no fixed note and keeps every row a radio', async () => {
    setupWithProjects({ projectFixed: false });
    const menu = await openLoaded();
    expect(within(menu).queryByTestId('project-fixed-note')).toBeNull();
    expect(within(projectGroup(menu)).queryAllByRole('menuitem', { name: /^Start a new chat/ })).toHaveLength(0);
  });

  it('reads the list on every open', async () => {
    const loadProjects = vi.fn(okProjects());
    setupWithProjects({ loadProjects });
    await openLoaded();
    fireEvent.click(plusButton());
    await openLoaded();
    expect(loadProjects).toHaveBeenCalledTimes(2);
  });

  it('shows a loading note until the open-time read answers, and an error note when it fails', async () => {
    let resolve!: (r: PickerListResult) => void;
    const { unmount } = setupWithProjects({ loadProjects: () => new Promise((r) => { resolve = r; }) });
    let menu = openMenu();
    expect(within(projectGroup(menu)).getByText('Loading projects…')).toBeInTheDocument();
    expect(within(menu).getByRole('menuitem', { name: 'Manage projects' })).toBeInTheDocument();
    resolve({ ok: true, value: PROJECTS });
    expect(await within(menu).findByRole('menuitemradio', { name: 'Launch Q3, member' })).toBeInTheDocument();
    unmount();

    setupWithProjects({ loadProjects: () => Promise.resolve({ ok: false }) });
    menu = openMenu();
    expect(await within(projectGroup(menu)).findByText('Projects could not be loaded.')).toBeInTheDocument();
  });

  it('reports a not-found Active_Project with the Personal_Project from the same read', async () => {
    const list = PROJECTS.filter((p) => p.projectId !== 'p1');
    const onActiveProjectNotFound = vi.fn();
    setupWithProjects({ loadProjects: okProjects(list), onActiveProjectNotFound });
    await openLoaded();
    await waitFor(() => expect(onActiveProjectNotFound).toHaveBeenCalledTimes(1));
    expect(onActiveProjectNotFound).toHaveBeenCalledWith({ projectId: 'p1', projectName: 'Launch Q3', personal: PERSONAL });
    expect(lostAccessNotice('Launch Q3')).toBe(
      'You no longer have access to Launch Q3. This session has switched to your Personal project.',
    );
  });

  it('Manage projects closes the menu and opens the Projects surface on the Active_Project', async () => {
    const { projects } = setupWithProjects();
    const menu = await openLoaded();
    fireEvent.click(within(menu).getByRole('menuitem', { name: 'Manage projects' }));
    expect(projects.onManageProjects).toHaveBeenCalledWith('p1');
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('offers no create, rename, invite, or delete control', async () => {
    setupWithProjects();
    const menu = await openLoaded();
    expect(menu).not.toHaveTextContent(/create|new project|rename|invite|delete/i);
  });
});

describe('composerSlots', () => {
  it('UploadSheetMount renders nothing until a sheet is registered, then passes its props', () => {
    const props: UploadSheetProps = {
      sessionId: 'S1',
      projectId: 'p1',
      projectName: 'Launch Q3',
      addToProject: true,
      locked: false,
      onAddToProjectChange: vi.fn(),
      onClose: vi.fn(),
      onViewInProjects: vi.fn(),
    };
    const { container } = render(<UploadSheetMount {...props} />);
    expect(container).toBeEmptyDOMElement();
    function Sheet(p: UploadSheetProps) {
      return <div role="dialog" aria-label={`Add files to ${p.projectName}`} data-pid={p.projectId} />;
    }
    act(() => {
      unregister.push(registerUploadSheet(Sheet));
    });
    expect(screen.getByRole('dialog', { name: 'Add files to Launch Q3' })).toHaveAttribute('data-pid', 'p1');
  });
});

describe('PlusMenu: search the Active_Project by name', () => {
  it('has no search box unless the host can search', async () => {
    setupWithProjects({ loadRecentAssets: okAssets([asset(1)]) });
    const menu = await openLoaded();
    expect(within(menu).queryByTestId('plus-menu-asset-search')).toBeNull();
  });

  it('typing replaces the recent rows with name matches; a match attaches; clearing restores Recent', async () => {
    const searchAssets = vi.fn(async (_pid: string, q: string): Promise<RecentAssetsResult> => ({
      ok: true,
      value: { assets: q === 'meridian'
        ? [asset(41, { displayName: 'meridian_trailer.mp4' }), asset(42, { displayName: 'old.mp4', status: 'removed' })]
        : [] },
    }));
    const onAttachRecentAsset = vi.fn();
    setupWithProjects({ loadRecentAssets: okAssets([asset(1), asset(2)]), searchAssets, onAttachRecentAsset });
    const menu = await openLoaded();
    expect(recentRows(menu)).toHaveLength(2);
    const box = within(menu).getByTestId('plus-menu-asset-search');
    fireEvent.change(box, { target: { value: 'meridian' } });
    await waitFor(() => expect(searchAssets).toHaveBeenCalledWith('p1', 'meridian'));
    await waitFor(() => expect(recentRows(menu).map((r) => r.querySelector('span:nth-child(2)')?.textContent))
      .toEqual(['meridian_trailer.mp4']));
    expect(menu).toHaveTextContent('Files in');

    fireEvent.change(box, { target: { value: 'zzz' } });
    await waitFor(() => expect(menu).toHaveTextContent('No files match'));

    fireEvent.change(box, { target: { value: '' } });
    await flush();
    expect(recentRows(menu)).toHaveLength(2);

    fireEvent.change(box, { target: { value: 'meridian' } });
    await waitFor(() => expect(recentRows(menu)).toHaveLength(1));
    fireEvent.click(recentRows(menu)[0]);
    expect(onAttachRecentAsset).toHaveBeenCalledWith(expect.objectContaining({ displayName: 'meridian_trailer.mp4' }),
      { id: 'p1', name: expect.any(String) });
  });

  it('Home and End edit the text instead of moving the menu focus', async () => {
    setupWithProjects({ loadRecentAssets: okAssets([asset(1)]), searchAssets: vi.fn(async () => ({ ok: true as const, value: { assets: [] } })) });
    const menu = await openLoaded();
    const box = within(menu).getByTestId('plus-menu-asset-search');
    box.focus();
    fireEvent.keyDown(box, { key: 'End' });
    fireEvent.keyDown(box, { key: 'Home' });
    expect(document.activeElement).toBe(box);
  });
});
