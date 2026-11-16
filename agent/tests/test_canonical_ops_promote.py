"""Tests for the canonical Promote operation across all four CRUDs.

Covers Task 3.1's behavior:

* Happy-path Promote per CRUD (Skill, SpecialtyAgent, Workflow,
  ToolCollection) — for tool-collection the call is expected to raise
  ``NotImplementedError`` since the design defers tool-collection
  promote to a follow-on sprint.
* Edge cases:
    - draft missing ⇒ ``NotFoundOrForbidden``
    - draft owned by a different actor ⇒ ``NotFoundOrForbidden``
    - DDB ``TransactionCanceledException`` ⇒ ``ConflictError`` and the
      compensating ``delete_prefix_absolute`` runs
    - compensating delete itself fails ⇒ ``CompensationFailedError``
      carrying the orphan prefix
* Existing-lineage promote: the next version is ``max(existing) + 1``
  and the prior current row's ``current=true`` precondition appears in
  the transaction.
* The Put item carries the new published row's PK / SK / GSI1
  attributes; the second update flips the prior current row;
  the third update writes ``lastPublishedAs`` on the draft.

Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.5, 2.8, 2.9, 2.10
Properties: 3 (exactly one current per lineage), 4 (Promote round-trip)
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import ClientError

# Add agent/src to sys.path so imports resolve.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab.context import LabRequestContext  # noqa: E402
from lab.errors import (  # noqa: E402
    CompensationFailedError,
    ConflictError,
    NotFoundOrForbidden,
)
from lab.models import (  # noqa: E402
    Skill,
    SpecialtyAgent,
    ToolCollection,
    Workflow,
)
from lab.operation_results import Promoted  # noqa: E402
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
    """Build a LabRequestContext whose ``actor_id`` matches the draft owner."""
    return LabRequestContext(
        actor_id=actor_id,
        session_id="sess-test",
        request_id="req-test",
    )


def _make_skill_draft(
    *, user_id: str = "u-jane", draft_id: str = "d-42"
) -> Skill:
    now = int(time.time() * 1000)
    return Skill(
        asset_id=draft_id,
        category="skill",
        name="my-skill",
        status="draft",
        scope="private",
        s3_prefix=f"users/{user_id}/drafts/skills/{draft_id}/",
        created_at=now,
        updated_at=now,
    )


def _make_specialty_draft(
    *, user_id: str = "u-jane", draft_id: str = "d-99"
) -> SpecialtyAgent:
    now = int(time.time() * 1000)
    return SpecialtyAgent(
        asset_id=draft_id,
        category="specialty-agent",
        name="my-specialty",
        status="draft",
        scope="private",
        s3_prefix=f"users/{user_id}/drafts/specialty-agents/{draft_id}/",
        created_at=now,
        updated_at=now,
        compatible_skills=["sk-001"],
    )


def _make_workflow_draft(
    *, user_id: str = "u-jane", draft_id: str = "d-77"
) -> Workflow:
    now = int(time.time() * 1000)
    return Workflow(
        asset_id=draft_id,
        category="workflow",
        name="my-workflow",
        status="draft",
        scope="private",
        s3_prefix=f"users/{user_id}/drafts/workflows/{draft_id}/",
        created_at=now,
        updated_at=now,
        steps=[],
    )


def _make_index_with_draft(draft):
    """Build a MagicMock AssetIndex that returns ``draft`` from .get()."""
    index = MagicMock()
    index.table_name = "test-asset-index"
    index.get = AsyncMock(return_value=draft)
    index.query_lineage = AsyncMock(return_value=[])
    index.transact_write = AsyncMock()
    return index


def _make_s3_mock():
    """Build a MagicMock S3UserFilesTool that succeeds at copy and delete."""
    s3 = MagicMock()
    s3.copy_prefix_absolute = AsyncMock()
    s3.delete_prefix_absolute = AsyncMock()
    # Promote's empty-source guard (BUG-2) lists the draft prefix before the
    # copy and refuses to publish nothing. Default to a non-empty listing so
    # happy-path specs pass; the empty-source case is covered by a dedicated
    # test below.
    s3.list_absolute = AsyncMock(return_value=["<draft-prefix>/artifact"])
    # Workflow Promote pre-flight reads the draft's workflow.json from
    # the actor-scoped namespace to walk Asset_Refs (Task 3.6 /
    # Requirement 11). Default to "no workflow.json" so happy-path
    # specs that don't seed a steps payload (Skill / Specialty /
    # ToolCollection promote tests) still exercise the canonical-op
    # layer untouched. Workflow-specific tests below override `read`
    # to return the steps payload they care about.
    s3.read = AsyncMock(side_effect=FileNotFoundError())
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
            {"Code": "None"},
        ],
    }
    return ClientError(response, "TransactWriteItems")


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


def test_skill_promote_new_lineage_to_org_happy_path():
    """Promoting a new Skill draft to org scope produces v1 published.

    Verifies:
      * Returned ``Promoted`` carries a fresh ``published_id``,
        ``version=1``, and the canonical org-prefix.
      * The draft was looked up under the user-drafts PK.
      * S3 ``copy_prefix_absolute`` was called with (draft_prefix → target).
      * ``transact_write`` received exactly three operations: Put new
        published row + Update draft.lastPublishedAs (no prior current
        flip because this is a new lineage) + the Registry projection
        outbox row, which rides the SAME transaction so the shared
        discovery catalog cannot diverge from canonical state (R7).
      * The Put item carries PK/SK/GSI1 keys, ``status="published"``,
        ``current=True``, ``promotedFrom`` equal to the draft id, and a
        single initial ``scopeHistory`` entry.
    """
    ctx = _make_ctx(actor_id="u-jane")
    draft = _make_skill_draft(user_id="u-jane", draft_id="d-42")
    index = _make_index_with_draft(draft)
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    result = _run(
        crud.promote(
            ctx=ctx,
            draft_id="d-42",
            scope="org",
            org_id="o-acme",
        )
    )

    assert isinstance(result, Promoted)
    assert result.version == 1
    assert result.published_id  # uuid4
    assert result.s3_prefix == (
        f"org/o-acme/published/skills/{result.published_id}/v1/"
    )

    # Draft lookup
    index.get.assert_awaited_once_with(
        "USER#u-jane#OWNED", "skill#d-42"
    )

    # S3 copy
    s3.copy_prefix_absolute.assert_awaited_once_with(
        draft.s3_prefix, result.s3_prefix
    )

    # Transaction shape: Put + Update draft + outbox Put (no prior current row)
    index.transact_write.assert_awaited_once()
    operations = index.transact_write.await_args.args[0]
    assert len(operations) == 3, (
        "expected Put new published + Update draft + Registry outbox; got: "
        f"{operations}"
    )
    put_op = operations[0]["Put"]
    assert put_op["TableName"] == "test-asset-index"
    assert put_op["ConditionExpression"] == "attribute_not_exists(PK)"
    new_item = put_op["Item"]
    assert new_item["PK"] == "ORG#o-acme#SHARED"
    assert new_item["SK"] == f"skill#{result.published_id}#v0000000001"
    assert new_item["GSI1PK"] == f"PUB#{result.published_id}"
    assert new_item["GSI1SK"] == "v0000000001"
    assert "status" not in new_item and new_item["deprecated"] is False
    assert new_item["current"] is True
    assert new_item["scope"] == "org"
    assert "recordType" not in new_item and new_item["version"] == 1
    assert new_item["promotedFrom"] == "d-42"
    assert new_item["publishPolicy"] == {
        "ownerIds": ["u-jane"],
        "contributorIds": [],
    }
    assert new_item["ownerOrgId"] == "o-acme"
    assert "ownerUserId" not in new_item
    assert "ownerGroupId" not in new_item

    # ScopeHistory: exactly one entry, from=None to=org
    history = new_item["scopeHistory"]
    assert len(history) == 1
    assert history[0]["from"] is None
    assert history[0]["to"] == "org"
    assert history[0]["actorId"] == "u-jane"
    # group_ids must be omitted when scope!="group"
    assert "groupIds" not in history[0]

    # Update draft.lastPublishedAs
    draft_update = operations[1]["Update"]
    assert draft_update["Key"] == {
        "PK": "USER#u-jane#OWNED",
        "SK": "skill#d-42",
    }
    assert "lastPublishedAs" in draft_update["UpdateExpression"]
    assert draft_update["ExpressionAttributeValues"][":ref"] == {
        "publishedId": result.published_id,
        "version": 1,
    }

    # Registry projection outbox (R7). Same transaction as the published row,
    # identifiers only — no descriptor text, package bytes, or credentials.
    outbox_item = operations[2]["Put"]["Item"]
    assert outbox_item["PK"] == f"OUTBOX#skill#{result.published_id}"
    assert outbox_item["SK"].startswith("EVT#")
    assert outbox_item["recordType"] == "outbox"
    assert outbox_item["eventType"] == "CapabilityPublished"
    assert outbox_item["category"] == "skill"
    assert outbox_item["assetId"] == result.published_id
    assert outbox_item["version"] == 1
    assert outbox_item["scope"] == "org"
    assert outbox_item["idempotencyKey"].endswith(
        f"/skill/{result.published_id}/v1/CapabilityPublished/"
        f"{new_item['SK']}"
    )


def test_promote_to_private_scope_emits_no_outbox_event():
    """A private publish stays out of the shared Registry entirely (R3).

    The outbox row is what makes an asset discoverable, so its absence — not
    a downstream filter — is what keeps a private version from ever reaching
    the harness-shared catalog (Property 2).
    """
    ctx = _make_ctx(actor_id="u-jane")
    draft = _make_skill_draft(user_id="u-jane", draft_id="d-42")
    index = _make_index_with_draft(draft)
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    _run(crud.promote(ctx=ctx, draft_id="d-42", scope="private"))

    operations = index.transact_write.await_args.args[0]
    assert not [
        op
        for op in operations
        if op.get("Put", {}).get("Item", {}).get("recordType") == "outbox"
    ], f"private promote must not emit an outbox event; got {operations}"


def test_specialty_promote_to_group_includes_group_ids_in_history():
    """Promoting a Specialty draft to group scope records groupIds.

    Verifies that ``scopeHistory`` for a ``scope="group"`` initial
    promote includes the ``groupIds`` list (Requirement 13.2).
    """
    ctx = _make_ctx(actor_id="u-jane")
    draft = _make_specialty_draft(user_id="u-jane", draft_id="d-99")
    index = _make_index_with_draft(draft)
    s3 = _make_s3_mock()

    crud = SpecialtyCRUD(s3=s3, index=index)
    result = _run(
        crud.promote(
            ctx=ctx,
            draft_id="d-99",
            scope="group",
            group_ids=["g-marketing"],
        )
    )

    assert result.s3_prefix == (
        f"groups/g-marketing/published/specialty-agents/"
        f"{result.published_id}/v1/"
    )

    operations = index.transact_write.await_args.args[0]
    new_item = operations[0]["Put"]["Item"]
    assert new_item["PK"] == "GROUP#g-marketing#SHARED"
    assert new_item["ownerGroupId"] == "g-marketing"
    history = new_item["scopeHistory"]
    assert history[0]["to"] == "group"
    assert history[0]["groupIds"] == ["g-marketing"]
    # Specialty-specific field is preserved on the published row.
    assert new_item["compatibleSkills"] == ["sk-001"]


def test_workflow_promote_to_private_uses_owner_user_id():
    """Promoting a Workflow draft to private scope owns it under the user."""
    ctx = _make_ctx(actor_id="u-jane")
    draft = _make_workflow_draft(user_id="u-jane", draft_id="d-77")
    index = _make_index_with_draft(draft)
    s3 = _make_s3_mock()

    crud = WorkflowCRUD(s3=s3, index=index)
    result = _run(
        crud.promote(ctx=ctx, draft_id="d-77", scope="private")
    )

    assert result.s3_prefix == (
        f"users/u-jane/published/workflows/{result.published_id}/v1/"
    )
    operations = index.transact_write.await_args.args[0]
    new_item = operations[0]["Put"]["Item"]
    assert new_item["PK"] == "USER#u-jane#VERSIONS"
    assert new_item["ownerUserId"] == "u-jane"


def test_tool_collection_promote_raises_not_implemented():
    """Tool collections have no versioned Promote in this sprint."""
    ctx = _make_ctx(actor_id="u-jane")
    s3 = _make_s3_mock()
    index = MagicMock()
    gateway = MagicMock()

    crud = ToolCollectionCRUD(s3=s3, index=index, gateway_browser=gateway)
    with pytest.raises(NotImplementedError):
        _run(
            crud.promote(
                ctx=ctx,
                draft_id="d-tc",
                scope="org",
                org_id="o-acme",
            )
        )


# ---------------------------------------------------------------------------
# Existing-lineage promote: max + 1, prior current flipped
# ---------------------------------------------------------------------------


def test_promote_existing_lineage_picks_next_version_and_flips_current():
    """Promoting into an existing lineage emits a 3-op transaction.

    Verifies Property 3 / Requirement 2.9:
      * version = max(existing) + 1
      * the prior current row is updated to current=false with a
        ``current=true`` precondition
      * the new row is inserted with current=true
    """
    ctx = _make_ctx(actor_id="u-jane")
    draft = _make_skill_draft(user_id="u-jane", draft_id="d-42")

    # Existing lineage: v1 (deprecated) and v2 (current=true at private scope).
    pid = "p-existing"
    now = int(time.time() * 1000)
    existing_v1 = Skill(
        asset_id=pid,
        category="skill",
        name="lineage",
        status="deprecated",
        scope="private",
        s3_prefix=f"users/u-jane/published/skills/{pid}/v1/",
        created_at=now,
        updated_at=now,
        version=1,
        current=False,
    )
    existing_v2 = Skill(
        asset_id=pid,
        category="skill",
        name="lineage",
        status="published",
        scope="private",
        s3_prefix=f"users/u-jane/published/skills/{pid}/v2/",
        created_at=now,
        updated_at=now,
        version=2,
        current=True,
    )
    index = _make_index_with_draft(draft)
    index.query_lineage = AsyncMock(return_value=[existing_v1, existing_v2])

    s3 = _make_s3_mock()
    crud = SkillCRUD(s3=s3, index=index)
    result = _run(
        crud.promote(
            ctx=ctx,
            draft_id="d-42",
            scope="private",
            published_id=pid,
        )
    )

    assert result.published_id == pid
    assert result.version == 3
    operations = index.transact_write.await_args.args[0]
    assert len(operations) == 3

    # 0: Put new v3 published
    put_item = operations[0]["Put"]["Item"]
    assert put_item["SK"] == f"skill#{pid}#v0000000003"
    assert put_item["current"] is True

    # 1: Update prior current (v2) → current=false with current=true precondition
    flip = operations[1]["Update"]
    assert flip["Key"] == {
        "PK": "USER#u-jane#VERSIONS",
        "SK": f"skill#{pid}#v0000000002",
    }
    assert flip["UpdateExpression"] == "SET #current = :false"
    assert flip["ConditionExpression"] == "#current = :true"

    # 2: Update draft.lastPublishedAs
    draft_up = operations[2]["Update"]
    assert draft_up["Key"]["SK"] == "skill#d-42"
    assert draft_up["ExpressionAttributeValues"][":ref"]["version"] == 3


# ---------------------------------------------------------------------------
# Authz / not-found edge cases
# ---------------------------------------------------------------------------


def test_promote_missing_draft_raises_not_found_or_forbidden():
    """Draft not present at the user-drafts PK ⇒ NotFoundOrForbidden."""
    ctx = _make_ctx(actor_id="u-jane")
    index = MagicMock()
    index.table_name = "test-asset-index"
    index.get = AsyncMock(return_value=None)
    index.query_lineage = AsyncMock(return_value=[])
    index.transact_write = AsyncMock()
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    with pytest.raises(NotFoundOrForbidden):
        _run(
            crud.promote(
                ctx=ctx,
                draft_id="d-nope",
                scope="org",
                org_id="o-acme",
            )
        )

    # No S3 copy, no transaction issued
    s3.copy_prefix_absolute.assert_not_awaited()
    index.transact_write.assert_not_awaited()


def test_promote_empty_source_prefix_raises_and_skips_copy():
    """Publishing a draft whose artifact prefix has ZERO objects fails loudly.

    Regression for BUG-2: a path mismatch left the draft prefix empty, so
    ``copy_prefix_absolute`` copied nothing and minted a published version
    pointing at an empty prefix (persona/SKILL.md silently absent at invoke
    time). The empty-source guard must raise BEFORE the copy/transaction so
    the failure surfaces at publish time.
    """
    ctx = _make_ctx(actor_id="u-jane")
    draft = _make_skill_draft(user_id="u-jane", draft_id="d-empty")
    index = _make_index_with_draft(draft)
    s3 = _make_s3_mock()
    s3.list_absolute = AsyncMock(return_value=[])  # empty draft prefix

    crud = SkillCRUD(s3=s3, index=index)
    with pytest.raises(ValueError, match="no objects"):
        _run(crud.promote(ctx=ctx, draft_id="d-empty", scope="org", org_id="o-acme"))

    # Guard fired before any copy or transaction.
    s3.copy_prefix_absolute.assert_not_awaited()
    index.transact_write.assert_not_awaited()


def test_promote_draft_owned_by_other_actor_raises_not_found_or_forbidden():
    """Draft lookup at the *requesting* actor's PK returns nothing when
    a different user owns the draft, so the Promote surfaces
    ``NotFoundOrForbidden`` (no existence disclosure — Requirement 4.5).
    """
    ctx = _make_ctx(actor_id="u-bob")
    # Draft is owned by u-jane, so u-bob's draft PK lookup returns None.
    index = MagicMock()
    index.table_name = "test-asset-index"
    index.get = AsyncMock(return_value=None)
    index.query_lineage = AsyncMock(return_value=[])
    index.transact_write = AsyncMock()
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    with pytest.raises(NotFoundOrForbidden):
        _run(
            crud.promote(
                ctx=ctx,
                draft_id="d-42",
                scope="org",
                org_id="o-acme",
            )
        )
    # Verify the lookup was scoped to u-bob's drafts partition,
    # confirming we never read u-jane's draft.
    index.get.assert_awaited_once_with(
        "USER#u-bob#OWNED", "skill#d-42"
    )


# ---------------------------------------------------------------------------
# Compensating action edge cases
# ---------------------------------------------------------------------------


def test_promote_transaction_canceled_runs_compensating_delete():
    """When the DDB transaction cancels, the target prefix is cleaned up
    and ``ConflictError`` surfaces. The S3 copy was already issued, so
    without compensation an orphan prefix would remain.
    """
    ctx = _make_ctx(actor_id="u-jane")
    draft = _make_skill_draft(user_id="u-jane", draft_id="d-42")
    index = _make_index_with_draft(draft)
    index.transact_write = AsyncMock(side_effect=_txn_canceled_error())
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    with pytest.raises(ConflictError) as exc_info:
        _run(
            crud.promote(
                ctx=ctx,
                draft_id="d-42",
                scope="org",
                org_id="o-acme",
            )
        )

    # ConflictError carries the cancellation reasons from boto3.
    assert exc_info.value.cancellation_reasons
    s3.copy_prefix_absolute.assert_awaited_once()
    s3.delete_prefix_absolute.assert_awaited_once()
    # The compensating delete used the same target prefix the copy wrote.
    copied_target = s3.copy_prefix_absolute.await_args.args[1]
    assert s3.delete_prefix_absolute.await_args.args[0] == copied_target


def test_promote_compensation_failure_raises_compensation_failed_error():
    """When the transaction cancels AND the compensating delete itself
    fails, the helper raises :class:`CompensationFailedError` carrying
    the orphan prefix and both root errors so a janitor / operator can
    reconcile.
    """
    ctx = _make_ctx(actor_id="u-jane")
    draft = _make_skill_draft(user_id="u-jane", draft_id="d-42")
    index = _make_index_with_draft(draft)
    index.transact_write = AsyncMock(side_effect=_txn_canceled_error())

    s3 = _make_s3_mock()
    s3.delete_prefix_absolute = AsyncMock(
        side_effect=RuntimeError("S3 unreachable")
    )

    crud = SkillCRUD(s3=s3, index=index)
    with pytest.raises(CompensationFailedError) as exc_info:
        _run(
            crud.promote(
                ctx=ctx,
                draft_id="d-42",
                scope="org",
                org_id="o-acme",
            )
        )

    err = exc_info.value
    assert err.orphan_prefix.startswith("org/o-acme/published/skills/")
    assert err.orphan_prefix.endswith("/v1/")
    assert isinstance(err.compensation_error, RuntimeError)
    # The original error should be the boto3 cancellation, not the
    # compensation error.
    assert isinstance(err.original_error, ClientError)


def test_promote_group_scope_requires_group_ids():
    """``scope='group'`` without ``group_ids`` is a validation error
    surfaced before any S3 or AssetIndex mutation.
    """
    ctx = _make_ctx(actor_id="u-jane")
    draft = _make_skill_draft(user_id="u-jane", draft_id="d-42")
    index = _make_index_with_draft(draft)
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    with pytest.raises(ValueError):
        _run(
            crud.promote(
                ctx=ctx,
                draft_id="d-42",
                scope="group",
                group_ids=None,
            )
        )

    s3.copy_prefix_absolute.assert_not_awaited()
    index.transact_write.assert_not_awaited()


def test_promote_org_scope_requires_org_id():
    """``scope='org'`` without ``org_id`` is a validation error."""
    ctx = _make_ctx(actor_id="u-jane")
    draft = _make_skill_draft(user_id="u-jane", draft_id="d-42")
    index = _make_index_with_draft(draft)
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    with pytest.raises(ValueError):
        _run(
            crud.promote(
                ctx=ctx,
                draft_id="d-42",
                scope="org",
                org_id=None,
            )
        )
    s3.copy_prefix_absolute.assert_not_awaited()
    index.transact_write.assert_not_awaited()
