"""Property-based test for Custom Agent version integrity (Property 10).

Feature: custom-agents, Property 10: Version integrity

Validates: Requirements 5.2, 8.2, 8.4, 8.7

Property 10 (design.md): For any Custom_Agent, publishing snapshots the full
draft configuration (persona, model, tool_collections, compatible_skills,
integration_config) into an immutable version; subsequent draft edits change
only the draft and never mutate existing snapshots; and an external invocation
always resolves and executes the current published version's configuration,
never the draft.

Chosen test layer
-----------------
This drives the *real* versioning code end to end, exactly as the design
prescribes it (design.md Component 5 / "Reuse over rebuild"):

    publish : ``_lifecycle_ops.publish_asset`` -> ``_canonical_ops.promote_asset``
              -> ``_build_published_asset`` (snapshots the extended config)
    edit    : ``_lifecycle_ops.edit_asset`` -> ``_clone_to_working_copy``
              (forks the current published version back into a working copy,
               carrying the extended fields per task 4.1)
    resolve : ``AssetIndex.query_lineage`` + the ``current == True`` selection
              (the same "current published version" lookup the external
               invoke path performs before dispatch)

To exercise those real code paths across 100+ generated configs without a live
DynamoDB/S3, this module provides two in-memory fakes that implement only the
async surface the lifecycle/canonical ops touch: ``AssetIndex`` (``get``,
``query_lineage``, ``transact_write``, ``put``, ``table_name``) and
``S3UserFilesTool`` (``copy_prefix_absolute`` / ``delete_prefix_absolute``).
The fakes interpret the exact TransactWriteItems shapes the canonical ops emit
(Put + conditional current-pointer flip + draft ``lastPublishedAs`` update +
working-copy Delete) so the transaction semantics that make snapshots immutable
are genuinely tested, not stubbed.
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
    make_pk_group_shared,
    make_pk_org_shared,
    make_pk_user_owned,
    make_pk_user_versions,
    make_sk_owned,
    make_sk_version,
)
from lab.context import LabRequestContext  # noqa: E402
from lab._lifecycle_ops import edit_asset, publish_asset  # noqa: E402
from lab.models import (  # noqa: E402
    IntegrationConfig,
    SpecialtyAgent,
    ToolCollection,
    ToolRef,
    from_index_item,
    to_index_item,
)

_ACTOR = "u-owner"
_CATEGORY = "specialty-agent"
_AGENT_ID = "agent-p10"
_PERSONA_FILE = "persona.md"


def _run(coro):
    """Run an async coroutine to completion in a sync test."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# In-memory fakes for the two collaborators the versioning ops touch.
# ---------------------------------------------------------------------------


def _txn_canceled_error() -> ClientError:
    """Mimic a boto3 TransactWriteItems cancellation (a conditional failure)."""
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
    """Apply a ``SET a = :x, b = :y`` UpdateExpression to ``item`` in place.

    Only the ``SET`` form the canonical ops emit is supported (the current
    pointer flip and the draft ``lastPublishedAs``/``updatedAt`` write).
    """
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
    """Stateful AssetIndex stand-in keyed by ``(PK, SK)`` raw DDB items.

    Implements exactly the async surface ``publish_asset`` / ``promote_asset``
    / ``edit_asset`` invoke, and interprets the TransactWriteItems payloads
    the canonical ops build.
    """

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict[str, Any]] = {}
        self.table_name = "test-asset-index"

    # --- helpers -------------------------------------------------------
    def _scope_keys(
        self,
        asset: SpecialtyAgent,
        owner_user_id: Optional[str],
        owner_group_id: Optional[str],
        owner_org_id: Optional[str],
    ) -> tuple[str, str]:
        if asset.version is None:
            return (
                make_pk_user_owned(owner_user_id),
                make_sk_owned(asset.category, asset.asset_id),
            )
        sk = make_sk_version(asset.category, asset.asset_id, asset.version)
        if asset.scope == "private":
            return make_pk_user_versions(owner_user_id), sk
        if asset.scope == "group":
            return make_pk_group_shared(owner_group_id), sk
        return make_pk_org_shared(owner_org_id), sk

    def raw_item(self, pk: str, sk: str) -> Optional[dict[str, Any]]:
        item = self._items.get((pk, sk))
        return copy.deepcopy(item) if item is not None else None

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
        owner_group_id: Optional[str] = None,
        owner_org_id: Optional[str] = None,
    ) -> None:
        item = to_index_item(
            asset,
            owner_user_id=owner_user_id,
            owner_group_id=owner_group_id,
            owner_org_id=owner_org_id,
        )
        pk, sk = self._scope_keys(
            asset, owner_user_id, owner_group_id, owner_org_id
        )
        item["PK"] = pk
        item["SK"] = sk
        if (
            asset.category != "tool-collection"
            and asset.version is not None
        ):
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


