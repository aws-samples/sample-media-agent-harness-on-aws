"""Tests for ToolCollectionCRUD — validates CRUD operations and Gateway validation.

Tests cover:
- Module imports and interface
- Create with valid tool refs
- Create with unresolved tool refs (rejection with error identifying each)
- Read returns correct data
- Update with re-validation
- Delete hard-delete via transact_write + S3 prefix cleanup
- List returns active drafts
- Cross-actor access returns None

The CRUD uses the new single-table AssetIndex API
(``put`` / ``get`` / ``update`` / ``query_pk`` / ``transact_write``)
under the ``USER#<u>#TOOL_COLLECTIONS`` flat partition.
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

from lab.asset_index import (
    make_pk_user_owned,
    make_sk_owned,
)
from lab.tool_collection_crud import (
    ToolCollectionCRUD,
    ToolCollectionCreateResult,
    ToolCollectionReadResult,
    _CATEGORY,
    _COLLECTION_FILENAME,
    _DRAFTS_PREFIX,
)
from lab.models import (
    GatewayProjection,
    GatewayToolDescriptor,
    ToolCollection,
    ToolRef,
)
from lab.context import LabRequestContext
from lab.gateway_browser import GatewayBrowser


def _run(coro):
    """Helper to run async coroutines in sync tests."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _make_ctx(actor_id: str = "actor-123", session_id: str = "session-abc") -> LabRequestContext:
    """Create a test LabRequestContext."""
    return LabRequestContext(
        actor_id=actor_id,
        session_id=session_id,
        request_id="req-test-001",
    )


def _make_projection(actor_id: str, tools: list[GatewayToolDescriptor]) -> GatewayProjection:
    """Create a test GatewayProjection."""
    return GatewayProjection(
        retrieved_at=1700000000000,
        actor_id=actor_id,
        tools=tools,
    )


def _make_gateway_browser(projection: GatewayProjection) -> GatewayBrowser:
    """Create a GatewayBrowser with a pre-populated projection."""
    client = MagicMock()
    browser = GatewayBrowser(gateway_client=client)
    # Pre-populate the cache
    cache_key = (projection.actor_id, "session-abc")
    browser._cache[cache_key] = projection
    return browser


def _make_mock_s3() -> MagicMock:
    """Create a mock S3UserFilesTool."""
    s3 = MagicMock()
    s3.write = AsyncMock()
    s3.read = AsyncMock()
    s3.delete = AsyncMock()
    s3.delete_prefix = AsyncMock()
    s3.list = AsyncMock(return_value=[])
    # Bucket-absolute companions: collection.json lives at the row's canonical prefix.
    s3.write_absolute = AsyncMock()
    s3.read_absolute = AsyncMock()
    s3.delete_absolute = AsyncMock()
    s3.delete_prefix_absolute = AsyncMock()
    s3.list_absolute = AsyncMock(return_value=[])
    return s3


