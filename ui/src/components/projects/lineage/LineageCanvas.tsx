/**
 * The Lineage view's canvas: a layered left-to-right SVG drawing of the lineage DAG — sources on the left, finals on
 * the right — plus the details panel of the selected asset. Loaded lazily by `LineageTab`, so none of it (or the
 * layout) is in the main bundle.
 *
 * Edges: a recorded edge is solid; an INFERRED one (from a plan's dependencies or a turn's fetches, not observed)
 * is dotted and amber; a CONTRACTED one (through files hidden by a filter, or another member's "this chat only"
 * file) is dashed. Click an asset: its whole upstream and downstream light up, the rest dims, and the panel lists
 * what it was made from — each input with the tool, job, source time range and producer settings of that edge —
 * where it was used, its original sources and final outputs, and the inputs that could not be linked (gaps).
 */
import React, { useEffect, useMemo, useState } from 'react';
import { theme } from '../../../styles/tokens';
import type { LineageEdge, LineageGap, LineageGraph, LineageNode } from '../../../projects/api';
import { request as requestAssetUrls } from '../../../projects/assetUrlBatcher';
import { NODE_H, NODE_W, STAGE_LABEL, closure, formatRange, shortTool } from './lineageModel';
import type { Layout } from './lineageModel';

export interface LineageCanvasProps {
  layout: Layout;
  graph: LineageGraph;
  selectedId: string | null;
  onSelect(id: string | null): void;
  onFocus(id: string): void;
  onShowInAssets?(node: LineageNode): void;
  /** The Project: previews and hover thumbnails come from its asset-URL route (view-time presigned, no token). */
  projectId?: string;
  /** Open the chat that made a file, at that turn. */
  onOpenChat?(sessionId: string, turnId?: string): void;
}

/** "Uploaded by Ana", "Downloaded from host", "Created from scratch", "Inputs not recorded", … (null: derived). */
export function originLabel(n: LineageNode): string | null {
  switch (n.origin) {
    case 'upload': return n.addedByDisplay ? `Uploaded by ${n.addedByDisplay}` : 'Uploaded';
    case 'import': return n.importedFrom ? `Imported from ${n.importedFrom}` : 'Imported';
    case 'external': return n.external ? 'External source · license unknown' : 'Made from outside media';
    case 'scratch': return 'Created from scratch';
    case 'unrecorded': return 'Inputs not recorded';
    case 'external_undecided': {
      const n2 = n.candidates?.length ?? 0;
      return `Downloaded from outside · source not decided${n2 > 1 ? ` (${n2} candidate URLs)` : ''}`;
    }
    default: return null;
  }
}

/** "Made in chat <title> · turn N" (or "Made in another member's chat"), null when the file has no chat. */
export function chatLabel(n: Pick<LineageNode, 'sessionId' | 'turnId'>, chats: LineageGraph['chats']): string | null {
  if (!n.sessionId) return null;
  const c = chats?.[n.sessionId];
  if (c && !c.mine) return "Made in another member's chat";
  const turn = n.turnId && c?.turns?.[n.turnId];
  const title = c?.title || `chat ${n.sessionId.slice(0, 8)}`;
  return `Made in chat ${title}${turn ? ` · turn ${turn}` : ''}`;
}

export const STAGE_COLOR: Record<string, string> = {
  source: theme.colors.success,
  intermediate: theme.colors.textTertiary,
  final: theme.colors.primary,
  standalone: theme.colors.borderStrong,
};

const KIND_GLYPH: Record<string, string> = {
  video: '▶', image: '◼', audio: '♪', document: '¶', data: '{}', external: '⤓', chat: '◇',
};

/** The left bar: external sources and chats get their own colour, everything else its stage. */
function barColor(n: LineageNode): string {
  if (n.external) return theme.colors.warning;
  if (n.kind === 'chat') return theme.colors.textSecondary;
  return STAGE_COLOR[n.stage] ?? theme.colors.border;
}

