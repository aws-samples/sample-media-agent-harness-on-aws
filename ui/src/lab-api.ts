// lab-api.ts — Domain types and error class for the MediaWorks Lab API client.
// This module is separate from api.ts to avoid coupling with the legacy agent API.

import { authFetch } from './auth/authFetch';
import { createAsyncCache } from './utils/asyncCache';
import { isControlPlaneAction } from './capability-actions';
import { CONTROL_PLANE_LAB_PATH, controlPlaneServesLabActions } from './capability-api';
import { normalizeUserTurnContent } from './promptNormalize';
import type { CitationBlock } from './api';
import { loadRuntimeConfig } from './config/runtimeConfig';

// --- Draft types ---

export type DraftCategory = 'skill' | 'specialty-agent' | 'tool-collection' | 'workflow' | 'hook';

export interface DraftMetadata {
  assetId: string;
  name: string;
  /**
   * What the author says this is FOR — shown on the inventory row beneath the name.
   *
   * OPTIONAL and `undefined`-when-unknown: the Capability API omits the field when
   * the source cannot answer (Gateway tools, built-ins, Registry-only rows), and the
   * legacy path collapses a blank author field to absent as well. Branch on
   * `undefined`; do not `?? ''` it, or "not known" renders as "has none".
   */
  description?: string;
  category: DraftCategory;
  status: 'active' | 'archived';
  createdAt: number;  // epoch ms
  updatedAt: number;  // epoch ms
  s3Prefix: string;
}

export interface SkillDraft extends DraftMetadata {
  category: 'skill';
  skillMd: string;
  /**
   * Package resources beside SKILL.md.
   *
   * OPTIONAL because two sources answer this list differently: the legacy `/lab`
   * path returns it, and the Capability API's normalized summary has no
   * per-category file list at all. `undefined` therefore means "not known from
   * this source", which is NOT the same as `[]` ("known to have none") — and the
   * difference is user-visible, because a skill with 3 resources rendered
   * through an empty array claims "1 file · SKILL.md". Keep them distinct;
   * `formatSkillPackageCount` renders the unknown case without a count.
   */
  supportingFiles?: string[];
}

export interface IntegrationConfig {
  enabled: boolean;
  issuer?: string;              // HTTPS OIDC issuer URL
  audience?: string;            // expected aud claim
  allowedClientIds?: string[];  // optional client-id allowlist
  // Tool collections (subset of the agent's selected toolCollections) the Owner
  // opts in to expose on the external invoke path. The runtime narrows external
  // tools to this set (fail-closed: empty ⇒ no external tools). Confused-deputy
  // gate, separate from the agent's own tool_collections (Req 11.1/6.6).
  externalToolCollections?: string[];
}

export interface SpecialtyAgentDraft extends DraftMetadata {
  category: 'specialty-agent';
  persona: string;
  description?: string;
  compatibleSkills: string[];  // assetId[]
  model?: string;              // Bedrock inference profile id (optional)
  toolCollections?: string[];  // Tool_Collection assetId[]
  integrationConfig?: IntegrationConfig | null;
  /**
   * True for the platform's built-in Task Agent, which the Lab service appends
   * to list results as a VIRTUAL row (`assetId: "core-task-agent"`,
   * `status: "active"`) — see `_CORE_TASK_AGENT_REF` in
   * `agent/src/lab/specialty_crud.py`. It has no AssetIndex row, so any
   * mutation addressed to it fails; consumers must not offer destructive
   * actions on it.
   */
  isCoreAgent?: boolean;
}

export interface ToolCollectionDraft extends DraftMetadata {
  category: 'tool-collection';
  /**
   * The tools in the collection — OPTIONAL and `undefined`-when-unknown, like
   * `SkillDraft.supportingFiles` and for the same reason.
   *
   * `readToolCollectionDraft` populates it; the Capability API's normalized
   * inventory summary does NOT carry it at all. Filling the gap with `[]` made
   * every listed collection claim zero tools — `media-tools`, with nine gateway
   * refs in DynamoDB, read as empty. Branch on `undefined`; do not `?? []` it,
   * or "not known" renders as "has none".
   */
  toolRefs?: ToolRef[];
}

export interface ToolRef {
  name: string;
  version: string;
}

export interface WorkflowDraft extends DraftMetadata {
  category: 'workflow';
  steps: WorkflowStep[];
}

export interface WorkflowStep {
  stepId: string;
  label?: string;  // Optional human-readable step name
  executionMode?: 'sequential' | 'parallel-map' | 'conditional';  // How this step executes at promotion
  mapInput?: string;  // JSONPath for parallel-map iteration (e.g., "$.categories")
  condition?: string;  // Condition expression for conditional execution
  outputSchema?: string;  // Expected output format description or JSON schema
  agent: { kind: 'core' | 'specialty'; assetId?: string; ref?: string };
  skill: { assetId: string | null };
  toolCollection: { assetId: string | null };
  prompt: string;
  inputs: string[];  // stepId references
}

// --- Gateway types ---

export interface GatewayToolDescriptor {
  name: string;
  description: string;
  version: string;
  inputSchema?: Record<string, unknown>;
  source?: string; // Section label from catalog (e.g., "Built-in Tools", "Gateway: Main Gateway")
}

// --- Chat types ---

export interface ChatMessage {
  id: string;
  /** `action`: a direct timeline action line (Frame / Clip / Crop) — saved with the chat, never a turn. */
  role: 'user' | 'assistant' | 'action';
  /** The action line's record (`projects/mediaActions.ts` ActionLineRecord), on `role: 'action'` only. */
  action?: unknown;
  content: string;
  steps?: StepEvent[];
  summary?: ExecutionSummary | null;
  error?: string;
  timestamp: number;
  /**
   * An uploaded input file attached to a user message, surfaced as a durable
   * CloudFront URL once the upload resolves. Rendered inline (image/video/audio
   * preview + download) in the user bubble via ArtifactLink.
   */
  attachment?: { url: string; name: string };
  /** Every file attached to a user message, in order (`attachment` is the first). */
  attachments?: { url: string; name: string }[];
}

export interface StepEvent {
  type: 'step';
  status: 'running' | 'done' | 'pending' | 'completed' | 'failed' | 'skipped' | 'replanning' | 'plan_ready';
  label: string;
  duration_ms?: number;
  agent?: string;
  phase?: string;
  node_id?: string;
}

export interface ExecutionSummary {
  type: 'execution_summary';
  total_duration_ms: number;
  cycles: number;
  tools: Record<string, { calls: number; total_ms: number; avg_ms: number }>;
  usage: { inputTokens?: number; outputTokens?: number; totalTokens?: number };
  model?: string;
  /** The model the agent actually ran this turn (authoritative for the badge). */
  model_id?: string;
  /**
   * Turn-end citation verification block (media-harness-search-qa-rendering Req 2.6, 2.12). Shared type
   * from api.ts. Present only on a Projects turn that produced citations; absent otherwise.
   */
  citations?: CitationBlock;
  /**
   * `switch_project` / `create_project` asked the chat to open a NEW chat in this project when the turn ends, and
   * send it `prompt` (agent `project_search.turn_actions`). Acted on from the live stream only.
   */
  open_project?: { projectId: string; projectName?: string; prompt?: string };
  /** The platform's Bedrock guardrail this turn ran with, and whether it blocked something. */
  guardrail?: { name?: string; version?: string; intervened?: boolean };
}

