"""Stage skill-bundled scripts into the code-interpreter sandbox the moment a cell references one.

Skills ship deterministic helpers -- ``agent-olive/scripts/assemble_reel.py``, ``media-references/scripts/
estimate_cost.py``, ``video-processing/scripts/trim_plan.py``, ``content-moderation/scripts/build_report.py`` --
and their instructions say "run ``python scripts/<x>.py``". But a code-interpreter sandbox starts EMPTY: nothing
copied the files there, the sandbox role has no ``skills/*`` grant, and only ``crop_video`` embedded its own
script. So every such instruction either failed or made the model re-type the script (the reason three sg5
moderation reports came out in three formats, staging session a2d759c2).

This module closes the gap for every skill at once. For a code_interpreter action whose ``code`` /
``command`` names a skill script, it resolves the reference against the INSTALLED skills (bundled + S3-
downloaded), and returns the files to write. The caller writes them into the target sandbox before the model's
action runs, at three equivalent paths relative to the sandbox's working directory::

    scripts/<file>                 skills/<skill>/<scripts|assets>/<file>        <skill>/<scripts|assets>/<file>

Only files inside a skill's ``scripts/`` or ``assets/`` directory are ever staged (never ``..``, never another
path), sibling ``.py`` modules of a staged script come along (scripts import each other), and the total is capped.
Everything here is pure and never raises into the caller.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import re
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

STAGE_DIRS = ("scripts", "assets")
MAX_STAGED_BYTES = 512 * 1024
MAX_FILE_BYTES = 256 * 1024

# `[skills/][<skill>/](scripts|assets)/<rel path with an extension>` -- the rel path may contain subdirs.
_REF_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(?:skills/)?(?:(?P<skill>[A-Za-z0-9][A-Za-z0-9_.-]*)/)?"
    r"(?P<dir>scripts|assets)/(?P<rel>[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.[A-Za-z0-9]+)")


def skill_roots() -> list[Path]:
    """Directories that hold installed skills, most authoritative first (bundle, S3 downloads, dev checkout)."""
    roots: list[Path] = []
    try:
        import skills_manager  # noqa: PLC0415

        roots.append(Path(skills_manager.SKILLS_DIR))
    except Exception:  # noqa: BLE001 - tests / stripped runtimes
        pass
    here = Path(__file__).resolve().parent
    roots += [here / "skills", Path("/tmp/skills"), here.parent / "skills"]
    out: list[Path] = []
    for r in roots:
        try:
            r = r.resolve()
        except OSError:
            continue
        if r.is_dir() and r not in out:
            out.append(r)
    return out


def _norm(name: str) -> str:
    return name.lower().replace("-", "_")


def _skill_dirs(roots: Iterable[Path]) -> dict[str, Path]:
    """``{skill dir name: path}``; the first root wins for a name present in several."""
    found: dict[str, Path] = {}
    for root in roots:
        try:
            for d in sorted(root.iterdir()):
                if d.is_dir() and not d.name.startswith((".", "_")) and d.name not in found:
                    found[d.name] = d
        except OSError:
            continue
    return found


def find_references(text: str) -> list[tuple[Optional[str], str, str]]:
    """``[(skill or None, dir, rel)]`` for every skill-script reference in ``text`` (deduplicated, in order)."""
    if not isinstance(text, str) or ("scripts/" not in text and "assets/" not in text):
        return []
    out: list[tuple[Optional[str], str, str]] = []
    for m in _REF_RE.finditer(text):
        ref = (m.group("skill"), m.group("dir"), m.group("rel"))
        if ".." in ref[2].split("/") or ref in out:
            continue
        if ref[0] in ("skills", ".", "tmp", "opt"):  # `skills/scripts/x` is not a skill named "skills"
            ref = (None, ref[1], ref[2])
        out.append(ref)
    return out


def _inside(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except (ValueError, OSError):
        return False


def resolve(refs: list[tuple[Optional[str], str, str]], active_skills: Iterable[str] = (),
            roots: Optional[list[Path]] = None) -> list[tuple[str, str, str, Path]]:
    """``[(skill, dir, rel, file)]`` for the references that name an existing file in an installed skill.

    A reference with a skill name resolves in that skill only. A bare ``scripts/<rel>`` resolves in the turn's
    active skills first; failing that, in every skill that has it (each staged under its own ``skills/<skill>/``).
    """
    dirs = _skill_dirs(roots if roots is not None else skill_roots())
    by_norm = {_norm(k): k for k in dirs}
    active = [by_norm[_norm(a)] for a in active_skills if isinstance(a, str) and _norm(a) in by_norm]
    out: list[tuple[str, str, str, Path]] = []

    def _file(skill: str, sub: str, rel: str) -> Optional[Path]:
        base = dirs[skill] / sub
        p = base / rel
        return p if p.is_file() and _inside(p, base) else None

    for skill, sub, rel in refs:
        if skill is not None:
            key = by_norm.get(_norm(skill))
            hit = _file(key, sub, rel) if key else None
            if hit:
                out.append((key, sub, rel, hit))
            continue
        cands = [(s, _file(s, sub, rel)) for s in (active or [])]
        cands = [(s, p) for s, p in cands if p]
        if not cands:
            cands = [(s, p) for s in dirs for p in [_file(s, sub, rel)] if p]
        out += [(s, sub, rel, p) for s, p in cands]
    seen: set = set()
    uniq = []
    for item in out:
        if (item[0], item[1], item[2]) not in seen:
            seen.add((item[0], item[1], item[2]))
            uniq.append(item)
    return uniq


_BARE_RE = re.compile(r"(?<![A-Za-z0-9_./-])([A-Za-z0-9_][A-Za-z0-9_.-]*\.(?:py|sh))(?![A-Za-z0-9_])")


def bare_script_references(text: str, active_skills: Iterable[str] = (),
                           roots: Optional[list[Path]] = None) -> list[tuple[str, str, str, Path]]:
    """A script of an ACTIVE skill named WITHOUT its ``scripts/`` dir (``python assemble_reel.py``, as the skill's own
    text says it): ``[(skill, "scripts", name, file)]``. Only the turn's active skills are searched, so an unrelated
    ``x.py`` mention never stages another skill's file. Staging silently skipped these (staging session 39d5fb04 spent
    five minutes copying the script into the sandbox by hand)."""
    if not isinstance(text, str) or ".py" not in text and ".sh" not in text:
        return []
    names = {m.group(1) for m in _BARE_RE.finditer(text)}
    if not names:
        return []
    dirs = _skill_dirs(roots if roots is not None else skill_roots())
    by_norm = {_norm(k): k for k in dirs}
    out: list[tuple[str, str, str, Path]] = []
    for a in active_skills:
        key = by_norm.get(_norm(a)) if isinstance(a, str) else None
        base = dirs[key] / "scripts" if key else None
        if base is None or not base.is_dir():
            continue
        for name in sorted(names):
            p = base / name
            if p.is_file() and _inside(p, base):
                out.append((key, "scripts", name, p))
    return out


def files_to_stage(text: str, active_skills: Iterable[str] = (),
                   roots: Optional[list[Path]] = None) -> dict[str, bytes]:
    """``{sandbox-relative path: bytes}`` to write before ``text`` runs; ``{}`` when it references no skill file."""
    try:
        resolved = resolve(find_references(text), active_skills, roots)
        bare = bare_script_references(text, active_skills, roots)
        resolved += [r for r in bare if (r[0], r[1], r[2]) not in {(x[0], x[1], x[2]) for x in resolved}]
        if not resolved:
            return {}
        files: dict[str, bytes] = {}
        direct: dict[str, bytes] = {}
        bare_taken: set[str] = set()

        def _add(target: dict, skill: str, sub: str, rel: str, data: bytes) -> None:
            target[f"skills/{skill}/{sub}/{rel}"] = data
            target[f"{skill}/{sub}/{rel}"] = data
            if rel not in bare_taken:  # first (preferred) match owns the bare `scripts/<rel>` path
                bare_taken.add(rel)
                target[f"{sub}/{rel}"] = data

        bare_rels = {(b[0], b[2]) for b in bare}
        for skill, sub, rel, path in resolved:
            data = path.read_bytes()
            if len(data) > MAX_FILE_BYTES:
                logger.warning("skill_scripts: %s/%s/%s is %d bytes (> %d); not staged", skill, sub, rel,
                               len(data), MAX_FILE_BYTES)
                continue
            _add(direct, skill, sub, rel, data)
            if (skill, rel) in bare_rels:
                direct.setdefault(rel, data)  # `python assemble_reel.py` runs from the working directory
        files.update(direct)
        # Sibling modules: a staged script may `import` its neighbours (same skill, same dir, .py only).
        for skill, sub, rel, path in resolved:
            if not rel.endswith(".py"):
                continue
            for sib in sorted(path.parent.glob("*.py")):
                srel = str(Path(rel).parent / sib.name) if "/" in rel else sib.name
                if f"skills/{skill}/{sub}/{srel}" in files:
                    continue
                data = sib.read_bytes()
                if len(data) <= MAX_FILE_BYTES:
                    _add(files, skill, sub, srel, data)
        total = sum(len(v) for k, v in files.items() if k.startswith("skills/"))
        if total > MAX_STAGED_BYTES:
            logger.warning("skill_scripts: %d bytes with siblings exceeds %d; staging the referenced files only",
                           total, MAX_STAGED_BYTES)
            files = direct
            total = sum(len(v) for k, v in files.items() if k.startswith("skills/"))
            if total > MAX_STAGED_BYTES:
                logger.warning("skill_scripts: referenced files alone are %d bytes (> %d); nothing staged",
                               total, MAX_STAGED_BYTES)
                return {}
        return files
    except Exception as e:  # noqa: BLE001 - staging must never break a code_interpreter call
        logger.warning("skill_scripts: resolution skipped: %s", e)
        return {}


# Where a runtime's skills live. The AgentSkills plugin shows the model each skill's
# ``Location: /var/task/skills/<skill>/SKILL.md``, so a model sometimes runs
# ``python /var/task/skills/agent-olive/scripts/assemble_reel.py`` in the SANDBOX -- a different
# machine with no /var/task. The file IS staged, at the sandbox-relative ``skills/<skill>/...``.
_RUNTIME_SKILL_ROOTS = ("/var/task/skills/",)


def relativize_runtime_paths(text: str, files: dict[str, bytes], roots: Optional[list[Path]] = None) -> str:
    """``text`` with every absolute runtime path of a STAGED skill file rewritten to its staged relative path.

    Only a path whose relative form is in ``files`` is rewritten, so nothing else in the code changes.
    """
    if not isinstance(text, str) or not files:
        return text
    prefixes = list(_RUNTIME_SKILL_ROOTS)
    for r in roots if roots is not None else skill_roots():
        prefixes.append(str(r).rstrip("/") + "/")
    for prefix in sorted(set(prefixes), key=len, reverse=True):
        if prefix not in text:
            continue
        pat = re.compile(re.escape(prefix) + r"(?P<rel>[A-Za-z0-9][A-Za-z0-9_.-]*/(?:scripts|assets)/"
                         r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)")
        text = pat.sub(lambda m: f"skills/{m.group('rel')}" if f"skills/{m.group('rel')}" in files
                       else m.group(0), text)
    return text


def writer_code(files: dict[str, bytes]) -> str:
    """A self-contained Python program that writes ``files`` relative to the current working directory."""
    payload = base64.b64encode(
        repr({k: base64.b64encode(v).decode("ascii") for k, v in files.items()}).encode()).decode("ascii")
    return ("import base64 as _b, os as _o, ast as _a\n"
            f"for _p, _d in _a.literal_eval(_b.b64decode('{payload}').decode()).items():\n"
            "    _o.makedirs(_o.path.dirname(_p) or '.', exist_ok=True)\n"
            "    open(_p, 'wb').write(_b.b64decode(_d))\n"
            "    _o.chmod(_p, 0o755)\n")


def fingerprint(files: dict[str, bytes]) -> str:
    h = hashlib.sha256()
    for k in sorted(files):
        h.update(k.encode())
        h.update(hashlib.sha256(files[k]).digest())
    return h.hexdigest()


def action_text(action: dict) -> str:
    """The text of a code_interpreter action that can reference a script (``code`` / ``command``)."""
    if not isinstance(action, dict):
        return ""
    return "\n".join(str(action.get(k) or "") for k in ("code", "command"))
