"""``ffmpeg`` and ``ffprobe`` on PATH in every code-interpreter sandbox — provisioned on first use.

The sandbox image has neither. Every cell that combined media had to fetch the harness's staged build itself, and
only the ``video-processing`` skill said how — so a plan step, which is handed no skills, ran without it (staging
c7d66001: the recast's assemble step found no ``ffprobe`` and made nothing). The harness already runs a program in
every sandbox before any model code (the scoped-credentials install, :mod:`sandbox_credentials`); this adds two
tiny launchers to it. A launcher downloads the staged build (``s3://<skills bucket>/tools/ffmpeg/``, read with the
scoped credentials) the first time either is called, then ``exec``\\ s the real binary — so a sandbox that never uses
ffmpeg pays nothing. The build lands where the bundled scripts' ``_ensure_ffmpeg_ffprobe`` looks
(``<tmp>/ffmpeg-static``), so the two share one download.
"""
from __future__ import annotations

import os

#: Printed by :func:`setup_code` once both launchers are in place.
READY_SENTINEL = "__SBX_FFMPEG_ON_PATH__"

# Runs under the KERNEL's interpreter (its shebang is written in the sandbox): that one has boto3, which a bare
# ``python3`` on PATH may not.
_LAUNCHER = r'''# Harness launcher: provisions the staged ffmpeg build on first use, then runs the real binary.
import fcntl, os, pathlib, platform, sys, tarfile, tempfile
_BUCKET = %(bucket)r
_REGION = %(region)r
name = os.path.basename(sys.argv[0])
root = pathlib.Path(tempfile.gettempdir()) / "ffmpeg-static"

def find():
    for c in sorted(root.rglob(name)):
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return None

real = find()
if not real:
    root.mkdir(parents=True, exist_ok=True)
    with open(root / ".provision.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        real = find()
        if not real:
            arch = "arm64" if platform.machine().lower() in ("aarch64", "arm64") else "amd64"
            archive = root / "ffmpeg-static.tar.xz"
            try:
                import boto3
                boto3.client("s3", region_name=_REGION or None).download_file(
                    _BUCKET, "tools/ffmpeg/ffmpeg-release-%%s-static.tar.xz" %% arch, str(archive))
                with tarfile.open(archive) as tar:
                    try:
                        tar.extractall(root, filter="data")
                    except TypeError:
                        tar.extractall(root)
            except Exception as exc:
                sys.exit("%%s: could not provision the harness ffmpeg build from s3://%%s/tools/ffmpeg/ (%%s)"
                         %% (name, _BUCKET, exc))
            finally:
                archive.unlink(missing_ok=True)
            for b in ("ffmpeg", "ffprobe"):
                for c in root.rglob(b):
                    if c.is_file():
                        c.chmod(0o755)
            real = find()
if not real:
    sys.exit("%%s: the harness ffmpeg build has no %%s" %% (name, name))
os.execv(real, [real] + sys.argv[1:])
'''


def setup_code(bucket: str | None = None, region: str | None = None) -> str:
    """Python, run in the sandbox kernel by the harness, that puts the ``ffmpeg`` / ``ffprobe`` launchers on PATH.

    They go in the first writable directory already on PATH (so ``executeCommand`` shells find them too), else
    ``~/.local/bin``, which is then put at the front of the kernel's PATH. A sandbox that already has both on PATH
    is left alone. ``""`` without a bucket to provision from (unit tests, local runs)."""
    bucket = (bucket if bucket is not None else os.environ.get("SKILLS_S3_BUCKET") or "").strip()
    if not bucket:
        return ""
    region = (region if region is not None else os.environ.get("AWS_REGION") or "").strip()
    launcher = _LAUNCHER % {"bucket": bucket, "region": region}
    return (
        "def _sbx_ffmpeg():\n"
        "    import os, shutil, sys\n"
        "    if shutil.which('ffmpeg') and shutil.which('ffprobe'):\n"
        "        return\n"
        "    dirs = [d for d in os.environ.get('PATH', '').split(os.pathsep) if d and os.path.isdir(d)"
        " and os.access(d, os.W_OK)]\n"
        "    home_bin = os.path.expanduser('~/.local/bin')\n"
        "    target = dirs[0] if dirs else home_bin\n"
        "    os.makedirs(target, exist_ok=True)\n"
        "    for n in ('ffmpeg', 'ffprobe'):\n"
        "        p = os.path.join(target, n)\n"
        f"        with open(p, 'w') as f: f.write('#!' + sys.executable + '\\n' + {launcher!r})\n"
        "        os.chmod(p, 0o755)\n"
        "    if target not in os.environ.get('PATH', '').split(os.pathsep):\n"
        "        os.environ['PATH'] = target + os.pathsep + os.environ.get('PATH', '')\n"
        "_sbx_ffmpeg(); del _sbx_ffmpeg\n"
        f"print({READY_SENTINEL!r})\n"
    )
