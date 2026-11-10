"""LabChatAgent — inventory-aware conversational agent for the Lab.

The Lab_Chat_Agent builds an Inventory_Snapshot per turn, injects it into
the system prompt as a structured listing, and exposes CRUD tools as thin
wrappers around Lab_Service operations. The chat agent role has NO direct
S3/DDB write permission — all mutations go through the validated CRUD layer.

Requirements: 6.1, 6.2, 6.3, 6.6
Properties: 13, 14
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Literal, Optional

from .asset_index import (
    AssetIndex,
    make_pk_user_owned,
)
from .context import LabRequestContext
from .conversational_memory import ConversationalMemory, TurnRecord
from .models import (
    AssetCommon,
    Category,
    Skill,
    SpecialtyAgent,
    ToolCollection,
    Workflow,
)
from prompts.chat import _SYSTEM_PROMPT_TEMPLATE

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Inventory Snapshot
# ---------------------------------------------------------------------------

CATEGORIES: list[Category] = ["skill", "specialty-agent", "tool-collection", "workflow"]

_CATEGORY_DISPLAY_NAMES: dict[str, str] = {
    "skill": "Skills",
    "specialty-agent": "Specialty Agents",
    "tool-collection": "Tool Collections",
    "workflow": "Workflows",
}


@dataclass
class InventorySnapshot:
    """Point-in-time snapshot of every asset owned by the current actor.

    Surfaces both:
      * working copies (mutable, in ``USER#<u>#OWNED``) — labeled "(working copy)"
      * the actor's own published versions (immutable, in
        ``USER#<u>#VERSIONS``) — surfaces the latest non-deprecated
        version per asset_id, labeled with its version number

    The keys ``drafts_by_category`` and ``total_count`` retain their
    legacy names for backwards compat with code that already uses them;
    new fields (``versions_by_category``) carry the published view.
    """

    actor_id: str
    drafts_by_category: dict[str, list[AssetCommon]] = field(default_factory=dict)
    versions_by_category: dict[str, list[AssetCommon]] = field(default_factory=dict)
    total_count: int = 0

    @classmethod
    async def build(cls, *, index: AssetIndex, actor_id: str) -> "InventorySnapshot":
        """Build inventory: query OWNED and VERSIONS, partition by category.

        For VERSIONS, only the *latest non-deprecated* version per
        asset_id is included so inventory listings stay compact even
        for assets with many published revisions.
        """
        from .asset_index import make_pk_user_versions

        drafts_by_category: dict[str, list[AssetCommon]] = {
            cat: [] for cat in CATEGORIES
        }
        versions_by_category: dict[str, list[AssetCommon]] = {
            cat: [] for cat in CATEGORIES
        }

        # --- OWNED partition: working copies across every category. ---
        owned_pk = make_pk_user_owned(actor_id)
        owned_items, _next = await index.query_pk(owned_pk)
        for item in owned_items:
            if isinstance(item, Skill):
                drafts_by_category["skill"].append(item)
            elif isinstance(item, SpecialtyAgent):
                drafts_by_category["specialty-agent"].append(item)
            elif isinstance(item, ToolCollection):
                drafts_by_category["tool-collection"].append(item)
            elif isinstance(item, Workflow):
                drafts_by_category["workflow"].append(item)

        # --- VERSIONS partition: actor's published versions. ---
        versions_pk = make_pk_user_versions(actor_id)
        version_items, _v_next = await index.query_pk(versions_pk)

        # Latest non-deprecated version per (category, asset_id). Sort
        # by version desc and pick the first per key.
        latest_per_asset: dict[tuple[str, str], AssetCommon] = {}
        for item in version_items:
            if item.deprecated:
                continue
            if item.version is None:
                continue
            key = (item.category, item.asset_id)
            existing = latest_per_asset.get(key)
            if existing is None or (
                existing.version is not None and item.version > existing.version
            ):
                latest_per_asset[key] = item

        for (cat, _aid), record in latest_per_asset.items():
            if cat in versions_by_category:
                versions_by_category[cat].append(record)

        total_count = sum(len(v) for v in drafts_by_category.values()) + sum(
            len(v) for v in versions_by_category.values()
        )

        return cls(
            actor_id=actor_id,
            drafts_by_category=drafts_by_category,
            versions_by_category=versions_by_category,
            total_count=total_count,
        )

    def format_as_prompt_section(self) -> str:
        """Render snapshot as a system-prompt section.

        Each category lists working copies first (labeled "(working
        copy)") then published versions (labeled "v<N>"). Empty
        categories are summarized in one line.
        """
        if self.total_count == 0:
            return "Your Lab inventory is empty."

        lines = [f"Your Lab inventory ({self.total_count} asset(s)):"]
        lines.append("")

        for category in CATEGORIES:
            display_name = _CATEGORY_DISPLAY_NAMES[category]
            drafts = self.drafts_by_category.get(category, [])
            versions = self.versions_by_category.get(category, [])
            total = len(drafts) + len(versions)

            if total == 0:
                lines.append(f"## {display_name} (0)")
                lines.append("- (none)")
                lines.append("")
                continue

            lines.append(f"## {display_name} ({total})")
            for d in drafts:
                lines.append(f"- {d.name} (id: {d.asset_id}) — working copy (unpublished)")
            for v in versions:
                lines.append(
                    f"- {v.name} (id: {v.asset_id}) — v{v.version}"
                    f"{' [current]' if v.current else ''}"
                )
            lines.append("")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Disambiguation (Requirement 6.6)
# ---------------------------------------------------------------------------

DisambiguationOutcome = Literal["resolved", "not_found", "ambiguous"]


@dataclass
class DisambiguationResult:
    """Result of attempting to resolve a draft by name within a category.

    Requirement 6.6: If the user's message asks the Lab_Chat_Agent to act on
    a Draft identified only by name and more than one Draft in the
    Inventory_Snapshot matches that name within the same category, THEN THE
    Lab_Chat_Agent SHALL ask the user to disambiguate and SHALL NOT mutate
    any Draft until the user selects one.

    Outcomes:
    - resolved: Exactly one match found; asset_id is set.
    - not_found: Zero matches; nearest_matches contains closest alternatives.
    - ambiguous: Two or more matches; matches lists all candidates.
    """

    outcome: DisambiguationOutcome
    asset_id: Optional[str] = None
    matches: list[AssetCommon] = field(default_factory=list)
    nearest_matches: list[AssetCommon] = field(default_factory=list)


def _similarity_score(a: str, b: str) -> float:
    """Compute a similarity score between two strings (case-insensitive).

    Uses SequenceMatcher ratio which returns a float in [0, 1].
    """
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def disambiguate(
    snapshot: InventorySnapshot,
    name: str,
    category: Category,
    *,
    max_nearest: int = 5,
) -> DisambiguationResult:
    """Resolve a draft by name within a category from the inventory snapshot.

    This implements the disambiguation flow required by Requirement 6.6:
    - Exactly 1 match → return that draft's assetId (no ambiguity)
    - 0 matches → return a "not found" result with nearest matches
    - 2+ matches → return an "ambiguous" result listing all matches

    The match is case-insensitive and exact (the full name must match).

    Args:
        snapshot: The current turn's InventorySnapshot.
        name: The draft name to search for.
        category: The category to search within.
        max_nearest: Maximum number of nearest matches to return on zero hits.

    Returns:
        A DisambiguationResult indicating the outcome.
    """
    drafts_in_category = snapshot.drafts_by_category.get(category, [])

    # Find exact matches (case-insensitive)
    exact_matches = [
        draft for draft in drafts_in_category
        if draft.name.lower() == name.lower()
    ]

    if len(exact_matches) == 1:
        # Exactly one match — resolved
        return DisambiguationResult(
            outcome="resolved",
            asset_id=exact_matches[0].asset_id,
            matches=exact_matches,
        )

    if len(exact_matches) >= 2:
        # Multiple matches — ambiguous; never mutate
        return DisambiguationResult(
            outcome="ambiguous",
            matches=exact_matches,
        )

    # Zero exact matches — find nearest alternatives
    # Use both substring containment and similarity scoring
    scored: list[tuple[float, AssetCommon]] = []
    name_lower = name.lower()

    for draft in drafts_in_category:
        draft_name = draft.name
        draft_name_lower = draft_name.lower()

        # Boost score if the search term is a substring or vice versa
        sim = _similarity_score(name, draft_name)
        if name_lower in draft_name_lower or draft_name_lower in name_lower:
            sim = max(sim, 0.6)  # Ensure substring matches rank high

        scored.append((sim, draft))

    # Sort by similarity descending, take top N
    scored.sort(key=lambda x: x[0], reverse=True)
    nearest = [draft for score, draft in scored[:max_nearest] if score > 0.0]

    return DisambiguationResult(
        outcome="not_found",
        nearest_matches=nearest,
    )


# ---------------------------------------------------------------------------
# System Prompt
# ---------------------------------------------------------------------------



def build_system_prompt(snapshot: InventorySnapshot, tool_catalog: list[dict] | None = None) -> str:
    """Build the full system prompt with the inventory snapshot and tool catalog injected.

    Args:
        snapshot: The current turn's InventorySnapshot.
        tool_catalog: Optional list of tool descriptors from the Gateway.

    Returns:
        The complete system prompt string.
    """
    inventory_section = snapshot.format_as_prompt_section()

    # Format tool catalog
    if tool_catalog:
        tool_lines = []
        for tool in tool_catalog:
            name = tool.get("name", "")
            desc = tool.get("description", "")
            # Show short name for readability
            short_name = name.split("___")[-1] if "___" in name else name
            tool_lines.append(f"- **{short_name}** (`{name}`): {desc[:100]}")
        tool_catalog_section = "\n".join(tool_lines)
    else:
        tool_catalog_section = "(Tool catalog not available)"

    return _SYSTEM_PROMPT_TEMPLATE.format(
        inventory_section=inventory_section,
        tool_catalog_section=tool_catalog_section,
    )


# ---------------------------------------------------------------------------
# CRUD Tool Definitions
# ---------------------------------------------------------------------------


@dataclass
class ToolDefinition:
    """Definition of a CRUD tool exposed to the chat agent."""

    name: str
    description: str
    parameters: dict


# Tool definitions that the chat agent can invoke
CRUD_TOOL_DEFINITIONS: list[ToolDefinition] = [
    # Skills
    ToolDefinition(
        name="create_skill",
        description="Create a new skill draft with a name and SKILL.md content.",
        parameters={"name": "string", "skill_md": "string"},
    ),
    ToolDefinition(
        name="read_skill",
        description="Read a skill draft by its asset ID.",
        parameters={"asset_id": "string"},
    ),
    ToolDefinition(
        name="update_skill",
        description="Update the SKILL.md content of an existing skill draft.",
        parameters={"asset_id": "string", "skill_md": "string"},
    ),
    ToolDefinition(
        name="delete_skill",
        description="Delete a skill draft by its asset ID.",
        parameters={"asset_id": "string"},
    ),
    # Specialty Agents
    ToolDefinition(
        name="create_specialty_agent",
        description="Create a new specialty agent draft with a name, persona prompt, and optional compatible skills.",
        parameters={"name": "string", "persona": "string", "compatible_skills": "list[string]?"},
    ),
    ToolDefinition(
        name="read_specialty_agent",
        description="Read a specialty agent draft by its asset ID.",
        parameters={"asset_id": "string"},
    ),
    ToolDefinition(
        name="update_specialty_agent",
        description="Update a specialty agent's persona or compatible skills.",
        parameters={"asset_id": "string", "persona": "string?", "compatible_skills": "list[string]?"},
    ),
    ToolDefinition(
        name="delete_specialty_agent",
        description="Delete a specialty agent draft by its asset ID.",
        parameters={"asset_id": "string"},
    ),
    # Tool Collections
    ToolDefinition(
        name="create_tool_collection",
        description="Create a new tool collection draft with a name and list of tool references.",
        parameters={"name": "string", "tool_refs": "list[{name, version}]"},
    ),
    ToolDefinition(
        name="read_tool_collection",
        description="Read a tool collection draft by its asset ID.",
        parameters={"asset_id": "string"},
    ),
    ToolDefinition(
        name="update_tool_collection",
        description="Update a tool collection's name or tool references.",
        parameters={"asset_id": "string", "name": "string?", "tool_refs": "list[{name, version}]?"},
    ),
    ToolDefinition(
        name="delete_tool_collection",
        description=(
            "Delete a tool collection by its asset ID. Refuses with error "
            "'still_referenced' if a specialty agent still selects it; report "
            "those agents to the user rather than retrying with force."
        ),
        parameters={"asset_id": "string", "force": "bool?"},
    ),
    # Workflows
    ToolDefinition(
        name="create_workflow",
        description="Create a new workflow draft with a name and ordered list of steps.",
        parameters={"name": "string", "steps": "list[WorkflowStep]"},
    ),
    ToolDefinition(
        name="read_workflow",
        description="Read a workflow draft by its asset ID.",
        parameters={"asset_id": "string"},
    ),
    ToolDefinition(
        name="update_workflow",
        description="Update a workflow's name or steps.",
        parameters={"asset_id": "string", "name": "string?", "steps": "list[WorkflowStep]?"},
    ),
    ToolDefinition(
        name="delete_workflow",
        description="Delete a workflow draft by its asset ID.",
        parameters={"asset_id": "string"},
    ),
]


# ---------------------------------------------------------------------------
# CRUD Tool Router
# ---------------------------------------------------------------------------


@dataclass
class ToolCallResult:
    """Result of a CRUD tool invocation."""

    success: bool
    data: Any = None
    error: Optional[str] = None


class CRUDToolRouter:
    """Routes chat agent tool calls to the appropriate Lab_Service CRUD operation.

    This is a thin wrapper layer — it does NOT perform any direct S3 or DDB
    writes. All mutations go through the validated CRUD classes which enforce
    all validation rules from Requirements 2–5.

    Property 14: Chat actions go through validated CRUD — chat mutation
    produces same state as equivalent direct CRUD call.
    """

    def __init__(self, lab_service) -> None:
        """Initialize the tool router with a reference to the LabService.

        Args:
            lab_service: The LabService instance that owns the CRUD classes.
        """
        self._service = lab_service
        self._tool_handlers: dict[str, Any] = {
            "create_skill": self._create_skill,
            "read_skill": self._read_skill,
            "update_skill": self._update_skill,
            "delete_skill": self._delete_skill,
            "create_specialty_agent": self._create_specialty_agent,
            "read_specialty_agent": self._read_specialty_agent,
            "update_specialty_agent": self._update_specialty_agent,
            "delete_specialty_agent": self._delete_specialty_agent,
            "create_tool_collection": self._create_tool_collection,
            "read_tool_collection": self._read_tool_collection,
            "update_tool_collection": self._update_tool_collection,
            "delete_tool_collection": self._delete_tool_collection,
            "create_workflow": self._create_workflow,
            "read_workflow": self._read_workflow,
            "update_workflow": self._update_workflow,
            "delete_workflow": self._delete_workflow,
        }

    @property
    def available_tools(self) -> list[str]:
        """Return the list of available tool names."""
        return list(self._tool_handlers.keys())

    async def invoke(self, tool_name: str, arguments: dict) -> ToolCallResult:
        """Invoke a CRUD tool by name with the given arguments.

        Routes to the appropriate Lab_Service CRUD operation. Returns a
        ToolCallResult indicating success or failure.

        Args:
            tool_name: The name of the tool to invoke.
            arguments: The tool arguments dict.

        Returns:
            A ToolCallResult with the operation outcome.
        """
        handler = self._tool_handlers.get(tool_name)
        if handler is None:
            return ToolCallResult(
                success=False,
                error=f"Unknown tool: {tool_name}",
            )

        try:
            result = await handler(arguments)
            return ToolCallResult(success=True, data=result)
        except Exception as e:
            logger.warning(
                "CRUD tool %s failed: %s", tool_name, str(e), exc_info=True
            )
            return ToolCallResult(success=False, error=str(e))

    # --- Skill tools ---

    async def _create_skill(self, args: dict) -> dict:
        result = await self._service.handle({
            "action": "CreateSkillDraft",
            "name": args["name"],
            "skillMd": args["skill_md"],
        })
        return result

    async def _read_skill(self, args: dict) -> dict:
        result = await self._service.handle({
            "action": "ReadSkillDraft",
            "assetId": args["asset_id"],
        })
        return result

    async def _update_skill(self, args: dict) -> dict:
        result = await self._service.handle({
            "action": "UpdateSkillMd",
            "assetId": args["asset_id"],
            "skillMd": args["skill_md"],
        })
        return result

    async def _delete_skill(self, args: dict) -> dict:
        result = await self._service.handle({
            "action": "DeleteSkillDraft",
            "assetId": args["asset_id"],
        })
        return result

    # --- Specialty Agent tools ---

    async def _create_specialty_agent(self, args: dict) -> dict:
        payload: dict[str, Any] = {
            "action": "CreateSpecialtyAgentDraft",
            "name": args["name"],
            "persona": args["persona"],
        }
        if "compatible_skills" in args and args["compatible_skills"] is not None:
            payload["compatibleSkills"] = args["compatible_skills"]
        result = await self._service.handle(payload)
        return result

    async def _read_specialty_agent(self, args: dict) -> dict:
        result = await self._service.handle({
            "action": "ReadSpecialtyAgentDraft",
            "assetId": args["asset_id"],
        })
        return result

    async def _update_specialty_agent(self, args: dict) -> dict:
        payload: dict[str, Any] = {
            "action": "UpdateSpecialtyAgentDraft",
            "assetId": args["asset_id"],
        }
        if "persona" in args and args["persona"] is not None:
            payload["persona"] = args["persona"]
        if "compatible_skills" in args and args["compatible_skills"] is not None:
            payload["compatibleSkills"] = args["compatible_skills"]
        result = await self._service.handle(payload)
        return result

    async def _delete_specialty_agent(self, args: dict) -> dict:
        result = await self._service.handle({
            "action": "DeleteSpecialtyAgentDraft",
            "assetId": args["asset_id"],
        })
        return result

    # --- Tool Collection tools ---

    async def _create_tool_collection(self, args: dict) -> dict:
        tool_refs = args.get("tool_refs", [])
        result = await self._service.handle({
            "action": "CreateToolCollectionDraft",
            "name": args["name"],
            "toolRefs": tool_refs,
        })
        return result

    async def _read_tool_collection(self, args: dict) -> dict:
        result = await self._service.handle({
            "action": "ReadToolCollectionDraft",
            "assetId": args["asset_id"],
        })
        return result

    async def _update_tool_collection(self, args: dict) -> dict:
        payload: dict[str, Any] = {
            "action": "UpdateToolCollectionDraft",
            "assetId": args["asset_id"],
        }
        if "name" in args and args["name"] is not None:
            payload["name"] = args["name"]
        if "tool_refs" in args and args["tool_refs"] is not None:
            payload["toolRefs"] = args["tool_refs"]
        result = await self._service.handle(payload)
        return result

    async def _delete_tool_collection(self, args: dict) -> dict:
        result = await self._service.handle({
            "action": "DeleteToolCollectionDraft",
            "assetId": args["asset_id"],
            "force": bool(args.get("force", False)),
        })
        return result

    # --- Workflow tools ---

    async def _create_workflow(self, args: dict) -> dict:
        result = await self._service.handle({
            "action": "CreateWorkflowDraft",
            "name": args["name"],
            "steps": args.get("steps", []),
        })
        return result

    async def _read_workflow(self, args: dict) -> dict:
        result = await self._service.handle({
            "action": "ReadWorkflowDraft",
            "assetId": args["asset_id"],
        })
        return result

    async def _update_workflow(self, args: dict) -> dict:
        payload: dict[str, Any] = {
            "action": "UpdateWorkflowDraft",
            "assetId": args["asset_id"],
        }
        if "name" in args and args["name"] is not None:
            payload["name"] = args["name"]
        if "steps" in args and args["steps"] is not None:
            payload["steps"] = args["steps"]
        result = await self._service.handle(payload)
        return result

    async def _delete_workflow(self, args: dict) -> dict:
        result = await self._service.handle({
            "action": "DeleteWorkflowDraft",
            "assetId": args["asset_id"],
        })
        return result


# ---------------------------------------------------------------------------
# LabChatAgent
# ---------------------------------------------------------------------------


@dataclass
class ChatTurnResult:
    """Result of processing a single chat turn."""

    response: str
    affected_asset_ids: list[str] = field(default_factory=list)
    tool_calls_made: list[dict] = field(default_factory=list)


class LabChatAgent:
    """Inventory-aware conversational agent for the MediaWorks Lab.

    On each turn, the agent:
    1. Builds an Inventory_Snapshot via a single Query(PK=actor_id, status=active)
    2. Injects the snapshot into the system prompt as a structured listing
    3. Processes the user message with access to CRUD tools as thin wrappers

    The chat agent has NO direct S3/DDB write permission. All mutations go
    through the validated CRUD layer (Property 14).

    Requirements: 6.1, 6.2, 6.3
    Properties: 13, 14
    """

    def __init__(self, lab_service) -> None:
        """Initialize the LabChatAgent.

        Args:
            lab_service: The LabService instance. The chat agent routes all
                mutations through this service's validated CRUD operations.
        """
        self._service = lab_service
        self._tool_router = CRUDToolRouter(lab_service)
        self._index = lab_service.index
        self._ctx = lab_service.ctx
        self._memory = ConversationalMemory(s3=lab_service.s3, ctx=self._ctx)

    @property
    def tool_definitions(self) -> list[ToolDefinition]:
        """Return the list of CRUD tool definitions available to the agent."""
        return CRUD_TOOL_DEFINITIONS

    @property
    def tool_router(self) -> CRUDToolRouter:
        """Return the CRUD tool router for external access."""
        return self._tool_router

    @property
    def memory(self) -> ConversationalMemory:
        """Return the ConversationalMemory instance for this agent."""
        return self._memory

    async def build_inventory_snapshot(self) -> InventorySnapshot:
        """Build the current turn's inventory snapshot.

        Performs the unified-platform query — one ``query_pk`` against
        the actor's drafts partition (skills, specialty agents,
        workflows) and one against the flat tool-collections partition.
        The snapshot is a consistent slice at query time.

        Property 13: snapshot equals drafts owned by actor at query time,
        no cross-actor drafts.

        Returns:
            An InventorySnapshot grouped by category, with typed records.
        """
        return await InventorySnapshot.build(
            index=self._index,
            actor_id=self._ctx.actor_id,
        )

    async def disambiguate_draft(
        self,
        name: str,
        category: Category,
        snapshot: Optional[InventorySnapshot] = None,
    ) -> DisambiguationResult:
        """Resolve a draft by name within a category.

        Implements Requirement 6.6: If the user refers to a draft by name
        and multiple drafts in the same category match, ask the user to
        disambiguate. If zero match, suggest nearest alternatives.

        Never mutates when ambiguous.

        Args:
            name: The draft name to search for.
            category: The category to search within.
            snapshot: Optional pre-built snapshot. If None, builds a fresh one.

        Returns:
            A DisambiguationResult indicating the outcome.
        """
        if snapshot is None:
            snapshot = await self.build_inventory_snapshot()
        return disambiguate(snapshot, name, category)

    def get_system_prompt(self, snapshot: InventorySnapshot) -> str:
        """Build the system prompt with the inventory snapshot and tool catalog injected.

        Args:
            snapshot: The current turn's InventorySnapshot.

        Returns:
            The complete system prompt string.
        """
        # Get tool catalog from the gateway browser if available
        tool_catalog = None
        try:
            gateway_browser = self._service.gateway_browser
            projection = gateway_browser.get_projection(self._ctx)
            if projection:
                tool_catalog = [
                    {"name": t.name, "description": t.description}
                    for t in projection.tools
                ]
            else:
                # Try to browse (lazy populate)
                try:
                    result = gateway_browser.browse(self._ctx)
                    tool_catalog = [
                        {"name": t.name, "description": t.description}
                        for t in result.projection.tools
                    ]
                except Exception:
                    pass
        except Exception:
            pass

        return build_system_prompt(snapshot, tool_catalog)

    async def handle_message(
        self,
        message: str,
        tool_calls: Optional[list[dict]] = None,
        history: Optional[list[dict]] = None,
    ) -> ChatTurnResult:
        """Handle a user message for a single chat turn using LLM inference.

        This method:
        1. Builds a fresh Inventory_Snapshot (Requirement 6.1)
        2. Constructs the system prompt with snapshot + tool catalog (Requirement 6.2)
        3. Invokes the LLM with CRUD tools available (Requirement 6.3)
        4. Processes any tool calls through the validated CRUD layer

        Args:
            message: The user's chat message.
            tool_calls: Optional list of tool calls to execute (for multi-turn).
            history: Optional conversation history for multi-turn context.

        Returns:
            A ChatTurnResult with the response and affected asset IDs.
        """
        import json as _json
        import os

        # Step 1: Build fresh inventory snapshot (Requirement 6.1, Property 13)
        snapshot = await self.build_inventory_snapshot()

        # Step 2: Build system prompt with inventory + tool catalog (Requirement 6.2)
        system_prompt = self.get_system_prompt(snapshot)

        logger.info(
            "Chat turn for actor=%s, inventory_count=%d, message_len=%d, history_len=%d",
            self._ctx.actor_id,
            snapshot.total_count,
            len(message),
            len(history) if history else 0,
        )

        # Step 3: Invoke LLM with CRUD tools
        affected_asset_ids: list[str] = []
        tool_results: list[dict] = []

        # If the caller supplied explicit ``tool_calls`` (used by the
        # service's multi-turn API and by unit tests), execute them
        # directly through the validated CRUD router and skip the LLM
        # invocation. This keeps test runs deterministic and matches
        # Property 14 — chat-driven mutations go through the same
        # validated CRUD path as direct service calls.
        if tool_calls:
            for tool_call in tool_calls:
                tool_name = tool_call.get("name", "")
                arguments = tool_call.get("arguments", {}) or {}
                call_result = await self._tool_router.invoke(tool_name, arguments)
                if call_result.success:
                    entry: dict[str, Any] = {
                        "tool": tool_name,
                        "success": True,
                        "data": call_result.data,
                    }
                    tool_results.append(entry)
                    # Track affected asset_id either from the response (on
                    # create) or from the explicit argument (on update /
                    # delete / read).
                    data = call_result.data
                    if isinstance(data, dict) and "assetId" in data:
                        affected_asset_ids.append(data["assetId"])
                    elif "asset_id" in arguments and tool_name.startswith(
                        ("update_", "delete_")
                    ):
                        affected_asset_ids.append(arguments["asset_id"])
                else:
                    tool_results.append(
                        {
                            "tool": tool_name,
                            "success": False,
                            "error": call_result.error,
                        }
                    )

            response_text = self._build_response_summary(
                message, snapshot, tool_results
            )

            # Persist a turn record only when mutations occurred
            # (Requirement 6.4).
            if affected_asset_ids:
                await self._memory.persist_turn(
                    user_message=message,
                    affected_asset_ids=affected_asset_ids,
                    tool_calls_made=tool_results,
                )

            return ChatTurnResult(
                response=response_text,
                affected_asset_ids=affected_asset_ids,
                tool_calls_made=tool_results,
            )

        try:
            from strands import Agent, tool as strands_tool
            from strands.models import BedrockModel
            from botocore.config import Config as BotoConfig

            boto_cfg = BotoConfig(read_timeout=300, connect_timeout=10, retries={"max_attempts": 4, "mode": "standard"})
            from model.defaults import model_for
            model_id = model_for("lab_chat")
            model = BedrockModel(model_id=model_id, boto_client_config=boto_cfg)

            # Build Strands tools from CRUD definitions
            crud_tools = self._build_strands_tools()

            agent = Agent(
                model=model,
                system_prompt=system_prompt,
                tools=crud_tools,
            )

            # If we have conversation history, build the full prompt with context
            if history and len(history) > 0:
                # Format history as a conversation context prefix
                history_lines = []
                for turn in history:
                    role = turn.get("role", "user")
                    content = turn.get("content", "")
                    if role == "user":
                        history_lines.append(f"User: {content}")
                    else:
                        history_lines.append(f"Assistant: {content}")
                history_context = "\n\n".join(history_lines)
                full_prompt = f"<conversation_history>\n{history_context}\n</conversation_history>\n\nUser's latest message: {message}"
            else:
                full_prompt = message

            # Run the agent
            result = agent(full_prompt)
            response_text = str(result)

            # Check if any CRUD tools were called by inspecting the agent's history
            # The tool calls are tracked via our wrapper functions
            affected_asset_ids = list(self._pending_affected_ids)
            tool_results = list(self._pending_tool_results)
            self._pending_affected_ids = []
            self._pending_tool_results = []

        except Exception as e:
            logger.error("LLM inference failed: %s", e, exc_info=True)
            # Fallback to stub response
            response_text = (
                f"I see your Lab has {snapshot.total_count} draft(s). "
                f"I encountered an error processing your message: {str(e)[:200]}. "
                f"How can I help you manage your inventory?"
            )

        # Step 4: Persist turn record if mutations occurred (Requirement 6.4)
        if affected_asset_ids:
            await self._memory.persist_turn(
                user_message=message,
                affected_asset_ids=affected_asset_ids,
                tool_calls_made=tool_results,
            )

        return ChatTurnResult(
            response=response_text,
            affected_asset_ids=affected_asset_ids,
            tool_calls_made=tool_results,
        )

    def _build_strands_tools(self) -> list:
        """Build Strands-compatible tool functions from CRUD definitions.

        Each tool is a Python function decorated with @tool that routes
        through the validated CRUD layer.
        """
        import asyncio
        import concurrent.futures
        from strands import tool as strands_tool

        # Track mutations for the current turn
        self._pending_affected_ids: list[str] = []
        self._pending_tool_results: list[dict] = []

        service = self._service
        pending_ids = self._pending_affected_ids
        pending_results = self._pending_tool_results

        def _run_async(coro):
            """Run an async coroutine from a sync context (Strands tool)."""
            try:
                loop = asyncio.get_running_loop()
                # We're inside an async context — use a thread to run the coroutine
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(asyncio.run, coro)
                    return future.result(timeout=30)
            except RuntimeError:
                # No running loop — safe to use asyncio.run directly
                return asyncio.run(coro)

        @strands_tool
        def create_skill(name: str, skill_md: str) -> str:
            """Create a new skill draft with a name and SKILL.md content.
            Args:
                name: The skill name (max 128 chars).
                skill_md: The SKILL.md content describing the skill's purpose and instructions.
            """
            import json
            try:
                result = _run_async(
                    service.handle({"action": "CreateSkillDraft", "name": name, "skillMd": skill_md})
                )
                if isinstance(result, dict) and "assetId" in result:
                    pending_ids.append(result["assetId"])
                pending_results.append({"tool": "create_skill", "success": True, "data": result})
                return json.dumps(result) if isinstance(result, dict) else str(result)
            except Exception as e:
                pending_results.append({"tool": "create_skill", "success": False, "error": str(e)})
                return f"Error creating skill: {e}"

        @strands_tool
        def read_skill(asset_id: str) -> str:
            """Read the full SKILL.md content of a skill draft. Use this to see what a skill contains before updating it.
            Args:
                asset_id: The skill's asset ID.
            """
            import json
            try:
                result = _run_async(
                    service.handle({"action": "ReadSkillDraft", "assetId": asset_id})
                )
                if isinstance(result, dict):
                    return result.get("skillMd", json.dumps(result))
                return str(result)
            except Exception as e:
                return f"Error reading skill: {e}"

        @strands_tool
        def update_skill(asset_id: str, skill_md: str) -> str:
            """Update the SKILL.md content of an existing skill draft.
            Args:
                asset_id: The skill's asset ID.
                skill_md: The new SKILL.md content.
            """
            import json
            try:
                result = _run_async(
                    service.handle({"action": "UpdateSkillMd", "assetId": asset_id, "skillMd": skill_md})
                )
                pending_ids.append(asset_id)
                pending_results.append({"tool": "update_skill", "success": True, "data": result})
                return json.dumps(result) if isinstance(result, dict) else str(result)
            except Exception as e:
                pending_results.append({"tool": "update_skill", "success": False, "error": str(e)})
                return f"Error updating skill: {e}"

        @strands_tool
        def delete_skill(asset_id: str) -> str:
            """Delete a skill draft by its asset ID.
            Args:
                asset_id: The skill's asset ID to delete.
            """
            import json
            try:
                result = _run_async(
                    service.handle({"action": "DeleteSkillDraft", "assetId": asset_id})
                )
                pending_ids.append(asset_id)
                pending_results.append({"tool": "delete_skill", "success": True, "data": result})
                return json.dumps(result) if isinstance(result, dict) else str(result)
            except Exception as e:
                pending_results.append({"tool": "delete_skill", "success": False, "error": str(e)})
                return f"Error deleting skill: {e}"

        @strands_tool
        def create_specialty_agent(name: str, persona: str, compatible_skills: str = "") -> str:
            """Create a new specialty agent draft.
            Args:
                name: The agent name (1-100 chars).
                persona: The persona prompt (1-5000 chars) describing the agent's role and behavior.
                compatible_skills: Comma-separated list of skill asset IDs this agent is compatible with (optional).
            """
            import json
            try:
                payload: dict = {"action": "CreateSpecialtyAgentDraft", "name": name, "persona": persona}
                if compatible_skills:
                    payload["compatibleSkills"] = [s.strip() for s in compatible_skills.split(",") if s.strip()]
                result = _run_async(service.handle(payload))
                if isinstance(result, dict) and "assetId" in result:
                    pending_ids.append(result["assetId"])
                pending_results.append({"tool": "create_specialty_agent", "success": True, "data": result})
                return json.dumps(result) if isinstance(result, dict) else str(result)
            except Exception as e:
                pending_results.append({"tool": "create_specialty_agent", "success": False, "error": str(e)})
                return f"Error creating specialty agent: {e}"

        @strands_tool
        def create_tool_collection(name: str, tool_names: str) -> str:
            """Create a new tool collection draft with selected tools.
            Args:
                name: The collection name (max 100 chars).
                tool_names: Comma-separated list of tool names to include (use exact names from the tool catalog).
            """
            import json
            try:
                tool_refs = [{"name": t.strip(), "version": "1.0"} for t in tool_names.split(",") if t.strip()]
                result = _run_async(
                    service.handle({"action": "CreateToolCollectionDraft", "name": name, "toolRefs": tool_refs})
                )
                if isinstance(result, dict) and "assetId" in result:
                    pending_ids.append(result["assetId"])
                pending_results.append({"tool": "create_tool_collection", "success": True, "data": result})
                return json.dumps(result) if isinstance(result, dict) else str(result)
            except Exception as e:
                pending_results.append({"tool": "create_tool_collection", "success": False, "error": str(e)})
                return f"Error creating tool collection: {e}"

        @strands_tool
        def create_workflow(name: str, steps_json: str) -> str:
            """Create a new workflow draft with ordered steps.
            Args:
                name: The workflow name (max 100 chars).
                steps_json: JSON array of workflow steps. Each step: {"stepId": "s1", "agent": {"kind": "core"}, "skill": {"assetId": null}, "toolCollection": {"assetId": null}, "prompt": "...", "inputs": []}
            """
            import json
            try:
                steps = json.loads(steps_json)
                result = _run_async(
                    service.handle({"action": "CreateWorkflowDraft", "name": name, "steps": steps})
                )
                if isinstance(result, dict) and "assetId" in result:
                    pending_ids.append(result["assetId"])
                pending_results.append({"tool": "create_workflow", "success": True, "data": result})
                return json.dumps(result) if isinstance(result, dict) else str(result)
            except Exception as e:
                pending_results.append({"tool": "create_workflow", "success": False, "error": str(e)})
                return f"Error creating workflow: {e}"

        # --- Specialty Agent read/update/delete ---

        @strands_tool
        def read_specialty_agent(asset_id: str) -> str:
            """Read a specialty agent draft to see its persona and compatible skills.
            Args:
                asset_id: The agent's asset ID.
            """
            import json
            try:
                result = _run_async(
                    service.handle({"action": "ReadSpecialtyAgentDraft", "assetId": asset_id})
                )
                return json.dumps(result) if isinstance(result, dict) else str(result)
            except Exception as e:
                return f"Error reading specialty agent: {e}"

        @strands_tool
        def update_specialty_agent(asset_id: str, persona: str = "", compatible_skills: str = "") -> str:
            """Update a specialty agent's persona or compatible skills.
            Args:
                asset_id: The agent's asset ID.
                persona: New persona text (leave empty to keep current).
                compatible_skills: Comma-separated skill asset IDs (leave empty to keep current).
            """
            import json
            try:
                payload: dict = {"action": "UpdateSpecialtyAgentDraft", "assetId": asset_id}
                if persona:
                    payload["persona"] = persona
                if compatible_skills:
                    payload["compatibleSkills"] = [s.strip() for s in compatible_skills.split(",") if s.strip()]
                result = _run_async(service.handle(payload))
                pending_ids.append(asset_id)
                pending_results.append({"tool": "update_specialty_agent", "success": True, "data": result})
                return json.dumps(result) if isinstance(result, dict) else str(result)
            except Exception as e:
                pending_results.append({"tool": "update_specialty_agent", "success": False, "error": str(e)})
                return f"Error updating specialty agent: {e}"

        @strands_tool
        def delete_specialty_agent(asset_id: str) -> str:
            """Delete a specialty agent draft.
            Args:
                asset_id: The agent's asset ID to delete.
            """
            import json
            try:
                result = _run_async(
                    service.handle({"action": "DeleteSpecialtyAgentDraft", "assetId": asset_id})
                )
                pending_ids.append(asset_id)
                pending_results.append({"tool": "delete_specialty_agent", "success": True, "data": result})
                return json.dumps(result) if isinstance(result, dict) else str(result)
            except Exception as e:
                pending_results.append({"tool": "delete_specialty_agent", "success": False, "error": str(e)})
                return f"Error deleting specialty agent: {e}"

        # --- Tool Collection read/update/delete ---

        @strands_tool
        def read_tool_collection(asset_id: str) -> str:
            """Read a tool collection to see its name and included tools.
            Args:
                asset_id: The collection's asset ID.
            """
            import json
            try:
                result = _run_async(
                    service.handle({"action": "ReadToolCollectionDraft", "assetId": asset_id})
                )
                return json.dumps(result) if isinstance(result, dict) else str(result)
            except Exception as e:
                return f"Error reading tool collection: {e}"

        @strands_tool
        def update_tool_collection(asset_id: str, name: str = "", tool_names: str = "") -> str:
            """Update a tool collection's name or tools.
            Args:
                asset_id: The collection's asset ID.
                name: New collection name (leave empty to keep current).
                tool_names: Comma-separated tool names to set (leave empty to keep current).
            """
            import json
            try:
                payload: dict = {"action": "UpdateToolCollectionDraft", "assetId": asset_id}
                if name:
                    payload["name"] = name
                if tool_names:
                    payload["toolRefs"] = [{"name": t.strip(), "version": "1.0"} for t in tool_names.split(",") if t.strip()]
                result = _run_async(service.handle(payload))
                pending_ids.append(asset_id)
                pending_results.append({"tool": "update_tool_collection", "success": True, "data": result})
                return json.dumps(result) if isinstance(result, dict) else str(result)
            except Exception as e:
                pending_results.append({"tool": "update_tool_collection", "success": False, "error": str(e)})
                return f"Error updating tool collection: {e}"

        @strands_tool
        def delete_tool_collection(asset_id: str, force: bool = False) -> str:
            """Delete a tool collection. Refuses with error 'still_referenced'
            (listing the agents) while a specialty agent still selects it.
            Args:
                asset_id: The collection's asset ID to delete.
                force: Delete despite those references. Only after telling the
                    user which agents would lose the tools.
            """
            import json
            try:
                result = _run_async(
                    service.handle({
                        "action": "DeleteToolCollectionDraft",
                        "assetId": asset_id,
                        "force": force,
                    })
                )
                pending_ids.append(asset_id)
                pending_results.append({"tool": "delete_tool_collection", "success": True, "data": result})
                return json.dumps(result) if isinstance(result, dict) else str(result)
            except Exception as e:
                pending_results.append({"tool": "delete_tool_collection", "success": False, "error": str(e)})
                return f"Error deleting tool collection: {e}"

        # --- Workflow read/update/delete ---

        @strands_tool
        def read_workflow(asset_id: str) -> str:
            """Read a workflow draft to see its steps and configuration.
            Args:
                asset_id: The workflow's asset ID.
            """
            import json
            try:
                result = _run_async(
                    service.handle({"action": "ReadWorkflowDraft", "assetId": asset_id})
                )
                return json.dumps(result) if isinstance(result, dict) else str(result)
            except Exception as e:
                return f"Error reading workflow: {e}"

        @strands_tool
        def update_workflow(asset_id: str, name: str = "", steps_json: str = "") -> str:
            """Update a workflow's name or steps.
            Args:
                asset_id: The workflow's asset ID.
                name: New workflow name (leave empty to keep current).
                steps_json: JSON array of workflow steps (leave empty to keep current).
            """
            import json
            try:
                payload: dict = {"action": "UpdateWorkflowDraft", "assetId": asset_id}
                if name:
                    payload["name"] = name
                if steps_json:
                    payload["steps"] = json.loads(steps_json)
                result = _run_async(service.handle(payload))
                pending_ids.append(asset_id)
                pending_results.append({"tool": "update_workflow", "success": True, "data": result})
                return json.dumps(result) if isinstance(result, dict) else str(result)
            except Exception as e:
                pending_results.append({"tool": "update_workflow", "success": False, "error": str(e)})
                return f"Error updating workflow: {e}"

        @strands_tool
        def delete_workflow(asset_id: str) -> str:
            """Delete a workflow draft.
            Args:
                asset_id: The workflow's asset ID to delete.
            """
            import json
            try:
                result = _run_async(
                    service.handle({"action": "DeleteWorkflowDraft", "assetId": asset_id})
                )
                pending_ids.append(asset_id)
                pending_results.append({"tool": "delete_workflow", "success": True, "data": result})
                return json.dumps(result) if isinstance(result, dict) else str(result)
            except Exception as e:
                pending_results.append({"tool": "delete_workflow", "success": False, "error": str(e)})
                return f"Error deleting workflow: {e}"

        return [
            create_skill, read_skill, update_skill, delete_skill,
            read_specialty_agent, create_specialty_agent, update_specialty_agent, delete_specialty_agent,
            read_tool_collection, create_tool_collection, update_tool_collection, delete_tool_collection,
            read_workflow, create_workflow, update_workflow, delete_workflow,
        ]

    def _build_response_summary(
        self,
        message: str,
        snapshot: InventorySnapshot,
        tool_results: list[dict],
    ) -> str:
        """Fallback response when LLM is unavailable."""
        if not tool_results:
            return (
                f"I see your Lab has {snapshot.total_count} draft(s). "
                f"How can I help you manage your inventory?"
            )

        successful = [r for r in tool_results if r["success"]]
        failed = [r for r in tool_results if not r["success"]]

        parts = []
        if successful:
            tools_used = ", ".join(r["tool"] for r in successful)
            parts.append(f"Completed: {tools_used}")
        if failed:
            errors = "; ".join(
                f"{r['tool']}: {r['error']}" for r in failed
            )
            parts.append(f"Failed: {errors}")

        return " | ".join(parts)
