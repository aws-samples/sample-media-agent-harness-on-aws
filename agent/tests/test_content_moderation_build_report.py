"""The deterministic moderation report builder (skills/content-moderation/scripts/build_report.py).

Staging session a2d759c2: three sg5 nodes of ONE plan improvised three JSON schemas (`asset.video_tag` vs
`asset.tag`, `summary` vs `counts`, three `rating_basis` shapes) and three PDF layouts, none of them the six
sections of report-template.md. The builder makes the format code: whatever the findings, the report has the
same keys in the same order and the PDF has the same six sections.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "skills" / "content-moderation" / "scripts" / "build_report.py"


def _mod():
    spec = importlib.util.spec_from_file_location("cm_build_report", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


BR = _mod()

TIDAL = {  # violent trailer: drug bricks, aimed pistol, alcohol, a threat in dialogue
    "asset": {"video_tag": "__23276af0", "file_name": "tidal_line_trailer_enUS.mp4", "duration_sec": 105.0},
    "evidence": {"shots_enriched": 23, "transcript_scanned": True, "transcript_duration_sec": 104.2,
                 "transcript_sentences": 9, "conversation_segments": 3},
    "findings": [
        {"start": 36.0, "end": 41.0, "category": "SUBSTANCE", "severity": "moderate", "source": "shot 9",
         "detail": "plastic-wrapped white bricks in a black bag", "evidence": "white bricks", "act": "drug_material"},
        {"start": 26.0, "end": 31.0, "category": "SUBSTANCE", "severity": "mild", "source": "shot 7",
         "detail": "beer bottle on the deck", "evidence": "beer bottle", "act": "alcohol"},
        {"start": 52.0, "end": 55.0, "category": "VIOLENCE", "severity": "moderate", "source": "shot 13",
         "detail": "pistol aimed at a man", "evidence": "aims a pistol", "act": "weapon_threat"},
        {"start": 60.0, "end": 62.0, "category": "LANGUAGE", "severity": "mild", "source": "transcript",
         "detail": "threat", "evidence": "They'll burn the whole damn coast down", "act": None},
    ],
    "corroboration": [{"start": 0, "end": 105, "tone": "tense", "topic": "smuggling"}],
}
NBA = {  # commentary only: mild language, injury talk
    "asset": {"video_tag": "__04226c1e", "file_name": "nba.mp4", "duration_sec": 300.1},
    "evidence": {"shots_enriched": 41, "transcript_scanned": True, "transcript_sentences": 96},
    "findings": [
        {"start": 62.0, "end": 63.0, "category": "LANGUAGE", "severity": "mild", "source": "transcript",
         "detail": "mild profanity", "evidence": "Oh, damn."},
        {"start": 0.0, "end": 3.0, "category": "HARM", "severity": "moderate", "source": "transcript",
         "detail": "injury reference", "evidence": "Ron was down after the collision", "confidence": "verified"},
    ],
}
IBC = {  # clean, no dialogue scanned
    "asset": {"video_tag": "__b3da44a3", "file_name": "ibc_intro_olive_final.mp4", "duration_sec": 117.9},
    "evidence": {"shots_enriched": 2, "transcript_scanned": False},
    "findings": [],
}


def _keys(v, path=""):
    """Every key path in the report (list items collapsed to [] by their first element)."""
    if isinstance(v, dict):
        out = []
        for k, x in v.items():
            out += [f"{path}.{k}"] + _keys(x, f"{path}.{k}")
        return out
    if isinstance(v, list) and v and isinstance(v[0], dict):
        return _keys(v[0], f"{path}[]")
    return []


def test_three_very_different_videos_get_the_same_report_shape():
    reports = [BR.build(x, generated_at="2026-10-11T00:00:00Z") for x in (TIDAL, NBA, IBC)]
    tops = [list(r.keys()) for r in reports]
    assert tops[0] == tops[1] == tops[2], tops
    assert reports[0]["schema"] == "content-moderation/v2"
    # nested shapes agree wherever both sides have rows
    assert set(_keys(reports[0]["asset"])) == set(_keys(reports[2]["asset"]))
    assert _keys(reports[0]["flagged_timeline"]) == _keys(reports[1]["flagged_timeline"])
    assert _keys(reports[0]["categories"]) == _keys(reports[1]["categories"])
    assert list(reports[0]["provenance"]) == list(reports[1]["provenance"]) == list(reports[2]["provenance"])


def test_ratings_and_assessments():
    t, n, i = (BR.build(x) for x in (TIDAL, NBA, IBC))
    assert t["suggested_rating"] == "Adult 18+" and t["assessment"]["recommendation"] == "ESCALATE"
    assert n["suggested_rating"] == "Teen 13+" and n["assessment"]["recommendation"] == "REVIEW"
    assert i["suggested_rating"] == "All Ages" and i["assessment"]["recommendation"] == "CLEAN"
    # floors applied in the report itself, not just the rating
    sev = {f["detail"]: f["severity"] for f in t["flagged_timeline"]}
    assert sev["plastic-wrapped white bricks in a black bag"] == "strong"
    assert sev["beer bottle on the deck"] == "moderate" and sev["threat"] == "moderate"
    # timeline sorted by time; categories by severity
    assert [f["start"] for f in t["flagged_timeline"]] == sorted(f["start"] for f in t["flagged_timeline"])
    assert t["categories"][0]["severity"] == "strong"


def test_categories_not_flagged_distinguishes_clean_from_not_assessed():
    i = BR.build(IBC)
    status = {c["category"]: c["status"] for c in i["categories_not_flagged"]}
    assert status["LANGUAGE"] == "not_assessed"           # no transcript scanned
    assert status["VIOLENCE"] == "attested_clean"         # shot descriptions covered it
    assert len(status) == 8
    t = BR.build({**TIDAL, "not_assessed": ["SEXUAL"]})
    st = {c["category"]: c["status"] for c in t["categories_not_flagged"]}
    assert st["SEXUAL"] == "not_assessed" and "SUBSTANCE" not in st


def test_supporting_evidence_never_sets_the_rating():
    x = {**IBC, "findings": [{"start": 1, "end": 2, "category": "FRIGHTENING", "severity": "strong",
                              "source": "shot 1", "detail": "tense music", "confidence": "supporting"}]}
    r = BR.build(x)
    assert r["suggested_rating"] == "All Ages" and r["assessment"]["recommendation"] == "CLEAN"


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d["findings"][0].update(category="Weapons"), "category must be one of"),
    (lambda d: d["findings"][0].update(severity="extreme"), "severity must be one of"),
    (lambda d: d["findings"][0].update(source="shot nine"), "source must be"),
    (lambda d: d["findings"][0].update(start=50, end=10), "is before start"),
    (lambda d: d["asset"].pop("file_name"), "asset.file_name"),
    (lambda d: d["evidence"].pop("transcript_scanned"), "transcript_scanned"),
])
def test_invalid_findings_are_rejected_precisely(mutate, message):
    d = json.loads(json.dumps(TIDAL))
    mutate(d)
    with pytest.raises(BR.FindingsError, match=message):
        BR.build(d)


def test_cli_writes_both_files_and_the_pdf_has_the_six_sections(tmp_path):
    from pypdf import PdfReader

    fin = tmp_path / "findings.json"
    fin.write_text(json.dumps(TIDAL))
    out = subprocess.run([sys.executable, str(SCRIPT), str(fin), "--out-dir", str(tmp_path)],
                         capture_output=True, text=True, check=True)
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res["suggested_rating"] == "Adult 18+"
    report = json.loads((tmp_path / "tidal_line_trailer_enUS_compliance_report.json").read_text())
    assert report["schema"] == "content-moderation/v2"
    text = "\n".join(p.extract_text() or "" for p in PdfReader(str(tmp_path / "tidal_line_trailer_enUS_compliance_report.pdf")).pages)
    for title in BR.SECTION_TITLES:
        assert title in text, (title, res["pdf_writer"])


def test_minimal_pdf_writer_works_without_any_pdf_library(tmp_path):
    from pypdf import PdfReader

    path = tmp_path / "x.pdf"
    BR._pdf_minimal(BR.build(NBA), str(path))
    text = "\n".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)
    for title in BR.SECTION_TITLES:
        assert title in text


def test_cli_invalid_findings_exits_2(tmp_path):
    fin = tmp_path / "findings.json"
    fin.write_text(json.dumps({**TIDAL, "findings": [{**TIDAL["findings"][0], "category": "Weapons"}]}))
    p = subprocess.run([sys.executable, str(SCRIPT), str(fin), "--out-dir", str(tmp_path)],
                       capture_output=True, text=True)
    assert p.returncode == 2 and "INVALID FINDINGS" in p.stderr and "category must be one of" in p.stderr


def test_cli_uploads_under_the_given_prefix(tmp_path, monkeypatch):
    calls = []

    class FakeS3:
        def put_object(self, **kw):
            calls.append(kw)

    import boto3

    monkeypatch.setattr(boto3, "client", lambda *a, **k: FakeS3())
    fin = tmp_path / "f.json"
    fin.write_text(json.dumps(NBA))
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = BR.main([str(fin), "--out-dir", str(tmp_path), "--bucket", "b", "--region", "us-west-2",
                      "--prefix", "projects/p1/sessions/s1/output", "--domain", "https://cdn.example/"])
    assert rc == 0
    res = json.loads(buf.getvalue().strip())
    assert [c["Key"] for c in calls] == ["projects/p1/sessions/s1/output/nba_compliance_report.json",
                                         "projects/p1/sessions/s1/output/nba_compliance_report.pdf"]
    assert [c["ContentType"] for c in calls] == ["application/json", "application/pdf"]
    assert res["pdf_url"] == "https://cdn.example/projects/p1/sessions/s1/output/nba_compliance_report.pdf"
