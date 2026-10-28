"""GeneratedMediaFn — the session output registrar (asset-lineage design §3; ingestion Req 9, design §16).

Registers what a turn (or async work the turn dispatched) produced as assets of the turn's Project, IN
PLACE: no byte is copied (asset-lineage R3.1, D4). Two event sources, one per producer:

- **SQS ``GeneratedMediaQueue``**, one message per finished turn from the proxy's transcript finalize:
  ``{kind: "turn", sessionId, turnId, projectId, actorSub, startedAt, endedAt, externalRefs[], lineage[]?}``
  (epoch ms). Candidates are the session's objects written inside ``[startedAt − 5 s, endedAt + 5 s]``, the
  ``artifacts/external/`` keys the turn's reply named, what a render manifest written in the window names,
  and the ``output`` of each ``lineage`` entry. Each is registered with ``source = session``,
  ``sourceUri = s3://<ingest>/<key>``, ``role`` by type (media → ``deliverable``, data → ``analysis``),
  ``visibility = project``, ``producer = {sessionId, turnId, tool?}`` and ``derivedFrom`` the asset ids of
  the entry's ``inputs`` (:meth:`GeneratedMedia.derived_from`).
- **The job-registry DynamoDB stream**, filtered to a row reaching a terminal status with a ``resultKey``.
  ``result.json`` names the outputs (:func:`ingestion.generated.job_outputs`): session keys register as
  above; every M2C-bucket output of a succeeded node registers IN PLACE with ``source = m2c``
  (``sourceUri = s3://<M2C proxy bucket>/<key>``), ``role`` by type (:func:`ingestion.generated.m2c_role`),
  ``derivedFrom`` the job's source media asset, in the folder ``Sessions/<…>/<Job label>``, and a
  ``JOB#<jobId>`` record lists them (R4.1). Its Display_Name names the source asset and the job rather
  than the workflow's own file name (``meridian_left_trim — Process Media (transcoded).mp4``,
  :func:`ingestion.generated.output_display_name`); a session output is renamed that way only when its
  own name is generic. The Project is the row's ``producingProjectId`` (stamped at
  dispatch from the turn, Req 9.3), else its plan row's; a row with neither registers nothing.

Invented folders (R3.4): the code tool writes ``artifacts/<uuid>/output/…`` under a uuid it made up. Such a
key is a candidate only when this turn NAMES it (lineage output or render manifest — the bucket is never
listed beyond this session's own prefix), it was written inside the turn window (``LastModified``), and the
uuid is not an existing session (one ``GetItem`` of ``SESSIONS_TABLE``; an unreadable row refuses the key,
and with no sessions table configured the window alone decides). ``input/`` and ``render/`` stay excluded.

Per message: ``check_access(actor, project)`` (lost membership or a deleted Project → nothing, Req 9.5);
HEAD each candidate (``ChecksumMode=ENABLED``, for the HASH dedupe guard, R5); :func:`ingestion.generated.select`;
then the shared registration transaction per object, unless its derived ``asset_id`` already exists. Folder:
``Sessions/<session title> · <YYYY-MM-DD>`` (:func:`folder_for`; the title is one ``GetItem`` of
``SESSIONS_TABLE`` per message, and an unreadable row only falls back to ``Sessions/Session <sid[:8]> · <date>``).
A registration error raises, so SQS redelivers the message (visibility 120 s, 4 receives, then the DLQ —
Req 9.8); the stream mapping reports the failed record for a retry. Legacy ``source = generated`` rows (the
old byte copies under ``projects/<pid>/``) are left as they are and stay readable.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

from projects import lineage_contract as lc
from projects.access import check_access
from projects.ids import is_project_id

from .. import generated as gen
from ..lineage_capture import LineageCapture, plan_entries, shot_index, shot_ranges
from ..lineage_edges import LineageEdges
from ..folders import SESSIONS_ROOT, date_from, session_folder
from ..limits import limits_from_env
from ..store import AssetStore, ProjectNotActive, RegistrationConflict

log = logging.getLogger()
log.setLevel(logging.INFO)

__all__ = ["Deps", "GeneratedMedia", "Work", "handler", "work_from_job_image", "work_from_turn"]

TERMINAL = frozenset({"SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"})
_MAX_LIST_PAGES = 20  # 20,000 keys: a session far larger than any real one
_MAX_HEADS = 200
_MAX_MANIFESTS = 20


@dataclass
class Work:
    """One producer's request: where to look, and whose Project the media joins."""

    project_id: str
    actor_sub: str
    session_id: Optional[str]
    producer: str  # "turn:<turnId>" | "job:<jobId>" (for logs)
    turn_id: Optional[str] = None
    window_ms: Optional[tuple[int, int]] = None  # turn: list the session in this window
    keys: list[str] = field(default_factory=list)  # explicit candidate keys (refs / result.json)
    lineage: list[gen.LineageEntry] = field(default_factory=list)  # turn: output → inputs (design §3)
    job_id: Optional[str] = None  # job: the registry row's jobId (execution ARN, or the plan id)
    job_tool: Optional[str] = None  # job: the row's trio, when the result does not name a tool
    job_nodes: list[gen.JobNode] = field(default_factory=list)  # job: M2C-bucket outputs per node
    source_media: list[str] = field(default_factory=list)  # job: the row's `sourceMedia`
    lineage_inputs: list[str] = field(default_factory=list)  # job: the row's `lineageInputs` (lineage edges)
    # projects.lineageEdges: the producer's entries with every edge field (lineage_contract.parse_entry), and a
    # settled plan's result document (its nodes' artifacts and depends_on) with where it was read from.
    edge_entries: list[dict] = field(default_factory=list)
    plan_doc: Optional[dict] = None
    result_key: Optional[str] = None


