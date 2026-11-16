/**
 * A view a renderer asks the panel to open — today, a Moments search's results as the next version of its tab.
 * The workspace opens it through its one client-view path (`openClientViewTab`): shown at once, saved with the
 * chat, grouped with the views of the same subject.
 */
export const OPEN_VIEW_EVENT = 'lab:open-view';

export interface OpenViewDetail {
  /** The chat the view belongs to; a view for another chat (switched away mid-search) is not opened. */
  sessionId: string;
  manifest: Record<string, unknown> & { id: string };
  label: string;
}

export function requestOpenView(detail: OpenViewDetail): void {
  window.dispatchEvent(new CustomEvent<OpenViewDetail>(OPEN_VIEW_EVENT, { detail }));
}
