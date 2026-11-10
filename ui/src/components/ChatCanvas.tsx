// ChatCanvas.tsx — Main chat canvas component for the MediaWorks Lab Frontend.
// Renders scrollable message history with virtual scrolling (up to 500 messages),
// Markdown rendering for assistant messages, step progress indicators, and execution summaries.
// Includes chat input with message submission, queue management, and error handling.
// Requirements: 6.1, 6.2, 6.3, 6.5, 6.6, 6.7, 6.10, 6.11

import SearchIndexedGlyph, { useProjectIndexing } from './projects/SearchIndexedGlyph';
import React, { useState, useRef, useEffect, useLayoutEffect, useCallback, useMemo, useId } from 'react';
import ReactMarkdown, { defaultUrlTransform } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { markdownComponents } from './chat/markdownComponents';
import { ChatAssetContext, keepAssetUrls } from './chat/InlineAssetImage';

/** Keep `asset:` image sources (InlineAssetImage); every other URL gets react-markdown's own sanitising. */
const chatUrlTransform = (url: string) => keepAssetUrls(url, defaultUrlTransform);
import { CitationContext } from './chat/CitationRef';
import { remarkCitations } from './chat/remarkCitations';
import { InputAttachment } from './chat/InputAttachment';
import type { ChatMessage, StepEvent, ExecutionSummary, StreamEvent } from '../lab-api';
import { createSession, updateSessionActivity, getConfig, getSessionTranscript } from '../lab-api';
import { setSessionCache } from '../utils/safeStorage';
import { parseOpenProject, requestNewChatInProject, takePendingOpen } from '../projects/newChatInProject';
import { refreshProjectsList } from '../projects/useProjectsList';
import { invokeAgentStreaming, selectedOrchestratorModelId, selectedReasoningEffort } from '../api';
import type { StreamCallback } from '../api';
import { shouldRenderManifest, isRenderPendingStep, shouldEnterRenderPending } from '../services/renderOrchestration';
import { useLabContext } from '../LabContext';
import { useCurrentUser } from '../auth/useCurrentUser';
import { theme, tint } from '../styles/tokens';
import JobReference from './chat/JobReference';
import WarningIcon from './icons/WarningIcon';
import PlusMenu, { type PlusMenuProjects } from './chat/PlusMenu';
import type { ActiveProjectNotFound } from './chat/ProjectPicker';
import PaperclipIcon from './icons/PaperclipIcon';
import CloseIcon from './icons/CloseIcon';
import { UploadSheetMount } from './chat/composerSlots';
import { attachmentKind } from './chat/attachmentKind';
import {
  addFiles as addChatFiles,
  addsToProject,
  attachProjectAsset,
  dismissFinishedProblems,
  removeAttachment,
  restoreReady,
  setAddToProject,
  takeReady,
  useChatAttachments,
  type AddFilesContext,
  type ReadyAttachment,
} from '../projects/upload/chatAttachments';
import { isInProgress } from '../projects/upload/uploadQueue';
import { listAssets, listProjects, parseProjectAccessLost, parseSessionProjectFixed, parseSessionProjectGone, peekProjectsEnabled, projectsEnabled, searchAssets } from '../projects/api';
import { findDuplicates, type Duplicate } from '../projects/upload/duplicates';
import { dragHasFiles, readDroppedFiles } from '../projects/upload/folders';
import DuplicatesDialog from './projects/DuplicatesDialog';
import type { AssetView, ProjectSummary, ProjectsResult } from '../projects/api';
import type { FindSimilarPayload } from '../projects/findSimilar';
import { setSessionTurnInProgress } from '../projects/turnState';
import { ActionLine } from './chat/ActionLine';
import {
  userMessageRowStyle, userBubbleStyle, userTextStyle, assistantMessageRowStyle, assistantBubbleStyle, markdownBodyStyle, errorIndicatorStyle, guardrailNoteStyle, streamingDotsContainerStyle, dotStyle, workingIndicatorStyle, workingLabelStyle, summaryContainerStyle, summaryItemStyle, dotKeyframes,
} from './chat/messageStyles';

import { CHAT_ACTION_EVENT, withActionContext, type ActionLineRecord, type ChatActionDetail } from '../projects/mediaActions';
import {
  dismissNotice,
  markSessionPersisted,
  markSessionProjectFixed,
  projectGoneNewChatMessage,
  reconcileWithProjectList,
  resetToPersonal,
  selectActiveProject,
  snapshot as snapshotSessionProject,
  useSessionProject,
} from '../projects/sessionProjectStore';

// --- Constants ---
/** Shown when a turn is refused because the session belongs to another project (asset-lineage R2.1). */
export const SESSION_PROJECT_FIXED_NOTICE =
  'This chat already belongs to another project. Use the + menu to start a new chat in the project you picked.';

const MAX_MESSAGES = 500;
export const MAX_QUEUE_SIZE = 10;
const MAX_INPUT_LENGTH = 10_000;

// The chat textarea grows with its content up to this height (px), then scrolls —
// enough to see ~8 lines of a long prompt at once without letting the input eat the
// whole pane.
const CHAT_TEXTAREA_MAX_HEIGHT = 200;

// Fallback client-side upload cap until /config reports the server's authoritative
// `maxUploadBytes` (which the proxy enforces). 2 GiB matches the proxy default.
const DEFAULT_MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024;


/**
 * One attachment's size as the prompt and the bubble state it: "12.3 MB", "11.6 KB", "512 B".
 * Megabytes alone printed every image under 50 KB as "0.0 MB" (session b42731df).
 */
