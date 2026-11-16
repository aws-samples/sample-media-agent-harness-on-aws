"""Tests for WorkflowCRUD — validates CRUD operations and reference validation.

Tests cover:
- Module imports and interface
- Create with valid steps and references
- Create with zero steps (valid)
- Create rejects duplicate stepIds
- Create rejects unresolved agent references
- Create rejects unresolved skill references
- Create rejects unresolved tool collection references
- Create rejects out-of-order input dependencies
- Create rejects incompatible skill/specialty pairing
- Read returns stored definition + resolved view
- Read marks deleted references as unresolved
- Update with full re-validation on steps change
- Delete two-phase soft-delete
- List returns only active drafts
- Validation failure produces no S3 write or index mutation (Property 11)
"""

from __future__ import annotations

import asyncio
import json
import sys
import os
from unittest.mock import MagicMock, AsyncMock

import pytest

# Add agent/src to path so imports resolve
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab.asset_index import make_pk_user_owned, make_sk_owned
from lab.models import (
    AgentRef,
    Skill,
    SkillRef,
    SpecialtyAgent,
    ToolCollection,
    ToolCollectionRef,
    Workflow,
    WorkflowStep,
)
from lab.workflow_crud import (
    WorkflowCRUD,
    WorkflowCreateResult,
    WorkflowReadResult,
    ResolvedStepView,
    _CATEGORY,
    _WORKFLOW_FILENAME,
    _DRAFTS_PREFIX,
)
from lab.context import LabRequestContext


