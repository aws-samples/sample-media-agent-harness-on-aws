// @vitest-environment jsdom
// Design A (new-ui-design/right-panel-tabs.html): one tab per subject. A clip's player and its shot timeline were
// two tabs on one video (IND403); now they are one tab with two versions, and × closes the subject.
import '@testing-library/jest-dom';
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';

vi.mock('../lab-api', () => ({ getConfig: async () => ({ artifactsDomain: '' }) }));
vi.mock('./renderer/RendererRegistry', async (orig) => {
  const actual = await orig<typeof import('./renderer/RendererRegistry')>();
  // Every view renders as a button that fires a Trim, so a test can act from inside a tab.
  return { ...actual, lookupRenderer: () => (p: { onInteraction?: (e: unknown) => void }) => (
    <button type="button" onClick={() => p.onInteraction?.({ type: 'action', target: 'trim-batch', data: { verb: 'trim' }, timestamp: 1 })}>act</button>) };
});

import SessionRendererPanel from './SessionRendererPanel';
import type { RenderHistoryEntry } from './renderer/types';

const CLIP = 's3://ingest/p1/clip.mp4';
const view = (key: string, label: string, ts: number, manifest: Record<string, unknown>): RenderHistoryEntry => ({
  turnIndex: 0, timestamp: ts, label, key, clientOnly: true,
  blocks: [{ template: 'manifest-router', title: label, data: { manifest } }],
} as unknown as RenderHistoryEntry);
const served = (key: string, label: string, ts: number, subjectIds: string[]): RenderHistoryEntry => ({
  turnIndex: 0, timestamp: ts, label, key, subjectIds,
  blocks: [{ template: 'manifest-router', title: label, data: { manifestUrl: `/api/artifact?key=${encodeURIComponent(key)}` } }],
} as unknown as RenderHistoryEntry);

const video = view('artifacts/s/render/view-0.json', 'demo.mp4', 1, { template: 'asset-player', ref: { asset_id: 'video:0' } });
const clipPlayer = view('artifacts/s/render/view-1.json', 'clip.mp4', 2, { template: 'asset-player', ref: { asset_id: 'video:1' }, sourceLocation: CLIP });
const shots = served('composite:loc:clip', 'Shot Segmentation', 3, [`loc:${CLIP}`]);
const history = [video, clipPlayer, shots];

function panel(currentIndex: number, extra: Partial<React.ComponentProps<typeof SessionRendererPanel>> = {}) {
  const onNavigate = vi.fn();
  const onRemoveTab = vi.fn();
  render(<SessionRendererPanel history={history} currentIndex={currentIndex} onNavigate={onNavigate} onClose={vi.fn()} onRemoveTab={onRemoveTab} {...extra} />);
  return { onNavigate, onRemoveTab };
}

describe('one tab per subject, its views as versions', () => {
  it('the clip player and the clip timeline are ONE tab, named after the clip, with a version count', () => {
    panel(2);
    const tabs = screen.getAllByRole('tab');
    expect(tabs.map((t) => t.getAttribute('aria-label'))).toEqual(['demo.mp4', 'clip.mp4']);
    expect(tabs[1]).toHaveAttribute('aria-selected', 'true');
    expect(within(tabs[1]).getByTestId('tab-version-count')).toHaveTextContent('2');
    expect(screen.getByText('Tab · 2 of 2')).toBeInTheDocument();
  });
  it('the switcher shows the version on screen and steps between them', () => {
    const { onNavigate } = panel(2);
    const sw = screen.getByTestId('version-switcher');
    expect(within(sw).getByRole('combobox', { name: 'Version' })).toHaveValue('2');
    expect(within(sw).getByText('of 2')).toBeInTheDocument();
    expect(within(sw).getByRole('button', { name: 'Next version' })).toBeDisabled();
    fireEvent.click(within(sw).getByRole('button', { name: 'Previous version' }));
    expect(onNavigate).toHaveBeenCalledWith(1);
  });
  it('the switcher and the refresh button share one toolbar row', () => {
    panel(2);
    const bar = screen.getByTestId('tab-toolbar');
    expect(within(bar).getByTestId('version-switcher')).toBeInTheDocument();
    expect(within(bar).getByTestId('refresh-render')).toBeInTheDocument();
  });
  it('a tab with one view has no switcher and no count', () => {
    panel(0);
    expect(screen.queryByTestId('version-switcher')).toBeNull();
    expect(within(screen.getAllByRole('tab')[0]).queryByTestId('tab-version-count')).toBeNull();
  });
  it('clicking a tab opens its newest version', () => {
    const { onNavigate } = panel(0);
    fireEvent.click(screen.getByRole('tab', { name: 'clip.mp4' }));
    expect(onNavigate).toHaveBeenCalledWith(2);
  });
  it('× closes the subject: every version of it, and nothing else', () => {
    const { onRemoveTab } = panel(2);
    fireEvent.click(screen.getByRole('button', { name: 'Remove clip.mp4 tab' }));
    expect(onRemoveTab.mock.calls.map((c) => c[0])).toEqual(['artifacts/s/render/view-1.json', 'composite:loc:clip']);
  });
});

describe("a gallery's next version lands in the gallery's tab", () => {
  it('an untitled gallery and the version that names it (view:<its key>) are one tab', () => {
    const gallery = served('artifacts/s/render/g1.json', 'Olive chunks', 1, []);
    const v2 = view('artifacts/s/render/view-9.json', 'Olive chunks · trimmed', 2, { template: 'clip-gallery', subjectIds: ['view:artifacts/s/render/g1.json'] });
    render(<SessionRendererPanel history={[gallery, v2]} currentIndex={1} onNavigate={vi.fn()} onClose={vi.fn()} />);
    expect(screen.getAllByRole('tab')).toHaveLength(1);
    expect(screen.getByTestId('version-switcher')).toHaveTextContent('of 2');
  });
  it("a Moments search's version joins the Moments tab and is named by its query in the switcher", () => {
    const agent = served('artifacts/s/render/m0.json', 'Moments', 1, ['title:Moments']);
    const searched = { ...view('artifacts/s/render/view-m1.json', 'Moments', 2,
      { template: 'clip-gallery', title: 'Moments', subjectIds: ['title:Moments'] }), note: '“rain on windshield”' };
    render(<SessionRendererPanel history={[agent, searched]} currentIndex={1} onNavigate={vi.fn()} onClose={vi.fn()} />);
    expect(screen.getAllByRole('tab').map((t) => t.getAttribute('aria-label'))).toEqual(['Moments']);
    const options = within(screen.getByTestId('version-switcher')).getAllByRole('option').map((o) => o.textContent);
    expect(options[0]).toMatch(/^v1 · Moments/);
    expect(options[1]).toMatch(/^v2 · “rain on windshield”/);
  });
  it('an action from inside a tab carries that tab as its origin (what it is about, its name)', () => {
    const onInteraction = vi.fn();
    const gallery = served('artifacts/s/render/g2.json', 'Voices', 1, ['title:Voices']);
    render(<SessionRendererPanel history={[gallery]} currentIndex={0} onNavigate={vi.fn()} onClose={vi.fn()} onInteraction={onInteraction} />);
    fireEvent.click(screen.getAllByRole('button', { name: 'act' })[0]);
    expect(onInteraction.mock.calls[0][0].data.origin).toEqual({ subjectIds: ['title:Voices', 'view:artifacts/s/render/g2.json'], label: 'Voices' });
  });
});
