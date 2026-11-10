"""What a turn asks the chat to do once it ends: open a new chat in a project and send it a first message.

``switch_project`` and ``create_project`` record the request here; ``main`` takes it when it builds the turn's
``execution_summary`` and sends it as ``open_project``. The chat acts on it only from the LIVE stream, so reopening
an old chat never opens anything again. One request per session per turn: the first one wins, so a skill (or text
injected into one) cannot bounce the user between projects within a turn.

A module dict with a lock rather than a contextvar: Strands runs each tool in its own task, and a contextvar set
there never reaches the code that builds the summary.
"""
from __future__ import annotations

import threading
from typing import Optional

PROMPT_MAX_CHARS = 2000

_LOCK = threading.Lock()
_PENDING: dict[str, dict] = {}


def request_open_project(session_id: str, project_id: str, project_name: str, prompt: str = "") -> bool:
    """Record the turn's request; ``False`` when this session already has one for this turn."""
    key = session_id or "-"
    entry = {"projectId": project_id, "projectName": project_name}
    clean = (prompt or "").strip()[:PROMPT_MAX_CHARS]
    if clean:
        entry["prompt"] = clean
    with _LOCK:
        if key in _PENDING:
            return False
        _PENDING[key] = entry
    return True


def take_open_project(session_id: str) -> Optional[dict]:
    """The request recorded this turn (removed), or ``None``."""
    with _LOCK:
        return _PENDING.pop(session_id or "-", None)
