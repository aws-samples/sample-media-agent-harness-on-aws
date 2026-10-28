"""Plan Chainer Lambda — reactive DAG orchestrator.

Triggered by DDB Streams on the Plan State table. When a node
transitions to a terminal state (SUCCEEDED/FAILED), this Lambda:
  1. Reads the full plan from the Plan State table
  2. Determines which downstream nodes are now READY
  3. Dispatches each READY node to the Task Agent (fire & forget)
  4. If all nodes are terminal, marks the parent run complete

Each Task Agent invocation is a single-action call (~15-30s).
This Lambda never waits for a response — purely reactive.
"""

import hashlib
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

from gateway_invoker import (
    GatewayDescriptorMissing,
    GatewayInvocationError,
    invoke_mcp_tool,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Payload keys whose values never reach a log line (media-harness-projects-access, design Component 7
# "Dispatch and plans carry no token"). Must equal agent/src/project_search/scrub.py's set; the agent
# suite's test_search_binding_wiring.py keeps them in step. The chainer never ADDS a project_token:
# node payloads are built from the plan record, which carries none.
SECRET_PAYLOAD_KEYS = frozenset({"project_token", "delegation_grant", "user_token", "search_dispatch"})


def _scrub_payload(payload: dict) -> dict:
    """A copy of an outbound payload safe to log: secret keys keep their name, never their value;
    long strings are cut and collections counted."""
    out = {}
    for k, v in payload.items():
        if k in SECRET_PAYLOAD_KEYS:
            out[k] = "<redacted>"
        elif isinstance(v, str) and len(v) > 80:
            out[k] = f"{v[:80]}…(+{len(v) - 80})"
        elif isinstance(v, (list, tuple, dict)):
            out[k] = f"<{len(v)} {'keys' if isinstance(v, dict) else 'items'}>"
        else:
            out[k] = v
    return out

PLAN_STATE_TABLE = os.environ["PLAN_STATE_TABLE"]
JOB_REGISTRY_TABLE = os.environ["JOB_REGISTRY_TABLE"]
TASK_AGENT_ARN = os.environ["TASK_AGENT_RUNTIME_ARN"]
SKILLS_S3_BUCKET = os.environ.get("SKILLS_S3_BUCKET", "")
# projects.lineageEdges: record each node's inputs (what it was handed) and outputs (what it made) on its
# job-registry row, which the Generated_Media registrar turns into lineage edges. Off: nothing is recorded.
LINEAGE_EDGES = (os.environ.get("LINEAGE_EDGES") or "").strip().lower() == "true"
ARTIFACTS_DOMAIN = os.environ.get("ARTIFACTS_DOMAIN", "")
# A plan's node list is stored inline on the PLAN row up to this size; beyond it (a large
# fan-out expands the graph past DynamoDB's 400KB item cap) it is offloaded to S3 with a
# nodes_s3_key pointer. See _load_nodes_json / _store_nodes_json_on_plan.
NODES_JSON_INLINE_MAX = 300_000
# Auto-imported plan deliverables land under this prefix; a bucket lifecycle rule expires it so
# the copies don't accumulate (the source of truth stays the M2C proxy bucket).
_IMPORT_PREFIX = "artifacts/imports/"
# Sliding-TTL window for plan-state rows (D-08). DynamoDB TTL is an absolute expiry timestamp.
# It is stamped at row CREATE and RE-STAMPED on every in-flight write that represents genuine
# progress, so an actively-progressing plan keeps its DAG-state rows alive while a STALLED plan
# still ages out at 24h-from-last-progress (that TTL sweep is D-01's only stop for a stalled
# plan — this fix must not weaken it). Defined once here so the sliding-window writes funnel
# through _ttl_epoch() instead of copy-pasting `+ 86400`; the create paths keep their own inline
# literal of the same value (deliberately not touched by D-08).
_PLAN_TTL_SECONDS = 86400

# WATCHDOG: how long a node may sit RUNNING with no progress before the chainer force-fails it so
# the plan can settle. A hung tool/code_interpreter that never writes a terminal status (session
# 20f7315e's seg1_validate: a code_interpreter probe blocked ~15 min and the plan hung RUNNING
# until the 24h TTL sweep) is otherwise invisible to the settle logic, which only reaps FAILED
# nodes. Set well above the node's own execution timeout (PLAN_NODE_EXEC_TIMEOUT, default 600s) so
# this only fires for a genuinely dead node (e.g. a runtime that died without writing terminal),
# never a node doing legitimate long work. Nodes with an in-flight gateway/SFN job are exempt.
#
# 900s = the node's own 600s PLAN_NODE_EXEC_TIMEOUT + 300s for what that timeout does not cover
# (gateway connects before the turn, the durable writes after it). It is evaluated on every stream
# event AND once a minute by the scheduled sweep (_watchdog_sweep): before the sweep existed a dead
# node was only noticed when some OTHER event happened to fire the chainer, so plan 81d9198e's two
# nodes lost with their microVM sat RUNNING for 3h18m ("no progress for 11922s").
_PLAN_NODE_STALL_SECONDS = int(os.environ.get("PLAN_NODE_STALL_SECONDS", "900"))

# Ready nodes are INVOKED concurrently (the claim/prepare and post-invoke writes stay serial). The
# serial loop dispatched plan 81d9198e's 8 shots over 29.4s -- two 502 retries and a 15s wait for a
# replacement microVM sat in front of every later sibling. Bounded so a wide map fan-out cannot
# open an unbounded number of connections from a 256 MB Lambda.
_DISPATCH_CONCURRENCY = max(1, int(os.environ.get("PLAN_DISPATCH_CONCURRENCY", "8")))

# The scheduled watchdog's work list: one small row per in-flight plan, in its OWN partition of the
# plan-state table (like HITS#), so the minute sweep is a single-partition Query instead of a Scan of
# a table whose PLAN rows carry up to 300 KB of nodes_json each. Written when a plan first
# dispatches, deleted when it settles; the 24h TTL is the backstop.
_WATCH_PARTITION = "WATCH#plans"
# The job registry GSI keyed (actorId, updatedAt): the only index that can find a job a RUNNING
# node registered mid-turn (its plan-state row gets job_id only at the END of the turn).
JOB_REGISTRY_ACTOR_INDEX = os.environ.get("JOB_REGISTRY_ACTOR_INDEX", "actor-status-index")

# Shipped as a Lambda layer (mounted on sys.path at /opt/python) so the polling
# worker and this handler share ONE copy of the resolution rules. Duplication is
# how the queue endpoint rule came to be fixed three times in three places.
import result_resolution
import fanout_extract  # shared layer (Phase 2) — pure collection extractor for map fan-out
GATEWAY_REGISTRY_TABLE = os.environ.get("GATEWAY_REGISTRY_TABLE", "")
REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-west-2"))


def _fanout_enabled() -> bool:
    """Whether runtime map/gather expansion is on. Mirrors dag_schema.dynamic_fanout_enabled
    (the agent side) so emission, the validator gate, and chainer expansion turn on together."""
    return os.environ.get("DYNAMIC_FANOUT_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")

# Reuse clients across invocations (Lambda warm start)
_ddb = boto3.resource("dynamodb", region_name=REGION)
_plan_table = _ddb.Table(PLAN_STATE_TABLE)
_job_table = _ddb.Table(JOB_REGISTRY_TABLE)
_gateway_table = _ddb.Table(GATEWAY_REGISTRY_TABLE) if GATEWAY_REGISTRY_TABLE else None
_s3 = boto3.client("s3", region_name=REGION)
_secrets = boto3.client("secretsmanager", region_name=REGION)
_agentcore = boto3.client(
    "bedrock-agentcore",
    region_name=REGION,
    config=boto3.session.Config(
        read_timeout=30,  # Enough to get the initial response acceptance
        connect_timeout=10,
        retries={"max_attempts": 1},
    ),
)

# Minimum length AgentCore enforces on runtimeSessionId; a shorter value is
# rejected outright with ValidationException. DUPLICATED from
# jobs/context.py's _MIN_RUNTIME_SESSION_ID_LEN and MUST stay in lock-step with
# it (same arrangement as SFN_EXECUTION_ARN_RE below). Not shared via the
# result-resolution layer because the OTHER copy that can drift lives in the
# agent runtime, which is a separate artifact with no /opt/python mount — a layer
# module would add a third home for this rule rather than remove the second.
_MIN_RUNTIME_SESSION_ID_LEN = 33

# AgentCore uses runtimeSessionId for microVM stickiness, so while a session is
# being provisioned (or torn down) a SECOND operation naming it is rejected with a
# retryable HTTP 409 RetryableConflictException, "Session operation in progress,
# please retry". Now that every node of a plan shares ONE session id this is
# reachable: _advance_plan dispatches all dependency-free nodes — and every child
# of a map expansion — from one serial loop, so siblings 2..N can land inside the
# window the first invoke opened (the first invoke is what provisions, and an
# already-running session is unaffected). The prescribed response is to retry the
# SAME session with short backoff, NOT to abandon it: dropping the session here
# would run the node in a fresh uncorrelated session, i.e. reproduce the very
# defect this propagation exists to fix, and be indistinguishable from it in the
# spans. A 409 also means the invoke was REJECTED, so retrying cannot double-
# dispatch the node.
_SESSION_CONFLICT_BACKOFF_S = (0.2, 0.4)
# Bound on the multiplier across a wide fan-out. Sleeping in Lambda burns billed
# duration and this function has a 60s timeout with settle work still to do, so
# ONE invocation may spend at most this long in conflict backoff no matter how many
# nodes it dispatches; past that, dispatch degrades to no session rather than
# risking the timeout. 3s is 5% of the budget, and the realistic cost is far lower:
# once any sibling gets through, the session exists and the rest cannot conflict.
_SESSION_CONFLICT_SLEEP_BUDGET_S = 3.0
# Reset per invocation in handler(); module scope so a warm container starts each
# invocation with the full budget instead of inheriting a spent one.
_session_conflict_sleep_left = _SESSION_CONFLICT_SLEEP_BUDGET_S
# Concurrent dispatch shares that budget across worker threads.
_session_conflict_lock = threading.Lock()

# Per-node runtime sessions. Every node of a plan used to be invoked with the PARENT chat session as
# its runtimeSessionId, so all of them ran in ONE microVM: when it was replaced after a 502 (plan
# 81d9198e) the nodes running inside it died silently together. Each node now gets its own session,
# derived deterministically from (plan_id, node_id, attempt): a re-delivered stream batch reuses the
# same id (idempotent), a retry gets a fresh one (a hung microVM is never reused for the retry).
# Everything that ATTRIBUTES work -- payload session_id / __plan_session_id, which the task agent uses
# for session outputs, render manifests, job-registry rows and plan-state keys -- stays the parent.
# The "plan-session-" prefix is the harness's own sub-session marker
# (improve.classify.PLAN_SESSION_PREFIX), so the evaluators classify these as plan-node sub-sessions.
_NODE_SESSION_PREFIX = "plan-session-"
_MAX_RUNTIME_SESSION_ID_LEN = 256
_SESSION_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


def _node_runtime_session_id(plan_id, node_id, attempt=0, parent_session_id=""):
    """The runtimeSessionId a plan node is invoked with (see _NODE_SESSION_PREFIX).

    Deterministic, 33..256 chars of [A-Za-z0-9_-]. Falls back to ``parent_session_id`` only when
    there is no plan/node to derive from (a legacy caller).
    """
    plan = str(plan_id or "")
    node = str(node_id or "")
    if not plan or not node:
        return parent_session_id
    digest = hashlib.sha256(f"{plan}\x1f{node}\x1f{attempt}".encode()).hexdigest()
    safe_plan = _SESSION_UNSAFE_RE.sub("-", plan)
    safe_node = _SESSION_UNSAFE_RE.sub("-", node)
    suffix = f"-r{int(attempt)}" if attempt else ""
    # Sanitising can merge two ids ("a.b" / "a-b"); a short hash of the RAW ids keeps them apart.
    tag = f"-{digest[:8]}" if (safe_plan != plan or safe_node != node) else ""
    sid = f"{_NODE_SESSION_PREFIX}{safe_plan}-{safe_node}{tag}{suffix}"
    if len(sid) > _MAX_RUNTIME_SESSION_ID_LEN:
        tail = f"-{digest[:16]}{suffix}"
        sid = sid[: _MAX_RUNTIME_SESSION_ID_LEN - len(tail)] + tail
    if len(sid) < _MIN_RUNTIME_SESSION_ID_LEN:
        sid = f"{sid}-{digest[: _MIN_RUNTIME_SESSION_ID_LEN - len(sid) - 1]}"
    return sid

# Same pattern as the polling worker's SFN_EXECUTION_ARN_RE (and
# jobs/interceptor.py's copy) — the two/three MUST stay in lock-step.
SFN_EXECUTION_ARN_RE = re.compile(
    r"^arn:aws:states:[^:]+:\d+:execution:[^:]+:[^:]+$"
)

# SFN clients keyed by region — execution ARNs may live in a different
# region than this Lambda. Mirrors polling-worker/handler.py.
_sfn_clients_by_region: dict[str, Any] = {}


def _sfn_client_for_arn(execution_arn: str) -> Any:
    region = execution_arn.split(":")[3]
    client = _sfn_clients_by_region.get(region)
    if client is None:
        client = boto3.client("stepfunctions", region_name=region)
        _sfn_clients_by_region[region] = client
    return client


TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED_OUT", "completed", "failed"}

# Statuses that the chainer should react to (dispatch results-fetch or chain downstream)
ACTIONABLE_STATUSES = TERMINAL_STATUSES | {"results_ready"}


def _has_pending_gateway_job(node_states: dict) -> bool:
    """True if any node spawned a gateway job (SFN execution) that is not yet terminal.

    A node that fires a fire-and-forget gateway job stores that job's SFN execution ARN
    in its ``job_id``. If the gateway job is still running, the PLAN is NOT done — firing
    the "workflow finished" notification now is the premature-completion bug (F2): a node
    whose Task-Agent turn returned (submission accepted) can be marked terminal while its
    Step Functions execution is still running. Each job is looked up by its PRIMARY KEY
    (GetItem, no GSI needed).

    Deadlock-safe: when a gateway job reaches terminal, the changefeed bridge
    UNCONDITIONALLY rewrites its node to results_ready/failed (matched by job_id, not
    status), which re-fires this lambda — so a deferred plan always gets another chance
    to settle. Fail-open on any read error so a transient failure can never wedge a plan.
    """
    for row in node_states.values():
        job_id = row.get("job_id")
        # Any REGISTERED non-terminal job blocks completion — SFN executions AND fal/queue
        # request_ids alike. fal jobs are registered by the interceptor and polled to terminal by
        # the polling worker, so an HOURS-long render/transcode is deferred on (not raced against
        # the node's own 600s budget). A node's coordinator id (plan#node) or any non-job ref is
        # simply absent from the registry → GetItem returns None → not treated as pending. Earlier
        # this filtered to `arn:aws:states:` (SFN only), so fal animate nodes were never waited on.
        if not job_id:
            continue
        try:
            item = _job_table.get_item(Key={"jobId": job_id}).get("Item")
        except Exception:
            logger.warning("F2 guard: gateway job lookup failed for %s; not deferring", job_id, exc_info=True)
            continue
        if item and str(item.get("status", "")).strip() not in TERMINAL_STATUSES:
            return True
    return False


def _node_gateway_pending(row: dict) -> bool:
    """True if THIS node spawned a gateway SFN job that is not yet terminal.

    Per-node counterpart of ``_has_pending_gateway_job``. Used to gate DOWNSTREAM DISPATCH:
    a node's Task-Agent turn returns (and the node is marked ``completed``) the moment it
    SUBMITS a fire-and-forget gateway job — long before that Step Functions execution
    finishes and writes its output. A dependent dispatched off that premature ``completed``
    runs against outputs that do not exist yet (the 369bbe3e bug: t3_translate_fr ran ~40s
    before the diarization SFN finished, found no ``final_transcript.json``, and failed).

    Looks the job up by PRIMARY KEY (GetItem, no GSI). Fail-OPEN on any read error or
    untracked job so a transient failure or an untracked ref can never wedge a dependent
    forever — it degrades to the old (dispatch-immediately) behaviour rather than deadlock.
    """
    job_id = row.get("job_id")
    if not job_id:  # covers SFN AND fal/queue request_ids (registry presence is the real test)
        return False
    try:
        item = _job_table.get_item(Key={"jobId": job_id}).get("Item")
    except Exception:
        logger.warning("dispatch gate: gateway job lookup failed for %s; not gating", job_id, exc_info=True)
        return False
    return bool(item and str(item.get("status", "")).strip() not in TERMINAL_STATUSES)


# Terminal-FAILURE statuses a fire-and-forget gateway job can land in (subset of
# TERMINAL_STATUSES that is NOT success). "SUCCEEDED"/"completed" are the success terminals.
_GATEWAY_FAILURE_STATUSES = {"FAILED", "ABORTED", "TIMED_OUT", "failed"}

# Plan-state statuses whose node/plan produced no usable output for a later phase's
# external_inputs (a re-run supersedes them; see _external_producer_pool).
_DEAD_PRODUCER_STATUSES = frozenset({"failed", "skipped"})


def _node_gateway_failed(row: dict) -> bool:
    """True if THIS node's fire-and-forget gateway SFN reached a terminal FAILURE.

    A node is marked ``completed`` the moment its Task-Agent turn returns — which for an async
    node is when it SUBMITS the gateway job, well before that Step Functions execution finishes.
    So a producer can read ``completed`` while its SFN later FAILS (e.g. ``probe_media_file`` 404s
    on a corrupted source path). ``_node_gateway_pending`` only tells pending from terminal, so a
    terminally-FAILED producer would otherwise count as ``succeeded`` and its dependents would be
    dispatched against an output that will never exist (observed: the diarization SFN failed at
    probe, yet ``sg4_analyze_conversation`` was dispatched and then errored that it could not
    resolve the diarization ``output_s3_prefix``). Excluding it defers the dependent until the
    changefeed bridge rewrites this node to ``failed``, after which the dead-dependency skip
    cascades correctly (the dependent is SKIPPED, not run against a phantom output).

    Looks the job up by PRIMARY KEY (GetItem, no GSI). Fail-OPEN on any read error / untracked
    ref so a transient failure can never wedge a plan — it degrades to the prior behaviour."""
    job_id = row.get("job_id")
    if not job_id:  # covers SFN AND fal/queue request_ids (registry presence is the real test)
        return False
    try:
        item = _job_table.get_item(Key={"jobId": job_id}).get("Item")
    except Exception:
        logger.warning("dispatch gate: gateway-failed lookup failed for %s; not gating", job_id, exc_info=True)
        return False
    return bool(item and str(item.get("status", "")).strip() in _GATEWAY_FAILURE_STATUSES)


MAX_NODE_RETRIES = int(os.environ.get("PLAN_NODE_MAX_RETRIES", "1"))


def _is_missing_input_error(error: str) -> bool:
    """A failure caused by a MISSING / UNRESOLVABLE INPUT reference — not a correctable parameter.

    Design §5: retrying re-dispatches the node WITH its error so the agent can fix a wrong/empty
    PARAMETER. But if the input itself is absent (a dependency's file/uri that doesn't exist, an
    unresolved reference), the agent cannot fix that by GUESSING a key — that path is exactly how
    a node invents ``final_transcript.json`` and fails again differently. A missing input is a
    handoff gap the framework must close (A1 hands over the persisted-file reference; A2 resolves
    the named deliverable), so we FAIL FAST here instead of retrying blindly."""
    e = (error or "").lower()
    signatures = (
        "nosuchkey", "no such key", "does not exist", "could not resolve reference",
        "unresolved reference", "no s3 uri", "no uri provided", "does not contain",
        "missing required input", "input not provided", "no input found", "referenced file not found",
        "ambiguous external_inputs",
    )
    return any(sig in e for sig in signatures)


def _is_content_policy_error(error: str) -> bool:
    """A provider CONTENT-POLICY rejection — e.g. a fal/Seedance image→video 422 that
    flags a real-person likeness in the REFERENCE IMAGE ("potentially containing
    likenesses of real people").

    Like a missing input, this is NOT a correctable parameter: the block is on the
    image the node was handed, so re-dispatching re-submits the SAME flagged image and
    422s again — retrying just loops (observed: plan 2cb0fec0 node shot6 oscillated
    complete→failed, burning a paid generation each pass). Fail fast to terminal. The
    real fix for a genuinely-wanted frame is a different / more-abstract image, which
    the author-side edit prompt must handle (see media-references) — never a chainer
    retry of the identical input."""
    e = (error or "").lower()
    signatures = (
        "content_policy_violation", "content policy", "partner validation",
        "likeness", "real person", "real people", "content filter",
    )
    return any(sig in e for sig in signatures)


def _is_mcp_transport_error(error: str) -> bool:
    """A gateway MCP TRANSPORT / SESSION failure — e.g. Strands'
    ``MCPClientInitializationError: the client session is not running``.

    Like a content-policy block, this is NOT a correctable parameter: the gateway's
    MCP session died, and re-dispatching the node re-submits into the SAME dead session
    and fails identically. Retrying just loops (observed: plan 0e134284 fal Kling/MiniMax
    submit nodes churned failed→running every ~1-2s for >1h, burning a Bedrock node
    invocation each pass, because the varying LLM-reasoning prefix defeated the
    error-signature stop). Fail fast to terminal — the real fix is MCP session reconnect
    in mcp_manager, never a chainer retry of the identical dead-session call."""
    e = (error or "").lower()
    signatures = (
        "mcpclientinitializationerror", "client session is not running",
        "the client session", "session is not running", "mcp client",
        "mcp transport", "transport-layer failure",
    )
    return any(sig in e for sig in signatures)


def _is_result_fetch_error(error: str) -> bool:
    """A node whose async job SUBMITTED fine but whose RESULT FETCH then failed — fal marks
    these with an ``upstream_request_id`` / ``http_status`` (the submit reached fal's queue and
    returned a request id; fetching the generated artifact is what 404'd).

    Re-dispatching such a node RE-SUBMITS a brand-new (paid) generation to recover a fetch that
    failed — never correct and non-convergent: session 6c69afb4 node sg1_video submitted 45
    fal-ai/minimax videos over 49 min, each ending in this fetch 404, stalling the mux node. The
    submit already happened; the right recovery is to re-FETCH (or reconnect), never re-generate.
    So THIS variant fails fast to terminal here.

    It must NOT swallow the CORRECTABLE variants that share the "Result fetch failed" prefix but
    have NO upstream request id, because those are SUBMIT-time errors the model can re-emit
    correctly and a re-dispatch legitimately recovers (evidence: e7292dc1 gen_shot1-8 all
    recovered on retry):
      * a wrong-endpoint 404 — ``Result fetch failed (404): {"detail":"Path /h3-max/image-to-video
        not found"}`` (model dropped the ``minimax/`` prefix), and
      * a ``(422)`` validation error on the parameters.
    Discriminating on the ``upstream_request_id`` / ``http_status`` marker (present only once the
    submit succeeded) keeps the burn case fail-fast while leaving the correctable ones retryable.
    Worst case of a mis-signatured burn is now ONE bounded retry (the cap works), not 45."""
    e = (error or "").lower()
    fetch_family = (
        "result fetch failed" in e
        or "result_fetch_failed" in e
        or ("result fetch" in e and "404" in e)
    )
    if not fetch_family:
        return False
    # Correctable submit-time variants (no upstream request id) stay retry-eligible.
    if "upstream_request_id" not in e and "http_status" not in e:
        return False
    return True


# Volatile fragments that make otherwise-identical errors look different every attempt,
# defeating the "same error -> stop" guard. A fal result-fetch 404 carries a fresh
# ``upstream_request_id`` each submit; async payloads also carry per-attempt job ids,
# timestamps, and presigned URLs. Stripping them lets the signature stop actually fire.
_VOLATILE_ERR_RE = re.compile(
    r"(upstream_request_id\"?\s*[:=]\s*\"?[0-9a-fA-F-]+"
    r"|request_id\"?\s*[:=]\s*\"?[0-9a-fA-F-]+"
    r"|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|\d{4}-\d{2}-\d{2}[tT]\d{2}:\d{2}:\d{2}[0-9.:+zZ-]*"
    r"|https?://[^\s\"']+)"
)


def _normalize_error_signature(error: str) -> str:
    """Collapse an error to its stable shape for the 'same error -> stop' comparison, by
    removing volatile per-attempt fragments (request ids, UUIDs, timestamps, URLs)."""
    return _VOLATILE_ERR_RE.sub("<v>", (error or "").strip()).lower()


def _should_retry_node(row: dict) -> bool:
    """Tier-1 error-aware self-correction eligibility (P2).

    A failed node is retried — re-dispatched WITH its error in context so the Task Agent
    can correct a wrong/empty parameter — when:
      * it carries an ``error`` (something to correct from; a bridge/gateway failure with
        no error text is not self-correctable here),
      * the error is a CORRECTABLE parameter error, NOT a missing/unresolvable INPUT (a
        missing input is a handoff gap — retrying by guessing a key just fails differently;
        see ``_is_missing_input_error``),
      * it is under the ``MAX_NODE_RETRIES`` cap, and
      * the error is NOT a repeat of the one we already retried (``last_retry_error``) —
        a deterministic failure won't fix itself by retrying the same way (error-signature
        stop). Together the cap + signature guarantee termination (no retry loop).
    """
    error = (row.get("error") or "").strip()
    if not error:
        return False
    # A node the changefeed bridge failed because its async JOB failed (gateway_status FAILED,
    # written only by the bridge) is not a parameter error: the submit was accepted and the
    # provider ran it to a deterministic failure (M2C "No VAD turns found"). Before the bridge
    # carried the job's reason these rows had no `error` and were never retried; writing the
    # reason must not newly make them retry-eligible -- a re-dispatch costs a task-agent turn and
    # possibly a fresh paid job, and a prose answer would overwrite the real reason.
    if str(row.get("gateway_status") or "").strip().upper() == "FAILED":
        return False
    if _is_missing_input_error(error):
        return False
    if _is_content_policy_error(error):
        return False
    if _is_mcp_transport_error(error):
        return False
    # The Task Agent could not BUILD the node (its agent / tools): the same payload builds the same way, so a retry
    # just fails again (plan 593333b5 retried eight such nodes, each after a 900s watchdog wait). Prefix =
    # task_agent.NODE_SETUP_FAILED.
    if error.startswith("Node setup failed"):
        return False
    # A RESULT-FETCH failure must not re-dispatch (which re-submits a fresh paid generation);
    # the submit already succeeded, so fail fast to terminal here (see _is_result_fetch_error).
    if _is_result_fetch_error(error):
        return False
    try:
        retries = int(row.get("retries", 0) or 0)
    except (TypeError, ValueError):
        retries = 0
    if retries >= MAX_NODE_RETRIES:
        return False
    # Error-signature stop: a deterministic failure won't fix itself by retrying the same way.
    # Compare NORMALISED signatures (volatile request-ids/uuids/timestamps/urls stripped) so a
    # varying-payload error can't defeat the guard, and fire it regardless of the retry count —
    # a re-dispatch write can reset ``retries`` (changefeed bridge rewrites the node row), so
    # relying on ``retries > 0`` alone let the loop run 45x on session 6c69afb4.
    last = (row.get("last_retry_error") or "").strip()
    if last and _normalize_error_signature(error) == _normalize_error_signature(last):
        return False
    return True


# Provider ACCESS / ENTITLEMENT rejection signatures. A submit against a model
# the connected account cannot use (e.g. a WRONG endpoint id — run 863816b4
# passed `fal-ai/minimax/h3-max/image-to-video`, whose real id has no `fal-ai/`
# prefix, so fal answered "This endpoint is unavailable for the connected
# account", which the model read as `insufficient_scope`). fal returns this on a
# COMPLETED call, so without detecting it the node laundered to SUCCEEDED (the
# false success + 232 non-renderable "jobs" observed on that run). Detected here
# so the node is marked FAILED — honest, and visible to the bounded, error-aware
# retry (`_should_retry_node`), which re-dispatches ONCE with this error so the
# agent can self-correct the id (drop the prefix / pick an accessible model)
# before the error-signature stop terminalises it.
_PROVIDER_ACCESS_SIGNATURES = (
    "insufficient_scope",
    "insufficient scope",
    "unavailable for the connected account",
    "select an accessible model",
    "endpoint is unavailable",
)


def _has_provider_access_error(text: str) -> bool:
    """True if *text* carries a provider access/entitlement rejection signature."""
    t = (text or "").lower()
    return any(sig in t for sig in _PROVIDER_ACCESS_SIGNATURES)


# How much of a failed result's payload is kept as the node's error (and logged). A fal 422 arrives
# wrapped in an MCP content envelope, so 500/200 chars cut off its ``field_errors`` / ``detail`` --
# the part that names the bad field (staging dcd11946: ``prompt_expansion_mode``).
_RESULT_ERROR_MAX_CHARS = 1000


def _result_error_text(raw_result) -> "str | None":
    """Return a short error string if a fetched node RESULT is actually a failure
    payload, else ``None``.

    A gateway job can COMPLETE (SFN SUCCEEDED / MCP call returned) while its payload
    is an error -- a fal 422 validation blob, an M2C ``status: FAILED``, an MCP
    ``isError``, or a ``content_policy_violation``. Terminalising such a node
    "completed" launders a real failure into a green node AND hides it from the P2
    retry gate (``_should_retry_node`` only fires on ``failed`` with an ``error``).
    Detect the error here so the node is marked FAILED instead: visible to recovery,
    and honestly terminal if it repeats.

    Conservative by design -- flags only CLEAR failure signals so a valid success is
    never marked failed. Top-level fields only (a nested sub-item ``status`` does not
    fail the whole node)."""
    obj = raw_result
    if isinstance(obj, (bytes, bytearray)):
        try:
            obj = obj.decode("utf-8", "replace")
        except Exception:
            return None
    if isinstance(obj, str):
        if "content_policy_violation" in obj.lower():
            return "content_policy_violation"
        if _has_provider_access_error(obj):
            return obj[:_RESULT_ERROR_MAX_CHARS]
        try:
            obj = json.loads(obj)
        except Exception:
            # Bare text -- only a very strong signal counts (avoid false positives
            # on prose that merely contains the word "error").
            return obj[:_RESULT_ERROR_MAX_CHARS] if '"iserror": true' in obj.lower() else None
    if not isinstance(obj, dict):
        return None

    def _short(o) -> str:
        try:
            return json.dumps(o, default=str)[:_RESULT_ERROR_MAX_CHARS]
        except Exception:
            return str(o)[:_RESULT_ERROR_MAX_CHARS]

    # 1. MCP tool error flag.
    if obj.get("isError") is True or obj.get("is_error") is True:
        return _short(obj)
    # 2. HTTP-style status >= 400 (fal 422, etc.). Booleans are not codes.
    for k in ("status_code", "statusCode", "http_status", "httpStatus", "status"):
        v = obj.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, int) and v >= 400:
            return _short(obj)
        if isinstance(v, str) and v.strip().isdigit() and int(v.strip()) >= 400:
            return _short(obj)
    # 3. Workflow status string that is explicitly a failure (M2C output_contract).
    st = obj.get("status")
    if isinstance(st, str) and st.strip().upper() in ("FAILED", "ERROR", "TIMED_OUT", "ABORTED"):
        return _short(obj)
    # 4. A populated error field (empty string / null / [] / {} are not errors).
    for k in ("error", "errorMessage", "error_message", "detail_error"):
        if obj.get(k):
            return _short({k: obj[k]})
    # 5. content_policy_violation surfaced inside an MCP content list.
    content = obj.get("content")
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict):
                t = c.get("text")
                if isinstance(t, str) and "content_policy_violation" in t.lower():
                    return "content_policy_violation"
    # 6. Provider access/entitlement rejection anywhere in the payload (a wrong /
    #    unentitled endpoint id — fal "unavailable for the connected account" /
    #    insufficient_scope — returned on a COMPLETED call; run 863816b4). Scan the
    #    serialized result so it is caught regardless of which key carries it
    #    (top-level `error` is already handled by #4; this also catches `detail`,
    #    an MCP content `text`, or a bare message).
    if _has_provider_access_error(_short(obj)):
        return _short(obj)
    return None