class InMemoryS3:
    """S3UserFilesTool stand-in storing objects by absolute key."""

    def __init__(self) -> None:
        self.objects: dict[str, str] = {}

    def put_text(self, key: str, text: str) -> None:
        self.objects[key] = text

    def read_text(self, key: str) -> Optional[str]:
        return self.objects.get(key)

    async def copy_prefix_absolute(self, src: str, dst: str) -> None:
        for key in list(self.objects):
            if key.startswith(src):
                self.objects[dst + key[len(src):]] = self.objects[key]

    async def delete_prefix_absolute(self, prefix: str) -> None:
        for key in list(self.objects):
            if key.startswith(prefix):
                del self.objects[key]

    async def list_absolute(self, prefix: str) -> list[str]:
        # Reflect real stored contents so promote's empty-source guard
        # (BUG-2) sees the seeded draft artifacts.
        return [k for k in self.objects if k.startswith(prefix)]


# ---------------------------------------------------------------------------
# Config extraction / comparison helpers.
# ---------------------------------------------------------------------------


def _config_of(agent: SpecialtyAgent) -> dict[str, Any]:
    """Extract the payload-relevant Custom Agent config from a record."""
    return {
        "model": agent.model,
        "compatible_skills": list(agent.compatible_skills),
        "tool_collections": list(agent.tool_collections),
        "integration_config": agent.integration_config,
    }


def _seed_tool_collections(index, collection_ids: list[str]) -> None:
    """Seed one flat tool-collection row per id in the actor's OWNED partition.

    Deduplicated because the generated draft/edit configs overlap, and a repeated
    ``put`` of the same key is harmless but pointless.
    """
    now = int(time.time() * 1000)
    for collection_id in dict.fromkeys(collection_ids):
        _run(
            index.put(
                ToolCollection(
                    asset_id=collection_id,
                    category="tool-collection",
                    name=f"collection-{collection_id}",
                    status="draft",
                    scope="private",
                    s3_prefix=f"tool-collections/{collection_id}/",
                    created_at=now,
                    updated_at=now,
                    tool_refs=[
                        ToolRef(name=f"tool-for-{collection_id}", version="1.0")
                    ],
                ),
                owner_user_id=_ACTOR,
            )
        )


def _make_working_copy(cfg: dict[str, Any]) -> SpecialtyAgent:
    """Build a draft (version=None) SpecialtyAgent working copy from a config."""
    now = int(time.time() * 1000)
    return SpecialtyAgent(
        asset_id=_AGENT_ID,
        category=_CATEGORY,
        name="Custom Agent P10",
        status="draft",
        scope="private",
        s3_prefix=f"users/{_ACTOR}/drafts/specialty-agents/{_AGENT_ID}/",
        created_at=now,
        updated_at=now,
        version=None,
        current=False,
        compatible_skills=list(cfg["compatible_skills"]),
        model=cfg["model"],
        tool_collections=list(cfg["tool_collections"]),
        integration_config=cfg["integration_config"],
    )


def _draft_persona_key() -> str:
    return f"users/{_ACTOR}/drafts/specialty-agents/{_AGENT_ID}/{_PERSONA_FILE}"


# ---------------------------------------------------------------------------
# Strategies: a draft config plus a guaranteed-different mutated config, so
# every example is a genuine "publish then edit the draft" scenario.
# ---------------------------------------------------------------------------

_id_text = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"),
        whitelist_characters=".:-_",
    ),
    min_size=1,
    max_size=12,
)
_id_list = st.lists(_id_text, min_size=0, max_size=5, unique=True)
_persona_text = st.text(min_size=0, max_size=80)

_integration_configs = st.one_of(
    st.none(),
    st.builds(
        IntegrationConfig,
        enabled=st.booleans(),
        issuer=st.one_of(st.none(), st.text(min_size=1, max_size=40)),
        audience=st.one_of(st.none(), st.text(min_size=1, max_size=40)),
        allowed_client_ids=st.lists(_id_text, max_size=4),
    ),
)


def _mutate_integration(ic: Optional[IntegrationConfig]) -> IntegrationConfig:
    """Produce an IntegrationConfig guaranteed to differ from ``ic``."""
    if ic is None:
        return IntegrationConfig(
            enabled=True,
            issuer="https://mutated.example.com",
            audience="mutated-audience",
            allowed_client_ids=["mutated-client"],
        )
    # Flip enabled + append a sentinel client id: distinct dataclass value.
    return IntegrationConfig(
        enabled=not ic.enabled,
        issuer=ic.issuer,
        audience=ic.audience,
        allowed_client_ids=list(ic.allowed_client_ids) + ["mutated-sentinel"],
    )


@st.composite
def _draft_and_edit(draw):
    """Generate ``(draft_cfg, edit_cfg, draft_persona, edit_persona)``.

    ``edit_cfg`` differs from ``draft_cfg`` in every payload-relevant field so
    the immutability assertions are meaningful.
    """
    draft_persona = draw(_persona_text)
    draft_model = draw(st.one_of(st.none(), _id_text))
    draft_skills = draw(_id_list)
    draft_tcs = draw(_id_list)
    draft_ic = draw(_integration_configs)

    draft_cfg = {
        "model": draft_model,
        "compatible_skills": draft_skills,
        "tool_collections": draft_tcs,
        "integration_config": draft_ic,
    }
    edit_cfg = {
        "model": (
            "mutated-model"
            if draft_model is None
            else draft_model + "-edited"
        ),
        "compatible_skills": list(draft_skills) + ["edited-skill-sentinel"],
        "tool_collections": list(draft_tcs) + ["edited-tc-sentinel"],
        "integration_config": _mutate_integration(draft_ic),
    }
    edit_persona = draft_persona + "\n<<EDITED-DRAFT-SENTINEL>>"
    return draft_cfg, edit_cfg, draft_persona, edit_persona


