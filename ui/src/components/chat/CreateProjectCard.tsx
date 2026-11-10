/**
 * A project the agent PROPOSED (`create_project` → `[Create project “X”](project:new?name=X…)`), rendered as a card
 * with one button. The agent cannot create a project; the user's click does, through the same `POST /projects`
 * the New project form uses, so the name and ownership rules are the server's as always. After it exists, the card
 * offers to start a chat in it (a chat's project is fixed by its first turn, so the CURRENT chat cannot move).
 * (`create_project` now creates the project itself and opens a chat there; this card serves links in older chats.)
 *
 * `delete_project` → `[Delete project “X”](project:delete?id=<pid>&name=X)` is {@link DeleteProjectCard}: the agent
 * cannot delete, the user does, by typing the exact name in the same confirmation the Projects page uses. The proxy
 * still checks that they own it and that the typed name matches.
 */
import React, { useState } from 'react';
import { createProject, deleteProject } from '../../projects/api';
import ConfirmDialog from '../ConfirmDialog';
import { requestNewChatInProject } from '../../projects/newChatInProject';
import { refreshProjectsList } from '../../projects/useProjectsList';
import { theme } from '../../styles/tokens';
import FolderIcon from '../icons/FolderIcon';

export const PROJECT_LINK_PREFIX = 'project:new';
export const DELETE_PROJECT_LINK_PREFIX = 'project:delete?';

/** True for either project card link: what the chat's `a` override renders as a card. */
export function isProjectCardLink(href: string): boolean {
  return href.startsWith(PROJECT_LINK_PREFIX) || href.startsWith(DELETE_PROJECT_LINK_PREFIX);
}

export function parseDeleteProjectLink(href: string): { id: string; name: string } | null {
  if (!href.startsWith(DELETE_PROJECT_LINK_PREFIX)) return null;
  const q = new URLSearchParams(href.slice(DELETE_PROJECT_LINK_PREFIX.length));
  const id = (q.get('id') || '').trim();
  const name = q.get('name') || '';
  return id && name.trim() ? { id, name } : null;
}

/** The card for a project link: {@link CreateProjectCard} or {@link DeleteProjectCard}. */
export function ProjectLinkCard({ href }: { href: string }) {
  return href.startsWith(DELETE_PROJECT_LINK_PREFIX) ? <DeleteProjectCard href={href} /> : <CreateProjectCard href={href} />;
}

type DeleteState = { phase: 'idle' } | { phase: 'confirm' } | { phase: 'busy' } | { phase: 'done' } | { phase: 'error'; message: string };

export function DeleteProjectCard({ href }: { href: string }) {
  const spec = parseDeleteProjectLink(href);
  const [state, setState] = useState<DeleteState>({ phase: 'idle' });
  if (!spec) return null;
  const remove = async () => {
    setState({ phase: 'busy' });
    const r = await deleteProject(spec.id, spec.name);
    if (r.ok) {
      setState({ phase: 'done' });
      void refreshProjectsList();
    } else {
      setState({
        phase: 'error',
        message: r.kind === 'error' ? r.message : r.kind === 'not_found' ? 'That project no longer exists.' : "Couldn't delete the project; try again.",
      });
    }
  };
  const open = state.phase === 'confirm' || state.phase === 'busy';
  return (
    <span style={cardStyle} data-testid="delete-project-card">
      <span style={iconStyle} aria-hidden="true"><FolderIcon size={16} /></span>
      <span style={bodyStyle}>
        <span style={nameStyle}>{spec.name}</span>
        <span style={metaStyle}>
          {state.phase === 'done' ? 'Project deleted' : 'Delete project · removes every file in it, for every member'}
        </span>
        {state.phase === 'error' && <span role="alert" style={errorStyle}>{state.message}</span>}
      </span>
      {state.phase !== 'done' && (
        <button type="button" style={dangerStyle} disabled={open} onClick={() => setState({ phase: 'confirm' })}
          data-testid="delete-project-button">Delete…</button>
      )}
      <ConfirmDialog
        open={open}
        title="Delete project"
        message={`Delete "${spec.name}"? This cannot be undone.`}
        confirmLabel="Delete project"
        requiredPhrase={spec.name}
        loading={state.phase === 'busy'}
        onConfirm={() => void remove()}
        onCancel={() => setState({ phase: 'idle' })}
      />
    </span>
  );
}

