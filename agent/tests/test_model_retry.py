"""A transient Bedrock error retries the model call (Meridian: internalServerException ended an 8-minute turn)."""
import os
import sys

from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from model.retry import TransientModelRetry, retry_strategy  # noqa: E402
from strands.types.exceptions import ModelThrottledException  # noqa: E402


def test_transient_bedrock_errors_and_throttling_are_retried_and_others_are_not():
    r = TransientModelRetry()
    assert r.is_retryable(ModelThrottledException("slow down"))
    err = ClientError({"Error": {"Code": "internalServerException", "Message": "x"}}, "ConverseStream")
    assert r.is_retryable(err)
    assert r.is_retryable(Exception("An error occurred (serviceUnavailableException) when calling ConverseStream"))
    assert r.is_retryable(Exception("modelStreamErrorException: stream broke"))
    assert not r.is_retryable(ClientError({"Error": {"Code": "ValidationException", "Message": "bad"}}, "Converse"))
    assert not r.is_retryable(ValueError("a bug"))
    assert isinstance(retry_strategy(), TransientModelRetry)


def test_the_long_running_agents_use_it():
    src = os.path.join(os.path.dirname(__file__), "..", "src")
    for name in ("main.py", "task_agent.py"):
        assert "_model_retry.retry_strategy()" in open(os.path.join(src, name)).read(), name
