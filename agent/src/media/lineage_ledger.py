"""The per-turn lineage ledger for agents that run WITHOUT the harness hook provider (plan nodes).

``media.turn_outputs`` is fed by ``hooks.provider.HarnessHookProvider._on_after_tool``, but a plan node's
agent is built with no hooks at all (the render hook must not fire mid-plan), so whatever a node made was
registered with no inputs (asset-lineage proposal §2.2 gap 2). With ``projects.lineageEdges`` on, a node
gets :class:`LedgerHooks` — the ledger callback ALONE — and :func:`write_node_lineage` persists what it
recorded next to the node's durable output (``plans/<sid>/<plan>/<node>/lineage.json``), where the
Generated_Media registrar reads it when the plan settles.

Never raises into a node.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from projects import lineage_contract as lc

logger = logging.getLogger(__name__)

__all__ = ["LedgerHooks", "node_hooks", "node_lineage_key", "write_node_lineage"]


def node_lineage_key(session_id: str, plan_id: str, node_id: str) -> str:
    return f"plans/{session_id}/{plan_id}/{node_id}/lineage.json"


class LedgerHooks:
    """A Strands hook provider (duck-typed, like ``HarnessHookProvider``) that feeds ``turn_outputs`` from every
    finished tool call, and nothing else: no render, no user hooks."""

    def register_hooks(self, registry: Any, **_kw: Any) -> None:
        from strands.hooks.events import AfterToolCallEvent
        registry.add_callback(AfterToolCallEvent, self._after_tool)

    @staticmethod
    def _after_tool(event: Any) -> None:
        try:
            from media import turn_outputs
            tool_use = getattr(event, "tool_use", None) or {}
            if isinstance(tool_use, dict):
                turn_outputs.note_tool_result(tool_use.get("name", ""), tool_use.get("input"),
                                              getattr(event, "result", None))
        except Exception as exc:  # noqa: BLE001
            logger.debug("lineage ledger: tool result not recorded: %s", exc)


def node_hooks() -> list:
    """``[LedgerHooks()]`` with ``projects.lineageEdges`` on, else ``[]`` (a node then runs exactly as before)."""
    try:
        if lc.edges_enabled():
            return [LedgerHooks()]
    except Exception:  # noqa: BLE001
        pass
    return []


def write_node_lineage(s3_client: Any, bucket: str, session_id: str, plan_id: str, node_id: str) -> int:
    """Persist this node's ledger (``via=plan_node``) beside its durable output. Returns the entry count."""
    try:
        if not (lc.edges_enabled() and bucket and session_id and plan_id and node_id):
            return 0
        from media import turn_outputs
        entries = [{**e, "via": e.get("via") or "plan_node"} for e in turn_outputs.lineage()]
        if not entries:
            return 0
        s3_client.put_object(Bucket=bucket, Key=node_lineage_key(session_id, plan_id, node_id),
                             Body=json.dumps({"schema": "node-lineage/v1", "node_id": node_id,
                                              "lineage": entries}).encode("utf-8"),
                             ContentType="application/json")
        return len(entries)
    except Exception as exc:  # noqa: BLE001 - lineage never fails a node
        logger.warning("lineage ledger: node %s lineage not written: %s", node_id, exc)
        return 0
