/**
 * The Projects surface's Lineage view (`/config.lineageEnabled`): what every file of the Project was made from and
 * where it was used, read from `POST /projects/:pid/assets/lineage` (Asset_Service `lineage`, over the lineage edge
 * records) and drawn by the lazily loaded `LineageCanvas`.
 *
 * Whole Project by default; an asset's ⋯ → Lineage (or "Focus lineage" in the panel) centres it, with a direction
 * (both / made from / used in). The toolbar filters stored roles (a hidden role's edges are contracted, never
 * dropped) and removed files; the status line says how complete the record is — generated files with no recorded
 * lineage, inputs that could not be linked (dropped edges), and whether the graph was cut at its size cap.
 */
import React, { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { theme } from '../../../styles/tokens';
import { getProjectLineage } from '../../../projects/api';
import type { LineageEdge, LineageGraph, LineageNode } from '../../../projects/api';
import { ROLES, filterGraph, layoutLineage } from './lineageModel';

const LineageCanvas = lazy(() => import('./LineageCanvas'));

export interface LineageTabProps {
  project: { id: string; name: string };
  onNotFound(): void;
  /** Open the Assets grid at a folder (`""` = the top level). */
  onShowFolder?(folder: string): void;
  /** Start focused on this asset (an asset's ⋯ → Lineage). */
  initialFocus?: string | null;
  /** The chat this surface is beside: its own "this chat only" files are shown to their owner. */
  sessionId?: string | null;
  /** Open a chat (that made a file) at a turn. */
  onOpenChat?(sessionId: string, turnId?: string): void;
}

/**
 * "Group by chat": a chat node owning each file a chat made (a dotted link chat → file), so a session's work reads as
 * one cluster. Chats the actor cannot see stay one anonymous node per chat.
 */
export function groupByChat(g: LineageGraph): LineageGraph {
  const chatNodes = new Map<string, LineageNode>();
  const edges = [...g.edges];
  for (const n of g.nodes) {
    if (!n.sessionId || n.external) continue;
    const id = `chat:${n.sessionId}`;
    if (!chatNodes.has(id)) {
      const c = g.chats?.[n.sessionId];
      chatNodes.set(id, {
        id, kind: 'chat', stage: 'source', parents: 0, children: 0, sessionId: n.sessionId,
        displayName: c?.mine === false ? "Another member's chat" : `Chat: ${c?.title || n.sessionId.slice(0, 8)}`,
      });
    }
    edges.push({ from: id, to: n.id, chat: true } as LineageEdge);
  }
  return { ...g, nodes: [...g.nodes, ...chatNodes.values()], edges };
}

type Direction = 'both' | 'ancestors' | 'descendants';
type View = { kind: 'loading' } | { kind: 'error'; message: string } | { kind: 'ready'; graph: LineageGraph };

const DIRECTIONS: { value: Direction; label: string }[] = [
  { value: 'both', label: 'Both ways' }, { value: 'ancestors', label: 'Made from' }, { value: 'descendants', label: 'Used in' },
];

export default function LineageTab({
  project, onNotFound, onShowFolder, initialFocus = null, sessionId, onOpenChat,
}: LineageTabProps) {
  const [byChat, setByChat] = useState(false);
  const [view, setView] = useState<View>({ kind: 'loading' });
  const [focus, setFocus] = useState<string | null>(initialFocus);
  const [direction, setDirection] = useState<Direction>('both');
  const [selected, setSelected] = useState<string | null>(initialFocus);
  const [roles, setRoles] = useState<Set<string>>(() => new Set(ROLES));
  const [showRemoved, setShowRemoved] = useState(true);
  const [showGaps, setShowGaps] = useState(false);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);

  const load = useCallback(async () => {
    const r = await getProjectLineage(project.id, {
      ...(focus ? { assetId: focus, direction } : { direction: 'project', maxNodes: 2000 }),
      ...(sessionId ? { sessionId } : {}),
    });
    if (!alive.current) return;
    if (r.ok) { setView({ kind: 'ready', graph: r.value }); return; }
    if (r.kind === 'not_found' && !focus) { onNotFound(); return; }
    setView({ kind: 'error', message: r.kind === 'not_found' ? 'That file is not available.'
      : (r.kind === 'error' && r.message) || 'Could not load the lineage.' });
  }, [project.id, focus, direction, sessionId, onNotFound]);

  useEffect(() => { void load(); }, [load]);

  const graph = view.kind === 'ready' ? view.graph : null;
  const shown = useMemo(() => (graph && byChat ? groupByChat(graph) : graph), [graph, byChat]);
  const layout = useMemo(() => (shown ? layoutLineage(filterGraph(shown, { roles, showRemoved })) : null),
    [shown, roles, showRemoved]);
  const nameOf = useCallback((id?: string | null) => graph?.nodes.find((n) => n.id === id)?.displayName ?? id ?? '', [graph]);

  const onFocus = useCallback((id: string) => { setFocus(id); setSelected(id); setView({ kind: 'loading' }); }, []);
  const showInAssets = onShowFolder ? (n: LineageNode) => onShowFolder(n.folder ?? '') : undefined;
  const toggleRole = (role: string) => setRoles((cur) => {
    const next = new Set(cur);
    if (next.has(role)) next.delete(role); else next.add(role);
    return next;
  });
  const stats = graph?.stats;
  const inferred = graph ? graph.edges.filter((e) => e.inferred).length : 0;

  return (
    <div style={wrapStyle} data-testid="lineage-tab">
      <div style={toolbarStyle}>
        {focus ? (
          <span style={chipStyle} data-testid="lineage-focus">
            Lineage of {nameOf(focus)}
            <button type="button" style={chipButtonStyle} aria-label="Clear focus"
              onClick={() => { setFocus(null); setSelected(null); setView({ kind: 'loading' }); }}>×</button>
          </span>
        ) : <span style={{ fontWeight: 500 }}>Whole project</span>}
        {focus && (
          <label style={checkStyle}>
            <select aria-label="Lineage direction" value={direction} style={selectStyle}
              onChange={(e) => { setDirection(e.target.value as Direction); setView({ kind: 'loading' }); }}>
              {DIRECTIONS.map((d) => <option key={d.value} value={d.value}>{d.label}</option>)}
            </select>
          </label>
        )}
        <span style={{ flex: 1 }} />
        {ROLES.map((r) => (
          <label key={r} style={checkStyle}>
            <input type="checkbox" checked={roles.has(r)} onChange={() => toggleRole(r)} /> {r}
          </label>
        ))}
        <label style={checkStyle}>
          <input type="checkbox" checked={showRemoved} onChange={() => setShowRemoved((v) => !v)} /> removed
        </label>
        <label style={checkStyle}>
          <input type="checkbox" checked={byChat} onChange={() => setByChat((v) => !v)} /> group by chat
        </label>
      </div>
      {graph && stats && (
        <div style={statusStyle} role="status" data-testid="lineage-status">
          <span>{stats.nodes} files · {stats.edges} links{inferred ? ` (${inferred} inferred)` : ''}
            {stats.externalSources ? ` · ${stats.externalSources} external source${stats.externalSources === 1 ? '' : 's'}` : ''}</span>
          {stats.origins && (
            <span data-testid="lineage-origins">
              {' · '}{[
                stats.origins.uploaded ? `${stats.origins.uploaded} uploaded` : '',
                stats.origins.imported ? `${stats.origins.imported} imported` : '',
                stats.origins.external ? `${stats.origins.external} from outside` : '',
                stats.origins.scratch ? `${stats.origins.scratch} created from scratch` : '',
                stats.origins.externalUndecided ? `${stats.origins.externalUndecided} from outside, source not decided` : '',
              ].filter(Boolean).join(' · ')}
            </span>
          )}
          {stats.generatedWithoutLineage !== undefined && stats.generatedWithoutLineage > 0 && (
            <strong data-testid="lineage-unlinked" style={{ color: theme.colors.warning }}>
              {' · '}{stats.generatedWithoutLineage} of {stats.generated} generated
              {stats.generatedWithoutLineage === 1 ? ' file has' : ' files have'} inputs not recorded
            </strong>
          )}
          {graph.gaps.length > 0 && (
            <button type="button" style={linkButtonStyle} onClick={() => setShowGaps((v) => !v)} data-testid="lineage-gaps-toggle">
              {' · '}{graph.gaps.length} input{graph.gaps.length === 1 ? '' : 's'} could not be linked
            </button>
          )}
          {graph.truncated && <span>{' · '}truncated at the size cap</span>}
          <span style={{ flex: 1 }} />
          <span style={legendStyle} aria-hidden="true">
            <svg width="22" height="6"><line x1="0" y1="3" x2="22" y2="3" stroke={theme.colors.borderStrong} strokeWidth="1.5" /></svg> recorded
            <svg width="22" height="6"><line x1="0" y1="3" x2="22" y2="3" stroke={theme.colors.warning} strokeWidth="1.5" strokeDasharray="2 4" /></svg> inferred
            <svg width="22" height="6"><line x1="0" y1="3" x2="22" y2="3" stroke={theme.colors.borderStrong} strokeWidth="1.5" strokeDasharray="6 4" /></svg> through hidden files
          </span>
        </div>
      )}
      {graph && showGaps && graph.gaps.length > 0 && (
        <ul style={gapListStyle} data-testid="lineage-gaps-list">
          {graph.gaps.slice(0, 50).map((g) => (
            <li key={`${g.child}|${g.ref}|${g.reason}`}>
              <button type="button" style={linkButtonStyle} onClick={() => setSelected(g.child)}>{nameOf(g.child)}</button>
              {' '}← <span title={g.ref}>{g.ref.split('/').pop()}</span> · {g.reason.replace(/_/g, ' ')}
              {g.tool ? ` · ${g.tool.split('___').pop()}` : ''}
            </li>
          ))}
        </ul>
      )}
      <div style={bodyStyle}>
        {view.kind === 'loading' && <p style={mutedPadStyle}>Loading the lineage…</p>}
        {view.kind === 'error' && (
          <p style={mutedPadStyle} role="alert">
            {view.message} <button type="button" style={buttonStyle} onClick={() => { setView({ kind: 'loading' }); void load(); }}>Retry</button>
          </p>
        )}
        {layout && graph && layout.nodes.length === 0 && (
          <p style={mutedPadStyle}>
            No lineage recorded yet. Files a chat makes from other files of this project appear here, linked to what
            they were made from.
          </p>
        )}
        {layout && graph && layout.nodes.length > 0 && (
          <Suspense fallback={<p style={mutedPadStyle}>Loading the lineage view…</p>}>
            <LineageCanvas
              layout={layout} graph={shown ?? graph} selectedId={selected} onSelect={setSelected} onFocus={onFocus}
              onShowInAssets={showInAssets} projectId={project.id} onOpenChat={onOpenChat}
            />
          </Suspense>
        )}
      </div>
    </div>
  );
}

