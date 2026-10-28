/**
 * HarnessTour — first-run guided tour of the harness (LOCAL MOCK, 2026-10-13).
 *
 * A welcome banner, then steps. Three kinds:
 *  - describe (default): the screen dims, one control glows, a popover explains it; Next moves on.
 *  - do: the user performs the action ON the highlighted control (the hole is click-through, everything else
 *    is blocked). The step advances by itself when its `done` check sees the result in the UI — e.g. an
 *    attachment chip, a sent message, a new job. "Do it for me" performs it instead; "Skip step" moves on.
 *  - wait: a job is running. No dim — the user is free to keep working — and a small card docks bottom-right
 *    until `done` fires (the job finished, a notification arrived).
 * `done` gets the value `snap` took when the step began, so "a NEW job" is "more jobs than at the start".
 *
 * Progress is saved, so a reload resumes the step it was on. Shown once; `?tour=1` forces it, and the ? beside
 * the bell reopens it (`startHarnessTour`).
 *
 * Step popovers use theme tokens only (global.css), so they follow Light / Dark / KiroCrew. The welcome
 * banner deliberately uses the splash page's fixed palette — it is the brand moment, not app chrome.
 */
import React, { useCallback, useEffect, useLayoutEffect, useState } from 'react';
import { TOUR_START_EVENT } from './startTour';
import { getActiveLogo } from '../../config/branding';
import { HERO_COPY } from '../../views/landingCopy';

const DONE_KEY = 'harness-tour-v1-done';
/** The step a tour in progress is on, so a reload (or a long job) doesn't lose the user's place. */
const STEP_KEY = 'harness-tour-v1-step';
const POLL_MS = 400;
const PAD = 8;
const POP_W = 340;

type Section = 'Chat' | 'Explore';
type FindOne = () => HTMLElement | null;
type Find = FindOne | (() => HTMLElement[] | null);
/** `inside` = top-centre within the highlight, for a whole panel too wide to sit beside. */
type Place = 'right' | 'left' | 'top' | 'bottom' | 'inside';

type StepKind = 'describe' | 'do' | 'wait';

interface TourStep {
  section: Section;
  title: string;
  body: React.ReactNode;
  find?: Find;
  before?: () => void;
  place?: Place;
  /** Default `describe`. */
  kind?: StepKind;
  /** Taken when the step begins; handed to `done`, so a check can mean "changed since the step started". */
  snap?: () => unknown;
  /** The step's goal is visible in the UI — advance. Polled while the step is open (do / wait steps). */
  done?: (snap: unknown) => boolean;
  /** "Do it for me": perform the action for a user who is stuck. */
  doIt?: () => void | Promise<void>;
}

/** Rendered and on screen. Not `offsetParent` — that is null for `position: fixed` controls (the panel edge toggle). */
const visible = (el: Element | null): el is HTMLElement => {
  if (!(el instanceof HTMLElement)) return false;
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden' && !el.closest('[style*="display: none"]');
};
/** Matches the aria-label, or the text's first line (nav entries carry a count on a second line: "Projects\n1"). */
const byLabel = (label: string | RegExp): FindOne => () => {
  const hit = (s: string) => (typeof label === 'string' ? s.startsWith(label) : label.test(s));
  return [...document.querySelectorAll<HTMLElement>('button,[role=button],a')]
    .find((b) => visible(b) && (hit((b.getAttribute('aria-label') || '').trim()) || hit((b.innerText || '').trim().split('\n')[0].trim()))) ?? null;
};
const bySelector = (sel: string): FindOne => () => [...document.querySelectorAll<HTMLElement>(sel)].find(visible) ?? null;
const clickNav = (label: string) => () => byLabel(label)()?.click();
/** The right panel can be collapsed, and Jobs is the second tab beside Renderer — open both as needed. */
const showRight = (tab: 'Renderer' | 'Jobs') => () => {
  byLabel('Show panel')()?.click();
  window.setTimeout(() => byLabel(tab)()?.click(), 60);
};
/** Chat steps can be reached with Back from an Explore step, so return to the chat surface first. */
const toChat = () => byLabel(/^Chat$/)()?.click();
// ---- the chat walk-through: real controls, real state -------------------------------------------------------
const turnRunning = () => !!byLabel('Stop agent')();
/** One `⏱ 12.3s` summary per finished assistant turn — the most reliable "the answer is complete" signal. */
const finishedTurns = () => (region('center')()?.innerText.match(/⏱/g) || []).length;
const turnDone = (before: unknown) => !turnRunning() && finishedTurns() > (before as number);
const isEmptySession = () => /what.s on your mind\?/i.test(region('center')()?.innerText || '');
const count = (sel: string) => document.querySelectorAll(sel).length;
const click = (find: FindOne) => find()?.click();
const later = (ms: number) => new Promise((r) => window.setTimeout(r, ms));
/** The tour's first ask. It must work on any video in any project — "what is this about?" is refused in a project
 *  without search indexing — so it asks for what every video has: its technical metadata. */
const FIRST_ASK = 'Extract the technical metadata of this video.';
const rendererTabs = () => [...document.querySelectorAll<HTMLElement>('[data-tour="right-panel"] [role="tab"]')];
const succeededJobs = () => count('[data-job-row][title$="· SUCCEEDED"]');
/** Renderer tabs when the user clicked Clip: a clip runs directly (no turn, no job) and opens as a new tab. */
let clipBaseline = { tabs: 0 };
const clipTabOpen = () => rendererTabs().length > clipBaseline.tabs;
const lastTab = () => { const t = rendererTabs(); return t.length ? t[t.length - 1] : null; };

/** "Do it for me" on the timeline: drag a range over roughly the first quarter, as a user would. */
function dragRange() {
  const ruler = document.querySelector<HTMLElement>('[data-testid="timeline-ruler"]');
  if (!ruler) return;
  const r = ruler.getBoundingClientRect();
  const y = r.top + r.height / 2, x1 = r.left + r.width * 0.04, x2 = r.left + r.width * 0.28;
  const opts = (x: number) => ({ clientX: x, clientY: y, bubbles: true, pointerId: 1, button: 0, isPrimary: true });
  ruler.dispatchEvent(new PointerEvent('pointerdown', opts(x1)));
  window.dispatchEvent(new PointerEvent('pointermove', opts(x2)));
  window.dispatchEvent(new PointerEvent('pointerup', opts(x2)));
}