def _declared_our_bucket_keys(raw_result) -> list:
    """S3 keys, in OUR skills bucket, that a node's result CLAIMS as deliverables.

    Only URIs we can actually verify — ``s3://<SKILLS_S3_BUCKET>/<key>`` or
    ``https://<ARTIFACTS_DOMAIN>/<key>``. A foreign bucket or CDN (e.g. ``fal.media``) is skipped:
    the chainer cannot head it, so its "absence" here would prove nothing.
    """
    if not SKILLS_S3_BUCKET:
        return []
    text = raw_result if isinstance(raw_result, str) else json.dumps(raw_result, default=str)
    keys: list = []
    for m in re.finditer(r"s3://([^/\s\"']+)/([^\s\"']+)", text):
        if m.group(1) == SKILLS_S3_BUCKET:
            keys.append(m.group(2).rstrip("\"'.,)]}"))
    if ARTIFACTS_DOMAIN:
        for m in re.finditer(r"https://" + re.escape(ARTIFACTS_DOMAIN) + r"/([^\s\"'?]+)", text):
            keys.append(m.group(1).rstrip("\"'.,)]}"))
    seen = set()
    out = []
    for k in keys:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _object_exists(key: str) -> "bool | None":
    """True/False when existence of ``key`` in our bucket is determinable, else None (unknown).

    None (a non-404 error — permissions, throttle, transient) must NEVER be read as absence: the
    caller treats it as "cannot conclude" so a real success is never failed on a flaky head."""
    try:
        _s3.head_object(Bucket=SKILLS_S3_BUCKET, Key=key)
        return True
    except Exception as e:  # noqa: BLE001 - classify by error code, default to "unknown"
        code = ""
        resp = getattr(e, "response", None)
        if isinstance(resp, dict):
            code = str(resp.get("Error", {}).get("Code", ""))
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        return None


def _missing_declared_deliverable(raw_result) -> "str | None":
    """Detect a FALSE SUCCESS: a node that completed while reporting deliverable URI(s) in OUR
    bucket that were never actually written.

    Run 627fb96e: oil-paint image nodes returned ``{"uri": "s3://.../meridian_shotNN_oil.png"}``
    for files that do not exist. ``_result_error_text`` sees no error signal (the payload is
    success-shaped), so the run laundered SUCCEEDED with 6 of 8 images missing. This closes that
    gap by verifying the claim.

    Conservative by design — returns a reason ONLY when at least one our-bucket deliverable is
    claimed AND EVERY claimed our-bucket object is a CONFIRMED 404. A single existing deliverable,
    or any object whose existence cannot be determined (permissions / transient), yields None, so
    a genuine success is never marked failed."""
    if not SKILLS_S3_BUCKET:
        return None
    keys = _declared_our_bucket_keys(raw_result)
    if not keys:
        return None
    missing = []
    for k in keys:
        exists = _object_exists(k)
        if exists is True:
            return None            # a claimed deliverable really exists -> not a false success
        if exists is None:
            return None            # cannot verify -> do not fail a possibly-real success
        missing.append(k)          # confirmed absent
    return ("declared deliverable(s) not found in the output bucket (node reported success but "
            "wrote nothing): " + ", ".join(missing[:3]))


def _should_dispatch_plan_record(event_name: str, status: str) -> bool:
    """Whether a PLAN-row stream record should kick off node dispatch.

    A plan row is keyed ``(session_id, node_id="PLAN#<plan_id>")`` and arrives as ``INSERT``.
    Legacy plans shared the fixed key ``"PLAN"``, so only the FIRST plan in a conversation
    arrived as ``INSERT`` — every later plan in that session OVERWROTE the row and arrived as
    ``MODIFY``.

    Reacting to ``INSERT`` alone meant the 2nd+ plan in a chat was accepted, wrote
    its node rows, told the user "dispatched", and then never ran: every node sat
    PENDING forever, the chainer no-op'd in ~2ms, and nothing logged an error.

    ``status == "pending"`` is what makes ``MODIFY`` safe: the chainer only ever
    writes NODE rows (running/completed/failed) and never writes a pending PLAN,
    so its own writes cannot re-trigger dispatch. Re-entry would be harmless
    anyway — ``_process_plan`` only dispatches nodes that are still pending with
    satisfied dependencies.
    """
    return event_name == "INSERT" or status == "pending"


def _is_watchdog_event(event) -> bool:
    """The minute EventBridge schedule (PlanChainerWatchdogSchedule), not a stream batch."""
    return isinstance(event, dict) and (
        event.get("watchdog") is True or event.get("source") == "aws.events")


def handler(event, context):
    """Process DDB Stream events from the Plan State table (or the minute watchdog sweep)."""
    # Fresh session-conflict backoff budget for THIS invocation — a warm container
    # must not inherit one an earlier invocation already spent (it would then never
    # retry a 409 again and silently lose session propagation).
    global _session_conflict_sleep_left
    _session_conflict_sleep_left = _SESSION_CONFLICT_SLEEP_BUDGET_S

    if _is_watchdog_event(event):
        return _watchdog_sweep(context)

    sessions_to_check = set()

    for record in event.get("Records", []):
        if record.get("eventName") not in ("INSERT", "MODIFY"):
            continue

        new_image = record.get("dynamodb", {}).get("NewImage")
        if not new_image:
            continue

        # Unmarshal DDB types
        node_id = new_image.get("node_id", {}).get("S", "")
        status = new_image.get("status", {}).get("S", "")
        session_id = new_image.get("session_id", {}).get("S", "")

        # Session_Hit_Store items share the plan-state table but live in a dedicated
        # `HITS#<session_id>` partition (agent/src/project_search/session_hits.py's
        # DynamoSessionHitStore, design Data Models "Session_Hit_Store"). They are Hit payloads,
        # not plan nodes, so skip them explicitly here — a Hit item must never reach
        # _process_plan or be mistaken for a plan node.
        if session_id.startswith("HITS#"):
            continue
        # The watchdog's own work list (_WATCH_PARTITION) shares the table too.
        if session_id.startswith("WATCH#"):
            continue

        # React to:
        # 1. Terminal node transitions (completed/failed) → chain next nodes
        # 2. results_ready transitions → dispatch results-fetch agent
        # 3. PLAN record written (new plan created) → dispatch initial READY nodes
        if _split_sk(node_id)[1] == "PLAN":
            if _should_dispatch_plan_record(record.get("eventName", ""), status):
                sessions_to_check.add(session_id)
            else:
                logger.debug(
                    "Plan record for session %s ignored (event=%s status=%s)",
                    session_id, record.get("eventName"), status,
                )
            continue
        if status not in ACTIONABLE_STATUSES:
            continue

        # A node that just COMPLETED records what it made on its job-registry row (`outputs`), whoever
        # wrote the completion (a node turn, the fast path, a results fetch). projects.lineageEdges only.
        # Best effort: never blocks chaining.
        if LINEAGE_EDGES and status == "completed" and _became_status(record, "completed"):
            try:
                plan_of, node_of = _split_sk(node_id)
                _record_node_outputs(
                    plan_of or new_image.get("plan_id", {}).get("S", ""), node_of,
                    new_image.get("result_s3_key", {}).get("S", ""))
            except Exception:  # noqa: BLE001 - outputs are lineage/display aids, chaining must go on
                logger.warning("Failed to record outputs for node %s", node_id, exc_info=True)

        sessions_to_check.add(session_id)

    # Process each affected plan
    for session_id in sessions_to_check:
        try:
            _process_plan(session_id)
        except Exception:
            logger.exception("Failed to process plan for session %s", session_id)

def _became_status(record: dict, status: str) -> bool:
    """True when this stream record moved the row INTO ``status`` (an INSERT, or a MODIFY from another)."""
    old = (record.get("dynamodb", {}).get("OldImage") or {}).get("status", {}).get("S", "")
    return old != status


# The media (and data) a node made. Extension -> kind; anything else is not an output.
_MEDIA_KINDS = {
    "mp4": "video", "mov": "video", "webm": "video", "mkv": "video", "m4v": "video",
    "png": "image", "jpg": "image", "jpeg": "image", "webp": "image", "gif": "image",
    "wav": "audio", "mp3": "audio", "m4a": "audio", "aac": "audio", "flac": "audio", "ogg": "audio",
}
_NODE_OUTPUTS_MAX = 24
_NODE_INPUT_REFS_MAX = 200
_NODE_RESULT_READ_BYTES = 512 * 1024


def _media_kind(key: str) -> "str | None":
    tail = key.rsplit("/", 1)[-1]
    if "." not in tail:
        return None
    return _MEDIA_KINDS.get(tail.rsplit(".", 1)[-1].lower())


def _is_session_output_key(key: str) -> bool:
    """A file a session MADE: under a session output root (either layout) or the shared external namespace
    (fal results). A source the node only read (a project asset, an upload under ``input/``) is not one."""
    return "/output/" in f"/{key}" or key.startswith("artifacts/external/")


def _read_node_result_text(result_s3_key: str) -> str:
    if not (SKILLS_S3_BUCKET and result_s3_key):
        return ""
    obj = _s3.get_object(Bucket=SKILLS_S3_BUCKET, Key=result_s3_key,
                         Range=f"bytes=0-{_NODE_RESULT_READ_BYTES - 1}")
    return obj["Body"].read().decode("utf-8", errors="replace")


def node_outputs_from_result(result_text: str, upstream_keys: "set | frozenset" = frozenset()) -> list:
    """The media files a node's result names as made by it: ``[{uri, key, kind}]``, in the order named.

    Generic over node types: a gateway job's stored result, a Task-Agent turn's prose (an ffmpeg assemble run
    inside the node registers no job -- its output exists only here), a tool fast-path's JSON. Inputs are left
    out: files outside a session output root, and files an UPSTREAM node recorded or the node was handed (a
    node's prose routinely names the frames it was given)."""
    out = []
    for key in _declared_our_bucket_keys(result_text):
        kind = _media_kind(key)
        if not kind or not _is_session_output_key(key) or key in upstream_keys:
            continue
        out.append({"uri": f"s3://{SKILLS_S3_BUCKET}/{key}", "key": key, "kind": kind})
        if len(out) >= _NODE_OUTPUTS_MAX:
            break
    return out


def _record_node_outputs(plan_id: str, node_id: str, result_s3_key: str) -> None:
    """Stamp ``outputs`` on a completed node's job-registry row (``<plan_id>#<node_id>``), once.

    The row is what ``GET /jobs`` and the jobs stream project, and what the Generated_Media registrar reads to
    give a node's in-turn outputs (an ffmpeg assemble, a code-interpreter restyle) their lineage. Written even
    when empty (``[]``) so a replay does no more S3 reads, and guarded by ``attribute_not_exists`` so it is
    idempotent and never overwrites."""
    if not (plan_id and node_id and node_id != "PLAN"):
        return
    job_key = {"jobId": f"{plan_id}#{node_id}"}
    row = _job_table.get_item(Key=job_key).get("Item")
    if not row or row.get("outputs") is not None:
        return
    upstream: set = set(str(k).split("/", 3)[-1] if str(k).startswith("s3://") else str(k)
                        for k in (row.get("inputRefs") or []))
    for dep in row.get("dependsOn") or []:
        dep_row = _job_table.get_item(Key={"jobId": f"{plan_id}#{dep}"}).get("Item") or {}
        for o in dep_row.get("outputs") or []:
            if isinstance(o, dict) and o.get("key"):
                upstream.add(str(o["key"]))
    outputs = node_outputs_from_result(_read_node_result_text(result_s3_key), upstream)
    try:
        _job_table.update_item(
            Key=job_key,
            UpdateExpression="SET outputs = :o",
            ConditionExpression="attribute_exists(jobId) AND attribute_not_exists(outputs)",
            ExpressionAttributeValues={":o": outputs},
        )
        logger.info("Plan %s: node %s recorded %d output(s)", plan_id, node_id, len(outputs))
    except _job_table.meta.client.exceptions.ConditionalCheckFailedException:
        pass


# Both stop at a backslash: the injected preview is JSON text, where a newline is the two characters `\n` (staging
# plan 0cc48c4d handed `.../cut1_20_23.mp4\n\n**clip2:**` as one "uri").
_HANDED_REF_RE = re.compile(r"s3://[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]/[^\s\"'<>)\]}`,\\]+")
_HANDED_URL_RE = re.compile(r"https://[^\s\"'<>)\]}`,\\]+")
_S3_VHOST_URL_RE = re.compile(r"^https://([a-z0-9][a-z0-9.\-]{1,61}[a-z0-9])\.s3(?:[.-][a-z0-9-]+)*\.amazonaws\.com/(.+)$")
# The harness's own bookkeeping (a node's stored text, a plan manifest) is never a media input.
_NOT_INPUT_PREFIXES = ("plans/", "jobs/", "tools/")


def _ref_of_url(url: str) -> "str | None":
    """``s3://bucket/key`` of OUR CDN URL or a presigned S3 URL (query dropped), else None."""
    from urllib.parse import unquote

    url = url.split("?", 1)[0].split("#", 1)[0].rstrip(".;:!?")
    if ARTIFACTS_DOMAIN and SKILLS_S3_BUCKET and url.startswith(f"https://{ARTIFACTS_DOMAIN}/"):
        return f"s3://{SKILLS_S3_BUCKET}/{unquote(url[len(ARTIFACTS_DOMAIN) + 9:])}"
    m = _S3_VHOST_URL_RE.match(url)
    return f"s3://{m.group(1)}/{unquote(m.group(2))}" if m else None


def handed_input_refs(*texts) -> list:
    """The media locations the chainer HANDED a node (its prompt with the dependency + prior-phase injection blocks,
    its fast-path arguments) as ``s3://`` uris -- ``s3://`` refs, our CDN URLs and presigned S3 URLs alike -- in
    order, unique, bounded: the node's declared inputs for lineage. Prefixes and harness bookkeeping are skipped."""
    out: list = []
    for t in texts:
        if not isinstance(t, str):
            t = json.dumps(t, default=str) if t is not None else ""
        found = [m.group(0).rstrip(".;:!?") for m in _HANDED_REF_RE.finditer(t)]
        found += [r for r in (_ref_of_url(m.group(0)) for m in _HANDED_URL_RE.finditer(t)) if r]
        for ref in found:
            key = ref[5:].split("/", 1)[-1]
            if ref.endswith("/") or ref in out or key.startswith(_NOT_INPUT_PREFIXES):
                continue
            out.append(ref)
            if len(out) >= _NODE_INPUT_REFS_MAX:
                return out
    return out


def _record_node_inputs(plan_id: str, node_id: str, refs: list) -> None:
    """Stamp ``inputRefs`` (what the node was handed) on its job-registry row. Overwritten by a retry, which is
    handed the same inputs. Best effort; projects.lineageEdges only."""
    if not (LINEAGE_EDGES and plan_id and node_id and refs):
        return
    try:
        _job_table.update_item(
            Key={"jobId": f"{plan_id}#{node_id}"},
            UpdateExpression="SET inputRefs = :r",
            ConditionExpression="attribute_exists(jobId)",
            ExpressionAttributeValues={":r": list(refs)[:_NODE_INPUT_REFS_MAX]},
        )
    except Exception:  # noqa: BLE001 - lineage never blocks a dispatch
        logger.debug("Plan %s: node %s input refs not recorded", plan_id, node_id, exc_info=True)


def _plan_sk(plan_id: str) -> str:
    """Sort key of a plan's own row.

    Was the literal "PLAN", which is a FIXED sort key -- so a second plan in the same session
    overwrote the first's row, nodes_json included, and the first plan became uncompletable.
    Qualifying it by plan_id lets a session hold more than one.
    """
    return f"PLAN#{plan_id}" if plan_id else "PLAN"


def _node_sk(plan_id: str, node_id: str) -> str:
    """Sort key of a node row, qualified by its plan.

    Node ids are positional (t1, t2, t3), so they repeat across plans in a session and
    collided on (session_id, node_id).
    """
    return f"{plan_id}#{node_id}" if plan_id else node_id


# plan_id -> its PLAN row's sort key, recorded as each plan is advanced. A plan's rows all use
# ONE key format, fixed when its PLAN row was written: "PLAN#<plan_id>" plans address nodes as
# "<plan_id>#<node_id>"; plans written under the legacy literal "PLAN" keep bare node ids until
# they finish, so a deploy never splits one plan's node across two rows. Every write below is
# reached from _advance_plan, which records the format first. Unknown plan → legacy.
_PLAN_ROW_KEYS: dict = {}
_PLAN_ROW_KEYS_MAX = 5000


def _remember_plan_key(plan_id: str, plan_row_sk: str) -> None:
    if len(_PLAN_ROW_KEYS) >= _PLAN_ROW_KEYS_MAX:
        _PLAN_ROW_KEYS.clear()  # warm-container bound; re-learned on the next pass
    _PLAN_ROW_KEYS[plan_id] = plan_row_sk


def _plan_is_qualified(plan_id: str) -> bool:
    return bool(plan_id) and str(_PLAN_ROW_KEYS.get(plan_id, "")).startswith("PLAN#")


def _plan_key(session_id: str, plan_id: str) -> dict:
    """DynamoDB key of a plan's own row, in the format that plan was written with."""
    return {"session_id": session_id, "node_id": _PLAN_ROW_KEYS.get(plan_id) or "PLAN"}


def _node_key(session_id: str, plan_id: str, node_id: str) -> dict:
    """DynamoDB key of a node row, in its plan's key format."""
    sk = _node_sk(plan_id, node_id) if _plan_is_qualified(plan_id) else node_id
    return {"session_id": session_id, "node_id": sk}


def _identity_set(plan_id: str, node_id: str, names: dict, values: dict) -> str:
    """SET fragment stamping ``plan_id`` and the bare ``node`` id on an updated node row.

    An UpdateItem can create the row, so each node write carries its identity rather than
    relying on the pending row having been written first.
    """
    names["#node"] = "node"
    values[":pid"] = plan_id
    values[":node"] = node_id
    return ", plan_id = :pid, #node = :node"


