"""Resolve OUR artifact URLs to S3 objects, and read them by key.

An artifact URL is a stable IDENTIFIER, not a fetchable address. The three forms all name
one object in the skills/artifacts bucket:

* ``https://<ARTIFACTS_DOMAIN>/artifacts/<rest>`` — the CloudFront form the tools hand out;
* ``[https://<host>]/api/artifact?key=artifacts/<rest>`` — the UI proxy read-through form;
* a bare ``artifacts/<rest>`` key.

One reserved namespace under that prefix names an object we do NOT own the bytes of:
``artifacts/m2c/<bucket>/<key>`` is an M2C_REFERENCE — the object M2C wrote, referenced where
it wrote it (asset-lineage D4/R4), never copied. It is an artifact key like any other so every
shape check, the proxy route and the SPA resolver accept it unchanged, but it resolves to
``<bucket>``, not to the skills bucket. See :func:`m2c_artifact_ref`.

When the artifacts CDN is private (``artifactsCdnPrivate`` in an env's config), an HTTP GET
of the first two forms returns 403 to the runtime, so every agent-side self-fetch reads the
object through boto3 by key instead. That is done ALWAYS, not behind the flag: S3 by key is
the same bytes without a CDN round trip, and it keeps working the moment the CDN is locked.

External tools (fal.ai, user MCP servers) fetch over the internet and cannot use a key, so
when the flag is on their tool input is rewritten to short-lived presigned S3 GETs
(:func:`presign_artifact_urls_in`, wired in ``jobs/strands_tool_wrap``). Nothing stores the
presigned URL: the identifier stays what the agent, the manifests and the UI keep.

Env is read at CALL time, never at import: ``runtime_config_bootstrap`` hydrates
``os.environ`` from the runtime-config secret, and a module imported before that ran would
otherwise freeze an empty bucket/domain.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from typing import Any, Optional
from urllib.parse import parse_qs, quote, unquote, urlparse

logger = logging.getLogger(__name__)

ARTIFACTS_KEY_PREFIX = "artifacts/"
_PROXY_PATH = "/api/artifact"
_TRUE = ("1", "true", "yes")

#: Reserved namespace for an M2C_REFERENCE: ``artifacts/m2c/<bucket>/<key>``.
M2C_REF_PREFIX = ARTIFACTS_KEY_PREFIX + "m2c/"

# S3 bucket naming (lower-case letters, digits, dots, hyphens; 3-63 chars). Keeping the check
# strict is what stops a reference from naming something that is not a bucket at all.
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$")

_S3_CLIENT: Any = None
_S3_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def artifacts_bucket() -> str:
    """The skills/artifacts bucket, resolved the way ``hooks/actions`` and ``persist_media`` do."""
    return os.environ.get("SKILLS_S3_BUCKET") or os.environ.get("S3_BUCKET", "")


def artifacts_domain() -> str:
    """``ARTIFACTS_DOMAIN`` as a bare lower-cased host (tolerates a configured scheme / slash)."""
    raw = (os.environ.get("ARTIFACTS_DOMAIN") or "").strip().lower()
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    return raw.strip("/")


def cdn_private() -> bool:
    """True when ``ARTIFACTS_CDN_PRIVATE`` is on (``1``/``true``/``yes``, any case)."""
    return (os.environ.get("ARTIFACTS_CDN_PRIVATE") or "").strip().lower() in _TRUE


def _client():
    """Lazy, shared boto3 S3 client (SigV4 so presigned URLs work in every region).

    Virtual addressing puts presigned URLs on the REGIONAL host (``<bucket>.s3.<region>.amazonaws.com``).
    On the global host a bucket younger than about a day still routes to us-east-1, which rejects a
    us-west-2 SigV4 credential with 403 ``AuthorizationQueryParametersError`` — every sandbox crop of a fresh
    customer install failed that way (E2E 2026-10-12).
    """
    global _S3_CLIENT
    if _S3_CLIENT is None:
        with _S3_LOCK:
            if _S3_CLIENT is None:
                import boto3
                from botocore.config import Config

                _S3_CLIENT = boto3.client(
                    "s3",
                    region_name=os.environ.get("AWS_REGION", "us-west-2"),
                    config=Config(signature_version="s3v4",
                                  s3={"addressing_style": "virtual"},
                                  retries={"max_attempts": 3, "mode": "standard"}),
                )
    return _S3_CLIENT


# ---------------------------------------------------------------------------
# Identifier -> key
# ---------------------------------------------------------------------------

def _normalize_key(key: str) -> Optional[str]:
    """Validate an ``artifacts/...`` key, or a projects-session OUTPUT key
    (``projects/<pid>/sessions/<sid>/output/…``, asset-lineage Phase 3 (a)); collapse a doubled
    prefix. None when unusable. Project ASSET keys (``projects/<pid>/<assetId>/…``) are not artifact
    keys: they stay asset-id-gated."""
    from projects.storage_layout import is_project_session_output_key

    if not isinstance(key, str):
        return None
    key = key.lstrip("/")
    if key.startswith("projects/"):
        return key if is_project_session_output_key(key) else None
    while key.startswith(ARTIFACTS_KEY_PREFIX + ARTIFACTS_KEY_PREFIX):
        key = key[len(ARTIFACTS_KEY_PREFIX):]
    if not key.startswith(ARTIFACTS_KEY_PREFIX) or len(key) == len(ARTIFACTS_KEY_PREFIX):
        return None
    if ".." in key or "\\" in key:
        return None
    return key


def own_artifact_key(url: str, domain: Optional[str] = None, _norm: Any = None) -> Optional[str]:
    """The S3 key for one of OUR artifact URLs, else None.

    Accepts the CloudFront form (host must equal ``ARTIFACTS_DOMAIN``, or ``domain`` when
    given), the ``/api/artifact?key=`` proxy form (relative, or on any https host — the key
    is what identifies the object and it must still be ``artifacts/...``), and a bare
    ``artifacts/...`` key. Rejects ``..``/backslashes and anything else: fal.media, presigned
    ``amazonaws.com`` URLs, other hosts, ``s3://`` URIs.
    """
    norm = _norm or _normalize_key
    if not isinstance(url, str):
        return None
    s = url.strip()
    if not s:
        return None
    if s.startswith((ARTIFACTS_KEY_PREFIX, "projects/")):
        return norm(s)
    try:
        parsed = urlparse(s)
    except ValueError:
        return None
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("", "http", "https"):
        return None
    if not scheme and parsed.netloc:
        return None  # protocol-relative "//host/..." — not a form we hand out
    # Proxy read-through form, relative or absolute.
    if parsed.path.rstrip("/") == _PROXY_PATH and (scheme or s.startswith("/")):
        keys = parse_qs(parsed.query).get("key") or []
        return norm(keys[0]) if keys else None
    if not scheme:
        return None
    host = (parsed.hostname or "").lower()
    # Our own bucket's presigned S3 GET (what "Copy source URL" puts on the clipboard, and
    # what the browser renders): identify it by key so it is read fresh by S3 and never
    # treated as an expiring external URL. Virtual-hosted (`<bucket>.s3[.<region>].amazonaws.com`)
    # and path-style (`s3[.<region>].amazonaws.com/<bucket>/...`) forms.
    bucket = artifacts_bucket().lower()
    if bucket and host.endswith(".amazonaws.com"):
        path = unquote(parsed.path)
        if host.startswith(bucket + ".s3"):
            return norm(path)
        if host.startswith("s3.") or host == "s3.amazonaws.com":
            prefix = "/" + bucket + "/"
            if path.lower().startswith(prefix):
                return norm(path[len(prefix):])
        return None
    dom = (domain if domain is not None else artifacts_domain()).strip().lower()
    if "://" in dom:
        dom = dom.split("://", 1)[1]
    dom = dom.strip("/")
    if not dom or host != dom.split(":", 1)[0]:
        return None
    return norm(unquote(parsed.path))


# ---------------------------------------------------------------------------
# M2C references (asset-lineage D4/R4)
# ---------------------------------------------------------------------------

def m2c_artifact_ref(bucket: Any, key: Any) -> Optional[str]:
    """The stable artifact key for an object referenced IN PLACE in the M2C proxy bucket:
    ``artifacts/m2c/<bucket>/<key>``. None when ``bucket``/``key`` cannot name one object.

    Why a key under ``artifacts/`` rather than a new URL scheme: every layer between the render
    pipeline and the player already accepts an artifact key as an identity (the proxy's
    ``key must be under artifacts/`` gate, ``isArtifactKey``, the SPA resolver, this module), so
    the reference travels with no new plumbing. And deliberately NOT the CloudFront form: no
    object exists at that CDN path, and a URL that looks fetchable but 403s is the broken-link
    failure this reference removes.
    """
    if not isinstance(bucket, str) or not isinstance(key, str):
        return None
    b = bucket.strip().lower()
    k = key.strip().lstrip("/")
    if not _BUCKET_RE.match(b):
        return None
    if not k or k.endswith("/") or ".." in k or "\\" in k:
        return None
    return f"{M2C_REF_PREFIX}{b}/{k}"


def _split_m2c_ref_key(key: Any) -> Optional[tuple[str, str]]:
    """``(bucket, key)`` of an ``artifacts/m2c/<bucket>/<key>`` reference KEY, else None."""
    if not isinstance(key, str) or not key.startswith(M2C_REF_PREFIX):
        return None
    bucket, _, rest = key[len(M2C_REF_PREFIX):].partition("/")
    return (bucket, rest) if m2c_artifact_ref(bucket, rest) else None


def parse_m2c_artifact_ref(ref: Any, domain: Optional[str] = None) -> Optional[tuple[str, str]]:
    """``(bucket, key)`` an M2C_REFERENCE names, in ANY of our identifier forms, else None."""
    return _split_m2c_ref_key(own_artifact_key(ref, domain) if isinstance(ref, str) else None)


def is_m2c_artifact_ref(ref: Any, domain: Optional[str] = None) -> bool:
    """True when ``ref`` is an M2C_REFERENCE in any of our identifier forms."""
    return parse_m2c_artifact_ref(ref, domain) is not None


# ---------------------------------------------------------------------------
# Identifier -> canonical location (s3://bucket/key)
# ---------------------------------------------------------------------------

# Key roots inside the skills/ingest bucket that a canonical location may name: session
# artifacts and project assets (``projects/<pid>/...``).
_PROJECTS_KEY_PREFIX = "projects/"


def _own_key_roots() -> tuple[str, ...]:
    prefix = (os.environ.get("ARTIFACTS_PREFIX") or ARTIFACTS_KEY_PREFIX).lstrip("/")
    return tuple(dict.fromkeys((prefix, ARTIFACTS_KEY_PREFIX, _PROJECTS_KEY_PREFIX)))


def _own_bucket_key(key: str) -> Optional[str]:
    """An ``artifacts/...`` or ``projects/...`` key, cleaned; None when it is neither or unsafe."""
    if not isinstance(key, str):
        return None
    key = unquote(key).lstrip("/")
    if ".." in key or "\\" in key or not key or key.endswith("/"):
        return None
    for root in _own_key_roots():
        if key.startswith(root) and len(key) > len(root):
            return key
    return None


# <bucket>.s3[.-<region>|.dualstack|-accelerate...].amazonaws.com — matched FIRST, so a bucket whose
# name starts with "s3-" is not mistaken for a path-style host.
_S3_VHOST_RE = re.compile(r"^(?P<bucket>.+?)\.s3(?:[.-][a-z0-9-]+)*\.amazonaws\.com$")
_S3_PATH_HOST_RE = re.compile(r"^s3(?:[.-][a-z0-9-]+)*\.amazonaws\.com$")


def _s3_host_location(host: str, path: str) -> Optional[tuple[str, str]]:
    """``(bucket, key)`` from an S3 REST host + path (virtual-hosted or path-style), else None."""
    path = unquote(path).lstrip("/")
    m = _S3_VHOST_RE.match(host)
    if m:
        return (m.group("bucket"), path) if path else None
    if _S3_PATH_HOST_RE.match(host):
        bucket, _, key = path.partition("/")
        return (bucket, key) if bucket and key else None
    return None


def _own_location(key: str) -> Optional[str]:
    """``s3://bucket/key`` for an own-bucket KEY.

    An M2C_REFERENCE resolves to the object it names in the M2C proxy bucket (the bytes were
    never copied), everything else to the skills/ingest bucket.
    """
    m2c = _split_m2c_ref_key(key)
    if m2c is not None:
        return f"s3://{m2c[0]}/{m2c[1]}"
    bucket = artifacts_bucket()
    return f"s3://{bucket}/{key}" if bucket else None


def canonical_location(ref: Any, *, domain: Optional[str] = None) -> Optional[str]:
    """The canonical ``s3://bucket/key`` of a media reference, else None.

    The ONE place an agent-side URL becomes a location identity (manifests' ``sourceLocation``,
    the turn-end ``lineage``). Accepts:

    * ``s3://bucket/key`` (any bucket, e.g. the M2C proxy bucket);
    * a bare ``artifacts/...`` / ``projects/...`` key (in the skills/ingest bucket), including
      an ``artifacts/m2c/<bucket>/<key>`` reference, which resolves to ``s3://<bucket>/<key>``;
    * the CloudFront form ``https://<ARTIFACTS_DOMAIN>/<key>`` and the ``/api/artifact?key=``
      proxy form, both naming a key in the skills bucket;
    * a presigned S3 GET of ANY bucket, virtual-hosted or path-style (the query is dropped).

    Anything else (fal.media, other hosts, an unconfigured bucket for a bare key) is None.
    """
    if not isinstance(ref, str):
        return None
    s = ref.strip()
    if not s:
        return None
    if s.lower().startswith("s3://"):
        bucket, _, key = s[5:].split("?", 1)[0].partition("/")
        key = unquote(key).lstrip("/")
        if not bucket or not key or key.endswith("/") or ".." in key:
            return None
        return f"s3://{bucket}/{key}"
    if not s.lower().startswith(("http://", "https://", "/")):
        key = _own_bucket_key(s.split("?", 1)[0])
        return _own_location(key) if key else None
    try:
        parsed = urlparse(s)
    except ValueError:
        return None
    if parsed.path.rstrip("/") == _PROXY_PATH:
        keys = parse_qs(parsed.query).get("key") or []
        key = _own_bucket_key(keys[0]) if keys else None
        return _own_location(key) if key else None
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    s3_loc = _s3_host_location(host, parsed.path)
    if s3_loc is not None:
        b, k = s3_loc
        if ".." in k or k.endswith("/"):
            return None
        return f"s3://{b}/{k}"
    dom = (domain if domain is not None else artifacts_domain()).strip().lower()
    if "://" in dom:
        dom = dom.split("://", 1)[1]
    dom = dom.strip("/").split(":", 1)[0]
    if dom and host == dom:
        key = _own_bucket_key(parsed.path)
        return _own_location(key) if key else None
    return None


# ---------------------------------------------------------------------------
# S3 reads
# ---------------------------------------------------------------------------

def _resolve(url: str, bucket: Optional[str], domain: Optional[str]) -> Optional[tuple[str, str]]:
    """``(bucket, key)`` to read one of our artifact identifiers from, else None.

    An M2C_REFERENCE resolves to the bucket and key IT names — the bytes live in the M2C proxy
    bucket and were never copied — so ``bucket`` (the artifacts-bucket override) does not apply.
    What the runtime may actually read there is bounded by its IAM grant, not by this function.
    """
    key = own_artifact_key(url, domain)
    if key is None:
        return None
    m2c = _split_m2c_ref_key(key)
    if m2c is not None:
        return m2c
    b = bucket if bucket is not None else artifacts_bucket()
    if not b:
        return None
    return b, key


def is_own_artifact(url: str, bucket: Optional[str] = None, domain: Optional[str] = None) -> bool:
    """True when ``url`` is ours AND a bucket is configured to read it from."""
    return _resolve(url, bucket, domain) is not None


def head_artifact(url: str, *, bucket: Optional[str] = None,
                  domain: Optional[str] = None) -> Optional[dict]:
    """``head_object`` for one of our artifact URLs.

    Returns None when ``url`` is not ours (or no bucket is configured) so the caller keeps
    its HTTP path. S3 errors (missing key, access denied) RAISE — the caller decides how a
    missing object is reported, exactly as it did for the HTTP error it replaces.
    """
    r = _resolve(url, bucket, domain)
    if r is None:
        return None
    b, key = r
    return _client().head_object(Bucket=b, Key=key)


def read_artifact_bytes(url: str, byte_range: Optional[str] = None, *,
                        bucket: Optional[str] = None, domain: Optional[str] = None,
                        max_bytes: Optional[int] = None) -> bytes:
    """``get_object`` body for one of our artifact URLs (``byte_range`` like ``bytes=0-99``).

    Raises ``ValueError`` when ``url`` is not ours — callers check :func:`is_own_artifact`
    first and keep HTTP for foreign URLs. S3 errors propagate. ``max_bytes`` reads at most
    that many bytes plus one, so a caller can detect an over-cap body without buffering it.
    """
    r = _resolve(url, bucket, domain)
    if r is None:
        raise ValueError(f"not one of our artifact URLs: {url!r}")
    b, key = r
    params = {"Bucket": b, "Key": key}
    if byte_range:
        params["Range"] = byte_range
    body = _client().get_object(**params)["Body"]
    try:
        return body.read(max_bytes + 1) if max_bytes is not None else body.read()
    finally:
        try:
            body.close()
        except Exception:  # noqa: BLE001
            pass


def presign_artifact_url(url: str, expires_in: int = 3600, *, bucket: Optional[str] = None,
                         domain: Optional[str] = None, method: str = "GET") -> Optional[str]:
    """A presigned S3 URL for one of our artifact URLs, or None (not ours / no bucket / error).

    ``method`` is ``GET`` (default) or ``HEAD``: S3 signs the HTTP method, so a GET-presigned
    URL answers a HEAD with 403. Any other method returns None.

    Presigning is local signing, but a failure (no credentials, bad config) must never break
    the tool call it decorates, so it is logged and reported as None. Note the effective
    lifetime is also bounded by the signing credentials' own expiry.
    """
    op = {"GET": "get_object", "HEAD": "head_object"}.get((method or "GET").upper())
    if op is None:
        return None
    r = _resolve(url, bucket, domain)
    if r is None:
        return None
    b, key = r
    try:
        return _client().generate_presigned_url(
            op, Params={"Bucket": b, "Key": key}, ExpiresIn=int(expires_in))
    except Exception as exc:  # noqa: BLE001
        logger.warning("artifact presign failed for %s: %s", key, exc)
        return None


_PROJECT_ASSET_KEY_RE = re.compile(r"^projects/p[0-9a-f]{32}/[a-z]+:[0-9a-f-]{36}/[^?#]+$")


def _project_asset_key(key: Any) -> Optional[str]:
    """A project ASSET key (``projects/<pid>/<kind>:<uuid>/<file>``), cleaned; None otherwise."""
    if not isinstance(key, str):
        return None
    key = key.lstrip("/")
    if ".." in key or "\\" in key or not _PROJECT_ASSET_KEY_RE.match(key):
        return None
    return key


def trusted_project_asset_key(url: str, project_id: Optional[str], domain: Optional[str] = None) -> Optional[str]:
    """The key of a project ASSET url (CloudFront / proxy / own-bucket S3 form) of ``project_id``, else None.

    Project asset keys are NOT artifact keys (they stay asset-id gated, see :func:`_normalize_key`), so this is
    deliberately separate and only consulted for an OUTBOUND tool input of a turn whose ``project_id`` is the
    TRUSTED one (``jobs.context.trusted_project_id``: verified membership, or forwarded by the dispatcher).
    """
    if not project_id:
        return None
    key = own_artifact_key(url, domain, _norm=_project_asset_key)
    return key if key and key.startswith(f"projects/{project_id}/") else None


# A presigned query string: SigV4 (`X-Amz-Signature=`) or the legacy SigV2 form (`Signature=` + `Expires=`).
_SIGNED_QUERY = re.compile(r"[?&](?:X-Amz-Signature=|X-Amz-Credential=|Signature=[^&]+&.*Expires=|Expires=[^&]+&.*Signature=)")


def shorten_own_presigned_urls(text: str, domain: Optional[str] = None) -> str:
    """Replace every presigned S3 URL of OUR bucket in ``text`` with its short stable identifier.

    A presigned GET is ~1.5 KB (mostly ``X-Amz-Security-Token``). Handed one in a prompt, a model has to copy
    it verbatim through a plan into each node's tool call -- and abbreviates it: session e2fb2692's planner wrote
    ``https://…amazonaws.com/.../xi.webp?...`` and every fal call failed on the literal ``...``. The identifier
    (``https://<ARTIFACTS_DOMAIN>/<key>``) is short, never expires, and the tool wrapper presigns it again at call
    time (:func:`presign_artifact_urls_in`). Keys that are neither artifacts nor project assets are left alone.
    """
    if not isinstance(text, str) or "amazonaws.com" not in text or not _SIGNED_QUERY.search(text):
        return text
    bucket = artifacts_bucket().lower()
    dom = (domain if domain is not None else artifacts_domain()).strip().lower()
    if "://" in dom:
        dom = dom.split("://", 1)[1]
    dom = dom.strip("/")
    if not bucket or not dom:
        return text

    def _sub(m: "re.Match[str]") -> str:
        url = m.group(0)
        if not _SIGNED_QUERY.search(url):
            return url
        key = own_artifact_key(url) or own_artifact_key(url, _norm=_project_asset_key)
        return f"https://{dom}/{quote(key, safe='/:')}" if key else url

    return re.sub(r"https://[^\s\"'<>()\[\]]+", _sub, text)


def presign_for_trusted_turn(url: str, expires_in: int, trusted_project_id: Optional[str],
                             method: str = "GET") -> Optional[str]:
    """Presign one of our artifact URLs, or a project-ASSET url of the turn's trusted project; else None."""
    if own_artifact_key(url) is not None:
        if (method or "GET").upper() == "GET":
            return presign_artifact_url(url, expires_in)
        return presign_artifact_url(url, expires_in, method=method)
    key = trusted_project_asset_key(url, trusted_project_id)
    return _presign_key(key, expires_in, method) if key else None


def _presign_key(key: str, expires_in: int, method: str = "GET") -> Optional[str]:
    """A presigned GET (or HEAD) for ``key`` in the artifacts bucket, or None (logged)."""
    b = artifacts_bucket()
    op = {"GET": "get_object", "HEAD": "head_object"}.get((method or "GET").upper())
    if not b or op is None:
        return None
    try:
        return _client().generate_presigned_url(op, Params={"Bucket": b, "Key": key},
                                                 ExpiresIn=int(expires_in))
    except Exception as exc:  # noqa: BLE001
        logger.warning("project-asset presign failed for %s: %s", key, exc)
        return None


# ---------------------------------------------------------------------------
# Tool-input rewrite (external tools; gated by ARTIFACTS_CDN_PRIVATE at the call site)
# ---------------------------------------------------------------------------

def presign_artifact_urls_in_text(text: str, expires_in: int = 21600, *,
                                  shell_quote: bool = False,
                                  trusted_project_id: Any = ...) -> tuple[str, bool]:
    """Replace every CDN artifact URL that appears INSIDE ``text`` (e.g. code the model hands
    the code-interpreter sandbox, which downloads it over HTTP) with a presigned S3 GET.

    Only the CloudFront form ``https://<ARTIFACTS_DOMAIN>/artifacts/...`` is matched (it is
    unambiguous inside free text); the URL ends at whitespace, a quote, or a closing bracket.
    Returns ``(new_text, changed)``; a URL whose presign fails is left as-is.

    ``shell_quote`` is for shell text (an ``executeCommand`` command, a ``.sh`` file): a
    presigned URL carries ``&`` separators, so an UNQUOTED URL is single-quoted or the shell
    would background the command at the first ``&`` and drop the signature. A URL already
    inside quotes is left in them.
    """
    import re  # noqa: PLC0415

    if not isinstance(text, str):
        return text, False
    domain = artifacts_domain()
    bucket = artifacts_bucket()
    alts = []
    # Either session root: `artifacts/…`, or a projects-session OUTPUT (asset-lineage Phase 3 (a)).
    if trusted_project_id is ...:
        try:
            from jobs.context import trusted_project_id as _tp  # noqa: PLC0415
            trusted_project_id = _tp()
        except Exception:  # noqa: BLE001 — no turn context: artifact URLs only
            trusted_project_id = None
    # A project ASSET of the turn's trusted project too: prompts now carry its short identifier (a cell that
    # downloads it would otherwise 403 on the private CDN).
    asset_root = (r"|projects/" + re.escape(trusted_project_id) + r"/[a-z]+:[0-9a-f-]{36}/"
                  if isinstance(trusted_project_id, str) and trusted_project_id else "")
    roots = (r"(?:artifacts/|projects/p[0-9a-f]{32}/sessions/[A-Za-z0-9][A-Za-z0-9._-]{7,127}/output/"
             + asset_root + ")")
    if domain and domain in text:
        alts.append(r"https://" + re.escape(domain) + r"/" + roots)
    # Our own bucket's presigned GETs (e.g. a "Copy source URL" link pasted into chat, possibly
    # already expired): re-sign them fresh so the sandbox download works.
    if bucket and bucket in text:
        alts.append(r"https://" + re.escape(bucket) + r"\.s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com/" + roots)
    if not alts:
        return text, False
    pattern = re.compile(r"(?:" + "|".join(alts) + r")[^\s'\"<>()\[\]{}`\\]+")
    changed = False

    def _sub(m: "re.Match[str]") -> str:
        nonlocal changed
        if _is_url_fragment(m.group(0), text, m.end()):
            return m.group(0)
        signed = presign_for_trusted_turn(m.group(0), expires_in, trusted_project_id)
        if not signed:
            return m.group(0)
        changed = True
        if shell_quote:
            before = text[m.start() - 1] if m.start() > 0 else ""
            if before not in ("'", '"'):
                return "'" + signed + "'"
        return signed

    return pattern.sub(_sub, text), changed


def _is_url_fragment(url: str, text: str, end: int) -> bool:
    """True when ``url`` (matched in ``text`` ending at ``end``) is only the START of a URL the code builds.

    Prod session d52d544e: a cell built each panel's address as ``BASE + name`` / ``f"…/external/{name}.jpg"``.
    The match stopped at the quote or ``{``, so the FOLDER ``artifacts/external/`` was signed and the name was
    appended after the signature -- every image in the picture-book page was broken. A prefix is never an object:
    leave a fragment alone (signing it can only break it) -- one that ends in ``/`` (a folder), or that continues
    into a template or concatenation (``{…}``, ``${…}``, ``%s``, ``" + name``).
    """
    import re  # noqa: PLC0415

    if url.split("?", 1)[0].endswith("/"):
        return True
    rest = text[end:end + 8]
    return bool(re.match(r"""(?:\{|\$\{|%[sd(]|["']\s*(?:\+|\.\s*format\b|%))""", rest))


def _is_url_form(s: str) -> bool:
    """Only URL forms are rewritten — a bare ``artifacts/...`` string in a tool input is
    most likely a KEY parameter, and turning it into a URL could break the tool."""
    low = s.lower()
    return low.startswith(("https://", "http://", _PROXY_PATH))


def map_strings(value: Any, fn) -> tuple[Any, bool]:
    """Deep-walk ``value`` and replace each string ``s`` with ``fn(s)`` (``None`` = keep). Returns
    ``(new_value, changed)``. The input is never mutated: containers along a changed path are copied,
    everything else is shared."""
    if isinstance(value, str):
        nv = fn(value)
        return (nv, True) if nv is not None and nv != value else (value, False)
    if isinstance(value, dict):
        out: dict = {}
        changed = False
        for k, v in value.items():
            nv, c = map_strings(v, fn)
            out[k] = nv
            changed = changed or c
        return (out, True) if changed else (value, False)
    if isinstance(value, (list, tuple)):
        items = []
        changed = False
        for v in value:
            nv, c = map_strings(v, fn)
            items.append(nv)
            changed = changed or c
        if not changed:
            return value, False
        return (type(value)(items) if isinstance(value, tuple) else items), True
    return value, False


def presign_artifact_urls_in(value: Any, expires_in: int = 21600,
                             trusted_project_id: Optional[str] = None) -> tuple[Any, bool]:
    """Deep-walk ``value``; replace each string that IS one of our artifact URLs with a
    presigned S3 GET. Returns ``(new_value, changed)``.

    The input is never mutated. Non-matching strings are untouched, and a string whose presign fails
    keeps its original value (with a warning) so the external tool still sees the URL.
    """
    def one(value: str) -> Optional[str]:
        if not _is_url_form(value):
            return None
        if own_artifact_key(value) is None:
            asset_key = trusted_project_asset_key(value, trusted_project_id)
            return _presign_key(asset_key, expires_in) if asset_key is not None else None
        signed = presign_artifact_url(value, expires_in)
        if not signed:
            logger.warning("tool-input artifact presign unavailable; passing the original URL "
                           "(%s)", value.split("?", 1)[0])
        return signed

    return map_strings(value, one)


# Copy -> origin, per object key: a copy never changes where it came from (the importer writes it once).
_ORIGIN_CACHE: dict[str, Optional[str]] = {}


def _origin_of(bucket: str, key: str) -> Optional[str]:
    """The ``s3://`` original an imported copy names in its ``origin`` metadata, if it still exists."""
    ck = f"{bucket}/{key}"
    if ck in _ORIGIN_CACHE:
        return _ORIGIN_CACHE[ck]
    origin = None
    try:
        meta = _client().head_object(Bucket=bucket, Key=key).get("Metadata") or {}
        cand = str(meta.get("origin") or "")
        if cand.startswith("s3://") and "/" in cand[5:]:
            ob, ok = cand[5:].split("/", 1)
            try:
                _client().head_object(Bucket=ob, Key=ok)
                origin = cand
            except Exception as exc:  # noqa: BLE001
                code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
                # Gone (the producer cleaned its scratch prefix up): keep the copy. Not ours to read (403): the
                # producer's tools read it with their own role, so the original still stands.
                origin = None if code in ("404", "NoSuchKey", "NotFound") else cand
    except Exception:  # noqa: BLE001 — no copy there, or unreadable: nothing to swap
        origin = None
    _ORIGIN_CACHE[ck] = origin
    return origin


def origin_refs_in(value: Any) -> tuple[Any, bool]:
    """Deep-walk a tool's input and replace each ``s3://`` URI of OUR copy of another system's output with that
    output's original (the copy's ``origin`` metadata, written by result_resolution.make_s3_importer).

    Our copies exist for durable links and display; a tool of the producer reads its own bucket (M2C: "must be on
    the proxy bucket"), so it must be handed what it returned. Only ``s3://`` identities are swapped — an https link
    is for fetching, and an external tool must keep being able to fetch it. Anything else is left as is.
    """
    bucket = artifacts_bucket()

    def one(value: str) -> Optional[str]:
        if not bucket or not value.startswith(f"s3://{bucket}/"):
            return None
        return _origin_of(bucket, value[len(f"s3://{bucket}/"):])

    return map_strings(value, one)
