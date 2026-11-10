#!/usr/bin/env python3
"""Deterministic content-moderation report builder: findings.json -> <stem>_compliance_report.{json,pdf}.

The sg5 node's model does ONE thing: classify the evidence into a findings file (schema below). Everything
else -- the severity floors, the rating, the report's structure and the PDF's six sections -- is THIS code, so
every video, every run and every model produces the same report shape. (Staging session a2d759c2: three sg5
nodes improvised three different JSON schemas and three different PDF layouts for one plan.)

Usage (one code_interpreter cell; the harness stages this script into the sandbox):

    python scripts/build_report.py findings.json --bucket <B> --region <R> --prefix <OUTPUT prefix> \
        --domain <ARTIFACTS_DOMAIN>

Without --bucket it only writes the two files locally. It prints ONE JSON line:
{"suggested_rating", "json_url", "pdf_url", "json_key", "pdf_key"} -- the node's final output is the two URLs.
An invalid findings file exits 2 with a precise message: fix the findings, never hand-write a report.

findings.json (content-moderation/findings-v1):

    {
      "asset": {"video_tag": str, "file_name": str, "duration_sec": number},
      "evidence": {                                   # what the classifier actually covered
        "shots_enriched": int,                        # shots with a usable description (sg3)
        "shots_without_description": int,             # optional, default 0
        "transcript_scanned": bool,                   # sg2 transcript read in full
        "transcript_duration_sec": number | null,     # optional
        "transcript_sentences": int,                  # optional
        "conversation_segments": int                  # optional, sg4 segments used for corroboration
      },
      "findings": [                                   # one per flagged moment
        {"start": sec, "end": sec,
         "category": one of CATEGORIES,
         "severity": "mild" | "moderate" | "strong",
         "source": "shot <N>" | "transcript",
         "detail": str,                               # short description
         "evidence": str,                             # verbatim quote or description excerpt
         "act": str | null,                           # the shot's depicted[] act, verbatim (visual findings)
         "confidence": "verified" | "supporting"}     # optional, default "verified"
      ],
      "corroboration": [{"start": sec, "end": sec, "tone": str, "topic": str}],   # optional, sg4
      "not_assessed": [category, ...]                 # optional: categories the evidence could not cover
    }
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
from typing import Any, Optional

SCHEMA_IN = "content-moderation/findings-v1"
SCHEMA_OUT = "content-moderation/v2"

CATEGORIES = ("VIOLENCE", "HARM", "LANGUAGE", "SEXUAL", "SUBSTANCE", "FRIGHTENING", "DISCRIMINATION", "IMITABLE")
SEVERITIES = ("mild", "moderate", "strong")
RATINGS = ("All Ages", "Child 7+", "Teen 13+", "Adult 18+")

# ---- the rating rules (moved verbatim from SKILL.md step 5; rating-system.md is the canonical table) ----------
_BY_SEV = {"strong": "Adult 18+", "moderate": "Teen 13+", "mild": "Child 7+"}
_RANK = {"mild": 0, "moderate": 1, "strong": 2}
# mild language => Teen 13+ (rating-system.md): any spoken LANGUAGE finding is AT LEAST moderate (session d6a560a8).
_CATEGORY_FLOOR = {"LANGUAGE": "moderate"}
# The controlled depicted[] act sets the MINIMUM severity: the model's gestalt per-finding severity is not
# reproducible (sessions 9d8c215e vs b8a59e30 rated the SAME video Teen 13+ and Adult 18+).
_ACT_FLOOR = {
    # SUBSTANCE
    "drug_material": "strong", "drug_use": "strong",
    "alcohol": "moderate", "smoking": "moderate",
    # VIOLENCE
    "weapon_threat": "strong",      # weapon aimed/pointed/fired AT a person
    "firearm_discharge": "strong", "physical_violence": "strong",
    "injury": "strong", "blood": "strong", "gore": "strong",
    "firearm": "moderate",          # weapon merely present/carried
    "peril": "moderate",
}

STANDING_LIMITATION = ("Shot descriptions summarize each shot, so a brief or background visual event can be "
                       "omitted: absence of a visual flag is not proof of absence. ESCALATE routes flagged spans "
                       "to human review.")


class FindingsError(ValueError):
    """The findings file does not match content-moderation/findings-v1."""


# ---------------------------------------------------------------------------------------------- validation
def _num(v: Any, where: str, *, allow_none: bool = False) -> Optional[float]:
    if v is None and allow_none:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise FindingsError(f"{where} must be a number, got {v!r}")
    if v < 0:
        raise FindingsError(f"{where} must be >= 0, got {v!r}")
    return float(v)


def _int(v: Any, where: str, default: Optional[int] = None) -> int:
    if v is None and default is not None:
        return default
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise FindingsError(f"{where} must be a non-negative integer, got {v!r}")
    return v


def _str(v: Any, where: str, *, required: bool = True) -> str:
    if v is None and not required:
        return ""
    if not isinstance(v, str) or (required and not v.strip()):
        raise FindingsError(f"{where} must be a non-empty string, got {v!r}")
    return v.strip()


def validate(raw: Any) -> dict:
    """Return a normalized copy of the findings, or raise FindingsError naming the first problem."""
    if not isinstance(raw, dict):
        raise FindingsError("findings must be a JSON object")
    asset = raw.get("asset")
    if not isinstance(asset, dict):
        raise FindingsError("asset must be an object {video_tag, file_name, duration_sec}")
    out_asset = {
        "video_tag": _str(asset.get("video_tag"), "asset.video_tag"),
        "file_name": _str(asset.get("file_name"), "asset.file_name"),
        "duration_sec": _num(asset.get("duration_sec"), "asset.duration_sec"),
    }
    ev = raw.get("evidence")
    if not isinstance(ev, dict):
        raise FindingsError("evidence must be an object (shots_enriched, transcript_scanned, ...)")
    if not isinstance(ev.get("transcript_scanned"), bool):
        raise FindingsError("evidence.transcript_scanned must be true or false")
    out_ev = {
        "shots_enriched": _int(ev.get("shots_enriched"), "evidence.shots_enriched"),
        "shots_without_description": _int(ev.get("shots_without_description"),
                                          "evidence.shots_without_description", 0),
        "transcript_scanned": ev["transcript_scanned"],
        "transcript_duration_sec": _num(ev.get("transcript_duration_sec"), "evidence.transcript_duration_sec",
                                        allow_none=True),
        "transcript_sentences": _int(ev.get("transcript_sentences"), "evidence.transcript_sentences", 0),
        "conversation_segments": _int(ev.get("conversation_segments"), "evidence.conversation_segments", 0),
    }
    fs = raw.get("findings")
    if not isinstance(fs, list):
        raise FindingsError("findings must be a list (empty when nothing was flagged)")
    out_f = []
    for i, f in enumerate(fs):
        w = f"findings[{i}]"
        if not isinstance(f, dict):
            raise FindingsError(f"{w} must be an object")
        cat = f.get("category")
        if cat not in CATEGORIES:
            raise FindingsError(f"{w}.category must be one of {', '.join(CATEGORIES)}, got {cat!r}")
        sev = f.get("severity")
        if sev not in SEVERITIES:
            raise FindingsError(f"{w}.severity must be one of mild, moderate, strong, got {sev!r}")
        start = _num(f.get("start"), f"{w}.start")
        end = _num(f.get("end"), f"{w}.end")
        if end < start:
            raise FindingsError(f"{w}: end {end} is before start {start}")
        source = _str(f.get("source"), f"{w}.source")
        if source != "transcript" and not re.fullmatch(r"shot \d+", source):
            raise FindingsError(f"{w}.source must be 'transcript' or 'shot <N>', got {source!r}")
        act = f.get("act")
        if act is not None and not isinstance(act, str):
            raise FindingsError(f"{w}.act must be a string or null, got {act!r}")
        conf = f.get("confidence", "verified")
        if conf not in ("verified", "supporting"):
            raise FindingsError(f"{w}.confidence must be 'verified' or 'supporting', got {conf!r}")
        out_f.append({"start": start, "end": end, "category": cat, "severity": sev, "source": source,
                      "detail": _str(f.get("detail"), f"{w}.detail"),
                      "evidence": _str(f.get("evidence"), f"{w}.evidence", required=False),
                      "act": act.strip() if isinstance(act, str) and act.strip() else None,
                      "confidence": conf})
    corr = []
    for i, c in enumerate(raw.get("corroboration") or []):
        if not isinstance(c, dict):
            raise FindingsError(f"corroboration[{i}] must be an object")
        corr.append({"start": _num(c.get("start"), f"corroboration[{i}].start"),
                     "end": _num(c.get("end"), f"corroboration[{i}].end"),
                     "tone": _str(c.get("tone"), f"corroboration[{i}].tone", required=False),
                     "topic": _str(c.get("topic"), f"corroboration[{i}].topic", required=False)})
    na = raw.get("not_assessed") or []
    if not isinstance(na, list) or any(c not in CATEGORIES for c in na):
        raise FindingsError(f"not_assessed must be a list of categories from {', '.join(CATEGORIES)}")
    return {"asset": out_asset, "evidence": out_ev, "findings": out_f, "corroboration": corr,
            "not_assessed": list(dict.fromkeys(na))}


# ------------------------------------------------------------------------------------------- rating logic
def apply_floors(findings: list[dict]) -> list[dict]:
    """Raise each finding's severity to its category floor and its depicted-act floor (never lowers)."""
    out = []
    for f in findings:
        sev = f["severity"]
        for floor in (_CATEGORY_FLOOR.get(f["category"]), _ACT_FLOOR.get(f.get("act") or "")):
            if floor and _RANK[sev] < _RANK[floor]:
                sev = floor
        out.append({**f, "severity": sev})
    return out


