import React, { useState, useEffect, useMemo, forwardRef, useImperativeHandle, useRef } from 'react';
import { NameWithIndexGlyph } from './projects/SearchIndexedGlyph';
import { theme } from '../styles/tokens';
import { useCurrentUser } from '../auth/useCurrentUser';
import { useIsGatewayAdmin } from '../auth/isGatewayAdmin';
import { useJobs } from '../hooks/useJobs';
import { rollUpRuns } from './JobsPanel';
import { statusLabel, statusColor } from '../utils/jobStatus';
import { getActiveLogo, BRAND_NAME } from '../config/branding';
import { appVersionOf, loadRuntimeConfig, peekRuntimeConfig } from '../config/runtimeConfig';
import TrashIcon from './icons/TrashIcon';
import LockIcon from './icons/LockIcon';
import FolderIcon from './icons/FolderIcon';
import { useProjectsList } from '../projects/useProjectsList';
import { orderProjects } from '../projects/orderProjects';
import type { Job, JobStatus } from '../types/jobs';

interface Session {
  id: string;
  title: string;
  timestamp: number;
  preview?: string;
  messageCount?: number;
  /** Demo full-freeze: when true, rename/delete are disabled and chatting is blocked. */
  locked?: boolean;
}

/**
 * Lightweight view model for a Custom Agent row in the sidebar's
 * Custom Agent Center_Mode list (Req 1.2). Mirrors the shape of the
 * `Session` summary above: the sidebar only needs enough to render a
 * row (name, lifecycle state, last-modified) and route a selection —
 * the editor fetches the full draft on open. The parent maps the
 * owner's `SpecialtyAgentDraft` inventory (via `LabContext`) onto this
 * shape; the prop is optional so existing consumers keep compiling.
 */
export interface CustomAgentSummary {
  id: string;
  name: string;
  /**
   * What the author says the agent is FOR, shown on its inventory row.
   *
   * Optional and `undefined`-when-unknown, as everywhere else: the platform's
   * built-in Task Agent row is virtual and the Registry-only tier carries no
   * description, and a row must not render "no description" for those.
   */
  description?: string;
  /** Lifecycle state (e.g. draft, published, deprecated). */
  lifecycleState?: string;
  /** Last-modified time, epoch ms. */
  lastModified?: number;
  /**
   * The platform's built-in Task Agent, surfaced as a virtual row with no
   * AssetIndex record behind it. Carried through so views can suppress
   * destructive actions that would always fail against it.
   */
  isCoreAgent?: boolean;
  /**
   * Highest published version of this lineage, when one exists. Set even on a
   * working-copy row — a draft shadows its published versions in the agent
   * list, and without this the row could not show that the lineage has shipped.
   */
  publishedVersion?: number;
}

/**
 * Imperative handle exposed to consumers that need to scroll the Jobs
 * list to a specific job and flash a temporary highlight on its row —
 * notably the inline `JobReference` chip rendered in the chat thread
 * (task 13.3 wires this to the chip's onClick). The handle is a no-op
 * when the row is not currently rendered (sidebar collapsed, or the job
 * has aged out of the visible window).
 */
export interface SessionSidebarHandle {
  scrollToJob(jobId: string): void;
}

/**
 * The sidebar's own copy of the Center_Mode union (media-harness-projects-access task 13.7; design
 * Component 11 "Registrations" 1). It MUST stay in step with `WorkspaceView`'s `CENTER_MODES` — the two are
 * separate declarations on purpose, so `ui/src/views/center-mode-unions.contract.test.ts` is what catches
 * drift (a run-time Set equality plus a compile-time `Expect<Equal<…>>`).
 */
export const SIDEBAR_CENTER_MODES = ['chat', 'activity', 'custom-agent', 'skills', 'projects'] as const;
export type SidebarCenterMode = typeof SIDEBAR_CENTER_MODES[number];

interface SessionSidebarProps {
  sessions: Session[];
  activeSessionId: string;
  collapsed: boolean;
  onToggleCollapse: () => void;
  onSelectSession: (id: string) => void;
  /** Start a brand-new chat session (creates a row, switches active session). */
  onNewSession: () => void;
  /**
   * Navigate to the chat surface without changing the active session.
   * Used for the primary "Chat" nav item; "+ New" handles creation.
   */
  onGoChat: () => void;
  /** Show the public splash page without signing out or resetting workspace state. */
  onShowLanding?: () => void;
  onOpenSettings: (tab?: string) => void;
  onOpenWorkflows: () => void;
  /**
   * Select the top-level Custom Agent center mode — the peer to
   * `onGoChat`/`onOpenWorkflows`. The Agents nav entry is repurposed to
   * this handler instead of opening the Settings agents sub-tab.
   */
  onOpenCustomAgents?: () => void;
  /**
   * Select the top-level Skills center mode. Skills was promoted out of
   * Settings → Capabilities so the thing agents load sits beside Agents rather
   * than behind the gear icon.
   */
  onOpenSkills?: () => void;
  /** Opens the Improve surface — a Capabilities tab in the Settings modal. */
  onOpenImprove?: () => void;
  /** Dot when open suggestions are waiting: on the Improve row when expanded, and on the
   *  Settings rail icon when collapsed (the rail has no per-tab icons). */
  improveBadge?: boolean;
  onDeleteSession?: (id: string) => void;
  /** Persist a new title for a session (DDB + local state). */
  onRenameSession?: (id: string, newTitle: string) => void;
  /** Lock/unlock a session (demo full-freeze). `locked` is the desired next state. */
  onToggleLock?: (id: string, locked: boolean) => void;
  /**
   * The Owner's Custom_Agent assets to list when the Custom Agent
   * Center_Mode is active (Req 1.2). Passed in by the parent the same
   * way `sessions` is; optional and defaults to empty so consumers that
   * don't yet supply it keep compiling and simply render the empty
   * state (Req 1.3).
   */
  customAgents?: CustomAgentSummary[];
  /** The currently-open Custom_Agent, for active-row styling. */
  activeCustomAgentId?: string;
  /** Open the editor for an existing Custom_Agent (Req 1.5). */
  onSelectCustomAgent?: (id: string) => void;
  /**
   * Create a new draft Custom_Agent and open the editor (Req 1.4, 1.6).
   * Bound to the shared New_Item_Action when the Custom Agent Center_Mode
   * is active, mirroring `onNewSession` (chat) and `onOpenWorkflows`
   * (workflows). Optional so consumers that don't yet supply it keep
   * compiling.
   */
  onNewCustomAgent?: () => void;
  /** The Owner's skill drafts, listed when the Skills center mode is active. */
  /**
   * The Owner's skills — working copies AND published lineages that have no working
   * copy. Both, because publishing CONSUMES the working copy: a drafts-only list
   * empties itself the moment you publish everything you own, and then reports "0".
   */
  skills?: {
    assetId: string;
    name: string;
    updatedAt?: number;
    /** Set only for a published lineage with no working copy behind it. */
    publishedVersion?: number;
  }[];
  /** Open the editor for an existing skill. */
  onSelectSkill?: (id: string) => void;
  /**
   * The currently-open skill, for active-row styling — the peer to
   * `activeSessionId` (Chat) and `activeCustomAgentId` (Agents). Without it the
   * Skills list was the odd one out: it alone could not show which row is
   * selected, breaking the shared left-list idiom.
   */
  activeSkillId?: string;
  /** Create a new skill draft and open the editor. */
  onNewSkill?: () => void;
  /**
   * Create a new workflow draft and open the editor.
   *
   * Distinct from `onOpenWorkflows`, which only NAVIGATES to the Workflow
   * center. "New workflow" was bound to that one, so pressing it while already
   * on the Workflow tab set the mode it was already in and nothing happened.
   */
  onNewWorkflow?: () => void;
  /** Select a workflow run (shown in the Workflow sidebar list). */
  onSelectRun?: (runId: string) => void;
  /** Currently-focused workflow run id (highlights its sidebar row). */
  activeRunId?: string;
  /**
   * Select the top-level Projects center mode — peer to `onOpenSkills` (media-harness-projects-access
   * task 13.7, Req 10.1). Rendered only when `projectsEnabled` and this handler are both set.
   */
  onOpenProjects?: () => void;
  /** Create a new Project: the shared New_Item_Action in `projects` mode (Req 10.4). */
  onNewProject?: () => void;
  /** Whether `/config.projectsEnabled` is true; gates the Projects nav entry (design Component 11). */
  projectsEnabled?: boolean;
  /** The Selected_Project, for active-row styling in the `projects` inventory — peer to `activeSkillId`. */
  activeProjectId?: string | null;
  /** Select a Project from the inventory. Changes only the Selected_Project, never a chat's Active_Project. */
  onSelectProject?: (id: string) => void;
  // The sidebar's own copy of the Center_Mode union — see `SIDEBAR_CENTER_MODES` above; it must stay in
  // step with WorkspaceView's `CenterMode`, which the unions contract test enforces.
  centerMode?: SidebarCenterMode;
}