/** How an edge is drawn: `inferred` dotted amber, `contracted` dashed, recorded solid. */
export function edgeStyle(e: Pick<LineageEdge, 'inferred' | 'contracted' | 'chat'>, lit: boolean, on: boolean): React.CSSProperties {
  if (e.chat) return { stroke: theme.colors.textTertiary, strokeWidth: 1, opacity: on ? 0.7 : 0.1, strokeDasharray: '1 3' };
  const base = e.inferred ? theme.colors.warning : theme.colors.borderStrong;
  return {
    stroke: on && lit ? (e.inferred ? theme.colors.warning : theme.colors.primary) : base,
    strokeWidth: on && lit ? 2 : 1.3,
    opacity: on ? 1 : 0.15,
    strokeDasharray: e.inferred ? '2 4' : e.contracted ? '6 4' : undefined,
  };
}

function edgeTitle(e: LineageEdge): string {
  const bits = [e.tool ? shortTool(e.tool) : '', ...(e.ranges ?? []).map(formatRange),
    e.inferred ? 'inferred' : '', e.contracted ? 'through hidden files' : ''];
  return bits.filter(Boolean).join(' · ') || 'made from';
}

export default function LineageCanvas({
  layout, graph, selectedId, onSelect, onFocus, onShowInAssets, projectId, onOpenChat,
}: LineageCanvasProps) {
  const [hover, setHover] = useState<{ id: string; x: number; y: number } | null>(null);
  const hoverThumb = useAssetUrl(projectId, hover ? layout.nodes.find((n) => n.id === hover.id) ?? null : null, 'thumb');
  const lit = useMemo(() => {
    if (!selectedId) return null;
    const c = closure(layout.edges, selectedId);
    return { ...c, all: new Set([selectedId, ...c.ancestors, ...c.descendants]) };
  }, [layout.edges, selectedId]);
  const selected = selectedId ? layout.nodes.find((n) => n.id === selectedId) ?? null : null;

  return (
    <div style={wrapStyle} data-testid="lineage-canvas">
      <div style={scrollStyle} onClick={() => onSelect(null)}>
        <svg width={layout.width} height={layout.height} role="img" aria-label="Asset lineage graph" style={{ display: 'block' }}>
          <defs>
            <marker id="lineage-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M0,0 L10,5 L0,10 z" style={{ fill: theme.colors.borderStrong }} />
            </marker>
          </defs>
          {layout.edges.map((e) => {
            const on = lit ? lit.all.has(e.from) && lit.all.has(e.to) : true;
            return (
              <path
                key={`${e.from}→${e.to}`} d={e.d} fill="none" markerEnd="url(#lineage-arrow)"
                data-testid={`lineage-edge-${e.from}-${e.to}`}
                data-inferred={e.inferred ? 'true' : undefined} data-contracted={e.contracted ? 'true' : undefined}
                data-chat={e.chat ? 'true' : undefined}
                style={edgeStyle(e, !!lit, on)}
              >
                <title>{edgeTitle(e)}</title>
              </path>
            );
          })}
          {layout.nodes.map((n) => {
            const dim = lit ? !lit.all.has(n.id) : false;
            const isSel = n.id === selectedId;
            return (
              <g
                key={n.id} transform={`translate(${n.x},${n.y})`} role="button" tabIndex={0}
                aria-label={`${n.displayName}, ${STAGE_LABEL[n.stage] ?? n.stage}`} aria-pressed={isSel}
                data-testid={`lineage-node-${n.id}`} data-stage={n.stage}
                data-removed={n.removed ? 'true' : undefined} data-missing={n.missing ? 'true' : undefined}
                data-origin={n.origin}
                style={{ cursor: 'pointer', opacity: dim ? 0.25 : 1 }}
                onMouseEnter={() => setHover({ id: n.id, x: n.x, y: n.y })}
                onMouseLeave={() => setHover((h) => (h?.id === n.id ? null : h))}
                onClick={(ev) => { ev.stopPropagation(); onSelect(isSel ? null : n.id); }}
                onKeyDown={(ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); onSelect(n.id); } }}
              >
                <rect
                  width={NODE_W} height={NODE_H} rx={8}
                  style={{
                    fill: n.missing || n.removed || n.external || n.kind === 'chat' ? theme.colors.surfaceSubtle
                      : theme.colors.surface,
                    stroke: isSel ? theme.colors.primary : n.origin === 'unrecorded' ? theme.colors.warning : theme.colors.border,
                    strokeWidth: isSel ? 2 : 1, strokeDasharray: n.removed || n.missing ? '4 3' : undefined,
                  }}
                />
                <rect width={5} height={NODE_H} rx={2} style={{ fill: barColor(n) }} />
                <text x={14} y={19} style={{ fontSize: 12, fill: theme.colors.textPrimary, fontWeight: 500 }}>
                  <tspan style={{ fill: theme.colors.textSecondary }}>{KIND_GLYPH[n.kind ?? ''] ?? '•'} </tspan>
                  {truncate(n.displayName, 24)}
                </text>
                <text x={14} y={36} style={{ fontSize: 10.5, fill: theme.colors.textSecondary }}>{subtitle(n)}</text>
                <title>{n.displayName}</title>
              </g>
            );
          })}
        </svg>
        {hover && hoverThumb && (
          <img src={hoverThumb} alt="" data-testid="lineage-hover-thumb" aria-hidden="true"
            style={{ ...hoverThumbStyle, left: hover.x, top: hover.y + NODE_H + 6 }} />
        )}
      </div>
      {selected && (
        <LineageDetails
          node={selected} graph={graph} ancestors={lit?.ancestors ?? new Set()} descendants={lit?.descendants ?? new Set()}
          onSelect={onSelect} onFocus={onFocus} onShowInAssets={onShowInAssets} projectId={projectId}
          onOpenChat={onOpenChat}
        />
      )}
    </div>
  );
}

