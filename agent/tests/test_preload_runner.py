"""End-to-end ``run_preload`` over moto: a package with several project directories, + idempotency."""
import pathlib

import pytest

from ingestion.import_inbox import import_folder, import_prefix
from preload.handler import locate_package
from preload.ids import project_id_for_name
from preload.runner import PreloadPackageError
from preload_fixtures import EMAIL, INGEST, OWNER_SUB, FakeIdp, moto_env, run, skill_md, zip_bytes

FX = pathlib.Path(__file__).parent / "fixtures" / "ingestion" / "evil_santa"
ROOT = "preload"
ONE, TWO = "Example · Video Understanding", "Example · Semantic Search"


def _package(env, root=ROOT):
    analysis = {k: FX.joinpath(f).read_bytes() for k, f in (
        ("shots.json", "shots.json"), ("transcript.json", "transcript.json"),
        ("frame_manifest.json", "frame_manifest.json"), ("shot_metadata.jsonl", "shot_metadata.jsonl"),
        ("analysis.json", "analysis.json"))}
    env.put_tree(root, {
        # lab01: the default `assets/**`, a nested folder, dotfiles, a skill with a reference.
        "lab01/manifest.json": {"version": 2, "name": ONE, "indexing": True},
        "lab01/assets/a.png": b"png-a",
        "lab01/assets/.DS_Store": b"junk",
        "lab01/assets/clips/day1/x.mp4": b"\x00mp4",
        "lab01/assets/clips/day1/._x.mp4": b"junk",
        "lab01/skills/SKILL.md": skill_md("lab-1-video-understanding"),
        "lab01/skills/references/guide.md": b"guide",
        "lab01/skills/.DS_Store": b"junk",
        # lab02: globs + an explicit preanalyzed entry whose analysis lives outside assets/.
        "lab02/manifest.json": {"version": 2, "name": TWO, "assets": [
            "assets/*.png",
            {"media": "assets/clip.mp4", "mode": "preanalyzed", "analysis": {
                "shot-list": "analysis/shots.json", "transcript": "analysis/transcript.json",
                "frames": "analysis/frame_manifest.json", "enrichment": "analysis/shot_metadata.jsonl",
                "analysis": "analysis/analysis.json"}},
        ]},
        "lab02/assets/b.png": b"png-b",
        "lab02/assets/skipped/c.png": b"not matched by assets/*.png",
        "lab02/assets/clip.mp4": b"\x00mp4",
        **{f"lab02/analysis/{name}": body for name, body in analysis.items()},
        "lab02/skills/SKILL.md": skill_md("lab-2-semantic-search"),
        # Ignored: no manifest.
        "scratch/assets/z.png": b"z",
        "README.md": b"hello",
    })


@pytest.fixture
def env(monkeypatch):
    with moto_env(monkeypatch) as e:
        yield e


def test_two_project_dirs_end_to_end(env):
    _package(env)
    result = run(env, ROOT)
    assert result.errors == []
    assert result.owner_created is True and result.owner_sub == OWNER_SUB
    assert [p["dir"] for p in result.projects] == ["lab01", "lab02"]
    p1, p2 = (project_id_for_name(ONE), project_id_for_name(TWO))
    assert [p["pid"] for p in result.projects] == [p1, p2]
    assert all(p["created"] for p in result.projects)

    # lab01: raw files land in the import inbox, under their folder; dotfiles never move.
    inbox1 = env.keys(import_prefix(p1))
    assert inbox1 == [f"{import_prefix(p1)}a.png", f"{import_prefix(p1)}clips/day1/x.mp4"]
    assert import_folder(p1, inbox1[0]) is None
    assert import_folder(p1, inbox1[1]) == "clips/day1"

    # lab02: only the top-level png is globbed; the preanalyzed clip is a queued asset; no analysis
    # JSON was placed as a separate raw document.
    assert env.keys(import_prefix(p2)) == [f"{import_prefix(p2)}b.png"]
    queued = env.store.assets_in(p2, ("queued",))
    assert len(queued) == 1 and queued[0]["source"] == "preload"
    modes = sorted(a["mode"] for a in result.projects[1]["assets"])
    assert modes == ["preanalyzed", "raw"]

    # Both projects are active and owned; indexing defaults on.
    for pid in (p1, p2):
        meta = env.store.get_meta(pid)
        assert meta["status"] == "active" and meta["ownerSub"] == OWNER_SUB and meta["indexing"] is True

    # One skill per project, named by its frontmatter, published privately.
    assert sorted(s["name"] for s in result.skills) == ["lab-1-video-understanding", "lab-2-semantic-search"]
    assert [p["skill"] for p in result.projects] == ["lab-1-video-understanding", "lab-2-semantic-search"]
    s1 = next(s for s in result.skills if s["project"] == "lab01")
    assert s1["files"] == 2 and s1["version"] == 1 and s1["created"] is True
    assert len(env.published()) == 2

    summary = result.summary()
    assert summary == {"ownerCreated": True, "projects": 2, "assets": 4, "skills": 2, "errors": []}


