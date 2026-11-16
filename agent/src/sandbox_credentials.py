"""Short-lived S3 credentials for the code sandbox, scoped to one (project, session).

asset-lineage Phase 3 (b), design §9.2. The code interpreter sandbox runs MODEL-AUTHORED code under one
execution role shared by every actor, project and session. This module assumes ``SandboxScopedS3Role``
(``SANDBOX_SCOPED_ROLE_ARN``, from RuntimeConfigSecret) with STS session tags ``project_id`` /
``session_id``; that role's policy uses ``${aws:PrincipalTag/...}`` so the credentials can write ONLY
``projects/<pid>/sessions/<sid>/*`` and ``artifacts/<sid>/*`` and read only ``projects/<pid>/*`` plus
legacy ``artifacts/*``. No delete anywhere.

The credentials are delivered OUT OF BAND (:func:`injection_code`, run by the harness through the
interpreter client, never through the model's tool input), as a ``~/.aws/credentials`` ``[default]``
profile. The 2026-10-09 staging probe showed why that is enough: the sandbox has NO ``AWS_*`` env vars,
its ambient credentials resolve as ``iam-role``, ``~`` is writable, and once the file exists boto3 in
the kernel AND in ``executeCommand`` shells resolves ``shared-credentials-file`` first.

Never logs a credential value. :func:`credentials_for` returns ``None`` on failure; :func:`install` — what
every sandbox caller uses — turns any failure into :class:`SandboxCredentialsError` and the caller FAILS
CLOSED: since B2 the sandbox's own execution role has no artifact/project S3 access, so a cell is never
run on it in place of the scoped credentials.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

ROLE_ARN_ENV = "SANDBOX_SCOPED_ROLE_ARN"
DURATION_SECONDS = 3600  # role chaining caps a session at 1 h anyway
# Served credentials always have at least this long left. The sandbox cannot refresh them itself (static
# `[default]` profile, and once that file exists boto3 never falls back to the sandbox's own role), so a
# long cell — a 1080p re-encode — that starts on nearly-expired credentials fails its final upload with
# `ExpiredToken`. Re-assuming after 15 min of use keeps every call's window >= 45 min (chain cap: 60).
REFRESH_BEFORE = timedelta(minutes=45)
NO_PROJECT = "none"

_lock = threading.Lock()
_cache: dict[tuple[str, str], dict] = {}
_sts: Any = None


def role_arn() -> str:
    return (os.environ.get(ROLE_ARN_ENV) or "").strip()


def _client():
    global _sts
    if _sts is None:
        import boto3

        region = os.environ.get("AWS_REGION", "us-west-2")
        _sts = boto3.client("sts", region_name=region,
                            endpoint_url=f"https://sts.{region}.amazonaws.com")
    return _sts


def assume_role_params(pid: Optional[str], sid: str, arn: str) -> dict:
    """The exact ``AssumeRole`` request (pinned against botocore's STS model in the tests)."""
    tag_pid = pid or NO_PROJECT
    return {
        "RoleArn": arn,
        "RoleSessionName": f"sbx-{sid[:8]}-{tag_pid[-8:]}"[:64],
        "DurationSeconds": DURATION_SECONDS,
        "Tags": [{"Key": "project_id", "Value": tag_pid}, {"Key": "session_id", "Value": sid}],
    }


def _fresh(creds: dict, now: datetime) -> bool:
    exp = creds.get("Expiration")
    return isinstance(exp, datetime) and exp - now > REFRESH_BEFORE


def credentials_for(pid: Optional[str], sid: Optional[str], *, now: Optional[datetime] = None) -> Optional[dict]:
    """Scoped credentials for (``pid``, ``sid``): ``{AccessKeyId, SecretAccessKey, SessionToken, Expiration}``.

    ``None`` when the role is not configured (tests, envs without it), there is no session, or STS refuses.
    Cached per (pid, sid) and refreshed when less than ``REFRESH_BEFORE`` (45 min) remains. ``pid`` must be a TRUSTED project
    id (``jobs.context.trusted_project_id``); ``None`` tags ``project_id=none`` (legacy writes only).
    """
    arn = role_arn()
    if not arn or not isinstance(sid, str) or not sid:
        return None
    now = now or datetime.now(timezone.utc)
    key = (pid or NO_PROJECT, sid)
    with _lock:
        hit = _cache.get(key)
        if hit is not None and _fresh(hit, now):
            return hit
    try:
        resp = _client().assume_role(**assume_role_params(pid, sid, arn))
        creds = resp["Credentials"]
    except Exception as exc:  # noqa: BLE001 — fail closed but recoverable: no injection
        logger.warning("sandbox_credentials: assume failed for session %s: %s", sid[:8], type(exc).__name__)
        return None
    logger.info("sandbox_credentials: assumed scoped role for session %s (project %s)",
                sid[:8], key[0][-8:])
    with _lock:
        _cache[key] = creds
    return creds


def fingerprint(pid: Optional[str], sid: str, creds: dict) -> tuple:
    exp = creds.get("Expiration")
    return (pid or NO_PROJECT, sid, exp.isoformat() if isinstance(exp, datetime) else str(exp))


def injection_code(creds: dict) -> str:
    """Python, run in the sandbox kernel by the HARNESS, that installs ``creds`` as the default profile.

    Writes ``~/.aws/credentials`` (0600) and drops boto3's cached default session so a client created
    afterwards resolves the new file. Prints only :data:`INSTALLED_SENTINEL` (never a credential), which
    :func:`install` requires before it reports success.
    """
    body = (
        "[default]\n"
        f"aws_access_key_id = {creds['AccessKeyId']}\n"
        f"aws_secret_access_key = {creds['SecretAccessKey']}\n"
        f"aws_session_token = {creds['SessionToken']}\n"
    )
    return (
        "import os as _o\n"
        "_d = _o.path.expanduser('~/.aws'); _o.makedirs(_d, exist_ok=True)\n"
        "_p = _o.path.join(_d, 'credentials')\n"
        "_f = _o.open(_p, _o.O_WRONLY | _o.O_CREAT | _o.O_TRUNC, 0o600)\n"
        f"_o.write(_f, {body!r}.encode()); _o.close(_f)\n"
        "try:\n"
        "    import boto3 as _b; _b.DEFAULT_SESSION = None\n"
        "except Exception:\n"
        "    pass\n"
        "del _o, _d, _p, _f\n"
        f"print({INSTALLED_SENTINEL!r})\n"
    )


#: Printed by :func:`injection_code` once the file is written; :func:`install` requires it.
INSTALLED_SENTINEL = "__SBX_SCOPED_CREDS_INSTALLED__"


class SandboxCredentialsError(RuntimeError):
    """Scoped credentials are required for this sandbox call and could not be installed.

    Raised instead of letting model code run on whatever the sandbox's own role allows. Since B2 the
    code interpreter's execution role holds NO artifact/project S3 grants (only ``tools/ffmpeg/*``), so
    the scoped credentials are the ONLY S3 access a cell has — and running a cell without them would at
    best fail later with an opaque ``AccessDenied``. The message never carries a credential."""


def required() -> bool:
    """True where the deploy configured the scoped role, i.e. every deployed env (it is on by default).

    Unset only in unit tests and local runs, where the sandbox is not ours to scope (the AWS-managed
    ``aws.codeinterpreter.v1`` has no S3 access at all)."""
    return bool(role_arn())


def _stdout_of(resp: Any) -> str:
    """stdout text of an ``invoke("executeCode")`` streaming response (same shape ``crop_video`` reads)."""
    stream = resp.get("stream") if isinstance(resp, dict) else None
    if stream is None:
        return ""
    chunks: list[str] = []
    for event in stream:
        result = event.get("result") if isinstance(event, dict) else None
        if not isinstance(result, dict):
            continue
        sc_ = result.get("structuredContent")
        if isinstance(sc_, dict) and sc_.get("stdout"):
            chunks.append(str(sc_["stdout"]))
        for block in result.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                chunks.append(str(block["text"]))
    return "\n".join(chunks)


def install(invoke: Any, pid: Optional[str], sid: Optional[str], setup: str = "") -> dict:
    """Install scoped credentials for (``pid``, ``sid``) through ``invoke`` (the interpreter client's
    ``invoke``) and CONFIRM they landed. Returns the credentials; raises :class:`SandboxCredentialsError`
    on any failure — no session to scope to, STS refused, the sandbox call failed, or the program's
    sentinel never came back (a file that may not exist must not be assumed to).

    ``setup`` is more harness code for the same sandbox call (the ffmpeg launchers, :mod:`sandbox_ffmpeg`). It runs
    AFTER the credentials are written, and a failure in it never fails the install."""
    if not isinstance(sid, str) or not sid:
        raise SandboxCredentialsError("no chat session is bound to this call, so the sandbox cannot be scoped")
    creds = credentials_for(pid, sid)
    if creds is None:
        raise SandboxCredentialsError("could not obtain session-scoped S3 credentials (STS refused or unavailable)")
    try:
        code = injection_code(creds)
        if setup:
            code += "try:\n" + "".join(f"    {line}\n" for line in setup.splitlines()) + "except Exception:\n    pass\n"
        resp = invoke("executeCode", {"code": code, "language": "python"})
    except Exception as exc:  # noqa: BLE001 - re-raised as the typed error, message kept (it names inactive sessions)
        raise SandboxCredentialsError(f"installing scoped credentials failed: {type(exc).__name__}: {exc}") from exc
    if INSTALLED_SENTINEL not in _stdout_of(resp):
        raise SandboxCredentialsError("the sandbox did not confirm the scoped credentials were installed")
    return creds


def _reset_for_tests() -> None:
    global _sts
    with _lock:
        _cache.clear()
    _sts = None
