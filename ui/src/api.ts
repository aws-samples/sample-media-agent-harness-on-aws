import { authFetch } from "./auth/authFetch";
import { invalidateAssetFolders } from "./projects/assetFolders";
import { createAsyncCache } from "./utils/asyncCache";

const AGENT_URL = "/api/invocations";

export interface AgentRequest {
  prompt: string;
  session_id?: string;
  /**
   * Orchestrator model for THIS turn. Sent per request because the agent builds
   * a fresh Agent per request; the previous approach mutated a container-level
   * object, which reverted on cold start, missed other concurrent containers,
   * and leaked across users sharing a container.
   */
  model_id?: string;
  /**
   * Reasoning-effort cap for THIS turn (the UI slider). One of low|medium|high,
   * or absent for "Default" — absent means the runtime applies its env
   * BEDROCK_REASONING_EFFORT default. Only sent for models that support it; the
   * backend is fail-safe and never attaches the field to an incapable model.
   */
  effort?: 'low' | 'medium' | 'high';
  /** When true, plan_and_execute dispatches the coordinator asynchronously
   *  and returns immediately. The Activity Panel shows real-time progress. */
  /**
   * Persistence hints for the proxy-authoritative transcript
   * (docs/design-proxy-authoritative-transcript.md). The proxy persists the turn server-side, so it
   * needs the CLEAN user text (the `prompt` may be augmented with `[File attached: …]` / `[source: …]`
   * / `[Current date: …]`), the attachment to preview, and a stable per-turn id it can upsert by.
   * All optional: absent, the proxy falls back to normalizing the prompt and generating a turn id.
   */
  display_text?: string;
  attachment?: { url: string; name: string };
  /** Every file attached to the turn, in order; `attachment` is the first, kept for single-file readers. */
  attachments?: { url: string; name: string }[];
  turn_id?: string;
  /**
   * media-harness-projects-access (Req 3.8, 3.9): the session's Active_Project and Web_Search_Toggle, from a
   * snapshot taken when the turn started. Omitted when Projects are disabled, so the body is unchanged.
   */
  active_project_id?: string;
  web_search?: boolean;
  /**
   * media-harness-search-qa-rendering (Req 3.23): a card's Find_Similar reference, forwarded verbatim from
   * the `chat:send` event. The PROXY turn gate (not the client) resolves it to the turn context's
   * `find_similar` object and builds the image URI; the client only names the asset. Present only on a turn
   * started from a Moment card's "Find similar".
   */
  find_similar_ref?: {
    project_id: string;
    asset_id: string;
    frame_s3_key: string | null;
    kind: string;
    display_name: string;
    timecode: string;
  };
}

export interface AgentResponse {
  response: string;
  session_id?: string;
}

export interface StepEvent {
  type: "step";
  status: "running" | "done" | "pending" | "completed" | "failed" | "skipped" | "replanning" | "plan_ready";
  label: string;
  duration_ms?: number;
  agent?: string;
  phase?: string;
  node_id?: string;
  plan_type?: "high_level_plan" | "execution_dag";
  plan_data?: Record<string, unknown>;
  gate_id?: string;
  // Set by the Render Agent's output_ref (forwarded via dispatch_tool): the
  // S3 key of a render manifest (artifacts/.../render/*.json) the frontend
  // should fetch and render.
  render_manifest_key?: string;
  render_label?: string;
  // Correlation id from dispatch_render: present on the leading "Composing…"
  // marker AND on the eventual manifest step, so the UI can open a per-tab
  // placeholder and fill the SAME tab when the manifest arrives.
  render_request_id?: string;
  // Terminal signal for a composing render that produced no manifest — the UI
  // removes the matching composing tab so it never spins forever.
  render_failed?: boolean;
}

/**
 * One citation record on the wire, exactly as the agent's `TurnOutcome.citations_wire()` serializes it
 * (media-harness-search-qa-rendering Component 4, "Wire and storage format", Req 2.6, 2.12). Built from
 * typed fields only — NO field ever holds a URL. The verified-only fields (`ref`, `kind`,
 * `asset_display_name`) are present when `verified` is true; `reason` is present when it is false.
 */
export interface CitationRecordWire {
  key: string;
  text: string;
  display_name: string;
  start_ms: number;
  end_ms: number;
  verified: boolean;
  reason?: "no_matching_hit" | "registry_unavailable";
  /** Verified only: the Asset_Reference. `frame_s3_key` is null for a time-range (non-frame) citation. */
  ref?: { project_id: string; asset_id: string; frame_s3_key: string | null };
  /** A page citation (`[name p.N]`, a document): the cited (first) page. Absent for a timecode citation. */
  page?: number;
  /** A page RANGE citation (`[name p.N–M]`): the last page. Absent for one page. */
  page_end?: number;
  /** Verified only. */
  kind?: "video" | "image" | "audio" | "document";
  /** Verified only. */
  asset_display_name?: string;
}

/**
 * The `citations` object carried on `execution_summary` and on the stored assistant message. At most
 * 100 items (about 25 KB), no URL. Classification is final once stored — nothing re-verifies it.
 */
export interface CitationBlock {
  v: 1;
  project_id: string;
  truncated: boolean;
  items: CitationRecordWire[];
}

export interface ExecutionSummary {
  type: "execution_summary";
  total_duration_ms: number;
  cycles: number;
  tools: Record<string, { calls: number; total_ms: number; avg_ms: number }>;
  usage: { inputTokens?: number; outputTokens?: number; totalTokens?: number };
  model?: string;
  /** The model the agent actually ran this turn (authoritative for the badge). */
  model_id?: string;
  /**
   * Turn-end citation verification block (media-harness-search-qa-rendering Req 2.6, 2.12). Present only
   * on a Projects turn that produced citations; absent on every other turn and on pre-feature transcripts.
   */
  citations?: CitationBlock;
}

/** The turn's summary event. A turn that filed files (`organize_project`) moved them behind every open view's
 *  back, so the folder store is told to re-ask where they live. */