def _make_mock_index() -> MagicMock:
    """Create a mock AssetIndex with the new async PK/SK API.

    Exposes ``put`` / ``get`` / ``update`` / ``query_pk`` /
    ``transact_write`` as AsyncMocks with empty defaults, plus
    ``table_name`` as a fixed string (the canonical-ops layer reads
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


def _make_tool_collection_record(
    *,
    asset_id: str = "tc-001",
    name: str = "media-primitives",
    status: str = "draft",
    tool_refs: list[ToolRef] | None = None,
) -> ToolCollection:
    """Build a typed ToolCollection record for the mock ``index.get``."""
    return ToolCollection(
        asset_id=asset_id,
        category=_CATEGORY,
        name=name,
        status=status,
        version=None if status == "draft" else 1,
        scope="private",
        s3_prefix=f"tool-collections/{asset_id}/",
        created_at=1700000000000,
        updated_at=1700000000000,
        tool_refs=list(tool_refs) if tool_refs else [],
    )


# ---------------------------------------------------------------------------
# Import and interface tests
# ---------------------------------------------------------------------------


def test_tool_collection_crud_has_expected_methods():
    """ToolCollectionCRUD class exposes the required CRUD methods."""
    assert hasattr(ToolCollectionCRUD, "create")
    assert hasattr(ToolCollectionCRUD, "read")
    assert hasattr(ToolCollectionCRUD, "update")
    assert hasattr(ToolCollectionCRUD, "delete")
    assert hasattr(ToolCollectionCRUD, "list")


def test_create_result_dataclass():
    """ToolCollectionCreateResult has the expected fields."""
    result = ToolCollectionCreateResult(
        asset_id="tc-001",
        s3_prefix="tool-collections/tc-001/",
    )
    assert result.asset_id == "tc-001"
    assert result.s3_prefix == "tool-collections/tc-001/"


def test_read_result_dataclass():
    """ToolCollectionReadResult has the expected fields."""
    result = ToolCollectionReadResult(
        asset_id="tc-001",
        name="media-primitives",
        tool_refs=[ToolRef(name="frame-extract", version="2.1.0")],
        metadata={"category": "tool-collection"},
    )
    assert result.asset_id == "tc-001"
    assert result.name == "media-primitives"
    assert len(result.tool_refs) == 1
    assert result.tool_refs[0].name == "frame-extract"


# ---------------------------------------------------------------------------
# Create tests
# ---------------------------------------------------------------------------


def test_create_with_valid_tool_refs():
    """Create succeeds when all tool refs resolve against the GatewayProjection."""
    ctx = _make_ctx()
    tools = [
        GatewayToolDescriptor(name="ns___frame-extract", description="Extract frames", version="2.1.0"),
        GatewayToolDescriptor(name="ns___speech-to-text", description="STT", version="1.4.2"),
    ]
    projection = _make_projection("actor-123", tools)
    browser = _make_gateway_browser(projection)
    s3 = _make_mock_s3()
    index = _make_mock_index()

    crud = ToolCollectionCRUD(s3=s3, index=index, gateway_browser=browser)

    tool_refs = [
        ToolRef(name="ns___frame-extract", version="2.1.0"),
        ToolRef(name="ns___speech-to-text", version="1.4.2"),
    ]

    result = _run(crud.create(ctx, name="media-primitives", tool_refs=tool_refs))

    assert result.asset_id is not None
    # Canonical s3_prefix uses the flat tool-collections/<id>/ form.
    assert result.s3_prefix == f"tool-collections/{result.asset_id}/"
    # collection.json is written at the canonical prefix the row advertises — never the
    # actor-relative path the S3 tool would prepend `{actor_id}/` to.
    s3.write_absolute.assert_called_once()
    assert s3.write_absolute.call_args.args[0] == f"{result.s3_prefix}collection.json"
    s3.write.assert_not_called()
    # Verify AssetIndex.put was called with a typed ToolCollection
    # under the requesting actor's owner_user_id.
    index.put.assert_awaited_once()
    put_arg = index.put.call_args.args[0]
    assert isinstance(put_arg, ToolCollection)
    assert put_arg.category == _CATEGORY
    assert put_arg.name == "media-primitives"
    assert put_arg.status == "draft"
    assert put_arg.scope == "private"
    assert len(put_arg.tool_refs) == 2
    assert index.put.call_args.kwargs["owner_user_id"] == ctx.actor_id


def test_create_with_empty_tool_refs():
    """Create succeeds with an empty tool_refs list (zero tools is valid)."""
    ctx = _make_ctx()
    projection = _make_projection("actor-123", [])
    browser = _make_gateway_browser(projection)
    s3 = _make_mock_s3()
    index = _make_mock_index()

    crud = ToolCollectionCRUD(s3=s3, index=index, gateway_browser=browser)

    result = _run(crud.create(ctx, name="empty-collection", tool_refs=[]))

    assert result.asset_id is not None
    s3.write_absolute.assert_called_once()
    index.put.assert_awaited_once()


def test_create_rejects_unresolved_tool_refs():
    """Create rejects when tool refs don't match the GatewayProjection (Req 4.4).

    Tool names use Gateway-style namespacing (``ns___name``) because
    ``_validate_tool_refs`` deliberately bypasses Gateway validation for
    bare-name built-in tools (see ``tool_collection_crud.py``). The
    validation gate this test exercises only fires for namespaced
    Gateway tools.
    """
    ctx = _make_ctx()
    # Only one tool available in the projection
    tools = [
        GatewayToolDescriptor(name="ns___frame-extract", description="Extract frames", version="2.1.0"),
    ]
    projection = _make_projection("actor-123", tools)
    browser = _make_gateway_browser(projection)
    s3 = _make_mock_s3()
    index = _make_mock_index()

    crud = ToolCollectionCRUD(s3=s3, index=index, gateway_browser=browser)

    # Try to create with a tool that doesn't exist in the projection
    tool_refs = [
        ToolRef(name="ns___frame-extract", version="2.1.0"),  # valid
        ToolRef(name="ns___nonexistent-tool", version="1.0.0"),  # invalid
    ]

    with pytest.raises(ValueError) as exc_info:
        _run(crud.create(ctx, name="bad-collection", tool_refs=tool_refs))

    # Error should identify the unresolved ref
    assert "ns___nonexistent-tool" in str(exc_info.value)
    # No S3 write or index put should have occurred
    s3.write.assert_not_called()
    index.put.assert_not_called()


def test_create_resolves_gateway_tool_by_name_ignoring_version():
    """Gateway refs resolve by NAME only; a differing version still resolves.

    Gateway tools carry no real version end to end (the catalog exposes none,
    the UI defaults to "1.0", the browser fabricates one), so validation matches
    on the fully-qualified name alone (BUG-3 fix). A ref whose name matches the
    projection is accepted regardless of the version string it carries.
    """
    ctx = _make_ctx()
    tools = [
        GatewayToolDescriptor(name="ns___frame-extract", description="Extract frames", version="2.1.0"),
    ]
    projection = _make_projection("actor-123", tools)
    browser = _make_gateway_browser(projection)
    s3 = _make_mock_s3()
    index = _make_mock_index()

    crud = ToolCollectionCRUD(s3=s3, index=index, gateway_browser=browser)

    # Version differs from the projection's "2.1.0" — must still resolve.
    tool_refs = [
        ToolRef(name="ns___frame-extract", version="9.9.9"),
    ]

    result = _run(crud.create(ctx, name="any-version", tool_refs=tool_refs))
    assert result.asset_id
    index.put.assert_awaited_once()


def test_create_rejects_unknown_gateway_tool_name():
    """Create rejects a gateway ref whose NAME is absent from the projection.

    Name-only resolution still rejects genuinely-unknown gateway tools and
    names each unresolved ref (Requirement 4.4).
    """
    ctx = _make_ctx()
    tools = [
        GatewayToolDescriptor(name="ns___frame-extract", description="Extract frames", version="2.1.0"),
    ]
    projection = _make_projection("actor-123", tools)
    browser = _make_gateway_browser(projection)
    s3 = _make_mock_s3()
    index = _make_mock_index()

    crud = ToolCollectionCRUD(s3=s3, index=index, gateway_browser=browser)

    tool_refs = [
        ToolRef(name="ns___does-not-exist", version="1.0"),  # unknown name
    ]

    with pytest.raises(ValueError) as exc_info:
        _run(crud.create(ctx, name="bad-name", tool_refs=tool_refs))

    assert "ns___does-not-exist" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Read tests
# ---------------------------------------------------------------------------


def test_read_returns_collection_data():
    """Read returns the collection name, tool refs, and metadata."""
    ctx = _make_ctx()
    projection = _make_projection("actor-123", [])
    browser = _make_gateway_browser(projection)
    s3 = _make_mock_s3()
    index = _make_mock_index()

    # Set up the typed record returned by the index.
    record = _make_tool_collection_record(
        asset_id="tc-001",
        name="media-primitives",
        status="draft",
        tool_refs=[ToolRef(name="frame-extract", version="2.1.0")],
    )
    index.get.return_value = record

    # Set up S3 to return collection.json
    collection_doc = {
        "name": "media-primitives",
        "tools": [{"name": "frame-extract", "version": "2.1.0"}],
    }
    s3.read_absolute.return_value = json.dumps(collection_doc).encode("utf-8")

    crud = ToolCollectionCRUD(s3=s3, index=index, gateway_browser=browser)

    result = _run(crud.read(ctx, "tc-001"))

    assert result is not None
    assert result.asset_id == "tc-001"
    assert result.name == "media-primitives"
    assert len(result.tool_refs) == 1
    assert result.tool_refs[0].name == "frame-extract"
    assert result.tool_refs[0].version == "2.1.0"
    assert result.metadata["category"] == _CATEGORY

    # Lookup uses the flat USER#<u>#TOOL_COLLECTIONS partition.
    index.get.assert_awaited_once_with(
        make_pk_user_owned(ctx.actor_id),
        make_sk_owned(_CATEGORY, "tc-001"),
    )


def test_read_returns_none_for_nonexistent():
    """Read returns None when the asset doesn't exist."""
    ctx = _make_ctx()
    projection = _make_projection("actor-123", [])
    browser = _make_gateway_browser(projection)
    s3 = _make_mock_s3()
    index = _make_mock_index()
    index.get.return_value = None

    crud = ToolCollectionCRUD(s3=s3, index=index, gateway_browser=browser)

    result = _run(crud.read(ctx, "nonexistent-id"))
    assert result is None


