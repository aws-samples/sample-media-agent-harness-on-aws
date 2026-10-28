import React, { useState, useCallback, useEffect, useRef } from 'react';
import HarnessTour from '../components/tour/HarnessTour';
import HelpButton from '../components/tour/HelpButton';
import { fetchModels, type ModelInfo } from '../api';
import { theme } from '../styles/tokens';
import { useLabContext } from '../LabContext';
import { listSessions, deleteSession as deleteSessionApi, updateSessionRenderHistory, deleteSessionRender, createSessionRender, createSession, updateSessionActivity, setSessionLock, editAsset, discardChanges, getSessionRenders, deleteSpecialtyAgentDraft, publishAsset, logPersistFailure } from '../lab-api';
import type { PublishedAssetSummary } from '../lab-api';
import ResizeDivider from '../components/ResizeDivider';
import { getSessionProject, persistSessionForAction, seedSessionProject } from '../projects/sessionProjectStore';
import { onNewChatInProjectRequest } from '../projects/newChatInProject';
import { projectsEnabled as isProjectsEnabled, peekProjectsEnabled } from '../projects/api';
import ProjectsSurface from '../components/projects/ProjectsSurface';
import { OPEN_PROJECT_GRAPH_EVENT } from '../components/projects/graph/graphResult';
import { routeTimelineAction, type TimelineActionLike } from '../projects/mediaActions';
import { openActionOutputs } from '../components/chat/ActionLine';
import { RESULT_GALLERY_EVENT, resultGalleryManifest, type ResultGalleryDetail } from '../projects/mediaActions';
import { OPEN_PROJECT_EVENT } from '../projects/openProject';
import SessionSidebar, { type SessionSidebarHandle } from '../components/SessionSidebar';
import ChatCanvas from '../components/ChatCanvas';
import CenterEditor from '../components/CenterEditor';
import SessionRendererPanel from '../components/SessionRendererPanel';
import JobsPanel, { type JobsPanelHandle } from '../components/JobsPanel';
import AgentTestPanel from '../components/AgentTestPanel';
import { jobsStore, useJobs } from '../hooks/useJobs';
// NOTE: staging deleted `WorkflowsCenter` (the center aggregate view replaced
// it), so it is deliberately NOT imported here even though our side had it.
import SpecialtyAgentEditor from '../components/SpecialtyAgentEditor';
import BuiltInAgentsBrowser from '../components/BuiltInAgentsBrowser';
import SkillsCenter from '../components/SkillsCenter';
import { hasActionableSuggestions, loadImproveData } from '../improve-data';
import type { SkillDetailTarget } from '../components/SkillDetailsView';
import {
  SkillsSurfaceHeader,
  skillsNestedStyle,
  skillsSurfaceStyle,
} from '../components/SkillsSurfaceHeader';
import { AgentsSurfaceHeader, agentsSurfaceStyle } from '../components/AgentsSurfaceHeader';
import { surfaceNestedHeaderStyle } from '../components/SurfaceHeader';
import PurgeAssetDialog, { type PurgeAssetTarget } from '../components/PurgeAssetDialog';
import type { PurgeResult } from '../capability-api';
import CapabilitySwitch from '../components/settings/CapabilitySwitch';
import { InventoryStateFilter, type InventoryState } from '../components/InventoryStateFilter';
import Button from '../components/ui/Button';
import EmptyState from '../components/ui/EmptyState';
import InventoryRow, { InventoryRowList } from '../components/ui/InventoryRow';
import Badge from '../components/ui/Badge';
import { InventorySectionHeader } from '../components/settings/panelChrome';
import type { CustomAgentSummary } from '../components/SessionSidebar';
import ActivityPanel from '../components/ActivityPanel';
import CompletionNotifications from '../components/CompletionNotifications';
import type { JobNotification } from '../services/notifications';
import { rollupRenderUrls } from '../services/notifications';
import { useMomentClipAutoOpen } from '../hooks/useMomentClipAutoOpen';
// buildResultTabs is intentionally not used here any more: the render-artifacts
// hook is the only producer of render tabs. renderTab/manifestBlock stay for
// lazy tabs persisted in sessions created before that change.
import { renderTabOutcome, manifestBlock } from '../services/renderTabs';
import type { RenderHistoryEntry, InteractionEvent, TimelineActionData, BatchClipActionData, OpenTabActionData } from '../components/renderer/types';
import { OPEN_ASSET_EVENT, type OpenAssetEventDetail } from '../projects/openCitation';
import { pageOfSpan } from '../shared/pageLocator';
import { normalizeViewState } from '../shared/renderViewState';
import type { RenderManifest } from '../components/nle/manifestParser';
import { renderTabKey, entryManifestKey, manifestKeyOfUrl, newClientId } from '../components/renderer/renderTabKey';
import { setActiveSessionId } from '../services/activeSession';
import { collapseToLineages } from '../utils/publishedLineages';
import { setRenderCache } from '../utils/safeStorage';
import { classifyUrl } from '../components/chat/artifactUrl';
import { shouldRevertSpuriousOpen, shouldShowRenderer, shouldDropComposingEntry } from '../services/renderOrchestration';
import { decideResultsAction, failedNotificationTurn, summaryNotificationTurn } from '../services/resultsFetch';
import {
  clampWidth, fitPanelWidths, persistWidth, readWidth,
  LEFT_MIN, LEFT_MAX, LEFT_DEFAULT,
  RIGHT_MIN, RIGHT_MAX, RIGHT_DEFAULT,
} from '../utils/layout';

// Legacy localStorage fallback: reconstruct render history from stored
// message text. Only recovers already-produced Render Agent manifests
// (artifacts/.../render/*.json). Live media rendering flows through the
// Render Agent and is persisted to the DynamoDB session renderHistory,
// which is the primary restore source — this text scan is a last resort.
const RENDER_MANIFEST_PATTERN = /artifacts\/[\w\-./]*\/render\/[\w\-]+\.json/i;

function scanMessagesForArtifacts(messages: { role: string; content: string }[]): RenderHistoryEntry[] {
  const entries: RenderHistoryEntry[] = [];
  const seen = new Set<string>();
  for (const msg of messages) {
    if (msg.role !== 'assistant') continue;
    const m = msg.content.match(RENDER_MANIFEST_PATTERN);
    if (m && !seen.has(m[0])) {
      seen.add(m[0]);
      entries.push({
        turnIndex: 0,
        timestamp: Date.now(),
        blocks: [{ template: 'manifest-router', title: 'Render Output', data: { manifestUrl: `/api/artifact?key=${encodeURIComponent(m[0])}` } }],
      });
    }
  }
  return entries;
}

// Merge the S3-authoritative render manifests into whatever the UI had cached.
// The SERVER composes per-source composites (`GET /sessions/:id/renders` returns ONE finished
// composite per source + solos). The UI does NOT merge — and, crucially, the SERVER IS
// AUTHORITATIVE for the render-tab set whenever it returns anything. It maps each server render to
// a tab (composite → inline block, solo → url) and keeps ONLY still-composing live placeholders
// (an in-flight render not yet written to S3). This is what makes the tab set stable AND correct:
// live tabs created from the stream (which are keyed differently than the S3 manifests) never
// linger beside the server's finished composite — so "audio extraction created a separate tab"
// can't happen once the server lists the composite. When the server returns NOTHING (a transient
// empty list / a session whose manifests aren't under its own prefix, e.g. seeded e2e fixtures),
// the cache is preserved so the panel is never blanked.
/** The tab that shows manifest `key`: its own key, a composite member, or its manifestUrl. -1 when none does. */
export function tabIndexHoldingManifest(tabs: RenderHistoryEntry[], key: string): number {
  return tabs.findIndex((e) => {
    if (renderTabKey(e) === key) return true;
    const d = (e.blocks?.[0]?.data ?? {}) as { memberKeys?: unknown; manifestUrl?: unknown };
    if (Array.isArray(d.memberKeys) && d.memberKeys.includes(key)) return true;
    return manifestKeyOfUrl(d.manifestUrl) === key;
  });
}

export function mergeServerRenders(
  cached: RenderHistoryEntry[],
  serverRenders: { key: string; label?: string; createdAt: string; manifest?: Record<string, unknown>; composite?: boolean; sourceKey?: string; memberKeys?: string[] }[],
): RenderHistoryEntry[] {
  const isComposite = (r: typeof serverRenders[number]) => r.composite === true || String(r.key).startsWith('composite:');
  // Every tab carries its stable identity (R8.3): the manifest key, or the composite key. A
  // composite's block also keeps `compositeKey` + `memberKeys`, so once leanRenderHistory strips its
  // inline manifest the persisted tab still names the render it is (ManifestRouter shows it loading
  // until this merge supplies the manifest) instead of degrading to a broken {sourceKey} block.
  const cachedByKey = new Map<string, RenderHistoryEntry>();
  for (const e of cached) { const k = renderTabKey(e); if (!cachedByKey.has(k)) cachedByKey.set(k, e); }
  const toEntry = (r: typeof serverRenders[number]): RenderHistoryEntry => {
    const sourceKey = r.sourceKey ?? (r.manifest as { sourceKey?: string } | undefined)?.sourceKey;
    const manifestUrl = `/api/artifact?key=${encodeURIComponent(r.key)}`;
    const data = isComposite(r)
      ? { manifest: r.manifest as unknown as RenderManifest, sourceKey, compositeKey: r.key, memberKeys: r.memberKeys }
      : { manifestUrl };
    const entry: RenderHistoryEntry = {
      turnIndex: 0,
      timestamp: Date.parse(r.createdAt) || Date.now(),
      label: r.label,
      key: r.key,
      blocks: [{ template: 'manifest-router', title: r.label || 'Render Output', data }],
    };
    // The SAME solo render already on screen keeps its blocks (by identity), so a reconcile never
    // reloads it — notably an Expand view, whose block also carries its inline manifest.
    const prev = isComposite(r) ? undefined : cachedByKey.get(r.key);
    const prevBlock = prev?.blocks?.[0];
    if (prevBlock?.template === 'manifest-router' && (prevBlock.data as { manifestUrl?: unknown }).manifestUrl === manifestUrl) {
      return { ...entry, blocks: prev!.blocks };
    }
    return entry;
  };

  const serverEntries = serverRenders.map(toEntry);
  // Empty/transient server list → keep the cache (never blank the panel; preserves seeded fixtures).
  if (serverEntries.length === 0) return cached;
  // Server is authoritative: its finished renders + any still-composing live placeholder.
  const composing = cached.filter(e => e.status === 'composing' && (e.blocks?.length ?? 0) === 0);
  // Client-originated PREVIEWS (upload / async-result renders carry `artifactUrls`) can FINISH
  // composing client-side — blocks populated, but `status` stays 'composing' — BEFORE their own S3
  // manifest is listed by the server. Such a preview must NOT be dropped just because an unrelated
  // render landed first: an uploaded source video vanished when a same-turn proxy's render arrived
  // and the server list did not yet contain the source's (slow) manifest. Preserve the preview
  // until the server LISTS or ABSORBS its manifest key, then let the server version supersede it
  // (dedup by key). Stale STREAM tabs carry a manifestUrl but NO artifactUrls, so they remain
  // server-authoritative-dropped (unchanged).
  const serverKeys = new Set<string>();
  for (const r of serverRenders) {
    if (r.key) serverKeys.add(r.key);
    for (const mk of r.memberKeys ?? []) serverKeys.add(mk);
  }
  // Content-based absorption. The manifest-KEY check below matches only previews that carry a
  // `manifestUrl`. A client preview carrying an INLINE manifest (no manifestUrl) has key '' and can
  // NEVER match serverKeys — so it lingered beside the server tab even after the server absorbed its
  // content (session c8a8fc62: three per-turn generated-image previews survived next to the ONE
  // server "Generated Images" gallery → duplicate tiles). The server is authoritative, so a preview
  // whose artifacts are ALL already present in a server composite is redundant and must drop. This
  // still preserves the be60b441 behaviour: a preview whose artifacts the server has NOT listed yet
  // (not in any composite) is kept until the server catches up.
  const serverArtifactSrcs = new Set<string>();
  for (const r of serverRenders) {
    const m = (r.manifest ?? {}) as { images?: Array<{ src?: unknown }>; tracks?: Array<{ src?: unknown }>; src?: unknown };
    for (const im of m.images ?? []) if (im?.src) serverArtifactSrcs.add(String(im.src));
    for (const t of m.tracks ?? []) if (t?.src) serverArtifactSrcs.add(String(t.src));
    if (typeof m.src === 'string' && m.src) serverArtifactSrcs.add(m.src);
  }
  const absorbedByContent = (e: RenderHistoryEntry): boolean => {
    const urls = e.artifactUrls ?? [];
    return urls.length > 0 && urls.every(u => serverArtifactSrcs.has(u));
  };
  const pendingPreviews = cached.filter(e =>
    (e.artifactUrls?.length ?? 0) > 0 &&
    !(e.status === 'composing' && (e.blocks?.length ?? 0) === 0) && // still-composing already kept above
    !serverKeys.has(entryManifestKey(e)) &&                        // server hasn't listed it by key, AND
    !absorbedByContent(e),                                         // server hasn't absorbed its content
  );
  // Client-authored tabs the server cannot list yet (an Expand view whose POST is in flight or
  // failed) survive the reconcile until the server lists them under the same key.
  const clientOnly = cached.filter(e => e.clientOnly && !serverKeys.has(renderTabKey(e)));
  return [...serverEntries, ...composing, ...pendingPreviews, ...clientOnly].sort((a, b) => a.timestamp - b.timestamp);
}

