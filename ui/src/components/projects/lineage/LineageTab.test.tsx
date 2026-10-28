/** @vitest-environment jsdom */
import '@testing-library/jest-dom';
import React from 'react';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { LineageGraph, LineageNode } from '../../../projects/api';

const mocks = vi.hoisted(() => ({ getProjectLineage: vi.fn(), requestAssetUrls: vi.fn() }));
vi.mock('../../../projects/api', () => ({ getProjectLineage: mocks.getProjectLineage }));
vi.mock('../../../projects/assetUrlBatcher', () => ({ request: mocks.requestAssetUrls }));

import LineageTab from './LineageTab';

const PID = 'p' + '1e'.repeat(16);
const n = (id: string, role: string, stage: LineageNode['stage'], extra: Partial<LineageNode> = {}): LineageNode => ({
  id, kind: 'video', displayName: `${id}.mp4`, role, stage, status: 'indexed', parents: 0, children: 0, folder: 'Chats', ...extra,
});
// The staging watercolor chain: source -> frame (range) -> restyle (fal, settings) -> animate -> final (inferred),
// plus a removed draft and a path through another member's hidden file (contracted).
const GRAPH: LineageGraph = {
  nodes: [
    n('src', 'source', 'source', { displayName: 'Meridian.mp4' }),
    n('frame', 'deliverable', 'intermediate', { kind: 'image', displayName: 'frame_0007.jpg', tool: 'extract_frames' }),
    n('restyle', 'deliverable', 'intermediate', { kind: 'image', displayName: 'watercolor.jpg' }),
    n('clip', 'deliverable', 'intermediate', { displayName: 'animated.mp4' }),
    n('final', 'deliverable', 'final', { displayName: 'watercolor_final.mp4', tool: 'code_interpreter' }),
    n('draft', 'deliverable', 'final', { displayName: 'draft.mp4', status: 'removed', removed: true }),
  ],
  edges: [
    { from: 'src', to: 'frame', tool: 'extract_frames', via: 'turn', ranges: [{ start: 7.191, end: 7.191, unit: 's' }],
      settings: { op: 'frame' }, recorded: 'edge' },
    { from: 'frame', to: 'restyle', tool: 'usr-8ddf___run_model', via: 'plan_node', jobId: 'plan-1#restyle',
      settings: { endpoint_id: 'fal-ai/flux-pro/kontext', prompt: 'watercolor' }, recorded: 'edge' },
    { from: 'restyle', to: 'clip', tool: 'usr-8ddf___submit_job', via: 'plan_node', jobId: 'plan-1#animate', recorded: 'edge' },
    { from: 'clip', to: 'final', tool: 'code_interpreter', via: 'turn', inferred: true, recorded: 'edge' },
    { from: 'src', to: 'draft', contracted: true },
  ],
  gaps: [{ child: 'final', ref: 's3://b/artifacts/x/still.jpg', reason: 'untracked_location', tool: 'code_interpreter' }],
  stats: { nodes: 6, edges: 5, droppedEdges: 1, assets: 9, generated: 7, generatedWithoutLineage: 2 },
  truncated: false,
  direction: 'project',
};

beforeEach(() => {
  mocks.getProjectLineage.mockReset();
  mocks.getProjectLineage.mockResolvedValue({ ok: true, value: GRAPH });
  mocks.requestAssetUrls.mockReset();
  mocks.requestAssetUrls.mockImplementation(async (_pid: string, ids: string[]) => new Map(ids.map((id) => [id, {
    ok: true, entry: { source: { url: `https://signed.example/${id}/src` }, rendition: { url: `https://signed.example/${id}/thumb` } },
  }])));
});
afterEach(cleanup);

async function renderTab(props: Partial<React.ComponentProps<typeof LineageTab>> = {}) {
  const onNotFound = vi.fn();
  const utils = render(<LineageTab project={{ id: PID, name: 'v10' }} onNotFound={onNotFound} {...props} />);
  await screen.findByTestId('lineage-canvas');
  return { ...utils, onNotFound };
}