def compute_rating(findings: list[dict]) -> str:
    """Highest severity among VERIFIED findings (after floors) -> rating. Same findings => same rating, always."""
    rating = "All Ages"
    for f in apply_floors(findings):
        if f.get("confidence", "verified") != "verified":
            continue
        lvl = _BY_SEV[f["severity"]]
        if RATINGS.index(lvl) > RATINGS.index(rating):
            rating = lvl
    return rating


def _tc(sec: float) -> str:
    sec = max(0.0, float(sec))
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    ms = int(round((sec - int(sec)) * 1000))
    if ms == 1000:
        s, ms = s + 1, 0
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def build(findings_in: dict, *, generated_at: Optional[str] = None) -> dict:
    """The canonical report (stable key order) from validated findings."""
    d = validate(findings_in)
    ev = d["evidence"]
    flagged = sorted(apply_floors(d["findings"]), key=lambda f: (f["start"], f["end"], f["category"]))
    rating = compute_rating(d["findings"])
    verified = [f for f in flagged if f["confidence"] == "verified"]

    cats = []
    for cat in CATEGORIES:
        mine = [f for f in flagged if f["category"] == cat]
        if not mine:
            continue
        top = max(mine, key=lambda f: (_RANK[f["severity"]], f["confidence"] == "verified", -f["start"]))
        cats.append({"category": cat, "severity": top["severity"], "count": len(mine),
                     "strongest_finding": f"{_tc(top['start'])} {top['detail']}"})
    cats.sort(key=lambda c: (-_RANK[c["severity"]], CATEGORIES.index(c["category"])))

    flagged_cats = {c["category"] for c in cats}
    visual_layer = ev["shots_enriched"] > 0
    spoken_layer = ev["transcript_scanned"]
    not_flagged = []
    for cat in CATEGORIES:
        if cat in flagged_cats:
            continue
        if cat in d["not_assessed"]:
            not_flagged.append({"category": cat, "status": "not_assessed",
                                "reason": "the classifier reported it could not cover this category"})
        elif cat == "LANGUAGE" and not spoken_layer:
            not_flagged.append({"category": cat, "status": "not_assessed", "reason": "no transcript was scanned"})
        elif not visual_layer and not spoken_layer:
            not_flagged.append({"category": cat, "status": "not_assessed", "reason": "no evidence layer was read"})
        else:
            layers = [n for n, on in (("shot descriptions", visual_layer and cat != "LANGUAGE"),
                                      ("transcript", spoken_layer)) if on]
            not_flagged.append({"category": cat, "status": "attested_clean",
                                "reason": "covered by " + " and ".join(layers) + "; nothing found"})

    justification = []
    for c in cats:
        mine = [f for f in verified if f["category"] == c["category"] and f["severity"] == c["severity"]]
        if mine:
            f = mine[0]
            justification.append(f"{c['category']} ({c['severity']}) at {_tc(f['start'])}-{_tc(f['end'])}, "
                                  f"{f['source']}: {f['detail']}")
    if not justification:
        justification.append("No verified finding in any category.")

    top_sev = max((_RANK[f["severity"]] for f in verified), default=-1)
    profile = {-1: "none", 0: "mild", 1: "moderate", 2: "severe"}[top_sev]
    recommendation = "CLEAN" if top_sev < 0 else ("ESCALATE" if top_sev == 2 else "REVIEW")
    reason = {"CLEAN": "No verified findings in any category.",
              "REVIEW": "Verified findings of at most moderate severity.",
              "ESCALATE": "High-severity verified findings: route the flagged spans to human review."}[recommendation]

    return {
        "schema": SCHEMA_OUT,
        "generated_at": generated_at or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "builder": "content-moderation/scripts/build_report.py",
        "asset": d["asset"],
        "suggested_rating": rating,
        "rating_scale": list(RATINGS),
        "rating_justification": justification,
        "elements": [c["category"] for c in cats],
        "categories": cats,
        "flagged_timeline": [{"start": f["start"], "end": f["end"], "timecode": f"{_tc(f['start'])}-{_tc(f['end'])}",
                              "category": f["category"], "severity": f["severity"], "source": f["source"],
                              "detail": f["detail"], "evidence": f["evidence"], "act": f["act"],
                              "confidence": f["confidence"]} for f in flagged],
        "categories_not_flagged": not_flagged,
        "provenance": {**ev, "corroboration": d["corroboration"], "standing_limitation": STANDING_LIMITATION},
        "assessment": {"profile": profile, "recommendation": recommendation, "reason": reason},
    }


