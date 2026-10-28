/**
 * MediaCard — the ONE card for every item the app shows: a file in chat, a file in a project, a moment, a saved
 * moment. Same frame, same two lines, same icons in the same order, everywhere:
 *
 *   picture (16:9)   — the thumbnail, a video's first frame, or a kind tile. Click = Preview. A video shows its
 *                      duration bottom-right; a moment its range bottom-left and its length bottom-right.
 *   title · meta     — the name, then one line of facts ("Video · 1280×720 · 2.3 MB", or a status badge).
 *   description      — optional, one line (a moment's matched text).
 *   icons            — Preview · <the one action this kind is for> · Open in side panel · ⋯ (everything else).
 *
 * No inline players or scrubbers: the preview plays, the side panel edits. Selection (a checkbox, top-left) and
 * drag are opt-in. Theme tokens only, so Light, Dark and KiroCrew follow.
 */
import React, { useEffect, useId, useRef, useState } from 'react';
import { theme } from '../../styles/tokens';
import { AssetIcon, type AssetIconKind } from '../chat/AssetIcons';

export type CardKind = 'video' | 'image' | 'audio' | 'document' | 'data';

export interface CardAction {
  label: string;
  /** The icon; omitted for a plain text menu item. */
  icon?: AssetIconKind;
  onClick: () => void;
  danger?: boolean;
  disabled?: boolean;
  /** Why it is disabled (tooltip). */
  reason?: string;
}

export interface MediaCardProps {
  kind: CardKind;
  title: string;
  /** Tooltip for the title (e.g. its folder path). */
  titleTip?: string;
  /** One line under the title: facts, or a status badge. */
  meta?: React.ReactNode;
  /** An optional one-line description (a moment's matched text). */
  description?: React.ReactNode;
  /** An image URL for the picture. */
  thumb?: string | null;
  /** A video URL whose first frame (or the frame at `posterAt` seconds) is the picture. */
  videoSrc?: string | null;
  posterAt?: number;
  /** Fit the picture whole (a frame strip, a vertical crop) instead of filling the box. */
  contain?: boolean;
  /**
   * An image at its OWN aspect ratio, never cropped (chat: a wide or tall image read badly cut to 16:9). It fits a
   * box as wide as the container and {@link FIT_MAX_HEIGHT_PX} tall, by whichever side is longer, and is never
   * enlarged past its own size; the card shrinks to the picture. Only while there is a `thumb` — the loading
   * skeleton keeps the 16:9 box.
   */
  fitWidth?: boolean;
  /** Bottom-right badge: a duration, a moment's length. */
  badge?: string;
  /** Bottom-left badge: a moment's range. */
  badgeLeft?: string;
  /** A status line drawn over the picture (loading, no access, failed). */
  overlay?: React.ReactNode;
  /** A small element at the picture's top-right corner (a multi-image search: the image(s) that found it). */
  corner?: React.ReactNode;
  /** Indexed and searchable in its project: a green dot before the meta line. */
  indexed?: boolean;

  onPreview: () => void;
  /** The one action this kind is for (Download for a file, Cut for a moment), between Preview and Side panel. */
  primary?: CardAction;
  onOpenInPanel?: () => void;
  /** Accessible names when the defaults ("Preview <title>", "Open <title> in the side panel") need more (a range). */
  previewLabel?: string;
  panelLabel?: string;
  /** Everything else, in the ⋯ menu. A trailing note (match type, score) renders muted at the bottom. */
  menu?: CardAction[];
  menuNote?: React.ReactNode;