const SessionSidebar = forwardRef<SessionSidebarHandle, SessionSidebarProps>(function SessionSidebar({
  sessions, activeSessionId, collapsed, onToggleCollapse,
  onSelectSession, onNewSession, onGoChat, onShowLanding = () => {}, onOpenSettings, onOpenWorkflows, onOpenCustomAgents, onOpenSkills, onOpenImprove, improveBadge = false, onDeleteSession, onRenameSession, onToggleLock,
  customAgents, activeCustomAgentId, onSelectCustomAgent, onNewCustomAgent,
  skills, onSelectSkill, activeSkillId, onNewSkill, onNewWorkflow,
  onSelectRun, activeRunId, centerMode = 'chat',
  onOpenProjects, onNewProject, projectsEnabled = false, activeProjectId, onSelectProject,
}, ref) {
  // The lower Settings shortcuts: collapsed until opened, and remembered.
  const [lowerOpen, setLowerOpen] = useState<boolean>(() => {
    try { return localStorage.getItem('sidebar-settings-shortcuts') === 'open'; } catch { return false; }
  });
  const toggleLower = () => setLowerOpen((o) => {
    try { localStorage.setItem('sidebar-settings-shortcuts', o ? 'closed' : 'open'); } catch { /* storage off */ }
    return !o;
  });

  // Shared New_Item_Action: the "+ New" affordance morphs with the active
  // Center_Mode (Req 1.4, 1.6) — chat creates a session, activity opens the
  // workflow creator, and custom-agent creates a new draft Custom_Agent and
  // opens the editor. No bespoke per-mode create control.
  // ('workflows' became 'activity' when staging replaced the workflows center
  // with the activity aggregate view.)
  const onNewItemAction =
    centerMode === 'activity'
      ? (onNewWorkflow ?? onOpenWorkflows)
      : centerMode === 'custom-agent'
        ? onNewCustomAgent
        : centerMode === 'skills'
          ? onNewSkill
          : centerMode === 'projects'
            ? onNewProject
            : onNewSession;
  const hasNewItemAction = true;
  const newItemLabel =
    centerMode === 'activity'
      ? 'New workflow'
      : centerMode === 'custom-agent'
        ? 'New agent'
        : centerMode === 'skills'
          ? 'New skill'
          : centerMode === 'projects'
            ? 'New project'
            : 'New session';
  // Gateway registry is admin-only (R3, R7). The Settings → Gateways
  // sub-item is hidden for non-admins; the runtime is the authoritative
  // gate, this just keeps the chrome clean.
  const isGatewayAdmin = useIsGatewayAdmin();
  // Inline rename editor state — only one row in edit mode at a time.
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState<string>('');
  // Flat list, newest-created first. Date bucketing (Today/Yesterday/Older) was
  // dropped: it added complexity and, combined with activity bumping, made rows
  // appear to jump between groups. Order is stable by creation time (`timestamp`).
  const orderedSessions = orderSessions(sessions);
  const currentUser = useCurrentUser();
  const [appVersion, setAppVersion] = useState(() => appVersionOf(peekRuntimeConfig()));
  useEffect(() => {
    let live = true;
    void loadRuntimeConfig().then((cfg) => { if (live) setAppVersion(appVersionOf(cfg)); }).catch(() => undefined);
    return () => { live = false; };
  }, []);
  // Projects inventory: the same store the Projects_Surface reads and refreshes after every create,
  // rename, delete, or leave, so the two never disagree. Inert outside `projects` mode.
  const showProjectInventory = projectsEnabled && centerMode === 'projects';
  const { projects: projectList } = useProjectsList({ enabled: showProjectInventory });
  const orderedProjects = useMemo(() => orderProjects(projectList ?? []), [projectList]);
  // Each category lists its 15 most recent rows; the pager row below a list reveals 15 more at a time.
  // Per category, in memory: a reload starts collapsed again, which keeps the sidebar short by default.
  const [listLimits, setListLimits] = useState<Partial<Record<SidebarListId, number>>>({});
  const limitOf = (id: SidebarListId) => listLimits[id] ?? SIDEBAR_LIST_PAGE;
  const pagerFor = (id: SidebarListId, total: number, shown: number) => (
    <ListPager
      total={total}
      shown={shown}
      expanded={limitOf(id) > SIDEBAR_LIST_PAGE}
      onMore={() => setListLimits(m => ({ ...m, [id]: (m[id] ?? SIDEBAR_LIST_PAGE) + SIDEBAR_LIST_PAGE }))}
      onLess={() => setListLimits(m => ({ ...m, [id]: SIDEBAR_LIST_PAGE }))}
    />
  );
  const visibleSessions = capWithActive(orderedSessions, limitOf('chat'), s => s.id === activeSessionId);
  const visibleProjects = capWithActive(orderedProjects, limitOf('projects'), p => p.projectId === activeProjectId);
  const visibleAgents = capWithActive(customAgents ?? [], limitOf('agents'), a => a.id === activeCustomAgentId);
  const visibleSkills = capWithActive(skills ?? [], limitOf('skills'), k => k.assetId === activeSkillId);

  // Jobs section — async-job-tracking feature (task 13.1). The hook
  // seeds itself from `GET /jobs` on first mount and subscribes to the
  // shared store updated by `useJobsSubscription` (mounted once at the
  // root in `LabApp.tsx`). The visible-list filter (active or terminal
  // within the recency window) is enforced inside `useJobs` so this
  // component just renders whatever it returns.
  const { jobs } = useJobs();
  const [jobsExpanded, setJobsExpanded] = useState(true);
  // Consolidated run rows (one per plan/standalone) — drives the Workflow
  // header count badge. Flow detail itself lives in the center aggregate view.
  const runRows = useMemo(() => rollUpRuns(jobs), [jobs]);

  // Activity badge: shows a notification dot on the Activity nav when a
  // run-grouped job reaches terminal state. Cleared when the user navigates
  // to the Activity panel (centerMode === 'activity').
  const [activityBadge, setActivityBadge] = useState(false);

  useEffect(() => {
    const handler = () => { setActivityBadge(true); };
    window.addEventListener('activity:run-job-terminal', handler);
    return () => { window.removeEventListener('activity:run-job-terminal', handler); };
  }, []);

  // Clear badge when user views Activity
  useEffect(() => {
    if (centerMode === 'activity') setActivityBadge(false);
  }, [centerMode]);

  // Snapshot of the current time for relative-time formatting. Captured
  // once per render — the hook re-renders on every store update, which
  // refreshes the labels alongside any status changes.
  const now = Date.now();

  // Root container ref — used by `scrollToJob` to scope the
  // `[data-job-id]` lookup to this sidebar instead of the whole
  // document.
  const containerRef = useRef<HTMLDivElement>(null);

  useImperativeHandle(ref, () => ({
    scrollToJob(jobId: string): void {
      if (typeof document === 'undefined' || !jobId) return;
      // CSS.escape protects against jobIds that legitimately contain
      // characters that need escaping in a CSS selector (the canonical
      // jobId shape is an SFN execution ARN, which contains colons).
      // Fallback to a manual quote-escape for older runtimes.
      const escaped = (typeof CSS !== 'undefined' && typeof CSS.escape === 'function')
        ? CSS.escape(jobId)
        : jobId.replace(/"/g, '\\"');
      const root: ParentNode = containerRef.current ?? document;
      const row = root.querySelector<HTMLElement>(`[data-job-id="${escaped}"]`);
      if (!row) return; // Row not rendered (collapsed sidebar, or aged out).
      row.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      row.classList.add('job-row-highlight');
      window.setTimeout(() => {
        // Guard: the row may have been unmounted before the timeout fires.
        row.classList.remove('job-row-highlight');
      }, 1500);
    },
  }), []);

  // ---- Job-row action handlers (hover-revealed icon buttons) ----
  //
  // ``handleCopyJobId`` writes the full ARN to the clipboard. The
  // copy is best-effort: a permission failure or a context lacking
  // ``navigator.clipboard`` (Safari Private Mode, dev tunnels over
  // plain HTTP) logs but does not surface an error to the user. The
  // canonical fallback would be a hidden `<textarea>` + execCommand,
  // but the existing app targets modern Vite-served origins where
  // ``navigator.clipboard`` is reliably available.
  //
  // ``handleGetResults`` builds a templated chat prompt and
  // dispatches a ``CustomEvent('chat-input:set', { value })`` on
  // ``window``. ``ChatCanvas`` listens for that event and stuffs the
  // value into its local input state, ready for the user to review
  // and press Enter. Intentionally NO auto-send — surprising the
  // user by submitting prompts on their behalf would erode trust in
  // the row click. The trio_id is cleaned for display: drop the
  // gateway-namespace prefix (everything before the last ``|``) and
  // turn underscores into spaces so ``audio-stem-separation|stem_separation``
  // renders as ``stem separation`` in the prompt body.
  const handleCopyJobId = (jobId: string): void => {
    if (typeof navigator === 'undefined' || !navigator.clipboard) return;
    navigator.clipboard.writeText(jobId).catch((err) => {
      console.error('SessionSidebar: copy job id failed:', err);
    });
  };

  const handleGetResults = (job: Job): void => {
    const cleanTrio = (job.trioId.split('|').pop() ?? job.trioId).replace(/_/g, ' ');
    const prompt = `Get the results for the ${cleanTrio} job: ${job.jobId}`;
    if (typeof window === 'undefined') return;
    window.dispatchEvent(
      new CustomEvent('chat-input:set', { detail: { value: prompt } }),
    );
  };

  const beginRename = (id: string, currentTitle: string) => {
    setEditingId(id);
    setDraftTitle(currentTitle);
  };
  const cancelRename = () => {
    setEditingId(null);
    setDraftTitle('');
  };
  const commitRename = () => {
    if (!editingId || !onRenameSession) return cancelRename();
    const trimmed = draftTitle.trim();
    if (trimmed.length === 0) return cancelRename();
    onRenameSession(editingId, trimmed.slice(0, 80));
    cancelRename();
  };

  if (collapsed) {
    return (
      <div ref={containerRef} style={collapsedRailStyle}>
        {/* In compact mode, the logo expands the sidebar. */}
        <button onClick={onToggleCollapse} style={collapsedLogoBtnStyle} title="Expand sidebar" aria-label="Expand sidebar">
          <img src={getActiveLogo().src} alt="" style={{ ...collapsedLogoStyle, borderRadius: getActiveLogo().borderRadius }} />
        </button>

        {/* Action: + New — morphs with the active Center_Mode (Req 1.4, 1.6),
            matches expanded position (below logo, above nav). Hidden entirely in
            read-only centers: rendering it with no handler leaves a dead control, and
            falling through to the chat default would create a session instead. */}
        {hasNewItemAction && (
          <button onClick={onNewItemAction} style={railIconBtnStyle} title={newItemLabel} aria-label={newItemLabel}>
            <NewChatIcon />
          </button>
        )}

        {/* Nav: Chat / Projects / Workflows / Agents / Skills — the same order as the expanded categories */}
        <button onClick={onGoChat} style={centerMode === 'chat' ? railIconActiveBtnStyle : railIconBtnStyle} title="Chat" aria-label="Chat">
          <AgentIcon />
        </button>
        {projectsEnabled && onOpenProjects && (
          <button onClick={onOpenProjects} style={centerMode === 'projects' ? railIconActiveBtnStyle : railIconBtnStyle} title="Projects" aria-label="Projects">
            <ProjectsIcon />
          </button>
        )}
        <button onClick={onOpenWorkflows} style={{ ...(centerMode === 'activity' ? railIconActiveBtnStyle : railIconBtnStyle), position: 'relative' as const }} title="Workflow" aria-label="Workflow">
          <WorkflowIcon />
          {activityBadge && <span style={badgeDotStyle} />}
        </button>
        <button onClick={onOpenCustomAgents} style={centerMode === 'custom-agent' ? railIconActiveBtnStyle : railIconBtnStyle} title="Agents" aria-label="Agents">
          <ConnectionsIcon />
        </button>
        <button onClick={onOpenSkills} style={centerMode === 'skills' ? railIconActiveBtnStyle : railIconBtnStyle} title="Skills" aria-label="Skills">
          <SkillIcon />
        </button>
        {/* Spacer pushes the lower group to the bottom so it matches the
            expanded view, where Settings lives in the lower menu (above the
            user row) rather than up with the primary nav. */}
        <div style={{ flex: 1 }} />

        {/* Bottom: Settings. The rail has no per-tab icons — Settings opens the modal and you pick
            the tab there. (A separate "Capabilities" button opened the same window on the same tab:
            IND403 asked why it was not just called Settings.) Improve follows General/Model/Tools/Hooks,
            so it has no icon of its own; its badge rides here instead, or it would be invisible when
            collapsed. */}
        <button onClick={() => onOpenSettings()} style={{ ...railIconBtnStyle, position: 'relative' as const }} title="Settings" aria-label="Settings">
          <SettingsIcon />
          {improveBadge && <span style={badgeDotStyle} />}
        </button>

        {/* User — mirrors the expanded lower user row so the collapsed rail shows the SAME
            bottom items (identity + click-to-sign-out), not just Settings. */}
        <button
          type="button"
          data-testid="rail-user"
          onClick={() => { void currentUser.signOut(); }}
          style={railIconBtnStyle}
          title={
            currentUser.mode === 'anonymous'
              ? 'Not signed in'
              : `${currentUser.displayName}${currentUser.email ? ` <${currentUser.email}>` : ''} — click to sign out`
          }
          aria-label={
            currentUser.mode === 'anonymous'
              ? 'Not signed in'
              : `Signed in as ${currentUser.displayName}. Click to sign out.`
          }
        >
          <div style={avatarStyle}>{currentUser.initials}</div>
        </button>
      </div>
    );
  }

  return (
    <div ref={containerRef} style={sidebarStyle}>
      {/* Hover-reveal styles for the row action icons. Inline styles can't
          express :hover, so we attach a small scoped stylesheet. The row
          itself carries `data-session-row`; the actions container carries
          `data-row-actions`. They show on hover or keyboard focus — the active row
          included, so the list reads as names until you reach for a row.

          The `.job-row-highlight` keyframes drive the temporary flash that
          `scrollToJob` (the imperative handle) applies to the matching row
          when the inline chip is clicked (task 13.3). Defined here so the
          rule lives next to the markup that uses it. */}
      <style>{`
        [data-session-row] [data-row-actions],
        [data-job-row] [data-row-actions],
        [data-run-row] [data-row-actions] {
          opacity: 0;
          transition: opacity 120ms ease;
          pointer-events: none;
        }
        @media (hover: none) {
          [data-session-row] [data-row-actions], [data-job-row] [data-row-actions], [data-run-row] [data-row-actions] {
            opacity: 1; pointer-events: auto;
          }
        }
        [data-session-row]:hover [data-row-actions],
        [data-session-row]:focus-within [data-row-actions],
        [data-job-row]:hover [data-row-actions],
        [data-job-row]:focus-within [data-row-actions],
        [data-run-row]:hover [data-row-actions],
        [data-run-row]:focus-within [data-row-actions] {
          opacity: 1;
          pointer-events: auto;
        }
        [data-session-row] [data-row-actions] button:hover,
        [data-job-row] [data-row-actions] button:hover,
        [data-run-row] [data-row-actions] button:hover {
          background: var(--color-sidebar-hover);
          color: ${theme.colors.sidebarText};
        }
        [data-list-pager] button:hover {
          background: var(--color-sidebar-hover);
          color: ${theme.colors.sidebarText};
        }
        [data-job-id].job-row-highlight {
          animation: job-row-flash 1.5s ease;
        }
        @keyframes job-row-flash {
          0%   { background: ${theme.colors.sidebarActiveBg}; }
          70%  { background: ${theme.colors.sidebarActiveBg}; }
          100% { background: transparent; }
        }
      `}</style>

      {/* Header: logo toggles the sidebar; the brand name opens the splash. */}
      <div style={headerStyle}>
        <button
          type="button"
          onClick={onToggleCollapse}
          style={headerLogoBtnStyle}
          title="Collapse sidebar"
          aria-label="Collapse sidebar"
        >
          <img src={getActiveLogo().src} alt="" style={{ ...logoStyle, borderRadius: getActiveLogo().borderRadius }} />
        </button>
        <button
          type="button"
          onClick={onShowLanding}
          style={brandHomeBtnStyle}
          title="View the Media Agent Harness overview"
          aria-label="View the Media Agent Harness overview"
        >
          {BRAND_NAME}
        </button>
      </div>

      {/* New — morphs with the active category */}
      <div style={{ padding: '6px 12px 10px' }}>
        <button onClick={onNewItemAction} style={newBtnStyle}>
          <PlusIcon /> <span>{newItemLabel}</span>
        </button>
      </div>

      {/* Categories */}
      <div style={catsScrollStyle}>
        <button onClick={onGoChat} style={centerMode === 'chat' ? catHeaderActiveStyle : catHeaderStyle}>
          <AgentIcon /> <span style={{ flex: 1, textAlign: 'left' }}>Chat</span>
          <span style={catCountStyle}>{sessions.length}</span>
        </button>

      {/* Jobs section (async-job-tracking, R6.1–R6.4). Rendered above
          Conversations per the design's UI Components mockup. The header
          shows the visible-count and toggles expand/collapse; rows carry
          `data-job-id` so the imperative `scrollToJob` handle can locate
          and flash them. The whole section is hidden when zero jobs are
          visible to keep the sidebar tidy when the feature is unused. */}
      {/* Jobs moved to the right panel (Renderer | Jobs). Kept here disabled. */}
      {false && jobs.length > 0 && (
        <div style={jobsSectionStyle}>
          <button
            type="button"
            onClick={() => setJobsExpanded(v => !v)}
            style={jobsHeaderBtnStyle}
            aria-expanded={jobsExpanded}
            aria-controls="jobs-section-list"
            title={jobsExpanded ? 'Collapse Jobs' : 'Expand Jobs'}
          >
            <span style={sectionLabelStyle}>Jobs ({jobs.length})</span>
            <span style={{
              marginLeft: 'auto',
              fontSize: 'var(--fs-3xs)',
              color: theme.colors.sidebarTextSecondary,
              transform: jobsExpanded ? 'rotate(180deg)' : 'rotate(0deg)',
              transition: '150ms ease',
            }}>▼</span>
          </button>
          {jobsExpanded && (
            <div id="jobs-section-list" style={{ padding: '0 4px 4px' }}>
              {jobs.map(job => (
                <div
                  key={job.jobId}
                  data-job-id={job.jobId}
                  data-job-row=""
                  style={jobRowStyle}
                  title={`${job.trioId} · ${job.status} · started ${job.startAt}`}
                >
                  <span style={jobIndicatorStyle} aria-hidden="true">
                    {jobStatusIndicator(job.status)}
                  </span>
                  <span style={jobTrioStyle}>{job.trioId}</span>
                  <span style={jobMetaSeparatorStyle}>·</span>
                  <span style={jobStatusTextStyle}>{job.status}</span>
                  <span style={jobMetaSeparatorStyle}>·</span>
                  <span style={jobTimeStyle}>{formatRelativeTime(job.startAt, now)}</span>
                  {/* Hover-revealed action affordances. The container
                      reuses the existing `[data-row-actions]` opacity
                      pattern from the conversation rows above (see the
                      style block at the top of this component); the
                      `[data-job-row]` selector was added to the same
                      rule so jobs and conversations share one CSS
                      contract. The container is absolute-positioned
                      over the right side of the row so the buttons
                      overlay the relative-time text without forcing a
                      layout shift when they appear. */}
                  <div data-row-actions="" style={jobRowActionsStyle}>
                    <button
                      type="button"
                      onClick={(e) => { e.stopPropagation(); handleCopyJobId(job.jobId); }}
                      style={iconBtnStyle}
                      title="Copy job ID"
                      aria-label="Copy job ID"
                    >
                      <CopyIcon />
                    </button>
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        if (job.sessionId) onSelectSession(job.sessionId);
                      }}
                      style={iconBtnStyle}
                      title="Open spawning session"
                      aria-label="Open spawning session"
                    >
                      <OpenSessionIcon />
                    </button>
                    {job.status === 'SUCCEEDED' && (
                      <button
                        type="button"
                        onClick={(e) => { e.stopPropagation(); handleGetResults(job); }}
                        style={iconBtnStyle}
                        title="Fill chat input with a 'get results' prompt"
                        aria-label="Get results"
                      >
                        <ResultsIcon />
                      </button>
                    )}
                    {(job.status === 'FAILED' || job.status === 'TIMED_OUT' || job.status === 'ABORTED') && (
                      <button
                        type="button"
                        // No onClick — the native `title` attribute is
                        // the affordance. A click could surface a
                        // popover later; for now hovering reveals the
                        // recorded last error (or a generic message
                        // when the registry has none).
                        style={iconBtnStyle}
                        title={job.lastError ?? `Job ${job.status.toLowerCase().replace('_', ' ')}`}
                        aria-label={`Job ${job.status.toLowerCase().replace('_', ' ')} — hover for error`}
                      >
                        <ErrorIcon />
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Chat body: conversations (only when Chat is the open category) */}
      {centerMode === 'chat' && (<>
      {/* Session List */}
      <div style={listStyle}>
        {visibleSessions.map(s => {
              const isEditing = editingId === s.id;
              const isActive = s.id === activeSessionId;
              return (
                <div
                  key={s.id}
                  data-session-row=""
                  data-active={isActive ? 'true' : 'false'}
                  onClick={() => { if (!isEditing) onSelectSession(s.id); }}
                  onDoubleClick={(e) => {
                    if (!onRenameSession) return;
                    e.stopPropagation();
                    beginRename(s.id, s.title || '');
                  }}
                  style={isActive ? activeItemStyle : itemStyle}
                  title={isEditing ? undefined : s.title}
                >
                  <span style={{ color: theme.colors.sidebarTextSecondary, display: 'flex', flexShrink: 0, opacity: 0.65 }}><ChatIcon /></span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    {isEditing ? (
                      <input
                        autoFocus
                        value={draftTitle}
                        onChange={(e) => setDraftTitle(e.target.value)}
                        onClick={(e) => e.stopPropagation()}
                        onBlur={commitRename}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') { e.preventDefault(); commitRename(); }
                          else if (e.key === 'Escape') { e.preventDefault(); cancelRename(); }
                        }}
                        style={renameInputStyle}
                        maxLength={80}
                      />
                    ) : (
                      <>
                        <div style={itemTitleStyle}>
                          {s.locked && (
                            <span style={lockBadgeStyle} title="Locked" aria-label="Locked">
                              <LockIcon size={11} />
                            </span>
                          )}
                          {s.title || `Session ${s.id.slice(0, 6)}`}
                        </div>
                        {s.preview && (
                          <div style={itemPreviewStyle}>{s.preview}</div>
                        )}
                      </>
                    )}
                  </div>
                  {!isEditing && (
                    <div data-row-actions="" style={rowActionsStyle}>
                      {onToggleLock && (
                        <button
                          onClick={(e) => { e.stopPropagation(); onToggleLock(s.id, !s.locked); }}
                          style={s.locked ? lockedToggleBtnStyle : iconBtnStyle}
                          aria-label={s.locked ? `Unlock session ${s.title}` : `Lock session ${s.title}`}
                          aria-pressed={!!s.locked}
                          title={s.locked ? 'Unlock session' : 'Lock session'}
                        >
                          <LockIcon open={!s.locked} />
                        </button>
                      )}
                      {onRenameSession && (
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            if (s.locked) return;
                            beginRename(s.id, s.title || '');
                          }}
                          style={s.locked ? disabledIconBtnStyle : iconBtnStyle}
                          disabled={!!s.locked}
                          aria-label={`Rename session ${s.title}`}
                          title={s.locked ? 'Locked — unlock to rename' : 'Rename'}
                        >
                          <PencilIcon />
                        </button>
                      )}
                      {onDeleteSession && (
                        <button
                          onClick={(e) => { e.stopPropagation(); if (s.locked) return; onDeleteSession(s.id); }}
                          style={s.locked ? disabledIconBtnStyle : deleteSessionBtnStyle}
                          disabled={!!s.locked}
                          aria-label={`Delete session ${s.title}`}
                          title={s.locked ? 'Locked — unlock to delete' : 'Delete'}
                        >
                          <TrashIcon />
                        </button>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
        {pagerFor('chat', orderedSessions.length, visibleSessions.length)}
        {sessions.length === 0 && (
          <div style={{ padding: '16px', color: theme.colors.sidebarTextSecondary, fontSize: 'var(--fs-xs)', textAlign: 'center' }}>
            No sessions yet
          </div>
        )}
      </div>
      </>)}

        {/* Projects category — second, directly below Chat (media-harness-projects-access task 13.7, Req 10.1). Gated on
            `projectsEnabled` + `onOpenProjects`. */}
        {projectsEnabled && onOpenProjects && (
          <button onClick={onOpenProjects} style={centerMode === 'projects' ? catHeaderActiveStyle : catHeaderStyle}>
            <ProjectsIcon /> <span style={{ flex: 1, textAlign: 'left' }}>Projects</span>
            {showProjectInventory && projectList && (
              <span style={catCountStyle}>{projectList.length}</span>
            )}
          </button>
        )}

        {/* Projects body: the Actor's projects, Personal first, as the Agents and Skills blocks list
            theirs — only while the category is open. This IS the Projects inventory; the center surface
            shows the selected Project full width. Rows come from `useProjectsList`, the store the surface
            refreshes after every change. */}
        {showProjectInventory && (<>
          <div style={{ ...listStyle, marginTop: 4 }} data-testid="project-inventory">
            {visibleProjects.map(p => {
              const isActive = p.projectId === activeProjectId;
              return (
                <div
                  key={p.projectId}
                  data-project-row=""
                  data-active={isActive ? 'true' : 'false'}
                  role="button"
                  tabIndex={0}
                  aria-current={isActive ? 'true' : undefined}
                  aria-label={p.personal ? `${p.name} (private), ${p.role}` : `${p.name}, ${p.role}`}
                  onClick={() => onSelectProject?.(p.projectId)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelectProject?.(p.projectId); }
                  }}
                  style={isActive ? activeItemStyle : itemStyle}
                  title={p.name}
                >
                  <span style={{ color: theme.colors.sidebarTextSecondary, display: 'flex', flexShrink: 0, opacity: 0.65 }}>
                    {p.personal ? <LockIcon size={16} /> : <FolderIcon />}
                  </span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <NameWithIndexGlyph name={p.name} indexing={p.indexing} style={itemTitleStyle} glyphColor={theme.colors.sidebarTextSecondary} />
                  </div>
                  {/* Only a project you joined says so; yours (most of them) carry no badge. */}
                  {p.role !== 'owner' && <span style={projectRoleBadgeStyle}>{p.role}</span>}
                </div>
              );
            })}
            {pagerFor('projects', orderedProjects.length, visibleProjects.length)}
            {projectList === null && (
              <div style={{ padding: '16px', color: theme.colors.sidebarTextSecondary, fontSize: 'var(--fs-xs)', textAlign: 'center' }}>
                Loading projects…
              </div>
            )}
          </div>
        </>)}

        <button onClick={onOpenWorkflows} style={{ ...(centerMode === 'activity' ? catHeaderActiveStyle : catHeaderStyle), position: 'relative' as const }}>
          <WorkflowIcon /> <span style={{ flex: 1, textAlign: 'left' }}>Workflow</span>
          {runRows.length > 0 && <span style={catCountStyle}>{runRows.length}</span>}
          {activityBadge && <span style={badgeDotStyle} />}
        </button>

        {/* Workflow is a nav-only header: all flow detail lives in the center
            aggregate view (every flow + status in one place, drill-in on
            click). The sidebar deliberately does NOT re-list individual flows. */}

        {/* Agents category — top-level Custom Agent center mode (peer to
            Chat/Activity). Kept from our side: routing to the center mode is
            what surfaces the published read-only view + test console; staging's
            variant opened the Settings agents sub-tab instead. */}
        <button onClick={onOpenCustomAgents} style={centerMode === 'custom-agent' ? catHeaderActiveStyle : catHeaderStyle}>
          <ConnectionsIcon /> <span style={{ flex: 1, textAlign: 'left' }}>Agents</span>
          {centerMode === 'custom-agent' && (
            <span style={catCountStyle}>{(customAgents ?? []).length}</span>
          )}
        </button>

        {/* Agents body: the Owner's Custom_Agent assets (only when the
            Custom Agent category is open). Mirrors the Chat conversations
            block above — each row shows the agent's name, lifecycle state,
            and last-modified time (Req 1.2). When the Owner owns none, an
            empty-state message prompts them to create one (Req 1.3). */}
        {centerMode === 'custom-agent' && (<>
          <div style={{ ...listStyle, marginTop: 4 }}>
            {visibleAgents.map(agent => {
              const isActive = agent.id === activeCustomAgentId;
              return (
                <div
                  key={agent.id}
                  data-agent-row=""
                  data-active={isActive ? 'true' : 'false'}
                  onClick={() => onSelectCustomAgent?.(agent.id)}
                  style={isActive ? activeItemStyle : itemStyle}
                  title={agent.name}
                >
                  <span style={{ color: theme.colors.sidebarTextSecondary, display: 'flex', flexShrink: 0, opacity: 0.65 }}><ConnectionsIcon /></span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={itemTitleStyle}>{agent.name || `Agent ${agent.id.slice(0, 6)}`}</div>
                    <div style={itemPreviewStyle}>
                      {formatLifecycleState(agent.lifecycleState)}
                      {typeof agent.lastModified === 'number'
                        ? ` · ${formatRelativeTime(new Date(agent.lastModified).toISOString(), now)}`
                        : ''}
                    </div>
                  </div>
                </div>
              );
            })}
            {pagerFor('agents', (customAgents ?? []).length, visibleAgents.length)}
            {(customAgents ?? []).length === 0 && (
              <div style={{ padding: '16px', color: theme.colors.sidebarTextSecondary, fontSize: 'var(--fs-xs)', textAlign: 'center' }}>
                No custom agents yet. Create one to get started.
              </div>
            )}
          </div>
        </>)}

        {/* Skills category — peer to Agents. Promoted out of Settings so the
            capability an agent loads is authored in the main app. */}
        <button onClick={onOpenSkills} style={centerMode === 'skills' ? catHeaderActiveStyle : catHeaderStyle}>
          <SkillIcon /> <span style={{ flex: 1, textAlign: 'left' }}>Skills</span>
          {centerMode === 'skills' && (
            <span style={catCountStyle}>{(skills ?? []).length}</span>
          )}
        </button>

        {/* Skills body: the Owner's skill drafts. Mirrors the Agents block —
            only rendered while the category is open, so the sidebar does not
            carry four inventories at once. */}
        {centerMode === 'skills' && (<>
          <div style={{ ...listStyle, marginTop: 4 }}>
            {visibleSkills.map(skill => {
              const isActive = skill.assetId === activeSkillId;
              return (
              <div
                key={skill.assetId}
                data-skill-row=""
                data-active={isActive ? 'true' : 'false'}
                onClick={() => onSelectSkill?.(skill.assetId)}
                style={isActive ? activeItemStyle : itemStyle}
                title={skill.name}
              >
                <span style={{ color: theme.colors.sidebarTextSecondary, display: 'flex', flexShrink: 0, opacity: 0.65 }}><SkillIcon /></span>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={itemTitleStyle}>{skill.name || `Skill ${skill.assetId.slice(0, 6)}`}</div>
                  <div style={itemPreviewStyle}>
                    {/* Which state, then when — as the Agents rows above do. A bare
                        timestamp could not tell a working copy from a published
                        version, and the two do different things when clicked. */}
                    {skill.publishedVersion !== undefined
                      ? `Published v${skill.publishedVersion}`
                      : 'Draft'}
                    {typeof skill.updatedAt === 'number'
                      ? ` · ${formatRelativeTime(new Date(skill.updatedAt).toISOString(), now)}`
                      : ''}
                  </div>
                </div>
              </div>
              );
            })}
            {pagerFor('skills', (skills ?? []).length, visibleSkills.length)}
            {(skills ?? []).length === 0 && (
              <div style={{ padding: '16px', color: theme.colors.sidebarTextSecondary, fontSize: 'var(--fs-xs)', textAlign: 'center' }}>
                No skills yet. Create one to get started.
              </div>
            )}
          </div>
        </>)}

      </div>

      {/* Lower menu */}
      <div style={lowerMenuStyle} data-tour="settings">
        {/* Collapsed by default (remembered): every entry below is also a tab of the Settings this heading
            opens, so the list is a shortcut, not the only way in — and it was half the sidebar. */}
        <div style={{ display: 'flex', alignItems: 'center' }}>
          <button onClick={() => onOpenSettings()} style={{ ...lowerHeadStyle, flex: 1, position: 'relative' as const }}>
            <SettingsIcon /> <span>Settings</span>
            {!lowerOpen && improveBadge && <span style={{ ...badgeDotStyle, top: '50%', right: 10, marginTop: -3.5 }} />}
          </button>
          <button type="button" onClick={toggleLower} aria-expanded={lowerOpen} data-testid="sidebar-lower-toggle"
            aria-label={lowerOpen ? 'Hide settings shortcuts' : 'Show settings shortcuts'}
            title={lowerOpen ? 'Hide shortcuts' : 'Show shortcuts'} style={lowerToggleStyle}>
            <span style={{ display: 'inline-flex', transform: lowerOpen ? 'none' : 'rotate(-90deg)', transition: 'transform 0.12s ease' }}>
              <ChevronDownIcon />
            </span>
          </button>
        </div>
        {lowerOpen && <>
        <button onClick={() => onOpenSettings('general')} style={lowerSubStyle}>
          <GeneralIcon /> <span>General</span>
        </button>
        <button onClick={() => onOpenSettings('model')} style={lowerSubStyle}>
          <ModelIcon /> <span>Model</span>
        </button>
        {/* Users sits directly below Model, admin-only (same gate as Gateways). */}
        {isGatewayAdmin && (
          <button onClick={() => onOpenSettings('users')} style={lowerSubStyle}>
            <UsersIcon /> <span>Users</span>
          </button>
        )}
        <button onClick={() => onOpenSettings('tools')} style={lowerSubStyle}>
          {/* Not ConnectionsIcon — that glyph already means "Agents" in this
              same sidebar (rail button, expanded row, and per-agent rows). */}
          <CustomizeIcon /> <span>Tools</span>
        </button>
        <button onClick={() => onOpenSettings('hooks')} style={lowerSubStyle}>
          <BellIcon /> <span>Hooks</span>
        </button>
        {/* Improve is a review surface you switch on and read, not a place you work, so it
            sits with the settings-class entries. Before Gateways and outside the admin gate,
            so its position is the same for every user. */}
        <button onClick={onOpenImprove} style={{ ...lowerSubStyle, position: 'relative' as const }}>
          <ImproveIcon /> <span style={{ flex: 1, textAlign: 'left' }}>Improve</span>
          {improveBadge && <span style={{ ...badgeDotStyle, top: '50%', right: 10, marginTop: -3.5 }} />}
        </button>
        {/* Admin-only, so it is not part of the four entries above and sits
            last rather than interleaved. */}
        {isGatewayAdmin && (
          <button onClick={() => onOpenSettings('gateways')} style={lowerSubStyle}>
            <GatewayIcon /> <span>Gateways</span>
          </button>
        )}
        </>}
      </div>

      {/* User — shows the signed-in identity (Cognito ID-token claims, or
          the dev shim's synthetic id when VITE_DEV_BYPASS_AUTH is set).
          Clicking signs out: hosted-UI logout for real sessions, dev-token
          clear for dev sessions. */}
      <button
        type="button"
        style={userRowBtnStyle}
        onClick={() => { void currentUser.signOut(); }}
        title={
          currentUser.mode === 'anonymous'
            ? 'Not signed in'
            : `${currentUser.displayName}${currentUser.email ? ` <${currentUser.email}>` : ''} — click to sign out`
        }
        aria-label={
          currentUser.mode === 'anonymous'
            ? 'Not signed in'
            : `Signed in as ${currentUser.displayName}. Click to sign out.`
        }
      >
        <div style={avatarStyle}>{currentUser.initials}</div>
        <span style={userLabelStyle}>
          {currentUser.displayName}
          {currentUser.mode === 'dev' ? (
            <span style={userBadgeStyle}>dev</span>
          ) : null}
        </span>
        {/* The release, quietly: the first thing to read off a screenshot of a bug. */}
        {appVersion && (
          <span style={versionStyle} title={`Version ${appVersion.full}`} data-testid="app-version">
            v{appVersion.short}
          </span>
        )}
      </button>
    </div>
  );
});

export default SessionSidebar;

// --- Job indicators / formatters ---

/**
 * Map a JobStatus to the completion-indicator emoji rendered alongside
 * the row. Active jobs (PENDING / RUNNING) render an hourglass; each
 * terminal status gets a distinctive glyph that matches the design's UI
 * Components mockup.
 */
function jobStatusIndicator(status: JobStatus): string {
  switch (status) {
    case 'SUCCEEDED': return '✅';
    case 'FAILED':    return '❌';
    case 'TIMED_OUT': return '⏱';
    case 'ABORTED':   return '🛑';
    case 'PENDING':
    case 'RUNNING':
    default:          return '⏳';
  }
}

/**
 * Render an ISO 8601 timestamp as a coarse "Xs ago" / "Xm ago" / "Xh
 * ago" / "Xd ago" string. Returns the raw string when the timestamp
 * fails to parse so we never silently swallow a malformed payload, and
 * "now" for any future-dated stamp (clock skew between proxy and
 * client) so the row doesn't read "−5s ago".
 */
function formatRelativeTime(iso: string, now: number): string {
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return iso;
  const diffSec = Math.floor((now - ms) / 1000);
  if (diffSec < 5) return 'now';
  if (diffSec < 60) return `${diffSec}s ago`;
  const min = Math.floor(diffSec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.floor(hr / 24);
  return `${day}d ago`;
}

/**
 * Present a Custom_Agent's lifecycle state as a short, capitalized
 * label for the sidebar row (Req 1.2). Falls back to "Draft" when the
 * parent hasn't supplied a state, matching the initial lifecycle state
 * of a newly created agent.
 */
function formatLifecycleState(state?: string): string {
  const s = (state ?? '').trim();
  if (s.length === 0) return 'Draft';
  return s.charAt(0).toUpperCase() + s.slice(1);
}

// --- Icons (simple SVG) ---

function NewChatIcon() {
  // "Compose" icon — used for the + New action button. Keeps the historical
  // pencil-on-card look so users with the muscle memory still recognize it.
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <rect x="2" y="2" width="12" height="12" rx="2" />
      <path d="M5 8h6M8 5v6" />
    </svg>
  );
}

function ChatIcon() {
  // Speech-bubble icon for the "Chat" navigation item.
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round">
      <path d="M2 4a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H6l-3 2.5V12H4a2 2 0 0 1-2-2V4z" />
    </svg>
  );
}

function PlusIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
      <path d="M7 2v10M2 7h10" />
    </svg>
  );
}

function PencilIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <path d="M8.5 1.5l2 2L4 10H2v-2l6.5-6.5z" />
    </svg>
  );
}

// ---- Job-row action icons ----
//
// Single-stroke 14px outlines, matching the existing PencilIcon style.
// Each icon is purely decorative; the meaningful affordance is the
// button's `title`/`aria-label`.

function CopyIcon() {
  // Two stacked squares — the standard "copy to clipboard" glyph.
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <rect x="4" y="4" width="8" height="8" rx="1.5" />
      <path d="M2 10V3a1 1 0 0 1 1-1h7" />
    </svg>
  );
}

function OpenSessionIcon() {
  // Speech-bubble + arrow: "go to the conversation that started this".
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2 3.5a1.5 1.5 0 0 1 1.5-1.5h7A1.5 1.5 0 0 1 12 3.5v5A1.5 1.5 0 0 1 10.5 10H6l-2.5 2v-2H3.5A1.5 1.5 0 0 1 2 8.5v-5z" />
      <path d="M5.5 6.5h3M7 5l1.5 1.5L7 8" />
    </svg>
  );
}

function ResultsIcon() {
  // Download/tray glyph — "fetch results" affordance for completed
  // jobs. Visually distinct from the chat-bubble of OpenSessionIcon
  // and the rectangle of CopyIcon.
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <path d="M7 2v7M4 6.5L7 9.5l3-3" />
      <path d="M2.5 11h9" />
    </svg>
  );
}

function ErrorIcon() {
  // Filled-circle "!" — read-only affordance whose tooltip carries
  // the recorded `lastError`.
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="7" cy="7" r="5" />
      <path d="M7 4.5v3" />
      <circle cx="7" cy="9.5" r="0.5" fill="currentColor" />
    </svg>
  );
}

function ActivityIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <polyline points="2,10 5,6 8,9 11,4 14,7" />
    </svg>
  );
}

function SessionsIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <rect x="3" y="2" width="10" height="3" rx="1" />
      <rect x="3" y="7" width="10" height="3" rx="1" />
      <rect x="3" y="12" width="10" height="2" rx="1" />
    </svg>
  );
}

// A book/manual glyph: a skill is authored knowledge the agent loads. NOT
// ConnectionsIcon (means Agents in this same sidebar) and not SettingsIcon.
function SkillIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" />
      <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" />
    </svg>
  );
}

// A folder glyph: a Project is a collection the user owns and shares. The shared
// `FolderIcon`, so the nav entry, the inventory rows, the composer chip, and the
// Projects surface all draw the same glyph. Distinct from every other sidebar glyph —
// NOT ConnectionsIcon (Agents), SettingsIcon (Settings), SkillIcon (Skills),
// WorkflowIcon (Workflow), or AgentIcon (Chat).
function ProjectsIcon() {
  return <FolderIcon />;
}

function WorkflowIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="6" cy="6" r="2.4" /><circle cx="6" cy="18" r="2.4" /><circle cx="18" cy="9" r="2.4" />
      <path d="M6 8.4v7.2M6 12h6a4 4 0 0 0 4-4" />
    </svg>
  );
}


function SettingsIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <circle cx="8" cy="8" r="2" />
      <path d="M8 1v2M8 13v2M1 8h2M13 8h2M3 3l1.5 1.5M11.5 11.5L13 13M13 3l-1.5 1.5M4.5 11.5L3 13" />
    </svg>
  );
}

function ConnectionsIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <circle cx="4" cy="4" r="2" /><circle cx="12" cy="4" r="2" /><circle cx="8" cy="12" r="2" />
      <path d="M4 6v2l4 4M12 6v2l-4 4" />
    </svg>
  );
}

function ModelIcon() {
  // A processor/chip, deliberately not another rounded-rect-with-lines:
  // GeneralIcon is a panel with text lines, and the two were near-identical
  // at 16px.
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <rect x="5" y="5" width="6" height="6" rx="1" />
      <path d="M8 2v3M8 11v3M2 8h3M11 8h3" />
    </svg>
  );
}

function GatewayIcon() {
  // Two stacked nodes connected by a horizontal bridge — evokes the
  // gateway/proxy mental model (one client, one upstream, one bridge).
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
      <circle cx="3" cy="8" r="1.6" />
      <circle cx="13" cy="8" r="1.6" />
      <path d="M4.6 8h6.8" />
      <path d="M6.5 5.5h3M6.5 10.5h3" />
    </svg>
  );
}

