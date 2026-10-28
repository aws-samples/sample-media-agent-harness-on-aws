/**
 * Projects_Surface — the `projects` center mode root (media-harness-projects-access task 13.6; design
 * Component 11, Req 1.7, 1.8, 1.20, 1.22, 2.7, 3.5, 3.12, 10).
 *
 * Laid out like the Agents and Skills surfaces: the inventory is in the LEFT SIDEBAR (`SessionSidebar`'s
 * project list, fed by the same {@link useProjectsList} store), and this surface is the Selected_Project,
 * full width — a `Projects` surface header, the Project's own header ({@link ProjectHeader}), then its body.
 *
 *   * On mount it re-reads `GET /projects` (which also creates a missing Personal_Project) and, when a chat
 *     session is open, reconciles that session's Active_Project with the list (Req 3.12).
 *   * The host persists the Selected_Project id (`selectedId` / `onSelectedChange`, Req 10.2); the sidebar
 *     changes it. Selecting changes only the Selected_Project, never a session's Active_Project (Req 10.7).
 *   * `New chat`, on every Project, asks the host to open a NEW chat session working in the Selected_Project
 *     (`onNewChatInProject`). Nothing here changes the Active_Project of a session that is already open.
 *   * The sidebar's `+ New project` bumps `createRequest`, which opens the create page (in place of
 *     everything else); a successful create re-reads the list and selects the new Project (Req 10.4).
 *   * A `404 project_not_found` for the Selected_Project drops it, selects Personal, and shows the
 *     lost-access notice — without touching any session (Req 10.10).
 */
import React, { useCallback, useEffect, useId, useRef, useState } from 'react';
import { theme } from '../../styles/tokens';
import { SurfaceHeader, surfaceStyle } from '../SurfaceHeader';
import ProjectCreateForm from './ProjectCreateForm';
import ProjectSettings from './ProjectSettings';
import ProjectHeader, { type ProjectPanelKey } from './ProjectHeader';
import ProjectAssetsSection, { TOP_LEVEL, projectSectionHeadingStyle } from './ProjectAssetsSection';
import MembersTab from './MembersTab';
import { getProjectTab } from './tabSlots';
import { createProject, peekGraphEnabled, peekLineageEnabled, type NewProjectSettings, type ProjectSummary } from '../../projects/api';
import SegmentedControl, { panelDomId, tabDomId } from '../ui/SegmentedControl';
import Badge from '../ui/Badge';
import GraphTab from './graph/GraphTab';
// The tab shell only: the canvas and its layout are a lazy chunk (LineageTab imports LineageCanvas with lazy()).
import LineageTab from './lineage/LineageTab';
import { useProjectsList } from '../../projects/useProjectsList';
import {
  getSessionProject,
  reconcileWithProjectList,
  useSessionProject,
} from '../../projects/sessionProjectStore';

/**
 * The Members section (owner/member list, invite, remove) is built (`MembersTab`) but hidden until sharing
 * ships. Leaving a Project stays reachable from the header's overflow menu.
 */
export const SHOW_PROJECT_MEMBERS = false;

export interface ProjectsSurfaceProps {
  /** The chat session currently on screen, for the mount reconcile and chat attachments. */
  activeSessionId?: string | null;
  /** Open a new chat session whose Active_Project is this Project. Absent → no `New chat` button. */
  onNewChatInProject?: (projectId: string, projectName: string) => void;
  /** Counter the sidebar's `New project` bumps to open the inline create form (Req 10.4). */
  createRequest?: number;
  /** The Selected_Project id the host persists (Req 10.2). The sidebar and `Manage projects` set it. */
  selectedId?: string | null;
  /** Report the Selected_Project id so the host can persist it. */
  onSelectedChange?: (id: string) => void;
  /** Open the Graph view of `projectId` with `nodeIds` highlighted (a chat graph result's "Open in project graph"). */
  graphFocus?: { projectId: string; nodeIds: string[]; nonce: number } | null;
  /** Open the Assets grid of `projectId` at `folder` (`""` = the top level): a file's location was clicked. */
  folderFocus?: { projectId: string; folder: string; nonce: number } | null;
  /** Open a chat at a turn (the Lineage view's "Made in chat … · turn N"). */
  onOpenChat?: (sessionId: string, turnId?: string) => void;
}

