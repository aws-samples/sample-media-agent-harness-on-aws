/**
 * @vitest-environment jsdom
 */
import '@testing-library/jest-dom';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, render, screen } from '@testing-library/react';

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>();
  return {
    patchSessionProject: vi.fn(),
    projectsEnabled: vi.fn(),
    isSessionProjectFixed: actual.isSessionProjectFixed,
    SESSION_PROJECT_FIXED_CODE: actual.SESSION_PROJECT_FIXED_CODE,
  };
});

import { patchSessionProject, projectsEnabled } from './api';
import {
  __resetSessionProjectStoreForTests,
  dismissNotice,
  forgetSessionProject,
  getSessionProject,
  lostAccessMessage,
  projectGoneMessage,
  markSessionPersisted,
  markSessionProjectFixed,
  persistSessionForAction,
  reconcileWithProjectList,
  resetToPersonal,
  seedSessionProject,
  selectActiveProject,
  setWebSearch,
  snapshot,
  useSessionProject,
} from './sessionProjectStore';
import type { ProjectSummary } from './api';

const mockPatch = patchSessionProject as unknown as ReturnType<typeof vi.fn>;
const mockEnabled = projectsEnabled as unknown as ReturnType<typeof vi.fn>;

const PERSONAL = { id: 'p00000000000000000000000000000001', name: 'Personal' };
const TRAILER = 'p00000000000000000000000000000002';
const OTHER = 'p00000000000000000000000000000003';
const OK = { ok: true, value: undefined } as const;
const NOT_FOUND = { ok: false, kind: 'not_found' } as const;

const LIST: ProjectSummary[] = [
  { projectId: PERSONAL.id, name: 'Personal', role: 'owner', personal: true },
  { projectId: TRAILER, name: 'Trailer cuts', role: 'member', personal: false },
];

beforeEach(() => {
  __resetSessionProjectStoreForTests();
  mockPatch.mockReset();
  mockEnabled.mockReset();
  mockEnabled.mockResolvedValue(true);
});

describe('sessionProjectStore when Projects are disabled', () => {
  it('creates no entry and makes no request', async () => {
    mockEnabled.mockResolvedValue(false);
    await seedSessionProject('s1', { activeProjectId: TRAILER, webSearch: true });
    expect(getSessionProject('s1')).toBeNull();
    expect(snapshot('s1')).toBeNull();
    expect(await selectActiveProject('s1', TRAILER, 'Trailer cuts')).toEqual({ ok: false, kind: 'disabled' });
    expect(await setWebSearch('s1', true)).toEqual({ ok: false, kind: 'disabled' });
    await resetToPersonal('s1', { personalProject: PERSONAL });
    expect(await reconcileWithProjectList('s1', LIST)).toBe('skipped');
    expect(getSessionProject('s1')).toBeNull();
    expect(mockPatch).not.toHaveBeenCalled();
  });
});