@dataclass
class Deps:
    table: Any  # boto3 resource Table (projects)
    s3: Any
    bucket: str
    limits: dict
    job_table: Any = None  # boto3 resource Table (job registry), for a plan row's project
    access: Callable[[Any, str, str], Any] = check_access
    sessions_table: Any = None  # boto3 resource Table (sessions): folder title + invented-folder check
    m2c_bucket: str = ""  # the M2C proxy bucket (`MEDIA_PROXY_BUCKET`); empty → no m2c registration
    lineage_edges: Optional[bool] = None  # projects.lineageEdges; None → from the env (INGESTION_CONFIG)


def work_from_turn(msg: dict) -> Optional[Work]:
    """A ``kind: turn`` message as :class:`Work`, or ``None`` when it is malformed."""
    pid, actor, sid = msg.get("projectId"), msg.get("actorSub"), msg.get("sessionId")
    start, end = msg.get("startedAt"), msg.get("endedAt")
    if not (is_project_id(pid) and isinstance(actor, str) and actor and gen.is_session_id(sid)):
        return None
    if not (isinstance(start, int) and isinstance(end, int) and 0 < start <= end):
        return None
    refs = [r for r in msg.get("externalRefs") or [] if isinstance(r, str)]
    turn = msg.get("turnId") if isinstance(msg.get("turnId"), str) else None
    return Work(project_id=pid, actor_sub=actor, session_id=sid, producer=f"turn:{turn or ''}", turn_id=turn,
                window_ms=(start - gen.WINDOW_SLACK_MS, end + gen.WINDOW_SLACK_MS),
                keys=gen.external_refs_in("\n".join(refs)), lineage=gen.lineage_entries(msg.get("lineage")),
                edge_entries=lc.entries(msg.get("lineage"), gen.MAX_LINEAGE_ENTRIES))


def work_from_job_image(new: dict, old: Optional[dict], deps: Deps) -> Optional[Work]:
    """A job-registry row that just turned terminal, as :class:`Work`; ``None`` when there is nothing to do."""
    status, result_key = new.get("status"), new.get("resultKey")
    if status not in TERMINAL or not isinstance(result_key, str) or not result_key:
        return None
    if old and old.get("status") in TERMINAL and old.get("resultKey") == result_key:
        return None  # an unrelated update to an already-settled row
    actor, sid = new.get("actorId"), new.get("sessionId")
    if not (isinstance(actor, str) and actor):
        return None
    pid = new.get("producingProjectId")
    if not is_project_id(pid):
        pid = _plan_project(new.get("runId"), new.get("jobId"), deps)
    if not is_project_id(pid):
        return None
    body = _read_text(deps, result_key)
    if body is None:
        return None
    sid = sid if gen.is_session_id(sid) else None
    keys, nodes = gen.job_outputs(body, sid, deps.m2c_bucket, pid)
    job_id = new.get("jobId") if isinstance(new.get("jobId"), str) else None
    trio = new.get("trioId") if isinstance(new.get("trioId"), str) else None
    sources = [s for s in new.get("sourceMedia") or [] if isinstance(s, str)]
    return Work(project_id=pid, actor_sub=actor, session_id=sid, producer=f"job:{job_id}",
                turn_id=new.get("turnId") if isinstance(new.get("turnId"), str) else None, keys=keys,
                job_id=job_id, job_tool=trio, job_nodes=nodes, source_media=sources,
                lineage_inputs=[s for s in new.get("lineageInputs") or [] if isinstance(s, str)],
                plan_doc=_plan_doc(body), result_key=result_key)


