"""update_timeline: an agent-computed lane set folds into the video's open timeline and replaces its set there.

IND403 Lab 1: a speaker relabel the agent wrote to a file, and the scenes it computed, never reached the timeline.
Speech is lanes + captions (the transcript); any other set is a band drawn like Shots, never transcript rows.
"""
from project_search.project_tools import LANE_ITEMS_MAX, _lane_manifest

SRC = "s3://ingest/projects/p1/import/Meridian.mp4"


def test_speakers_become_lanes_and_captions_and_name_the_video():
    m, why = _lane_manifest("sess", "video:a", "Meridian.mp4", SRC, "speakers", [
        {"start": 1, "end": 2.5, "lane": "SPEAKER_00", "text": "So these guys just disappeared?"},
        {"start": 336.0, "end": 337.3, "lane": "SPEAKER_01", "text": "Officer, please come in, please."},
    ])
    assert why == "" and m["laneSet"] == "speakers"
    assert m["sourceAssetId"] == "video:a" and m["sourceLocation"] == SRC  # folds into the video's tab
    assert [s["group"] for s in m["segments"]] == ["SPEAKER_00", "SPEAKER_01"]
    assert m["captions"][1] == {"start": 336.0, "end": 337.3, "speaker": "SPEAKER_01", "text": "Officer, please come in, please."}
    assert m["template"] == "nle-timeline" and m["duration"] == 337.3 and "markers" not in m


def test_scenes_are_a_band_not_transcript_rows():
    m, _ = _lane_manifest("sess", "video:a", "Meridian.mp4", SRC, "scenes",
                          [{"start": 0, "end": 23.0, "text": "street_sidewalk"}, {"start": 23.0, "end": 200.0}])
    assert "segments" not in m and "captions" not in m
    assert m["markers"] == [
        {"id": "scenes-0", "time": 0.0, "endTime": 23.0, "label": "1", "category": "street_sidewalk", "laneSet": "scenes"},
        {"id": "scenes-1", "time": 23.0, "endTime": 200.0, "label": "2", "laneSet": "scenes"},
    ]


def test_one_object_per_session_video_and_set():
    a, _ = _lane_manifest("s", "video:a", "x", SRC, "scenes", [{"start": 0, "end": 1}])
    b, _ = _lane_manifest("s", "video:a", "x", SRC, "scenes", [{"start": 2, "end": 3}])
    c, _ = _lane_manifest("s", "video:a", "x", SRC, "speakers", [{"start": 0, "end": 1, "lane": "S0"}])
    assert a["id"] == b["id"] != c["id"]


def test_bad_items_are_refused_with_a_reason():
    for set_name, items, needle in [("scenes", [], "non-empty"), ("scenes", [{"start": 2, "end": 1}], "start < end"),
                                    ("scenes", [{"end": 1}], "needs start and end"),
                                    ("speakers", [{"start": 0, "end": 1}], "needs its lane"),
                                    ("scenes", [{"start": 0, "end": 1}] * (LANE_ITEMS_MAX + 1), "at most")]:
        m, why = _lane_manifest("s", "video:a", "x", SRC, set_name, items)
        assert m is None and needle in why
