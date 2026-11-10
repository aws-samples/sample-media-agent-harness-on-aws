"""``http_request``: the builtin HTTP client tool, on the shared SSRF guard.

Replaces ``strands_tools.http_request`` (strands-agents-tools is no longer a dependency). The model
sees the same tool name and the same arguments it used, minus the ones that reached the RUNTIME'S
own credentials or filesystem:

* ``auth_type`` ``aws_sig_v4`` / ``aws_auth`` signed the request with the runtime role (every user's
  data, the delegation key) -- refused, never signed;
* ``basic`` / ``digest`` (``basic_auth`` / ``digest_auth``) -- refused (a caller who must send Basic
  credentials it was given can still put an ``Authorization`` header in ``headers``);
* ``auth_env_var`` read a token out of ``os.environ`` (where ``RuntimeConfigSecret`` lands) -- refused;
* ``cookie`` / ``cookie_jar`` read and wrote files on the runtime host -- refused;
* ``verify_ssl: false`` -- refused (it already was, absent an env var no env sets).

What stays: GET/HEAD/POST/PUT/PATCH/DELETE, ``headers``, ``body``, ``auth_token`` with ``Bearer`` /
``token`` / ``custom`` / ``api_key`` (the model's OWN token, as before), ``jwt`` (signed with the
model-supplied secret), redirects, ``timeout``, ``metrics`` and ``convert_to_markdown``, and the result
text the model and ``media.turn_outputs`` read (``Status Code:`` / ``Redirects:`` / ``Headers:`` /
``Body:`` / ``Metrics:`` blocks).

Every request goes through :func:`security.url_guard.fetch`: each hop (redirects included) is
vetted -- public addresses only -- and the socket pinned to the vetted address; at most
:data:`url_guard.MAX_REDIRECTS` hops, :data:`MAX_RESPONSE_BYTES` of body (truncated, and said so) and a
wall-clock deadline. ``tool_input_normalize.wrap_http_request`` adds the private-CDN presign and a
pre-check that refuses a blocked URL before any socket.
"""
from __future__ import annotations

import datetime
import json
import logging
import time
from typing import Any, Optional

from strands import tool

logger = logging.getLogger(__name__)

TOOL_NAME = "http_request"
#: Body bytes kept; the rest is dropped and the result says so.
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
DEFAULT_TIMEOUT_S = 30.0
#: Per-socket-operation ceiling for a model-chosen ``timeout``; the whole transfer gets 3x, capped.
MAX_TIMEOUT_S = 300.0
MAX_DEADLINE_S = 600.0
METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")
#: ``auth_type`` values that are the model's own token in a header (unchanged from the old tool).
TOKEN_AUTH_TYPES = ("Bearer", "token", "custom", "api_key")
#: Never: they signed with the runtime's credentials or sent stored credentials.
REFUSED_AUTH_TYPES = frozenset({"aws_sig_v4", "basic", "digest"})
#: Arguments that reached the runtime's credentials, environment or filesystem.
REFUSED_ARGUMENTS = {
    "aws_auth": "http_request never signs with the runtime's AWS credentials",
    "basic_auth": "send an Authorization header in `headers` instead",
    "digest_auth": "digest authentication is not supported",
    "auth_env_var": "tokens are never read from the runtime's environment",
    "cookie": "cookie files on the runtime host are not readable",
    "cookie_jar": "cookie files on the runtime host are not writable",
}

DESCRIPTION = (
    "Make HTTP requests (GET, HEAD, POST, PUT, PATCH, DELETE) to any public URL, with optional token "
    "authentication you supply (Bearer, token, custom Authorization value, X-API-Key, or a JWT signed "
    "with a secret you provide), custom headers and body, redirect control, request metrics, and optional "
    "HTML to markdown conversion. It never signs requests with this runtime's own AWS credentials and "
    "cannot reach private or internal addresses."
)

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "method": {"type": "string", "description": "HTTP method (GET, HEAD, POST, PUT, PATCH, DELETE)"},
        "url": {"type": "string", "description": "The URL to send the request to"},
        "auth_type": {
            "type": "string",
            "enum": ["Bearer", "token", "jwt", "custom", "api_key"],
            "description": "Authentication type to use with auth_token (or jwt_config for jwt)",
        },
        "auth_token": {"type": "string", "description": "Authentication token"},
        "headers": {"type": "object", "description": "HTTP headers as key-value pairs"},
        "body": {"type": "string", "description": "Request body (for POST, PUT, etc.)"},
        "metrics": {"type": "boolean", "description": "Whether to collect request metrics"},
        "streaming": {"type": "boolean", "description": "Enable streaming response handling"},
        "allow_redirects": {"type": "boolean", "description": "Whether to follow redirects (default: True)"},
        "max_redirects": {
            "type": "integer",
            "description": "Maximum number of redirects to follow (default and ceiling: 5)",
        },
        "timeout": {"type": "number", "description": "Request timeout in seconds (default: 30, at most 300)."},
        "convert_to_markdown": {
            "type": "boolean",
            "description": "Convert HTML responses to markdown format (default: False).",
        },
        "jwt_config": {
            "type": "object",
            "description": "JWT configuration",
            "properties": {
                "secret": {"type": "string"},
                "algorithm": {"type": "string"},
                "expiry": {"type": "integer"},
            },
        },
    },
    "required": ["method", "url"],
}