function pickSelection(
  list: readonly ProjectSummary[],
  current: string | null,
  sessionActiveId: string | null,
): string | null {
  if (current && list.some((p) => p.projectId === current)) return current;
  if (sessionActiveId && list.some((p) => p.projectId === sessionActiveId)) return sessionActiveId;
  const personal = list.find((p) => p.personal);
  return personal?.projectId ?? list[0]?.projectId ?? null;
}

const PANEL_ORDER: ProjectPanelKey[] = ['upload', 's3-location'];

type ProjectView = 'assets' | 'lineage' | 'graph';
/** Assets | Lineage (where `/config.lineageEnabled`) | Graph (where `/config.graphEnabled`). */
export function viewOptions(
  lineage: boolean,
  graph: boolean,
): { value: ProjectView; label: React.ReactNode; ariaLabel?: string }[] {
  return [
    { value: 'assets', label: 'Assets' },
    // Lineage is still a preview feature — a small BETA pill marks it. `ariaLabel` carries the same
    // status to screen readers (the visible pill alone would be silent to them).
    ...(lineage
      ? [{
          value: 'lineage' as const,
          ariaLabel: 'Lineage (beta)',
          label: (
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
              Lineage<Badge variant="info" caps>Beta</Badge>
            </span>
          ),
        }]
      : []),
    ...(graph ? [{ value: 'graph' as const, label: 'Graph' }] : []),
  ];
}

