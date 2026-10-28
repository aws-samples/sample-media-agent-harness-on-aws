"""Per-project ``manifest.json`` (version 2) parsing: defaults, rejections, globs + explicit entries."""
import pytest

from preload.manifest import (DEFAULT_ASSETS, PREANALYZED, RAW, AssetSpec, PreloadManifestError,
                              ProjectManifest, parse_manifest)


def test_minimal_manifest_takes_the_defaults():
    m = parse_manifest({"version": 2, "name": "Workshop Lab 1"})
    assert isinstance(m, ProjectManifest)
    assert m.name == "Workshop Lab 1"
    assert m.indexing is True
    assert m.assets == DEFAULT_ASSETS == ("assets/**",)


def test_indexing_false_is_kept():
    assert parse_manifest({"version": 2, "name": "P", "indexing": False}).indexing is False


@pytest.mark.parametrize("version", [1, 3, "2", None, True, 2.5])
def test_version_other_than_2_is_rejected(version):
    raw = {"name": "P"} if version is None else {"version": version, "name": "P"}
    with pytest.raises(PreloadManifestError, match="version"):
        parse_manifest(raw)


@pytest.mark.parametrize("raw", [{"version": 2}, {"version": 2, "name": ""}, {"version": 2, "name": "  "},
                                 {"version": 2, "name": 7}])
def test_name_is_required(raw):
    with pytest.raises(PreloadManifestError, match="name"):
        parse_manifest(raw)


def test_not_an_object_is_rejected():
    with pytest.raises(PreloadManifestError):
        parse_manifest(["version", 2])


def test_mixed_globs_and_explicit_entries_keep_their_order():
    m = parse_manifest({"version": 2, "name": "P", "assets": [
        "assets/*.mp4",
        {"media": "assets/x.mp4", "mode": "preanalyzed", "analysis": {"shot-list": "analysis/x/shots.json"}},
        "assets/clips/**",
        {"media": "assets/still.png"},
    ]})
    assert m.assets[0] == "assets/*.mp4"
    assert m.assets[2] == "assets/clips/**"
    pre = m.assets[1]
    assert isinstance(pre, AssetSpec) and pre.mode == PREANALYZED and pre.kind == "video"
    assert pre.analysis == {"shot-list": "analysis/x/shots.json"}
    still = m.assets[3]
    assert isinstance(still, AssetSpec) and still.mode == RAW and still.kind == "image"


@pytest.mark.parametrize("media,kind", [("a.mp4", "video"), ("a.MOV", "video"), ("a.wav", "audio"),
                                        ("a.jpg", "image"), ("a.pdf", "document"), ("a.bin", "document")])
def test_kind_is_inferred_from_the_extension(media, kind):
    m = parse_manifest({"version": 2, "name": "P", "assets": [{"media": f"assets/{media}"}]})
    assert m.assets[0].kind == kind


def test_explicit_kind_wins_and_unknown_kind_is_rejected():
    m = parse_manifest({"version": 2, "name": "P", "assets": [{"media": "assets/a.bin", "kind": "video"}]})
    assert m.assets[0].kind == "video"
    with pytest.raises(PreloadManifestError, match="kind"):
        parse_manifest({"version": 2, "name": "P", "assets": [{"media": "a.mp4", "kind": "hologram"}]})


def test_mode_defaults_from_analysis():
    raw = parse_manifest({"version": 2, "name": "P", "assets": [{"media": "a.mp4"}]})
    assert raw.assets[0].mode == RAW
    pre = parse_manifest({"version": 2, "name": "P", "assets": [
        {"media": "a.mp4", "analysis": {"shot-list": "s.json"}}]})
    assert pre.assets[0].mode == PREANALYZED


def test_preanalyzed_without_analysis_is_rejected():
    with pytest.raises(PreloadManifestError, match="analysis"):
        parse_manifest({"version": 2, "name": "P", "assets": [{"media": "a.mp4", "mode": "preanalyzed"}]})


@pytest.mark.parametrize("bad", ["/abs/a.mp4", "../other/a.mp4", "assets/../../a.mp4", "..", ""])
def test_paths_must_stay_inside_the_project_dir(bad):
    with pytest.raises(PreloadManifestError):
        parse_manifest({"version": 2, "name": "P", "assets": [bad]})
    with pytest.raises(PreloadManifestError):
        parse_manifest({"version": 2, "name": "P", "assets": [{"media": bad or "", "kind": "video"}]})


def test_assets_must_be_a_list_of_strings_or_objects():
    with pytest.raises(PreloadManifestError):
        parse_manifest({"version": 2, "name": "P", "assets": "assets/**"})
    with pytest.raises(PreloadManifestError):
        parse_manifest({"version": 2, "name": "P", "assets": [42]})


def test_an_empty_assets_list_places_nothing():
    assert parse_manifest({"version": 2, "name": "P", "assets": []}).assets == ()
