"""The caching gate and the legacy-model filter.

Both exist because of one live failure: selecting a model from the picker
produced `AccessDeniedException ... unsupported model or your request did not
allow prompt caching` on every turn. `cache_tools` was applied to whatever model
was chosen while only `cache_config` auto-detected support.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from model.factory import supports_prompt_caching
from model.catalog import supported


class TestCachingSupport:
    def test_current_claude_generations_cache(self):
        for mid in ("us.anthropic.claude-sonnet-5",
                    "global.anthropic.claude-sonnet-5",
                    "us.anthropic.claude-opus-4-5-20251101-v1:0",
                    "global.anthropic.claude-opus-4-8",
                    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                    "us.anthropic.claude-fable-5"):
            assert supports_prompt_caching(mid), mid

    def test_region_prefix_is_irrelevant(self):
        """`us.` and `global.` route the same model; the gate must not care."""
        for pre in ("", "us.", "global."):
            assert supports_prompt_caching(f"{pre}anthropic.claude-sonnet-5")

    def test_nova_caches(self):
        assert supports_prompt_caching("us.amazon.nova-pro-v1:0")

    def test_families_without_caching_are_refused(self):
        """These are what made the picker able to break a turn on one click."""
        for mid in ("us.meta.llama3-3-70b-instruct-v1:0",
                    "us.meta.llama4-maverick-17b-instruct-v1:0",
                    "us.deepseek.r1-v1:0",
                    "us.writer.palmyra-x5-v1:0",
                    "us.mistral.pixtral-large-2502-v1:0",
                    "us.anthropic.claude-3-haiku-20240307-v1:0",
                    "us.anthropic.claude-3-sonnet-20240229-v1:0"):
            assert not supports_prompt_caching(mid), mid

    def test_empty_and_none_are_safe(self):
        assert not supports_prompt_caching("")
        assert not supports_prompt_caching(None)  # type: ignore[arg-type]


class TestLegacyFilter:
    # The supported list (models.json `picker`) replaced the legacy/keyword lists: dropped = not supported.
    def _dropped(self, profile_id: str) -> bool:
        return not supported(profile_id)

    def test_superseded_generations_are_dropped(self):
        for pid in ("us.anthropic.claude-3-haiku-20240307-v1:0",
                    "us.anthropic.claude-3-sonnet-20240229-v1:0",
                    "us.anthropic.claude-opus-4-1-20250805-v1:0",
                    "us.meta.llama3-1-8b-instruct-v1:0",
                    "us.meta.llama3-2-90b-instruct-v1:0",
                    "us.meta.llama3-3-70b-instruct-v1:0"):
            assert self._dropped(pid), pid

    def test_current_models_survive(self):
        """Sonnet 5 was reported missing; it was present but buried. It must
        stay present after pruning, which is the point of pruning."""
        for pid in ("us.anthropic.claude-sonnet-5",
                    "global.anthropic.claude-sonnet-5",
                    "us.anthropic.claude-opus-5",
                    "global.anthropic.claude-sonnet-4-6",
                    "us.anthropic.claude-haiku-4-5-20251001-v1:0"):
            assert not self._dropped(pid), pid
        # Llama 4 caps output at 8,192 tokens, under the harness's 16K budget: every turn failed (IND403).
        assert self._dropped("us.meta.llama4-scout-17b-instruct-v1:0")

    def test_claude_4_and_5_are_not_caught_by_the_3_x_patterns(self):
        """`claude-3-` must not match `claude-3-5`-style or 4.x/5 ids."""
        assert not self._dropped("us.anthropic.claude-opus-4-6-v1")
        assert not self._dropped("us.anthropic.claude-sonnet-4-5-20250929-v1:0")

    def test_image_models_are_not_offered(self):
        assert self._dropped("us.stability.sd3-5-large-v1:0") and self._dropped("us.stability.stable-image-inpaint-v1:0")
