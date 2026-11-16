"""Property-based test for revocation precedence across rollback (Property 16).

Feature: custom-agents, Property 16: Revocation precedence across rollback

Validates: Requirements 16.2, 16.3

Property 16 (design.md): *For any* revocation record and *any* version snapshot
restored by rollback, the effective external-access state is
``enabled_in_snapshot AND NOT revoked``. Whenever a revocation record exists for
an agent, external access resolves to disabled regardless of the restored
snapshot's ``integration_config`` or the agent's version history, until the
owner explicitly re-enables.

Design model (design.md, Req 16):
    effective_access = snapshot.integration_config.enabled
                       AND external_access_enabled
where the revocation record is represented by ``external_access_enabled=False``
on every row of the lineage. Rollback (``_canonical_ops.rollback_asset``) only
flips the ``current`` pointer between existing snapshot rows and NEVER touches
``externalAccessEnabled``; the revoke/re-enable control
(``_canonical_ops.set_external_access_asset``) stamps the flag uniformly across
every row of the version history so a rolled-back snapshot cannot silently
re-grant revoked access.

Chosen test layer
-----------------
This drives the *real* revocation + rollback code end to end against an
in-memory AssetIndex stand-in that interprets the exact TransactWriteItems
shapes the canonical ops emit (conditional current-pointer flip for rollback;
uniform ``externalAccessEnabled`` write across the lineage for revoke). The
gate is resolved from the *resolved current row* after each rollback — exactly
what the request-time proxy does — so the rollback-safety mechanism is genuinely
exercised, not stubbed.
"""

from __future__ import annotations

import asyncio
import copy
import os
import sys
import time
from typing import Any, Optional

from botocore.exceptions import ClientError
from hypothesis import given, settings
from hypothesis import strategies as st

# Add agent/src to sys.path so imports resolve (mirrors the other agent tests).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab.asset_index import (  # noqa: E402
    make_gsi1_keys,
    make_pk_user_owned,
    make_pk_user_versions,
    make_sk_owned,
    make_sk_version,
)
from lab.context import LabRequestContext  # noqa: E402
from lab._canonical_ops import (  # noqa: E402
    rollback_asset,
    set_external_access_asset,
)
from lab.models import (  # noqa: E402
    IntegrationConfig,
    PublishPolicy,
    SpecialtyAgent,
    from_index_item,
    to_index_item,
)

_ACTOR = "u-owner"
_CATEGORY = "specialty-agent"
_AGENT_ID = "agent-p16"