/** "Do it for me" on the + menu: open it and attach a VIDEO from the project — never just the first item (a
 *  project's recent list is often generated images). Searches by extension when none is listed. */
const VIDEO_NAME = /\.(mp4|mov|m4v|webm|mkv)\b/i;
const videoItems = () => [...document.querySelectorAll<HTMLElement>('[data-testid="plus-menu-recent-asset"]')]
  .filter((b) => b.getAttribute('aria-disabled') !== 'true' && VIDEO_NAME.test(b.getAttribute('aria-label') || ''));
/** The tour's video: every user's Personal project holds it, pinned first (`infra/lib/personal-samples.ts`). */
export const DEMO_VIDEO = '0-demo.mp4';
const demoItem = () => videoItems().find((b) => (b.getAttribute('aria-label') || '').startsWith(`${DEMO_VIDEO},`)
  || (b.getAttribute('aria-label') || '') === DEMO_VIDEO);
const plusMenuOpen = () => !!document.querySelector('[data-testid="plus-menu-asset-search"]');
async function searchPlusMenu(text: string) {
  const search = document.querySelector<HTMLInputElement>('[data-testid="plus-menu-asset-search"]');
  if (!search) return;
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set?.call(search, text);
  search.dispatchEvent(new Event('input', { bubbles: true }));
  await later(1500);
}
/** Attach the demo video: switch the (still empty) session to Personal if it is elsewhere, then pick
 *  `0-demo.mp4` by name. Falls back to the first video when the demo is not there (e.g. a user who deleted it). */
export async function attachDemoVideo() {
  if (!plusMenuOpen()) { click(byLabel('Attach and tools')); await later(500); }
  if (!demoItem()) {
    // The session's project is the + menu's checked radio; Personal's row is labelled "Personal, private, …".
    const personal = [...document.querySelectorAll<HTMLElement>('[role="menuitemradio"]')]
      .find((r) => /, private, /.test(r.getAttribute('aria-label') || ''));
    if (personal && personal.getAttribute('aria-checked') !== 'true') {
      personal.click();
      await later(800);
      if (!plusMenuOpen()) { click(byLabel('Attach and tools')); await later(800); }
    }
  }
  if (!demoItem()) await searchPlusMenu(DEMO_VIDEO);
  const demo = demoItem();
  if (demo) { demo.click(); return; }
  if (!videoItems().length) await searchPlusMenu('.mp4');
  videoItems()[0]?.click();
}

const region = (name: string): FindOne => bySelector(`[data-tour="${name}"]`);
/** A finished job announces itself twice: a banner dropped beneath the bell (12s), and the bell's list. Highlight
 *  both, so whichever the user reaches for is inside the click-through hole. */
const bell = byLabel(/^Notifications/);
const banner = bySelector('.lab-notif-banner');
const notice: Find = () => { const els = [bell(), banner()].filter((e): e is HTMLElement => !!e); return els.length ? els : null; };
async function openNotice() {
  const b = document.querySelector<HTMLElement>('.lab-notif-banner-body');
  if (b && visible(b)) { b.click(); return; }
  click(bell); await later(300); click(bySelector('.lab-notif-item'));
}
/** Several controls highlighted as one (the Workflow / Agents / Skills nav entries). */
const all = (...finds: FindOne[]): Find => () => {
  const els = finds.map((f) => f()).filter((e): e is HTMLElement => !!e);
  return els.length ? els : null;
};