def _run(coro):
    """Helper to run async coroutines in sync tests."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


# Canonical S3 prefix for a workflow draft owned by ``actor_id``. The
# AssetIndex row carries the bucket-absolute form
# ``users/<actor>/drafts/workflows/<asset_id>/``; the S3UserFilesTool
# itself auto-prepends the actor scope when resolving relative paths.
def _draft_prefix(actor_id: str, asset_id: str, plural: str = "workflows") -> str:
    return f"users/{actor_id}/drafts/{plural}/{asset_id}/"


def _make_ctx(actor_id: str = "actor-123", session_id: str = "session-abc") -> LabRequestContext:
    """Create a test LabRequestContext."""
    return LabRequestContext(
        actor_id=actor_id,
        session_id=session_id,
        request_id="req-test-001",
    )


def _make_mock_s3() -> MagicMock:
    """Create a mock S3UserFilesTool."""
    s3 = MagicMock()
    s3.write = AsyncMock()
    s3.read = AsyncMock()
    s3.delete = AsyncMock()
    s3.delete_prefix = AsyncMock()
    s3.list = AsyncMock(return_value=[])
    # Artifact I/O moved to canonical-prefix absolute methods (BUG-2); share
    # the mock objects so existing stubs still drive the CRUD code.
    s3.actor_id = "test-actor"
    s3.read_absolute = s3.read
    s3.write_absolute = s3.write
    s3.list_absolute = s3.list
    s3.delete_absolute = s3.delete
    s3.delete_prefix_absolute = s3.delete_prefix
    return s3


def _make_mock_index() -> MagicMock:
    """Create a mock AssetIndex with the new async PK/SK API.

    The mock exposes:
      * ``put`` / ``get`` / ``update`` / ``query_pk`` / ``transact_write``
        as AsyncMocks with empty defaults.
      * ``table_name`` as a fixed string (the canonical-ops layer reads
        this for ``transact_write`` payloads).
    """
    index = MagicMock()
    index.table_name = "test-asset-index"
    index.put = AsyncMock(return_value=None)
    index.get = AsyncMock(return_value=None)
    index.update = AsyncMock(return_value=None)
    index.query_pk = AsyncMock(return_value=([], None))
    index.transact_write = AsyncMock(return_value=None)
    index.query_lineage = AsyncMock(return_value=[])
    return index


def _make_workflow_record(
    *,
    asset_id: str,
    actor_id: str = "actor-123",
    name: str = "Test Workflow",
    status: str = "draft",
) -> Workflow:
    """Build a typed Workflow draft for the mock index.get to return."""
    return Workflow(
        asset_id=asset_id,
        category="workflow",
        name=name,
        status=status,
        scope="private",
        s3_prefix=_draft_prefix(actor_id, asset_id),
        created_at=1700000000000,
        updated_at=1700000000000,
        steps=[],
    )


def _make_specialty_record(
    *,
    asset_id: str,
    actor_id: str = "actor-123",
    name: str = "Test Specialty",
    compatible_skills: list[str] | None = None,
    status: str = "draft",
) -> SpecialtyAgent:
    return SpecialtyAgent(
        asset_id=asset_id,
        category="specialty-agent",
        name=name,
        status=status,
        scope="private",
        s3_prefix=_draft_prefix(actor_id, asset_id, plural="specialty-agents"),
        created_at=1700000000000,
        updated_at=1700000000000,
        compatible_skills=list(compatible_skills or []),
    )


def _make_skill_record(
    *,
    asset_id: str,
    actor_id: str = "actor-123",
    name: str = "Test Skill",
    status: str = "draft",
) -> Skill:
    return Skill(
        asset_id=asset_id,
        category="skill",
        name=name,
        status=status,
        scope="private",
        s3_prefix=_draft_prefix(actor_id, asset_id, plural="skills"),
        created_at=1700000000000,
        updated_at=1700000000000,
    )


def _make_tool_collection_record(
    *,
    asset_id: str,
    actor_id: str = "actor-123",
    name: str = "Test Tools",
    status: str = "draft",
) -> ToolCollection:
    return ToolCollection(
        asset_id=asset_id,
        category="tool-collection",
        name=name,
        status=status,
        scope="private",
        s3_prefix=f"tool-collections/{asset_id}/",
        created_at=1700000000000,
        updated_at=1700000000000,
    )


def _install_index_get(index: MagicMock, records: dict[tuple[str, str], object]) -> None:
    """Install a side_effect on ``index.get`` that resolves (PK, SK) from a map.

    Returns ``None`` for keys not present in the map, matching real
    AssetIndex semantics (missing rows ⇒ None).
    """

    async def _get(pk: str, sk: str):
        return records.get((pk, sk))

    index.get.side_effect = _get


def _make_core_step(step_id: str, prompt: str = "Do something", inputs: list[str] = None) -> WorkflowStep:
    """Create a workflow step using the core task-agent."""
    return WorkflowStep(
        step_id=step_id,
        agent=AgentRef(kind="core", ref="task-agent"),
        skill=SkillRef(asset_id=None),
        tool_collection=ToolCollectionRef(asset_id=None),
        prompt=prompt,
        inputs=inputs or [],
    )


def _make_specialty_step(
    step_id: str,
    agent_asset_id: str,
    skill_asset_id: str = None,
    tc_asset_id: str = None,
    prompt: str = "Do something",
    inputs: list[str] = None,
) -> WorkflowStep:
    """Create a workflow step using a specialty agent."""
    return WorkflowStep(
        step_id=step_id,
        agent=AgentRef(kind="specialty", asset_id=agent_asset_id),
        skill=SkillRef(asset_id=skill_asset_id),
        tool_collection=ToolCollectionRef(asset_id=tc_asset_id),
        prompt=prompt,
        inputs=inputs or [],
    )


def _setup_index_for_valid_refs(index: MagicMock, actor_id: str = "actor-123") -> None:
    """Configure the mock index to resolve common test references.

    Maps the canonical (PK, SK) form expected by the new AssetIndex
    API to typed dataclass records covering the standard
    ``sa-001`` / ``sk-001`` / ``tc-001`` test fixtures.
    """
    drafts_pk = make_pk_user_owned(actor_id)
    records: dict[tuple[str, str], object] = {
        (drafts_pk, make_sk_owned("specialty-agent", "sa-001")): _make_specialty_record(
            asset_id="sa-001",
            actor_id=actor_id,
            name="Moderation Specialty",
            compatible_skills=["sk-001"],
        ),
        (drafts_pk, make_sk_owned("skill", "sk-001")): _make_skill_record(
            asset_id="sk-001",
            actor_id=actor_id,
            name="Frame Analysis",
        ),
        (drafts_pk, make_sk_owned("tool-collection", "tc-001")): _make_tool_collection_record(
            asset_id="tc-001",
            actor_id=actor_id,
            name="Media Tools",
        ),
    }
    _install_index_get(index, records)


# ---------------------------------------------------------------------------
# Import and interface tests
# ---------------------------------------------------------------------------


def test_workflow_crud_has_expected_methods():
    """WorkflowCRUD class exposes the required CRUD methods."""
    assert hasattr(WorkflowCRUD, "create")
    assert hasattr(WorkflowCRUD, "read")
    assert hasattr(WorkflowCRUD, "update")
    assert hasattr(WorkflowCRUD, "delete")
    assert hasattr(WorkflowCRUD, "list")


def test_create_result_dataclass():
    """WorkflowCreateResult has the expected fields."""
    result = WorkflowCreateResult(
        asset_id="wf-001",
        s3_prefix="drafts/workflows/wf-001/",
    )
    assert result.asset_id == "wf-001"
    assert result.s3_prefix == "drafts/workflows/wf-001/"


def test_read_result_dataclass():
    """WorkflowReadResult has the expected fields."""
    result = WorkflowReadResult(
        asset_id="wf-001",
        name="test-workflow",
        steps=[],
        resolved_view=[],
        metadata={"category": "workflow"},
    )
    assert result.asset_id == "wf-001"
    assert result.name == "test-workflow"


# ---------------------------------------------------------------------------
# Create tests
# ---------------------------------------------------------------------------


def test_create_with_zero_steps():
    """Create succeeds with zero steps (valid, just not promotion-ready)."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()

    crud = WorkflowCRUD(s3=s3, index=index)

    result = _run(crud.create(ctx, name="empty-workflow", steps=[]))

    assert result.asset_id is not None
    assert result.s3_prefix.startswith("users/")
    assert "/drafts/workflows/" in result.s3_prefix
    s3.write.assert_called_once()
    index.put.assert_awaited_once()
    # AssetIndex.put now takes a typed Workflow draft as the positional
    # arg plus owner_user_id as a kwarg.
    put_workflow = index.put.call_args.args[0]
    assert isinstance(put_workflow, Workflow)
    assert put_workflow.category == _CATEGORY
    assert put_workflow.name == "empty-workflow"
    assert put_workflow.status == "draft"
    assert index.put.call_args.kwargs["owner_user_id"] == ctx.actor_id


