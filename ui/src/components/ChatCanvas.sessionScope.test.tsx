/**
 * @vitest-environment jsdom
 *
 * Session-scoped concurrency for the single, persisted ChatCanvas instance.
 *
 * WorkspaceView mounts ONE <ChatCanvas> with no `key`, so the same instance
 * survives session switches (a key would remount it and kill the live SSE
 * stream — explicitly out of scope). That persistence caused two defects that
 * these tests pin against regression:
 *
 *   (a) In-flight state used to be a single boolean. A dispatch started in
 *       session A left it true, so a brand-new session B — whose own
 *       `isStreaming` is reset on switch — still looked "processing" and queued
 *       the user's first message behind A's unrelated turn ("1 message queued"
 *       on a fresh session). It must dispatch immediately instead.
 *
 *   (b) The queue used to be a single shared array. A message typed in one
 *       session could dequeue after a DIFFERENT session's stream ended and run
 *       against whatever session was active then. A queued message must only
 *       ever run in the session it was typed in.
 *
 * The streaming call is mocked so a dispatch can be held "in flight" for as
 * long as a test needs, exactly mimicking a slow turn in another session.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import '@testing-library/jest-dom';
import React from 'react';
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react';

const mocks = vi.hoisted(() => ({
  useLabContext: vi.fn(),
  invokeAgentStreaming: vi.fn(),
  selectedOrchestratorModelId: vi.fn(() => 'model-x'),
  selectedReasoningEffort: vi.fn((): 'low' | 'medium' | 'high' | undefined => undefined),
  createSession: vi.fn(),
  listMemoryEvents: vi.fn(),
  memoryEventsToChatMessages: vi.fn(() => []),
  updateSessionActivity: vi.fn(),
  getConfig: vi.fn(),
  getSessionTranscript: vi.fn(() => Promise.resolve([] as unknown[])),
  updateSessionTranscript: vi.fn(() => Promise.resolve()),
  upload: vi.fn(),
}));

vi.mock('../LabContext', () => ({ useLabContext: mocks.useLabContext }));
vi.mock('../api', () => ({
  invokeAgentStreaming: mocks.invokeAgentStreaming,
  selectedOrchestratorModelId: mocks.selectedOrchestratorModelId,
  selectedReasoningEffort: mocks.selectedReasoningEffort,
}));
vi.mock('../lab-api', () => ({
  createSession: mocks.createSession,
  listMemoryEvents: mocks.listMemoryEvents,
  memoryEventsToChatMessages: mocks.memoryEventsToChatMessages,
  updateSessionActivity: mocks.updateSessionActivity,
  getConfig: mocks.getConfig,
  getSessionTranscript: mocks.getSessionTranscript,
  updateSessionTranscript: mocks.updateSessionTranscript,
}));
vi.mock('../services/agent-service', () => ({ AgentService: { upload: mocks.upload } }));
// Only rendered when a message carries structured job refs (never, today), but
// stub it so the import resolves without pulling its transitive deps.
vi.mock('./chat/JobReference', () => ({ default: () => null }));

import ChatCanvas from './ChatCanvas';
// The composer's upload sheet (where a refused file is listed) is filled by this side-effect module in the app.
import '../projects/upload/registerUploadUi';

// jsdom has no ResizeObserver; ChatCanvas observes its scroll container on
// mount. A no-op stub is enough — virtual-scroll sizing is irrelevant here.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = ResizeObserverStub;

// A promise whose settlement the test controls — models a dispatch that stays
// in flight until we choose to complete it.
function deferred<T = void>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((res) => { resolve = res; });
  return { promise, resolve };
}

// Flush microtasks so the `await createSession(...)` inside processMessage
// resolves and invokeAgentStreaming is actually called.
async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

function setSession(id: string) {
  mocks.useLabContext.mockReturnValue({ sessionId: id, refreshCategory: vi.fn() });
}

function typeAndSubmit(text: string) {
  const textarea = screen.getByLabelText('Chat message input');
  fireEvent.change(textarea, { target: { value: text } });
  // Enter submits. We use the key (not the Send button) because once a turn is
  // streaming the Send button is swapped for Stop, and one path here submits
  // while the current session is mid-stream.
  fireEvent.keyDown(textarea, { key: 'Enter' });
}

// session_id each invokeAgentStreaming call was made with, in call order.
function dispatchedSessionIds(): string[] {
  return mocks.invokeAgentStreaming.mock.calls.map((c) => c[0].session_id);
}
function dispatchedPrompts(): string[] {
  return mocks.invokeAgentStreaming.mock.calls.map((c) => c[0].prompt);
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  mocks.createSession.mockResolvedValue(true);
  mocks.listMemoryEvents.mockResolvedValue([]);
  mocks.memoryEventsToChatMessages.mockReturnValue([]);
  mocks.updateSessionActivity.mockResolvedValue(undefined);
  mocks.getConfig.mockResolvedValue({});
  // Default: any dispatch we don't explicitly hand a deferred stays in flight
  // (never resolves), so the turn's session remains "busy" for the whole test.
  mocks.invokeAgentStreaming.mockReturnValue(new Promise<void>(() => {}));
});

describe('ChatCanvas — per-session in-flight scoping', () => {
  it('a brand-new session dispatches immediately while another session is mid-dispatch', async () => {
    setSession('A');
    const { rerender } = render(<ChatCanvas />);

    // Session A: send a turn and leave it streaming (never-resolving dispatch).
    typeAndSubmit('hello from A');
    await flush();
    expect(dispatchedSessionIds()).toEqual(['A']);

    // Switch to a brand-new session B on the SAME persisted instance. A is
    // still mid-dispatch (its promise never resolved).
    setSession('B');
    rerender(<ChatCanvas />);
    await flush();

    // B's first message must dispatch RIGHT AWAY, not queue behind A.
    typeAndSubmit('hello from B');
    await flush();

    expect(dispatchedSessionIds()).toEqual(['A', 'B']);
    // No queued-message banner on the fresh session.
    expect(screen.queryByText(/message.? queued/)).toBeNull();
  });
});

describe('ChatCanvas — per-session queue scoping', () => {
  it('a queued message runs only in the session it was typed in, never the active one', async () => {
    // A's stream is resolvable so we can end it while viewing B.
    const dA = deferred();
    mocks.invokeAgentStreaming.mockReturnValueOnce(dA.promise);

    setSession('A');
    const { rerender } = render(<ChatCanvas />);

    // A: first turn dispatches and stays in flight.
    typeAndSubmit('first in A');
    await flush();
    expect(dispatchedSessionIds()).toEqual(['A']);

    // A: a second message typed while A is busy is queued (in A's queue).
    typeAndSubmit('queued in A');
    await flush();
    expect(dispatchedSessionIds()).toEqual(['A']); // still just the first turn
    expect(screen.getByText('1 message queued')).toBeInTheDocument();

    // Switch to B — B has no queue of its own, so no banner here.
    setSession('B');
    rerender(<ChatCanvas />);
    await flush();
    expect(screen.queryByText(/message.? queued/)).toBeNull();

    // A's stream now completes WHILE B is on screen. The queued "queued in A"
    // must NOT dequeue against B.
    await act(async () => { dA.resolve(); });
    await flush();
    expect(dispatchedSessionIds()).toEqual(['A']); // no B dispatch happened
    expect(dispatchedSessionIds()).not.toContain('B');

    // Returning to A drains its queue — the message finally runs, in A.
    setSession('A');
    rerender(<ChatCanvas />);
    await flush();
    expect(dispatchedSessionIds()).toEqual(['A', 'A']);
    expect(dispatchedPrompts()[1]).toBe('queued in A');
  });
});

// The live stream folds a turn's steps + text + execution summary into ONE rich assistant message.
// Since the proxy became the authoritative transcript writer, the durable record (S3 + row) is the
// COMPLETE conversation, so restore is a SINGLE read of `getSessionTranscript` — AgentCore Memory is
// the model's context only and is no longer consulted for display.
describe('ChatCanvas — durable transcript restore (proxy-authoritative)', () => {
  it('restores the rich durable transcript from a single read and never consults Memory', async () => {
    setSession('DUR');
    mocks.getSessionTranscript.mockResolvedValueOnce([
      { id: 'u1', role: 'user', content: 'question one', timestamp: 1 },
      {
        id: 'a1',
        role: 'assistant',
        content: 'answer with steps',
        steps: [{ type: 'step', status: 'done', label: 'Analyzing' }],
        summary: { type: 'execution_summary', total_duration_ms: 4200, cycles: 1, tools: {}, usage: {} },
        timestamp: 2,
      },
    ]);

    render(<ChatCanvas />);

    await waitFor(() => expect(mocks.getSessionTranscript).toHaveBeenCalledWith('DUR'));
    await flush();
    // Memory is NOT read for display any more.
    expect(mocks.listMemoryEvents).not.toHaveBeenCalled();
    // Each durable turn renders exactly once.
    expect(screen.getAllByText(/question one/)).toHaveLength(1);
    expect(screen.getAllByText(/answer with steps/)).toHaveLength(1);
  });

  it('renders nothing from Memory when the durable transcript is empty', async () => {
    setSession('MEM');
    mocks.getSessionTranscript.mockResolvedValueOnce([]); // nothing durable

    render(<ChatCanvas />);

    await waitFor(() => expect(mocks.getSessionTranscript).toHaveBeenCalledWith('MEM'));
    await flush();
    expect(mocks.listMemoryEvents).not.toHaveBeenCalled(); // Memory no longer backfills the display
  });

  it('does NOT duplicate turns when the localStorage cache has different ids than the durable record (regression: session 5a003722)', async () => {
    // The proxy persists turns with ids <turnId>-u/-a. A pre-fix localStorage cache held the SAME
    // turns under client-random ids. A union-by-id merge kept BOTH → every turn doubled on refresh.
    // The authoritative durable record must REPLACE the instant-paint cache (not union).
    setSession('DUP');
    localStorage.setItem('lab-session-DUP', JSON.stringify([
      { id: 'msg-111-aaaaaa', role: 'user', content: 'clip the video', timestamp: 1 },
      { id: 'msg-222-bbbbbb', role: 'assistant', content: 'trimming now', timestamp: 2 },
    ]));
    mocks.getSessionTranscript.mockResolvedValueOnce([
      { id: 't-999-x-u', role: 'user', content: 'clip the video', timestamp: 1 },
      { id: 't-999-x-a', role: 'assistant', content: 'trimming now', timestamp: 2 },
    ]);

    render(<ChatCanvas />);
    await waitFor(() => expect(mocks.getSessionTranscript).toHaveBeenCalledWith('DUP'));
    await flush();

    expect(screen.getAllByText(/clip the video/)).toHaveLength(1);
    expect(screen.getAllByText(/trimming now/)).toHaveLength(1);
  });
});

// The client is no longer the durable transcript writer — the PROXY accumulates the SSE stream and
// persists the turn server-side (docs/design-proxy-authoritative-transcript.md). So the client must
// NOT PATCH the transcript, and it now sends the display fields the proxy needs (display_text, turn_id).
describe('ChatCanvas — client no longer writes the durable transcript', () => {
  it('does not call updateSessionTranscript on turn completion, and sends display_text + turn_id', async () => {
    mocks.createSession.mockResolvedValue(true);
    mocks.invokeAgentStreaming.mockImplementation(async (_req: unknown, cb: (e: unknown) => void) => {
      cb({ type: 'text', text: 'here is your snake game' });
      cb({ type: 'execution_summary', summary: { type: 'execution_summary', total_duration_ms: 1200, cycles: 1, tools: {}, usage: {} } });
    });

    setSession('SNAKE');
    render(<ChatCanvas />);
    typeAndSubmit('generate a snake game');
    await flush();
    await flush();

    // The proxy owns durable persistence — the client never PATCHes the transcript.
    expect(mocks.updateSessionTranscript).not.toHaveBeenCalled();
    // The completed turn still renders on screen.
    expect(screen.getByText(/here is your snake game/)).toBeInTheDocument();
    // The invoke carried the fields the proxy persists the clean user turn by.
    const req = mocks.invokeAgentStreaming.mock.calls[0][0];
    expect(req.session_id).toBe('SNAKE');
    expect(req.display_text).toBe('generate a snake game');
    expect(typeof req.turn_id).toBe('string');
    expect(req.turn_id.length).toBeGreaterThan(0);
  });

  it('forwards the reasoning effort when the slider has set one, and omits it otherwise', async () => {
    mocks.createSession.mockResolvedValue(true);
    mocks.invokeAgentStreaming.mockReturnValue(new Promise<void>(() => {}));

    // Default (slider at "Default" → undefined): no effort field on the wire.
    mocks.selectedReasoningEffort.mockReturnValue(undefined);
    setSession('E0');
    const { rerender, unmount } = render(<ChatCanvas />);
    typeAndSubmit('no effort');
    await flush();
    expect('effort' in mocks.invokeAgentStreaming.mock.calls[0][0]).toBe(false);
    unmount();

    // Slider set to High → effort:'high' rides the invoke payload.
    vi.clearAllMocks();
    mocks.createSession.mockResolvedValue(true);
    mocks.invokeAgentStreaming.mockReturnValue(new Promise<void>(() => {}));
    mocks.selectedReasoningEffort.mockReturnValue('high');
    setSession('E1');
    render(<ChatCanvas />);
    typeAndSubmit('with effort');
    await flush();
    expect(mocks.invokeAgentStreaming.mock.calls[0][0].effort).toBe('high');
  });
});

// Large-file handling: the proxy streams to S3 and 413s past its cap, but the client
// also rejects an oversized file up front (no point uploading gigabytes to be refused)
// with a clean, specific inline message — the "graceful >limit" UX.
describe('ChatCanvas — upload size guard', () => {
  it('rejects a file over the server cap up front with a clean inline error, and does not attach it', async () => {
    mocks.getConfig.mockResolvedValue({ maxUploadBytes: 5 }); // tiny cap for the test
    setSession('U');
    const { container } = render(<ChatCanvas />);
    // Let the /config effect apply the cap before we pick a file.
    await waitFor(() => expect(mocks.getConfig).toHaveBeenCalled());
    await flush();

    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement;
    const big = new File(['0123456789'], 'huge.mp4', { type: 'video/mp4' }); // 10 bytes > 5
    fireEvent.change(fileInput, { target: { files: [big] } });

    // The upload sheet lists it as not uploaded, with the limit.
    expect(await screen.findByText(/maximum upload size/i)).toBeTruthy();
    // Not attached: no attachment chip, and nothing is uploaded.
    expect(screen.queryByTestId('attached-file-chip')).toBeNull();
    expect(mocks.upload).not.toHaveBeenCalled();
  });
});


// The agent's switch_project / create_project: a turn's execution_summary.open_project opens a NEW chat in that
// project (live stream only), and that chat sends the carried-over prompt as its first message.
describe('ChatCanvas — open_project from a live turn', () => {
  const summary = (extra: Record<string, unknown>) => ({
    type: 'execution_summary', summary: { type: 'execution_summary', total_duration_ms: 1, cycles: 1, tools: {}, usage: {}, ...extra },
  });

  it('asks the host for a new chat in that project, with the prompt', async () => {
    const { onNewChatInProjectRequest } = await import('../projects/newChatInProject');
    const seen = vi.fn();
    const off = onNewChatInProjectRequest(seen);
    mocks.invokeAgentStreaming.mockImplementation(async (_req: unknown, cb: (e: unknown) => void) => {
      cb({ type: 'text', text: 'Opening workshop-lab01.' });
      cb(summary({ open_project: { projectId: 'plab', projectName: 'workshop-lab01', prompt: 'start lab01' } }));
    });
    setSession('FROM');
    render(<ChatCanvas />);
    typeAndSubmit('start lab01');
    await flush();
    await flush();
    expect(seen).toHaveBeenCalledWith({ projectId: 'plab', projectName: 'workshop-lab01', prompt: 'start lab01' });
    off();
  });

  it('opens nothing when the user left that chat before the turn ended', async () => {
    const { onNewChatInProjectRequest } = await import('../projects/newChatInProject');
    const seen = vi.fn();
    const off = onNewChatInProjectRequest(seen);
    const d = deferred();
    let emit: ((e: unknown) => void) | null = null;
    mocks.invokeAgentStreaming.mockImplementationOnce((_req: unknown, cb: (e: unknown) => void) => { emit = cb; return d.promise; });
    setSession('LEFT');
    const { rerender } = render(<ChatCanvas />);
    typeAndSubmit('go to the lab');
    await flush();
    setSession('ELSEWHERE');
    rerender(<ChatCanvas />);
    await flush();
    emit!(summary({ open_project: { projectId: 'plab', projectName: 'workshop-lab01' } }));
    await act(async () => { d.resolve(); });
    await flush();
    expect(seen).not.toHaveBeenCalled();
    off();
  });

  it('a chat opened for the agent sends the carried-over prompt once, in that chat', async () => {
    const { setPendingOpen } = await import('../projects/newChatInProject');
    setPendingOpen('NEWCHAT', { prompt: 'start lab01' });
    setSession('NEWCHAT');
    const { rerender } = render(<ChatCanvas />);
    await flush();
    await flush();
    expect(dispatchedSessionIds()).toEqual(['NEWCHAT']);
    expect(dispatchedPrompts()).toEqual(['start lab01']);
    rerender(<ChatCanvas />);
    await flush();
    expect(dispatchedPrompts()).toEqual(['start lab01']);
  });

  it('a chat opened with a draft puts it in the composer and sends nothing', async () => {
    const { setPendingOpen } = await import('../projects/newChatInProject');
    setPendingOpen('DRAFTCHAT', { draft: 'what files do I have?' });
    setSession('DRAFTCHAT');
    render(<ChatCanvas />);
    await flush();
    expect(screen.getByLabelText('Chat message input')).toHaveValue('what files do I have?');
    expect(mocks.invokeAgentStreaming).not.toHaveBeenCalled();
  });

  it("a turn refused because the chat's project is gone opens a new Personal chat with the message as a draft", async () => {
    const { onNewChatInProjectRequest } = await import('../projects/newChatInProject');
    const seen = vi.fn();
    const off = onNewChatInProjectRequest(seen);
    mocks.invokeAgentStreaming.mockRejectedValueOnce(Object.assign(new Error('409'), {
      status: 409,
      body: { error: { code: 'session_project_gone', projectId: 'pgone', personalProject: { id: 'ppers', name: 'Personal' } } },
    }));
    setSession('GONE');
    render(<ChatCanvas />);
    typeAndSubmit('what files do I have in this project');
    await flush();
    await flush();
    expect(seen).toHaveBeenCalledTimes(1);
    const d = seen.mock.calls[0][0];
    expect(d).toMatchObject({ projectId: 'ppers', projectName: 'Personal', draft: 'what files do I have in this project' });
    expect(d.notice).toMatch(/was deleted, so this is a new chat in Personal/);
    expect(d.prompt).toBeUndefined();
    expect(screen.queryByText('what files do I have in this project')).toBeNull();
    off();
  });
});

describe('ChatCanvas — the platform guardrail', () => {
  it('notes a reply the guardrail blocked, and says nothing otherwise', async () => {
    mocks.invokeAgentStreaming.mockImplementationOnce(async (_req: unknown, cb: (e: unknown) => void) => {
      cb({ type: 'text', text: 'This response was blocked by the platform’s content guardrail.' });
      cb({ type: 'execution_summary', summary: { type: 'execution_summary', total_duration_ms: 1, cycles: 1, tools: {}, usage: {},
        guardrail: { name: 'harness-default', version: '1', intervened: true } } });
    });
    setSession('GR');
    render(<ChatCanvas />);
    typeAndSubmit('something');
    await flush();
    await flush();
    expect(screen.getByTestId('guardrail-note')).toHaveTextContent("Blocked by the platform's content guardrail (harness-default).");
  });

  it('a turn the guardrail let through shows no note', async () => {
    mocks.invokeAgentStreaming.mockImplementationOnce(async (_req: unknown, cb: (e: unknown) => void) => {
      cb({ type: 'text', text: 'fine' });
      cb({ type: 'execution_summary', summary: { type: 'execution_summary', total_duration_ms: 1, cycles: 1, tools: {}, usage: {},
        guardrail: { name: 'harness-default', version: '1', intervened: false } } });
    });
    setSession('GR2');
    render(<ChatCanvas />);
    typeAndSubmit('something');
    await flush();
    await flush();
    expect(screen.queryByTestId('guardrail-note')).toBeNull();
  });
});