const STEPS: TourStep[] = [
  { section: 'Chat', kind: 'do', title: 'Start a session', before: toChat, find: byLabel(/^(\+\s*)?New session$/), place: 'right',
    done: isEmptySession, doIt: () => click(byLabel(/^(\+\s*)?New session$/)),
    body: <>Every chat is a <b>session</b> with its own conversation and results. Click <b>New session</b>.</> },
  { section: 'Chat', kind: 'do', title: 'Bring a video', find: byLabel('Attach and tools'), place: 'top',
    done: () => count('[data-testid="attached-file-chip"]') > 0, doIt: attachDemoVideo,
    body: <>Click <b>+</b> and pick <b>{DEMO_VIDEO}</b>, the first file in your <b>Personal</b> project: a short recording of this app. It's attached by reference — nothing is copied.</> },
  { section: 'Chat', kind: 'do', title: 'Ask about it',
    before: () => window.dispatchEvent(new CustomEvent('chat-input:set', { detail: { value: FIRST_ASK } })),
    find: all(bySelector('textarea[aria-label="Chat message input"]'), byLabel('Send message')), place: 'top',
    done: turnRunning, doIt: () => click(byLabel('Send message')),
    body: <>We've typed a first question for you. Press <b>Send</b> — or write your own.</> },
  { section: 'Chat', kind: 'wait', title: 'The agent is answering', snap: finishedTurns, done: turnDone,
    body: 'It reads the video and answers in the chat. The video itself opens in the renderer on the right.' },
  { section: 'Chat', kind: 'do', title: 'Pick a range', before: showRight('Renderer'),
    find: bySelector('[data-testid="timeline-editor"]'), place: 'left',
    done: () => count('[data-testid^="range-band-"]') > 0, doIt: dragRange,
    body: <>This is the video's timeline. <b>Drag along the ruler</b> to mark the part you want — it snaps to shot cuts.</> },
  { section: 'Chat', kind: 'do', title: 'Cut a clip', find: bySelector('[data-testid="action-clip"]'), place: 'left',
    snap: () => { clipBaseline = { tabs: rendererTabs().length }; return null; },
    done: clipTabOpen, doIt: () => click(bySelector('[data-testid="action-clip"]')),
    body: <>Click <b>Clip</b>. It's cut right away — no agent turn, no job — and opens in a new tab; the chat keeps a
      line of what you did. <b>Frame</b> and <b>Crop</b> work the same way.</> },
  { section: 'Chat', kind: 'do', title: 'Open your clip', before: showRight('Renderer'),
    find: lastTab, place: 'bottom',
    done: () => lastTab()?.getAttribute('aria-selected') === 'true',
    doIt: () => lastTab()?.click(),
    body: <>Your clip opened in its own tab. <b>Click it</b> if it isn't selected — everything you run next works on just this clip.</> },
  { section: 'Chat', kind: 'do', title: 'Find the shots', find: bySelector('[data-testid="analyze-menu"]'), place: 'left',
    done: turnRunning,
    doIt: async () => { click(bySelector('[data-testid="analyze-menu"]')); await later(300); click(bySelector('[data-testid="analyze-item-shots"]')); },
    body: <>Heavier analysis runs as a background <b>job</b>. On your clip, open <b>Analyze</b> and choose <b>Shot Segmentation</b>.</> },
  { section: 'Chat', kind: 'wait', title: 'Starting the job', snap: finishedTurns, done: turnDone,
    body: 'The agent hands the work to a background job and replies right away.' },
  { section: 'Chat', title: 'Watch it in Jobs', before: showRight('Jobs'), find: region('right-panel'), place: 'left',
    body: <>Every job this chat started, with its status. A running job pulses; when it turns <b>Completed</b> you'll also get a notification. You can keep chatting meanwhile.</> },
  { section: 'Chat', kind: 'wait', title: 'Shot segmentation is running', find: region('right-panel'),
    snap: succeededJobs, done: (n) => succeededJobs() > (n as number),
    body: 'This takes a few minutes. Explore anything you like — the tour picks up when the job is done.' },
  { section: 'Chat', kind: 'do', title: 'Bring the results back', find: notice, place: 'bottom',
    done: turnRunning, doIt: openNotice,
    body: <>Your job finished. Click the <b>notification</b> under the bell (or open the <b>bell</b> and pick it) — <b>View results</b> brings the shots onto your clip's timeline, and the agent summarises what it found.</> },
  { section: 'Chat', kind: 'wait', title: 'Fetching the results', snap: finishedTurns, done: turnDone,
    body: 'The shots appear on the clip\'s timeline as a Shots lane.' },
  { section: 'Explore', title: 'Projects', before: clickNav('Projects'), find: byLabel(/^Projects$/), place: 'right',
    body: <>Your files live in <b>projects</b> — the media, its analysis, and the chats about it. You start with a default project; create more for a campaign, a show or a team and invite members. A <b>magnifier</b> marks a searchable project: its media is analysed, so you can search it and ask about what's inside.</> },
  { section: 'Explore', title: 'The file browser', before: () => bySelector('[data-project-row]')()?.click(), find: region('center'), place: 'inside',
    body: <>A project's files. Search by name, filter by type or folder, switch between icons and a sortable list, and <b>upload</b> files or a whole folder — in a searchable project they are analysed automatically. Click any file to open it.</> },
  { section: 'Explore', title: 'Workflow, Agents and Skills', find: all(byLabel(/^Workflow$/), byLabel(/^Agents$/), byLabel(/^Skills$/)), place: 'right',
    body: <><b>Workflow</b> shows multi-step plans the agent runs, step by step. <b>Agents</b> is where you build your own — a role, tools and skills — to use in chat or share. <b>Skills</b> is reusable know-how the agent loads when needed: browse the built-ins or write your own.</> },
  { section: 'Explore', title: 'Settings', find: region('settings'), place: 'right',
    body: <><b>Capabilities</b> holds the settings: <b>General</b> for theme and text size, <b>Model</b> for the agent's model, <b>Tools</b> and <b>Hooks</b> for what it can call and what runs around each turn, and <b>Improve</b> for feedback on past answers. Your name at the bottom signs you out.</> },
];

/** The route the welcome banner lists: every step the user acts on or reads — waits are not stops. */
const STOPS = STEPS.filter((st) => st.kind !== 'wait').map((st, k) => ({ ...st, n: k + 1 }));

const SECTION_META: Record<Section, { icon: string; blurb: string }> = {
  Chat: { icon: '💬', blurb: 'Ask, open media, run a job, get the results back.' },
  Explore: { icon: '🧭', blurb: 'Where your files, workflows, agents and skills live.' },
};

function forced(): boolean {
  try { return new URLSearchParams(window.location.search).get('tour') === '1'; } catch { return false; }
}

function savedStep(): number | null {
  try {
    const v = localStorage.getItem(STEP_KEY);
    const n = v === null ? NaN : Number(v);
    return Number.isInteger(n) && n >= 0 && n < STEPS.length ? n : null;
  } catch { return null; }
}

/** Typing in the harness (composer, inputs) must not page the tour. */
const typingIn = (t: EventTarget | null) =>
  t instanceof HTMLElement && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));

const tourDone = () => { try { return !!localStorage.getItem(DONE_KEY); } catch { return true; } };

/** `firstVisit`: the user has no saved sessions (WorkspaceView), `null` while unknown. The tour opens by itself
 *  ONLY then — a returning user who never took it is not ambushed by it; they reopen it from ?. */
export default function HarnessTour({ firstVisit = null }: { firstVisit?: boolean | null } = {}) {
  const [stage, setStage] = useState<'off' | 'welcome' | 'steps'>(() => {
    if (forced()) return 'welcome';
    if (savedStep() !== null) return 'steps';
    return 'off';
  });
  useEffect(() => {
    if (firstVisit === true && !tourDone()) setStage((st) => (st === 'off' ? 'welcome' : st));
  }, [firstVisit]);
  const [i, setI] = useState(() => (forced() ? 0 : savedStep() ?? 0));
  const [rect, setRect] = useState<DOMRect | null>(null);
  const [justDone, setJustDone] = useState(false);
  const step = STEPS[i];
  const kind = step.kind ?? 'describe';

  const finish = useCallback(() => {
    try { localStorage.setItem(DONE_KEY, '1'); localStorage.removeItem(STEP_KEY); } catch { /* storage off */ }
    setStage('off');
  }, []);
  const go = useCallback((n: number) => setI(Math.max(0, Math.min(STEPS.length - 1, n))), []);
  const next = useCallback(() => (i === STEPS.length - 1 ? finish() : go(i + 1)), [i, go, finish]);

  useEffect(() => {
    if (stage !== 'steps') return;
    try { localStorage.setItem(STEP_KEY, String(i)); } catch { /* ignore */ }
  }, [stage, i]);

  // Enter a step: run its setup, snapshot, then keep measuring (an interactive UI moves — menus open, panels
  // fill) and, for do/wait steps, watch for the goal.
  useLayoutEffect(() => {
    if (stage !== 'steps') return;
    setJustDone(false);
    step.before?.();
    const snap = step.snap?.();
    let alive = true, advanced = false;
    const tick = () => {
      if (!alive) return;
      setRect(unionRect(collect(step)));
      if (!advanced && kind !== 'describe' && step.done?.(snap)) {
        advanced = true;
        setJustDone(true);
        window.setTimeout(() => { if (alive) next(); }, 900);
      }
    };
    const first = window.setTimeout(tick, 260);
    const poll = window.setInterval(tick, POLL_MS);
    window.addEventListener('resize', tick);
    return () => { alive = false; window.clearTimeout(first); window.clearInterval(poll); window.removeEventListener('resize', tick); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stage, i]);

  useEffect(() => {
    const reopen = () => { setI(0); setStage('welcome'); };
    window.addEventListener(TOUR_START_EVENT, reopen);
    return () => window.removeEventListener(TOUR_START_EVENT, reopen);
  }, []);

  useEffect(() => {
    if (stage === 'off') return;
    const onKey = (e: KeyboardEvent) => {
      if (stage === 'welcome') {
        if (e.key === 'Escape') finish();
        if (e.key === 'Enter' || e.key === 'ArrowRight') { setI(0); setStage('steps'); }
        return;
      }
      // While the user is doing things, keys belong to the harness (Esc closes its menus, arrows scrub video).
      if (kind !== 'describe' || typingIn(e.target)) return;
      if (e.key === 'Escape') finish();
      if (e.key === 'ArrowRight') next();
      if (e.key === 'ArrowLeft') go(i - 1);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [stage, i, kind, go, next, finish]);

  if (stage === 'off') return null;

  const doIt = step.doIt ? () => { void step.doIt!(); } : undefined;
  return (
    <>
      <style>{TOUR_CSS}</style>
      {stage === 'welcome' ? (
        <div className="ht-backdrop" role="dialog" aria-modal="true" aria-labelledby="ht-welcome-title" data-testid="tour-welcome">
          <Splash onStart={() => { setI(0); setStage('steps'); }} onSkip={finish} />
        </div>
      ) : kind === 'wait' ? (
        <Docked step={step} i={i} rect={rect} justDone={justDone} onSkip={next} onFinish={finish} />
      ) : (
        <Spotlight step={step} i={i} rect={rect} justDone={justDone} onDoIt={doIt}
          onBack={() => go(i - 1)} onNext={next} onFinish={finish} />
      )}
    </>
  );
}

/** What a step highlights: its target, plus — on a `do` step — any menu or dialog the user opened from it, so the
 *  click-through hole follows them into the picker instead of blocking it. */
function collect(step: TourStep): HTMLElement | HTMLElement[] | null {
  const found = step.find?.() ?? null;
  if ((step.kind ?? 'describe') !== 'do') return found;
  const extra = [...document.querySelectorAll<HTMLElement>('[role="menu"],[role="listbox"],[role="dialog"]')]
    .filter((el) => visible(el) && !el.closest('.ht-layer,.ht-dock,.ht-backdrop'));
  const base = found ? (Array.isArray(found) ? found : [found]) : [];
  const els = [...base, ...extra];
  return els.length ? els : null;
}

/** The welcome banner — the splash page's look AND copy (LandingPage.css palette, `HERO_COPY`), so the first
 *  screen after sign-in reads as the same product moment as the page before it. Left: the logo and the pitch.
 *  Right: the route the tour takes. */
function Splash({ onStart, onSkip }: { onStart: () => void; onSkip: () => void }) {
  const logo = getActiveLogo();
  return (
    <div className="ht-splash">
      <div className="ht-splash-orb ht-splash-orb--one" /><div className="ht-splash-orb ht-splash-orb--two" />
      <button type="button" className="ht-splash-close" onClick={onSkip} aria-label="Close the tour">✕</button>
      <div className="ht-splash-copy">
        <div className="ht-splash-logo"><span className="ht-splash-logo-pulse" /><img src={logo.src} alt="" /></div>
        <div className="ht-splash-eyebrow"><span />{HERO_COPY.eyebrow}</div>
        <h2 id="ht-welcome-title">{HERO_COPY.headline} <em>{HERO_COPY.headlineEmphasis}</em></h2>
        <p className="ht-splash-lead">{HERO_COPY.lead}</p>
        <div className="ht-splash-actions">
          <button type="button" className="ht-splash-primary" onClick={onStart} data-testid="tour-start" autoFocus>
            Take the guided tour <ArrowIcon />
          </button>
          <button type="button" className="ht-splash-quiet" onClick={onSkip}>Skip for now</button>
        </div>
        <p className="ht-splash-note">Reopen it any time from <b>?</b> at the top right.</p>
      </div>
      <div className="ht-route" aria-hidden="true">
        <div className="ht-route-label">The tour · about 10 minutes</div>
        {(['Chat', 'Explore'] as Section[]).map((sec) => (
          <div key={sec} className="ht-route-group">
            <div className="ht-route-sec">{sec}</div>
            {STOPS.filter((st) => st.section === sec).map((st) => (
              <div key={st.n} className="ht-route-stop"><span className="ht-route-dot">{st.n}</span>{st.title}</div>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

function ArrowIcon() {
  return <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6" /></svg>;
}

function Spotlight({ step, i, rect, justDone, onDoIt, onBack, onNext, onFinish }: {
  step: TourStep; i: number; rect: DOMRect | null; justDone: boolean; onDoIt?: () => void;
  onBack: () => void; onNext: () => void; onFinish: () => void;
}) {
  const interactive = step.kind === 'do';
  const hole = rect ? clampHole(rect) : null;
  // Placed from the card's REAL size: its height varies with the copy, and in a small window a fixed guess put
  // it over the control it describes or past the bottom edge.
  const cardRef = React.useRef<HTMLDivElement | null>(null);
  const [size, setSize] = useState<{ w: number; h: number } | null>(null);
  useLayoutEffect(() => {
    const el = cardRef.current;
    if (!el) return;
    const measure = () => setSize((prev) => (prev && prev.w === el.offsetWidth && prev.h === el.offsetHeight ? prev : { w: el.offsetWidth, h: el.offsetHeight }));
    measure();
    const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(measure) : null;
    ro?.observe(el);
    return () => ro?.disconnect();
  }, [i, justDone]);
  const { style, arrow } = popoverPosition(hole, step.place, size);
  const firstOfSection = i === 0 || STEPS[i - 1].section !== step.section;
  const last = i === STEPS.length - 1;
  return (
    <div className={`ht-layer${interactive ? ' ht-interactive' : ''}`} data-testid="tour-step" data-step={i} data-kind={step.kind ?? 'describe'}>
      {interactive && hole ? (
        // Four panels around the hole: they dim AND block, while the hole itself is a real gap the user's
        // clicks fall through to the control. (An SVG mask dims but cannot let clicks through one region.)
        <>
          <div className="ht-block" style={{ top: 0, left: 0, right: 0, height: hole.top }} />
          <div className="ht-block" style={{ top: hole.top + hole.height, left: 0, right: 0, bottom: 0 }} />
          <div className="ht-block" style={{ top: hole.top, left: 0, width: hole.left, height: hole.height }} />
          <div className="ht-block" style={{ top: hole.top, left: hole.left + hole.width, right: 0, height: hole.height }} />
        </>
      ) : (
        /* Full-screen dim with a rounded cut-out (SVG mask — robust, unlike a giant box-shadow). */
        <svg className="ht-mask" aria-hidden="true">
          <defs>
            <mask id="ht-cutout">
              <rect width="100%" height="100%" fill="white" />
              {hole && <rect className="ht-cut" x={hole.left} y={hole.top} width={hole.width} height={hole.height} rx={12} fill="black" />}
            </mask>
          </defs>
          <rect className="ht-dim" width="100%" height="100%" mask="url(#ht-cutout)" />
        </svg>
      )}
      {hole && <div className={`ht-hole${justDone ? ' ht-hole-done' : ''}`} style={hole} />}
      <div key={i} ref={cardRef} role="dialog" aria-labelledby="ht-step-title" className={`ht-card ht-pop ht-arrow-${arrow}`} style={style}>
        <div className="ht-accent" />
        <div className="ht-pop-head">
          <span className="ht-section"><span aria-hidden="true">{SECTION_META[step.section].icon}</span>{step.section}</span>
          {interactive && <span className="ht-yourturn">Your turn</span>}
          <span className="ht-count">{i + 1} / {STEPS.length}</span>
        </div>
        {firstOfSection && <div className="ht-chapter">{SECTION_META[step.section].blurb}</div>}
        <h3 id="ht-step-title" className="ht-step-title">{justDone ? <>✓ {step.title}</> : step.title}</h3>
        <div className="ht-body">{step.body}</div>
        <Progress i={i} />
        <div className="ht-pop-actions">
          <button type="button" className="ht-link" onClick={onFinish}>Skip tour</button>
          <span style={{ flex: 1 }} />
          {interactive ? (
            <>
              <button type="button" className="ht-link" onClick={onNext} data-testid="tour-skip-step">Skip step</button>
              {onDoIt && <button type="button" className="ht-btn ht-ghost ht-sm" onClick={onDoIt} data-testid="tour-do-it">Do it for me</button>}
            </>
          ) : (
            <>
              {i > 0 && <button type="button" className="ht-btn ht-ghost ht-sm" onClick={onBack}>Back</button>}
              {last
                ? <button type="button" className="ht-btn ht-primary ht-sm" onClick={onFinish}>Finish ✓</button>
                : <button type="button" className="ht-btn ht-primary ht-sm" onClick={onNext} data-testid="tour-next">Next</button>}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

/** A `wait` step: no dim — the user keeps working while a job runs — just a small card docked bottom-right. */
function Docked({ step, i, rect, justDone, onSkip, onFinish }: {
  step: TourStep; i: number; rect: DOMRect | null; justDone: boolean; onSkip: () => void; onFinish: () => void;
}) {
  // A wait step may point at where the work shows (the Jobs panel): a ring only — no dim, nothing blocked.
  const ring = rect ? clampHole(rect) : null;
  return (
    <>
    {ring && <div className="ht-hole ht-ring" style={ring} data-testid="tour-ring" />}
    <div className="ht-card ht-dock" role="status" aria-live="polite" data-testid="tour-step" data-step={i} data-kind="wait">
      <div className="ht-accent" />
      <div className="ht-pop-head">
        <span className={`ht-spinner${justDone ? ' ht-spinner-done' : ''}`} aria-hidden="true" />
        <span className="ht-dock-title">{justDone ? <>✓ {step.title}</> : step.title}</span>
        <span className="ht-count">{i + 1} / {STEPS.length}</span>
      </div>
      <div className="ht-body">{step.body}</div>
      <div className="ht-pop-actions">
        <button type="button" className="ht-link" onClick={onFinish}>Skip tour</button>
        <span style={{ flex: 1 }} />
        <button type="button" className="ht-link" onClick={onSkip} data-testid="tour-skip-step">Don't wait</button>
      </div>
    </div>
    </>
  );
}

function unionRect(found: HTMLElement | HTMLElement[] | null): DOMRect | null {
  const els = found ? (Array.isArray(found) ? found : [found]) : [];
  if (!els.length) return null;
  const rs = els.map((e) => e.getBoundingClientRect());
  const left = Math.min(...rs.map((r) => r.left)), top = Math.min(...rs.map((r) => r.top));
  return new DOMRect(left, top, Math.max(...rs.map((r) => r.right)) - left, Math.max(...rs.map((r) => r.bottom)) - top);
}

/** One segment per step, grouped by section with a gap between sections. */
function Progress({ i }: { i: number }) {
  return (
    <div className="ht-progress" aria-hidden="true">
      {(['Chat', 'Explore'] as Section[]).map((sec) => (
        <div key={sec} className="ht-progress-group">
          {STEPS.map((s, n) => s.section === sec && (
            <span key={n} className={`ht-seg${n < i ? ' done' : n === i ? ' now' : ''}`} />
          ))}
        </div>
      ))}
    </div>
  );
}

type Hole = { top: number; left: number; width: number; height: number };

/** A highlight padded by PAD, kept 4px inside the viewport (a whole-panel highlight would spill off-screen). */
function clampHole(rect: DOMRect): Hole {
  const left = Math.max(4, rect.left - PAD), top = Math.max(4, rect.top - PAD);
  const right = Math.min(window.innerWidth - 4, rect.right + PAD), bottom = Math.min(window.innerHeight - 4, rect.bottom + PAD);
  return { top, left, width: Math.max(0, right - left), height: Math.max(0, bottom - top) };
}

const EDGE = 12, GAP = 16;
const FLIP: Record<Place, Place[]> = {
  right: ['right', 'left', 'bottom', 'top'], left: ['left', 'right', 'bottom', 'top'],
  top: ['top', 'bottom', 'right', 'left'], bottom: ['bottom', 'top', 'left', 'right'], inside: ['inside'],
};

/** Popover beside the hole, its arrow pointing back at the control. It takes the requested side when the card
 *  fits there, else the next side that fits (a narrow window has no room left of the timeline); if none does it
 *  is clamped into the viewport, arrowless, rather than drawn half off-screen. Centred when the control isn't on
 *  screen. `size` is the card's measured size (null on the first frame). Exported for tests. */
export function popoverPosition(hole: Hole | null, place: Place = 'right', size: { w: number; h: number } | null = null,
  vw = window.innerWidth, vh = window.innerHeight) {
  const W = Math.min(POP_W, vw - 2 * EDGE);
  const H = Math.min(size?.h ?? 270, vh - 2 * EDGE);
  // Scroll only a card taller than the window: overflow would also clip the arrow drawn just outside its edge.
  const tooTall = !!size && size.h >= vh - 2 * EDGE;
  const base: React.CSSProperties = tooTall ? { width: W, maxHeight: vh - 2 * EDGE, overflowY: 'auto' } : { width: W };
  const clampTop = (t: number) => Math.max(EDGE, Math.min(t, vh - H - EDGE));
  const clampLeft = (l: number) => Math.max(EDGE, Math.min(l, vw - W - EDGE));
  if (!hole) return { style: { ...base, top: clampTop(vh / 2 - H / 2), left: clampLeft(vw / 2 - W / 2) }, arrow: 'none', place: 'none' };
  const at = (p: Place) => {
    switch (p) {
      case 'right': return { top: hole.top + hole.height / 2 - 40, left: hole.left + hole.width + GAP, arrow: 'left',
        fits: hole.left + hole.width + GAP + W <= vw - EDGE };
      case 'left': return { top: hole.top + hole.height / 2 - 40, left: hole.left - W - GAP, arrow: 'right',
        fits: hole.left - W - GAP >= EDGE };
      case 'top': return { top: hole.top - H - GAP, left: hole.left + hole.width / 2 - W / 2, arrow: 'bottom',
        fits: hole.top - H - GAP >= EDGE };
      case 'bottom': return { top: hole.top + hole.height + GAP, left: hole.left + hole.width - W, arrow: 'top',
        fits: hole.top + hole.height + GAP + H <= vh - EDGE };
      default: return { top: hole.top + 64, left: hole.left + hole.width / 2 - W / 2, arrow: 'none', fits: true };
    }
  };
  const order = FLIP[place];
  const chosen = order.map((p) => ({ p, ...at(p) })).find((c) => c.fits);
  const c = chosen ?? { p: place, ...at(place) };
  return {
    style: { ...base, top: clampTop(c.top), left: clampLeft(c.left) } as React.CSSProperties,
    arrow: chosen ? c.arrow : 'none',
    place: chosen ? c.p : 'none',
  };
}

const TOUR_CSS = `
.ht-backdrop{position:fixed;inset:0;z-index:10000;display:flex;align-items:center;justify-content:center;font-family:var(--font-body);
  background:var(--color-overlay);backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);animation:ht-fade .25s ease}
.ht-card{position:relative;box-sizing:border-box;color:var(--color-text);font-family:var(--font-body);border-radius:16px;
  background:var(--color-surface-raised);border:1px solid var(--color-border);
  box-shadow:var(--elevation-elevated),0 0 0 1px color-mix(in srgb,var(--color-primary) 14%,transparent)}
.ht-btn{font-family:inherit;font-size:var(--fs-md);font-weight:600;border-radius:999px;padding:10px 20px;cursor:pointer;border:1px solid transparent;
  transition:transform .12s ease,box-shadow .2s ease,background .2s ease,filter .2s ease}
.ht-btn:active{transform:translateY(1px)}
.ht-btn:focus-visible{outline:2px solid var(--color-primary);outline-offset:2px}
.ht-primary{color:var(--color-on-primary);background:var(--color-primary-gradient);box-shadow:0 6px 18px color-mix(in srgb,var(--color-primary) 32%,transparent)}
.ht-primary:hover{filter:brightness(1.06);box-shadow:0 8px 24px color-mix(in srgb,var(--color-primary) 45%,transparent)}
.ht-ghost{color:var(--color-text-secondary);background:transparent;border-color:var(--color-border-strong)}
.ht-ghost:hover{background:var(--color-sidebar-hover);color:var(--color-text)}
.ht-sm{font-size:var(--fs-sm);padding:7px 15px}
/* Welcome banner — the splash page's palette (views/LandingPage.css), fixed across themes like the splash itself. */
.ht-splash{position:relative;box-sizing:border-box;width:min(1000px,calc(100vw - 48px));padding:48px 44px 40px;overflow:hidden;
  display:grid;grid-template-columns:minmax(0,1.15fr) minmax(260px,.85fr);gap:44px;align-items:center;
  border-radius:24px;color:#fff;font-family:Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;-webkit-font-smoothing:antialiased;
  border:1px solid rgba(151,178,226,.16);box-shadow:0 40px 120px rgba(0,0,0,.5),inset 0 1px rgba(255,255,255,.05);
  background:linear-gradient(rgba(255,255,255,.025) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.025) 1px,transparent 1px),
    radial-gradient(circle at 75% 42%,rgba(48,106,212,.22),transparent 40%),#07111f;
  background-size:56px 56px,56px 56px,auto,auto;animation:ht-rise .5s cubic-bezier(.2,.8,.2,1)}
.ht-splash>*:not(.ht-splash-orb):not(.ht-splash-close){position:relative;z-index:1}
.ht-splash-orb{position:absolute;border-radius:50%;pointer-events:none;opacity:.5;animation:ht-drift 8s ease-in-out infinite alternate}
.ht-splash-orb--one{width:340px;height:340px;right:-130px;top:-80px;background:radial-gradient(circle,rgba(67,199,215,.18),transparent 67%)}
.ht-splash-orb--two{width:280px;height:280px;left:30%;bottom:-150px;background:radial-gradient(circle,rgba(79,124,255,.22),transparent 70%);animation-delay:-3s}
.ht-splash-close{position:absolute;z-index:2;top:16px;right:16px;width:32px;height:32px;border-radius:9px;border:1px solid rgba(255,255,255,.1);
  background:transparent;color:#6f829e;cursor:pointer;font-size:12px}
.ht-splash-close:hover{color:#fff;background:rgba(255,255,255,.06)}
.ht-splash-logo{position:relative;width:84px;height:84px;margin-bottom:26px;animation:ht-float 6s 1s ease-in-out infinite}
.ht-splash-logo img{position:relative;width:84px;height:84px;border-radius:22px;object-fit:cover;display:block;
  box-shadow:0 16px 44px rgba(49,91,224,.45),0 0 0 1px rgba(126,160,255,.35)}
.ht-splash-logo-pulse{position:absolute;inset:-1px;border-radius:23px;border:1px solid rgba(94,136,255,.5);animation:ht-core-pulse 2.8s ease-out infinite}
.ht-splash-eyebrow{display:flex;align-items:center;gap:9px;color:#aebfe9;text-transform:uppercase;letter-spacing:.12em;font-size:11px;font-weight:700}
.ht-splash-eyebrow>span{width:6px;height:6px;border-radius:50%;background:#43c7d7;box-shadow:0 0 14px #43c7d7}
.ht-splash h2{margin:16px 0 16px;font-size:clamp(32px,3.4vw,46px);line-height:1.04;letter-spacing:-.035em;font-weight:700;font-family:inherit}
.ht-splash h2 em{color:#7fa3ff;font-style:normal}
.ht-splash-lead{margin:0 0 28px;max-width:470px;color:#aebbd0;font-size:15px;line-height:1.65}
.ht-splash-actions{display:flex;align-items:center;gap:22px}
.ht-splash-primary{min-height:50px;padding:0 22px;display:inline-flex;align-items:center;gap:9px;border:0;border-radius:10px;cursor:pointer;
  color:#fff;font:700 14px Inter,ui-sans-serif,sans-serif;background:linear-gradient(135deg,#527fff,#436ae2);
  box-shadow:0 14px 38px rgba(49,91,224,.3);transition:transform .25s ease,box-shadow .25s ease}
.ht-splash-primary:hover{transform:translateY(-2px);box-shadow:0 18px 44px rgba(49,91,224,.42)}
.ht-splash-primary:focus-visible,.ht-splash-quiet:focus-visible,.ht-splash-close:focus-visible{outline:3px solid #8ca9ff;outline-offset:3px}
.ht-splash-quiet{padding:10px 0;border:0;background:transparent;color:#aebbd0;cursor:pointer;font:600 14px Inter,ui-sans-serif,sans-serif}
.ht-splash-quiet:hover{color:#fff}
.ht-splash-note{margin:20px 0 0;color:#6f829e;font-size:12px}
.ht-splash-note b{display:inline-grid;place-items:center;width:18px;height:18px;border-radius:50%;border:1px solid rgba(151,178,226,.3);color:#aebbd0;font-size:10.5px}
.ht-route{padding:20px 18px 16px;border:1px solid rgba(151,178,226,.16);border-radius:18px;
  background:linear-gradient(145deg,rgba(18,34,57,.89),rgba(8,20,36,.75));box-shadow:0 30px 80px rgba(0,0,0,.35),inset 0 1px rgba(255,255,255,.05);
  animation:ht-rise .7s .1s cubic-bezier(.2,.8,.2,1) both}
.ht-route-label{color:#6f829e;font-size:10px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;margin-bottom:10px}
.ht-route-group+.ht-route-group{margin-top:12px}
.ht-route-sec{color:#aebfe9;font-size:11px;font-weight:700;margin:0 0 4px 2px}
.ht-route-stop{display:flex;align-items:center;gap:10px;padding:6px 2px;color:#b9c5d8;font-size:12.5px;font-weight:600}
.ht-route-dot{width:22px;height:22px;flex:0 0 auto;display:grid;place-items:center;border-radius:50%;
  color:#85a6ff;border:1px solid rgba(126,160,255,.4);background:rgba(78,119,235,.12);font-size:10px;font-weight:700}
@media (max-width:860px){.ht-splash{grid-template-columns:1fr}.ht-route{display:none}}
@keyframes ht-drift{to{transform:translate3d(-30px,24px,0) scale(1.08)}}
@keyframes ht-float{0%,100%{transform:translateY(0)}50%{transform:translateY(-6px)}}
@keyframes ht-core-pulse{0%{transform:scale(1);opacity:.6}70%,100%{transform:scale(1.18);opacity:0}}
.ht-layer{position:fixed;inset:0;z-index:10000}
.ht-layer.ht-interactive{pointer-events:none}
.ht-layer.ht-interactive .ht-block,.ht-layer.ht-interactive .ht-pop{pointer-events:auto}
.ht-block{position:fixed;background:var(--color-overlay);transition:all .25s ease}
.ht-hole-done{box-shadow:0 0 0 2px var(--color-success),0 0 24px 4px color-mix(in srgb,var(--color-success) 45%,transparent)}
.ht-hole-done::after{border-color:color-mix(in srgb,var(--color-success) 55%,transparent)}
.ht-yourturn{font-size:var(--fs-3xs);font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--color-on-primary);
  background:var(--color-primary);padding:2px 8px;border-radius:999px}
.ht-ring{z-index:9999}
.ht-dock{position:fixed;z-index:10000;right:20px;bottom:20px;width:min(320px,calc(100vw - 40px));max-height:calc(100vh - 40px);overflow-y:auto;padding:14px 16px 12px;animation:ht-pop .28s cubic-bezier(.2,.8,.2,1)}
.ht-dock-title{font-size:var(--fs-md);font-weight:700;color:var(--color-text)}
.ht-dock .ht-body{margin-bottom:10px}
.ht-spinner{width:14px;height:14px;border-radius:50%;border:2px solid var(--color-border);border-top-color:var(--color-primary);animation:ht-spin 1s linear infinite}
.ht-spinner-done{animation:none;border-color:var(--color-success)}
@keyframes ht-spin{to{transform:rotate(360deg)}}
.ht-mask{position:fixed;top:0;left:0;width:100vw;height:100vh;display:block;pointer-events:auto;animation:ht-fade .2s ease}
.ht-mask .ht-dim{fill:var(--color-overlay)}
.ht-cut{transition:all .32s cubic-bezier(.2,.8,.2,1)}
.ht-hole{position:fixed;border-radius:12px;pointer-events:none;transition:all .32s cubic-bezier(.2,.8,.2,1);
  box-shadow:0 0 0 2px var(--color-primary),0 0 24px 4px color-mix(in srgb,var(--color-primary) 45%,transparent)}
.ht-hole::after{content:"";position:absolute;inset:-6px;border-radius:16px;border:2px solid color-mix(in srgb,var(--color-primary) 55%,transparent);
  animation:ht-pulse 1.8s ease-out infinite}
.ht-pop{position:fixed;padding:16px 18px 14px;animation:ht-pop .28s cubic-bezier(.2,.8,.2,1);transition:top .32s ease,left .32s ease}
.ht-accent{position:absolute;left:0;right:0;top:0;height:3px;border-radius:16px 16px 0 0;background:var(--color-primary-gradient)}
.ht-pop-head{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.ht-section{display:inline-flex;align-items:center;gap:6px;font-size:var(--fs-3xs);font-weight:700;letter-spacing:.08em;text-transform:uppercase;
  color:var(--color-primary);padding:3px 9px;border-radius:999px;background:var(--color-primary-subtle);border:1px solid var(--color-section-border)}
.ht-count{margin-left:auto;font-size:var(--fs-xs);color:var(--color-text-tertiary);font-variant-numeric:tabular-nums}
.ht-chapter{font-size:var(--fs-xs);color:var(--color-text-tertiary);margin:-2px 0 6px}
.ht-step-title{margin:0 0 6px;font-size:var(--fs-xl);font-weight:700;color:var(--color-text);letter-spacing:-.005em}
.ht-body{font-size:var(--fs-sm);line-height:1.6;color:var(--color-text-secondary)}
.ht-body b{color:var(--color-text);font-weight:600}
.ht-body em{color:var(--color-text)}
.ht-progress{display:flex;gap:10px;margin:14px 0 12px}
.ht-progress-group{display:flex;gap:3px;flex:1}
.ht-seg{flex:1;height:4px;border-radius:2px;background:var(--color-border);transition:background .3s ease}
.ht-seg.done{background:color-mix(in srgb,var(--color-primary) 50%,var(--color-border))}
.ht-seg.now{background:var(--color-primary)}
.ht-pop-actions{display:flex;align-items:center;gap:8px}
.ht-link{background:none;border:none;color:var(--color-text-tertiary);font-size:var(--fs-xs);cursor:pointer;padding:0;font-family:inherit}
.ht-link:hover{color:var(--color-text)}
.ht-pop::before{content:"";position:absolute;width:14px;height:14px;transform:rotate(45deg);
  background:var(--color-surface-raised);border:1px solid var(--color-border)}
.ht-arrow-none::before{display:none}
.ht-arrow-left::before{left:-8px;top:38px;border-right:none;border-top:none}
.ht-arrow-right::before{right:-8px;top:38px;border-left:none;border-bottom:none}
.ht-arrow-top::before{top:-8px;right:40px;border-right:none;border-bottom:none}
.ht-arrow-bottom::before{bottom:-8px;left:calc(50% - 7px);border-left:none;border-top:none}
@keyframes ht-fade{from{opacity:0}to{opacity:1}}
@keyframes ht-rise{from{opacity:0;transform:translateY(14px) scale(.98)}to{opacity:1;transform:none}}
@keyframes ht-pop{from{opacity:0;transform:translateY(6px) scale(.98)}to{opacity:1;transform:none}}
@keyframes ht-pulse{0%{opacity:.9;transform:scale(1)}100%{opacity:0;transform:scale(1.06)}}
@media (prefers-reduced-motion:reduce){.ht-spinner,.ht-dock,.ht-block,.ht-hole,.ht-pop,.ht-splash,.ht-route,.ht-splash-logo,.ht-splash-logo-pulse,.ht-splash-orb,.ht-backdrop{animation:none;transition:none}.ht-hole::after{animation:none}}
`;
