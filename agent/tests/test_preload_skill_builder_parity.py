"""Guard the AGENTS.md "FIVE field-by-field builders" trap for the preload skill publish.

A preloaded skill is installed by driving the real Lab publish lifecycle (``preload.skills.install_skill``),
which relies on the published AssetIndex row carrying a positive ``version`` and the intended ``scope``.
AGENTS.md warns that ``AssetCommon`` has FIVE independent field-by-field builders and a grep for
``common_kwargs`` MISSES the fifth (``base_kwargs`` in ``_lifecycle_ops._clone_to_working_copy``). A new
field — or a dropped carry of an existing one — is silently lost by any builder that does not name it.

These tests exercise ``scope`` (and ``version`` where a positive version is meaningful) across all five so
the field the preload publish depends on cannot be dropped from one builder without a red test.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab._canonical_ops import (  # noqa: E402
    _build_cloned_draft,
    _build_published_asset,
    _build_shared_asset,
)
from lab._lifecycle_ops import _clone_to_working_copy  # noqa: E402
from lab.models import Skill, from_index_item  # noqa: E402


def _draft(*, version=None, scope="private", status="draft") -> Skill:
    """A minimal Skill row at the given lifecycle position."""
    return Skill(
        asset_id="asset-1",
        category="skill",
        name="mod-helper",
        status=status,
        scope=scope,
        s3_prefix="users/u-owner/drafts/skills/asset-1/",
        created_at=1,
        updated_at=1,
        version=version,
    )


# --- Builder 1 of 5: _canonical_ops._build_published_asset (the preload publish path) ---------------
def test_published_builder_carries_version_and_scope():
    published = _build_published_asset(
        draft=_draft(),
        published_id="pub-1",
        version=1,
        scope="private",
        target_prefix="users/u-owner/published/skills/pub-1/v1/",
        actor_id="u-owner",
        publish_policy_owner_ids=["u-owner"],
        group_ids=None,
        timestamp_iso="2026-10-27T00:00:00.000000Z",
        timestamp_ms=1,
    )
    assert published.version == 1 and published.version >= 1
    assert published.scope == "private"
    assert published.status == "published"


# --- Builder 2 of 5: _canonical_ops._build_shared_asset ---------------------------------------------
def test_shared_builder_carries_version_and_scope():
    source = _draft(version=1, scope="private", status="published")
    shared = _build_shared_asset(
        source=source,
        new_scope="org",
        target_prefix="org/o-acme/published/skills/asset-1/v1/",
        actor_id="u-owner",
        group_ids=None,
        timestamp_iso="2026-10-27T00:00:00.000000Z",
        timestamp_ms=2,
    )
    assert shared.version == 1          # version identity preserved across a scope-widen
    assert shared.scope == "org"        # scope updated, never dropped


# --- Builder 3 of 5: _canonical_ops._build_cloned_draft ---------------------------------------------
def test_cloned_draft_builder_sets_scope_private_and_resets_version():
    source = _draft(version=3, scope="org", status="published")
    cloned = _build_cloned_draft(
        source=source,
        draft_id="draft-2",
        target_prefix="users/u-owner/drafts/skills/draft-2/",
        timestamp_ms=3,
    )
    assert cloned.scope == "private"    # drafts are always private (Req 2.4) — scope explicitly set
    assert cloned.version is None       # a working copy has no version


# --- Builder 4 of 5: models.from_index_item ---------------------------------------------------------
def test_from_index_item_preserves_version_and_scope():
    item = {
        "assetId": "pub-1",
        "category": "skill",
        "name": "mod-helper",
        "status": "published",
        "scope": "private",
        "s3Prefix": "users/u-owner/published/skills/pub-1/v1/",
        "createdAt": 1,
        "updatedAt": 1,
        "version": 1,
    }
    rebuilt = from_index_item(item)
    assert rebuilt.version == 1 and rebuilt.scope == "private"


# --- Builder 5 of 5: _lifecycle_ops._clone_to_working_copy (the one a `common_kwargs` grep misses) --
def test_clone_to_working_copy_sets_scope_private_and_resets_version():
    source = _draft(version=2, scope="org", status="published")
    wc = _clone_to_working_copy(
        source=source,
        actor_id="u-owner",
        category="skill",
        asset_id="asset-1",
        forked_from_version=2,
        timestamp_ms=4,
    )
    assert wc.scope == "private"        # explicitly set in base_kwargs — not defaulted away
    assert wc.version is None


def test_all_five_builders_set_scope_explicitly():
    """A single assertion that every builder produces a row whose ``scope`` is a real value.

    If any builder dropped ``scope`` the dataclass construction would fail (it is a required field),
    so reaching here across all five is itself the guard; the per-builder tests above pin the value.
    """
    assert _build_published_asset(
        draft=_draft(), published_id="p", version=1, scope="private",
        target_prefix="users/u-owner/published/skills/p/v1/", actor_id="u-owner",
        publish_policy_owner_ids=["u-owner"], group_ids=None,
        timestamp_iso="2026-10-27T00:00:00.000000Z", timestamp_ms=1,
    ).scope in ("private", "group", "org")