function UsersIcon() {
  // Two figures — the standard people/users glyph.
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="6" cy="5.5" r="2.2" />
      <path d="M2.2 13c0-2.2 1.7-3.6 3.8-3.6s3.8 1.4 3.8 3.6" />
      <path d="M10.5 3.6a2.2 2.2 0 0 1 0 4.2" />
      <path d="M11 9.5c1.8.2 3 1.5 3 3.5" />
    </svg>
  );
}

function CustomizeIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M4 3v10M8 3v10M12 3v10" />
      <circle cx="4" cy="6" r="1.5" fill="currentColor" /><circle cx="8" cy="10" r="1.5" fill="currentColor" /><circle cx="12" cy="5" r="1.5" fill="currentColor" />
    </svg>
  );
}

function GeneralIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <rect x="2" y="3" width="12" height="10" rx="2" />
      <path d="M5 6h6M5 9h3" />
    </svg>
  );
}

// --- Helpers ---

/** How many rows a sidebar category shows before its pager, and how many each "Show more" adds. */
export const SIDEBAR_LIST_PAGE = 15;
type SidebarListId = 'chat' | 'projects' | 'agents' | 'skills';

/**
 * The first `limit` items, plus the active one when it sits beyond them — so selecting an old session (from
 * a link, or a search elsewhere) never leaves the sidebar without its highlighted row.
 */
