/**
 * @vitest-environment jsdom
 */
import { describe, expect, it, vi } from 'vitest';
import {
  NEW_CHAT_IN_PROJECT_EVENT, onNewChatInProjectRequest, parseOpenProject, requestNewChatInProject, setPendingOpen,
  takePendingOpen,
} from './newChatInProject';

describe('new chat in a project (asset-lineage D1)', () => {
  it('delivers the request to the host until it unsubscribes', () => {
    const handler = vi.fn();
    const off = onNewChatInProjectRequest(handler);
    requestNewChatInProject('p1', 'Launch Q3');
    expect(handler).toHaveBeenCalledWith({ projectId: 'p1', projectName: 'Launch Q3' });
    off();
    requestNewChatInProject('p2', 'Other');
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('ignores an event that names no project', () => {
    const handler = vi.fn();
    const off = onNewChatInProjectRequest(handler);
    window.dispatchEvent(new CustomEvent(NEW_CHAT_IN_PROJECT_EVENT, { detail: { projectName: 'x' } }));
    window.dispatchEvent(new CustomEvent(NEW_CHAT_IN_PROJECT_EVENT));
    expect(handler).not.toHaveBeenCalled();
    off();
  });

  it("carries the agent's prompt for the new chat", () => {
    const handler = vi.fn();
    const off = onNewChatInProjectRequest(handler);
    requestNewChatInProject('p1', 'workshop-lab01', '  start lab01 ');
    requestNewChatInProject('p2', 'Other', '   ');
    expect(handler).toHaveBeenNthCalledWith(1, { projectId: 'p1', projectName: 'workshop-lab01', prompt: 'start lab01' });
    expect(handler).toHaveBeenNthCalledWith(2, { projectId: 'p2', projectName: 'Other' });
    off();
  });

  it("reads a turn's open_project and refuses anything else", () => {
    expect(parseOpenProject({ projectId: 'p1', projectName: 'Lab', prompt: ' go ' })).toEqual({ projectId: 'p1', projectName: 'Lab', prompt: 'go' });
    expect(parseOpenProject({ projectId: 'p1' })).toEqual({ projectId: 'p1', projectName: '' });
    expect(parseOpenProject({ projectId: 'p1', prompt: 'x'.repeat(3000) })!.prompt).toHaveLength(2000);
    for (const bad of [null, undefined, 'p1', {}, { projectId: '' }, { projectId: 7 }]) expect(parseOpenProject(bad)).toBeNull();
  });

  it('hands what a new chat does to its session once', () => {
    setPendingOpen('s-new', { prompt: 'start lab01' });
    setPendingOpen('s-empty', {});
    expect(takePendingOpen('s-other')).toBeUndefined();
    expect(takePendingOpen('s-empty')).toBeUndefined();
    expect(takePendingOpen('s-new')).toEqual({ prompt: 'start lab01' });
    expect(takePendingOpen('s-new')).toBeUndefined();
  });

  it('carries a draft and a notice (a chat whose project was deleted)', () => {
    const handler = vi.fn();
    const off = onNewChatInProjectRequest(handler);
    requestNewChatInProject('pp', 'Personal', undefined, { draft: 'what files?', notice: '"P" was deleted' });
    expect(handler).toHaveBeenCalledWith({ projectId: 'pp', projectName: 'Personal', draft: 'what files?', notice: '"P" was deleted' });
    off();
  });
});
