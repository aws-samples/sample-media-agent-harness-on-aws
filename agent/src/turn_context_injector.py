"""Per-turn context that keeps the conversation prefix byte-identical across a turn's model calls.

Strands' ``ContextInjector`` folds its text into the LATEST user message on every call and drops it again on the
next, so within one tool-use turn the text moves: call 1 sees it in the user's ask, call 2 in the tool results, and
the ask no longer carries it. Claude models with *preserved thinking* (Claude Opus 5.5, Sonnet 5.5, Haiku 5.5,
Fable 5.1) sign each thinking block over the exact prefix that produced it (system prompt, tools, every earlier
message) and reject a replayed block whose prefix changed: ``Invalid signature in thinking block. The block is bound
to a different conversation ... Content that preceded this block when it was created is missing from this request``.
That is enforced by default for AWS accounts created on or after 2026-08-31, so every new account (a workshop's
included) failed graph-project turns, whose turn context is always non-empty (seen 2026-11-09).

``TurnContextInjector`` renders the text ONCE per turn, on the turn's first model call, and folds that same text into
the turn's opening user message (the newest ``user`` message with no tool result) on every call of the turn. The
prefix is therefore identical on every call of the turn, and the text is still never persisted. Across turns the
previous turn's copy disappears, which only touches earlier turns, whose reasoning the model factory drops anyway
(``drop_prior_turn_reasoning``).
"""
from __future__ import annotations

import hashlib
import inspect
import json
import logging
from dataclasses import replace
from typing import Any, Callable, Optional

from strands._middleware.stages import InvokeModelStage
from strands.injection.types import InjectionContext
from strands.plugins import Plugin

from model.factory import is_turn_start

logger = logging.getLogger(__name__)


def turn_start_index(messages: list) -> int:
    """Index of the turn's opening user message (the newest ``user`` message with no tool result), or -1."""
    for index in range(len(messages) - 1, -1, -1):
        if is_turn_start(messages[index]):
            return index
    return -1


def turn_key(messages: list) -> Optional[tuple]:
    """Identifies the current turn: its opening message's position and content. None when there is none."""
    index = turn_start_index(messages)
    if index < 0:
        return None
    body = json.dumps(messages[index].get("content"), sort_keys=True, default=str)
    return index, hashlib.sha256(body.encode("utf-8")).hexdigest()


def fold_into_turn_start(messages: list, text: str) -> tuple[list, int]:
    """Append ``text`` to the turn's opening user message, returning a NEW list and how many trailing blocks of the
    LAST message are per-call content (1 when the opening message is the last message, else 0). The input list and
    its messages are never mutated; with no opening message the list is returned unchanged."""
    index = turn_start_index(messages)
    if index < 0:
        return messages, 0
    target = messages[index]
    # Some providers concatenate adjacent text blocks, which would run this onto the user's own words.
    separator = "\n\n" if target.get("content") and not text.startswith("\n") else ""
    folded: dict[str, Any] = {"role": target["role"], "content": [*target["content"], {"text": f"{separator}{text}"}]}
    if "metadata" in target:
        folded["metadata"] = target["metadata"]
    result = list(messages)
    result[index] = folded
    return result, (1 if index == len(messages) - 1 else 0)


class TurnContextInjector(Plugin):
    """Folds a per-turn text into the turn's opening user message on every model call, rendered once per turn.

    ``render_content(InjectionContext)`` returns the text (sync or async); ``None``/empty, or a raise, injects
    nothing for the whole turn (fail open). The text is frozen at the turn's first call, because text appearing,
    changing or moving between calls of one turn would change the prefix and invalidate the turn's thinking blocks.
    """

    name = "harness:turn-context"

    def __init__(self, render_content: Callable[..., Any], *, name: Optional[str] = None) -> None:
        self.name = name or type(self).name
        self._render_content = render_content
        self._key: Optional[tuple] = None
        self._text = ""
        super().__init__()

    def init_agent(self, agent: Any) -> None:
        agent._middleware_registry.add_middleware(InvokeModelStage.Input, self._handler)

    async def _render(self, context: Any) -> str:
        injection_context = InjectionContext(messages=list(context.messages), state=context.agent.state,
                                             agent=context.agent)
        try:
            text = self._render_content(injection_context)
            if inspect.isawaitable(text):
                text = await text
        except Exception as error:  # noqa: BLE001 - fail open: a bad callback must not abort the model call
            logger.warning("reason=<%s> | turn context render raised | injecting nothing this turn", error)
            return ""
        return text if isinstance(text, str) and text.strip() else ""

    async def _handler(self, context: Any) -> Any:
        key = turn_key(context.messages)
        if key is None:
            return context
        if key != self._key:   # the turn's first call: render once, then reuse the same text for the whole turn
            self._key, self._text = key, await self._render(context)
        if not self._text:
            return context
        folded, appended = fold_into_turn_start(context.messages, self._text)
        return replace(context, messages=folded, dynamic_trailing_blocks=context.dynamic_trailing_blocks + appended)
