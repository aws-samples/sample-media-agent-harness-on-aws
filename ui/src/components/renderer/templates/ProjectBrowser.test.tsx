/**
 * @vitest-environment jsdom
 *
 * `project-browser` — the agent's browse_project tab: the project grid in browse-only mode inside the Renderer.
 * Registered in every template table; nothing that changes the Project is reachable; the agent's filter seeds the
 * grid; opening a media file opens an asset-player tab in the CHAT's session.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import '@testing-library/jest-dom';
import React from 'react';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';

const mocks = vi.hoisted(() => ({
  listAssets: vi.fn(),
  searchAssets: vi.fn(),
  getProject: vi.fn(),
  requestUrls: vi.fn(),
  openAssetAt: vi.fn(),
  openProject: vi.fn(),
}));
vi.mock('../../../projects/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../projects/api')>()),
  listAssets: mocks.listAssets,
  searchAssets: mocks.searchAssets,
  getProject: mocks.getProject,
}));
vi.mock('../../../projects/assetUrlBatcher', () => ({ request: mocks.requestUrls }));
vi.mock('../../../projects/openCitation', () => ({ openAssetAt: mocks.openAssetAt }));
vi.mock('../../../projects/openProject', () => ({ openProject: mocks.openProject }));
vi.mock('../../../services/activeSession', () => ({ getActiveSessionId: () => 'S-chat' }));

import ProjectBrowser from './ProjectBrowser';
import { UPLOAD_BUTTON_NAME } from '../../projects/ProjectAssetsSection';
import { __resetProjectTabsForTests, registerProjectTab } from '../../projects/tabSlots';
import type { AssetView } from '../../../projects/api';
import { theme } from '../../../styles/tokens';

const PID = 'p' + 'a'.repeat(32);

function asset(id: string, over: Partial<AssetView> = {}): AssetView {
  return { assetId: id, displayName: `${id}.mp4`, kind: 'video', status: 'ready', sourceSize: 1024, ...over };
}

async function flush() {
  await act(async () => {
    for (let i = 0; i < 20; i++) await Promise.resolve();
  });
}

const show = (data: Record<string, unknown>) =>
  render(<ProjectBrowser title="Browse" theme={theme} data={{ project_id: PID, project_name: 'Storyboards', ...data } as never} />);

beforeEach(() => {
  vi.clearAllMocks();
  __resetProjectTabsForTests();
  mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [] } });
  mocks.searchAssets.mockResolvedValue({ ok: true, value: { assets: [], truncated: false } });
  mocks.getProject.mockResolvedValue({ ok: true, value: { project: { projectId: PID, name: 'Storyboards', personal: false }, role: 'member' } });
  mocks.requestUrls.mockResolvedValue(new Map());
  mocks.openAssetAt.mockResolvedValue({ ok: true });
});
afterEach(() => cleanup());

describe('project-browser registration', () => {
  const read = (rel: string) => readFileSync(join(__dirname, rel), 'utf8');
  it('is in the artifact registry, the manifest router, and both non-timeline lists', () => {
    expect(read('../RendererRegistry.ts')).toMatch(/registry\.set\('project-browser', ProjectBrowser\)/);
    expect(read('ManifestRouter.tsx')).toMatch(/'project-browser': ProjectBrowser/);
    const parser = read('../../nle/manifestParser.ts');
    const parserSet = /const NON_TIMELINE_TEMPLATES = new Set\(\[([\s\S]*?)\]\);/.exec(parser)?.[1] ?? '';
    expect(parserSet).toContain("'project-browser'");
    const server = read('../../../../server/renderSession.ts');
    const serverSet = /const NON_TIMELINE_TEMPLATES = new Set\(\[([\s\S]*?)\]\);/.exec(server)?.[1] ?? '';
    expect(serverSet).toContain('"project-browser"');
  });
});

describe('ProjectBrowser', () => {
  it('shows the project grid with search, filters and the view toggle, and nothing that changes the Project', async () => {
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [asset('video:1', { status: 'failed' })] } });
    show({});
    await flush();
    expect(screen.getByTestId('project-browser')).toHaveTextContent('Storyboards');
    expect(screen.getByTestId('assets-search')).toBeInTheDocument();
    expect(screen.getByTestId('assets-kind')).toBeInTheDocument();
    expect(screen.getByTestId('assets-view-toggle')).toBeInTheDocument();
    expect(screen.getAllByTestId('asset-card')).toHaveLength(1);
    // No upload, no per-card actions (Delete / Retry), no selection checkbox.
    expect(screen.queryByRole('button', { name: UPLOAD_BUTTON_NAME })).toBeNull();
    expect(screen.queryByTestId('assets-upload')).toBeNull();
    expect(screen.queryByRole('button', { name: /^Actions for/ })).toBeNull();
    expect(screen.queryByRole('checkbox')).toBeNull();
    expect(screen.getByTestId('project-assets')).toHaveAttribute('data-browse-only', 'true');
  });

  it('a deleted project says so once and stops reading (no polling a 404 forever)', async () => {
    mocks.listAssets.mockResolvedValue({ ok: false, kind: 'not_found' });
    show({});
    await flush();
    expect(screen.getByTestId('project-browser-gone')).toHaveTextContent('"Storyboards" no longer exists');
    expect(screen.queryByTestId('project-assets')).toBeNull();
    const calls = mocks.listAssets.mock.calls.length;
    await act(async () => { await new Promise((r) => setTimeout(r, 50)); });
    await flush();
    expect(mocks.listAssets.mock.calls.length).toBe(calls);
  });

  it('a registered assets slot does not replace the browse grid', async () => {
    registerProjectTab('assets', () => <div data-testid="slot-grid" />);
    show({});
    await flush();
    expect(screen.queryByTestId('slot-grid')).toBeNull();
  });

  it("seeds the grid from the agent's filter", async () => {
    show({ filter: { q: 'meridian', folder: '', kind: 'video' } });
    await flush();
    expect(screen.getByTestId('assets-search')).toHaveValue('meridian');
    expect(screen.getByTestId('assets-kind')).toHaveValue('video');
  });

  it('ignores a kind the grid does not offer', async () => {
    show({ filter: { kind: 'spreadsheet' } });
    await flush();
    expect(screen.getByTestId('assets-kind')).toHaveValue('');
  });

  it("Open in side panel opens a media file as an asset-player tab in the chat's session", async () => {
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [asset('video:1', { displayName: 'cut.mp4' })] } });
    show({});
    await flush();
    fireEvent.mouseEnter(screen.getAllByTestId('asset-card')[0]);
    fireEvent.click(screen.getByRole('button', { name: 'Open cut.mp4 in the side panel' }));
    await flush();
    expect(mocks.openAssetAt).toHaveBeenCalledWith(
      { ref: { project_id: PID, asset_id: 'video:1', frame_s3_key: null }, kind: 'video', displayName: 'cut.mp4', startMs: 0 },
      'S-chat',
    );
  });

  it('"Open in Projects" opens that project on the Projects surface', async () => {
    show({});
    await flush();
    fireEvent.click(screen.getByTestId('project-browser-open'));
    expect(mocks.openProject).toHaveBeenCalledWith(PID);
  });

  it('shows a quiet empty line when the manifest names no project', () => {
    render(<ProjectBrowser title="Browse" theme={theme} data={{} as never} />);
    expect(screen.getByTestId('project-browser-empty')).toBeInTheDocument();
  });
});
