"""Tests for the canonical Deprecate operation across all four CRUDs.

Covers Task 3.3's behavior:

* Happy paths:
    - Deprecate a non-current version: only the target row is updated;
      current pointer is unchanged.
    - Deprecate the current version with another non-deprecated version
      available: target → deprecated+current=false, that other →
      current=true; both Updates appear in the same transaction.
    - Deprecate the only published version: target →
      deprecated+current=false; no second update (lineage has no
      current).
    - Deprecate the current version when other versions are also
      deprecated: target → deprecated+current=false; no second update
      (no non-deprecated remains).
    - Deprecate selects highest version when multiple non-deprecated
      remain: e.g. v1, v2 (current), v3 (non-deprecated, not-current);
      deprecating v2 promotes v3.
* Authz: actor not in publish_policy.{owner_ids ∪ contributor_ids} ⇒
  AuthorityDenied (Property 8). No transaction issued.
* Authz: contributor (not owner) is allowed.
* Lineage empty ⇒ NotFoundOrForbidden.
* Version missing from lineage ⇒ NotFoundOrForbidden.
* TransactionCanceledException ⇒ ConflictError. (No S3 mutation, so no
  compensating action; verify S3 was never touched.)
* ToolCollection.deprecate raises NotImplementedError.

Validates: Requirements 6.1, 6.2, 6.3, 6.4, 6.5
Properties: 3 (Exactly one current per lineage), 8 (Authority gate
            independent of visibility)
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import ClientError

# Add agent/src to sys.path so imports resolve.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab.context import LabRequestContext  # noqa: E402
from lab.errors import (  # noqa: E402
    AuthorityDenied,
    ConflictError,
    NotFoundOrForbidden,
)
from lab.gateway_browser import GatewayBrowser  # noqa: E402
from lab.models import (  # noqa: E402
    PublishPolicy,
    Scope,
    ScopeHistoryEntry,
    Skill,
    SpecialtyAgent,
    Workflow,
)
from lab.operation_results import Deprecated  # noqa: E402
from lab.skill_crud import SkillCRUD  # noqa: E402
from lab.specialty_crud import SpecialtyCRUD  # noqa: E402
from lab.tool_collection_crud import ToolCollectionCRUD  # noqa: E402
from lab.workflow_crud import WorkflowCRUD  # noqa: E402


def _run(coro):
    """Run an async coroutine to completion in a sync test."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_ctx(actor_id: str = "u-jane") -> LabRequestContext:
    """Build a LabRequestContext with the given actor."""
    return LabRequestContext(
        actor_id=actor_id,
        session_id="sess-test",
        request_id="req-test",
    )


def _initial_history(
    *,
    to_scope: Scope = "private",
    actor_id: str = "u-jane",
    group_ids: Optional[list[str]] = None,
) -> list[ScopeHistoryEntry]:
    """Render the single-entry history a Promote would have produced."""
    return [
        ScopeHistoryEntry(
            from_scope=None,
            to_scope=to_scope,
            actor_id=actor_id,
            timestamp="2024-01-01T00:00:00.000000Z",
            group_ids=list(group_ids) if to_scope == "group" and group_ids else None,
        )
    ]


def _make_skill(
    *,
    scope: Scope = "private",
    user_id: str = "u-jane",
    actor_id: str = "u-jane",
    contributor_ids: Optional[list[str]] = None,
    published_id: str = "p-skill-1",
    version: int = 1,
    status: str = "published",
    current: bool = True,
) -> Skill:
    """Build a Skill row (published or deprecated) at a private scope.

    The S3 prefix is rendered to match the canonical grammar so the
    helper's prefix-derivation can recover the owner from it.
    """
    now = int(time.time() * 1000)
    if scope == "private":
        prefix = f"users/{user_id}/published/skills/{published_id}/v{version}/"
    elif scope == "group":
        prefix = f"groups/g-marketing/published/skills/{published_id}/v{version}/"
    else:  # org
        prefix = f"org/o-acme/published/skills/{published_id}/v{version}/"

    return Skill(
        asset_id=published_id,
        category="skill",
        name="my-skill",
        status=status,
        scope=scope,
        s3_prefix=prefix,
        created_at=now,
        updated_at=now,
        version=version,
        publish_policy=PublishPolicy(
            owner_ids=[actor_id],
            contributor_ids=list(contributor_ids) if contributor_ids else [],
        ),
        scope_history=_initial_history(to_scope=scope, actor_id=actor_id),
        current=current,
        promoted_from="d-original",
    )


