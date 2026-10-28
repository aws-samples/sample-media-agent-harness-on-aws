"""Lineage roots in the runtime ledger (``projects.lineageEdges``, round 2): media downloaded from OUTSIDE is named as
an external source (credential-free), and a step that read nothing is "created from scratch" — never a gap.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from media import artifact_fetch, turn_outputs  # noqa: E402
from projects import lineage_contract as lc  # noqa: E402
from test_lineage_edges_runtime import BUCKET, CDN, EXT, OUT, _Head, _ci, _entry, _text, on  # noqa: E402,F401


def test_a_cell_downloading_a_public_sample_records_the_url_as_an_external_source(on, monkeypatch):
    """versant-poc01 (session 556a8a7c): the agent downloaded sample .mxf files from the public internet in a cell
    and saved them as outputs; they registered with no lineage at all."""
    out = f"{OUT}/sample_xdcam_op1a.mxf"
    monkeypatch.setattr(artifact_fetch, "_client", lambda: _Head({out: 1_000_010.0}))
    code = ("import requests\n"
            "url = 'https://filesamples.example.com/samples/video/mxf/sample_xdcam_op1a.mxf?sig=abc&token=xyz'\n"
            "open('/tmp/a.mxf','wb').write(requests.get(url).content)\n"
            f"s3.upload_file('/tmp/a.mxf', '{BUCKET}', '{out}')\n")
    turn_outputs.note_tool_result("code_interpreter", _ci(code), _text(f"s3://{BUCKET}/{out}"))
    e = _entry(out)
    assert e["inputs"] == []
    assert e["unresolved"] == [{"ref": "https://filesamples.example.com/samples/video/mxf/sample_xdcam_op1a.mxf",
                                "reason": "external_url"}]
    assert "sig=" not in json.dumps(e) and "scratch" not in e


def test_a_download_in_one_cell_and_the_upload_in_the_next_still_names_the_source(on, monkeypatch):
    out = f"{OUT}/sample.mxf"
    monkeypatch.setattr(artifact_fetch, "_client", lambda: _Head({out: 1_000_010.0}))
    turn_outputs.note_tool_result("code_interpreter", _ci(
        "!wget -q https://cdn.public.example/v/sample.mxf -O /tmp/s.mxf"), _text("saved"))
    turn_outputs.note_tool_result("code_interpreter", _ci(f"s3.upload_file(p, B, K)\nprint('{out}')"),
                                  _text(f"s3://{BUCKET}/{out}"))
    e = _entry(out)
    assert e["unresolved"] == [{"ref": "https://cdn.public.example/v/sample.mxf", "reason": "external_url"}]
    assert e.get("inferred") is True


def test_a_cell_that_reads_nothing_is_created_from_scratch_but_one_reading_local_media_is_not(on, monkeypatch):
    chart, report = f"{OUT}/chart.png", f"{OUT}/report.pdf"
    monkeypatch.setattr(artifact_fetch, "_client", lambda: _Head({chart: 1_000_010.0, report: 1_000_011.0}))
    turn_outputs.note_tool_result("code_interpreter", _ci(
        "import matplotlib.pyplot as plt\nplt.plot([1,2])\nplt.savefig('chart.png')\n"
        f"s3.upload_file('chart.png', B, '{chart}')"), _text(f"s3://{BUCKET}/{chart}"))
    assert _entry(chart)["scratch"] is True
    turn_outputs.note_tool_result("code_interpreter", _ci(
        "from fpdf import FPDF\npdf.image('screenshot1.png')\n"
        f"pdf.output('report.pdf'); s3.upload_file('report.pdf', B, '{report}')"), _text(f"s3://{BUCKET}/{report}"))
    assert "scratch" not in _entry(report)  # it read screenshot1.png from disk: a genuine gap


def test_a_text_to_image_generation_is_from_scratch_but_a_result_fetch_is_not(on):
    turn_outputs.note_external_copy("https://v3.fal.media/files/a/t2i.png", f"s3://{BUCKET}/{EXT}")
    turn_outputs.note_tool_result("usr-fal___run_model", {"model": "flux", "prompt": "a red fox"},
                                  _text({"images": [{"url": f"https://{CDN}/{EXT}"}]}))
    assert _entry(EXT)["scratch"] is True
    ext2 = "artifacts/external/" + "12" * 12 + ".mp4"
    turn_outputs.note_external_copy("https://v3.fal.media/files/b/v.mp4", f"s3://{BUCKET}/{ext2}")
    turn_outputs.note_tool_result("usr-fal___get_result", {"request_id": "req-unknown-1"},
                                  _text({"video": {"url": f"https://{CDN}/{ext2}"}}))
    assert "scratch" not in _entry(ext2)


def test_http_request_fetches_are_the_fallback_sources_of_a_later_output(on, monkeypatch):
    out = f"{OUT}/fetched.png"
    monkeypatch.setattr(artifact_fetch, "_client", lambda: _Head({out: 1_000_010.0}))
    turn_outputs.note_tool_result("http_request", {"method": "GET", "url": "https://img.example.org/p.png?k=s"},
                                  _text("<binary>"))
    turn_outputs.note_tool_result("code_interpreter", _ci("save(data)"), _text(f"s3://{BUCKET}/{out}"))
    assert _entry(out)["unresolved"] == [{"ref": "https://img.example.org/p.png", "reason": "external_url"}]


def test_a_tracked_jobs_row_keeps_every_input_it_was_given(on):
    from jobs.strands_tool_wrap import _lineage_inputs_from_tool_use, _source_media_from_tool_use
    from jobs.models import JobRecord  # noqa: F401 - the field exists
    use = {"name": "media-processing___process_media", "input": {
        "input_s3_uri": f"s3://{BUCKET}/{OUT}/sample_xdcam_op1a.mxf",
        "reference": "https://filesamples.example.com/x/sample.mxf?sig=1", "preset": "mp4_proxy"}}
    assert _source_media_from_tool_use(use) == []  # .mxf is not playable media for the render overlay
    assert _lineage_inputs_from_tool_use(use) == [f"s3://{BUCKET}/{OUT}/sample_xdcam_op1a.mxf",
                                                  "https://filesamples.example.com/x/sample.mxf"]


def test_external_ids_are_stable_and_credential_free():
    a = lc.external_id("https://Host.example/p/x.mp4?X-Amz-Signature=1")
    assert a == lc.external_id("https://host.example/p/x.mp4") and lc.is_external_id(a)
    assert lc.external_url("s3://Public-Bucket/k/x.mxf?versionId=3") == "s3://public-bucket/k/x.mxf"
    assert lc.external_host("https://cdn.example.com/a") == "cdn.example.com"
    assert lc.parse_edge_sk(lc.edge_up_sk("video:" + "0" * 8 + "-0000-4000-8000-" + "0" * 12, a))[2] == a


def test_our_own_cdn_root_in_a_download_cell_is_not_an_external_source(on, monkeypatch):
    """staging session d829030f: a download cell that also built `f"https://{CDN}/" + key` recorded the bare CDN
    root as a second external source of the download."""
    out = f"{OUT}/public_sample.mp4"
    monkeypatch.setattr(artifact_fetch, "_client", lambda: _Head({out: 1_000_010.0}))
    monkeypatch.setenv("ARTIFACTS_DOMAIN", CDN)
    code = ("import requests\n"
            "url = 'https://download.samplelib.com/mp4/sample-5s.mp4'\n"
            "open('/tmp/s.mp4','wb').write(requests.get(url).content)\n"
            f"s3.upload_file('/tmp/s.mp4', '{BUCKET}', '{out}')\n"
            f"print('https://{CDN}/' + '{out}')\n"
            "print('see https://other.example/')\n")
    turn_outputs.note_tool_result("code_interpreter", _ci(code), _text(f"s3://{BUCKET}/{out}"))
    assert _entry(out)["unresolved"] == [{"ref": "https://download.samplelib.com/mp4/sample-5s.mp4",
                                          "reason": "external_url"}]
    assert lc.external_url("https://other.example/") is None and lc.external_url("https://other.example") is None
    assert lc.external_url("https://other.example/a.mp4") == "https://other.example/a.mp4"


_FALLBACK_CELL = ("import requests\n"
                  "urls = ['https://download.samplelib.com/mp4/sample-5s.mp4',\n"
                  "        'https://sample-videos.com/video321/mp4/360/big_buck_bunny_360p_1mb.mp4',\n"
                  "        'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4']\n"
                  "for u in urls:\n"
                  "    try:\n"
                  "        r = requests.get(u, timeout=30); r.raise_for_status(); data = r.content; break\n"
                  "    except Exception: pass\n"
                  "open('/tmp/s.mp4','wb').write(data)\n"
                  "s3.upload_file('/tmp/s.mp4', B, '{out}')\n")


def test_a_cell_trying_fallback_urls_records_only_the_one_its_output_names(on, monkeypatch):
    """staging session a4a205a9: a download cell named three sample sites and kept the first that worked; all three
    became parents of recheck_sample.mp4."""
    out = f"{OUT}/recheck_sample.mp4"
    monkeypatch.setattr(artifact_fetch, "_client", lambda: _Head({out: 1_000_010.0}))
    turn_outputs.note_tool_result("code_interpreter", _ci(_FALLBACK_CELL.format(out=out)),
                                  _text(f"downloaded https://download.samplelib.com/mp4/sample-5s.mp4 (2.7 MB)\n"
                                        f"s3://{BUCKET}/{out}"))
    assert _entry(out)["unresolved"] == [{"ref": "https://download.samplelib.com/mp4/sample-5s.mp4",
                                          "reason": "external_url"}]


def test_a_printed_host_unique_among_the_candidates_decides_too():
    picked, amb = turn_outputs.pick_external_sources(
        ["https://a.example/x.mp4", "https://b.example/y.mp4"], f"s3://{BUCKET}/{OUT}/z.mp4", "OK from b.example")
    assert (picked, amb) == (["https://b.example/y.mp4"], [])
    # ...and the output's file name, when the cell kept the remote name.
    picked, amb = turn_outputs.pick_external_sources(
        ["https://a.example/x.mp4", "https://b.example/y.mp4"], f"s3://{BUCKET}/{OUT}/y.mp4", "saved")
    assert (picked, amb) == (["https://b.example/y.mp4"], [])


def test_undecidable_fallbacks_claim_no_parent_and_are_marked_ambiguous(on, monkeypatch):
    out = f"{OUT}/recheck_sample.mp4"
    monkeypatch.setattr(artifact_fetch, "_client", lambda: _Head({out: 1_000_010.0}))
    turn_outputs.note_tool_result("code_interpreter", _ci(_FALLBACK_CELL.format(out=out)),
                                  _text(f"saved s3://{BUCKET}/{out}"))
    e = _entry(out)
    assert [u["reason"] for u in e["unresolved"]] == [lc.AMBIGUOUS_EXTERNAL] * 3
    assert "scratch" not in e
