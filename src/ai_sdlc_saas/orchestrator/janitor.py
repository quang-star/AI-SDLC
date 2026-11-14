"""Sandbox resource tracking ledger and independent orphan cleanup / janitor sweep."""

from __future__ import annotations

import os
import secrets
import shutil
import stat
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_sdlc_saas.db.engine import system_routing_scope
from ai_sdlc_saas.db.models import CleanupJob, SandboxResource, Task
from ai_sdlc_saas.governance.observability import record_cleanup_metric

ResourceDeleter = Callable[[SandboxResource], None]


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _handle_rmtree_readonly(func: Any, path: str, _exc: Any) -> None:
    """Clear read-only bits (e.g. Windows .git/objects/pack files) before retrying unlink/rmdir."""
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass
    func(path)


def default_resource_deleter(res: SandboxResource) -> None:
    """Default local worktree/preview deleter; remote providers override via registered hook."""
    if res.provider in ("local_worktree", "local_preview") and res.external_resource_id:
        p = Path(res.external_resource_id)
        if p.exists():
            shutil.rmtree(p, ignore_errors=False, onexc=_handle_rmtree_readonly)


def register_sandbox_resource(
    session: Session,
    *,
    tenant_id: str,
    task_id: str,
    run_id: str,
    resource_type: str,
    provider: str,
    external_resource_id: str,
    idempotency_key: str,
    ttl_seconds: int = 1800,
    metadata: dict[str, Any] | None = None,
    status: str = "active",
) -> SandboxResource:
    """Record a sandbox resource BEFORE/AROUND external provisioning with TTL + idempotency."""
    existing = session.scalar(
        select(SandboxResource).where(
            SandboxResource.tenant_id == tenant_id,
            SandboxResource.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        return existing

    res = SandboxResource(
        id=f"sbx_{secrets.token_hex(8)}",
        tenant_id=tenant_id,
        task_id=task_id,
        run_id=run_id,
        resource_type=resource_type,
        provider=provider,
        external_resource_id=external_resource_id,
        idempotency_key=idempotency_key,
        status=status,
        ttl_seconds=ttl_seconds,
        expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
        metadata_json=dict(metadata or {}),
    )
    session.add(res)
    session.flush()
    return res


def mark_sandbox_active(
    session: Session,
    *,
    tenant_id: str,
    resource_id: str,
) -> SandboxResource | None:
    """Promote a `creating` SandboxResource to `active` once backing creation succeeds."""
    res = session.scalar(
        select(SandboxResource).where(
            SandboxResource.id == resource_id,
            SandboxResource.tenant_id == tenant_id,
        )
    )
    if res is not None:
        res.status = "active"
        session.flush()
    return res


def cleanup_resource(
    session: Session,
    *,
    resource: SandboxResource,
    deleter: ResourceDeleter | None = None,
) -> bool:
    """Delete one tracked resource; record `CleanupJob` failure rather than silently marking clean."""
    fn = deleter or default_resource_deleter
    resource.status = "cleaning"
    session.flush()
    try:
        fn(resource)
        resource.status = "deleted"
        resource.deleted_at = datetime.now(UTC)
        # Complete any open CleanupJob rows for this resource
        for cj in session.scalars(
            select(CleanupJob).where(
                CleanupJob.tenant_id == resource.tenant_id,
                CleanupJob.resource_id == resource.id,
                CleanupJob.status != "completed",
            )
        ):
            cj.status = "completed"
            cj.finished_at = datetime.now(UTC)
        record_cleanup_metric(outcome="deleted")
        session.flush()
        return True
    except Exception as exc:
        resource.status = "failed_cleanup"
        record_cleanup_metric(outcome="failed")
        existing_cj = session.scalar(
            select(CleanupJob).where(
                CleanupJob.tenant_id == resource.tenant_id,
                CleanupJob.resource_id == resource.id,
            )
        )
        if existing_cj is None:
            cj = CleanupJob(
                id=f"cln_{secrets.token_hex(8)}",
                tenant_id=resource.tenant_id,
                resource_id=resource.id,
                status="pending",
                attempts=1,
                last_error=str(exc)[:1000],
            )
            session.add(cj)
        else:
            existing_cj.status = "pending"
            existing_cj.attempts += 1
            existing_cj.last_error = str(exc)[:1000]
        session.flush()
        return False


def cleanup_task_resources(
    session: Session,
    *,
    tenant_id: str,
    task_id: str,
    deleter: ResourceDeleter | None = None,
) -> str:
    """Cleanup every non-deleted resource for `task_id` and update `Task.cleanup_status`."""
    resources = list(
        session.scalars(
            select(SandboxResource).where(
                SandboxResource.tenant_id == tenant_id,
                SandboxResource.task_id == task_id,
                SandboxResource.status != "deleted",
            )
        )
    )
    all_ok = True
    for res in resources:
        if not cleanup_resource(session, resource=res, deleter=deleter):
            all_ok = False

    task = session.scalar(select(Task).where(Task.id == task_id, Task.tenant_id == tenant_id))
    new_status = "CLEANED" if all_ok else "FAILED"
    if task is not None:
        task.cleanup_status = new_status
    session.flush()
    return new_status


def janitor_sweep(
    session: Session,
    *,
    now: datetime | None = None,
    stale_creating_seconds: int = 300,
    deleter: ResourceDeleter | None = None,
) -> dict[str, Any]:
    """Sweep expired TTL, stale `creating`, cancelled-task, or previously failed_cleanup resources."""
    with system_routing_scope(session):
        current = now or datetime.now(UTC)
        resources = list(
            session.scalars(
                select(SandboxResource).where(SandboxResource.status != "deleted")
            )
        )
        swept_ok = 0
        swept_failed = 0
        touched_tasks: set[tuple[str, str]] = set()

        for res in resources:
            task = session.get(Task, res.task_id)
            task_terminal = task is not None and task.status in ("CANCELLED", "FAILED", "DELIVERED", "DEAD_LETTER")
            expired = _as_utc(res.expires_at) <= current
            stale_create = res.status == "creating" and (
                current - _as_utc(res.created_at)
            ) >= timedelta(seconds=stale_creating_seconds)
            needs_retry = res.status == "failed_cleanup"
            if expired or stale_create or task_terminal or needs_retry:
                touched_tasks.add((res.tenant_id, res.task_id))
                if cleanup_resource(session, resource=res, deleter=deleter):
                    swept_ok += 1
                else:
                    swept_failed += 1

        for tid, task_id in touched_tasks:
            task = session.get(Task, task_id)
            if task is None or task.tenant_id != tid:
                continue
            remaining_bad = session.scalar(
                select(SandboxResource.id).where(
                    SandboxResource.tenant_id == tid,
                    SandboxResource.task_id == task_id,
                    SandboxResource.status != "deleted",
                )
            )
            task.cleanup_status = "CLEANED" if remaining_bad is None else "FAILED"

        session.flush()
        return {
            "checked": len(resources),
            "cleaned": swept_ok,
            "failed_cleanup": swept_failed,
        }
