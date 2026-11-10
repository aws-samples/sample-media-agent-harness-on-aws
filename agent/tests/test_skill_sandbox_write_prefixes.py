"""Every sandbox write a shipped skill tells the model to make is one ``SandboxScopedS3Role`` allows.

asset-lineage Phase 3 (b). Where ``sandboxScopedCredentials`` is on (staging), the harness injects the
scoped role's credentials as the sandbox's ``[default]`` profile, and from then on boto3 never consults
the sandbox's own execution role (the 2026-10-09 probe, design §9.2). So the scoped role's two write
prefixes are the ONLY places a code_interpreter cell can write, and a skill that says "upload to
``artifacts/<name>.pdf``" or "under ``artifacts/<job-uuid>/output/``" breaks on deploy, not after the
ambient grants are removed. The pdf and content_moderation skills shipped exactly that.

The role's prefixes are read from the CDK source, so a change on either side fails here.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
SKILLS = ROOT / "agent" / "skills"
INFRA = ROOT / "infra" / "lib" / "agent-infra-stack.ts"

# A placeholder that names THIS session (the role's `${aws:PrincipalTag/session_id}`).
_SESSION = r"<(?:session|this[- ]session|session[_-]id|sid|this session)>"
_PROJECT = r"<(?:project|project[_-]id|pid)>"
ALLOWED = [re.compile(rf"^artifacts/{_SESSION}/"), re.compile(rf"^projects/{_PROJECT}/sessions/{_SESSION}/")]

_WRITE_VERB = re.compile(r"upload|put_object|put |write|under prefix|under the prefix|Key=|key\s*=", re.I)
_NEGATED = re.compile(r"\bnever\b|\bnot\b|\bdenied\b|AccessDenied|instead of|either `", re.I)
_DENIED = re.compile(r"\bdenied\b|AccessDenied", re.I)
_READ = re.compile(r"\bread\b|download|source media", re.I)
_LITERAL = re.compile(r"[`\"'](?:s3://[^/`\"']+/)?((?:artifacts|projects)/[^`\"'\s]*)")


def _role_write_prefixes() -> list[str]:
    m = re.search(r"const writePrefixes = \[(.*?)\];", INFRA.read_text(), re.S)
    assert m, "SandboxScopedS3Role writePrefixes not found in agent-infra-stack.ts"
    return re.findall(r"`([^`]*)`", m.group(1))


def test_role_write_prefixes_are_the_two_this_test_assumes():
    assert _role_write_prefixes() == [
        "projects/${pidTag}/sessions/${sidTag}/*",
        "artifacts/${sidTag}/*",
    ], "update ALLOWED above to match the role's new write prefixes"


def _skill_docs():
    return sorted(p for p in SKILLS.rglob("*.md"))


def _violations():
    out = []
    for path in _skill_docs():
        lines = path.read_text().splitlines()
        for n, line in enumerate(lines, 1):
            # Instructions wrap: the verb may sit up to two lines above the key ("under prefix\n `…`").
            window = " ".join(lines[max(0, n - 3):n])
            # A prohibition ("never … / … is denied") or a READ instruction around the key is not a write.
            nxt = lines[n] if n < len(lines) else ""
            if (not _WRITE_VERB.search(window) or _NEGATED.search(line) or _DENIED.search(nxt)
                    or _READ.search(window)):
                continue
            for lit in _LITERAL.findall(line):
                if not any(a.match(lit) for a in ALLOWED):
                    out.append(f"{path.relative_to(ROOT)}:{n}: {lit}")
    return out


def test_no_skill_writes_outside_the_scoped_role():
    bad = _violations()
    assert not bad, "skill text tells the sandbox to write a key SandboxScopedS3Role denies:\n" + "\n".join(bad)


def test_detector_catches_the_shipped_regressions(tmp_path, monkeypatch):
    """The two recipes that broke must be caught if they come back."""
    global SKILLS
    d = tmp_path / "skills" / "x"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        'key = "artifacts/report.pdf"  # plain key\n'
        "   `boto3 put_object` both files under prefix\n"
        "   `artifacts/<job-uuid>/output/` with the correct ContentType\n"
        'key = f"{prefix}report.pdf"\n'
        "upload to `artifacts/<session>/output/x.pdf`\n"
    )
    monkeypatch.setattr(pathlib.Path, "relative_to", lambda self, other: self)
    old = SKILLS
    SKILLS = tmp_path / "skills"
    try:
        bad = _violations()
    finally:
        SKILLS = old
    assert len(bad) == 2 and "artifacts/report.pdf" in bad[0] and "<job-uuid>" in bad[1], bad


def test_the_two_recipes_name_the_instructed_output_prefix():
    pdf = (SKILLS / "pdf" / "SKILL.md").read_text()
    cm = (SKILLS / "content-moderation" / "SKILL.md").read_text()
    assert "SESSION OUTPUT LOCATION" in pdf and "S3 CONTEXT" in pdf
    assert "artifacts/<job-uuid>" not in cm and "OUTPUT prefix" in cm


# ---------------------------------------------------------------------------------------- B2: no fallback role
def test_the_sandbox_execution_role_holds_no_artifact_or_project_grant():
    """B2: the code interpreter's OWN role (shared by every actor, and reachable by model code that deletes
    the injected credentials file) grants nothing but the staged ffmpeg build. Every artifact/project
    access goes through ``SandboxScopedS3Role``."""
    src = INFRA.read_text()
    grants = re.findall(r"skillsBucket\.grant\w+\(\s*codeInterpreter\s*(?:,\s*([^)]*))?\)", src)
    assert grants == ['"tools/ffmpeg/*"'], f"unexpected code interpreter grants: {grants}"
    assert not re.search(r"\b(?:uploadsBucket|labArtifactsBucket|skillsBucket)\.grant\w*\(\s*codeInterpreter\s*\)", src)


def test_scoped_credentials_are_on_unless_explicitly_disabled():
    src = INFRA.read_text()
    assert "props.envConfig?.sandboxScopedCredentials !== false" in src, (
        "sandboxScopedCredentials must default ON: without it a cell has no S3 access at all")
