"""Delivery, PR/CI feedback, private previews, traceability sign-off, release notes & promotion endpoints."""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ai_sdlc_saas.api.deps import get_db, get_runtime, require_permission
from ai_sdlc_saas.api.runtime import AppRuntime
from ai_sdlc_saas.db.engine import set_session_tenant
from ai_sdlc_saas.db.models import (
    Membership,
    NotificationRecord,
    PullRequestRecord,
    SpecChangeRequestRecord,
    Task,
    TaskRun,
    Tenant,
    TraceabilityEntryRecord,
    VerificationReportRecord,
)
from ai_sdlc_saas.delivery.preview import create_private_preview, destroy_preview, verify_preview_access
from ai_sdlc_saas.delivery.publisher import handle_pr_ci_feedback
from ai_sdlc_saas.delivery.release import generate_release_bundle, promote_release
from ai_sdlc_saas.errors import AuthError, NotFoundError
from ai_sdlc_saas.governance.audit import record_audit_event
from ai_sdlc_saas.identity.service import AuthContext, authenticate_jwt
from ai_sdlc_saas.verification.engine import confirm_manual_traceability_check

router = APIRouter(prefix="/api/v1", tags=["delivery"])


class CiFeedbackRequest(BaseModel):
    ci_status: str = Field(..., min_length=2, max_length=32)
    summary: str = Field(default="", max_length=2000)


class PreviewCreateRequest(BaseModel):
    ttl_seconds: int = Field(default=1800, ge=60, le=86400)


class ReleasePromoteRequest(BaseModel):
    comment: str = Field(default="", max_length=1000)


class ResolveScrRequest(BaseModel):
    resolution_note: str = Field(default="", max_length=1000)


@router.get("/tasks/{task_id}/prs")
def api_list_task_prs(
    task_id: str,
    perm: tuple[AuthContext, Tenant, Membership] = Depends(require_permission("task:read")),
    db: Session = Depends(get_db, scope="function"),
) -> list[dict[str, Any]]:
    _ctx, tenant, _mem = perm
    rows = list(
        db.scalars(
            select(PullRequestRecord)
            .where(PullRequestRecord.tenant_id == tenant.id, PullRequestRecord.task_id == task_id)
            .order_by(PullRequestRecord.created_at.desc())
        )
    )
    return [
        {
            "id": p.id,
            "task_id": p.task_id,
            "run_id": p.run_id,
            "repo_id": p.repo_id,
            "change_set_id": p.change_set_id,
            "branch_name": p.branch_name,
            "base_branch": p.base_branch,
            "base_commit_sha": p.base_commit_sha,
            "patch_sha256": p.patch_sha256,
            "external_pr_number": p.external_pr_number,
            "external_pr_url": p.external_pr_url,
            "status": p.status,
            "ci_status": p.ci_status,
            "release_notes": p.release_notes,
            "created_at": p.created_at.isoformat(),
        }
        for p in rows
    ]