def test_create_with_valid_core_steps():
    """Create succeeds with valid core agent steps."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()

    steps = [
        _make_core_step("step1", "Extract data"),
        _make_core_step("step2", "Process data", inputs=["step1"]),
    ]

    crud = WorkflowCRUD(s3=s3, index=index)

    result = _run(crud.create(ctx, name="two-step-workflow", steps=steps))

    assert result.asset_id is not None
    s3.write.assert_called_once()
    index.put.assert_awaited_once()


def test_create_with_valid_specialty_and_skill():
    """Create succeeds with valid specialty agent + compatible skill."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()
    _setup_index_for_valid_refs(index, actor_id=ctx.actor_id)

    steps = [
        _make_specialty_step(
            "judge",
            agent_asset_id="sa-001",
            skill_asset_id="sk-001",
            tc_asset_id="tc-001",
            prompt="Score the frames",
        ),
    ]

    crud = WorkflowCRUD(s3=s3, index=index)

    result = _run(crud.create(ctx, name="moderation-workflow", steps=steps))

    assert result.asset_id is not None
    s3.write.assert_called_once()
    index.put.assert_awaited_once()


def test_create_rejects_duplicate_step_ids():
    """Create rejects workflows with duplicate stepIds."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()

    steps = [
        _make_core_step("duplicate-id", "First step"),
        _make_core_step("duplicate-id", "Second step"),
    ]

    crud = WorkflowCRUD(s3=s3, index=index)

    with pytest.raises(ValueError) as exc_info:
        _run(crud.create(ctx, name="bad-workflow", steps=steps))

    assert "Duplicate stepId" in str(exc_info.value)
    assert "duplicate-id" in str(exc_info.value)
    # Property 11: no S3 write or index mutation on validation failure
    s3.write.assert_not_called()
    index.put.assert_not_awaited()


def test_create_rejects_unresolved_specialty_agent():
    """Create rejects when specialty agent assetId doesn't resolve (Req 5.3)."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()
    # Default index.get returns None ⇒ nothing resolves.

    steps = [
        _make_specialty_step("step1", agent_asset_id="nonexistent-sa"),
    ]

    crud = WorkflowCRUD(s3=s3, index=index)

    with pytest.raises(ValueError) as exc_info:
        _run(crud.create(ctx, name="bad-workflow", steps=steps))

    assert "nonexistent-sa" in str(exc_info.value)
    assert "Specialty_Agent_Draft" in str(exc_info.value)
    s3.write.assert_not_called()
    index.put.assert_not_awaited()


def test_create_rejects_unresolved_skill():
    """Create rejects when skill assetId doesn't resolve (Req 5.3)."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()

    # Only the specialty resolves; the skill lookup returns None.
    drafts_pk = make_pk_user_owned(ctx.actor_id)
    _install_index_get(
        index,
        {
            (drafts_pk, make_sk_owned("specialty-agent", "sa-001")): _make_specialty_record(
                asset_id="sa-001",
                actor_id=ctx.actor_id,
                name="Test Specialty",
                compatible_skills=[],
            )
        },
    )

    steps = [
        _make_specialty_step(
            "step1",
            agent_asset_id="sa-001",
            skill_asset_id="nonexistent-skill",
        ),
    ]

    crud = WorkflowCRUD(s3=s3, index=index)

    with pytest.raises(ValueError) as exc_info:
        _run(crud.create(ctx, name="bad-workflow", steps=steps))

    assert "nonexistent-skill" in str(exc_info.value)
    assert "Skill_Draft" in str(exc_info.value)
    s3.write.assert_not_called()
    index.put.assert_not_awaited()


def test_create_rejects_unresolved_tool_collection():
    """Create rejects when tool collection assetId doesn't resolve (Req 5.3)."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()

    steps = [
        WorkflowStep(
            step_id="step1",
            agent=AgentRef(kind="core", ref="task-agent"),
            skill=SkillRef(asset_id=None),
            tool_collection=ToolCollectionRef(asset_id="nonexistent-tc"),
            prompt="Do something",
            inputs=[],
        ),
    ]

    crud = WorkflowCRUD(s3=s3, index=index)

    with pytest.raises(ValueError) as exc_info:
        _run(crud.create(ctx, name="bad-workflow", steps=steps))

    assert "nonexistent-tc" in str(exc_info.value)
    assert "Tool_Collection_Draft" in str(exc_info.value)
    s3.write.assert_not_called()
    index.put.assert_not_awaited()


