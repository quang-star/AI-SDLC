"""Idempotent multi-repo Git branch publisher, PR creator, CI feedback router, and notifications."""

from __future__ import annotations

import secrets
import shutil
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_sdlc.contracts import TaskSpec
from ai_sdlc_saas.db.models import (
    ArtifactVersion,
    GitHubInstallation,
    Job,
    NotificationRecord,
    PullRequestRecord,
    Repository,
    Task,
    TaskRun,
    TraceabilityEntryRecord,
    VerificationReportRecord,
)
from ai_sdlc_saas.errors import ConflictError, NotFoundError
from ai_sdlc_saas.governance.audit import record_audit_event
from ai_sdlc_saas.orchestrator.events import emit_task_event
from ai_sdlc_saas.orchestrator.graph import OrchestratorContext, SdlcGraphState
from ai_sdlc_saas.orchestrator.job_engine import enqueue_job
from ai_sdlc_saas.repos.git_ops import clone_at_commit, commit_and_push_branch
from ai_sdlc_saas.repos.github_app import GitHubRestClient


def _load_latest_patch_and_spec(
    session: Session,
    *,
    tenant_id: str,
    task_id: str,
    object_store: Any,
) -> tuple[dict[str, Any], TaskSpec | None]:
    patch_row = session.scalar(
        select(ArtifactVersion)
        .where(
            ArtifactVersion.tenant_id == tenant_id,
            ArtifactVersion.task_id == task_id,
            ArtifactVersion.artifact_type == "Patch",
        )
        .order_by(ArtifactVersion.version.desc(), ArtifactVersion.created_at.desc())
    )
    patch_payload = dict(object_store.get_json(tenant_id, patch_row.object_key)) if patch_row else {}

    spec_row = session.scalar(
        select(ArtifactVersion)
        .where(
            ArtifactVersion.tenant_id == tenant_id,
            ArtifactVersion.task_id == task_id,
            ArtifactVersion.artifact_type == "TaskSpec",
        )
        .order_by(ArtifactVersion.version.desc(), ArtifactVersion.created_at.desc())
    )
    spec = TaskSpec.from_dict(object_store.get_json(tenant_id, spec_row.object_key)) if spec_row else None
    return patch_payload, spec


def _assert_verification_cleared_for_publish(
    session: Session,
    *,
    tenant_id: str,
    run_id: str,
) -> list[VerificationReportRecord]:
    reports = list(
        session.scalars(
            select(VerificationReportRecord).where(
                VerificationReportRecord.tenant_id == tenant_id,
                VerificationReportRecord.run_id == run_id,
            )
        )
    )
    for rep in reports:
        if rep.verdict == "FAIL":
            raise ConflictError("Cannot publish PR while latest VerificationReport has verdict='FAIL'.")
        pending_manual = session.scalar(
            select(TraceabilityEntryRecord).where(
                TraceabilityEntryRecord.tenant_id == tenant_id,
                TraceabilityEntryRecord.report_id == rep.id,
                TraceabilityEntryRecord.status == "pending_manual",
            )
        )
        if pending_manual is not None:
            raise ConflictError(
                f"Cannot publish PR while manual verification check {pending_manual.ac_id!r} remains unconfirmed."
            )
    return reports


def _next_local_pr_number(session: Session, *, tenant_id: str, repo_id: str) -> int:
    existing = list(
        session.scalars(
            select(PullRequestRecord).where(
                PullRequestRecord.tenant_id == tenant_id,
                PullRequestRecord.repo_id == repo_id,
            )
        )
    )
    nums = [int(p.external_pr_number or 0) for p in existing]
    return (max(nums) if nums else 0) + 1


