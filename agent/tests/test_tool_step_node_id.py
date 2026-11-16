"""A tool call is ONE step: its running line, its live ticks and its done line share a node_id, so the chat
closes it in place (ChatCanvas replaces by node_id) instead of leaving the first "running" line frozen for the
whole turn ("Dispatching to sub-agent…" for 68–151 s in the IND403 dry run)."""
import json

import main


def test_a_tool_step_carries_its_call_id():
    step = json.loads(main._step_event("running", "Running shot segmentation · 45s", main._tool_node_id("tu-1")))
    assert step == {"type": "step", "status": "running", "label": "Running shot segmentation · 45s", "node_id": "tool-tu-1"}


def test_a_step_without_an_id_is_unchanged():
    assert json.loads(main._step_event("running", "Working...")) == {"type": "step", "status": "running", "label": "Working..."}


def test_ticks_start_after_a_short_wait_and_repeat():
    assert 0 < main.TOOL_TICK_AFTER_S <= 15 and 0 < main.TOOL_TICK_S <= 15
