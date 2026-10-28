/**
 * One Project_Asset in the Projects_Surface's Assets grid: a thumbnail (an image's rendition or source URL from the
 * Asset_URL_Route; a kind icon otherwise), the Display_Name (its folder path in the tooltip), kind and size, and a
 * status badge only while the asset needs attention (queued, analyzing, indexing, failed, …; never for the resting
 * `stored` / `ready`), with a failure's reason on hover and behind `Details`. An `indexed` file shows a small green magnifier — the
 * same search mark a searchable Project carries after its name (SearchIndexedGlyph).
 *
 * Activating the card opens the asset: video, image, and audio in the existing asset player (`openAssetAt`, the
 * `lab:open-asset` path the citations use); a document's or a data file's source URL in a new tab. The overflow menu
 * holds Retry (failed assets) and Delete; the grid owns the confirmation. With `onSelectedChange`, a checkbox selects
 * the card for the grid's bulk delete: always mounted (keyboard reachable) but shown only on hover or focus, or on
 * every card while the grid is `selecting`.
 */
import React, { useCallback, useEffect, useId, useRef, useState } from 'react';
import { theme } from '../../styles/tokens';
import Badge from '../ui/Badge';
import MoreIcon from '../icons/MoreIcon';
import SearchIcon from '../icons/SearchIcon';
import { request as requestAssetUrls } from '../../projects/assetUrlBatcher';
import { openAssetAt } from '../../projects/openCitation';
import { formatBytes } from '../../projects/upload/kinds';
import { assetRole, type AssetView } from '../../projects/api';
import { ASSET_DRAG_TYPE } from './assetDrag';
import { AssetPreviewModal, type PreviewKind } from '../chat/AssetPreviewModal';
import { AssetIcon } from '../chat/AssetIcons';
import { MediaCard } from '../cards/MediaCard';
import { FileLocation } from './FileLocation';
import { findSimilar } from '../../projects/findSimilar';
import { downloadFile } from '../chat/artifactUrl';
import type { AssetSort, AssetSortKey } from './assetSort';
import { AssetKindIcon as KindIcon, KIND_LABEL, ROLE_LABEL, needsAttention, notIndexedReason, statusLabel, statusVariant } from './assetDisplay';

export { statusLabel };

/** A failed asset's reason as text: the `failure` string, or its `message` / `reason` / `code`. */
export function failureReason(asset: AssetView): string | null {
  const f = asset.failure ?? asset.failureReason;
  if (typeof f === 'string' && f.trim()) return f;
  if (f && typeof f === 'object') {
    const o = f as Record<string, unknown>;
    for (const k of ['message', 'reason', 'code']) {
      if (typeof o[k] === 'string' && (o[k] as string).trim()) return o[k] as string;
    }
  }
  return null;
}

export interface AssetCardProps {
  asset: AssetView;
  projectId: string;
  /** The chat session on screen; the asset player opens in its Renderer panel. */
  sessionId: string | null;
  /** Absent → no Delete (a browse-only grid); with no Retry either, the card has no actions menu. */
  onDelete?(asset: AssetView): void;
  /** Retry a `failed` asset (shown in the menu only for one). */
  onRetry?(asset: AssetView): void;
  /** Report an open that could not happen (the message names the asset). */
  onOpenError(message: string): void;
  /** `grid` (default): a tile. `list`: one row of a Finder-style list. */
  layout?: AssetLayout;
  /** A list row under a folder header: indented. */
  inFolder?: boolean;
  /** The folder on screen around this card: the card names its own folder only when it differs (a sub-folder's
   *  file). `null` = no folder on screen (a search across every folder), so it is always shown. Absent → never. */
  shownIn?: string | null;
  /** Selected for the grid's bulk delete. */
  selected?: boolean;
  /** At least one asset in the grid is selected: every card shows its checkbox. */
  selecting?: boolean;
  /** Absent → no checkbox (the card cannot be selected). */
  onSelectedChange?(asset: AssetView, selected: boolean, range?: boolean): void;
  /**
   * The asset ids a drag of this card carries (the whole selection when the card is part of it). Absent → the card
   * is not draggable. The payload is `ASSET_DRAG_TYPE`, so a folder accepts it and the upload drop zone ignores it.
   */
  dragIds?(asset: AssetView): string[];
  /** ⋯ → Move to folder… for this one file (the section opens its folder picker). */
  onMove?(asset: AssetView): void;
  /** ⋯ → Lineage: the Projects Lineage view focused on this file's chain. Absent where lineage is off. */
  onLineage?(asset: AssetView): void;
}

