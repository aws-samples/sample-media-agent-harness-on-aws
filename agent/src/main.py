"""Strands agent on Bedrock AgentCore — orchestrator with tools, skills, memory, and sub-agent dispatch.

Startup strategy: register the BedrockAgentCoreApp (and its /ping endpoint)
BEFORE importing heavy libraries. This ensures the runtime reports Healthy
within the platform's 30-second init window even if downstream imports are
slow on a cold microVM. Importing this module constructs nothing. The
user-independent objects (boto clients, model, registries, bundled skills,
model catalog) are built by ``warm_up()`` just before ``app.run()``, within a
time budget, so AgentCore Runtime V2 captures them in its snapshot; the rest
(and all of it, when warm-up did not run) is built by
``_init_agent_subsystems()`` on the first invocation.
"""
import asyncio
import json, logging, os, base64, sys, threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# --- CRITICAL: alias this module under the name "main" ---
# The AgentCore runtime launches this file as a script
# (``opentelemetry-instrument main.py``), so Python registers it in
# ``sys.modules`` as ``"__main__"``, NOT ``"main"``. Other modules in this
# package (notably ``plan_and_execute_tool.py``) do ``import main as
# _main_module`` at request time to reach the live, already-initialized
# globals (``mcp_manager``, ``get_catalog()``, etc.) populated by
# ``_init_agent_subsystems()``.
#
# Without this alias, ``import main`` finds no cached module named "main"
# and Python re-executes this entire file as a SECOND, independent module
# object. That fresh copy has its own fresh globals — ``mcp_manager`` stays
# ``None`` forever because nothing ever calls ``_init_agent_subsystems()``
# on it — so any caller that does ``import main`` gets an uninitialized
# module and ``get_catalog()`` raises ``AttributeError: 'NoneType' object
# has no attribute 'list_gateways'``. This is deterministic (not a cold-start
# race): it reproduces on the very first call after this module is loaded.
# Confirmed via CloudWatch logs for job 29cc7fd0-313b-4d0b-8179-74fd96d8a24c.
#
# Registering the alias as early as possible (before any heavy imports
# below, which could themselves trigger an ``import main`` through some
# indirect path) ensures every subsequent ``import main`` resolves to THIS
# module object rather than triggering a duplicate execution. ``setdefault``
# makes this a no-op in contexts where "main" is already the canonical
# module name (e.g. if this file is ever imported normally instead of run
# as a script, or in tests).
sys.modules.setdefault("main", sys.modules[__name__])

try:
    from dotenv import load_dotenv
    _p = Path(__file__).parent.parent / ".env"
    if _p.exists(): load_dotenv(_p)
except ImportError: pass

# Hydrate os.environ from the Secrets Manager runtime-config BEFORE any first-party import or
# env read (keeps the runtime env under V2's 1536-byte cap). Self-contained; no-op when
# RUNTIME_CONFIG_SECRET_ARN is unset (local dev / tests).
import runtime_config_bootstrap  # noqa: F401,E402
from app_version import app_version  # noqa: E402  (reads APP_VERSION at call time, after the bootstrap hydrates it)
import runtime_arns  # noqa: E402 — sub-agent ARNs at call time (SSM parameters, not runtime env)

# --- CRITICAL: Create the app FIRST so /ping responds immediately ---
from bedrock_agentcore.runtime import BedrockAgentCoreApp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Heavy imports ---
from strands import Agent, tool
from strands.models import BedrockModel
from strands.agent.conversation_manager import SummarizingConversationManager
from model.factory import build_model
from strands_tools import file_read, http_request, current_time, load_tool, shell
from strands_tools.code_interpreter import AgentCoreCodeInterpreter
from tool_input_normalize import wrap_code_interpreter, wrap_http_request
from bedrock_agentcore.memory import MemoryClient
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
import boto3

from security import identity
from skills_manager import SkillsManager
from mcp_manager import MCPManager
from model.catalog import discover_models
from orchestration.progress_channel import ProgressChannel, set_active_channel
from gateway.registry import DuplicateGatewayError, GatewayDescriptor, GatewayRegistry
from jobs import context as jobs_context
import ltm
import memory_context
from jobs import get_job_tool, list_my_jobs_tool, get_plan_results_tool
from jobs.get_job_tool import get_job
from jobs.list_my_jobs_tool import list_my_jobs
from jobs.get_plan_results_tool import get_plan_results
from tools.result_files import read_result_files
from tools.view_images import view_images
from tools.crop_video import crop_video
from tools.media_quick import clip_video, extract_frames
from tools.media_gallery import show_media_gallery
from jobs.registry import JobRegistry
from orchestration.step_labels import TOOL_LABELS, coerce_input as _safe_input, has_input_payload, label_for_tool_done, label_for_tool_use
from orchestration import dispatch_tool
from orchestration import plan_and_execute_tool
import custom_agent_tool

# Orchestrator model default. Overridable PER REQUEST via the invoke payload's
# `model_id` (see _resolve_request_model) — the Agent is constructed fresh for
# every request, so the model belongs in that construction alongside the
# per-actor skills/tools/hooks rather than being mutated on a shared object.
DEFAULT_ORCHESTRATOR_MODEL_ID = "global.anthropic.claude-opus-5-5"


def _resolve_request_model(payload: dict, fallback):
    """Pick the model for THIS request.

    The Agent is rebuilt per request (see the request_agent construction below)
    precisely so per-actor state cannot leak between callers. The model was the
    one setting still taken from a container-level object, which meant the UI's
    model choice was applied by mutating that shared object: it silently reverted
    on cold start, did not apply to other concurrent containers, and leaked
    across users sharing a container.

    Resolving it here makes the choice per-request and per-actor, and survives
    container churn. Returns `(model, resolved_id)`; an unusable id falls back to
    the default rather than failing the turn.
    """
    requested = (payload or {}).get("model_id")
    # Reasoning-effort cap from the UI slider. Validate to the enum; anything else
    # (incl. absent / "Default") means None -> env BEDROCK_REASONING_EFFORT fallback,
    # exactly as build_model does. The backend is fail-safe: build_model only attaches
    # output_config.effort for a model that supports it, so a stale effort never 400s.
    effort = (payload or {}).get("effort")
    if effort not in ("low", "medium", "high"):
        effort = None
    from botocore.config import Config as _BotoCfg
    _cfg = _BotoCfg(read_timeout=300, connect_timeout=10,
                    retries={"max_attempts": 4, "mode": "standard"})
    if not requested or not isinstance(requested, str):
        # No explicit model pick. Honor a UI effort on the DEFAULT model by building
        # one here; otherwise return the prebuilt fallback unchanged (today's behavior).
        if effort is None:
            return fallback, DEFAULT_ORCHESTRATOR_MODEL_ID
        try:
            return (build_model(model_id=DEFAULT_ORCHESTRATOR_MODEL_ID,
                                boto_client_config=_cfg, effort=effort),
                    DEFAULT_ORCHESTRATOR_MODEL_ID)
        except Exception as exc:  # noqa: BLE001 - never fail a turn over effort
            logger.warning("default-model effort build failed, using fallback: %s", exc)
            return fallback, DEFAULT_ORCHESTRATOR_MODEL_ID
    try:
        return build_model(model_id=requested, boto_client_config=_cfg,
                           effort=effort), requested
    except Exception as exc:  # noqa: BLE001 - never fail a turn over model choice
        logger.warning("model_id %r unusable, falling back to %s: %s",
                       requested, DEFAULT_ORCHESTRATOR_MODEL_ID, exc)
        return fallback, DEFAULT_ORCHESTRATOR_MODEL_ID

from hooks.loader import build_provider as _build_hook_provider
import main_lab_tools
from persistence import persistence_tool
from lab.asset_index import AssetIndex
from lab.chat_agent import InventorySnapshot
from lab.context import LabRequestContext
from lab.gateway_browser import GatewayBrowser
from lab.models import GatewayToolDescriptor
from lab.s3_user_files_tool import S3UserFilesTool
from lab.service import (
    LabService,
    _DefaultGatewayClient,
    _load_gateway_endpoints,
)
from prompts.orchestrator import (
    SKILL_GEN_PROMPT,
    build_orchestrator_system_prompt,
    build_session_output_guidance,
)

