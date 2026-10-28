/**
 * The Plus_Menu's `Recent in <Active_Project>` section: up to {@link RECENT_ASSET_LIMIT} of the project's newest
 * assets, so a file already in the project can go with the next message without being uploaded again.
 *
 * {@link useRecentAssets} reads the first page of `GET /projects/:pid/assets` each time the menu opens (no
 * polling) and keeps the answer per project for as long as the menu is mounted: a reopen shows the cached rows at
 * once while the read refreshes them, and a failed refresh keeps them. {@link RecentAssetsSection} draws the rows,
 * the loading / empty / error notes, and `View all`, which opens the Projects_Surface on the project.
 */
import React, { useEffect, useRef, useState } from 'react';
import Badge from '../ui/Badge';
import { theme } from '../../styles/tokens';
import { AssetKindIcon, statusLabel, statusVariant } from '../projects/assetDisplay';
import FolderIcon from '../icons/FolderIcon';
import { formatBytes } from '../../projects/upload/kinds';
import type { AssetIngestionStatus, AssetView } from '../../projects/api';
import { iconColumnStyle, itemNameStyle, metaStyle, noteStyle, rowStyle, sectionHeadingStyle } from './plusMenuStyles';

export const RECENT_ASSET_LIMIT = 5;
/** Rows a name search shows in the menu (the server answers at most 50). */
export const SEARCH_RESULT_LIMIT = 20;
const SEARCH_DEBOUNCE_MS = 250;

/** The first-page read. Structurally compatible with `listAssets`'s result. */
export type RecentAssetsResult =
  | { ok: true; value: { assets: readonly AssetView[] } }
  | { ok: false };

/** Statuses drawn without a badge: the resting states a user does not need told about. */
const QUIET_STATUSES: ReadonlySet<AssetIngestionStatus> = new Set(['ready', 'indexed', 'queued', 'stored']);

/** Statuses that cannot be attached yet, with the reason given on the row's tooltip (its badge says why). */
const NOT_ATTACHABLE: Partial<Record<AssetIngestionStatus, string>> = { uploading: 'Still uploading' };

/**
 * Whether a row may be offered here: not removed, and not a "this chat only" file — a `visibility: 'session'`
 * asset is hidden, belongs to the chat that uploaded it, and is not attachable anywhere else (asset-lineage D2).
 */
export function isAttachableFromProject(a: AssetView): boolean {
  return a.status !== 'removed' && a.visibility !== 'session';
}

/** The newest {@link RECENT_ASSET_LIMIT} attachable assets, newest first — pinned samples (0-demo.mp4) first of all. */
export function pickRecentAssets(assets: readonly AssetView[]): AssetView[] {
  const live = assets.filter(isAttachableFromProject);
  // The route already answers newest first; order by `registeredAt` too, where both rows carry one, so a
  // differently ordered page still shows the newest. The sort is stable, so rows without it keep their place.
  // A pinned sample stays on top: the demo tour attaches the first video listed here.
  const ordered = [...live].sort((a, b) => {
    const pa = a.pinned === true ? 1 : 0;
    const pb = b.pinned === true ? 1 : 0;
    if (pa !== pb) return pb - pa;
    const ta = typeof a.registeredAt === 'string' ? Date.parse(a.registeredAt) : NaN;
    const tb = typeof b.registeredAt === 'string' ? Date.parse(b.registeredAt) : NaN;
    if (Number.isNaN(ta) || Number.isNaN(tb)) return 0;
    return tb - ta;
  });
  return ordered.slice(0, RECENT_ASSET_LIMIT);
}

export type RecentAssetsState =
  | { status: 'loading'; assets: readonly AssetView[] }
  | { status: 'ready'; assets: readonly AssetView[] }
  | { status: 'error'; assets: readonly AssetView[] };

const LOADING: RecentAssetsState = { status: 'loading', assets: [] };

