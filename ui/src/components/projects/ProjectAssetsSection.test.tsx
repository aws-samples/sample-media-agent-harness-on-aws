/**
 * @vitest-environment jsdom
 *
 * The Projects_Surface Assets section: the one-row toolbar (identical in both views; Upload ▾ with Files / Folder +
 * drop zone through the upload queue), the Assets grid (thumbnail or kind icon, name, kind, size, a status badge only
 * while an asset needs attention, with a failure reason, Load more), hover/selection-mode checkboxes and the
 * selection bar, opening an asset (player for media, source URL for a document), Delete with its confirmation and an
 * optimistic hide, the 10 s poll while anything is non-terminal, and the `No assets yet` empty state.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import '@testing-library/jest-dom';
import React from 'react';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';

const mocks = vi.hoisted(() => ({
  listAssets: vi.fn(),
  removeAsset: vi.fn(),
  retryAsset: vi.fn(),
  retryRebuild: vi.fn(),
  registerS3Location: vi.fn(),
  getIngestionCapabilities: vi.fn(),
  createUploads: vi.fn(),
  completeUpload: vi.fn(),
  abortUpload: vi.fn(),
  registerAsset: vi.fn(),
  requestUrls: vi.fn(),
  openAssetAt: vi.fn(),
  searchAssets: vi.fn(),
  findDuplicatesBatch: vi.fn(),
  getProject: vi.fn(),
  listFolders: vi.fn(),
  moveAssets: vi.fn(),
  renameFolder: vi.fn(),
  createFolder: vi.fn(),
  listBookmarks: vi.fn(),
}));
vi.mock('../../projects/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../projects/api')>()),
  listAssets: mocks.listAssets,
  removeAsset: mocks.removeAsset,
  retryAsset: mocks.retryAsset,
  retryRebuild: mocks.retryRebuild,
  registerS3Location: mocks.registerS3Location,
  getIngestionCapabilities: mocks.getIngestionCapabilities,
  createUploads: mocks.createUploads,
  completeUpload: mocks.completeUpload,
  abortUpload: mocks.abortUpload,
  registerAsset: mocks.registerAsset,
  searchAssets: mocks.searchAssets,
  findDuplicatesBatch: mocks.findDuplicatesBatch,
  getProject: mocks.getProject,
  listFolders: mocks.listFolders,
  moveAssets: mocks.moveAssets,
  renameFolder: mocks.renameFolder,
  createFolder: mocks.createFolder,
  listBookmarks: mocks.listBookmarks,
}));
vi.mock('../../projects/assetUrlBatcher', () => ({ request: mocks.requestUrls }));
vi.mock('../../projects/openCitation', () => ({ openAssetAt: mocks.openAssetAt }));

import ProjectAssetsSection, {
  deleteFolderMessage,
  ANALYSIS_TYPE,
  DATA_TYPE,
  ASSETS_EMPTY_TITLE,
  ASSETS_VIEW_KEY,
  UPLOAD_BUTTON_NAME,
  deleteAssetMessage,
  resetFolderListCacheForTests,
} from './ProjectAssetsSection';
import { __resetProjectTabsForTests } from './tabSlots';
import { ASSET_POLL_MS } from './useProjectAssets';
import { __resetUploadQueueForTests, __setTransferDepsForTests } from '../../projects/upload/uploadQueue';
import { DEFAULT_CAPABILITIES } from '../../projects/upload/kinds';
import { EMOJI_OR_GLYPH } from '../../test-utils/glyphs';
import type { AssetView } from '../../projects/api';
import type { PutFn } from '../../projects/upload/transfer';

const PROJECT = { id: 'p1', name: 'Launch Q3', personal: false };

function asset(id: string, over: Partial<AssetView> = {}): AssetView {
  return { assetId: id, displayName: `${id}.mp4`, kind: 'video', status: 'ready', sourceSize: 2 * 1024 * 1024, ...over };
}

async function flush() {
  await act(async () => {
    for (let i = 0; i < 20; i++) await Promise.resolve();
  });
}

/**
 * Answer like the server for `assets`: the list, the complete folder list (direct counts), and a folder's own
 * files for `searchAssets({folder})` (a folder pick is fetched, not filtered from the loaded page).
 */
function serveAssets(assets: AssetView[]) {
  mocks.listAssets.mockResolvedValue({ ok: true, value: { assets } });
  const counts = new Map<string, number>();
  for (const a of assets) if (a.folder) counts.set(a.folder, (counts.get(a.folder) ?? 0) + 1);
  mocks.listFolders.mockResolvedValue({ ok: true, value: { truncated: false,
    folders: [...counts].map(([folder, count]) => ({ folder, count })), rootCount: assets.filter((a) => !a.folder).length } });
  mocks.searchAssets.mockImplementation(async (_pid: string, q: { q?: string; kind?: string; folder?: string }) => ({
    ok: true, value: { truncated: false, assets: assets.filter((a) => q.folder === undefined || (a.folder ?? '') === q.folder) },
  }));
}

/** Pick a folder in the tree by its path, then let the (debounced) folder fetch land. */
async function pickFolder(path: string) {
  const node = screen.getAllByTestId('folder-tree-node').find((n) => n.getAttribute('data-folder') === path);
  if (!node) throw new Error(`no tree node for ${path}`);
  fireEvent.click(within(node).getAllByRole('button').find((b) => b.getAttribute('aria-hidden') !== 'true')!);
  await act(async () => { vi.advanceTimersByTime(300); });
  await flush();
}

function renderSection(onNotFound = vi.fn()) {
  const utils = render(<ProjectAssetsSection project={PROJECT} role="owner" onNotFound={onNotFound} sessionId="S1" />);
  return { ...utils, onNotFound };
}

const cards = () => screen.queryAllByTestId('asset-card');
const openUploadMenu = () => fireEvent.click(screen.getByRole('button', { name: UPLOAD_BUTTON_NAME }));

beforeEach(() => {
  vi.clearAllMocks();
  resetFolderListCacheForTests();
  __resetProjectTabsForTests();
  __resetUploadQueueForTests();
  mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [] } });
  mocks.removeAsset.mockResolvedValue({ ok: true, value: undefined });
  mocks.retryAsset.mockResolvedValue({ ok: true, value: undefined });
  mocks.retryRebuild.mockResolvedValue({ ok: true, value: undefined });
  mocks.requestUrls.mockResolvedValue(new Map());
  mocks.openAssetAt.mockResolvedValue({ ok: true });
  mocks.getIngestionCapabilities.mockResolvedValue({ ok: true, value: DEFAULT_CAPABILITIES });
  mocks.searchAssets.mockResolvedValue({ ok: true, value: { assets: [], truncated: false } });
  mocks.findDuplicatesBatch.mockResolvedValue({ ok: true, value: { duplicates: [] } });
  mocks.listFolders.mockResolvedValue({ ok: true, value: { folders: [], truncated: false } });
  mocks.listBookmarks.mockResolvedValue({ ok: true, value: [] });
  mocks.createFolder.mockImplementation(async (_pid: string, folder: string) => ({ ok: true, value: { folder } }));
  mocks.moveAssets.mockImplementation(async (_pid: string, assetIds: string[], folder: string) =>
    ({ ok: true, value: { folder, moved: assetIds, failed: [] } }));
  mocks.renameFolder.mockImplementation(async (_pid: string, from: string, to: string) =>
    ({ ok: true, value: { from, to, moved: [], failed: [], remaining: 0 } }));
  mocks.getProject.mockResolvedValue({ ok: true, value: { project: { projectId: 'p1', name: 'Launch Q3', personal: false }, role: 'owner' } });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  __resetUploadQueueForTests();
});

describe('Assets grid', () => {
  it('shows the empty state when the Project has no assets', async () => {
    renderSection();
    await flush();
    expect(screen.getByTestId('project-assets-empty')).toHaveTextContent(ASSETS_EMPTY_TITLE);
    expect(screen.queryByTestId('project-assets-grid')).toBeNull();
  });

  it('renders a card per asset: image thumbnail, kind icons, name, kind, size, and a badge only when it needs attention', async () => {
    mocks.listAssets.mockResolvedValue({
      ok: true,
      value: {
        assets: [
          asset('image:1', { kind: 'image', displayName: 'poster.png', status: 'queued', sourceSize: 512 * 1024 }),
          asset('video:1', { status: 'indexing' }),
          asset('audio:1', { kind: 'audio', displayName: 'vo.wav', status: 'ready' }),
          asset('document:1', { kind: 'document', displayName: 'script.pdf', status: 'queued' }),
          asset('video:2', { status: 'removed' }),
        ],
      },
    });
    mocks.requestUrls.mockResolvedValue(
      new Map([['image:1', { ok: true, entry: { source: { url: 'https://s/src', expiresAt: 1 }, rendition: { url: 'https://s/thumb', expiresAt: 1 } } }]]),
    );
    renderSection();
    await flush();
    // A removed row never shows.
    expect(cards().map((c) => c.getAttribute('data-asset-id'))).toEqual(['image:1', 'video:1', 'audio:1', 'document:1']);
    const [img, vid, aud, doc] = cards();
    expect(img.querySelector('img')).toHaveAttribute('src', 'https://s/thumb');
    expect(mocks.requestUrls).toHaveBeenCalledWith('p1', ['image:1']);
    expect(img).toHaveTextContent('poster.png');
    expect(img).toHaveTextContent('Image · 512 KB');
    expect(img).toHaveTextContent('Queued');
    expect(within(vid).getByTestId('card-kind-icon').querySelector('svg')).not.toBeNull();
    expect(vid).toHaveTextContent('Video · 2 MB');
    expect(vid).toHaveTextContent('Indexing');
    expect(aud).not.toHaveTextContent('Ready'); // a resting state: no badge
    expect(doc).toHaveTextContent('Document');
    expect(screen.getByTestId('project-assets').textContent ?? '').not.toMatch(EMOJI_OR_GLYPH);
  });

  it('shows a video’s hero image, and the kind icon (never the source file) when it has none', async () => {
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [
      asset('video:1', { displayName: 'a.mp4', status: 'indexed' }),
      asset('video:2', { displayName: 'b.mp4', status: 'queued' }),
    ] } });
    mocks.requestUrls.mockImplementation(async (_p: string, ids: string[]) => new Map(ids.map((id) => [id, {
      ok: true,
      entry: id === 'video:1'
        ? { source: { url: 'https://s/a.mp4', expiresAt: 1 }, rendition: { url: 'https://s/hero.jpg', expiresAt: 1 } }
        : { source: { url: 'https://s/b.mp4', expiresAt: 1 } },
    }])));
    renderSection();
    await flush();
    const [a, b] = cards();
    expect(a.querySelector('img')).toHaveAttribute('src', 'https://s/hero.jpg');
    expect(b.querySelector('img')).toBeNull();
    expect(within(b).getByTestId('card-kind-icon')).toBeInTheDocument();
  });

  it('shows a failed asset’s reason on hover and behind Details', async () => {
    mocks.listAssets.mockResolvedValue({
      ok: true,
      value: { assets: [asset('video:1', { status: 'failed', failure: { code: 'decode', message: 'The video could not be decoded.' } })] },
    });
    renderSection();
    await flush();
    const card = cards()[0];
    expect(within(card).getByText('Failed')).toHaveAttribute('title', 'The video could not be decoded.');
    const details = within(card).getByRole('button', { name: 'Details' });
    expect(details).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(details);
    expect(card).toHaveTextContent('The video could not be decoded.');
  });

  it('loads more with the cursor and appends', async () => {
    mocks.listAssets
      .mockResolvedValueOnce({ ok: true, value: { assets: [asset('video:1')], cursor: 'c1' } })
      .mockResolvedValueOnce({ ok: true, value: { assets: [asset('video:2')] } });
    renderSection();
    await flush();
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
    await flush();
    expect(mocks.listAssets).toHaveBeenLastCalledWith('p1', 'c1');
    expect(cards()).toHaveLength(2);
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull();
  });

  it('calls onNotFound when the list answers the uniform not-found', async () => {
    mocks.listAssets.mockResolvedValue({ ok: false, kind: 'not_found' });
    const { onNotFound } = renderSection();
    await flush();
    expect(onNotFound).toHaveBeenCalled();
  });
});

