"""CloudFormation custom-resource entry point for the deploy-time preload (design Task outline 1).

``PreloadFn`` runs on stack Create/Update (``DependsOn`` the user pool, projects table, Ingest bucket,
skills store and ingestion pipeline). It:

1. locates the preload package at ``preloadPath`` — a FOLDER, or a ``.zip`` it unpacks into
   ``<path>-unpacked/`` first — and finds its project directories (each a folder holding a
   ``manifest.json``; :func:`preload.package.discover_projects`);
2. **no-ops** (SUCCESS) when there is no project directory — the harness deploys exactly as today;
3. **fails loudly** when a package is present but ``ownerEmail`` is empty (it cannot choose an owner);
4. otherwise builds the clients and calls :func:`preload.runner.run_preload`.

Delete is a success no-op (preloaded content is owned data, never torn down by a stack delete). The
decision logic (:func:`locate_package`) is separated from the CFN plumbing so it is unit-testable without a
real event.
"""
from __future__ import annotations

import io
import json
import logging
import urllib.request
import zipfile
from dataclasses import dataclass
from typing import Any, Optional

from .package import discover_projects, object_exists, root_prefix

log = logging.getLogger("preload.handler")
log.setLevel(logging.INFO)

UNPACKED_SUFFIX = "-unpacked"  # keep in step with `infra/lib/preload.ts` (the role's write grant)


class PreloadConfigError(RuntimeError):
    """A package is present but the deploy cannot act on it (e.g. no ownerEmail) — fail the deploy."""


@dataclass(frozen=True)
class Decision:
    action: str            # "run" | "noop"
    package_root: Optional[str] = None
    reason: str = ""


def locate_package(s3: Any, bucket: str, preload_path: str, owner_email: str) -> Decision:
    """Decide whether to run, unpacking a ``.zip`` first. Raises :class:`PreloadConfigError` on fail-loud.

    A package is "present" when its root holds at least one project directory (a folder with a
    ``manifest.json``); anything less is the no-package no-op.
    """
    path = (preload_path or "preload/").strip()
    package_root = path.rstrip("/")

    if path.endswith(".zip"):
        if not object_exists(s3, bucket, path):
            return Decision(action="noop", reason=f"no preload package at s3://{bucket}/{path}")
        package_root = _unpack_zip(s3, bucket, path)

    projects = discover_projects(s3, bucket, package_root)
    if not projects:
        return Decision(action="noop", reason=(
            f"no preload package at s3://{bucket}/{path} (no project folder holding manifest.json under "
            f"s3://{bucket}/{root_prefix(package_root)})"))
    if not owner_email:
        raise PreloadConfigError(
            f"a preload package is present at s3://{bucket}/{path} but no ownerEmail was configured")
    return Decision(action="run", package_root=package_root,
                    reason=f"{len(projects)} project(s): " + ", ".join(p.name for p in projects))


def _clear_prefix(s3: Any, bucket: str, prefix: str) -> None:
    """Delete every object under ``prefix`` (the zip's scratch unpack area, owned by the preload alone)."""
    token = None
    while True:
        kw: dict = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kw)
        keys = [{"Key": o["Key"]} for o in resp.get("Contents", []) or []]
        if keys:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True})
        if not resp.get("IsTruncated"):
            return
        token = resp.get("NextContinuationToken")


def _unpack_zip(s3: Any, bucket: str, zip_key: str) -> str:
    """Extract a package ``.zip`` into ``<zip_key without .zip>-unpacked/`` in the same bucket; return it.

    The unpack area is cleared first, so a file removed from the zip since the last deploy does not linger
    there and get picked up again (a glob or a skill's file set would otherwise still see it).
    """
    dest = zip_key[: -len(".zip")].rstrip("/") + UNPACKED_SUFFIX
    body = s3.get_object(Bucket=bucket, Key=zip_key)["Body"].read()
    _clear_prefix(s3, bucket, dest + "/")
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            s3.put_object(Bucket=bucket, Key=f"{dest}/{info.filename}", Body=zf.read(info.filename))
    return dest


def _run(props: dict) -> dict:
    import boto3

    from ingestion.pipeline_store import PipelineStore
    from lab.asset_index import AssetIndex
    from .runner import run_preload

    bucket = props["IngestBucket"]
    owner_email = props.get("OwnerEmail") or ""
    preload_path = props.get("PreloadPath") or "preload/"
    user_pool_id = props["UserPoolId"]
    table_name = props["ProjectsTable"]
    asset_index_table = props["AssetIndexTable"]
    artifacts_bucket = props["ArtifactsBucket"]

    s3 = boto3.client("s3")
    decision = locate_package(s3, bucket, preload_path, owner_email)
    if decision.action == "noop":
        log.info("preload: %s", decision.reason)
        return {"noop": True, "reason": decision.reason}
    log.info("preload: running %s", decision.reason)

    idp = boto3.client("cognito-idp")
    store = PipelineStore(boto3.resource("dynamodb").Table(table_name))
    skill_index = AssetIndex(ddb_table=boto3.resource("dynamodb").Table(asset_index_table))
    result = run_preload(s3=s3, store=store, idp=idp, bucket=bucket, package_root=decision.package_root,
                         user_pool_id=user_pool_id, owner_email=owner_email,
                         skill_index=skill_index, artifacts_bucket=artifacts_bucket)
    summary = result.summary()
    log.info("preload complete: %s", json.dumps(summary))
    return summary


def handler(event: dict, context: Any) -> dict:
    """CloudFormation custom-resource handler. Create/Update runs the preload; Delete is a no-op."""
    request_type = event.get("RequestType")
    props = event.get("ResourceProperties", {}) or {}
    status, data = "SUCCESS", {}
    try:
        if request_type in ("Create", "Update"):
            data = _run(props)
        else:  # Delete — never tear down preloaded data
            data = {"noop": True, "reason": f"{request_type} is a no-op"}
    except Exception as exc:  # noqa: BLE001 — a deploy failure must surface as a FAILED signal
        log.exception("preload failed")
        status, data = "FAILED", {"error": str(exc)[:1000]}
    _send(event, context, status, data)
    return {"status": status, **({"data": data} if status == "SUCCESS" else {"reason": data.get("error")})}


def _send(event: dict, context: Any, status: str, data: dict) -> None:
    """POST a CloudFormation custom-resource response (no cfnresponse dependency)."""
    url = event.get("ResponseURL")
    if not url:  # local / unit invocation
        return
    body = json.dumps({
        "Status": status,
        "Reason": (data.get("error") or "see CloudWatch logs")[:1000],
        "PhysicalResourceId": event.get("PhysicalResourceId") or (context and getattr(context, "log_stream_name", "preload")),
        "StackId": event.get("StackId"),
        "RequestId": event.get("RequestId"),
        "LogicalResourceId": event.get("LogicalResourceId"),
        "Data": data if status == "SUCCESS" else {},
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="PUT", headers={"content-type": ""})
    urllib.request.urlopen(req)  # noqa: S310 — the pre-signed S3 URL CloudFormation provides
