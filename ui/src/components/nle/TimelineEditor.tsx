/**
 * TimelineEditor — interactive, professional editing surface for a video (or
 * single-audio) render. Deliberately LEAN: one action row (Clip · Frame ·
 * Analyze▾) plus a ruler you mark on directly, so nothing wraps to a second row.
 *
 *   - Drag on the ruler → create a range (in/out); a committed range shows
 *     draggable edge handles + a remove ✕.
 *   - Click the ruler → move the playhead.
 *   - Double-click the ruler → drop a point marker (remove ✕).
 *   - The selection pill summarises ranges/markers and clears them (✕).
 *
 * Contract: the UI only MARKS + COMPOSES a request; the backend agent does the
 * work. Each action emits InteractionEvent{type:'action'} with a `prompt`, which
 * WorkspaceView pushes to the agent as a live turn (chat:send). RANGE verbs
 * (Clip / analyze) batch over all ranges; FRAME batches over all points.
 *
 * The Analyze menu is PORTALED to <body> so SessionRendererPanel's overflow can't
 * clip it. Under components/nle/** (lint:colors-exempt): mark-in/out signal
 * colours are editorial CONTENT and must not flip with the chrome theme.
 */
import React, { useRef, useState, useCallback, useEffect } from 'react';
import { createPortal } from 'react-dom';
import type { InteractionEvent, ThemeTokens, TimelineActionData } from '../renderer/types';
import type { TimelineEngine, TimelineState } from './TimelineEngine';
import { formatTimecode } from './formatTimecode';
import { useTimelineView, useWheelZoom, type TimelineView } from './timelineView';
import { snapToFrame } from './frameTime';
import { HoverPeek, timecodeFor } from './HoverPeek';
import CloseIcon from '../icons/CloseIcon';
import ScissorsIcon from '../icons/ScissorsIcon';
import ImageIcon from '../icons/ImageIcon';
import ChevronDownIcon from '../icons/ChevronDownIcon';

const IN_COLOR = '#3FBF77';
const OUT_COLOR = '#E05D52';
const POINT_COLOR = '#E8A24C';
/** How close (on screen) a drag must come to a snap target — a shot boundary, a keyframe — to land on it. In pixels,
 *  as in any editor: a fixed 0.4 s was under one pixel on a 9-minute timeline, so nothing ever snapped zoomed out,
 *  while zoomed in the same pixels are a few frames and a mark can still go anywhere. */
const SNAP_PX = 8;

/** The snap target nearest `t` within `SNAP_PX` on a track `trackPx` wide showing `dur` seconds, else `t` itself. */
export function snapTime(t: number, targets: readonly number[] | undefined, trackPx: number, dur: number): number {
  if (!targets || targets.length === 0 || trackPx <= 0 || dur <= 0) return t;
  let best = t, bestD = (SNAP_PX / trackPx) * dur;
  for (const s of targets) { const d = Math.abs(s - t); if (d < bestD) { bestD = d; best = s; } }
  return best;
}
const DRAG_THRESHOLD_PX = 4;
const MENU_WIDTH = 184;

export interface TimelineEditorProps {
  engine: TimelineEngine;
  state: TimelineState;
  duration: number;
  src?: string;
  theme: ThemeTokens;
  onInteraction?: (event: InteractionEvent) => void;
  /** Optional snap targets (e.g. shot-marker times) for drag handles. */
  snapTimes?: number[];
  /** The zoom/scroll shared with the analysis lanes (NleTimeline); a local one when absent. */
  view?: TimelineView;
  /** Width of the lanes' label column, so the ruler's time track lines up with theirs; 0 = no lanes. */
  gutter?: number;
  /** The video's frame rate when known: timecodes read HH:MM:SS:FF and marks land on frame starts. */
  fps?: number | null;
  /**
   * The hovered time, shared with the lanes (NleTimeline): the ruler shows the line and the frame peek wherever the
   * pointer is on the timeline. Absent → the ruler tracks its own hover.
   */
  hoverTime?: number | null;
  onHover?: (time: number | null) => void;
  /** Show the hovered frame in the peek (a video); audio shows the timecode alone. */
  peekFrames?: boolean;
}

interface RangeLite { in: number; out: number; }

const ANALYZE_VERBS: { verb: string; label: string }[] = [
  { verb: 'shots', label: 'Shot Segmentation' },
  { verb: 'faces', label: 'People Detection' },
  { verb: 'transcribe', label: 'Speaker Diarization' },
  { verb: 'stems', label: 'Audio Stems' },
  { verb: 'extract-audio', label: 'Extract Audio' },
];

