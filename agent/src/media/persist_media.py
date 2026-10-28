"""Copy externally-hosted media into our own bucket so it stops being borrowed.

Why
---
A user-registered MCP server (fal.ai, say) returns a URL on its own CDN:

    https://v3b.fal.media/files/b/0aa50f49/8c4ABgOJ45LpIlkl7V6pa.jpg

That renders nowhere and outlives nothing. Three concrete consequences, in the
order they bite:

1. **The follow-up edit breaks.** "Change the cat to a dog, keep the background"
   requires sending that exact image back as an input. If the host has expired
   the URL by then, the edit is impossible -- not merely unrendered.

2. **Downstream media tools can't touch it.** Trim, transcode, frame extraction
   and face recognition all take ``s3://`` in the skills-store bucket. An asset
   that never lands there is a dead end for every one of them, and for the NLE
   timeline.

3. **Render history rots.** Entries are persisted per session, so a foreign URL
   leaves a conversation that looks intact and displays nothing.

Copying it in fixes all three at once and needs no UI change: the renderer's
templates already key on the CloudFront domain.

What this deliberately does NOT do
----------------------------------
It does not fetch arbitrary URLs. The guards below exist because this downloads
from a host named in a *tool result* -- which is to say, content influenced by a
third party. Scheme, host, size and content-type are all checked, and the URL
must carry a recognised media extension before we go near it.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import re
import socket
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

# Ceiling on a single copied object. Generated images are ~1 MB and short videos
# tens of MB; anything beyond this is not something a chat turn should be
# streaming through the runtime's memory.
MAX_BYTES = 200 * 1024 * 1024
FETCH_TIMEOUT_S = 30.0

# Extension -> content type. Intentionally the same set the hook provider uses to
# decide something is renderable: if the renderer cannot show it, copying it in
# achieves nothing.
MEDIA_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
    ".pdf": "application/pdf",
}

# Ends at a backslash too: escaped tool text carries a LITERAL `\n` between URLs (session
# 865950fd), which must split them rather than join two URLs into one (see hooks/provider).
_URL_RE = re.compile(r"https?://[^\s\"'<>)\]}\\]+")


class PersistError(Exception):
    """Copy refused or failed. Carries a reason fit for a log line."""


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def media_type_for(url: str) -> str | None:
    """Content type implied by the URL's extension, or None.

    Matches on the PATH only. A query string routinely ends in something that
    looks like an extension (``?sig=...jpg``) and that must not count.
    """
    try:
        path = urllib.parse.urlparse(url).path.lower()
    except ValueError:
        return None
    for ext, ctype in MEDIA_TYPES.items():
        if path.endswith(ext):
            return ctype
    return None


def is_already_ours(url: str, artifacts_domain: str) -> bool:
    """True when the URL already points at OUR CloudFront or OUR artifacts bucket.

    Guards against copying an asset we already own on every subsequent turn, which
    would otherwise duplicate it once per tool result that mentions it.

    Scoped to OUR domain + OUR bucket ONLY — NOT every ``*.amazonaws.com`` host. The
    M2C proxy bucket is also on ``*.amazonaws.com`` and its presigned URLs EXPIRE, so
    the old ``endswith('.amazonaws.com')`` catch-all made the render mirror treat those
    expiring links as "ours" and SKIP the copy — which is why the inline player broke
    after ~1 hour. Matching only our own bucket lets M2C proxy URLs be mirrored.
    """
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    if artifacts_domain and host == artifacts_domain.lower():
        return True
    # Our own artifacts bucket, virtual-hosted (``<bucket>.s3[.region].amazonaws.com``)
    # or path-style (``s3[.region].amazonaws.com/<bucket>/...``). A foreign bucket (the
    # M2C proxy) matches neither, so it is correctly treated as external and mirrored.
    import os
    ours = (os.environ.get("SKILLS_S3_BUCKET") or os.environ.get("S3_BUCKET", "")).lower()
    if not ours:
        return False
    if host.startswith(f"{ours}.s3"):
        return True
    try:
        path = (urllib.parse.urlparse(url).path or "").lstrip("/").lower()
    except ValueError:
        path = ""
    return host.startswith("s3.") and host.endswith(".amazonaws.com") and path.startswith(f"{ours}/")


def external_media_urls(result: object, artifacts_domain: str) -> list[str]:
    """External, renderable media URLs mentioned anywhere in a tool result.

    Order-preserving and de-duplicated, so a URL repeated in both a summary and
    a structured field is copied once.
    """
    found: list[str] = []
    _walk(result, found)
    out: list[str] = []
    seen: set[str] = set()
    for url in found:
        url = url.rstrip(".,;)")
        if url in seen or "\\" in url:
            continue
        seen.add(url)
        if media_type_for(url) and not is_already_ours(url, artifacts_domain):
            out.append(url)
    return out


def _walk(obj: object, out: list[str]) -> None:
    if isinstance(obj, str):
        out.extend(_URL_RE.findall(obj))
    elif isinstance(obj, dict):
        for value in obj.values():
            _walk(value, out)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            _walk(value, out)


# ---------------------------------------------------------------------------
# Fetch guards
# ---------------------------------------------------------------------------


def validate_fetchable(url: str) -> None:
    """Refuse anything that is not a plain public HTTPS media fetch.

    The URL comes from a tool result, so a third party chose it. Without this the
    runtime would be an open proxy into the VPC and, more to the point, into the
    instance metadata endpoint.
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError as e:
        raise PersistError(f"unparseable url: {e}") from e
    if parsed.scheme != "https":
        raise PersistError(f"scheme must be https, got {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise PersistError("url has no host")
    lowered = host.lower()
    if lowered in ("localhost", "metadata.google.internal") or lowered.endswith(
        (".localhost", ".internal", ".local")
    ):
        raise PersistError(f"host not allowed: {host}")
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise PersistError(f"cannot resolve {host}: {e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        # Checked for EVERY resolved address: a name that returns one public and
        # one private answer would otherwise pass on the strength of the public
        # one and connect to the private one.
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast):
            raise PersistError(f"{host} resolves to non-public {ip}")


def fetch(url: str, *, max_bytes: int = MAX_BYTES,
          timeout: float = FETCH_TIMEOUT_S) -> tuple[bytes, str]:
    """Download `url`. Returns (body, content_type).

    Reads with a cap rather than trusting Content-Length, which a hostile or
    merely wrong server can understate.
    """
    validate_fetchable(url)
    req = urllib.request.Request(url, method="GET")
    req.add_header("user-agent", "mediaworks-harness/1.0")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise PersistError(f"HTTP {resp.status}")
        declared = (resp.headers.get("content-length") or "").strip()
        if declared.isdigit() and int(declared) > max_bytes:
            raise PersistError(f"declared {declared} bytes exceeds cap {max_bytes}")
        body = resp.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise PersistError(f"body exceeds cap {max_bytes}")
        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    if not body:
        raise PersistError("empty body")
    return body, ctype


def key_for(url: str, body: bytes, prefix: str) -> str:
    """Content-addressed key, so re-copying the same bytes is idempotent.

    Digest of the BODY rather than the URL: hosts hand out different signed URLs
    for identical content, and keying on the URL would store it repeatedly.
    """
    digest = hashlib.sha256(body).hexdigest()[:24]
    path = urllib.parse.urlparse(url).path
    ext = ""
    for candidate in MEDIA_TYPES:
        if path.lower().endswith(candidate):
            ext = candidate
            break
    return f"{prefix}external/{digest}{ext}"


# ---------------------------------------------------------------------------
# Copy
# ---------------------------------------------------------------------------


def persist_url(url: str, *, s3_client, bucket: str, prefix: str,
                artifacts_domain: str, max_bytes: int = MAX_BYTES) -> dict:
    """Copy one external media URL into our bucket.

    Returns ``{"source_url", "url", "key", "content_type", "bytes"}``.
    ``source_url`` is retained because provenance matters: the record should say
    which third party generated an asset, and the copy is otherwise
    indistinguishable from something we made.
    """
    declared = media_type_for(url)
    if not declared:
        raise PersistError("url has no recognised media extension")

    body, served = fetch(url, max_bytes=max_bytes)

    # Prefer what the URL says over what the server says. A generic
    # application/octet-stream would otherwise be written to S3 and the browser
    # would download the image instead of displaying it.
    content_type = declared
    if served and served != declared and served != "application/octet-stream":
        # A real disagreement (URL says .jpg, server says video/mp4) is worth
        # recording, but the extension still wins: it is what the renderer and
        # the templates keyed off when they decided this was renderable at all.
        logger.info("persist: %s declared %s, server said %s; using %s",
                    url[:80], declared, served, declared)

    key = key_for(url, body, prefix)
    s3_client.put_object(Bucket=bucket, Key=key, Body=body,
                         ContentType=content_type)
    if artifacts_domain:
        new_url = f"https://{artifacts_domain}/{key}"
    else:
        new_url = s3_client.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=3600)
    logger.info("persist: copied %d bytes from %s -> %s",
                len(body), urllib.parse.urlparse(url).hostname, key)
    # Lineage (projects.lineageEdges): the ONE place every mirror (tool results, plan results, the render
    # harvest) copies third-party media, so the origin -> copy mapping and the copy's attribution to the tool
    # call that returned it are recorded here. A no-op with the flag off; never raises.
    try:
        from media import turn_outputs
        turn_outputs.note_external_copy(url, f"s3://{bucket}/{key}")
    except Exception:  # noqa: BLE001 - lineage must never break a copy
        pass
    return {"source_url": url, "url": new_url, "key": key,
            "content_type": content_type, "bytes": len(body)}


