/** @vitest-environment jsdom */
import '@testing-library/jest-dom';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import HarnessTour, { popoverPosition } from './HarnessTour';
import HelpButton from './HelpButton';

describe('HarnessTour', () => {
  beforeEach(() => localStorage.clear());

  it('welcomes a first-time user (no saved sessions)', () => {
    render(<HarnessTour firstVisit />);
    expect(screen.getByTestId('tour-welcome')).toBeInTheDocument();
  });

  it('does not open by itself for a returning user, or before the sessions list has loaded', () => {
    const { unmount } = render(<HarnessTour firstVisit={false} />);
    expect(screen.queryByTestId('tour-welcome')).not.toBeInTheDocument();
    unmount();
    render(<HarnessTour firstVisit={null} />);
    expect(screen.queryByTestId('tour-welcome')).not.toBeInTheDocument();
  });

  it('stays closed once finished, and Skip records that', () => {
    const { unmount } = render(<HarnessTour firstVisit />);
    fireEvent.click(screen.getByRole('button', { name: 'Skip for now' }));
    expect(screen.queryByTestId('tour-welcome')).not.toBeInTheDocument();
    unmount();
    render(<HarnessTour firstVisit />);
    expect(screen.queryByTestId('tour-welcome')).not.toBeInTheDocument();
  });

  it('the help button brings it back after it was skipped', () => {
    localStorage.setItem('harness-tour-v1-done', '1');
    render(<><HarnessTour /><HelpButton /></>);
    expect(screen.queryByTestId('tour-welcome')).not.toBeInTheDocument();
    act(() => { fireEvent.click(screen.getByRole('button', { name: 'Take the tour' })); });
    expect(screen.getByTestId('tour-welcome')).toBeInTheDocument();
  });

  it('Start opens the first step as a hands-on step: no Next, but Skip step and Do it for me', () => {
    render(<HarnessTour firstVisit />);
    fireEvent.click(screen.getByTestId('tour-start'));
    const step = screen.getByTestId('tour-step');
    expect(step).toHaveAttribute('data-step', '0');
    expect(step).toHaveAttribute('data-kind', 'do');
    expect(screen.queryByTestId('tour-next')).toBeNull();
    expect(screen.getByTestId('tour-do-it')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('tour-skip-step'));
    expect(screen.getByTestId('tour-step')).toHaveAttribute('data-step', '1');
  });

  it('resumes the step it was on after a reload', () => {
    localStorage.setItem('harness-tour-v1-step', '3');
    render(<HarnessTour />);
    expect(screen.queryByTestId('tour-welcome')).not.toBeInTheDocument();
    const step = screen.getByTestId('tour-step');
    expect(step).toHaveAttribute('data-step', '3');
    expect(step).toHaveAttribute('data-kind', 'wait'); // a wait step docks; the user is free to work
  });

  it('finishing clears the saved step', () => {
    localStorage.setItem('harness-tour-v1-step', '3');
    render(<HarnessTour />);
    fireEvent.click(screen.getByRole('button', { name: 'Skip tour' }));
    expect(localStorage.getItem('harness-tour-v1-step')).toBeNull();
    expect(localStorage.getItem('harness-tour-v1-done')).toBe('1');
  });

  describe('popoverPosition (a small window)', () => {
    const card = { w: 340, h: 300 };
    it('takes the requested side when the card fits there', () => {
      const p = popoverPosition({ top: 200, left: 700, width: 300, height: 200 }, 'left', card, 1440, 900);
      expect(p.place).toBe('left');
      expect(p.style.left).toBe(700 - 340 - 16);
    });
    it('flips to the other side when the requested one has no room', () => {
      // the timeline sits at the window's left edge: no room on its left
      const p = popoverPosition({ top: 200, left: 40, width: 300, height: 200 }, 'left', card, 1100, 700);
      expect(p.place).toBe('right');
      expect(p.arrow).toBe('left');
    });
    it('never leaves the window, and narrows to fit it', () => {
      const p = popoverPosition({ top: 10, left: 10, width: 330, height: 680 }, 'left', card, 360, 700);
      const left = p.style.left as number, top = p.style.top as number, width = p.style.width as number;
      expect(width).toBe(360 - 24);
      expect(left).toBeGreaterThanOrEqual(12);
      expect(left + width).toBeLessThanOrEqual(360 - 12);
      expect(top).toBeGreaterThanOrEqual(12);
      expect(top + 300).toBeLessThanOrEqual(700 - 12);
      expect(p.arrow).toBe('none');
    });
    it('scrolls only a card taller than the window', () => {
      expect(popoverPosition(null, 'right', card, 1100, 700).style.overflowY).toBeUndefined();
      expect(popoverPosition(null, 'right', { w: 340, h: 676 }, 1100, 700).style.overflowY).toBe('auto');
    });
  });
});

