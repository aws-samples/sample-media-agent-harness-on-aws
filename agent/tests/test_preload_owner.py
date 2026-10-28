"""Owner resolution with a mocked Cognito client (design Part 1, model A)."""
import pytest

from preload.owner import ADMIN_GROUP, Owner, OwnerResolutionError, resolve_owner

POOL = "us-west-2_pool"
EMAIL = "owner@example.com"


class FakeIdp:
    def __init__(self, existing=None):
        self._existing = existing or []
        self.created = []
        self.group_adds = []

    def list_users(self, UserPoolId, Filter, Limit):
        assert UserPoolId == POOL
        assert Filter == f'email = "{EMAIL}"'
        return {"Users": list(self._existing)}

    def admin_create_user(self, UserPoolId, Username, UserAttributes, DesiredDeliveryMediums):
        assert DesiredDeliveryMediums == ["EMAIL"]
        self.created.append(Username)
        return {"User": {"Username": Username,
                         "Attributes": [{"Name": "sub", "Value": "new-sub-abc"}]}}

    def admin_add_user_to_group(self, UserPoolId, Username, GroupName):
        self.group_adds.append((Username, GroupName))


def test_existing_user_is_reused_not_created():
    idp = FakeIdp(existing=[{"Username": "owner@example.com",
                             "Attributes": [{"Name": "sub", "Value": "existing-sub"}]}])
    owner = resolve_owner(idp, POOL, EMAIL)
    assert owner == Owner(sub="existing-sub", email=EMAIL, created=False)
    assert idp.created == []                                  # idempotent
    assert idp.group_adds == [("owner@example.com", ADMIN_GROUP)]


def test_absent_user_is_created_and_added_to_admin_group():
    idp = FakeIdp(existing=[])
    owner = resolve_owner(idp, POOL, EMAIL)
    assert owner.sub == "new-sub-abc"
    assert owner.created is True
    assert idp.created == [EMAIL]
    assert idp.group_adds == [(EMAIL, ADMIN_GROUP)]


def test_bad_email_rejected():
    with pytest.raises(OwnerResolutionError):
        resolve_owner(FakeIdp(), POOL, "not-an-email")


def test_existing_user_without_sub_is_an_error():
    idp = FakeIdp(existing=[{"Username": "x", "Attributes": []}])
    with pytest.raises(OwnerResolutionError):
        resolve_owner(idp, POOL, EMAIL)
