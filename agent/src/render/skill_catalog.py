"""Render skill CATALOG — the single reader/parser for render-* skill declarations.

Extracted from ``render_agent`` so the declaration reader (frontmatter parse +
tool→skill lookup + the ``deliverable:`` output contract) can be reused WITHOUT
importing the render-agent runtime. Importing ``render_agent`` pulls in
``task_agent`` and therefore spins up a code-interpreter session, the job
registry and the gateway registry — far too heavy for the render-artifacts hook,
which needs the deliverable contract at BUFFER time (in the main/task runtime) to
decide whether a byproduct output should open its own render tab.

Keeping the parser here — and importing these same symbols back into
``render_agent`` — means there is ONE parser, not two that drift.

Nothing in this module has import-time side effects beyond creating a lazy S3
client (only when a skills bucket is configured), so it is safe to import from a
hot path.
"""

import logging
import os
from pathlib import Path
from typing import Optional

import boto3

logger = logging.getLogger(__name__)

# Read the same environment the render agent reads. This is an env lookup, not a
# second parser — the frontmatter parser itself lives once, below.
REGION = os.environ.get("AWS_REGION", "us-west-2")
S3_BUCKET = os.environ.get("SKILLS_S3_BUCKET", "")
_s3 = boto3.client("s3", region_name=REGION) if S3_BUCKET else None

# AgentCore's federated tool naming: "<target>___<verb>". Only the target segment
# identifies the producing job (matches `_FEDERATED_SEP` in hooks/actions.py).
_FEDERATED_SEP = "___"


def _resolve_render_skills_dir() -> Path:
    """Locate the bundled skills directory in either layout.

    The deployed package is flat — `agent/src/*.py` become top-level and
    `agent/skills/**` lands at `skills/` beside them — so at runtime the directory
    is `<parent>/skills`. In the repo, this module sits in `agent/src/`, so the
    same expression points at `agent/src/skills`, which is NOT what ships: the
    bundler only takes `.py`/`.txt` from packages that have an `__init__.py`.

    Checking both means a skill authored in `agent/skills/` is discovered locally and
    in the runtime, instead of passing every local test and shipping nothing.
    """
    here = Path(__file__).parent
    # This module now lives in the `render/` subpackage, one level deeper than
    # before in BOTH layouts: repo `agent/src/render/`, deployed `<app>/render/`.
    # So the skills dir is one `parent` further out than the historical
    # `<module-dir>/skills` + `<module-dir>/../skills`. Runtime still resolves
    # `here.parent/skills` (= `<app>/skills`, the deployed skills) FIRST via the
    # non-empty-wins rule; the extra `here.parent.parent/skills` restores repo
    # discovery of `agent/skills` (from `agent/src/render/`).
    candidates = (here / "skills", here.parent / "skills", here.parent.parent / "skills")
    # A non-empty directory wins: an empty leftover must not shadow the real one.
    for candidate in candidates:
        if candidate.is_dir() and any(candidate.iterdir()):
            return candidate
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


RENDER_SKILLS_DIR = _resolve_render_skills_dir()


