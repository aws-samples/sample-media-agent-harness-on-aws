"""The per-turn context injector keeps every model call's request a strict extension of the previous one.

Claude models with preserved thinking (Opus/Sonnet/Haiku 5.5) sign each thinking block over the prefix that produced
it and reject a replayed block whose prefix changed. Strands' ContextInjector folds its text into the LATEST user
message on each call, so the text moves within a tool-use turn and the turn's second call failed with "Content that
preceded this block when it was created is missing from this request" (a new AWS account, 2026-11-09).
"""
from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from turn_context_injector import (TurnContextInjector, fold_into_turn_start,  # noqa: E402
                                   turn_start_index)

ASK = {"role": "user", "content": [{"text": "start lab 3"}]}


def _assistant(i: int) -> dict:
    return {"role": "assistant", "content": [{"reasoningContent": {"reasoningText": {"text": f"r{i}",
                                                                                     "signature": f"s{i}"}}},
                                             {"toolUse": {"toolUseId": f"t{i}", "name": "show_asset", "input": {}}}]}


def _result(i: int) -> dict:
    return {"role": "user", "content": [{"toolResult": {"toolUseId": f"t{i}", "content": [{"text": "ok"}]}}]}


@dataclass
class _Ctx:   # the fields of strands' InvokeModelContext the middleware reads and replaces
    messages: list
    agent: Any = field(default_factory=lambda: SimpleNamespace(state={}))
    dynamic_trailing_blocks: int = 0


def _turn(n_calls: int, history: list | None = None) -> list[list]:
    """The message list of each model call of one turn: the ask, then one (assistant, tool result) pair per call."""
    msgs, calls = [*(history or []), ASK], []
    for i in range(n_calls):
        calls.append(list(msgs))
        msgs += [_assistant(i), _result(i)]
    return calls


def _run(handler, calls: list[list]) -> list[list]:
    return [asyncio.run(handler(_Ctx(messages=m))).messages for m in calls]


def test_every_call_extends_the_previous_request_byte_for_byte():
    renders = iter(["CTX-1", "CTX-2", "CTX-3"])   # a render that would change between calls
    inj = TurnContextInjector(lambda _c: next(renders))
    sent = _run(inj._handler, _turn(3))
    for previous, current in zip(sent, sent[1:]):
        assert current[:len(previous)] == previous   # the prefix the previous call's thinking was signed over
    assert sent[0][0]["content"][-1] == {"text": "\n\nCTX-1"}   # rendered once, at the turn's first call
    assert all(m[0] == sent[0][0] for m in sent) and all("CTX" not in str(m[1:]) for m in sent)


def test_strands_context_injector_moves_the_text_which_is_why_it_is_replaced():
    from strands.injection._message_injection import _create_injection_middleware
    sent = _run(_create_injection_middleware(lambda _c: "CTX", trigger="everyTurn"), _turn(2))
    assert sent[1][:len(sent[0])] != sent[0]   # call 2 no longer carries the text where call 1 saw it


def test_a_new_turn_renders_again_and_drops_the_old_copy():
    renders = iter(["FIRST", "SECOND"])
    inj = TurnContextInjector(lambda _c: next(renders))
    first = _run(inj._handler, _turn(2))
    history = [*first[-1], _assistant(9), {"role": "assistant", "content": [{"text": "done"}]}][:-1]
    plain = [m if m is not first[-1][0] else ASK for m in history]   # durable history holds no injected text
    nxt = {"role": "user", "content": [{"text": "next"}]}
    second = asyncio.run(inj._handler(_Ctx(messages=[*plain, _result(9), nxt]))).messages
    assert second[-1]["content"][-1] == {"text": "\n\nSECOND"} and "FIRST" not in str(second)


def test_fail_open_and_empty_render_inject_nothing():
    def boom(_c):
        raise RuntimeError("no memory")
    for render in (boom, lambda _c: "", lambda _c: None):
        calls = _turn(2)
        assert _run(TurnContextInjector(render)._handler, calls) == calls


def test_fold_targets_the_turn_start_and_reports_trailing_blocks():
    calls = _turn(2)
    folded, trailing = fold_into_turn_start(calls[0], "X")
    assert trailing == 1 and folded[0]["content"][-1] == {"text": "\n\nX"} and calls[0][0] is ASK
    folded, trailing = fold_into_turn_start(calls[1], "X")
    assert trailing == 0 and turn_start_index(calls[1]) == 0 and folded[2] == calls[1][2]
    assert fold_into_turn_start([], "X") == ([], 0)


def test_main_uses_the_turn_stable_injector():
    src = open(os.path.join(os.path.dirname(__file__), "..", "src", "main.py")).read()
    assert "from turn_context_injector import TurnContextInjector as _ContextInjector" in src
    assert 'trigger="everyTurn"' not in src