/** An asset's folder path, or `''` for the top level. */
export function assetFolder(asset: AssetView): string {
  return typeof asset.folder === 'string' ? asset.folder.trim() : '';
}

export type AssetLayout = 'grid' | 'list';

export default function AssetCard({
  asset, projectId, sessionId, onDelete, onRetry, onOpenError, layout = 'grid', inFolder = false, shownIn,
  selected = false, selecting = false, onSelectedChange, dragIds, onMove, onLineage,
}: AssetCardProps) {
  const { assetId, kind, displayName, status } = asset;
  const [thumb, setThumb] = useState<string | null>(null);
  const [thumbRetried, setThumbRetried] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [showReason, setShowReason] = useState(false);
  const [opening, setOpening] = useState(false);
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const reasonId = useId();
  const reason = status === 'failed' ? failureReason(asset) : null;

  const loadThumb = useCallback(async () => {
    const results = await requestAssetUrls(projectId, [assetId]);
    const out = results.get(assetId);
    // A video shows only its hero image (never the source: that is the whole video file).
    if (out?.ok) setThumb(out.entry.rendition?.url ?? (kind === 'image' ? out.entry.source?.url : undefined) ?? null);
    else setThumb(null);
  }, [projectId, assetId, kind]);

  useEffect(() => {
    if (kind !== 'image' && kind !== 'video') return;
    let live = true;
    void requestAssetUrls(projectId, [assetId]).then((results) => {
      if (!live) return;
      const out = results.get(assetId);
      if (out?.ok) setThumb(out.entry.rendition?.url ?? (kind === 'image' ? out.entry.source?.url : undefined) ?? null);
    });
    return () => {
      live = false;
    };
  }, [kind, projectId, assetId, status]);

  // The project file standard, the same as a file in chat: click → the preview (a modal over the grid); its icons
  // are Preview, Download and Open in side panel. The side panel is where a file is worked on.
  const [preview, setPreview] = useState<{ src: string | null; error: string | null } | null>(null);
  const previewKind: PreviewKind = kind === 'video' || kind === 'image' || kind === 'audio' ? kind : 'document';
  const sourceUrl = async (): Promise<string | null> => {
    const results = await requestAssetUrls(projectId, [assetId]);
    const out = results.get(assetId);
    return out?.ok ? out.entry.source?.url ?? null : null;
  };
  const openPreview = async () => {
    setPreview({ src: null, error: null });
    const url = await sourceUrl();
    setPreview(url ? { src: url, error: null } : { src: null, error: `Can't open ${displayName}: it could not be loaded.` });
  };
  const download = async () => {
    const url = preview?.src ?? await sourceUrl();
    if (url) await downloadFile(url, displayName);
    else onOpenError(`Can't download ${displayName}: it could not be loaded.`);
  };

  const open = async () => {
    if (opening) return;
    setOpening(true);
    try {
      // A data file (JSON, CSV) has no player: it opens in a browser tab. Everything else opens in the side panel.
      if (kind === 'data') {
        // Open the tab now (a later window.open is a blocked popup), then point it at the fresh URL.
        const win = typeof window.open === 'function' ? window.open('', '_blank') : null;
        const results = await requestAssetUrls(projectId, [assetId]);
        const out = results.get(assetId);
        const url = out?.ok ? out.entry.source?.url : undefined;
        if (!url) {
          win?.close();
          onOpenError(
            out && !out.ok && out.kind === 'not_found'
              ? `Can't open ${displayName}: you no longer have access to this project.`
              : `Can't open ${displayName}: it could not be loaded.`,
          );
          return;
        }
        if (win) {
          win.opener = null;
          win.location.href = url;
        }
        return;
      }
      const r = await openAssetAt(
        { ref: { project_id: projectId, asset_id: assetId, frame_s3_key: null }, kind: kind as 'video' | 'image' | 'audio' | 'document', displayName, startMs: 0 },
        sessionId ?? '',
      );
      if (!r.ok) onOpenError(r.message);
    } finally {
      setOpening(false);
    }
  };

  const size = typeof asset.sourceSize === 'number' ? formatBytes(asset.sourceSize) : '';
  const roleLabel = ROLE_LABEL[assetRole(asset)] ?? '';
  const kindLabel = KIND_LABEL[kind] ?? kind;
  const meta = [kindLabel, size, roleLabel].filter(Boolean).join(' · ');
  const list = layout === 'list';
  // Where a file sits lives in the tooltip only: a visible path line made cards uneven heights.
  const folder = assetFolder(asset);
  const tooltip = folder ? `${folder}/${displayName}` : displayName;
  // The checkbox stays mounted (so Tab reaches it) but only shows on hover/focus, or for every card once anything is
  // selected.
  const showCheckbox = selecting || selected || hovered || focused;
  const hoverProps = {
    onMouseEnter: () => setHovered(true),
    onMouseLeave: () => setHovered(false),
    onFocus: () => setFocused(true),
    onBlur: (e: React.FocusEvent<HTMLLIElement>) => {
      if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setFocused(false);
    },
  };

  const thumbEl = (
    <span style={list ? rowThumbStyle : thumbStyle}>
      {thumb ? (
        <img
          src={thumb}
          alt=""
          style={imgStyle}
          onError={() => {
            // A presigned URL can expire while the card sits on screen: ask once for a fresh one.
            if (thumbRetried) {
              setThumb(null);
              return;
            }
            setThumbRetried(true);
            void loadThumb();
          }}
        />
      ) : (
        <span style={kindIconStyle} data-testid="asset-kind-icon"><KindIcon kind={kind} size={list ? 18 : 28} /></span>
      )}
    </span>
  );

  // A stored file a run deliberately left unindexed (audio with no speech) says so, neutrally -- never `Failed`.
  const notIndexed = notIndexedReason(asset);
  // A state that needs attention gets a badge. An INDEXED file gets a quiet green magnifier instead (the project-name search mark) — "this one is done and
  // searchable" was field feedback: without it a finished file looked the same as one never analysed.
  const badgeEl = status === 'indexed' && !notIndexed ? (
    <span style={indexedCheckStyle} title="Indexed — searchable in this project" aria-label="Indexed" role="img" data-testid="asset-indexed">
      <SearchIcon size={14} />
    </span>
  ) : notIndexed ? (
    <Badge variant="neutral" title={notIndexed} style={{ flexShrink: 0 }}>
      Not indexed
    </Badge>
  ) : needsAttention(status) ? (
    <>
      <Badge variant={statusVariant(status)} title={reason ?? undefined} style={{ flexShrink: 0 }}>
        {statusLabel(status)}
      </Badge>
      {reason && (
        <button
          type="button"
          style={linkBtnStyle}
          aria-expanded={showReason}
          aria-controls={reasonId}
          onClick={() => setShowReason((v) => !v)}
        >
          {showReason ? 'Hide details' : 'Details'}
        </button>
      )}
    </>
  ) : null;

  const hasActions = !!onDelete || !!onLineage || (status === 'failed' && !!onRetry);
  const menuEl = !hasActions ? null : (
    <span style={{ position: 'relative', display: 'inline-flex', flexShrink: 0 }}>
      <button
        type="button"
        style={moreBtnStyle}
        aria-label={`Actions for ${displayName}`}
        aria-haspopup="menu"
        aria-expanded={menuOpen}
        onClick={() => setMenuOpen((v) => !v)}
      >
        <MoreIcon size={14} />
      </button>
      {menuOpen && (
        <>
          <span style={scrimStyle} aria-hidden="true" onClick={() => setMenuOpen(false)} />
          <div
            role="menu"
            aria-label={`Actions for ${displayName}`}
            style={menuStyle}
            onKeyDown={(e) => {
              if (e.key === 'Escape') setMenuOpen(false);
            }}
          >
            {onLineage && (
              <button
                type="button"
                role="menuitem"
                style={menuItemStyle}
                onClick={() => {
                  setMenuOpen(false);
                  onLineage(asset);
                }}
              >
                Lineage
              </button>
            )}
            {status === 'failed' && onRetry && (
              <button
                type="button"
                role="menuitem"
                style={menuItemStyle}
                autoFocus
                onClick={() => {
                  setMenuOpen(false);
                  onRetry(asset);
                }}
              >
                Retry
              </button>
            )}
            {onDelete && (
              <button
                type="button"
                role="menuitem"
                style={{ ...menuItemStyle, color: theme.colors.error }}
                autoFocus={!(status === 'failed' && onRetry)}
                onClick={() => {
                  setMenuOpen(false);
                  onDelete(asset);
                }}
              >
                Delete
              </button>
            )}
          </div>
        </>
      )}
    </span>
  );

  const reasonEl = reason && showReason ? <p id={reasonId} style={reasonStyle}>{reason}</p> : null;

  const checkboxBase = list ? rowCheckboxStyle : cardCheckboxStyle;
  const shiftPick = useRef(false);
  const checkboxEl = onSelectedChange ? (
    <input
      type="checkbox"
      checked={selected}
      onClick={(e) => { shiftPick.current = e.shiftKey; }}
      onChange={(e) => { onSelectedChange(asset, e.target.checked, shiftPick.current); shiftPick.current = false; }}
      aria-label={`Select ${displayName}`}
      style={showCheckbox ? checkboxBase : { ...checkboxBase, opacity: 0 }}
      data-testid="asset-select"
      data-visible={showCheckbox ? 'true' : 'false'}
    />
  ) : null;

  // Preview · Download · Open in side panel: the chat card's three actions, with its icons, in its order.
  const iconBtn = (label: string, title: string, icon: 'expand' | 'download' | 'panel', onClick: () => void, testId: string) => (
    <button type="button" style={actionIconStyle} onClick={onClick} aria-label={label} title={title} data-testid={testId}>
      <AssetIcon kind={icon} size={14} />
    </button>
  );
  const actionIcons = (
    <span style={actionIconsStyle}>
      {iconBtn(`Preview ${displayName}`, 'Preview', 'expand', () => void openPreview(), 'asset-preview-btn')}
      {iconBtn(`Download ${displayName}`, 'Download', 'download', () => void download(), 'asset-download-btn')}
      {iconBtn(`Open ${displayName} in the side panel`, 'Open in side panel', 'panel', () => void open(), 'asset-panel-btn')}
    </span>
  );
  const previewEl = preview ? (
    <AssetPreviewModal kind={previewKind} src={preview.src} name={displayName} sub={meta} error={preview.error} projectId={projectId}
      moveTarget={onMove ? { projectId, assetId } : undefined}
      onClose={() => setPreview(null)} onOpenInPanel={() => void open()} onDownload={() => void download()} />
  ) : null;

  const dragProps = dragIds ? {
    draggable: true,
    onDragStart: (e: React.DragEvent) => {
      const ids = dragIds(asset);
      e.dataTransfer.setData(ASSET_DRAG_TYPE, JSON.stringify(ids));
      e.dataTransfer.setData('text/plain', ids.length > 1 ? `${ids.length} files` : displayName);
      e.dataTransfer.effectAllowed = 'move';
    },
  } : {};

  if (list) {
    // One row: thumbnail + name (the open target), kind, size, status (only when it needs attention), actions.
    return (
      <li
        style={inFolder ? { ...rowStyle, paddingLeft: 22 } : rowStyle}
        data-testid="asset-card"
        data-asset-id={assetId}
        data-status={status}
        data-layout="list"
        data-selected={selected || undefined}
        {...hoverProps}
        {...dragProps}
      >
        <div style={rowMainStyle}>
          {checkboxEl}
          <button
            type="button"
            style={rowOpenBtnStyle}
            onClick={() => void openPreview()}
            aria-label={`Preview ${displayName}`}
          >
            {thumbEl}
            <span style={rowNameStyle} title={tooltip} data-testid="asset-name">{displayName}</span>
          </button>
          {shownIn !== undefined && shownIn !== assetFolder(asset) && (
            <span style={rowLocationStyle}><FileLocation projectId={projectId} folder={assetFolder(asset)} /></span>
          )}
          <span style={rowKindStyle}>{roleLabel ? `${kindLabel} · ${roleLabel}` : kindLabel}</span>
          <span style={rowSizeStyle}>{size}</span>
          <span style={rowStatusStyle}>{badgeEl}</span>
          {actionIcons}
          {menuEl}
        </div>
        {reasonEl}
        {previewEl}
      </li>
    );
  }

  // A tile: the ONE card (MediaCard) — the same frame, lines and icons as a chat file or a moment. Preview ·
  // Download · Open in side panel; ⋯ → Move to folder, Find similar, Retry, Delete.
  const indexed = status === 'indexed' && !notIndexed;
  const statusBadge = indexed ? null : badgeEl;
  const menu = [
    ...(onMove ? [{ label: 'Move to folder…', icon: 'folder' as const, onClick: () => onMove(asset) }] : []),
    ...(onLineage ? [{ label: 'Lineage', onClick: () => onLineage(asset) }] : []),
    ...(sessionId && (kind === 'video' || kind === 'image') ? [{
      label: 'Find similar', icon: 'expand' as const,
      onClick: () => findSimilar({ ref: { project_id: projectId, asset_id: assetId, frame_s3_key: null }, kind,
        display_name: displayName, start_ms: 0, end_ms: 0 } as never, sessionId),
    }] : []),
    ...(status === 'failed' && onRetry ? [{ label: 'Retry', onClick: () => onRetry(asset) }] : []),
    ...(onDelete ? [{ label: 'Delete', danger: true, onClick: () => onDelete(asset) }] : []),
  ];
  return (
    <MediaCard testId="asset-card" kind={kind === 'data' ? 'data' : kind} title={displayName} titleTip={tooltip}
      dataAttrs={{ 'data-asset-id': assetId, 'data-status': status, 'data-layout': 'grid', 'data-selected': selected ? 'true' : undefined }}
      thumb={thumb} contain={kind === 'image'}
      meta={<>{meta}{statusBadge}</>} indexed={indexed}
      location={shownIn !== undefined ? <FileLocation projectId={projectId} folder={assetFolder(asset)} hideIn={shownIn ?? undefined} /> : undefined}
      onPreview={() => void openPreview()}
      primary={{ label: `Download ${displayName}`, icon: 'download', onClick: () => void download() }}
      onOpenInPanel={() => void open()}
      menu={menu}
      selected={selected} selecting={selecting}
      onSelectedChange={onSelectedChange ? (on, range) => onSelectedChange(asset, on, range) : undefined}
      onDragStart={dragProps.onDragStart}>
      {reasonEl}
      {previewEl}
    </MediaCard>
  );
}

