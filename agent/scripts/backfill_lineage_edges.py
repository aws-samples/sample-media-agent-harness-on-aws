#!/usr/bin/env python3
"""Build lineage EDGE records (``UP#<child>#<parent>`` / ``DOWN#<parent>#<child>``) from existing ``derivedFrom``.

Before ``projects.lineageEdges``, lineage lived only in each ASSET row's ``derivedFrom`` list. This writes the edge
items for every (child, parent) pair those lists name, carrying what the row knows about its producer — ``tool``,
``jobId``, ``turnId``, ``sessionId`` from ``producer`` — with ``via: backfill``. Idempotent: an edge that already
exists (written live, or by an earlier run) keeps its own fields (``LineageEdges.put_edge`` writes only absent
fields), so re-running writes nothing new. A parent with no row (or the row itself) is skipped and counted. Rows
are read, never modified: ``derivedFrom`` stays exactly as it is.

Safety (same pattern as ``migrate_chat_folders.py``):

* Dry run is the default (prints what it would write). Writes need ``--execute``.
* ``--env`` is required and the ambient credentials must be that env's account; anything else refuses.
* ``prod`` additionally requires ``--i-understand-this-is-production``.
* ``--project <pid>`` (repeatable) limits it to those Projects.

Usage::

    AWS_PROFILE=<p> uv run python scripts/backfill_lineage_edges.py --env staging                   # dry run
    AWS_PROFILE=<p> uv run python scripts/backfill_lineage_edges.py --env staging --execute
    AWS_PROFILE=<p> uv run python scripts/backfill_lineage_edges.py --env staging --project p<32hex> --execute
"""
from __future__ import annotations

import argparse
import collections
import os
import sys
from typing import Any, Iterable, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ingestion.lineage_edges import LineageEdges  # noqa: E402
from projects import lineage_contract as lc  # noqa: E402
from projects import schema  # noqa: E402

ENVIRONMENTS = {"sandbox": "100000000001", "staging": "100000000002", "prod": "100000000003"}
REGION = "us-west-2"
PROJECT_NAME = "mediaworks-harness"


def verify_caller(env: str, sts: Any) -> str:
    """Refuse unless the ambient credentials are ``env``'s account."""
    identity = sts.get_caller_identity()
    account, expected = identity["Account"], ENVIRONMENTS[env]
    if account != expected:
        raise SystemExit(f"refusing to act: --env {env} expects account {expected}, but the current credentials "
                         f"are account {account} ({identity.get('Arn')})")
    return account


def derived_rows(table: Any, projects: Optional[Iterable[str]] = None) -> dict[str, list[dict]]:
    """Project id -> its ASSET rows that carry a non-empty ``derivedFrom`` (a Query per named Project, else a Scan)."""
    from boto3.dynamodb.conditions import Attr, Key

    proj = "PK, SK, derivedFrom, producer, sessionId, jobId"
    by_pid: dict[str, list[dict]] = collections.defaultdict(list)
    if projects:
        for pid in projects:
            kwargs: dict = {"KeyConditionExpression": Key("PK").eq(schema.project_pk(pid))
                            & Key("SK").begins_with(schema.ASSET_SK_PREFIX), "ProjectionExpression": proj}
            while True:
                resp = table.query(**kwargs)
                by_pid[pid].extend(r for r in resp.get("Items", []) if r.get("derivedFrom"))
                if not resp.get("LastEvaluatedKey"):
                    break
                kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return by_pid
    kwargs = {"FilterExpression": Attr("SK").begins_with(schema.ASSET_SK_PREFIX) & Attr("derivedFrom").exists(),
              "ProjectionExpression": proj}
    while True:
        resp = table.scan(**kwargs)
        for row in resp.get("Items", []):
            if row.get("derivedFrom"):
                by_pid[str(row["PK"]).split("#", 1)[-1]].append(row)
        if not resp.get("LastEvaluatedKey"):
            return by_pid
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _s(v: Any) -> Optional[str]:
    return v if isinstance(v, str) and v else None


def run(table: Any, *, execute: bool, projects: Optional[Iterable[str]] = None, out=print) -> dict:
    """Write (or, without ``execute``, report) one edge per ``derivedFrom`` pair, Project by Project."""
    edges = LineageEdges(table)
    pairs = written = existing = skipped = missing = 0
    known: dict[tuple[str, str], bool] = {}

    def parent_exists(pid: str, aid: str) -> bool:
        if (pid, aid) not in known:
            known[(pid, aid)] = bool(table.get_item(Key=schema.asset_key(pid, aid), ProjectionExpression="PK")
                                     .get("Item"))
        return known[(pid, aid)]

    for pid, rows in sorted(derived_rows(table, projects).items()):
        for r in rows:
            child = str(r["SK"])[len(schema.ASSET_SK_PREFIX):]
            producer = r.get("producer") if isinstance(r.get("producer"), dict) else {}
            for parent in dict.fromkeys(r.get("derivedFrom") or []):
                if not (schema.is_asset_id(parent) and schema.is_asset_id(child)) or parent == child:
                    skipped += 1
                    continue
                pairs += 1
                if not parent_exists(pid, parent):
                    missing += 1  # still written: the read API shows it as a "missing" ghost, history kept
                there =table.get_item(Key={"PK": schema.project_pk(pid), "SK": lc.edge_up_sk(child, parent)},
                                       ProjectionExpression="PK").get("Item")
                if there:
                    existing += 1
                    continue
                out(f"{'write' if execute else 'would write'} {pid}: {parent} -> {child} "
                    f"(tool={_s(producer.get('tool'))}, job={_s(r.get('jobId')) or _s(producer.get('jobId'))})")
                if execute:
                    ok = edges.put_edge(pid, child, parent, tool=_s(producer.get("tool")),
                                        job_id=_s(r.get("jobId")) or _s(producer.get("jobId")),
                                        turn_id=_s(producer.get("turnId")),
                                        session_id=_s(r.get("sessionId")) or _s(producer.get("sessionId")),
                                        via="backfill")
                    if ok:
                        written += 1
                    else:
                        skipped += 1
    summary = {"pairs": pairs, "written": written, "alreadyThere": existing, "skipped": skipped, "parentMissing": missing,
               "dryRun": not execute}
    out(f"summary: {summary}")
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", required=True, choices=sorted(ENVIRONMENTS))
    parser.add_argument("--execute", action="store_true", help="write (default: dry run)")
    parser.add_argument("--project", action="append", default=[], help="limit to this Project id (repeatable)")
    parser.add_argument("--i-understand-this-is-production", dest="prod_ok", action="store_true")
    parser.add_argument("--projects-table", default=f"{PROJECT_NAME}-projects")
    args = parser.parse_args(argv)
    if args.env == "prod" and args.execute and not args.prod_ok:
        raise SystemExit("refusing to write prod without --i-understand-this-is-production")

    import boto3

    account = verify_caller(args.env, boto3.client("sts", region_name=REGION))
    print(f"env={args.env} account={account} projects={args.projects_table} "
          f"{'EXECUTE' if args.execute else 'dry run'}")
    run(boto3.resource("dynamodb", region_name=REGION).Table(args.projects_table), execute=args.execute,
        projects=args.project or None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
