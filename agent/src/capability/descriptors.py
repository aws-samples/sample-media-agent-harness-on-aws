"""Registry record descriptors, built deterministically from canonical state.

One module builds every descriptor the harness projects, and both writers use
it — the projection Lambda (:mod:`capability.projector`) and the backfill
command (``agent/scripts/registry_backfill.py``). They must agree byte for
byte: the backfill's dry-run report is only meaningful as a preview of what
the live projector would write, and the descriptor digest is the idempotency
signal (Property 4).

What this module refuses to build is as important as what it builds:

* **Prohibited internal tools** (:data:`PROHIBITED_TOOLS`) — execution
  primitives, orchestration/job internals, Lab CRUD, persistence/OMS, and
  Code Interpreter never become shared records or Tools cards (R10). A
  descriptor containing one raises :class:`PoisonDescriptorError`.
* **Secret-bearing fields** (:data:`SECRET_FIELD_NAMES`) — OAuth client
  secrets, credential-provider ARNs, gateway authorizer configuration, and
  pre-signed URLs are stripped from Gateway data before it is shaped, and any
  survivor raises rather than shipping (R4).
* **Oversize payloads** — the API caps a descriptor ``data`` string at 100 KiB;
  exceeding it is a local rejection with a redacted log, not an API error
  discovered after a retry storm.

Registry is a discovery projection, never a package store (R2): skill records
carry ``SKILL.md`` for search plus an *immutable pointer* to the package
(bucket ARN + key + object version or manifest digest). Never a pre-signed URL.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Iterable, Mapping, Optional, Sequence

from .naming import qualified_tool_name

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Category → Registry record type / descriptor member (R4 mapping table)
# ---------------------------------------------------------------------------

#: ``recordType`` per projected category. Specialty agents map to ``AGENT``
#: with a ``custom`` descriptor rather than ``a2aAgentCard``: today's assets
#: are Task-Agent configurations, not independently addressable A2A services,
#: and claiming otherwise would advertise an endpoint that does not exist.
RECORD_TYPE: dict[str, str] = {
    "skill": "SKILL",
    "specialty-agent": "AGENT",
    "tool-collection": "CUSTOM",
    "workflow": "CUSTOM",
    "mcp-endpoint": "MCP",
}

#: Descriptor member each category populates.
DESCRIPTOR_KEY: dict[str, str] = {
    "skill": "agentSkillsDefinition",
    "specialty-agent": "custom",
    "tool-collection": "custom",
    "workflow": "custom",
    "mcp-endpoint": "mcpServer",
}

#: Harness-owned descriptor payload schema versions. ``dataSchemaVersion`` is
#: free-form in the API (1..255 chars), so these are namespaced and dated:
#: a consumer can tell which shape it is reading, and bumping one is a
#: deliberate, greppable change that re-digests every record.
SCHEMA_VERSIONS: dict[str, str] = {
    "skill": "mediaworks.skill/2026-08-13",
    "specialty-agent": "mediaworks.specialty-agent/2026-08-13",
    "tool-collection": "mediaworks.tool-collection/2026-08-13",
    "workflow": "mediaworks.workflow/2026-08-13",
    "mcp-endpoint": "mediaworks.mcp-endpoint/2026-08-13",
    "mcp-endpoint-tools": "mediaworks.mcp-tools/2026-08-13",
    "skill-md": "text/markdown",
}

#: Categories eligible for projection at all. ``hook`` is absent by design:
#: actor-private hooks stay management-only (R3). Shared hook templates may
#: arrive later as ``CUSTOM`` records.
PROJECTABLE_CATEGORIES: frozenset[str] = frozenset(RECORD_TYPE)

#: The only canonical scope that may enter the shared Registry (R3).
ELIGIBLE_SCOPE: str = "org"

#: The API's per-descriptor ``data`` limit.
MAX_DESCRIPTOR_DATA_BYTES: int = 102_400


# ---------------------------------------------------------------------------
# Prohibition rules (R10)
# ---------------------------------------------------------------------------

#: Internal execution primitives. Present in every agent process; not shared,
#: reusable, external capabilities, so they are neither Registry records nor
#: Tools cards. There is no separately named ``file_write``: write behaviour
#: goes through ``artifact`` / asset CRUD, all excluded here. ``file_read``,
#: ``load_tool`` and ``shell`` are no longer bound anywhere (host-exec tools,
#: tests/test_no_host_exec_tools.py); they stay on this deny list so a gateway
#: tool of that name can never be projected as a capability either.
_EXECUTION_PRIMITIVES: frozenset[str] = frozenset(
    {
        "file_read",
        "http_request",
        "current_time",
        "load_tool",
        "shell",
        "artifact",
        "code_interpreter",
    }
)

#: Orchestration and job internals — implementation of dispatch/planning, not
#: capabilities a user selects.
#:
#: ``invoke_custom_agent`` belongs here for the same reason as ``dispatch_tool``:
#: it is the orchestrator's route onto another runtime, not a capability anyone
#: picks from the Tools catalog. Nothing currently fails without it — the drift
#: test derives only from ``LAB_TOOLS`` / ``_INDIVIDUAL_LAB_TOOLS`` /
#: ``PERSISTENCE_TOOLS`` plus a literal set — so it is listed defensively, which
#: is exactly why the other two are here.
_ORCHESTRATION_INTERNALS: frozenset[str] = frozenset(
    {
        "dispatch_tool",
        "invoke_custom_agent",
        "plan_and_execute_tool",
        "get_job",
        "list_my_jobs",
        "get_plan_results",
        "read_result_files",
        "view_images",
    }
)

#: Lab CRUD / control-plane tools — this migration's whole point is that they
#: stop being Runtime tools at all. ``lab_asset`` is what the Main Agent binds
#: (``main_lab_tools.LAB_TOOLS``); the individual wrappers in
#: ``main_lab_tools._INDIVIDUAL_LAB_TOOLS`` remain importable and are listed
#: too so a direct binding cannot slip one into the catalog.
#:
#: This set is asserted against the live modules by
#: ``tests/test_capability_prohibited_tools.py`` — when a Lab tool is added or
#: renamed, that test fails here rather than the tool silently appearing as a
#: shared Tools card.
_LAB_CRUD_TOOLS: frozenset[str] = frozenset(
    {
        "lab_asset",
        "create_hook",
        "create_skill_draft",
        "create_specialty_agent_draft",
        "create_tool_collection_draft",
        "create_workflow_draft",
        "delete_hook",
        "delete_skill_draft",
        "delete_specialty_agent_draft",
        "delete_tool_collection_draft",
        "delete_workflow_draft",
        "discard_changes",
        "edit_asset",
        "list_asset_versions",
        "list_custom_agents",
        "list_hooks",
        "list_my_published_assets",
        "list_skill_drafts",
        "list_specialty_agent_drafts",
        "list_tool_collection_drafts",
        "list_workflow_drafts",
        "publish_asset",
        "read_hook",
        "read_skill_draft",
        "read_specialty_agent_draft",
        "read_tool_collection_draft",
        "read_workflow_draft",
        "update_hook",
        "update_skill_md",
        "update_specialty_agent_draft",
        "update_tool_collection_draft",
        "update_workflow_draft",
        "validate_hook_condition",
    }
)

#: Persistence / OMS internals (``persistence.persistence_tool.PERSISTENCE_TOOLS``).
_PERSISTENCE_TOOLS: frozenset[str] = frozenset(
    {"persist_workflow", "confirm_workflow_activation"}
)

#: Render and hook behaviour. There is no named ``render`` *tool* today —
#: rendering is a hook action (``hooks/actions.py``) plus the Render Agent
#: runtime — so this set guards against one being introduced and projected as
#: a user-selectable MCP tool instead of being surfaced through the Agents or
#: Hooks tab (R10).
_RENDER_HOOK_TOOLS: frozenset[str] = frozenset(
    {"render", "render_action", "notify", "notify_action", "dispatch_action"}
)

#: Union of everything that must never appear in a shared record or the Tools
#: catalog. Matched on the **bare** tool name so a target-qualified alias
#: (``some-target___shell``) cannot smuggle one through.
PROHIBITED_TOOLS: frozenset[str] = (
    _EXECUTION_PRIMITIVES
    | _ORCHESTRATION_INTERNALS
    | _LAB_CRUD_TOOLS
    | _PERSISTENCE_TOOLS
    | _RENDER_HOOK_TOOLS
)

#: Field names that carry (or point at) a credential. Matched
#: case-insensitively anywhere in a descriptor tree.
SECRET_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "clientsecret",
        "client_secret",
        "credentialproviderarn",
        "credentialproviderconfigurations",
        "authorizerconfiguration",
        "presignedurl",
        "presigned_url",
        "secretarn",
        "secret_arn",
        "accesstoken",
        "access_token",
        "bearertoken",
        "apikey",
        "api_key",
        "privatekey",
        "password",
    }
)

#: Substrings whose presence in a *value* means a credential leaked in even
#: though the field name looked innocent.
_SECRET_VALUE_MARKERS: tuple[str, ...] = (
    "x-amz-signature=",
    "x-amz-credential=",
    "aws_secret_access_key",
)


class PoisonDescriptorError(ValueError):
    """A descriptor is unprojectable and must not be retried as-is.

    Raised for a prohibited tool, a surviving secret field, an oversize
    payload, or an unknown category. The projector lets this reach the event
    source so the batch lands in the DLQ with the canonical asset untouched
    (R7, design "Poison descriptor"). ``reason`` is a stable class string safe
    to log; the offending payload is never attached.
    """

    def __init__(self, reason: str, *, detail: Optional[str] = None) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(reason if detail is None else f"{reason}: {detail}")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def assert_no_prohibited_tools(tool_names: Iterable[str]) -> None:
    """Raise if any name resolves to a prohibited internal tool.

    Accepts bare or target-qualified names; the check is on the bare part, so
    ``media-processing___shell`` is rejected exactly like ``shell``.
    """
    for name in tool_names:
        bare = name.rpartition("___")[2] if "___" in name else name
        if bare in PROHIBITED_TOOLS:
            raise PoisonDescriptorError(
                "prohibited_internal_tool", detail=bare
            )


def assert_no_secrets(payload: Any, *, path: str = "$") -> None:
    """Walk a descriptor tree and raise on any credential-shaped field.

    Defence in depth: :func:`build_mcp_endpoint_descriptor` already drops
    these keys, so reaching here means a new Gateway field appeared that the
    shaping code did not know about — precisely the case worth failing on
    rather than publishing.
    """
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if isinstance(key, str) and key.lower() in SECRET_FIELD_NAMES:
                raise PoisonDescriptorError(
                    "secret_field_in_descriptor", detail=f"{path}.{key}"
                )
            assert_no_secrets(value, path=f"{path}.{key}")
        return
    if isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            assert_no_secrets(value, path=f"{path}[{index}]")
        return
    if isinstance(payload, str):
        lowered = payload.lower()
        for marker in _SECRET_VALUE_MARKERS:
            if marker in lowered:
                raise PoisonDescriptorError(
                    "secret_value_in_descriptor", detail=path
                )


def _encode(payload: Any, *, what: str) -> str:
    """Serialize a descriptor payload deterministically and bound its size.

    ``sort_keys`` is what makes the digest stable across processes and Python
    versions; without it two equivalent descriptors would digest differently
    and projection would rewrite records forever.
    """
    assert_no_secrets(payload)
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    size = len(text.encode("utf-8"))
    if size > MAX_DESCRIPTOR_DATA_BYTES:
        raise PoisonDescriptorError(
            "descriptor_too_large",
            detail=f"{what} is {size} bytes (limit {MAX_DESCRIPTOR_DATA_BYTES})",
        )
    return text


def _truncate_markdown(text: str, *, limit: int = MAX_DESCRIPTOR_DATA_BYTES) -> str:
    """Clip discovery markdown to the API limit at a UTF-8 safe boundary.

    Truncating SKILL.md is acceptable — it exists here for *search*, and the
    executable package is fetched from S3 by the pointer (R4). Failing the
    whole projection because a skill has a long README would not be.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    marker = b"\n\n<!-- truncated for discovery; full body in the S3 package -->"
    clipped = encoded[: limit - len(marker)]
    return (clipped.decode("utf-8", errors="ignore") + marker.decode()).strip()