export type StreamEvent =
  | { type: 'step'; step: StepEvent }
  | { type: 'text'; text: string }
  | { type: 'execution_summary'; summary: ExecutionSummary }
  | { type: 'draft_mutation'; category: DraftCategory; assetId: string; mutation: 'created' | 'updated' | 'deleted' };

// --- Readiness types ---

export interface ReadinessReport {
  workflowAssetId: string;
  workflowName: string;
  overallStatus: 'ready' | 'not_ready';
  assets: ReadinessAssetEntry[];
}

export interface ReadinessAssetEntry {
  assetId: string;
  name: string;
  category: DraftCategory;
  status: 'ready' | 'not_ready';
  failures?: string[];
}

// --- Error type ---

export class LabApiError extends Error {
  statusCode: number;
  constructor(statusCode: number, message: string) {
    super(message);
    this.name = 'LabApiError';
    this.statusCode = statusCode;
  }
}

// --- Stream event parser ---

export function parseStreamEvent(payload: string): StreamEvent | null {
  try {
    const parsed = JSON.parse(payload);
    if (!parsed || typeof parsed !== 'object' || !parsed.type) return null;

    switch (parsed.type) {
      case 'step':
        return {
          type: 'step',
          step: {
            type: 'step',
            status: parsed.status ?? parsed.step?.status ?? 'running',
            label: parsed.label ?? parsed.step?.label ?? '',
            duration_ms: parsed.duration_ms ?? parsed.step?.duration_ms,
            agent: parsed.agent ?? parsed.step?.agent,
            phase: parsed.phase ?? parsed.step?.phase,
            node_id: parsed.node_id ?? parsed.step?.node_id,
          },
        };
      case 'text':
        return { type: 'text', text: parsed.text ?? '' };
      case 'execution_summary':
        return {
          type: 'execution_summary',
          summary: parsed.summary ?? {
            type: 'execution_summary',
            total_duration_ms: parsed.total_duration_ms ?? 0,
            cycles: parsed.cycles ?? 0,
            tools: parsed.tools ?? {},
            usage: parsed.usage ?? {},
            model: parsed.model,
          },
        };
      case 'draft_mutation':
        return {
          type: 'draft_mutation',
          category: parsed.category,
          assetId: parsed.assetId ?? parsed.asset_id ?? '',
          mutation: parsed.mutation ?? 'updated',
        };
      default:
        return null;
    }
  } catch {
    return null;
  }
}

// --- Non-streaming API helper ---

/**
 * Parse a response body that may be either a raw JSON object or an SSE
 * stream of `data: ...` lines.
 *
 * Why both shapes? When `LAB_SERVICE_RUNTIME_ARN` is set, the proxy
 * forwards `/lab` to the legacy Lab_Service runtime which returns plain
 * JSON. When it's unset (the merged default), `/lab` falls back to the
 * Main Agent runtime, whose `@app.entrypoint` is an async generator and
 * always emits an SSE-shaped response — even for the structured Lab CRUD
 * actions that have a single deterministic result.
 *
 * Returns the first object it can decode; later events are ignored
 * because every Lab CRUD action emits exactly one terminal payload. If
 * none of the SSE lines decode to an object we fall back to plain JSON
 * parsing so the legacy path still works.
 */
function parseLabResponseBody(text: string): unknown {
  // Fast path: a raw JSON object (legacy Lab_Service runtime). Try this
  // first because it's the cheapest discriminator — no SSE prefix means
  // SSE parsing would never produce a result anyway.
  if (!text.includes('data: ')) {
    return JSON.parse(text);
  }

  // SSE path. Iterate `data: ...` lines, JSON-parse each payload, and
  // tolerate double-encoded strings (the Main Agent serializes
  // structured action results via `json.dumps(json.dumps(...))` in some
  // paths — same handling as `skillsAction` in `api.ts`).
  for (const line of text.split('\n')) {
    if (!line.startsWith('data: ')) continue;
    try {
      let value: unknown = JSON.parse(line.slice(6));
      if (typeof value === 'string') {
        try { value = JSON.parse(value); } catch { /* keep the raw string */ }
      }
      if (value !== null && typeof value === 'object') {
        return value;
      }
      // A bare string event (e.g. a streaming text chunk) — skip and
      // keep scanning for the structured terminal event.
    } catch { /* not JSON, try next line */ }
  }

  // Last resort: maybe the body lied about containing `data: `. Fall back
  // to plain JSON parsing so the caller's catch path produces the right
  // error message rather than a silent undefined.
  return JSON.parse(text);
}

/**
 * Which host serves this action.
 *
 * Control-plane actions go to the always-on Capability API, which answers from
 * DynamoDB/S3 in ~200ms; everything else goes to `/lab` and the agent runtime,
 * which pays a cold start but is the only host with a model and a live gateway
 * session. Both mount the *same* `LabService` dispatcher, so the response shape
 * does not depend on the answer here — only the latency does.
 *
 * Both conditions are required. The action must be one the control plane serves
 * (`capability-actions.ts`), and this environment must actually be running it —
 * staging deploys the service with zero tasks, where sending writes there would
 * replace a slow path with a broken one.
 */
async function labActionUrl(action: string): Promise<string> {
  if (!isControlPlaneAction(action)) return '/lab';
  return (await controlPlaneServesLabActions()) ? CONTROL_PLANE_LAB_PATH : '/lab';
}

export async function labAction<T>(action: string, params: Record<string, unknown> = {}, timeoutMs = 90_000): Promise<T> {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const res = await authFetch(await labActionUrl(action), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action, ...params }),
      signal: controller.signal,
    });

    clearTimeout(timeoutId);

    if (!res.ok) {
      const errBody = await res.text().catch(() => '');
      let parsedErr: any = {};
      try { parsedErr = parseLabResponseBody(errBody); } catch { parsedErr = { error: res.statusText }; }
      // `detail` is FastAPI's error shape, which the control-plane host uses.
      // Without it a misrouted action would surface as a bare "Bad Request",
      // hiding the one message that says which host to use instead.
      const detail = parsedErr?.detail;
      const fromDetail = typeof detail === 'string'
        ? detail
        : (detail?.message ?? detail?.code);
      throw new LabApiError(
        res.status,
        parsedErr?.error ?? parsedErr?.message ?? fromDetail ?? res.statusText,
      );
    }

    const text = await res.text();
    const body = parseLabResponseBody(text);

    // AgentCore runtime returns 200 even on handler errors — check for error in body
    if (body && typeof body === 'object' && 'error' in body && 'message' in body && !Array.isArray(body)) {
      const errBody = body as { error: string; message?: string };
      throw new LabApiError(400, errBody.message ?? errBody.error);
    }

    return body as T;
  } catch (err) {
    clearTimeout(timeoutId);
    if (err instanceof LabApiError) throw err;
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new LabApiError(0, `Request timed out after ${Math.round(timeoutMs / 1000)} seconds`);
    }
    throw new LabApiError(0, err instanceof Error ? err.message : 'Network error');
  }
}

