"""Lineage capture at the registration chokepoint (``projects.lineageEdges``): turn every producer's ``lineage``
entries into edge records once the outputs they name are assets.

Called by GeneratedMediaFn (``handlers.generated_media``) AFTER a message's registrations, so an output made from
another output of the same message (a clip from a frame from a shot, all in one plan) finds its parent registered,
whatever order the candidates were registered in. Inputs resolve to assets through:

1. a ``projects/<pid>/<assetId>/…`` key (the asset's own bytes or rendition);
2. the SRC guard of the location (``s3://bucket/key``), which also covers M2C-bucket outputs and an
   ``artifacts/m2c/<bucket>/<key>`` reference to one;
3. the ORIGIN guard, for a third-party URL (fal.media) one of the Project's assets was mirrored from.

An input that resolves to nothing is a DROPPED edge: logged (``lineage_edge_dropped``) with its reason, recorded as
a gap, and — when it may yet be registered (a location, an external URL) — parked as a WAIT item, so the edge is
written the moment its parent becomes an asset of the Project (:meth:`LineageCapture.on_registered`).

Plan results (:func:`plan_entries`): a settled plan's ``result.json`` lists each node's artifacts and
``depends_on``; the chainer stamps each node's job-registry row with what it was HANDED (``inputRefs``: the
dependency / prior-phase deliverable uris it injected, the fast-path arguments) and what it MADE (``outputs``);
a Task-Agent node writes its own ledger to ``plans/<sid>/<plan>/<node>/lineage.json``. Precedence per output: the
node's ledger (observed), then the node's declared inputs, then — only when the node declared nothing — its
upstream nodes' outputs, marked ``inferred``. So a multi-hop plan (source → shots → frames → restyles → clip →
final) records each hop, not every output pointing at the original source.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Callable, Iterable, Mapping, Optional

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from projects import lineage_contract as lc
from projects import schema

from . import generated as gen
from .lineage_edges import LineageEdges, ddb_value

log = logging.getLogger(__name__)

__all__ = ["LineageCapture", "plan_entries", "shot_index", "shot_ranges", "WAIT_SK_PREFIX"]

WAIT_SK_PREFIX = "LINEAGEWAIT#"
ORIGIN_WAIT_SK_PREFIX = "LINEAGEORIGINWAIT#"
_M2C_REF_RE = re.compile(r"^artifacts/m2c/([a-z0-9][a-z0-9.-]{1,61}[a-z0-9])/(.+)$")
_RESOLVABLE_LATER = frozenset({"untracked_location", "external_url_unknown"})


def _wait_hash(ref: str) -> str:
    return hashlib.sha256(ref.encode("utf-8")).hexdigest()[:32]


class LineageCapture:
    def __init__(self, table: Any, store: Any, ingest_bucket: str, *, m2c_bucket: str = ""):
        self.table = table
        self.store = store
        self.edges = LineageEdges(table)
        self.bucket = ingest_bucket
        self.m2c_bucket = m2c_bucket
        self.stats = {"edges": 0, "dropped": 0, "derivedAdded": 0, "external": 0}
        self._owned: Optional[set[str]] = None

    # ------------------------------------------------------------------ resolution
    def location(self, uri: str) -> Optional[tuple[str, str]]:
        """``(bucket, key)`` an input names, an M2C reference resolved to the object it names."""
        loc = gen.parse_location(uri, self.bucket)
        if loc is None:
            return None
        bucket, key = loc
        m = _M2C_REF_RE.match(key) if bucket == self.bucket else None
        if m:
            return m.group(1), m.group(2)
        return bucket, key

    def resolve(self, pid: str, ref: str) -> tuple[Optional[str], str]:
        """``(asset_id, "")`` or ``(None, reason)``."""
        if not isinstance(ref, str) or not ref:
            return None, "not_a_location"
        loc = self.location(ref)
        if loc is not None:
            bucket, key = loc
            if bucket == self.bucket:
                aid = gen.asset_id_in_project_key(key, pid)
                if aid:
                    return aid, ""
            aid = self.store.asset_id_for_location(pid, bucket, key) or self.edges.alias_of(pid, bucket, key)
            return (aid, "") if aid else (None, "untracked_location")
        if lc.normalize_origin_url(ref):
            aid = self.edges.asset_for_origin(pid, ref)
            return (aid, "") if aid else (None, "external_url_unknown")
        return None, "not_a_location"

    def owned_buckets(self) -> set[str]:
        """Buckets whose objects are (or become) this deployment's assets: the Ingest bucket, the M2C proxy bucket,
        and the S3-location allowlist. An untracked object anywhere else came from outside."""
        if self._owned is None:
            from .registrar import allowlist_from_env

            self._owned = {b for b in (self.bucket, self.m2c_bucket, *allowlist_from_env()) if b}
        return self._owned

    def _is_external(self, ref: str, reason: str) -> bool:
        if reason == "external_url_unknown":
            return True
        if reason == "untracked_location":
            loc = self.location(ref)
            return loc is not None and loc[0] not in self.owned_buckets()
        return False

    def _wait_ref(self, ref: str) -> Optional[str]:
        """The identity a WAIT item is keyed on: ``s3://bucket/key`` for a location, the normalized URL otherwise."""
        loc = self.location(ref)
        if loc is not None:
            return f"s3://{loc[0]}/{loc[1]}"
        return lc.normalize_origin_url(ref)

    # ------------------------------------------------------------------ capture
    def capture(self, pid: str, child: str, entry: Mapping[str, Any], *, session_id: Optional[str],
                turn_id: Optional[str], job_id: Optional[str], default_via: str) -> list[str]:
        """Write the edges of one registered output. Returns the parent ids resolved. Never raises for one
        input: a failing input is logged and dropped, so the others still land."""
        tool = entry.get("tool")
        via = entry.get("via") or default_via
        job = entry.get("jobId") or job_id
        ranges_by_input: dict[str, list] = {}
        for r in entry.get("ranges") or []:
            ranges_by_input.setdefault(r.get("input"), []).append(r)
        ambiguous = [u.get("ref") for u in entry.get("unresolved") or []
                     if u.get("ref") and u.get("reason") == lc.AMBIGUOUS_EXTERNAL]
        refs = [*entry.get("inputs", []), *[u.get("ref") for u in entry.get("unresolved") or []
                                            if u.get("ref") and u.get("reason") != lc.AMBIGUOUS_EXTERNAL]]
        # A ref ends at whitespace or a backslash: a producer that scraped JSON-escaped text can hand over
        # `…/clip.mp4\\n\\n**clip2:**` as one string (staging plan 0cc48c4d).
        refs = [re.split(r"[\s\\]", r.strip(), maxsplit=1)[0] for r in refs if isinstance(r, str) and r.strip()]
        parents: list[str] = []
        for ref in dict.fromkeys(refs):
            aid, reason = self.resolve(pid, ref)
            if aid == child:
                continue
            if aid is None and reason == "external_url_unknown" and not lc.external_url(ref):
                continue  # a bare site root (`https://host/`) names no file: not a source, not a gap
            if aid is None and self._is_external(ref, reason):
                # Media from OUTSIDE the Project (the public internet, a bucket we do not own): a lineage root, not a
                # gap. Rights matter here, so it is a node of its own (host, first seen, tool, turn, license unknown).
                try:
                    aid = self.edges.put_external(pid, ref, tool=tool, session_id=session_id, turn_id=turn_id,
                                                  job_id=job)
                except ClientError as exc:
                    log.warning("lineage: external source of %s not recorded: %s", child, exc)
                    aid = None
                if aid:
                    self.stats["external"] = self.stats.get("external", 0) + 1
            if aid is None:
                self._drop(pid, child, ref, reason, tool=tool, job_id=job, session_id=session_id, entry=entry,
                           turn_id=turn_id, via=via)
                continue
            try:
                ok = self.edges.put_edge(pid, child, aid, tool=tool, job_id=job, turn_id=turn_id,
                                         session_id=session_id, via=via, ranges=ranges_by_input.get(ref, ()),
                                         settings=entry.get("settings"), inferred=bool(entry.get("inferred")))
            except ClientError as exc:
                log.warning("lineage: edge %s -> %s in %s not written: %s", aid, child, pid, exc)
                continue
            if not ok:
                self._drop(pid, child, ref, "cycle", tool=tool, job_id=job, session_id=session_id, entry=entry,
                           turn_id=turn_id, via=via)
                continue
            self.stats["edges"] += 1
            parents.append(aid)
        if not parents and ambiguous:
            # Downloaded from one of several URLs and the step did not say which: a root that names the candidates,
            # rather than a false parent (or a gap: nothing is missing that could ever be linked).
            try:
                self.edges.put_root(pid, child, lc.AMBIGUOUS_EXTERNAL, tool=tool, session_id=session_id,
                                    turn_id=turn_id, candidates=ambiguous)
                self.stats["ambiguous"] = self.stats.get("ambiguous", 0) + 1
            except ClientError as exc:
                log.warning("lineage: ambiguous source of %s not recorded: %s", child, exc)
        if not parents and not refs and not ambiguous and entry.get("scratch"):
            # The producing step read no media at all (a synthesized image, a generated report): a root.
            try:
                self.edges.put_root(pid, child, "scratch", tool=tool, session_id=session_id, turn_id=turn_id)
            except ClientError as exc:
                log.warning("lineage: root of %s not recorded: %s", child, exc)
        if parents:
            try:
                self.stats["derivedAdded"] += len(self.edges.add_derived_from(pid, child, parents))
            except ClientError as exc:
                log.warning("lineage: derivedFrom of %s not updated: %s", child, exc)
        origin = entry.get("origin")
        if origin:
            try:
                self.edges.put_origin(pid, origin, child)
                norm = lc.normalize_origin_url(origin)
                if norm and entry.get("output", "").startswith("s3://"):
                    parents.extend(self._resolve_parked_output(pid, child, norm, collect=True))
            except ClientError as exc:
                log.warning("lineage: origin of %s not recorded: %s", child, exc)
        return parents

    def _drop(self, pid: str, child: str, ref: str, reason: str, *, tool, job_id, session_id, entry, turn_id,
              via) -> None:
        safe = lc.sanitize_ref(ref)
        self.stats["dropped"] += 1
        log.info("lineage_edge_dropped %s", json.dumps({"projectId": pid, "child": child, "ref": safe,
                                                         "reason": reason, "tool": tool, "jobId": job_id}))
        try:
            self.edges.record_gap(pid, child, safe, reason, tool=tool, job_id=job_id, session_id=session_id)
            wait = self._wait_ref(ref) if reason in _RESOLVABLE_LATER else None
            if wait:
                ranges = [r for r in entry.get("ranges") or [] if r.get("input") == ref]
                item = {"PK": schema.project_pk(pid), "SK": f"{WAIT_SK_PREFIX}{_wait_hash(wait)}#{child}",
                        "child": child, "ref": safe, "reason": reason, "via": via}
                for k, v in (("tool", tool), ("jobId", job_id), ("sessionId", session_id), ("turnId", turn_id)):
                    if isinstance(v, str) and v:
                        item[k] = v[:512]
                if ranges:
                    item["ranges"] = [{k: r[k] for k in ("start", "end", "unit") if k in r} for r in ranges]
                if entry.get("settings"):
                    item["settings"] = lc.sanitize_settings(entry.get("settings"))
                if entry.get("inferred"):
                    item["inferred"] = True
                self.table.put_item(Item=ddb_value(item))
        except ClientError as exc:
            log.warning("lineage: gap of %s not recorded: %s", child, exc)

    def on_registered(self, pid: str, asset_id: str, *, bucket: str, key: str, origin: Optional[str] = None) -> int:
        """A new asset of the Project: write every edge that was waiting for it (an earlier output named this
        location or URL before it was registered). Returns how many were written."""
        n = 0
        for ref in [f"s3://{bucket}/{key}", lc.normalize_origin_url(origin) if origin else None]:
            if not ref:
                continue
            resp = self.table.query(
                KeyConditionExpression=Key("PK").eq(schema.project_pk(pid))
                & Key("SK").begins_with(f"{WAIT_SK_PREFIX}{_wait_hash(ref)}#"), ConsistentRead=True)
            for w in resp.get("Items") or []:
                if w.get("resolvedBy"):
                    continue
                child = w.get("child")
                try:
                    ok = self.edges.put_edge(pid, child, asset_id, tool=w.get("tool"), job_id=w.get("jobId"),
                                             turn_id=w.get("turnId"), session_id=w.get("sessionId"),
                                             via=w.get("via"), ranges=w.get("ranges") or (),
                                             settings=w.get("settings"), inferred=bool(w.get("inferred")))
                    if not ok:
                        continue
                    self.edges.add_derived_from(pid, child, [asset_id])
                    self.table.update_item(Key={"PK": w["PK"], "SK": w["SK"]},
                                           UpdateExpression="SET resolvedBy = :a", ExpressionAttributeValues={":a": asset_id})
                    self._resolve_gap(pid, child, w.get("ref") or "", w.get("reason") or "", asset_id)
                    n += 1
                except ClientError as exc:
                    log.warning("lineage: waiting edge %s -> %s not written: %s", asset_id, child, exc)
        if n:
            log.info("lineage: %d waiting edge(s) resolved by %s in %s", n, asset_id, pid)
            self.stats["edges"] += n
        return n

    # An OUTPUT whose bytes are not ours yet (a plan node's fal result, still a fal.media URL in the plan's result):
    # its entry waits under its URL, and becomes the lineage of the asset later mirrored from that URL.
    def park_output(self, pid: str, entry: Mapping[str, Any], *, session_id: Optional[str],
                    turn_id: Optional[str], job_id: Optional[str], default_via: str) -> bool:
        norm = lc.normalize_origin_url(entry.get("output"))
        if not norm:
            return False
        item = {"PK": schema.project_pk(pid), "SK": f"{ORIGIN_WAIT_SK_PREFIX}{_wait_hash(norm)}",
                "origin": norm, "entry": {**dict(entry), "via": entry.get("via") or default_via,
                                          "jobId": entry.get("jobId") or job_id}}
        for k, v in (("sessionId", session_id), ("turnId", turn_id)):
            if isinstance(v, str) and v:
                item[k] = v
        try:
            self.table.put_item(Item=ddb_value(item), ConditionExpression="attribute_not_exists(PK)")
            mirrored = self.edges.asset_for_origin(pid, norm)  # the copy already registered: resolve now
            if mirrored:
                self.stats["edges"] += self._resolve_parked_output(pid, mirrored, norm)
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                log.warning("lineage: parked output %s not recorded: %s", norm, exc)
            return False

    def _resolve_parked_output(self, pid: str, asset_id: str, norm: str, collect: bool = False):
        """Write the lineage parked under ``norm`` onto ``asset_id`` (the copy mirrored from it), once."""
        item = self.table.get_item(Key={"PK": schema.project_pk(pid),
                                        "SK": f"{ORIGIN_WAIT_SK_PREFIX}{_wait_hash(norm)}"},
                                   ConsistentRead=True).get("Item")
        entry = None
        if item and not item.get("resolvedBy") and isinstance(item.get("entry"), Mapping):
            entry = lc.parse_entry({**item["entry"], "output": f"origin:{norm}"})
        if not entry:
            return [] if collect else 0
        self.table.update_item(Key={"PK": item["PK"], "SK": item["SK"]}, UpdateExpression="SET resolvedBy = :a",
                               ExpressionAttributeValues={":a": asset_id})
        entry.pop("origin", None)
        parents = self.capture(pid, asset_id, entry, session_id=item.get("sessionId"), turn_id=item.get("turnId"),
                               job_id=entry.get("jobId"), default_via=entry.get("via") or "plan_node")
        return parents if collect else len(parents)

    def _resolve_gap(self, pid: str, child: str, ref: str, reason: str, asset_id: str) -> None:
        try:
            self.table.update_item(
                Key={"PK": schema.project_pk(pid), "SK": lc.gap_sk(child, ref, reason)},
                UpdateExpression="SET resolvedBy = :a", ConditionExpression="attribute_exists(PK) AND "
                                                                            "attribute_not_exists(resolvedBy)",
                ExpressionAttributeValues={":a": asset_id})
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return
            raise
        self.table.update_item(Key={"PK": schema.project_pk(pid), "SK": lc.STATS_SK},
                               UpdateExpression="ADD droppedEdges :m", ExpressionAttributeValues={":m": -1})