export function formatAttachmentSize(bytes: number): string {
  const n = Number.isFinite(bytes) && bytes > 0 ? bytes : 0;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

/**
 * The model-only block that names one attached file, in the format the prompt has always carried (and
 * `promptNormalize` strips): name and size, the project asset it IS (asset-lineage R1.1 — identity is
 * `{project_id, asset_id}`, never a URL), bucket, key, S3 URI, and an HTTPS URL for fetching it.
 */
export function attachmentPromptBlock(a: ReadyAttachment): string {
  const lines = [`[File attached: ${a.name} (${formatAttachmentSize(a.size)})`];
  if (a.projectId) lines.push(`project_id: ${a.projectId}`);
  if (a.assetId) lines.push(`asset_id: ${a.assetId}`);
  const sourceUri = a.sourceUri || (a.bucket ? `s3://${a.bucket}/${a.key}` : '');
  if (a.bucket) lines.push(`S3 bucket: ${a.bucket}`);
  lines.push(`S3 key: ${a.key}`);
  if (sourceUri) lines.push(`Full S3 URI: ${sourceUri}`);
  if (a.url) {
    lines.push(`Public HTTPS URL (use this as the image_url/video_url for generation models like fal.ai — do NOT try to read the s3:// URI): ${a.url}`);
  }
  return `${lines.join('\n')}]`;
}

/** The outbound prompt: every attachment's block, in order, then the typed message. */
export function buildAttachmentPrompt(attachments: readonly ReadyAttachment[], message: string): string {
  if (attachments.length === 0) return message;
  return `${attachments.map(attachmentPromptBlock).join('\n\n')}\n\n${message}`;
}

/** The user bubble: one `📎 name (size)` line per attachment, then the typed message. */
export function attachmentBubbleText(attachments: readonly ReadyAttachment[], message: string): string {
  if (attachments.length === 0) return message;
  return `${attachments.map((a) => `📎 ${a.name} (${formatAttachmentSize(a.size)})`).join('\n')}\n${message}`;
}

// Stable identity for "this session has no live steps". A fresh [] on every
// render would give the memoized readers below a new dependency each pass.
const EMPTY_STEPS: StepEvent[] = [];
// Stable empty transcript, shown for the one render between a session switch and the
// [sessionId] reload effect (so the outgoing session's chat never paints under the new header).
const EMPTY_MESSAGES: ChatMessage[] = [];

// --- Exported utility for testability ---

/**
 * Enqueues a message into the queue, capped at MAX_QUEUE_SIZE.
 * Returns the new queue. If the queue is already at capacity, the message is dropped.
 */
export interface QueuedMessage { prompt: string; displayText?: string }

export function enqueueMessage(queue: QueuedMessage[], message: QueuedMessage): QueuedMessage[] {
  if (queue.length >= MAX_QUEUE_SIZE) {
    return queue;
  }
  return [...queue, message];
}

/**
 * Non-destructively merge a cached/durable transcript into the current in-memory one.
 *
 * The background auto-render path writes the session cache and fires
 * `session-cache-external-update`; the on-screen handler used to `setMessages(cachedCopy)`,
 * REPLACING state. But the cache can lag the latest `setMessages` (its write effect hadn't run)
 * or be built off a stale base, so replacing it WIPED the just-added assistant turn — which then
 * persisted away. Symptom: on async media workflows, every user turn survived but only the LAST
 * assistant reply did.
 *
 * This unions by message id — every in-memory turn is RETAINED (in-memory wins on id collision),
 * any cache/durable-only turn (e.g. a background job summary, or a turn restored on switch-back) is
 * added, ordered by timestamp and capped. The refresh can only ADD messages, never drop one.
 *
 * Since the proxy became the authoritative transcript writer, there is a SINGLE source shape
 * (`msg-` turns) — the former `mem-`/provisional dedup and content-normalization are gone. The
 * third argument is accepted for call-site compatibility but no longer used.
 */
export function mergeSessionMessages(current: ChatMessage[], cached: ChatMessage[], _liveAssistantContent = ''): ChatMessage[] {
  void _liveAssistantContent;
  const seenIds = new Set<string>();
  const keep = (m: ChatMessage): boolean => {
    if (!m) return false;
    if (m.id && seenIds.has(m.id)) return false; // exact same event already kept (in-memory wins)
    if (m.id) seenIds.add(m.id);
    return true;
  };

  const keptCurrent = current.filter(keep);
  const additions = cached.filter(keep); // keep() also skips ids already seen in current

  // No-op fast path: nothing to add → return the SAME reference so setMessages is a no-op and
  // React never re-renders (zero flicker).
  if (additions.length === 0) return current;

  // Preserve on-screen order; a genuinely-missing (restored) turn is placed by timestamp so it
  // lands in its correct spot rather than always at the end.
  const merged = [...keptCurrent, ...additions].sort((a, b) => (a.timestamp ?? 0) - (b.timestamp ?? 0));
  return merged.length > MAX_MESSAGES ? merged.slice(merged.length - MAX_MESSAGES) : merged;
}

/**
 * While a turn streams, the timeline actions the user made since its question: they belong AFTER the turn's reply
 * (which is still the live bubble), so they draw below it rather than above an unfinished answer.
 */
export function splitLateActions(messages: ChatMessage[]): { before: ChatMessage[]; late: ChatMessage[] } {
  let lastUser = -1;
  for (let i = messages.length - 1; i >= 0; i--) if (messages[i].role === 'user') { lastUser = i; break; }
  if (lastUser < 0) return { before: messages, late: [] };
  const before: ChatMessage[] = [];
  const late: ChatMessage[] = [];
  messages.forEach((m, i) => (i > lastUser && m.role === 'action' ? late : before).push(m));
  return late.length ? { before, late } : { before: messages, late };
}

/**
 * Commit a finished turn's reply right after its question — ahead of any timeline action made while it ran, which
 * is where the proxy's durable copy (stamped question + 1 ms) puts it on reload too.
 */
export function commitTurnReply(prev: ChatMessage[], reply: ChatMessage, userMsgId: string): ChatMessage[] {
  const u = prev.findIndex(m => m.id === userMsgId);
  if (u < 0) return [...prev, reply];
  let at = u + 1;
  while (at < prev.length && prev[at].role === 'action') at++;
  // Only actions follow the question: the reply goes before them, stamped like the durable copy.
  if (at === prev.length && at > u + 1) {
    const stamped = { ...reply, timestamp: (prev[u].timestamp ?? reply.timestamp) + 1 };
    return [...prev.slice(0, u + 1), stamped, ...prev.slice(u + 1)];
  }
  return [...prev, reply];
}

/**
 * Validates chat input: must be non-empty, non-whitespace-only, and <= 10,000 chars.
 */
export function isValidChatInput(input: string): boolean {
  return input.trim().length > 0 && input.length <= MAX_INPUT_LENGTH;
}

export function deriveInitialSessionTitle(
  message: string,
  fileName?: string,
  localTitle = 'New Session',
): string {
  if (localTitle.trim() && localTitle !== 'New Session') return localTitle;
  const source = message.trim() || fileName?.trim() || 'New Session';
  return source.slice(0, 40) + (source.length > 40 ? '…' : '');
}
// --- Component ---

interface ChatCanvasProps {
  /**
   * A render block produced by a turn, addressed to the session that ORIGINATED
   * the turn (captured at dispatch time), NOT whatever session is active when
   * the event lands. WorkspaceView routes on this: a render from a background
   * (not-on-screen) session is persisted into that session instead of leaking a
   * tab into the viewed session's Renderer. See the per-session routing note in
   * `processMessage`.
   */
  onRenderBlock?: (entry: import('./renderer/types').RenderHistoryEntry, originatingSessionId: string) => void;
  /** Current local sidebar title, including an optional pre-send rename. */
  sessionTitle?: string;
  /** Called synchronously once valid input starts, before persistence awaits. */
  onSessionInputStarted?: (sessionId: string) => void;
  /** Called after the session row is created or confirmed in DynamoDB. */
  onSessionPersisted?: (sessionId: string, createdTitle?: string) => void;
  /**
   * Click handler for inline `JobReference` chips rendered alongside
   * tool-call results that carry a `job_id` (task 13.3 / R7.2). When
   * provided, each chip wires its `onActivate` to call this with its
   * `jobId`; the parent (WorkspaceView) routes that to
   * `SessionSidebar.scrollToJob`, scrolling the matching sidebar row
   * into view and flashing the highlight styling. Optional — the chip
   * still renders with a no-op click when omitted.
   */
  onActivateJob?: (jobId: string) => void;
  /**
   * Called with `true` when render-agent activity appears in the stream and
   * `false` when the render arrives or the turn ends — drives the renderer
   * panel's "Rendering…" animation. Carries the ORIGINATING session id (captured
   * at dispatch time) so WorkspaceView only drives the pending animation / auto-
   * opens the drawer when that session is the one on screen — a background
   * session's turn must not pop the renderer over the viewed session.
   */
  onRenderPending?: (pending: boolean, originatingSessionId: string) => void;
  /** Leading "Composing…" marker → open a per-tab placeholder, correlated by id. */
  onRenderComposing?: (renderRequestId: string, label: string | undefined, originatingSessionId: string) => void;
  /** Terminal for a composing render that yielded no manifest → drop the placeholder. */
  onRenderFailed?: (renderRequestId: string, originatingSessionId: string) => void;
  /**
   * A turn to send on the user's behalf, identified by `id` so it fires exactly
   * once. Used when opening a completion notification: the user expects the
   * results, not just a rendered tab, and previously had to ask for them by hand.
   * Queued if a turn is already streaming.
   */
  pendingPrompt?: { id: string; text: string } | null;
  /**
   * When true the active session is frozen (demo lock): the composer (input,
   * Send, Upload) is disabled and submits are blocked. The server also returns
   * 423 for a locked session's turn, so this is the UX mirror of that gate.
   */
  locked?: boolean;
  /**
   * Plus_Menu `Manage projects` (and the Upload_Sheet's `View in Projects`): open the Projects_Surface on
   * this Project (Req 3.5). WorkspaceView wires it; absent, the item does nothing.
   */
  onManageProjects?: (activeProjectId: string) => void;
}

export default function ChatCanvas({
  onRenderBlock,
  onActivateJob,
  onRenderPending,
  onRenderComposing,
  onRenderFailed,
  sessionTitle = 'New Session',
  onSessionInputStarted,
  onSessionPersisted,
  pendingPrompt,
  locked = false,
  onManageProjects,
}: ChatCanvasProps = {}) {
  const { sessionId, refreshCategory } = useLabContext();

  // Personalized greeting name for the empty-state welcome. Use the signed-in
  // user's first token (from the Cognito display name — name / given_name /
  // email local-part), falling back to a neutral greeting when we don't have a
  // real name (anonymous / dev / unresolved claims). Previously hardcoded.
  const { displayName, mode } = useCurrentUser();
  const greetingName = useMemo(() => {
    if (mode === 'anonymous') return '';
    const label = (displayName || '').trim();
    if (!label || label === 'Anonymous' || label === 'Not signed in') return '';
    const first = label.split(/[\s@]/).filter(Boolean)[0] || '';
    if (!first) return '';
    return first.charAt(0).toUpperCase() + first.slice(1);
  }, [displayName, mode]);
  const welcomeHeading = greetingName
    ? `Hi ${greetingName}, what's on your mind?`
    : `Hi there, what's on your mind?`;

  // Internal messages state — initialize from localStorage; backend fetch happens in effect below
  const [messages, setMessages] = useState<ChatMessage[]>(() => {
    try {
      const stored = localStorage.getItem(`lab-session-${sessionId}`);
      if (stored) {
        const parsed = JSON.parse(stored);
        if (Array.isArray(parsed) && parsed.length > 0) return parsed;
      }
    } catch { /* ignore */ }
    return [];
  });
  // The committed messages, for reads that must not wait on a lazily-run state updater: the timeline action
  // summary is built when a turn is SENT, and `setMessages(prev => …)` snapshots run only at the next render.
  const committedMessagesRef = useRef<ChatMessage[]>([]);
  committedMessagesRef.current = messages;

  // Restore a session's transcript from the DURABLE record. Since the proxy became the
  // authoritative writer (docs/design-proxy-authoritative-transcript.md), `getSessionTranscript`
  // returns the COMPLETE rich conversation (full history from S3, rich step groups + duration),
  // so this is a single read — no AgentCore Memory merge, no dedup. Memory is now the model's
  // context only (still viewable via the raw-Memory debug path), never the display source.
  // Returns `[]` when the session has no durable transcript yet.
  const loadPersistedMessages = useCallback(async (sid: string): Promise<ChatMessage[]> => {
    try {
      return await getSessionTranscript(sid);
    } catch {
      return []; // durable unavailable — paint from localStorage cache / live stream instead
    }
  }, []);

  // On initial mount, reconcile against the durable rich transcript (the authoritative,
  // cross-browser source), even when localStorage seeded some messages — a stale/partial local
  // cache must not mask persisted turns. Merge is a non-destructive union by id: it retains any
  // already-present turn and restores durable-only turns; the localStorage write effect then
  // re-persists the reconciled set.
  const initialLoadRef = useRef(false);
  useEffect(() => {
    if (initialLoadRef.current) return;
    initialLoadRef.current = true;
    loadPersistedMessages(sessionId).then(loaded => {
      if (loaded.length === 0) return;
      // Durable is the authoritative, complete record. REPLACE the localStorage instant-paint copy
      // with it (rather than union) so a cache written with a different id scheme can't double a
      // turn. Only union when THIS session is mid-stream, to protect an in-flight turn the durable
      // record doesn't hold yet.
      setMessages(prev => inFlightSessionsRef.current.has(sessionId)
        ? mergeSessionMessages(prev, loaded, liveTextRef.current)
        : loaded);
    }).catch(() => {});
  }, []);
  // Live stream state keyed by the session that OWNS the stream.
  //
  // These were three singletons (StepEvent[], string, boolean). Because ONE
  // ChatCanvas instance persists across session switches (WorkspaceView mounts
  // it WITHOUT a key) and a switch does NOT abort the in-flight dispatch, the
  // old stream's callbacks kept writing into the shared buffers via
  // handleStreamEvent — so session A's tokens streamed into session B's view,
  // then "corrected" when the guarded completion path filed the real message
  // into A and cleared the buffers. Keying by session is the same fix already
  // applied to `messageQueues` and `inFlightSessionsRef` below, for the same
  // reason.
  //
  // Keying also removes the need to CLEAR live state on switch (see the switch
  // effect): a stream you navigate away from stays visible in its own session,
  // so switching back mid-turn shows it still running instead of a blank panel.
  const [liveStepsBySession, setLiveStepsBySession] = useState<Record<string, StepEvent[]>>({});
  const [liveTextBySession, setLiveTextBySession] = useState<Record<string, string>>({});
  const [streamingSessions, setStreamingSessions] = useState<ReadonlySet<string>>(() => new Set());

  // The on-screen session's slice. Every read site below consumes these, so
  // scoping the writes did not have to touch the ~40 readers.
  const liveSteps = liveStepsBySession[sessionId] ?? EMPTY_STEPS;
  const liveText = liveTextBySession[sessionId] ?? '';
  const isStreaming = streamingSessions.has(sessionId);

  // Chat input state
  const [input, setInput] = useState('');
  // Auto-grow the chat textarea with its content (up to CHAT_TEXTAREA_MAX_HEIGHT,
  // then scroll), so a long prompt is fully visible instead of trapped in one line.
  // Runs on every `input` change — covers typing, clearing after send, and external
  // prefills via the `chat-input:set` event.
  const chatTextareaRef = useRef<HTMLTextAreaElement>(null);
  const fittedWidthRef = useRef(-1); // the width the current height was measured at
  const fitChatTextarea = useCallback(() => {
    const el = chatTextareaRef.current;
    if (!el) return;
    fittedWidthRef.current = el.clientWidth;
    el.style.height = 'auto'; // shrink-to-fit first so deleting text lowers the box
    el.style.height = `${Math.min(el.scrollHeight, CHAT_TEXTAREA_MAX_HEIGHT)}px`;
  }, []);
  useLayoutEffect(() => { fitChatTextarea(); }, [input, fitChatTextarea]);
  // ...and whenever its WIDTH changes. Measured only on input changes, a box sized while the pane was narrow
  // (a Renderer tab opening, the sidebar or window resizing, the chat pane hidden behind another surface) kept
  // that wrapped height after the pane widened: a near-empty composer stayed ~200px tall until the text
  // changed or the page was reloaded. Width only — re-fitting sets the height, which must not re-trigger it.
  const [chatTextareaEl, setChatTextareaEl] = useState<HTMLTextAreaElement | null>(null);
  const setChatTextareaRef = useCallback((el: HTMLTextAreaElement | null) => {
    chatTextareaRef.current = el;
    setChatTextareaEl(el);
  }, []);
  useEffect(() => {
    const el = chatTextareaEl;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(() => {
      if (el.clientWidth !== fittedWidthRef.current) fitChatTextarea();
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [chatTextareaEl, fitChatTextarea]);
  // Outbound messages waiting for their OWN session's in-flight turn to finish,
  // keyed by the session they were typed in. This used to be a single shared
  // string[]; because ONE ChatCanvas instance persists across session switches
  // (WorkspaceView mounts it WITHOUT a key), that shared queue let a message
  // typed in session B dequeue after session A's unrelated stream ended and run
  // against whatever session was active at that moment. Keying by sessionId
  // guarantees a queued message can only ever execute in the session it was
  // typed in.
  const [messageQueues, setMessageQueues] = useState<Record<string, QueuedMessage[]>>({});
  // In-flight dispatches keyed by the session that OWNS them. Previously a
  // single boolean `isProcessingRef`: with the persisted single instance, a
  // dispatch started in session A left that boolean true, so a brand-new
  // session B — whose own `isStreaming` was reset to false on the switch effect
  // below — still saw "processing" in handleSubmit and QUEUED the user's first
  // message behind A's turn (the reported "new session shows '1 message
  // queued'" bug). Keying by sessionId lets B dispatch immediately while A is
  // still mid-flight, and each session's busy state is tracked independently.
  const inFlightSessionsRef = useRef<Set<string>>(new Set());
  // Abort controllers keyed by owning session. A single ref meant the second
  // concurrent stream overwrote the first, so Stop aborted the WRONG session's
  // stream and left the other one unstoppable.
  const abortControllersRef = useRef<Map<string, AbortController>>(new Map());

  // The queue belonging to the session currently on screen. Empty when this
  // session has nothing waiting, even if OTHER sessions have queued messages.
  const currentQueue = messageQueues[sessionId] ?? [];

  // Listen for `chat-input:set` window events so external surfaces
  // (currently the Jobs sidebar's "Get results" action button) can
  // pre-fill the chat input without prop-drilling through the whole
  // workspace tree. The contract is intentionally narrow: the
  // dispatcher hands us a string, we replace `input` with it, and
  // never auto-submit. The user reviews the prefilled prompt and
  // presses Enter (or edits) themselves — surprising the user by
  // submitting on their behalf would erode trust in the affordance.
  // Mirrors the existing `model-changed` window-event pattern used
  // in WorkspaceView.
  useEffect(() => {
    const handler = (e: Event) => {
      const ce = e as CustomEvent<{ value?: unknown }>;
      const value = ce.detail?.value;
      if (typeof value === 'string') {
        setInput(value);
      }
    };
    window.addEventListener('chat-input:set', handler as EventListener);
    return () => {
      window.removeEventListener('chat-input:set', handler as EventListener);
    };
  }, []);

  // File attachments. Files upload as soon as they are chosen (into the Active_Project, or this chat only), so
  // the session's attachment store holds them per session; a finished file is a chip until the turn takes it.
  const fileInputRef = useRef<HTMLInputElement>(null);
  const chatAttachments = useChatAttachments(sessionId);
  const readyAttachments = useMemo(
    () => chatAttachments.items.filter(it => it.status === 'ready' && it.result),
    [chatAttachments.items],
  );
  const uploadsBusy = chatAttachments.items.some(it => isInProgress(it.status));
  // A batch-level problem (e.g. too many files) shown near the input as a transient alert.
  const [uploadError, setUploadError] = useState<string | null>(null);
  // Authoritative upload cap from the proxy (/config); the proxy also enforces it with
  // a 413, this just lets us reject an oversized file before wasting a round trip.
  const [maxUploadBytes, setMaxUploadBytes] = useState<number>(DEFAULT_MAX_UPLOAD_BYTES);
  useEffect(() => {
    getConfig()
      .then(c => { if (typeof c.maxUploadBytes === 'number' && c.maxUploadBytes > 0) setMaxUploadBytes(c.maxUploadBytes); })
      .catch(() => { /* keep the default */ });
  }, []);

  // --- Projects (media-harness-projects-access; design Component 4) -------------------------------
  // `/config.projectsEnabled`. Off: no status line, Plus_Menu Recent/Project sections, or per-turn project
  // fields — the Plus_Menu still renders with `Attach files`, the only home of chat attachment now.
  const [projectsOn, setProjectsOn] = useState(() => peekProjectsEnabled() ?? false);
  useEffect(() => {
    let live = true;
    projectsEnabled().then(on => { if (live) setProjectsOn(on); }).catch(() => {});
    return () => { live = false; };
  }, []);
  const projectsOnRef = useRef(projectsOn);
  projectsOnRef.current = projectsOn;
  // Null when Projects are off or the session is not seeded yet (WorkspaceView seeds from the row).
  const sessionProject = useSessionProject(projectsOn ? sessionId : null);
  // The latest `GET /projects` rows, so a Plus_Menu project pick can label the composer with the row's name.
  const lastProjectListRef = useRef<readonly ProjectSummary[]>([]);
  const composerStatusId = useId();

  const loadProjects = useCallback(async (): Promise<ProjectsResult<ProjectSummary[]>> => {
    const r = await listProjects();
    if (r.ok) lastProjectListRef.current = r.value;
    return r;
  }, []);

  // Label the Active_Project (the row carries only its id) or, for a session with none yet, take Personal.
  // Once per session; the Plus_Menu's open-time read covers later changes.
  const reconciledSessionsRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    if (!projectsOn || !sessionProject) return;
    if (sessionProject.activeProjectId && sessionProject.activeProjectName) return;
    if (reconciledSessionsRef.current.has(sessionId)) return;
    reconciledSessionsRef.current.add(sessionId);
    const sid = sessionId;
    loadProjects()
      .then(r => { if (r.ok) return reconcileWithProjectList(sid, r.value); })
      .catch(() => { /* the turn gate falls back to the row / Personal */ });
  }, [projectsOn, sessionProject, sessionId, loadProjects]);

  const handleSelectProject = useCallback((projectId: string) => {
    const row = lastProjectListRef.current.find(p => p.projectId === projectId);
    void selectActiveProject(sessionId, projectId, row?.name ?? null);
  }, [sessionId]);

  const handleActiveProjectNotFound = useCallback((lost: ActiveProjectNotFound) => {
    if (!lost.personal) return; // nothing to switch to; the turn gate will 409 and switch server-side
    void resetToPersonal(sessionId, {
      personalProject: { id: lost.personal.projectId, name: lost.personal.name },
      lostProjectName: lost.projectName || null,
    });
  }, [sessionId]);

  const handleManageProjects = useCallback((activeProjectId: string) => {
    onManageProjects?.(activeProjectId);
  }, [onManageProjects]);

  // Scroll container for the transcript. (Pins to bottom on new messages/stream.)
  const scrollContainerRef = useRef<HTMLDivElement>(null);

  // Track whether a session switch is in progress to prevent stale writes
  const switchingRef = useRef(false);
  const currentSessionRef = useRef(sessionId);
  // Track whether this session has been persisted to DynamoDB yet
  const sessionCreatedRef = useRef(false);

  // Persist messages to localStorage when they change (skip during session switch)
  useEffect(() => {
    if (switchingRef.current) return;
    if (messages.length > 0) {
      try {
        setSessionCache(currentSessionRef.current, JSON.stringify(messages));
        window.dispatchEvent(new CustomEvent('session-messages-updated'));
      } catch { /* quota exceeded — silently drop */ }
    }
  }, [messages]);

  // Durable transcript persistence moved to the PROXY (docs/design-proxy-authoritative-transcript.md):
  // it accumulates the SSE stream server-side and upserts the turn to S3 + the DynamoDB row at
  // completion (and throttled mid-stream for reload recovery), even if the browser navigates away.
  // The client therefore no longer debounces a `updateSessionTranscript` PATCH here, nor keeps a
  // `messagesRef` mirror for a synchronous durable write — localStorage (below) stays as the fast
  // within-browser instant-paint cache only.

  // Track session changes — load from localStorage cache or the durable transcript.
  useEffect(() => {
    if (currentSessionRef.current === sessionId) return;
    switchingRef.current = true;
    sessionCreatedRef.current = false;
    currentSessionRef.current = sessionId;
    // Live state is keyed by owning session (see the declarations above), so
    // there is nothing to clear here any more. This used to reset liveSteps /
    // liveText / isStreaming defensively, because the previous session's still-
    // running dispatch wrote into shared buffers and would otherwise bleed into
    // this view. That reset was itself lossy: navigating back to a session
    // mid-turn showed an empty panel until the turn completed. Now each
    // session's live stream stays with that session, and the switch is a pure
    // change of which slice is displayed.
    // Deliberately NOT reset here: `inFlightSessionsRef` and `messageQueues`.
    // Both are keyed by sessionId, so they stay correctly scoped across the
    // switch. Clearing the in-flight set would wrongly mark the OTHER session's
    // still-running dispatch as finished; clearing the queues would drop a
    // message the user legitimately queued for a session that is still busy.

    // Paint instantly from the localStorage cache if present, but do NOT stop there:
    // always reconcile against the durable transcript below so a stale/partial cache
    // can't mask persisted turns (the durable row is the authoritative source).
    let hadLocalCache = false;
    try {
      const stored = localStorage.getItem(`lab-session-${sessionId}`);
      if (stored) {
        const parsed = JSON.parse(stored);
        if (Array.isArray(parsed) && parsed.length > 0) {
          setMessages(parsed);
          hadLocalCache = true;
        }
      }
    } catch { /* fall through */ }
    if (!hadLocalCache) setMessages([]);

    // Fetch the durable rich transcript (then Memory) and MERGE (union by id): any
    // cached/in-flight turn is retained and durable-only turns are restored — never
    // masked. The localStorage write effect re-persists the reconciled set once the
    // switch guard clears.
    loadPersistedMessages(sessionId).then(loaded => {
      // A rapid A→B→A switch can resolve this after the user left B; only apply
      // if B is still the session on screen.
      if (currentSessionRef.current !== sessionId) return;
      if (loaded.length > 0) {
        // Durable is authoritative — REPLACE the instant-paint cache with it (union only when the
        // session is mid-stream, to keep an in-flight turn the durable record lacks). This prevents
        // a cache written with a different id scheme from doubling a turn on switch-in.
        setMessages(prev => inFlightSessionsRef.current.has(sessionId)
          ? mergeSessionMessages(prev, loaded, liveTextRef.current)
          : loaded);
      }
    }).catch(() => {}).finally(() => {
      switchingRef.current = false;
    });
  }, [sessionId]);

  // Live-refresh the transcript when a BACKGROUND write (implicit auto-render
  // on job completion, or a mid-stream completion) lands in the session that
  // is currently on screen. Reloads from the cache the background path already
  // wrote. Guarded so it never clobbers an in-flight local stream.
  useEffect(() => {
    const handler = (e: Event) => {
      const ce = e as CustomEvent<{ sessionId?: string }>;
      if (ce.detail?.sessionId !== sessionId) return;
      if (isStreaming || inFlightSessionsRef.current.has(sessionId) || switchingRef.current) return;
      try {
        const stored = localStorage.getItem(`lab-session-${sessionId}`);
        if (stored) {
          const parsed = JSON.parse(stored);
          if (Array.isArray(parsed)) setMessages(prev => mergeSessionMessages(prev, parsed, liveTextRef.current));
        }
      } catch { /* ignore */ }
    };
    window.addEventListener('session-cache-external-update', handler as EventListener);
    return () => window.removeEventListener('session-cache-external-update', handler as EventListener);
  }, [sessionId, isStreaming]);

  // Auto-scroll to bottom when new messages arrive or live text updates
  useEffect(() => {
    const container = scrollContainerRef.current;
    if (container) {
      container.scrollTop = container.scrollHeight;
    }
  }, [messages.length, liveText]);

  // Render the full (MAX_MESSAGES-capped) transcript directly. A previous
  // fixed-height virtual window (80px/message) computed scroll spacers from an
  // estimate that is wildly wrong for real chat turns (code, images, collapsed
  // step groups, render tabs), which desynced the scrollbar from real content
  // and made the bottom unreachable once a session passed ~100 messages. Chat
  // turns are variable-height; a correct virtual window would need real
  // measurement. With MAX_MESSAGES=500 capped, rendering directly is correct.
  // `messages` is a single array, not keyed by session (unlike the live-stream maps). On a
  // session switch, `sessionId` (from LabContext) changes and this component re-renders BEFORE
  // the `[sessionId]` effect reloads `messages` for the new session — so for one render `messages`
  // still holds the OUTGOING session's turns. `currentSessionRef` is updated inside that effect,
  // so until it catches up we are showing a stale transcript: gate on it so the previous session's
  // chat can never paint under the new session's header (the "leftover chat on a new session until
  // refresh" bug). During that window we render the new session's empty state instead.
  const showingActiveSession = currentSessionRef.current === sessionId;
  const messagesToRender = showingActiveSession ? messages : EMPTY_MESSAGES;
  const renderMessage = (msg: ChatMessage) => (msg.role === 'action' && msg.action
    ? <ActionLine key={msg.id} actionId={msg.id.replace(/-x$/, '')} action={msg.action as ActionLineRecord} sessionId={sessionId} />
    : <MessageBubble key={msg.id} message={msg} sessionId={sessionId} onActivateJob={onActivateJob} />);

  // Expose methods for parent components to add messages and handle streaming
  // These are used by ChatInput (task 11.2) via a shared ref or direct state manipulation
  const addUserMessage = useCallback((content: string, id?: string) => {
    const msg: ChatMessage = {
      id: id ?? `msg-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      role: 'user',
      content,
      timestamp: Date.now(),
    };
    setMessages(prev => {
      const next = [...prev, msg];
      return next.length > MAX_MESSAGES ? next.slice(next.length - MAX_MESSAGES) : next;
    });
    return msg.id;
  }, []);

  // Create the session row once, as the first turn does, so an upload that starts before the first message lands
  // in this session's input prefix (`POST /upload` only honours a session the actor owns). Concurrent callers (an
  // attach and then a quick send) share the one request.
  const sessionPersistInFlightRef = useRef<Map<string, Promise<boolean>>>(new Map());
  const ensureSessionPersisted = useCallback((sid: string, initialTitle: string): Promise<boolean> => {
    if (sessionCreatedRef.current && currentSessionRef.current === sid) return Promise.resolve(true);
    const inflight = sessionPersistInFlightRef.current.get(sid);
    if (inflight) return inflight;
    const run = async (): Promise<boolean> => {
      try {
        const created = await createSession(sid, initialTitle);
        // The row exists now (created or already there): Project changes may PATCH it from here on.
        markSessionPersisted(sid);
        if (currentSessionRef.current === sid) {
          sessionCreatedRef.current = true;
        }
        onSessionPersisted?.(sid, created ? initialTitle : undefined);
        return true;
      } catch (error) {
        // Chat remains available during a transient metadata-store failure.
        // Do not PATCH a missing row later; the next input will retry creation.
        console.warn('[chat] Failed to persist session metadata:', error);
        return false;
      }
    };
    const p = run();
    sessionPersistInFlightRef.current.set(sid, p);
    void p.then(() => {
      if (sessionPersistInFlightRef.current.get(sid) === p) sessionPersistInFlightRef.current.delete(sid);
    });
    return p;
  }, [onSessionPersisted]);

  // Where a chosen file goes: the Active_Project (switch ON, the default) or this chat only. Projects off, or an
  // Active_Project not labelled yet, means this chat only.
  const uploadProjectId = projectsOn ? sessionProject?.activeProjectId ?? null : null;
  const uploadProjectName = projectsOn ? sessionProject?.activeProjectName ?? null : null;
  const uploadProject = useMemo(
    () => (uploadProjectId && uploadProjectName ? { id: uploadProjectId, name: uploadProjectName } : null),
    [uploadProjectId, uploadProjectName],
  );
  const attachContext = useCallback((firstFileName?: string): AddFilesContext => {
    const sid = sessionId;
    const title = deriveInitialSessionTitle(input, firstFileName, sessionTitle);
    return {
      project: uploadProject,
      maxChatBytes: maxUploadBytes,
      ensureSession: () => ensureSessionPersisted(sid, title),
    };
  }, [sessionId, input, sessionTitle, uploadProject, maxUploadBytes, ensureSessionPersisted]);

  // The ONE attachment path shared by the Plus_Menu's `Attach files` (via the hidden input), a drop on the
  // composer, and a paste (Req 3.15, 3.19): 1 to 20 files per action, each uploading at once.
  const sendChatFiles = useCallback((sid: string, files: File[], ctx: AddFilesContext) => {
    if (files.length === 0) return;
    void addChatFiles(sid, files, ctx).then(r => {
      if (!r.ok && currentSessionRef.current === sid) setUploadError(r.message);
    });
  }, []);

  // Files bound for the Active_Project that are already in it: one prompt for the batch.
  const [pendingDup, setPendingDup] = useState<{ sid: string; files: File[]; duplicates: Duplicate[]; ctx: AddFilesContext } | null>(null);

  const attachFiles = useCallback((files: ArrayLike<File>) => {
    if (locked || files.length === 0) return;
    setUploadError(null);
    const sid = sessionId;
    const list = Array.from(files);
    const ctx = attachContext(list[0]?.name);
    if (!ctx.project || !addsToProject(sid, ctx)) {
      sendChatFiles(sid, list, ctx);
      return;
    }
    const project = ctx.project;
    // Fails open: if the check cannot run, the files upload as before.
    void findDuplicates(project.id, list).catch(() => [] as Duplicate[]).then((duplicates) => {
      if (currentSessionRef.current !== sid) return;
      if (duplicates.length === 0) sendChatFiles(sid, list, ctx);
      else setPendingDup({ sid, files: list, duplicates, ctx });
    });
  }, [locked, sessionId, attachContext, sendChatFiles]);

  const resolveDuplicates = useCallback((choice: 'existing' | 'both' | 'cancel') => {
    const p = pendingDup;
    setPendingDup(null);
    if (!p || choice === 'cancel' || !p.ctx.project) return;
    if (choice === 'both') {
      sendChatFiles(p.sid, p.files, p.ctx);
      return;
    }
    // Use existing: attach each matching asset instead of uploading it again; upload only the new files.
    const project = p.ctx.project;
    const dup = new Set(p.duplicates.map((d) => d.file));
    p.duplicates.forEach((d) => attachProjectAsset(p.sid, project, d.existing, p.ctx));
    sendChatFiles(p.sid, p.files.filter((f) => !dup.has(f)), p.ctx);
  }, [pendingDup, sendChatFiles]);

  const handleFileSelect = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    // Copy before clearing: resetting `value` empties the input's FileList.
    const files = Array.from(e.target.files ?? []);
    e.target.value = '';
    if (files.length === 0) return;
    attachFiles(files);
  }, [attachFiles]);

  const handleComposerDragOver = useCallback((e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
  }, []);

  const handleComposerDrop = useCallback((e: React.DragEvent<HTMLDivElement>) => {
    const dt = e.dataTransfer;
    if (!dragHasFiles(dt)) return; // text drop: the browser's default
    // Always swallow a FILE drop — otherwise the browser navigates to the file — but attach
    // nothing while the session is locked (Req 3.23).
    e.preventDefault();
    if (locked) return;
    // A dropped folder is walked (its files keep their relative folder for the Active_Project); the chat's
    // per-message file cap still applies to the result. The items must be read during the event.
    void readDroppedFiles(dt).then((files) => {
      if (files.length > 0) attachFiles(files);
    });
  }, [locked, attachFiles]);

  const handleTextareaPaste = useCallback((e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const files = e.clipboardData?.files;
    // No file: return BEFORE preventDefault so the text pastes as it always has (Req 3.21).
    if (!files || files.length === 0) return;
    e.preventDefault();
    if (locked) return;
    attachFiles(Array.from(files));
  }, [locked, attachFiles]);

  // Plus_Menu `Attach files`: the multi-file picker (Req 3.15).
  const handleAttachToChat = useCallback(() => {
    if (locked) return;
    fileInputRef.current?.click();
  }, [locked]);

  // Plus_Menu `Recent in <Active_Project>`: attach an asset already in the project without re-uploading it — the
  // same session-row creation as an upload, then a `chat-copy` into this session, then the same chip.
  const handleAttachRecentAsset = useCallback((asset: AssetView, project: { id: string; name: string }) => {
    if (locked) return;
    setUploadError(null);
    attachProjectAsset(sessionId, project, asset, attachContext(asset.displayName));
  }, [locked, sessionId, attachContext]);

  // The upload sheet's `Add to <Active_Project>` switch. Files still transferring move to the new path.
  const handleAddToProjectChange = useCallback((on: boolean) => {
    void setAddToProject(sessionId, on, attachContext());
  }, [sessionId, attachContext]);

  const startStreaming = useCallback((owner?: string) => {
    const sid = owner || currentSessionRef.current;
    setStreamingSessions(prev => {
      const next = new Set(prev);
      next.add(sid);
      return next;
    });
    // Mirror this session's turn as in-progress so a Moment card's Find similar gate (Req 3.24) is
    // correct at first paint and updates live via `lab:turn-state`.
    setSessionTurnInProgress(sid, true);
    setLiveStepsBySession(prev => ({ ...prev, [sid]: [] }));
    setLiveTextBySession(prev => ({ ...prev, [sid]: '' }));
  }, []);

  // Drop a finished session's live slice. Keeps the maps from growing with one
  // entry per session visited, and makes the "is this session streaming" reads
  // false again without touching any other session's stream.
  const clearLiveState = useCallback((owner: string) => {
    setStreamingSessions(prev => {
      if (!prev.has(owner)) return prev;
      const next = new Set(prev);
      next.delete(owner);
      return next;
    });
    // This session's turn is no longer streaming — update the mirror and fire `lab:turn-state`.
    setSessionTurnInProgress(owner, false);
    setLiveStepsBySession(prev => {
      if (!(owner in prev)) return prev;
      const { [owner]: _drop, ...rest } = prev;
      return rest;
    });
    setLiveTextBySession(prev => {
      if (!(owner in prev)) return prev;
      const { [owner]: _drop, ...rest } = prev;
      return rest;
    });
  }, []);

  // Writes are addressed to the session that OWNS the stream, never to whatever
  // is on screen. `owner` defaults to the active session for the few callers
  // that are inherently user-driven (chatActionsRef consumers).
  const handleStreamEvent = useCallback((event: StreamEvent, owner?: string) => {
    const sid = owner || currentSessionRef.current;
    switch (event.type) {
      case 'step':
        setLiveStepsBySession(prev => {
          const cur = prev[sid] ?? [];
          // Update existing step or add new one
          const existing = cur.findIndex(s => s.node_id === event.step.node_id && event.step.node_id);
          if (existing >= 0) {
            const updated = [...cur];
            updated[existing] = event.step;
            return { ...prev, [sid]: updated };
          }
          return { ...prev, [sid]: [...cur, event.step] };
        });
        break;
      case 'text':
        setLiveTextBySession(prev => ({ ...prev, [sid]: (prev[sid] ?? '') + event.text }));
        break;
      case 'execution_summary':
        // execution_summary is handled by the caller via finalizeStreamWithSummary
        // This case is intentionally a no-op here since the caller orchestrates finalization
        break;
      case 'draft_mutation':
        // When the agent creates or modifies a draft, refresh the affected inventory drawer
        refreshCategory(event.category);
        break;
    }
  }, [refreshCategory]);

  const finalizeStream = useCallback((error?: string) => {
    setMessages(prev => {
      const assistantMsg: ChatMessage = {
        id: `msg-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        role: 'assistant',
        content: liveText,
        steps: liveSteps.length > 0 ? [...liveSteps] : undefined,
        summary: undefined,
        error,
        timestamp: Date.now(),
      };
      // Commit the accumulated turn AND collapse any per-cycle Memory (mem-) fragments it
      // now subsumes, so the turn never lingers as a duplicate after finalizing in place.
      return mergeSessionMessages([...prev, assistantMsg], []);
    });
    clearLiveState(currentSessionRef.current);
  }, [liveText, liveSteps, clearLiveState]);

  const finalizeStreamWithSummary = useCallback((summary: ExecutionSummary) => {
    setMessages(prev => {
      const assistantMsg: ChatMessage = {
        id: `msg-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        role: 'assistant',
        content: liveText,
        steps: liveSteps.length > 0 ? [...liveSteps] : undefined,
        summary,
        timestamp: Date.now(),
      };
      // Commit the accumulated turn AND collapse any per-cycle Memory (mem-) fragments it
      // now subsumes, so the turn never lingers as a duplicate after finalizing in place.
      return mergeSessionMessages([...prev, assistantMsg], []);
    });
    clearLiveState(currentSessionRef.current);
  }, [liveText, liveSteps, clearLiveState]);

  // Expose chat state and actions for child components (task 11.2 will use these)
  // Store on a ref so ChatInput can access them
  const chatActionsRef = useRef({
    addUserMessage,
    startStreaming,
    handleStreamEvent,
    finalizeStream,
    finalizeStreamWithSummary,
    messages,
    isStreaming,
  });
  chatActionsRef.current = {
    addUserMessage,
    startStreaming,
    handleStreamEvent,
    finalizeStream,
    finalizeStreamWithSummary,
    messages,
    isStreaming,
  };

  // --- Message submission logic ---

  // Refs to access latest state in async callbacks
  const liveTextRef = useRef(liveText);
  liveTextRef.current = liveText;
  const liveStepsRef = useRef(liveSteps);
  liveStepsRef.current = liveSteps;

  // Append a completed message directly to a specific session's localStorage
  // cache. Used when a stream finishes AFTER the user has switched away from
  // the session that started it: writing into React `messages` would mis-file
  // the turn under whatever session is now on screen (the reported "missing
  // conversation turn" bug). The runtime's AgentCore Memory is the durable
  // source of truth; this keeps the local cache consistent so switching back
  // shows the turn immediately without a Memory refetch.
  const appendMessageToSessionCache = useCallback((sid: string, msg: ChatMessage): ChatMessage[] | null => {
    try {
      const stored = localStorage.getItem(`lab-session-${sid}`);
      const arr: ChatMessage[] = stored ? JSON.parse(stored) : [];
      const next = [...arr, msg];
      const capped = next.length > MAX_MESSAGES ? next.slice(next.length - MAX_MESSAGES) : next;
      setSessionCache(sid, JSON.stringify(capped));
      window.dispatchEvent(new CustomEvent('session-messages-updated'));
      window.dispatchEvent(new CustomEvent('session-cache-external-update', { detail: { sessionId: sid } }));
      return capped;
    } catch { /* quota exceeded — drop */ return null; }
  }, []);

  const processMessage = useCallback(async (message: string, attachments?: readonly ReadyAttachment[] | null, displayText?: string, opts?: { findSimilar?: FindSimilarPayload }) => {
    // A card's Find_Similar reference (Req 3.23), forwarded verbatim as the turn body's `find_similar_ref`.
    // The proxy turn gate resolves it; the client only names the asset. Present only for a Find_Similar turn.
    const findSimilarRef = opts?.findSimilar;
    // Session this turn belongs to, captured at send time. If the user
    // switches sessions mid-stream, `currentSessionRef.current` diverges from
    // this and we must NOT write the result into the on-screen session.
    const capturedSessionId = sessionId;
    // Per-turn Project values, frozen when the turn starts and held for the life of this request, so a
    // change made mid-turn (Plus_Menu or Projects_Surface) applies from the NEXT turn (Req 3.8, 3.9).
    // Null when Projects are off — the body then carries no project fields at all.
    const turnProject = projectsOnRef.current ? snapshotSessionProject(capturedSessionId) : null;
    inFlightSessionsRef.current.add(capturedSessionId);
    onSessionInputStarted?.(capturedSessionId);

    // Perceived-speed: surface the user's bubble and the "Working…" indicator
    // IMMEDIATELY — before the session-create round-trip — so the app feels instant.
    // Attachments finished uploading before the turn (the composer holds Send until they do).
    const turnAttachments = attachments ?? [];
    // `displayText` lets a caller show a CLEAN bubble while sending the agent the full
    // prompt — used for machine-authored turns (timeline actions, notification summaries)
    // so job ARNs, account ids, CloudFront paths and "do NOT re-fetch" plumbing never
    // surface as chat dirty laundry. Falls back to the message when not provided.
    // Stable per-turn id — the SINGLE correlation key between the client's on-screen/cached copy of
    // a turn and the copy the proxy persists durably. Client message ids and the proxy's persisted
    // ids MUST derive from it (`<turnId>-u` / `<turnId>-a`) so mergeSessionMessages dedups them by id
    // on reload; otherwise the localStorage instant-paint copy (client id) and the durable copy
    // (proxy id) are different ids for the same turn and BOTH render (duplicate turns on refresh).
    const turnId = `t-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const userBubbleContent = turnAttachments.length > 0
      ? attachmentBubbleText(turnAttachments, message)
      : (displayText ?? message);
    const userMsgId = addUserMessage(userBubbleContent, `${turnId}-u`);
    startStreaming(capturedSessionId);
    // The durable URL of each attachment: previewed inline in the user bubble, and sent with the turn so the
    // proxy persists the same preview. `attachment` (the first) is kept for readers that know only one.
    const attachmentRefs = turnAttachments.filter(a => a.url).map(a => ({ url: a.url, name: a.name }));
    if (attachmentRefs.length > 0) {
      setMessages(prev => prev.map(m =>
        m.id === userMsgId ? { ...m, attachment: attachmentRefs[0], attachments: attachmentRefs } : m,
      ));
    }

    // Authoritative committed transcript for capturedSessionId, snapshotted at send time.
    // addUserMessage (above) queued the user turn via setMessages; this updater runs AFTER it in
    // the same queue, so `prev` here includes the user turn + all prior committed turns. Off-screen
    // durable writes MUST use this, never the localStorage cache: when the user switches away
    // before the messages->cache effect has flushed, that cache is empty, and basing a durable
    // write on it truncated a whole transcript down to a lone assistant provisional — the user
    // turn and all prior turns vanished (session e865ed97). Empty base ⇒ off-screen writes are
    // skipped rather than risk overwriting a good server transcript.
    let capturedBase: ChatMessage[] = [];
    setMessages(prev => { capturedBase = prev; return prev; });

    let persistedForTurn = sessionCreatedRef.current;
    if (!persistedForTurn) {
      // The CLEAN text names the chat: the agent prompt can carry a source url (a timeline action handed to the
      // agent was titled "Clip the video. [source: https://mediawo…").
      const initialTitle = deriveInitialSessionTitle(displayText ?? message, turnAttachments[0]?.name, sessionTitle);
      persistedForTurn = await ensureSessionPersisted(capturedSessionId, initialTitle);
    }

    // Every attachment's S3 refs and permanent HTTPS URL go to the model ahead of the message: the real bucket
    // and key as separate, unambiguous fields (so the agent never mistakes the `artifacts/` prefix for a bucket),
    // and the CloudFront URL that external generators (fal.ai image_url, etc.) can fetch where s3:// cannot.
    // Timeline action lines since the last reply ride along, hidden (the bubble shows `display_text`), so the agent
    // knows about the frames / clips / crops the user made directly (projects/mediaActions.ts).
    // From the committed messages, NOT `capturedBase`: that snapshot is taken in a state updater React runs lazily,
    // so on a chat that already exists (no await above) it was still empty here and the summary never went out.
    const prompt = withActionContext(committedMessagesRef.current, buildAttachmentPrompt(turnAttachments, message));
    // An uploaded video renders its editing timeline in the panel right away (deterministic), so the source is
    // ready to mark/clip regardless of the accompanying request. WorkspaceView composes the NLE.
    for (const a of turnAttachments) {
      if (a.url && attachmentKind(a) === 'video') {
        window.dispatchEvent(new CustomEvent('lab:render-media', {
          detail: { url: a.url, name: a.name, sessionId: capturedSessionId },
        }));
      }
    }
    // Attached images open in the panel too (one tab for the batch), so a picture picked from the project or
    // uploaded is on screen to work with, the same as a video. The bubble keeps its small preview.
    const imageAttachments = turnAttachments.filter((a) => a.url && attachmentKind(a) === 'image');
    if (imageAttachments.length > 0) {
      window.dispatchEvent(new CustomEvent('lab:render-urls', {
        detail: {
          urls: imageAttachments.map((a) => a.url),
          label: imageAttachments.length === 1 ? imageAttachments[0].name : 'Images',
          sessionId: capturedSessionId,
        },
      }));
    }

    let receivedSummary: ExecutionSummary | null = null;
    let accumulatedText = '';
    // Accumulate steps in a local array (mirrors accumulatedText) so the
    // finalized message is built from a source that is guaranteed complete at
    // await-resolution time. Reading liveStepsRef.current here was racy: it is
    // only synced during React render, so the last steps (or all of them, when
    // the turn produced no text to force interim renders) could be missing —
    // the live panel showed them mid-stream but the persisted message dropped
    // them. Dedup by node_id when present (DAG steps), else append.
    const accumulatedSteps: StepEvent[] = [];
    const recordStep = (step: StepEvent) => {
      if (step.node_id) {
        const i = accumulatedSteps.findIndex(s => s.node_id === step.node_id);
        if (i >= 0) { accumulatedSteps[i] = step; return; }
      }
      accumulatedSteps.push(step);
    };

    // Turn-scoped single-flight: a given manifest key renders at most once per
    // turn. Guards against the same artifact being emitted by more than one
    // path (e.g. the render hook plus another relay) producing duplicate
    // render blocks. A genuinely modified artifact has a NEW key (versioned
    // outputs), so legitimate re-renders still pass through.
    const seenManifestKeys = new Set<string>();
    // Once a manifest has rendered this turn, a LATER render-agent step event
    // must NOT flip the renderer back to the "Rendering…" animation (the
    // reported double-render / flicker). Turn-scoped so a fresh turn re-arms.
    let renderArrived = false;

    // --- Off-screen finalize: cache-only ------------------------------------------------------
    // Durable transcript persistence is now owned by the PROXY (it accumulates the SSE stream and
    // upserts the turn to S3 + the DynamoDB row at completion, and throttled during the stream for
    // mid-turn-reload recovery — see docs/design-proxy-authoritative-transcript.md). The client no
    // longer writes the durable transcript at all. This helper only keeps the localStorage CACHE
    // current for a backgrounded session so its on-screen tab live-refreshes instantly on return
    // (the proxy's write is the authoritative record either way). No base ⇒ append-only cache write.
    const persistOffscreenFinal = (msg: ChatMessage) => {
      if (capturedBase.length > 0) {
        const arr = [...capturedBase, msg];
        const cappedFinal = arr.length > MAX_MESSAGES ? arr.slice(arr.length - MAX_MESSAGES) : arr;
        try {
          setSessionCache(capturedSessionId, JSON.stringify(cappedFinal));
          window.dispatchEvent(new CustomEvent('session-messages-updated'));
          window.dispatchEvent(new CustomEvent('session-cache-external-update', { detail: { sessionId: capturedSessionId } }));
        } catch { /* quota — drop cache copy; the proxy's durable write is authoritative */ }
      } else {
        appendMessageToSessionCache(capturedSessionId, msg);
      }
    };

    try {
      const callback: StreamCallback = (event) => {
        if (event.type === 'execution_summary') {
          receivedSummary = event.summary;
          // Publish the model the agent ACTUALLY used. The header badge used to
          // fall back to the first entry of the model list when nothing was
          // explicitly selected, so it advertised a model nothing was running.
          if (event.summary?.model_id) {
            try { localStorage.setItem('lab-orchestrator-model-active', event.summary.model_id); } catch { /* ignore */ }
            window.dispatchEvent(new CustomEvent('orchestrator:model', { detail: { modelId: event.summary.model_id } }));
          }
        } else if (event.type === 'text') {
          accumulatedText += event.text;
          handleStreamEvent(event as StreamEvent, capturedSessionId);
        } else if (event.type === 'render') {
          // Manifest arrived — stop the "Rendering…" animation and show it,
          // unless this key already rendered this turn (dedup, see T5).
          onRenderPending?.(false, capturedSessionId);
          if (!shouldRenderManifest(seenManifestKeys, event.manifestKey)) return;
          renderArrived = true;
          // The hook names the view after the work that produced it (e.g.
          // "Shot Segmentation"); WorkspaceView numbers it when absent.
          onRenderBlock?.({
            turnIndex: 0,
            timestamp: Date.now(),
            label: event.label,
            renderRequestId: event.id,
            status: 'ready',
            blocks: [{
              template: 'manifest-router',
              title: event.label || 'Render Output',
              data: { manifestUrl: `/api/artifact?key=${encodeURIComponent(event.manifestKey)}` },
            }],
          }, capturedSessionId);
        } else if (event.type === 'render_pending') {
          // Leading "Composing…" marker → open a per-tab placeholder NOW,
          // correlated to the manifest by id so it fills in place on the render
          // event. This is the per-tab replacement for the window-level animation.
          onRenderComposing?.(event.id, event.label, capturedSessionId);
        } else if (event.type === 'render_failed') {
          // Terminal: the composing render produced no manifest → drop the
          // placeholder so the tab doesn't spin forever.
          onRenderFailed?.(event.id, capturedSessionId);
        } else {
          // Render-agent activity in the step stream → show the composing
          // animation until the manifest arrives. Deterministic via the
          // agent==='render' marker (see isRenderPendingStep / T6). Suppressed
          // once a manifest has already rendered this turn so a trailing
          // render-agent step can't flip the panel back to the loading
          // animation and cause the render to flash twice.
          if (event.type === 'step' && shouldEnterRenderPending(isRenderPendingStep(event.step), renderArrived)) {
            onRenderPending?.(true, capturedSessionId);
          }
          if (event.type === 'step') recordStep(event.step);
          handleStreamEvent(event as StreamEvent, capturedSessionId);
        }
      };

      const controller = new AbortController();
      abortControllersRef.current.set(capturedSessionId, controller);
      const reasoningEffort = selectedReasoningEffort();
      await invokeAgentStreaming(
        { prompt, session_id: sessionId, model_id: selectedOrchestratorModelId(),
          ...(reasoningEffort ? { effort: reasoningEffort } : {}),
          display_text: userBubbleContent, turn_id: turnId,
          ...(attachmentRefs.length > 0 ? { attachment: attachmentRefs[0], attachments: attachmentRefs } : {}),
          ...(turnProject?.activeProjectId ? { active_project_id: turnProject.activeProjectId } : {}),
          ...(turnProject ? { web_search: turnProject.webSearch } : {}),
          ...(findSimilarRef ? { find_similar_ref: findSimilarRef } : {}) },
        callback, controller.signal);
      abortControllersRef.current.delete(capturedSessionId);
      // Turn finished — clear any lingering pending state (covers the case
      // where the agent mentioned rendering but no manifest was produced).
      onRenderPending?.(false, capturedSessionId);

      // Stream completed — finalize with the accumulated text.
      // Route to the session that started the turn: if the user switched
      // away, write to that session's cache instead of the on-screen state.
      const assistantMsg: ChatMessage = {
        id: `${turnId}-a`,
        role: 'assistant',
        content: accumulatedText,
        steps: accumulatedSteps.length > 0 ? [...accumulatedSteps] : undefined,
        summary: receivedSummary ?? undefined,
        timestamp: Date.now(),
      };
      if (currentSessionRef.current === capturedSessionId) {
        // Commit the completed turn to on-screen state from the AUTHORITATIVE post-append array
        // (computed from `prev` inside the updater). The proxy owns durable persistence.
        setMessages(prev => {
          const next = commitTurnReply(prev, assistantMsg, `${turnId}-u`);
          return next.length > MAX_MESSAGES ? next.slice(next.length - MAX_MESSAGES) : next;
        });
      } else {
        // User navigated away mid-stream — the proxy persists the turn to the originating session
        // durably; here we only refresh that session's localStorage cache (see persistOffscreenFinal).
        persistOffscreenFinal(assistantMsg);
      }

      // Conversation persistence is owned exclusively by the agent runtime's
      // AgentCoreMemorySessionManager (Strands SDK). It writes each turn under
      // (actor_id, session_id) using a JSON-serialized SessionMessage envelope
      // in the conversational `content.text` field.
      //
      // The UI used to ALSO write here via writeMemoryEvent, but it sent raw
      // text (`content.text = "<message>"`). The SDK reader's `events_to_messages`
      // calls `json.loads(content.text)` with no try/except on the conversational
      // branch, so it threw JSONDecodeError on every UI-written event; that
      // exception was swallowed by `list_messages` which returned `[]`. Result:
      // the agent saw no prior turns ("this is the start of our conversation")
      // even though the UI's own reader could still display history.
      //
      // Removed to make the runtime the sole writer. The UI's
      // `memoryEventsToChatMessages` now parses the SDK envelope when reading
      // /memory/events back, so the sidebar/restart history still works.
      // Session metadata is created lazily at the start of the first valid
      // input. Once the turn completes, bump activity only when that POST
      // succeeded (or confirmed that an existing row already exists).
      if (persistedForTurn) {
        updateSessionActivity(capturedSessionId).catch(() => {});
      }
      // The agent's switch_project / create_project: open a NEW chat in that project and send it the prompt.
      // Only from this live stream (a reloaded transcript never comes through here), and only while the user is
      // still on the chat that asked, so a turn finishing in the background never pulls them somewhere else.
      const openProject = parseOpenProject((receivedSummary as ExecutionSummary | null)?.open_project);
      if (openProject && currentSessionRef.current === capturedSessionId) {
        void refreshProjectsList(); // a project create_project just made is listed at once
        requestNewChatInProject(openProject.projectId, openProject.projectName, openProject.prompt);
      }
    } catch (err) {
      onRenderPending?.(false, capturedSessionId);
      abortControllersRef.current.delete(capturedSessionId);
      // `409 project_access_lost` (Req 3.11): the turn gate started NO turn and already moved the session row to
      // Personal. Switch the store (no second write), show the notice, and put the unsent message back in the
      // composer — no assistant bubble, and the user bubble is withdrawn since nothing was sent.
      const httpErr = err as { status?: unknown; body?: unknown } | null;
      const lostTo = typeof httpErr?.status === 'number' ? parseProjectAccessLost(httpErr.status, httpErr.body) : null;
      if (lostTo) {
        void resetToPersonal(capturedSessionId, {
          personalProject: lostTo,
          lostProjectName: turnProject?.activeProjectName ?? null,
          alreadyPersisted: true,
        });
        if (currentSessionRef.current === capturedSessionId) {
          setMessages(prev => prev.filter(m => m.id !== userMsgId));
          // Only a turn the user typed goes back in the box; a machine-authored turn (it has a clean
          // `displayText`) is not composer text.
          if (displayText === undefined) {
            setInput(prev => (prev ? `${message}\n${prev}` : message));
            if (turnAttachments.length > 0) restoreReady(capturedSessionId, turnAttachments);
          }
        }
        return;
      }
      // `409 session_project_gone`: this chat's own project was deleted (or the user was removed from it). A chat is
      // fixed to its project, so it cannot continue: open a NEW chat in Personal with the message in its composer
      // (not sent: it was written for the old project) and a line saying why. The old chat stays as history.
      const goneTo = typeof httpErr?.status === 'number' ? parseSessionProjectGone(httpErr.status, httpErr.body) : null;
      if (goneTo) {
        if (currentSessionRef.current === capturedSessionId) {
          setMessages(prev => prev.filter(m => m.id !== userMsgId));
          requestNewChatInProject(goneTo.id, goneTo.name, undefined, {
            ...(displayText === undefined ? { draft: message } : {}),
            notice: projectGoneNewChatMessage(turnProject?.activeProjectName),
          });
        }
        return;
      }
      // `409 session_project_fixed` (asset-lineage R2.1): this session already belongs to another project, so the
      // gate started NO turn. Mark the session fixed (the Plus menu then offers "Start a new chat in <project>"),
      // put the message back in the composer, and say why instead of a generic failure bubble.
      const fixedTo = typeof httpErr?.status === 'number' ? parseSessionProjectFixed(httpErr.status, httpErr.body) : null;
      if (fixedTo !== null) {
        markSessionProjectFixed(capturedSessionId);
        if (currentSessionRef.current === capturedSessionId) {
          setMessages(prev => [
            ...prev.filter(m => m.id !== userMsgId),
            {
              id: `${turnId}-a`,
              role: 'assistant',
              content: '',
              error: SESSION_PROJECT_FIXED_NOTICE,
              timestamp: Date.now(),
            },
          ]);
          if (displayText === undefined) {
            setInput(prev => (prev ? `${message}\n${prev}` : message));
            if (turnAttachments.length > 0) restoreReady(capturedSessionId, turnAttachments);
          }
        }
        return;
      }
      const aborted = err instanceof DOMException && err.name === 'AbortError';
      const assistantMsg: ChatMessage = {
        id: `${turnId}-a`,
        role: 'assistant',
        content: accumulatedText || (aborted ? 'Stopped.' : ''),
        steps: accumulatedSteps.length > 0 ? [...accumulatedSteps] : undefined,
        error: aborted ? undefined : (err instanceof Error ? err.message : 'Connection error'),
        timestamp: Date.now(),
      };
      if (currentSessionRef.current === capturedSessionId) {
        // Same as the success path: commit to on-screen state; the proxy owns durable persistence.
        setMessages(prev => {
          const next = commitTurnReply(prev, assistantMsg, `${turnId}-u`);
          return next.length > MAX_MESSAGES ? next.slice(next.length - MAX_MESSAGES) : next;
        });
      } else {
        persistOffscreenFinal(assistantMsg);
      }
    } finally {
      // This turn is over on EVERY path (completed, errored, aborted, or its
      // session navigated away from), so retire the session's live slice here
      // alongside the in-flight entry. Doing it inside the "still on screen"
      // branch above left a session that finished while you were elsewhere
      // permanently marked as streaming, which blocked its own queue from
      // draining when you returned to it.
      clearLiveState(capturedSessionId);
      inFlightSessionsRef.current.delete(capturedSessionId);
    }
  }, [sessionId, sessionTitle, onSessionInputStarted, ensureSessionPersisted, addUserMessage, startStreaming, handleStreamEvent, appendMessageToSessionCache, onRenderPending, onRenderComposing, onRenderFailed, onRenderBlock, clearLiveState]);

  // Process queued messages after a stream completes. Scoped to the CURRENT
  // session: only that session's queue drains, and only when THAT session is
  // idle. A message queued in a still-busy session stays put until its own
  // session is both on screen and free — it can never run elsewhere. The
  // sessionId dep also drains a queue when the user returns to the session the
  // message was typed in (e.g. after switching away while it was streaming).
  useEffect(() => {
    const queue = messageQueues[sessionId];
    if (!isStreaming && !inFlightSessionsRef.current.has(sessionId) && queue && queue.length > 0) {
      const [nextMessage, ...rest] = queue;
      setMessageQueues(prev => ({ ...prev, [sessionId]: rest }));
      processMessage(nextMessage.prompt, null, nextMessage.displayText);
    }
  }, [isStreaming, messageQueues, processMessage, sessionId]);

  // Implicit auto-render (job completed while viewing this session): run it as
  // a LIVE turn — same path as a typed message — so the user immediately sees
  // the request, the "Working…" animation, live steps, and the renderer drawer
  // composing before the final render. Queued if a turn is already streaming.
  useEffect(() => {
    const handler = (e: Event) => {
      const ce = e as CustomEvent<{ prompt?: string; sessionId?: string; displayText?: string }>;
      const p = ce.detail?.prompt;
      if (!p) return;
      // Only handle an auto-render aimed at the session we're actually showing.
      // A background job completion (or a session-switch race between the
      // dispatch-time active-session check and this handler) could otherwise
      // run the results turn in a DIFFERENT/new session — popping a stale
      // renderer and queuing the user's just-typed message behind a turn they
      // never started (the reported stuck-session). When the target doesn't
      // match, ignore it: the durable bell notification still lets the user
      // open that session and render on demand.
      const targetSid = ce.detail?.sessionId;
      if (targetSid && targetSid !== sessionId) return;
      // Frozen session: a job-completion results/summary turn is agent WORK — do not run it on a
      // locked session (the server 423s the turn too). Results stay reachable once unlocked.
      if (locked) return;
      window.dispatchEvent(new CustomEvent('chat:auto-render-ack'));
      if (isStreaming || inFlightSessionsRef.current.has(sessionId)) {
        setMessageQueues(prev => ({ ...prev, [sessionId]: enqueueMessage(prev[sessionId] ?? [], { prompt: p, displayText: ce.detail?.displayText }) }));
      } else {
        processMessage(p, null, ce.detail?.displayText);
      }
    };
    window.addEventListener('chat:auto-render', handler as EventListener);
    return () => window.removeEventListener('chat:auto-render', handler as EventListener);
  }, [isStreaming, processMessage, sessionId, locked]);

  // Explicit "send this prompt as a turn" from external surfaces (the Jobs
  // panel's Results button). Unlike `chat-input:set` (which only PRE-FILLS the
  // box), this PUSHES the message to the agent on the same live path as a typed
  // message, so results stream back without the user re-typing/pressing Enter.
  // Session-scoped like auto-render so a background job never runs in the wrong
  // session; queued if a turn is already in flight.
  useEffect(() => {
    const handler = (e: Event) => {
      const ce = e as CustomEvent<{ prompt?: string; sessionId?: string; displayText?: string; findSimilar?: FindSimilarPayload }>;
      const p = ce.detail?.prompt;
      if (!p) return;
      const targetSid = ce.detail?.sessionId;
      if (targetSid && targetSid !== sessionId) return;
      if (locked) return;  // frozen session: block the Jobs-panel Results fetch turn (server 423s it too)
      const findSimilar = ce.detail?.findSimilar;
      if (findSimilar) {
        // A Find_Similar send carries per-turn context (`find_similar_ref`) that cannot survive queuing:
        // its reference is resolved against the Active_Project at send time. The card disables the action
        // while a turn is in progress, so this only fires through a race — drop it rather than run the
        // search without its reference (design Component 7, "Starting the turn").
        if (isStreaming || inFlightSessionsRef.current.has(sessionId)) {
          console.warn('[find-similar] dropped: a turn is already in progress for this session');
          return;
        }
        processMessage(p, null, ce.detail?.displayText, { findSimilar });
        return;
      }
      if (isStreaming || inFlightSessionsRef.current.has(sessionId)) {
        setMessageQueues(prev => ({ ...prev, [sessionId]: enqueueMessage(prev[sessionId] ?? [], { prompt: p, displayText: ce.detail?.displayText }) }));
      } else {
        processMessage(p, null, ce.detail?.displayText);
      }
    };
    window.addEventListener('chat:send', handler as EventListener);
    return () => window.removeEventListener('chat:send', handler as EventListener);
  }, [isStreaming, processMessage, sessionId, locked]);

  // A chat opened by the agent's switch_project / create_project sends the carried-over request as its first message;
  // one opened because the old chat's project was deleted gets the unsent message back in its composer.
  useEffect(() => {
    if (!sessionId || locked || isStreaming) return;
    const open = takePendingOpen(sessionId);
    if (open?.prompt) processMessage(open.prompt, null);
    else if (open?.draft) setInput(open.draft);
  }, [sessionId, locked, isStreaming, processMessage]);

  // Action lines (direct timeline actions): upserted by id into the on-screen chat, never a turn. The proxy
  // records the finished line in the transcript, so another session's line arrives with its transcript.
  useEffect(() => {
    const onAction = (e: Event) => {
      const d = (e as CustomEvent<ChatActionDetail>).detail;
      if (!d?.message || d.sessionId !== sessionId) return;
      setMessages(prev => {
        const rest = prev.filter(m => m.id !== d.message.id);
        if (d.remove) return rest.length === prev.length ? prev : rest;
        const at = prev.findIndex(m => m.id === d.message.id);
        if (at >= 0) { const next = [...prev]; next[at] = d.message; return next; }
        const next = [...prev, d.message];
        return next.length > MAX_MESSAGES ? next.slice(next.length - MAX_MESSAGES) : next;
      });
    };
    window.addEventListener(CHAT_ACTION_EVENT, onAction);
    return () => window.removeEventListener(CHAT_ACTION_EVENT, onAction);
  }, [sessionId]);

  const handleSubmit = useCallback(() => {
    if (locked) return;  // full-freeze: a locked session cannot send turns
    if (uploadsBusy) return;  // Send waits for every chosen file to finish uploading
    const hasText = isValidChatInput(input);
    if (!hasText && readyAttachments.length === 0) return;

    const trimmedInput = input;

    if (isStreaming || inFlightSessionsRef.current.has(sessionId)) {
      // Queue the message if THIS session is currently busy. Queued messages carry no attachments, so the chips
      // stay on the composer for the next send. A brand-new session is not considered busy just because another
      // session's turn is still running, so its first message dispatches immediately instead of queuing.
      if (!hasText) return;
      setInput('');
      setMessageQueues(prev => ({ ...prev, [sessionId]: enqueueMessage(prev[sessionId] ?? [], { prompt: trimmedInput }) }));
    } else {
      setInput('');
      processMessage(trimmedInput, takeReady(sessionId));
    }
  }, [input, readyAttachments.length, uploadsBusy, isStreaming, processMessage, sessionId, locked]);

  // Send an externally requested turn exactly once per id. Keyed on the id rather
  // than the text so the same request for a different job still fires, and the same
  // job re-opened does not ask twice.
  const sentPromptIdsRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    if (!pendingPrompt?.id || !pendingPrompt.text.trim()) return;
    if (sentPromptIdsRef.current.has(pendingPrompt.id)) return;
    if (locked) return;  // don't auto-send into a frozen session (fires once unlocked)
    sentPromptIdsRef.current.add(pendingPrompt.id);
    if (isStreaming || inFlightSessionsRef.current.has(sessionId)) {
      setMessageQueues((prev) => ({ ...prev, [sessionId]: enqueueMessage(prev[sessionId] ?? [], { prompt: pendingPrompt.text }) }));
    } else {
      processMessage(pendingPrompt.text);
    }
  }, [pendingPrompt, isStreaming, processMessage, sessionId, locked]);

  // Stop aborts ONLY the on-screen session's stream. With a single shared
  // controller ref, a second concurrent stream overwrote the first, so this
  // aborted the WRONG session's stream and left the other one unstoppable.
  const handleStop = useCallback(() => {
    const sid = currentSessionRef.current;
    const controller = abortControllersRef.current.get(sid);
    if (controller) {
      controller.abort();
      abortControllersRef.current.delete(sid);
    }
  }, []);

  const handleKeyDown = useCallback((e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  }, [handleSubmit]);

  // Req 3.7: the Active_Project's name and the web-search value, visible without opening the menu and in the
  // textarea's accessible description. Shown once the name is known (the row carries only the id).
  const showProjectStatus = projectsOn && !!sessionProject?.activeProjectName;
  const activeProjectIndexing = useProjectIndexing(sessionProject?.activeProjectId, projectsOn);
  // What an answer's `![caption](asset:<id>)` image resolves against (InlineAssetImage).
  const chatAssetContext = useMemo(
    () => ({ sessionId, projectId: projectsOn ? sessionProject?.activeProjectId ?? null : null }),
    [sessionId, projectsOn, sessionProject?.activeProjectId],
  );
  // The Plus_Menu's Recent and Project sections, once the Active_Project is labelled.
  const plusMenuProjects: PlusMenuProjects | null = showProjectStatus && sessionProject?.activeProjectId
    ? {
        activeProjectId: sessionProject.activeProjectId,
        activeProjectName: sessionProject.activeProjectName ?? '',
        loadProjects,
        onSelectProject: handleSelectProject,
        onActiveProjectNotFound: handleActiveProjectNotFound,
        onManageProjects: handleManageProjects,
        loadRecentAssets: listAssets,
        onAttachRecentAsset: handleAttachRecentAsset,
        searchAssets: (pid: string, q: string) => searchAssets(pid, { q }),
        // D1: a session with a turn keeps its project; the Plus_Menu offers a new chat in another one.
        projectFixed: sessionProject.projectFixed === true || messagesToRender.some(m => m.role === 'user'),
      }
    : null;
  // Send needs text or a finished attachment, and waits while any chosen file is still uploading.
  const canSend = !locked && !uploadsBusy && (isValidChatInput(input) || readyAttachments.length > 0);

  return (
    <ChatAssetContext.Provider value={chatAssetContext}>
    <div style={canvasContainerStyle}>
      {/* Constrain markdown code/JSON so long, unbreakable lines (e.g. a job's
          execution-ARN result) scroll INSIDE the assistant bubble instead of
          overflowing it. `pre` scrolls horizontally; inline code wraps. */}
      <style>{`
        .lab-md { min-width: 0; max-width: 100%; }
        .lab-md pre { max-width: 100%; overflow-x: auto; white-space: pre; }
        .lab-md pre code { white-space: pre; word-break: normal; }
        .lab-md code { word-break: break-word; overflow-wrap: anywhere; }
        .lab-md table { display: block; max-width: 100%; overflow-x: auto; }
        .lab-md img { max-width: 100%; height: auto; }
        @keyframes mh-spin { to { transform: rotate(360deg); } }
      `}</style>
      {/* Message list */}
      <div
        ref={scrollContainerRef}
        style={messageListStyle}
      >
        {/* Rendered messages (timeline actions made during the running turn wait below its live reply) */}
        {(isStreaming ? splitLateActions(messagesToRender).before : messagesToRender).map(renderMessage)}

        {/* Live streaming message */}
        {isStreaming && (
          <div style={assistantMessageRowStyle}>
            <div style={assistantBubbleStyle}>
              {/* Step progress indicators */}
              {liveSteps.length > 0 && (
                <StepsIndicator steps={liveSteps} />
              )}
              {/* Streaming text */}
              {liveText && (
                <div className="lab-md markdown-body" style={markdownBodyStyle}>
                  <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents} urlTransform={chatUrlTransform}>
                    {liveText}
                  </ReactMarkdown>
                </div>
              )}
              {/* Streaming indicator — a persistent animated "Working…" row
                  visible for the ENTIRE turn (not only before the first step),
                  so it stays obvious the agent is active while steps/text
                  stream in. */}
              <span style={workingIndicatorStyle}>
                <span style={streamingDotsContainerStyle}>
                  <span style={{ ...dotStyle, animationDelay: '0s' }}>●</span>
                  <span style={{ ...dotStyle, animationDelay: '0.2s' }}>●</span>
                  <span style={{ ...dotStyle, animationDelay: '0.4s' }}>●</span>
                </span>
                <span style={workingLabelStyle}>Working…</span>
                <style>{dotKeyframes}</style>
              </span>
            </div>
          </div>
        )}
        {isStreaming && splitLateActions(messagesToRender).late.map(renderMessage)}

        {/* Empty state — personalized welcome */}
        {(!showingActiveSession || messages.length === 0) && !isStreaming && (
          <div style={emptyStateStyle}>
            <span style={welcomeHeadingStyle}>{welcomeHeading}</span>
            <span style={emptyTextStyle}>Ask me to process media, generate workflows, or explore capabilities.</span>
          </div>
        )}
      </div>

      {/* Chat input area. A file dropped anywhere on the composer attaches like `Attach files`. */}
      <div style={chatInputContainerStyle} onDragOver={handleComposerDragOver} onDrop={handleComposerDrop}>
        {/* The upload sheet: every chosen file that is not an attachment chip yet (in progress, failed, or
            cancelled), and the `Add to <Active_Project>` switch. Finished files leave it as chips. */}
        {chatAttachments.items.some(it => it.status !== 'ready') && (
          <UploadSheetMount
            sessionId={sessionId}
            projectId={uploadProject?.id ?? null}
            projectName={uploadProject?.name ?? null}
            addToProject={chatAttachments.addToProject}
            locked={locked}
            onAddToProjectChange={handleAddToProjectChange}
            onClose={() => dismissFinishedProblems(sessionId)}
            onViewInProjects={handleManageProjects}
          />
        )}
        {sessionProject?.notice && (
          <div style={projectNoticeStyle} role="alert">
            <span>{sessionProject.notice}</span>
            <button onClick={() => dismissNotice(sessionId)} style={removeFileBtnStyle} aria-label="Dismiss notice"><CloseIcon size={14} /></button>
          </div>
        )}
        {currentQueue.length > 0 && (
          <div style={queueIndicatorStyle}>
            {currentQueue.length} message{currentQueue.length !== 1 ? 's' : ''} queued
          </div>
        )}
        {readyAttachments.length > 0 && (
          <div style={attachedChipsRowStyle} data-testid="attached-file-chips">
            {readyAttachments.map(it => (
              <div key={it.id} style={attachedFileChipStyle} data-testid="attached-file-chip">
                <span style={attachedFileIconStyle}><PaperclipIcon size={14} /></span>
                <span style={{ fontSize: 'var(--fs-xs)' }}>{it.name} ({formatAttachmentSize(it.size)})</span>
                <button
                  onClick={() => removeAttachment(sessionId, it.id)}
                  style={removeFileBtnStyle}
                  aria-label={`Remove attached file ${it.name}`}
                >
                  <CloseIcon size={14} />
                </button>
              </div>
            ))}
          </div>
        )}
        {uploadError && (
          <div style={uploadErrorStyle} role="alert">
            <span>{uploadError}</span>
            <button onClick={() => setUploadError(null)} style={removeFileBtnStyle} aria-label="Dismiss upload error"><CloseIcon size={14} /></button>
          </div>
        )}
        <div style={chatInputRowStyle}>
          {/* The chat file picker (1 to 20 files). Its visible `Upload file` button is gone (Req 3.18); the
              Plus_Menu's `Attach files` clicks it. */}
          <input
            ref={fileInputRef}
            type="file"
            multiple
            onChange={handleFileSelect}
            disabled={locked}
            style={hiddenFileInputStyle}
            tabIndex={-1}
            aria-hidden="true"
          />
          <PlusMenu
            sessionId={sessionId}
            locked={locked}
            onAttachToChat={handleAttachToChat}
            projects={plusMenuProjects}
          />
          <DuplicatesDialog
            duplicates={pendingDup?.duplicates ?? null}
            batchSize={pendingDup?.files.length ?? 0}
            onUseExisting={() => resolveDuplicates('existing')}
            onKeepBoth={() => resolveDuplicates('both')}
            onCancel={() => resolveDuplicates('cancel')}
          />
          <textarea
            ref={setChatTextareaRef}
            style={locked ? { ...chatTextareaStyle, opacity: 0.6, cursor: 'not-allowed' } : chatTextareaStyle}
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            onPaste={handleTextareaPaste}
            placeholder={locked ? 'Session locked — unlock it in the sidebar to continue' : 'Type a message...'}
            maxLength={MAX_INPUT_LENGTH}
            rows={1}
            disabled={locked}
            aria-label="Chat message input"
            aria-describedby={showProjectStatus ? composerStatusId : undefined}
          />
          {isStreaming ? (
            <button
              style={{ ...chatStopButtonStyle }}
              onClick={handleStop}
              aria-label="Stop agent"
            >
              <svg width="14" height="14" viewBox="0 0 14 14" fill="currentColor" style={{ pointerEvents: 'none' }}>
                <rect x="2" y="2" width="10" height="10" rx="2" />
              </svg>
              Stop
            </button>
          ) : (
            <button
              style={{
                ...chatSubmitButtonStyle,
                opacity: canSend ? 1 : 0.5,
                cursor: canSend ? 'pointer' : 'not-allowed',
              }}
              onClick={handleSubmit}
              disabled={!canSend}
              title={uploadsBusy ? 'Wait for the files to finish uploading' : undefined}
              aria-label="Send message"
            >
              Send
            </button>
          )}
        </div>
        {showProjectStatus && (
          <div id={composerStatusId} style={composerStatusStyle} data-testid="composer-project-status">
            Project: {sessionProject?.activeProjectName}
            {activeProjectIndexing && (
              <span style={{ display: 'inline-flex', verticalAlign: '-1px', marginLeft: 4 }}>
                <SearchIndexedGlyph indexing size={11} />
              </span>
            )}
            {' '}· Web search {sessionProject?.webSearch ? 'on' : 'off'}
          </div>
        )}
        {input.length > 9000 && (
          <div style={charCountStyle}>
            {input.length.toLocaleString()} / {MAX_INPUT_LENGTH.toLocaleString()}
          </div>
        )}
      </div>
    </div>
    </ChatAssetContext.Provider>
  );
}

