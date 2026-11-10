"""Render skills must live where the deployment bundler actually ships them.

This nearly shipped silently. `RENDER_SKILLS_DIR` was `Path(render_agent.py).parent /
"skills"`, which in the repo means `agent/src/skills` — but the CDK bundler
(infra/lib/agent-infra-stack.ts) takes only `.py`/`.txt` from directories under
`agent/src/` that contain `__init__.py`, and reads skill resources from `agent/skills`
instead, landing them at `skills/` beside the flattened modules.

So a SKILL.md authored under `agent/src/skills/` is discovered by every local test and
is absent from the deployed runtime: all the local evidence passes and the feature does
nothing in staging.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "agent" / "src"
sys.path.insert(0, str(SRC))

SHIPPED_SKILLS_DIR = ROOT / "agent" / "skills"
STACK = ROOT / "infra" / "lib" / "agent-infra-stack.ts"


def test_render_skills_live_in_the_shipped_directory():
    bundle = SHIPPED_SKILLS_DIR / "render-media"
    assert bundle.is_dir(), "render-media bundle is not in agent/skills — it would not deploy"
    refs = {p.stem for p in (bundle / "references").glob("*.md")}
    for required in ("shot-segmentation", "speaker-diarization", "face-people", "face-recognition"):
        assert required in refs, f"{required} render mapping missing from render-media/references"


def test_no_render_skills_hide_under_agent_src():
    stray = SRC / "skills"
    if stray.is_dir():
        found = [p.name for p in stray.iterdir() if p.is_dir() and p.name.startswith("render-")]
        assert not found, (
            f"{found} are under agent/src/skills, which the bundler does not ship; "
            "move them to agent/skills"
        )


def test_the_bundler_still_reads_the_directory_we_rely_on():
    """If this path changes in CDK, the assumption above is void."""
    ts = STACK.read_text()
    assert 'path.join(__dirname, "../../agent/skills")' in ts
    assert 'readDirRecursive(skillsDir, "skills")' in ts, \
        "skills must land at the runtime prefix `skills/`"


def test_bundler_would_not_ship_markdown_from_agent_src():
    """Documents the constraint that makes agent/src/skills the wrong home."""
    ts = STACK.read_text()
    walker = re.search(r"const bundleAgentPackage[\s\S]*?\n    \};", ts)
    assert walker, "package walker not found"
    body = walker.group(0)
    assert '.endsWith(".py")' in body and '.endsWith(".txt")' in body
    assert '.md' not in body, "if the walker gains .md support, revisit this test"


def test_discovery_finds_every_shipped_render_skill():
    import render_agent as ra
    ra.reset_render_skill_cache()
    names = {s["name"] for s in ra._discover_render_skills()}
    for required in ("render-shot-segmentation", "render-speaker-diarization",
                     "render-face-people", "render-face-recognition"):
        assert required in names, f"{required} not discovered from {ra.RENDER_SKILLS_DIR}"
    # the pre-existing built-ins must still be found
    assert {"render-audio", "render-video", "render-document"} <= names


def test_declarations_survive_the_move():
    import render_agent as ra
    ra.reset_render_skill_cache()
    by = {s["name"]: s for s in ra._discover_render_skills()}
    assert by["render-shot-segmentation"]["declaration"]["markers"].startswith("shots[]")
    assert "units=ms" in by["render-face-people"]["declaration"]["lanes"]
    assert by["render-speaker-diarization"]["declaration"]["segments"].startswith("sentences[]")


def test_every_builtin_skill_dir_matches_its_frontmatter_name():
    """The catalog row is keyed by the DIRECTORY name, the runtime by the frontmatter ``name``.

    ``content_moderation/`` declared ``name: content-moderation``, so the built-in Skills panel
    listed it twice (the catalog row, plus the runtime's row tagged "Other"). agentskills.io
    also requires the two to match.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "skills"
    mismatched = []
    for d in sorted(p for p in root.iterdir() if p.is_dir() and (p / "SKILL.md").exists()):
        m = re.search(r"^name:\s*\"?([^\"\n]+)\"?\s*$", (d / "SKILL.md").read_text(encoding="utf-8"), re.M)
        if not m or m.group(1).strip() != d.name:
            mismatched.append((d.name, m.group(1).strip() if m else None))
    assert not mismatched, mismatched