export function summaryEvent(summary: ExecutionSummary): { type: "execution_summary"; summary: ExecutionSummary } {
  if (Object.keys(summary.tools ?? {}).some((name) => name.endsWith("organize_project"))) invalidateAssetFolders();
  return { type: "execution_summary", summary };
}

export interface SkillInfo {
  name: string;
  description: string;
  activated?: boolean;
  tools?: string[];
  saved?: boolean;
  dirty?: boolean;
  source?: "local" | "s3" | "memory";
}

/**
 * A built-in sub-agent runtime (task / render / planning / design).
 *
 * These ship with the harness — distinct from user-authored specialty agents.
 * `invokedVia` is surfaced because the harness has two sub-agent invocation
 * paths: `dispatch_tool` (task, render) and `plan_and_execute_tool`
 * (planning, design).
 */
/**
 * @deprecated The Agents panel uses `BuiltInAgentRow` from `capability-api`, which is
 * what the Capability API actually returns. Kept only for callers of the runtime's
 * `mcp_list_agents` action, whose response still has this shape.
 */
export interface BuiltInAgentInfo {
  key: string;
  label: string;
  purpose: string;
  invokedVia: string;
  configured: boolean;
  runtimeId?: string;
  arnEnv?: string;
}

export type StreamCallback = (event:
  | { type: "step"; step: StepEvent }
  | { type: "text"; text: string }
  | { type: "execution_summary"; summary: ExecutionSummary }
  | { type: "render"; manifestKey: string; label?: string; id?: string }
  | { type: "render_pending"; id: string; label?: string }
  | { type: "render_failed"; id: string }
) => void;

function tryParseStep(raw: string): StepEvent | null {
  try {
    let parsed = JSON.parse(raw);
    if (typeof parsed === "string") {
      try { parsed = JSON.parse(parsed); } catch { return null; }
    }
    if (parsed?.type === "step") return parsed as StepEvent;
  } catch { /* not JSON */ }
  return null;
}

function tryParseSummary(raw: string): ExecutionSummary | null {
  try {
    let parsed = JSON.parse(raw);
    if (typeof parsed === "string") {
      try { parsed = JSON.parse(parsed); } catch { return null; }
    }
    if (parsed?.type === "execution_summary") return parsed as ExecutionSummary;
  } catch { /* not JSON */ }
  return null;
}

function extractText(raw: string): string {
  try {
    const parsed = JSON.parse(raw);
    if (typeof parsed === "string") return parsed;
    if (parsed?.type === "step") return "";
    return raw;
  } catch {
    return raw;
  }
}

// Streaming invoke (used by chat)

/**
 * A non-2xx answer from `/api/invocations`. The message is the same text this function always threw; `status`
 * is added, and `body` is read ONLY for a 409 so the turn gate's `project_access_lost` can be recognised
 * (`parseProjectAccessLost`). Every other status behaves exactly as before.
 */
export class AgentRequestError extends Error {
  readonly status: number;
  readonly body: unknown;
  constructor(status: number, message: string, body?: unknown) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

export async function invokeAgentStreaming(
  request: AgentRequest,
  onEvent: StreamCallback,
  signal?: AbortSignal,
): Promise<void> {
  const res = await authFetch(AGENT_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
    signal,
  });
  if (!res.ok) {
    const body = res.status === 409 ? await res.json().catch(() => undefined) : undefined;
    throw new AgentRequestError(res.status, `Agent request failed: ${res.status} ${res.statusText}`, body);
  }
  const reader = res.body?.getReader();
  if (!reader) throw new Error("No response body");

  const decoder = new TextDecoder();
  let buffer = "";
  // Emit a step, and if it carries a render manifest key (forwarded from the
  // Render Agent's output_ref), also emit a render event so the UI fetches
  // and renders the manifest.
  const handleStep = (step: StepEvent) => {
    onEvent({ type: "step", step });
    if (step.render_manifest_key) {
      onEvent({ type: "render", manifestKey: step.render_manifest_key, label: step.render_label, id: step.render_request_id });
    } else if (step.render_failed && step.render_request_id) {
      // Terminal: composing render produced nothing → drop the placeholder tab.
      onEvent({ type: "render_failed", id: step.render_request_id });
    } else if (step.render_request_id) {
      // Leading "Composing…" marker — open a per-tab placeholder, correlated to
      // the manifest by id.
      onEvent({ type: "render_pending", id: step.render_request_id, label: step.render_label });
    }
  };
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      const payload = trimmed.startsWith("data: ") ? trimmed.slice(6) : trimmed;
      const step = tryParseStep(payload);
      if (step) { handleStep(step); continue; }
      const summary = tryParseSummary(payload);
      if (summary) { onEvent(summaryEvent(summary)); continue; }
      const text = extractText(payload);
      if (text) onEvent({ type: "text", text });
    }
  }
  if (buffer.trim()) {
    const payload = buffer.trim().startsWith("data: ") ? buffer.trim().slice(6) : buffer.trim();
    const step = tryParseStep(payload);
    if (step) { handleStep(step); }
    else {
      const summary = tryParseSummary(payload);
      if (summary) { onEvent(summaryEvent(summary)); }
      else { const text = extractText(payload); if (text) onEvent({ type: "text", text }); }
    }
  }
}

/** Request for an owner-side Custom Agent test invoke (`POST /agent-test`). */
export interface AgentTestRequest {
  agentId: string;
  /** Which row to resolve. Defaults to `published` server-side. */
  source?: "draft" | "published";
  /** Explicit published version; ignored for drafts. */
  version?: number;
  prompt: string;
  sessionId?: string;
}

/**
 * Run one of the owner's own Custom Agents and stream the result.
 *
 * Deliberately carries NO config fields. The runtime resolves the agent's
 * persona / model / tools / skills server-side from `agentId` + `source`, which
 * is what makes the test faithful — the console cannot show a different
 * configuration than a real invocation would bind. The proxy stamps the actor
 * from the verified JWT, so an agent can only ever be tested by its owner.
 *
 * Reuses the same frame parsing as `invokeAgentStreaming` (the Task Agent emits
 * the same step / text / execution_summary shapes).
 */
