"""Tests for PromotionReadiness — validates the promotion-readiness checker.

Tests cover:
- Owner check: returns None for non-existent or non-draft workflows
- Zero-step short-circuit: single entry with zero-steps failure (Property 24)
- Transitive reference enumeration (Requirement 8.2)
- Skill readiness: missing/empty SKILL.md, no name (Requirement 8.5)
- Specialty readiness: empty persona, dangling compatible-skill (Requirement 8.6)
- Tool collection readiness: empty collection, unresolved tool ref (Requirement 8.7)
- Workflow-level rules: unresolved refs, out-of-order inputs, incompatible pairing (Requirement 8.3)
- Overall status is conjunction of all entries (Property 16)
- Read-only: no writes to S3/DDB (Property 18)
- not_ready entries have non-empty failures; ready entries have empty (Property 17)

These fixtures use the unified-platform AssetIndex API: ``index.get`` is an
``AsyncMock`` keyed on ``(PK, SK)`` and returns typed dataclass records
(:class:`Skill`, :class:`SpecialtyAgent`, :class:`Workflow`,
:class:`ToolCollection`) whose ``status`` is ``"draft"`` for active drafts.
The legacy ``status="active"`` / ``"archived"`` domain has been replaced
by ``draft | published | deprecated`` (see the unified-mediaworks-platform
spec); readiness operates on drafts so non-draft rows surface as
not-found.

Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 8.9, 8.10
Properties: 16, 17, 18, 19, 24
"""

from __future__ import annotations

import asyncio
import json
import sys
import os
from unittest.mock import MagicMock, AsyncMock

# Add agent/src to path so imports resolve
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab.asset_index import (
    make_pk_user_owned,
    make_sk_owned,
)
from lab.context import LabRequestContext
from lab.models import (
    GatewayProjection,
    GatewayToolDescriptor,
    Skill,
    SpecialtyAgent,
    ToolCollection,
    ToolRef,
    Workflow,
)
from lab.readiness import FAILURE_ZERO_STEPS, PromotionReadiness