# ---------------------------------------------------------------------------
# Package pointers (R4)
# ---------------------------------------------------------------------------


def build_package_reference(
    *,
    bucket: Optional[str],
    s3_prefix: Optional[str],
    manifest_digest: Optional[str] = None,
    object_version: Optional[str] = None,
) -> dict[str, Any]:
    """An immutable pointer to a package body — never a pre-signed URL.

    ``s3Uri`` plus at least one of ``manifestDigest`` / ``objectVersion`` is
    what lets a consumer prove it fetched the same bytes the record described.
    A record with neither is still valid (the prefix under a published version
    is itself immutable), but the digest is preferred and is what
    reconciliation compares.
    """
    reference: dict[str, Any] = {"kind": "s3-prefix"}
    if bucket:
        reference["bucketArn"] = f"arn:aws:s3:::{bucket}"
        reference["bucket"] = bucket
    if s3_prefix:
        reference["key"] = s3_prefix
        if bucket:
            reference["s3Uri"] = f"s3://{bucket}/{s3_prefix}"
    if manifest_digest:
        reference["manifestDigest"] = manifest_digest
    if object_version:
        reference["objectVersion"] = object_version
    return reference


# ---------------------------------------------------------------------------
# Canonical-row helpers
# ---------------------------------------------------------------------------


