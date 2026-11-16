"""The model call retry of the long-running agents (main, task): Strands' default retries throttling only.

A ``ConverseStream`` that fails mid-stream with a transient Bedrock error (``internalServerException``,
``serviceUnavailableException``, ``modelStreamErrorException``) ended the turn after eight minutes of finished
recasts ("This turn failed: … internalServerException when calling the ConverseStream operation"; Meridian, project
A). Retrying the model call is safe: the tool results already in the conversation are kept, nothing is run again.
"""
from __future__ import annotations

import logging
from typing import Any

from strands.event_loop._retry import ModelRetryStrategy
from strands.types.exceptions import ModelThrottledException

logger = logging.getLogger(__name__)

# Bedrock's transient failures, as their error codes appear in the raised exception's text or response.
TRANSIENT = ("internalserverexception", "serviceunavailableexception", "modelstreamerrorexception",
             "internal server error", "service unavailable")


def is_unavailable(exception: BaseException) -> bool:
    """The model could not serve the call (throttled, or one of Bedrock's transient failures): a reason to wait and
    retry, then to fall back to another model. Anything else (a bad request, a context overflow, a guardrail) is
    about the request, and another model would only hide it."""
    if isinstance(exception, ModelThrottledException):
        return True
    code = ""
    resp = getattr(exception, "response", None)
    if isinstance(resp, dict):
        code = str((resp.get("Error") or {}).get("Code") or "")
    text = f"{code} {type(exception).__name__} {exception}".lower()
    return "throttl" in text or any(t in text for t in TRANSIENT)


class TransientModelRetry(ModelRetryStrategy):
    """Throttling (as Strands) plus Bedrock's transient errors."""

    def is_retryable(self, exception: Exception) -> bool:
        return super().is_retryable(exception) or is_unavailable(exception)


class UnavailableFallback:
    """Strands' ordered failover (``FallbackStrategy``), taken only when the model is UNAVAILABLE.

    The router asks after a failed call its retries did not claim. An unavailable model moves to the next candidate;
    any other failure declines, so that error surfaces as it would without fallback."""

    def __init__(self) -> None:
        from strands.models.routing import FallbackStrategy

        self._inner = FallbackStrategy()

    async def select(self, context: Any, **kwargs: Any) -> Any:
        attempts = getattr(context, "attempts", None) or []
        failed = attempts[-1].exception if attempts else None
        if failed is not None and not is_unavailable(failed):
            return None
        choice = await self._inner.select(context, **kwargs)
        if failed is not None and choice is not None:
            frm = getattr(getattr(attempts[-1], "candidate", None), "model", None)
            logger.warning("model fallback: %s unavailable (%s); switching to %s", _model_id(frm),
                           type(failed).__name__, _model_id(getattr(choice, "model", None)))
        return choice


def _model_id(model: Any) -> str:
    try:
        return str(model.get_config().get("model_id"))
    except Exception:  # noqa: BLE001 - a log label only
        return "?"


def retry_strategy() -> TransientModelRetry:
    return TransientModelRetry(max_attempts=4, initial_delay=4, max_delay=30)