export async function runAgentTestStreaming(
  request: AgentTestRequest,
  onEvent: StreamCallback,
  signal?: AbortSignal,
): Promise<void> {
  const res = await authFetch("/agent-test", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
    signal,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body?.error?.message ?? detail;
    } catch {
      /* non-JSON error body — fall back to statusText */
    }
    throw new Error(`Agent test failed: ${res.status} ${detail}`);
  }
  const reader = res.body?.getReader();
  if (!reader) throw new Error("No response body");

  const decoder = new TextDecoder();
  let buffer = "";
  const handleStep = (step: StepEvent) => {
    onEvent({ type: "step", step });
    if (step.render_manifest_key) {
      onEvent({ type: "render", manifestKey: step.render_manifest_key, label: step.render_label, id: step.render_request_id });
    } else if (step.render_failed && step.render_request_id) {
      // Terminal: composing render produced nothing → drop the placeholder tab.
      onEvent({ type: "render_failed", id: step.render_request_id });
    } else if (step.render_request_id) {
      // Leading "Composing…" marker — open a per-tab placeholder, correlated to
      // the manifest by id.
      onEvent({ type: "render_pending", id: step.render_request_id, label: step.render_label });
    }
  };
  const handlePayload = (payload: string) => {
    const step = tryParseStep(payload);
    if (step) { handleStep(step); return; }
    const summary = tryParseSummary(payload);
    if (summary) { onEvent(summaryEvent(summary)); return; }
    const text = extractText(payload);
    if (text) onEvent({ type: "text", text });
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      handlePayload(trimmed.startsWith("data: ") ? trimmed.slice(6) : trimmed);
    }
  }
  if (buffer.trim()) {
    const t = buffer.trim();
    handlePayload(t.startsWith("data: ") ? t.slice(6) : t);
  }
}

// Non-streaming invoke (used by task dispatch)
export async function invokeAgent(request: AgentRequest): Promise<AgentResponse> {
  const res = await authFetch(AGENT_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  if (!res.ok) throw new Error(`Agent request failed: ${res.status} ${res.statusText}`);
  const text = await res.text();
  const chunks: string[] = [];
  for (const line of text.split("\n")) {
    if (line.startsWith("data: ")) {
      const payload = line.slice(6);
      try {
        const parsed = JSON.parse(payload);
        if (typeof parsed === "string") chunks.push(parsed);
      } catch { chunks.push(payload); }
    }
  }
  return { response: chunks.join("") || text, session_id: request.session_id };
}

// --- Skills management API ---

async function skillsAction(payload: Record<string, unknown>): Promise<Record<string, unknown>> {
  const res = await authFetch(AGENT_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`Skills request failed: ${res.status} ${res.statusText}`);
  const text = await res.text();
  let parsed: Record<string, unknown> | undefined;
  for (const line of text.split("\n")) {
    if (line.startsWith("data: ")) {
      try {
        let value = JSON.parse(line.slice(6));
        if (typeof value === "string") { try { value = JSON.parse(value); } catch { /* single-encoded */ } }
        if (typeof value === "object" && value !== null) { parsed = value; break; }
      } catch { /* fall through */ }
    }
  }
  if (!parsed) { try { parsed = JSON.parse(text); } catch { parsed = { response: text }; } }
  if (!parsed) throw new Error("Could not parse response from agent");
  if (parsed.error) {
    // Runtime actions answer with {error: <code>, message: <human text>}. This
    // used to throw only the code, discarding the message -- so a registration
    // that failed for a specific, explainable reason surfaced in the UI as the
    // bare string "registration_failed", with the actual explanation dropped on
    // the floor. The gateway admin helpers further down this file already keep
    // both; this brings the shared helper in line.
    const code = String(parsed.error);
    const detail =
      typeof parsed.message === "string" && parsed.message.trim() !== ""
        ? parsed.message.trim()
        : "";
    const err = new Error(detail ? `${code}: ${detail}` : code) as Error & {
      code?: string;
      detail?: string;
    };
    err.code = code;
    if (detail) err.detail = detail;
    throw err;
  }
  return parsed;
}

export async function resolveGate(gateId: string, approved: boolean, feedback: string = ""): Promise<Record<string, unknown>> {
  return skillsAction({ action: "resolve_gate", gate_id: gateId, approved, feedback });
}

const skillsListCache = createAsyncCache<SkillInfo[]>(5 * 60_000);

export function peekSkills(): SkillInfo[] | undefined {
  return skillsListCache.peek();
}

/**
 * Built-in skills, as the RUNTIME sees them.
 *
 * The Skills panel no longer waits on this for its list — the catalog (name,
 * description) comes from the Capability API page the tab already fetched. This is
 * kept for what only the runtime can answer: which skills are loaded into the
 * current agent session (`activated`, `source`, `dirty`). Call it off the blocking
 * path and merge the result in.
 */
export async function listSkills(forceRefresh = false): Promise<SkillInfo[]> {
  return skillsListCache.get(async () => {
    const data = await skillsAction({ action: "list_skills" });
    return (data.skills as SkillInfo[]) ?? [];
  }, forceRefresh);
}

// `listBuiltInAgents` / `peekBuiltInAgents` are gone. The three sub-agent runtimes are
// static (a literal plus three env vars), so the Agents panel reads them out of the
// Capability API page it already has — see `toBuiltInAgents`. Fetching them here woke
// the agent runtime and paid a cold start to be told a constant. The runtime's
// `mcp_list_agents` action still exists for non-SPA callers.

export async function loadSkill(skill: { name: string; description: string; instructions: string; tools?: string[] }): Promise<Record<string, unknown>> {
  const result = await skillsAction({ action: "load_skill", ...skill });
  skillsListCache.invalidate();
  return result;
}

export async function unloadSkill(name: string): Promise<Record<string, unknown>> {
  const result = await skillsAction({ action: "unload_skill", name });
  skillsListCache.invalidate();
  return result;
}

export async function reloadSkills(): Promise<SkillInfo[]> {
  const data = await skillsAction({ action: "reload_skills" });
  const skills = (data.skills as SkillInfo[]) ?? [];
  skillsListCache.prime(skills);
  return skills;
}

// File upload (from remote-pdf-workflow branch)
export async function uploadFile(file: File, sessionId?: string): Promise<{ key: string }> {
  const buf = await file.arrayBuffer();
  const bytes = new Uint8Array(buf);
  let binary = "";
  const chunkSize = 8192;
  for (let i = 0; i < bytes.length; i += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize));
  }
  const b64 = btoa(binary);
  const data = await skillsAction({
    action: "upload_file",
    filename: file.name,
    data: b64,
    // Session-scopes the upload to artifacts/<session_id>/input/ (CloudFront-served).
    ...(sessionId ? { session_id: sessionId } : {}),
  });
  return { key: data.key as string };
}

