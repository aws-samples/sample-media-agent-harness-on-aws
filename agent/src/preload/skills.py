"""Install a project's skill under the owner (design Part 3, user-level publish).

A project directory's ``skills/`` folder, when it holds a ``SKILL.md``, IS that project's one skill
package (``SKILL.md`` plus any ``references/`` / ``scripts/`` / ``assets/`` beside it). Its name is the
``name:`` in the ``SKILL.md`` frontmatter.

A preloaded skill is installed by driving the **existing Lab lifecycle** under the owner's identity, so the
end state is byte-for-byte what a user publishing their own skill privately produces:

1. :meth:`lab.skill_crud.SkillCRUD.import_package` validates the package, writes a fresh unindexed
   draft under ``users/<sub>/drafts/skills/<draftId>/`` and records the draft AssetIndex row; then
2. :meth:`lab.skill_crud.SkillCRUD.promote` (``scope="private"``) copies the artifacts to the canonical
   published prefix ``users/<sub>/published/skills/<publishedId>/vN/`` and writes the **published
   AssetIndex row** (``version>=1`` + ``scope="private"``) in one ``TransactWriteItems``.

Reusing the real lifecycle — rather than hand-building the row in this deploy Lambda — is deliberate:
a published row must carry a positive ``version`` + the intended ``scope`` through all FIVE
``AssetCommon`` field-by-field builders (``_canonical_ops._build_published_asset`` /
``_build_shared_asset`` / ``_build_cloned_draft``, ``models.from_index_item``, and
``_lifecycle_ops._clone_to_working_copy`` — see AGENTS.md "FIVE field-by-field builders"), and any
hand-rolled copy risks a shape the projector or runtime rejects. ``promote_asset`` already threads
them correctly; ``tests/test_preload_skill_builder_parity.py`` pins that the two fields this module
relies on survive every builder.

Re-deploys (keyed by a content digest, recorded in a marker row in the projects table):

- marker digest == package digest → nothing to do (``created=False``);
- no draft of that name, but the owner already has a PUBLISHED skill of that name (its draft was deleted, or
  it was published elsewhere) → its newest version is cloned into a working copy and the package is
  published as the NEXT version of that same skill, never as a second skill with the same name. When several
  lineages share the name, the most recently published one is used;
- no draft and no published skill of that name → import + promote (v1 of a new lineage);
- a draft of that name exists and the marker is absent or differs → the package REPLACES the skill as a
  new version: the draft is updated in place (``update_skill_md``, ``put_supporting_file`` for every
  file, ``delete_supporting_file`` for files the package no longer has), then promoted under the SAME
  lineage id (``published_id = lastPublishedAs``), so ``vN+1`` is minted and references to the skill's id
  keep working. The skill is never deleted (that would change its id).

A private publish is user-scoped (not org), so — exactly like a user's own private skill — it is NOT
projected to the shared org catalog (``lab.outbox`` is an org-only filter); the owner activates it from
their Lab inventory.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from lab.context import LabRequestContext
from lab.path_validator import derive_record_prefix
from lab.skill_crud import SkillCRUD
from lab.skill_package_validator import _FRONTMATTER_RE, validate_skill_package

SKILL_MD = "SKILL.md"
MARKER_PK = "PRELOAD#SKILL"


class SkillInstallError(ValueError):
    """A skill package that cannot be installed (fail this one skill, not the deploy)."""


@dataclass(frozen=True)
class ValidatedSkill:
    """The pure result of validating a package: its name, normalized files and content digest."""

    name: str
    normalized_files: list[tuple[str, bytes]]
    digest: str
    file_count: int


@dataclass(frozen=True)
class InstallPlan:
    name: str
    draft_id: str
    published_id: str
    version: int
    published_prefix: str
    file_count: int
    validated: bool
    created: bool  # True → a new published version was minted by this run
    action: str    # "installed" | "updated" | "unchanged"


def published_prefix(owner_sub: str, asset_id: str, version: int = 1) -> str:
    """The canonical published prefix ``users/<sub>/published/skills/<assetId>/vN/`` (``derive_record_prefix``)."""
    return derive_record_prefix(
        scope="private",
        owner_user_id=owner_sub,
        owner_group_id=None,
        owner_org_id=None,
        category="skill",
        asset_id=asset_id,
        version=version,
        status="published",
    )


def _as_bytes(content: Any) -> bytes:
    return content if isinstance(content, bytes) else str(content).encode("utf-8")


def skill_name_from_md(skill_md: str) -> str:
    """The ``name:`` from ``SKILL.md``'s YAML frontmatter (parsed as ``skill_package_validator`` does)."""
    import yaml  # PyYAML ships in the preload Lambda; imported lazily like the validator does

    match = _FRONTMATTER_RE.match(skill_md or "")
    if match is None:
        raise SkillInstallError("SKILL.md must begin with YAML frontmatter enclosed by --- delimiters")
    try:
        front = yaml.safe_load(match.group("yaml"))
    except yaml.YAMLError as exc:
        raise SkillInstallError(f"SKILL.md frontmatter is invalid YAML: {exc}") from exc
    name = front.get("name") if isinstance(front, Mapping) else None
    if not isinstance(name, str) or not name.strip():
        raise SkillInstallError("SKILL.md frontmatter requires a string name")
    return name.strip()