def _run(coro):
    """Helper to run async coroutines in sync tests."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


_TS = 1700000000000


def _make_ctx(
    actor_id: str = "actor-123", session_id: str = "session-abc"
) -> LabRequestContext:
    """Create a test LabRequestContext."""
    return LabRequestContext(
        actor_id=actor_id,
        session_id=session_id,
        request_id="req-test-001",
    )


def _make_mock_s3() -> MagicMock:
    """Create a mock S3UserFilesTool.

    ``read_absolute``/``list_absolute``/``write_absolute`` delegate to their
    actor-scoped counterparts so existing tests that stub ``s3.read`` keep
    driving the readiness code after it moved to canonical-prefix reads
    (BUG-2). Tests that need to distinguish physical keys can still override
    ``read_absolute`` directly.
    """
    s3 = MagicMock()
    s3.write = AsyncMock()
    s3.read = AsyncMock()
    s3.delete = AsyncMock()
    s3.delete_prefix = AsyncMock()
    s3.list = AsyncMock(return_value=[])
    s3.actor_id = "actor-1"
    # Absolute variants ARE the scoped mocks, so a test setting
    # `s3.read.return_value` also drives `read_absolute` (the code moved to
    # canonical-prefix reads for BUG-2). Sharing the object avoids
    # coroutine-wrapping issues from delegating side_effects.
    s3.read_absolute = s3.read
    s3.write_absolute = s3.write
    s3.list_absolute = s3.list
    s3.delete_absolute = s3.delete
    s3.delete_prefix_absolute = s3.delete_prefix
    return s3


def _make_mock_index() -> MagicMock:
    """Create a mock AssetIndex with the unified-platform async API.

    All read/write methods are AsyncMocks. ``get`` defaults to returning
    ``None`` so a test that does not explicitly install records sees the
    same not-found semantics as a live AssetIndex with an empty table.
    """
    index = MagicMock()
    index.put = AsyncMock(return_value=None)
    index.get = AsyncMock(return_value=None)
    index.update = AsyncMock(return_value=None)
    index.delete = AsyncMock(return_value=None)
    index.query_pk = AsyncMock(return_value=([], None))
    index.query_lineage = AsyncMock(return_value=[])
    index.transact_write = AsyncMock(return_value=None)
    return index


def _make_mock_gateway_browser(tools=None) -> MagicMock:
    """Create a mock GatewayBrowser with optional tool projection."""
    browser = MagicMock()
    if tools is not None:
        projection = GatewayProjection(
            retrieved_at=_TS,
            actor_id="actor-123",
            tools=tools,
        )
        browser.get_projection.return_value = projection
    else:
        browser.get_projection.return_value = None
    return browser


def _make_workflow_json(name: str, steps: list[dict]) -> bytes:
    """Build a workflow.json document as bytes."""
    doc = {"name": name, "steps": steps}
    return json.dumps(doc).encode("utf-8")


def _make_step(
    step_id: str,
    agent_kind: str = "core",
    agent_ref: str = "task-agent",
    agent_asset_id: str = None,
    skill_asset_id: str = None,
    tc_asset_id: str = None,
    prompt: str = "Do something",
    inputs: list[str] = None,
) -> dict:
    """Build a raw workflow step dict for workflow.json."""
    step = {
        "stepId": step_id,
        "agent": {"kind": agent_kind},
        "skill": {"assetId": skill_asset_id},
        "toolCollection": {"assetId": tc_asset_id},
        "prompt": prompt,
        "inputs": inputs or [],
    }
    if agent_kind == "core":
        step["agent"]["ref"] = agent_ref
    if agent_asset_id:
        step["agent"]["assetId"] = agent_asset_id
    return step


# ---------------------------------------------------------------------------
# Typed-record builders for the index.get mock
# ---------------------------------------------------------------------------


def _workflow_record(
    *,
    asset_id: str,
    actor_id: str = "actor-123",
    name: str = "WF",
    status: str = "draft",
) -> Workflow:
    return Workflow(
        asset_id=asset_id,
        category="workflow",
        name=name,
        status=status,
        version=None if status == "draft" else 1,
        scope="private",
        s3_prefix=f"users/{actor_id}/drafts/workflows/{asset_id}/",
        created_at=_TS,
        updated_at=_TS,
        steps=[],
    )


def _specialty_record(
    *,
    asset_id: str,
    actor_id: str = "actor-123",
    name: str = "Specialty",
    compatible_skills: list[str] | None = None,
    status: str = "draft",
) -> SpecialtyAgent:
    return SpecialtyAgent(
        asset_id=asset_id,
        category="specialty-agent",
        name=name,
        status=status,
        scope="private",
        s3_prefix=f"users/{actor_id}/drafts/specialty-agents/{asset_id}/",
        created_at=_TS,
        updated_at=_TS,
        compatible_skills=list(compatible_skills or []),
    )


def _skill_record(
    *,
    asset_id: str,
    actor_id: str = "actor-123",
    name: str = "Skill",
    status: str = "draft",
) -> Skill:
    return Skill(
        asset_id=asset_id,
        category="skill",
        name=name,
        status=status,
        scope="private",
        s3_prefix=f"users/{actor_id}/drafts/skills/{asset_id}/",
        created_at=_TS,
        updated_at=_TS,
    )


def _tool_collection_record(
    *,
    asset_id: str,
    actor_id: str = "actor-123",
    name: str = "TC",
    tool_refs: list[ToolRef] | list[dict] | None = None,
    status: str = "draft",
) -> ToolCollection:
    refs: list[ToolRef] = []
    for raw in tool_refs or []:
        if isinstance(raw, ToolRef):
            refs.append(raw)
        else:
            refs.append(ToolRef(name=raw["name"], version=raw["version"]))
    return ToolCollection(
        asset_id=asset_id,
        category="tool-collection",
        name=name,
        status=status,
        scope="private",
        s3_prefix=f"users/{actor_id}/tool-collections/{asset_id}/",
        created_at=_TS,
        updated_at=_TS,
        tool_refs=refs,
    )


def _install_index_get(
    index: MagicMock,
    records: dict[tuple[str, str], object],
) -> None:
    """Install an async side_effect on ``index.get`` from a (PK, SK) map.

    Returns ``None`` for keys not present, matching real AssetIndex
    semantics (missing rows ⇒ None).
    """

    async def _get(pk: str, sk: str):
        return records.get((pk, sk))

    index.get.side_effect = _get


# Convenience PK/SK builders used throughout the tests.
def _pk(actor_id: str = "actor-123") -> str:
    return make_pk_user_owned(actor_id)


def _tcs_pk(actor_id: str = "actor-123") -> str:
    return make_pk_user_owned(actor_id)


def _wf_sk(asset_id: str) -> str:
    return make_sk_owned("workflow", asset_id)


def _sk_sk(asset_id: str) -> str:
    return make_sk_owned("skill", asset_id)


def _sa_sk(asset_id: str) -> str:
    return make_sk_owned("specialty-agent", asset_id)


def _tc_sk(asset_id: str) -> str:
    return make_sk_owned("tool-collection", asset_id)


# ---------------------------------------------------------------------------
# Owner check tests (Requirement 8.1)
# ---------------------------------------------------------------------------


def test_check_returns_none_for_nonexistent_workflow():
    """check() returns None when the workflow doesn't exist in the index."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()
    browser = _make_mock_gateway_browser()

    readiness = PromotionReadiness(s3=s3, index=index, gateway_browser=browser)
    result = _run(readiness.check(ctx, "nonexistent-wf"))

    assert result is None
    # Read-only: no writes
    s3.write.assert_not_called()
    index.put.assert_not_called()
    index.update.assert_not_called()


