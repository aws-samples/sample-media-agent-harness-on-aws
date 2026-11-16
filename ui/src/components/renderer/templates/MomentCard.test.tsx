// @vitest-environment jsdom
//
// MomentCard (Component 7, media-harness-search-qa-rendering; Req 3.10, 3.11, 3.15, 3.22, 3.24, 3.28) on the app's
// ONE card: the range and its length on the picture, one line of matched text, and Preview · Cut to a new tab ·
// Open in side panel · ⋯ (Find similar, Save to folder…, Download source, the match as a note). No inline player:
// the side panel opener is injected via `openAsset`, so no network path runs.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import '@testing-library/jest-dom';
import { act, cleanup, render, screen, fireEvent, within } from '@testing-library/react';
import React from 'react';

import MomentCard, { CLIP_SENT_MS, type MomentCardProps } from './MomentCard';
import { theme } from '../../../styles/tokens';
import type { MomentItem } from '../types';
import type { CardUrl } from '../../../projects/useMomentUrls';

function moment(over: Partial<MomentItem> = {}): MomentItem {
  return {
    id: 'm1',
    ref: { project_id: 'p1', asset_id: 'a1', frame_s3_key: 'projects/f.jpg' },
    kind: 'video',
    display_name: 'nba.mp4',
    start_ms: 74000,
    end_ms: 96000,
    source_index: 'scene',
    final_score: 0.510417,
    text: 'the <b>chase</b> scene',
    text_truncated: false,
    highlight_terms: ['chase'],
    citation: null,
    ...over,
  };
}

const ready: CardUrl = { phase: 'ready', url: 'https://cdn/u1', obtainedAt: 0, poster: 'https://cdn/r1.jpg' };

function renderCard(over: Partial<MomentCardProps> = {}) {
  const props: MomentCardProps = {
    moment: moment(),
    theme,
    sessionId: 'sess-1',
    cardUrl: ready,
    onLoadStart: vi.fn(),
    onLoadOk: vi.fn(),
    onLoadError: vi.fn(),
    onPreview: vi.fn(),
    onFindSimilar: vi.fn(),
    openAsset: vi.fn(async () => ({ ok: true as const })),
    ...over,
  };
  return { props, ...render(<ul><MomentCard {...props} /></ul>) };
}

const more = (name = 'nba.mp4') => fireEvent.click(screen.getByRole('button', { name: `More for ${name}` }));
const items = () => screen.queryAllByRole('menuitem').map((b) => b.textContent);