def test_create_rejects_out_of_order_inputs():
    """Create rejects when inputs reference a step that appears later (Req 5.4)."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()

    steps = [
        _make_core_step("step1", "First", inputs=["step2"]),  # step2 not yet defined
        _make_core_step("step2", "Second"),
    ]

    crud = WorkflowCRUD(s3=s3, index=index)

    with pytest.raises(ValueError) as exc_info:
        _run(crud.create(ctx, name="bad-workflow", steps=steps))

    assert "step2" in str(exc_info.value)
    assert "earlier" in str(exc_info.value)
    s3.write.assert_not_called()
    index.put.assert_not_awaited()


def test_create_rejects_incompatible_skill_specialty():
    """Create rejects when skill is not in specialty's compatibleSkills (Req 5.5)."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()

    # Specialty exists but skill is NOT in its compatibleSkills
    drafts_pk = make_pk_user_owned(ctx.actor_id)
    _install_index_get(
        index,
        {
            (drafts_pk, make_sk_owned("specialty-agent", "sa-001")): _make_specialty_record(
                asset_id="sa-001",
                actor_id=ctx.actor_id,
                name="Test Specialty",
                compatible_skills=["sk-other"],  # sk-001 NOT listed
            ),
            (drafts_pk, make_sk_owned("skill", "sk-001")): _make_skill_record(
                asset_id="sk-001",
                actor_id=ctx.actor_id,
                name="Frame Analysis",
            ),
        },
    )

    steps = [
        _make_specialty_step(
            "step1",
            agent_asset_id="sa-001",
            skill_asset_id="sk-001",  # Not in sa-001's compatibleSkills
        ),
    ]

    crud = WorkflowCRUD(s3=s3, index=index)

    with pytest.raises(ValueError) as exc_info:
        _run(crud.create(ctx, name="bad-workflow", steps=steps))

    assert "sk-001" in str(exc_info.value)
    assert "compatibleSkills" in str(exc_info.value)
    s3.write.assert_not_called()
    index.put.assert_not_awaited()