def _session_rows(session_id: str) -> list:
    """Every plan-state row of a session (paginated: a dropped page reads as pending forever)."""
    items = []
    kwargs = {"KeyConditionExpression": Key("session_id").eq(session_id)}
    while True:
        response = _plan_table.query(**kwargs)
        items.extend(response.get("Items", []))
        last = response.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last


def _split_sk(sort_key: str) -> tuple:
    """(plan_id, node_id) from either key format.

    Legacy rows carry a bare node_id and no plan prefix; they are returned with plan_id "" so
    a caller can fall back to the row's plan_id attribute. Reading both formats is what lets
    this deploy without stranding plans that are mid-flight.
    """
    text = str(sort_key or "")
    if text == "PLAN":
        return "", "PLAN"
    if text.startswith("PLAN#"):
        return text[len("PLAN#"):], "PLAN"
    if "#" in text:
        plan, node = text.split("#", 1)
        return plan, node
    return "", text


def _ttl_epoch() -> int:
    """Absolute DynamoDB TTL = now + the sliding window (D-08).

    One definition for every sliding-window write so the 24h window can never diverge across
    the paths that re-stamp it. ``ttl`` is a DynamoDB RESERVED WORD, so callers that SET it in an
    UpdateExpression must use ``ExpressionAttributeNames={"#ttl": "ttl"}`` (mirrors the existing
    job-registry terminal write).
    """
    return int(datetime.now(timezone.utc).timestamp()) + _PLAN_TTL_SECONDS


def _node_stalled_seconds(row: dict) -> float:
    """Seconds a node has sat with no progress, derived from the sliding TTL
    (``ttl`` is re-stamped to ``now + _PLAN_TTL_SECONDS`` on every genuine progress write, so
    ``ttl - _PLAN_TTL_SECONDS`` ≈ the last-progress time). Returns -1.0 when unknown, so the
    watchdog fails OPEN (never reaps a node whose age it can't establish)."""
    try:
        last_progress = int(row.get("ttl")) - _PLAN_TTL_SECONDS
    except (TypeError, ValueError):
        return -1.0
    return datetime.now(timezone.utc).timestamp() - last_progress


def _slide_plan_ttl(session_id: str, plan_id: str) -> None:
    """Re-stamp the PLAN row's sliding TTL on genuine NODE progress (D-08, constraint 2).

    Most node transitions write only a NODE row, but the PLAN row is what holds the DAG SHAPE
    (``nodes_json`` / the ``nodes_s3_key`` pointer). Without this, a long node-by-node plan with
    no fan-out would slide its node rows while its PLAN row still aged out at 24h-from-creation —
    the shape would evaporate while the nodes lived on. So whenever a node advances, bump the
    PLAN row's ttl too.

    The PLAN row's sort key is ``_plan_sk(plan_id)`` (``PLAN#<plan_id>``) on qualified rows and
    the bare ``"PLAN"`` on legacy ones; read BOTH forms exactly as the rest of this file does. A
    point read locates the real key first, so a miss is a no-op and we can never create a spurious
    PLAN row. Best-effort: any failure just leaves the PLAN row's existing ttl — sliding the ttl
    must never turn a node transition into an error, and it must NEVER fire on a no-progress poll
    (this is only ever called from a genuine node-transition write path).
    """
    seen = set()
    for sk in (_plan_sk(plan_id), "PLAN"):
        if sk in seen:
            continue
        seen.add(sk)
        try:
            found = _plan_table.get_item(
                Key={"session_id": session_id, "node_id": sk}).get("Item")
        except Exception:
            # A table double without get_item (or a transient read error) → treat as "not here"
            # and try the other form; never raise into the caller's transition.
            continue
        if not found:
            continue
        try:
            _plan_table.update_item(
                Key={"session_id": session_id, "node_id": sk},
                UpdateExpression="SET #ttl = :ttl",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={":ttl": _ttl_epoch()},
            )
        except Exception:
            logger.debug("D-08: could not slide PLAN ttl for plan %s (sk=%s)", plan_id, sk)
        return


def _reap_stalled_nodes(session_id, plan_id, actor_id, nodes_json, node_statuses, node_states) -> list:
    """Force-fail RUNNING nodes with no progress for _PLAN_NODE_STALL_SECONDS. Returns their ids.

    A node stuck RUNNING with no progress — a hung tool/code_interpreter that never wrote a
    terminal status (session 20f7315e), or a runtime that died with its microVM (plan 81d9198e) —
    is invisible to the settle logic (which only reaps FAILED nodes), so the plan hangs RUNNING
    until the 24h TTL sweep and its review gate never fires. Failing it lets the retry pass re-run
    it (a stall is retry-eligible) or the skip-dependents pass settle the plan.

    Two kinds of RUNNING node are legitimately quiet and are NOT reaped:
      * one whose row carries a job_id for a registered, non-terminal job, and
      * one that registered an async job MID-TURN (fal/SFN, found by run+node in the job
        registry): if its turn died after the submit, the changefeed bridge still maps that job
        back to the node by planNodeId when it finishes, so reaping it would throw away paid work.
    The fail is CONDITIONAL on the row still being RUNNING, so a node that reports in the same
    instant is never overwritten. Mutates ``node_statuses``.
    """
    reaped = []
    for node_def in nodes_json:
        nid = node_def["id"]
        if str(node_statuses.get(nid, "")).lower() != "running":
            continue
        row = node_states.get(nid, {})
        stalled = _node_stalled_seconds(row)
        if stalled < _PLAN_NODE_STALL_SECONDS:
            continue
        job_id = row.get("job_id")
        if job_id and _has_pending_gateway_job({nid: row}):
            continue  # a real gateway job (SFN or fal/queue) is still in flight — not a hang
        if _node_has_inflight_registered_job(actor_id, plan_id, nid, stalled):
            logger.info("Plan %s node %s: quiet for %.0fs but waiting on a registered async job — "
                        "not stalled", plan_id, nid, stalled)
            continue
        logger.warning(
            "Plan %s node %s: RUNNING with no progress for %.0fs (> %ds) — force-failing so the "
            "plan can settle", plan_id, nid, stalled, _PLAN_NODE_STALL_SECONDS)
        if _mark_node_failed(session_id, plan_id, nid,
                             f"node stalled: no progress for {stalled:.0f}s (watchdog)",
                             only_if_running=True):
            node_statuses[nid] = "failed"
            reaped.append(nid)
    return reaped


def _node_has_inflight_registered_job(actor_id, plan_id, node_id, stalled_seconds) -> bool:
    """True if a job registered by this plan node during its turn is still non-terminal.

    Queries the job registry's (actorId, updatedAt) GSI for rows touched since the node's last
    progress (the job's row is written at submit and bumped by every poll), filtered to this
    run + node. On a read error it answers True (do not reap): a node is only force-failed when
    we positively know nothing is in flight for it — the next sweep, a minute later, re-asks.
    """
    if not actor_id or not plan_id or not node_id:
        return False
    since = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() - max(0.0, float(stalled_seconds)) - 60,
        tz=timezone.utc).isoformat()
    kwargs = {
        "IndexName": JOB_REGISTRY_ACTOR_INDEX,
        "KeyConditionExpression": Key("actorId").eq(actor_id) & Key("updatedAt").gte(since),
    }
    try:
        while True:
            resp = _job_table.query(**kwargs)
            for item in resp.get("Items", []):
                if item.get("runId") != plan_id:
                    continue
                if node_id not in (item.get("planNodeId"), item.get("nodeId")):
                    continue
                if item.get("gatewayId") == "__coordinator__":
                    continue  # the node's OWN coordinator row, not a job it submitted
                if str(item.get("status", "")).strip() not in TERMINAL_STATUSES:
                    return True
            last = resp.get("LastEvaluatedKey")
            if not last:
                return False
            kwargs["ExclusiveStartKey"] = last
    except Exception:
        logger.warning("watchdog: registered-job lookup failed for %s#%s; not reaping it",
                       plan_id, node_id, exc_info=True)
        return True


# (session_id, plan_id) pairs this warm container has already put on the watch list, so a
# plan's every pass does not re-write its marker (each write is also a stream record).
_WATCHED: set = set()
_WATCHED_MAX = 5000
# Leave this much of the Lambda's time unused by the sweep (it shares the 60s timeout).
_SWEEP_RESERVE_MS = 10_000


def _watch_plan(session_id: str, plan_id: str) -> None:
    """Put an in-flight plan on the minute watchdog's list (idempotent, best-effort)."""
    if not session_id or not plan_id or (session_id, plan_id) in _WATCHED:
        return
    try:
        _plan_table.put_item(
            Item={
                "session_id": _WATCH_PARTITION,
                "node_id": f"{session_id}|{plan_id}",
                "watched_session": session_id,
                "plan_id": plan_id,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "ttl": _ttl_epoch(),
            },
            ConditionExpression="attribute_not_exists(node_id)",
        )
    except Exception as exc:
        if not _is_conditional_check_failure(exc):
            logger.warning("watchdog: could not watch plan %s: %s", plan_id, exc)
            return
    if len(_WATCHED) >= _WATCHED_MAX:
        _WATCHED.clear()
    _WATCHED.add((session_id, plan_id))


def _unwatch_plan(session_id: str, plan_id: str) -> None:
    """Take a settled plan off the watchdog's list (best-effort; the TTL is the backstop)."""
    _WATCHED.discard((session_id, plan_id))
    try:
        _plan_table.delete_item(
            Key={"session_id": _WATCH_PARTITION, "node_id": f"{session_id}|{plan_id}"})
    except Exception:
        logger.debug("watchdog: could not unwatch plan %s", plan_id, exc_info=True)


def _watched_plans() -> list:
    """Every (session_id, plan_id) on the watch list (one paginated single-partition Query)."""
    out = []
    kwargs = {"KeyConditionExpression": Key("session_id").eq(_WATCH_PARTITION)}
    while True:
        resp = _plan_table.query(**kwargs)
        for item in resp.get("Items", []):
            sid = item.get("watched_session") or str(item.get("node_id", "")).split("|", 1)[0]
            pid = item.get("plan_id") or str(item.get("node_id", "")).split("|", 1)[-1]
            if sid and pid:
                out.append((sid, pid))
        last = resp.get("LastEvaluatedKey")
        if not last:
            return out
        kwargs["ExclusiveStartKey"] = last


def _watchdog_sweep(context=None) -> dict:
    """The minute sweep: reap stalled nodes of every in-flight plan.

    Deliberately ONLY the watchdog — it never dispatches, fetches results or settles. A reaped
    node's failed write is a stream record, and THAT invocation runs the ordinary pass (retry,
    skip-dependents, settle), so the sweep can never race the stream into double work. A plan
    found settled (or gone) is dropped from the list.
    """
    stats = {"plans": 0, "reaped": 0, "unwatched": 0, "errors": 0}
    try:
        watched = _watched_plans()
    except Exception:
        logger.exception("watchdog: could not read the watch list")
        stats["errors"] += 1
        return stats
    by_session: dict = {}
    for sid, pid in watched:
        by_session.setdefault(sid, []).append(pid)
    for session_id, plan_ids in by_session.items():
        if context is not None and hasattr(context, "get_remaining_time_in_millis"):
            if context.get_remaining_time_in_millis() < _SWEEP_RESERVE_MS:
                logger.warning("watchdog: time budget spent; %d session(s) left for the next sweep",
                               len(by_session) - stats["plans"])
                break
        try:
            items = _session_rows(session_id)
            records = {}
            for item in items:
                row_plan, row_node = _split_sk(item.get("node_id"))
                if row_node == "PLAN":
                    records[item.get("plan_id") or row_plan] = item
            for plan_id in plan_ids:
                stats["plans"] += 1
                record = records.get(plan_id)
                if not record or str(record.get("status", "")).lower() in ("completed", "failed"):
                    _unwatch_plan(session_id, plan_id)
                    stats["unwatched"] += 1
                    continue
                nodes_json = _load_nodes_json(record)
                if not nodes_json:
                    continue
                node_states = {
                    nid: row for nid, row in _plan_node_rows(record, items).items()
                    if not row.get("plan_id") or row.get("plan_id") == plan_id
                }
                node_statuses = {
                    n["id"]: node_states.get(n["id"], {}).get("status", "pending") for n in nodes_json
                }
                reaped = _reap_stalled_nodes(session_id, plan_id, record.get("actor_id", ""),
                                             nodes_json, node_statuses, node_states)
                stats["reaped"] += len(reaped)
        except Exception:
            logger.exception("watchdog: sweep failed for session %s", session_id)
            stats["errors"] += 1
    logger.info("watchdog sweep: %s", stats)
    return stats


def _process_plan(session_id: str):
    """Read plan state and advance EVERY plan in this session.

    Was written for one plan per session, which the key schema silently enforced. Now that a
    session can hold several, each is advanced independently -- otherwise one plan's stream
    event would evaluate completion against another plan's node list.
    """
    items = _session_rows(session_id)

    plan_records = [i for i in items if _split_sk(i.get("node_id"))[1] == "PLAN"]
    if not plan_records:
        logger.warning("No PLAN record for session %s", session_id)
        return
    for plan_record in plan_records:
        _advance_plan(session_id, plan_record, items)


def _plan_node_rows(plan_record: dict, items: list) -> dict:
    """{node_id: row} of the session rows that belong to ``plan_record``'s plan.

    A "PLAN#<plan_id>" plan owns exactly its "<plan_id>#<node>" rows. A legacy "PLAN" plan owns
    the bare rows that are unstamped or stamped with its id (plus any "<plan_id>#" row, which
    wins over the bare one). Also records the plan's key format (_remember_plan_key).
    """
    plan_sk = plan_record.get("node_id") or "PLAN"
    _this_plan = plan_record.get("plan_id", "") or _split_sk(plan_sk)[0]
    qualified = str(plan_sk).startswith("PLAN#")
    if _this_plan:
        _remember_plan_key(_this_plan, plan_sk)
    all_node_rows = {}
    for item in items:
        row_plan, row_node = _split_sk(item.get("node_id"))
        if row_node == "PLAN":
            continue
        if row_plan:
            if row_plan == _this_plan:
                all_node_rows[row_node] = item  # this plan's own row always wins
            continue
        if qualified:
            continue  # a bare row belongs to some legacy plan, never to a qualified one
        # A legacy row carries no owner at all; it is treated as this plan's so runs that
        # predate the stamp keep advancing.
        owner = item.get("plan_id", "")
        if owner and owner != _this_plan:
            continue
        all_node_rows.setdefault(row_node, item)
    return all_node_rows


def _advance_plan(session_id: str, plan_record: dict, items: list):
    """Dispatch ready nodes, or settle, for ONE plan.

    A "PLAN#<plan_id>" plan owns exactly its "<plan_id>#<node>" rows. A legacy "PLAN" plan owns
    the bare rows that are unstamped or stamped with its id (plus any "<plan_id>#" row, which
    wins over the bare one), so plans in flight when the qualified keys shipped still finish.
    """
    if not plan_record:
        logger.warning("No PLAN record for session %s", session_id)
        return
    plan_sk = plan_record.get("node_id") or "PLAN"
    all_node_rows = _plan_node_rows(plan_record, items)

    # A settled plan is done: with one PLAN row per plan, every later event in the session
    # would otherwise re-settle it (re-publish its result, re-notify).
    if str(plan_record.get("status", "")).lower() in ("completed", "failed"):
        if (_this_plan_id := plan_record.get("plan_id", "")) and \
                (session_id, _this_plan_id) in _WATCHED:
            _unwatch_plan(session_id, _this_plan_id)
        return

    plan_id = plan_record.get("plan_id", "")
    actor_id = plan_record.get("actor_id", "")
    # The turn that asked for this plan. Persisted on the PLAN row because nodes
    # are dispatched here, asynchronously, long after that invocation ended.
    turn_id = plan_record.get("turn_id", "")
    # Signed delegation grant (option E), minted by the orchestrator while it
    # still held the actor and persisted beside actor_id/turn_id. Presented to
    # the node's Task Agent so it can forward it to the harness gateway, where
    # the interceptor derives the actor from the VERIFIED grant — not from the
    # untrusted actor_id that also rides the payload. Empty for plans that need
    # no user_jwt-gated tools (they were never granted one).
    delegation_grant = plan_record.get("delegation_grant", "")
    # Orchestrator's picked model, persisted by plan_and_execute. Forwarded onto each node's
    # dispatch so plan nodes run on the model the user selected (empty → node uses its default).
    model_id = plan_record.get("model_id", "")
    # The graph-enabled Project the plan's turn ran in (knowledge-graph tools). An id, never a token: each
    # node's Task Agent mints its own Project_Token from the delegation grant above.
    project_scope_id = plan_record.get("project_scope_id", "") or ""
    # The plan's producing project, stamped on the PLAN row itself by plan_and_execute. Seeded into
    # the cache _plan_producing_project reads, because the job-registry plan row that used to be the
    # only source is written ~3 s AFTER these rows fire this lambda: staging plan 73d92d46 dispatched
    # both sg5 nodes at 23:33:06.9, the registry row landed at 23:33:10.1, so the nodes ran with no
    # project, got `project_id=none` sandbox credentials, and every write to the
    # projects/<pid>/sessions/<sid>/output/ prefix the orchestrator named was AccessDenied.
    _seed_plan_producing_project(plan_id, plan_record.get("producing_project_id"))

    # Skip records that aren't from the async plan dispatch system.
    # The Planning/Design agents use the same table with node_id="PLAN"
    # but don't have a plan_id or nodes_json field.
    if not plan_id:
        return

    # Parse the DAG definition from the PLAN record (inline, or offloaded to S3 for large plans)
    nodes_json = _load_nodes_json(plan_record)

    if not nodes_json:
        return

    # Keep only rows that belong to THIS plan. A node row stamped with a different
    # plan_id is a leftover from an earlier plan in the same session -- the table is
    # keyed (session_id, node_id), so ids repeat across plans and a stale terminal row
    # would otherwise be counted as this plan's progress. Rows with no plan_id predate
    # the stamp and are treated as belonging, so existing runs keep working.
    node_states = {
        nid: row for nid, row in all_node_rows.items()
        if not row.get("plan_id") or row.get("plan_id") == plan_id
    }
    foreign = len(all_node_rows) - len(node_states)
    if foreign:
        logger.info("Ignoring %d node row(s) from another plan in session %s",
                    foreign, session_id)

    # Build node status map
    node_statuses = {}
    for node_def in nodes_json:
        nid = node_def["id"]
        state_row = node_states.get(nid, {})
        node_statuses[nid] = state_row.get("status", "pending")

    # WATCHDOG (session 20f7315e): see _reap_stalled_nodes. Runs on every pass AND from the
    # minute sweep (_watchdog_sweep), so a dead node is reaped even when no other event arrives.
    _reap_stalled_nodes(session_id, plan_id, actor_id, nodes_json, node_statuses, node_states)

    # Phase 2: expand any READY map node into per-item child task nodes (feature-gated).
    # This rewrites nodes_json IN PLACE (+ persists it on the PLAN row), so every check below
    # — readiness, skip-propagation, all-terminal, and future passes — treats the children as
    # ordinary nodes. No union-model refactor needed: the graph literally grows the children.
    if _fanout_enabled() and any(n.get("kind") == "map" for n in nodes_json):
        if _expand_ready_map_nodes(session_id, plan_id, actor_id, nodes_json, node_statuses, node_states, plan_sk):
            logger.info("Plan %s: map expansion grew the graph to %d nodes", plan_id, len(nodes_json))

    # Tier-1 error-aware self-correction (P2): before judging the plan terminal, re-dispatch
    # any failed node that is retry-eligible WITH its error in context, so a correctable
    # failure (a wrong/empty parameter) fixes itself instead of failing the whole plan.
    # Bounded by MAX_NODE_RETRIES + error-signature stop (see _should_retry_node), so it
    # cannot loop. A retried node becomes 'running' this pass and is not counted terminal.
    for node_def in nodes_json:
        nid = node_def["id"]
        # Control nodes (map/gather) are not task-retryable: a failed expansion or fan-in is
        # terminal, not a wrong-parameter task to re-dispatch. Re-dispatching a map as a task
        # ran its empty prompt and left it spuriously "running".
        if node_def.get("kind") in ("map", "gather"):
            continue
        if node_statuses.get(nid, "").lower() == "failed" and _should_retry_node(node_states.get(nid, {})):
            _retry_failed_node(session_id, plan_id, actor_id, node_def, nodes_json, node_states,
                               node_states.get(nid, {}), turn_id=turn_id, delegation_grant=delegation_grant,
                               model_id=model_id, project_scope_id=project_scope_id)
            node_statuses[nid] = "running"

    # Skip the (transitive) dependents of a terminally-failed node (Phase 1.5). A node
    # still "failed" after the retry pass above is terminal — retry flips eligible nodes
    # to "running". Anything still pending that depends on a failed (or now-skipped) node
    # can NEVER become READY (a dep must be "succeeded"/"completed"), so without this the
    # plan never reaches all-terminal and hangs RUNNING until TTL (observed on plan
    # plan-awsmcp001: t1_research failed, t2_pdf_report sat pending forever). Marking those
    # dependents "skipped" lets the plan settle FAILED. Runs in THIS pass, before the
    # all-terminal check, so settle happens without needing another stream event.
    terminally_failed = {
        n["id"] for n in nodes_json
        if node_statuses.get(n["id"], "pending").lower() == "failed"
    }
    if terminally_failed:
        dead = set(terminally_failed)
        newly_skipped: set[str] = set()
        changed = True
        while changed:
            changed = False
            for node_def in nodes_json:
                nid = node_def["id"]
                if nid in dead or node_statuses.get(nid, "pending").lower() != "pending":
                    continue
                if any(dep in dead for dep in node_def.get("depends_on", [])):
                    newly_skipped.add(nid)
                    dead.add(nid)
                    node_statuses[nid] = "skipped"
                    changed = True
        for nid in newly_skipped:
            _mark_node_skipped(session_id, plan_id, nid, "upstream dependency failed")
        if newly_skipped:
            logger.info("Plan %s: skipped %d node(s) with a failed upstream dependency: %s",
                        plan_id, len(newly_skipped), sorted(newly_skipped))

    # Check if all nodes are terminal (completed/failed/skipped — NOT results_ready/processing)
    all_terminal = all(
        node_statuses.get(n["id"], "pending").lower() in
        ("succeeded", "failed", "aborted", "timed_out", "completed", "skipped")
        for n in nodes_json
    )

    if all_terminal:
        if _has_pending_gateway_job(node_states):
            logger.info(
                "Plan %s: all nodes terminal but a spawned gateway job is still running "
                "— deferring completion so the notification does not fire early (F2). The "
                "changefeed bridge will re-fire this lambda when the job settles.",
                plan_id,
            )
            return
        _mark_run_complete(session_id, plan_id, nodes_json, node_statuses, node_states=node_states)
        return

    # --- Handle nodes in "results_ready" state ---
    # These are async nodes whose gateway job completed but results haven't
    # been fetched yet. Dispatch a results-fetch agent for each.
    results_ready_nodes = []
    for node_def in nodes_json:
        nid = node_def["id"]
        if node_statuses.get(nid, "").lower() == "results_ready":
            results_ready_nodes.append(node_def)

    for node_def in results_ready_nodes:
        nid = node_def["id"]
        state_row = node_states.get(nid, {})
        _dispatch_results_fetch(session_id, plan_id, actor_id, nid, state_row,
                                delegation_grant=delegation_grant)

    # --- Handle nodes ready for execution ---
    # Only dispatch downstream when deps are truly "completed" (not just results_ready) AND
    # any gateway job the dep spawned has reached a terminal state. A node is marked
    # "completed" as soon as its Task-Agent turn returns — which for an async node is when
    # it SUBMITS the gateway job, not when that job finishes and writes its output. Gating on
    # _node_gateway_pending stops a dependent from racing ahead of the real output (the
    # 369bbe3e bug). Deadlock-safe: the gate reads the job's live status each pass, so once
    # the job is terminal the dep counts as satisfied on the next pass the bridge triggers.
    succeeded = {
        nid for nid, status in node_statuses.items()
        if status.lower() in ("succeeded", "completed")
        and not _node_gateway_pending(node_states.get(nid, {}))
        and not _node_gateway_failed(node_states.get(nid, {}))
    }

    ready_nodes = []
    for node_def in nodes_json:
        nid = node_def["id"]
        current_status = node_statuses.get(nid, "pending").lower()
        if current_status != "pending":
            continue
        deps = node_def.get("depends_on", [])
        if all(dep in succeeded for dep in deps):
            ready_nodes.append(node_def)

    if not ready_nodes and not results_ready_nodes:
        logger.info("Plan %s: no ready nodes (waiting for dependencies)", plan_id)
        return

    if ready_nodes:
        logger.info(
            "Plan %s: dispatching %d ready node(s): %s",
            plan_id,
            len(ready_nodes),
            [n["id"] for n in ready_nodes],
        )

    # Dispatch each ready node. Gathers are mechanical and local; task nodes are invoked
    # concurrently (see _dispatch_ready_nodes).
    task_nodes = []
    for node_def in ready_nodes:
        if node_def.get("kind") == "gather":
            # Fan-in: aggregate the map's children mechanically (no LLM, no re-concentration).
            _run_gather(session_id, plan_id, node_def)
        else:
            task_nodes.append(node_def)
    if task_nodes:
        _watch_plan(session_id, plan_id)
        _dispatch_ready_nodes(session_id, plan_id, actor_id, task_nodes, nodes_json, node_states,
                              turn_id=turn_id, delegation_grant=delegation_grant, model_id=model_id,
                              project_scope_id=project_scope_id)


