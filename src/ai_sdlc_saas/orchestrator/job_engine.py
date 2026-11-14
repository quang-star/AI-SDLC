"""Durable DB-backed job queue, transactional outbox, worker leases, backoff retry, and DLQ."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session

from ai_sdlc_saas.db.engine import system_routing_scope
from ai_sdlc_saas.db.models import Job, OutboxEvent, Task, WorkerLease


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def enqueue_job(
    session: Session,
    *,
    tenant_id: str,
    task_id: str,
    run_id: str,
    job_type: str,
    payload: dict[str, Any],
    idempotency_key: str,
    max_attempts: int = 3,
    outbox_event_type: str | None = None,
) -> Job:
    """Insert a `Job` and matching transactional `OutboxEvent` idempotently."""
    existing = session.scalar(
        select(Job).where(Job.tenant_id == tenant_id, Job.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return existing

    job = Job(
        id=f"job_{secrets.token_hex(8)}",
        tenant_id=tenant_id,
        task_id=task_id,
        run_id=run_id,
        job_type=job_type,
        payload_json=dict(payload),
        idempotency_key=idempotency_key,
        status="queued",
        attempts=0,
        max_attempts=max_attempts,
        available_at=datetime.now(UTC),
    )
    session.add(job)
    if outbox_event_type:
        session.add(
            OutboxEvent(
                id=f"obx_{secrets.token_hex(8)}",
                tenant_id=tenant_id,
                aggregate_type="task",
                aggregate_id=task_id,
                event_type=outbox_event_type,
                payload_json={"job_id": job.id, "job_type": job_type, "run_id": run_id},
            )
        )
    session.flush()
    return job


def register_worker_heartbeat(session: Session, *, worker_id: str, hostname: str) -> WorkerLease:
    lease = session.get(WorkerLease, worker_id)
    now = datetime.now(UTC)
    if lease is None:
        lease = WorkerLease(worker_id=worker_id, hostname=hostname, status="alive", last_heartbeat_at=now)
        session.add(lease)
    else:
        lease.hostname = hostname
        lease.status = "alive"
        lease.last_heartbeat_at = now
    session.flush()
    return lease


def claim_next_job(
    session: Session,
    *,
    worker_id: str,
    lease_seconds: int = 60,
) -> Job | None:
    """Atomically claim the oldest ready `queued` job or expired `leased` job across tenants."""
    with system_routing_scope(session):
        now = datetime.now(UTC)
        candidates = list(
            session.scalars(
                select(Job)
                .where(Job.status.in_(("queued", "leased")))
                .order_by(Job.created_at.asc())
                .limit(50)
            )
        )
        for cand in candidates:
            if cand.status == "queued":
                if _as_utc(cand.available_at) > now:
                    continue
            elif cand.status == "leased":
                if cand.lease_expires_at is None or _as_utc(cand.lease_expires_at) > now:
                    continue
                if cand.attempts >= cand.max_attempts:
                    # Exhausted retries after worker crash -> dead letter cleanly
                    cand.status = "dead_letter"
                    cand.last_error = cand.last_error or "Worker lease expired and max_attempts exhausted."
                    task = session.get(Task, cand.task_id)
                    if task and task.status not in ("CANCELLED", "DELIVERED", "DEAD_LETTER"):
                        task.status = "DEAD_LETTER"
                        task.error_code = "dead_letter"
                        task.error_message = cand.last_error
                    session.flush()
                    continue

            new_expiry = now + timedelta(seconds=lease_seconds)
            stmt = (
                update(Job)
                .where(
                    Job.id == cand.id,
                    Job.status == cand.status,
                    Job.attempts == cand.attempts,
                )
                .values(
                    status="leased",
                    worker_id=worker_id,
                    attempts=cand.attempts + 1,
                    lease_expires_at=new_expiry,
                )
            )
            res = cast(CursorResult[Any], session.execute(stmt))
            if res.rowcount == 1:
                session.flush()
                session.expire(cand)
                return session.get(Job, cand.id)
        return None


def renew_job_lease(session: Session, *, job_id: str, worker_id: str, lease_seconds: int = 60) -> bool:
    with system_routing_scope(session):
        res = cast(
            CursorResult[Any],
            session.execute(
                update(Job)
                .where(Job.id == job_id, Job.worker_id == worker_id, Job.status == "leased")
                .values(lease_expires_at=datetime.now(UTC) + timedelta(seconds=lease_seconds))
            ),
        )
        session.flush()
        return res.rowcount == 1


def complete_job(session: Session, *, job_id: str) -> None:
    with system_routing_scope(session):
        job = session.get(Job, job_id)
        if job is not None:
            job.status = "succeeded"
            job.lease_expires_at = None
            session.flush()


def fail_or_requeue_job(
    session: Session,
    *,
    job_id: str,
    error_message: str,
    base_backoff_seconds: int = 1,
) -> Job | None:
    with system_routing_scope(session):
        job = session.get(Job, job_id)
        if job is None:
            return None
        job.last_error = error_message[:2000]
        job.lease_expires_at = None
        if job.attempts < job.max_attempts:
            delay = base_backoff_seconds * (2 ** max(0, job.attempts - 1))
            job.status = "queued"
            job.worker_id = None
            job.available_at = datetime.now(UTC) + timedelta(seconds=delay)
        else:
            job.status = "dead_letter"
            task = session.get(Task, job.task_id)
            if task and task.status not in ("CANCELLED", "DELIVERED", "FAILED"):
                task.status = "DEAD_LETTER"
                task.error_code = "job_dead_letter"
                task.error_message = job.last_error
        session.flush()
        return job


def cancel_pending_jobs_for_task(session: Session, *, tenant_id: str, task_id: str) -> int:
    rows = list(
        session.scalars(
            select(Job).where(
                Job.tenant_id == tenant_id,
                Job.task_id == task_id,
                Job.status.in_(("queued", "leased")),
            )
        )
    )
    for r in rows:
        r.status = "cancelled"
        r.lease_expires_at = None
    session.flush()
    return len(rows)
