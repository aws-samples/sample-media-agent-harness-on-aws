"""The lineage read model of one Project (``projects.lineageEdges``): ancestors, descendants, or the whole graph.

Backs the Asset_Service ``lineage`` command. Edges come from the edge records (``ingestion.lineage_edges``), unioned
with the ASSET rows' ``derivedFrom`` lists so a row registered before edges existed (and not yet backfilled) still
shows its parents. Nodes are ASSET rows, read in batches, each checked exactly like an asset read:

* a ``visibility=session`` ("this chat only") asset is shown only to the actor who added it, and only when the
  request is for that chat (``sessionId``); for anyone else it is NOT a node at all, and edges through it are
  contracted (``parent → hidden → child`` is reported as ``parent → child`` with ``contracted: true``), so a chain
  never looks broken and nothing about the hidden file leaks;
* a removed asset stays a node (``status: removed``, ``removed: true``): its edges are history;
* an id with no row is a ``missing`` ghost.

``stage`` is derived from the edges: ``source`` (feeds something, made from nothing), ``intermediate``, ``final``
(made from something, feeds nothing) or ``standalone``.
"""
from __future__ import annotations

from collections import deque
from typing import Any, Iterable, Mapping, Optional

from boto3.dynamodb.conditions import Key

from projects import lineage_contract as lc
from projects import schema

from .lineage_edges import LineageEdges

__all__ = ["DIRECTIONS", "MAX_DEPTH", "MAX_NODES", "LineageReader", "visible"]

DIRECTIONS = ("ancestors", "descendants", "both", "project")
MAX_DEPTH = 50
DEFAULT_DEPTH = 10
MAX_NODES = 2000
DEFAULT_NODES = 500
_PROJECT_ROW_SCAN_MAX = 5000
_GENERATED_SOURCES = frozenset({"session", "m2c", "generated"})
_NODE_ATTRS = ("assetId", "kind", "displayName", "status", "role", "source", "folder", "sessionId", "producer",
               "derivedFrom", "jobId", "registeredAt", "visibility", "addedBy", "addedByDisplay", "sourceUri")
_EDGE_ATTRS = ("tool", "jobId", "turnId", "sessionId", "via", "ranges", "settings", "inferred", "createdAt")


def _s(v: Any) -> Optional[str]:
    return v if isinstance(v, str) and v else None


def visible(row: Optional[Mapping[str, Any]], actor: str, session_id: Optional[str]) -> bool:
    """Whether ``actor`` may see this asset in lineage (``None`` row = a missing ghost, which is shown)."""
    if row is None:
        return True
    if row.get("visibility") != "session":
        return True
    return row.get("addedBy") == actor and bool(session_id) and row.get("sessionId") == session_id


def _num(v: Any) -> Any:
    try:
        from decimal import Decimal
        if isinstance(v, Decimal):
            return int(v) if v == v.to_integral_value() else float(v)
    except Exception:  # noqa: BLE001
        pass
    return v


def _clean(v: Any) -> Any:
    if isinstance(v, Mapping):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    if isinstance(v, (set, frozenset)):
        return sorted(_clean(x) for x in v)
    return _num(v)


