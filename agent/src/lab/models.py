"""Data models for the unified MediaWorks Platform.

Pure data containers — no business logic. Uses dataclasses with Literal types
for enum-like fields.

This module exposes one dataclass per asset category — Skill, SpecialtyAgent,
Workflow, ToolCollection — all extending a shared AssetCommon. Lifecycle
stage is carried by the `status` field; there is no separate Draft type
(Requirement 15).

The two helpers `to_index_item` and `from_index_item` round-trip an asset
to the AssetIndex DynamoDB item shape used in the design (camelCase DDB
attributes, snake_case dataclass fields).

Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 15.1, 15.2, 15.3
Properties: 2 (AssetIndex shape invariants)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional


# ---------------------------------------------------------------------------
# Type aliases for Literal enums
# ---------------------------------------------------------------------------

Category = Literal["skill", "specialty-agent", "tool-collection", "workflow", "hook"]
Status = Literal["draft", "published", "deprecated"]
Scope = Literal["private", "group", "org"]

# Other existing literals retained for sibling modules.
HarnessSubject = Literal["skill", "specialty", "workflow", "tool"]
HarnessRunStatus = Literal["succeeded", "failed"]
ReadinessStatus = Literal["ready", "not_ready"]
AgentKind = Literal["core", "specialty"]
SampleRefKind = Literal["inline", "s3"]


# ---------------------------------------------------------------------------
# Workflow composition primitives
# ---------------------------------------------------------------------------


@dataclass
class AgentRef:
    """Reference to an agent in a workflow step."""

    kind: AgentKind
    ref: Optional[str] = None  # e.g. "task-agent" for core
    asset_id: Optional[str] = None  # for specialty agents


@dataclass
class SkillRef:
    """Optional skill binding in a workflow step."""

    asset_id: Optional[str] = None


@dataclass
class ToolCollectionRef:
    """Optional tool-collection binding in a workflow step."""

    asset_id: Optional[str] = None


@dataclass
class WorkflowStep:
    """A single step in a Workflow.

    Each step binds an agent, optional skill, optional tool collection,
    a prompt template, and explicit input dependencies on prior steps.
    """

    step_id: str
    agent: AgentRef
    skill: SkillRef
    tool_collection: ToolCollectionRef
    prompt: str
    inputs: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Tool and Gateway models
# ---------------------------------------------------------------------------


@dataclass
class ToolRef:
    """A reference to a Gateway tool by name and version."""

    name: str
    version: str


@dataclass
class ToolCollectionSnapshot:
    """The tool refs one Tool_Collection held when an agent version was published.

    A published agent version stores its ``tool_collections`` as bare ids, and a
    tool collection is flat and mutable — there is no published form to pin to. So
    resolving those ids live at every invoke meant an immutable version's tool set
    changed whenever the owner edited the draft collection, and vanished when they
    deleted it. This is that missing pin: ``promote_asset`` resolves each id once
    and freezes the answer onto the version row.

    Per-collection rather than a flattened union of refs, because the external
    opt-in (``integration_config.external_tool_collections``) selects by
    collection id — flattening would make that filter unrepresentable.

    Field names are deliberately the ones
    :mod:`custom_agent_resolver` already reads off a resolved collection
    (``asset_id`` via ``_collection_id``, ``tool_refs`` via
    ``_collection_tool_refs``), so a snapshot can be handed to the resolver in
    place of a live AssetIndex row with no branching there.
    """

    asset_id: str
    name: str
    tool_refs: list[ToolRef] = field(default_factory=list)


@dataclass
class GatewayToolDescriptor:
    """Descriptor for a single tool exposed by the AgentCore_Gateway."""

    name: str
    description: str
    version: str
    input_schema: dict = field(default_factory=dict)


@dataclass
class GatewayProjection:
    """A per-session, per-actor read-only projection of Gateway tool listings."""

    retrieved_at: int  # epoch ms
    actor_id: str
    tools: list[GatewayToolDescriptor] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Test Harness models
# ---------------------------------------------------------------------------


@dataclass
class SampleRef:
    """Reference to sample input data for a test harness run."""

    kind: SampleRefKind
    value: str


@dataclass
class DraftRef:
    """Reference to the draft under test."""

    category: Category
    asset_id: str


@dataclass
class HarnessError:
    """Error details for a failed test harness run."""

    cause: str
    step_id: Optional[str] = None


@dataclass
class TestHarnessRunRecord:
    """Persisted record of a single Test_Harness_Run."""

    run_id: str
    subject: HarnessSubject
    draft_ref: DraftRef
    sample_ref: SampleRef
    status: HarnessRunStatus
    outputs: list = field(default_factory=list)
    logs: str = ""
    started_at: int = 0  # epoch ms
    ended_at: int = 0  # epoch ms
    error: Optional[HarnessError] = None


# ---------------------------------------------------------------------------
# Promotion Readiness models
# ---------------------------------------------------------------------------


@dataclass
class ReadinessEntry:
    """Readiness status for a single asset in a Promotion_Readiness_Check."""

    category: Category
    asset_id: str
    status: ReadinessStatus
    failures: list[str] = field(default_factory=list)


@dataclass
class ReadinessReport:
    """Structured result of a Promotion_Readiness_Check for a Workflow."""

    workflow_asset_id: str
    overall_status: ReadinessStatus
    entries: list[ReadinessEntry] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Composition models
# ---------------------------------------------------------------------------


@dataclass
class ComposedInvocation:
    """The final composed prompt pair sent to the agent runtime."""

    system_prompt: str
    user_prompt: str


# ---------------------------------------------------------------------------
# Unified asset model — Skill, SpecialtyAgent, Workflow, ToolCollection
# ---------------------------------------------------------------------------


@dataclass
class PublishPolicy:
    """Authority record attached to a Published_Record.

    Authority is independent of visibility (Requirement 3): Scope governs
    who can READ, PublishPolicy governs who can MUTATE.
    """

    owner_ids: list[str]
    contributor_ids: list[str] = field(default_factory=list)


@dataclass
class ScopeHistoryEntry:
    """Single append-only audit entry for a scope mutation.

    `from_scope` is None on the initial publish (Requirement 13.2).
    `group_ids` is required when `to_scope == "group"` and otherwise
    expected to be None.
    """

    from_scope: Optional[Scope]
    to_scope: Scope
    actor_id: str
    timestamp: str  # ISO-8601 UTC
    group_ids: Optional[list[str]] = None


@dataclass
class AssetCommon:
    """Fields shared by every asset category.

    v2 lifecycle model (asset-lifecycle-redesign):
      - ``version is None``       → working copy (mutable, owner-only).
      - ``version is not None``   → immutable version snapshot.
      - ``deprecated``            → flag on a version, hides it from group / org
                                    listings unless include_deprecated is set.

    ``status`` is DERIVED, never stored: ``__post_init__`` computes it from
    ``(version, deprecated)``, so the two can't disagree. The only meaning a
    passed ``status`` keeps is the legacy ``"deprecated"`` (rows written before
    the ``deprecated`` flag), which sets the flag.
    """

    asset_id: str  # working-copy id and version-lineage id are the same
    category: Category
    name: str
    status: Status  # derived in __post_init__ from (version, deprecated)
    scope: Scope
    s3_prefix: str
    created_at: int  # epoch ms
    updated_at: int  # epoch ms

    # Human-authored one-liner used for discovery. Read by
    # ``capability/descriptors.py`` for every projectable category (it already
    # defaults to "" there), and by ``list_custom_agents`` so the orchestrator
    # can pick an agent by purpose rather than by name alone.
    description: str = ""

    # Versioned (v2 immutable snapshot) fields. None on working copies.
    version: Optional[int] = None
    deprecated: bool = False
    publish_policy: Optional[PublishPolicy] = None
    scope_history: list[ScopeHistoryEntry] = field(default_factory=list)
    current: bool = False
    promoted_from: Optional[str] = None  # originating asset_id (kept for v1 compat)
    last_published_as: Optional[tuple[str, int]] = None  # legacy
    cloned_from: Optional[tuple[str, int]] = None  # set when this row was clone-created

    def __post_init__(self) -> None:
        if self.status == "deprecated":
            self.deprecated = True
        self.status = self.derive_status(self.version, self.deprecated)

    @staticmethod
    def derive_status(version: Optional[int], deprecated: bool) -> "Status":
        """Compute the legacy status string from the v2 fields."""
        if version is None:
            return "draft"
        if deprecated:
            return "deprecated"
        return "published"


@dataclass
class Skill(AssetCommon):
    """A skill asset.

    Drafts and Published_Records share this type; `status` distinguishes
    them (Requirement 15).
    """

    pass


@dataclass
class IntegrationConfig:
    """Per-agent OIDC settings enabling external HTTP invocation.

    Off by default (``enabled=False``). When enabled, ``issuer`` must be a
    valid HTTPS OIDC issuer URL and ``audience`` the expected ``aud`` claim;
    ``allowed_client_ids`` optionally restricts which ``client_id``/``azp``
    values may invoke the agent (empty means no client-id restriction).
    """

    enabled: bool = False
    issuer: Optional[str] = None  # HTTPS OIDC issuer URL
    audience: Optional[str] = None  # expected aud claim
    allowed_client_ids: list[str] = field(default_factory=list)
    # Owner opt-in subset of tool_collections bindable on the external path
    # (Req 11.1); ⊆ the agent's selected tool_collections. Empty = no external
    # tools bound (fail-closed). Distinct from the full Req 7 allowlist used on
    # the internal / in-editor test paths.
    external_tool_collections: list[str] = field(default_factory=list)


@dataclass
class SpecialtyAgent(AssetCommon):
    """A specialty-agent asset (persona + compatible-skill bindings).

    Extended for Custom Agents with an optional ``model`` (Bedrock inference
    profile id), ``tool_collections`` (Tool_Collection ids), and
    ``integration_config`` (external-invoke OIDC settings). All three are
    optional; an unset ``model`` falls back to the platform default and a ``None``
    ``integration_config`` means external access is not configured.

    ``tool_collections`` holds ids only. On a **published** row those ids are
    accompanied by ``tool_collection_snapshots``, the resolved tool refs frozen at
    publish time, which is what makes the version's tool set immutable; on a
    **working copy** the snapshots are ``None`` and the ids resolve live against
    the collections the owner is currently editing.
    """

    compatible_skills: list[str] = field(default_factory=list)
    model: Optional[str] = None
    tool_collections: list[str] = field(default_factory=list)
    # The resolved tool refs behind ``tool_collections``, frozen at publish time.
    # ``None`` and ``[]`` mean different things and must not be conflated:
    #
    #   None -> NOT pinned. Working copies (which must track the live collection
    #           the owner is editing) and published rows written before this field
    #           existed. The invoke path resolves ``tool_collections`` live.
    #   []   -> pinned to nothing, i.e. this version was published with no tool
    #           collections selected. Resolving live would be wrong.
    #
    # So every read must branch on ``is None``, never on falsiness — `or []` here
    # turns "unknown" into "none" and silently unpins the version.
    tool_collection_snapshots: Optional[list[ToolCollectionSnapshot]] = None
    integration_config: Optional[IntegrationConfig] = None
    # Req 16 — Auth revocation & rollback safety. An explicit, request-time
    # external-access gate that is DELIBERATELY SEPARATE from the immutable
    # version snapshot's ``integration_config``. Effective external access =
    # ``integration_config.enabled AND external_access_enabled``. Defaults to
    # ``True`` so a freshly-published agent whose ``integration_config`` is
    # enabled is reachable; the Owner flips it to ``False`` to REVOKE access
    # immediately and independently of publishing. Because it lives on the row
    # (not inside the frozen snapshot config) and rollback only flips the
    # ``current`` pointer, a rolled-back snapshot cannot silently re-grant
    # revoked access — the gate is resolved at request time from the resolved
    # row, not from the restored snapshot's ``integration_config``.
    external_access_enabled: bool = True


@dataclass
class Workflow(AssetCommon):
    """A workflow asset (ordered steps with Asset_Refs)."""

    steps: list[WorkflowStep] = field(default_factory=list)


@dataclass
class ToolCollection(AssetCommon):
    """A tool-collection asset (flat: no version sub-tier).

    Tool collections live at a single flat S3 prefix and AssetIndex PK
    (`USER#<u>#TOOL_COLLECTIONS`) per the design; lifecycle stage is
    carried solely by the `status` field (Requirement 1.5).
    """

    tool_refs: list[ToolRef] = field(default_factory=list)


@dataclass
class Hook(AssetCommon):
    """A declarative hook asset.

    Hooks are harness-enforced triggers evaluated by the ``HarnessHookProvider``
    on Strands lifecycle events. Authoring is declarative (no user code):

      - ``event``        — the normalized lifecycle event name (e.g.
                           ``after_tool_result``, ``before_tool``,
                           ``session_start``, ``session_end``).
      - ``condition``    — a boolean expression (restricted expr language)
                           evaluated against the normalized event context.
      - ``action_type``  — one of ``render`` | ``notify`` | ``dispatch``.
      - ``action_params``— action-specific parameters (Map).
      - ``priority``     — lower runs first when multiple hooks match.
      - ``enabled``      — toggled on/off without deleting.
      - ``builtin``      — harness-provided default (read-only; clone to edit).
      - ``hook_scope``   — forward-compat targeting filter ``{agent?, skill?}``;
                           None means global (v1 default).
    """

    event: str = ""
    condition: str = ""
    action_type: str = "notify"
    action_params: dict = field(default_factory=dict)
    priority: int = 100
    enabled: bool = True
    builtin: bool = False
    hook_scope: Optional[dict] = None


# ---------------------------------------------------------------------------
# Round-trip helpers: dataclass <-> AssetIndex DDB item
# ---------------------------------------------------------------------------

# Mapping from singular AssetIndex `category` value to the dataclass that
# represents it. This is the only place a category string is paired with
# a Python type, so `from_index_item` can dispatch without importing each
# subclass at every call site.
_CATEGORY_TO_CLASS: dict[str, type[AssetCommon]] = {
    "skill": Skill,
    "specialty-agent": SpecialtyAgent,
    "workflow": Workflow,
    "tool-collection": ToolCollection,
    "hook": Hook,
}


def _publish_policy_to_item(pp: Optional[PublishPolicy]) -> Optional[dict]:
    """Serialize a PublishPolicy to its DDB Map shape, or None for drafts."""
    if pp is None:
        return None
    return {
        "ownerIds": list(pp.owner_ids),
        "contributorIds": list(pp.contributor_ids),
    }


def _publish_policy_from_item(value: Any) -> Optional[PublishPolicy]:
    """Parse a PublishPolicy from its DDB Map shape, or None for drafts."""
    if value is None:
        return None
    return PublishPolicy(
        owner_ids=list(value.get("ownerIds", [])),
        contributor_ids=list(value.get("contributorIds", [])),
    )


def _scope_history_entry_to_item(entry: ScopeHistoryEntry) -> dict:
    """Serialize a single ScopeHistoryEntry to its DDB Map shape."""
    out: dict[str, Any] = {
        # `from` is a Python keyword; the design doc uses `from`/`to` as
        # JSON keys so we preserve those names in the DDB item.
        "from": entry.from_scope,
        "to": entry.to_scope,
        "actorId": entry.actor_id,
        "timestamp": entry.timestamp,
    }
    if entry.group_ids is not None:
        out["groupIds"] = list(entry.group_ids)
    return out


def _scope_history_entry_from_item(item: dict) -> ScopeHistoryEntry:
    """Parse a single ScopeHistoryEntry from its DDB Map shape."""
    return ScopeHistoryEntry(
        from_scope=item.get("from"),
        to_scope=item["to"],
        actor_id=item["actorId"],
        timestamp=item["timestamp"],
        group_ids=list(item["groupIds"]) if "groupIds" in item else None,
    )


def _integration_config_to_item(cfg: Optional[IntegrationConfig]) -> Optional[dict]:
    """Serialize an IntegrationConfig to its DDB Map shape, or None when unset.

    Uses camelCase keys (`allowedClientIds`) to match the AssetIndex
    convention; the config is omitted entirely from the item when None.
    """
    if cfg is None:
        return None
    return {
        "enabled": bool(cfg.enabled),
        "issuer": cfg.issuer,
        "audience": cfg.audience,
        "allowedClientIds": list(cfg.allowed_client_ids),
        "externalToolCollections": list(cfg.external_tool_collections),
    }


def _integration_config_from_item(value: Any) -> Optional[IntegrationConfig]:
    """Parse an IntegrationConfig from its DDB Map shape, or None when absent."""
    if value is None:
        return None
    return IntegrationConfig(
        enabled=bool(value.get("enabled", False)),
        issuer=value.get("issuer"),
        audience=value.get("audience"),
        allowed_client_ids=list(value.get("allowedClientIds", [])),
        external_tool_collections=list(
            value.get("externalToolCollections", [])
        ),
    )


def _tool_collection_snapshots_from_item(
    value: Any,
) -> Optional[list[ToolCollectionSnapshot]]:
    """Parse pinned tool-collection snapshots, or None when the row is unpinned.

    ``None`` in, ``None`` out — deliberately not ``[]``. An absent attribute means
    the row never pinned its collections (a working copy, or a version published
    before the field existed) and the invoke path must resolve them live;
    collapsing that to an empty list would silently bind zero tools instead.
    """
    if value is None:
        return None
    return [
        ToolCollectionSnapshot(
            asset_id=snap["assetId"],
            name=snap.get("name", ""),
            tool_refs=[
                ToolRef(name=ref["name"], version=ref["version"])
                for ref in snap.get("toolRefs", [])
            ],
        )
        for snap in value
    ]


def _published_ref_to_item(ref: Optional[tuple[str, int]]) -> Optional[dict]:
    """Serialize a (publishedId, version) tuple to a DDB Map, or None."""
    if ref is None:
        return None
    return {"publishedId": ref[0], "version": int(ref[1])}


def _published_ref_from_item(value: Any) -> Optional[tuple[str, int]]:
    """Parse a (publishedId, version) DDB Map back into a tuple, or None."""
    if value is None:
        return None
    return (value["publishedId"], int(value["version"]))


def to_index_item(
    asset: AssetCommon,
    *,
    owner_user_id: Optional[str],
    owner_group_id: Optional[str],
    owner_org_id: Optional[str],
) -> dict:
    """Serialize a unified asset to an AssetIndex DDB item.

    Performs the camelCase ↔ snake_case mapping for `publishPolicy`,
    `scopeHistory`, `promotedFrom`, `lastPublishedAs`, `clonedFrom`, and
    `s3Prefix`. The owner attributes are passed in by the caller (the CRUD
    layer) because they are derived from `LabRequestContext`, not from the
    asset itself.

    Drafts MUST be passed `owner_user_id` (the author) with the group/org
    arguments None. Published_Records pass exactly one of the three based
    on the record's scope:
      - scope="private" → owner_user_id only
      - scope="group"   → owner_group_id only
      - scope="org"     → owner_org_id only

    Optional fields that are None are omitted from the item rather than
    written as null, matching the design's "absent means N/A" convention
    for DynamoDB.
    """
    # No "recordType" either: working copy vs. version IS ``version`` (absent = working copy), so a second
    # attribute could only disagree with it. ``recordType`` is written by the outbox alone ("outbox"), which is
    # what the projector's stream filter matches.
    item: dict[str, Any] = {
        "assetId": asset.asset_id,
        "category": asset.category,
        "name": asset.name,
        # No "status": it is derived from (version, deprecated) on read.
        "deprecated": bool(asset.deprecated),
        "scope": asset.scope,
        "s3Prefix": asset.s3_prefix,
        "createdAt": int(asset.created_at),
        "updatedAt": int(asset.updated_at),
        "current": bool(asset.current),
        "scopeHistory": [
            _scope_history_entry_to_item(e) for e in asset.scope_history
        ],
    }

    if asset.version is not None:
        item["version"] = int(asset.version)

    # Omitted when empty, matching the "absent means N/A" convention above.
    if asset.description:
        item["description"] = asset.description

    pp = _publish_policy_to_item(asset.publish_policy)
    if pp is not None:
        item["publishPolicy"] = pp

    if asset.promoted_from is not None:
        item["promotedFrom"] = asset.promoted_from

    last_pub = _published_ref_to_item(asset.last_published_as)
    if last_pub is not None:
        item["lastPublishedAs"] = last_pub

    cloned = _published_ref_to_item(asset.cloned_from)
    if cloned is not None:
        item["clonedFrom"] = cloned

    # Owner attributes — exactly one of these is populated for any given
    # row, but we let the caller decide; we just pass the non-None values
    # through.
    if owner_user_id is not None:
        item["ownerUserId"] = owner_user_id
    if owner_group_id is not None:
        item["ownerGroupId"] = owner_group_id
    if owner_org_id is not None:
        item["ownerOrgId"] = owner_org_id

    # Category-specific fields, kept on the dataclass subclasses.
    if isinstance(asset, SpecialtyAgent):
        item["compatibleSkills"] = list(asset.compatible_skills)
        # Custom Agents extension: model, tool collections, and integration
        # config. Optional fields are omitted when None (absent means N/A).
        if asset.model is not None:
            item["model"] = asset.model
        item["toolCollections"] = list(asset.tool_collections)
        # Absent ≠ empty: written only when pinned, so a working copy (and any
        # row predating the field) round-trips back to ``None`` and keeps
        # resolving live, while a version published with no collections
        # round-trips to ``[]`` and stays pinned to nothing.
        if asset.tool_collection_snapshots is not None:
            item["toolCollectionSnapshots"] = [
                {
                    "assetId": snap.asset_id,
                    "name": snap.name,
                    "toolRefs": [
                        {"name": ref.name, "version": ref.version}
                        for ref in snap.tool_refs
                    ],
                }
                for snap in asset.tool_collection_snapshots
            ]
        integration = _integration_config_to_item(asset.integration_config)
        if integration is not None:
            item["integrationConfig"] = integration
        # Req 16: the external-access gate is always written (unlike the
        # optional snapshot config) so the request-time resolver can read it
        # directly off the row/GSI without a fallback lookup. Kept OUTSIDE
        # ``integrationConfig`` so it is not part of the immutable snapshot.
        item["externalAccessEnabled"] = bool(asset.external_access_enabled)
    elif isinstance(asset, ToolCollection):
        item["toolRefs"] = [
            {"name": ref.name, "version": ref.version}
            for ref in asset.tool_refs
        ]
    elif isinstance(asset, Workflow):
        # Steps are reconstructed from S3 (workflow.json) on read; the
        # AssetIndex row carries metadata only. We deliberately do not
        # serialize steps here.
        pass
    elif isinstance(asset, Hook):
        item["event"] = asset.event
        item["condition"] = asset.condition
        item["action"] = {
            "type": asset.action_type,
            "params": dict(asset.action_params or {}),
        }
        item["priority"] = int(asset.priority)
        item["enabled"] = bool(asset.enabled)
        item["builtin"] = bool(asset.builtin)
        if asset.hook_scope is not None:
            item["hookScope"] = dict(asset.hook_scope)

    return item


def from_index_item(item: dict) -> AssetCommon:
    """Reconstruct a unified asset from an AssetIndex DDB item.

    The category determines which concrete subclass is returned:
        skill            -> Skill
        specialty-agent  -> SpecialtyAgent
        workflow         -> Workflow
        tool-collection  -> ToolCollection

    Camel-case DDB attributes are mapped to snake_case dataclass fields.
    """
    category = item["category"]
    cls = _CATEGORY_TO_CLASS[category]

    common_kwargs: dict[str, Any] = {
        "asset_id": item["assetId"],
        "category": category,
        "name": item.get("name", ""),
        "description": item.get("description", ""),
        # Legacy rows still carry a stored status; __post_init__ honours
        # only its "deprecated" and derives the rest.
        "status": item.get("status", "draft"),
        "scope": item.get("scope", "private"),
        "s3_prefix": item["s3Prefix"],
        "created_at": int(item.get("createdAt", 0)),
        "updated_at": int(item.get("updatedAt", 0)),
        "version": int(item["version"]) if "version" in item else None,
        "deprecated": bool(item.get("deprecated", False)),
        "publish_policy": _publish_policy_from_item(item.get("publishPolicy")),
        "scope_history": [
            _scope_history_entry_from_item(e)
            for e in item.get("scopeHistory", [])
        ],
        "current": bool(item.get("current", False)),
        "promoted_from": item.get("promotedFrom"),
        "last_published_as": _published_ref_from_item(item.get("lastPublishedAs")),
        "cloned_from": _published_ref_from_item(item.get("clonedFrom")),
    }

    if cls is SpecialtyAgent:
        return SpecialtyAgent(
            **common_kwargs,
            compatible_skills=list(item.get("compatibleSkills", [])),
            model=item.get("model"),
            tool_collections=list(item.get("toolCollections", [])),
            tool_collection_snapshots=_tool_collection_snapshots_from_item(
                item.get("toolCollectionSnapshots")
            ),
            integration_config=_integration_config_from_item(
                item.get("integrationConfig")
            ),
            # Req 16: default True when the attribute is absent (legacy rows
            # predating the gate) so external access is governed solely by the
            # snapshot's ``integration_config.enabled`` until an explicit
            # revocation is recorded.
            external_access_enabled=bool(
                item.get("externalAccessEnabled", True)
            ),
        )
    if cls is ToolCollection:
        tool_refs = [
            ToolRef(name=t["name"], version=t["version"])
            for t in item.get("toolRefs", [])
        ]
        return ToolCollection(**common_kwargs, tool_refs=tool_refs)
    if cls is Workflow:
        # Steps live in S3 (workflow.json); the AssetIndex item does not
        # carry them. Callers that want the full Workflow with steps load
        # them via the WorkflowCRUD read path.
        return Workflow(**common_kwargs, steps=[])
    if cls is Hook:
        action = item.get("action") or {}
        return Hook(
            **common_kwargs,
            event=item.get("event", ""),
            condition=item.get("condition", ""),
            action_type=action.get("type", "notify"),
            action_params=dict(action.get("params", {})),
            priority=int(item.get("priority", 100)),
            enabled=bool(item.get("enabled", True)),
            builtin=bool(item.get("builtin", False)),
            hook_scope=dict(item["hookScope"]) if "hookScope" in item else None,
        )
    return Skill(**common_kwargs)