// --- Sub-components ---

/**
 * Derive a `Job_Trio` identifier from a `start_*` MCP tool name.
 *
 * Mirrors the convention path in the Python `Job_Detector`
 * (`agent/src/jobs/detector.py`): the trio_id is the tool name with
 * the `start_` prefix stripped (e.g. `start_stem_separation` →
 * `stem_separation`). A future enhancement could call `GET /catalog`
 * to honor annotation-overridden trios; the MVP path skips that
 * lookup and trusts the convention. Returns `null` when the name
 * doesn't match the convention.
 *
 * Exported so component tests (task 13.8) and the
 * `extractJobReferences` scan below can share one derivation.
 */
export function deriveTrioIdFromStartToolName(toolName: string): string | null {
  const PREFIX = 'start_';
  if (typeof toolName !== 'string') return null;
  if (!toolName.startsWith(PREFIX) || toolName.length <= PREFIX.length) {
    return null;
  }
  return toolName.slice(PREFIX.length);
}

/**
 * Scan a chat message for inline `JobReference` candidates.
 *
 * The contract: returns one entry per tool-call response on the
 * message that carries a top-level non-empty string `job_id`, with
 * `trioId` derived from the calling tool's name via
 * `deriveTrioIdFromStartToolName`.
 *
 * Hook-point status (current data shape gap)
 * ------------------------------------------
 * The chat streaming protocol in `lab-api.ts` (`StreamEvent`) emits
 * only `step` / `text` / `execution_summary` / `draft_mutation`
 * frames; tool-call response payloads are never surfaced as
 * structured data — they get baked into the assistant's natural
 * language `content`. The `ChatMessage` model therefore has no
 * top-level field carrying a tool-call response object today, so
 * this scan has nothing structured to inspect and intentionally
 * returns `[]`.
 *
 * The hook is in place so that, the moment the chat protocol gains
 * a structured carrier for tool-call responses (e.g. a
 * `tool_response` stream event populating a `message.toolCalls`
 * array), this function picks them up and the surrounding render
 * site downstream emits chips automatically — no further plumbing
 * is required because the callback chain
 * (WorkspaceView → ChatCanvas → MessageBubble → JobReference
 * → SessionSidebar.scrollToJob) is already complete.
 *
 * TODO(async-job-tracking): wire the scan once the chat stream
 * surfaces tool-call responses in a structured way. The expected
 * shape is `{ name: string; response: unknown }[]` on the message;
 * the predicate is `typeof response.job_id === 'string' &&
 * response.job_id !== ''` AND
 * `deriveTrioIdFromStartToolName(name) !== null`. Per the spec's
 * MVP path note, no `/catalog` lookup is needed for trio resolution
 * on this branch.
 */