def _dispatch_ready_nodes(session_id, plan_id, actor_id, node_defs, all_nodes, node_states, **kw):
    """Dispatch several ready nodes: prepare + claim serially, INVOKE concurrently, finish serially.

    Same per-node semantics as _dispatch_node -- the conditional pending->running claim still runs
    before any invoke (a re-delivered batch claims nothing and invokes nothing), a 409 still retries
    the same session, a read timeout is still an accepted invoke, and any other invoke failure
    still fails the node loudly so the retry pass sees it. Only the network wait overlaps: the
    claims and the DynamoDB writes stay on this thread (boto3 resources are not thread-safe), and
    the worker threads touch only the low-level bedrock-agentcore client.
    """
    preps = []
    for node_def in node_defs:
        prep = _prepare_node_dispatch(session_id, plan_id, actor_id, node_def, all_nodes,
                                      node_states, **kw)
        if prep is not None:
            preps.append(prep)
    if not preps:
        return
    if len(preps) == 1 or _DISPATCH_CONCURRENCY == 1:
        outcomes = [_invoke_prepared(p) for p in preps]
    else:
        with ThreadPoolExecutor(max_workers=min(_DISPATCH_CONCURRENCY, len(preps)),
                                thread_name_prefix="dispatch") as pool:
            outcomes = list(pool.map(_invoke_prepared, preps))
    for prep, outcome in zip(preps, outcomes):
        _finish_node_dispatch(prep, outcome)


# Key name fragments dropped when injecting a dependency's output into a
# downstream prompt. The manifest keeps the raw value regardless — this
# only trims what gets echoed into the next node's context window. Applied
# case-insensitively, substring match.
_NOISY_KEY_PATTERNS = (
    "presigned", "signature", "x-amz-", "cost", "billing", "price",
    "invocations", "wall_time", "billed_", "breakdown",
)

# String-VALUE markers for presigned URLs, checked independent of the key
# name that holds them. Some tools return presigned URLs under an
# innocuous key like "url" (e.g. speech-diarization's get_diarization_results
# shape: {"transcript_json": {"s3uri": ..., "url": "https://...?AWSAccessKeyId=..."}}),
# which _NOISY_KEY_PATTERNS' key-based check never catches. Left unfiltered,
# a single presigned URL (frequently 1500-2500+ chars of query-string noise)
# can consume the entire _MAX_INJECTED_CHARS budget on its own, silently
# crowding out the one field (an s3uri or job_id) a downstream node actually
# needs — this was the root cause of a t3_translate MaxTokensReachedException
# where the injected dependency was 100% presigned-URL noise and 0% usable
# transcript pointer.
_PRESIGNED_URL_MARKERS = ("awsaccesskeyid=", "x-amz-", "signature=")

# Hard cap on injected dependency size (chars) after filtering, as a
# backstop for tools whose raw response has no obviously-noisy keys but
# is still huge (e.g. large transcripts). Truncated with a note so the
# downstream node knows more data exists in the manifest if needed.
_MAX_INJECTED_CHARS = 3000

# Higher cap applied only when we've inlined real S3 content into the
# value (see _inline_primary_text_artifact below). A text-synthesis node
# (tools=[]) needs the ACTUAL transcript text, not just its pointer, so
# the 3000-char pointer-only cap is too small once real content is
# present. Still bounded — this is a deliberate ceiling, not "no limit" —
# so a single huge inlined artifact can't reintroduce the output-token
# budget problem fix #5 solved (MaxTokensReachedException on t3_translate).
_MAX_INJECTED_CHARS_WITH_INLINE = 50000

# Byte range fetched from S3 for inlining (Range GET, not a full download).
# Chosen to comfortably cover a multi-minute diarization transcript while
# still being a bounded, cheap read within the Lambda's per-invocation
# budget. Content beyond this is noted as truncated, not silently dropped.
# Applies to non-JSON text formats (.txt/.vtt/.srt/.md) — see
# _MAX_FULL_FETCH_BYTES_FOR_JSON for the JSON-specific path, which needs
# the whole object to parse and extract dialogue rather than a raw slice.
_MAX_INLINE_FETCH_BYTES = 40_000

# Ceiling for a full (non-range) GET of a JSON transcript, so it can be
# parsed and reduced to just the speaker-labeled dialogue (see
# _extract_dialogue_lines) instead of being truncated mid-structure. Above
# this, fall back to the bounded Range-GET-with-truncation-note path used
# for other text formats — a pathological multi-hour transcript shouldn't
# be pulled into Lambda memory in full just to extract dialogue from it.
_MAX_FULL_FETCH_BYTES_FOR_JSON = 2_000_000

# Keys, in priority order, that hold the actual per-utterance dialogue in
# a diarization/transcription tool's JSON output. Everything else in the
# object (word-level timing/probability arrays, confidence scores, audio
# metadata) is noise for a text-synthesis node and, worse, can dwarf the
# dialogue itself: observed a 61KB diarization transcript where 89% of
# the bytes were a "words" array of {id, word, start, end, probability}
# entries, and only ~7KB was the actual speaker-labeled sentences. Range-
# truncating that file cut off mid-"words"-array, handing the downstream
# node invalid JSON dominated by timing noise instead of the ~80 lines of
# dialogue it actually needed — asking it to translate that garbage into
# two languages is what blew the OUTPUT token budget on t3_translate in
# plan e9eb990d-e644-4ac8-bb7e-0abf372b87ed.
_DIALOGUE_ARRAY_KEYS = ("sentences", "segments", "utterances")

# Keys checked (in order) for the speaker label on each dialogue entry.
_SPEAKER_KEYS = ("speaker_id", "speaker", "speaker_label")

# File extensions eligible for fetch-and-inline, in priority order (first
# match wins when a dependency's output has multiple candidates, e.g.
# transcript_json AND transcript_vtt are the same transcript in two
# formats — inlining both would double the injected size for no benefit).
_INLINABLE_EXTENSIONS = (".json", ".txt", ".vtt", ".srt", ".md")

_S3URI_RE = re.compile(r"^s3://([^/]+)/(.+)$")


def _extract_dialogue_lines(parsed) -> str | None:
    """Reduce a parsed transcript JSON to just its speaker-labeled dialogue.

    Looks for the first key in _DIALOGUE_ARRAY_KEYS whose value is a list
    of dicts containing a "text" field, and formats each entry as
    ``SPEAKER_ID: text``. Returns None if the object doesn't match this
    shape (caller falls back to raw content) so this never raises or
    silently drops content it doesn't recognize.
    """
    if not isinstance(parsed, dict):
        return None
    for array_key in _DIALOGUE_ARRAY_KEYS:
        entries = parsed.get(array_key)
        if not isinstance(entries, list) or not entries:
            continue
        lines = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            text = entry.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            speaker = next(
                (entry[k] for k in _SPEAKER_KEYS if isinstance(entry.get(k), str)),
                None,
            )
            lines.append(f"{speaker}: {text}" if speaker else text)
        if lines:
            return "\n".join(lines)
    return None


def _looks_like_presigned_url(value) -> bool:
    """True if a string value is a presigned URL (query-string markers)."""
    if not isinstance(value, str) or not value.startswith(("http://", "https://")):
        return False
    lowered = value.lower()
    return any(marker in lowered for marker in _PRESIGNED_URL_MARKERS)


def _parse_s3uri(uri: str) -> tuple[str, str] | None:
    """Parse ``s3://bucket/key`` into ``(bucket, key)``, or None if malformed."""
    if not isinstance(uri, str):
        return None
    m = _S3URI_RE.match(uri)
    return (m.group(1), m.group(2)) if m else None


def _fetch_json_dialogue(bucket: str, key: str) -> str | None:
    """Fetch a JSON transcript in full and reduce it to speaker dialogue.

    Unlike _fetch_range_text, this does a full (non-range) GET because
    the object must be parsed as whole, valid JSON before it can be
    reduced — a byte-range slice of JSON risks cutting off mid-structure
    (observed: a Range GET stopped mid-"words"-array, handing the
    downstream node invalid JSON dominated by per-word timing noise
    instead of the dialogue it needed). Bounded by
    _MAX_FULL_FETCH_BYTES_FOR_JSON via a HEAD check first, so a
    pathologically large transcript falls back to the Range path instead
    of pulling megabytes into Lambda memory.

    Returns None if the object is too large, isn't valid JSON, doesn't
    match a recognized dialogue shape (see _extract_dialogue_lines), or
    the fetch fails for any reason — callers fall back to
    _fetch_range_text in all of these cases.
    """
    try:
        head = _s3.head_object(Bucket=bucket, Key=key)
        size = head.get("ContentLength", 0)
        if size > _MAX_FULL_FETCH_BYTES_FOR_JSON:
            return None
        resp = _s3.get_object(Bucket=bucket, Key=key)
        raw = resp["Body"].read()
        parsed = json.loads(raw.decode("utf-8"))
    except Exception:
        logger.exception("Failed to fetch/parse JSON transcript s3://%s/%s", bucket, key)
        return None
    return _extract_dialogue_lines(parsed)


def _fetch_range_text(bucket: str, key: str) -> str | None:
    """Fetch up to _MAX_INLINE_FETCH_BYTES of a text object from S3.

    Uses a Range GET rather than a full download to keep the fetch cheap
    and bounded regardless of the object's actual size. Returns None (not
    an exception) on any failure — access denied, missing object, etc. —
    so the caller can fall back to leaving the S3 pointer un-inlined
    rather than failing the whole dependency-injection step.

    NOTE: for JSON, this is only a fallback (see _fetch_json_dialogue) —
    a raw byte-range slice of JSON can cut off mid-structure and, for
    transcript-shaped JSON specifically, includes whatever non-dialogue
    fields (word-level timing arrays, etc.) happen to sort before the
    real dialogue in the file, which is exactly the noise
    _fetch_json_dialogue exists to strip.
    """
    try:
        resp = _s3.get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{_MAX_INLINE_FETCH_BYTES - 1}")
        raw = resp["Body"].read()
        text = raw.decode("utf-8", errors="replace")
        # ContentRange is present (e.g. "bytes 0-39999/61387") whenever the
        # server honored the Range request and the object is larger than
        # what we asked for — that's our truncation signal, independent of
        # whether len(text) happens to be short due to multi-byte trimming.
        content_range = resp.get("ContentRange", "")
        total_size = None
        if "/" in content_range:
            try:
                total_size = int(content_range.rsplit("/", 1)[-1])
            except ValueError:
                total_size = None
        if total_size is not None and total_size > _MAX_INLINE_FETCH_BYTES:
            text += f"\n... [truncated — full content is {total_size} bytes, see plan manifest / s3uri for complete content]"
        return text
    except Exception:
        logger.exception("Failed to inline text artifact s3://%s/%s", bucket, key)
        return None


def _fetch_text_artifact(bucket: str, key: str) -> str | None:
    """Fetch S3 text content for inlining, extracting dialogue for JSON.

    For .json keys, tries _fetch_json_dialogue first (parse-and-reduce to
    speaker lines) and only falls back to the raw Range-GET path
    (_fetch_range_text) if that returns None — e.g. the JSON is too
    large, isn't valid JSON, or doesn't match a known transcript shape
    (a generic JSON blob with no "sentences"/"segments"/"utterances"
    array). Non-JSON keys (.txt/.vtt/.srt/.md) go straight to the Range
    path, since VTT/SRT are already dialogue-formatted plain text with
    no comparable noise-to-signal problem.
    """
    if key.lower().endswith(".json"):
        dialogue = _fetch_json_dialogue(bucket, key)
        if dialogue is not None:
            return dialogue
    return _fetch_range_text(bucket, key)


def _find_inlinable_candidates(value, _path=()) -> list[tuple[int, tuple, dict, str]]:
    """Recursively find dicts with an ``s3uri`` key pointing at a text file.

    Returns a list of ``(priority, path, container_dict, s3uri)`` tuples,
    one per candidate found. Lower priority sorts first (see
    _INLINABLE_EXTENSIONS ordering). ``container_dict`` is the actual dict
    object in the tree (not a copy) so the caller can mutate it in place
    once a winner is chosen.
    """
    candidates: list[tuple[int, tuple, dict, str]] = []
    if isinstance(value, dict):
        for k, v in value.items():
            # Recognize a direct-file s3:// uri under ANY key (not just "s3uri"), matching how
            # _find_prefix_candidates was generalized — gateway result shapes vary.
            if isinstance(v, str) and v.lower().startswith("s3://"):
                lowered = v.lower()
                hit = False
                for i, ext in enumerate(_INLINABLE_EXTENSIONS):
                    if lowered.endswith(ext):
                        candidates.append((i, _path + (k,), value, v))
                        hit = True
                        break
                if not hit:
                    candidates.extend(_find_inlinable_candidates(v, _path + (k,)))
            else:
                candidates.extend(_find_inlinable_candidates(v, _path + (k,)))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            candidates.extend(_find_inlinable_candidates(item, _path + (i,)))
    return candidates


def _is_prefix_s3uri(uri: str) -> bool:
    """True when an ``s3uri`` points at a PREFIX (directory) rather than a file.

    Async media jobs report only the output PREFIX where their result files land
    (speech-diarization's output.s3uri = ``s3://.../speech-diarization/<jobid>/``),
    never the transcript filename. A prefix has a trailing slash or a final path
    segment with no file extension.
    """
    parsed = _parse_s3uri(uri)
    if parsed is None:
        return False
    _, key = parsed
    if key.endswith("/"):
        return True
    return "." not in key.rsplit("/", 1)[-1]


def _find_prefix_candidates(value, _path=()) -> list[tuple[tuple, dict, str]]:
    """Find dicts whose value is an s3:// PREFIX. Returns (path, container, prefix_uri).

    Recognizes the prefix under ANY key, not just the literal ``s3uri`` — gateway result shapes
    vary (speech-diarization returns ``output_s3_prefix``, others ``output.s3uri``). Keying only on
    ``s3uri`` meant a diarize pointer was never listed and its transcript never inlined, so a
    tools=[] translate node got a bare pointer and refused (translate bug, session 96198138)."""
    out: list[tuple[tuple, dict, str]] = []
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(v, str) and _is_prefix_s3uri(v):
                out.append((_path + (k,), value, v))
            else:
                out.extend(_find_prefix_candidates(v, _path + (k,)))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            out.extend(_find_prefix_candidates(item, _path + (i,)))
    return out


def _list_prefix_inlinable_keys(bucket: str, prefix: str) -> list[str]:
    """List a prefix; return inlinable text keys, best-extension first.

    Fail-safe: any error (AccessDenied on ListBucket, etc.) returns [] so inlining
    falls back to leaving the bare pointer rather than failing dependency injection.
    """
    key_prefix = prefix if prefix.endswith("/") else prefix + "/"
    try:
        resp = _s3.list_objects_v2(Bucket=bucket, Prefix=key_prefix, MaxKeys=200)
    except Exception:
        logger.exception("Failed to list prefix s3://%s/%s for inlining", bucket, key_prefix)
        return []

    def _rank(k: str) -> int:
        low = k.lower()
        for i, ext in enumerate(_INLINABLE_EXTENSIONS):
            if low.endswith(ext):
                return i
        return len(_INLINABLE_EXTENSIONS)

    keys = [o["Key"] for o in resp.get("Contents", []) if isinstance(o.get("Key"), str)]
    inlinable = [k for k in keys if _rank(k) < len(_INLINABLE_EXTENSIONS)]
    inlinable.sort(key=_rank)
    return inlinable


def _inline_primary_text_artifact(value):
    """Fetch and inline the single best text artifact referenced in *value*.

    Gateway tools that produce long-form text (transcripts, subtitles)
    return only S3 pointers + a presigned URL — never the content itself
    (see speech-diarization's get_diarization_results shape). A downstream
    text-synthesis node with tools=[] (by design, per fix #5 — it must NOT
    receive the full gateway tool catalog) has no way to read that
    pointer. Left as a bare pointer, the node correctly refuses to
    fabricate content instead of hallucinating a translation — but that
    refusal is currently rolled up as a plan-level SUCCEEDED, i.e. a
    silent no-op. Observed in plan 2f5f4c47-ac8c-4614-9071-2d9b53d39d4a's
    t3_translate node.

    Only the highest-priority candidate (per _INLINABLE_EXTENSIONS) is
    inlined — e.g. transcript_json wins over transcript_vtt, since they're
    the same content in two formats and inlining both would waste the
    injection budget on duplicate text. Mutates a deep-copied tree; the
    manifest's stored raw value is never touched.
    """
    candidates = _find_inlinable_candidates(value)
    candidates.sort(key=lambda c: c[0])
    # Direct file artifacts first: try each (best extension first) until one inlines.
    for _, _, container, s3uri in candidates:
        parsed = _parse_s3uri(s3uri)
        if parsed is None:
            continue
        text = _fetch_text_artifact(*parsed)
        if text is not None:
            container["inline_content"] = text
            return value, True

    # No direct-file artifact — an async media job (diarization/transcription) reports only
    # the output PREFIX. List it and inline the best transcript file. This is the t3_translate
    # silent no-op: the diarization node's output was an s3uri PREFIX with no filename, so
    # nothing was ever inlined and the text-synthesis node (tools=[]) got an un-fetchable
    # pointer and correctly refused to fabricate a translation. Prefer a JSON that yields real
    # speaker dialogue over an arbitrary JSON that merely sorts first (config/words vs the
    # transcript) — no filename matching, so it generalizes across tools.
    for _, container, prefix_uri in _find_prefix_candidates(value):
        parsed = _parse_s3uri(prefix_uri)
        if parsed is None:
            continue
        bucket, prefix = parsed
        keys = _list_prefix_inlinable_keys(bucket, prefix)
        for key in keys:
            if key.lower().endswith(".json"):
                dialogue = _fetch_json_dialogue(bucket, key)
                if dialogue:
                    container["inline_content"] = dialogue
                    container["inline_source_key"] = key
                    return value, True
        for key in keys:
            text = _fetch_text_artifact(bucket, key)
            if text:
                container["inline_content"] = text
                container["inline_source_key"] = key
                return value, True

    return value, False


def _filter_for_injection(value):
    """Strip noisy keys/values from a manifest value before prompt injection.

    Recursively drops dict keys matching _NOISY_KEY_PATTERNS and replaces
    any string value that looks like a presigned URL (regardless of its
    key name) with a short placeholder, then caps the serialized size.
    Non-dict/list values pass through untouched (size cap still applies
    via the caller's str() truncation) unless they themselves are a
    presigned URL.

    Before stripping, attempts to fetch-and-inline the primary text
    artifact (if any) referenced by an s3uri — see
    _inline_primary_text_artifact. When inlining succeeds, a higher size
    cap applies so the real content isn't immediately truncated back out.
    """
    value, inlined = _inline_primary_text_artifact(value)

    def _strip(v):
        if isinstance(v, dict):
            return {
                k: _strip(val) for k, val in v.items()
                if not any(pat in k.lower() for pat in _NOISY_KEY_PATTERNS)
            }
        if isinstance(v, list):
            return [_strip(item) for item in v]
        if _looks_like_presigned_url(v):
            return "[presigned URL omitted — see plan manifest for full value]"
        return v

    stripped = _strip(value)
    serialized = json.dumps(stripped, default=str) if isinstance(stripped, (dict, list)) else str(stripped)
    cap = _MAX_INJECTED_CHARS_WITH_INLINE if inlined else _MAX_INJECTED_CHARS
    if len(serialized) <= cap:
        return stripped

    # Still too big — truncate the serialized form with a note rather than
    # trying to guess which nested fields to drop further.
    truncated = serialized[:cap]
    return f"{truncated}... [truncated — see plan manifest for full output]"


def _gateway_of(tool_name):
    """The gateway/target a federated tool belongs to, or "" for a local tool.

    AgentCore names federated tools ``<target>___<tool>``, and a name can carry
    TWO separators (``target___aws___call_aws``), so split once.
    """
    text = str(tool_name or "")
    return text.split("___", 1)[0] if "___" in text else ""


def _ensure_node_can_reach_its_inputs(node_def, all_nodes):
    """Union in the tools that PRODUCED this node's inputs. Returns (tools, note).

    A node that consumes another node's output must be able to address that
    output. That is not a heuristic -- it is true by construction: whatever
    gateway produced a url can evidently accept urls of that kind.

    This exists because a plan declared a mux node with ONLY
    ``media-processing___process_media`` while its two dependencies had produced
    ``https://`` fal urls. M2C reads assets from its own bucket and cannot take a
    second external audio track, so the sub-agent correctly refused with
    "missing_capability" -- having been handed a tool that could not do the job.
    Its siblings held exactly the tool that could (fal's ``submit_job``).

    Deliberately NOT a name list, and deliberately not a rejection:

      * no target, tool or model is named here. The repair is derived from the
        plan's own evidence, so it keeps working for gateways nobody has
        registered yet -- the same reason the render hook stopped using a
        tool-name allowlist.
      * widening beats failing. The planner gets this right most of the time (two
        of three observed merge nodes were correct), so the goal is to survive the
        occasional wrong pick, not to abort a plan over it.
      * scoped to DIRECT dependencies only, so a long plan does not accumulate
        every tool it ever used.

    The mismatch is still logged, because papering over a planner defect without
    saying so is how it stays invisible.
    """
    declared = [t for t in (node_def.get("tools") or []) if isinstance(t, str)]
    deps = node_def.get("depends_on") or []
    if not deps:
        return declared, ""

    by_id = {n.get("id"): n for n in (all_nodes or []) if isinstance(n, dict)}
    producer_tools = []
    for dep_id in deps:
        for tool in (by_id.get(dep_id, {}).get("tools") or []):
            if isinstance(tool, str) and tool not in producer_tools:
                producer_tools.append(tool)

    # By BARE name: "<target>___<tool>" and "<tool>" are one tool, and listing both makes the node agent refuse a
    # duplicate registration (plan 593333b5).
    declared_bare = {t.rsplit("___", 1)[-1] for t in declared}
    missing = [t for t in producer_tools if t.rsplit("___", 1)[-1] not in declared_bare]
    if not missing:
        return declared, ""

    declared_gateways = {_gateway_of(t) for t in declared if _gateway_of(t)}
    producer_gateways = {_gateway_of(t) for t in producer_tools if _gateway_of(t)}
    note = (
        f"declared tools reach {sorted(declared_gateways) or ['local']} but this node "
        f"consumes output produced by {sorted(producer_gateways) or ['local']}; "
        f"adding {len(missing)} producer tool(s)"
    )
    return declared + missing, note


def _retry_failed_node(session_id, plan_id, actor_id, node_def, all_nodes, node_states, row,
                       *, turn_id="", delegation_grant="", model_id="", project_scope_id=""):
    """Re-dispatch a failed node with its error injected (P2 Tier-1 self-correction).

    Records the retry (bumps ``retries``, stores ``last_retry_error`` = the error being
    corrected) and flips the node to ``running`` BEFORE re-dispatch, so a concurrent
    chainer pass does not double-dispatch and the node is not counted terminal until the
    retry settles. The row is addressed in its plan's key format (``_node_key``). Never
    raises: a failure to record the retry just leaves the node failed.
    """
    nid = node_def["id"]
    error = row.get("error") or ""
    try:
        retries = int(row.get("retries", 0) or 0)
    except (TypeError, ValueError):
        retries = 0
    now = datetime.now(timezone.utc).isoformat()
    try:
        names = {"#st": "status", "#ttl": "ttl"}
        values = {":r": "running", ":n": retries + 1, ":e": error, ":now": now,
                  ":ttl": _ttl_epoch()}
        _plan_table.update_item(
            Key=_node_key(session_id, plan_id, nid),
            UpdateExpression=("SET #st = :r, retries = :n, last_retry_error = :e, "
                              "updated_at = :now, #ttl = :ttl"
                              + _identity_set(plan_id, nid, names, values)),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except Exception:
        logger.exception("Plan %s node %s: could not record retry; leaving failed", plan_id, nid)
        return
    # D-08: a node advanced (failed → running for self-correction). Slide the PLAN row's ttl so
    # the DAG shape outlives the sliding window along with the node row just written.
    _slide_plan_ttl(session_id, plan_id)
    # Reset the job-registry row to RUNNING so a SUCCESSFUL retry can terminalize it. The first
    # attempt set it FAILED (terminal) and set_terminal() only transitions from an ACTIVE status,
    # so without this a node that recovers on retry keeps a stale FAILED badge (observed on
    # sg3_report: plan SUCCEEDED, report produced, but the node showed FAILED).
    try:
        _job_table.update_item(
            Key={"jobId": f"{plan_id}#{nid}"},
            UpdateExpression="SET #st = :r, updatedAt = :now REMOVE terminalAt",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":r": "RUNNING", ":now": now},
        )
    except Exception:
        logger.debug("retry: could not reset job row to RUNNING for %s#%s", plan_id, nid)
    logger.info("Plan %s node %s: error-aware retry %d/%d after error: %s",
                plan_id, nid, retries + 1, MAX_NODE_RETRIES, error[:200])
    # attempt=retries+1 gives the retry its OWN runtime session: the attempt that failed may have
    # died WITH its microVM (or be hung in it), and the same id would route the retry back there.
    _dispatch_node(session_id, plan_id, actor_id, node_def, all_nodes, node_states,
                   turn_id=turn_id, delegation_grant=delegation_grant, retry_error=error,
                   model_id=model_id, claimed=True, project_scope_id=project_scope_id,
                   attempt=retries + 1)


