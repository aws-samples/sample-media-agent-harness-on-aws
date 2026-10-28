/**
 * The ONE card every file in a chat answer renders as — image, video, audio or document — so they look and
 * act the same: a 16:9 preview area (the picture, the video's first frame, a waveform or a page tile), then
 * the name with "type · details", then the same three actions in the same order: Preview, Download, Open in
 * side panel. Clicking the media is Preview too.
 *
 * Audio is the one exception, and a compact one: there is nothing to look at, so a large preview adds a step
 * and no information. Its card is one row (name, Download, Open in side panel) over a native player that
 * plays in place. `preload="none"`: nothing downloads until the user presses play.
 *
 * `consent`: an image from a host we do not trust is not fetched until the user asks (the card shows a
 * "Load image" tile instead), so a chat answer cannot make the browser call an arbitrary server.
 */
import React, { useState } from 'react';
import { AssetIcon } from './AssetIcons';
import { MediaCard } from '../cards/MediaCard';
import { useMoveToFolder, type MoveTarget } from './MoveToFolderButton';
import type { PreviewKind } from './AssetPreviewModal';
import './ChatAssets.css';

const KIND_LABEL: Record<PreviewKind, string> = { image: 'Image', video: 'Video', audio: 'Audio', document: 'Document' };

export interface ChatAssetCardProps {
  kind: PreviewKind;
  name: string;
  /** A loadable URL for the preview area; `null` while resolving (a skeleton shows). */
  src: string | null;
  /** The document's type label (PDF, DOCX…); defaults to "Document". */
  docLabel?: string;
  /** Untrusted-host image: show "Load image" until the user clicks (nothing is fetched before). */
  needsConsent?: boolean;
  onConsent?: () => void;
  /** Not used for audio, which plays in the card. */
  onPreview?: () => void;
  onDownload?: () => void;
  onOpenInPanel?: () => void;
  /** The chat project's file this is, when it is one: ⋯ → Move to folder. */
  moveTarget?: MoveTarget;
}

const fmtDur = (s: number) => {
  const t = Math.max(0, Math.round(s)), h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), sec = t % 60;
  return (h ? `${h}:${String(m).padStart(2, '0')}` : `${m}`) + `:${String(sec).padStart(2, '0')}`;
};

export function ChatAssetCard(p: ChatAssetCardProps) {
  const move = useMoveToFolder(p.moveTarget, p.name, true);
  // What the media itself tells us once it loads: size and length. Never guessed.
  const [dims, setDims] = useState<string | null>(null);
  const [duration, setDuration] = useState<number | null>(null);
  const label = p.kind === 'document' ? (p.docLabel || KIND_LABEL.document) : KIND_LABEL[p.kind];
  const sub = [label, dims, duration !== null ? fmtDur(duration) : null].filter(Boolean).join(' · ');

  if (p.kind === 'audio') {
    return (
      <span className="cap-card cap-card--audio" data-testid="chat-asset-card" data-kind="audio">
        <span className="cap-foot">
          <span className="cap-audio-glyph" aria-hidden="true"><AssetIcon kind="audio" size={18} /></span>
          <span className="cap-meta">
            <span className="cap-name" title={p.name}>{p.name}</span>
            <span className="cap-sub">{sub}</span>
          </span>
          <span className="cap-acts">{actions(p)}</span>
        </span>
        {/* No crossOrigin: plain playback needs no CORS, and it would refuse hosts that send none. */}
        {p.src
          ? <audio className="cap-audio" src={p.src} controls preload="none" aria-label={`Play ${p.name}`}
              onLoadedMetadata={(e) => { if (Number.isFinite(e.currentTarget.duration)) setDuration(e.currentTarget.duration); }} />
          : <span className="cap-audio cap-audio--loading" aria-label={`Loading ${p.name}`} />}
      </span>
    );
  }

  // Image, video, document: the ONE card (MediaCard), the same as a project file or a moment. Preview · Download ·
  // Open in side panel, and ⋯ → Move to folder when this is a file of the chat's project.
  const kindForCard = p.kind === 'document' ? 'document' : p.kind;
  return (
    <MediaCard as="span" testId="chat-asset-card" kind={kindForCard} title={p.name} meta={sub}
      thumb={p.kind === 'image' && !p.needsConsent ? p.src : null}
      // An image in chat keeps its own shape, scaled to the bubble's width: never cropped to 16:9.
      fitWidth={p.kind === 'image'}
      videoSrc={p.kind === 'video' ? p.src : null}
      badge={p.kind === 'video' && duration !== null ? fmtDur(duration) : undefined}
      pictureOverride={p.kind === 'image' && p.needsConsent ? (
        <span style={consentWrapStyle}>
          <button type="button" className="cap-consent" onClick={(e) => { e.stopPropagation(); p.onConsent?.(); }}>
            <AssetIcon kind="image" size={22} /> Load image
          </button>
        </span>
      ) : (!p.src && (p.kind === 'image' || p.kind === 'video')) ? <span className="cap-skeleton" aria-label={`Loading ${p.name}`} /> : undefined}
      onMediaMeta={(m) => {
        if (m.width && m.height) setDims(`${m.width}×${m.height}`);
        if (m.duration !== undefined) setDuration(m.duration);
      }}
      onPreview={p.needsConsent ? (p.onConsent ?? (() => {})) : (p.onPreview ?? (() => {}))}
      primary={p.onDownload ? { label: `Download ${p.name}`, icon: 'download', onClick: p.onDownload } : undefined}
      onOpenInPanel={p.onOpenInPanel}
      menu={move.available ? [{ label: 'Move to folder…', icon: 'folder', onClick: move.openPicker }] : undefined}>
      {move.ui}
    </MediaCard>
  );
}

const consentWrapStyle: React.CSSProperties = { position: 'absolute', inset: 0, display: 'grid', placeItems: 'center', background: 'var(--color-media-stage)' };

/** The card's actions, always in this order: Preview (not for audio), Download, Open in side panel. */
function actions(p: ChatAssetCardProps) {
  return (
    <>
      {p.onPreview && (
        <button type="button" className="cap-icon" onClick={p.onPreview} aria-label={`Preview ${p.name}`} data-tip="Preview">
          <AssetIcon kind="expand" />
        </button>
      )}
      {p.onDownload && (
        <button type="button" className="cap-icon" onClick={p.onDownload} aria-label={`Download ${p.name}`} data-tip="Download">
          <AssetIcon kind="download" />
        </button>
      )}
      {p.onOpenInPanel && (
        <button type="button" className="cap-icon" onClick={p.onOpenInPanel} aria-label={`Open ${p.name} in the side panel`} data-tip="Open in side panel">
          <AssetIcon kind="panel" />
        </button>
      )}
    </>
  );
}