describe('LineageTab', () => {
  it('reads the whole project and draws every file and link, with the completeness line', async () => {
    await renderTab();
    expect(mocks.getProjectLineage).toHaveBeenCalledWith(PID, { direction: 'project', maxNodes: 2000 });
    for (const id of ['src', 'frame', 'restyle', 'clip', 'final', 'draft']) {
      expect(screen.getByTestId(`lineage-node-${id}`)).toBeInTheDocument();
    }
    expect(screen.getByTestId('lineage-status')).toHaveTextContent('6 files · 5 links (1 inferred)');
    expect(screen.getByTestId('lineage-unlinked')).toHaveTextContent('2 of 7 generated files have inputs not recorded');
    expect(screen.getByTestId('lineage-gaps-toggle')).toHaveTextContent('1 input could not be linked');
  });

  it('styles inferred and contracted edges distinctly, and marks removed files', async () => {
    await renderTab();
    const inferred = screen.getByTestId('lineage-edge-clip-final');
    expect(inferred).toHaveAttribute('data-inferred', 'true');
    expect(inferred.getAttribute('style')).toContain('stroke-dasharray: 2 4');
    const contracted = screen.getByTestId('lineage-edge-src-draft');
    expect(contracted).toHaveAttribute('data-contracted', 'true');
    expect(contracted.getAttribute('style')).toContain('stroke-dasharray: 6 4');
    expect(screen.getByTestId('lineage-edge-src-frame').getAttribute('style')).not.toContain('stroke-dasharray');
    expect(screen.getByTestId('lineage-node-draft')).toHaveAttribute('data-removed', 'true');
    // Hiding removed files drops the draft and its edge.
    fireEvent.click(screen.getByLabelText('removed'));
    expect(screen.queryByTestId('lineage-node-draft')).toBeNull();
  });

  it('the panel shows each input with its tool, time range, job and settings, and the unlinked inputs', async () => {
    await renderTab();
    fireEvent.click(screen.getByTestId('lineage-node-frame'));
    const facts = screen.getByTestId('lineage-edge-facts-src');
    expect(facts).toHaveTextContent('extract frames');
    expect(facts).toHaveTextContent('7.19s');
    fireEvent.click(screen.getByTestId('lineage-node-restyle'));
    const fal = screen.getByTestId('lineage-edge-facts-frame');
    expect(fal).toHaveTextContent('run model');
    expect(fal).toHaveTextContent('plan-1#restyle');
    expect(fal).toHaveTextContent('fal-ai/flux-pro/kontext');
    expect(fal).toHaveTextContent('watercolor');
    fireEvent.click(screen.getByTestId('lineage-node-final'));
    expect(screen.getByTestId('lineage-edge-facts-clip')).toHaveTextContent('inferred, not observed');
    expect(within(screen.getByTestId('lineage-gaps')).getByText(/still\.jpg/)).toBeInTheDocument();
    expect(within(screen.getByTestId('lineage-sources')).getByText('Meridian.mp4')).toBeInTheDocument();
  });

  it('starts focused on an asset (the asset ⋯ → Lineage) and re-reads per direction; clearing returns to the project', async () => {
    mocks.getProjectLineage.mockResolvedValue({ ok: true, value: { ...GRAPH, focus: 'final', direction: 'both' } });
    await renderTab({ initialFocus: 'final', sessionId: 'sess-1' });
    expect(mocks.getProjectLineage).toHaveBeenLastCalledWith(PID, { assetId: 'final', direction: 'both', sessionId: 'sess-1' });
    expect(screen.getByTestId('lineage-focus')).toHaveTextContent('Lineage of watercolor_final.mp4');
    expect(screen.getByTestId('lineage-details')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Lineage direction'), { target: { value: 'ancestors' } });
    await waitFor(() => expect(mocks.getProjectLineage).toHaveBeenLastCalledWith(PID,
      { assetId: 'final', direction: 'ancestors', sessionId: 'sess-1' }));
    await screen.findByTestId('lineage-canvas');
    fireEvent.click(screen.getByRole('button', { name: 'Clear focus' }));
    await waitFor(() => expect(mocks.getProjectLineage).toHaveBeenLastCalledWith(PID,
      { direction: 'project', maxNodes: 2000, sessionId: 'sess-1' }));
  });

  it('focus lineage from the panel; show in assets opens the folder', async () => {
    const onShowFolder = vi.fn();
    await renderTab({ onShowFolder });
    fireEvent.click(screen.getByTestId('lineage-node-restyle'));
    fireEvent.click(screen.getByRole('button', { name: 'Show in Assets' }));
    expect(onShowFolder).toHaveBeenCalledWith('Chats');
    fireEvent.click(screen.getByRole('button', { name: 'Focus lineage' }));
    await waitFor(() => expect(mocks.getProjectLineage).toHaveBeenLastCalledWith(PID, { assetId: 'restyle', direction: 'both' }));
  });

  it('an empty project explains itself; an error offers Retry; a gone project is not-found', async () => {
    mocks.getProjectLineage.mockResolvedValueOnce({ ok: true, value: { ...GRAPH, nodes: [], edges: [], gaps: [] } });
    render(<LineageTab project={{ id: PID, name: 'v10' }} onNotFound={vi.fn()} />);
    expect(await screen.findByText(/No lineage recorded yet/)).toBeInTheDocument();
    cleanup();
    mocks.getProjectLineage.mockResolvedValueOnce({ ok: false, kind: 'error', message: 'boom' });
    render(<LineageTab project={{ id: PID, name: 'v10' }} onNotFound={vi.fn()} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('boom');
    cleanup();
    const onNotFound = vi.fn();
    mocks.getProjectLineage.mockResolvedValueOnce({ ok: false, kind: 'not_found' });
    render(<LineageTab project={{ id: PID, name: 'v10' }} onNotFound={onNotFound} />);
    await waitFor(() => expect(onNotFound).toHaveBeenCalled());
  });
});


// ------------------------------------------------------------------ round 2: roots, chats, preview
const EXT = 'ext:' + 'a'.repeat(32);
const R2: LineageGraph = {
  nodes: [
    { id: EXT, kind: 'external', stage: 'source', parents: 0, children: 1, displayName: 'Downloaded from filesamples.example.com',
      external: true, origin: 'external', url: 'https://filesamples.example.com/s/sample.mxf', host: 'filesamples.example.com',
      license: 'unknown', firstSeenAt: '2026-10-23T14:00:00.000Z', tool: 'code_interpreter' },
    n('mxf', 'deliverable', 'intermediate', { displayName: 'sample.mxf', origin: 'external', sessionId: 's1', turnId: 't-2' }),
    n('mp4', 'deliverable', 'final', { displayName: 'Process Media (sample).mp4', origin: 'derived', sessionId: 's1', turnId: 't-3' }),
    n('up', 'source', 'standalone', { kind: 'image', displayName: 'brief.png', origin: 'upload', addedByDisplay: 'ana' }),
    n('chart', 'deliverable', 'standalone', { kind: 'image', displayName: 'chart.png', origin: 'scratch', sessionId: 's2' }),
  ],
  edges: [{ from: EXT, to: 'mxf', tool: 'code_interpreter' }, { from: 'mxf', to: 'mp4', tool: 'process-media|process_media' }],
  gaps: [],
  stats: { nodes: 5, edges: 2, droppedEdges: 0, generated: 3, generatedWithoutLineage: 0, externalSources: 1,
    origins: { uploaded: 1, imported: 0, external: 1, scratch: 1, unrecorded: 0 } },
  truncated: false, direction: 'project',
  chats: { s1: { mine: true, title: 'Versant ingest', turns: { 't-2': 2, 't-3': 3 } }, s2: { mine: false } },
};

describe('LineageTab roots, chat provenance and preview', () => {
  beforeEach(() => mocks.getProjectLineage.mockResolvedValue({ ok: true, value: R2 }));

  it('reports roots neutrally and only genuine gaps as a problem', async () => {
    await renderTab();
    expect(screen.getByTestId('lineage-origins')).toHaveTextContent('1 uploaded · 1 from outside · 1 created from scratch');
    expect(screen.queryByTestId('lineage-unlinked')).toBeNull();
    expect(screen.getByTestId('lineage-status')).toHaveTextContent('1 external source');
    expect(screen.getByTestId(`lineage-node-${EXT}`)).toHaveAttribute('data-origin', 'external');
    expect(screen.getByTestId('lineage-node-chart')).toHaveTextContent('Created from scratch');
    expect(screen.getByTestId('lineage-node-up')).toHaveTextContent('Uploaded by ana');
  });

  it('an external source shows its URL, host and unknown rights, with no preview', async () => {
    await renderTab();
    fireEvent.click(screen.getByTestId(`lineage-node-${EXT}`));
    const ext = screen.getByTestId('lineage-external');
    expect(ext).toHaveTextContent('https://filesamples.example.com/s/sample.mxf');
    expect(ext).toHaveTextContent('Unknown — check before use');
    expect(screen.queryByTestId('lineage-preview')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Show in Assets' })).toBeNull();
  });

  it('a file made in a chat links back to that chat and turn; another member\'s chat stays anonymous', async () => {
    const onOpenChat = vi.fn();
    await renderTab({ onOpenChat });
    fireEvent.click(screen.getByTestId('lineage-node-mp4'));
    expect(screen.getByTestId('lineage-chat')).toHaveTextContent('Made in chat Versant ingest · turn 3');
    fireEvent.click(within(screen.getByTestId('lineage-chat')).getByRole('button', { name: 'Open chat' }));
    expect(onOpenChat).toHaveBeenCalledWith('s1', 't-3');
    fireEvent.click(screen.getByTestId('lineage-node-chart'));
    expect(screen.getByTestId('lineage-chat')).toHaveTextContent("Made in another member's chat");
    expect(within(screen.getByTestId('lineage-chat')).queryByRole('button')).toBeNull();
  });

  it('previews the selected file through the asset-URL route and shows a hover thumbnail', async () => {
    await renderTab();
    fireEvent.click(screen.getByTestId('lineage-node-mp4'));
    const pv = await screen.findByTestId('lineage-preview');
    expect(pv).toHaveAttribute('data-kind', 'video');
    expect(pv.querySelector('video')).toHaveAttribute('src', 'https://signed.example/mp4/src');
    expect(mocks.requestAssetUrls).toHaveBeenCalledWith(PID, ['mp4']);
    fireEvent.mouseEnter(screen.getByTestId('lineage-node-up'));
    expect(await screen.findByTestId('lineage-hover-thumb')).toHaveAttribute('src', 'https://signed.example/up/thumb');
    fireEvent.mouseLeave(screen.getByTestId('lineage-node-up'));
    await waitFor(() => expect(screen.queryByTestId('lineage-hover-thumb')).toBeNull());
  });

  it('group by chat draws a chat node owning each file a chat made', async () => {
    await renderTab();
    expect(screen.queryByTestId('lineage-node-chat:s1')).toBeNull();
    fireEvent.click(screen.getByLabelText('group by chat'));
    expect(screen.getByTestId('lineage-node-chat:s1')).toHaveTextContent('Chat: Versant ingest');
    expect(screen.getByTestId('lineage-node-chat:s2')).toHaveTextContent("Another member's chat");
    expect(screen.getByTestId('lineage-edge-chat:s1-mp4')).toHaveAttribute('data-chat', 'true');
    expect(screen.queryByTestId(`lineage-edge-chat:s1-${EXT}`)).toBeNull();
  });
});

describe('a download whose source URL could not be decided', () => {
  const UNDECIDED: LineageGraph = {
    ...R2,
    nodes: [...R2.nodes,
      n('dl', 'deliverable', 'standalone', { displayName: 'recheck_sample.mp4', origin: 'external_undecided', sessionId: 's1',
        candidates: ['https://download.samplelib.com/mp4/sample-5s.mp4', 'https://sample-videos.com/v/bbb.mp4'] })],
    stats: { ...R2.stats, origins: { uploaded: 1, imported: 0, external: 1, scratch: 1, unrecorded: 0, externalUndecided: 1 } },
  };
  beforeEach(() => mocks.getProjectLineage.mockResolvedValue({ ok: true, value: UNDECIDED }));

  it('says so, lists the candidates, and does not count it as a missing input', async () => {
    await renderTab();
    expect(screen.getByTestId('lineage-origins')).toHaveTextContent('1 from outside, source not decided');
    expect(screen.queryByTestId('lineage-unlinked')).toBeNull();
    expect(screen.getByTestId('lineage-node-dl')).toHaveTextContent('source not decided (2 candidate URLs)');
    fireEvent.click(screen.getByTestId('lineage-node-dl'));
    expect(await screen.findByTestId('lineage-candidates')).toHaveTextContent('https://sample-videos.com/v/bbb.mp4');
  });
});