describe('opening an asset', () => {
  it('a click opens the preview (the chat standard): the file large, with Download and Open in side panel', async () => {
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [asset('video:1', { displayName: 'cut.mp4' })] } });
    mocks.requestUrls.mockResolvedValue(new Map([['video:1', { ok: true, entry: { source: { url: 'https://s/cut.mp4', expiresAt: 1 } } }]]));
    renderSection();
    await flush();
    const card = cards()[0];
    fireEvent.mouseEnter(card);                                         // a tile shows its actions on hover
    expect([...card.querySelectorAll('button')].map((b) => b.getAttribute('aria-label')).filter((l) => /^(Preview|Download|Open)/.test(l ?? '')))
      .toEqual(['Preview cut.mp4', 'Download cut.mp4', 'Open cut.mp4 in the side panel']);
    fireEvent.click(within(card).getAllByRole('button', { name: 'Preview cut.mp4' })[0]);
    await flush();
    expect(screen.getByRole('dialog', { name: 'Preview of cut.mp4' })).toBeInTheDocument();
    expect(screen.getByTestId('asset-preview-video')).toHaveAttribute('src', 'https://s/cut.mp4');
    expect(mocks.openAssetAt).not.toHaveBeenCalled();                    // viewing is not opening a tab
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: /Open in side panel/ }));
    await flush();
    expect(mocks.openAssetAt).toHaveBeenCalledTimes(1);
  });

  it('Open in side panel opens media in the asset player at 0 for the chat on screen', async () => {
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [asset('video:1', { displayName: 'cut.mp4' })] } });
    renderSection();
    await flush();
    fireEvent.mouseEnter(cards()[0]);
    fireEvent.click(screen.getByRole('button', { name: 'Open cut.mp4 in the side panel' }));
    await flush();
    expect(mocks.openAssetAt).toHaveBeenCalledWith(
      { ref: { project_id: 'p1', asset_id: 'video:1', frame_s3_key: null }, kind: 'video', displayName: 'cut.mp4', startMs: 0 },
      'S1',
    );
  });

  it('shows the player’s refusal message', async () => {
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [asset('video:1', { displayName: 'cut.mp4' })] } });
    mocks.openAssetAt.mockResolvedValue({ ok: false, message: "Can't open cut.mp4: it was removed from the project." });
    renderSection();
    await flush();
    fireEvent.mouseEnter(cards()[0]);
    fireEvent.click(screen.getByRole('button', { name: 'Open cut.mp4 in the side panel' }));
    await flush();
    expect(screen.getByRole('alert')).toHaveTextContent("Can't open cut.mp4: it was removed from the project.");
  });

  it('a document previews in place and opens in the side panel like media', async () => {
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [asset('document:1', { kind: 'document', displayName: 'notes.pdf' })] } });
    mocks.requestUrls.mockResolvedValue(new Map([['document:1', { ok: true, entry: { source: { url: 'https://s/notes', expiresAt: 1 } } }]]));
    renderSection();
    await flush();
    fireEvent.click(within(cards()[0]).getAllByRole('button', { name: 'Preview notes.pdf' })[0]);
    await flush();
    expect(screen.getByTestId('asset-preview-document')).toHaveAttribute('src', 'https://s/notes');
    fireEvent.click(screen.getByRole('button', { name: 'Close preview' }));
    fireEvent.mouseEnter(cards()[0]);
    fireEvent.click(screen.getByRole('button', { name: 'Open notes.pdf in the side panel' }));
    await flush();
    expect(mocks.openAssetAt).toHaveBeenCalledWith(expect.objectContaining({ kind: 'document', displayName: 'notes.pdf' }), 'S1');
  });
});

describe('Collection rebuild state', () => {
  const state = (collectionState: string) =>
    ({ ok: true, value: { assets: [asset('video:1', { status: 'indexed' })], collectionState } });

  it('says search can be partial while the collection rebuilds, and nothing when ready', async () => {
    mocks.listAssets.mockResolvedValue(state('rebuilding'));
    renderSection();
    await flush();
    expect(screen.getByTestId('collection-rebuilding')).toHaveTextContent('Results can be partial');
    cleanup();
    mocks.listAssets.mockResolvedValue(state('ready'));
    renderSection();
    await flush();
    expect(screen.queryByTestId('collection-rebuilding')).toBeNull();
    expect(screen.queryByTestId('collection-rebuild-failed')).toBeNull();
  });

  it('offers a rebuild retry after a failed rebuild', async () => {
    mocks.listAssets.mockResolvedValue(state('rebuild_failed'));
    renderSection();
    await flush();
    mocks.listAssets.mockResolvedValue(state('rebuilding'));
    fireEvent.click(within(screen.getByTestId('collection-rebuild-failed')).getByRole('button', { name: 'Retry rebuild' }));
    await flush();
    expect(mocks.retryRebuild).toHaveBeenCalledWith('p1');
    expect(screen.getByTestId('collection-rebuilding')).toBeInTheDocument();
  });
});

describe('Retry', () => {
  it('offers Retry only on a failed asset, retries it, and refetches', async () => {
    mocks.listAssets
      .mockResolvedValueOnce({ ok: true, value: { assets: [
        asset('video:1', { displayName: 'bad.mp4', status: 'failed', failure: 'shot-segmentation: boom' }),
        asset('video:2', { displayName: 'good.mp4', status: 'indexed' }),
      ] } })
      .mockResolvedValue({ ok: true, value: { assets: [asset('video:1', { displayName: 'bad.mp4', status: 'queued' })] } });
    renderSection();
    await flush();
    fireEvent.click(screen.getByRole('button', { name: 'More for good.mp4' }));
    expect(screen.queryByRole('menuitem', { name: 'Retry' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'More for good.mp4' }));
    fireEvent.click(screen.getByRole('button', { name: 'More for bad.mp4' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Retry' }));
    await flush();
    expect(mocks.retryAsset).toHaveBeenCalledWith('p1', 'video:1');
    expect(mocks.listAssets).toHaveBeenCalledTimes(2);
  });

  it('shows why a retry was refused', async () => {
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [
      asset('video:1', { displayName: 'bad.mp4', status: 'failed' })] } });
    mocks.retryAsset.mockResolvedValue({ ok: false, kind: 'error', status: 503, code: 'catalog_missing',
      message: 'the M2C catalog is missing shot-segmentation___segment_shots' });
    renderSection();
    await flush();
    fireEvent.click(screen.getByRole('button', { name: 'More for bad.mp4' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Retry' }));
    await flush();
    expect(screen.getByRole('alert')).toHaveTextContent("Couldn't retry bad.mp4: the M2C catalog is missing");
  });
});

describe('Delete', () => {
  it('confirms with the exact message, hides the card at once, removes, and refetches', async () => {
    mocks.listAssets
      .mockResolvedValueOnce({ ok: true, value: { assets: [asset('video:1', { displayName: 'cut.mp4' }), asset('video:2')] } })
      .mockResolvedValue({ ok: true, value: { assets: [asset('video:2')] } });
    let finish: (v: unknown) => void = () => {};
    mocks.removeAsset.mockReturnValue(new Promise((r) => { finish = r; }));
    renderSection();
    await flush();
    fireEvent.click(screen.getByRole('button', { name: 'More for cut.mp4' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Delete' }));
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveTextContent(deleteAssetMessage('cut.mp4'));
    expect(deleteAssetMessage('cut.mp4')).toBe('Delete cut.mp4? This removes it from the project and from chats it was attached to.');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
    await flush();
    // Optimistic: gone before the server answers.
    expect(cards().map((c) => c.getAttribute('data-asset-id'))).toEqual(['video:2']);
    expect(mocks.removeAsset).toHaveBeenCalledWith('p1', 'video:1');
    await act(async () => finish({ ok: true, value: undefined }));
    await flush();
    expect(mocks.listAssets).toHaveBeenCalledTimes(2);
    expect(cards()).toHaveLength(1);
  });

  it('brings the card back with an error when the delete fails', async () => {
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [asset('video:1', { displayName: 'cut.mp4' })] } });
    mocks.removeAsset.mockResolvedValue({ ok: false, kind: 'error', status: 500, code: 'boom', message: 'Storage unavailable' });
    renderSection();
    await flush();
    fireEvent.click(screen.getByRole('button', { name: 'More for cut.mp4' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Delete' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete' }));
    await flush();
    expect(screen.getByRole('alert')).toHaveTextContent("Couldn't delete cut.mp4: Storage unavailable");
    expect(cards()).toHaveLength(1);
  });
});

describe('polling', () => {
  it('refreshes every 10 s while an asset is non-terminal, stops when all are final and on unmount', async () => {
    vi.useFakeTimers();
    mocks.listAssets
      .mockResolvedValueOnce({ ok: true, value: { assets: [asset('video:1', { status: 'queued' })] } })
      .mockResolvedValueOnce({ ok: true, value: { assets: [asset('video:1', { status: 'indexing' })] } })
      .mockResolvedValue({ ok: true, value: { assets: [asset('video:1', { status: 'ready' })] } });
    renderSection();
    await flush();
    expect(cards()[0]).toHaveTextContent('Queued');

    await act(async () => { vi.advanceTimersByTime(ASSET_POLL_MS); });
    await flush();
    expect(mocks.listAssets).toHaveBeenCalledTimes(2);
    expect(cards()[0]).toHaveTextContent('Indexing');

    await act(async () => { vi.advanceTimersByTime(ASSET_POLL_MS); });
    await flush();
    expect(mocks.listAssets).toHaveBeenCalledTimes(3);
    expect(cards()[0]).toHaveAttribute('data-status', 'ready');
    expect(cards()[0]).not.toHaveTextContent('Ready'); // at rest: the badge goes away

    await act(async () => { vi.advanceTimersByTime(ASSET_POLL_MS * 3); });
    await flush();
    expect(mocks.listAssets).toHaveBeenCalledTimes(3);
  });

  it('stops polling when the section unmounts', async () => {
    vi.useFakeTimers();
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [asset('video:1', { status: 'queued' })] } });
    const { unmount } = renderSection();
    await flush();
    unmount();
    await act(async () => { vi.advanceTimersByTime(ASSET_POLL_MS * 3); });
    expect(mocks.listAssets).toHaveBeenCalledTimes(1);
  });
});

describe('Add S3 location (removed from the UI for now)', () => {
  it('offers no S3 location button or form', async () => {
    renderSection();
    await flush();
    expect(screen.queryByRole('button', { name: /S3 location/i })).toBeNull();
    expect(screen.queryByLabelText('S3 URI')).toBeNull();
  });
});