def test_check_returns_none_for_non_draft_workflow():
    """check() returns None when the workflow row is not in draft status.

    The new schema has no ``archived`` status; a workflow row whose
    ``status`` is ``published`` or ``deprecated`` is not a draft and is
    therefore not eligible for promotion checking.
    """
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()
    browser = _make_mock_gateway_browser()

    _install_index_get(
        index,
        {
            (_pk(), _wf_sk("wf-001")): _workflow_record(
                asset_id="wf-001", status="published"
            ),
        },
    )

    readiness = PromotionReadiness(s3=s3, index=index, gateway_browser=browser)
    result = _run(readiness.check(ctx, "wf-001"))

    assert result is None


# ---------------------------------------------------------------------------
# Zero-step short-circuit (Requirement 8.4, Property 24)
# ---------------------------------------------------------------------------


def test_zero_step_workflow_short_circuits():
    """Zero-step workflow returns single entry with zero-steps failure (Property 24)."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()
    browser = _make_mock_gateway_browser()

    _install_index_get(
        index,
        {
            (_pk(), _wf_sk("wf-empty")): _workflow_record(
                asset_id="wf-empty", name="Empty Workflow"
            ),
        },
    )

    # workflow.json with zero steps
    s3.read.return_value = _make_workflow_json("Empty Workflow", [])

    readiness = PromotionReadiness(s3=s3, index=index, gateway_browser=browser)
    result = _run(readiness.check(ctx, "wf-empty"))

    assert result is not None
    assert result.workflow_asset_id == "wf-empty"
    assert result.overall_status == "not_ready"
    # Exactly one entry
    assert len(result.entries) == 1
    entry = result.entries[0]
    assert entry.category == "workflow"
    assert entry.asset_id == "wf-empty"
    assert entry.status == "not_ready"
    assert len(entry.failures) == 1
    assert entry.failures[0] == FAILURE_ZERO_STEPS


def test_zero_step_workflow_no_per_asset_entries():
    """Zero-step workflow does NOT enumerate per-asset entries (Property 24)."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()
    browser = _make_mock_gateway_browser()

    _install_index_get(
        index,
        {
            (_pk(), _wf_sk("wf-empty")): _workflow_record(
                asset_id="wf-empty", name="Empty Workflow"
            ),
        },
    )

    s3.read.return_value = _make_workflow_json("Empty Workflow", [])

    readiness = PromotionReadiness(s3=s3, index=index, gateway_browser=browser)
    result = _run(readiness.check(ctx, "wf-empty"))

    # No skill, specialty, or tool-collection entries
    categories = [e.category for e in result.entries]
    assert "skill" not in categories
    assert "specialty-agent" not in categories
    assert "tool-collection" not in categories