# ---------------------------------------------------------------------------------------------- PDF output
SECTION_TITLES = ("1. General Rating System Analysis", "2. Content Compliance Summary",
                  "3. Flagged Moments Timeline", "4. Categories Not Flagged", "5. Evidence Provenance",
                  "6. Overall Assessment")


def _sections(r: dict) -> list[tuple[str, list[str], list[list[str]]]]:
    """(title, paragraphs, table rows incl. header) for each of the six sections -- shared by every PDF writer."""
    a, p, s = r["asset"], r["provenance"], r["assessment"]
    return [
        (SECTION_TITLES[0],
         [f"Suggested rating: {r['suggested_rating']}  (scale: {' | '.join(r['rating_scale'])}; internal scale, "
          "not a real-world rating)",
          "Justification:", *[f"- {j}" for j in r["rating_justification"]],
          "Elements: " + (", ".join(r["elements"]) or "none")], []),
        (SECTION_TITLES[1], [] if r["categories"] else ["No category flagged."],
         [["Category", "Severity", "Count", "Strongest finding"]]
         + [[c["category"], c["severity"], str(c["count"]), c["strongest_finding"]] for c in r["categories"]]
         if r["categories"] else []),
        (SECTION_TITLES[2], [] if r["flagged_timeline"] else ["No flagged moments."],
         [["Timestamp", "Category", "Source", "Detail"]]
         + [[f["timecode"], f"{f['category']} ({f['severity']})", f["source"],
             f["detail"] + (f' -- "{f["evidence"]}"' if f["evidence"] else "")] for f in r["flagged_timeline"]]
         if r["flagged_timeline"] else []),
        (SECTION_TITLES[3], [] if r["categories_not_flagged"] else ["Every category was flagged."],
         [["Category", "Status", "Basis"]]
         + [[c["category"], "attested clean" if c["status"] == "attested_clean" else "NOT ASSESSED", c["reason"]]
            for c in r["categories_not_flagged"]] if r["categories_not_flagged"] else []),
        (SECTION_TITLES[4],
         [f"Asset: {a['file_name']} (tag {a['video_tag']}), duration {a['duration_sec']:.1f}s",
          f"Shots enriched: {p['shots_enriched']} (without description: {p['shots_without_description']})",
          "Transcript scanned: " + ("yes" + (f", {p['transcript_duration_sec']:.1f}s"
                                              if p.get("transcript_duration_sec") is not None else "")
                                    + f", {p['transcript_sentences']} sentences" if p["transcript_scanned"] else "no"),
          f"Conversation segments used for corroboration: {p['conversation_segments']}",
          p["standing_limitation"]], []),
        (SECTION_TITLES[5],
         [f"Profile: {s['profile']}", f"Recommendation: {s['recommendation']}", s["reason"]], []),
    ]


