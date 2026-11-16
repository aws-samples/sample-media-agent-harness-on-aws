"""Video-annotation rendering: source-media context + structural box normalization.

These tests use the real shapes observed in a live face-recognition run
(``facemetadata.json`` / ``facegroup.json``), but the code under test keys off
*structure* only — no tool names, gateway ids, or field allowlists — so any
analyzer that reports timestamped normalized boxes renders the same way.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import render_agent as ra
from hooks.provider import ctx_after_tool_result, extract_source_uris


PROXY = "s3://mediaworks-proxy-100000000002-us-west-2"
RUN = f"{PROXY}/micro-workflow/face-recognition/run-1"
ASSET = "s3://mediaworks-harness-skills-store-100000000002-us-west-2/artifacts/meridian.mp4"


def _status_result() -> dict:
    """The live ``get_job_status`` success payload (trimmed)."""
    return {
        "status": "success",
        "content": [{
            "json": {
                "status": "SUCCEEDED",
                "asset_s3uri": ASSET,
                "output_s3_prefix": RUN,
                "outputs": [
                    {"uri": f"{RUN}/facemetadata.json", "label": "face-metadata",
                     "mime": "application/json"},
                    {"uri": f"{RUN}/facegroup.json", "label": "face-groups",
                     "mime": "application/json"},
                    {"uri": f"{RUN}/usage_tracking.json", "label": "usage",
                     "mime": "application/json"},
                    {"uri": f"{RUN}/debug/people/", "label": "people-images"},
                ],
            },
        }],
    }


# --- source media is context, never an eligibility signal -------------------

def test_source_media_extracted_from_input_reference():
    assert extract_source_uris(_status_result()) == [ASSET]


def test_source_media_is_not_an_output_artifact():
    ctx = ctx_after_tool_result(type("E", (), {
        "tool_use": {"name": "face-recognition___get_job_status", "toolUseId": "t1"},
        "result": _status_result(),
    })())
    assert ctx["result"]["source_urls"] == [ASSET]
    assert ASSET not in ctx["result"]["urls"]
    # mimes drive the render condition; the source asset must not contribute.
    assert "video/mp4" not in ctx["result"]["mimes"]


def test_running_poll_with_only_source_media_does_not_trigger_render():
    """A poll that names only the input asset must not fire the render hook.

    Source media stays available as render context, but the built-in condition
    still requires a completed job, so an in-progress poll renders nothing.
    """
    from hooks.defaults import default_hook_specs
    from hooks.expr import evaluate

    input_only = {
        "status": "success",
        "content": [{"json": {"status": "RUNNING", "asset_s3uri": ASSET}}],
    }
    ctx = ctx_after_tool_result(type("E", (), {
        "tool_use": {"name": "any___get_job_status", "toolUseId": "t2"},
        "result": input_only,
    })())
    assert ctx["result"]["source_urls"] == [ASSET]
    assert ctx["result"]["job_status"] == "RUNNING"

    condition = next(s for s in default_hook_specs() if s.name == "render-artifacts").condition
    assert evaluate(condition, ctx) is False


def test_render_hook_passes_source_media_as_a_separate_parameter():
    from hooks.defaults import default_hook_specs

    spec = next(s for s in default_hook_specs() if s.name == "render-artifacts")
    # Renders the gated set, not every url in the result (see
    # test_render_dispatches_only_the_artifacts_the_condition_matched).
    assert spec.action_params["urls"] == "result.new_artifacts"
    assert spec.action_params["source_urls"] == "result.source_urls"
    # Eligibility must stay output-only: the condition never reads source media.
    assert "source_urls" not in spec.condition


def test_source_media_section_is_parsed_separately_from_artifacts():
    prompt = (
        "Render these artifacts as one unified timeline:\n"
        "https://cdn.example.com/facemetadata.json\n"
        "https://cdn.example.com/person_0000.jpg\n\n"
        "Source media (context only, not an output artifact):\n"
        "https://cdn.example.com/meridian.mp4"
    )
    artifacts, sources = ra._split_render_request(prompt)
    assert artifacts == [
        "https://cdn.example.com/facemetadata.json",
        "https://cdn.example.com/person_0000.jpg",
    ]
    assert sources == ["https://cdn.example.com/meridian.mp4"]


def test_prompt_without_source_section_keeps_all_urls_as_artifacts():
    artifacts, sources = ra._split_render_request(
        "Render these artifacts as one unified timeline:\nhttps://cdn.example.com/a.mp4")
    assert artifacts == ["https://cdn.example.com/a.mp4"]
    assert sources == []


def test_already_rendered_section_urls_are_not_deliverables():
    # Root cause of "1-url panel dispatch renders 5 images" (session ccbc9559): with no source
    # section, the ALREADY-RENDERED reference urls were scraped as deliverables. They must be
    # neither artifacts nor sources — pure context.
    prompt = (
        "These are the CANDIDATE OUTPUT URLS (pass them as candidates ...):\n"
        "https://cdn.example.com/panel4.jpg\n\n"
        "ALREADY RENDERED this session (pass as rendered to validate_render_urls; do not "
        "re-render):\n"
        "https://cdn.example.com/1786724952475-James-dark.jpg\n"
        "https://cdn.example.com/ref2.jpg"
    )
    artifacts, sources = ra._split_render_request(prompt)
    assert artifacts == ["https://cdn.example.com/panel4.jpg"]
    assert sources == []


def test_pdf_candidate_with_already_rendered_source_videos_split():
    # Split-level repro of "PDF renders the source video" (session 2693e705): the prior source
    # videos arrived in the ALREADY-RENDERED section with no source heading, so they were scraped
    # as deliverables and the media timeline won. Only the PDF is a deliverable.
    prompt = (
        "These are the CANDIDATE OUTPUT URLS (...):\n"
        "https://cdn.example.com/report.pdf\n\n"
        "ALREADY RENDERED this session (...):\n"
        "https://cdn.example.com/proxy_60s.mp4\n"
        "https://cdn.example.com/Netflix_Open_Content_Meridian.mp4"
    )
    artifacts, sources = ra._split_render_request(prompt)
    assert artifacts == ["https://cdn.example.com/report.pdf"]
    assert sources == []


def test_recent_user_requests_urls_are_not_deliverables():
    prompt = (
        "These are the CANDIDATE OUTPUT URLS (...):\n"
        "https://cdn.example.com/panel.jpg\n\n"
        "RECENT USER REQUESTS (most recent first) — render the one(s) matching what the user "
        "asked for:\n"
        "- please compare with https://cdn.example.com/old_reference.jpg"
    )
    artifacts, sources = ra._split_render_request(prompt)
    assert artifacts == ["https://cdn.example.com/panel.jpg"]
    assert sources == []


def test_source_section_bounded_by_following_context_section():
    # Sources come only from the SOURCE section, not the ALREADY-RENDERED section that follows it.
    prompt = (
        "These are the CANDIDATE OUTPUT URLS (...):\n"
        "https://cdn.example.com/facegroup.json\n\n"
        "Source media (context only, not an output artifact):\n"
        "https://cdn.example.com/meridian.mp4\n\n"
        "ALREADY RENDERED this session (...):\n"
        "https://cdn.example.com/old_clip.mp4"
    )
    artifacts, sources = ra._split_render_request(prompt)
    assert artifacts == ["https://cdn.example.com/facegroup.json"]
    assert sources == ["https://cdn.example.com/meridian.mp4"]


def test_split_regexes_match_the_actual_dispatcher_headings():
    # Drift guard for the exact bug class: the parser (_split_render_request) must recognize
    # every section heading the dispatcher (render_artifacts.dispatch_render) actually emits. If
    # a heading is reworded there without updating the regexes here, context urls silently get
    # scraped as deliverables again (PDF renders source video / 5 images from a 1-url dispatch).
    src = (Path(ra.__file__).resolve().parent / "render" / "artifacts.py").read_text()
    # 1) The dispatcher still emits each heading we bound on (catches a rename there).
    assert 'SOURCE_MEDIA_PROMPT_HEADING = "Source media' in src
    assert "ALREADY RENDERED this session" in src
    assert "RECENT USER REQUESTS" in src
    # 2) Each of those headings is classified as the right kind of boundary here.
    assert ra._SOURCE_SECTION_RE.search("Source media (context only, not an output artifact):")
    assert ra._CONTEXT_SECTION_RE.search(
        "ALREADY RENDERED this session (pass as rendered to validate_render_urls; do not re-render):")
    assert ra._CONTEXT_SECTION_RE.search(
        "RECENT USER REQUESTS (most recent first) — render the one(s) matching what the user asked for:")
    # 3) The candidate heading is NOT a boundary (deliverables live under it).
    assert not ra._SOURCE_SECTION_RE.search("These are the CANDIDATE OUTPUT URLS (...):")
    assert not ra._CONTEXT_SECTION_RE.search("These are the CANDIDATE OUTPUT URLS (...):")


# --- structural box normalization -------------------------------------------

def test_normalize_box_accepts_known_spellings():
    expected = {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4}
    assert ra._normalize_box({"l": 0.1, "t": 0.2, "w": 0.3, "h": 0.4}) == expected
    assert ra._normalize_box({"left": 0.1, "top": 0.2, "width": 0.3, "height": 0.4}) == expected
    assert ra._normalize_box({"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}) == expected
    assert ra._normalize_box([0.1, 0.2, 0.3, 0.4]) == expected


def test_normalize_box_rejects_pixel_and_degenerate_boxes():
    assert ra._normalize_box({"l": 12, "t": 40, "w": 220, "h": 300}) is None
    assert ra._normalize_box({"l": 0.1, "t": 0.1, "w": 0, "h": 0.2}) is None
    assert ra._normalize_box("nope") is None


def test_collect_timed_boxes_handles_seconds_and_milliseconds():
    found = ra._collect_timed_boxes({
        "faces": [
            {"timestamp": 30.03, "box": {"l": 0.2, "t": 0.1, "w": 0.1, "h": 0.2}},
            {"begin_ms": 31031, "box": {"l": 0.4, "t": 0.1, "w": 0.1, "h": 0.2}},
        ],
    })
    assert [entry["time"] for entry in found] == [30.03, 31.031]


def test_annotations_hold_then_expire():
    boxes = [
        {"time": 10.0, "box": {"x": 0, "y": 0, "width": 0.1, "height": 0.1}},
        {"time": 11.0, "box": {"x": 0.1, "y": 0, "width": 0.1, "height": 0.1}},
        {"time": 40.0, "box": {"x": 0.2, "y": 0, "width": 0.1, "height": 0.1}},
    ]
    annotations = ra._annotations_for_entity("entity-0", boxes)
    # Close samples bridge to the next detection…
    assert annotations[0]["end"] == 11.0
    # …a long gap only holds briefly instead of inventing a detection.
    assert annotations[1]["end"] == 12.0
    assert annotations[2]["end"] == 41.0


# --- manifest composition ---------------------------------------------------

def _facegroup(person_id: int, name: str, thumb: str) -> dict:
    return {
        "person_id": person_id,
        "name": name,
        "named": True,
        "confidence": 98.4,
        "total_screen_time_sec": 8.8,
        "thumbnail_key": thumb,
        "appearances": [{"begin_ms": 30030, "end_ms": 30830, "representative_frame": 900}],
        "tracks": [{
            "faces": [
                {"frame_num": 900, "timestamp": 30.03, "frame_key": "frame_0000900.jpg",
                 "box": {"l": 0.31, "t": 0.18, "w": 0.14, "h": 0.27}},
                {"frame_num": 930, "timestamp": 31.031, "frame_key": "frame_0000930.jpg",
                 "box": {"l": 0.33, "t": 0.18, "w": 0.14, "h": 0.27}},
            ],
        }],
    }


def _sidecars() -> dict:
    return {
        "https://cdn.example.com/facegroup.json": {
            "version": 1,
            "people": [
                _facegroup(0, "Recognized Person", "run-1/debug/people/person_0000.jpg"),
                _facegroup(1, "Person_2", "run-1/debug/people/person_0001.jpg"),
            ],
            "duration_sec": 718.9,
            "stats": {"frames_analyzed": 59, "num_people": 2},
        },
    }


def test_video_annotations_manifest_from_live_shape():
    images = [
        "https://cdn.example.com/debug/people/person_0000.jpg?sig=a",
        "https://cdn.example.com/debug/people/person_0001.jpg?sig=b",
    ]
    manifest = ra._build_video_annotations_manifest(
        "https://cdn.example.com/meridian.mp4?sig=v",
        _sidecars(),
        images,
        images + ["https://cdn.example.com/facegroup.json"],
        "manifest-1",
    )
    assert manifest is not None
    assert manifest["template"] == "video-annotations"
    assert manifest["media"]["src"] == "https://cdn.example.com/meridian.mp4?sig=v"
    # A real video track keeps the manifest schema-valid and gives the UI a
    # sensible fallback if the annotation template is unavailable.
    assert manifest["tracks"][0]["type"] == "video"
    assert manifest["duration"] == 718.9

    assert [e["label"] for e in manifest["entities"]] == ["Recognized Person", "Person_2"]
    assert manifest["entities"][0]["thumbnail"] == images[0]
    assert manifest["entities"][0]["confidence"] == 98.4
    assert manifest["entities"][0]["screenTimeSec"] == 8.8

    boxes = [a for a in manifest["annotations"] if a["entityId"] == "entity-0"]
    assert [a["start"] for a in boxes] == [30.03, 31.031]
    assert boxes[0]["box"] == {"x": 0.31, "y": 0.18, "width": 0.14, "height": 0.27}
    assert manifest["appearances"][0] == {"entityId": "entity-0", "start": 30.03, "end": 30.83}
    assert manifest["metadata"]["entities"] == 2


def _raw_track(track_id: int) -> dict:
    """A face_tracks.json entry: a raw per-detection track, no identity grouping."""
    return {
        "track_id": track_id,
        "faces": [
            {"frame_num": 30 * track_id, "timestamp": float(track_id),
             "frame_key": f"frame_{track_id}.jpg",
             "box": {"l": 0.3, "t": 0.2, "w": 0.1, "h": 0.2}},
        ],
    }


def test_grouped_people_win_over_raw_tracks():
    """Job 8ec0687a grouped 57 tracks / 58 detections into 2 named people, but selecting the
    entity list purely by box count rendered 57 anonymous 'Person N' subjects. When both the
    grouped facegroup people[] and the raw face_tracks tracks[] are present, the grouped,
    named identities must win even though the raw list has far more boxes."""
    sidecars = _sidecars()
    sidecars["https://cdn.example.com/face_tracks.json"] = {
        "tracks": [_raw_track(i) for i in range(57)]  # 57 boxes vs the people list's 4
    }
    manifest = ra._build_video_annotations_manifest(
        "https://cdn.example.com/meridian.mp4?sig=v",
        sidecars,
        ["https://cdn.example.com/debug/people/person_0000.jpg?sig=a"],
        [],
        "m2",
    )
    assert manifest is not None
    assert manifest["metadata"]["entities"] == 2, "grouped people must win, not 57 raw tracks"
    assert [e["label"] for e in manifest["entities"]] == ["Recognized Person", "Person_2"]


def test_raw_tracks_still_render_when_no_grouped_list_exists():
    """Fallback intact: if only a raw track list exists (no facegroup), it still renders."""
    sidecars = {"https://cdn.example.com/face_tracks.json": {
        "tracks": [_raw_track(i) for i in range(3)]
    }}
    manifest = ra._build_video_annotations_manifest(
        "https://cdn.example.com/meridian.mp4?sig=v", sidecars, [], [], "m3")
    assert manifest is not None
    assert manifest["metadata"]["entities"] == 3


def test_video_annotations_manifest_is_tool_agnostic():
    """An unrelated analyzer with the same structure renders identically."""
    sidecars = {
        "https://cdn.example.com/detections.json": {
            "objects": [{
                "label": "Delivery van",
                "score": 91.2,
                "detections": [
                    {"time": 2.0, "bbox": {"x": 0.5, "y": 0.4, "width": 0.2, "height": 0.3}},
                ],
            }],
        },
    }
    manifest = ra._build_video_annotations_manifest(
        "https://cdn.example.com/street.mp4", sidecars, [], [], "manifest-2")
    assert manifest is not None
    assert manifest["entities"][0]["label"] == "Delivery van"
    assert manifest["annotations"][0]["box"]["x"] == 0.5


def test_no_annotation_manifest_without_boxes_or_media():
    plain = {"https://cdn.example.com/summary.json": {"people": [{"name": "A", "count": 2}]}}
    assert ra._build_video_annotations_manifest(
        "https://cdn.example.com/a.mp4", plain, [], [], "m") is None
    assert ra._build_video_annotations_manifest("", _sidecars(), [], [], "m") is None


def test_generic_manifest_describes_artifacts_for_gallery():
    manifest = ra._build_generic_manifest([
        "https://cdn.example.com/facemetadata.json",
        "https://cdn.example.com/person_0000.jpg",
    ], "manifest-3")
    assert manifest["template"] == "generic-preview"
    kinds = {a["kind"] for a in manifest["artifacts"]}
    assert kinds == {"data", "image"}
    assert manifest["artifacts"][1]["label"] == "person 0000"


def test_new_candidate_labeled_prompt_still_splits_outputs_from_sources():
    """dispatch_render now labels the urls as CANDIDATE OUTPUTS and adds a source note; the
    render agent's parser must still separate outputs from sources so validate_render_urls gets
    the right arrays."""
    prompt = (
        "Render these artifacts as one unified timeline. These are the CANDIDATE OUTPUT URLS "
        "(pass them as candidates to validate_render_urls, and render only the urls it returns "
        "as valid):\n"
        "https://cdn.example.com/proxy/transcoded.mp4\n"
        "https://cdn.example.com/facemetadata.json\n\n"
        "Source media (context only, not an output artifact):\n"
        "https://cdn.example.com/original_upload.mp4\n"
        "(These SOURCE urls are inputs — pass them as source to validate_render_urls; never "
        "render them as outputs.)"
    )
    artifacts, sources = ra._split_render_request(prompt)
    assert "https://cdn.example.com/proxy/transcoded.mp4" in artifacts
    assert "https://cdn.example.com/facemetadata.json" in artifacts
    assert sources == ["https://cdn.example.com/original_upload.mp4"]
    # and the input must NOT leak into artifacts
    assert "original_upload.mp4" not in " ".join(artifacts)


def test_deliverable_declaration_is_parsed():
    import render_agent as ra
    md = ("---\nname: render-shot-segmentation\nhandles: application/json\n"
          "detect: shots\ntemplate: video-markers\ndeliverable: shots.json\n---\nbody")
    meta = ra._parse_skill_frontmatter(md)
    assert meta["declaration"].get("deliverable") == "shots.json"


def test_deliverable_absent_is_fine():
    import render_agent as ra
    md = "---\nname: x\nhandles: application/json\ndetect: shots\ntemplate: t\n---\nbody"
    assert "deliverable" not in ra._parse_skill_frontmatter(md)["declaration"]


def test_humanize_skill_name_titles_the_work():
    import render_agent as ra
    assert ra._humanize_skill_name("render-shot-segmentation") == "Shot Segmentation"
    assert ra._humanize_skill_name("render-face-recognition") == "Face Recognition"
    assert ra._humanize_skill_name("") == ""


def test_label_from_tool_uses_the_target_segment():
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
    from hooks import provider as pv
    assert pv._label_from_tool("face-recognition___recognize_faces") == "Face Recognition"
    assert pv._label_from_tool("shot-segmentation___segment_shots") == "Shot Segmentation"
    assert pv._label_from_tool(None) == ""
    assert pv._label_from_tool("") == ""


def test_a_stated_duration_is_the_media_length_and_clamps_the_last_sample():
    """8e2acba8: the last 1-second face sample (at 168.17 s) ended at 169.17 s on a 168.27 s clip, and the
    timeline ran past the last frame. A stated duration is the media's length; appearances are clamped to it."""
    sc = _sidecars()
    data = next(v for v in sc.values() if isinstance(v, dict) and "duration_sec" in v)
    data["duration_sec"] = 1.5  # shorter than the last sample's end
    manifest = ra._build_video_annotations_manifest(
        "https://cdn.example.com/meridian.mp4?sig=v", sc, [], ["https://cdn.example.com/facegroup.json"], "m-2")
    assert manifest is not None and manifest["duration"] == 1.5
    for a in manifest.get("annotations") or []:
        assert a["end"] <= 1.5


def test_a_recognized_persons_name_rides_on_its_boxes():
    """The people lanes and the box label read the annotation; 8e2acba8 showed "Person 1" for Kevin Kilner."""
    sc = _sidecars()
    data = next(v for v in sc.values() if isinstance(v, dict) and "people" in v)
    data["people"][0]["name_source"] = "celebrity"
    data["people"][1]["named"] = False  # an unrecognized face keeps the friendly "Person N"
    manifest = ra._build_video_annotations_manifest(
        "https://cdn.example.com/meridian.mp4?sig=v", sc, [], ["https://cdn.example.com/facegroup.json"], "m-3")
    named = [a for a in manifest["annotations"] if a["entityId"] == "entity-0"]
    assert named and all(a.get("name") == "Recognized Person" for a in named)
    assert all("name" not in a for a in manifest["annotations"] if a["entityId"] == "entity-1")
