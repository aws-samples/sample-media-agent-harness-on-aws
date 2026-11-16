/**
 * The image half of the Moments tab's search bar (design 2026-10-18 §2, mock `moments-image.html` option A and
 * `action-trail.html` §2):
 * The bar holds words OR an image, never both (M2C searches one kind per call; mock `search-and-actions.html` §1):
 *   - {@link ImageQueryValue}: image mode — the field becomes the image (thumbnail + name + "· similar moments"), its
 *     menu (Use a different image…, Open at <time>, Remove), "Search by words" and ✕;
 *   - {@link SearchByImageButton}: words mode's image button at the field's right end, and {@link SearchByImage}, its
 *     popover, laid out like the composer's + menu — upload (or drop / paste) a picture, kept with this chat only;
 *     the selected card's frame; or "Recent images in <project>" with a name search (the + menu's own section);
 *   - {@link MatchedBadges}: on a card of a multi-image search, the image(s) that found it.
 */
import React, { useEffect, useId, useRef, useState } from 'react';
import { AssetIcon } from '../../chat/AssetIcons';
import type { RendererProps, MomentsQueryImage } from '../types';
import type { MomentsTabSearch } from './momentsTabSearch';
import { request as requestAssetUrls } from '../../../projects/assetUrlBatcher';
import { openAssetAt } from '../../../projects/openCitation';
import { isQueryImageFile, uploadQueryImage } from '../../../projects/upload/queryImage';
import { searchAssets } from '../../../projects/api';
import { getSessionProject } from '../../../projects/sessionProjectStore';
import PaperclipIcon from '../../icons/PaperclipIcon';
import { theme as tokens } from '../../../styles/tokens';
import { RecentAssetsSection, useAssetSearch, useRecentAssets, type RecentAssetsResult } from '../../chat/RecentAssets';
import { iconColumnStyle, itemNameStyle, metaStyle, noteStyle, rowStyle, separatorStyle } from '../../chat/plusMenuStyles';

type Theme = RendererProps['theme'];

const mmss = (ms: number) => {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
};

/** A query image's thumbnail: an image's rendition, or the source video seeked to the frame's moment. */
export function QueryThumb({ image, projectId, width = 38, height = 22 }: { image: MomentsQueryImage; projectId: string; width?: number | string; height?: number }) {
  const [src, setSrc] = useState<string | null>(null);
  const id = image.assetId ?? image.sourceAssetId ?? '';
  useEffect(() => {
    let live = true;
    if (!id || !projectId) return;
    void requestAssetUrls(projectId, [id]).then((r) => {
      const out = r.get(id);
      if (!live || !out?.ok) return;
      setSrc(image.assetId ? out.entry.rendition?.url ?? out.entry.source.url : out.entry.source.url);
    });
    return () => { live = false; };
  }, [id, projectId, image.assetId]);
  const box: React.CSSProperties = { width, height, borderRadius: 4, objectFit: 'cover', flexShrink: 0, display: 'block', background: '#0f1115' };
  if (!src) return <span aria-hidden="true" style={box} data-testid="query-thumb" />;
  if (image.assetId) return <img src={src} alt="" style={box} data-testid="query-thumb" />;
  const at = Math.max(0.1, (image.atMs ?? 0) / 1000);
  return <video src={`${src.split('#')[0]}#t=${at}`} muted playsInline preload="metadata" tabIndex={-1} aria-hidden="true" style={box} data-testid="query-thumb" />;
}

type Picker = { mode: 'camera' } | { mode: 'add' } | { mode: 'replace'; index: number };

/** "similar moments" / "frame 00:12" — what an image query is, after its name. */
function imageKindText(image: MomentsQueryImage): string {
  return image.frameKey ? `frame${typeof image.atMs === 'number' ? ` ${mmss(image.atMs)}` : ''}` : 'similar moments';
}

/**
 * IMAGE MODE: the search field BECOMES the image (mock `search-and-actions.html` §1). Thumbnail + name + "· similar
 * moments" opens the image's menu (Use a different image…, Open at…, Remove); then "Search by words" and ✕. There is
 * no text input here: M2C searches words OR an image, never both.
 */
