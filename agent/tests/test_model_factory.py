"""Every agent must get the same output cap and prompt-caching defaults.

Two measured problems drove this. `max_tokens` was unset everywhere, so a multi-node
DAG turn died mid-plan with MaxTokensReachedException. And prompt caching was never
enabled: a fresh session answering "Reply with just: ok" with ZERO tool calls cost
26,507 input tokens (2,084 system prompt + 10,768 builtin tool specs + ~13,000
federated gateway tool specs), a floor paid on every model call in every cycle.

`BedrockModel(...)` was built at ten sites across six modules. Configuring them
individually is how they drift, so the guard here is that none of them do.
"""
import re
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from model.factory import (DEFAULT_MAX_TOKENS, build_model,  # noqa: E402
                           caching_enabled, max_tokens_for, output_ceiling,
                           reasoning_effort, reasoning_request_fields,
                           supports_reasoning_effort)

AGENT_MODULES = ["main.py", "planning_agent.py",
                 "render_agent.py", "task_agent.py"]


class TestDefaults:
    def test_output_cap_is_set(self):
        cfg = build_model("us.anthropic.claude-sonnet-5").get_config()
        assert cfg["max_tokens"] == DEFAULT_MAX_TOKENS
        assert cfg["max_tokens"] >= 8192, "must fit a multi-step plan plus its summary"

    def test_tool_specs_are_cached(self):
        """The largest stable block — ~24k tokens of specs re-sent every cycle."""
        cfg = build_model("us.anthropic.claude-sonnet-5").get_config()
        cc = cfg.get("cache_config")
        assert cc is not None and cc.tools_ttl is True, \
            "tool specs must be cached via the modern CacheConfig(tools_ttl=...)"
        assert cfg.get("cache_tools") is None, "the deprecated cache_tools flag must not be set"

    def test_caching_emits_no_deprecation_warning(self):
        """Task 7: the modern CacheConfig(tools_ttl) path must not trip the cache_tools
        DeprecationWarning that strands >=1.55 raises for the old model-level flag."""
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            build_model("us.anthropic.claude-sonnet-5")  # must not raise

    def test_automatic_cache_points_are_requested(self):
        cfg = build_model("us.anthropic.claude-sonnet-5").get_config()
        cc = cfg.get("cache_config")
        assert cc is not None and cc.strategy == "auto"

    def test_deprecated_cache_prompt_is_not_used(self):
        cfg = build_model("us.anthropic.claude-sonnet-5").get_config()
        assert cfg.get("cache_prompt") is None