def _run(coro):
    """Run an async coroutine to completion in a sync test."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# In-memory AssetIndex stand-in — interprets the exact TransactWriteItems the
# canonical ops emit (uniform external-access write + conditional pointer flip).
# ---------------------------------------------------------------------------


def _txn_canceled_error() -> ClientError:
    response = {
        "Error": {
            "Code": "TransactionCanceledException",
            "Message": "Transaction cancelled",
        },
        "CancellationReasons": [{"Code": "ConditionalCheckFailed"}],
    }
    return ClientError(response, "TransactWriteItems")


def _apply_set_expression(
    item: dict[str, Any],
    update_expression: str,
    names: Optional[dict[str, str]],
    values: dict[str, Any],
) -> None:
    """Apply a ``SET a = :x, b = :y`` UpdateExpression to ``item`` in place."""
    body = update_expression.strip()
    assert body.upper().startswith("SET "), f"unsupported expr: {body!r}"
    names = names or {}
    for assignment in body[4:].split(","):
        lhs, rhs = (part.strip() for part in assignment.split("="))
        attr = names.get(lhs, lhs)
        item[attr] = values[rhs]


def _evaluate_condition(
    condition: str,
    *,
    exists: bool,
    item: Optional[dict[str, Any]],
    names: Optional[dict[str, str]],
    values: Optional[dict[str, Any]],
) -> bool:
    """Evaluate the small set of ConditionExpressions the canonical ops use."""
    cond = condition.strip()
    if cond == "attribute_not_exists(PK)":
        return not exists
    if cond == "attribute_exists(PK)":
        return exists
    if "=" in cond:  # e.g. "#current = :true"
        lhs, rhs = (part.strip() for part in cond.split("="))
        attr = (names or {}).get(lhs, lhs)
        expected = (values or {})[rhs]
        return item is not None and item.get(attr) == expected
    raise ValueError(f"unsupported condition: {condition!r}")


class InMemoryAssetIndex:
    """Stateful AssetIndex stand-in keyed by ``(PK, SK)`` raw DDB items."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict[str, Any]] = {}
        self.table_name = "test-asset-index"

    # --- helpers -------------------------------------------------------
    def _scope_keys(
        self,
        asset: SpecialtyAgent,
        owner_user_id: Optional[str],
    ) -> tuple[str, str]:
        if asset.version is None:
            return (
                make_pk_user_owned(owner_user_id),
                make_sk_owned(asset.category, asset.asset_id),
            )
        # Only the private scope is exercised by this property.
        sk = make_sk_version(asset.category, asset.asset_id, asset.version)
        return make_pk_user_versions(owner_user_id), sk

    # --- AssetIndex async surface -------------------------------------
    async def get(self, pk: str, sk: str) -> Optional[SpecialtyAgent]:
        item = self._items.get((pk, sk))
        if item is None:
            return None
        return from_index_item(copy.deepcopy(item))

    async def query_lineage(self, published_id: str) -> list[SpecialtyAgent]:
        gsi1pk = f"PUB#{published_id}"
        rows = [
            copy.deepcopy(v)
            for v in self._items.values()
            if v.get("GSI1PK") == gsi1pk
        ]
        rows.sort(key=lambda r: r.get("GSI1SK", ""))
        return [from_index_item(r) for r in rows]

    async def put(
        self,
        asset: SpecialtyAgent,
        *,
        owner_user_id: Optional[str] = None,
    ) -> None:
        item = to_index_item(
            asset,
            owner_user_id=owner_user_id,
            owner_group_id=None,
            owner_org_id=None,
        )
        pk, sk = self._scope_keys(asset, owner_user_id)
        item["PK"] = pk
        item["SK"] = sk
        if asset.version is not None:
            gsi1pk, gsi1sk = make_gsi1_keys(asset.asset_id, asset.version)
            item["GSI1PK"] = gsi1pk
            item["GSI1SK"] = gsi1sk
        self._items[(pk, sk)] = item

    async def transact_write(self, operations: list[dict[str, Any]]) -> None:
        # Phase 1: validate every ConditionExpression against current state.
        for op in operations:
            (_verb, params), = op.items()
            if "Item" in params:
                key = (params["Item"]["PK"], params["Item"]["SK"])
            else:
                key = (params["Key"]["PK"], params["Key"]["SK"])
            existing = self._items.get(key)
            cond = params.get("ConditionExpression")
            if cond and not _evaluate_condition(
                cond,
                exists=existing is not None,
                item=existing,
                names=params.get("ExpressionAttributeNames"),
                values=params.get("ExpressionAttributeValues"),
            ):
                raise _txn_canceled_error()

        # Phase 2: apply mutations atomically.
        for op in operations:
            (verb, params), = op.items()
            if verb == "Put":
                item = copy.deepcopy(params["Item"])
                self._items[(item["PK"], item["SK"])] = item
            elif verb == "Update":
                key = (params["Key"]["PK"], params["Key"]["SK"])
                item = self._items.get(key)
                if item is None:
                    item = {"PK": key[0], "SK": key[1]}
                    self._items[key] = item
                _apply_set_expression(
                    item,
                    params["UpdateExpression"],
                    params.get("ExpressionAttributeNames"),
                    params["ExpressionAttributeValues"],
                )
            elif verb == "Delete":
                self._items.pop((params["Key"]["PK"], params["Key"]["SK"]), None)
            else:  # pragma: no cover - defensive
                raise ValueError(f"unsupported transact verb: {verb}")