def test_read_returns_none_for_non_draft_status():
    """Read returns None for rows whose status is not 'draft'.

    The new schema replaces the legacy ``"archived"`` soft-delete with
    hard-delete, but the read still gates on ``status == "draft"`` so a
    deprecated/published row (e.g., from a future visibility-tier
    follow-on) does not surface through the draft read path.
    """
    ctx = _make_ctx()
    projection = _make_projection("actor-123", [])
    browser = _make_gateway_browser(projection)
    s3 = _make_mock_s3()
    index = _make_mock_index()
    index.get.return_value = _make_tool_collection_record(
        asset_id="tc-001",
        name="deprecated-collection",
        status="deprecated",
    )

    crud = ToolCollectionCRUD(s3=s3, index=index, gateway_browser=browser)

    result = _run(crud.read(ctx, "tc-001"))
    assert result is None


# ---------------------------------------------------------------------------
# Update tests
# ---------------------------------------------------------------------------


def test_update_name_only():
    """Update with only name change succeeds without re-validating tool refs."""
    ctx = _make_ctx()
    projection = _make_projection("actor-123", [])
    browser = _make_gateway_browser(projection)
    s3 = _make_mock_s3()
    index = _make_mock_index()

    # Existing record returned by the index.
    index.get.return_value = _make_tool_collection_record(
        asset_id="tc-001",
        name="old-name",
        status="draft",
    )

    # Set up S3 to return current collection.json
    collection_doc = {
        "name": "old-name",
        "tools": [{"name": "frame-extract", "version": "2.1.0"}],
    }
    s3.read_absolute.return_value = json.dumps(collection_doc).encode("utf-8")

    crud = ToolCollectionCRUD(s3=s3, index=index, gateway_browser=browser)

    result = _run(crud.update(ctx, "tc-001", name="new-name"))

    assert result is not None
    assert "updatedAt" in result
    # S3 write should have been called with updated collection.json, at the canonical prefix
    s3.write_absolute.assert_called_once()
    # AssetIndex update should have been called with a SET expression
    # bumping updatedAt and renaming via the reserved-word placeholder.
    index.update.assert_awaited_once()
    update_kwargs = index.update.call_args.kwargs
    assert "#name = :name" in update_kwargs["update_expression"]
    assert update_kwargs["expression_attribute_values"][":name"] == "new-name"
    assert update_kwargs["expression_attribute_names"]["#name"] == "name"