@router.post("/prs/{pr_id}/ci-feedback")
def api_post_pr_ci_feedback(
    pr_id: str,
    body: CiFeedbackRequest,
    perm: tuple[AuthContext, Tenant, Membership] = Depends(require_permission("task:create")),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    ctx, tenant, _mem = perm
    return handle_pr_ci_feedback(
        db,
        tenant_id=tenant.id,
        pr_id=pr_id,
        ci_status=body.ci_status,
        summary=body.summary,
        actor_user_id=ctx.user.id,
    )


@router.post("/tasks/{task_id}/previews", status_code=201)
def api_create_preview(
    task_id: str,
    body: PreviewCreateRequest | None = None,
    perm: tuple[AuthContext, Tenant, Membership] = Depends(require_permission("task:create")),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    ctx, tenant, _mem = perm
    task = db.scalar(select(Task).where(Task.id == task_id, Task.tenant_id == tenant.id))
    if task is None:
        raise NotFoundError(f"Task {task_id} not found.")
    run_id = task.active_run_id
    if not run_id:
        latest = db.scalar(
            select(TaskRun)
            .where(TaskRun.tenant_id == tenant.id, TaskRun.task_id == task_id)
            .order_by(TaskRun.created_at.desc())
        )
        if latest is None:
            raise NotFoundError(f"TaskRun for task {task_id} not found.")
        run_id = latest.id
    ttl = body.ttl_seconds if body else 1800
    return create_private_preview(
        db,
        tenant_id=tenant.id,
        task_id=task.id,
        run_id=run_id,
        actor_user_id=ctx.user.id,
        ttl_seconds=ttl,
    )


@router.get("/previews/{preview_id}/view")
def api_view_preview(
    preview_id: str,
    request: Request,
    token: str | None = Query(default=None),
    x_preview_token: str | None = Header(default=None, alias="X-Preview-Token"),
    db: Session = Depends(get_db, scope="function"),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    member_tenant_id: str | None = None
    auth_header = request.headers.get("Authorization", "").strip()
    if auth_header.lower().startswith("bearer "):
        raw_jwt = auth_header[7:].strip()
        if not raw_jwt:
            raise AuthError("Empty bearer token.")
        auth_ctx = authenticate_jwt(db, token=raw_jwt, jwt_secret=runtime.settings.jwt_secret)
        tnt, _mem = auth_ctx.require_tenant()
        member_tenant_id = tnt.id
        set_session_tenant(db, None)

    row = verify_preview_access(
        db,
        preview_id=preview_id,
        preview_token=token or x_preview_token,
        member_tenant_id=member_tenant_id,
    )
    set_session_tenant(db, row.tenant_id)
    task = db.scalar(select(Task).where(Task.id == row.task_id, Task.tenant_id == row.tenant_id))
    prs = list(
        db.scalars(
            select(PullRequestRecord).where(
                PullRequestRecord.tenant_id == row.tenant_id,
                PullRequestRecord.task_id == row.task_id,
            )
        )
    )
    return {
        "preview_id": row.id,
        "tenant_id": row.tenant_id,
        "task_id": row.task_id,
        "run_id": row.run_id,
        "status": row.status,
        "expires_at": row.expires_at.isoformat(),
        "task_title": task.title if task else "",
        "task_status": task.status if task else "",
        "branches": [pr.branch_name for pr in prs],
    }


@router.delete("/previews/{preview_id}")
def api_destroy_preview(
    preview_id: str,
    perm: tuple[AuthContext, Tenant, Membership] = Depends(require_permission("task:cancel")),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    ctx, tenant, _mem = perm
    row = destroy_preview(db, tenant_id=tenant.id, preview_id=preview_id, actor_user_id=ctx.user.id)
    return {"preview_id": row.id, "status": row.status}


@router.get("/tasks/{task_id}/release-notes")
def api_get_release_notes(
    task_id: str,
    perm: tuple[AuthContext, Tenant, Membership] = Depends(require_permission("task:read")),
    db: Session = Depends(get_db, scope="function"),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    _ctx, tenant, _mem = perm
    return generate_release_bundle(
        db,
        tenant_id=tenant.id,
        task_id=task_id,
        object_store=runtime.object_store,
    )


@router.post("/tasks/{task_id}/release")
def api_promote_release(
    task_id: str,
    body: ReleasePromoteRequest | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    perm: tuple[AuthContext, Tenant, Membership] = Depends(require_permission("release:approve")),
    db: Session = Depends(get_db, scope="function"),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    ctx, tenant, membership = perm
    key = (idempotency_key or "").strip() or f"rel_{secrets.token_hex(8)}"
    return promote_release(
        db,
        tenant_id=tenant.id,
        actor_user_id=ctx.user.id,
        actor_role=membership.role,
        task_id=task_id,
        idempotency_key=key,
        object_store=runtime.object_store,
        comment=body.comment if body else "",
    )


@router.get("/notifications")
def api_list_notifications(
    perm: tuple[AuthContext, Tenant, Membership] = Depends(require_permission("task:read")),
    db: Session = Depends(get_db, scope="function"),
) -> list[dict[str, Any]]:
    ctx, tenant, _mem = perm
    rows = list(
        db.scalars(
            select(NotificationRecord)
            .where(
                NotificationRecord.tenant_id == tenant.id,
                or_(NotificationRecord.user_id == ctx.user.id, NotificationRecord.user_id.is_(None)),
            )
            .order_by(NotificationRecord.created_at.desc())
            .limit(100)
        )
    )
    return [
        {
            "id": n.id,
            "channel": n.channel,
            "event_type": n.event_type,
            "title": n.title,
            "body": n.body,
            "is_read": n.is_read,
            "created_at": n.created_at.isoformat(),
        }
        for n in rows
    ]


@router.post("/notifications/{notification_id}/read")
def api_mark_notification_read(
    notification_id: str,
    perm: tuple[AuthContext, Tenant, Membership] = Depends(require_permission("task:read")),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    _ctx, tenant, _mem = perm
    row = db.scalar(
        select(NotificationRecord).where(
            NotificationRecord.id == notification_id,
            NotificationRecord.tenant_id == tenant.id,
        )
    )
    if row is None:
        raise NotFoundError(f"NotificationRecord {notification_id} not found.")
    row.is_read = True
    db.flush()
    return {"id": row.id, "is_read": row.is_read}


@router.get("/tasks/{task_id}/verification")
def api_get_task_verification(
    task_id: str,
    perm: tuple[AuthContext, Tenant, Membership] = Depends(require_permission("task:read")),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    _ctx, tenant, _mem = perm
    reports = list(
        db.scalars(
            select(VerificationReportRecord)
            .where(VerificationReportRecord.tenant_id == tenant.id, VerificationReportRecord.task_id == task_id)
            .order_by(VerificationReportRecord.created_at.desc())
        )
    )
    out_reports: list[dict[str, Any]] = []
    for rep in reports:
        entries = list(
            db.scalars(
                select(TraceabilityEntryRecord).where(
                    TraceabilityEntryRecord.tenant_id == tenant.id,
                    TraceabilityEntryRecord.report_id == rep.id,
                )
            )
        )
        out_reports.append(
            {
                "id": rep.id,
                "task_id": rep.task_id,
                "run_id": rep.run_id,
                "repo_id": rep.repo_id,
                "patch_sha256": rep.patch_sha256,
                "report_sha256": rep.report_sha256,
                "verdict": rep.verdict,
                "payload": rep.payload_json,
                "created_at": rep.created_at.isoformat(),
                "traceability": [
                    {
                        "id": e.id,
                        "ac_id": e.ac_id,
                        "status": e.status,
                        "linked_test_cases": e.linked_test_cases_json,
                        "observed_test_ids": e.observed_test_ids_json,
                        "manual_confirmed_by": e.manual_confirmed_by,
                    }
                    for e in entries
                ],
            }
        )
    return {"task_id": task_id, "reports": out_reports}


@router.post("/verification-reports/{report_id}/traceability/{ac_id}/confirm")
def api_confirm_manual_traceability(
    report_id: str,
    ac_id: str,
    perm: tuple[AuthContext, Tenant, Membership] = Depends(require_permission("delivery:approve")),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    ctx, tenant, _mem = perm
    rep = confirm_manual_traceability_check(
        db,
        tenant_id=tenant.id,
        actor_user_id=ctx.user.id,
        report_id=report_id,
        ac_id=ac_id,
    )
    return {
        "report_id": rep.id,
        "ac_id": ac_id,
        "verdict": rep.verdict,
    }


@router.get("/tasks/{task_id}/scrs")
def api_list_task_scrs(
    task_id: str,
    perm: tuple[AuthContext, Tenant, Membership] = Depends(require_permission("task:read")),
    db: Session = Depends(get_db, scope="function"),
) -> list[dict[str, Any]]:
    _ctx, tenant, _mem = perm
    rows = list(
        db.scalars(
            select(SpecChangeRequestRecord)
            .where(SpecChangeRequestRecord.tenant_id == tenant.id, SpecChangeRequestRecord.task_id == task_id)
            .order_by(SpecChangeRequestRecord.created_at.desc())
        )
    )
    return [
        {
            "id": r.id,
            "task_id": r.task_id,
            "run_id": r.run_id,
            "spec_sha256": r.spec_sha256,
            "raised_by_role": r.raised_by_role,
            "reason": r.reason,
            "payload": r.payload_json,
            "status": r.status,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.post("/tasks/{task_id}/scrs/{scr_id}/resolve")
def api_resolve_task_scr(
    task_id: str,
    scr_id: str,
    body: ResolveScrRequest | None = None,
    perm: tuple[AuthContext, Tenant, Membership] = Depends(require_permission("spec:approve")),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    ctx, tenant, _mem = perm
    row = db.scalar(
        select(SpecChangeRequestRecord).where(
            SpecChangeRequestRecord.id == scr_id,
            SpecChangeRequestRecord.task_id == task_id,
            SpecChangeRequestRecord.tenant_id == tenant.id,
        )
    )
    if row is None:
        raise NotFoundError(f"SpecChangeRequestRecord {scr_id} not found.")
    row.status = "resolved"
    record_audit_event(
        db,
        tenant_id=tenant.id,
        actor_user_id=ctx.user.id,
        action="spec_change_request.resolved",
        target_type="spec_change_request",
        target_id=row.id,
        details={"task_id": task_id, "resolution_note": body.resolution_note if body else ""},
    )
    db.flush()
    return {"id": row.id, "task_id": task_id, "status": row.status}
