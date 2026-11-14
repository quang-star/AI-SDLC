"""Scoped time-limited operator support access grants and audited read-only diagnostics."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ai_sdlc_saas.db.engine import set_session_tenant
from ai_sdlc_saas.db.models import (
    AuditEvent,
    Job,
    Project,
    PullRequestRecord,
    Repository,
    SandboxResource,
    Subscription,
    SupportAccessGrant,
    Task,
    TaskRun,
    Tenant,
    User,
)
from ai_sdlc_saas.errors import ForbiddenError, NotFoundError, ValidationError
from ai_sdlc_saas.governance.audit import record_audit_event


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def grant_support_access(
    session: Session,
    *,
    tenant_id: str,
    granted_by_user_id: str,
    operator_user_id: str,
    ticket_id: str,
    reason: str,
    ttl_minutes: int = 60,
) -> SupportAccessGrant:
    """Create a time-limited support grant allowing a platform operator to inspect one tenant."""
    if ttl_minutes < 1 or ttl_minutes > 1440:
        raise ValidationError("ttl_minutes must be between 1 and 1440.")
    clean_ticket = ticket_id.strip()
    clean_reason = reason.strip()
    if not clean_ticket or not clean_reason:
        raise ValidationError("ticket_id and reason are required for support access.")

    tenant = session.get(Tenant, tenant_id)
    if tenant is None or tenant.status == "deleted":
        raise NotFoundError(f"Tenant {tenant_id} not found.")

    operator = session.get(User, operator_user_id)
    if operator is None:
        raise NotFoundError(f"User {operator_user_id} not found.")
    if not operator.is_platform_operator:
        raise ForbiddenError("Target user is not designated as a platform operator.")

    now = datetime.now(UTC)
    grant = SupportAccessGrant(
        id=f"sup_{secrets.token_hex(8)}",
        tenant_id=tenant_id,
        operator_user_id=operator_user_id,
        ticket_id=clean_ticket,
        reason=clean_reason,
        expires_at=now + timedelta(minutes=ttl_minutes),
        created_at=now,
    )
    session.add(grant)
    record_audit_event(
        session,
        tenant_id=tenant_id,
        actor_user_id=granted_by_user_id,
        action="support.access_granted",
        target_type="support_access_grant",
        target_id=grant.id,
        details={
            "operator_user_id": operator_user_id,
            "ticket_id": clean_ticket,
            "ttl_minutes": ttl_minutes,
        },
    )
    session.flush()
    return grant


def revoke_support_access(
    session: Session,
    *,
    tenant_id: str,
    actor_user_id: str,
    grant_id: str,
) -> SupportAccessGrant:
    """Revoke an existing support grant immediately."""
    grant = session.scalar(
        select(SupportAccessGrant).where(
            SupportAccessGrant.id == grant_id,
            SupportAccessGrant.tenant_id == tenant_id,
        )
    )
    if grant is None:
        raise NotFoundError(f"SupportAccessGrant {grant_id} not found.")
    if grant.revoked_at is None:
        grant.revoked_at = datetime.now(UTC)
        record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="support.access_revoked",
            target_type="support_access_grant",
            target_id=grant.id,
            details={"operator_user_id": grant.operator_user_id, "ticket_id": grant.ticket_id},
        )
        session.flush()
    return grant


def list_support_grants(session: Session, *, tenant_id: str) -> list[SupportAccessGrant]:
    """List support access grants scoped strictly to tenant_id."""
    return list(
        session.scalars(
            select(SupportAccessGrant)
            .where(SupportAccessGrant.tenant_id == tenant_id)
            .order_by(SupportAccessGrant.created_at.desc())
        )
    )


def get_active_support_grant(
    session: Session,
    *,
    tenant_id: str,
    operator_user_id: str,
    now: datetime | None = None,
) -> SupportAccessGrant:
    """Verify the operator holds an active, non-expired, unrevoked grant for `tenant_id`."""
    current = now or datetime.now(UTC)
    grants = list(
        session.scalars(
            select(SupportAccessGrant)
            .where(
                SupportAccessGrant.tenant_id == tenant_id,
                SupportAccessGrant.operator_user_id == operator_user_id,
                SupportAccessGrant.revoked_at.is_(None),
            )
            .order_by(SupportAccessGrant.created_at.desc())
        )
    )
    for g in grants:
        if _as_utc(g.expires_at) > _as_utc(current):
            return g
    raise ForbiddenError(f"Operator lacks an active, unexpired SupportAccessGrant for tenant {tenant_id!r}.")


def inspect_tenant_for_support(
    session: Session,
    *,
    tenant_id: str,
    operator: User,
) -> dict[str, Any]:
    """Return read-only sanitized tenant diagnostics and enforce mandatory audit logging."""
    if not operator.is_platform_operator:
        raise ForbiddenError("Platform operator privileges are required.")

    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise NotFoundError(f"Tenant {tenant_id} not found.")

    grant = get_active_support_grant(
        session,
        tenant_id=tenant_id,
        operator_user_id=operator.id,
    )

    prev_tenant = session.info.get("tenant_id")
    set_session_tenant(session, tenant_id)
    try:
        sub = session.scalar(select(Subscription).where(Subscription.tenant_id == tenant_id))
        tasks = list(
            session.scalars(
                select(Task)
                .where(Task.tenant_id == tenant_id)
                .order_by(Task.created_at.desc())
                .limit(20)
            )
        )
        jobs = list(
            session.scalars(
                select(Job)
                .where(Job.tenant_id == tenant_id)
                .order_by(Job.created_at.desc())
                .limit(20)
            )
        )
        active_sandboxes = int(
            session.scalar(
                select(func.count())
                .select_from(SandboxResource)
                .where(SandboxResource.tenant_id == tenant_id, SandboxResource.status != "deleted")
            )
            or 0
        )
        projects_count = int(
            session.scalar(select(func.count()).select_from(Project).where(Project.tenant_id == tenant_id)) or 0
        )
        repos_count = int(
            session.scalar(select(func.count()).select_from(Repository).where(Repository.tenant_id == tenant_id)) or 0
        )
        prs_count = int(
            session.scalar(select(func.count()).select_from(PullRequestRecord).where(PullRequestRecord.tenant_id == tenant_id))
            or 0
        )
        recent_runs = list(
            session.scalars(
                select(TaskRun)
                .where(TaskRun.tenant_id == tenant_id)
                .order_by(TaskRun.created_at.desc())
                .limit(10)
            )
        )
        recent_audits = list(
            session.scalars(
                select(AuditEvent)
                .where(AuditEvent.tenant_id == tenant_id)
                .order_by(AuditEvent.created_at.desc())
                .limit(15)
            )
        )

        # Mandatory audit log every single time an operator inspects tenant state.
        record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_user_id=operator.id,
            action="support.tenant_inspected",
            target_type="tenant",
            target_id=tenant_id,
            details={"grant_id": grant.id, "ticket_id": grant.ticket_id},
        )
        session.flush()
    finally:
        set_session_tenant(session, prev_tenant)

    return {
        "grant": {
            "id": grant.id,
            "ticket_id": grant.ticket_id,
            "expires_at": _as_utc(grant.expires_at).isoformat(),
        },
        "tenant": {
            "id": tenant.id,
            "slug": tenant.slug,
            "name": tenant.name,
            "status": tenant.status,
            "data_region": tenant.data_region,
        },
        "subscription": (
            {
                "plan_id": sub.plan_id,
                "status": sub.status,
                "credit_balance_cents": sub.credit_balance_cents,
                "reserved_cents": sub.reserved_cents,
            }
            if sub is not None
            else None
        ),
        "counts": {
            "projects": projects_count,
            "repositories": repos_count,
            "pull_requests": prs_count,
            "active_sandboxes": active_sandboxes,
        },
        "recent_tasks": [
            {
                "id": t.id,
                "title": t.title,
                "status": t.status,
                "current_stage": t.current_stage,
                "cleanup_status": t.cleanup_status,
                "error_code": t.error_code,
            }
            for t in tasks
        ],
        "recent_runs": [
            {
                "id": r.id,
                "task_id": r.task_id,
                "status": r.status,
                "current_node": r.current_node,
                "verification_verdict": r.verification_verdict,
            }
            for r in recent_runs
        ],
        "recent_jobs": [
            {
                "id": j.id,
                "task_id": j.task_id,
                "job_type": j.job_type,
                "status": j.status,
                "attempts": j.attempts,
                "last_error": j.last_error,
            }
            for j in jobs
        ],
        "recent_audit_actions": [
            {"id": a.id, "action": a.action, "target_type": a.target_type, "target_id": a.target_id}
            for a in recent_audits
        ],
    }