def test_create_rejects_invalid_core_agent_ref():
    """Create rejects when core agent ref is not 'task-agent'."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()

    steps = [
        WorkflowStep(
            step_id="step1",
            agent=AgentRef(kind="core", ref="invalid-agent"),
            skill=SkillRef(asset_id=None),
            tool_collection=ToolCollectionRef(asset_id=None),
            prompt="Do something",
            inputs=[],
        ),
    ]

    crud = WorkflowCRUD(s3=s3, index=index)

    with pytest.raises(ValueError) as exc_info:
        _run(crud.create(ctx, name="bad-workflow", steps=steps))

    assert "invalid-agent" in str(exc_info.value)
    assert "task-agent" in str(exc_info.value)
    s3.write.assert_not_called()
    index.put.assert_not_awaited()


# ---------------------------------------------------------------------------
# Read tests
# ---------------------------------------------------------------------------


def test_read_returns_workflow_data():
    """Read returns the workflow definition and resolved view."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()

    # Setup index.get to return a typed Workflow draft for wf-001 PLUS
    # the standard sa-001 / sk-001 / tc-001 references.
    drafts_pk = make_pk_user_owned(ctx.actor_id)
    _install_index_get(
        index,
        {
            (drafts_pk, make_sk_owned("workflow", "wf-001")): _make_workflow_record(
                asset_id="wf-001",
                actor_id=ctx.actor_id,
            ),
            (drafts_pk, make_sk_owned("specialty-agent", "sa-001")): _make_specialty_record(
                asset_id="sa-001",
                actor_id=ctx.actor_id,
                name="Moderation Specialty",
                compatible_skills=["sk-001"],
            ),
            (drafts_pk, make_sk_owned("skill", "sk-001")): _make_skill_record(
                asset_id="sk-001",
                actor_id=ctx.actor_id,
                name="Frame Analysis",
            ),
            (drafts_pk, make_sk_owned("tool-collection", "tc-001")): _make_tool_collection_record(
                asset_id="tc-001",
                actor_id=ctx.actor_id,
                name="Media Tools",
            ),
        },
    )

    # Set up S3 to return workflow.json
    workflow_doc = {
        "name": "moderation-workflow",
        "steps": [
            {
                "stepId": "extract",
                "agent": {"kind": "core", "ref": "task-agent"},
                "skill": {"assetId": None},
                "toolCollection": {"assetId": "tc-001"},
                "prompt": "Extract frames",
                "inputs": [],
            },
            {
                "stepId": "judge",
                "agent": {"kind": "specialty", "assetId": "sa-001"},
                "skill": {"assetId": "sk-001"},
                "toolCollection": {"assetId": None},
                "prompt": "Score the frames",
                "inputs": ["extract"],
            },
        ],
    }
    s3.read.return_value = json.dumps(workflow_doc).encode("utf-8")

    crud = WorkflowCRUD(s3=s3, index=index)

    result = _run(crud.read(ctx, "wf-001"))

    assert result is not None
    assert result.asset_id == "wf-001"
    assert result.name == "moderation-workflow"
    assert len(result.steps) == 2
    assert result.steps[0].step_id == "extract"
    assert result.steps[1].step_id == "judge"

    # Check resolved view
    assert len(result.resolved_view) == 2
    # First step: core agent, no skill, tc resolved
    assert result.resolved_view[0].agent_kind == "core"
    assert result.resolved_view[0].agent_resolved is True
    assert result.resolved_view[0].tool_collection_resolved is True
    assert result.resolved_view[0].tool_collection_name == "Media Tools"
    # Second step: specialty agent, skill resolved
    assert result.resolved_view[1].agent_kind == "specialty"
    assert result.resolved_view[1].agent_resolved is True
    assert result.resolved_view[1].agent_name == "Moderation Specialty"
    assert result.resolved_view[1].skill_resolved is True
    assert result.resolved_view[1].skill_name == "Frame Analysis"


def test_read_marks_deleted_references_as_unresolved():
    """Read marks deleted references as unresolved (Req 5.7, Property 12)."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()

    drafts_pk = make_pk_user_owned(ctx.actor_id)
    # The workflow exists; the referenced specialty and skill are gone.
    # `sa-deleted` is returned with a non-draft status (modeling a row
    # that has been promoted/deprecated and no longer counts as a
    # draft); `sk-deleted` is fully absent.
    deprecated_specialty = SpecialtyAgent(
        asset_id="sa-deleted",
        category="specialty-agent",
        name="Deleted Specialty",
        status="deprecated",
        scope="org",
        s3_prefix="org/o-acme/published/specialty-agents/sa-deleted/v1/",
        created_at=1700000000000,
        updated_at=1700000000000,
        version=1,
    )
    _install_index_get(
        index,
        {
            (drafts_pk, make_sk_owned("workflow", "wf-001")): _make_workflow_record(
                asset_id="wf-001",
                actor_id=ctx.actor_id,
            ),
            # `sa-deleted` resolves to a non-draft row (so the resolver
            # marks it unresolved) — but the (PK, SK) is still under the
            # drafts partition, mirroring how a deleted-then-promoted
            # row would be looked up.
            (drafts_pk, make_sk_owned("specialty-agent", "sa-deleted")): deprecated_specialty,
            # sk-deleted is omitted entirely ⇒ index.get returns None
        },
    )

    workflow_doc = {
        "name": "workflow-with-deleted-refs",
        "steps": [
            {
                "stepId": "step1",
                "agent": {"kind": "specialty", "assetId": "sa-deleted"},
                "skill": {"assetId": "sk-deleted"},
                "toolCollection": {"assetId": None},
                "prompt": "Do something",
                "inputs": [],
            },
        ],
    }
    s3.read.return_value = json.dumps(workflow_doc).encode("utf-8")

    crud = WorkflowCRUD(s3=s3, index=index)

    result = _run(crud.read(ctx, "wf-001"))

    assert result is not None
    # The stored definition is returned unchanged
    assert len(result.steps) == 1
    assert result.steps[0].agent.asset_id == "sa-deleted"
    assert result.steps[0].skill.asset_id == "sk-deleted"

    # But the resolved view marks them as unresolved
    assert result.resolved_view[0].agent_resolved is False
    assert result.resolved_view[0].agent_name is None
    assert result.resolved_view[0].skill_resolved is False
    assert result.resolved_view[0].skill_name is None


def test_read_marks_deleted_tool_collection_as_unresolved():
    """Read marks deleted tool collection references as unresolved (Req 5.7, Property 12)."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()

    drafts_pk = make_pk_user_owned(ctx.actor_id)
    archived_tc = ToolCollection(
        asset_id="tc-archived",
        category="tool-collection",
        name="Archived Tools",
        status="deprecated",
        version=1,
        scope="private",
        s3_prefix="tool-collections/tc-archived/",
        created_at=1700000000000,
        updated_at=1700000000000,
    )
    _install_index_get(
        index,
        {
            (drafts_pk, make_sk_owned("workflow", "wf-002")): _make_workflow_record(
                asset_id="wf-002",
                actor_id=ctx.actor_id,
            ),
            (drafts_pk, make_sk_owned("tool-collection", "tc-archived")): archived_tc,
        },
    )

    workflow_doc = {
        "name": "workflow-with-deleted-tc",
        "steps": [
            {
                "stepId": "step1",
                "agent": {"kind": "core", "ref": "task-agent"},
                "skill": {"assetId": None},
                "toolCollection": {"assetId": "tc-archived"},
                "prompt": "Do something",
                "inputs": [],
            },
        ],
    }
    s3.read.return_value = json.dumps(workflow_doc).encode("utf-8")

    crud = WorkflowCRUD(s3=s3, index=index)

    result = _run(crud.read(ctx, "wf-002"))

    assert result is not None
    # The stored definition is returned unchanged
    assert result.steps[0].tool_collection.asset_id == "tc-archived"
    # The resolved view marks it as unresolved
    assert result.resolved_view[0].tool_collection_resolved is False
    assert result.resolved_view[0].tool_collection_name is None
    # Agent (core) is still resolved
    assert result.resolved_view[0].agent_resolved is True
    # Skill (null) is still resolved (no reference to check)
    assert result.resolved_view[0].skill_resolved is True
    # S3 was NOT rewritten (read-only operation)
    s3.write.assert_not_called()