def _deliverable_refs(value) -> list:
    """``(bucket, key, name)`` for a producer value's deliverable files, for prompt injection.

    The producer's DECLARED ``outputs[]`` (M2C status contract, folded in by
    ``_merge_declared_status_outputs``) plus the canonical JSON and media resolvers, deduped by
    object. Declared outputs carry files the canonical allowlist omits (``shot_metadata.jsonl``,
    ``conversation_analysis.json``), which is why Phase 3 got only a uuid without them. Never raises."""
    refs: list = []
    seen: set = set()
    for resolver in (lambda v: result_resolution.declared_output_refs(v),
                     lambda v: result_resolution.canonical_result_refs(v, limit=None),
                     lambda v: result_resolution.media_outputs_of(v)):
        try:
            found = resolver(value) or []
        except Exception:
            found = []
        for ref in found:
            if (ref[0], ref[1]) not in seen:
                seen.add((ref[0], ref[1]))
                refs.append(ref)
    return refs


def _dependency_injection_text(node_def, dep_outputs, node_states, session_id, plan_id) -> str:
    """Build the '--- Output from step X ---' block appended to a node's prompt per dependency.

    DETERMINISTIC HANDOFF (design §3 A1): a dependency's PERSISTED OUTPUT FILE is the source of
    truth. Every consumer is ALWAYS handed that file's reference + a bounded preview; it reads the
    file for the complete content. The inline `value` can be a placeholder / summary / truncated
    (e.g. an author node whose real 21 KB doc is in the file but whose structured value says
    "…delivered above"), so it is NEVER the authoritative handoff. Uniform for every tool/shape —
    no size-triggering, no per-tool branching. Never raises.
    """
    parts: list = []
    for dep_id in (node_def.get("depends_on") or []):
        dep_entry = (dep_outputs or {}).get(dep_id, {}) or {}
        dep_value = dep_entry.get("value")
        dep_state = (node_states or {}).get(dep_id, {}) or {}
        if dep_value is None:
            # Same idempotent-absorption gap as the cross-plan path: a resubmit that absorbed into
            # an already-finished M2C execution never gets a results-fetch, so the manifest has no
            # value. Read the job's own terminal output so the consumer still gets exact uris.
            dep_value = _on_demand_job_value(dep_state.get("job_id") or "")
        # Absent != empty: a missing `result_s3_key` means NO durable output was recorded. Do NOT
        # synthesise a path — a fabricated "authoritative" URI to a file that was never written is
        # a dead end the consumer is explicitly forbidden to reconstruct from. Only a recorded key
        # is treated as authoritative.
        recorded_key = dep_state.get("result_s3_key", "") or ""
        # Bounded preview: the manifest value if present, else a bounded read of the recorded file.
        if dep_value is not None:
            filtered = _filter_for_injection(dep_value)
            preview = json.dumps(filtered, indent=2) if isinstance(filtered, (dict, list)) else str(filtered)
        elif recorded_key:
            try:
                obj = _s3.get_object(Bucket=SKILLS_S3_BUCKET, Key=recorded_key)
                preview = obj["Body"].read().decode("utf-8")[:_MAX_INJECTED_CHARS]
            except Exception:
                preview = ""
        else:
            preview = ""
        preview = preview[:_MAX_INJECTED_CHARS]
        if recorded_key:
            parts.append(
                f'\n\n--- Output from step "{dep_id}" ---\n'
                f'FULL OUTPUT (authoritative — read this for the COMPLETE content): '
                f's3://{SKILLS_S3_BUCKET}/{recorded_key}\n'
                f'Read it with file_read / read_result_files (or boto3). The preview below may be a '
                f'summary, a placeholder, or truncated — never treat it as the full content, and never '
                f'guess or reconstruct content from it.\nPREVIEW:\n{preview}'
            )
        else:
            # No durable file was recorded for this step: the bounded content below is ALL that
            # exists. Say so plainly so the consumer uses it as-is instead of chasing a file that
            # was never written.
            parts.append(
                f'\n\n--- Output from step "{dep_id}" ---\n'
                f'No durable output file was recorded for this step; the content below is all that '
                f'is available (it may be a summary or truncated). Use it as-is.\nCONTENT:\n{preview}'
            )
        # A2 (design §3): if the producer was a gateway job, its output value carries an s3 PREFIX,
        # not a filename. Resolve the producer's NAMED deliverable files here and hand the consumer
        # the EXACT uris — deterministically — so it never invents `final_transcript.json`, a wrong
        # `audio.wav`, or a stem filename. canonical_result_refs covers JSON deliverables
        # (transcript/faces/shots); media_outputs_of covers non-JSON media (audio stems, video).
        # Text producers yield no refs (no-op).
        refs: list = _deliverable_refs(dep_value) if dep_value is not None else []
        if refs:
            listed = "\n".join(f"  - s3://{b}/{k}  ({name})" for (b, k, name) in refs[:60])
            parts.append(
                f'\nDELIVERABLE FILES produced by "{dep_id}" (use these EXACT uris — do NOT guess '
                f'or construct filenames):\n{listed}'
            )
    return "".join(parts)


def _on_demand_job_value(job_id: str):
    """Fetch a TERMINAL-SUCCEEDED SFN job's output directly, when no manifest value exists.

    The manifest is normally filled by the results-fetch path when the job registry flips a job
    to terminal (changefeed → results_ready → _dispatch_results_fetch). But M2C is idempotent and
    the registry is keyed by the SFN execution ARN: re-submitting an already-processed asset
    ABSORBS into an execution that finished long ago (and whose registry row belongs to the
    ORIGINAL session). No transition ever fires for the new session, so its node never reaches
    results_ready and its manifest stays empty — every consumer then sees "unavailable" (observed:
    session 28514e98 reran the cc09dea9 trailer; Phase-1 manifest outputs were {}). The SFN
    execution's own output IS the M2C result (same source as _dispatch_sfn_output_as_result), so
    read it on demand. Returns None for non-SFN ids, non-SUCCEEDED executions, error payloads, or
    any read failure — never raises."""
    if not job_id or not SFN_EXECUTION_ARN_RE.match(str(job_id)):
        return None
    try:
        resp = _sfn_client_for_arn(job_id).describe_execution(executionArn=job_id)
    except Exception:
        logger.warning("on-demand result: DescribeExecution failed for %s", str(job_id)[:80], exc_info=True)
        return None
    if resp.get("status") != "SUCCEEDED":
        return None
    raw_text = resp.get("output", "")
    try:
        value = json.loads(raw_text) if raw_text else None
    except (json.JSONDecodeError, TypeError):
        return None
    if value is None or _result_error_text(value):
        return None
    # Same declared-outputs gap as _dispatch_sfn_output_as_result: an absorbed enrichment /
    # conversation-analysis execution's own output is only {uuid, cost}. The registry row is keyed
    # by the execution ARN, so it holds the status tool regardless of whose run it was.
    if isinstance(value, dict) and not result_resolution.output_prefix(value):
        try:
            job_item = _job_table.get_item(Key={"jobId": job_id}).get("Item") or {}
        except Exception:
            job_item = {}
        value = _merge_declared_status_outputs(value, job_id, job_item, where="on-demand result")
    return value


def _external_producer_pool(session_id: str, node_id: str, plan_name: str = "",
                            consumer_plan: str = "") -> tuple:
    """(pool, legacy_named): the qualified rows that could be the producer of ``node_id``.

    ``pool`` is the rows in plans whose ``display_name`` (or plan id) is the reference's ``plan``;
    if there are none and that name is not a legacy plan's, every other plan's row of that node id.
    The consumer's own plan is never its producer. Never raises.
    """
    try:
        rows = _session_rows(session_id)
    except Exception:
        rows = []
    named, legacy_named = set(), False
    candidates = []
    dead_plans = set()
    for row in rows:
        row_plan, row_node = _split_sk(row.get("node_id"))
        if row_node == "PLAN":
            if row_plan and row.get("status") in _DEAD_PRODUCER_STATUSES:
                dead_plans.add(row_plan)
            if plan_name and row.get("display_name") == plan_name:
                if row_plan:
                    named.add(row_plan)
                else:
                    legacy_named = True
        elif (row_plan and row_node == node_id and row_plan != consumer_plan
              and row.get("status") not in _DEAD_PRODUCER_STATUSES):
            # A failed/skipped producer node produced nothing, so it is never a
            # candidate. Re-running a failed phase under the skill's required name leaves the
            # failed attempt's row beside the retry's; counting it made the hand-off ambiguous.
            # Pending/running rows stay: readiness waits on them instead of failing.
            candidates.append(row)
    if plan_name:
        named.add(plan_name)  # a reference may name the producer plan by its plan_id
    in_named = [r for r in candidates if _split_sk(r.get("node_id"))[0] in named]
    pool = in_named or ([] if legacy_named else candidates)
    # A plan that FAILED may still hold a completed copy of this node (Phase 1 failing after
    # its segmentation step). A re-run supersedes it, so prefer producers in live/completed
    # plans; a failed plan's output is used only when nothing else holds the node.
    live = [r for r in pool if _split_sk(r.get("node_id"))[0] not in dead_plans]
    return (live or pool), legacy_named


def _external_producer_plans(session_id: str, node_id: str, plan_name: str = "",
                             consumer_plan: str = "") -> list:
    """The distinct plan ids that could be the producer (sorted). More than one is AMBIGUOUS."""
    pool, _ = _external_producer_pool(session_id, node_id, plan_name, consumer_plan)
    return sorted({_split_sk(r.get("node_id"))[0] for r in pool})


def _external_producer_row(session_id: str, node_id: str, plan_name: str = "",
                           consumer_plan: str = "") -> dict:
    """The plan-state row of a prior-phase producer node, or {}.

    Node ids repeat across plans (sg1..sg5 in every guided phase), so a node id alone does not
    name a row. Qualified rows ("<plan_id>#<node_id>") are preferred: the one in the plan whose
    ``display_name`` (or plan id) is the reference's ``plan``, else -- unless that name is a legacy
    plan's -- the one in any other plan. When MORE THAN ONE plan qualifies the reference is
    ambiguous and this returns {}: it never picks the newest. Staging session d6a560a8 ran three
    Phase 1 plans all named "plan-content-moderation-foundation" (one per video); "newest wins"
    handed NBA's shots and transcript to every video's Phase 2. ``_dispatch_node`` fails such a node
    with a named error (``_external_inputs_ambiguity``). A legacy producer (bare "<node_id>" row) is
    the fallback, so phases run before the qualified keys still hand off. Never raises.
    """
    pool, legacy_named = _external_producer_pool(session_id, node_id, plan_name, consumer_plan)
    if pool:
        if len({_split_sk(r.get("node_id"))[0] for r in pool}) > 1:
            return {}
        return max(pool, key=lambda r: str(r.get("updated_at", "")))
    try:
        return _plan_table.get_item(
            Key={"session_id": session_id, "node_id": node_id}).get("Item") or {}
    except Exception:
        return {}


def _external_inputs_ambiguity(node_def, session_id: str, plan_id: str = "") -> str:
    """A named error when an external_inputs reference matches more than one producer plan, else "".

    Checked before dispatch: an ambiguous reference must fail the node, never resolve to a guess."""
    problems = []
    for ref in (node_def.get("external_inputs") or []):
        prod_node = (ref or {}).get("node") or ""
        if not prod_node:
            continue
        plan_name = (ref or {}).get("plan") or ""
        plans = _external_producer_plans(session_id, prod_node, plan_name, plan_id)
        if len(plans) > 1:
            problems.append(f'{len(plans)} plans{f" named {plan_name!r}" if plan_name else ""} hold '
                            f'"{prod_node}" (plan ids {", ".join(plans)})')
    if not problems:
        return ""
    return ("ambiguous external_inputs: " + "; ".join(problems) + ". Give each producer plan a unique "
            "name or node id (one plan per phase, one node per source), or reference the producer by "
            "its plan_id.")


def _external_producer_value(session_id: str, node_id: str, plan_name: str = "",
                             consumer_plan: str = "", row: dict = None):
    """Resolve a prior-phase producer's stored output VALUE, by NODE id.

    The producer's node row (``_external_producer_row``) has a ``result_s3_key`` that embeds
    the plan_id (``plans/<session>/<plan_id>/<node>/output``), which points at the manifest
    holding the value. So resolve producer → node row → plan_id → manifest.outputs[node].value.
    (Resolving by display_name alone failed while every plan shared the one "PLAN" row --
    session cc09dea9 -- so the node row stays the anchor and the name only disambiguates.)
    Returns None when the producer has no recorded output yet (caller emits an
    'unavailable' note and never fabricates a key). Never raises."""
    if row is None:
        row = _external_producer_row(session_id, node_id, plan_name, consumer_plan)
    value = None
    m = re.match(r"plans/[^/]+/([^/]+)/", row.get("result_s3_key") or "")
    if m:
        try:
            manifest = json.loads(_s3.get_object(
                Bucket=SKILLS_S3_BUCKET,
                Key=f"plans/{session_id}/{m.group(1)}/manifest.json")["Body"].read().decode("utf-8"))
            value = (manifest.get("outputs") or {}).get(node_id, {}).get("value")
        except Exception:
            value = None
    if value is None:
        # Idempotent-absorption gap: the job finished in an EARLIER session, so no results-fetch
        # ever filled this session's manifest. Read the job's own terminal output instead.
        value = _on_demand_job_value(row.get("job_id") or "")
    return value


def _external_inputs_readiness(node_def, session_id: str, plan_id: str = ""):
    """(missing, pending) for a node's external_inputs, checked BEFORE dispatch.

    ``missing``: producer node ids whose output cannot be resolved (manifest or on-demand job
    output). ``pending``: True when any missing producer's gateway job is still non-terminal — the
    output simply isn't there YET. Lets _dispatch_node DEFER (pending) or FAIL FAST (resolvable
    never) instead of dispatching an agent that can only report "UNAVAILABLE" and then gets marked
    completed — the false success seen in session 28514e98 (sg3/sg4 refused, plan said COMPLETED)."""
    missing: list = []
    pending = False
    for ref in (node_def.get("external_inputs") or []):
        prod_node = (ref or {}).get("node") or ""
        if not prod_node:
            continue
        row = _external_producer_row(session_id, prod_node, (ref or {}).get("plan") or "",
                                     plan_id)
        if _external_producer_value(session_id, prod_node, row=row) is not None:
            continue
        missing.append(prod_node)
        if _node_gateway_pending(row):
            pending = True
    return missing, pending


def _external_injection_text(node_def, session_id: str, plan_id: str = "") -> str:
    """Build the deliverable-URI block for a node's external_inputs (cross-plan,
    guided-mode phase handoff). Reuses result_resolution.canonical_result_refs /
    media_outputs_of — the SAME deterministic resolver as in-plan depends_on — so a
    Phase-2 consumer is handed the producer's EXACT files and never types a uuid.
    Never raises; a missing producer yields a plain 'unavailable' note."""
    parts: list = []
    for ref in (node_def.get("external_inputs") or []):
        plan_name = (ref or {}).get("plan") or ""
        prod_node = (ref or {}).get("node") or ""
        # Resolve by producer NODE id; the `plan` name only picks among plans that share
        # that node id (see _external_producer_row).
        value = (_external_producer_value(session_id, prod_node, plan_name, plan_id)
                 if prod_node else None)
        if value is None:
            parts.append(
                f'\n\n--- Prior-phase input "{prod_node}" (from plan "{plan_name}") ---\n'
                f'UNAVAILABLE: that producer output could not be resolved. Do NOT guess '
                f'or reconstruct an s3 key for it; if this input is required, report the '
                f'missing dependency rather than proceeding.'
            )
            continue
        refs: list = _deliverable_refs(value)
        # Parity with in-plan _dependency_injection_text: surface BOTH the resolved
        # deliverable files (for FILE-reading consumers) AND a bounded preview of the
        # producer's value (which carries output_s3_prefix / uuid — what a PREFIX-style
        # input like enrich's shot_segmentation_s3uri needs). Without the preview a
        # prefix input would force the model to strip a filename off a deliverable uri,
        # reintroducing the very reconstruction this handoff removes.
        filtered = _filter_for_injection(value)
        preview = (json.dumps(filtered, indent=2) if isinstance(filtered, (dict, list))
                   else str(filtered))[:_MAX_INJECTED_CHARS]
        block = f'\n\n--- Prior-phase input "{prod_node}" (from plan "{plan_name}") ---\n'
        if refs:
            listed = "\n".join(f"  - s3://{b}/{k}  ({name})" for (b, k, name) in refs[:60])
            block += (f'DELIVERABLE FILES (use these EXACT uris for file reads — do NOT guess '
                      f'or construct filenames):\n{listed}\n')
        block += (f'PRODUCER OUTPUT (metadata incl. the output prefix — for a PREFIX-style '
                  f'input take it from here; for file reads use the deliverable uris above, '
                  f'never reconstruct a key):\n{preview}')
        parts.append(block)
    return "".join(parts)


def _is_read_timeout(exc):
    """True when an invoke failed only on READING the response.

    urllib3 raises ReadTimeoutError after the request was sent and accepted — the
    Task Agent is already running — so for a fire-and-forget dispatch this is a
    success, never something to retry.

    The agent runtime keeps its OWN copy of this rule under the SAME name
    (``jobs.context._is_read_timeout``) because neither artifact can import the
    other — the arrangement ``_MIN_RUNTIME_SESSION_ID_LEN`` and
    ``SFN_EXECUTION_ARN_RE`` already use. That copy adds an ``isinstance`` check
    against botocore's and urllib3's ReadTimeoutError ahead of this string test,
    which it can because it imports them; both classes are literally NAMED
    ``ReadTimeoutError``, so the two predicates agree on every exception either
    one actually sees. Keep them in lock-step if the rule changes.
    """
    return "ReadTimeoutError" in type(exc).__name__ or "read operation timed out" in str(exc)


def _is_session_conflict(exc):
    """True for AgentCore's retryable 409 on a session that is mid-provision.

    Matched on the error CODE and the HTTP status rather than by catching the
    modelled ``_agentcore.exceptions.RetryableConflictException``: the Lambda
    runtime's bundled botocore may predate that shape, and referencing an
    unmodelled exception attribute raises — turning a retryable conflict into a
    crash. Both spellings mean the same thing here.
    """
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code") or ""
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        if code == "RetryableConflictException" or status == 409:
            return True
    return "RetryableConflict" in type(exc).__name__


def _consume_conflict_sleep(seconds):
    """Sleep ``seconds`` of session-conflict backoff if this invocation can afford it.

    Returns False WITHOUT sleeping once the per-invocation budget is spent, so a
    very wide fan-out degrades to an uncorrelated session instead of pushing a 60s
    Lambda toward its timeout.
    """
    global _session_conflict_sleep_left
    with _session_conflict_lock:
        if seconds > _session_conflict_sleep_left:
            return False
        _session_conflict_sleep_left -= seconds
    time.sleep(seconds)
    return True


def _invoke_task_agent(session_id, payload_bytes):
    """Invoke the Task Agent for one node under runtime session ``session_id``.

    ``session_id`` is now the node's OWN runtime session (_node_runtime_session_id), not the parent
    chat session: the parent rides the payload (``session_id`` / ``__plan_session_id``), which is
    what the task agent attributes outputs, manifests and job rows to. The history below explains
    why a runtimeSessionId is sent at all.

    This dispatch used to pass no ``runtimeSessionId``, so the session id reached
    the node ONLY inside the payload. That is not enough on this path, because a
    runtime is correlated by one of two mechanisms and a plan node gets neither:
    the request header (absent — that is what this function now sends) or an OTEL
    baggage tag the runtime sets from the payload during its own startup. The Task
    Agent entrypoint branches to the plan-node handler and RETURNS before it
    reaches that baggage call, and neither plan-node handler sets it. So the
    session id sits in the node's own input and nothing ever reads it into
    telemetry.

    Two consequences, both of which this fixes. AgentCore names per-session runtime
    log streams ``runtime-logs-<sessionId>``, so a node's entire stdout landed under
    a random uuid appearing in no table, span or plan row — unattributable, and
    invisible to the session-scoped built-in evaluations that read that log group.
    And without a consistent id each node may be routed to a NEW microVM, so an
    N-node plan paid N cold starts where it should pay about one.

    Best-effort in exactly the way jobs/context.invoke_agent_runtime_traced is: an
    id too short for AgentCore is dropped rather than sent (it would fail the
    whole invoke), and a rejection of the kwarg itself retries once WITHOUT it —
    propagation is a nice-to-have, a working dispatch is not.

    A retryable 409 is the case that must NOT take that fallback, because doing so
    looks exactly like the bug: see _SESSION_CONFLICT_BACKOFF_S. It retries the
    SAME session id, briefly, before giving up on correlation.

    The one exception to that retry is a read timeout: the caller's contract
    treats it as an ACCEPTED invoke, so retrying would dispatch the node a second
    time — two transcodes, two status writes. Re-raise so the caller's existing
    read-timeout branch records the dispatch as accepted.

    Deliberately no W3C trace-context params (traceParent/traceState/baggage) —
    they collide with the runtime's post-signing OTEL header injection and break
    SigV4; see the note in jobs/context.invoke_runtime_session_kwargs.
    """
    invoke_kwargs = {
        "agentRuntimeArn": TASK_AGENT_ARN,
        "qualifier": "DEFAULT",
        "payload": payload_bytes,
        "contentType": "application/json",
    }
    if not isinstance(session_id, str) or len(session_id) < _MIN_RUNTIME_SESSION_ID_LEN:
        logger.warning(
            "Dispatching without runtimeSessionId: session id %r is shorter than the "
            "%d chars AgentCore requires, so this node's spans will not correlate",
            session_id, _MIN_RUNTIME_SESSION_ID_LEN,
        )
        return _agentcore.invoke_agent_runtime(**invoke_kwargs)
    conflicts = 0
    while True:
        try:
            return _agentcore.invoke_agent_runtime(runtimeSessionId=session_id, **invoke_kwargs)
        except Exception as e:
            if _is_read_timeout(e):
                raise
            if _is_session_conflict(e) and conflicts < len(_SESSION_CONFLICT_BACKOFF_S):
                delay = _SESSION_CONFLICT_BACKOFF_S[conflicts]
                if _consume_conflict_sleep(delay):
                    conflicts += 1
                    logger.info(
                        "Session %s was mid-provision (409); backed off %.1fs and "
                        "retrying the SAME session (retry %d of %d)",
                        session_id, delay, conflicts, len(_SESSION_CONFLICT_BACKOFF_S),
                    )
                    continue
                logger.warning(
                    "Session %s still conflicting and this invocation's %.1fs backoff "
                    "budget is spent; dropping correlation for this node",
                    session_id, _SESSION_CONFLICT_SLEEP_BUDGET_S,
                )
            logger.warning(
                "invoke_agent_runtime with runtimeSessionId failed (%s); retrying without "
                "session propagation", e,
            )
            return _agentcore.invoke_agent_runtime(**invoke_kwargs)


def _fast_path_tool_call(node_def, tools, retry_error=""):
    """``{"name", "arguments"}`` for a node the Task Agent may execute WITHOUT a model loop, else None.

    Only a ``kind: "tool"`` node whose call is complete at plan time qualifies: a ``config.tool``
    it is allowed to call and an ``config.arguments`` object. Anything that would make the
    planned arguments stale disqualifies it, and the node runs as an ordinary model turn on its
    prompt instead: a dependency or external input (the chainer injects their values into the
    PROMPT, which a literal argument object cannot see), or a retry (the retry exists so a model
    can read the previous error and correct the call — re-sending the same arguments would only
    repeat it).
    """
    if node_def.get("kind") != "tool" or retry_error:
        return None
    if node_def.get("depends_on") or node_def.get("external_inputs"):
        return None
    cfg = node_def.get("config") or {}
    name, arguments = cfg.get("tool"), cfg.get("arguments")
    if not isinstance(name, str) or not name or not isinstance(arguments, dict):
        return None
    if name not in (tools or []):
        return None
    return {"name": name, "arguments": arguments}


