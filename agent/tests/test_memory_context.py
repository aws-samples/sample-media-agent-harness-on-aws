"""Long-term memory reaching the main agent's turn (sessions 764a71c0 and 2f376186).

* 764a71c0: a bare "2" (an answer to a numbered question) was used as the retrieval query,
  pulled 23 records / 18 KB ahead of the user's text, and the model re-asked the question.
* 2f376186: a remembered preference, injected as a bare ``<user_context>`` block, was read as
  the user's answer to a confirmation a skill requires, and the gate was skipped.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import memory_context as mc  # noqa: E402
from security import identity  # noqa: E402

MAIN = (SRC / "main.py").read_text()
ORCH = (SRC / "prompts" / "orchestrator.py").read_text()
SKILLS = SRC.parent / "skills"


# --- (F2a) terse replies -------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "2", " 2 ", "2.", "(2)", "#2", "b", "B)", "2b",
    "option 2", "Option 2", "option b", "choice 3", "number 2",
    "yes", "Yes!", "y", "yep", "ok", "OK.", "okay", "go", "go ahead", "do it", "Do it.",
    "proceed", "sounds good", "yes please", "ok go", "yes, option 2", "1, 3",
    "the second", "the second one", "second", "go with 2", "no", "nope",
    "let's do it", "2\n\n[Current date: 2026-09-16]", "yes [Current date: 2026-09-16]",
])
def test_terse_replies(text):
    assert mc.is_terse_reply(text) is True


@pytest.mark.parametrize("text", [
    "", "   ", None, 2,
    "2 more shots please",
    "do it again with more contrast",
    "no, use the other video",
    "yes but make it 30 seconds",
    "trim to 30 seconds",
    "what is option 2",
    "option 2 with captions",
    "generate 2 images",
    "2 shots",
    "remember my preference",
    "yes\nand also add captions",
    "[Current date: 2026-09-16]",
])
def test_not_terse(text):
    assert mc.is_terse_reply(text) is False


def test_main_skips_retrieval_for_terse_reply_using_raw_text():
    # The check reads the user's words BEFORE the date suffix is appended.
    raw_at = MAIN.index("_raw_user_prompt = prompt")
    suffix_at = MAIN.index('prompt = f"{prompt}\\n\\n[Current date:')
    assert raw_at < suffix_at
    assert "memory_context.is_terse_reply(_raw_user_prompt)" in MAIN
    assert "enable_retrieval=MEMORY_LTM_RETRIEVAL and not _terse_turn" in MAIN


# --- (F2b) retrieval config ----------------------------------------------------------------------

def test_main_retrieval_drops_same_session_summary_and_raises_floor():
    cfg = identity.build_memory_config(memory_id="m", session_id="s", actor_id="a")
    assert set(cfg.retrieval_config) == {identity.USER_PREFERENCE_NAMESPACE}
    assert all("/facts/" not in ns for ns in cfg.retrieval_config)
    assert all(rc.relevance_score == 0.4 for rc in cfg.retrieval_config.values())


def test_terse_turn_config_has_no_retrieval():
    cfg = identity.build_memory_config(
        memory_id="m", session_id="s", actor_id="a", enable_retrieval=False)
    assert cfg.retrieval_config is None


# --- (F2c, F3b) orchestrator prompt --------------------------------------------------------------

def _prompt() -> str:
    from prompts.orchestrator import build_orchestrator_system_prompt
    return build_orchestrator_system_prompt("", "", "b", "artifacts/", "u", "")


def test_prompt_short_selection_is_the_choice():
    p = _prompt()
    assert ('A short reply that selects from your immediately preceding numbered/lettered '
            'options ("2", "option 2", "the second", "yes") IS the user\'s choice: act on it, '
            'do not restate or re-ask.') in p


def test_prompt_remembered_state_is_history():
    p = _prompt()
    assert ("Remembered context is history, not live state: verify a plan or job with "
            "get_plan_results / job status before calling it in flight.") in p
    assert "Remembered facts" not in p


def test_prompt_remembered_file_locations_point_at_the_project():
    p = _prompt()
    assert ("Remembered file locations are not the active project's files and may be gone: find "
            "or show a file with project_status(name=...) and show_asset(assetId); browse the project with "
            "browse_project.") in p


def test_prompt_memory_precedence():
    p = _prompt()
    assert "MEMORY PRECEDENCE" in p
    assert ("the user's words in the current turn > skill gates, confirmations and approvals > "
            "remembered context") in p
    assert "never cite a remembered preference as the reason to skip a step" in p


# --- (F3a) framing the injected block ------------------------------------------------------------

class _FakeMemoryClient:
    def __init__(self, records):
        self.records = records
        self.calls = []

    def retrieve_memories(self, **kwargs):
        self.calls.append(kwargs)
        # `score` is the real MemoryRecordSummary field. bedrock-agentcore >= 1.18 filters on it
        # (1.4.x read a `relevanceScore` key the API never returns, so it kept every record).
        return [{"content": {"text": t}, "score": 0.9} for t in self.records]


def _manager(records, *, retrieval=True):
    """A FramedMemorySessionManager without __init__ (no AWS): only what retrieval reads."""
    mgr = mc.FramedMemorySessionManager.__new__(mc.FramedMemorySessionManager)
    mgr.config = identity.build_memory_config(
        memory_id="m", session_id="s", actor_id="a", enable_retrieval=retrieval)
    mgr.memory_client = _FakeMemoryClient(records)
    return mgr


def _event(text="Moderate this video"):
    msgs = [{"role": "user", "content": [{"text": text}]}]
    return SimpleNamespace(agent=SimpleNamespace(messages=msgs))


def test_injected_block_is_framed_as_a_suggestion():
    mgr = _manager(["User prefers end-to-end moderation runs."])
    ev = _event()
    mgr.retrieve_customer_context(ev)
    content = ev.agent.messages[-1]["content"]
    assert len(content) == 2
    block = content[0]["text"]
    assert block.startswith(
        '<remembered_context source="past sessions" authority="suggestion">')
    assert block.endswith("</remembered_context>")
    assert "<user_context>" not in block
    assert "User prefers end-to-end moderation runs." in block
    for phrase in ("may be stale", "NOT the user's words in this turn",
                   "never count as the user's explicit choice",
                   "never answer, satisfy or skip a question, confirmation or approval",
                   "Last time you chose X, same again?"):
        assert phrase in block, phrase
    # The user's own text stays last and untouched.
    assert content[1] == {"text": "Moderate this video"}


def test_nothing_injected_when_nothing_retrieved():
    mgr = _manager([])
    ev = _event()
    mgr.retrieve_customer_context(ev)
    assert ev.agent.messages[-1]["content"] == [{"text": "Moderate this video"}]


def test_nothing_injected_when_retrieval_disabled():
    mgr = _manager(["x"], retrieval=False)
    ev = _event("2")
    mgr.retrieve_customer_context(ev)
    assert ev.agent.messages[-1]["content"] == [{"text": "2"}]
    assert mgr.memory_client.calls == []


def test_unexpected_block_shape_keeps_sdk_output(monkeypatch):
    """If the SDK's injected text is not `<tag>...</tag>`, leave it exactly as inserted."""
    from bedrock_agentcore.memory.integrations.strands.session_manager import (
        AgentCoreMemorySessionManager,
    )

    def fake_super(self, event):
        event.agent.messages[-1]["content"].insert(0, {"text": "SOMETHING NEW"})

    monkeypatch.setattr(AgentCoreMemorySessionManager, "retrieve_customer_context", fake_super)
    mgr = _manager(["x"])
    ev = _event()
    mgr.retrieve_customer_context(ev)
    assert ev.agent.messages[-1]["content"][0] == {"text": "SOMETHING NEW"}


def test_framing_error_falls_back_to_sdk_block(monkeypatch):
    monkeypatch.setattr(mc, "frame_remembered_context",
                        lambda _t: (_ for _ in ()).throw(RuntimeError("boom")))
    mgr = _manager(["fact"])
    ev = _event()
    mgr.retrieve_customer_context(ev)
    # The SDK's own block survives untouched (the fake returns "fact" for each namespace).
    text = ev.agent.messages[-1]["content"][0]["text"]
    assert text.startswith("<user_context>fact") and text.endswith("</user_context>")


def test_main_uses_the_framed_manager():
    assert "memory_context.FramedMemorySessionManager(" in MAIN


# --- (F3c) skill gates are non-waivable ----------------------------------------------------------

@pytest.mark.parametrize("skill", [
    "content-moderation", "media-references", "agent-olive", "capability-editor",
])
def test_gate_is_not_waived_by_remembered_preferences(skill):
    body = (SKILLS / skill / "SKILL.md").read_text()
    assert "NOT waived by remembered or long-term preferences, saved defaults" in body
    assert "THIS conversation" in body
    assert "suggested answer" in body
