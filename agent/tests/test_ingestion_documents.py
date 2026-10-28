"""``ingestion.adapters`` / ``documents`` / ``records`` / ``chunking`` (Req 5, 6.3–6.6, 6.10, 8.11, 12.8).

The golden test builds from REAL staging M2C outputs (``fixtures/ingestion/evil_santa``: the spike's 52 s trailer)
and pins the result the validated prototype produced for the same outputs: 15 shots, 15 scenes, 11 utterances,
1 topic, nothing omitted, and that record searched correctly in the staging spike.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ingestion import adapters as A  # noqa: E402
from ingestion import chunking, contract, records  # noqa: E402
from ingestion import documents as D  # noqa: E402
from ingestion.adapters import SceneIn, ShotIn, TopicIn, TurnIn  # noqa: E402

FX = pathlib.Path(__file__).parent / "fixtures" / "ingestion" / "evil_santa"
ASSET = "video:08765c1a3635"
# Recomputed when the fixture's bucket/account were replaced with placeholders (the document
# frame keys embed the bucket name); the builder code was unchanged.
GOLDEN_DIGEST = "8006290935f9bf00092c89028118ed73019dea1e2985b16c5960e183893f314c"


def _fixture():
    shots = json.loads((FX / "shots.json").read_text())
    transcript = json.loads((FX / "transcript.json").read_text())
    md = [json.loads(ln) for ln in (FX / "shot_metadata.jsonl").read_text().split("\n") if ln.strip()]
    analysis = json.loads((FX / "analysis.json").read_text())
    manifest = json.loads((FX / "frame_manifest.json").read_text())
    uri = f"s3://m2c-proxy/{manifest['prefix']}/frame_manifest.json"
    return shots, transcript, md, analysis, manifest, uri


def build_fixture():
    shots, transcript, md, analysis, manifest, uri = _fixture()
    frames = A.pick_frames(shots["shots"], manifest, uri)
    dur = A.duration_ms(shots["actual_duration_sec"])
    return D.build_video(ASSET, A.shots_input(shots, frames), A.enrich_input(shots, md), A.diar_input(transcript),
                         A.topics_input(analysis), dur), dur


# ------------------------------------------------------------------------------------------- golden
def test_golden_real_m2c_outputs():
    res, dur = build_fixture()
    assert dur == 52083
    assert res.counts() == {"shot": 15, "scene": 15, "utterance": 11, "topic": 1}
    assert (res.omitted, res.truncated_scenes) == (0, 0)
    contract.check(res.documents, ASSET, dur)
    shot = res.documents[0]
    assert shot["fields"]["frame_s3_key"].endswith("/frames/frame_0000000.jpg")
    assert shot["fields"]["scene_parent_id"] == "scene:0:5125"
    first_utt = next(d for d in res.documents if d["doc_type"] == "utterance")
    assert first_utt["content"][0]["value"].startswith("Speaker 1: T 'was the night before Christmas")
    assert first_utt["fields"]["topic_parent_id"] == "topic:2743:46061"


def test_golden_matches_staging_spike_digest_with_real_bucket():
    """Pins determinism (Req 5.20) across code changes: a different digest means the documents changed."""
    shots, transcript, md, analysis, manifest, _ = _fixture()
    uri = f"s3://m2cv6-0e0000000002-100000000002-us-west-2-proxy/{manifest['prefix']}/frame_manifest.json"
    frames = A.pick_frames(shots["shots"], manifest, uri)
    res = D.build_video(ASSET, A.shots_input(shots, frames), A.enrich_input(shots, md), A.diar_input(transcript),
                        A.topics_input(analysis), A.duration_ms(shots["actual_duration_sec"]))
    rec = records.serialize(res.documents)
    assert rec.isascii() and not rec.endswith("\n")
    assert records.digest(rec) == GOLDEN_DIGEST


# ------------------------------------------------------------------------------------------ adapters
@pytest.mark.parametrize("v,ms", [(1.0005, 1001), (0.0005, 1), (2.743, 2743), ("5.125", 5125), (0, 0),
                                  (0.0004999, 0), (None, None), ("x", None), (True, None), (float("nan"), None)])
def test_sec_to_ms_rounds_half_up_on_the_decimal(v, ms):
    assert A.sec_to_ms(v) == ms


@pytest.mark.parametrize("v,ms", [("00:00:02.743", 2743), ("01:02:03.0005", 3723001), ("0:00:46.061", 46061),
                                  ("bad", None), ("00:61:00", None), (3.5, 3500)])
def test_hms_to_ms(v, ms):
    assert A.hms_to_ms(v) == ms


@pytest.mark.parametrize("v,d", [(52.083333, 52083), (0, None), (-1, None), (None, None), ("", None)])
def test_duration(v, d):
    assert A.duration_ms(v) == d


def test_pick_frames_nearest_selected_inside_span_ties_smaller():
    shots = [{"shot_id": 0, "start_frame": 0, "end_frame": 10, "selected_frames": [5]},
             {"shot_id": 1, "start_frame": 11, "end_frame": 20, "selected_frames": []},  # target 15
             {"shot_id": 2, "start_frame": 21, "end_frame": 29, "selected_frames": [25]}]  # nothing inside
    man = {"prefix": "run/frames", "keys": ["frame_0000004.jpg", "frame_0000006.jpg", "frame_0000014.jpg",
                                            "frame_0000016.jpg", "frame_0000030.jpg", "notaframe.txt"]}
    got = A.pick_frames(shots, man, "s3://b/run/frames/frame_manifest.json")
    assert got == {0: "s3://b/run/frames/frame_0000004.jpg", 1: "s3://b/run/frames/frame_0000014.jpg"}


def test_has_speech():
    assert not A.has_speech(None)
    assert not A.has_speech({"sentences": [{"text": "  "}]})
    assert A.has_speech({"sentences": [{"text": " "}, {"text": "hi"}]})


# ----------------------------------------------------------------------------------- the five fixes
def _scene(s, e, desc="d", **kw):
    return SceneIn(s, e, description=desc, **kw)


def test_fix1_duplicate_scene_keeps_first_and_counts():
    res = D.build_video("video:x", [ShotIn(0, 10, "s3://b/k.jpg")],
                        [_scene(0, 10, "first"), _scene(0, 10, "second")], [], [], 100)
    scenes = [d for d in res.documents if d["doc_type"] == "scene"]
    assert [d["content"][0]["value"] for d in scenes] == ["first"]
    assert res.omitted == 1
    assert res.documents[0]["fields"]["scene_parent_id"] == "scene:0:10"


def test_fix1_shot_links_only_a_surviving_scene():
    res = D.build_video("video:x", [ShotIn(0, 10, "s3://b/k.jpg")], [_scene(0, 10, "  ")], [], [], 100)
    assert "scene_parent_id" not in res.documents[0]["fields"]
    assert res.omitted == 1


def test_fix2_speakers_numbered_by_first_non_empty_turn_before_span_checks():
    turns = [TurnIn(0, 5, "   ", "B"),       # empty: does not number B
             TurnIn(200, 300, "out of span", "C"),  # dropped by span, but numbers C first
             TurnIn(10, 20, "hello", "A"), TurnIn(30, 40, "there", "C"), TurnIn(50, 60, "x", "B")]
    res = D.build_video("video:x", [], [], turns, [], 100)
    texts = [d["content"][0]["value"] for d in res.documents]
    assert texts == ["Speaker 2: hello", "Speaker 1: there", "Speaker 3: x"]
    assert res.omitted == 2


def test_fix3_image_scene_text_from_parsed_object_and_entities():
    desc = {"description": "A red bike.", "objects": ["bike", 3, "helmet"], "setting": "street",
            "on_screen_text": ["SALE", "50%"], "brands": "Acme", "sentiment": 7,
            "entities": ["Acme, Inc", "acme  inc", "Bob"]}
    res = D.build_image("image:1", "s3://ingest/projects/p/image:1/rendition.jpg", desc)
    shot, scene = res.documents
    assert shot == {"asset_id": "image:1", "doc_type": "shot", "start_ms": 0, "end_ms": 1,
                    "content": [{"kind": "image", "field": "vector",
                                 "value": "s3://ingest/projects/p/image:1/rendition.jpg"}],
                    "fields": {"frame_s3_key": "projects/p/image:1/rendition.jpg", "scene_parent_id": "scene:0:1"}}
    assert scene["content"][0]["value"] == ("A red bike.\nObjects: bike, helmet\nSetting: street\n"
                                            "On-screen text: SALE, 50%\nBrands: Acme")
    assert scene["content"][2] == {"kind": "text", "field": "named_entities", "value": "Acme Inc, Bob"}
    contract.check(res.documents, "image:1", 1)


def test_fix4_truncation_counted_and_whole_lines():
    long = "x" * 7990
    res = D.build_video("video:x", [], [_scene(0, 10, long, setting="y" * 50), _scene(10, 20, "z" * 9000)],
                        [], [], 100)
    a, b = [d["content"][0]["value"] for d in res.documents]
    assert a == long and len(b) == 8000
    assert res.truncated_scenes == 2


def test_fix5_record_no_trailing_newline_and_blank_line_rejected():
    rec = records.serialize([{"a": 1}, {"b": 2}])
    assert rec == '{"a":1}\n{"b":2}'
    with pytest.raises(ValueError):
        records.parse('{"a":1}\n\n{"b":2}')
    with pytest.raises(ValueError):
        records.parse('{"a":1}\n')


# ------------------------------------------------------------------------------------------- rules
def test_scene_text_order_and_blank_lines():
    text, cut = D.scene_text("  desc ", ["a", " ", "b"], "", "  ", ["X"], "calm")
    assert (text, cut) == ("desc\nObjects: a, b\nBrands: X\nSentiment: calm", False)


def test_entities_normalized():
    assert D.clean_entities([" A,  B ", "a b", "", "  ", "C", 5]) == "A B, C"


# ----------------------------------------------- entity-metadata fold into scene named_entities (overlap)
def _scene_entities(res):
    """(start_ms, end_ms) -> the scene doc's named_entities string ("" when absent)."""
    out = {}
    for d in res.documents:
        if d["doc_type"] == "scene":
            out[(d["start_ms"], d["end_ms"])] = next(
                (c["value"] for c in d["content"] if c["field"] == "named_entities"), "")
    return out


