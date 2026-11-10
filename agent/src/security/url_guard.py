"""The ONE outbound-fetch guard for model- or user-supplied URLs (SSRF defence).

Every server-side fetch of a URL that a model or a user chose goes through here:
``read_result_files``, ``view_images``, ``media_probe``, ``caption_parser``, peak
extraction, the Render Agent's sidecar / container reads, ``artifact_import``, and the
builtin ``http_request`` tool (``tools/http_request.py``, which sends every request through :func:`fetch`).
User MCP endpoint registration uses :func:`validate_url` (the gateway connects, not us).

What it enforces
----------------
* **Scheme** — ``https`` only. ``allow_http=True`` exists for the general-purpose
  ``http_request`` tool alone; the IP rules below apply to it exactly the same.
* **Host shape** — a hostname of letters/digits/``-``/``.``/``_``, or a CANONICAL IP
  literal. Non-canonical numeric forms that ``inet_aton`` would still accept
  (``2130706433``, ``0177.0.0.1``, ``0x7f000001``, ``127.1``) are refused outright,
  as are ``localhost`` / ``*.localhost``.
* **Address** — the host is resolved and EVERY address must be globally routable:
  no loopback, RFC1918, link-local (incl. the EC2/ECS/AgentCore metadata endpoints
  169.254.169.254 and 169.254.170.2, and ``fd00:ec2::254``), CGNAT 100.64/10, ULA
  fc00::/7, unspecified, multicast, reserved or documentation space. IPv4 embedded in
  IPv6 (``::ffff:a.b.c.d``, NAT64 ``64:ff9b::/96``, 6to4 ``2002::/16``) is judged by
  its IPv4 address.
* **Pinning** — the TCP connection goes to the address that was vetted, not to a
  second DNS answer (DNS rebinding). :class:`GuardedAdapter` swaps the pool's host
  for the vetted IP while keeping TLS SNI + certificate verification and the
  ``Host`` header on the original hostname, so a public certificate still validates.
* **Redirects** — every hop is a fresh ``adapter.send``, so every hop is re-validated
  and re-pinned; the number of hops is capped (:data:`MAX_REDIRECTS`).
* **Bounds** — :func:`fetch` streams with a byte cap and a wall-clock deadline.

No internal endpoint is allowlisted: every runtime uses AgentCore's PUBLIC network
mode, so our own presigned S3 / CloudFront URLs resolve to public AWS addresses and
pass on their merits. Our own artifact URLs are mostly read by key through S3
(``media.artifact_fetch``) before any HTTP is attempted anyway.

Tests inject a fake resolver with :func:`set_resolver`; nothing here needs network.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Optional
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter

#: Default redirect-hop cap for every guarded session.
MAX_REDIRECTS = 5
#: Default per-socket-operation timeout (connect / read), seconds.
DEFAULT_TIMEOUT_S = 10.0

# Addresses called out explicitly (they are also non-global, so the generic rule
# catches them too): the instance / task / AgentCore credential endpoints.
_METADATA_ADDRESSES = frozenset({
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("169.254.170.2"),
    ipaddress.ip_address("169.254.170.23"),
    ipaddress.ip_address("fd00:ec2::254"),
    ipaddress.ip_address("fd00:ec2::23"),
})
_EXTRA_BLOCKED_NETS = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8",        # "this network"
    "100.64.0.0/10",    # CGNAT (not is_private, but never a public server)
    "192.0.0.0/24",     # IETF protocol assignments
    "198.18.0.0/15",    # benchmarking
    "240.0.0.0/4",      # reserved (incl. broadcast)
    "fc00::/7",         # ULA
    "fe80::/10",        # link-local
    "fec0::/10",        # deprecated site-local
))
_NAT64 = ipaddress.ip_network("64:ff9b::/96")

# Hostname labels: letters, digits, hyphen, underscore (some CDNs), dots between.
_HOSTNAME_RE = re.compile(r"^(?=.{1,253}\.?$)[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,62})"
                          r"(?:\.[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,62}))*\.?$")
# Anything made only of digits, dots and hex/octal markers is an IP literal attempt.
_NUMERIC_HOST_RE = re.compile(r"^(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]*|[0-9]*))*\.?$", re.I)


_PRIVATE_SUFFIXES = (".localhost", ".internal", ".local", ".localdomain", ".home.arpa", ".lan",
                     ".intranet", ".corp")


class BlockedURLError(ValueError):
    """The URL may not be fetched. The message is safe to show a model or a user."""


Resolver = Callable[[str, int], list]


def _system_resolver(host: str, port: int) -> list:
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    return [info[4][0] for info in infos]


_resolver: Resolver = _system_resolver


def set_resolver(resolver: Optional[Resolver]) -> Resolver:
    """Install ``resolver(host, port) -> [ip, ...]`` (``None`` restores DNS). Returns the previous one."""
    global _resolver
    previous = _resolver
    _resolver = resolver or _system_resolver
    return previous


def _embedded_v4(ip: ipaddress.IPv6Address) -> Optional[ipaddress.IPv4Address]:
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]
    return None


def is_blocked_ip(ip) -> bool:
    """True when ``ip`` (str or ipaddress object) is not a public unicast address."""
    try:
        addr = ipaddress.ip_address(str(ip).split("%", 1)[0]) if not isinstance(
            ip, (ipaddress.IPv4Address, ipaddress.IPv6Address)) else ip
    except ValueError:
        return True
    if isinstance(addr, ipaddress.IPv6Address):
        inner = _embedded_v4(addr)
        if inner is not None and is_blocked_ip(inner):
            return True
    if addr in _METADATA_ADDRESSES:
        return True
    if any(addr in net for net in _EXTRA_BLOCKED_NETS if net.version == addr.version):
        return True
    return (not addr.is_global or addr.is_multicast or addr.is_reserved
            or addr.is_unspecified or addr.is_loopback or addr.is_link_local
            or addr.is_private)


@dataclass(frozen=True)
class Target:
    """A vetted fetch target: connect to ``ip``; present ``hostname`` for TLS and Host."""
    url: str
    scheme: str
    hostname: str
    port: int
    ip: str
    host_header: str
    addresses: tuple = field(default_factory=tuple)


def _parse(url: str, allow_http: bool):
    if not isinstance(url, str) or not url.strip():
        raise BlockedURLError("URL is required")
    url = url.strip()
    if any(c in url for c in "\r\n\t ") or "\\" in url:
        raise BlockedURLError("URL contains whitespace or backslashes")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise BlockedURLError(f"URL is not valid: {exc}") from None
    scheme = (parts.scheme or "").lower()
    allowed = ("https", "http") if allow_http else ("https",)
    if scheme not in allowed:
        raise BlockedURLError("URL must use https" if not allow_http else "URL must use http or https")
    host = parts.hostname or ""
    if not host:
        raise BlockedURLError("URL must include a host")
    return url, scheme, host, port or (443 if scheme == "https" else 80)


def _literal_ip(host: str):
    """``ipaddress`` object for a canonical literal; raise on a non-canonical numeric host."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    if ":" in host:
        raise BlockedURLError("URL host is not a valid address")
    if _NUMERIC_HOST_RE.match(host):
        # 2130706433, 0177.0.0.1, 0x7f000001, 127.1 — all reach loopback via inet_aton.
        raise BlockedURLError("URL host is a non-canonical numeric address")
    return None


