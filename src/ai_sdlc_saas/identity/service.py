"""Identity, session lifecycle, organization, invitation, and RBAC membership services."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ai_sdlc_saas.billing.ledger import ensure_trial_subscription
from ai_sdlc_saas.billing.plans import seed_default_plans
from ai_sdlc_saas.db.engine import set_session_tenant, system_routing_scope
from ai_sdlc_saas.db.models import Invitation, Membership, Tenant, User, UserSession
from ai_sdlc_saas.errors import (
    AuthError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from ai_sdlc_saas.governance.audit import record_audit_event
from ai_sdlc_saas.security.crypto import (
    decode_session_jwt,
    generate_opaque_token,
    hash_password,
    hash_token,
    issue_session_jwt,
    verify_password,
)
from ai_sdlc_saas.security.rbac import VALID_ROLES, has_permission

_SLUG_CLEAN_RE = re.compile(r"[^a-z0-9-]+")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _normalize_email(email: str) -> str:
    cleaned = email.strip().lower()
    if not _EMAIL_RE.match(cleaned):
        raise ValidationError("Invalid email address.")
    return cleaned


def _slugify(value: str) -> str:
    slug = _SLUG_CLEAN_RE.sub("-", value.strip().lower()).strip("-")
    if not slug or len(slug) < 2:
        slug = f"org-{secrets.token_hex(3)}"
    return slug[:64]


@dataclass(slots=True)
class AuthContext:
    user: User
    session: UserSession
    tenant: Tenant | None
    membership: Membership | None

    def require_tenant(self) -> tuple[Tenant, Membership]:
        if self.tenant is None or self.membership is None:
            raise ForbiddenError("No active organization selected for this session.")
        if self.tenant.status != "active":
            raise ForbiddenError(f"Organization status is {self.tenant.status!r}.")
        if self.membership.status != "active":
            raise ForbiddenError("Organization membership is not active.")
        return self.tenant, self.membership

    def require_permission(self, permission: str) -> tuple[Tenant, Membership]:
        tenant, membership = self.require_tenant()
        if not has_permission(membership.role, permission):
            raise ForbiddenError(
                f"Role {membership.role!r} lacks required permission {permission!r}."
            )
        return tenant, membership


def _create_session_for_user(
    session: Session,
    *,
    user: User,
    active_tenant_id: str | None,
    jwt_secret: str,
    jwt_ttl_minutes: int,
    user_agent: str = "",
) -> tuple[UserSession, str]:
    jti = generate_opaque_token("jti", nbytes=18)
    sess_id = f"ses_{secrets.token_hex(8)}"
    token, expires_at = issue_session_jwt(
        user_id=user.id,
        session_id=sess_id,
        jti=jti,
        jwt_secret=jwt_secret,
        ttl_minutes=jwt_ttl_minutes,
    )
    user_session = UserSession(
        id=sess_id,
        user_id=user.id,
        jti_hash=hash_token(jti),
        active_tenant_id=active_tenant_id,
        user_agent=user_agent[:250],
        expires_at=expires_at,
    )
    session.add(user_session)
    session.flush()
    return user_session, token


def _create_org_internal(
    session: Session,
    *,
    owner_user: User,
    name: str,
    slug: str | None = None,
    data_region: str = "us-east",
) -> tuple[Tenant, Membership]:
    cleaned_name = name.strip()
    if len(cleaned_name) < 2:
        raise ValidationError("Organization name must be at least 2 characters.")
    candidate_slug = _slugify(slug or cleaned_name)
    existing_slug = session.scalar(select(Tenant).where(Tenant.slug == candidate_slug))
    if existing_slug is not None:
        if slug:
            raise ConflictError(f"Organization slug {candidate_slug!r} already exists.")
        candidate_slug = f"{candidate_slug[:55]}-{secrets.token_hex(3)}"

    seed_default_plans(session)
    tenant = Tenant(
        id=f"org_{secrets.token_hex(8)}",
        slug=candidate_slug,
        name=cleaned_name[:150],
        data_region=data_region,
        status="active",
    )
    session.add(tenant)
    session.flush()
    set_session_tenant(session, tenant.id)

    membership = Membership(
        id=f"mem_{secrets.token_hex(8)}",
        tenant_id=tenant.id,
        user_id=owner_user.id,
        role="owner",
        status="active",
    )
    session.add(membership)
    ensure_trial_subscription(session, tenant.id)
    record_audit_event(
        session,
        tenant_id=tenant.id,
        actor_user_id=owner_user.id,
        action="org.created",
        target_type="tenant",
        target_id=tenant.id,
        details={"slug": tenant.slug, "data_region": data_region},
    )
    session.flush()
    return tenant, membership


def register_user(
    session: Session,
    *,
    email: str,
    password: str,
    full_name: str,
    org_name: str | None = None,
    org_slug: str | None = None,
    data_region: str = "us-east",
    jwt_secret: str,
    jwt_ttl_minutes: int,
    user_agent: str = "",
) -> dict[str, Any]:
    """Register a user, optionally bootstrap their first organization, and open a session."""
    normalized_email = _normalize_email(email)
    if len(password) < 8:
        raise ValidationError("Password must be at least 8 characters.")
    if not full_name.strip():
        raise ValidationError("Full name is required.")
    if session.scalar(select(User).where(User.email == normalized_email)) is not None:
        raise ConflictError("An account with this email already exists.")

    verify_token = generate_opaque_token("evf", nbytes=24)
    user = User(
        id=f"usr_{secrets.token_hex(8)}",
        email=normalized_email,
        full_name=full_name.strip()[:120],
        password_hash=hash_password(password),
        is_verified=False,
        verification_token_hash=hash_token(verify_token),
    )
    session.add(user)
    session.flush()

    tenant: Tenant | None = None
    membership: Membership | None = None
    if org_name:
        tenant, membership = _create_org_internal(
            session,
            owner_user=user,
            name=org_name,
            slug=org_slug,
            data_region=data_region,
        )

    user_session, jwt_token = _create_session_for_user(
        session,
        user=user,
        active_tenant_id=tenant.id if tenant else None,
        jwt_secret=jwt_secret,
        jwt_ttl_minutes=jwt_ttl_minutes,
        user_agent=user_agent,
    )
    return {
        "user": user,
        "tenant": tenant,
        "membership": membership,
        "session": user_session,
        "access_token": jwt_token,
        "verification_token": verify_token,
    }


def verify_email(session: Session, *, token: str) -> User:
    token_digest = hash_token(token)
    user = session.scalar(select(User).where(User.verification_token_hash == token_digest))
    if user is None:
        raise ValidationError("Invalid or already used verification token.")
    user.is_verified = True
    user.verification_token_hash = None
    session.flush()
    return user


def login_user(
    session: Session,
    *,
    email: str,
    password: str,
    jwt_secret: str,
    jwt_ttl_minutes: int,
    org_id: str | None = None,
    user_agent: str = "",
) -> dict[str, Any]:
    normalized_email = _normalize_email(email)
    user = session.scalar(select(User).where(User.email == normalized_email))
    if user is None or not verify_password(password, user.password_hash):
        raise AuthError("Invalid email or password.")
    if user.is_locked:
        raise ForbiddenError("User account is locked.")

    with system_routing_scope(session):
        memberships = list(
            session.scalars(
                select(Membership).where(
                    Membership.user_id == user.id,
                    Membership.status == "active",
                )
            )
        )
    selected_membership: Membership | None = None
    selected_tenant: Tenant | None = None
    if org_id is not None:
        selected_membership = next((m for m in memberships if m.tenant_id == org_id), None)
        if selected_membership is None:
            raise ForbiddenError("User is not an active member of the requested organization.")
    elif memberships:
        selected_membership = memberships[0]

    if selected_membership is not None:
        selected_tenant = session.get(Tenant, selected_membership.tenant_id)
        if selected_tenant is None or selected_tenant.status != "active":
            selected_membership = None
            selected_tenant = None
        else:
            set_session_tenant(session, selected_tenant.id)

    user_session, jwt_token = _create_session_for_user(
        session,
        user=user,
        active_tenant_id=selected_tenant.id if selected_tenant else None,
        jwt_secret=jwt_secret,
        jwt_ttl_minutes=jwt_ttl_minutes,
        user_agent=user_agent,
    )
    return {
        "user": user,
        "tenant": selected_tenant,
        "membership": selected_membership,
        "session": user_session,
        "access_token": jwt_token,
    }


def request_password_reset(session: Session, *, email: str) -> str | None:
    normalized_email = _normalize_email(email)
    user = session.scalar(select(User).where(User.email == normalized_email))
    if user is None:
        return None
    reset_token = generate_opaque_token("rst", nbytes=24)
    user.reset_token_hash = hash_token(reset_token)
    user.reset_token_expires_at = datetime.now(UTC) + timedelta(minutes=30)
    session.flush()
    return reset_token


def confirm_password_reset(session: Session, *, token: str, new_password: str) -> User:
    if len(new_password) < 8:
        raise ValidationError("Password must be at least 8 characters.")
    user = session.scalar(select(User).where(User.reset_token_hash == hash_token(token)))
    if user is None or user.reset_token_expires_at is None:
        raise ValidationError("Invalid or expired password reset token.")
    if _as_utc(user.reset_token_expires_at) < datetime.now(UTC):
        raise ValidationError("Password reset token has expired.")

    user.password_hash = hash_password(new_password)
    user.reset_token_hash = None
    user.reset_token_expires_at = None
    now = datetime.now(UTC)
    session.execute(
        update(UserSession)
        .where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    session.flush()
    return user


def authenticate_jwt(session: Session, *, token: str, jwt_secret: str) -> AuthContext:
    """Validate bearer JWT, active session, non-locked user, and active membership."""
    try:
        payload = decode_session_jwt(token, jwt_secret=jwt_secret)
    except ValueError as exc:
        raise AuthError("Invalid or expired authentication token.") from exc

    user_id = str(payload.get("sub", ""))
    session_id = str(payload.get("sid", ""))
    jti = str(payload.get("jti", ""))
    if not user_id or not session_id or not jti:
        raise AuthError("Malformed token claims.")

    set_session_tenant(session, None)
    user_session = session.get(UserSession, session_id)
    if user_session is None or user_session.user_id != user_id:
        raise AuthError("Session not found.")
    if user_session.revoked_at is not None:
        raise AuthError("Session has been revoked.")
    if _as_utc(user_session.expires_at) <= datetime.now(UTC):
        raise AuthError("Session has expired.")
    if user_session.jti_hash != hash_token(jti):
        raise AuthError("Session token mismatch.")

    user = session.get(User, user_id)
    if user is None or user.is_locked:
        raise AuthError("Account is locked or does not exist.")

    tenant: Tenant | None = None
    membership: Membership | None = None
    if user_session.active_tenant_id:
        set_session_tenant(session, user_session.active_tenant_id)
        tenant = session.get(Tenant, user_session.active_tenant_id)
        membership = session.scalar(
            select(Membership).where(
                Membership.tenant_id == user_session.active_tenant_id,
                Membership.user_id == user.id,
            )
        )
        if tenant is None or tenant.status != "active" or membership is None or membership.status != "active":
            set_session_tenant(session, None)
            raise ForbiddenError("Active organization membership was revoked or tenant is inactive.")

    return AuthContext(user=user, session=user_session, tenant=tenant, membership=membership)


def verify_active_actor_access(
    session: Session,
    *,
    tenant_id: str,
    user_id: str,
    required_permission: str | None = None,
) -> tuple[User, Membership]:
    """Re-verify user, tenant, and active membership before resuming or publishing work."""
    user = session.get(User, user_id)
    if user is None or user.is_locked:
        raise ForbiddenError("Actor account is missing or locked.")
    tenant = session.get(Tenant, tenant_id)
    if tenant is None or tenant.status != "active":
        raise ForbiddenError("Organization is not active.")
    membership = session.scalar(
        select(Membership).where(
            Membership.tenant_id == tenant_id,
            Membership.user_id == user_id,
            Membership.status == "active",
        )
    )
    if membership is None:
        raise ForbiddenError("Actor membership was revoked or is inactive.")
    if required_permission and not has_permission(membership.role, required_permission):
        raise ForbiddenError(f"Actor role {membership.role!r} no longer has {required_permission!r}.")
    return user, membership


def logout_session(session: Session, *, user_session: UserSession) -> None:
    user_session.revoked_at = datetime.now(UTC)
    session.flush()


def list_sessions(session: Session, *, user_id: str) -> list[UserSession]:
    return list(
        session.scalars(
            select(UserSession)
            .where(UserSession.user_id == user_id)
            .order_by(UserSession.created_at.desc())
        )
    )


def revoke_session(session: Session, *, user_id: str, target_session_id: str) -> UserSession:
    target = session.get(UserSession, target_session_id)
    if target is None or target.user_id != user_id:
        raise NotFoundError("Session not found.")
    if target.revoked_at is None:
        target.revoked_at = datetime.now(UTC)
    session.flush()
    return target


def list_user_orgs(session: Session, *, user_id: str) -> list[dict[str, Any]]:
    with system_routing_scope(session):
        rows = session.execute(
            select(Tenant, Membership)
            .join(Membership, Membership.tenant_id == Tenant.id)
            .where(
                Membership.user_id == user_id,
                Membership.status == "active",
                Tenant.status == "active",
            )
            .order_by(Tenant.created_at.asc())
        ).all()
        return [{"tenant": t, "membership": m} for t, m in rows]


def create_org(
    session: Session,
    *,
    ctx: AuthContext,
    name: str,
    slug: str | None = None,
    data_region: str = "us-east",
) -> tuple[Tenant, Membership]:
    set_session_tenant(session, None)
    tenant, membership = _create_org_internal(
        session,
        owner_user=ctx.user,
        name=name,
        slug=slug,
        data_region=data_region,
    )
    ctx.session.active_tenant_id = tenant.id
    set_session_tenant(session, tenant.id)
    return tenant, membership


def switch_active_org(
    session: Session,
    *,
    ctx: AuthContext,
    target_tenant_id: str,
) -> tuple[Tenant, Membership]:
    set_session_tenant(session, None)
    tenant = session.get(Tenant, target_tenant_id)
    if tenant is None or tenant.status != "active":
        raise NotFoundError("Organization not found or inactive.")
    set_session_tenant(session, target_tenant_id)
    membership = session.scalar(
        select(Membership).where(
            Membership.tenant_id == target_tenant_id,
            Membership.user_id == ctx.user.id,
            Membership.status == "active",
        )
    )
    if membership is None:
        set_session_tenant(session, None)
        raise ForbiddenError("You are not an active member of that organization.")
    ctx.session.active_tenant_id = tenant.id
    record_audit_event(
        session,
        tenant_id=tenant.id,
        actor_user_id=ctx.user.id,
        action="org.switched",
        target_type="user_session",
        target_id=ctx.session.id,
        details={"tenant_id": tenant.id},
    )
    session.flush()
    return tenant, membership


def create_invitation(
    session: Session,
    *,
    tenant_id: str,
    actor_user_id: str,
    email: str,
    role: str,
    expires_hours: int = 72,
) -> tuple[Invitation, str]:
    normalized = _normalize_email(email)
    if role not in VALID_ROLES:
        raise ValidationError(f"Unsupported role {role!r}.")
    if role == "owner":
        raise ValidationError("Cannot invite directly as owner; use ownership transfer.")

    raw_token = generate_opaque_token("inv", nbytes=24)
    invite = Invitation(
        id=f"inv_{secrets.token_hex(8)}",
        tenant_id=tenant_id,
        email=normalized,
        role=role,
        invited_by_user_id=actor_user_id,
        token_hash=hash_token(raw_token),
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(hours=expires_hours),
    )
    session.add(invite)
    record_audit_event(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="member.invited",
        target_type="invitation",
        target_id=invite.id,
        details={"email": normalized, "role": role},
    )
    session.flush()
    return invite, raw_token


def accept_invitation(
    session: Session,
    *,
    user: User,
    user_session: UserSession | None,
    token: str,
) -> tuple[Tenant, Membership]:
    with system_routing_scope(session):
        invite = session.scalar(select(Invitation).where(Invitation.token_hash == hash_token(token)))
    if invite is None:
        raise NotFoundError("Invitation not found.")
    set_session_tenant(session, invite.tenant_id)
    if invite.status != "pending":
        raise ConflictError(f"Invitation is already {invite.status}.")
    if _as_utc(invite.expires_at) < datetime.now(UTC):
        invite.status = "expired"
        session.flush()
        raise ValidationError("Invitation has expired.")
    if invite.email.lower() != user.email.lower():
        raise ForbiddenError("Invitation email does not match signed-in account.")

    tenant = session.get(Tenant, invite.tenant_id)
    if tenant is None or tenant.status != "active":
        raise NotFoundError("Target organization is unavailable.")

    membership = session.scalar(
        select(Membership).where(
            Membership.tenant_id == invite.tenant_id,
            Membership.user_id == user.id,
        )
    )
    if membership is None:
        membership = Membership(
            id=f"mem_{secrets.token_hex(8)}",
            tenant_id=invite.tenant_id,
            user_id=user.id,
            role=invite.role,
            status="active",
        )
        session.add(membership)
    else:
        membership.role = invite.role
        membership.status = "active"
        membership.revoked_at = None

    invite.status = "accepted"
    if user_session is not None:
        user_session.active_tenant_id = tenant.id
    set_session_tenant(session, tenant.id)
    record_audit_event(
        session,
        tenant_id=tenant.id,
        actor_user_id=user.id,
        action="member.invitation_accepted",
        target_type="membership",
        target_id=membership.id,
        details={"role": membership.role},
    )
    session.flush()
    return tenant, membership


def revoke_invitation(
    session: Session,
    *,
    tenant_id: str,
    actor_user_id: str,
    invitation_id: str,
) -> Invitation:
    invite = session.scalar(
        select(Invitation).where(Invitation.id == invitation_id, Invitation.tenant_id == tenant_id)
    )
    if invite is None:
        raise NotFoundError("Invitation not found.")
    if invite.status == "pending":
        invite.status = "revoked"
        record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="member.invitation_revoked",
            target_type="invitation",
            target_id=invite.id,
        )
    session.flush()
    return invite


def list_members(session: Session, *, tenant_id: str) -> list[dict[str, Any]]:
    rows = session.execute(
        select(Membership, User)
        .join(User, User.id == Membership.user_id)
        .where(Membership.tenant_id == tenant_id)
        .order_by(Membership.created_at.asc())
    ).all()
    return [{"membership": m, "user": u} for m, u in rows]


def update_member_role(
    session: Session,
    *,
    tenant_id: str,
    actor_user_id: str,
    target_user_id: str,
    new_role: str,
) -> Membership:
    if new_role not in VALID_ROLES:
        raise ValidationError(f"Unsupported role {new_role!r}.")
    if new_role == "owner":
        raise ValidationError("Use explicit ownership transfer endpoint to assign owner.")
    membership = session.scalar(
        select(Membership).where(
            Membership.tenant_id == tenant_id,
            Membership.user_id == target_user_id,
            Membership.status == "active",
        )
    )
    if membership is None:
        raise NotFoundError("Active membership not found.")
    if membership.role == "owner":
        raise ConflictError("Cannot demote owner via role change; transfer ownership first.")

    old_role = membership.role
    membership.role = new_role
    record_audit_event(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="member.role_updated",
        target_type="membership",
        target_id=membership.id,
        details={"target_user_id": target_user_id, "old_role": old_role, "new_role": new_role},
    )
    session.flush()
    return membership


def remove_or_leave_member(
    session: Session,
    *,
    tenant_id: str,
    actor_user_id: str,
    target_user_id: str,
) -> Membership:
    membership = session.scalar(
        select(Membership).where(
            Membership.tenant_id == tenant_id,
            Membership.user_id == target_user_id,
            Membership.status == "active",
        )
    )
    if membership is None:
        raise NotFoundError("Active membership not found.")

    if membership.role == "owner":
        other_owners = list(
            session.scalars(
                select(Membership).where(
                    Membership.tenant_id == tenant_id,
                    Membership.role == "owner",
                    Membership.status == "active",
                    Membership.user_id != target_user_id,
                )
            )
        )
        if not other_owners:
            raise ConflictError("Cannot remove the last active owner; transfer ownership first.")

    membership.status = "revoked"
    membership.revoked_at = datetime.now(UTC)

    # Clear active tenant or revoke sessions actively pointing to this tenant.
    now = datetime.now(UTC)
    session.execute(
        update(UserSession)
        .where(
            UserSession.user_id == target_user_id,
            UserSession.active_tenant_id == tenant_id,
            UserSession.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    record_audit_event(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="member.removed" if actor_user_id != target_user_id else "member.left",
        target_type="membership",
        target_id=membership.id,
        details={"target_user_id": target_user_id},
    )
    session.flush()
    return membership


def transfer_ownership(
    session: Session,
    *,
    tenant_id: str,
    current_owner_id: str,
    new_owner_id: str,
) -> tuple[Membership, Membership]:
    if current_owner_id == new_owner_id:
        raise ValidationError("Target user is already the owner.")
    current_owner = session.scalar(
        select(Membership).where(
            Membership.tenant_id == tenant_id,
            Membership.user_id == current_owner_id,
            Membership.status == "active",
            Membership.role == "owner",
        )
    )
    if current_owner is None:
        raise ForbiddenError("Only the active organization owner can transfer ownership.")

    target = session.scalar(
        select(Membership).where(
            Membership.tenant_id == tenant_id,
            Membership.user_id == new_owner_id,
            Membership.status == "active",
        )
    )
    if target is None:
        raise NotFoundError("Target user must be an active member before receiving ownership.")

    current_owner.role = "admin"
    target.role = "owner"
    record_audit_event(
        session,
        tenant_id=tenant_id,
        actor_user_id=current_owner_id,
        action="org.ownership_transferred",
        target_type="tenant",
        target_id=tenant_id,
        details={"from_user_id": current_owner_id, "to_user_id": new_owner_id},
    )
    session.flush()
    return current_owner, target