/**
 * "Do it for me" on "Bring a video": attach the demo video (`0-demo.mp4`, pinned in every Personal project) by
 * name — switching an empty session to Personal first — never just whichever video happens to be first.
 * A fake + menu stands in for PlusMenu: the button toggles it, a project radio swaps its rows.
 */
describe('attachDemoVideo', () => {
  const clicked: string[] = [];
  const row = (label: string) => `<button role="menuitem" data-testid="plus-menu-recent-asset" aria-label="${label}">${label}</button>`;
  const ROWS: Record<string, string[]> = {
    personal: ['0-demo.mp4, 7 MB', 'later-upload.mp4, 3 MB'],
    other: ['campaign-cut.mp4, 9 MB', 'poster.png, 1 MB'],
  };
  let project = 'other';
  const realRect = HTMLElement.prototype.getBoundingClientRect;
  afterEach(() => { HTMLElement.prototype.getBoundingClientRect = realRect; });
  function renderMenu() {
    const menu = document.getElementById('menu')!;
    menu.innerHTML = `<input data-testid="plus-menu-asset-search" />`
      + `<button role="menuitemradio" aria-checked="${project === 'personal'}" aria-label="Personal, private, owner" data-p="personal">Personal</button>`
      + `<button role="menuitemradio" aria-checked="${project === 'other'}" aria-label="Campaign, owner" data-p="other">Campaign</button>`
      + ROWS[project].map(row).join('');
    menu.querySelectorAll<HTMLElement>('[role="menuitemradio"]').forEach((b) => b.addEventListener('click', () => {
      project = b.dataset.p!;
      menu.innerHTML = ''; // picking a project closes the menu, as PlusMenu does
    }));
    menu.querySelectorAll<HTMLElement>('[data-testid="plus-menu-recent-asset"]').forEach((b) =>
      b.addEventListener('click', () => clicked.push(b.getAttribute('aria-label')!)));
  }
  beforeEach(() => {
    clicked.length = 0;
    // The tour only clicks what is visible; jsdom lays nothing out, so give every element a box.
    HTMLElement.prototype.getBoundingClientRect = () => ({ x: 0, y: 0, top: 0, left: 0, right: 10, bottom: 10,
      width: 10, height: 10, toJSON: () => ({}) }) as DOMRect;
    document.body.innerHTML = '<button aria-label="Attach and tools" id="plus"></button><div id="menu"></div>';
    document.getElementById('plus')!.addEventListener('click', () => {
      const menu = document.getElementById('menu')!;
      if (menu.innerHTML) menu.innerHTML = ''; else renderMenu();
    });
  });

  it('picks 0-demo.mp4 when the session is already in Personal', async () => {
    const { attachDemoVideo } = await import('./HarnessTour');
    project = 'personal';
    vi.useFakeTimers();
    const run = attachDemoVideo();
    await vi.runAllTimersAsync();
    await run;
    vi.useRealTimers();
    expect(clicked).toEqual(['0-demo.mp4, 7 MB']);
  });

  it('switches an empty session from another project to Personal, then picks 0-demo.mp4', async () => {
    const { attachDemoVideo } = await import('./HarnessTour');
    project = 'other';
    vi.useFakeTimers();
    const run = attachDemoVideo();
    await vi.runAllTimersAsync();
    await run;
    vi.useRealTimers();
    expect(project).toBe('personal');
    expect(clicked).toEqual(['0-demo.mp4, 7 MB']);
  });

  it('falls back to the first video when Personal has no demo (a user deleted it)', async () => {
    const { attachDemoVideo } = await import('./HarnessTour');
    project = 'personal';
    const saved = ROWS.personal;
    ROWS.personal = ['later-upload.mp4, 3 MB'];
    vi.useFakeTimers();
    const run = attachDemoVideo();
    await vi.runAllTimersAsync();
    await run;
    vi.useRealTimers();
    ROWS.personal = saved;
    expect(clicked).toEqual(['later-upload.mp4, 3 MB']);
  });
});