export function ImageQueryValue({ search, projectId, sessionId, selectedImage, theme: t, onWords }: {
  search: MomentsTabSearch; projectId: string; sessionId: string; selectedImage: MomentsQueryImage | null; theme: Theme;
  /** Back to words mode (the bar focuses its input). */
  onWords: () => void;
}) {
  const [menu, setMenu] = useState(false);
  const [picker, setPicker] = useState<Picker | null>(null);
  const image = search.images[0];
  if (!image) return null;
  const label = search.images.map((x) => x.label).join(' · ');
  return (
    <>
      <span style={{ position: 'relative', display: 'flex', flex: 1, minWidth: 0 }}>
        <button type="button" data-testid="query-image-chip" aria-haspopup="menu" aria-expanded={menu} title={label}
          onClick={() => setMenu(!menu)} style={imageValueStyle(t, menu)}>
          <QueryThumb image={image} projectId={projectId} width={46} height={28} />
          <span style={{ fontWeight: 600, color: t.colors.textPrimary, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 }}>{label}</span>
          <span style={{ color: t.colors.textTertiary, whiteSpace: 'nowrap', fontWeight: 400 }}>· {imageKindText(image)}</span>
        </button>
        {menu && (
          <>
            <span style={scrimStyle} aria-hidden="true" onMouseDown={() => setMenu(false)} />
            <span role="menu" aria-label={`${image.label} options`} style={menuStyle(t)} data-testid="query-image-menu">
              <MenuItem t={t} onClick={() => { setMenu(false); setPicker({ mode: 'replace', index: 0 }); }}>Use a different image…</MenuItem>
              {(image.sourceAssetId || image.assetId) && (
                <MenuItem t={t} onClick={() => {
                  setMenu(false);
                  void openAssetAt({
                    ref: { project_id: projectId, asset_id: (image.sourceAssetId ?? image.assetId) as string, frame_s3_key: null },
                    kind: image.sourceAssetId ? 'video' : 'image', displayName: image.label, startMs: image.atMs ?? 0,
                  }, sessionId);
                }}>↗ Open{typeof image.atMs === 'number' ? ` at ${mmss(image.atMs)}` : ''}</MenuItem>
              )}
              <MenuItem t={t} danger onClick={() => { setMenu(false); onWords(); }}>✕ Remove</MenuItem>
            </span>
          </>
        )}
        {picker && (
          <SearchByImage theme={t} projectId={projectId} sessionId={sessionId} selectedImage={selectedImage}
            hasImages canAdd={false} mode={picker.mode} replaceLabel={image.label}
            onClose={() => setPicker(null)} onPick={(img) => { setPicker(null); search.searchImages([img]); }} />
        )}
      </span>
      <button type="button" data-testid="query-search-words" onClick={onWords} style={linkStyle(t)}>Search by words</button>
      <button type="button" aria-label={`Clear ${image.label}`} title="Clear" data-testid="query-image-remove" onClick={onWords}
        style={iconButtonStyle(t, false)}><AssetIcon kind="close" size={14} /></button>
    </>
  );
}

/** WORDS MODE's image button (right end of the field) and its Search-by-image popover. */
export function SearchByImageButton({ search, projectId, sessionId, selectedImage, theme: t }: {
  search: MomentsTabSearch; projectId: string; sessionId: string; selectedImage: MomentsQueryImage | null; theme: Theme;
}) {
  const [open, setOpen] = useState(false);
  return (
    <span style={{ position: 'relative', display: 'inline-flex' }}>
      <button type="button" aria-label="Search by image" title="Search by image" data-testid="search-by-image"
        aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen(!open)} style={iconButtonStyle(t, open)}>
        <AssetIcon kind="image" size={15} />
      </button>
      {open && (
        <SearchByImage theme={t} projectId={projectId} sessionId={sessionId} selectedImage={selectedImage}
          hasImages={false} canAdd={false} mode="camera"
          onClose={() => setOpen(false)} onPick={(img) => { setOpen(false); search.searchImages([img]); }} />
      )}
    </span>
  );
}