def package_digest(skill_md: str, files: list[tuple[str, bytes]]) -> str:
    """sha256 over ``SKILL.md`` and every supporting file as ``(relative path, bytes)``, in path order.

    Each part is length-prefixed so no two different packages can produce the same byte stream.
    """
    h = hashlib.sha256()
    for path, data in [(SKILL_MD, skill_md.encode("utf-8"))] + sorted(files):
        p = path.encode("utf-8")
        h.update(len(p).to_bytes(4, "big") + p + len(data).to_bytes(8, "big") + data)
    return h.hexdigest()


def validate_skill(skill_md: str, files: Mapping[str, Any]) -> ValidatedSkill:
    """Name the package from its frontmatter, validate it, and digest it (no writes).

    Raises :class:`SkillInstallError` on a package the validator rejects.
    """
    name = skill_name_from_md(skill_md)
    supporting = [(p, _as_bytes(c)) for p, c in files.items()]
    try:
        normalized = validate_skill_package(name=name, skill_md=skill_md, supporting_files=supporting)
    except Exception as exc:  # noqa: BLE001 — surface the validator's message cleanly
        raise SkillInstallError(f"skill {name!r} failed validation: {exc}") from exc
    normalized = list(normalized)
    return ValidatedSkill(name=name, normalized_files=normalized, digest=package_digest(skill_md, normalized),
                          file_count=len(normalized) + 1)


# ---------------------------------------------------------------------------------------------- marker row


def marker_key(owner_sub: str, name: str) -> dict:
    return {"PK": MARKER_PK, "SK": f"{owner_sub}#{name.casefold()}"}


def read_marker(table: Any, owner_sub: str, name: str) -> Optional[dict]:
    return table.get_item(Key=marker_key(owner_sub, name), ConsistentRead=True).get("Item")


def write_marker(table: Any, owner_sub: str, name: str, *, digest: str, draft_id: str, published_id: str,
                 version: int) -> None:
    table.put_item(Item={
        **marker_key(owner_sub, name),
        "name": name,
        "digest": digest,
        "draftId": draft_id,
        "publishedId": published_id,
        "version": int(version),
        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })


# ----------------------------------------------------------------------------------------------- install


async def _replace_draft(crud: SkillCRUD, ctx: LabRequestContext, draft_id: str, skill_md: str,
                         files: list[tuple[str, bytes]]) -> None:
    """Make the existing draft hold exactly this package (SKILL.md + files; stale files deleted)."""
    if await crud.update_skill_md(ctx, draft_id, skill_md) is None:
        raise SkillInstallError(f"draft {draft_id} vanished while updating SKILL.md")
    for path, content in files:
        if await crud.put_supporting_file(ctx, draft_id, path, content) is None:
            raise SkillInstallError(f"draft {draft_id} vanished while writing {path}")
    keep = {p for p, _ in files}
    existing = (await crud.list_supporting_files([draft_id])).get(draft_id, [])
    for path in existing:
        if path not in keep:
            await crud.delete_supporting_file(ctx, draft_id, path)