def _make_specialty(
    *,
    actor_id: str = "u-jane",
    user_id: str = "u-jane",
    published_id: str = "p-spec-1",
    version: int = 1,
    status: str = "published",
    current: bool = True,
) -> SpecialtyAgent:
    now = int(time.time() * 1000)
    prefix = f"users/{user_id}/published/specialty-agents/{published_id}/v{version}/"
    return SpecialtyAgent(
        asset_id=published_id,
        category="specialty-agent",
        name="my-specialty",
        status=status,
        scope="private",
        s3_prefix=prefix,
        created_at=now,
        updated_at=now,
        version=version,
        publish_policy=PublishPolicy(owner_ids=[actor_id]),
        scope_history=_initial_history(actor_id=actor_id),
        current=current,
        promoted_from="d-spec",
        compatible_skills=["sk-001"],
    )


def _make_workflow(
    *,
    actor_id: str = "u-jane",
    user_id: str = "u-jane",
    published_id: str = "p-wf-1",
    version: int = 1,
    status: str = "published",
    current: bool = True,
) -> Workflow:
    now = int(time.time() * 1000)
    prefix = f"users/{user_id}/published/workflows/{published_id}/v{version}/"
    return Workflow(
        asset_id=published_id,
        category="workflow",
        name="my-workflow",
        status=status,
        scope="private",
        s3_prefix=prefix,
        created_at=now,
        updated_at=now,
        version=version,
        publish_policy=PublishPolicy(owner_ids=[actor_id]),
        scope_history=_initial_history(actor_id=actor_id),
        current=current,
        promoted_from="d-wf",
        steps=[],
    )


def _make_index(lineage: list) -> MagicMock:
    """Build a MagicMock AssetIndex whose query_lineage returns ``lineage``."""
    index = MagicMock()
    index.table_name = "test-asset-index"
    index.query_lineage = AsyncMock(return_value=lineage)
    index.transact_write = AsyncMock()
    return index


def _make_s3_mock() -> MagicMock:
    """Build a MagicMock S3UserFilesTool. Deprecate must NEVER call it."""
    s3 = MagicMock()
    s3.copy_prefix_absolute = AsyncMock()
    s3.delete_prefix_absolute = AsyncMock()
    return s3