def _row_str(row: Mapping[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _row_int(row: Mapping[str, Any], key: str) -> Optional[int]:
    value = row.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    # DynamoDB resource-level reads decode numbers as Decimal.
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _row_list(row: Mapping[str, Any], key: str) -> list[str]:
    value = row.get(key)
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value]
    return []


def is_eligible(row: Mapping[str, Any]) -> bool:
    """Whether a canonical row belongs in the harness-shared Registry (R3).

    Eligible means: a projectable category, an immutable published version
    (positive ``version``), organization scope, and not deprecated. Working
    copies, private and group versions, and deprecated versions are all
    excluded — a deprecated version's *Registry* revision is deprecated
    rather than deleted, which is the projector's job, not this predicate's.
    """
    category = _row_str(row, "category")
    if category not in PROJECTABLE_CATEGORIES:
        return False
    version = _row_int(row, "version")
    if version is None or version <= 0:
        return False
    if _row_str(row, "scope") != ELIGIBLE_SCOPE:
        return False
    if bool(row.get("deprecated")):
        return False
    return True


# ---------------------------------------------------------------------------
# Descriptor builders
# ---------------------------------------------------------------------------


def build_skill_descriptor(
    row: Mapping[str, Any],
    *,
    artifacts_bucket: Optional[str] = None,
    skill_md: Optional[str] = None,
    manifest_digest: Optional[str] = None,
) -> dict[str, Any]:
    """``SKILL`` + ``agentSkillsDefinition``.

    ``data`` follows the A2A ``AgentSkill`` shape (id / name / description /
    tags) so a generic consumer gets something meaningful, with harness
    specifics under a namespaced ``mediaworks`` block. ``additionalData.skillMd``
    carries the discovery markdown; the executable package stays in S3.
    """
    asset_id = _row_str(row, "assetId", "asset_id")
    if not asset_id:
        raise PoisonDescriptorError("missing_asset_id")
    version = _row_int(row, "version")

    definition: dict[str, Any] = {
        "id": asset_id,
        "name": _row_str(row, "name", "displayName") or asset_id,
        "description": _row_str(row, "description") or "",
        "tags": _row_list(row, "tags"),
        "mediaworks": {
            "category": "skill",
            "assetId": asset_id,
            "version": version,
            "scope": _row_str(row, "scope"),
            "package": build_package_reference(
                bucket=artifacts_bucket,
                s3_prefix=_row_str(row, "s3Prefix", "s3_prefix"),
                manifest_digest=manifest_digest,
            ),
        },
    }

    descriptor: dict[str, Any] = {
        "agentSkillsDefinition": {
            "data": _encode(definition, what="skill definition"),
            "dataSchemaVersion": SCHEMA_VERSIONS["skill"],
        }
    }
    if skill_md:
        descriptor["agentSkillsDefinition"]["additionalData"] = {
            "skillMd": {
                "data": _truncate_markdown(skill_md),
                "dataSchemaVersion": SCHEMA_VERSIONS["skill-md"],
            }
        }
    return descriptor


def build_specialty_agent_descriptor(
    row: Mapping[str, Any], *, artifacts_bucket: Optional[str] = None
) -> dict[str, Any]:
    """``AGENT`` + ``custom``.

    Deliberately not ``a2aAgentCard``: an agent card advertises a callable A2A
    endpoint, and these assets are Task-Agent configurations resolved by the
    Runtime from canonical storage. ``integrationConfig`` is reduced to a
    boolean — its issuer/audience/client-id values are authorization
    configuration, not discovery metadata.
    """
    asset_id = _row_str(row, "assetId", "asset_id")
    if not asset_id:
        raise PoisonDescriptorError("missing_asset_id")

    integration = row.get("integrationConfig") or row.get("integration_config") or {}
    payload = {
        "schemaVersion": SCHEMA_VERSIONS["specialty-agent"],
        "category": "specialty-agent",
        "assetId": asset_id,
        "version": _row_int(row, "version"),
        "displayName": _row_str(row, "name", "displayName") or asset_id,
        "description": _row_str(row, "description") or "",
        "scope": _row_str(row, "scope"),
        "model": _row_str(row, "model"),
        "compatibleSkills": _row_list(row, "compatibleSkills"),
        "toolCollections": _row_list(row, "toolCollections"),
        "externalInvokeEnabled": bool(
            isinstance(integration, Mapping) and integration.get("enabled")
        ),
        "package": build_package_reference(
            bucket=artifacts_bucket,
            s3_prefix=_row_str(row, "s3Prefix", "s3_prefix"),
        ),
    }
    return {"custom": {"data": _encode(payload, what="specialty-agent descriptor")}}


def build_tool_collection_descriptor(
    row: Mapping[str, Any], *, artifacts_bucket: Optional[str] = None
) -> dict[str, Any]:
    """``CUSTOM`` + ``custom`` — a curated selection of shared Gateway tools.

    Tool references keep their target-qualified names so the collection
    remains resolvable when two targets expose the same bare name (R10).
    """
    asset_id = _row_str(row, "assetId", "asset_id")
    if not asset_id:
        raise PoisonDescriptorError("missing_asset_id")

    tool_refs = _normalize_tool_refs(row.get("tools") or row.get("toolRefs") or [])
    assert_no_prohibited_tools(
        [ref.get("qualifiedToolName") or ref.get("bareToolName", "") for ref in tool_refs]
    )
    payload = {
        "schemaVersion": SCHEMA_VERSIONS["tool-collection"],
        "category": "tool-collection",
        "assetId": asset_id,
        "version": _row_int(row, "version"),
        "displayName": _row_str(row, "name", "displayName") or asset_id,
        "description": _row_str(row, "description") or "",
        "scope": _row_str(row, "scope"),
        "tools": tool_refs,
        "package": build_package_reference(
            bucket=artifacts_bucket,
            s3_prefix=_row_str(row, "s3Prefix", "s3_prefix"),
        ),
    }
    return {"custom": {"data": _encode(payload, what="tool-collection descriptor")}}


def build_workflow_descriptor(
    row: Mapping[str, Any], *, artifacts_bucket: Optional[str] = None
) -> dict[str, Any]:
    """``CUSTOM`` + ``custom``.

    Step *shape* only — agent/skill/tool-collection bindings by canonical id.
    The execution DAG is not projected: a stored workflow and a run-time DAG
    are different contracts, and conflating them was the reason workflows are
    the last category to cut over (R3).
    """
    asset_id = _row_str(row, "assetId", "asset_id")
    if not asset_id:
        raise PoisonDescriptorError("missing_asset_id")

    steps: list[dict[str, Any]] = []
    raw_steps = row.get("steps")
    if isinstance(raw_steps, (list, tuple)):
        for index, step in enumerate(raw_steps):
            if not isinstance(step, Mapping):
                continue
            agent = step.get("agent") if isinstance(step.get("agent"), Mapping) else {}
            skill = step.get("skill") if isinstance(step.get("skill"), Mapping) else {}
            collection = (
                step.get("toolCollection")
                if isinstance(step.get("toolCollection"), Mapping)
                else {}
            )
            steps.append(
                {
                    "stepId": str(step.get("stepId") or step.get("step_id") or index),
                    "agentKind": agent.get("kind"),
                    "agentRef": agent.get("ref"),
                    "agentAssetId": agent.get("assetId") or agent.get("asset_id"),
                    "skillAssetId": skill.get("assetId") or skill.get("asset_id"),
                    "toolCollectionAssetId": collection.get("assetId")
                    or collection.get("asset_id"),
                    "inputs": [str(i) for i in (step.get("inputs") or [])],
                }
            )

    payload = {
        "schemaVersion": SCHEMA_VERSIONS["workflow"],
        "category": "workflow",
        "assetId": asset_id,
        "version": _row_int(row, "version"),
        "displayName": _row_str(row, "name", "displayName") or asset_id,
        "description": _row_str(row, "description") or "",
        "scope": _row_str(row, "scope"),
        "stepCount": len(steps),
        "steps": steps,
        "package": build_package_reference(
            bucket=artifacts_bucket,
            s3_prefix=_row_str(row, "s3Prefix", "s3_prefix"),
        ),
    }
    return {"custom": {"data": _encode(payload, what="workflow descriptor")}}


def _normalize_tool_refs(raw: Any) -> list[dict[str, Any]]:
    """Accept the several shapes a stored tool reference has had."""
    refs: list[dict[str, Any]] = []
    if not isinstance(raw, (list, tuple)):
        return refs
    for entry in raw:
        if isinstance(entry, str):
            target, bare = (
                entry.split("___", 1) if "___" in entry else (None, entry)
            )
            refs.append(
                {
                    "qualifiedToolName": entry,
                    "bareToolName": bare,
                    **({"targetName": target} if target else {}),
                }
            )
            continue
        if not isinstance(entry, Mapping):
            continue
        bare = (
            entry.get("bareToolName")
            or entry.get("bare_tool_name")
            or entry.get("name")
            or ""
        )
        target = entry.get("targetName") or entry.get("target_name")
        qualified = (
            entry.get("qualifiedToolName")
            or entry.get("qualified_tool_name")
            or (qualified_tool_name(target, bare) if target and bare else bare)
        )
        ref: dict[str, Any] = {"bareToolName": bare, "qualifiedToolName": qualified}
        for source_key, out_key in (
            ("gatewayId", "gatewayId"),
            ("gateway_id", "gatewayId"),
            ("targetId", "targetId"),
            ("target_id", "targetId"),
            ("targetName", "targetName"),
            ("target_name", "targetName"),
            ("version", "version"),
            ("schemaDigest", "schemaDigest"),
        ):
            value = entry.get(source_key)
            if value:
                ref.setdefault(out_key, value)
        refs.append(ref)
    return refs


# ---------------------------------------------------------------------------
# Shared MCP Gateway endpoint (exactly one record per reachable endpoint)
# ---------------------------------------------------------------------------


def schema_digest(input_schema: Any) -> str:
    """``sha256:...`` over a tool's input schema.

    Lets reconciliation notice that an upstream target silently changed a
    tool's contract, which a name/version comparison cannot see.
    """
    text = json.dumps(input_schema or {}, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def build_mcp_endpoint_descriptor(
    *,
    gateway_id: str,
    gateway_url: str,
    display_name: Optional[str] = None,
    description: Optional[str] = None,
    targets: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """``MCP`` + ``mcpServer`` for one reachable Gateway endpoint.

    ``targets`` is a sequence of ``{"targetId", "targetName", "tools": [...]}``
    where each tool has at least a ``name`` and optionally an ``inputSchema``.
    Every tool is nested under ``additionalData.tools`` carrying full
    provenance — ``gatewayId``, ``targetId``, ``targetName``, the
    target-qualified name, the bare name, and a schema digest.

    Three invariants this function exists to hold (R4):

    * **One record per endpoint.** Targets are nested, never promoted. M2C's
      media-processing / stem-separation / diarization targets stay
      distinguishable by provenance rather than by record count.
    * **Repeated bare names are preserved.** Two targets both exposing
      ``get_job_status`` yield two nested entries, not one; collapsing them
      would destroy invocation identity.
    * **No credentials.** The endpoint URL is discovery metadata; the
      authorizer configuration, credential-provider ARNs, and client secrets
      that make it callable are not, and ``source.fromUrl`` is not populated
      because its credential-provider block is exactly what must not be here.

    Raises:
        PoisonDescriptorError: A nested tool is a prohibited internal tool, a
            credential-shaped field survived, or the payload exceeds the API
            limit.
    """
    if not gateway_id:
        raise PoisonDescriptorError("missing_gateway_id")

    nested_tools: list[dict[str, Any]] = []
    target_summaries: list[dict[str, Any]] = []
    for target in targets:
        target_id = str(target.get("targetId") or target.get("target_id") or "")
        target_name = str(target.get("targetName") or target.get("target_name") or "")
        tools = target.get("tools") or []
        tool_names: list[str] = []
        for tool in tools:
            if isinstance(tool, str):
                bare, input_schema, tool_description = tool, None, ""
            elif isinstance(tool, Mapping):
                bare = str(tool.get("name") or tool.get("bareToolName") or "")
                input_schema = tool.get("inputSchema") or tool.get("input_schema")
                tool_description = str(tool.get("description") or "")
            else:
                continue
            if not bare:
                continue
            # A gateway may already report target-qualified names; normalize so
            # the qualification is applied exactly once.
            if "___" in bare:
                prefix, _, remainder = bare.partition("___")
                if not target_name:
                    target_name = prefix
                bare = remainder
            tool_names.append(bare)
            nested_tools.append(
                {
                    "gatewayId": gateway_id,
                    "targetId": target_id or None,
                    "targetName": target_name or None,
                    "qualifiedToolName": (
                        qualified_tool_name(target_name, bare) if target_name else bare
                    ),
                    "bareToolName": bare,
                    "description": tool_description,
                    "schemaDigest": schema_digest(input_schema),
                }
            )
        assert_no_prohibited_tools(tool_names)
        target_summaries.append(
            {
                "targetId": target_id or None,
                "targetName": target_name or None,
                "toolCount": len(tool_names),
            }
        )

    server = {
        "schemaVersion": SCHEMA_VERSIONS["mcp-endpoint"],
        "gatewayId": gateway_id,
        "url": gateway_url,
        "transport": "streamable-http",
        "displayName": display_name or gateway_id,
        "description": description or "",
        "targets": target_summaries,
        "toolCount": len(nested_tools),
    }

    return {
        "mcpServer": {
            "data": _encode(server, what="mcp endpoint descriptor"),
            "dataSchemaVersion": SCHEMA_VERSIONS["mcp-endpoint"],
            "additionalData": {
                "tools": {
                    "data": _encode({"tools": nested_tools}, what="mcp tool list"),
                    "dataSchemaVersion": SCHEMA_VERSIONS["mcp-endpoint-tools"],
                }
            },
        }
    }


# ---------------------------------------------------------------------------
# Dispatch + digest
# ---------------------------------------------------------------------------

#: Asset categories with a builder. ``mcp-endpoint`` is absent because a
#: Gateway endpoint has no AssetIndex row — it is built from Gateway control
#: plane data via :func:`build_mcp_endpoint_descriptor` instead.
_ASSET_BUILDERS = {
    "specialty-agent": build_specialty_agent_descriptor,
    "tool-collection": build_tool_collection_descriptor,
    "workflow": build_workflow_descriptor,
}


def build_descriptor(
    category: str,
    row: Mapping[str, Any],
    *,
    artifacts_bucket: Optional[str] = None,
    skill_md: Optional[str] = None,
    manifest_digest: Optional[str] = None,
) -> dict[str, Any]:
    """Build the descriptor for one canonical asset row.

    The single entry point the projector and the backfill both call, so a
    dry-run report is a faithful preview of what the live projector writes.

    Raises:
        PoisonDescriptorError: ``category`` is not projectable, or the row
            fails one of the prohibition/size checks.
    """
    if category == "skill":
        return build_skill_descriptor(
            row,
            artifacts_bucket=artifacts_bucket,
            skill_md=skill_md,
            manifest_digest=manifest_digest,
        )
    builder = _ASSET_BUILDERS.get(category)
    if builder is None:
        raise PoisonDescriptorError("unprojectable_category", detail=category)
    return builder(row, artifacts_bucket=artifacts_bucket)


def descriptor_digest(descriptor: Mapping[str, Any]) -> str:
    """Stable ``sha256:...`` over a built descriptor.

    The projector's idempotency signal: an event whose recomputed digest
    matches the stored one converges to a no-op instead of rewriting the
    record (Property 4).
    """
    text = json.dumps(descriptor, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


__all__ = [
    "DESCRIPTOR_KEY",
    "ELIGIBLE_SCOPE",
    "MAX_DESCRIPTOR_DATA_BYTES",
    "PROHIBITED_TOOLS",
    "PROJECTABLE_CATEGORIES",
    "PoisonDescriptorError",
    "RECORD_TYPE",
    "SCHEMA_VERSIONS",
    "SECRET_FIELD_NAMES",
    "assert_no_prohibited_tools",
    "assert_no_secrets",
    "build_descriptor",
    "build_mcp_endpoint_descriptor",
    "build_package_reference",
    "build_skill_descriptor",
    "build_specialty_agent_descriptor",
    "build_tool_collection_descriptor",
    "build_workflow_descriptor",
    "descriptor_digest",
    "is_eligible",
    "schema_digest",
]
