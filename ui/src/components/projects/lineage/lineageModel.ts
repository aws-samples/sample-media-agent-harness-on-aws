/**
 * Pure model of the project Lineage view (ported from the explore/asset-lineage-graph prototype onto the lineage
 * edge API, `POST /projects/:pid/assets/lineage`): filtering, a layered
 * left-to-right DAG layout (sources on the left, finals on the right) and the ancestor/descendant closure used for
 * highlighting. No React, no DOM — every function here is unit-tested in `lineageModel.test.ts`.
 *
 * Why not ForceAtlas2/Sigma like the knowledge graph: lineage is a DAG with a direction that MEANS something
 * (raw → final). A force layout scrambles that; a layered layout reads like an edit timeline, and the graphs are
 * small (one story is tens of nodes), so plain SVG is enough and testable in jsdom.
 */
import type { LineageEdge, LineageGraph, LineageNode } from '../../../projects/api';

export const ROLES = ['source', 'deliverable', 'analysis', 'intermediate'] as const;
export type Role = (typeof ROLES)[number];

export interface LineageFilter {
  /** Stored roles to show; a hidden node's edges are CONTRACTED (A → hidden → B is drawn A ⇢ B). */
  roles: ReadonlySet<string>;
  showRemoved: boolean;
}

/** An edge as drawn: a recorded edge with all its fields, or one contracted through hidden nodes. */
export type FilteredEdge = LineageEdge;

export interface FilteredGraph {
  nodes: LineageNode[];
  edges: FilteredEdge[];
}

function roleOf(n: LineageNode): string {
  return n.role ?? 'source';
}

function visible(n: LineageNode, f: LineageFilter, focus?: string): boolean {
  if (n.id === focus) return true;
  if (n.removed && !f.showRemoved) return false;
  if (n.missing) return true; // a ghost keeps the chain readable whatever the filter
  return f.roles.has(roleOf(n));
}

/**
 * Drop the filtered-out nodes and contract the edges through them, so hiding the analysis files never makes the
 * interview look unrelated to the reel cut from its shots. Duplicate edges collapse; a direct edge wins over a
 * contracted one.
 */
export function filterGraph(g: Pick<LineageGraph, 'nodes' | 'edges' | 'focus'>, f: LineageFilter): FilteredGraph {
  const keep = new Set(g.nodes.filter((n) => visible(n, f, g.focus)).map((n) => n.id));
  const out = new Map<string, FilteredEdge>();
  const children = new Map<string, LineageEdge[]>();
  for (const e of g.edges) {
    const list = children.get(e.from) ?? [];
    list.push(e);
    children.set(e.from, list);
  }
  for (const start of keep) {
    const direct = children.get(start) ?? [];
    // Direct edges first, so a direct edge always wins over a path through hidden nodes to the same asset.
    for (const e of direct) {
      if (keep.has(e.to)) out.set(`${start}→${e.to}`, { ...e });
    }
    // Then walk forward through hidden nodes until a kept node is reached.
    const stack = direct.filter((e) => !keep.has(e.to)).map((e) => e.to);
    const seen = new Set<string>();
    while (stack.length) {
      const id = stack.pop()!;
      if (seen.has(id)) continue;
      seen.add(id);
      if (keep.has(id)) {
        const key = `${start}→${id}`;
        if (!out.has(key) && id !== start) out.set(key, { from: start, to: id, contracted: true });
        continue;
      }
      for (const e of children.get(id) ?? []) stack.push(e.to);
    }
  }
  return { nodes: g.nodes.filter((n) => keep.has(n.id)), edges: [...out.values()] };
}

/** Everything upstream (`ancestors`) and downstream (`descendants`) of `id` in this edge set. */
export function closure(edges: readonly LineageEdge[], id: string): { ancestors: Set<string>; descendants: Set<string> } {
  const up = new Map<string, string[]>();
  const down = new Map<string, string[]>();
  for (const e of edges) {
    (up.get(e.to) ?? up.set(e.to, []).get(e.to)!).push(e.from);
    (down.get(e.from) ?? down.set(e.from, []).get(e.from)!).push(e.to);
  }
  const walk = (adj: Map<string, string[]>) => {
    const seen = new Set<string>();
    const stack = [...(adj.get(id) ?? [])];
    while (stack.length) {
      const n = stack.pop()!;
      if (seen.has(n) || n === id) continue;
      seen.add(n);
      stack.push(...(adj.get(n) ?? []));
    }
    return seen;
  };
  return { ancestors: walk(up), descendants: walk(down) };
}

export interface PlacedNode extends LineageNode {
  x: number;
  y: number;
  layer: number;
}

export interface PlacedEdge extends FilteredEdge {
  /** SVG path data, from the right edge of `from` to the left edge of `to`. */
  d: string;
}

export interface Layout {
  nodes: PlacedNode[];
  edges: PlacedEdge[];
  width: number;
  height: number;
}

