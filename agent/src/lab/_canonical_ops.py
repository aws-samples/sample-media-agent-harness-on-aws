"""Shared implementation of the canonical lifecycle operations.

The four category CRUD modules (``skill_crud``, ``specialty_crud``,
``workflow_crud``, ``tool_collection_crud``) all expose the same five
canonical operations — Promote, Share, Deprecate, Clone, Rollback — and
the implementation is identical except for the category constant and the
model class. To keep the CRUD layer a thin dispatch shell, the actual
operation bodies live here and are invoked from each CRUD with its
category constant.

Task 3.1 lands the Promote helper. Task 3.2 adds Share. Task 3.3 adds
Deprecate. Tasks 3.4–3.5 will follow with Clone and Rollback.

Per the design's "Promote (sequence)" diagram:

  1. Look up the draft via ``AssetIndex.get(USER#u#DRAFTS, <cat>#<id>#draft)``.
     Missing or owned by a different actor ⇒ ``NotFoundOrForbidden``.
  2. Determine the new ``(published_id, version)``:
       - new lineage:      uuid4(), version=1
       - existing lineage: max(query_lineage(published_id).version) + 1
  3. Render the target prefix from
     ``derive_record_prefix(scope, owner, category, published_id, version)``.
  4. ``S3UserFilesTool.copy_prefix_absolute(draft.s3_prefix → target_prefix)``.
  5. Build the new published :class:`AssetCommon` row with
     ``status="published"``, ``current=true``, ``scope_history`` initialized
     to one entry, ``promoted_from = draft_id``, and ``publish_policy``.
  6. ``AssetIndex.transact_write([Put new published, optionally Update
     prior current to current=false, Update draft.lastPublishedAs])``.
  7. On ``TransactionCanceledException``: run ``delete_prefix_absolute``
     to remove the orphan target prefix. Compensating-delete failure
     raises :class:`CompensationFailedError`. Otherwise raise
     :class:`ConflictError`.
  8. Return :class:`Promoted`.

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 2.8, 2.9, 2.10
Properties: 3 (exactly one current per lineage), 4 (Promote round-trip)
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from botocore.exceptions import ClientError

from .asset_index import (
    AssetIndex,
    make_gsi1_keys,
    make_pk_group_shared,
    make_pk_org_shared,
    make_pk_user_owned,
    make_pk_user_versions,
    make_sk_owned,
    make_sk_version,
)
from .context import LabRequestContext
from .errors import (
    AuthorityDenied,
    CompensationFailedError,
    ConflictError,
    NotFoundOrForbidden,
    ScopeNarrowingError,
)
from .models import (
    AssetCommon,
    PublishPolicy,
    Scope,
    ScopeHistoryEntry,
    Skill,
    SpecialtyAgent,
    ToolCollection,
    ToolCollectionSnapshot,
    Workflow,
    to_index_item,
)
from .operation_results import (
    AgentReferenceHit,
    Cloned,
    Deprecated,
    ExternalAccessSet,
    Promoted,
    Purged,
    PurgePreflight,
    RolledBack,
    Shared,
)
from .outbox import (
    EVENT_DEPRECATED,
    EVENT_PUBLISHED,
    EVENT_SCOPE_CHANGED,
    build_outbox_put,
    canonical_revision,
    registry_eligible,
    should_emit,
)
from .path_validator import derive_record_prefix, validate_record_prefix
from .s3_user_files_tool import S3UserFilesTool

logger = logging.getLogger(__name__)


# Categories that participate in the versioned Promote flow. Tool
# collections are flat (no version sub-tier) and follow a different
# Promote path that is out of scope for this task; the helper rejects
# them up-front so a caller's mistake fails loudly rather than silently
# producing a malformed row.
_VERSIONED_CATEGORIES: frozenset[str] = frozenset(
    {"skill", "specialty-agent", "workflow"}
)


def _now_iso() -> str:
    """Return the current UTC timestamp in ISO-8601 form (with ``Z``)."""
    # ``datetime.now(timezone.utc).isoformat()`` yields '+00:00'; we
    # replace it with 'Z' to match the design's "ISO-8601 timestamp"
    # convention used in the ScopeHistory examples.
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _now_ms() -> int:
    """Return the current epoch in milliseconds."""
    return int(time.time() * 1000)


async def _resolve_tool_collection_snapshots(
    *,
    asset_index: AssetIndex,
    actor_id: str,
    collection_ids: list[str],
) -> list[ToolCollectionSnapshot]:
    """Freeze the tool refs behind each of an agent's ``tool_collections`` ids.

    Called once per publish so the resulting version pins its tool set. Without
    this the published row carried bare ids that were re-resolved live at every
    invoke against the owner's **mutable** working-copy collection rows — so
    editing a draft collection silently changed the behaviour of every already
    published version (including historical ones restored by rollback) and
    silently widened the external allowlist, with no publish, review, or version
    bump. ``_build_published_asset``'s docstring promised the snapshot captured
    the FULL config; for tools it did not.

    Reads the same rows the invoke path used to read: the owner's flat
    ``USER#<actor>#OWNED`` partition. Tool collections have no published form to
    prefer (the category is absent from :data:`_VERSIONED_CATEGORIES`), so the
    working copy is the only source there has ever been.

    Raises rather than skipping an unresolvable id. The alternative is worse than
    the bug being fixed: omitting it would freeze the loss permanently into an
    immutable version, where today's live resolution at least recovers if the
    collection comes back. Refusing at publish time matches how this function's
    caller already treats an empty artifact prefix — surface it to the author now,
    not as a bare persona at invoke time.

    Args:
        asset_index: Single-table AssetIndex wrapper (read-only here).
        actor_id: The publishing owner, whose OWNED partition holds the rows.
        collection_ids: The draft's ``tool_collections``, in order.

    Returns:
        One :class:`ToolCollectionSnapshot` per id, order preserved. ``[]`` when
        the draft selected no collections — which pins "no tools", distinct from
        the ``None`` that means "not pinned".

    Raises:
        ValueError: An id resolves to no visible collection row.
    """
    snapshots: list[ToolCollectionSnapshot] = []
    owned_pk = make_pk_user_owned(actor_id)
    for collection_id in collection_ids:
        row = await asset_index.get(
            owned_pk, make_sk_owned("tool-collection", collection_id)
        )
        if row is None:
            raise ValueError(
                f"refusing to publish: tool collection {collection_id!r} is "
                f"selected by this agent but no longer exists for actor "
                f"{actor_id}. Remove it from the agent, or recreate the "
                "collection, then publish again."
            )
        snapshots.append(
            ToolCollectionSnapshot(
                asset_id=collection_id,
                name=getattr(row, "name", "") or "",
                # Copied, not referenced: the source row stays mutable.
                tool_refs=list(getattr(row, "tool_refs", []) or []),
            )
        )
    return snapshots


def _build_published_asset(
    *,
    draft: AssetCommon,
    published_id: str,
    version: int,
    scope: Scope,
    target_prefix: str,
    actor_id: str,
    publish_policy_owner_ids: list[str],
    group_ids: Optional[list[str]],
    timestamp_iso: str,
    timestamp_ms: int,
    tool_collection_snapshots: Optional[list[ToolCollectionSnapshot]] = None,
) -> AssetCommon:
    """Construct the new Published_Record from the source draft.

    Returns a typed :class:`AssetCommon` subclass matching ``draft``'s
    category — :class:`Skill`, :class:`SpecialtyAgent`, or
    :class:`Workflow`. The new row carries:

      - ``status = "published"``
      - ``current = True``
      - ``scope = scope``
      - ``s3_prefix = target_prefix``
      - ``version`` and ``publish_policy`` populated
      - ``scope_history`` initialized with the single first entry
        ``{from: None, to: scope, actor_id, timestamp, group_ids?}``
        (Requirement 4.4)
      - ``promoted_from = draft.asset_id`` (Requirement 2.8)

    Category-specific fields are copied from the draft so the published
    row carries the same compatible-skill / step / tool-ref payload as
    the draft did.
    """
    initial_entry = ScopeHistoryEntry(
        from_scope=None,
        to_scope=scope,
        actor_id=actor_id,
        timestamp=timestamp_iso,
        group_ids=list(group_ids) if scope == "group" and group_ids else None,
    )
    publish_policy = PublishPolicy(owner_ids=list(publish_policy_owner_ids))

    common_kwargs: dict[str, Any] = {
        "asset_id": published_id,
        "category": draft.category,
        "name": draft.name,
        # Carried explicitly: this dict is field-by-field, so a new
        # AssetCommon field is dropped unless named here.
        "description": draft.description,
        "status": "published",
        "scope": scope,
        "s3_prefix": target_prefix,
        "created_at": timestamp_ms,
        "updated_at": timestamp_ms,
        "version": version,
        "publish_policy": publish_policy,
        "scope_history": [initial_entry],
        "current": True,
        "promoted_from": draft.asset_id,
        # The draft is the one carrying ``last_published_as``; the
        # published row only carries ``promoted_from``.
        "last_published_as": None,
        "cloned_from": None,
    }

    if isinstance(draft, SpecialtyAgent):
        # Custom Agents (Req 8.2): the published snapshot must capture the
        # FULL draft config, not just ``compatible_skills``. Carry the
        # extended fields — ``model``, ``tool_collections``, and
        # ``integration_config`` — so authentication/integration settings
        # travel with (and stay immutable alongside) the version they were
        # published with. Rollback (see ``rollback_asset``) only flips the
        # ``current`` pointer between existing snapshot rows, so once these
        # fields are captured here they are automatically restored on
        # rollback (Req 8.6). ``model`` is an immutable str; the list is
        # copied; ``integration_config`` is carried by reference like
        # ``publish_policy`` (drafts are not mutated in place).
        return SpecialtyAgent(
            **common_kwargs,
            compatible_skills=list(draft.compatible_skills),
            model=draft.model,
            tool_collections=list(draft.tool_collections),
            # The ids alone are not a snapshot — they point at mutable working
            # copies. `_resolve_tool_collection_snapshots` froze the tool refs
            # behind them; carrying that here is what makes the version's tool
            # set immutable and survive the collection being edited or deleted.
            tool_collection_snapshots=tool_collection_snapshots,
            integration_config=draft.integration_config,
            # Req 16: carry the working copy's external-access gate onto the new
            # snapshot so publish never silently re-opens (or drops) revoked
            # access — a working copy revoked before publish stays revoked, and
            # the gate remains a per-row attribute the Owner can flip after
            # publish independently of the frozen integration_config.
            external_access_enabled=getattr(
                draft, "external_access_enabled", True
            ),
        )
    if isinstance(draft, Workflow):
        return Workflow(**common_kwargs, steps=list(draft.steps))
    if isinstance(draft, ToolCollection):
        return ToolCollection(**common_kwargs, tool_refs=list(draft.tool_refs))
    # Default → Skill (no extra fields).
    return Skill(**common_kwargs)


def _resolve_owner_keys(
    *,
    scope: Scope,
    actor_id: str,
    group_ids: Optional[list[str]],
    org_id: Optional[str],
) -> tuple[Optional[str], Optional[str], Optional[str], str]:
    """Resolve the (owner_user_id, owner_group_id, owner_org_id, target_pk)
    tuple for a Promote into the requested ``scope``.

    - ``private`` → owner is the Actor; PK = ``USER#<actor>#PUBLISHED``.
    - ``group``   → owner is the group at index 0 of ``group_ids`` (the
                    caller selects which group; for now the helper
                    requires exactly one group_id and uses it as the
                    target). PK = ``GROUP#<g>#PUBLISHED``.
    - ``org``     → owner is the supplied ``org_id`` (or, in a future
                    revision, ``ctx.org_id`` once that field lands on
                    :class:`LabRequestContext`). PK = ``ORG#<o>#PUBLISHED``.

    Raises :class:`ValueError` for malformed combinations so the caller
    can surface a 400 to the user.
    """
    if scope == "private":
        return actor_id, None, None, make_pk_user_versions(actor_id)
    if scope == "group":
        if not group_ids:
            raise ValueError(
                "Promote with scope='group' requires a non-empty group_ids list"
            )
        # Today, callers pass a single group_id when promoting to a group
        # scope. If multi-group fan-out is needed later, that becomes a
        # separate operation rather than a many-PK transaction.
        if len(group_ids) != 1:
            raise ValueError(
                "Promote with scope='group' requires exactly one group_id "
                "(multi-group promote is not supported in this helper)"
            )
        gid = group_ids[0]
        return None, gid, None, make_pk_group_shared(gid)
    if scope == "org":
        if not org_id:
            raise ValueError(
                "Promote with scope='org' requires org_id"
            )
        return None, None, org_id, make_pk_org_shared(org_id)
    raise ValueError(f"unknown scope: {scope!r}")


async def promote_asset(
    *,
    ctx: LabRequestContext,
    asset_index: AssetIndex,
    s3: S3UserFilesTool,
    category: str,
    draft_id: str,
    scope: Scope,
    group_ids: Optional[list[str]] = None,
    published_id: Optional[str] = None,
    org_id: Optional[str] = None,
    publish_policy_owner_ids: Optional[list[str]] = None,
) -> Promoted:
    """Promote a Draft into a new Published_Record.

    See the module docstring for the full sequence. This function is
    invoked from each category's ``CRUD.promote`` method with the
    appropriate ``category`` constant.

    Args:
        ctx: The request context. ``ctx.actor_id`` is used both to
            authorize the operation (the draft must be owned by this
            actor — Requirement 4.5) and as the default
            ``publishPolicy.ownerIds`` entry on the new published row.
        asset_index: Single-table AssetIndex wrapper (Task 2.1).
        s3: ``S3UserFilesTool`` instance. Only its ``copy_prefix_absolute``
            and ``delete_prefix_absolute`` methods are used; the actor
            scoping does not apply to cross-actor promotions.
        category: Singular AssetIndex category. Must be one of
            ``"skill"``, ``"specialty-agent"``, ``"workflow"``.
        draft_id: The ``draftId`` of the source draft.
        scope: Target visibility scope for the new published row.
        group_ids: For ``scope="group"``, the single target group id
            (in a one-element list). Ignored for other scopes; included
            in ``scopeHistory`` when ``scope="group"``.
        published_id: ``None`` to start a new lineage (a fresh uuid4 is
            generated and the new row becomes v1). Otherwise the lineage
            id of an existing published lineage; the new version is
            ``max(query_lineage(...).version) + 1``.
        org_id: Required when ``scope="org"``. Identifies the org owner.
        publish_policy_owner_ids: Optional override for the
            ``publishPolicy.ownerIds`` list on the new published row.
            Defaults to ``[ctx.actor_id]``.

    Returns:
        :class:`Promoted` with the new ``(published_id, version,
        s3_prefix)``.

    Raises:
        NotFoundOrForbidden: The draft does not exist or is not owned by
            ``ctx.actor_id``.
        ValueError: ``category`` is not a versioned category, or the
            ``scope`` / ``group_ids`` / ``org_id`` combination is
            malformed.
        ConflictError: The DynamoDB transaction cancelled (typically a
            concurrent Promote on the same lineage). The compensating
            ``delete_prefix_absolute`` was successful, so no orphan S3
            prefix remains.
        CompensationFailedError: The transaction cancelled AND the
            compensating ``delete_prefix_absolute`` itself failed,
            leaving an orphan S3 prefix that no AssetIndex row
            references. The caller is expected to log the orphan prefix
            and surface a 5xx; a janitor reconciles later.
    """
    if category not in _VERSIONED_CATEGORIES:
        raise ValueError(
            f"promote_asset only supports versioned categories "
            f"({sorted(_VERSIONED_CATEGORIES)}); got {category!r}"
        )

    # ------------------------------------------------------------------
    # 1. Look up the draft and verify ownership.
    # ------------------------------------------------------------------
    draft_pk = make_pk_user_owned(ctx.actor_id)
    draft_sk = make_sk_owned(category, draft_id)
    draft = await asset_index.get(draft_pk, draft_sk)
    if draft is None:
        # Either does not exist or belongs to a different user. Either
        # way we surface NotFoundOrForbidden so existence is not
        # disclosed (Requirement 4.5).
        raise NotFoundOrForbidden(
            f"draft {category}#{draft_id} not found for actor {ctx.actor_id}"
        )
    if draft.status != "draft":
        # Defensive: a non-draft row living under a drafts PK is a
        # platform bug, but we treat it the same as missing.
        raise NotFoundOrForbidden(
            f"row at {draft_pk}/{draft_sk} is not a draft"
        )

    # ------------------------------------------------------------------
    # 2. Determine published_id and version.
    # ------------------------------------------------------------------
    if published_id is None:
        new_published_id = str(uuid.uuid4())
        new_version = 1
        prior_current_row: Optional[AssetCommon] = None
    else:
        new_published_id = published_id
        lineage = await asset_index.query_lineage(published_id)
        if not lineage:
            # asset-lifecycle-redesign: the caller passes published_id ==
            # working_copy.asset_id so the working copy and its versions
            # share a lineage id. On first publish there are no existing
            # versions; we treat this as v1 of a new lineage anchored at
            # the supplied id.
            new_version = 1
            prior_current_row = None
        else:
            existing_versions = [
                r.version for r in lineage if r.version is not None
            ]
            new_version = max(existing_versions) + 1
            # The current pointer (if any) needs to be flipped to current=False
            # in the same transaction. There may be zero non-deprecated
            # rows currently marked current.
            prior_current_candidates = [r for r in lineage if r.current]
            prior_current_row = (
                prior_current_candidates[0] if prior_current_candidates else None
            )

    # ------------------------------------------------------------------
    # 3. Render target prefix and resolve owner keys.
    # ------------------------------------------------------------------
    owner_user_id, owner_group_id, owner_org_id, target_pk = (
        _resolve_owner_keys(
            scope=scope,
            actor_id=ctx.actor_id,
            group_ids=group_ids,
            org_id=org_id,
        )
    )
    target_prefix = derive_record_prefix(
        scope=scope,
        owner_user_id=owner_user_id,
        owner_group_id=owner_group_id,
        owner_org_id=owner_org_id,
        category=category,
        asset_id=new_published_id,
        version=new_version,
        status="published",
    )

    # ------------------------------------------------------------------
    # 4. Copy the draft artifact prefix to the target prefix.
    # S3 first so a failed transaction can compensate by deleting the
    # target; reversing the order would let consumers see an AssetIndex
    # row pointing at an empty prefix (per the design's failure-modes
    # section we explicitly do not reorder).
    #
    # Guard: publishing a draft whose artifact prefix lists ZERO objects
    # would mint an immutable published version pointing at nothing — the
    # exact failure that let a persona/SKILL.md path mismatch hide behind a
    # green publish (BUG-2). Fail loudly instead so the caller sees it at
    # publish time rather than as a bare persona at invoke time. Categories
    # whose artifacts legitimately live only in the row (none today) would
    # need an allowlist here; every current category writes at least one file.
    src_objects = await s3.list_absolute(draft.s3_prefix)
    if not src_objects:
        raise ValueError(
            f"refusing to publish {category}#{draft_id}: draft artifact prefix "
            f"'{draft.s3_prefix}' contains no objects (nothing to publish)"
        )
    await s3.copy_prefix_absolute(draft.s3_prefix, target_prefix)

    # ------------------------------------------------------------------
    # 5. Build the new published row.
    #
    # Custom Agents pin their tool collections here. Resolved AFTER the S3 copy
    # so the ordering matches the existing failure model: a raise leaves only an
    # orphan target prefix (which the transaction path already compensates for),
    # never an AssetIndex row referencing tools nobody verified.
    # ------------------------------------------------------------------
    timestamp_iso = _now_iso()
    timestamp_ms = _now_ms()
    tool_collection_snapshots: Optional[list[ToolCollectionSnapshot]] = None
    if isinstance(draft, SpecialtyAgent):
        try:
            tool_collection_snapshots = await _resolve_tool_collection_snapshots(
                asset_index=asset_index,
                actor_id=ctx.actor_id,
                collection_ids=list(draft.tool_collections),
            )
        except ValueError:
            # Compensate the copy we just made before surfacing the refusal, so a
            # rejected publish leaves nothing behind.
            try:
                await s3.delete_prefix_absolute(target_prefix)
            except Exception as cleanup_err:  # noqa: BLE001
                logger.warning(
                    "promote_asset: tool-collection snapshot failed and the "
                    "compensating delete of %s also failed: %r",
                    target_prefix,
                    cleanup_err,
                )
            raise
    new_published = _build_published_asset(
        draft=draft,
        tool_collection_snapshots=tool_collection_snapshots,
        published_id=new_published_id,
        version=new_version,
        scope=scope,
        target_prefix=target_prefix,
        actor_id=ctx.actor_id,
        publish_policy_owner_ids=(
            list(publish_policy_owner_ids)
            if publish_policy_owner_ids
            else [ctx.actor_id]
        ),
        group_ids=group_ids,
        timestamp_iso=timestamp_iso,
        timestamp_ms=timestamp_ms,
    )

    # ------------------------------------------------------------------
    # 6. Compose the TransactWriteItems payload.
    # ------------------------------------------------------------------
    # validate_record_prefix would normally run inside AssetIndex.put;
    # transact_write does not call it, so we invoke it directly here
    # before serialising. A mismatch would indicate a programming error
    # in the helper itself rather than user input.
    validate_record_prefix(
        new_published,
        owner_user_id=owner_user_id,
        owner_group_id=owner_group_id,
        owner_org_id=owner_org_id,
    )

    new_item = to_index_item(
        new_published,
        owner_user_id=owner_user_id,
        owner_group_id=owner_group_id,
        owner_org_id=owner_org_id,
    )
    new_item["PK"] = target_pk
    new_item["SK"] = make_sk_version(category, new_published_id, new_version)
    gsi1pk, gsi1sk = make_gsi1_keys(new_published_id, new_version)
    new_item["GSI1PK"] = gsi1pk
    new_item["GSI1SK"] = gsi1sk

    operations: list[dict[str, Any]] = [
        {
            "Put": {
                "TableName": asset_index.table_name,
                "Item": new_item,
                # Requirement 2.9 / Property 3: the new published row
                # must not collide with an existing row at the same PK,
                # SK; this also rules out a concurrent Promote landing
                # the same (publishedId, version) pair.
                "ConditionExpression": "attribute_not_exists(PK)",
            }
        }
    ]

    # If an existing lineage had a current pointer, flip it to false in
    # the same transaction with a precondition so we cannot demote a row
    # that is no longer current (lost-update protection).
    if prior_current_row is not None and prior_current_row.version is not None:
        prior_pk, prior_sk = _scope_keys_for_published(prior_current_row)
        operations.append(
            {
                "Update": {
                    "TableName": asset_index.table_name,
                    "Key": {"PK": prior_pk, "SK": prior_sk},
                    "UpdateExpression": "SET #current = :false",
                    "ConditionExpression": "#current = :true",
                    "ExpressionAttributeNames": {"#current": "current"},
                    "ExpressionAttributeValues": {
                        ":false": False,
                        ":true": True,
                    },
                }
            }
        )

    # Update the draft to record the published target. This both
    # satisfies Requirement 2.8 (lastPublishedAs round-trip) and
    # supports Property 4.
    operations.append(
        {
            "Update": {
                "TableName": asset_index.table_name,
                "Key": {"PK": draft_pk, "SK": draft_sk},
                "UpdateExpression": (
                    "SET lastPublishedAs = :ref, updatedAt = :now"
                ),
                "ExpressionAttributeValues": {
                    ":ref": {
                        "publishedId": new_published_id,
                        "version": new_version,
                    },
                    ":now": timestamp_ms,
                },
            }
        }
    )

    # Registry projection outbox (R7). Rides the SAME transaction as the
    # published row so the discovery catalog cannot diverge from canonical
    # state: either both land or neither does. Only org-scoped publishes are
    # harness-shared, and the projector re-checks that against freshly
    # reloaded canonical state before it writes anything.
    if registry_eligible(new_published.scope, category):
        operations.append(
            build_outbox_put(
                table_name=asset_index.table_name,
                event_type=EVENT_PUBLISHED,
                category=category,
                asset_id=new_published_id,
                version=new_version,
                scope=new_published.scope,
                canonical_revision=canonical_revision(new_item),
            )
        )

    # ------------------------------------------------------------------
    # 7. Execute the transaction; compensate on cancel.
    # ------------------------------------------------------------------
    try:
        await asset_index.transact_write(operations)
    except ClientError as err:
        code = err.response.get("Error", {}).get("Code", "")
        if code == "TransactionCanceledException":
            cancellation_reasons = err.response.get(
                "CancellationReasons", []
            )
            try:
                await s3.delete_prefix_absolute(target_prefix)
            except Exception as comp_err:  # noqa: BLE001
                logger.error(
                    "Promote compensating delete failed: orphan_prefix=%s "
                    "original=%r compensation=%r",
                    target_prefix,
                    err,
                    comp_err,
                )
                raise CompensationFailedError(
                    orphan_prefix=target_prefix,
                    original_error=err,
                    compensation_error=comp_err,
                ) from comp_err
            raise ConflictError(
                f"Promote of {category}#{draft_id} cancelled; retry",
                cancellation_reasons=cancellation_reasons,
            ) from err
        # Non-cancellation, non-availability errors bubble up. The
        # AssetIndex wrapper has already mapped availability issues to
        # ServiceUnavailableError, so we should not normally reach this
        # branch.
        raise

    logger.info(
        "Promote ok: actor=%s draft=%s/%s -> %s/v%d (scope=%s)",
        ctx.actor_id,
        category,
        draft_id,
        new_published_id,
        new_version,
        scope,
    )

    return Promoted(
        published_id=new_published_id,
        version=new_version,
        s3_prefix=target_prefix,
    )


# ---------------------------------------------------------------------------
# Share
# ---------------------------------------------------------------------------

# Strict scope ordering for Property 5 / Requirement 5.5: a Share is
# accepted only when ``_SCOPE_ORDER[new] > _SCOPE_ORDER[old]``. Equal
# scopes are rejected (no-op share is a validation error rather than a
# silent success); narrower scopes raise ``ScopeNarrowingError``.
_SCOPE_ORDER: dict[str, int] = {"private": 0, "group": 1, "org": 2}


def _build_shared_asset(
    *,
    source: AssetCommon,
    new_scope: Scope,
    target_prefix: str,
    actor_id: str,
    group_ids: Optional[list[str]],
    timestamp_iso: str,
    timestamp_ms: int,
) -> AssetCommon:
    """Construct the post-Share row from the source Published_Record.

    Share preserves the ``(published_id, version)`` identity, the
    ``publish_policy``, ``current``, ``promoted_from``, and the
    category-specific payload. It mutates only:

      - ``scope`` ⇒ ``new_scope``
      - ``s3_prefix`` ⇒ ``target_prefix``
      - ``scope_history`` ⇒ source.scope_history + [new entry]
      - ``updated_at`` ⇒ now

    Per Requirements 5.2 / 13.1 / 13.2, the appended ``scopeHistoryEntry``
    carries ``from = source.scope``, ``to = new_scope``, ``actor_id``,
    ISO-8601 ``timestamp``, and ``group_ids`` whenever
    ``new_scope == "group"``. Earlier entries are reproduced verbatim;
    Property 6 requires the history to be a strict prefix-extension.
    """
    new_entry = ScopeHistoryEntry(
        from_scope=source.scope,
        to_scope=new_scope,
        actor_id=actor_id,
        timestamp=timestamp_iso,
        group_ids=list(group_ids) if new_scope == "group" and group_ids else None,
    )
    new_history = list(source.scope_history) + [new_entry]

    common_kwargs: dict[str, Any] = {
        "asset_id": source.asset_id,
        "category": source.category,
        "name": source.name,
        "description": source.description,
        "status": source.status,
        "scope": new_scope,
        "s3_prefix": target_prefix,
        "created_at": source.created_at,
        "updated_at": timestamp_ms,
        "version": source.version,
        "deprecated": source.deprecated,
        # publish_policy is invariant under Share — authority travels
        # with the lineage row across partition keys.
        "publish_policy": source.publish_policy,
        "scope_history": new_history,
        "current": source.current,
        "promoted_from": source.promoted_from,
        "last_published_as": source.last_published_as,
        "cloned_from": source.cloned_from,
    }

    if isinstance(source, SpecialtyAgent):
        # Share preserves the (publishedId, version) identity while moving
        # the snapshot across scopes. Carry the extended Custom Agent config
        # so a scope-widen never silently drops the model/tool/integration
        # settings that were published with this version (Req 8.2 — the
        # settings travel with the version).
        return SpecialtyAgent(
            **common_kwargs,
            compatible_skills=list(source.compatible_skills),
            model=source.model,
            tool_collections=list(source.tool_collections),
            # Carried, and load-bearing here specifically: a shared version is
            # reachable by actors who cannot see the owner's private collection
            # rows at all, so dropping the pin would leave them with ids nothing
            # can resolve. Copied per-collection so the shared row does not alias
            # the source's list.
            tool_collection_snapshots=(
                None
                if source.tool_collection_snapshots is None
                else list(source.tool_collection_snapshots)
            ),
            integration_config=source.integration_config,
            # Req 16: preserve the external-access gate across a scope-widen so
            # sharing never resets a revoked agent back to enabled.
            external_access_enabled=getattr(
                source, "external_access_enabled", True
            ),
        )
    if isinstance(source, Workflow):
        return Workflow(**common_kwargs, steps=list(source.steps))
    if isinstance(source, ToolCollection):
        return ToolCollection(**common_kwargs, tool_refs=list(source.tool_refs))
    return Skill(**common_kwargs)


async def share_asset(
    *,
    ctx: LabRequestContext,
    asset_index: AssetIndex,
    s3: S3UserFilesTool,
    category: str,
    published_id: str,
    version: int,
    new_scope: Scope,
    group_ids: Optional[list[str]] = None,
    org_id: Optional[str] = None,
) -> Shared:
    """Widen the scope of an existing Published_Record.

    Implements the design's "Share (sequence)" diagram. Share preserves
    the ``(published_id, version)`` identity but moves the row across
    partition keys (e.g., ``USER#u#PUBLISHED`` → ``GROUP#g#PUBLISHED``)
    and rewrites its S3 prefix to match the new scope. Only the four
    versioned categories participate (Skill, SpecialtyAgent, Workflow);
    tool-collections are flat and remain private in this sprint.

    Sequence:

      1. Look up the source row via ``query_lineage(published_id)`` and
         find the matching ``version``. Missing ⇒ ``NotFoundOrForbidden``.
      2. Authz: ``ctx.actor_id`` must be in
         ``source.publish_policy.{owner_ids ∪ contributor_ids}``
         (Requirement 5.6, Property 8).
      3. Validate scope ordering: if
         ``_SCOPE_ORDER[new_scope] <= _SCOPE_ORDER[source.scope]`` the
         operation is rejected with :class:`ScopeNarrowingError` BEFORE
         any S3 or AssetIndex mutation (Requirement 5.5).
      4. If ``new_scope == "group"`` require non-empty ``group_ids``;
         if ``new_scope == "org"`` require ``org_id`` (Requirement 5.4).
      5. Resolve the target prefix via :func:`derive_record_prefix` and
         the target PK via :func:`_resolve_owner_keys`.
      6. **S3 first**: ``copy_prefix_absolute(source.s3_prefix →
         target_prefix)``. S3 precedes the AssetIndex transaction so a
         failed transaction can compensate by deleting the target.
      7. **TransactWriteItems**: Delete source row at
         ``(source_pk, source_sk)`` with condition
         ``scope = :old_scope`` (defense in depth against a concurrent
         Share); Put target row at ``(target_pk, target_sk)`` with
         condition ``attribute_not_exists(PK)``. Both rows share the
         same SK (same ``category#publishedId#version``); only the PK
         differs.
      8. On ``TransactionCanceledException``: run compensating
         ``delete_prefix_absolute(target_prefix)`` and raise
         :class:`ConflictError`. On compensation failure raise
         :class:`CompensationFailedError`.
      9. **On success**: post-transaction, run
         ``delete_prefix_absolute(source.s3_prefix)`` to reclaim the old
         prefix. If this fails, log the orphan source prefix but DO NOT
         fail the operation — AssetIndex is correct, consumers read
         from the new prefix, and the janitor cleans up the orphan
         (per the design's "Failure modes" section).

    Args:
        ctx: The request context. ``ctx.actor_id`` is the principal for
            both authz and the appended scopeHistory entry.
        asset_index: Single-table AssetIndex wrapper.
        s3: ``S3UserFilesTool`` instance (only ``copy_prefix_absolute``
            and ``delete_prefix_absolute`` are used; cross-actor moves
            cannot use the actor-scoped methods).
        category: Singular AssetIndex category. Must be one of
            ``"skill"``, ``"specialty-agent"``, ``"workflow"``.
        published_id: Lineage id of the row to widen.
        version: Specific version to widen. Share addresses one row at a
            time; widening a whole lineage is the caller's responsibility.
        new_scope: Target scope. Must be strictly greater than the
            source scope under ``private < group < org``.
        group_ids: Required when ``new_scope == "group"``; appended to
            the new ``scopeHistory`` entry. Single-element list is
            expected (matches Promote semantics).
        org_id: Required when ``new_scope == "org"``.

    Returns:
        :class:`Shared` with the unchanged ``published_id`` and
        ``version`` plus the new ``s3_prefix`` and ``scope``.

    Raises:
        ValueError: Category is not a versioned category, or
            scope/group_ids/org_id combination is malformed, or no
            source version matches.
        NotFoundOrForbidden: The source row does not exist.
        AuthorityDenied: ``ctx.actor_id`` is not in the source row's
            ``publishPolicy``.
        ScopeNarrowingError: The requested ``new_scope`` is not strictly
            wider than the source scope.
        ConflictError: The DynamoDB transaction cancelled (typically a
            concurrent Share or scope precondition mismatch). The
            compensating ``delete_prefix_absolute`` was successful.
        CompensationFailedError: The transaction cancelled AND the
            compensating delete itself failed; an orphan target prefix
            exists with no AssetIndex row.
    """
    if category not in _VERSIONED_CATEGORIES:
        raise ValueError(
            f"share_asset only supports versioned categories "
            f"({sorted(_VERSIONED_CATEGORIES)}); got {category!r}"
        )
    if not isinstance(version, int) or version <= 0:
        raise ValueError(
            f"version must be a positive int, got {version!r}"
        )

    # ------------------------------------------------------------------
    # 1. Look up the source row via the byPublishedId GSI.
    # ------------------------------------------------------------------
    lineage = await asset_index.query_lineage(published_id)
    source: Optional[AssetCommon] = next(
        (r for r in lineage if r.version == version), None
    )
    if source is None:
        raise NotFoundOrForbidden(
            f"published {category}#{published_id}@v{version} not found"
        )
    if source.category != category:
        # Defensive: GSI1 is keyed by publishedId only; if the lineage
        # somehow contains a row of a different category, fail loudly.
        raise NotFoundOrForbidden(
            f"category mismatch for {published_id}@v{version}: "
            f"expected {category!r}, got {source.category!r}"
        )

    # ------------------------------------------------------------------
    # 2. Authz against publishPolicy.{ownerIds ∪ contributorIds}.
    # Requirement 5.6, Property 8: visibility (scope) is independent of
    # authority (publish_policy); a reader is not a mutator.
    # ------------------------------------------------------------------
    pp = source.publish_policy
    authorized: set[str] = set()
    if pp is not None:
        authorized.update(pp.owner_ids)
        authorized.update(pp.contributor_ids)
    if ctx.actor_id not in authorized:
        raise AuthorityDenied(
            published_id=published_id,
            version=version,
            message=(
                f"actor {ctx.actor_id!r} not authorized to share "
                f"{category}#{published_id}@v{version}"
            ),
        )

    # ------------------------------------------------------------------
    # 3. Validate scope ordering BEFORE any mutation.
    # Equal scope (no-op) and narrower scope both raise
    # ScopeNarrowingError so the caller gets a single, consistent error
    # for any non-widening request.
    # ------------------------------------------------------------------
    if new_scope not in _SCOPE_ORDER:
        raise ValueError(f"unknown scope: {new_scope!r}")
    if _SCOPE_ORDER[new_scope] <= _SCOPE_ORDER[source.scope]:
        raise ScopeNarrowingError(
            from_scope=source.scope, to_scope=new_scope
        )

    # ------------------------------------------------------------------
    # 4. Resolve source PK (where the row currently lives) and target PK
    # (where it must move). Source owner is recovered from
    # ``source.s3_prefix`` since the typed record carries only ``scope``;
    # this avoids a second AssetIndex round-trip in the hot path and is
    # symmetric with how Promote flips a prior current row.
    # ------------------------------------------------------------------
    source_pk, source_sk = _scope_keys_for_published(source)

    target_owner_user_id, target_owner_group_id, target_owner_org_id, target_pk = (
        _resolve_owner_keys(
            scope=new_scope,
            actor_id=ctx.actor_id,
            group_ids=group_ids,
            org_id=org_id,
        )
    )
    target_sk = make_sk_version(category, published_id, version)
    target_prefix = derive_record_prefix(
        scope=new_scope,
        owner_user_id=target_owner_user_id,
        owner_group_id=target_owner_group_id,
        owner_org_id=target_owner_org_id,
        category=category,
        asset_id=published_id,
        version=version,
        status="published",
    )

    # ------------------------------------------------------------------
    # 5. S3 copy first. If this fails, AssetIndex is untouched and the
    # source row still resolves; the partial target prefix is cleaned
    # up by the S3UserFilesTool's own retry/cleanup pattern (see
    # design "Share: partial S3 copy"). We do not catch here so
    # ClientError surfaces to the caller.
    # ------------------------------------------------------------------
    await s3.copy_prefix_absolute(source.s3_prefix, target_prefix)

    # ------------------------------------------------------------------
    # 6. Build the new typed row and serialise to a DDB item.
    # ------------------------------------------------------------------
    timestamp_iso = _now_iso()
    timestamp_ms = _now_ms()
    new_record = _build_shared_asset(
        source=source,
        new_scope=new_scope,
        target_prefix=target_prefix,
        actor_id=ctx.actor_id,
        group_ids=group_ids,
        timestamp_iso=timestamp_iso,
        timestamp_ms=timestamp_ms,
    )

    validate_record_prefix(
        new_record,
        owner_user_id=target_owner_user_id,
        owner_group_id=target_owner_group_id,
        owner_org_id=target_owner_org_id,
    )

    new_item = to_index_item(
        new_record,
        owner_user_id=target_owner_user_id,
        owner_group_id=target_owner_group_id,
        owner_org_id=target_owner_org_id,
    )
    new_item["PK"] = target_pk
    new_item["SK"] = target_sk
    gsi1pk, gsi1sk = make_gsi1_keys(published_id, version)
    new_item["GSI1PK"] = gsi1pk
    new_item["GSI1SK"] = gsi1sk

    # ------------------------------------------------------------------
    # 7. TransactWriteItems: Delete source PK row + Put target PK row.
    # The Delete carries a ``scope = :old_scope`` precondition so a
    # concurrent Share that already moved the row to a different scope
    # cancels this transaction (defence in depth; the GSI1 query in
    # step 1 read the source row's scope a moment ago, but DDB has no
    # built-in optimistic-concurrency on the source).
    # The Put's ``attribute_not_exists(PK)`` guards against a bizarre
    # case where the target PK already has a row at this SK (which
    # would indicate a programming error or a torn prior Share).
    # ------------------------------------------------------------------
    operations: list[dict[str, Any]] = [
        {
            "Delete": {
                "TableName": asset_index.table_name,
                "Key": {"PK": source_pk, "SK": source_sk},
                "ConditionExpression": "#scope = :old_scope",
                "ExpressionAttributeNames": {"#scope": "scope"},
                "ExpressionAttributeValues": {":old_scope": source.scope},
            }
        },
        {
            "Put": {
                "TableName": asset_index.table_name,
                "Item": new_item,
                "ConditionExpression": "attribute_not_exists(PK)",
            }
        },
    ]

    # Registry projection outbox (R7). Emitted for BOTH scope directions: a
    # widening to org creates discoverability, and a narrowing away from org
    # must remove it. The projector decides which, from reloaded canonical
    # state — a writer-side decision here could act on a scope that has since
    # changed again (Property 2).
    if should_emit(
        category=category, old_scope=source.scope, new_scope=new_scope
    ):
        operations.append(
            build_outbox_put(
                table_name=asset_index.table_name,
                event_type=EVENT_SCOPE_CHANGED,
                category=category,
                asset_id=published_id,
                version=version,
                scope=new_scope,
                previous_scope=source.scope,
                canonical_revision=canonical_revision(new_item),
            )
        )

    try:
        await asset_index.transact_write(operations)
    except ClientError as err:
        code = err.response.get("Error", {}).get("Code", "")
        if code == "TransactionCanceledException":
            cancellation_reasons = err.response.get(
                "CancellationReasons", []
            )
            try:
                await s3.delete_prefix_absolute(target_prefix)
            except Exception as comp_err:  # noqa: BLE001
                logger.error(
                    "Share compensating delete failed: orphan_prefix=%s "
                    "original=%r compensation=%r",
                    target_prefix,
                    err,
                    comp_err,
                )
                raise CompensationFailedError(
                    orphan_prefix=target_prefix,
                    original_error=err,
                    compensation_error=comp_err,
                ) from comp_err
            raise ConflictError(
                f"Share of {category}#{published_id}@v{version} cancelled; "
                f"retry",
                cancellation_reasons=cancellation_reasons,
            ) from err
        raise

    # ------------------------------------------------------------------
    # 8. Post-transaction: delete the source prefix. AssetIndex is
    # already pointing at the target, so a failure here leaves an
    # orphan source prefix but does NOT compromise correctness. We log
    # and proceed — the janitor reconciles (per design "Failure modes →
    # Share: source delete fails after AssetIndex commit").
    # ------------------------------------------------------------------
    try:
        await s3.delete_prefix_absolute(source.s3_prefix)
    except Exception as src_err:  # noqa: BLE001
        logger.warning(
            "Share post-txn source delete failed; orphan source prefix "
            "left for janitor: orphan_source_prefix=%s actor=%s "
            "published=%s@v%d error=%r",
            source.s3_prefix,
            ctx.actor_id,
            published_id,
            version,
            src_err,
        )

    logger.info(
        "Share ok: actor=%s %s#%s@v%d scope=%s -> %s",
        ctx.actor_id,
        category,
        published_id,
        version,
        source.scope,
        new_scope,
    )

    return Shared(
        published_id=published_id,
        version=version,
        s3_prefix=target_prefix,
        scope=new_scope,
    )


# ---------------------------------------------------------------------------
# Deprecate
# ---------------------------------------------------------------------------


async def deprecate_asset(
    *,
    ctx: LabRequestContext,
    asset_index: AssetIndex,
    category: str,
    published_id: str,
    version: int,
) -> Deprecated:
    """Mark a published version as deprecated.

    Implements the design's "Deprecate" section. Deprecate is the only
    canonical operation that does NOT mutate S3: artifacts are retained
    so pinned references continue to resolve (Requirement 6.4). The
    operation is therefore a single ``TransactWriteItems`` against
    AssetIndex with no compensating-action path.

    Sequence:

      1. Load the full lineage via
         :meth:`AssetIndex.query_lineage`. If empty or the requested
         ``version`` is missing, raise :class:`NotFoundOrForbidden`
         (Requirement 6.1; "missing" is treated the same as "not yours"
         to avoid existence disclosure).
      2. Authz: ``ctx.actor_id`` must be in
         ``target.publish_policy.{owner_ids ∪ contributor_ids}``
         (Requirement 6.5, Property 8). Otherwise raise
         :class:`AuthorityDenied`.
      3. Build a ``TransactWriteItems`` payload:

         * **Update target row**: SET ``status = "deprecated"``,
           ``current = false``, ``updatedAt = now_ms``. PK/SK come from
           :func:`_scope_keys_for_published`.
         * **If target was current AND a non-deprecated other version
           remains**: append a second Update on the highest such other
           version setting ``current = true`` and bumping ``updatedAt``,
           with precondition ``current = :false`` so a concurrent
           Promote/Rollback that already flipped it cancels this
           transaction.
         * **If no non-deprecated version remains**: omit the second
           update; the lineage simply has no current (Requirement 6.3,
           Property 3 second clause).

      4. Call :meth:`AssetIndex.transact_write`. On
         ``TransactionCanceledException`` raise :class:`ConflictError`.
         No compensating action is needed because there are no S3
         mutations.

    Args:
        ctx: The request context. ``ctx.actor_id`` is the principal for
            the authority gate.
        asset_index: Single-table AssetIndex wrapper.
        category: Singular AssetIndex category. Must be one of
            ``"skill"``, ``"specialty-agent"``, ``"workflow"``.
        published_id: Lineage id of the version to deprecate.
        version: Specific positive-int version to deprecate.

    Returns:
        :class:`Deprecated` with the unchanged ``published_id`` and
        ``version`` plus the post-state ``new_current_version`` (the
        version now marked ``current=true``, or ``None`` when no
        non-deprecated version remains).

    Raises:
        ValueError: ``category`` is not a versioned category, or
            ``version`` is not a positive int.
        NotFoundOrForbidden: Lineage is empty or the requested version
            is not present.
        AuthorityDenied: ``ctx.actor_id`` is not in the target row's
            ``publish_policy``.
        ConflictError: The DynamoDB transaction cancelled (typically a
            concurrent mutation already moved the current pointer).
    """
    if category not in _VERSIONED_CATEGORIES:
        raise ValueError(
            f"deprecate_asset only supports versioned categories "
            f"({sorted(_VERSIONED_CATEGORIES)}); got {category!r}"
        )
    if not isinstance(version, int) or version <= 0:
        raise ValueError(
            f"version must be a positive int, got {version!r}"
        )

    # ------------------------------------------------------------------
    # 1. Load the lineage and locate the target version.
    # ------------------------------------------------------------------
    lineage = await asset_index.query_lineage(published_id)
    if not lineage:
        raise NotFoundOrForbidden(
            f"published lineage {published_id!r} has no rows"
        )
    target: Optional[AssetCommon] = next(
        (r for r in lineage if r.version == version), None
    )
    if target is None:
        raise NotFoundOrForbidden(
            f"published {category}#{published_id}@v{version} not found"
        )
    if target.category != category:
        # Defensive: GSI1 is keyed by publishedId only; surface a
        # category mismatch loudly.
        raise NotFoundOrForbidden(
            f"category mismatch for {published_id}@v{version}: "
            f"expected {category!r}, got {target.category!r}"
        )

    # ------------------------------------------------------------------
    # 2. Authz against publish_policy.{owner_ids ∪ contributor_ids}.
    # Requirement 6.5, Property 8: visibility (scope) is independent of
    # authority (publish_policy); a reader is not a mutator.
    # ------------------------------------------------------------------
    pp = target.publish_policy
    authorized: set[str] = set()
    if pp is not None:
        authorized.update(pp.owner_ids)
        authorized.update(pp.contributor_ids)
    if ctx.actor_id not in authorized:
        raise AuthorityDenied(
            published_id=published_id,
            version=version,
            message=(
                f"actor {ctx.actor_id!r} not authorized to deprecate "
                f"{category}#{published_id}@v{version}"
            ),
        )

    # ------------------------------------------------------------------
    # 3. Identify the next current pointer (if any).
    # If the target is current AND another non-deprecated version
    # remains, promote the highest such version to current=true in the
    # same transaction. Otherwise the lineage simply has no current
    # (Requirement 6.3 / Property 3).
    # ------------------------------------------------------------------
    next_current: Optional[AssetCommon] = None
    if target.current:
        candidates = [
            r
            for r in lineage
            if r.version is not None
            and r.version != target.version
            and r.status != "deprecated"
        ]
        if candidates:
            # Highest version wins (Requirement 6.2: "highest-numbered
            # remaining non-deprecated version").
            next_current = max(candidates, key=lambda r: r.version or 0)

    # ------------------------------------------------------------------
    # 4. Build the TransactWriteItems payload.
    # ------------------------------------------------------------------
    timestamp_ms = _now_ms()
    target_pk, target_sk = _scope_keys_for_published(target)

    operations: list[dict[str, Any]] = [
        {
            "Update": {
                "TableName": asset_index.table_name,
                "Key": {"PK": target_pk, "SK": target_sk},
                # ``deprecated`` is the stored truth; a legacy ``status``
                # attribute is dropped so nothing can contradict it.
                "UpdateExpression": (
                    "SET deprecated = :true, "
                    "#current = :false, "
                    "updatedAt = :now "
                    "REMOVE #status"
                ),
                "ExpressionAttributeNames": {
                    "#status": "status",
                    "#current": "current",
                },
                "ExpressionAttributeValues": {
                    ":true": True,
                    ":false": False,
                    ":now": timestamp_ms,
                },
            }
        }
    ]

    if next_current is not None and next_current.version is not None:
        nc_pk, nc_sk = _scope_keys_for_published(next_current)
        operations.append(
            {
                "Update": {
                    "TableName": asset_index.table_name,
                    "Key": {"PK": nc_pk, "SK": nc_sk},
                    "UpdateExpression": (
                        "SET #current = :true, updatedAt = :now"
                    ),
                    # Precondition guards against a concurrent
                    # Promote/Rollback that already flipped this row.
                    # If our read of the lineage was stale we cancel
                    # rather than double-promote.
                    "ConditionExpression": "#current = :false",
                    "ExpressionAttributeNames": {"#current": "current"},
                    "ExpressionAttributeValues": {
                        ":true": True,
                        ":false": False,
                        ":now": timestamp_ms,
                    },
                }
            }
        )

    # Registry projection outbox (R7). Deprecating an org-scoped version
    # deprecates its Registry revision — it never deletes it, so canonical and
    # projected history both stay auditable (R16). A version that was never
    # eligible has nothing to deprecate in the shared catalog.
    if registry_eligible(target.scope, category):
        operations.append(
            build_outbox_put(
                table_name=asset_index.table_name,
                event_type=EVENT_DEPRECATED,
                category=category,
                asset_id=published_id,
                version=version,
                scope=target.scope,
                # The version's immutable sort key. Combined with the event
                # type in the idempotency key, this keeps a Deprecated event
                # distinct from the CapabilityPublished event of the same
                # version while staying recomputable on replay.
                canonical_revision=target_sk,
            )
        )

    # ------------------------------------------------------------------
    # 5. Execute the transaction. No compensating action — there are no
    # S3 mutations to roll back.
    # ------------------------------------------------------------------
    try:
        await asset_index.transact_write(operations)
    except ClientError as err:
        code = err.response.get("Error", {}).get("Code", "")
        if code == "TransactionCanceledException":
            cancellation_reasons = err.response.get(
                "CancellationReasons", []
            )
            raise ConflictError(
                f"Deprecate of {category}#{published_id}@v{version} "
                f"cancelled; retry",
                cancellation_reasons=cancellation_reasons,
            ) from err
        raise

    # ------------------------------------------------------------------
    # 6. Compute new_current_version for the result.
    # If we promoted a successor, that's the new current. If the target
    # was current with no successors, the lineage has no current
    # (None). If the target was NOT current, the existing current
    # pointer is unchanged — echo its version.
    # ------------------------------------------------------------------
    new_current_version: Optional[int]
    if next_current is not None:
        new_current_version = next_current.version
    elif target.current:
        # Target was current and no successor exists → no current.
        new_current_version = None
    else:
        # Target was not current → existing current pointer unchanged.
        existing_current = next(
            (
                r
                for r in lineage
                if r.current
                and r.version is not None
                and r.version != target.version
            ),
            None,
        )
        new_current_version = (
            existing_current.version if existing_current is not None else None
        )

    logger.info(
        "Deprecate ok: actor=%s %s#%s@v%d new_current=%r",
        ctx.actor_id,
        category,
        published_id,
        version,
        new_current_version,
    )

    return Deprecated(
        published_id=published_id,
        version=version,
        new_current_version=new_current_version,
    )


# ---------------------------------------------------------------------------
# Clone
# ---------------------------------------------------------------------------


def _build_cloned_draft(
    *,
    source: AssetCommon,
    draft_id: str,
    target_prefix: str,
    timestamp_ms: int,
) -> AssetCommon:
    """Construct the new Draft row from the source Published_Record.

    Clone preserves the category-specific payload (``compatible_skills``
    for SpecialtyAgent, ``steps`` for Workflow, ``tool_refs`` for
    ToolCollection) so the cloned draft is a faithful working copy of
    the source. It mutates only the lifecycle / lineage fields:

      - ``asset_id`` ⇒ new ``draft_id``
      - ``status`` ⇒ ``"draft"``
      - ``scope`` ⇒ ``"private"`` (drafts are always private; Req 2.4)
      - ``s3_prefix`` ⇒ rendered draft prefix
      - ``created_at`` / ``updated_at`` ⇒ now
      - ``version`` ⇒ ``None`` (Req 2.7)
      - ``publish_policy`` ⇒ ``None`` (Req 2.4)
      - ``scope_history`` ⇒ ``[]`` (drafts have no history)
      - ``current`` ⇒ ``False``
      - ``promoted_from`` ⇒ ``None`` (Req 7.3)
      - ``last_published_as`` ⇒ ``None``
      - ``cloned_from`` ⇒ ``(source.publishedId, source.version)``
        (Req 7.3)

    The source's ``name`` is carried forward so the cloned draft is
    identifiable in the actor's draft list; the caller may rename via a
    subsequent ``update`` call.
    """
    if source.version is None:
        # Defensive: Clone targets a published row (versioned categories
        # only). A row with version=None should not reach here.
        raise ValueError(
            f"clone source must have a positive version, got {source.version!r}"
        )

    common_kwargs: dict[str, Any] = {
        "asset_id": draft_id,
        "category": source.category,
        "name": source.name,
        "description": source.description,
        "status": "draft",
        "scope": "private",
        "s3_prefix": target_prefix,
        "created_at": timestamp_ms,
        "updated_at": timestamp_ms,
        "version": None,
        "publish_policy": None,
        "scope_history": [],
        "current": False,
        "promoted_from": None,
        "last_published_as": None,
        "cloned_from": (source.asset_id, source.version),
    }

    if isinstance(source, SpecialtyAgent):
        # Clone produces a faithful working copy of the source Custom Agent.
        # Carry the extended config (model/tool_collections/integration_config)
        # so the cloned draft reproduces the source's full configuration; the
        # Owner can edit it afterwards via ``update``.
        return SpecialtyAgent(
            **common_kwargs,
            compatible_skills=list(source.compatible_skills),
            model=source.model,
            tool_collections=list(source.tool_collections),
            # Deliberately NOT carried, unlike every other field here. The pin
            # belongs to an immutable version; a working copy must track the
            # collections the Owner is currently editing, and the next publish
            # re-resolves it. Carrying the source's snapshot would freeze this
            # draft against collection contents the Owner can no longer see or
            # change.
            tool_collection_snapshots=None,
            integration_config=source.integration_config,
            # Req 16: carry the source's external-access gate onto the cloned
            # working copy so cloning a revoked agent does not silently produce
            # an enabled one; the Owner can re-enable on the clone explicitly.
            external_access_enabled=getattr(
                source, "external_access_enabled", True
            ),
        )
    if isinstance(source, Workflow):
        return Workflow(**common_kwargs, steps=list(source.steps))
    if isinstance(source, ToolCollection):
        return ToolCollection(**common_kwargs, tool_refs=list(source.tool_refs))
    return Skill(**common_kwargs)


async def clone_asset(
    *,
    ctx: LabRequestContext,
    asset_index: AssetIndex,
    s3: S3UserFilesTool,
    category: str,
    published_id: str,
    version: int,
) -> Cloned:
    """Clone a Published_Record into a new Draft owned by ``ctx.actor_id``.

    Implements the design's "Clone" section. Clone reads the source
    Published_Record directly (visibility is enforced by the Lab handler
    *before* this CRUD method is reached, per the design); generates a
    fresh ``draft_id``; copies the source's S3 prefix to a new draft
    prefix under the actor's drafts partition; and writes a single
    AssetIndex working-copy row (no ``version``),
    ``cloned_from=(publishedId, version)``, and ``promoted_from=None``.

    Sequence:

      1. Load the lineage via :meth:`AssetIndex.query_lineage` and
         locate the matching ``version``. Empty lineage or missing
         version ⇒ :class:`NotFoundOrForbidden` (Requirement 7.4 —
         visibility checks delegated to the handler, but we still treat
         a missing source row as not-found rather than disclosing
         existence).
      2. Generate ``draft_id = uuid4()`` and render
         ``target_prefix = users/<actor>/drafts/<cat>/<draft_id>/`` via
         :func:`derive_record_prefix`.
      3. **S3 first**: ``copy_prefix_absolute(source.s3_prefix →
         target_prefix)``. S3 precedes the AssetIndex put so a failed
         put can compensate by deleting the target (matches the Promote
         compensating-delete pattern).
      4. Build the new Draft row via :func:`_build_cloned_draft` and
         persist via :meth:`AssetIndex.put` with
         ``owner_user_id=ctx.actor_id``. Clone is a single-row insert,
         so :meth:`transact_write` is not used.
      5. **On put failure**: run compensating
         ``delete_prefix_absolute(target_prefix)``. If compensation
         itself fails, raise :class:`CompensationFailedError` carrying
         the orphan prefix; otherwise re-raise the original exception.

    Authority check: NONE. Clone does not require authority on the
    source row's ``publish_policy`` — the result is a *new* Draft owned
    by ``ctx.actor_id``, not a mutation of the source. This mirrors the
    design's "Clone" sequence, which omits the authority gate that
    Share / Deprecate / Rollback enforce.

    Args:
        ctx: The request context. ``ctx.actor_id`` is the owner of the
            new draft.
        asset_index: Single-table AssetIndex wrapper.
        s3: ``S3UserFilesTool`` instance. Only ``copy_prefix_absolute``
            and ``delete_prefix_absolute`` are used; cross-actor copies
            cannot use the actor-scoped methods.
        category: Singular AssetIndex category. Must be one of
            ``"skill"``, ``"specialty-agent"``, ``"workflow"``.
            Tool-collections are flat and out of scope; their CRUD
            wrapper raises :class:`NotImplementedError` directly without
            calling this helper.
        published_id: Lineage id of the source Published_Record.
        version: Specific positive-int version of the source to clone.

    Returns:
        :class:`Cloned` with the new ``draft_id``, the rendered
        ``s3_prefix`` of the draft, and a ``cloned_from`` tuple
        ``(published_id, version)`` pointing at the source.

    Raises:
        ValueError: ``category`` is not a versioned category, or
            ``version`` is not a positive int.
        NotFoundOrForbidden: The source lineage is empty or the
            requested version is missing.
        CompensationFailedError: The AssetIndex put failed AND the
            compensating ``delete_prefix_absolute`` itself failed,
            leaving an orphan target prefix with no AssetIndex row.
    """
    if category not in _VERSIONED_CATEGORIES:
        raise ValueError(
            f"clone_asset only supports versioned categories "
            f"({sorted(_VERSIONED_CATEGORIES)}); got {category!r}"
        )
    if not isinstance(version, int) or version <= 0:
        raise ValueError(
            f"version must be a positive int, got {version!r}"
        )

    # ------------------------------------------------------------------
    # 1. Load the source lineage and locate the requested version.
    # Visibility (Requirement 9) is enforced by the Lab handler before
    # this CRUD method is reached; the CRUD reads the source row
    # directly. A missing source is treated as NotFoundOrForbidden so
    # we never disclose existence (Requirement 7.4).
    # ------------------------------------------------------------------
    lineage = await asset_index.query_lineage(published_id)
    if not lineage:
        raise NotFoundOrForbidden(
            f"published lineage {published_id!r} has no rows"
        )
    source: Optional[AssetCommon] = next(
        (r for r in lineage if r.version == version), None
    )
    if source is None:
        raise NotFoundOrForbidden(
            f"published {category}#{published_id}@v{version} not found"
        )
    if source.category != category:
        # Defensive: GSI1 is keyed by publishedId only; surface a
        # category mismatch loudly.
        raise NotFoundOrForbidden(
            f"category mismatch for {published_id}@v{version}: "
            f"expected {category!r}, got {source.category!r}"
        )

    # ------------------------------------------------------------------
    # 2. Generate a fresh draft_id and render the target prefix.
    # ------------------------------------------------------------------
    draft_id = str(uuid.uuid4())
    target_prefix = derive_record_prefix(
        scope="private",
        owner_user_id=ctx.actor_id,
        owner_group_id=None,
        owner_org_id=None,
        category=category,
        asset_id=draft_id,
        version=None,
        status="draft",
    )

    # ------------------------------------------------------------------
    # 3. S3 copy first. If this fails, AssetIndex is untouched and the
    # source row still resolves; no compensating action is needed.
    # ------------------------------------------------------------------
    await s3.copy_prefix_absolute(source.s3_prefix, target_prefix)

    # ------------------------------------------------------------------
    # 4. Build the new draft row and persist via AssetIndex.put.
    # Clone is a single-row insert; transact_write is not used.
    # ------------------------------------------------------------------
    timestamp_ms = _now_ms()
    new_draft = _build_cloned_draft(
        source=source,
        draft_id=draft_id,
        target_prefix=target_prefix,
        timestamp_ms=timestamp_ms,
    )

    try:
        await asset_index.put(new_draft, owner_user_id=ctx.actor_id)
    except Exception as err:  # noqa: BLE001
        # Any put failure (ConditionalCheckFailed, ServiceUnavailable,
        # ClientError, ...) requires us to compensate by deleting the
        # target prefix we just copied. We catch broadly because S3
        # has already moved bytes; the AssetIndex row is the only
        # remaining state and we must not leave an orphan prefix.
        try:
            await s3.delete_prefix_absolute(target_prefix)
        except Exception as comp_err:  # noqa: BLE001
            logger.error(
                "Clone compensating delete failed: orphan_prefix=%s "
                "original=%r compensation=%r",
                target_prefix,
                err,
                comp_err,
            )
            raise CompensationFailedError(
                orphan_prefix=target_prefix,
                original_error=err,
                compensation_error=comp_err,
            ) from comp_err
        raise

    logger.info(
        "Clone ok: actor=%s %s#%s@v%d -> draft=%s",
        ctx.actor_id,
        category,
        published_id,
        version,
        draft_id,
    )

    return Cloned(
        draft_id=draft_id,
        s3_prefix=target_prefix,
        cloned_from=(published_id, version),
    )


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


async def rollback_asset(
    *,
    ctx: LabRequestContext,
    asset_index: AssetIndex,
    category: str,
    published_id: str,
    to_version: Optional[int] = None,
) -> RolledBack:
    """Move the ``current`` pointer of a lineage to a prior version.

    Implements the design's "Rollback" section. Rollback NEVER mutates
    ``status`` (Requirement 8.3) and never touches S3 — artifacts are
    retained so any pinned references continue to resolve. The
    operation is therefore a single ``TransactWriteItems`` against
    AssetIndex with no compensating-action path.

    Custom Agents (Requirement 8.6): rollback only flips the ``current``
    pointer between existing immutable snapshot rows; it does NOT
    reconstruct the row or its category-specific payload. Because
    ``_build_published_asset`` captures the FULL Custom Agent config
    (``model``, ``tool_collections``, ``integration_config``) into each
    snapshot at publish time, restoring an earlier version's config on
    rollback is automatic — the target snapshot already carries the
    extended integration settings it was published with.

    Sequence:

      1. Load the lineage via :meth:`AssetIndex.query_lineage`. If
         empty, raise :class:`NotFoundOrForbidden` (Requirement 8.4 —
         missing lineage is treated the same as not-yours to avoid
         existence disclosure).
      2. Locate the current row (where ``current=true``). If no current
         row exists, raise :class:`NotFoundOrForbidden`: a lineage with
         no current pointer cannot be "rolled back" to anything.
      3. Authz: ``ctx.actor_id`` must be in
         ``current_row.publish_policy.{owner_ids ∪ contributor_ids}``
         (Requirement 8.5, Property 8). Otherwise raise
         :class:`AuthorityDenied` BEFORE any mutation.
      4. Resolve the rollback target:

         * ``to_version is None`` → select
           ``max(v.version for v in lineage if v.version <
           current.version and v.status != "deprecated")``. If no such
           version exists, raise :class:`NotFoundOrForbidden`
           (Requirement 8.1).
         * ``to_version`` supplied → find that version in the lineage.
           Missing ⇒ :class:`NotFoundOrForbidden`. Deprecated ⇒
           :class:`ValueError` (Requirement 8.4 explicitly: deprecated
           targets are a validation error).

      5. Reject a no-op (target == current). Rolling back to the
         already-current version is a validation error rather than a
         silent success.
      6. Build a ``TransactWriteItems`` payload:

         * **Update target row**: SET ``current=true``,
           ``updatedAt=now_ms`` with precondition ``current=:false``
           (lost-update protection against a concurrent
           Promote/Rollback that already flipped it).
         * **Update prior current row**: SET ``current=false``,
           ``updatedAt=now_ms`` with precondition ``current=:true``
           (lost-update protection against a concurrent Deprecate that
           already cleared it).

         Neither Update touches ``status`` (Requirement 8.3).

      7. Call :meth:`AssetIndex.transact_write`. On
         ``TransactionCanceledException`` raise :class:`ConflictError`.
         No compensating action is needed — there are no S3 mutations.

    Args:
        ctx: The request context. ``ctx.actor_id`` is the principal for
            the authority gate.
        asset_index: Single-table AssetIndex wrapper.
        category: Singular AssetIndex category. Must be one of
            ``"skill"``, ``"specialty-agent"``, ``"workflow"``.
            Tool-collections are flat and out of scope; their CRUD
            wrapper raises :class:`NotImplementedError` directly without
            calling this helper.
        published_id: Lineage id whose current pointer should be moved.
        to_version: Optional explicit target version. When ``None`` the
            highest non-deprecated version strictly less than the
            current version is selected automatically.

    Returns:
        :class:`RolledBack` with the unchanged ``published_id`` plus
        the ``new_current_version`` (the resolved target) and
        ``prior_current_version`` (the version that was current
        before).

    Raises:
        ValueError: ``category`` is not a versioned category, or the
            requested ``to_version`` is deprecated, or the rollback
            target equals the current version.
        NotFoundOrForbidden: Lineage is empty, has no current row, the
            requested ``to_version`` is not present, or no prior
            non-deprecated version is available for an implicit
            rollback.
        AuthorityDenied: ``ctx.actor_id`` is not in the current row's
            ``publish_policy``.
        ConflictError: The DynamoDB transaction cancelled (typically a
            concurrent mutation already moved the current pointer).
    """
    if category not in _VERSIONED_CATEGORIES:
        raise ValueError(
            f"rollback_asset only supports versioned categories "
            f"({sorted(_VERSIONED_CATEGORIES)}); got {category!r}"
        )
    if to_version is not None and (
        not isinstance(to_version, int) or to_version <= 0
    ):
        raise ValueError(
            f"to_version must be a positive int or None, got {to_version!r}"
        )

    # ------------------------------------------------------------------
    # 1. Load the lineage.
    # ------------------------------------------------------------------
    lineage = await asset_index.query_lineage(published_id)
    if not lineage:
        raise NotFoundOrForbidden(
            f"published lineage {published_id!r} has no rows"
        )

    # ------------------------------------------------------------------
    # 2. Locate the current row.
    # ------------------------------------------------------------------
    current_candidates = [
        r for r in lineage if r.current and r.version is not None
    ]
    if not current_candidates:
        # A lineage with no current pointer cannot be rolled back —
        # there is nothing to flip away from. Treat this the same as a
        # missing lineage to avoid existence disclosure across tenants.
        raise NotFoundOrForbidden(
            f"lineage {published_id!r} has no current pointer to roll back"
        )
    # Property 3 invariant: at most one current row per lineage. If the
    # store somehow produced multiple, the highest version wins (this
    # would only happen under a torn write that the conditions on
    # Promote/Deprecate normally prevent).
    current_row: AssetCommon = max(
        current_candidates, key=lambda r: r.version or 0
    )
    if current_row.category != category:
        raise NotFoundOrForbidden(
            f"category mismatch for {published_id} current row: "
            f"expected {category!r}, got {current_row.category!r}"
        )

    # ------------------------------------------------------------------
    # 3. Authz against the current row's publish_policy.
    # Requirement 8.5, Property 8: visibility (scope) is independent of
    # authority (publish_policy). The design's Rollback section
    # explicitly validates against the current row.
    # ------------------------------------------------------------------
    pp = current_row.publish_policy
    authorized: set[str] = set()
    if pp is not None:
        authorized.update(pp.owner_ids)
        authorized.update(pp.contributor_ids)
    if ctx.actor_id not in authorized:
        raise AuthorityDenied(
            published_id=published_id,
            version=current_row.version,
            message=(
                f"actor {ctx.actor_id!r} not authorized to roll back "
                f"{category}#{published_id} (current v{current_row.version})"
            ),
        )

    # ------------------------------------------------------------------
    # 4. Resolve the rollback target.
    # ------------------------------------------------------------------
    target: Optional[AssetCommon]
    current_version = current_row.version  # narrowed: not None per step 2.
    assert current_version is not None
    if to_version is None:
        # Implicit: highest non-deprecated v strictly less than current.
        prior_candidates = [
            r
            for r in lineage
            if r.version is not None
            and r.version < current_version
            and r.status != "deprecated"
        ]
        if not prior_candidates:
            raise NotFoundOrForbidden(
                f"no prior non-deprecated version available for rollback "
                f"of {category}#{published_id} (current v{current_version})"
            )
        target = max(prior_candidates, key=lambda r: r.version or 0)
    else:
        target = next(
            (r for r in lineage if r.version == to_version), None
        )
        if target is None:
            raise NotFoundOrForbidden(
                f"published {category}#{published_id}@v{to_version} not found"
            )
        if target.status == "deprecated":
            # Requirement 8.4: explicit rollback to a deprecated
            # version is a validation error, distinct from a missing
            # version (which is NotFoundOrForbidden).
            raise ValueError(
                f"cannot rollback to a deprecated version: "
                f"{category}#{published_id}@v{to_version}"
            )
        if target.category != category:
            raise NotFoundOrForbidden(
                f"category mismatch for {published_id}@v{to_version}: "
                f"expected {category!r}, got {target.category!r}"
            )

    # ------------------------------------------------------------------
    # 5. Reject a no-op (target equals current). The atomic two-row
    # toggle would either fail its precondition or leave the lineage
    # in a self-inconsistent state; surfacing this as a validation
    # error matches Share's "no-op is rejected" stance.
    # ------------------------------------------------------------------
    if target.version == current_version:
        raise ValueError(
            f"rollback target equals current version: "
            f"{category}#{published_id}@v{current_version}"
        )

    # ------------------------------------------------------------------
    # 6. Build the TransactWriteItems payload.
    # Two Updates: target → current=true, prior → current=false. Each
    # carries a precondition on the row's prior current value to guard
    # against concurrent Promote / Deprecate / Rollback racing on the
    # same lineage. Status is NEVER touched.
    # ------------------------------------------------------------------
    timestamp_ms = _now_ms()
    target_pk, target_sk = _scope_keys_for_published(target)
    prior_pk, prior_sk = _scope_keys_for_published(current_row)

    operations: list[dict[str, Any]] = [
        {
            "Update": {
                "TableName": asset_index.table_name,
                "Key": {"PK": target_pk, "SK": target_sk},
                "UpdateExpression": "SET #current = :true, updatedAt = :now",
                "ConditionExpression": "#current = :false",
                "ExpressionAttributeNames": {"#current": "current"},
                "ExpressionAttributeValues": {
                    ":true": True,
                    ":false": False,
                    ":now": timestamp_ms,
                },
            }
        },
        {
            "Update": {
                "TableName": asset_index.table_name,
                "Key": {"PK": prior_pk, "SK": prior_sk},
                "UpdateExpression": "SET #current = :false, updatedAt = :now",
                "ConditionExpression": "#current = :true",
                "ExpressionAttributeNames": {"#current": "current"},
                "ExpressionAttributeValues": {
                    ":false": False,
                    ":true": True,
                    ":now": timestamp_ms,
                },
            }
        },
    ]

    # ------------------------------------------------------------------
    # 7. Execute the transaction. No compensating action — there are no
    # S3 mutations to roll back.
    # ------------------------------------------------------------------
    try:
        await asset_index.transact_write(operations)
    except ClientError as err:
        code = err.response.get("Error", {}).get("Code", "")
        if code == "TransactionCanceledException":
            cancellation_reasons = err.response.get(
                "CancellationReasons", []
            )
            raise ConflictError(
                f"Rollback of {category}#{published_id} from "
                f"v{current_version} to v{target.version} cancelled; retry",
                cancellation_reasons=cancellation_reasons,
            ) from err
        raise

    logger.info(
        "Rollback ok: actor=%s %s#%s v%d -> v%d",
        ctx.actor_id,
        category,
        published_id,
        current_version,
        target.version,
    )

    return RolledBack(
        published_id=published_id,
        new_current_version=target.version,
        prior_current_version=current_version,
    )


async def set_external_access_asset(
    *,
    ctx: LabRequestContext,
    asset_index: AssetIndex,
    category: str,
    published_id: str,
    enabled: bool,
) -> ExternalAccessSet:
    """Toggle the Req 16 external-access gate for an agent lineage.

    This is the Owner's revoke/re-enable control. It writes
    ``externalAccessEnabled`` onto EVERY row of the agent's lineage — the
    working copy (if one exists) AND every immutable version snapshot — in a
    single ``TransactWriteItems`` so the gate behaves as a per-lineage
    revocation record rather than per-snapshot state.

    Why write every row (Property 16 / Req 16.2, 16.3): rollback
    (:func:`rollback_asset`) only flips the ``current`` pointer between
    existing snapshot rows and NEVER touches ``externalAccessEnabled``. If the
    gate were set on just the current row, rolling back to an older snapshot
    (published while access was still enabled) would silently re-grant revoked
    access. By stamping the flag uniformly across the whole version history,
    whichever snapshot rollback makes current continues to read the revoked
    state until the Owner explicitly re-enables. The flag lives OUTSIDE each
    snapshot's immutable ``integration_config``, so this is consistent with
    keeping the published config frozen.

    Effect is immediate and independent of publishing (Req 16.1): the proxy
    resolves the gate at request time off the resolved current row.

    Only ``specialty-agent`` lineages carry an external-access gate (external
    invocation is a Custom Agent concept); other categories are rejected.

    Args:
        ctx: The request context. ``ctx.actor_id`` authorizes the toggle.
        asset_index: Single-table AssetIndex wrapper.
        category: Must be ``"specialty-agent"``.
        published_id: Lineage id (== working-copy asset_id) of the agent.
        enabled: ``False`` revokes external access; ``True`` re-enables it.

    Returns:
        :class:`ExternalAccessSet` echoing the lineage id, the new
        ``enabled`` state, and how many rows were updated.

    Raises:
        ValueError: ``category`` is not ``"specialty-agent"``.
        NotFoundOrForbidden: No working copy and no lineage rows exist for the
            actor, or the actor is not authorized on the published lineage.
        ConflictError: The DynamoDB transaction cancelled (a concurrent
            mutation changed a row); the caller may retry.
    """
    if category != "specialty-agent":
        raise ValueError(
            "set_external_access_asset only supports the 'specialty-agent' "
            f"category; got {category!r}"
        )

    # Gather every addressable row for this lineage: the working copy (owned
    # partition, keyed to the actor) plus all published version snapshots.
    owned_pk = make_pk_user_owned(ctx.actor_id)
    owned_sk = make_sk_owned(category, published_id)
    working = await asset_index.get(owned_pk, owned_sk)
    lineage = await asset_index.query_lineage(published_id)

    if working is None and not lineage:
        # Neither a working copy the actor owns nor any published version —
        # treat as not-found to avoid existence disclosure across tenants.
        raise NotFoundOrForbidden(
            f"no {category} lineage {published_id!r} for actor {ctx.actor_id}"
        )

    # Authorization. A working copy under the actor's OWNED partition implies
    # ownership. For published rows, mirror the rollback/deprecate authority
    # gate: the actor must appear in a version's publish_policy (owner or
    # contributor). This keeps visibility (scope) independent of authority.
    authorized = working is not None
    if not authorized:
        allowed: set[str] = set()
        for row in lineage:
            pp = row.publish_policy
            if pp is not None:
                allowed.update(pp.owner_ids)
                allowed.update(pp.contributor_ids)
        authorized = ctx.actor_id in allowed
    if not authorized:
        raise NotFoundOrForbidden(
            f"actor {ctx.actor_id!r} not authorized to change external access "
            f"for {category}#{published_id}"
        )

    # Build one Update per addressable row. Each carries an
    # ``attribute_exists(PK)`` precondition so a row vanishing mid-flight
    # cancels the transaction rather than resurrecting it.
    timestamp_ms = _now_ms()
    keyed_rows: list[tuple[str, str]] = []
    if working is not None:
        keyed_rows.append((owned_pk, owned_sk))
    for row in lineage:
        if row.version is None:
            # Defensive: a working copy should not appear in the lineage GSI,
            # but skip it if it does to avoid a duplicate key in the txn.
            continue
        keyed_rows.append(_scope_keys_for_published(row))

    operations: list[dict[str, Any]] = [
        {
            "Update": {
                "TableName": asset_index.table_name,
                "Key": {"PK": pk, "SK": sk},
                "UpdateExpression": (
                    "SET externalAccessEnabled = :enabled, updatedAt = :now"
                ),
                "ConditionExpression": "attribute_exists(PK)",
                "ExpressionAttributeValues": {
                    ":enabled": bool(enabled),
                    ":now": timestamp_ms,
                },
            }
        }
        for (pk, sk) in keyed_rows
    ]

    try:
        await asset_index.transact_write(operations)
    except ClientError as err:
        code = err.response.get("Error", {}).get("Code", "")
        if code == "TransactionCanceledException":
            cancellation_reasons = err.response.get("CancellationReasons", [])
            raise ConflictError(
                f"Setting external access for {category}#{published_id} "
                f"cancelled; retry",
                cancellation_reasons=cancellation_reasons,
            ) from err
        raise

    logger.info(
        "External access %s: actor=%s %s#%s rows=%d",
        "enabled" if enabled else "revoked",
        ctx.actor_id,
        category,
        published_id,
        len(keyed_rows),
    )

    return ExternalAccessSet(
        published_id=published_id,
        enabled=bool(enabled),
        rows_updated=len(keyed_rows),
    )


# ---------------------------------------------------------------------------
# Purge (hard delete of a published lineage)
# ---------------------------------------------------------------------------

#: Purge destroys immutable published state, so a category belongs here only
#: once :func:`lab.purge_scan.scan_references` can say what still points at it —
#: otherwise the pre-flight reports an empty blast radius for an asset other
#: things depend on, which is worse than refusing.
#:
#: * ``specialty-agent`` — workflow steps are scanned.
#: * ``skill`` — agents' ``compatible_skills`` are scanned.
#:
#: ``workflow``, ``tool-collection`` and ``hook`` stay out for the original
#: reason: nothing scans for references to them yet. Adding one here without
#: adding its scan raises from ``scan_references`` rather than shipping a
#: confident "nothing references this".
_PURGEABLE_CATEGORIES: frozenset[str] = frozenset({"skill", "specialty-agent"})


def _authorize_purge(
    ctx: LabRequestContext, category: str, published_id: str, rows: list[AssetCommon]
) -> None:
    """Require purge authority on EVERY version, or raise.

    Deliberately per-row rather than a union across the lineage. Being a
    contributor on v3 must not license destroying v1 — the check
    :func:`deprecate_asset` applies to one target version is applied here to
    each version the purge would remove.

    ``publish_policy is None`` yields an empty authorized set, so it denies
    everyone (fail-closed), matching every other published-row mutation.
    """
    for row in rows:
        pp = row.publish_policy
        authorized: set[str] = set()
        if pp is not None:
            authorized.update(pp.owner_ids)
            authorized.update(pp.contributor_ids)
        if ctx.actor_id not in authorized:
            raise AuthorityDenied(
                published_id=published_id,
                version=row.version,
                message=(
                    f"actor {ctx.actor_id!r} not authorized to purge "
                    f"{category}#{published_id}@v{row.version}"
                ),
            )


def _purge_prefix_or_raise(row: AssetCommon) -> str:
    """Return a version's S3 prefix, having proved it is the canonical one.

    A hard ``delete_prefix`` against an unrecognized prefix is the one failure
    this operation cannot tolerate, so the stored value is re-derived from the
    row's own scope/owner/category/version and compared. Anything surprising —
    an empty prefix, a bare ``/``, a value that does not match the grammar —
    aborts the purge before the first write rather than deleting an unknown
    location.
    """
    prefix = row.s3_prefix
    if not isinstance(prefix, str) or not prefix.strip() or prefix.strip() == "/":
        raise ValueError(
            f"refusing to purge {row.category}#{row.asset_id}@v{row.version}: "
            f"unusable s3_prefix {prefix!r}"
        )
    if not prefix.endswith("/"):
        raise ValueError(
            f"refusing to purge {row.category}#{row.asset_id}@v{row.version}: "
            f"s3_prefix {prefix!r} is not a prefix (no trailing slash)"
        )
    # validate_record_prefix raises PrefixMismatchError when the stored prefix
    # disagrees with the canonical grammar for this row.
    owner_user_id = owner_group_id = owner_org_id = None
    if row.scope == "private":
        owner_user_id = _owner_user_id_from_prefix(prefix)
    elif row.scope == "group":
        owner_group_id = _owner_group_id_from_prefix(prefix)
    elif row.scope == "org":
        owner_org_id = _owner_org_id_from_prefix(prefix)
    else:
        raise ValueError(f"unknown scope: {row.scope!r}")
    validate_record_prefix(
        row,
        owner_user_id=owner_user_id,
        owner_group_id=owner_group_id,
        owner_org_id=owner_org_id,
    )
    return prefix


def build_purge_preflight(
    *,
    category: str,
    published_id: str,
    versions: list[AssetCommon],
    working_copy: Optional[AssetCommon],
    hits: tuple[AgentReferenceHit, ...] = (),
    partial: tuple[str, ...] = (),
) -> PurgePreflight:
    """Assemble the blast-radius report for a lineage.

    Pure: takes rows already read and returns the report. Kept separate from
    the reference scan (which does I/O) so the purge can record exactly what it
    was told, and so tests can assert the report shape without S3.

    ``external_access_live`` is computed from the CURRENT version, because that
    is the row `POST /invoke/:agentId` resolves — and both gates must be set:
    the agent's own `integration_config.enabled` plus the separately revocable
    `external_access_enabled`.
    """
    ordered = sorted(versions, key=lambda r: r.version or 0)
    current = next((r for r in ordered if r.current), ordered[-1] if ordered else None)
    live = False
    if current is not None:
        cfg = getattr(current, "integration_config", None)
        configured = bool(cfg is not None and getattr(cfg, "enabled", False))
        gate = bool(getattr(current, "external_access_enabled", True))
        live = configured and gate
    return PurgePreflight(
        published_id=published_id,
        category=category,
        versions=tuple(r.version for r in ordered if r.version is not None),
        scopes=tuple(sorted({r.scope for r in ordered})),
        working_copy_exists=working_copy is not None,
        external_access_live=live,
        prefixes=tuple(r.s3_prefix for r in ordered),
        hits=hits,
        partial=partial,
    )


async def _prepare_purge(
    *,
    ctx: LabRequestContext,
    asset_index: AssetIndex,
    category: str,
    published_id: str,
    hits: tuple[AgentReferenceHit, ...],
    partial: tuple[str, ...],
) -> tuple[
    list[AssetCommon], Optional[AssetCommon], dict[Optional[int], str], PurgePreflight
]:
    """Enumerate, authorize, and validate — reads only, no writes.

    Shared verbatim by :func:`preflight_asset_purge` and
    :func:`purge_asset_lineage` so the preview and the destructive call cannot
    disagree about what would be deleted or about who is allowed to do it. A
    preview that applied looser authorization than the purge would be worse than
    no preview at all.

    Returns ``(versions, working_copy, prefixes_by_version, preflight)``.
    """
    if category not in _PURGEABLE_CATEGORIES:
        raise ValueError(
            f"purge only supports {sorted(_PURGEABLE_CATEGORIES)}; got {category!r}"
        )

    working_pk = make_pk_user_owned(ctx.actor_id)
    working_sk = make_sk_owned(category, published_id)
    working_copy = await asset_index.get(working_pk, working_sk)
    lineage = [
        r
        for r in await asset_index.query_lineage(published_id)
        if r.version is not None and r.category == category
    ]
    if working_copy is None and not lineage:
        raise NotFoundOrForbidden(
            f"no {category}#{published_id} to purge for actor {ctx.actor_id}"
        )

    _authorize_purge(ctx, category, published_id, lineage)
    prefixes = {row.version: _purge_prefix_or_raise(row) for row in lineage}
    preflight = build_purge_preflight(
        category=category,
        published_id=published_id,
        versions=lineage,
        working_copy=working_copy,
        hits=hits,
        partial=partial,
    )
    return lineage, working_copy, prefixes, preflight


async def preflight_asset_purge(
    *,
    ctx: LabRequestContext,
    asset_index: AssetIndex,
    category: str,
    published_id: str,
    hits: tuple[AgentReferenceHit, ...] = (),
    partial: tuple[str, ...] = (),
) -> PurgePreflight:
    """Report what :func:`purge_asset_lineage` would destroy. Writes nothing.

    Raises exactly what the purge would raise — so a caller shown a preflight
    has already passed the authorization and prefix-validation gates and cannot
    be surprised by a 403 after typing the confirmation.

    ``hits``/``partial`` come from :func:`lab.purge_scan.scan_agent_references`;
    this function does no reference I/O of its own.
    """
    _, _, _, preflight = await _prepare_purge(
        ctx=ctx,
        asset_index=asset_index,
        category=category,
        published_id=published_id,
        hits=hits,
        partial=partial,
    )
    return preflight


async def purge_asset_lineage(
    *,
    ctx: LabRequestContext,
    asset_index: AssetIndex,
    s3: S3UserFilesTool,
    category: str,
    published_id: str,
    hits: tuple[AgentReferenceHit, ...] = (),
    partial: tuple[str, ...] = (),
) -> Purged:
    """Hard-delete every version of a published lineage, plus its artifacts.

    The only operation in this layer that destroys immutable published state.
    :func:`deprecate_asset` is the reversible alternative and deliberately never
    touches S3; this does the opposite, and there is no undo.

    **Order: DynamoDB first, S3 second.** This is the reverse of
    :func:`promote_asset`, which copies S3 first precisely so a cancelled
    transaction can compensate. Here the asymmetry runs the other way: a
    surviving row pointing at deleted objects is a *broken published version*
    that resolvers will happily serve, whereas objects left behind by a failed
    sweep are unreferenced storage with no correctness impact. So rows go first
    and an S3 failure is reported, not raised — the same tradeoff
    ``_delete_owned_row_and_prefix`` already accepts for working copies.

    Sequence:

      1. Reject non-purgeable categories.
      2. Enumerate the lineage through GSI1 (:meth:`AssetIndex.query_lineage`),
         which is what finds versions spread across
         ``USER#u#VERSIONS`` / ``GROUP#g#SHARED`` / ``ORG#o#SHARED``, plus the
         working copy from the actor's OWNED partition. Neither present ⇒
         :class:`NotFoundOrForbidden`.
      3. Authorize every version (:func:`_authorize_purge`).
      4. Validate every S3 prefix (:func:`_purge_prefix_or_raise`).
      5. Delete rows, highest version first, one transaction per version:
         the row ``Delete`` plus — for org-scoped versions — a ``Deprecated``
         outbox row so the Registry projection converges to non-discoverable.
      6. Delete the working copy row and its prefix.
      7. Sweep the version prefixes.

    Steps 1–4 complete before any write, so an unauthorized or malformed
    lineage is rejected without partial destruction.

    No ``ConditionExpression`` on the deletes: ``attribute_exists(PK)`` would
    make a resumed purge cancel on rows a previous attempt already removed,
    turning an idempotent retry into a permanent failure. A DynamoDB delete of a
    missing key is a no-op, which is exactly the semantics a retry wants.

    Highest version first so that if the row phase fails partway, the surviving
    prefix of the lineage is the LOWER versions — both
    ``task_agent._pick_lineage_row`` and ``specialty_crud._pick_published_row``
    fall back to "highest non-deprecated", so degradation is monotonically
    downward rather than to an arbitrary survivor.

    Args:
        ctx: Request context; ``ctx.actor_id`` is the purge principal.
        asset_index: Single-table AssetIndex wrapper.
        s3: Artifact tool, used only for prefix deletion.
        category: Must be ``"specialty-agent"``.
        published_id: Lineage id to destroy.
        hits: Reference-scan findings to record on the result.
        partial: Sources the reference scan could not read.

    Returns:
        :class:`Purged`.

    Raises:
        ValueError: Category is not purgeable, or a stored prefix is unusable.
        PrefixMismatchError: A stored prefix disagrees with the canonical
            grammar for its row.
        NotFoundOrForbidden: Nothing to purge under this id for this actor.
        AuthorityDenied: The actor lacks authority on at least one version.
        ConflictError: A delete transaction cancelled; earlier versions may
            already be gone and a retry converges.
    """
    # ------------------------------------------------------------------
    # 1-2. Enumerate, authorize, validate — reads only, and identical to what
    # the preflight the user confirmed was computed from.
    # ------------------------------------------------------------------
    lineage, working_copy, prefixes, preflight = await _prepare_purge(
        ctx=ctx,
        asset_index=asset_index,
        category=category,
        published_id=published_id,
        hits=hits,
        partial=partial,
    )

    # ------------------------------------------------------------------
    # 3. Delete rows, highest version first, one transaction per version.
    # ------------------------------------------------------------------
    versions_deleted: list[int] = []
    outbox_events = 0
    for row in sorted(lineage, key=lambda r: r.version or 0, reverse=True):
        pk, sk = _scope_keys_for_published(row)
        operations: list[dict[str, Any]] = [
            {
                "Delete": {
                    "TableName": asset_index.table_name,
                    "Key": {"PK": pk, "SK": sk},
                }
            }
        ]
        if registry_eligible(row.scope, category):
            # The projector has no delete capability by design; a Deprecated
            # event makes it converge the Registry revision to DEPRECATED once
            # it reloads canonical state and finds the row gone.
            operations.append(
                build_outbox_put(
                    table_name=asset_index.table_name,
                    event_type=EVENT_DEPRECATED,
                    category=category,
                    asset_id=published_id,
                    version=row.version,
                    scope=row.scope,
                    canonical_revision=sk,
                )
            )
        try:
            await asset_index.transact_write(operations)
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code", "")
            if code == "TransactionCanceledException":
                logger.error(
                    "Purge partially applied: lineage=%s purged=%s actor=%s",
                    published_id,
                    versions_deleted,
                    ctx.actor_id,
                )
                raise ConflictError(
                    f"Purge of {category}#{published_id} cancelled after "
                    f"removing {versions_deleted}; retry to finish",
                    cancellation_reasons=err.response.get("CancellationReasons", []),
                ) from err
            raise
        versions_deleted.append(row.version)
        if len(operations) > 1:
            outbox_events += 1

    # ------------------------------------------------------------------
    # 4. Working copy (row + prefix), reusing the existing helper.
    # ------------------------------------------------------------------
    working_copy_deleted = False
    if working_copy is not None:
        from ._lifecycle_ops import _delete_owned_row_and_prefix

        await _delete_owned_row_and_prefix(
            asset_index=asset_index,
            s3=s3,
            actor_id=ctx.actor_id,
            category=category,
            asset_id=published_id,
            s3_prefix=working_copy.s3_prefix,
        )
        working_copy_deleted = True

    # ------------------------------------------------------------------
    # 5. Sweep artifacts. Never raises: the rows are already gone, so a
    # failure here leaves unreferenced objects, not a broken asset.
    # ------------------------------------------------------------------
    deleted: list[str] = []
    orphaned: list[str] = []
    for version in versions_deleted:
        prefix = prefixes[version]
        try:
            await s3.delete_prefix_absolute(prefix)
            deleted.append(prefix)
        except Exception as err:  # noqa: BLE001 — reported, never fatal
            orphaned.append(prefix)
            logger.warning(
                "orphan_purge_prefix=%s actor=%s lineage=%s version=%s error=%r",
                prefix,
                ctx.actor_id,
                published_id,
                version,
                err,
            )

    logger.info(
        "Purge ok: actor=%s %s#%s versions=%s working_copy=%s outbox=%d orphans=%d refs=%d",
        ctx.actor_id,
        category,
        published_id,
        versions_deleted,
        working_copy_deleted,
        outbox_events,
        len(orphaned),
        len(hits),
    )

    return Purged(
        published_id=published_id,
        category=category,
        versions_deleted=tuple(versions_deleted),
        working_copy_deleted=working_copy_deleted,
        prefixes_deleted=tuple(deleted),
        prefixes_orphaned=tuple(orphaned),
        outbox_events=outbox_events,
        preflight=preflight,
    )


def _scope_keys_for_published(record: AssetCommon) -> tuple[str, str]:
    """Compute the (PK, SK) of an existing Published_Record.

    The PK depends on ``scope`` and the owner attribute populated on the
    record; the SK is always ``<category>#<published_id>#v<NNN...>``.
    Used by :func:`promote_asset` to address the prior current row in
    its TransactWriteItems payload.
    """
    if record.version is None:
        raise ValueError(
            "cannot compute keys for a row with version=None"
        )
    sk = make_sk_version(record.category, record.asset_id, record.version)
    if record.scope == "private":
        # The owner_user_id is not on the typed record; we recover it
        # from the s3_prefix via the prefix grammar. This avoids a
        # second AssetIndex round-trip in the hot path.
        owner_user_id = _owner_user_id_from_prefix(record.s3_prefix)
        return make_pk_user_versions(owner_user_id), sk
    if record.scope == "group":
        owner_group_id = _owner_group_id_from_prefix(record.s3_prefix)
        return make_pk_group_shared(owner_group_id), sk
    if record.scope == "org":
        owner_org_id = _owner_org_id_from_prefix(record.s3_prefix)
        return make_pk_org_shared(owner_org_id), sk
    raise ValueError(f"unknown scope: {record.scope!r}")


def _owner_user_id_from_prefix(s3_prefix: str) -> str:
    """Extract ``<userId>`` from a ``users/<userId>/published/...`` prefix."""
    parts = s3_prefix.split("/")
    if len(parts) < 2 or parts[0] != "users":
        raise ValueError(f"not a users/ prefix: {s3_prefix!r}")
    return parts[1]


def _owner_group_id_from_prefix(s3_prefix: str) -> str:
    """Extract ``<groupId>`` from a ``groups/<groupId>/published/...`` prefix."""
    parts = s3_prefix.split("/")
    if len(parts) < 2 or parts[0] != "groups":
        raise ValueError(f"not a groups/ prefix: {s3_prefix!r}")
    return parts[1]


def _owner_org_id_from_prefix(s3_prefix: str) -> str:
    """Extract ``<orgId>`` from an ``org/<orgId>/published/...`` prefix."""
    parts = s3_prefix.split("/")
    if len(parts) < 2 or parts[0] != "org":
        raise ValueError(f"not an org/ prefix: {s3_prefix!r}")
    return parts[1]


__all__ = [
    "promote_asset",
    "share_asset",
    "deprecate_asset",
    "clone_asset",
    "rollback_asset",
    "set_external_access_asset",
    "purge_asset_lineage",
    "preflight_asset_purge",
    "build_purge_preflight",
]