export function useRecentAssets(
  open: boolean,
  projectId: string | null,
  load: ((projectId: string) => Promise<RecentAssetsResult>) | null,
): RecentAssetsState {
  const cacheRef = useRef(new Map<string, AssetView[]>());
  const [view, setView] = useState<{ projectId: string | null; state: RecentAssetsState }>({
    projectId: null,
    state: LOADING,
  });
  const loadRef = useRef(load);
  loadRef.current = load;
  const enabled = !!load;

  useEffect(() => {
    if (!open || !projectId || !enabled) return;
    let live = true;
    const cached = cacheRef.current.get(projectId);
    setView({ projectId, state: cached ? { status: 'ready', assets: cached } : LOADING });
    const failed = () => {
      if (live && !cached) setView({ projectId, state: { status: 'error', assets: [] } });
    };
    let pending: Promise<RecentAssetsResult>;
    try {
      pending = (loadRef.current as (pid: string) => Promise<RecentAssetsResult>)(projectId);
    } catch {
      failed();
      return;
    }
    pending
      .then((r) => {
        if (!live) return;
        if (!r.ok) {
          failed();
          return;
        }
        const recent = pickRecentAssets(r.value.assets);
        cacheRef.current.set(projectId, recent);
        setView({ projectId, state: { status: 'ready', assets: recent } });
      })
      .catch(failed);
    return () => {
      live = false;
    };
  }, [open, projectId, enabled]);

  if (view.projectId !== projectId) {
    const cached = projectId ? cacheRef.current.get(projectId) : undefined;
    return cached ? { status: 'ready', assets: cached } : LOADING;
  }
  return view.state;
}

/**
 * A filename search of the active project while the menu is open: `null` until something is typed. Debounced;
 * an answer for an older query never replaces a newer one. Works whether the project is indexed or not.
 */
export function useAssetSearch(
  open: boolean,
  projectId: string | null,
  query: string,
  search: ((projectId: string, q: string) => Promise<RecentAssetsResult>) | null,
): RecentAssetsState | null {
  const [state, setState] = useState<RecentAssetsState | null>(null);
  const searchRef = useRef(search);
  searchRef.current = search;
  const q = query.trim();
  useEffect(() => {
    if (!open || !projectId || !q || !searchRef.current) {
      setState(null);
      return;
    }
    let live = true;
    setState((cur) => ({ status: 'loading', assets: cur?.assets ?? [] }));
    const t = setTimeout(() => {
      Promise.resolve()
        .then(() => (searchRef.current as (pid: string, q: string) => Promise<RecentAssetsResult>)(projectId, q))
        .then((r) => {
          if (!live) return;
          if (!r.ok) setState({ status: 'error', assets: [] });
          else setState({ status: 'ready', assets: r.value.assets.filter(isAttachableFromProject).slice(0, SEARCH_RESULT_LIMIT) });
        })
        .catch(() => { if (live) setState({ status: 'error', assets: [] }); });
    }, SEARCH_DEBOUNCE_MS);
    return () => { live = false; clearTimeout(t); };
  }, [open, projectId, q]);
  return q ? state : null;
}

export interface RecentAssetsSectionProps {
  headingId: string;
  projectName: string;
  state: RecentAssetsState;
  locked: boolean;
  lockedReason: string;
  highlight: string | null;
  onHover: (e: React.MouseEvent<HTMLElement>) => void;
  onAttach: (asset: AssetView) => void;
  /** Absent: no `View all` row (a host with no Projects_Surface to open). */
  onViewAll?: () => void;
  /** What the rows are, for the heading: `files` (default) or e.g. `images` → "Recent images in <project>". */
  noun?: string;
  /** Name search: shown when the host can search. A non-empty `query` replaces the recent rows with matches. */
  search?: { query: string; onQuery: (q: string) => void; state: RecentAssetsState | null } | null;
}