def test_update_tool_refs_validates():
    """Update with new tool_refs re-validates against the GatewayProjection."""
    ctx = _make_ctx()
    tools = [
        GatewayToolDescriptor(name="ns___new-tool", description="New", version="1.0.0"),
    ]
    projection = _make_projection("actor-123", tools)
    browser = _make_gateway_browser(projection)
    s3 = _make_mock_s3()
    index = _make_mock_index()

    index.get.return_value = _make_tool_collection_record(
        asset_id="tc-001",
        name="my-collection",
        status="draft",
    )

    crud = ToolCollectionCRUD(s3=s3, index=index, gateway_browser=browser)

    new_refs = [ToolRef(name="ns___new-tool", version="1.0.0")]
    result = _run(crud.update(ctx, "tc-001", tool_refs=new_refs))

    assert result is not None
    assert "updatedAt" in result
    # The toolRefs DDB attribute is camelCased in the update expression.
    update_kwargs = index.update.call_args.kwargs
    assert "toolRefs = :toolRefs" in update_kwargs["update_expression"]
    refs_values = update_kwargs["expression_attribute_values"][":toolRefs"]
    assert refs_values == [{"name": "ns___new-tool", "version": "1.0.0"}]


def test_update_rejects_unresolved_tool_refs():
    """Update rejects when new tool refs don't resolve (Req 4.4).

    Uses Gateway-style namespaced names; bare-name tools bypass the
    Gateway validation gate (built-in tools) and would not exercise
    the rejection path this test asserts.
    """
    ctx = _make_ctx()
    tools = [
        GatewayToolDescriptor(name="ns___valid-tool", description="Valid", version="1.0.0"),
    ]
    projection = _make_projection("actor-123", tools)
    browser = _make_gateway_browser(projection)
    s3 = _make_mock_s3()
    index = _make_mock_index()

    index.get.return_value = _make_tool_collection_record(
        asset_id="tc-001",
        name="my-collection",
        status="draft",
    )

    crud = ToolCollectionCRUD(s3=s3, index=index, gateway_browser=browser)

    bad_refs = [ToolRef(name="ns___nonexistent", version="0.0.1")]
    with pytest.raises(ValueError) as exc_info:
        _run(crud.update(ctx, "tc-001", tool_refs=bad_refs))

    assert "ns___nonexistent" in str(exc_info.value)
    # S3 should NOT have been written (validation failed before write)
    s3.write.assert_not_called()