def publish_pr_stage(
    session: Session,
    state: SdlcGraphState,
    ctx: OrchestratorContext,
) -> dict[str, Any]:
    """Push verified branch commits per repo and record idempotent synchronized PRs."""
    task = session.get(Task, state["task_id"])
    run = session.get(TaskRun, state["run_id"])
    assert task is not None and run is not None

    _assert_verification_cleared_for_publish(session, tenant_id=state["tenant_id"], run_id=run.id)
    patch_payload, spec = _load_latest_patch_and_spec(
        session,
        tenant_id=state["tenant_id"],
        task_id=task.id,
        object_store=ctx.object_store,
    )
    patches_by_repo: dict[str, Any] = dict(patch_payload.get("patches_by_repo") or {})

    change_set_id = str(state.get("change_set_id") or "").strip() or f"cs_{secrets.token_hex(6)}"
    pr_urls: list[str] = []

    if not patches_by_repo:
        # Pure docs or lightweight pipeline where no repo patch was recorded
        repo_ids = list(state.get("repo_ids") or [])
        if not repo_ids:
            pr_urls.append(f"local://docs/{task.id}")
            return {"change_set_id": change_set_id, "pr_urls": pr_urls}

    repo_entries = list(patches_by_repo.items())
    for repo_key, entry in repo_entries:
        if repo_key == "docs":
            pr_urls.append(f"local://docs/{task.id}")
            continue

        idemp_key = f"pr:{task.id}:{run.id}:{repo_key}"
        existing_pr = session.scalar(
            select(PullRequestRecord).where(
                PullRequestRecord.tenant_id == state["tenant_id"],
                PullRequestRecord.idempotency_key == idemp_key,
            )
        )
        if existing_pr is not None:
            pr_urls.append(existing_pr.external_pr_url)
            continue

        repo = session.scalar(
            select(Repository).where(Repository.id == repo_key, Repository.tenant_id == state["tenant_id"])
        )
        if repo is None:
            continue

        branch_name = str(entry.get("target_branch") or f"ai-sdlc/task-{task.id[-8:]}")
        base_branch = repo.default_branch or "main"
        base_sha = str(entry.get("base_commit_sha") or (run.base_commits_json or {}).get(repo.id) or "HEAD")
        patch_text = str(entry.get("patch_text") or "")
        patch_sha = str(entry.get("patch_sha256") or run.patch_sha256 or "")

        if patch_text.strip():
            pub_tmp = Path(tempfile.mkdtemp(prefix="ai_sdlc_pub_"))
            try:
                clone_at_commit(
                    repo.clone_url_or_path,
                    pub_tmp,
                    branch_or_sha=base_sha,
                    allow_local_repos=ctx.settings.allow_local_git_repos,
                )
                commit_and_push_branch(
                    pub_tmp,
                    remote_url=repo.clone_url_or_path,
                    branch_name=branch_name,
                    target_branch=base_branch,
                    expected_base_sha=base_sha,
                    commit_message=f"feat: {task.title} ({task.id})",
                    patch_text=patch_text,
                )
            finally:
                shutil.rmtree(pub_tmp, ignore_errors=True)

        pr_body = (
            f"Automated AI SDLC Pull Request\n\n"
            f"- Task: `{task.id}` ({task.title})\n"
            f"- ChangeSet: `{change_set_id}`\n"
            f"- Spec SHA-256: `{run.spec_sha256 or ''}`\n"
            f"- Patch SHA-256: `{patch_sha}`\n"
            f"- Changed files: {', '.join(entry.get('changed_paths') or []) or 'none'}\n"
            f"- Verification verdict: `{run.verification_verdict or 'PASS'}`\n"
        )
        if spec:
            pr_body += f"- Acceptance Criteria: {', '.join(ac.ac_id for ac in spec.acceptance_criteria)}\n"

        pr_num: int | None = None
        pr_url = ""
        if repo.provider == "github_cloud" and repo.installation_id:
            inst = session.scalar(
                select(GitHubInstallation).where(
                    GitHubInstallation.id == repo.installation_id,
                    GitHubInstallation.tenant_id == state["tenant_id"],
                )
            )
            if inst and inst.encrypted_token and inst.status == "active":
                token = ctx.cipher.decrypt(inst.encrypted_token)
                client = GitHubRestClient(token=token)
                gh_resp = client.create_pull_request(
                    owner_repo=repo.full_name,
                    title=task.title,
                    body=pr_body,
                    head=branch_name,
                    base=base_branch,
                )
                pr_num = int(gh_resp.get("number") or 1)
                pr_url = str(gh_resp.get("html_url") or "")

        if not pr_url:
            pr_num = _next_local_pr_number(session, tenant_id=state["tenant_id"], repo_id=repo.id)
            pr_url = f"local-pr://{repo.full_name}/pull/{pr_num}"

        pr_row = PullRequestRecord(
            id=f"pr_{secrets.token_hex(8)}",
            tenant_id=state["tenant_id"],
            task_id=task.id,
            run_id=run.id,
            repo_id=repo.id,
            change_set_id=change_set_id,
            branch_name=branch_name,
            base_branch=base_branch,
            base_commit_sha=base_sha,
            patch_sha256=patch_sha,
            external_pr_number=pr_num,
            external_pr_url=pr_url,
            status="open",
            ci_status="pending",
            idempotency_key=idemp_key,
        )
        session.add(pr_row)
        pr_urls.append(pr_url)

    session.add(
        NotificationRecord(
            id=f"ntf_{secrets.token_hex(8)}",
            tenant_id=state["tenant_id"],
            user_id=task.created_by_user_id,
            channel="in_app",
            event_type="pr.delivered",
            title=f"PR Ready: {task.title}",
            body=f"Synchronized PR(s) created for ChangeSet {change_set_id}: {', '.join(pr_urls)}",
        )
    )
    record_audit_event(
        session,
        tenant_id=state["tenant_id"],
        actor_user_id=None,
        action="delivery.pr_published",
        target_type="task",
        target_id=task.id,
        details={"change_set_id": change_set_id, "pr_urls": pr_urls},
    )
    session.flush()
    return {"change_set_id": change_set_id, "pr_urls": pr_urls}


