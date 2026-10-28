"""Lineage edge records in the projects table — the source of truth for "made from" (``projects.lineageEdges``).

The item shapes and why they are two items per edge are in :mod:`projects.lineage_contract`. This module is the
ONLY writer and reader of them:

- :meth:`LineageEdges.put_edge` writes ``UP#<child>#<parent>`` and ``DOWN#<parent>#<child>`` in one transaction,
  idempotently: a replayed message, the turn and job paths both seeing one output, a backfill over an edge that
  already exists — all land on the same two items. The first writer's ``tool``/``jobId``/``settings`` stay; a new
  source time range is appended once (``rangeKeys`` set); an OBSERVED edge clears ``inferred``, an inferred one
  never overwrites an observed one.
- :meth:`add_derived_from` unions parents into the ASSET row's ``derivedFrom`` after registration (the list stays
  for compatibility), conditioned on the list it read so concurrent writers never lose an id.
- ORIGIN guards map a third-party URL an asset was mirrored from to the asset.
- A dropped edge (an input that resolved to no asset) is a ``LINEAGEGAP#`` item with its reason, counted once in
  ``LINEAGE#STATS.droppedEdges``.

Project purge deletes every item of the partition, so edges, guards and gaps go with the Project. Edges to an
asset that is later removed stay: the ASSET row is kept as ``removed``, and readers show the node as such.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from projects import lineage_contract as lc
from projects import schema
from projects.ids import is_project_id

log = logging.getLogger(__name__)

__all__ = ["LineageEdges", "ddb_value"]

_MAX_QUERY_PAGES = 50


def ddb_value(v: Any) -> Any:
    """A value DynamoDB accepts: floats as ``Decimal`` (boto3 refuses ``float``), recursively."""
    from decimal import Decimal
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, float):
        return Decimal(str(v))
    if isinstance(v, Mapping):
        return {k: ddb_value(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [ddb_value(x) for x in v]
    return v


def _now_iso(clock) -> str:
    return datetime.fromtimestamp(clock(), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _canceled(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") in ("TransactionCanceledException", "ConditionalCheckFailedException")


class LineageEdges:
    def __init__(self, table: Any, *, clock=time.time):
        self.table = table
        self.client = table.meta.client
        self.name = table.name
        self._clock = clock

    # ------------------------------------------------------------------ edges
    def put_edge(self, pid: str, child: str, parent: str, *, tool: Optional[str] = None,
                 job_id: Optional[str] = None, turn_id: Optional[str] = None, session_id: Optional[str] = None,
                 via: Optional[str] = None, ranges: Iterable[Mapping] = (), settings: Optional[Mapping] = None,
                 inferred: bool = False) -> bool:
        """Write (or merge into) the edge ``parent → child``. Returns False for a refused edge (self, malformed,
        a direct cycle); True otherwise. Raises on a DynamoDB failure other than an idempotent no-op."""
        if not (is_project_id(pid) and schema.is_asset_id(child) and (schema.is_asset_id(parent) or lc.is_external_id(parent))) or child == parent:
            return False
        pk = schema.project_pk(pid)
        if self._exists(pk, lc.edge_up_sk(parent, child)):  # child is already parent's parent: a cycle
            log.warning("lineage: refused cycle edge %s -> %s in %s", parent, child, pid)
            return False
        names: dict[str, str] = {"#c": "child", "#p": "parent", "#ca": "createdAt"}
        values: dict[str, Any] = {":c": child, ":p": parent, ":now": _now_iso(self._clock)}
        sets = ["#c = :c", "#p = :p", "#ca = if_not_exists(#ca, :now)"]
        for attr, val in (("tool", tool), ("jobId", job_id), ("turnId", turn_id), ("sessionId", session_id),
                          ("via", via if via in lc.VIA else None)):
            if isinstance(val, str) and val:
                names[f"#{attr}"] = attr
                values[f":{attr}"] = val[:512]
                sets.append(f"#{attr} = if_not_exists(#{attr}, :{attr})")
        clean = lc.sanitize_settings(settings) if settings else {}
        if clean:
            names["#st"] = "settings"
            values[":st"] = ddb_value(clean)
            sets.append("#st = if_not_exists(#st, :st)")
        expr = "SET " + ", ".join(sets)
        cond = None
        if inferred:
            names["#inf"] = "inferred"
            values[":t"] = True
            expr += ", #inf = :t"
            cond = "attribute_not_exists(PK)"  # an inferred edge never touches an existing (observed) one
        else:
            names["#inf"] = "inferred"
            expr += " REMOVE #inf"

        def upd(sk: str) -> dict:
            op = {"TableName": self.name, "Key": {"PK": pk, "SK": sk}, "UpdateExpression": expr,
                  "ExpressionAttributeNames": names, "ExpressionAttributeValues": values}
            if cond:
                op["ConditionExpression"] = cond
            return {"Update": op}

        try:
            self.client.transact_write_items(TransactItems=[upd(lc.edge_up_sk(child, parent)),
                                                            upd(lc.edge_down_sk(parent, child))])
        except ClientError as exc:
            if not (inferred and _canceled(exc)):
                raise
            return True  # the edge already exists (observed or inferred): nothing to add
        for r in list(ranges or ())[: lc.MAX_RANGES_PER_EDGE]:
            self._add_range(pk, child, parent, r)
        return True

    def _add_range(self, pk: str, child: str, parent: str, r: Mapping) -> None:
        nr = lc.normalize_range(r.get("start"), r.get("end"), r.get("unit", "s"))
        if not nr:
            return
        rk = lc.range_key(nr)
        op = {
            "UpdateExpression": "SET #r = list_append(if_not_exists(#r, :e), :r) ADD #rk :rks",
            "ConditionExpression": "(attribute_not_exists(#rk) OR NOT contains(#rk, :k)) AND "
                                   "(attribute_not_exists(#r) OR size(#r) < :max)",
            "ExpressionAttributeNames": {"#r": "ranges", "#rk": "rangeKeys"},
            "ExpressionAttributeValues": {":e": [], ":r": [ddb_value(nr)], ":rks": {rk}, ":k": rk,
                                          ":max": lc.MAX_RANGES_PER_EDGE},
        }
        try:
            self.client.transact_write_items(TransactItems=[
                {"Update": {"TableName": self.name, "Key": {"PK": pk, "SK": lc.edge_up_sk(child, parent)}, **op}},
                {"Update": {"TableName": self.name, "Key": {"PK": pk, "SK": lc.edge_down_sk(parent, child)}, **op}},
            ])
        except ClientError as exc:
            if not _canceled(exc):
                raise

    def _exists(self, pk: str, sk: str) -> bool:
        return bool(self.table.get_item(Key={"PK": pk, "SK": sk}, ProjectionExpression="PK",
                                        ConsistentRead=True).get("Item"))

    def _query(self, pid: str, prefix: str, limit: int) -> list[dict]:
        items: list[dict] = []
        kwargs: dict = {"KeyConditionExpression": Key("PK").eq(schema.project_pk(pid)) & Key("SK").begins_with(prefix),
                        "ConsistentRead": True}
        for _ in range(_MAX_QUERY_PAGES):
            resp = self.table.query(**kwargs)
            items.extend(resp.get("Items") or [])
            if len(items) >= limit or not resp.get("LastEvaluatedKey"):
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return items[:limit]

    def parents_of(self, pid: str, child: str, limit: int = 1000) -> list[dict]:
        """The UP edges of ``child`` (what it was made from)."""
        if not (is_project_id(pid) and lc.is_node_id(child)):
            return []
        return self._query(pid, f"{lc.EDGE_UP_PREFIX}{child}#", limit)

    def children_of(self, pid: str, parent: str, limit: int = 1000) -> list[dict]:
        """The DOWN edges of ``parent`` (where it was used)."""
        if not (is_project_id(pid) and lc.is_node_id(parent)):
            return []
        return self._query(pid, f"{lc.EDGE_DOWN_PREFIX}{parent}#", limit)

    def all_edges(self, pid: str, limit: int = 20000) -> list[dict]:
        """Every edge of the Project, once (its UP items)."""
        if not is_project_id(pid):
            return []
        return self._query(pid, lc.EDGE_UP_PREFIX, limit)

    # ------------------------------------------------------------------ derivedFrom (compatibility)
    def add_derived_from(self, pid: str, asset_id: str, parents: Sequence[str]) -> list[str]:
        """Union ``parents`` into the ASSET row's ``derivedFrom`` (bounded), idempotently. Returns the ids added.

        Conditioned on the list it read (optimistic, 3 attempts), so two concurrent writers never lose an id; a
        missing row is a no-op."""
        if not (is_project_id(pid) and schema.is_asset_id(asset_id)):
            return []
        wanted = [p for p in dict.fromkeys(parents) if schema.is_asset_id(p) and p != asset_id]
        if not wanted:
            return []
        key = schema.asset_key(pid, asset_id)
        for attempt in range(5):
            if attempt:
                time.sleep(0.05 * attempt)  # a registration transaction on the row (staging 2026-10-23) settles fast
            row = self.table.get_item(Key=key, ProjectionExpression="derivedFrom, SK", ConsistentRead=True).get("Item")
            if not row:
                return []
            cur = [p for p in (row.get("derivedFrom") or []) if isinstance(p, str)]
            add = [p for p in wanted if p not in cur]
            new = (cur + add)[: lc.MAX_ROW_DERIVED_FROM]
            added = [p for p in add if p in new]
            if not added:
                return []
            try:
                if cur:
                    self.table.update_item(Key=key, UpdateExpression="SET derivedFrom = :n",
                                           ConditionExpression="attribute_exists(SK) AND derivedFrom = :o",
                                           ExpressionAttributeValues={":n": new, ":o": cur})
                else:
                    self.table.update_item(Key=key, UpdateExpression="SET derivedFrom = :n",
                                           ConditionExpression="attribute_exists(SK) AND (attribute_not_exists("
                                                               "derivedFrom) OR size(derivedFrom) = :z)",
                                           ExpressionAttributeValues={":n": new, ":z": 0})
                return added
            except ClientError as exc:
                if not (_canceled(exc) or exc.response.get("Error", {}).get("Code") == "TransactionConflictException"):
                    raise
        log.warning("lineage: derivedFrom of %s in %s kept changing; %d parent(s) not added", asset_id, pid,
                    len(wanted))
        return []

    # ------------------------------------------------------------------ external sources and roots
    def put_external(self, pid: str, ref: str, *, tool: Optional[str] = None, session_id: Optional[str] = None,
                     turn_id: Optional[str] = None, job_id: Optional[str] = None) -> Optional[str]:
        """Record (first writer wins) the external source ``ref`` — a URL or a bucket we do not own — and return its
        ``ext:<hash>`` id. Only the credential-free form is stored (``lineage_contract.external_url``)."""
        url = lc.external_url(ref)
        ext = lc.external_id(ref)
        if not (url and ext and is_project_id(pid)):
            return None
        names = {"#u": "url", "#h": "host", "#k": "kind", "#f": "firstSeenAt", "#l": "license", "#i": "sourceId"}
        values: dict[str, Any] = {":u": url, ":h": lc.external_host(url), ":k": "s3" if url.startswith("s3://") else "url",
                                  ":f": _now_iso(self._clock), ":l": "unknown", ":i": ext}
        sets = ["#u = :u", "#h = :h", "#k = :k", "#i = :i", "#f = if_not_exists(#f, :f)", "#l = if_not_exists(#l, :l)"]
        for attr, val in (("tool", tool), ("sessionId", session_id), ("turnId", turn_id), ("jobId", job_id)):
            if isinstance(val, str) and val:
                names[f"#{attr}"] = attr
                values[f":{attr}"] = val[:512]
                sets.append(f"#{attr} = if_not_exists(#{attr}, :{attr})")
        self.table.update_item(Key={"PK": schema.project_pk(pid), "SK": lc.EXTSRC_SK_PREFIX + ext[4:]},
                               UpdateExpression="SET " + ", ".join(sets), ExpressionAttributeNames=names,
                               ExpressionAttributeValues=values)
        return ext

    def externals(self, pid: str, ids: Sequence[str]) -> dict[str, dict]:
        """``{ext_id: EXTSRC item}`` for the external ids asked for (one BatchGet per 100)."""
        want = [i for i in dict.fromkeys(ids) if lc.is_external_id(i)]
        out: dict[str, dict] = {}
        for i in range(0, len(want), 100):
            keys = [{"PK": schema.project_pk(pid), "SK": lc.EXTSRC_SK_PREFIX + x[4:]} for x in want[i:i + 100]]
            resp = self.client.batch_get_item(RequestItems={self.name: {"Keys": keys, "ConsistentRead": True}})
            for it in resp.get("Responses", {}).get(self.name, []):
                out[str(it.get("sourceId"))] = it
        return out

    def put_root(self, pid: str, asset_id: str, kind: str, *, tool: Optional[str] = None,
                 session_id: Optional[str] = None, turn_id: Optional[str] = None,
                 candidates: Sequence[str] = ()) -> None:
        """Evidence that ``asset_id`` is a lineage ROOT although generated: ``kind="scratch"`` (its producing step
        read no media at all) or ``kind="external_ambiguous"`` (downloaded from outside, but from which of the
        ``candidates`` URLs could not be decided). First writer wins."""
        if not (is_project_id(pid) and schema.is_asset_id(asset_id)):
            return
        item = {"PK": schema.project_pk(pid), "SK": lc.ROOT_SK_PREFIX + asset_id, "assetId": asset_id, "kind": kind,
                "at": _now_iso(self._clock)}
        urls = list(dict.fromkeys(u for u in (lc.external_url(c) for c in candidates) if u))[:10]
        if urls:
            item["candidates"] = urls
        for k, v in (("tool", tool), ("sessionId", session_id), ("turnId", turn_id)):
            if isinstance(v, str) and v:
                item[k] = v[:512]
        try:
            self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
        except ClientError as exc:
            if not _canceled(exc):
                raise

    def roots(self, pid: str, limit: int = 20000) -> dict[str, dict]:
        return {str(it.get("assetId")): it for it in self._query(pid, lc.ROOT_SK_PREFIX, limit)} \
            if is_project_id(pid) else {}

    # ------------------------------------------------------------------ aliases
    def put_alias(self, pid: str, bucket: str, key: str, asset_id: str) -> bool:
        """Record that ``s3://bucket/key`` holds a copy of ``asset_id`` (first writer wins). True when written."""
        if not (is_project_id(pid) and schema.is_asset_id(asset_id) and bucket and key):
            return False
        try:
            self.table.put_item(Item={"PK": schema.project_pk(pid), "SK": lc.alias_sk(bucket, key),
                                      "assetId": asset_id, "location": f"s3://{bucket}/{key}"[:1024],
                                      "createdAt": _now_iso(self._clock)},
                                ConditionExpression="attribute_not_exists(PK)")
            return True
        except ClientError as exc:
            if not _canceled(exc):
                raise
            return False

    def alias_of(self, pid: str, bucket: str, key: str) -> Optional[str]:
        if not (is_project_id(pid) and bucket and key):
            return None
        item = self.table.get_item(Key={"PK": schema.project_pk(pid), "SK": lc.alias_sk(bucket, key)},
                                   ConsistentRead=True).get("Item") or {}
        aid = item.get("assetId")
        return aid if schema.is_asset_id(aid) else None

    # ------------------------------------------------------------------ origins
    def put_origin(self, pid: str, url: str, asset_id: str) -> None:
        sk = lc.origin_sk(url)
        if not (sk and is_project_id(pid) and schema.is_asset_id(asset_id)):
            return
        try:
            self.table.put_item(Item={"PK": schema.project_pk(pid), "SK": sk, "assetId": asset_id,
                                      "origin": lc.normalize_origin_url(url)},
                                ConditionExpression="attribute_not_exists(PK)")
        except ClientError as exc:
            if not _canceled(exc):
                raise

    def asset_for_origin(self, pid: str, url: str) -> Optional[str]:
        sk = lc.origin_sk(url)
        if not (sk and is_project_id(pid)):
            return None
        item = self.table.get_item(Key={"PK": schema.project_pk(pid), "SK": sk}, ConsistentRead=True).get("Item")
        aid = (item or {}).get("assetId")
        return aid if schema.is_asset_id(aid) else None

    # ------------------------------------------------------------------ gaps (dropped edges)
    def record_gap(self, pid: str, child: str, ref: str, reason: str, *, tool: Optional[str] = None,
                   job_id: Optional[str] = None, session_id: Optional[str] = None) -> bool:
        """Record one dropped edge (logged by the caller). Counted once per (child, ref, reason)."""
        if not (is_project_id(pid) and schema.is_asset_id(child)):
            return False
        safe = lc.sanitize_ref(ref) or "(unnamed)"
        item = {"PK": schema.project_pk(pid), "SK": lc.gap_sk(child, safe, reason), "child": child, "ref": safe,
                "reason": reason[:64], "at": _now_iso(self._clock)}
        for k, v in (("tool", tool), ("jobId", job_id), ("sessionId", session_id)):
            if isinstance(v, str) and v:
                item[k] = v[:512]
        try:
            self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
        except ClientError as exc:
            if not _canceled(exc):
                raise
            return False
        self.table.update_item(Key={"PK": schema.project_pk(pid), "SK": lc.STATS_SK},
                               UpdateExpression="ADD droppedEdges :one", ExpressionAttributeValues={":one": 1})
        return True

    def gaps(self, pid: str, limit: int = 200) -> list[dict]:
        return self._query(pid, lc.GAP_SK_PREFIX, limit) if is_project_id(pid) else []

    def stats(self, pid: str) -> dict:
        if not is_project_id(pid):
            return {}
        item = self.table.get_item(Key={"PK": schema.project_pk(pid), "SK": lc.STATS_SK},
                                   ConsistentRead=True).get("Item") or {}
        return {"droppedEdges": int(item.get("droppedEdges") or 0)}