def _dispatch_node(session_id, plan_id, actor_id, node_def, all_nodes, node_states,
                   turn_id="", delegation_grant="", retry_error="", model_id="",
                   claimed=False, project_scope_id="", attempt=0):
    """Invoke the Task Agent for a single node. Fire and forget.

    ``claimed=True`` means the caller already transitioned this node out of ``pending``
    (the error-aware retry pass does, to record ``retries``), so this function must not
    try to claim it again — its claim is conditioned on ``pending`` and would refuse.

    Three phases, split so _dispatch_ready_nodes can run the slow middle one concurrently:
    _prepare_node_dispatch (gates, prompt, CLAIM — DynamoDB/S3, serial), _invoke_prepared (the
    InvokeAgentRuntime call only — thread-safe low-level client), _finish_node_dispatch (the
    failure or job-registry write — serial).
    """
    prep = _prepare_node_dispatch(
        session_id, plan_id, actor_id, node_def, all_nodes, node_states,
        turn_id=turn_id, delegation_grant=delegation_grant, retry_error=retry_error,
        model_id=model_id, claimed=claimed, project_scope_id=project_scope_id, attempt=attempt)
    if prep is None:
        return
    _finish_node_dispatch(prep, _invoke_prepared(prep))


def _prepare_node_dispatch(session_id, plan_id, actor_id, node_def, all_nodes, node_states,
                           turn_id="", delegation_grant="", retry_error="", model_id="",
                           claimed=False, project_scope_id="", attempt=0):
    """Gate, build and CLAIM one node's dispatch. Returns the prepared dispatch, or None.

    None = nothing to invoke: deferred, failed fast, or already claimed by another delivery.
    """
    node_id = node_def["id"]

    # Cross-phase readiness gate. A node whose declared prior-phase input can't be resolved must
    # not be dispatched: the agent can only answer "UNAVAILABLE", and that answer was recorded as
    # a COMPLETED node (false success). Producer job still running → DEFER (leave pending; the
    # job's terminal update re-fires this lambda). Never resolvable → FAIL FAST with a missing-input
    # reason, which the retry gate treats as non-retryable (_is_missing_input_error).
    if node_def.get("external_inputs"):
        ambiguity = _external_inputs_ambiguity(node_def, session_id, plan_id)
        if ambiguity:
            _mark_node_failed(session_id, plan_id, node_id, ambiguity)
            return None
        missing, pending = _external_inputs_readiness(node_def, session_id, plan_id)
        if missing and pending:
            logger.info("Plan %s node %s: deferring — prior-phase input(s) %s still running",
                        plan_id, node_id, missing)
            return None
        if missing:
            _mark_node_failed(
                session_id, plan_id, node_id,
                f"unresolved reference: prior-phase input(s) {', '.join(missing)} could not be "
                f"resolved (no recorded output and no terminal job output). Re-run the earlier "
                f"phase before this one.",
            )
            return None

    prompt = node_def.get("prompt", "")
    tools, capability_note = _ensure_node_can_reach_its_inputs(node_def, all_nodes)
    if capability_note:
        logger.warning(
            "Plan %s node %s: %s", plan_id, node_id, capability_note,
        )

    # Inject outputs from completed dependencies — read structured values from
    # the plan manifest. The manifest stores the RAW tool/agent output (no
    # extraction at write time, per design), so we apply lightweight
    # filtering + a size cap here at read/injection time. This keeps the
    # manifest itself a complete record for debugging while keeping
    # downstream prompts lean.
    if SKILLS_S3_BUCKET:
        try:
            manifest_key = f"plans/{session_id}/{plan_id}/manifest.json"
            manifest_obj = _s3.get_object(Bucket=SKILLS_S3_BUCKET, Key=manifest_key)
            manifest = json.loads(manifest_obj["Body"].read().decode("utf-8"))
            dep_outputs = manifest.get("outputs", {})
        except Exception:
            dep_outputs = {}

        _dep_block = _dependency_injection_text(node_def, dep_outputs, node_states, session_id, plan_id)
        prompt += _dep_block
        # Cross-plan (guided-mode phase handoff): resolve external_inputs against the
        # referenced prior-phase plan's manifest so a later phase gets the producer's
        # EXACT deliverable uris rather than the model reconstructing a wrong-uuid path.
        _ext_block = _external_injection_text(node_def, session_id, plan_id)
        prompt += _ext_block
        if LINEAGE_EDGES:
            _record_node_inputs(plan_id, node_id, handed_input_refs(
                prompt, (node_def.get("config") or {}).get("arguments")))

    # Tier-1 error-aware self-correction (P2): on a retry, hand the Task Agent the exact
    # error from the prior attempt so it can correct the cause rather than repeat it.
    if retry_error:
        prompt += (
            "\n\n--- YOUR PREVIOUS ATTEMPT AT THIS STEP FAILED ---\n"
            f"{retry_error}\n"
            "Diagnose the cause (commonly a wrong or empty parameter, e.g. an unresolved "
            "input URI) and correct it this time. Do NOT repeat the same call unchanged.\n"
            "If the error names a path, URI, or endpoint that was 'not found' / 404, you most "
            "likely mis-copied an identifier. COPY every s3:// URI, actor/session id, and model "
            "endpoint id VERBATIM from the context above — do not retype or reconstruct them "
            "character by character, and do not add or drop a prefix."
        )

    payload = {
        "prompt": prompt,
        "tools": tools,
        "session_id": session_id,
        "actor_id": actor_id,
        "node_id": node_id,
        "plan_id": plan_id,
        # The node's own declared output contract, forwarded so the executor can
        # check it produced what it promised. A mux node declaring output_type
        # "s3_uri" returned prose explaining it could not merge, and was recorded
        # completed -- the plan then reported SUCCEEDED with no merged file.
        **({"output_type": node_def.get("output_type")} if node_def.get("output_type") else {}),
        **({"output_key": node_def.get("output_key")} if node_def.get("output_key") else {}),
        **({"turn_id": turn_id} if turn_id else {}),
        # Present the signed delegation grant beside actor_id (option E). The
        # Task Agent forwards it to the harness gateway as a request header; the
        # interceptor verifies it and derives the actor from the grant, treating
        # this actor_id as untrusted. Only set when the plan carried a grant.
        **({"delegation_grant": delegation_grant} if delegation_grant else {}),
        # Orchestrator's picked model, so the node's task agent runs on the same model the
        # user selected (task_agent falls back to its default when this is absent).
        **({"model_id": model_id} if model_id else {}),
        # Knowledge-graph tools: the Project's id only — the Task Agent mints its own Project_Token from the
        # delegation grant, so no token is ever stored on a plan or carried here.
        **({"project_scope": {"project_id": project_scope_id}} if project_scope_id else {}),
        # The plan's producing project (its plan row), so the node writes its outputs under
        # projects/<pid>/sessions/<sid>/output/ where the env opts in (asset-lineage Phase 3 (a)).
        **({"producing_project_id": _ppid} if (_ppid := _plan_producing_project(plan_id)) else {}),
        # The fal endpoint(s) this node was planned to use. The task agent refuses a submit to any
        # other endpoint before it reaches fal (session c3447913: planned H3-Max, ran Hailuo, 422).
        **({"endpoint_ids": node_def["endpoint_ids"]}
           if isinstance(node_def.get("endpoint_ids"), list) and node_def["endpoint_ids"] else {}),
        "s3_bucket": SKILLS_S3_BUCKET,
        "ddb_table": PLAN_STATE_TABLE,
        # Signal: write status back to plan state on completion
        "__plan_node": True,
        "__plan_session_id": session_id,
    }
    # Fast path (kind "tool"): the node names ONE call with complete arguments, so the Task Agent
    # makes it directly instead of running a model loop to arrive at the same call.
    tool_call = _fast_path_tool_call(node_def, tools, retry_error)
    if tool_call:
        payload["tool_call"] = tool_call
        if not payload["prompt"].strip():
            payload["prompt"] = (f"Call `{tool_call['name']}` exactly once with these arguments and "
                                 f"return its result:\n{json.dumps(tool_call['arguments'])}")

    # CLAIM THE NODE BEFORE INVOKING (idempotent dispatch). The plan-state write used to
    # happen AFTER the invoke, so a chainer invocation that died mid-loop left already-
    # dispatched nodes still `pending`; the DynamoDB stream then retried the SAME batch and
    # dispatched them a SECOND time. Session 27c33c57 ran 5 of its 7 trims twice that way
    # (400s of duplicate compute, both runs writing output and both setting `completed`).
    # A conditional pending->running claim makes a re-delivered batch a no-op per node.
    # Nothing has executed yet at this point, so there is no "agent already finished"
    # race to lose — and a claim followed by a crash before the invoke is recovered by the
    # RUNNING-with-no-progress watchdog above, which force-fails it for the retry pass.
    now = datetime.now(timezone.utc).isoformat()
    if not claimed:
        try:
            _plan_table.put_item(
                Item={
                    **_node_key(session_id, plan_id, node_id),
                    # Stamped so completion can tell THIS plan's nodes from a previous
                    # plan's leftovers in the same session. Without it a stale
                    # "completed" row satisfied the all-terminal check for a plan whose
                    # node was still running, and the run was announced done early.
                    "plan_id": plan_id,
                    "node": node_id,
                    "status": "running",
                    "updated_at": now,
                    "ttl": int(datetime.now(timezone.utc).timestamp()) + 86400,
                },
                ConditionExpression="attribute_not_exists(#st) OR #st = :pending",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={":pending": "pending"},
            )
        except _plan_table.meta.client.exceptions.ConditionalCheckFailedException:
            # Already claimed (a re-delivered stream batch) or already terminal. Either way
            # this node must NOT be dispatched again.
            logger.info("Node %s already claimed/past pending; skipping duplicate dispatch", node_id)
            return None

    logger.debug("Node %s payload: %s", node_id, json.dumps(_scrub_payload(payload), default=str))
    return {
        "session_id": session_id,
        "plan_id": plan_id,
        "node_id": node_id,
        "now": now,
        "payload_bytes": json.dumps(payload).encode(),
        "runtime_session_id": _node_runtime_session_id(plan_id, node_id, attempt, session_id),
    }


def _invoke_prepared(prep):
    """Invoke the Task Agent for a prepared dispatch. Never raises; returns the outcome.

    Outcomes: ``("accepted", "")``, ``("read_timeout", "")`` (also an accepted invoke), or
    ``("failed", reason)``. Touches only the low-level ``_agentcore`` client (thread-safe) so it can
    run on a worker thread; every table write happens in _finish_node_dispatch.
    """
    node_id = prep["node_id"]
    # Fire-and-forget: invoke the Task Agent and do NOT read the response stream.
    #
    # This used to `body.read(100)` to "confirm the stream opened". The Task Agent writes
    # nothing until its turn ends, so that read always blocked for the client's full
    # read_timeout (30s) and was then swallowed as "fine". It cost 30s PER NODE inside a
    # 60s Lambda: an 8-node fan-out needed 240s, so the function timed out mid-loop, the
    # stream re-delivered the batch, and the nodes dispatched so far ran twice. It also
    # turned a parallel fan-out into a ~30s staircase — session 27c33c57's 7 trims were
    # dispatched over 293s and the turn took 458s where the slowest trim was 133s.
    # The read confirmed nothing: `invoke_agent_runtime` has already returned response
    # HEADERS, which is the acceptance. Closing the stream releases the connection without
    # waiting for bytes; the runtime keeps running (it already outlived the abandoned read).
    try:
        response = _invoke_task_agent(prep["runtime_session_id"], prep["payload_bytes"])
        body = response.get("response", b"")
        if hasattr(body, "close"):
            try:
                body.close()
            except Exception:
                pass  # connection teardown is best-effort; the invoke was accepted
        logger.info("Dispatched node %s for plan %s (runtime session %s)",
                    node_id, prep["plan_id"], prep["runtime_session_id"])
        return ("accepted", "")
    except _agentcore.exceptions.ClientError as e:
        # A dispatch INVOKE failure (throttle, capacity, 403, ValidationException) used to `return`
        # SILENTLY, leaving the node `pending` with NO plan-state write — so no stream event fired,
        # the chainer was never re-invoked, and this node plus every dependent sat PENDING forever
        # (session 1a4b98f2: seg*_validate stranded, gallery never reached). Fail the node LOUDLY
        # instead: _mark_node_failed writes terminal state to BOTH stores, which emits a stream
        # event → the chainer re-runs → the retry pass re-dispatches a transient failure (or, past
        # the cap, skip-propagation settles the plan and surfaces the error). Never an eternal stall.
        logger.exception("Failed to dispatch node %s (ClientError) — marking failed so the plan settles", node_id)
        return ("failed", f"dispatch failed: {str(e)[:200]}")
    except Exception as e:
        # ReadTimeoutError from urllib3 means the request was sent but
        # we couldn't read the response in time. The Task Agent is still running.
        if _is_read_timeout(e):
            logger.info("Dispatched node %s for plan %s (read timed out — invoke accepted)",
                        node_id, prep["plan_id"])
            return ("read_timeout", "")
        else:
            # Same silent-stall trap as the ClientError branch above — fail loudly so the plan
            # settles / retries instead of leaving the node (and its dependents) PENDING forever.
            logger.exception("Failed to dispatch node %s — marking failed so the plan settles", node_id)
            return ("failed", f"dispatch failed: {str(e)[:200]}")



def _finish_node_dispatch(prep, outcome):
    """Record a dispatch's outcome (serial: table writes stay on the calling thread)."""
    session_id, plan_id, node_id, now = (
        prep["session_id"], prep["plan_id"], prep["node_id"], prep["now"])
    kind, reason = outcome
    if kind == "failed":
        _mark_node_failed(session_id, plan_id, node_id, reason)
        return
    # Plan state is already `running` (claimed before the invoke, or by the retry pass).
    # Nothing is written here on purpose: a post-dispatch write could clobber a fast node
    # that has already reported `completed`.
    #
    # Update Job Registry node entry to RUNNING (for Activity panel) —
    # conditional: only if status is still PENDING.
    try:
        _job_table.update_item(
            Key={"jobId": f"{plan_id}#{node_id}"},
            UpdateExpression="SET #st = :s, updatedAt = :now",
            ConditionExpression="#st = :pending",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":s": "RUNNING",
                ":now": now,
                ":pending": "PENDING",
            },
        )
    except Exception:
        logger.debug("Job registry update failed for %s#%s", plan_id, node_id)


def _merge_declared_status_outputs(raw_result, job_id: str, job_item: dict, actor_id: str = "",
                                   delegation_grant: str = "", where: str = ""):
    """Fold the M2C status tool's declared ``outputs[]`` into an SFN execution output.

    ``shot-metadata-enrichment`` and ``conversation-analysis`` finish with an execution output of
    only ``{uuid, total_estimated_cost}``; their files are named solely by the shared status tool
    the job-registry row records (``statusTool`` on ``resultsGatewayId``). Without this the node's
    stored value names no file, so ``external_inputs`` / ``get_plan_results`` hand the consumer a
    bare uuid (staging session b8a59e30). Called only when the value has no output prefix, so the
    state machines that already echo ``output.s3uri`` (shot segmentation, diarization) are
    untouched. One call, no retry: on any failure the SFN value is kept (no worse than before) and
    a WARNING names the job. Never raises."""
    if not isinstance(raw_result, dict) or result_resolution.output_prefix(raw_result):
        return raw_result
    item = job_item or {}
    status_tool = item.get("statusTool") or ""
    gateway_id = item.get("resultsGatewayId") or ""
    if not status_tool or not gateway_id or gateway_id == "__aws_sfn__" or not _gateway_table:
        return raw_result
    try:
        status = invoke_mcp_tool(
            gateway_id=gateway_id,
            tool_name=status_tool,
            job_id=job_id,
            actor_id=actor_id or item.get("actorId", "") or "",
            gateway_registry_table=_gateway_table,
            secrets_client=_secrets,
            delegation_grant=delegation_grant or item.get("delegationGrant", "") or "",
        )
    except Exception as exc:  # noqa: BLE001 - keep the SFN value; see docstring
        logger.warning("%s: status tool %s for %s failed; keeping the SFN output without declared "
                       "outputs: %s", where or "declared outputs", status_tool, str(job_id)[:80], exc)
        return raw_result
    merged = result_resolution.merge_status_outputs(raw_result, status)
    if merged is raw_result:
        logger.info("%s: status tool %s declared no outputs for %s", where or "declared outputs",
                    status_tool, str(job_id)[:80])
    return merged