# ---------------------------------------------------------------------------
# The design's effective-access formula, as a pure function.
# ---------------------------------------------------------------------------


def _snapshot_enabled(row: SpecialtyAgent) -> bool:
    """``enabled_in_snapshot`` — the frozen integration_config's enabled flag."""
    ic = row.integration_config
    return bool(ic is not None and ic.enabled)


def _effective_access(row: SpecialtyAgent) -> bool:
    """Design formula: snapshot.enabled AND external_access_enabled (NOT revoked)."""
    return _snapshot_enabled(row) and bool(row.external_access_enabled)


def _resolve_current(lineage: list[SpecialtyAgent]) -> SpecialtyAgent:
    """The request-time resolution: the single ``current`` published row."""
    current = [r for r in lineage if r.current and r.version is not None]
    assert len(current) == 1, "exactly one current published version"
    return current[0]


# ---------------------------------------------------------------------------
# Strategy: a private-scope lineage of N version snapshots, each carrying an
# independently-generated integration_config (so ``enabled_in_snapshot`` varies
# across the version history), all initially externally enabled and with the
# highest version marked current.
# ---------------------------------------------------------------------------

_id_text = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"),
        whitelist_characters=".:-_",
    ),
    min_size=1,
    max_size=12,
)

_integration_configs = st.one_of(
    st.none(),
    st.builds(
        IntegrationConfig,
        enabled=st.booleans(),
        issuer=st.one_of(st.none(), st.text(min_size=1, max_size=32)),
        audience=st.one_of(st.none(), st.text(min_size=1, max_size=32)),
        allowed_client_ids=st.lists(_id_text, max_size=3),
    ),
)


def _make_version_row(version: int, ic: Optional[IntegrationConfig]) -> SpecialtyAgent:
    """Build one immutable published version snapshot (private scope)."""
    now = int(time.time() * 1000)
    return SpecialtyAgent(
        asset_id=_AGENT_ID,
        category=_CATEGORY,
        name="Custom Agent P16",
        status="published",
        scope="private",
        # ``users/<userId>/...`` so _owner_user_id_from_prefix recovers the owner.
        s3_prefix=(
            f"users/{_ACTOR}/published/specialty-agents/{_AGENT_ID}/v{version}/"
        ),
        created_at=now,
        updated_at=now,
        version=version,
        current=False,
        publish_policy=PublishPolicy(owner_ids=[_ACTOR]),
        integration_config=ic,
        external_access_enabled=True,
    )


@st.composite
def _lineage(draw) -> list[IntegrationConfig]:
    """Generate a list of per-version integration_configs (len 2..5)."""
    n = draw(st.integers(min_value=2, max_value=5))
    return draw(
        st.lists(_integration_configs, min_size=n, max_size=n)
    )


def _seed_index(configs: list[Optional[IntegrationConfig]]) -> InMemoryAssetIndex:
    """Seed a fresh index with versions 1..N, marking the highest current."""
    index = InMemoryAssetIndex()
    n = len(configs)
    for i, ic in enumerate(configs):
        version = i + 1
        row = _make_version_row(version, ic)
        row.current = version == n  # highest version is current
        _run(index.put(row, owner_user_id=_ACTOR))
    return index


def _ctx() -> LabRequestContext:
    return LabRequestContext(
        actor_id=_ACTOR,
        session_id="sess-p16",
        request_id="req-p16",
    )


