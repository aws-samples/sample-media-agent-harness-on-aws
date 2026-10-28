"""Per-source readers for the Capability API (R6).

Four independent readers, each normalizing into :class:`CapabilitySummary`:

============  ====================================================
AssetIndex    actor/group/org-scoped canonical rows — authoritative
Registry      approved harness-shared records — discovery projection
Gateway       shared MCP endpoints, targets, and their tools
builtin       capabilities that ship with the harness
============  ====================================================

No reader ever invokes the AgentCore Runtime, and none falls back to it on
failure — a failing source becomes a typed partial error and the request says
so (R12/Property 5).

The AssetIndex reader delegates to :func:`lab.service.list_visible_assets`
rather than re-deriving the four-tier visibility union. That resolver is what
the Runtime path uses today, so reusing it is what makes the shadow-mode parity
comparison meaningful: a divergence points at the new *transport*, not at two
independent reimplementations of authorization (R5/R15).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Iterable, Optional, Sequence

import runtime_arns

from .config import CapabilityConfig
from .descriptors import PROHIBITED_TOOLS, schema_digest
from .dto import CapabilitySummary
from .naming import (
    parse_record_name,
    parse_record_version,
    qualified_tool_name,
    summary_id,
)
from . import registry as registry_client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Gateway registry schema (mirrors ui/server/toolCatalog.ts)
# ---------------------------------------------------------------------------

#: Partition holding one row per registered gateway.
REGISTERED_PK: str = "GATEWAY#REGISTERED"

#: Partition holding one row per user-registered target, written by the agent
#: at registration time. Ownership is *read* here, never derived, so this
#: surface and the gateway interceptor cannot disagree about who owns what.
TARGET_OWNER_PK: str = "TARGET#OWNER"

#: Hyphen, not underscore: the service's target-name pattern rejects
#: underscores. Must match ``_TARGET_PREFIX`` in ``agent/src/user_mcp.py`` and
#: ``USER_TARGET_PREFIX`` in the gateway interceptor — a mismatch makes every
#: user tool look like platform surface, which fails OPEN.
USER_TARGET_PREFIX: str = "usr-"

#: Synthesized by the gateway itself when semantic search is enabled. Not a
#: target, so it never appears in a target's inline payload; omitting it would
#: under-report the gateway by exactly the tool that makes narrowing possible.
SEARCH_TOOL_NAME: str = "x_amz_bedrock_agentcore_search"

#: Which agents can bind a shared Gateway tool.
_GATEWAY_AGENT_AVAILABILITY: tuple[str, ...] = ("main", "task")

#: Bound on concurrent per-target control-plane reads. Measured serially at
#: ~1.84s for six targets, almost all round-trip latency, which is why these
#: fan out — but not unboundedly, so one gateway with many targets cannot
#: exhaust the connection pool.
_TARGET_FETCH_CONCURRENCY: int = 8


class SourceUnavailable(RuntimeError):
    """A source could not be read. Carries a stable error *class*.

    ``kind`` is one of ``config`` / ``timeout`` / ``unavailable`` / ``error``,
    which is what reaches the browser and the metrics — never a raw exception
    string, which can echo private paths or descriptor bodies.
    """

    def __init__(self, kind: str, detail: Optional[str] = None) -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(kind if detail is None else f"{kind}: {detail}")


# ---------------------------------------------------------------------------
# AssetIndex
# ---------------------------------------------------------------------------


def _asset_scope_label(asset: Any) -> str:
    return str(getattr(asset, "scope", None) or "private")


def summary_from_asset(
    asset: Any, *, projection_status: Optional[str] = None
) -> CapabilitySummary:
    """Normalize a typed ``lab.models`` asset into a summary.

    ``status`` comes from the v2 pair ``(version, deprecated)`` via the model's
    own ``derive_status`` rather than the legacy stored ``status`` field, so a
    row written before that field was made redundant still reports correctly.
    """
    version = getattr(asset, "version", None)
    deprecated = bool(getattr(asset, "deprecated", False))
    category = str(asset.category)
    updated_at_ms = getattr(asset, "updated_at", None)
    return CapabilitySummary(
        id=summary_id(category=category, asset_id=asset.asset_id, version=version),
        category=category,
        capability_class=(
            "tool-collection" if category == "tool-collection" else "asset"
        ),
        display_name=str(getattr(asset, "name", "") or asset.asset_id),
        # Blank collapses to None: the row renders nothing either way, and an
        # empty string in the payload would read as "the author answered, with
        # nothing" to any client that branches on presence.
        description=(str(getattr(asset, "description", "") or "").strip() or None),
        scope=_asset_scope_label(asset),
        status=type(asset).derive_status(version, deprecated)
        if hasattr(type(asset), "derive_status")
        else ("draft" if version is None else "published"),
        source="asset-index",
        asset_id=asset.asset_id,
        version=version,
        agent_availability=("main", "task"),
        projection_status=projection_status,
        updated_at=_epoch_ms_to_iso(updated_at_ms),
    )


def _epoch_ms_to_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


async def read_asset_index(
    category: str,
    *,
    ctx: Any,
    asset_index: Any,
    org_id: Optional[str] = None,
    cursor: Optional[str] = None,
    limit: int = 100,
    include_deprecated: bool = False,
) -> tuple[tuple[CapabilitySummary, ...], Optional[str]]:
    """Actor/group/org-scoped canonical rows for one category.

    Delegates to the existing four-tier visibility resolver in
    ``lab.service``. ``mode="lab"`` returns the full union the owner is
    entitled to see — working copies included — because this is the
    management surface, not a chat inventory.

    Raises:
        SourceUnavailable: DynamoDB was unreachable. Deliberately not
            swallowed: authorization must never be reconstructed from a
            partial canonical read, and there is no S3 fallback (R2).
    """
    if category == "tool":
        # A Gateway tool is not an authored asset; there is nothing to read
        # here for it. Returning empty (rather than raising) keeps the
        # bootstrap fan-out uniform across categories.
        return (), None

    from lab.errors import ServiceUnavailableError
    from lab.service import list_visible_assets

    try:
        page = await list_visible_assets(
            ctx,
            category,
            "lab",
            asset_index=asset_index,
            org_id=org_id,
            include_deprecated=include_deprecated,
            cursor=cursor,
            limit=limit,
        )
    except ServiceUnavailableError as err:
        raise SourceUnavailable("unavailable", "asset index") from err

    return (
        tuple(summary_from_asset(asset) for asset in page.items),
        getattr(page, "next_cursor", None),
    )


# ---------------------------------------------------------------------------
# Agent Registry (discovery projection)
# ---------------------------------------------------------------------------


def summary_from_registry_record(record: dict[str, Any]) -> Optional[CapabilitySummary]:
    """Normalize one discoverable Registry record.

    Returns ``None`` for a record outside this harness's namespace or one whose
    status is not discoverable — a record nobody projected must not be
    presented as organization-discoverable (R12).
    """
    identity = parse_record_name(str(record.get("name") or ""))
    if identity is None:
        return None
    if record.get("status") != registry_client.DISCOVERABLE_STATUS:
        return None
    category = identity.category
    if category == "mcp-endpoint":
        # Endpoint records are consumed by the Gateway reader, which pairs them
        # with live target data; surfacing the record itself as a "tool" row
        # would double-count the endpoint.
        return None

    version = parse_record_version(record.get("recordVersion"))
    updated_at = record.get("updatedAt")
    return CapabilitySummary(
        id=summary_id(
            category=category, asset_id=identity.asset_id, version=version or 1
        ),
        category=category,
        capability_class="builtin" if identity.is_builtin else "asset",
        display_name=str(
            record.get("displayName") or record.get("name") or identity.asset_id
        ),
        scope="harness-shared",
        status="published",
        source="agent-registry",
        asset_id=identity.asset_id,
        version=version,
        registry_record_id=str(record.get("recordId") or "") or None,
        agent_availability=("main", "task"),
        projection_status="in_sync",
        updated_at=updated_at.isoformat() if hasattr(updated_at, "isoformat") else None,
    )


async def read_registry(
    category: str, *, config: CapabilityConfig, client: Any = None
) -> tuple[CapabilitySummary, ...]:
    """Approved harness-shared records for one category.

    ``ListDiscoverableRegistryRecords`` returns metadata only — no
    descriptors — which is exactly what a list view needs, so no
    ``BatchGet`` follow-up happens here. The API's ``recordType`` filter is too
    coarse to isolate a category (``CUSTOM`` covers tool collections *and*
    workflows), so filtering is on the deterministic record name.

    Raises:
        SourceUnavailable: No registry configured, or the data plane failed.
    """
    if not config.registry_id:
        raise SourceUnavailable("config", "registry not configured")

    data = client or registry_client.data_client(config.region)

    def _list() -> list[CapabilitySummary]:
        summaries: list[CapabilitySummary] = []
        for record in registry_client.iter_discoverable_records(
            data, config.registry_id
        ):
            identity = parse_record_name(str(record.get("name") or ""))
            if identity is None or identity.category != category:
                continue
            summary = summary_from_registry_record(record)
            if summary is not None:
                summaries.append(summary)
        return summaries

    try:
        return tuple(await asyncio.to_thread(_list))
    except SourceUnavailable:
        raise
    except Exception as err:  # noqa: BLE001 — classified, never echoed raw
        raise SourceUnavailable("unavailable", "agent-registry") from err


# ---------------------------------------------------------------------------
# Shared MCP Gateways
# ---------------------------------------------------------------------------


def gateway_id_from_url(url: Optional[str]) -> Optional[str]:
    """``https://<id>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp`` -> ``<id>``.

    Returns ``None`` for anything else. A directly federated MCP endpoint (an
    AgentCore *Runtime* invocations URL, say) is not a gateway and has no
    control-plane representation, so it has no target hierarchy to read.
    """
    if not url:
        return None
    from urllib.parse import urlparse

    host = urlparse(url).hostname or ""
    parts = host.split(".")
    if len(parts) < 2 or parts[1] != "gateway":
        return None
    if not ".".join(parts[1:]).startswith("gateway.bedrock-agentcore."):
        return None
    return parts[0] or None


def is_user_target(target_name: Optional[str]) -> bool:
    """Whether a target belongs to one user rather than the platform."""
    return bool(target_name) and str(target_name).startswith(USER_TARGET_PREFIX)


def actor_may_see_target(
    actor_id: str, target_name: Optional[str], owners: dict[str, str]
) -> bool:
    """Whether ``actor_id`` may see a target, per recorded ownership.

    Platform targets are visible to everyone. A user target with no ownership
    record, or no actor, is hidden — fail closed, matching the interceptor.
    The Gateway control plane is *not* interceptor-filtered (the interceptor
    only sees the MCP data plane), so without this every user's registered MCP
    tools would be listed to everyone even though calling them is denied.
    """
    if not is_user_target(target_name):
        return True
    if not actor_id:
        return False
    return owners.get(str(target_name)) == actor_id


def _query_all(table: Any, pk: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": "PK = :pk",
        "ExpressionAttributeValues": {":pk": pk},
    }
    while True:
        response = table.query(**kwargs)
        items.extend(response.get("Items", []))
        last = response.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last


def read_enabled_gateways(table: Any) -> list[dict[str, Any]]:
    """Enabled gateway descriptors. Disabled rows must not appear."""
    gateways: list[dict[str, Any]] = []
    for item in _query_all(table, REGISTERED_PK):
        if item.get("enabled") is not True:
            continue
        gateway_id = str(item.get("id") or item.get("SK") or "")
        if not gateway_id:
            continue
        gateways.append(
            {
                "id": gateway_id,
                "name": str(item.get("name") or gateway_id),
                "url": str(item.get("url") or "") or None,
            }
        )
    return gateways


def read_target_owners(table: Any) -> dict[str, str]:
    """Every recorded target ownership as one map.

    One Query rather than a GetItem per tool: the catalog needs the whole set
    anyway.
    """
    owners: dict[str, str] = {}
    for item in _query_all(table, TARGET_OWNER_PK):
        name = item.get("SK")
        owner = item.get("ownerActorId")
        if isinstance(name, str) and isinstance(owner, str) and name and owner:
            owners[name] = owner
    return owners


def read_gateway_targets(
    control: Any, gateway_id: str
) -> tuple[list[dict[str, Any]], bool]:
    """Targets and their inline tool definitions for one gateway.

    Returns ``(targets, semantic_search_enabled)``. Each target is
    ``{"targetId", "targetName", "tools": [{"name", "description",
    "inputSchema"}]}``.

    Lambda targets carry their full definitions in
    ``toolSchema.inlinePayload``, so the control plane is authoritative and
    there is no projection to keep in sync. An MCP-server target keeps its
    capabilities in the gateway's synchronized catalog instead and exposes none
    here, so it contributes nothing rather than wrong data.

    A single unreadable *target* is skipped — partial truth beats none for a
    browse surface — but an unreadable *gateway* raises, because reporting an
    empty gateway would look like a gateway with no tools.
    """
    try:
        gateway = control.get_gateway(gatewayIdentifier=gateway_id)
    except Exception as err:  # noqa: BLE001
        raise SourceUnavailable("unavailable", "gateway control plane") from err

    semantic = (
        (gateway.get("protocolConfiguration") or {}).get("mcp") or {}
    ).get("searchType") == "SEMANTIC"

    try:
        listing = control.list_gateway_targets(gatewayIdentifier=gateway_id)
    except Exception as err:  # noqa: BLE001
        raise SourceUnavailable("unavailable", "gateway targets") from err

    targets: list[dict[str, Any]] = []
    for item in listing.get("items", []):
        target_id = str(item.get("targetId") or "")
        if not target_id:
            continue
        try:
            detail = control.get_gateway_target(
                gatewayIdentifier=gateway_id, targetId=target_id
            )
        except Exception as err:  # noqa: BLE001 — skip one target, keep the rest
            logger.info(
                "skipping unreadable gateway target: gateway=%s target=%s error=%s",
                gateway_id,
                target_id,
                type(err).__name__,
            )
            continue
        target_name = str(detail.get("name") or "")
        mcp = (detail.get("targetConfiguration") or {}).get("mcp") or {}
        inline = ((mcp.get("lambda") or {}).get("toolSchema") or {}).get(
            "inlinePayload"
        ) or []
        tools = [
            {
                "name": str(tool.get("name") or ""),
                "description": str(tool.get("description") or ""),
                "inputSchema": tool.get("inputSchema"),
            }
            for tool in inline
            if isinstance(tool, dict) and tool.get("name")
        ]
        targets.append(
            {"targetId": target_id, "targetName": target_name, "tools": tools}
        )
    return targets, semantic


def summaries_from_gateway(
    *,
    gateway_id: str,
    gateway_name: str,
    targets: Sequence[dict[str, Any]],
    semantic_search: bool,
    actor_id: str,
    owners: dict[str, str],
) -> tuple[CapabilitySummary, ...]:
    """One summary per (gateway, target, tool), with prohibited tools dropped.

    Repeated bare names across targets stay distinct rows: the summary id keys
    on ``(gatewayId, targetId, bareToolName)`` and every row keeps its
    target-qualified invocation name, so the UI may group Gateway → Target →
    Tool without ever collapsing two different tools into one (R10).

    Prohibited internal tools are filtered rather than raising: unlike a
    descriptor being *written* to the shared Registry, a read surface should
    degrade to "we don't show that" instead of failing the whole tab.
    """
    summaries: list[CapabilitySummary] = []
    for target in targets:
        target_name = target.get("targetName") or ""
        if not actor_may_see_target(actor_id, target_name, owners):
            continue
        for tool in target.get("tools", []):
            bare = str(tool.get("name") or "")
            if not bare:
                continue
            if "___" in bare:
                # Some gateways report already-qualified names; normalize so
                # the qualification is applied exactly once.
                prefix, _, remainder = bare.partition("___")
                if not target_name:
                    target_name = prefix
                bare = remainder
            if bare in PROHIBITED_TOOLS:
                logger.debug("excluding prohibited tool from catalog: %s", bare)
                continue
            summaries.append(
                CapabilitySummary(
                    id=summary_id(
                        category="tool",
                        gateway_id=gateway_id,
                        target_id=str(target.get("targetId") or ""),
                        bare_tool_name=bare,
                    ),
                    category="tool",
                    capability_class="gateway-mcp",
                    display_name=bare,
                    scope="harness-shared" if not is_user_target(target_name) else "private",
                    status="published",
                    source="gateway",
                    gateway_id=gateway_id,
                    target_id=str(target.get("targetId") or "") or None,
                    target_name=str(target_name) or None,
                    qualified_tool_name=(
                        qualified_tool_name(str(target_name), bare)
                        if target_name
                        else bare
                    ),
                    bare_tool_name=bare,
                    schema_digest=schema_digest(tool.get("inputSchema")),
                    agent_availability=_GATEWAY_AGENT_AVAILABILITY,
                )
            )
    if semantic_search:
        summaries.append(
            CapabilitySummary(
                id=summary_id(
                    category="tool",
                    gateway_id=gateway_id,
                    bare_tool_name=SEARCH_TOOL_NAME,
                ),
                category="tool",
                capability_class="gateway-mcp",
                display_name=f"{gateway_name}: semantic tool search",
                scope="harness-shared",
                status="published",
                source="gateway",
                gateway_id=gateway_id,
                qualified_tool_name=SEARCH_TOOL_NAME,
                bare_tool_name=SEARCH_TOOL_NAME,
                agent_availability=_GATEWAY_AGENT_AVAILABILITY,
            )
        )
    return tuple(summaries)


async def read_gateway_catalog(
    *,
    config: CapabilityConfig,
    actor_id: str,
    gateway_table: Any,
    control: Any = None,
) -> tuple[CapabilitySummary, ...]:
    """Every shared Gateway tool the actor may see.

    Raises:
        SourceUnavailable: No gateway registry configured, or a gateway could
            not be read. Never silently returns a short list — an
            under-reported tool catalog looks like a working one.
    """
    if gateway_table is None or not config.gateway_registry_table:
        raise SourceUnavailable("config", "gateway registry not configured")

    if control is None:
        import boto3

        control = boto3.client("bedrock-agentcore-control", region_name=config.region)

    def _read() -> tuple[CapabilitySummary, ...]:
        gateways = read_enabled_gateways(gateway_table)
        owners = read_target_owners(gateway_table)
        collected: list[CapabilitySummary] = []
        for gateway in gateways:
            gateway_id = gateway_id_from_url(gateway.get("url")) or gateway["id"]
            targets, semantic = read_gateway_targets(control, gateway_id)
            collected.extend(
                summaries_from_gateway(
                    gateway_id=gateway_id,
                    gateway_name=gateway["name"],
                    targets=targets,
                    semantic_search=semantic,
                    actor_id=actor_id,
                    owners=owners,
                )
            )
        return tuple(collected)

    try:
        return await asyncio.to_thread(_read)
    except SourceUnavailable:
        raise
    except Exception as err:  # noqa: BLE001
        raise SourceUnavailable("unavailable", "gateway catalog") from err


# ---------------------------------------------------------------------------
# Built-ins
# ---------------------------------------------------------------------------

#: Capabilities that ship with the harness. Rendered in their own tabs — never
#: duplicated as Tools cards (R10) — and projected into the shared Registry
#: under the reserved ``mediaworks/builtin/...`` identity.
#:
#: Deliberately a small, explicit table rather than an import from the Runtime:
#: importing ``skills_manager`` to enumerate built-in skills would pull Strands
#: into the Capability API and defeat R1. The Runtime remains the source of
#: truth for their *content*; this is the inventory the control plane can state
#: without booting an agent.
#: The built-in sub-agent runtimes, as the Agents panel renders them.
#:
#: ``description`` and ``invokedVia`` were the agent runtime's to serve
#: (``mcp_list_agents`` in ``main.py``) until the panel started painting from here.
#: They live in this descriptor now so there is ONE copy — the runtime handler reads
#: these same entries rather than restating them.
#:
#: ``invokedVia`` is deliberately explicit, because the harness has two sub-agent
#: invocation paths (``dispatch_tool`` for task/render, ``plan_and_execute_tool`` for
#: planning) and stating one implies every runtime is a dispatch target.
#:
#: ``arnEnv`` is the environment variable naming the deployed runtime. It is read at
#: read from the environment (see ``_builtin_runtime_state``), which is why a Capability
#: API task that never received these variables reports every sub-agent as unconfigured.
BUILTIN_CAPABILITIES: tuple[dict[str, Any], ...] = (
    {
        "category": "specialty-agent",
        "slug": "task-agent",
        "displayName": "Task Agent",
        "version": "1",
        "agentAvailability": ("main",),
        "description": (
            "General-purpose sub-agent for isolated, tool-heavy work "
            "(code_interpreter + artifact)."
        ),
        "invokedVia": "dispatch_tool",
        "arnEnv": "TASK_AGENT_RUNTIME_ARN",
    },
    {
        "category": "specialty-agent",
        "slug": "planning-agent",
        "displayName": "Planning Agent",
        "version": "1",
        "agentAvailability": ("main",),
        "description": (
            "Turns a goal into a HighLevelPlan (DAG) for complex multi-step work."
        ),
        "invokedVia": "plan_and_execute_tool",
        "arnEnv": "PLANNING_AGENT_RUNTIME_ARN",
    },
    {
        "category": "specialty-agent",
        "slug": "render-agent",
        "displayName": "Render Agent",
        "version": "1",
        "agentAvailability": ("main",),
        "description": (
            "Turns artifact URLs (media/documents) into an interactive render "
            "manifest (NLE timeline)."
        ),
        "invokedVia": "dispatch_tool + render-artifacts hook",
        "arnEnv": "RENDER_AGENT_RUNTIME_ARN",
    },
)


def _builtin_runtime_state() -> dict[str, tuple[bool, str]]:
    """``slug -> (configured, runtime_id)`` from the ARN environment variables.

    Read per call rather than once at import. The values do not change under a running
    task, so caching would be free — but an import-time constant makes the function
    untestable without ``importlib.reload``, and reloading a module mid-suite swaps its
    class objects, so ``except SourceUnavailable`` silently stops matching in whatever
    test runs next. Four ``os.environ`` lookups are cheaper than that failure mode.

    ``runtime_id`` is the ARN's last path segment, matching how ``main.py`` derived
    it — the panel shows that id, so deriving it differently here would surface two
    different ids for one runtime depending on which host answered.
    """
    state: dict[str, tuple[bool, str]] = {}
    for entry in BUILTIN_CAPABILITIES:
        env_name = entry.get("arnEnv")
        if not env_name:
            continue
        # Env first (the Capability API task def carries the ARNs), else the runtime-ARN SSM
        # parameters — the agent runtimes no longer receive these as env (runtime_arns.py).
        arn = runtime_arns.get(env_name)
        state[entry["slug"]] = (bool(arn), arn.rsplit("/", 1)[-1] if arn else "")
    return state


#: Where the deploy writes the built-in skill catalog. Same PK as the builtin TOOL
#: projection the runtime publishes (``main.py:_CATALOG_PK``, read by
#: ``ui/server/toolCatalog.ts``); skills are not gateway-derived, the shared PK just
#: means "builtin catalog projections".
BUILTIN_CATALOG_PK: str = "GATEWAY#CATALOG"

#: One item per skill: ``__skill__<slug>``. Per-skill rather than one row holding all of
#: them because each item carries the whole ``SKILL.md`` (so the detail view needs no
#: runtime call), and bodies make DynamoDB's 400 KB per-item limit a real constraint.
#:
#: The prefix is unambiguous under this PK: it does not match the builtin TOOL row
#: ``__builtin__``, nor the single ``__skills__`` row this replaced (those diverge at
#: index 7 — ``__skills_`` vs ``__skill__``).
BUILTIN_SKILL_SK_PREFIX: str = "__skill__"

#: How long a fetched skill catalog is reused. Short, and deliberately NOT per-process:
#: the row is rewritten by ``cdk deploy`` without this task restarting, so a
#: life-of-container cache would serve the pre-deploy catalog until the task was
#: replaced — the exact staleness this projection exists to remove.
_SKILL_CATALOG_TTL_SECONDS: float = 60.0
_skill_catalog_cache: tuple[float, tuple[dict[str, Any], ...]] | None = None


def _read_builtin_skills(gateway_table: Any) -> tuple[dict[str, Any], ...]:
    """Built-in skill descriptors from the catalog row the deploy wrote.

    Read from DynamoDB rather than from files in this image, so that editing a
    ``SKILL.md`` reaches the panel on ``cdk deploy`` alone. The row is written by CDK
    (``infra/lib/builtin-skills.ts`` + the ``BuiltinSkillCatalog`` custom resource),
    not by the agent runtime: a runtime starts on demand, so a row it published "at
    startup" would be absent on a fresh environment and describe the previous version
    until someone happened to invoke it.

    Raises ``SourceUnavailable`` when the row cannot be read or is absent. That
    surfaces as a ``partialErrors`` entry and a "Partial results" banner, which is the
    honest answer — an empty list would state "this harness ships no skills", and a
    failed catalog write would look like a deliberate product decision.
    """
    global _skill_catalog_cache

    now = time.monotonic()
    if _skill_catalog_cache is not None:
        cached_at, cached = _skill_catalog_cache
        if now - cached_at < _SKILL_CATALOG_TTL_SECONDS:
            return cached

    if gateway_table is None:
        raise SourceUnavailable("config", "gateway registry table is not configured")

    try:
        # PROJECTED on purpose. Each item also carries the whole SKILL.md, and the list
        # renders none of it — pulling all seven bodies (~67 KB) on every Skills tab open
        # to populate a name and a description would undo the point of serving the list
        # from here. The body is fetched per skill by `read_builtin_skill`.
        response = gateway_table.query(
            # String condition to match `_query_all` above rather than introducing
            # `boto3.dynamodb.conditions` for one call. `#n` because `name` is a
            # DynamoDB reserved word and a bare projection on it fails.
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
            ExpressionAttributeValues={
                ":pk": BUILTIN_CATALOG_PK,
                ":sk": BUILTIN_SKILL_SK_PREFIX,
            },
            ProjectionExpression="#n, description",
            ExpressionAttributeNames={"#n": "name"},
        )
    except Exception as err:  # noqa: BLE001 — reported as a class, never a raw string
        raise SourceUnavailable("unavailable", "builtin skill catalog") from err

    rows = response.get("Items")
    if not rows:
        raise SourceUnavailable("unavailable", "builtin skill catalog not published")

    entries = tuple(
        {
            "category": "skill",
            "slug": str(row.get("name") or ""),
            "displayName": str(row.get("name") or ""),
            "version": "1",
            "agentAvailability": ("main",),
            # Absent stays absent: a skill whose frontmatter had no description must
            # render without a description line, not with an empty one.
            "description": (str(row["description"]) if row.get("description") else None),
        }
        for row in sorted(rows, key=lambda r: str(r.get("name") or ""))
        if isinstance(row, dict) and row.get("name")
    )
    _skill_catalog_cache = (now, entries)
    return entries


async def read_builtin_skill(slug: str, gateway_table: Any) -> Optional[dict[str, Any]]:
    """One built-in skill's package, for the detail view. ``None`` if there is no such row.

    Separate from the list read because this is the expensive half: the whole ``SKILL.md``
    plus the package's file names. Fetched only when a skill is actually opened, which is
    why it is a ``GetItem`` on a known key rather than anything the list pays for.

    Not cached: a View is a deliberate action, ~200 ms uncached, and caching it would mean
    a deploy's new text could be masked by a warm task for the cache's lifetime — the
    staleness this whole projection exists to remove.
    """
    if gateway_table is None:
        raise SourceUnavailable("config", "gateway registry table is not configured")
    try:
        response = gateway_table.get_item(
            Key={"PK": BUILTIN_CATALOG_PK, "SK": f"{BUILTIN_SKILL_SK_PREFIX}{slug}"}
        )
    except Exception as err:  # noqa: BLE001
        raise SourceUnavailable("unavailable", "builtin skill package") from err

    item = response.get("Item")
    if not item:
        return None
    return {
        "name": str(item.get("name") or slug),
        "description": str(item.get("description") or ""),
        "skillMd": str(item.get("skillMd") or ""),
        # A list is always written, even empty, so absent means the row predates this
        # field rather than "this package has no files".
        "supportingFiles": [str(f) for f in (item.get("supportingFiles") or [])],
    }


async def read_builtins(
    category: str, *, gateway_table: Any = None
) -> tuple[CapabilitySummary, ...]:
    """Built-in inventory for one category — no runtime, no model.

    This is what lets the Agents and Skills built-in panels paint without waking a
    microVM. Agents come from a descriptor in this module; skills come from the
    DynamoDB catalog the deploy writes, so a ``SKILL.md`` edit does not require a
    Capability API image rebuild.
    """
    if category == "skill":
        entries: tuple[dict[str, Any], ...] = _read_builtin_skills(gateway_table)
    else:
        entries = tuple(
            e for e in BUILTIN_CAPABILITIES if e["category"] == category
        )

    runtime_state = _builtin_runtime_state()
    summaries: list[CapabilitySummary] = []
    for entry in entries:
        configured: Optional[bool] = None
        runtime_id: Optional[str] = None
        if entry.get("arnEnv"):
            is_configured, resolved_id = runtime_state.get(
                entry["slug"], (False, "")
            )
            configured = is_configured
            runtime_id = resolved_id or None
        summaries.append(
            CapabilitySummary(
                id=f"builtin:{entry['category']}:{entry['slug']}",
                category=entry["category"],
                capability_class="builtin",
                display_name=entry["displayName"],
                scope="harness-shared",
                status="published",
                source="builtin",
                description=entry.get("description"),
                asset_id=entry["slug"],
                version=entry.get("version"),
                agent_availability=tuple(entry.get("agentAvailability", ())),
                invoked_via=entry.get("invokedVia"),
                configured=configured,
                runtime_id=runtime_id,
            )
        )
    return tuple(summaries)


def iter_projectable_gateway_targets(
    targets: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop user-private targets before projecting a Gateway endpoint.

    A ``usr-`` target belongs to one actor. It is legitimately visible to its
    owner through :func:`read_gateway_catalog`, but the shared Registry may
    contain only records safe for *every* authorized consumer, so it must not
    be nested into the endpoint's ``MCP`` record (R3/R5).
    """
    return [t for t in targets if not is_user_target(t.get("targetName"))]


__all__ = [
    "BUILTIN_CAPABILITIES",
    "REGISTERED_PK",
    "SEARCH_TOOL_NAME",
    "SourceUnavailable",
    "TARGET_OWNER_PK",
    "USER_TARGET_PREFIX",
    "actor_may_see_target",
    "gateway_id_from_url",
    "is_user_target",
    "iter_projectable_gateway_targets",
    "read_asset_index",
    "read_builtins",
    "read_enabled_gateways",
    "read_gateway_catalog",
    "read_gateway_targets",
    "read_registry",
    "read_target_owners",
    "summaries_from_gateway",
    "summary_from_asset",
    "summary_from_registry_record",
]