def handle_pr_ci_feedback(
    session: Session,
    *,
    tenant_id: str,
    ci_status: str,
    summary: str,
    pr_id: str | None = None,
    external_pr_number: int | None = None,
    repo_full_name: str | None = None,
    actor_user_id: str | None = None,
) -> dict[str, Any]:
    """Process CI status or PR comment feedback and enqueue bounded rework on failure."""
    pr: PullRequestRecord | None = None
    if pr_id:
        pr = session.scalar(
            select(PullRequestRecord).where(PullRequestRecord.id == pr_id, PullRequestRecord.tenant_id == tenant_id)
        )
    elif external_pr_number is not None:
        stmt = select(PullRequestRecord).where(
            PullRequestRecord.tenant_id == tenant_id,
            PullRequestRecord.external_pr_number == external_pr_number,
        )
        if repo_full_name:
            stmt = stmt.join(Repository, Repository.id == PullRequestRecord.repo_id).where(
                Repository.full_name == repo_full_name
            )
        pr = session.scalar(stmt.order_by(PullRequestRecord.created_at.desc()))

    if pr is None:
        raise NotFoundError("Pull request record not found for CI/review feedback.")

    normalized = ci_status.strip().lower()
    pr.ci_status = "passed" if normalized in ("passed", "success", "green") else "failed"
    rework_triggered = False

    task = session.get(Task, pr.task_id)
    if pr.ci_status == "failed" and task is not None:
        pr.status = "ci_failed"
        prior_reworks = list(
            session.scalars(
                select(Job).where(
                    Job.tenant_id == tenant_id,
                    Job.task_id == task.id,
                    Job.job_type == "ci_rework_task",
                )
            )
        )
        ci_rework_count = len(prior_reworks)
        if ci_rework_count < 2:
            new_run_id = f"run_{secrets.token_hex(8)}"
            new_run = TaskRun(
                id=new_run_id,
                tenant_id=tenant_id,
                task_id=task.id,
                thread_id=f"{tenant_id}:{task.id}:{new_run_id}",
                status="QUEUED",
                current_node="intake",
                frozen_policy_json={"ci_feedback": summary[:1000], "ci_rework_round": ci_rework_count + 1},
            )
            session.add(new_run)
            task.active_run_id = new_run_id
            task.status = "QUEUED"
            rework_triggered = True
            enqueue_job(
                session,
                tenant_id=tenant_id,
                task_id=task.id,
                run_id=new_run_id,
                job_type="ci_rework_task",
                idempotency_key=f"ci_rework:{pr.id}:{ci_rework_count + 1}",
                payload={
                    "pr_id": pr.id,
                    "initial_state": {
                        "tenant_id": tenant_id,
                        "task_id": task.id,
                        "run_id": new_run_id,
                        "project_id": task.project_id,
                        "repo_ids": list(task.repo_ids_json or []),
                        "task_type": task.task_type,
                        "risk_level": task.risk_level,
                        "fast_track": False,
                        "max_repair_rounds": 2,
                        "last_verification_feedback": summary[:1000],
                    },
                },
            )
        emit_task_event(
            session,
            tenant_id=tenant_id,
            task_id=task.id,
            run_id=pr.run_id,
            event_type="pr.ci_feedback",
            stage="publish_pr",
            payload={"pr_id": pr.id, "ci_status": pr.ci_status, "rework_triggered": rework_triggered, "summary": summary[:300]},
        )
        record_audit_event(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="delivery.ci_feedback",
            target_type="pull_request",
            target_id=pr.id,
            details={"ci_status": pr.ci_status, "rework_triggered": rework_triggered},
        )
    elif task is not None and pr.ci_status == "passed":
        pr.status = "open"
        emit_task_event(
            session,
            tenant_id=tenant_id,
            task_id=task.id,
            run_id=pr.run_id,
            event_type="pr.ci_passed",
            stage="publish_pr",
            payload={"pr_id": pr.id},
        )

    session.flush()
    return {
        "pr_id": pr.id,
        "task_id": pr.task_id,
        "ci_status": pr.ci_status,
        "pr_status": pr.status,
        "rework_triggered": rework_triggered,
    }
