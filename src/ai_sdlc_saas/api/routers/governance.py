"""Audit log explorer, org data export, org hard delete, kill switches, and Prometheus metrics."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_sdlc_saas.api.deps import get_auth_context, get_db, get_runtime, require_permission
from ai_sdlc_saas.api.runtime import AppRuntime
from ai_sdlc_saas.db.engine import set_session_tenant
from ai_sdlc_saas.db.models import AuditEvent, Membership, Tenant
from ai_sdlc_saas.errors import ForbiddenError
from ai_sdlc_saas.governance.lifecycle import (
    export_tenant_archive,
    get_export_archive_bytes,
    hard_delete_tenant,
    list_export_jobs,
    list_kill_switches,
    set_kill_switch,
)
from ai_sdlc_saas.governance.observability import render_metrics_text
from ai_sdlc_saas.identity.service import AuthContext

router = APIRouter(prefix="/api/v1", tags=["governance"])


class HardDeleteRequest(BaseModel):
    confirm_slug: str = Field(..., min_length=2, max_length=100)


class KillSwitchRequest(BaseModel):
    scope: str = Field(..., min_length=2, max_length=32)
    target_key: str = Field(..., min_length=1, max_length=128)
    is_active: bool
    reason: str = Field(default="", max_length=500)


@router.get("/audit-events")
def api_list_audit_events(
    limit: int = 100,
    perm: tuple[AuthContext, Tenant, Membership] = Depends(require_permission("audit:read")),
    db: Session = Depends(get_db, scope="function"),
) -> list[dict[str, Any]]:
    _ctx, tenant, _mem = perm
    bounded = max(1, min(limit, 500))
    rows = list(
        db.scalars(
            select(AuditEvent)
            .where(AuditEvent.tenant_id == tenant.id)
            .order_by(AuditEvent.created_at.desc())
            .limit(bounded)
        )
    )
    return [
        {
            "id": a.id,
            "actor_user_id": a.actor_user_id,
            "action": a.action,
            "target_type": a.target_type,
            "target_id": a.target_id,
            "details": a.details_json,
            "created_at": a.created_at.isoformat(),
        }
        for a in rows
    ]


@router.post("/org/exports", status_code=201)
def api_create_export(
    perm: tuple[AuthContext, Tenant, Membership] = Depends(require_permission("data:export")),
    db: Session = Depends(get_db, scope="function"),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    ctx, tenant, _mem = perm
    job = export_tenant_archive(
        db,
        object_store=runtime.object_store,
        tenant_id=tenant.id,
        requested_by_user_id=ctx.user.id,
    )
    return {
        "id": job.id,
        "status": job.status,
        "object_key": job.object_key,
        "created_at": job.created_at.isoformat(),
    }


@router.get("/org/exports")
def api_list_exports(
    perm: tuple[AuthContext, Tenant, Membership] = Depends(require_permission("data:export")),
    db: Session = Depends(get_db, scope="function"),
) -> list[dict[str, Any]]:
    _ctx, tenant, _mem = perm
    jobs = list_export_jobs(db, tenant_id=tenant.id)
    return [
        {
            "id": j.id,
            "requested_by_user_id": j.requested_by_user_id,
            "status": j.status,
            "object_key": j.object_key,
            "created_at": j.created_at.isoformat(),
        }
        for j in jobs
    ]


@router.get("/org/exports/{export_id}/download")
def api_download_export(
    export_id: str,
    perm: tuple[AuthContext, Tenant, Membership] = Depends(require_permission("data:export")),
    db: Session = Depends(get_db, scope="function"),
    runtime: AppRuntime = Depends(get_runtime),
) -> Response:
    _ctx, tenant, _mem = perm
    job, payload = get_export_archive_bytes(
        db,
        object_store=runtime.object_store,
        tenant_id=tenant.id,
        export_id=export_id,
    )
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{job.id}.zip"'},
    )


@router.post("/org/hard-delete")
def api_hard_delete_org(
    body: HardDeleteRequest,
    perm: tuple[AuthContext, Tenant, Membership] = Depends(require_permission("org:delete")),
    db: Session = Depends(get_db, scope="function"),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    ctx, tenant, _mem = perm
    ctx.require_permission("data:delete")
    set_session_tenant(db, tenant.id)
    retention = hard_delete_tenant(
        db,
        object_store=runtime.object_store,
        tenant_id=tenant.id,
        requested_by_user_id=ctx.user.id,
        confirm_slug=body.confirm_slug,
    )
    return {
        "retention_job_id": retention.id,
        "status": retention.status,
        "deleted_counts": retention.deleted_counts_json,
    }


@router.get("/ops/kill-switches")
def api_list_kill_switches(
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db, scope="function"),
) -> list[dict[str, Any]]:
    if not ctx.user.is_platform_operator:
        raise ForbiddenError("Platform operator privileges are required.")
    switches = list_kill_switches(db)
    return [
        {
            "id": s.id,
            "scope": s.scope,
            "target_key": s.target_key,
            "is_active": s.is_active,
            "reason": s.reason,
            "updated_by": s.updated_by,
            "updated_at": s.updated_at.isoformat(),
        }
        for s in switches
    ]


@router.post("/ops/kill-switches")
def api_set_kill_switch(
    body: KillSwitchRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    if not ctx.user.is_platform_operator:
        raise ForbiddenError("Platform operator privileges are required.")
    sw = set_kill_switch(
        db,
        scope=body.scope,
        target_key=body.target_key,
        is_active=body.is_active,
        reason=body.reason,
        updated_by=ctx.user.id,
    )
    return {
        "id": sw.id,
        "scope": sw.scope,
        "target_key": sw.target_key,
        "is_active": sw.is_active,
        "reason": sw.reason,
        "updated_by": sw.updated_by,
        "updated_at": sw.updated_at.isoformat(),
    }


@router.get("/metrics")
def api_metrics(db: Session = Depends(get_db, scope="function")) -> Response:
    set_session_tenant(db, None)
    payload, media_type = render_metrics_text(db)
    return Response(content=payload, media_type=media_type)
