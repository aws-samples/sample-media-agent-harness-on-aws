"""Lineage-edge capture in the runtime (``projects.lineageEdges``): the per-turn ledger (``media.turn_outputs``)
for gateway (fal) tools, async submissions, code_interpreter (other buckets, earlier cells, ffmpeg ranges),
crop_video ranges, sub-agent merges, the contract helpers, and — flag off — exactly the old ledger.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from media import artifact_fetch, lineage_ledger, turn_outputs  # noqa: E402
from projects import lineage_contract as lc  # noqa: E402

BUCKET = "skills-bucket"
M2C = "m2c-proxy-bucket"
CDN = "d123.cloudfront.net"
SID = "833a079f-1111-4222-8333-444455556666"
OUT = f"artifacts/{SID}/output"
FAL = "https://v3b.fal.media/files/b/0aa50f49/8c4ABgOJ45LpIlkl7V6pa.png"
EXT = "artifacts/external/0123456789abcdef01234567.png"


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("SKILLS_S3_BUCKET", BUCKET)
    monkeypatch.setenv("ARTIFACTS_DOMAIN", CDN)
    monkeypatch.setenv("MEDIA_PROXY_BUCKET", M2C)
    monkeypatch.delenv("ARTIFACTS_PREFIX", raising=False)
    monkeypatch.setenv("INGESTION_CONFIG", json.dumps({"frameIntervalSec": 1, "lineageEdges": True}))
    import jobs.context as jc
    tok = jc._current_session_id.set(SID)
    turn_outputs._external_by_origin.clear()
    turn_outputs._pending_jobs.clear()
    turn_outputs._ci_vars.clear()
    turn_outputs._ci_local.clear()
    turn_outputs.open_turn(now=1_000_000.0)
    yield
    jc._current_session_id.reset(tok)
    turn_outputs.open_turn()


@pytest.fixture
def off(monkeypatch):
    monkeypatch.setenv("SKILLS_S3_BUCKET", BUCKET)
    monkeypatch.setenv("ARTIFACTS_DOMAIN", CDN)
    monkeypatch.delenv("INGESTION_CONFIG", raising=False)
    monkeypatch.delenv("LINEAGE_EDGES", raising=False)
    turn_outputs.open_turn(now=1_000_000.0)
    yield
    turn_outputs.open_turn()


class _Head:
    def __init__(self, modified: dict):
        self.modified = modified

    def head_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.modified:
            raise RuntimeError("404")
        return {"LastModified": datetime.fromtimestamp(self.modified[Key], tz=timezone.utc)}


def _text(obj) -> dict:
    return {"status": "success", "content": [{"text": json.dumps(obj) if not isinstance(obj, str) else obj}]}


def _ci(code: str) -> dict:
    return {"code_interpreter_input": {"action": {"type": "executeCode", "language": "python", "code": code}}}


def _entry(output_key: str, bucket: str = BUCKET) -> dict:
    loc = f"s3://{bucket}/{output_key}"
    return next(e for e in turn_outputs.lineage() if e["output"] == loc)


# ------------------------------------------------------------------ contract helpers
def test_flag_reads_ingestion_config_or_chainer_env():
    assert lc.edges_enabled({"INGESTION_CONFIG": json.dumps({"lineageEdges": True})})
    assert not lc.edges_enabled({"INGESTION_CONFIG": json.dumps({"frameIntervalSec": 1})})
    assert not lc.edges_enabled({"INGESTION_CONFIG": "{not json lineageEdges"})
    assert lc.edges_enabled({"LINEAGE_EDGES": "true"})
    assert not lc.edges_enabled({})


def test_settings_allowlist_drops_urls_secrets_and_stays_under_1kb():
    s = lc.sanitize_settings({
        "model": "fal-ai/flux-pro/kontext", "prompt": "make it a watercolor", "seed": 42, "strength": 0.6,
        "image_url": "https://v3.fal.media/x.png", "api_key": "sk-123", "callback": "https://evil/cb",
        "arguments": {"guidance_scale": 3.5, "num_inference_steps": 28, "token": "abc"},
        "style": "see https://x.example/y", "box": [0.1, 0.2, 0.5, 0.5],
    })
    assert s == {"model": "fal-ai/flux-pro/kontext", "prompt": "make it a watercolor", "seed": 42, "strength": 0.6,
                 "guidance_scale": 3.5, "num_inference_steps": 28, "box": [0.1, 0.2, 0.5, 0.5]}
    huge = lc.sanitize_settings({"prompt": "p" * 5000, "negative_prompt": "n" * 5000, "model": "m"})
    assert len(json.dumps(huge, separators=(",", ":")).encode()) <= lc.MAX_SETTINGS_BYTES
    assert huge["model"] == "m"


def test_time_ranges_from_args_and_timecodes():
    assert lc.ranges_from_args({"start_time": 3, "end_time": 7.5}) == {"start": 3.0, "end": 7.5, "unit": "s"}
    assert lc.ranges_from_args({"arguments": {"start": "00:01:02.5", "duration": 2}}) == \
        {"start": 62.5, "end": 64.5, "unit": "s"}
    assert lc.ranges_from_args({"timestamp": 12.5}) == {"start": 12.5, "end": 12.5, "unit": "s"}
    assert lc.ranges_from_args({"frame_index": 300}) == {"start": 300, "end": 300, "unit": "frame"}
    assert lc.ranges_from_args({"prompt": "x"}) is None
    assert lc.normalize_range(9, 4) == {"start": 4.0, "end": 9.0, "unit": "s"}
    assert lc.normalize_range("nan") is None and lc.normalize_range(-1) is None


def test_parse_entry_bounds_and_strips_queries():
    e = lc.parse_entry({"output": "s3://b/o.png", "inputs": ["s3://b/i.png"] * 3 + [5],
                        "unresolved": [{"ref": "https://h/x.png?X-Amz-Signature=abc", "reason": "external_url"}],
                        "origin": "https://V3.fal.media/f.png?sig=1", "ranges": [{"input": "s3://b/i.png", "start": 1}],
                        "settings": {"model": "m", "api_key": "k"}, "via": "nope", "jobId": "j1"})
    assert e == {"output": "s3://b/o.png", "inputs": ["s3://b/i.png"],
                 "unresolved": [{"ref": "https://h/x.png", "reason": "external_url"}],
                 "origin": "https://v3.fal.media/f.png", "ranges": [{"input": "s3://b/i.png", "start": 1.0, "end": 1.0,
                                                                      "unit": "s"}],
                 "settings": {"model": "m"}, "jobId": "j1"}
    assert lc.parse_entry({"inputs": []}) is None


# ------------------------------------------------------------------ fal / gateway tools
def test_fal_sync_output_is_the_mirrored_copy_made_from_the_frame(on):
    frame = f"https://{CDN}/{OUT}/frame_0012.jpg"
    # The tool wrapper mirrors fal's URL into our bucket BEFORE the hook sees the (rewritten) result.
    turn_outputs.note_external_copy(FAL, f"s3://{BUCKET}/{EXT}")
    turn_outputs.note_tool_result(
        "usr-fal-1a2b___run_model",
        {"model": "fal-ai/flux-pro/kontext", "prompt": "watercolor", "image_url": frame, "seed": 7},
        _text({"images": [{"url": f"https://{CDN}/{EXT}"}]}))
    e = _entry(EXT)
    assert e["inputs"] == [f"s3://{BUCKET}/{OUT}/frame_0012.jpg"]
    assert e["tool"] == "usr-fal-1a2b___run_model"
    assert e["origin"] == FAL
    assert e["settings"] == {"model": "fal-ai/flux-pro/kontext", "prompt": "watercolor", "seed": 7}


def test_a_fal_url_from_an_earlier_turn_resolves_to_our_copy(on):
    turn_outputs.note_external_copy(FAL, f"s3://{BUCKET}/{EXT}")
    turn_outputs.open_turn(now=1_000_100.0)  # next turn: the origin map survives, the ledger does not
    ext2 = "artifacts/external/fedcba9876543210fedcba98.mp4"
    turn_outputs.note_external_copy("https://v3.fal.media/files/z/clip.mp4", f"s3://{BUCKET}/{ext2}")
    turn_outputs.note_tool_result("usr-fal-1a2b___image_to_video", {"image_url": FAL + "?token=zz", "duration": "5"},
                                  _text({"video": {"url": f"https://{CDN}/{ext2}"}}))
    assert _entry(ext2)["inputs"] == [f"s3://{BUCKET}/{EXT}"]
    assert "unresolved" not in _entry(ext2)


def test_an_unknown_external_input_is_kept_unresolved_without_its_query(on):
    turn_outputs.note_external_copy("https://v3.fal.media/files/out.png", f"s3://{BUCKET}/{EXT}")
    turn_outputs.note_tool_result("usr-fal___run_model", {"image_url": "https://cdn.other.example/in.jpg?sig=SECRET"},
                                  _text({"images": [{"url": f"https://{CDN}/{EXT}"}]}))
    e = _entry(EXT)
    assert e["inputs"] == []
    assert e["unresolved"] == [{"ref": "https://cdn.other.example/in.jpg", "reason": "external_url"}]
    assert "SECRET" not in json.dumps(turn_outputs.lineage())


def test_async_submission_inputs_reach_the_result_call(on):
    frame = f"s3://{BUCKET}/{OUT}/still.png"
    turn_outputs.note_tool_result("usr-fal___submit_job", {"model": "kling", "image_url": frame, "duration": "5"},
                                  _text({"request_id": "req-1234567", "status": "IN_QUEUE"}))
    assert turn_outputs.lineage() == []
    fal_video = "https://v3.fal.media/files/v/out.mp4"
    turn_outputs.note_tool_result("usr-fal___get_result", {"request_id": "req-1234567"},
                                  _text({"video": {"url": fal_video}}))
    # Not mirrored yet: claimed when the mirror copies it.
    ext = "artifacts/external/aaaabbbbccccddddeeeeffff.mp4"
    turn_outputs.note_external_copy(fal_video, f"s3://{BUCKET}/{ext}")
    e = _entry(ext)
    assert e["inputs"] == [frame] and e["tool"] == "usr-fal___submit_job"
    assert e["settings"]["model"] == "kling" and e["jobId"] == "req-1234567" and e["origin"] == fal_video


def test_readers_record_nothing(on):
    turn_outputs.note_external_copy(FAL, f"s3://{BUCKET}/{EXT}")
    turn_outputs.note_tool_result("project-search___search", {"query": "beach", "image_url": f"https://{CDN}/{EXT}"},
                                  _text({"hits": [{"url": f"https://{CDN}/{EXT}"}]}))
    # The search claims nothing: the copy is reported only with its origin (no inputs, no tool), so the registrar
    # can attach lineage parked under that URL.
    assert turn_outputs.lineage() == [{"output": f"s3://{BUCKET}/{EXT}", "inputs": [], "tool": "", "origin": FAL}]


def test_a_fresh_own_output_a_gateway_wrote_is_an_output(on, monkeypatch):
    monkeypatch.setattr(artifact_fetch, "_client", lambda: _Head({f"{OUT}/stitched.mp4": 1_000_050.0}))
    turn_outputs.note_tool_result("media-tools___stitch", {"clips": [f"s3://{M2C}/wf/x/shot_1.mp4"]},
                                  _text({"output": f"s3://{BUCKET}/{OUT}/stitched.mp4"}))
    assert _entry(f"{OUT}/stitched.mp4")["inputs"] == [f"s3://{M2C}/wf/x/shot_1.mp4"]


def test_the_20_input_cap_is_lifted(on):
    ins = [f"s3://{BUCKET}/{OUT}/clip_{i:02d}.mp4" for i in range(30)]
    turn_outputs.note_write(f"s3://{BUCKET}/{OUT}/final.mp4", ins, tool="code_interpreter")
    assert len(_entry(f"{OUT}/final.mp4")["inputs"]) == 30


def test_flag_off_gateway_tools_record_nothing_and_the_cap_stays(off):
    turn_outputs.note_external_copy(FAL, f"s3://{BUCKET}/{EXT}")
    turn_outputs.note_tool_result("usr-fal___run_model", {"image_url": f"https://{CDN}/{OUT}/f.jpg"},
                                  _text({"images": [{"url": f"https://{CDN}/{EXT}"}]}))
    assert turn_outputs.lineage() == []
    ins = [f"s3://{BUCKET}/{OUT}/clip_{i:02d}.mp4" for i in range(30)]
    turn_outputs.note_write(f"s3://{BUCKET}/{OUT}/final.mp4", ins, tool="code_interpreter",
                            ranges=[{"input": ins[0], "start": 1, "end": 2}], settings={"model": "m"})
    (e,) = turn_outputs.lineage()
    assert len(e["inputs"]) == 20 and set(e) == {"output", "inputs", "tool"}


# ------------------------------------------------------------------ code_interpreter
def test_code_interpreter_reads_from_the_m2c_bucket_and_records_the_ffmpeg_range(on, monkeypatch):
    out = f"{OUT}/frame_12s.jpg"
    monkeypatch.setattr(artifact_fetch, "_client", lambda: _Head({out: 1_000_010.0}))
    code = (f"s3.download_file('{M2C}', 'micro-workflow/media-processing/e1/transcoded.mp4', '/tmp/src.mp4')\n"
            "subprocess.run(['ffmpeg', '-ss', '12.5', '-i', '/tmp/src.mp4', '-frames:v', '1', '/tmp/frame_12s.jpg'])\n"
            f"s3.upload_file('/tmp/frame_12s.jpg', '{BUCKET}', '{out}')\n")
    turn_outputs.note_tool_result("code_interpreter", _ci(code), _text(f"uploaded s3://{BUCKET}/{out}"))
    e = _entry(out)
    src = f"s3://{M2C}/micro-workflow/media-processing/e1/transcoded.mp4"
    assert e["inputs"] == [src]
    assert e["ranges"] == [{"input": src, "start": 12.5, "end": 12.5, "unit": "s"}]


def test_code_interpreter_inputs_carried_from_an_earlier_cell(on, monkeypatch):
    out = f"{OUT}/reel.mp4"
    monkeypatch.setattr(artifact_fetch, "_client", lambda: _Head({out: 1_000_020.0}))
    turn_outputs.note_tool_result("code_interpreter", _ci(
        f"clips = ['s3://{M2C}/wf/a/shot_1.mp4', 's3://{M2C}/wf/a/shot_2.mp4']\n"
        f"aws_cmd = 'aws s3 cp s3://{BUCKET}/{OUT}/music.wav /tmp/music.wav'\n"), _text("ok"))
    assert turn_outputs.lineage() == []  # a cell that writes nothing records nothing
    turn_outputs.note_tool_result("code_interpreter", _ci(
        "paths = [dl(c) for c in clips]\nmix('/tmp/music.wav')\n"
        f"ffmpeg_concat(paths, '/tmp/reel.mp4'); put('{out}')"), _text(f"done s3://{BUCKET}/{out}"))
    assert set(_entry(out)["inputs"]) == {f"s3://{M2C}/wf/a/shot_1.mp4", f"s3://{M2C}/wf/a/shot_2.mp4",
                                          f"s3://{BUCKET}/{OUT}/music.wav"}


def test_code_interpreter_stitch_from_files_an_earlier_cell_fetched_by_variable(on, monkeypatch):
    """The staging stitch (session 05850798): cell 1 urlretrieve(still_url, "still.jpg") with the URL in a variable;
    cell 2 concatenates the LOCAL files and uploads under a key it builds - no URL in sight."""
    final = f"projects/p{'e' * 32}/sessions/{SID}/output/watercolor_final.mp4"
    monkeypatch.setattr(artifact_fetch, "_client", lambda: _Head({final: 1_000_030.0}))
    still, clip = "artifacts/external/cdd6e06b4c35ffd4437aa031.jpg", "artifacts/external/7e8efdd4fd9b1ed2ea5ac2b9.mp4"
    turn_outputs.note_tool_result("code_interpreter", _ci(
        f'SKILLS_BUCKET="{BUCKET}"\n'
        's3.download_file(SKILLS_BUCKET,"tools/ffmpeg/ffmpeg-release-arm64-static.tar.xz","ffmpeg.tar.xz")\n'
        f'still_url="https://{CDN}/{still}"  # shot3 watercolor\n'
        f'clip_url="https://{CDN}/{clip}"\n'
        'urllib.request.urlretrieve(still_url,"still.jpg")\nurllib.request.urlretrieve(clip_url,"clip.mp4")\n'),
        _text("still 558599 clip 10882695"))
    turn_outputs.note_tool_result("code_interpreter", _ci(
        'r=subprocess.run(["ffmpeg","-y","-loop","1","-i","still.jpg","-t","2","still_seg.mp4"])\n'
        'r=subprocess.run(["ffmpeg","-y","-i","clip.mp4","clip_norm.mp4"])\n'
        f'prefix="projects/p{"e" * 32}/sessions/{SID}/output/"\nkey=prefix+"watercolor_final.mp4"\n'
        's3.upload_file("watercolor_final.mp4",SKILLS_BUCKET,key)\nprint("uploaded", key)'),
        _text(f"uploaded {final}"))
    e = _entry(final)
    assert set(e["inputs"]) == {f"s3://{BUCKET}/{still}", f"s3://{BUCKET}/{clip}"}  # never the ffmpeg tarball


def test_code_interpreter_never_takes_a_mirrored_copy_as_its_output_and_falls_back_to_this_turns_fetches(
        on, monkeypatch):
    """Staging session 71f1073f: the animated clip was mirrored this turn (get_plan_results), so the stitch cell
    that only READ it looked like its writer; and the local paths came from os.path.join, not literals."""
    still, clip = "artifacts/external/e7ae6fc6c020720a9b6c4818.jpg", "artifacts/external/a619a5d7d5e2c547cbfd1455.mp4"
    final = f"projects/p{'e' * 32}/sessions/{SID}/output/watercolor_final_v2.mp4"
    turn_outputs.note_external_copy("https://v3b.fal.media/files/b/x/minimax-h3.mp4", f"s3://{BUCKET}/{clip}")
    monkeypatch.setattr(artifact_fetch, "_client", lambda: _Head({clip: 1_000_005.0, final: 1_000_040.0}))
    turn_outputs.note_tool_result("code_interpreter", _ci(
        "WORK='/tmp/w'\n"
        f"still_url='https://{CDN}/{still}'\nclip_url='https://{CDN}/{clip}'\n"
        "still_p=os.path.join(WORK,'shot3.jpg'); clip_p=os.path.join(WORK,'clip.mp4')\n"
        "urllib.request.urlretrieve(still_url, still_p)\nurllib.request.urlretrieve(clip_url, clip_p)\n"),
        _text("w 1376 h 736"))
    assert all(e["output"] != f"s3://{BUCKET}/{clip}" or not e.get("inputs") for e in turn_outputs.lineage())
    turn_outputs.note_tool_result("code_interpreter", _ci(
        "subprocess.run(['ffmpeg','-y','-f','concat','-i',lst,out_p])\ns3.upload_file(out_p, B, key)\nprint(key)"),
        _text(final))
    e = _entry(final)
    assert set(e["inputs"]) == {f"s3://{BUCKET}/{still}", f"s3://{BUCKET}/{clip}"} and e["inferred"] is True


def test_code_interpreter_rebinding_a_variable_drops_its_old_refs(on, monkeypatch):
    out = f"{OUT}/x.mp4"
    monkeypatch.setattr(artifact_fetch, "_client", lambda: _Head({out: 1_000_020.0}))
    turn_outputs.note_tool_result("code_interpreter", _ci(f"src = 's3://{M2C}/wf/a/old.mp4'"), _text("ok"))
    turn_outputs.note_tool_result("code_interpreter", _ci(f"src = make()\nrun(src, '{out}')"),
                                  _text(f"s3://{BUCKET}/{out}"))
    # Not an OBSERVED input any more: at most the turn's-fetches fallback, which is marked inferred.
    e = _entry(out)
    assert e["inputs"] == [] or e.get("inferred") is True


def test_crop_video_records_its_trim_and_box(on):
    src = f"https://{CDN}/{OUT}/source.mp4"
    turn_outputs.note_tool_result("crop_video", {"source_url": src, "box": "0.1,0.2,0.5,0.5", "trim": "3,8"},
                                  _text({"status": "cropped", "key": f"{OUT}/crop.mp4", "trim": [3, 8]}))
    e = _entry(f"{OUT}/crop.mp4")
    assert e["ranges"] == [{"input": f"s3://{BUCKET}/{OUT}/source.mp4", "start": 3.0, "end": 8.0, "unit": "s"}]
    assert e["settings"] == {"box": "0.1,0.2,0.5,0.5"}


# ------------------------------------------------------------------ sub-agents / plan nodes
def test_merge_lineage_takes_only_our_output_locations(on):
    n = turn_outputs.merge_lineage([
        {"output": f"s3://{BUCKET}/{OUT}/sub.mp4", "inputs": [f"s3://{M2C}/wf/a.mp4"], "tool": "code_interpreter",
         "ranges": [{"input": f"s3://{M2C}/wf/a.mp4", "start": 1, "end": 4}]},
        {"output": "s3://someone-elses-bucket/x.mp4", "inputs": [f"s3://{BUCKET}/{OUT}/sub.mp4"]},
        {"output": f"s3://{BUCKET}/projects/p{'a' * 32}/a1/v.mp4", "inputs": []},  # an asset's bytes, not an output
    ], via="task_agent")
    assert n == 1
    e = _entry(f"{OUT}/sub.mp4")
    assert e["via"] == "task_agent" and e["ranges"][0]["end"] == 4.0


def test_node_ledger_is_written_beside_the_node_output(on):
    class S3:
        def __init__(self):
            self.put = {}

        def put_object(self, Bucket, Key, Body, ContentType):  # noqa: N803
            self.put[(Bucket, Key)] = json.loads(Body)

    turn_outputs.note_write(f"s3://{BUCKET}/{OUT}/n.mp4", [f"s3://{BUCKET}/{OUT}/in.mp4"], tool="code_interpreter")
    s3 = S3()
    assert lineage_ledger.write_node_lineage(s3, BUCKET, SID, "plan-1", "assemble") == 1
    doc = s3.put[(BUCKET, f"plans/{SID}/plan-1/assemble/lineage.json")]
    assert doc["lineage"][0]["via"] == "plan_node"
    assert len(lineage_ledger.node_hooks()) == 1


def test_node_ledger_flag_off_writes_nothing(off):
    turn_outputs.note_write(f"s3://{BUCKET}/{OUT}/n.mp4", [], tool="x")
    assert lineage_ledger.write_node_lineage(object(), BUCKET, SID, "p", "n") == 0
    assert lineage_ledger.node_hooks() == []