export function parseProjectLink(href: string): { name: string; description?: string; indexing: boolean } | null {
  if (!href.startsWith(PROJECT_LINK_PREFIX)) return null;
  const q = new URLSearchParams(href.slice(href.indexOf('?') + 1));
  const name = (q.get('name') || '').trim();
  if (!name) return null;
  const description = (q.get('description') || '').trim();
  return { name, ...(description ? { description } : {}), indexing: q.get('indexing') !== '0' };
}

type State = { phase: 'idle' } | { phase: 'busy' } | { phase: 'done'; id: string; name: string } | { phase: 'error'; message: string };

export function CreateProjectCard({ href }: { href: string }) {
  const spec = parseProjectLink(href);
  const [state, setState] = useState<State>({ phase: 'idle' });
  if (!spec) return null;
  const create = async () => {
    setState({ phase: 'busy' });
    const r = await createProject(spec.name, { ...(spec.description ? { description: spec.description } : {}), indexing: spec.indexing });
    if (r.ok) {
      setState({ phase: 'done', id: r.value.projectId, name: r.value.name });
      void refreshProjectsList(); // the sidebar lists it at once
    } else {
      setState({ phase: 'error', message: r.kind === 'error' ? r.message : "Couldn't create the project; try again." });
    }
  };
  return (
    <span style={cardStyle} data-testid="create-project-card">
      <span style={iconStyle} aria-hidden="true"><FolderIcon size={16} /></span>
      <span style={bodyStyle}>
        <span style={nameStyle}>{state.phase === 'done' ? state.name : spec.name}</span>
        <span style={metaStyle}>
          {state.phase === 'done' ? 'Project created' : `New project · ${spec.indexing ? 'files analyzed for search' : 'store only'}`}
          {spec.description && state.phase !== 'done' ? ` · ${spec.description}` : ''}
        </span>
        {state.phase === 'error' && <span role="alert" style={errorStyle}>{state.message}</span>}
      </span>
      {state.phase === 'done' ? (
        <button type="button" style={btnStyle} onClick={() => requestNewChatInProject(state.id, state.name)}
          data-testid="create-project-chat">Start a chat in it</button>
      ) : (
        <button type="button" style={primaryStyle} disabled={state.phase === 'busy'} onClick={() => void create()}
          data-testid="create-project-button">{state.phase === 'busy' ? 'Creating…' : 'Create project'}</button>
      )}
    </span>
  );
}

const cardStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 10, width: 'min(100%, 420px)', margin: '8px 0', padding: '10px 12px',
  boxSizing: 'border-box', border: `1px solid ${theme.colors.border}`, borderRadius: theme.radius.panel, background: theme.colors.surface,
};
const iconStyle: React.CSSProperties = {
  width: 32, height: 32, flexShrink: 0, display: 'grid', placeItems: 'center', borderRadius: theme.radius.button,
  background: theme.colors.surfaceSubtle, color: theme.colors.textSecondary,
};
const bodyStyle: React.CSSProperties = { flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column' };
const nameStyle: React.CSSProperties = {
  fontSize: 'var(--fs-sm)', fontWeight: 600, color: theme.colors.textPrimary, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
};
const metaStyle: React.CSSProperties = { fontSize: 'var(--fs-xs)', color: theme.colors.textTertiary };
const errorStyle: React.CSSProperties = { fontSize: 'var(--fs-xs)', color: theme.colors.error, marginTop: 2 };
const btnStyle: React.CSSProperties = {
  flexShrink: 0, height: 30, padding: '0 12px', border: `1px solid ${theme.colors.borderStrong}`, borderRadius: theme.radius.button,
  background: theme.colors.surface, color: theme.colors.textPrimary, font: 'inherit', fontSize: 'var(--fs-sm)', fontWeight: 600, cursor: 'pointer',
};
const primaryStyle: React.CSSProperties = { ...btnStyle, border: 'none', background: theme.colors.primary, color: theme.colors.onPrimary };
const dangerStyle: React.CSSProperties = { ...btnStyle, border: `1px solid ${theme.colors.error}`, color: theme.colors.error };
