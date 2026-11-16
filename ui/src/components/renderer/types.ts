import type { theme } from '../../styles/tokens';

import type { SearchKind } from '../../shared/searchKinds';

export interface RenderBlock {
  template: string;
  title: string;
  data: Record<string, unknown>;
  html?: string;
}

export type ThemeTokens = typeof theme;

export interface RendererProps<T = Record<string, unknown>> {
  data: T;
  title: string;
  theme: ThemeTokens;
  onInteraction?: (event: InteractionEvent) => void;
}

export interface InteractionEvent {
  // 'mark'   — the user set/cleared a mark-in/out range on a timeline.
  // 'action' — the user invoked a timeline verb (clip, stems, extract frame…);
  //            carries `data.prompt`; WorkspaceView sends it to the agent as a live turn.
  // 'open-tab' — open an EXISTING media url as its own Renderer tab (client-side, no
  //            agent turn). Used by a clip-gallery video card's "Expand" → a single-video
  //            timeline tab. Carries `data` as `OpenTabActionData`.
  type: 'click' | 'select' | 'hover' | 'scrub' | 'mark' | 'action' | 'open-tab';
  target: string;
  data?: unknown;
  timestamp: number;
}

/** Payload for a `type: 'open-tab'` interaction: open `url` as its own Renderer tab. */
export interface OpenTabActionData {
  url: string;
  label: string;
}

/** Payload shape for `type: 'action'` interactions emitted by the timeline editor. */
export interface TimelineActionData {
  verb: string;
  prompt: string;
  /** Committed ranges (plus the current draft) the verb applies to; may be empty (whole clip). */
  ranges: { in: number; out: number }[];
  /** Point timestamps the verb applies to (frame extraction); playhead fallback when none. */
  points: number[];
  src?: string;
}

/** A spatial crop region, NORMALIZED to the video CONTENT rect (0..1, letterbox-corrected).
 *  Same units as an annotation box (`_normalize_box`); the agent's crop_video.py resolves it
 *  to real pixels via ffprobe. */
export interface CropBox { x: number; y: number; w: number; h: number; }

/** Payload for a single-video `type: 'action'` crop (from the nle-timeline crop overlay). The
 *  normalized box is also baked into `prompt` (human-readable numbers) so it survives to the
 *  agent through the existing verb-agnostic dispatch; `src` is appended as `[source: …]`. */
export interface CropActionData {
  verb: 'crop';
  prompt: string;
  src: string;
  box: CropBox;
  /** The preset aspect the box matches on this video ("9:16"), for the chat's wording. */
  aspect?: string;
  /** The range marked on the timeline when Crop was pressed (seconds): the crop covers just that span. */
  ranges?: { in: number; out: number }[];
}

/** One INDEPENDENT, editable clip in a clip-gallery — an audio take or a video take.
 *  Unlike a timeline track/segment, each owns its OWN duration and is not time-aligned
 *  with the others, so each gets its own ruler + mark-in/out. */
export interface ClipGalleryClip {
  id: string;
  label: string;
  text?: string;            // the spoken line / caption for this clip
  src: string;              // https URL (element src; no auth)
  kind: 'audio' | 'video';
  durationHint?: number;    // seconds; the media element measures the truth on load
  status?: string;          // 'passed' | 'flagged' | ...
  peaks?: number[];         // optional waveform peaks (audio)
}

/** One Moment card in a Moments_Gallery — a Hit cited in (or matched by) a turn.
 *  Carries NO media URL by construction: `ref` names the asset and `frame_s3_key`
 *  is a bucket key, not a URL. The SPA mints presigned URLs at view time (per card
 *  mount), so nothing URL-bearing is persisted in SPA state. */
export interface MomentItem {
  id: string;
  ref: { project_id: string; asset_id: string; frame_s3_key: string | null };
  /** `document`: a PDF page hit; its `start_ms` encodes the page (`shared/pageLocator.ts`), not a time. */
  kind: SearchKind;
  display_name: string;
  start_ms: number;
  end_ms: number;
  source_index: string | null;
  final_score: number;
  /** The Hit's matched text, verbatim (markup kept literal; rendered as React text). */
  text: string;
  text_truncated: boolean;
  /** Terms to style within `text` (the `<em>` inner texts of the Hit's highlights). */
  highlight_terms: string[];
  citation: string | null;
  /** A tab search by several images ("match any"): the indexes of the query images that found this moment. */
  matched_by?: number[];
  /** Which of hybrid search's searches matched it: "meaning" (vector) and/or "keyword" (full text). */
  found_by?: string[];
}

/** One query image of a Moments search (design 2026-10-18 §2): an image asset, or a frame of a project video. */
export interface MomentsQueryImage { assetId?: string; frameKey?: string; sourceAssetId?: string; atMs?: number; label: string }

/** The typed query a Moments tab starts from: the turn's text search, or its query image(s). */
export type MomentsQuery =
  | { type: 'text'; q: string }
  | { type: 'image'; images: MomentsQueryImage[]; match: 'any' };

