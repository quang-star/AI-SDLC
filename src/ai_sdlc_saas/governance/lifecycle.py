"""Enterprise governance lifecycle: kill switches, trial guardrails, GDPR data export, and hard delete."""

from __future__ import annotations

import hashlib
import io
import json
import secrets
import zipfile
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from ai_sdlc_saas.db.engine import set_session_tenant
from ai_sdlc_saas.db.models import (
    Approval,
    ArtifactVersion,
    AuditEvent,
    Base,
    DataExportJob,
    KillSwitch,
    Membership,
    NotificationRecord,
    Plan,
    PreviewEnvironment,
    Project,
    PullRequestRecord,
    Repository,
    RetentionJob,
    ReviewFindingRecord,
    SpecChangeRequestRecord,
    Subscription,
    SupportAccessGrant,
    Task,
    TaskRun,
    Tenant,
    TenantMemory,
    TestPlanRecord,
    TraceabilityEntryRecord,
    UsageLedgerEntry,
    User,
    UserSession,
    VerificationReportRecord,
)
from ai_sdlc_saas.errors import NotFoundError, RateLimitError, ServiceUnavailableError, ValidationError
from ai_sdlc_saas.governance.audit import record_audit_event
from ai_sdlc_saas.storage.object_store import ObjectStore, build_artifact_key

_SENSITIVE_EXPORT_COLUMNS = frozenset(
    {
        "encrypted_blob",
        "secret_ciphertext",
        "ciphertext",
        "password_hash",
        "jti_hash",
        "verification_token_hash",
        "reset_token_hash",
        "preview_token_hash",
    }
)


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _serialize_row(obj: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in obj.__table__.columns:
        if col.name in _SENSITIVE_EXPORT_COLUMNS:
            continue
        val = getattr(obj, col.name)
        if isinstance(val, datetime):
            utc_val = _as_utc(val)
            out[col.name] = utc_val.isoformat() if utc_val is not None else None
        else:
            out[col.name] = val
    return out


# ---------------------------------------------------------------------------
# Kill Switches
# ---------------------------------------------------------------------------


def set_kill_switch(
    session: Session,
    *,
    scope: str,
    target_key: str,
    is_active: bool,
    reason: str = "",
    updated_by: str,
) -> KillSwitch:
    """Create or update an emergency kill switch."""
    clean_scope = scope.strip().lower()
    clean_target = target_key.strip()
    if clean_scope not in {"global", "tenant", "provider", "execution_mode"}:
        raise ValidationError(f"Unsupported kill switch scope {scope!r}.")
    if not clean_target:
        raise ValidationError("target_key is required.")

    existing = session.scalar(
        select(KillSwitch).where(
            KillSwitch.scope == clean_scope,
            KillSwitch.target_key == clean_target,
        )
    )
    now = datetime.now(UTC)
    if existing is None:
        existing = KillSwitch(
            id=f"ksw_{secrets.token_hex(8)}",
            scope=clean_scope,
            target_key=clean_target,
            is_active=is_active,
            reason=reason.strip(),
            updated_by=updated_by,
            updated_at=now,
        )
        session.add(existing)
    else:
        existing.is_active = is_active
        existing.reason = reason.strip()
        existing.updated_by = updated_by
        existing.updated_at = now
    session.flush()
    return existing


def list_kill_switches(session: Session, *, active_only: bool = False) -> list[KillSwitch]:
    """List emergency kill switches ordered by most recently updated."""
    stmt = select(KillSwitch).order_by(KillSwitch.updated_at.desc())
    if active_only:
        stmt = stmt.where(KillSwitch.is_active.is_(True))
    return list(session.scalars(stmt))


def assert_operation_allowed(
    session: Session,
    *,
    tenant_id: str | None = None,
    provider: str | None = None,
    execution_mode: str | None = None,
) -> None:
    """Raise ServiceUnavailableError if an active emergency kill switch blocks this action."""
    active = list_kill_switches(session, active_only=True)
    for sw in active:
        if sw.scope == "global" and sw.target_key in {"*", "all", "workers", "api"}:
            raise ServiceUnavailableError(
                f"Platform emergency kill switch active ({sw.target_key}): {sw.reason or 'suspended'}."
            )
        if tenant_id and sw.scope == "tenant" and sw.target_key == tenant_id:
            raise ServiceUnavailableError(
                f"Tenant execution disabled by kill switch: {sw.reason or 'suspended'}."
            )
        if provider and sw.scope == "provider" and sw.target_key == provider:
            raise ServiceUnavailableError(
                f"Model provider {provider!r} disabled by kill switch: {sw.reason or 'suspended'}."
            )
        if execution_mode and sw.scope == "execution_mode" and sw.target_key == execution_mode:
            raise ServiceUnavailableError(
                f"Execution mode {execution_mode!r} disabled by kill switch: {sw.reason or 'suspended'}."
            )


# ---------------------------------------------------------------------------
# Free / Trial Abuse Guardrails
# ---------------------------------------------------------------------------


def enforce_trial_guardrails(
    session: Session,
    *,
    tenant_id: str,
    actor_user_id: str | None = None,
    hourly_task_cap: int = 20,
) -> None:
    """Apply stricter concurrency and hourly creation caps to free / trialing tenants."""
    sub = session.scalar(select(Subscription).where(Subscription.tenant_id == tenant_id))
    if sub is None:
        return
    plan = session.get(Plan, sub.plan_id)
    is_free_or_trial = (plan is not None and plan.id in ("free", "trial")) or sub.status == "trialing"
    if not is_free_or_trial:
        return

    max_concurrent = min(plan.max_concurrent_tasks if plan else 1, 2)
    active_runs = int(
        session.scalar(
            select(func.count())
            .select_from(TaskRun)
            .where(
                TaskRun.tenant_id == tenant_id,
                TaskRun.status.notin_(["DONE", "FAILED", "CANCELLED", "VERIFICATION_FAILED", "SPEC_CHANGE_REQUIRED"]),
            )
        )
        or 0
    )
    if active_runs >= max_concurrent:
        record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="guardrail.trial_concurrency_throttled",
            target_type="tenant",
            target_id=tenant_id,
            details={"active_runs": active_runs, "max_concurrent": max_concurrent},
        )
        raise RateLimitError(
            f"Free/trial concurrency limit reached ({active_runs}/{max_concurrent} active runs)."
        )

    cutoff = datetime.now(UTC) - timedelta(hours=1)
    recent_tasks = int(
        session.scalar(
            select(func.count())
            .select_from(Task)
            .where(Task.tenant_id == tenant_id, Task.created_at >= cutoff)
        )
        or 0
    )
    if recent_tasks >= hourly_task_cap:
        record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="guardrail.trial_hourly_rate_throttled",
            target_type="tenant",
            target_id=tenant_id,
            details={"recent_tasks_1h": recent_tasks, "hourly_task_cap": hourly_task_cap},
        )
        raise RateLimitError(
            f"Free/trial hourly task creation limit reached ({recent_tasks}/{hourly_task_cap})."
        )


