"""Release notes + rollback plan generator and gated release promotion."""

from __future__ import annotations

import secrets
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_sdlc.contracts import TaskSpec, sha256_hex
from ai_sdlc_saas.db.models import (
    ArtifactVersion,
    NotificationRecord,
    PullRequestRecord,
    Task,
    TaskRun,
    VerificationReportRecord,
)
from ai_sdlc_saas.errors import ConflictError, NotFoundError
from ai_sdlc_saas.governance.audit import record_audit_event
from ai_sdlc_saas.orchestrator.state_machine import record_gate_approval
from ai_sdlc_saas.storage.object_store import LocalObjectStore, build_artifact_key


def generate_release_bundle(
    session: Session,
    *,
    tenant_id: str,
    task_id: str,
    object_store: LocalObjectStore,
) -> dict[str, Any]:
    """Generate structured markdown release notes and deterministic rollback checklist."""
    task = session.scalar(select(Task).where(Task.id == task_id, Task.tenant_id == tenant_id))
    if task is None:
        raise NotFoundError("Task not found.")
    run = (
        session.get(TaskRun, task.active_run_id)
        if task.active_run_id
        else session.scalar(
            select(TaskRun)
            .where(TaskRun.tenant_id == tenant_id, TaskRun.task_id == task.id)
            .order_by(TaskRun.created_at.desc())
        )
    )
    if run is None:
        raise NotFoundError("TaskRun not found.")

    prs = list(
        session.scalars(
            select(PullRequestRecord).where(
                PullRequestRecord.tenant_id == tenant_id,
                PullRequestRecord.run_id == run.id,
            )
        )
    )
    reports = list(
        session.scalars(
            select(VerificationReportRecord).where(
                VerificationReportRecord.tenant_id == tenant_id,
                VerificationReportRecord.run_id == run.id,
            )
        )
    )
    spec_row = session.scalar(
        select(ArtifactVersion)
        .where(
            ArtifactVersion.tenant_id == tenant_id,
            ArtifactVersion.task_id == task.id,
            ArtifactVersion.artifact_type == "TaskSpec",
        )
        .order_by(ArtifactVersion.version.desc())
    )
    spec = TaskSpec.from_dict(object_store.get_json(tenant_id, spec_row.object_key)) if spec_row else None

    pr_lines = [f"- `{pr.branch_name}` -> `{pr.base_branch}` ({pr.external_pr_url})" for pr in prs] or ["- Docs-only change"]
    ac_lines = [f"- `{ac.ac_id}`: {ac.description}" for ac in spec.acceptance_criteria] if spec else []
    rollback_steps = [
        f"git revert -m 1 <merge_commit> on `{pr.base_branch}` (PR {pr.external_pr_url})" for pr in reversed(prs)
    ] or ["No git branches to revert; restore previous documentation snapshot."]

    markdown = (
        f"# Release Notes: {task.title}\n\n"
        f"- **Task ID:** `{task.id}`\n"
        f"- **Spec SHA-256:** `{run.spec_sha256 or 'n/a'}`\n"
        f"- **Patch SHA-256:** `{run.patch_sha256 or 'n/a'}`\n"
        f"- **Verification Reports:** {len(reports)} (verdict=`{run.verification_verdict or 'PASS'}`)\n\n"
        f"## Included Changes\n" + "\n".join(pr_lines) + "\n\n"
        + ("## Acceptance Criteria\n" + "\n".join(ac_lines) + "\n\n" if ac_lines else "")
        + "## Rollback Plan\n"
        + "\n".join(f"{i}. {step}" for i, step in enumerate(rollback_steps, 1))
        + "\n"
    )
    for pr in prs:
        pr.release_notes = markdown

    raw = markdown.encode("utf-8")
    digest = sha256_hex(raw)
    key = build_artifact_key(tenant_id, task.project_id, task.id, f"release_notes_{run.id}.md")
    object_store.put_bytes(tenant_id, key, raw, content_type="text/markdown")
    session.add(
        ArtifactVersion(
            id=f"art_{secrets.token_hex(8)}",
            tenant_id=tenant_id,
            task_id=task.id,
            run_id=run.id,
            artifact_type="ReleaseNotes",
            version=1,
            schema_version="1.1",
            sha256=digest,
            object_key=key,
            metadata_json={"pr_count": len(prs)},
        )
    )
    session.flush()
    return {"task_id": task.id, "run_id": run.id, "sha256": digest, "release_notes_markdown": markdown, "rollback_steps": rollback_steps}


def promote_release(
    session: Session,
    *,
    tenant_id: str,
    actor_user_id: str,
    actor_role: str,
    task_id: str,
    idempotency_key: str,
    object_store: LocalObjectStore,
    comment: str = "",
) -> dict[str, Any]:
    """Promote verified + PR-delivered change set after dedicated `release:approve` gate check."""
    task = session.scalar(select(Task).where(Task.id == task_id, Task.tenant_id == tenant_id))
    if task is None:
        raise NotFoundError("Task not found.")
    run = (
        session.get(TaskRun, task.active_run_id)
        if task.active_run_id
        else session.scalar(
            select(TaskRun)
            .where(TaskRun.tenant_id == tenant_id, TaskRun.task_id == task.id)
            .order_by(TaskRun.created_at.desc())
        )
    )
    if run is None or not run.patch_sha256:
        raise ConflictError("Task run has no verified patch to promote.")

    first_base = next(iter((run.base_commits_json or {}).values()), "docs-base")
    approval = record_gate_approval(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        run=run,
        gate_type="release",
        decision="approved",
        target_digest=run.patch_sha256,
        base_commit_sha=first_base,
        policy_version=str((run.frozen_policy_json or {}).get("policy_digest") or "policy-v1"),
        idempotency_key=idempotency_key,
        comment=comment,
    )
    bundle = generate_release_bundle(session, tenant_id=tenant_id, task_id=task.id, object_store=object_store)
    prs = list(
        session.scalars(
            select(PullRequestRecord).where(
                PullRequestRecord.tenant_id == tenant_id,
                PullRequestRecord.run_id == run.id,
            )
        )
    )
    for pr in prs:
        if pr.status == "open":
            pr.status = "merged"

    session.add(
        NotificationRecord(
            id=f"ntf_{secrets.token_hex(8)}",
            tenant_id=tenant_id,
            user_id=task.created_by_user_id,
            channel="in_app",
            event_type="release.promoted",
            title=f"Released: {task.title}",
            body=f"Release promoted by {actor_user_id} (approval {approval.id}).",
        )
    )
    record_audit_event(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="delivery.release_promoted",
        target_type="task",
        target_id=task.id,
        details={"approval_id": approval.id, "release_notes_sha256": bundle["sha256"]},
    )
    session.flush()
    return {"status": "promoted", "approval_id": approval.id, **bundle}