def _parse_skill_frontmatter(md: str) -> dict:
    """Parse a render skill's frontmatter into a view DECLARATION.

    `handles` is a comma-separated list of MIME types and/or `.ext` tokens
    (Req 11.6). Returns lowercased, stripped tokens.

    Beyond `name`/`handles`/`template`, a skill may declare how to project its
    analysis JSON onto a template's props. That keeps the mapping as DATA rather
    than per-tool code: a new analysis tool ships a skill, not a renderer.

    Recognised declaration keys (all optional):

        detect:    dotted path that must exist for this skill to apply
                   (e.g. `shots` or `tracks`) — disambiguates same-MIME sidecars
        markers:   <listPath> time=<field> [end=<field>] [label=<field>]
                   [category=<field>]
        segments:  <listPath> start=<field> end=<field> [name=<field>]
                   [group=<field>] [units=ms]
        lanes:     <entityPath> name=<field> spans=<field> start=<field>
                   end=<field> [units=ms]
                   — one lane per entity, spans nested inside it
        boxes:     <listPath> box=<field> [time=<field>] [label=<field>]
        rows:      <listPath> cols=<f1>,<f2>,…
        thumbnails: <listPath> key=<field> label=<field>
                   — stored image per entity, shown as a gallery
        duration:  <field> holding an authoritative duration in seconds
        summary:   <f1>,<f2>,… scalar fields worth surfacing as metadata
        deliverable: <name-or-glob>,… the output FILENAMES this skill renders
                   (e.g. `shots.json`). Everything else the tool emits
                   (`seg*_win*`, `recurrence*`, `probe.json`, frame tarballs)
                   is scratch and is NOT rendered. This is the output contract
                   the render agent uses to pick the deliverable — and to skip
                   the generic-data fallback that turned scratch into tabs.
        subsumes:  <name-or-glob>,… sibling deliverables this skill's canonical
                   output SUPERSEDES. When this skill's `deliverable` is present
                   among the candidates, the subsumed siblings are dropped so
                   they never open their own (duplicate) tab. Declarative and
                   per-skill: e.g. face-recognition's facegroup.json (per-face
                   boxes + tracks + names) subsumes facemetadata.json /
                   face_clusters.json (flat/derived views of the same people).
                   Applied only within one render's candidate set, so it can
                   never collapse unrelated deliverables.

    Unknown keys are ignored so a newer skill stays loadable on an older agent.
    """
    meta: dict = {"name": "", "handles": set(), "template": "", "declaration": {}}
    if not md.startswith("---"):
        return meta
    parts = md.split("---", 2)
    if len(parts) < 3:
        return meta
    _DECL_KEYS = {"detect", "markers", "segments", "lanes", "boxes", "boxes_by", "rows",
                  "thumbnails", "duration", "summary", "deliverable", "subsumes",
                  # Which LANE SET the declaration's segments/lanes draw ("speakers", "people", "scenes"): the
                  # timeline shows each set as its newest view drew it (composeTimeline), so a re-run or an agent
                  # correction replaces that set and leaves every other lane alone.
                  "lane_set",
                  # The producing TOOL's federated target (e.g. "shot-segmentation").
                  # This is the AUTHORITATIVE view key: the tool that ran determines the
                  # view, deterministically. `detect:` content-scoring stays only as a
                  # fallback for outputs whose tool we don't recognise.
                  "tool"}
    for line in parts[1].strip().split("\n"):
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip().lower()
        val = val.strip()
        if key == "name":
            meta["name"] = val
        elif key == "template":
            meta["template"] = val
        elif key == "handles":
            meta["handles"] = {t.strip().lower() for t in val.split(",") if t.strip()}
        elif key in _DECL_KEYS and val:
            meta["declaration"][key] = val
    return meta


_render_skills_cache: Optional[list[dict]] = None


def reset_render_skill_cache() -> None:
    """Clear the per-process discovery cache (tests re-write skills then re-discover)."""
    global _render_skills_cache
    _render_skills_cache = None


def _discover_s3_render_skills(seen_names: set[str]) -> list[dict]:
    """Discover render-* skills from the S3 skills bucket (Req 11.5, 16.1).

    Lists ``{SKILLS_S3_PREFIX}render-*/`` folders, reads each SKILL.md, and
    parses handles/template. S3 (customer) skills are discovered BEFORE local
    built-ins so a same-named or same-MIME custom skill overrides the built-in
    (Req 11.3). Best-effort: any per-skill error is logged and skipped.
    """
    out: list[dict] = []
    if not _s3 or not S3_BUCKET:
        return out
    prefix = os.environ.get("SKILLS_S3_PREFIX", "skills/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    try:
        paginator = _s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix, Delimiter="/"):
            for cp in page.get("CommonPrefixes", []):
                folder = cp.get("Prefix", "")           # e.g. "skills/render-foo/"
                name = folder[len(prefix):].strip("/")
                if not name.startswith("render-"):
                    continue
                try:
                    obj = _s3.get_object(Bucket=S3_BUCKET, Key=f"{folder}SKILL.md")
                    md = obj["Body"].read().decode("utf-8")
                except Exception:
                    continue
                meta = _parse_skill_frontmatter(md)
                if not meta["name"]:
                    meta["name"] = name
                if meta["name"] in seen_names:
                    continue
                meta["dir"] = f"s3://{S3_BUCKET}/{folder}"
                meta["source"] = "s3"
                seen_names.add(meta["name"])
                out.append(meta)
                logger.info("Discovered S3 render skill '%s' handles=%s template=%s",
                            meta["name"], sorted(meta["handles"]), meta["template"])
    except Exception as e:
        logger.warning("S3 render-skill discovery failed (non-fatal): %s", e)
    return out


