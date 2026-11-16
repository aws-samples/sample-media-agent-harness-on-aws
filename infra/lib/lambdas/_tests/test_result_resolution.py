"""The resolver must be deterministic, total, and never sink the aggregate.

These pin the three things that were actually broken in production:

  1. the endpoint rule (three separate fixes, and still left to the model to
     guess -- one run tried variant after variant of get_job_result, collected
     404/405s, and reported a video that fal had already produced as
     "unreachable");
  2. that a node which cannot be resolved degrades to kind="error" instead of
     failing the whole call, because today a failed plan produces nothing at all;
  3. that only DISPLAYABLE media becomes an artifact -- feeding status urls and
     api endpoints to the renderer is what produced empty players.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

# Loaded by path: the module ships as a Lambda layer, so it is not importable
# from either bundle's directory during tests.
_spec = importlib.util.spec_from_file_location(
    "result_resolution_under_test",
    Path(__file__).resolve().parents[1]
    / "_layers"
    / "result-resolution"
    / "python"
    / "result_resolution.py",
)
rr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rr)


# --------------------------------------------------------------------------
# The endpoint rule
# --------------------------------------------------------------------------

def test_status_url_is_authoritative_over_the_model_path():
    # Sending the full model path produced "Status check failed (405)".
    app = rr.queue_app_id(
        "fal-ai/stable-audio-3/small/music/text-to-audio",
        "https://queue.fal.run/fal-ai/stable-audio-3/requests/abc/status",
    )
    assert app == "fal-ai/stable-audio-3"


def test_model_path_falls_back_to_its_first_two_segments():
    assert rr.queue_app_id("bytedance/seedance-2.0/fast/image-to-video") == "bytedance/seedance-2.0"


def test_a_status_url_without_requests_is_not_parsed():
    # "https://queue.fal.run/x/status" once yielded "x/status" -- a parse that
    # looked successful and produced nonsense.
    assert rr.queue_app_id("owner/app/extra", "https://queue.fal.run/x/status") == "owner/app"


def test_seedance_shape_is_recognised_without_a_status_url():
    h = rr.queue_handle({"request_id": "r1", "model": "bytedance/seedance-2.0/fast/image-to-video"})
    assert h == {"job_id": "r1", "status_url": "", "endpoint_id": "bytedance/seedance-2.0"}


def test_stable_audio_shape_is_recognised_without_a_model_field():
    h = rr.queue_handle({
        "job_id": "j1",
        "request_id": "r1",
        "status_url": "https://queue.fal.run/fal-ai/stable-audio-25/requests/r1/status",
    })
    assert h["job_id"] == "r1"
    assert h["endpoint_id"] == "fal-ai/stable-audio-25"


def test_a_value_with_neither_endpoint_nor_status_url_is_not_a_handle():
    assert rr.queue_handle({"request_id": "r1"}) is None
    assert rr.queue_handle("not a dict") is None


# --------------------------------------------------------------------------
# Totality: a bad node must not sink the aggregate
# --------------------------------------------------------------------------

def test_a_failing_reader_degrades_to_an_error_record():
    def boom(bucket, key):
        raise RuntimeError("AccessDenied")

    rec = rr.resolve_node("t1", {"status": "completed", "value": "s3://b/k.json"}, read_s3_json=boom)
    assert rec["kind"] == rr.KIND_ERROR
    assert "AccessDenied" in rec["error"]
    assert rec["node_id"] == "t1"


def test_a_failing_queue_resolver_degrades_to_an_error_record():
    def boom(handle):
        raise RuntimeError("405")

    rec = rr.resolve_node(
        "t1",
        {"status": "completed", "value": {"request_id": "r", "model": "a/b"}},
        resolve_queue=boom,
    )
    assert rec["kind"] == rr.KIND_ERROR
    assert "405" in rec["error"]
    # The handle survives on the record so a human can still chase it.
    assert rec["handle"]["endpoint_id"] == "a/b"


def test_one_bad_node_still_yields_the_others():
    def read(bucket, key):
        if key == "bad.json":
            raise RuntimeError("nope")
        return {"n_shots": 94}, 20

    agg = rr.build_aggregate(
        plan_id="p1",
        status="FAILED",
        nodes={
            "good": {"status": "completed", "value": "s3://b/ok.json"},
            "bad": {"status": "completed", "value": "s3://b/bad.json"},
        },
        read_s3_json=read,
    )
    kinds = {n["node_id"]: n["kind"] for n in agg["nodes"]}
    assert kinds == {"good": rr.KIND_ANALYSIS, "bad": rr.KIND_ERROR}
    assert agg["counts"] == {rr.KIND_ANALYSIS: 1, rr.KIND_ERROR: 1}


def test_resolve_node_never_raises_on_a_hostile_entry():
    for hostile in (None, 7, "string", [], {"value": object()}):
        rec = rr.resolve_node("n", hostile)
        assert rec["node_id"] == "n"
        assert rec["kind"] in (rr.KIND_ERROR, rr.KIND_PENDING, rr.KIND_ANALYSIS, rr.KIND_MEDIA)


# --------------------------------------------------------------------------
# Only displayable media becomes an artifact
# --------------------------------------------------------------------------

def test_only_media_urls_become_artifacts():
    resolved = {
        "video": {"url": "https://cdn/x_combined_output.mp4"},
        "status_url": "https://queue.fal.run/a/b/requests/r/status",
        "response_url": "https://queue.fal.run/a/b/requests/r",
    }
    arts = rr.artifacts_from(resolved)
    assert [a["url"] for a in arts] == ["https://cdn/x_combined_output.mp4"]
    assert arts[0]["media_kind"] == "video"


def test_a_resolved_queue_job_becomes_a_media_record():
    rec = rr.resolve_node(
        "t3_merge",
        {"status": "completed", "value": {"request_id": "r", "model": "fal-ai/ffmpeg-api"}},
        resolve_queue=lambda h: {"video": {"url": "https://cdn/combined.mp4"}},
    )
    assert rec["kind"] == rr.KIND_MEDIA
    assert rec["artifacts"][0]["url"] == "https://cdn/combined.mp4"


def test_an_unsettled_queue_job_is_pending_not_an_error():
    rec = rr.resolve_node(
        "t1",
        {"status": "completed", "value": {"request_id": "r", "model": "a/b"}},
        resolve_queue=lambda h: {},
    )
    assert rec["kind"] == rr.KIND_PENDING
    assert rec["handle"]["job_id"] == "r"


def test_an_s3_media_ref_needs_no_reader():
    rec = rr.resolve_node("t2", {"status": "completed", "value": "s3://b/proxy/transcoded.mp4"})
    assert rec["kind"] == rr.KIND_MEDIA
    assert rec["artifacts"][0]["media_kind"] == "video"


def test_s3_json_is_inlined_as_findings():
    rec = rr.resolve_node(
        "t4",
        {"status": "completed", "value": {"s3uri": "s3://b/shots.json"}},
        read_s3_json=lambda b, k: ({"n_shots": 94, "n_recurrence_groups": 6}, 40),
    )
    assert rec["kind"] == rr.KIND_ANALYSIS
    assert rec["findings"]["n_shots"] == 94


def test_oversized_json_is_reported_rather_than_inlined():
    rec = rr.resolve_node(
        "t4",
        {"status": "completed", "value": "s3://b/huge.json"},
        read_s3_json=lambda b, k: (None, rr.MAX_INLINE_FINDINGS_BYTES + 1),
    )
    assert rec["kind"] == rr.KIND_ERROR
    assert "inline limit" in rec["error"]


# --------------------------------------------------------------------------
# The aggregate
# --------------------------------------------------------------------------

def test_a_failed_plan_still_publishes_an_aggregate():
    # The half that makes failures visible: today a failed plan yields nothing.
    agg = rr.build_aggregate(
        plan_id="p1",
        status="FAILED",
        nodes={"t1": {"status": "failed", "error": "403 HeadObject Forbidden"}},
        display_name="Analysis",
    )
    assert agg["status"] == "FAILED"
    assert agg["nodes"][0]["kind"] == rr.KIND_ERROR
    assert "403" in agg["nodes"][0]["error"]
    assert agg["schema"] == "plan-result/v1"


def test_nodes_are_ordered_so_the_artifact_is_stable():
    agg = rr.build_aggregate(plan_id="p", status="SUCCEEDED", nodes={"b": {}, "a": {}, "c": {}})
    assert [n["node_id"] for n in agg["nodes"]] == ["a", "b", "c"]


def test_artifact_count_is_summed_across_nodes():
    agg = rr.build_aggregate(
        plan_id="p",
        status="SUCCEEDED",
        nodes={
            "v": {"status": "completed", "value": "s3://b/a.mp4"},
            "a": {"status": "completed", "value": "s3://b/a.wav"},
        },
    )
    assert agg["artifact_count"] == 2


def test_aggregate_json_survives_unserialisable_values():
    payload = rr.aggregate_json(
        plan_id="p", status="SUCCEEDED", nodes={"n": {"status": "completed", "value": object()}}
    )
    assert '"schema": "plan-result/v1"' in payload


# ---------------------------------------------------------------------------
# REAL production shapes. Every one of these was a defect the invented fixtures
# above missed; each was found by replaying live manifests through the resolver.
# ---------------------------------------------------------------------------

def test_m2c_shape_reads_inline_data_and_never_leaks_the_input_video():
    """Session 842bb417's t3_detect_faces, verbatim in structure.

    Two defects lived here. `output.s3uri` is a PREFIX the result files sit under,
    not a file -- reading it as JSON turned four healthy analyses into NoSuchKey
    errors. And `input.s3uri` is the SOURCE video, which a naive url walk surfaced
    as though the node had produced it: exactly the "I don't need the source
    videos" complaint.
    """
    value = {
        "operation": "recognize_faces",
        "output": {"s3uri": "s3://proxy-bucket/micro-workflow/face-recognition/59313e5d"},
        "input": {"s3uri": "s3://skills/artifacts/media-processing/4a1f252e/proxy/transcoded.mp4"},
        "data": {"frame_meta": {"wxh": [1280, 720]}, "num_faces": 5},
        "execution_arn": "arn:aws:states:us-west-2:1:execution:mediaworks-face-recognition:59313e5d",
    }
    rec = rr.resolve_node("t3_detect_faces", {"status": "completed", "value": value})
    assert rec["kind"] == rr.KIND_ANALYSIS
    assert rec["findings"]["num_faces"] == 5
    # The source video must not be presented as something this node PRODUCED.
    produced = [a for a in rec["artifacts"] if a["role"] != "source"]
    assert all("proxy/transcoded.mp4" not in a["url"] for a in produced)
    # It may appear as role="source" -- the canvas an overlay view draws on, which
    # dispatch_render carries separately as source_urls.
    sources = [a for a in rec["artifacts"] if a["role"] == "source"]
    assert sources and "proxy/transcoded.mp4" in sources[0]["url"]
    # The prefix is kept as a pointer rather than read.
    assert "face-recognition/59313e5d" in rec["handle"]["output_prefix"]


def test_a_settled_queue_node_uses_the_result_it_already_carries():
    """plan-carvideo01's nodes, verbatim in structure.

    The handle and the answer are recorded together. Treating it as an unresolved
    handle and going to the registry reported `pending` for three nodes whose urls
    were sitting in the value.
    """
    value = {
        "status": "completed",
        "endpoint_id": "fal-ai/ffmpeg-api",
        "request_id": "019fe249-a457-77a1-8aef-d106359e7002",
        "result": {"video": {"url": "https://v3b.fal.media/files/b/x/combined_output.mp4",
                             "content_type": "video/mp4", "file_size": 1377338}},
    }
    rec = rr.resolve_node("merge_av", {"status": "completed", "value": value},
                          resolve_queue=lambda h: (_ for _ in ()).throw(
                              AssertionError("registry must not be consulted")))
    assert rec["kind"] == rr.KIND_MEDIA
    assert rec["artifacts"][0]["url"].endswith("combined_output.mp4")


def test_a_bare_request_id_still_reaches_the_registry():
    """plan-dogvideo-merge's t1_video_gen, verbatim.

    No model, no status_url -- so no endpoint is derivable from the value and
    queue_handle declines it. But the registry row is keyed by that id and holds
    the resolved urls, so declining to look was how a video fal had already
    produced came back as findings=['request_id'].
    """
    seen = {}

    def resolver(handle):
        seen["job_id"] = handle["job_id"]
        return {"artifactUrls": ["https://v3b.fal.media/files/b/x/dog_video.mp4"]}

    rec = rr.resolve_node(
        "t1_video_gen",
        {"status": "completed", "value": {"request_id": "019fe261-d4ff-74b1-af8c-d962d8141f47"}},
        resolve_queue=resolver,
    )
    assert seen["job_id"] == "019fe261-d4ff-74b1-af8c-d962d8141f47"
    assert rec["kind"] == rr.KIND_MEDIA
    assert rec["artifacts"][0]["url"].endswith("dog_video.mp4")


def test_an_error_recorded_inside_a_completed_node_is_an_error():
    """plan-dogvideo-merge's t3_merge_submit, verbatim: status "completed",
    value {"error": ...}. Read as findings, a failure surfaced as a result."""
    rec = rr.resolve_node("t3_merge_submit", {
        "status": "completed",
        "value": {"error": "Missing resolved video output URL from t1_video_gen; "
                           "only request_id was provided"},
    })
    assert rec["kind"] == rr.KIND_ERROR
    assert "Missing resolved video output URL" in rec["error"]


# ---------------------------------------------------------------------------
# Canonical result files: findings must carry ANSWERS, within a size ceiling.
# ---------------------------------------------------------------------------

def _m2c(prefix_bucket="proxy-bkt", **keys):
    data = {"is_large_lambda": False, "frames_to_shots_cfg": {"threshold": 0.2}}
    data.update(keys)
    return {
        "operation": "op",
        "output": {"s3uri": f"s3://{prefix_bucket}/micro-workflow/x/exec-1"},
        "input": {"s3uri": "s3://skills/artifacts/proxy/transcoded.mp4"},
        "data": data,
    }


def test_shots_json_is_found_even_though_it_lives_under_embeddings():
    """The regression that left the shot COUNT unreachable.

    A path-based exclusion dropped anything under /embeddings/ -- which is exactly
    where shot segmentation writes shots.json (69KB) and recurrence.json. The node
    whose whole answer is a number came back with only probe.json.
    """
    value = _m2c(
        shots_key="micro-workflow/x/exec-1/embeddings/shots.json",
        probe_s3_key="micro-workflow/x/exec-1/probe.json",
    )
    names = [n for _, _, n in rr.canonical_result_refs(value)]
    assert "shots.json" in names
    assert names[0] == "shots.json", "the answer file must outrank probe.json"


def test_scratch_files_are_not_selected():
    # Excluded by simply not being on the allowlist, rather than by path guessing.
    value = _m2c(
        embeddings_key="micro-workflow/x/exec-1/embeddings/seg0000_win0001.json",
        pts_key="micro-workflow/x/exec-1/proxy/segment_0000_pts.json",
        shots_key="micro-workflow/x/exec-1/embeddings/shots.json",
    )
    names = [n for _, _, n in rr.canonical_result_refs(value)]
    assert names == ["shots.json"]


def test_answer_dense_files_outrank_their_verbose_duplicates():
    # facemetadata.json is 29KB with stats + named people; facegroup.json repeats
    # the same people in 229KB, and inlining it produced a 340KB artifact.
    value = _m2c(
        facegroup_key="micro-workflow/x/exec-1/facegroup.json",
        facemetadata_key="micro-workflow/x/exec-1/facemetadata.json",
    )
    names = [n for _, _, n in rr.canonical_result_refs(value)]
    assert names[0] == "facemetadata.json"


def test_an_oversized_result_file_is_referenced_not_inlined():
    value = _m2c(facegroup_key="micro-workflow/x/exec-1/facegroup.json")

    def reader(bucket, key, max_bytes=None):
        size = 229 * 1024
        return (None, size) if size > (max_bytes or 0) else ({}, size)

    rec = rr.resolve_node("faces", {"status": "completed", "value": value}, read_s3_json=reader)
    assert rec["kind"] == rr.KIND_ANALYSIS
    assert "referenced not inlined" in rec["error"]
    # The pointer survives so it can still be fetched deliberately.
    assert rec["handle"]["output_prefix"].endswith("exec-1")


def test_the_inline_budget_is_shared_across_nodes():
    """One verbose node must not spend the whole artifact."""
    big = 100 * 1024

    def reader(bucket, key, max_bytes=None):
        if (max_bytes or 0) < big:
            return None, big
        return {"payload": "x"}, big

    agg = rr.build_aggregate(
        plan_id="p", status="SUCCEEDED",
        nodes={
            "a": {"status": "completed", "value": _m2c(shots_key="micro-workflow/x/exec-1/embeddings/shots.json")},
            "b": {"status": "completed", "value": _m2c(facemetadata_key="micro-workflow/x/exec-1/facemetadata.json")},
            "c": {"status": "completed", "value": _m2c(speaker_manifest_key="micro-workflow/x/exec-1/speakers/speaker_manifest.json")},
        },
        read_s3_json=reader,
    )
    inlined = [n for n in agg["nodes"] if isinstance(n["findings"], dict) and "payload" in n["findings"]]
    assert len(inlined) < 3, "the shared ceiling must stop the third node"
    # The ceiling bites by shrinking the per-call cap, so denied nodes report the
    # file as referenced rather than embedded. Either wording is acceptable; going
    # silent is not.
    denied = [n for n in agg["nodes"] if n not in inlined]
    assert denied
    for node in denied:
        assert "not inlined" in node["error"] or "budget exhausted" in node["error"], node["error"]


def test_the_input_video_is_never_presented_as_a_node_output():
    value = _m2c(facemetadata_key="micro-workflow/x/exec-1/facemetadata.json")
    rec = rr.resolve_node("faces", {"status": "completed", "value": value},
                          read_s3_json=lambda b, k, max_bytes=None: ({"stats": {}}, 10))
    outputs = [a for a in rec["artifacts"] if a["role"] in ("output", "analysis")]
    assert outputs, "the analysis file itself must be offered to the renderer"
    assert all("transcoded.mp4" not in a["url"] for a in outputs)


def test_the_analysis_files_are_offered_to_the_renderer_as_urls():
    """The render path is URL-based, so inlining alone renders nothing.

    A four-analysis plan produced artifact_count=0, so dispatch_render had only a
    stray presigned video to work with: the result was one duplicated video track
    and an empty generic-preview instead of face/shot/speaker timelines.
    """
    value = _m2c(
        facemetadata_key="micro-workflow/x/exec-1/facemetadata.json",
        shots_key="micro-workflow/x/exec-1/embeddings/shots.json",
    )
    rec = rr.resolve_node("n", {"status": "completed", "value": value},
                          read_s3_json=lambda b, k, max_bytes=None: ({"stats": {}}, 10))
    analysis = [a for a in rec["artifacts"] if a["role"] == "analysis"]
    assert [a["name"] for a in analysis] == ["facemetadata.json", "shots.json"]
    # Inline findings are still there for the agent to summarise from.
    assert isinstance(rec["findings"], dict) and rec["findings"]


# ---------------------------------------------------------------------------
# Not every deliverable is JSON.
# ---------------------------------------------------------------------------

def test_audio_stems_become_artifacts_so_the_node_renders():
    """The stem node produced NO render tab while its three siblings each got one.

    Its deliverables are WAVs, not JSON -- speech_key/music_key/sfx_key under
    audiostem_sep_output -- and canonical_result_refs only accepted `.json`, so it
    emitted zero artifacts and the render path had nothing to dispatch. Shape below
    is verbatim from the live stem_separation node.
    """
    value = {
        "operation": "StateUsageTracking",
        "output": {"s3uri": "s3://proxy-bkt/micro-workflow/audio-stem/exec-1"},
        "input": {"s3uri": "s3://skills/artifacts/nba.mp4"},
        "data": {
            "audio_key": "micro-workflow/.../media_processing/audiostem_sep/audio_48k_mono.wav",
            "audiostem_sep_output": {
                "speech_key": "micro-workflow/audio-stem/exec-1/audio_speech.wav",
                "music_key": "micro-workflow/audio-stem/exec-1/audio_music.wav",
                "sfx_key": "micro-workflow/audio-stem/exec-1/audio_sfx.wav",
                "metrics_key": "micro-workflow/audio-stem/exec-1/audiostem_sep.json",
            },
            "usage_tracking": {
                "audio_key": "micro-workflow/.../audiostem_sep/audio_48k_mono.wav",
            },
        },
    }
    rec = rr.resolve_node("stem_separation", {"status": "completed", "value": value},
                          read_s3_json=lambda b, k, max_bytes=None: ({"ok": 1}, 10))
    outputs = [a for a in rec["artifacts"] if a["role"] == "output"]
    names = sorted(a["name"] for a in outputs)
    assert names == ["audio_music.wav", "audio_sfx.wav", "audio_speech.wav"]
    assert all(a["media_kind"] == "audio" for a in outputs)
    # The INPUT audio must not be offered as a product, from data or usage_tracking.
    assert all("audio_48k_mono" not in a["url"] for a in outputs)


def test_media_keys_outside_an_output_subtree_are_ignored():
    """No fallback, on purpose.

    Hedging with "any media key we can find" made diarization surface its own INPUT
    (data.audio_key, the 16kHz mono file fed in) and shot segmentation surface
    per-segment scratch (segment_0000.mp4, transcoded.mp4) as though those were
    products -- two extra junk tabs per node.
    """
    value = {
        "output": {"s3uri": "s3://proxy-bkt/wf/exec-2"},
        "data": {
            "audio_key": "wf/exec-2/media_processing/audio_16k_mono.wav",
            "iterators": [{"proxy_key": "wf/exec-2/proxy/segment_0000.mp4"}],
        },
    }
    rec = rr.resolve_node("n", {"status": "completed", "value": value})
    assert [a for a in rec["artifacts"] if a["role"] == "output"] == []


def test_media_outputs_are_capped():
    keys = {f"seg{i}_key": f"wf/exec/seg{i}.wav" for i in range(20)}
    value = {"output": {"s3uri": "s3://b/wf/exec"}, "data": {"stems_output": keys}}
    rec = rr.resolve_node("n", {"status": "completed", "value": value})
    outputs = [a for a in rec["artifacts"] if a["role"] == "output"]
    assert len(outputs) == rr.MAX_MEDIA_OUTPUTS_PER_NODE


def test_output_prefix_is_never_read_as_json():
    """M2C's output.s3uri is a PREFIX. A thin value (no data.*_key list) reached the raw-ref
    branch, which HeadObject'd the prefix -> 403 -> node kind=error, artifact_count 0. That
    empty aggregate is why retrieving one proxy job cost 32 hand-fetch steps. A prefix must be
    recorded as a pointer, not read.
    """
    # The real thin shape the poller recorded for f97e63e4 (proxy job): a prefix, no file list.
    value = {"output": {"s3uri": "s3://proxy-bkt/micro-workflow/media-processing/f97e63e4-eaf9/"}}

    def boom(bucket, key):
        raise Exception("An error occurred (403) when calling the HeadObject operation")

    rec = rr.resolve_node("n", {"status": "completed", "value": value}, read_s3_json=boom)
    assert rec["kind"] != "error", rec
    assert rec.get("handle", {}).get("output_prefix", "").endswith("/f97e63e4-eaf9/")


def test_a_prefix_with_no_trailing_slash_is_still_a_prefix():
    """The exec-id prefix often has no trailing slash and no extension."""
    value = {"output": {"s3uri": "s3://b/wf/media-processing/f97e63e4-eaf9-5082-ba89-c01dc7f4e962"}}
    rec = rr.resolve_node("n", {"status": "completed", "value": value},
                          read_s3_json=lambda b, k: (_ for _ in ()).throw(AssertionError("read a prefix")))
    assert rec["kind"] != "error"
    assert rec.get("handle", {}).get("output_prefix")


def test_a_real_json_file_is_still_read_not_treated_as_prefix():
    """A concrete .json key must still be read as findings (has an extension)."""
    value = {"output": {"s3uri": "s3://b/wf/exec/probe.json"}}
    rec = rr.resolve_node("n", {"status": "completed", "value": value},
                          read_s3_json=lambda b, k: ({"duration": 60.0}, 40))
    assert rec["kind"] == "analysis"
    assert rec["findings"] == {"duration": 60.0}


def test_prefix_is_enumerated_into_real_output_artifacts():
    """F4a: a thin prefix node yields its ACTUAL files when a lister is wired, not a bare
    pointer -- which is what killed the empty aggregate / 32-step hand-fetch."""
    value = {"output": {"s3uri": "s3://proxy/micro-workflow/media-processing/EXEC/"}}
    listed = [
        "micro-workflow/media-processing/EXEC/proxy/transcoded.mp4",
        "micro-workflow/media-processing/EXEC/probe.json",  # not media -> not an artifact here
        "micro-workflow/media-processing/EXEC/input/source.mp4",  # input-named -> skipped
    ]
    rec = rr.resolve_node("n", {"status": "completed", "value": value},
                          list_prefix=lambda b, p: listed)
    arts = [a["url"] for a in rec["artifacts"]]
    assert rec["kind"] == "media"
    assert any(u.endswith("proxy/transcoded.mp4") for u in arts)
    assert not any("source.mp4" in u for u in arts), "input-named object must be skipped"
    assert rec.get("handle", {}).get("output_prefix")  # pointer retained


def test_prefix_with_no_lister_still_returns_the_pointer():
    value = {"output": {"s3uri": "s3://proxy/wf/EXEC/"}}
    rec = rr.resolve_node("n", {"status": "completed", "value": value})
    assert rec["kind"] == "media" and rec.get("handle", {}).get("output_prefix")


def test_prefix_lister_failure_degrades_to_pointer():
    value = {"output": {"s3uri": "s3://proxy/wf/EXEC/"}}
    def boom(b, p):
        raise Exception("list denied")
    rec = rr.resolve_node("n", {"status": "completed", "value": value}, list_prefix=boom)
    assert rec["kind"] == "media" and rec.get("handle", {}).get("output_prefix")


def test_explicit_artifact_urls_are_used_without_any_s3_access():
    """The real fix for the 39-step hand-fetch: the poller can't ListBucket the proxy bucket
    (implicitDeny), so list_prefix returned nothing. But the gateway already gave it the output
    urls (on the row). resolve_node uses them directly -- no read_s3_json, no list_prefix."""
    entry = {
        "status": "completed",
        "value": {"output": {"s3uri": "s3://proxy/micro-workflow/media-processing/EXEC/"}},
        "artifact_urls": ["s3://proxy/micro-workflow/media-processing/EXEC/proxy/transcoded.mp4"],
    }
    # Deliberately provide NO read_s3_json and NO list_prefix — proves it needs neither.
    rec = rr.resolve_node("n", entry)
    assert rec["kind"] == "media"
    assert [a["url"].rsplit("/", 1)[-1] for a in rec["artifacts"]] == ["transcoded.mp4"]
    assert rec["artifacts"][0]["role"] == "output"


def test_explicit_artifact_urls_skip_input_named_entries():
    entry = {"status": "completed", "value": {},
             "artifact_urls": ["s3://b/wf/EXEC/proxy/transcoded.mp4",
                               "s3://b/wf/EXEC/input/source.mp4"]}
    rec = rr.resolve_node("n", entry)
    names = [a["url"].rsplit("/", 1)[-1] for a in rec["artifacts"]]
    assert "transcoded.mp4" in names and "source.mp4" not in names


def test_no_artifact_urls_falls_through_to_existing_resolution():
    # empty/absent artifact_urls must not short-circuit; prefix/read paths still apply.
    entry = {"status": "completed", "value": {"output": {"s3uri": "s3://b/wf/EXEC/"}},
             "artifact_urls": []}
    rec = rr.resolve_node("n", entry)  # no lister -> pointer handle, not the explicit branch
    assert rec.get("handle", {}).get("output_prefix")


def _face_value():
    base = "micro-workflow/face-recognition/EXEC/ephemeral"
    return {"output": {"s3uri": "s3://proxy/micro-workflow/face-recognition/EXEC/"},
            "data": {"facemetadata_key": f"{base}/facemetadata.json",
                     "identity_clusters_key": f"{base}/identity_clusters.json",
                     "face_clusters_key": f"{base}/face_clusters.json",
                     "face_tracks_key": f"{base}/face_tracks.json",
                     "facegroup_key": f"{base}/facegroup.json",
                     "frame_index_key": f"{base}/frame_index.json",
                     "detection_plan_key": f"{base}/detection_plan.json"}}


def test_canonical_refs_capped_for_inlining_but_uncapped_for_artifacts():
    v = _face_value()
    capped = [n for _, _, n in rr.canonical_result_refs(v)]
    allrefs = [n for _, _, n in rr.canonical_result_refs(v, limit=None)]
    assert len(capped) == 2
    assert "face_tracks.json" not in capped          # the box file is cut by the 2-cap
    assert "face_tracks.json" in allrefs             # but present uncapped
    assert "frame_index.json" not in allrefs         # scratch is never canonical
    assert "detection_plan.json" not in allrefs


def test_face_node_surfaces_face_tracks_as_artifact_but_inlines_only_two():
    v = _face_value()
    rec = rr.resolve_node("t1", {"status": "completed", "value": v},
                          read_s3_json=lambda b, k, max_bytes=None: ({"stub": 1}, 100))
    arts = {a["url"].rsplit("/", 1)[-1] for a in rec["artifacts"]}
    assert "face_tracks.json" in arts                # the box file reaches the render
    assert not (arts & {"frame_index.json", "detection_plan.json"})
    assert set((rec.get("findings") or {}).keys()) == {"facemetadata.json", "identity_clusters.json"}


def test_probe_json_is_a_readable_finding_but_never_a_render_artifact():
    # probe.json answers "what codec / how many audio tracks" so it stays a readable finding,
    # but it is findings-only: it must never be surfaced as a render artifact (no tab).
    assert "probe.json" in rr._CANONICAL_BASENAMES            # inlined as a finding
    assert "probe.json" in rr._FINDINGS_ONLY_BASENAMES        # but not a render deliverable
    # M2C data path with probe_key present: probe.json inlined as a finding, NOT an artifact.
    value = {"output": {"s3uri": "s3://proxy/wf/EXEC/"},
             "data": {"probe_key": "wf/EXEC/probe.json", "shots_key": "wf/EXEC/shots.json"}}
    rec = rr.resolve_node("t", {"status": "completed", "value": value},
                          read_s3_json=lambda b, k, max_bytes=None: ({"stub": 1}, 100))
    art_names = {a["url"].rsplit("/", 1)[-1] for a in rec["artifacts"]}
    assert "probe.json" not in art_names                      # no render tab for probe
    assert "shots.json" in art_names                          # a real deliverable still surfaces
    assert "probe.json" in (rec.get("findings") or {})        # but probe IS readable as a finding


def test_probe_json_not_surfaced_by_the_artifact_urls_branch():
    rec = rr.resolve_node("t", {"status": "completed", "value": {},
                                "artifact_urls": ["s3://b/proxy/transcoded.mp4", "s3://b/probe.json"]})
    names = {a["url"].rsplit("/", 1)[-1] for a in rec["artifacts"]}
    assert names == {"transcoded.mp4"}                        # probe.json is findings-only


# --------------------------------------------------------------------------
# Auto-import of deliverables into our bucket (downloadable CloudFront urls)
# --------------------------------------------------------------------------

def _proxy_node():
    # M2C outputs surfaced as s3:// deliverables in the proxy bucket.
    return {"n1": {"status": "completed", "artifact_urls": [
        "s3://m2cv6-proxy/x/facemetadata.json",
        "s3://m2cv6-proxy/x/transcoded.mp4",
    ]}}


def test_build_aggregate_auto_imports_s3_deliverables():
    calls = []

    def fake_import(src_bucket, src_key, relpath):
        calls.append((src_bucket, src_key, relpath))
        return f"https://cdn.cloudfront.net/artifacts/imports/{relpath}"

    agg = rr.build_aggregate(plan_id="p1", status="SUCCEEDED", nodes=_proxy_node(),
                             import_artifact=fake_import)
    arts = agg["nodes"][0]["artifacts"]
    assert len(arts) == 2
    for a in arts:
        assert a["url"].startswith("https://cdn.cloudfront.net/artifacts/imports/p1/n1/")
        assert a["source_ref"].startswith("s3://m2cv6-proxy/")
    assert len(calls) == 2  # both the analysis json and the media were imported


def test_build_aggregate_without_import_keeps_s3():
    agg = rr.build_aggregate(plan_id="p1", status="SUCCEEDED", nodes=_proxy_node())
    for a in agg["nodes"][0]["artifacts"]:
        assert a["url"].startswith("s3://")
        assert "source_ref" not in a


def test_auto_import_failure_leaves_original_ref():
    agg = rr.build_aggregate(plan_id="p1", status="SUCCEEDED", nodes=_proxy_node(),
                             import_artifact=lambda *a: None)
    for a in agg["nodes"][0]["artifacts"]:
        assert a["url"].startswith("s3://")
        assert "source_ref" not in a


def test_auto_import_prioritizes_output_over_analysis_under_cap(monkeypatch):
    """The requested media (role=output) must be imported even when many secondary
    analysis artifacts precede it and the cap is smaller than the total. A single
    positional pass starved the output (blank player + presigned download); the
    priority pass fixes it."""
    monkeypatch.setattr(rr, "MAX_AUTO_IMPORTS", 2)
    records = [
        {"node_id": "faces", "artifacts": [
            {"role": "analysis", "url": "s3://b/faces/c1.jpg", "name": "c1.jpg"},
            {"role": "analysis", "url": "s3://b/faces/c2.jpg", "name": "c2.jpg"},
            {"role": "analysis", "url": "s3://b/faces/c3.jpg", "name": "c3.jpg"},
        ]},
        {"node_id": "trim", "artifacts": [
            {"role": "output", "url": "s3://b/trim/out.mp4", "name": "out.mp4"},
        ]},
    ]
    imported = []

    def imp(sb, sk, rp):
        imported.append(sk)
        return f"https://cdn.cloudfront.net/artifacts/imports/{rp}"

    rr._auto_import_deliverables("p", records, imp)
    out_art = records[1]["artifacts"][0]
    assert out_art["url"].startswith("https://cdn.cloudfront.net/")  # output converted despite cap
    assert out_art["source_ref"] == "s3://b/trim/out.mp4"
    assert len(imported) == 2                        # cap respected
    assert "trim/out.mp4" in imported                # and the output made the cut


def test_auto_import_lone_job_uses_bare_filename_relpath():
    """A lone/direct job passes its execution ARN as BOTH plan_id and node_id. The
    relpath must collapse to just the filename — the import_prefix already scopes
    artifacts/<sid>/output/<uuid>/ — not "<arn>/<arn>/<name>", which doubled the ARN
    (colons and all) into the S3 key. Regression for the observed 468b45cf key."""
    arn = "arn:aws:states:us-west-2:100000000002:execution:m2cv6-x:abb14e36"
    records = [{"node_id": arn, "artifacts": [
        {"role": "output", "url": "s3://m2c/exec/Meridian_clip.mp4", "name": "Meridian_clip.mp4"},
    ]}]
    seen = []
    rr._auto_import_deliverables(arn, records, lambda sb, sk, rp: (seen.append(rp), f"https://cdn/{rp}")[1])
    assert seen == ["Meridian_clip.mp4"]
    assert "arn:aws" not in records[0]["artifacts"][0]["url"]


def test_auto_import_plan_keeps_node_namespacing():
    """A source with NO execution UUID keeps the legacy plan/node layout — we don't invent an
    opaque path for a producer that doesn't scope by execution."""
    records = [{"node_id": "trim", "artifacts": [
        {"role": "output", "url": "s3://b/trim/out.mp4", "name": "out.mp4"},
    ]}]
    seen = []
    rr._auto_import_deliverables("plan-7", records, lambda sb, sk, rp: (seen.append(rp), f"https://cdn/{rp}")[1])
    assert seen == ["plan-7/trim/out.mp4"]


