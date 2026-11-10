"""content_moderation SKILL.md: multi-video guided runs, per-video report names, deterministic rating.

Staging session d6a560a8 (three videos, guided): the skill described one video, so the model ran one
plan per video with the same name; Phase 2 then resolved every video's inputs to NBA's (the newest),
three sg5 nodes shared `compliance_report.pdf` on one OUTPUT prefix, and "damn" was labelled `mild`
(Child 7+) although the rating table puts mild language at Teen 13+.
"""
from __future__ import annotations

import re
from pathlib import Path

SKILL = Path(__file__).parents[1] / "skills" / "content-moderation" / "SKILL.md"
TEXT = SKILL.read_text(encoding="utf-8")
FLAT = " ".join(TEXT.split())


def test_several_videos_run_one_plan_per_phase_with_per_video_nodes():
    assert "**Several videos (N > 1) — one plan per PHASE, one node per VIDEO.**" in FLAT
    assert "never make one plan per video" in FLAT
    for node in ("sg1_segment_shots__<v>", "sg2_diarize_speech__<v>", "sg3_enrich_shot_metadata__<v>",
                 "sg4_analyze_conversation__<v>", "sg5_report__<v>"):
        assert node in FLAT, node
    assert "Phase 3 is still a PLAN" in FLAT and "never a direct dispatch" in FLAT


def test_every_report_file_name_carries_the_video_stem():
    assert "<session OUTPUT prefix><video-stem>_compliance_report.pdf" in FLAT
    assert "always prefix the stem, even for a single video" in FLAT
    # No instruction still writes a bare shared name.
    assert "<session OUTPUT prefix>compliance_report" not in FLAT


def _rate(findings):
    # The rating moved into the bundled builder (session a2d759c2); run it as the sg5 cell now does.
    import importlib.util

    spec = importlib.util.spec_from_file_location("cm_build_report", SKILL.parent / "scripts" / "build_report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.compute_rating([{"confidence": "verified", **f} for f in findings])


def test_mild_language_rates_teen_13_like_the_rating_table():
    assert "| Teen 13+ | Moderate material: mild language" in (SKILL.parent / "references" / "rating-system.md").read_text()
    assert _rate([{"category": "LANGUAGE", "severity": "mild", "detail": "Oh, damn."}]) == "Teen 13+"
    assert _rate([{"category": "LANGUAGE", "severity": "strong", "detail": "slur"}]) == "Adult 18+"
    assert _rate([{"category": "FRIGHTENING", "severity": "mild", "detail": "dark hallway"}]) == "Child 7+"
    assert _rate([]) == "All Ages"
