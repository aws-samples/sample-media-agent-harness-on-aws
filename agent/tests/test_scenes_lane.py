"""Scenes on the timeline: an enrichment run's scenes.json renders one Scenes lane in its VIDEO's tab.

IND403 Lab 1: "show scenes" listed 18 scenes in chat and nothing appeared on the timeline. scenes.json had no render
declaration, and the enrichment job (handed a shot RUN, not the video) recorded no source video.
"""
from pathlib import Path

from hooks.provider import upstream_job_ids
from render import projection
from render.skill_catalog import _parse_skill_frontmatter

REFS = Path(__file__).resolve().parents[1] / "skills" / "render-media" / "references"
SCENES = {"scenes": [
    {"scene_id": 0, "start_time": 0.0, "end_time": 0.968, "setting": None, "boundary_reason": "after_black"},
    {"scene_id": 1, "start_time": 1.001, "end_time": 29.696, "setting": "street_sidewalk", "boundary_reason": "location_change"},
]}


def test_the_scenes_declaration_projects_one_tagged_scenes_row():
    decl = _parse_skill_frontmatter((REFS / "scenes.md").read_text())["declaration"]
    assert decl["tool"] == "shot-metadata-enrichment" and decl["lane_set"] == "scenes"
    # A band like Shots (markers), never transcript rows (segments): the first version mixed 18 scenes into the
    # transcript list and drew them as one solid bar.
    assert "segments" not in decl
    marks = projection.project_markers(SCENES, decl["markers"], decl["lane_set"])
    assert marks == [
        {"time": 0.0, "endTime": 0.968, "label": "0", "note": "after_black", "id": "scenes-0", "laneSet": "scenes"},
        {"time": 1.001, "endTime": 29.696, "label": "1", "category": "street_sidewalk", "note": "location_change",
         "id": "scenes-1", "laneSet": "scenes"},
    ]  # ids never collide with the shots band's m-<n>


def test_lane_rules_name_their_sets():
    sets = {f: _parse_skill_frontmatter((REFS / f).read_text())["declaration"].get("lane_set")
            for f in ("speaker-diarization.md", "face-recognition.md", "face-people.md", "scenes.md")}
    assert sets == {"speaker-diarization.md": "speakers", "face-recognition.md": "people",
                    "face-people.md": "people", "scenes.md": "scenes"}


def test_an_undeclared_set_keeps_the_old_ids():
    segs = projection.project_segments(SCENES, "scenes[] start=start_time end=end_time group=boundary_reason")
    assert segs[0]["id"] == "s-0" and "laneSet" not in segs[0]


def test_a_chained_job_finds_the_run_it_ran_on():
    enrichment = {
        "jobId": "arn:aws:states:us-west-2:100000000002:execution:m2cv6-0e0000000002-shot-metadata-enrichment:"
                 "527bb2c5-ebc3-5d9f-8f69-8b120d493032",
        "statusTool": "shot-metadata-enrichment___get_job_status",
        "lineageInputs": [
            "s3://proxy/micro-workflow/shot-segmentation/bf46610e-f202-5b62-8c6f-f0102e6e22ce",
            "s3://proxy/micro-workflow/speech-diarization/14e0af78-4b03-5832-a269-fed27adb87f3/transcripts/final_transcript.json",
        ],
    }
    assert upstream_job_ids(enrichment) == [
        "arn:aws:states:us-west-2:100000000002:execution:m2cv6-0e0000000002-shot-segmentation:bf46610e-f202-5b62-8c6f-f0102e6e22ce",
        "arn:aws:states:us-west-2:100000000002:execution:m2cv6-0e0000000002-speech-diarization:14e0af78-4b03-5832-a269-fed27adb87f3",
    ]


def test_a_moment_says_which_search_found_it():
    from render.moments import found_by
    assert found_by({"match_sources": ["ann:vector", "fts:named_entities"]}) == ("meaning", "keyword")
    assert found_by({"match_sources": ["fts:text"]}) == ("keyword",)
    assert found_by({}) == () and found_by({"match_sources": ["rrf"]}) == ()
    # The shape M2C sends: source -> score.
    assert found_by({"match_sources": {"ann:vector": 0.0117, "fts:named_entities": 0.02}}) == ("meaning", "keyword")


def test_a_job_on_media_or_of_another_shape_has_no_upstream():
    assert upstream_job_ids({"jobId": "fal-request-123", "statusTool": "fal___status", "lineageInputs": []}) == []
    assert upstream_job_ids({"jobId": "arn:aws:states:us-west-2:1:execution:x-shot-segmentation:"
                             "bf46610e-f202-5b62-8c6f-f0102e6e22ce", "statusTool": "shot-segmentation___get_job_status",
                             "lineageInputs": ["s3://b/projects/p/import/Meridian.mp4"]}) == []