def _M(name, s, e):
    return A.EntityMentionIn(name, s, e)


def test_entity_fold_appends_only_names_overlapping_the_scene_span():
    scenes = [_scene(0, 10), _scene(10, 20)]
    res = D.build_video("video:x", [], scenes, [], [], 100,
                        people=[_M("Kevin Kilner", 5, 8)],          # overlaps [0,10) only
                        cast=[_M("Det. Rios", 12, 15), _M("Tomas Vega", 12, 15)],  # [10,20) only
                        segments=[_M("office", 0, 20)])             # overlaps both
    ents = _scene_entities(res)
    assert ents[(0, 10)] == "Kevin Kilner, office"
    assert ents[(10, 20)] == "Det. Rios, Tomas Vega, office"


def test_entity_fold_touching_spans_do_not_overlap():
    # [0,10) and a mention at [10,20): zero overlap (< 1 ms), the same rule _best_overlap uses.
    res = D.build_video("video:x", [], [_scene(0, 10)], [], [], 100, people=[_M("Edge", 10, 20)])
    assert _scene_entities(res)[(0, 10)] == ""


def test_entity_fold_preserves_enrichment_entities_and_dedupes_case_insensitively():
    scenes = [_scene(0, 10, entities=("Acme", "Bob"))]
    res = D.build_video("video:x", [], scenes, [], [], 100,
                        people=[_M("acme", 1, 9)],          # case-insensitive duplicate of "Acme"
                        cast=[_M("Carol", 1, 9)])
    # existing enrichment entities kept first (first casing wins), new name appended, dup dropped.
    assert _scene_entities(res)[(0, 10)] == "Acme, Bob, Carol"


