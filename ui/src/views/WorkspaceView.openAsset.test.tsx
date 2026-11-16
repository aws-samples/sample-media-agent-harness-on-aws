/**
 * @vitest-environment jsdom
 *
 * WorkspaceView `lab:open-asset` routing (Component 5, media-harness-search-qa-rendering; Req 2.8, 2.12).
 *
 * A verified Citation's open (and a Moment's Expand) dispatches `lab:open-asset` on `window`.
 * WorkspaceView listens for it and appends a new render tab for the ACTIVE session carrying an
 * `asset-player` view labeled with the Display_Name, whose data holds an Asset_Reference and NO
 * media URL (Req 2.12) — AssetPlayer re-mints on mount. An event for a non-active session is dropped.
 *
 * The view is persisted (lineage spec R8.1): POSTed to the session's render store, keyed by the
 * manifest key the server returns, so it survives refresh and every later render-list merge.
 *
 * The heavy panels are stubbed; SessionRendererPanel's stub captures the `history` it receives so the
 * test can read the appended tab's block data.
 *
 * Covers Requirements 2.8, 2.12.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import '@testing-library/jest-dom';
import React from 'react';
import { render, act, fireEvent, screen } from '@testing-library/react';
import { OPEN_ASSET_EVENT } from '../projects/openCitation';
import type { RenderHistoryEntry } from '../components/renderer/types';

const mocks = vi.hoisted(() => ({
  useLabContext: vi.fn(),
  listSessions: vi.fn(),
  createSession: vi.fn(),
  deleteSession: vi.fn(),
  updateSessionRenderHistory: vi.fn(),
  updateSessionActivity: vi.fn(),
  editAsset: vi.fn(),
  discardChanges: vi.fn(),
  getSessionRenders: vi.fn(),
  createSessionRender: vi.fn(),
  deleteSessionRender: vi.fn(),
}));

// SessionRendererPanel stub records the history it was handed each render.
let rendererHistory: RenderHistoryEntry[] | null = null;

vi.mock('../LabContext', () => ({ useLabContext: mocks.useLabContext }));
vi.mock('../lab-api', () => ({
  listSessions: mocks.listSessions,
  createSession: mocks.createSession,
  deleteSession: mocks.deleteSession,
  updateSessionRenderHistory: mocks.updateSessionRenderHistory,
  updateSessionActivity: mocks.updateSessionActivity,
  editAsset: mocks.editAsset,
  discardChanges: mocks.discardChanges,
  getSessionRenders: mocks.getSessionRenders,
  createSessionRender: mocks.createSessionRender,
  deleteSessionRender: mocks.deleteSessionRender,
  logPersistFailure: vi.fn(),
  resolveAgentConfig: vi.fn(() => new Promise(() => {})),
}));
vi.mock('../hooks/useJobs', () => ({
  jobsStore: { subscribeNewJob: vi.fn(() => () => {}), getSnapshot: () => ({ jobsById: new Map() }) },
  useJobs: () => ({ jobs: [] }),
}));
vi.mock('../auth/useCurrentUser', () => ({
  useCurrentUser: () => ({
    mode: 'dev', displayName: 'Local Dev', email: undefined, initials: 'LD',
    sub: 'local-dev-user', signOut: vi.fn(),
  }),
}));
vi.mock('../auth/isGatewayAdmin', () => ({ useIsGatewayAdmin: () => false }));
vi.mock('../config/branding', () => ({
  BRAND_NAME: 'TestBrand',
  getActiveLogo: () => ({ src: 'logo.png', alt: 'logo', borderRadius: 0 }),
}));
vi.mock('../components/ChatCanvas', () => ({ default: () => <div data-testid="chat-canvas">chat</div> }));
vi.mock('../components/CenterEditor', () => ({ default: () => <div data-testid="center-editor" /> }));
vi.mock('../components/SessionRendererPanel', () => ({
  default: (props: { history: RenderHistoryEntry[] }) => {
    rendererHistory = props.history;
    return <div data-testid="renderer-panel" />;
  },
}));
vi.mock('../components/JobsPanel', () => ({
  default: () => <div data-testid="jobs-panel" />,
  rollUpRuns: () => [],
}));
vi.mock('../components/SpecialtyAgentEditor', () => ({ default: () => <div data-testid="specialty-agent-editor" /> }));

import WorkspaceView from './WorkspaceView';

function labContextValue(overrides: Record<string, unknown> = {}) {
  return {
    sessionId: 'session-1',
    switchSession: vi.fn(),
    editingTarget: null,
    setEditingTarget: vi.fn(),
    specialtyAgents: [],
    publishedSpecialtyAgents: [],
    loadingCategories: { skill: false, 'specialty-agent': false, 'tool-collection': false, workflow: false, hook: false },
    refreshCategory: vi.fn().mockResolvedValue(undefined),
    ensureCategoryLoaded: vi.fn().mockResolvedValue(undefined),
    skills: [],
    publishedSkills: [],
    ...overrides,
  };
}

async function flush() {
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
}

function openAssetDetail(sessionId: string) {
  return {
    ref: { project_id: 'p1', asset_id: 'a1', frame_s3_key: null },
    kind: 'video' as const,
    displayName: 'nba.mp4',
    startMs: 5000,
    sessionId,
    url: 'https://cdn.example/transient-signed-url.mp4',
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  rendererHistory = null;
  mocks.listSessions.mockResolvedValue([]);
  mocks.createSession.mockResolvedValue(undefined);
  mocks.deleteSession.mockResolvedValue(undefined);
  mocks.updateSessionRenderHistory.mockResolvedValue(undefined);
  mocks.updateSessionActivity.mockResolvedValue(undefined);
  mocks.getSessionRenders.mockResolvedValue([]);
  mocks.createSessionRender.mockImplementation(async (sid: string, m: { id: string }) => `artifacts/${sid}/render/${m.id}.json`);
  mocks.deleteSessionRender.mockResolvedValue(undefined);
  mocks.useLabContext.mockReturnValue(labContextValue());
});

describe('WorkspaceView — lab:open-asset routing', () => {
  it('appends an asset-player tab labeled with the Display_Name, whose block data has no URL', async () => {
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    await flush();

    await act(async () => {
      window.dispatchEvent(new CustomEvent(OPEN_ASSET_EVENT, { detail: openAssetDetail('session-1') }));
    });
    await flush();

    expect(rendererHistory).not.toBeNull();
    expect(rendererHistory).toHaveLength(1);
    const entry = rendererHistory![0];
    expect(entry.label).toBe('nba.mp4');
    expect(entry.status).toBe('ready');

    const block = entry.blocks[0];
    expect(block.template).toBe('manifest-router');
    expect(block.title).toBe('nba.mp4');
    // The view is an asset-player manifest carrying an Asset_Reference, never a URL (Req 2.12).
    expect(block.data.manifest).toMatchObject({
      template: 'asset-player',
      ref: { project_id: 'p1', asset_id: 'a1', frame_s3_key: null },
      kind: 'video',
      displayName: 'nba.mp4',
      startMs: 5000,
    });
    const serialized = JSON.stringify(block.data);
    expect(serialized).not.toContain('https://');
    expect(serialized).not.toContain('transient-signed-url');
  });

  it('persists the view (POST) and adopts the returned manifest key as the tab identity', async () => {
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    await flush();
    await act(async () => {
      window.dispatchEvent(new CustomEvent(OPEN_ASSET_EVENT, { detail: openAssetDetail('session-1') }));
    });
    await flush();

    expect(mocks.createSessionRender).toHaveBeenCalledTimes(1);
    const [sid, manifest] = mocks.createSessionRender.mock.calls[0];
    expect(sid).toBe('session-1');
    expect(manifest).toMatchObject({ template: 'asset-player', displayName: 'nba.mp4', ref: { asset_id: 'a1' } });
    expect(JSON.stringify(manifest)).not.toContain('https://');

    const entry = rendererHistory![0];
    const key = `artifacts/session-1/render/${manifest.id}.json`;
    expect(entry.key).toBe(key);
    expect(entry.blocks[0].data.manifestUrl).toBe(`/api/artifact?key=${encodeURIComponent(key)}`);
    // Not listed by the server yet → still kept as client-authored.
    expect(entry.clientOnly).toBe(true);
  });

  it("a Moments search's results (lab:open-view) open and save as a view named by its query", async () => {
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    await flush();
    const manifest = { id: 'view-m1', template: 'clip-gallery', title: 'Moments', subjectIds: ['title:Moments'],
      galleryMoments: { v: 1, project_id: 'p1', turn_id: 't', basis: 'searched', truncated: false, items: [], query: 'rain' } };
    await act(async () => {
      window.dispatchEvent(new CustomEvent('lab:open-view', { detail: { sessionId: 'other-chat', label: 'Moments', manifest } }));
    });
    await flush();
    expect(mocks.createSessionRender).not.toHaveBeenCalled(); // another chat's search: not opened here
    await act(async () => {
      window.dispatchEvent(new CustomEvent('lab:open-view', { detail: { sessionId: 'session-1', label: 'Moments', manifest } }));
    });
    await flush();
    expect(mocks.createSessionRender).toHaveBeenCalledWith('session-1', manifest);
    const entry = rendererHistory![rendererHistory!.length - 1];
    expect(entry).toMatchObject({ label: 'Moments', note: '“rain”', subjectIds: ['title:Moments'] });
  });

  it('a Moment Expand opens the FULL asset with its span selected: viewState.range on the block and the POST', async () => {
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    await flush();
    await act(async () => {
      window.dispatchEvent(new CustomEvent(OPEN_ASSET_EVENT, {
        detail: { ...openAssetDetail('session-1'), startMs: 74000, range: { start: 74, end: 96 } },
      }));
    });
    await flush();
    const [, manifest] = mocks.createSessionRender.mock.calls[0];
    expect(manifest).toMatchObject({ template: 'asset-player', startMs: 74000, origin: 'moment',
      viewState: { range: { start: 74, end: 96 } } });
    expect(rendererHistory![0].blocks[0].data.manifest).toMatchObject({ viewState: { range: { start: 74, end: 96 } } });
  });

  it('a Citation open (no range) carries no viewState and stays origin citation', async () => {
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    await flush();
    await act(async () => {
      window.dispatchEvent(new CustomEvent(OPEN_ASSET_EVENT, { detail: openAssetDetail('session-1') }));
    });
    await flush();
    const [, manifest] = mocks.createSessionRender.mock.calls[0];
    expect(manifest).not.toHaveProperty('viewState');
    expect(manifest.origin).toBe('citation');
  });

  it('the Expand tab survives a later server render list (the reconcile no longer drops it)', async () => {
    let resolveList: (v: unknown[]) => void = () => {};
    mocks.getSessionRenders.mockImplementation(() => new Promise((r) => { resolveList = r; }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    await flush();
    await act(async () => {
      window.dispatchEvent(new CustomEvent(OPEN_ASSET_EVENT, { detail: openAssetDetail('session-1') }));
    });
    await flush();
    const expandKey = rendererHistory![0].key;

    // An unrelated render lands and the server list arrives WITHOUT the view (not listed yet).
    await act(async () => {
      resolveList([{ key: 'artifacts/session-1/render/shots.json', label: 'Shots', createdAt: '2026-10-08T09:00:00Z' }]);
    });
    await flush();
    expect(rendererHistory!.map(e => e.key)).toEqual(['artifacts/session-1/render/shots.json', expandKey]);
  });

  it('once the server lists the saved view, it replaces the client tab in place (same key, same block)', async () => {
    let resolveList: (v: unknown[]) => void = () => {};
    mocks.getSessionRenders.mockImplementation(() => new Promise((r) => { resolveList = r; }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    await flush();
    await act(async () => {
      window.dispatchEvent(new CustomEvent(OPEN_ASSET_EVENT, { detail: openAssetDetail('session-1') }));
    });
    await flush();
    const before = rendererHistory![0];
    await act(async () => {
      resolveList([{ key: before.key, label: 'nba.mp4', createdAt: '2026-10-08T09:00:00Z' }]);
    });
    await flush();
    expect(rendererHistory).toHaveLength(1);
    const after = rendererHistory![0];
    expect(after.key).toBe(before.key);
    expect(after.clientOnly).toBeUndefined();
    expect(after.blocks).toBe(before.blocks); // reused → the mounted player is not reloaded
  });

  it('a view whose save fails stays as a client-only tab that merges keep', async () => {
    mocks.createSessionRender.mockRejectedValue(new Error('404'));
    let resolveList: (v: unknown[]) => void = () => {};
    mocks.getSessionRenders.mockImplementation(() => new Promise((r) => { resolveList = r; }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    await flush();
    await act(async () => {
      window.dispatchEvent(new CustomEvent(OPEN_ASSET_EVENT, { detail: openAssetDetail('session-1') }));
    });
    await flush();
    expect(rendererHistory![0].clientOnly).toBe(true);
    await act(async () => {
      resolveList([{ key: 'artifacts/session-1/render/other.json', createdAt: '2026-10-08T09:00:00Z' }]);
    });
    await flush();
    expect(rendererHistory).toHaveLength(2);
    expect(rendererHistory!.some(e => e.clientOnly && e.label === 'nba.mp4')).toBe(true);
  });

  it('drops a lab:open-asset event addressed to a non-active session', async () => {
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    await flush();

    await act(async () => {
      window.dispatchEvent(new CustomEvent(OPEN_ASSET_EVENT, { detail: openAssetDetail('other-session') }));
    });
    await flush();

    // No tab was appended for the viewed session, so the renderer never mounted.
    expect(rendererHistory).toBeNull();
    expect(mocks.updateSessionRenderHistory).not.toHaveBeenCalled();
  });
});

describe('WorkspaceView — deleting a session', () => {
  it('deleting the OPEN session moves to a new one, so a refresh cannot restore it', async () => {
    const switchSession = vi.fn();
    mocks.useLabContext.mockReturnValue(labContextValue({ switchSession }));
    mocks.listSessions.mockResolvedValue([
      { sessionId: 'session-1', title: 'Doomed', createdAt: '2026-10-07T10:00:00Z', lastActivityAt: '2026-10-07T10:00:00Z' },
      { sessionId: 'session-2', title: 'Keeper', createdAt: '2026-10-07T09:00:00Z', lastActivityAt: '2026-10-07T09:00:00Z' },
    ]);
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    await flush();
    switchSession.mockClear();

    fireEvent.click(screen.getByRole('button', { name: 'Delete session Doomed' }));
    await flush();

    expect(mocks.deleteSession).toHaveBeenCalledWith('session-1');
    expect(switchSession).toHaveBeenCalledTimes(1);
    expect(switchSession.mock.calls[0][0]).not.toBe('session-1');
    expect(screen.queryByRole('button', { name: 'Delete session Doomed' })).toBeNull();
  });

  it('deleting another session leaves the open one alone', async () => {
    const switchSession = vi.fn();
    mocks.useLabContext.mockReturnValue(labContextValue({ switchSession }));
    mocks.listSessions.mockResolvedValue([
      { sessionId: 'session-1', title: 'Open', createdAt: '2026-10-07T10:00:00Z', lastActivityAt: '2026-10-07T10:00:00Z' },
      { sessionId: 'session-2', title: 'Old', createdAt: '2026-10-07T09:00:00Z', lastActivityAt: '2026-10-07T09:00:00Z' },
    ]);
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    await flush();
    switchSession.mockClear();

    fireEvent.click(screen.getByRole('button', { name: 'Delete session Old' }));
    await flush();

    expect(mocks.deleteSession).toHaveBeenCalledWith('session-2');
    expect(switchSession).not.toHaveBeenCalled();
  });
});