def _txn_canceled_error() -> ClientError:
    """Build a botocore ClientError that mimics a TransactWriteItems cancel."""
    response = {
        "Error": {
            "Code": "TransactionCanceledException",
            "Message": "Transaction cancelled, please refer to "
            "cancellation reasons for specific reasons",
        },
        "CancellationReasons": [
            {"Code": "ConditionalCheckFailed"},
            {"Code": "None"},
        ],
    }
    return ClientError(response, "TransactWriteItems")


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_deprecate_non_current_version_only_updates_target():
    """Deprecate a non-current version → single Update on target only.

    Lineage: v1 (deprecated, not current), v2 (published, current).
    Deprecate v1 should NOT touch the current pointer; only the target
    row is updated. ``new_current_version`` echoes v2 (unchanged).
    """
    ctx = _make_ctx(actor_id="u-jane")
    v1 = _make_skill(version=1, status="published", current=False)
    v2 = _make_skill(version=2, status="published", current=True)
    index = _make_index([v1, v2])
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    result = _run(
        crud.deprecate(ctx=ctx, published_id="p-skill-1", version=1)
    )

    assert isinstance(result, Deprecated)
    assert result.published_id == "p-skill-1"
    assert result.version == 1
    # Existing current pointer (v2) is unchanged.
    assert result.new_current_version == 2

    # Single Update operation against target.
    index.transact_write.assert_awaited_once()
    operations = index.transact_write.await_args.args[0]
    assert len(operations) == 1
    update_op = operations[0]["Update"]
    assert update_op["Key"] == {
        "PK": "USER#u-jane#VERSIONS",
        "SK": "skill#p-skill-1#v0000000001",
    }
    # SET status=deprecated and current=false on target.
    assert update_op["ExpressionAttributeValues"][":true"] is True
    assert "deprecated = :true" in update_op["UpdateExpression"]
    assert "REMOVE #status" in update_op["UpdateExpression"]
    assert update_op["ExpressionAttributeValues"][":false"] is False
    assert "deprecated" in update_op["UpdateExpression"]
    assert "current" in update_op["UpdateExpression"]
    assert "updatedAt" in update_op["UpdateExpression"]

    # No S3 mutations whatsoever (Requirement 6.4).
    s3.copy_prefix_absolute.assert_not_awaited()
    s3.delete_prefix_absolute.assert_not_awaited()


def test_deprecate_current_with_other_non_deprecated_promotes_other():
    """Deprecate the current version → target deprecated, other promoted.

    Lineage: v1 (published, current), v2 (published, not current).
    Deprecate v1 should:
      * mark v1 status=deprecated, current=false
      * mark v2 current=true with precondition current=:false
    Both Updates land in the same transaction.
    ``new_current_version`` reports v2.
    """
    ctx = _make_ctx(actor_id="u-jane")
    v1 = _make_skill(version=1, status="published", current=True)
    v2 = _make_skill(version=2, status="published", current=False)
    index = _make_index([v1, v2])
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    result = _run(
        crud.deprecate(ctx=ctx, published_id="p-skill-1", version=1)
    )

    assert result.new_current_version == 2

    # Two updates in one transaction.
    operations = index.transact_write.await_args.args[0]
    assert len(operations) == 2

    target_update = operations[0]["Update"]
    promote_update = operations[1]["Update"]

    # Target row (v1) → deprecated, current=false.
    assert target_update["Key"]["SK"] == "skill#p-skill-1#v0000000001"
    assert target_update["ExpressionAttributeValues"][":true"] is True
    assert "deprecated = :true" in target_update["UpdateExpression"]
    assert target_update["ExpressionAttributeValues"][":false"] is False

    # Successor row (v2) → current=true with precondition current=:false.
    assert promote_update["Key"]["SK"] == "skill#p-skill-1#v0000000002"
    assert promote_update["ConditionExpression"] == "#current = :false"
    assert promote_update["ExpressionAttributeValues"][":true"] is True
    assert promote_update["ExpressionAttributeValues"][":false"] is False
    assert "current" in promote_update["UpdateExpression"]

    s3.copy_prefix_absolute.assert_not_awaited()
    s3.delete_prefix_absolute.assert_not_awaited()


def test_deprecate_only_version_no_successor():
    """Deprecate the only version → single Update; lineage has no current.

    Lineage: v1 (published, current). Deprecate v1 → target marked
    deprecated+current=false; no second update because no other
    non-deprecated version exists. ``new_current_version`` is None
    (Requirement 6.3).
    """
    ctx = _make_ctx(actor_id="u-jane")
    v1 = _make_skill(version=1, status="published", current=True)
    index = _make_index([v1])
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    result = _run(
        crud.deprecate(ctx=ctx, published_id="p-skill-1", version=1)
    )

    assert result.new_current_version is None

    operations = index.transact_write.await_args.args[0]
    assert len(operations) == 1
    update_op = operations[0]["Update"]
    assert update_op["Key"]["SK"] == "skill#p-skill-1#v0000000001"