# --- one durable copy per source, whatever resolves it -------------------------------------
#
# Regression for prod session c80b7144: a plan trimmed two scenes, and each clip was imported
# TWICE — once by the polling worker as artifacts/<sid>/output/<execUUID>/clip.mp4 and once by
# the plan-chainer as artifacts/<sid>/output/<planRunUUID>/<node>/clip.mp4. Because the render
# identity is <last-UUID>/<basename>, one clip had two identities, so the diarization and face
# detection of a scene opened their own tabs instead of folding onto that scene's timeline.

# The real ephemeral source path from that session.
_M2C_SRC = ("micro-workflow/media-processing/8dbfe4fa-8598-5d47-bcc4-d6c336a4d947/"
            "scene1_trim/scene1_violence_harm_trim.mp4")
_EXEC_UUID = "8dbfe4fa-8598-5d47-bcc4-d6c336a4d947"


def _relpaths(plan_id, records):
    seen = []
    rr._auto_import_deliverables(plan_id, records, lambda sb, sk, rp: (seen.append(rp), f"https://cdn/{rp}")[1])
    return seen


def test_plan_node_import_is_keyed_by_source_execution_not_plan_run():
    """The plan-chainer must key on the SOURCE execution, so it converges with the polling
    worker instead of minting a second identity under the plan-run UUID."""
    records = [{"node_id": "clip1", "artifacts": [
        {"role": "output", "url": f"s3://m2c-proxy/{_M2C_SRC}", "name": "scene1_violence_harm_trim.mp4"},
    ]}]
    seen = _relpaths("6d93ebc8-317f-4cd8-8f1e-719fdcfbdfca", records)
    assert seen == [f"{_EXEC_UUID}/scene1_violence_harm_trim.mp4"]
    # The plan run id must NOT appear in the key — that is what split the identity.
    assert "6d93ebc8" not in seen[0]


