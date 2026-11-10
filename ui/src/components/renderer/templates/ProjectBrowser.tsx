/**
 * `project-browser` — the agent's "Browse · <project>" tab (`browse_project`, agent/src/project_search/project_tools.py):
 * the Active_Project's file grid inside the chat Renderer, in browse-only mode. The user searches and filters
 * here; opening a video, image or audio file adds an `asset-player` tab next to this one (AssetCard → openAssetAt,
 * with the chat's session id, which WorkspaceView requires). Documents and data files open in a browser tab, as
 * they do on the Projects page. Nothing here changes the Project: no upload, delete, retry or rebuild.
 *
 * The manifest carries the project id, its name and the starting filter — never asset rows or URLs: the grid lists
 * the assets as the signed-in user, so what shows is always current and access-checked.
 */
import React, { useCallback, useState } from 'react';
import type { RendererProps } from '../types';
import { theme } from '../../../styles/tokens';
import ProjectAssetsSection, { type AssetsInitialFilter } from '../../projects/ProjectAssetsSection';
import { getActiveSessionId } from '../../../services/activeSession';
import { openProject } from '../../../projects/openProject';

export interface ProjectBrowserData {
  project_id?: string;
  project_name?: string;
  title?: string;
  filter?: AssetsInitialFilter;
}

export default function ProjectBrowser({ data }: RendererProps<ProjectBrowserData>) {
  const projectId = typeof data?.project_id === 'string' ? data.project_id : '';
  const name = typeof data?.project_name === 'string' && data.project_name ? data.project_name : 'Project';
  const filter = data?.filter && typeof data.filter === 'object' ? data.filter : undefined;
  // The project was deleted (or the user lost access): the grid's reads answer 404. Say so once and unmount the grid,
  // which also stops its status polling (it used to ignore the 404 and poll forever).
  const [gone, setGone] = useState(false);
  const onNotFound = useCallback(() => setGone(true), []);
  if (!projectId) {
    return <p style={emptyStyle} data-testid="project-browser-empty">No project to browse.</p>;
  }
  if (gone) {
    return (
      <p style={emptyStyle} data-testid="project-browser-gone">
        "{name}" no longer exists, or you no longer have access to it.
      </p>
    );
  }
  return (
    <div style={wrapStyle} data-testid="project-browser">
      <div style={headerStyle}>
        <span style={eyebrowStyle}>Project</span>
        <span style={nameStyle} title={name}>{name}</span>
        <span style={{ flex: 1 }} />
        <button type="button" style={openStyle} onClick={() => openProject(projectId)}
          data-testid="project-browser-open">
          Open in Projects
        </button>
      </div>
      <div style={bodyStyle}>
        <ProjectAssetsSection
          project={{ id: projectId, name, personal: false }}
          role="member"
          onNotFound={onNotFound}
          sessionId={getActiveSessionId() || null}
          browseOnly
          initialFilter={filter}
        />
      </div>
    </div>
  );
}

const wrapStyle: React.CSSProperties = {
  // Fills the side panel (header, tab strip, toolbar and footer are ~210px): the browser is the tab's whole view.
  display: 'flex', flexDirection: 'column', height: '100%', minHeight: 'max(420px, calc(100vh - 210px))', background: theme.colors.background,
};
const headerStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 8, padding: '8px 14px',
  borderBottom: `1px solid ${theme.colors.border}`, flexShrink: 0,
};
const eyebrowStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)', fontWeight: 600, letterSpacing: '0.04em', textTransform: 'uppercase',
  color: theme.colors.textSecondary,
};
const nameStyle: React.CSSProperties = {
  fontSize: 'var(--fs-sm)', fontWeight: 600, color: theme.colors.textPrimary,
  minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
};
const bodyStyle: React.CSSProperties = { flex: 1, minHeight: 0, overflowY: 'auto', padding: '12px 14px' };
const emptyStyle: React.CSSProperties = { margin: 0, padding: 16, color: theme.colors.textSecondary, fontSize: 'var(--fs-sm)' };
const openStyle: React.CSSProperties = {
  padding: '5px 10px', fontSize: 'var(--fs-xs)', color: theme.colors.textPrimary, background: 'transparent',
  border: `1px solid ${theme.colors.border}`, borderRadius: theme.radius.button, cursor: 'pointer', flexShrink: 0,
};