function formatModelId(id: string): string {
  if (!id) return '';
  const last = id.split('.').pop() || id;
  return last.replace(/-/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

const LEFT_WIDTH_KEY = 'lab-left-sidebar-width';
const RIGHT_WIDTH_KEY = 'lab-right-panel-width';
const SIDEBAR_COLLAPSED_KEY = 'lab-sidebar-collapsed';
const RIGHT_COLLAPSED_KEY = 'lab-right-collapsed';
const COLLAPSED_SIDEBAR_WIDTH = 48;

// Union of both branches: staging replaced 'workflows' with 'activity' (the
// center aggregate view), and 'custom-agent' is ours (the agent editor +
// published read-only view that hosts the test console). 'projects' is the
// Projects_Surface (media-harness-projects-access task 13.8).
//
// This is one of the TWO center-mode union declarations; `SessionSidebar`'s
// `SIDEBAR_CENTER_MODES` is the other. They are kept separate on purpose, so
// `center-mode-unions.contract.test.ts` is what guards them against drift.
export const CENTER_MODES = ['chat', 'activity', 'custom-agent', 'skills', 'projects'] as const;
export type CenterMode = typeof CENTER_MODES[number];

/**
 * Lab `DraftCategory` categories whose editor the Projects_Surface hosts itself (design Component 11
 * "The two traps"). EMPTY in v1: no Projects tab edits a Lab asset, so `HOSTS_OWN_EDITOR` is unchanged and
 * an `editingTarget` opened while the surface is up still moves the user to chat. The guard rule is wired
 * now (the chat-pane CenterEditor is suppressed, and the effect does not force chat, for a listed
 * category) so a future tab that hosts an editor adds its category here and nothing else.
 */
export const PROJECTS_HOSTED_EDITOR_CATEGORIES: readonly string[] = [];

interface WorkspaceViewProps {
  onOpenSettings: (tab?: string) => void;
  onShowLanding?: () => void;
}

export interface WorkspaceSession {
  id: string;
  title: string;
  timestamp: number;
  preview?: string;
  messageCount?: number;
  renderHistory?: RenderHistoryEntry[];
  /** Demo full-freeze mirror of SessionMetadata.locked. */
  locked?: boolean;
}

export function prependEphemeralSession(
  sessions: WorkspaceSession[],
  activeSessionId: string,
  nextSession: WorkspaceSession,
  retainedSessionIds: ReadonlySet<string>,
): WorkspaceSession[] {
  return [
    nextSession,
    ...sessions.filter(session =>
      session.id !== nextSession.id &&
      (session.id !== activeSessionId || retainedSessionIds.has(session.id)),
    ),
  ];
}

// Name a render entry for a tab. Labeled entries (the hook names the view after
// the tool that produced it) pass through unchanged; unlabeled ones are
// numbered "Render Output N" against the EXISTING history so several unnamed
// renders stay distinguishable instead of all reading "Render Output". Pure so
// both the active-session path (numbered against React state) and the
// background-session path (numbered against the persisted history) share one
// rule. Exported for unit testing.
export function nameRenderEntry(
  existing: RenderHistoryEntry[],
  entry: RenderHistoryEntry,
): RenderHistoryEntry {
  if (entry.label) return entry;
  const n = existing.filter((e) => !e.label || /^Render Output( \d+)?$/.test(e.label)).length + 1;
  const label = `Render Output ${n}`;
  return {
    ...entry,
    label,
    blocks: (entry.blocks ?? []).map((b, i) => (i === 0 ? { ...b, title: label } : b)),
  };
}

// Compose the labeled `[sources: …]` line appended to a batch clip action's agent prompt.
// Each clip carries its own url so the agent can fan out per file; a frame-extract action
// also carries each clip's capture point (its mark-in), appended as @<t>s. Exported for testing.
export function batchSourcesLine(
  clips: { label: string; src: string; at?: number }[],
): string {
  return (clips || [])
    .map((c) => {
      const at = typeof c.at === 'number' && Number.isFinite(c.at) ? `@${c.at.toFixed(2)}s` : '';
      return `"${c.label}"=${c.src}${at}`;
    })
    .join('; ');
}

export default function WorkspaceView({ onOpenSettings, onShowLanding = () => {} }: WorkspaceViewProps) {
  const { sessionId, switchSession, editingTarget, setEditingTarget, specialtyAgents,
    publishedSpecialtyAgents, refreshCategory, ensureCategoryLoaded,
    loadingCategories, skills, publishedSkills } = useLabContext();

  const [centerMode, setCenterMode] = useState<CenterMode>('chat');
  const [activeRunId, setActiveRunId] = useState<string>('');

  // Projects_Surface state (media-harness-projects-access task 13.8).
  // `projectsEnabledState` from `/config.projectsEnabled` gates the sidebar nav entry. Seeded from the
  // already-loaded config (sign-in fetched it) so the tab does not pop in after the other nav entries.
  const [projectsEnabledState, setProjectsEnabledState] = useState(() => peekProjectsEnabled() ?? false);
  useEffect(() => {
    let live = true;
    isProjectsEnabled().then((on) => { if (live) setProjectsEnabledState(on); }).catch(() => {});
    return () => { live = false; };
  }, []);
  // The Selected_Project id, per browser tab, surviving reload and mode switches (Req 10.2).
  const [projectsSelectedId, setProjectsSelectedId] = useState<string | null>(() => {
    try { return sessionStorage.getItem('lab-projects-selected'); } catch { return null; }
  });
  const persistProjectsSelected = useCallback((id: string) => {
    setProjectsSelectedId(id);
    try { sessionStorage.setItem('lab-projects-selected', id); } catch { /* storage unavailable */ }
  }, []);
  // Bumped by "New project" to open the surface's inline create form (Req 10.4).
  const [projectsCreateRequest, setProjectsCreateRequest] = useState(0);

  // Custom Agent center-mode state (Req 1.1, 1.5, 1.6). `WorkspaceView`
  // owns `centerMode`, so it also owns which Custom_Agent (if any) has its
  // editor open. `customAgentEditorOpen` distinguishes the list view (false)
  // from the editor view (true); `editingCustomAgentId` is the asset under
  // edit, or `null` for a brand-new draft (create mode).
  const [customAgentEditorOpen, setCustomAgentEditorOpen] = useState(false);
  const [editingCustomAgentId, setEditingCustomAgentId] = useState<string | null>(null);
  // A published agent the Owner is VIEWING (read-only) — set when a published
  // row is clicked. Non-null → the read-only detail panel is shown instead of
  // the editor; clicking Edit here forks it into a working copy.
  const [viewingPublished, setViewingPublished] = useState<PublishedAssetSummary | null>(null);
  const [forkingPublished, setForkingPublished] = useState(false);
  // When Edit forks a published agent, remember that working-copy id. If the
  // editor closes WITHOUT the user saving/publishing, we discard it so a mere
  // "open to edit" never leaves a lingering draft (the published version stays
  // the canonical state). Genuine drafts (New agent / pre-existing) are never
  // recorded here, so they're never discarded.
  const justForkedIdRef = React.useRef<string | null>(null);

  // The tool/workflow editor (CenterEditor) only renders in chat mode. An
  // editingTarget can be set from Settings while the Workflows or Agents center
  // is active (e.g. + Create on the Settings Tools tab), so force chat mode
  // whenever one is set — otherwise the editor mounts into a branch that isn't
  // rendered and the action appears to do nothing.
  //
  // SKILLS AND WORKFLOWS ARE EXEMPT: both centers render their own
  // CenterEditor, so forcing chat here would throw the user out on every Edit
  // and leave "Back" returning them to Chat instead of the list they came from.
  const HOSTS_OWN_EDITOR = new Set(['skill', 'workflow']);
  React.useEffect(() => {
    // A projects-hosted category (empty in v1) is exempt too, so a Projects editor is not thrown to chat.
    if (
      editingTarget
      && !HOSTS_OWN_EDITOR.has(editingTarget.category)
      && !PROJECTS_HOSTED_EDITOR_CATEGORIES.includes(editingTarget.category)
    ) setCenterMode('chat');
  }, [editingTarget]);

  // Map the Owner's SpecialtyAgent drafts (from LabContext) onto the
  // lightweight row view model the sidebar/list render (Req 1.2). Drafts are
  // working copies, so the lifecycle state surfaces as "Draft" (the summary
  // omits it and the row falls back accordingly).
  // Drafts (editable working copies) + published versions the Owner has shipped.
  // A published agent whose working copy was deleted on publish otherwise never
  // appears here (the drafts query only returns the OWNED partition), so we merge
  // in publishedSpecialtyAgents, tagged 'published' and deduped by id (a live
  // working copy wins, shown as a draft).
  // Purge state. `purgedIds` is a session-local tombstone list: when the
  // specialty-agent category is served from the Registry, the projector converges
  // asynchronously, so for a few seconds after a successful purge the catalog
  // still lists what DynamoDB no longer has. Filtering here keeps the list honest
  // without waiting on convergence.
  const [purgeTarget, setPurgeTarget] = useState<PurgeAssetTarget | null>(null);
  const [purgedIds, setPurgedIds] = useState<ReadonlySet<string>>(() => new Set());

  const draftIds = new Set(specialtyAgents.map(a => a.assetId));
  const publishedByAssetId = new Map(publishedSpecialtyAgents.map(p => [p.assetId, p]));
  const publishedOnly = publishedSpecialtyAgents.filter(p => !draftIds.has(p.assetId));
  const customAgentsAll: CustomAgentSummary[] = [
    ...specialtyAgents.map(a => ({
      id: a.assetId,
      name: a.name,
      description: a.description,
      lastModified: a.updatedAt,
      // Preserved so the row can withhold destructive/edit actions: the core
      // Task Agent is a virtual row appended by the service layer
      // (`lab/service.py` `_handle_list_specialty_agent_drafts`) with no
      // AssetIndex record, so every mutation addressed to it fails.
      isCoreAgent: a.isCoreAgent,
      // A working copy shadows its published versions in this list. Carrying
      // the published version through means the row can still offer "View
      // published" instead of hiding that the lineage has shipped versions.
      publishedVersion: publishedByAssetId.get(a.assetId)?.version,
    })),
    ...publishedOnly.map(p => ({
      id: p.assetId,
      name: p.name,
      description: p.description,
      lifecycleState: 'published',
      lastModified: p.updatedAt,
      publishedVersion: p.version,
    })),
  ];
  /*
   * The sidebar's Skills entry listed WORKING COPIES only, so publishing everything
   * you own emptied it: publishing consumes the working copy, so two published
   * skills and no drafts read as "Skills 0" over "No skills yet. Create one to get
   * started." — while the Skills centre showed both, correctly.
   *
   * Merged the way the Agents entry above already merges, and for the same reason. A
   * working copy SHADOWS its own published versions rather than listing twice, and
   * `collapseToLineages` is what keeps a lineage with three versions from becoming
   * three rows.
   */
  const draftSkillIds = new Set(skills.map(skill => skill.assetId));
  const publishedOnlySkills = collapseToLineages(
    publishedSkills.filter(published => !draftSkillIds.has(published.assetId)),
  ).map(({ summary }) => summary);
  const skillSummaries = [
    ...skills.map(skill => ({
      assetId: skill.assetId,
      name: skill.name,
      updatedAt: skill.updatedAt,
    })),
    ...publishedOnlySkills.map(summary => ({
      assetId: summary.assetId,
      name: summary.name,
      updatedAt: summary.updatedAt,
      publishedVersion: summary.version,
    })),
  ];

  const customAgents = purgedIds.size
    ? customAgentsAll.filter(a => !purgedIds.has(a.id))
    : customAgentsAll;
  // Every published version the actor owns, keyed by lineage id — including
  // ones shadowed by a live working copy, which `publishedOnly` excludes.
  // `publishedAgentById` is the subset a row can open directly as its own
  // read-only detail view.
  const publishedAgentById = new Map(
    publishedOnly.filter(p => !purgedIds.has(p.assetId)).map(p => [p.assetId, p]),
  );
  const [leftWidth, setLeftWidth] = useState<number>(() => readWidth(LEFT_WIDTH_KEY, LEFT_DEFAULT, LEFT_MIN, LEFT_MAX));
  const [rightWidth, setRightWidth] = useState<number>(() => readWidth(RIGHT_WIDTH_KEY, RIGHT_DEFAULT, RIGHT_MIN, RIGHT_MAX));
  const [viewportWidth, setViewportWidth] = useState<number>(() => (typeof window === 'undefined' ? 1920 : window.innerWidth));
  useEffect(() => {
    const onResize = () => setViewportWidth(window.innerWidth);
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);
  const [sidebarCollapsed, setSidebarCollapsed] = useState<boolean>(() => localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === 'true');
  const [rightCollapsed, setRightCollapsed] = useState<boolean>(() => localStorage.getItem(RIGHT_COLLAPSED_KEY) === 'true');
  const [rightTab, setRightTab] = useState<'renderer' | 'jobs'>('renderer');

  const [sessions, setSessions] = useState<WorkspaceSession[]>(
    [{ id: sessionId, title: 'New Session', timestamp: Date.now() }]
  );
  // First visit = the user has no saved sessions yet. Only then does the tour open by itself; `null` until the
  // list has loaded (and stays false if it fails) so a returning user is never shown it by accident.
  const [firstVisit, setFirstVisit] = useState<boolean | null>(null);

  // Imperative handle to the sidebar's Jobs section. Task 13.1 exposes
  // `scrollToJob(jobId)`; task 13.3 plumbs this into the inline
  // `JobReference` chip's onClick via the `handleActivateJob` callback
  // below. Held here at the WorkspaceView level because both the
  // sidebar (left pane) and the chat (center pane) live in this subtree.
  // A turn requested on the user's behalf when they open a completion
  // notification, so the results are fetched and answered rather than leaving them
  // to ask. Carries the job id so it fires exactly once per job.
  const [pendingPrompt, setPendingPrompt] = useState<{ id: string; text: string } | null>(null);
  const sidebarRef = useRef<SessionSidebarHandle>(null);
  const jobsPanelRef = useRef<JobsPanelHandle>(null);

  // Stable callback the chat surface invokes when an inline JobReference
  // chip is clicked (task 13.3, R7.2). Wraps the imperative handle so
  // ChatCanvas (and any descendant rendering a chip) doesn't take a
  // ref directly. The callback is stable across renders so memoized
  // children won't re-render just because we passed it down. The
  // sidebar's `scrollToJob` no-ops when the row isn't currently
  // rendered, so this stays safe even when the sidebar is collapsed
  // or the job has aged out of the visible window.
  const handleActivateJob = useCallback((jobId: string) => {
    setRightCollapsed(false);
    localStorage.setItem(RIGHT_COLLAPSED_KEY, 'false');
    setRightTab('jobs');
    // Wait for the Jobs panel to mount/switch before scrolling to the row.
    requestAnimationFrame(() => jobsPanelRef.current?.scrollToJob(jobId));
  }, []);

  // Auto-open the Jobs tab the moment a newly-dispatched job's first
  // `job.update` SSE frame lands in the shared store — this fires
  // regardless of which session/agent turn started it, so it also covers
  // jobs kicked off by a specialty/task agent the chat stream doesn't
  // narrate step-by-step. `subscribeNewJob` only fires for jobs the store
  // didn't already know about (i.e. NOT the initial GET /jobs seed or a
  // reconnect resync of already-visible jobs), so this won't yank an
  // already-open renderer view on every page load.
  // A newly-dispatched job no longer force-switches the right rail to the Jobs tab —
  // that yanked the user out of whatever they were doing. Jobs run in the BACKGROUND;
  // discoverability is a passive running-dot on the Jobs tab (below) + the completion
  // bell. The user opens Jobs on their own. (subscribeNewJob is still used elsewhere for
  // per-session render wiring; we simply don't hijack the view here.)

  const [activeSession, setActiveSession] = useState<string>(sessionId);
  // A ref mirror of `activeSession`, read inside the render callbacks below to
  // decide whether an incoming render/pending event belongs to the session on
  // screen. A ref (not the state value captured in a useCallback closure) so a
  // background turn's late-arriving event is compared against the CURRENT active
  // session, never a stale one from when the callback was memoized.
  const activeSessionRef = useRef(activeSession);
  // Passive running-jobs indicator for the Jobs tab (jobs run in the background; no
  // auto-open). Excludes plan CHILD rows so an N-node DAG counts once.
  const { jobs: jobsForBadge } = useJobs();
  const jobsRunningCount = jobsForBadge.filter(
    j => (j.status === 'PENDING' || j.status === 'RUNNING') && !(j.runId && j.runId !== j.jobId),
  ).length;
  const [renderHistory, setRenderHistory] = useState<RenderHistoryEntry[]>(() => {
    try {
      const stored = localStorage.getItem(`lab-render-${sessionId}`);
      return stored ? JSON.parse(stored) : [];
    } catch { return []; }
  });
  const [renderIndex, setRenderIndex] = useState<number>(() => {
    try {
      const stored = localStorage.getItem(`lab-render-${sessionId}`);
      const parsed = stored ? JSON.parse(stored) : [];
      return parsed.length;
    } catch { return 0; }
  });
  // #1 fetch/focus: read the latest render tabs + track per-job fetch dispatch times inside the
  // notification callback without re-memoizing it on every renderHistory change.
  const renderHistoryRef = useRef(renderHistory);
  // The manifest the stream just announced, to select once a reconcile shows which tab holds it.
  const focusManifestRef = useRef<{ key: string; until: number } | null>(null);
  useEffect(() => { renderHistoryRef.current = renderHistory; }, [renderHistory]);
  const lastResultsFetchRef = useRef<Map<string, number>>(new Map());
  // Keys of tabs whose manifest is being composed right now (handleRenderTab). Merges drop an empty
  // 'composing' tab as stale — except one of these, which its compose is about to fill (by key).
  const composingTabKeysRef = useRef<Set<string>>(new Set());
  const isStaleComposing = useCallback(
    (e: RenderHistoryEntry) => shouldDropComposingEntry(e) && !composingTabKeysRef.current.has(renderTabKey(e)),
    [],
  );
  // Keys of tabs the user closed this session — a view whose save lands after its tab was closed
  // deletes the manifest it created instead of resurrecting the tab.
  const removedTabKeysRef = useRef<Set<string>>(new Set());
  // Load-time merge of the server render list (mount + session switch): merged into the current
  // tabs, selecting the last one as before.
  const applyServerRenders = useCallback((serverRenders: Parameters<typeof mergeServerRenders>[1]) => {
    setRenderHistory(prev => {
      const merged = mergeServerRenders(prev.filter(e => !isStaleComposing(e)), serverRenders);
      if (merged.length === 0) return prev;
      setRenderIndex(merged.length - 1);
      return merged;
    });
  }, []);
  // The model the agent reported using on the last turn. Authoritative for the
  // badge: a user selection is a REQUEST, this is what actually ran.
  const [reportedModel, setReportedModel] = useState<string>(() => {
    try { return localStorage.getItem('lab-orchestrator-model-active') || ''; } catch { return ''; }
  });
  useEffect(() => {
    const onModel = (e: Event) => {
      const id = (e as CustomEvent<{ modelId?: string }>).detail?.modelId;
      if (id) setReportedModel(id);
    };
    window.addEventListener('orchestrator:model', onModel);
    return () => window.removeEventListener('orchestrator:model', onModel);
  }, []);

  const [activeModel, setActiveModel] = useState<string>(() =>
    localStorage.getItem('lab-orchestrator-model-id') || ''
  );
  // Model list (id -> friendly name); the first entry is the orchestrator
  // default. Used to show the ACTUAL model in the top bar instead of a
  // generic "default model" label.
  const [models, setModels] = useState<ModelInfo[]>([]);
  useEffect(() => { fetchModels().then(setModels).catch(() => {}); }, []);
  const [renderPending, setRenderPending] = useState<boolean>(false);

  useEffect(() => {
    const handler = () => {
      setActiveModel(localStorage.getItem('lab-orchestrator-model-id') || '');
    };
    window.addEventListener('storage', handler);
    window.addEventListener('model-changed', handler);
    return () => { window.removeEventListener('storage', handler); window.removeEventListener('model-changed', handler); };
  }, []);

  // Load sessions from DynamoDB — source of truth for persisted sidebar rows.
  // The current empty workspace remains local until its first valid input.
  const persistedSessionIdsRef = useRef(new Set<string>());
  const retainedSessionIdsRef = useRef(new Set<string>());
  const deletedSessionIdsRef = useRef(new Set<string>());
  const backendLoadedRef = useRef(false);
  useEffect(() => {
    if (backendLoadedRef.current) return;
    backendLoadedRef.current = true;
    listSessions().then(remote => {
      setFirstVisit(remote.length === 0);
      const remoteIds = new Set(remote.map(session => session.sessionId));
      persistedSessionIdsRef.current = new Set(remoteIds);
      retainedSessionIdsRef.current = new Set(remoteIds);
      const dbSessions: WorkspaceSession[] = remote.map(r => ({
        id: r.sessionId,
        title: r.title || r.sessionId.slice(0, 8),
        // Order by CREATION time (ms) so the list is stable — activity must not
        // reorder rows under the user. Fall back to lastActivityAt only if a
        // row somehow lacks createdAt.
        timestamp: new Date(r.createdAt || r.lastActivityAt).getTime(),
        renderHistory: r.renderHistory,
        locked: r.locked,
      }));
      // Keep the current session visible locally even when it has not been
      // persisted yet. It becomes durable only on its first valid input.
      if (!dbSessions.find(s => s.id === sessionId)) {
        dbSessions.unshift({ id: sessionId, title: 'New Session', timestamp: Date.now() });
        void seedSessionProject(sessionId, { persisted: false });
      }
      // Projects: restore each session's Active_Project and web-search value from its row (Req 3.6).
      // Inert when Projects are disabled.
      for (const r of remote) {
        void seedSessionProject(r.sessionId, { activeProjectId: r.activeProjectId, webSearch: r.webSearch });
      }
      dbSessions.sort((a, b) => b.timestamp - a.timestamp);
      setSessions(dbSessions);
      // NB: we deliberately do NOT cache the sessions list in localStorage.
      // It was written to `lab-sessions` but never read back (the list is always
      // reloaded from DynamoDB via listSessions()), and each entry carries its
      // full renderHistory — so the blob grew unbounded until it filled the ~5MB
      // localStorage quota, at which point the next unrelated setItem (e.g.
      // 'lab-orchestrator-model-id') threw QuotaExceededError. Purge any stale
      // copy left by older builds to reclaim that space.
      try { localStorage.removeItem('lab-sessions'); } catch { /* ignore */ }
      // Restore this session's renders.
      //
      // The S3 manifests are authoritative; renderHistory is a cache the UI itself wrote, and
      // it only ever captured renders that completed while a browser was connected. Media jobs
      // settle asynchronously, so the renders that mattered most were the ones it missed --
      // one live session had 4 manifests in S3 against 2 cached, another 3 against 0.
      //
      // Cached entries are kept and merged rather than discarded: they may carry a better
      // label than one derived from renderSkill, and dropping them would lose ordering for
      // anything already on screen.
      const active = dbSessions.find(s => s.id === sessionId);
      // A persisted 'composing' entry is stale on load (the render that would
      // fill it is long gone) — never restore a spinning placeholder.
      const cached: RenderHistoryEntry[] = (active?.renderHistory ?? []).filter(e => !shouldDropComposingEntry(e));
      if (cached.length > 0) { setRenderHistory(cached); setRenderIndex(cached.length - 1); }
      getSessionRenders(sessionId).then((serverRenders) => {
        if (activeSessionRef.current !== sessionId) return;
        // Merge into the CURRENT tabs (the cache applied above, plus anything opened since — an
        // Expand view opened while this list was in flight must not be overwritten by the snapshot).
        applyServerRenders(serverRenders);
      }).catch(() => { /* keep cached (already applied above) */ });
    }).catch(() => {});
  }, []);

  // The sessions list is NOT persisted to localStorage: it was a write-only
  // cache (nothing read it back — the list reloads from DynamoDB), and carrying
  // renderHistory per entry made it the blob that filled the storage quota and
  // broke unrelated setItem calls. See the removeItem('lab-sessions') above.

  // Note: Session persistence is handled per-turn in ChatCanvas via AgentCore Memory.
  // No debounced batch-save needed — each turn writes immediately after stream completion.

  // Update session title and preview from messages
  useEffect(() => {
    const handler = () => {
      try {
        const stored = localStorage.getItem(`lab-session-${activeSession}`);
        if (!stored) return;
        const msgs = JSON.parse(stored) as { role: string; content: string }[];
        const firstUser = msgs.find(m => m.role === 'user');
        const lastNonEmpty = [...msgs].reverse().find(m => m.content && m.content.trim().length > 0);
        const preview = lastNonEmpty
          ? (lastNonEmpty.role === 'assistant'
            ? lastNonEmpty.content.replace(/[#*_`>\[\]]/g, '').trim().slice(0, 80)
            : lastNonEmpty.content.slice(0, 80))
          : undefined;
        setSessions(prev => prev.map(s => {
          if (s.id !== activeSession) return s;
          const updates: Partial<typeof s> = { preview, messageCount: msgs.length };
          // Do NOT bump timestamp on new activity: the sidebar is ordered by
          // CREATION time and must stay stable — clicking into or messaging a
          // chat must not move it to the top under the user.
          if (firstUser && s.title === 'New Session') {
            updates.title = firstUser.content.slice(0, 40) + (firstUser.content.length > 40 ? '…' : '');
          }
          return { ...s, ...updates };
        }));
      } catch { /* ignore */ }
    };
    window.addEventListener('storage', handler);
    const timer = setInterval(handler, 2000);
    return () => { window.removeEventListener('storage', handler); clearInterval(timer); };
  }, [activeSession]);

  // A drag starts from the width the panel is DRAWN at (fitted to the window), not the saved one — otherwise
  // the first pixel of a drag in a small window would jump the panel back to its saved size.
  const fittedRef = useRef({ left: 0, right: 0 });
  const handleLeftResize = useCallback((delta: number) => {
    setLeftWidth(() => { const next = clampWidth(fittedRef.current.left + delta, LEFT_MIN, LEFT_MAX); fittedRef.current.left = next; persistWidth(LEFT_WIDTH_KEY, next); return next; });
  }, []);

  const handleRightResize = useCallback((delta: number) => {
    setRightWidth(() => { const next = clampWidth(fittedRef.current.right - delta, RIGHT_MIN, RIGHT_MAX); fittedRef.current.right = next; persistWidth(RIGHT_WIDTH_KEY, next); return next; });
  }, []);

  const handleToggleSidebar = useCallback(() => {
    setSidebarCollapsed(prev => { const next = !prev; localStorage.setItem(SIDEBAR_COLLAPSED_KEY, String(next)); return next; });
  }, []);

  const handleToggleRightPanel = useCallback(() => {
    setRightCollapsed(prev => { const next = !prev; localStorage.setItem(RIGHT_COLLAPSED_KEY, String(next)); return next; });
  }, []);

  // Renderer → session bridge. A timeline verb (Clip / Stems / Extract frame …)
  // emits an `action` interaction whose payload carries a composed prompt; we
  // push it to the agent as a LIVE turn via `chat:send` (scoped to the active
  // session) — the user asked for a one-click action, not a prefill they must
  // still submit. Scrub/select/mark events are view-only and ignored.
  const handleRenderInteraction = useCallback((event: InteractionEvent) => {
    // Open an EXISTING media url as its own Renderer tab (a clip-gallery video card's
    // "Expand" → a single-video timeline). Client-side, NO agent turn: reuse the
    // `lab:render-urls` path (appends a tab + composes its manifest). Dispatched as an
    // event rather than calling renderUrlsInPanel directly, because that callback is
    // declared later in this component (a direct dep would hit its TDZ during render).
    if (event.type === 'open-tab') {
      const open = event.data as OpenTabActionData | undefined;
      if (open?.url) {
        window.dispatchEvent(new CustomEvent('lab:render-urls', {
          detail: { urls: [open.url], label: open.label || 'Video', sessionId: activeSessionRef.current },
        }));
      }
      return;
    }
    if (event.type !== 'action') return;
    const data = event.data as (TimelineActionData & Partial<BatchClipActionData>) | undefined;
    const clean = data?.prompt;
    if (!clean) return;
    // The bubble shows the CLEAN action text; the agent gets the source URL(s) appended (it
    // needs them to target the right clip). Keeping URLs out of `displayText` stops the
    // CloudFront path from showing as chat dirty laundry.
    let agentPrompt = clean;
    const batchClips = data?.clips;
    if (Array.isArray(batchClips) && batchClips.length > 0) {
      // Batch clip/trim/extract across INDEPENDENT clips: thread each clip's own source,
      // labeled, so the agent can fan out one op per file and never confuse which is which.
      // A frame-extract action also carries each clip's capture point (its mark-in).
      agentPrompt = `${clean} [sources: ${batchSourcesLine(batchClips)}]`;
    } else if (data?.src) {
      agentPrompt = `${clean} [source: ${data.src}]`;
    }
    const sendToAgent = () => window.dispatchEvent(new CustomEvent('chat:send', {
      detail: { prompt: agentPrompt, displayText: clean, sessionId: activeSessionRef.current },
    }));
    // Frame / Clip / Crop on ONE file of the chat's project run DIRECTLY (no model turn): the proxy runs them in
    // the chat's warm sandbox and the chat gets an action line. Anything it refuses goes to the agent as before.
    routeTimelineAction(data as TimelineActionLike, activeSessionRef.current, {
      projectOf: (sid) => getSessionProject(sid)?.activeProjectId,
      sendToAgent,
      openOutputs: openActionOutputs,
      // A brand-new chat has no row until its first turn; the action names the chat, as a first message would.
      prepare: (sid) => persistSessionForAction(sid, (s) => createSession(s, clean.slice(0, 80))),
    });
  }, []);

  // `project` seeds the new session's Active_Project (the Projects surface's `New chat`); without it the
  // session starts with none and the first turn falls back to Personal as before.
  const startNewSession = useCallback((project?: { id: string; name: string }) => {
    const newId = crypto.randomUUID?.() || `session-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
    const newRow: WorkspaceSession = { id: newId, title: 'New Session', timestamp: Date.now() };
    // Replace the prior active row when it was also ephemeral. Persisted rows
    // remain in history, while repeated empty New clicks never accumulate.
    setSessions(prev => prependEphemeralSession(
      prev,
      activeSession,
      newRow,
      retainedSessionIdsRef.current,
    ));
    setActiveSession(newId);
    void seedSessionProject(newId, project
      ? { persisted: false, activeProjectId: project.id, activeProjectName: project.name }
      : { persisted: false });
    setRenderHistory([]);
    setRenderIndex(0);
    setCenterMode('chat');
    switchSession(newId);
  }, [activeSession, switchSession]);
  const handleNewSession = useCallback(() => startNewSession(), [startNewSession]);
  const handleNewChatInProject = useCallback(
    (projectId: string, projectName: string) => startNewSession({ id: projectId, name: projectName }),
    [startNewSession],
  );
  // The Plus_Menu's "Start a new chat in <project>" on a session whose project a turn fixed (asset-lineage D1).
  useEffect(
    () => onNewChatInProjectRequest(({ projectId, projectName }) => handleNewChatInProject(projectId, projectName)),
    [handleNewChatInProject],
  );

  // Persist render history — localStorage (immediate) + DynamoDB (debounced 2s)
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (renderHistory.length > 0) {
      try { setRenderCache(activeSession, JSON.stringify(renderHistory)); } catch { /* ignore */ }
      if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
      saveTimerRef.current = setTimeout(() => {
        updateSessionRenderHistory(activeSession, renderHistory).catch((e) => logPersistFailure('session renderHistory', e));
      }, 2000);
    }
    return () => { if (saveTimerRef.current) clearTimeout(saveTimerRef.current); };
  }, [renderHistory, activeSession]);

  const handleGoHome = useCallback(() => {
    setCenterMode('chat');
  }, []);

  // Publish the active session so the implicit auto-render can decide whether
  // to drive the live chat stream (active session) or persist in the
  // background (a session you're not viewing).
  useEffect(() => { activeSessionRef.current = activeSession; setActiveSessionId(activeSession); }, [activeSession]);

  // Live-refresh the renderer when a background auto-render (implicit
  // render-on-job-completion) writes into the session currently on screen.
  // The render was already persisted to localStorage + DynamoDB by
  // autoRenderOnComplete; here we just re-read it so the panel updates without
  // a manual session re-select.
  useEffect(() => {
    const handler = (e: Event) => {
      const ce = e as CustomEvent<{ sessionId?: string }>;
      if (ce.detail?.sessionId !== activeSession) return;
      try {
        const stored = localStorage.getItem(`lab-render-${activeSession}`);
        const hist = stored ? (JSON.parse(stored) as RenderHistoryEntry[]) : [];
        setRenderHistory(hist);
        setRenderIndex(hist.length > 0 ? hist.length - 1 : 0);
        if (hist.length > 0 && rightCollapsed) {
          setRightCollapsed(false);
          localStorage.setItem(RIGHT_COLLAPSED_KEY, 'false');
        }
      } catch { /* ignore */ }
    };
    window.addEventListener('session-render-updated', handler as EventListener);
    return () => window.removeEventListener('session-render-updated', handler as EventListener);
  }, [activeSession, rightCollapsed]);

  // The Lineage view's "Open chat": that chat, scrolled to the turn that made the file (its `<turnId>-u` message).
  const handleSelectSessionRef = useRef<((id: string) => void) | null>(null);
  const handleOpenChatAtTurn = useCallback((id: string, turnId?: string) => {
    handleSelectSessionRef.current?.(id);
    if (!turnId) return;
    let tries = 0;
    const tick = () => {
      const el = document.querySelector(`[data-message-id="${CSS.escape(`${turnId}-u`)}"]`)
        ?? document.querySelector(`[data-message-id="${CSS.escape(`${turnId}-a`)}"]`);
      if (el) { el.scrollIntoView({ block: 'center' }); return; }
      if (++tries < 40) window.setTimeout(tick, 150);
    };
    window.setTimeout(tick, 150);
  }, []);

  const handleSelectSession = useCallback((id: string) => {
    setActiveSession(id);
    switchSession(id);
    setCenterMode('chat');
    // Compute the cached history (DynamoDB session row → localStorage → legacy scan),
    // drop any stale 'composing' placeholder, then merge the S3-authoritative manifests
    // on top. Switching a session used to load ONLY the cache (which under-counts async
    // renders), which is why a hard refresh "found" the missing tabs.
    let cached: RenderHistoryEntry[] = [];
    const sessionData = sessions.find(s => s.id === id);
    if (sessionData && (sessionData as any).renderHistory?.length > 0) {
      cached = (sessionData as any).renderHistory as RenderHistoryEntry[];
    } else {
      try {
        const stored = localStorage.getItem(`lab-render-${id}`);
        cached = stored ? (JSON.parse(stored) as RenderHistoryEntry[]) : [];
        if (cached.length === 0) {
          const msgs = localStorage.getItem(`lab-session-${id}`);
          if (msgs) {
            const parsed = JSON.parse(msgs);
            if (Array.isArray(parsed)) {
              cached = scanMessagesForArtifacts(parsed);
              if (cached.length > 0) setRenderCache(id, JSON.stringify(cached));
            }
          }
        }
      } catch { cached = []; }
    }
    cached = cached.filter(e => !shouldDropComposingEntry(e));
    setRenderHistory(cached);
    setRenderIndex(cached.length > 0 ? cached.length - 1 : 0);
    getSessionRenders(id).then((serverRenders) => {
      if (activeSessionRef.current !== id) return;  // user switched away mid-fetch
      applyServerRenders(serverRenders);
    }).catch(() => { /* keep cached (already applied) */ });
  }, [switchSession, sessions]);
  handleSelectSessionRef.current = handleSelectSession;

  const handleSessionInputStarted = useCallback((id: string) => {
    retainedSessionIdsRef.current.add(id);
    // New turn → reset auto-follow so this turn's first composing tab is selected,
    // and sweep any composing placeholder the PREVIOUS turn left unfilled (spurious
    // — that turn produced nothing renderable for it). Done at turn start rather
    // than turn end to avoid racing the render event that fills a tab.
    autoFollowConsumedRef.current = false;
    userPinnedRef.current = false;
    setRenderHistory(prev => prev.some(shouldDropComposingEntry)
      ? prev.filter(e => !shouldDropComposingEntry(e)) : prev);
  }, []);

  // Open the session a notification came from, then fetch its results as a
  // LIVE chat turn: the assistant response lands in the transcript (with
  // downloadable artifact links) AND the render hook fires, opening the
  // renderer on the result. Failures / artifact-less successes just surface
  // the session. The dispatch is deferred a tick so the session switch settles
  // and ChatCanvas re-subscribes `chat:auto-render` for the now-active session.
  // Notification click: open the originating session and surface ONE TAB PER
  // RESULT in the Renderer. For a DAG that means one tab per node that produced
  // artifacts, so the whole run's results are reachable from a single nudge.
  //
  // Tabs are metadata only — the manifest for a tab is composed by the Render
  // Agent on first click (see onRenderTab). Rendering all of them up front fired
  // the Render Agent for results nobody opened and measurably slowed dispatch.
  const handleOpenNotification = useCallback((n: JobNotification) => {
    if (n.sessionId) handleSelectSession(n.sessionId);
    if (n.kind !== 'success' && n.kind !== 'failure') return;
    // Notification click is now pure NAVIGATION: open the session (above) and
    // focus the renderer (below). The result tabs come from the S3-authoritative
    // render list (getSessionRenders in handleSelectSession) + the render-artifacts
    // hook — both surface a completed job's tab on their own now that node
    // dispatch is session-correlated, so the results populate without a click.
    //
    // This used to ALSO fire a "retrieve the results and summarise" agent TURN
    // (setPendingPrompt), which re-fetched and re-read work that had already
    // rendered — the redundant double-fetch the user hit. Dropped: the render is
    // the deliverable; a summary is now an explicit ask, not an automatic
    // side effect of opening a notification.
    setRightTab('renderer');
    setRightCollapsed(false);
    localStorage.setItem(RIGHT_COLLAPSED_KEY, 'false');
    // CONTRACT: any async job OR flow → the final-completion notification fetches the result and
    // renders it. Gather the renderable deliverable url(s): the job's own resolved artifactUrls,
    // OR — for a plan/flow whose deliverable lives on a member (e.g. a fal video under a plan) —
    // the members' urls rolled up. Then render DETERMINISTICALLY from them (renderTab composes ONE
    // manifest — no LLM fetch/presign loop) + a brief TEXT-ONLY summary. Only when nothing
    // renderable is resolvable (rare) do we fall back to a last-resort LLM fetch+feature turn.
    // Frozen session: navigation above (open session + focus renderer) is fine, but do NOT fire a
    // results/summary agent TURN (work) for a locked session. The ChatCanvas listener drops it too
    // and the server 423s the turn — this is the source-side gate so the fetch never even attempts.
    const targetLocked = sessions.find(s => s.id === n.sessionId)?.locked ?? false;
    if (n.kind === 'success' && n.sessionId && !targetLocked) {
      // #1: address the result by job id. If a render tab for this job already exists (results are
      // rendered) FOCUS it instead of firing another agent turn; dedup rapid repeat clicks; else
      // fetch — the same branch serves the first click AND a retry after a failed/empty fetch that
      // left no tab (that is the recovery path; the Jobs-panel Results button re-fetches too).
      const action = decideResultsAction({
        jobId: n.jobId,
        renderHistory: n.sessionId === activeSessionRef.current ? renderHistoryRef.current : [],
        lastFetchAt: lastResultsFetchRef.current.get(n.jobId),
        now: Date.now(),
      });
      if (action.kind === 'focus') {
        userPinnedRef.current = true;
        setRenderIndex(action.index);
      } else if (action.kind === 'fetch') {
        lastResultsFetchRef.current.set(n.jobId, Date.now());
        const urls = rollupRenderUrls(n, Array.from(jobsStore.getSnapshot().jobsById.values()));
        if (urls.length) {
          // Tab comes from the server render list (handleSelectSession above) + the reconcile/hook — do
          // NOT also author a client URL tab here (lab:render-urls). That appended a duplicate
          // 'composing' tab which the reconcile then removed: the notification-click tab flicker the
          // user reported. This stays pure navigation + a text-only summary.
          const { prompt: summary, displayText } = summaryNotificationTurn(n.jobId, n.label);
          requestAnimationFrame(() => window.dispatchEvent(
            new CustomEvent('chat:auto-render', { detail: { prompt: summary, displayText, sessionId: n.sessionId } }),
          ));
        } else {
          const prompt = `The ${n.label || 'requested'} job/flow completed (${n.jobId}). Fetch its results, give me a brief summary, and feature the artifacts.`;
          requestAnimationFrame(() => window.dispatchEvent(
            new CustomEvent('chat:auto-render', { detail: { prompt, displayText: `Show the ${n.label || 'job'} results`, sessionId: n.sessionId } }),
          ));
        }
      }
      // action.kind === 'suppress' → in-flight dedup: a fetch was just dispatched; do nothing.
    } else if (n.kind === 'failure' && n.sessionId && !targetLocked) {
      // Opening a FAILED notification hands the agent a "why did this fail" turn — the same
      // get_job → explain → (recover) flow that works when asked by hand — so it surfaces the
      // reason + job id and attempts a fix, instead of doing nothing (the reported blank click).
      const turn = failedNotificationTurn(n.jobId, n.label);
      requestAnimationFrame(() => window.dispatchEvent(
        new CustomEvent('chat:auto-render', { detail: { prompt: turn.prompt, displayText: turn.displayText, sessionId: n.sessionId } }),
      ));
    }
  }, [handleSelectSession, sessions]);

  // A Moment card's Clip: its job's result opens like a bell click, in the session on screen only.
  useMomentClipAutoOpen(activeSession, handleOpenNotification);

  // Kick off the deferred first-tab render once the new tabs are committed. Doing
  // this in an effect (rather than inside the state updater) keeps the side effect
  // out of a function React may invoke more than once.
  // A QUEUE, not a single index: a multi-video upload fires one `lab:render-media` per
  // file in the same tick, and React batches those updates — a single slot kept only the
  // last tab's index, so only the last video composed and the others sat at 'composing'
  // forever (session 41b19dd4). Deduped on drain because the enqueue happens inside a
  // state updater React may invoke twice. Queued by tab KEY, not index (R8.3): by the time the
  // compose settles the list may have reordered, and an index would fill whichever tab moved there.
  const [pendingRenderTabs, setPendingRenderTabs] = useState<string[]>([]);
  useEffect(() => {
    if (pendingRenderTabs.length === 0) return;
    const keys = [...new Set(pendingRenderTabs)];
    setPendingRenderTabs([]);
    for (const key of keys) handleRenderTabRef.current?.(key);
  }, [pendingRenderTabs]);

  // Compose the manifest for a tab the user just opened, then cache it in place.
  const handleRenderTabRef = useRef<((key: string) => void) | null>(null);
  const composeInFlightRef = useRef(0);
  const handleRenderTab = useCallback((tabKey: string) => {
    setRenderHistory((prev) => {
      const entry = prev.find(e => renderTabKey(e) === tabKey);
      if (!entry || entry.blocks.length > 0 || !entry.artifactUrls?.length) return prev;
      const urls = entry.artifactUrls;
      const label = entry.label ?? 'Result';
      if (composingTabKeysRef.current.has(tabKey)) return prev; // already composing
      composingTabKeysRef.current.add(tabKey);
      composeInFlightRef.current += 1;
      setRenderPending(true);
      void renderTabOutcome(urls, activeSession).then((outcome) => {
        composingTabKeysRef.current.delete(tabKey);
        composeInFlightRef.current = Math.max(0, composeInFlightRef.current - 1);
        // Several tabs can compose at once (multi-video upload): clear the pending
        // indicator only when the LAST one settles.
        if (composeInFlightRef.current === 0) setRenderPending(false);
        if (outcome.kind === 'no_view' || outcome.kind === 'failed') {
          // Definitive: nothing to show. Remove the placeholder instead of leaving "Composing…" forever
          // (a2d759c2: a data-only result became a tab, then an error record filled it).
          setRenderHistory((cur) => cur.filter((e) => renderTabKey(e) !== tabKey));
          return;
        }
        if (outcome.kind === 'unavailable') {
          // Transport failure: keep the tab but make it clickable again (a composing tab is not).
          setRenderHistory((cur) => cur.map((e) => (
            renderTabKey(e) === tabKey && e.status === 'composing' ? { ...e, status: undefined } : e)));
          return;
        }
        const manifestUrl = outcome.manifestUrl;
        // Fill THE tab that asked (by key), and adopt the manifest key as its identity so the
        // server's listing of that manifest supersedes it in place (same key → no remount).
        setRenderHistory((cur) => cur.map((e) => (
          renderTabKey(e) === tabKey
            ? { ...e, key: manifestKeyOfUrl(manifestUrl) || e.key, blocks: [manifestBlock(manifestUrl, label)] }
            : e
        )));
      });
      return prev;
    });
  }, [activeSession]);
  handleRenderTabRef.current = handleRenderTab;

  // Auto-render an uploaded SOURCE video into the panel (deterministic) — the user
  // should see the editing timeline the moment they upload, without having to ask,
  // and independent of whatever the accompanying chat request was (metadata, etc.).
  // ChatCanvas fires 'lab:render-media' on a video upload; we add a composing tab
  // for that URL and compose it via the same lazy-tab path. Deduped by URL.
  // Deterministically render one or more resolved artifact URLs into the panel as a
  // composing tab (the existing lazy-tab effect composes it via renderTab — ONE
  // render-agent pass from the URLs, no per-output re-fetch/presign loop). Used by
  // upload AND notification-click so async results render reliably without the
  // 60-step "fetch and feature" turn that stalled. Deduped by URL; session-scoped.
  const renderUrlsInPanel = useCallback((urls: string[], label: string, sessionId?: string) => {
    const clean = (urls || [])
      .filter(u => typeof u === 'string' && /^https?:\/\//.test(u))
      // Drop non-displayable artifacts (kind 'link' — e.g. raw .json data/plumbing). Rendering a
      // lone json opens a generic file-viewer tab, and the render agent now declines JSON-only
      // input, which would otherwise leave an empty "composing" tab spinning forever.
      .filter(u => classifyUrl(u).kind !== 'link');
    if (!clean.length || (sessionId && sessionId !== activeSession)) return;
    setRightTab('renderer');
    setRightCollapsed(false);
    localStorage.setItem(RIGHT_COLLAPSED_KEY, 'false');
    setRenderHistory(prev => {
      if (prev.some(e => e.artifactUrls?.some(u => clean.includes(u)))) return prev; // already have a tab for these
      const key = `client:${newClientId()}`;
      const entry: RenderHistoryEntry = {
        turnIndex: prev.length, blocks: [], timestamp: Date.now(),
        label, artifactUrls: clean, status: 'composing', key,
      };
      const next = [...prev, entry];
      setRenderIndex(next.length - 1);
      setPendingRenderTabs(q => (q.includes(key) ? q : [...q, key])); // effect composes it via renderTab
      return next;
    });
  }, [activeSession]);
  const handleUploadedMedia = useCallback((detail: { url?: string; name?: string; sessionId?: string }) => {
    const { url, name, sessionId } = detail || {};
    if (!url) return;
    renderUrlsInPanel([url], name || 'Video', sessionId);
  }, [renderUrlsInPanel]);

  // Open a verified Citation (Component 5) or a Moment's Expand (Component 7) as its own
  // asset-player tab. `openAssetAt` has already read the asset status, minted a URL, and
  // preflight-loaded the media; this only paints the tab. The block carries an Asset_Reference
  // and NO URL (Req 2.12) — AssetPlayer re-mints from `assetUrlCache` on mount — so unlike
  // `renderUrlsInPanel` it stores no `artifactUrls` and never calls `renderTab`/the render agent
  // (Req 2.8). Every activation opens a new tab (Req 2.8, 2.11).
  //
  // The view is PERSISTED (R8.1): its `asset-player` manifest is POSTed to the session's render
  // store, so the server lists it like any render and it survives a refresh and every later
  // reconcile (it used to be a client-only tab with no server key, which the next merge dropped).
  // The manifest id is chosen here, so the tab's key — the manifest key the server returns — is
  // known up front and the tab never changes identity. Until the POST lands the tab is `clientOnly`
  // (kept by every merge until the server lists it); once the save lands the block gains its
  // `manifestUrl`, which is what lets the timeline save its range / frame marker / crop box (R8.2).
  // Paint a client-built view as a tab now and persist its manifest (R8.1); shared by the asset player and the
  // result galleries. The key is predicted from the manifest id, so the tab never changes identity.
  const openClientViewTab = useCallback((manifest: Record<string, unknown> & { id: string }, label: string) => {
    setRightTab('renderer');
    setRightCollapsed(false);
    localStorage.setItem(RIGHT_COLLAPSED_KEY, 'false');
    const sid = activeSession;
    const predictedKey = sid ? `artifacts/${sid}/render/${manifest.id}.json` : `client:${manifest.id}`;
    setRenderHistory(prev => {
      const entry: RenderHistoryEntry = {
        turnIndex: prev.length,
        blocks: [{ template: 'manifest-router', title: label, data: { manifest: manifest as unknown as RenderManifest } }],
        timestamp: Date.now(),
        label,
        status: 'ready',
        key: predictedKey,
        clientOnly: true,
      };
      const next = [...prev, entry];
      setRenderIndex(next.length - 1);
      return next;
    });
    if (!sid) return;
    // A brand-new chat has no row yet: save it (with its project) first, or the view's POST is a 404 and the tab
    // is lost on refresh (staging 2026-10-21, same root cause as a new chat's first timeline action).
    persistSessionForAction(sid, (s) => createSession(s, label.slice(0, 80))).catch(() => undefined)
      .then(() => createSessionRender(sid, manifest)).then((savedKey) => {
      if (activeSessionRef.current !== sid) return;
      // Closed before the save landed: the tab is gone, so remove the manifest the save created.
      if (removedTabKeysRef.current.has(predictedKey)) {
        deleteSessionRender(sid, savedKey).catch(() => {});
        return;
      }
      // Saved: the block gains its manifestUrl (so the timeline can save its state). The tab stays
      // `clientOnly` until a server list includes it — a list fetched before the save landed must
      // not drop it — and the first merge that lists it replaces it in place (same key, same block).
      const manifestUrl = `/api/artifact?key=${encodeURIComponent(savedKey)}`;
      setRenderHistory(prev => prev.map(e => {
        if (renderTabKey(e) !== predictedKey) return e;
        const block = e.blocks[0];
        return { ...e, key: savedKey, blocks: [{ ...block, data: { ...block.data, manifestUrl } }] };
      }));
    }).catch((err) => {
      // Not saved (a session with no row yet, a locked session, a transient failure): the tab stays
      // a client-only view — still shown, and still kept by every reconcile in this browser.
      logPersistFailure('render view', err);
    });
  }, [activeSession]);

  const openAssetInPanel = useCallback((detail: OpenAssetEventDetail) => {
    const { ref, kind, displayName, startMs, sessionId, range } = detail || ({} as OpenAssetEventDetail);
    if (!ref || !displayName || (sessionId && sessionId !== activeSession)) return;
    // A Moment's Expand carries the card's In/Out: the FULL asset opens with that span selected (the
    // persisted `viewState.range`, restored by the timeline once the media length is known) — the
    // video is never trimmed to the Moment. A Citation carries no range and opens unselected.
    const viewState = normalizeViewState(range ? { range } : undefined);
    // A document tab names its page: two pages of one report would otherwise be two identical tabs.
    const label = kind === 'document' ? `${displayName} · p. ${pageOfSpan(startMs)}` : displayName;
    openClientViewTab({
      id: `view-${newClientId()}`,
      template: 'asset-player',
      createdAt: new Date().toISOString(),
      title: label,
      ref, kind, displayName, startMs, origin: viewState ? 'moment' : 'citation',
      ...(viewState ? { viewState } : {}),
    }, label);
  }, [activeSession, openClientViewTab]);

  // Several results of one action (3 clips, 8 frames) open as ONE gallery tab: one tab appears and takes focus
  // once, instead of a tab popping up per result (and a frame set showing only its first still).
  const openResultGallery = useCallback((detail: ResultGalleryDetail) => {
    const { items, label, kind, sessionId } = detail || ({} as ResultGalleryDetail);
    if (!Array.isArray(items) || items.length === 0 || (sessionId && sessionId !== activeSession)) return;
    openClientViewTab(resultGalleryManifest({ items, label, kind, sessionId }, `view-${newClientId()}`, new Date().toISOString()), label);
  }, [activeSession, openClientViewTab]);

  useEffect(() => {
    const onMedia = (e: Event) => handleUploadedMedia((e as CustomEvent).detail);
    const onUrls = (e: Event) => {
      const d = (e as CustomEvent).detail || {};
      renderUrlsInPanel(d.urls || [], d.label || 'Result', d.sessionId);
    };
    const onOpenAsset = (e: Event) => openAssetInPanel((e as CustomEvent).detail as OpenAssetEventDetail);
    const onGallery = (e: Event) => openResultGallery((e as CustomEvent).detail as ResultGalleryDetail);
    window.addEventListener(RESULT_GALLERY_EVENT, onGallery as EventListener);
    window.addEventListener('lab:render-media', onMedia as EventListener);
    window.addEventListener('lab:render-urls', onUrls as EventListener);
    window.addEventListener(OPEN_ASSET_EVENT, onOpenAsset as EventListener);
    return () => {
      window.removeEventListener('lab:render-media', onMedia as EventListener);
      window.removeEventListener('lab:render-urls', onUrls as EventListener);
      window.removeEventListener(OPEN_ASSET_EVENT, onOpenAsset as EventListener);
      window.removeEventListener(RESULT_GALLERY_EVENT, onGallery as EventListener);
    };
  }, [handleUploadedMedia, renderUrlsInPanel, openAssetInPanel, openResultGallery]);

  // Remove a render tab (each generation adds one; let the user prune extras —
  // they can always reprompt for more). Prunes history, keeps the selected index
  // sensible, and persists the pruned list.
  // Removal is by the tab's stable KEY (R8.3): an index can point at a different tab once the list
  // has reordered — and this also DELETEs the manifest, so a stale index removed the wrong render
  // permanently.
  const handleRemoveRenderTab = useCallback((tabKey: string) => {
    removedTabKeysRef.current.add(tabKey);
    setRenderHistory(prev => {
      const idx = prev.findIndex(e => renderTabKey(e) === tabKey);
      if (idx < 0) return prev;
      const removed = prev[idx];
      const next = prev.filter((_, i) => i !== idx);
      try { setRenderCache(activeSession, JSON.stringify(next)); } catch { /* ignore */ }
      updateSessionRenderHistory(activeSession, next).catch((e) => logPersistFailure('session renderHistory', e));
      // Delete the AUTHORITATIVE S3 manifest so the tab does not reappear on the next load /
      // session switch (getSessionRenders re-lists the manifests and mergeServerRenders unions
      // them back in). The manifest key lives on the tab's manifest-router block as
      // /api/artifact?key=<key>; legacy/composing tabs without one are local-only, no server delete.
      // An Expand view whose save is still in flight has no manifestUrl yet; its save deletes what
      // it creates (removedTabKeysRef).
      const key = entryManifestKey(removed);
      if (key && activeSession) deleteSessionRender(activeSession, key).catch(() => {});
      // Removing a tab before the current one shifts selection left; removing the
      // current/after is handled by the panel's Math.min clamp on renderIndex.
      setRenderIndex(cur => (idx < cur ? Math.max(0, cur - 1) : cur));
      return next;
    });
  }, [activeSession]);

  const handleSessionPersisted = useCallback((id: string, title?: string) => {
    if (deletedSessionIdsRef.current.delete(id)) {
      deleteSessionApi(id).catch(() => {});
      return;
    }
    retainedSessionIdsRef.current.add(id);
    persistedSessionIdsRef.current.add(id);
    setSessions(prev => prev.map(session =>
      session.id === id
        // Do NOT bump timestamp here — the row is ordered by creation time and
        // must not jump when its title changes or it becomes persisted.
        ? { ...session, ...(title ? { title } : {}) }
        : session,
    ));
  }, []);
  const handleOpenWorkflows = useCallback(() => {
    setCenterMode('activity');
    // Leaving a target set would re-open the editor instead of the run list.
    setEditingTarget(null);
  }, [setEditingTarget]);

  /**
   * "New workflow" — open the workflow editor on a blank draft.
   *
   * `handleOpenWorkflows` only switches center mode, so binding the New button
   * to it made the button a no-op for anyone already on the Workflow tab. The
   * editor itself (CenterEditor → WorkflowEditor → createWorkflowDraft) existed
   * the whole time; nothing routed to it.
   */
  const handleNewWorkflow = useCallback(() => {
    setCenterMode('activity');
    setEditingTarget({ category: 'workflow', assetId: null });
  }, [setEditingTarget]);

  const handleSelectRun = useCallback((runId: string) => {
    setActiveRunId(runId);
    setCenterMode('activity');
  }, []);

  // Agents nav → open the Custom Agent center mode and show the list
  // (Req 1.1). Selecting the nav closes any open editor so the Owner lands
  // on the list, mirroring how Chat/Workflows navigate.
  const handleOpenCustomAgents = useCallback(() => {
    setCenterMode('custom-agent');
    setCustomAgentEditorOpen(false);
    setEditingCustomAgentId(null);
    // Category loading is LAZY (staging replaced the eager load with
    // `ensureCategoryLoaded`), so the Agents center must pull its own
    // inventory — otherwise it renders "No custom agents yet" even when the
    // Owner has published agents.
    void ensureCategoryLoaded('specialty-agent');
  }, [ensureCategoryLoaded]);

  // Skills centre — peer to Agents. Loading is LAZY (`ensureCategoryLoaded`),
  // so this must pull its own inventory or the view renders "no skills yet"
  // over assets the Owner actually has — the same bug the Agents centre hit.
  const handleOpenSkills = useCallback(() => {
    setCenterMode('skills');
    setEditingTarget(null);
    void ensureCategoryLoaded('skill');
  }, [ensureCategoryLoaded, setEditingTarget]);

  // Projects centre (media-harness-projects-access task 13.8). Clears `editingTarget` as the other
  // centres do, so no stale target forces chat mode back on (see HOSTS_OWN_EDITOR). `opts.select` sets the
  // Selected_Project, which `Manage projects` uses to override the surface's last-shown Project (Req 3.5).
  const handleOpenProjects = useCallback((opts?: { select?: string }) => {
    setCenterMode('projects');
    setEditingTarget(null);
    if (opts?.select) persistProjectsSelected(opts.select);
  }, [setEditingTarget, persistProjectsSelected]);
  // "Open in project graph" on a chat graph result: switch to Projects, select the project, show its Graph tab
  // with those nodes highlighted. The nonce re-applies the focus when the same nodes are opened twice.
  const [projectsGraphFocus, setProjectsGraphFocus] = useState<{ projectId: string; nodeIds: string[]; nonce: number } | null>(null);
  const [projectsFolderFocus, setProjectsFolderFocus] = useState<{ projectId: string; folder: string; nonce: number } | null>(null);
  useEffect(() => {
    const onOpen = (e: Event) => {
      const d = (e as CustomEvent<{ projectId?: unknown; nodeIds?: unknown }>).detail;
      if (!d || typeof d.projectId !== 'string' || !d.projectId) return;
      const nodeIds = Array.isArray(d.nodeIds) ? d.nodeIds.filter((x): x is string => typeof x === 'string').slice(0, 200) : [];
      handleOpenProjects({ select: d.projectId });
      setProjectsGraphFocus({ projectId: d.projectId, nodeIds, nonce: Date.now() });
    };
    window.addEventListener(OPEN_PROJECT_GRAPH_EVENT, onOpen);
    return () => window.removeEventListener(OPEN_PROJECT_GRAPH_EVENT, onOpen);
  }, [handleOpenProjects]);
  // "Open in Projects" on a chat project-browser tab: the Projects surface with that Project selected.
  useEffect(() => {
    const onOpen = (e: Event) => {
      const d = (e as CustomEvent<{ projectId?: unknown; folder?: unknown }>).detail;
      if (!d || typeof d.projectId !== 'string' || !d.projectId) return;
      handleOpenProjects({ select: d.projectId });
      // A file's location was clicked: open that folder in the Assets grid.
      if (typeof d.folder === 'string') setProjectsFolderFocus({ projectId: d.projectId, folder: d.folder, nonce: Date.now() });
    };
    window.addEventListener(OPEN_PROJECT_EVENT, onOpen);
    return () => window.removeEventListener(OPEN_PROJECT_EVENT, onOpen);
  }, [handleOpenProjects]);
  const handleNewProject = useCallback(() => {
    handleOpenProjects();
    setProjectsCreateRequest((n) => n + 1);
  }, [handleOpenProjects]);

  // Improve centre — read-only, and deliberately does NOT call
  // `ensureCategoryLoaded`: it reads scored runs and suggestions, not the Lab
  // inventory, so pulling a category here would be a wasted ~7s cold `/lab` call.
  // `editingTarget` is cleared for the same reason the other centres clear it — a
  // target left set would force chat mode straight back on (see HOSTS_OWN_EDITOR).
  // Dot on the sidebar's Improve row (and the Capabilities rail icon when collapsed):
  // is there anything the user can APPLY? Escalation is
  // not improvement, so `canSendToTeam` does not count (see `hasActionableSuggestions`).
  //
  // Reads through the same cached promise `ImproveCenter` uses, so opening the surface does not
  // re-fetch. A failure is swallowed: an absent payload means no dot, which is correct, and the
  // surface itself is where the error belongs.
  const [improveBadge, setImproveBadge] = useState(false);
  useEffect(() => {
    let live = true;
    void loadImproveData()
      .then((data) => { if (live) setImproveBadge(hasActionableSuggestions(data)); })
      .catch(() => { if (live) setImproveBadge(false); });
    return () => { live = false; };
  }, []);

  // Improve lives in the Settings modal as a Capabilities tab, so this navigates there
  // rather than swapping the center pane. Keeps one code path: the sidebar row and the
  // modal's own nav entry land on the same surface.
  const handleOpenImprove = useCallback(() => {
    onOpenSettings('improve');
  }, [onOpenSettings]);

  /*
   * A detail view requested from the sidebar, handed to `SkillsCenter` and dropped
   * the moment it is applied.
   *
   * Cleared on handover rather than held: `SkillsCenter` re-applies this prop on
   * mount, so a target left sitting here reopened a published skill every time the
   * component remounted — clicking the Skills nav, or closing a skill editor, landed
   * on a skill detail instead of the list. `SkillsCenter` owns the live one.
   */
  const [sidebarSkillDetail, setSidebarSkillDetail] = useState<SkillDetailTarget | null>(null);
  const handleSkillDetailOpened = useCallback(() => setSidebarSkillDetail(null), []);

  const handleOpenSkillEditor = useCallback(
    (target: { category: 'skill'; assetId: string | null }) => {
      setCenterMode('skills');
      setEditingTarget(target);
    },
    [setEditingTarget],
  );

  // Shared New_Item_Action in custom-agent mode → open the editor in create
  // mode (assetId = null) so the Owner starts a fresh draft (Req 1.6).
  const handleNewCustomAgent = useCallback(() => {
    setCenterMode('custom-agent');
    setEditingCustomAgentId(null);
    setCustomAgentEditorOpen(true);
  }, []);

  // Selecting an agent row → open the editor for that existing draft
  // (Req 1.5).
  const handleSelectCustomAgent = useCallback((id: string) => {
    setCenterMode('custom-agent');
    setEditingCustomAgentId(id);
    setCustomAgentEditorOpen(true);
  }, []);

  // Delete a WORKING COPY. Only ever offered on a draft row: the underlying
  // action targets the owner's OWNED partition, so calling it for an agent that
  // exists only as a published version would fail — and published versions are
  // immutable, so there is nothing there to delete. Purging a published lineage
  // is a separate, destructive operation that does not exist yet.
  //
  // Deliberately NOT optimistic. Removing the row before the refresh settles
  // re-flows the list under the pointer, and a click can then land on whichever
  // row moves up — which is how a stray Edit forked a published agent during
  // testing. The row keeps itself mounted in a `deleting` state instead.
  const handleDeleteCustomAgent = useCallback(async (id: string) => {
    await deleteSpecialtyAgentDraft(id);
    await refreshCategory('specialty-agent');
  }, [refreshCategory]);

  // Publish a working copy straight from its row. Same call the editor's Publish
  // makes (`SpecialtyAgentEditor.handlePublish`), so both paths mint a version
  // identically; the refresh then moves the row to its published state.
  const handlePublishCustomAgent = useCallback(async (id: string) => {
    await publishAsset('specialty-agent', id);
    await refreshCategory('specialty-agent');
  }, [refreshCategory]);

  // Purge a PUBLISHED lineage — the irreversible one. Unlike every other action
  // on this view it does not go through `/lab`: it is a Capability API
  // control-plane call, so it is unreachable from the agent and from the model.
  const handlePurgedCustomAgent = useCallback(async (result: PurgeResult) => {
    setPurgedIds(prev => new Set(prev).add(result.assetId));
    setPurgeTarget(null);
    // A purged lineage may have been the one open in the editor or the read-only
    // view; leaving either mounted would show a version that no longer exists.
    setViewingPublished(prev => (prev?.assetId === result.assetId ? null : prev));
    setEditingCustomAgentId(prev => (prev === result.assetId ? null : prev));
    await refreshCategory('specialty-agent');
  }, [refreshCategory]);

  // Selecting a PUBLISHED agent row → open a READ-ONLY detail view (no fork, no
  // state change). The user must click Edit there to fork into a working copy.
  const handleViewPublishedCustomAgent = useCallback((summary: PublishedAssetSummary) => {
    setCenterMode('custom-agent');
    setCustomAgentEditorOpen(false);
    setEditingCustomAgentId(null);
    setViewingPublished(summary);
  }, []);

  // Edit from the read-only view → NOW fork the published version into a fresh
  // OWNED working copy (parity with Skills/Tools), refresh drafts, then open the
  // editor on the new working copy.
  const handleEditPublishedCustomAgent = useCallback(async (id: string) => {
    setForkingPublished(true);
    try {
      const wc = await editAsset('specialty-agent', id);
      await refreshCategory('specialty-agent');
      // `EditAsset` reports an already-existing working copy as a 200 with an
      // `error` field rather than throwing, so `assetId` can be absent — which
      // would otherwise open the editor on `undefined` and render the same blank
      // "Agent not found" form this path exists to avoid. The working copy
      // reuses the lineage id (`_lifecycle_ops.edit_asset` keys the OWNED row on
      // the same `asset_id`), so that id is the right target either way.
      const forked = Boolean(wc?.assetId);
      const workingCopyId = wc?.assetId ?? id;
      // Only auto-discard on close when THIS click created the working copy.
      // Marking a pre-existing draft as "just forked" would delete edits the
      // user made earlier merely because they opened and closed the editor.
      justForkedIdRef.current = forked ? workingCopyId : null;
      setViewingPublished(null);
      setCenterMode('custom-agent');
      setEditingCustomAgentId(workingCopyId);
      setCustomAgentEditorOpen(true);
    } finally {
      setForkingPublished(false);
    }
  }, [refreshCategory]);

  // Editor close → return to the list. If this session was a fork-to-edit of a
  // published agent and the user did NOT save/publish, discard the working copy
  // so it reverts to Published-only (don't keep a draft unless saved).
  const handleCloseCustomAgentEditor = useCallback(async (saved: boolean) => {
    const forkedId = justForkedIdRef.current;
    justForkedIdRef.current = null;
    setCustomAgentEditorOpen(false);
    setEditingCustomAgentId(null);
    if (forkedId && !saved) {
      try {
        await discardChanges('specialty-agent', forkedId);
        await refreshCategory('specialty-agent');
      } catch {
        /* non-fatal: a lingering working copy is harmless; leave it */
      }
    }
  }, [refreshCategory]);

  const handleDeleteSession = useCallback((id: string) => {
    setSessions(prev => prev.filter(s => s.id !== id));
    try {
      localStorage.removeItem(`lab-session-${id}`);
      localStorage.removeItem(`lab-render-${id}`);
    } catch { /* ignore */ }
    retainedSessionIdsRef.current.delete(id);
    if (persistedSessionIdsRef.current.delete(id)) {
      deleteSessionApi(id).catch((e) => logPersistFailure('session delete', e));
    } else {
      deletedSessionIdsRef.current.add(id);
    }
    // Deleting the open chat moves to a new one. It used to stay open, still named in `lab-active-session`,
    // so a refresh put the deleted session back at the top of the list with its transcript.
    if (id === activeSessionRef.current) startNewSession();
  }, [startNewSession]);

  const handleRenameSession = useCallback((id: string, newTitle: string) => {
    let snapshot: WorkspaceSession[] = [];
    setSessions(prev => {
      snapshot = prev;
      return prev.map(s => (s.id === id ? { ...s, title: newTitle } : s));
    });
    // Renaming an empty local workspace remains local; its chosen title is
    // supplied to ChatCanvas when the first input eventually persists it.
    if (!persistedSessionIdsRef.current.has(id)) return;
    updateSessionActivity(id, newTitle).catch(() => {
      setSessions(snapshot);
    });
  }, []);

  const handleToggleLock = useCallback((id: string, locked: boolean) => {
    let snapshot: WorkspaceSession[] = [];
    setSessions(prev => {
      snapshot = prev;
      return prev.map(s => (s.id === id ? { ...s, locked } : s));
    });
    // A not-yet-persisted local session has no DDB row to lock; keep it local
    // (it becomes durable on first input). Only call the API for persisted rows,
    // and revert the optimistic toggle if the server rejects it.
    if (!persistedSessionIdsRef.current.has(id)) return;
    setSessionLock(id, locked).catch(() => {
      setSessions(snapshot);
    });
  }, []);

  // Tracks a single "render pending" episode so a spurious open (render-agent
  // activity that never yields an artifact) doesn't leave the renderer drawer
  // open over an empty panel. See handleRenderPending(false).
  const autoOpenedByPendingRef = useRef(false);
  const pendingGotBlockRef = useRef(false);
  // Auto-follow (tab selection) state, per the situations table. Reset at turn
  // start; autoFollowConsumed enforces "the turn's FIRST composing tab wins";
  // userPinned records a manual tab click so we never yank the user mid-turn.
  const autoFollowConsumedRef = useRef(false);
  const userPinnedRef = useRef(false);

  // Persist a render addressed to a session that is NOT on screen. Mirrors the
  // implicit auto-render persistence (autoRenderOnComplete.persistRenders):
  // merge into that session's localStorage history (cap 50), push to DynamoDB,
  // and keep the in-memory session row in sync so switching to it shows the
  // render immediately (handleSelectSession prefers the sessions-list copy).
  // Deliberately touches NONE of the active view's renderer state — that is the
  // whole point: a background turn's render can never add a tab to the viewed
  // session's Renderer.
  const persistBackgroundRender = useCallback((sid: string, entry: RenderHistoryEntry) => {
    let existing: RenderHistoryEntry[] = [];
    try {
      const raw = localStorage.getItem(`lab-render-${sid}`);
      existing = raw ? (JSON.parse(raw) as RenderHistoryEntry[]) : [];
    } catch { existing = []; }
    const named = nameRenderEntry(existing, entry);
    const merged = [...existing, named].slice(-50);
    try { setRenderCache(sid, JSON.stringify(merged)); } catch { /* quota */ }
    updateSessionRenderHistory(sid, merged).catch((e) => logPersistFailure('session renderHistory', e));
    setSessions(prev => prev.map(s => (s.id === sid ? { ...s, renderHistory: merged } : s)));
    window.dispatchEvent(new CustomEvent('session-render-updated', { detail: { sessionId: sid } }));
  }, []);

  // ── Server-authoritative render tabs ────────────────────────────────────────────────────────
  // Live render tabs are NO LONGER authored client-side. A stream render block only means "a render
  // for this session just landed on S3", so we trigger the SAME server reconcile used on load/refresh
  // (getSessionRenders → composeTimeline composites). The server `/renders` is the SOLE author of tabs
  // — which is exactly why the ~1s tab FLASH, the DAG-fan-out extra tabs, and "stems didn't fold" can
  // no longer happen: there is no transient client tab to race the reconcile. In-flight work shows via
  // the jobs-driven progress strip (SessionRendererPanel `runningCount`) + the render-pending
  // animation; finished composites fold in on the reconcile ladder. The upload SOURCE tab
  // (renderUrlsInPanel/lazy-tab) is intentionally left intact so the source still appears instantly.
  const reconcileTabsRef = useRef<() => void>(() => {});
  const liveReconcileTimersRef = useRef<ReturnType<typeof setTimeout>[]>([]);
  const scheduleLiveReconcile = useCallback(() => {
    // S3 is read-after-write consistent, so the just-written manifest is listable immediately; the
    // widening ladder only covers the brief window where the stream block beats the PUT's visibility.
    for (const d of [0, 500, 1500, 4000, 9000]) {
      liveReconcileTimersRef.current.push(setTimeout(() => reconcileTabsRef.current(), d));
    }
  }, []);

  const handleRenderBlock = useCallback((blocks: RenderHistoryEntry, originatingSessionId?: string) => {
    // Route by the session that ORIGINATED the turn (captured at dispatch time), not the one on
    // screen. A background render persists to its OWN session, never a stray tab in the viewed one.
    if (originatingSessionId && originatingSessionId !== activeSessionRef.current) {
      persistBackgroundRender(originatingSessionId, blocks);
      return;
    }
    // Bring the tab that ends up holding THIS manifest to the front (reconcileTabs). A re-render of a file already
    // in a tab folds into that tab without adding one, so "show me v4" again changed nothing on screen (a5b5acaf).
    const url = (blocks.blocks?.[0]?.data as { manifestUrl?: unknown } | undefined)?.manifestUrl;
    const key = manifestKeyOfUrl(url) || '';
    if (key) focusManifestRef.current = { key, until: Date.now() + 60_000 };
    pendingGotBlockRef.current = true;   // a real artifact arrived this episode
    autoOpenedByPendingRef.current = false;
    setRenderPending(false);
    setRightTab('renderer');
    if (rightCollapsed) {
      setRightCollapsed(false);
      localStorage.setItem(RIGHT_COLLAPSED_KEY, 'false');
    }
    // No client tab is appended — the server re-list composes the authoritative composite (see note).
    scheduleLiveReconcile();
  }, [rightCollapsed, persistBackgroundRender, scheduleLiveReconcile]);

  // Leading "Composing…" marker → open a per-tab placeholder immediately,
  // correlated to the manifest by renderRequestId. The animation now lives in
  // THIS tab's content (never blanking the whole panel), and the manifest fills
  // it in place via handleRenderBlock. Selection follows the auto-follow rule.
  // A leading "Composing…" marker used to open a per-tab spinner placeholder; now that the server
  // authors all tabs, it only ensures the renderer is open and kicks the reconcile (a re-render of an
  // existing source is often already listable). In-flight is shown by the jobs-driven progress strip,
  // not a transient client tab — so composing can't spawn a tab that later has to disappear (churn).
  const handleRenderComposing = useCallback((_renderRequestId: string, _label: string | undefined, originatingSessionId?: string) => {
    if (originatingSessionId && originatingSessionId !== activeSessionRef.current) return;
    setRightTab('renderer');
    if (rightCollapsed) {
      autoOpenedByPendingRef.current = true;
      setRightCollapsed(false);
      localStorage.setItem(RIGHT_COLLAPSED_KEY, 'false');
    }
    scheduleLiveReconcile();
  }, [rightCollapsed, scheduleLiveReconcile]);

  // Terminal for a composing render that produced no manifest (deduped, budget exceeded, empty,
  // error). With no client-authored composing tab to remove, this only resets the pending animation;
  // the server reconcile (or its absence) governs what is shown.
  const handleRenderFailed = useCallback((_renderRequestId: string, originatingSessionId?: string) => {
    if (originatingSessionId && originatingSessionId !== activeSessionRef.current) return;
    setRenderPending(false);
  }, []);

  // SINGLE SOURCE OF TRUTH for the tab set. Re-derive tabs from the session's server manifests via
  // the SAME mergeServerRenders used on mount/session-switch — so "live" produces the identical
  // grouped/composite result as "load". Previously merging (sourceKey grouping) ran ONLY on a
  // full re-list, so an analysis folded onto its source only after a manual refresh; this makes it
  // fold automatically. renderIndex is clamped (never jumped) so the fold doesn't move the user's
  // view — a lane just appears on the source's composite tab.
  const reconcileTabs = useCallback(() => {
    const sid = activeSessionRef.current;
    if (!sid) return;
    getSessionRenders(sid).then((serverRenders) => {
      if (activeSessionRef.current !== sid || !serverRenders.length) return;
      setRenderHistory(prev => {
        const cached = prev.filter(e => !isStaleComposing(e));
        const merged = mergeServerRenders(cached, serverRenders);
        if (merged.length === prev.length
            && merged.every((e, i) => e === prev[i])) {
          const f = focusManifestRef.current;
          const at = f && f.until > Date.now() ? tabIndexHoldingManifest(prev, f.key) : -1;
          if (at >= 0) { focusManifestRef.current = null; setRenderIndex(at); }
          return prev; // no change → no churn
        }
        // #1 auto-follow: when a NEW tab appears (e.g. a trimmed/derived clip the server just
        // composed), SELECT it so it doesn't land silently in the background — unless the user has
        // pinned a tab. On first load (prev empty) keep the default clamp; a growing set mid-session
        // means a fresh deliverable the user asked for. Analyses fold onto the existing source tab
        // (same key, no length change), so this never hijacks the view for a lane update.
        const grewMidSession = merged.length > prev.length && prev.length > 0;
        const focus = focusManifestRef.current;
        const focusAt = focus && focus.until > Date.now() ? tabIndexHoldingManifest(merged, focus.key) : -1;
        if (focus && focus.until <= Date.now()) focusManifestRef.current = null;
        if (focusAt >= 0) {
          // The view this turn just produced: shown even when it folded into an existing tab, and even over a pin.
          focusManifestRef.current = null;
          setRenderIndex(focusAt);
        } else if (grewMidSession && !userPinnedRef.current) {
          setRenderIndex(merged.length - 1);
        } else {
          // Keep the SAME tab selected (by key) when the merge reorders the list around it.
          setRenderIndex(cur => {
            const selected = prev[cur] ? renderTabKey(prev[cur]) : '';
            const at = selected ? merged.findIndex(e => renderTabKey(e) === selected) : -1;
            return at >= 0 ? at : Math.max(0, Math.min(cur, merged.length - 1));
          });
        }
        return merged;
      });
    }).catch(() => { /* transient list failure is non-fatal; next completion retries */ });
  }, []);
  // The live handlers are defined before reconcileTabs, so they invoke it through this ref.
  reconcileTabsRef.current = reconcileTabs;

  // Fire the reconcile when a renderable job for THIS session reaches a terminal state. The source
  // render and analyses land in the session's manifest list at DIFFERENT times (the source render
  // is slow + concurrent), so a single snapshot can miss one. Poll on a widening ladder until the
  // derived tab set stops changing — a late-arriving source render still folds in. Each job triggers
  // the ladder once (seen-set); reconcileTabs is idempotent (no-op when unchanged), so extra passes
  // are free and can't loop. Timers are self-limiting (<=45s) and cleared on unmount.
  const reconcileSeenRef = useRef<Set<string>>(new Set());
  const reconcileTimersRef = useRef<ReturnType<typeof setTimeout>[]>([]);
  useEffect(() => {
    const sid = activeSession;
    if (!sid) return;
    const fresh = jobsForBadge.filter(j =>
      j.sessionId === sid && j.status === 'SUCCEEDED' && j.hasRenderableArtifacts
      && !reconcileSeenRef.current.has(`${sid}:${j.jobId}`));
    if (fresh.length === 0) return;
    fresh.forEach(j => reconcileSeenRef.current.add(`${sid}:${j.jobId}`));
    // Fire IMMEDIATELY on completion so the finished tab renders as fast as the manifest is
    // listable — not after a deliberate debounce. The early retries are only a safety-net for the
    // brief window where the terminal event beats the manifest write; then it backs off.
    for (const d of [0, 400, 1000, 2500, 6000, 15000, 30000]) {
      reconcileTimersRef.current.push(setTimeout(reconcileTabs, d));
    }
  }, [jobsForBadge, activeSession, reconcileTabs]);
  useEffect(() => () => { reconcileTimersRef.current.forEach(clearTimeout); reconcileTimersRef.current = []; liveReconcileTimersRef.current.forEach(clearTimeout); liveReconcileTimersRef.current = []; }, []);

  // True while the Render Agent is composing a view — drives the renderer
  // panel's "Rendering…" animation. Set when render-agent activity appears in
  // the chat stream; cleared when the render block arrives or the turn ends.
  const handleRenderPending = useCallback((pending: boolean, originatingSessionId?: string) => {
    // A render-pending signal from a background session must NOT drive the
    // viewed session's renderer (start the "Rendering…" animation or auto-open
    // the drawer). Its render is persisted into its own session via
    // handleRenderBlock; ignore the pending state here.
    if (originatingSessionId && originatingSessionId !== activeSessionRef.current) return;
    setRenderPending(pending);
    if (pending) {
      pendingGotBlockRef.current = false;
      setRightTab('renderer');
      if (rightCollapsed) {
        autoOpenedByPendingRef.current = true;
        setRightCollapsed(false);
        localStorage.setItem(RIGHT_COLLAPSED_KEY, 'false');
      }
    } else {
      // Turn/render episode settled. Fire one more reconcile ladder so a FINAL render whose stream
      // block raced the turn-close — or whose manifest PUT lagged the ladder kicked from
      // handleRenderBlock — still folds into the gallery, instead of lingering as its own tab until
      // a manual reload ("leaves the last one"). Gated on a block having arrived this episode, and
      // reconcileTabs is idempotent (no-op when the server list is unchanged), so it is cheap.
      if (pendingGotBlockRef.current) scheduleLiveReconcile();
      // Pending resolved. If we auto-opened the drawer this episode but NO
      // render block ever arrived, the turn produced nothing renderable —
      // revert the spurious open so the renderer isn't left showing an empty
      // "No rendered content yet" panel.
      if (shouldRevertSpuriousOpen(autoOpenedByPendingRef.current, pendingGotBlockRef.current)) {
        setRightCollapsed(true);
        localStorage.setItem(RIGHT_COLLAPSED_KEY, 'true');
      }
      autoOpenedByPendingRef.current = false;
    }
  }, [rightCollapsed, scheduleLiveReconcile]);

  const fitted = fitPanelWidths(viewportWidth, leftWidth, rightWidth, sidebarCollapsed, rightCollapsed, COLLAPSED_SIDEBAR_WIDTH);
  fittedRef.current = fitted;
  const effectiveLeftWidth = fitted.left;
  const drawnRightWidth = fitted.right;

  return (
    <div style={shellStyle}>
      {/* First-run guided tour: opens by itself only on a first visit; `?tour=1` or the ? button reopen it. */}
      <HarnessTour firstVisit={firstVisit} />
      {/* Left Sidebar */}
      <div style={{ width: effectiveLeftWidth, flexShrink: 0, transition: 'width 200ms ease' }} data-tour="sidebar">
        <SessionSidebar
          ref={sidebarRef}
          sessions={sessions}
          activeSessionId={activeSession}
          collapsed={sidebarCollapsed}
          onToggleCollapse={handleToggleSidebar}
          onSelectSession={handleSelectSession}
          onNewSession={handleNewSession}
          onGoChat={handleGoHome}
          onShowLanding={onShowLanding}
          onOpenSettings={onOpenSettings}
          onOpenWorkflows={handleOpenWorkflows}
          onNewWorkflow={handleNewWorkflow}
          onOpenCustomAgents={handleOpenCustomAgents}
          onNewCustomAgent={handleNewCustomAgent}
          onSelectCustomAgent={handleSelectCustomAgent}
          customAgents={customAgents}
          onOpenSkills={handleOpenSkills}
          onOpenImprove={handleOpenImprove}
          improveBadge={improveBadge}
          skills={skillSummaries}
          onSelectSkill={assetId => {
            /*
             * A published lineage with no working copy has nothing to edit:
             * `SkillEditor` reads the draft-only OWNED partition, which publishing
             * emptied, so routing it to the editor yields a blank form. Its
             * read-only viewer is the right destination — the same one the row's
             * View opens. Editing from there forks first.
             */
            const published = publishedOnlySkills.find(summary => summary.assetId === assetId);
            if (!published) {
              handleOpenSkillEditor({ category: 'skill', assetId });
              return;
            }
            setCenterMode('skills');
            setEditingTarget(null);
            setSidebarSkillDetail({ kind: 'published', assetId, version: published.version });
          }}
          onNewSkill={() => handleOpenSkillEditor({ category: 'skill', assetId: null })}
          activeSkillId={
            (editingTarget?.category === 'skill' ? (editingTarget.assetId ?? undefined) : undefined)
            ?? (sidebarSkillDetail && 'assetId' in sidebarSkillDetail ? sidebarSkillDetail.assetId : undefined)
            ?? undefined
          }
          activeCustomAgentId={customAgentEditorOpen ? (editingCustomAgentId ?? undefined) : undefined}
          onDeleteSession={handleDeleteSession}
          onRenameSession={handleRenameSession}
          onToggleLock={handleToggleLock}
          onSelectRun={handleSelectRun}
          activeRunId={activeRunId}
          centerMode={centerMode}
          onOpenProjects={handleOpenProjects}
          onNewProject={handleNewProject}
          projectsEnabled={projectsEnabledState}
          activeProjectId={projectsSelectedId}
          onSelectProject={persistProjectsSelected}
        />
      </div>

      {!sidebarCollapsed && <ResizeDivider onResize={handleLeftResize} orientation="vertical" />}

      {/* Center */}
      <div style={centerStyle} data-tour="center">
        {/* Notification bell. The chat top bar hosts it in chat mode; on every
            other center mode (agents / settings / home / workflows) render a
            floating one so the bell — and its durable completion notifications —
            stays reachable regardless of which tab you're on. The two are
            mutually exclusive by mode, so they never double up. */}
        {centerMode !== 'chat' && (
          <div style={{ position: 'absolute', top: 10, right: 14, zIndex: theme.zIndex.rail, display: 'flex', gap: 8 }}>
            <HelpButton />
            <CompletionNotifications onOpen={handleOpenNotification} />
          </div>
        )}
        {/* Chat panel — always mounted to preserve state, hidden via display:none */}
        <div style={{ display: centerMode === 'chat' ? 'flex' : 'none', flexDirection: 'column', flex: 1, overflow: 'hidden' }}>
          {/* Top Bar */}
          <div style={topBarStyle}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              {/* The chat's name; its id (for support) is the tooltip, not a bar of its own. */}
              <span style={{ fontSize: 'var(--fs-sm)', fontWeight: theme.typography.weights.medium, color: theme.colors.textPrimary }}
                title={`Session ${activeSession}`}>
                {sessions.find(s => s.id === activeSession)?.title || 'Session'}
              </span>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span style={modelBadgeStyle} title="Orchestrator model">{
                // Prefer what the agent reported running; fall back to the
                // pending selection. Never guess from the model LIST — that is
                // how this badge came to advertise a model nothing was using.
                models.find(m => m.id === (reportedModel || activeModel))?.name
                || ((reportedModel || activeModel) ? formatModelId(reportedModel || activeModel) : '')
                || 'Default model'
              }</span>
              <HelpButton />
              <CompletionNotifications onOpen={handleOpenNotification} />
            </div>
          </div>

          <>
            <div style={{ flex: 1, display: editingTarget ? 'none' : 'flex', flexDirection: 'column', overflow: 'hidden' }}>
              <ChatCanvas
                  onRenderBlock={handleRenderBlock}
                  onActivateJob={handleActivateJob}
                  onRenderPending={handleRenderPending}
                  onRenderComposing={handleRenderComposing}
                  onRenderFailed={handleRenderFailed}
                  pendingPrompt={pendingPrompt}
                  sessionTitle={sessions.find(session => session.id === activeSession)?.title}
                  locked={sessions.find(session => session.id === activeSession)?.locked ?? false}
                  onSessionInputStarted={handleSessionInputStarted}
                  onSessionPersisted={handleSessionPersisted}
                  onManageProjects={(pid) => handleOpenProjects({ select: pid })}
                />
            </div>
            {/* The chat pane stays mounted (display:none) behind the other
                centers, so a target whose own center hosts CenterEditor would
                mount it twice — duplicating the mount and its data fetch. */}
            {editingTarget
              && !(centerMode === 'skills' && editingTarget.category === 'skill')
              && !(centerMode === 'activity' && editingTarget.category === 'workflow')
              && !(centerMode === 'projects' && PROJECTS_HOSTED_EDITOR_CATEGORIES.includes(editingTarget.category)) && (
              <CenterEditor
                key={`${editingTarget.category}-${editingTarget.assetId ?? 'new'}`}
                category={editingTarget.category}
                assetId={editingTarget.assetId}
                onClose={() => setEditingTarget(null)}
              />
            )}
          </>
        </div>

        {centerMode === 'activity' && (
          <div style={{ position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column' }}>
            {editingTarget?.category === 'workflow' ? (
              <CenterEditor
                key={`workflow-${editingTarget.assetId ?? 'new'}`}
                category="workflow"
                assetId={editingTarget.assetId}
                onClose={() => setEditingTarget(null)}
                // Closing keeps centerMode on 'activity', so this returns to the
                // Workflow surface — the default "Back to Chat" would misdescribe
                // where the button actually goes.
                backLabel="Back to Workflow"
              />
            ) : (
              <ActivityPanel revealRunId={activeRunId} onRevealed={() => setActiveRunId('')} />
            )}
          </div>
        )}

        {centerMode === 'custom-agent' && (
          /*
           * The surface header sits OUTSIDE the scroller, so it stays put on all
           * three branches — inventory, published detail, editor — exactly as
           * `SkillsSurfaceHeader` does for Skills. Inside it, and the heading
           * would scroll away on the two branches with long content, which is
           * the same disappearance it exists to prevent.
           */
          <div style={agentsSurfaceStyle} data-testid="agents-surface">
            <AgentsSurfaceHeader />
            <div style={customAgentCenterScrollStyle} data-testid="custom-agent-center-scroll">
              {customAgentEditorOpen ? (
                /*
                 * The editor's own `containerStyle` pads 12px; the list and the
                 * published view both sit at 24px. Made up here rather than in the
                 * editor because the OTHER host (`CenterEditor`) already wraps it in
                 * `editorContentStyle` — widening the editor itself would double the
                 * inset there.
                 */
                <div style={agentEditorFrameStyle}>
                  <SpecialtyAgentEditor
                    key={editingCustomAgentId ?? 'new'}
                    assetId={editingCustomAgentId}
                    onClose={handleCloseCustomAgentEditor}
                  />
                </div>
              ) : viewingPublished ? (
                <PublishedAgentView
                  summary={viewingPublished}
                  forking={forkingPublished}
                  onEdit={() => handleEditPublishedCustomAgent(viewingPublished.assetId)}
                  onClose={() => setViewingPublished(null)}
                />
              ) : (
                <CustomAgentCenterList
                  agents={customAgents}
                  publishedById={publishedAgentById}
                  loading={loadingCategories['specialty-agent']}
                  onSelect={handleSelectCustomAgent}
                  onViewPublished={handleViewPublishedCustomAgent}
                  onEditPublished={handleEditPublishedCustomAgent}
                  onDelete={handleDeleteCustomAgent}
                  onPublish={handlePublishCustomAgent}
                  onPurge={setPurgeTarget}
                />
              )}
            </div>
          </div>
        )}

        {centerMode === 'skills' && (
          // Bounded flex column, as the activity branch does. Without a definite
          // height here, `SkillsCenter`'s `height: 100%` has nothing to resolve
          // against, so its body's `overflowY: auto` never engages and a long
          // SKILL.md runs off the pane instead of scrolling.
          <div style={{ position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column' }}>
            {editingTarget?.category === 'skill' ? (
              /*
               * The editor is NOT hosted inside `SkillsCenter` — it replaces it — so
               * it is the one Skills branch that has to compose the header itself.
               * Without this, editing a skill was the single place the heading and
               * its rule still vanished, which is the whole reason they were made to
               * persist. Imported rather than re-declared: two definitions of the
               * same header drift.
               */
              <div style={skillsSurfaceStyle}>
                <SkillsSurfaceHeader />
                <div style={skillsNestedStyle}>
                  <CenterEditor
                    key={`skill-${editingTarget.assetId ?? 'new'}`}
                    category="skill"
                    assetId={editingTarget.assetId}
                    onClose={() => setEditingTarget(null)}
                  />
                </div>
              </div>
            ) : (
              <SkillsCenter
                onOpenEditor={handleOpenSkillEditor}
                openDetail={sidebarSkillDetail}
                onDetailOpened={handleSkillDetailOpened}
              />
            )}
          </div>
        )}

        {centerMode === 'projects' && (
          // Bounded flex column like the activity/skills branches, so the surface's own scrollers resolve
          // against a definite height. The project inventory is in the sidebar (as for Agents/Skills); this
          // is the Selected_Project, full width. The chat pane stays mounted (display:none) behind this; the
          // Projects_Surface hosts no CenterEditor in v1, so there is no second editor mount to guard.
          <div style={{ position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column' }}>
            <ProjectsSurface
              activeSessionId={activeSession}
              onNewChatInProject={handleNewChatInProject}
              createRequest={projectsCreateRequest}
              selectedId={projectsSelectedId}
              onSelectedChange={persistProjectsSelected}
              graphFocus={projectsGraphFocus}
              folderFocus={projectsFolderFocus}
              onOpenChat={handleOpenChatAtTurn}
            />
          </div>
        )}

      </div>

      {/* Purge is mounted outside the center pane so it survives the view
          switching underneath it — a dialog unmounted mid-request would lose
          the "retry to finish" message a partial purge returns. */}
      <PurgeAssetDialog
        category="specialty-agent"
        agent={purgeTarget}
        onCancel={() => setPurgeTarget(null)}
        onPurged={handlePurgedCustomAgent}
      />

      {/* Right Panel — Renderer | Jobs */}
      {!rightCollapsed && (
        <>
          <ResizeDivider onResize={handleRightResize} orientation="vertical" />
          <div style={{ ...rightPanelStyle, width: drawnRightWidth }} data-tour="right-panel">
            <div style={rightTabBarStyle}>
              <div style={segGroupStyle}>
                <button onClick={() => setRightTab('renderer')} style={rightTab === 'renderer' ? segActiveStyle : segStyle}>
                  <RendererTabIcon /> Renderer
                </button>
                <button onClick={() => setRightTab('jobs')} style={rightTab === 'jobs' ? segActiveStyle : segStyle}>
                  <JobsTabIcon /> Jobs{jobsRunningCount > 0 && (
                    <span
                      data-testid="jobs-running-dot"
                      title={`${jobsRunningCount} job${jobsRunningCount > 1 ? 's' : ''} running`}
                      style={{ marginLeft: 6, width: 8, height: 8, borderRadius: '50%', background: theme.colors.primary, display: 'inline-block', animation: 'jobsPulse 1.4s ease-in-out infinite' }}
                    />
                  )}
                </button>
              </div>
            </div>
            <div style={{ flex: 1, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
              {rightTab === 'renderer' ? (
                // renderer-visibility invariant: only mount the renderer when
                // there's something to show (a render in history or one
                // composing). Otherwise the tab stays empty instead of
                // presenting a misleading "No rendered content yet" drawer.
                shouldShowRenderer(false, renderHistory.length, renderPending) ? (
                  <SessionRendererPanel
                    history={renderHistory}
                    currentIndex={Math.min(renderIndex, renderHistory.length - 1)}
                    onNavigate={(i: number) => { userPinnedRef.current = true; setRenderIndex(i); }}
                    onClose={handleToggleRightPanel}
                    onInteraction={handleRenderInteraction}
                    pending={renderPending}
                    onRenderTab={handleRenderTab}
                    onRemoveTab={handleRemoveRenderTab}
                    activeSessionId={activeSession}
                    locked={sessions.find(session => session.id === activeSession)?.locked ?? false}
                  />
                ) : null
              ) : (
                <JobsPanel
                  ref={jobsPanelRef}
                  activeSessionId={activeSession}
                  onOpenFlow={(runId) => { setActiveRunId(runId); setCenterMode('activity'); }}
                />
              )}
            </div>
          </div>
          {/* Collapse toggle — vertically centered on the panel's left edge (the
              divider), the SAME middle-of-page spot the expand toggle uses when
              collapsed, so the control never jumps. */}
          <button onClick={handleToggleRightPanel} style={{ ...edgeToggleStyle, right: drawnRightWidth - 11 }} title="Collapse panel" aria-label="Collapse panel">
            <ChevronRightIcon />
          </button>
        </>
      )}
      {rightCollapsed && (
        <button onClick={handleToggleRightPanel} style={{ ...edgeToggleStyle, right: 10 }} title="Show panel" aria-label="Show panel">
          <ChevronLeftIcon />
        </button>
      )}
    </div>
  );
}

// Custom Agent center-panel list (Req 1.2, 1.3). Rendered when the
// Custom Agent center mode is active and no editor is open. Lists the
// Owner's agents (name, lifecycle state, last-modified) with a shared
// "+ New agent" action, and shows an empty state when the Owner owns none.

/**
 * The single Agents surface.
 *
 * This used to be one of two: a Settings → Capabilities → Agents tab rendered
 * the same inventory with its own row actions. The overlap was almost total —
 * list, create, edit, test, and publish were reachable both ways — but three
 * things lived ONLY in the settings tab, so consolidating meant moving them
 * here rather than deleting them:
 *
 *   - Delete. `deleteSpecialtyAgentDraft` had exactly one call site in the app;
 *     `SpecialtyAgentEditor` has no delete, so dropping the tab would have left
 *     no way to remove a specialty agent from the UI at all.
 *   - The built-in agents browser, the only surface listing the Task/Planning/
 *     Design/Render sub-agent runtimes. It is off by default because these are
 *     read-only platform runtimes, not things the Owner authors.
 *   - Search.
 *
 * Rows are full-width, one per agent, with the actions pinned right. The action
 * set depends on what the row actually IS, so no button is offered that can only
 * fail:
 *
 *   Draft (working copy)  Edit · Publish · Delete
 *   Published version     View · Edit (forks into a working copy)
 *   Core Task Agent       none, and not clickable
 *
 * The core Task Agent is appended by the service layer
 * (`lab/service.py::_handle_list_specialty_agent_drafts`) as a virtual row with
 * no AssetIndex record, so Read/Edit/Delete against it all fail — it is inert
 * here rather than a row that errors when clicked.
 *
 * One absence is deliberate: there is no row-level **Test**. `TestHarnessPanel`
 * is mounted only inside Settings, and both the editor and the published view
 * already embed a test panel, so a Test button here would just be Edit with
 * extra steps.
 *
 * Delete on a published row means **purge** — a different operation from the
 * draft Delete beside it, not a stronger version of it. It destroys immutable
 * versions and their S3 artifacts, cannot be undone, and goes through the
 * Capability API rather than `/lab`, so it opens a dialog showing the blast
 * radius and requiring the agent's name to be typed.
 */
function CustomAgentCenterList({ agents, publishedById, loading, onSelect, onViewPublished, onEditPublished, onDelete, onPublish, onPurge }: {
  agents: CustomAgentSummary[];
  publishedById: Map<string, PublishedAssetSummary>;
  loading: boolean;
  onSelect: (id: string) => void;
  onViewPublished: (summary: PublishedAssetSummary) => void;
  /** Fork a published lineage into a working copy, then open the editor on it. */
  onEditPublished: (id: string) => Promise<void> | void;
  onDelete: (id: string) => Promise<void> | void;
  onPublish: (id: string) => Promise<void> | void;
  onPurge: (target: PurgeAssetTarget) => void;
}) {
  const [search, setSearch] = useState('');
  const [showBuiltIn, setShowBuiltIn] = useState(false);

  const query = search.trim().toLowerCase();
  const visible = query
    ? agents.filter(a => (a.name || a.id).toLowerCase().includes(query))
    : agents;

  // Split into the same two sections Skills uses (Yours drafts / Published) so
  // the two center views are structurally consistent. Published-only agents
  // (published with no working copy) land in the Published section; the core
  // Task Agent and working copies land under Yours (drafts).
  const visibleDrafts = visible.filter(a => a.lifecycleState !== 'published');
  const visiblePublished = visible.filter(a => a.lifecycleState === 'published');

  // Same All/Drafts/Published control and semantics as Skills: `all` keeps the
  // established shape (drafts always, Published only when populated), while an
  // explicit pick is literal and shows that one section even when empty — so
  // choosing a filter never blanks the pane without saying why.
  const [stateFilter, setStateFilter] = useState<InventoryState>('all');
  const showDrafts = stateFilter !== 'published';
  const showPublished = stateFilter === 'published' || (stateFilter === 'all' && visiblePublished.length > 0);

  // One row renderer, reused by both sections so the per-lifecycle action set
  // stays identical to before the split.
  const renderAgentRow = (agent: CustomAgentSummary) => {
    const published = publishedById.get(agent.id);
    const isPublishedRow = agent.lifecycleState === 'published';
    return (
      <CustomAgentRow
        key={agent.id}
        agent={agent}
        // A published row opens its read-only detail view; a working copy opens
        // the editor. The core Task Agent opens nothing.
        onOpen={agent.isCoreAgent
          ? undefined
          : isPublishedRow
            ? (published ? () => onViewPublished(published) : undefined)
            : () => onSelect(agent.id)}
        onView={isPublishedRow && published ? () => onViewPublished(published) : undefined}
        // Edit follows the same published/working-copy split as `onOpen` above.
        // Publishing consumes the working copy, so a published row has NO draft
        // to edit: routing it to `onSelect` opened the editor on a lineage id
        // whose draft-only read returns nothing, giving a blank form and "Agent
        // not found" with no way to iterate on a published agent at all. Fork to
        // a working copy first, exactly as the read-only detail view's Edit does.
        onEdit={agent.isCoreAgent
          ? undefined
          : isPublishedRow
            ? () => onEditPublished(agent.id)
            : () => onSelect(agent.id)}
        // Publish and Delete address the owner's OWNED partition, so they belong
        // to working copies only.
        onPublish={isPublishedRow || agent.isCoreAgent ? undefined : () => onPublish(agent.id)}
        onDelete={isPublishedRow || agent.isCoreAgent ? undefined : () => onDelete(agent.id)}
        // Purge replaces Delete on a published row. Not offered for the core
        // Task Agent, which has no AssetIndex record at all.
        onPurge={isPublishedRow && !agent.isCoreAgent
          ? () => onPurge({
              id: agent.id,
              name: agent.name || `Agent ${agent.id.slice(0, 6)}`,
            })
          : undefined}
      />
    );
  };

  return (
    /*
     * No title header here. The "Agents" heading and its description are the
     * SURFACE header (`AgentsSurfaceHeader`), rendered by the host above all three
     * branches — this list, the published detail view and the editor. Rendering a
     * second copy here would show the heading twice on the inventory and, worse,
     * make it look owned by the list, which is how it came to vanish the moment
     * you opened an agent.
     *
     * No create button either: the sidebar's shared New action already reads
     * "New agent" in this mode, so a second primary button one glance away was
     * pure duplication. The empty state below does not repeat it either — it
     * names that action instead, so the surface has exactly one create control.
     */
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div style={caToolbarStyle}>
        <input
          value={search}
          onChange={e => setSearch(e.target.value)}
          placeholder="Search agents..."
          aria-label="Search agents"
          style={caSearchStyle}
        />
        <CapabilitySwitch checked={showBuiltIn} onChange={setShowBuiltIn} label="Built-in agents" />
      </div>

      <div style={caBodyStyle}>
        {showBuiltIn && (
          <div style={{ marginBottom: 20, border: `1px solid ${theme.colors.border}`, borderRadius: theme.radius.panel, overflow: 'hidden' }}>
            <BuiltInAgentsBrowser externalSearch={search} />
          </div>
        )}

        {/* Filters this actor's inventory, not the built-in browser above. Hidden
            when there is nothing to filter. */}
        {agents.length > 0 && (
          <InventoryStateFilter
            value={stateFilter}
            onChange={setStateFilter}
            ariaLabel="Filter agents by state"
            counts={{ all: visible.length, drafts: visibleDrafts.length, published: visiblePublished.length }}
          />
        )}

        {loading && agents.length === 0 ? (
          <div style={caEmptyStyle}>
            <style>{'@keyframes caSpin { to { transform: rotate(360deg); } }'}</style>
            <span style={caSpinnerStyle} aria-hidden="true" />
            <p style={{ margin: '12px 0 0', fontSize: 'var(--fs-sm)', color: theme.colors.textSecondary }}>
              Loading agents…
            </p>
          </div>
        ) : agents.length === 0 ? (
          // No agents at all → "Yours (drafts)" header + empty card, matching
          // Skills' empty state (which also shows the section header).
          //
          // The card NAMES the sidebar "New agent" action rather than repeating
          // it: a second identical button a glance from the sidebar one read as
          // a duplicate, the same complaint Skills' empty state answered.
          <>
            <InventorySectionHeader label="Yours (drafts)" />
            <EmptyState label="custom agents" cta="+ New agent" />
          </>
        ) : visible.length === 0 ? (
          // Search matched nothing across BOTH sections. Distinct from "none
          // exist" so a typo doesn't read as the inventory having been wiped.
          <div style={caEmptyStyle}>
            <p style={{ margin: 0, fontSize: 'var(--fs-sm)', color: theme.colors.textSecondary }}>
              No agents match “{search.trim()}”.
            </p>
          </div>
        ) : (
          // Two sections mirroring Skills' SkillsTabBody: Yours (drafts) always
          // shown; Published shown only when populated.
          //
          // Merge note: this two-section structure is upstream's. The per-row
          // props live in `renderAgentRow` above, where our published-row Edit
          // fork was reapplied — upstream's refactor had collapsed `onEdit` to a
          // bare `onSelect`, which reintroduces the blank-form / "Agent not
          // found" bug for published rows.
          <>
            {showDrafts && (
              <>
                <InventorySectionHeader label="Yours (drafts)" />
                {visibleDrafts.length > 0 ? (
                  <InventoryRowList>{visibleDrafts.map(renderAgentRow)}</InventoryRowList>
                ) : (
                  <EmptyState>{query ? 'No draft agents match.' : 'No draft agents yet.'}</EmptyState>
                )}
              </>
            )}
            {showPublished && (
              <>
                <InventorySectionHeader label="Published" />
                {visiblePublished.length > 0 ? (
                  <InventoryRowList>{visiblePublished.map(renderAgentRow)}</InventoryRowList>
                ) : (
                  <EmptyState>{query ? 'No published agents match.' : 'No published agents yet.'}</EmptyState>
                )}
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}

/**
 * One agent row: identity on the left, actions pinned right.
 *
 * Delete is a two-step confirm, and Publish reports in-flight state, matching
 * the row semantics already established by `ListRow` in
 * `components/settings/CapabilitiesSettings.tsx`.
 *
 * Two behaviours are load-bearing:
 *
 *   - Every control calls `swallow` first. The row body is click-to-open, so
 *     without it Cancel would also fire `onOpen` and drop the user into the
 *     editor for the agent they had just decided to keep.
 *   - The row stays MOUNTED while deleting, dimmed and with its controls
 *     disabled, until the parent's refresh removes it. Unmounting it optimistically
 *     re-flows the list under the pointer, and a click can then land on whichever
 *     row moves up — observed in the deployed app as a stray Edit that forked a
 *     published agent.
 */
function CustomAgentRow({ agent, onOpen, onView, onEdit, onPublish, onDelete, onPurge }: {
  agent: CustomAgentSummary;
  onOpen?: () => void;
  onView?: () => void;
  onEdit?: () => void;
  onPublish?: () => Promise<void> | void;
  onDelete?: () => Promise<void> | void;
  /**
   * Published rows only. Deliberately NOT routed through the row's two-step
   * confirm: the parent opens a dialog that shows the blast radius and requires
   * the agent's name typed, because "Confirm delete" next to the pointer is not
   * a proportionate gate on an irreversible action.
   */
  onPurge?: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const name = agent.name || `Agent ${agent.id.slice(0, 6)}`;
  const busy = deleting || publishing;

  const swallow = (e: React.MouseEvent) => { e.stopPropagation(); };

  const run = async (
    e: React.MouseEvent,
    action: (() => Promise<void> | void) | undefined,
    setBusy: (v: boolean) => void,
    failure: string,
  ) => {
    swallow(e);
    if (!action) return;
    setError(null);
    setBusy(true);
    try {
      await action();
      setConfirming(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : failure);
    } finally {
      setBusy(false);
    }
  };

  // Published version, shown even when a live working copy shadows it in the
  // list — otherwise a lineage with a draft looks like it never shipped.
  const versionNote = typeof agent.publishedVersion === 'number'
    ? `v${agent.publishedVersion} published`
    : null;
  const updatedNote = typeof agent.lastModified === 'number'
    ? `Updated ${new Date(agent.lastModified).toLocaleString()}`
    : agent.isCoreAgent ? 'Platform built-in' : 'Not yet saved';

  /*
   * `Built-in` is provenance (informational), `published` is a lifecycle success,
   * and everything else is a resting draft. Draft is `neutral` rather than a hue:
   * it used to be `accent`, which is the SAME colour as `success` in
   * dark-kirocrew, so Draft and Published badges were indistinguishable there.
   */
  const badgeVariant = agent.isCoreAgent
    ? 'info'
    : agent.lifecycleState === 'published' ? 'success' : 'neutral';

  return (
    <InventoryRow
      onClick={onOpen && !busy ? onOpen : undefined}
      dimmed={deleting}
      name={name}
      description={agent.description}
      badge={{
        label: agent.isCoreAgent
          ? 'Built-in'
          : agent.lifecycleState ? formatCustomAgentState(agent.lifecycleState) : 'Draft',
        variant: badgeVariant,
      }}
      /* The version qualifies the NAME, so it sits beside it rather than costing the
         row a line — same reason as the published Skills rows. The prose date stays on
         the meta line in the body face; the version keeps the tabular-mono spine so it
         lines up down the column. */
      nameSuffix={versionNote && <span className="numeric">{versionNote}</span>}
      meta={updatedNote}
      error={error}
      actions={
        confirming ? (
          <>
            <Button
              variant="danger"
              onClick={(e) => void run(e, onDelete, setDeleting, 'Delete failed')}
              disabled={busy}
              busy={deleting}
              busyLabel="Deleting…"
              aria-label={`Confirm delete ${name}`}
            >
              Confirm delete
            </Button>
            <Button variant="ghost" onClick={(e) => { swallow(e); setConfirming(false); }} disabled={busy}>
              Cancel
            </Button>
          </>
        ) : (
          <>
            {onView && (
              <Button onClick={(e) => { swallow(e); onView(); }} aria-label={`View ${name}`}>View</Button>
            )}
            {onEdit && (
              <Button onClick={(e) => { swallow(e); onEdit(); }} aria-label={`Edit ${name}`}>Edit</Button>
            )}
            {onPublish && (
              <Button
                variant="primary"
                onClick={(e) => void run(e, onPublish, setPublishing, 'Publish failed')}
                disabled={busy}
                busy={publishing}
                busyLabel="Publishing…"
                aria-label={`Publish ${name}`}
              >
                Publish
              </Button>
            )}
            {onDelete && (
              <Button onClick={(e) => { swallow(e); setConfirming(true); }} disabled={busy} aria-label={`Delete ${name}`}>
                Delete
              </Button>
            )}
            {onPurge && (
              <Button onClick={(e) => { swallow(e); onPurge(); }} disabled={busy} aria-label={`Delete ${name}`}>
                Delete
              </Button>
            )}
          </>
        )
      }
    />
  );
}

function formatCustomAgentState(state: string): string {
  if (!state) return 'Draft';
  return state.charAt(0).toUpperCase() + state.slice(1);
}

// Read-only detail for a PUBLISHED custom agent. Shows the summary already in
// hand (name/version/scope/updated + invoke URL) without forking. Editing the
// full config (persona/tools/integration) requires forking into a working copy,
// which the Edit button does explicitly.
function PublishedAgentView({ summary, forking, onEdit, onClose }: {
  summary: PublishedAssetSummary;
  forking: boolean;
  onEdit: () => void;
  onClose: () => void;
}) {
  const invokeUrl = `${window.location.origin}/invoke/${summary.assetId}`;
  const rows: [string, string][] = [
    ['Agent ID', summary.assetId],
    ['Version', `v${summary.version}${summary.current ? ' (current)' : ''}`],
    ['Scope', summary.scope],
    ['Published', new Date(summary.updatedAt).toLocaleString()],
  ];
  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {/* The agent's OWN header, one level under the Agents surface header the
          host keeps on screen — the same two-tier arrangement `SkillDetailsView`
          renders under `SkillsSurfaceHeader`, down to the metrics. It carries no
          bottom rule: the surface header directly above already draws one. */}
      <div style={surfaceNestedHeaderStyle}>
        <div style={{ minWidth: 0 }}>
          <h2 style={caDetailTitleStyle}>{summary.name}</h2>
          <p style={caDetailDescriptionStyle}>
            Published agent (read-only). Edit forks it into a working copy.
          </p>
        </div>
        <div style={caDetailActionsStyle}>
          <Button variant="secondary" size="md" onClick={onClose} disabled={forking}>Back</Button>
          <Button variant="primary" size="md" onClick={onEdit} disabled={forking} busy={forking} busyLabel="Opening…">
            Edit
          </Button>
        </div>
      </div>

      <div style={caBodyStyle}>
        <div style={{ maxWidth: 640 }}>
          <Badge variant="success">Published</Badge>
          <dl style={{ margin: '16px 0 0', display: 'grid', gridTemplateColumns: 'max-content 1fr', gap: '8px 16px' }}>
            {rows.map(([k, v]) => (
              <React.Fragment key={k}>
                <dt style={{ fontSize: 'var(--fs-xs)', color: theme.colors.textSecondary }}>{k}</dt>
                <dd style={{ margin: 0, fontSize: 'var(--fs-xs)', color: theme.colors.textPrimary, fontFamily: 'var(--font-mono)', wordBreak: 'break-all' }}>{v}</dd>
              </React.Fragment>
            ))}
            <dt style={{ fontSize: 'var(--fs-xs)', color: theme.colors.textSecondary }}>Invoke URL</dt>
            <dd style={{ margin: 0, fontSize: 'var(--fs-xs)', color: theme.colors.textPrimary, fontFamily: 'var(--font-mono)', wordBreak: 'break-all' }}>
              POST {invokeUrl}
            </dd>
          </dl>
          <p style={{ margin: '20px 0 0', fontSize: 'var(--fs-xs)', color: theme.colors.textSecondary, lineHeight: 1.5 }}>
            Persona, skills, tools, and integration settings are part of the
            published version. Click <strong>Edit</strong> to fork this version into
            an editable working copy and view or change them.
          </p>

          {/* Test the published version in place — no fork required. Reports
              which tools/skills/model actually bind, and can invoke it for
              real. Previously the only test path refused non-draft rows, so a
              published version could not be exercised at all. */}
          <AgentTestPanel
            assetId={summary.assetId}
            source="published"
            version={summary.version}
          />
        </div>
      </div>
    </div>
  );
}

function SessionTabsIcon({ collapsed }: { collapsed: boolean }) {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <rect x="1" y="2" width="14" height="12" rx="2" />
      <path d="M10 2v12" />
      {collapsed ? <path d="M5 7l2 1-2 1" /> : <path d="M7 7l-2 1 2 1" />}
    </svg>
  );
}

const shellStyle: React.CSSProperties = {
  display: 'flex', height: '100vh', overflow: 'hidden',
  background: theme.colors.background, fontFamily: theme.typography.fontFamily,
};

const centerStyle: React.CSSProperties = {
  flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0, overflow: 'hidden',
  position: 'relative',
  background: theme.colors.surface, borderLeft: `1px solid ${theme.colors.border}`,
  borderRight: `1px solid ${theme.colors.border}`,
};

// The custom-agent center mounts its editor / published view / list DIRECTLY,
// unlike the skills center which routes through `CenterEditor` and inherits that
// component's scroll region (`editorContentStyle`). Without this wrapper the
// content grows to its natural height inside `centerStyle`, which is deliberately
// `overflow: hidden` — so the bottom is simply unreachable. Measured on a 1203px
// viewport: 1330px of editor in a 1204px pane, putting Save, Cancel and Run Test
// below the fold with no scrollbar possible.
//
// `minHeight: 0` is load-bearing, not decoration: a flex child will not shrink
// below its content height without it, so `overflowY: 'auto'` alone does nothing
// here. That is why this class of bug hides.
export const customAgentCenterScrollStyle: React.CSSProperties = {
  flex: 1, minHeight: 0, overflowY: 'auto',
};

const topBarStyle: React.CSSProperties = {
  padding: '10px 16px', borderBottom: `1px solid ${theme.colors.border}`,
  display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexShrink: 0,
};


const modelBadgeStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)', color: theme.colors.textSecondary, padding: '4px 8px',
  background: theme.colors.background, borderRadius: theme.radius.button,
};

const rightPanelStyle: React.CSSProperties = {
  flexShrink: 0, display: 'flex', flexDirection: 'column',
  background: theme.colors.sidebarBg, overflow: 'hidden',
};

const rightTabBarStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 6, padding: '8px 8px 8px 10px',
  borderBottom: `1px solid ${theme.colors.border}`, flexShrink: 0,
};

const segGroupStyle: React.CSSProperties = {
  display: 'flex', gap: 2, flex: 1, background: theme.colors.border,
  borderRadius: theme.radius.button, padding: 2,
};

const segStyle: React.CSSProperties = {
  flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 6,
  fontSize: 'var(--fs-xs)', fontWeight: theme.typography.weights.medium, padding: '5px', borderRadius: theme.radius.button,
  border: 'none', background: 'none', cursor: 'pointer', color: theme.colors.textSecondary,
};

const segActiveStyle: React.CSSProperties = {
  ...segStyle, background: theme.colors.surface, color: theme.colors.primary,
  boxShadow: theme.elevation.subtle,
};

// One centered edge toggle for BOTH collapse (open) and expand (collapsed) so
// the control lives at the same middle-of-page spot in either state. Callers set
// `right` inline: `drawnRightWidth - 11` (on the divider) when open, `10` when collapsed.
const edgeToggleStyle: React.CSSProperties = {
  position: 'fixed', top: '50%', transform: 'translateY(-50%)',
  width: 24, height: 46, borderRadius: theme.radius.button, border: `1px solid ${theme.colors.border}`,
  background: theme.colors.surface, boxShadow: theme.elevation.subtle, cursor: 'pointer',
  display: 'flex', alignItems: 'center', justifyContent: 'center', color: theme.colors.textSecondary,
  zIndex: theme.zIndex.rail,
};

// The Agents surface header is `AgentsSurfaceHeader`, shared with Skills through
// `SurfaceHeader` — there is deliberately no local copy of it here any more. What
// remains are the styles for the published agent's OWN header, which sits one level
// under it and matches `SkillDetailsView`'s equivalent.
const caDetailTitleStyle: React.CSSProperties = {
  margin: 0, fontSize: 'var(--fs-xl)', fontWeight: theme.typography.weights.semibold,
  color: theme.colors.textPrimary, overflowWrap: 'anywhere',
};

const caDetailDescriptionStyle: React.CSSProperties = {
  margin: '5px 0 0', maxWidth: 720, fontSize: 'var(--fs-sm)',
  color: theme.colors.textSecondary, lineHeight: 1.5,
};

const caDetailActionsStyle: React.CSSProperties = {
  display: 'flex', gap: 8, alignItems: 'center', flexShrink: 0,
};

// Tops the agent editor's own 12px up to the 24px the rest of the surface uses.
// `paddingTop: 8` because the editor leads with its title row, which needs less
// air under the surface header's rule than a 24px body would give it.
const agentEditorFrameStyle: React.CSSProperties = {
  padding: '8px 12px 12px',
};

// Mirrors SkillsCenter's `bodyStyle`, `minHeight` included. That property is what
// makes THIS box the scroller: a flex child will not shrink below its content
// height without it, so `overflowY: auto` was inert and the whole surface scrolled
// in the pane's outer scroller instead — taking the search row with it, while the
// equivalent row on Skills stayed put.
export const caBodyStyle: React.CSSProperties = {
  flex: 1, minHeight: 0, overflowY: 'auto', padding: 24,
};

const caEmptyStyle: React.CSSProperties = {
  display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
  textAlign: 'center', padding: '48px 24px', color: theme.colors.textSecondary,
};
// Self-contained spinner (injects its own keyframe) shown while the agent list loads.
const caSpinnerStyle: React.CSSProperties = {
  width: 22, height: 22, borderRadius: '50%',
  border: `2px solid ${theme.colors.border}`, borderTopColor: theme.colors.primary,
  animation: 'caSpin 0.8s linear infinite',
};
// Search + built-in toggle row. Sits between the header and the grid so the
// controls stay put while the grid scrolls under them.
const caToolbarStyle: React.CSSProperties = {
  display: 'flex', flexWrap: 'wrap', gap: 10, alignItems: 'center',
  justifyContent: 'space-between',
  padding: '14px clamp(12px, 3vw, 24px)', flexShrink: 0,
};

const caSearchStyle: React.CSSProperties = {
  flex: '1 1 220px', minWidth: 0, padding: '7px 10px', fontSize: 'var(--fs-sm)',
  border: `1px solid ${theme.colors.border}`, borderRadius: theme.radius.button,
  background: theme.colors.surface, color: theme.colors.textPrimary,
  fontFamily: theme.typography.fontFamily,
};
function RendererTabIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2" y="3" width="20" height="14" /><path d="M8 21h8M12 17v4" />
    </svg>
  );
}

function JobsTabIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" />
    </svg>
  );
}

function ChevronRightIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
      <path d="M9 18l6-6-6-6" />
    </svg>
  );
}

function ChevronLeftIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
      <path d="M15 18l-6-6 6-6" />
    </svg>
  );
}
