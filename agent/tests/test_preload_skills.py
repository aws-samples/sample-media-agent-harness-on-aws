"""Skill install from a project's ``skills/`` package, over moto, driving the real Lab lifecycle.

Rerun rules: an identical package is a no-op (digest marker); a changed ``SKILL.md`` or a changed / added
/ removed supporting file republishes the SAME lineage as version N+1, with the draft updated in place
(a removed file is deleted from it). The skill is never deleted, so its id is stable.
"""
import asyncio

import pytest

from lab.s3_user_files_tool import S3UserFilesTool
from preload import skills as S
from preload_fixtures import ARTIFACTS, OWNER_SUB, moto_env, skill_md


@pytest.fixture
def env(monkeypatch):
    with moto_env(monkeypatch) as e:
        yield e


def _install(env, md, files):
    tool = S3UserFilesTool(bucket=ARTIFACTS, actor_id=OWNER_SUB)
    return asyncio.run(S.install_skill(index=env.index, s3=tool, marker_table=env.store.table,
                                       owner_sub=OWNER_SUB, skill_md=md, files=files))


def _draft_files(env, draft_id):
    prefix = f"users/{OWNER_SUB}/drafts/skills/{draft_id}/"
    return {k[len(prefix):] for k in env.keys(prefix, bucket=ARTIFACTS)}


def _published_files(env, prefix):
    return {k[len(prefix):] for k in env.keys(prefix, bucket=ARTIFACTS)}


def _body(env, key):
    return env.s3.get_object(Bucket=ARTIFACTS, Key=key)["Body"].read()


MD = skill_md("lab-helper")
FILES = {"references/a.md": b"alpha", "scripts/run.py": b"print(1)"}


def test_name_comes_from_the_frontmatter():
    assert S.skill_name_from_md(MD) == "lab-helper"
    with pytest.raises(S.SkillInstallError):
        S.skill_name_from_md("# no frontmatter")
    with pytest.raises(S.SkillInstallError):
        S.skill_name_from_md("---\ndescription: d\n---\nbody")


def test_digest_covers_paths_bytes_and_order_independence():
    a = S.package_digest(MD, [("references/a.md", b"x"), ("scripts/b.py", b"y")])
    assert a == S.package_digest(MD, [("scripts/b.py", b"y"), ("references/a.md", b"x")])
    assert a != S.package_digest(MD, [("references/a.md", b"x")])
    assert a != S.package_digest(MD, [("references/a.md", b"x!"), ("scripts/b.py", b"y")])
    assert a != S.package_digest(MD, [("references/b.md", b"x"), ("scripts/b.py", b"y")])
    assert a != S.package_digest(MD + "\n", [("references/a.md", b"x"), ("scripts/b.py", b"y")])


def test_invalid_package_fails_cleanly():
    with pytest.raises(S.SkillInstallError):
        S.validate_skill(MD, {"elsewhere/a.md": b"x"})        # not under references/ scripts/ assets/
    with pytest.raises(S.SkillInstallError):
        S.validate_skill(skill_md("Bad_Name"), {})


def test_first_install_publishes_v1_and_writes_the_marker(env):
    plan = _install(env, MD, FILES)
    assert plan.created is True and plan.action == "installed" and plan.version == 1
    assert plan.name == "lab-helper" and plan.file_count == 3
    assert plan.published_prefix == f"users/{OWNER_SUB}/published/skills/{plan.published_id}/v1/"
    assert _published_files(env, plan.published_prefix) == {"SKILL.md", "references/a.md", "scripts/run.py"}
    pub = env.published()
    assert len(pub) == 1 and pub[0]["scope"] == "private" and int(pub[0]["version"]) == 1
    marker = S.read_marker(env.store.table, OWNER_SUB, "LAB-HELPER")   # keyed case-insensitively
    assert marker["digest"] == S.validate_skill(MD, FILES).digest
    assert marker["draftId"] == plan.draft_id and marker["publishedId"] == plan.published_id
    assert int(marker["version"]) == 1


def test_identical_rerun_mints_no_version(env):
    first = _install(env, MD, FILES)
    again = _install(env, MD, FILES)
    assert again.created is False and again.action == "unchanged"
    assert (again.published_id, again.version) == (first.published_id, 1)
    assert len(env.published()) == 1


