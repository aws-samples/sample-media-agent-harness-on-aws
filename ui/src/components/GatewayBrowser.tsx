import React, { useState, useEffect, useRef, useCallback } from 'react';
import ClampedText from './ui/ClampedText';
import {
  RefreshButton,
  SectionHeader,
  groupColour,
} from './settings/panelChrome';
import type { GatewayToolDescriptor } from '../lab-api';
import {
  listGatewayCatalog,
  loadLiveGatewayCatalog,
  peekGatewayTools,
  refreshGatewayTools,
} from '../lab-api';
import { theme, tint } from '../styles/tokens';

/**
 * Filters a list of GatewayToolDescriptors by case-insensitive substring match
 * on name or description. Returns all tools if query is empty.
 * Exported for testability (Property 8: Gateway tool filter correctness).
 */
export function filterGatewayTools(tools: GatewayToolDescriptor[], query: string): GatewayToolDescriptor[] {
  if (!query.trim()) return tools;
  const lowerQuery = query.toLowerCase();
  return tools.filter(
    tool =>
      tool.name.toLowerCase().includes(lowerQuery) ||
      tool.description.toLowerCase().includes(lowerQuery)
  );
}

/**
 * Extracts a short display name from a full Gateway tool name.
 * Gateway tools have format: "prefix___server___tool_name" — returns just the last part.
 */
/**
 * Group by PROVENANCE, not by role.
 *
 * An earlier version bucketed built-ins by role (Sandbox & IO, Orchestration,
 * Lab authoring...) from a hardcoded name table. That rots: a new built-in falls
 * into "Other", and a tool the USER creates has no place in the taxonomy at all.
 *
 * Provenance is intrinsic and self-extending. Federated tools already encode
 * their target as `target___tool` — AgentCore's own convention, not a table
 * maintained here — so splitting a gateway by target costs nothing and keeps
 * working for gateways nobody has registered yet.
 *
 * The rule: group on data the system already carries, never on a list we curate.
 */
function groupKeyFor(source: string, toolName: string): string {
  // `x_amz_...search` is synthesized BY a gateway, so it belongs to that
  // gateway's group. Keying it on its own name instead would merge the search
  // tools of two different gateways into one indistinguishable group.
  if (toolName.includes('___')) return `${source} · ${toolName.split('___')[0]}`;
  return source;
}

function shortToolName(fullName: string): string {
  const parts = fullName.split('___');
  return parts.length > 1 ? parts[parts.length - 1] : fullName;
}

/**
 * GatewayBrowser — Sub-view that lists tools available on the AgentCore Gateway.
 * Provides text filtering with 300ms debounce, refresh, and per-tool test invocation.
 *
 * When mounted inside another view that already has a search box (e.g. the
 * Capabilities tab), pass that view's search query as `externalSearch`. The
 * browser will use it as the active filter and hide its own input so the
 * user only sees one search field.
 */