function MenuItem({ t, onClick, hint, danger, children }: { t: Theme; onClick: () => void; hint?: string; danger?: boolean; children: React.ReactNode }) {
  return (
    <button type="button" role="menuitem" onClick={onClick} style={{ ...menuItemStyle(t), ...(danger ? { color: t.colors.error } : null) }}>
      <span style={{ flex: 1 }}>{children}</span>
      {hint && <span style={{ color: t.colors.textTertiary, fontSize: 11 }}>{hint}</span>}
    </button>
  );
}

/**
 * The Search-by-image popover (the image icon at the right end of the search field). `mode`: `camera` (from the 📷 button — with images already in the bar it asks Add / Replace),
 * `add`, or `replace` (from a chip's menu). A dropped or pasted picture is uploaded as this chat's own file.
 */
export function SearchByImage({ theme: t, projectId, sessionId, selectedImage, hasImages, canAdd, mode, replaceLabel, onPick, onClose }: {
  theme: Theme; projectId: string; sessionId: string; selectedImage: MomentsQueryImage | null; hasImages: boolean; canAdd: boolean;
  mode: Picker['mode']; replaceLabel?: string;
  onPick: (image: MomentsQueryImage, action: 'add' | 'replace' | 'new') => void; onClose: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  // One image per search (M2C searches one at a time): picking a picture REPLACES the one in the bar, never asks.
  const action: 'add' | 'replace' | 'new' = mode === 'add' && canAdd ? 'add' : hasImages ? 'replace' : 'new';
  const useFile = async (file: File | null | undefined) => {
    setError(null);
    if (!isQueryImageFile(file)) { setError('Use a JPEG, PNG, WebP or GIF image.'); return; }
    setBusy(`Uploading ${file.name}…`);
    const r = await uploadQueryImage(projectId, sessionId, file);
    setBusy(null);
    if (!r.ok) { setError(r.message); return; }
    onPick({ assetId: r.assetId, label: [...r.name].slice(0, 80).join('') }, action);
  };
  useEffect(() => {
    const onPaste = (e: ClipboardEvent) => {
      const file = Array.from(e.clipboardData?.files ?? []).find((f) => isQueryImageFile(f));
      if (file) { e.preventDefault(); void useFile(file); }
    };
    window.addEventListener('paste', onPaste);
    return () => window.removeEventListener('paste', onPaste);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [action]);
  // "Recent images in <project>" + name search: the composer's + menu section, so both pickers look and read alike.
  const projectName = getSessionProject(sessionId)?.activeProjectName || 'this project';
  const [query, setQuery] = useState('');
  const recent = useRecentAssets(true, projectId || null, loadProjectImages);
  const found = useAssetSearch(true, projectId || null, query, findProjectImages);
  const headingId = useId();
  return (
    <>
      <span style={scrimStyle} aria-hidden="true" onMouseDown={onClose} />
      <div role="dialog" aria-label="Search by image" data-testid="search-by-image-dialog" style={popStyle(t)}
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => { e.preventDefault(); void useFile(e.dataTransfer.files?.[0]); }}>
        <button type="button" data-testid="search-by-image-drop" disabled={!!busy}
          onClick={() => fileRef.current?.click()} style={rowStyle(null, 'upload')}>
          <span style={iconColumnStyle}><PaperclipIcon /></span>
          <span style={itemNameStyle}>{busy ?? 'Upload an image'}</span>
          {!busy && <span style={metaStyle}>or drop / paste</span>}
        </button>
        <input ref={fileRef} type="file" accept="image/*" hidden data-testid="search-by-image-file"
          onChange={(e) => void useFile(e.target.files?.[0])} />
        {selectedImage && (
          <button type="button" data-testid="search-by-image-selected" onClick={() => onPick(selectedImage, action)} style={rowStyle(null, 'selected')}>
            <QueryThumb image={selectedImage} projectId={projectId} width={22} height={14} />
            <span style={itemNameStyle}>Selected card's {selectedImage.frameKey ? 'frame' : 'image'}</span>
          </button>
        )}
        {error && <div role="alert" style={{ ...noteStyle, color: t.colors.error }}>{error}</div>}
        {projectId && (
          <>
            <div role="separator" style={separatorStyle} />
            <RecentAssetsSection headingId={headingId} projectName={projectName} noun="images" state={recent}
              locked={false} lockedReason="" highlight={null} onHover={() => {}}
              onAttach={(a) => onPick({ assetId: a.assetId, label: [...a.displayName].slice(0, 80).join('') }, action)}
              search={{ query, onQuery: setQuery, state: found }} />
          </>
        )}
      </div>
    </>
  );
}

const toRecent = (r: Awaited<ReturnType<typeof searchAssets>>): RecentAssetsResult =>
  (r.ok ? { ok: true, value: { assets: r.value.assets.filter((a) => a.status !== 'failed') } } : { ok: false });
const loadProjectImages = (pid: string) => searchAssets(pid, { kind: 'image' }).then(toRecent);
const findProjectImages = (pid: string, q: string) => searchAssets(pid, { kind: 'image', q }).then(toRecent);

/** On a card of a multi-image search: the image(s) that found it, as tiny thumbnails. */
export function MatchedBadges({ images, matchedBy, projectId }: { images: MomentsQueryImage[]; matchedBy?: number[]; projectId: string }) {
  if (images.length < 2 || !matchedBy?.length) return null;
  return (
    <span data-testid="matched-badges" title={`Matched ${matchedBy.map((i) => images[i]?.label).filter(Boolean).join(' and ')}`}
      style={{ display: 'inline-flex', gap: 3 }}>
      {matchedBy.filter((i) => images[i]).map((i) => (
        <span key={i} style={{ border: '1.5px solid #fff', borderRadius: 3, lineHeight: 0 }}>
          <QueryThumb image={images[i]} projectId={projectId} width={22} height={13} />
        </span>
      ))}
    </span>
  );
}

// ---------------------------------------------------------------------------------------------------- styles
const scrimStyle: React.CSSProperties = { position: 'fixed', inset: 0, zIndex: 40 };
function menuStyle(t: Theme): React.CSSProperties {
  return { position: 'absolute', left: 0, top: 'calc(100% + 4px)', zIndex: 41, minWidth: 230, padding: 5, display: 'flex', flexDirection: 'column',
    background: t.colors.surface, border: `1px solid ${t.colors.border}`, borderRadius: 10, boxShadow: '0 14px 36px rgba(15,23,42,.16)' };
}
function menuItemStyle(t: Theme): React.CSSProperties {
  return { display: 'flex', alignItems: 'center', gap: 8, padding: '7px 9px', border: 'none', background: 'transparent', borderRadius: 7,
    textAlign: 'left', cursor: 'pointer', fontSize: 13, fontFamily: 'inherit', color: t.colors.textPrimary };
}
/** The + menu's panel (PlusMenu `menuStyle`), opening downward from the search field. */
function popStyle(_t: Theme): React.CSSProperties {
  return { position: 'absolute', right: 0, top: 'calc(100% + 6px)', zIndex: 41, width: 300, maxHeight: '60vh', overflowY: 'auto',
    boxSizing: 'border-box', padding: 6, background: tokens.colors.surfaceRaised, border: `1px solid ${tokens.colors.border}`,
    borderRadius: tokens.radius.panel, boxShadow: tokens.elevation.elevated, fontFamily: tokens.typography.fontFamily,
    fontSize: tokens.typography.sizes.body, color: tokens.colors.textPrimary, textAlign: 'left' };
}
function imageValueStyle(t: Theme, on: boolean): React.CSSProperties {
  return { display: 'flex', alignItems: 'center', gap: 9, flex: 1, minWidth: 0, height: 32, padding: '0 6px 0 2px', border: 'none',
    borderRadius: 7, background: on ? t.colors.surfaceSubtle : 'transparent', fontSize: 13, fontFamily: 'inherit', cursor: 'pointer', textAlign: 'left' };
}
function iconButtonStyle(t: Theme, on: boolean): React.CSSProperties {
  return { width: 28, height: 26, display: 'grid', placeItems: 'center', border: 'none', borderRadius: 7, cursor: 'pointer', flexShrink: 0,
    background: on ? t.colors.accentSubtle : 'transparent', color: on ? t.colors.primary : t.colors.textSecondary };
}
function linkStyle(t: Theme): React.CSSProperties {
  return { border: 'none', background: 'none', padding: '0 4px', font: 'inherit', fontSize: 12.5, fontWeight: 600, color: t.colors.primary,
    cursor: 'pointer', whiteSpace: 'nowrap', flexShrink: 0 };
}