describe('Upload files', () => {
  it('uploads picked files through the queue, lists them with progress, and refreshes the grid when one is ready', async () => {
    let release: () => void = () => {};
    const put = vi.fn<PutFn>().mockImplementation(
      (_url, body, opts) =>
        new Promise((resolve) => {
          opts.onProgress?.(Math.floor(body.size / 4));
          release = () => resolve({ etag: '"e"' });
        }),
    );
    __setTransferDepsForTests({ put, sleep: () => Promise.resolve() });
    mocks.createUploads.mockResolvedValue({
      ok: true,
      value: { uploads: [{ index: 0, assetId: 'video:N', key: 'projects/p1/video:N/new.mp4', mode: 'single', url: 'https://s3/n', headers: { 'Content-Type': 'video/mp4' } }], errors: {} },
    });
    mocks.registerAsset.mockResolvedValue({ ok: true, value: asset('video:N', { displayName: 'new.mp4', status: 'queued' }) });
    renderSection();
    await flush();
    const input = screen.getByTestId('project-upload-input') as HTMLInputElement;
    const click = vi.spyOn(input, 'click');
    openUploadMenu();
    fireEvent.click(screen.getByRole('menuitem', { name: 'Files' }));
    expect(click).toHaveBeenCalled();
    fireEvent.change(input, { target: { files: [new File([new Uint8Array(100)], 'new.mp4', { type: 'video/mp4' })] } });
    await flush();
    const list = screen.getByTestId('project-uploads');
    expect(within(list).getByRole('progressbar', { name: 'Uploading new.mp4' })).toHaveAttribute('aria-valuenow', '25');
    expect(within(list).getByRole('button', { name: 'Cancel new.mp4' })).toBeInTheDocument();

    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [asset('video:N', { displayName: 'new.mp4', status: 'queued' })] } });
    await act(async () => release());
    await flush();
    expect(screen.getByTestId('upload-summary')).toHaveTextContent('Upload finished: 1 uploaded, 0 failed, 0 rejected, 0 canceled.');
    expect(cards().map((c) => c.getAttribute('data-asset-id'))).toEqual(['video:N']);
    // Once the file is IN the grid its "Uploaded" row is redundant (it read as a second list view of the
    // same files), so it leaves on its own; the announcement survives it.
    expect(screen.queryByTestId('project-uploads')).toBeNull();
    expect(screen.getByTestId('upload-summary')).toHaveTextContent('1 uploaded');
  });

  it('a failed upload stays listed until cleared (it shows nowhere else)', async () => {
    __setTransferDepsForTests({ put: vi.fn<PutFn>().mockRejectedValue(new Error('The upload failed: network error.')) });
    mocks.createUploads.mockResolvedValue({
      ok: true,
      value: { uploads: [{ index: 0, assetId: 'video:F', key: 'k', mode: 'single', url: 'https://s3/f', headers: {} }], errors: {} },
    });
    renderSection();
    await flush();
    fireEvent.change(screen.getByTestId('project-upload-input'), {
      target: { files: [new File([new Uint8Array(5)], 'net.mp4', { type: 'video/mp4' })] },
    });
    await flush();
    await flush();
    expect(screen.getByTestId('project-uploads')).toHaveTextContent('Failed');
    fireEvent.click(screen.getByRole('button', { name: 'Clear finished' }));
    expect(screen.queryByTestId('project-uploads')).toBeNull();
  });

  it('a drop on the section uploads any number of files, 20 per grant request', async () => {
    mocks.createUploads.mockResolvedValue({ ok: true, value: { uploads: [], errors: {} } });
    renderSection();
    await flush();
    const files = Array.from({ length: 21 }, (_, i) => new File([new Uint8Array(1)], `f${i}.png`, { type: 'image/png' }));
    fireEvent.drop(screen.getByTestId('project-assets'), { dataTransfer: { files, types: ['Files'] } });
    await flush();
    await flush();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(mocks.createUploads.mock.calls.map((c) => (c[1] as unknown[]).length)).toEqual([20, 1]);
  });

  it('a failed file offers Retry; a rejected one does not', async () => {
    __setTransferDepsForTests({ put: vi.fn<PutFn>().mockRejectedValue(new Error('The upload failed: network error.')) });
    mocks.createUploads.mockResolvedValue({
      ok: true,
      value: { uploads: [{ index: 0, assetId: 'video:F', key: 'k', mode: 'single', url: 'https://s3/f', headers: {} }], errors: {} },
    });
    renderSection();
    await flush();
    fireEvent.change(screen.getByTestId('project-upload-input'), {
      target: { files: [new File([new Uint8Array(5)], 'net.mp4', { type: 'video/mp4' }), new File([], 'empty.mp4', { type: 'video/mp4' })] },
    });
    await flush();
    const list = screen.getByTestId('project-uploads');
    expect(list).toHaveTextContent('Failed: The upload failed: network error.');
    expect(within(list).getByRole('button', { name: 'Retry net.mp4' })).toBeInTheDocument();
    expect(list).toHaveTextContent('Not uploaded: The file is empty.');
    expect(within(list).queryByRole('button', { name: 'Retry empty.mp4' })).toBeNull();
  });
});

describe('View toggle (Icons | List)', () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => localStorage.clear());

  it('defaults to Icons, switches to List, and remembers the choice', async () => {
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [asset('video:A'), asset('video:B')] } });
    const first = renderSection();
    await flush();
    const icons = screen.getByRole('radio', { name: 'Icons view' });
    const list = screen.getByRole('radio', { name: 'List view' });
    expect(icons).toHaveAttribute('aria-checked', 'true');
    expect(list).toHaveAttribute('aria-checked', 'false');
    expect(screen.getByTestId('project-assets-grid')).toHaveAttribute('data-view', 'grid');
    expect(cards().every((c) => c.getAttribute('data-layout') === 'grid')).toBe(true);

    fireEvent.click(list);
    expect(list).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByTestId('project-assets-grid')).toHaveAttribute('data-view', 'list');
    expect(cards().map((c) => c.getAttribute('data-layout'))).toEqual(['list', 'list']);
    // A row still opens and still offers Delete.
    expect(screen.getAllByRole('button', { name: 'Preview video:A.mp4' }).length).toBeGreaterThan(0);
    expect(screen.getByRole('button', { name: 'Open video:A.mp4 in the side panel' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Actions for video:A.mp4' })).toBeInTheDocument();
    expect(localStorage.getItem(ASSETS_VIEW_KEY)).toBe('list');

    first.unmount();
    renderSection();
    await flush();
    expect(screen.getByRole('radio', { name: 'List view' })).toHaveAttribute('aria-checked', 'true');
    expect(cards().map((c) => c.getAttribute('data-layout'))).toEqual(['list', 'list']);
  });
});

describe('Assets search and duplicate uploads', () => {
  it('searches by name and type through the server, and goes back to the full list when cleared', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [asset('video:1'), asset('video:2'), asset('image:3', { kind: 'image' })] } });
    mocks.searchAssets.mockResolvedValue({ ok: true, value: { assets: [asset('video:2', { displayName: 'meridian.mp4' })], truncated: false } });
    renderSection();
    await flush();
    expect(cards()).toHaveLength(3);
    fireEvent.change(screen.getByLabelText('Search files by name'), { target: { value: 'meridian' } });
    fireEvent.change(screen.getByLabelText('File type'), { target: { value: 'video' } });
    await act(async () => { vi.advanceTimersByTime(300); });
    await flush();
    expect(mocks.searchAssets).toHaveBeenLastCalledWith('p1', { q: 'meridian', kind: 'video' });
    expect(cards().map((c) => c.getAttribute('data-asset-id'))).toEqual(['video:2']);
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull();

    mocks.searchAssets.mockResolvedValue({ ok: true, value: { assets: [], truncated: false } });
    fireEvent.change(screen.getByLabelText('Search files by name'), { target: { value: 'nothing' } });
    await act(async () => { vi.advanceTimersByTime(300); });
    await flush();
    expect(screen.getByTestId('assets-search-empty')).toHaveTextContent('No files match.');
    expect(screen.queryByTestId('project-assets-empty')).toBeNull(); // not the "no assets yet" state

    fireEvent.change(screen.getByLabelText('Search files by name'), { target: { value: '' } });
    fireEvent.change(screen.getByLabelText('File type'), { target: { value: '' } });
    await flush();
    expect(cards()).toHaveLength(3);
  });

  function pick(files: File[]) {
    fireEvent.change(screen.getByTestId('project-upload-input'), { target: { files } });
  }

  it('a file already in the project asks once; Use existing uploads only the new files', async () => {
    mocks.findDuplicatesBatch.mockImplementation(async (_pid: string, files: { name: string }[]) => ({
      ok: true, value: { duplicates: files.flatMap((f, index) => (f.name === 'trailer.mp4'
        ? [{ index, asset: asset('video:T', { displayName: 'trailer.mp4', sourceSize: 100 }) }] : [])) },
    }));
    mocks.createUploads.mockResolvedValue({ ok: true, value: { uploads: [], errors: {} } });
    renderSection();
    await flush();
    pick([new File([new Uint8Array(100)], 'trailer.mp4', { type: 'video/mp4' }),
      new File([new Uint8Array(10)], 'fresh.mp4', { type: 'video/mp4' })]);
    await flush();
    expect(mocks.findDuplicatesBatch).toHaveBeenCalledTimes(1);
    expect(mocks.findDuplicatesBatch).toHaveBeenCalledWith('p1', [{ name: 'trailer.mp4', size: 100 }, { name: 'fresh.mp4', size: 10 }]);
    const dialog = screen.getByTestId('duplicates-dialog');
    expect(dialog).toHaveTextContent('This file is already in the project');
    expect(dialog).toHaveTextContent('The other 1 file will upload either way.');
    expect(mocks.createUploads).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Use existing' }));
    await flush();
    expect(mocks.createUploads).toHaveBeenCalledTimes(1);
    const sent = mocks.createUploads.mock.calls[0][1] as Array<{ name: string }>;
    expect(sent.map((f) => f.name)).toEqual(['fresh.mp4']);
  });

  it('Keep both uploads every file; Cancel uploads none', async () => {
    mocks.findDuplicatesBatch.mockResolvedValue({ ok: true, value: { duplicates: [
      { index: 0, asset: asset('video:T', { displayName: 'trailer.mp4', sourceSize: 100 }) }] } });
    mocks.createUploads.mockResolvedValue({ ok: true, value: { uploads: [], errors: {} } });
    renderSection();
    await flush();
    pick([new File([new Uint8Array(100)], 'trailer.mp4', { type: 'video/mp4' })]);
    await flush();
    fireEvent.click(within(screen.getByTestId('duplicates-dialog')).getByRole('button', { name: 'Cancel' }));
    await flush();
    expect(mocks.createUploads).not.toHaveBeenCalled();
    pick([new File([new Uint8Array(100)], 'trailer.mp4', { type: 'video/mp4' })]);
    await flush();
    fireEvent.click(within(screen.getByTestId('duplicates-dialog')).getByRole('button', { name: 'Keep both' }));
    await flush();
    expect((mocks.createUploads.mock.calls[0][1] as Array<{ name: string }>).map((f) => f.name)).toEqual(['trailer.mp4']);
  });
});