def _pdf_reportlab(r: dict, path: str) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape

    st = getSampleStyleSheet()
    cell = st["BodyText"].clone("cell", fontSize=8, leading=10)
    story = [Paragraph(escape(f"Content Moderation Report - {r['asset']['file_name']}"), st["Title"]),
             Paragraph(escape(f"Schema {r['schema']} - generated {r['generated_at']}"), st["Normal"]),
             Spacer(1, 0.15 * inch)]
    for title, paras, rows in _sections(r):
        story.append(Paragraph(escape(title), st["Heading2"]))
        for t in paras:
            story.append(Paragraph(escape(t), st["BodyText"]))
        if rows:
            data = [[Paragraph(escape(str(c)), cell) for c in row] for row in rows]
            tbl = Table(data, repeatRows=1, hAlign="LEFT",
                        colWidths=None if len(rows[0]) != 4 else [1.3 * inch, 1.3 * inch, 0.9 * inch, 3.6 * inch])
            tbl.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eaf0")),
                                     ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#b0b4c0")),
                                     ("VALIGN", (0, 0), (-1, -1), "TOP")]))
            story.append(tbl)
        story.append(Spacer(1, 0.12 * inch))
    SimpleDocTemplate(path, pagesize=letter, title=f"Content Moderation Report - {r['asset']['file_name']}",
                      leftMargin=0.7 * inch, rightMargin=0.7 * inch).build(story)


