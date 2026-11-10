/**
 * "Start a new chat in <project>" from anywhere in the chat (asset-lineage D1, R2.1).
 *
 * A session's project is fixed by its first turn, so the Plus_Menu answers a pick of another project with a NEW
 * chat in it. It cannot reach the host's `startNewSession(project)` (the Projects_Surface's `New chat` path) through
 * props, so it dispatches this window event and `WorkspaceView` runs that same path. Same shape as `chat:send`.
 *
 * The agent's `switch_project` / `create_project` use the same path from a turn's `execution_summary.open_project`
 * ({@link parseOpenProject}), with a `prompt`: the host keeps it for the new session ({@link takePendingOpen}) and the
 * chat sends it as that session's first message. A chat whose project was deleted opens a new one with a `draft`
 * (put in the composer, not sent) and a `notice`.
 */
export const NEW_CHAT_IN_PROJECT_EVENT = 'projects:new-chat';

export interface NewChatInProjectDetail {
  projectId: string;
  projectName: string;
  /** The new chat's first message, sent as soon as it opens. */
  prompt?: string;
  /** Text put in the new chat's composer, NOT sent (the user reviews it). */
  draft?: string;
  /** A line shown on the new chat's composer saying why it opened. */
  notice?: string;
}

/** Ask the host to open a new chat session whose Active_Project is `projectId` (and send it `prompt`). */
export function requestNewChatInProject(
  projectId: string, projectName: string, prompt?: string, extra: { draft?: string; notice?: string } = {},
): void {
  const clean = typeof prompt === 'string' ? prompt.trim() : '';
  window.dispatchEvent(
    new CustomEvent<NewChatInProjectDetail>(NEW_CHAT_IN_PROJECT_EVENT, {
      detail: {
        projectId, projectName,
        ...(clean ? { prompt: clean } : {}),
        ...(extra.draft ? { draft: extra.draft } : {}),
        ...(extra.notice ? { notice: extra.notice } : {}),
      },
    }),
  );
}

/** A turn's `execution_summary.open_project`, validated; `null` for anything else. */
export function parseOpenProject(value: unknown): NewChatInProjectDetail | null {
  if (!value || typeof value !== 'object') return null;
  const v = value as Record<string, unknown>;
  if (typeof v.projectId !== 'string' || !v.projectId) return null;
  const prompt = typeof v.prompt === 'string' ? v.prompt.trim().slice(0, 2000) : '';
  return {
    projectId: v.projectId,
    projectName: typeof v.projectName === 'string' ? v.projectName : '',
    ...(prompt ? { prompt } : {}),
  };
}

/** What a new chat does when it opens: send `prompt`, or put `draft` in its composer. */
export interface PendingOpen {
  prompt?: string;
  draft?: string;
}

// session id → what that (new) session's chat does when it opens. One per session, taken once.
const pendingOpens = new Map<string, PendingOpen>();

/** Keep `open` for the new session `sessionId`; the chat takes it when that session is on screen. */
export function setPendingOpen(sessionId: string, open: PendingOpen): void {
  if (sessionId && (open.prompt || open.draft)) pendingOpens.set(sessionId, open);
}

/** What was kept for `sessionId` (removed), or `undefined`. */
export function takePendingOpen(sessionId: string): PendingOpen | undefined {
  const p = pendingOpens.get(sessionId);
  pendingOpens.delete(sessionId);
  return p;
}

/** Subscribe the host; returns the unsubscribe. Events without a project id are ignored. */
export function onNewChatInProjectRequest(handler: (detail: NewChatInProjectDetail) => void): () => void {
  const listener = (e: Event) => {
    const d = (e as CustomEvent<Partial<NewChatInProjectDetail>>).detail;
    if (d && typeof d.projectId === 'string' && d.projectId) {
      handler({
        projectId: d.projectId,
        projectName: typeof d.projectName === 'string' ? d.projectName : '',
        ...(typeof d.prompt === 'string' && d.prompt ? { prompt: d.prompt } : {}),
        ...(typeof d.draft === 'string' && d.draft ? { draft: d.draft } : {}),
        ...(typeof d.notice === 'string' && d.notice ? { notice: d.notice } : {}),
      });
    }
  };
  window.addEventListener(NEW_CHAT_IN_PROJECT_EVENT, listener);
  return () => window.removeEventListener(NEW_CHAT_IN_PROJECT_EVENT, listener);
}