def persist_result_media(result: object, *, s3_client, bucket: str, prefix: str,
                         artifacts_domain: str, limit: int = 8) -> list[dict]:
    """Copy every external media URL in a tool result.

    One failure never blocks the others, and no failure propagates: a tool that
    worked must not be reported as failed because we could not mirror its
    output. Callers get whatever succeeded.
    """
    urls = external_media_urls(result, artifacts_domain)[:limit]
    copied: list[dict] = []
    for url in urls:
        try:
            copied.append(persist_url(
                url, s3_client=s3_client, bucket=bucket, prefix=prefix,
                artifacts_domain=artifacts_domain))
        except PersistError as e:
            logger.warning("persist: skipped %s (%s)", url[:100], e)
        except Exception as e:  # noqa: BLE001 - never break the turn
            logger.warning("persist: failed %s (%s: %s)", url[:100], type(e).__name__, e)
    return copied


def _rewrite_urls(obj: object, mapping: dict[str, str]) -> object:
    """Deep-copy ``obj`` with every occurrence of a mapped source URL replaced.

    Substring replacement (not equality) so a URL embedded in a text block, a
    markdown link, or a structured field is all swapped the same way. Order the
    replacements longest-source-first so one source URL that is a prefix of
    another cannot partially rewrite it.
    """
    ordered = sorted(mapping.items(), key=lambda kv: len(kv[0]), reverse=True)
    if isinstance(obj, str):
        s = obj
        for src, new in ordered:
            if src and src in s:
                s = s.replace(src, new)
        return s
    if isinstance(obj, dict):
        return {k: _rewrite_urls(v, mapping) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_rewrite_urls(v, mapping) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_rewrite_urls(v, mapping) for v in obj)
    return obj


