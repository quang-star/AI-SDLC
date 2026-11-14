"""Role-Based Access Control (RBAC) matrix for multi-tenant AI SDLC SaaS."""

from __future__ import annotations

from typing import Final, Literal

RoleName = Literal["owner", "admin", "developer", "reviewer", "viewer"]

VALID_ROLES: Final[frozenset[str]] = frozenset({"owner", "admin", "developer", "reviewer", "viewer"})

ROLE_PERMISSIONS: Final[dict[str, frozenset[str]]] = {
    "owner": frozenset(
        {
            "org:read",
            "org:manage",
            "org:delete",
            "org:transfer_owner",
            "members:read",
            "members:manage",
            "billing:read",
            "billing:manage",
            "repo:read",
            "repo:connect",
            "model:read",
            "model:configure",
            "task:read",
            "task:create",
            "task:cancel",
            "spec:approve",
            "plan:approve",
            "delivery:approve",
            "release:approve",
            "audit:read",
            "data:export",
            "data:delete",
        }
    ),
    "admin": frozenset(
        {
            "org:read",
            "org:manage",
            "members:read",
            "members:manage",
            "billing:read",
            "billing:manage",
            "repo:read",
            "repo:connect",
            "model:read",
            "model:configure",
            "task:read",
            "task:create",
            "task:cancel",
            "spec:approve",
            "plan:approve",
            "delivery:approve",
            "release:approve",
            "audit:read",
            "data:export",
        }
    ),
    "developer": frozenset(
        {
            "org:read",
            "members:read",
            "repo:read",
            "model:read",
            "task:read",
            "task:create",
            "task:cancel",
            "spec:approve",
            "plan:approve",
        }
    ),
    "reviewer": frozenset(
        {
            "org:read",
            "members:read",
            "repo:read",
            "model:read",
            "task:read",
            "spec:approve",
            "plan:approve",
            "delivery:approve",
            "audit:read",
        }
    ),
    "viewer": frozenset(
        {
            "org:read",
            "members:read",
            "repo:read",
            "task:read",
        }
    ),
}


def has_permission(role: str, permission: str) -> bool:
    """Return True if `role` grants `permission`."""
    perms = ROLE_PERMISSIONS.get((role or "").lower(), frozenset())
    return permission in perms


def list_permissions(role: str) -> tuple[str, ...]:
    """Return sorted tuple of permissions granted to `role`."""
    return tuple(sorted(ROLE_PERMISSIONS.get((role or "").lower(), frozenset())))