export default function ProjectsSurface({
  activeSessionId,
  onNewChatInProject,
  createRequest,
  selectedId: hostSelectedId,
  onSelectedChange,
  graphFocus,
  folderFocus,
  onOpenChat,
}: ProjectsSurfaceProps) {
  const { projects, refresh } = useProjectsList();
  const [selectedId, setSelectedId] = useState<string | null>(hostSelectedId ?? null);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [panel, setPanel] = useState<ProjectPanelKey | null>(null);
  const [creating, setCreating] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  // Assets | Graph. The Graph tab exists only where the graph stack is deployed (`/config.graphEnabled`).
  const [view, setView] = useState<ProjectView>('assets');
  const [showGraphTab] = useState(peekGraphEnabled);
  // The Lineage tab exists only where lineage edges are on (`/config.lineageEnabled`).
  const [showLineageTab] = useState(peekLineageEnabled);
  const showViewTabs = showGraphTab || showLineageTab;
  const VIEW_OPTIONS = viewOptions(showLineageTab, showGraphTab);
  // An asset's ⋯ → Lineage: the Lineage view focused on it (remounted on the nonce).
  const [lineageFocus, setLineageFocus] = useState<{ projectId: string; assetId: string; nonce: number } | null>(null);
  // "Show in Assets" from the Lineage view: the Assets grid at that asset's folder (remounted on the nonce).
  const [lineageFolder, setLineageFolder] = useState<{ projectId: string; folder: string; nonce: number } | null>(null);
  const viewIds = useId();
  // Bumped after a settings change so the asset list re-reads (indexing on → stored files become queued).
  const [assetsEpoch, setAssetsEpoch] = useState(0);
  // Indexing per Project, once Settings has read it; the Assets section reads it itself when this is unknown.
  const [indexingById, setIndexingById] = useState<Record<string, boolean>>({});
  const noteIndexing = useCallback((projectId: string, on: boolean) => {
    setIndexingById((cur) => (cur[projectId] === on ? cur : { ...cur, [projectId]: on }));
    // The shared list drives the sidebar's and the chat picker's search mark: re-read it when it is behind.
    const listed = projects?.find((p) => p.projectId === projectId);
    if (listed && (listed.indexing === true) !== on) void refresh();
  }, [projects, refresh]);
  const sessionProject = useSessionProject(activeSessionId ?? null);
  // The host's id (sidebar selection, Manage projects) wins over the surface's last-shown Project.
  const hostRef = useRef(hostSelectedId);
  const lastCreateRequest = useRef(createRequest);

  const changeSelection = useCallback(
    (id: string | null) => {
      setSelectedId(id);
      if (id) onSelectedChange?.(id);
    },
    [onSelectedChange],
  );

  const load = useCallback(async () => {
    const r = await refresh();
    if (!r.ok) return;
    const list = r.value;
    const sessionActiveId = activeSessionId ? getSessionProject(activeSessionId)?.activeProjectId ?? null : null;
    setSelectedId((cur) => {
      const next = pickSelection(list, hostRef.current ?? cur, sessionActiveId);
      if (next && next !== hostRef.current) onSelectedChange?.(next);
      return next;
    });
    // Reconcile the open session's Active_Project against the fresh list (Req 3.12).
    if (activeSessionId) void reconcileWithProjectList(activeSessionId, list);
  }, [refresh, activeSessionId, onSelectedChange]);

  useEffect(() => {
    void load();
  }, [load]);

  // The host's selection (sidebar row, Manage projects) selects directly.
  useEffect(() => {
    hostRef.current = hostSelectedId;
    if (hostSelectedId) setSelectedId(hostSelectedId);
  }, [hostSelectedId]);

  // A different Project starts clean: no stale error or open panel.
  useEffect(() => {
    setActionError(null);
    setPanel(null);
    setSettingsOpen(false);
    setView(graphFocus && graphFocus.projectId === selectedId ? 'graph' : 'assets');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId]);

  // A graph focus request (from a chat graph result) opens the Graph view once the project is selected.
  useEffect(() => {
    if (graphFocus && showGraphTab && graphFocus.projectId === selectedId) {
      setSettingsOpen(false);
      setView('graph');
    }
  }, [graphFocus, selectedId, showGraphTab]);
  const focusIds = graphFocus && graphFocus.projectId === selectedId ? graphFocus.nodeIds : undefined;
  // A folder focus request opens the Assets grid at that folder (the grid remounts on the nonce, seeded with it).
  const hostFolderSeed = folderFocus && folderFocus.projectId === selectedId ? folderFocus : null;
  const lineageFolderSeed = lineageFolder && lineageFolder.projectId === selectedId ? lineageFolder : null;
  const folderSeed = lineageFolderSeed && (!hostFolderSeed || lineageFolderSeed.nonce > hostFolderSeed.nonce)
    ? lineageFolderSeed : hostFolderSeed;
  const lineageSeed = lineageFocus && lineageFocus.projectId === selectedId ? lineageFocus : null;
  useEffect(() => {
    if (folderSeed) { setSettingsOpen(false); setView('assets'); }
  }, [folderSeed]);

  // The sidebar's `+ New project`.
  useEffect(() => {
    if (createRequest !== lastCreateRequest.current) {
      lastCreateRequest.current = createRequest;
      setCreating(true);
    }
  }, [createRequest]);

  const selected = projects?.find((p) => p.projectId === selectedId) ?? null;

  const handleCreate = useCallback(
    async (name: string, settings: NewProjectSettings = {}): Promise<string | null> => {
      const r = await createProject(name, settings);
      if (r.ok) {
        // Select the new Project once the re-read includes it, so the header never blinks to empty.
        hostRef.current = r.value.projectId;
        await load();
        changeSelection(r.value.projectId);
        setCreating(false);
        return null;
      }
      if (r.kind === 'disabled') return 'Projects are not enabled';
      if (r.kind === 'not_found') return 'Project not found';
      if (r.kind === 'access_lost') return 'Access changed; please retry';
      return r.message;
    },
    [load, changeSelection],
  );

  const handleSelectedNotFound = useCallback(() => {
    setNotice(`You no longer have access to ${selected?.name ?? 'that project'}`);
    void load();
  }, [selected, load]);

  const handleChanged = useCallback(() => {
    void load();
  }, [load]);

  const handleNewChat = onNewChatInProject && selected
    ? () => onNewChatInProject(selected.projectId, selected.name)
    : undefined;

  const panels = PANEL_ORDER.filter((k) => !!getProjectTab(k));
  const PanelSlot = panel ? getProjectTab(panel) : undefined;
  const bannerText = notice ?? sessionProject?.notice ?? null;

  // `+ New project` is its own page, like the Agents and Skills create flows: nothing of the selected
  // project shows behind it, and Back returns to the project that was showing.
  if (creating) {
    return (
      <div style={surfaceStyle} data-testid="projects-surface">
        <ProjectCreateForm onCreate={handleCreate} onCancel={() => setCreating(false)} />
      </div>
    );
  }

  return (
    <div style={surfaceStyle} data-testid="projects-surface">
      {/* The subtitle explains the page only until a project is open; then the project's own header says it. */}
      <SurfaceHeader title="Projects" subtitle={selected ? undefined : 'Choose the project your chats work in and manage its assets.'} />
      {bannerText && <div style={noticeStyle} role="status">{bannerText}</div>}

      {selected ? (
        <div style={detailStyle}>
          <ProjectHeader
            key={selected.projectId}
            project={{
              id: selected.projectId,
              name: selected.name,
              personal: selected.personal,
              activeAssetCount: selected.activeAssetCount,
              // Settings' fresh read wins (it learns of a switch-on first); else the list's value.
              indexing: indexingById[selected.projectId] ?? selected.indexing,
            }}
            role={selected.role}
            onNewChat={handleNewChat}
            panels={panels}
            activePanel={panel}
            onTogglePanel={(k) => { setView('assets'); setPanel((cur) => (cur === k ? null : k)); }}
            onNotFound={handleSelectedNotFound}
            onChanged={handleChanged}
            onError={setActionError}
            settingsOpen={settingsOpen}
            onToggleSettings={() => setSettingsOpen((v) => !v)}
          />
          {showViewTabs && !settingsOpen && (
            <SegmentedControl
              options={VIEW_OPTIONS} value={view} onChange={setView} label="Project view" variant="underline"
              idPrefix={viewIds} style={viewTabsStyle}
            />
          )}
          {showLineageTab && !settingsOpen && view === 'lineage' ? (
            <div style={graphBodyStyle} role="tabpanel" id={panelDomId(viewIds, 'lineage')}
              aria-labelledby={tabDomId(viewIds, 'lineage')} data-testid="project-lineage-view">
              <LineageTab
                key={`${selected.projectId}:${lineageSeed?.nonce ?? 0}`}
                project={{ id: selected.projectId, name: selected.name }}
                initialFocus={lineageSeed?.assetId ?? null}
                sessionId={activeSessionId ?? null}
                onOpenChat={onOpenChat}
                onNotFound={handleSelectedNotFound}
                onShowFolder={(folder) => {
                  setLineageFolder({ projectId: selected.projectId, folder, nonce: Date.now() });
                  setView('assets');
                }}
              />
            </div>
          ) : showGraphTab && !settingsOpen && view === 'graph' ? (
            <div style={graphBodyStyle} role="tabpanel" id={panelDomId(viewIds, 'graph')}
              aria-labelledby={tabDomId(viewIds, 'graph')} data-testid="project-graph-view">
              <GraphTab
                key={`${selected.projectId}:${graphFocus?.nonce ?? 0}`}
                project={{ id: selected.projectId, name: selected.name, personal: selected.personal }}
                role={selected.role}
                onNotFound={handleSelectedNotFound}
                focusIds={focusIds}
              />
            </div>
          ) : (
          <div style={bodyStyle}>
            {actionError && <p style={errorStyle} role="alert">{actionError}</p>}
            {!settingsOpen && panel && PanelSlot && (
              <section style={sectionStyle} data-testid={`project-panel-${panel}`}>
                <PanelSlot
                  project={{
                    id: selected.projectId,
                    name: selected.name,
                    personal: selected.personal,
                    activeAssetCount: selected.activeAssetCount,
                  }}
                  role={selected.role}
                  onNotFound={handleSelectedNotFound}
                />
              </section>
            )}
            {settingsOpen && (
              <section style={sectionStyle} data-testid="project-panel-settings">
                <ProjectSettings
                  project={{ id: selected.projectId, name: selected.name, personal: selected.personal }}
                  role={selected.role}
                  onNotFound={handleSelectedNotFound}
                  onChanged={() => setAssetsEpoch((n) => n + 1)}
                  onIndexingKnown={(on) => noteIndexing(selected.projectId, on)}
                />
              </section>
            )}
            {/* Settings is its own page: it replaces the asset list until Settings is pressed again. */}
            {!settingsOpen && (
              <ProjectAssetsSection
                key={`${selected.projectId}:${assetsEpoch}:${folderSeed?.nonce ?? 0}`}
                initialFilter={folderSeed ? { folder: folderSeed.folder || TOP_LEVEL } : undefined}
                project={{
                  id: selected.projectId,
                  name: selected.name,
                  personal: selected.personal,
                  activeAssetCount: selected.activeAssetCount,
                }}
                role={selected.role}
                onNotFound={handleSelectedNotFound}
                sessionId={activeSessionId ?? null}
                indexing={indexingById[selected.projectId]}
                onShowLineage={showLineageTab ? (assetId) => {
                  setLineageFocus({ projectId: selected.projectId, assetId, nonce: Date.now() });
                  setSettingsOpen(false);
                  setView('lineage');
                } : undefined}
              />
            )}
            {SHOW_PROJECT_MEMBERS && (
              <section style={{ ...sectionStyle, marginTop: 24 }}>
                <h3 style={projectSectionHeadingStyle}>Members</h3>
                <MembersTab
                  project={{ id: selected.projectId, name: selected.name, personal: selected.personal }}
                  role={selected.role}
                  onNotFound={handleSelectedNotFound}
                  onChanged={handleChanged}
                />
              </section>
            )}
          </div>
          )}
        </div>
      ) : (
        <p style={emptyStyle}>{projects === null ? 'Loading projects…' : 'No project selected.'}</p>
      )}
    </div>
  );
}

const detailStyle: React.CSSProperties = { flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' };
// Matches the Agents surface body (`caBodyStyle`).
const bodyStyle: React.CSSProperties = { flex: 1, minHeight: 0, overflowY: 'auto', padding: 24, paddingTop: 8 };
const sectionStyle: React.CSSProperties = { marginBottom: 24 };
const viewTabsStyle: React.CSSProperties = { padding: '0 24px', flexShrink: 0 };
// The graph fills the remaining height; the canvas manages its own scrolling (pan/zoom).
const graphBodyStyle: React.CSSProperties = { flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column', paddingTop: 8 };
const noticeStyle: React.CSSProperties = {
  padding: '8px 24px',
  background: theme.colors.surfaceRaised,
  color: theme.colors.textPrimary,
  fontSize: 'var(--fs-sm)',
  borderBottom: `1px solid ${theme.colors.border}`,
};
const errorStyle: React.CSSProperties = { margin: '0 0 16px', color: theme.colors.error, fontSize: 'var(--fs-sm)' };
const emptyStyle: React.CSSProperties = {
  margin: 0,
  padding: 24,
  color: theme.colors.textSecondary,
  fontSize: 'var(--fs-sm)',
};
