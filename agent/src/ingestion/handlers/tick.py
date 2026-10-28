"""IngestTick Lambda: one step of an asset's ingestion run (design Component 8).

``AssetIngestion`` is ``Tick → Choice(done?) → Wait(wait_s) → Tick``. Each tick re-reads the ASSET and META rows
(consistent), observes what the last tick started (job statuses; outputs on success), asks
:mod:`ingestion.reducer` what to do, does it, and writes the row conditioned on ``rowVersion``. A lost write means
another writer moved the row, so the tick re-reads and decides again rather than overwriting.

Input/output: ``{pid, assetId, runSeq}`` → the same plus ``{done, wait_s}``. ``{abort: true, error}`` (the state
machine's Catch, after its retries) fails the asset with the step it was in.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from projects import schema

from .. import adapters as A
from .. import chunking, collection, contract, image_description, pdf, poster, rebuild, records, reasons, reducer, rendition
from .. import documents as D
from .. import workflows as W
from ..indexer import Done, Failed, IndexRun, Poll, Submit, Wait, chunk_arguments, SUBMIT_TOOL, STATUS_TOOL
from ..m2c import M2CClient, M2CConfig, M2CError, Transient
from ..outputs import OutputError, output_uri, read_output, readable, split_uri
from ..pipeline_store import Conflict, PipelineStore, from_ddb

log = logging.getLogger()
log.setLevel(logging.INFO)

ACTIVE = ("analyzing", "indexing")
# The most PDF bytes IngestTick reads into memory to extract text. A larger document (within the 500 MiB
# document upload limit) is kept but not indexed (``stored`` + ``indexSkippedReason``), never failed.
MAX_DOCUMENT_SOURCE_BYTES = 100 * 1024 * 1024


@dataclass
class Deps:
    store: PipelineStore
    m2c: Any
    s3: Any                      # reads M2C outputs (proxy bucket) and reads/writes the Ingest bucket
    ingest_bucket: str
    cfg: reducer.Config = reducer.Config()
    dispatch: Optional[Callable[[str], Any]] = None  # fill freed slots in-process (Req 4.5)
    clock: Callable[[], float] = time.time
    # Where Document_Records are written/read; defaults to ``s3``. The local driver points it at moto so a
    # run against real M2C writes nothing to the real Ingest bucket.
    records_s3: Any = None
    # Start or coalesce a Collection_Rebuild: ``(pid, reason)``.
    request_rebuild: Optional[Callable[[str, str], Any]] = None

    @property
    def rs3(self) -> Any:
        return self.records_s3 if self.records_s3 is not None else self.s3


def _out(event: Mapping, done: bool, wait_s: int = 15) -> dict:
    return {"pid": event["pid"], "assetId": event["assetId"], "runSeq": int(event["runSeq"]),
            "done": done, "wait_s": int(wait_s)}


# ------------------------------------------------------------------------------------------ helpers
def _index_run_from(d: Mapping) -> IndexRun:
    return IndexRun(n_chunks=int(d["n_chunks"]), chunk=int(d.get("chunk") or 0), job_id=d.get("job_id") or None,
                    accepted=bool(d.get("accepted")), resubmits=int(d.get("resubmits") or 0),
                    call_failures=int(d.get("call_failures") or 0), not_before=float(d.get("not_before") or 0),
                    totals=tuple(int(x) for x in (d.get("totals") or (0, 0, 0))))


def _index_run_to(run: IndexRun) -> dict:
    d = dataclasses.asdict(run)
    d.pop("outcome", None)
    d["totals"] = list(run.totals)
    return d


class Tick:
    def __init__(self, deps: Deps):
        self.d = deps

    # ---------------------------------------------------------------------------------- entry
    def run(self, event: Mapping) -> dict:
        for _ in range(3):
            try:
                return self._once(event)
            except Conflict:
                continue  # someone else moved the row: re-read and decide again
        return _out(event, False, 15)

    def _once(self, event: Mapping) -> dict:
        pid, aid, seq = str(event["pid"]), str(event["assetId"]), int(event["runSeq"])
        now = self.d.clock()
        row = from_ddb(self.d.store.get_asset(pid, aid) or {})
        meta = from_ddb(self.d.store.get_meta(pid) or {})
        if (not row or not meta or meta.get("status") != "active" or row.get("status") not in ACTIVE
                or int((row.get("run") or {}).get("seq") or 0) != seq):
            return _out(event, True)  # removed, deleted, finished, or superseded: no status or slot change
        if event.get("abort"):
            step = reducer.failure_step_for(row.get("kind", ""), row["status"], row.get("steps") or {})
            self._fail(pid, row, step, f"internal error: {str(event.get('error') or '')[:300]}")
            return _out(event, True)
        if row["status"] == "analyzing":
            return self._analyzing(event, pid, row, now, seq)
        return self._indexing(event, pid, row, meta, now)

    def _fail(self, pid: str, row: Mapping, step: str, message: str, attrs: Optional[dict] = None) -> None:
        reason = reasons.compose(step, message)
        self.d.store.transition(pid, row, "failed", attrs or {}, failure={"step": step, "reason": reason})
        log.info("asset %s failed: %s", row.get("assetId"), reason)
        if self.d.dispatch:
            self.d.dispatch(pid)

    def _skip(self, pid: str, row: Mapping, step: str, reason: str, attrs: Optional[dict] = None) -> None:
        """Keep the asset but do not index it: ``stored`` with ``indexSkippedReason`` (no Failed badge, no FAILIDX
        entry, not counted as a failure, and never requeued by turning indexing on -- ``store.queueable``)."""
        self.d.store.transition(pid, row, "stored", {**(attrs or {}), "indexSkippedReason": reason,
                                                     "indexSkippedStep": step})
        log.info("asset %s stored, not indexed: %s (%s)", row.get("assetId"), reason, step)
        if self.d.dispatch:
            self.d.dispatch(pid)

    # ------------------------------------------------------------------------------ analyzing
    def _observe(self, kind: str, step: str, rec: dict, now: float) -> dict:
        spec = W.WORKFLOWS[step]
        try:
            st = self.d.m2c.call_tool(spec.status_tool, {"job_id": rec["jobId"]})
        except M2CError as exc:
            return {**rec, "callFailures": int(rec.get("callFailures") or 0) + 1, "error": str(exc)[:1000],
                    "notBefore": now + reducer.CALL_RETRY_GAP_S}
        rec = {**rec, "callFailures": 0, "notBefore": 0}
        status = st.get("status")
        if status not in W.TERMINAL:
            return rec
        rec["finishedAt"] = now
        if status != "SUCCEEDED":
            err = str(st.get("error") or status)
            # A VIDEO whose diarization fails because the source has no decodable audio (silent video) OR
            # holds audio with no usable speech (near-silent / music-only clip -- "No high-quality embeddings
            # found", "No VAD turns found") is indexed VISUALLY, not failed: the speech portion is simply
            # empty, exactly like the silent-video case. Only a diarization failure matching neither pattern
            # (a real decode error, OOM, …) still fails the asset.
            if step == W.SPEECH_DIARIZATION and kind == "video" and status == "FAILED" and (
                    W.is_no_audio_stream(err) or W.is_no_speech(err)):
                return {**rec, "status": reducer.NO_AUDIO, "error": err[:1000]}
            # An audio asset with sound but no speech (music / sfx stem): not a failure, the asset is kept unindexed.
            if step == W.SPEECH_DIARIZATION and kind == "audio" and status == "FAILED" and W.is_no_speech(err):
                return {**rec, "status": reducer.NO_AUDIO, "error": err[:1000]}
            return {**rec, "status": status, "error": err[:1000]}
        try:
            outs = {label: output_uri(st, label, step) for label in spec.labels}
            facts: dict = {}
            if step == W.SHOT_SEGMENTATION:
                shot_list = read_output(self.d.s3, outs["shot-list"], step, "shot-list")
                dur = A.duration_ms((shot_list or {}).get("actual_duration_sec"))
                if dur is None:
                    return {**rec, "status": "FAILED", "outputs": outs, "error": "video duration is missing or invalid"}
                facts["durationMs"] = dur
            elif step == W.SPEECH_DIARIZATION:
                transcript = read_output(self.d.s3, outs["transcript"], step, "transcript")
                facts["hasSpeech"] = A.has_speech(transcript)
                facts["durationMs"] = A.duration_ms((transcript or {}).get("audio_duration_sec"))
            elif step == W.IMAGE_DESCRIPTION:
                lines = read_output(self.d.s3, outs["response"], step, "response")
                obj, why = image_description.parse_response_line(lines if isinstance(lines, list) else [],
                                                                 str(rec.get("recordId") or ""))
                if obj is None:
                    return {**rec, "status": "FAILED", "outputs": outs, "error": why}
                facts["description"] = obj
            elif step == W.FRAME_CAPTURE:
                tech = self._technical(st, outs[W.FRAME_GROUP], rec)
                if tech:
                    facts["technical"] = tech
            elif step == W.FACE_RECOGNITION:
                facts["people"] = A.person_identities(read_output(self.d.s3, outs["face-metadata"], step, "face-metadata"))
            elif step == W.PROGRAM_STRUCTURE:
                facts["program"] = A.program_structure(read_output(self.d.s3, outs["structure"], step, "structure"))
            elif step == W.CAST_RESOLUTION:
                facts["cast"] = A.cast_characters(read_output(self.d.s3, outs["persons"], step, "persons"))
        except OutputError as exc:
            return {**rec, "status": "FAILED", "error": f"output {exc.label!r} {exc.problem}"}
        return {**rec, "status": reducer.SUCCEEDED, "outputs": outs, "facts": facts,
                "outputPrefix": st.get("output_s3_prefix") or rec.get("outputPrefix")}

    def _technical(self, st: Mapping, frames_uri: str, rec: Mapping) -> Optional[dict]:
        """The frame-capture job's ``probe.json`` projected to ``facts.technical`` (codec, resolution, fps, …).

        The job already probed the source (``probe_s3_key``, documented in the media-processing contract); keeping
        it means "what is the codec/resolution" is answered from the asset instead of a re-probe (staging session
        7e92b95d took 55 steps for it). Best effort: an unreadable or odd probe omits ``technical`` and never
        fails the step.
        """
        try:
            key = st.get("probe_s3_key")
            if isinstance(key, str) and key.startswith("s3://"):
                uri = key
            elif isinstance(key, str) and key:
                uri = f"s3://{split_uri(frames_uri)[0]}/{key.lstrip('/')}"
            else:
                prefix = st.get("output_s3_prefix") or rec.get("outputPrefix")
                if not isinstance(prefix, str) or not prefix.startswith("s3://"):
                    return None
                uri = prefix.rstrip("/") + "/probe.json"
            return A.technical_from_probe(read_output(self.d.s3, uri, W.FRAME_CAPTURE, "probe"))
        except Exception as exc:  # noqa: BLE001 — technical metadata is a convenience, never a failure
            log.info("frame capture: no technical metadata (%s)", exc)
            return None

    def _submit(self, row: Mapping, step: str, rec: dict, now: float) -> dict:
        extra: dict = {}
        if step == W.IMAGE_DESCRIPTION:
            try:
                jpeg = self.d.s3.get_object(Bucket=self.d.ingest_bucket,
                                            Key=(row.get("steps") or {})[W.IMAGE_RENDITION]["key"])["Body"].read()
            except Exception as exc:  # noqa: BLE001 — the rendition vanished: rerun it on a Retry
                return {**rec, "status": reducer.REJECTED, "error": f"the rendition is unreadable: {exc}",
                        "finishedAt": now}
            args = image_description.request_arguments(str(row["assetId"]), jpeg, self.d.cfg.image_model_chain,
                                                       rec.get("token"), self.d.cfg.image_thinking)
            extra["recordId"] = str(row["assetId"])
        else:
            args = reducer.submit_arguments(step, row.get("steps") or {}, str(row["sourceUri"]), rec.get("token"),
                                            self.d.cfg)
        try:
            res = self.d.m2c.call_tool(W.WORKFLOWS[step].submit_tool, args)
        except Transient as exc:
            return {**rec, "callFailures": int(rec.get("callFailures") or 0) + 1, "error": str(exc)[:1000],
                    "notBefore": now + reducer.CALL_RETRY_GAP_S}
        except M2CError as exc:  # a refusal (validation): Req 7.4 "submission rejected"
            return {**rec, "status": reducer.REJECTED, "error": str(exc)[:1000], "finishedAt": now}
        job = res.get("job_id")
        if not job:
            return {**rec, "status": reducer.REJECTED, "error": f"no job id: {json.dumps(res)[:500]}",
                    "finishedAt": now}
        return {**rec, **extra, "status": reducer.RUNNING, "jobId": job, "submittedAt": now, "callFailures": 0,
                "notBefore": 0, "error": None, "outputPrefix": res.get("output_s3_prefix"),
                "absorbed": bool(res.get("message"))}

    def _render(self, pid: str, row: Mapping, now: float, seq: int) -> dict:
        """The Search_Rendition, stored at ``projects/<pid>/<assetId>/rendition.jpg`` (Req 8.5, 8.6)."""
        rec = {"runSeq": seq, "finishedAt": now}
        out_key = f"{schema.project_prefix(pid)}{row['assetId']}/rendition.jpg"
        try:
            bucket, key = split_uri(str(row["sourceUri"]))
            out = rendition.store_search_rendition(self.d.s3, source_bucket=bucket, source_key=key,
                                                   ingest_bucket=self.d.ingest_bucket, out_key=out_key)
        except rendition.RenditionError as exc:
            return {**rec, "status": "FAILED", "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 — an unreadable source is a rendition failure too
            return {**rec, "status": "FAILED", "error": f"the image cannot be read: {type(exc).__name__}"}
        return {**rec, "status": reducer.SUCCEEDED, **out}

    def _extract_document(self, pid: str, row: Mapping, now: float, seq: int) -> dict:
        """Extract a PDF's text in the tick (pypdf), chunk it page-aware, and stash the chunks at
        ``projects/<pid>/<assetId>/extract/pages.json`` (Requirement 16, mirrors :meth:`_render`).

        A PDF with no extractable text, or one too large to extract, is recorded ``SUCCEEDED`` with
        ``hasText: False`` and a ``skipReason`` so the next tick DEGRADES it to ``stored`` -- never a hard
        failure. Only an unreadable S3 object re-raises, so the state machine's retries/abort treat it as
        infra (as :meth:`_render` does not: an uploaded source that vanished is an infra condition here).
        """
        rec: dict = {"runSeq": seq, "finishedAt": now}
        bucket, key = split_uri(str(row["sourceUri"]))
        obj = self.d.s3.get_object(Bucket=bucket, Key=key)  # AccessDenied / NoSuchKey → infra, re-raised
        if int(obj.get("ContentLength") or 0) > MAX_DOCUMENT_SOURCE_BYTES:
            return {**rec, "status": reducer.SUCCEEDED, "hasText": False,
                    "skipReason": reducer.DOC_TOO_LARGE_REASON}
        data = obj["Body"].read(MAX_DOCUMENT_SOURCE_BYTES + 1)
        if len(data) > MAX_DOCUMENT_SOURCE_BYTES:
            return {**rec, "status": reducer.SUCCEEDED, "hasText": False,
                    "skipReason": reducer.DOC_TOO_LARGE_REASON}
        try:
            chunks = pdf.chunk_pages(pdf.extract_pages(data))
        except pdf.NoText:
            return {**rec, "status": reducer.SUCCEEDED, "hasText": False, "skipReason": reducer.NO_TEXT_REASON}
        except Exception as exc:  # noqa: BLE001 — a parse failure is "no text", never a crash of the run
            log.info("document %s: text extraction failed (%s)", row.get("assetId"), type(exc).__name__)
            return {**rec, "status": reducer.SUCCEEDED, "hasText": False, "skipReason": reducer.NO_TEXT_REASON}
        if not chunks:
            return {**rec, "status": reducer.SUCCEEDED, "hasText": False, "skipReason": reducer.NO_TEXT_REASON}
        out_key = f"{schema.project_prefix(pid)}{row['assetId']}/extract/pages.json"
        body = json.dumps({"chunks": [dataclasses.asdict(c) for c in chunks]}, ensure_ascii=True)
        self.d.s3.put_object(Bucket=self.d.ingest_bucket, Key=out_key, Body=body.encode("utf-8"),
                             ContentType="application/json")
        return {**rec, "status": reducer.SUCCEEDED, "key": out_key, "chunkCount": len(chunks), "hasText": True}

    def _read_extract(self, steps: Mapping) -> list[dict]:
        """The chunks written by :meth:`_extract_document`, read back from ``extract/pages.json``."""
        key = (steps.get(W.DOCUMENT_EXTRACT) or {}).get("key")
        if not key:
            raise OutputError(W.DOCUMENT_EXTRACT, "pages", "is missing from the recorded result")
        body = self.d.s3.get_object(Bucket=self.d.ingest_bucket, Key=key)["Body"].read().decode("utf-8")
        return list(json.loads(body).get("chunks") or [])

    def _plan(self, kind: str, steps: Mapping, now: float, seq: int) -> reducer.Plan:
        if kind == "video":
            return reducer.plan_video(steps, now, seq, self.d.cfg)
        if kind == "audio":
            return reducer.plan_audio(steps, now, seq, self.d.cfg)
        if kind == "image":
            return reducer.plan_image(steps, now, seq, self.d.cfg)
        if kind == "document":
            return reducer.plan_document(steps, now, seq, self.d.cfg)
        return reducer.Plan(fail=("document build", f"no pipeline for kind {kind!r}"))

    def _check_reuse(self, row: Mapping, steps: dict, seq: int) -> bool:
        """Retry / Re-add: a step record from an earlier run is reused only while its outputs still answer
        HeadObject (Req 7.11, 8.14). An unreadable one is marked failed IN ITS OWN run, so this run reruns it
        with an idempotency token. Returns whether anything changed."""
        changed = False
        for step, rec in list(steps.items()):
            if not isinstance(rec, dict) or rec.get("status") not in reducer.DONE_OK:
                continue
            if int(rec.get("runSeq") or 0) >= seq:
                continue
            uris = list((rec.get("outputs") or {}).values())
            if step == W.IMAGE_RENDITION and rec.get("key"):
                uris.append(f"s3://{self.d.ingest_bucket}/{rec['key']}")
            if step == W.DOCUMENT_EXTRACT and rec.get("key"):
                uris.append(f"s3://{self.d.ingest_bucket}/{rec['key']}")
            if any(not readable(self.d.s3, u) for u in uris):
                steps[step] = {**rec, "status": "FAILED", "error": "the recorded output is no longer readable"}
                changed = True
        return changed

    def _analyzing(self, event: Mapping, pid: str, row: dict, now: float, seq: int) -> dict:
        kind = str(row.get("kind"))
        steps = dict(row.get("steps") or {})
        changed = False
        run = dict(row.get("run") or {})
        if seq > 1 and not run.get("reuseChecked"):
            self._check_reuse(row, steps, seq)
            row = from_ddb(self.d.store.patch(pid, row, {"steps": steps, "run": {**run, "reuseChecked": True}}))
        for step, rec in list(steps.items()):
            if isinstance(rec, dict) and reducer.poll_due(rec, now):
                steps[step] = self._observe(kind, step, rec, now)
                changed = True
        plan = self._plan(kind, steps, now, seq)
        if plan.fail:
            self._fail(pid, row, plan.fail[0], plan.fail[1], {"steps": steps})
            return _out(event, True)
        if plan.skip:
            self._skip(pid, row, plan.skip[0], plan.skip[1], {"steps": steps})
            return _out(event, True)
        if plan.blocked and self._record_blocked(steps, plan.blocked, seq, now):
            row = from_ddb(self.d.store.patch(pid, row, {"steps": steps}))
            changed = False
        if plan.render:
            steps[W.IMAGE_RENDITION] = self._render(pid, row, now, seq)
            attrs: dict = {"steps": steps}
            rend = steps[W.IMAGE_RENDITION]
            if rend.get("status") == reducer.SUCCEEDED:
                attrs["rendition"] = {k: rend.get(k) for k in ("key", "width", "height", "bytes")}
            self.d.store.patch(pid, row, attrs)
            return _out(event, False, 15)
        if plan.extract:
            steps[W.DOCUMENT_EXTRACT] = self._extract_document(pid, row, now, seq)
            self.d.store.patch(pid, row, {"steps": steps})
            return _out(event, False, 15)
        due = [s for s in plan.submit if reducer.submit_due(steps.get(s) or {}, now)]
        if due:
            speech = reducer.has_speech(steps)
            for s in due:
                prev = steps.get(s) or {}
                carry = prev if prev.get("status") == reducer.SUBMITTING and prev.get("runSeq") == seq else {}
                steps[s] = {**carry, "status": reducer.SUBMITTING, "runSeq": seq,
                            "inputs": list(reducer.inputs_for(s, steps, speech)),
                            "token": carry.get("token") if carry else reducer.token_for(s, prev, seq, row["assetId"])}
            row = from_ddb(self.d.store.patch(pid, row, {"steps": steps}))  # claim: at most one submission
            for s in due:
                steps[s] = self._submit({**row, "steps": steps}, s, steps[s], now)
            row = from_ddb(self.d.store.patch(pid, row, {"steps": steps}))
            plan = self._plan(kind, steps, now, seq)
            if plan.fail:
                self._fail(pid, row, plan.fail[0], plan.fail[1])
                return _out(event, True)
            if plan.skip:
                self._skip(pid, row, plan.skip[0], plan.skip[1])
                return _out(event, True)
        elif changed:
            row = from_ddb(self.d.store.patch(pid, row, {"steps": steps}))
        if plan.build:
            return self._build(event, pid, row, steps, now)
        return _out(event, False, reducer.wait_seconds(steps, now))

    # ---------------------------------------------------------------------------------- build
    def _read(self, steps: Mapping, step: str, label: str) -> Any:
        rec = steps.get(step) or {}
        uri = (rec.get("outputs") or {}).get(label)
        if not uri:
            raise OutputError(step, label, "is missing from the recorded result")
        return read_output(self.d.s3, uri, step, label)

    def _record_blocked(self, steps: dict, blocked: tuple, seq: int, now: float) -> bool:
        """Record each blocked entity-metadata step as a ``SKIPPED`` step record (status + the reason the
        panel reads), idempotently: an existing SKIPPED record built from the same inputs is left alone, so
        this writes at most once per blocking. Returns whether anything changed."""
        speech = reducer.has_speech(steps)
        changed = False
        for step, reason in blocked:
            want = list(reducer.inputs_for(step, steps, speech))
            prev = steps.get(step) or {}
            if prev.get("status") == reducer.SKIPPED and list(prev.get("inputs") or ()) == want:
                continue
            steps[step] = {"status": reducer.SKIPPED, "runSeq": seq, "inputs": want,
                           "error": reason, "finishedAt": now}
            log.info("asset entity-metadata step %s skipped: %s", step, reason)
            changed = True
        return changed

    def _build(self, event: Mapping, pid: str, row: dict, steps: Mapping, now: float) -> dict:
        aid, kind = str(row["assetId"]), str(row.get("kind"))
        try:
            turns, topics = [], []
            if kind != "image" and reducer.has_speech(steps):
                turns = A.diar_input(self._read(steps, W.SPEECH_DIARIZATION, "transcript"))
                topics = A.topics_input(self._read(steps, W.CONVERSATION_ANALYSIS, "analysis"))
            if kind == "image":
                rend = steps[W.IMAGE_RENDITION]
                desc = (steps[W.IMAGE_DESCRIPTION].get("facts") or {}).get("description") or {}
                dur = 1
                res = D.build_image(aid, f"s3://{self.d.ingest_bucket}/{rend['key']}", desc)
            elif kind == "video":
                shot_list = self._read(steps, W.SHOT_SEGMENTATION, "shot-list")
                manifest = self._read(steps, W.FRAME_CAPTURE, W.FRAME_GROUP)
                md = self._read(steps, W.ENRICHMENT, "shots")
                frames_uri = steps[W.FRAME_CAPTURE]["outputs"][W.FRAME_GROUP]
                frame_for = A.pick_frames(shot_list.get("shots") or [], manifest, frames_uri)
                dur = int((steps[W.SHOT_SEGMENTATION].get("facts") or {})["durationMs"])
                people = cast = segments = ()
                if self.d.cfg.entity_metadata:
                    # Fold the three best-effort workflows' identities/characters/segments into the member
                    # scenes' named_entities by time overlap (searchable), from the facts each step captured.
                    # A step that failed or was skipped simply left no facts and contributes nothing.
                    people = A.person_mentions((steps.get(W.FACE_RECOGNITION) or {}).get("facts", {}).get("people"))
                    cast = A.cast_mentions((steps.get(W.CAST_RESOLUTION) or {}).get("facts", {}).get("cast"))
                    segments = A.segment_mentions((steps.get(W.PROGRAM_STRUCTURE) or {}).get("facts", {}).get("program"))
                res = D.build_video(aid, A.shots_input(shot_list, frame_for), A.enrich_input(shot_list, md),
                                    turns, topics, dur, people=people, cast=cast, segments=segments)
            elif kind == "document":
                chunks = self._read_extract(steps)
                pages = []
                for c in chunks:
                    try:
                        pages.append(int(c["page"]))
                    except (KeyError, TypeError, ValueError):
                        continue  # build_document drops the same malformed chunk; don't crash the duration calc
                dur = max(pages, default=1) * D.PAGE_STRIDE_MS
                res = D.build_document(aid, chunks)
            else:
                dur = (steps[W.SPEECH_DIARIZATION].get("facts") or {}).get("durationMs")
                if not dur:
                    self._fail(pid, row, W.SPEECH_DIARIZATION, "audio duration is missing or invalid")
                    return _out(event, True)
                res = D.build_audio(aid, turns, topics, int(dur))
        except OutputError as exc:
            self._fail(pid, row, exc.step, f"output {exc.label!r} {exc.problem}")
            return _out(event, True)
        if not res.documents:
            if kind == "document":
                # hasText guaranteed chunks, but if every chunk's text was blank there is nothing to index:
                # keep the file, do not fail it (Requirement 16).
                self._skip(pid, row, "document text", reducer.NO_TEXT_REASON, {"steps": steps})
                return _out(event, True)
            self._fail(pid, row, "document build", "the asset produced no indexable content")
            return _out(event, True)
        try:
            contract.check(res.documents, aid, dur)
        except contract.ContractError as exc:
            self._fail(pid, row, "document build", str(exc))
            return _out(event, True)
        rec = records.serialize(res.documents)
        digest = records.digest(rec)
        key = records.record_key(pid, aid, digest)
        self.d.rs3.put_object(Bucket=self.d.ingest_bucket, Key=key, Body=rec.encode("ascii"),
                              ContentType="application/x-ndjson")
        index = dict(row.get("index") or {})
        index.pop("run", None)  # every submission starts at chunk 1 (Req 6.8)
        attrs: dict = {
            "doc": {"recordKey": key, "sha256": digest, "counts": res.counts(), "omitted": res.omitted,
                    "truncatedScenes": res.truncated_scenes},
            "index": index,
        }
        if kind == "video":
            frame = poster.pick_frame(res.documents)
            if frame:
                try:
                    attrs["rendition"] = poster.copy_poster(self.d.s3, frame, self.d.ingest_bucket, pid, aid,
                                                            self.d.cfg.frames.width, self.d.cfg.frames.height)
                except Exception:  # noqa: BLE001 — a missing hero image never fails ingestion
                    log.warning("hero image copy failed for %s", aid, exc_info=True)
        elif kind == "image":
            rend = steps[W.IMAGE_RENDITION]
            attrs["rendition"] = {k: rend.get(k) for k in ("key", "width", "height", "bytes")}
        self.d.store.transition(pid, row, "indexing", attrs)
        return _out(event, False, 15)

    # -------------------------------------------------------------------------------- indexing
    def _indexing(self, event: Mapping, pid: str, row: dict, meta: dict, now: float) -> dict:
        index = dict(row.get("index") or {})
        state = meta.get("collectionState") or "absent"
        if state == "absent":
            if now < float(index.get("createNotBefore") or 0):
                return _out(event, False, 30)
            try:
                collection.ensure(self.d.m2c, pid, str(meta.get("name") or pid))
            except M2CError as exc:
                n = int(index.get("createFailures") or 0) + 1
                if not isinstance(exc, Transient) or n >= reducer.CALL_FAILURES_TO_FAIL:
                    self._fail(pid, row, "indexing", f"{getattr(exc, 'step', 'create_collection')} failed: {exc}")
                    return _out(event, True)
                self.d.store.patch(pid, row, {"index": {**index, "createFailures": n,
                                                        "createNotBefore": now + reducer.CALL_RETRY_GAP_S}})
                return _out(event, False, 30)
            self.d.store.set_collection_ready(pid)
            return _out(event, False, 15)
        if state != "ready" and not (state == "rebuilding"
                                     and (meta.get("rebuild") or {}).get("phase") == "resubmitting"):
            # Held while a Collection_Rebuild is before step (e) or failed (Req 10.11). The run restarts at
            # chunk 1 on the new incarnation, so the hold never counts toward the 1 h indexing timeout.
            return _out(event, False, 30)

        inc = str(int(meta.get("incarnation") or 1))
        doc = row.get("doc") or {}
        body = self.d.rs3.get_object(Bucket=self.d.ingest_bucket, Key=doc["recordKey"])["Body"].read()
        docs = records.parse(body.decode("ascii"))
        by_inc = dict(index.get("byIncarnation") or {})
        hist_raw = by_inc.get(inc) or {}
        history = chunking.History(bool(hist_raw.get("accepted")),
                                   (hist_raw.get("lastCompleted") or {}).get("sha256"),
                                   frozenset(hist_raw.get("scopesAccepted") or ()))
        run_raw = index.get("run")
        if not run_raw or str(run_raw.get("incarnation")) != inc:
            refresh = chunking.refresh_mode(history, doc["sha256"])
            run_raw = {**_index_run_to(IndexRun(n_chunks=chunking.chunk_count(len(docs)))),
                       "refresh": refresh, "startedAt": now, "incarnation": inc}
        if now - float(run_raw.get("startedAt") or now) > self.d.cfg.index_timeout_s:
            self._fail(pid, row, "indexing", f"timed out after {self.d.cfg.index_timeout_s // 3600} h")
            return _out(event, True)
        run = _index_run_from(run_raw)
        parts = chunking.chunks(docs, bool(run_raw.get("refresh")))
        act = run.next(now)
        submitted_at = run_raw.get("submittedAt")
        try:
            if isinstance(act, Submit):
                res = self.d.m2c.call_tool(SUBMIT_TOOL, chunk_arguments(pid, parts[act.chunk],
                                                                       str(row.get("displayName") or "")))
                job = res.get("job_id")
                if not job:
                    raise M2CError(f"index_content returned no job id: {json.dumps(res)[:300]}")
                run = run.submitted(job)
                submitted_at = now
                scopes = set(hist_raw.get("scopesAccepted") or ()) | {d["doc_type"] for d in parts[act.chunk]}
                by_inc[inc] = {**hist_raw, "accepted": True, "scopesAccepted": sorted(scopes)}
            elif isinstance(act, Poll):
                run = run.status(self.d.m2c.call_tool(STATUS_TOOL, {"job_id": act.job_id}), now)
        except M2CError as exc:
            run = run.call_failed(exc, now)
        outcome = run.next(now)
        if isinstance(outcome, Failed):
            self._fail(pid, row, "indexing", outcome.message, {"index": {**index, "byIncarnation": by_inc}})
            return _out(event, True)
        if isinstance(outcome, Done):
            scopes = sorted({d["doc_type"] for d in docs})
            prev_scopes = frozenset(by_inc.get(inc, {}).get("scopesAccepted") or ())
            by_inc[inc] = {**by_inc.get(inc, {}), "accepted": True,
                           "lastCompleted": {"sha256": doc["sha256"], "scopes": scopes}}
            keep = sorted(by_inc, key=lambda k: int(k))[-2:]  # this incarnation and the one before
            totals = {"prepared": outcome.prepared, "skipped": outcome.skipped, "indexed": outcome.indexed}
            self.d.store.transition(pid, row, "indexed", {"index": {
                "totals": totals, "acceptedIncarnation": int(inc), "byIncarnation": {k: by_inc[k] for k in keep}}})
            stale = prev_scopes - set(scopes)
            if stale and self.d.request_rebuild:
                # A scope that held rows for this asset got no document: only a rebuild clears it (Req 6.9).
                self.d.request_rebuild(pid, f"{row.get('assetId')} left {', '.join(sorted(stale))}")
            if self.d.dispatch:
                self.d.dispatch(pid)
            return _out(event, True)
        run_raw = {**run_raw, **_index_run_to(run), "submittedAt": submitted_at}
        self.d.store.patch(pid, row, {"index": {**index, "run": run_raw, "byIncarnation": by_inc}})
        wait = reducer.wait_seconds({}, now, float(submitted_at) if submitted_at else None)
        if isinstance(act, Wait) or isinstance(run.next(now), Wait):
            wait = max(wait, 30)
        return _out(event, False, wait)


# ------------------------------------------------------------------------------------- Lambda wiring
_TICK: Optional[Tick] = None


def config_from_env() -> reducer.Config:
    raw = json.loads(os.environ.get("INGESTION_CONFIG") or "{}")
    return reducer.Config(frames=W.FrameConfig(interval_sec=float(raw.get("frameIntervalSec") or 1),
                                               width=int(raw.get("frameWidth") or 960),
                                               height=int(raw.get("frameHeight") or 540)),
                          image_model_chain=tuple(str(m) for m in raw.get("imageModelChain") or ()),
                          image_thinking=str(raw.get("imageThinking") or ""),
                          entity_metadata=bool(raw.get("entityMetadata")))


def from_env() -> Tick:
    import boto3
    from .dispatcher import from_env as dispatcher_from_env

    store = PipelineStore.open(os.environ["PROJECTS_TABLE"])
    dispatcher = dispatcher_from_env()
    sfn = boto3.client("stepfunctions")
    rebuild_arn = os.environ.get("COLLECTION_REBUILD_SFN_ARN", "")

    def request_rebuild(pid: str, reason: str) -> Any:
        from ..store import _iso_now
        return rebuild.request_rebuild(store.table, sfn, rebuild_arn, pid, reason=reason, now_iso=_iso_now(time.time))

    return Tick(Deps(store=store, m2c=M2CClient(M2CConfig.from_env()), s3=boto3.client("s3"),
                     ingest_bucket=os.environ["INGEST_BUCKET"], cfg=config_from_env(),
                     dispatch=dispatcher.dispatch_project,
                     request_rebuild=request_rebuild if rebuild_arn else None))


def handler(event: dict, _context: Any = None) -> dict:
    global _TICK
    _TICK = _TICK or from_env()
    out = _TICK.run(event)
    log.info("tick %s", out)
    return out