// ------------------------------------------------------------------------------------------ styles

const actionIconsStyle: React.CSSProperties = { display: 'inline-flex', gap: 0, flexShrink: 0 };
const actionIconStyle: React.CSSProperties = {
  width: 24, height: 24, display: 'grid', placeItems: 'center', padding: 0, border: 'none', background: 'none',
  borderRadius: theme.radius.button, color: theme.colors.textSecondary, cursor: 'pointer',
};

/**
 * The List view's column header (Name · Type · Size), drawn with the SAME geometry as a row -- the row's
 * padding and gap, the checkbox and actions slots when rows have them, and the Type / Size / Status widths --
 * so the labels sit over their columns. Each label is a sort button (asc → desc → default, `assetSort.ts`)
 * whose accessible name states the current sort; the active one shows ▲ / ▼. (The list is a `<ul>`, not a
 * table, so there is no columnheader / aria-sort.)
 */
export function AssetListHeader({ sort, onSort, withCheckbox, withMenu }: {
  sort: AssetSort | null;
  onSort: (key: AssetSortKey) => void;
  withCheckbox: boolean;
  withMenu: boolean;
}) {
  const col = (key: AssetSortKey, label: string, style: React.CSSProperties) => {
    const active = sort?.key === key ? sort.dir : null;
    const next = active === 'asc' ? 'descending' : active === 'desc' ? 'the default order' : 'ascending';
    const now = active === 'asc' ? ', sorted ascending' : active === 'desc' ? ', sorted descending' : '';
    return (
      <span style={style}>
        <button
          type="button"
          data-testid={`assets-sort-${key}`}
          data-sort={active ?? undefined}
          onClick={() => onSort(key)}
          aria-label={`${label}${now}. Sort ${next}`}
          title={`Sort by ${label.toLowerCase()}`}
          style={{
            ...headerBtnStyle,
            justifyContent: style.textAlign === 'right' ? 'flex-end' : 'flex-start',
            color: active ? theme.colors.textPrimary : theme.colors.textSecondary,
          }}
        >
          {label}
          <span aria-hidden="true" style={{ fontSize: 9, width: 8, opacity: active ? 1 : 0 }}>{active === 'desc' ? '▼' : '▲'}</span>
        </button>
      </span>
    );
  };
  return (
    <div data-testid="assets-list-header" style={{ ...rowMainStyle, ...headerRowStyle }}>
      {withCheckbox && <span aria-hidden="true" style={{ width: rowCheckboxStyle.width, flexShrink: 0 }} />}
      {/* The name column starts where the row's NAME starts: past the 36px thumbnail and its 10px gap. */}
      {col('name', 'Name', { flex: 1, minWidth: 0, paddingLeft: 46 })}
      {col('type', 'Type', { ...rowKindStyle, color: undefined })}
      {col('size', 'Size', { ...rowSizeStyle, color: undefined })}
      <span aria-hidden="true" style={{ width: rowStatusStyle.width, flexShrink: 0 }} />
      {withMenu && <span aria-hidden="true" style={{ width: moreBtnStyle.width, flexShrink: 0 }} />}
    </div>
  );
}

