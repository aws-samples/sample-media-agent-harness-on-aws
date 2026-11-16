import React, { useContext, useEffect, useState, useRef, useCallback } from 'react';
import { cropAspectLabel } from '../../../projects/mediaActions';
import type { RendererProps, InteractionEvent, CropActionData, CropBox } from '../types';
import { useTimelineEngine, TimelineEngine, TimelineState } from '../../nle/TimelineEngine';
import { parseManifest } from '../../nle/manifestParser';
import type { RenderManifest, ManifestTrack } from '../../nle/manifestParser';
import WaveformVisualizer from '../../nle/WaveformVisualizer';
import { applyMediaGain } from '../mediaGain';
import { gainCrossOrigin, watchCorsRefusal } from '../mediaCors';
import CaptionRenderer from '../../nle/CaptionRenderer';
import { activeBoxesAt, normalizeAnnotations, personLabel } from '../annotations';
import { RenderFailure } from '../renderFailure';
import type { Annotation } from '../annotations';
import ClipManager from '../../nle/ClipManager';
import InspectorPanel from '../../nle/InspectorPanel';
import { computeInspectorSelection } from '../../../services/renderOrchestration';
import { authFetch } from '../../../auth/authFetch';
import TimelineEditor from '../../nle/TimelineEditor';
import { useVideoFps } from '../../nle/HoverPeek';
import CropOverlay from '../../nle/CropOverlay';
import SegmentTable from '../../nle/SegmentTable';
import UnifiedTimelineLanes, { LANE_GUTTER } from '../../nle/UnifiedTimelineLanes';
import { useTimelineView } from '../../nle/timelineView';
import MetadataPanel from '../../nle/MetadataPanel';
import DocumentViewer from './DocumentViewer';
import ClipGallery from './ClipGallery';
import CropIcon from '../../icons/CropIcon';
import CloseIcon from '../../icons/CloseIcon';
import CheckIcon from '../../icons/CheckIcon';
import { normalizeViewState, sameViewState, type RenderViewState } from '../../../shared/renderViewState';
import { ChatAssetContext } from '../../chat/InlineAssetImage';
import { useTimelineKeyframes } from '../../../projects/useKeyframes';

interface NleTimelineData {
  manifestUrl: string;
  manifest?: RenderManifest;
  /** Opt-in (Component 5): seconds to seek to, paused, on the FIRST loadedmetadata after mount.
   *  AssetPlayer passes the Citation's `startMs / 1000`. Existing manifests never set it, so the
   *  default (undefined) reproduces today's behavior — no seek on load. */
  initialTime?: number;
  /** Opt-in (Component 5): the media element NleTimeline attaches to its TimelineEngine, reported up
   *  (and `null` on unmount) so a parent can watch load/play/seek/error for a URL refresh. Default
   *  undefined — no-op for every existing caller. */
  onMediaElement?: (el: HTMLMediaElement | null) => void;
  /** Persisted view state to restore on mount (R8.2): selected range, frame marker, crop box.
   *  Absent → the manifest's own `viewState`, if any. */
  viewState?: RenderViewState;
  /** Set only for a persisted view (ManifestRouter): called with the full state on each change. */
  onViewStateChange?: (viewState: RenderViewState) => void;
  /** When true (default) the media element keeps `crossOrigin="anonymous"` so the waveform/gain path
   *  can decode it via Web Audio. AssetPlayer passes false: its single-track inline manifest points at
   *  a presigned S3 URL (the m2c proxy bucket has NO CORS config), and `crossOrigin="anonymous"` turns
   *  a missing Access-Control-Allow-Origin into a refused load — a silent audio that will not play, or
   *  "could not be loaded" (staging project p925d3f6's per-speaker diarization .wav, 2026-10-09). There
   *  are no peaks on that manifest, so nothing needs CORS. Mirrors ClipEditor's `mediaCrossOrigin`. */
  mediaCrossOrigin?: boolean;
}
type LoadState = { status: 'loading' } | { status: 'loaded'; manifest: RenderManifest } | { status: 'error'; message: string; url: string };
const MANIFEST_LOAD_TIMEOUT_MS = 10_000;
const MAX_TRACKS = 32;
const VALID_TRACK_TYPES = new Set(['video', 'audio', 'overlay', 'subtitle']);

/** The single-video crop prompt. Pure + exported so the box contract is unit-tested without
 *  mounting the timeline. The box value IS the crop (aspect baked into w/h); the SKILL routes it
 *  to the crop_video tool. Concise on purpose — the agent copies box="x,y,w,h" into the tool. */
export function buildCropPrompt(box: CropBox): string {
  const f = (n: number) => Math.max(0, Math.min(1, n)).toFixed(3);
  const arg = `${f(box.x)},${f(box.y)},${f(box.w)},${f(box.h)}`;
  return `Crop the video to box="${arg}" (x, y, w, h as fractions of the frame).`;
}

/** 5xx / 408 / 429 / network — worth the Retry button rather than dropping the tab. */
export function isTransientLoadError(message: string): boolean {
  return /Failed: (5\d\d|408|429)\b/.test(message) || /network|fetch|timed? ?out|abort/i.test(message);
}

interface EBProps { children: React.ReactNode; fallback: (error: Error, retry: () => void) => React.ReactNode; }
interface EBState { error: Error | null; }
class NleErrorBoundary extends React.Component<EBProps, EBState> {
  state: EBState = { error: null };
  static getDerivedStateFromError(error: Error): EBState { return { error }; }
  handleRetry = () => { this.setState({ error: null }); };
  render() { return this.state.error ? this.props.fallback(this.state.error, this.handleRetry) : this.props.children; }
}