// --- Streaming API helper ---

export async function labStream(
  action: string,
  params: Record<string, unknown>,
  onEvent: (event: StreamEvent) => void,
): Promise<void> {
  const res = await authFetch('/lab', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, ...params }),
  });

  if (!res.ok) {
    const body = await res.json().catch(() => ({ error: res.statusText }));
    throw new LabApiError(res.status, body.error ?? res.statusText);
  }

  const reader = res.body?.getReader();
  if (!reader) throw new LabApiError(0, 'No response body');

  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() || '';
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      const payload = trimmed.startsWith('data: ') ? trimmed.slice(6) : trimmed;
      const event = parseStreamEvent(payload);
      if (event) onEvent(event);
    }
  }
  // Flush remaining buffer
  if (buffer.trim()) {
    const payload = buffer.trim().startsWith('data: ') ? buffer.trim().slice(6) : buffer.trim();
    const event = parseStreamEvent(payload);
    if (event) onEvent(event);
  }
}

// =============================================================================
// Exported Operation Functions
// =============================================================================

// --- Skills ---

export async function listSkillDrafts(): Promise<SkillDraft[]> {
  return labAction<SkillDraft[]>('ListSkillDrafts', {});
}

export async function createSkillDraft(name: string, skillMd: string): Promise<{ assetId: string; s3Prefix: string }> {
  return labAction<{ assetId: string; s3Prefix: string }>('CreateSkillDraft', { name, skillMd });
}

export interface SkillPackageFileUpload {
  path: string;
  contentBase64: string;
}

export async function importSkillPackage(
  name: string,
  skillMd: string,
  files: SkillPackageFileUpload[],
): Promise<{ assetId: string; s3Prefix: string; supportingFiles: string[] }> {
  return labAction<{ assetId: string; s3Prefix: string; supportingFiles: string[] }>(
    'ImportSkillPackage',
    { name, skillMd, files },
  );
}

export async function readSkillDraft(assetId: string): Promise<SkillDraft> {
  return labAction<SkillDraft>('ReadSkillDraft', { assetId });
}

/**
 * Read an immutable published Skill version.
 *
 * A separate action from `readSkillDraft`, not a flag on it: publishing
 * consumes the working copy, and the draft read is contractually scoped to the
 * owner's drafts partition, so it returns nothing for a published lineage.
 * Omit `version` to get the current one.
 */
export async function readPublishedSkill(assetId: string, version?: number): Promise<SkillDraft> {
  return labAction<SkillDraft>('ReadPublishedSkill', {
    assetId,
    ...(version !== undefined ? { version } : {}),
  });
}

export async function updateSkillMd(assetId: string, skillMd: string): Promise<{ updatedAt: number }> {
  return labAction<{ updatedAt: number }>('UpdateSkillMd', { assetId, skillMd });
}

export async function putSupportingFile(assetId: string, path: string, content: string): Promise<{ key: string }> {
  return labAction<{ key: string }>('PutSupportingFile', { assetId, path, content });
}

export async function deleteSupportingFile(assetId: string, path: string): Promise<void> {
  return labAction<void>('DeleteSupportingFile', { assetId, path });
}

export async function deleteSkillDraft(assetId: string): Promise<void> {
  return labAction<void>('DeleteSkillDraft', { assetId });
}

// ---------------------------------------------------------------------------
// Declarative Hooks (harness triggers)
// ---------------------------------------------------------------------------

export type HookEventName = 'after_tool_result' | 'before_tool' | 'session_start' | 'session_end';
export type HookActionType = 'render' | 'notify' | 'dispatch';

export interface HookAction {
  type: HookActionType;
  params: Record<string, unknown>;
}

export interface HookAsset {
  assetId: string;
  name: string;
  event: HookEventName | string;
  condition: string;
  action: HookAction;
  priority: number;
  enabled: boolean;
  builtin: boolean;
}

export interface HookListResult {
  userHooks: HookAsset[];
  builtinHooks: HookAsset[];
}

export interface HookValidateResult {
  valid: boolean;
  matched?: boolean;
  error?: string;
}

export interface HookUpsert {
  name: string;
  event: string;
  condition: string;
  actionType: HookActionType;
  actionParams?: Record<string, unknown>;
  priority?: number;
  enabled?: boolean;
}

const hookListCache = createAsyncCache<HookListResult>(30 * 60_000);

export function peekHooks(): HookListResult | undefined {
  return hookListCache.peek();
}

export async function listHooks(forceRefresh = false): Promise<HookListResult> {
  return hookListCache.get(() => labAction<HookListResult>('ListHooks', {}), forceRefresh);
}

export async function createHook(hook: HookUpsert): Promise<{ assetId: string; s3Prefix: string }> {
  const result = await labAction<{ assetId: string; s3Prefix: string }>('CreateHook', { ...hook });
  const cached = hookListCache.peek();
  if (cached) {
    hookListCache.prime({
      ...cached,
      userHooks: [{
        assetId: result.assetId,
        name: hook.name,
        event: hook.event,
        condition: hook.condition,
        action: { type: hook.actionType, params: hook.actionParams ?? {} },
        priority: hook.priority ?? 100,
        enabled: hook.enabled ?? true,
        builtin: false,
      }, ...cached.userHooks.filter(item => item.assetId !== result.assetId)],
    });
  } else hookListCache.invalidate();
  return result;
}

export async function updateHook(assetId: string, updates: Partial<HookUpsert>): Promise<{ updated: boolean }> {
  const result = await labAction<{ updated: boolean }>('UpdateHook', { assetId, ...updates });
  const cached = hookListCache.peek();
  if (cached) {
    hookListCache.prime({
      ...cached,
      userHooks: cached.userHooks.map(item => item.assetId === assetId ? {
        ...item,
        ...(updates.name !== undefined ? { name: updates.name } : {}),
        ...(updates.event !== undefined ? { event: updates.event } : {}),
        ...(updates.condition !== undefined ? { condition: updates.condition } : {}),
        ...(updates.priority !== undefined ? { priority: updates.priority } : {}),
        ...(updates.enabled !== undefined ? { enabled: updates.enabled } : {}),
        action: {
          type: updates.actionType ?? item.action.type,
          params: updates.actionParams ?? item.action.params,
        },
      } : item),
    });
  } else hookListCache.invalidate();
  return result;
}