_IMPORTANT_HEADERS = frozenset(h.lower() for h in ("Content-Type", "Content-Length", "Date", "Server",
                                                   "Payment-Required"))


class RequestRefused(ValueError):
    """The input asks for something this tool deliberately does not do."""


def _refusal(args: dict) -> Optional[str]:
    auth_type = args.get("auth_type")
    if isinstance(auth_type, str) and auth_type.strip().lower() in REFUSED_AUTH_TYPES:
        return (f"auth_type '{auth_type}' is not supported: http_request never signs with the runtime's "
                "credentials or sends stored ones")
    for name, why in REFUSED_ARGUMENTS.items():
        if args.get(name) not in (None, "", {}):
            return f"`{name}` is not supported: {why}"
    if args.get("verify_ssl") is False:
        return "SSL verification cannot be disabled"
    return None


def _auth_headers(headers: dict, args: dict) -> dict:
    """The old tool's token handling, for the model's OWN token only."""
    token = args.get("auth_token")
    auth_type = args.get("auth_type")
    if auth_type == "jwt":
        headers.update(_jwt_header(args.get("jwt_config") or {}))
        return headers
    if not token:
        return headers
    if auth_type == "Bearer":
        headers["Authorization"] = f"Bearer {token}"
    elif auth_type == "token":
        headers["Authorization"] = f"token {token}"
        if "Accept" not in headers and "github" in str(args.get("url") or "").lower():
            headers["Accept"] = "application/vnd.github.v3+json"
    elif auth_type == "custom":
        headers["Authorization"] = str(token)
    elif auth_type == "api_key":
        headers["X-API-Key"] = str(token)
    return headers


def _jwt_header(config: dict) -> dict:
    import jwt  # noqa: PLC0415 - PyJWT, only when asked for

    for key in ("secret", "algorithm", "expiry"):
        if key not in config:
            raise RequestRefused(f"jwt_config.{key} is required for jwt authentication")
    expiry = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=int(config["expiry"]))
    token = jwt.encode({"exp": expiry}, config["secret"], algorithm=config["algorithm"])
    return {"Authorization": f"Bearer {token.decode('utf-8') if hasattr(token, 'decode') else token}"}


def _timeout(value: Any) -> float:
    if value in (None, ""):
        return DEFAULT_TIMEOUT_S
    try:
        t = float(value)
    except (TypeError, ValueError) as e:
        raise RequestRefused(f"timeout must be a number of seconds, got {value!r}") from e
    return min(max(t, 1.0), MAX_TIMEOUT_S)


def _decode(body: bytes, headers: dict) -> str:
    from requests.utils import get_encoding_from_headers  # noqa: PLC0415

    encoding = get_encoding_from_headers({k.lower(): v for k, v in headers.items()}) or "utf-8"
    try:
        return body.decode(encoding, "replace")
    except LookupError:
        return body.decode("utf-8", "replace")


def _to_markdown(content: str, headers: dict) -> str:
    content_type = next((v for k, v in headers.items() if k.lower() == "content-type"), "") or ""
    head = content[:100].lower()
    if "text/html" not in content_type.lower() and "<html" not in head and "<!doctype html" not in head:
        return content
    try:
        import markdownify  # noqa: PLC0415

        return markdownify.markdownify(content, heading_style=markdownify.ATX)
    except Exception:  # noqa: BLE001 - as before: conversion failure returns the original HTML
        return content


def _retries():
    from urllib3 import Retry  # noqa: PLC0415

    # The old tool's session retry policy; raise_on_status=False so a final 5xx is REPORTED, not raised.
    return Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504], raise_on_status=False)