# ---------------------------------------------------------------------------
# Skill readiness (Requirement 8.5)
# ---------------------------------------------------------------------------


def test_skill_missing_skill_md_is_not_ready():
    """Skill with missing SKILL.md is marked not_ready (Requirement 8.5)."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()
    browser = _make_mock_gateway_browser()

    _install_index_get(
        index,
        {
            (_pk(), _wf_sk("wf-001")): _workflow_record(asset_id="wf-001"),
            (_pk(), _sk_sk("sk-001")): _skill_record(asset_id="sk-001"),
        },
    )

    steps = [_make_step("s1", skill_asset_id="sk-001")]
    s3.read.side_effect = [
        _make_workflow_json("WF", steps),  # workflow.json
        FileNotFoundError("SKILL.md not found"),  # SKILL.md read fails
    ]

    readiness = PromotionReadiness(s3=s3, index=index, gateway_browser=browser)
    result = _run(readiness.check(ctx, "wf-001"))

    assert result is not None
    skill_entries = [e for e in result.entries if e.category == "skill"]
    assert len(skill_entries) == 1
    assert skill_entries[0].status == "not_ready"
    assert "missing" in skill_entries[0].failures[0].lower()


def test_skill_empty_skill_md_is_not_ready():
    """Skill with empty SKILL.md is marked not_ready (Requirement 8.5)."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()
    browser = _make_mock_gateway_browser()

    _install_index_get(
        index,
        {
            (_pk(), _wf_sk("wf-001")): _workflow_record(asset_id="wf-001"),
            (_pk(), _sk_sk("sk-001")): _skill_record(asset_id="sk-001"),
        },
    )

    steps = [_make_step("s1", skill_asset_id="sk-001")]
    s3.read.side_effect = [
        _make_workflow_json("WF", steps),  # workflow.json
        b"",  # SKILL.md is empty
    ]

    readiness = PromotionReadiness(s3=s3, index=index, gateway_browser=browser)
    result = _run(readiness.check(ctx, "wf-001"))

    skill_entries = [e for e in result.entries if e.category == "skill"]
    assert len(skill_entries) == 1
    assert skill_entries[0].status == "not_ready"
    assert "empty" in skill_entries[0].failures[0].lower()


def test_skill_no_name_is_not_ready():
    """Skill with SKILL.md that has no name declaration is not_ready (Requirement 8.5)."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()
    browser = _make_mock_gateway_browser()

    _install_index_get(
        index,
        {
            (_pk(), _wf_sk("wf-001")): _workflow_record(asset_id="wf-001"),
            (_pk(), _sk_sk("sk-001")): _skill_record(asset_id="sk-001"),
        },
    )

    steps = [_make_step("s1", skill_asset_id="sk-001")]
    # SKILL.md with content but no name (no heading, no frontmatter name)
    skill_content = b"This is just some text without a heading or frontmatter name."
    s3.read.side_effect = [
        _make_workflow_json("WF", steps),
        skill_content,
    ]

    readiness = PromotionReadiness(s3=s3, index=index, gateway_browser=browser)
    result = _run(readiness.check(ctx, "wf-001"))

    skill_entries = [e for e in result.entries if e.category == "skill"]
    assert len(skill_entries) == 1
    assert skill_entries[0].status == "not_ready"
    assert "name" in skill_entries[0].failures[0].lower()


def test_skill_with_valid_name_is_ready():
    """Skill with valid SKILL.md (has name) is marked ready."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()
    browser = _make_mock_gateway_browser()

    _install_index_get(
        index,
        {
            (_pk(), _wf_sk("wf-001")): _workflow_record(asset_id="wf-001"),
            (_pk(), _sk_sk("sk-001")): _skill_record(asset_id="sk-001"),
        },
    )

    steps = [_make_step("s1", skill_asset_id="sk-001")]
    skill_content = b"# My Skill\n\nThis skill does something useful."
    s3.read.side_effect = [
        _make_workflow_json("WF", steps),
        skill_content,
    ]

    readiness = PromotionReadiness(s3=s3, index=index, gateway_browser=browser)
    result = _run(readiness.check(ctx, "wf-001"))

    skill_entries = [e for e in result.entries if e.category == "skill"]
    assert len(skill_entries) == 1
    assert skill_entries[0].status == "ready"
    assert skill_entries[0].failures == []