export async function deleteHook(assetId: string): Promise<{ deleted: boolean }> {
  const result = await labAction<{ deleted: boolean }>('DeleteHook', { assetId });
  const cached = hookListCache.peek();
  if (cached) hookListCache.prime({ ...cached, userHooks: cached.userHooks.filter(item => item.assetId !== assetId) });
  else hookListCache.invalidate();
  return result;
}

export async function validateHookCondition(
  condition: string,
  sampleEvent?: Record<string, unknown>,
): Promise<HookValidateResult> {
  return labAction('ValidateHookCondition', { condition, sampleEvent });
}

// --- Published assets (read-only-ish) --------------------------------------
//
// `ListMyPublishedAssets` returns the latest non-deprecated version per
// asset_id from `USER#<actor>#VERSIONS`. These are immutable — the only
// edit path is `EditAsset`, which forks a working copy back into
// `USER#<actor>#OWNED`. The Capabilities UI surfaces this list in the
// "Published" sub-section of each per-type tab so users can see what
// they've shipped (and fork it for further edits) without conflating it
// with their drafts.

export interface PublishedAssetSummary {
  assetId: string;
  name: string;
  /** As on `DraftMetadata` — absent means "unknown", not "blank". */
  description?: string;
  version: number;
  current: boolean;
  scope: 'private' | 'group' | 'org';
  createdAt: number;  // epoch ms
  updatedAt: number;  // epoch ms
}

export async function listMyPublishedAssets(category: DraftCategory): Promise<PublishedAssetSummary[]> {
  const result = await labAction<PublishedAssetSummary[]>('ListMyPublishedAssets', { category });
  return Array.isArray(result) ? result : [];
}

// EditAsset forks a published version into a working copy under
// `USER#<actor>#OWNED`. Defaults to forking the current version when
// `fromVersion` is omitted. Returns the freshly created working copy's
// metadata; the caller refreshes the drafts list to display it.
export async function editAsset(
  category: DraftCategory,
  assetId: string,
  fromVersion?: number,
): Promise<{ assetId: string; category: DraftCategory; name: string }> {
  return labAction<{ assetId: string; category: DraftCategory; name: string }>(
    'EditAsset',
    fromVersion != null ? { category, assetId, fromVersion } : { category, assetId },
  );
}

// Discard an unpublished working copy (OWNED partition + its S3 prefix). The
// published version, if any, is left intact. Idempotent: discarding a
// non-existent working copy returns `{ discarded: false }`.
export async function discardChanges(
  category: DraftCategory,
  assetId: string,
): Promise<{ discarded: boolean; assetId?: string }> {
  return labAction<{ discarded: boolean; assetId?: string }>(
    'DiscardChanges',
    { category, assetId },
  );
}

// --- Versioning / lifecycle ------------------------------------------------
//
// `PublishAsset` turns the mutable working copy into the next immutable
// version. `ListAssetVersions` enumerates every version of a lineage with
// its version number, lifecycle flags, and scope so the editor can render
// version history (Requirement 8.5).
//
// Publishing is the user's decision: a draft can sit unpublished indefinitely.
// The ONLY thing that stops a publish is a configuration/validation error
// raised by the backend (e.g. scope='group' without groupIds, or a
// missing/empty asset prefix) — those surface as a `LabApiError` whose message
// the caller shows inline.

export interface AssetVersionSummary {
  version: number | null;
  deprecated: boolean;
  current: boolean;
  scope: 'private' | 'group' | 'org';
  createdAt: number;
  updatedAt: number;
  name: string;
}

export async function publishAsset(
  category: DraftCategory,
  assetId: string,
  scope: 'private' | 'group' | 'org' = 'private',
  groupIds?: string[],
): Promise<{ assetId: string; version: number; scope?: string }> {
  return labAction<{ assetId: string; version: number; scope?: string }>(
    'PublishAsset',
    groupIds && groupIds.length ? { category, assetId, scope, groupIds } : { category, assetId, scope },
  );
}

export async function listAssetVersions(assetId: string): Promise<AssetVersionSummary[]> {
  const result = await labAction<AssetVersionSummary[]>('ListAssetVersions', { assetId });
  return Array.isArray(result) ? result : [];
}

// --- Specialty Agents ---

export async function listSpecialtyAgentDrafts(): Promise<SpecialtyAgentDraft[]> {
  return labAction<SpecialtyAgentDraft[]>('ListSpecialtyAgentDrafts', {});
}

export async function createSpecialtyAgentDraft(
  name: string,
  persona: string,
  compatibleSkills?: string[],
  extras?: { model?: string; toolCollections?: string[]; integrationConfig?: IntegrationConfig | null; description?: string },
): Promise<{ assetId: string; s3Prefix: string }> {
  return labAction<{ assetId: string; s3Prefix: string }>('CreateSpecialtyAgentDraft', {
    name,
    persona,
    compatibleSkills,
    ...(extras ?? {}),
  });
}

export async function readSpecialtyAgentDraft(assetId: string): Promise<SpecialtyAgentDraft> {
  return labAction<SpecialtyAgentDraft>('ReadSpecialtyAgentDraft', { assetId });
}

export async function updateSpecialtyAgentDraft(
  assetId: string,
  updates: {
    persona?: string;
    compatibleSkills?: string[];
    model?: string;
    toolCollections?: string[];
    integrationConfig?: IntegrationConfig | null;
    description?: string;
  },
): Promise<{ updatedAt: number }> {
  return labAction<{ updatedAt: number }>('UpdateSpecialtyAgentDraft', { assetId, ...updates });
}

export async function deleteSpecialtyAgentDraft(assetId: string): Promise<void> {
  return labAction<void>('DeleteSpecialtyAgentDraft', { assetId });
}

// --- Tool Collections ---

export async function listToolCollectionDrafts(): Promise<ToolCollectionDraft[]> {
  return labAction<ToolCollectionDraft[]>('ListToolCollectionDrafts', {});
}

/**
 * A catalog read, plus the gateways it could not reach.
 *
 * `unreadable` is why this type exists. `/catalog`'s fast path reads gateway
 * tools from the AgentCore control plane, which cannot describe a cross-account
 * gateway or an MCP-server target — those are visible only from the data plane.
 * Carrying the names lets a caller paint what arrived and then ask for the rest,
 * instead of treating "not read yet" as "has no tools".
 */
export interface GatewayCatalog {
  tools: GatewayToolDescriptor[];
  unreadable: string[];
}

const gatewayToolsCache = createAsyncCache<GatewayCatalog>(10 * 60_000);

export function peekGatewayTools(): GatewayToolDescriptor[] | undefined {
  return gatewayToolsCache.peek()?.tools;
}