export function extractJobReferences(
  _message: ChatMessage,
): { jobId: string; trioId: string }[] {
  // No structured tool-call payload exists on the message today (see
  // doc comment above). Returning an empty array keeps the chat
  // surface visually unchanged while every callback wires through.
  return [];
}

function MessageBubble({
  message,
  sessionId,
  onActivateJob,
}: {
  message: ChatMessage;
  sessionId: string;
  onActivateJob?: (jobId: string) => void;
}) {
  if (message.role === 'user') {
    // Every attached file previews inline; a turn persisted before multi-attach carries only `attachment`.
    const inputs = message.attachments?.length ? message.attachments : message.attachment ? [message.attachment] : [];
    return (
      <div style={userMessageRowStyle} data-message-id={message.id}>
        <div style={userBubbleStyle}>
          <span style={userTextStyle}>{message.content}</span>
          {inputs.map((a, i) => (
            <div key={`${a.url}-${i}`} style={{ marginTop: 8 }}>
              <InputAttachment url={a.url} name={a.name} />
            </div>
          ))}
        </div>
      </div>
    );
  }

  // Inline JobReference chips for any tool-call responses on this
  // message that carry a `job_id`. Currently always empty (the chat
  // stream doesn't surface tool-call payloads structurally — see
  // `extractJobReferences` above); the wiring is plumbed end-to-end so
  // future protocol changes light up the chips with no further work.
  const jobRefs = extractJobReferences(message);

  return (
    <div style={assistantMessageRowStyle} data-message-id={message.id}>
      <div style={assistantBubbleStyle}>
        {/* Step progress indicators (collapsible) */}
        {message.steps && message.steps.length > 0 && (
          <StepsIndicator steps={message.steps} settled />
        )}
        {/* Markdown body */}
        {message.content && (
          <div className="lab-md markdown-body" style={markdownBodyStyle}>
            {message.summary?.citations ? (
              // Projects turn with a verified-citation block: classify bracketed Citations at render
              // time. remarkCitations runs AFTER remarkGfm, and CitationContext hands the block +
              // active session down to each <CitationRef>. Only persisted messages get this — the
              // live stream below never does (classification exists only at turn end).
              <CitationContext.Provider value={{ block: message.summary.citations, sessionId }}>
                <ReactMarkdown
                  remarkPlugins={[remarkGfm, remarkCitations]}
                  components={markdownComponents} urlTransform={chatUrlTransform}
                >
                  {message.content}
                </ReactMarkdown>
              </CitationContext.Provider>
            ) : (
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents} urlTransform={chatUrlTransform}>
                {message.content}
              </ReactMarkdown>
            )}
          </div>
        )}
        {/* Inline JobReference chips. Rendered before the error/
            summary footer so the chip flows visually with the
            assistant's prose. The chip itself returns `null` if its
            visibility predicate (job present, sessionId match) fails,
            so this row collapses cleanly when no chips render. */}
        {jobRefs.length > 0 && (
          <div style={jobReferencesRowStyle}>
            {jobRefs.map(ref => (
              <JobReference
                key={ref.jobId}
                jobId={ref.jobId}
                trioId={ref.trioId}
                messageSessionId={sessionId}
                onActivate={onActivateJob ? () => onActivateJob(ref.jobId) : undefined}
              />
            ))}
          </div>
        )}
        {/* Error display */}
        {message.error && (
          <div style={errorIndicatorStyle}>
            <WarningIcon /> {message.error}
          </div>
        )}
        {/* The platform's content guardrail blocked part of this turn: say so, so a replaced reply is not a mystery. */}
        {message.summary?.guardrail?.intervened && (
          <div style={guardrailNoteStyle} data-testid="guardrail-note">
            <WarningIcon /> Blocked by the platform's content guardrail{message.summary.guardrail.name ? ` (${message.summary.guardrail.name})` : ''}.
          </div>
        )}
        {/* Execution summary */}
        {message.summary && (
          <ExecutionSummaryDisplay summary={message.summary} />
        )}
      </div>
    </div>
  );
}