export interface GeneratedSkill { name: string; description: string; instructions: string; tools: string[]; }

export async function generateSkill(description: string, availableTools: string[] = []): Promise<GeneratedSkill> {
  const data = await skillsAction({ action: "generate_skill", description, available_tools: availableTools });
  return { name: data.name as string, description: data.description as string, instructions: data.instructions as string, tools: (data.tools as string[]) ?? [] };
}

export interface SkillPackageInfo {
  name: string;
  description: string;
  instructions: string;
  skillMd: string;
  supportingFiles: string[];
  tools: string[];
  source?: SkillInfo['source'];
}

export async function getSkill(name: string): Promise<SkillPackageInfo> {
  const data = await skillsAction({ action: "get_skill", name });
  return {
    name: data.name as string,
    description: data.description as string,
    instructions: data.instructions as string,
    skillMd: (data.skillMd as string) ?? '',
    supportingFiles: (data.supportingFiles as string[]) ?? [],
    tools: (data.tools as string[]) ?? [],
    source: data.source as SkillInfo['source'],
  };
}

export async function saveSkill(name: string, target: "local" | "s3" | "both" = "local"): Promise<Record<string, unknown>> {
  const result = await skillsAction({ action: "save_skill", name, target });
  skillsListCache.invalidate();
  return result;
}

// --- MCP Gateway management API ---

export interface MCPToolInfo { name: string; description: string; state: "not_added" | "available" | "trusted"; }
export interface GatewayInfo { id: string; name: string; connected: boolean; tools: MCPToolInfo[]; search_available: boolean; tool_count: number; }
export interface GatewayListResult { gateways: GatewayInfo[]; builtinTools: MCPToolInfo[]; }

export async function mcpListAvailable(): Promise<GatewayListResult> {
  const data = await skillsAction({ action: "mcp_list_available" });
  return { gateways: (data.gateways as GatewayInfo[]) ?? [], builtinTools: (data.builtin_tools as MCPToolInfo[]) ?? [] };
}

export async function mcpConnectGateway(gatewayId: string): Promise<Record<string, unknown>> { return skillsAction({ action: "mcp_connect_gateway", gateway_id: gatewayId }); }
export async function mcpDisconnectGateway(gatewayId: string): Promise<Record<string, unknown>> { return skillsAction({ action: "mcp_disconnect_gateway", gateway_id: gatewayId }); }
export async function mcpRefreshTools(gatewayId: string): Promise<Record<string, unknown>> { return skillsAction({ action: "mcp_refresh_tools", gateway_id: gatewayId }); }
export async function mcpSearchTools(gatewayId: string, query: string): Promise<MCPToolInfo[]> { const data = await skillsAction({ action: "mcp_search_tools", gateway_id: gatewayId, query }); return (data.results as MCPToolInfo[]) ?? []; }
export async function mcpAddTool(name: string): Promise<{ name: string; state: string }> { const data = await skillsAction({ action: "mcp_add_tool", name }); return { name: data.name as string, state: data.state as string }; }
export async function mcpRemoveTool(name: string): Promise<{ name: string; state: string }> { const data = await skillsAction({ action: "mcp_remove_tool", name }); return { name: data.name as string, state: data.state as string }; }
export async function mcpTrustTool(name: string): Promise<{ name: string; state: string }> { const data = await skillsAction({ action: "mcp_trust_tool", name }); return { name: data.name as string, state: data.state as string }; }
export async function mcpUntrustTool(name: string): Promise<{ name: string; state: string }> { const data = await skillsAction({ action: "mcp_untrust_tool", name }); return { name: data.name as string, state: data.state as string }; }

export async function getAllToolNames(): Promise<string[]> {
  const data = await mcpListAvailable();
  const names: string[] = [];
  for (const t of data.builtinTools) names.push(t.name);
  for (const gw of data.gateways) for (const t of gw.tools) names.push(t.name);
  return names;
}

// --- Gateway Registry (admin) API ---
//
// These wrap the `gateway_*` runtime actions added in tasks 5.2–5.6. The
// registry is admin-managed, but `gateway_list` is authenticated-only — non-
// admins can read the current set for the consent UI (R8). Mutations (not
// implemented in this task) are gated server-side via `_is_admin`; the UI
// also gates the admin surface itself with `useIsGatewayAdmin()` so the
// menu and route disappear for non-admins (R3).
//
// The runtime returns a JSON array directly for `gateway_list` (one entry
// per enabled descriptor's `public_dict()`). `skillsAction` returns
// `Record<string, unknown>` but its parsing path accepts any JSON object
// including arrays — `typeof [] === "object"` — so the cast below is sound.

export type GatewayAuthType = "jwt" | "sigv4" | "none" | "bearer";

export interface GatewayJwtAuthConfig {
  auth_type: "jwt";
  token_endpoint: string;
  client_id: string;
  scope: string;
  // `client_secret_arn` is intentionally omitted: `public_dict()` strips it
  // for non-admin readers. Admins read it via the dedicated edit modal in
  // a separate task.
}

export interface GatewaySigV4AuthConfig {
  auth_type: "sigv4";
  region: string;
  service: string;
}

export interface GatewayNoneAuthConfig {
  auth_type: "none";
}

/**
 * Static-token auth for a user-registered remote MCP server.
 *
 * `header_name` / `value_prefix` are configurable so this one variant
 * covers both `Authorization: Bearer <token>` (the defaults) and a raw
 * API-key header such as `X-API-Key: <token>` (empty prefix).
 *
 * `token_arn` is intentionally omitted for the same reason as
 * `client_secret_arn`: `public_dict()` strips it.
 */