def _discover_render_skills() -> list[dict]:
    """Discover render-* skills from S3 (custom) then local disk (built-in).

    Cached per process. S3-first ordering means customer skills override
    built-ins for the same name/MIME (Req 11.3/11.5). The base SkillsManager
    scans the same sources but ignores the `handles`/`template` frontmatter
    this selection path needs, so we parse it here.
    """
    global _render_skills_cache
    if _render_skills_cache is not None:
        return _render_skills_cache
    skills: list[dict] = []
    seen_names: set[str] = set()

    # 1) S3 (customer / runtime-added) skills first — override built-ins.
    skills.extend(_discover_s3_render_skills(seen_names))

    # 2) Local built-in skills (bundled into the runtime). Uses the resolved
    # directory rather than recomputing it, so repo and runtime layouts agree.
    skills_dir = RENDER_SKILLS_DIR
    if skills_dir.exists():
        for d in sorted(skills_dir.iterdir()):
            if not (d.is_dir() and d.name.startswith("render-")):
                continue
            # Consolidated bundle: one render-media skill whose references/*.md each carry a
            # per-type mapping (detect/template/deliverable/projections). Parse each reference as
            # its own mapping so the deterministic fallback + correlation keep every render type,
            # while the agent loads the single bundle via progressive disclosure.
            refs_dir = d / "references"
            mapping_files = (sorted(refs_dir.glob("*.md")) if refs_dir.is_dir()
                             else ([d / "SKILL.md"] if (d / "SKILL.md").exists() else []))
            for skill_md in mapping_files:
                try:
                    meta = _parse_skill_frontmatter(skill_md.read_text())
                    if not meta["name"]:
                        meta["name"] = f"{d.name}:{skill_md.stem}"
                    if meta["name"] in seen_names:
                        continue
                    # A bundle index (render-media/SKILL.md) has no detect/template/projection —
                    # it's a router, not a mapping — so skip it in the deterministic catalog.
                    if refs_dir.is_dir() and skill_md.name == "SKILL.md":
                        continue
                    meta["dir"] = str(d)
                    meta["source"] = "local"
                    seen_names.add(meta["name"])
                    skills.append(meta)
                    logger.info("Discovered local render mapping '%s' handles=%s template=%s",
                                meta["name"], sorted(meta["handles"]), meta["template"])
                except Exception as e:
                    logger.warning("Failed to parse render mapping %s: %s", skill_md, e)
    _render_skills_cache = skills
    return skills


def select_render_skill_by_tool(tool_name: Optional[str]) -> Optional[dict]:
    """Return the render skill declared for the TOOL that produced the output.

    First principle: the view is known at dispatch. We chose ``shot-segmentation``,
    so the view is a shot timeline — no inference from the output's JSON shape, which
    is what made three identical shot-segmentation jobs pick three different views.

    ``tool_name`` may be the federated form ``<target>___<verb>``
    (e.g. ``shot-segmentation___get_job_status``); only the target segment carries the
    identity, so we match on that. A skill opts in by declaring ``tool:`` in its
    frontmatter. Returns None for an unknown/blank tool so the caller keeps its
    MIME-based choice (and is labelled generic) rather than being handed a
    confidently-wrong specialised view.

    Exact, first-match, no scoring: one tool → one view is a lookup, not a ranking.
    """
    if not tool_name:
        return None
    target = str(tool_name).partition(_FEDERATED_SEP)[0].strip().lower()
    if not target:
        return None
    for skill in _discover_render_skills():
        # A COMMA LIST, because one view legitimately serves several target spellings: the
        # stems tool ships as `audio-stem` in the deployed gateway but is referred to as
        # `audio-stem-separation` elsewhere in the codebase. A single exact string silently
        # matched neither — the failure mode of any hand-maintained name list, so accept the
        # known aliases rather than betting on one spelling.
        declared = str((skill.get("declaration") or {}).get("tool") or "")
        aliases = {a.strip().lower() for a in declared.split(",") if a.strip()}
        if target in aliases:
            return skill
    return None


def deliverables_for_tool(tool_name: Optional[str]) -> set[str]:
    """Return the lowercased ``deliverable:`` basenames/globs the tool's render skill declares.

    The skill's ``deliverable:`` is the authoritative output contract: it names the
    file(s) that are the actual result, so everything else the tool emits (a proxy
    ``segment_*.mp4``, ``recurrence*.json``, frame tarballs) is a byproduct. Callers use
    this to keep a byproduct from opening its own render tab beside the real deliverable.

    Empty set = unknown tool or no declared deliverable → the caller keeps its default
    behaviour (no exclusion), so this can only ever remove a KNOWN byproduct, never guess.
    """
    skill = select_render_skill_by_tool(tool_name)
    if not skill:
        return set()
    decl = skill.get("declaration") or {}
    return {d.strip().lower() for d in str(decl.get("deliverable") or "").split(",") if d.strip()}
