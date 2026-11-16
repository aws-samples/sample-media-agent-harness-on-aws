/** @vitest-environment jsdom */
import '@testing-library/jest-dom';
import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

const request = vi.fn(async (_pid: string, ids: string[]) => new Map(ids.map((id) => [id, {
  ok: true, entry: { source: { url: 'https://s3/clip.mp4' }, rendition: { url: 'https://s3/rendition.jpg' } },
}])));
vi.mock('../../projects/assetUrlBatcher', () => ({ request: (pid: string, ids: string[]) => request(pid, ids) }));

import { ChatAssetCard } from './ChatAssetCard';

describe('ChatAssetCard — a video is pictured by its hero still, never a player', () => {
  it('shows the project file\'s rendition and mounts no <video>', async () => {
    render(<ChatAssetCard kind="video" name="clip.mp4" src="https://s3/clip.mp4"
      moveTarget={{ projectId: 'p1', assetId: 'video:a' }} onPreview={() => {}} />);
    const card = screen.getByTestId('chat-asset-card');
    await waitFor(() => expect(card.querySelector('img')).toHaveAttribute('src', 'https://s3/rendition.jpg'));
    expect(card.querySelector('video')).toBeNull();
    expect(request).toHaveBeenCalledWith('p1', ['video:a']);
  });
});