export interface GatewayBearerAuthConfig {
  auth_type: "bearer";
  header_name: string;
  value_prefix: string;
}

export type GatewayAuthConfig =
  | GatewayJwtAuthConfig
  | GatewaySigV4AuthConfig
  | GatewayNoneAuthConfig
  | GatewayBearerAuthConfig;

/**
 * Public view of one gateway-registry row. Mirrors the shape returned by
 * `GatewayDescriptor.public_dict()` in `agent/src/gateway_registry.py`:
 *
 *     { id, name, description, url, auth_type, auth_config, enabled,
 *       created_at?, updated_at?, created_by? }
 *
 * Timestamps are ISO-8601 strings stamped by the registry. `created_by` is
 * the Cognito sub of the admin who registered the gateway.
 */
export interface GatewayDescriptor {
  id: string;
  name: string;
  description: string;
  url: string;
  auth_type: GatewayAuthType;
  auth_config: GatewayAuthConfig;
  enabled: boolean;
  created_at?: string;
  updated_at?: string;
  created_by?: string;
}

/** Result of probing an MCP endpoint. See `probe_mcp_endpoint` in user_mcp.py. */
export interface McpProbeResult {
  ok: boolean;
  tools: string[];
  tool_count: number;
  /** Explanation on failure, or a caveat on success (e.g. server exposes no tools). */
  detail: string;
}

/** One registered MCP server, as returned by registration. */
export interface McpRegistration {
  target_name: string;
  target_id: string;
  /** `ready` once the gateway has synchronised; `synchronizing` can take minutes. */
  status: "ready" | "synchronizing" | "failed";
  server_name: string;
  endpoint: string;
  /** A shared target living on a gateway rather than a user registration. */
  platform?: boolean;
  /** Display name of the gateway a shared target came from. */
  gateway_name?: string;
  /**
   * AgentCore gateway id a shared target came from. Absent on the caller's own
   * registrations. Part of the row's identity — a target name is unique per
   * gateway, not across them.
   */
  gateway_id?: string;
  /**
   * Whether the caller may delete this server.
   *
   * `false` for a shared target on a gateway — removing it would take it away
   * from every actor, so only the caller's own registrations are theirs to
   * delete. Optional so a UI newer than the runtime keeps its previous
   * behaviour rather than hiding every Remove button; read it as
   * `removable !== false`.
   */
  removable?: boolean;
}

/** Servers plus the gateways that could not be enumerated. */
export interface McpServerListing {
  servers: McpRegistration[];
  /**
   * Gateways that could not be read, e.g. a cross-account gateway invisible to
   * this account's control plane. Load-bearing: without it a short list is
   * indistinguishable from "those gateways have no servers".
   *
   * The fast phase reports `["Platform gateway"]` because it asks no gateway
   * anything — the platform rows are pending, not absent.
   */
  unreadable: string[];
  /**
   * Fail-closed (`actor_unresolved`) or degraded (`gateway_unavailable`) reason.
   * `servers` is then empty or a safe subset, never unfiltered.
   */
  error?: string;
}

/**
 * Probe an MCP endpoint before registering it: `initialize` + `tools/list`.
 *
 * Never invokes a tool — that would cause side effects on the user's own
 * system. A failed probe is a normal result carried on `ok`/`detail`, not a
 * thrown error, so the form can render it inline.
 */
export async function testMcpServer(payload: {
  url: string;
  header_name?: string;
  /** Whole header value as the provider documents it; split server-side. */
  header_value?: string;
}): Promise<McpProbeResult> {
  const data = await skillsAction({ action: "user_mcp_test", ...payload });
  return data as unknown as McpProbeResult;
}

/**
 * Register an MCP server as a gateway target owned by the caller.
 *
 * The token is sent once and never comes back — it is stored in AgentCore
 * Identity, so nothing in this app can read it again.
 */
export async function registerMcpServer(payload: {
  name: string;
  url: string;
  header_name?: string;
  /** Whole header value as the provider documents it; split server-side. */
  header_value?: string;
  description?: string;
}): Promise<McpRegistration> {
  const data = await skillsAction({ action: "user_mcp_register", ...payload });
  return data as unknown as McpRegistration;
}

/**
 * MCP servers visible to the caller.
 *
 * Served by the proxy's `/mcp/servers` control-plane route — NOT the agent
 * runtime — so opening the Tools panel no longer pays an ~8s cold start
 * (backlog 2.2). Two phases:
 *
 *   - default (`live` omitted/false): the caller's registered servers from the
 *     ownership registry only. One DynamoDB read in the proxy; paints instantly.
 *   - `{ live: true }`: merges in the live gateway targets from the control
 *     plane — platform servers such as aws-mcp, plus live connection status
 *     (backlog 2.1). Slower, so the panel fires it asynchronously and never
 *     blocks the first paint on it.
 *
 * Fail-closed: when the proxy cannot resolve the actor it returns
 * `{ servers: [], error }` and NEVER an unfiltered list. Both `servers` and
 * `error` are surfaced so the panel can show empty + a message rather than
 * pretending success.
 */
export async function listMcpServers(
  opts: { live?: boolean } = {},
): Promise<McpServerListing> {
  const res = await authFetch(opts.live ? "/mcp/servers?live=1" : "/mcp/servers");
  if (!res.ok) {
    throw new Error(`MCP server listing failed: ${res.status} ${res.statusText}`);
  }
  const data = await res.json();
  const servers = (data?.servers as McpRegistration[]) ?? [];
  const unreadable = (data?.unreadable as string[]) ?? [];
  return {
    servers: Array.isArray(servers) ? servers : [],
    unreadable: Array.isArray(unreadable) ? unreadable : [],
    // Fail-closed and degraded results carry a reason. Surfaced, not swallowed:
    // an unresolved actor must read as "we could not attribute you", never as
    // "you have no servers".
    ...(typeof data?.error === "string" ? { error: data.error } : {}),
  };
}