def test_deprecate_current_when_others_already_deprecated_no_successor():
    """Deprecate current → no second update if no non-deprecated remain.

    Lineage: v1 (deprecated, not current), v2 (published, current).
    Deprecate v2 → target deprecated+current=false; v1 cannot be
    promoted because it is already deprecated. No second update.
    ``new_current_version`` is None.
    """
    ctx = _make_ctx(actor_id="u-jane")
    v1 = _make_skill(version=1, status="deprecated", current=False)
    v2 = _make_skill(version=2, status="published", current=True)
    index = _make_index([v1, v2])
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    result = _run(
        crud.deprecate(ctx=ctx, published_id="p-skill-1", version=2)
    )

    assert result.new_current_version is None

    operations = index.transact_write.await_args.args[0]
    assert len(operations) == 1
    update_op = operations[0]["Update"]
    assert update_op["Key"]["SK"] == "skill#p-skill-1#v0000000002"


def test_deprecate_current_selects_highest_non_deprecated_version():
    """When multiple non-deprecated remain, the highest version is promoted.

    Lineage: v1 (published, not current), v2 (published, current),
    v3 (published, not current). Deprecate v2 → target deprecated;
    v3 (the highest non-deprecated, non-target version) becomes
    current. v1 is left untouched. (Requirement 6.2.)
    """
    ctx = _make_ctx(actor_id="u-jane")
    v1 = _make_skill(version=1, status="published", current=False)
    v2 = _make_skill(version=2, status="published", current=True)
    v3 = _make_skill(version=3, status="published", current=False)
    index = _make_index([v1, v2, v3])
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    result = _run(
        crud.deprecate(ctx=ctx, published_id="p-skill-1", version=2)
    )

    assert result.new_current_version == 3

    operations = index.transact_write.await_args.args[0]
    assert len(operations) == 2
    target_update = operations[0]["Update"]
    promote_update = operations[1]["Update"]

    assert target_update["Key"]["SK"] == "skill#p-skill-1#v0000000002"
    # Highest non-deprecated, non-target version is v3.
    assert promote_update["Key"]["SK"] == "skill#p-skill-1#v0000000003"


def test_deprecate_specialty_happy_path():
    """Deprecate works against SpecialtyAgent rows (category routing)."""
    ctx = _make_ctx(actor_id="u-jane")
    v1 = _make_specialty(version=1, status="published", current=True)
    index = _make_index([v1])
    s3 = _make_s3_mock()

    crud = SpecialtyCRUD(s3=s3, index=index)
    result = _run(
        crud.deprecate(ctx=ctx, published_id="p-spec-1", version=1)
    )

    assert result.new_current_version is None
    operations = index.transact_write.await_args.args[0]
    assert len(operations) == 1
    assert (
        operations[0]["Update"]["Key"]["SK"] == "specialty-agent#p-spec-1#v0000000001"
    )


def test_deprecate_workflow_happy_path():
    """Deprecate works against Workflow rows (category routing)."""
    ctx = _make_ctx(actor_id="u-jane")
    v1 = _make_workflow(version=1, status="published", current=True)
    v2 = _make_workflow(version=2, status="published", current=False)
    index = _make_index([v1, v2])
    s3 = _make_s3_mock()

    crud = WorkflowCRUD(s3=s3, index=index)
    result = _run(
        crud.deprecate(ctx=ctx, published_id="p-wf-1", version=1)
    )

    assert result.new_current_version == 2
    operations = index.transact_write.await_args.args[0]
    assert len(operations) == 2
    assert operations[0]["Update"]["Key"]["SK"] == "workflow#p-wf-1#v0000000001"
    assert operations[1]["Update"]["Key"]["SK"] == "workflow#p-wf-1#v0000000002"


# ---------------------------------------------------------------------------
# Authz / not-found
# ---------------------------------------------------------------------------


