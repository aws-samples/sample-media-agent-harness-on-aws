"""JobRegistry — DynamoDB wrapper for Async_Job records.

Mirrors the construction, conditional-write, and TTL idioms of
``agent/src/state_store.py``: same ``boto3.resource("dynamodb",
region_name=region).Table(...)`` shape, same conditional ``PutItem``
for first-write idempotency, same epoch-seconds ``ttl`` attribute
(set on terminal transition only), and the same ``UpdateItem`` +
``ConditionExpression`` pattern with expected-status guards for
status transitions.

This is a separate store from :class:`StateStore`. ``StateStore`` is
session-scoped (PK ``session_id``) and tracks the agent's own DAG
nodes; ``JobRegistry`` is actor-scoped (GSI PK ``actorId``) and
tracks external MCP jobs that outlive any single chat session and
span sessions for the same Actor. They MUST NOT be merged into a
single table — different lifetimes, different access patterns,
different retention policies.

The Python-facing API uses snake_case (matching :class:`JobRecord`
in ``models.py``); the on-disk DynamoDB attributes use camelCase per
the Job_Registry schema declared in design.md. The translation runs
through :meth:`JobRegistry._to_item` and :meth:`JobRegistry._from_item`.

See ``.kiro/specs/async-job-tracking/design.md`` ("Prior Art /
Pattern Reuse" and "Implementation idioms (mirror state_store.py)")
for the canonical design.

Requirements: 3.1, 3.2, 3.4, 4.2, 4.3, 4.5, 5.1, 5.2, 5.3, 11.1, 11.2
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Attr, Key

from .models import ACTIVE_STATUSES, JobRecord, JobStatus

logger = logging.getLogger(__name__)

# Default 30-day post-terminal retention. Overridable via env var so
# tests (and ops) can shorten the window without a code change.
RETENTION_SECONDS: int = int(
    os.environ.get("JOB_REGISTRY_RETENTION_SECONDS", str(30 * 24 * 60 * 60))
)

# GSI name from the CDK stack (see infra/lib/agent-infra-stack.ts and
# tasks.md task 1.3). Keyed on (actorId, updatedAt) for newest-first
# sidebar listing scoped to a single actor.
ACTOR_STATUS_INDEX = "actor-status-index"

# Sentinel value written to ``pollPartition`` while a job is active so
# the polling worker's GSI sees it. ``set_terminal`` REMOVEs the
# attribute, dropping the row out of that index.
ACTIVE_POLL_PARTITION = "ACTIVE"


class JobRegistry:
    """DynamoDB wrapper for Async_Job records.

    All read paths take an ``actor_id`` parameter and enforce equality
    with the stored ``actorId``; cross-actor lookups return the same
    not-found shape as a missing record (R11.2).
    """

    def __init__(self, table_name: str, region: str):
        # Same construction shape as :class:`StateStore`: resource +
        # ``.Table(...)``, no client wrapper, no session caching layer.
        self._resource = boto3.resource("dynamodb", region_name=region)
        self._table = self._resource.Table(table_name)

    # ------------------------------------------------------------------
    # Serialization helpers (snake_case Python <-> camelCase DynamoDB).
    # The mapping is deliberately explicit so a casual reader can see
    # which Python field lands on which DDB attribute.
    # ------------------------------------------------------------------

    @staticmethod
    def _to_item(record: JobRecord) -> dict:
        """Translate a :class:`JobRecord` into a DDB-attribute dict.

        Optional fields (``terminal_at``, ``last_error``) are omitted
        when ``None`` so the corresponding DDB attribute simply does
        not exist on the item — matching what the polling worker and
        the sidebar reader expect.
        """
        item: dict = {
            "jobId": record.job_id,
            "actorId": record.actor_id,
            "sessionId": record.session_id,
            "trioId": record.trio_id,
            "gatewayId": record.gateway_id,
            "statusTool": record.status_tool,
            "resultsTool": record.results_tool,
            "status": record.status,
            "startAt": record.start_at,
            "updatedAt": record.updated_at or record.start_at,
            "consecutiveErrors": record.consecutive_errors,
            "hasRenderableArtifacts": record.has_renderable_artifacts,
        }
        if record.terminal_at is not None:
            item["terminalAt"] = record.terminal_at
        if record.last_error is not None:
            item["lastError"] = record.last_error
        if record.run_id is not None:
            item["runId"] = record.run_id
        if record.turn_id is not None:
            item["turnId"] = record.turn_id
        # Persist the real results gateway distinctly from gatewayId (which
        # may hold the SFN status-routing sentinel). Falls back to
        # gateway_id when the caller didn't set it, so old readers that
        # only know about resultsGatewayId still get a usable value.
        item["resultsGatewayId"] = record.results_gateway_id or record.gateway_id
        if record.endpoint_id:
            item["endpointId"] = record.endpoint_id
        if record.delegation_grant:
            # A capability scoped to this job, not a credential: it authorises
            # polling THIS job for THIS actor and expires with it. Never logged.
            item["delegationGrant"] = record.delegation_grant
        if record.node_id:
            item["nodeId"] = record.node_id
        if record.depends_on:
            item["dependsOn"] = list(record.depends_on)
        if record.source_media:
            item["sourceMedia"] = list(record.source_media)
        if record.lineage_inputs:
            item["lineageInputs"] = list(record.lineage_inputs)
        if record.producing_project_id:
            item["producingProjectId"] = record.producing_project_id
        if record.plan_node_id:
            item["planNodeId"] = record.plan_node_id
        return item

    @staticmethod
    def _from_item(item: dict) -> JobRecord:
        """Translate a DDB-attribute dict back into a :class:`JobRecord`."""
        # ``consecutiveErrors`` arrives as a ``Decimal`` from boto3's
        # DynamoDB resource; coerce to ``int`` so callers can compare
        # it against the polling worker's threshold without surprises.
        ce_raw = item.get("consecutiveErrors", 0)
        try:
            consecutive = int(ce_raw)
        except (TypeError, ValueError):
            consecutive = 0
        return JobRecord(
            job_id=item["jobId"],
            actor_id=item["actorId"],
            session_id=item["sessionId"],
            trio_id=item["trioId"],
            gateway_id=item["gatewayId"],
            status_tool=item["statusTool"],
            results_tool=item["resultsTool"],
            status=item["status"],
            start_at=item["startAt"],
            updated_at=item.get("updatedAt", ""),
            terminal_at=item.get("terminalAt"),
            last_error=item.get("lastError"),
            consecutive_errors=consecutive,
            has_renderable_artifacts=bool(item.get("hasRenderableArtifacts", False)),
            run_id=item.get("runId"),
            turn_id=item.get("turnId"),
            results_gateway_id=item.get("resultsGatewayId"),
            node_id=item.get("nodeId"),
            depends_on=list(item.get("dependsOn", [])),
            delegation_grant=item.get("delegationGrant"),
            endpoint_id=item.get("endpointId"),
            source_media=list(item.get("sourceMedia", [])),
            lineage_inputs=list(item.get("lineageInputs", [])),
            producing_project_id=item.get("producingProjectId"),
            plan_node_id=item.get("planNodeId"),
        )

    # ------------------------------------------------------------------
    # Public API.
    # ------------------------------------------------------------------

    def register(self, record: JobRecord) -> JobRecord:
        """Idempotent registration (R3.1, R3.2, R3.4).

        Performs a conditional ``PutItem`` with
        ``attribute_not_exists(jobId)``. On conflict, fetches and
        returns the existing record so the caller sees the canonical
        registration result regardless of whether this call won the
        race (R3.4).

        First writes seed ``pollPartition="ACTIVE"`` so the polling
        worker's GSI picks the row up, and ``consecutiveErrors=0``
        so the error counter starts clean.
        """
        now = datetime.now(timezone.utc).isoformat()
        item = self._to_item(record)
        # Override updatedAt with the wall clock at write time so
        # newest-first GSI ordering reflects when the record actually
        # landed in DDB, not when the caller built the record.
        item["updatedAt"] = now
        item["pollPartition"] = ACTIVE_POLL_PARTITION
        item["consecutiveErrors"] = 0

        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(jobId)",
            )
            return self._from_item(item)
        except self._resource.meta.client.exceptions.ConditionalCheckFailedException:
            # Conflict — return the existing record (R3.4).
            existing = self._table.get_item(Key={"jobId": record.job_id}).get("Item")
            if existing is not None:
                return self._from_item(existing)
            # R3.5 fallback: the existence check failed but a follow-up
            # GetItem found nothing (e.g. the prior record was TTL-swept
            # between the two calls). Fall back to a non-conditional
            # put using the original arguments.
            logger.info(
                "register: conditional check failed for jobId=%s but record "
                "not found on follow-up read; writing unconditionally",
                record.job_id,
            )
            self._table.put_item(Item=item)
            return self._from_item(item)

    def get(self, job_id: str, *, actor_id: str) -> Optional[JobRecord]:
        """Fetch a single record, scoped to ``actor_id`` (R11.2).

        Returns ``None`` when the record is missing OR when the
        stored ``actorId`` does not match the caller-supplied
        ``actor_id`` — the cross-actor case is indistinguishable from
        the missing case so we never leak the existence of another
        actor's job.
        """
        item = self._table.get_item(Key={"jobId": job_id}).get("Item")
        if item is None:
            return None
        if item.get("actorId") != actor_id:
            return None
        return self._from_item(item)

    def list_for_actor(
        self,
        actor_id: str,
        *,
        since: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[JobRecord]:
        """Query the actor-status GSI, newest-first by ``updatedAt`` (R5.2).

        ``since`` is an ISO-8601 string; when supplied, only records
        with ``updatedAt >= since`` are returned (cheap because
        ``updatedAt`` is the GSI sort key). ``limit`` caps the page
        size; pagination beyond a single page is left to callers that
        need it.
        """
        key_cond = Key("actorId").eq(actor_id)
        if since is not None:
            key_cond = key_cond & Key("updatedAt").gte(since)

        query_kwargs: dict = {
            "IndexName": ACTOR_STATUS_INDEX,
            "KeyConditionExpression": key_cond,
            # Newest-first matches the sidebar's display order without
            # a client-side sort.
            "ScanIndexForward": False,
        }
        if limit is not None:
            query_kwargs["Limit"] = limit

        response = self._table.query(**query_kwargs)
        return [self._from_item(item) for item in response.get("Items", [])]

    def set_active_status(
        self,
        job_id: str,
        *,
        status: JobStatus,
        expected_status: JobStatus,
    ) -> bool:
        """Conditional active->active status update (R4.2).

        Mirrors :meth:`StateStore.update_node_status`: the
        ``ConditionExpression`` guards on the current status matching
        ``expected_status`` so two concurrent updates can't race a
        terminal write back to active. Returns ``False`` on guard
        violation (caller should re-read and re-decide).
        """
        now = datetime.now(timezone.utc).isoformat()
        try:
            self._table.update_item(
                Key={"jobId": job_id},
                UpdateExpression="SET #st = :s, updatedAt = :now",
                ConditionExpression=Attr("status").eq(expected_status),
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={":s": status, ":now": now},
            )
            return True
        except self._resource.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    def bump_error(self, job_id: str, error_message: str) -> int:
        """Atomically increment ``consecutiveErrors`` and stamp ``lastError`` (R4.5).

        Used by the polling worker on a non-transient status-tool
        failure. Returns the new counter value so the caller can
        decide whether to call :meth:`set_terminal` after the
        configured threshold is breached.
        """
        now = datetime.now(timezone.utc).isoformat()
        response = self._table.update_item(
            Key={"jobId": job_id},
            UpdateExpression=(
                "SET lastError = :err, updatedAt = :now "
                "ADD consecutiveErrors :one"
            ),
            ExpressionAttributeValues={
                ":err": error_message,
                ":now": now,
                ":one": 1,
            },
            ReturnValues="UPDATED_NEW",
        )
        new_value = response.get("Attributes", {}).get("consecutiveErrors", 0)
        try:
            return int(new_value)
        except (TypeError, ValueError):
            return 0

    def set_terminal(
        self,
        job_id: str,
        *,
        status: JobStatus,
        error: Optional[str] = None,
        has_renderable_artifacts: bool = False,
    ) -> bool:
        """Terminal transition with TTL stamp and pollPartition cleanup (R4.3, R5.3).

        Conditional on the current status being one of
        :data:`ACTIVE_STATUSES`. Sets ``status``, ``terminalAt``,
        ``updatedAt``, and ``ttl = epoch_now + RETENTION_SECONDS``;
        optionally sets ``lastError`` when supplied. REMOVEs
        ``pollPartition`` (so the row drops out of the polling
        worker's GSI) and ``consecutiveErrors`` (so the error counter
        is cleared on terminal transition, per the design's
        "set_terminal clears consecutiveErrors" contract).

        Returns ``False`` if the job is already terminal (guard
        prevents reverting a terminal record).
        """
        now = datetime.now(timezone.utc).isoformat()
        ttl = int(time.time()) + RETENTION_SECONDS

        set_clauses = [
            "#st = :s",
            "terminalAt = :now",
            "updatedAt = :now",
            "#ttl = :ttl",
            "hasRenderableArtifacts = :renderable",
        ]
        expr_values: dict = {
            ":s": status,
            ":now": now,
            ":ttl": ttl,
            ":renderable": bool(has_renderable_artifacts),
        }
        if error is not None:
            set_clauses.append("lastError = :err")
            expr_values[":err"] = error

        update_expression = (
            "SET " + ", ".join(set_clauses)
            + " REMOVE pollPartition, consecutiveErrors"
        )

        try:
            self._table.update_item(
                Key={"jobId": job_id},
                UpdateExpression=update_expression,
                # Same expected-status guard idiom as
                # set_active_status, generalized to "any active
                # status" via Attr.is_in for the terminal direction.
                ConditionExpression=Attr("status").is_in(list(ACTIVE_STATUSES)),
                ExpressionAttributeNames={"#st": "status", "#ttl": "ttl"},
                ExpressionAttributeValues=expr_values,
            )
            return True
        except self._resource.meta.client.exceptions.ConditionalCheckFailedException:
            return False
