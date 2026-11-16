"""AssetIndex — single-table DynamoDB wrapper for the Unified MediaWorks Platform.

This module replaces the per-actor draft index with a single-table design
that spans drafts, all three published visibility tiers (private / group /
org), and the flat tool-collection partition. The previous attribute names
(``ownerActorId`` / ``assetIdSk``) are gone; the new schema uses ``PK`` /
``SK`` plus a ``byPublishedId`` GSI keyed by ``GSI1PK`` / ``GSI1SK``.

PK encoding (one of)::

    USER#<userId>#DRAFTS              all drafts owned by the user
    USER#<userId>#PUBLISHED           private-scoped published rows
    GROUP#<groupId>#PUBLISHED         group-scoped published rows
    ORG#<orgId>#PUBLISHED             org-scoped published rows
    USER#<userId>#TOOL_COLLECTIONS    flat tool-collection partition
    MIGRATION#MARKER                  reserved for migration idempotency

SK encoding (one of)::

    <category>#<draftId>#draft                       drafts
    <category>#<publishedId>#v<version, %010d>       versioned published
    <category>#<id>                                  tool-collection (flat)

GSI ``byPublishedId``::

    GSI1PK = "PUB#" + publishedId    (only on a version: ``version`` present)
    GSI1SK = "v" + zpad(version)

Connection-error semantics are preserved from the prior implementation:
``EndpointConnectionError`` / ``ConnectTimeoutError`` and the
``ServiceUnavailable`` / ``InternalServerError`` /
``ProvisionedThroughputExceededException`` / ``RequestLimitExceeded`` DDB
client error codes are mapped to :class:`ServiceUnavailableError`. The
Lab_Service must never fall back to S3 listing when AssetIndex is
unavailable (Requirement 9.8).

Migration markers used by ``agent/src/lab/migration/`` live under a
reserved partition with ``PK = "MIGRATION#MARKER"`` and ``SK = <key>``;
they are intentionally outside the asset-record key space so that listing
or scanning the asset partitions never returns marker rows.

.. note::
    Rows written by the canonical-operation layer are also the projection
    source for the harness-shared AWS Agent Registry catalog: lifecycle
    transactions append an ``OUTBOX#`` row (see :mod:`lab.outbox`) which the
    projection Lambda consumes off this table's DynamoDB stream. The stream
    is therefore a hard prerequisite for projection and is enabled on the CDK
    table (``stream: NEW_IMAGE``); the projector's event-source filter keys on
    ``recordType = "outbox"`` so asset-row writes do not invoke it.

Requirements: 1.6, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10
Properties: 1, 2, 3, 12
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
)

from .errors import ServiceUnavailableError
from .models import AssetCommon, from_index_item, to_index_item
from .path_validator import validate_record_prefix


# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------

#: Width of the zero-padded version segment in SK and GSI1SK. Padding to a
#: fixed width keeps ``begins_with(SK, "<cat>#<pid>#")`` ordered ascending
#: by version under DynamoDB's lexicographic sort, which lets Rollback and
#: "highest non-deprecated" lookups walk the lineage without an extra GSI.
VERSION_PAD_WIDTH: int = 10

#: Reserved partition for migration idempotency markers. Outside the asset
#: PK encoding so that asset queries never collide with marker rows.
MIGRATION_MARKER_PK: str = "MIGRATION#MARKER"

#: GSI name; emitted on every published record write and queried by
#: ``query_lineage``.
GSI1_NAME: str = "byPublishedId"


# ---------------------------------------------------------------------------
# PK / SK helpers (module-level, exported)
# ---------------------------------------------------------------------------


# --- v2 partition helpers (asset-lifecycle-redesign) -------------------------
#
# v2 collapses the lifecycle axis (draft / published / deprecated) into
# visibility (owned / versions / shared). Working copies live under OWNED;
# immutable snapshots live under VERSIONS; sharing is a separate row in
# GROUP#SHARED or ORG#SHARED.


def make_pk_user_owned(user_id: str) -> str:
    """Render the partition key for a user's working-copy partition (v2)."""
    if not user_id:
        raise ValueError("user_id must be a non-empty string")
    return f"USER#{user_id}#OWNED"


