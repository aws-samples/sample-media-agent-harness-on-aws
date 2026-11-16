"""discover_models tags each model with supportsEffort.

The UI shows the reasoning-effort slider only for capable models, and sending
output_config.effort to an incapable model is a hard 400 — so the capability
must be surfaced per model from the catalog, computed from the SAME gate the
backend uses (model.factory.supports_reasoning_effort).
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from model import catalog  # noqa: E402


def _fake_profiles(ids):
    return [
        {
            "inferenceProfileId": pid,
            "inferenceProfileName": pid.split(".")[-1],
            "status": "ACTIVE",
        }
        for pid in ids
    ]


def _run_discover(ids):
    client = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {"inferenceProfileSummaries": _fake_profiles(ids)}
    ]
    client.get_paginator.return_value = paginator
    client.list_foundation_models.side_effect = RuntimeError("no grant")  # lifecycles unreadable: no lifecycle filter
    with patch.object(catalog.boto3, "client", return_value=client):
        return {m["id"]: m for m in catalog.discover_models(region="us-west-2")}


def test_supports_effort_is_tagged_true_for_capable_models():
    ids = ["us.anthropic.claude-opus-5", "us.anthropic.claude-sonnet-5",
           "us.anthropic.claude-haiku-5-5", "us.anthropic.claude-opus-4-6"]
    out = _run_discover(ids)
    for pid in ids:
        assert out[pid]["supportsEffort"] is True, pid


def test_supports_effort_is_tagged_false_for_incapable_models():
    # opus-4-5 and sonnet-4-5 are the trap: close ids that 400 on the field.
    ids = ["us.anthropic.claude-opus-4-5", "us.anthropic.claude-sonnet-4-5",
           "us.anthropic.claude-haiku-4-5-20251001-v1:0"]  # incapable and on the supported list
    out = _run_discover(ids)
    for pid in ids:
        assert out[pid]["supportsEffort"] is False, pid


def test_each_model_still_carries_id_and_name():
    out = _run_discover(["us.anthropic.claude-opus-5"])
    m = out["us.anthropic.claude-opus-5"]
    assert m["id"] == "us.anthropic.claude-opus-5"
    assert m["name"]
    assert "supportsEffort" in m