def validate_url(url: str, *, allow_http: bool = False, resolve: bool = True) -> Target:
    """Vet ``url`` and return the :class:`Target` to connect to. Raises :class:`BlockedURLError`."""
    url, scheme, host, port = _parse(url, allow_http)
    host_l = host.lower().rstrip(".")
    literal = _literal_ip(host_l)
    if literal is None:
        if not _HOSTNAME_RE.match(host_l) or "." not in host_l:
            raise BlockedURLError("URL host is not a public hostname")
        if host_l == "localhost" or host_l.endswith(_PRIVATE_SUFFIXES):
            # Names that only ever mean something inside a private network (AWS VPC
            # ``*.internal``, mDNS ``*.local``) — refused before any DNS is consulted.
            raise BlockedURLError("URL must point at a publicly reachable host")
    if literal is not None:
        addresses = [str(literal)]
    elif not resolve:
        addresses = []
    else:
        try:
            addresses = [str(a) for a in (_resolver(host_l, port) or [])]
        except (OSError, UnicodeError) as exc:
            raise BlockedURLError(f"URL host does not resolve: {exc}") from None
        if not addresses:
            raise BlockedURLError("URL host does not resolve")
    for addr in addresses:
        if is_blocked_ip(addr):
            raise BlockedURLError(
                "URL must point at a publicly reachable host "
                "(private, loopback, link-local and metadata addresses are not allowed)")
    netloc_host = f"[{host_l}]" if ":" in host_l else host_l
    default_port = 443 if scheme == "https" else 80
    host_header = netloc_host if port == default_port else f"{netloc_host}:{port}"
    return Target(url=url, scheme=scheme, hostname=host_l, port=port,
                  ip=addresses[0] if addresses else "", host_header=host_header,
                  addresses=tuple(addresses))