def test_entity_fold_off_is_byte_identical_to_no_entity_args():
    scenes = [_scene(0, 10, entities=("Acme",)), _scene(10, 20)]
    shots = [ShotIn(0, 10, "s3://b/k.jpg")]
    base = D.build_video("video:x", shots, scenes, [], [], 100)
    # 1. empty entity lists == omitting them entirely
    empty = D.build_video("video:x", shots, scenes, [], [], 100, people=[], cast=[], segments=[])
    assert empty.documents == base.documents
    # 2. mentions that overlap NO scene leave the documents byte-identical too
    none_overlap = D.build_video("video:x", shots, scenes, [], [], 100,
                                 people=[_M("Nobody", 500, 600)])
    assert none_overlap.documents == base.documents


def test_entity_fold_enriched_docs_pass_the_contract():
    scenes = [_scene(0, 10, entities=("Acme",)), _scene(10, 20)]
    res = D.build_video("video:x", [ShotIn(0, 10, "s3://b/k.jpg")], scenes, [], [], 100,
                        people=[_M("Kevin Kilner", 1, 9)], cast=[_M("Det. Rios", 11, 19)],
                        segments=[_M("end credits", 0, 20)])
    contract.check(res.documents, "video:x", 100)  # raises on any violation
    assert "Kevin Kilner" in _scene_entities(res)[(0, 10)]


