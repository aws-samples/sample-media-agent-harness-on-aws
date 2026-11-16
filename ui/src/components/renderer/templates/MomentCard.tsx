/**
 * MomentCard — one Moment in a Moments_Gallery (Component 7, media-harness-search-qa-rendering), drawn as the
 * app's ONE card ({@link MediaCard}), the same frame a chat file or a project file uses:
 *
 *   picture      — the source's frame at the Moment's start (a video), the image, or a kind tile (audio). The
 *                  range sits bottom-left and its length bottom-right. Click = Preview: the modal plays ONLY the
 *                  range, once. Minting / no access / load failed draw a status line over the picture instead.
 *   title        — Display_Name (the full name, span, scope and score in its tooltip).
 *   description  — one line of matched text, `segmentHighlights` as React text with `<mark className="mh-moment-hit">`
 *                  runs, NEVER markup (Req 3.10). An open refusal (Req 2.9 messages) takes the line instead.
 *   icons        — Preview · Cut to a new tab (video/audio; Download for an image) · Open in side panel (the
 *                  whole asset, this range selected) · ⋯ Find similar · Save to folder… · Download source, with
 *                  the match type and score as the menu's note.
 *
 * No inline player, marks or crop: to adjust a range, open it in the side panel and cut from there; Cut on the
 * card cuts the range as found. The card carries NO media URL of its own — the gallery's {@link useMomentUrls}
 * mints one per card ({@link CardUrl}), and the picture's load events route back into it (`onLoadStart` /
 * `onLoadOk` / `onLoadError`) so its refresh machine can run.
 *
 * Find similar (Req 3.24): absent for audio, for a video with no frame, and on a `no_access` card (Req 3.28);
 * disabled with a reason while a turn streams or when the session's Active_Project is another project.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { theme } from '../../../styles/tokens';
import { MediaCard, formatSpan, type CardAction } from '../../cards/MediaCard';
import { FileLocation } from '../../projects/FileLocation';
import { downloadFile } from '../../chat/artifactUrl';
import { formatRankScore, momentTimecode, segmentHighlights } from './momentText';
import { openAssetAt } from '../../../projects/openCitation';
import type { CardUrl } from '../../../projects/useMomentUrls';
import type { Resume } from '../../../projects/assetUrl';
import type { MomentItem, ThemeTokens } from '../types';

export interface MomentCardProps {
  moment: MomentItem;
  theme: ThemeTokens;
  /** The active session; the side panel and Find_Similar are scoped to it. */
  sessionId: string;
  /** This card's URL phase/url, from the gallery's {@link useMomentUrls}. */
  cardUrl: CardUrl;
  /** A video moment's own picture (the frame of the shot it starts in), from the gallery's useMomentFrames. */
  frameUrl?: string | null;

  // --- Load-event routing back into useMomentUrls (the gallery binds the moment id). ---
  /** The card's mount: may trigger the load's one refresh. */
  onLoadStart: (resume: Resume) => void;
  /** The picture's metadata / image `load`: the (re)load landed. */
  onLoadOk: () => void;
  /** The picture's `error`: refreshes once, then the card moves to `load_failed`. */
  onLoadError: (resume: Resume) => void;

  /** Preview: the gallery's modal, playing this range once. */
  onPreview: (moment: MomentItem) => void;
  /** True while Play all is on this card. */
  playing?: boolean;

  // --- Find_Similar gating supplied by the gallery / session (design "Find_Similar"). ---
  /** A turn of this session is streaming: Find similar is disabled while it runs. */
  turnInProgress?: boolean;
  /** The session's current Active_Project id; a mismatch disables Find similar. `null` / `undefined` → no gate. */
  activeProjectId?: string | null;
  onFindSimilar?: (moment: MomentItem) => void;

  /** Side-panel opener; defaults to {@link openAssetAt}. Overridable for tests. */
  openAsset?: typeof openAssetAt;
  /**
   * Cut this moment to a new tab: the gallery sends a visible chat turn that cuts `range` (seconds) out of the
   * source through the media-processing pipeline. Absent → no Cut. Never offered for an image.
   */
  onClip?: (moment: MomentItem, range: { in: number; out: number }) => void;
  /** Save to folder… (a bookmark of this range). Absent → not offered. */
  onSaveToFolder?: (moment: MomentItem) => void;

  selected?: boolean;
  /** Absent → no checkbox. */
  onSelectedChange?: (selected: boolean) => void;
  selecting?: boolean;
  /** At the picture's top-right: in a multi-image search, the query image(s) that found this moment. */
  badges?: React.ReactNode;
}