/** Flatten `{ sections: [{ label, tools }] }` into the descriptor list the UI uses. */
function toGatewayCatalog(data: any): GatewayCatalog {
  const sections = data?.sections ?? [];
  const tools: GatewayToolDescriptor[] = [];
  for (const section of sections) {
    const sectionLabel = section?.label ?? 'Unknown';
    const sectionTools = section?.tools ?? [];
    if (!Array.isArray(sectionTools)) continue;
    for (const t of sectionTools) {
      tools.push({
        name: t.name ?? '',
        description: t.description ?? '',
        version: t.version ?? '1.0',
        inputSchema: t.inputSchema,
        source: sectionLabel,
      });
    }
  }
  // Absent ≠ empty, but here absent genuinely means complete: an older proxy that
  // predates the field never had anything outstanding to report.
  const unreadable = Array.isArray(data?.unreadable) ? data.unreadable.map(String) : [];
  return { tools, unreadable };
}

async function fetchCatalogPath(path: string): Promise<GatewayCatalog> {
  try {
    const res = await authFetch(path);
    if (!res.ok) {
      throw new LabApiError(res.status, `Failed to fetch gateway tools: ${res.statusText}`);
    }
    return toGatewayCatalog(await res.json());
  } catch (err) {
    if (err instanceof LabApiError) throw err;
    throw new LabApiError(0, err instanceof Error ? err.message : 'Failed to fetch gateway tools');
  }
}

/**
 * Phase 1 — the fast catalog: DynamoDB plus the control plane, no runtime.
 *
 * May be incomplete; `unreadable` says so. Callers that display tools should
 * follow up with `loadLiveGatewayCatalog` when it is non-empty.
 */
export async function listGatewayCatalog(forceRefresh = false): Promise<GatewayCatalog> {
  // Explicit Refresh adds ?refresh=1 to bypass the proxy's own 60s cache too.
  const path = forceRefresh ? '/catalog?refresh=1' : '/catalog';
  return gatewayToolsCache.get(() => fetchCatalogPath(path), forceRefresh);
}

/**
 * Phase 2 — ask the data plane for what the control plane could not see.
 *
 * This is the one call that can wake the agent runtime, so it belongs in the
 * background, never on first paint. The result is primed into the shared cache so
 * the next reader (the tool picker, say) gets the complete list for free.
 */
export async function loadLiveGatewayCatalog(): Promise<GatewayCatalog> {
  const complete = await fetchCatalogPath('/catalog?live=1');
  gatewayToolsCache.prime(complete);
  return complete;
}

export async function listGatewayTools(forceRefresh = false): Promise<GatewayToolDescriptor[]> {
  return (await listGatewayCatalog(forceRefresh)).tools;
}

export async function refreshGatewayTools(): Promise<GatewayToolDescriptor[]> {
  return listGatewayTools(true);
}

export async function createToolCollectionDraft(name: string, toolRefs: ToolRef[]): Promise<{ assetId: string; s3Prefix: string }> {
  return labAction<{ assetId: string; s3Prefix: string }>('CreateToolCollectionDraft', { name, toolRefs });
}

export async function readToolCollectionDraft(assetId: string): Promise<ToolCollectionDraft> {
  return labAction<ToolCollectionDraft>('ReadToolCollectionDraft', { assetId });
}

export async function updateToolCollectionDraft(assetId: string, updates: { name?: string; toolRefs?: ToolRef[] }): Promise<{ updatedAt: number }> {
  return labAction<{ updatedAt: number }>('UpdateToolCollectionDraft', { assetId, ...updates });
}

export async function deleteToolCollectionDraft(assetId: string): Promise<void> {
  return labAction<void>('DeleteToolCollectionDraft', { assetId });
}


// --- Workflows ---

export async function listWorkflowDrafts(): Promise<WorkflowDraft[]> {
  return labAction<WorkflowDraft[]>('ListWorkflowDrafts', {});
}

export async function createWorkflowDraft(name: string, steps: WorkflowStep[]): Promise<{ assetId: string; s3Prefix: string }> {
  return labAction<{ assetId: string; s3Prefix: string }>('CreateWorkflowDraft', { name, steps });
}

export async function readWorkflowDraft(assetId: string): Promise<WorkflowDraft> {
  return labAction<WorkflowDraft>('ReadWorkflowDraft', { assetId });
}

export async function updateWorkflowDraft(assetId: string, updates: { name?: string; steps?: WorkflowStep[] }): Promise<{ updatedAt: number }> {
  return labAction<{ updatedAt: number }>('UpdateWorkflowDraft', { assetId, ...updates });
}

export async function deleteWorkflowDraft(assetId: string): Promise<void> {
  return labAction<void>('DeleteWorkflowDraft', { assetId });
}

// --- Chat (streaming) ---

export async function sendChatMessage(
  message: string,
  sessionId: string,
  onEvent: (event: StreamEvent) => void,
  history?: Array<{ role: string; content: string }>,
): Promise<void> {
  // ChatMessage returns a regular JSON response (not streaming)
  const params: Record<string, unknown> = { message, sessionId };
  if (history && history.length > 0) {
    // Send last 20 messages as context
    params.history = history.slice(-20);
  }
  const result = await labAction<{
    response: string;
    affectedAssetIds?: string[];
    toolCallsMade?: unknown[];
  }>('ChatMessage', params, 120_000);

  // Synthesize stream events from the response
  if (result.response) {
    onEvent({ type: 'text', text: result.response });
  }

  // If assets were affected, emit draft_mutation events to refresh all drawers
  if (result.affectedAssetIds && result.affectedAssetIds.length > 0) {
    onEvent({ type: 'draft_mutation', category: 'skill', assetId: '', mutation: 'updated' });
    onEvent({ type: 'draft_mutation', category: 'specialty-agent', assetId: '', mutation: 'updated' });
    onEvent({ type: 'draft_mutation', category: 'tool-collection', assetId: '', mutation: 'updated' });
    onEvent({ type: 'draft_mutation', category: 'workflow', assetId: '', mutation: 'updated' });
  }

  // Emit execution summary
  onEvent({
    type: 'execution_summary',
    summary: {
      type: 'execution_summary',
      total_duration_ms: 0,
      cycles: 1,
      tools: {},
      usage: {},
    },
  });
}

// --- Custom Agent config resolution (test console) ---

/** What a Custom Agent would actually bind at invoke time. */
export interface ResolvedAgentConfig {
  assetId: string;
  name: string;
  source: 'draft' | 'published';
  version: number | null;
  /** Pinned Bedrock inference profile, or null when the runtime default applies. */
  modelId: string | null;
  modelSource: 'pinned' | 'runtime-default';
  /** Gateway tool names that will bind. */
  tools: string[];
  /** Skill ids the agent declares as compatible. */
  skills: string[];
  /** Tool collections that resolved to nothing and were dropped. */
  droppedCollections: string[];
  toolCollections: { id: string; name?: string | null }[];
  personaChars: number;
  /** False when the gateway projection could not be read — an empty `tools`
   *  list then means "unknown", not "none". */
  gatewayProjectionAvailable: boolean;
}

/**
 * Resolve what a Custom Agent would bind, without invoking it.
 *
 * Answers "which tools and skills actually load" — which a run cannot, because
 * `execution_summary.tools` reports tools *called*, leaving a bound-but-unused
 * tool invisible. Costs no model tokens.
 */