const headerRowStyle: React.CSSProperties = {
  position: 'sticky', top: 0, zIndex: 1, background: theme.colors.background,
  borderBottom: `1px solid ${theme.colors.border}`, paddingTop: 4, paddingBottom: 4,
};
const headerBtnStyle: React.CSSProperties = {
  display: 'inline-flex', alignItems: 'center', gap: 4, width: '100%', padding: '2px 0', margin: 0, border: 'none',
  background: 'none', cursor: 'pointer', fontFamily: 'inherit', fontSize: 'var(--fs-xs)',
  fontWeight: theme.typography.weights.semibold, letterSpacing: '0.02em',
};

const rowStyle: React.CSSProperties = {
  minWidth: 0,
  borderBottom: `1px solid ${theme.colors.border}`,
};
const rowMainStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 12,
  padding: '6px 6px 6px 4px',
  minWidth: 0,
};
const rowOpenBtnStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 10,
  flex: 1,
  minWidth: 0,
  padding: 0,
  margin: 0,
  border: 'none',
  background: 'none',
  textAlign: 'left',
  cursor: 'pointer',
  color: theme.colors.textPrimary,
  fontFamily: 'inherit',
};
const rowThumbStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  width: 36,
  height: 36,
  flexShrink: 0,
  background: theme.colors.surfaceSubtle,
  borderRadius: theme.radius.button,
  overflow: 'hidden',
};
const rowNameStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  fontSize: 'var(--fs-sm)',
  fontWeight: theme.typography.weights.medium,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
};
const rowKindStyle: React.CSSProperties = {
  width: 80,
  flexShrink: 0,
  fontSize: 'var(--fs-xs)',
  color: theme.colors.textSecondary,
};
const rowSizeStyle: React.CSSProperties = {
  width: 72,
  flexShrink: 0,
  textAlign: 'right',
  fontSize: 'var(--fs-xs)',
  color: theme.colors.textSecondary,
  fontVariantNumeric: 'tabular-nums',
};
const rowStatusStyle: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: 6,
  width: 130,
  flexShrink: 0,
};