describe('sessionProjectStore', () => {
  it('seeds from the session row and never overwrites an existing entry', async () => {
    await seedSessionProject('s1', { activeProjectId: TRAILER, activeProjectName: 'Trailer cuts', webSearch: true });
    expect(getSessionProject('s1')).toEqual({
      activeProjectId: TRAILER, activeProjectName: 'Trailer cuts', webSearch: true, notice: null,
    });
    await seedSessionProject('s1', { activeProjectId: OTHER });
    expect(getSessionProject('s1')?.activeProjectId).toBe(TRAILER);

    await seedSessionProject('s2', {});
    expect(getSessionProject('s2')).toEqual({
      activeProjectId: null, activeProjectName: null, webSearch: false, notice: null,
    });
  });

  it('selectActiveProject PATCHes and updates the entry only on 200 (Req 3.3)', async () => {
    await seedSessionProject('s1', { activeProjectId: PERSONAL.id, activeProjectName: 'Personal' });
    mockPatch.mockResolvedValueOnce(NOT_FOUND);
    expect(await selectActiveProject('s1', OTHER, 'Gone')).toEqual(NOT_FOUND);
    expect(getSessionProject('s1')?.activeProjectId).toBe(PERSONAL.id);

    mockPatch.mockResolvedValueOnce(OK);
    expect(await selectActiveProject('s1', TRAILER, 'Trailer cuts')).toEqual(OK);
    expect(mockPatch).toHaveBeenLastCalledWith('s1', { activeProjectId: TRAILER });
    expect(getSessionProject('s1')).toMatchObject({ activeProjectId: TRAILER, activeProjectName: 'Trailer cuts' });
  });

  it('ignores an older selection that answers after a newer one', async () => {
    await seedSessionProject('s1', { activeProjectId: PERSONAL.id });
    let releaseFirst!: (v: typeof OK) => void;
    mockPatch.mockReturnValueOnce(new Promise((r) => { releaseFirst = r; }));
    mockPatch.mockResolvedValueOnce(OK);
    const first = selectActiveProject('s1', OTHER, 'Other');
    await selectActiveProject('s1', TRAILER, 'Trailer cuts');
    releaseFirst(OK);
    await first;
    expect(getSessionProject('s1')?.activeProjectId).toBe(TRAILER);
  });

  it('snapshot is frozen at submit time; later changes apply only to the next turn (Req 3.9)', async () => {
    await seedSessionProject('s1', { activeProjectId: PERSONAL.id, activeProjectName: 'Personal', webSearch: false });
    const turn1 = snapshot('s1');
    mockPatch.mockResolvedValue(OK);
    await selectActiveProject('s1', TRAILER, 'Trailer cuts');
    await setWebSearch('s1', true);
    expect(turn1).toEqual({ activeProjectId: PERSONAL.id, activeProjectName: 'Personal', webSearch: false });
    expect(Object.isFrozen(turn1)).toBe(true);
    expect(snapshot('s1')).toEqual({ activeProjectId: TRAILER, activeProjectName: 'Trailer cuts', webSearch: true });
  });

  it('resetToPersonal switches, shows the notice, and persists unless the server already did', async () => {
    await seedSessionProject('s1', { activeProjectId: TRAILER, activeProjectName: 'Trailer cuts', webSearch: true });
    mockPatch.mockResolvedValue(OK);
    await resetToPersonal('s1', { personalProject: PERSONAL });
    expect(getSessionProject('s1')).toEqual({
      activeProjectId: PERSONAL.id,
      activeProjectName: 'Personal',
      webSearch: true,
      notice: lostAccessMessage('Trailer cuts'),
    });
    expect(mockPatch).toHaveBeenCalledWith('s1', { activeProjectId: PERSONAL.id });

    mockPatch.mockClear();
    await resetToPersonal('s1', { personalProject: PERSONAL, lostProjectName: null, alreadyPersisted: true });
    expect(mockPatch).not.toHaveBeenCalled();
    expect(getSessionProject('s1')?.notice).toBe(lostAccessMessage(null));
    dismissNotice('s1');
    expect(getSessionProject('s1')?.notice).toBeNull();
  });

  it('reconcileWithProjectList labels, adopts Personal, or resets on a missing Active_Project (Req 3.12)', async () => {
    mockPatch.mockResolvedValue(OK);
    await seedSessionProject('known', { activeProjectId: TRAILER });
    expect(await reconcileWithProjectList('known', LIST)).toBe('ok');
    expect(getSessionProject('known')?.activeProjectName).toBe('Trailer cuts');

    await seedSessionProject('fresh', {});
    expect(await reconcileWithProjectList('fresh', LIST)).toBe('ok');
    expect(getSessionProject('fresh')).toMatchObject({ activeProjectId: PERSONAL.id, notice: null });

    // A chat with no row yet (no turn) still moves to Personal.
    await seedSessionProject('lost', { persisted: false, activeProjectId: OTHER, activeProjectName: 'Gone' });
    expect(await reconcileWithProjectList('lost', LIST)).toBe('lost');
    expect(getSessionProject('lost')).toMatchObject({
      activeProjectId: PERSONAL.id, notice: lostAccessMessage('Gone'),
    });
    expect(mockPatch).not.toHaveBeenCalled(); // no row to write: the first turn carries Personal

    // A chat with a row has had a turn: its project is fixed, so it keeps it, says so, and writes nothing.
    await seedSessionProject('ended', { activeProjectId: OTHER, activeProjectName: 'Gone' });
    expect(await reconcileWithProjectList('ended', LIST)).toBe('lost');
    expect(getSessionProject('ended')).toMatchObject({ activeProjectId: OTHER, notice: projectGoneMessage('Gone') });
    expect(getSessionProject('ended')!.notice).toMatch(/was deleted, so this chat can't continue/);
    expect(mockPatch).not.toHaveBeenCalled();
  });

  it('useSessionProject re-renders on change', async () => {
    function Probe({ sid }: { sid: string }) {
      const s = useSessionProject(sid);
      return <div>{s ? `${s.activeProjectName ?? '-'}|${s.webSearch ? 'on' : 'off'}` : 'none'}</div>;
    }
    render(<Probe sid="s1" />);
    expect(screen.getByText('none')).toBeInTheDocument();
    await act(() => seedSessionProject('s1', { activeProjectId: PERSONAL.id, activeProjectName: 'Personal' }));
    expect(screen.getByText('Personal|off')).toBeInTheDocument();
    mockPatch.mockResolvedValue(OK);
    await act(() => selectActiveProject('s1', TRAILER, 'Trailer cuts'));
    expect(screen.getByText('Trailer cuts|off')).toBeInTheDocument();
  });
});

describe('session ↔ project binding (asset-lineage D1, R2)', () => {
  const FIXED_409 = {
    ok: false, kind: 'error', status: 409, code: 'session_project_fixed', message: 'This chat already has turns',
  } as const;

  it('a session seeded with turns refuses another project without a request, and allows its own', async () => {
    await seedSessionProject('s1', { activeProjectId: TRAILER, activeProjectName: 'Trailer cuts', hasTurns: true });
    expect(getSessionProject('s1')).toMatchObject({ activeProjectId: TRAILER, projectFixed: true });
    const r = await selectActiveProject('s1', OTHER, 'Other');
    expect(r).toMatchObject({ ok: false, kind: 'error', status: 409, code: 'session_project_fixed' });
    expect(mockPatch).not.toHaveBeenCalled();
    expect(getSessionProject('s1')?.activeProjectId).toBe(TRAILER);

    mockPatch.mockResolvedValueOnce(OK);
    expect(await selectActiveProject('s1', TRAILER, 'Trailer cuts')).toEqual(OK);
  });

  it('a session with no turn still switches; markSessionProjectFixed fixes it and survives later writes', async () => {
    await seedSessionProject('s1', { activeProjectId: PERSONAL.id, activeProjectName: 'Personal' });
    expect(getSessionProject('s1')?.projectFixed).toBeUndefined();
    mockPatch.mockResolvedValue(OK);
    expect(await selectActiveProject('s1', TRAILER, 'Trailer cuts')).toEqual(OK);
    markSessionProjectFixed('s1');
    await setWebSearch('s1', true);
    expect(getSessionProject('s1')).toMatchObject({ activeProjectId: TRAILER, webSearch: true, projectFixed: true });
    expect((await selectActiveProject('s1', OTHER, 'Other')).ok).toBe(false);
    expect(mockPatch).toHaveBeenCalledTimes(2); // the switch and the web-search write, not the refused pick
  });

  it('learns the binding from the proxy’s 409 session_project_fixed (another tab, a reopened session)', async () => {
    await seedSessionProject('s1', { activeProjectId: TRAILER, activeProjectName: 'Trailer cuts' });
    markSessionPersisted('s1');
    mockPatch.mockResolvedValueOnce(FIXED_409);
    expect(await selectActiveProject('s1', OTHER, 'Other')).toEqual(FIXED_409);
    expect(getSessionProject('s1')).toMatchObject({ activeProjectId: TRAILER, projectFixed: true });
    // A bare `{error: "session_project_fixed"}` body is recognised too.
    await seedSessionProject('s2', { activeProjectId: TRAILER });
    mockPatch.mockResolvedValueOnce({ ok: false, kind: 'error', status: 409, code: 'http_error', message: 'session_project_fixed' });
    await selectActiveProject('s2', OTHER, 'Other');
    expect(getSessionProject('s2')?.projectFixed).toBe(true);
  });

  it('an unseeded session keeps the flag for its seed; forget clears it', async () => {
    markSessionProjectFixed('s9');
    await seedSessionProject('s9', { activeProjectId: TRAILER });
    expect(getSessionProject('s9')?.projectFixed).toBe(true);
    forgetSessionProject('s9');
    await seedSessionProject('s9', { activeProjectId: TRAILER });
    expect(getSessionProject('s9')?.projectFixed).toBeUndefined();
  });
});


describe('persistSessionForAction (a direct media action before the first turn)', () => {
  it('creates the row, then writes the chosen project; later changes PATCH as usual', async () => {
    mockPatch.mockResolvedValue(OK);
    await seedSessionProject('s-new', { persisted: false, activeProjectId: TRAILER, activeProjectName: 'Trailer cuts' });
    const create = vi.fn(async () => true);
    await persistSessionForAction('s-new', create);
    expect(create).toHaveBeenCalledWith('s-new');
    expect(mockPatch).toHaveBeenCalledWith('s-new', { activeProjectId: TRAILER });
    await persistSessionForAction('s-new', create);          // already saved: nothing again
    expect(create).toHaveBeenCalledTimes(1);
  });

  it('does nothing for a session that already has a row', async () => {
    await seedSessionProject('s-old', { activeProjectId: TRAILER });
    const create = vi.fn(async () => true);
    await persistSessionForAction('s-old', create);
    expect(create).not.toHaveBeenCalled();
    expect(mockPatch).not.toHaveBeenCalled();
  });
});


describe('Web Search default (user decision 2026-10-20)', () => {
  it('a NEW chat starts with Web Search on where it is deployed; a recorded choice is kept', async () => {
    const rc = await import('../config/runtimeConfig');
    const spy = vi.spyOn(rc, 'peekRuntimeConfig').mockReturnValue({ webSearchAvailable: true } as never);
    await seedSessionProject('new-chat', { persisted: false });
    expect(getSessionProject('new-chat')?.webSearch).toBe(true);
    await seedSessionProject('old-off', { webSearch: false });
    expect(getSessionProject('old-off')?.webSearch).toBe(false);
    await seedSessionProject('old-row', {});                 // a persisted row with no recorded value: unchanged (off)
    expect(getSessionProject('old-row')?.webSearch).toBe(false);
    spy.mockReturnValue({ webSearchAvailable: false } as never);
    await seedSessionProject('new-no-feature', { persisted: false });
    expect(getSessionProject('new-no-feature')?.webSearch).toBe(false);
    spy.mockRestore();
  });
});