def test_both_resolvers_converge_on_one_durable_key():
    """The polling worker and the plan-chainer must land on the SAME final S3 key for the same
    source object — one copy, one render identity."""
    art = {"role": "output", "url": f"s3://m2c-proxy/{_M2C_SRC}", "name": "scene1_violence_harm_trim.mp4"}

    # Polling worker: plan_id == node_id (the execution ARN), and its import_prefix already
    # carries the execution segment.
    arn = f"arn:aws:states:us-west-2:1:execution:m2cv6-media-processing:{_EXEC_UUID}"
    worker_rel = _relpaths(arn, [{"node_id": arn, "artifacts": [dict(art)]}])
    worker_key = f"artifacts/sess/output/{_EXEC_UUID}/" + worker_rel[0]

    # Plan-chainer: import_prefix is the bare session output prefix.
    plan_rel = _relpaths("6d93ebc8-317f-4cd8-8f1e-719fdcfbdfca",
                         [{"node_id": "clip1", "artifacts": [dict(art)]}])
    plan_key = "artifacts/sess/output/" + plan_rel[0]

    assert worker_key == plan_key == (
        f"artifacts/sess/output/{_EXEC_UUID}/scene1_violence_harm_trim.mp4")


def test_two_clips_from_different_executions_stay_distinct():
    """Two scenes trimmed by two executions must keep separate identities even when the
    basename is identical — otherwise both ranges mix into one timeline."""
    u1, u2 = _EXEC_UUID, "4ac89bbf-2863-55c4-9979-5632d462e0ea"
    records = [
        {"node_id": "clip1", "artifacts": [
            {"role": "output", "url": f"s3://m2c/micro-workflow/mp/{u1}/scene1_trim/clip.mp4", "name": "clip.mp4"}]},
        {"node_id": "clip2", "artifacts": [
            {"role": "output", "url": f"s3://m2c/micro-workflow/mp/{u2}/scene2_trim/clip.mp4", "name": "clip.mp4"}]},
    ]
    seen = _relpaths("plan-1", records)
    assert seen == [f"{u1}/clip.mp4", f"{u2}/clip.mp4"]
    assert len(set(seen)) == 2