export async function resolveAgentConfig(params: {
  assetId: string;
  source?: 'draft' | 'published';
  version?: number;
}): Promise<ResolvedAgentConfig> {
  return labAction<ResolvedAgentConfig>('ResolveAgentConfig', {
    assetId: params.assetId,
    source: params.source ?? 'draft',
    ...(params.version !== undefined ? { version: params.version } : {}),
  });
}

// --- Test Harness ---

export async function runHarness(
  params: { targetAssetId: string; sampleInput: string; bindings?: { agentAssetId?: string; skillAssetId?: string; toolCollectionAssetId?: string }; timeoutSeconds?: number },
  onEvent: (event: StreamEvent) => void,
): Promise<void> {
  // RunHarness returns a regular JSON response (not streaming)
  // Use a longer timeout since LLM invocations can take minutes
  const timeoutMs = ((params.timeoutSeconds ?? 300) + 30) * 1000; // harness timeout + 30s buffer
  const result = await labAction<{
    runId: string;
    subject: string;
    status: string;
    startedAt: number;
    endedAt: number;
    outputs: unknown[];
    logs?: string;
    error?: { cause: string; stepId?: string };
  }>('RunHarness', {
    assetId: params.targetAssetId,
    sampleInput: params.sampleInput,
    bindings: params.bindings,
    timeoutSeconds: params.timeoutSeconds,
  }, timeoutMs);

  // Synthesize stream events from the response so the panel can display results
  if (result.outputs && result.outputs.length > 0) {
    const outputText = result.outputs.map(o => typeof o === 'string' ? o : JSON.stringify(o, null, 2)).join('\n');
    onEvent({ type: 'text', text: outputText });
  }

  // Send execution summary
  const durationMs = (result.endedAt && result.startedAt) ? result.endedAt - result.startedAt : 0;
  onEvent({
    type: 'execution_summary',
    summary: {
      type: 'execution_summary',
      total_duration_ms: durationMs,
      cycles: 1,
      tools: {},
      usage: {},
    },
  });

  // If there was an error, throw so the panel shows failure
  if (result.status === 'failed' && result.error) {
    const errMsg = result.error.stepId
      ? JSON.stringify({ cause: result.error.cause, stepId: result.error.stepId })
      : result.error.cause;
    throw new LabApiError(0, errMsg);
  }
}

export async function cancelHarnessRun(runId: string): Promise<void> {
  return labAction<void>('CancelHarnessRun', { runId });
}

/*
 * `ListHarnessRuns` has no client. The Test Harness no longer shows past runs, and
 * the shape below is why removing this is a fix rather than a deletion: the interface
 * it returned was a fiction. `HarnessRunRecord` declared `draftRef`, `subject`,
 * `outputs`, `logs` and `durationMs`, while the server's `list_runs` — still a stub in
 * its own docstring — returns only `runId`, `targetAssetId`, `status`, `startedAt` and
 * `endedAt`. `labAction` casts without validating, so TypeScript asserted a shape that
 * never arrived and the history rows crashed the app on `run.draftRef.category` the
 * moment a single run existed.
 *
 * Re-add both together, against the response the server actually sends.
 */

// --- Readiness ---

export async function checkReadiness(workflowAssetId: string): Promise<ReadinessReport> {
  return labAction<ReadinessReport>('CheckReadiness', { workflowAssetId });
}

// --- Session Persistence (AgentCore Memory) ---

export interface MemorySession {
  sessionId: string;
  actorId: string;
  createdAt?: string;
}

export interface MemoryEvent {
  eventId: string;
  sessionId: string;
  actorId: string;
  eventTimestamp?: string;
  payload?: Array<{
    conversational?: { role: string; content: { text: string } };
  }>;
  metadata?: Record<string, unknown>;
}

/**
 * List sessions from AgentCore Memory.
 */
export async function listMemorySessions(): Promise<MemorySession[]> {
  const res = await authFetch('/memory/sessions');
  if (!res.ok) throw new LabApiError(res.status, 'Failed to list memory sessions');
  const data = await res.json();
  return data.sessions ?? data.sessionSummaries ?? [];
}

/**
 * List events (messages) for a session from AgentCore Memory.
 */
export async function listMemoryEvents(sessionId: string): Promise<MemoryEvent[]> {
  const res = await authFetch(`/memory/events?sid=${encodeURIComponent(sessionId)}`);
  if (!res.ok) throw new LabApiError(res.status, 'Failed to list memory events');
  const data = await res.json();
  return data.events ?? data.eventSummaries ?? [];
}

/**
 * Write a conversation turn to AgentCore Memory.
 * @param sessionId - The session to write to
 * @param messages - Array of {role, content} for the turn (user + assistant)
 */
export async function writeMemoryEvent(
  sessionId: string,
  messages: Array<{ role: string; content: string }>,
): Promise<void> {
  const res = await authFetch('/memory/events', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sessionId, messages }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new LabApiError(res.status, body.error ?? 'Failed to write memory event');
  }
}

/**
 * Convert AgentCore Memory events to ChatMessage format for the UI.
 *
 * Two on-the-wire formats are supported in the conversational `content.text`
 * field:
 *
 * 1. **Strands SDK envelope (current, written by `AgentCoreMemorySessionManager`).**
 *    `text` is `JSON.stringify(SessionMessage)`, which decodes to roughly
 *    `{ message: { role: 'user'|'assistant', content: [{ text: '...' }, ...] }, message_id, ... }`.
 *    We parse the envelope and concatenate every text block in `message.content`.
 *    Tool-use / tool-result blocks are skipped — they're internal agent state,
 *    not user-visible chat content.
 *
 * 2. **Legacy raw-text** (written by an older UI direct-write path that has
 *    since been removed from `ChatCanvas`). `text` is the raw user/assistant
 *    string. We surface it as-is. This branch keeps history readable for
 *    sessions that were persisted before the writer consolidation.
 *
 * Empty content (e.g. an SDK envelope with only tool-use blocks) is dropped so
 * the UI doesn't render blank bubbles.
 */
/**
 * Strip the agent's internal `[Current date: …]` augmentation from a user turn.
 * Defined in ./promptNormalize (kept out of lab-api's mockable surface); imported
 * here so Memory-sourced user turns display clean.
 */