export function capWithActive<T>(items: T[], limit: number, isActive: (item: T) => boolean): T[] {
  if (items.length <= limit) return items;
  const head = items.slice(0, limit);
  const active = items.slice(limit).find(isActive);
  return active ? [...head, active] : head;
}

/**
 * The quiet row under a capped list: "Show more" with the hidden count, and "Show less" once expanded.
 * Styled as a muted list row (same inset as the items), not a button, so it never competes with the rows.
 */
function ListPager({ total, shown, expanded, onMore, onLess }: {
  total: number; shown: number; expanded: boolean; onMore: () => void; onLess: () => void;
}) {
  const hidden = total - shown;
  if (hidden <= 0 && !expanded) return null;
  return (
    <div style={pagerRowStyle} data-list-pager="">
      {hidden > 0 && (
        <button type="button" onClick={onMore} style={pagerBtnStyle} aria-label={`Show ${Math.min(hidden, SIDEBAR_LIST_PAGE)} more`}>
          <ChevronDownIcon />
          <span>Show more</span>
          <span style={pagerCountStyle}>{hidden}</span>
        </button>
      )}
      {expanded && (
        <button type="button" onClick={onLess} style={{ ...pagerBtnStyle, marginLeft: 'auto' }}>
          <span>Show less</span>
        </button>
      )}
    </div>
  );
}

function ChevronDownIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
         strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="6 9 12 15 18 9" />
    </svg>
  );
}

export function orderSessions(sessions: Session[]): Session[] {
  // STABLE order by creation time (newest first). Derived from `timestamp` here
  // because in-place updates (preview/messageCount) don't re-sort the array.
  // Activity must NOT reorder — a session never jumps under the user when it is
  // clicked or messaged. Date bucketing (Today/Yesterday/Older) was removed as
  // unnecessary complexity.
  return [...sessions].sort((a, b) => b.timestamp - a.timestamp);
}

// --- Styles ---

const RAIL_WIDTH = 48;

const collapsedRailStyle: React.CSSProperties = {
  display: 'flex', flexDirection: 'column', alignItems: 'center',
  width: RAIL_WIDTH, height: '100%', padding: '12px 0',
  background: theme.colors.sidebarBg, borderRight: 'none',
  gap: 4,
};

const collapsedLogoBtnStyle: React.CSSProperties = {
  width: 36, height: 36, marginBottom: 8, padding: 4,
  display: 'grid', placeItems: 'center', flexShrink: 0,
  border: 'none', borderRadius: theme.radius.button,
  background: 'none', cursor: 'pointer',
};

const collapsedLogoStyle: React.CSSProperties = {
  width: 28, height: 28, borderRadius: theme.radius.button,
  objectFit: 'cover', display: 'block',
};

const railIconBtnStyle: React.CSSProperties = {
  width: 36, height: 36, borderRadius: theme.radius.button,
  // Explicit border longhands (NOT the `border: 'none'` shorthand): React's inline-style diffing
  // can leave a stray `border-top` longhand from a prior render when only the shorthand changes,
  // which resurfaces as a UA-default "2px outset" divider by the Skills icon. Longhands always
  // clear every side. (Same footgun the user row below works around.)
  borderWidth: 0, borderStyle: 'none', borderColor: 'transparent',
  background: 'none', cursor: 'pointer', display: 'flex', alignItems: 'center',
  justifyContent: 'center', color: theme.colors.sidebarTextSecondary,
};

const railIconActiveBtnStyle: React.CSSProperties = {
  ...railIconBtnStyle,
  background: theme.colors.sidebarActiveBg, color: theme.colors.sidebarAccent,
};

const badgeDotStyle: React.CSSProperties = {
  position: 'absolute',
  top: 4,
  right: 4,
  width: 7,
  height: 7,
  borderRadius: '50%',
  background: theme.colors.success,
  border: `1.5px solid ${theme.colors.sidebarBg}`,
};

const sidebarStyle: React.CSSProperties = {
  display: 'flex', flexDirection: 'column', height: '100%',
  background: theme.colors.sidebarBg, borderRight: 'none',
};

const headerStyle: React.CSSProperties = {
  padding: '10px 10px 10px 12px', display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0,
};

const headerLogoBtnStyle: React.CSSProperties = {
  width: 32, height: 32, padding: 4,
  display: 'grid', placeItems: 'center', flexShrink: 0,
  border: 'none', borderRadius: theme.radius.button,
  background: 'none', cursor: 'pointer',
};

const logoStyle: React.CSSProperties = {
  width: 24, height: 24, borderRadius: theme.radius.button, flexShrink: 0,
  objectFit: 'cover', display: 'block',
};

const brandHomeBtnStyle: React.CSSProperties = {
  minWidth: 0, flex: 1, padding: '6px 4px',
  border: 'none', borderRadius: theme.radius.button,
  background: 'none', cursor: 'pointer',
  color: theme.colors.sidebarText, textAlign: 'left',
  overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
  fontSize: 'var(--font-size-base)', fontWeight: theme.typography.weights.semibold,
};

const navItemBtnStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 8, width: '100%',
  padding: '8px 10px', background: 'none', border: 'none', borderRadius: theme.radius.button,
  cursor: 'pointer', fontSize: 'var(--font-size-base)', color: theme.colors.sidebarText, textAlign: 'left',
};

const navItemActiveBtnStyle: React.CSSProperties = {
  ...navItemBtnStyle,
  background: theme.colors.sidebarActiveBg, color: theme.colors.sidebarAccent,
  fontWeight: theme.typography.weights.medium,
};

const sectionLabelStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)', textTransform: 'uppercase', color: theme.colors.sidebarTextSecondary,
  letterSpacing: 0.5, fontWeight: theme.typography.weights.medium,
};

const recentsAddBtnStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 4,
  height: 22,
  padding: '0 8px',
  borderRadius: theme.radius.button,
  border: `1px solid var(--color-sidebar-hairline)`,
  background: 'var(--color-sidebar-hover)',
  color: theme.colors.sidebarText,
  cursor: 'pointer',
  fontSize: 'var(--fs-2xs)',
  fontWeight: theme.typography.weights.medium,
};

const listStyle: React.CSSProperties = { padding: '0 8px 0 12px', marginBottom: 4 };

const itemStyle: React.CSSProperties = {
  padding: '8px 10px', borderRadius: theme.radius.button, marginBottom: 1, cursor: 'pointer',
  whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
  display: 'flex', alignItems: 'center', gap: 4, position: 'relative',
};

const activeItemStyle: React.CSSProperties = {
  ...itemStyle,
  background: theme.colors.sidebarActiveBg,
  // Accent bar is painted as an INSET box-shadow, not a left border. A border —
  // even with the old `paddingLeft: 7` compensation — changes the row's box
  // geometry, and selection must never shift a row's content or perturb layout.
  // box-shadow is painted, not laid out, so an active row stays pixel-identical to
  // its inactive siblings (verified: every row's content sits at the same x
  // whether or not it is selected). Keeps `itemStyle`'s padding untouched.
  boxShadow: `inset 3px 0 0 ${theme.colors.sidebarAccent}`,
};

const rowActionsStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 2,
  flexShrink: 0,
};

const deleteSessionBtnStyle: React.CSSProperties = {
  width: 24, height: 24, borderRadius: theme.radius.button, border: 'none', background: 'none',
  color: theme.colors.sidebarTextSecondary, fontSize: 'var(--fs-md)', cursor: 'pointer',
  display: 'flex', alignItems: 'center', justifyContent: 'center',
  flexShrink: 0,
};

const iconBtnStyle: React.CSSProperties = {
  width: 24, height: 24, borderRadius: theme.radius.button, border: 'none', background: 'none',
  color: theme.colors.sidebarTextSecondary, cursor: 'pointer',
  display: 'flex', alignItems: 'center', justifyContent: 'center',
  flexShrink: 0,
};

// Lock affordances. `lockedToggleBtnStyle` uses the sidebar accent so a locked
// session's toggle reads as "active"; `disabledIconBtnStyle` dims rename/delete
// to the standard disabled treatment (still visible, not-allowed cursor);
// `lockBadgeStyle` is the subtle persistent glyph on a locked row's title.
const lockedToggleBtnStyle: React.CSSProperties = {
  ...iconBtnStyle,
  color: theme.colors.sidebarAccent,
};

const disabledIconBtnStyle: React.CSSProperties = {
  ...iconBtnStyle,
  opacity: 0.3,
  cursor: 'not-allowed',
};

const lockBadgeStyle: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  verticalAlign: 'middle',
  marginRight: 4,
  color: theme.colors.sidebarTextSecondary,
  opacity: 0.75,
};

const renameInputStyle: React.CSSProperties = {
  width: '100%',
  background: 'var(--color-sidebar-hover)',
  color: theme.colors.sidebarText,
  border: `1px solid ${theme.colors.sidebarAccent}`,
  borderRadius: theme.radius.button,
  padding: '4px 6px',
  fontSize: 'var(--font-size-base)',
  outline: 'none',
};

const itemTitleStyle: React.CSSProperties = {
  fontSize: 'var(--font-size-base)', color: theme.colors.sidebarText, whiteSpace: 'nowrap',
  overflow: 'hidden', textOverflow: 'ellipsis',
};

const itemPreviewStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)', color: theme.colors.sidebarTextSecondary, marginTop: 2,
  whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
  lineHeight: '1.3',
};

const pagerRowStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 4, marginTop: 2,
};

// Matches a list row's inset (itemStyle's 10px) so the chevron lines up under the row icons.
const pagerBtnStyle: React.CSSProperties = {
  display: 'inline-flex', alignItems: 'center', gap: 6, padding: '6px 10px',
  border: 'none', background: 'none', borderRadius: theme.radius.button, cursor: 'pointer',
  fontSize: 'var(--fs-2xs)', fontWeight: theme.typography.weights.medium,
  color: theme.colors.sidebarTextSecondary,
};

const pagerCountStyle: React.CSSProperties = {
  fontSize: 'var(--fs-3xs)', fontWeight: theme.typography.weights.semibold,
  background: 'var(--color-sidebar-hover)', borderRadius: theme.radius.pill, padding: '0 6px',
  lineHeight: '16px', minWidth: 18, textAlign: 'center',
};

