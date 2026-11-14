"""Orchestration service API + worker execution loop bridging durable jobs and LangGraph."""

from __future__ import annotations

import secrets
import socket
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from ai_sdlc_saas.api.runtime import AppRuntime
from ai_sdlc_saas.db.engine import set_session_tenant
from ai_sdlc_saas.db.models import Approval, ArtifactVersion, Job, Task, TaskRun
from ai_sdlc_saas.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from ai_sdlc_saas.gateway.adapters import ModelAdapter
from ai_sdlc_saas.governance.audit import record_audit_event
from ai_sdlc_saas.governance.lifecycle import assert_operation_allowed, enforce_trial_guardrails
from ai_sdlc_saas.governance.observability import record_worker_job_metric
from ai_sdlc_saas.orchestrator.checkpointer import TenantSqlAlchemyCheckpointSaver
from ai_sdlc_saas.orchestrator.events import emit_task_event
from ai_sdlc_saas.orchestrator.graph import (
    OrchestratorContext,
    SdlcGraphState,
    StageExecutors,
    build_sdlc_graph,
)
from ai_sdlc_saas.orchestrator.janitor import ResourceDeleter, cleanup_task_resources
from ai_sdlc_saas.orchestrator.job_engine import (
    cancel_pending_jobs_for_task,
    claim_next_job,
    complete_job,
    enqueue_job,
    fail_or_requeue_job,
    register_worker_heartbeat,
)
from ai_sdlc_saas.orchestrator.state_machine import (
    assert_approval_valid_for_resume,
    record_gate_approval,
    resolve_effective_risk,
)
from ai_sdlc_saas.repos.service import assert_repo_dispatchable


def create_and_enqueue_task(
    session: Session,
    *,
    tenant_id: str,
    actor_user_id: str,
    project_id: str,
    title: str,
    requirement_text: str,
    task_type: str,
    repo_ids: list[str],
    risk_level: str = "standard",
    fast_track: bool = False,
    budget_cap_cents: int = 500,
    idempotency_key: str,
    require_release_approval: bool = False,
) -> dict[str, Any]:
    """Create Task + TaskRun + start_run Job idempotently after validating repos & risk policy."""
    if not title.strip() or not requirement_text.strip():
        raise ValidationError("Task title and requirement_text are required.")

    # Idempotency dedup by job key (`start:<tenant>:<idempotency_key>`)
    job_key = f"start:{tenant_id}:{idempotency_key}"
    existing_job = session.scalar(
        select(Job).where(Job.tenant_id == tenant_id, Job.idempotency_key == job_key)
    )
    if existing_job is not None:
        existing_task = session.get(Task, existing_job.task_id)
        existing_run = session.get(TaskRun, existing_job.run_id)
        return {"task": existing_task, "run": existing_run, "job": existing_job, "deduplicated": True}

    unique_repos = list(dict.fromkeys(repo_ids))
    if task_type != "docs" and not unique_repos:
        raise ValidationError("At least one connected repository is required.")

    assert_operation_allowed(session, tenant_id=tenant_id)
    enforce_trial_guardrails(session, tenant_id=tenant_id, actor_user_id=actor_user_id)

    for rid in unique_repos:
        repo = assert_repo_dispatchable(session, tenant_id=tenant_id, repo_id=rid)
        if repo.project_id != project_id:
            raise ConflictError(f"Repository {rid!r} does not belong to project {project_id!r}.")

    eff_risk, eff_fast_track = resolve_effective_risk(
        task_type=task_type,
        requested_risk=risk_level,
        requirement_text=requirement_text,
        repo_count=len(unique_repos),
        requested_fast_track=fast_track,
    )

    task_id = f"tsk_{secrets.token_hex(8)}"
    run_id = f"run_{secrets.token_hex(8)}"
    thread_id = f"{tenant_id}:{task_id}:{run_id}"

    task = Task(
        id=task_id,
        tenant_id=tenant_id,
        project_id=project_id,
        created_by_user_id=actor_user_id,
        title=title.strip()[:190],
        requirement_text=requirement_text,
        task_type=task_type,
        risk_level=eff_risk,
        status="QUEUED",
        current_stage="intake",
        cleanup_status="PENDING",
        repo_ids_json=unique_repos,
        budget_cap_cents=max(10, budget_cap_cents),
        active_run_id=run_id,
    )
    run = TaskRun(
        id=run_id,
        tenant_id=tenant_id,
        task_id=task_id,
        thread_id=thread_id,
        status="QUEUED",
        current_node="intake",
    )
    session.add(task)
    session.flush()
    session.add(run)
    session.flush()

    initial_state: SdlcGraphState = {
        "tenant_id": tenant_id,
        "task_id": task_id,
        "run_id": run_id,
        "project_id": project_id,
        "repo_ids": unique_repos,
        "task_type": task_type,
        "risk_level": eff_risk,
        "fast_track": eff_fast_track,
        "require_release_approval": require_release_approval,
        "max_repair_rounds": 2,
    }
    job = enqueue_job(
        session,
        tenant_id=tenant_id,
        task_id=task_id,
        run_id=run_id,
        job_type="start_run",
        payload={"initial_state": dict(initial_state)},
        idempotency_key=job_key,
        outbox_event_type="task.queued",
    )
    emit_task_event(
        session,
        tenant_id=tenant_id,
        task_id=task_id,
        run_id=run_id,
        event_type="task.created",
        stage="intake",
        payload={"risk_level": eff_risk, "fast_track": eff_fast_track},
    )
    record_audit_event(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="task.created",
        target_type="task",
        target_id=task_id,
        details={"task_type": task_type, "risk_level": eff_risk, "fast_track": eff_fast_track},
    )
    session.flush()
    return {"task": task, "run": run, "job": job, "deduplicated": False}