def _plan_doc(body: str) -> Optional[dict]:
    try:
        doc = json.loads(body or "")
    except (TypeError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("schema") == "plan-result/v1" else None


def _plan_project(run_id: object, job_id: object, deps: Deps) -> Optional[str]:
    if deps.job_table is None or not isinstance(run_id, str) or not run_id or run_id == job_id:
        return None
    try:
        item = deps.job_table.get_item(Key={"jobId": run_id}).get("Item") or {}
    except Exception as exc:  # noqa: BLE001 — no plan row → not a Projects turn
        log.warning("generated_media: plan row %s unreadable: %s", run_id, exc)
        return None
    pid = item.get("producingProjectId")
    return pid if is_project_id(pid) else None


def _read_text(deps: Deps, key: str) -> Optional[str]:
    try:
        obj = deps.s3.get_object(Bucket=deps.bucket, Key=key)
        return obj["Body"].read(4 * 1024 * 1024).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 — a missing result names nothing
        log.warning("generated_media: result %s unreadable: %s", key, exc)
        return None


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


class GeneratedMedia:
    def __init__(self, deps: Deps):
        self.deps = deps
        self.store = AssetStore(deps.table)
        self.edges_on = lc.edges_enabled() if deps.lineage_edges is None else bool(deps.lineage_edges)
        self._made: list[tuple[str, str, str, bool, Optional[dict]]] = []

    # ------------------------------------------------------------ candidates
    def _window_keys(self, work: Work) -> tuple[list[str], list[str]]:
        """``(session keys, invented-folder keys)`` the turn wrote or named."""
        lo, hi = work.window_ms
        keys: list[str] = []
        manifests: list[str] = []
        legacy_render = gen.session_prefix(work.session_id) + "render/"
        # Every root of this session (asset-lineage Phase 3 (a)): legacy `artifacts/<sid>/` — which also
        # holds the render manifests — and, for this turn's Project, `projects/<pid>/sessions/<sid>/`.
        for root in gen.session_roots(work.session_id, work.project_id):
            token = None
            for _ in range(_MAX_LIST_PAGES):
                kw = {"Bucket": self.deps.bucket, "Prefix": root}
                if token:
                    kw["ContinuationToken"] = token
                resp = self.deps.s3.list_objects_v2(**kw)
                for o in resp.get("Contents") or []:
                    lm = o.get("LastModified")
                    if not isinstance(lm, datetime) or not lo <= _ms(lm) <= hi:
                        continue
                    key = o.get("Key", "")
                    if gen.is_session_source(key, work.session_id, work.project_id):
                        keys.append(key)
                    elif key.startswith(legacy_render) and key.endswith(".json"):
                        manifests.append(key)
                token = resp.get("NextContinuationToken")
                if not resp.get("IsTruncated") or not token:
                    break
        # A render manifest written in the turn names what the user was shown — external fal outputs and files
        # the agent saved outside this session's folder (including a folder it invented).
        invented: list[str] = []
        for m in sorted(manifests)[:_MAX_MANIFESTS]:
            text = _read_text(self.deps, m) or ""
            keys.extend(gen.rendered_refs_in(text, work.session_id, work.project_id))
            invented.extend(gen.invented_refs_in(text, work.session_id))
        return keys, invented

    def _lineage_keys(self, work: Work) -> tuple[list[str], list[str]]:
        """``(session/external keys, invented-folder keys)`` among the turn's ``lineage`` outputs."""
        own: list[str] = []
        invented: list[str] = []
        for e in work.lineage:
            loc = gen.parse_location(e.output, self.deps.bucket)
            if loc is None or loc[0] != self.deps.bucket:
                continue
            key = loc[1]
            if gen.invented_output_namespace(key, work.session_id):
                invented.append(key)
            elif ((work.session_id and gen.is_session_source(key, work.session_id, work.project_id))
                  or gen.external_refs_in(key) == [key]):
                own.append(key)
        return own, invented

    def _is_other_session(self, ns: str) -> bool:
        """True when ``ns`` is an existing session's id (or that cannot be read: refuse rather than adopt it)."""
        if self.deps.sessions_table is None:
            return False  # no sessions table here: the turn window alone decides (see the module docstring)
        try:
            item = self.deps.sessions_table.get_item(
                Key={"sessionId": ns}, ProjectionExpression="sessionId").get("Item")
        except Exception as exc:  # noqa: BLE001 — unreadable: do not adopt another chat's folder
            log.warning("generated_media: session %s unreadable for the invented-folder check: %s", ns, exc)
            return True
        return bool(item)

    def _head(self, bucket: str, key: str) -> Optional[dict]:
        try:
            return self.deps.s3.head_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED")
        except Exception:  # noqa: BLE001 — gone or unreadable: nothing to register
            return None

    def candidates(self, work: Work) -> tuple[list[gen.Candidate], list[dict]]:
        keys = list(work.keys)
        invented: list[str] = []
        if work.window_ms is not None and work.session_id:
            k, inv = self._window_keys(work)
            keys.extend(k)
            invented.extend(inv)
            k, inv = self._lineage_keys(work)
            keys.extend(k)
            invented.extend(inv)
        invented_set = set(invented) - set(keys)
        known_other: dict[str, bool] = {}
        out: list[gen.Candidate] = []
        skipped: list[dict] = []
        for key in sorted(set(keys) | invented_set)[:_MAX_HEADS]:
            head = self._head(self.deps.bucket, key)
            if head is None:
                skipped.append({"key": key, "reason": "not_found"})
                continue
            if key in invented_set:
                lm = head.get("LastModified")
                lo, hi = work.window_ms
                if not isinstance(lm, datetime) or not lo <= _ms(lm) <= hi:
                    skipped.append({"key": key, "reason": "outside_turn_window"})
                    continue
                ns = gen.invented_output_namespace(key, work.session_id) or ""
                if ns not in known_other:
                    known_other[ns] = self._is_other_session(ns)
                if known_other[ns]:
                    skipped.append({"key": key, "reason": "other_session"})
                    continue
            cand, reason = gen.candidate_from_head(key, head, self.deps.limits, bucket=self.deps.bucket)
            if cand is None:
                skipped.append({"key": key, "reason": reason})
            else:
                out.append(cand)
        return out, skipped

    # ------------------------------------------------------------ lineage
    def asset_for_location(self, pid: str, uri: str) -> Optional[str]:
        """The asset id an input URI names: a ``projects/<pid>/<assetId>/…`` key carries it; any other tracked
        location is one SRC-guard read (R1.2). ``None`` for an untracked location."""
        loc = gen.parse_location(uri, self.deps.bucket)
        if loc is None:
            return None
        bucket, key = loc
        if bucket == self.deps.bucket:
            aid = gen.asset_id_in_project_key(key, pid)
            if aid:
                return aid
        aid = self.store.asset_id_for_location(pid, bucket, key)
        if aid is None and self.edges_on:
            aid = LineageEdges(self.deps.table).alias_of(pid, bucket, key)
        return aid

    def derived_from(self, pid: str, inputs: Iterable[str]) -> list[str]:
        return list(dict.fromkeys(a for a in (self.asset_for_location(pid, u) for u in inputs) if a))

    def display_name_of(self, pid: str, asset_id: Optional[str]) -> Optional[str]:
        """The Display_Name of an asset of this Project (one cached GetItem), for the output-name
        qualifier. ``None`` when there is no such asset or its row carries no name."""
        if not asset_id:
            return None
        if asset_id not in self._names:
            row = self.store.get_asset(pid, asset_id) or {}
            name = row.get("displayName")
            self._names[asset_id] = name if isinstance(name, str) and name else None
        return self._names[asset_id]

    # ------------------------------------------------------------ run
    def run(self, work: Work) -> dict:
        access = self.deps.access(self.deps.table, work.actor_sub, work.project_id)
        if not getattr(access, "ok", False):
            log.info("generated_media: %s has no access to %s (or it is gone); registering nothing (%s)",
                     work.actor_sub, work.project_id, work.producer)
            return {"registered": [], "existing": [], "deduped": [], "skipped": [], "jobs": [],
                    "reason": "no_access"}
        self._node_rows = {}
        if self.edges_on and work.plan_doc is not None:
            work.keys = list(dict.fromkeys([*work.keys, *self._in_node_outputs(work)]))
        cands, skipped = self.candidates(work)
        selected, over_cap = gen.select(cands)
        if over_cap:
            skipped.append({"reason": "limit", "count": over_cap})
        out = {"registered": [], "existing": [], "deduped": [], "skipped": skipped, "jobs": []}
        self._session: Optional[dict] = None
        self._session_read = False
        self._names: dict[str, Optional[str]] = {}
        self._made = []
        self._aliases: list[tuple[str, str]] = []  # (asset id, url of a durable copy of its bytes)
        by_output: dict[tuple[str, str], gen.LineageEntry] = {}
        for e in work.lineage:
            loc = gen.parse_location(e.output, self.deps.bucket)
            if loc:
                by_output[loc] = e
        try:
            for c in selected:
                entry = by_output.get((c.bucket, c.key))
                derived = self.derived_from(work.project_id, entry.inputs) if entry else []
                self._register(work, c, out, source="session", role=gen.session_role(c.kind),
                               tool=entry.tool if entry else None, derived=derived,
                               folder=self._folder(work, c.last_modified),
                               name=gen.output_display_name(
                                   c, self.display_name_of(work.project_id, derived[0] if derived else None),
                                   gen.output_label(entry.tool if entry else None)))
            for node in work.job_nodes:
                self._register_job(work, node, out)
            if self.edges_on:
                out["lineage"] = self._capture_lineage(work)
        except ProjectNotActive:
            log.info("generated_media: project %s is no longer active; stopping", work.project_id)
        log.info("generated_media: %s → %s registered=%d existing=%d deduped=%d jobs=%d skipped=%s",
                 work.producer, work.project_id, len(out["registered"]), len(out["existing"]),
                 len(out["deduped"]), len(out["jobs"]), json.dumps(skipped)[:2000])
        return out

    def _folder(self, work: Work, last_modified: object, label: Optional[str] = None) -> str:
        if not self._session_read:  # one GetItem per message, and only when something will be registered
            self._session, self._session_read = session_row(self.deps.sessions_table, work.session_id), True
        return folder_for(work.session_id, self._session, last_modified, label=label)

    def _note_made(self, aid: str, c: gen.Candidate, new: bool, default_entry: Optional[dict]) -> None:
        if self.edges_on:
            self._made.append((aid, c.bucket, c.key, new, default_entry))

    def _register(self, work: Work, c: gen.Candidate, out: dict, *, source: str, role: str, folder: str,
                  tool: Optional[str] = None, derived: Optional[list[str]] = None,
                  job_id: Optional[str] = None, name: Optional[str] = None,
                  default_entry: Optional[dict] = None) -> Optional[str]:
        """Register ``c`` in place; append its id to ``out``'s registered/existing/deduped. Returns the id."""
        aid = gen.location_asset_id(c.kind, work.project_id, c.bucket, c.key)
        if self.store.get_asset(work.project_id, aid) is not None:
            out["existing"].append(aid)
            self._note_made(aid, c, False, default_entry)
            return aid
        producer = {"sessionId": work.session_id, "turnId": work.turn_id, "tool": tool, "jobId": job_id}
        try:
            row = self.store.register_asset(
                pid=work.project_id, asset_id=aid, source=source,
                source_uri=f"s3://{c.bucket}/{c.key}", source_bucket=c.bucket, source_key=c.key,
                source_size=c.size, content_type=c.content_type,
                file_name=name or gen.display_name_for(c),
                added_by=work.actor_sub, added_by_display=work.actor_sub, folder=folder,
                role=role, visibility="project", session_id=work.session_id, producer=producer,
                derived_from=derived, job_id=job_id, checksum=c.checksum,
                # A PDF is indexed for its page text; an HTML/Markdown/Office deliverable is kept, not queued.
                queue_for_indexing=False if c.kind == "document" and not gen.is_indexed_document(c.content_type, c.key) else None,
            )
        except RegistrationConflict:
            # Another asset already owns this location (a concurrent registration, or an older id scheme).
            other = self.store.asset_id_for_location(work.project_id, c.bucket, c.key)
            if other is None:
                raise
            out["existing"].append(other)
            self._note_made(other, c, False, default_entry)
            return other
        rid = str(row.get("assetId") or aid)
        if row.get("deduped"):
            out["deduped"].append(rid)
            out["existing"].append(rid)
            self._note_made(rid, c, False, default_entry)
        else:
            out["registered"].append(rid)
            self._note_made(rid, c, True, default_entry)
        return rid

    def _register_job(self, work: Work, node: gen.JobNode, out: dict) -> None:
        """Register one node's M2C-bucket outputs by reference and write its ``JOB#`` record (R4)."""
        m2c = self.deps.m2c_bucket
        if not m2c:
            return
        tool = node.tool or work.job_tool
        label = gen.job_label(tool)
        # A lone job's node IS the job (its node id is the execution ARN); a plan node is scoped by its plan.
        job_id = node.node_id if not work.job_id or node.node_id == work.job_id else f"{work.job_id}#{node.node_id}"
        source_asset = next((a for a in (self.asset_for_location(work.project_id, u)
                                         for u in [*node.sources, *work.source_media]) if a), None)
        # Every output of a workflow is named by the workflow (`transcoded.mp4`), so qualify all of them
        # with the source asset and the job — with neither, the raw name is kept (`gen.qualify_name`).
        source_name = self.display_name_of(work.project_id, source_asset)
        folder = self._folder(work, datetime.now(timezone.utc), label=label)
        outputs: list[str] = []
        shots = self._shot_ranges(m2c, node) if self.edges_on else {}
        for key in node.keys[:gen.MAX_PER_MESSAGE]:
            head = self._head(m2c, key)
            if head is None:
                out["skipped"].append({"key": f"s3://{m2c}/{key}", "reason": "not_found"})
                continue
            cand, reason = gen.candidate_from_head(key, head, self.deps.limits, bucket=m2c)
            if cand is None:
                out["skipped"].append({"key": f"s3://{m2c}/{key}", "reason": reason})
                continue
            aid = self._register(work, cand, out, source="m2c", role=gen.m2c_role(cand.kind, key), folder=folder,
                                 tool=tool, derived=[source_asset] if source_asset else [], job_id=job_id,
                                 name=gen.output_display_name(cand, source_name, gen.output_label(tool),
                                                              always=True),
                                 default_entry=self._job_entry(node, work, m2c, key, tool, job_id, shots)
                                 if self.edges_on else None)
            if aid:
                outputs.append(aid)
                if self.edges_on:
                    self._aliases.extend((aid, url) for k, url in node.copies if k == key)
        if not outputs:
            return
        prefix = node.output_prefix or f"s3://{m2c}/{node.keys[0].rsplit('/', 1)[0]}/"
        self.store.put_job(work.project_id, {
            "jobId": job_id, "tool": tool, "label": label, "sessionId": work.session_id, "turnId": work.turn_id,
            "outputPrefix": prefix, "sourceAssetId": source_asset, "outputs": outputs,
        })
        out["jobs"].append(job_id)


    # ------------------------------------------------------------ lineage edges (projects.lineageEdges)
    def _capture_lineage(self, work: Work) -> dict:
        """Write the edges of everything this message registered (or found registered), AFTER all of it is an
        asset, so an output made from another output of the same message resolves its parent. Best effort per
        edge (``ingestion.lineage_capture``); a failure here never fails the registration."""
        cap = LineageCapture(self.deps.table, self.store, self.deps.bucket, m2c_bucket=self.deps.m2c_bucket)
        try:
            for aid, url in self._aliases:
                # The durable copy of an M2C output (`<session output>/<execution>/<name>`): later steps are handed
                # the copy's URL, so it resolves to the M2C output's asset -- and resolves what already waited on it.
                loc = cap.location(url)
                if loc and loc[0] == self.deps.bucket:
                    cap.edges.put_alias(work.project_id, loc[0], loc[1], aid)
                    cap.on_registered(work.project_id, aid, bucket=loc[0], key=loc[1])
            entries = list(work.edge_entries)
            if work.plan_doc is not None:
                entries = lc.merge_entries(entries, self._plan_entries(work, cap))
            by_loc: dict[tuple[str, str], dict] = {}
            via = "turn" if work.window_ms is not None else "job"
            for e in entries:
                loc = cap.location(e["output"])
                if loc:
                    by_loc.setdefault(loc, e)
                elif lc.normalize_origin_url(e["output"]):
                    cap.park_output(work.project_id, e, session_id=work.session_id, turn_id=work.turn_id,
                                    job_id=work.job_id, default_via=via)
            seen: set[str] = set()
            for aid, bucket, key, new, default in self._made:
                if aid in seen:
                    continue
                seen.add(aid)
                entry = by_loc.get((bucket, key))
                if default is not None:
                    # The producer's entry (a plan node's) wins its fields; the job's own (the M2C source, a shot's
                    # time range) fills in what it lacks.
                    entry = lc.merge_entries([entry], [default])[0] if entry else lc.parse_entry(default)
                if entry is None and work.window_ms is None and (work.source_media or work.lineage_inputs):
                    # A lone async job (a fal queue render polled to completion): its row's `sourceMedia` is what
                    # the submission was given (jobs.strands_tool_wrap extracts it from the tool input).
                    entry = lc.parse_entry({"output": f"s3://{bucket}/{key}",
                                            "inputs": list(dict.fromkeys([*work.source_media, *work.lineage_inputs])),
                                            "tool": work.job_tool, "via": "job", "jobId": work.job_id})
                if entry:
                    cap.capture(work.project_id, aid, entry, session_id=work.session_id, turn_id=work.turn_id,
                                job_id=work.job_id, default_via=via)
                if new:
                    cap.on_registered(work.project_id, aid, bucket=bucket, key=key,
                                      origin=(entry or {}).get("origin"))
        except ProjectNotActive:
            raise
        except Exception:  # noqa: BLE001 - lineage is recorded best effort; the assets are registered
            log.exception("generated_media: lineage capture failed for %s", work.producer)
        log.info("generated_media: %s lineage edges=%d dropped=%d derivedAdded=%d", work.producer,
                 cap.stats["edges"], cap.stats["dropped"], cap.stats["derivedAdded"])
        return dict(cap.stats)

    @staticmethod
    def _job_entry(node: gen.JobNode, work: Work, m2c: str, key: str, tool: Optional[str], job_id: str,
                   shots: dict) -> dict:
        """An M2C output's default lineage: made from the media the node ran on — over the shot's own time range
        when the output is one shot of a shot segmentation (``shot_images/shot_000003.jpg``)."""
        # The node's own sources; the row's `sourceMedia` only when the node names none (a plan row's would be every
        # video the plan touched, not this node's).
        inputs = list(dict.fromkeys(node.sources or [*work.source_media, *work.lineage_inputs]))
        entry: dict = {"output": f"s3://{m2c}/{key}", "tool": tool, "via": "job", "jobId": job_id, "inputs": inputs}
        idx = shot_index(key)
        if idx is not None and idx in shots:
            entry["ranges"] = [{"input": i, **shots[idx]} for i in inputs]
            entry["settings"] = {"shot": idx}
        return entry

    def _shot_ranges(self, m2c: str, node: gen.JobNode) -> dict:
        key = next((k for k in node.keys if k.rsplit("/", 1)[-1] == "shots.json"), None)
        if not key:
            return {}
        try:
            body = self.deps.s3.get_object(Bucket=m2c, Key=key)["Body"].read(8 * 1024 * 1024)
            return shot_ranges(json.loads(body))
        except Exception as exc:  # noqa: BLE001 - no ranges, the edges still land
            log.info("generated_media: shots %s unreadable for ranges: %s", key, exc)
            return {}

    def _plan_entries(self, work: Work, cap: LineageCapture) -> list[dict]:
        doc = work.plan_doc or {}
        plan_id = str(doc.get("plan_id") or work.job_id or "")
        prefix = (work.result_key or "").rsplit("/", 1)[0]

        def node_row(nid: str) -> dict:
            return self._node_row(plan_id, nid)

        def node_ledger(nid: str) -> list:
            if not prefix.startswith("plans/"):
                return []
            body = self._read_quiet(f"{prefix}/{nid}/lineage.json")
            try:
                doc_ = json.loads(body) if body else {}
            except ValueError:
                return []
            return doc_.get("lineage") if isinstance(doc_, dict) and isinstance(doc_.get("lineage"), list) else []

        return plan_entries(doc, bucket=self.deps.bucket, plan_id=plan_id, node_row=node_row,
                            node_ledger=node_ledger)

    def _node_row(self, plan_id: str, nid: str) -> dict:
        """A plan node's job-registry row (``outputs``, ``inputRefs``), read once per message."""
        cache = self.__dict__.setdefault("_node_rows", {})
        key = f"{plan_id}#{nid}"
        if key not in cache:
            row: dict = {}
            if self.deps.job_table is not None and plan_id:
                try:
                    row = self.deps.job_table.get_item(Key={"jobId": key}).get("Item") or {}
                except Exception as exc:  # noqa: BLE001 - a missing node row only loses that node's precision
                    log.warning("generated_media: node row %s unreadable: %s", key, exc)
            cache[key] = row
        return cache[key]

    def _in_node_outputs(self, work: Work) -> list[str]:
        """Session keys a plan's nodes made INSIDE their own turn (an ffmpeg assemble), recorded by the chainer on
        each node's row: candidates the aggregate may not name."""
        doc = work.plan_doc or {}
        plan_id = str(doc.get("plan_id") or work.job_id or "")
        keys: list[str] = []
        for n in doc.get("nodes") or []:
            if not isinstance(n, dict) or not n.get("node_id"):
                continue
            for o in self._node_row(plan_id, str(n["node_id"])).get("outputs") or []:
                k = o.get("key") if isinstance(o, dict) else None
                if not isinstance(k, str) or not k:
                    continue
                if (work.session_id and gen.is_session_source(k, work.session_id, work.project_id)) \
                        or gen.external_refs_in(k) == [k]:
                    keys.append(k)
        return keys

    def _read_quiet(self, key: str) -> Optional[str]:
        try:
            obj = self.deps.s3.get_object(Bucket=self.deps.bucket, Key=key)
            return obj["Body"].read(1024 * 1024).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - most nodes write no ledger
            return None


# ---------------------------------------------------------------- folder (design D20, asset-lineage R3.3)
def session_row(sessions_table: Any, session_id: Optional[str]) -> Optional[dict]:
    """The session row's ``title``/``createdAt``, ``{}`` when there is no row, ``None`` when unreadable.

    Never raises: a folder name is not worth failing (and redelivering) a registration for.
    """
    if sessions_table is None or not session_id:
        return None
    try:
        item = sessions_table.get_item(
            Key={"sessionId": session_id}, ProjectionExpression="#t, #c",
            ExpressionAttributeNames={"#t": "title", "#c": "createdAt"},
        ).get("Item")
    except Exception as exc:  # noqa: BLE001 — fall back to the session-id folder
        log.warning("generated_media: session %s unreadable for its title: %s", session_id, exc)
        return None
    return item if isinstance(item, dict) else {}


def folder_for(session_id: Optional[str], session: Optional[dict], last_modified: object,
               label: Optional[str] = None) -> str:
    """``Sessions/<title> · <YYYY-MM-DD>`` (asset-lineage R3.3) with its fallbacks; always a valid folder.

    The date is the session's ``createdAt`` (UTC), else the object's ``LastModified``. No session row or no
    usable title → ``Sessions/Session <sid[:8]> · <date>``; no session at all (an external ref of a job
    with no session) → ``Sessions/<date>``. ``label`` (an M2C job's) adds the subfolder ``…/<label>``.
    """
    try:
        row = session or {}
        day = date_from(row.get("createdAt")) or date_from(last_modified) \
            or datetime.now(timezone.utc).date().isoformat()
        return session_folder(title=row.get("title"), session_id=session_id, day=day, label=label)
    except Exception as exc:  # noqa: BLE001 — never fail a registration over its folder
        log.warning("generated_media: folder derivation failed: %s", exc)
        return f"{SESSIONS_ROOT}/{datetime.now(timezone.utc).date().isoformat()}"


# ---------------------------------------------------------------- Lambda entry
_SERVICE: Optional[GeneratedMedia] = None


def _service() -> GeneratedMedia:
    global _SERVICE
    if _SERVICE is None:
        import boto3

        ddb = boto3.resource("dynamodb")
        job_table_name = os.environ.get("JOB_REGISTRY_TABLE", "")
        sessions_table_name = os.environ.get("SESSIONS_TABLE", "")
        _SERVICE = GeneratedMedia(Deps(
            table=ddb.Table(os.environ["PROJECTS_TABLE"]), s3=boto3.client("s3"),
            bucket=os.environ["INGEST_BUCKET"], limits=limits_from_env(),
            job_table=ddb.Table(job_table_name) if job_table_name else None,
            sessions_table=ddb.Table(sessions_table_name) if sessions_table_name else None,
            m2c_bucket=os.environ.get("MEDIA_PROXY_BUCKET", ""),
        ))
    return _SERVICE


def _deserialize(image: Optional[dict]) -> dict:
    if not image:
        return {}
    from boto3.dynamodb.types import TypeDeserializer

    d = TypeDeserializer()
    return {k: d.deserialize(v) for k, v in image.items()}


def _works(event: dict, svc: GeneratedMedia) -> Iterable[tuple[str, Optional[Work]]]:
    for rec in event.get("Records") or []:
        if rec.get("eventSource") == "aws:sqs":
            try:
                msg = json.loads(rec.get("body") or "{}")
            except ValueError:
                msg = {}
            yield rec.get("messageId", ""), work_from_turn(msg) if msg.get("kind") == "turn" else None
        elif rec.get("eventSource") == "aws:dynamodb":
            ddb = rec.get("dynamodb") or {}
            new, old = _deserialize(ddb.get("NewImage")), _deserialize(ddb.get("OldImage"))
            yield ddb.get("SequenceNumber", ""), work_from_job_image(new, old, svc.deps)


def handler(event: dict, _context: Any = None, *, service: Optional[GeneratedMedia] = None) -> dict:
    """Partial-batch response: a failed item is retried alone (SQS redelivery / stream retry)."""
    svc = service or _service()
    failures: list[dict] = []
    for item_id, work in _works(event, svc):
        if work is None:
            continue
        try:
            svc.run(work)
        except Exception:  # noqa: BLE001 — retried by the event source, then the DLQ / stream retry cap
            log.exception("generated_media: %s failed", work.producer)
            failures.append({"itemIdentifier": item_id})
    return {"batchItemFailures": failures}
