import { describe, expect, it } from 'vitest';
import type { LineageGraph, LineageNode } from '../../../projects/api';
import { NODE_W, ROLES, closure, filterGraph, formatRange, layoutLineage, shortTool } from './lineageModel';

function node(id: string, role: string, stage: LineageNode['stage'], extra: Partial<LineageNode> = {}): LineageNode {
  return { id, kind: 'video', displayName: id, role, stage, status: 'indexed', parents: 0, children: 0, ...extra };
}

// raw → transcript(analysis) → captions(analysis) → final ;  raw → clip → reel → final
const G: Pick<LineageGraph, 'nodes' | 'edges' | 'focus'> = {
  nodes: [
    node('raw', 'source', 'source'),
    node('transcript', 'analysis', 'intermediate'),
    node('captions', 'analysis', 'intermediate'),
    node('clip', 'deliverable', 'intermediate'),
    node('reel', 'deliverable', 'intermediate'),
    node('old', 'deliverable', 'final', { removed: true }),
    node('final', 'deliverable', 'final'),
  ],
  edges: [
    { from: 'raw', to: 'transcript', tool: 'process_media' },
    { from: 'transcript', to: 'captions' },
    { from: 'captions', to: 'final' },
    { from: 'raw', to: 'clip' },
    { from: 'clip', to: 'reel' },
    { from: 'clip', to: 'old' },
    { from: 'reel', to: 'final', tool: 'timeline_trim' },
  ],
};
const ALL = { roles: new Set<string>(ROLES), showRemoved: true };

describe('filterGraph', () => {
  it('keeps everything with every role on', () => {
    const f = filterGraph(G, ALL);
    expect(f.nodes).toHaveLength(7);
    expect(f.edges).toHaveLength(7);
    expect(f.edges).toContainEqual({ from: 'reel', to: 'final', tool: 'timeline_trim' });
  });

  it('contracts edges through a hidden role instead of dropping the link', () => {
    const f = filterGraph(G, { roles: new Set(['source', 'deliverable']), showRemoved: true });
    expect(f.nodes.map((n) => n.id)).not.toContain('transcript');
    expect(f.edges).toContainEqual({ from: 'raw', to: 'final', contracted: true });
    expect(f.edges).toContainEqual({ from: 'raw', to: 'clip' });
  });

  it('prefers a direct edge over a contracted one to the same asset', () => {
    const g = { nodes: [node('a', 'source', 'source'), node('h', 'analysis', 'intermediate'), node('b', 'deliverable', 'final')],
      edges: [{ from: 'a', to: 'h' }, { from: 'h', to: 'b' }, { from: 'a', to: 'b', tool: 'x' }] };
    const f = filterGraph(g, { roles: new Set(['source', 'deliverable']), showRemoved: true });
    expect(f.edges).toEqual([{ from: 'a', to: 'b', tool: 'x' }]);
  });

  it('hides removed files on request but never the focus or a ghost', () => {
    const g = { ...G, nodes: [...G.nodes, node('ghost', 'source', 'source', { missing: true })],
      edges: [...G.edges, { from: 'ghost', to: 'raw' }] };
    const f = filterGraph(g, { roles: new Set(['deliverable']), showRemoved: false });
    expect(f.nodes.map((n) => n.id)).toEqual(expect.arrayContaining(['ghost', 'clip', 'reel', 'final']));
    expect(f.nodes.map((n) => n.id)).not.toContain('old');
    expect(filterGraph({ ...G, focus: 'old' }, { roles: new Set(), showRemoved: false }).nodes.map((n) => n.id)).toEqual(['old']);
  });
});

describe('closure', () => {
  it('collects every ancestor and descendant', () => {
    const c = closure(G.edges, 'clip');
    expect([...c.ancestors]).toEqual(['raw']);
    expect([...c.descendants].sort()).toEqual(['final', 'old', 'reel']);
  });

  it('survives a cycle', () => {
    const c = closure([{ from: 'a', to: 'b' }, { from: 'b', to: 'a' }], 'a');
    expect([...c.ancestors]).toEqual(['b']);
    expect([...c.descendants]).toEqual(['b']);
  });
});

describe('layoutLineage', () => {
  it('puts each asset one column right of its right-most input', () => {
    const l = layoutLineage(filterGraph(G, ALL));
    const layer = Object.fromEntries(l.nodes.map((n) => [n.id, n.layer]));
    expect(layer).toEqual({ raw: 0, transcript: 1, clip: 1, captions: 2, reel: 2, old: 2, final: 3 });
    const x = Object.fromEntries(l.nodes.map((n) => [n.id, n.x]));
    expect(x.final).toBeGreaterThan(x.reel);
    expect(l.width).toBeGreaterThanOrEqual(x.final + NODE_W);
  });

  it('draws every edge left to right with a path', () => {
    const l = layoutLineage(filterGraph(G, ALL));
    expect(l.edges).toHaveLength(7);
    for (const e of l.edges) expect(e.d).toMatch(/^M[\d.]+,[\d.]+ C/);
  });

  it('lays out a cycle without recursing forever and copes with an empty graph', () => {
    const l = layoutLineage({ nodes: [node('a', 'source', 'source'), node('b', 'source', 'source')],
      edges: [{ from: 'a', to: 'b' }, { from: 'b', to: 'a' }] });
    expect(l.nodes).toHaveLength(2);
    expect(layoutLineage({ nodes: [], edges: [] }).nodes).toEqual([]);
  });

  it('keeps nodes of one column from overlapping', () => {
    const l = layoutLineage(filterGraph(G, ALL));
    const col2 = l.nodes.filter((n) => n.layer === 2).map((n) => n.y).sort((a, b) => a - b);
    for (let i = 1; i < col2.length; i++) expect(col2[i] - col2[i - 1]).toBeGreaterThanOrEqual(46);
  });
});

describe('edge facts', () => {
  it('a recorded edge keeps every field through the filter (tool, job, ranges, settings, inferred)', () => {
    const e = { from: 'a', to: 'b', tool: 't', jobId: 'p#n', ranges: [{ start: 1, end: 2, unit: 's' as const }],
      settings: { model: 'm' }, inferred: true };
    const g = { nodes: [node('a', 'source', 'source'), node('b', 'deliverable', 'final')], edges: [e] };
    expect(filterGraph(g, ALL).edges).toEqual([e]);
  });

  it('formats time ranges and gateway tool names', () => {
    expect(formatRange({ start: 7.191, end: 7.191, unit: 's' })).toBe('7.19s');
    expect(formatRange({ start: 3, end: 7.5, unit: 's' })).toBe('3–7.5s');
    expect(formatRange({ start: 300, end: 300, unit: 'frame' })).toBe('frame 300');
    expect(formatRange({ start: 10, end: 20, unit: 'frame' })).toBe('frames 10–20');
    expect(shortTool('usr-8ddf64af24be45c894a7a8c1f254c83f___run_model')).toBe('run model');
    expect(shortTool('shot-segmentation|segment_shots')).toBe('segment shots');
    expect(shortTool('code_interpreter')).toBe('code interpreter');
  });
});