def test_utterance_parents_greatest_overlap_ties_earlier_start():
    scenes = [_scene(0, 10), _scene(10, 20)]
    topics = [TopicIn(0, 15, "t1"), TopicIn(5, 20, "t2")]
    res = D.build_video("video:x", [], scenes, [TurnIn(5, 15, "hi", None), TurnIn(30, 40, "far", None)],
                        topics, 100)
    utts = [d for d in res.documents if d["doc_type"] == "utterance"]
    assert utts[0]["content"][0]["value"] == "hi"  # no speaker label → bare text
    assert utts[0]["fields"] == {"scene_parent_id": "scene:0:10", "topic_parent_id": "topic:0:15"}
    assert "fields" not in utts[1]


def test_omissions_spans_frames_and_text():
    res = D.build_video("video:x",
                        [ShotIn(0, 10, ""), ShotIn(10, 10, "s3://b/k.jpg"), ShotIn(-1, 5, "s3://b/k.jpg"),
                         ShotIn(90, 101, "s3://b/k.jpg"), ShotIn(None, 5, "s3://b/k.jpg"), ShotIn(0, 5, "s3://b")],
                        [], [], [TopicIn(0, 5, " ")], 100)
    assert res.documents == () and res.omitted == 7  # six shots and the blank topic


def test_no_speech_still_builds_visual_docs():
    res = D.build_video("video:x", [ShotIn(0, 10, "s3://b/k.jpg")], [_scene(0, 10)], [], [], 100)
    assert res.counts() == {"shot": 1, "scene": 1, "utterance": 0, "topic": 0}


def test_audio_has_no_scene_parent():
    res = D.build_audio("audio:x", [TurnIn(0, 10, "hi", "S")], [TopicIn(0, 10, "talk", ("Ann",))], 10)
    assert res.counts() == {"shot": 0, "scene": 0, "utterance": 1, "topic": 1}
    assert res.documents[0]["fields"] == {"topic_parent_id": "topic:0:10"}
    contract.check(res.documents, "audio:x", 10)


