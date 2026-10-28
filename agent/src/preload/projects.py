"""Seed a Project's four rows at deploy time, byte-shape-identical to the proxy's create (design Part 1).

The proxy (``ui/server/projects/registry.ts``) is the only writer of a Project today, and its access check
rejects a META that is not ``status == "active"`` with an ``ownerSub`` and not a tombstone. So seeding MUST
write the same shape. :func:`meta_item` mirrors ``registry.ts :: metaItem`` field-for-field; the parallel
member / mirror / name-guard builders mirror the other three puts in ``registry.create``.

Known trap (AGENTS.md): parallel item-builders drift and silently drop a field. :data:`META_FIELDS` pins the
exact META attribute set, and ``tests/test_preload_project_seeding.py`` asserts :func:`meta_item` produces
exactly it — so adding a field to the proxy without adding it here fails the build.

One ``TransactWriteItems`` writes all four rows, each conditioned ``attribute_not_exists(PK)``: a re-deploy
(same ``uuid5`` id) is a clean no-op (the transaction is cancelled, which :func:`seed_project` reports as
``created=False``). Counters (``ownedCount``) are intentionally NOT touched — a conditional increment is not
idempotent across re-deploys, and the design's four-item set omits it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from botocore.exceptions import ClientError

from projects import schema

# The exact attribute names META carries (``registry.ts :: metaItem``), excluding the PK/SK key. ``graph`` is
# written only when the graph feature is on, so it is NOT in this required set (mirrors the proxy's
# conditional spread).
META_FIELDS = (
    "projectId", "name", "nameFold", "ownerSub", "personal", "status", "memberCount", "createdAt",
    "activeAssetCount", "collectionState", "description", "tags", "indexing",
)


def meta_item(
    pid: str, name: str, name_fold: str, owner_sub: str, personal: bool, at: str,
    *, description: str = "", tags: Optional[list] = None, indexing: bool = False,
    graph: Optional[bool] = None,
) -> dict[str, Any]:
    """The META item, identical to ``registry.ts :: metaItem`` (design Part 1 parity row)."""
    item: dict[str, Any] = {
        **schema.meta_key(pid),
        "projectId": pid,
        "name": name,
        "nameFold": name_fold,
        "ownerSub": owner_sub,
        "personal": personal,
        "status": "active",
        "memberCount": 0,
        "createdAt": at,
        "activeAssetCount": 0,
        "collectionState": "absent",
        # Written explicitly (never absent), as the proxy does.
        "description": description,
        "tags": list(tags or []),
        "indexing": indexing,
    }
    if graph is not None:
        item["graph"] = graph
    return item


def member_item(pid: str, sub: str, role: str, display: str, added_at: str, added_by: str) -> dict[str, Any]:
    """The owner MEMBER item (``registry.ts :: memberItem``)."""
    return {**schema.member_key(pid, sub), "role": role, "display": display,
            "addedAt": added_at, "addedBy": added_by}


def mirror_item(sub: str, pid: str) -> dict[str, Any]:
    """The user→Project mirror (``{ ...mirrorKey, role: "owner" }`` in ``registry.create``)."""
    return {**schema.mirror_key(sub, pid), "role": "owner"}


def name_guard_item(owner_sub: str, name_fold: str, pid: str) -> dict[str, Any]:
    """The per-owner name-uniqueness guard (``{ ...guardKey, projectId: pid }`` in ``registry.create``)."""
    return {**schema.name_guard_key(owner_sub, name_fold), "projectId": pid}


@dataclass(frozen=True)
class SeedResult:
    pid: str
    created: bool  # False → META already existed (idempotent re-seed)


def seed_project(
    client: Any, table_name: str, *, pid: str, name: str, owner_sub: str, owner_display: str, at: str,
    description: str = "", tags: Optional[list] = None, indexing: bool = False, graph: Optional[bool] = None,
) -> SeedResult:
    """Write META + owner MEMBER + mirror + name-guard in one idempotent transaction (design Part 1).

    ``client`` is a DynamoDB *client* (``table.meta.client`` or ``boto3.client('dynamodb')`` wrapped by a
    resource is fine; we use the resource serializer-free path via the resource Table in tests). Returns
    ``created=False`` when META already exists (the whole transaction is cancelled and nothing changes).
    """
    name_fold = schema.fold_name(name)
    items = [
        meta_item(pid, name, name_fold, owner_sub, False, at, description=description, tags=tags,
                  indexing=indexing, graph=graph),
        member_item(pid, owner_sub, "owner", owner_display, at, owner_sub),
        mirror_item(owner_sub, pid),
        name_guard_item(owner_sub, name_fold, pid),
    ]
    ops = [{"Put": {"TableName": table_name, "Item": it,
                    "ConditionExpression": "attribute_not_exists(PK)"}} for it in items]
    try:
        client.transact_write_items(TransactItems=ops)
        return SeedResult(pid=pid, created=True)
    except ClientError as exc:
        if _is_cancelled(exc):
            return SeedResult(pid=pid, created=False)
        raise


def _is_cancelled(exc: ClientError) -> bool:
    code = exc.response.get("Error", {}).get("Code", "")
    return code in ("TransactionCanceledException", "ConditionalCheckFailedException")