# ---------------------------------------------------------------------------
# Feature: custom-agents, Property 16: Revocation precedence across rollback
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(configs=_lineage())
def test_property16_revocation_survives_rollback(configs):
    """A revocation record forces effective access disabled at EVERY restored
    snapshot, regardless of that snapshot's integration_config or the agent's
    version history.

    **Validates: Requirements 16.2, 16.3**
    """
    index = _seed_index(configs)
    ctx = _ctx()
    n = len(configs)

    # --- Revoke external access (writes the record across the whole lineage).
    result = _run(
        set_external_access_asset(
            ctx=ctx,
            asset_index=index,
            category=_CATEGORY,
            published_id=_AGENT_ID,
            enabled=False,
        )
    )
    assert result.enabled is False
    assert result.rows_updated == n  # every version snapshot stamped

    # Assert at the initial current version (v=N): revoked => disabled.
    current = _resolve_current(_run(index.query_lineage(_AGENT_ID)))
    assert current.version == n
    assert current.external_access_enabled is False
    assert _effective_access(current) is False

    # --- Walk the current pointer DOWN the entire history via rollback.
    # Each rollback restores an earlier snapshot; effective access must stay
    # disabled no matter what that snapshot's integration_config.enabled is.
    for expected_version in range(n - 1, 0, -1):
        _run(
            rollback_asset(
                ctx=ctx,
                asset_index=index,
                category=_CATEGORY,
                published_id=_AGENT_ID,
            )
        )
        restored = _resolve_current(_run(index.query_lineage(_AGENT_ID)))
        assert restored.version == expected_version

        # 16.2/16.3: rollback did NOT touch the revocation record, so the
        # restored snapshot resolves to disabled independent of its own
        # snapshot flag and independent of the version history.
        assert restored.external_access_enabled is False
        assert _effective_access(restored) is False
        # ...and this holds EVEN WHEN the restored snapshot was published with
        # external access enabled (proves rollback cannot silently re-grant).
        if _snapshot_enabled(restored):
            assert _effective_access(restored) is False


@settings(max_examples=200, deadline=None)
@given(configs=_lineage())
def test_property16_effective_access_equals_snapshot_and_not_revoked(configs):
    """The effective-access formula holds across revoke, rollback, and
    re-enable: effective == enabled_in_snapshot AND NOT revoked.

    After the owner explicitly re-enables, the revocation record is cleared and
    effective access is once again governed solely by each restored snapshot's
    integration_config.enabled.

    **Validates: Requirements 16.2, 16.3**
    """
    index = _seed_index(configs)
    ctx = _ctx()
    n = len(configs)

    # Baseline (not revoked): effective == enabled_in_snapshot for current.
    current = _resolve_current(_run(index.query_lineage(_AGENT_ID)))
    assert current.external_access_enabled is True
    assert _effective_access(current) == _snapshot_enabled(current)

    # Revoke, then re-enable: the record is cleared uniformly across the lineage.
    _run(
        set_external_access_asset(
            ctx=ctx,
            asset_index=index,
            category=_CATEGORY,
            published_id=_AGENT_ID,
            enabled=False,
        )
    )
    reenable = _run(
        set_external_access_asset(
            ctx=ctx,
            asset_index=index,
            category=_CATEGORY,
            published_id=_AGENT_ID,
            enabled=True,
        )
    )
    assert reenable.enabled is True
    assert reenable.rows_updated == n

    # Walk the whole history: with the record cleared (NOT revoked), effective
    # access at every restored snapshot equals that snapshot's own flag.
    current = _resolve_current(_run(index.query_lineage(_AGENT_ID)))
    assert current.external_access_enabled is True
    assert _effective_access(current) == _snapshot_enabled(current)

    for expected_version in range(n - 1, 0, -1):
        _run(
            rollback_asset(
                ctx=ctx,
                asset_index=index,
                category=_CATEGORY,
                published_id=_AGENT_ID,
            )
        )
        restored = _resolve_current(_run(index.query_lineage(_AGENT_ID)))
        assert restored.version == expected_version
        assert restored.external_access_enabled is True
        # Formula: effective == enabled_in_snapshot AND NOT revoked (True).
        assert _effective_access(restored) == _snapshot_enabled(restored)
