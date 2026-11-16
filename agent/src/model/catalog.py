"""Discover available Bedrock inference profiles at startup.

Queries ListInferenceProfiles and filters to text/chat models,
providing a catalog the main agent can reference in its system prompt
and pass to dispatch_tool.
"""

import logging
import os
import re
from typing import Optional

import boto3

from model.defaults import PICKER
from model.factory import supports_reasoning_effort

logger = logging.getLogger(__name__)

# THE supported list (models.json `picker`), the same one the UI proxy's GET /models applies: one list, so the
# picker never offers a model the agents would refuse, and neither offers one that fails every turn.
_PICKER = tuple(re.compile(p) for p in PICKER)


def supported(model_id: str) -> bool:
    """Whether ``model_id`` is on the supported list (every model when the list is empty)."""
    return not _PICKER or any(p.search(model_id or "") for p in _PICKER)


def _active_model_ids(client) -> Optional[set[str]]:
    """The foundation models Bedrock lists as ACTIVE (not LEGACY), or None when that cannot be read."""
    try:
        resp = client.list_foundation_models()
    except Exception as e:  # a missing grant or a blip: offer the supported list unfiltered, never nothing
        logger.warning("Could not read model lifecycles: %s", e)
        return None
    return {m["modelId"] for m in resp.get("modelSummaries", [])
            if (m.get("modelLifecycle") or {}).get("status") == "ACTIVE"}


def current(profile: dict, active: Optional[set[str]]) -> bool:
    """Whether every model behind an inference profile is current: ACTIVE, not LEGACY and not retired.

    A model Bedrock no longer lists is retired (its profile can outlive it: Nova Premier, Sonnet 4 in 2026-11).
    ``active`` None (lifecycles unreadable) keeps every profile.
    """
    if active is None:
        return True
    ids = [str(m.get("modelArn", "")).split("foundation-model/")[-1] for m in profile.get("models") or []]
    return bool(ids) and all(i in active for i in ids)


def discover_models(region: Optional[str] = None) -> list[dict]:
    """Return available text/chat inference profiles as [{id, name, supportsEffort}].

    ``supportsEffort`` is whether the model accepts the reasoning-effort cap
    (``output_config.effort``); the UI uses it to show the effort slider only for
    capable models, and sending the field to an incapable model is a hard 400.
    """
    region = region or os.environ.get("AWS_REGION", "us-west-2")
    try:
        client = boto3.client("bedrock", region_name=region)
        active = _active_model_ids(client)
        profiles = []
        paginator = client.get_paginator("list_inference_profiles")
        for page in paginator.paginate(typeEquals="SYSTEM_DEFINED", maxResults=100):
            for p in page.get("inferenceProfileSummaries", []):
                if p.get("status") != "ACTIVE":
                    continue
                if not supported(p["inferenceProfileId"]) or not current(p, active):
                    continue
                profiles.append({
                    "id": p["inferenceProfileId"],
                    "name": p["inferenceProfileName"],
                    "supportsEffort": supports_reasoning_effort(p["inferenceProfileId"]),
                })
        logger.info("Discovered %d text/chat models from Bedrock", len(profiles))
        return profiles
    except Exception as e:
        logger.error("Failed to discover models: %s", e)
        return []