def test_changed_skill_md_republishes_the_same_lineage_as_v2(env):
    first = _install(env, MD, FILES)
    md2 = skill_md("lab-helper", body="New body.")
    second = _install(env, md2, FILES)
    assert second.created is True and second.action == "updated"
    assert second.published_id == first.published_id and second.version == 2
    assert second.draft_id == first.draft_id                     # never deleted, same draft
    assert _body(env, f"{second.published_prefix}SKILL.md").decode() == md2
    assert _body(env, f"{first.published_prefix}SKILL.md").decode() == MD   # v1 is immutable
    pub = sorted(env.published(), key=lambda r: int(r["version"]))
    assert [int(r["version"]) for r in pub] == [1, 2]
    assert [bool(r.get("current")) for r in pub] == [False, True]
    assert int(S.read_marker(env.store.table, OWNER_SUB, "lab-helper")["version"]) == 2
    # And a third identical run is a no-op again.
    assert _install(env, md2, FILES).created is False


def test_changed_and_removed_supporting_files_republish_and_clean_the_draft(env):
    first = _install(env, MD, FILES)
    files2 = {"references/a.md": b"alpha v2", "assets/logo.txt": b"new"}   # scripts/run.py removed
    second = _install(env, MD, files2)
    assert second.published_id == first.published_id and second.version == 2
    assert _draft_files(env, first.draft_id) == {"SKILL.md", "references/a.md", "assets/logo.txt"}
    assert _published_files(env, second.published_prefix) == {"SKILL.md", "references/a.md", "assets/logo.txt"}
    assert _body(env, f"{second.published_prefix}references/a.md") == b"alpha v2"
    assert _published_files(env, first.published_prefix) == {"SKILL.md", "references/a.md", "scripts/run.py"}


def test_removing_one_file_alone_is_a_change(env):
    first = _install(env, MD, FILES)
    second = _install(env, MD, {"references/a.md": b"alpha"})
    assert second.created is True and second.version == 2 and second.published_id == first.published_id
    assert _draft_files(env, first.draft_id) == {"SKILL.md", "references/a.md"}


def test_an_existing_draft_without_a_marker_becomes_a_new_version(env):
    # E.g. installed before the marker existed: the draft is reused, never duplicated or deleted.
    first = _install(env, MD, FILES)
    env.store.table.delete_item(Key=S.marker_key(OWNER_SUB, "lab-helper"))
    second = _install(env, MD, FILES)
    assert second.created is True and second.action == "updated"
    assert second.published_id == first.published_id and second.version == 2
    drafts = [r for r in env.lab_rows() if r.get("status") == "draft"]
    assert len(drafts) == 1


# ---------------------------------------------------------------- a published skill of the same name, no draft
def _crud(env):
    from lab.context import LabRequestContext
    from lab.skill_crud import SkillCRUD

    tool = S3UserFilesTool(bucket=ARTIFACTS, actor_id=OWNER_SUB)
    return SkillCRUD(s3=tool, index=env.index), LabRequestContext(actor_id=OWNER_SUB, session_id="t", request_id="t")


def _drop_draft_and_marker(env, draft_id):
    crud, ctx = _crud(env)
    assert asyncio.run(crud.delete(ctx, draft_id)) is True
    env.store.table.delete_item(Key=S.marker_key(OWNER_SUB, "lab-helper"))


def _lineages(env):
    return {r["assetId"] for r in env.published()}


def test_a_published_skill_without_a_draft_gets_the_next_version_not_a_second_skill(env):
    # The staging case: the owner's own skill was published long ago and its draft is gone.
    first = _install(env, MD, FILES)
    _drop_draft_and_marker(env, first.draft_id)
    md2 = skill_md("lab-helper", body="Preloaded body.")
    second = _install(env, md2, FILES)
    assert second.created is True and second.action == "updated"
    assert second.published_id == first.published_id and second.version == 2
    assert _lineages(env) == {first.published_id}                     # still ONE skill of that name
    assert _body(env, f"{second.published_prefix}SKILL.md").decode() == md2
    drafts = [r for r in env.lab_rows() if r.get("status") == "draft"]
    assert len(drafts) == 1                                            # the clone, kept as the working copy
    # And the marker now makes an identical rerun a no-op.
    assert _install(env, md2, FILES).created is False


def test_with_two_same_named_skills_the_most_recently_published_one_is_versioned(env):
    first = _install(env, MD, FILES)                                   # lineage A, v1
    _drop_draft_and_marker(env, first.draft_id)
    crud, ctx = _crud(env)
    imported = asyncio.run(crud.import_package(ctx, "lab-helper", MD, list(FILES.items())))
    later = asyncio.run(crud.promote(ctx, imported.asset_id, scope="private"))   # lineage B, v1, published later
    assert later.published_id != first.published_id
    asyncio.run(crud.delete(ctx, imported.asset_id))
    third = _install(env, skill_md("lab-helper", body="Newest."), FILES)
    assert third.published_id == later.published_id and third.version == 2
    assert _lineages(env) == {first.published_id, later.published_id}  # no third lineage
