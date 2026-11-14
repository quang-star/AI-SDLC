"""Time-limited tenant-consented operator support access grants and audited inspection endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ai_sdlc_saas.api.deps import get_auth_context, get_db, require_permission
from ai_sdlc_saas.db.engine import set_session_tenant
from ai_sdlc_saas.db.models import Membership, Tenant
from ai_sdlc_saas.governance.support import (
    grant_support_access,
    inspect_tenant_for_support,
    list_support_grants,
    revoke_support_access,
)
from ai_sdlc_saas.identity.service import AuthContext

router = APIRouter(prefix="/api/v1", tags=["support"])


class GrantSupportRequest(BaseModel):
    operator_user_id: str = Field(..., min_length=3, max_length=64)
    ticket_id: str = Field(..., min_length=2, max_length=64)
    reason: str = Field(..., min_length=4, max_length=500)
    ttl_minutes: int = Field(default=60, ge=1, le=1440)


@router.post("/support/grants", status_code=201)
def api_grant_support_access(
    body: GrantSupportRequest,
    perm: tuple[AuthContext, Tenant, Membership] = Depends(require_permission("org:manage")),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    ctx, tenant, _mem = perm
    grant = grant_support_access(
        db,
        tenant_id=tenant.id,
        granted_by_user_id=ctx.user.id,
        operator_user_id=body.operator_user_id,
        ticket_id=body.ticket_id,
        reason=body.reason,
        ttl_minutes=body.ttl_minutes,
    )
    return {
        "id": grant.id,
        "tenant_id": grant.tenant_id,
        "operator_user_id": grant.operator_user_id,
        "ticket_id": grant.ticket_id,
        "reason": grant.reason,
        "expires_at": grant.expires_at.isoformat(),
        "revoked_at": grant.revoked_at.isoformat() if grant.revoked_at else None,
        "created_at": grant.created_at.isoformat(),
    }


@router.get("/support/grants")
def api_list_support_grants(
    perm: tuple[AuthContext, Tenant, Membership] = Depends(require_permission("org:manage")),
    db: Session = Depends(get_db, scope="function"),
) -> list[dict[str, Any]]:
    _ctx, tenant, _mem = perm
    grants = list_support_grants(db, tenant_id=tenant.id)
    return [
        {
            "id": g.id,
            "tenant_id": g.tenant_id,
            "operator_user_id": g.operator_user_id,
            "ticket_id": g.ticket_id,
            "reason": g.reason,
            "expires_at": g.expires_at.isoformat(),
            "revoked_at": g.revoked_at.isoformat() if g.revoked_at else None,
            "created_at": g.created_at.isoformat(),
        }
        for g in grants
    ]


@router.delete("/support/grants/{grant_id}")
def api_revoke_support_grant(
    grant_id: str,
    perm: tuple[AuthContext, Tenant, Membership] = Depends(require_permission("org:manage")),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    ctx, tenant, _mem = perm
    grant = revoke_support_access(
        db,
        tenant_id=tenant.id,
        actor_user_id=ctx.user.id,
        grant_id=grant_id,
    )
    return {
        "id": grant.id,
        "revoked_at": grant.revoked_at.isoformat() if grant.revoked_at else None,
    }


@router.get("/ops/tenants/{tenant_id}/inspect")
def api_ops_inspect_tenant(
    tenant_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    set_session_tenant(db, None)
    return inspect_tenant_for_support(
        db,
        tenant_id=tenant_id,
        operator=ctx.user,
    )
