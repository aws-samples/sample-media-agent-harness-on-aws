"""Built-in ``list_my_jobs`` Strands tool — actor-scoped job enumeration.

Companion to :func:`agent.src.jobs.get_job_tool.get_job`. Where
``get_job`` answers "what's the status of job X" given an id, this
tool answers "what jobs do I have outstanding" without one — the
agent can use it to recover the job_id list across sessions, which is
the bridge that lets a fresh chat answer questions like "is my stem
separation done?" without the user pasting an id the agent itself
emitted in a prior session.

The registry is wired by ``main.py`` at startup via :func:`set_registry`
once ``JOB_REGISTRY_TABLE`` is resolved. Until then, calls return a
``registry_unavailable`` error JSON so the agent fails loudly instead
of silently returning an empty list — that distinction matters because
an empty list is a legitimate result for a new actor, and conflating
it with "registry isn't configured" would mask the misconfiguration.

Actor scoping (R11.2): :meth:`JobRegistry.list_for_actor` queries the
``actor-status-index`` GSI on equality with the caller's bound
``actor_id``. Cross-actor jobs are simply absent from the result set
— there is no per-row visibility check because the GSI partitions on
``actorId``.

Why a separate module from ``get_job_tool``: the two tools share a
registry binding pattern but have different return shapes, different
arguments, and different system-prompt guidance. Keeping them in
their own files (one tool per module) keeps the
``set_registry`` / ``get_job`` pair from getting cluttered.

Requirements: 11.2 (actor scoping). The original async-job-tracking
spec didn't surface this tool — see ``docs/backlog.md`` (2026-06-25
"add list_my_jobs Strands tool") for the rationale.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from strands import tool

from . import context as jobs_context
from .models import ACTIVE_STATUSES, TERMINAL_STATUSES
from .registry import JobRegistry

logger = logging.getLogger(__name__)

# Module-level singleton wired by ``main.py`` once the JobRegistry is
# constructed. Kept ``None`` in dev environments without
# ``JOB_REGISTRY_TABLE`` so the tool's own ``registry_unavailable``
# error path activates cleanly.
_registry: Optional[JobRegistry] = None

# Default page cap for :func:`list_my_jobs`. Picked to match the
# sidebar's display window so the agent's view of "recent jobs"
# corresponds to what the user sees in the UI without an extra
# round-trip. Callers can override per-invocation up to ``_MAX_LIMIT``.
_DEFAULT_LIMIT: int = 20

# Hard cap on the per-call page size. The GSI query is cheap, but
# returning hundreds of records to the LLM bloats context for no
# additional decision-making value — anything beyond a few dozen is
# noise the model has to wade through.
_MAX_LIMIT: int = 100

# Closed set of status filter values, plus the two convenience
# pseudo-filters that select an active or terminal partition. Aligns
# with the ``JobStatus`` Literal in :mod:`agent.src.jobs.models` so a
# typo in the system prompt doesn't silently match nothing.
_VALID_STATUS_FILTERS: frozenset[str] = (
    ACTIVE_STATUSES | TERMINAL_STATUSES | frozenset({"ACTIVE", "TERMINAL"})
)


def set_registry(registry: Optional[JobRegistry]) -> None:
    """Bind the JobRegistry singleton used by :func:`list_my_jobs`.

    Mirrors :func:`agent.src.jobs.get_job_tool.set_registry`. Called by
    ``main.py`` during startup once the registry is built. Idempotent —
    safe to call multiple times. Passing ``None`` explicitly resets
    the binding back to "unwired".
    """
    global _registry
    _registry = registry


@tool
def list_my_jobs(
    limit: int = _DEFAULT_LIMIT,
    status_filter: Optional[str] = None,
) -> str:
    """List the current actor's recent async jobs.

    Use this when the user asks about their jobs without supplying a
    specific ``job_id`` — for example "what jobs do I have running",
    "is my stem separation done", or "show me my recent jobs". For
    a known ``job_id`` use ``get_job`` instead; that tool returns
    full per-record details and is the cheap status-only path.

    Workflow hint: when the user describes a job by content (e.g.
    "the audio job"), call this tool first to narrow the candidates,
    then call ``get_job(job_id)`` on the matching record for the
    full status + last_error payload.

    Args:
        limit: Maximum number of records to return, newest-first by
            ``updated_at``. Defaults to 20 (matches the sidebar's
            display window). Capped at 100 — the LLM doesn't benefit
            from larger pages and the context cost grows linearly.
        status_filter: Optional filter applied client-side after the
            query. Accepts any single status (``PENDING``, ``RUNNING``,
            ``SUCCEEDED``, ``FAILED``, ``TIMED_OUT``, ``ABORTED``)
            or one of the convenience aliases ``ACTIVE`` (PENDING +
            RUNNING) / ``TERMINAL`` (everything else). Omit to
            return all statuses.

    Returns:
        A JSON string. On success, an object with a ``jobs`` array
        whose rows carry ``job_id``, ``trio_id``, ``status``,
        ``start_at``, and ``updated_at`` (compact shape — full
        per-record detail is one ``get_job`` call away). Also
        includes ``count`` and ``actor_id`` for the agent's logging
        + reasoning. On failure, an ``error`` code
        (``registry_unavailable``, ``no_actor``, ``invalid_limit``,
        or ``invalid_status_filter``) plus a human-readable
        ``message``.
    """
    # ----- Argument validation. Be strict here so a malformed call
    # surfaces an error the model can correct on the next turn,
    # rather than a silently-wrong empty list.

    # ``limit`` arrives as an ``int`` per the type hint, but Strands'
    # JSON-schema layer permits the model to send a string for
    # ``integer``-typed args in some shapes (older Bedrock model
    # responses do this). Coerce defensively rather than raising on
    # the type system.
    try:
        coerced_limit = int(limit)
    except (TypeError, ValueError):
        return json.dumps(
            {
                "error": "invalid_limit",
                "message": (
                    f"limit must be an integer between 1 and {_MAX_LIMIT}; "
                    f"got {limit!r}."
                ),
            }
        )
    if coerced_limit < 1 or coerced_limit > _MAX_LIMIT:
        return json.dumps(
            {
                "error": "invalid_limit",
                "message": (
                    f"limit must be between 1 and {_MAX_LIMIT}; "
                    f"got {coerced_limit}."
                ),
            }
        )

    # Normalise ``status_filter`` to upper-case for case-insensitive
    # matching, but only after confirming we actually got a string.
    # Empty string is treated as "no filter" so the model can pass
    # an explicit empty string without error.
    normalised_filter: Optional[str] = None
    if status_filter is not None:
        if not isinstance(status_filter, str):
            return json.dumps(
                {
                    "error": "invalid_status_filter",
                    "message": (
                        "status_filter must be a string; got "
                        f"{type(status_filter).__name__}."
                    ),
                }
            )
        stripped = status_filter.strip().upper()
        if stripped:
            if stripped not in _VALID_STATUS_FILTERS:
                return json.dumps(
                    {
                        "error": "invalid_status_filter",
                        "message": (
                            "status_filter must be one of "
                            f"{sorted(_VALID_STATUS_FILTERS)}; "
                            f"got {status_filter!r}."
                        ),
                    }
                )
            normalised_filter = stripped

    # ----- Registry / actor-context resolution. Same shape as
    # ``get_job`` so the two tools fail identically when wiring is
    # missing.
    if _registry is None:
        return json.dumps(
            {
                "error": "registry_unavailable",
                "message": (
                    "Job registry is not configured for this runtime; "
                    "async job tracking is disabled."
                ),
            }
        )

    try:
        actor_id = jobs_context.current_actor_id()
    except RuntimeError as exc:
        # Same path ``get_job`` takes — a missing actor means the
        # tool ran outside a request context (e.g. unit test).
        # Surface a structured error rather than RuntimeError.
        logger.warning("list_my_jobs: actor context unset: %s", exc)
        return json.dumps(
            {
                "error": "no_actor",
                "message": (
                    "No actor context bound for the current request; "
                    "cannot resolve job ownership."
                ),
            }
        )

    # ----- Registry read. Pull a slightly larger page than ``limit``
    # when a filter is active so the post-filter page can hit the
    # caller's requested size on common cases (mostly-active or
    # mostly-terminal job sets). Capped at ``_MAX_LIMIT`` so a
    # filter never lets the caller bypass the absolute ceiling.
    fetch_size = min(coerced_limit * 4, _MAX_LIMIT) if normalised_filter else coerced_limit

    try:
        records = _registry.list_for_actor(actor_id, limit=fetch_size)
    except Exception as exc:  # noqa: BLE001 — defensive: surface unknown DDB issues
        logger.error(
            "list_my_jobs: registry.list_for_actor raised for "
            "actor_id=%r: %s",
            actor_id, exc,
        )
        return json.dumps(
            {
                "error": "registry_read_failed",
                "message": "Could not read the job registry; try again later.",
            }
        )

    # ----- Filter + project. Filtering client-side (rather than
    # adding a GSI partition) keeps the table simple — this code
    # path runs at human-interactive volumes (one chat turn at a
    # time per actor), so the cost is negligible.
    if normalised_filter == "ACTIVE":
        filtered = [r for r in records if r.status in ACTIVE_STATUSES]
    elif normalised_filter == "TERMINAL":
        filtered = [r for r in records if r.status in TERMINAL_STATUSES]
    elif normalised_filter is not None:
        filtered = [r for r in records if r.status == normalised_filter]
    else:
        filtered = records

    # Honour the caller's ``limit`` after filtering, in case the
    # over-fetch above pulled more than they asked for.
    page = filtered[:coerced_limit]

    return json.dumps(
        {
            "actor_id": actor_id,
            "count": len(page),
            "jobs": [
                {
                    "job_id": r.job_id,
                    "trio_id": r.trio_id,
                    "status": r.status,
                    "start_at": r.start_at,
                    "updated_at": r.updated_at,
                }
                for r in page
            ],
        }
    )


__all__ = ["list_my_jobs", "set_registry"]
