// Unit tests for the asset-URL request batcher (Component 7, "URLs at view time"; Req 3.26, 3.27).
//
// The batcher coalesces concurrent `getAssetUrls` calls for one Project into a single request per
// microtask (chunked to 100 ids), and distils the batched response into a per-id outcome every caller
// receives only its own ids from. `getAssetUrls` is mocked so no network / lab-api is touched.
import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('./api', () => ({ getAssetUrls: vi.fn() }));

import { getAssetUrls, type AssetUrlEntry, type AssetUrlsResponse, type ProjectsResult } from './api';
import { request, requestFrames, MAX_IDS_PER_CALL } from './assetUrlBatcher';

const mockGet = getAssetUrls as unknown as ReturnType<typeof vi.fn>;

const entry = (url: string, rendition?: string): AssetUrlEntry => ({
  source: { url, expiresAt: 0 },
  ...(rendition ? { rendition: { url: rendition, expiresAt: 0 } } : {}),
});

/** An `ok` route reply mapping each id to its own `entry("url:<id>")`. */
function okFor(ids: string[]): ProjectsResult<AssetUrlsResponse> {
  const urls: Record<string, AssetUrlEntry> = {};
  for (const id of ids) urls[id] = entry(`url:${id}`);
  return { ok: true, value: { urls, errors: {} } };
}

beforeEach(() => {
  mockGet.mockReset();
});

// A moment's own picture rides the cards' URL request: one gallery page is still one call (IND403 per-moment frames).
describe('assetUrlBatcher.requestFrames', () => {
  it('coalesces frame asks into the same call as the asset ids, and answers each ask', async () => {
    mockGet.mockImplementation(async (_pid: string, ids: string[]) => ({
      ok: true, value: { urls: Object.fromEntries(ids.map((id) => [id, entry(`url:${id}`)])), errors: {},
        frames: { 'video:a@1200': { url: 'https://s3/f.jpg', expiresAt: 1 } } },
    }));
    const [urls, frames] = await Promise.all([
      request('p1', ['video:a']),
      requestFrames('p1', [{ asset_id: 'video:a', at_ms: 1200 }, { asset_id: 'video:a', at_ms: 9000 }]),
    ]);
    expect(mockGet).toHaveBeenCalledTimes(1);
    expect(mockGet).toHaveBeenCalledWith('p1', ['video:a'], [{ asset_id: 'video:a', at_ms: 1200 }, { asset_id: 'video:a', at_ms: 9000 }]);
    expect(urls.get('video:a')?.ok).toBe(true);
    expect([...frames]).toEqual([['video:a@1200', 'https://s3/f.jpg']]);
  });
});