def test_same_execution_same_basename_is_disambiguated_by_source_parent():
    """Two outputs of ONE execution sharing a basename would overwrite each other on a shared
    key. They are separated by the SOURCE's parent segment — never by node id, which the
    polling worker cannot see (that would re-split the key across resolvers)."""
    u = _EXEC_UUID
    records = [{"node_id": "clip1", "artifacts": [
        {"role": "output", "url": f"s3://m2c/micro-workflow/mp/{u}/scene1_trim/clip.mp4", "name": "clip.mp4"},
        {"role": "output", "url": f"s3://m2c/micro-workflow/mp/{u}/scene2_trim/clip.mp4", "name": "clip.mp4"},
    ]}]
    seen = _relpaths("plan-1", records)
    assert seen == [f"{u}/scene1_trim-clip.mp4", f"{u}/scene2_trim-clip.mp4"]
    assert len(set(seen)) == 2


def test_collision_guard_agrees_across_resolvers():
    """The disambiguation must be source-derived, so the polling worker (which sees the same
    execution's artifacts but no node id) produces the SAME names as the plan-chainer."""
    u = _EXEC_UUID
    arts = [
        {"role": "output", "url": f"s3://m2c/micro-workflow/mp/{u}/a_trim/clip.mp4", "name": "clip.mp4"},
        {"role": "output", "url": f"s3://m2c/micro-workflow/mp/{u}/b_trim/clip.mp4", "name": "clip.mp4"},
    ]
    arn = f"arn:aws:states:us-west-2:1:execution:wf:{u}"
    worker = _relpaths(arn, [{"node_id": arn, "artifacts": [dict(a) for a in arts]}])
    plan = _relpaths("plan-9", [{"node_id": "n1", "artifacts": [dict(a) for a in arts]}])
    # The worker's prefix supplies the uuid segment, so compare the basenames it produced.
    assert worker == ["a_trim-clip.mp4", "b_trim-clip.mp4"]
    assert plan == [f"{u}/a_trim-clip.mp4", f"{u}/b_trim-clip.mp4"]
    assert [p.rsplit("/", 1)[-1] for p in plan] == worker