def _dispatch_sfn_output_as_result(session_id, plan_id, node_id, job_id, now, job_item=None,
                                   actor_id="", delegation_grant=""):
    """Use an SFN execution's own terminal output as the node's result.

    Covers the M2C "shared-status" trio shape (jobs/detector.py
    _shared_status_job_trios): some services expose only a start tool +
    a shared get_job_status tool, no dedicated *_results tool. For these,
    the Step Functions execution's own output payload (visible via
    DescribeExecution once status=SUCCEEDED) IS the result — there is
    nothing further to fetch via MCP.

    Mechanically identical to the MCP results-fetch path otherwise: store
    the raw output in the manifest, write the S3 summary, transition the
    node to completed. Marks the node failed if the execution didn't
    actually succeed or the output can't be parsed as JSON (stored as
    raw text in that case rather than dropped).
    """
    try:
        resp = _sfn_client_for_arn(job_id).describe_execution(executionArn=job_id)
    except Exception as exc:
        logger.warning("Plan %s: node %s DescribeExecution failed: %s", plan_id, node_id, exc)
        # Leave in results_ready — next DDB Stream trigger retries.
        return

    if resp.get("status") != "SUCCEEDED":
        # Bridge fired on a status we don't expect here (defensive) —
        # leave alone rather than guessing; a later trigger will resolve it.
        logger.info(
            "Plan %s: node %s SFN execution status is %s, not SUCCEEDED yet; skipping",
            plan_id, node_id, resp.get("status"),
        )
        return

    raw_output_text = resp.get("output", "")
    try:
        raw_result = json.loads(raw_output_text) if raw_output_text else {}
    except (json.JSONDecodeError, TypeError):
        raw_result = {"raw_text": raw_output_text}

    # Even with SFN status == SUCCEEDED, the output body can be a failure (an M2C
    # workflow that reports status:FAILED in its own payload, etc.). Terminalise from
    # result validity so recovery sees it rather than laundering it completed.
    result_error = _result_error_text(raw_result)
    if result_error:
        logger.info(
            "Plan %s: node %s SFN succeeded but output body is an error; marking FAILED: %s",
            plan_id, node_id, result_error[:_RESULT_ERROR_MAX_CHARS],
        )
        _mark_node_failed(session_id, plan_id, node_id, result_error)
        return

    # The execution output of some M2C state machines names no file; their deliverables are
    # declared only by the status tool. Fold them in so the stored value is self-describing.
    raw_result = _merge_declared_status_outputs(
        raw_result, job_id, job_item or {}, actor_id, delegation_grant,
        where=f"Plan {plan_id}: node {node_id}")

    # A node can COMPLETE while reporting a deliverable URI in our own bucket that was never
    # written (run 627fb96e: success-shaped payload, missing image). Verify the claim; a
    # confirmed-missing deliverable is a false success -> FAILED (visible to the P2 retry gate).
    missing_deliverable = _missing_declared_deliverable(raw_result)
    if missing_deliverable:
        logger.info(
            "Plan %s: node %s SFN succeeded but %s; marking FAILED",
            plan_id, node_id, missing_deliverable,
        )
        _mark_node_failed(session_id, plan_id, node_id, missing_deliverable)
        return

    manifest_updated = False
    if SKILLS_S3_BUCKET:
        try:
            manifest_key = f"plans/{session_id}/{plan_id}/manifest.json"
            try:
                obj = _s3.get_object(Bucket=SKILLS_S3_BUCKET, Key=manifest_key)
                manifest = json.loads(obj["Body"].read().decode("utf-8"))
            except Exception:
                manifest = {"plan_id": plan_id, "outputs": {}}
            manifest.setdefault("outputs", {})[node_id] = {
                "status": "completed",
                "output_type": "json",
                "value": raw_result,
                "completed_at": now,
            }
            manifest["updated_at"] = now
            _s3.put_object(
                Bucket=SKILLS_S3_BUCKET, Key=manifest_key,
                Body=json.dumps(manifest, indent=2, default=str).encode("utf-8"),
                ContentType="application/json",
            )
            manifest_updated = True
        except Exception:
            logger.exception("Plan %s: failed to update manifest for node %s", plan_id, node_id)

    if not manifest_updated:
        return

    s3_key = f"plans/{session_id}/{plan_id}/{node_id}/output"
    try:
        _s3.put_object(
            Bucket=SKILLS_S3_BUCKET, Key=s3_key,
            Body=json.dumps(raw_result, indent=2, default=str).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception:
        logger.debug("Plan %s: failed to write node %s output summary", plan_id, node_id)

    node_advanced = False
    try:
        _plan_table.update_item(
            Key=_node_key(session_id, plan_id, node_id),
            UpdateExpression="SET #st = :s, updated_at = :now, result_s3_key = :key, #ttl = :ttl",
            ConditionExpression="#st = :ready",
            ExpressionAttributeNames={"#st": "status", "#ttl": "ttl"},
            ExpressionAttributeValues={
                ":s": "completed",
                ":now": now,
                ":key": s3_key,
                ":ready": "results_ready",
                ":ttl": _ttl_epoch(),
            },
        )
        node_advanced = True
    except _plan_table.meta.client.exceptions.ConditionalCheckFailedException:
        # Do NOT return here. This used to, and it is the "1/2 · Running" bug: the job-registry
        # row is a PROJECTION of this table, and "the authoritative row already advanced" is
        # precisely the case where the projection is the stale one — so skipping the mirror
        # skipped it exactly when it was needed. Nothing else recovers it either: plan/node rows
        # have `pollPartition` removed, so the polling worker is deliberately excluded.
        logger.info("Plan %s: node %s already advanced past results_ready; still mirroring "
                    "terminal status to the job registry", plan_id, node_id)

    # D-08: slide the PLAN row's ttl ONLY when THIS pass actually transitioned the node. A
    # ConditionalCheckFailed above means a prior pass already advanced it — re-mirroring the
    # projection is not new plan progress, so it must not restamp the PLAN ttl (constraint 1).
    if node_advanced:
        _slide_plan_ttl(session_id, plan_id)

    # Mirror the terminal status onto the projection UNCONDITIONALLY (both paths above lead
    # here), guarded so it is idempotent and cannot regress.
    try:
        _job_table.update_item(
            Key={"jobId": f"{plan_id}#{node_id}"},
            UpdateExpression=(
                "SET #st = :s, terminalAt = :now, updatedAt = :now, #ttl = :ttl "
                "REMOVE pollPartition"
            ),
            # `terminalAt` is stamped only by a terminal write, so requiring its absence makes
            # this write once-only: a replay is a no-op, and a late SUCCEEDED can never relabel
            # a node that already FAILED.
            ConditionExpression="attribute_not_exists(terminalAt)",
            # `TTL` is a DynamoDB reserved word, hence the name placeholder.
            ExpressionAttributeNames={"#st": "status", "#ttl": "ttl"},
            ExpressionAttributeValues={
                ":s": "SUCCEEDED",
                ":now": now,
                # A row the mirror never reaches used to stick in the Activity panel forever;
                # 24h matches the plan-state rows written in _dispatch_node.
                ":ttl": int(datetime.now(timezone.utc).timestamp()) + 86400,
            },
        )
    except _job_table.meta.client.exceptions.ConditionalCheckFailedException:
        logger.info("Plan %s: node %s projection is already terminal; nothing to mirror",
                    plan_id, node_id)
    except Exception:
        # Was logger.debug, which is why this whole class of divergence reproduced the exact
        # user-visible symptom with no error anywhere to find.
        logger.warning("Plan %s: job registry terminal update FAILED for %s#%s (job %s) — the "
                       "Activity panel will show this node as still running",
                       plan_id, plan_id, node_id, job_id, exc_info=True)

    logger.info("Plan %s: node %s SFN output used as result and marked completed", plan_id, node_id)


def _dispatch_results_fetch(session_id, plan_id, actor_id, node_id, node_state,
                            delegation_grant=""):
    """Fetch results for a node in results_ready state — no LLM involved.

    This is a purely mechanical step: call the results tool directly via
    the MCP gateway (same pattern as the polling worker's status checks),
    store the RAW tool response in the plan manifest, and transition the
    node to "completed". No extraction/parsing logic — we keep everything
    the tool returns so downstream steps (or humans debugging) never lose
    data to an extractor that didn't anticipate a field.

    Falls back to "failed" if the gateway call errors — the DDB Stream
    retry (next chainer invocation) will pick it up again since we don't
    move the node out of results_ready until this succeeds.
    """
    job_id = node_state.get("job_id", "")  # SFN ARN stored on the node
    now = datetime.now(timezone.utc).isoformat()

    if not job_id or not _gateway_table:
        logger.warning(
            "Plan %s: node %s missing job_id or gateway table not configured; cannot fetch results",
            plan_id, node_id,
        )
        _mark_node_failed(session_id, plan_id, node_id, "missing job_id or gateway registry")
        return

    # Look up the results_tool + results gateway from the Job Registry (the
    # auto-registered SFN job entry has it populated by strands_tool_wrap).
    # NOTE: gatewayId on this record may be the "__aws_sfn__" sentinel used
    # to route STATUS polling to DescribeExecution — that sentinel says
    # nothing about where results_tool lives. resultsGatewayId is the
    # trio's real (un-overridden) gateway and is what results_tool must be
    # invoked against. Older registry entries written before this field
    # existed fall back to gatewayId.
    try:
        resp = _job_table.get_item(Key={"jobId": job_id})
        job_item = resp.get("Item", {})
        results_tool = job_item.get("resultsTool", "")
        gateway_id = job_item.get("resultsGatewayId") or job_item.get("gatewayId", "")
    except Exception:
        logger.exception("Plan %s: failed to look up job registry entry for %s", plan_id, job_id)
        _mark_node_failed(session_id, plan_id, node_id, "job registry lookup failed")
        return

    if not results_tool:
        # M2C shared-status shape (see jobs/detector.py _shared_status_job_trios):
        # some trios have no dedicated *_results tool at all — the terminal
        # DescribeExecution output on the SFN execution IS the result. Fetch
        # it directly via boto3 rather than failing the node.
        if SFN_EXECUTION_ARN_RE.match(job_id):
            _dispatch_sfn_output_as_result(
                session_id, plan_id, node_id, job_id, now, job_item=job_item,
                actor_id=actor_id,
                delegation_grant=delegation_grant or node_state.get("delegation_grant", "") or "")
            return

        # No results tool and job_id isn't an SFN execution ARN either —
        # nothing we know how to fetch. Mark failed so the plan doesn't
        # hang; the underlying node's own output already has whatever
        # data the Task Agent could see at dispatch time.
        logger.warning(
            "Plan %s: node %s has no resolvable results tool and job_id is not an SFN ARN (job_id=%s)",
            plan_id, node_id, job_id[:60],
        )
        _mark_node_failed(session_id, plan_id, node_id, "no results tool resolvable")
        return

    if not gateway_id or gateway_id == "__aws_sfn__":
        # Has a results_tool name but no real gateway to call it on — the
        # sentinel leaked through with nothing usable behind it.
        logger.warning(
            "Plan %s: node %s has a results_tool but no resolvable gateway (gateway_id=%s, results_tool=%s)",
            plan_id, node_id, gateway_id, results_tool,
        )
        _mark_node_failed(session_id, plan_id, node_id, "no results gateway resolvable")
        return

    logger.info(
        "Plan %s: fetching results for node %s (job_id=%s, gateway=%s, tool=%s)",
        plan_id, node_id, job_id[:40], gateway_id, results_tool,
    )

    try:
        raw_result = invoke_mcp_tool(
            gateway_id=gateway_id,
            tool_name=results_tool,
            job_id=job_id,
            actor_id=actor_id,
            gateway_registry_table=_gateway_table,
            secrets_client=_secrets,
            # A user_jwt gateway needs both: the grant proves whose work this is, and
            # a queue results tool requires the endpoint alongside the request id.
            delegation_grant=delegation_grant or node_state.get("delegation_grant", "") or "",
            endpoint_id=node_state.get("endpoint_id", "") or "",
        )
    except (GatewayInvocationError, GatewayDescriptorMissing) as exc:
        logger.warning("Plan %s: results fetch failed for node %s: %s", plan_id, node_id, exc)
        # Leave the node in results_ready — the next DDB Stream trigger
        # (or a manual retry) will attempt the fetch again. Only mark
        # failed after repeated attempts would require a retry counter;
        # for now, transient gateway errors self-heal on the next trigger.
        return
    except Exception:
        logger.exception("Plan %s: unexpected error fetching results for node %s", plan_id, node_id)
        _mark_node_failed(session_id, plan_id, node_id, "unexpected error during results fetch")
        return

    # A completed job can still carry a FAILURE in its result body (fal 422, M2C
    # status:FAILED, MCP isError, moderation). Terminalise from result VALIDITY, not
    # completion: mark FAILED so the P2 retry gate can see it and, if it repeats, the
    # plan settles FAILED honestly instead of laundering a green node downstream.
    result_error = _result_error_text(raw_result)
    if result_error:
        logger.info(
            "Plan %s: node %s job completed but returned an error payload; marking FAILED: %s",
            plan_id, node_id, result_error[:_RESULT_ERROR_MAX_CHARS],
        )
        _mark_node_failed(session_id, plan_id, node_id, result_error)
        return

    # False-success guard (run 627fb96e): a node can return a success-shaped payload naming a
    # deliverable in our bucket that was never written. Verify the claim before completing.
    missing_deliverable = _missing_declared_deliverable(raw_result)
    if missing_deliverable:
        logger.info(
            "Plan %s: node %s completed but %s; marking FAILED",
            plan_id, node_id, missing_deliverable,
        )
        _mark_node_failed(session_id, plan_id, node_id, missing_deliverable)
        return

    # Store the RAW tool response in the plan manifest — no extraction.
    # Downstream consumers (chainer's prompt injection, debugging) filter
    # as needed at read time.
    manifest_updated = False
    if SKILLS_S3_BUCKET:
        try:
            manifest_key = f"plans/{session_id}/{plan_id}/manifest.json"
            try:
                obj = _s3.get_object(Bucket=SKILLS_S3_BUCKET, Key=manifest_key)
                manifest = json.loads(obj["Body"].read().decode("utf-8"))
            except Exception:
                manifest = {"plan_id": plan_id, "outputs": {}}
            manifest.setdefault("outputs", {})[node_id] = {
                "status": "completed",
                "output_type": "json",
                "value": raw_result,
                "completed_at": now,
            }
            manifest["updated_at"] = now
            _s3.put_object(
                Bucket=SKILLS_S3_BUCKET, Key=manifest_key,
                Body=json.dumps(manifest, indent=2, default=str).encode("utf-8"),
                ContentType="application/json",
            )
            manifest_updated = True
        except Exception:
            logger.exception("Plan %s: failed to update manifest for node %s", plan_id, node_id)

    if not manifest_updated:
        # Don't advance the node to completed if we couldn't persist the
        # result anywhere — better to retry than to silently lose data.
        return

    # Also write a lightweight text summary to the node's S3 output key so
    # existing consumers of result_s3_key (e.g. UI previews) keep working.
    s3_key = f"plans/{session_id}/{plan_id}/{node_id}/output"
    try:
        _s3.put_object(
            Bucket=SKILLS_S3_BUCKET, Key=s3_key,
            Body=json.dumps(raw_result, indent=2, default=str).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception:
        logger.debug("Plan %s: failed to write node %s output summary", plan_id, node_id)

    # Transition plan state: results_ready -> completed
    try:
        _plan_table.update_item(
            Key=_node_key(session_id, plan_id, node_id),
            UpdateExpression="SET #st = :s, updated_at = :now, result_s3_key = :key, #ttl = :ttl",
            ConditionExpression="#st = :ready",
            ExpressionAttributeNames={"#st": "status", "#ttl": "ttl"},
            ExpressionAttributeValues={
                ":s": "completed",
                ":now": now,
                ":key": s3_key,
                ":ready": "results_ready",
                ":ttl": _ttl_epoch(),
            },
        )
    except _plan_table.meta.client.exceptions.ConditionalCheckFailedException:
        logger.info("Plan %s: node %s already advanced past results_ready", plan_id, node_id)
        return

    # D-08: the node genuinely transitioned results_ready → completed (the ConditionalCheckFailed
    # path returned above), so slide the PLAN row's ttl to keep the DAG shape alive with it.
    _slide_plan_ttl(session_id, plan_id)

    # Transition Job Registry entry: RUNNING -> SUCCEEDED (for Activity panel)
    try:
        _job_table.update_item(
            Key={"jobId": f"{plan_id}#{node_id}"},
            UpdateExpression="SET #st = :s, terminalAt = :now, updatedAt = :now REMOVE pollPartition",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":s": "SUCCEEDED", ":now": now},
        )
    except Exception:
        logger.debug("Job registry terminal update failed for %s#%s", plan_id, node_id)

    logger.info("Plan %s: node %s results fetched and marked completed", plan_id, node_id)


def _mark_node_skipped(session_id, plan_id, node_id, reason):
    """Mark a node skipped in both stores (an upstream dep failed, so it can never run).

    Skipped is a TERMINAL state for completion accounting (so the plan settles) but is
    NOT success — a plan with any skipped node settles FAILED. Mirrors _mark_node_failed.
    """
    now = datetime.now(timezone.utc).isoformat()
    try:
        names = {"#st": "status", "#err": "error", "#ttl": "ttl"}
        values = {":s": "skipped", ":now": now, ":reason": reason, ":ttl": _ttl_epoch()}
        _plan_table.update_item(
            Key=_node_key(session_id, plan_id, node_id),
            UpdateExpression=("SET #st = :s, updated_at = :now, #err = :reason, #ttl = :ttl"
                              + _identity_set(plan_id, node_id, names, values)),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except Exception:
        logger.exception("Plan %s: failed to mark node %s skipped", plan_id, node_id)
    # D-08: skip is a terminal node transition (genuine progress) — slide the PLAN row's ttl.
    _slide_plan_ttl(session_id, plan_id)
    try:
        _job_table.update_item(
            Key={"jobId": f"{plan_id}#{node_id}"},
            UpdateExpression="SET #st = :s, terminalAt = :now, updatedAt = :now REMOVE pollPartition",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":s": "SKIPPED", ":now": now},
        )
    except Exception:
        logger.debug("Job registry skip update failed for %s#%s", plan_id, node_id)


def _read_manifest_outputs(session_id, plan_id):
    """The plan manifest's ``outputs`` map ({node_id: {value, status, ...}}), or {}.

    Best-effort: any read/parse failure returns {} so a missing manifest degrades to
    "no upstream value" (the caller fails the map node loudly) rather than raising.
    """
    if not SKILLS_S3_BUCKET:
        return {}
    try:
        key = f"plans/{session_id}/{plan_id}/manifest.json"
        obj = _s3.get_object(Bucket=SKILLS_S3_BUCKET, Key=key)
        return json.loads(obj["Body"].read().decode("utf-8")).get("outputs", {}) or {}
    except Exception:
        logger.info("map-expand: no manifest outputs for plan %s (yet)", plan_id)
        return {}


def _render_item_template(text, item_var, item):
    """Substitute a map item into a template prompt.

    ``{{item_var}}`` -> the item (str as-is, else compact JSON); ``{{item_var.field}}``
    -> that field of a dict item. Deliberately tiny (no Jinja dependency in the Lambda);
    the design grammar uses exactly these two forms.
    """
    out = text or ""
    if isinstance(item, dict):
        for k, v in item.items():
            out = out.replace("{{%s.%s}}" % (item_var, k), str(v))
    out = out.replace("{{%s}}" % item_var, item if isinstance(item, str) else json.dumps(item))
    return out


def _nodes_s3_key(session_id, plan_id):
    return f"plans/{session_id}/{plan_id}/nodes.json"


def _load_nodes_json(plan_record):
    """Node defs from the PLAN row: inline ``nodes_json``, else the offloaded S3 pointer.

    Large fan-out plans exceed DynamoDB's 400KB item cap, so the node list is offloaded to
    ``plans/<session>/<plan>/nodes.json`` with a ``nodes_s3_key`` pointer on the PLAN row (see
    ``_store_nodes_json_on_plan`` and the agent-side writer). Reads either; ``[]`` when neither
    is present/valid so the caller returns without dispatching (existing behaviour).
    """
    inline = plan_record.get("nodes_json")
    if inline:
        try:
            parsed = json.loads(inline)
            if parsed:
                return parsed
        except (json.JSONDecodeError, TypeError):
            logger.error("Invalid inline nodes_json for plan %s", plan_record.get("plan_id"))
            return []
    key = plan_record.get("nodes_s3_key")
    if key and SKILLS_S3_BUCKET:
        try:
            return json.loads(
                _s3.get_object(Bucket=SKILLS_S3_BUCKET, Key=key)["Body"].read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.error("Could not load offloaded nodes_json %s for plan %s: %s",
                         key, plan_record.get("plan_id"), e)
    return []


def _store_nodes_json_on_plan(session_id, plan_id, plan_sk, nodes_json):
    """Persist the (possibly map-grown) node list on the PLAN row, offloading to S3 when it
    would breach the DynamoDB item cap. Inline for small plans (backward-compatible); for large
    ones write ``plans/<session>/<plan>/nodes.json`` + a ``nodes_s3_key`` pointer and clear the
    inline copy so the pointer is authoritative and the item stays small.
    """
    now = datetime.now(timezone.utc).isoformat()
    serialized = json.dumps(nodes_json)
    if len(serialized.encode("utf-8")) <= NODES_JSON_INLINE_MAX or not SKILLS_S3_BUCKET:
        # D-08: this write IS a PLAN-row write (the DAG shape just grew), so stamping ttl here
        # slides the PLAN row directly — no separate _slide_plan_ttl needed.
        _plan_table.update_item(
            Key={"session_id": session_id, "node_id": plan_sk},
            UpdateExpression="SET nodes_json = :nj, updated_at = :now, #ttl = :ttl REMOVE nodes_s3_key",
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={":nj": serialized, ":now": now, ":ttl": _ttl_epoch()},
        )
        return
    key = _nodes_s3_key(session_id, plan_id)
    _s3.put_object(Bucket=SKILLS_S3_BUCKET, Key=key,
                   Body=serialized.encode("utf-8"), ContentType="application/json")
    _plan_table.update_item(
        Key={"session_id": session_id, "node_id": plan_sk},
        UpdateExpression="SET nodes_s3_key = :k, nodes_json = :empty, updated_at = :now, #ttl = :ttl",
        ExpressionAttributeNames={"#ttl": "ttl"},
        ExpressionAttributeValues={":k": key, ":empty": "", ":now": now, ":ttl": _ttl_epoch()},
    )
    logger.info("Plan %s: offloaded %d-byte nodes_json to s3://%s/%s",
                plan_id, len(serialized), SKILLS_S3_BUCKET, key)


def _persist_map_children(session_id, plan_id, actor_id, child_defs, nodes_json, plan_sk, mid):
    """Write the materialized child node rows + register them, rewrite the PLAN row's
    nodes_json to include children, and reflect the expansion in the job registry so the
    UI's PlanDagView renders producer -> map -> children -> gather. Children carry
    ``dependsOn`` (the map) so the graph draws their edges."""
    now = datetime.now(timezone.utc).isoformat()
    ttl = int(datetime.now(timezone.utc).timestamp()) + 86400
    for c in child_defs:
        _plan_table.put_item(Item={
            **_node_key(session_id, plan_id, c["id"]), "plan_id": plan_id, "node": c["id"],
            "status": "pending", "depends_on": json.dumps(c["depends_on"]),
            "tools": json.dumps(c["tools"]), "prompt": c["prompt"],
            "output_type": c["output_type"], "updated_at": now, "ttl": ttl,
        })
    # Persist the expanded graph on the PLAN row so future passes (and completion
    # accounting) see the children. Offloads to S3 when the grown list would breach the
    # 400KB item cap (a wide fan-out). One write; the PLAN row keeps status "pending".
    _store_nodes_json_on_plan(session_id, plan_id, plan_sk, nodes_json)
    if not (JOB_REGISTRY_TABLE and actor_id):
        return
    # Register each child as a node| job row WITH dependsOn — PlanDagView builds edges
    # from nodeId + dependsOn, so without dependsOn the children would render as orphans.
    for c in child_defs:
        try:
            _job_table.put_item(Item={
                "jobId": f"{plan_id}#{c['id']}", "actorId": actor_id, "sessionId": session_id,
                "runId": plan_id, "nodeId": c["id"], "dependsOn": c["depends_on"],
                "gatewayId": "__coordinator__", "trioId": f"node|{c['id']}", "status": "PENDING",
                "startAt": now, "updatedAt": now, "ttl": ttl,
            })
        except Exception:
            logger.debug("map-expand: child job register failed for %s", c["id"])
    # The map (control) node did its work by expanding — mark its job row terminal so the
    # DAG shows it done rather than perpetually running.
    try:
        _job_table.update_item(
            Key={"jobId": f"{plan_id}#{mid}"},
            UpdateExpression="SET #st = :s, terminalAt = :now, updatedAt = :now REMOVE pollPartition",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":s": "SUCCEEDED", ":now": now},
        )
    except Exception:
        logger.debug("map-expand: map job terminal update failed for %s", mid)
    # Rewire the gather's job row to depend on the children (it was registered depending on
    # the map at plan creation), so the graph draws children -> gather, not map -> gather.
    child_ids = [c["id"] for c in child_defs]
    for g in nodes_json:
        if g.get("kind") == "gather" and (g.get("config") or {}).get("of") == mid:
            try:
                _job_table.update_item(
                    Key={"jobId": f"{plan_id}#{g['id']}"},
                    UpdateExpression="SET dependsOn = :d, updatedAt = :now",
                    ExpressionAttributeValues={":d": child_ids or [mid], ":now": now},
                )
            except Exception:
                logger.debug("map-expand: gather dep rewire failed for %s", g["id"])


# _distinct_speakers was removed: deriving a fan-out list from an async producer's transcript
# (or any raw output) is no longer the runtime's job. The planner emits a collector node that
# materialises {"items": [...]} and the map points its over.from at that collector — see
# docs/fanout-collector-and-large-artifact-design.md. This keeps the map runtime domain-agnostic.


class _MapNotReady(Exception):
    """The producer's result is not resolvable YET — a transient condition; retry next tick."""


def _resolve_map_items(session_id, plan_id, producer, path, node_states, outputs):
    """Resolve a map node's fan-out list from ``over.from`` — GENERICALLY.

    The map fans out over a NAMED node's INLINE list output: read ``over.from``'s result and
    extract the list at ``path``. No domain heuristics, no S3-prefix crawling. Deriving a list
    from raw producer files (speakers from a transcript, files under an S3 prefix) is the job
    of a COLLECTOR node the planner emits — a node that materialises ``{"items": [...]}`` — not
    of this runtime. The map's ``over.from`` points at that collector.

    The producer value is read FRESH (manifest, else a fresh GetItem of the producer row's
    ``result_s3_key``) rather than from the possibly-stale ``node_states`` snapshot: a node's
    status and result_s3_key are written in one atomic update, so a fresh read after completion
    always sees the result. This closes the same-invocation stale-snapshot race that
    permanently failed plan e8e4f52b even though the diarize output object existed.

    Raises:
        _MapNotReady        the producer result is not visible yet (transient — retry).
        FanoutExtractError  the result is present but has no list at ``path`` (terminal — a
                            planning error; the message says to add a collector node).
    """
    value = (outputs.get(producer) or {}).get("value")
    raw_text = None  # producer result text — kept for a tolerant fallback (prose-wrapped / multi-object)
    rk = ""
    # Fetch the producer row for its result_s3_key. Needed to (a) load the value when the manifest
    # snapshot lacks it, AND (b) as a tolerant fallback source when a (possibly WRONG) snapshot value
    # fails the path — see 466addc8: the captured value was {"uris":[...]} but the raw result text
    # carried the real {"batches":[...]} (the model prepended prose so a strict json.loads failed).
    # Strongly-consistent so a just-committed result_s3_key is authoritative on the first attempt.
    try:
        row = _plan_table.get_item(
            Key=_node_key(session_id, plan_id, producer),
            ConsistentRead=True).get("Item") or {}
        rk = row.get("result_s3_key") or ""
    except Exception as e:  # a row-read blip IS transient — retry, don't fail the plan
        if value is None:
            raise _MapNotReady(f"producer '{producer}' row read failed transiently: {e}") from e
    if rk and SKILLS_S3_BUCKET:
        try:
            raw_text = _s3.get_object(Bucket=SKILLS_S3_BUCKET, Key=rk)["Body"].read().decode("utf-8")
        except Exception as e:  # rk set (object exists) but momentarily unreadable — transient
            if value is None:
                raise _MapNotReady(
                    f"producer '{producer}' result_s3_key {rk} not readable yet: {e}") from e
    if value is None:
        if not rk:
            # Completed + strongly-consistent + no manifest value + no result_s3_key => the producer
            # genuinely produced nothing to fan out over. TERMINAL (retrying cannot conjure a result;
            # leaving it pending would HANG the plan). An empty {"items": []} is on_empty's job.
            raise fanout_extract.FanoutExtractError(
                f"map over.from '{producer}' completed but produced no result to fan out over — its "
                f"collector node must emit a JSON list, e.g. {{\"items\": [...]}}.")
        if raw_text is None:
            raise _MapNotReady(f"producer '{producer}' result_s3_key {rk} not readable yet")
        try:
            value = json.loads(raw_text)
        except Exception:
            value = None  # not clean JSON (e.g. prose-wrapped) — the tolerant scan below recovers it

    # 1) Fast path: the captured value already carries the list at `path`.
    if value is not None:
        try:
            return fanout_extract.extract_collection(value, path)
        except fanout_extract.FanoutExtractError:
            pass  # wrong/insufficient captured value — fall through to the tolerant scan
    # 2) Tolerant fallback: scan the raw result text for the embedded JSON object carrying `path`.
    if raw_text:
        try:
            return fanout_extract.find_collection_in_text(raw_text, path)
        except fanout_extract.FanoutExtractError:
            pass
    raise fanout_extract.FanoutExtractError(
        f"map over.from '{producer}' output has no list at path {path!r} — checked the captured "
        f"value AND scanned the raw result for an embedded JSON object with that path. over.from must "
        f"reference a node whose output contains the list INLINE, e.g. {{\"{path}\": [...]}}. If the "
        f"collection is derived (speakers in a transcript, files under an S3 prefix), add a collector "
        f"node that emits {{\"items\": [...]}} and point the map at it."
    )


def _expand_ready_map_nodes(session_id, plan_id, actor_id, nodes_json, node_statuses, node_states, plan_sk):
    """Phase 2 core: expand each READY ``map`` node into N per-item child task nodes.

    A map node is READY when its ``over.from`` producer is completed. Expansion reads the
    producer's output from the manifest, resolves ``over.path`` to a list, materializes one
    child task node per item (``<map_id>__<i>``, depends_on the producer so it's immediately
    runnable), rewires any ``gather`` whose ``config.of`` is this map to depend on the
    children, appends the children to ``nodes_json``, and marks the map node completed.

    Mutates ``nodes_json`` and ``node_statuses`` in place and persists. Idempotent: a map
    whose children already exist in ``nodes_json`` is skipped (re-delivery safe). Returns
    True if anything changed.
    """
    changed = False
    existing_ids = {n["id"] for n in nodes_json}
    outputs = None
    for map_def in [n for n in list(nodes_json) if n.get("kind") == "map"]:
        mid = map_def["id"]
        if node_statuses.get(mid, "pending").lower() != "pending":
            continue
        if any(cid.startswith(f"{mid}__") for cid in existing_ids):
            continue  # already expanded
        cfg = map_def.get("config") or {}
        over = cfg.get("over") or {}
        producer, path = over.get("from"), over.get("path", "")
        if not producer or node_statuses.get(producer, "").lower() not in ("completed", "succeeded"):
            continue  # producer not done yet — try again when it completes
        if outputs is None:
            outputs = _read_manifest_outputs(session_id, plan_id)
        try:
            items = _resolve_map_items(session_id, plan_id, producer, path, node_states, outputs)
        except _MapNotReady as e:
            # Transient: the producer's result isn't visible yet. Leave the map PENDING so the
            # next stream tick re-attempts, rather than permanently failing the plan — the exact
            # bug that killed e8e4f52b when a stale snapshot missed a result that had landed.
            logger.info("Plan %s: map %s not ready (%s) — leaving pending to retry", plan_id, mid, e)
            continue
        except Exception as e:
            logger.warning("Plan %s: map %s expansion failed: %s", plan_id, mid, e)
            _mark_node_failed(session_id, plan_id, mid, f"map expansion failed: {e}")
            node_statuses[mid] = "failed"
            changed = True
            continue
        max_fanout = int(cfg.get("max_fanout", 50) or 50)
        on_empty = (cfg.get("on_empty") or "skip").lower()
        if not items and on_empty == "fail":
            _mark_node_failed(session_id, plan_id, mid,
                              "map produced an empty collection (on_empty=fail)")
            node_statuses[mid] = "failed"
            changed = True
            continue
        if len(items) > max_fanout:
            _mark_node_failed(session_id, plan_id, mid,
                              f"fan-out width {len(items)} exceeds max_fanout {max_fanout}")
            node_statuses[mid] = "failed"
            changed = True
            continue
        item_var = cfg.get("item_var", "item")
        template = cfg.get("template") or {}
        child_defs = [{
            "id": f"{mid}__{i}", "kind": "task",
            "prompt": _render_item_template(template.get("prompt", ""), item_var, item),
            "tools": list(template.get("tools", [])), "skills": list(template.get("skills", [])),
            # Children depend on the MAP node (not the producer): the item is already baked
            # into the prompt, so this gives a clean producer -> map -> children -> gather
            # graph and avoids re-injecting the producer's (possibly large) full collection.
            "depends_on": [mid],
            "output_key": f"{map_def.get('output_key', mid)}_{i}",
            "output_type": template.get("output_type", "text"),
            **({"endpoint_ids": list(template["endpoint_ids"])}
               if isinstance(template.get("endpoint_ids"), list) and template["endpoint_ids"] else {}),
        } for i, item in enumerate(items)]
        # Rewire the gather(s) fanning in on this map to depend on the children.
        for g in nodes_json:
            if g.get("kind") == "gather" and (g.get("config") or {}).get("of") == mid:
                g["depends_on"] = [c["id"] for c in child_defs] or [mid]
        nodes_json.extend(child_defs)
        for c in child_defs:
            node_statuses[c["id"]] = "pending"
            existing_ids.add(c["id"])
        node_statuses[mid] = "completed"
        _persist_map_children(session_id, plan_id, actor_id, child_defs, nodes_json, plan_sk, mid)
        # Mark the map (control) node completed — it did its work by expanding.
        _plan_table.put_item(Item={
            **_node_key(session_id, plan_id, mid), "plan_id": plan_id, "node": mid,
            "status": "completed", "updated_at": datetime.now(timezone.utc).isoformat(),
            "ttl": int(datetime.now(timezone.utc).timestamp()) + 86400,
        })
        logger.info("Plan %s: expanded map %s into %d child node(s)", plan_id, mid, len(child_defs))
        changed = True
    return changed


def _run_gather(session_id, plan_id, gather_def):
    """Phase 2: mechanically aggregate a ready ``gather`` node's children (no LLM).

    Reads each child's output from the manifest and writes the gather's output as a LIST of
    child outputs — never inlining them into an LLM turn (the non-concentration contract, so
    the fan-in cannot re-hit the per-node output cap). Marks the gather completed.
    """
    gid = gather_def["id"]
    now = datetime.now(timezone.utc).isoformat()
    outputs = _read_manifest_outputs(session_id, plan_id)
    collected = [{"node": dep, "output": (outputs.get(dep) or {}).get("value")}
                 for dep in gather_def.get("depends_on", [])]
    # Persist the aggregate into the manifest as this node's structured value.
    if SKILLS_S3_BUCKET:
        try:
            mkey = f"plans/{session_id}/{plan_id}/manifest.json"
            try:
                m = json.loads(_s3.get_object(Bucket=SKILLS_S3_BUCKET, Key=mkey)["Body"].read().decode("utf-8"))
            except Exception:
                m = {"plan_id": plan_id, "outputs": {}}
            m.setdefault("outputs", {})[gid] = {"status": "completed", "output_type": "json",
                                                "value": {"items": collected}, "completed_at": now}
            m["updated_at"] = now
            _s3.put_object(Bucket=SKILLS_S3_BUCKET, Key=mkey,
                           Body=json.dumps(m, default=str).encode("utf-8"), ContentType="application/json")
        except Exception:
            logger.exception("Plan %s: gather %s manifest write failed", plan_id, gid)
    skey = f"plans/{session_id}/{plan_id}/{gid}/output"
    try:
        _s3.put_object(Bucket=SKILLS_S3_BUCKET, Key=skey,
                       Body=json.dumps({"items": collected}, default=str).encode("utf-8"),
                       ContentType="application/json")
    except Exception:
        logger.debug("Plan %s: gather %s output write failed", plan_id, gid)
    _plan_table.put_item(Item={
        **_node_key(session_id, plan_id, gid), "plan_id": plan_id, "node": gid,
        "status": "completed",
        "result_s3_key": skey, "updated_at": now,
        "ttl": int(datetime.now(timezone.utc).timestamp()) + 86400,
    })
    try:
        _job_table.update_item(
            Key={"jobId": f"{plan_id}#{gid}"},
            UpdateExpression="SET #st = :s, terminalAt = :now, updatedAt = :now REMOVE pollPartition",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":s": "SUCCEEDED", ":now": now},
        )
    except Exception:
        logger.debug("Plan %s: gather %s job registry update failed", plan_id, gid)
    logger.info("Plan %s: gather %s aggregated %d child output(s)", plan_id, gid, len(collected))


