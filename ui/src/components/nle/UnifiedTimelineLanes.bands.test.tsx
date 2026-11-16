// @vitest-environment jsdom
/**
 * IND403 Lab 1 (staging 2026-11-15): scenes drew as ONE solid bar on a speaker-style lane and filled the transcript
 * list. They are a band like Shots: its own row, numbered blocks, the scene's facts on hover; shots keep their row.
 */
import '@testing-library/jest-dom';
import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import UnifiedTimelineLanes from './UnifiedTimelineLanes';

const shot = (n: number, t: number) => ({ id: `m-${n}`, time: t, endTime: t + 10, label: String(n), category: 'content' });
const scene = (n: number, t: number, e: number, setting?: string) => ({
  id: `scenes-${n}`, time: t, endTime: e, label: String(n), laneSet: 'scenes', note: 'location_change',
  ...(setting ? { category: setting } : {}),
});
const props = (onSeek = vi.fn()) => ({
  audioTracks: [], peaks: {}, duration: 300, currentTime: 0, onSeek, trackLabel: () => 'track', mediaRef: { current: null },
  segments: [], markers: [shot(0, 0), shot(1, 10), scene(1, 0, 120, 'beach_coast'), scene(2, 120, 300)] as never,
});

describe('UnifiedTimelineLanes — marker bands', () => {
  it('draws Shots and Scenes as two band rows, each with its own blocks', () => {
    render(<UnifiedTimelineLanes {...props()} />);
    expect(within(screen.getByTestId('lane-shots')).getByText('Shots · 2')).toBeInTheDocument();
    const scenes = screen.getByTestId('lane-band-scenes');
    expect(within(scenes).getByText('Scenes · 2')).toBeInTheDocument();
    expect(within(scenes).getAllByRole('button')).toHaveLength(2);
    expect(within(screen.getByTestId('lane-shots')).getAllByRole('button')).toHaveLength(2);
  });

  it("names a scene's facts on hover and seeks to it on click", () => {
    const onSeek = vi.fn();
    render(<UnifiedTimelineLanes {...props(onSeek)} />);
    const block = screen.getByRole('button', { name: 'Scene 1 · beach coast · location change · 0:00–2:00' });
    fireEvent.click(block);
    expect(onSeek).toHaveBeenCalledWith(0);
  });
});
