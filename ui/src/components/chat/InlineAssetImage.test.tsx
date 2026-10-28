/** @vitest-environment jsdom */
import '@testing-library/jest-dom';
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import ReactMarkdown, { defaultUrlTransform } from 'react-markdown';

const mocks = vi.hoisted(() => ({ request: vi.fn(), openAssetAt: vi.fn(), resolveLocations: vi.fn() }));
vi.mock('../../projects/assetUrlBatcher', () => ({ request: mocks.request }));
vi.mock('../../projects/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../projects/api')>()),
  resolveLocations: mocks.resolveLocations,
}));
vi.mock('../../projects/openCitation', () => ({ openAssetAt: mocks.openAssetAt }));

import { markdownComponents } from './markdownComponents';
import { ChatAssetContext, keepAssetUrls, isAssetId, referenceLocations, RESOLVE_RETRY_MS } from './InlineAssetImage';

const transform = (u: string) => keepAssetUrls(u, defaultUrlTransform);
function answer(md: string, projectId: string | null = 'p1') {
  return render(
    <ChatAssetContext.Provider value={{ sessionId: 's1', projectId }}>
      <ReactMarkdown components={markdownComponents} urlTransform={transform}>{md}</ReactMarkdown>
    </ChatAssetContext.Provider>,
  );
}

describe('inline project images in a chat answer', () => {
  beforeEach(() => {
    mocks.request.mockReset();
    mocks.openAssetAt.mockReset();
    mocks.resolveLocations.mockReset();
    mocks.request.mockResolvedValue(new Map([['image:1', { ok: true, entry: { source: { url: 'https://signed/src' }, rendition: { url: 'https://signed/r' } } }]]));
    mocks.openAssetAt.mockResolvedValue({ ok: true });
  });

  it('`![caption](asset:id)` shows the image inline, from an authorised URL of the active project', async () => {
    answer('Here: ![Frames to shots](asset:image:1)');
    await waitFor(() => expect(screen.getByRole('img', { name: 'Frames to shots' })).toHaveAttribute('src', 'https://signed/r'));
    expect(mocks.request).toHaveBeenCalledWith('p1', ['image:1']);
  });

  it('Preview shows it large; Open in side panel opens it as a Renderer tab', async () => {
    answer('![Frames to shots](asset:image:1)');
    fireEvent.click(await screen.findByRole('button', { name: 'Preview Frames to shots' }));
    expect(screen.getByRole('dialog', { name: /Preview of Frames to shots/ })).toBeInTheDocument();
    expect(mocks.openAssetAt).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: /Open in side panel/ }));
    await waitFor(() => expect(mocks.openAssetAt).toHaveBeenCalledWith(
      expect.objectContaining({ ref: { project_id: 'p1', asset_id: 'image:1', frame_s3_key: null }, kind: 'image', displayName: 'Frames to shots' }),
      's1',
    ));
  });

  it('says so, without a broken image, when the chat has no project or the id is not visible', async () => {
    answer('![Diagram](asset:image:1)', null);
    expect(await screen.findByTestId('inline-asset-missing')).toHaveTextContent('Diagram — not available');
    mocks.request.mockResolvedValue(new Map([['image:2', { ok: false, kind: 'not_found' }]]));
    answer('![Other](asset:image:2)');
    await waitFor(() => expect(screen.getAllByTestId('inline-asset-missing')).toHaveLength(2));
  });

  it('a file NAME the agent wrote (no id yet: made this turn) resolves in the chat output folder, never 404s', async () => {
    // staging session a4a205a9: `![…](asset:recheck_sample_proxy_frame_2s.png)` asked the asset-URL route for the
    // file name and showed "not available in this project".
    const pid = 'p' + 'a'.repeat(32);
    const key = `projects/${pid}/sessions/s1-session/output/frame_2s.png`;
    mocks.resolveLocations.mockResolvedValue({ ok: true, value: { [key]: 'image:1' } });
    render(
      <ChatAssetContext.Provider value={{ sessionId: 's1-session', projectId: pid }}>
        <ReactMarkdown components={markdownComponents} urlTransform={transform}>{'![Frame](asset:frame_2s.png)'}</ReactMarkdown>
      </ChatAssetContext.Provider>,
    );
    await waitFor(() => expect(screen.getByRole('img', { name: 'Frame' })).toHaveAttribute('src', 'https://signed/r'));
    expect(mocks.resolveLocations).toHaveBeenCalledWith(pid, [key, 'artifacts/s1-session/output/frame_2s.png']);
    expect(mocks.request).toHaveBeenCalledWith(pid, ['image:1']);
    expect(mocks.request).not.toHaveBeenCalledWith(pid, ['frame_2s.png']);
  });

  it('asks again while the just-made file is still being registered', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      mocks.resolveLocations
        .mockResolvedValueOnce({ ok: true, value: {} })
        .mockResolvedValue({ ok: true, value: { 's3://b/projects/x/frame.png': 'image:1' } });
      answer('![Frame](asset:s3://b/projects/x/frame.png)');
      await vi.advanceTimersByTimeAsync(RESOLVE_RETRY_MS[0] + 10);
      await waitFor(() => expect(screen.getByRole('img', { name: 'Frame' })).toHaveAttribute('src', 'https://signed/r'));
      expect(mocks.resolveLocations).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it('an id is used as is; a path-like name is never guessed at', () => {
    expect(isAssetId('image:1')).toBe(true);
    expect(isAssetId('image:6f1c0d2e-0000-4000-8000-000000000000')).toBe(true);
    expect(isAssetId('frame_2s.png')).toBe(false);
    expect(isAssetId('s3://b/k.png')).toBe(false);
    expect(referenceLocations('../x.png', 's1-session', 'p1')).toEqual([]);
    expect(referenceLocations('a/b.png', 's1-session', 'p1')).toEqual([]);
    expect(referenceLocations('https://cdn/projects/p/x.png', 's1-session', 'p1')).toEqual(['https://cdn/projects/p/x.png']);
  });

  it('other URLs are still sanitised by react-markdown', () => {
    answer('[x](javascript:alert(1))');
    expect(screen.getByText('x').closest('a')?.getAttribute('href') ?? '').not.toMatch(/^javascript:/);
  });
});