# --------------------------------------------------------------------------------------- documents (PDF)
def _chunk(page, ci, text, heading=""):
    return {"page": page, "chunk_index": ci, "heading": heading, "text": text}


def test_build_document_scene_only_with_span_page_encoding():
    chunks = [_chunk(1, 0, "page one a"), _chunk(1, 1, "page one b"), _chunk(2, 0, "page two")]
    res = D.build_document("document:x", chunks)
    assert res.counts() == {"shot": 0, "scene": 0, "utterance": 0, "topic": 0} | {"scene": 3}
    assert [(d["start_ms"], d["end_ms"]) for d in res.documents] == [(0, 1), (1, 2), (10_000, 10_001)]
    for ch, doc in zip(chunks, res.documents):
        assert D.page_of_span(doc["start_ms"]) == ch["page"]
    # scene docs carry exactly text + vector, no named_entities (v1 does not extract entities)
    assert all([c["field"] for c in d["content"]] == ["text", "vector"] for d in res.documents)
    dur = 2 * D.PAGE_STRIDE_MS
    contract.check(res.documents, "document:x", dur)


def test_build_document_spans_are_unique_across_many_chunks_on_one_page():
    chunks = [_chunk(1, i, f"chunk {i}") for i in range(50)]
    res = D.build_document("document:x", chunks)
    spans = [(d["start_ms"], d["end_ms"]) for d in res.documents]
    assert len(spans) == len(set(spans)) == 50
    assert res.omitted == 0


def test_build_document_prefixes_heading_but_not_when_text_already_starts_with_it():
    chunks = [_chunk(1, 0, "Title\nbody of the first chunk", heading="Title"),
              _chunk(1, 1, "later body on the page", heading="Title")]
    res = D.build_document("document:x", chunks)
    first, second = (d["content"][0]["value"] for d in res.documents)
    assert first == "Title\nbody of the first chunk"            # no duplicate heading
    assert second == "Title\nlater body on the page"            # heading prefixed for page context


def test_build_document_drops_blank_and_malformed_chunks_and_counts_them():
    chunks = [_chunk(1, 0, "   "), _chunk(1, 1, "real text"), {"page": 2}, _chunk(0, 0, "bad page")]
    res = D.build_document("document:x", chunks)
    assert [d["content"][0]["value"] for d in res.documents] == ["real text"]
    assert res.omitted == 3


def test_build_document_empty_is_empty_result():
    assert D.build_document("document:x", []) == D.BuildResult((), 0, 0)


def test_page_of_span_round_trips():
    for page in (1, 2, 7, 100):
        for ci in (0, 1, 9999):
            assert D.page_of_span((page - 1) * D.PAGE_STRIDE_MS + ci) == page


def test_build_order_and_keys():
    res = D.build_video("video:x", [ShotIn(5, 9, "s3://b/2.jpg"), ShotIn(0, 5, "s3://b/1.jpg")],
                        [_scene(5, 9), _scene(0, 5)], [TurnIn(1, 2, "a", None)], [TopicIn(0, 9, "t")], 10)
    assert [(d["doc_type"], d["start_ms"]) for d in res.documents] == [
        ("shot", 0), ("shot", 5), ("scene", 0), ("scene", 5), ("utterance", 1), ("topic", 0)]
    for d in res.documents:
        assert set(d) <= {"asset_id", "doc_type", "start_ms", "end_ms", "content", "fields"}


# --------------------------------------------------------------------------------------- chunking
def _docs(n, types=("shot",)):
    return [{"asset_id": "video:x", "doc_type": types[i % len(types)], "start_ms": i, "end_ms": i + 1,
             "content": []} for i in range(n)]


def test_chunk_sizes():
    assert [len(c) for c in chunking.chunks(_docs(250), False)] == [100, 100, 50]
    assert [len(c) for c in chunking.chunks(_docs(100), False)] == [100]
    assert chunking.chunk_count(201) == 3