def test_read_multiple_unresolved_references_across_steps():
    """Read marks multiple unresolved references across multiple steps (Req 5.7, Property 12).

    Validates that when multiple assets referenced by different steps are deleted,
    each is independently marked as unresolved in the resolved view while the
    stored workflow.json remains unchanged.
    """
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()

    drafts_pk = make_pk_user_owned(ctx.actor_id)
    archived_tc = ToolCollection(
        asset_id="tc-gone",
        category="tool-collection",
        name="Gone Tools",
        status="deprecated",
        version=1,
        scope="private",
        s3_prefix="tool-collections/tc-gone/",
        created_at=1700000000000,
        updated_at=1700000000000,
    )
    _install_index_get(
        index,
        {
            (drafts_pk, make_sk_owned("workflow", "wf-multi")): _make_workflow_record(
                asset_id="wf-multi",
                actor_id=ctx.actor_id,
                name="Multi-Ref Workflow",
            ),
            # Active drafts resolve cleanly.
            (drafts_pk, make_sk_owned("specialty-agent", "sa-active")): _make_specialty_record(
                asset_id="sa-active",
                actor_id=ctx.actor_id,
                name="Active Specialty",
                compatible_skills=["sk-active"],
            ),
            (drafts_pk, make_sk_owned("skill", "sk-active")): _make_skill_record(
                asset_id="sk-active",
                actor_id=ctx.actor_id,
                name="Active Skill",
            ),
            # Deprecated tool-collection ⇒ resolver marks as unresolved.
            (drafts_pk, make_sk_owned("tool-collection", "tc-gone")): archived_tc,
            # sa-gone / sk-gone are absent ⇒ index.get returns None.
        },
    )

    workflow_doc = {
        "name": "multi-ref-workflow",
        "steps": [
            {
                "stepId": "step1",
                "agent": {"kind": "specialty", "assetId": "sa-gone"},
                "skill": {"assetId": "sk-gone"},
                "toolCollection": {"assetId": "tc-gone"},
                "prompt": "All refs deleted",
                "inputs": [],
            },
            {
                "stepId": "step2",
                "agent": {"kind": "specialty", "assetId": "sa-active"},
                "skill": {"assetId": "sk-active"},
                "toolCollection": {"assetId": None},
                "prompt": "All refs valid",
                "inputs": ["step1"],
            },
        ],
    }
    s3.read.return_value = json.dumps(workflow_doc).encode("utf-8")

    crud = WorkflowCRUD(s3=s3, index=index)

    result = _run(crud.read(ctx, "wf-multi"))

    assert result is not None
    # Stored definition is unchanged
    assert len(result.steps) == 2
    assert result.steps[0].agent.asset_id == "sa-gone"
    assert result.steps[0].skill.asset_id == "sk-gone"
    assert result.steps[0].tool_collection.asset_id == "tc-gone"
    assert result.steps[1].agent.asset_id == "sa-active"
    assert result.steps[1].skill.asset_id == "sk-active"

    # Step 1: all three references unresolved
    view0 = result.resolved_view[0]
    assert view0.agent_resolved is False
    assert view0.agent_name is None
    assert view0.skill_resolved is False
    assert view0.skill_name is None
    assert view0.tool_collection_resolved is False
    assert view0.tool_collection_name is None

    # Step 2: all references resolved
    view1 = result.resolved_view[1]
    assert view1.agent_resolved is True
    assert view1.agent_name == "Active Specialty"
    assert view1.skill_resolved is True
    assert view1.skill_name == "Active Skill"
    assert view1.tool_collection_resolved is True  # None ref = no check needed

    # S3 was NOT rewritten (read-only operation)
    s3.write.assert_not_called()