def test_source_uuid_case_is_normalised():
    """Identity lowercases the UUID, so the mirror key must too — otherwise a mixed-case
    producer path yields a key whose identity never matches its siblings'."""
    upper = _EXEC_UUID.upper()
    records = [{"node_id": "n", "artifacts": [
        {"role": "output", "url": f"s3://m2c/wf/{upper}/stage/clip.mp4", "name": "clip.mp4"}]}]
    assert _relpaths("plan-1", records) == [f"{_EXEC_UUID}/clip.mp4"]


def test_auto_import_says_what_the_cap_dropped(monkeypatch, caplog):
    """A cap that truncates silently reads as "we imported everything".

    Anything past MAX_AUTO_IMPORTS keeps its foreign `s3://` url, which the UI cannot
    display -- a blank player plus a presigned download. That is indistinguishable from a
    render bug unless the truncation says so, and it was logged nowhere.
    """
    import logging
    monkeypatch.setattr(rr, "MAX_AUTO_IMPORTS", 1)
    records = [{"node_id": "n", "artifacts": [
        {"role": "output", "url": "s3://b/n/a.mp4", "name": "a.mp4"},
        {"role": "output", "url": "s3://b/n/b.mp4", "name": "b.mp4"},
        {"role": "analysis", "url": "s3://b/n/c.jpg", "name": "c.jpg"},
    ]}]
    with caplog.at_level(logging.WARNING):
        rr._auto_import_deliverables("p1", records, lambda sb, sk, rp: f"https://cdn/{rp}")
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "p1" in joined, f"cap hit was not reported: {joined!r}"
    # The COUNT is the actionable part -- "2 left unimported", not just "cap reached".
    assert "2" in joined, f"cap message does not say how many were dropped: {joined!r}"