def test_update_returns_none_for_nonexistent():
    """Update returns None when the asset doesn't exist."""
    ctx = _make_ctx()
    projection = _make_projection("actor-123", [])
    browser = _make_gateway_browser(projection)
    s3 = _make_mock_s3()
    index = _make_mock_index()
    index.get.return_value = None

    crud = ToolCollectionCRUD(s3=s3, index=index, gateway_browser=browser)

    result = _run(crud.update(ctx, "nonexistent", name="new-name"))
    assert result is None


# ---------------------------------------------------------------------------
# Delete tests
# ---------------------------------------------------------------------------


def test_delete_two_phase():
    """Delete hard-deletes the index row, then reclaims the S3 prefix."""
    ctx = _make_ctx()
    projection = _make_projection("actor-123", [])
    browser = _make_gateway_browser(projection)
    s3 = _make_mock_s3()
    index = _make_mock_index()

    crud = ToolCollectionCRUD(s3=s3, index=index, gateway_browser=browser)

    result = _run(crud.delete(ctx, "tc-001"))

    assert result is True
    # Phase 1: hard-delete via transact_write under the canonical key,
    # gated by attribute_exists(PK) so a missing row surfaces as
    # not-found rather than 5xx.
    index.transact_write.assert_awaited_once()
    ops = index.transact_write.call_args.args[0]
    assert isinstance(ops, list)
    assert len(ops) == 1
    delete_op = ops[0]["Delete"]
    assert delete_op["TableName"] == index.table_name
    # Scalar key shape (matches skill_crud / specialty_crud) — the
    # boto3 Table layer handles marshalling.
    assert delete_op["Key"]["PK"] == make_pk_user_owned(ctx.actor_id)
    assert delete_op["Key"]["SK"] == make_sk_owned(_CATEGORY, "tc-001")
    assert delete_op["ConditionExpression"] == "attribute_exists(PK)"
    # Phase 2: S3 prefix delete — the canonical prefix AND the pre-fix actor-relative one
    s3.delete_prefix_absolute.assert_called_once_with("tool-collections/tc-001/")
    s3.delete_prefix.assert_called_once_with(f"{_DRAFTS_PREFIX}/tc-001/")


def test_delete_returns_none_for_nonexistent():
    """Delete returns None when the row doesn't exist.

    The CRUD wraps ``transact_write`` so a
    ``TransactionCanceledException`` whose only condition was
    ``attribute_exists(PK)`` (i.e. the row was missing) surfaces as
    not-found rather than as a 5xx.
    """
    from botocore.exceptions import ClientError

    ctx = _make_ctx()
    projection = _make_projection("actor-123", [])
    browser = _make_gateway_browser(projection)
    s3 = _make_mock_s3()
    index = _make_mock_index()
    index.transact_write.side_effect = ClientError(
        {
            "Error": {
                "Code": "TransactionCanceledException",
                "Message": "txn cancelled",
            },
            "CancellationReasons": [{"Code": "ConditionalCheckFailed"}],
        },
        "TransactWriteItems",
    )

    crud = ToolCollectionCRUD(s3=s3, index=index, gateway_browser=browser)

    result = _run(crud.delete(ctx, "nonexistent"))
    assert result is None
    # S3 delete_prefix should NOT have been called when the index op
    # surfaced not-found.
    s3.delete_prefix.assert_not_called()


