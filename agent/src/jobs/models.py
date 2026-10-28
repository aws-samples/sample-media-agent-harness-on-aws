"""Async job tracking — shared data models.

Defines the typed shapes used by the Job_Detector, the JobRegistry,
and the per-tool job-tracking wrap (see
:mod:`agent.src.jobs.strands_tool_wrap`). The DDB persistence layer
(registry.py) serializes :class:`JobRecord` into the camelCase
attribute names declared in design.md (``job_id`` -> ``jobId``,
``actor_id`` -> ``actorId``, etc.); this module keeps Python-style
snake_case on the dataclass.

See ``.kiro/specs/async-job-tracking/design.md`` ("Data Models" and
"Implementation idioms (mirror state_store.py)") for the canonical
field list and DDB mapping.

Requirements: 3.1, 5.1
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

# Status values are a closed set; using a Literal lets call sites
# narrow the type and lets static analysis catch typos at the
# registration / polling boundaries.
JobStatus = Literal[
    "PENDING",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "TIMED_OUT",
    "ABORTED",
]

# Active vs. terminal partitions. The polling worker only invokes
# status tools for jobs whose current status is in ACTIVE_STATUSES
# (R4.1), and the registry stamps ``terminal_at`` + ``ttl`` only on
# transitions into TERMINAL_STATUSES (R4.3, R5.3).
ACTIVE_STATUSES: frozenset[str] = frozenset({"PENDING", "RUNNING"})
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"}
)


@dataclass(frozen=True)
class JobTrio:
    """A detected start/status/results triplet hosted by one gateway.

    Produced by :mod:`agent.src.jobs.detector` from an MCP tool list.
    Cached on the gateway connection alongside the existing tool
    metadata so the per-tool job-tracking wrap can look up trio
    membership in O(1) when wrapping a freshly-listed tool set.
    """

    trio_id: str
    gateway_id: str
    start_tool: str
    status_tool: str
    results_tool: str


@dataclass(frozen=True)
class JobRecord:
    """A single Async_Job tracked by the Job_Registry.

    Field names match the keyword arguments used by the per-tool
    job-tracking wrap and the ``JobRegistry.register`` call sites in
    design.md. The DDB persistence layer maps these snake_case
    attributes to the camelCase column names declared on the table
    schema.

    The trailing fields (``updated_at``, ``terminal_at``,
    ``last_error``, ``consecutive_errors``) carry defaults so a fresh
    registration only needs the identity-and-trio fields plus the
    initial ``status`` and ``start_at``; the registry and polling
    worker fill in the rest as the job advances.
    """

    job_id: str
    actor_id: str
    session_id: str
    trio_id: str
    gateway_id: str
    status_tool: str
    results_tool: str
    status: JobStatus
    start_at: str
    updated_at: str = ""
    terminal_at: Optional[str] = None
    last_error: Optional[str] = None
    consecutive_errors: int = 0
    has_renderable_artifacts: bool = False
    run_id: Optional[str] = None
    # The user request that caused this job. See jobs.context.current_turn_id.
    turn_id: Optional[str] = None
    # The gateway that actually hosts ``results_tool``. ``gateway_id`` may
    # be overridden to the SFN sentinel (see
    # ``jobs.interceptor._resolve_gateway_id_for_job``) so the polling
    # worker takes the DescribeExecution status path — but SFN has no
    # equivalent for fetching a tool's rich results payload, so any
    # results fetch (e.g. the Plan Chainer's direct results-fetch) must
    # use the trio's real gateway, not the status-routing sentinel.
    # Defaults to ``gateway_id`` for callers that don't set it explicitly
    # (e.g. trios with no results tool at all).
    results_gateway_id: Optional[str] = None
    # Signed capability letting the polling worker prove WHOSE job it is polling
    # on a user_jwt gateway, where no user is present at poll time and a machine
    # token carries no ``sub``. Scoped to this job and expiring with it; not a
    # credential to the upstream service, which the gateway authenticates itself.
    delegation_grant: Optional[str] = None
    # Queue endpoint this job was submitted to. The status call needs it alongside the
    # request id -- fal's check_job rejects a call without it -- and it is not
    # derivable from the job id.
    endpoint_id: Optional[str] = None
    # DAG node id (the ``DAGNode.id`` this job record represents). Empty
    # for the parent "plan" job record and for standalone (non-plan) jobs.
    # Lets the Activity Panel reconstruct the plan's node identity without
    # parsing ``trio_id`` (which is a display-oriented "node|<id>" string).
    node_id: Optional[str] = None
    # DAG edges: the node ids this job's node depends on (``DAGNode.
    # depends_on``). Empty for the parent "plan" record and for standalone
    # jobs. Lets the Activity Panel render the real dependency graph
    # instead of a flat, timestamp-ordered list.
    depends_on: list[str] = field(default_factory=list)
    # The SOURCE media this job was invoked on (``s3://`` video/audio the analysis
    # runs over). Captured once at registration from the start-tool input / node
    # prompt, so the ASYNC completion render — which fires minutes later in another
    # container, from a job output that does not echo its own input — can overlay the
    # analysis (face boxes, shot markers, speaker lanes) on the media it describes.
    # Uniform across every media analysis; empty for non-media / coordinator rows.
    source_media: list[str] = field(default_factory=list)
    # projects.lineageEdges: EVERY location / external URL the start tool was given (credential-free), not only the
    # playable media `source_media` keeps for the render overlay (an `.mxf`, an image, a JSON), so the job's outputs
    # register made from what the job actually read. Empty with the flag off.
    lineage_inputs: list[str] = field(default_factory=list)
    # The Producing_Turn's Active_Project (ingestion Req 9.3): media this job writes is registered into
    # THIS Project when the job completes, even if the session's Active_Project changed meanwhile. Stamped at
    # registration from the turn context; ``None`` outside a Projects turn (nothing is registered then).
    producing_project_id: Optional[str] = None
    # The plan node that submitted this gateway job (``planNodeId``), set by the job-tracking wrap
    # inside a plan node. The changefeed bridge maps the finished job to its node by it. Distinct
    # from ``node_id``, which marks a plan NODE's own coordinator row for the UI.
    plan_node_id: Optional[str] = None