class GuardedAdapter(HTTPAdapter):
    """``HTTPAdapter`` that vets EVERY request it sends and pins the socket to the vetted IP.

    Mounted for both ``http://`` and ``https://`` so nothing on the session can bypass it;
    a plain-http request is refused unless the adapter was built with ``allow_http``.
    Proxies are dropped: a proxied connection would resolve the host itself.
    """

    def __init__(self, *args, allow_http: bool = False, **kwargs):
        self._allow_http = allow_http
        super().__init__(*args, **kwargs)

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        target = validate_url(request.url, allow_http=self._allow_http)
        # Pin: the pool connects to target.ip; Host/SNI stay the hostname. Overwritten on
        # every hop, so a redirect cannot carry a stale (or a model-chosen) Host.
        request.headers["Host"] = target.host_header
        request._url_guard_target = target  # read by build_connection_pool_key_attributes
        return super().send(request, stream=stream, timeout=timeout, verify=verify,
                            cert=cert, proxies={})

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        host_params, pool_kwargs = super().build_connection_pool_key_attributes(request, verify, cert)
        target: Optional[Target] = getattr(request, "_url_guard_target", None)
        if target is None:  # never sent through send() — refuse rather than connect unpinned
            raise BlockedURLError("request was not vetted")
        host_params = {**host_params, "host": target.ip, "port": target.port}
        if target.scheme == "https":
            pool_kwargs = {**pool_kwargs, "server_hostname": target.hostname,
                           "assert_hostname": target.hostname}
        return host_params, pool_kwargs


def mount(session: requests.Session, *, allow_http: bool = False,
          max_redirects: int = MAX_REDIRECTS, **adapter_kwargs) -> requests.Session:
    """Mount :class:`GuardedAdapter` on ``session`` for every scheme; returns the session."""
    adapter = GuardedAdapter(allow_http=allow_http, **adapter_kwargs)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.max_redirects = max_redirects
    session.trust_env = False  # no env proxies / netrc: the connection must go where we vetted
    return session


def session(*, allow_http: bool = False, max_redirects: int = MAX_REDIRECTS,
            **adapter_kwargs) -> requests.Session:
    """A fresh ``requests.Session`` with the guard mounted."""
    return mount(requests.Session(), allow_http=allow_http, max_redirects=max_redirects, **adapter_kwargs)


def request(method: str, url: str, *, timeout: float = DEFAULT_TIMEOUT_S,
            headers: Optional[Mapping[str, str]] = None, data=None, stream: bool = False,
            allow_redirects: bool = True, max_redirects: int = MAX_REDIRECTS,
            allow_http: bool = False, max_retries=None) -> requests.Response:
    """Guarded ``requests.request``. Raises :class:`BlockedURLError` (a ``ValueError``) when refused.

    ``max_retries`` (an int or a urllib3 ``Retry``) goes to the adapter; a retry re-sends on the
    already-pinned connection pool, so it never resolves the host again.
    """
    # No separate pre-check: GuardedAdapter.send vets each hop exactly once and pins THAT
    # answer, so a second lookup can never disagree with the address actually connected to.
    adapter_kwargs = {} if max_retries is None else {"max_retries": max_retries}
    s = session(allow_http=allow_http, max_redirects=max_redirects, **adapter_kwargs)
    try:
        resp = s.request(method, url, timeout=timeout, headers=dict(headers or {}), data=data,
                         stream=stream, allow_redirects=allow_redirects)
    except BaseException:
        s.close()
        raise
    if not stream:
        s.close()  # a streamed body still needs its pooled connection; GC closes that session
    return resp


