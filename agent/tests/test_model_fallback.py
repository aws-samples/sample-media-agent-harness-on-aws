"""Model fallback: models.json names each model's backups; the router takes them only when the model is unavailable.

A real Strands Agent with only ``converse_stream`` faked per model, so what runs is the router, the strategy and the
retry exactly as in production.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import sys

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from strands import Agent  # noqa: E402
from strands.event_loop._retry import ModelRetryStrategy  # noqa: E402
from strands.models.routing import ModelRouter  # noqa: E402

from model import defaults  # noqa: E402
from model.factory import build_model, with_fallbacks  # noqa: E402
from model.retry import UnavailableFallback, is_unavailable  # noqa: E402

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
MODELS = json.loads((SRC / "model" / "models.json").read_text())


def _err(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "ConverseStream")


def _ok_stream(text: str):
    return {"stream": [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"delta": {"text": text}, "contentBlockIndex": 0}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}, "metrics": {"latencyMs": 1}}},
    ]}


def _fake(router_or_model, behaviour: dict) -> list:
    """Fake each candidate's converse_stream: ``behaviour[model_id]`` is an exception to raise or a text to answer."""
    calls: list = []
    models = [c.model for c in router_or_model.candidates] if isinstance(router_or_model, ModelRouter) else [router_or_model]
    for m in models:
        mid = m.get_config()["model_id"]

        def stream(_mid=mid, **_req):
            calls.append(_mid)
            out = behaviour[_mid]
            if isinstance(out, BaseException):
                raise out
            return _ok_stream(out)
        m.client.converse_stream = stream
    return calls


@pytest.fixture(autouse=True)
def _us_runtime(monkeypatch):
    """Staging, redesign and prod run in us-west-2 / us-east-1; a test that needs another Region sets its own."""
    monkeypatch.setenv("AWS_REGION", "us-west-2")


US_OPUS_CHAIN = ("us.anthropic.claude-opus-5-5", "global.anthropic.claude-opus-5", "us.anthropic.claude-opus-5",
                 "global.anthropic.claude-sonnet-5-5", "us.anthropic.claude-sonnet-5-5")


def test_in_a_us_region_each_model_is_followed_by_its_own_us_profile():
    """A global. ServiceUnavailableException ('Bedrock is unable to process your request', 2026-11-13) reached
    the user after four retries: the same model's us. profile is tried before any other model."""
    for region in ("us-west-2", "us-east-1"):
        assert defaults.fallbacks_for("global.anthropic.claude-opus-5-5", region) == US_OPUS_CHAIN
        assert defaults.fallbacks_for("global.anthropic.claude-sonnet-5-5", region) == (
            "us.anthropic.claude-sonnet-5-5", "global.anthropic.claude-sonnet-5", "us.anthropic.claude-sonnet-5")


def test_outside_the_us_no_us_profile_is_ever_a_backup():
    """A us. profile does not exist from eu-west-1 or ap-*: it would fail with a ValidationException, which is not
    'unavailable', so the chain would END there and the global. backups after it would never run."""
    for region in ("eu-west-1", "ap-southeast-2", "ap-northeast-1", ""):
        assert defaults.fallbacks_for("global.anthropic.claude-opus-5-5", region) == (
            "global.anthropic.claude-opus-5", "global.anthropic.claude-sonnet-5-5"), region


def test_models_json_stays_region_neutral():
    """The geography profiles are derived per Region, never written: models.json names global. ids only."""
    for primary, backups in MODELS["fallbacks"].items():
        assert all(m.startswith("global.") for m in (primary, *backups)), primary
    assert MODELS["geo_profile_fallbacks"] == {"us-": "us."}


def test_the_chains_live_in_models_json_and_every_backup_is_a_different_model():
    chains = MODELS["fallbacks"]
    assert chains["global.anthropic.claude-opus-5-5"] == ["global.anthropic.claude-opus-5", "global.anthropic.claude-sonnet-5-5"]
    assert chains["global.anthropic.claude-sonnet-5-5"] == ["global.anthropic.claude-sonnet-5"]
    for primary, backups in chains.items():
        assert backups and primary not in backups and len(set(backups)) == len(backups)
    assert defaults.fallbacks_for("global.anthropic.claude-opus-5-5", "eu-west-1") == tuple(chains["global.anthropic.claude-opus-5-5"])
    assert defaults.fallbacks_for("openai.gpt-6") == ()