const settingsAccordionStyle: React.CSSProperties = {
  borderTop: '1px solid var(--color-sidebar-hairline)', flexShrink: 0,
};

const settingsHeaderBtnStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 8, width: '100%',
  padding: '10px 16px', background: 'none', border: 'none',
  cursor: 'pointer', fontSize: 'var(--font-size-base)', color: theme.colors.sidebarTextSecondary, textAlign: 'left',
};

const settingsSubItemStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 8, width: '100%',
  padding: '7px 16px 7px 28px', background: 'none', border: 'none',
  cursor: 'pointer', fontSize: 'var(--font-size-base)', color: theme.colors.sidebarText, textAlign: 'left',
};

const userRowBtnStyle: React.CSSProperties = {
  // Visually identical to the previous `userRowStyle` div, but rendered as
  // a button so the whole row is keyboard-focusable and clicking signs out.
  padding: '10px 16px',
  borderTop: '1px solid var(--color-sidebar-hairline)',
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  flexShrink: 0,
  width: '100%',
  background: 'transparent',
  border: 'none',
  borderTopColor: 'var(--color-sidebar-hairline)',
  borderTopStyle: 'solid',
  borderTopWidth: 1,
  cursor: 'pointer',
  color: theme.colors.sidebarText,
  font: 'inherit',
  textAlign: 'left',
};

const versionStyle: React.CSSProperties = {
  marginLeft: 'auto', flexShrink: 0, fontSize: 'var(--fs-xs)', color: theme.colors.textTertiary,
  fontVariantNumeric: 'tabular-nums', whiteSpace: 'nowrap',
};
const userLabelStyle: React.CSSProperties = {
  fontSize: 'var(--font-size-base)',
  color: theme.colors.sidebarText,
  display: 'flex',
  alignItems: 'center',
  gap: 6,
  minWidth: 0,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
};

const userBadgeStyle: React.CSSProperties = {
  fontSize: 'var(--fs-3xs)',
  fontWeight: 700,
  textTransform: 'uppercase',
  letterSpacing: 0.5,
  color: theme.colors.onPrimary,
  background: theme.colors.sidebarAccent,
  borderRadius: theme.radius.button,
  padding: '1px 5px',
};

const avatarStyle: React.CSSProperties = {
  width: 24, height: 24, borderRadius: '50%', fontSize: 'var(--fs-3xs)', fontWeight: 600,
  background: theme.colors.sidebarAccent, color: theme.colors.onPrimary,
  display: 'flex', alignItems: 'center', justifyContent: 'center',
};

// --- Jobs section styles ---

const jobsSectionStyle: React.CSSProperties = {
  flexShrink: 0,
  padding: '4px 8px 4px',
  borderBottom: '1px solid var(--color-sidebar-hairline)',
};

const jobsHeaderBtnStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
  width: '100%',
  padding: '6px 8px',
  background: 'none',
  border: 'none',
  borderRadius: theme.radius.button,
  cursor: 'pointer',
  textAlign: 'left',
  color: theme.colors.sidebarText,
};

const jobRowStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
  padding: '6px 8px',
  borderRadius: theme.radius.button,
  marginBottom: 1,
  fontSize: 'var(--fs-xs)',
  color: theme.colors.sidebarText,
  whiteSpace: 'nowrap',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  // `position: relative` so the absolute-positioned actions container
  // (`jobRowActionsStyle`) anchors against the row's right edge.
  position: 'relative',
  // The CSS keyframes animation for `.job-row-highlight` (defined in the
  // expanded sidebar's <style> block) overrides this background while
  // the temporary highlight is active.
  background: 'transparent',
};

const jobIndicatorStyle: React.CSSProperties = {
  fontSize: 'var(--fs-sm)',
  flexShrink: 0,
  width: 16,
  textAlign: 'center',
};

const jobTrioStyle: React.CSSProperties = {
  flex: '0 1 auto',
  minWidth: 0,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  fontWeight: theme.typography.weights.medium,
};

const jobStatusTextStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)',
  color: theme.colors.sidebarTextSecondary,
  letterSpacing: 0.3,
  flexShrink: 0,
};

const jobTimeStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)',
  color: theme.colors.sidebarTextSecondary,
  marginLeft: 'auto',
  flexShrink: 0,
};

const jobMetaSeparatorStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)',
  color: theme.colors.sidebarTextSecondary,
  flexShrink: 0,
};

// Hover-revealed action group on each job row. Anchored to the right
// edge with absolute positioning so it visually overlays the time
// text rather than pushing the layout. The matching opacity rule in
// the inline `<style>` block at the top of the component (the
// `[data-job-row]:hover [data-row-actions]` selector) handles the
// fade-in. The background gradient occludes the time text underneath
// during the brief crossfade.
const jobRowActionsStyle: React.CSSProperties = {
  position: 'absolute',
  right: 6,
  top: '50%',
  transform: 'translateY(-50%)',
  display: 'flex',
  alignItems: 'center',
  gap: 2,
  paddingLeft: 12,
  background: `linear-gradient(to right, transparent 0px, ${theme.colors.sidebarBg} 8px)`,
  borderRadius: theme.radius.button,
};

// --- New layout styles (accordion categories + lower menu) ---

const newBtnStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 8, width: '100%', justifyContent: 'center',
  padding: '9px 12px', borderRadius: theme.radius.button, border: 'none',
  background: theme.colors.primary, color: theme.colors.onPrimary, cursor: 'pointer',
  fontSize: 'var(--font-size-base)', fontWeight: theme.typography.weights.semibold,
  boxShadow: theme.elevation.subtle,
};

const catsScrollStyle: React.CSSProperties = {
  flex: 1, overflowY: 'auto', padding: '2px 8px 8px',
};

const catHeaderStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 9, width: '100%', padding: '8px 10px',
  borderRadius: theme.radius.button, border: 'none', background: 'none', cursor: 'pointer',
  fontSize: 'var(--fs-sm)', fontWeight: theme.typography.weights.semibold, color: theme.colors.sidebarText,
  textAlign: 'left', marginBottom: 4,
};

const catHeaderActiveStyle: React.CSSProperties = {
  ...catHeaderStyle,
  background: theme.colors.accentSubtle, color: theme.colors.sidebarAccent,
};

// The role on a project inventory row, in the sidebar's own palette (the app `Badge`
// is drawn for the light content surfaces, not the sidebar).
const projectRoleBadgeStyle: React.CSSProperties = {
  flexShrink: 0, fontSize: 'var(--fs-3xs)', fontWeight: theme.typography.weights.semibold,
  textTransform: 'uppercase', letterSpacing: 0.5, color: theme.colors.sidebarTextSecondary,
  background: 'var(--color-sidebar-hover)', borderRadius: theme.radius.badge, padding: '1px 6px',
  marginLeft: 6,
};

const catCountStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)', fontWeight: theme.typography.weights.semibold, color: 'inherit',
  background: 'var(--color-sidebar-hover)', borderRadius: theme.radius.pill, padding: '1px 8px',
  minWidth: 20, textAlign: 'center',
};

const lowerMenuStyle: React.CSSProperties = {
  padding: '6px 8px 4px', borderTop: '1px solid var(--color-sidebar-hairline)', flexShrink: 0,
};

const lowerHeadStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 10, width: '100%', padding: '7px 10px',
  borderRadius: theme.radius.button, border: 'none', background: 'none', cursor: 'pointer',
  fontSize: 'var(--font-size-base)', fontWeight: theme.typography.weights.semibold,
  color: theme.colors.sidebarText, textAlign: 'left',
};

const lowerToggleStyle: React.CSSProperties = {
  width: 28, height: 28, flexShrink: 0, display: 'grid', placeItems: 'center', padding: 0, border: 'none',
  background: 'none', borderRadius: theme.radius.button, color: theme.colors.sidebarTextSecondary, cursor: 'pointer',
};

const lowerSubStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 10, width: '100%', padding: '6px 10px 6px 16px',
  borderRadius: theme.radius.button, border: 'none', background: 'none', cursor: 'pointer',
  fontSize: 'var(--fs-sm)', color: theme.colors.sidebarTextSecondary, textAlign: 'left',
};

function AgentIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinejoin="round">
      <path d="M12 3l2.4 6.6L21 12l-6.6 2.4L12 21l-2.4-6.6L3 12l6.6-2.4z" />
    </svg>
  );
}


// A rising trend line. Distinct from every glyph already in use here: AgentIcon (star)
// means Chat, WorkflowIcon means Workflow, ConnectionsIcon means Agents, SkillIcon means
// Skills, and SettingsIcon means Settings in the lower menu.
function ImproveIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3 17l6-6 4 4 7-7" />
      <path d="M14 8h6v6" />
    </svg>
  );
}

function StarIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinejoin="round">
      <path d="M12 2l3 6 6 .9-4.5 4.3L18 20l-6-3-6 3 1.5-6.8L3 8.9 9 8z" />
    </svg>
  );
}

function BellIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
      <path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9M13.7 21a2 2 0 0 1-3.4 0" />
    </svg>
  );
}
