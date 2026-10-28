"""Stable Project ids for preload (design Part 1: ``pid = uuid5(namespace, name)``).

A Project id is ``p`` + the 32 lowercase hex digits of a UUID (``projects.ids.PROJECT_ID_PATTERN``).
The design says "UUID v4"; the load-bearing rule in code is ``^p[0-9a-f]{32}$`` (``projects/ids.py``),
which ``uuid5(...).hex`` satisfies — the version nibble is irrelevant to the id pattern. Using ``uuid5``
of the project NAME makes a re-deploy derive the same id, so re-seeding is a no-op (the ``attribute_not_exists``
guard on META then rejects a duplicate write).
"""
from __future__ import annotations

import uuid

from projects.ids import is_project_id

# A fixed, arbitrary namespace for preload-derived project ids. Changing it would re-key every seeded
# project (a different id for the same name), so it is pinned here forever.
PRELOAD_NAMESPACE = uuid.UUID("7f3c9b2a-1d4e-5a6b-8c7d-0e1f2a3b4c5d")


def project_id_for_name(name: str) -> str:
    """The stable Project id for a project NAME: ``p`` + ``uuid5(namespace, name).hex`` (design Part 1)."""
    if not isinstance(name, str) or not name:
        raise ValueError("project name must be a non-empty string")
    pid = "p" + uuid.uuid5(PRELOAD_NAMESPACE, name).hex
    if not is_project_id(pid):  # defensive: the format can only drift if uuid5 or the pattern changes
        raise ValueError(f"derived project id is not well-formed: {pid!r}")
    return pid