# ---------------------------------------------------------------------------
# Specialty readiness (Requirement 8.6)
# ---------------------------------------------------------------------------


def test_specialty_with_dangling_compatible_skill_is_not_ready():
    """Specialty with dangling compatible-skill reference is not_ready (Requirement 8.6)."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()
    browser = _make_mock_gateway_browser()

    # Specialty references sk-gone in compatibleSkills, but no record
    # exists at (drafts_pk, skill#sk-gone).
    _install_index_get(
        index,
        {
            (_pk(), _wf_sk("wf-001")): _workflow_record(asset_id="wf-001"),
            (_pk(), _sa_sk("sa-001")): _specialty_record(
                asset_id="sa-001",
                compatible_skills=["sk-gone"],
            ),
        },
    )

    steps = [_make_step("s1", agent_kind="specialty", agent_asset_id="sa-001")]
    s3.read.return_value = _make_workflow_json("WF", steps)

    readiness = PromotionReadiness(s3=s3, index=index, gateway_browser=browser)
    result = _run(readiness.check(ctx, "wf-001"))

    specialty_entries = [
        e for e in result.entries if e.category == "specialty-agent"
    ]
    assert len(specialty_entries) == 1
    assert specialty_entries[0].status == "not_ready"
    assert any("sk-gone" in f for f in specialty_entries[0].failures)


def test_specialty_with_valid_compatible_skills_is_ready():
    """Specialty with all compatible skills resolving is ready."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()
    browser = _make_mock_gateway_browser()

    _install_index_get(
        index,
        {
            (_pk(), _wf_sk("wf-001")): _workflow_record(asset_id="wf-001"),
            (_pk(), _sa_sk("sa-001")): _specialty_record(
                asset_id="sa-001",
                compatible_skills=["sk-001"],
            ),
            (_pk(), _sk_sk("sk-001")): _skill_record(asset_id="sk-001"),
        },
    )

    steps = [
        _make_step(
            "s1",
            agent_kind="specialty",
            agent_asset_id="sa-001",
            skill_asset_id="sk-001",
        )
    ]
    skill_content = b"# My Skill\n\nContent."
    s3.read.side_effect = [
        _make_workflow_json("WF", steps),
        skill_content,  # SKILL.md for sk-001
    ]

    readiness = PromotionReadiness(s3=s3, index=index, gateway_browser=browser)
    result = _run(readiness.check(ctx, "wf-001"))

    specialty_entries = [
        e for e in result.entries if e.category == "specialty-agent"
    ]
    assert len(specialty_entries) == 1
    assert specialty_entries[0].status == "ready"
    assert specialty_entries[0].failures == []


# ---------------------------------------------------------------------------
# Tool collection readiness (Requirement 8.7)
# ---------------------------------------------------------------------------