def make_pk_user_versions(user_id: str) -> str:
    """Render the partition key for a user's immutable versions partition (v2)."""
    if not user_id:
        raise ValueError("user_id must be a non-empty string")
    return f"USER#{user_id}#VERSIONS"


def make_pk_group_shared(group_id: str) -> str:
    """Render the partition key for a group's shared-version partition (v2)."""
    if not group_id:
        raise ValueError("group_id must be a non-empty string")
    return f"GROUP#{group_id}#SHARED"


def make_pk_org_shared(org_id: str) -> str:
    """Render the partition key for an org's shared-version partition (v2)."""
    if not org_id:
        raise ValueError("org_id must be a non-empty string")
    return f"ORG#{org_id}#SHARED"


def make_sk_owned(category: str, asset_id: str) -> str:
    """Render the sort key for a working-copy row (v2).

    ``category`` is included so ``begins_with(SK, "<cat>#")`` filters work
    uniformly across partitions, mirroring v1 semantics.
    """
    if not category or not asset_id:
        raise ValueError("category and asset_id must be non-empty strings")
    return f"{category}#{asset_id}"


def make_sk_version(category: str, asset_id: str, version: int) -> str:
    """Render the sort key for a versioned row (v2).

    Version is zero-padded so lexicographic ordering matches numeric.
    """
    if not category or not asset_id:
        raise ValueError("category and asset_id must be non-empty strings")
    if not isinstance(version, int) or version <= 0:
        raise ValueError(f"version must be a positive int, got {version!r}")
    padded = str(version).zfill(VERSION_PAD_WIDTH)
    return f"{category}#{asset_id}#v{padded}"


# --- v1 helpers retained as deprecated aliases -------------------------------
#
# Kept so the migration script and any test paths still under v1 semantics
# continue to import. New code should use the v2 helpers above.


def make_pk_user_drafts(user_id: str) -> str:
    """[v1, deprecated] Render the partition key for a user's draft partition.

    v2: working copies live under :func:`make_pk_user_owned`.
    """
    if not user_id:
        raise ValueError("user_id must be a non-empty string")
    return f"USER#{user_id}#DRAFTS"


def make_pk_user_published(user_id: str) -> str:
    """[v1, deprecated] Render the partition key for a user's private-scope published partition.

    v2: versions live under :func:`make_pk_user_versions`.
    """
    if not user_id:
        raise ValueError("user_id must be a non-empty string")
    return f"USER#{user_id}#PUBLISHED"


def make_pk_group_published(group_id: str) -> str:
    """[v1, deprecated] v2: see :func:`make_pk_group_shared`."""
    if not group_id:
        raise ValueError("group_id must be a non-empty string")
    return f"GROUP#{group_id}#PUBLISHED"


def make_pk_org_published(org_id: str) -> str:
    """[v1, deprecated] v2: see :func:`make_pk_org_shared`."""
    if not org_id:
        raise ValueError("org_id must be a non-empty string")
    return f"ORG#{org_id}#PUBLISHED"


def make_pk_user_tool_collections(user_id: str) -> str:
    """[v1, deprecated] Flat tool-collection partition.

    v2 collapses tool collections into the unified OWNED partition; this
    helper is kept only for the migration script.
    """
    if not user_id:
        raise ValueError("user_id must be a non-empty string")
    return f"USER#{user_id}#TOOL_COLLECTIONS"


def make_sk_draft(category: str, draft_id: str) -> str:
    """Render the sort key for a draft row."""
    if not category or not draft_id:
        raise ValueError("category and draft_id must be non-empty strings")
    return f"{category}#{draft_id}#draft"


def make_sk_published(category: str, published_id: str, version: int) -> str:
    """Render the sort key for a versioned published row.

    The version is zero-padded to :data:`VERSION_PAD_WIDTH` so SK ranges
    sort numerically under lexicographic comparison.
    """
    if not category or not published_id:
        raise ValueError("category and published_id must be non-empty strings")
    if not isinstance(version, int) or version <= 0:
        raise ValueError(f"version must be a positive int, got {version!r}")
    padded = str(version).zfill(VERSION_PAD_WIDTH)
    return f"{category}#{published_id}#v{padded}"