/** How long Cut stays in its "sent" state after a click (the turn is on its way). */
export const CLIP_SENT_MS = 4000;

export default function MomentCard(props: MomentCardProps): React.ReactElement {
  const { moment, sessionId, cardUrl, onLoadStart, onLoadOk, onLoadError, onPreview, playing,
    turnInProgress = false, activeProjectId, onFindSimilar, openAsset, onClip, onSaveToFolder } = props;

  const kind = moment.kind;
  const name = moment.display_name;
  const tc = useMemo(() => momentTimecode(moment.start_ms, moment.end_ms), [moment.start_ms, moment.end_ms]);
  const hasFrame = !!moment.ref.frame_s3_key;
  const [refusal, setRefusal] = useState<string | null>(null);

  const phase = cardUrl.phase;
  const readyUrl = cardUrl.phase === 'ready' ? cardUrl.url : null;
  // In `no_access`, an Actor without access sees no more than the asset's name and span.
  const hideContent = phase === 'no_access';

  // The mount `load_start`, once: a card seeded `ready` from cache re-checks staleness.
  const onLoadStartRef = useRef(onLoadStart);
  onLoadStartRef.current = onLoadStart;
  const startSec = moment.start_ms / 1000;
  useEffect(() => { onLoadStartRef.current({ time: startSec, playing: false }); }, [startSec]);

  // The side panel opens the WHOLE asset with this span selected — never a trimmed clip.
  const openInPanel = useCallback(async () => {
    setRefusal(null);
    const range = kind !== 'image' && moment.end_ms > moment.start_ms && moment.start_ms >= 0
      ? { start: moment.start_ms / 1000, end: moment.end_ms / 1000 } : undefined;
    const res = await (openAsset ?? openAssetAt)(
      { ref: moment.ref, kind, displayName: name, startMs: moment.start_ms, ...(range ? { range } : {}) },
      sessionId,
    );
    if (!res.ok) setRefusal(res.message);
  }, [openAsset, moment.ref, kind, name, moment.start_ms, moment.end_ms, sessionId]);

  const [clipSent, setClipSent] = useState(false);
  useEffect(() => {
    if (!clipSent) return;
    const id = setTimeout(() => setClipSent(false), CLIP_SENT_MS);
    return () => clearTimeout(id);
  }, [clipSent]);
  const cut = useCallback(() => {
    if (clipSent || !onClip || !readyUrl) return;
    onClip(moment, { in: moment.start_ms / 1000, out: moment.end_ms / 1000 });
    setClipSent(true);
  }, [clipSent, onClip, readyUrl, moment]);

  // Find similar (Req 3.24). Structural "no frame" takes precedence over the transient session gates.
  const findSimilar: CardAction | null = useMemo(() => {
    if (kind === 'audio' || hideContent || !onFindSimilar) return null;
    if (kind === 'video' && !hasFrame) return null;
    const base = { label: 'Find similar', icon: 'similar' as const, onClick: () => onFindSimilar(moment) };
    if (turnInProgress) return { ...base, disabled: true, reason: 'Available when the current reply finishes' };
    if (activeProjectId != null && activeProjectId !== moment.ref.project_id) {
      return { ...base, disabled: true, reason: 'Switch the active project to search from this moment' };
    }
    return base;
  }, [kind, hideContent, hasFrame, turnInProgress, activeProjectId, moment, onFindSimilar]);

  const primary: CardAction | undefined = hideContent ? undefined
    : kind === 'image'
      ? (readyUrl ? { label: `Download ${name}`, icon: 'download', onClick: () => void downloadFile(readyUrl, name) } : undefined)
      : onClip
        ? {
          label: clipSent ? `Cutting ${name}` : `Cut ${name} ${tc} to a new tab`, icon: 'cut', onClick: cut,
          disabled: clipSent || !readyUrl,
          reason: clipSent ? 'Cutting… the clip opens as a new tab when it is ready'
            : !readyUrl ? 'Available when the moment has loaded' : 'Cut to a new tab',
        }
        : undefined;

  const menu: CardAction[] = [];
  if (findSimilar) menu.push(findSimilar);
  if (onSaveToFolder && moment.ref.asset_id && !hideContent) {
    menu.push({ label: 'Save to folder…', icon: 'folder', onClick: () => onSaveToFolder(moment) });
  }
  if (readyUrl && kind !== 'image') menu.push({ label: 'Download source', icon: 'download', onClick: () => void downloadFile(readyUrl, name) });

  const score = formatRankScore(moment.final_score);
  const segments = !hideContent && moment.source_index !== 'shot' && moment.text.length > 0
    ? segmentHighlights(moment.text, moment.highlight_terms) : null;
  const tooltip = [name, [kind !== 'image' ? tc : null, moment.source_index].filter(Boolean).join(' · '),
    hideContent ? null : `rank score ${score}`].filter(Boolean).join('\n');

  const overlay = phase === 'no_access'
    ? <span role="status" data-testid={`moment-no-access-${moment.id}`}>You no longer have access to this project</span>
    : phase === 'load_failed'
      ? <span role="status" data-testid={`moment-load-failed-${moment.id}`}>This moment could not be loaded</span>
      : !readyUrl
        ? <span role="status" aria-busy="true" data-testid={`moment-minting-${moment.id}`}>Loading…</span>
        : undefined;

  const description = refusal
    ? <span role="alert" data-testid={`moment-refusal-${moment.id}`} title={refusal} style={{ color: theme.colors.error }}>{refusal}</span>
    : segments
      ? (
        <span data-testid={`moment-text-${moment.id}`} title={moment.text}>
          {segments.map((seg, i) => (seg.highlighted
            ? <mark key={i} className="mh-moment-hit">{seg.text}</mark>
            : <React.Fragment key={i}>{seg.text}</React.Fragment>))}
          {moment.text_truncated ? '…' : null}
        </span>
      )
      : undefined;

  const resume = (): Resume => ({ time: startSec, playing: false });
  return (
    <MediaCard
      as="li"
      kind={kind === 'audio' ? 'audio' : kind === 'image' ? 'image' : 'video'}
      title={name}
      titleTip={tooltip}
      // Which of hybrid search's searches found it (Lab 2: meaning and keyword combine).
      meta={moment.found_by?.length ? `Found by ${moment.found_by.join(' + ')}` : undefined}
      description={description}
      location={<FileLocation projectId={moment.ref.project_id} assetId={moment.ref.asset_id} />}
      // A video shows its hero still, never its source: that is the whole file, and one player per card
      // starved the browser's connections (Firefox). Playback is the gallery's one Preview player.
      thumb={kind === 'image' ? readyUrl : kind === 'video' && cardUrl.phase === 'ready' ? props.frameUrl ?? cardUrl.poster ?? null : null}
      contain
      badgeLeft={kind !== 'image' ? tc : undefined}
      badge={kind !== 'image' ? formatSpan(moment.end_ms - moment.start_ms) : undefined}
      overlay={overlay}
      corner={props.badges}
      onMediaMeta={onLoadOk}
      onMediaError={() => onLoadError(resume())}
      onPreview={() => onPreview(moment)}
      primary={primary}
      onOpenInPanel={hideContent ? undefined : () => void openInPanel()}
      menu={menu}
      menuNote={hideContent ? undefined : [moment.source_index, `match ${score}`].filter(Boolean).join(' · ')}
      selected={props.selected}
      onSelectedChange={props.onSelectedChange}
      selecting={props.selecting}
      highlight={playing}
      testId={`moment-card-${moment.id}`}
      dataAttrs={{ 'data-moment-card': '', 'data-playing': playing ? 'true' : undefined }}
    />
  );
}