/**
 * A finished turn's steps: a tool's "Using x…" start event and its completion are separate steps, so the start one
 * stays `running` forever. Drop a start that has a finished twin and show any other leftover as `pending` — a
 * committed turn must never read as still working (live dot, "· Calling …").
 */
export function settleSteps(steps: StepEvent[]): StepEvent[] {
  const base = (label?: string) => (label ?? '').replace(/(\.\.\.|…)\s*$/, '').trim();
  const finished = new Set(steps.filter(s => s.status !== 'running').map(s => base(s.label)));
  return steps
    .filter(s => s.status !== 'running' || !finished.has(base(s.label)))
    .map(s => (s.status === 'running' ? { ...s, status: 'pending' as StepEvent['status'] } : s));
}

function StepsIndicator({ steps: rawSteps, settled = false }: { steps: StepEvent[]; settled?: boolean }) {
  // Default collapsed: the step list is opt-in detail. While the agent is
  // working we surface a flashing blue dot + the current step label so the
  // user sees live progress without expanding; they can expand at any time.
  const [collapsed, setCollapsed] = useState(true);
  const steps = useMemo(() => (settled ? settleSteps(rawSteps) : rawSteps), [rawSteps, settled]);
  const runningStep = steps.find(s => s.status === 'running');
  const isRunning = Boolean(runningStep);

  return (
    <div style={stepsContainerStyle}>
      <style>{stepsPulseKeyframes}</style>
      <button
        style={stepsToggleStyle}
        onClick={() => setCollapsed(prev => !prev)}
        aria-label={collapsed ? 'Expand steps' : 'Collapse steps'}
        aria-expanded={!collapsed}
      >
        <span style={stepsChevronStyle}>{collapsed ? '▶' : '▼'}</span>
        {/* Flashing blue dot indicates the agent is actively working. */}
        {isRunning && <span style={workingDotStyle} aria-hidden="true">●</span>}
        <span style={stepsLabelStyle}>
          {steps.length} step{steps.length !== 1 ? 's' : ''}
        </span>
        {/* When collapsed and still running, show the current step label so
            progress is visible without expanding the list. */}
        {collapsed && isRunning && runningStep?.label && (
          <span style={currentStepLabelStyle}>· {runningStep.label}</span>
        )}
      </button>
      {!collapsed && (
        <div style={stepsListStyle}>
          {steps.map((step, idx) => (
            <div key={step.node_id || idx} style={stepItemStyle}>
              <span style={stepStatusIconStyle(step.status)}>
                {stepStatusIcon(step.status)}
              </span>
              <span style={stepLabelTextStyle}>{step.label}</span>
              {step.duration_ms !== undefined && (
                <span style={stepDurationStyle}>
                  {step.duration_ms < 1000
                    ? `${step.duration_ms}ms`
                    : `${(step.duration_ms / 1000).toFixed(1)}s`}
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ExecutionSummaryDisplay({ summary }: { summary: ExecutionSummary }) {
  const duration = summary.total_duration_ms < 1000
    ? `${summary.total_duration_ms}ms`
    : `${(summary.total_duration_ms / 1000).toFixed(1)}s`;

  // Token counts intentionally NOT shown. `summary.usage` reflects only the main
  // orchestrator turn — dispatched sub-agents (task/planning/design/render) and
  // async DAG/gateway jobs are separate invocations whose usage is not summed in,
  // so any in/out count here under-reports. Rather than display a misleading
  // number (or a hardcoded, drift-prone per-model cost table), show wall-clock
  // speed only. See feedback #6.
  return (
    <div style={summaryContainerStyle}>
      {/* Numeric spine: wall-clock in tabular mono so turn timings compare
          down a conversation instead of jittering with each digit width. */}
      <span style={summaryItemStyle}>⏱ <span className="numeric">{duration}</span></span>
      {summary.model && (
        <span style={summaryItemStyle}>🤖 {summary.model}</span>
      )}
    </div>
  );
}

// --- Helpers ---

function stepStatusIcon(status: StepEvent['status']): string {
  switch (status) {
    case 'running': return '⟳';
    case 'done':
    case 'completed': return '✓';
    case 'failed': return '✗';
    case 'skipped': return '⊘';
    case 'pending': return '○';
    default: return '○';
  }
}

function stepStatusIconStyle(status: StepEvent['status']): React.CSSProperties {
  const base: React.CSSProperties = { fontSize: 'var(--fs-xs)', marginRight: 6, fontWeight: 600 };
  switch (status) {
    case 'running': return { ...base, color: theme.colors.primary };
    case 'done':
    case 'completed': return { ...base, color: theme.colors.success };
    case 'failed': return { ...base, color: theme.colors.error };
    case 'skipped': return { ...base, color: theme.colors.textTertiary };
    case 'pending': return { ...base, color: theme.colors.textTertiary };
    default: return { ...base, color: theme.colors.textTertiary };
  }
}

// --- Styles ---

const canvasContainerStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  height: '100%',
  overflow: 'hidden',
};



const newSessionButtonStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  padding: '4px 10px',
  borderRadius: theme.radius.button,
  border: `1px solid ${theme.colors.border}`,
  background: theme.colors.surface,
  color: theme.colors.textPrimary,
  cursor: 'pointer',
  fontWeight: theme.typography.weights.medium,
};

const messageListStyle: React.CSSProperties = {
  flex: 1,
  overflowY: 'auto',
  padding: '16px 24px',
  display: 'flex',
  flexDirection: 'column',
  gap: 12,
};








const jobReferencesRowStyle: React.CSSProperties = {
  display: 'flex',
  flexWrap: 'wrap',
  gap: 6,
  marginTop: 6,
};






const emptyStateStyle: React.CSSProperties = {
  flex: 1,
  display: 'flex',
  flexDirection: 'column',
  alignItems: 'center',
  justifyContent: 'center',
  gap: 8,
};

const welcomeHeadingStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xl)',
  fontWeight: 600,
  color: theme.colors.textPrimary,
  marginBottom: 4,
};

const emptyTextStyle: React.CSSProperties = {
  fontSize: 'var(--fs-md)',
  color: theme.colors.textSecondary,
};

// Steps indicator styles

const stepsContainerStyle: React.CSSProperties = {
  marginBottom: 8,
  borderBottom: `1px solid ${theme.colors.border}`,
  paddingBottom: 8,
};

const stepsToggleStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 4,
  background: 'none',
  border: 'none',
  cursor: 'pointer',
  padding: '2px 0',
  fontSize: 'var(--fs-xs)',
  color: theme.colors.textSecondary,
};

const stepsChevronStyle: React.CSSProperties = {
  fontSize: 'var(--fs-3xs)',
  marginRight: 2,
};

const stepsLabelStyle: React.CSSProperties = {
  fontWeight: 500,
};

// Flashing blue "working" dot shown in the collapsed steps header while a step
// is running. The keyframes live in `stepsPulseKeyframes`, injected next to the
// component so the animation is available wherever the indicator renders.
const stepsPulseKeyframes = `
@keyframes chatStepDotPulse {
  0%, 100% { opacity: 0.3; }
  50% { opacity: 1; }
}`;

const workingDotStyle: React.CSSProperties = {
  color: theme.colors.primary,
  fontSize: 'var(--fs-3xs)',
  marginRight: 2,
  animation: 'chatStepDotPulse 1.2s ease-in-out infinite',
};

const currentStepLabelStyle: React.CSSProperties = {
  marginLeft: 6,
  fontSize: 'var(--fs-2xs)',
  color: theme.colors.textSecondary,
  maxWidth: 240,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
};

const stepsListStyle: React.CSSProperties = {
  marginTop: 6,
  display: 'flex',
  flexDirection: 'column',
  gap: 4,
  paddingLeft: 4,
};

const stepItemStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  fontSize: 'var(--fs-xs)',
  color: theme.colors.textPrimary,
};

const stepLabelTextStyle: React.CSSProperties = {
  flex: 1,
};

const stepDurationStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)',
  color: theme.colors.textSecondary,
  marginLeft: 8,
};

// Execution summary styles



// Chat input styles

const chatInputContainerStyle: React.CSSProperties = {
  padding: '12px 24px 16px',
  borderTop: `1px solid ${theme.colors.border}`,
  background: theme.colors.surface,
  flexShrink: 0,
};

const chatInputRowStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'flex-end',
  gap: 8,
};