# ---------------------------------------------------------------------------
# Property 10: Version integrity
# ---------------------------------------------------------------------------

# Feature: custom-agents, Property 10: Version integrity
@settings(max_examples=150, deadline=None)
@given(scenario=_draft_and_edit())
def test_property10_version_integrity(scenario):
    """Publish snapshots the full draft; later draft edits never mutate it.

    1. Publishing snapshots the FULL draft config into an immutable version
       (the published snapshot's extended fields equal the draft's).
    2. A subsequent draft edit (fork + mutate the working copy) does NOT
       mutate the existing published snapshot.
    3. Resolving the "current published" selection always returns the
       published config, never the (now-differing) draft.

    Validates: Requirements 5.2, 8.2, 8.4, 8.7
    """
    draft_cfg, edit_cfg, draft_persona, edit_persona = scenario

    index = InMemoryAssetIndex()
    s3 = InMemoryS3()
    ctx = LabRequestContext(
        actor_id=_ACTOR,
        session_id="sess-p10",
        request_id="req-p10",
    )

    # --- Seed the draft working copy + its persona.md -----------------------
    # Plus a flat row for every tool collection either config names: publish
    # resolves each selected collection to freeze its tool refs onto the version,
    # and refuses an id with no row (an id the owner cannot resolve would
    # otherwise be frozen into an immutable version as "no tools", forever).
    _seed_tool_collections(
        index,
        list(draft_cfg["tool_collections"]) + list(edit_cfg["tool_collections"]),
    )
    working = _make_working_copy(draft_cfg)
    _run(index.put(working, owner_user_id=_ACTOR))
    s3.put_text(_draft_persona_key(), draft_persona)

    # --- 1. Publish: snapshot the full draft config into an immutable version
    result = _run(
        publish_asset(
            ctx=ctx,
            asset_index=index,
            s3=s3,
            category=_CATEGORY,
            asset_id=_AGENT_ID,
            scope="private",
        )
    )
    assert result["version"] == 1

    lineage = _run(index.query_lineage(_AGENT_ID))
    current_versions = [r for r in lineage if r.current]
    assert len(current_versions) == 1, "exactly one current published version"
    published_v1 = current_versions[0]

    # The published snapshot captures the FULL draft config (Req 8.2).
    assert _config_of(published_v1) == draft_cfg
    assert published_v1.version == 1
    assert published_v1.status == "published"

    # Persona (S3 content) travelled with the version snapshot.
    published_persona_key = published_v1.s3_prefix + _PERSONA_FILE
    assert s3.read_text(published_persona_key) == draft_persona

    # Capture the immutable raw snapshot row for a later byte-for-byte check.
    published_pk = make_pk_user_versions(_ACTOR)
    published_sk = make_sk_version(_CATEGORY, _AGENT_ID, 1)
    snapshot_before = index.raw_item(published_pk, published_sk)
    assert snapshot_before is not None

    # --- 2. Edit the draft: fork the current version, then mutate it --------
    forked = _run(
        edit_asset(
            ctx=ctx,
            asset_index=index,
            s3=s3,
            category=_CATEGORY,
            asset_id=_AGENT_ID,
        )
    )
    # The fork carries the full published config forward (task 4.1 fix), so the
    # edit genuinely starts from the snapshot's configuration.
    assert _config_of(forked) == draft_cfg

    # Apply a differing draft edit: replace the working-copy row + its persona.
    edited_working = _make_working_copy(edit_cfg)
    _run(index.put(edited_working, owner_user_id=_ACTOR))
    s3.put_text(_draft_persona_key(), edit_persona)

    # --- 3. Re-resolve the current published version ------------------------
    lineage_after = _run(index.query_lineage(_AGENT_ID))
    current_after = [r for r in lineage_after if r.current]
    assert len(current_after) == 1, "still exactly one current published version"
    published_after = current_after[0]

    # Immutability: the published snapshot is unchanged by the draft edit.
    assert _config_of(published_after) == draft_cfg
    assert _config_of(published_after) != edit_cfg
    assert index.raw_item(published_pk, published_sk) == snapshot_before

    # The versioned persona.md is untouched by the draft-side rewrite.
    assert s3.read_text(published_persona_key) == draft_persona
    assert s3.read_text(published_persona_key) != edit_persona

    # Resolution returns the PUBLISHED config, never the (differing) draft.
    working_now = _run(
        index.get(
            make_pk_user_owned(_ACTOR),
            make_sk_owned(_CATEGORY, _AGENT_ID),
        )
    )
    assert working_now is not None
    assert _config_of(working_now) == edit_cfg
    assert _config_of(published_after) != _config_of(working_now)
