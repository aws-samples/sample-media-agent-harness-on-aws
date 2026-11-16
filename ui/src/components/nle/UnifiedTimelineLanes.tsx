import { snapToFrame } from './frameTime';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import type { ManifestTrack, ManifestMarker, ManifestSegment } from './manifestParser';
import { WaveformVisualizer } from './WaveformVisualizer';
import { groupAnnotationsByEntity, personLabel } from '../renderer/annotations';
import type { Annotation } from '../renderer/annotations';
import { MAX_ZOOM, useTimelineView, useWheelZoom, type TimelineView } from './timelineView';

/**
 * UnifiedTimelineLanes — ONE shared horizontal time scale for every lane (#1) + per-stem solo (#2).
 *
 * #1: every row is [fixed sticky label gutter] + [time track], all inside ONE horizontally-scrollable
 * container whose inner width = `zoom × 100%`, so all lanes share the same time→x mapping (they line
 * up) and one zoom control scales them all together. That zoom/scroll is the SAME `TimelineView` as the
 * editor's ruler above (NleTimeline passes it in), so wheel-zooming either one zooms both, scrolling one
 * scrolls the other, and the ruler's track has these lanes' gutter in front of it so the two line up.
 *
 * #2 (mute/solo): the audio stems were waveform-only; here each stem has a hidden <audio> synced to
 * the video (mediaRef), and a solo button per lane. Soloing a stem mutes the video + the other stems
 * and plays ONLY that stem in sync with the playhead — so you can listen to just speech, music, etc.
 */
interface Props {
  audioTracks: ManifestTrack[];
  peaks: Record<string, number[]>;
  markers: ManifestMarker[];
  segments: ManifestSegment[];
  duration: number;
  currentTime: number;
  onSeek: (t: number) => void;
  trackLabel: (tr: ManifestTrack, i: number) => string;
  mediaRef: React.RefObject<HTMLMediaElement | null>;
  annotations?: Annotation[];
  /** Ignored: the stem players only play (no Web Audio), so they ask for no CORS (mediaCors.ts). */
  mediaCrossOrigin?: boolean;
  /** The zoom/scroll shared with the editor's ruler; a local one when absent. */
  view?: TimelineView;
  /** The hovered time shared with the ruler (one line across every lane), and the report of it. */
  hoverTime?: number | null;
  onHover?: (time: number | null) => void;
  /** The video's frame rate when known: the hover lands on the frame under the pointer. */
  fps?: number | null;
}

/** The label column in front of every lane. The editor's ruler reserves the same width to line up. */
export const LANE_GUTTER = 104;
const GUTTER = LANE_GUTTER;
const SHOT_COLORS: Record<string, string> = { content: '#3b6ea5', black: '#4b5158', scene: '#3fae8f', cut: '#3fae8f', event: '#c99a3b' };
const SHOT_PALETTE = ['#3b6ea5', '#3fae8f', '#c99a3b', '#8b6fd0', '#c76b6b', '#4b5158'];
const SPEAKER_HUES = [210, 150, 32, 280, 0, 180, 260, 100];

function shotColor(m: ManifestMarker, i: number): string {
  if (m.color) return m.color;
  const cat = (m.category || '').toLowerCase();
  if (SHOT_COLORS[cat]) return SHOT_COLORS[cat];
  let h = 0; const key = cat || String(i);
  for (let k = 0; k < key.length; k++) h = (h * 31 + key.charCodeAt(k)) >>> 0;
  return SHOT_PALETTE[h % SHOT_PALETTE.length];
}
function tickStep(dur: number): number {
  if (dur <= 0) return 1;
  const target = dur / 8;
  const pows = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];
  return pows.find((p) => p >= target) ?? 3600;
}
function fmt(t: number): string {
  const m = Math.floor(t / 60); const s = Math.floor(t % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}

function LaneRow({ label, height, children, testId, action }: { label: string; height: number; children: React.ReactNode; testId?: string; action?: React.ReactNode }) {
  return (
    <div style={{ display: 'flex', alignItems: 'stretch', borderTop: '1px solid var(--color-border)' }} data-testid={testId}>
      <div style={{ width: GUTTER, flexShrink: 0, position: 'sticky', left: 0, zIndex: 3, background: 'var(--color-background)', display: 'flex', alignItems: 'center', gap: 4, padding: '0 8px', fontSize: 'var(--fs-xs)', color: 'var(--color-text-secondary)', borderRight: '1px solid var(--color-border)' }}>
        {action}
        <span style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }} title={label}>{label}</span>
      </div>
      <div style={{ flex: 1, minWidth: 0, position: 'relative', height }} data-testid="lane-track">{children}</div>
    </div>
  );
}