def make_sk_tool_collection(category: str, asset_id: str) -> str:
    """Render the sort key for a flat tool-collection row.

    ``category`` is included for symmetry with other SKs and to keep the
    ``begins_with(SK, "<cat>#")`` query pattern uniform across partitions.
    """
    if not category or not asset_id:
        raise ValueError("category and asset_id must be non-empty strings")
    return f"{category}#{asset_id}"


def make_gsi1_keys(published_id: str, version: int) -> tuple[str, str]:
    """Render the (GSI1PK, GSI1SK) pair for a versioned published row.

    GSI1 is sparse — it is populated only on rows with
    a positive ``version``. Drafts and the
    flat tool-collection rows omit both attributes so they do not project
    into GSI1.
    """
    if not published_id:
        raise ValueError("published_id must be a non-empty string")
    if not isinstance(version, int) or version <= 0:
        raise ValueError(f"version must be a positive int, got {version!r}")
    padded = str(version).zfill(VERSION_PAD_WIDTH)
    return f"PUB#{published_id}", f"v{padded}"


# ---------------------------------------------------------------------------
# Connection-error mapping
# ---------------------------------------------------------------------------


_UNAVAILABLE_CLIENT_ERROR_CODES: frozenset[str] = frozenset(
    {
        "ServiceUnavailable",
        "InternalServerError",
        "ProvisionedThroughputExceededException",
        "RequestLimitExceeded",
        "ThrottlingException",
    }
)


def _raise_if_unavailable(err: BaseException) -> None:
    """Map DDB connectivity / throttling errors to :class:`ServiceUnavailableError`.

    Raises :class:`ServiceUnavailableError` for the cases listed in the
    module docstring; otherwise re-raises the original exception. Caller
    is expected to invoke this from within an ``except`` block.
    """
    if isinstance(err, (EndpointConnectionError, ConnectTimeoutError)):
        raise ServiceUnavailableError(f"DynamoDB unavailable: {err}") from err
    if isinstance(err, ClientError):
        code = err.response.get("Error", {}).get("Code", "")
        if code in _UNAVAILABLE_CLIENT_ERROR_CODES:
            raise ServiceUnavailableError(
                f"DynamoDB unavailable ({code}): {err}"
            ) from err
        # Fall through: caller decides what to do with non-availability errors.
        raise
    raise err


# ---------------------------------------------------------------------------
# AssetIndex
# ---------------------------------------------------------------------------