  selected?: boolean;
  /** Absent → no checkbox. `range`: Shift was held (select everything since the last one picked). */
  onSelectedChange?: (selected: boolean, range?: boolean) => void;
  /** Something is selected: the checkbox shows without hover, and a click on the picture selects instead of
   *  previewing (the Preview icon still previews). */
  selecting?: boolean;
  /** Drag payload; absent → not draggable. */
  onDragStart?: (e: React.DragEvent) => void;
  /** Rendered after the card (a portaled preview, an inline error). */
  children?: React.ReactNode;
  testId?: string;
  dataAttrs?: Record<string, string | undefined>;
  /** `li` in a grid (default); `span` inside chat text, where a list item or a div would be invalid. */
  as?: 'li' | 'span';
  /** What the picture reports once loaded (a video's size and length, an image's size). */
  onMediaMeta?: (meta: { width?: number; height?: number; duration?: number }) => void;
  /** The picture failed to load (an expired URL): the owner refreshes or gives up. */
  onMediaError?: () => void;
  /** Ring the card like a selected one without selecting it (the moment Play all is on). */
  highlight?: boolean;
  /** Where the file is filed (a {@link FileLocation} line), under the description. */
  location?: React.ReactNode;
  /** Replaces the picture entirely (an untrusted image's "Load image" consent tile). */
  pictureOverride?: React.ReactNode;
}

