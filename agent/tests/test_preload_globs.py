"""The path-aware glob helper and the expansion of a manifest's ``assets`` into planned placements."""
import pytest

from preload.globs import expand_assets, folder_for, glob_match, glob_to_regex
from preload.manifest import PREANALYZED, RAW, AssetSpec


@pytest.mark.parametrize("pattern,path,hit", [
    # `*` stays inside one segment.
    ("assets/*.mp4", "assets/a.mp4", True),
    ("assets/*.mp4", "assets/x/a.mp4", False),
    ("assets/*", "assets/a.png", True),
    ("assets/*", "assets/x/a.png", False),
    ("*.mp4", "a.mp4", True),
    ("*.mp4", "assets/a.mp4", False),
    # `?` is exactly one non-slash character.
    ("assets/a?.png", "assets/a1.png", True),
    ("assets/a?.png", "assets/a.png", False),
    ("assets/a?.png", "assets/a12.png", False),
    ("assets/a?b", "assets/a/b", False),
    # `**` spans any number of segments, including zero.
    ("assets/**", "assets/a.png", True),
    ("assets/**", "assets/x/y/b.mp4", True),
    ("assets/**", "other/a.png", False),
    ("assets/**", "assetsX/a.png", False),
    ("assets/**/x.mp4", "assets/x.mp4", True),
    ("assets/**/x.mp4", "assets/a/b/x.mp4", True),
    ("assets/**/x.mp4", "assets/a/b/y.mp4", False),
    ("**/*.png", "a.png", True),
    ("**/*.png", "assets/deep/a.png", True),
    ("**", "anything/at/all.bin", True),
    # A `**` that is not a whole segment is a single-segment `*`.
    ("assets/a**b", "assets/axxb", True),
    ("assets/a**b", "assets/a/x/b", False),
    # Literal characters are literal (no regex leak).
    ("assets/a.mp4", "assets/aXmp4", False),
    ("assets/(1)+[2].png", "assets/(1)+[2].png", True),
    # Whole-path match only.
    ("assets/a", "assets/abc", False),
])
def test_glob_semantics(pattern, path, hit):
    assert glob_match(pattern, path) is hit


def test_glob_to_regex_is_a_full_match():
    assert glob_to_regex("assets/*.png").fullmatch("assets/a.png")
    assert not glob_to_regex("assets/*.png").fullmatch("xassets/a.png")


@pytest.mark.parametrize("rel,folder", [
    ("assets/a.png", None),
    ("assets/clips/x.mp4", "clips"),
    ("assets/clips/day1/x.mp4", "clips/day1"),
    ("media/x.mp4", "media"),
    ("x.mp4", None),
])
def test_folder_for(rel, folder):
    assert folder_for(rel) == folder


FILES = [
    "manifest.json",
    "assets/a.png",
    "assets/.DS_Store",
    "assets/clips/",                 # a folder marker
    "assets/clips/day1/x.mp4",
    "assets/clips/day1/._x.mp4",     # an AppleDouble dotfile
    "assets/notes.pdf",
    "skills/SKILL.md",
]


def _by_media(planned):
    return {p.spec.media: p for p in planned}


def test_default_glob_expands_every_asset_with_its_folder_and_skips_dotfiles():
    planned, unmatched = expand_assets(("assets/**",), FILES)
    got = _by_media(planned)
    assert set(got) == {"assets/a.png", "assets/clips/day1/x.mp4", "assets/notes.pdf"}
    assert got["assets/a.png"].folder is None
    assert got["assets/clips/day1/x.mp4"].folder == "clips/day1"
    assert {m: p.spec.kind for m, p in got.items()} == {
        "assets/a.png": "image", "assets/clips/day1/x.mp4": "video", "assets/notes.pdf": "document"}
    assert all(p.spec.mode == RAW for p in planned)
    assert unmatched == []


def test_a_file_matched_by_several_patterns_is_placed_once():
    planned, _ = expand_assets(("assets/**", "assets/*.png", "**/*.mp4"), FILES)
    medias = [p.spec.media for p in planned]
    assert len(medias) == len(set(medias)) == 3


def test_an_explicit_entry_wins_over_a_glob_and_its_analysis_is_not_globbed():
    pre = AssetSpec(media="assets/clips/day1/x.mp4", kind="video", mode=PREANALYZED,
                    analysis={"shot-list": "assets/clips/day1/x.shots.json"})
    files = FILES + ["assets/clips/day1/x.shots.json"]
    planned, _ = expand_assets(("assets/**", pre), files)
    got = _by_media(planned)
    assert got["assets/clips/day1/x.mp4"].spec.mode == PREANALYZED
    assert got["assets/clips/day1/x.mp4"].folder == "clips/day1"
    assert "assets/clips/day1/x.shots.json" not in got
    assert len(planned) == 3


def test_a_duplicate_explicit_entry_is_placed_once():
    e = AssetSpec(media="assets/a.png", kind="image", mode=RAW)
    planned, _ = expand_assets((e, e), FILES)
    assert [p.spec.media for p in planned] == ["assets/a.png"]


def test_the_manifest_itself_is_never_placed_and_unmatched_patterns_are_reported():
    planned, unmatched = expand_assets(("**", "assets/*.mov"), FILES)
    assert "manifest.json" not in _by_media(planned)
    assert "skills/SKILL.md" in _by_media(planned)  # `**` is the author's explicit choice
    assert unmatched == ["assets/*.mov"]