export const NODE_W = 200;
export const NODE_H = 46;
const COL_GAP = 90;
const ROW_GAP = 18;
const PAD = 24;

/**
 * Longest-path layering (a node sits one column right of its right-most input), then two barycentre sweeps to
 * order each column by its inputs' rows, which keeps one story's chain roughly horizontal. Cycles (which the data
 * should never contain) are broken by ignoring the edge that closes them.
 */
export function layoutLineage(g: FilteredGraph): Layout {
  const ids = g.nodes.map((n) => n.id);
  const known = new Set(ids);
  const edges = g.edges.filter((e) => known.has(e.from) && known.has(e.to) && e.from !== e.to);
  const parents = new Map<string, string[]>(ids.map((i) => [i, []]));
  const children = new Map<string, string[]>(ids.map((i) => [i, []]));
  for (const e of edges) {
    parents.get(e.to)!.push(e.from);
    children.get(e.from)!.push(e.to);
  }
  // Layer by DFS with an on-stack guard (cycle-safe).
  const layer = new Map<string, number>();
  const onStack = new Set<string>();
  const depthOf = (id: string): number => {
    const known_ = layer.get(id);
    if (known_ !== undefined) return known_;
    onStack.add(id);
    let d = 0;
    for (const p of parents.get(id) ?? []) if (!onStack.has(p)) d = Math.max(d, depthOf(p) + 1);
    onStack.delete(id);
    layer.set(id, d);
    return d;
  };
  ids.forEach(depthOf);

  const columns: string[][] = [];
  for (const id of ids) (columns[layer.get(id)!] ??= []).push(id);
  for (const col of columns) if (col) col.sort();
  const row = new Map<string, number>();
  const index = () => columns.forEach((col) => col?.forEach((id, i) => row.set(id, i)));
  index();
  const bary = (id: string, adj: Map<string, string[]>) => {
    const ns = (adj.get(id) ?? []).filter((n) => row.has(n));
    return ns.length ? ns.reduce((s, n) => s + row.get(n)!, 0) / ns.length : row.get(id)!;
  };
  for (let sweep = 0; sweep < 2; sweep++) {
    for (let c = 1; c < columns.length; c++) {
      columns[c]?.sort((a, b) => bary(a, parents) - bary(b, parents) || a.localeCompare(b));
      index();
    }
    for (let c = columns.length - 2; c >= 0; c--) {
      columns[c]?.sort((a, b) => bary(a, children) - bary(b, children) || a.localeCompare(b));
      index();
    }
  }
  const tallest = Math.max(0, ...columns.map((c) => c?.length ?? 0));
  const height = PAD * 2 + Math.max(1, tallest) * (NODE_H + ROW_GAP) - ROW_GAP;
  const pos = new Map<string, { x: number; y: number }>();
  columns.forEach((col, c) => {
    if (!col) return;
    const colH = col.length * (NODE_H + ROW_GAP) - ROW_GAP;
    const top = (height - colH) / 2;
    col.forEach((id, i) => pos.set(id, { x: PAD + c * (NODE_W + COL_GAP), y: top + i * (NODE_H + ROW_GAP) }));
  });
  const nodes = g.nodes.map((n) => ({ ...n, ...pos.get(n.id)!, layer: layer.get(n.id)! }));
  const placed = edges.map((e) => {
    const a = pos.get(e.from)!;
    const b = pos.get(e.to)!;
    const x1 = a.x + NODE_W;
    const y1 = a.y + NODE_H / 2;
    const x2 = b.x;
    const y2 = b.y + NODE_H / 2;
    const mid = (x1 + x2) / 2;
    return { ...e, d: `M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}` };
  });
  const width = PAD * 2 + Math.max(1, columns.length) * (NODE_W + COL_GAP) - COL_GAP;
  return { nodes, edges: placed, width, height };
}

/** Human labels for the derived stage. */
export const STAGE_LABEL: Record<string, string> = {
  source: 'Source',
  intermediate: 'Intermediate',
  final: 'Final',
  standalone: 'Unlinked',
};

/** `12.5s`, `3.0–7.5s`, `frame 300`, `frames 10–20`. */
export function formatRange(r: { start: number; end: number; unit?: string }): string {
  const f = (v: number) => (Number.isInteger(v) ? String(v) : v.toFixed(Math.abs(v) < 10 ? 2 : 1).replace(/0+$/, '').replace(/\.$/, ''));
  if (r.unit === 'frame') return r.start === r.end ? `frame ${r.start}` : `frames ${r.start}–${r.end}`;
  return r.start === r.end ? `${f(r.start)}s` : `${f(r.start)}–${f(r.end)}s`;
}

/** A gateway tool name without its target prefix (`usr-8ddf…___run_model` → `run_model`), `|` trio → its tool. */
export function shortTool(t: string): string {
  return t.split('|').pop()!.split('___').pop()!.replace(/_/g, ' ');
}