export function MediaCard(p: MediaCardProps) {
  const [hover, setHover] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const menuId = useId();
  const moreRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!menuOpen) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') { setMenuOpen(false); moreRef.current?.focus(); } };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [menuOpen]);
  const showCheck = !!p.onSelectedChange && (p.selecting || p.selected || hover);
  const shiftRef = useRef(false);
  const pictureClick = (e: React.MouseEvent) => {
    if (p.selecting && p.onSelectedChange) p.onSelectedChange(!p.selected, e.shiftKey);
    else p.onPreview();
  };
  // The icons show on hover / keyboard focus (and always on touch), so at rest the name and meta get the whole
  // line. Opacity only: hidden icons stay in the tab order, and focusing one reveals them all.
  const showActs = hover || menuOpen || NO_HOVER;

  let picture: React.ReactNode;
  const fitWidth = !!p.fitWidth && !p.pictureOverride && !!p.thumb;
  if (p.pictureOverride) {
    picture = p.pictureOverride;
  } else if (p.thumb) {
    picture = <img src={p.thumb} alt={p.title} loading="lazy"
      style={fitWidth ? fitWidthImgStyle : { ...fillStyle, objectFit: p.contain ? 'contain' : 'cover' }}
      onLoad={(e) => p.onMediaMeta?.({ width: e.currentTarget.naturalWidth, height: e.currentTarget.naturalHeight })}
      onError={p.onMediaError} />;
  } else if (p.videoSrc) {
    // The frame at `posterAt` (#t) is the poster; nothing plays on the card. No crossOrigin: playback needs no CORS.
    const at = Math.max(0.1, p.posterAt ?? 0.1);
    picture = (
      <video src={`${p.videoSrc.split('#')[0]}#t=${at}`} preload="metadata" muted playsInline tabIndex={-1} aria-hidden="true"
        style={{ ...fillStyle, objectFit: p.contain ? 'contain' : 'cover' }}
        onLoadedMetadata={(e) => {
          const v = e.currentTarget;
          p.onMediaMeta?.({ width: v.videoWidth || undefined, height: v.videoHeight || undefined,
            duration: Number.isFinite(v.duration) ? v.duration : undefined });
        }}
        onError={p.onMediaError} />
    );
  } else {
    picture = <span style={tileStyle} aria-hidden="true" data-testid="card-kind-icon"><AssetIcon kind={tileIcon(p.kind)} size={30} /></span>;
  }

  const Root = (p.as ?? 'li') as 'li';
  return (
    // An open menu lifts the whole card: the icon row is transformed (its own stacking context), so the menu's
    // z-index alone cannot clear the next row of cards, which painted over it.
    <Root style={{ ...(p.selected || p.highlight ? { ...cardStyle, ...cardSelStyle } : cardStyle), ...(p.as === 'span' ? spanRootStyle : null),
      ...(fitWidth && p.as === 'span' ? spanFitWidthStyle : null),
      ...(menuOpen ? { zIndex: theme.zIndex.popover } : null) }}
      data-testid={p.testId ?? 'media-card'} data-kind={p.kind} data-fit={fitWidth ? 'width' : undefined}
      {...p.dataAttrs} onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}
      onFocus={() => setHover(true)} onBlur={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setHover(false); }}
      draggable={!!p.onDragStart} onDragStart={p.onDragStart}>
      <span style={{ ...mediaStyle, ...(fitWidth ? mediaFitWidthStyle : null),
        ...(p.selecting && p.onSelectedChange ? { cursor: 'pointer' } : null) }} onClick={pictureClick}
        data-card-media="">
        {picture}
        {(p.kind === 'video' || p.videoSrc) && !p.overlay && <span style={playStyle} aria-hidden="true"><span style={playGlyphStyle} /></span>}
        {p.overlay && <span style={overlayStyle}>{p.overlay}</span>}
        {p.badgeLeft && <span style={{ ...badgeStyle, left: 8, right: 'auto' }} data-testid="card-badge-left">{p.badgeLeft}</span>}
        {p.badge && <span style={badgeStyle} data-testid="card-badge">{p.badge}</span>}
        {p.corner && <span style={cornerStyle}>{p.corner}</span>}
      </span>
      {p.onSelectedChange && (
        <input type="checkbox" checked={!!p.selected} onClick={(e) => { shiftRef.current = e.shiftKey; }}
          onChange={(e) => { p.onSelectedChange!(e.target.checked, shiftRef.current); shiftRef.current = false; }}
          aria-label={`Select ${p.title}`} data-testid="card-select" data-visible={showCheck ? 'true' : 'false'}
          style={showCheck ? checkStyle : { ...checkStyle, opacity: 0 }} />
      )}
      <span style={footStyle}>
        <span style={metaColStyle}>
          <span style={titleStyle} title={p.titleTip ?? p.title} data-testid="card-title">{p.title}</span>
          {p.meta !== undefined && (
            <span style={metaStyle}>
              {p.indexed && <span style={dotStyle} role="img" aria-label="Indexed" title="Indexed — searchable in this project" data-testid="card-indexed" />}
              {p.meta}
            </span>
          )}
          {p.description && <span style={descStyle}>{p.description}</span>}
          {p.location}
        </span>
        <span style={showActs ? actsStyle : { ...actsStyle, opacity: 0, pointerEvents: 'none' }} data-card-acts=""
          data-visible={showActs ? 'true' : 'false'}>
          <IconButton label={p.previewLabel ?? `Preview ${p.title}`} tip="Preview" icon="expand" onClick={p.onPreview} />
          {p.primary && <IconButton label={p.primary.label} tip={p.primary.reason ?? p.primary.label} icon={p.primary.icon ?? 'expand'}
            onClick={p.primary.onClick} disabled={p.primary.disabled} />}
          {p.onOpenInPanel && <IconButton label={p.panelLabel ?? `Open ${p.title} in the side panel`} tip="Open in side panel" icon="panel" onClick={p.onOpenInPanel} />}
          {(p.menu?.length || p.menuNote) ? (
            <span style={{ position: 'relative', display: 'inline-flex' }}>
              <button ref={moreRef} type="button" style={menuOpen ? { ...iconBtnStyle, ...iconBtnOnStyle } : iconBtnStyle}
                aria-label={`More for ${p.title}`} title="More" aria-haspopup="menu" aria-expanded={menuOpen}
                aria-controls={menuOpen ? menuId : undefined} onClick={() => setMenuOpen((v) => !v)}>
                <MoreDots />
              </button>
              {menuOpen && (
                <>
                  <span style={scrimStyle} aria-hidden="true" onMouseDown={() => setMenuOpen(false)} />
                  <span id={menuId} role="menu" aria-label={`More for ${p.title}`} style={menuStyle}>
                    {p.menu?.map((a) => (
                      <button key={a.label} type="button" role="menuitem" disabled={a.disabled} title={a.reason}
                        style={{ ...menuItemStyle, ...(a.danger ? { color: theme.colors.error } : null), ...(a.disabled ? { opacity: 0.5, cursor: 'default' } : null) }}
                        onClick={() => { setMenuOpen(false); a.onClick(); }}>
                        {a.icon && <AssetIcon kind={a.icon} size={14} />}
                        {a.label}
                      </button>
                    ))}
                    {p.menuNote && <span style={menuNoteStyle}>{p.menuNote}</span>}
                  </span>
                </>
              )}
            </span>
          ) : null}
        </span>
      </span>
      {p.children}
    </Root>
  );
}

