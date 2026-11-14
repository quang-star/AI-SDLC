"""Task intake, lifecycle, clarification, artifacts, cancellation, and dev worker drain."""

from __future__ import annotations

import json
import secrets
from typing import Any, cast

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_sdlc_saas.adapters.metagpt_service import edit_task_spec
from ai_sdlc_saas.api.deps import get_db, get_runtime, require_permission
from ai_sdlc_saas.api.runtime import AppRuntime
from ai_sdlc_saas.db.models import (
    ArtifactVersion,
    Membership,
    ReviewFindingRecord,
    Task,
    TaskRun,
    Tenant,
    TestPlanRecord,
)
from ai_sdlc_saas.errors import NotFoundError
from ai_sdlc_saas.gateway.adapters import ModelAdapter
from ai_sdlc_saas.orchestrator.executors import build_production_executors
from ai_sdlc_saas.orchestrator.graph import StageExecutors
from ai_sdlc_saas.orchestrator.janitor import janitor_sweep
from ai_sdlc_saas.orchestrator.worker import (
    answer_task_clarification,
    cancel_task_execution,
    create_and_enqueue_task,
    drain_ready_jobs,
    list_task_approvals,
    list_task_artifacts,
)

router = APIRouter(prefix="/api/v1", tags=["tasks"])


class CreateTaskRequest(BaseModel):
    project_id: str
    title: str = Field(min_length=2, max_length=190)
    requirement_text: str = Field(min_length=5)
    task_type: str = "bugfix"
    repo_ids: list[str] = Field(default_factory=list)
    risk_level: str = "standard"
    fast_track: bool = False
    budget_cap_cents: int = Field(default=500, ge=10, le=500_000)
    require_release_approval: bool = False


class ClarifyTaskRequest(BaseModel):
    answers: dict[str, str] = Field(default_factory=dict)


def _task_summary(t: Task) -> dict[str, Any]:
    return {
        "id": t.id,
        "project_id": t.project_id,
        "title": t.title,
        "requirement_text": t.requirement_text,
        "task_type": t.task_type,
        "risk_level": t.risk_level,
        "status": t.status,
        "current_stage": t.current_stage,
        "cleanup_status": t.cleanup_status,
        "repo_ids": t.repo_ids_json,
        "budget_cap_cents": t.budget_cap_cents,
        "spent_cents": t.spent_cents,
        "active_run_id": t.active_run_id,
        "error_code": t.error_code,
        "error_message": t.error_message,
        "created_at": t.created_at.isoformat(),
    }


@router.get("/tasks")
def api_list_tasks(
    project_id: str | None = Query(default=None),
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("task:read")),
    db: Session = Depends(get_db, scope="function"),
) -> list[dict[str, Any]]:
    _ctx, tenant, _mem = perm
    stmt = select(Task).where(Task.tenant_id == tenant.id)
    if project_id:
        stmt = stmt.where(Task.project_id == project_id)
    rows = list(db.scalars(stmt.order_by(Task.created_at.desc())))
    return [_task_summary(t) for t in rows]


@router.post("/tasks", status_code=201)
def api_create_task(
    body: CreateTaskRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("task:create")),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    ctx, tenant, _mem = perm
    key = (idempotency_key or "").strip() or secrets.token_hex(12)
    res = create_and_enqueue_task(
        db,
        tenant_id=tenant.id,
        actor_user_id=ctx.user.id,
        project_id=body.project_id,
        title=body.title,
        requirement_text=body.requirement_text,
        task_type=body.task_type,
        repo_ids=body.repo_ids,
        risk_level=body.risk_level,
        fast_track=body.fast_track,
        budget_cap_cents=body.budget_cap_cents,
        idempotency_key=key,
        require_release_approval=body.require_release_approval,
    )
    task: Task = res["task"]
    run: TaskRun = res["run"]
    return {
        **_task_summary(task),
        "run_id": run.id,
        "job_id": res["job"].id,
        "deduplicated": res["deduplicated"],
    }


@router.get("/tasks/{task_id}")
def api_get_task(
    task_id: str,
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("task:read")),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    _ctx, tenant, _mem = perm
    task = db.scalar(select(Task).where(Task.id == task_id, Task.tenant_id == tenant.id))
    if task is None:
        raise NotFoundError("Task not found.")
    run = db.get(TaskRun, task.active_run_id) if task.active_run_id else None
    artifacts = list_task_artifacts(db, tenant_id=tenant.id, task_id=task.id)
    approvals = list_task_approvals(db, tenant_id=tenant.id, task_id=task.id)
    return {
        **_task_summary(task),
        "run": (
            {
                "id": run.id,
                "status": run.status,
                "current_node": run.current_node,
                "base_commits": run.base_commits_json,
                "policy_version": str((run.frozen_policy_json or {}).get("policy_digest") or "policy-v1"),
                "spec_version": run.spec_version,
                "spec_sha256": run.spec_sha256,
                "plan_sha256": run.plan_sha256,
                "patch_sha256": run.patch_sha256,
                "verification_verdict": run.verification_verdict,
            }
            if run
            else None
        ),
        "artifacts": [
            {
                "id": a.id,
                "artifact_type": a.artifact_type,
                "version": a.version,
                "schema_version": a.schema_version,
                "sha256": a.sha256,
                "metadata": a.metadata_json,
                "created_at": a.created_at.isoformat(),
            }
            for a in artifacts
        ],
        "approvals": [
            {
                "id": ap.id,
                "gate_type": ap.gate_type,
                "decision": ap.decision,
                "target_digest": ap.target_digest,
                "actor_user_id": ap.actor_user_id,
                "is_valid": ap.is_valid,
                "invalidated_reason": ap.invalidated_reason,
                "created_at": ap.created_at.isoformat(),
            }
            for ap in approvals
        ],
    }