describe('folders', () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => localStorage.clear());

  const FOLDERED = [
    asset('video:r1', { displayName: 'root-new.mp4' }),
    asset('video:d2', { displayName: 'day2.mp4', folder: 'Footage/Day 2' }),
    asset('video:d1a', { displayName: 'day1-new.mp4', folder: 'Footage/Day 1' }),
    asset('video:g', { displayName: 'gen.mp4', folder: 'Generated/Trailer · 2026-10-07' }),
    asset('video:d1b', { displayName: 'day1-old.mp4', folder: 'Footage/Day 1' }),
    asset('video:r2', { displayName: 'root-old.mp4', folder: '' }),
  ];
  const ids = () => cards().map((c) => c.getAttribute('data-asset-id'));

  it('List view groups by folder: top level first, folders by path, newest first inside, each collapsible', async () => {
    localStorage.setItem(ASSETS_VIEW_KEY, 'list');
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: FOLDERED } });
    renderSection();
    await flush();
    const rows = screen.getAllByTestId('asset-folder-row');
    expect(rows.map((r) => r.getAttribute('data-folder'))).toEqual(['Footage/Day 1', 'Footage/Day 2', 'Generated/Trailer · 2026-10-07']);
    expect(rows[0]).toHaveTextContent('Footage/Day 1');
    expect(rows[0]).toHaveTextContent('2');
    expect(ids()).toEqual(['video:r1', 'video:r2', 'video:d1a', 'video:d1b', 'video:d2', 'video:g']);
    // DOM order: the header precedes its rows.
    const grid = screen.getByTestId('project-assets-grid');
    const order = Array.from(grid.children).map((li) => li.getAttribute('data-folder') ?? li.getAttribute('data-asset-id'));
    expect(order).toEqual(['video:r1', 'video:r2', 'Footage/Day 1', 'video:d1a', 'video:d1b', 'Footage/Day 2', 'video:d2',
      'Generated/Trailer · 2026-10-07', 'video:g']);

    const day1 = within(rows[0]).getByRole('button');
    expect(day1).toHaveAttribute('aria-expanded', 'true');
    fireEvent.click(day1);
    expect(day1).toHaveAttribute('aria-expanded', 'false');
    expect(ids()).toEqual(['video:r1', 'video:r2', 'video:d2', 'video:g']);
    fireEvent.click(day1);
    expect(ids()).toContain('video:d1a');
  });

  it('a picked folder is fetched at once — only typed words wait for a pause', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false });
    serveAssets(FOLDERED);
    renderSection();
    await flush();
    mocks.searchAssets.mockClear();
    const node = screen.getAllByTestId('folder-tree-node').find((n) => n.getAttribute('data-folder') === 'Footage/Day 1')!;
    fireEvent.click(within(node).getAllByRole('button').find((b) => b.getAttribute('aria-hidden') !== 'true')!);
    await act(async () => { vi.advanceTimersByTime(1); });
    await flush();
    expect(mocks.searchAssets).toHaveBeenCalledWith('p1', expect.objectContaining({ folder: 'Footage/Day 1' }));
    expect(ids()).toEqual(['video:d1a', 'video:d1b']);
  });

  it('reopening a project paints its last folder tree at once, then refreshes it', async () => {
    serveAssets(FOLDERED);
    const first = renderSection();
    await flush();
    expect(screen.getAllByTestId('folder-tree-node')).toHaveLength(5);
    first.unmount();
    // The next read is slow: the remembered tree is there before it answers.
    let answer: (v: unknown) => void = () => undefined;
    mocks.listFolders.mockImplementation(() => new Promise((r) => { answer = r; }));
    renderSection();
    expect(screen.getAllByTestId('folder-tree-node').map((n) => n.getAttribute('data-folder'))).toEqual([
      'Footage', 'Footage/Day 1', 'Footage/Day 2', 'Generated', 'Generated/Trailer · 2026-10-07',
    ]);
    await act(async () => { answer({ ok: true, value: { truncated: false, folders: [{ folder: 'Footage', count: 9 }], rootCount: 0 } }); });
    await flush();
    expect(screen.getAllByTestId('folder-tree-node').map((n) => n.getAttribute('data-folder'))).toEqual(['Footage']);
  });

  it('the folder tree lists every folder (Chats last) and a picked folder loads its own files from the server', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    serveAssets(FOLDERED);
    renderSection();
    await flush();
    const tree = screen.getByTestId('folder-tree');
    expect(screen.queryByLabelText('Folder')).toBeNull(); // the tree replaces the Folder select here
    expect(within(tree).getAllByTestId('folder-tree-node').map((n) => n.getAttribute('data-folder'))).toEqual([
      'Footage', 'Footage/Day 1', 'Footage/Day 2', 'Generated', 'Generated/Trailer · 2026-10-07',
    ]);
    const footage = within(tree).getAllByTestId('folder-tree-node')[0];
    expect(footage).toHaveAccessibleName('Footage, 3 files'); // its subtree
    expect(cards()).toHaveLength(6);
    await pickFolder('Footage/Day 1');
    expect(mocks.searchAssets).toHaveBeenLastCalledWith('p1', expect.objectContaining({ folder: 'Footage/Day 1' }));
    expect(ids()).toEqual(['video:d1a', 'video:d1b']);
    // The List view keeps the pick (flat: one folder needs no header) …
    fireEvent.click(screen.getByRole('radio', { name: 'List view' }));
    expect(screen.queryAllByTestId('asset-folder-row')).toHaveLength(0);
    expect(ids()).toEqual(['video:d1a', 'video:d1b']);
    // … and All files groups again, from the loaded list.
    fireEvent.click(within(screen.getByTestId('folder-tree-all')).getByRole('button'));
    await flush();
    expect(screen.getAllByTestId('asset-folder-row')).toHaveLength(3);
    expect(cards()).toHaveLength(6);
  });

  it('Move to… files the selection in a folder (find or create), then offers Undo', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    serveAssets(FOLDERED);
    renderSection();
    await flush();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select root-new.mp4' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select day2.mp4' }));
    fireEvent.click(screen.getByTestId('assets-move'));
    const picker = screen.getByTestId('folder-picker');
    expect(picker).toHaveAccessibleName('Move 2 files to');
    fireEvent.change(within(picker).getByLabelText('Find or create a folder'), { target: { value: ' Boards / Board 07 ' } });
    const create = within(picker).getByTestId('folder-picker-create');
    expect(create).toHaveTextContent('Create “Boards/Board 07” and move');
    fireEvent.mouseDown(create);
    await flush();
    expect(mocks.moveAssets).toHaveBeenCalledTimes(1); // one batch (up to 100 ids per call)
    expect(mocks.moveAssets.mock.calls.flatMap((c) => c[1]).sort()).toEqual(['video:d2', 'video:r1']);
    expect(mocks.moveAssets.mock.calls.every((c) => c[2] === 'Boards/Board 07')).toBe(true);
    const status = screen.getByTestId('assets-move-status');
    expect(status).toHaveTextContent('Moved 2 files to Boards/Board 07');
    mocks.moveAssets.mockClear();
    fireEvent.click(within(status).getByRole('button', { name: 'Undo' }));
    await flush();
    // Undo puts each file back where IT was: the root and Footage/Day 2.
    expect(mocks.moveAssets.mock.calls.map((c) => [c[1], c[2]]).sort()).toEqual([[['video:d2'], 'Footage/Day 2'], [['video:r1'], '']]);
  });

  it('dragging a selected card onto a tree folder moves the whole selection; a file drag is still an upload', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    serveAssets(FOLDERED);
    renderSection();
    await flush();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select root-new.mp4' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select root-old.mp4' }));
    const store: Record<string, string> = {};
    const dt = { types: [] as string[], setData(t: string, v: string) { store[t] = v; this.types.push(t); },
      getData: (t: string) => store[t] ?? '', effectAllowed: '', dropEffect: '' };
    const card = cards().find((c) => c.getAttribute('data-asset-id') === 'video:r1')!;
    expect(card).toHaveAttribute('draggable', 'true');
    fireEvent.dragStart(card, { dataTransfer: dt });
    const target = screen.getAllByTestId('folder-tree-node').find((n) => n.getAttribute('data-folder') === 'Footage/Day 2')!;
    const row = target.firstElementChild as HTMLElement;
    fireEvent.dragOver(row, { dataTransfer: dt });
    fireEvent.drop(row, { dataTransfer: dt });
    await flush();
    expect(mocks.moveAssets).toHaveBeenCalledWith('p1', expect.arrayContaining(['video:r1', 'video:r2']), 'Footage/Day 2');
  });

  const folderMenu = (path: string) => {
    const node = screen.getAllByTestId('folder-tree-node').find((n) => n.getAttribute('data-folder') === path)!;
    fireEvent.click(within(node).getByRole('button', { name: `Actions for folder ${path.split('/').pop()}` }));
    return screen.getByRole('menu');
  };
  const typeName = async (value: string) => {
    const input = screen.getByTestId('folder-tree-name-input');
    fireEvent.change(input, { target: { value } });
    fireEvent.keyDown(input, { key: 'Enter' });
    await act(async () => { vi.advanceTimersByTime(300); });
    await flush();
  };

  it('New folder makes one where you are: at the top level from All files, inside the open folder otherwise', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    serveAssets(FOLDERED);
    renderSection();
    await flush();
    expect(screen.getByTestId('folder-tree-new')).toHaveTextContent(/^\+New folder$/);
    fireEvent.click(screen.getByTestId('folder-tree-new'));
    await typeName('Boards');
    expect(screen.getAllByTestId('folder-tree-node').map((n) => n.getAttribute('data-folder'))).toContain('Boards');
    expect(screen.getByTestId('assets-folder-empty')).toHaveTextContent('This folder is empty');
    await pickFolder('Footage');
    expect(screen.getByTestId('folder-tree-new')).toHaveTextContent('New folder in Footage');
    fireEvent.click(screen.getByTestId('folder-tree-new'));
    expect(screen.getByRole('textbox', { name: 'New folder in Footage' })).toBeInTheDocument();
    await typeName('Day 3');
    expect(screen.getAllByTestId('folder-tree-node').map((n) => n.getAttribute('data-folder'))).toContain('Footage/Day 3');
    // … and a folder's ⋯ offers the same, inside it.
    fireEvent.click(within(folderMenu('Footage/Day 1')).getByRole('menuitem', { name: 'New folder inside' }));
    await typeName('Selects');
    expect(screen.getAllByTestId('folder-tree-node').map((n) => n.getAttribute('data-folder'))).toContain('Footage/Day 1/Selects');
    // Each is recorded on the server, so every member and device sees it before anything is filed in it.
    expect(mocks.createFolder.mock.calls.map((c) => c[1])).toEqual(['Boards', 'Footage/Day 3', 'Footage/Day 1/Selects']);
  });

  it('a name the server would refuse is explained as you type and never sent', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    serveAssets(FOLDERED);
    renderSection();
    await flush();
    fireEvent.click(within(folderMenu('Footage')).getByRole('menuitem', { name: 'Rename' }));
    const input = screen.getByTestId('folder-tree-name-input');
    fireEvent.change(input, { target: { value: 'Day #2' } });
    expect(screen.getByTestId('folder-name-error')).toHaveTextContent('A name can’t contain [ ] @ or #.');
    expect(input).toHaveAttribute('aria-invalid', 'true');
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(screen.getByTestId('folder-tree-name-input')).toBeInTheDocument(); // still open to fix
    fireEvent.change(input, { target: { value: 'a/b' } });
    expect(screen.getByTestId('folder-name-error')).toHaveTextContent('A name can’t contain “/”.');
    fireEvent.keyDown(input, { key: 'Escape' });
    await flush();
    expect(mocks.renameFolder).not.toHaveBeenCalled();
  });

  it('Rename re-files everything under it and offers Undo; onto an existing name it asks before merging', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    serveAssets(FOLDERED);
    mocks.renameFolder.mockImplementation(async (_pid: string, from: string, to: string) => ({ ok: true, value: {
      from, to, moved: ['video:d1a'], moves: [{ assetId: 'video:d1a', from: 'Footage/Day 1', to: `${to}` }], failed: [], remaining: 0 } }));
    renderSection();
    await flush();
    fireEvent.click(within(folderMenu('Footage/Day 1')).getByRole('menuitem', { name: 'Rename' }));
    await typeName('Day One');
    expect(mocks.renameFolder).toHaveBeenCalledWith('p1', 'Footage/Day 1', 'Footage/Day One');
    const status = screen.getByTestId('assets-move-status');
    expect(status).toHaveTextContent('Renamed Day 1 to Day One');
    fireEvent.click(within(status).getByRole('button', { name: 'Undo' }));
    await flush();
    expect(mocks.moveAssets).toHaveBeenCalledWith('p1', ['video:d1a'], 'Footage/Day 1');
    // Onto a name that exists: a question, nothing sent until Merge.
    mocks.renameFolder.mockClear();
    fireEvent.click(within(folderMenu('Footage/Day 2')).getByRole('menuitem', { name: 'Rename' }));
    await typeName('Day 1');
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveTextContent('“Day 1” already exists');
    expect(mocks.renameFolder).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Merge' }));
    await flush();
    expect(mocks.renameFolder).toHaveBeenCalledWith('p1', 'Footage/Day 2', 'Footage/Day 1');
  });

  it('Delete folder moves its contents up to its parent (nothing is deleted) and can be undone', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    serveAssets(FOLDERED);
    mocks.renameFolder.mockImplementation(async (_pid: string, from: string, to: string) => ({ ok: true, value: {
      from, to, moved: ['video:d1a', 'video:d1b'], failed: [], remaining: 0,
      moves: [{ assetId: 'video:d1a', from, to }, { assetId: 'video:d1b', from, to }] } }));
    renderSection();
    await flush();
    fireEvent.click(within(folderMenu('Footage/Day 1')).getByRole('menuitem', { name: 'Delete folder…' }));
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveTextContent('Delete the folder “Day 1”?');
    expect(dialog).toHaveTextContent('Its 2 items move to “Footage”. No file is deleted, and you can undo this.');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete folder' }));
    await flush();
    expect(mocks.renameFolder).toHaveBeenCalledWith('p1', 'Footage/Day 1', 'Footage');
    expect(screen.getAllByTestId('folder-tree-node').find((n) => n.getAttribute('aria-selected') === 'true'))
      .toHaveAttribute('data-folder', 'Footage'); // you land where the files went
    const status = screen.getByTestId('assets-move-status');
    expect(status).toHaveTextContent('Deleted Day 1 · 2 items moved to Footage');
    fireEvent.click(within(status).getByRole('button', { name: 'Undo' }));
    await flush();
    expect(mocks.moveAssets).toHaveBeenCalledWith('p1', ['video:d1a', 'video:d1b'], 'Footage/Day 1');
  });

  it('a folder whose files are all in subfolders shows them as tiles (open on click, drop to move), not as empty', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    serveAssets(FOLDERED);
    renderSection();
    await flush();
    await pickFolder('Footage');
    expect(screen.queryByTestId('assets-folder-empty')).toBeNull();
    const tiles = screen.getAllByTestId('assets-subfolder');
    expect(tiles.map((t) => t.textContent)).toEqual(['Day 12', 'Day 21']);
    fireEvent.click(tiles[1]);
    await act(async () => { vi.advanceTimersByTime(300); });
    await flush();
    expect(ids()).toEqual(['video:d2']);
    expect(screen.queryByTestId('assets-subfolders')).toBeNull(); // Day 2 has none
  });

  it('the folder list collapses to the Folder button and pins back; the choice is remembered', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    serveAssets(FOLDERED);
    const { unmount } = renderSection();
    await flush();
    fireEvent.click(screen.getByTestId('folder-tree-collapse'));
    expect(screen.queryByTestId('folder-tree')).toBeNull();
    fireEvent.click(screen.getByTestId('assets-folder-button'));
    const pop = screen.getByTestId('assets-folder-popover');
    expect(within(pop).getAllByTestId('folder-tree-node').length).toBeGreaterThan(0); // the same tree, every action
    unmount();
    renderSection();
    await flush();
    expect(screen.queryByTestId('folder-tree')).toBeNull();                          // remembered
    fireEvent.click(screen.getByTestId('assets-folder-button'));
    fireEvent.click(screen.getByTestId('folder-tree-pin'));
    expect(screen.getByTestId('folder-tree')).toBeInTheDocument();
    // … and the plain way back: a Show folders button where the list was.
    fireEvent.click(screen.getByTestId('folder-tree-collapse'));
    expect(screen.queryByTestId('folder-tree')).toBeNull();
    fireEvent.click(screen.getByTestId('assets-show-folders'));
    expect(screen.getByTestId('folder-tree')).toBeInTheDocument();
    expect(screen.queryByTestId('assets-show-folders')).toBeNull();
    expect(screen.getByTestId('folder-tree-collapse')).toBeInTheDocument();
    localStorage.removeItem('assets-folders-collapsed');
  });

  it('browse-only (the Browse tab) docks the same tree read-only: navigate and collapse, no folder actions', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    serveAssets(FOLDERED);
    render(<ProjectAssetsSection project={PROJECT} role="owner" onNotFound={vi.fn()} sessionId="S1" browseOnly />);
    await flush();
    const tree = screen.getByTestId('folder-tree');
    expect(within(tree).getAllByTestId('folder-tree-node').length).toBeGreaterThan(0);
    expect(within(tree).queryByRole('button', { name: /Actions for folder/ })).toBeNull();
    expect(screen.queryByTestId('folder-tree-new')).toBeNull();
    await pickFolder('Footage');
    expect(screen.getAllByTestId('assets-subfolder').length).toBeGreaterThan(0);
    fireEvent.click(screen.getByTestId('folder-tree-collapse'));
    expect(screen.queryByTestId('folder-tree')).toBeNull();
    fireEvent.click(screen.getByTestId('assets-show-folders'));
    expect(screen.getByTestId('folder-tree')).toBeInTheDocument();
    localStorage.removeItem('assets-folders-collapsed');
  });

  it('a focused folder renames with F2 and asks to delete with Delete', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    serveAssets(FOLDERED);
    renderSection();
    await flush();
    const node = screen.getAllByTestId('folder-tree-node').find((n) => n.getAttribute('data-folder') === 'Footage/Day 2')!;
    const name = within(node).getAllByRole('button').find((b) => b.getAttribute('aria-hidden') !== 'true')!;
    fireEvent.keyDown(name, { key: 'F2' });
    expect(screen.getByRole('textbox', { name: 'Rename Day 2' })).toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole('textbox', { name: 'Rename Day 2' }), { key: 'Escape' });
    const again = screen.getAllByTestId('folder-tree-node').find((n) => n.getAttribute('data-folder') === 'Footage/Day 2')!;
    fireEvent.keyDown(within(again).getAllByRole('button').find((b) => b.getAttribute('aria-hidden') !== 'true')!, { key: 'Delete' });
    expect(screen.getByRole('dialog')).toHaveTextContent('Delete the folder “Day 2”?');
  });

  it('a top-level folder deletes to Top level, which the tree shows with its own count; an empty one goes without a question', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    serveAssets(FOLDERED);
    renderSection();
    await flush();
    const top = screen.getByTestId('folder-tree-top');
    expect(top).toHaveAccessibleName('Top level, 2 files');
    fireEvent.click(within(folderMenu('Generated')).getByRole('menuitem', { name: 'Delete folder…' }));
    expect(screen.getByRole('dialog')).toHaveTextContent('Its 1 item and 1 subfolder move to Top level.');
    expect(deleteFolderMessage('Solo', [{ path: 'Solo', name: 'Solo', count: 1, total: 1, children: [] }]))
      .toBe('Its 1 item moves to Top level. No file is deleted, and you can undo this.');
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }));
    expect(mocks.renameFolder).not.toHaveBeenCalled();
    // An empty folder made here: deleted at once.
    fireEvent.click(screen.getByTestId('folder-tree-new'));
    await typeName('Scratch');
    fireEvent.click(within(folderMenu('Scratch')).getByRole('menuitem', { name: 'Delete folder…' }));
    await flush();
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getAllByTestId('folder-tree-node').map((n) => n.getAttribute('data-folder'))).not.toContain('Scratch');
    expect(mocks.renameFolder).not.toHaveBeenCalled();
    // Top level opens the files in no folder.
    fireEvent.click(within(top).getByRole('button'));
    await act(async () => { vi.advanceTimersByTime(300); });
    await flush();
    expect(ids().sort()).toEqual(['video:r1', 'video:r2']);
  });

  it('a search across every folder names each result’s folder (the location line), and opening it is one click', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: FOLDERED } });
    mocks.searchAssets.mockResolvedValue({ ok: true, value: { truncated: false, assets: [FOLDERED[2]] } });
    localStorage.setItem(ASSETS_VIEW_KEY, 'list');
    renderSection();
    await flush();
    fireEvent.change(screen.getByLabelText('Search files by name'), { target: { value: 'day 1' } });
    await act(async () => { vi.advanceTimersByTime(300); });
    await flush();
    expect(screen.queryAllByTestId('asset-folder-row')).toHaveLength(0); // flat while searching
    expect(within(cards()[0]).getByTestId('asset-name')).toHaveAttribute('title', 'Footage/Day 1/day1-new.mp4');
    // The design (mock location.html): outside its folder a file says where it lives.
    expect(within(cards()[0]).getByTestId('file-location')).toHaveAttribute('data-folder', 'Footage/Day 1');
    expect(within(cards()[0]).getByTestId('file-location')).toHaveTextContent('Footage / Day 1');
  });

  it('names a file’s folder in the flat All-files grid, but not inside the folder itself', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    serveAssets(FOLDERED);
    renderSection();
    await flush();
    // All files: every foldered file says where it lives; a top-level one has nothing to add here.
    const flat = cards().map((c) => within(c).queryByTestId('file-location')?.getAttribute('data-folder') ?? null);
    expect(flat.filter(Boolean).sort()).toEqual(['Footage/Day 1', 'Footage/Day 1', 'Footage/Day 2', 'Generated/Trailer · 2026-10-07']);
    await pickFolder('Footage/Day 1');
    expect(ids()).toEqual(['video:d1a', 'video:d1b']);
    expect(screen.queryAllByTestId('file-location')).toHaveLength(0);
  });

  it('Upload ▾ Folder opens a webkitdirectory picker and uploads each file with its folder', async () => {
    mocks.createUploads.mockResolvedValue({ ok: true, value: { uploads: [], errors: {} } });
    renderSection();
    await flush();
    const input = screen.getByTestId('project-folder-input') as HTMLInputElement;
    expect(input).toHaveAttribute('webkitdirectory');
    const click = vi.spyOn(input, 'click');
    openUploadMenu();
    fireEvent.click(screen.getByRole('menuitem', { name: 'Folder' }));
    expect(click).toHaveBeenCalled();
    const mk = (rel: string) => {
      const f = new File([new Uint8Array(3)], rel.split('/').pop() as string, { type: 'video/mp4' });
      Object.defineProperty(f, 'webkitRelativePath', { value: rel });
      return f;
    };
    fireEvent.change(input, { target: { files: [mk('Footage/Day 1/a.mp4'), mk('Footage/.DS_Store'), mk('Footage/b.mp4')] } });
    await flush();
    await flush();
    expect(mocks.createUploads.mock.calls[0][1]).toEqual([
      { name: 'a.mp4', size: 3, contentType: 'video/mp4', folder: 'Footage/Day 1' },
      { name: 'b.mp4', size: 3, contentType: 'video/mp4', folder: 'Footage' },
    ]);
  });

  it('a dropped folder is walked and uploads with its relative folders; the hint says files or folders', async () => {
    mocks.createUploads.mockResolvedValue({ ok: true, value: { uploads: [], errors: {} } });
    renderSection();
    await flush();
    const fe = (name: string) => ({ isFile: true, isDirectory: false, name, file: (cb: (f: File) => void) => cb(new File([new Uint8Array(2)], name, { type: 'video/mp4' })) });
    const de = (name: string, children: unknown[]) => ({
      isFile: false, isDirectory: true, name,
      createReader: () => { let done = false; return { readEntries: (cb: (b: unknown[]) => void) => { cb(done ? [] : children); done = true; } }; },
    });
    const root = de('Footage', [fe('a.mp4'), de('Day 1', [fe('b.mp4')])]);
    const zone = screen.getByTestId('project-assets');
    fireEvent.dragOver(zone, { dataTransfer: { types: ['Files'], files: [] } });
    expect(zone).toHaveTextContent('Drop files or folders here');
    fireEvent.drop(zone, { dataTransfer: { types: ['Files'], files: [], items: [{ kind: 'file', webkitGetAsEntry: () => root, getAsFile: () => null }] } });
    await flush();
    await flush();
    expect((mocks.createUploads.mock.calls[0][1] as { name: string; folder?: string }[]).map((f) => [f.folder, f.name])).toEqual([
      ['Footage', 'a.mp4'], ['Footage/Day 1', 'b.mp4'],
    ]);
  });
});