export default function GatewayBrowser({ externalSearch }: { externalSearch?: string } = {}) {
  const cachedTools = useRef(peekGatewayTools()).current;
  const [tools, setTools] = useState<GatewayToolDescriptor[]>(cachedTools ?? []);
  const [loaded, setLoaded] = useState(cachedTools !== undefined);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filterInput, setFilterInput] = useState('');
  const [debouncedFilter, setDebouncedFilter] = useState('');
  const [collapsedGroups, setCollapsedGroups] = useState<Record<string, boolean>>({});
  // Gateways the fast path could not read, and whether the data-plane pass that
  // completes them is still running. Three distinct states, deliberately:
  // pending (we are fetching), failed (we tried and could not), and neither.
  // Collapsing them would let an unreachable gateway read as "no tools".
  const [pendingGateways, setPendingGateways] = useState<string[]>([]);
  const [completing, setCompleting] = useState(false);

  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const usingExternalSearch = externalSearch !== undefined;

  // Fetch tools on mount
  useEffect(() => {
    fetchTools();
  }, []);

  // Debounce filter input at 300ms (only when using the local input — the
  // parent search query is already debounced upstream).
  useEffect(() => {
    if (usingExternalSearch) return;
    if (debounceRef.current) {
      clearTimeout(debounceRef.current);
    }
    debounceRef.current = setTimeout(() => {
      setDebouncedFilter(filterInput);
    }, 300);
    return () => {
      if (debounceRef.current) {
        clearTimeout(debounceRef.current);
      }
    };
  }, [filterInput, usingExternalSearch]);

  const activeFilter = usingExternalSearch ? (externalSearch ?? '') : debouncedFilter;

  const toggleGroup = (source: string) => {
    setCollapsedGroups(prev => ({ ...prev, [source]: !prev[source] }));
  };

  /**
   * Complete the catalog from the data plane, in the background.
   *
   * Only reached when the fast path reported gateways it could not describe —
   * cross-account ones, or MCP-server targets whose capabilities exist only in the
   * gateway's synchronized catalog. This is the call that can cold-start the agent
   * runtime, which is exactly why it runs after the paint rather than before it.
   *
   * A failure leaves the tools already on screen alone and keeps the gateway names
   * listed as unavailable. Showing fewer tools than we have would be worse than
   * showing them with a caveat.
   */
  const completeFromDataPlane = useCallback(async () => {
    setCompleting(true);
    try {
      const complete = await loadLiveGatewayCatalog();
      setTools(complete.tools);
      setPendingGateways(complete.unreadable);
    } catch {
      // Deliberately not `setError`: the list is usable, just short. The pending
      // names stay so the panel can say which gateways are missing.
    } finally {
      setCompleting(false);
    }
  }, []);

  const fetchTools = async () => {
    setLoading(true);
    setError(null);
    try {
      // Phase 1: DynamoDB + control plane. No runtime, so this paints immediately.
      const result = await listGatewayCatalog();
      setTools(result.tools);
      setPendingGateways(result.unreadable);
      setLoaded(true);
      // Phase 2, only if something is actually missing.
      if (result.unreadable.length > 0) void completeFromDataPlane();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load gateway tools');
    } finally {
      setLoading(false);
    }
  };

  /**
   * Explicit Refresh goes straight to the data plane.
   *
   * Someone pressing Refresh wants the complete, current list and is willing to
   * wait; the fast path would hand back the same partial answer they are trying to
   * get past.
   */
  const handleRefresh = async () => {
    setLoading(true);
    setError(null);
    try {
      const complete = await loadLiveGatewayCatalog();
      setTools(complete.tools);
      setPendingGateways(complete.unreadable);
      setLoaded(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to refresh gateway tools');
    } finally {
      setLoading(false);
    }
  };

  const handleFilterChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setFilterInput(e.target.value);
  };

  // Filter tools using the exported utility function
  const filteredTools = filterGatewayTools(tools, activeFilter);


  // Empty state: API returned zero tools.
  //
  // Suppressed while a gateway is still outstanding. "No tools are available" is a
  // claim about the harness; with a data-plane pass in flight the honest answer is
  // "not yet", and the notice below says so.
  if (loaded && !loading && !error && tools.length === 0
      && !completing && pendingGateways.length === 0) {
    return (
      <div style={containerStyle}>
        <div style={headerStyle}>
          <span style={titleStyle}>MCP Tools</span>
          <RefreshButton onClick={handleRefresh} loading={loading} ariaLabel="Refresh gateway tools" />
        </div>
        <div style={emptyStateStyle}>No tools are available on the Gateway.</div>
      </div>
    );
  }

  return (
    <div style={containerStyle}>
      {/* Header with title and refresh */}
      <div style={headerStyle}>
        <span style={titleStyle}>MCP Tools</span>
        <RefreshButton onClick={handleRefresh} loading={loading} ariaLabel="Refresh gateway tools" />
      </div>

      {/* Local filter input — hidden when the parent passes its own
          search query down via `externalSearch` so the user doesn't see
          two redundant boxes side by side. */}
      {!usingExternalSearch && (
        <input
          type="text"
          placeholder="Filter tools..."
          value={filterInput}
          onChange={handleFilterChange}
          style={filterInputStyle}
          aria-label="Filter tools"
        />
      )}

      {/* Loading state */}
      {!loaded && loading && <div style={loadingStyle}>Loading tools...</div>}

      {/* Gateways the fast path could not describe.
          The tools already listed are NOT dimmed and NOT withheld — they are
          current. Only these gateways are outstanding, and saying so beats both
          silence (their tools look absent) and a whole-list spinner (implies what
          is on screen is stale). Same correction made to the built-in Skills
          panel: report the pending PART, not the whole. */}
      {pendingGateways.length > 0 && (
        <div style={pendingGatewayStyle} aria-busy={completing || undefined}>
          {completing
            ? `Loading tools from ${pendingGateways.join(', ')}…`
            : `Could not reach ${pendingGateways.join(', ')} — its tools are not listed. Refresh to retry.`}
        </div>
      )}

      {/* Error state */}
      {error && (
        <div style={errorStyle}>
          {error}
          <button onClick={() => setError(null)} style={dismissBtnStyle}>✕</button>
        </div>
      )}

      {/* Tool list */}
      {loaded && !error && filteredTools.length === 0 && activeFilter && (
        <div style={noMatchStyle}>No tools match your filter</div>
      )}

      {loaded && (() => {
        // Group tools by source
        const groups: Record<string, typeof filteredTools> = {};
        // Dedupe within a group. Every M2C target republishes get_job_status and
        // describe_tool, so the same operation appeared once per target — five
        // identical rows carrying no extra information.
        const seenInGroup = new Set<string>();
        for (const tool of filteredTools) {
          const src = tool.source || 'Other';
          const key = groupKeyFor(src, shortToolName(tool.name));
          const dedupeKey = `${key}|${shortToolName(tool.name)}`;
          if (seenInGroup.has(dedupeKey)) continue;
          seenInGroup.add(dedupeKey);
          if (!groups[key]) groups[key] = [];
          groups[key].push(tool);
        }
        return Object.entries(groups).map(([source, tools]) => {
          // Collapsed unless explicitly opened: 70 expanded rows made this page a
          // scroll-forever list. A search auto-expands so filtering still works
          // without opening groups by hand.
          const isCollapsed = activeFilter
            ? false
            : (collapsedGroups[source] ?? true);
          return (
          <div key={source} style={{ marginBottom: 8 }}>
            <SectionHeader
              label={source}
              count={tools.length}
              collapsed={isCollapsed}
              onToggle={() => toggleGroup(source)}
              colourKey={source}
            />
            {!isCollapsed && tools.map(tool => (
        <div
          key={`${tool.name}-${tool.version}`}
          style={{ ...toolCardStyle, borderLeft: `2px solid ${groupColour(source)}` }}
        >
          <div style={toolHeaderStyle}>
            <div style={toolNameStyle} title={tool.name}>{shortToolName(tool.name)}</div>
            {/* Every built-in reported v1.0, so the badge carried no information
                and repeated on all 45 rows. Shown only when a real version exists. */}
            {tool.version && tool.version !== '1.0' && (
              <span style={toolVersionStyle}>v{tool.version}</span>
            )}
          </div>
          {tool.description && (
            /* Two lines and "more", like every other inventory row: one line with an ellipsis left the rest
               readable only through a hover tooltip (IND403 dry run: "cannot read text"). */
            <div style={toolDescStyle}><ClampedText lines={2} style={{ fontSize: 'var(--fs-2xs)' }}>{tool.description}</ClampedText></div>
          )}
        </div>
      ))}
          </div>
          );
        });
      })()}
    </div>
  );
}