/** Poll a registration until the gateway reports the target ready. */
export async function mcpServerStatus(
  targetId: string,
  targetName?: string,
): Promise<McpRegistration["status"]> {
  // Passing the name lets the runtime persist a resolved status, so a later
  // list does not show a settled server as still setting up.
  const data = await skillsAction({
    action: "user_mcp_status",
    target_id: targetId,
    ...(targetName ? { target_name: targetName } : {}),
  });
  return (data.status as McpRegistration["status"]) ?? "synchronizing";
}

/** Remove a server the caller owns. Ownership is re-checked server-side. */
export async function deleteMcpServer(targetName: string, targetId?: string): Promise<void> {
  await skillsAction({
    action: "user_mcp_delete",
    target_name: targetName,
    ...(targetId ? { target_id: targetId } : {}),
  });
}

/**
 * List enabled gateways via the `gateway_list` runtime action.
 *
 * Authenticated read; no admin gate. Returns the full descriptor minus the
 * secret ARN (`public_dict()`).
 */
export async function listGateways(): Promise<GatewayDescriptor[]> {
  const data = await skillsAction({ action: "gateway_list" });
  if (Array.isArray(data)) {
    return data as unknown as GatewayDescriptor[];
  }
  // Defensive: if the runtime ever wraps the array in `{ gateways: [...] }`
  // (it doesn't today), unwrap that shape too rather than silently dropping
  // results.
  if (Array.isArray((data as { gateways?: unknown }).gateways)) {
    return (data as { gateways: GatewayDescriptor[] }).gateways;
  }
  return [];
}

// --- Gateway registration ---
//
// The runtime returns either a public descriptor on success or
// ``{"error": "<code>", "message"?: "..."}`` on a structured failure
// (admin_required, duplicate_id, invalid_payload, actor_unresolved). The
// shared `skillsAction` helper throws on any `error` key, which collapses
// these into a single `Error` and loses the discriminator the modal needs
// for inline messaging. We bypass it here with a thin parser that
// preserves the raw response shape.

/** Stable error codes the modal renders inline. */
export type RegisterGatewayErrorCode =
  | "admin_required"
  | "duplicate_id"
  | "invalid_payload"
  | "actor_unresolved"
  | "gateway_registry_unavailable"
  | "unknown";

export type RegisterGatewayResult =
  | { ok: true; gateway: GatewayDescriptor }
  | { ok: false; error: RegisterGatewayErrorCode; message?: string };

/**
 * Shape of the JWT auth fields collected by the registration modal. The
 * runtime separates `client_secret` from the durable `auth_config`: the
 * raw secret is written to Secrets Manager and the resulting ARN replaces
 * it on the row (R2). The modal forwards the secret here exactly once and
 * never echoes it back into UI state after submission.
 */
export interface RegisterGatewayJwtAuth {
  auth_type: "jwt";
  token_endpoint: string;
  client_id: string;
  client_secret: string;
  scope: string;
}

export interface RegisterGatewaySigV4Auth {
  auth_type: "sigv4";
  region: string;
  service: string;
}

export interface RegisterGatewayNoneAuth {
  auth_type: "none";
}

export type RegisterGatewayAuth =
  | RegisterGatewayJwtAuth
  | RegisterGatewaySigV4Auth
  | RegisterGatewayNoneAuth;

export interface RegisterGatewayPayload {
  id: string;
  name: string;
  description: string;
  url: string;
  auth_type: GatewayAuthType;
  auth_config: RegisterGatewayAuth;
  enabled?: boolean;
}

/**
 * Parse a `data: ...` SSE line back into its JSON object. Mirrors the
 * single-encoded-string handling in `skillsAction`.
 */
function parseSseObject(text: string): Record<string, unknown> | undefined {
  for (const line of text.split("\n")) {
    if (!line.startsWith("data: ")) continue;
    try {
      let value: unknown = JSON.parse(line.slice(6));
      if (typeof value === "string") {
        try { value = JSON.parse(value); } catch { /* keep the raw string */ }
      }
      if (value !== null && typeof value === "object") {
        return value as Record<string, unknown>;
      }
    } catch { /* not JSON, try next line */ }
  }
  try {
    const parsed = JSON.parse(text);
    if (parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
  } catch { /* fall through */ }
  return undefined;
}

/** Whitelist of error codes the runtime is documented to return. */
const KNOWN_REGISTER_ERRORS: ReadonlyArray<RegisterGatewayErrorCode> = [
  "admin_required",
  "duplicate_id",
  "invalid_payload",
  "actor_unresolved",
  "gateway_registry_unavailable",
];

function coerceErrorCode(raw: unknown): RegisterGatewayErrorCode {
  if (typeof raw !== "string") return "unknown";
  return (KNOWN_REGISTER_ERRORS as ReadonlyArray<string>).includes(raw)
    ? (raw as RegisterGatewayErrorCode)
    : "unknown";
}

/** Whitelist of error codes the update/delete runtime actions can return. */
const KNOWN_MUTATE_ERRORS: ReadonlyArray<MutateGatewayErrorCode> = [
  "admin_required",
  "invalid_payload",
  "not_found",
  "actor_unresolved",
  "gateway_registry_unavailable",
];

function coerceMutateErrorCode(raw: unknown): MutateGatewayErrorCode {
  if (typeof raw !== "string") return "unknown";
  return (KNOWN_MUTATE_ERRORS as ReadonlyArray<string>).includes(raw)
    ? (raw as MutateGatewayErrorCode)
    : "unknown";
}

/**
 * Register a new gateway via the `gateway_register` runtime action.
 *
 * Returns a discriminated result so the modal can render the runtime's
 * structured errors (`admin_required`, `duplicate_id`, `invalid_payload`,
 * etc.) inline rather than as a generic "Something went wrong". Network
 * and HTTP errors still throw — callers should wrap the call in a
 * try/catch and surface the message in the same error pane.
 *
 * The payload's `client_secret` (when present) is forwarded verbatim once
 * and never persisted in `api.ts`. The modal is responsible for clearing
 * it from React state after this call resolves (R2).
 */
export async function registerGateway(
  payload: RegisterGatewayPayload,
): Promise<RegisterGatewayResult> {
  const res = await authFetch(AGENT_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "gateway_register", ...payload }),
  });
  if (!res.ok) {
    throw new Error(`gateway_register failed: ${res.status} ${res.statusText}`);
  }
  const text = await res.text();
  const parsed = parseSseObject(text);
  if (!parsed) {
    throw new Error("Could not parse response from agent");
  }
  if (parsed.error !== undefined) {
    return {
      ok: false,
      error: coerceErrorCode(parsed.error),
      message: typeof parsed.message === "string" ? parsed.message : undefined,
    };
  }
  return { ok: true, gateway: parsed as unknown as GatewayDescriptor };
}