describe('large batches', () => {
  const many = (n: number) => Array.from({ length: n }, (_, i) => new File([new Uint8Array(1)], `f${i}.png`, { type: 'image/png' }));
  const pickMany = (files: File[]) => fireEvent.change(screen.getByTestId('project-upload-input'), { target: { files } });

  it('over 50 files shows one summary row with Retry failed, the per-file rows behind Show files', async () => {
    // The first grant request fails (20 failed); the later ones never answer.
    mocks.createUploads
      .mockResolvedValueOnce({ ok: false, kind: 'error', status: 503, code: 'x', message: 'Try later.' })
      .mockReturnValue(new Promise(() => {}));
    renderSection();
    await flush();
    pickMany(many(60));
    await flush();
    await flush();
    const summary = screen.getByTestId('upload-batch-summary');
    expect(within(summary).getByTestId('upload-batch-counts')).toHaveTextContent('0 of 60 uploaded · 20 failed');
    expect(screen.queryAllByTestId('upload-row')).toHaveLength(0);
    const toggle = within(summary).getByRole('button', { name: 'Show files' });
    fireEvent.click(toggle);
    expect(screen.getAllByTestId('upload-row')).toHaveLength(60);
    expect(within(summary).getByRole('button', { name: 'Hide files' })).toHaveAttribute('aria-expanded', 'true');
    fireEvent.click(within(summary).getByRole('button', { name: 'Retry failed' }));
    await flush();
    expect(within(summary).getByTestId('upload-batch-counts')).toHaveTextContent('0 of 60 uploaded');
    expect(within(summary).getByTestId('upload-batch-counts')).not.toHaveTextContent('failed');
  });

  it('formats large counts: 312 of 1,000 uploaded · 2 failed', async () => {
    const { uploadCountsText } = await import('./UploadFileList');
    expect(uploadCountsText({ total: 1000, ready: 312, failed: 2, rejected: 0, canceled: 0, inProgress: 686 }))
      .toBe('312 of 1,000 uploaded · 2 failed');
  });

  it('indexing ON and more than 200 files: confirms the cost first; Cancel uploads nothing', async () => {
    mocks.createUploads.mockResolvedValue({ ok: true, value: { uploads: [], errors: {} } });
    renderSection();
    await flush();
    pickMany(many(201));
    await flush();
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveTextContent('201 files will be analyzed (media workflows run for each). Continue?');
    expect(mocks.getProject).toHaveBeenCalledWith('p1');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    await flush();
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(mocks.createUploads).not.toHaveBeenCalled();

    pickMany(many(201));
    await flush();
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Continue' }));
    await flush();
    expect(mocks.createUploads).toHaveBeenCalled();
    expect(mocks.getProject).toHaveBeenCalledTimes(1); // read once
  });

  it('asks nothing for 200 files, or for a Project with indexing OFF', async () => {
    mocks.createUploads.mockResolvedValue({ ok: true, value: { uploads: [], errors: {} } });
    renderSection();
    await flush();
    pickMany(many(200));
    await flush();
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(mocks.getProject).not.toHaveBeenCalled();
    expect(mocks.createUploads).toHaveBeenCalled();
    cleanup();
    __resetUploadQueueForTests();
    mocks.createUploads.mockClear();

    mocks.getProject.mockResolvedValue({ ok: true, value: { project: { projectId: 'p1', name: 'x', personal: false, indexing: false }, role: 'owner' } });
    renderSection();
    await flush();
    pickMany(many(500));
    await flush();
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(mocks.createUploads).toHaveBeenCalled();
  });

  it('uses the host’s indexing prop without reading the Project', async () => {
    mocks.createUploads.mockResolvedValue({ ok: true, value: { uploads: [], errors: {} } });
    render(<ProjectAssetsSection project={PROJECT} role="owner" onNotFound={vi.fn()} indexing={false} />);
    await flush();
    pickMany(many(300));
    await flush();
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(mocks.getProject).not.toHaveBeenCalled();
  });
});

