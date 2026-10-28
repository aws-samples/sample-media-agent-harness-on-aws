"""Finding a package's project directories (folder and ``.zip``) and the handler's run / no-op decision."""
import pytest

from preload.handler import PreloadConfigError, handler, locate_package
from preload.package import discover_projects, list_files, root_prefix
from preload_fixtures import EMAIL, INGEST, moto_env, zip_bytes

M = {"version": 2, "name": "P"}


@pytest.fixture
def env(monkeypatch):
    with moto_env(monkeypatch) as e:
        yield e


@pytest.mark.parametrize("root,prefix", [("preload", "preload/"), ("preload/", "preload/"), ("/a/b//", "a/b/"),
                                         ("", "")])
def test_root_prefix(root, prefix):
    assert root_prefix(root) == prefix


def test_discovers_every_dir_with_a_manifest_in_key_order(env):
    env.put_tree("preload", {
        "lab02/manifest.json": {**M, "name": "Two"},
        "lab01/manifest.json": {**M, "name": "One"},
        "lab01/assets/a.png": b"png",
        "notes/readme.txt": b"no manifest here",       # ignored
        "lab03/assets/manifest.json": M,              # a manifest NOT directly in the dir: ignored
        "README.md": b"root file",                      # not a dir
    })
    dirs = discover_projects(env.s3, INGEST, "preload")
    assert [(d.name, d.prefix) for d in dirs] == [("lab01", "preload/lab01/"), ("lab02", "preload/lab02/")]


def test_discovery_does_not_reach_a_sibling_prefix(env):
    env.put_tree("preload-other", {"lab01/manifest.json": M})
    assert discover_projects(env.s3, INGEST, "preload/") == []


def test_list_files_is_relative_to_the_project_dir(env):
    env.put_tree("preload/lab01", {"manifest.json": M, "assets/x/y.mp4": b"v"})
    assert sorted(list_files(env.s3, INGEST, "preload/lab01/")) == ["assets/x/y.mp4", "manifest.json"]


def test_no_project_dir_is_a_noop_even_without_an_owner(env):
    env.put_tree("preload", {"stray/readme.txt": b"x", "README.md": b"x"})
    assert locate_package(env.s3, INGEST, "preload/", "").action == "noop"
    assert locate_package(env.s3, INGEST, "does-not-exist/", EMAIL).action == "noop"


def test_a_package_without_an_owner_fails_loud(env):
    env.put_tree("preload", {"lab01/manifest.json": M})
    with pytest.raises(PreloadConfigError):
        locate_package(env.s3, INGEST, "preload/", "")


def test_a_folder_package_runs(env):
    env.put_tree("preload", {"lab01/manifest.json": M})
    d = locate_package(env.s3, INGEST, "preload/", EMAIL)
    assert d.action == "run" and d.package_root == "preload" and "lab01" in d.reason


def test_a_zip_with_one_project_dir_unpacks_and_runs(env):
    env.put("pkg/one.zip", zip_bytes({"lab01/manifest.json": M, "lab01/assets/a.png": "png"}))
    d = locate_package(env.s3, INGEST, "pkg/one.zip", EMAIL)
    assert d.action == "run" and d.package_root == "pkg/one-unpacked"
    assert [p.name for p in discover_projects(env.s3, INGEST, d.package_root)] == ["lab01"]
    assert env.keys("pkg/one-unpacked/") == ["pkg/one-unpacked/lab01/assets/a.png",
                                             "pkg/one-unpacked/lab01/manifest.json"]


def test_a_zip_with_two_project_dirs_finds_both(env):
    env.put("two.zip", zip_bytes({"README.md": "hi", "lab01/manifest.json": M,
                                  "lab02/manifest.json": {**M, "name": "Q"}, "__MACOSX/lab01/._x": "junk"}))
    d = locate_package(env.s3, INGEST, "two.zip", EMAIL)
    assert d.action == "run"
    assert [p.name for p in discover_projects(env.s3, INGEST, d.package_root)] == ["lab01", "lab02"]


def test_a_rezip_clears_files_dropped_since_the_last_unpack(env):
    env.put("pkg.zip", zip_bytes({"lab01/manifest.json": M, "lab01/assets/old.png": "png"}))
    locate_package(env.s3, INGEST, "pkg.zip", EMAIL)
    env.put("pkg.zip", zip_bytes({"lab01/manifest.json": M, "lab01/assets/new.png": "png"}))
    locate_package(env.s3, INGEST, "pkg.zip", EMAIL)
    assert env.keys("pkg-unpacked/") == ["pkg-unpacked/lab01/assets/new.png", "pkg-unpacked/lab01/manifest.json"]


def test_a_zip_without_project_dirs_is_a_noop(env):
    # The common mistake: zipping the package FOLDER, so the project dirs sit one level too deep.
    env.put("wrapped.zip", zip_bytes({"README.md": "hi", "pkg/lab01/x.txt": "no manifest at pkg/"}))
    d = locate_package(env.s3, INGEST, "wrapped.zip", EMAIL)
    assert d.action == "noop" and "manifest.json" in d.reason


def test_missing_zip_is_a_noop(env):
    assert locate_package(env.s3, INGEST, "absent.zip", "").action == "noop"


def test_handler_delete_is_a_noop():
    out = handler({"RequestType": "Delete", "ResourceProperties": {}}, None)
    assert out["status"] == "SUCCESS" and out["data"]["noop"] is True