def _latin1(t: str) -> str:
    return t.replace("–", "-").replace("—", "-").replace("’", "'").replace("“", '"') \
            .replace("”", '"').encode("latin-1", "replace").decode("latin-1")


def _pdf_fpdf(r: dict, path: str) -> None:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.set_auto_page_break(True, 15)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 15)
    pdf.multi_cell(0, 8, _latin1(f"Content Moderation Report - {r['asset']['file_name']}"))
    pdf.set_font("Helvetica", "", 9)
    pdf.multi_cell(0, 5, _latin1(f"Schema {r['schema']} - generated {r['generated_at']}"))
    for title, paras, rows in _sections(r):
        pdf.ln(2)
        pdf.set_font("Helvetica", "B", 12)
        pdf.multi_cell(0, 7, _latin1(title))
        pdf.set_font("Helvetica", "", 9)
        for t in paras:
            pdf.multi_cell(0, 5, _latin1(t))
        for i, row in enumerate(rows):
            pdf.set_font("Helvetica", "B" if i == 0 else "", 8)
            pdf.multi_cell(0, 4.5, _latin1(" | ".join(row)))
    pdf.output(path)


def _pdf_minimal(r: dict, path: str) -> None:
    """A dependency-free single-font PDF (text only) -- the last resort, so a missing library never fails sg5."""
    lines = [f"Content Moderation Report - {r['asset']['file_name']}", f"Schema {r['schema']}", ""]
    for title, paras, rows in _sections(r):
        lines += [title, *paras, *[" | ".join(row) for row in rows], ""]
    wrapped: list[str] = []
    for ln in lines:
        ln = _latin1(ln)
        while len(ln) > 100:
            wrapped.append(ln[:100])
            ln = "  " + ln[100:]
        wrapped.append(ln)
    pages = [wrapped[i:i + 60] for i in range(0, len(wrapped), 60)] or [[]]
    objs: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for pg in pages:
        esc = [ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for ln in pg]
        body = "BT /F1 9 Tf 12 TL 40 760 Td " + " ".join(f"({t}) '" for t in esc) + " ET"
        stream = body.encode("latin-1")
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        content_id = len(objs)
        objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> "
                    b"/Contents %d 0 R >>" % content_id)
        kids.append(len(objs))
    objs[1] = b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids) + b"] /Count %d >>" % len(kids)
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    with open(path, "wb") as fh:
        fh.write(bytes(out))