// ------------------------------------------------------------------------- asset-lineage R10 / R11

describe('bulk select and delete (R10)', () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => localStorage.clear());

  const ids = () => cards().map((c) => c.getAttribute('data-asset-id'));
  const check = (name: string) => fireEvent.click(screen.getByRole('checkbox', { name: `Select ${name}` }));
  const bar = () => screen.getByTestId('assets-selection-bar');
  const noBar = () => expect(screen.queryByTestId('assets-selection-bar')).toBeNull();

  it('selects cards with a checkbox, deletes them after one "Delete N files" confirmation, and refreshes once', async () => {
    mocks.listAssets
      .mockResolvedValueOnce({ ok: true, value: { assets: [asset('video:1'), asset('video:2'), asset('video:3')] } })
      .mockResolvedValue({ ok: true, value: { assets: [asset('video:2')] } });
    renderSection();
    await flush();
    expect(screen.getAllByTestId('card-select')).toHaveLength(3);
    check('video:1.mp4');
    check('video:3.mp4');
    expect(bar()).toHaveTextContent('2 selected');
    fireEvent.click(within(bar()).getByRole('button', { name: 'Delete' }));
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveTextContent('Delete 2 files');
    expect(within(dialog).getByTestId('bulk-delete-names')).toHaveTextContent('video:1.mp4video:3.mp4');
    expect(mocks.removeAsset).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
    await flush();
    expect(mocks.removeAsset.mock.calls).toEqual([['p1', 'video:1'], ['p1', 'video:3']]);
    expect(mocks.listAssets).toHaveBeenCalledTimes(2); // the first page, then ONE refresh at the end
    expect(ids()).toEqual(['video:2']);
    noBar();
    expect(screen.getByTestId('assets-toolbar')).toBeInTheDocument();
  });

  it('Clear empties the selection; Cancel deletes nothing', async () => {
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [asset('video:1'), asset('video:2')] } });
    renderSection();
    await flush();
    check('video:1.mp4');
    fireEvent.click(within(bar()).getByRole('button', { name: 'Delete' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }));
    expect(mocks.removeAsset).not.toHaveBeenCalled();
    expect(bar()).toHaveTextContent('1 selected');
    fireEvent.click(within(bar()).getByRole('button', { name: 'Clear' }));
    noBar();
    expect(screen.getByRole('checkbox', { name: 'Select video:1.mp4' })).not.toBeChecked();
  });

  it('Select all applies to the current folder filter only, and names 5 files then "+ N more"', async () => {
    const inDay = Array.from({ length: 7 }, (_, i) => asset(`video:d${i}`, { displayName: `d${i}.mp4`, folder: 'Footage' }));
    vi.useFakeTimers({ shouldAdvanceTime: true });
    serveAssets([asset('video:top', { displayName: 'top.mp4' }), ...inDay]);
    renderSection();
    await flush();
    await pickFolder('Footage');
    check('d3.mp4');
    expect(bar()).toHaveTextContent('1 selected');
    fireEvent.click(screen.getByTestId('assets-select-all'));
    expect(bar()).toHaveTextContent('7 selected');
    expect(screen.getByTestId('assets-select-all')).toBeDisabled();
    fireEvent.click(within(bar()).getByRole('button', { name: 'Delete' }));
    const names = within(screen.getByRole('dialog')).getByTestId('bulk-delete-names');
    expect(Array.from(names.querySelectorAll('li')).map((li) => li.textContent)).toEqual([
      'd0.mp4', 'd1.mp4', 'd2.mp4', 'd3.mp4', 'd4.mp4', '+ 2 more',
    ]);
    expect(screen.getByRole('dialog')).toHaveTextContent('Delete 7 files');
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete' }));
    await flush();
    expect(mocks.removeAsset.mock.calls.map((c) => c[1]).sort()).toEqual(inDay.map((a) => a.assetId).sort());
    expect(mocks.removeAsset).not.toHaveBeenCalledWith('p1', 'video:top');
  });

  it('Select all over a search result selects only the matches; Clear brings the search back as it was', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [asset('video:1'), asset('video:2'), asset('video:3')] } });
    mocks.searchAssets.mockResolvedValue({ ok: true, value: { assets: [asset('video:2')], truncated: false } });
    renderSection();
    await flush();
    fireEvent.change(screen.getByLabelText('Search files by name'), { target: { value: 'two' } });
    await act(async () => { vi.advanceTimersByTime(300); });
    await flush();
    check('video:2.mp4');
    fireEvent.click(screen.getByTestId('assets-select-all'));
    expect(bar()).toHaveTextContent('1 selected');
    fireEvent.click(within(bar()).getByRole('button', { name: 'Clear' }));
    noBar();
    expect(screen.getByLabelText('Search files by name')).toHaveValue('two');
    expect(cards().map((c) => c.getAttribute('data-asset-id'))).toEqual(['video:2']);
  });

  it('List view rows have checkboxes; a collapsed folder’s rows leave the selection on screen', async () => {
    localStorage.setItem(ASSETS_VIEW_KEY, 'list');
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [
      asset('video:r', { displayName: 'root.mp4' }),
      asset('video:f', { displayName: 'inside.mp4', folder: 'Footage' }),
    ] } });
    renderSection();
    await flush();
    expect(cards().every((c) => c.querySelector('input[type="checkbox"]'))).toBe(true);
    check('root.mp4');
    fireEvent.click(screen.getByTestId('assets-select-all'));
    expect(bar()).toHaveTextContent('2 selected');
    fireEvent.click(within(screen.getByTestId('asset-folder-row')).getByRole('button'));
    expect(bar()).toHaveTextContent('1 selected');
  });

  it('runs at most 6 removes at once, shows "Deleting k of N", and lists each failure', async () => {
    const many = Array.from({ length: 8 }, (_, i) => asset(`video:${i}`, { displayName: `f${i}.mp4` }));
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: many } });
    const pending: Array<(v: unknown) => void> = [];
    mocks.removeAsset.mockImplementation(() => new Promise((r) => pending.push(r)));
    renderSection();
    await flush();
    check('f0.mp4');
    fireEvent.click(screen.getByTestId('assets-select-all'));
    fireEvent.click(within(bar()).getByRole('button', { name: 'Delete' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete' }));
    await flush();
    expect(mocks.removeAsset).toHaveBeenCalledTimes(6);
    expect(within(bar()).getByRole('status')).toHaveTextContent('Deleting 1 of 8');
    expect(cards()).toHaveLength(0); // every target hides at once
    await act(async () => {
      pending[0]({ ok: false, kind: 'error', status: 500, code: 'boom', message: 'Storage unavailable' });
      pending[1]({ ok: true, value: undefined });
    });
    await flush();
    expect(mocks.removeAsset).toHaveBeenCalledTimes(8);
    expect(within(bar()).getByRole('status')).toHaveTextContent('Deleting 3 of 8');
    expect(mocks.listAssets).toHaveBeenCalledTimes(1); // no refresh until every remove settles
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [many[0]] } });
    await act(async () => {
      for (const r of pending.slice(2)) r({ ok: false, kind: 'not_found' }); // gone already counts as deleted
    });
    await flush();
    expect(mocks.listAssets).toHaveBeenCalledTimes(2);
    const failures = screen.getByTestId('bulk-delete-failures');
    expect(failures).toHaveTextContent("Couldn't delete 1 file:");
    expect(failures).toHaveTextContent('f0.mp4: Storage unavailable');
    expect(cards().map((c) => c.getAttribute('data-asset-id'))).toEqual(['video:0']);
    fireEvent.click(within(failures).getByRole('button', { name: 'Dismiss' }));
    expect(screen.queryByTestId('bulk-delete-failures')).toBeNull();
  });
});