export default function UnifiedTimelineLanes({ audioTracks, peaks, markers: rawMarkers, segments: rawSegments, duration, currentTime, onSeek, trackLabel, mediaRef, annotations: rawAnnotations = [], view: sharedView, hoverTime = null, onHover, fps = null }: Props) {
  // Rows without finite times are skipped, not rendered: one bad row used to throw on `.toFixed` and take the
  // whole tab down with it (session 61deed9b).
  const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
  const markers = rawMarkers.filter((m) => isNum(m.time));
  const segments = rawSegments.filter((s) => isNum(s.start) && isNum(s.end));
  const annotations = rawAnnotations.filter((a) => isNum(a.start) && isNum(a.end));
  const localView = useTimelineView();
  const view = sharedView ?? localView;
  const { zoom } = view;
  const [soloId, setSoloId] = useState<string | null>(null);
  const [scrollEl, setScrollEl] = useState<HTMLDivElement | null>(null);
  const registerScroll = view.register('lanes', GUTTER);
  const scrollRef = useCallback((el: HTMLDivElement | null) => { registerScroll(el); setScrollEl(el); }, [registerScroll]);
  useWheelZoom(view, scrollEl);
  const audioRefs = useRef<Record<string, HTMLAudioElement | null>>({});
  const dur = duration > 0 ? duration : 1;
  const pct = (t: number) => Math.min(100, Math.max(0, (t / dur) * 100));
  const playheadPct = pct(currentTime);

  const groupNames = Array.from(new Set(segments.filter((s) => s.group).map((s) => s.group as string)));
  // People lanes (#6) — face annotations grouped per entity into merged on-screen intervals.
  const peopleLanes = Array.from(groupAnnotationsByEntity(annotations).entries()).map(([entityId, list]) => {
    const sorted = [...list].sort((a, b) => a.start - b.start);
    const intervals: { start: number; end: number }[] = [];
    for (const a of sorted) {
      const last = intervals[intervals.length - 1];
      if (last && a.start <= last.end + 0.5) last.end = Math.max(last.end, a.end);
      else intervals.push({ start: a.start, end: a.end });
    }
    const first = (list[0] ?? {}) as { name?: string; label?: string };
    return { entityId, label: personLabel(entityId, first), intervals };
  })
    // A person the manifest already lays out as a segment group (shot-segmentation timelines carry both
    // `segments` grouped by Person_N and the face `annotations` behind them) gets ONE lane, not two.
    .filter((p) => !groupNames.includes(p.label) && !groupNames.includes(p.entityId));
  // Marker bands: untagged markers are the Shots band (as before); each named set (`laneSet`) gets its own.
  const markerBands = Array.from(markers.reduce((acc, m) => {
    const set = typeof m.laneSet === 'string' ? m.laneSet : '';
    acc.set(set, [...(acc.get(set) ?? []), m]);
    return acc;
  }, new Map<string, ManifestMarker[]>())).map(([set, list]) => ({ set, list })).sort((a, b) => (a.set ? 1 : 0) - (b.set ? 1 : 0));
  const shotMarkers = markerBands.find((b) => !b.set)?.list ?? [];
  const looksLikeShots = shotMarkers.length > 0 && shotMarkers.every(
    (m) => /shot|content|black|scene|cut/i.test(String(m.category ?? '') + ' ' + String(m.label ?? '')),
  );
  const clock = (t: number) => `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, '0')}`;
  const step = tickStep(dur / zoom);
  const ticks: number[] = [];
  for (let t = 0; t <= dur + 0.001; t += step) ticks.push(t);

  const seekFromEvent = (e: React.MouseEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    if (r.width <= 0) return;
    onSeek(Math.max(0, Math.min(dur, ((e.clientX - r.left) / r.width) * dur)));
  };

  // #2 — keep the soloed stem's <audio> in sync with the video and mute everything else.
  const syncAudio = useCallback(() => {
    const v = mediaRef.current;
    if (v) v.muted = soloId !== null; // soloing a stem mutes the video's own audio
    for (const tr of audioTracks) {
      const a = audioRefs.current[tr.id];
      if (!a) continue;
      const on = soloId === tr.id;
      a.muted = !on;
      if (on && v) {
        const target = Math.max(0, v.currentTime - (tr.start || 0));
        if (Math.abs(a.currentTime - target) > 0.3) a.currentTime = target;
        if (v.paused) a.pause(); else void a.play().catch(() => {});
      } else {
        a.pause();
      }
    }
  }, [soloId, audioTracks, mediaRef]);

  useEffect(() => {
    syncAudio();
    const v = mediaRef.current;
    if (!v) return;
    const evs = ['play', 'pause', 'seeking', 'seeked', 'ratechange', 'timeupdate'];
    const handler = () => syncAudio();
    evs.forEach((e) => v.addEventListener(e, handler));
    return () => evs.forEach((e) => v.removeEventListener(e, handler));
  }, [syncAudio, mediaRef]);

  return (
    <div data-testid="unified-timeline" style={{ borderTop: '1px solid var(--color-border)' }}>
      {/* Hidden per-stem players (solo playback). Muted by default; the soloed one is unmuted + synced. */}
      {audioTracks.map((tr) => (
        <audio key={tr.id} data-testid={`stem-audio-${tr.id}`} ref={(el) => { audioRefs.current[tr.id] = el; }} src={tr.src} preload="none" muted style={{ display: 'none' }} />
      ))}

      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '6px 12px' }}>
        <span style={{ fontSize: 'var(--fs-xs)', fontWeight: 600, color: 'var(--color-text-secondary)', textTransform: 'uppercase', letterSpacing: 0.4 }}>Timeline{soloId ? ' · soloing 1 stem' : ''}</span>
        <div style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
          <button data-testid="zoom-out" aria-label="Zoom out" onClick={() => view.zoomBy(1 / 1.5)} disabled={zoom <= 1} style={zoomBtn}>−</button>
          <span data-testid="zoom-level" style={{ fontSize: 'var(--fs-xs)', color: 'var(--color-text-secondary)', minWidth: 28, textAlign: 'center' }}>{+zoom.toFixed(1)}×</span>
          <button data-testid="zoom-in" aria-label="Zoom in" onClick={() => view.zoomBy(1.5)} disabled={zoom >= MAX_ZOOM} style={zoomBtn}>+</button>
        </div>
      </div>
      <div ref={scrollRef} data-testid="lanes-scroll" style={{ overflowX: 'auto', overflowY: 'hidden' }}>
        {/* The TRACK (not the row) is zoom × the visible track, so its width equals the ruler's. */}
        <div data-testid="lanes-inner" style={{ position: 'relative', width: `calc(${zoom} * (100% - ${GUTTER}px) + ${GUTTER}px)`, minWidth: '100%' }}
          onPointerMove={onHover ? (e) => {
            // The time track starts after the label column; over the labels there is no time to show.
            const r = e.currentTarget.getBoundingClientRect();
            const w = r.width - GUTTER;
            const x = e.clientX - r.left - GUTTER;
            onHover(x < 0 || w <= 0 ? null : snapToFrame(Math.min(1, x / w) * dur, fps));
          } : undefined}
          onPointerLeave={onHover ? () => onHover(null) : undefined}>
          {/* One hover line across every lane, over the time track only (not the labels), never catching the pointer. */}
          {hoverTime !== null && (
            <div aria-hidden="true" style={{ position: 'absolute', top: 0, bottom: 0, left: GUTTER, right: 0, pointerEvents: 'none', zIndex: 5 }}>
              <div data-testid="lanes-hover-line" style={{ position: 'absolute', top: 0, bottom: 0, left: `${pct(hoverTime)}%`, width: 1, background: 'var(--color-primary)' }} />
            </div>
          )}
          <LaneRow label="Time" height={22} testId="lane-ruler">
            <div onClick={seekFromEvent} style={{ position: 'absolute', inset: 0, cursor: 'pointer' }}>
              {ticks.map((t) => (
                <div key={t} style={{ position: 'absolute', left: `${pct(t)}%`, top: 0, bottom: 0, borderLeft: '1px solid var(--color-border)', paddingLeft: 3, fontSize: 'var(--fs-3xs)', color: 'var(--color-text-secondary)' }}>{fmt(t)}</div>
              ))}
              <div style={{ position: 'absolute', left: `${playheadPct}%`, top: 0, bottom: 0, width: 2, background: 'var(--color-text)', opacity: 0.6 }} />
            </div>
          </LaneRow>

          {audioTracks.map((tr, i) => {
            const soloed = soloId === tr.id;
            const soloBtnNode = (
              <button data-testid={`solo-${tr.id}`} aria-pressed={soloed} title={soloed ? 'Stop solo (play full mix)' : 'Solo — play only this stem'}
                onClick={() => setSoloId((cur) => (cur === tr.id ? null : tr.id))}
                style={{ width: 18, height: 18, flexShrink: 0, borderRadius: 'var(--radius-badge)', border: `1px solid ${soloed ? 'var(--color-primary)' : 'var(--color-border-strong)'}`, background: soloed ? 'var(--color-primary)' : 'transparent', color: soloed ? '#fff' : 'var(--color-text-secondary)', cursor: 'pointer', fontSize: 'var(--fs-3xs)', fontWeight: 700, lineHeight: 1, padding: 0 }}>S</button>
            );
            return (
              <LaneRow key={tr.id} label={trackLabel(tr, i)} height={34} testId="lane-audio" action={soloBtnNode}>
                <WaveformVisualizer peaks={peaks[tr.id] ?? []} duration={tr.end - tr.start} currentTime={currentTime - tr.start} zoomLevel={100} scrollOffset={0} onSeek={(t) => onSeek(t + tr.start)} />
              </LaneRow>
            );
          })}

          {/* Spans that tile the film are bands: Shots (untagged markers, as ever) and each named set (Scenes, ...)
              on its own row, drawn the same way — numbered blocks with a gap at every boundary. */}
          {markerBands.map(({ set, list }) => {
            const named = set ? set.charAt(0).toUpperCase() + set.slice(1) : '';
            const one = named.replace(/s$/, '');
            const label = named ? `${named} · ${list.length}` : looksLikeShots ? `Shots · ${list.length}` : `Markers · ${list.length}`;
            return (
            <LaneRow key={set || 'shots'} label={label} height={30} testId={set ? `lane-band-${set}` : 'lane-shots'}>
              <div style={{ position: 'absolute', inset: 0 }}>
                <div style={{ position: 'absolute', left: `${playheadPct}%`, top: 0, bottom: 0, width: 2, background: 'var(--color-text)', opacity: 0.5, zIndex: 2 }} />
                {list.map((m, i) => {
                  const left = pct(m.time);
                  const w = m.endTime && m.endTime > m.time ? Math.min(100 - left, ((m.endTime - m.time) / dur) * 100) : 0;
                  const isSpan = w > 0;
                  // Use the marker's own number (label, e.g. "0","1"…) so it matches the extracted
                  // shot_NN files — NOT a 1-based render index.
                  const shotNo = (m.label ?? String(i)).trim();
                  const note = typeof m.note === 'string' ? m.note.replace(/_/g, ' ') : '';
                  const title = set
                    ? [`${one} ${shotNo}`, m.category && m.category.replace(/_/g, ' '), note,
                      `${clock(m.time)}–${clock(m.endTime ?? m.time)}`].filter(Boolean).join(' · ')
                    : `Shot ${shotNo}${m.category ? ` (${m.category})` : ''} @ ${m.time.toFixed(2)}s`;
                  const color = set ? (i % 2 ? 'var(--color-primary)' : 'color-mix(in srgb, var(--color-primary) 62%, var(--color-text))') : shotColor(m, i);
                  return (
                    <button key={m.id || i} title={title} aria-label={title} onClick={() => onSeek(m.time)}
                      style={{ position: 'absolute', top: 4, bottom: 4, left: `${left}%`, width: isSpan ? `max(3px, calc(${w}% - 2px))` : 3, background: color, border: isSpan ? '1px solid rgba(0,0,0,0.4)' : 'none', borderRadius: isSpan ? 'var(--radius-badge)' : '50% 50% 50% 0', padding: 0, cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center', overflow: 'hidden' }}>
                      {isSpan && w * zoom > 3 && <span style={{ fontSize: 'var(--fs-3xs)', fontWeight: 700, color: '#fff', textShadow: '0 1px 1px rgba(0,0,0,0.55)', pointerEvents: 'none', whiteSpace: 'nowrap', padding: '0 3px' }}>
                        {set && m.category && w * zoom > 9 ? `${shotNo} ${m.category.replace(/_/g, ' ')}` : shotNo}
                      </span>}
                    </button>
                  );
                })}
              </div>
            </LaneRow>
            );
          })}

          {groupNames.map((g, gi) => {
            const color = `hsl(${SPEAKER_HUES[gi % SPEAKER_HUES.length]}, 62%, 52%)`;
            const segs = segments.filter((s) => s.group === g);
            return (
              <LaneRow key={g} label={g} height={24} testId="lane-speaker">
                <div style={{ position: 'absolute', inset: 0 }}>
                  {segs.map((s, si) => {
                    const left = pct(s.start);
                    const w = Math.max(0.6, Math.min(100 - left, ((s.end - s.start) / dur) * 100));
                    const active = currentTime >= s.start && currentTime < s.end;
                    return (
                      <button key={si} title={`${g} ${s.start.toFixed(1)}–${s.end.toFixed(1)}s`} onClick={() => onSeek(s.start)}
                        style={{ position: 'absolute', top: 4, bottom: 4, left: `${left}%`, width: `${w}%`, background: color, opacity: active ? 1 : 0.82, border: 'none', borderRadius: 'var(--radius-badge)', outline: active ? '2px solid var(--color-text)' : 'none', cursor: 'pointer', padding: 0 }} />
                    );
                  })}
                </div>
              </LaneRow>
            );
          })}

          {peopleLanes.map((p, pi) => {
            const color = `hsl(${SPEAKER_HUES[(pi + 3) % SPEAKER_HUES.length]}, 55%, 55%)`;
            return (
              <LaneRow key={p.entityId} label={p.label} height={22} testId="lane-person">
                <div style={{ position: 'absolute', inset: 0 }}>
                  {p.intervals.map((iv, ii) => {
                    const left = pct(iv.start);
                    const w = Math.max(0.6, Math.min(100 - left, ((iv.end - iv.start) / dur) * 100));
                    const active = currentTime >= iv.start && currentTime < iv.end;
                    return (
                      <button key={ii} title={`${p.label} ${iv.start.toFixed(1)}–${iv.end.toFixed(1)}s`} onClick={() => onSeek(iv.start)}
                        style={{ position: 'absolute', top: 4, bottom: 4, left: `${left}%`, width: `${w}%`, background: color, opacity: active ? 1 : 0.8, border: 'none', borderRadius: 'var(--radius-badge)', outline: active ? '2px solid var(--color-text)' : 'none', cursor: 'pointer', padding: 0 }} />
                    );
                  })}
                </div>
              </LaneRow>
            );
          })}
        </div>
      </div>
    </div>
  );
}

const zoomBtn: React.CSSProperties = { width: 22, height: 22, borderRadius: 'var(--radius-button)', border: '1px solid var(--color-border-strong)', background: 'var(--color-surface)', color: 'var(--color-text)', cursor: 'pointer', fontSize: 'var(--fs-sm)', lineHeight: 1, display: 'inline-flex', alignItems: 'center', justifyContent: 'center' };