function subtitle(n: LineageNode): string {
  if (n.missing) return 'Missing — no record';
  if (n.external) return n.license === 'unknown' || !n.license ? 'External · license unknown' : `External · ${n.license}`;
  if (n.kind === 'chat') return 'Chat';
  const root = n.origin && n.origin !== 'derived' ? originLabel(n) : null;
  const bits = [root ?? STAGE_LABEL[n.stage] ?? n.stage, !root && n.role && n.role !== n.stage ? n.role : ''];
  if (n.removed) bits.push('removed');
  else if (n.tool && !root) bits.push(shortTool(n.tool));
  return bits.filter(Boolean).join(' · ');
}

/** A view-time URL of an asset node: its thumbnail (an image's rendition / source, a video's hero image) or its
 *  source bytes for the preview. Through the Project's asset-URL route, so nothing carries a token. */
export function useAssetUrl(projectId: string | undefined, n: LineageNode | null, want: 'thumb' | 'source'): string | null {
  const [url, setUrl] = useState<string | null>(null);
  const id = n && !n.external && !n.missing && n.kind !== 'chat' ? n.id : null;
  const kind = n?.kind;
  useEffect(() => {
    setUrl(null);
    if (!projectId || !id) return;
    if (want === 'thumb' && kind !== 'image' && kind !== 'video') return;
    let live = true;
    void requestAssetUrls(projectId, [id]).then((r) => {
      const out = r.get(id);
      if (!live || !out?.ok) return;
      const e = out.entry;
      setUrl(want === 'thumb' ? e.rendition?.url ?? (kind === 'image' ? e.source?.url ?? null : null) : e.source?.url ?? null);
    });
    return () => { live = false; };
  }, [projectId, id, kind, want]);
  return url;
}

/** Plays / shows / opens the selected file: video, image, audio, or a document (a PDF opens at its page). */
export function NodePreview({ node, projectId }: { node: LineageNode; projectId?: string }) {
  const src = useAssetUrl(projectId, node, 'source');
  if (!projectId || node.external || node.missing || node.kind === 'chat') return null;
  if (!src) return <p style={mutedStyle} data-testid="lineage-preview-loading">Loading the preview…</p>;
  const k = node.kind;
  return (
    <div style={previewStyle} data-testid="lineage-preview" data-kind={k ?? ''}>
      {k === 'video' && <video src={src} controls preload="metadata" style={mediaStyle} />}
      {k === 'image' && <img src={src} alt={node.displayName} style={mediaStyle} />}
      {k === 'audio' && <audio src={src} controls preload="metadata" style={{ width: '100%' }} />}
      {k === 'document' && <iframe src={`${src}#page=1`} title={node.displayName} style={{ ...mediaStyle, height: 220 }} />}
      {(k === 'data' || !k) && <a href={src} target="_blank" rel="noopener noreferrer" style={linkStyle}>Open the file</a>}
    </div>
  );
}