class AssetIndex:
    """Single-table DynamoDB wrapper for the Unified MediaWorks Platform.

    Exposes ``put``, ``get``, ``update``, ``query_pk``, ``query_lineage``
    (GSI1), ``transact_write``, plus ``get_marker`` / ``put_marker`` used
    by the migration package. Methods are async to match the per-category
    CRUD layer; the boto3 internals are synchronous and run in the calling
    thread (boto3 is not natively async).

    The class is no longer actor-bound — the caller computes the PK from
    the appropriate ``make_pk_*`` helper based on scope/owner. The
    ``actor_id`` constructor parameter is retained as a no-op for
    backward compatibility with call sites that have not yet migrated.

    Args:
        ddb_table: A boto3 DynamoDB ``Table`` resource (or compatible
            mock).
        actor_id: Deprecated and ignored. Retained so existing call sites
            can construct the index without modification while the
            canonical-operation tasks (3.x) migrate them off the
            actor-bound API.
        ddb_client: Optional explicit boto3 ``DynamoDB.Client`` for
            ``transact_write_items``. When omitted, the client is fetched
            from ``ddb_table.meta.client``.
    """

    def __init__(
        self,
        ddb_table: Any,
        actor_id: Optional[str] = None,
        *,
        ddb_client: Any = None,
    ) -> None:
        self._table = ddb_table
        self.actor_id = actor_id  # retained for back-compat; no-op
        if ddb_client is None:
            try:
                ddb_client = ddb_table.meta.client
            except AttributeError:
                ddb_client = None
        self._client = ddb_client

    @property
    def table_name(self) -> str:
        """The DynamoDB table name backing this index.

        Exposed for the canonical-operation layer
        (``_canonical_ops.py``), which builds ``TransactWriteItems``
        payloads requiring an explicit ``TableName`` per item. Reading
        ``self._table.name`` directly works for boto3 ``Table``
        resources; mocks in tests should set ``.name`` accordingly.
        """
        return self._table.name

    # ------------------------------------------------------------------
    # Item assembly
    # ------------------------------------------------------------------

    def _scope_keys_for(
        self,
        asset: AssetCommon,
        *,
        owner_user_id: Optional[str],
        owner_group_id: Optional[str],
        owner_org_id: Optional[str],
    ) -> tuple[str, str]:
        """Compute the (PK, SK) pair for an asset based on its category and
        v2 lifecycle position.

        v2 routing (asset-lifecycle-redesign):

          version is None  →  USER#<u>#OWNED          (working copy, owner-only)
          version > 0 + scope=private →  USER#<u>#VERSIONS
          version > 0 + scope=group   →  GROUP#<g>#SHARED
          version > 0 + scope=org     →  ORG#<o>#SHARED

        Tool collections route through the same scheme as every other
        category (no flat partition).
        """
        category = asset.category

        # Working copy: any category with version=None lives in the owner's
        # OWNED partition.
        if asset.version is None:
            if not owner_user_id:
                raise ValueError("working-copy rows require owner_user_id")
            return (
                make_pk_user_owned(owner_user_id),
                make_sk_owned(category, asset.asset_id),
            )

        # Versioned row.
        sk = make_sk_version(category, asset.asset_id, asset.version)

        if asset.scope == "private":
            if not owner_user_id:
                raise ValueError("scope='private' requires owner_user_id")
            return make_pk_user_versions(owner_user_id), sk
        if asset.scope == "group":
            if not owner_group_id:
                raise ValueError("scope='group' requires owner_group_id")
            return make_pk_group_shared(owner_group_id), sk
        if asset.scope == "org":
            if not owner_org_id:
                raise ValueError("scope='org' requires owner_org_id")
            return make_pk_org_shared(owner_org_id), sk
        raise ValueError(f"unknown scope: {asset.scope!r}")

    def _build_item(
        self,
        asset: AssetCommon,
        *,
        owner_user_id: Optional[str],
        owner_group_id: Optional[str],
        owner_org_id: Optional[str],
    ) -> dict[str, Any]:
        """Serialize an asset to a DDB item with PK / SK / GSI1 attributes.

        The model-level :func:`to_index_item` produces the camelCase
        attribute payload (``recordType``, ``status``, ``scope``,
        ``ownerUserId`` / ``ownerGroupId`` / ``ownerOrgId``,
        ``publishPolicy``, ``scopeHistory``, ``current``, ``promotedFrom``,
        ``lastPublishedAs``, ``clonedFrom``, ``s3Prefix``, ...). This
        method overlays the index-level attributes on top.
        """
        item = to_index_item(
            asset,
            owner_user_id=owner_user_id,
            owner_group_id=owner_group_id,
            owner_org_id=owner_org_id,
        )

        pk, sk = self._scope_keys_for(
            asset,
            owner_user_id=owner_user_id,
            owner_group_id=owner_group_id,
            owner_org_id=owner_org_id,
        )
        item["PK"] = pk
        item["SK"] = sk

        # Sparse GSI: only published rows of versioned categories project
        # into GSI1. Tool-collections (no version) and drafts are excluded.
        if (
            asset.category != "tool-collection"
            and asset.version is not None
        ):
            gsi1pk, gsi1sk = make_gsi1_keys(asset.asset_id, asset.version)
            item["GSI1PK"] = gsi1pk
            item["GSI1SK"] = gsi1sk

        return item

    # ------------------------------------------------------------------
    # CRUD (async)
    # ------------------------------------------------------------------

    async def put(
        self,
        asset: AssetCommon,
        *,
        owner_user_id: Optional[str] = None,
        owner_group_id: Optional[str] = None,
        owner_org_id: Optional[str] = None,
    ) -> None:
        """Write an asset record, validating its prefix first.

        Calls :func:`validate_record_prefix` before issuing the boto3
        ``PutItem`` so any divergence between the cached ``s3_prefix`` and
        the canonical prefix derived from the record's scope/owner/
        category/asset_id/version raises :class:`PrefixMismatchError`
        before a write can land. This is the single-record write path; the
        condition expression ``attribute_not_exists(PK)`` makes the call
        idempotent against accidental re-writes (the canonical operations
        layer composes Put with explicit conditions via
        :meth:`transact_write` when stronger guarantees are needed).

        Raises:
            PrefixMismatchError: When ``asset.s3_prefix`` does not match
                the canonical prefix.
            ServiceUnavailableError: When DynamoDB is unreachable.
            ClientError: For ``ConditionalCheckFailedException`` and other
                non-availability DDB errors.
        """
        validate_record_prefix(
            asset,
            owner_user_id=owner_user_id,
            owner_group_id=owner_group_id,
            owner_org_id=owner_org_id,
        )

        item = self._build_item(
            asset,
            owner_user_id=owner_user_id,
            owner_group_id=owner_group_id,
            owner_org_id=owner_org_id,
        )

        def _do_put() -> None:
            try:
                self._table.put_item(
                    Item=item,
                    ConditionExpression="attribute_not_exists(PK)",
                )
            except (EndpointConnectionError, ConnectTimeoutError) as err:
                _raise_if_unavailable(err)
            except ClientError as err:
                _raise_if_unavailable(err)

        await asyncio.to_thread(_do_put)

    async def get(self, pk: str, sk: str) -> Optional[AssetCommon]:
        """Fetch a single record by its (PK, SK) and return the asset.

        Returns None when the record does not exist. The DDB item is
        round-tripped through :func:`from_index_item` so the caller
        receives a typed :class:`AssetCommon` subclass rather than the
        raw camelCase dict.
        """

        def _do_get() -> Optional[dict[str, Any]]:
            try:
                resp = self._table.get_item(Key={"PK": pk, "SK": sk})
            except (EndpointConnectionError, ConnectTimeoutError) as err:
                _raise_if_unavailable(err)
            except ClientError as err:
                _raise_if_unavailable(err)
            return resp.get("Item")

        item = await asyncio.to_thread(_do_get)
        if item is None:
            return None
        return from_index_item(item)

    async def update(
        self,
        pk: str,
        sk: str,
        *,
        update_expression: str,
        expression_attribute_names: Optional[dict[str, str]] = None,
        expression_attribute_values: Optional[dict[str, Any]] = None,
        condition_expression: Optional[str] = None,
    ) -> None:
        """Issue an ``UpdateItem`` against a (PK, SK) pair.

        The caller supplies the full ``UpdateExpression`` (and optional
        names / values / condition) so the canonical-operation layer can
        express the precise mutation it needs — for example
        ``SET current = :true`` with ``ConditionExpression =
        "current = :false"`` to swing the per-lineage ``current`` pointer
        atomically.

        Raises:
            ServiceUnavailableError: When DynamoDB is unreachable.
            ClientError: For ``ConditionalCheckFailedException`` and other
                non-availability DDB errors. Callers are expected to
                catch and translate the specific failure into a domain
                error (e.g., ``ConflictError``).
        """
        kwargs: dict[str, Any] = {
            "Key": {"PK": pk, "SK": sk},
            "UpdateExpression": update_expression,
        }
        if expression_attribute_names is not None:
            kwargs["ExpressionAttributeNames"] = expression_attribute_names
        if expression_attribute_values is not None:
            kwargs["ExpressionAttributeValues"] = expression_attribute_values
        if condition_expression is not None:
            kwargs["ConditionExpression"] = condition_expression

        def _do_update() -> None:
            try:
                self._table.update_item(**kwargs)
            except (EndpointConnectionError, ConnectTimeoutError) as err:
                _raise_if_unavailable(err)
            except ClientError as err:
                _raise_if_unavailable(err)

        await asyncio.to_thread(_do_update)

    async def query_pk(
        self,
        pk: str,
        *,
        sk_prefix: Optional[str] = None,
        last_evaluated_key: Optional[dict[str, Any]] = None,
        limit: int = 100,
    ) -> tuple[list[AssetCommon], Optional[dict[str, Any]]]:
        """Query a partition by PK with an optional SK prefix.

        Returns ``(items, next_cursor)`` where ``next_cursor`` is the
        DDB ``LastEvaluatedKey`` to be passed back as
        ``last_evaluated_key`` on the next call, or ``None`` when the
        query is exhausted. Items are decoded into typed assets via
        :func:`from_index_item`; rows that fail to decode (e.g., migration
        markers that slipped into a wrong partition, which should not
        happen in practice) are skipped silently.

        The caller is expected to know whether the partition is a
        scope-aware partition (``USER#u#PUBLISHED``, ``GROUP#g#PUBLISHED``,
        ``ORG#o#PUBLISHED``) or a draft / tool-collection partition; the
        ``sk_prefix`` is typically ``"<category>#"`` so the query returns
        only one category at a time, matching the access-pattern table in
        the design.
        """
        kwargs: dict[str, Any] = {"Limit": limit}
        if sk_prefix is None:
            kwargs["KeyConditionExpression"] = "PK = :pk"
            kwargs["ExpressionAttributeValues"] = {":pk": pk}
        else:
            kwargs["KeyConditionExpression"] = (
                "PK = :pk AND begins_with(SK, :sk)"
            )
            kwargs["ExpressionAttributeValues"] = {":pk": pk, ":sk": sk_prefix}
        if last_evaluated_key is not None:
            kwargs["ExclusiveStartKey"] = last_evaluated_key

        def _do_query() -> dict[str, Any]:
            try:
                return self._table.query(**kwargs)
            except (EndpointConnectionError, ConnectTimeoutError) as err:
                _raise_if_unavailable(err)
            except ClientError as err:
                _raise_if_unavailable(err)

        resp = await asyncio.to_thread(_do_query)
        raw_items = resp.get("Items", [])
        decoded: list[AssetCommon] = []
        for raw in raw_items:
            try:
                decoded.append(from_index_item(raw))
            except (KeyError, ValueError):
                # Defensive: skip malformed rows rather than failing the
                # whole page. This keeps a single bad row from breaking
                # the visibility resolver.
                continue
        return decoded, resp.get("LastEvaluatedKey")

    async def query_lineage(self, published_id: str) -> list[AssetCommon]:
        """Return every published version of a lineage via GSI1.

        Issues a ``Query`` against the ``byPublishedId`` GSI with
        ``GSI1PK = "PUB#" + published_id`` and pages until exhaustion,
        returning all versions of the lineage decoded into typed assets.
        Used by Promote (to compute next version), Deprecate (to find the
        next-highest non-deprecated version), Rollback (to validate the
        target), and Share (to load the source row).

        The result is in ascending version order because GSI1SK uses the
        zero-padded ``v<NNNNNNNNNN>`` form.
        """
        if not published_id:
            raise ValueError("published_id must be a non-empty string")

        gsi1pk = f"PUB#{published_id}"
        items: list[AssetCommon] = []
        last_key: Optional[dict[str, Any]] = None

        while True:
            kwargs: dict[str, Any] = {
                "IndexName": GSI1_NAME,
                "KeyConditionExpression": "GSI1PK = :pk",
                "ExpressionAttributeValues": {":pk": gsi1pk},
            }
            if last_key is not None:
                kwargs["ExclusiveStartKey"] = last_key

            def _do_query(_kwargs: dict[str, Any] = kwargs) -> dict[str, Any]:
                try:
                    return self._table.query(**_kwargs)
                except (EndpointConnectionError, ConnectTimeoutError) as err:
                    _raise_if_unavailable(err)
                except ClientError as err:
                    _raise_if_unavailable(err)

            resp = await asyncio.to_thread(_do_query)
            for raw in resp.get("Items", []):
                try:
                    items.append(from_index_item(raw))
                except (KeyError, ValueError):
                    continue

            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                break

        return items

    async def transact_write(self, operations: list[dict[str, Any]]) -> None:
        """Execute a multi-row transaction with the connection-error wrapper.

        ``operations`` is a list of ``TransactItems`` entries in the exact
        boto3 ``transact_write_items`` shape — each element is a dict with
        a single key of ``Put``, ``Update``, ``Delete``, or
        ``ConditionCheck``, mapped to the corresponding parameters dict.
        The wrapper does not re-shape the operations; it only translates
        availability errors. Callers (the canonical-operation layer) are
        responsible for embedding the correct ``PK`` / ``SK`` / GSI
        attributes and any ``ConditionExpression`` clauses they need.

        DynamoDB transactions are limited to 100 actions per request; the
        canonical operations stay well within that cap.

        Raises:
            ServiceUnavailableError: When DynamoDB is unreachable or
                throttling.
            ClientError: For ``TransactionCanceledException`` and other
                non-availability errors. Callers translate
                ``TransactionCanceledException`` into ``ConflictError``.
        """
        if self._client is None:
            raise RuntimeError(
                "AssetIndex was constructed without a DynamoDB client; "
                "transact_write requires either a Table whose meta.client "
                "is reachable or an explicit ddb_client."
            )

        def _do_txn() -> None:
            try:
                self._client.transact_write_items(TransactItems=operations)
            except (EndpointConnectionError, ConnectTimeoutError) as err:
                _raise_if_unavailable(err)
            except ClientError as err:
                _raise_if_unavailable(err)

        await asyncio.to_thread(_do_txn)

    # ------------------------------------------------------------------
    # Migration markers
    # ------------------------------------------------------------------

    async def get_marker(self, key: str) -> Optional[dict[str, Any]]:
        """Read a migration idempotency marker.

        Returns the stored ``value`` dict (the raw item without the
        partition key tuple) or ``None`` when no marker exists for
        ``key``. Markers live under the reserved
        ``PK = "MIGRATION#MARKER"`` partition with ``SK = key``.
        """
        if not key:
            raise ValueError("marker key must be a non-empty string")

        def _do_get() -> Optional[dict[str, Any]]:
            try:
                resp = self._table.get_item(
                    Key={"PK": MIGRATION_MARKER_PK, "SK": key}
                )
            except (EndpointConnectionError, ConnectTimeoutError) as err:
                _raise_if_unavailable(err)
            except ClientError as err:
                _raise_if_unavailable(err)
            return resp.get("Item")

        item = await asyncio.to_thread(_do_get)
        if item is None:
            return None
        # Strip the key attributes; return only the payload.
        return {k: v for k, v in item.items() if k not in ("PK", "SK")}

    async def put_marker(self, key: str, value: dict[str, Any]) -> None:
        """Write or overwrite a migration idempotency marker.

        The marker is committed *after* both S3 and AssetIndex writes
        succeed (per the migration-package design); on resume any
        operation whose marker is present is skipped. This method itself
        does not enforce the post-commit ordering — that is the migration
        package's responsibility.
        """
        if not key:
            raise ValueError("marker key must be a non-empty string")
        if not isinstance(value, dict):
            raise TypeError("marker value must be a dict")

        item = {"PK": MIGRATION_MARKER_PK, "SK": key, **value}

        def _do_put() -> None:
            try:
                self._table.put_item(Item=item)
            except (EndpointConnectionError, ConnectTimeoutError) as err:
                _raise_if_unavailable(err)
            except ClientError as err:
                _raise_if_unavailable(err)

        await asyncio.to_thread(_do_put)


__all__ = [
    "AssetIndex",
    "GSI1_NAME",
    "MIGRATION_MARKER_PK",
    "VERSION_PAD_WIDTH",
    "make_gsi1_keys",
    "make_pk_group_published",
    "make_pk_org_published",
    "make_pk_user_drafts",
    "make_pk_user_published",
    "make_pk_user_tool_collections",
    "make_sk_draft",
    "make_sk_published",
    "make_sk_tool_collection",
]