async def _latest_published_of_name(index: Any, owner_sub: str, name: str) -> Optional[tuple[str, int]]:
    """``(lineage id, newest version)`` of the owner's PRIVATE published skill named ``name``, or ``None``.

    Several lineages can share a name (the duplicates this guards against); the one whose newest version was
    published most recently wins. Deprecated versions are ignored when a live one exists.
    """
    from lab.asset_index import make_pk_user_versions

    best: dict[str, tuple[int, int]] = {}   # lineage -> (newest version, its created_at)
    cursor = None
    while True:
        items, cursor = await index.query_pk(make_pk_user_versions(owner_sub), sk_prefix="skill#",
                                             last_evaluated_key=cursor)
        for it in items:
            if (it.name or "").casefold() != name.casefold() or it.version is None or getattr(it, "deprecated", False):
                continue
            lid = it.asset_id
            cur = best.get(lid)
            if cur is None or it.version > cur[0]:
                best[lid] = (int(it.version), int(getattr(it, "created_at", 0) or 0))
        if not cursor:
            break
    if not best:
        return None
    lineage, (version, _) = max(best.items(), key=lambda kv: (kv[1][1], kv[1][0]))
    return lineage, version


async def install_skill(
    *,
    index: Any,
    s3: Any,
    marker_table: Any,
    owner_sub: str,
    skill_md: str,
    files: Mapping[str, Any],
) -> InstallPlan:
    """Install or update a project's skill under ``owner_sub`` (rules in the module docstring).

    ``index`` is a :class:`lab.asset_index.AssetIndex` (or a compatible fake); ``s3`` is a
    :class:`lab.s3_user_files_tool.S3UserFilesTool` bound to ``owner_sub``; ``marker_table`` is the
    projects table (boto3 ``Table``) that holds the ``PRELOAD#SKILL`` digest markers. Raises
    :class:`SkillInstallError` on a package the validator rejects so the runner fails this one skill.
    """
    validated = validate_skill(skill_md, files)
    name = validated.name
    marker = read_marker(marker_table, owner_sub, name)
    if marker and marker.get("digest") == validated.digest:
        pub_id, version = str(marker["publishedId"]), int(marker["version"])
        return InstallPlan(
            name=name, draft_id=str(marker.get("draftId", "")), published_id=pub_id, version=version,
            published_prefix=published_prefix(owner_sub, pub_id, version), file_count=validated.file_count,
            validated=True, created=False, action="unchanged",
        )

    ctx = LabRequestContext(actor_id=owner_sub, session_id="preload", request_id="preload")
    crud = SkillCRUD(s3=s3, index=index)
    existing = [sk for sk in await crud.list(ctx) if sk.name.casefold() == name.casefold()]
    lineage: Optional[str] = None
    if not existing:
        # No draft, but the owner may already have a PUBLISHED skill of this name (its draft deleted, or
        # published from another surface). Publish into THAT lineage rather than starting a second skill of the
        # same name: clone its newest version into a working copy and replace that below.
        prior = await _latest_published_of_name(index, owner_sub, name)
        if prior is not None:
            lineage, prior_version = prior
            cloned = await crud.clone(ctx, lineage, prior_version)
            existing = [sk for sk in await crud.list(ctx) if sk.asset_id == cloned.draft_id]
    if existing:
        draft = existing[0]
        draft_id = draft.asset_id
        await _replace_draft(crud, ctx, draft_id, skill_md, validated.normalized_files)
        if lineage is None and draft.last_published_as:
            lineage = draft.last_published_as[0]
        if lineage is None and getattr(draft, "cloned_from", None):
            lineage = draft.cloned_from[0]
        action = "updated"
    else:
        result = await crud.import_package(ctx, name, skill_md, validated.normalized_files)
        draft_id = result.asset_id
        action = "installed"

    promoted = await crud.promote(ctx, draft_id, scope="private", published_id=lineage)
    write_marker(marker_table, owner_sub, name, digest=validated.digest, draft_id=draft_id,
                 published_id=promoted.published_id, version=promoted.version)
    return InstallPlan(
        name=name, draft_id=draft_id, published_id=promoted.published_id, version=promoted.version,
        published_prefix=promoted.s3_prefix, file_count=validated.file_count, validated=True, created=True,
        action=action,
    )
