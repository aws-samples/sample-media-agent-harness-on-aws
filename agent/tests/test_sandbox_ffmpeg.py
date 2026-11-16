"""The ffmpeg launchers the harness puts on every sandbox's PATH (``sandbox_ffmpeg``), run for real: the setup program
in a fresh interpreter, then ``ffmpeg`` / ``ffprobe`` by bare name — with a stand-in ``boto3`` whose download hands
over a local tarball shaped like the staged build. No network, no media binaries."""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tarfile
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import sandbox_ffmpeg  # noqa: E402

_FAKE_BOTO3 = '''
import os, shutil
CALLS = os.environ["FAKE_S3_LOG"]
class _C:
    def download_file(self, bucket, key, dst):
        with open(CALLS, "a") as f:
            f.write(f"{bucket}/{key}\\n")
        shutil.copy(os.environ["FAKE_TARBALL"], dst)
def client(service, region_name=None):
    assert service == "s3"
    return _C()
'''


def _tarball(path: Path) -> None:
    with tarfile.open(path, "w:xz") as tar:
        for name in ("ffmpeg", "ffprobe"):
            body = f"#!/bin/sh\necho real-{name} \"$@\"\n".encode()
            info = tarfile.TarInfo(f"ffmpeg-7.0.2-arm64-static/{name}")
            info.size, info.mode = len(body), 0o644  # not executable in the archive: the launcher must fix that
            tar.addfile(info, io.BytesIO(body))


@pytest.fixture
def sandbox(tmp_path):
    (tmp_path / "boto3").mkdir()
    (tmp_path / "boto3" / "__init__.py").write_text(_FAKE_BOTO3)
    (tmp_path / "bin").mkdir()
    (tmp_path / "home").mkdir()
    (tmp_path / "tmp").mkdir()
    _tarball(tmp_path / "build.tar.xz")
    env = {
        "HOME": str(tmp_path / "home"), "TMPDIR": str(tmp_path / "tmp"),
        "PATH": f"{tmp_path / 'bin'}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "PYTHONPATH": str(tmp_path), "FAKE_TARBALL": str(tmp_path / "build.tar.xz"),
        "FAKE_S3_LOG": str(tmp_path / "s3.log"),
    }
    return tmp_path, env


def _run(code: str, env: dict) -> str:
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_ffmpeg_and_ffprobe_resolve_by_name_and_provision_once(sandbox):
    root, env = sandbox
    code = sandbox_ffmpeg.setup_code("skills-bkt", "us-west-2") + textwrap.dedent("""
        import shutil, subprocess
        print(subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True).stdout.strip())
        print(subprocess.run(["ffprobe", "-i", "x.mp4"], capture_output=True, text=True).stdout.strip())
        print(subprocess.run("ffmpeg -h", shell=True, capture_output=True, text=True).stdout.strip())
    """)
    out = _run(code, env).splitlines()
    assert out == [sandbox_ffmpeg.READY_SENTINEL, "real-ffmpeg -version", "real-ffprobe -i x.mp4", "real-ffmpeg -h"]
    # the first writable PATH dir holds the launchers, so a plain shell finds them too
    assert (root / "bin" / "ffmpeg").exists() and (root / "bin" / "ffprobe").exists()
    # one download, of the staged build for this machine's arch, shared by both names and every later call
    calls = (root / "s3.log").read_text().splitlines()
    assert len(calls) == 1 and calls[0].startswith("skills-bkt/tools/ffmpeg/ffmpeg-release-")
    assert calls[0].endswith("-static.tar.xz")
    # where the bundled scripts' _ensure_ffmpeg_ffprobe looks, so they reuse it
    assert list((root / "tmp" / "ffmpeg-static").rglob("ffprobe"))


def test_without_a_writable_path_dir_it_uses_home_bin_and_the_kernel_path(sandbox):
    root, env = sandbox
    env = {**env, "PATH": str(root / "no-such-dir")}  # nothing on PATH is writable, even for root (CI)
    code = sandbox_ffmpeg.setup_code("skills-bkt", "") + textwrap.dedent("""
        import subprocess
        print(subprocess.run(["ffprobe", "-v"], capture_output=True, text=True).stdout.strip())
    """)
    assert _run(code, env).splitlines()[-1] == "real-ffprobe -v"
    assert (root / "home" / ".local" / "bin" / "ffprobe").exists()


def test_a_sandbox_that_already_has_both_is_left_alone(sandbox):
    root, env = sandbox
    for n in ("ffmpeg", "ffprobe"):
        p = root / "bin" / n
        p.write_text("#!/bin/sh\necho system\n")
        p.chmod(0o755)
    _run(sandbox_ffmpeg.setup_code("skills-bkt", ""), env)
    assert (root / "bin" / "ffmpeg").read_text() == "#!/bin/sh\necho system\n"


def test_a_failed_download_says_where_it_looked(sandbox):
    root, env = sandbox
    env = {**env, "FAKE_TARBALL": str(root / "missing.tar.xz")}
    code = sandbox_ffmpeg.setup_code("skills-bkt", "") + textwrap.dedent("""
        import subprocess
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
        print(r.returncode, r.stderr.strip())
    """)
    last = _run(code, env).splitlines()[-1]
    assert last.startswith("1 ffmpeg: could not provision the harness ffmpeg build from s3://skills-bkt/tools/ffmpeg/")


def test_no_bucket_no_setup(monkeypatch):
    monkeypatch.delenv("SKILLS_S3_BUCKET", raising=False)
    assert sandbox_ffmpeg.setup_code() == ""


def test_install_runs_setup_after_the_credentials_and_survives_its_failure(monkeypatch):
    import sandbox_credentials as sc

    creds = {"AccessKeyId": "AKIAEXAMPLE", "SecretAccessKey": "s", "SessionToken": "t", "Expiration": "2099"}
    monkeypatch.setattr(sc, "credentials_for", lambda pid, sid: creds)
    sent = {}

    def invoke(action, body):
        sent["code"] = body["code"]
        return {"stream": [{"result": {"structuredContent": {"stdout": sc.INSTALLED_SENTINEL}}}]}

    sc.install(invoke, "p1", "s1", setup="raise RuntimeError('boom')\nprint('never')")
    code = sent["code"]
    assert code.index(sc.INSTALLED_SENTINEL) < code.index("boom")
    # the combined program is valid Python, and a raising setup is contained
    ns: dict = {}
    exec(compile(code.replace("_o.path.expanduser('~/.aws')", repr(str(Path(os.environ.get("TMPDIR", "/tmp")) / f"aws-{os.getpid()}"))), "x", "exec"), ns)  # noqa: S102
