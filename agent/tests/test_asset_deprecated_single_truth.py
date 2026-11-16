"""`deprecated` is the one stored truth; `status` is derived from (version, deprecated).

Regression: `deprecate_asset` once wrote only `status="deprecated"` while every v2 row also stored
`deprecated: false`, so the runtime (which reads `deprecated`) kept loading deprecated skills.
"""
import pytest

from lab.models import Skill, from_index_item, to_index_item


def _skill(**kw) -> Skill:
    base = dict(asset_id="s1", category="skill", name="n", status="published", scope="private",
                s3_prefix="users/u/published/skills/s1/v1/", created_at=1, updated_at=1, version=1)
    return Skill(**{**base, **kw})


def _row(**kw) -> dict:
    return {"assetId": "s1", "category": "skill", "name": "n", "s3Prefix": "p/", "version": 1, **kw}


@pytest.mark.parametrize("row", [
    _row(status="deprecated", deprecated=False),  # written by the old deprecate_asset
    _row(status="published", deprecated=True),  # flag set, stale stored status
    _row(deprecated=True),  # current shape: no stored status
    _row(status="deprecated"),  # pre-flag legacy row
])
def test_a_deprecated_row_decodes_as_deprecated_whatever_status_says(row):
    asset = from_index_item(row)
    assert asset.deprecated is True and asset.status == "deprecated"


def test_status_is_derived_and_never_stored():
    assert _skill(status="draft").status == "published"  # a version is never a draft
    assert _skill(version=None).status == "draft"
    assert _skill(deprecated=True).status == "deprecated"
    item = to_index_item(_skill(deprecated=True), owner_user_id="u", owner_group_id=None, owner_org_id=None)
    assert "status" not in item and item["deprecated"] is True
