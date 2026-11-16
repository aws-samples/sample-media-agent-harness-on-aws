/**
 * A direct timeline action the user ran (Frame / Clip / Crop), shown as a normal chat TURN: their request as a
 * blue bubble, the result as a reply. Live: running ("Clipping…") → done (summary + media + footer) or failed
 * (reason + Retry). Mock: .playwright-mcp/mock/search-and-actions.html §2.
 */
import React from 'react';
import { theme } from '../../styles/tokens';
import { AssetIcon } from './AssetIcons';
import { resolveLocations } from '../../projects/api';
import { openAssetAt } from '../../projects/openCitation';
import { getSessionProject } from '../../projects/sessionProjectStore';
import { ArtifactLink } from './ArtifactLink';
import CropIcon from '../icons/CropIcon';
import WarningIcon from '../icons/WarningIcon';
import {
  assistantBubbleStyle, assistantMessageRowStyle, dotKeyframes, dotStyle, errorIndicatorStyle, markdownBodyStyle,
  streamingDotsContainerStyle, summaryContainerStyle, summaryItemStyle, userBubbleStyle, userMessageRowStyle, userTextStyle,
  workingIndicatorStyle, workingLabelStyle,
} from './messageStyles';
import {
  actionReplyFooter, actionReplySummary, actionRequestMeta, actionRequestText, actionRunningLabel, actionStepLabel,
  batchFailures, canRetryDirectAction, galleryLabel, RESULT_GALLERY_EVENT, retryDirectAction, type ActionLineRecord, type MediaActionOp,
  type MediaActionOutput, type ResultGalleryDetail,
} from '../../projects/mediaActions';

const ICON: Record<ActionLineRecord['op'], React.ReactNode> = {
  frame: <AssetIcon kind="image" size={13} />, clip: <AssetIcon kind="cut" size={13} />, crop: <CropIcon size={13} />,
};

export interface ActionLineProps {
  actionId: string;
  action: ActionLineRecord;
  sessionId: string;
}

/**
 * Open an action's results in the side panel the way a project file opens: as an asset-player tab built in the
 * browser (OPEN_ASSET_EVENT via openAssetAt), never through `/render`, whose Render Agent is a model turn. The
 * outputs become project assets a moment after they are written, so each is resolved to its asset id (a few quick
 * retries); only one that is still not registered after ~6 s falls back to the composed `lab:render-urls` tab.
 */
export function openActionOutputs(outputs: MediaActionOutput[], sessionId: string, op?: MediaActionOp, sourceName?: string,
  deps: { resolve?: typeof resolveLocations; open?: typeof openAssetAt; sleep?: (ms: number) => Promise<void> } = {}): Promise<void> {
  // Several results: ONE gallery tab with all of them, opened at once (no per-result tab popping in as each
  // registers). Its URLs resolve like any manifest's.
  if (outputs.length > 1) {
    window.dispatchEvent(new CustomEvent<ResultGalleryDetail>(RESULT_GALLERY_EVENT, { detail: {
      sessionId, kind: outputs.every((o) => o.kind === 'image') ? 'image' : outputs.every((o) => o.kind === 'audio') ? 'audio' : 'video',
      label: galleryLabel(op ?? (outputs[0].kind === 'image' ? 'frame' : 'clip'), outputs.length, sourceName),
      items: outputs.map((o) => ({ url: o.url, name: o.name, kind: o.kind })) } }));
    return Promise.resolve();
  }
  const resolve = deps.resolve ?? resolveLocations;
  const open = deps.open ?? openAssetAt;
  const sleep = deps.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
  const pid = getSessionProject(sessionId)?.activeProjectId ?? null;
  const fallback = (o: MediaActionOutput) => window.dispatchEvent(new CustomEvent('lab:render-urls', {
    detail: { urls: [o.url], label: o.name, sessionId } }));
  if (!pid) { outputs.forEach(fallback); return Promise.resolve(); }
  return (async () => {
    const pending = new Map(outputs.map((o) => [o.s3uri || o.url, o]));
    for (let attempt = 0; attempt < 12 && pending.size; attempt++) {
      if (attempt) await sleep(500);
      const r = await resolve(pid, [...pending.keys()]);
      if (!r.ok) continue;
      for (const [loc, assetId] of Object.entries(r.value)) {
        const o = pending.get(loc);
        if (!o || !assetId) continue;
        pending.delete(loc);
        void open({ ref: { project_id: pid, asset_id: assetId, frame_s3_key: null }, kind: o.kind, displayName: o.name, startMs: 0,
          ...(o.s3uri ? { sourceLocation: o.s3uri } : {}) },
          sessionId).then((res) => { if (!res.ok) fallback(o); });
      }
    }
    pending.forEach(fallback);
  })();
}

/**
 * A direct timeline action as a CHAT TURN (mock `search-and-actions.html` §2): the user's request as their own
 * blue bubble, worded as they would type it ("Clip 00:00–01:34 of x.mp4" · "from the timeline"), and the result
 * as a reply in the agent's container — step label, one line, the media (one standard card, or ONE thumbnail
 * strip for several frames), and the chat's quiet footer ("Opened in a new tab · 0.6 s"). Both reuse the chat's
 * own message styles (chat/messageStyles.ts). It is ONE stored transcript entry (role 'action'), and the agent is
 * never called; it hears about the action on the user's next message (`withActionContext`).
 */