function SkeletonBlock({ height, theme: t }: { height: number; theme: RendererProps['theme'] }) {
  return <div style={{ height, background: `linear-gradient(90deg, ${t.colors.border} 25%, transparent 50%, ${t.colors.border} 75%)`, backgroundSize: '200% 100%', borderRadius: 4 }} />;
}

interface ContentProps { loadState: LoadState; engine: TimelineEngine; state: TimelineState; theme: RendererProps['theme']; mediaRef: React.MutableRefObject<HTMLMediaElement | null>; manifestUrl: string; onRetry: () => void; onInteraction?: (event: InteractionEvent) => void; onMeasuredDuration?: (seconds: number) => void; initialTime?: number; onMediaElement?: (el: HTMLMediaElement | null) => void; cropBox?: CropBox | null; restoredCrop?: CropBox | null; onCropChange?: (box: CropBox | null) => void; mediaCrossOrigin?: boolean; }

function NleTimelineContent({ loadState, engine, state, theme: t, mediaRef, onRetry, onInteraction, onMeasuredDuration, initialTime, onMediaElement, cropBox = null, restoredCrop = null, onCropChange, mediaCrossOrigin = true }: ContentProps) {
  const [showFaces, setShowFaces] = useState(true); // #6 — face bbox overlay toggle (default on so boxes are visible)
  const [showCrop, setShowCrop] = useState(false);  // spatial crop selector over the video (off by default)
  // ONE zoom/scroll for the ruler and every lane, so they line up at any zoom (a shot on a 2-hour film
  // is a few pixels at 1×). While zoomed, playback or a seek past the visible window pages it along.
  const timelineView = useTimelineView();
  // Where am I: the hovered time, shared by the ruler and every lane (one line, one frame peek), and the video's
  // measured frame rate, which makes every timecode read HH:MM:SS:FF and lands marks on frame starts.
  const [hoverTime, setHoverTime] = useState<number | null>(null);
  const [fps, setFps] = useState<number | null>(null);
  const { follow } = timelineView;
  useEffect(() => { follow(state.currentTime, state.duration); }, [follow, state.currentTime, state.duration]);
  const didInitialSeekRef = useRef(false); // Component 5: initialTime applies on the FIRST load only, never on a URL refresh.
  // A persisted view that was left with a crop box drawn reopens with it (R8.2).
  useEffect(() => { if (restoredCrop) setShowCrop(true); }, [restoredCrop]);
  // The file's keyframes (snap targets, below): read once per file from its index, through the chat's project.
  const assetCtx = useContext(ChatAssetContext);
  const keyframes = useTimelineKeyframes(assetCtx?.projectId, (loadState as { manifest?: unknown }).manifest);
  if (loadState.status === 'loading') {
    return (<div style={{ display: 'flex', flexDirection: 'column', gap: 8, padding: 16, background: t.colors.surface, border: `1px solid ${t.colors.border}`, borderRadius: t.radius.panel }}><SkeletonBlock height={200} theme={t} /><SkeletonBlock height={48} theme={t} /><SkeletonBlock height={80} theme={t} /></div>);
  }
  if (loadState.status === 'error') {
    const card = <div style={{ padding: 24, background: t.colors.surface, border: `1px solid ${t.colors.border}`, borderRadius: t.radius.panel, textAlign: 'center' }}><p style={{ color: t.colors.textPrimary, marginBottom: 8 }}>Failed to load timeline</p><p style={{ color: t.colors.textSecondary, fontSize: 12, marginBottom: 12 }}>{loadState.message}</p>{loadState.url && <a href={loadState.url} target="_blank" rel="noopener noreferrer" style={{ color: t.colors.primary, fontSize: 12, display: 'block', marginBottom: 12 }}>Open raw URL</a>}<button onClick={onRetry} style={{ padding: '6px 16px', background: t.colors.primary, color: '#fff', border: 'none', borderRadius: 4, cursor: 'pointer', fontSize: 12 }}>Retry</button></div>;
    // A 5xx/408/429 or network failure may pass: keep the Retry card. Anything else (4xx, an invalid manifest,
    // no URL) will not render on a retry either, so inside a panel the tab is dropped instead.
    if (isTransientLoadError(loadState.message)) return card;
    return <RenderFailure reason={`timeline: ${loadState.message}`} fallback={card} />;
  }

  // A manifest the parser flagged as having no usable timeline (duration 0, or a track with no time
  // range) degrades to a calm "no preview" state rather than the hard error. The analysis still
  // happened — its findings live in the node's data output — there is just nothing to play here.
  const unrenderable = (loadState.manifest as { unrenderable?: { reason?: string } }).unrenderable;
  if (unrenderable) {
    // Nothing to play: inside a panel the tab is dropped (the findings still live in the node's data output).
    return (
      <RenderFailure
        reason={`timeline: ${unrenderable.reason || 'no usable timeline'}`}
        fallback={(
        <div
          data-testid="timeline-no-preview"
          style={{ padding: 24, background: t.colors.surface, border: `1px solid ${t.colors.border}`, borderRadius: t.radius.panel, textAlign: 'center' }}
        >
          <p style={{ color: t.colors.textPrimary, marginBottom: 8 }}>No preview available</p>
          <p style={{ color: t.colors.textSecondary, fontSize: 12 }}>
            {unrenderable.reason || 'This render has no usable timeline.'}
          </p>
        </div>
        )}
      />
    );
  }
  const { manifest } = loadState;
  // #6 — face detection boxes ride on the composite as `annotations` (composeTimeline unions them).
  // Normalized: a manifest may carry the flat `{x, y, w, h, label, time}` detection shape instead of
  // `{entityId, start, end, box}` (session 61deed9b) — raw, it crashed the people lanes on `iv.start.toFixed`.
  const faceAnnotations = normalizeAnnotations((manifest as unknown as { annotations?: unknown }).annotations);
  // A manifest may declare a non-timeline template (e.g. PDF). The block is
  // emitted generically as nle-timeline, so dispatch here once we know the
  // real template from the loaded manifest.
  if (manifest.template === 'document-viewer') {
    return <DocumentViewer data={{ manifest }} title="Document" theme={t} />;
  }
  const declaredValid: ManifestTrack[] = manifest.tracks.filter((track: ManifestTrack) => VALID_TRACK_TYPES.has(track.type)).slice(0, MAX_TRACKS);
  // Top-level `src` fallback, as a SYNTHESIZED track.
  //
  // The old marker-strip template (`video-markers`) read `data.src` and needed no `tracks` at
  // all — the render agent even stamped `manifest["src"]` on that branch. Now that every
  // time-based analysis routes to THIS container, a stored manifest carrying only `src` must
  // still play, or it shows "No media source available": a blank tab for a perfectly good
  // video. Synthesizing a track (rather than threading a second src through the component)
  // means the video-vs-audio element choice, the lane gutter and the empty states all keep
  // working through the one existing path.
  //
  // mimeType decides the track type and gates the fallback, so a document/image manifest can
  // never be turned into a player.
  const looseSrc = (manifest as { src?: unknown }).src;
  const looseMime = String((manifest as { mimeType?: unknown }).mimeType ?? '');
  const synthesized: ManifestTrack[] =
    declaredValid.length === 0 && typeof looseSrc === 'string' && looseSrc &&
    (!looseMime || looseMime.startsWith('video/') || looseMime.startsWith('audio/'))
      ? [{
          id: 'src-fallback',
          type: looseMime.startsWith('audio/') ? 'audio' : 'video',
          src: looseSrc,
          label: String((manifest as { title?: unknown }).title ?? 'Source'),
          start: 0,
          end: typeof manifest.duration === 'number' ? manifest.duration : 0,
        } as ManifestTrack]
      : [];
  const validTracks: ManifestTrack[] = [...declaredValid, ...synthesized];
  const invalidTracks = manifest.tracks.filter((track: ManifestTrack) => !VALID_TRACK_TYPES.has(track.type));
  const videoTracks = validTracks.filter((tr: ManifestTrack) => tr.type === 'video');
  // Per-speaker diarization channels (e.g. speaker_00/01) are already shown as SPEAKER segment lanes;
  // rendering them ALSO as audio waveform lanes is redundant (and they arrive empty), so drop them
  // from the AUDIO group — the speaker LANES carry the who/when. Keep all real audio stems.
  const isSpeakerChannel = (tr: ManifestTrack) => /speaker[_-]?\d+/i.test(String(tr.id ?? '') + ' ' + String(tr.label ?? ''));
  const realAudio = validTracks.filter((tr: ManifestTrack) => tr.type === 'audio');
  const nonSpeakerAudio = realAudio.filter((tr: ManifestTrack) => !isSpeakerChannel(tr));
  // Drop per-speaker channels from the AUDIO group (they are redundant with the SPEAKER lanes),
  // EXCEPT when that would leave a video-less render with no playable audio at all. A standalone
  // diarized-speaker .wav opened from the grid (AssetPlayer) is ONE audio track whose label is the
  // asset's display name — which can itself contain "SPEAKER_01" — so the unscoped filter stripped
  // the only track and `primarySrc` collapsed to undefined → "No media source available" for a good
  // file. With a video present the speaker channels stay dropped, as before.
  const audioTracks = (videoTracks.length === 0 && nonSpeakerAudio.length === 0) ? realAudio : nonSpeakerAudio;
  const hasVideo = videoTracks.length > 0;
  const primarySrc = hasVideo ? videoTracks[0].src : audioTracks.length > 0 ? audioTracks[0].src : undefined;
  // Did this render actually draw anything on the timeline? Mirrors the lane conditional below
  // EXACTLY — kept adjacent so the two cannot drift apart.
  const hasLanes = audioTracks.length > 0
    || (manifest.markers?.length ?? 0) > 0
    || (manifest.segments?.some(s => s.group) ?? false)
    || faceAnnotations.length > 0;
  // Skills whose deliverable IS the media: no lanes is the correct, expected outcome for them,
  // so they must never be flagged. Anything else claims to analyse something.
  const PLAIN_MEDIA_SKILLS = new Set(['render-video', 'render-audio', 'render-image',
                                      'render-document', 'render-data', 'render-code']);
  const renderSkill = String((manifest as { renderSkill?: unknown }).renderSkill ?? '');
  const analysisProducedNoLanes = !!renderSkill && !PLAIN_MEDIA_SKILLS.has(renderSkill) && !hasLanes;
  // Multiple audio stems (and no video) → give each its own labeled player.
  const multiAudio = !hasVideo && audioTracks.length > 1;
  const showLanes = !multiAudio && (audioTracks.length > 0 || (manifest.markers?.length ?? 0) > 0 || (manifest.segments?.some(s => s.group) ?? false) || faceAnnotations.length > 0);
  const trackLabel = (tr: ManifestTrack, i: number): string => {
    const raw = (tr.label ?? tr.id) as string;
    return String(raw).replace(/[-_]/g, ' ').replace(/\b\w/g, c => c.toUpperCase()) || `Track ${i + 1}`;
  };
  // The element is the only thing that can measure an mp3's length: peak extraction is WAV-only
  // agent-side and the container probe only reads MP4/MOV, so those manifests carry an estimate.
  const reportDuration = (el: HTMLMediaElement | null) => {
    if (!el || !onMeasuredDuration) return;
    const d = el.duration;
    if (Number.isFinite(d) && d > 0) onMeasuredDuration(d);
  };
  const handleMediaRef = (el: HTMLMediaElement | null) => {
    mediaRef.current = el;
    // Component 5: report the element up (and null on unmount) so AssetPlayer can watch it.
    onMediaElement?.(el);
    if (el) {
      engine.attachMedia(el);
      const onLoadedMeta = () => {
        reportDuration(el);
        // Component 5: seek to `initialTime` (paused) on the FIRST load only. We set
        // `el.currentTime` directly rather than `engine.seek`, because the engine clamps to its
        // `state.duration`, which can still be 0 at this instant (the measured duration has not
        // yet propagated); the engine's sync loop then adopts the element's position.
        if (initialTime != null && !didInitialSeekRef.current) {
          didInitialSeekRef.current = true;
          const d = el.duration;
          const target = Number.isFinite(d) && d > 0 ? Math.min(initialTime, d) : initialTime;
          if (Number.isFinite(target) && target >= 0) {
            try { el.currentTime = target; } catch { /* not seekable yet */ }
          }
        }
      };
      el.addEventListener('loadedmetadata', onLoadedMeta);
      el.addEventListener('durationchange', () => reportDuration(el));
      reportDuration(el);
    } else {
      engine.detachMedia();
    }
  };
  const handleSeek = (time: number) => { engine.seek(time); onInteraction?.({ type: 'scrub', target: 'timeline', data: { time }, timestamp: Date.now() }); };
  const handleClipSelect = (clipId: string) => { engine.selectClip(clipId); const clip = manifest.clips.find(c => c.id === clipId); if (clip) { onInteraction?.({ type: 'select', target: `clip-${clipId}`, data: { clipId, start: clip.start, end: clip.end }, timestamp: Date.now() }); } };
  // Crop: emit a single-video crop action. The normalized box is baked into the prompt (human
  // numbers) AND carried structured in data.box; WorkspaceView appends `[source: src]`. The agent
  // runs crop_video.py with the box → a new cropped clip that auto-renders as its own tab.
  const emitCrop = (src: string) => (box: CropBox) => {
    const prompt = buildCropPrompt(box);
    const v = mediaRef.current as HTMLVideoElement | null;
    const aspect = v ? cropAspectLabel(box, v.videoWidth, v.videoHeight) : undefined;
    // A range marked on the timeline limits the crop to that span (the first one, as the crop runs one pass).
    const st = engine.getState();
    const marked = [...st.ranges.map(r => ({ in: r.in, out: r.out })),
      ...(st.markIn != null && st.markOut != null ? [{ in: st.markIn, out: st.markOut }] : [])].filter(r => r.out > r.in);
    const data: CropActionData = { verb: 'crop', prompt, src, box, ...(aspect ? { aspect } : {}),
      ...(marked.length ? { ranges: marked.slice(0, 1) } : {}) };
    onInteraction?.({ type: 'action', target: 'crop', data, timestamp: Date.now() });
    setShowCrop(false);
    onCropChange?.(null);
  };
  const closeCrop = () => { setShowCrop(false); onCropChange?.(null); };
  const selectedClip = state.selectedClipId ? manifest.clips.find(c => c.id === state.selectedClipId) ?? null : null;
  const selectedCue = state.selectedCueIndex !== null && manifest.captions ? manifest.captions[state.selectedCueIndex] ?? null : null;
  // Default the inspector to the first selectable item when nothing is
  // explicitly selected — replaces the "Select an item…" placeholder (which
  // read as leftover scaffolding) with real metadata. See renderOrchestration.
  const inspectorSelection = computeInspectorSelection(manifest.clips, manifest.captions, selectedClip, selectedCue);
  // Snap targets for the editor's drag/handles: boundaries from shot markers,
  // segment lanes, and clips — so marking lands on real cut points, not a
  // pixel-approximate time. De-duped; only meaningful values.
  const snapTimes = Array.from(new Set<number>([
    // The file's keyframes: a range that starts on one is cut as a stream copy — instant, and exact.
    ...keyframes,
    ...(manifest.markers ?? []).flatMap(m => [m.time, ...(typeof m.endTime === 'number' ? [m.endTime] : [])]),
    ...(manifest.segments ?? []).flatMap(s => [s.start, s.end]),
    ...(manifest.clips ?? []).flatMap(c => [c.start, c.end]),
  ].filter(n => typeof n === 'number' && Number.isFinite(n))));
  // Stable key for persisting per-group collapse state (composite lanes).
  const grpKey = String((manifest as { sourceKey?: string }).sourceKey || manifest.id || '');
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8, background: t.colors.surface, border: `1px solid ${t.colors.border}`, borderRadius: t.radius.panel, overflow: 'hidden' }}>
      {multiAudio ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10, padding: 12, background: t.colors.background }}>
          {/* Independent audio clips (e.g. TTS chunks, stems) → the editable clip gallery:
              each gets a player + waveform + its own mark-in/out ruler, plus a batch
              "Trim all marked". These tracks were never on a shared axis here anyway (the
              multiAudio layout always showed separate players), so this only ADDS trimming.
              Single-audio / video keep the TimelineEditor below. */}
          <ClipGallery
            data={{
              title: (manifest as { title?: string }).title || 'Audio clips',
              subtitle: 'Mark a keep-range on each clip, then “Trim all marked”.',
              galleryClips: audioTracks.map((track, i) => ({
                id: track.id,
                label: trackLabel(track, i),
                src: track.src,
                kind: 'audio' as const,
                peaks: manifest.peaks?.[track.id],
                durationHint: track.end - track.start > 0 ? track.end - track.start : undefined,
              })),
            }}
            title={(manifest as { title?: string }).title || 'Audio clips'}
            theme={t}
            onInteraction={onInteraction}
          />
          {/* Subtitles as a clickable transcript — the audio-only layout has
              no video to overlay CaptionRenderer on, so we surface the parsed
              caption cues here (speaker + text, seek on click, active cue
              highlighted). Without this, subtitles parsed by the render agent
              would never be visible for diarization / stem results. */}
          {manifest.captions && manifest.captions.length > 0 && (
            <div data-testid="subtitle-transcript" style={{ background: t.colors.surface, border: `1px solid ${t.colors.border}`, borderRadius: 8, padding: 12 }}>
              <div style={{ fontSize: 11, fontWeight: 600, color: t.colors.textSecondary, textTransform: 'uppercase', letterSpacing: 0.4, marginBottom: 6 }}>Subtitles</div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 2, maxHeight: 220, overflowY: 'auto' }}>
                {manifest.captions.map((cue, i) => {
                  const active = state.currentTime >= cue.start && state.currentTime < cue.end;
                  return (
                    <div
                      key={i}
                      onClick={() => handleSeek(cue.start)}
                      style={{ cursor: 'pointer', padding: '4px 6px', borderRadius: 4, background: active ? t.colors.activeBg : 'transparent', fontSize: 12, color: t.colors.textPrimary, lineHeight: 1.4 }}
                    >
                      {cue.speaker && <span style={{ fontWeight: 600, color: t.colors.textSecondary, marginRight: 6 }}>{cue.speaker}</span>}
                      {cue.text}
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      ) : (
        <div style={{ position: 'relative', background: hasVideo ? '#000' : t.colors.background }}>
          {hasVideo && primarySrc ? (
            <>
              {/* Cap the display at a 16:9 frame with black padding: a vertical (9:16) or square
                  clip is letterboxed inside the 16:9 box (objectFit: contain), NOT scaled up to
                  the full pane width — which upscales a narrow source and looks blurry. A real
                  16:9 clip fills the box exactly, so the face-overlay mapping is unchanged. */}
              <div style={{ position: 'relative', width: '100%', aspectRatio: '16 / 9', background: '#000' }}>
                <video ref={handleMediaRef as (el: HTMLVideoElement | null) => void} src={primarySrc} controls
                  style={{ width: '100%', height: '100%', display: 'block', objectFit: 'contain' }} /></div>
              {faceAnnotations.length > 0 && (
                <>
                  {showFaces && (
                    <div data-testid="face-overlay" style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }}>
                      {activeBoxesAt(faceAnnotations, state.currentTime).map(({ entityId, box }) => (
                        <div key={entityId} data-testid={`face-box-${entityId}`} style={{ position: 'absolute', left: `${box.x * 100}%`, top: `${box.y * 100}%`, width: `${box.width * 100}%`, height: `${box.height * 100}%`, border: `2px solid ${t.colors.primary}`, borderRadius: t.radius.badge, boxShadow: '0 0 0 1px rgba(0,0,0,0.4)' }}>
                          <span style={{ position: 'absolute', top: -18, left: -2, maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', background: t.colors.primary, color: '#fff', fontSize: t.typography.sizes.badge, fontWeight: 600, padding: '1px 5px', borderRadius: t.radius.badge }}>{personLabel(entityId, faceAnnotations.find(a => a.entityId === entityId))}</span>
                        </div>
                      ))}
                    </div>
                  )}
                  <button data-testid="toggle-faces" aria-pressed={showFaces} onClick={() => setShowFaces(v => !v)} title="Toggle face boxes on the video"
                    style={{ position: 'absolute', top: 8, right: 8, zIndex: 4, padding: '4px 9px', borderRadius: t.radius.button, border: 'none', background: showFaces ? t.colors.primary : 'rgba(0,0,0,0.55)', color: '#fff', fontSize: t.typography.sizes.meta, fontWeight: 600, cursor: 'pointer', display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                    {/* Fixed-width slot: the check appears without the label moving. */}
                    <span style={{ display: 'inline-flex', width: 14, height: 14 }}>{showFaces && <CheckIcon size={14} />}</span>Faces
                  </button>
                </>
              )}
              <button data-testid="toggle-crop" aria-pressed={showCrop} onClick={() => (showCrop ? closeCrop() : setShowCrop(true))} title="Crop the video to a region"
                style={{ position: 'absolute', top: 8, left: 8, zIndex: 6, padding: '4px 9px', borderRadius: t.radius.button, border: 'none', background: showCrop ? t.colors.primary : 'rgba(0,0,0,0.55)', color: '#fff', fontSize: t.typography.sizes.meta, fontWeight: 600, cursor: 'pointer', display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                {showCrop ? <CloseIcon size={14} /> : <CropIcon size={14} />}Crop
              </button>
              {showCrop && <CropOverlay videoRef={mediaRef} theme={t} onCommit={emitCrop(primarySrc)} onCancel={closeCrop} initialBox={cropBox} onChange={onCropChange ? (box) => onCropChange(box) : undefined} />}
            </>
          ) : primarySrc ? (
            <div style={{ padding: 16 }}>
              <audio ref={(el) => { (handleMediaRef as (e: HTMLAudioElement | null) => void)(el); watchCorsRefusal(el); applyMediaGain(el, audioTracks[0] ? manifest.peaks?.[audioTracks[0].id] : undefined); }} crossOrigin={mediaCrossOrigin ? gainCrossOrigin(primarySrc) : undefined} src={primarySrc} controls style={{ width: '100%' }} />
              {audioTracks.length > 0 && manifest.peaks && manifest.peaks[audioTracks[0].id] && (<div style={{ height: 56, overflow: 'hidden' }}><WaveformVisualizer peaks={manifest.peaks[audioTracks[0].id]} duration={manifest.duration} currentTime={state.currentTime} zoomLevel={state.zoomLevel} scrollOffset={0} onSeek={handleSeek} /></div>)}
            </div>
          ) : (<div style={{ padding: 24, textAlign: 'center', color: t.colors.textSecondary }}>No media source available</div>)}
          {hasVideo && manifest.captions && manifest.captions.length > 0 && (<CaptionRenderer cues={manifest.captions} currentTime={state.currentTime} visible={true} speakerColors={new Map()} />)}
        </div>
      )}
      {/* Professional editor surface: always-on for a video (or single-audio)
          render — ruler, draggable playhead, mark-in/out + range band, and the
          action bar whose verbs compose an agent request. This is what turns a
          bare <video> into the interactive timeline; see TimelineEditor. */}
      {!multiAudio && primarySrc && state.duration > 0 && (
        <>
          {hasVideo && <FpsProbe src={primarySrc} onFps={setFps} />}
          <TimelineEditor engine={engine} state={state} duration={state.duration} src={primarySrc} theme={t} onInteraction={onInteraction} snapTimes={snapTimes}
            view={timelineView} gutter={showLanes ? LANE_GUTTER : 0}
            fps={hasVideo ? fps : null} hoverTime={hoverTime} onHover={setHoverTime} peekFrames={hasVideo} />
        </>
      )}
      {!multiAudio && !hasVideo && manifest.captions && manifest.captions.length > 0 && (
        <div data-testid="subtitle-transcript" style={{ background: t.colors.surface, border: `1px solid ${t.colors.border}`, borderRadius: 8, padding: 12, margin: '0 12px 8px' }}>
          <div style={{ fontSize: 11, fontWeight: 600, color: t.colors.textSecondary, textTransform: 'uppercase', letterSpacing: 0.4, marginBottom: 6 }}>Transcript</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2, maxHeight: 220, overflowY: 'auto' }}>
            {manifest.captions.map((cue, i) => {
              const active = state.currentTime >= cue.start && state.currentTime < cue.end;
              return (
                <div
                  key={i}
                  onClick={() => handleSeek(cue.start)}
                  style={{ cursor: 'pointer', padding: '4px 6px', borderRadius: 4, background: active ? t.colors.activeBg : 'transparent', fontSize: 12, color: t.colors.textPrimary, lineHeight: 1.4 }}
                >
                  {cue.speaker && <span style={{ fontWeight: 600, color: t.colors.textSecondary, marginRight: 6 }}>{cue.speaker}</span>}
                  {cue.text}
                </div>
              );
            })}
          </div>
        </div>
      )}
      {invalidTracks.length > 0 && (<div style={{ padding: '4px 12px', fontSize: 11, color: '#c9a227' }}>Skipped {invalidTracks.length} track(s) with invalid type: {invalidTracks.map(tr => tr.id).join(', ')}</div>)}
      {/* Unified timeline (#1): audio waveforms, shots, and speaker lanes share ONE gutter + one
          zoomable/scrollable time track, so they line up and zoom together. */}
      {showLanes && (
        <UnifiedTimelineLanes
          view={timelineView}
          audioTracks={audioTracks}
          peaks={manifest.peaks ?? {}}
          markers={manifest.markers ?? []}
          segments={manifest.segments ?? []}
          annotations={faceAnnotations}
          duration={manifest.duration}
          currentTime={state.currentTime}
          onSeek={handleSeek}
          trackLabel={trackLabel}
          mediaRef={mediaRef}
          hoverTime={hoverTime}
          onHover={setHoverTime}
          fps={hasVideo ? fps : null}
        />
      )}
      {manifest.segments && manifest.segments.length > 0 && (
        <SegmentTable segments={manifest.segments} currentTime={state.currentTime} onSeek={handleSeek} />
      )}
      {manifest.metadata && Object.keys(manifest.metadata).length > 0 && (
        <MetadataPanel metadata={manifest.metadata} />
      )}
      {manifest.clips.length > 0 && (<ClipManager clips={manifest.clips} tracks={manifest.tracks} onSeek={handleSeek} onOpenRender={(clip) => { onInteraction?.({ type: 'select', target: `clip-${clip.id}`, data: { clipId: clip.id, start: clip.start, end: clip.end, src: primarySrc }, timestamp: Date.now() }); }} onSelect={handleClipSelect} />)}
      {inspectorSelection && <InspectorPanel selection={inspectorSelection} />}
      {/* An analysis that produced NOTHING must say so.
          Now that every time-based analysis shares this container, a projection that yields no
          markers, no grouped segments and no boxes would otherwise render as a plain player —
          indistinguishable from "the video, as requested". That is the silent-degradation trap
          this codebase keeps paying for (the face render that never appeared, nodes reporting
          artifacts:0, a completed node classified kind=error). A uniform container should make
          failure GRACEFUL, not invisible: the media still plays, and the empty result is stated.
          Keyed on `renderSkill`, so a plain video/audio/image render — which legitimately has no
          lanes — never shows it. */}
      {analysisProducedNoLanes && (
        <div
          data-testid="analysis-no-lanes"
          style={{ margin: '4px 12px 0', padding: '8px 10px', borderRadius: t.radius.badge,
                   border: `1px solid ${t.colors.border}`, background: t.colors.surfaceSubtle ?? t.colors.surface,
                   color: t.colors.textSecondary, fontSize: 12 }}
        >
          This analysis produced no timeline lanes — the media is shown unchanged.
        </div>
      )}
      {validTracks.length === 0 && (<div style={{ padding: 24, textAlign: 'center', color: t.colors.textSecondary, fontSize: 12 }}>No media tracks available</div>)}
    </div>
  );
}

export default function NleTimeline({ data, theme: t, onInteraction }: RendererProps<NleTimelineData>) {
  const [loadState, setLoadState] = useState<LoadState>(() => data.manifest ? { status: 'loaded', manifest: data.manifest } : { status: 'loading' });
  // A manifest may say its duration is a guess (durationIsEstimate) -- an mp3 track cannot be
  // measured agent-side, so it arrives as a fixed 600s hint. The media element knows the truth
  // once loaded, so prefer that over the estimate rather than drawing a 5s clip on a 10-minute
  // timeline.
  const [measuredDuration, setMeasuredDuration] = useState(0);
  const manifestDuration = loadState.status === 'loaded' ? loadState.manifest.duration : 0;
  const durationIsEstimate = loadState.status === 'loaded'
    && Boolean((loadState.manifest as { durationIsEstimate?: boolean }).durationIsEstimate);
  const effectiveDuration = durationIsEstimate && measuredDuration > 0
    ? measuredDuration
    : manifestDuration;
  const { engine, state } = useTimelineEngine(effectiveDuration);
  const mediaRef = useRef<HTMLMediaElement | null>(null);
  const retryCountRef = useRef(0);
  const fetchManifest = useCallback(async () => {
    if (data.manifest) { setLoadState({ status: 'loaded', manifest: data.manifest }); return; }
    if (!data.manifestUrl) { setLoadState({ status: 'error', message: 'No manifest URL provided', url: '' }); return; }
    setLoadState({ status: 'loading' });
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), MANIFEST_LOAD_TIMEOUT_MS);
    try {
      // authFetch, not fetch: the manifest is served by the auth-protected
      // /api/artifact proxy route, so it needs the bearer token. (Media
      // inside the manifest — track.src etc. — uses direct public artifacts-
      // CDN URLs and is loaded via element src, so it needs no auth.) Plain
      // fetch here 401'd in the hosted deployment; local dev masked it via
      // DEV_BYPASS_AUTH.
      const res = await authFetch(data.manifestUrl, { signal: controller.signal });
      clearTimeout(timeout);
      if (!res.ok) { setLoadState({ status: 'error', message: `Failed: ${res.status}`, url: data.manifestUrl }); return; }
      const json = await res.text();
      const result = parseManifest(json);
      if (!result.ok) { setLoadState({ status: 'error', message: `Invalid manifest: ${result.errors.map(e => e.message).join('; ')}`, url: data.manifestUrl }); return; }
      setLoadState({ status: 'loaded', manifest: result.manifest });
    } catch (err) {
      clearTimeout(timeout);
      const message = err instanceof Error && err.name === 'AbortError' ? 'Manifest loading timed out (10s)' : `Failed: ${err instanceof Error ? err.message : String(err)}`;
      setLoadState({ status: 'error', message, url: data.manifestUrl });
    }
  }, [data.manifestUrl, data.manifest]);
  useEffect(() => {
    // React to data changes, not just mount: when the block's inline manifest changes (e.g. a
    // solo tab folds into a composite in place, or a tab is swapped), refresh loadState from it.
    // Previously loadState was only set by the useState initializer + a fetch that ran only when
    // there was NO inline manifest, so an inline-manifest CHANGE was silently ignored — the tab
    // kept rendering the stale manifest (the composite-fold "no video / audio-only" bug).
    if (data.manifest) { setLoadState({ status: 'loaded', manifest: data.manifest }); return; }
    fetchManifest();
  }, [fetchManifest, data.manifest]);
  const handleInteraction = useCallback((event: InteractionEvent) => { onInteraction?.(event); }, [onInteraction]);
  useEffect(() => engine.onInteraction(handleInteraction), [engine, handleInteraction]);

  // Persisted view state (R8.2): restore the selected range, frame marker and crop box once the
  // timeline has a duration (ranges are clamped to it), then report every change. The restored
  // state is the baseline, so mounting never writes back what it just read.
  const persistedView = data.viewState !== undefined
    ? normalizeViewState(data.viewState)
    : loadState.status === 'loaded' ? normalizeViewState((loadState.manifest as { viewState?: unknown }).viewState) : undefined;
  const persistedViewRef = useRef(persistedView);
  persistedViewRef.current = persistedView;
  // `viewReady` flips in the render AFTER the restore, so the change reporter never sees the
  // pre-restore (empty) engine state and mistakes it for the user clearing everything.
  const [viewReady, setViewReady] = useState(false);
  const lastViewRef = useRef<RenderViewState | undefined>(undefined);
  const [cropBox, setCropBox] = useState<CropBox | null>(null);
  const [restoredCrop, setRestoredCrop] = useState<CropBox | null>(null);
  useEffect(() => {
    if (viewReady || loadState.status !== 'loaded' || state.duration <= 0) return;
    const vs = persistedViewRef.current;
    lastViewRef.current = vs ?? {};
    if (vs?.range) engine.addRange(vs.range.start, vs.range.end);
    if (vs?.frameMs !== undefined) engine.addPoint(vs.frameMs / 1000);
    if (vs?.crop) { setCropBox(vs.crop); setRestoredCrop(vs.crop); }
    // No persisted state: fall back to the per-browser marks (below), restored HERE so they too wait
    // for the duration. A measured-duration manifest (AssetPlayer: `duration: 0` +
    // `durationIsEstimate`) has a 0-length engine until the media loads, and restoring into it
    // clamped every range to zero length (rejected) and every marker to 0 — which the change
    // reporter then PATCHed over the stored view (staging 2026-10-09, session 9d649e32).
    if (!vs) restoreEditsRef.current();
    setViewReady(true);
  }, [viewReady, loadState.status, state.duration, engine]);
  const onViewStateChange = data.onViewStateChange;
  useEffect(() => {
    if (!onViewStateChange || !viewReady) return;
    const range = state.ranges[0];
    const point = state.points[0];
    const vs: RenderViewState = {};
    if (range && range.out > range.in) vs.range = { start: range.in, end: range.out };
    if (point) vs.frameMs = Math.round(point.t * 1000);
    if (cropBox) vs.crop = cropBox;
    if (sameViewState(lastViewRef.current, vs)) return;
    lastViewRef.current = vs;
    onViewStateChange(vs);
  }, [viewReady, state.ranges, state.points, cropBox, onViewStateChange]);

  // Persisted edit state (Level-2): a session's marks/ranges/markers survive a
  // reload and can be referenced across turns ("the segment I marked earlier").
  // Keyed by the artifact's stable identity (sourceKey → composite id → manifest
  // url) so each video keeps its OWN marks. Ephemeral marks the user cleared are
  // removed. localStorage only (per-browser) — deliberately not the session row.
  const editKey = loadState.status === 'loaded'
    ? String((loadState.manifest as { sourceKey?: string }).sourceKey || loadState.manifest.id || data.manifestUrl || '')
    : '';
  const editsRestoredRef = useRef(false);
  // Called once, by the duration-gated restore above, only when the view has no persisted state
  // (a view that carries its own restores from that — restoring both would duplicate the range).
  const restoreEditsRef = useRef<() => void>(() => {});
  restoreEditsRef.current = () => {
    if (editsRestoredRef.current || !editKey) return;
    editsRestoredRef.current = true;
    try {
      const saved = JSON.parse(localStorage.getItem(`nle-edits-${editKey}`) || 'null') as
        { ranges?: { in: number; out: number }[]; points?: { t: number }[] } | null;
      if (saved) {
        (saved.ranges ?? []).forEach(r => engine.addRange(r.in, r.out));
        (saved.points ?? []).forEach(p => engine.addPoint(p.t));
      }
    } catch { /* ignore malformed cache */ }
  };
  useEffect(() => {
    // Not before the restore: the pre-restore (empty) engine would erase the saved marks.
    if (!editKey || !viewReady) return;
    const id = setTimeout(() => {
      try {
        const payload = {
          ranges: state.ranges.map(r => ({ in: r.in, out: r.out })),
          points: state.points.map(p => ({ t: p.t })),
        };
        if (payload.ranges.length || payload.points.length) {
          localStorage.setItem(`nle-edits-${editKey}`, JSON.stringify(payload));
        } else {
          localStorage.removeItem(`nle-edits-${editKey}`);
        }
      } catch { /* quota — ignore */ }
    }, 400);
    return () => clearTimeout(id);
  }, [editKey, viewReady, state.ranges, state.points]);

  const handleRetry = useCallback(() => { retryCountRef.current += 1; fetchManifest(); }, [fetchManifest]);
  // A render-time throw: inside a panel the tab is dropped; standalone the card (with Retry) still shows.
  const renderFallback = (error: Error, retry: () => void) => (
    <RenderFailure reason={`timeline threw: ${error.message}`} fallback={<div style={{ padding: 24, background: t.colors.surface, border: `1px solid ${t.colors.border}`, borderRadius: t.radius.panel, textAlign: 'center' }}><p style={{ color: t.colors.textPrimary, marginBottom: 8 }}>An error occurred while rendering the timeline</p><p style={{ color: t.colors.textSecondary, fontSize: 12, marginBottom: 12 }}>{error.message}</p>{data.manifestUrl && <a href={data.manifestUrl} target="_blank" rel="noopener noreferrer" style={{ color: t.colors.primary, fontSize: 12, display: 'block', marginBottom: 12 }}>Open raw manifest</a>}<button onClick={retry} style={{ padding: '6px 16px', background: t.colors.primary, color: '#fff', border: 'none', borderRadius: 4, cursor: 'pointer', fontSize: 12 }}>Retry</button></div>} />
  );
  return (<NleErrorBoundary fallback={renderFallback}><NleTimelineContent loadState={loadState} engine={engine} state={state} theme={t} mediaRef={mediaRef} manifestUrl={data.manifestUrl} onRetry={handleRetry} onInteraction={onInteraction} onMeasuredDuration={setMeasuredDuration} initialTime={data.initialTime} onMediaElement={data.onMediaElement} cropBox={cropBox} restoredCrop={restoredCrop} onCropChange={setCropBox} mediaCrossOrigin={data.mediaCrossOrigin} /></NleErrorBoundary>);
}

/** Measures the video's frame rate (no UI): a component, because the source is only known after NleTimelineContent's
 *  early returns, where a hook cannot be called. */
function FpsProbe({ src, onFps }: { src?: string; onFps: (fps: number | null) => void }) {
  const fps = useVideoFps(src, !!src);
  useEffect(() => { onFps(fps); }, [fps, onFps]);
  return null;
}