def test_read_returns_none_for_nonexistent():
    """Read returns None when the asset doesn't exist."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()
    # Default mock returns None for any (PK, SK).

    crud = WorkflowCRUD(s3=s3, index=index)

    result = _run(crud.read(ctx, "nonexistent-id"))
    assert result is None


def test_read_returns_none_for_archived():
    """Read returns None for non-draft rows (e.g., promoted/deprecated)."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()
    # The new schema models lifecycle stage with `status`; a promoted
    # row would carry `status="published"` (or `"deprecated"`), which
    # the read path treats as not-found because it scopes to drafts.
    drafts_pk = make_pk_user_owned(ctx.actor_id)
    promoted = Workflow(
        asset_id="wf-001",
        category="workflow",
        name="archived-workflow",
        status="published",
        scope="private",
        s3_prefix=f"users/{ctx.actor_id}/published/workflows/wf-001/v1/",
        created_at=1700000000000,
        updated_at=1700000000000,
        version=1,
    )
    _install_index_get(
        index,
        {(drafts_pk, make_sk_owned("workflow", "wf-001")): promoted},
    )

    crud = WorkflowCRUD(s3=s3, index=index)

    result = _run(crud.read(ctx, "wf-001"))
    assert result is None


# ---------------------------------------------------------------------------
# Update tests
# ---------------------------------------------------------------------------


def test_update_name_only():
    """Update with only name change succeeds without re-validating steps."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()

    drafts_pk = make_pk_user_owned(ctx.actor_id)
    _install_index_get(
        index,
        {
            (drafts_pk, make_sk_owned("workflow", "wf-001")): _make_workflow_record(
                asset_id="wf-001",
                actor_id=ctx.actor_id,
                name="old-name",
            )
        },
    )

    # S3 returns current workflow.json
    workflow_doc = {
        "name": "old-name",
        "steps": [
            {
                "stepId": "step1",
                "agent": {"kind": "core", "ref": "task-agent"},
                "skill": {"assetId": None},
                "toolCollection": {"assetId": None},
                "prompt": "Do something",
                "inputs": [],
            },
        ],
    }
    s3.read.return_value = json.dumps(workflow_doc).encode("utf-8")

    crud = WorkflowCRUD(s3=s3, index=index)

    result = _run(crud.update(ctx, "wf-001", name="new-name"))

    assert result is not None
    assert "updatedAt" in result
    s3.write.assert_called_once()
    # The new update API takes (pk, sk, update_expression=..., ...)
    # kwargs; assert that the row was updated under the canonical key.
    index.update.assert_awaited_once()
    call = index.update.call_args
    assert call.args[0] == drafts_pk
    assert call.args[1] == make_sk_owned("workflow", "wf-001")
    assert "SET" in call.kwargs["update_expression"]
    # Both updatedAt and name appear in the expression-attribute-values
    # because the caller asked for a name change.
    assert ":name" in call.kwargs["expression_attribute_values"]
    assert call.kwargs["expression_attribute_values"][":name"] == "new-name"


def test_update_steps_triggers_revalidation():
    """Update with new steps performs full re-validation."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()

    drafts_pk = make_pk_user_owned(ctx.actor_id)
    _install_index_get(
        index,
        {
            (drafts_pk, make_sk_owned("workflow", "wf-001")): _make_workflow_record(
                asset_id="wf-001",
                actor_id=ctx.actor_id,
                name="my-workflow",
            )
        },
    )

    crud = WorkflowCRUD(s3=s3, index=index)

    new_steps = [_make_core_step("new-step", "New task")]
    result = _run(crud.update(ctx, "wf-001", steps=new_steps))

    assert result is not None
    assert "updatedAt" in result
    s3.write.assert_called_once()