# ---------------------------------------------------------------------- shot-based M2C outputs
_SHOT_KEY_RE = re.compile(r"(?:^|/)shot_0*(\d{1,6})(?:[._-][^/]*)?\.(?:jpe?g|png|webp|mp4|mov|webm)$", re.IGNORECASE)


def shot_index(key: str) -> Optional[int]:
    """The shot number an M2C shot-segmentation output names (``shot_images/shot_000003.jpg`` → 3), else None."""
    m = _SHOT_KEY_RE.search(key or "")
    return int(m.group(1)) if m else None


def shot_ranges(doc: Any) -> dict[int, dict]:
    """``{shot_id: {start, end, unit}}`` from an M2C ``shots.json`` (``shots[].shot_id/start_time/end_time``)."""
    out: dict[int, dict] = {}
    for s in (doc.get("shots") if isinstance(doc, Mapping) else None) or []:
        if not isinstance(s, Mapping):
            continue
        sid = s.get("shot_id")
        if isinstance(sid, bool) or not isinstance(sid, int):
            continue
        r = lc.normalize_range(s.get("start_time"), s.get("end_time"))
        if r:
            out[sid] = r
    return out


# ---------------------------------------------------------------------- plan results
def _canonical(url: Any, bucket: str) -> Optional[str]:
    loc = gen.parse_location(url, bucket) if isinstance(url, str) else None
    if loc is None:
        return None
    m = _M2C_REF_RE.match(loc[1]) if loc[0] == bucket else None
    if m:
        return f"s3://{m.group(1)}/{m.group(2)}"
    return f"s3://{loc[0]}/{loc[1]}"