class LineageReader:
    def __init__(self, table: Any, store: Any):
        self.table = table
        self.store = store
        self.edges = LineageEdges(table)

    # ------------------------------------------------------------------ reads
    def _rows(self, pid: str, ids: Iterable[str]) -> dict[str, dict]:
        want = [i for i in dict.fromkeys(ids) if schema.is_asset_id(i)]
        return self.store.batch_get_assets(pid, want) if want else {}

    def _all_rows(self, pid: str) -> tuple[list[dict], bool]:
        rows: list[dict] = []
        kwargs: dict = {"KeyConditionExpression": Key("PK").eq(schema.project_pk(pid))
                        & Key("SK").begins_with(schema.ASSET_SK_PREFIX), "ConsistentRead": True,
                        "ProjectionExpression": ", ".join(f"#a{i}" for i in range(len(_NODE_ATTRS))),
                        "ExpressionAttributeNames": {f"#a{i}": a for i, a in enumerate(_NODE_ATTRS)}}
        while True:
            resp = self.table.query(**kwargs)
            rows.extend(resp.get("Items") or [])
            if len(rows) >= _PROJECT_ROW_SCAN_MAX:
                return rows[:_PROJECT_ROW_SCAN_MAX], True
            if not resp.get("LastEvaluatedKey"):
                return rows, False
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    @staticmethod
    def _edge_of(item: Mapping[str, Any]) -> Optional[dict]:
        parsed = lc.parse_edge_sk(item.get("SK"))
        if not parsed:
            return None
        d, a, b = parsed
        child, parent = (a, b) if d == "up" else (b, a)
        e = {"from": parent, "to": child, "recorded": "edge"}
        for k in _EDGE_ATTRS:
            v = item.get(k)
            if v not in (None, "", [], {}):
                e[k] = _clean(v)
        return e

    # ------------------------------------------------------------------ the command
    def lineage(self, pid: str, actor: str, *, asset_id: Optional[str] = None, direction: str = "both",
                depth: int = DEFAULT_DEPTH, max_nodes: int = DEFAULT_NODES,
                session_id: Optional[str] = None) -> dict:
        edges: dict[tuple[str, str], dict] = {}
        rows: dict[str, dict] = {}
        truncated = False
        distances: dict[str, int] = {}
        all_rows: Optional[list[dict]] = None

        def add_edge(e: Mapping[str, Any]) -> None:
            k = (e["from"], e["to"])
            if k not in edges or edges[k].get("recorded") != "edge":
                edges[k] = dict(e)

        if direction == "project" or not asset_id:
            for it in self.edges.all_edges(pid, limit=max(1, max_nodes) * 20):
                e = self._edge_of(it)
                if e:
                    add_edge(e)
            all_rows, truncated = self._all_rows(pid)
            for r in all_rows:
                aid = _s(r.get("assetId"))
                if not aid:
                    continue
                rows[aid] = r
                for p in r.get("derivedFrom") or []:
                    if isinstance(p, str) and p != aid:
                        add_edge({"from": p, "to": aid, "recorded": "derivedFrom"})
            linked = {x for k in edges for x in k}
            node_ids = sorted(linked, key=lambda i: str((rows.get(i) or {}).get("registeredAt") or ""), reverse=True)
            if len(node_ids) > max_nodes:
                keep = set(node_ids[:max_nodes])
                edges = {k: v for k, v in edges.items() if k[0] in keep and k[1] in keep}
                truncated = True
            missing = [i for i in linked if i not in rows]
            rows.update(self._rows(pid, missing))
        else:
            focus_row = self.store.get_asset(pid, asset_id)
            if focus_row is None or not visible(focus_row, actor, session_id):
                raise LookupError("asset_not_found")
            rows[asset_id] = focus_row
            distances[asset_id] = 0
            for sign, walk in ((-1, "up"), (1, "down")):
                if (direction == "ancestors" and walk == "down") or (direction == "descendants" and walk == "up"):
                    continue
                q: deque[tuple[str, int]] = deque([(asset_id, 0)])
                seen = {asset_id}
                while q:
                    node, d = q.popleft()
                    if d >= depth:
                        truncated = truncated or self._has_more(pid, node, walk, rows)
                        continue
                    nxt: list[str] = []
                    items = (self.edges.parents_of(pid, node) if walk == "up" else self.edges.children_of(pid, node))
                    for it in items:
                        e = self._edge_of(it)
                        if e:
                            add_edge(e)
                            nxt.append(e["from"] if walk == "up" else e["to"])
                    if walk == "up":
                        for p in (rows.get(node) or {}).get("derivedFrom") or []:
                            if isinstance(p, str) and p != node:
                                add_edge({"from": p, "to": node, "recorded": "derivedFrom"})
                                nxt.append(p)
                    fresh = [n for n in dict.fromkeys(nxt) if n not in seen]
                    if len(seen) + len(fresh) > max_nodes:
                        fresh = fresh[: max(0, max_nodes - len(seen))]
                        truncated = True
                    rows.update(self._rows(pid, [n for n in fresh if n not in rows]))
                    for n in fresh:
                        seen.add(n)
                        distances.setdefault(n, sign * (d + 1))
                        q.append((n, d + 1))
            keep = set(distances)
            edges = {k: v for k, v in edges.items() if k[0] in keep and k[1] in keep}

        return self._view(pid, actor, session_id, rows, edges, distances, truncated, asset_id, direction,
                          all_rows)

    def _has_more(self, pid: str, node: str, walk: str, rows: Mapping[str, Mapping]) -> bool:
        if walk == "up" and (rows.get(node) or {}).get("derivedFrom"):
            return True
        items = self.edges.parents_of(pid, node, limit=1) if walk == "up" else self.edges.children_of(pid, node, limit=1)
        return bool(items)

    # ------------------------------------------------------------------ view
    def _view(self, pid: str, actor: str, session_id: Optional[str], rows: dict, edges: dict, distances: dict,
              truncated: bool, focus: Optional[str], direction: str, all_rows: Optional[list[dict]]) -> dict:
        ids = {x for k in edges for x in k} | set(distances)
        hidden = {i for i in ids if not visible(rows.get(i), actor, session_id)}
        # Contract every path through hidden nodes into a direct edge between the visible ends.
        parents: dict[str, list[str]] = {}
        children: dict[str, list[str]] = {}
        for (p, c) in edges:
            parents.setdefault(c, []).append(p)
            children.setdefault(p, []).append(c)
        out_edges: dict[tuple[str, str], dict] = {k: v for k, v in edges.items()
                                                  if k[0] not in hidden and k[1] not in hidden}
        for h in hidden:
            ups = self._visible_ends(h, parents, hidden)
            downs = self._visible_ends(h, children, hidden)
            for p in ups:
                for c in downs:
                    if p != c and (p, c) not in out_edges:
                        out_edges[(p, c)] = {"from": p, "to": c, "contracted": True}
        vis_ids = sorted(i for i in ids if i not in hidden)
        par_v: dict[str, set] = {}
        chi_v: dict[str, set] = {}
        for (p, c) in out_edges:
            par_v.setdefault(c, set()).add(p)
            chi_v.setdefault(p, set()).add(c)

        def stage(i: str) -> str:
            live_children = [c for c in chi_v.get(i, ()) if str((rows.get(c) or {}).get("status")) != "removed"]
            if par_v.get(i) and live_children:
                return "intermediate"
            if par_v.get(i):
                return "final"
            if live_children:
                return "source"
            return "standalone"

        externals = self.edges.externals(pid, [i for i in vis_ids if lc.is_external_id(i)])
        roots = self.edges.roots(pid) if any(not lc.is_external_id(i) for i in vis_ids) else {}

        def origin(i: str, r: Optional[Mapping]) -> Optional[str]:
            """Where a file's lineage begins: upload | import | external (made from outside media only) |
            derived | scratch (made from nothing) | unrecorded (generated, inputs not recorded)."""
            ps = par_v.get(i, set())
            if ps:
                return "external" if all(lc.is_external_id(x) for x in ps) else "derived"
            src = (r or {}).get("source")
            if src == "upload":
                return "upload"
            if src == "s3":
                return "import"
            if src in _GENERATED_SOURCES:
                if i in roots:
                    return "external_undecided" if roots[i].get("kind") == lc.AMBIGUOUS_EXTERNAL else "scratch"
                return "unrecorded"
            return None

        nodes = []
        for i in vis_ids:
            r = rows.get(i)
            if lc.is_external_id(i):
                x = externals.get(i) or {}
                url = _s(x.get("url")) or ""
                host = _s(x.get("host")) or lc.external_host(url)
                n_ext: dict[str, Any] = {
                    "id": i, "stage": "source", "parents": 0, "children": len(chi_v.get(i, ())), "kind": "external",
                    "displayName": (f"From {url}" if url.startswith("s3://") else f"Downloaded from {host}") if url
                    else "External source", "origin": "external", "external": True, "url": url, "host": host,
                    "license": _s(x.get("license")) or "unknown"}
                for k in ("firstSeenAt", "tool", "sessionId", "turnId", "jobId"):
                    if _s(x.get(k)):
                        n_ext[k] = x[k]
                if i in distances:
                    n_ext["distance"] = distances[i]
                nodes.append(n_ext)
                continue
            n: dict[str, Any] = {"id": i, "stage": stage(i), "parents": len(par_v.get(i, ())),
                                 "children": len(chi_v.get(i, ()))}
            if i in distances:
                n["distance"] = distances[i]
            if r is None:
                n.update({"kind": i.split(":", 1)[0], "displayName": "Missing asset", "missing": True})
            else:
                prod = r.get("producer") if isinstance(r.get("producer"), Mapping) else {}
                n.update({"kind": _s(r.get("kind")), "displayName": _s(r.get("displayName")) or i,
                          "role": _s(r.get("role")) or "source", "status": _s(r.get("status")),
                          "source": _s(r.get("source")), "folder": _s(r.get("folder")) or "",
                          "registeredAt": _s(r.get("registeredAt"))})
                for k, v in (("sessionId", _s(r.get("sessionId")) or _s(prod.get("sessionId"))),
                             ("tool", _s(prod.get("tool"))), ("jobId", _s(r.get("jobId")) or _s(prod.get("jobId"))),
                             ("turnId", _s(prod.get("turnId")))):
                    if v:
                        n[k] = v
                if r.get("status") == "removed":
                    n["removed"] = True
                if r.get("visibility") == "session":
                    n["visibility"] = "session"
                o = origin(i, r)
                if o:
                    n["origin"] = o
                if o == "upload" and _s(r.get("addedByDisplay")):
                    n["addedByDisplay"] = r["addedByDisplay"]
                if o == "import" and _s(r.get("sourceUri")):
                    n["importedFrom"] = lc.external_url(r["sourceUri"]) or ""
                if o == "external_undecided":
                    n["candidates"] = [u for u in roots[i].get("candidates") or [] if isinstance(u, str)][:10]
            nodes.append(n)
        gaps = []
        open_gaps = [g for g in self.edges.gaps(pid, limit=500) if not g.get("resolvedBy")]
        node_set = set(vis_ids)
        for g in open_gaps:
            child = g.get("child")
            if child in hidden or (focus and child not in node_set):
                continue
            gaps.append({k: _clean(g.get(k)) for k in ("child", "ref", "reason", "tool", "jobId", "at") if g.get(k)})
        stats: dict[str, Any] = {"nodes": len(nodes), "edges": len(out_edges),
                                 "droppedEdges": self.edges.stats(pid).get("droppedEdges", 0)}
        if all_rows is not None:
            generated = [r for r in all_rows if r.get("source") in _GENERATED_SOURCES and r.get("status") != "removed"
                         and visible(r, actor, session_id)]
            with_parents = {c for (_, c) in edges}
            ext_children = {c for (p, c) in edges if lc.is_external_id(p)}
            all_roots = roots or self.edges.roots(pid)
            vis_rows = [r for r in all_rows if r.get("status") != "removed" and visible(r, actor, session_id)]
            unrecorded = [r for r in generated if r.get("assetId") not in with_parents
                          and r.get("assetId") not in all_roots]
            stats["generated"] = len(generated)
            # Only GENUINE gaps: a generated file whose inputs were not recorded. A file made from nothing (scratch),
            # an upload, an import and a download from outside are roots, reported in `origins`, not as a problem.
            stats["generatedWithoutLineage"] = len(unrecorded)
            stats["origins"] = {
                "uploaded": sum(1 for r in vis_rows if r.get("source") == "upload"),
                "imported": sum(1 for r in vis_rows if r.get("source") == "s3"),
                "external": len({r.get("assetId") for r in vis_rows} & ext_children),
                "scratch": sum(1 for r in generated if r.get("assetId") in all_roots
                               and r.get("assetId") not in with_parents
                               and all_roots[r["assetId"]].get("kind") != lc.AMBIGUOUS_EXTERNAL),
                "externalUndecided": sum(1 for r in generated if r.get("assetId") in all_roots
                                         and r.get("assetId") not in with_parents
                                         and all_roots[r["assetId"]].get("kind") == lc.AMBIGUOUS_EXTERNAL),
                "unrecorded": len(unrecorded),
            }
            stats["externalSources"] = len({p for (p, _) in edges if lc.is_external_id(p)})
            stats["assets"] = sum(1 for r in all_rows if visible(r, actor, session_id))
        out: dict[str, Any] = {"nodes": nodes, "edges": [_clean(e) for e in out_edges.values()],
                               "gaps": gaps, "stats": stats, "truncated": truncated, "direction": direction}
        if focus:
            out["focus"] = focus
        return out

    @staticmethod
    def _visible_ends(start: str, adj: Mapping[str, list[str]], hidden: set) -> set:
        out: set = set()
        q = deque([start])
        seen = {start}
        while q:
            n = q.popleft()
            for m in adj.get(n, ()):
                if m in seen:
                    continue
                seen.add(m)
                if m in hidden:
                    q.append(m)
                else:
                    out.add(m)
        return out