# --- Config ---
REGION = os.environ.get("AWS_REGION", "us-west-2")
S3_BUCKET = os.environ.get("SKILLS_S3_BUCKET", "dynamic-skills-agent-artifacts")
UPLOADS_BUCKET = os.environ.get("UPLOADS_S3_BUCKET", "")
# The M2C gateway tools write outputs to this bucket (service-derived prefix).
# Per-environment, injected into the system prompt for context.
MEDIA_PROXY_BUCKET = os.environ.get("MEDIA_PROXY_BUCKET", "")
S3_PREFIX = os.environ.get("ARTIFACTS_PREFIX", "artifacts/")
ARTIFACTS_DOMAIN = os.environ.get("ARTIFACTS_DOMAIN", "")
MEMORY_ID = os.environ.get("MEMORY_ID", "")
# Kill-switch for the long-term-memory RETRIEVAL/injection path (default on).
# When off, the session manager still persists conversation history but does not
# semantic-search the preferences namespace and inject a <user_context> block
# each turn. NOTE: an env change only takes effect for a FRESH runtime session —
# warm microVMs keep the old value until the idle timeout / StopRuntimeSession
# (see AGENTS.md "Runtime env-var changes don't reach a warm session").
MEMORY_LTM_RETRIEVAL = os.environ.get("MEMORY_LTM_RETRIEVAL", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
# Context-window COMPRESSION via Strands' SummarizingConversationManager: on
# context overflow, summarize the oldest `summary_ratio` of messages instead of
# dropping them (the default SlidingWindowConversationManager only trims). This
# is DISTINCT from the AgentCore SUMMARY *strategy* (long-term, cross-session
# summaries persisted in Memory) — the conversation manager governs the
# in-context window for this turn. Gated + tunable via env (default on); the
# extra LLM summarize call only fires when the window actually overflows.
MEMORY_COMPRESSION = os.environ.get("MEMORY_COMPRESSION", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
MEMORY_COMPRESSION_RATIO = float(os.environ.get("MEMORY_COMPRESSION_RATIO", "0.3"))
MEMORY_COMPRESSION_PRESERVE_RECENT = int(
    os.environ.get("MEMORY_COMPRESSION_PRESERVE_RECENT", "10")
)
# History writes off the event loop (memory_context.FramedMemorySessionManager async_writes):
# each message is serialized when added, sent by a background writer in order, and drained at
# turn end. Was one synchronous CreateEvent (0.1-0.6s) per message and per agent-state sync.
# Off restores the SDK default (batch_size=1, inline writes). The batch size bounds the buffer.
MEMORY_ASYNC_WRITES = os.environ.get("MEMORY_ASYNC_WRITES", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
MEMORY_WRITE_BATCH_SIZE = max(2, min(100, int(os.environ.get("MEMORY_WRITE_BATCH_SIZE", "50"))))
PLANNING_AGENT_ARN = os.environ.get("PLANNING_AGENT_RUNTIME_ARN", "")
PLAN_STATE_TABLE = os.environ.get("PLAN_STATE_TABLE", "")
LAB_ASSET_INDEX_TABLE = os.environ.get("ASSET_INDEX_TABLE", "")
LAB_ARTIFACTS_BUCKET = os.environ.get("ARTIFACTS_BUCKET", "")
GATEWAY_REGISTRY_TABLE = os.environ.get("GATEWAY_REGISTRY_TABLE", "")
JOB_REGISTRY_TABLE = os.environ.get("JOB_REGISTRY_TABLE", "")

# Async-plan machine creds. Used to arm the delegated (machine-token + grant)
# connect for user_jwt gateways on a TOKEN-LESS but grant-eligible invocation
# (e.g. the Slack ingress path, which invokes via IAM/SigV4 with no user JWT).
# Same client the task agent's async-plan connect uses; empty in dev, in which
# case the delegated connect is a no-op and user_jwt gateways stay deferred.
ASYNC_PLAN_TOKEN_ENDPOINT = os.environ.get("ASYNC_PLAN_TOKEN_ENDPOINT", "")
ASYNC_PLAN_CLIENT_ID = os.environ.get("ASYNC_PLAN_CLIENT_ID", "")
ASYNC_PLAN_CLIENT_SECRET = os.environ.get("ASYNC_PLAN_CLIENT_SECRET", "")
ASYNC_PLAN_SCOPE = os.environ.get("ASYNC_PLAN_SCOPE", "")

app = BedrockAgentCoreApp()


# ---------------------------------------------------------------------------
# Lab capability — heavy resources cached at module scope (boto3 resource +
# Gateway client). The per-request LabService is instantiated fresh so it
# binds the correct LabRequestContext, but it reuses these singletons.
# ---------------------------------------------------------------------------

_lab_ddb_table = None
_lab_gateway_client = None


def _get_lab_table():
    global _lab_ddb_table
    if _lab_ddb_table is None and LAB_ASSET_INDEX_TABLE:
        ddb = boto3.resource("dynamodb", region_name=REGION)
        _lab_ddb_table = ddb.Table(LAB_ASSET_INDEX_TABLE)
    return _lab_ddb_table


class _RegistryGatewayClient:
    """Lab GatewayClient backed by the registry-loaded ``mcp_manager``.

    The Lab tool *catalog* (``/catalog`` → ``mcp_list_available`` →
    ``mcp_manager.list_gateways``) enumerates every gateway in the DDB registry,
    including federated ones (e.g. the M2C media gateway). The Lab *validator*
    (``tool_collection_crud._validate_tool_refs`` via ``GatewayBrowser``)
    previously used ``_DefaultGatewayClient``, which lists only the single
    ``GATEWAY_ENDPOINTS`` URL — so federated-gateway tools appeared in the
    catalog but failed validation (BUG-3). This client sources the browser's
    projection from the SAME ``mcp_manager`` the catalog uses, so the two agree.

    ``list_tools`` returns the fully-qualified ``gateway___tool`` names (the
    validator matches on name only; gateway tools carry no real version).
    ``call_tool`` (the tool-test path) delegates to ``_DefaultGatewayClient``.
    """

    def __init__(self) -> None:
        self._fallback = _DefaultGatewayClient()

    def list_tools(self, actor_id: str, timeout: float) -> list[GatewayToolDescriptor]:
        # mcp_manager is the module-global registry-backed manager (built in
        # deferred_construction). If Lab is queried before it exists, fall back
        # to the single-endpoint client rather than failing closed.
        mgr = globals().get("mcp_manager")
        if mgr is None:
            return self._fallback.list_tools(actor_id, timeout)
        descriptors: list[GatewayToolDescriptor] = []
        for gw in mgr.list_gateways():
            for tool in gw.get("tools", []) or []:
                descriptors.append(GatewayToolDescriptor(
                    name=tool.get("name", ""),
                    description=tool.get("description", ""),
                    version="1.0",  # gateway tools carry no real version; matched by name
                    input_schema=tool.get("inputSchema") or {},
                ))
        return descriptors

    def call_tool(self, tool_name, tool_version, input_data, timeout):
        # Route through the registry-backed manager, matching `list_tools`
        # above. The fallback client sends every invocation to the single
        # GATEWAY_ENDPOINTS URL, so a federated tool went to the wrong gateway
        # (the invocation half of BUG-3) and, now that gateway authenticates
        # end users rather than a machine identity, would be rejected outright.
        import time as _time
        from lab.gateway_browser import ToolInvocationResult

        mgr = globals().get("mcp_manager")
        if mgr is None:
            return self._fallback.call_tool(tool_name, tool_version, input_data, timeout)
        start = _time.time()
        try:
            response = mgr.call_tool(tool_name, input_data or {})
        except Exception as exc:
            # Surface as a failed invocation rather than an exception: the test
            # harness reports tool failures as results, not crashes.
            return ToolInvocationResult(
                response_body={"error": str(exc)},
                status=502,
                duration_ms=int((_time.time() - start) * 1000),
            )
        return ToolInvocationResult(
            response_body=response,
            status=200,
            duration_ms=int((_time.time() - start) * 1000),
        )


def _get_lab_gateway_client():
    global _lab_gateway_client
    if _lab_gateway_client is None:
        _lab_gateway_client = _RegistryGatewayClient()
    return _lab_gateway_client


_LAB_DEFAULT_ACTOR_ID = identity.LAB_DEFAULT_ACTOR_ID


# Actor-id resolution and JWT ``sub`` decoding now live in the shared
# ``identity`` module so ``main.py`` and ``task_agent.py`` use one
# implementation (custom-agents design, Component 5). These thin aliases
# preserve the existing internal call sites and semantics unchanged.
_decode_jwt_sub = identity.decode_jwt_sub
_resolve_actor_id = identity.resolve_actor_id


# Cognito group whose members are allowed to mutate the gateway registry.
# Mirrors the UI helper in `ui/src/auth/isGatewayAdmin.ts`.
GATEWAY_ADMIN_GROUP = "gateway-admins"


def _is_admin(agentcore_context, payload: Optional[dict] = None) -> bool:
    """Return True iff the caller is a member of the ``gateway-admins`` group.

    The cloud-managed AgentCore runtime does not expose JWT claims to the
    handler context (see :func:`_resolve_actor_id` for the platform
    constraint). The proxy validates the bearer token at the edge, pulls
    the ``cognito:groups`` claim from the access token, and stamps the
    list into the request body as ``__actor_groups``. The body is the
    only channel that survives the AgentCore boundary, so we read group
    membership from there.

    Resolution order (first non-empty wins):
      1. ``payload['__actor_groups']`` — the proxy's primary path.
      2. ``context.claims['cognito:groups']`` — forward-compat for SDK
         builds that eventually surface JWT claims on the context.

    Fail-closed semantics (Requirement R3): any exception, missing
    payload, missing claim, or unexpected shape resolves to ``False``.
    Only an explicit list of strings containing ``"gateway-admins"``
    produces ``True``.

    Args:
        agentcore_context: The AgentCore runtime context object.
        payload: The request payload. The proxy stamps
            ``__actor_groups`` here after JWT validation; we look there
            first because it's the only channel that survives the
            cloud-managed runtime's header stripping.

    Returns:
        ``True`` only when one of the sources lists ``"gateway-admins"``;
        ``False`` otherwise.
    """
    try:
        if isinstance(payload, dict):
            groups = payload.get("__actor_groups")
            if isinstance(groups, list) and GATEWAY_ADMIN_GROUP in groups:
                return True
        # Forward-compat fallback: future SDK builds may surface claims.
        claims = getattr(agentcore_context, "claims", None)
        if not isinstance(claims, dict):
            return False
        groups = claims.get("cognito:groups")
        if not isinstance(groups, list):
            return False
        return GATEWAY_ADMIN_GROUP in groups
    except Exception:
        return False


def _build_lab_service_for_request(
    agentcore_context, payload: Optional[dict] = None
) -> "LabService | None":
    """Build a fresh LabService bound to the request's actor.

    Returns None if Lab capability is not configured (missing env vars) so the
    Main Agent still functions in environments where Lab is disabled.

    The proxy stamps ``__actor_id`` into the request body after JWT
    validation; passing ``payload`` here lets :func:`_resolve_actor_id`
    pick that up. Without the payload arg the resolver has no way to
    reach the actor id on cloud-managed runtimes (see the
    :func:`_resolve_actor_id` docstring for the platform constraint).
    """
    if not LAB_ASSET_INDEX_TABLE or not LAB_ARTIFACTS_BUCKET:
        return None
    try:
        actor_id = _resolve_actor_id(agentcore_context, payload=payload)
        session_id = getattr(agentcore_context, "session_id", None) or ""
        import uuid as _uuid
        ctx = LabRequestContext(
            actor_id=actor_id,
            session_id=session_id,
            request_id=str(_uuid.uuid4()),
        )
    except Exception as exc:
        logger.warning("LabRequestContext binding failed: %s", exc)
        return None
    table = _get_lab_table()
    if table is None:
        return None
    s3_tool = S3UserFilesTool(bucket=LAB_ARTIFACTS_BUCKET, actor_id=ctx.actor_id)
    index = AssetIndex(ddb_table=table, actor_id=ctx.actor_id)
    gateway_browser = GatewayBrowser(
        gateway_client=_get_lab_gateway_client(),
        configured_endpoints=_load_gateway_endpoints(),
    )
    return LabService(ctx=ctx, s3=s3_tool, index=index, gateway_browser=gateway_browser)


def _build_persistence_service_for_request(agentcore_context, payload: Optional[dict] = None):
    """Build a fresh PersistenceService for the request (R9.1, R11.1).

    Mirrors :func:`_build_lab_service_for_request`: instantiated per turn, reusing
    the shared ``MCPManager`` for OMS Core access (``OMSMcpClient.from_manager``)
    and the module boto3 clients for the plan read (``PlanReader``). The
    persistence ``@tool`` surface is registered unconditionally; this binding is
    defensive — a missing/unregistered OMS Core gateway (or any dependency gap)
    logs and returns None so an unrelated turn never crashes. The offending turn
    simply has no bound PersistenceService and the tools raise a clear
    "no service bound" error if invoked.
    """
    try:
        if mcp_manager is None:
            return None
        # OMS Core client — resolve the gateway connection from the shared manager
        # (no new transport). Raises OMSUnavailableError if the gateway is absent.
        from persistence.oms_mcp_client import OMSMcpClient
        from persistence.plan_reader import PlanReader
        from persistence.persistence_service import PersistenceService

        oms_client = OMSMcpClient.from_manager(mcp_manager)
        # PlanReader reads the DynamoDB Plan_State_Table (via a dynamodb resource
        # exposing .Table(...)) and fetches the plan object from S3. Table/bucket
        # names are sourced from env vars inside PlanReader (repo convention).
        ddb_resource = boto3.resource("dynamodb", region_name=REGION)
        plan_reader = PlanReader(ddb_client=ddb_resource, s3_client=s3_client)
        return PersistenceService(oms_client=oms_client, plan_reader=plan_reader)
    except Exception as exc:
        logger.warning("PersistenceService binding skipped: %s", exc)
        return None

# --- Deferred construction (runs once on first invocation) ---
# These globals are populated by _init_agent_subsystems() to keep module-load
# time minimal. The AgentCore platform imposes a 30-second init window; moving
# boto3 client creation, Agent construction, and registry wiring out of module
# scope saves ~2-5s of credential resolution that compounds under otel
# instrumentation on cold microVMs.
code_interpreter = None
s3_client = None
skills_manager = None
skills_plugin = None
mcp_manager = None
_gateway_initialized = False
_available_models: list[dict] = []
_catalog_cache_valid = False
_cached_catalog_dict: dict | None = None
_cached_catalog_text_str: str = ""
_gateway_registry: GatewayRegistry | None = None
_job_registry: Optional[JobRegistry] = None
_all_builtin_tools: dict = {}
_added_builtins: set[str] = set()
_trusted_builtins: set[str] = set()
model = None
agent = None
harness_hook_provider = None

_AGENT_SUBSYSTEMS_READY = False


# Max base64 length accepted inline by artifact(upload). ~16 KB of base64 ≈ ~12 KB of file and
# ~4K output tokens — a safe "tiny inline" ceiling well under the model's output budget. Anything
# larger must go code_interpreter -> S3 -> adopt (see the upload guard) so bytes never round-trip
# through the model.
_MAX_INLINE_UPLOAD_B64_CHARS = 16384


def _artifact_session_id() -> str:
    try:
        sid = (jobs_context.current_session_id() or "").strip()
    except Exception:  # noqa: BLE001 - context may be unbound; fall back safely
        sid = ""
    return sid.replace("/", "_").strip() if sid else ""


def _artifact_output_prefix() -> str:
    """The consistent, session-scoped OUTPUT location for generated artifacts.

    ``projects/<pid>/sessions/<sid>/output/`` in an env with ``SESSION_OUTPUT_LAYOUT=project`` and a
    trusted project for this turn (asset-lineage Phase 3 (a)), else the legacy
    ``artifacts/<session_id>/output/``. Falls back to the flat ``ARTIFACTS_PREFIX`` when no session is
    bound to the request context, so this never raises and stays backward-compatible with
    already-stored flat ``artifacts/<file>`` URLs.
    """
    from projects.storage_layout import session_output_prefix

    return session_output_prefix(_artifact_session_id(), jobs_context.trusted_project_id(),
                                 artifacts_prefix=S3_PREFIX)


def _legacy_output_prefix() -> str:
    """``artifacts/<sid>/output/`` (or flat) — where this session's PRE-Phase-3 outputs live. Readers
    (adopt/download) fall back to it so a continuation session still finds its older files."""
    sid = _artifact_session_id()
    return f"{S3_PREFIX}{sid}/output/" if sid else S3_PREFIX


def _artifact_input_prefix(session_id: str = "") -> str:
    """The consistent, session-scoped INPUT location for user uploads:
    ``artifacts/<session_id>/input/`` (served via CloudFront from the skills bucket).
    Prefers an explicit ``session_id`` (passed by the UI upload), else the request
    context, else falls back to ``artifacts/uploads/input/``.

    The fallback keeps the ``/input/`` segment even with no session. That segment is what
    marks an artifact as SOURCE media by its PATH (``hooks.provider._has_input_path``; the
    same rule lives in ``ui/server/uploadKey.ts``), so dropping it would classify every
    session-less upload as a deliverable and give it its own spurious render tab. Losing the
    session here is unavoidable; losing the role is not."""
    sid = (session_id or "").strip()
    if not sid:
        try:
            sid = (jobs_context.current_session_id() or "").strip()
        except Exception:  # noqa: BLE001 - context may be unbound; fall back safely
            sid = ""
    safe = sid.replace("/", "_").strip() if sid else ""
    return f"{S3_PREFIX}{safe}/input/" if safe else f"{S3_PREFIX}uploads/input/"


@tool
def artifact(operation: str, key: str = "", data_b64: str = "", content_type: str = "", source_uri: str = "") -> str:
    """Manage files in shared S3 storage.
    Args:
        operation: One of 'upload', 'adopt', 'import', 'download', or 'list'.
        key: Filename only — do NOT include any path prefix like 'artifacts/'. Just the bare filename, e.g. 'report.pdf' or 'chart.png'. The storage prefix is added automatically. For 'import' this is the OPTIONAL destination filename (defaults to the source basename).
        data_b64: Base64-encoded file content — for TINY inline files only (< ~12 KB). For
            anything larger, and ALWAYS for a file code_interpreter produced (games, apps, HTML,
            PDFs, charts, data exports), do NOT use this — the model would have to emit every byte
            as output tokens, which blows the output-token limit and can corrupt the file. Instead,
            from code_interpreter write the file DIRECTLY to the session OUTPUT prefix given in
            the SESSION OUTPUT LOCATION section of your instructions, via boto3; then call
            operation='adopt' with just the filename.
        content_type: MIME type. Auto-detected from file extension if not provided.
        source_uri: For 'import' only — either a full ``s3://bucket/key`` URI (e.g. an M2C
            micro-workflow output in the proxy bucket, copied server-side) OR an ``https://`` URL
            (e.g. a fal generation output) which is fetched and stored durably.

    Use 'import' to make an M2C / gateway RESULT file downloadable + renderable when it lives in
    another bucket the runtime can read (probe.json, shots.json, facemetadata.json, audio stems,
    a synthesized .wav): it server-side copies the object BY KEY (no bytes through the model) into
    the artifacts prefix, fixes its content-type, and returns the permanent CloudFront URL. Prefer
    this whenever the user wants to download or play an analysis/result file that is referenced by
    an ``s3://`` URI rather than an https URL on our CloudFront.

    Use 'adopt' for a file you ALREADY wrote to the artifacts prefix from
    code_interpreter (a large video / PDF / HTML). Pass just its filename as
    `key` and NO data_b64: it verifies the object, fixes its content-type if
    needed, and returns the permanent CloudFront URL so the file renders — WITHOUT
    base64-relaying bytes through the model (which corrupts large files and blows
    the output-token limit) and WITHOUT presigning (CloudFront serves artifacts
    permanently). Prefer this over uploading big files with data_b64 or
    presigning S3 URLs yourself.
    """
    from render.types import mime_for
    prefix = _artifact_output_prefix()
    # Robustly strip ANY leading copy of our known prefixes so the prefix is never DOUBLED. A key
    # like "<session>/output/report.pdf" (the session segment WITHOUT the leading "artifacts/") was
    # re-prefixed into "artifacts/<session>/output/<session>/output/report.pdf", which 404s and made
    # the renderer fall back to the harness SPA. Strip the full prefix, the session segment, and the
    # bare artifacts/ prefix, repeatedly, so no combination doubles.
    legacy_prefix = _legacy_output_prefix()
    _sess_seg = legacy_prefix[len(S3_PREFIX):] if legacy_prefix.startswith(S3_PREFIX) else ""  # "<session>/output/"
    _changed = True
    while _changed:
        _changed = False
        for _p in (prefix, legacy_prefix, _sess_seg, S3_PREFIX):
            if _p and key.startswith(_p):
                key = key[len(_p):]
                _changed = True
                break
    # A 'download' whose key is actually an s3://bucket/key URI names an object in ANOTHER bucket
    # (an M2C/gateway result). Building an artifacts CloudFront URL for it returns a dead 403 that
    # gets baked into render manifests (prod session 1792bb4d). Route it through the 'import' path,
    # which server-side copies the object in BY KEY and returns a URL that actually resolves.
    if operation == "download" and (key or "").strip().startswith("s3://"):
        source_uri = (key or "").strip()
        key = ""
        operation = "import"
    if operation == "upload":
        full_key = f"{prefix}{key}"
        # Backstop against the output-token loop: base64-relaying a sizeable file makes the MODEL
        # emit every byte as tool-call output, which for a large file exceeds max_tokens and never
        # completes (the constant "stopped at the output token limit" loop). Reject an oversized
        # inline payload with the exact adopt recipe — including the concrete bucket/prefix so the
        # agent can write from code_interpreter without guessing. (This only catches payloads the
        # model managed to emit; the docstring steers the truly-huge ones to adopt up front.)
        if len(data_b64 or "") > _MAX_INLINE_UPLOAD_B64_CHARS:
            return json.dumps({"error": (
                "INLINE_UPLOAD_TOO_LARGE: do NOT base64-relay this file — it blows the output-token "
                "limit and can corrupt on transcription. From code_interpreter, write it to "
                f"s3://{S3_BUCKET}/{full_key} via boto3 (import boto3; boto3.client('s3')."
                f"put_object(Bucket='{S3_BUCKET}', Key='{full_key}', Body=<bytes>)), then call "
                f"artifact(operation='adopt', key='{key}') to get the permanent URL. This is the "
                "required path for any code_interpreter-produced file.")})
        if not content_type:
            content_type = mime_for(key) or "application/octet-stream"
        s3_client.put_object(Bucket=S3_BUCKET, Key=full_key, Body=base64.b64decode(data_b64), ContentType=content_type)
        if ARTIFACTS_DOMAIN:
            url = f"https://{ARTIFACTS_DOMAIN}/{full_key}"
        else:
            url = s3_client.generate_presigned_url("get_object", Params={"Bucket": S3_BUCKET, "Key": full_key}, ExpiresIn=3600)
        return json.dumps({"status": "uploaded", "key": full_key, "url": url})
    elif operation == "adopt":
        # Register an object code_interpreter already wrote under artifacts/ — by
        # KEY, no bytes. This is the escape hatch for large files (video/PDF/HTML):
        # relaying them as data_b64 corrupts on transcription and blows the output
        # token cap, and presigning is pointless because CloudFront serves the
        # artifacts prefix permanently. Returns the CloudFront URL so the result
        # renders like any other artifact.
        full_key = f"{prefix}{key}"
        src_key = full_key
        try:
            head = s3_client.head_object(Bucket=S3_BUCKET, Key=full_key)
        except Exception as e:  # noqa: BLE001 - report a clear, actionable message
            # The file may have been written to the legacy session prefix (`artifacts/<sid>/output/`,
            # older skill text) or the flat artifacts/ prefix (guidance lag / older habit). Find it
            # there and relocate it into the canonical session-scoped output key below.
            head = None
            for alt_key in dict.fromkeys((f"{legacy_prefix}{key}", f"{S3_PREFIX}{key}")):
                if alt_key == full_key:
                    continue
                try:
                    head = s3_client.head_object(Bucket=S3_BUCKET, Key=alt_key)
                    src_key = alt_key
                    break
                except Exception:  # noqa: BLE001 - genuinely absent
                    head = None
            if head is None:
                return json.dumps({"error": (
                    f"No object at '{full_key}' to adopt ({e}). Write the file to "
                    f"s3://{S3_BUCKET}/{full_key} first (e.g. from code_interpreter via "
                    f"boto3), then adopt it by filename.")})
        existing_ct = head.get("ContentType") or ""
        # Canonical mime resolution (render_types): a SPECIFIC existing type wins; a
        # generic octet-stream from a raw boto3/CLI upload falls back to the extension.
        ct = content_type or mime_for(key, existing_ct) or existing_ct or "application/octet-stream"
        # A direct boto3/CLI upload often leaves ContentType as octet-stream, which
        # makes the browser download rather than render (a video won't play, HTML
        # won't display). Backfill it in place so CloudFront serves it correctly.
        if src_key != full_key:
            # Relocate a file written to the flat prefix into the canonical session
            # output key (also fixes content-type in the same copy).
            try:
                s3_client.copy_object(Bucket=S3_BUCKET, Key=full_key,
                                      CopySource={"Bucket": S3_BUCKET, "Key": src_key},
                                      ContentType=ct, MetadataDirective="REPLACE")
            except Exception:  # noqa: BLE001 - non-fatal; serve from where it is
                full_key = src_key
        elif ct != existing_ct:
            try:
                s3_client.copy_object(Bucket=S3_BUCKET, Key=full_key,
                                      CopySource={"Bucket": S3_BUCKET, "Key": full_key},
                                      ContentType=ct, MetadataDirective="REPLACE")
            except Exception:  # noqa: BLE001 - non-fatal; the URL still resolves
                pass
        if ARTIFACTS_DOMAIN:
            url = f"https://{ARTIFACTS_DOMAIN}/{full_key}"
        else:
            url = s3_client.generate_presigned_url("get_object", Params={"Bucket": S3_BUCKET, "Key": full_key}, ExpiresIn=3600)
        return json.dumps({"status": "adopted", "key": full_key, "url": url, "content_type": ct})
    elif operation == "import":
        # Copy an object from another bucket the runtime can read (the M2C proxy bucket carries
        # micro-workflow result files) into our artifacts prefix — server-side, by key, so no
        # bytes pass through the model or this process. Makes M2C result files (probe.json,
        # shots.json, facemetadata.json, stems, .wav) downloadable via our CloudFront and
        # playable inline, instead of being stuck as an un-fetchable s3:// URI.
        src = (source_uri or "").strip()
        if src.startswith("https://"):
            # One of OUR artifact URLs (CloudFront or /api/artifact?key=): copy it server-side
            # by key via the s3:// branch below instead of fetching it back over HTTP — the
            # CDN may be private, and no bytes need to pass through this process.
            from artifact_import import own_artifact_s3_uri
            src = own_artifact_s3_uri(src, S3_BUCKET, ARTIFACTS_DOMAIN) or src
        if src.startswith("https://"):
            # Fetch a third-party https artifact (a fal.media generation output, or our own
            # CloudFront mirror of one) and store it durably in the output prefix — the reliable
            # https->s3 persist for generation nodes with no code_interpreter (run 627fb96e).
            from artifact_import import fetch_https_bytes
            dest_name = (key or src.split("?", 1)[0].rsplit("/", 1)[-1] or "imported.bin").rsplit("/", 1)[-1]
            full_key = f"{prefix}{dest_name}"
            try:
                body, remote_ct = fetch_https_bytes(src)
            except Exception as e:  # noqa: BLE001 - clear, actionable message
                return json.dumps({"error": f"import failed fetching {src} ({e})"})
            ct = content_type or mime_for(dest_name) or remote_ct or "application/octet-stream"
            s3_client.put_object(Bucket=S3_BUCKET, Key=full_key, Body=body, ContentType=ct)
            if ARTIFACTS_DOMAIN:
                url = f"https://{ARTIFACTS_DOMAIN}/{full_key}"
            else:
                url = s3_client.generate_presigned_url("get_object", Params={"Bucket": S3_BUCKET, "Key": full_key}, ExpiresIn=3600)
            return json.dumps({"status": "imported", "key": full_key, "url": url, "content_type": ct})
        if not src.startswith("s3://") or "/" not in src[len("s3://"):]:
            return json.dumps({"error": "import requires source_uri as a full s3://bucket/key or https:// URI"})
        src_bucket, src_key = src[len("s3://"):].split("/", 1)
        dest_name = (key or src_key.rsplit("/", 1)[-1]).rsplit("/", 1)[-1]
        full_key = f"{prefix}{dest_name}"
        ct = content_type or mime_for(dest_name) or "application/octet-stream"
        try:
            s3_client.copy_object(Bucket=S3_BUCKET, Key=full_key,
                                  CopySource={"Bucket": src_bucket, "Key": src_key},
                                  ContentType=ct, MetadataDirective="REPLACE")
        except Exception as e:  # noqa: BLE001 - clear, actionable message
            return json.dumps({"error": (
                f"import failed copying s3://{src_bucket}/{src_key} ({e}). The runtime can only "
                "read buckets it has been granted (e.g. the M2C proxy bucket).")})
        if ARTIFACTS_DOMAIN:
            url = f"https://{ARTIFACTS_DOMAIN}/{full_key}"
        else:
            url = s3_client.generate_presigned_url("get_object", Params={"Bucket": S3_BUCKET, "Key": full_key}, ExpiresIn=3600)
        return json.dumps({"status": "imported", "key": full_key, "url": url, "content_type": ct})
    elif operation == "download":
        full_key = f"{prefix}{key}"
        # Verify the object exists at the expected key BEFORE returning a URL. An unverified
        # artifacts URL for a key that actually lives in another bucket returns a dead 403 that
        # then gets baked into render manifests (prod session 1792bb4d). On a miss, point the
        # model at 'import' (server-side copy) rather than handing back a URL that 403s.
        try:
            s3_client.head_object(Bucket=S3_BUCKET, Key=full_key)
        except Exception:  # noqa: BLE001 - absent/unreadable; return an actionable error, not a dead URL
            full_key = None
            # An output this session wrote before the project layout (asset-lineage Phase 3 (a)).
            if legacy_prefix != prefix:
                try:
                    s3_client.head_object(Bucket=S3_BUCKET, Key=f"{legacy_prefix}{key}")
                    full_key = f"{legacy_prefix}{key}"
                except Exception:  # noqa: BLE001 - genuinely absent
                    full_key = None
        if full_key is None:
            full_key = f"{prefix}{key}"
            return json.dumps({"error": (
                f"No object at s3://{S3_BUCKET}/{full_key}. If the file is an M2C/gateway result "
                "in another bucket, call artifact(operation='import', "
                "source_uri='s3://<bucket>/<key>').")})
        if ARTIFACTS_DOMAIN:
            url = f"https://{ARTIFACTS_DOMAIN}/{full_key}"
        else:
            url = s3_client.generate_presigned_url("get_object", Params={"Bucket": S3_BUCKET, "Key": full_key}, ExpiresIn=3600)
        return json.dumps({"key": full_key, "url": url})
    elif operation == "list":
        # Both roots: a session that spans the layout flip keeps its pre-flip outputs listable
        # (asset-lineage Phase 3 (a); adopt/download already fall back the same way).
        keys: list = []
        for _pfx in dict.fromkeys((prefix, legacy_prefix)):
            resp = s3_client.list_objects_v2(Bucket=S3_BUCKET, Prefix=_pfx, MaxKeys=50)
            keys.extend(obj["Key"] for obj in resp.get("Contents", []))
        return json.dumps({"keys": keys[:50]})
    return json.dumps({"error": f"Unknown operation: {operation}"})


def _current_memory_actor() -> str:
    """The AgentCore Memory actor id for the current request, keyed identically
    to the session manager (JWT actor → fallback LAB_DEFAULT_ACTOR_ID). Safe
    outside a request (returns the fallback)."""
    try:
        bound = jobs_context.current_actor_id()
    except Exception:
        bound = ""
    return identity.resolve_memory_actor_id(bound)


def _current_session_safe() -> str:
    try:
        return jobs_context.current_session_id()
    except Exception:
        return ""


@tool
def remember(text: str) -> str:
    """Save a user PREFERENCE that should apply across all sessions and projects.

    Use for how the user likes things done, e.g. "always 16:9", "answer briefly",
    "captions in Spanish by default". `text` is the preference in a short sentence.

    NEVER use this for files, file locations, URLs, asset ids, or facts about media
    content: those live in the project (project_status / show_asset), and text with an
    s3:// or http(s):// URL or an artifacts/ or projects/ path is refused.
    """
    if not MEMORY_ID:
        return "Long-term memory is not configured; nothing was saved."
    if not isinstance(text, str) or not text.strip():
        return "Nothing to remember: provide the preference as `text`."
    rejection = ltm.preference_rejection(text)
    if rejection:
        return f"Not saved: {rejection}"
    result = ltm.write_preference(
        memory_id=MEMORY_ID,
        actor_id=_current_memory_actor(),
        text=text,
        region=REGION,
    )
    if result.ok:
        return "Saved to long-term memory as a preference."
    return _ltm_error_text("Not saved to long-term memory", result.error, result.message)


def _ltm_error_text(what: str, error: str, message: str = "") -> str:
    """A memory-tool failure the model can act on: the error CLASS, and whether retrying helps.

    `remember` used to answer every failure with "Could not save to long-term memory.", so a
    permanent AccessDeniedException (the runtime role lacked BatchCreateMemoryRecords) read as
    transient and was retried turn after turn (session 2f376186).
    """
    code = error or "UnknownError"
    tail = (" This is not transient; do not retry. Tell the user memory is unavailable."
            if code in ltm.NON_RETRYABLE_ERRORS else "")
    detail = f" ({message})" if message else ""
    return f"{what}: {code}{detail}.{tail}"


@tool
def forget(query: str, max_records: int = 3) -> str:
    """Delete the user's OWN saved preferences (long-term memory) that match `query`.

    Use when the user asks you to forget, stop using, or correct something remembered from past
    sessions ("forget that I prefer end-to-end runs"). Make `query` specific to what should go:
    up to `max_records` (default 3, max 20) of the closest matches are deleted. Returns the text
    of every removed record; tell the user what was removed. Only the current user's own records
    can be deleted.
    """
    if not MEMORY_ID:
        return "Long-term memory is not configured; nothing was removed."
    if not query or not query.strip():
        return "Provide a query describing what to forget."
    result = ltm.forget(
        memory_id=MEMORY_ID,
        actor_id=_current_memory_actor(),
        query=query,
        max_records=max_records,
        region=REGION,
    )
    lines = [f"- {r['text'] or r['id']}" for r in result.removed]
    if result.error and not result.removed:
        return _ltm_error_text("Nothing was removed", result.error, result.message)
    if not lines:
        return "No matching long-term memories; nothing was removed."
    out = f"Removed {len(lines)} long-term memor{'y' if len(lines) == 1 else 'ies'}:\n" + "\n".join(lines)
    if result.error:
        out += "\n" + _ltm_error_text("Some records were not removed", result.error, result.message)
    return out


@tool
def recall(query: str) -> str:
    """Search long-term memory: the user's saved preferences (from any session) and
    this session's summary. Memory holds no files or file locations: to find or show a
    file, use project_status / show_asset on the active project."""
    if not MEMORY_ID:
        return "Long-term memory is not configured."
    if not query or not query.strip():
        return "Provide a search query."
    hits = ltm.recall(
        memory_id=MEMORY_ID,
        actor_id=_current_memory_actor(),
        query=query,
        session_id=_current_session_safe(),
        region=REGION,
        top_k=8,
    )
    if not hits:
        return "No matching long-term memories."
    return "\n".join(f"- {h}" for h in hits)


# --- Snapshot-friendly initialization -------------------------------------------------------
# AgentCore Runtime V2 restores every new session's microVM from a snapshot taken after the
# container started, so work done at START is paid once per deployed version, while work done on
# the first request is paid again by EVERY new session (measured cold: skills 3.1s + subsystems and
# model catalog 2.7s before the first model call). The split:
#
# * ``_warm_up_static()`` -- everything user-independent: boto clients, the BUNDLED skills
#   (parsed from disk), the gateway/job registry objects, the model object, the default hooks and
#   the model catalog. Runs before ``app.run()`` (see the bottom of this module) and is captured
#   in the snapshot. It never sees a request, so it can capture no user token, actor or session
#   state; it resolves only the runtime role's own (auto-refreshing) credentials, the same ones
#   these clients resolved on first request before.
# * ``_init_agent_subsystems()`` -- the per-container remainder, on the first invocation as
#   before. It runs ``_warm_up_static()`` itself when warm-up did not happen (tests, local runs,
#   a warm-up that failed), so the lazy path is unchanged. Two things stay per container on
#   purpose: the code-interpreter client (it names a random sandbox session when built, which a
#   snapshot would hand to every session) with the builtin tool table that wraps it, and the
#   S3-authored skills (they change at runtime). The skill load starts on a worker thread and
#   overlaps the gateway connects; ``_await_skills_ready()`` joins it before anything reads skills.
_INIT_LOCK = threading.RLock()
_STATIC_READY = False
_skills_load_future = None          # concurrent.futures.Future[AgentSkills] for the S3+disk load
_skills_load_lock = threading.Lock()
_models_discovered_at: Optional[float] = None   # wall-clock time of the last model discovery
_models_refresh_lock = threading.Lock()
_models_refreshing = False
# A snapshot can be restored long after it was taken, so a catalog older than this is refreshed
# in the background on first use (the stale one serves meanwhile; see _ensure_model_catalog).
_MODEL_CATALOG_MAX_AGE_S = float(os.environ.get("MODEL_CATALOG_MAX_AGE_S", "21600"))


def _warm_up_static() -> None:
    """User-independent construction (see the block comment above). Idempotent, thread-safe."""
    global _STATIC_READY
    if _STATIC_READY:
        return
    with _INIT_LOCK:
        if _STATIC_READY:
            return
        try:
            _construct_static_subsystems()
        finally:
            # Same latch semantics as before: a partial failure is not retried on every call.
            _STATIC_READY = True


def _construct_per_container() -> None:
    """The part of init that must NOT be snapshotted: it runs once per container, on first use.

    ``AgentCoreCodeInterpreter`` picks a random sandbox session name when constructed; built at
    warm-up, every session restored from the snapshot would share it. So it is built here, as
    before, and the builtin tool table (which wraps it) is filled right after -- cheap, and in
    the same insertion order as always, so the tool list the model sees is unchanged.
    """
    global code_interpreter
    _ci_id = os.environ.get("CODE_INTERPRETER_ID", "")
    try:
        if _ci_id:
            code_interpreter = AgentCoreCodeInterpreter(region=REGION, identifier=_ci_id)
        else:
            code_interpreter = AgentCoreCodeInterpreter(region=REGION)
        logger.info("Code interpreter initialized (id=%s)", _ci_id or "auto")
    except Exception as e:
        logger.warning("Code interpreter init failed: %s", e)
    _register_builtin_tools()


def _construct_static_subsystems() -> None:
    global s3_client, skills_manager, mcp_manager
    global _gateway_registry, _job_registry, model, harness_hook_provider

    # S3 client
    s3_client = boto3.client("s3", region_name=REGION)

    # Skills (bundled ones only -- S3-authored skills load per container) & MCP
    skills_manager = SkillsManager()
    try:
        skills_manager.preload_disk_skills()
    except Exception:  # noqa: BLE001 - create_plugin reads the disk itself when this is absent
        logger.warning("bundled-skill preload failed; loading on first request", exc_info=True)
    mcp_manager = MCPManager()

    # Gateway registry
    if GATEWAY_REGISTRY_TABLE:
        _gw_ddb = boto3.resource("dynamodb", region_name=REGION)
        _gw_secrets = boto3.client("secretsmanager", region_name=REGION)
        _gateway_registry = GatewayRegistry(
            ddb_table=_gw_ddb.Table(GATEWAY_REGISTRY_TABLE),
            secrets_client=_gw_secrets,
        )
        # Same table, used directly to publish the builtin-tool catalog so the
        # UI can read it without invoking this runtime. See
        # _publish_builtin_catalog.
        globals()["_catalog_table"] = _gw_ddb.Table(GATEWAY_REGISTRY_TABLE)
        logger.info("GatewayRegistry initialized against table %s", GATEWAY_REGISTRY_TABLE)
    else:
        logger.info("GATEWAY_REGISTRY_TABLE not set; gateway_* actions will be unavailable")

    # Job registry
    if JOB_REGISTRY_TABLE:
        try:
            _job_registry = JobRegistry(table_name=JOB_REGISTRY_TABLE, region=REGION)
            logger.info("JobRegistry initialized against table %s", JOB_REGISTRY_TABLE)
        except Exception as exc:
            logger.error("JobRegistry init failed for table %s: %s", JOB_REGISTRY_TABLE, exc)
    else:
        logger.warning("JOB_REGISTRY_TABLE not set; per-tool job tracking disabled")

    get_job_tool.set_registry(_job_registry)
    list_my_jobs_tool.set_registry(_job_registry)
    get_plan_results_tool.set_registry(_job_registry)
    get_plan_results_tool.set_manifest_location(bucket=S3_BUCKET, region=REGION)

    # Model
    from botocore.config import Config as BotoConfig
    _boto_cfg = BotoConfig(read_timeout=300, connect_timeout=10, retries={"max_attempts": 4, "mode": "standard"})
    model = build_model(model_id=DEFAULT_ORCHESTRATOR_MODEL_ID, boto_client_config=_boto_cfg)

    # Hooks
    harness_hook_provider = _build_hook_provider()

    # Clients every request builds: creating one parses the service model JSON once per boto
    # session, so doing it here keeps that parse in the snapshot (see _memory_boto_session).
    _warm_boto_clients()
    logger.info("Static agent subsystems initialized")


def _register_builtin_tools() -> None:
    _all_builtin_tools.update({
        "file_read": file_read, "http_request": http_request,
        "current_time": current_time, "load_tool": load_tool,
        "shell": shell, "artifact": artifact, "dispatch_tool": dispatch_tool,
        "plan_and_execute_tool": plan_and_execute_tool,
        "invoke_custom_agent": custom_agent_tool.invoke_custom_agent,
        "get_job": get_job,
        "list_my_jobs": list_my_jobs,
        # User-facing "what can this do right now" — sanitized live capability
        # summary for the agent-harness-help Skill (grounding tool, option B).
        "get_available_capabilities": get_available_capabilities,
        # Plan job rows carry no results tool, so without this a SUCCEEDED plan
        # has no readable outputs and "retrieve the results" loops.
        "get_plan_results": get_plan_results,
        # Reads many result files in one concurrent call. Without it the agent
        # presigns and fetches one file per model round-trip.
        "read_result_files": read_result_files,
        # The model SEES up to 8 images (frames of a shot, a chart, a generated still) — the only tool
        # that returns pixels to it rather than a url.
        "view_images": view_images,
        # Deterministic arbitrary-box video crop (typed tool, not a hand-authored
        # code_interpreter cell) — pass the UI's normalized box verbatim.
        "crop_video": crop_video,
        # The timeline's fast Frame / Clip in the chat's warm sandbox (ffmpeg + source cached): ~1 s.
        "extract_frames": extract_frames,
        "clip_video": clip_video, "show_media_gallery": show_media_gallery,
        # Long-term memory: user preferences (+ this session's summary on recall).
        "remember": remember,
        "recall": recall,
        "forget": forget,
    })
    # Private artifacts CDN: `http_request` on one of OUR artifact URLs gets a presigned S3 URL
    # (GET or HEAD) instead of a 403 from CloudFront. No-op unless ARTIFACTS_CDN_PRIVATE.
    _all_builtin_tools["http_request"] = wrap_http_request(http_request)
    if code_interpreter:
        # Wrap so a model-stringified `code_interpreter_input` (a JSON string) is
        # coerced back to the dict the tool's schema requires before validation —
        # otherwise the model retries the same call several ways per invocation.
        _all_builtin_tools["code_interpreter"] = wrap_code_interpreter(code_interpreter.code_interpreter)
    for _lab_tool in main_lab_tools.LAB_TOOLS:
        _all_builtin_tools[_lab_tool.tool_spec["name"]] = _lab_tool
    for _persistence_tool in persistence_tool.PERSISTENCE_TOOLS:
        _all_builtin_tools[_persistence_tool.tool_spec["name"]] = _persistence_tool

    _added_builtins.update({
        "dispatch_tool", "plan_and_execute_tool", "invoke_custom_agent",
        "file_read", "shell",
        "http_request", "current_time",
        "artifact", "code_interpreter",
        "get_job", "list_my_jobs", "read_result_files", "get_plan_results",
        "view_images",
        "get_available_capabilities",
        "crop_video", "extract_frames", "clip_video", "show_media_gallery",
        "remember", "recall", "forget",
        *(t.tool_spec["name"] for t in main_lab_tools.LAB_TOOLS),
        *(t.tool_spec["name"] for t in persistence_tool.PERSISTENCE_TOOLS),
    })
    _trusted_builtins.update(_added_builtins)


# One boto session for the per-request memory session managers. Each manager otherwise builds a
# fresh ``boto3.Session``, and a fresh session re-parses its service models for every client it
# creates. Created (and its two clients' models parsed) at warm-up; used from the event-loop
# thread only, where every session manager is constructed.
_memory_boto_session = None


def _warm_boto_clients() -> None:
    """Pre-parse the service models of the clients built per request. Best-effort."""
    global _memory_boto_session
    try:
        if MEMORY_ID:
            session = boto3.Session(region_name=REGION)
            for svc in ("bedrock-agentcore", "bedrock-agentcore-control"):
                session.client(svc, region_name=REGION)
                # MemoryClient() also builds clients of these from the default session.
                boto3.client(svc, region_name=REGION)
            _memory_boto_session = session
    except Exception:  # noqa: BLE001 - a cold parse on first request is the fallback
        logger.debug("boto client warm-up skipped", exc_info=True)


def _ensure_model_catalog(*, block: bool) -> None:
    """Make ``_available_models`` current.

    No catalog yet -> discover now when ``block`` (the system prompt names the models). A catalog
    older than ``_MODEL_CATALOG_MAX_AGE_S`` (a snapshot restored long after warm-up) keeps serving
    while a daemon thread refreshes it; the catalog cache is invalidated only when the refreshed
    list differs, so an unchanged list never perturbs the cached prompt prefix.
    """
    global _available_models, _models_discovered_at, _models_refreshing
    import time as _t

    if _models_discovered_at is None:
        if not block:
            return
        with _models_refresh_lock:
            if _models_discovered_at is None:
                _available_models = discover_models()
                # discover_models returns [] when Bedrock is unreachable: leave the catalog
                # "never discovered" so the next blocking caller (the first request, when this
                # was warm-up) tries again rather than serving an empty list for hours.
                if _available_models:
                    _models_discovered_at = _t.time()
        return
    if _t.time() - _models_discovered_at < _MODEL_CATALOG_MAX_AGE_S:
        return
    with _models_refresh_lock:
        if _models_refreshing:
            return
        _models_refreshing = True

    def _refresh() -> None:
        global _available_models, _models_discovered_at, _models_refreshing, _catalog_cache_valid
        try:
            fresh = discover_models()
            # discover_models returns [] on failure: keep the list we have rather than empty it.
            if fresh:
                if fresh != _available_models:
                    _available_models = fresh
                    _catalog_cache_valid = False
                _models_discovered_at = _t.time()
        except Exception:  # noqa: BLE001 - the cached catalog keeps serving
            logger.warning("background model-catalog refresh failed", exc_info=True)
        finally:
            _models_refreshing = False

    threading.Thread(target=_refresh, name="model-catalog-refresh", daemon=True).start()


def warm_up() -> None:
    """Container-start warm-up, run before ``app.run()``. Never raises."""
    import time as _t

    started = _t.monotonic()
    try:
        _warm_up_static()
        _ensure_model_catalog(block=True)
    except Exception:  # noqa: BLE001 - the lazy first-request path covers whatever failed
        logger.warning("container warm-up failed; the first request initializes", exc_info=True)
    logger.info("Container warm-up finished in %.2fs", _t.monotonic() - started)


def _start_skills_load():
    """Start the S3 + bundled skill load on a worker thread, once per container."""
    global _skills_load_future
    with _skills_load_lock:
        if _skills_load_future is None:
            from concurrent.futures import Future

            fut: Future = Future()

            def _run() -> None:
                try:
                    fut.set_result(skills_manager.create_plugin())
                except BaseException as exc:  # noqa: BLE001 - re-raised to the joiner
                    fut.set_exception(exc)

            threading.Thread(target=_run, name="skills-load", daemon=True).start()
            _skills_load_future = fut
        return _skills_load_future


def _finish_skills_load(plugin) -> None:
    """Build the process-global Agent around the loaded skills plugin (on the caller's thread)."""
    global skills_plugin, agent
    with _INIT_LOCK:
        if agent is not None:
            return
        skills_plugin = plugin
        agent = Agent(model=model, system_prompt="", tools=[], plugins=[skills_plugin],
                      hooks=[harness_hook_provider])
        skills_manager.set_agent(agent)
        mcp_manager.set_agent(agent)
        logger.info("Agent subsystems initialized")


def _ensure_skills_ready() -> None:
    """Join the skills load synchronously (for callers outside the event loop)."""
    if agent is not None:
        return
    _finish_skills_load(_start_skills_load().result())


async def _await_skills_ready() -> None:
    """Join the skills load without blocking the event loop, then build the global Agent."""
    if agent is not None:
        return
    plugin = await asyncio.wrap_future(_start_skills_load())
    _finish_skills_load(plugin)


def _init_agent_subsystems(*, wait_for_skills: bool = True):
    """One-time construction of boto clients, Agent, managers, registries.

    Called on the first invocation (inside the @app.entrypoint handler). The user-independent
    part normally already ran at container start (``warm_up``); this runs it when it did not.
    The skill load then starts on a worker thread. ``wait_for_skills=False`` returns without
    joining it -- the entrypoint overlaps it with the gateway connects and joins it with
    ``_await_skills_ready`` -- while the default joins, so any other caller gets a fully built
    runtime on return, as before.
    """
    global _AGENT_SUBSYSTEMS_READY
    if not _AGENT_SUBSYSTEMS_READY:
        with _INIT_LOCK:
            if not _AGENT_SUBSYSTEMS_READY:
                try:
                    _warm_up_static()
                    _start_skills_load()
                    _construct_per_container()
                finally:
                    _AGENT_SUBSYSTEMS_READY = True
    if wait_for_skills:
        _ensure_skills_ready()


def _attach_turn_lineage(summary: dict) -> None:
    """Add the turn's ``lineage`` to the ``execution_summary`` frame (asset-lineage design §3/§4).

    ``[{output, inputs, tool}]`` in canonical ``s3://bucket/key`` form, from the per-turn ledger
    the hook provider fills (``media.turn_outputs``). The proxy keeps the summary frame verbatim
    and forwards ``lineage`` in the Generated_Media turn message, so the registrar can record
    ``derivedFrom``. Absent when the turn wrote nothing. Never raises.
    """
    try:
        from media import turn_outputs
        lineage = turn_outputs.lineage()
        if lineage:
            summary["lineage"] = lineage
    except Exception:  # noqa: BLE001 - a summary must still be emitted without lineage
        logger.warning("attaching lineage to execution_summary failed", exc_info=True)


def _build_system_prompt(inventory_snapshot=None, prompt: str = "", session_id: str = ""):
    """Build system prompt with awareness of available skills/models/inventory.

    The full tool list is intentionally NOT inlined — Strands sends tool
    specs in the request body. Inventory is conditional on prompt content
    to keep the system prompt small for non-asset queries. ``session_id`` adds this turn's
    session-scoped output location (the same guidance the task agent gets).
    """
    catalog = _build_catalog()
    catalog_text = _format_catalog_text(catalog)
    inventory_text = ""
    # Only inject inventory when the user prompt suggests they're operating
    # on assets. The Strands tool list still advertises *all* lab tools so
    # the model can list/read on demand even without inventory in the prompt.
    if inventory_snapshot is not None and _prompt_mentions_assets(prompt):
        inv = main_lab_tools.render_inventory_section(inventory_snapshot)
        if inv:
            inventory_text = "\n## Lab Inventory (your assets)\n" + inv + "\n"

    return build_orchestrator_system_prompt(
        catalog_text, inventory_text, S3_BUCKET, S3_PREFIX, UPLOADS_BUCKET, MEDIA_PROXY_BUCKET,
        session_output_guidance=build_session_output_guidance(
            session_id, S3_BUCKET, S3_PREFIX, REGION, ARTIFACTS_DOMAIN,
            project_id=jobs_context.trusted_project_id()),
    )


LAB_INVENTORY_HEADING = "## Lab Inventory (your assets)"


def _build_turn_prompt(inventory_snapshot=None, prompt: str = "", session_id: str = ""):
    """This turn's prompt, split by how often each part changes: ``(stable, session, turn)``.

    * ``stable`` -- the orchestrator instructions plus the tool/model catalog: the same for every
      session of a deployment that sees the same gateways, so it is cached ACROSS sessions.
    * ``session`` -- the session-scoped output guidance (this session's id and prefix): constant
      within a session; placed after the system cache point (see ``_request_system_prompt``).
    * ``turn`` -- the Lab inventory hint, present only when THIS prompt mentions assets. It used to
      sit inside the system prompt, so a turn that mentioned "skill" and the next that did not had
      different system prompts and the whole cached history behind them was rewritten. It is now
      per-call context folded in after the last cache point (``ContextInjector``), like memory.

    The text of each part is what ``_build_system_prompt`` assembles (that function keeps the
    single-string form for its other callers).
    """
    catalog = _build_catalog()
    catalog_text = _format_catalog_text(catalog)
    stable = build_orchestrator_system_prompt(
        catalog_text, "", S3_BUCKET, S3_PREFIX, UPLOADS_BUCKET, MEDIA_PROXY_BUCKET,
        session_output_guidance="",
    )
    session = build_session_output_guidance(
        session_id, S3_BUCKET, S3_PREFIX, REGION, ARTIFACTS_DOMAIN,
        project_id=jobs_context.trusted_project_id())
    turn = ""
    if inventory_snapshot is not None and _prompt_mentions_assets(prompt):
        inv = main_lab_tools.render_inventory_section(inventory_snapshot)
        if inv:
            turn = f"{LAB_INVENTORY_HEADING}\n{inv}"
    return stable, session, turn


def _model_caches_prompts(m) -> bool:
    """Whether ``m`` places Anthropic-style cache points (strategy ``auto`` resolved to anthropic)."""
    try:
        return getattr(m, "_cache_strategy", None) == "anthropic"
    except Exception:  # noqa: BLE001 - unknown model shape: no caller-placed points
        return False


def _request_system_prompt(stable: str, session: str, search_prompt: str, *, cacheable: bool):
    """The per-request system prompt: ``[stable, cachePoint, session + search]`` when cacheable.

    One cache point after the stable part (Strands then adds none of its own: it only appends one
    when the prompt has none), so the instructions + catalog are read from cache by every new
    session, while the session/turn-specific tail -- and the skills XML the AgentSkills plugin
    appends after it -- is covered by the message cache points. Plain text otherwise.
    """
    tail = f"{session or ''}{search_prompt or ''}"
    if not cacheable:
        return f"{stable}{tail}"
    blocks = [{"text": stable}, {"cachePoint": {"type": "default"}}]
    if tail:
        blocks.append({"text": tail})
    return blocks


def _compose_turn_context(*parts: str) -> str:
    return "\n\n".join(p.strip() for p in parts if isinstance(p, str) and p.strip())


try:  # Strands' per-call context injection (folds text into the latest user message, after its cache point)
    from strands.vended_plugins.context_injector import ContextInjector as _ContextInjector
except Exception:  # noqa: BLE001 - older strands: memory/inventory stay where they were
    _ContextInjector = None


def _build_catalog() -> dict:
    """Build a structured catalog of all available tools, skills, and models.

    The orchestrator only registers built-ins + lab CRUD on its own
    ``tool_use`` payload — gateway tools are loaded per-dispatch by
    sub-agents to keep the orchestrator's request body small. We still
    surface the gateway tool *catalog* here, grouped by gateway, so
    callers (notably ``_format_catalog_text`` and ``plan_and_execute_tool``'s
    Planning Agent) can advertise what's reachable via ``dispatch_tool``.

    Results are cached and only rebuilt when ``_catalog_cache_valid`` is
    cleared (see ``_ensure_gateway_connected``).
    """
    global _catalog_cache_valid, _cached_catalog_dict, _cached_catalog_text_str
    if _catalog_cache_valid and _cached_catalog_dict is not None:
        return _cached_catalog_dict
    tools = []
    for name, obj in _all_builtin_tools.items():
        desc = ""
        if hasattr(obj, "TOOL_SPEC"): desc = obj.TOOL_SPEC.get("description", "")[:120]
        elif hasattr(obj, "tool_spec"): desc = obj.tool_spec.get("description", "")[:120]
        tools.append({"name": name, "description": desc})
    gateway_tools: list[dict] = []
    partial_sources: list[str] = []
    for gw in mcp_manager.list_gateways():
        if not gw["connected"]:
            # A registered gateway we could not reach this build. The Public
            # Capability View must report availability as UNCONFIRMED rather than
            # silently omitting it (Req 4.2). Use a generic, non-leaking label —
            # never the gateway id/name.
            partial_sources.append("external connectors")
            continue
        for t in gw["tools"]:
            entry = {
                "name": t["name"],
                "description": (t.get("description") or "")[:120],
                "gateway_id": gw["id"],
                "gateway_name": gw["name"],
                "auth_type": gw.get("auth_type"),
            }
            tools.append(entry)
            gateway_tools.append(entry)
    skills = [{"name": s["name"], "description": s["description"]} for s in skills_manager.list_skills()]
    models = [{"id": m["id"], "name": m["name"],
               "supportsEffort": bool(m.get("supportsEffort"))} for m in _available_models]
    result = {"tools": tools, "gateway_tools": gateway_tools, "skills": skills, "models": models, "partial_sources": sorted(set(partial_sources))}
    _cached_catalog_dict = result
    # Clear cached text before calling _format_catalog_text so its identity
    # check (catalog is _cached_catalog_dict) doesn't short-circuit with
    # stale text from a previous cache cycle.
    _cached_catalog_text_str = ""
    _cached_catalog_text_str = _format_catalog_text(result)
    _catalog_cache_valid = True
    return result


def get_catalog() -> dict:
    """Public accessor for the current tool/skill/model catalog. Used by plan_and_execute_tool."""
    return _build_catalog()


def _public_capabilities_text() -> str:
    """Sanitized, user-facing summary of what the harness can do right now.

    Backs the ``get_available_capabilities`` tool. Kept as a plain function so it
    is unit-testable without the Strands tool wrapper, and reuses the cached
    ``_build_catalog()`` so it adds no extra gateway round-trips. Every surfaced
    string is passed through ``help_capabilities``' internal-identifier guard, so
    no ``target___tool`` name, ARN, bucket, path, or prohibited tool can leak.
    """
    from help_capabilities import build_public_capability_view, render_public_capabilities

    catalog = _build_catalog()
    view = build_public_capability_view(
        skills=catalog.get("skills") or (),
        external_tools=catalog.get("gateway_tools") or (),
        partial_sources=catalog.get("partial_sources") or (),
    )
    return render_public_capabilities(view)


@tool
def get_available_capabilities() -> str:
    """Summarize, in plain user-facing language, what this assistant can do right
    now in THIS deployment: its foundational abilities plus the Skills and
    external capabilities currently available. Call this when the user asks what
    you can do, what's available, what's enabled, or how to get started, so the
    answer reflects the live environment instead of a guess. The result contains
    only friendly capability names and never exposes internal tool identifiers.
    """
    return _public_capabilities_text()


_INVENTORY_TRIGGER_KEYWORDS = (
    "skill", "specialty", "agent", "tool collection", "tool-collection",
    "workflow", "draft", "drafts", "publish", "edit", "version", "lab",
    "discard", "fork", "inventory", "what do i have", "what i have",
    "list mine", "my work", "delete my", "what about my", "my own",
    "my published", "what skills", "do you have",
)


def _prompt_mentions_assets(prompt: str) -> bool:
    """Cheap keyword check to decide whether to inject the inventory section.

    The full Strands tool list still includes every lab tool, so the
    model can call list_skill_drafts on demand even when this returns
    False. The inventory section is just a hint for cases where the
    user references assets by name.
    """
    if not prompt:
        return False
    p = prompt.lower()
    return any(kw in p for kw in _INVENTORY_TRIGGER_KEYWORDS)


def _format_catalog_text(catalog: dict) -> str:
    """Format catalog dict as text for the system prompt.

    Built-in tools are intentionally omitted from this text — Strands
    already serializes their full spec into the Bedrock request body via
    ``tool_use``. Repeating them in the prompt wastes tokens.

    Skills are also omitted — the Strands ``AgentSkills`` plugin injects
    them as ``<available_skills>`` XML, so duplicating them here wastes
    tokens.

    We keep two sections:
      * Gateway tools — loaded per-dispatch by sub-agents (not on the
        orchestrator's ``tool_use`` payload), so the orchestrator can
        only know about them through the prompt. Grouped by gateway so
        the model can describe what each gateway provides and pass the
        right tool name(s) to ``dispatch_tool``.
      * Models — the catalog of inference profiles is consumed by
        ``dispatch_tool``'s ``model_id`` parameter, which the model
        chooses per-call. Without listing them, the model can't pick
        a non-default model.

    Note: When called from ``_build_catalog``, the result is cached in
    ``_cached_catalog_text_str``. Direct callers that pass the cached
    catalog dict get the cached text via identity check.
    """
    # Fast path: if the catalog is the same object we already formatted, reuse.
    if catalog is _cached_catalog_dict and _cached_catalog_text_str:
        return _cached_catalog_text_str
    model_lines = [
        f"  - {m['id']} ({m['name']})"
        for m in catalog.get("models", [])
    ]

    # Group gateway tools by display name so the model sees one section
    # per gateway. Keep the per-tool description trimmed (already capped
    # at 120 chars in _build_catalog) so the prompt stays bounded even
    # with a dozen gateways.
    gateway_groups: dict[str, list[dict]] = {}
    for t in catalog.get("gateway_tools", []):
        gw_name = t.get("gateway_name") or t.get("gateway_id") or "Unnamed gateway"
        gateway_groups.setdefault(gw_name, []).append(t)

    text = ""
    if gateway_groups:
        text += (
            "## Gateway tools (call via dispatch_tool)\n"
            "These tools are reachable through `dispatch_tool` — the "
            "sub-agent will load them on demand. Each entry uses markdown "
            "link form `[short_name](canonical://full_name)`: the bracketed "
            "short_name is what you DISPLAY to the user, the canonical:// "
            "value is what you PASS to dispatch_tool's `tools=[...]` "
            "argument.\n\n"
        )
        for gw_name, tools in gateway_groups.items():
            text += f"### {gw_name}\n"
            for t in tools:
                desc = t.get("description") or ""
                full = t["name"]
                short = full.rsplit("___", 1)[-1] if "___" in full else full
                text += f"  - [{short}](canonical://{full}): {desc}\n"
            text += "\n"
    if model_lines:
        text += (
            "## Models — dispatch_tool INHERITS the session model; omit `model_id`\n"
            "Listed only so an explicit user request can be honoured. Naming one\n"
            "here OVERRIDES the user's own selection, so do not pick from this list\n"
            "unless they asked.\n" + "\n".join(model_lines) + "\n\n"
        )
    return text


# Header carrying the end user's token, forwarded proxy -> runtime. MUST match
# USER_TOKEN_HEADER in infra/lib/agent-infra-stack.ts and the runtime's
# RequestHeaderAllowlist; a header that is not allowlisted is dropped silently.
USER_TOKEN_HEADER = os.environ.get(
    "USER_TOKEN_HEADER", "X-Amzn-Bedrock-AgentCore-Runtime-Custom-UserToken"
)


def _bind_turn_user_token(agentcore_context) -> str:
    """Resolve the forwarded token AND bind it for this invocation.

    Binding matters because dispatch happens deep inside a turn: the sub-agent
    runtime is invoked from a tool, far from the entrypoint that has the request
    headers. Without this the sub-agent loaded gateways with no credential, so
    the user_jwt gateway failed there while working in the main agent -- the
    catalog listed the user's tools and the executing agent could not reach them.
    """
    token = _resolve_user_token(agentcore_context)
    try:
        from jobs.context import set_user_token
        set_user_token(token)
    except Exception as e:  # never let telemetry plumbing break a turn
        logger.warning("could not bind user token to context: %s", e)
    return token


def _resolve_user_token(agentcore_context) -> str:
    """Pull the forwarded end-user token off the request context.

    Returns "" when absent, which is not an error: only gateways whose
    auth_type is "user_jwt" need it, and those fail loudly at connect. Header
    names are case-insensitive and the runtime makes no promise about casing.
    """
    headers = getattr(agentcore_context, "request_headers", None)
    if not isinstance(headers, dict):
        return ""
    wanted = USER_TOKEN_HEADER.lower()
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == wanted:
            if isinstance(value, str):
                token = value.strip()
                # Tolerate either a bare token or a Bearer-prefixed one.
                if token.lower().startswith("bearer "):
                    token = token[7:].strip()
                return token
            return ""
    return ""


async def _connect_delegated_gateways(actor_id: str) -> None:
    """Arm the delegated connect for ``user_jwt`` gateways on a TOKEN-LESS
    invocation (e.g. the Slack ingress, which invokes via IAM/SigV4 with no user
    JWT). ``connect_all`` defers ``user_jwt`` gateways when no user token is
    forwarded, so their tools (e.g. fal) never load. When delegation grants are
    enabled and this actor can delegate, mint a grant for the actor and connect
    those gateways with the async-plan machine token as the front-door credential
    and the grant as the interceptor's actor proof — mirroring the Task Agent's
    async-plan connect, but MINTING the grant here because an interactive turn has
    no upstream plan to supply one.

    No-op (user_jwt gateways stay deferred — unchanged behavior) unless delegation
    is enabled, the actor is grant-eligible, and the async-plan machine creds are
    configured. Best-effort: a connect failure must never break startup.
    """
    if not actor_id:
        return
    try:
        from security import delegation
        if not (delegation.grants_enabled() and delegation.can_delegate(actor_id)):
            return
        if not (ASYNC_PLAN_TOKEN_ENDPOINT and ASYNC_PLAN_CLIENT_ID and ASYNC_PLAN_CLIENT_SECRET):
            logger.warning(
                "delegated connect skipped: async-plan machine creds are not "
                "configured (ASYNC_PLAN_TOKEN_ENDPOINT / ASYNC_PLAN_CLIENT_ID / "
                "ASYNC_PLAN_CLIENT_SECRET); user_jwt gateways stay deferred"
            )
            return
        # plan_id is bound into the grant purely for traceability here (there is
        # no plan); a stable per-actor value keeps the interceptor's actor proof
        # correct while making the grant's origin legible in logs.
        grant = delegation.mint_grant(actor_id, f"main-{actor_id}")
        if not grant:
            logger.warning("delegated connect: mint_grant returned nothing for actor=%s", actor_id[:8])
            return
        results = await asyncio.to_thread(
            lambda: mcp_manager.bind_delegation_grant_and_connect(
                grant,
                token_endpoint=ASYNC_PLAN_TOKEN_ENDPOINT,
                client_id=ASYNC_PLAN_CLIENT_ID,
                client_secret=ASYNC_PLAN_CLIENT_SECRET,
                scope=ASYNC_PLAN_SCOPE,
            )
        )
        statuses = {
            gw: (r.get("status") if isinstance(r, dict) else r)
            for gw, r in (results or {}).items()
        }
        logger.info(
            "Main agent armed delegated connect for user_jwt gateways (actor=%s): %s",
            actor_id[:8], statuses,
        )
    except Exception as e:  # noqa: BLE001 — never break startup on a connect failure
        logger.warning("Main agent delegated gateway connect failed (actor=%s): %s", actor_id[:8], e)


async def _ensure_gateway_connected(actor_id: str = "", user_token: str = ""):
    global _gateway_initialized, _available_models, _catalog_cache_valid
    if _gateway_initialized:
        # The latch below is per container, but the user's token arrives per
        # invocation and not on every one: management calls (listing MCP
        # servers, fetching the catalog) carry none. Whichever call cold-starts
        # the container therefore decides whether the user_jwt gateway gets a
        # credential, and without this retry a tokenless first call left it
        # failed for the container's entire life -- so the chat turn that DID
        # forward a token found the work already marked done and never saw the
        # user's tools. Cheap and idempotent: a no-op without a token, and a
        # no-op when those gateways are already connected.
        if user_token:
            retried = mcp_manager.bind_user_token_and_connect(user_token)
            if retried:
                _catalog_cache_valid = False
        return
    _gateway_initialized = True
    # The model catalog normally comes from container warm-up (snapshotted). Without one, it is
    # discovered on a worker thread WHILE the gateways below load and connect, and joined before
    # the catalog cache is invalidated -- previously a blocking call ahead of all of them.
    _models_task = None
    if _models_discovered_at is None:
        _models_task = asyncio.ensure_future(
            asyncio.to_thread(_ensure_model_catalog, block=True)
        )
    else:
        _ensure_model_catalog(block=False)  # background refresh only when stale; never waits
    # Cold-load the gateway set from the DDB registry (R6). The legacy
    # `GATEWAYS` env-var path was retired in task 6.3, so this is the
    # only loader. If the registry singleton isn't configured (no
    # `GATEWAY_REGISTRY_TABLE` env var, e.g. dev runs against a stack
    # without the table), skip the load and let the runtime come up
    # with zero gateways — gateway tools will be empty until an admin
    # registers one via `gateway_register`.
    #
    # `actor_id` is asserted to the gateway on every request over these
    # connections so its interceptor can scope user-registered MCP tools to
    # this caller. Binding it once here is correct because AgentCore gives
    # each session its own microVM and a session belongs to one user, so this
    # container serves one actor for its lifetime.
    if _gateway_registry is not None:
        try:
            loaded_ids = await mcp_manager.load_gateways_from_registry(
                _gateway_registry, actor_id=actor_id, user_token=user_token
            )
            logger.info("Loaded %d gateway(s) from registry: %s", len(loaded_ids), loaded_ids)
        except Exception as e:
            logger.error("Failed to load gateways from registry: %s", e)
    else:
        logger.warning("Gateway registry unavailable; starting with zero gateways")

    # Additively connect the shared Platform gateway (AWS_IAM/SigV4, plane=platform) if
    # provisioned. Inert until shared targets are attached; never blocks the tenant path.
    try:
        _pgid = await mcp_manager.load_platform_gateway_from_env(actor_id=actor_id, user_token=user_token)
        if _pgid:
            logger.info("Loaded platform gateway: %s", _pgid)
    except Exception as e:
        logger.error("Failed to load platform gateway: %s", e)

    # Publish the builtin projection for the browse catalog. Done after the
    # gateway load so the recorded `state` reflects the final tool set. A
    # best-effort DynamoDB write whose result nothing on this path reads, so it
    # runs on a daemon thread instead of holding up the connects.
    threading.Thread(
        target=lambda: _publish_builtin_catalog(), name="publish-builtin-catalog", daemon=True
    ).start()
    try:
        # Off the event loop (a blocking OAuth + MCP handshake per gateway, run
        # concurrently inside connect_all), so the progress poller keeps going.
        results = await asyncio.to_thread(mcp_manager.connect_all)
        for gw_id, result in results.items():
            s = result.get("status", "unknown")
            if s == "connected": logger.info("Gateway '%s': connected with %d tools", gw_id, len(result.get("tools", [])))
            else: logger.warning("Gateway '%s': %s", gw_id, result)
    except Exception as e:
        logger.error("Failed to connect gateways: %s", e)
    if _models_task is not None:
        await _models_task
    # Invalidate catalog cache so the next _build_catalog() picks up
    # newly connected gateway tools and freshly discovered models.
    _catalog_cache_valid = False
    # Wire per-tool job tracking (async-job-tracking task 5.3) AFTER
    # ``connect_all`` so the per-gateway detection caches are
    # populated — :func:`jobs.strands_tool_wrap.wrap_trio_tools` reads
    # those caches when deciding which tools to wrap. Skipped when
    # the registry singleton is unwired (no JOB_REGISTRY_TABLE env
    # var) so dev environments without the registry table still come
    # up cleanly.
    if _job_registry is not None:
        try:
            # Sits on the path Strands' ``Agent`` actually invokes
            # (:meth:`MCPAgentTool.stream`) — replaces each
            # trio-member tool with a tracking subclass. The wrap
            # runs over every already-connected gateway's tools and
            # re-applies on subsequent connect / refresh cycles.
            mcp_manager.set_job_tracking(
                _job_registry,
                actor_id_resolver=jobs_context.current_actor_id,
                session_id_resolver=jobs_context.current_session_id,
            )
        except Exception as exc:
            # Wiring failure is non-fatal — the agent can still serve
            # non-job-bearing tool calls. Log loudly so the operator
            # knows job tracking is offline.
            logger.error(
                "Failed to install per-tool job tracking: %s; "
                "async job tracking will be inactive on this runtime",
                exc,
            )
    # Token-less but grant-eligible invocations (Slack IAM/SigV4) forward no user
    # JWT, so connect_all() above deferred the user_jwt gateway(s) and their tools
    # (fal) never loaded. Mint a grant and arm the delegated connect now — AFTER
    # set_job_tracking so its per-gateway job-tracking wrap applies to the
    # freshly-connected trio tools, and BEFORE _rebuild_agent_tools below so those
    # tools make it into the agent's registry on this cold-start. No-op with a
    # user token (the UI path) or when delegation/creds are unavailable.
    if not user_token:
        await _connect_delegated_gateways(actor_id)
    # The skills (S3-authored ones load per container) were loading on a worker
    # thread during all of the above; the catalog and the global Agent need them.
    await _await_skills_ready()
    agent.system_prompt = _build_system_prompt()
    _rebuild_agent_tools()
    logger.info("System prompt and tools initialized")


def _rebuild_agent_tools(gateway_tools: Optional[list] = None):
    added_bi = [t for n, t in _all_builtin_tools.items() if n in _added_builtins]
    gw_tools = mcp_manager.get_all_added_tools() if gateway_tools is None else gateway_tools
    if hasattr(agent.tool_registry, "registry"): agent.tool_registry.registry.clear()
    elif hasattr(agent.tool_registry, "_tools"): agent.tool_registry._tools.clear()
    all_t = added_bi + gw_tools
    if all_t: agent.tool_registry.process_tools(all_t)
    logger.info("Rebuilt agent tools: %d built-in + %d gateway", len(added_bi), len(gw_tools))


def _get_builtin_tools():
    tools = []
    for name, obj in _all_builtin_tools.items():
        desc = ""
        if hasattr(obj, "TOOL_SPEC"): desc = obj.TOOL_SPEC.get("description", "")
        elif hasattr(obj, "tool_spec"): desc = obj.tool_spec.get("description", "")
        elif hasattr(obj, "description"): desc = obj.description or ""
        if name in _trusted_builtins: state = "trusted"
        elif name in _added_builtins: state = "available"
        else: state = "not_added"
        tools.append({"name": name, "description": desc, "state": state})
    return tools


# Registry keys for the browse catalog. Kept beside the gateway descriptors
# (PK GATEWAY#REGISTERED) in the same table rather than in a new one.
_CATALOG_PK = "GATEWAY#CATALOG"
_CATALOG_BUILTIN_SK = "__builtin__"
_catalog_table = None


def _publish_builtin_catalog() -> bool:
    """Persist the builtin-tool projection so `/catalog` need not wake this runtime.

    Loading the tool catalogue used to cost a full runtime invocation: measured
    at 8.6s cold versus 0.2s on an already-warm session, because the proxy sends
    no session id and therefore cold-starts every time. Gateway tools can be read
    straight from the AgentCore control plane, but builtin specs exist ONLY in
    this process — so without publishing them the runtime stays on the read path
    and nothing is gained.

    Builtins change only when this runtime is redeployed, so a single write at
    startup is naturally correct. It is re-published when an admin toggles a
    builtin, because the projection carries each tool's added/trusted `state`.

    Best-effort by design: a failure here must never stop the agent from
    starting, and the reader falls back to invoking the runtime.
    """
    table = globals().get("_catalog_table")
    if table is None:
        return False
    try:
        tools = _get_builtin_tools()
        table.put_item(Item={
            "PK": _CATALOG_PK,
            "SK": _CATALOG_BUILTIN_SK,
            "tools": tools,
            "tool_count": len(tools),
            "published_at": datetime.now(timezone.utc).isoformat(),
        })
        logger.info("Published builtin catalog: %d tool(s)", len(tools))
        return True
    except Exception as exc:  # noqa: BLE001 - never block startup on a cache write
        logger.warning("Could not publish builtin catalog: %s", exc)
        return False


# Agent construction is deferred to _init_agent_subsystems() above.


def _stream_error_text(err: object) -> str:
    """User-facing explanation for a failed turn.

    The client ends a turn only when it receives terminal output, so a failure that
    is merely logged leaves the step indicator running forever — a turn that had
    already died looked like one still working an hour later.

    Kept generic: it reports what the model reported, and only distinguishes the
    output-limit case because the remedy there is different (continue or split the
    request, rather than retry).
    """
    text = str(err or "").strip()
    lowered = text.lower()
    if "maximum token limit" in lowered or "maxtokensreached" in lowered:
        return (
            "\n\n**This turn stopped at the model's output token limit.** The partial "
            "response is kept in the conversation, so nothing is lost — ask me to "
            "continue, or split the request into smaller steps.\n"
        )
    detail = text[:400] if text else "the model stream ended unexpectedly"
    return f"\n\n**This turn failed:** {detail}\n"


def _step_event(status: str, label: str) -> str:
    return json.dumps({"type": "step", "status": status, "label": label})


def _message_stop_reason(event) -> str:
    """``stopReason`` of a raw Bedrock ``messageStop`` chunk forwarded by Strands, else ``""``."""
    raw = event.get("event") if isinstance(event, dict) else None
    if not isinstance(raw, dict):
        return ""
    stop = raw.get("messageStop")
    if not isinstance(stop, dict):
        return ""
    reason = stop.get("stopReason")
    return reason if isinstance(reason, str) else ""


TURN_START_LABEL = "Working…"
TURN_READY_LABEL = "Ready"


class _TurnStartStep:
    """The step a chat turn emits the moment it starts, closed in place on first real progress.

    ``open()`` returns a ``running`` frame; ``close()`` returns the matching ``done`` frame (same
    ``node_id``, so the UI and the proxy transcript REPLACE the entry instead of adding one, and
    the collapsed step label moves on to the next running step) with the time it took, exactly
    once. Both return ``""`` when disabled or already used, so call sites need no guards.
    """

    def __init__(self, *, enabled: bool) -> None:
        import time as _t
        import uuid as _u

        self._enabled = bool(enabled)
        self._open = False
        self._node_id = f"turn-start-{_u.uuid4().hex[:12]}"
        self._started = _t.monotonic()

    def open(self) -> str:
        if not self._enabled or self._open:
            return ""
        self._open = True
        return json.dumps({"type": "step", "status": "running", "label": TURN_START_LABEL,
                           "node_id": self._node_id})

    def close(self) -> str:
        if not self._open:
            return ""
        import time as _t

        self._open = False
        self._enabled = False
        return json.dumps({"type": "step", "status": "done", "label": TURN_READY_LABEL,
                           "node_id": self._node_id,
                           "duration_ms": int((_t.monotonic() - self._started) * 1000)})


def _sanitize_tool_pairs(messages: list) -> int:
    """Repair mismatched toolUse/toolResult pairing in restored session history.

    Bedrock's Converse API requires that every ``toolUse`` block in an assistant
    message is answered by exactly one ``toolResult`` block (same ``toolUseId``)
    in the immediately following message, and it rejects any ``toolResult`` that
    lacks a matching ``toolUse`` in the preceding message ("The number of
    toolResult blocks at messages.N.content exceeds the number of toolUse blocks
    of previous turn").

    An interrupted or aborted tool call can persist a message list that violates
    this — an orphan/duplicate ``toolResult``, or a ``toolUse`` with no result.
    Strands' own restore-time repair is best-effort and can still leave an
    invalid sequence, which then hard-fails EVERY subsequent turn on that
    session (the model returns only an execution summary, no text/tools). This
    normalizes the pairing in place so a corrupted session self-heals instead of
    dead-ending. It is non-destructive to the durable store: it only fixes the
    in-memory list used for the current invocation.

    Returns the number of blocks synthesized or dropped (for logging).
    """
    def _blocks(m) -> list:
        c = m.get("content") if isinstance(m, dict) else None
        return c if isinstance(c, list) else []

    def _use_ids(msg) -> list:
        ids = []
        for b in _blocks(msg):
            if isinstance(b, dict) and isinstance(b.get("toolUse"), dict):
                uid = b["toolUse"].get("toolUseId")
                if uid:
                    ids.append(uid)
        return ids

    changed = 0
    i = 0
    while i < len(messages):
        msg = messages[i]
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            i += 1
            continue
        use_ids = _use_ids(msg)
        if not use_ids:
            i += 1
            continue

        nxt = messages[i + 1] if i + 1 < len(messages) else None
        nxt_is_results_turn = isinstance(nxt, dict) and nxt.get("role") in ("user", "tool")
        present: dict = {}
        if nxt_is_results_turn:
            for b in _blocks(nxt):
                if isinstance(b, dict) and isinstance(b.get("toolResult"), dict):
                    uid = b["toolResult"].get("toolUseId")
                    if uid is not None:
                        present.setdefault(uid, b)

        # Build a 1:1 toolResult set matching this assistant's toolUse ids,
        # deduped and ordered, synthesizing an error result for any unanswered
        # (interrupted) call.
        want = set(use_ids)
        new_results = []
        seen = set()
        for uid in use_ids:
            if uid in seen:
                continue
            seen.add(uid)
            if uid in present:
                new_results.append(present[uid])
            else:
                new_results.append({
                    "toolResult": {
                        "toolUseId": uid,
                        "status": "error",
                        "content": [{"text": "Tool was interrupted."}],
                    }
                })
                changed += 1

        if nxt_is_results_turn:
            nxt_blocks = _blocks(nxt)
            non_results = [
                b for b in nxt_blocks
                if not (isinstance(b, dict) and isinstance(b.get("toolResult"), dict))
            ]
            # Count orphan/duplicate toolResults we are dropping.
            kept_ids = set()
            for b in nxt_blocks:
                if isinstance(b, dict) and isinstance(b.get("toolResult"), dict):
                    uid = b["toolResult"].get("toolUseId")
                    if uid in want and uid not in kept_ids:
                        kept_ids.add(uid)
                    else:
                        changed += 1
            # toolResult blocks must lead the user turn per Bedrock; keep the
            # matched results first, then any trailing non-result content.
            nxt["content"] = new_results + non_results
            i += 2
        else:
            # No following results turn at all — inject one so the dangling
            # toolUse(s) are satisfied before the conversation continues.
            messages.insert(i + 1, {"role": "user", "content": new_results})
            i += 2


    # SECOND PASS — orphan toolResults whose predecessor has NO toolUse at all.
    # The loop above only ever inspects the message FOLLOWING an assistant message
    # that contains toolUse, so a toolResult sitting after a text-only assistant
    # turn (or after another user turn) is never visited. That is precisely what
    # pressing Stop mid-tool-call can persist, and it bricked a session
    # permanently: every later turn failed with "The number of toolResult blocks at
    # messages.N.content exceeds the number of toolUse blocks of previous turn",
    # including retries, because the corruption is in the restored history rather
    # than in the new turn.
    for j, m in enumerate(messages):
        if not isinstance(m, dict):
            continue
        blocks = _blocks(m)
        if not any(isinstance(b, dict) and isinstance(b.get("toolResult"), dict) for b in blocks):
            continue
        prev_ids = set(_use_ids(messages[j - 1])) if j > 0 else set()
        keep = []
        seen = set()
        for b in blocks:
            if isinstance(b, dict) and isinstance(b.get("toolResult"), dict):
                uid = b["toolResult"].get("toolUseId")
                # Keep only a result the immediately preceding turn actually asked
                # for, and only once.
                if uid in prev_ids and uid not in seen:
                    seen.add(uid)
                    keep.append(b)
                else:
                    changed += 1
            else:
                keep.append(b)
        if keep:
            m["content"] = keep
        else:
            # Bedrock rejects an empty content list, and dropping the message
            # would break role alternation, so leave a truthful placeholder.
            m["content"] = [{"text": "(interrupted tool results removed)"}]

    return changed


def _handle_user_mcp_action(action: str, payload: dict, context) -> dict:
    """Register / remove / poll a user's own remote MCP server.

    Not admin-gated, unlike the ``gateway_*`` actions: these are the caller's own
    servers, scoped to their identity. The actor comes from the validated JWT
    rather than the payload, so a caller cannot register a server as, or delete
    one belonging to, anyone else. ``user_mcp`` re-checks ownership against the
    recorded row for the same reason.
    """
    import gateway.user_mcp as user_mcp

    try:
        actor_id = _resolve_actor_id(context, payload=payload)
    except RuntimeError as exc:
        return {"error": "actor_unresolved", "message": str(exc)}

    try:
        if action == "user_mcp_register":
            return user_mcp.register_user_mcp_server(
                actor_id=actor_id,
                server_name=payload.get("name", ""),
                url=payload.get("url", ""),
                token=payload.get("token", ""),
                header_name=payload.get("header_name", "Authorization"),
                value_prefix=payload.get("value_prefix", "Bearer "),
                header_value=payload.get("header_value", ""),
                description=payload.get("description", ""),
            )
        if action == "user_mcp_delete":
            return user_mcp.deregister_user_mcp_server(
                actor_id=actor_id,
                target_name=payload.get("target_name", ""),
                target_id=payload.get("target_id", ""),
            )
        if action == "user_mcp_test":
            # Pre-save probe: the token is still in the request, so this checks
            # the endpoint directly. Once registered the credential lives in
            # AgentCore Identity and never comes back, so a re-test has to go
            # through the gateway instead.
            return user_mcp.probe_mcp_endpoint(
                url=payload.get("url", ""),
                token=payload.get("token", ""),
                header_name=payload.get("header_name", "Authorization"),
                value_prefix=payload.get("value_prefix", "Bearer "),
                header_value=payload.get("header_value", ""),
            )
        if action == "user_mcp_list":
            # The caller's own registrations PLUS the harness gateway's own
            # platform targets. aws-mcp is a platform target, not a registration,
            # which is the only reason it never showed here.
            #
            # Deliberately NOT every registered gateway: their tools already reach
            # agents over the gateway's own OAuth connection, so listing their
            # targets here would add nothing actionable and would need
            # ListGatewayTargets across the account. Settings -> Gateways manages
            # those.
            #
            # `unreadable` is part of the contract, not diagnostics — an empty
            # panel should state a reason rather than read as "the harness has no
            # MCP server". Platform listing is best-effort, so it cannot break the
            # owned list.
            platform, unreadable = user_mcp.list_platform_mcp_servers()
            return {
                "servers": user_mcp.list_user_mcp_servers(actor_id) + platform,
                "unreadable": unreadable,
            }
        if action == "user_mcp_status":
            target_id = payload.get("target_id", "")
            return {
                "target_id": target_id,
                "status": user_mcp.registration_status(
                    target_id, target_name=payload.get("target_name", "")
                ),
            }
    except user_mcp.RegistrationError as exc:
        # Message is written for a user; surface it rather than a stack trace.
        return {"error": "registration_failed", "message": str(exc)}
    except Exception as exc:
        logger.exception("user_mcp action %s failed", action)
        return {"error": type(exc).__name__, "message": str(exc)}

    return {"error": "unknown_action", "message": f"Unknown action: {action}"}


# Lab bypass actions that reach only DynamoDB/S3 and therefore need no gateway.
#
# The entrypoint's ``_ensure_gateway_connected()`` loads the DDB gateway registry
# and completes an MCP handshake to every gateway — ~8.3s of one-time container
# init that a pure CRUD read has no use for. Skipping it for these actions is
# what makes the Capabilities panel load in ~190ms instead of ~8.5s.
#
# Deliberately a allowlist of the SAFE actions rather than a denylist of the
# unsafe ones: a new gateway-dependent action added to ``LAB_BYPASS_ACTIONS``
# then defaults to the correct (slow, connected) path. Inverting this would make
# a new action silently return an empty tool set — a correctness bug dressed as a
# speedup. Excluded for that reason: ``BrowseGatewayTools`` /
# ``RefreshGatewayTools`` / ``TestGatewayTool`` (read the module-global
# ``mcp_manager`` via ``_RegistryGatewayClient``), and ``ChatMessage`` /
# ``RunHarness`` (run the LLM, so they need the agent and its tools).
LAB_GATEWAY_FREE_ACTIONS: frozenset[str] = frozenset({
    # Skills. `ReadPublishedSkill` reads an immutable version's row + S3 objects
    # exactly as `ReadSkillDraft` reads a working copy's; it was simply missed
    # here, so the skill drill-in paid the handshake it has no use for.
    "CreateSkillDraft", "ImportSkillPackage", "ReadSkillDraft", "UpdateSkillMd",
    "ReadPublishedSkill",
    "PutSupportingFile", "DeleteSupportingFile",
    "DeleteSkillDraft", "ListSkillDrafts",
    # Specialty Agents. `ResolvePublishedAgentRef` only resolves a lineage row to
    # a version — unlike `ResolveAgentConfig`, which resolves tool *bindings* and
    # is therefore gateway-dependent and absent from this set.
    "CreateSpecialtyAgentDraft", "ReadSpecialtyAgentDraft",
    "UpdateSpecialtyAgentDraft", "DeleteSpecialtyAgentDraft",
    "ListSpecialtyAgentDrafts", "ResolvePublishedAgentRef",
    # Tool Collections — reads and delete only.
    #
    # `CreateToolCollectionDraft` / `UpdateToolCollectionDraft` are deliberately
    # ABSENT despite looking like peers of the others: both call
    # `ToolCollectionCRUD._validate_tool_refs`, which resolves every `___`-bearing
    # ref against the gateway projection. On this skip path `mcp_manager` is not
    # connected, so the projection is empty and every gateway ref is reported
    # unresolved — the request fails, or worse silently rejects a valid ref. It is
    # order-dependent, hence intermittent: a warm container that already served a
    # gateway-connected action for this actor has a populated manager and the save
    # succeeds. This is precisely the "correctness bug dressed as a speedup" the
    # comment above warns about.
    "ReadToolCollectionDraft", "DeleteToolCollectionDraft",
    "ListToolCollectionDrafts",
    # Workflows
    "CreateWorkflowDraft", "ReadWorkflowDraft", "UpdateWorkflowDraft",
    "DeleteWorkflowDraft", "ListWorkflowDrafts",
    # Hooks — validation is pure expression evaluation, no tools involved.
    "CreateHook", "ReadHook", "UpdateHook", "DeleteHook", "ListHooks",
    "ValidateHookCondition",
    # Lifecycle / generic — index and S3 only.
    "PublishAsset", "EditAsset", "DiscardChanges", "ListAssetVersions",
    "ListMyPublishedAssets", "RenameAsset",
    "ListHarnessRuns", "ReadHarnessRun",
})


def _synthesize_session_id() -> str:
    """Mint the session id used when the frontend omits one.

    Length is load-bearing. This id is bound as the request's session
    (``jobs_context.set_request_context``), so it is what every sub-agent dispatch
    propagates as AgentCore's ``runtimeSessionId`` — and, via
    ``plan_and_execute_tool``'s ``effective_session``, it OVERRIDES the plan's own
    synthesized id and becomes the plan-state key the chainer dispatches nodes
    under. AgentCore rejects a ``runtimeSessionId`` under 33 chars, so the old
    ``session-<YmdHMSf>`` form (28 chars) silently disabled span correlation for
    the whole turn: sub-agent work landed in fresh, uncorrelated runtime sessions.

    The ``session-`` prefix and timestamp are kept (nothing parses them, but they
    make a synthesized id recognisable in logs); the uuid is what carries it over
    the minimum.
    """
    import uuid as _sid_uuid  # local, as elsewhere in this module (cold-start import policy)
    stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    return f"session-{stamp}-{_sid_uuid.uuid4().hex}"


@app.entrypoint
async def invoke(payload, context):
    # Direct timeline actions (Frame / Clip / Crop from the Renderer): deterministic ffmpeg in this chat's warm
    # code-interpreter sandbox, one JSON frame back. Before EVERYTHING else -- no turn step, no gateway connect,
    # no skills, no Agent, no model (media_actions.py; a test pins it).
    if isinstance(payload, dict) and payload.get("__media_action"):
        import media_actions

        async for frame in media_actions.handle(payload):
            yield frame
        return
    # A chat turn shows progress AT ONCE: before this, the first frame waited for the gateway
    # connects, memory restore and the first model token (seconds on a cold session). A
    # node_id'd step so it is closed in place (`_turn_start.close()`) once real progress arrives
    # -- the UI and the proxy's transcript both dedupe steps by node_id. Management actions
    # answer with a single JSON frame and get no step.
    _turn_start = _TurnStartStep(enabled=not payload.get("action"))
    _opening = _turn_start.open()
    if _opening:
        yield _opening
    # Static subsystems normally came from container warm-up; the skill load (which needs S3)
    # starts here and is joined after the gateway connects it overlaps (_await_skills_ready).
    _init_agent_subsystems(wait_for_skills=False)
    # Resolve the actor before the gateway connects: the connection carries the
    # actor header for its whole lifetime, so it must be known at connect time.
    # Best-effort — an unresolvable actor connects without the header, and the
    # interceptor fails closed (user-registered tools hidden, platform tools
    # unaffected) rather than exposing another actor's tools.
    try:
        _turn_actor_id = _resolve_actor_id(context, payload=payload)
    except Exception:
        _turn_actor_id = ""
    # Bind the forwarded token for the whole invocation before any gateway work:
    # dispatch happens deep inside a turn, far from these request headers. It is
    # a contextvar set, not a network call, so the CRUD skip below still leaves
    # it in place unconditionally.
    _turn_user_token = _bind_turn_user_token(context)
    # Gateway-free Lab CRUD skips the registry load and MCP handshake entirely.
    # Checked before the connect call because reaching that call IS the cost.
    _early_action = payload.get("action")
    if not (_early_action and _early_action in LAB_GATEWAY_FREE_ACTIONS):
        await _ensure_gateway_connected(
            actor_id=_turn_actor_id, user_token=_turn_user_token
        )
    # Every path below may read skills or the global Agent (a gateway-free action skips the
    # connect, and a concurrent first request can return from it early).
    await _await_skills_ready()
    prompt = payload.get("prompt", "")
    # Our own presigned S3 URLs (a project file attached in chat arrives as one, ~1.5 KB) become their short
    # stable identifier BEFORE any model sees them: copied through a plan, the model abbreviated the long form to
    # `.../xi.webp?...` and every node failed (session e2fb2692). The tool wrapper presigns the identifier again
    # at call time, freshly -- so an async node no longer inherits a URL minted hours earlier, either.
    if isinstance(prompt, str):
        from media.artifact_fetch import shorten_own_presigned_urls

        prompt = shorten_own_presigned_urls(prompt)
    # The user's literal words, before the date suffix below. Long-term-memory retrieval is
    # skipped for a terse reply ("2", "yes", "option 2"): the SDK searches with the literal
    # message, so a bare "2" retrieved 23 records (18 KB) and inserted them ahead of the
    # answer, and the model re-asked the question it had just been answered (session 764a71c0).
    _raw_user_prompt = prompt if isinstance(prompt, str) else ""
    if prompt:
        # Today's date, in the BODY rather than the system prompt. The model has no
        # inherent sense of "now", so documentation dated 2026 read as impossible and
        # it burned ~20 searches re-verifying a contradiction that was in its own
        # missing context, not in the pages. The system prompt is the CACHED prefix
        # (it carries the whole gateway catalog), so putting a date there would
        # invalidate that cache every day; the turn body is not cached. Day
        # granularity on purpose -- a timestamp would churn for no benefit.
        prompt = f"{prompt}\n\n[Current date: {datetime.now(timezone.utc):%Y-%m-%d}]"

    # Management API
    action = payload.get("action")
    if action:
        # Lab direct-CRUD bypass: structured {action, ...} payloads matching a
        # LabService action skip the LLM entirely and dispatch through the
        # validated CRUD layer. Used by the Lab UI's /lab proxy route.
        if action in main_lab_tools.LAB_BYPASS_ACTIONS:
            lab_service = _build_lab_service_for_request(context, payload=payload)
            if lab_service is None:
                yield json.dumps({
                    "error": "lab_unavailable",
                    "message": "Lab capability is not configured on this runtime.",
                })
                return
            # Strip the proxy-injected actor id and groups before handing
            # the payload off to the CRUD layer so they don't propagate as
            # opaque extra fields on assets.
            payload.pop("__actor_id", None)
            payload.pop("__actor_groups", None)
            try:
                result = await lab_service.handle(payload)
            except Exception as exc:
                logger.exception("Lab bypass action %s failed", action)
                result = {"error": type(exc).__name__, "message": str(exc)}
            yield json.dumps(result)
            return

        if action == "resolve_gate":
            result = _handle_gate_resolution(payload)
        elif action.startswith("mcp_"):
            result = _handle_mcp_action(action, payload)
        elif action.startswith("gateway_"):
            result = await _handle_gateway_action(action, payload, context)
        elif action.startswith("user_mcp_"):
            result = _handle_user_mcp_action(action, payload, context)
        else:
            result = _handle_skills_action(action, payload)
        yield json.dumps(result)
        return

    # Project search (media-harness-projects-access tasks 11.1/11.11): read the proxy-authored
    # __turn_context and the ProjectToken header, bind this turn's Search_Tool. Must run before the
    # OTEL block below, which rebinds the name `context`. Never breaks the turn.
    from project_search import turn as _project_search_turn
    _search_tools, _search_prompt = await _project_search_turn.prepare_turn(context, payload)
    # Web Search for THIS turn (media-harness-web-search-toggle): the chat toggle the proxy put in the turn
    # context, gated on the deployment flag. Decides the toolset below, what dispatches may forward, and the
    # gateway wrapper's refusal. A turn without a turn context never searches.
    import web_search as _web_search
    from project_search.context import current_turn_context as _current_turn_context
    _turn_ctx = _current_turn_context.get()
    _web_search.set_turn_allowed(bool(getattr(_turn_ctx, "web_search", False)) if _turn_ctx is not None else False)
    _web_lines = _web_search.prompt_lines()
    if _web_lines:
        _search_prompt = (_search_prompt or "") + "\n" + "\n".join(_web_lines) + "\n"

    # Bind a per-request LabService so chat tool calls land on the right actor.
    lab_service = _build_lab_service_for_request(context, payload=payload)
    lab_token = main_lab_tools.set_current_lab_service(lab_service) if lab_service else None
    # Bind a per-request PersistenceService (OMS workflow persistence) the same
    # way. Defensive: a missing OMS gateway leaves it unbound without failing the
    # turn — the persistence tools are still registered.
    persistence_service = _build_persistence_service_for_request(context, payload=payload)
    persistence_token = (
        persistence_tool.set_current_persistence_service(persistence_service)
        if persistence_service else None
    )
    inventory_snapshot = None
    request_owned_skills = []
    request_package_roots = []

    def _cleanup_request_skill_packages() -> None:
        for package_root in request_package_roots:
            try:
                skills_manager.cleanup_runtime_package(package_root)
            except Exception:
                logger.warning(
                    "Runtime Skill package cleanup failed: %s",
                    package_root,
                    exc_info=True,
                )
        request_package_roots.clear()

    if lab_service is not None:
        try:
            inventory_snapshot = await InventorySnapshot.build(
                index=lab_service.index, actor_id=lab_service.ctx.actor_id
            )
        except Exception as exc:
            logger.warning("InventorySnapshot.build failed: %s", exc)
        # Prepare the actor's owned Skills for an isolated request plugin so
        # they're directly invokable in chat — no publish step required.
        try:
            request_owned_skills, request_package_roots = (
                await main_lab_tools.load_owned_drafts_into_skills_manager(
                    skills_manager, lab_service
                )
            )
        except Exception as exc:
            logger.warning("Owned-drafts skill load failed: %s", exc)
    # Refresh the system prompt with the current inventory section so the model
    # sees the user's drafts. The base system prompt is rebuilt every turn —
    # cheap, and avoids stale snapshot risk after a CRUD mutation. Inventory
    # is only injected when the prompt suggests asset-related work.
    #
    # Resolve the session id. Frontend passes a stable UUID per chat session;
    # if it's missing we synthesize one but that path should be transient.
    # The synthesized form is long enough to be propagated as runtimeSessionId —
    # see _synthesize_session_id, that is not incidental. Resolved BEFORE the prompt
    # rebuild so the prompt names this session's real output prefix.
    session_id = payload.get("session_id") or _synthesize_session_id()
    # (stable, session, turn) prompt parts -- see _build_turn_prompt. None keeps the previous
    # whole-string prompt (agent.system_prompt), as a failed rebuild always did.
    _prompt_parts = None
    try:
        _prompt_parts = _build_turn_prompt(
            inventory_snapshot=inventory_snapshot, prompt=prompt, session_id=session_id
        )
        if _ContextInjector is None:
            # No per-call injection available: the inventory stays in the system prompt.
            agent.system_prompt = _build_system_prompt(
                inventory_snapshot=inventory_snapshot, prompt=prompt, session_id=session_id
            )
            _prompt_parts = None
    except Exception:
        logger.warning("system prompt rebuild failed; keeping the previous one", exc_info=True)
        _prompt_parts = None

    # Bind per-request actor/session for the per-tool job-tracking
    # wrap (async-job-tracking task 5.3). The wrap's resolvers read
    # these contextvars on every dispatch, so the binding MUST happen
    # before any tool call in this request runs. Even when the
    # registry is unwired (dev environments without
    # JOB_REGISTRY_TABLE), set the binding anyway — it's cheap and
    # keeps any future code path that consults ``jobs.context`` happy.
    #
    # Source the actor from the same resolver the LabService uses so
    # the two stay in lock-step (the wrap's actor scoping per R11.2
    # must agree with the rest of the runtime's actor binding). If
    # the resolver raises (no JWT, no proxy stamp), leave the
    # binding unset — the wrap's defensive ``try/except`` declines
    # registration cleanly when ``current_actor_id`` raises.
    jobs_context_token: Optional[jobs_context.RequestContextTokens] = None
    try:
        _actor_id_for_jobs = _resolve_actor_id(context, payload=payload)
    except Exception:
        _actor_id_for_jobs = ""
    if _actor_id_for_jobs:
        try:
            jobs_context_token = jobs_context.set_request_context(
                actor_id=_actor_id_for_jobs,
                session_id=session_id,
            )
        except Exception as exc:
            logger.warning("jobs_context.set_request_context failed: %s", exc)
            jobs_context_token = None

    # Identify the user request. Every job dispatched anywhere under this
    # invocation is stamped with it, so "show me the results of what I just
    # asked for" can scope to one request. run_id cannot serve this: it is only
    # bound while a plan executes, so a standalone dispatch has none, and a
    # session accumulates several unrelated intents.
    #
    # A sub-agent inherits the turn_id from its dispatch payload rather than
    # minting a new one, so work fanned out across runtimes stays in one group.
    jobs_turn_token = None
    try:
        import uuid as _turn_uuid
        _turn_id = payload.get("turn_id") if isinstance(payload, dict) else None
        if not (isinstance(_turn_id, str) and _turn_id.strip()):
            _turn_id = f"turn-{_turn_uuid.uuid4().hex}"
        jobs_turn_token = jobs_context.set_turn_id(_turn_id.strip())
    except Exception as exc:
        logger.warning("jobs_context.set_turn_id failed: %s", exc)
        jobs_turn_token = None

    # Set OTEL baggage for trace correlation.
    try:
        from opentelemetry import baggage, context
        otel_ctx = baggage.set_baggage("session.id", session_id)
        context.attach(otel_ctx)
    except Exception:
        pass

    # Resolve the actor for the durable memory store. Prefer the JWT-derived
    # actor_id (when the LabService bound one), fall back to LAB_DEFAULT_ACTOR_ID.
    memory_actor_id = (
        lab_service.ctx.actor_id if lab_service is not None else _LAB_DEFAULT_ACTOR_ID
    )

    # Build a per-request Agent so Strands' AgentCoreMemorySessionManager can
    # bind to the correct (actor_id, session_id) for THIS turn. Sharing the
    # singleton Agent across sessions caused conversation-history bleed when
    # users switched chat sessions in the UI (the singleton's in-memory
    # message list was never reset).
    session_manager = None
    if MEMORY_ID:
        try:
            # AgentCore-Memory keying is centralized in the shared identity
            # module so main.py and task_agent.py compute the memory
            # actor/session/namespace one way (custom-agents design,
            # Component 5). Behavior is unchanged for the main agent.
            _terse_turn = memory_context.is_terse_reply(_raw_user_prompt)
            if _terse_turn and MEMORY_LTM_RETRIEVAL:
                logger.info("LTM retrieval skipped for terse reply (session %s)", session_id)
            mem_config = identity.build_memory_config(
                memory_id=MEMORY_ID,
                session_id=session_id,
                actor_id=memory_actor_id,
                enable_retrieval=MEMORY_LTM_RETRIEVAL and not _terse_turn,
                batch_size=MEMORY_WRITE_BATCH_SIZE if MEMORY_ASYNC_WRITES else 1,
            )
            # Framed: retrieved records reach the model as
            # <remembered_context source="past sessions" authority="suggestion"> with a note
            # that they never answer a required confirmation (session 2f376186).
            # ephemeral: that block reaches the model per call AFTER the cache point instead of
            # being spliced into the (already persisted) user message, so later turns' history
            # matches what this turn sent. async_writes: history writes leave the event loop.
            _sm_kwargs = {"agentcore_memory_config": mem_config, "region_name": REGION,
                          "ephemeral": _ContextInjector is not None,
                          "async_writes": MEMORY_ASYNC_WRITES}
            if _memory_boto_session is not None:
                _sm_kwargs["boto_session"] = _memory_boto_session
            session_manager = memory_context.FramedMemorySessionManager(**_sm_kwargs)
            # Long-term-memory retrieval for THIS message starts now, overlapping the session
            # restore below; the retrieval hook uses it only if the message text matches.
            session_manager.prefetch(prompt)
        except Exception as exc:
            logger.warning("Memory init failed for session %s: %s", session_id, exc)

    # Construct a fresh Agent and an isolated Skill plugin for every request.
    # Shared built-ins/S3 Skills are copied from the immutable baseline; only
    # this actor's owned Skills are added. This prevents private Skill content
    # or package files from leaking through the process-global plugin.
    added_bi = [t for n, t in _all_builtin_tools.items() if n in _added_builtins]
    # The WebSearch tool is bound only on a turn whose chat has web search on (web_search.filter_tools).
    gw_tools = _web_search.filter_tools(mcp_manager.get_all_added_tools())
    # ...and is bound DIRECTLY on such a turn, so "search with WebSearch" is a tool the orchestrator really has.
    _have = {getattr(t, "tool_name", None) for t in gw_tools}
    gw_tools += [t for t in _web_search.direct_tools(mcp_manager.find_tools) if getattr(t, "tool_name", None) not in _have]
    request_hook_provider = harness_hook_provider
    if lab_service is not None:
        try:
            _user_hooks = await lab_service.hook_crud.list(lab_service.ctx)
            request_hook_provider = _build_hook_provider(_user_hooks)
        except Exception:
            logger.debug("per-request user-hook load failed; using defaults", exc_info=True)
    request_skills_plugin = skills_manager.create_request_plugin(request_owned_skills)
    request_model, resolved_model_id = _resolve_request_model(payload, model)
    # Bind the turn's model so dispatch_tool/plan_and_execute_tool can inherit it
    # instead of relying on the LLM to name the right one (it picked
    # claude-sonnet-4-6 while the session was set to Sonnet 5).
    jobs_context.set_model_id(resolved_model_id)
    # Bind this turn's active skills the same way, so dispatch_tool /
    # plan_and_execute_tool forward them to sub-agents by construction. Skills
    # reached this request plugin, but a sub-agent's skills used to arrive only
    # as an LLM-filled dispatch_tool argument the model left empty --
    # "Dispatching to task-agent: 1 tools, 0 skills" while the turn narrated
    # using the aws-research skill. Serialized to defs (not names) because the
    # deployed Task Agent has no per-actor name->definition resolver and this
    # plugin already holds the resolved bodies, including owned drafts the global
    # store lacks -- see jobs.context.set_active_skills for the full rationale.
    try:
        _active_skill_defs = [
            {
                "name": s.name,
                "description": s.description or "",
                "instructions": s.instructions or "",
            }
            for s in request_skills_plugin.get_available_skills()
        ]
        jobs_context.set_active_skills(_active_skill_defs)
    except Exception:
        logger.warning(
            "Failed to bind active skills for sub-agent inheritance", exc_info=True
        )
    if _prompt_parts is not None:
        _stable_prompt, _session_prompt, _turn_prompt = _prompt_parts
        _request_prompt = _request_system_prompt(
            _stable_prompt, _session_prompt, _search_prompt,
            cacheable=_model_caches_prompts(request_model),
        )
    else:
        _turn_prompt = ""
        _request_prompt = (f"{agent.system_prompt}{_search_prompt}" if _search_prompt else agent.system_prompt)
    _request_plugins = [request_skills_plugin]
    if _ContextInjector is not None and (
        _turn_prompt or getattr(session_manager, "ephemeral", False)
    ):
        # Per-call, never persisted, folded in AFTER the message cache point: this turn's
        # remembered context (read when the hook has run) and the inventory hint. On every call
        # of the turn, as when they lived in the turn's message / the system prompt.
        _sm_for_ctx = session_manager

        def _render_turn_context(_ctx, **_kw) -> str:
            return _compose_turn_context(getattr(_sm_for_ctx, "turn_context", "") or "", _turn_prompt)

        _request_plugins.append(
            _ContextInjector(_render_turn_context, name="harness:turn-context", trigger="everyTurn")
        )
    request_agent_kwargs = {
        "model": request_model,
        "system_prompt": _request_prompt,
        "tools": added_bi + gw_tools + _search_tools,
        "plugins": _request_plugins,
        "hooks": [request_hook_provider],
    }
    if session_manager is not None:
        request_agent_kwargs["session_manager"] = session_manager
    # Context-window compression: summarize older turns on overflow instead of
    # trimming. Orthogonal to the session manager (persistence) — this only
    # governs the in-context window. See MEMORY_COMPRESSION_* above.
    if MEMORY_COMPRESSION:
        request_agent_kwargs["conversation_manager"] = SummarizingConversationManager(
            summary_ratio=MEMORY_COMPRESSION_RATIO,
            preserve_recent_messages=MEMORY_COMPRESSION_PRESERVE_RECENT,
        )
    try:
        request_agent = Agent(**request_agent_kwargs)
    except Exception:
        _cleanup_request_skill_packages()
        raise
    # Self-heal corrupted session history before streaming. An interrupted tool
    # call can leave orphaned/duplicate toolResult blocks (or a toolUse with no
    # result) in the restored message list, which makes Bedrock reject EVERY
    # subsequent turn on that session with a ValidationException (toolResult vs
    # toolUse count mismatch) — the symptom is empty, summary-only responses.
    # Strands' restore-time repair is best-effort and can still leave an invalid
    # sequence, so normalize the pairing here as a backstop.
    try:
        _repaired = _sanitize_tool_pairs(request_agent.messages)
        if _repaired:
            logger.warning(
                "Repaired %d tool-pair block(s) in restored history for session %s",
                _repaired, session_id,
            )
    except Exception:
        logger.warning(
            "tool-pair sanitation failed for session %s", session_id, exc_info=True
        )
    import time as _time
    _exec_start = _time.time()
    # Time-to-first-answer-token for search telemetry (Component 9, Req 4.1): monotonic from here to
    # the first answer-text yield to the proxy. Stays None when the turn yields no answer text.
    _ttft_mono_start = _time.monotonic()
    _ttft_ms = None
    _tool_timings: dict[str, list[float]] = {}
    _cycle_count = 0
    # Active tool tracking is keyed by toolUseId, NOT tool name. The model
    # often calls the same tool repeatedly within a single turn (e.g.
    # poll-loop on get_diarization_status); keying by name caused later
    # close events to inherit the very first start_time, producing
    # nonsense durations like "2389s" on what was actually a 2s call.
    # Each entry is {"name": str, "started_at": float, "info": dict}.
    active_tools: dict[str, dict] = {}
    usage = {}

    # Set up progress channel for tool → orchestrator streaming
    try:
        progress_channel = ProgressChannel()
        set_active_channel(progress_channel)
        merge_q: asyncio.Queue = asyncio.Queue()
    except Exception:
        set_active_channel(None)
        _cleanup_request_skill_packages()
        raise

    # --- Merged event loop ---
    # The Strands stream blocks during long tool executions. A background
    # poller drains progress events and keeps the HTTP stream active.
    _SENTINEL = object()

    # Citation verification (design Component 4, Req 2.6). On a Projects turn, prepare_turn bound a
    # TurnContext; its turn_id ties the streamed answer buffer, finish_turn's TurnOutcome and the
    # execution_summary block together. Empty on a non-Projects turn, which disables all three.
    try:
        from project_search import turn_end as _turn_end
        from project_search import telemetry as _telemetry
        from project_search.context import current_turn_context as _ps_turn_ctx

        _ps_tc = _ps_turn_ctx.get()
        _citation_turn_id = _ps_tc.turn_id if _ps_tc is not None else ""
    except Exception:  # noqa: BLE001 - never break a turn over citation setup
        _turn_end = None
        _telemetry = None
        _ps_tc = None
        _citation_turn_id = ""

    from stream_text import ProseStream  # noqa: PLC0415
    _prose = ProseStream()

    def _prose_out(items):
        """Answer text streams as text; a narration note joins the turn's steps (stream_text)."""
        if items:
            _closed = _turn_start.close()
            if _closed:
                yield _closed
        for kind, value in items:
            yield value if kind == "text" else _step_event("done", value)

    async def _strands_producer():
        """Forward Strands stream events into the merge queue."""
        try:
            stream = request_agent.stream_async(prompt)
            async for event in stream:
                # Buffer the whole answer for citation verification: each str `data` event is one
                # chunk of the model's answer text (design Component 4, "Answer text").
                if _citation_turn_id and _turn_end is not None and isinstance(event, dict):
                    _chunk = event.get("data")
                    if isinstance(_chunk, str) and _chunk:
                        _turn_end.note_answer_text(_citation_turn_id, _chunk)
                await merge_q.put(("strands", event))
        except Exception as exc:
            await merge_q.put(("error", exc))
        finally:
            await merge_q.put(("strands", _SENTINEL))

    async def _progress_poller():
        """Poll the ProgressChannel every second and push events into the merge queue.
        Also emits a keepalive heartbeat every 30s if nothing else is flowing."""
        last_yield = _time.time()
        while True:
            await asyncio.sleep(1)
            drained = progress_channel.drain()
            for pe in drained:
                await merge_q.put(("progress", pe))
                last_yield = _time.time()
            # Keepalive: if nothing has been yielded for 30s, send a heartbeat
            if _time.time() - last_yield > 30:
                await merge_q.put(("keepalive", None))
                last_yield = _time.time()

    strands_task = None
    try:
        strands_task = asyncio.ensure_future(_strands_producer())
        poller_task = asyncio.ensure_future(_progress_poller())
    except Exception:
        if strands_task is not None:
            strands_task.cancel()
            try:
                await strands_task
            except asyncio.CancelledError:
                pass
        set_active_channel(None)
        _cleanup_request_skill_packages()
        raise

    stream_done = False
    try:
        while not stream_done:
            tag, value = await merge_q.get()

            if tag == "progress":
                _closed = _turn_start.close()
                if _closed:
                    yield _closed
                yield value.to_step_json()
                continue

            if tag == "keepalive":
                yield _step_event("running", "Working...")
                continue

            if tag == "error":
                logger.exception("Strands stream error: %s", value)
                # Tell the client. Logging alone ended the stream with no terminal
                # output, so the UI kept showing the last step indicator and the turn
                # appeared to run indefinitely.
                _closed = _turn_start.close()
                if _closed:
                    yield _closed
                yield _step_event("error", "Turn failed")
                yield _stream_error_text(value)
                stream_done = True
                continue

            # tag == "strands"
            if value is _SENTINEL:
                stream_done = True
                continue

            event = value

            if event.get("start_event_loop"):
                _cycle_count += 1

            if "current_tool_use" in event:
                tool_info = event["current_tool_use"]
                if isinstance(tool_info, dict):
                    tool_use_id = tool_info.get("toolUseId") or ""
                    tool_name = tool_info.get("name") or ""
                    if tool_use_id and tool_name:
                        if tool_use_id not in active_tools:
                            # Emit running on first sight of this toolUseId.
                            # Input may still be empty (Strands assembles it
                            # from deltas), but we MUST emit something here
                            # — if we wait for input and never see another
                            # current_tool_use event, the close event has
                            # nothing to match and the call goes invisible.
                            # The label_for_tool_use helper degrades to the
                            # generic TOOL_LABELS entry when input is empty.
                            active_tools[tool_use_id] = {
                                "name": tool_name,
                                "started_at": _time.time(),
                                "info": tool_info,
                            }
                            for _out in _prose_out(_prose.tool_started()):
                                yield _out
                            _closed = _turn_start.close()
                            if _closed:
                                yield _closed
                            yield _step_event("running", label_for_tool_use(tool_info))
                        else:
                            # Subsequent current_tool_use event for a tool
                            # we're already tracking — refresh the cached
                            # info so the eventual close event can produce
                            # a rich label even if the running emit was
                            # generic. We compare populated-input richness:
                            # the latest event with more keys wins.
                            current = active_tools[tool_use_id]["info"]
                            if has_input_payload(tool_info) and (
                                not has_input_payload(current)
                                or len(_safe_input(tool_info)) > len(_safe_input(current))
                            ):
                                active_tools[tool_use_id]["info"] = tool_info

            if "data" in event and isinstance(event["data"], dict):
                tool_use_id = event["data"].get("toolUseId")
                if tool_use_id and tool_use_id in active_tools:
                    # Close ONLY the tool call whose toolUseId matches.
                    # The previous behaviour flushed every active tool on
                    # any data event, which produced misleading durations
                    # when the same tool was active under several ids.
                    data_dict = event["data"]
                    entry = active_tools.pop(tool_use_id)
                    duration = _time.time() - entry["started_at"]
                    name = entry["name"]
                    _tool_timings.setdefault(name, []).append(duration)
                    status, label = label_for_tool_done(entry["info"], result=data_dict)
                    yield json.dumps({
                        "type": "step",
                        "status": status,
                        "label": label,
                        "duration_ms": int(duration * 1000),
                    })

            if "data" in event and isinstance(event["data"], str):
                if _ttft_ms is None:
                    _ttft_ms = int((_time.monotonic() - _ttft_mono_start) * 1000)
                # Narration before a tool call becomes a step note; answer text gets a paragraph break
                # where it resumes after a tool call (stream_text).
                for _out in _prose_out(_prose.text(event["data"])):
                    yield _out

            # The model finished a message WITHOUT asking for a tool: what ProseStream is holding
            # is the answer, so release it now instead of after the turn's post-processing
            # (AfterInvocation hooks, memory flush, citation checks) -- a short answer used to
            # wait for all of that. Same text, same lead-in handling as _prose.end().
            _stop_reason = _message_stop_reason(event)
            if _stop_reason and _stop_reason != "tool_use":
                for _out in _prose_out(_prose.finish_answer()):
                    yield _out

            if "result" in event:
                result = event["result"]
                try:
                    metrics = result.metrics if hasattr(result, "metrics") else None
                    if hasattr(metrics, "accumulated_usage") and metrics.accumulated_usage:
                        u = metrics.accumulated_usage
                        usage = {"inputTokens": u.get("inputTokens", 0), "outputTokens": u.get("outputTokens", 0), "totalTokens": u.get("totalTokens", 0)}
                except Exception:
                    pass
    finally:
        strands_task.cancel()
        poller_task.cancel()
        for task in (strands_task, poller_task):
            try:
                await task
            except asyncio.CancelledError:
                pass
        _cleanup_request_skill_packages()

    # The turn's last piece of text is the answer (or its tail): emit what ProseStream held back.
    for _out in _prose_out(_prose.end()):
        yield _out
    # A turn that produced no visible output at all still closes its start step.
    _closed = _turn_start.close()
    if _closed:
        yield _closed

    # Flush any tools still active when the stream ended (e.g. cancelled
    # or stream cut short). Each entry is closed with whatever info we
    # cached at start time.
    for tool_use_id, entry in list(active_tools.items()):
        duration = _time.time() - entry["started_at"]
        _tool_timings.setdefault(entry["name"], []).append(duration)
        status, label = label_for_tool_done(entry["info"])
        yield json.dumps({
            "type": "step",
            "status": status,
            "label": label,
            "duration_ms": int(duration * 1000),
        })
    active_tools.clear()

    # Final drain of any remaining progress events
    for pe in progress_channel.drain():
        yield pe.to_step_json()
    set_active_channel(None)

    # Every history write of this turn is stored before the turn reports done (the answer has
    # already streamed). Off the event loop; the next turn restores from what lands here.
    if session_manager is not None and getattr(session_manager, "async_writes", False):
        try:
            await asyncio.to_thread(session_manager.finish_writes)
        except Exception:  # noqa: BLE001 - the answer is delivered; the loss is logged loudly
            logger.error("conversation history flush failed for session %s", session_id, exc_info=True)

    # Emit execution summary
    _exec_duration = _time.time() - _exec_start
    tool_summary = {tn: {"calls": len(ds), "total_ms": int(sum(ds)*1000), "avg_ms": int(sum(ds)/len(ds)*1000)} for tn, ds in _tool_timings.items()}
    _summary = {
        "type": "execution_summary",
        "total_duration_ms": int(_exec_duration * 1000),
        "cycles": _cycle_count,
        "tools": tool_summary,
        "usage": usage,
        # The model actually used for this turn. The UI badge previously fell
        # back to the first entry of the model list when nothing was explicitly
        # selected, so it advertised a model the agent was not running. Report
        # the resolved id so the badge can state fact instead of guessing.
        "model_id": resolved_model_id,
        # The release that answered: Improve and evaluations can compare results across releases.
        "app_version": app_version(),
    }
    # Attach the CitationBlock when this turn had a Projects context (design Component 4, Req 2.6,
    # 2.12). _on_session_end already ran finish_turn (AfterInvocationEvent fires before the stream
    # ends), so outcome_for returns the classified TurnOutcome; citations_wire carries no URL.
    if _citation_turn_id and _turn_end is not None:
        try:
            _outcome = _turn_end.outcome_for(_citation_turn_id)
            if _outcome is not None:
                _summary["citations"] = _outcome.citations_wire()
        except Exception:  # noqa: BLE001 - a summary must still be emitted without citations
            logger.exception("attaching the citation block to execution_summary failed")

    # Lineage of the files this turn wrote (asset-lineage design §3/§4). Never raises.
    _attach_turn_lineage(_summary)

    # Search telemetry (design Component 9, Req 4.1, 4.2, 4.3). Built only on a Projects turn, from
    # this turn's per-call samples and the TurnOutcome's citation counts — never from answer text, a
    # prompt, tool arguments or Hits. It rides on execution_summary so a local developer can read it,
    # and is logged (emit) after the answer frame below. A failure here changes neither the answer nor
    # the citation classifications (Req 4.3).
    _telemetry_record = None
    if _citation_turn_id and _telemetry is not None and _ps_tc is not None:
        try:
            try:
                from project_search.skill_text import load_media_search_qa as _load_msq

                _loaded_skill = _load_msq()
                _skill_sha = _loaded_skill.sha256 if _loaded_skill is not None else None
            except Exception:  # noqa: BLE001 - a missing skill never blocks telemetry
                _skill_sha = None
            _tel_outcome = _turn_end.outcome_for(_citation_turn_id) if _turn_end is not None else None
            _telemetry_record = _telemetry.build_record(
                tc=_ps_tc,
                session_id=session_id,
                model_id=resolved_model_id,
                ttft_ms=_ttft_ms,
                total_ms=int(_exec_duration * 1000),
                usage=usage,
                outcome=_tel_outcome,
                skill_sha256=_skill_sha,
            )
            _summary["search_telemetry"] = _telemetry_record
        except Exception as _tel_exc:  # noqa: BLE001 - a summary must still be emitted without telemetry
            _telemetry_record = None
            logger.warning(
                "search telemetry failed session=%s turn=%s %s",
                session_id, _citation_turn_id, type(_tel_exc).__name__,
            )

    yield json.dumps(_summary)

    # Write the telemetry record after the answer frame has been delivered (Req 4.3). emit never raises.
    if _telemetry_record is not None and _telemetry is not None:
        _telemetry.emit(_telemetry_record)

    # Drop the answer buffer, the outcome and the per-turn Search_Ledger (design Component 4), and the
    # turn's telemetry samples (Component 9).
    if _citation_turn_id and _turn_end is not None:
        try:
            _turn_end.release(_citation_turn_id)
        except Exception:  # noqa: BLE001 - release is best-effort cleanup
            logger.debug("turn_end.release failed", exc_info=True)
    if _citation_turn_id and _telemetry is not None:
        try:
            _telemetry.release(_citation_turn_id)
        except Exception:  # noqa: BLE001 - release is best-effort cleanup
            logger.debug("telemetry.release failed", exc_info=True)

    # Release the per-request LabService binding so the contextvar doesn't
    # bleed across requests in long-lived runtime sessions.
    if lab_token is not None:
        try:
            main_lab_tools.reset_current_lab_service(lab_token)
        except Exception:
            pass

    # Release the per-request PersistenceService binding (same rationale as the
    # lab_token reset above — contextvars persist for the asyncio task lifetime).
    if persistence_token is not None:
        try:
            persistence_tool.reset_current_persistence_service(persistence_token)
        except Exception:
            pass

    # Release the per-request jobs_context binding (async-job-tracking
    # task 5.3). Same rationale as the lab_token reset above —
    # contextvars persist for the lifetime of the asyncio task, and a
    # long-lived AgentCore runtime serves many requests on the same
    # task so leaks would silently mis-attribute future job
    # registrations to a stale Actor.
    if jobs_context_token is not None:
        try:
            jobs_context.reset_request_context(jobs_context_token)
        except Exception:
            pass
    if jobs_turn_token is not None:
        try:
            jobs_context.reset_turn_id(jobs_turn_token)
        except Exception:
            pass


def _handle_gate_resolution(payload):
    """Handle gate approval/rejection from the UI."""
    from orchestration.progress_channel import get_active_channel
    gate_id = payload.get("gate_id")
    approved = payload.get("approved", True)
    feedback = payload.get("feedback", "")
    if not gate_id:
        return {"error": "gate_id is required"}
    ch = get_active_channel()
    if ch is None:
        return {"error": "No active channel — no request in progress"}
    resolved = ch.resolve_gate(gate_id, approved=approved, feedback=feedback)
    if not resolved:
        return {"error": f"Gate '{gate_id}' not found or already resolved"}
    return {"status": "resolved", "gate_id": gate_id, "approved": approved}


def _handle_skills_action(action, payload):
    try:
        if action == "list_skills": return {"skills": skills_manager.list_skills()}
        elif action == "load_skill":
            sp = payload.get("skill_path")
            if sp: return {"status": "loaded", **skills_manager.load_skill_from_directory(sp)}
            n, d, i, t = payload.get("name"), payload.get("description",""), payload.get("instructions",""), payload.get("tools",[])
            if n and i: return {"status": "loaded", **skills_manager.load_skill_programmatic(n, d, i, t)}
            return {"error": "Provide skill_path or (name + instructions)"}
        elif action == "unload_skill":
            n = payload.get("name")
            return {"error": "name is required"} if not n else {"status": "unloaded" if skills_manager.unload_skill(n) else "not_found", "name": n}
        elif action == "get_skill":
            n = payload.get("name")
            if not n: return {"error": "name is required"}
            d = skills_manager.get_skill(n)
            return d if d else {"error": f"Skill '{n}' not found"}
        elif action == "save_skill":
            n = payload.get("name")
            return {"error": "name is required"} if not n else skills_manager.save_skill(n, payload.get("target", "local"))
        elif action == "generate_skill":
            d = payload.get("description", "")
            return {"error": "description is required"} if not d else _generate_skill(d, payload.get("available_tools", []))
        elif action == "reload_skills": return {"status": "reloaded", "skills": skills_manager.reload_all()}
        elif action == "upload_file":
            from render.types import mime_for
            filename = payload.get("filename", "upload.pdf")
            data_b64 = payload.get("data")
            if not data_b64: return {"error": "data (base64) is required"}
            # Consistent, session-scoped INPUT location: artifacts/<session_id>/input/<file>
            # in the skills bucket (CloudFront-served). Was a broken flat key referencing an
            # undefined `prefix` (NameError) with a hardcoded application/pdf content-type.
            in_prefix = _artifact_input_prefix(payload.get("session_id", "") or payload.get("sessionId", ""))
            full_key = f"{in_prefix}{filename}"
            ct = payload.get("content_type", "") or mime_for(filename) or "application/octet-stream"
            s3_client.put_object(Bucket=S3_BUCKET, Key=full_key, Body=base64.b64decode(data_b64), ContentType=ct)
            url = f"https://{ARTIFACTS_DOMAIN}/{full_key}" if ARTIFACTS_DOMAIN else ""
            return {"status": "uploaded", "key": full_key, "bucket": S3_BUCKET,
                    "full_key": full_key, "url": url, "content_type": ct}
        else: return {"error": f"Unknown action: {action}"}
    except Exception as e:
        logger.exception("Skills action failed: %s", action)
        return {"error": str(e)}


def _generate_skill(description, available_tools=None):
    ts = "\n".join(f"- {t}" for t in (available_tools or [])) or "None"
    ga = Agent(model=model, system_prompt="Respond only with valid JSON.")
    result = ga(SKILL_GEN_PROMPT.format(description=description, tools=ts))
    raw = str(result.message["content"][0]["text"]).strip()
    if raw.startswith("```"): raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
    if raw.endswith("```"): raw = raw[:raw.rfind("```")]
    try: data = json.loads(raw.strip())
    except json.JSONDecodeError as e: return {"error": f"Invalid JSON: {e}"}
    for f in ("name", "description", "instructions"):
        if f not in data or not data[f]: return {"error": f"Missing: {f}"}
    return {"status": "generated", **{k: data.get(k, [] if k == "tools" else "") for k in ("name", "description", "instructions", "tools")}}


def _handle_mcp_action(action, payload):
    try:
        if action == "mcp_list_available":
            return {"gateways": mcp_manager.list_gateways(), "builtin_tools": _get_builtin_tools()}
        elif action == "mcp_connect_gateway":
            gw_id = payload.get("gateway_id")
            return {"error": "gateway_id is required"} if not gw_id else mcp_manager.connect_gateway(gw_id)
        elif action == "mcp_disconnect_gateway":
            gw_id = payload.get("gateway_id")
            if not gw_id: return {"error": "gateway_id is required"}
            r = mcp_manager.disconnect_gateway(gw_id); _rebuild_agent_tools(); return r
        elif action == "mcp_refresh_tools":
            gw_id = payload.get("gateway_id")
            if not gw_id: return {"error": "gateway_id is required"}
            r = mcp_manager.refresh_gateway(gw_id)
            if "error" not in r: _rebuild_agent_tools()
            return r
        elif action == "mcp_search_tools":
            gw_id = payload.get("gateway_id", ""); q = payload.get("query", "")
            return {"error": "query is required"} if not q else {"results": mcp_manager.search_gateway(gw_id, q)}
        elif action == "mcp_add_tool":
            n = payload.get("name")
            if not n: return {"error": "name is required"}
            if n in _all_builtin_tools:
                _added_builtins.add(n); _rebuild_agent_tools()
                _publish_builtin_catalog()
                return {"name": n, "state": "available"}
            r = mcp_manager.add_tool(n)
            if "error" not in r: _rebuild_agent_tools()
            return r
        elif action == "mcp_remove_tool":
            n = payload.get("name")
            if not n: return {"error": "name is required"}
            if n in _all_builtin_tools:
                _added_builtins.discard(n); _trusted_builtins.discard(n); _rebuild_agent_tools()
                _publish_builtin_catalog()
                return {"name": n, "state": "not_added"}
            r = mcp_manager.remove_tool(n)
            if "error" not in r: _rebuild_agent_tools()
            return r
        elif action == "mcp_trust_tool":
            n = payload.get("name")
            if not n: return {"error": "name is required"}
            if n in _all_builtin_tools:
                if n not in _added_builtins: return {"error": f"Not added: {n}"}
                _trusted_builtins.add(n); return {"name": n, "state": "trusted"}
            return mcp_manager.trust_tool(n)
        elif action == "mcp_untrust_tool":
            n = payload.get("name")
            if not n: return {"error": "name is required"}
            if n in _all_builtin_tools:
                _trusted_builtins.discard(n); return {"name": n, "state": "available"}
            return mcp_manager.untrust_tool(n)
        elif action == "mcp_list_agents":
            # Read-only inventory of the built-in sub-agent runtimes.
            #
            # The SPA no longer calls this: the Agents panel paints the same four
            # agents out of the Capability API bootstrap it already fetches, which is
            # why opening that tab no longer wakes this runtime. Kept for the model
            # and any non-SPA caller, and kept response-shape-compatible with what it
            # returned before (short `key`, `purpose`) so those callers are unaffected.
            #
            # Sourced from `capability.sources.BUILTIN_CAPABILITIES` rather than a
            # literal here. Two copies of these strings meant the panel could describe
            # an agent differently depending on which host answered, and the two had
            # already drifted once.
            from capability.sources import BUILTIN_CAPABILITIES

            _builtin_agents = []
            for _entry in BUILTIN_CAPABILITIES:
                if _entry["category"] != "specialty-agent":
                    continue
                # Env, else the runtime-ARN SSM parameters: the deploy no longer puts these
                # ARNs in this runtime's env (see runtime_arns.py).
                _arn = runtime_arns.get(_entry.get("arnEnv", "")) if _entry.get("arnEnv") else ""
                _builtin_agents.append({
                    # `key` keeps its historical short form (`task`, not
                    # `task-agent`) — this response is a contract for existing
                    # callers, not a place to renumber identities.
                    "key": _entry["slug"].removesuffix("-agent"),
                    "label": _entry["displayName"],
                    "purpose": _entry.get("description", ""),
                    "arnEnv": _entry.get("arnEnv", ""),
                    "invokedVia": _entry.get("invokedVia", ""),
                    "configured": bool(_arn),
                    # Expose only the runtime id (tail), never the full ARN.
                    "runtimeId": _arn.rsplit("/", 1)[-1] if _arn else "",
                })
            return {"agents": _builtin_agents}
        elif action == "mcp_list_models":
            return {"models": _available_models}
        elif action == "mcp_set_model":
            mid = payload.get("model_id")
            if not mid: return {"error": "model_id is required"}
            # Optional reasoning-effort cap (UI slider). Validate to the enum; anything
            # else means None -> env fallback. build_model is fail-safe: it only attaches
            # output_config.effort to a model that supports it, so a stale effort never 400s.
            eff = payload.get("effort")
            if eff not in ("low", "medium", "high"):
                eff = None
            try:
                # Build the boto config locally — the module's `_boto_cfg` is
                # defined inside the agent-build function scope and is NOT in
                # scope here, which previously raised
                # `NameError: name '_boto_cfg' is not defined` and broke model
                # switching. Construct an equivalent config inline instead.
                from botocore.config import Config as _BotoCfg
                _cfg = _BotoCfg(read_timeout=300, connect_timeout=10, retries={"max_attempts": 4, "mode": "standard"})
                agent.model = build_model(model_id=mid, boto_client_config=_cfg, effort=eff)
                return {"model_id": mid}
            except Exception as e: return {"error": f"Failed: {e}"}
        else: return {"error": f"Unknown MCP action: {action}"}
    except Exception as e:
        logger.exception("MCP action failed: %s", action)
        return {"error": str(e)}


async def _handle_gateway_action(action, payload, context):
    """Dispatch ``gateway_*`` runtime actions (R4).

    Reads (``gateway_list``) are authenticated but unrestricted — any caller
    with a valid JWT can list enabled gateways for the consent UI (R8).
    Mutations (``gateway_register`` / ``gateway_update`` / ``gateway_delete``
    / ``gateway_reload`` / ``gateway_test_connection``) are gated by
    :func:`_is_admin` in their respective task implementations (5.3+).
    """
    if _gateway_registry is None:
        return {"error": "gateway_registry_unavailable",
                "message": "GATEWAY_REGISTRY_TABLE not configured on this runtime."}
    try:
        if action == "gateway_list":
            descriptors = await _gateway_registry.list_enabled()
            return [d.public_dict() for d in descriptors]

        if action == "gateway_register":
            return await _gateway_register(payload, context)

        if action == "gateway_update":
            return await _gateway_update(payload, context)

        if action == "gateway_delete":
            return await _gateway_delete(payload, context)

        if action == "gateway_reload":
            return await _gateway_reload(payload, context)

        if action == "gateway_test_connection":
            return await _gateway_test_connection(payload, context)

        return {"error": f"Unknown gateway action: {action}"}
    except Exception as e:
        logger.exception("Gateway action failed: %s", action)
        return {"error": str(e)}


async def _gateway_register(payload, context):
    """Handle the ``gateway_register`` admin action (R3, R4).

    Flow:

    1. Admin gate via :func:`_is_admin`. Non-admins get
       ``{"error": "admin_required"}`` with no DDB or SM contact.
    2. Validate the payload has the minimum fields a descriptor needs
       (``id``, ``name``, ``url``, ``auth_type``). Anything missing
       returns a structured ``{"error": "invalid_payload", ...}``.
    3. Build the typed :class:`GatewayDescriptor` from the payload, popping
       any literal ``client_secret`` out of the ``auth_config`` dict so it
       never lands on the descriptor (R2). The raw secret is handed to
       ``registry.register`` which writes it to Secrets Manager.
    4. Stamp ``created_by`` from the JWT-derived actor id so the audit
       trail (R1) is bound to the validated identity.
    5. ``registry.register`` does the conditional ``PutItem`` and SM
       write atomically from the caller's perspective. On
       :class:`DuplicateGatewayError` we map back to
       ``{"error": "duplicate_id"}`` for a stable UI contract.
    6. Once the row is persisted, call
       ``mcp_manager.register_gateway`` to bring the gateway live without
       a runtime restart. A failure here is logged but does **not**
       fail the action — the row is durable and ``gateway_reload``
       (task 5.5) can recover the live connection later.
    7. Refresh the agent's tool registry so any tools the new gateway
       advertises are visible to the running agent on the next turn.
    """
    if not _is_admin(context, payload=payload):
        return {"error": "admin_required"}

    if not isinstance(payload, dict):
        return {"error": "invalid_payload", "message": "payload must be an object"}

    required = ("id", "name", "url", "auth_type")
    missing = [f for f in required if not payload.get(f)]
    if missing:
        return {
            "error": "invalid_payload",
            "message": f"missing required field(s): {', '.join(missing)}",
        }

    auth_type = payload.get("auth_type")
    auth_config_raw = payload.get("auth_config") or {}
    if not isinstance(auth_config_raw, dict):
        return {
            "error": "invalid_payload",
            "message": "auth_config must be an object",
        }
    # Work on a copy so we never mutate the caller's payload, and so
    # popping `client_secret` doesn't leak into any retry path.
    auth_config_dict = dict(auth_config_raw)
    # The discriminator on the inner dict is what `_auth_config_from_dict`
    # in gateway_registry consumes; honour either source.
    auth_config_dict.setdefault("auth_type", auth_type)

    # Accept either credential shape: `client_secret` for OAuth2
    # client-credentials, `token` for a static bearer / API key. Both are
    # popped unconditionally — short-circuiting with `or` would leave the
    # unused key in the dict and persist a literal credential on the row,
    # breaking R2. Whichever is present funnels into `raw_secret`, which
    # `GatewayRegistry.register` writes to Secrets Manager before the
    # DDB write so the row only ever holds an ARN.
    _literal_client_secret = auth_config_dict.pop("client_secret", None)
    _literal_token = auth_config_dict.pop("token", None)
    raw_secret = _literal_client_secret or _literal_token or None

    try:
        descriptor = GatewayDescriptor.from_dict(
            {
                "id": payload["id"],
                "name": payload["name"],
                "description": payload.get("description", ""),
                "url": payload["url"],
                "auth_type": auth_type,
                "auth_config": auth_config_dict,
                "enabled": payload.get("enabled", True),
            }
        )
    except (KeyError, ValueError) as exc:
        return {"error": "invalid_payload", "message": str(exc)}

    # Audit trail: bind the row to the validated JWT identity, not anything
    # the caller put in the payload (other than the proxy-stamped __actor_id,
    # which the proxy sets after JWT validation at the edge).
    try:
        descriptor.created_by = _resolve_actor_id(context, payload=payload)
    except RuntimeError as exc:
        # _is_admin already passed, so the JWT is present — but be
        # explicit if the resolver still can't find an actor id.
        return {"error": "actor_unresolved", "message": str(exc)}

    try:
        await _gateway_registry.register(descriptor, raw_secret=raw_secret)
    except DuplicateGatewayError:
        return {"error": "duplicate_id"}
    except ValueError as exc:
        return {"error": "invalid_payload", "message": str(exc)}

    # The DDB row is persisted at this point. Live registration is
    # best-effort: a failure here means tools won't appear until the
    # next reload, but the descriptor survives so the row isn't lost.
    try:
        await mcp_manager.register_gateway(descriptor)
    except Exception as exc:
        logger.warning(
            "Gateway '%s': live registration failed after DDB write: %s",
            descriptor.id, exc,
        )
    else:
        # Refresh the agent's tool registry so the new gateway's tools
        # are visible on the next turn. Wrapped in its own try so a
        # rebuild error doesn't shadow a successful registration.
        try:
            _rebuild_agent_tools()
        except Exception as exc:
            logger.warning(
                "Gateway '%s': tool registry rebuild failed: %s",
                descriptor.id, exc,
            )

    return descriptor.public_dict()


async def _gateway_update(payload, context):
    """Handle the ``gateway_update`` admin action (R3, R4).

    Flow:

    1. Admin gate via :func:`_is_admin`. Non-admins get
       ``{"error": "admin_required"}`` with no DDB contact.
    2. Validate that the payload carries an ``id``; everything else is
       optional and treated as a partial patch.
    3. Hand the patch (the payload minus ``id``) to
       ``registry.update``. The registry layer enforces which fields
       are mutable and preserves ``client_secret_arn`` when the patch
       doesn't explicitly rotate it (per task 2.5).
    4. ``update`` returns ``None`` when no row exists, which we surface
       as ``{"error": "not_found"}`` for a stable UI contract.
    5. On success, refresh the live connection by calling
       ``mcp_manager.unregister_gateway`` followed by
       ``register_gateway(updated)``. The unregister is idempotent;
       a register failure (e.g. bad URL) is logged but does **not**
       fail the action — the DDB row is durable, ``gateway_reload``
       (task 5.5) can recover the live state later.
    6. Refresh the agent's tool registry so the patched gateway's tool
       list (which may have shifted) is visible on the next turn.
    """
    if not _is_admin(context, payload=payload):
        return {"error": "admin_required"}

    if not isinstance(payload, dict):
        return {"error": "invalid_payload", "message": "payload must be an object"}

    gateway_id = payload.get("id")
    if not gateway_id:
        return {
            "error": "invalid_payload",
            "message": "missing required field(s): id",
        }

    # The patch is everything except the id; registry.update only reads
    # mutable fields, so extra keys are harmless but we strip `id` to
    # keep the surface tight and avoid round-tripping it through the
    # patch path.
    patch = {k: v for k, v in payload.items() if k != "id"}

    try:
        updated = await _gateway_registry.update(gateway_id, patch)
    except ValueError as exc:
        return {"error": "invalid_payload", "message": str(exc)}

    if updated is None:
        return {"error": "not_found"}

    # Refresh the live connection. Unregister is idempotent (it tolerates
    # an unknown id), so we can call it unconditionally; register may
    # fail on a bad URL or unreachable endpoint, but the row is
    # already persisted so we log and move on.
    try:
        await mcp_manager.unregister_gateway(updated.id)
    except Exception as exc:
        logger.warning(
            "Gateway '%s': unregister during update failed: %s",
            updated.id, exc,
        )

    try:
        await mcp_manager.register_gateway(updated)
    except Exception as exc:
        logger.warning(
            "Gateway '%s': live re-registration after update failed: %s",
            updated.id, exc,
        )

    try:
        _rebuild_agent_tools()
    except Exception as exc:
        logger.warning(
            "Gateway '%s': tool registry rebuild after update failed: %s",
            updated.id, exc,
        )

    return updated.public_dict()


async def _gateway_delete(payload, context):
    """Handle the ``gateway_delete`` admin action (R3, R4).

    Soft-delete: flips ``enabled`` to ``False`` via
    ``registry.disable`` and tears down the live MCP connection. The
    DDB row is intentionally preserved so the audit trail
    (``created_by`` / ``created_at``) survives and an admin can
    re-enable later by toggling.

    Flow:

    1. Admin gate via :func:`_is_admin`. Non-admins get
       ``{"error": "admin_required"}``.
    2. Validate that the payload carries an ``id``.
    3. Call ``registry.disable``; ``None`` means no row exists, which
       we surface as ``{"error": "not_found"}``.
    4. Call ``mcp_manager.unregister_gateway`` (idempotent) to drop the
       live connection so its tools stop appearing in the consent UI.
    5. Refresh the agent's tool registry.
    """
    if not _is_admin(context, payload=payload):
        return {"error": "admin_required"}

    if not isinstance(payload, dict):
        return {"error": "invalid_payload", "message": "payload must be an object"}

    gateway_id = payload.get("id")
    if not gateway_id:
        return {
            "error": "invalid_payload",
            "message": "missing required field(s): id",
        }

    try:
        disabled = await _gateway_registry.disable(gateway_id)
    except ValueError as exc:
        return {"error": "invalid_payload", "message": str(exc)}

    if disabled is None:
        return {"error": "not_found"}

    # Idempotent: tolerates an id that's already absent from _gateways.
    try:
        await mcp_manager.unregister_gateway(gateway_id)
    except Exception as exc:
        logger.warning(
            "Gateway '%s': unregister during delete failed: %s",
            gateway_id, exc,
        )

    try:
        _rebuild_agent_tools()
    except Exception as exc:
        logger.warning(
            "Gateway '%s': tool registry rebuild after delete failed: %s",
            gateway_id, exc,
        )

    return {"status": "disabled", "id": gateway_id}


async def _gateway_reload(payload, context):
    """Handle the ``gateway_reload`` admin action (R4).

    Forces the runtime's :class:`MCPManager` to reconcile its in-memory
    ``_gateways`` map with the current DDB state of the registry.
    Useful when a sibling runtime has mutated the registry, when an
    admin has hand-edited DDB out of band, or when a previous live
    register/unregister failed and left the runtime out of sync with
    the durable record.

    Flow:

    1. Admin gate via :func:`_is_admin`. Non-admins get
       ``{"error": "admin_required"}`` with no DDB or SM contact.
    2. Snapshot the current ``_gateways`` ids before any mutation so
       we can compute the set of stale entries to evict after reload.
    3. Call ``mcp_manager.load_gateways_from_registry`` with the
       module-level registry. The loader does an in-place
       upsert keyed on gateway id — it will refresh entries that
       still exist in DDB but won't drop entries whose rows have
       been disabled or deleted.
    4. For every id that was in the snapshot but is **not** in the
       freshly loaded set, call ``mcp_manager.unregister_gateway``
       so stale connections (e.g. a gateway disabled on another
       runtime since the last reload) are torn down here too.
       Idempotent — tolerates an id that's already absent.
    5. Refresh the agent's tool registry so the gateway tool surface
       (which may have grown or shrunk) is visible on the next turn.

    Returns:
        ``{"status": "reloaded", "gateways": [...]}`` listing the ids
        currently live after the reload completes.
    """
    if not _is_admin(context, payload=payload):
        return {"error": "admin_required"}

    # Snapshot before mutation so we can compute the eviction set.
    # `list(...)` so we're not iterating a dict while it's being
    # rewritten by the loader below.
    previous_ids = list(mcp_manager._gateways.keys())

    loaded_ids = await mcp_manager.load_gateways_from_registry(_gateway_registry)

    # Keep the shared Platform gateway (SigV4, plane=platform) connected across reloads.
    try:
        _pgid = await mcp_manager.load_platform_gateway_from_env()
        if _pgid:
            logger.info("Loaded platform gateway: %s", _pgid)
    except Exception as e:
        logger.error("Failed to load platform gateway on reload: %s", e)

    # Connect all loaded gateways (OAuth handshake + tools/list)
    try:
        results = mcp_manager.connect_all()
        for gw_id, result in results.items():
            s = result.get("status", "unknown")
            if s == "connected":
                logger.info("Gateway '%s': connected with %d tools", gw_id, len(result.get("tools", [])))
            else:
                logger.warning("Gateway '%s': %s", gw_id, result)
    except Exception as e:
        logger.error("connect_all after reload failed: %s", e)

    # Anything in the snapshot that didn't come back from DDB is now
    # stale — drop the live connection so its tools stop appearing.
    stale_ids = [gw_id for gw_id in previous_ids if gw_id not in loaded_ids]
    for gw_id in stale_ids:
        try:
            await mcp_manager.unregister_gateway(gw_id)
        except Exception as exc:
            logger.warning(
                "Gateway '%s': unregister during reload failed: %s",
                gw_id, exc,
            )

    try:
        _rebuild_agent_tools()
    except Exception as exc:
        logger.warning(
            "Tool registry rebuild after gateway reload failed: %s", exc,
        )

    return {"status": "reloaded", "gateways": loaded_ids}


async def _gateway_test_connection(payload, context):
    """Handle the ``gateway_test_connection`` admin action (R4, R7).

    Verifies that a gateway is reachable and reporting tools — the
    "is this thing on?" probe behind the admin UI's "Test connection"
    button. Works for both already-live gateways (uses the existing
    :class:`GatewayConnection`) and gateways that exist in DDB but
    aren't currently in :attr:`MCPManager._gateways` (live-registers
    them on the fly so the probe doesn't require a prior reload).

    Flow:

    1. Admin gate via :func:`_is_admin`. Non-admins get
       ``{"error": "admin_required"}`` with no DDB or SM contact.
    2. Validate that the payload carries an ``id``.
    3. Look up the gateway in ``mcp_manager._gateways``. If absent,
       fetch the descriptor from the registry and call
       :meth:`MCPManager.register_gateway` to bring it live. If the
       descriptor is also missing, return ``{"error": "not_found"}``.
    4. Refresh the live connection's tool list via
       :meth:`MCPManager.refresh_gateway` so the count reflects the
       gateway's current ``tools/list`` response, not a stale snapshot.
    5. Return ``{"ok": True, "tool_count": <n>, "sample_tool_names":
       <up to 10 names>}``.
    6. Any exception below the admin/validation gate is surfaced as
       ``{"ok": False, "error": str(exc)}`` — the UI renders that
       inline on the row, so structured-but-stringy is the right shape.
    """
    if not _is_admin(context, payload=payload):
        return {"error": "admin_required"}

    if not isinstance(payload, dict):
        return {"error": "invalid_payload", "message": "payload must be an object"}

    gateway_id = payload.get("id")
    if not gateway_id:
        return {
            "error": "invalid_payload",
            "message": "missing required field(s): id",
        }

    try:
        gw = mcp_manager._gateways.get(gateway_id)
        if gw is None:
            # Not live yet — see if the registry knows about it and
            # bring it online for the probe.
            descriptor = await _gateway_registry.get(gateway_id)
            if descriptor is None:
                return {"error": "not_found"}
            await mcp_manager.register_gateway(descriptor)
            gw = mcp_manager._gateways.get(gateway_id)
            if gw is None:
                # register_gateway should have inserted it; if it
                # didn't, treat as a connection failure.
                return {"ok": False, "error": "registration_failed"}

        # Refresh against the live gateway so tool_count reflects the
        # current `tools/list` response, not a stale snapshot from a
        # prior connect. `refresh_tools` is synchronous (httpx + MCP
        # client), so dispatch it to a worker thread.
        refresh_result = await asyncio.to_thread(gw.refresh_tools)
        if isinstance(refresh_result, dict) and refresh_result.get("error"):
            return {"ok": False, "error": refresh_result["error"]}

        tool_names = [m["name"] for m in gw._tool_metadata]
        return {
            "ok": True,
            "tool_count": len(tool_names),
            "sample_tool_names": tool_names[:10],
        }
    except Exception as exc:
        logger.exception("gateway_test_connection failed for '%s'", gateway_id)
        return {"ok": False, "error": str(exc)}


def _run_container_warm_up() -> None:
    """Warm up before serving, within a time budget (the platform's init window is 30s).

    The warm-up runs on a thread joined for at most ``CONTAINER_WARMUP_BUDGET_S``: past that the
    server starts anyway and the warm-up finishes in the background -- the first invocation then
    waits on the same init lock instead of building a second copy. ``CONTAINER_WARMUP=0`` turns it
    off (pure lazy init, the previous behaviour).
    """
    if os.environ.get("CONTAINER_WARMUP", "1").strip().lower() in ("0", "false", "no", "off"):
        return
    budget = float(os.environ.get("CONTAINER_WARMUP_BUDGET_S", "15"))
    t = threading.Thread(target=warm_up, name="container-warm-up", daemon=True)
    t.start()
    t.join(timeout=max(0.0, budget))
    if t.is_alive():
        logger.warning("container warm-up still running after %.0fs; serving anyway", budget)


if __name__ == "__main__":
    _run_container_warm_up()
    app.run()