@router.get("/tasks/{task_id}/artifacts/{artifact_id}/content")
def api_get_artifact_content(
    task_id: str,
    artifact_id: str,
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("task:read")),
    db: Session = Depends(get_db, scope="function"),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    _ctx, tenant, _mem = perm
    art = db.scalar(
        select(ArtifactVersion).where(
            ArtifactVersion.id == artifact_id,
            ArtifactVersion.task_id == task_id,
            ArtifactVersion.tenant_id == tenant.id,
        )
    )
    if art is None:
        raise NotFoundError("Artifact not found.")
    raw = runtime.object_store.get_bytes(tenant.id, art.object_key).decode("utf-8", errors="replace")
    try:
        parsed: Any = json.loads(raw)
    except Exception:
        parsed = raw
    return {
        "id": art.id,
        "artifact_type": art.artifact_type,
        "version": art.version,
        "sha256": art.sha256,
        "content": parsed,
    }


@router.post("/tasks/{task_id}/clarify")
def api_clarify_task(
    task_id: str,
    body: ClarifyTaskRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("task:create")),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    ctx, tenant, _mem = perm
    key = (idempotency_key or "").strip() or secrets.token_hex(12)
    job = answer_task_clarification(
        db,
        tenant_id=tenant.id,
        actor_user_id=ctx.user.id,
        task_id=task_id,
        answers=body.answers,
        idempotency_key=key,
    )
    return {"accepted": True, "job_id": job.id}


@router.post("/tasks/{task_id}/cancel")
def api_cancel_task(
    task_id: str,
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("task:cancel")),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    ctx, tenant, _mem = perm
    task = cancel_task_execution(db, tenant_id=tenant.id, actor_user_id=ctx.user.id, task_id=task_id)
    return _task_summary(task)


@router.post("/workers/drain")
def api_drain_workers(
    max_jobs: int = Query(default=10, ge=1, le=100),
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("task:create")),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    _ctx, _tenant, _mem = perm
    executors = cast(StageExecutors | None, runtime.extra.get("executors")) or build_production_executors()
    override_adapters = cast(dict[str, ModelAdapter] | None, runtime.extra.get("override_adapters"))
    processed = drain_ready_jobs(
        runtime,
        max_jobs=max_jobs,
        executors=executors,
        override_adapters=override_adapters,
    )
    return {"processed_count": len(processed), "results": processed}


@router.post("/workers/janitor-sweep")
def api_janitor_sweep(
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("org:manage")),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    return janitor_sweep(db)


class EditSpecRequest(BaseModel):
    title: str | None = None
    summary: str | None = None
    problem_statement: str | None = None
    target_files: list[str] | None = None
    expected_new_test_files: list[str] | None = None
    acceptance_criteria: list[dict[str, Any]] | None = None


@router.put("/tasks/{task_id}/spec")
def api_edit_task_spec(
    task_id: str,
    body: EditSpecRequest,
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("task:create")),
    db: Session = Depends(get_db, scope="function"),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    ctx, tenant, _mem = perm
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    return edit_task_spec(
        db,
        tenant_id=tenant.id,
        actor_user_id=ctx.user.id,
        task_id=task_id,
        updates=updates,
        object_store=runtime.object_store,
    )


@router.get("/tasks/{task_id}/test-plan")
def api_get_task_test_plan(
    task_id: str,
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("task:read")),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    _ctx, tenant, _mem = perm
    rec = db.scalar(
        select(TestPlanRecord)
        .where(TestPlanRecord.tenant_id == tenant.id, TestPlanRecord.task_id == task_id)
        .order_by(TestPlanRecord.created_at.desc())
    )
    if rec is None:
        raise NotFoundError("Test plan not yet generated.")
    return {
        "id": rec.id,
        "spec_sha256": rec.spec_sha256,
        "verdict": rec.verdict,
        "test_plan": rec.payload_json,
    }


@router.get("/tasks/{task_id}/findings")
def api_list_task_findings(
    task_id: str,
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("task:read")),
    db: Session = Depends(get_db, scope="function"),
) -> list[dict[str, Any]]:
    _ctx, tenant, _mem = perm
    rows = list(
        db.scalars(
            select(ReviewFindingRecord)
            .where(ReviewFindingRecord.tenant_id == tenant.id, ReviewFindingRecord.task_id == task_id)
            .order_by(ReviewFindingRecord.created_at.asc())
        )
    )
    return [
        {
            "id": f.id,
            "severity": f.severity,
            "file_path": f.file_path,
            "line_number": f.line_number,
            "title": f.title,
            "detail": f.detail,
            "status": f.status,
        }
        for f in rows
    ]