// --- Gateway update / delete ---
//
// Both wrap admin-only `gateway_update` / `gateway_delete` runtime actions
// and use the same discriminated result shape as `registerGateway` so the
// list view can render structured runtime errors (`admin_required`,
// `not_found`, `invalid_payload`) inline next to the row that triggered
// the action. The list view (task 8.4) calls these from the row-action
// handlers (edit, disable/enable toggle, delete).

/** Stable error codes the update/delete actions can return. */
export type MutateGatewayErrorCode =
  | "admin_required"
  | "invalid_payload"
  | "not_found"
  | "actor_unresolved"
  | "gateway_registry_unavailable"
  | "unknown";

export type UpdateGatewayResult =
  | { ok: true; gateway: GatewayDescriptor }
  | { ok: false; error: MutateGatewayErrorCode; message?: string };

export type DeleteGatewayResult =
  | { ok: true; id: string }
  | { ok: false; error: MutateGatewayErrorCode; message?: string };

/**
 * Patch shape for `gateway_update`. All fields are optional — the runtime
 * patches only the keys present in the payload and preserves the rest. In
 * particular, leaving `auth_config.client_secret` off a JWT patch is the
 * documented way to keep the existing `client_secret_arn` (the registry's
 * update-time secret-preservation contract); callers that want to rotate
 * the secret include `client_secret` to write a fresh value.
 */
export interface UpdateGatewayPayload {
  name?: string;
  description?: string;
  url?: string;
  auth_type?: GatewayAuthType;
  auth_config?: RegisterGatewayAuth;
  enabled?: boolean;
}

/**
 * Update a gateway row via the `gateway_update` runtime action.
 *
 * The list view uses this for both edit-mode form submissions and the
 * inline enabled/disabled toggle. Returns a discriminated result so the
 * caller can render `admin_required` / `not_found` / `invalid_payload`
 * inline rather than collapsing everything to a generic error string.
 */
export async function updateGateway(
  id: string,
  patch: UpdateGatewayPayload,
): Promise<UpdateGatewayResult> {
  const res = await authFetch(AGENT_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "gateway_update", id, ...patch }),
  });
  if (!res.ok) {
    throw new Error(`gateway_update failed: ${res.status} ${res.statusText}`);
  }
  const text = await res.text();
  const parsed = parseSseObject(text);
  if (!parsed) {
    throw new Error("Could not parse response from agent");
  }
  if (parsed.error !== undefined) {
    return {
      ok: false,
      error: coerceMutateErrorCode(parsed.error),
      message: typeof parsed.message === "string" ? parsed.message : undefined,
    };
  }
  return { ok: true, gateway: parsed as unknown as GatewayDescriptor };
}

/**
 * Soft-delete a gateway via the `gateway_delete` runtime action.
 *
 * The runtime preserves the DDB row (audit trail) and flips
 * ``enabled = false``; on success the response is
 * ``{ status: "disabled", id }``. We collapse that into a stable
 * ``{ ok: true, id }`` shape so callers don't have to special-case the
 * status string.
 */
export async function deleteGateway(id: string): Promise<DeleteGatewayResult> {
  const res = await authFetch(AGENT_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "gateway_delete", id }),
  });
  if (!res.ok) {
    throw new Error(`gateway_delete failed: ${res.status} ${res.statusText}`);
  }
  const text = await res.text();
  const parsed = parseSseObject(text);
  if (!parsed) {
    throw new Error("Could not parse response from agent");
  }
  if (parsed.error !== undefined) {
    return {
      ok: false,
      error: coerceMutateErrorCode(parsed.error),
      message: typeof parsed.message === "string" ? parsed.message : undefined,
    };
  }
  const responseId = typeof parsed.id === "string" ? parsed.id : id;
  return { ok: true, id: responseId };
}

// --- Gateway test-connection ---
//
// Wraps the admin-only `gateway_test_connection` runtime action. Unlike the
// register/update/delete actions above (which use a structured `{error}`
// envelope), the runtime here returns an `ok` flag alongside the payload:
//
//   - success:           { ok: true, tool_count, sample_tool_names }
//   - probe failure:     { ok: false, error: "<message>" }
//
// On admin-gate / validation failures the runtime drops the `ok` key and
// falls back to the same `{error, message?}` shape the other actions use
// (`admin_required`, `invalid_payload`, `not_found`). We normalize all of
// those into a single discriminated `TestGatewayConnectionResult` so the
// list view's row handler can render any failure inline with the same
// code path.

export type TestGatewayConnectionErrorCode =
  | "admin_required"
  | "invalid_payload"
  | "not_found"
  | "actor_unresolved"
  | "gateway_registry_unavailable"
  | "registration_failed"
  | "probe_failed"
  | "unknown";

export type TestGatewayConnectionResult =
  | {
      ok: true;
      tool_count: number;
      sample_tool_names: string[];
    }
  | {
      ok: false;
      error: TestGatewayConnectionErrorCode;
      message?: string;
    };

/** Whitelist of error codes that arrive via the `{error}` envelope. */
const KNOWN_TEST_ERROR_CODES: ReadonlyArray<TestGatewayConnectionErrorCode> = [
  "admin_required",
  "invalid_payload",
  "not_found",
  "actor_unresolved",
  "gateway_registry_unavailable",
  "registration_failed",
];