def test_rerun_is_idempotent(env):
    _package(env)
    idp = FakeIdp()
    run(env, ROOT, idp)
    before_inbox = env.keys("projects/")
    second = run(env, ROOT, idp)
    assert second.errors == []
    assert second.owner_created is False
    assert all(p["created"] is False for p in second.projects)
    assert all(a["created"] is False for p in second.projects for a in p["assets"])
    assert all(s["created"] is False and s["action"] == "unchanged" for s in second.skills)
    assert env.keys("projects/") == before_inbox
    assert len(env.published()) == 2
    assert len(env.store.assets_in(project_id_for_name(TWO), ("queued", "analyzing", "indexing", "indexed"))) == 1


def test_a_changed_skill_on_rerun_is_a_new_version_of_the_same_skill(env):
    _package(env)
    first = {s["name"]: s for s in run(env, ROOT).skills}
    env.put(f"{ROOT}/lab01/skills/SKILL.md", skill_md("lab-1-video-understanding", body="Changed."))
    env.delete(f"{ROOT}/lab01/skills/references/guide.md")
    second = {s["name"]: s for s in run(env, ROOT).skills}
    a1, a2 = first["lab-1-video-understanding"], second["lab-1-video-understanding"]
    assert a2["publishedId"] == a1["publishedId"] and a2["version"] == 2 and a2["action"] == "updated"
    assert second["lab-2-semantic-search"]["action"] == "unchanged"
    assert len(env.published()) == 3


def test_a_duplicate_skill_name_installs_the_first_and_reports_the_second(env):
    _package(env)
    env.put(f"{ROOT}/lab02/skills/SKILL.md", skill_md("LAB-1-VIDEO-UNDERSTANDING".lower(), body="dupe"))
    result = run(env, ROOT)
    assert [s["project"] for s in result.skills] == ["lab01"]
    assert len(result.errors) == 1
    assert "duplicate skill name" in result.errors[0] and "lab02" in result.errors[0]
    assert result.projects[1]["skill"] is None
    assert len(result.projects[1]["assets"]) == 2      # the project itself still loaded


def test_a_bad_manifest_fails_only_its_project(env):
    _package(env)
    env.put(f"{ROOT}/lab03/manifest.json", {"version": 1, "projects": []})
    env.put(f"{ROOT}/lab04/manifest.json", b"{not json")
    result = run(env, ROOT)
    assert [p["dir"] for p in result.projects] == ["lab01", "lab02"]
    assert len(result.errors) == 2
    assert result.errors[0].startswith("project lab03:") and "version" in result.errors[0]
    assert result.errors[1].startswith("project lab04:") and "JSON" in result.errors[1]


def test_two_dirs_with_the_same_project_name_load_once(env):
    _package(env)
    env.put(f"{ROOT}/lab03/manifest.json", {"version": 2, "name": ONE})
    result = run(env, ROOT)
    assert [p["dir"] for p in result.projects] == ["lab01", "lab02"]
    assert len(result.errors) == 1 and "already used by project lab01" in result.errors[0]


def test_an_invalid_skill_fails_only_itself(env):
    _package(env)
    env.put(f"{ROOT}/lab02/skills/notes.md", b"not under references/")
    result = run(env, ROOT)
    assert [s["project"] for s in result.skills] == ["lab01"]
    assert len(result.errors) == 1 and "lab-2-semantic-search" in result.errors[0]


def test_no_project_dir_raises_for_a_direct_run(env):
    env.put(f"{ROOT}/README.md", b"x")
    with pytest.raises(PreloadPackageError):
        run(env, ROOT)


def test_a_zip_package_runs_end_to_end(env):
    env.put("example.zip", zip_bytes({
        "README.md": "readme",
        "lab01/manifest.json": {"version": 2, "name": ONE},
        "lab01/assets/a.png": "png",
        "lab01/skills/SKILL.md": skill_md("lab-1-video-understanding"),
        "lab02/manifest.json": {"version": 2, "name": TWO},
        "lab02/assets/b.png": "png",
    }))
    decision = locate_package(env.s3, INGEST, "example.zip", EMAIL)
    result = run(env, decision.package_root)
    assert result.errors == []
    assert [p["dir"] for p in result.projects] == ["lab01", "lab02"]
    assert result.summary()["assets"] == 2 and result.summary()["skills"] == 1