describe('assetUrlBatcher.request', () => {
  it('coalesces concurrent calls for one Project into a single request; each waiter gets only its ids', async () => {
    mockGet.mockImplementation(async (_pid: string, ids: string[]) => okFor(ids));

    const p1 = request('p1', ['a']);
    const p2 = request('p1', ['b']);
    const [r1, r2] = await Promise.all([p1, p2]);

    expect(mockGet).toHaveBeenCalledTimes(1);
    const [, ids] = mockGet.mock.calls[0];
    expect([...ids].sort()).toEqual(['a', 'b']);

    // A waiter sees only the ids it asked for, never a sibling's.
    expect(r1.get('a')).toEqual({ ok: true, entry: entry('url:a') });
    expect(r1.has('b')).toBe(false);
    expect(r2.get('b')).toEqual({ ok: true, entry: entry('url:b') });
    expect(r2.has('a')).toBe(false);
  });

  it('dedupes repeated ids within a batch and chunks to at most 100 ids per call', async () => {
    mockGet.mockImplementation(async (_pid: string, ids: string[]) => okFor(ids));

    await request('p1', ['a', 'a', 'a']);
    expect(mockGet).toHaveBeenCalledWith('p1', ['a'], []);

    mockGet.mockClear();
    const many = Array.from({ length: MAX_IDS_PER_CALL + 50 }, (_, i) => `id${i}`);
    const r = await request('p1', many);
    expect(mockGet).toHaveBeenCalledTimes(2); // 100 + 50
    for (const call of mockGet.mock.calls) expect(call[1].length).toBeLessThanOrEqual(MAX_IDS_PER_CALL);
    expect(r.size).toBe(many.length);
  });

  it('makes no request for an empty id list and drops blank ids', async () => {
    const empty = await request('p1', []);
    expect(empty.size).toBe(0);
    expect(mockGet).not.toHaveBeenCalled();

    mockGet.mockImplementation(async (_pid: string, ids: string[]) => okFor(ids));
    const r = await request('p1', ['', 'a']);
    expect(mockGet).toHaveBeenCalledWith('p1', ['a'], []);
    expect(r.has('')).toBe(false);
    expect(r.get('a')).toEqual({ ok: true, entry: entry('url:a') });
  });

  it('maps a per-id project_not_found to not_found and any other per-id code to error', async () => {
    mockGet.mockResolvedValue({
      ok: true,
      value: {
        urls: {},
        errors: {
          a: { code: 'project_not_found', message: 'gone' },
          b: { code: 'weird', message: 'boom' },
        },
      },
    } satisfies ProjectsResult<AssetUrlsResponse>);

    const r = await request('p1', ['a', 'b']);
    expect(r.get('a')).toEqual({ ok: false, kind: 'not_found' });
    expect(r.get('b')).toEqual({ ok: false, kind: 'error', code: 'weird', message: 'boom' });
  });

  it('maps an id present in neither map to an url_missing error', async () => {
    mockGet.mockResolvedValue({ ok: true, value: { urls: {}, errors: {} } });
    const r = await request('p1', ['a']);
    expect(r.get('a')).toMatchObject({ ok: false, kind: 'error', code: 'url_missing' });
  });

  it('maps whole-route not_found and access_lost to not_found for every id', async () => {
    mockGet.mockResolvedValue({ ok: false, kind: 'not_found' });
    expect((await request('p1', ['a'])).get('a')).toEqual({ ok: false, kind: 'not_found' });

    mockGet.mockResolvedValue({ ok: false, kind: 'access_lost', personalProject: { id: 'p0', name: 'Personal' } });
    expect((await request('p1', ['a'])).get('a')).toEqual({ ok: false, kind: 'not_found' });
  });

  it('maps a disabled route to a projects_disabled error and a route error through verbatim', async () => {
    mockGet.mockResolvedValue({ ok: false, kind: 'disabled' });
    expect((await request('p1', ['a'])).get('a')).toEqual({
      ok: false,
      kind: 'error',
      code: 'projects_disabled',
      message: 'Projects are not enabled.',
    });

    mockGet.mockResolvedValue({ ok: false, kind: 'error', status: 500, code: 'boom', message: 'bad' });
    expect((await request('p1', ['a'])).get('a')).toEqual({ ok: false, kind: 'error', code: 'boom', message: 'bad' });
  });

  it('treats a thrown getAssetUrls as a per-chunk batch_error instead of rejecting', async () => {
    mockGet.mockRejectedValue(new Error('kaboom'));
    const r = await request('p1', ['a']);
    expect(r.get('a')).toMatchObject({ ok: false, kind: 'error', code: 'batch_error', message: 'kaboom' });
  });

  it('keeps batches of different Projects independent (one request each)', async () => {
    mockGet.mockImplementation(async (_pid: string, ids: string[]) => okFor(ids));
    const [rp, rq] = await Promise.all([request('pA', ['a']), request('pB', ['b'])]);
    expect(mockGet).toHaveBeenCalledTimes(2);
    expect(rp.get('a')).toEqual({ ok: true, entry: entry('url:a') });
    expect(rq.get('b')).toEqual({ ok: true, entry: entry('url:b') });
  });
});

describe('assetUrlBatcher feeds the folder store', () => {
  it("records each answered id's folder", async () => {
    const { peekAssetFolder, _resetAssetFolders } = await import('./assetFolders');
    _resetAssetFolders();
    mockGet.mockResolvedValue({ ok: true, value: { urls: { a: { ...entry('u'), folder: 'Boards/Board 07' }, b: { ...entry('v'), folder: '' } }, errors: {} } });
    await request('pf', ['a', 'b']);
    expect(peekAssetFolder('pf', 'a')).toBe('Boards/Board 07');
    expect(peekAssetFolder('pf', 'b')).toBe('');
  });
});