def test_delete_returns_none_for_bare_conditional_check_failed():
    """Delete tolerates a bare ConditionalCheckFailedException as not-found."""
    from botocore.exceptions import ClientError

    ctx = _make_ctx()
    projection = _make_projection("actor-123", [])
    browser = _make_gateway_browser(projection)
    s3 = _make_mock_s3()
    index = _make_mock_index()
    index.transact_write.side_effect = ClientError(
        {
            "Error": {
                "Code": "ConditionalCheckFailedException",
                "Message": "condition failed",
            }
        },
        "TransactWriteItems",
    )

    crud = ToolCollectionCRUD(s3=s3, index=index, gateway_browser=browser)

    result = _run(crud.delete(ctx, "nonexistent"))
    assert result is None
    s3.delete_prefix.assert_not_called()


# ---------------------------------------------------------------------------
# List tests
# ---------------------------------------------------------------------------


def test_list_returns_active_drafts():
    """List returns all tool-collection rows for the actor's flat partition."""
    ctx = _make_ctx()
    projection = _make_projection("actor-123", [])
    browser = _make_gateway_browser(projection)
    s3 = _make_mock_s3()
    index = _make_mock_index()

    items = [
        _make_tool_collection_record(asset_id="tc-001", name="collection-1"),
        _make_tool_collection_record(asset_id="tc-002", name="collection-2"),
    ]
    index.query_pk.return_value = (items, None)

    crud = ToolCollectionCRUD(s3=s3, index=index, gateway_browser=browser)

    results = _run(crud.list(ctx))

    assert len(results) == 2
    assert all(isinstance(r, ToolCollection) for r in results)
    index.query_pk.assert_awaited_once()
    pk_arg = index.query_pk.call_args.args[0]
    assert pk_arg == make_pk_user_owned(ctx.actor_id)
    assert index.query_pk.call_args.kwargs["sk_prefix"] == f"{_CATEGORY}#"


def test_list_returns_empty_when_no_drafts():
    """List returns empty list when no tool collections exist."""
    ctx = _make_ctx()
    projection = _make_projection("actor-123", [])
    browser = _make_gateway_browser(projection)
    s3 = _make_mock_s3()
    index = _make_mock_index()
    index.query_pk.return_value = ([], None)

    crud = ToolCollectionCRUD(s3=s3, index=index, gateway_browser=browser)

    results = _run(crud.list(ctx))
    assert results == []


# ---------------------------------------------------------------------------
# Delete: the referential-integrity gate
# ---------------------------------------------------------------------------
#
# A collection is flat, so this delete is the irreversible operation a purge is
# for other categories — and it used to run with no reference check at all.
# Deleting one an agent still selected did not fail: the agent's next invoke
# resolved the id to nothing, dropped it with a log line, and answered in persona
# with its tools silently missing.


def _make_agent_row(
    asset_id: str,
    *,
    collections: list[str],
    name: str = "Media Agent",
    status: str = "draft",
    version=None,
) -> MagicMock:
    row = MagicMock()
    row.asset_id = asset_id
    row.name = name
    row.status = status
    row.version = version
    # Explicit: a bare MagicMock attribute reads as a malformed list.
    row.tool_collections = collections
    return row


def _patch_visible_agents(monkeypatch, rows, error=None):
    """Feed the reference scan a fixed agent listing.

    Patches the symbol on ``lab.service``, which ``purge_scan._visible_agents``
    imports lazily at call time.
    """
    import lab.service as lab_service
    from lab.operation_results import VisibleAssetsPage

    async def _fake(ctx, category, mode, **kwargs):
        if error is not None:
            raise error
        return VisibleAssetsPage(items=list(rows), next_cursor=None)

    monkeypatch.setattr(lab_service, "list_visible_assets", _fake)


def _delete_crud(index=None, s3=None):
    projection = _make_projection("actor-123", [])
    browser = _make_gateway_browser(projection)
    return ToolCollectionCRUD(
        s3=s3 if s3 is not None else _make_mock_s3(),
        index=index if index is not None else _make_mock_index(),
        gateway_browser=browser,
    )


