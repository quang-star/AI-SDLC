"""Authentication, password recovery, and session management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ai_sdlc_saas.api.deps import get_auth_context, get_db, get_runtime
from ai_sdlc_saas.api.runtime import AppRuntime
from ai_sdlc_saas.api.schemas import (
    AuthResponse,
    LoginRequest,
    MeResponse,
    PasswordResetConfirmRequest,
    PasswordResetRequest,
    RegisterRequest,
    SessionSummary,
    TenantSummary,
    UserSummary,
    VerifyEmailRequest,
)
from ai_sdlc_saas.db.models import Membership, Tenant, User
from ai_sdlc_saas.identity.service import (
    AuthContext,
    confirm_password_reset,
    list_sessions,
    list_user_orgs,
    login_user,
    logout_session,
    register_user,
    request_password_reset,
    revoke_session,
    verify_email,
)
from ai_sdlc_saas.security.rbac import list_permissions

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _to_user_summary(user: User) -> UserSummary:
    return UserSummary(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        is_verified=user.is_verified,
        is_platform_operator=user.is_platform_operator,
        created_at=user.created_at,
    )


def _to_tenant_summary(tenant: Tenant, membership: Membership | None = None) -> TenantSummary:
    return TenantSummary(
        id=tenant.id,
        slug=tenant.slug,
        name=tenant.name,
        data_region=tenant.data_region,
        status=tenant.status,
        retention_days=tenant.retention_days,
        role=membership.role if membership is not None else None,
    )


@router.post("/register", response_model=AuthResponse, status_code=201)
def api_register(
    body: RegisterRequest,
    request: Request,
    db: Session = Depends(get_db, scope="function"),
    runtime: AppRuntime = Depends(get_runtime),
) -> AuthResponse:
    runtime.rate_limiter.check(f"auth:register:{request.client.host if request.client else 'unknown'}", limit_override=30)
    res = register_user(
        db,
        email=body.email,
        password=body.password,
        full_name=body.full_name,
        org_name=body.org_name,
        org_slug=body.org_slug,
        data_region=body.data_region,
        jwt_secret=runtime.settings.jwt_secret,
        jwt_ttl_minutes=runtime.settings.jwt_ttl_minutes,
        user_agent=request.headers.get("User-Agent", ""),
    )
    expose_tokens = runtime.settings.env in ("development", "dev", "test", "local")
    return AuthResponse(
        access_token=res["access_token"],
        session_id=res["session"].id,
        user=_to_user_summary(res["user"]),
        active_org=_to_tenant_summary(res["tenant"], res["membership"]) if res["tenant"] else None,
        verification_token=res["verification_token"] if expose_tokens else None,
    )


@router.post("/verify-email")
def api_verify_email(
    body: VerifyEmailRequest,
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, object]:
    user = verify_email(db, token=body.token)
    return {"verified": True, "user_id": user.id}


@router.post("/login", response_model=AuthResponse)
def api_login(
    body: LoginRequest,
    request: Request,
    db: Session = Depends(get_db, scope="function"),
    runtime: AppRuntime = Depends(get_runtime),
) -> AuthResponse:
    runtime.rate_limiter.check(f"auth:login:{body.email.lower().strip()}", limit_override=40)
    res = login_user(
        db,
        email=body.email,
        password=body.password,
        jwt_secret=runtime.settings.jwt_secret,
        jwt_ttl_minutes=runtime.settings.jwt_ttl_minutes,
        org_id=body.org_id,
        user_agent=request.headers.get("User-Agent", ""),
    )
    return AuthResponse(
        access_token=res["access_token"],
        session_id=res["session"].id,
        user=_to_user_summary(res["user"]),
        active_org=_to_tenant_summary(res["tenant"], res["membership"]) if res["tenant"] else None,
    )


@router.post("/logout")
def api_logout(
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, bool]:
    logout_session(db, user_session=ctx.session)
    return {"logged_out": True}


@router.post("/password-reset/request")
def api_password_reset_request(
    body: PasswordResetRequest,
    db: Session = Depends(get_db, scope="function"),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict[str, object]:
    token = request_password_reset(db, email=body.email)
    payload: dict[str, object] = {"accepted": True}
    if token and runtime.settings.env in ("development", "dev", "test", "local"):
        payload["reset_token"] = token
    return payload


@router.post("/password-reset/confirm")
def api_password_reset_confirm(
    body: PasswordResetConfirmRequest,
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, object]:
    user = confirm_password_reset(db, token=body.token, new_password=body.new_password)
    return {"reset": True, "user_id": user.id}


@router.get("/me", response_model=MeResponse)
def api_me(
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db, scope="function"),
) -> MeResponse:
    orgs = [
        _to_tenant_summary(row["tenant"], row["membership"])
        for row in list_user_orgs(db, user_id=ctx.user.id)
    ]
    perms = list(list_permissions(ctx.membership.role)) if ctx.membership else []
    active_org = _to_tenant_summary(ctx.tenant, ctx.membership) if ctx.tenant else None
    return MeResponse(
        user=_to_user_summary(ctx.user),
        active_org=active_org,
        permissions=perms,
        orgs=orgs,
    )


@router.get("/sessions", response_model=list[SessionSummary])
def api_list_sessions(
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db, scope="function"),
) -> list[SessionSummary]:
    return [
        SessionSummary(
            id=s.id,
            active_tenant_id=s.active_tenant_id,
            user_agent=s.user_agent,
            revoked_at=s.revoked_at,
            expires_at=s.expires_at,
            created_at=s.created_at,
        )
        for s in list_sessions(db, user_id=ctx.user.id)
    ]


@router.delete("/sessions/{session_id}", response_model=SessionSummary)
def api_revoke_session(
    session_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db, scope="function"),
) -> SessionSummary:
    s = revoke_session(db, user_id=ctx.user.id, target_session_id=session_id)
    return SessionSummary(
        id=s.id,
        active_tenant_id=s.active_tenant_id,
        user_agent=s.user_agent,
        revoked_at=s.revoked_at,
        expires_at=s.expires_at,
        created_at=s.created_at,
    )