describe('session folders and analysis files (R11)', () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => localStorage.clear());

  const SESSION = 'Sessions/Trailer cut · 2026-10-08';
  const ROWS = [
    asset('video:legacy', { displayName: 'legacy.mp4' }), // no role: a source
    asset('video:del', { displayName: 'final.mp4', role: 'deliverable', source: 'session', folder: SESSION }),
    asset('data:shots', { kind: 'data', displayName: 'shots.json', role: 'analysis', status: 'stored', folder: SESSION }),
    asset('image:frame', { kind: 'image', displayName: 'frame.jpg', role: 'intermediate', folder: SESSION }),
    asset('video:gen', { displayName: 'old-gen.mp4', source: 'generated', folder: 'Generated/Old · 2026-09-01' }),
  ];
  const ids = () => cards().map((c) => c.getAttribute('data-asset-id'));

  it('All types hides analysis and intermediate files; the Type select’s Analysis files shows them', async () => {
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: ROWS } });
    renderSection();
    await flush();
    expect(screen.queryByRole('switch')).toBeNull(); // folded into the Type select
    const type = screen.getByLabelText('File type') as HTMLSelectElement;
    expect(Array.from(type.options).map((o) => o.textContent)).toEqual([
      'All types', 'Video', 'Image', 'Audio', 'Document', 'Analysis files', 'Metadata',
    ]);
    expect(type).toHaveValue('');
    expect(ids()).toEqual(['video:legacy', 'video:del', 'video:gen']);
    fireEvent.change(type, { target: { value: ANALYSIS_TYPE } });
    expect(ids()).toEqual(['data:shots', 'image:frame']);
    expect(mocks.searchAssets).not.toHaveBeenCalled(); // a role filter, not a server kind
    // In the List view too (flat: they are filtered, not grouped).
    fireEvent.click(screen.getByRole('radio', { name: 'List view' }));
    expect(ids()).toEqual(['data:shots', 'image:frame']);
    fireEvent.change(type, { target: { value: '' } });
    expect(ids()).toEqual(['video:legacy', 'video:gen']); // the Sessions folder starts collapsed in List
  });

  it('the Type select’s Metadata shows only kind:data files (including role=analysis ones) and never searches', async () => {
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: ROWS } });
    renderSection();
    await flush();
    const type = screen.getByLabelText('File type') as HTMLSelectElement;
    fireEvent.change(type, { target: { value: DATA_TYPE } });
    expect(ids()).toEqual(['data:shots']); // the only kind:'data' row, surfaced despite its analysis role
    expect(mocks.searchAssets).not.toHaveBeenCalled(); // a client-side kind filter, not a server kind
    // The toolbar controls (and their order) do not shift when the metadata filter is active.
    const bar = screen.getByTestId('assets-toolbar');
    const controls = Array.from(bar.querySelectorAll('input, select, button, [role="radiogroup"]'))
      .filter((el) => !el.closest('[role="radiogroup"]') || el.getAttribute('role') === 'radiogroup')
      .map((el) => el.getAttribute('aria-label'));
    expect(controls).toEqual(['Search files by name', 'File type', 'View', UPLOAD_BUTTON_NAME]);
    expect(bar.style.flexWrap).toBe('nowrap');
    // In the List view too it is a flat list (filtered, not grouped) and still only the data file.
    fireEvent.click(screen.getByRole('radio', { name: 'List view' }));
    expect(ids()).toEqual(['data:shots']);
    expect(screen.queryAllByTestId('asset-folder-row')).toHaveLength(0);
  });

  it('Metadata on a project with no data files says so, without hiding the toolbar', async () => {
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [ROWS[0], ROWS[1]] } });
    renderSection();
    await flush();
    fireEvent.change(screen.getByLabelText('File type'), { target: { value: DATA_TYPE } });
    expect(cards()).toHaveLength(0);
    expect(screen.getByTestId('assets-metadata-empty')).toHaveTextContent('No metadata files here.');
    expect(screen.getByTestId('assets-toolbar')).toBeInTheDocument();
  });

  it('lists Sessions/… and Generated/… folders, and an opened Sessions folder shows everything in it', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    serveAssets(ROWS);
    renderSection();
    await flush();
    // Chat folders (`Sessions/…`, now `Chats/…`) come last, after the user's own; they start collapsed.
    expect(screen.getAllByTestId('folder-tree-node').map((n) => n.getAttribute('data-folder'))).toEqual([
      'Generated', 'Generated/Old · 2026-09-01', 'Sessions',
    ]);
    fireEvent.click(within(screen.getAllByTestId('folder-tree-node')[2]).getAllByRole('button', { hidden: true })[0]);
    await pickFolder(SESSION);
    expect(ids()).toEqual(['video:del', 'data:shots', 'image:frame']);
  });

  it('List view: a Sessions folder starts collapsed and shows everything once opened; other folders start open', async () => {
    localStorage.setItem(ASSETS_VIEW_KEY, 'list');
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: ROWS } });
    renderSection();
    await flush();
    const rows = screen.getAllByTestId('asset-folder-row');
    expect(rows.map((r) => r.getAttribute('data-folder'))).toEqual(['Generated/Old · 2026-09-01', SESSION]);
    const session = within(rows[1]).getByRole('button');
    expect(session).toHaveAttribute('aria-expanded', 'false');
    expect(session).toHaveAccessibleName(`${SESSION}, 3 files`);
    expect(ids()).toEqual(['video:legacy', 'video:gen']);
    fireEvent.click(session);
    expect(ids()).toEqual(['video:legacy', 'video:gen', 'video:del', 'data:shots', 'image:frame']);
  });

  it('says when only analysis files are hidden', async () => {
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [ROWS[2]] } });
    renderSection();
    await flush();
    expect(cards()).toHaveLength(0);
    expect(screen.getByTestId('assets-analysis-hidden')).toHaveTextContent('Choose Analysis files');
    expect(screen.queryByTestId('project-assets-empty')).toBeNull();
  });

  it('draws a data file with its icon, no thumbnail request, its role, and opens it in a new tab', async () => {
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [ROWS[2]] } });
    mocks.requestUrls.mockResolvedValue(new Map([['data:shots', { ok: true, entry: { source: { url: 'https://s/shots', expiresAt: 1 } } }]]));
    const win = { opener: {} as unknown, location: { href: '' }, close: vi.fn() };
    const open = vi.spyOn(window, 'open').mockReturnValue(win as unknown as Window);
    renderSection();
    await flush();
    fireEvent.change(screen.getByLabelText('File type'), { target: { value: ANALYSIS_TYPE } });
    const card = cards()[0];
    expect(card).toHaveTextContent('Data');
    expect(card).toHaveTextContent('Analysis');
    expect(within(card).getByTestId('card-kind-icon').querySelector('svg')).not.toBeNull();
    expect(card.querySelector('img')).toBeNull();
    expect(mocks.requestUrls).not.toHaveBeenCalled();
    fireEvent.mouseEnter(cards()[0]);
    fireEvent.click(screen.getByRole('button', { name: 'Open shots.json in the side panel' }));
    await flush();
    expect(win.location.href).toBe('https://s/shots');
    expect(mocks.openAssetAt).not.toHaveBeenCalled();
    open.mockRestore();
  });
});

// ------------------------------------------------------------------------- minimal toolbar / selection mode

describe('toolbar: one fixed row in both views (the Icons ↔ List layout-shift regression)', () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => localStorage.clear());

  /** Every control in the toolbar row, in DOM order, by accessible name. */
  const toolbarControls = () => {
    const bar = screen.getByTestId('assets-toolbar');
    return Array.from(bar.querySelectorAll('input, select, button, [role="radiogroup"]'))
      .filter((el) => !el.closest('[role="radiogroup"]') || el.getAttribute('role') === 'radiogroup')
      .map((el) => el.getAttribute('aria-label'));
  };

  it('renders the same controls in the same DOM order in the Icons and List views, without wrapping', async () => {
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [
      asset('video:1', { displayName: 'a.mp4', folder: 'Footage' }),
      asset('video:2', { displayName: 'b.mp4' }),
    ] } });
    renderSection();
    await flush();
    const bar = screen.getByTestId('assets-toolbar');
    // No Folder select on the full surface: the folder tree beside the grid does that job.
    const expected = ['Search files by name', 'File type', 'View', UPLOAD_BUTTON_NAME];
    expect(toolbarControls()).toEqual(expected);
    expect(bar.style.flexWrap).toBe('nowrap');
    // No section heading inside the section: the Assets tab already names it; the region keeps its name.
    expect(within(screen.getByTestId('project-assets')).queryByRole('heading')).toBeNull();
    expect(screen.getByRole('region', { name: 'Assets' })).toBe(screen.getByTestId('project-assets'));

    fireEvent.click(screen.getByRole('radio', { name: 'List view' }));
    expect(screen.getByTestId('project-assets-grid')).toHaveAttribute('data-view', 'list');
    expect(screen.getByTestId('assets-toolbar')).toBe(bar); // the same node: nothing was re-mounted
    expect(toolbarControls()).toEqual(expected);

    fireEvent.click(screen.getByRole('radio', { name: 'Icons view' }));
    expect(toolbarControls()).toEqual(expected);
  });

  it('shows the Folder select only when the Project has folders, in either view', async () => {
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [asset('video:1'), asset('video:2')] } });
    renderSection();
    await flush();
    const expected = ['Search files by name', 'File type', 'View', UPLOAD_BUTTON_NAME];
    expect(toolbarControls()).toEqual(expected);
    fireEvent.click(screen.getByRole('radio', { name: 'List view' }));
    expect(toolbarControls()).toEqual(expected);
  });

  it('only the search field shrinks; every other control keeps its size', async () => {
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [asset('video:1', { folder: 'Footage' })] } });
    renderSection();
    await flush();
    expect(screen.getByLabelText('Search files by name').style.flexShrink).toBe('1');
    expect(screen.getByLabelText('File type').style.flexShrink).toBe('0');
    expect(screen.getByRole('radiogroup', { name: 'View' }).style.flexShrink).toBe('0');
    expect(screen.getByRole('button', { name: UPLOAD_BUTTON_NAME }).style.flexShrink).toBe('0');
  });
});

describe('status badges only for states that need attention', () => {
  it('draws no badge for stored / indexed / ready, and one for queued / analyzing / indexing / failed', async () => {
    const statuses = ['stored', 'indexed', 'ready', 'queued', 'analyzing', 'indexing', 'failed', 'uploading'] as const;
    mocks.listAssets.mockResolvedValue({ ok: true, value: {
      assets: statuses.map((st) => asset(`video:${st}`, { displayName: `${st}.mp4`, status: st })),
    } });
    renderSection();
    await flush();
    const byStatus = (st: string) => cards().find((c) => c.getAttribute('data-status') === st) as HTMLElement;
    for (const st of ['stored', 'indexed', 'ready']) {
      expect(byStatus(st)).not.toHaveTextContent(/Stored|Ready|Indexed/);
    }
    expect(within(byStatus('queued')).getByText('Queued')).toBeInTheDocument();
    expect(within(byStatus('analyzing')).getByText('Analyzing')).toBeInTheDocument();
    expect(within(byStatus('indexing')).getByText('Indexing')).toBeInTheDocument();
    expect(within(byStatus('failed')).getByText('Failed')).toBeInTheDocument();
    expect(within(byStatus('uploading')).getByText('Uploading')).toBeInTheDocument();
  });

  it('an indexed file carries a quiet check (searchable); a stored or ready one does not', async () => {
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [
      asset('video:1', { displayName: 'done.mp4', status: 'indexed' }),
      asset('video:2', { displayName: 'kept.mp4', status: 'stored' }),
      asset('video:3', { displayName: 'ok.mp4', status: 'ready' }),
    ] } });
    renderSection();
    await flush();
    const [indexed, stored, ready] = cards();
    expect(within(indexed).getByTestId('card-indexed')).toHaveAttribute('title', expect.stringMatching(/searchable/));
    expect(within(stored).queryByTestId('card-indexed')).toBeNull();
    expect(within(ready).queryByTestId('card-indexed')).toBeNull();
  });

  it('a stored audio file with no speech says "Not indexed" (neutral), never Failed', async () => {
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [
      asset('audio:1', { kind: 'audio', displayName: 'audio_music.wav', status: 'stored',
        indexSkippedReason: 'no_speech' }),
      asset('audio:2', { kind: 'audio', displayName: 'plain.wav', status: 'stored' }),
    ] } });
    renderSection();
    await flush();
    const [skipped, plain] = cards();
    const badge = within(skipped).getByText('Not indexed');
    expect(badge).toHaveAttribute('title', expect.stringMatching(/No usable speech to index/));
    expect(skipped).not.toHaveTextContent('Failed');
    expect(within(skipped).queryByRole('button', { name: 'Retry' })).toBeNull();
    expect(plain).not.toHaveTextContent(/Not indexed|Stored|Failed/);
  });

  it('a stored PDF with no text layer says "Not indexed" (neutral), never Failed', async () => {
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [
      asset('document:1', { kind: 'document', displayName: 'scan.pdf', status: 'stored',
        indexSkippedReason: 'no_text_layer' }),
    ] } });
    renderSection();
    await flush();
    const [skipped] = cards();
    const badge = within(skipped).getByText('Not indexed');
    expect(badge).toHaveAttribute('title', expect.stringMatching(/No extractable text/));
    expect(skipped).not.toHaveTextContent('Failed');
    expect(within(skipped).queryByRole('button', { name: 'Retry' })).toBeNull();
  });

  it('List view rows: name, type, size, actions, and no badge at rest', async () => {
    localStorage.setItem(ASSETS_VIEW_KEY, 'list');
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [
      asset('image:1', { kind: 'image', displayName: 'kiro.jpg', status: 'stored', sourceSize: 249 * 1024 }),
      asset('video:2', { displayName: 'cut.mp4', status: 'failed', failure: 'decode' }),
    ] } });
    renderSection();
    await flush();
    const [row, failed] = cards();
    expect(row).toHaveTextContent('kiro.jpg');
    expect(row).toHaveTextContent('Image');
    expect(row).toHaveTextContent('249 KB');
    expect(row).not.toHaveTextContent('Stored');
    expect(within(row).getByRole('button', { name: 'Actions for kiro.jpg' })).toBeInTheDocument();
    expect(within(failed).getByText('Failed')).toHaveAttribute('title', 'decode');
    expect(within(failed).getByRole('button', { name: 'Details' })).toBeInTheDocument();
    localStorage.clear();
  });

  it('a grid card is a name line and one meta line ("Image · 249 KB"); its folder lives in the tooltip', async () => {
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [
      asset('image:1', { kind: 'image', displayName: 'kiro-folder-b.jpg', status: 'stored', sourceSize: 249 * 1024,
        folder: 'Footage/Day 2' }),
    ] } });
    renderSection();
    await flush();
    const card = cards()[0];
    const name = within(card).getByTestId('card-title');
    expect(name).toHaveTextContent('kiro-folder-b.jpg');
    expect(name).toHaveAttribute('title', 'Footage/Day 2/kiro-folder-b.jpg');
    expect(name.style.whiteSpace).toBe('nowrap');
    expect(name.style.textOverflow).toBe('ellipsis');
    expect(card).toHaveTextContent('Image · 249 KB');
    expect(card).not.toHaveTextContent('Footage/Day 2');
    expect(within(card).queryByTestId('asset-folder')).toBeNull();
  });
});