export function ActionLine({ actionId, action, sessionId: _sessionId }: ActionLineProps) {
  const failed = action.status === 'failed';
  const running = action.status === 'running';
  const outputs = action.outputs ?? [];
  return (
    <div data-testid="action-line" data-status={action.status} style={turnStyle}>
      <div style={userMessageRowStyle}>
        <div style={userBubbleStyle} data-testid="action-request">
          <span style={userTextStyle}>{actionRequestText(action)}</span>
          <span style={requestMetaStyle}>{actionRequestMeta(action)}</span>
        </div>
      </div>
      <div style={assistantMessageRowStyle}>
        <div style={assistantBubbleStyle} role="status" data-testid="action-reply">
          <div style={stepLabelStyle}><span style={iconStyle} aria-hidden="true">{ICON[action.op]}</span>{actionStepLabel(action)}</div>
          {running && (
            <span style={workingIndicatorStyle} data-testid="action-working">
              <span style={streamingDotsContainerStyle}>
                <span style={{ ...dotStyle, animationDelay: '0s' }}>●</span>
                <span style={{ ...dotStyle, animationDelay: '0.2s' }}>●</span>
                <span style={{ ...dotStyle, animationDelay: '0.4s' }}>●</span>
              </span>
              <span style={workingLabelStyle}>{actionRunningLabel(action)}</span>
              <style>{dotKeyframes}</style>
            </span>
          )}
          {action.status === 'done' && (
            <>
              <div style={markdownBodyStyle} data-testid="action-summary">{actionReplySummary(action)}</div>
              {/* Every result is the chat's standard media card — frames as big as any chat image, and each clip of a
                  multi-range Clip its own card (the strip of 92 px thumbs, and "first output only", read as broken). */}
              {outputs.length > 0 && (
                <div style={cardsStyle} data-testid="action-cards">
                  {outputs.slice(0, MAX_INLINE_CARDS).map((o) => <ArtifactLink key={o.key} href={o.url} />)}
                </div>
              )}
              {outputs.length > MAX_INLINE_CARDS && (
                <button type="button" style={showAllStyle} data-testid="action-show-all"
                  onClick={() => void openActionOutputs(outputs, _sessionId, action.op,
                    /^\d+ videos$/.test(action.sourceName) ? undefined : action.sourceName)}>
                  Show all {outputs.length}
                </button>
              )}
              {batchFailures(action).length > 0 && (
                <div style={{ ...errorIndicatorStyle, marginTop: 8 }} data-testid="action-batch-failures">
                  <WarningIcon /> {batchFailures(action).map((f) => `${f.name}: ${f.error}`).join(' · ')}
                  {canRetryDirectAction(actionId) && (
                    <button type="button" style={linkStyle} onClick={() => retryDirectAction(actionId)}>Retry</button>
                  )}
                </div>
              )}
              <div style={summaryContainerStyle}><span style={summaryItemStyle} data-testid="action-footer">{actionReplyFooter(action)}</span></div>
            </>
          )}
          {failed && (
            <>
              <div style={errorIndicatorStyle} data-testid="action-error">
                <WarningIcon /> {action.error ?? 'Something went wrong.'}
                {canRetryDirectAction(actionId) && (
                  <button type="button" style={linkStyle} onClick={() => retryDirectAction(actionId)}>Retry</button>
                )}
              </div>
              <div style={summaryContainerStyle}>
                <span style={summaryItemStyle}>{action.handedToAgent ? 'Nothing was saved · Sent to the agent instead' : 'Nothing was saved'}</span>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

export default ActionLine;

// The message list spaces its messages 12 px apart; the two halves of this turn sit the same distance apart.
const turnStyle: React.CSSProperties = { display: 'flex', flexDirection: 'column', gap: 12, minWidth: 0 };
const requestMetaStyle: React.CSSProperties = { display: 'block', marginTop: 2, fontSize: 'var(--fs-2xs)', opacity: 0.8 };
const stepLabelStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6, fontSize: 'var(--fs-2xs)', color: theme.colors.textSecondary,
};
const iconStyle: React.CSSProperties = { display: 'inline-flex', flexShrink: 0 };
const linkStyle: React.CSSProperties = {
  border: 'none', background: 'none', padding: '0 6px', font: 'inherit', fontWeight: 600, color: theme.colors.primary, cursor: 'pointer',
};
/** A batch can make dozens of results: the chat shows the first few as cards; "Show all" opens the gallery. */
const MAX_INLINE_CARDS = 4;
const showAllStyle: React.CSSProperties = {
  marginTop: 8, padding: '6px 10px', border: `1px solid ${theme.colors.border}`, borderRadius: theme.radius.button,
  background: theme.colors.surface, color: theme.colors.primary, font: 'inherit', fontSize: 'var(--fs-xs)', fontWeight: 600,
  cursor: 'pointer',
};
const cardsStyle: React.CSSProperties = { display: 'flex', flexDirection: 'column', gap: 8, marginTop: 8 };
