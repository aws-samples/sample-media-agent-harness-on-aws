/**
 * The chat's message styles — the user bubble, the assistant reply container, the in-progress "Working…" row and the
 * duration footer — shared by ChatCanvas's own messages and by the turns a direct timeline action renders
 * (chat/ActionLine.tsx), so an action reads exactly like any other turn instead of imitating one.
 */
import type React from 'react';
import { theme } from '../../styles/tokens';

export const userMessageRowStyle: React.CSSProperties = {
  display: 'flex',
  justifyContent: 'flex-end',
};

export const userBubbleStyle: React.CSSProperties = {
  maxWidth: '70%',
  padding: '10px 14px',
  borderRadius: '12px 12px 2px 12px',
  background: theme.colors.primary,
  color: theme.colors.onPrimary,
};

export const userTextStyle: React.CSSProperties = {
  fontSize: 'var(--font-size-base)',
  lineHeight: 1.5,
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
};

export const assistantMessageRowStyle: React.CSSProperties = {
  display: 'flex',
  justifyContent: 'flex-start',
};

export const assistantBubbleStyle: React.CSSProperties = {
  maxWidth: '80%',
  minWidth: 0,
  overflow: 'hidden',
  padding: '12px 16px',
  borderRadius: '2px 12px 12px 12px',
  background: theme.colors.surface,
  border: `1px solid ${theme.colors.border}`,
  borderLeft: `3px solid ${theme.colors.sectionBorder}`,
  color: theme.colors.textPrimary,
};

export const markdownBodyStyle: React.CSSProperties = {
  fontSize: 'var(--font-size-base)',
  lineHeight: 1.6,
  wordBreak: 'break-word',
  minWidth: 0,
  maxWidth: '100%',
  overflow: 'hidden',
};

/** A reply the platform's content guardrail blocked: a quiet note under it, not an error. */
export const guardrailNoteStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 6, marginTop: 6, fontSize: 'var(--fs-xs)', color: theme.colors.textSecondary,
};
export const errorIndicatorStyle: React.CSSProperties = {
  fontSize: 'var(--fs-sm)',
  color: theme.colors.error,
  padding: '6px 0',
  marginTop: 4,
};

export const streamingDotsContainerStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 4,
  padding: '4px 0',
};

export const dotStyle: React.CSSProperties = {
  color: theme.colors.textTertiary,
  fontSize: 'var(--fs-md)',
  animation: 'chatDotWave 1.4s ease-in-out infinite',
  display: 'inline-block',
};

export const workingIndicatorStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
  marginTop: 6,
};

export const workingLabelStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)',
  color: theme.colors.textSecondary,
  animation: 'chatWorkingPulse 1.4s ease-in-out infinite',
};

export const summaryContainerStyle: React.CSSProperties = {
  display: 'flex',
  flexWrap: 'wrap',
  gap: 12,
  marginTop: 8,
  paddingTop: 8,
  borderTop: `1px solid ${theme.colors.border}`,
};

export const summaryItemStyle: React.CSSProperties = {
  fontSize: 'var(--fs-2xs)',
  color: theme.colors.textSecondary,
  display: 'flex',
  alignItems: 'center',
  gap: 4,
};

export const dotKeyframes = `
@keyframes chatDotWave {
  0%, 60%, 100% { opacity: 0.3; transform: translateY(0); }
  30% { opacity: 1; transform: translateY(-3px); }
}
@keyframes chatWorkingPulse {
  0%, 100% { opacity: 0.45; }
  50% { opacity: 1; }
}
`;