def get(url: str, *, timeout: float = DEFAULT_TIMEOUT_S, headers: Optional[Mapping[str, str]] = None,
        stream: bool = False, allow_redirects: bool = True, max_redirects: int = MAX_REDIRECTS,
        allow_http: bool = False, max_retries=None) -> requests.Response:
    """Guarded ``requests.get``. Raises :class:`BlockedURLError` (a ``ValueError``) when refused."""
    return request("GET", url, timeout=timeout, headers=headers, stream=stream,
                   allow_redirects=allow_redirects, max_redirects=max_redirects, allow_http=allow_http,
                   max_retries=max_retries)


class ResponseTooLarge(ValueError):
    """The body ran past the caller's byte cap."""


@dataclass
class FetchResult:
    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str
    truncated: bool = False
    #: Status codes of the redirect hops followed before this response, in order.
    history: tuple = ()

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400

    def text(self, encoding: str = "utf-8") -> str:
        return self.body.decode(encoding, "replace")


def fetch(url: str, *, max_bytes: int, timeout: float = DEFAULT_TIMEOUT_S,
          deadline_s: Optional[float] = None, headers: Optional[Mapping[str, str]] = None,
          truncate: bool = False, raise_for_status: bool = True,
          max_redirects: int = MAX_REDIRECTS, allow_http: bool = False,
          method: str = "GET", data=None, allow_redirects: bool = True,
          max_retries=None) -> FetchResult:
    """Send ``method`` (default GET) to ``url`` through the guard, reading at most ``max_bytes``.

    ``truncate=True`` returns the first ``max_bytes`` with ``truncated=True``; otherwise an
    over-cap body (or an over-cap ``Content-Length``) raises :class:`ResponseTooLarge`.
    ``deadline_s`` bounds the whole transfer (default: 3x ``timeout``). ``data`` is the request
    body; ``allow_redirects=False`` returns a 3xx as is (every hop that IS followed is vetted).
    """
    deadline = time.monotonic() + (deadline_s if deadline_s is not None else 3 * timeout)
    kwargs = {"timeout": timeout, "headers": headers, "stream": True, "allow_redirects": allow_redirects,
              "max_redirects": max_redirects, "allow_http": allow_http}
    if max_retries is not None:
        kwargs["max_retries"] = max_retries
    if method.upper() == "GET" and data is None:
        resp = get(url, **kwargs)  # a plain GET keeps going through get(), the seam callers' tests fake
    else:
        resp = request(method, url, data=data, **kwargs)
    try:
        if raise_for_status:
            resp.raise_for_status()
        clen = (resp.headers.get("Content-Length") or "").strip()
        if not truncate and clen.isdigit() and int(clen) > max_bytes:
            raise ResponseTooLarge(f"response is {int(clen)} bytes, over the {max_bytes}-byte cap")
        chunks: list[bytes] = []
        size = 0
        truncated = False
        for chunk in resp.iter_content(chunk_size=65536):
            if time.monotonic() > deadline:
                raise TimeoutError("fetch exceeded its time budget")
            if not chunk:
                continue
            chunks.append(chunk)
            size += len(chunk)
            if size > max_bytes:
                if not truncate:
                    raise ResponseTooLarge(f"response exceeds the {max_bytes}-byte cap")
                truncated = True
                break
        body = b"".join(chunks)[:max_bytes]
        return FetchResult(status=resp.status_code, headers=dict(resp.headers), body=body,
                           url=resp.url, truncated=truncated,
                           history=tuple(r.status_code for r in getattr(resp, "history", None) or ()))
    finally:
        resp.close()


__all__: Iterable[str] = (
    "BlockedURLError", "FetchResult", "GuardedAdapter", "MAX_REDIRECTS", "ResponseTooLarge",
    "Target", "fetch", "get", "is_blocked_ip", "mount", "request", "session", "set_resolver",
    "validate_url",
)