const ANALYZE_BASE: Record<string, string> = {
  'extract-audio': 'Extract the audio (soundtrack) from the video',
  stems: 'Separate the audio stems (speech, music, SFX) from the video',
  shots: 'Segment the video into shots (scene/cut detection)',
  faces: 'Detect and identify the people (face recognition) in the video',
  transcribe: 'Transcribe and diarize the speech (identify who spoke when) in the video',
};

// 'frame' is the only point-based verb (operates on markers / playhead, not
// ranges): a single Frame button that extracts one still (at the playhead) or
// bulk-extracts stills at every marker. No crop/thumbnail sub-menu — "set as
// thumbnail" can't write a poster into the read-only source, and it cluttered
// the bar. Keep it to what the tools actually do: extract frame(s).
const isPointVerb = (verb: string) => verb === 'frame';

function dedupeRanges(ranges: RangeLite[]): RangeLite[] {
  const seen = new Set<string>();
  const out: RangeLite[] = [];
  for (const r of ranges) {
    const k = `${r.in.toFixed(3)}|${r.out.toFixed(3)}`;
    if (!seen.has(k)) { seen.add(k); out.push(r); }
  }
  return out;
}
function dedupeNums(nums: number[]): number[] {
  const seen = new Set<string>();
  const out: number[] = [];
  for (const n of nums) { const k = n.toFixed(3); if (!seen.has(k)) { seen.add(k); out.push(n); } }
  return out;
}

/** Compose the natural-language request the agent turn will carry. */
export function buildActionPrompt(verb: string, ranges: RangeLite[], points: number[], src?: string): string {
  const tc = formatTimecode;
  // The source URL is threaded separately (TimelineActionData.src) and appended to the
  // AGENT prompt only at send time — NEVER shown in the chat bubble — so CloudFront paths
  // never leak into the transcript. So the composed text here is clean.
  const srcTxt = '';
  if (verb === 'frame') {
    const list = dedupeNums(points);
    if (list.length <= 1) return `Extract a still frame at ${tc(list.length === 1 ? list[0] : 0)}.${srcTxt}`;
    return `Extract ${list.length} still frames at ${list.map(tc).join(', ')}.${srcTxt}`;
  }
  const rl = dedupeRanges(ranges);
  if (verb === 'clip') {
    if (rl.length === 0) return `Clip the video.${srcTxt}`;
    if (rl.length === 1) return `Clip the video from ${tc(rl[0].in)} to ${tc(rl[0].out)}.${srcTxt}`;
    return `Clip the video into ${rl.length} segments: ${rl.map(r => `${tc(r.in)}–${tc(r.out)}`).join(', ')}.${srcTxt}`;
  }
  const base = ANALYZE_BASE[verb] ?? 'Process the video';
  if (rl.length === 0) return `${base}.${srcTxt}`;
  if (rl.length === 1) return `${base} for the marked range ${tc(rl[0].in)}–${tc(rl[0].out)}.${srcTxt}`;
  return `${base} for ${rl.length} ranges: ${rl.map(r => `${tc(r.in)}–${tc(r.out)}`).join(', ')}.${srcTxt}`;
}

/** Pick a "nice" ruler tick step (seconds) so ~6–10 ticks span the duration. */
export function tickStep(duration: number): number {
  if (duration <= 0) return 1;
  const target = duration / 8;
  const steps = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900];
  for (const s of steps) if (s >= target) return s;
  return steps[steps.length - 1];
}

type DragKind = 'range-in' | 'range-out';
interface HandleDrag { kind: DragKind; id: string; }

