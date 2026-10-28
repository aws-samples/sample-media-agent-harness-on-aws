/**
 * Capability API client — adapters, grouping, and safe defaults.
 *
 * The behaviours worth pinning here are the ones whose failure is silent:
 *
 *  - flag fetch failures must resolve to all-`legacy`, because the alternative
 *    is a blank Capabilities tab when the control plane is briefly unreachable;
 *  - Gateway grouping must NOT deduplicate repeated bare tool names, or the UI
 *    loses the identity a runtime actually invokes by (R10);
 *  - the DTO→view adapters must not fabricate fields (an invented `current`
 *    flag would mislabel which version is live).
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import {
  fetchCapabilityConfig,
  groupGatewayTools,
  toDraftMetadata,
  toPublishedSummaries,
  type CapabilitySummary,
  type CategoryPage,
} from './capability-api';

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function summary(overrides: Partial<CapabilitySummary> = {}): CapabilitySummary {
  return {
    id: 'skill:asset-1:v1',
    category: 'skill',
    capabilityClass: 'asset',
    displayName: 'Skill One',
    scope: 'org',
    status: 'published',
    source: 'asset-index',
    assetId: 'asset-1',
    version: 1,
    updatedAt: '2026-08-13T12:00:00Z',
    ...overrides,
  };
}

function page(items: CapabilitySummary[]): CategoryPage {
  return { category: 'skill', items };
}

describe('fetchCapabilityConfig', () => {
  it('returns the served flags', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          deployEnv: 'sandbox',
          capabilitySources: { skill: 'registry' },
          registryConfigured: true,
        }),
      }),
    );
    const config = await fetchCapabilityConfig();
    expect(config.capabilitySources.skill).toBe('registry');
  });

  it('falls back to all-legacy when the control plane is unreachable', async () => {
    // The safe answer is "keep using the path that works" — NOT an empty tab.
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('network down')));
    const config = await fetchCapabilityConfig();
    expect(config.capabilitySources).toEqual({});
    expect(config.registryConfigured).toBe(false);
  });

  it('falls back to all-legacy on a non-2xx response', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 503 }));
    const config = await fetchCapabilityConfig();
    expect(config.capabilitySources).toEqual({});
  });

  it('does not send a token to the unauthenticated config route', async () => {
    // Uses bare `fetch`, not `authFetch`: the route needs no identity, and a
    // token fetch would fail before the app has signed in.
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ deployEnv: 'x', capabilitySources: {}, registryConfigured: false }),
    });
    vi.stubGlobal('fetch', fetchMock);
    await fetchCapabilityConfig();
    const [, init] = fetchMock.mock.calls[0];
    expect(init?.headers?.Authorization).toBeUndefined();
  });
});

describe('toDraftMetadata', () => {
  it('keeps working copies and drops immutable versions', () => {
    const rows = toDraftMetadata(
      page([
        summary({ id: 'skill:asset-1', status: 'draft', version: undefined }),
        summary({ id: 'skill:asset-2:v1', status: 'published', assetId: 'asset-2' }),
      ]),
    );
    expect(rows.map((r) => r.assetId)).toEqual(['asset-1']);
  });

  it('does not fabricate an S3 prefix', () => {
    // Canonical package paths are deliberately not exposed to the browser (R4),
    // so an empty string is the honest answer rather than a guessed path.
    const rows = toDraftMetadata(
      page([summary({ status: 'draft', version: undefined })]),
    );
    expect(rows[0].s3Prefix).toBe('');
  });

  it('converts the ISO timestamp to epoch ms', () => {
    const rows = toDraftMetadata(
      page([summary({ status: 'draft', version: undefined })]),
    );
    expect(rows[0].updatedAt).toBe(Date.parse('2026-08-13T12:00:00Z'));
  });

  it('tolerates a missing timestamp', () => {
    const rows = toDraftMetadata(
      page([summary({ status: 'draft', version: undefined, updatedAt: undefined })]),
    );
    expect(rows[0].updatedAt).toBe(0);
  });
});

describe('description passthrough', () => {
  // The whole reason the server DTO grew the field: the inventory rows render it.
  // Both mappers must carry it, and must NOT substitute '' when it is absent —
  // `undefined` is "this source cannot say", which the row renders as no line at
  // all rather than as "the author wrote nothing".
  it('carries a draft description through to the drafts list', () => {
    const [draft] = toDraftMetadata(
      page([summary({ status: 'draft', version: undefined, description: 'Turn a script into a storyboard.' })]),
    );
    expect(draft.description).toBe('Turn a script into a storyboard.');
  });

  it('carries a published description through to the PUBLISHED section', () => {
    const [published] = toPublishedSummaries(page([summary({ description: 'Screen a cut.' })]));
    expect(published.description).toBe('Screen a cut.');
  });

  it('leaves it undefined — never empty — when the source omits it', () => {
    const [draft] = toDraftMetadata(page([summary({ status: 'draft', version: undefined })]));
    const [published] = toPublishedSummaries(page([summary()]));
    expect(draft.description).toBeUndefined();
    expect(published.description).toBeUndefined();
  });
});

describe('toPublishedSummaries', () => {
  it('keeps only numbered versions', () => {
    const summaries = toPublishedSummaries(
      page([
        summary({ version: 2 }),
        summary({ id: 'skill:asset-3', version: undefined, status: 'draft', assetId: 'asset-3' }),
      ]),
    );
    expect(summaries.map((s) => s.version)).toEqual([2]);
  });

  it('maps harness-shared scope back to org', () => {
    const summaries = toPublishedSummaries(
      page([summary({ source: 'agent-registry', scope: 'harness-shared' })]),
    );
    expect(summaries[0].scope).toBe('org');
  });

  it('does not claim a version is current', () => {
    // `current` is canonical lifecycle state the summary does not carry.
    // Under-claiming is safe; mislabelling which version is live is not.
    const summaries = toPublishedSummaries(page([summary({ version: 3 })]));
    expect(summaries[0].current).toBe(false);
  });
});

describe('groupGatewayTools', () => {
  const gatewayTool = (
    targetId: string,
    targetName: string,
    bare: string,
  ): CapabilitySummary => ({
    id: `tool:gw-1:${targetId}:${bare}`,
    category: 'tool',
    capabilityClass: 'gateway-mcp',
    displayName: bare,
    scope: 'harness-shared',
    status: 'published',
    source: 'gateway',
    gatewayId: 'gw-1',
    targetId,
    targetName,
    bareToolName: bare,
    qualifiedToolName: `${targetName}___${bare}`,
  });

  it('groups Gateway -> Target -> Tool', () => {
    const grouped = groupGatewayTools([
      gatewayTool('t1', 'media-processing', 'process_media'),
      gatewayTool('t1', 'media-processing', 'get_job_status'),
      gatewayTool('t2', 'speech-diarization', 'diarize_speech'),
    ]);
    expect(grouped).toHaveLength(1);
    expect(grouped[0].gatewayId).toBe('gw-1');
    expect(grouped[0].targets.map((t) => t.targetName)).toEqual([
      'media-processing',
      'speech-diarization',
    ]);
  });

  it('never collapses a bare tool name that repeats across targets', () => {
    // Three targets legitimately expose get_job_status. Collapsing them would
    // leave the UI unable to say which target to call.
    const grouped = groupGatewayTools([
      gatewayTool('t1', 'media-processing', 'get_job_status'),
      gatewayTool('t2', 'speech-diarization', 'get_job_status'),
      gatewayTool('t3', 'audiostem-separation', 'get_job_status'),
    ]);
    expect(grouped[0].targets).toHaveLength(3);
    const qualified = grouped[0].targets.flatMap((t) =>
      t.tools.map((tool) => tool.qualifiedToolName),
    );
    expect(qualified.sort()).toEqual([
      'audiostem-separation___get_job_status',
      'media-processing___get_job_status',
      'speech-diarization___get_job_status',
    ]);
  });

  it('keys targets on id so two targets can share a display name', () => {
    const grouped = groupGatewayTools([
      gatewayTool('t1', 'shared-name', 'a'),
      gatewayTool('t2', 'shared-name', 'b'),
    ]);
    expect(grouped[0].targets).toHaveLength(2);
  });

  it('ignores non-gateway capabilities', () => {
    expect(groupGatewayTools([summary()])).toEqual([]);
  });
});

describe('built-in agents must not reach the owner-asset lists', () => {
  // The Capability API returns the four platform runtimes (Task / Planning /
  // Design / Render Agent) alongside the owner's own agents, from the `builtin`
  // source. They have no AssetIndex row, so every mutation addressed to them
  // fails — and on a published row the Agents tab now offers **Delete**, which
  // is a purge. A built-in leaking into that list is a dead destructive button,
  // the exact bug already fixed once for the core Task Agent.
  //
  // They are excluded today only because `sources.py` declares `version` as the
  // STRING "1" while `toPublishedSummaries` filters `typeof === 'number'`. That
  // is an implicit dependency between two files in different languages, so it
  // gets a test rather than a comment.
  const builtinAgent = (slug: string, name: string): CapabilitySummary => ({
    id: `builtin:specialty-agent:${slug}`,
    category: 'specialty-agent',
    capabilityClass: 'builtin',
    displayName: name,
    scope: 'harness-shared',
    status: 'published',
    source: 'builtin',
    assetId: slug,
    // Verbatim from `BUILTIN_CAPABILITIES` — a string, not a number.
    version: '1',
  });

  const builtins = [
    builtinAgent('task-agent', 'Task Agent'),
    builtinAgent('planning-agent', 'Planning Agent'),
    builtinAgent('render-agent', 'Render Agent'),
  ];

  it('excludes them from the published list', () => {
    const mine = summary({ category: 'specialty-agent', assetId: 'mine-1', version: 1 });
    const rows = toPublishedSummaries({
      category: 'specialty-agent',
      items: [...builtins, mine],
    });
    expect(rows.map((r) => r.assetId)).toEqual(['mine-1']);
  });

  it('excludes them from the drafts list too', () => {
    // They are `published`, so the status filter also holds them back. Both
    // gates matter: either one alone would be a single point of failure.
    const rows = toDraftMetadata({ category: 'specialty-agent', items: builtins });
    expect(rows).toEqual([]);
  });

  it('would leak if a built-in version ever became a number', () => {
    // Documents the coupling explicitly. If `sources.py` starts emitting
    // `"version": 1`, this flips and the built-in runtimes appear as owner-owned
    // published agents, each with a Delete button wired to purge.
    const asNumber = builtins.map((b) => ({ ...b, version: 1 }));
    const leaked = toPublishedSummaries({ category: 'specialty-agent', items: asNumber });
    expect(leaked).toHaveLength(builtins.length);
    // So the filter must be tightened to exclude `source === 'builtin'` before
    // that change can be made safely.
    const safe = asNumber.filter((i) => i.source !== 'builtin');
    expect(toPublishedSummaries({ category: 'specialty-agent', items: safe })).toEqual([]);
  });
});