def test_update_rejects_invalid_steps():
    """Update rejects invalid steps without writing (Property 11)."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()

    drafts_pk = make_pk_user_owned(ctx.actor_id)
    _install_index_get(
        index,
        {
            (drafts_pk, make_sk_owned("workflow", "wf-001")): _make_workflow_record(
                asset_id="wf-001",
                actor_id=ctx.actor_id,
                name="my-workflow",
            )
        },
    )

    crud = WorkflowCRUD(s3=s3, index=index)

    # Steps with out-of-order dependency
    bad_steps = [
        _make_core_step("step1", "First", inputs=["step2"]),
        _make_core_step("step2", "Second"),
    ]

    with pytest.raises(ValueError):
        _run(crud.update(ctx, "wf-001", steps=bad_steps))

    # Property 11: no S3 write or AssetIndex update on validation failure.
    s3.write.assert_not_called()
    index.update.assert_not_awaited()


def test_update_returns_none_for_nonexistent():
    """Update returns None when the asset doesn't exist."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()
    # Default index.get returns None ⇒ caller treats as not-found.

    crud = WorkflowCRUD(s3=s3, index=index)

    result = _run(crud.update(ctx, "nonexistent", name="new-name"))
    assert result is None
    s3.write.assert_not_called()
    index.update.assert_not_awaited()


# ---------------------------------------------------------------------------
# Delete tests
# ---------------------------------------------------------------------------


def test_delete_two_phase():
    """Delete performs two-phase: hard-delete index row, then delete S3 prefix."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()
    # Delete now fetches the row first to reclaim the canonical (bucket-absolute)
    # prefix where artifacts physically live (BUG-2), so seed a record.
    record = _make_workflow_record(asset_id="wf-001", actor_id=ctx.actor_id)
    index.get = AsyncMock(return_value=record)

    crud = WorkflowCRUD(s3=s3, index=index)

    result = _run(crud.delete(ctx, "wf-001"))

    assert result is True
    # Phase 1: hard-delete via transact_write under the canonical key,
    # gated by attribute_exists(PK) so a missing row surfaces as
    # not-found rather than 5xx.
    index.transact_write.assert_awaited_once()
    ops = index.transact_write.call_args.args[0]
    assert len(ops) == 1
    delete_op = ops[0]["Delete"]
    assert delete_op["TableName"] == index.table_name
    assert delete_op["Key"]["PK"]["S"] == make_pk_user_owned(ctx.actor_id)
    assert delete_op["Key"]["SK"]["S"] == make_sk_owned(_CATEGORY, "wf-001")
    assert delete_op["ConditionExpression"] == "attribute_exists(PK)"
    # Phase 2: S3 prefix delete at the record's canonical (bucket-absolute)
    # prefix, matching where create/update write artifacts (BUG-2).
    s3.delete_prefix_absolute.assert_called_once_with(record.s3_prefix)


def test_delete_returns_none_for_nonexistent():
    """Delete returns None when the asset doesn't exist (condition fails)."""
    from botocore.exceptions import ClientError

    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()

    # Simulate the AssetIndex transaction cancelling because the
    # attribute_exists(PK) precondition was not satisfied (i.e., the
    # row never existed for this actor).
    index.transact_write.side_effect = ClientError(
        {
            "Error": {"Code": "TransactionCanceledException", "Message": "txn cancelled"},
            "CancellationReasons": [{"Code": "ConditionalCheckFailed"}],
        },
        "TransactWriteItems",
    )

    crud = WorkflowCRUD(s3=s3, index=index)

    result = _run(crud.delete(ctx, "nonexistent"))
    assert result is None
    s3.delete_prefix.assert_not_called()


# ---------------------------------------------------------------------------
# List tests
# ---------------------------------------------------------------------------


def test_list_returns_active_drafts():
    """List returns all workflow drafts for the actor."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()

    drafts = [
        _make_workflow_record(asset_id="wf-001", actor_id=ctx.actor_id, name="workflow-1"),
        _make_workflow_record(asset_id="wf-002", actor_id=ctx.actor_id, name="workflow-2"),
    ]
    # query_pk returns (items, last_evaluated_key); pagination is
    # internal to the list method's contract.
    index.query_pk.return_value = (drafts, None)

    crud = WorkflowCRUD(s3=s3, index=index)

    results = _run(crud.list(ctx))

    assert len(results) == 2
    assert all(isinstance(r, Workflow) for r in results)
    index.query_pk.assert_awaited_once()
    call = index.query_pk.call_args
    assert call.args[0] == make_pk_user_owned(ctx.actor_id)
    assert call.kwargs["sk_prefix"] == f"{_CATEGORY}#"


def test_list_returns_empty_when_no_drafts():
    """List returns empty list when no workflows exist."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()
    # Default mock returns ([], None).

    crud = WorkflowCRUD(s3=s3, index=index)

    results = _run(crud.list(ctx))
    assert results == []