# ---------------------------------------------------------------------------
# Tenant Data Export (GDPR / Portability)
# ---------------------------------------------------------------------------


def export_tenant_archive(
    session: Session,
    *,
    object_store: ObjectStore,
    tenant_id: str,
    requested_by_user_id: str,
) -> DataExportJob:
    """Bundle all tenant-scoped non-secret records into a tenant-scoped ZIP archive."""
    tenant = session.get(Tenant, tenant_id)
    if tenant is None or tenant.status == "deleted":
        raise NotFoundError(f"Tenant {tenant_id} not found.")

    export_id = f"exp_{secrets.token_hex(8)}"
    export_tables: list[tuple[str, type[Any]]] = [
        ("projects", Project),
        ("repositories", Repository),
        ("tasks", Task),
        ("task_runs", TaskRun),
        ("artifact_versions", ArtifactVersion),
        ("approvals", Approval),
        ("spec_change_requests", SpecChangeRequestRecord),
        ("test_plans", TestPlanRecord),
        ("verification_reports", VerificationReportRecord),
        ("traceability_entries", TraceabilityEntryRecord),
        ("review_findings", ReviewFindingRecord),
        ("pull_requests", PullRequestRecord),
        ("preview_environments", PreviewEnvironment),
        ("tenant_memory", TenantMemory),
        ("subscriptions", Subscription),
        ("usage_ledger", UsageLedgerEntry),
        ("audit_events", AuditEvent),
        ("notifications", NotificationRecord),
    ]

    sections: dict[str, Any] = {
        "tenant": _serialize_row(tenant),
        "memberships": [],
    }
    member_rows = list(session.scalars(select(Membership).where(Membership.tenant_id == tenant_id)))
    for m in member_rows:
        user = session.get(User, m.user_id)
        item = _serialize_row(m)
        if user is not None:
            item["user_email"] = user.email
            item["user_full_name"] = user.full_name
        sections["memberships"].append(item)

    for section_name, model_cls in export_tables:
        rows = list(session.scalars(select(model_cls).where(model_cls.tenant_id == tenant_id)))
        sections[section_name] = [_serialize_row(r) for r in rows]

    manifest_counts: dict[str, int] = {
        k: (1 if isinstance(v, dict) else len(v)) for k, v in sections.items()
    }
    manifest_hashes: dict[str, str] = {}

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for section_name, data in sections.items():
            raw_json = json.dumps(data, indent=2, sort_keys=True, default=str).encode("utf-8")
            manifest_hashes[f"data/{section_name}.json"] = hashlib.sha256(raw_json).hexdigest()
            zf.writestr(f"data/{section_name}.json", raw_json)
        manifest = {
            "export_id": export_id,
            "tenant_id": tenant_id,
            "tenant_slug": tenant.slug,
            "generated_at": datetime.now(UTC).isoformat(),
            "requested_by_user_id": requested_by_user_id,
            "record_counts": manifest_counts,
            "sha256_files": manifest_hashes,
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"))

    zip_bytes = buf.getvalue()
    object_key = build_artifact_key(
        tenant_id,
        project_id="org-export",
        task_id=export_id,
        filename="tenant-export.zip",
    )
    object_store.put_bytes(tenant_id, object_key, zip_bytes, content_type="application/zip")

    job = DataExportJob(
        id=export_id,
        tenant_id=tenant_id,
        requested_by_user_id=requested_by_user_id,
        object_key=object_key,
        status="completed",
    )
    session.add(job)
    record_audit_event(
        session,
        tenant_id=tenant_id,
        actor_user_id=requested_by_user_id,
        action="tenant.data_exported",
        target_type="data_export_job",
        target_id=export_id,
        details={"object_key": object_key, "bytes": len(zip_bytes), "counts": manifest_counts},
    )
    session.flush()
    return job


def list_export_jobs(session: Session, *, tenant_id: str) -> list[DataExportJob]:
    """List data export jobs strictly scoped to tenant_id."""
    return list(
        session.scalars(
            select(DataExportJob)
            .where(DataExportJob.tenant_id == tenant_id)
            .order_by(DataExportJob.created_at.desc())
        )
    )


def get_export_archive_bytes(
    session: Session,
    *,
    object_store: ObjectStore,
    tenant_id: str,
    export_id: str,
) -> tuple[DataExportJob, bytes]:
    """Load export ZIP bytes ensuring strict tenant ownership."""
    job = session.scalar(
        select(DataExportJob).where(
            DataExportJob.id == export_id,
            DataExportJob.tenant_id == tenant_id,
        )
    )
    if job is None:
        raise NotFoundError(f"DataExportJob {export_id} not found.")
    return job, object_store.get_bytes(tenant_id, job.object_key)


# ---------------------------------------------------------------------------
# Tenant Hard Delete (Right to Be Forgotten & Tombstone)
# ---------------------------------------------------------------------------


def hard_delete_tenant(
    session: Session,
    *,
    object_store: ObjectStore,
    tenant_id: str,
    requested_by_user_id: str,
    confirm_slug: str,
) -> RetentionJob:
    """Purge all tenant-owned data, revoke active sessions, delete object storage prefix, and tombstone tenant."""
    set_session_tenant(session, tenant_id)
    tenant = session.get(Tenant, tenant_id)
    if tenant is None or tenant.status == "deleted":
        raise NotFoundError(f"Tenant {tenant_id} not found.")
    if confirm_slug.strip() != tenant.slug:
        raise ValidationError("Confirmation slug does not match organization slug.")

    now = datetime.now(UTC)
    deleted_counts: dict[str, int] = {}

    # Clear active sessions pointing at this tenant so existing JWTs fail immediately.
    sess_res = session.execute(
        update(UserSession)
        .where(UserSession.active_tenant_id == tenant_id)
        .values(revoked_at=now, active_tenant_id=None)
    )
    deleted_counts["revoked_sessions"] = int(getattr(sess_res, "rowcount", 0) or 0)

    # Clear SupportAccessGrants for this tenant.
    sup_res = session.execute(
        delete(SupportAccessGrant).where(SupportAccessGrant.tenant_id == tenant_id)
    )
    deleted_counts["support_access_grants"] = int(getattr(sup_res, "rowcount", 0) or 0)

    # Delete tenant-scoped tables in strict reverse dependency order, leaving RetentionJob receipt only.
    for table in reversed(Base.metadata.sorted_tables):
        if table.name == RetentionJob.__tablename__:
            continue
        if "tenant_id" not in table.c:
            continue
        res = session.execute(delete(table).where(table.c.tenant_id == tenant_id))
        deleted_counts[table.name] = int(getattr(res, "rowcount", 0) or 0)

    objects_removed = object_store.delete_tenant_prefix(tenant_id)
    deleted_counts["object_store_files"] = objects_removed

    tenant.status = "deleted"
    tenant.deleted_at = now
    tenant.name = f"[deleted-{tenant_id}]"
    tenant.slug = f"deleted-{tenant_id}-{secrets.token_hex(3)}"[:64]

    retention = RetentionJob(
        id=f"ret_{secrets.token_hex(8)}",
        tenant_id=tenant_id,
        job_kind="hard_delete",
        requested_by_user_id=requested_by_user_id,
        status="completed",
        deleted_counts_json=deleted_counts,
        created_at=now,
    )
    session.add(retention)
    session.flush()
    return retention