def write_pdf(r: dict, path: str) -> str:
    """Write the PDF with the best available writer; returns which one was used."""
    for name, fn in (("reportlab", _pdf_reportlab), ("fpdf", _pdf_fpdf), ("minimal", _pdf_minimal)):
        try:
            fn(r, path)
            return name
        except ImportError:
            continue
    raise RuntimeError("no PDF writer succeeded")  # unreachable: the minimal writer has no imports


# ------------------------------------------------------------------------------------------------- CLI
def stem_of(file_name: str) -> str:
    base = os.path.basename(file_name)
    return base.rsplit(".", 1)[0] if "." in base else base


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Build the content-moderation report from findings.json")
    ap.add_argument("findings")
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--bucket")
    ap.add_argument("--region")
    ap.add_argument("--prefix", default="", help="the session OUTPUT prefix from your S3 CONTEXT, verbatim")
    ap.add_argument("--domain", default="", help="ARTIFACTS_DOMAIN, for the durable URLs")
    a = ap.parse_args(argv)
    try:
        with open(a.findings, encoding="utf-8") as fh:
            report = build(json.load(fh))
    except (FindingsError, json.JSONDecodeError) as exc:
        print(f"INVALID FINDINGS: {exc}", file=sys.stderr)
        return 2
    stem = stem_of(report["asset"]["file_name"])
    os.makedirs(a.out_dir, exist_ok=True)
    json_path = os.path.join(a.out_dir, f"{stem}_compliance_report.json")
    pdf_path = os.path.join(a.out_dir, f"{stem}_compliance_report.pdf")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
    writer = write_pdf(report, pdf_path)
    result: dict = {"suggested_rating": report["suggested_rating"], "pdf_writer": writer,
                    "json_path": json_path, "pdf_path": pdf_path}
    if a.bucket:
        import boto3

        s3 = boto3.client("s3", region_name=a.region) if a.region else boto3.client("s3")
        prefix = a.prefix if not a.prefix or a.prefix.endswith("/") else a.prefix + "/"
        for path, ext, ctype in ((json_path, "json", "application/json"), (pdf_path, "pdf", "application/pdf")):
            key = f"{prefix}{stem}_compliance_report.{ext}"
            with open(path, "rb") as fh:
                s3.put_object(Bucket=a.bucket, Key=key, Body=fh.read(), ContentType=ctype)
            result[f"{ext}_key"] = key
            dom = a.domain.replace("https://", "").replace("http://", "").strip("/")
            result[f"{ext}_url"] = f"https://{dom}/{key}" if dom else f"s3://{a.bucket}/{key}"
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