export function RecentAssetsSection({
  headingId,
  projectName,
  state,
  locked,
  lockedReason,
  highlight,
  onHover,
  onAttach,
  onViewAll,
  noun,
  search = null,
}: RecentAssetsSectionProps) {
  const searching = !!search && search.query.trim() !== '' && !!search.state;
  const shown = searching ? search!.state! : state;
  const things = noun ?? 'files';
  const Things = things[0].toUpperCase() + things.slice(1);
  return (
    <div role="group" aria-labelledby={headingId} data-plus-menu-section="recent">
      <div id={headingId} style={sectionHeadingStyle} title={`Recent ${noun ? `${noun} ` : ''}in ${projectName}`}>
        {searching ? `${Things} in ${projectName}` : `Recent ${noun ? `${noun} ` : ''}in ${projectName}`}
      </div>
      {search && (
        <div style={searchRowStyle} data-plus-menu-slot="asset-search">
          <input
            type="search"
            value={search.query}
            onChange={(e) => search.onQuery(e.target.value)}
            // The menu's roving keys move between items; inside the box, Home/End/Space edit the text.
            onKeyDown={(e) => { if (e.key === 'Home' || e.key === 'End' || e.key === ' ') e.stopPropagation(); }}
            placeholder={`Search ${projectName} by name`}
            aria-label={`Search files in ${projectName} by name`}
            data-testid="plus-menu-asset-search"
            style={searchInputStyle}
          />
        </div>
      )}
      {locked && shown.assets.length > 0 && <div style={noteStyle}>{lockedReason}</div>}
      {shown.status === 'loading' && shown.assets.length === 0 && (
        <div style={noteStyle} role="status">{searching ? 'Searching…' : `Loading ${things}…`}</div>
      )}
      {shown.status === 'error' && <div style={noteStyle} role="status">{Things} could not be loaded.</div>}
      {shown.status === 'ready' && shown.assets.length === 0 && (
        <div style={noteStyle} role="status">{searching ? `No ${things} match` : `No ${things} yet in ${projectName}`}</div>
      )}
      {shown.assets.map((a) => {
        const key = `recent:${a.assetId}`;
        const reason = locked ? lockedReason : NOT_ATTACHABLE[a.status] ?? null;
        const size = typeof a.sourceSize === 'number' ? formatBytes(a.sourceSize) : '';
        const badge = QUIET_STATUSES.has(a.status) ? null : statusLabel(a.status);
        const folder = typeof a.folder === 'string' ? a.folder.trim() : '';
        const label = [a.displayName, folder ? `in ${folder}` : null, size, badge, locked ? lockedReason : null]
          .filter(Boolean).join(', ');
        return (
          <button
            key={a.assetId}
            type="button"
            role="menuitem"
            aria-label={label}
            aria-disabled={reason ? true : undefined}
            data-menu-key={key}
            data-testid="plus-menu-recent-asset"
            title={reason ?? (folder ? `${folder}/${a.displayName}` : a.displayName)}
            style={rowStyle(highlight, key, !!reason)}
            onMouseEnter={onHover}
            onClick={() => {
              if (!reason) onAttach(a);
            }}
          >
            <span style={iconColumnStyle}><AssetKindIcon kind={a.kind} size={14} /></span>
            <span style={itemNameStyle}>{a.displayName}</span>
            {folder && <span style={folderStyle} data-testid="plus-menu-asset-folder">{folder}</span>}
            {size && <span style={metaStyle}>{size}</span>}
            {badge && <Badge variant={statusVariant(a.status)}>{badge}</Badge>}
          </button>
        );
      })}
      {onViewAll && <button
        type="button"
        role="menuitem"
        data-menu-key="view-all"
        style={rowStyle(highlight, 'view-all')}
        onMouseEnter={onHover}
        onClick={onViewAll}
      >
        <span style={iconColumnStyle}><FolderIcon size={14} /></span>
        <span style={itemNameStyle}>View all</span>
      </button>}
    </div>
  );
}

const searchRowStyle: React.CSSProperties = { padding: '4px 10px 6px' };
// Secondary text: shrinks (and ellipsizes) before the name does.
const folderStyle: React.CSSProperties = {
  ...metaStyle,
  flexShrink: 1,
  minWidth: 0,
  maxWidth: '45%',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
};
const searchInputStyle: React.CSSProperties = {
  width: '100%',
  boxSizing: 'border-box',
  padding: '5px 8px',
  fontSize: 'var(--fs-xs)',
  color: theme.colors.textPrimary,
  backgroundColor: theme.colors.background,
  border: `1px solid ${theme.colors.borderStrong}`,
  borderRadius: theme.radius.button,
  fontFamily: 'inherit',
};