def test_backups_are_built_exactly_like_the_primary():
    primary = build_model("global.anthropic.claude-opus-5-5", effort="high", max_tokens=12000)
    routed = with_fallbacks(primary)
    assert isinstance(routed, ModelRouter)
    ids = [c.model.get_config()["model_id"] for c in routed.candidates]
    assert ids == ["global.anthropic.claude-opus-5-5", *US_OPUS_CHAIN]
    assert routed.candidates[0].model is primary
    for c in routed.candidates[1:]:
        assert c.model._harness_build_args == {"effort": "high", "max_tokens": 12000}
    # No chain configured -> the model itself, unchanged.
    plain = build_model("global.anthropic.claude-haiku-5-5")
    assert with_fallbacks(plain) is plain


def test_unavailable_means_throttled_or_a_bedrock_outage_never_a_bad_request():
    for code in ("ThrottlingException", "ServiceUnavailableException", "InternalServerException",
                 "ModelStreamErrorException"):
        assert is_unavailable(_err(code)), code
    for code in ("ValidationException", "AccessDeniedException", "ResourceNotFoundException"):
        assert not is_unavailable(_err(code)), code


def _agent(routed):
    return Agent(model=routed, callback_handler=None, retry_strategy=ModelRetryStrategy(max_attempts=1))


def test_an_unavailable_model_hands_the_call_to_its_backup():
    routed = with_fallbacks(build_model("global.anthropic.claude-opus-5-5"))
    calls = _fake(routed, {"global.anthropic.claude-opus-5-5": _err("ServiceUnavailableException"),
                           "us.anthropic.claude-opus-5-5": "from us opus 5.5",
                           "global.anthropic.claude-opus-5": "should not run"})
    out = _agent(routed)("hi")
    assert str(out).strip() == "from us opus 5.5"  # the turn stays on Opus 5.5
    assert calls == ["global.anthropic.claude-opus-5-5", "us.anthropic.claude-opus-5-5"]


def test_the_chain_continues_down_the_list():
    routed = with_fallbacks(build_model("global.anthropic.claude-opus-5-5"))
    calls = _fake(routed, {"global.anthropic.claude-opus-5-5": _err("ThrottlingException"),
                           "us.anthropic.claude-opus-5-5": _err("ServiceUnavailableException"),
                           "global.anthropic.claude-opus-5": _err("InternalServerException"),
                           "us.anthropic.claude-opus-5": _err("ServiceUnavailableException"),
                           "global.anthropic.claude-sonnet-5-5": "from sonnet 5.5",
                           "us.anthropic.claude-sonnet-5-5": "should not run"})
    assert str(_agent(routed)("hi")).strip() == "from sonnet 5.5"
    assert calls == ["global.anthropic.claude-opus-5-5", *US_OPUS_CHAIN[:4]]


def test_an_eu_install_keeps_its_global_backups(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    routed = with_fallbacks(build_model("global.anthropic.claude-opus-5-5"))
    calls = _fake(routed, {"global.anthropic.claude-opus-5-5": _err("ServiceUnavailableException"),
                           "global.anthropic.claude-opus-5": "from opus 5",
                           "global.anthropic.claude-sonnet-5-5": "should not run"})
    assert str(_agent(routed)("hi")).strip() == "from opus 5"
    assert calls == ["global.anthropic.claude-opus-5-5", "global.anthropic.claude-opus-5"]


def test_a_bad_request_is_not_an_outage_and_surfaces():
    routed = with_fallbacks(build_model("global.anthropic.claude-opus-5-5"))
    calls = _fake(routed, {"global.anthropic.claude-opus-5-5": _err("ValidationException"),
                           "us.anthropic.claude-opus-5-5": "should not run",
                           "global.anthropic.claude-opus-5": "should not run",
                           "global.anthropic.claude-sonnet-5-5": "should not run"})
    with pytest.raises(Exception, match="ValidationException"):
        _agent(routed)("hi")
    assert calls == ["global.anthropic.claude-opus-5-5"]


def test_the_strategy_declines_after_a_non_availability_failure():
    class _A:
        def __init__(self, exc): self.exception, self.candidate = exc, None

    class _Ctx:
        def __init__(self, attempts, candidates): self.attempts, self.candidates = attempts, candidates

    routed = with_fallbacks(build_model("global.anthropic.claude-sonnet-5-5"))
    s = UnavailableFallback()
    assert asyncio.run(s.select(_Ctx([_A(_err("ValidationException"))], routed.candidates))) is None


def test_every_agent_built_on_a_harness_model_gets_its_fallbacks():
    """The per-request agents pass their model through with_fallbacks (main's process-global agent is excluded: a
    model-switch action replaces its model in place, which a router would override)."""
    expect = {"main.py": 1, "task_agent.py": 3, "planning_agent.py": 1, "render_agent.py": 2}
    for name, n in expect.items():
        text = (SRC / name).read_text()
        assert len(re.findall(r"with_fallbacks\((?:request_model|node_model|model)\)", text)) == n, name