def answer_task_clarification(
    session: Session,
    *,
    tenant_id: str,
    actor_user_id: str,
    task_id: str,
    answers: dict[str, str],
    idempotency_key: str,
) -> Job:
    task = session.scalar(select(Task).where(Task.id == task_id, Task.tenant_id == tenant_id))
    if task is None or not task.active_run_id:
        raise NotFoundError("Task not found.")
    if task.status != "WAITING_INPUT":
        raise ConflictError(f"Task is in status {task.status!r}, not awaiting clarification input.")

    job = enqueue_job(
        session,
        tenant_id=tenant_id,
        task_id=task.id,
        run_id=task.active_run_id,
        job_type="resume_run",
        payload={"resume_value": dict(answers), "actor_user_id": actor_user_id},
        idempotency_key=f"clarify:{tenant_id}:{task.id}:{idempotency_key}",
        outbox_event_type="task.clarification_answered",
    )
    emit_task_event(
        session,
        tenant_id=tenant_id,
        task_id=task.id,
        run_id=task.active_run_id,
        event_type="task.clarification_answered",
        stage="clarify_gate",
        payload={"keys": sorted(answers.keys())},
    )
    session.flush()
    return job


def submit_gate_decision(
    session: Session,
    *,
    tenant_id: str,
    actor_user_id: str,
    actor_role: str,
    task_id: str,
    gate_type: str,
    decision: str,
    target_digest: str,
    base_commit_sha: str,
    policy_version: str,
    idempotency_key: str,
    comment: str = "",
) -> dict[str, Any]:
    task = session.scalar(select(Task).where(Task.id == task_id, Task.tenant_id == tenant_id))
    if task is None or not task.active_run_id:
        raise NotFoundError("Task not found.")
    if task.status in ("CANCELLED", "DELIVERED", "FAILED"):
        raise ConflictError(f"Task is already terminal ({task.status}).")
    run = session.get(TaskRun, task.active_run_id)
    if run is None:
        raise NotFoundError("Active TaskRun not found.")

    approval = record_gate_approval(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        run=run,
        gate_type=gate_type,
        decision=decision,
        target_digest=target_digest,
        base_commit_sha=base_commit_sha,
        policy_version=policy_version,
        idempotency_key=idempotency_key,
        comment=comment,
    )

    job: Job | None = None
    if decision in ("approved", "rejected"):
        job = enqueue_job(
            session,
            tenant_id=tenant_id,
            task_id=task.id,
            run_id=run.id,
            job_type="resume_run",
            payload={
                "gate_type": gate_type,
                "approval_id": approval.id,
                "resume_value": {"decision": decision, "gate_type": gate_type, "approval_id": approval.id},
            },
            idempotency_key=f"gate:{tenant_id}:{run.id}:{gate_type}:{approval.id}",
            outbox_event_type=f"gate.{gate_type}.{decision}",
        )
        emit_task_event(
            session,
            tenant_id=tenant_id,
            task_id=task.id,
            run_id=run.id,
            event_type=f"gate.{gate_type}.{decision}",
            stage=task.current_stage,
            payload={"approval_id": approval.id, "actor_user_id": actor_user_id},
        )
    session.flush()
    return {"approval": approval, "job": job}