def test_tool_collection_empty_is_not_ready():
    """Empty tool collection is marked not_ready (Requirement 8.7)."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()
    browser = _make_mock_gateway_browser()

    _install_index_get(
        index,
        {
            (_pk(), _wf_sk("wf-001")): _workflow_record(asset_id="wf-001"),
            # Tool collections live in their own flat partition.
            (_tcs_pk(), _tc_sk("tc-001")): _tool_collection_record(
                asset_id="tc-001", name="Empty TC", tool_refs=[]
            ),
        },
    )

    steps = [_make_step("s1", tc_asset_id="tc-001")]
    s3.read.return_value = _make_workflow_json("WF", steps)

    readiness = PromotionReadiness(s3=s3, index=index, gateway_browser=browser)
    result = _run(readiness.check(ctx, "wf-001"))

    tc_entries = [e for e in result.entries if e.category == "tool-collection"]
    assert len(tc_entries) == 1
    assert tc_entries[0].status == "not_ready"
    assert "empty" in tc_entries[0].failures[0].lower()


def test_tool_collection_unresolved_tool_is_not_ready():
    """Tool collection with unresolved Gateway tool is not_ready (Requirement 8.7)."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()

    # Gateway has tool "frame-extract@2.1.0" but NOT "missing-tool@1.0.0"
    gateway_tools = [
        GatewayToolDescriptor(
            name="frame-extract", description="Extract", version="2.1.0"
        ),
    ]
    browser = _make_mock_gateway_browser(tools=gateway_tools)

    _install_index_get(
        index,
        {
            (_pk(), _wf_sk("wf-001")): _workflow_record(asset_id="wf-001"),
            (_tcs_pk(), _tc_sk("tc-001")): _tool_collection_record(
                asset_id="tc-001",
                name="My TC",
                tool_refs=[
                    {"name": "frame-extract", "version": "2.1.0"},
                    {"name": "missing-tool", "version": "1.0.0"},
                ],
            ),
        },
    )

    steps = [_make_step("s1", tc_asset_id="tc-001")]
    s3.read.return_value = _make_workflow_json("WF", steps)

    readiness = PromotionReadiness(s3=s3, index=index, gateway_browser=browser)
    result = _run(readiness.check(ctx, "wf-001"))

    tc_entries = [e for e in result.entries if e.category == "tool-collection"]
    assert len(tc_entries) == 1
    assert tc_entries[0].status == "not_ready"
    assert any("missing-tool" in f for f in tc_entries[0].failures)


def test_tool_collection_all_resolved_is_ready():
    """Tool collection with all tools resolving against Gateway is ready."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()

    gateway_tools = [
        GatewayToolDescriptor(
            name="frame-extract", description="Extract", version="2.1.0"
        ),
        GatewayToolDescriptor(
            name="speech-to-text", description="STT", version="1.4.2"
        ),
    ]
    browser = _make_mock_gateway_browser(tools=gateway_tools)

    _install_index_get(
        index,
        {
            (_pk(), _wf_sk("wf-001")): _workflow_record(asset_id="wf-001"),
            (_tcs_pk(), _tc_sk("tc-001")): _tool_collection_record(
                asset_id="tc-001",
                name="My TC",
                tool_refs=[
                    {"name": "frame-extract", "version": "2.1.0"},
                    {"name": "speech-to-text", "version": "1.4.2"},
                ],
            ),
        },
    )

    steps = [_make_step("s1", tc_asset_id="tc-001")]
    s3.read.return_value = _make_workflow_json("WF", steps)

    readiness = PromotionReadiness(s3=s3, index=index, gateway_browser=browser)
    result = _run(readiness.check(ctx, "wf-001"))

    tc_entries = [e for e in result.entries if e.category == "tool-collection"]
    assert len(tc_entries) == 1
    assert tc_entries[0].status == "ready"
    assert tc_entries[0].failures == []


# ---------------------------------------------------------------------------
# Workflow-level rules (Requirement 8.3)
# ---------------------------------------------------------------------------


def test_workflow_unresolved_agent_is_not_ready():
    """Workflow with unresolved agent reference is not_ready (Requirement 8.3)."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()
    browser = _make_mock_gateway_browser()

    # Specialty agent doesn't exist — only the workflow row resolves.
    _install_index_get(
        index,
        {
            (_pk(), _wf_sk("wf-001")): _workflow_record(asset_id="wf-001"),
        },
    )

    steps = [_make_step("s1", agent_kind="specialty", agent_asset_id="sa-gone")]
    s3.read.return_value = _make_workflow_json("WF", steps)

    readiness = PromotionReadiness(s3=s3, index=index, gateway_browser=browser)
    result = _run(readiness.check(ctx, "wf-001"))

    wf_entries = [e for e in result.entries if e.category == "workflow"]
    assert len(wf_entries) == 1
    assert wf_entries[0].status == "not_ready"
    assert any("sa-gone" in f for f in wf_entries[0].failures)


