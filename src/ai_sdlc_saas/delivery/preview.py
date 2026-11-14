"""Private authenticated ephemeral preview environments with janitor TTL cleanup."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_sdlc_saas.db.engine import system_routing_scope
from ai_sdlc_saas.db.models import PreviewEnvironment, Task, TaskRun
from ai_sdlc_saas.errors import AuthError, ForbiddenError, NotFoundError
from ai_sdlc_saas.governance.audit import record_audit_event
from ai_sdlc_saas.orchestrator.janitor import mark_sandbox_active, register_sandbox_resource
from ai_sdlc_saas.security.crypto import hash_token


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def create_private_preview(
    session: Session,
    *,
    tenant_id: str,
    task_id: str,
    run_id: str,
    actor_user_id: str,
    ttl_seconds: int = 1800,
) -> dict[str, Any]:
    """Create a private signed ephemeral preview environment tracked for TTL cleanup."""
    task = session.scalar(select(Task).where(Task.id == task_id, Task.tenant_id == tenant_id))
    run = session.scalar(select(TaskRun).where(TaskRun.id == run_id, TaskRun.tenant_id == tenant_id))
    if task is None or run is None:
        raise NotFoundError("Task or run not found for preview creation.")

    ttl = max(60, min(int(ttl_seconds), 86400))
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=ttl)
    preview_id = f"prv_{secrets.token_hex(8)}"
    raw_token = f"pv_{secrets.token_urlsafe(24)}"

    preview_url = f"/api/v1/previews/{preview_id}/view"
    row = PreviewEnvironment(
        id=preview_id,
        tenant_id=tenant_id,
        task_id=task.id,
        run_id=run.id,
        access_token_hash=hash_token(raw_token),
        preview_url=preview_url,
        status="running",
        expires_at=expires_at,
    )
    session.add(row)

    res = register_sandbox_resource(
        session,
        tenant_id=tenant_id,
        task_id=task.id,
        run_id=run.id,
        resource_type="preview",
        provider="preview",
        external_resource_id=preview_id,
        idempotency_key=f"preview:{preview_id}",
        ttl_seconds=ttl,
        metadata={"preview_url": preview_url},
    )
    mark_sandbox_active(session, tenant_id=tenant_id, resource_id=res.id)

    record_audit_event(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="preview.created",
        target_type="preview",
        target_id=preview_id,
        details={"task_id": task.id, "expires_at": expires_at.isoformat()},
    )
    session.flush()
    return {
        "preview_id": row.id,
        "task_id": row.task_id,
        "run_id": row.run_id,
        "preview_url": row.preview_url,
        "preview_token": raw_token,
        "status": row.status,
        "expires_at": row.expires_at.isoformat(),
    }


def verify_preview_access(
    session: Session,
    *,
    preview_id: str,
    preview_token: str | None = None,
    member_tenant_id: str | None = None,
) -> PreviewEnvironment:
    """Require either a valid preview token OR verified authenticated tenant membership."""
    with system_routing_scope(session):
        row = session.scalar(select(PreviewEnvironment).where(PreviewEnvironment.id == preview_id))
        if row is None:
            raise NotFoundError("Preview environment not found.")
        if row.status != "running":
            raise ForbiddenError(f"Preview environment is {row.status!r} and no longer accessible.")
        if _as_utc(row.expires_at) <= datetime.now(UTC):
            row.status = "expired"
            session.flush()
            raise ForbiddenError("Preview environment TTL has expired.")

        if member_tenant_id is not None:
            if member_tenant_id != row.tenant_id:
                raise ForbiddenError("Cross-tenant preview access denied.")
            return row

        if not preview_token or hash_token(preview_token) != row.access_token_hash:
            raise AuthError("Valid preview token or authenticated tenant session is required.")
        return row


def destroy_preview(
    session: Session,
    *,
    tenant_id: str,
    preview_id: str,
    actor_user_id: str | None = None,
) -> PreviewEnvironment:
    row = session.scalar(
        select(PreviewEnvironment).where(
            PreviewEnvironment.id == preview_id,
            PreviewEnvironment.tenant_id == tenant_id,
        )
    )
    if row is None:
        raise NotFoundError("Preview environment not found.")
    row.status = "destroyed"
    record_audit_event(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="preview.destroyed",
        target_type="preview",
        target_id=row.id,
    )
    session.flush()
    return row