def test_auto_import_reports_a_copy_failure(monkeypatch, caplog):
    # `except Exception: new_url = None` also degrades silently: the artifact keeps an
    # undisplayable s3:// ref and nothing anywhere says a copy was attempted and failed.
    import logging

    def boom(sb, sk, rp):
        raise RuntimeError("access denied")

    records = [{"node_id": "n", "artifacts": [
        {"role": "output", "url": "s3://b/n/a.mp4", "name": "a.mp4"},
    ]}]
    with caplog.at_level(logging.WARNING):
        rr._auto_import_deliverables("p1", records, boom)
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "a.mp4" in joined or "n/a.mp4" in joined, f"copy failure not reported: {joined!r}"
    # ...and it must still not sink the aggregate.
    assert records[0]["artifacts"][0]["url"] == "s3://b/n/a.mp4"


def test_auto_import_never_touches_source_role():
    records = [{"node_id": "n", "artifacts": [
        {"role": "source", "url": "s3://b/src/original.mp4", "name": "original.mp4"},
    ]}]
    rr._auto_import_deliverables("p", records, lambda *a: "https://cdn/x")
    assert records[0]["artifacts"][0]["url"] == "s3://b/src/original.mp4"  # source never imported


# --------------------------------------------------------------------------
# Shared CloudFront importer (make_s3_importer) — durable links for BOTH the
# chainer (plans) and the polling worker (lone/direct jobs).
# --------------------------------------------------------------------------

class _FakeS3Copy:
    def __init__(self):
        self.copies = []

    def copy_object(self, **kw):
        self.copies.append(kw)


def test_importer_copies_proxy_object_to_cloudfront():
    s3 = _FakeS3Copy()
    imp = rr.make_s3_importer(s3, "skills-bkt", "d123.cloudfront.net")
    url = imp("m2c-proxy", "micro-workflow/x/transcoded.mp4", "job1/node1/transcoded.mp4")
    assert url == "https://d123.cloudfront.net/artifacts/imports/job1/node1/transcoded.mp4"
    assert len(s3.copies) == 1
    c = s3.copies[0]
    assert c["Bucket"] == "skills-bkt"
    assert c["Key"] == "artifacts/imports/job1/node1/transcoded.mp4"
    assert c["CopySource"] == {"Bucket": "m2c-proxy", "Key": "micro-workflow/x/transcoded.mp4"}
    assert c.get("ContentType") == "video/mp4"   # inferred + REPLACE'd
    # The copy says where it came from, so a tool of the producer can be handed the original (agent origin_refs).
    assert c["Metadata"] == {"origin": "s3://m2c-proxy/micro-workflow/x/transcoded.mp4"}
    assert c["MetadataDirective"] == "REPLACE"


def test_importer_records_the_origin_even_for_an_unknown_type():
    s3 = _FakeS3Copy()
    rr.make_s3_importer(s3, "skills-bkt", "d123.cloudfront.net")("m2c-proxy", "micro-workflow/x/embeddings.npz", "j/embeddings.npz")
    c = s3.copies[0]
    assert c["Metadata"] == {"origin": "s3://m2c-proxy/micro-workflow/x/embeddings.npz"}
    assert c["ContentType"] == "application/octet-stream"


def test_importer_skips_copy_when_already_in_our_bucket():
    s3 = _FakeS3Copy()
    imp = rr.make_s3_importer(s3, "skills-bkt", "d123.cloudfront.net")
    url = imp("skills-bkt", "artifacts/existing.wav", "ignored")
    assert url == "https://d123.cloudfront.net/artifacts/existing.wav"
    assert s3.copies == []   # no copy — already ours


def test_importer_none_when_unconfigured():
    imp = rr.make_s3_importer(_FakeS3Copy(), "", "")
    assert imp("m2c-proxy", "k", "r") is None


def test_importer_best_effort_none_on_copy_failure():
    class _Boom:
        def copy_object(self, **kw):
            raise RuntimeError("access denied")
    imp = rr.make_s3_importer(_Boom(), "skills-bkt", "d123.cloudfront.net")
    assert imp("m2c-proxy", "k", "r") is None   # never raises; caller keeps original ref


def test_process_media_extract_audio_surfaces_output_from_prefix():
    """Session 7eaa1a75 regression: a process_media extract-audio node reports its product
    under output.s3uri (a PREFIX) with no data.*_key media field, so media_outputs_of found
    nothing and the aggregate surfaced only the INPUT (role=source) — the audio never rendered.
    The prefix must be enumerated so the extracted .wav is a role=output artifact."""
    value = {
        "operation": "process_media", "status": "SUCCEEDED",
        "input": {"asset_s3uri": "s3://b/artifacts/in.mp4"},
        "output": {"output_groups": [{"group_name": "audio", "format": "wav"}],
                   "s3uri": "s3://proxy/micro-workflow/media-processing/exec1/"},
        "data": {"probe": {"duration": 30}},
    }

    def list_prefix(bucket, prefix):
        assert bucket == "proxy" and prefix.startswith("micro-workflow/media-processing/exec1/")
        return ["micro-workflow/media-processing/exec1/audio/extracted.wav"]

    rec = rr.resolve_node("extract_1", {"status": "completed", "value": value},
                          read_s3_json=lambda b, k, max_bytes=None: ({"duration": 30}, 10),
                          list_prefix=list_prefix)
    out_arts = [a for a in rec["artifacts"] if a["role"] == "output"]
    assert any(a["url"].endswith("audio/extracted.wav") and a["media_kind"] == "audio"
               for a in out_arts), rec["artifacts"]


