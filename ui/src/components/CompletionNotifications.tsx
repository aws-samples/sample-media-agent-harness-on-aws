import React, { useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { createPortal } from 'react-dom';
import { theme } from '../styles/tokens';
import {
  notificationsStore,
  type JobNotification,
} from '../services/notifications';

/** When a notification arrived, so rows of the same job type (a re-run, a re-taken tour) tell apart. */
export function notifiedAgo(ts: number, now = Date.now()): string {
  const s = Math.max(0, Math.round((now - ts) / 1000));
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

/**
 * CompletionNotifications — the passive completion surface.
 *
 *  - **Banner (flash):** the newest unseen notification appears as a transient
 *    toast, auto-dismisses after a few seconds, and has an X to close early.
 *    Clicking it opens the originating session and renders results.
 *  - **Bell:** a persistent top-bar button with an unread count. Its dropdown
 *    lists recent completions (success + failure), newest first, each opening
 *    its session on click. The bell is the durable record so a missed banner
 *    is never lost.
 *
 * Both read the shared `notificationsStore`. Rendering is delegated to the
 * caller via `onOpen` (WorkspaceView selects the session + triggers the render).
 */

const BANNER_TTL_MS = 12000;

interface Props {
  onOpen: (n: JobNotification) => void;
}

function useNotifications() {
  return useSyncExternalStore(notificationsStore.subscribe, notificationsStore.getSnapshot);
}

export default function CompletionNotifications({ onOpen }: Props) {
  const { items, unseen } = useNotifications();
  const [open, setOpen] = useState(false);
  const bellRef = useRef<HTMLDivElement | null>(null);
  const [dismissed, setDismissed] = useState<Set<string>>(() => new Set());

  // The banner shows the most recent unseen, not-yet-bannered, not-just-dismissed
  // notification. `bannered` is PERSISTED (survives reload → no re-fire); `dismissed`
  // is the in-session immediate hide.
  const banner = useMemo(
    () => items.find((n) => !n.seen && !n.bannered && !dismissed.has(n.jobId)) ?? null,
    [items, dismissed],
  );

  // Auto-dismiss the banner (the bell keeps it). Reset the timer per banner id.
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (!banner) return;
    if (timerRef.current) clearTimeout(timerRef.current);
    const id = banner.jobId;
    timerRef.current = setTimeout(() => {
      notificationsStore.markBannered(id);
      setDismissed((prev) => new Set(prev).add(id));
    }, BANNER_TTL_MS);
    return () => { if (timerRef.current) clearTimeout(timerRef.current); };
  }, [banner]);

  // The banner spans the chat column just BELOW the top bar (over the session-id line, briefly), so it reads as
  // part of the conversation and never covers the bell. It used to be a full-width strip pinned over the top of
  // the window, which hid the top bar — the bell included — for its whole 12s, so the one control that keeps the
  // record could not be clicked while the banner advertised it (and the guided tour, pointing at the bell,
  // stalled). With no chat column on screen (Projects, Skills), it drops from beneath the bell instead.
  const [anchor, setAnchor] = useState<Anchor | null>(null);
  useEffect(() => {
    if (!banner) return;
    const place = () => setAnchor(bannerAnchor(bellRef.current, window.innerWidth));
    place();
    window.addEventListener('resize', place);
    return () => window.removeEventListener('resize', place);
  }, [banner]);

  const dismissBanner = (id: string) => {
    notificationsStore.markBannered(id);
    setDismissed((prev) => new Set(prev).add(id));
  };

  const handleOpen = (n: JobNotification) => {
    notificationsStore.markSeen(n.jobId);
    setOpen(false);
    onOpen(n);
  };

  return (
    <>
      <style>{`
        @keyframes lab-notif-slide-in {
          from { transform: translateY(-8px); opacity: 0; }
          to   { transform: translateY(0); opacity: 1; }
        }
        @keyframes lab-notif-countdown {
          from { transform: scaleX(1); }
          to   { transform: scaleX(0); }
        }
        .lab-notif-banner { animation: lab-notif-slide-in 280ms cubic-bezier(0.16,1,0.3,1); }
        .lab-notif-banner-body:hover .lab-notif-banner-hint { text-decoration: underline; }
        .lab-notif-countdown {
          transform-origin: left center;
          animation: lab-notif-countdown ${BANNER_TTL_MS}ms linear forwards;
        }
        .lab-notif-item { transition: background ${theme.transitions.hover}; }
        .lab-notif-item:hover { background: ${theme.colors.surfaceSubtle}; }
        .lab-notif-linkbtn:hover { text-decoration: underline; }
        .lab-notif-bell:hover { border-color: ${theme.colors.primary}; color: ${theme.colors.textPrimary}; }
        .lab-notif-close:hover { background: ${theme.colors.surfaceSubtle}; color: ${theme.colors.textPrimary}; }
      `}</style>

      {/* Bell */}
      <div style={{ position: 'relative' }} ref={bellRef}>
        <button
          type="button"
          className="lab-notif-bell"
          onClick={() => {
            // Read state is settled when the panel CLOSES, not when it opens.
            //
            // Opening must preserve the "these are new" signal: the badge stays up and each
            // unread row keeps full opacity plus its unread pip while the user reads the list.
            // Marking on open (the first cut of this fix) dimmed every row the instant the panel
            // appeared, so the user could never see WHICH completions were new.
            //
            // Clicking an individual row marks just that row (see handleOpen) — the rest stay
            // unread. Dismissing the panel is the "I've read these" signal that clears the badge,
            // which is what stops already-handled items from re-alerting on every reload/login
            // (the original bug: `seen` was set ONLY by an item click or "Mark all read", so an
            // auto-expired banner left an item unread forever).
            if (open) notificationsStore.markAllSeen();
            // Opening the bell shows the same item the banner does, so the banner gives way to the list.
            if (!open && banner) dismissBanner(banner.jobId);
            setOpen(!open);
          }}
          style={bellBtnStyle}
          aria-label={unseen > 0 ? `Notifications (${unseen} unread)` : 'Notifications'}
          title="Notifications"
        >
          <BellIcon />
          {unseen > 0 && <span style={bellBadgeStyle} aria-hidden="true">{unseen > 9 ? '9+' : unseen}</span>}
        </button>

        {open && (
          <div style={dropdownStyle} role="menu">
            <div style={dropdownHeaderStyle}>
              <span style={{ fontWeight: theme.typography.weights.semibold }}>Notifications</span>
              <div style={{ display: 'flex', gap: 8 }}>
                {unseen > 0 && (
                  <button type="button" className="lab-notif-linkbtn" style={linkBtnStyle} onClick={() => notificationsStore.markAllSeen()}>
                    Mark all read
                  </button>
                )}
                {items.length > 0 && (
                  <button type="button" className="lab-notif-linkbtn" style={linkBtnStyle} onClick={() => notificationsStore.clearAll()}>
                    Clear
                  </button>
                )}
              </div>
            </div>
            {items.length === 0 ? (
              <div style={emptyStyle}>No notifications yet</div>
            ) : (
              <div style={{ maxHeight: 340, overflowY: 'auto' }}>
                {items.map((n) => (
                  <button
                    key={n.jobId}
                    type="button"
                    className="lab-notif-item"
                    onClick={() => handleOpen(n)}
                    style={{ ...itemStyle, opacity: n.seen ? 0.6 : 1 }}
                    role="menuitem"
                    data-notification-id={n.jobId}
                  >
                    <span style={{ ...dotStyle, background: n.kind === 'success' ? theme.colors.success : theme.colors.error }} aria-hidden="true" />
                    <span style={{ flex: 1, minWidth: 0 }}>
                      <span style={itemTitleStyle}>{n.message}</span>
                      {n.kind === 'failure' && n.error && (
                        <span style={itemSubStyle}>{notifiedAgo(n.ts)} · {n.error.slice(0, 80)}</span>
                      )}
                      {n.kind === 'success' && (
                        <span style={itemSubStyle}>{n.renderable ? 'View results' : 'Open session'} · {notifiedAgo(n.ts)}</span>
                      )}
                    </span>
                    {!n.seen && <span style={unreadPipStyle} aria-hidden="true" />}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      {/* Transient banner — a card dropped from beneath the bell, never over it
          (portaled to <body> so no ancestor stacking/transform/display context
          can clip or hide it). Slides down on arrival, shows a status-colored
          accent + countdown bar, and auto-dismisses after BANNER_TTL_MS (the
          bell keeps the durable record). */}
      {banner && typeof document !== 'undefined' && createPortal(
        (() => {
          const accent = banner.kind === 'success' ? theme.colors.success : theme.colors.error;
          const hint = banner.kind === 'success'
            ? (banner.renderable ? 'View results' : 'Open session')
            : 'View details';
          return (
            <div
              className="lab-notif-banner"
              style={{
                ...bannerStripStyle,
                ...(anchor ?? { top: 12, right: 12, width: 'min(420px, calc(100vw - 24px))' }),
                borderLeft: `3px solid ${accent}`,
              }}
              role="status"
              aria-live="polite"
            >
              <button
                type="button"
                className="lab-notif-banner-body"
                style={bannerBodyStyle}
                onClick={() => handleOpen(banner)}
              >
                <span style={{ ...bannerDotStyle, background: accent }} aria-hidden="true" />
                <span style={bannerMessageStyle}>{banner.message}</span>
                <span className="lab-notif-banner-hint" style={{ ...bannerHintStyle, color: accent }}>
                  {hint} →
                </span>
              </button>
              <button
                type="button"
                className="lab-notif-close"
                style={bannerCloseStyle}
                onClick={() => dismissBanner(banner.jobId)}
                aria-label="Dismiss notification"
                title="Dismiss"
              >
                ×
              </button>
              <div
                className="lab-notif-countdown"
                style={{ ...bannerCountdownStyle, background: accent }}
                aria-hidden="true"
              />
            </div>
          );
        })(),
        document.body,
      )}
    </>
  );
}

function BellIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <path d="M8 2a3.5 3.5 0 0 0-3.5 3.5c0 3-1.5 4-1.5 4h10s-1.5-1-1.5-4A3.5 3.5 0 0 0 8 2z" />
      <path d="M6.5 12.5a1.5 1.5 0 0 0 3 0" />
    </svg>
  );
}

const bellBtnStyle: React.CSSProperties = {
  position: 'relative', width: 30, height: 30, borderRadius: theme.radius.button,
  border: `1px solid ${theme.colors.border}`, background: theme.colors.surface,
  color: theme.colors.textSecondary, cursor: 'pointer', display: 'flex',
  alignItems: 'center', justifyContent: 'center',
};
const bellBadgeStyle: React.CSSProperties = {
  position: 'absolute', top: -5, right: -5, minWidth: 15, height: 15, padding: '0 3px',
  borderRadius: theme.radius.panel, background: theme.colors.error, color: theme.colors.onPrimary, fontSize: 'var(--fs-3xs)',
  fontWeight: theme.typography.weights.semibold, display: 'flex', alignItems: 'center', justifyContent: 'center',
};
const dropdownStyle: React.CSSProperties = {
  position: 'absolute', top: 38, right: 0, width: 320, background: theme.colors.surface,
  border: `1px solid ${theme.colors.border}`, borderRadius: theme.radius.panel,
  boxShadow: theme.elevation.elevated, zIndex: theme.zIndex.modal, overflow: 'hidden',
};
const dropdownHeaderStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', justifyContent: 'space-between',
  padding: '10px 12px', borderBottom: `1px solid ${theme.colors.border}`, fontSize: 'var(--fs-xs)',
  color: theme.colors.textPrimary,
};
const linkBtnStyle: React.CSSProperties = {
  border: 'none', background: 'none', color: theme.colors.primary, cursor: 'pointer', fontSize: 'var(--fs-2xs)', padding: 0,
};
const emptyStyle: React.CSSProperties = { padding: '20px 12px', textAlign: 'center', color: theme.colors.textSecondary, fontSize: 'var(--fs-xs)' };
const itemStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'flex-start', gap: 8, width: '100%', textAlign: 'left',
  padding: '10px 12px', border: 'none', borderBottom: `1px solid ${theme.colors.border}`,
  background: 'none', cursor: 'pointer',
};
const itemTitleStyle: React.CSSProperties = { display: 'block', fontSize: 'var(--fs-xs)', color: theme.colors.textPrimary, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' };
const itemSubStyle: React.CSSProperties = { display: 'block', fontSize: 'var(--fs-3xs)', color: theme.colors.textSecondary, marginTop: 2 };
const dotStyle: React.CSSProperties = { width: 8, height: 8, borderRadius: '50%', flexShrink: 0, marginTop: 4 };
const unreadPipStyle: React.CSSProperties = { width: 6, height: 6, borderRadius: '50%', background: theme.colors.primary, flexShrink: 0, marginTop: 4 };

/** Where the banner sits: across the chat column under the top bar, else beneath the bell. Exported for tests. */
export type Anchor = { top: number; left?: number; right?: number; width: number | string };
export function bannerAnchor(bell: HTMLElement | null, viewportWidth: number): Anchor | null {
  const r = bell?.getBoundingClientRect();
  if (!r || r.width <= 0) return null;
  const col = (bell?.closest('[data-tour="center"]') as HTMLElement | null)?.getBoundingClientRect();
  if (col && col.width > 0) return { top: r.bottom + 6, left: col.left + 8, width: col.width - 16 };
  return { top: r.bottom + 8, right: Math.max(12, viewportWidth - r.right), width: 'min(420px, calc(100vw - 24px))' };
}

const bannerStripStyle: React.CSSProperties = {
  position: 'fixed', zIndex: theme.zIndex.toast, boxSizing: 'border-box',
  display: 'flex', alignItems: 'center',
  padding: '12px 44px 12px 14px', background: theme.colors.surface,
  border: `1px solid ${theme.colors.border}`, borderRadius: theme.radius.panel,
  boxShadow: theme.elevation.elevated, overflow: 'hidden',
};
const bannerBodyStyle: React.CSSProperties = {
  display: 'inline-flex', alignItems: 'center', gap: 10, minWidth: 0, flex: 1, textAlign: 'left',
  border: 'none', background: 'none', cursor: 'pointer',
  color: theme.colors.textPrimary, fontSize: 'var(--fs-md)', padding: 0,
};
const bannerDotStyle: React.CSSProperties = {
  width: 9, height: 9, borderRadius: '50%', flexShrink: 0,
};
const bannerMessageStyle: React.CSSProperties = {
  fontWeight: theme.typography.weights.semibold, color: theme.colors.textPrimary,
  overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
};
const bannerHintStyle: React.CSSProperties = {
  fontSize: 'var(--fs-sm)', fontWeight: theme.typography.weights.medium, flexShrink: 0,
};
const bannerCloseStyle: React.CSSProperties = {
  position: 'absolute', right: 14, top: '50%', transform: 'translateY(-50%)',
  width: 26, height: 26, borderRadius: theme.radius.button, border: 'none', background: 'none',
  color: theme.colors.textSecondary, cursor: 'pointer', fontSize: 'var(--fs-xl)', lineHeight: 1,
  display: 'flex', alignItems: 'center', justifyContent: 'center',
};
const bannerCountdownStyle: React.CSSProperties = {
  position: 'absolute', left: 0, bottom: 0, height: 3, width: '100%', opacity: 0.5,
};