def cancel_task_execution(
    session: Session,
    *,
    tenant_id: str,
    actor_user_id: str,
    task_id: str,
    deleter: ResourceDeleter | None = None,
) -> Task:
    task = session.scalar(select(Task).where(Task.id == task_id, Task.tenant_id == tenant_id))
    if task is None:
        raise NotFoundError("Task not found.")
    if task.status in ("DELIVERED", "CANCELLED"):
        return task

    task.status = "CANCELLED"
    task.error_code = "cancelled_by_user"
    task.error_message = f"Cancelled by user {actor_user_id}."
    cancel_pending_jobs_for_task(session, tenant_id=tenant_id, task_id=task_id)
    cleanup_task_resources(session, tenant_id=tenant_id, task_id=task_id, deleter=deleter)
    if task.active_run_id:
        run = session.get(TaskRun, task.active_run_id)
        if run:
            run.status = "CANCELLED"
        emit_task_event(
            session,
            tenant_id=tenant_id,
            task_id=task.id,
            run_id=task.active_run_id,
            event_type="task.cancelled",
            stage=task.current_stage,
            payload={"actor_user_id": actor_user_id},
        )
    record_audit_event(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="task.cancelled",
        target_type="task",
        target_id=task_id,
    )
    session.flush()
    return task


def process_one_job(
    runtime: AppRuntime,
    *,
    worker_id: str | None = None,
    lease_seconds: int = 60,
    executors: StageExecutors | None = None,
    fault_hook: Callable[[str, SdlcGraphState], None] | None = None,
    override_adapters: dict[str, ModelAdapter] | None = None,
    resource_deleter: ResourceDeleter | None = None,
) -> dict[str, Any] | None:
    """Claim one ready/expired job and execute its LangGraph turn cleanly."""
    wid = worker_id or f"wrk_{socket.gethostname()}_{secrets.token_hex(3)}"
    with runtime.session_factory() as s:
        register_worker_heartbeat(s, worker_id=wid, hostname=socket.gethostname())
        job = claim_next_job(s, worker_id=wid, lease_seconds=lease_seconds)
        s.commit()

    if job is None:
        return None

    tenant_id = job.tenant_id
    try:
        with runtime.session_factory() as s:
            set_session_tenant(s, tenant_id)
            run = s.get(TaskRun, job.run_id)
            task = s.get(Task, job.task_id)
            if run is None or task is None or task.status == "CANCELLED":
                complete_job(s, job_id=job.id)
                s.commit()
                return {"job_id": job.id, "task_id": job.task_id, "status": "skipped_cancelled"}

            assert_operation_allowed(s, tenant_id=tenant_id)

            # Re-verify approving actor still holds active org permissions when resuming past a gate.
            gate_type = job.payload_json.get("gate_type")
            resume_val = job.payload_json.get("resume_value")
            if gate_type and isinstance(resume_val, dict) and resume_val.get("decision") == "approved":
                try:
                    assert_approval_valid_for_resume(
                        s, tenant_id=tenant_id, run=run, gate_type=str(gate_type)
                    )
                except (ForbiddenError, ConflictError) as exc:
                    apr_id = str(resume_val.get("approval_id") or "")
                    apr = s.get(Approval, apr_id)
                    if apr is not None and apr.is_valid:
                        apr.is_valid = False
                        apr.invalidated_reason = str(exc)[:250]
                    task.error_code = "actor_revoked" if isinstance(exc, ForbiddenError) else "stale_approval"
                    task.error_message = str(exc)
                    emit_task_event(
                        s,
                        tenant_id=tenant_id,
                        task_id=task.id,
                        run_id=run.id,
                        event_type="gate.approval_invalidated",
                        stage=task.current_stage,
                        payload={"gate_type": gate_type, "reason": str(exc)},
                    )
                    complete_job(s, job_id=job.id)
                    s.commit()
                    return {
                        "job_id": job.id,
                        "task_id": job.task_id,
                        "task_status": task.status,
                        "rejected_reason": str(exc),
                    }
            thread_id = run.thread_id
            s.commit()

        orch_ctx = OrchestratorContext(
            settings=runtime.settings,
            session_factory=runtime.session_factory,
            object_store=runtime.object_store,
            cipher=runtime.cipher,
            fault_hook=fault_hook,
            override_adapters=override_adapters,
            resource_deleter=resource_deleter,
        )
        saver = TenantSqlAlchemyCheckpointSaver(runtime.session_factory)
        graph = build_sdlc_graph(ctx=orch_ctx, executors=executors, checkpointer=saver)
        config: RunnableConfig = {
            "configurable": {
                "tenant_id": tenant_id,
                "thread_id": thread_id,
            }
        }

        if job.job_type in ("start_run", "ci_rework_task"):
            existing_cp = saver.get_tuple(config)
            if existing_cp is not None:
                # Crash recovery replay: resume graph cleanly from latest saved checkpoint!
                graph.invoke(None, config)
            else:
                initial_state = dict(job.payload_json.get("initial_state") or {})
                graph.invoke(initial_state, config)
        elif job.job_type == "resume_run":
            resume_val = job.payload_json.get("resume_value")
            graph.invoke(Command(resume=resume_val), config)
        else:
            raise ValueError(f"Unsupported job_type={job.job_type!r}")

        with runtime.session_factory() as s:
            complete_job(s, job_id=job.id)
            record_worker_job_metric(job_type=job.job_type, outcome="completed")
            set_session_tenant(s, tenant_id)
            task_after = s.get(Task, job.task_id)
            status_after = task_after.status if task_after else "UNKNOWN"
            s.commit()

        return {"job_id": job.id, "task_id": job.task_id, "task_status": status_after}

    except Exception as exc:
        with runtime.session_factory() as s:
            updated = fail_or_requeue_job(
                s,
                job_id=job.id,
                error_message=f"{type(exc).__name__}: {exc}",
                base_backoff_seconds=0,
            )
            record_worker_job_metric(job_type=job.job_type, outcome="failed")
            s.commit()
        return {
            "job_id": job.id,
            "task_id": job.task_id,
            "error": str(exc),
            "job_status": updated.status if updated else "unknown",
        }