const cardStyle: React.CSSProperties = {
  position: 'relative',
  display: 'flex',
  flexDirection: 'column',
  minWidth: 0,
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.panel,
  background: theme.colors.surface,
  overflow: 'visible',
};
const cardSelectedStyle: React.CSSProperties = { borderColor: theme.colors.primary };
/** Over the thumbnail's top-left corner, above the open button. */
const cardCheckboxStyle: React.CSSProperties = {
  position: 'absolute',
  top: 6,
  left: 6,
  zIndex: 1,
  width: 16,
  height: 16,
  margin: 0,
  cursor: 'pointer',
  accentColor: theme.colors.primary,
  transition: `opacity ${theme.transitions.hover}`,
};
const rowCheckboxStyle: React.CSSProperties = {
  width: 16,
  height: 16,
  margin: 0,
  flexShrink: 0,
  cursor: 'pointer',
  accentColor: theme.colors.primary,
  transition: `opacity ${theme.transitions.hover}`,
};
const openBtnStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  alignItems: 'stretch',
  gap: 0,
  padding: 0,
  margin: 0,
  border: 'none',
  background: 'none',
  textAlign: 'left',
  cursor: 'pointer',
  color: theme.colors.textPrimary,
  fontFamily: 'inherit',
  minWidth: 0,
};
const thumbStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  height: 110,
  background: theme.colors.surfaceSubtle,
  borderTopLeftRadius: theme.radius.panel,
  borderTopRightRadius: theme.radius.panel,
  overflow: 'hidden',
};
const imgStyle: React.CSSProperties = { width: '100%', height: '100%', objectFit: 'cover', display: 'block' };
const kindIconStyle: React.CSSProperties = { display: 'inline-flex', color: theme.colors.textTertiary };
const nameStyle: React.CSSProperties = {
  padding: '8px 10px 0',
  fontSize: 'var(--fs-sm)',
  fontWeight: theme.typography.weights.medium,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
};
const metaStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  fontSize: 'var(--fs-2xs)',
  color: theme.colors.textSecondary,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
};
const indexedCheckStyle: React.CSSProperties = {
  display: 'inline-flex', alignItems: 'center', flexShrink: 0, color: theme.colors.success,
};

