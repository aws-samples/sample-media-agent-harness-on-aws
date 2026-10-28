"""Phase 1 entity-metadata adapters (``ingestion.adapters``): face-recognition / program-structure / cast,
and the timed entity MENTIONS they flatten into for the scene-document fold.

Fixtures are the real staging outputs (``facemetadata.json``, ``structure.json``) and the
cast-resolution ``describe_tool`` contract example (``persons.json`` — the workflow had no historical
run), trimmed. See docs/entity-metadata-workflows.md.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ingestion import adapters as A  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "ingestion", "entities")


def _load(name):
    with open(os.path.join(FIX, name)) as fh:
        return json.load(fh)


def test_person_identities_from_face_metadata():
    people = A.person_identities(_load("facemetadata.json"))
    assert [p["person_id"] for p in people] == [0, 2]
    kevin, anon = people
    # A resolved celebrity identity, times already in ms, screen time seconds -> ms.
    assert kevin["name"] == "Kevin Kilner" and kevin["named"] is True
    assert kevin["name_source"] == "celebrity" and kevin["screen_ms"] == 8800
    assert kevin["appearances"] == [{"start_ms": 30030, "end_ms": 30830},
                                    {"start_ms": 31031, "end_ms": 31831}]
    # An anonymous track keeps its placeholder name but named=False and no name_source.
    assert anon["name"] == "Person_2" and anon["named"] is False
    assert "name_source" not in anon  # _compact drops the null source


def test_person_identities_is_total_on_junk():
    assert A.person_identities(None) == []
    assert A.person_identities({"people": [None, {}, {"person_id": "x"}]}) == []


def test_program_structure_from_structure_json():
    prog = A.program_structure(_load("structure.json"))
    assert prog["mode"] == "narrative"
    # Only non-empty marker kinds survive; seconds -> whole ms.
    assert set(prog["markers"]) == {"blackframes", "end_credits"}
    assert prog["markers"]["blackframes"][0] == {"start_ms": 0, "end_ms": 968, "kinds": ["black"],
                                                 "shot_ids": [0]}
    assert prog["markers"]["end_credits"][0]["kinds"] == ["credit_roll", "logo_bumper"]
    # Sequences carry id/span/scene ids/settings.
    seqs = prog["sequences"]
    assert [s["sequence_id"] for s in seqs] == [0, 1]
    assert seqs[1]["start_ms"] == 1001 and seqs[1]["settings"] == ["beach_coast", "office"]
    assert seqs[1]["scene_ids"] == [1, 2, 3]
    # Break candidates: at -> ms, score kept (including 0-ish values).
    assert prog["break_candidates"] == [{"at_ms": 580110, "score": 2.0}]


def test_program_structure_is_total_on_junk():
    assert A.program_structure(None) == {}
    assert A.program_structure({"sequences": [None], "break_candidates": [None]}) == {}


def test_cast_characters_from_persons_json():
    cast = A.cast_characters(_load("persons.json"))
    assert [c["person_id"] for c in cast] == [3, 7, 11]
    miriam, tomas, anon = cast
    assert miriam["name"] == "Miriam Okafor" and miriam["named"] is True
    assert miriam["role"] == "anchor" and miriam["type"] == "on_camera_speaker"
    assert miriam["screen_ms"] == 388200 and miriam["speech_ms"] == 412500
    assert miriam["appearances"] == [{"start_ms": 12400, "end_ms": 31800}]
    # character_name is additive; the actor name stays in `name`.
    assert tomas["name"] == "Tomas Vega" and tomas["character_name"] == "Det. Rios"
    # An abstained entry: name=null -> named False, aliases preserved.
    assert anon["named"] is False and "name" not in anon and anon["aliases"] == ["Tom"]


def test_cast_characters_is_total_on_junk():
    assert A.cast_characters(None) == []
    assert A.cast_characters({"persons": [None, {"person_id": -1}]}) == []


# ------------------------------------------------- output-shape drift observability (Change 3)
def test_cast_drift_logs_when_nonempty_input_yields_no_rows(caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="ingestion.adapters"):
        # person_id missing from every entry (a shape drift) → zero emitted despite a non-empty input.
        assert A.cast_characters({"persons": [{"name": "A"}, {"name": "B"}]}) == []
    assert any("cast-resolution" in r.message and "drift" in r.message for r in caplog.records)


def test_person_identities_drift_logs(caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="ingestion.adapters"):
        assert A.person_identities({"people": [{"name": "A"}, {"name": "B"}]}) == []
    assert any("face-recognition" in r.message and "drift" in r.message for r in caplog.records)


def test_program_structure_drift_logs(caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="ingestion.adapters"):
        # sequences present but each entry is unreadable → no compact output.
        assert A.program_structure({"sequences": [None, None]}) == {}
    assert any("program-structure-analysis" in r.message and "drift" in r.message for r in caplog.records)


def test_no_drift_log_on_healthy_or_empty_input(caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="ingestion.adapters"):
        A.cast_characters({"persons": []})          # empty input: not drift
        A.cast_characters(_load("persons.json"))    # healthy: emits rows
        A.person_identities(_load("facemetadata.json"))
    assert not any("drift" in r.message for r in caplog.records)


# ------------------------------------------------- entity mentions flattened for the scene-document fold
def test_person_mentions_one_per_appearance_with_resolved_name():
    mentions = A.person_mentions(A.person_identities(_load("facemetadata.json")))
    # Kevin Kilner has two appearance spans -> two mentions; the anonymous Person_2 track has spans too.
    kevin = [m for m in mentions if m.name == "Kevin Kilner"]
    assert [(m.start_ms, m.end_ms) for m in kevin] == [(30030, 30830), (31031, 31831)]
    assert all(isinstance(m, A.EntityMentionIn) for m in mentions)


def test_person_mentions_total_on_junk_and_drops_nameless_or_spanless():
    assert A.person_mentions(None) == []
    # a named person with no appearances, and an appearance-only row with no name → nothing
    assert A.person_mentions([{"name": "A", "appearances": []},
                              {"appearances": [{"start_ms": 1, "end_ms": 2}]}]) == []


def test_cast_mentions_fold_both_character_and_actor_names():
    mentions = A.cast_mentions(A.cast_characters(_load("persons.json")))
    names = {m.name for m in mentions}
    # Tomas Vega played Det. Rios → BOTH the character and the actor are folded.
    assert "Det. Rios" in names and "Tomas Vega" in names
    # Miriam's single span yields her actor name (no character card).
    miriam = [m for m in mentions if m.name == "Miriam Okafor"]
    assert [(m.start_ms, m.end_ms) for m in miriam] == [(12400, 31800)]


def test_segment_mentions_from_sequences_and_markers_are_humanised():
    mentions = A.segment_mentions(A.program_structure(_load("structure.json")))
    labels = {m.name for m in mentions}
    # Sequence settings become labels, humanised (underscore → space).
    assert "beach coast" in labels and "office" in labels
    # Marker kinds become labels: end_credits → "end credits".
    assert "end credits" in labels and "blackframes" in labels
    # A marker label spans the marker's own ms range (end_credits is late in the program).
    ends = [m for m in mentions if m.name == "end credits"]
    assert ends and all(m.start_ms < m.end_ms for m in ends)


def test_segment_mentions_total_on_junk():
    assert A.segment_mentions(None) == []
    assert A.segment_mentions({"sequences": [None], "markers": {"x": [None]}}) == []
