"""Orchestrate one preload package end to end (design "Shape: a deploy-time custom resource").

``run_preload`` is pure orchestration over injected clients (``s3``, ``store``, ``idp``), so it is testable
with moto / fakes and carries no CloudFormation concerns (that is :mod:`preload.handler`). It:

1. finds the project directories under the package root (:func:`preload.package.discover_projects`);
2. resolves ``ownerEmail`` → Cognito ``sub`` (creating the user if absent);
3. per project directory, in key order: reads + validates its ``manifest.json``, seeds the four project
   rows (idempotent), expands ``assets`` (globs + explicit entries) and places each file (``raw`` → the
   import inbox under its folder, ``preanalyzed`` → a registered queued asset with seeded steps), then
   installs ``skills/SKILL.md`` when present (see :mod:`preload.skills`).

A project whose manifest is invalid, an asset, or a skill that fails is recorded in ``errors`` and does NOT
abort the rest (design: "fail an asset cleanly on mismatch"). A re-deploy of the same package is a no-op:
every write is idempotent, and an unchanged skill is recognised by its content digest.
"""
from __future__ import annotations

import asyncio
import json
import logging
import posixpath
from dataclasses import dataclass, field
from typing import Any, Optional

from . import assets as A
from . import skills as S
from .globs import expand_assets
from .ids import project_id_for_name
from .manifest import AssetSpec, PreloadManifestError, ProjectManifest, parse_manifest
from .owner import resolve_owner
from .package import MANIFEST_NAME, ProjectDir, discover_projects, list_files, read_bytes
from .projects import seed_project

log = logging.getLogger("preload.runner")

SKILLS_DIR = "skills"
SKILL_MD_REL = f"{SKILLS_DIR}/{S.SKILL_MD}"
# A fixed, deploy-stable createdAt (the project rows are idempotent on PK).
SEED_AT = "2026-10-27T00:00:00Z"


class PreloadPackageError(RuntimeError):
    """The package root holds no project directory (nothing to preload)."""


@dataclass
class PreloadResult:
    owner_sub: str
    owner_created: bool
    projects: list[dict] = field(default_factory=list)
    skills: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "ownerCreated": self.owner_created,
            "projects": len(self.projects),
            "assets": sum(len(p.get("assets", [])) for p in self.projects),
            "skills": len(self.skills),
            "errors": self.errors,
        }


def _resolve(project_prefix: str, rel: str) -> str:
    """A project-relative manifest path as a bucket key under the project directory."""
    return posixpath.normpath(f"{project_prefix.rstrip('/')}/{rel.lstrip('/')}")


def _abs_asset(project_prefix: str, spec: AssetSpec) -> AssetSpec:
    return AssetSpec(media=_resolve(project_prefix, spec.media), kind=spec.kind, mode=spec.mode,
                     analysis={k: _resolve(project_prefix, v) for k, v in spec.analysis.items()})


def read_project_manifest(s3: Any, bucket: str, pdir: ProjectDir) -> ProjectManifest:
    """Read + validate ``<project dir>/manifest.json`` from the Ingest bucket."""
    body = read_bytes(s3, bucket, f"{pdir.prefix}{MANIFEST_NAME}")
    try:
        raw = json.loads(body)
    except ValueError as exc:
        raise PreloadManifestError(f"{pdir.name}/{MANIFEST_NAME} is not valid JSON: {exc}") from exc
    return parse_manifest(raw, where=f"{pdir.name}/{MANIFEST_NAME}")


def _skill_files(s3: Any, bucket: str, pdir: ProjectDir, files: list[str]) -> tuple[str, dict[str, bytes]]:
    """``(SKILL.md text, {path relative to skills/: bytes})``. Dotfiles (``.DS_Store``) are skipped."""
    skill_md = read_bytes(s3, bucket, f"{pdir.prefix}{SKILL_MD_REL}").decode("utf-8")
    supporting: dict[str, bytes] = {}
    for rel in files:
        if not rel.startswith(SKILLS_DIR + "/") or rel.endswith("/") or rel == SKILL_MD_REL:
            continue
        if posixpath.basename(rel).startswith("."):
            continue
        supporting[rel[len(SKILLS_DIR) + 1:]] = read_bytes(s3, bucket, f"{pdir.prefix}{rel}")
    return skill_md, supporting


def run_preload(
    *, s3: Any, store: Any, idp: Any, bucket: str, package_root: str, user_pool_id: str, owner_email: str,
    skill_index: Any, artifacts_bucket: str,
) -> PreloadResult:
    """Execute the whole preload for one package. See the module docstring for the steps.

    ``skill_index`` is a :class:`lab.asset_index.AssetIndex` over the Lab asset-index table and
    ``artifacts_bucket`` the Lab artifacts bucket; together they drive the skill publish (Part 3). An
    :class:`lab.s3_user_files_tool.S3UserFilesTool` is built per owner (it binds the actor at
    construction, and the owner's ``sub`` is known only after :func:`resolve_owner`).
    """
    dirs = discover_projects(s3, bucket, package_root)
    if not dirs:
        raise PreloadPackageError(
            f"no project directory (a folder holding {MANIFEST_NAME}) under s3://{bucket}/{package_root}")
    owner = resolve_owner(idp, user_pool_id, owner_email)
    result = PreloadResult(owner_sub=owner.sub, owner_created=owner.created)
    ctx = _RunContext(s3=s3, store=store, bucket=bucket, owner=owner, skill_index=skill_index,
                      artifacts_bucket=artifacts_bucket, result=result)
    for pdir in dirs:
        ctx.do_project(pdir)
    return result