def _topo(nodes: list[dict]) -> list[dict]:
    by_id = {n["node_id"]: n for n in nodes}
    done: set[str] = set()
    out: list[dict] = []

    def visit(nid: str, stack: frozenset) -> None:
        if nid in done or nid in stack or nid not in by_id:
            return
        for d in by_id[nid].get("depends_on") or []:
            visit(d, stack | {nid})
        done.add(nid)
        out.append(by_id[nid])

    for n in nodes:
        visit(n["node_id"], frozenset())
    return out


def plan_entries(doc: Mapping[str, Any], *, bucket: str, plan_id: str,
                 node_row: Callable[[str], Mapping[str, Any]],
                 node_ledger: Callable[[str], list]) -> list[dict]:
    """Lineage entries for every output of a settled plan's nodes, in dependency order (see the module docstring).

    ``node_row(node_id)`` returns the node's job-registry row (``outputs``, ``inputRefs``), ``node_ledger(node_id)``
    its ``lineage.json`` entries; both are injected so this stays testable without AWS, and either may be empty."""
    raw_nodes = [n for n in doc.get("nodes") or [] if isinstance(n, Mapping) and n.get("node_id")]
    nodes = _topo([{**n, "node_id": str(n["node_id"]),
                    "depends_on": [str(d) for d in n.get("depends_on") or [] if isinstance(d, (str, int))]}
                   for n in raw_nodes])
    outs_of: dict[str, list[str]] = {}
    claimed: set[str] = set()
    entries: list[dict] = []
    for n in nodes:
        nid = n["node_id"]
        job = f"{plan_id}#{nid}"
        row = node_row(nid) or {}
        ledger = lc.entries(node_ledger(nid) or [], 200)
        tool = n.get("tool") if isinstance(n.get("tool"), str) else None
        sources: list[str] = []
        outs: list[str] = []
        for a in n.get("artifacts") or []:
            if not isinstance(a, Mapping):
                continue
            c = _canonical(a.get("source_ref") or a.get("url"), bucket) if a.get("role") != "source" else None
            if a.get("role") == "source":
                c = _canonical(a.get("url"), bucket)
                if c and c not in sources:
                    sources.append(c)
            elif c and c not in outs:
                outs.append(c)
            elif c is None and a.get("role") in ("output", None):
                # A third-party result nothing mirrored yet (a fal.media URL): parked under its URL, it becomes
                # the lineage of the asset later copied from it (LineageCapture.park_output).
                ext = lc.normalize_origin_url(a.get("url"))
                if ext and ext not in outs:
                    outs.append(ext)
        for o in row.get("outputs") or []:
            c = _canonical(o.get("uri") if isinstance(o, Mapping) else None, bucket)
            if c and c not in outs:
                outs.append(c)
        handed = [c for c in (_canonical(r, bucket) for r in row.get("inputRefs") or []) if c]
        # A node's result routinely NAMES what it was given (a stitch's prose lists its clips): a file an earlier
        # node made, or one this node was handed, is never this node's output.
        outs = [c for c in outs if c not in claimed and c not in handed and c not in sources]
        ledger_outs = set()
        ledger_entries = []
        for e in ledger:
            c = _canonical(e["output"], bucket) or e["output"]
            ledger_outs.add(c)
            ledger_entries.append({**e, "output": c, "via": e.get("via") or "plan_node", "jobId": e.get("jobId") or job,
                                   "tool": e.get("tool") or tool})
            if c not in outs:
                outs.append(c)
        declared = [c for c in dict.fromkeys([*sources, *handed]) if c not in outs]
        upstream = [c for d in n.get("depends_on") or [] for c in outs_of.get(d, []) if c not in outs]
        for e in ledger_entries:
            # The ledger is what the node's code was SEEN to read; what the chainer handed it is what it was given.
            # A cell that builds keys in a loop (`download_file(b, pfx + f, f)`) is invisible to the first, so the
            # node's declared inputs are added; with neither, its upstream outputs (inferred).
            if e.get("inputs") or e.get("unresolved"):
                e["inputs"] = list(dict.fromkeys([*e.get("inputs", []), *declared]))
            elif declared:
                e["inputs"] = list(declared)
            elif upstream:
                e["inputs"], e["inferred"] = list(dict.fromkeys(upstream)), True
            entries.append(e)
        for out in outs:
            if out in ledger_outs:
                continue
            if declared:
                entries.append({"output": out, "inputs": declared, "tool": tool, "via": "plan_node", "jobId": job})
            elif upstream:
                entries.append({"output": out, "inputs": list(dict.fromkeys(upstream)), "tool": tool,
                                "via": "plan_node", "jobId": job, "inferred": True})
        outs_of[nid] = outs
        claimed.update(outs)
    return [e for e in (lc.parse_entry(x) for x in entries) if e]
