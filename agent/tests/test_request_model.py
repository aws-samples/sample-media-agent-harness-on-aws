"""Per-request orchestrator model resolution.

The Agent is rebuilt for every request so per-actor state cannot leak between
callers, but the model was still taken from a container-level object. The UI's
model choice was therefore applied by mutating that shared object, which:
  - silently reverted to the default on cold start,
  - did not apply to other concurrent containers, and
  - leaked across users sharing a container.

Resolving the model from the request payload fixes all three.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import main  # noqa: E402


class _Sentinel:
    """Stands in for the container-level default model object."""


def test_absent_model_id_uses_the_fallback():
    fallback = _Sentinel()
    model, resolved = main._resolve_request_model({}, fallback)
    assert model is fallback
    assert resolved == main.DEFAULT_ORCHESTRATOR_MODEL_ID


def test_non_string_model_id_uses_the_fallback():
    fallback = _Sentinel()
    for bad in (None, 123, {}, [], ""):
        model, resolved = main._resolve_request_model({"model_id": bad}, fallback)
        assert model is fallback, bad
        assert resolved == main.DEFAULT_ORCHESTRATOR_MODEL_ID


def test_requested_model_is_honoured_and_reported():
    fallback = _Sentinel()
    wanted = "us.anthropic.claude-3-haiku-20240307-v1:0"
    model, resolved = main._resolve_request_model({"model_id": wanted}, fallback)
    # A real BedrockModel is constructed, so it must NOT be the fallback object.
    assert model is not fallback
    assert resolved == wanted


def test_resolution_never_raises_out_of_a_turn(monkeypatch):
    # An unusable id must degrade to the default rather than failing the request.
    def boom(*_a, **_k):
        raise RuntimeError("no such model")

    # Patch the seam `_resolve_request_model` actually calls. Model construction moved
    # behind `model_factory.build_model` so every agent shares the same max_tokens and
    # prompt-caching defaults; patching BedrockModel here no longer intercepts it.
    monkeypatch.setattr(main, "build_model", boom)
    fallback = _Sentinel()
    model, resolved = main._resolve_request_model({"model_id": "garbage"}, fallback)
    assert model is fallback
    assert resolved == main.DEFAULT_ORCHESTRATOR_MODEL_ID


def test_default_is_a_single_source_of_truth():
    # The literal must not be duplicated back into the agent build.
    #
    # Derived from the constant rather than spelled out: the invariant is "declared
    # once", not "declared as Sonnet 5". Hard-coding the model id here meant changing
    # the platform default failed this test for the wrong reason — the assertion
    # tracked the value instead of the property.
    # Since 2026-11 the one declaration is agent/src/model/models.json (tests/test_model_defaults.py).
    from model.defaults import model_for

    src = (Path(__file__).resolve().parents[1] / "src" / "main.py").read_text()
    assert main.DEFAULT_ORCHESTRATOR_MODEL_ID == model_for("orchestrator")
    assert f'"{main.DEFAULT_ORCHESTRATOR_MODEL_ID}"' not in src


class _Captured:
    """Records the kwargs build_model was called with (stand-in for a BedrockModel)."""


def _patch_build_model(monkeypatch):
    seen = {}

    def fake(*_a, **kwargs):
        seen.update(kwargs)
        return _Captured()

    monkeypatch.setattr(main, "build_model", fake)
    return seen


def test_valid_effort_is_passed_to_build_model(monkeypatch):
    seen = _patch_build_model(monkeypatch)
    wanted = "us.anthropic.claude-opus-5"
    model, resolved = main._resolve_request_model(
        {"model_id": wanted, "effort": "high"}, _Sentinel())
    assert resolved == wanted
    assert seen["model_id"] == wanted
    assert seen["effort"] == "high"


def test_invalid_effort_falls_back_to_env(monkeypatch):
    # An out-of-enum effort means None -> build_model applies the env default.
    for bad in ("xhigh", "max", "", "LOW", 3, None):
        seen = _patch_build_model(monkeypatch)
        main._resolve_request_model(
            {"model_id": "us.anthropic.claude-opus-5", "effort": bad}, _Sentinel())
        assert seen["effort"] is None, bad


def test_effort_on_default_model_builds_rather_than_reusing_fallback(monkeypatch):
    # No model_id but an effort set: the slider must still take effect on the default
    # model, so a model is built (not the prebuilt fallback) carrying the effort.
    seen = _patch_build_model(monkeypatch)
    fallback = _Sentinel()
    model, resolved = main._resolve_request_model({"effort": "medium"}, fallback)
    assert model is not fallback
    assert resolved == main.DEFAULT_ORCHESTRATOR_MODEL_ID
    assert seen["model_id"] == main.DEFAULT_ORCHESTRATOR_MODEL_ID
    assert seen["effort"] == "medium"


def test_no_model_and_no_effort_returns_prebuilt_fallback(monkeypatch):
    # The unchanged default path: no build when neither model nor effort is given.
    seen = _patch_build_model(monkeypatch)
    fallback = _Sentinel()
    model, resolved = main._resolve_request_model({}, fallback)
    assert model is fallback
    assert resolved == main.DEFAULT_ORCHESTRATOR_MODEL_ID
    assert seen == {}, "no model should be constructed on the pure-default path"