/** Middle truncation: job outputs share a long prefix (`interview_raw — Process Media (…)`), so keep the end. */
export function truncate(s: string, n: number): string {
  if (s.length <= n) return s;
  const tail = Math.floor((n - 1) / 2);
  return `${s.slice(0, n - 1 - tail)}…${s.slice(s.length - tail)}`;
}

function settingText(v: unknown): string {
  if (Array.isArray(v)) return v.join(', ');
  return typeof v === 'string' ? v : JSON.stringify(v);
}

/** How one input became this asset: tool, job, the source time range, producer settings, and how it is known. */
export function EdgeFacts({ edge }: { edge: LineageEdge }) {
  const settings = Object.entries(edge.settings ?? {});
  return (
    <dl style={edgeDlStyle} data-testid={`lineage-edge-facts-${edge.from}`}>
      {edge.tool && <><dt>Tool</dt><dd title={edge.tool}>{shortTool(edge.tool)}</dd></>}
      {edge.ranges?.length ? <><dt>Range</dt><dd>{edge.ranges.map(formatRange).join(', ')}</dd></> : null}
      {edge.jobId && <><dt>Job</dt><dd style={monoStyle} title={edge.jobId}>{truncate(edge.jobId, 34)}</dd></>}
      {edge.via && <><dt>Via</dt><dd>{edge.via.replace(/_/g, ' ')}</dd></>}
      {settings.map(([k, v]) => <React.Fragment key={k}><dt>{k}</dt><dd style={settingStyle}>{settingText(v)}</dd></React.Fragment>)}
      {edge.inferred && <><dt>Known</dt><dd style={{ color: theme.colors.warning }}>inferred, not observed</dd></>}
      {edge.contracted && <><dt>Known</dt><dd>through files you cannot see</dd></>}
      {edge.recorded === 'derivedFrom' && <><dt>Known</dt><dd>from the file's record only</dd></>}
    </dl>
  );
}

interface DetailsProps {
  node: LineageNode;
  graph: LineageGraph;
  ancestors: ReadonlySet<string>;
  descendants: ReadonlySet<string>;
  onSelect(id: string | null): void;
  onFocus(id: string): void;
  onShowInAssets?(node: LineageNode): void;
  projectId?: string;
  onOpenChat?(sessionId: string, turnId?: string): void;
}

export function LineageDetails({
  node, graph, ancestors, descendants, onSelect, onFocus, onShowInAssets, projectId, onOpenChat,
}: DetailsProps) {
  const byId = useMemo(() => new Map(graph.nodes.map((n) => [n.id, n])), [graph.nodes]);
  const inEdges = graph.edges.filter((e) => e.to === node.id && byId.has(e.from));
  const outputs = graph.edges.filter((e) => e.from === node.id).map((e) => byId.get(e.to)).filter(Boolean) as LineageNode[];
  const sources = [...ancestors].map((i) => byId.get(i)).filter((n): n is LineageNode => !!n && n.stage === 'source');
  const finals = [...descendants].map((i) => byId.get(i)).filter((n): n is LineageNode => !!n && n.stage === 'final');
  const gaps: LineageGap[] = graph.gaps.filter((g) => g.child === node.id);
  const link = (n: LineageNode) => (
    <button type="button" style={linkStyle} onClick={() => onSelect(n.id)}>
      <span style={{ ...dotStyle, background: STAGE_COLOR[n.stage] }} />{n.displayName}
    </button>
  );
  const list = (title: string, items: LineageNode[], testid: string) => (
    <section style={{ marginTop: 12 }} data-testid={testid}>
      <h4 style={h4Style}>{title} <span style={countStyle}>{items.length}</span></h4>
      {items.length === 0 ? <p style={mutedStyle}>None recorded</p> : (
        <ul style={ulStyle}>{items.map((n) => <li key={n.id}>{link(n)}</li>)}</ul>
      )}
    </section>
  );
  return (
    <aside style={panelStyle} aria-label="Asset lineage details" data-testid="lineage-details">
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
        <h3 style={h3Style}>{node.displayName}</h3>
        <button type="button" style={closeStyle} aria-label="Close details" onClick={() => onSelect(null)}>×</button>
      </div>
      {originLabel(node) && (
        <p style={{ ...originStyle, color: node.origin === 'unrecorded' ? theme.colors.warning : theme.colors.textSecondary }}
          data-testid="lineage-origin">{originLabel(node)}</p>
      )}
      {node.external && (
        <dl style={dlStyle} data-testid="lineage-external">
          <dt>Source</dt><dd style={monoStyle}>{node.url}</dd>
          {node.host && <><dt>Host</dt><dd>{node.host}</dd></>}
          {node.firstSeenAt && <><dt>First seen</dt><dd>{node.firstSeenAt.slice(0, 16).replace('T', ' ')}</dd></>}
          {node.tool && <><dt>Fetched by</dt><dd>{shortTool(node.tool)}</dd></>}
          <dt>Rights</dt><dd>{node.license && node.license !== 'unknown' ? node.license : 'Unknown — check before use'}</dd>
        </dl>
      )}
      {node.origin === 'external_undecided' && (node.candidates?.length ?? 0) > 0 && (
        <dl style={dlStyle} data-testid="lineage-candidates">
          <dt>One of</dt><dd style={monoStyle}>{node.candidates!.map((u) => <div key={u}>{u}</div>)}</dd>
          <dt>Rights</dt><dd>Unknown — check before use</dd>
        </dl>
      )}
      {chatLabel(node, graph.chats) && (
        <div style={chatRowStyle} data-testid="lineage-chat">
          <span>{chatLabel(node, graph.chats)}</span>
          {onOpenChat && node.sessionId && graph.chats?.[node.sessionId]?.mine && (
            <button type="button" style={actionStyle} onClick={() => onOpenChat(node.sessionId!, node.turnId)}>Open chat</button>
          )}
        </div>
      )}
      <NodePreview node={node} projectId={projectId} />
      <dl style={dlStyle}>
        <dt>Stage</dt><dd>{STAGE_LABEL[node.stage]}</dd>
        {node.role && <><dt>Role</dt><dd>{node.role}</dd></>}
        {node.kind && <><dt>Kind</dt><dd>{node.kind}</dd></>}
        {node.status && <><dt>Status</dt><dd>{node.status}</dd></>}
        {node.tool && <><dt>Made by</dt><dd title={node.tool}>{shortTool(node.tool)}</dd></>}
        {node.jobId && <><dt>Job</dt><dd style={monoStyle} title={node.jobId}>{truncate(node.jobId, 34)}</dd></>}
        {node.folder !== undefined && !node.missing && <><dt>Folder</dt><dd>{node.folder || 'Top level'}</dd></>}
      </dl>
      {!node.missing && node.kind !== 'chat' && (
        <div style={{ display: 'flex', gap: 6, marginTop: 10, flexWrap: 'wrap' }}>
          {projectId && !node.external && <OpenButton projectId={projectId} node={node} />}
          <button type="button" style={actionStyle} onClick={() => onFocus(node.id)}>Focus lineage</button>
          {onShowInAssets && !node.removed && !node.external && (
            <button type="button" style={actionStyle} onClick={() => onShowInAssets(node)}>Show in Assets</button>
          )}
        </div>
      )}
      <section style={{ marginTop: 12 }} data-testid="lineage-inputs">
        <h4 style={h4Style}>Made from <span style={countStyle}>{inEdges.length}</span></h4>
        {inEdges.length === 0 ? <p style={mutedStyle}>None recorded</p> : (
          <ul style={ulStyle}>
            {inEdges.map((e) => (
              <li key={e.from} style={{ marginBottom: 6 }}>
                {link(byId.get(e.from)!)}
                <EdgeFacts edge={e} />
              </li>
            ))}
          </ul>
        )}
      </section>
      {gaps.length > 0 && (
        <section style={{ marginTop: 12 }} data-testid="lineage-gaps">
          <h4 style={h4Style}>Inputs not linked <span style={countStyle}>{gaps.length}</span></h4>
          <ul style={ulStyle}>
            {gaps.map((g) => (
              <li key={`${g.ref}|${g.reason}`} style={gapStyle} title={g.ref}>
                {truncate(g.ref.split('/').pop() || g.ref, 36)} <span style={countStyle}>· {g.reason.replace(/_/g, ' ')}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
      {list('Used in', outputs, 'lineage-outputs')}
      {list('Original sources', sources, 'lineage-sources')}
      {list('Final outputs', finals, 'lineage-finals')}
    </aside>
  );
}

function OpenButton({ projectId, node }: { projectId: string; node: LineageNode }) {
  const src = useAssetUrl(projectId, node, 'source');
  return (
    <a href={src ?? undefined} target="_blank" rel="noopener noreferrer" aria-disabled={!src}
      style={{ ...actionStyle, textDecoration: 'none', opacity: src ? 1 : 0.5 }}>Open</a>
  );
}

const wrapStyle: React.CSSProperties = { display: 'flex', flex: 1, minHeight: 0, minWidth: 0, height: '100%' };
const scrollStyle: React.CSSProperties = {
  flex: 1, minWidth: 0, overflow: 'auto', background: theme.colors.background, position: 'relative',
};
const hoverThumbStyle: React.CSSProperties = {
  position: 'absolute', width: 160, maxHeight: 110, objectFit: 'cover', borderRadius: 6, pointerEvents: 'none',
  border: `1px solid ${theme.colors.border}`, boxShadow: '0 4px 14px rgba(0,0,0,.18)', background: theme.colors.surface,
};
const previewStyle: React.CSSProperties = { marginTop: 10 };
const mediaStyle: React.CSSProperties = { width: '100%', maxHeight: 200, objectFit: 'contain', borderRadius: 6,
  background: '#000', border: 0 };
const originStyle: React.CSSProperties = { margin: '6px 0 0', fontSize: 'var(--fs-xs)' };
const chatRowStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 8, marginTop: 8, fontSize: 'var(--fs-xs)', color: theme.colors.textSecondary,
};
const panelStyle: React.CSSProperties = {
  width: 320, flexShrink: 0, overflowY: 'auto', padding: 14, borderLeft: `1px solid ${theme.colors.border}`,
  background: theme.colors.surface, fontSize: 'var(--fs-sm)', color: theme.colors.textPrimary,
};
const h3Style: React.CSSProperties = { margin: 0, flex: 1, fontSize: 'var(--fs-md)', wordBreak: 'break-word' };
const h4Style: React.CSSProperties = { margin: '0 0 4px', fontSize: 'var(--fs-xs)', textTransform: 'uppercase', letterSpacing: 0.4, color: theme.colors.textSecondary };
const countStyle: React.CSSProperties = { color: theme.colors.textTertiary, fontWeight: 400 };
const dlStyle: React.CSSProperties = {
  display: 'grid', gridTemplateColumns: 'auto 1fr', gap: '3px 10px', margin: '10px 0 0', fontSize: 'var(--fs-xs)',
};
const edgeDlStyle: React.CSSProperties = {
  ...dlStyle, margin: '2px 0 0 14px', gap: '1px 8px', fontSize: 'var(--fs-2xs)', color: theme.colors.textSecondary,
};
const settingStyle: React.CSSProperties = { wordBreak: 'break-word', maxHeight: 48, overflow: 'hidden' };
const monoStyle: React.CSSProperties = { fontFamily: 'var(--font-mono, monospace)', wordBreak: 'break-all' };
const ulStyle: React.CSSProperties = { listStyle: 'none', margin: 0, padding: 0 };
const mutedStyle: React.CSSProperties = { margin: 0, fontSize: 'var(--fs-xs)', color: theme.colors.textTertiary };
const gapStyle: React.CSSProperties = { fontSize: 'var(--fs-xs)', color: theme.colors.textSecondary, padding: '2px 0' };
const linkStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 6, width: '100%', textAlign: 'left', padding: '3px 0', border: 0,
  background: 'none', color: theme.colors.textPrimary, cursor: 'pointer', fontSize: 'var(--fs-xs)',
};
const dotStyle: React.CSSProperties = { width: 8, height: 8, borderRadius: '50%', flexShrink: 0, display: 'inline-block' };
const actionStyle: React.CSSProperties = {
  padding: '4px 9px', fontSize: 'var(--fs-xs)', color: theme.colors.textPrimary, background: theme.colors.surface,
  border: `1px solid ${theme.colors.borderStrong}`, borderRadius: theme.radius.button, cursor: 'pointer',
};
const closeStyle: React.CSSProperties = {
  border: 0, background: 'none', cursor: 'pointer', fontSize: 18, lineHeight: 1, color: theme.colors.textSecondary,
};