describe('selection mode', () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => localStorage.clear());

  const boxes = () => screen.getAllByTestId('card-select') as HTMLInputElement[];
  const visible = (el: HTMLElement) => el.getAttribute('data-visible') === 'true';

  it("a card's ⋯ Move to folder…, cancelled, leaves nothing selected", async () => {
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [asset('video:1', { displayName: 'a.mp4' }), asset('video:2')] } });
    renderSection();
    await flush();
    fireEvent.click(screen.getByRole('button', { name: 'More for a.mp4' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Move to folder…' }));
    expect(screen.getByTestId('folder-picker')).toBeInTheDocument();
    fireEvent.keyDown(screen.getByLabelText('Find or create a folder'), { key: 'Escape' });
    expect(screen.queryByTestId('assets-selection-bar')).toBeNull();
    expect(boxes().every((b) => !b.checked)).toBe(true);
  });

  it('Shift-click selects the range since the last pick, and while selecting a click on a card selects it', async () => {
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [asset('video:1'), asset('video:2'), asset('video:3'), asset('video:4')] } });
    renderSection();
    await flush();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select video:1.mp4' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select video:3.mp4' }), { shiftKey: true });
    expect(boxes().map((b) => b.checked)).toEqual([true, true, true, false]);
    // Selecting: the picture selects (no preview opens); the Preview icon still previews.
    fireEvent.click(cards()[3].querySelector('[data-card-media]')!);
    expect(boxes()[3].checked).toBe(true);
    expect(screen.queryByRole('dialog')).toBeNull();
    fireEvent.click(cards()[0].querySelector('[data-card-media]')!);
    expect(boxes()[0].checked).toBe(false);
  });

  it('hides every checkbox until hover or focus, then shows them all once one is selected', async () => {
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [asset('video:1'), asset('video:2'), asset('video:3')] } });
    renderSection();
    await flush();
    expect(screen.queryByTestId('assets-selection-bar')).toBeNull();
    expect(screen.queryByText('Select all')).toBeNull();
    expect(boxes()).toHaveLength(3);
    expect(boxes().some(visible)).toBe(false);
    expect(boxes()[0].style.opacity).toBe('0');

    // Hover shows the one card's checkbox …
    fireEvent.mouseEnter(cards()[1]);
    expect(boxes().map(visible)).toEqual([false, true, false]);
    fireEvent.mouseLeave(cards()[1]);
    expect(boxes().some(visible)).toBe(false);
    // … and so does keyboard focus (it stays in the tab order).
    expect(boxes()[2].tabIndex).not.toBe(-1);
    fireEvent.focus(boxes()[2]);
    expect(boxes().map(visible)).toEqual([false, false, true]);
    fireEvent.blur(boxes()[2]);
    expect(boxes().some(visible)).toBe(false);

    // Selecting one: every checkbox shows, and the toolbar row becomes the selection bar in place.
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select video:1.mp4' }));
    expect(boxes().every(visible)).toBe(true);
    expect(screen.queryByTestId('assets-toolbar')).toBeNull();
    const bar = screen.getByTestId('assets-selection-bar');
    expect(bar.style.height).toBe('32px');
    expect(bar).toHaveTextContent('1 selected');
    expect(within(bar).getByRole('button', { name: 'Select all' })).toBeInTheDocument();
    expect(within(bar).getByRole('button', { name: 'Delete' })).toBeInTheDocument();
    expect(within(bar).getByRole('button', { name: 'Clear' })).toBeInTheDocument();

    fireEvent.click(within(bar).getByRole('button', { name: 'Select all' }));
    expect(screen.getByTestId('assets-selection-bar')).toHaveTextContent('3 selected');

    // Clear leaves selection mode: the toolbar is back (same height) and the checkboxes hide again.
    fireEvent.click(within(screen.getByTestId('assets-selection-bar')).getByRole('button', { name: 'Clear' }));
    expect(screen.queryByTestId('assets-selection-bar')).toBeNull();
    expect(screen.getByTestId('assets-toolbar').style.height).toBe('32px');
    expect(boxes().some(visible)).toBe(false);
    expect(boxes().some((b) => b.checked)).toBe(false);
  });

  it('bulk delete from the selection bar still confirms once, removes each, and returns to the toolbar', async () => {
    localStorage.setItem(ASSETS_VIEW_KEY, 'list');
    mocks.listAssets
      .mockResolvedValueOnce({ ok: true, value: { assets: [asset('video:1'), asset('video:2')] } })
      .mockResolvedValue({ ok: true, value: { assets: [] } });
    renderSection();
    await flush();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select video:2.mp4' }));
    fireEvent.click(screen.getByTestId('assets-select-all'));
    fireEvent.click(within(screen.getByTestId('assets-selection-bar')).getByRole('button', { name: 'Delete' }));
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveTextContent('Delete 2 files');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
    await flush();
    expect(mocks.removeAsset.mock.calls.map((c) => c[1]).sort()).toEqual(['video:1', 'video:2']);
    expect(screen.queryByTestId('assets-selection-bar')).toBeNull();
    expect(screen.getByTestId('assets-toolbar')).toBeInTheDocument();
  });
});

describe('Upload ▾ menu', () => {
  it('offers Files and Folder, each opening its own picker; Escape closes and refocuses the button', async () => {
    renderSection();
    await flush();
    const files = screen.getByTestId('project-upload-input') as HTMLInputElement;
    const folder = screen.getByTestId('project-folder-input') as HTMLInputElement;
    const clickFiles = vi.spyOn(files, 'click');
    const clickFolder = vi.spyOn(folder, 'click');
    const trigger = screen.getByRole('button', { name: UPLOAD_BUTTON_NAME });
    expect(trigger).toHaveTextContent('Upload');
    expect(trigger).toHaveAttribute('aria-haspopup', 'menu');
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('button', { name: 'Upload folder' })).toBeNull(); // one button, not two

    openUploadMenu();
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    const menu = screen.getByRole('menu', { name: 'Upload' });
    expect(within(menu).getAllByRole('menuitem').map((m) => m.textContent)).toEqual(['Files', 'Folder', 'Import from S3…']);
    fireEvent.click(within(menu).getByRole('menuitem', { name: 'Files' }));
    expect(clickFiles).toHaveBeenCalledTimes(1);
    expect(clickFolder).not.toHaveBeenCalled();
    expect(screen.queryByRole('menu')).toBeNull();

    openUploadMenu();
    fireEvent.click(screen.getByRole('menuitem', { name: 'Folder' }));
    expect(clickFolder).toHaveBeenCalledTimes(1);
    expect(clickFiles).toHaveBeenCalledTimes(1);

    openUploadMenu();
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' });
    expect(screen.queryByRole('menu')).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });
});

// The List view's column header: Name · Type · Size, each a sort (asc → desc → default), remembered.
describe('List view header and sort', () => {
  const FILES = [
    asset('video:b', { displayName: 'b-clip.mp4', kind: 'video', sourceSize: 300 }),
    asset('document:a', { displayName: 'A-report.pdf', kind: 'document', sourceSize: 5_000 }),
    asset('image:c', { displayName: 'c10.jpg', kind: 'image', sourceSize: undefined }), // no size: sorts last both ways
    asset('audio:d', { displayName: 'c2.wav', kind: 'audio', sourceSize: 40 }),
  ];
  const ids = () => cards().map((c) => c.getAttribute('data-asset-id'));

  beforeEach(() => {
    localStorage.removeItem('projects-assets-sort');
    localStorage.setItem(ASSETS_VIEW_KEY, 'list');
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: FILES } });
  });

  it('shows Name, Type and Size headers over the list, and none in the Icons view', async () => {
    renderSection();
    await flush();
    const header = screen.getByTestId('assets-list-header');
    expect(header).toHaveTextContent(/Name.*Type.*Size/);
    fireEvent.click(screen.getByRole('radio', { name: /Icons/ }));
    expect(screen.queryByTestId('assets-list-header')).toBeNull();
  });

  it('sorts by each column, asc then desc then back to newest first', async () => {
    renderSection();
    await flush();
    expect(ids()).toEqual(['video:b', 'document:a', 'image:c', 'audio:d']); // server order
    fireEvent.click(screen.getByTestId('assets-sort-name'));
    expect(ids()).toEqual(['document:a', 'video:b', 'audio:d', 'image:c']); // natural: c2 before c10
    expect(screen.getByTestId('assets-sort-name')).toHaveAttribute('data-sort', 'asc');
    fireEvent.click(screen.getByTestId('assets-sort-name'));
    expect(ids()).toEqual(['image:c', 'audio:d', 'video:b', 'document:a']);
    fireEvent.click(screen.getByTestId('assets-sort-name'));
    expect(ids()).toEqual(['video:b', 'document:a', 'image:c', 'audio:d']);

    fireEvent.click(screen.getByTestId('assets-sort-size'));
    expect(ids()).toEqual(['audio:d', 'video:b', 'document:a', 'image:c']);
    fireEvent.click(screen.getByTestId('assets-sort-size'));
    expect(ids()).toEqual(['document:a', 'video:b', 'audio:d', 'image:c']); // unknown size still last

    fireEvent.click(screen.getByTestId('assets-sort-type'));
    expect(ids()).toEqual(['audio:d', 'document:a', 'image:c', 'video:b']);
    expect(screen.getByTestId('assets-sort-type')).toHaveAccessibleName('Type, sorted ascending. Sort descending');
  });

  it('remembers the sort and sorts inside every folder', async () => {
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [
      asset('video:z', { displayName: 'z.mp4', folder: 'Footage' }),
      asset('video:y', { displayName: 'y.mp4', folder: '' }),
      asset('video:a', { displayName: 'a.mp4', folder: 'Footage' }),
      asset('video:b', { displayName: 'b.mp4', folder: '' }),
    ] } });
    const first = renderSection();
    await flush();
    fireEvent.click(screen.getByTestId('assets-sort-name'));
    expect(ids()).toEqual(['video:b', 'video:y', 'video:a', 'video:z']); // top level, then Footage -- each sorted
    first.unmount();
    renderSection();
    await flush();
    expect(ids()).toEqual(['video:b', 'video:y', 'video:a', 'video:z']);
  });
});

describe('organize: failures are said, never silent', () => {
  it('a move the server refuses for some files names how many could not move', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [asset('video:a'), asset('video:b')] } });
    mocks.moveAssets.mockResolvedValue({ ok: true, value: { folder: 'X', moved: ['video:a'],
      failed: [{ assetId: 'video:b', code: 'asset_not_found' }] } });
    renderSection();
    await flush();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select video:a.mp4' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select video:b.mp4' }));
    fireEvent.click(screen.getByTestId('assets-move'));
    fireEvent.change(screen.getByLabelText('Find or create a folder'), { target: { value: 'X' } });
    fireEvent.mouseDown(screen.getByTestId('folder-picker-create'));
    await flush();
    expect(screen.getByRole('alert')).toHaveTextContent("Couldn't move 1 file: removed, or no longer in this project.");
    expect(screen.getByTestId('assets-move-status')).toHaveTextContent('Moved video:a.mp4 to X');
  });
});