def test_workflow_out_of_order_input_is_not_ready():
    """Workflow with out-of-order input dependency is not_ready (Requirement 8.3)."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()
    browser = _make_mock_gateway_browser()

    _install_index_get(
        index,
        {
            (_pk(), _wf_sk("wf-001")): _workflow_record(asset_id="wf-001"),
        },
    )

    # step1 references step2 which comes later
    steps = [
        _make_step("step1", inputs=["step2"]),
        _make_step("step2"),
    ]
    s3.read.return_value = _make_workflow_json("WF", steps)

    readiness = PromotionReadiness(s3=s3, index=index, gateway_browser=browser)
    result = _run(readiness.check(ctx, "wf-001"))

    wf_entries = [e for e in result.entries if e.category == "workflow"]
    assert len(wf_entries) == 1
    assert wf_entries[0].status == "not_ready"
    assert any("step2" in f and "earlier" in f for f in wf_entries[0].failures)


def test_workflow_incompatible_skill_specialty_is_not_ready():
    """Workflow with incompatible skill/specialty pairing is not_ready (Requirement 8.3)."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()
    browser = _make_mock_gateway_browser()

    _install_index_get(
        index,
        {
            (_pk(), _wf_sk("wf-001")): _workflow_record(asset_id="wf-001"),
            (_pk(), _sa_sk("sa-001")): _specialty_record(
                asset_id="sa-001",
                compatible_skills=["sk-other"],  # sk-001 NOT listed
            ),
            (_pk(), _sk_sk("sk-001")): _skill_record(asset_id="sk-001"),
        },
    )

    steps = [
        _make_step(
            "s1",
            agent_kind="specialty",
            agent_asset_id="sa-001",
            skill_asset_id="sk-001",
        )
    ]
    skill_content = b"# My Skill\n\nContent."
    s3.read.side_effect = [
        _make_workflow_json("WF", steps),
        skill_content,  # SKILL.md for sk-001
    ]

    readiness = PromotionReadiness(s3=s3, index=index, gateway_browser=browser)
    result = _run(readiness.check(ctx, "wf-001"))

    wf_entries = [e for e in result.entries if e.category == "workflow"]
    assert len(wf_entries) == 1
    assert wf_entries[0].status == "not_ready"
    assert any(
        "sk-001" in f and "compatible" in f.lower()
        for f in wf_entries[0].failures
    )


# ---------------------------------------------------------------------------
# Overall status (Property 16) and failure list invariants (Property 17)
# ---------------------------------------------------------------------------


def test_overall_ready_when_all_entries_ready():
    """Overall status is ready iff all entries are ready (Property 16)."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()
    browser = _make_mock_gateway_browser()

    _install_index_get(
        index,
        {
            (_pk(), _wf_sk("wf-001")): _workflow_record(asset_id="wf-001"),
            (_pk(), _sk_sk("sk-001")): _skill_record(asset_id="sk-001"),
        },
    )

    steps = [_make_step("s1", skill_asset_id="sk-001")]
    skill_content = b"# Valid Skill\n\nThis skill is complete."
    s3.read.side_effect = [
        _make_workflow_json("WF", steps),
        skill_content,
    ]

    readiness = PromotionReadiness(s3=s3, index=index, gateway_browser=browser)
    result = _run(readiness.check(ctx, "wf-001"))

    assert result.overall_status == "ready"
    # All entries should be ready
    for entry in result.entries:
        assert entry.status == "ready"
        assert entry.failures == []


def test_overall_not_ready_when_any_entry_not_ready():
    """Overall status is not_ready when any entry is not_ready (Property 16)."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()
    browser = _make_mock_gateway_browser()

    _install_index_get(
        index,
        {
            (_pk(), _wf_sk("wf-001")): _workflow_record(asset_id="wf-001"),
            (_pk(), _sk_sk("sk-001")): _skill_record(asset_id="sk-001"),
        },
    )

    steps = [_make_step("s1", skill_asset_id="sk-001")]
    # SKILL.md is empty → skill not_ready
    s3.read.side_effect = [
        _make_workflow_json("WF", steps),
        b"   ",  # whitespace-only = empty
    ]

    readiness = PromotionReadiness(s3=s3, index=index, gateway_browser=browser)
    result = _run(readiness.check(ctx, "wf-001"))

    assert result.overall_status == "not_ready"


