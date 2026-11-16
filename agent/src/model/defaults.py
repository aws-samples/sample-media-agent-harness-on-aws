"""The harness's default models, from ``models.json`` beside this module (the one place they are set).

The runtime zip bundles package ``.json`` files, and the deploy reads the same file for the UI proxy's default and
``BEDROCK_REASONING_EFFORT``. No environment variable overrides these: a per-runtime override is how the task agent
once ran a different model than the code said. A user's Settings -> Model pick is per request (``model_id``), not
here.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

_PATH = Path(__file__).with_name("models.json")
_DATA = json.loads(_PATH.read_text(encoding="utf-8"))

AGENT_MODELS: dict[str, str] = dict(_DATA["agents"])
REASONING_EFFORT: str = str(_DATA["reasoning_effort"])
#: Preserved-thinking prefix check (model/factory.py thinking_binding_mode): drop_block | error | off.
THINKING_PREFIX_MISMATCH: str = str(_DATA["thinking_prefix_mismatch"])
#: Per model id, the models that take over (in order) when it is unavailable (model/factory.py with_fallbacks).
FALLBACKS: dict[str, tuple[str, ...]] = {k: tuple(v) for k, v in (_DATA.get("fallbacks") or {}).items()}
#: Runtime Region prefix -> that geography's profile prefix ("us-" -> "us."): each global. model in a chain is
#: followed by the same model on it there. A geography absent here gets no such profile (it may not exist there).
GEO_PROFILE_FALLBACKS: dict[str, str] = dict(_DATA.get("geo_profile_fallbacks") or {})
#: The models a person may pick and the agents may choose (inference-profile id patterns); empty = every model.
PICKER: tuple[str, ...] = tuple(_DATA.get("picker") or ())

_GLOBAL = "global."


def _geo_twin(model_id: str, region: str) -> str:
    """``model_id`` on the geography profile of ``region`` (``global.X`` -> ``us.X`` in us-*); "" when none applies."""
    if not model_id.startswith(_GLOBAL):
        return ""
    for region_prefix, profile_prefix in GEO_PROFILE_FALLBACKS.items():
        if region.startswith(region_prefix):
            return profile_prefix + model_id[len(_GLOBAL):]
    return ""


def fallbacks_for(model_id: str, region: Optional[str] = None) -> tuple[str, ...]:
    """The models that take over from ``model_id`` when it is unavailable, in order; ``()`` when none are set.

    In a Region ``geo_profile_fallbacks`` names, each global. model (the primary included) is followed by its own
    geography profile, so ``global.opus-5-5`` in us-west-2 is backed by ``us.opus-5-5, global.opus-5, us.opus-5, ...``.
    ``region`` defaults to the runtime's ``AWS_REGION``."""
    chain = FALLBACKS.get(model_id or "", ())
    if not chain:
        return ()
    region = region if region is not None else os.environ.get("AWS_REGION", "")
    out: list[str] = []
    for m in (model_id, *chain):
        if m != model_id:
            out.append(m)
        twin = _geo_twin(m, region)
        if twin:
            out.append(twin)
    return tuple(dict.fromkeys(x for x in out if x != model_id))


def model_for(agent: str) -> str:
    """The default model id for ``agent`` (``orchestrator``, ``task``, ``planning``, ...). KeyError if unknown."""
    return AGENT_MODELS[agent]