def test_refresh_modes():
    h0 = chunking.History()
    assert chunking.refresh_mode(h0, "d1") is False                                     # Req 6.4
    assert chunking.refresh_mode(chunking.History(True, "d1"), "d1") is False           # Req 6.5
    assert chunking.refresh_mode(chunking.History(True, "d1"), "d2") is True            # Req 6.6
    assert chunking.refresh_mode(chunking.History(True, None), "d2") is True            # none completed


def test_refresh_only_in_first_chunk_containing_scope():
    docs = [{"doc_type": "shot", "n": i} for i in range(150)] + [{"doc_type": "scene", "n": i} for i in range(60)]
    c1, c2, c3 = chunking.chunks(docs, True)
    assert all(d["refresh"] for d in c1)
    assert [d["refresh"] for d in c2] == [False] * 50 + [True] * 50
    assert all(not d["refresh"] for d in c3)


def test_stale_scopes():
    h = chunking.History(True, "d", frozenset({"shot", "scene", "utterance"}))
    assert chunking.stale_scopes(h, frozenset({"shot", "scene"})) == {"utterance"}


# ------------------------------------------------------------------------------------- properties
_ms = st.integers(min_value=-5, max_value=1100)
_text = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=30)
_label = st.one_of(st.none(), st.sampled_from(["A", "B", "C"]))


@st.composite
def _inputs(draw):
    shots = draw(st.lists(st.builds(ShotIn, _ms, _ms, st.sampled_from(["", "s3://b/f/1.jpg", "s3://b/f/2.jpg"])),
                          max_size=6))
    scenes = draw(st.lists(st.builds(SceneIn, _ms, _ms, _text, st.tuples(_text), _text, _text, st.tuples(_text),
                                     _text, st.tuples(_text, _text)), max_size=6))
    turns = draw(st.lists(st.builds(TurnIn, _ms, _ms, _text, _label), max_size=6))
    topics = draw(st.lists(st.builds(TopicIn, _ms, _ms, _text, st.tuples(_text)), max_size=4))
    return shots, scenes, turns, topics


@settings(max_examples=300, deadline=None)
@given(_inputs())
def test_property_validity_determinism_roundtrip_chunking(inp):
    shots, scenes, turns, topics = inp
    res = D.build_video("video:x", shots, scenes, turns, topics, 1000)
    again = D.build_video("video:x", shots, scenes, turns, topics, 1000)
    assert res == again                                                    # Req 5.20
    contract.check(res.documents, "video:x", 1000)                         # Req 5.13
    n_in = len(shots) + len(scenes) + len(turns) + len(topics)
    assert len(res.documents) + res.omitted == n_in                       # every input accounted for
    keys = [(d["doc_type"], d["start_ms"], d["end_ms"]) for d in res.documents]
    assert len(keys) == len(set(keys))                                    # Req 5.15
    assert keys == sorted(keys, key=lambda k: (D._ORDER[k[0]], k[1], k[2]))  # Req 5.19
    rec = records.serialize(res.documents)
    assert rec.isascii() and records.parse(rec) == list(res.documents)    # Req 5.21/5.22
    for refresh in (False, True):
        flat = [{k: v for k, v in d.items() if k != "refresh"} for c in chunking.chunks(list(res.documents), refresh)
                for d in c]
        assert flat == list(res.documents)                                # Req 6.10


@given(st.lists(st.text(alphabet="a\u0085\u2028\u2029\n\\\"é😀", max_size=10), max_size=5))
def test_property_record_roundtrip_separators(texts):
    docs = [{"asset_id": "video:x", "doc_type": "scene", "start_ms": i, "end_ms": i + 1,
             "content": [{"kind": "text", "field": "text", "value": t}]} for i, t in enumerate(texts)]
    rec = records.serialize(docs)
    assert rec.isascii()
    assert records.parse(rec) == docs