def test_not_ready_entries_have_nonempty_failures():
    """not_ready entries have non-empty failure lists (Property 17)."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()
    browser = _make_mock_gateway_browser()

    _install_index_get(
        index,
        {
            (_pk(), _wf_sk("wf-001")): _workflow_record(asset_id="wf-001"),
            (_pk(), _sk_sk("sk-001")): _skill_record(asset_id="sk-001"),
        },
    )

    steps = [_make_step("s1", skill_asset_id="sk-001")]
    s3.read.side_effect = [
        _make_workflow_json("WF", steps),
        b"",  # empty SKILL.md
    ]

    readiness = PromotionReadiness(s3=s3, index=index, gateway_browser=browser)
    result = _run(readiness.check(ctx, "wf-001"))

    for entry in result.entries:
        if entry.status == "not_ready":
            assert len(entry.failures) > 0, (
                f"not_ready entry {entry.asset_id} has empty failures"
            )
        if entry.status == "ready":
            assert entry.failures == [], (
                f"ready entry {entry.asset_id} has non-empty failures"
            )


# ---------------------------------------------------------------------------
# Read-only invariant (Property 18, Requirement 8.10)
# ---------------------------------------------------------------------------


def test_check_is_read_only():
    """check() does not write to S3, DDB, or memory (Property 18, Requirement 8.10)."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()
    browser = _make_mock_gateway_browser()

    _install_index_get(
        index,
        {
            (_pk(), _wf_sk("wf-001")): _workflow_record(asset_id="wf-001"),
            (_pk(), _sk_sk("sk-001")): _skill_record(asset_id="sk-001"),
        },
    )

    steps = [_make_step("s1", skill_asset_id="sk-001")]
    s3.read.side_effect = [
        _make_workflow_json("WF", steps),
        b"# Valid Skill\n\nContent.",
    ]

    readiness = PromotionReadiness(s3=s3, index=index, gateway_browser=browser)
    _run(readiness.check(ctx, "wf-001"))

    # Verify no writes occurred
    s3.write.assert_not_called()
    s3.delete.assert_not_called()
    s3.delete_prefix.assert_not_called()
    index.put.assert_not_called()
    index.update.assert_not_called()
    index.transact_write.assert_not_called()


# ---------------------------------------------------------------------------
# Transitive reference enumeration (Requirement 8.2)
# ---------------------------------------------------------------------------


def test_transitive_skill_enumeration_via_specialty():
    """Skills referenced transitively via specialty compatible-skills are checked."""
    ctx = _make_ctx()
    s3 = _make_mock_s3()
    index = _make_mock_index()
    browser = _make_mock_gateway_browser()

    _install_index_get(
        index,
        {
            (_pk(), _wf_sk("wf-001")): _workflow_record(asset_id="wf-001"),
            (_pk(), _sa_sk("sa-001")): _specialty_record(
                asset_id="sa-001",
                compatible_skills=["sk-transitive"],
            ),
            (_pk(), _sk_sk("sk-transitive")): _skill_record(
                asset_id="sk-transitive", name="Transitive Skill"
            ),
        },
    )

    # Workflow references specialty but NOT the skill directly
    steps = [_make_step("s1", agent_kind="specialty", agent_asset_id="sa-001")]
    skill_content = b"# Transitive Skill\n\nContent."
    s3.read.side_effect = [
        _make_workflow_json("WF", steps),
        skill_content,  # SKILL.md for sk-transitive
    ]

    readiness = PromotionReadiness(s3=s3, index=index, gateway_browser=browser)
    result = _run(readiness.check(ctx, "wf-001"))

    # The transitive skill should appear in the entries
    skill_entries = [e for e in result.entries if e.category == "skill"]
    assert len(skill_entries) == 1
    assert skill_entries[0].asset_id == "sk-transitive"
    assert skill_entries[0].status == "ready"