/** The Moments block attached to a clip-gallery manifest. Every eligible Hit of the turn's searches
 *  is an item (up to 200); cited items come first and carry `citation`. `basis` is `cited` when at
 *  least one cited item exists, else `searched`; `cited_count` counts them (absent on older manifests). */
export interface MomentsBlock {
  v: 1;
  project_id: string;
  turn_id: string;
  basis: 'cited' | 'searched';
  cited_count?: number;
  truncated: boolean;
  items: MomentItem[];
  /** The turn's first search term — pre-fills the tab's search box (absent on older manifests). */
  query?: string;
  /** The turn's typed query: an image search arrives as its image chip(s). Absent on older manifests. */
  queryRef?: MomentsQuery;
  /** The type chips pressed when this view's search ran (a tab search's version); absent = every kind. */
  kinds?: Array<MomentItem['kind']>;
}

export interface ClipGalleryData {
  title?: string;
  subtitle?: string;
  /** Direct-artifact path passes `clips`; the manifest path passes `galleryClips`
   *  (a distinct field so it never collides with RenderManifest.clips = segments). */
  clips?: ClipGalleryClip[];
  galleryClips?: ClipGalleryClip[];
  /** Present only on a Moments manifest; drives ClipGallery's Moments mode. When
   *  absent, the gallery runs its legacy `galleryClips` path unchanged. */
  galleryMoments?: MomentsBlock;
  manifest?: unknown;
}

/** Payload for a batch clip/trim action across several INDEPENDENT clips. Each entry
 *  names its own clip + src + keep-range, so the agent knows exactly which file to trim
 *  and to what — the "which one?" ambiguity is resolved by construction. */
export interface BatchClipActionData {
  verb: string; // 'trim' | 'clip' | 'extract' | 'crop'
  prompt: string;
  /** Each entry names its own clip + src. `in`/`out` = a keep-range (trim/clip); `at` = a single
   *  capture point (frame-extract); `box` = a normalized crop region (crop). Only the fields the
   *  verb needs are set. Moment cards also carry the Hit's `ref` and `kind` so the agent can
   *  resolve the asset without a URL. */
  clips: { clipId: string; label: string; src: string; in?: number; out?: number; at?: number; box?: CropBox; ref?: { project_id: string; asset_id: string; frame_s3_key: string | null }; kind?: 'video' | 'image' | 'audio' }[];
  /** The whole gallery the action was launched on, in order: trimming some of a set returns the set's NEXT VERSION
   *  (the trimmed items in place of their originals), in the same tab. */
  set?: { clipId: string; label: string; src: string; kind?: 'video' | 'image' | 'audio' }[];
  /** The tab the action was launched from (SessionRendererPanel adds it): what it is about, and its name. */
  origin?: { subjectIds: string[]; label?: string };
}

export interface RenderHistoryEntry {
  turnIndex: number;
  blocks: RenderBlock[];
  timestamp: number;
  /**
   * Tab label — the TASK name (e.g. "Separate Audio Stems"). Deliberately not
   * the plan's internal node id (`sg2_stems`), which is planner bookkeeping and
   * means nothing to the reader. Absent on legacy entries, where the panel falls
   * back to the block title.
   */
  label?: string;
  /** Job this tab's results came from. Used for dedupe and lazy rendering. */
  jobId?: string;
  /**
   * Canonical artifact refs for a tab that has NOT been rendered yet.
   *
   * A finished DAG creates one tab per node immediately, but rendering all of
   * them up front would fire the Render Agent N times for results the user may
   * never open (that eager approach measurably slowed dispatch). Tabs are cheap
   * metadata; the manifest is composed on first click and then cached here.
   */
  artifactUrls?: string[];
  /**
   * Lifecycle of this tab's view:
   *  - 'composing' → the Render Agent is building the manifest; the tab shows the
   *    render animation in its own content area (never blanks the whole panel).
   *  - 'ready'     → blocks are populated (or the tab is a legacy lazy tab).
   * Absent on legacy entries → treated as 'ready'.
   */
  status?: 'composing' | 'ready';
  /**
   * Correlation id from dispatch_render. Set on the composing placeholder created
   * from the leading "Composing…" marker AND matched when the manifest arrives, so
   * the SAME tab fills in place instead of a second tab appearing.
   */
  renderRequestId?: string;
  /**
   * The tab's STABLE identity (R8.3): the render manifest key for a persisted render
   * (`artifacts/<session>/render/<id>.json`), `composite:<fold key>` for a server composite, or a
   * `client:<id>` for a tab the client authored. Filling and removing a tab address it by this key,
   * never by its position. Absent on legacy entries — `renderTabKey` derives one.
   */
  key?: string;
  /**
   * True until the server's render list includes this client-authored tab (an Expand view whose
   * save is in flight, has failed, or landed after the last list was read). A merge keeps it
   * rather than dropping it, and the first list that names its key replaces it in place.
   */
  clientOnly?: boolean;
  /**
   * What this view is about (`shared/renderSubject.viewSubjectIds`): the file it shows, else a gallery's title.
   * The panel shows every view sharing a subject as ONE tab with versions. Absent = its own subject.
   */
  subjectIds?: string[];
  /** What tells this version apart from the others of its tab (`viewVersionNote`: a search's query). */
  note?: string;
}
