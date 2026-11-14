"""Organization, membership, invitation, and ownership transfer endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_sdlc_saas.api.deps import get_auth_context, get_db, get_runtime, require_permission
from ai_sdlc_saas.api.routers.auth import _to_tenant_summary
from ai_sdlc_saas.api.runtime import AppRuntime
from ai_sdlc_saas.api.schemas import (
    AcceptInvitationRequest,
    CreateInvitationRequest,
    CreateOrgRequest,
    InvitationResponse,
    MemberSummary,
    SwitchOrgRequest,
    TenantSummary,
    TransferOwnershipRequest,
    UpdateMemberRoleRequest,
    UpdateOrgRequest,
)
from ai_sdlc_saas.db.models import Invitation, Membership, Tenant
from ai_sdlc_saas.governance.audit import record_audit_event
from ai_sdlc_saas.identity.service import (
    AuthContext,
    accept_invitation,
    create_invitation,
    create_org,
    list_members,
    list_user_orgs,
    remove_or_leave_member,
    revoke_invitation,
    switch_active_org,
    transfer_ownership,
    update_member_role,
)

router = APIRouter(prefix="/api/v1/orgs", tags=["orgs"])


@router.get("", response_model=list[TenantSummary])
def api_list_orgs(
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db, scope="function"),
) -> list[TenantSummary]:
    return [
        _to_tenant_summary(row["tenant"], row["membership"])
        for row in list_user_orgs(db, user_id=ctx.user.id)
    ]


@router.post("", response_model=TenantSummary, status_code=201)
def api_create_org(
    body: CreateOrgRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db, scope="function"),
) -> TenantSummary:
    tenant, membership = create_org(
        db,
        ctx=ctx,
        name=body.name,
        slug=body.slug,
        data_region=body.data_region,
    )
    return _to_tenant_summary(tenant, membership)


@router.post("/switch", response_model=TenantSummary)
def api_switch_org(
    body: SwitchOrgRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db, scope="function"),
) -> TenantSummary:
    tenant, membership = switch_active_org(db, ctx=ctx, target_tenant_id=body.tenant_id)
    return _to_tenant_summary(tenant, membership)


@router.patch("/current", response_model=TenantSummary)
def api_update_current_org(
    body: UpdateOrgRequest,
    perm: tuple[AuthContext, Tenant, Membership] = Depends(require_permission("org:manage")),
    db: Session = Depends(get_db, scope="function"),
) -> TenantSummary:
    ctx, tenant, membership = perm
    changes: dict[str, object] = {}
    if body.name is not None:
        tenant.name = body.name.strip()
        changes["name"] = tenant.name
    if body.retention_days is not None:
        tenant.retention_days = body.retention_days
        changes["retention_days"] = tenant.retention_days
    if changes:
        record_audit_event(
            db,
            tenant_id=tenant.id,
            actor_user_id=ctx.user.id,
            action="org.updated",
            target_type="tenant",
            target_id=tenant.id,
            details=changes,
        )
    db.flush()
    return _to_tenant_summary(tenant, membership)


@router.get("/current/members", response_model=list[MemberSummary])
def api_list_members(
    perm: tuple[AuthContext, Tenant, Membership] = Depends(require_permission("members:read")),
    db: Session = Depends(get_db, scope="function"),
) -> list[MemberSummary]:
    _ctx, tenant, _mem = perm
    rows = list_members(db, tenant_id=tenant.id)
    return [
        MemberSummary(
            membership_id=r["membership"].id,
            user_id=r["user"].id,
            email=r["user"].email,
            full_name=r["user"].full_name,
            role=r["membership"].role,
            status=r["membership"].status,
            created_at=r["membership"].created_at,
        )
        for r in rows
    ]


@router.get("/current/invitations", response_model=list[InvitationResponse])
def api_list_invitations(
    perm: tuple[AuthContext, Tenant, Membership] = Depends(require_permission("members:read")),
    db: Session = Depends(get_db, scope="function"),
) -> list[InvitationResponse]:
    _ctx, tenant, _mem = perm
    invites = list(
        db.scalars(
            select(Invitation)
            .where(Invitation.tenant_id == tenant.id)
            .order_by(Invitation.created_at.desc())
        )
    )
    return [
        InvitationResponse(
            id=inv.id,
            email=inv.email,
            role=inv.role,
            status=inv.status,
            expires_at=inv.expires_at,
        )
        for inv in invites
    ]


@router.post("/current/invitations", response_model=InvitationResponse, status_code=201)
def api_create_invitation(
    body: CreateInvitationRequest,
    perm: tuple[AuthContext, Tenant, Membership] = Depends(require_permission("members:manage")),
    db: Session = Depends(get_db, scope="function"),
    runtime: AppRuntime = Depends(get_runtime),
) -> InvitationResponse:
    ctx, tenant, _mem = perm
    invite, raw_token = create_invitation(
        db,
        tenant_id=tenant.id,
        actor_user_id=ctx.user.id,
        email=body.email,
        role=body.role,
        expires_hours=body.expires_hours,
    )
    expose = runtime.settings.env in ("dev", "test", "local")
    return InvitationResponse(
        id=invite.id,
        email=invite.email,
        role=invite.role,
        status=invite.status,
        expires_at=invite.expires_at,
        invite_token=raw_token if expose else None,
    )


@router.post("/invitations/accept", response_model=TenantSummary)
def api_accept_invitation(
    body: AcceptInvitationRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db, scope="function"),
) -> TenantSummary:
    tenant, membership = accept_invitation(
        db,
        user=ctx.user,
        user_session=ctx.session,
        token=body.token,
    )
    return _to_tenant_summary(tenant, membership)


@router.delete("/current/invitations/{invitation_id}", response_model=InvitationResponse)
def api_revoke_invitation(
    invitation_id: str,
    perm: tuple[AuthContext, Tenant, Membership] = Depends(require_permission("members:manage")),
    db: Session = Depends(get_db, scope="function"),
) -> InvitationResponse:
    ctx, tenant, _mem = perm
    inv = revoke_invitation(
        db,
        tenant_id=tenant.id,
        actor_user_id=ctx.user.id,
        invitation_id=invitation_id,
    )
    return InvitationResponse(
        id=inv.id,
        email=inv.email,
        role=inv.role,
        status=inv.status,
        expires_at=inv.expires_at,
    )


@router.patch("/current/members/{target_user_id}/role", response_model=MemberSummary)
def api_update_member_role(
    target_user_id: str,
    body: UpdateMemberRoleRequest,
    perm: tuple[AuthContext, Tenant, Membership] = Depends(require_permission("members:manage")),
    db: Session = Depends(get_db, scope="function"),
) -> MemberSummary:
    ctx, tenant, _mem = perm
    updated = update_member_role(
        db,
        tenant_id=tenant.id,
        actor_user_id=ctx.user.id,
        target_user_id=target_user_id,
        new_role=body.role,
    )
    rows = list_members(db, tenant_id=tenant.id)
    row = next(r for r in rows if r["membership"].id == updated.id)
    return MemberSummary(
        membership_id=updated.id,
        user_id=row["user"].id,
        email=row["user"].email,
        full_name=row["user"].full_name,
        role=updated.role,
        status=updated.status,
        created_at=updated.created_at,
    )


@router.delete("/current/members/{target_user_id}")
def api_remove_or_leave_member(
    target_user_id: str,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, object]:
    if target_user_id == ctx.user.id:
        tenant, _ = ctx.require_tenant()
    else:
        tenant, _ = ctx.require_permission("members:manage")
    mem = remove_or_leave_member(
        db,
        tenant_id=tenant.id,
        actor_user_id=ctx.user.id,
        target_user_id=target_user_id,
    )
    return {"removed": True, "membership_id": mem.id, "status": mem.status}


@router.post("/current/transfer-ownership")
def api_transfer_ownership(
    body: TransferOwnershipRequest,
    perm: tuple[AuthContext, Tenant, Membership] = Depends(require_permission("org:transfer_owner")),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, str]:
    ctx, tenant, _mem = perm
    old_owner, new_owner = transfer_ownership(
        db,
        tenant_id=tenant.id,
        current_owner_id=ctx.user.id,
        new_owner_id=body.new_owner_user_id,
    )
    return {
        "previous_owner_user_id": old_owner.user_id,
        "new_owner_user_id": new_owner.user_id,
    }