def drain_ready_jobs(
    runtime: AppRuntime,
    *,
    max_jobs: int = 25,
    executors: StageExecutors | None = None,
    fault_hook: Callable[[str, SdlcGraphState], None] | None = None,
    override_adapters: dict[str, ModelAdapter] | None = None,
    resource_deleter: ResourceDeleter | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for idx in range(max_jobs):
        res = process_one_job(
            runtime,
            worker_id=f"wrk_drain_{idx}",
            executors=executors,
            fault_hook=fault_hook,
            override_adapters=override_adapters,
            resource_deleter=resource_deleter,
        )
        if res is None:
            break
        results.append(res)
    return results


def list_task_artifacts(
    session: Session, *, tenant_id: str, task_id: str
) -> list[ArtifactVersion]:
    return list(
        session.scalars(
            select(ArtifactVersion)
            .where(ArtifactVersion.tenant_id == tenant_id, ArtifactVersion.task_id == task_id)
            .order_by(ArtifactVersion.created_at.asc(), ArtifactVersion.version.asc())
        )
    )


def list_task_approvals(session: Session, *, tenant_id: str, task_id: str) -> list[Approval]:
    return list(
        session.scalars(
            select(Approval)
            .where(Approval.tenant_id == tenant_id, Approval.task_id == task_id)
            .order_by(Approval.created_at.asc())
        )
    )


def run_cli() -> None:
    """Console script entrypoint running the polling SDLC worker + periodic janitor loop."""
    import os
    import time

    from ai_sdlc_saas.api.runtime import AppRuntime
    from ai_sdlc_saas.config import Settings
    from ai_sdlc_saas.orchestrator.executors import build_production_executors
    from ai_sdlc_saas.orchestrator.janitor import janitor_sweep

    settings = Settings()
    runtime = AppRuntime.from_settings(settings)
    executors = build_production_executors()
    poll_s = float(os.getenv("AI_SDLC_WORKER_POLL_SECONDS", "1.0"))
    sweep_counter = 0
    try:
        while True:
            processed = drain_ready_jobs(runtime, max_jobs=10, executors=executors)
            sweep_counter += 1
            if sweep_counter >= 30:
                sweep_counter = 0
                with runtime.session_factory() as s:
                    janitor_sweep(s)
                    s.commit()
            if not processed:
                time.sleep(poll_s)
    except KeyboardInterrupt:
        pass