const wrapStyle: React.CSSProperties = { display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0, minWidth: 0, overflow: 'hidden' };
const toolbarStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 10, padding: '8px 24px', flexWrap: 'wrap',
  borderBottom: `1px solid ${theme.colors.border}`, fontSize: 'var(--fs-xs)', color: theme.colors.textPrimary,
};
const statusStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 6, padding: '6px 24px', fontSize: 'var(--fs-xs)', flexWrap: 'wrap',
  color: theme.colors.textSecondary,
};
const legendStyle: React.CSSProperties = { display: 'inline-flex', alignItems: 'center', gap: 6 };
const bodyStyle: React.CSSProperties = { display: 'flex', flex: 1, minHeight: 0, minWidth: 0 };
const selectStyle: React.CSSProperties = {
  padding: '3px 6px', fontSize: 'var(--fs-xs)', color: theme.colors.textPrimary, background: theme.colors.surface,
  border: `1px solid ${theme.colors.borderStrong}`, borderRadius: theme.radius.button,
};
const buttonStyle: React.CSSProperties = {
  padding: '4px 10px', fontSize: 'var(--fs-xs)', color: theme.colors.textPrimary, background: theme.colors.surface,
  border: `1px solid ${theme.colors.borderStrong}`, borderRadius: theme.radius.button, cursor: 'pointer',
};
const linkButtonStyle: React.CSSProperties = {
  border: 0, background: 'none', padding: 0, cursor: 'pointer', color: theme.colors.textPrimary, fontSize: 'var(--fs-xs)',
  textDecoration: 'underline',
};
const checkStyle: React.CSSProperties = { display: 'inline-flex', alignItems: 'center', gap: 4, cursor: 'pointer' };
const chipStyle: React.CSSProperties = {
  display: 'inline-flex', alignItems: 'center', gap: 4, padding: '2px 4px 2px 9px', borderRadius: 999,
  background: theme.colors.primarySubtle, color: theme.colors.textPrimary,
};
const chipButtonStyle: React.CSSProperties = { border: 0, background: 'none', cursor: 'pointer', fontSize: 14, lineHeight: 1 };
const gapListStyle: React.CSSProperties = {
  margin: 0, padding: '4px 24px 8px 40px', fontSize: 'var(--fs-xs)', color: theme.colors.textSecondary, maxHeight: 160,
  overflowY: 'auto', borderBottom: `1px solid ${theme.colors.border}`,
};
const mutedStyle: React.CSSProperties = { margin: 0, color: theme.colors.textSecondary, fontSize: 'var(--fs-xs)' };
const mutedPadStyle: React.CSSProperties = { ...mutedStyle, padding: 24, fontSize: 'var(--fs-sm)' };