def test_process_media_extract_without_lister_does_not_crash():
    """No lister wired -> no output artifact from the prefix, but the node still resolves
    (falls back to the input/source + findings) rather than raising."""
    value = {
        "operation": "process_media", "status": "SUCCEEDED",
        "input": {"asset_s3uri": "s3://b/artifacts/in.mp4"},
        "output": {"output_groups": [{"group_name": "audio", "format": "wav"}],
                   "s3uri": "s3://proxy/micro-workflow/media-processing/exec1/"},
        "data": {"probe": {"duration": 30}},
    }
    rec = rr.resolve_node("extract_1", {"status": "completed", "value": value},
                          read_s3_json=lambda b, k, max_bytes=None: ({"duration": 30}, 10))
    assert rec["node_id"] == "extract_1"  # resolved, no crash


def test_shot_node_without_data_key_surfaces_shots_json_from_prefix():
    """Real a314ed42 sg_shots shape: `data` carries only CONFIG (is_large_lambda, frames_to_shots_cfg)
    with NO `*_key`; the deliverable `shots.json` lives under `output.s3uri` with nothing naming it,
    and the `input.s3uri` is the SOURCE video. The render must get `shots.json` (role=analysis) so it
    renders video-markers — not the input video, and not an empty tab. Resolver previously enumerated
    the output prefix for MEDIA only, so a JSON-only deliverable node came back with no artifacts."""
    value = {
        "operation": "StateUsageTracking",
        "output": {"s3uri": "s3://proxy-bkt/micro-workflow/shot-segmentation/EXEC/"},
        "input": {"s3uri": "s3://proxy-bkt/micro-workflow/media-processing/EXEC2/proxy/transcoded.mp4"},
        "data": {"is_large_lambda": False, "frames_to_shots_cfg": {"segment_duration_secs": 1200}},
    }

    def _list(bucket, prefix):
        if "shot-segmentation" in prefix:
            return [
                "micro-workflow/shot-segmentation/EXEC/shots.json",
                "micro-workflow/shot-segmentation/EXEC/embeddings/recurrence.json",  # scratch
            ]
        return []

    def _read(b, k, max_bytes=None):
        return ({"shots": [{"shot_id": 0, "start_time": 0.0, "end_time": 1.0}]}, 40) if "shots.json" in k else (None, 0)

    rec = rr.resolve_node("sg_shots", {"status": "completed", "value": value},
                          list_prefix=_list, read_s3_json=_read)
    by_role = {}
    for a in rec["artifacts"]:
        by_role.setdefault(a.get("role"), []).append((a.get("name") or a.get("url", "").rsplit("/", 1)[-1]))
    # shots.json is the render DELIVERABLE (role=analysis) -> video-markers.
    assert any("shots.json" in n for n in by_role.get("analysis", [])), f"shots.json not analysis: {rec['artifacts']}"
    # The input video is the overlay CANVAS (role=source), never a rendered output/analysis track.
    src_urls = [a.get("url", "") for a in rec["artifacts"] if a.get("role") == "source"]
    non_source_urls = [a.get("url", "") for a in rec["artifacts"] if a.get("role") != "source"]
    assert not any("transcoded.mp4" in u for u in non_source_urls), f"source rendered as deliverable: {rec['artifacts']}"


def test_face_node_without_data_key_surfaces_facegroup_from_prefix():
    """People-detection sibling of the shot case: a face node whose `data` names no `*_key` must
    still surface the analysis JSON (facegroup.json / facemetadata.json) from the output prefix as
    role=analysis, with the source video role=source. The render agent's skill selection then picks
    video-annotations (boxes) from facegroup.json."""
    value = {
        "operation": "StateUsageTracking",
        "output": {"s3uri": "s3://proxy-bkt/micro-workflow/face-recognition/EXEC/"},
        "input": {"s3uri": "s3://proxy-bkt/micro-workflow/media-processing/EXEC2/proxy/transcoded.mp4"},
        "data": {"is_large_lambda": False},
    }

    def _list(bucket, prefix):
        if "face-recognition" in prefix:
            return [
                "micro-workflow/face-recognition/EXEC/facegroup.json",
                "micro-workflow/face-recognition/EXEC/facemetadata.json",
                "micro-workflow/face-recognition/EXEC/ephemeral/detection_plan.json",  # scratch (not canonical)
            ]
        return []

    rec = rr.resolve_node("sg_faces", {"status": "completed", "value": value},
                          list_prefix=_list, read_s3_json=lambda b, k, max_bytes=None: ({"people": []}, 10))
    analysis = [ (a.get("name") or "") for a in rec["artifacts"] if a.get("role") == "analysis" ]
    assert "facegroup.json" in analysis and "facemetadata.json" in analysis, rec["artifacts"]
    assert not any("detection_plan.json" in n for n in analysis), "scratch surfaced"
    assert any(a.get("role") == "source" and "transcoded.mp4" in a.get("url", "") for a in rec["artifacts"])


def test_make_s3_importer_copies_ephemeral_same_bucket_keys_into_artifacts():
    """M2C writes job outputs under an EPHEMERAL micro-workflow/ prefix in OUR bucket. A bare
    CloudFront URL to that key 403s/NoSuchKeys once purged (shot-segmentation shots.json went
    unreadable → stuck render), so the importer must COPY it into the durable artifacts/ prefix.
    A key already under artifacts/ is reused in place (no copy)."""
    copies = []

    class S3:
        def copy_object(self, **kw):
            copies.append(kw)

    imp = rr.make_s3_importer(S3(), "skills-bkt", "d.cloudfront.net", import_prefix="artifacts/s/output/j/")
    url = imp("skills-bkt", "micro-workflow/shot-segmentation/x/shots.json", "shots.json")
    assert url == "https://d.cloudfront.net/artifacts/s/output/j/shots.json"
    assert copies and copies[0]["Key"] == "artifacts/s/output/j/shots.json"  # COPIED (durable)
    copies.clear()
    url2 = imp("skills-bkt", "artifacts/s/output/existing.mp4", "existing.mp4")
    assert url2 == "https://d.cloudfront.net/artifacts/s/output/existing.mp4"
    assert copies == []  # already durable → reused, no copy


# --------------------------------------------------------------------------
# Sibling clips that emit the SAME basename must not collapse into one render tab.
# A multi-clip extract gives every node a "clip.mp4"; the render identity keys on
# <uuid>/<basename> and drops the per-node subfolder, so identical basenames shared
# one sourceKey and the two clips merged into a single composite with both ranges.
# Auto-import now node-prefixes ONLY colliding basenames.
# --------------------------------------------------------------------------

def _capture_importer(recorded):
    def _imp(src_bucket, src_key, relpath):
        recorded.append(relpath)
        return f"https://cdn/{relpath}"
    return _imp


def test_sibling_clips_with_same_basename_get_distinct_import_names():
    recorded = []
    records = [
        {"node_id": "clip1", "artifacts": [
            {"url": "s3://foreign/exec/clip1/clip.mp4", "role": "output"}]},
        {"node_id": "clip2", "artifacts": [
            {"url": "s3://foreign/exec/clip2/clip.mp4", "role": "output"}]},
    ]
    rr._auto_import_deliverables("plan1", records, _capture_importer(recorded))
    # Colliding basenames are node-prefixed → distinct keys → distinct <uuid>/<basename>
    # render identities → each clip renders in its own tab.
    assert recorded == ["plan1/clip1/clip1-clip.mp4", "plan1/clip2/clip2-clip.mp4"]
    assert records[0]["artifacts"][0]["url"] != records[1]["artifacts"][0]["url"]


def test_a_unique_basename_is_not_renamed():
    # A unique basename is left untouched, so a single clip's durable/ephemeral copies keep
    # one basename and still merge onto one timeline (no regression to the intended collapse).
    recorded = []
    records = [
        {"node_id": "clip1", "artifacts": [
            {"url": "s3://foreign/exec/clip1/clip.mp4", "role": "output"}]},
        {"node_id": "faces", "artifacts": [
            {"url": "s3://foreign/exec/faces/facemetadata.json", "role": "analysis"}]},
    ]
    rr._auto_import_deliverables("plan1", records, _capture_importer(recorded))
    assert recorded == ["plan1/clip1/clip.mp4", "plan1/faces/facemetadata.json"]


