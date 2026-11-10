"""The content_moderation deterministic rating code floors severity by depicted act.

Issue 2b (sessions 9d8c215e vs b8a59e30): the SAME Tidal video was rated Teen 13+ one run
and Adult 18+ the next. The rating COMPUTATION in SKILL.md step 5 is deterministic from
per-finding ``severity`` (highest-severity-wins), but ``severity`` itself was the model's
free gestalt call — so identical ``depicted`` acts (drug_material, weapon_threat,
physical_violence, injury) were scored ``strong`` once and ``moderate`` the next, flipping
the rating.

The fix adds ``_ACT_FLOOR``: a deterministic floor keyed on the controlled ``depicted`` act,
applied in the same cell that already floors LANGUAGE. These tests extract the actual
rating code block from SKILL.md and exec it against fixtures of Tidal's REAL findings. The
drug/weapon/violence case was ``Teen 13+`` before the floor (test fails on the pre-fix skill)
and is ``Adult 18+`` after; the alcohol-only case stays ``Teen 13+`` (floor doesn't
over-escalate).
"""
from __future__ import annotations

import pathlib
import textwrap

SKILL = (
    pathlib.Path(__file__).resolve().parents[1]
    / "skills"
    / "content-moderation"
    / "SKILL.md"
)


# 2026-10-11 (session a2d759c2): the rating code moved out of SKILL.md into the deterministic report builder,
# `skills/content-moderation/scripts/build_report.py`, which every sg5 node now RUNS instead of re-typing.
# These cases exercise that builder: same fixtures, same expectations.
_BUILDER = SKILL.parent / "scripts" / "build_report.py"


def _builder():
    import importlib.util

    spec = importlib.util.spec_from_file_location("cm_build_report", _BUILDER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _rate(flagged_timeline: list[dict]) -> str:
    return _builder().compute_rating([{"confidence": "verified", **f} for f in flagged_timeline])


def test_rating_code_block_extractable_and_runnable():
    mod = _builder()
    assert mod._ACT_FLOOR["drug_material"] == "strong", "deterministic act->severity floor missing"
    assert "scripts/build_report.py" in SKILL.read_text(), "SKILL.md must route sg5 through the builder"
    # sanity: an empty timeline is All Ages
    assert _rate([]) == "All Ages"


def test_tidal_real_findings_force_adult_18():
    """Tidal's acts labelled 'moderate' by the model must still floor to Adult 18+.

    These are the exact findings from session 9d8c215e (which wrongly rated Teen 13+): the
    model scored drug bricks and an aimed/fired pistol 'moderate'. Without _ACT_FLOOR the
    code returns 'Teen 13+' (this test fails); with it, 'Adult 18+'.
    """
    findings = [
        {"start": 9.0, "end": 13.0, "category": "SUBSTANCE", "severity": "moderate",
         "detail": "plastic-wrapped white drug bricks", "evidence": "shot 9", "act": "drug_material"},
        {"start": 13.0, "end": 17.0, "category": "VIOLENCE", "severity": "moderate",
         "detail": "pistol aimed, incoming gunfire", "evidence": "shot 13", "act": "weapon_threat"},
        {"start": 17.0, "end": 18.0, "category": "VIOLENCE", "severity": "moderate",
         "detail": "grabbed by throat, slammed", "evidence": "shot 17", "act": "physical_violence"},
        {"start": 16.0, "end": 17.0, "category": "VIOLENCE", "severity": "moderate",
         "detail": "blood on forehead", "evidence": "shot 16", "act": "injury"},
    ]
    assert _rate(findings) == "Adult 18+"


def test_alcohol_and_mild_language_stay_teen_13():
    """The floor must not over-escalate: alcohol + mild language is Teen 13+, not Adult 18+."""
    findings = [
        {"start": 7.0, "end": 8.0, "category": "SUBSTANCE", "severity": "mild",
         "detail": "beer bottle on table", "evidence": "shot 7", "act": "alcohol"},
        {"start": 20.0, "end": 21.0, "category": "LANGUAGE", "severity": "mild",
         "detail": '"damn"', "evidence": "transcript", "act": None},
    ]
    assert _rate(findings) == "Teen 13+"