beforeEach(() => vi.clearAllMocks());
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe('MomentCard', () => {
  it('is the one card: the range and its length on the picture, the name, one line of matched text', () => {
    renderCard();
    const card = screen.getByTestId('moment-card-m1');
    expect(card).toHaveAttribute('data-moment-card', '');
    expect(within(card).getByTestId('card-title')).toHaveTextContent('nba.mp4');
    expect(within(card).getByTestId('card-badge-left')).toHaveTextContent('01:14–01:36');
    expect(within(card).getByTestId('card-badge')).toHaveTextContent('22s');
    // The source's frame at the moment's start is the picture; nothing plays on the card.
    // The picture is the video's hero still; no card mounts a player (Firefox stalled on 24 at once).
    expect(card.querySelector('video')).toBeNull();
    expect(card.querySelector('img')!.getAttribute('src')).toBe('https://cdn/r1.jpg');
    // Literal markup (Req 3.10): the matched text is text, the hit a <mark>.
    const text = screen.getByTestId('moment-text-m1');
    expect(text.textContent).toBe('the <b>chase</b> scene');
    expect(text.querySelector('b')).toBeNull();
    expect(text.querySelector('mark.mh-moment-hit')).toHaveTextContent('chase');
    // The full detail lives in the title's tooltip.
    expect(within(card).getByTestId('card-title')).toHaveAttribute('title', expect.stringContaining('rank score 0.51'));
  });

  it('icons are Preview · Cut to a new tab · Open in side panel · ⋯, in that order', () => {
    renderCard({ onClip: vi.fn() });
    const labels = [...screen.getByTestId('moment-card-m1').querySelectorAll('[data-card-acts] > button, [data-card-acts] > span > button')]
      .map((b) => b.getAttribute('aria-label'));
    expect(labels).toEqual(['Preview nba.mp4', 'Cut nba.mp4 01:14–01:36 to a new tab', 'Open nba.mp4 in the side panel', 'More for nba.mp4']);
  });

  it('Preview (the icon or the picture) hands the moment to the gallery', () => {
    const { props } = renderCard();
    fireEvent.click(screen.getByRole('button', { name: 'Preview nba.mp4' }));
    fireEvent.click(screen.getByTestId('moment-card-m1').querySelector('[data-card-media]')!);
    expect(props.onPreview).toHaveBeenCalledTimes(2);
    expect(props.onPreview).toHaveBeenCalledWith(props.moment);
  });

  it('the side panel opens the WHOLE asset with the moment span selected; an image sends no range', async () => {
    const { props } = renderCard();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Open nba.mp4 in the side panel' })); });
    expect(props.openAsset).toHaveBeenCalledWith(
      expect.objectContaining({ ref: props.moment.ref, kind: 'video', startMs: 74000, range: { start: 74, end: 96 } }), 'sess-1');
    cleanup();
    const img = renderCard({ moment: moment({ kind: 'image', id: 'i1', display_name: 'still.png' }) });
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Open still.png in the side panel' })); });
    expect((img.props.openAsset as ReturnType<typeof vi.fn>).mock.calls[0][0]).not.toHaveProperty('range');
  });

  it('a side-panel refusal takes the description line (Req 2.9)', async () => {
    renderCard({ openAsset: vi.fn(async () => ({ ok: false as const, message: 'This file is still processing' })) });
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Open nba.mp4 in the side panel' })); });
    expect(screen.getByTestId('moment-refusal-m1')).toHaveTextContent('This file is still processing');
    expect(screen.queryByTestId('moment-text-m1')).toBeNull();
  });

  it('Cut cuts the span as found, then shows a brief sent state; it waits for the media URL', () => {
    vi.useFakeTimers();
    const onClip = vi.fn();
    renderCard({ onClip });
    fireEvent.click(screen.getByRole('button', { name: 'Cut nba.mp4 01:14–01:36 to a new tab' }));
    expect(onClip).toHaveBeenCalledWith(expect.objectContaining({ id: 'm1' }), { in: 74, out: 96 });
    expect(screen.getByRole('button', { name: 'Cutting nba.mp4' })).toHaveAttribute('aria-disabled', 'true');
    act(() => { vi.advanceTimersByTime(CLIP_SENT_MS); });
    expect(screen.getByRole('button', { name: 'Cut nba.mp4 01:14–01:36 to a new tab' })).not.toHaveAttribute('aria-disabled');
    cleanup();
    const onClip2 = vi.fn();
    renderCard({ onClip: onClip2, cardUrl: { phase: 'minting' } as CardUrl });
    const btn = screen.getByRole('button', { name: 'Cut nba.mp4 01:14–01:36 to a new tab' });
    expect(btn).toHaveAttribute('aria-disabled', 'true');
    fireEvent.click(btn);
    expect(onClip2).not.toHaveBeenCalled();
  });

  it('an image has no range or Cut: Download is its action', () => {
    renderCard({ onClip: vi.fn(), moment: moment({ kind: 'image', display_name: 'still.png' }) });
    expect(screen.queryByTestId('card-badge-left')).toBeNull();
    expect(screen.queryByRole('button', { name: /^Cut/ })).toBeNull();
    expect(screen.getByRole('button', { name: 'Download still.png' })).toBeInTheDocument();
    expect(screen.getByTestId('moment-card-m1').querySelector('img')).toHaveAttribute('src', 'https://cdn/u1');
  });

  it('⋯ holds Find similar, Save to folder… and Download source, with the match type and score as a note', () => {
    const onSaveToFolder = vi.fn();
    const { props } = renderCard({ onSaveToFolder });
    more();
    expect(items()).toEqual(['Find similar', 'Save to folder…', 'Download source']);
    expect(screen.getByRole('menu')).toHaveTextContent('scene · match 0.51');
    fireEvent.click(screen.getByRole('menuitem', { name: 'Find similar' }));
    expect(props.onFindSimilar).toHaveBeenCalledWith(props.moment);
    more();
    fireEvent.click(screen.getByRole('menuitem', { name: 'Save to folder…' }));
    expect(onSaveToFolder).toHaveBeenCalledWith(props.moment);
  });

  it('Find similar: absent for audio and a frameless video; disabled with a reason mid-turn or in another project (Req 3.24)', () => {
    renderCard({ moment: moment({ kind: 'audio' }) });
    more();
    expect(items()).not.toContain('Find similar');
    cleanup();
    renderCard({ moment: moment({ ref: { project_id: 'p1', asset_id: 'a1', frame_s3_key: null } }) });
    more();
    expect(items()).not.toContain('Find similar');
    cleanup();
    const busy = renderCard({ turnInProgress: true });
    more();
    const item = screen.getByRole('menuitem', { name: 'Find similar' });
    expect(item).toBeDisabled();
    expect(item).toHaveAttribute('title', 'Available when the current reply finishes');
    fireEvent.click(item);
    expect(busy.props.onFindSimilar).not.toHaveBeenCalled();
    cleanup();
    renderCard({ activeProjectId: 'other' });
    more();
    expect(screen.getByRole('menuitem', { name: 'Find similar' })).toHaveAttribute('title', 'Switch the active project to search from this moment');
  });

  it('no_access: the name and span only — no text, score, Cut, side panel or Find similar (Req 3.28)', () => {
    renderCard({ onClip: vi.fn(), cardUrl: { phase: 'no_access' } as CardUrl });
    expect(screen.getByTestId('moment-no-access-m1')).toHaveTextContent('You no longer have access to this project');
    expect(screen.getByTestId('card-badge-left')).toHaveTextContent('01:14–01:36');
    expect(screen.queryByTestId('moment-text-m1')).toBeNull();
    expect(screen.queryByRole('button', { name: /^Cut/ })).toBeNull();
    expect(screen.queryByRole('button', { name: /side panel/ })).toBeNull();
    expect(screen.queryByRole('button', { name: 'More for nba.mp4' })).toBeNull();
    expect(screen.getByTestId('card-title').getAttribute('title')).not.toMatch(/rank score/);
  });

  it('load_failed keeps the matched text and the score (Req 3.15); minting says Loading', () => {
    renderCard({ cardUrl: { phase: 'load_failed' } as CardUrl });
    expect(screen.getByTestId('moment-load-failed-m1')).toHaveTextContent('This moment could not be loaded');
    expect(screen.getByTestId('moment-text-m1')).toBeInTheDocument();
    more();
    expect(screen.getByRole('menu')).toHaveTextContent('match 0.51');
    cleanup();
    renderCard({ cardUrl: { phase: 'minting' } as CardUrl });
    expect(screen.getByTestId('moment-minting-m1')).toHaveTextContent('Loading');
  });

  it('a shot has no description line', () => {
    renderCard({ moment: moment({ source_index: 'shot', text: '' }) });
    expect(screen.queryByTestId('moment-text-m1')).toBeNull();
  });

  it('routes the picture load into the URL hook: metadata → ok, error → refresh at the start', () => {
    const { props } = renderCard();
    expect(props.onLoadStart).toHaveBeenCalledWith({ time: 74, playing: false });
    const img = screen.getByTestId('moment-card-m1').querySelector('img')!;
    fireEvent.load(img);
    expect(props.onLoadOk).toHaveBeenCalled();
    fireEvent.error(img);
    expect(props.onLoadError).toHaveBeenCalledWith({ time: 74, playing: false });
  });

  it('a checkbox selects it; the playing card is ringed', () => {
    const onSelectedChange = vi.fn();
    renderCard({ onSelectedChange, playing: true });
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select nba.mp4' }));
    expect(onSelectedChange).toHaveBeenCalledWith(true, false);
    expect(screen.getByTestId('moment-card-m1')).toHaveAttribute('data-playing', 'true');
  });
});