export function memoryEventsToChatMessages(events: MemoryEvent[]): ChatMessage[] {
  const messages: ChatMessage[] = [];
  // AgentCore ListEvents returns events newest-first. The chat must render
  // oldest-first, so sort ascending by timestamp before flattening. Without
  // this, a session rebuilt from Memory (localStorage cache miss) shows the
  // transcript reversed. A stable sort preserves intra-turn payload order
  // (user then assistant) for events that share a timestamp.
  const ordered = [...events].sort((a, b) => {
    const ta = a.eventTimestamp ? Date.parse(a.eventTimestamp) : 0;
    const tb = b.eventTimestamp ? Date.parse(b.eventTimestamp) : 0;
    if (!Number.isFinite(ta) || !Number.isFinite(tb) || ta === tb) return 0;
    return ta - tb;
  });
  for (const event of ordered) {
    if (!event.payload) continue;
    for (const item of event.payload) {
      if (!item.conversational) continue;
      const { role, content } = item.conversational;
      const rawText = content?.text ?? '';

      // Try the SDK envelope first. If `JSON.parse` succeeds and the parsed
      // value has the SessionMessage shape, extract text blocks; otherwise
      // fall through to the raw-text path (legacy UI writes, or any other
      // shape we don't recognize — better to render *something* than to
      // drop the message).
      let displayText = rawText;
      let envelopeRole: string | undefined;
      try {
        const parsed = JSON.parse(rawText);
        const message = parsed?.message;
        const blocks = message?.content;
        if (Array.isArray(blocks)) {
          const texts = blocks
            .filter((b: any) => b && typeof b === 'object' && typeof b.text === 'string')
            .map((b: any) => b.text as string);
          // Only treat this as a Strands envelope if we actually pulled text
          // out — otherwise the message is a pure tool-use/tool-result event,
          // which the chat surface should ignore.
          if (texts.length > 0) {
            displayText = texts.join('');
            if (typeof message?.role === 'string') {
              envelopeRole = message.role;
            }
          } else if (blocks.length > 0) {
            // Recognized envelope, but no chat-visible content. Skip.
            continue;
          }
        }
      } catch {
        // Not JSON — legacy raw-text path. `displayText` already holds the
        // raw string.
      }

      if (!displayText) continue;

      const effectiveRole = (envelopeRole ?? role ?? '').toLowerCase();
      const mappedRole = effectiveRole === 'user' ? 'user' : 'assistant';
      const displayContent = mappedRole === 'user' ? normalizeUserTurnContent(displayText) : displayText;
      const last = messages[messages.length - 1];
      if (mappedRole === 'assistant' && last && last.role === 'assistant') {
        // Coalesce consecutive assistant Memory events into ONE turn. The Strands SDK writes a
        // multi-part agent response (pre-tool text, mid-turn text, post-tool text) as several
        // events; rendered 1:1 they became separate bubbles — the "turn 0 split into many chunks"
        // fragmentation (session cd95cd42), seen only when the durable transcript is missing the
        // turn and chat falls back to Memory. One logical turn = one bubble.
        last.content = last.content ? `${last.content}\n\n${displayContent}` : displayContent;
        continue;
      }
      messages.push({
        id: `mem-${event.eventId}-${messages.length}`,
        role: mappedRole,
        content: displayContent,
        timestamp: event.eventTimestamp ? new Date(event.eventTimestamp).getTime() : Date.now(),
      });
    }
  }
  return messages;
}

// --- Session Metadata Store (DynamoDB) ---

export interface SessionMetadata {
  sessionId: string;
  actorId: string;
  title: string;
  active: number;
  createdAt: string;
  lastActivityAt: string;
  /**
   * When true the session is fully frozen (demo lock): it cannot be deleted,
   * renamed, or chatted in. The server enforces this (returns 423 for those
   * operations); the UI mirrors it by disabling the affordances. Only
   * unlocking (setting locked:false) is permitted while locked.
   */
  locked?: boolean;
  renderHistory?: any[];
  /**
   * Durable copy of the rich chat transcript (per-message steps + execution
   * summary), persisted via {@link updateSessionTranscript} and restored via
   * {@link getSessionTranscript}. This is what lets a session reloaded in a
   * different browser (or after a localStorage eviction) render the same
   * collapsed step groups + turn duration it showed live, rather than the
   * text-only bubbles rebuilt from AgentCore Memory.
   */
  transcript?: ChatMessage[];
  /** media-harness-projects-access: the session's Active_Project id, when the row has one (Req 3.6). */
  activeProjectId?: string;
  /** media-harness-projects-access: the Web_Search_Toggle value, when the row has one (Req 3.6). */
  webSearch?: boolean;
}

/**
 * List active sessions from DynamoDB (sorted by lastActivityAt desc).
 */
export async function listSessions(): Promise<SessionMetadata[]> {
  const res = await authFetch('/sessions');
  if (!res.ok) {
    throw new LabApiError(res.status, 'Failed to list sessions');
  }
  const data = await res.json();
  return data.sessions ?? [];
}

/**
 * Create a session row in DynamoDB (called on first message).
 */
export async function createSession(sessionId: string, title: string): Promise<boolean> {
  const res = await authFetch('/sessions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sessionId, title }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new LabApiError(res.status, body.error ?? 'Failed to create session');
  }
  absentSessions.delete(sessionId);
  return res.status === 201;
}

/**
 * Sessions whose row the server says does not exist (a 404 from a render-history PATCH): deleted in
 * another tab, or never persisted. Every renderHistory change would otherwise re-PATCH and re-log
 * forever (observed in staging: `404 Session not found` + `[persist] session renderHistory failed`
 * every few seconds for a session deleted elsewhere). Remembering the id makes the first failure the
 * only one; `createSession` clears it, so a session persisted later resumes saving normally.
 */
const absentSessions = new Set<string>();

/** Test seam: forget the absent-session markers. */
export function __resetAbsentSessionsForTests(): void {
  absentSessions.clear();
}

/**
 * Update session activity timestamp (called after each turn).
 */
export async function updateSessionActivity(sessionId: string, title?: string): Promise<void> {
  const body: Record<string, string> = {};
  if (title) body.title = title;
  const res = await authFetch(`/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new LabApiError(res.status, data.error ?? 'Failed to update session');
  }
}

/**
 * Soft-delete a session (sets active=0, TTL 30 days).
 */
export async function deleteSession(sessionId: string): Promise<void> {
  const res = await authFetch(`/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'DELETE',
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new LabApiError(res.status, data.error ?? 'Failed to delete session');
  }
}

/**
 * Lock or unlock a session (demo full-freeze). A locked session is frozen
 * server-side: delete / rename / new turns all return 423 while locked. Only
 * unlocking (locked:false) is accepted while locked. A 423 surfaces as a
 * {@link LabApiError} with `status === 423`.
 */