def test_delete_refuses_while_an_agent_still_selects_the_collection(monkeypatch):
    from lab.errors import StillReferencedError

    _patch_visible_agents(
        monkeypatch, [_make_agent_row("a-1", collections=["tc-001"])]
    )
    index = _make_mock_index()
    s3 = _make_mock_s3()
    crud = _delete_crud(index=index, s3=s3)

    with pytest.raises(StillReferencedError) as excinfo:
        _run(crud.delete(_make_ctx(), "tc-001"))

    err = excinfo.value
    assert err.asset_id == "tc-001"
    assert [h.asset_id for h in err.hits] == ["a-1"]
    # The refusal names the agent, because "still referenced" without saying by
    # what leaves the user nothing to act on.
    assert "Media Agent" in str(err)
    # And it changed nothing: refused before either phase ran.
    assert not index.transact_write.called
    assert not s3.delete_prefix.called


def test_delete_names_a_published_reference_as_such(monkeypatch):
    """A published version's reference can never be edited away — say so."""
    from lab.errors import StillReferencedError

    _patch_visible_agents(
        monkeypatch,
        [
            _make_agent_row(
                "a-1", collections=["tc-001"], status="published", version=3
            )
        ],
    )
    crud = _delete_crud()

    with pytest.raises(StillReferencedError) as excinfo:
        _run(crud.delete(_make_ctx(), "tc-001"))

    assert excinfo.value.hits[0].status == "published"
    assert "v3" in excinfo.value.hits[0].detail


def test_delete_proceeds_when_nothing_references_the_collection(monkeypatch):
    _patch_visible_agents(
        monkeypatch, [_make_agent_row("a-1", collections=["tc-other"])]
    )
    index = _make_mock_index()
    s3 = _make_mock_s3()
    crud = _delete_crud(index=index, s3=s3)

    assert _run(crud.delete(_make_ctx(), "tc-001")) is True
    index.transact_write.assert_awaited_once()
    s3.delete_prefix.assert_called_once_with(f"{_DRAFTS_PREFIX}/tc-001/")


def test_force_deletes_despite_references(monkeypatch):
    """The escape hatch: cleaning up once the references are known to be stale."""
    _patch_visible_agents(
        monkeypatch, [_make_agent_row("a-1", collections=["tc-001"])]
    )
    index = _make_mock_index()
    crud = _delete_crud(index=index)

    assert _run(crud.delete(_make_ctx(), "tc-001", force=True)) is True
    index.transact_write.assert_awaited_once()


def test_force_skips_the_scan_entirely(monkeypatch):
    """Not just ignore the result — a forced delete must not need the listing."""
    _patch_visible_agents(monkeypatch, [], error=RuntimeError("must not be called"))
    crud = _delete_crud()

    assert _run(crud.delete(_make_ctx(), "tc-001", force=True)) is True


def test_delete_proceeds_when_the_scan_is_incomplete(monkeypatch):
    """An unscannable tier must not make a collection permanently undeletable.

    ``LabRequestContext`` carries no org id, so the org tier is almost always
    unscanned. Blocking on ``partial`` would therefore block nearly every delete;
    the incompleteness is logged instead.
    """
    _patch_visible_agents(monkeypatch, [], error=RuntimeError("ddb down"))
    index = _make_mock_index()
    crud = _delete_crud(index=index)

    assert _run(crud.delete(_make_ctx(), "tc-001")) is True
    index.transact_write.assert_awaited_once()


def test_delete_still_reports_not_found_after_passing_the_gate(monkeypatch):
    """The gate must not turn a missing row into a spurious success."""
    from botocore.exceptions import ClientError

    _patch_visible_agents(monkeypatch, [])
    index = _make_mock_index()
    index.transact_write.side_effect = ClientError(
        {
            "Error": {"Code": "TransactionCanceledException", "Message": "x"},
            "CancellationReasons": [{"Code": "ConditionalCheckFailed"}],
        },
        "TransactWriteItems",
    )
    crud = _delete_crud(index=index)

    assert _run(crud.delete(_make_ctx(), "gone")) is None


# ---------------------------------------------------------------------------
# The refusal's wire shape
# ---------------------------------------------------------------------------


