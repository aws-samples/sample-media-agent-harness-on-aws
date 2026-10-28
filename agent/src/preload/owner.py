"""Resolve ``ownerEmail`` to a Cognito ``sub`` (design Part 1, owner "model A").

The end-user pool is native Cognito (email alias, self-signup off), so ``AdminCreateUser`` returns a user
whose ``sub`` is known immediately. Resolution is: ``ListUsers`` by email; if none, ``AdminCreateUser`` (the
user gets Cognito's invite with a temporary password) and add to the admin group. That ``sub`` owns the
Cognito user, every seeded project, and every installed skill.

Idempotent: a re-deploy finds the existing user and creates nothing. The email is never logged (only the
resolved opaque ``sub``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

ADMIN_GROUP = "gateway-admins"


class OwnerResolutionError(RuntimeError):
    """ownerEmail could not be resolved or created."""


@dataclass(frozen=True)
class Owner:
    sub: str
    email: str
    created: bool


def _sub_of(user: dict) -> Optional[str]:
    for attr in user.get("Attributes") or user.get("UserAttributes") or []:
        if attr.get("Name") == "sub":
            return attr.get("Value")
    return None


def resolve_owner(idp: Any, user_pool_id: str, email: str, *, admin_group: str = ADMIN_GROUP) -> Owner:
    """Return the owner's ``sub`` for ``email``, creating the Cognito user + admin membership if absent."""
    if not isinstance(email, str) or "@" not in email:
        raise OwnerResolutionError("ownerEmail must be a valid email address")

    existing = idp.list_users(
        UserPoolId=user_pool_id,
        Filter=f'email = "{email}"',
        Limit=1,
    )
    users = existing.get("Users") or []
    if users:
        sub = _sub_of(users[0])
        if not sub:
            raise OwnerResolutionError("existing Cognito user has no 'sub' attribute")
        _ensure_group(idp, user_pool_id, users[0].get("Username") or email, admin_group)
        return Owner(sub=sub, email=email, created=False)

    created = idp.admin_create_user(
        UserPoolId=user_pool_id,
        Username=email,
        UserAttributes=[
            {"Name": "email", "Value": email},
            {"Name": "email_verified", "Value": "true"},
        ],
        DesiredDeliveryMediums=["EMAIL"],
    )
    user = created.get("User") or {}
    sub = _sub_of(user)
    if not sub:
        raise OwnerResolutionError("AdminCreateUser returned no 'sub' attribute")
    _ensure_group(idp, user_pool_id, user.get("Username") or email, admin_group)
    return Owner(sub=sub, email=email, created=True)


def _ensure_group(idp: Any, user_pool_id: str, username: str, group: str) -> None:
    """Add the user to ``group`` (idempotent — a repeated add is a no-op in Cognito)."""
    idp.admin_add_user_to_group(UserPoolId=user_pool_id, Username=username, GroupName=group)