def send(args: dict, tool_use_id: str = "") -> dict:
    """Run one ``http_request`` input (the model's arguments) and return the tool result. Never raises."""
    from security import url_guard  # noqa: PLC0415

    try:
        refusal = _refusal(args)
        if refusal:
            raise RequestRefused(refusal)
        url = args.get("url")
        if not isinstance(url, str) or not url.strip():
            raise RequestRefused("url is required")
        method = str(args.get("method") or "GET").strip().upper()
        if method not in METHODS:
            raise RequestRefused(f"method {method!r} is not supported (use one of {', '.join(METHODS)})")
        raw_headers = args.get("headers") or {}
        if not isinstance(raw_headers, dict):
            raise RequestRefused("headers must be an object of name/value pairs")
        headers = _auth_headers({str(k): str(v) for k, v in raw_headers.items()}, args)
        body = args.get("body")
        data = None
        if body not in (None, ""):
            data = body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8")
        timeout = _timeout(args.get("timeout"))
        max_redirects = url_guard.MAX_REDIRECTS
        if args.get("max_redirects") is not None:
            try:
                max_redirects = max(0, min(int(args["max_redirects"]), url_guard.MAX_REDIRECTS))
            except (TypeError, ValueError) as e:
                raise RequestRefused("max_redirects must be an integer") from e
        allow_redirects = args.get("allow_redirects")
        allow_redirects = True if allow_redirects is None else bool(allow_redirects)

        start = time.time()
        res = url_guard.fetch(
            url, method=method, data=data, headers=headers, max_bytes=MAX_RESPONSE_BYTES, truncate=True,
            timeout=timeout, deadline_s=min(3 * timeout, MAX_DEADLINE_S), raise_for_status=False,
            allow_redirects=allow_redirects, max_redirects=max_redirects, allow_http=True,
            max_retries=_retries())

        content = _decode(res.body, res.headers)
        if args.get("convert_to_markdown"):
            content = _to_markdown(content, res.headers)

        lines = [f"Status Code: {res.status}"]
        if res.history:
            chain = " -> ".join([str(s) for s in res.history] + [str(res.status)])
            lines.append(f"Redirects: {len(res.history)} redirects followed ({chain})")
        important = {k: v for k, v in res.headers.items() if k.lower() in _IMPORTANT_HEADERS}
        lines.append(f"Headers: {important}")
        lines.append(f"Body: {content}")
        if res.truncated:
            lines.append(f"Note: the body was cut at {MAX_RESPONSE_BYTES} bytes; the response is larger.")
        if args.get("metrics"):
            measured = {"duration": round(time.time() - start, 3), "status_code": res.status,
                        "bytes_sent": len(data or b""), "bytes_received": len(res.body),
                        "timestamp": datetime.datetime.now().isoformat()}
            lines.append(f"Metrics: {measured}")
        return {"toolUseId": tool_use_id, "status": "success", "content": [{"text": t} for t in lines]}
    except Exception as e:  # noqa: BLE001 - the model gets the reason, never a traceback
        if not isinstance(e, (RequestRefused, url_guard.BlockedURLError)):
            logger.info("http_request failed: %s: %s", type(e).__name__, e)
        return {"toolUseId": tool_use_id, "status": "error", "content": [{"text": f"Error: {e}"}]}


@tool(name=TOOL_NAME, description=DESCRIPTION, inputSchema={"json": INPUT_SCHEMA})
def http_request(
    method: Any = None,
    url: Any = None,
    auth_type: Any = None,
    auth_token: Any = None,
    headers: Any = None,
    body: Any = None,
    metrics: Any = None,
    streaming: Any = None,
    allow_redirects: Any = None,
    max_redirects: Any = None,
    timeout: Any = None,
    convert_to_markdown: Any = None,
    jwt_config: Any = None,
    # Not in the schema, but a model that remembers the old tool may still send them: declared so
    # they are SEEN (and refused) rather than silently dropped by input validation.
    aws_auth: Any = None,
    basic_auth: Any = None,
    digest_auth: Any = None,
    auth_env_var: Any = None,
    cookie: Any = None,
    cookie_jar: Any = None,
    verify_ssl: Any = None,
    session_config: Any = None,
) -> dict:
    """Make an HTTP request. See the module docstring."""
    args = {k: v for k, v in locals().items() if v is not None}
    return send(args)


__all__ = ["DESCRIPTION", "INPUT_SCHEMA", "MAX_RESPONSE_BYTES", "TOOL_NAME", "http_request", "send"]
