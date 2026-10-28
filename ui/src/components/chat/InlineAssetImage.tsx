/**
 * A project image shown INSIDE a chat answer: the agent writes `![caption](asset:<assetId>)` and the image sits
 * next to the explanation it belongs to (a diagram, a frame). Clicking it, or Expand, shows it large in a
 * preview over the chat; Edit in that preview opens it in the side panel (the Renderer), which is for editing.
 *
 * The id resolves in the chat's ACTIVE project through the Asset_URL_Route (the same authorised, short-lived
 * URLs Moment cards use), so the answer text never carries a URL and an old answer re-mints on view.
 */
import React, { createContext, useContext, useEffect, useState } from 'react';
import { theme } from '../../styles/tokens';
import { request as requestAssetUrls } from '../../projects/assetUrlBatcher';
import { resolveLocations } from '../../projects/api';
import { projectSessionOutputPrefix } from '../../shared/sessionLayout';
import { openAssetAt } from '../../projects/openCitation';
import { AssetPreviewModal } from './AssetPreviewModal';
import { ChatAssetCard } from './ChatAssetCard';
import { downloadFile } from './artifactUrl';

/** The scheme an answer uses for a project file: `asset:<assetId>`. */
export const ASSET_SCHEME = 'asset:';

/** The chat's session and its active project — what an `asset:` reference resolves against. */
export interface ChatAssetContextValue {
  sessionId: string;
  projectId: string | null;
}
export const ChatAssetContext = createContext<ChatAssetContextValue | null>(null);

/** react-markdown drops URLs with unknown schemes; keep ours, defer everything else to its default. */
export function keepAssetUrls(url: string, fallback: (u: string) => string): string {
  // `asset:` (an inline project image) and `project:new?…` (a proposed project's card) are ours; the default
  // transform would blank both as unsafe schemes.
  return url.startsWith(ASSET_SCHEME) || url.startsWith('project:new?') ? url : fallback(url);
}

type Phase = { state: 'loading' } | { state: 'ready'; url: string } | { state: 'unavailable' };

/** An asset id (`image:<uuid>`): a kind, a colon, an id with no path, scheme or extension. */
const ASSET_ID_RE = /^[a-z]+:[0-9a-z-]+$/i;
export function isAssetId(ref: string): boolean {
  return ASSET_ID_RE.test(ref);
}

/**
 * The locations an `asset:` reference that is NOT an id may name. A file the agent saved THIS turn has no asset id
 * yet (it is registered after the turn), so the agent writes its file name (`asset:frame_2s.png`), its key or its
 * URL instead (staging session a4a205a9). A name is looked up in this chat's output folder, under both session
 * roots; a key, `s3://` uri or URL as it is.
 */
export function referenceLocations(ref: string, sessionId: string, projectId: string): string[] {
  const r = ref.trim();
  if (!r) return [];
  if (/^(s3|https?):\/\//i.test(r) || r.startsWith('projects/') || r.startsWith('artifacts/')) return [r];
  if (/[\/\\]/.test(r) || r.startsWith('.')) return [];
  if (!sessionId) return [];
  // A bare file name, as written: the chat's output folder under both roots.
  return [`${projectSessionOutputPrefix(projectId, sessionId)}${r}`, `artifacts/${sessionId}/output/${r}`];
}

/** Registration of a just-made file lags the turn by seconds: ask again, a few times, before giving up. */
export const RESOLVE_RETRY_MS = [1500, 3000, 6000, 12000];

async function resolveReference(ref: string, sessionId: string, projectId: string,
                                alive: () => boolean): Promise<string | null> {
  if (isAssetId(ref)) return ref;
  const locations = referenceLocations(ref, sessionId, projectId);
  if (locations.length === 0) return null;
  for (let attempt = 0; ; attempt += 1) {
    const res = await resolveLocations(projectId, locations);
    if (!alive()) return null;
    const hit = res.ok ? locations.map((l) => res.value[l]).find((v): v is string => typeof v === 'string' && !!v) : undefined;
    if (hit) return hit;
    if (attempt >= RESOLVE_RETRY_MS.length) return null;
    await new Promise((r) => setTimeout(r, RESOLVE_RETRY_MS[attempt]));
    if (!alive()) return null;
  }
}

export function InlineAssetImage({ src, alt }: { src: string; alt?: string }) {
  const ctx = useContext(ChatAssetContext);
  const ref = src.slice(ASSET_SCHEME.length).trim();
  // The id the reference resolves to: itself when it is one, else (a file name, key or URL) looked up.
  const [assetId, setAssetId] = useState<string>(isAssetId(ref) ? ref : '');
  const caption = (alt || '').trim();
  const [phase, setPhase] = useState<Phase>({ state: 'loading' });
  const [openError, setOpenError] = useState<string | null>(null);
  const [preview, setPreview] = useState(false);

  useEffect(() => {
    let alive = true;
    const projectId = ctx?.projectId;
    if (!projectId || !ref) { setPhase({ state: 'unavailable' }); return; }
    setPhase({ state: 'loading' });
    resolveReference(ref, ctx?.sessionId || '', projectId, () => alive).then((id) => {
      if (!alive) return null;
      if (!id) { setPhase({ state: 'unavailable' }); return null; }
      setAssetId(id);
      return requestAssetUrls(projectId, [id]).then((map) => {
        if (!alive) return;
        const out = map.get(id);
        const url = out && out.ok ? (out.entry.rendition?.url ?? out.entry.source?.url) : undefined;
        setPhase(url ? { state: 'ready', url } : { state: 'unavailable' });
      });
    }).catch(() => { if (alive) setPhase({ state: 'unavailable' }); });
    return () => { alive = false; };
  }, [ctx?.projectId, ctx?.sessionId, ref]);

  // Edit: the side panel, where the asset can be worked on (what Expand used to do).
  const editInPanel = async () => {
    if (!ctx?.projectId) return;
    setOpenError(null);
    const res = await openAssetAt(
      { ref: { project_id: ctx.projectId, asset_id: assetId, frame_s3_key: null }, kind: 'image', displayName: caption || assetId, startMs: 0 },
      ctx.sessionId,
    );
    if (!res.ok) setOpenError(res.message);
  };

  if (phase.state === 'unavailable') {
    return <span style={missingStyle} data-testid="inline-asset-missing">{caption || 'Image'} — not available in this project</span>;
  }
  const name = caption || 'Image';
  const url = phase.state === 'ready' ? phase.url : null;
  const download = url ? () => { void downloadFile(url, name); } : undefined;
  return (
    <span data-testid="inline-asset" data-asset-id={assetId || undefined}>
      <ChatAssetCard kind="image" name={name} src={url}
        onPreview={() => setPreview(true)} onDownload={download} onOpenInPanel={() => { void editInPanel(); }}
        moveTarget={ctx?.projectId ? { projectId: ctx.projectId, assetId } : undefined} />
      {openError && <span style={missingStyle}>{openError}</span>}
      {preview && (
        <AssetPreviewModal kind="image" src={url} name={name} onClose={() => setPreview(false)}
          onOpenInPanel={() => { void editInPanel(); }} onDownload={download}
          moveTarget={ctx?.projectId ? { projectId: ctx.projectId, assetId } : undefined} />
      )}
    </span>
  );
}

const missingStyle: React.CSSProperties = { fontSize: 'var(--fs-xs)', color: theme.colors.textTertiary };