function IconButton({ label, tip, icon, onClick, disabled }: { label: string; tip: string; icon: AssetIconKind; onClick: () => void; disabled?: boolean }) {
  return (
    <button type="button" style={disabled ? { ...iconBtnStyle, opacity: 0.45, cursor: 'default' } : iconBtnStyle}
      onClick={disabled ? undefined : onClick} aria-label={label} aria-disabled={disabled || undefined} title={tip}>
      <AssetIcon kind={icon} size={15} />
    </button>
  );
}

function MoreDots() {
  return (
    <svg width={15} height={15} viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <circle cx="5" cy="12" r="1.6" /><circle cx="12" cy="12" r="1.6" /><circle cx="19" cy="12" r="1.6" />
    </svg>
  );
}

const tileIcon = (k: CardKind): AssetIconKind => (k === 'audio' ? 'audio' : k === 'image' ? 'image' : k === 'video' ? 'video' : 'page');

/** `m:ss` (or `h:mm:ss`) for a length in seconds. */
export function formatDuration(seconds: number): string {
  const t = Math.max(0, Math.round(seconds));
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`;
}

/** A moment's length: `43s` under a minute, else `m:ss`. */
export function formatSpan(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  return s < 60 ? `${s}s` : formatDuration(s);
}

const cardStyle: React.CSSProperties = {
  position: 'relative', listStyle: 'none', display: 'flex', flexDirection: 'column', minWidth: 0,
  border: `1px solid ${theme.colors.border}`, borderRadius: theme.radius.panel, background: theme.colors.surface,
};
// Inline in chat: a block of its own (never on the sentence's line), at most 380px wide.
const spanRootStyle: React.CSSProperties = { width: 'min(100%, 380px)', margin: '8px 0 12px', verticalAlign: 'top' };
const cardSelStyle: React.CSSProperties = { borderColor: theme.colors.primary, boxShadow: `0 0 0 2px ${theme.colors.primarySubtle}` };
const mediaStyle: React.CSSProperties = {
  position: 'relative', display: 'block', aspectRatio: '16 / 9', overflow: 'hidden', cursor: 'zoom-in',
  background: theme.colors.surfaceSubtle, borderRadius: `${theme.radius.panel}px ${theme.radius.panel}px 0 0`,
};
const fillStyle: React.CSSProperties = { position: 'absolute', inset: 0, width: '100%', height: '100%', display: 'block' };
// `fitWidth`: the picture sets the box (no 16:9 frame) and is never cropped. It is scaled down to fit the container's
// width and FIT_MAX_HEIGHT_PX, so the longer side decides: a wide strip spans the bubble, a portrait stops at the
// height cap. Auto width/height with both maxima keep the aspect ratio, and nothing is enlarged past its natural size
// (a 204×271 face crop used to be blown up to the bubble's full width).
export const FIT_MAX_HEIGHT_PX = 380;
const mediaFitWidthStyle: React.CSSProperties = { aspectRatio: 'auto' };
const fitWidthImgStyle: React.CSSProperties = {
  display: 'block', width: 'auto', height: 'auto', maxWidth: '100%', maxHeight: FIT_MAX_HEIGHT_PX, margin: '0 auto',
};
// In chat the card hugs the picture (up to the whole bubble, not the 380px card cap); a minimum keeps the name and
// actions readable under a small image, which then sits centred on the stage colour.
const spanFitWidthStyle: React.CSSProperties = { width: 'fit-content', maxWidth: '100%', minWidth: 'min(100%, 240px)' };
const tileStyle: React.CSSProperties = { position: 'absolute', inset: 0, display: 'grid', placeItems: 'center', color: theme.colors.textTertiary };
const playStyle: React.CSSProperties = {
  position: 'absolute', left: '50%', top: '50%', width: 34, height: 34, marginLeft: -17, marginTop: -17, borderRadius: theme.radius.pill,
  background: 'var(--color-media-play)', boxShadow: theme.elevation.subtle, display: 'grid', placeItems: 'center',
};
const playGlyphStyle: React.CSSProperties = {
  width: 0, height: 0, marginLeft: 3, borderLeft: '11px solid var(--color-media-play-glyph)',
  borderTop: '7px solid transparent', borderBottom: '7px solid transparent',
};
const overlayStyle: React.CSSProperties = {
  position: 'absolute', inset: 0, display: 'grid', placeItems: 'center', padding: 12, textAlign: 'center',
  fontSize: 'var(--fs-xs)', color: theme.colors.textSecondary, background: theme.colors.surfaceSubtle,
};
const cornerStyle: React.CSSProperties = { position: 'absolute', top: 6, right: 6, display: 'inline-flex', lineHeight: 0 };
const badgeStyle: React.CSSProperties = {
  position: 'absolute', right: 8, bottom: 8, padding: '2px 7px', borderRadius: theme.radius.pill,
  background: 'var(--color-media-chip)', color: 'var(--color-media-text)', fontFamily: theme.typography.fontMono,
  fontVariantNumeric: 'tabular-nums', fontSize: 'var(--fs-2xs)', fontWeight: 600, whiteSpace: 'nowrap',
};
const checkStyle: React.CSSProperties = {
  position: 'absolute', top: 8, left: 8, zIndex: 2, width: 16, height: 16, margin: 0, cursor: 'pointer',
  accentColor: theme.colors.primary, transition: `opacity ${theme.transitions.hover}`,
};
const footStyle: React.CSSProperties = { position: 'relative', display: 'flex', alignItems: 'flex-start', gap: 4, padding: '8px 6px 9px 10px', minWidth: 0 };
const metaColStyle: React.CSSProperties = { flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 1 };
const titleStyle: React.CSSProperties = {
  fontSize: 'var(--fs-sm)', fontWeight: 600, color: theme.colors.textPrimary, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
};
const metaStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 5, minWidth: 0, fontSize: 'var(--fs-xs)', color: theme.colors.textTertiary,
  whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
};
const descStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)', color: theme.colors.textSecondary, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', marginTop: 2,
};
const dotStyle: React.CSSProperties = { width: 7, height: 7, flexShrink: 0, borderRadius: theme.radius.pill, background: theme.colors.success };
// Over the end of the name line, on the card's own surface, so revealing the icons never reflows the text.
const actsStyle: React.CSSProperties = {
  position: 'absolute', right: 4, top: '50%', transform: 'translateY(-50%)', display: 'inline-flex', padding: '0 0 0 6px', background: theme.colors.surface,
  borderRadius: theme.radius.button, transition: `opacity ${theme.transitions.hover}`,
};
const NO_HOVER = typeof window !== 'undefined' && typeof window.matchMedia === 'function'
  && window.matchMedia('(hover: none)').matches;
const iconBtnStyle: React.CSSProperties = {
  width: 27, height: 27, display: 'grid', placeItems: 'center', padding: 0, border: 'none', background: 'none',
  borderRadius: theme.radius.button, color: theme.colors.textSecondary, cursor: 'pointer',
};
const iconBtnOnStyle: React.CSSProperties = { background: theme.colors.surfaceSubtle, color: theme.colors.textPrimary };
const scrimStyle: React.CSSProperties = { position: 'fixed', inset: 0, zIndex: theme.zIndex.popover };
const menuStyle: React.CSSProperties = {
  display: 'block', position: 'absolute', right: 0, top: 'calc(100% + 4px)', zIndex: theme.zIndex.popover + 1, minWidth: 190, padding: 5,
  background: theme.colors.surfaceRaised, border: `1px solid ${theme.colors.border}`, borderRadius: theme.radius.panel,
  boxShadow: theme.elevation.elevated,
};
const menuItemStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 8, width: '100%', padding: '7px 9px', border: 'none', background: 'none',
  borderRadius: theme.radius.button, font: 'inherit', fontSize: 'var(--fs-sm)', color: theme.colors.textPrimary, textAlign: 'left', cursor: 'pointer',
};
const menuNoteStyle: React.CSSProperties = {
  display: 'block', padding: '6px 9px 3px', marginTop: 4, borderTop: `1px solid ${theme.colors.border}`, fontSize: 'var(--fs-xs)', color: theme.colors.textTertiary,
};