@dataclass
class _RunContext:
    s3: Any
    store: Any
    bucket: str
    owner: Any
    skill_index: Any
    artifacts_bucket: str
    result: PreloadResult
    _skill_s3: Any = None
    _pids: dict = field(default_factory=dict)          # pid -> project dir that claimed it
    _skill_names: dict = field(default_factory=dict)   # casefold name -> project dir that installed it

    def do_project(self, pdir: ProjectDir) -> None:
        result = self.result
        try:
            manifest = read_project_manifest(self.s3, self.bucket, pdir)
        except Exception as exc:  # noqa: BLE001 — a bad manifest fails only its own project
            log.warning("preload: project %s skipped: %s", pdir.name, exc)
            result.errors.append(f"project {pdir.name}: {exc}")
            return
        pid = project_id_for_name(manifest.name)
        if pid in self._pids:
            result.errors.append(f"project {pdir.name}: name {manifest.name!r} is already used by project "
                                 f"{self._pids[pid]}; skipped")
            return
        self._pids[pid] = pdir.name

        seed = seed_project(self.store.client, self.store.name, pid=pid, name=manifest.name,
                            owner_sub=self.owner.sub, owner_display=self.owner.email, at=SEED_AT,
                            indexing=manifest.indexing)
        files = list_files(self.s3, self.bucket, pdir.prefix)
        planned, unmatched = expand_assets(manifest.assets, files)
        for pattern in unmatched:
            log.info("preload: project %s: pattern %r matched no file", pdir.name, pattern)

        placed: list[dict] = []
        for item in planned:
            abs_spec = _abs_asset(pdir.prefix, item.spec)
            try:
                if abs_spec.mode == "raw":
                    p = A.place_raw(self.s3, self.bucket, pid, abs_spec, folder=item.folder)
                else:
                    p = A.register_preanalyzed(self.s3, self.store, self.bucket, pid, self.owner.sub,
                                               self.owner.email, abs_spec, folder=item.folder)
                placed.append({"mode": p.mode, "assetId": p.asset_id, "key": p.key, "folder": item.folder,
                               "created": p.created})
            except Exception as exc:  # noqa: BLE001 — one bad asset fails only itself
                log.warning("preload: asset %s failed: %s", abs_spec.media, exc)
                result.errors.append(f"asset {abs_spec.media}: {exc}")

        skill_name: Optional[str] = None
        if SKILL_MD_REL in files:
            skill_name = self.do_skill(pdir, files)
        result.projects.append({"name": manifest.name, "dir": pdir.name, "pid": pid, "created": seed.created,
                                "assets": placed, "unmatched": unmatched, "skill": skill_name})

    def do_skill(self, pdir: ProjectDir, files: list[str]) -> Optional[str]:
        result = self.result
        label = f"{pdir.name}/{SKILL_MD_REL}"
        try:
            skill_md, supporting = _skill_files(self.s3, self.bucket, pdir, files)
            name = S.skill_name_from_md(skill_md)
        except Exception as exc:  # noqa: BLE001
            log.warning("preload: skill %s failed: %s", label, exc)
            result.errors.append(f"skill {label}: {exc}")
            return None
        first = self._skill_names.get(name.casefold())
        if first is not None:
            result.errors.append(f"skill {name} ({label}): duplicate skill name — already installed from "
                                 f"project {first}; skipped")
            return None
        self._skill_names[name.casefold()] = pdir.name
        try:
            if self._skill_s3 is None:
                from lab.s3_user_files_tool import S3UserFilesTool
                self._skill_s3 = S3UserFilesTool(bucket=self.artifacts_bucket, actor_id=self.owner.sub)
            plan = asyncio.run(S.install_skill(
                index=self.skill_index, s3=self._skill_s3, marker_table=self.store.table,
                owner_sub=self.owner.sub, skill_md=skill_md, files=supporting,
            ))
        except Exception as exc:  # noqa: BLE001 — one bad skill fails only itself
            log.warning("preload: skill %s failed: %s", name, exc)
            result.errors.append(f"skill {name} ({label}): {exc}")
            return None
        result.skills.append({
            "name": plan.name, "project": pdir.name, "draftId": plan.draft_id,
            "publishedId": plan.published_id, "version": plan.version,
            "publishedPrefix": plan.published_prefix, "files": plan.file_count,
            "validated": plan.validated, "created": plan.created, "action": plan.action,
        })
        return plan.name