export async function setSessionLock(sessionId: string, locked: boolean): Promise<void> {
  const res = await authFetch(`/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ locked }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new LabApiError(res.status, data.error ?? 'Failed to update session lock');
  }
}

// --- Config (runtime settings exposed by proxy) ---

export async function getConfig(): Promise<{ artifactsDomain: string; maxUploadBytes?: number }> {
  // The shared page-wide load (config/runtimeConfig.ts). A failure is NOT cached: the old fallback
  // `{artifactsDomain: ''}` stuck for the life of the page, hiding Projects until a reload.
  try {
    return (await loadRuntimeConfig()) as { artifactsDomain: string; maxUploadBytes?: number };
  } catch {
    return { artifactsDomain: '' };
  }
}

export interface SessionRender {
  key: string;
  manifestId: string;
  createdAt: string;
  label?: string;
  manifest?: Record<string, unknown>;
  /** Server-composed composite tab fields (GET /sessions/:id/renders composes per source). */
  composite?: boolean;
  sourceKey?: string;
  /** For a composite: the solo render keys it absorbed, so the UI drops those solo tabs. */
  memberKeys?: string[];
}

/**
 * The AUTHORITATIVE renders for a session, read from the manifests in S3.
 *
 * renderHistory used to be the only record, and the UI was its only writer -- so a render
 * that completed after a turn's stream closed was computed, stored in S3 and then invisible.
 * That is the normal case here, because media jobs settle asynchronously. This reads the
 * store that always had the truth in it.
 */
export async function getSessionRenders(sessionId: string): Promise<SessionRender[]> {
  const res = await authFetch(`/sessions/${encodeURIComponent(sessionId)}/renders`);
  if (!res.ok) return [];
  const body = await res.json();
  return Array.isArray(body?.renders) ? body.renders : [];
}

/**
 * Surface a persistence failure that callers previously swallowed with `.catch(() => {})`.
 *
 * A failed transcript/renderHistory write means a chat turn or render silently does NOT survive a
 * refresh — e.g. the session row hit DynamoDB's 400 KB item limit (the "assistant turns vanish on
 * reload" bug). Swallowing it made that data loss invisible. Logging keeps it diagnosable; callers
 * stay non-throwing (localStorage remains the within-browser copy).
 */
export function logPersistFailure(op: string, err: unknown): void {
  // eslint-disable-next-line no-console
  console.error(`[persist] ${op} failed — this may not survive a page refresh:`, err);
}

export async function updateSessionRenderHistory(sessionId: string, renderHistory: any[]): Promise<void> {
  // A session the server has no row for never accepts this write; keep quiet after the first 404.
  if (absentSessions.has(sessionId)) return;
  // Persist LEAN refs — drop each block's inlined `manifest` (a composite timeline manifest can be
  // hundreds of KB). renderHistory shares the session row with the chat transcript, so an inlined
  // manifest could push the row past DynamoDB's 400 KB limit and silently block ALL writes to the
  // row (dropping chat turns). The S3 render manifests are authoritative and re-derive renders on
  // load, so only manifestUrl / sourceKey / memberKeys refs need to persist. The proxy strips this
  // too (belt-and-suspenders); doing it here also keeps the PATCH payload small.
  const lean = (Array.isArray(renderHistory) ? renderHistory : []).map((entry) => {
    if (!entry || typeof entry !== 'object' || !Array.isArray(entry.blocks)) return entry;
    return {
      ...entry,
      blocks: entry.blocks.map((b: any) => {
        if (!b || typeof b !== 'object' || !b.data || typeof b.data !== 'object' || !('manifest' in b.data)) return b;
        const { manifest, ...leanData } = b.data;
        void manifest;
        return { ...b, data: leanData };
      }),
    };
  });
  const res = await authFetch(`/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ renderHistory: lean }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    if (res.status === 404) absentSessions.add(sessionId);
    throw new LabApiError(res.status, data.error ?? 'Failed to update render history');
  }
}

/**
 * The durable rich chat transcript for a session (per-message steps + execution
 * summary), read from the DynamoDB session row.
 *
 * The mirror of {@link getSessionRenders} for chat: the live stream folds a turn's
 * steps + text + execution summary into ONE assistant message, but that rich shape
 * was only ever cached in localStorage. On a cache miss (a different browser, an
 * eviction) the UI fell back to AgentCore Memory, which stores text only — so the
 * SAME session reloaded as N separate bubbles with no collapsed steps and no turn
 * duration. This reads the durable copy so the restore matches what was on screen.
 *
 * Returns `[]` when the row has no persisted transcript (older sessions, or ones
 * created before this field existed) so the caller falls through to Memory.
 */
export async function getSessionTranscript(sessionId: string): Promise<ChatMessage[]> {
  const res = await authFetch(`/sessions/${encodeURIComponent(sessionId)}/transcript`);
  if (!res.ok) return [];
  const body = await res.json().catch(() => ({}));
  return Array.isArray(body?.transcript) ? body.transcript : [];
}

/**
 * Persist the rich chat transcript to the durable DynamoDB session row. Best-effort
 * and debounced by the caller — localStorage stays the fresher within-browser copy;
 * this is the cross-browser / cache-miss restore source. The proxy caps the stored
 * array (last 100 messages) to keep the row under DynamoDB's 400 KB item limit.
 */
export async function updateSessionTranscript(sessionId: string, transcript: ChatMessage[]): Promise<void> {
  const res = await authFetch(`/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ transcript }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new LabApiError(res.status, data.error ?? 'Failed to update transcript');
  }
}

/**
 * Permanently delete a render by removing its AUTHORITATIVE S3 manifest.
 *
 * The render list is derived from the manifests in S3 (see getSessionRenders), so removing a tab
 * from local state alone is undone on the next load — the manifest is re-listed and the tab "comes
 * back". Deleting the manifest is the clean delete: the tab is derived from it and there is no
 * second store to reconcile. `key` is the manifest S3 key (artifacts/<session>/render/<id>.json).
 */
export async function deleteSessionRender(sessionId: string, key: string): Promise<void> {
  const res = await authFetch(
    `/sessions/${encodeURIComponent(sessionId)}/renders?key=${encodeURIComponent(key)}`,
    { method: 'DELETE' },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new LabApiError(res.status, data.error ?? 'Failed to delete render');
  }
}

/**
 * Persist a client-authored render view (an Expand asset-player tab) as a manifest in the session's
 * render store, so the server lists it like any render and it survives refresh and later renders.
 * Resolves the manifest key (artifacts/<session>/render/<id>.json), the tab's stable identity.
 */
export async function createSessionRender(sessionId: string, manifest: Record<string, unknown>): Promise<string> {
  const res = await authFetch(`/sessions/${encodeURIComponent(sessionId)}/renders`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ manifest }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || typeof data?.key !== 'string') {
    throw new LabApiError(res.status, data?.error ?? 'Failed to save render view');
  }
  return data.key;
}

/**
 * Merge a view's timeline state (selected range, frame marker, crop box) into its persisted
 * manifest. A field sent as `null` is cleared.
 */
export async function patchSessionRenderView(
  sessionId: string,
  key: string,
  viewState: Record<string, unknown>,
): Promise<void> {
  const res = await authFetch(
    `/sessions/${encodeURIComponent(sessionId)}/renders?key=${encodeURIComponent(key)}`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ viewState }),
    },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new LabApiError(res.status, data.error ?? 'Failed to save view state');
  }
}

// --- Generic ---

export async function renameAsset(assetId: string, category: DraftCategory, name: string): Promise<{ assetId: string; name: string; updatedAt: number }> {
  return labAction<{ assetId: string; name: string; updatedAt: number }>('RenameAsset', { assetId, category, name });
}