def _mark_node_failed(session_id, plan_id, node_id, reason, only_if_running=False):
    """Mark a node as failed in both Plan State and Job Registry.

    ``only_if_running`` (the watchdog) makes the plan-state write conditional on the row still
    being ``running``; when it is not, nothing is written and False is returned. True otherwise.
    """
    now = datetime.now(timezone.utc).isoformat()
    try:
        names = {"#st": "status", "#err": "error", "#ttl": "ttl"}
        values = {":s": "failed", ":now": now, ":reason": reason, ":ttl": _ttl_epoch()}
        extra = {}
        if only_if_running:
            values[":running"] = "running"
            extra["ConditionExpression"] = "#st = :running"
        _plan_table.update_item(
            Key=_node_key(session_id, plan_id, node_id),
            UpdateExpression=("SET #st = :s, updated_at = :now, #err = :reason, #ttl = :ttl"
                              + _identity_set(plan_id, node_id, names, values)),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            **extra,
        )
    except Exception as exc:
        if only_if_running and _is_conditional_check_failure(exc):
            logger.info("Plan %s: node %s reported in before the watchdog fired; left as is",
                        plan_id, node_id)
            return False
        logger.exception("Plan %s: failed to mark node %s as failed", plan_id, node_id)
    # D-08: failed is a terminal node transition (genuine progress) — slide the PLAN row's ttl.
    _slide_plan_ttl(session_id, plan_id)
    try:
        _job_table.update_item(
            Key={"jobId": f"{plan_id}#{node_id}"},
            # Mirror the failure reason onto the job-registry row too (not just plan-state):
            # the Jobs panel / failed-notification reads the job row, and without this its
            # error was null so a failed-job click surfaced nothing. Field name is `lastError`
            # — the canonical attribute the poller (JobRegistry.bump_error) writes and the proxy
            # projects to job.lastError → the notification's error. Bounded like the plan-state
            # copy so a verbose reason can't bloat the row.
            UpdateExpression=("SET #st = :s, terminalAt = :now, updatedAt = :now, lastError = :reason "
                              "REMOVE pollPartition"),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":s": "FAILED", ":now": now,
                                       ":reason": (reason or "node failed")[:2000]},
        )
    except Exception:
        logger.debug("Job registry terminal update failed for %s#%s", plan_id, node_id)
    return True


def _is_conditional_check_failure(exc) -> bool:
    """A DynamoDB ConditionalCheckFailed, matched by name/code (test doubles raise their own)."""
    code = ((getattr(exc, "response", None) or {}).get("Error") or {}).get("Code", "")
    return "ConditionalCheckFailed" in type(exc).__name__ or code == "ConditionalCheckFailedException"


def _read_s3_json(bucket, key, max_bytes=None):
    """``(parsed, size)`` for a small JSON object, or ``(None, size)`` if unusable.

    Size-bounded on purpose: findings are inlined into an artifact meant to be
    fetched and rendered in one hop. The caller passes the REMAINING budget, so one
    verbose node cannot spend the whole artifact -- reading three files per node
    without a ceiling produced a 340KB object.
    """
    limit = result_resolution.MAX_INLINE_FINDINGS_BYTES if max_bytes is None else max_bytes
    head = _s3.head_object(Bucket=bucket, Key=key)
    size = head.get("ContentLength", 0) or 0
    if size > limit:
        return None, size
    body = _s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    try:
        return json.loads(body.decode("utf-8")), size
    except Exception:
        return None, size


def _list_output_prefix(bucket, prefix):
    """List real object keys under an output prefix, so the aggregate carries actual files
    instead of a bare pointer (F4a). Bounded and best-effort -- a listing failure degrades to
    the pointer, never fails the aggregate.
    """
    keys = []
    try:
        resp = _s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=100)
        for obj in resp.get("Contents", []) or []:
            k = obj.get("Key")
            if k and not k.endswith("/"):
                keys.append(k)
    except Exception:
        logger.exception("aggregate: prefix list failed for s3://%s/%s", bucket, prefix)
    return keys


def _resolve_queue_from_registry(handle):
    """A queue handle's outputs, read from the job row the POLLER already resolved.

    Deliberately not a gateway re-invocation. The polling worker has already
    driven this job to terminal and recorded ``artifactUrls`` on the row, keyed by
    the queue's own request id -- so reading the row reuses finished work, needs no
    delegation grant, and cannot produce the 404/405 endpoint guessing that left
    one run reporting a video fal had already made as "unreachable".

    Returns ``{}`` when the job is not terminal yet, which the resolver renders as
    ``pending`` rather than an error.
    """
    job_id = (handle or {}).get("job_id") or ""
    if not job_id:
        return {}
    try:
        row = _job_table.get_item(Key={"jobId": job_id}).get("Item") or {}
    except Exception:
        logger.exception("aggregate: job row lookup failed for %s", job_id)
        return {}
    if str(row.get("status", "")).upper() != "SUCCEEDED":
        return {}
    urls = [u for u in (row.get("artifactUrls") or []) if isinstance(u, str)]
    return {"artifactUrls": urls} if urls else {}


# --- Session output layout (asset-lineage Phase 3 (a)) -----------------------------------------------
# A copy of agent/src/projects/storage_layout.session_output_prefix: this Lambda is a separate bundle.
# Pinned to the agent helper by test_session_output_layout.py over agent/tests/fixtures/session_layout_cases.json.
_LAYOUT_SID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")
_LAYOUT_PID_RE = re.compile(r"^p[0-9a-f]{32}$")
_LAYOUT_SHARED = frozenset({"external", "durable", "imports", "uploads", "m2c"})


def _layout_output_prefix(sid, pid=None):
    """``projects/<pid>/sessions/<sid>/output/`` iff SESSION_OUTPUT_LAYOUT=project and pid/sid are valid,
    else ``artifacts/<sid>/output/``; ``artifacts/`` with no session."""
    if not sid:
        return "artifacts/"
    if ((os.environ.get("SESSION_OUTPUT_LAYOUT") or "").strip().lower() == "project"
            and isinstance(pid, str) and _LAYOUT_PID_RE.fullmatch(pid)
            and _LAYOUT_SID_RE.fullmatch(sid) and sid not in _LAYOUT_SHARED):
        return f"projects/{pid}/sessions/{sid}/output/"
    return f"artifacts/{sid}/output/"


_PLAN_PROJECT_CACHE: dict = {}


def _seed_plan_producing_project(plan_id, pid) -> None:
    """Record the PLAN row's own ``producing_project_id`` for :func:`_plan_producing_project`.

    A malformed or absent value seeds nothing (the job-registry row stays the fallback)."""
    if plan_id and isinstance(pid, str) and _LAYOUT_PID_RE.fullmatch(pid):
        _PLAN_PROJECT_CACHE[plan_id] = pid


def _plan_producing_project(plan_id) -> str:
    """The plan's producing project: the PLAN row's ``producing_project_id`` (seeded by
    :func:`_seed_plan_producing_project`), else the job-registry plan row's ``producingProjectId``
    (stamped by plan_and_execute from the dispatching turn), else "". A plan's project never changes,
    so a found value is cached for the container's life. A read failure is not cached, and neither is
    an ABSENT registry row: that row is written after the plan-state rows that trigger this lambda, so
    "not there yet" must not be remembered as "no project". Only read where the env writes the
    project layout: elsewhere the answer could not change a prefix, so no extra read is spent."""
    if not plan_id or (os.environ.get("SESSION_OUTPUT_LAYOUT") or "").strip().lower() != "project":
        return ""
    if plan_id in _PLAN_PROJECT_CACHE:
        return _PLAN_PROJECT_CACHE[plan_id]
    try:
        row = _job_table.get_item(Key={"jobId": plan_id}).get("Item")
    except Exception:  # noqa: BLE001 - no project → legacy layout
        logger.warning("plan %s: plan row unreadable; producing project unknown", plan_id)
        return ""
    if not row:
        logger.info("plan %s: job-registry plan row not written yet; producing project unknown", plan_id)
        return ""
    pid = row.get("producingProjectId")
    pid = pid if isinstance(pid, str) and _LAYOUT_PID_RE.fullmatch(pid) else ""
    _PLAN_PROJECT_CACHE[plan_id] = pid
    return pid


def _session_output_prefix(session_id: str, project_id: str = "") -> str:
    """The consistent, session-scoped OUTPUT prefix for imported M2C deliverables
    (:func:`_layout_output_prefix`). Falls back to the legacy ``artifacts/imports/`` prefix when no
    session is known."""
    safe = (session_id or "").replace("/", "_").strip()
    return _layout_output_prefix(safe, project_id) if safe else _IMPORT_PREFIX


def _make_import_artifact(session_id: str = "", project_id: str = ""):
    """Build the ``import_artifact(src_bucket, src_key, relpath) -> url|None`` injected into
    result_resolution.build_aggregate so get_plan_results returns downloadable https urls (and
    inline-playable audio) instead of un-fetchable s3:// refs.

    Copies a plan deliverable from a foreign bucket (the M2C proxy) into OUR bucket under this
    session's OUTPUT prefix (``artifacts/<session_id>/output/``) and returns a permanent CloudFront
    url. Skips objects already in our bucket; server-side copy_object (no bytes through the
    process); best-effort — never raises.
    """
    out_prefix = _session_output_prefix(session_id, project_id)

    def _import_artifact(src_bucket, src_key, relpath):
        if not SKILLS_S3_BUCKET or not ARTIFACTS_DOMAIN:
            return None
        if src_bucket == SKILLS_S3_BUCKET:
            # Already in our bucket — no copy; just form the CloudFront url for the existing key
            # so the aggregate carries a browser-fetchable https url instead of an s3:// ref.
            return f"https://{ARTIFACTS_DOMAIN}/{src_key}"
        import mimetypes
        dest_key = f"{out_prefix}{(relpath or '').lstrip('/')}"
        ct = mimetypes.guess_type(dest_key)[0]
        try:
            kwargs = {"Bucket": SKILLS_S3_BUCKET, "Key": dest_key,
                      "CopySource": {"Bucket": src_bucket, "Key": src_key}}
            if ct:
                kwargs["ContentType"] = ct
                kwargs["MetadataDirective"] = "REPLACE"
            _s3.copy_object(**kwargs)
        except Exception:
            logger.exception("auto-import: copy failed s3://%s/%s -> s3://%s/%s",
                             src_bucket, src_key, SKILLS_S3_BUCKET, dest_key)
            return None
        return f"https://{ARTIFACTS_DOMAIN}/{dest_key}"

    return _import_artifact


def _entry_has_result(entry) -> bool:
    """True if a manifest node entry already carries a resolved result (a terminal status
    plus a value or artifact urls). Used to decide whether we must recover the node's output
    from its durable per-node file because the shared manifest lost it to the write race."""
    if not isinstance(entry, dict):
        return False
    st = str(entry.get("status") or "").lower()
    if st in ("completed", "succeeded"):
        if entry.get("value") is not None or entry.get("artifact_urls"):
            return True
    if entry.get("error"):
        return True  # a recorded failure is a resolved terminal outcome, not a lost node
    return False


def _recover_node_output(session_id: str, plan_id: str, node_id: str):
    """Recover a node's result from its DURABLE per-node output file when the shared manifest
    lost it.

    Each completed gateway node writes its raw result to ``plans/<session>/<plan>/<node>/output``
    via an UNCONDITIONAL per-node put_object (no cross-node race), AND read-modify-writes it into
    the single shared ``manifest.json`` (RACY: parallel nodes clobber each other, so the manifest
    keeps only the last writers). The aggregate reads the manifest, so a clobbered-but-completed
    node showed ``pending`` even though it succeeded. This reads the per-node file — the reliable
    record — and returns a resolve_node-shaped entry. Best-effort: None on any miss/parse error.
    """
    if not SKILLS_S3_BUCKET:
        return None
    key = f"plans/{session_id}/{plan_id}/{node_id}/output"
    try:
        body = _s3.get_object(Bucket=SKILLS_S3_BUCKET, Key=key)["Body"].read().decode("utf-8")
    except Exception:  # noqa: BLE001 - genuinely absent / unreadable
        return None
    try:
        value = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        value = {"raw_text": body}
    return {"status": "completed", "output_type": "json", "value": value}


_AGGREGATE_FAILED_STATUSES = ("failed", "skipped", "aborted", "timed_out")


def _terminal_failure_overlay(nid: str, node_statuses, node_states, entry):
    """``{**entry, status, error}`` when plan-state holds a terminal non-success for ``nid``,
    else None.

    The manifest is the OUTPUT record, not the status record: a node whose fal job 422'd keeps its
    ``{request_id}`` handle there (``status: completed``) while plan-state says ``failed`` with the
    error. Reading only the manifest published those nodes as ``completed`` / kind ``pending``
    (staging dcd11946), so the orchestrator diagnosed a 422 as "timing". ``node_statuses`` is the
    settle pass's map (it carries skips decided THIS pass); ``node_states`` supplies the error,
    which wins over any older one in the manifest.
    """
    row = (node_states or {}).get(nid) or {}
    status = str((node_statuses or {}).get(nid) or row.get("status") or "").lower()
    if status not in _AGGREGATE_FAILED_STATUSES:
        return None
    base = entry if isinstance(entry, dict) else {}
    row_error = row.get("error") if str(row.get("status") or "").lower() == status else None
    error = (row_error or base.get("error")
             or ("upstream dependency failed" if status == "skipped" else f"node {status}"))
    return {**base, "status": status, "error": str(error)}


def _assemble_aggregate_nodes(session_id: str, plan_id: str, nodes_json, manifest: dict,
                              node_statuses=None, node_states=None) -> dict:
    """Build the {node_id: entry} map the aggregate resolves, recovering any node the shared
    manifest lost to its concurrent read-modify-write race from the durable per-node output file.

    Without recovery, a completed node whose manifest write was clobbered by a parallel node's
    write resolved to ``pending`` — so a fully-finished plan published ``SUCCEEDED`` with several
    nodes shown as still-running (the "notification came back but most jobs not done" bug).

    Plan-state's terminal ``failed``/``skipped`` status and ``error`` are overlaid last (see
    :func:`_terminal_failure_overlay`), so result.json never reports a failed node as completed."""
    outputs = (manifest.get("outputs") or {}) if isinstance(manifest, dict) else {}
    declared = (manifest.get("nodes") or {}) if isinstance(manifest, dict) else {}
    nodes: dict = {}
    for node_def in nodes_json or []:
        nid = node_def.get("id") if isinstance(node_def, dict) else None
        if not nid:
            continue
        entry = outputs.get(nid) or declared.get(nid) or {}
        failed = _terminal_failure_overlay(nid, node_statuses, node_states, entry)
        if failed is not None:
            nodes[nid] = failed
            continue
        if not _entry_has_result(entry):
            recovered = _recover_node_output(session_id, plan_id, nid)
            if recovered is not None:
                entry = recovered
        nodes[nid] = entry
    # Preserve any manifest outputs for nodes not in nodes_json (defensive; unchanged behaviour).
    for nid, entry in outputs.items():
        nodes.setdefault(nid, entry)
    return nodes


def _publish_plan_result(session_id, plan_id, status, nodes_json, node_statuses=None, node_states=None):
    """Write the plan's single resolved result artifact and return its key.

    Runs at settle for SUCCEEDED *and* FAILED. A run where three of four nodes
    worked has three results worth showing, and the failure is the part the user
    most needs -- previously a failed plan produced nothing readable at all.

    Never raises: the plan's terminal status must be recorded even if aggregation
    fails, so a failure here degrades to "no result artifact" rather than leaving
    the plan stuck non-terminal.
    """
    if not SKILLS_S3_BUCKET:
        return ""
    try:
        manifest_key = f"plans/{session_id}/{plan_id}/manifest.json"
        manifest = json.loads(
            _s3.get_object(Bucket=SKILLS_S3_BUCKET, Key=manifest_key)["Body"].read().decode("utf-8")
        )
    except Exception:
        logger.warning("aggregate: no manifest for plan %s; skipping result artifact", plan_id)
        return ""

    nodes = _assemble_aggregate_nodes(session_id, plan_id, nodes_json, manifest,
                                      node_statuses=node_statuses, node_states=node_states)

    try:
        payload = result_resolution.aggregate_json(
            plan_id=plan_id,
            status=status,
            nodes=nodes,
            display_name=manifest.get("display_name", ""),
            goal=manifest.get("goal", ""),
            read_s3_json=_read_s3_json,
            resolve_queue=_resolve_queue_from_registry,
            list_prefix=_list_output_prefix,
            import_artifact=_make_import_artifact(session_id, _plan_producing_project(plan_id)),
        )
    except Exception:
        logger.exception("aggregate: build failed for plan %s", plan_id)
        return ""

    # Stamp each node's TOOL and DEPENDS_ON onto the aggregate.
    #
    # tool        -> titles the render tab after the work ("Face Recognition") instead of the
    #                plan's bookkeeping node_id ("t1"), AND selects the render view
    #                deterministically (render_agent.select_render_skill_by_tool).
    # depends_on  -> lets the render hook tell a DELIVERABLE from PLUMBING
    #                (hooks.provider.intermediate_node_ids): a proxy transcode that exists only
    #                to feed three analyses must not become its own tab. Without this field the
    #                filter has no edges to reason about and correctly does nothing, so the
    #                aggregate is where the graph has to be published.
    #
    # Both live on the node DEF (`nodes_json`); the aggregate itself carries neither.
    try:
        defs_by_node = {
            nd.get("id"): nd for nd in (nodes_json or []) if isinstance(nd, dict)
        }
        obj = json.loads(payload)
        for n in obj.get("nodes", []):
            node_def = defs_by_node.get(n.get("node_id")) or {}
            tool = (node_def.get("tools") or [""])[0]
            if tool:
                n["tool"] = tool
            deps = node_def.get("depends_on")
            if isinstance(deps, list):
                # Written even when empty: an explicit [] tells the consumer "this node has no
                # dependencies", which is different from "we don't know the graph" (field absent).
                n["depends_on"] = [str(d) for d in deps]
        payload = json.dumps(obj)
    except Exception:
        logger.warning("aggregate: could not stamp node tools/deps for plan %s", plan_id)

    result_key = f"plans/{session_id}/{plan_id}/result.json"
    try:
        _s3.put_object(
            Bucket=SKILLS_S3_BUCKET,
            Key=result_key,
            Body=payload.encode("utf-8"),
            ContentType="application/json",
        )
    except Exception:
        logger.exception("aggregate: could not write %s", result_key)
        return ""
    logger.info("aggregate: wrote %s (%d bytes)", result_key, len(payload))
    return result_key


def _mark_run_complete(session_id, plan_id, nodes_json, node_statuses, node_states=None):
    """Mark the parent run as complete in the Job Registry.

    Also publishes the plan's resolved result artifact and stamps its key on the
    row, so the changefeed notification this write triggers carries a pointer to
    ONE fetchable result instead of leaving the orchestrator to re-derive it. The
    artifact is written BEFORE the status write for exactly that reason: the
    notification must never point at a key that does not exist yet.
    """
    all_succeeded = all(
        node_statuses.get(n["id"], "").lower() in ("succeeded", "completed")
        for n in nodes_json
    )
    status = "SUCCEEDED" if all_succeeded else "FAILED"
    now = datetime.now(timezone.utc).isoformat()

    result_key = _publish_plan_result(session_id, plan_id, status, nodes_json,
                                      node_statuses=node_statuses, node_states=node_states)

    try:
        expr = "SET #st = :s, terminalAt = :t, updatedAt = :now"
        values = {":s": status, ":t": now, ":now": now}
        if result_key:
            expr += ", resultKey = :rk"
            values[":rk"] = result_key
        expr += " REMOVE pollPartition"
        _job_table.update_item(
            Key={"jobId": plan_id},
            UpdateExpression=expr,
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues=values,
        )
        logger.info("Run %s marked %s (resultKey=%s)", plan_id, status, result_key or "none")
    except Exception:
        logger.exception("Failed to mark run %s complete", plan_id)

    # Keep the plan-state PLAN row consistent with the run outcome. That row —
    # (session_id, node_id="PLAN#<plan_id>", legacy "PLAN") — is what the flow view reads; leaving it "pending"
    # after the run settled is why a fully-completed plan still showed a node/flow
    # "running" (session 2221f53d). Terminal status must land in BOTH stores, exactly as
    # _mark_node_failed / _mark_node_skipped write each node to both. Its own try so a
    # job-registry failure never skips this write (and vice versa).
    plan_row_status = "completed" if all_succeeded else "failed"
    try:
        _plan_table.update_item(
            Key=_plan_key(session_id, plan_id),
            UpdateExpression="SET #st = :s, updated_at = :now, #ttl = :ttl",
            ExpressionAttributeNames={"#st": "status", "#ttl": "ttl"},
            ExpressionAttributeValues={":s": plan_row_status, ":now": now, ":ttl": _ttl_epoch()},
        )
    except Exception:
        logger.exception("Plan %s: failed to mark PLAN row %s", plan_id, plan_row_status)
    _unwatch_plan(session_id, plan_id)