class TestOverrides:
    def test_explicit_max_tokens_wins(self):
        assert build_model("m", max_tokens=1234).get_config()["max_tokens"] == 1234

    def test_env_sets_max_tokens(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_MAX_TOKENS", "2048")
        assert max_tokens_for() == 2048

    def test_bad_env_falls_back_rather_than_crashing(self, monkeypatch):
        for bad in ("", "0", "-5", "lots"):
            monkeypatch.setenv("BEDROCK_MAX_TOKENS", bad)
            assert max_tokens_for() == DEFAULT_MAX_TOKENS

    def test_caching_can_be_disabled(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_PROMPT_CACHE", "off")
        assert caching_enabled() is False


class TestOutputCeiling:
    """Bedrock rejects an over-limit `max_tokens` outright rather than capping it.

    Observed live in sandbox: the model picker defaulted to Claude 3 Haiku (4096
    output) while the harness asked for 16384, and EVERY chat turn died with
    `ValidationException ... exceeds the model limit of 4096`. Nothing in the
    message named the model, and the picker offers 40+ entries, so it was one
    ordinary click away.
    """

    def test_a_low_ceiling_model_is_clamped(self):
        cfg = build_model("us.anthropic.claude-3-haiku-20240307-v1:0").get_config()
        assert cfg["max_tokens"] == 4096

    def test_clamping_applies_to_every_known_low_family(self):
        for model_id in ("us.anthropic.claude-3-haiku-20240307-v1:0",
                         "us.anthropic.claude-3-sonnet-20240229-v1:0",
                         "us.anthropic.claude-3-opus-20240229-v1:0"):
            assert build_model(model_id).get_config()["max_tokens"] == 4096, model_id

    def test_region_prefix_and_version_suffix_are_irrelevant(self):
        for model_id in ("anthropic.claude-3-haiku-20240307-v1:0",
                         "global.anthropic.claude-3-haiku-20240307-v1:0",
                         "US.ANTHROPIC.CLAUDE-3-HAIKU-20240307-V1:0"):
            assert output_ceiling(model_id) == 4096, model_id

    def test_claude_3_5_is_not_swallowed_by_the_claude_3_patterns(self):
        # `claude-3-5-sonnet` allows 8192; a `claude-3-` prefix match would have
        # wrongly clamped it to 4096, which is why the families are spelled out.
        assert output_ceiling("us.anthropic.claude-3-5-sonnet-20241022-v2:0") is None

    def test_current_generation_is_untouched(self):
        for model_id in ("us.anthropic.claude-sonnet-5", "us.anthropic.claude-opus-5",
                         "us.amazon.nova-pro-v1:0"):
            assert output_ceiling(model_id) is None, model_id
            assert build_model(model_id).get_config()["max_tokens"] == DEFAULT_MAX_TOKENS

    def test_an_unknown_model_keeps_the_configured_cap(self):
        # A missing entry must degrade to previous behaviour, never to a guess.
        assert output_ceiling("some.future.model-v9") is None
        assert build_model("some.future.model-v9").get_config()["max_tokens"] == DEFAULT_MAX_TOKENS

    def test_an_explicit_override_is_still_clamped(self):
        # The caller asking for more than the model allows is the same hard failure.
        assert max_tokens_for(16384, "us.anthropic.claude-3-haiku-20240307-v1:0") == 4096

    def test_an_override_below_the_ceiling_is_respected(self):
        assert max_tokens_for(1024, "us.anthropic.claude-3-haiku-20240307-v1:0") == 1024

    def test_env_cap_is_clamped_too(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_MAX_TOKENS", "16384")
        assert max_tokens_for(None, "us.anthropic.claude-3-haiku-20240307-v1:0") == 4096

    def test_model_id_is_optional_so_existing_callers_are_unaffected(self):
        assert max_tokens_for() == DEFAULT_MAX_TOKENS
        assert max_tokens_for(4096) == 4096
        assert build_model("m").get_config().get("cache_tools") is None

    def test_caching_is_on_by_default(self, monkeypatch):
        monkeypatch.delenv("BEDROCK_PROMPT_CACHE", raising=False)
        assert caching_enabled() is True

    def test_extra_kwargs_pass_through(self):
        cfg = build_model("m", temperature=0.2).get_config()
        assert cfg["temperature"] == 0.2
        assert cfg["max_tokens"] == DEFAULT_MAX_TOKENS, "defaults still applied"


class TestNoAgentBypassesTheFactory:
    """A site constructing BedrockModel directly silently loses both defaults."""

    def test_no_direct_construction_in_agent_modules(self):
        offenders = {}
        for name in AGENT_MODULES:
            text = (SRC / name).read_text()
            hits = re.findall(r"(?<!\w)BedrockModel\s*\(", text)
            if hits:
                offenders[name] = len(hits)
        assert not offenders, (
            f"these modules construct BedrockModel directly: {offenders} — "
            "route them through model_factory.build_model or they lose max_tokens "
            "and prompt caching"
        )

    def test_every_agent_module_uses_the_factory(self):
        missing = [n for n in AGENT_MODULES
                   if "build_model(" not in (SRC / n).read_text()]
        assert not missing, f"modules not using the factory: {missing}"


OPUS = "global.anthropic.claude-opus-4-8"


class TestReasoningEffort:
    """The effort plumb reaches BedrockModel via additionalModelRequestFields, is gated
    to the verified-capable (Opus) family, and is carried through the TypeError fallback.

    Schema verified empirically against the live model (Bedrock Converse, us-west-2):
    `output_config:{effort:...}` ALONE is ACCEPTED and is the actual latency win
    (~17% less wall time, ~16% fewer output tokens, TTFT held ~0.75s). `effort` /
    `reasoning_effort` as top-level fields and `thinking.type:"enabled"` are REJECTED,
    and an explicit `thinking:{type:"adaptive"}` front-loads reasoning (8-18s TTFT),
    so we send NO thinking field — the model keeps its default adaptive thinking at
    the reduced effort.
    """

    def _arf(self, model):
        return model.get_config().get("additional_request_fields")

    def test_explicit_effort_reaches_bedrock_for_opus(self):
        arf = self._arf(build_model(OPUS, effort="medium"))
        assert arf == {"output_config": {"effort": "medium"}}

    def test_env_default_is_applied_when_no_explicit_effort(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_REASONING_EFFORT", "low")
        arf = self._arf(build_model(OPUS))
        assert arf == {"output_config": {"effort": "low"}}

    def test_no_thinking_field_is_sent(self, monkeypatch):
        # An explicit thinking:{type:adaptive} spiked TTFT to 8-18s in the live probe;
        # we must never send it. Omitting it keeps the model's default adaptive thinking.
        monkeypatch.setenv("BEDROCK_REASONING_EFFORT", "medium")
        arf = self._arf(build_model(OPUS))
        assert "thinking" not in (arf or {})

    def test_explicit_effort_overrides_env(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_REASONING_EFFORT", "high")
        arf = self._arf(build_model(OPUS, effort="low"))
        assert arf["output_config"]["effort"] == "low"

    def test_absent_when_unset(self, monkeypatch):
        monkeypatch.delenv("BEDROCK_REASONING_EFFORT", raising=False)
        assert self._arf(build_model(OPUS)) is None

    def test_invalid_env_value_is_ignored(self, monkeypatch):
        for bad in ("", "lots", "none", "0"):
            monkeypatch.setenv("BEDROCK_REASONING_EFFORT", bad)
            assert reasoning_effort() is None
            assert self._arf(build_model(OPUS)) is None

    def test_supported_generations_are_capable(self):
        # Verified live via Bedrock Converse (us-west-2, 2026-10): generation >=4.6 or
        # >=5 across opus/sonnet/haiku accepts output_config.effort.
        for mid in ("us.anthropic.claude-opus-4-6", "us.anthropic.claude-opus-4-7",
                    "global.anthropic.claude-opus-4-8", "us.anthropic.claude-opus-5",
                    "us.anthropic.claude-opus-5-5", "us.anthropic.claude-sonnet-4-6",
                    "us.anthropic.claude-sonnet-5", "us.anthropic.claude-sonnet-5-5",
                    "us.anthropic.claude-haiku-5-5"):
            assert supports_reasoning_effort(mid) is True, mid
            assert self._arf(build_model(mid, effort="high")) == {
                "output_config": {"effort": "high"}}, mid

    def test_unsupported_generations_are_gated_off(self, monkeypatch):
        # opus-4-5 / sonnet-4-5 / haiku-4-5, 4-1 and 3.x reject this schema live (HTTP
        # 400), so they must never receive it even when the global effort env is set.
        monkeypatch.setenv("BEDROCK_REASONING_EFFORT", "low")
        for mid in ("us.anthropic.claude-opus-4-5", "us.anthropic.claude-sonnet-4-5",
                    "us.anthropic.claude-haiku-4-5", "us.anthropic.claude-opus-4-1",
                    "us.anthropic.claude-3-5-sonnet-20241022-v2:0",
                    "us.amazon.nova-pro-v1:0"):
            assert supports_reasoning_effort(mid) is False, mid
            assert self._arf(build_model(mid, effort="low")) is None, mid

    def test_substring_entries_do_not_overmatch(self):
        # The 'claude-opus-5'/'claude-sonnet-5' entries (which also cover 5-5) must NOT
        # make the excluded 4-5 ids reachable, and opus-4-8 must not be reached by them.
        assert supports_reasoning_effort("us.anthropic.claude-opus-4-5") is False
        assert supports_reasoning_effort("us.anthropic.claude-sonnet-4-5") is False
        # opus-4-8 is capable via its OWN entry, not via a 5-entry — sanity that it
        # does not accidentally contain 'claude-opus-5'.
        assert "claude-opus-5" not in "claude-opus-4-8"

    def test_region_prefix_and_version_suffix_are_irrelevant_for_opus(self):
        for mid in ("anthropic.claude-opus-4-8", "GLOBAL.ANTHROPIC.CLAUDE-OPUS-4-8",
                    "us.anthropic.claude-opus-5"):
            assert supports_reasoning_effort(mid) is True, mid

    def test_helper_returns_empty_without_a_level(self):
        assert reasoning_request_fields(OPUS, None) == {}

    def test_effort_merges_with_caller_supplied_request_fields(self):
        arf = self._arf(build_model(
            OPUS, effort="low",
            additional_request_fields={"anthropic_beta": ["x"]}))
        assert arf["anthropic_beta"] == ["x"]
        assert arf["output_config"]["effort"] == "low"

    def test_typeerror_fallback_carries_effort_not_silently_dropped(self, monkeypatch):
        """If a strands version rejects a CACHE option (TypeError), the rebuilt model must
        still carry the effort additionalModelRequestFields — never degrade to full effort."""
        import model.factory as f

        captured = {}

        class FakeModel:
            def __init__(self, **kwargs):
                # Mimic an older strands that doesn't accept cache_config: raise on the
                # FIRST (cache-bearing) construction, succeed on the fallback rebuild.
                if "cache_config" in kwargs or "cache_tools" in kwargs:
                    raise TypeError("cache_config unsupported")
                captured.update(kwargs)

        monkeypatch.setattr(f, "StablePrefixBedrockModel", FakeModel)
        monkeypatch.setattr(f, "BedrockModel", FakeModel)
        # The uncached fallback now builds the harness base (prior-turn reasoning stripped), not bare BedrockModel.
        monkeypatch.setattr(f, "HarnessBedrockModel", FakeModel)
        f.build_model(OPUS, effort="low")
        assert captured.get("additional_request_fields") == {
            "output_config": {"effort": "low"}}, \
            "fallback dropped the effort field — it would silently restore full effort"