/** One fixed-height line (the actions button's height), badge or not, so every tile is the same height. */
const footerStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
  height: 26,
  padding: '2px 4px 6px 10px',
  boxSizing: 'content-box',
};
const reasonStyle: React.CSSProperties = {
  margin: 0,
  padding: '0 10px 8px',
  fontSize: 'var(--fs-2xs)',
  color: theme.colors.error,
  overflowWrap: 'anywhere',
};
const linkBtnStyle: React.CSSProperties = {
  flexShrink: 0,
  whiteSpace: 'nowrap',
  background: 'none',
  border: 'none',
  padding: 0,
  color: theme.colors.primary,
  cursor: 'pointer',
  fontFamily: 'inherit',
  fontSize: 'var(--fs-2xs)',
};
const moreBtnStyle: React.CSSProperties = {
  width: 26,
  height: 26,
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  padding: 0,
  border: `1px solid transparent`,
  borderRadius: theme.radius.button,
  background: 'none',
  color: theme.colors.textSecondary,
  cursor: 'pointer',
};
const scrimStyle: React.CSSProperties = { position: 'fixed', inset: 0, zIndex: theme.zIndex.popover };
const menuStyle: React.CSSProperties = {
  position: 'absolute',
  top: 'calc(100% + 4px)',
  right: 0,
  zIndex: theme.zIndex.popover + 1,
  minWidth: 140,
  background: theme.colors.surfaceRaised,
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.panel,
  boxShadow: theme.elevation.elevated,
  padding: 4,
};
const menuItemStyle: React.CSSProperties = {
  display: 'flex',
  width: '100%',
  padding: '6px 8px',
  background: 'none',
  border: 'none',
  borderRadius: theme.radius.button,
  textAlign: 'left',
  cursor: 'pointer',
  fontFamily: 'inherit',
  fontSize: 'var(--fs-sm)',
};
const rowLocationStyle: React.CSSProperties = { flex: '0 1 220px', minWidth: 0, display: 'flex' };