// --- Styles ---

const containerStyle: React.CSSProperties = {
  padding: '8px 0',
};

const headerStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  // Match the 10px horizontal inset of `panelSectionHeaderStyle` on the group
  // rows below. The container has no horizontal padding, so without this the
  // title sits flush against the panel edge while every row beneath it is
  // indented — which reads as a cramped, misaligned header.
  padding: '0 10px',
  marginBottom: 10,
};

const titleStyle: React.CSSProperties = {
  fontSize: 'var(--fs-sm)',
  fontWeight: 600,
  color: theme.colors.textPrimary,
};

const filterInputStyle: React.CSSProperties = {
  width: '100%',
  padding: '6px 8px',
  fontSize: 'var(--fs-xs)',
  borderRadius: theme.radius.button,
  border: `1px solid ${theme.colors.borderStrong}`,
  marginBottom: 8,
  boxSizing: 'border-box',
};

const loadingStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  color: theme.colors.textTertiary,
  fontStyle: 'italic',
  padding: '8px 0',
};

/**
 * The outstanding-gateway line. Same weight as `loadingStyle` on purpose: it is a
 * note about one part of the list, not a warning about the list.
 */
const pendingGatewayStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  color: theme.colors.textTertiary,
  fontStyle: 'italic',
  padding: '6px 0',
};

const errorStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  color: theme.colors.error,
  background: tint(theme.colors.error),
  padding: '6px 8px',
  borderRadius: theme.radius.button,
  marginBottom: 8,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
};

const dismissBtnStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  background: 'none',
  border: 'none',
  cursor: 'pointer',
  color: theme.colors.error,
  padding: '0 4px',
};

const emptyStateStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  color: theme.colors.textTertiary,
  fontStyle: 'italic',
  padding: '8px 0',
};

const noMatchStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  color: theme.colors.textTertiary,
  fontStyle: 'italic',
  padding: '8px 0',
};

const toolCardStyle: React.CSSProperties = {
  padding: '6px 10px',
  borderRadius: theme.radius.button,
  border: `1px solid ${theme.colors.border}`,
  background: theme.colors.surfaceSubtle,
  marginBottom: 4,
};

const toolHeaderStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  gap: 8,
};

const toolNameStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  fontWeight: 600,
  color: theme.colors.textPrimary,
  // Tool names are identifiers; monospace makes
  // `speech-diarization___diarize_speech` scannable instead of a blur.
  fontFamily: 'var(--font-mono)',
};

const toolVersionStyle: React.CSSProperties = {
  fontSize: 'var(--fs-3xs)',
  color: theme.colors.textTertiary,
};

const toolDescStyle: React.CSSProperties = {
  color: theme.colors.textSecondary,
  marginTop: 2,
  lineHeight: 1.3,
};