# --------------------------------------------------------------------------
# asset-lineage Phase 3 (a): a projects-session OUTPUT key is ours and durable.
# --------------------------------------------------------------------------
import json as _json  # noqa: E402

import pytest  # noqa: E402

_LAYOUT = _json.loads((Path(__file__).resolve().parents[4] / "agent" / "tests" / "fixtures"
                       / "session_layout_cases.json").read_text())


@pytest.mark.parametrize("case", _LAYOUT["keys"], ids=lambda c: c["key"] or "<empty>")
def test_project_session_output_key_matches_the_shared_fixture(case):
    assert rr.is_project_session_output_key(case["key"]) is case["isProjectSessionOutput"]


def test_importer_reuses_a_project_session_output_in_place():
    s3 = _FakeS3Copy()
    imp = rr.make_s3_importer(s3, "skills-bkt", "d123.cloudfront.net")
    key = f"projects/{_LAYOUT['pid']}/sessions/{_LAYOUT['sid']}/output/clip.mp4"
    assert imp("skills-bkt", key, "ignored") == f"https://d123.cloudfront.net/{key}"
    assert not s3.copies


def test_importer_still_copies_a_project_asset_key():
    # projects/<pid>/<assetId>/… is a project ASSET, not a session output: not reused as an artifact.
    s3 = _FakeS3Copy()
    imp = rr.make_s3_importer(s3, "skills-bkt", "d123.cloudfront.net")
    imp("skills-bkt", f"projects/{_LAYOUT['pid']}/video:0b6c1b5e-3a3f-4b44-9c55-111122223333/x.mp4", "j/x.mp4")
    assert s3.copies


# --------------------------------------------------------------------------
# Producer-DECLARED outputs (M2C status tool), staging session b8a59e30
# --------------------------------------------------------------------------

_PROXY = "m2cv6-0e0000000002-100000000002-us-west-2-proxy"
# Verbatim staging execution output of shot-metadata-enrichment 6e060617 (80 bytes).
_ENRICH_SFN = {"uuid": "6e060617-adb4-5327-a362-0dcd5733489f", "total_estimated_cost": 0.0421725}
_ENRICH_STATUS = {
    "status": "SUCCEEDED",
    "outputs": [{"label": "shots", "uri": f"s3://{_PROXY}/micro-workflow/shot-metadata-enrichment/"
                                        "6e060617-adb4-5327-a362-0dcd5733489f/shot_metadata.jsonl"}],
    "output_s3_prefix": f"s3://{_PROXY}/micro-workflow/shot-metadata-enrichment/"
                        "6e060617-adb4-5327-a362-0dcd5733489f",
}
_CONV_SFN = {"uuid": "cf9ccf64-5d66-5372-90bd-8380987b72df", "total_estimated_cost": 0.010674}
_CONV_STATUS = {
    "status": "SUCCEEDED",
    "outputs": [{"label": "analysis", "uri": f"s3://{_PROXY}/micro-workflow/conversation-analysis/"
                                           "cf9ccf64-5d66-5372-90bd-8380987b72df/"
                                           "conversation_analysis.json"}],
    "output_s3_prefix": f"s3://{_PROXY}/micro-workflow/conversation-analysis/"
                        "cf9ccf64-5d66-5372-90bd-8380987b72df",
}


def test_bare_enrichment_sfn_output_resolves_to_nothing():
    # Pins the defect: the stored value alone names no file.
    rec = rr.resolve_node("sg3", {"status": "completed", "value": _ENRICH_SFN})
    assert rec["artifacts"] == []
    assert rr.declared_output_refs(_ENRICH_SFN) == []


def test_merge_status_outputs_declares_the_enrichment_jsonl():
    merged = rr.merge_status_outputs(_ENRICH_SFN, _ENRICH_STATUS)
    refs = rr.declared_output_refs(merged)
    assert [(b, k) for b, k, _ in refs] == [(
        _PROXY, "micro-workflow/shot-metadata-enrichment/6e060617-adb4-5327-a362-0dcd5733489f/"
                "shot_metadata.jsonl")]
    assert "shots" in refs[0][2]
    assert rr.output_prefix(merged) == _ENRICH_STATUS["output_s3_prefix"]
    assert merged["uuid"] == _ENRICH_SFN["uuid"]
    assert "outputs" not in _ENRICH_SFN  # input not mutated


def test_resolve_node_emits_declared_outputs_as_artifacts():
    for sfn, status, base in ((_ENRICH_SFN, _ENRICH_STATUS, "shot_metadata.jsonl"),
                              (_CONV_SFN, _CONV_STATUS, "conversation_analysis.json")):
        rec = rr.resolve_node("n", {"status": "completed",
                                    "value": rr.merge_status_outputs(sfn, status)})
        assert rec["kind"] == rr.KIND_ANALYSIS
        assert [a["url"].rsplit("/", 1)[-1] for a in rec["artifacts"]] == [base]
        assert rec["artifacts"][0]["role"] == "analysis"


def test_merge_status_outputs_accepts_an_mcp_envelope_and_ignores_junk():
    import json as _json
    env = {"content": [{"type": "text", "text": _json.dumps(_CONV_STATUS)}]}
    assert rr.declared_output_refs(rr.merge_status_outputs(_CONV_SFN, env))
    # A status with no outputs, a prefix-only entry, or a non-s3 uri declares nothing.
    for status in ({"status": "SUCCEEDED"},
                   {"outputs": [{"label": "x", "uri": "s3://b/prefix/"}]},
                   {"outputs": [{"label": "x", "uri": "https://example.com/a.json"}]},
                   "not a dict"):
        assert rr.merge_status_outputs(_CONV_SFN, status) is _CONV_SFN


def test_merge_keeps_an_existing_output_prefix():
    value = {"output": {"s3uri": "s3://b/micro-workflow/shot-segmentation/x/"}}
    merged = rr.merge_status_outputs(value, _ENRICH_STATUS)
    assert rr.output_prefix(merged) == "s3://b/micro-workflow/shot-segmentation/x/"


def test_bookkeeping_outputs_are_not_declared_deliverables():
    # The live enrichment status reply (6e060617) also declares window_manifest.json and
    # usage_tracking.json; they are M2C bookkeeping and must not reach Phase 3 prompts or imports.
    pre = _ENRICH_STATUS["output_s3_prefix"]
    status = {**_ENRICH_STATUS, "outputs": _ENRICH_STATUS["outputs"] + [
        {"label": "scenes", "uri": f"{pre}/scenes.json"},
        {"label": "window-manifest", "uri": f"{pre}/window_manifest.json"},
        {"label": "usage", "uri": f"{pre}/usage_tracking.json"},
        {"label": "renamed", "uri": f"{pre}/usage_tracking.json"},
    ]}
    bases = [k.rsplit("/", 1)[-1]
             for _, k, _ in rr.declared_output_refs(rr.merge_status_outputs(_ENRICH_SFN, status))]
    assert bases == ["shot_metadata.jsonl", "scenes.json"]
    # conversation-analysis's live reply (session 00ad3584) also lists its chunking plan.
    cpre = _CONV_STATUS["output_s3_prefix"]
    conv = {**_CONV_STATUS, "outputs": _CONV_STATUS["outputs"] + [
        {"label": "chunk-manifest", "uri": f"{cpre}/chunk_manifest.json"}]}
    assert [k.rsplit("/", 1)[-1] for _, k, _ in
            rr.declared_output_refs(rr.merge_status_outputs(_CONV_SFN, conv))] == ["conversation_analysis.json"]


def test_resolve_node_reads_declared_outputs_inside_a_wrapped_execution_output():
    # The poller's lone-job payload is {status, error, executionOutput: <merged>}.
    wrapped = {"status": "SUCCEEDED", "error": None,
               "executionOutput": rr.merge_status_outputs(_ENRICH_SFN, _ENRICH_STATUS)}
    rec = rr.resolve_node("job", {"status": "SUCCEEDED", "value": wrapped},
                          read_s3_json=lambda b, k, max_bytes=None: (None, 10))
    assert rec["kind"] == rr.KIND_ANALYSIS, rec
    assert rec["error"] == ""
    assert [a["url"].rsplit("/", 1)[-1] for a in rec["artifacts"]] == ["shot_metadata.jsonl"]
