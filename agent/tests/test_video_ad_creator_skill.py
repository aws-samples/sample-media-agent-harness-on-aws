"""Pins the rules of the built-in ``video-ad-creator`` skill that each cost a failed run.

Every assertion below maps to a measured failure in a staging or prod end-to-end run
(Nov 2026). Editing the skill so one of these disappears re-opens that failure.
"""
from __future__ import annotations

import re
from pathlib import Path

from orchestration.plan_and_execute_tool import _SKILL_TOOL_RELEVANCE

SKILL_DIR = Path(__file__).resolve().parents[1] / "skills" / "video-ad-creator"
SKILL_MD = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
FRONTMATTER = re.match(r"^---\n(.*?)\n---\n", SKILL_MD, re.S).group(1)
BODY = SKILL_MD[len(FRONTMATTER) + 8:]


def test_frontmatter_name_matches_directory_and_description_fits():
    assert re.search(r"^name: video-ad-creator$", FRONTMATTER, re.M)
    desc = re.search(r'^description: "(.*)"$', FRONTMATTER, re.M).group(1)
    assert 0 < len(desc) <= 1024


def test_lip_sync_model_is_minimax_h3_at_1080p_with_the_5s_minimum():
    # sync-lipsync v3 over-animated the mouth; MiniMax H3 rejects audio under 5 s at RESULT time.
    assert "minimax/h3-max/lip-sync/image-to-video" in BODY
    assert '`resolution: "1080P"`' in BODY
    assert "Lip-sync minimum: 5 s" in BODY
    assert "| `fal-ai/sync-lipsync/v3/image-to-video` |" not in BODY


def test_voice_is_elevenlabs_jessica_with_the_expressive_settings():
    assert "fal-ai/elevenlabs/tts/turbo-v2.5" in BODY
    assert '"voice": "Jessica", "stability": 0.3, "similarity_boost": 0.75, "style": 0.5' in BODY


def test_models_get_session_output_copies_never_project_asset_urls():
    # prod: fal could not download /projects/<pid>/image:<id>/... -> file_download_error.
    assert "Model inputs must be session-output URLs" in BODY
    assert "`src-<asset id>" in BODY


def test_sandbox_rules_that_each_failed_a_run_are_present():
    assert "tools/ffmpeg/ffmpeg-release-arm64-static.tar.xz" in BODY  # no list_objects (AccessDenied)
    assert "Never write a file to a path you also read" in BODY      # music.wav in == out
    assert "fps=30,settb=1/30" in BODY                               # xfade "rate of 1/0"
    assert "Never feed `acrossfade` from one `asplit`" in BODY         # ffmpeg 7.0.2 drops copy 2
    assert "two calls" in BODY                                       # 5-minute code_interpreter cap


def test_presenter_rules_against_known_lip_sync_artifacts():
    assert "except green" in BODY            # green clothing keys out
    assert "no knit" in BODY                 # ribbed knit became "MINIMAX" lettering
    assert "Crop before you place" in BODY   # full-width plate landed over the logo


def test_plan_nodes_inherit_the_skill_only_for_matching_tools():
    tools = _SKILL_TOOL_RELEVANCE["video-ad-creator"]
    for needed in ("submit_job", "code_interpreter", "show_media_gallery"):
        assert needed in tools


def test_no_instance_specific_content_remains():
    # The skill is a generic engine; the first demo brand must not leak into instructions.
    for word in ("nanoo", "pasta", "grandma", "grandmother", "italian", "mandolin", "buon"):
        assert word not in BODY.lower(), word


def test_spec_presets_and_all_formats_are_defined():
    for preset in ("**Presenter-led**", "**Product showcase**", "**Promo / offer**"):
        assert preset in BODY
    for aspect, canvas in (("16:9", "1920×1080"), ("9:16", "1080×1920"), ("1:1", "1080×1080"), ("4:5", "1080×1350")):
        assert f"| {aspect} | {canvas} |" in BODY
    for visual in ("presenter_on_brand", "presenter_over_clip", "`clip`", "text_card", "end_card"):
        assert visual in BODY
    for mode in ('"synthetic"', '"user_photo"', '"none"'):
        assert mode in BODY
