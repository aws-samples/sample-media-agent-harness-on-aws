"""Multi-gateway MCP tool manager via AgentCore Gateway.

Supports connecting to multiple AgentCore Gateways simultaneously.
Each gateway provides its own set of tools via tools/list and optional
semantic search via x_amz_bedrock_agentcore_search.

Tool states per gateway:
  - discovered: tool exists on the gateway (from tools/list)
  - added: tool is registered in the agent's tool list (agent can see and attempt to use it)
  - trusted: tool can be used without user approval
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import random
import threading
import time
import uuid
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Callable, Iterator, Optional

import boto3
import httpx
from strands import Agent
from strands.tools.mcp import MCPClient
# Keep gateway tool-ERROR results readable: strict mcp 2.x result validation rejects error results
# that lack ``resultType`` and would otherwise hide the server's message (see module docstring).
import mcp_result_compat  # noqa: F401,E402

# NOTE: absolute imports (``from jobs ...``) rather than relative
# (``from .jobs ...``) so this module loads under both import styles
# the agent runtime uses: as ``src.mcp_manager`` (when callers do
# ``from src.mcp_manager import MCPManager`` from ``agent/``) and as
# top-level ``mcp_manager`` (the AgentCore entry-point loads
# ``main.py`` with ``src/`` on ``sys.path``, which makes both
# ``mcp_manager`` and ``jobs`` siblings rather than members of a
# package). The mismatch was introduced in task 3.1 as relative
# imports; switched here in task 5.3 so ``main.py`` — which uses
# absolute imports for every other module — loads cleanly.
from jobs import JobTrio
from jobs.detector import DetectionResult, ToolDescriptor, classify_and_group

if TYPE_CHECKING:
    from gateway.registry import GatewayDescriptor, GatewayRegistry

    from jobs.registry import JobRegistry
logger = logging.getLogger(__name__)


# ---- Gateway boundary resilience (P1) ----
# Bound the OAuth token fetch and circuit-break a persistently-unreachable
# gateway so a down media gateway degrades gracefully (chat keeps working)
# instead of hanging agent startup or being re-hammered on every connect.
_TOKEN_HTTP_TIMEOUT = float(os.environ.get("GATEWAY_TOKEN_TIMEOUT", "10"))
_TOKEN_MAX_ATTEMPTS = int(os.environ.get("GATEWAY_TOKEN_MAX_ATTEMPTS", "3"))
_CB_FAILURE_THRESHOLD = int(os.environ.get("GATEWAY_CB_THRESHOLD", "3"))
_CB_COOLDOWN_SECONDS = float(os.environ.get("GATEWAY_CB_COOLDOWN", "60"))
# connect_all connects gateways side by side; bounded so a registry with many rows
# cannot open an unbounded number of OAuth/MCP handshakes at once.
_CONNECT_MAX_PARALLEL = max(1, int(os.environ.get("GATEWAY_CONNECT_MAX_PARALLEL", "8")))

# Header used to assert the calling actor to an AgentCore Gateway interceptor,
# which scopes user-registered MCP tools per actor. Must match the
# `ACTOR_HEADER` the interceptor Lambda reads; the gateway only forwards
# headers to the interceptor when `passRequestHeaders` is enabled.
ACTOR_HEADER = os.environ.get("GATEWAY_ACTOR_HEADER", "X-Actor-Id")

# Header carrying a signed delegation grant to the gateway interceptor for
# async (grant-bearing) plan traffic (Wave 1 / option E). This MUST byte-match
# the interceptor's ``DELEGATION_GRANT_HEADER``
# (infra/lib/lambdas/gateway-interceptor/index.py): the interceptor reads
# exactly this name and, when present, derives the actor SOLELY from the
# verified grant. A mismatch is not an error there — it silently presents as
# "no grant", so the request falls to the JWT path and denies. The
# ``X-Amzn-Bedrock-AgentCore-Runtime-Custom-`` prefix is the runtime
# custom-header convention, chosen so the gateway forwards it to the
# interceptor on the same ``passRequestHeaders`` path the user token uses.
# Do NOT diverge from the interceptor's literal without changing both.
DELEGATION_GRANT_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-DelegationGrant"


def _backoff_sleep(attempt: int, *, base: float = 0.5, cap: float = 8.0) -> None:
    """Exponential backoff with full jitter between retry attempts."""
    delay = min(cap, base * (2 ** attempt))
    time.sleep(random.uniform(0, delay))


def _user_token_is_expired(token: str, *, skew_seconds: float = 30.0) -> bool:
    """True ONLY when ``token`` is present and provably past its ``exp``.

    Reads the JWT ``exp`` claim WITHOUT verifying the signature — the gateway
    front door does the real verification; here we only need to know whether a
    *pinned* user token can still cross that door. A token whose ``exp`` cannot
    be read (not a JWT, no ``exp`` claim, malformed base64) returns ``False`` —
    "unknown, treat as valid" — so a parsing quirk never tears down a live
    interactive session. The fix only fires when we can PROVE the token is
    stale.

    ``skew_seconds`` treats a token as expired slightly early so it is not
    presented when it would lapse mid-connect. Matches the machine token's own
    300s-early refresh intent (smaller here: user tokens are re-bound per turn).
    """
    if not token:
        return False
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return False
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)  # restore base64url padding
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        exp = claims.get("exp")
        if exp is None:
            return False
        return time.time() >= (float(exp) - skew_seconds)
    except Exception:
        return False


# Gateway tool names are ``<target>___<tool>``. Kept local rather than imported
# from jobs.detector so this module has no dependency on job detection internals.
# NOTE: a name can carry TWO of these separators (``target___aws___call_aws``);
# always split with maxsplit=1.
_GATEWAY_NAMESPACE_SEPARATOR = "___"
# Service-emitted semantic tool-search name — a Gateway built-in, distinct from
# public web search. Special-cased in a few places; named once here so the
# magic string is not duplicated.
SEARCH_TOOL_NAME = "x_amz_bedrock_agentcore_search"


def _load_withheld_tool_prefixes() -> tuple[str, ...]:
    """Load the shared hybrid-search withhold prefixes (media-harness-projects-access task 11.8).

    The prefixes live in ``project_search/withheld-tool-prefixes.json`` — a single fixture read by both
    this Python filter and the proxy's ``ui/server/toolCatalog.ts`` so the two lists can never drift. A
    read failure falls back to the design's literal defaults rather than failing a connect, since the
    worst case of a stale list is exposing a tool that should be hidden, not a crash on startup.
    """
    path = os.path.join(os.path.dirname(__file__), "project_search", "withheld-tool-prefixes.json")
    defaults = ("hybrid-search___", "hybrid-search-admin___")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        logger.warning("could not read withheld-tool-prefixes.json at %s; using defaults", path)
        return defaults
    prefixes = tuple(p for p in data if isinstance(p, str) and p) if isinstance(data, list) else ()
    return prefixes or defaults


# Tool-name prefixes WITHHELD from every gateway connection and the tool catalog (Req 5.4, Component 8).
# The hybrid-search tools are Projects-only now (reached through the runtime Search_Tool, not as a
# user-facing gateway tool), so a tool whose full name — or its name after one ``___`` split — begins
# with any of these is never exposed to the model on ANY gateway. Loaded from the shared fixture.
WITHHELD_TOOL_PREFIXES = _load_withheld_tool_prefixes()


def _extract_tool_name(tool) -> str:
    """Extract the tool name from various tool object types."""
    if hasattr(tool, "tool_spec") and isinstance(tool.tool_spec, dict):
        return tool.tool_spec.get("name", str(tool))
    if hasattr(tool, "name") and isinstance(tool.name, str):
        return tool.name
    if hasattr(tool, "tool_name") and isinstance(tool.tool_name, str):
        return tool.tool_name
    return str(tool)


def _extract_tool_description(tool) -> str:
    """Extract the tool description from various tool object types."""
    if hasattr(tool, "tool_spec") and isinstance(tool.tool_spec, dict):
        return tool.tool_spec.get("description", "")
    if hasattr(tool, "description") and isinstance(tool.description, str):
        return tool.description
    return ""


def _extract_tool_annotations(tool) -> Optional[dict]:
    """Extract the MCP ``annotations`` block from a Strands MCP tool wrapper.

    Same access path as :func:`_extract_tool_meta`: Strands keeps the underlying
    ``mcp.types.Tool`` on ``mcp_tool``, and ``annotations`` is a first-class field
    on it alongside name/description.

    ``readOnlyHint`` is the protocol's own answer to "does this tool change
    anything", which makes it a far better artifact discriminator than a
    hand-maintained list of tool names: a read-only tool cannot have produced a
    new artifact. It also covers servers nobody has registered yet, which a list
    by definition cannot.

    Returns None when the server omits annotations -- they are optional in the
    spec, so callers must have a fallback rather than treating absence as false.
    """
    inner = getattr(tool, "mcp_tool", None) or tool
    ann = getattr(inner, "annotations", None)
    if ann is None:
        return None
    if isinstance(ann, dict):
        return ann
    dump = getattr(ann, "model_dump", None)
    if callable(dump):
        try:
            return dump()
        except Exception:  # noqa: BLE001 - annotations are advisory; never fail a connect
            return None
    return None


# Names of tools whose server declared readOnlyHint. Module-level because the
# render hook needs it per tool NAME with no gateway handle, and it is global
# across gateways since federated names carry their target prefix.
_READ_ONLY_TOOL_NAMES: set[str] = set()


def read_only_tool_names() -> set[str]:
    """Tool names the SERVER declared read-only via MCP annotations.

    Advisory only, and deliberately never used for authorisation: annotations
    are server-supplied, so a tool declaring itself harmless must not thereby
    become permitted. Safe for rendering decisions, wrong for access control --
    that stays an explicit denylist.
    """
    return set(_READ_ONLY_TOOL_NAMES)


def _extract_tool_meta(tool) -> Optional[dict]:
    """Extract the MCP ``_meta`` dict from a Strands MCP tool wrapper.

    Strands wraps each MCP tool returned by ``list_tools_sync`` as an
    :class:`strands.tools.mcp.MCPAgentTool`, which keeps the underlying
    ``mcp.types.Tool`` on the ``mcp_tool`` attribute. The pydantic model
    exposes the ``_meta`` block under the Python attribute ``meta``
    (``Field(alias="_meta")``), which is the dict the Job_Detector wants
    for annotation-driven classification (R2.1, R2.3).

    Returns ``None`` when the tool isn't a Strands MCP wrapper, when the
    server emitted no ``_meta`` block, or when the value isn't a dict.
    The detector only treats ``meta`` as authoritative when it carries a
    legal ``job_role`` value, so a missing or malformed block simply
    falls through to convention-based detection.
    """
    mcp_tool = getattr(tool, "mcp_tool", None)
    if mcp_tool is None:
        return None
    meta = getattr(mcp_tool, "meta", None)
    if isinstance(meta, dict):
        return meta
    return None


def _fetch_secret_string(secrets_client: Any, secret_id: str) -> str:
    """Resolve a Secrets Manager secret to the OAuth client-secret value.

    Pulled out so :meth:`MCPManager.load_gateways_from_registry` can
    hand it to :func:`asyncio.to_thread` without capturing ``self``.
    Raises whatever ``secrets_client.get_secret_value`` raises when the
    secret is missing or access is denied; the caller logs and skips
    the offending descriptor.

    Two secret shapes are accepted:

    - a bare string — the raw client secret (the shape written by
      ``GatewayRegistry.register`` and the legacy migration); returned
      verbatim.
    - a JSON object carrying a ``clientSecret`` / ``client_secret``
      field (e.g. the M2C ``media-workflow-gateway`` credentials bundle
      ``{endpointUrl, tokenEndpoint, clientId, clientSecret, scope}``);
      the client-secret field is extracted so a registry row can point
      ``client_secret_arn`` at that bundle directly without duplicating
      the secret.
    """
    resp = secrets_client.get_secret_value(SecretId=secret_id)
    raw = resp["SecretString"]
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return raw
    if isinstance(data, dict):
        for key in ("clientSecret", "client_secret", "token", "access_token"):
            val = data.get(key)
            if isinstance(val, str) and val:
                return val
    return raw


def _search_hits(payload: object) -> list:
    """Normalise a gateway search response to a list of tool entries.

    AgentCore returns ``{"tools": [{name, description, inputSchema}, ...]}``.
    Returning that dict verbatim made callers iterate it, which yields its KEYS
    — so a search would "succeed" with the single literal name "tools", match no
    real tool, and narrowing would fail open on every request. Accepts a bare
    list too, since the envelope is not contractual.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("tools", "results", "capabilities"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _describe_exc(exc: BaseException, _depth: int = 0) -> str:
    """Render `exc` including anything an ExceptionGroup is hiding.

    The MCP transport runs under anyio, so a transport failure surfaces as
    "unhandled errors in a TaskGroup (1 sub-exception)" -- which says only that
    something failed, not what. The single sub-exception is almost always the
    answer (a 401, a TLS error, a refused connection), so it has to be in the
    message: the alternative is diagnosing a connection failure with no
    information about it, which cost a full deploy cycle to learn.

    Recurses because groups nest, bounded so a cycle cannot hang startup.
    """
    text = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    inner = getattr(exc, "exceptions", None)
    if inner and _depth < 4:
        parts = [_describe_exc(sub, _depth + 1) for sub in inner]
        return f"{text} [{'; '.join(parts)}]"
    cause = exc.__cause__
    if cause is not None and _depth < 4:
        return f"{text} (caused by {_describe_exc(cause, _depth + 1)})"
    return text


def _is_auth_expiry(text: str) -> bool:
    """True when a connect/transport failure description indicates an AUTH failure
    (an expired/invalid forwarded token or grant) rather than a real upstream outage.

    A ``user_jwt`` gateway crosses the AgentCore Gateway front door with the caller's
    forwarded token/grant; when that credential expires mid-session the gateway answers
    the ``/mcp`` connect with ``403 Forbidden`` (or ``401``). That is a transient,
    refresh-on-retry condition — NOT a fal.ai/platform outage — so it must be described
    as such (both for the retry logic and the user-facing message). Matches on the
    rendered status text since the failure arrives wrapped in a TaskGroup/HTTPStatusError.
    """
    low = (text or "").lower()
    return (
        "403 forbidden" in low
        or "401 unauthorized" in low
        or ("forbidden" in low and "403" in low)
        or ("unauthorized" in low and "401" in low)
    )


class GatewayConnection:
    """A single Gateway connection with its own auth and tool state."""

    def __init__(self, gw_id: str, config: dict):
        self.id = gw_id
        self.name = config.get("name", gw_id)
        self.url = config["url"]
        self.auth_type = config.get("auth_type", "jwt")
        self.client_id = config.get("client_id", "")
        self.client_secret = config.get("client_secret", "")
        self.token_endpoint = config.get("token_endpoint", "")
        self.scope = config.get("scope", "")
        # SigV4 config (used when auth_type == "sigv4")
        self.sigv4_region = config.get("region", os.environ.get("AWS_REGION", "us-east-1"))
        self.sigv4_service = config.get("service", "bedrock-agentcore")
        # Static-token config (used when auth_type == "bearer"). The
        # literal is merged in by MCPManager._connection_for_descriptor
        # after resolving `token_arn`; the descriptor never carries it.
        self.bearer_token = config.get("bearer_token", "")
        # Per-target job ROLE declarations for servers that follow neither the
        # start_*/*_status/*_results naming convention nor MCP's _meta job_role
        # annotation. fal is the case in point: submit_job / check_job /
        # get_job_result form a complete async trio that the convention cannot
        # see, so its jobs were never registered, never polled, and never
        # notified -- unlike every other async job in the system.
        #
        # This is configuration, not a code path: the Job_Detector already treats
        # _meta.job_role as authoritative (R2.3), so declaring roles here feeds
        # the SAME mechanism an annotating server would use. Nothing downstream
        # learns that fal exists.
        #   {"submit_job": {"job_role": "start", "job_group": "fal|job"}, ...}
        self.job_role_overrides = config.get("job_role_overrides") or {}
        # Optional fail-closed tool allowlist. When set, ONLY these tool names are
        # exposed, filtered at the two points where the server's tool list is read
        # so every downstream consumer (list_tools, get_added_tools, search,
        # trusted names, job detection) sees the reduced set and cannot be handed
        # a tool the allowlist excluded.
        self.tool_allowlist = frozenset(config.get("tool_allowlist") or ())
        self.header_name = config.get("header_name") or "Authorization"
        self.value_prefix = (
            config["value_prefix"]
            if config.get("value_prefix") is not None
            else "Bearer "
        )
        # Actor asserted to the gateway so its interceptor can scope tools to
        # this caller. Bound at connect time rather than per request, which is
        # correct because AgentCore gives each session its own microVM and a
        # session belongs to one user — so one container serves one actor for
        # its lifetime. (It also has to be bound here: Strands' MCPClient runs
        # its transport on a background thread seeded with a contextvars
        # snapshot taken at __enter__, so a contextvar set later during a
        # request would never reach the request headers.)
        self.actor_id = config.get("actor_id", "") or ""
        # The latest calling-user token received by this runtime. Never
        # persisted. This is the token to use for the NEXT interactive connect,
        # but it is not necessarily the token baked into an already-live
        # MCPClient, whose headers are static.
        self.user_token = config.get("user_token", "") or ""
        # Track the user token actually baked into the live MCP transport. The
        # distinction is essential: a later browser turn can refresh
        # ``user_token`` without changing an existing MCPClient's static
        # Authorization header. Detached auth decisions must inspect the bound
        # credential, not mistake the latest field value for transport state.
        self._bound_user_token = ""
        # Signed delegation grant for async plan traffic (Wave 1 / option E).
        # When set together with the machine (client_credentials) OAuth creds
        # above, the ``user_jwt`` connect path crosses the gateway front door
        # with a MACHINE token and presents this grant as
        # ``DELEGATION_GRANT_HEADER``; the interceptor derives the actor from
        # the verified grant, not from the machine token (which carries no user
        # ``sub``). Empty on the normal interactive (user-token) path.
        self.delegation_grant = config.get("delegation_grant", "") or ""

        self._client: Optional[MCPClient] = None
        self._connected = False
        self._all_tools: list = []  # raw Strands tool objects
        self._tool_metadata: list[dict] = []  # {name, description}
        self._added: set[str] = set()  # tools registered on the agent
        self._trusted: set[str] = set()  # tools that skip consent
        self._search_available = False
        self._token: Optional[str] = None
        self._token_expires_at: Optional[float] = None
        # Job_Detector cache (task 3.1). Re-populated by
        # ``_run_job_detection`` on every successful ``connect`` /
        # ``refresh_tools`` so the per-tool job-tracking wrap (task
        # 5.3, see ``jobs.strands_tool_wrap``) and the catalog
        # endpoint can resolve trio membership in O(1).
        self.job_detection: Optional[DetectionResult] = None
        # Circuit breaker (P1): after _CB_FAILURE_THRESHOLD consecutive connect
        # failures, open the breaker for _CB_COOLDOWN_SECONDS so a down gateway
        # is skipped (fast fail) instead of re-running the slow OAuth+connect on
        # every attempt. Half-opens automatically once the cooldown elapses.
        self._cb_failures = 0
        self._cb_open_until = 0.0
        # Serialize (re)connect/disconnect on THIS connection. A warm task-runtime
        # container serves an async plan's whole fan-out, so many nodes call the
        # delegated reconnect concurrently against the SAME GatewayConnection; with
        # no mutual exclusion they tore down and rebuilt one shared MCPClient at
        # once, producing a cascade of "MCPClientInitializationError: the client
        # session is not running" and leaving the user-MCP tools unresolvable
        # (session 23f1d54b: 16 concurrent failures on one instance). Reentrant so
        # reconnect() -> disconnect()/connect() nest under a single held lock.
        self._connect_lock = threading.RLock()
        # Protect live tool calls from credential-mode replacement. Auth changes
        # run in worker threads and wait for existing calls to drain before
        # closing their MCPClient. Calls beginning during a transition fail
        # closed rather than running under the replacement invocation's token or
        # grant; same-auth fan-out remains fully concurrent.
        self._call_state = threading.Condition()
        self._active_calls = 0
        self._auth_switching = False
        self._auth_transition_owner: int | None = None
        self._auth_transition_depth = 0

    @contextmanager
    def call_lease(self, expected_client: MCPClient) -> Iterator[None]:
        """Keep ``expected_client`` alive for one complete MCP tool call."""
        with self._call_state:
            if (
                self._auth_switching
                or not self._connected
                or expected_client is not self._client
            ):
                raise RuntimeError(
                    f"Gateway '{self.name}' auth context changed before tool execution"
                )
            self._active_calls += 1
        try:
            yield
        finally:
            with self._call_state:
                self._active_calls -= 1
                if self._active_calls == 0:
                    self._call_state.notify_all()

    @contextmanager
    def _auth_transition(self) -> Iterator[None]:
        """Wait for live calls to drain, then make one auth change atomically.

        Reentrant for the thread holding ``_connect_lock`` so a manager-level
        transition can call the public ``disconnect`` and ``connect`` methods
        without briefly reopening the call gate between them.
        """
        owner = threading.get_ident()
        with self._call_state:
            nested = self._auth_transition_owner == owner
            if nested:
                self._auth_transition_depth += 1
            else:
                while self._auth_switching:
                    self._call_state.wait()
                self._auth_switching = True
                self._auth_transition_owner = owner
                self._auth_transition_depth = 1
                while self._active_calls:
                    self._call_state.wait()
        try:
            yield
        finally:
            with self._call_state:
                self._auth_transition_depth -= 1
                if self._auth_transition_depth == 0:
                    self._auth_transition_owner = None
                    self._auth_switching = False
                    self._call_state.notify_all()

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def auth_fingerprint(self) -> tuple[str, str] | None:
        """Identify the credential baked into the current MCP transport."""
        if not self._connected:
            return None
        if self._bound_user_token:
            raw = self._bound_user_token
            mode = "user"
        elif self.auth_type == "user_jwt" and self.delegation_grant:
            raw = self.delegation_grant
            mode = "grant"
        else:
            return None
        return mode, hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def reconnect_for_current_auth(self) -> Callable[[], Any]:
        """Return a reconnect callback pinned to the current auth context."""
        expected = self.auth_fingerprint
        return lambda: self.reconnect(expected_auth_fingerprint=expected)

    #: Upper bound on how long a stale tool waits for an in-flight auth transition to finish
    #: before giving up on re-leasing (the caller then surfaces its original lease error).
    _RELINK_WAIT_SECONDS = 30.0

    def current_client_for_auth(
        self,
        expected_auth_fingerprint: tuple[str, str] | None,
        *,
        timeout: float | None = None,
    ) -> Optional[MCPClient]:
        """The live client, ONLY when it is bound to exactly ``expected_auth_fingerprint``.

        A tool object holds the ``MCPClient`` it was wrapped with. When this connection is
        rebuilt under the SAME credential (the delegated machine token reached its refresh
        deadline, or a peer's session-down reconnect), that tool's client is gone and
        :meth:`call_lease` correctly refuses it — but the agent keeps the stale tool object,
        so every later call fails the same way (session 36685185: 125 failures after one
        02:22 machine-token rebuild under an unchanged grant). Handing that tool the current
        client is safe precisely when the bound credential is unchanged: same grant (or same
        user token) means the same actor, verified by the gateway interceptor.

        Returns None — and the caller fails closed — when the credential differs (another
        invocation's token or grant), the gateway is disconnected, or a transition does not
        finish within ``timeout``. A ``None`` fingerprint (no per-user credential) is matched
        only on non-``user_jwt`` gateways, whose shared credential is not user-specific.
        Waits outside any lease, so it can never hold up the transition it waits for.
        """
        if expected_auth_fingerprint is None and self.auth_type == "user_jwt":
            return None
        wait = self._RELINK_WAIT_SECONDS if timeout is None else timeout
        deadline = time.monotonic() + max(0.0, wait)
        with self._call_state:
            while self._auth_switching:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._call_state.wait(remaining)
            if not self._connected or self._client is None:
                return None
            if self.auth_fingerprint != expected_auth_fingerprint:
                return None
            return self._client

    def relink_for_current_auth(self) -> Callable[[], Any]:
        """Return a re-lease callback pinned to the current auth context (see
        :meth:`current_client_for_auth`); paired with :meth:`reconnect_for_current_auth`."""
        expected = self.auth_fingerprint
        return lambda: self.current_client_for_auth(expected)

    @property
    def tool_metadata(self) -> list[dict]:
        """The ``{name, description}`` entries this gateway reported."""
        return list(self._tool_metadata)

    def call_tool_sync(self, tool_name: str, arguments: dict) -> Any:
        """Invoke one tool on this gateway's MCP session.

        Strands' ``MCPClient.call_tool_sync`` signature is
        ``(tool_use_id, name, arguments)``. Passing ``(name, arguments)``
        positionally silently misbinds the arguments dict into ``name`` and
        drops the real arguments, so a unique ``tool_use_id`` is generated per
        call and the tool name/arguments are forwarded in their correct slots.
        """
        client = self._client
        if client is None:
            raise RuntimeError(f"Gateway '{self.name}' is not connected")
        tool_use_id = f"{tool_name}-{uuid.uuid4().hex}"
        from tool_args import normalize_duration, normalize_endpoint_ids
        arguments = normalize_endpoint_ids(normalize_duration(arguments))
        with self.call_lease(client):
            return client.call_tool_sync(tool_use_id, tool_name, arguments)

    def _session_alive(self) -> bool:
        """Best-effort check that the MCP session behind ``_client`` is actually running.

        ``_connected`` is a sticky flag cleared only by :meth:`disconnect`, but Strands runs
        the transport/``ClientSession`` on a background thread whose streamable-HTTP connection
        the gateway/upstream can idle-close — leaving ``_connected`` ``True`` over a DEAD session.
        A subsequent tool call then raises ``MCPClientInitializationError: the client session is
        not running`` with no reconnect (the fal video-gen infinite-loop root cause: a warm
        container reused a same-grant connection whose session had idle-closed). Probing with a
        cheap ``list_tools_sync`` surfaces the dead session so callers reconnect instead of
        reusing it. Any exception (session not running, transport closed) means "not alive"."""
        if not self._connected or self._client is None:
            return False
        # Use Strands' OWN synchronous liveness check (``_is_session_active``): it inspects the
        # background thread + close-future with NO round-trip and NO coroutine. An earlier version
        # probed with ``list_tools_sync()``, which on a warm-but-dead session spawned an un-awaited
        # ``_list_tools_async`` coroutine and raised inside ``connect()`` — tripping the circuit
        # breaker and dropping the whole gateway, so fal video-gen nodes failed with "tools could
        # not be resolved". If the private hook is ever renamed, fall back to assuming alive so we
        # never destabilize connect (worst case reverts to the old reuse behavior, not a crash).
        check = getattr(self._client, "_is_session_active", None)
        if not callable(check):
            return True
        try:
            return bool(check())
        except Exception:  # noqa: BLE001 — any failure means treat the session as not usable
            return False

    def _meta_with_declared_roles(self, tool_name: str, meta: Optional[dict]) -> Optional[dict]:
        """Merge configured job-role declarations into a tool's MCP ``_meta``.

        A server-supplied annotation always wins: config fills a GAP, it does not
        override what the server said about itself. Returns ``meta`` untouched when
        no declaration applies, so tools on well-behaved servers are unaffected.

        Tool names arrive as ``<target>___<tool>`` and can carry TWO ``___``
        separators (``target___aws___call_aws``), so the lookup tries the full name
        first and then the remainder after the FIRST separator only.
        """
        if not self.job_role_overrides:
            return meta
        if isinstance(meta, dict) and meta.get("job_role"):
            return meta  # the server declared its own role; leave it alone

        declared = self.job_role_overrides.get(tool_name)
        if declared is None and _GATEWAY_NAMESPACE_SEPARATOR in tool_name:
            declared = self.job_role_overrides.get(
                tool_name.split(_GATEWAY_NAMESPACE_SEPARATOR, 1)[1]
            )
        if not isinstance(declared, dict) or not declared.get("job_role"):
            return meta

        merged = dict(meta) if isinstance(meta, dict) else {}
        merged["job_role"] = declared["job_role"]
        group = declared.get("job_group")
        if group:
            merged["job_group"] = group
        return merged

    def _run_job_detection(self) -> None:
        """Refresh the cached :class:`DetectionResult` for this gateway.

        Called from :meth:`connect` and :meth:`refresh_tools` after
        ``self._all_tools`` is repopulated. Builds a list of
        :class:`ToolDescriptor` from the raw Strands tools — pulling
        ``name`` via the existing :func:`_extract_tool_name` helper and
        the optional MCP ``_meta`` block via :func:`_extract_tool_meta`
        — and stamps the gateway id onto every emitted trio so the
        downstream catalog and dispatch paths know which gateway hosts
        each one.

        Defensive against detector failures: any unexpected exception
        is logged and the prior cache is cleared rather than allowed
        to propagate. Tool-list refresh is a critical path and we'd
        rather lose job-detection coverage for one cycle than fail the
        whole connect/refresh.
        """
        try:
            descriptors = [
                ToolDescriptor(
                    name=_extract_tool_name(t),
                    meta=self._meta_with_declared_roles(
                        _extract_tool_name(t), _extract_tool_meta(t)
                    ),
                )
                for t in self._all_tools
            ]
            self.job_detection = classify_and_group(
                descriptors, gateway_id=self.id
            )
            logger.info(
                "Gateway '%s': Job_Detector found %d trio(s), %d orphan(s)",
                self.name,
                len(self.job_detection.trios),
                len(self.job_detection.orphans),
            )
        except Exception as e:  # noqa: BLE001 — keep connect path resilient
            logger.error(
                "Gateway '%s': Job_Detector failed: %s", self.name, e,
            )
            self.job_detection = None

    def _get_oauth_token(self) -> str:
        if self._token and self._token_expires_at and time.time() < self._token_expires_at:
            return self._token
        if not self.token_endpoint:
            raise RuntimeError(f"Gateway '{self.name}': token endpoint not configured")
        # Bounded retries with backoff+jitter on transient failures (network
        # errors, 429, 5xx). A 4xx (bad creds/scope) fails fast — retrying
        # won't help and would just delay startup. A hard timeout prevents a
        # hung token endpoint from blocking agent startup (graceful media
        # degradation: connect_all swallows the raise and the agent loads
        # without this gateway's tools).
        last_err: Optional[str] = None
        for attempt in range(_TOKEN_MAX_ATTEMPTS):
            try:
                resp = httpx.post(
                    self.token_endpoint,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "scope": self.scope,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=_TOKEN_HTTP_TIMEOUT,
                )
            except httpx.HTTPError as e:
                last_err = f"network error: {e}"
                if attempt < _TOKEN_MAX_ATTEMPTS - 1:
                    _backoff_sleep(attempt)
                continue
            if resp.status_code == 200:
                data = resp.json()
                self._token = data["access_token"]
                self._token_expires_at = time.time() + data.get("expires_in", 3600) - 300
                return self._token
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                last_err = f"status {resp.status_code}"
                if attempt < _TOKEN_MAX_ATTEMPTS - 1:
                    _backoff_sleep(attempt)
                continue
            # 4xx (other than 429) — client/config error, do not retry.
            raise RuntimeError(
                f"Gateway '{self.name}' OAuth failed: {resp.status_code}"
            )
        raise RuntimeError(
            f"Gateway '{self.name}' OAuth failed after {_TOKEN_MAX_ATTEMPTS} "
            f"attempts: {last_err}"
        )

    def connect(self) -> dict:
        # Hold the per-connection lock across the ENTIRE connect (liveness check +
        # teardown + rebuild). The auth transition also prevents a live tool call
        # from observing its MCPClient being replaced.
        with self._connect_lock:
            with self._auth_transition():
                return self._connect_impl()

    def _connect_impl(self) -> dict:
        if self._connected:
            # A sticky ``_connected`` can mask a session the gateway/upstream idle-closed.
            # Reuse only if the session is actually alive; otherwise tear down so the reconnect
            # below re-establishes a live one (self-heal) rather than handing back a dead session.
            if self._session_alive():
                return {"status": "already_connected", "tools": self._tool_metadata}
            logger.warning(
                "Gateway '%s': cached MCP session is not running; reconnecting", self.name,
            )
            self._disconnect_impl()

        # Circuit breaker: fail fast while open so a down gateway doesn't make
        # every connect attempt pay the full OAuth + tools/list timeout.
        now = time.time()
        if self._cb_open_until > now:
            raise RuntimeError(
                f"Gateway '{self.name}' circuit open "
                f"({int(self._cb_open_until - now)}s cooldown remaining after "
                f"{self._cb_failures} consecutive failures)"
            )
        if self._cb_open_until:
            # HALF-OPEN: the cooldown has elapsed (the guard above did not fire).
            # Reset the counter so the next attempt is a clean single probe.
            # Without this the count stays >= threshold, so ONE post-cooldown
            # failure instantly re-opens for another FULL cooldown — which is how
            # a couple of transient blips kept an entire fan-out gated in
            # repeating 60s cycles (session 05a6ff2f: 8-frame image edit, breaker
            # opened, then re-gated the remaining staggered frames). A probe
            # failure now counts from 1 and needs the full threshold to re-open.
            self._cb_failures = 0
            self._cb_open_until = 0.0
        try:
            result = self._do_connect()
        except Exception:
            self._cb_failures += 1
            if self._cb_failures >= _CB_FAILURE_THRESHOLD:
                self._cb_open_until = time.time() + _CB_COOLDOWN_SECONDS
                logger.warning(
                    "Gateway '%s': circuit opened after %d consecutive connect "
                    "failures; cooling down %.0fs (chat degrades gracefully — "
                    "agent runs without this gateway's tools)",
                    self.name, self._cb_failures, _CB_COOLDOWN_SECONDS,
                )
            raise
        # Success — reset the breaker.
        self._cb_failures = 0
        self._cb_open_until = 0.0
        return result

    def _actor_headers(self) -> dict:
        """Header asserting the caller's actor to the gateway interceptor.

        Empty when no actor is known. The interceptor fails closed, so an
        absent header means user-scoped tools are hidden rather than exposed.
        """
        if not self.actor_id:
            return {}
        return {ACTOR_HEADER: self.actor_id}

    def _build_auth_headers(self) -> dict:
        """Compute the base auth headers for this connection by auth type.

        Delegates to the pure resolver in ``gateway/auth.py`` (Wave 2). The
        stateful OAuth mint + circuit breaker stay here via ``_get_oauth_token``.
        The caller merges the actor header on top.
        """
        from gateway.auth import GatewayAuthConfig, build_auth_headers

        cfg = GatewayAuthConfig(
            auth_type=self.auth_type,
            url=self.url,
            gateway_name=self.name,
            sigv4_region=self.sigv4_region,
            sigv4_service=self.sigv4_service,
            bearer_token=self.bearer_token,
            header_name=self.header_name,
            value_prefix=self.value_prefix,
            user_token=self.user_token,
            delegation_grant=self.delegation_grant,
            client_id=self.client_id,
            client_secret=self.client_secret,
            token_endpoint=self.token_endpoint,
        )
        return build_auth_headers(
            cfg,
            oauth_token_fn=self._get_oauth_token,
            delegation_grant_header=DELEGATION_GRANT_HEADER,
        )

    def _do_connect(self) -> dict:
        # Each auth type contributes its own headers; the actor header is
        # merged in once so no auth path can silently omit it.
        # SigV4 (AWS_IAM Platform gateways) signs EACH request via an httpx auth
        # provider with current credentials (Wave 3); every other auth type sets
        # static connection headers. The actor header is always included.
        if self.auth_type == "sigv4":
            from gateway.sigv4 import SigV4RequestSigner, default_credentials_provider

            actor = self._actor_headers()
            self._client = MCPClient(
                url=self.url,
                headers=actor or None,
                auth_provider=SigV4RequestSigner(
                    service=self.sigv4_service,
                    region=self.sigv4_region,
                    credentials_provider=default_credentials_provider(self.sigv4_region),
                ),
            )
        else:
            headers = self._build_auth_headers()
            headers.update(self._actor_headers())
            # Construct via Strands' public url/headers interface (forward-
            # compatible with MCP 2.x). Headers apply to every request.
            if headers:
                self._client = MCPClient(url=self.url, headers=headers)
            else:
                self._client = MCPClient(url=self.url)
        try:
            self._client.__enter__()
            raw_tools = self._list_all_tools()
        except Exception as e:
            try:
                self._client.__exit__(None, None, None)
            except Exception:
                pass
            self._client = None
            desc = _describe_exc(e)
            if _is_auth_expiry(desc):
                raise RuntimeError(
                    f"Gateway '{self.name}' authorization failed — the forwarded user "
                    f"token/grant expired or was rejected (auth error). This is a transient "
                    f"session-refresh condition, not a provider outage: retry the request to "
                    f"reconnect with a fresh token. ({desc})"
                ) from e
            raise RuntimeError(
                f"Gateway '{self.name}' connect failed: {desc}"
            ) from e

        # Record the credential that is now immutable inside this MCPClient's
        # static headers. Do not infer it later from ``self.user_token`` because
        # that field can receive a newer browser token between connections.
        self._bound_user_token = (
            self.user_token if self.auth_type == "user_jwt" and self.user_token else ""
        )
        self._connected = True
        raw_tools = self._withhold_tools(self._apply_tool_allowlist(list(raw_tools)))
        self._all_tools = list(raw_tools)
        self._apply_resilient_wrap()
        self._rebuild_tool_metadata()
        # Nothing added or trusted by default
        self._added = set()
        self._trusted = set()
        # Run Job_Detector now that ``_all_tools`` is fresh. Cache
        # lifecycle is tied to the connect/refresh cycle: a successful
        # connect always re-runs detection, a disconnect clears it.
        self._run_job_detection()
        logger.info("Gateway '%s': connected with %d tools (search: %s)%s",
                     self.name, len(self._tool_metadata), self._search_available,
                     self._per_target_summary())
        return {"status": "connected", "tools": self._tool_metadata, "search_available": self._search_available}

    def disconnect(self) -> bool:
        with self._connect_lock:
            with self._auth_transition():
                return self._disconnect_impl()

    def _disconnect_impl(self) -> bool:
        if not self._connected or not self._client:
            return False
        try:
            self._client.__exit__(None, None, None)
        except Exception as e:
            logger.warning("Gateway '%s' close error: %s", self.name, e)
        self._client = None
        self._connected = False
        self._bound_user_token = ""
        self._all_tools = []
        self._tool_metadata = []
        self._added.clear()
        self._trusted.clear()
        self._search_available = False
        # Detector cache is only valid while we hold a live tool list.
        self.job_detection = None
        return True

    def reconnect(
        self,
        *,
        expected_auth_fingerprint: tuple[str, str] | None = None,
    ):
        """Force a FRESH MCP session and return its live client.

        The resilient tool wrapper (:mod:`jobs.strands_tool_wrap`) calls this when a
        tool invocation dies with a pre-request session-down error
        (``MCPClientInitializationError``). We do NOT go through :meth:`connect`'s
        already-connected fast path — its liveness probe (``_is_session_active``)
        only inspects local state and reads a gateway-idle-closed session as still
        alive, which is exactly what produced the dead session. Tear down
        unconditionally, then reconnect so a brand-new ``initialize`` runs. Returns
        the fresh ``MCPClient`` (or ``None`` if reconnect could not establish one).

        STILL REQUIRED on mcp 2.x / strands 1.55 — do not delete on the strength of
        the "stateless MCP" upgrade. Earlier commits called this an interim workaround
        "until Strands unpins mcp<2.0.0 and a stateless client removes the long-lived
        session." Only the FIRST half happened: we run mcp 2.1.1 and the 2026-07-28
        *protocol* is stateless, but strands' ``MCPClient`` is STILL a long-lived,
        background-thread ``ClientSession`` — so the gateway can still idle-close it
        server-side while ``_is_session_active`` (local state only) reads "alive", and
        a reused call still dies with ``MCPClientInitializationError``. Removal must be
        gated on evidence, not on the version bump: see
        ``tests/test_mcp_idle_close_recovery.py`` — only if strands transparently
        recovers a server-closed session WITHOUT this wrapper is it safe to drop.
        """
        with self._connect_lock:
            if (
                expected_auth_fingerprint is not None
                and self.auth_fingerprint != expected_auth_fingerprint
            ):
                raise RuntimeError(
                    f"Gateway '{self.name}' auth context changed; refusing to "
                    "retry under another invocation's credential"
                )
            with self._auth_transition():
                # Re-check after active calls drain. A waiting credential
                # transition cannot pass _connect_lock, but keeping the check
                # here documents and enforces the retry invariant locally.
                if (
                    expected_auth_fingerprint is not None
                    and self.auth_fingerprint != expected_auth_fingerprint
                ):
                    raise RuntimeError(
                        f"Gateway '{self.name}' auth context changed while waiting"
                    )
                try:
                    self._disconnect_impl()
                except Exception as e:  # noqa: BLE001 — best-effort teardown
                    logger.warning("Gateway '%s': reconnect teardown failed: %s", self.name, e)
                self._connect_impl()  # respects the circuit breaker; raises if truly down
                if (
                    expected_auth_fingerprint is not None
                    and self.auth_fingerprint != expected_auth_fingerprint
                ):
                    raise RuntimeError(
                        f"Gateway '{self.name}' reconnected with an unexpected auth context"
                    )
                return self._client

    def _apply_resilient_wrap(self) -> None:
        """Wrap every plain MCP tool so a dead session self-heals (reconnect+retry once).

        Strict-additive and best-effort: any failure leaves the tool list untouched so
        connect never destabilizes. Job-tracking trio members are re-wrapped later by
        :meth:`MCPManager._apply_job_tracking_wrap` (they inherit the same resilience).
        """
        try:
            from jobs.strands_tool_wrap import wrap_resilient
            self._all_tools = wrap_resilient(
                self._all_tools,
                reconnect=self.reconnect_for_current_auth(),
                call_lease=self.call_lease,
                relink=self.relink_for_current_auth(),
            )
        except Exception as e:  # noqa: BLE001 — never break connect over resilience wrapping
            logger.error("Gateway '%s': resilient tool wrap skipped: %s", self.name, e)

    # A gateway with several targets returns its tool list in PAGES. Strands'
    # ``list_tools_sync`` fetches exactly ONE page -- it forwards
    # ``pagination_token`` as the MCP ``cursor`` and hands back a
    # ``PaginatedList`` whose ``pagination_token`` is the server's
    # ``nextCursor``. Following that cursor is the caller's job.
    #
    # Not following it silently truncates the catalogue. This cost a live
    # regression: the harness gateway reported a suspiciously constant 13 tools
    # while its two targets actually offered ~20, so fal's ``run_model`` sat one
    # page past the boundary and the agent concluded -- correctly, given what it
    # could see -- that no image-generation tool existed. A truncated list is
    # indistinguishable from a short one, which is why this failed quietly.
    #
    # ``PaginatedList`` subclasses ``list``, so ``list(...)`` around the call
    # drops the token without any type error. Never wrap the call directly.
    _MAX_TOOL_PAGES = 50

    def _per_target_summary(self) -> str:
        """Per-target tool counts, appended to the connect log.

        An aggregate count cannot distinguish a healthy gateway from one whose
        target holds a truncated tool snapshot. That ambiguity hid a live bug for
        a day: the fal target had synchronised 3 of its 11 tools at registration
        and never re-synced, so ``run_model`` did not exist as far as the agent
        was concerned -- and "13 tools" looked perfectly ordinary. A federated
        gateway target is a SNAPSHOT (see its ``lastSynchronizedAt``), not a live
        passthrough, so this drifts silently whenever an upstream server adds
        tools.

        Printing the split makes that visible at a glance: a target you know
        offers eleven tools reporting three is obvious, where 13 was not.
        """
        counts: dict[str, int] = {}
        for m in self._tool_metadata:
            target = m["name"].split(_GATEWAY_NAMESPACE_SEPARATOR, 1)[0] if _GATEWAY_NAMESPACE_SEPARATOR in m["name"] else "(un-namespaced)"
            counts[target] = counts.get(target, 0) + 1
        if len(counts) <= 1:
            return ""
        parts = ", ".join(f"{t}={n}" for t, n in sorted(counts.items()))
        return f" [by target: {parts}]"

    def _list_all_tools(self) -> list:
        """Fetch every page of ``tools/list``, following the pagination cursor."""
        collected: list = []
        seen: set[str] = set()
        token = None
        for page in range(self._MAX_TOOL_PAGES):
            batch = self._client.list_tools_sync(pagination_token=token)
            for t in batch:
                # A cursor that fails to advance would otherwise loop forever
                # re-adding the same page; dedupe by name so it terminates.
                name = _extract_tool_name(t)
                if name and name in seen:
                    continue
                if name:
                    seen.add(name)
                collected.append(t)
            token = getattr(batch, "pagination_token", None)
            if not token:
                if page:
                    logger.info(
                        "Gateway '%s': tools/list spanned %d page(s), %d tool(s)",
                        self.name, page + 1, len(collected),
                    )
                return collected
        logger.warning(
            "Gateway '%s': tools/list still paginating after %d pages; "
            "using the %d tool(s) collected so far",
            self.name, self._MAX_TOOL_PAGES, len(collected),
        )
        return collected

    def _rebuild_tool_metadata(self) -> None:
        """Rebuild ``_tool_metadata`` + search/read-only flags from ``_all_tools``.

        Shared by connect and refresh (the block was duplicated verbatim in
        both). Pure per-tool normalization lives in ``gateway/catalog.py``; the
        read-only-name registry and search-availability side effects stay here.
        """
        from gateway.catalog import normalize_tool_metadata

        self._tool_metadata = normalize_tool_metadata(
            self._all_tools,
            extract_name=_extract_tool_name,
            extract_description=_extract_tool_description,
            extract_annotations=_extract_tool_annotations,
        )
        self._search_available = False
        for m in self._tool_metadata:
            name = m["name"]
            ann = m["annotations"]
            if ann and ann.get("readOnlyHint"):
                _READ_ONLY_TOOL_NAMES.add(name)
            else:
                _READ_ONLY_TOOL_NAMES.discard(name)
            if name == SEARCH_TOOL_NAME:
                self._search_available = True

    def refresh_tools(self) -> dict:
        if not self._connected or not self._client:
            return {"error": f"Gateway '{self.name}' not connected"}
        raw_tools = self._withhold_tools(self._apply_tool_allowlist(self._list_all_tools()))
        self._all_tools = list(raw_tools)
        self._apply_resilient_wrap()
        old_added = self._added.copy()
        old_trusted = self._trusted.copy()
        self._rebuild_tool_metadata()
        current_names = {m["name"] for m in self._tool_metadata}
        self._added = old_added & current_names
        self._trusted = old_trusted & current_names
        # Re-run detection against the refreshed tool set so any
        # newly published trios become visible and any tools that
        # disappeared drop out of the cached lookup tables.
        self._run_job_detection()
        return {"status": "refreshed", "tools": self.list_tools(), "search_available": self._search_available}

    def awaiting_user_token(self) -> bool:
        """True when this gateway needs the caller's token and hasn't got one.

        A `user_jwt` gateway is only connectable on invocations that actually
        carry a user token. Management calls do not: opening the MCP panel or
        fetching the tool catalog reaches the runtime without one. Attempting the
        connection anyway raises, which `connect_all` logs at ERROR -- 20 times an
        hour in production, for a condition that is expected and harmless.

        This is NOT the token-latch bug (fixed separately by
        `bind_user_token_and_connect`, which retries once a token does arrive).
        This only stops the doomed attempt, and the wasted round trip, from being
        reported as a failure.

        A gateway armed with a delegation grant + machine creds (the async plan
        path, `set_delegated_machine_auth`) is NOT awaiting a user token — it can
        cross the front door on its own — so it is excluded here and
        `connect_all` will attempt it.
        """
        return (
            self.auth_type == "user_jwt"
            and not self.user_token
            and not (
                self.delegation_grant
                and self.client_id
                and self.client_secret
                and self.token_endpoint
            )
        )

    def set_delegated_machine_auth(
        self,
        *,
        grant: str,
        token_endpoint: str,
        client_id: str,
        client_secret: str,
        scope: str = "",
    ) -> None:
        """Select machine OAuth + delegation-grant auth for this connection.

        A grant-bearing task must not inherit an interactive browser token from
        the warm runtime. The grant is the task's durable actor credential, and
        the machine token is renewable. Clear ``user_token`` atomically with
        arming that mode because the auth resolver intentionally gives a user
        token precedence whenever one remains present.
        """
        self.user_token = ""
        self.delegation_grant = grant or ""
        self.token_endpoint = token_endpoint or ""
        self.client_id = client_id or ""
        self.client_secret = client_secret or ""
        self.scope = scope or ""

    def _apply_tool_allowlist(self, raw_tools: list) -> list:
        """Drop any tool not on the allowlist. No allowlist means no filtering."""
        if not self.tool_allowlist:
            return raw_tools
        kept, dropped = [], []
        for t in raw_tools:
            name = _extract_tool_name(t) or ""
            # Gateway tools arrive prefixed (`<target>___<tool>`); match on either
            # the full name or the unprefixed tool name so the allowlist does not
            # have to encode a target name that can change.
            #
            # Split ONCE. The tool name can itself contain `___` -- the AWS MCP
            # Server namespaces its own tools, so the full name is
            # `<target>___aws___search_documentation` and splitting on every
            # separator would yield `search_documentation`, silently matching
            # nothing and (fail-closed) dropping every tool.
            bare = name.split(_GATEWAY_NAMESPACE_SEPARATOR, 1)[-1]
            if name in self.tool_allowlist or bare in self.tool_allowlist:
                kept.append(t)
            else:
                dropped.append(name)
        if dropped:
            logger.info(
                "Gateway '%s': allowlist kept %d tool(s), dropped %d: %s",
                self.name, len(kept), len(dropped), sorted(dropped)[:12],
            )
        return kept

    def _withhold_tools(self, raw_tools: list) -> list:
        """Drop every tool whose name begins with a withheld hybrid-search prefix (Req 5.4, Component 8).

        Runs at the same points as :meth:`_apply_tool_allowlist` (chained after it in ``connect`` and
        ``refresh_tools``) so ``self._all_tools`` — and therefore every downstream consumer: tool
        metadata, search, trusted names, job detection, ``list_tools`` — never sees a withheld tool.

        Unlike the allowlist this is UNCONDITIONAL (there is no "no filter" case) and applies to EVERY
        ``GatewayConnection``, not only the M2C row: the hybrid-search tools are Projects-only now and
        must not be exposed to the model on any gateway. Matching is by PREFIX against both the full name
        and the name after ONE ``___`` split (gateway tools arrive as ``<target>___<tool>``, and a tool
        name can itself carry a ``___`` separator), so a namespaced ``tgt___hybrid-search___search_content``
        and a bare ``hybrid-search___search_content`` are both caught, and tools M2C publishes later under
        these prefixes are covered automatically.
        """
        if not WITHHELD_TOOL_PREFIXES:
            return raw_tools
        kept, dropped = [], []
        for t in raw_tools:
            name = _extract_tool_name(t) or ""
            # Split ONCE: a tool name can contain `___` itself (`tgt___aws___search_documentation`), so
            # splitting on every separator would drop the target prefix twice and mismatch.
            bare = name.split(_GATEWAY_NAMESPACE_SEPARATOR, 1)[-1]
            if any(name.startswith(p) or bare.startswith(p) for p in WITHHELD_TOOL_PREFIXES):
                dropped.append(name)
            else:
                kept.append(t)
        if dropped:
            logger.info(
                "Gateway '%s': withheld %d hybrid-search tool(s): %s",
                self.name, len(dropped), sorted(dropped)[:12],
            )
        return kept

    def list_tools(self) -> list[dict]:
        """Return tool metadata with state: not_added, available, or trusted."""
        result = []
        for m in self._tool_metadata:
            name = m["name"]
            if name in self._trusted:
                state = "trusted"
            elif name in self._added:
                state = "available"
            else:
                state = "not_added"
            result.append({"name": name, "description": m["description"], "state": state})
        return result

    def add_tool(self, tool_name: str) -> bool:
        known = {m["name"] for m in self._tool_metadata}
        if tool_name not in known:
            return False
        self._added.add(tool_name)
        return True

    def remove_tool(self, tool_name: str) -> bool:
        if tool_name not in self._added:
            return False
        self._added.discard(tool_name)
        self._trusted.discard(tool_name)
        return True

    def trust_tool(self, tool_name: str) -> bool:
        if tool_name not in self._added:
            return False
        self._trusted.add(tool_name)
        return True

    def untrust_tool(self, tool_name: str) -> bool:
        if tool_name not in self._trusted:
            return False
        self._trusted.discard(tool_name)
        return True

    def get_added_tools(self) -> list:
        """Return raw tool objects for all added tools (for agent registry)."""
        return [t for t in self._all_tools if _extract_tool_name(t) in self._added]

    def get_trusted_tool_names(self) -> set[str]:
        return set(self._trusted)

    def get_all_tools(self) -> list:
        return list(self._all_tools)

    def search_tools(self, query: str) -> list:
        if not self._search_available or not self._connected or not self._client:
            return []
        try:
            result = self._client.call_tool_sync(
                f"search-{uuid.uuid4().hex}",
                SEARCH_TOOL_NAME,
                {"query": query},
            )
            if hasattr(result, "content"):
                for block in result.content:
                    if hasattr(block, "text"):
                        try:
                            payload = json.loads(block.text)
                        except (json.JSONDecodeError, TypeError):
                            return [{"raw": block.text}]
                        return _search_hits(payload)
            return []
        except Exception as e:
            logger.error("Gateway '%s' search failed: %s", self.name, e)
            return []


class MCPManager:
    """Manages multiple Gateway connections."""

    def __init__(self):
        self._gateways: dict[str, GatewayConnection] = {}
        self._agent: Optional[Agent] = None
        # ---- async-job-tracking per-tool wrapper state ----
        # Set by :meth:`set_job_tracking`; consumed by
        # :meth:`_apply_job_tracking_wrap` after every successful
        # connect / refresh. Kept ``None`` in dev environments without
        # the registry table; in that case the wrap step is a no-op
        # and Strands sees the unwrapped MCPAgentTools (same behaviour
        # as before async-job-tracking landed). See
        # :mod:`agent.src.jobs.strands_tool_wrap` for the rationale —
        # we wrap each trio-member ``MCPAgentTool`` with a subclass
        # whose ``stream`` performs the registry write / results gate
        # because Strands' ``Agent`` invokes ``MCPAgentTool.stream``
        # directly, leaving any manager-level chokepoint unreachable.
        self._job_registry: Optional["JobRegistry"] = None
        self._job_actor_id_resolver: Optional[Callable[[], str]] = None
        self._job_session_id_resolver: Optional[Callable[[], str]] = None

    def set_agent(self, agent: Agent) -> None:
        self._agent = agent

    async def _build_connection_for_descriptor(
        self,
        descriptor: "GatewayDescriptor",
        secrets_client: Any = None,
        actor_id: str = "",
        user_token: str = "",
    ) -> Optional[GatewayConnection]:
        """Materialize a :class:`GatewayConnection` for one descriptor.

        Shared between :meth:`load_gateways_from_registry` (cold-load,
        task 3.1) and :meth:`register_gateway` (live admin add, task
        3.2). Owns the R2 secret-resolution boundary: descriptors carry
        only ``client_secret_arn``, this method resolves it against
        Secrets Manager and merges the literal ``client_secret`` into
        the connection config that :class:`GatewayConnection` actually
        consumes.

        Returns ``None`` when the secret cannot be resolved so the
        caller can decide whether to skip (cold-load) or surface the
        error (admin action).
        """
        cfg = descriptor.to_connection_config()
        if actor_id:
            cfg["actor_id"] = actor_id
        if user_token:
            cfg["user_token"] = user_token
        # One resolution boundary for every variant that references secret
        # material by ARN: map the ARN field to the literal field that
        # GatewayConnection consumes. Every candidate is popped whether or
        # not it is set, so no ARN leaks through into the connection config.
        secret_arn: Optional[str] = None
        literal_key: Optional[str] = None
        for arn_field, target_field in (
            ("client_secret_arn", "client_secret"),
            ("token_arn", "bearer_token"),
        ):
            candidate = cfg.pop(arn_field, None)
            if candidate and secret_arn is None:
                secret_arn, literal_key = candidate, target_field
        if secret_arn:
            # Cross-region secret resolution: a federated gateway's secret can
            # live in a different region than the runtime's default (e.g. the
            # OMS Core M2M secret is in us-east-1 while this harness runs in
            # us-west-2). A default-region `boto3.client("secretsmanager")`
            # cannot GetSecretValue an out-of-region ARN, so derive the region
            # from the ARN (`arn:aws:secretsmanager:<region>:...`) and build a
            # region-pinned client. An explicitly injected client wins so tests
            # can supply their own; a malformed ARN falls back to the default.
            if secrets_client is not None:
                sm_client = secrets_client
            else:
                region = None
                parts = secret_arn.split(":")
                if len(parts) >= 4 and parts[3]:
                    region = parts[3]
                sm_client = (
                    boto3.client("secretsmanager", region_name=region)
                    if region
                    else boto3.client("secretsmanager")
                )
            try:
                cfg[literal_key] = await asyncio.to_thread(
                    _fetch_secret_string, sm_client, secret_arn
                )
            except Exception as e:
                logger.error(
                    "Gateway '%s': failed to resolve %s from %s: %s",
                    descriptor.id, literal_key, secret_arn, e,
                )
                return None
        return GatewayConnection(descriptor.id, cfg)

    async def load_gateways_from_registry(
        self,
        registry: "GatewayRegistry",
        secrets_client: Any = None,
        actor_id: str = "",
        user_token: str = "",
    ) -> list[str]:
        """Cold-load every enabled gateway from the registry (R6, R8).

        The canonical startup loader after task 6.3 retired the legacy
        ``GATEWAYS`` env var path. Flow:

        1. ``registry.list_enabled()`` returns the enabled descriptors.
        2. For each descriptor, build the connection-config dict via
           :meth:`GatewayDescriptor.to_connection_config`.
        3. For JWT descriptors that carry a ``client_secret_arn``,
           resolve the literal secret from Secrets Manager **at this
           layer** (R2 — the descriptor never holds the raw secret) and
           merge it into the config under ``client_secret`` while
           popping the ARN so :class:`GatewayConnection` sees only the
           shape it already understands.
        4. Instantiate one :class:`GatewayConnection` per descriptor and
           install it in :attr:`_gateways`. Existing entries with the
           same id are replaced — call sites that need a graceful
           handoff should call :meth:`unregister_gateway` first
           (task 3.2).

        Connect-time work (``connect_all`` / per-gateway OAuth) stays in
        the caller, mirroring the env-var path.

        Args:
            registry: The :class:`GatewayRegistry` to read from.
            secrets_client: Optional boto3 ``secretsmanager`` client. When
                omitted, each descriptor that needs secret resolution builds
                its own region-pinned client (region derived from the secret
                ARN) so cross-region federated secrets resolve correctly.

        Returns:
            The list of gateway ids loaded into :attr:`_gateways`.
        """
        descriptors = await registry.list_enabled()
        loaded: list[str] = []
        # Do NOT eagerly build a single shared default-region Secrets Manager
        # client here. Federated gateways can carry secrets in different regions
        # (e.g. OMS Core's secret is in us-east-1 while the runtime defaults to
        # us-west-2), so a shared default-region client would fail to resolve
        # out-of-region ARNs. When a client is injected (tests) we pass it
        # through unchanged; otherwise we pass `None` so each descriptor derives
        # its own region-appropriate client in `_build_connection_for_descriptor`.
        #
        # Secret resolution is one Secrets Manager round trip per descriptor, each
        # already on a worker thread; resolve them concurrently (gather keeps the
        # results in descriptor order, so `_gateways` is filled in registry order
        # exactly as before). A descriptor that fails resolves to None, not raise.
        connections = await asyncio.gather(*(
            self._build_connection_for_descriptor(
                descriptor, secrets_client=secrets_client, actor_id=actor_id,
                user_token=user_token,
            )
            for descriptor in descriptors
        ), return_exceptions=True)
        for descriptor, connection in zip(descriptors, connections):
            if isinstance(connection, BaseException):
                # Same outcome as the sequential loop: descriptors before the
                # failing one are installed, then the error propagates.
                raise connection
            if connection is None:
                # Skip this descriptor rather than instantiating a
                # connection that will fail at OAuth time with a
                # less obvious error.
                continue

            self._gateways[descriptor.id] = connection
            loaded.append(descriptor.id)

        logger.info("Loaded %d gateway(s) from registry", len(loaded))
        return loaded

    async def load_platform_gateway_from_env(
        self, *, actor_id: str = "", user_token: str = "",
    ) -> Optional[str]:
        """Connect the shared Platform gateway (AWS_IAM / SigV4, plane=platform) from env.

        Wave 4: the Platform gateway is provisioned in CDK behind AWS_IAM inbound auth,
        but it is NOT a registry row, so :meth:`load_gateways_from_registry` never connects
        it. This wires it in ADDITIVELY from ``HARNESS_PLATFORM_GATEWAY_ID`` +
        ``HARNESS_PLATFORM_GATEWAY_URL``, authenticated by the runtime's own IAM identity via
        per-request SigV4 (no JWT, no secret, no interceptor — shared platform tools are
        governed by IAM plus the gateway's Cedar policy engine, never per-user ownership).

        Safe + inert until a shared target is attached: an empty Platform gateway simply
        discovers zero tools. No-op when the env is unset (unprovisioned envs). ``user_token``
        is accepted for signature symmetry but unused — SigV4 uses the runtime role, not a JWT.
        Returns the loaded gateway id, or ``None``.
        """
        gid = os.environ.get("HARNESS_PLATFORM_GATEWAY_ID", "").strip()
        url = os.environ.get("HARNESS_PLATFORM_GATEWAY_URL", "").strip()
        if not (gid and url):
            return None
        from gateway.registry import GatewayDescriptor, SigV4AuthConfig
        region = os.environ.get("AWS_REGION", "us-east-1")
        descriptor = GatewayDescriptor(
            id=gid, name="platform-gateway", url=url, auth_type="sigv4",
            auth_config=SigV4AuthConfig(region=region, service="bedrock-agentcore"),
            plane="platform",
        )
        connection = await self._build_connection_for_descriptor(
            descriptor, actor_id=actor_id, user_token=user_token,
        )
        if connection is None:
            return None
        self._gateways[gid] = connection
        logger.info("Loaded platform gateway '%s' (SigV4, plane=platform)", gid)
        return gid

    async def register_gateway(
        self,
        descriptor: "GatewayDescriptor",
        secrets_client: Any = None,
    ) -> GatewayConnection:
        """Bring a single gateway online without restarting the runtime (R4).

        Counterpart to :meth:`load_gateways_from_registry`'s cold-load,
        but for the admin-driven path: the ``gateway_register`` action
        (task 5.3) writes the descriptor to DDB, then calls this to
        install the live connection. Flow:

        1. Build the connection config and resolve any
           ``client_secret_arn`` against Secrets Manager via the shared
           helper. R2 keeps secret resolution out of the descriptor.
        2. Connect synchronously (the underlying OAuth + ``tools/list``
           call is blocking; wrapped in :func:`asyncio.to_thread` so the
           AgentCore request handler stays cooperative).
        3. Replace any existing entry under ``descriptor.id``. The admin
           edit flow (task 5.4) calls :meth:`unregister_gateway` first
           for a graceful handoff, but we still tolerate an in-place
           swap so the method is safe to call from request handlers
           that don't track the previous state.

        Returns:
            The live :class:`GatewayConnection`. Already inserted into
            :attr:`_gateways` by the time this returns.

        Raises:
            RuntimeError: When the secret cannot be resolved or the
                connection's OAuth/``tools/list`` handshake fails. The
                runtime action layer maps this to a structured error
                response.
        """
        connection = await self._build_connection_for_descriptor(
            descriptor, secrets_client=secrets_client
        )
        if connection is None:
            # `_build_connection_for_descriptor` already logged the
            # underlying boto error; surface a typed failure so the
            # admin action can return `{"error": "secret_resolve_failed"}`
            # rather than a generic 500.
            raise RuntimeError(
                f"Gateway '{descriptor.id}': failed to resolve client_secret"
            )

        # Connect on a worker thread — `GatewayConnection.connect` is
        # synchronous (httpx + MCP context manager) and we don't want
        # to block the event loop while a fresh OAuth flow runs. If
        # `connect()` raises, propagate it: we haven't inserted into
        # `_gateways` yet, and `connect()` already cleaned up its own
        # MCP client on failure.
        await asyncio.to_thread(connection.connect)

        self._gateways[descriptor.id] = connection
        logger.info("Gateway '%s': registered live", descriptor.id)
        return connection

    async def unregister_gateway(self, id: str) -> bool:
        """Take a gateway offline without restarting the runtime (R4).

        Idempotent: returns ``True`` when a connection was removed,
        ``False`` when no entry existed under ``id``. Best-effort
        disconnect — :meth:`GatewayConnection.disconnect` already
        swallows close errors internally, but we belt-and-brace it here
        so a flaky underlying transport can't block the registry from
        evicting the entry.

        Safe to call from request handlers (admin edit / delete flows,
        tasks 5.4) and from the cold-load path's "swap" semantics.
        """
        gw = self._gateways.pop(id, None)
        if gw is None:
            return False
        try:
            # `disconnect()` returns False when the connection was never
            # established; that's fine — we still want to drop the entry.
            await asyncio.to_thread(gw.disconnect)
        except Exception as e:
            logger.warning("Gateway '%s' disconnect error during unregister: %s", id, e)
        logger.info("Gateway '%s': unregistered", id)
        return True

    def _connect_one(self, gw_id: str, gw: "GatewayConnection") -> dict:
        """One gateway's step of :meth:`connect_all`. Never raises."""
        if gw.awaiting_user_token():
            # Expected on tokenless management calls. Skip quietly; the
            # connection is retried by bind_user_token_and_connect as soon as
            # an invocation carries a token.
            logger.debug(
                "Gateway '%s': deferred, awaiting the caller's token", gw_id
            )
            return {"status": "deferred", "reason": "awaiting user token"}
        try:
            result = gw.connect()
            # async-job-tracking: re-wrap trio members on every
            # successful connect. No-op when set_job_tracking has
            # not been called.
            self._apply_job_tracking_wrap(gw)
            return result
        except Exception as e:
            logger.error("Failed to connect gateway '%s': %s", gw_id, e)
            return {"status": "error", "error": str(e)}

    def connect_all(self) -> dict:
        """Connect every registered gateway, CONCURRENTLY, and report per gateway.

        Each connect is an independent OAuth + MCP initialize + paginated
        ``tools/list`` round trip against its own :class:`GatewayConnection`
        (its own lock, client, tool list and Job_Detector cache), so they run on
        worker threads side by side: four gateways measured 7.5s back to back on
        a cold staging session, i.e. the SUM of the connects, where the slowest
        one alone is the floor.

        Semantics are unchanged: the result map is keyed in registry order (the
        order ``_gateways`` was loaded, which is what every caller iterates), a
        failing gateway is reported as ``error`` without affecting the others,
        and the job-tracking wrap is applied per gateway right after its own
        connect. Each worker runs in a COPY of the caller's contextvars, because
        Strands' MCPClient seeds its transport thread from the context current
        at ``__enter__`` — a bare pool thread would hand it an empty one.
        """
        items = list(self._gateways.items())
        if len(items) <= 1:
            return {gw_id: self._connect_one(gw_id, gw) for gw_id, gw in items}
        import contextvars
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(
            max_workers=min(_CONNECT_MAX_PARALLEL, len(items)),
            thread_name_prefix="gw-connect",
        ) as pool:
            futures = [
                (gw_id, pool.submit(contextvars.copy_context().run, self._connect_one, gw_id, gw))
                for gw_id, gw in items
            ]
            # Collected in submission (= registry) order, not completion order.
            return {gw_id: fut.result() for gw_id, fut in futures}

    def bind_user_token_and_connect(self, user_token: str) -> dict:
        """Bind the current user token to each ``user_jwt`` gateway transport.

        MCPClient headers are immutable after connect. A fresh browser token
        therefore has to rotate a live connection immediately; assigning it to
        ``gw.user_token`` alone leaves the old bearer on the wire. Rotation is
        serialized with delegated reconnects by the per-gateway lock and occurs
        only when the token actually bound to the transport differs.

        Returns a per-gateway result map, empty when there is nothing to do.
        """
        if not user_token:
            return {}
        if _user_token_is_expired(user_token):
            # Binding a lapsed bearer can only produce a front-door 403 insufficient_scope.
            # Leave the transport alone (a dispatched sub-agent should be on a delegation grant
            # instead — see orchestration/dispatch_credentials.py).
            logger.warning("bind_user_token_and_connect: refusing to bind an expired user token")
            return {}
        results: dict = {}
        for gw_id, gw in self._gateways.items():
            if gw.auth_type != "user_jwt":
                continue
            with gw._connect_lock:
                bound_user_token = getattr(gw, "_bound_user_token", "")
                if gw.is_connected and bound_user_token == user_token:
                    gw.user_token = user_token
                    continue

                with gw._auth_transition():
                    if gw.is_connected:
                        logger.info(
                            "Gateway '%s': rotating live session to the current user token",
                            gw_id,
                        )
                        try:
                            gw.disconnect()
                        except Exception as e:  # noqa: BLE001 — reconnect below is authoritative
                            logger.warning(
                                "Gateway '%s': disconnect before user-token rotation failed: %s",
                                gw_id,
                                e,
                            )

                    # Select interactive mode explicitly. A stale grant left by an
                    # earlier detached task must not survive this auth transition.
                    gw.user_token = user_token
                    gw.delegation_grant = ""
                    try:
                        results[gw_id] = gw.connect()
                        self._apply_job_tracking_wrap(gw)
                        logger.info("Gateway '%s': connected with current user token", gw_id)
                    except Exception as e:
                        logger.error("Gateway '%s': connect with user token failed: %s", gw_id, e)
                        results[gw_id] = {"status": "error", "error": str(e)}
        return results

    def bind_delegation_grant_and_connect(
        self,
        grant: str,
        *,
        token_endpoint: str,
        client_id: str,
        client_secret: str,
        scope: str = "",
    ) -> dict:
        """Connect ``user_jwt`` gateways for an async (grant-bearing) plan node.

        The async counterpart to :meth:`bind_user_token_and_connect`. A plan
        node runs long after the request that created it, with no user JWT, so
        instead of the caller's token it crosses the gateway front door with a
        machine (client_credentials) token minted against the end-user pool's
        async-plan client and presents the signed ``grant`` as
        ``DELEGATION_GRANT_HEADER``. The interceptor verifies the grant with KMS
        and derives the actor FROM THE GRANT — the machine token has no user
        ``sub`` (option E's entire premise).

        Credential precedence is explicit:

        * Only ``user_jwt`` gateways are touched.
        * A supplied grant always wins for detached work. A warm interactive
          connection is replaced even when its latest ``gw.user_token`` field
          looks valid, because that field may not match the static bearer bound
          to the live MCPClient.
        * A live same-grant delegated connection is reused only while its
          renewable machine-token cache is before the refresh deadline. A
          different grant, expired machine credential, or dead session forces
          one serialized rebuild.

        No-op (returns ``{}``) when the grant or any machine credential is
        missing, so a dev environment without the async-plan client simply
        leaves ``user_jwt`` gateways deferred — the pre-option-E behavior.
        """
        if not (grant and client_id and client_secret and token_endpoint):
            return {}
        results: dict = {}
        for gw_id, gw in self._gateways.items():
            if gw.auth_type != "user_jwt":
                continue
            # Serialize the whole check-then-act on THIS gateway so a fan-out of
            # concurrent async-plan nodes collapses to ONE rebuild: the first node
            # reconnects; each waiter then re-evaluates the reuse test below with the
            # lock held and REUSES the freshly-built session instead of racing another
            # teardown (the 23f1d54b thundering-herd that failed all 16 submit nodes).
            with gw._connect_lock:
                # A delegation grant is the authority for this detached task,
                # even if the warm runtime also remembers a browser token. The
                # staging failure in session bbdcceab exposed why: at 01:01 a
                # fresh browser token updated ``gw.user_token``, but the live
                # MCPClient still carried its 17:35 static bearer. The old guard
                # inspected the fresh field, declared the connection valid, and
                # sent all 16 plan calls through the expired transport token.
                # Always transition grant-bearing work to machine+grant mode.
                bound_user_token = getattr(gw, "_bound_user_token", "")
                switching_from_user = bool(gw.user_token or bound_user_token)
                if switching_from_user:
                    logger.info(
                        "Gateway '%s': switching from user-token transport to "
                        "delegation grant (detached task)",
                        gw_id,
                    )

                # Reuse a live, same-grant delegated session instead of tearing it
                # down every node. The unconditional per-node teardown+reconnect
                # below churned the shared, warm-container MCP session and produced
                # intermittent "the client session was not initialized" errors; 3 of
                # those trip the circuit breaker (threshold 3), which then gates the
                # rest of a fan-out for the cooldown (session 05a6ff2f: 8-frame image
                # edit -- breaker opened, then re-gated the staggered frames, ~140
                # refusals, one frame lost). Reusing a live same-grant session cuts
                # that churn at the root. Re-evaluated INSIDE the lock so a waiter
                # sees the peer's fresh session and reuses it.
                #
                # SAFETY (why reuse is now sound, though it once looped the plan):
                # the risk was a streamable-HTTP session the gateway idle-closed
                # server-side that still reads as "alive" via the local
                # ``_is_session_active`` probe, getting reused so the next tool call
                # dies with "the client session is not running". That is now
                # backstopped by the resilient tool wrapper
                # (``jobs.strands_tool_wrap``): a session-down error on the first
                # round-trip triggers a reconnect + retry ONCE, so a stale reuse
                # self-heals instead of failing the node. We still reconnect fresh
                # when the grant differs (different actor/plan) or no live session
                # exists.
                machine_refresh_due = (
                    getattr(gw, "_token_expires_at", None) is not None
                    and time.time() >= gw._token_expires_at
                )
                if (
                    gw.is_connected
                    and not switching_from_user
                    and gw.delegation_grant == grant
                    and gw._session_alive()
                    and not machine_refresh_due
                ):
                    self._apply_job_tracking_wrap(gw)
                    results[gw_id] = {"status": "already_connected", "reused": True}
                    logger.info(
                        "Gateway '%s': reusing live delegated session (async plan)",
                        gw_id,
                    )
                    continue
                with gw._auth_transition():
                    if machine_refresh_due:
                        logger.info(
                            "Gateway '%s': delegated machine token reached its refresh "
                            "deadline; rebuilding the MCP session",
                            gw_id,
                        )
                    if gw.is_connected:
                        try:
                            gw.disconnect()
                        except Exception as e:  # noqa: BLE001 — best-effort teardown
                            logger.warning(
                                "Gateway '%s': disconnect before delegated reconnect "
                                "failed: %s", gw_id, e,
                            )
                    gw.set_delegated_machine_auth(
                        grant=grant,
                        token_endpoint=token_endpoint,
                        client_id=client_id,
                        client_secret=client_secret,
                        scope=scope,
                    )
                    try:
                        results[gw_id] = gw.connect()
                        self._apply_job_tracking_wrap(gw)
                        logger.info(
                            "Gateway '%s': connected with delegation grant (async plan)",
                            gw_id,
                        )
                    except Exception as e:
                        logger.error(
                            "Gateway '%s': delegated connect failed: %s", gw_id, e
                        )
                        results[gw_id] = {"status": "error", "error": str(e)}
        return results

    def get_gateway(self, gw_id: str) -> Optional[GatewayConnection]:
        """Return the live :class:`GatewayConnection` for ``gw_id`` (or ``None``).

        Public accessor so collaborators that need to drive a specific gateway's
        MCP tools directly — e.g. the OMS workflow-persistence ``OMSMcpClient``
        adapter, which invokes the six OMS Core MCP tools through the connection's
        ``MCPClient.call_tool_sync`` — can resolve the connection without reaching
        into the private ``_gateways`` map.
        """
        return self._gateways.get(gw_id)

    def find_gateway_for_tool(self, tool_name: str) -> Optional[GatewayConnection]:
        """Return the SINGLE connected gateway exposing ``tool_name``, or None.

        Resolves on what each connection reported from ``tools/list`` rather than
        parsing the ``target___tool`` prefix (the prefix names a TARGET, not a
        gateway). If TWO gateways expose the exact same tool name the binding is
        ambiguous, so this fails CLOSED (returns None) rather than routing to
        whichever happens to be first — silently picking one could send a call
        to the wrong trust plane (Property 13 / Req 13.10-13.11).
        """
        matches = [
            gw
            for gw in self._gateways.values()
            if any(meta.get("name") == tool_name for meta in gw.tool_metadata)
        ]
        if len(matches) > 1:
            logger.warning(
                "Tool %r is exposed by %d gateways (%s); refusing to route "
                "ambiguously — fail-closed.",
                tool_name, len(matches), [gw.name for gw in matches],
            )
            return None
        return matches[0] if matches else None

    def call_tool(self, tool_name: str, arguments: dict) -> Any:
        """Invoke a tool on whichever registry gateway exposes it.

        Counterpart to how the Lab's tool *listing* already works. Listing was
        moved onto the registry-backed manager to fix BUG-3 -- federated tools
        appeared in the catalog but failed validation because the validator only
        knew the single ``GATEWAY_ENDPOINTS`` URL. Invocation never got the same
        treatment, so calling a federated tool still went to the wrong gateway
        and, once that gateway's inbound auth stopped being machine-to-machine,
        stopped working at all.

        Raises:
            RuntimeError: When no connected gateway exposes the tool, which is
                a clearer failure than sending it to an arbitrary endpoint.
        """
        gw = self.find_gateway_for_tool(tool_name)
        if gw is None:
            raise RuntimeError(
                f"No connected gateway exposes tool {tool_name!r}"
            )
        if not gw.is_connected:
            gw.connect()
        return gw.call_tool_sync(tool_name, arguments)

    def connect_gateway(self, gw_id: str) -> dict:
        gw = self._gateways.get(gw_id)
        if not gw:
            return {"error": f"Unknown gateway: {gw_id}"}
        result = gw.connect()
        self._apply_job_tracking_wrap(gw)
        return result

    def disconnect_gateway(self, gw_id: str) -> dict:
        gw = self._gateways.get(gw_id)
        if not gw:
            return {"error": f"Unknown gateway: {gw_id}"}
        removed = gw.disconnect()
        return {"status": "disconnected" if removed else "not_connected"}

    def refresh_gateway(self, gw_id: str) -> dict:
        gw = self._gateways.get(gw_id)
        if not gw:
            return {"error": f"Unknown gateway: {gw_id}"}
        result = gw.refresh_tools()
        # Tool list may have changed — re-wrap.
        self._apply_job_tracking_wrap(gw)
        return result

    def search_gateway(self, gw_id: str, query: str) -> list:
        gw = self._gateways.get(gw_id)
        if not gw:
            return []
        return gw.search_tools(query)

    def list_gateways(self) -> list[dict]:
        return [
            {
                "id": gw.id,
                "name": gw.name,
                "connected": gw.is_connected,
                "auth_type": gw.auth_type,
                "tools": gw.list_tools(),
                "search_available": gw._search_available,
                "tool_count": len(gw._tool_metadata),
            }
            for gw in self._gateways.values()
        ]

    # --- Tool state management (across all gateways) ---

    def add_tool(self, tool_name: str) -> dict:
        for gw in self._gateways.values():
            if gw.add_tool(tool_name):
                return {"name": tool_name, "state": "available"}
        return {"error": f"Unknown tool: {tool_name}"}

    def remove_tool(self, tool_name: str) -> dict:
        for gw in self._gateways.values():
            if gw.remove_tool(tool_name):
                return {"name": tool_name, "state": "not_added"}
        return {"error": f"Tool not added: {tool_name}"}

    def trust_tool(self, tool_name: str) -> dict:
        for gw in self._gateways.values():
            if gw.trust_tool(tool_name):
                return {"name": tool_name, "state": "trusted"}
        return {"error": f"Tool not added (add first): {tool_name}"}

    def untrust_tool(self, tool_name: str) -> dict:
        for gw in self._gateways.values():
            if gw.untrust_tool(tool_name):
                return {"name": tool_name, "state": "available"}
        return {"error": f"Tool not trusted: {tool_name}"}

    def get_all_added_tools(self) -> list:
        """Raw tool objects for tools explicitly added on the agent, across gateways.

        Empty in the chat path BY DESIGN: ``connect()`` resets ``self._added`` on
        every connect, and the only caller of ``add_tool`` is the admin endpoint
        (main.py ``mcp_add_tool``). The orchestrator registers ZERO gateway tools —
        the catalog lives in the system prompt (``_format_catalog_text``) so the LLM
        names tools to ``dispatch_tool`` — so this returns ``[]`` there. It is
        non-empty only after an admin ``mcp_add_tool``, whose ``_rebuild_agent_tools``
        is the sole consumer.
        """
        tools = []
        for gw in self._gateways.values():
            if gw.is_connected:
                tools.extend(gw.get_added_tools())
        return tools

    def find_tools(self, predicate) -> list:
        """Wrapped tool objects, across connected gateways, whose name satisfies ``predicate``.

        For the few gateway tools the orchestrator binds DIRECTLY instead of naming them to ``dispatch_tool``
        (the managed WebSearch tool: a quick read-only lookup a sub-agent round trip only slows down).
        """
        out = []
        for gw in self._gateways.values():
            if gw.is_connected:
                out.extend(t for t in gw.get_all_tools() if predicate(_extract_tool_name(t)))
        return out

    def get_all_trusted_tool_names(self) -> set[str]:
        """Return names of all trusted tools across gateways."""
        names: set[str] = set()
        for gw in self._gateways.values():
            if gw.is_connected:
                names.update(gw.get_trusted_tool_names())
        return names

    def get_job_trios(self) -> list[JobTrio]:
        """Flatten the cached :class:`JobTrio` records across gateways.

        Surfaces the per-gateway :class:`DetectionResult` cache that
        :meth:`GatewayConnection._run_job_detection` populates on every
        connect / refresh. Used by the ``/catalog`` endpoint (task 12.x)
        and by :func:`agent.src.jobs.strands_tool_wrap.wrap_trio_tools`
        so neither callsite needs to reach into ``GatewayConnection``
        directly.

        Skips gateways that have no detection result yet (never
        connected, mid-disconnect, or detector failure logged in
        ``_run_job_detection``). Each emitted :class:`JobTrio` already
        carries the owning ``gateway_id`` stamp from the detector.
        """
        trios: list[JobTrio] = []
        for gw in self._gateways.values():
            detection = gw.job_detection
            if detection is None:
                continue
            trios.extend(detection.trios)
        return trios

    # ------------------------------------------------------------------
    # async-job-tracking — per-tool wrapper (the path Strands actually
    # takes).
    #
    # Why this is the canonical hook: Strands' ``Agent`` dispatches MCP
    # tools via :class:`MCPAgentTool.stream` directly, bypassing any
    # manager-level dispatch chokepoint. The wrap below replaces the
    # ``MCPAgentTool`` instance for each trio member with a subclass
    # whose ``stream`` performs the registry write (start tools) or
    # the results gate (results tools) before / after delegating to
    # the underlying MCP call. See :mod:`agent.src.jobs.strands_tool_wrap`
    # for the full rationale and the ``JobTrackingMCPAgentTool``
    # implementation.
    # ------------------------------------------------------------------

    def set_job_tracking(
        self,
        registry: "JobRegistry",
        *,
        actor_id_resolver: Callable[[], str],
        session_id_resolver: Callable[[], str],
    ) -> None:
        """Enable per-tool job tracking on every connected gateway.

        Stores the registry / resolvers on the manager so subsequent
        connect / refresh cycles re-apply the wrap automatically (the
        wrap is idempotent — see
        :func:`agent.src.jobs.strands_tool_wrap.wrap_trio_tools`),
        then walks every already-connected gateway and applies the
        wrap once for the current tool list.

        No-op when ``registry`` is ``None`` — kept callable from the
        bootstrap path so dev environments without
        ``JOB_REGISTRY_TABLE`` set don't have to branch around the
        call.
        """
        if registry is None:
            logger.info(
                "MCPManager.set_job_tracking: registry is None; "
                "per-tool job-tracking wrap will not be applied"
            )
            return

        self._job_registry = registry
        self._job_actor_id_resolver = actor_id_resolver
        self._job_session_id_resolver = session_id_resolver

        # Apply the wrap to every gateway that's already connected.
        # Newly-connecting gateways pick up the wrap via the
        # connect/refresh hooks in ``connect_all`` /
        # ``connect_gateway`` / ``refresh_gateway``.
        wrapped_count = 0
        for gw in self._gateways.values():
            if gw.is_connected:
                self._apply_job_tracking_wrap(gw)
                wrapped_count += 1
        logger.info(
            "MCPManager: per-tool job-tracking wrap enabled "
            "(applied to %d already-connected gateway(s))",
            wrapped_count,
        )

    def _apply_job_tracking_wrap(self, gw: "GatewayConnection") -> None:
        """Replace trio-member tools on ``gw`` with tracking proxies.

        Called after every successful ``connect`` / ``refresh_tools``
        on each gateway. No-op when:

        * :meth:`set_job_tracking` has not been called (dev path
          without ``JOB_REGISTRY_TABLE``);
        * the gateway has no detection result (fresh disconnect, or
          detector failure logged by ``_run_job_detection``);
        * the detection result has zero trios (gateway has no
          job-bearing tools).

        The wrap mutates ``gw._all_tools`` in place — that list is
        the canonical source the catalog endpoint and
        :meth:`_all_gateway_tools_by_name` (used by
        ``task_agent.py`` / ``main.py``) read from when building the
        Strands ``Agent(tools=...)`` argument. By the time the agent
        loop sees the tool list, trio members are already wrapped.
        """
        if self._job_registry is None:
            return
        if gw.job_detection is None:
            return
        if not gw.job_detection.trios:
            return

        # Lazy import to keep the wrap module out of the import graph
        # for callers that never enable job tracking.
        from jobs.strands_tool_wrap import wrap_trio_tools

        try:
            gw._all_tools = wrap_trio_tools(
                gw._all_tools,
                gw.job_detection,
                registry=self._job_registry,
                actor_id_resolver=self._job_actor_id_resolver,  # type: ignore[arg-type]
                session_id_resolver=self._job_session_id_resolver,  # type: ignore[arg-type]
                reconnect=(
                    gw.reconnect_for_current_auth()
                    if hasattr(gw, "reconnect_for_current_auth")
                    else gw.reconnect
                ),
                call_lease=getattr(gw, "call_lease", None),
                relink=(
                    gw.relink_for_current_auth()
                    if hasattr(gw, "relink_for_current_auth")
                    else None
                ),
            )
            logger.info(
                "Gateway '%s': per-tool job-tracking wrap applied "
                "to %d trio(s)",
                gw.name, len(gw.job_detection.trios),
            )
        except Exception as e:  # noqa: BLE001 — defensive
            # Strict-additive: if the wrap fails, leave the tools
            # untouched. The agent still works; job tracking just
            # silently doesn't fire for this gateway's trios.
            logger.error(
                "Gateway '%s': per-tool job-tracking wrap FAILED: %s — "
                "leaving tools unwrapped (job tracking inactive for "
                "this gateway)",
                gw.name, e,
            )

    def shutdown(self) -> None:
        for gw in self._gateways.values():
            gw.disconnect()