const chatTextareaStyle: React.CSSProperties = {
  flex: 1,
  resize: 'none',
  padding: '8px 14px',
  boxSizing: 'border-box',
  borderRadius: theme.radius.panel,
  border: `1px solid ${theme.colors.border}`,
  fontSize: 'var(--font-size-base)',
  lineHeight: 1.5,
  fontFamily: 'inherit',
  outline: 'none',
  minHeight: 40,
  maxHeight: CHAT_TEXTAREA_MAX_HEIGHT,
  overflowY: 'auto',
  background: theme.colors.background,
  // Without this the textarea takes the UA default (black), which looked fine
  // on a white field and was pure black on #16181C in dark mode. A form control
  // does NOT inherit `color` from the page — it has to be set.
  color: theme.colors.textPrimary,
};

const chatSubmitButtonStyle: React.CSSProperties = {
  height: 40,
  boxSizing: 'border-box',
  padding: '0 18px',
  borderRadius: theme.radius.button,
  border: 'none',
  background: theme.colors.primary,
  color: theme.colors.onPrimary,
  fontSize: 'var(--font-size-base)',
  fontWeight: theme.typography.weights.semibold,
  flexShrink: 0,
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
};

const chatStopButtonStyle: React.CSSProperties = {
  height: 40,
  boxSizing: 'border-box',
  padding: '0 18px',
  borderRadius: theme.radius.button,
  border: 'none',
  background: theme.colors.error,
  color: theme.colors.onPrimary,
  fontSize: 'var(--font-size-base)',
  fontWeight: theme.typography.weights.semibold,
  flexShrink: 0,
  cursor: 'pointer',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  gap: 6,
};

const queueIndicatorStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)',
  color: theme.colors.textSecondary,
  marginBottom: 6,
  paddingLeft: 4,
};

const charCountStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)',
  color: theme.colors.textSecondary,
  textAlign: 'right',
  marginTop: 4,
};

// Visually hidden but clickable programmatically (the Plus_Menu's `Attach files`).
const hiddenFileInputStyle: React.CSSProperties = {
  position: 'absolute', width: 1, height: 1, margin: -1, padding: 0, border: 0,
  overflow: 'hidden', clip: 'rect(0 0 0 0)', whiteSpace: 'nowrap', opacity: 0,
};

// Plain text (the project is changed from the Plus_Menu); a long project name ellipsizes.
const composerStatusStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)',
  color: theme.colors.textSecondary,
  marginTop: 6,
  paddingLeft: 4,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
};

const projectNoticeStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8,
  padding: '6px 10px', marginBottom: 8,
  borderRadius: theme.radius.button,
  fontSize: 'var(--fs-xs)', color: theme.colors.textPrimary,
  background: tint(theme.colors.warning),
  border: `1px solid ${theme.colors.warning}`,
};

const attachedFileChipStyle: React.CSSProperties = {
  display: 'inline-flex', alignItems: 'center', gap: 8,
  padding: '6px 10px', marginBottom: 8,
  borderRadius: theme.radius.button,
  background: theme.colors.background,
  border: `1px solid ${theme.colors.border}`,
};

const removeFileBtnStyle: React.CSSProperties = {
  background: 'none', border: 'none', cursor: 'pointer',
  color: theme.colors.textSecondary, lineHeight: 1,
  padding: 2, display: 'inline-flex', alignItems: 'center',
};

const attachedFileIconStyle: React.CSSProperties = {
  display: 'inline-flex', color: theme.colors.textSecondary,
};

// In-bubble upload progress — a slim bar on the user's message bubble (which is
// `primary`-filled), so it reads against that surface via `onPrimary`.
const attachedChipsRowStyle: React.CSSProperties = {
  display: 'flex',
  flexWrap: 'wrap',
  gap: 6,
};

const uploadErrorStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8,
  padding: '6px 10px', marginBottom: 8,
  borderRadius: theme.radius.button,
  fontSize: 'var(--fs-xs)', color: theme.colors.error,
  background: tint(theme.colors.error),
  border: `1px solid ${theme.colors.error}`,
};
