"""The supported-model list (models.json `picker`): the picker and the agents offer only models the harness runs on.

IND403 dry run: Settings -> Model offered models that failed every turn in a workshop account ("Marketplace access
denied", an 8,192-token output limit under the harness's 16K budget).
"""
import pytest

from model.catalog import supported
from model.defaults import AGENT_MODELS, FALLBACKS, PICKER


def test_there_is_a_list():
    assert PICKER


@pytest.mark.parametrize("model_id", [
    "global.anthropic.claude-opus-5-5", "us.anthropic.claude-sonnet-5-5", "global.anthropic.claude-haiku-4-5",
    "us.anthropic.claude-sonnet-4-20250514-v1:0", "global.anthropic.claude-fable-5-2",
    # Not Claude only (2026-11-15): Nova 2 and GPT, each checked live with a tool and a 16K output budget.
    "global.amazon.nova-2-lite-v1:0", "us.amazon.nova-2-lite-v1:0",
    "global.openai.gpt-6.1-sol", "us.openai.gpt-6-astra", "global.openai.gpt-5.6-terra", "global.openai.gpt-5.4",
])
def test_the_families_the_harness_runs_on_are_supported(model_id):
    assert supported(model_id)


@pytest.mark.parametrize("model_id", [
    "us.anthropic.claude-opus-4-1-20250805-v1:0",   # superseded
    "us.anthropic.claude-3-5-sonnet-20241022-v2:0",  # 8,192-token output limit
    "us.meta.llama4-maverick-17b-instruct-v1:0", "us.amazon.nova-lite-v1:0", "us.amazon.nova-premier-v1:0",
    "us.amazon.nova-pro-v1:0", "us.openai.gpt-oss-120b-1:0",
    "us.writer.palmyra-x5-v1:0", "us.stability.sd3-5-large-v1:0", "us.twelvelabs.pegasus-1-2-v1:0",
])
def test_models_that_failed_or_are_not_chat_models_are_not(model_id):
    assert not supported(model_id)


def test_every_default_and_fallback_is_on_the_list():
    ids = set(AGENT_MODELS.values()) | set(FALLBACKS) | {m for chain in FALLBACKS.values() for m in chain}
    assert all(supported(m) for m in ids), [m for m in ids if not supported(m)]


def _discover(profiles, lifecycles):
    from unittest.mock import MagicMock, patch
    from model import catalog
    client = MagicMock()
    client.get_paginator.return_value.paginate.return_value = [{"inferenceProfileSummaries": profiles}]
    if isinstance(lifecycles, Exception):
        client.list_foundation_models.side_effect = lifecycles
    else:
        client.list_foundation_models.return_value = {"modelSummaries": [
            {"modelId": mid, "modelLifecycle": {"status": st}} for mid, st in lifecycles.items()]}
    with patch.object(catalog.boto3, "client", return_value=client):
        return [m["id"] for m in catalog.discover_models(region="us-west-2")]


def _profile(pid, *models):
    return {"inferenceProfileId": pid, "inferenceProfileName": pid, "status": "ACTIVE",
            "models": [{"modelArn": f"arn:aws:bedrock:{r}::foundation-model/{m}"} for m in models for r in ("us-west-2", "us-east-1")]}


def test_a_legacy_or_retired_model_is_not_offered():
    # Staging 2026-11-15: Sonnet 4.5 is LEGACY; Sonnet 4's model is no longer listed at all (retired), though its
    # profile remains -- the same state as Nova Premier.
    profiles = [
        _profile("us.anthropic.claude-sonnet-5-5", "anthropic.claude-sonnet-5-5"),
        _profile("us.anthropic.claude-sonnet-4-5-20250929-v1:0", "anthropic.claude-sonnet-4-5-20250929-v1:0"),
        _profile("us.anthropic.claude-sonnet-4-20250514-v1:0", "anthropic.claude-sonnet-4-20250514-v1:0"),
    ]
    lifecycles = {"anthropic.claude-sonnet-5-5": "ACTIVE", "anthropic.claude-sonnet-4-5-20250929-v1:0": "LEGACY"}
    assert _discover(profiles, lifecycles) == ["us.anthropic.claude-sonnet-5-5"]


def test_unreadable_lifecycles_keep_the_supported_list():
    profiles = [_profile("us.anthropic.claude-sonnet-4-5-20250929-v1:0", "anthropic.claude-sonnet-4-5-20250929-v1:0")]
    assert _discover(profiles, RuntimeError("AccessDenied")) == ["us.anthropic.claude-sonnet-4-5-20250929-v1:0"]