export default function TimelineEditor({ engine, state, duration, src, theme: t, onInteraction, snapTimes, view: sharedView, gutter = 0,
  fps = null, hoverTime: sharedHover, onHover, peekFrames = true }: TimelineEditorProps) {
  // Where am I: the hovered time (shared with the lanes when NleTimeline passes it), its line and frame peek.
  const [localHover, setLocalHover] = useState<number | null>(null);
  const hoverTime = sharedHover !== undefined ? sharedHover : localHover;
  const setHover = useCallback((tv: number | null) => { (onHover ?? setLocalHover)(tv); }, [onHover]);
  const trackRef = useRef<HTMLDivElement>(null);
  const viewportRef = useRef<HTMLDivElement>(null);
  const analyzeBtnRef = useRef<HTMLButtonElement>(null);
  const handleDragRef = useRef<HandleDrag | null>(null);
  // Range-create-by-drag on the ruler body.
  const anchorTimeRef = useRef<number | null>(null);
  const anchorXRef = useRef<number>(0);
  const draggedRef = useRef(false);
  const [menu, setMenu] = useState<'analyze' | null>(null);
  const [menuPos, setMenuPos] = useState<{ top: number; left: number } | null>(null);
  // 1× = fit-to-width; higher = scrollable, finer marking. Shared with the lanes below when NleTimeline
  // passes `view`, so the ruler and every lane zoom and scroll as one.
  const localView = useTimelineView();
  const view = sharedView ?? localView;
  const { zoom } = view;
  const [viewportEl, setViewportEl] = useState<HTMLDivElement | null>(null);
  const registerViewport = view.register('ruler');
  const viewportRefCb = useCallback((el: HTMLDivElement | null) => {
    (viewportRef as React.MutableRefObject<HTMLDivElement | null>).current = el;
    registerViewport(el);
    setViewportEl(el);
  }, [registerViewport]);
  useWheelZoom(view, viewportEl);

  const { markIn, markOut, currentTime, ranges, points } = state;
  const dur = duration > 0 ? duration : 0;
  const pct = (s: number) => (dur > 0 ? Math.max(0, Math.min(100, (s / dur) * 100)) : 0);

  const timeFromClientX = useCallback((clientX: number): number => {
    const el = trackRef.current;
    if (!el) return 0;
    const r = el.getBoundingClientRect();
    if (r.width <= 0) return 0;
    const ratio = Math.max(0, Math.min(1, (clientX - r.left) / r.width));
    // Shot snapping first, then onto the frame under the pointer, so a mark is the frame the timecode names.
    return snapToFrame(snapTime(ratio * dur, snapTimes, r.width, dur), fps);
  }, [dur, snapTimes, fps]);

  // Global pointer move/up: handle-edge drags AND ruler range-create drags keep
  // tracking off the element.
  useEffect(() => {
    const onMove = (e: PointerEvent) => {
      const hd = handleDragRef.current;
      if (hd) {
        const tv = timeFromClientX(e.clientX);
        const r = engine.getState().ranges.find(x => x.id === hd.id);
        if (r) {
          if (hd.kind === 'range-in') engine.updateRange(hd.id, tv, r.out);
          else engine.updateRange(hd.id, r.in, tv);
        }
        return;
      }
      if (anchorTimeRef.current !== null) {
        if (!draggedRef.current && Math.abs(e.clientX - anchorXRef.current) > DRAG_THRESHOLD_PX) {
          draggedRef.current = true;
        }
        if (draggedRef.current) {
          const tv = timeFromClientX(e.clientX);
          const a = Math.min(anchorTimeRef.current, tv);
          const b = Math.max(anchorTimeRef.current, tv);
          engine.setMarkIn(a);
          engine.setMarkOut(b);
        }
      }
    };
    const onUp = () => {
      if (anchorTimeRef.current !== null) {
        if (draggedRef.current) engine.commitDraftRange();  // real drag → a range
        // a plain click was just a seek (done on pointerdown); no draft to clear
        anchorTimeRef.current = null;
        draggedRef.current = false;
      }
      handleDragRef.current = null;
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    return () => { window.removeEventListener('pointermove', onMove); window.removeEventListener('pointerup', onUp); };
  }, [engine, timeFromClientX]);

  const onRulerPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    const tv = timeFromClientX(e.clientX);
    anchorTimeRef.current = tv;
    anchorXRef.current = e.clientX;
    draggedRef.current = false;
    engine.seek(tv);  // move the playhead; a range only forms if the pointer drags
  };
  const onRulerDoubleClick = (e: React.PointerEvent<HTMLDivElement>) => {
    engine.addPoint(timeFromClientX(e.clientX));
  };
  const startHandleDrag = (e: React.PointerEvent, kind: DragKind, id: string) => {
    e.stopPropagation();
    handleDragRef.current = { kind, id };
  };

  const gatherRanges = (): RangeLite[] => {
    const list = ranges.map(r => ({ in: r.in, out: r.out }));
    if (markIn !== null && markOut !== null) list.push({ in: markIn, out: markOut });
    return list;
  };
  const emitAction = (verb: string) => {
    const rl = gatherRanges();
    const pts = points.length ? points.map(p => p.t) : [currentTime];
    const prompt = isPointVerb(verb)
      ? buildActionPrompt(verb, [], pts, src)
      : buildActionPrompt(verb, rl, [], src);
    const data: TimelineActionData = { verb, prompt, ranges: rl, points: pts, src };
    onInteraction?.({ type: 'action', target: verb, data, timestamp: Date.now() });
    setMenu(null);
  };

  // Wheel over the ruler zooms it about the pointer (no +/- buttons — saves bar space): useWheelZoom.

  const openMenu = (kind: 'analyze', ref: React.RefObject<HTMLButtonElement | null>) => {
    const r = ref.current?.getBoundingClientRect();
    if (r) setMenuPos({ top: r.bottom + 4, left: Math.max(8, r.right - MENU_WIDTH) });
    setMenu(m => (m === kind ? null : kind));
  };
  // Close the portaled menu on any outside pointer-down or on resize.
  useEffect(() => {
    if (!menu) return;
    const onDown = (e: PointerEvent) => {
      const tgt = e.target as Node | null;
      if (analyzeBtnRef.current?.contains(tgt as Node)) return;
      const menuEl = document.getElementById('nle-timeline-menu');
      if (menuEl && menuEl.contains(tgt as Node)) return;
      setMenu(null);
    };
    const onResize = () => setMenu(null);
    window.addEventListener('pointerdown', onDown, true);
    window.addEventListener('resize', onResize);
    return () => {
      window.removeEventListener('pointerdown', onDown, true);
      window.removeEventListener('resize', onResize);
    };
  }, [menu]);

  const hasDraft = markIn !== null && markOut !== null;
  const nRanges = ranges.length + (hasDraft ? 1 : 0);
  const nPoints = points.length;
  const hasSelection = nRanges > 0 || nPoints > 0;
  const step = tickStep(dur / Math.max(1, zoom));
  const ticks: number[] = [];
  for (let s = 0; dur > 0 && s <= dur + 1e-6; s += step) ticks.push(s);

  const btn: React.CSSProperties = {
    padding: '5px 12px', fontSize: t.typography.sizes.meta, borderRadius: t.radius.button,
    border: `1px solid ${t.colors.borderStrong}`, background: t.colors.surface,
    color: t.colors.textPrimary, cursor: 'pointer', fontFamily: 'inherit', whiteSpace: 'nowrap',
    display: 'inline-flex', alignItems: 'center', gap: 6,
  };
  // The small ✕-style remove controls: an SVG glyph centred in a fixed square, so nothing shifts.
  const iconBtn: React.CSSProperties = { display: 'inline-flex', alignItems: 'center', justifyContent: 'center', padding: 0 };
  const primaryBtn: React.CSSProperties = { ...btn, background: t.colors.primary, borderColor: t.colors.primary, color: t.colors.onPrimary, fontWeight: t.typography.weights.semibold };
  const handleStyle = (color: string): React.CSSProperties => ({ position: 'absolute', top: 0, bottom: 0, width: 6, background: color, cursor: 'ew-resize', zIndex: 8 });

  const renderBand = (a: number, b: number, key: string, closed: boolean, id?: string) => (
    <div key={key} data-testid={id ? `range-band-${id}` : 'range-band'} style={{ position: 'absolute', top: 0, bottom: 0, left: `${pct(a)}%`, width: `${pct(b) - pct(a)}%`, background: t.colors.activeBg, borderTop: `1px solid ${IN_COLOR}`, borderBottom: `1px solid ${OUT_COLOR}` }}>
      {id && <div data-testid={`handle-in-${id}`} onPointerDown={(e) => startHandleDrag(e, 'range-in', id)} style={{ ...handleStyle(IN_COLOR), left: -3 }} />}
      {id && closed && <div data-testid={`handle-out-${id}`} onPointerDown={(e) => startHandleDrag(e, 'range-out', id)} style={{ ...handleStyle(OUT_COLOR), right: -3 }} />}
      {id && (
        <button data-testid={`range-remove-${id}`} aria-label="Remove range" title="Remove range" onPointerDown={(e) => e.stopPropagation()} onClick={(e) => { e.stopPropagation(); engine.removeRange(id); }}
          style={{ ...iconBtn, position: 'absolute', top: 1, right: 2, zIndex: 9, width: 14, height: 14, borderRadius: t.radius.badge, border: 'none', background: t.colors.overlay, color: '#fff', cursor: 'pointer' }}><CloseIcon size={10} /></button>
      )}
    </div>
  );

  return (
    <div data-testid="timeline-editor" style={{ display: 'flex', flexDirection: 'column', gap: 8, padding: '10px 12px', background: t.colors.surface, borderTop: `1px solid ${t.colors.border}` }}>
      {/* Single action row */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'nowrap' }}>
        <span data-testid="timeline-timecode" style={{ fontFamily: t.typography.fontMono, fontVariantNumeric: 'tabular-nums', fontSize: t.typography.sizes.code, color: t.colors.textPrimary, whiteSpace: 'nowrap' }}>
          {timecodeFor(currentTime, fps)} / {timecodeFor(dur, fps)}
        </span>
        {zoom > 1 && (
          <span data-testid="zoom-level" title="Scroll over the ruler to zoom" style={{ fontSize: t.typography.sizes.badge, color: t.colors.textTertiary, minWidth: 30, textAlign: 'center', fontFamily: t.typography.fontMono, whiteSpace: 'nowrap' }}>{zoom.toFixed(1)}×</span>
        )}
        {hasSelection && (
          <span data-testid="selection-summary" style={{ display: 'inline-flex', alignItems: 'center', gap: 8, padding: '3px 6px 3px 10px', borderRadius: t.radius.pill, border: `1px solid ${t.colors.primary}`, background: t.colors.accentSubtle, color: t.colors.primary, fontFamily: t.typography.fontMono, fontVariantNumeric: 'tabular-nums', fontSize: t.typography.sizes.meta, whiteSpace: 'nowrap' }}>
            {nRanges > 0 ? `${nRanges} range${nRanges > 1 ? 's' : ''}` : ''}{nRanges > 0 && nPoints > 0 ? ' · ' : ''}{nPoints > 0 ? `${nPoints} marker${nPoints > 1 ? 's' : ''}` : ''}
            <button data-testid="selection-clear" onClick={() => { engine.clearMarks(); engine.clearRanges(); engine.clearPoints(); }}
              title="Clear all ranges & markers" aria-label="Clear all ranges and markers" style={{ ...iconBtn, width: 15, height: 15, borderRadius: t.radius.badge, border: 'none', background: 'transparent', color: t.colors.primary, cursor: 'pointer' }}><CloseIcon size={12} /></button>
          </span>
        )}
        <div style={{ display: 'flex', gap: 6, marginLeft: 'auto' }}>
          <button data-testid="action-clip" onClick={() => emitAction('clip')} style={primaryBtn} title="Clip the marked range(s)"><ScissorsIcon size={14} />Clip</button>
          <button data-testid="action-frame" onClick={() => emitAction('frame')} style={btn} title="Extract a still frame at each marker (or the playhead if none)"><ImageIcon size={14} />Frame</button>
          <button ref={analyzeBtnRef} data-testid="analyze-menu" onClick={() => openMenu('analyze', analyzeBtnRef)} aria-expanded={menu === 'analyze'} style={btn} title="Analyze the whole video (or just the marked ranges)">Analyze<ChevronDownIcon size={14} /></button>
        </div>
      </div>

      {/* Ruler + scrubber — zoomable + horizontally scrollable for long media. With a `gutter` it spans the
          panel edge to edge behind a label column as wide as the lanes', so its time track has exactly the
          lanes' width and offset: a time is at the same x on the ruler and on every lane. */}
      <div style={gutter > 0 ? { display: 'flex', margin: '0 -12px', borderTop: `1px solid ${t.colors.border}`, borderBottom: `1px solid ${t.colors.border}` } : { display: 'flex' }}>
      {gutter > 0 && (
        <div data-testid="timeline-ruler-gutter" style={{ width: gutter, flexShrink: 0, display: 'flex', alignItems: 'center', padding: '0 8px', boxSizing: 'border-box', fontSize: t.typography.sizes.badge, color: t.colors.textTertiary, borderRight: `1px solid ${t.colors.border}`, background: t.colors.surfaceSubtle }}>Ruler</div>
      )}
      <div ref={viewportRefCb} data-testid="timeline-ruler-viewport" style={{ flex: 1, minWidth: 0, overflowX: zoom > 1 ? 'auto' : 'hidden', overflowY: 'hidden', background: t.colors.surfaceSubtle, ...(gutter > 0 ? {} : { border: `1px solid ${t.colors.border}`, borderRadius: t.radius.button }) }}>
      <div ref={trackRef} data-testid="timeline-ruler" onPointerDown={onRulerPointerDown} onDoubleClick={onRulerDoubleClick}
        onPointerMove={(e) => {
          // The hover follows the pointer exactly (frame under it), never snapping to a shot boundary like a mark does.
          const r = trackRef.current?.getBoundingClientRect();
          if (r && r.width > 0) setHover(snapToFrame(Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * dur, fps));
        }}
        onPointerLeave={() => setHover(null)}
        title="Drag to mark a range · double-click to add a marker · scroll to zoom"
        style={{ position: 'relative', height: 38, width: `${zoom * 100}%`, minWidth: '100%', cursor: 'pointer', touchAction: 'none', userSelect: 'none' }}>
        {ticks.map((s, i) => (
          <div key={i} style={{ position: 'absolute', top: 0, bottom: 0, left: `${pct(s)}%`, borderLeft: `1px solid ${t.colors.border}` }}>
            <span style={{ position: 'absolute', top: 4, left: 4, fontFamily: t.typography.fontMono, fontVariantNumeric: 'tabular-nums', fontSize: t.typography.sizes.badge, color: t.colors.textTertiary, whiteSpace: 'nowrap' }}>{formatTimecode(s).slice(0, 5)}</span>
          </div>
        ))}
        {ranges.map(r => renderBand(r.in, r.out, `r-${r.id}`, true, r.id))}
        {markIn !== null && renderBand(markIn, markOut !== null ? markOut : currentTime, 'draft', markOut !== null)}
        {points.map(p => (
          <div key={p.id} data-testid={`point-${p.id}`} style={{ position: 'absolute', top: 0, bottom: 0, left: `${pct(p.t)}%`, width: 2, background: POINT_COLOR, zIndex: 7 }}>
            <button data-testid={`point-remove-${p.id}`} aria-label="Remove marker" title="Remove marker" onPointerDown={(e) => e.stopPropagation()} onClick={(e) => { e.stopPropagation(); engine.removePoint(p.id); }}
              style={{ ...iconBtn, position: 'absolute', top: -1, left: -6, width: 14, height: 14, borderRadius: t.radius.badge, border: 'none', background: POINT_COLOR, color: '#22160d', cursor: 'pointer' }}><CloseIcon size={10} /></button>
          </div>
        ))}
        <div data-testid="timeline-playhead" style={{ position: 'absolute', top: 0, bottom: 0, left: `${pct(currentTime)}%`, width: 2, background: t.colors.textPrimary, opacity: 0.85, pointerEvents: 'none', zIndex: 6 }} />
        {hoverTime !== null && (
          <div data-testid="timeline-hover-line" style={{ position: 'absolute', top: 0, bottom: 0, left: `${pct(hoverTime)}%`, width: 1, background: t.colors.primary, pointerEvents: 'none', zIndex: 9 }} />
        )}
      </div>
      </div>
      </div>

      {/* The hovered frame + timecode, above the ruler wherever the pointer is on the timeline (portaled: the ruler's
          scroll viewport would clip it). */}
      <HoverPeek src={src} time={hoverTime} fps={fps} showFrame={peekFrames} theme={t}
        anchor={hoverTime !== null && trackRef.current ? (() => {
          const r = trackRef.current!.getBoundingClientRect();
          return { x: r.left + (pct(hoverTime) / 100) * r.width, top: r.top };
        })() : null} />

      {/* Frame / Analyze menu — PORTALED to <body> so the panel's overflow can't clip it. */}
      {menu && menuPos && createPortal(
        <div id="nle-timeline-menu" data-testid="analyze-menu-list" role="menu"
          style={{ position: 'fixed', top: menuPos.top, left: menuPos.left, width: MENU_WIDTH, zIndex: t.zIndex.toast, background: t.colors.surfaceRaised, border: `1px solid ${t.colors.border}`, borderRadius: t.radius.button, boxShadow: t.elevation.elevated, overflow: 'hidden' }}>
          {ANALYZE_VERBS.map(a => (
            <button key={a.verb} data-testid={`analyze-item-${a.verb}`} role="menuitem" onClick={() => emitAction(a.verb)}
              style={{ display: 'block', width: '100%', textAlign: 'left', padding: '8px 12px', fontSize: t.typography.sizes.meta, background: 'transparent', border: 'none', color: t.colors.textPrimary, cursor: 'pointer', fontFamily: 'inherit' }}>
              {a.label}
            </button>
          ))}
        </div>,
        document.body,
      )}
    </div>
  );
}