function coerceTestErrorCode(raw: unknown): TestGatewayConnectionErrorCode {
  if (typeof raw !== "string") return "unknown";
  return (KNOWN_TEST_ERROR_CODES as ReadonlyArray<string>).includes(raw)
    ? (raw as TestGatewayConnectionErrorCode)
    : "unknown";
}

/**
 * Probe a registered gateway via the `gateway_test_connection` runtime
 * action.
 *
 * Live-registers the gateway transparently if it isn't already in
 * `MCPManager._gateways`, refreshes the live `tools/list`, and returns
 * the resulting tool count plus a sample of names. The list view renders
 * the count (and sample-names tooltip) inline on the row, replacing the
 * "Last connected" column for that row.
 *
 * The result is transient — the caller does not refresh the gateway
 * list, it just updates the row's status display.
 */
export async function testGatewayConnection(
  id: string,
): Promise<TestGatewayConnectionResult> {
  const res = await authFetch(AGENT_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "gateway_test_connection", id }),
  });
  if (!res.ok) {
    throw new Error(
      `gateway_test_connection failed: ${res.status} ${res.statusText}`,
    );
  }
  const text = await res.text();
  const parsed = parseSseObject(text);
  if (!parsed) {
    throw new Error("Could not parse response from agent");
  }

  // Admin gate / validation: runtime returns `{error, message?}` with no
  // `ok` flag for `admin_required`, `invalid_payload`, and `not_found`.
  // Surface those with a stable code so the row handler can branch.
  if (parsed.ok === undefined && parsed.error !== undefined) {
    return {
      ok: false,
      error: coerceTestErrorCode(parsed.error),
      message: typeof parsed.message === "string" ? parsed.message : undefined,
    };
  }

  if (parsed.ok === true) {
    const toolCount =
      typeof parsed.tool_count === "number" ? parsed.tool_count : 0;
    const sampleToolNames = Array.isArray(parsed.sample_tool_names)
      ? (parsed.sample_tool_names as unknown[]).filter(
          (n): n is string => typeof n === "string",
        )
      : [];
    return { ok: true, tool_count: toolCount, sample_tool_names: sampleToolNames };
  }

  // Probe failure: the runtime caught an exception or the live refresh
  // surfaced an error. The `error` field is a free-form string here, so
  // we stash it under the `probe_failed` code with the original text in
  // `message` for inline display.
  if (parsed.ok === false) {
    const rawError = typeof parsed.error === "string" ? parsed.error : undefined;
    return {
      ok: false,
      error: "probe_failed",
      message: rawError,
    };
  }

  return {
    ok: false,
    error: "unknown",
    message: "Unexpected response shape from gateway_test_connection",
  };
}

// --- Tool Catalog ---
export interface CatalogTool { name: string; description?: string; inputSchema?: Record<string, unknown>; }
export interface CatalogSection { label: string; tools: CatalogTool[]; }

const toolCatalogCache = createAsyncCache<CatalogSection[]>(10 * 60_000);

export function peekCatalog(): CatalogSection[] | undefined {
  return toolCatalogCache.peek();
}

export async function fetchCatalog(forceRefresh = false): Promise<CatalogSection[]> {
  return toolCatalogCache.get(async () => {
    const res = await authFetch(forceRefresh ? "/catalog?refresh=1" : "/catalog");
    if (!res.ok) throw new Error(`Catalog request failed: ${res.status} ${res.statusText}`);
    const data = await res.json();
    return (data.sections as CatalogSection[]) ?? [];
  }, forceRefresh);
}

export function refreshCatalog(): Promise<CatalogSection[]> {
  return fetchCatalog(true);
}

// --- Models ---
export interface ModelInfo { id: string; name: string; supportsEffort?: boolean; }
export interface ModelCatalog {
  models: ModelInfo[];
  /**
   * The platform's configured orchestrator model, reported by the proxy.
   *
   * Exists so a caller never has to invent a default from list position:
   * `ListInferenceProfiles` returns Claude 3 Haiku first, whose 4096 output cap
   * makes every chat turn fail against the harness's 16384 request. Absent only
   * on an older proxy, in which case the caller should leave the selection alone
   * rather than guessing.
   */
  defaultModelId?: string;
}

export async function fetchModelCatalog(): Promise<ModelCatalog> {
  const res = await authFetch("/models");
  if (!res.ok) throw new Error(`Models request failed: ${res.status} ${res.statusText}`);
  const data = await res.json();
  return {
    models: (data.models as ModelInfo[]) ?? [],
    defaultModelId: typeof data.defaultModelId === "string" ? data.defaultModelId : undefined,
  };
}

/** The model list alone. Six call sites only need this shape. */
export async function fetchModels(): Promise<ModelInfo[]> {
  return (await fetchModelCatalog()).models;
}
export async function setAgentModel(modelId: string, effort?: 'low' | 'medium' | 'high'): Promise<{ model_id: string }> {
  const data = await skillsAction({
    action: "mcp_set_model",
    model_id: modelId,
    ...(effort ? { effort } : {}),
  });
  return { model_id: data.model_id as string };
}


/**
 * The orchestrator model this client should ask for, or undefined to let the
 * agent use its default. Read at send time so a change applies to the next turn
 * without needing to reach into a running container.
 */
export function selectedOrchestratorModelId(): string | undefined {
  try {
    return localStorage.getItem('lab-orchestrator-model-id') || undefined;
  } catch {
    return undefined;
  }
}

/**
 * The reasoning-effort cap this client should ask for, or undefined for Default
 * (let the runtime apply its env BEDROCK_REASONING_EFFORT). Mirrors the model_id
 * mechanism: a global localStorage key written by the effort slider. Returns
 * undefined for '' / absent / any value outside the enum. The caller may send
 * this unconditionally — the backend only attaches it to a capable model, so a
 * stale value (slider left set while an incapable model is picked) never 400s.
 */
export function selectedReasoningEffort(): 'low' | 'medium' | 'high' | undefined {
  try {
    const v = localStorage.getItem('lab-orchestrator-model-effort') || '';
    return v === 'low' || v === 'medium' || v === 'high' ? v : undefined;
  } catch {
    return undefined;
  }
}
