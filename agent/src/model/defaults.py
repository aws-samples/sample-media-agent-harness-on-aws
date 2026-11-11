"""The harness's default models, from ``models.json`` beside this module (the one place they are set).

The runtime zip bundles package ``.json`` files, and the deploy reads the same file for the UI proxy's default and
``BEDROCK_REASONING_EFFORT``. No environment variable overrides these: a per-runtime override is how the task agent
once ran a different model than the code said. A user's Settings -> Model pick is per request (``model_id``), not
here.
"""
from __future__ import annotations

import json
from pathlib import Path

_PATH = Path(__file__).with_name("models.json")
_DATA = json.loads(_PATH.read_text(encoding="utf-8"))

AGENT_MODELS: dict[str, str] = dict(_DATA["agents"])
REASONING_EFFORT: str = str(_DATA["reasoning_effort"])
#: Preserved-thinking prefix check (model/factory.py thinking_binding_mode): drop_block | error | off.
THINKING_PREFIX_MISMATCH: str = str(_DATA["thinking_prefix_mismatch"])


def model_for(agent: str) -> str:
    """The default model id for ``agent`` (``orchestrator``, ``task``, ``planning``, ...). KeyError if unknown."""
    return AGENT_MODELS[agent]