def test_delete_handler_returns_the_references_as_data(monkeypatch):
    """The UI has to name the blocking agents, so this is data, not an exception.

    ``labAction`` turns a body carrying both ``error`` and ``message`` into a
    thrown ``LabApiError``, which is how the row surfaces it inline — so the
    message has to stand alone, and ``references`` carries the structured form
    for anything that wants more. ``partial`` travels too: the scan sees only
    what this actor can see, so the list is "references visible to you".
    """
    from lab.service import LabService

    _patch_visible_agents(
        monkeypatch, [_make_agent_row("a-1", collections=["tc-001"])]
    )

    service = LabService.__new__(LabService)
    service.ctx = _make_ctx()
    service.tool_collection_crud = _delete_crud()

    out = _run(service._handle_delete_tool_collection_draft({"assetId": "tc-001"}))

    assert out["error"] == "still_referenced"
    assert "Media Agent" in out["message"]
    assert out["references"] == [
        {
            "kind": "agent-tool-collection",
            "assetId": "a-1",
            "name": "Media Agent",
            "status": "draft",
            "detail": "tool collection (working copy)",
        }
    ]
    # No org id on the context, so the org tier went unscanned and says so.
    assert any("org-shared agents" in p for p in out["partial"])


def test_delete_handler_passes_force_through(monkeypatch):
    from lab.service import LabService

    _patch_visible_agents(
        monkeypatch, [_make_agent_row("a-1", collections=["tc-001"])]
    )

    service = LabService.__new__(LabService)
    service.ctx = _make_ctx()
    service.tool_collection_crud = _delete_crud()

    out = _run(
        service._handle_delete_tool_collection_draft(
            {"assetId": "tc-001", "force": True}
        )
    )

    assert out == {"status": "ok"}


# ---------------------------------------------------------------------------
# Path fix: collection.json lives where the row says it does
# ---------------------------------------------------------------------------
#
# It used to be written ACTOR-RELATIVE (the S3 tool prepends `{actor_id}/`), landing at
# `{actor}/drafts/tool-collections/<id>/` while the row advertised `s3_prefix`. Anything that
# trusted `s3_prefix` — as every other lab category is read — found nothing.


def _legacy_only_s3(doc: dict):
    """An S3 fake holding collection.json ONLY at the pre-fix actor-relative path."""
    s3 = _make_mock_s3()
    s3.read_absolute.side_effect = FileNotFoundError("not at the canonical prefix")
    s3.read.return_value = json.dumps(doc).encode("utf-8")
    return s3


def test_read_falls_back_to_a_collection_written_before_the_fix():
    ctx = _make_ctx()
    browser = _make_gateway_browser(_make_projection("actor-123", []))
    index = _make_mock_index()
    record = MagicMock(status="draft", s3_prefix="tool-collections/tc-legacy/", category=_CATEGORY,
                       created_at=1, updated_at=1, tool_refs=[])
    record.name = "stale-row-name"
    index.get.return_value = record
    s3 = _legacy_only_s3({"name": "from-legacy-file", "tools": [{"name": "frame-extract", "version": "2.1.0"}]})

    result = _run(ToolCollectionCRUD(s3=s3, index=index, gateway_browser=browser).read(ctx, "tc-legacy"))

    s3.read_absolute.assert_awaited_once_with("tool-collections/tc-legacy/collection.json")
    s3.read.assert_awaited_once_with(f"{_DRAFTS_PREFIX}/tc-legacy/collection.json")
    assert result.name == "from-legacy-file"
    assert [r.name for r in result.tool_refs] == ["frame-extract"]


def test_update_migrates_a_legacy_collection_to_the_canonical_prefix():
    ctx = _make_ctx()
    browser = _make_gateway_browser(_make_projection("actor-123", []))
    index = _make_mock_index()
    record = MagicMock(status="draft", s3_prefix="tool-collections/tc-legacy/", category=_CATEGORY,
                       created_at=1, updated_at=1, tool_refs=[])
    record.name = "old"
    index.get.return_value = record
    s3 = _legacy_only_s3({"name": "old", "tools": [{"name": "frame-extract", "version": "2.1.0"}]})

    _run(ToolCollectionCRUD(s3=s3, index=index, gateway_browser=browser).update(ctx, "tc-legacy", name="new"))

    # The existing refs were preserved from the legacy file, and the result is written where
    # the row says — so the next read is a canonical hit.
    s3.write_absolute.assert_awaited_once()
    key, body = s3.write_absolute.call_args.args
    assert key == "tool-collections/tc-legacy/collection.json"
    written = json.loads(body)
    assert written["name"] == "new"
    assert [t["name"] for t in written["tools"]] == ["frame-extract"]
    s3.write.assert_not_called()