def swap_result_media(result: object, *, s3_client, bucket: str, prefix: str,
                      artifacts_domain: str, limit: int = 8) -> tuple[object, list[dict]]:
    """Copy external media in a tool result to our durable store AND return the
    result with every source URL rewritten to its durable URL.

    This is the source-mirror chokepoint for third-party (e.g. fal.media) output:
    swapping at the results boundary means EVERY downstream consumer — the chat
    text the agent writes, the render manifest, a follow-up edit that re-sends the
    asset — sees the durable CloudFront URL, so nothing points at an ephemeral
    third-party URL that later 404s.

    Best-effort and never fatal: any URL that cannot be copied is left unchanged
    (a working tool is never reported as failed because we could not mirror it),
    and it is idempotent with the render hook's own mirror because
    :func:`is_already_ours` URLs are never re-copied. Returns
    ``(new_result, copied)`` where ``copied`` is the list of persist records
    (empty when there was nothing external to mirror, in which case
    ``new_result is result``).
    """
    copied = persist_result_media(
        result, s3_client=s3_client, bucket=bucket, prefix=prefix,
        artifacts_domain=artifacts_domain, limit=limit)
    if not copied:
        return result, []
    mapping = {c["source_url"]: c["url"] for c in copied
               if c.get("source_url") and c.get("url")}
    if not mapping:
        return result, copied
    return _rewrite_urls(result, mapping), copied
