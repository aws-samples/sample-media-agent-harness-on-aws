"""Project seeding parity + idempotency (design Part 1).

The parity assertion is the guard against the AGENTS.md trap: parallel item-builders drift. ``_EXPECTED_META``
is transcribed from ``ui/server/projects/registry.ts :: metaItem`` — if the proxy gains a META field, this
test fails until :func:`preload.projects.meta_item` grows it too.
"""
import pytest
from botocore.exceptions import ClientError

from preload import projects as P
from projects import schema

# The exact non-key attributes ``registry.ts :: metaItem`` writes (graph is conditional, so excluded here).
_EXPECTED_META = {
    "projectId", "name", "nameFold", "ownerSub", "personal", "status", "memberCount", "createdAt",
    "activeAssetCount", "collectionState", "description", "tags", "indexing",
}

PID = "p" + "a" * 32
SUB = "sub-123"


def test_meta_fields_match_the_proxy():
    assert set(P.META_FIELDS) == _EXPECTED_META


def test_meta_item_shape_is_exactly_key_plus_fields():
    item = P.meta_item(PID, "Demo", schema.fold_name("Demo"), SUB, False, "2026-10-27T00:00:00Z",
                       description="d", tags=["t"], indexing=True)
    assert set(item) == {"PK", "SK"} | _EXPECTED_META
    assert item["PK"] == schema.project_pk(PID)
    assert item["SK"] == schema.META_SK
    assert item["status"] == "active"
    assert item["ownerSub"] == SUB
    assert item["personal"] is False
    assert item["memberCount"] == 0
    assert item["activeAssetCount"] == 0
    assert item["collectionState"] == "absent"
    assert item["indexing"] is True
    assert "graph" not in item


def test_graph_written_only_when_set():
    assert "graph" not in P.meta_item(PID, "D", "d", SUB, False, "t")
    assert P.meta_item(PID, "D", "d", SUB, False, "t", graph=True)["graph"] is True


def test_member_mirror_and_guard_shapes():
    m = P.member_item(PID, SUB, "owner", "owner@example.com", "t", SUB)
    assert m == {**schema.member_key(PID, SUB), "role": "owner", "display": "owner@example.com",
                 "addedAt": "t", "addedBy": SUB}
    assert P.mirror_item(SUB, PID) == {**schema.mirror_key(SUB, PID), "role": "owner"}
    g = P.name_guard_item(SUB, "demo", PID)
    assert g == {**schema.name_guard_key(SUB, "demo"), "projectId": PID}


class FakeDDBClient:
    """Minimal transact_write_items with attribute_not_exists(PK) enforcement."""

    def __init__(self):
        self.items: dict[tuple[str, str], dict] = {}
        self.calls = 0

    def transact_write_items(self, TransactItems):
        self.calls += 1
        # All-or-nothing: first check every condition, then apply.
        pending = []
        for op in TransactItems:
            put = op["Put"]
            item = put["Item"]
            pk, sk = item["PK"], item["SK"]
            if put.get("ConditionExpression") == "attribute_not_exists(PK)" and (pk, sk) in self.items:
                raise ClientError({"Error": {"Code": "TransactionCanceledException",
                                             "Message": "conditional"}}, "TransactWriteItems")
            pending.append(((pk, sk), item))
        for key, item in pending:
            self.items[key] = item


def test_seed_is_idempotent():
    client = FakeDDBClient()
    r1 = P.seed_project(client, "tbl", pid=PID, name="Demo", owner_sub=SUB,
                        owner_display="owner@example.com", at="2026-10-27T00:00:00Z", indexing=True)
    assert r1.created is True
    # Four rows written: META, MEMBER, mirror, name-guard.
    assert len(client.items) == 4
    r2 = P.seed_project(client, "tbl", pid=PID, name="Demo", owner_sub=SUB,
                        owner_display="owner@example.com", at="2026-10-27T00:00:00Z", indexing=True)
    assert r2.created is False          # re-seed: transaction cancelled, nothing added
    assert len(client.items) == 4


def test_seeded_meta_passes_a_proxy_style_access_check():
    client = FakeDDBClient()
    P.seed_project(client, "tbl", pid=PID, name="Demo", owner_sub=SUB, owner_display="o", at="t",
                   indexing=True)
    meta = client.items[(schema.project_pk(PID), schema.META_SK)]
    # The proxy admits a project only if active, owned, not a tombstone.
    assert meta["status"] == "active"
    assert meta["ownerSub"] == SUB
    assert "tombstone" not in meta