def test_deprecate_actor_not_in_publish_policy_raises_authority_denied():
    """An actor not in publish_policy.{owner_ids ∪ contributor_ids} is denied.

    Property 8: authority is independent of visibility. No transaction
    issued.
    """
    ctx = _make_ctx(actor_id="u-bob")
    # publish_policy lists u-jane only; u-bob is not authorized.
    v1 = _make_skill(actor_id="u-jane", version=1, current=True)
    index = _make_index([v1])
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    with pytest.raises(AuthorityDenied):
        _run(
            crud.deprecate(
                ctx=ctx, published_id="p-skill-1", version=1
            )
        )

    index.transact_write.assert_not_awaited()
    s3.copy_prefix_absolute.assert_not_awaited()
    s3.delete_prefix_absolute.assert_not_awaited()


def test_deprecate_contributor_is_authorized():
    """A contributor (not owner) may Deprecate — covers Requirement 6.5."""
    ctx = _make_ctx(actor_id="u-bob")
    v1 = _make_skill(
        actor_id="u-jane",
        contributor_ids=["u-bob"],
        version=1,
        current=True,
    )
    index = _make_index([v1])
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    result = _run(
        crud.deprecate(ctx=ctx, published_id="p-skill-1", version=1)
    )
    assert isinstance(result, Deprecated)
    index.transact_write.assert_awaited_once()


def test_deprecate_lineage_empty_raises_not_found_or_forbidden():
    """When ``query_lineage`` returns nothing, surface NotFoundOrForbidden."""
    ctx = _make_ctx(actor_id="u-jane")
    index = _make_index([])
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    with pytest.raises(NotFoundOrForbidden):
        _run(
            crud.deprecate(
                ctx=ctx, published_id="p-nope", version=1
            )
        )

    index.transact_write.assert_not_awaited()


def test_deprecate_version_missing_raises_not_found_or_forbidden():
    """Lineage exists but the requested version is missing ⇒ not-found."""
    ctx = _make_ctx(actor_id="u-jane")
    v1 = _make_skill(version=1, current=True)
    index = _make_index([v1])
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    with pytest.raises(NotFoundOrForbidden):
        _run(
            crud.deprecate(
                ctx=ctx, published_id="p-skill-1", version=2
            )
        )

    index.transact_write.assert_not_awaited()


# ---------------------------------------------------------------------------
# Conflict on transaction cancel
# ---------------------------------------------------------------------------


def test_deprecate_transaction_canceled_raises_conflict_no_s3_calls():
    """When the DDB transaction cancels, surface ConflictError.

    Deprecate has no S3 mutations, so there is no compensating action;
    the operation simply re-raises ConflictError. Verify S3 is never
    touched.
    """
    ctx = _make_ctx(actor_id="u-jane")
    v1 = _make_skill(version=1, current=True)
    v2 = _make_skill(version=2, current=False)
    index = _make_index([v1, v2])
    index.transact_write = AsyncMock(side_effect=_txn_canceled_error())
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    with pytest.raises(ConflictError) as exc_info:
        _run(
            crud.deprecate(
                ctx=ctx, published_id="p-skill-1", version=1
            )
        )

    assert exc_info.value.cancellation_reasons
    s3.copy_prefix_absolute.assert_not_awaited()
    s3.delete_prefix_absolute.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tool collections — out of scope in this sprint
# ---------------------------------------------------------------------------


def test_tool_collection_deprecate_raises_not_implemented():
    """Tool collections have no per-version Deprecate in this sprint."""
    ctx = _make_ctx(actor_id="u-jane")
    s3 = _make_s3_mock()
    index = MagicMock()
    gateway = MagicMock(spec=GatewayBrowser)

    crud = ToolCollectionCRUD(s3=s3, index=index, gateway_browser=gateway)
    with pytest.raises(NotImplementedError):
        _run(
            crud.deprecate(
                ctx=ctx, published_id="p-tc", version=1
            )
        )
