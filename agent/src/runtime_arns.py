"""Resolve sub-agent runtime ARNs (TASK_/PLANNING_/RENDER_AGENT_RUNTIME_ARN) at call time.

The deploy no longer puts these ARNs in any runtime's env: an env var holding another runtime's
``agentRuntimeArn`` made CloudFormation update the runtimes one after another. They are SSM
parameters under ``RUNTIME_ARN_PARAM_PATH`` (from RuntimeConfigSecret), named for the env var:
``<path>/TASK_AGENT_RUNTIME_ARN`` etc. ``runtime_config_bootstrap`` hydrates them into os.environ
at startup, but that is not enough on its own:

* on a brand-new environment the parameters are written AFTER the runtimes, so a runtime that
  initialises first finds none of them;
* with AgentCore Runtime V2 snapshot-restore, import-time env can be captured in the snapshot.

So every use site calls :func:`get` when it needs the ARN. Env wins (local dev, tests, the
Capability API task def); otherwise SSM is read once by path and positive results are cached in
os.environ. A miss is retried at most every ``_MISS_RETRY_SECONDS``. Any failure returns ``''``,
which callers already treat as "not configured".

SELF-CONTAINED: stdlib + boto3 only, lazy client — the Capability API image and every runtime
import it without pulling in anything else.
"""
from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

_MISS_RETRY_SECONDS = 30.0

_lock = threading.Lock()
_client = None
_last_miss_at: float | None = None  # monotonic time of the last SSM read that left a gap
_warned = False


def _ssm_client(region: str):
    # Local import keeps the module importable without boto3 present (tests patch this helper).
    import boto3
    return boto3.client("ssm", region_name=region)


def _region() -> str:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"


def fetch_by_path(client, path: str) -> dict[str, str]:
    """``{last path segment: value}`` for every parameter directly under ``path``. Raises on error."""
    out: dict[str, str] = {}
    kwargs = {"Path": path.rstrip("/"), "Recursive": False}
    while True:
        resp = client.get_parameters_by_path(**kwargs)
        for p in resp.get("Parameters") or []:
            name = str(p.get("Name") or "")
            value = p.get("Value")
            if name and value:
                out[name.rsplit("/", 1)[-1]] = str(value)
        token = resp.get("NextToken")
        if not token:
            return out
        kwargs["NextToken"] = token


def _warn_once(msg: str, *args) -> None:
    global _warned
    if not _warned:
        _warned = True
        logger.warning(msg, *args)


def get(env_name: str) -> str:
    """The runtime ARN for ``env_name``, or ``''`` when it cannot be resolved. Never raises."""
    global _client, _last_miss_at
    value = os.environ.get(env_name, "")
    if value:
        return value
    path = os.environ.get("RUNTIME_ARN_PARAM_PATH", "").strip()
    if not path:
        return ""
    with _lock:
        value = os.environ.get(env_name, "")  # another thread may have filled it
        if value:
            return value
        now = time.monotonic()
        if _last_miss_at is not None and now - _last_miss_at < _MISS_RETRY_SECONDS:
            return ""
        try:
            if _client is None:
                _client = _ssm_client(_region())
            found = fetch_by_path(_client, path)
        except Exception as e:  # noqa: BLE001 — a missing ARN degrades to "not configured"
            _last_miss_at = now
            _warn_once("runtime_arns: could not read %s: %s", path, e)
            return ""
        for k, v in found.items():
            os.environ.setdefault(k, v)
        value = os.environ.get(env_name, "")
        if not value:
            _last_miss_at = now
            _warn_once("runtime_arns: %s not found under %s", env_name, path)
        return value


def _reset_for_tests() -> None:
    global _client, _last_miss_at, _warned
    with _lock:
        _client = None
        _last_miss_at = None
        _warned = False
