"""16-stage LangGraph SDLC orchestrator compiled with tenant-isolated checkpointing."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ai_sdlc.contracts import canonical_json_bytes, sha256_hex
from ai_sdlc_saas.billing.ledger import InsufficientBudgetError, reserve_quota, settle_reservation
from ai_sdlc_saas.config import Settings
from ai_sdlc_saas.db.engine import tenant_transaction
from ai_sdlc_saas.db.models import (
    ArtifactVersion,
    QuotaReservation,
    RequestUsage,
    Task,
    TaskRun,
)
from ai_sdlc_saas.errors import BaselineStaleError
from ai_sdlc_saas.gateway.adapters import ModelAdapter
from ai_sdlc_saas.gateway.service import check_kill_switches, freeze_policy_snapshot
from ai_sdlc_saas.orchestrator.checkpointer import TenantSqlAlchemyCheckpointSaver
from ai_sdlc_saas.orchestrator.events import emit_task_event
from ai_sdlc_saas.orchestrator.janitor import ResourceDeleter, cleanup_task_resources
from ai_sdlc_saas.orchestrator.state_machine import (
    assert_approval_valid_for_resume,
    serialize_gate_prompt,
)
from ai_sdlc_saas.repos.service import assert_repo_dispatchable, load_snapshot_repo_context
from ai_sdlc_saas.security.crypto import EnvelopeCipher
from ai_sdlc_saas.storage.object_store import LocalObjectStore, build_artifact_key


class WorkerCrashError(RuntimeError):
    """Simulated crash exception for fault-injection and lease-recovery tests."""


class SdlcGraphState(TypedDict, total=False):
    tenant_id: str
    task_id: str
    run_id: str
    project_id: str
    repo_ids: list[str]
    task_type: str
    risk_level: str
    fast_track: bool
    require_release_approval: bool
    current_stage: str
    clarification_questions: list[str]
    clarification_answers: dict[str, str]
    spec_sha256: str
    spec_version: int
    spec_approved: bool
    plan_sha256: str
    plan_approved: bool
    qa_verdict: str
    implementation_attempts: int
    max_repair_rounds: int
    patch_sha256: str
    verification_verdict: str
    last_verification_feedback: str
    review_verdict: str
    delivery_approved: bool
    release_approved: bool
    change_set_id: str
    pr_urls: list[str]
    stop_reason: str
    error_code: str
    error_message: str


@dataclass
class OrchestratorContext:
    settings: Settings
    session_factory: sessionmaker[Session]
    object_store: LocalObjectStore
    cipher: EnvelopeCipher
    fault_hook: Callable[[str, SdlcGraphState], None] | None = None
    override_adapters: dict[str, ModelAdapter] | None = None
    resource_deleter: ResourceDeleter | None = None


StageFn = Callable[[Session, SdlcGraphState, OrchestratorContext], dict[str, Any]]


def _save_json_artifact(
    session: Session,
    *,
    object_store: LocalObjectStore,
    tenant_id: str,
    project_id: str,
    task_id: str,
    run_id: str,
    artifact_type: str,
    filename: str,
    payload: dict[str, Any],
    version: int = 1,
    metadata: dict[str, Any] | None = None,
) -> tuple[ArtifactVersion, str]:
    raw = canonical_json_bytes(payload)
    digest = sha256_hex(raw)
    existing = session.scalar(
        select(ArtifactVersion).where(
            ArtifactVersion.tenant_id == tenant_id,
            ArtifactVersion.task_id == task_id,
            ArtifactVersion.artifact_type == artifact_type,
            ArtifactVersion.version == version,
        )
    )
    key = build_artifact_key(tenant_id, project_id, task_id, f"v{version}_{filename}")
    object_store.put_bytes(tenant_id, key, raw, content_type="application/json")
    if existing is not None:
        existing.sha256 = digest
        existing.object_key = key
        existing.metadata_json = dict(metadata or {})
        session.flush()
        return existing, digest

    art = ArtifactVersion(
        id=f"art_{secrets.token_hex(8)}",
        tenant_id=tenant_id,
        task_id=task_id,
        run_id=run_id,
        artifact_type=artifact_type,
        version=version,
        schema_version="1.1",
        sha256=digest,
        object_key=key,
        metadata_json=dict(metadata or {}),
    )
    session.add(art)
    session.flush()
    return art, digest


def _default_ba(session: Session, state: SdlcGraphState, ctx: OrchestratorContext) -> dict[str, Any]:
    task = session.get(Task, state["task_id"])
    assert task is not None
    spec_payload = {
        "schema_version": "1.1",
        "task_id": task.id,
        "title": task.title,
        "requirement": task.requirement_text,
        "acceptance_criteria": [f"Satisfy requirement: {task.title}"],
        "clarification_questions": list(state.get("clarification_questions") or []),
    }
    _art, digest = _save_json_artifact(
        session,
        object_store=ctx.object_store,
        tenant_id=state["tenant_id"],
        project_id=state["project_id"],
        task_id=state["task_id"],
        run_id=state["run_id"],
        artifact_type="TaskSpec",
        filename="task_spec.json",
        payload=spec_payload,
        version=int(state.get("spec_version") or 1),
    )
    run = session.get(TaskRun, state["run_id"])
    if run:
        run.spec_id = _art.id
        run.spec_version = _art.version
        run.spec_sha256 = digest
    return {"spec_sha256": digest, "spec_version": _art.version}


def _default_architect(session: Session, state: SdlcGraphState, ctx: OrchestratorContext) -> dict[str, Any]:
    _save_json_artifact(
        session,
        object_store=ctx.object_store,
        tenant_id=state["tenant_id"],
        project_id=state["project_id"],
        task_id=state["task_id"],
        run_id=state["run_id"],
        artifact_type="ArchitectDesign",
        filename="architect_design.json",
        payload={"spec_sha256": state.get("spec_sha256"), "modules": []},
    )
    return {}


def _default_qa_critique(session: Session, state: SdlcGraphState, ctx: OrchestratorContext) -> dict[str, Any]:
    _save_json_artifact(
        session,
        object_store=ctx.object_store,
        tenant_id=state["tenant_id"],
        project_id=state["project_id"],
        task_id=state["task_id"],
        run_id=state["run_id"],
        artifact_type="TestPlan",
        filename="test_plan.json",
        payload={"spec_sha256": state.get("spec_sha256"), "verdict": "READY", "cases": []},
    )
    return {"qa_verdict": "READY"}


def _default_execution_plan(session: Session, state: SdlcGraphState, ctx: OrchestratorContext) -> dict[str, Any]:
    plan_payload = {
        "schema_version": "1.1",
        "spec_sha256": state.get("spec_sha256"),
        "repo_ids": state.get("repo_ids", []),
        "subtasks": [],
    }
    art, digest = _save_json_artifact(
        session,
        object_store=ctx.object_store,
        tenant_id=state["tenant_id"],
        project_id=state["project_id"],
        task_id=state["task_id"],
        run_id=state["run_id"],
        artifact_type="ExecutionPlan",
        filename="execution_plan.json",
        payload=plan_payload,
    )
    run = session.get(TaskRun, state["run_id"])
    if run:
        run.plan_id = art.id
        run.plan_sha256 = digest
    return {"plan_sha256": digest}


def _default_coding(session: Session, state: SdlcGraphState, ctx: OrchestratorContext) -> dict[str, Any]:
    patch_payload = {"diff": "", "changed_files": []}
    _art, digest = _save_json_artifact(
        session,
        object_store=ctx.object_store,
        tenant_id=state["tenant_id"],
        project_id=state["project_id"],
        task_id=state["task_id"],
        run_id=state["run_id"],
        artifact_type="Patch",
        filename="patch.json",
        payload=patch_payload,
        version=int(state.get("implementation_attempts") or 0) + 1,
    )
    run = session.get(TaskRun, state["run_id"])
    if run:
        run.patch_sha256 = digest
    return {
        "patch_sha256": digest,
        "implementation_attempts": int(state.get("implementation_attempts") or 0) + 1,
    }


def _default_verify(session: Session, state: SdlcGraphState, ctx: OrchestratorContext) -> dict[str, Any]:
    report_payload: dict[str, Any] = {
        "verdict": "PASS",
        "checks": [],
        "patch_sha256": state.get("patch_sha256"),
    }
    _save_json_artifact(
        session,
        object_store=ctx.object_store,
        tenant_id=state["tenant_id"],
        project_id=state["project_id"],
        task_id=state["task_id"],
        run_id=state["run_id"],
        artifact_type="VerificationReport",
        filename="verification_report.json",
        payload=report_payload,
    )
    run = session.get(TaskRun, state["run_id"])
    if run:
        run.verification_verdict = "PASS"
    return {"verification_verdict": "PASS"}


def _default_review(session: Session, state: SdlcGraphState, ctx: OrchestratorContext) -> dict[str, Any]:
    _save_json_artifact(
        session,
        object_store=ctx.object_store,
        tenant_id=state["tenant_id"],
        project_id=state["project_id"],
        task_id=state["task_id"],
        run_id=state["run_id"],
        artifact_type="ReviewReport",
        filename="review_report.json",
        payload={"verdict": "APPROVED", "findings": []},
    )
    return {"review_verdict": "APPROVED"}


def _default_publish_pr(session: Session, state: SdlcGraphState, ctx: OrchestratorContext) -> dict[str, Any]:
    return {"pr_urls": ["https://github.example/pull/1"]}


@dataclass
class StageExecutors:
    ba_specify: StageFn = field(default=_default_ba)
    ba_refine: StageFn = field(default=_default_ba)
    architect_design: StageFn = field(default=_default_architect)
    qa_critique: StageFn = field(default=_default_qa_critique)
    execution_plan: StageFn = field(default=_default_execution_plan)
    code_and_tdd: StageFn = field(default=_default_coding)
    independent_verify: StageFn = field(default=_default_verify)
    code_review: StageFn = field(default=_default_review)
    publish_pr: StageFn = field(default=_default_publish_pr)


def _check_cancelled(session: Session, task_id: str) -> bool:
    task = session.get(Task, task_id)
    return task is not None and task.status == "CANCELLED"


def _update_stage(session: Session, state: SdlcGraphState, stage: str, status: str | None = None) -> None:
    task = session.get(Task, state["task_id"])
    run = session.get(TaskRun, state["run_id"])
    if task:
        task.current_stage = stage
        if status and task.status != "CANCELLED":
            task.status = status
    if run:
        run.current_node = stage


def build_sdlc_graph(
    *,
    ctx: OrchestratorContext,
    executors: StageExecutors | None = None,
    checkpointer: TenantSqlAlchemyCheckpointSaver | None = None,
) -> Any:
    """Build and compile the 16-stage LangGraph SDLC state machine with persistent checkpoints."""
    ex = executors or StageExecutors()
    saver = checkpointer or TenantSqlAlchemyCheckpointSaver(ctx.session_factory)
    builder = StateGraph(SdlcGraphState)

    def _fire_fault(stage: str, state: SdlcGraphState) -> None:
        if ctx.fault_hook is not None:
            ctx.fault_hook(stage, state)

    def intake_and_budget(state: SdlcGraphState) -> dict[str, Any]:
        _fire_fault("intake_and_budget", state)
        with tenant_transaction(ctx.session_factory, state["tenant_id"]) as s:
            if _check_cancelled(s, state["task_id"]):
                return {"stop_reason": "CANCELLED"}
            check_kill_switches(s, tenant_id=state["tenant_id"], workflow=state.get("task_type"))

            task = s.get(Task, state["task_id"])
            run = s.get(TaskRun, state["run_id"])
            assert task is not None and run is not None
            _update_stage(s, state, "intake_and_budget", "RUNNING")

            base_commits: dict[str, str] = {}
            for rid in state.get("repo_ids", []):
                assert_repo_dispatchable(s, tenant_id=state["tenant_id"], repo_id=rid)
                snap, _ = load_snapshot_repo_context(
                    s, tenant_id=state["tenant_id"], repo_id=rid, object_store=ctx.object_store
                )
                base_commits[rid] = snap.commit_sha

            if not run.base_commits_json:
                run.base_commits_json = base_commits
            if "policy_digest" not in (run.frozen_policy_json or {}):
                snap_policy = freeze_policy_snapshot(
                    s, tenant_id=state["tenant_id"], project_id=state["project_id"]
                )
                run.frozen_policy_json = {**snap_policy, **dict(run.frozen_policy_json or {})}

            try:
                reserve_quota(
                    s,
                    tenant_id=state["tenant_id"],
                    task_id=task.id,
                    run_id=run.id,
                    amount_cents=max(10, min(task.budget_cap_cents, 100)),
                    idempotency_key=f"reserve:{run.id}",
                )
            except InsufficientBudgetError as exc:
                task.status = "WAITING_BUDGET"
                task.error_code = "insufficient_budget"
                task.error_message = str(exc)
                emit_task_event(
                    s,
                    tenant_id=state["tenant_id"],
                    task_id=task.id,
                    run_id=run.id,
                    event_type="task.waiting_budget",
                    stage="intake_and_budget",
                    payload={"error": str(exc)},
                )
                return {"stop_reason": "WAITING_BUDGET", "error_code": "insufficient_budget", "error_message": str(exc)}

            emit_task_event(
                s,
                tenant_id=state["tenant_id"],
                task_id=task.id,
                run_id=run.id,
                event_type="stage.completed",
                stage="intake_and_budget",
                payload={"base_commits": base_commits},
            )
            return {"current_stage": "intake_and_budget"}

    def snapshot_context(state: SdlcGraphState) -> dict[str, Any]:
        _fire_fault("snapshot_context", state)
        with tenant_transaction(ctx.session_factory, state["tenant_id"]) as s:
            if _check_cancelled(s, state["task_id"]):
                return {"stop_reason": "CANCELLED"}
            _update_stage(s, state, "snapshot_context", "RUNNING")
            contexts: dict[str, Any] = {}
            for rid in state.get("repo_ids", []):
                snap, data = load_snapshot_repo_context(
                    s, tenant_id=state["tenant_id"], repo_id=rid, object_store=ctx.object_store
                )
                contexts[rid] = {"snapshot_id": snap.id, "commit_sha": snap.commit_sha, "language": data.get("language")}
            _save_json_artifact(
                s,
                object_store=ctx.object_store,
                tenant_id=state["tenant_id"],
                project_id=state["project_id"],
                task_id=state["task_id"],
                run_id=state["run_id"],
                artifact_type="RepoContext",
                filename="repo_contexts.json",
                payload=contexts,
            )
            emit_task_event(
                s,
                tenant_id=state["tenant_id"],
                task_id=state["task_id"],
                run_id=state["run_id"],
                event_type="stage.completed",
                stage="snapshot_context",
                payload={"repos": list(contexts.keys())},
            )
            return {"current_stage": "snapshot_context"}

    def ba_specify(state: SdlcGraphState) -> dict[str, Any]:
        _fire_fault("model_call", state)
        with tenant_transaction(ctx.session_factory, state["tenant_id"]) as s:
            if _check_cancelled(s, state["task_id"]):
                return {"stop_reason": "CANCELLED"}
            _update_stage(s, state, "ba_specify", "BA_CLARIFYING")
            out = ex.ba_specify(s, state, ctx)
            emit_task_event(
                s,
                tenant_id=state["tenant_id"],
                task_id=state["task_id"],
                run_id=state["run_id"],
                event_type="artifact.TaskSpec",
                stage="ba_specify",
                payload={"spec_sha256": out.get("spec_sha256")},
            )
            return {"current_stage": "ba_specify", **out}

    def clarify_gate(state: SdlcGraphState) -> dict[str, Any]:
        questions = list(state.get("clarification_questions") or [])
        if not questions or state.get("clarification_answers"):
            return {"current_stage": "clarify_gate"}

        with tenant_transaction(ctx.session_factory, state["tenant_id"]) as s:
            if _check_cancelled(s, state["task_id"]):
                return {"stop_reason": "CANCELLED"}
            _update_stage(s, state, "clarify_gate", "WAITING_INPUT")
            emit_task_event(
                s,
                tenant_id=state["tenant_id"],
                task_id=state["task_id"],
                run_id=state["run_id"],
                event_type="gate.clarification_required",
                stage="clarify_gate",
                payload={"questions": questions},
            )

        answers = interrupt({"gate": "clarification", "task_id": state["task_id"], "questions": questions})
        answered_state: SdlcGraphState = {**state, "clarification_answers": dict(answers or {})}
        with tenant_transaction(ctx.session_factory, state["tenant_id"]) as s:
            if _check_cancelled(s, state["task_id"]):
                return {"stop_reason": "CANCELLED"}
            out = ex.ba_refine(s, answered_state, ctx)
            _update_stage(s, state, "clarify_gate", "RUNNING")
            return {"current_stage": "clarify_gate", "clarification_answers": answered_state["clarification_answers"], **out}

    def architect_design(state: SdlcGraphState) -> dict[str, Any]:
        with tenant_transaction(ctx.session_factory, state["tenant_id"]) as s:
            if _check_cancelled(s, state["task_id"]):
                return {"stop_reason": "CANCELLED"}
            _update_stage(s, state, "architect_design", "ARCHITECTING")
            out = ex.architect_design(s, state, ctx)
            emit_task_event(
                s,
                tenant_id=state["tenant_id"],
                task_id=state["task_id"],
                run_id=state["run_id"],
                event_type="stage.completed",
                stage="architect_design",
                payload={},
            )
            return {"current_stage": "architect_design", **out}

    def qa_critique_testplan(state: SdlcGraphState) -> dict[str, Any]:
        with tenant_transaction(ctx.session_factory, state["tenant_id"]) as s:
            if _check_cancelled(s, state["task_id"]):
                return {"stop_reason": "CANCELLED"}
            _update_stage(s, state, "qa_critique_testplan", "SPEC_CRITIQUE")
            out = ex.qa_critique(s, state, ctx)
            emit_task_event(
                s,
                tenant_id=state["tenant_id"],
                task_id=state["task_id"],
                run_id=state["run_id"],
                event_type="stage.completed",
                stage="qa_critique_testplan",
                payload={"qa_verdict": out.get("qa_verdict", "READY")},
            )
            return {"current_stage": "qa_critique_testplan", **out}

    def spec_approval_gate(state: SdlcGraphState) -> dict[str, Any]:
        if state.get("fast_track"):
            return {"current_stage": "spec_approval_gate", "spec_approved": True}

        _fire_fault("approval_wait", state)
        with tenant_transaction(ctx.session_factory, state["tenant_id"]) as s:
            if _check_cancelled(s, state["task_id"]):
                return {"stop_reason": "CANCELLED"}
            run = s.get(TaskRun, state["run_id"])
            assert run is not None
            prompt = serialize_gate_prompt(run, "spec")
            _update_stage(s, state, "spec_approval_gate", "WAITING_APPROVAL")
            emit_task_event(
                s,
                tenant_id=state["tenant_id"],
                task_id=state["task_id"],
                run_id=state["run_id"],
                event_type="gate.approval_required",
                stage="spec_approval_gate",
                payload=prompt,
            )

        decision_payload = interrupt(prompt)
        with tenant_transaction(ctx.session_factory, state["tenant_id"]) as s:
            if _check_cancelled(s, state["task_id"]):
                return {"stop_reason": "CANCELLED"}
            run = s.get(TaskRun, state["run_id"])
            assert run is not None
            if (decision_payload or {}).get("decision") == "rejected":
                return {"stop_reason": "REJECTED", "spec_approved": False}
            assert_approval_valid_for_resume(s, tenant_id=state["tenant_id"], run=run, gate_type="spec")
            _update_stage(s, state, "spec_approval_gate", "RUNNING")
            return {"current_stage": "spec_approval_gate", "spec_approved": True}

    def execution_plan(state: SdlcGraphState) -> dict[str, Any]:
        with tenant_transaction(ctx.session_factory, state["tenant_id"]) as s:
            if _check_cancelled(s, state["task_id"]):
                return {"stop_reason": "CANCELLED"}
            _update_stage(s, state, "execution_plan", "RUNNING")
            out = ex.execution_plan(s, state, ctx)
            emit_task_event(
                s,
                tenant_id=state["tenant_id"],
                task_id=state["task_id"],
                run_id=state["run_id"],
                event_type="artifact.ExecutionPlan",
                stage="execution_plan",
                payload={"plan_sha256": out.get("plan_sha256")},
            )
            return {"current_stage": "execution_plan", **out}

    def plan_approval_gate(state: SdlcGraphState) -> dict[str, Any]:
        if state.get("fast_track"):
            return {"current_stage": "plan_approval_gate", "plan_approved": True}

        with tenant_transaction(ctx.session_factory, state["tenant_id"]) as s:
            if _check_cancelled(s, state["task_id"]):
                return {"stop_reason": "CANCELLED"}
            run = s.get(TaskRun, state["run_id"])
            assert run is not None
            prompt = serialize_gate_prompt(run, "plan")
            _update_stage(s, state, "plan_approval_gate", "WAITING_APPROVAL")
            emit_task_event(
                s,
                tenant_id=state["tenant_id"],
                task_id=state["task_id"],
                run_id=state["run_id"],
                event_type="gate.approval_required",
                stage="plan_approval_gate",
                payload=prompt,
            )

        decision_payload = interrupt(prompt)
        with tenant_transaction(ctx.session_factory, state["tenant_id"]) as s:
            if _check_cancelled(s, state["task_id"]):
                return {"stop_reason": "CANCELLED"}
            run = s.get(TaskRun, state["run_id"])
            assert run is not None
            if (decision_payload or {}).get("decision") == "rejected":
                return {"stop_reason": "REJECTED", "plan_approved": False}
            assert_approval_valid_for_resume(s, tenant_id=state["tenant_id"], run=run, gate_type="plan")
            _update_stage(s, state, "plan_approval_gate", "RUNNING")
            return {"current_stage": "plan_approval_gate", "plan_approved": True}

    def code_and_tdd(state: SdlcGraphState) -> dict[str, Any]:
        _fire_fault("vm_create", state)
        with tenant_transaction(ctx.session_factory, state["tenant_id"]) as s:
            if _check_cancelled(s, state["task_id"]):
                return {"stop_reason": "CANCELLED"}
            _update_stage(s, state, "code_and_tdd", "EXECUTING")
            out = ex.code_and_tdd(s, state, ctx)
            emit_task_event(
                s,
                tenant_id=state["tenant_id"],
                task_id=state["task_id"],
                run_id=state["run_id"],
                event_type="stage.completed",
                stage="code_and_tdd",
                payload={"patch_sha256": out.get("patch_sha256"), "attempts": out.get("implementation_attempts")},
            )
            return {"current_stage": "code_and_tdd", **out}

    def independent_verify(state: SdlcGraphState) -> dict[str, Any]:
        with tenant_transaction(ctx.session_factory, state["tenant_id"]) as s:
            if _check_cancelled(s, state["task_id"]):
                return {"stop_reason": "CANCELLED"}
            _update_stage(s, state, "independent_verify", "VERIFYING")
            out = ex.independent_verify(s, state, ctx)
            emit_task_event(
                s,
                tenant_id=state["tenant_id"],
                task_id=state["task_id"],
                run_id=state["run_id"],
                event_type="verification.completed",
                stage="independent_verify",
                payload={"verdict": out.get("verification_verdict")},
            )
            return {"current_stage": "independent_verify", **out}

    def code_review(state: SdlcGraphState) -> dict[str, Any]:
        with tenant_transaction(ctx.session_factory, state["tenant_id"]) as s:
            if _check_cancelled(s, state["task_id"]):
                return {"stop_reason": "CANCELLED"}
            _update_stage(s, state, "code_review", "REVIEWING")
            out = ex.code_review(s, state, ctx)
            emit_task_event(
                s,
                tenant_id=state["tenant_id"],
                task_id=state["task_id"],
                run_id=state["run_id"],
                event_type="review.completed",
                stage="code_review",
                payload={"verdict": out.get("review_verdict")},
            )
            return {"current_stage": "code_review", **out}

    def delivery_approval_gate(state: SdlcGraphState) -> dict[str, Any]:
        with tenant_transaction(ctx.session_factory, state["tenant_id"]) as s:
            if _check_cancelled(s, state["task_id"]):
                return {"stop_reason": "CANCELLED"}
            run = s.get(TaskRun, state["run_id"])
            assert run is not None
            prompt = serialize_gate_prompt(run, "delivery")
            _update_stage(s, state, "delivery_approval_gate", "WAITING_APPROVAL")
            emit_task_event(
                s,
                tenant_id=state["tenant_id"],
                task_id=state["task_id"],
                run_id=state["run_id"],
                event_type="gate.approval_required",
                stage="delivery_approval_gate",
                payload=prompt,
            )

        decision_payload = interrupt(prompt)
        with tenant_transaction(ctx.session_factory, state["tenant_id"]) as s:
            if _check_cancelled(s, state["task_id"]):
                return {"stop_reason": "CANCELLED"}
            run = s.get(TaskRun, state["run_id"])
            assert run is not None
            if (decision_payload or {}).get("decision") == "rejected":
                return {"stop_reason": "REJECTED", "delivery_approved": False}
            assert_approval_valid_for_resume(s, tenant_id=state["tenant_id"], run=run, gate_type="delivery")
            _update_stage(s, state, "delivery_approval_gate", "RUNNING")
            return {"current_stage": "delivery_approval_gate", "delivery_approved": True}

    def publish_pr(state: SdlcGraphState) -> dict[str, Any]:
        _fire_fault("publish_intent", state)
        with tenant_transaction(ctx.session_factory, state["tenant_id"]) as s:
            if _check_cancelled(s, state["task_id"]):
                return {"stop_reason": "CANCELLED"}
            _update_stage(s, state, "publish_pr", "PR_CREATING")
            try:
                out = ex.publish_pr(s, state, ctx)
            except BaselineStaleError as exc:
                task = s.get(Task, state["task_id"])
                if task:
                    task.status = "BASELINE_STALE"
                    task.error_code = "baseline_stale"
                    task.error_message = str(exc)
                emit_task_event(
                    s,
                    tenant_id=state["tenant_id"],
                    task_id=state["task_id"],
                    run_id=state["run_id"],
                    event_type="task.baseline_stale",
                    stage="publish_pr",
                    payload={"error": str(exc)},
                )
                return {"stop_reason": "BASELINE_STALE", "error_code": "baseline_stale", "error_message": str(exc)}

            emit_task_event(
                s,
                tenant_id=state["tenant_id"],
                task_id=state["task_id"],
                run_id=state["run_id"],
                event_type="pr.created",
                stage="publish_pr",
                payload={"pr_urls": out.get("pr_urls", [])},
            )
            return {"current_stage": "publish_pr", **out}

    def preview_and_release_gate(state: SdlcGraphState) -> dict[str, Any]:
        if not state.get("require_release_approval"):
            return {"current_stage": "preview_and_release_gate"}

        with tenant_transaction(ctx.session_factory, state["tenant_id"]) as s:
            if _check_cancelled(s, state["task_id"]):
                return {"stop_reason": "CANCELLED"}
            run = s.get(TaskRun, state["run_id"])
            assert run is not None
            prompt = serialize_gate_prompt(run, "release")
            _update_stage(s, state, "preview_and_release_gate", "WAITING_APPROVAL")
            emit_task_event(
                s,
                tenant_id=state["tenant_id"],
                task_id=state["task_id"],
                run_id=state["run_id"],
                event_type="gate.approval_required",
                stage="preview_and_release_gate",
                payload=prompt,
            )

        decision_payload = interrupt(prompt)
        with tenant_transaction(ctx.session_factory, state["tenant_id"]) as s:
            if _check_cancelled(s, state["task_id"]):
                return {"stop_reason": "CANCELLED"}
            run = s.get(TaskRun, state["run_id"])
            assert run is not None
            if (decision_payload or {}).get("decision") == "rejected":
                return {"stop_reason": "REJECTED", "release_approved": False}
            assert_approval_valid_for_resume(s, tenant_id=state["tenant_id"], run=run, gate_type="release")
            return {"current_stage": "preview_and_release_gate", "release_approved": True}

    def cleanup_and_settle(state: SdlcGraphState) -> dict[str, Any]:
        with tenant_transaction(ctx.session_factory, state["tenant_id"]) as s:
            cleanup_task_resources(
                s,
                tenant_id=state["tenant_id"],
                task_id=state["task_id"],
                deleter=ctx.resource_deleter,
            )
            res = s.scalar(
                select(QuotaReservation).where(
                    QuotaReservation.tenant_id == state["tenant_id"],
                    QuotaReservation.run_id == state["run_id"],
                    QuotaReservation.status == "held",
                )
            )
            spent = sum(
                int(u.customer_cost_cents)
                for u in s.scalars(
                    select(RequestUsage).where(
                        RequestUsage.tenant_id == state["tenant_id"],
                        RequestUsage.run_id == state["run_id"],
                    )
                )
            )
            upstream = sum(
                int(u.upstream_cost_microcents)
                for u in s.scalars(
                    select(RequestUsage).where(
                        RequestUsage.tenant_id == state["tenant_id"],
                        RequestUsage.run_id == state["run_id"],
                    )
                )
            )
            task = s.get(Task, state["task_id"])
            run = s.get(TaskRun, state["run_id"])
            if res is not None:
                confirmed = max(1 if (state.get("pr_urls") or spent > 0) else 0, spent)
                settle_reservation(
                    s,
                    tenant_id=state["tenant_id"],
                    reservation_id=res.id,
                    confirmed_customer_cents=confirmed,
                    upstream_cost_cents=upstream // 100,
                    idempotency_key=f"settle:{state['run_id']}",
                    description=f"Task {state['task_id']} execution settlement",
                )
                if task:
                    task.spent_cents = confirmed

            stop = state.get("stop_reason")
            verdict = state.get("verification_verdict")
            final_status = "DELIVERED"
            if _check_cancelled(s, state["task_id"]) or stop == "CANCELLED":
                final_status = "CANCELLED"
            elif stop in ("WAITING_BUDGET", "BASELINE_STALE", "SPEC_CHANGE_REQUIRED"):
                final_status = stop
            elif stop in ("REJECTED", "VERIFICATION_FAILED") or verdict == "FAIL":
                final_status = "FAILED"

            if task:
                task.status = final_status
                task.current_stage = "cleanup_and_settle"
                if state.get("error_code"):
                    task.error_code = state["error_code"]
                if state.get("error_message"):
                    task.error_message = state["error_message"]
            if run:
                run.status = final_status
                run.current_node = "cleanup_and_settle"
                run.completed_at = datetime.now(UTC)

            emit_task_event(
                s,
                tenant_id=state["tenant_id"],
                task_id=state["task_id"],
                run_id=state["run_id"],
                event_type="task.finished",
                stage="cleanup_and_settle",
                payload={"status": final_status, "pr_urls": state.get("pr_urls", [])},
            )
            return {"current_stage": "cleanup_and_settle"}

    # Register nodes
    for name, fn in [
        ("intake_and_budget", intake_and_budget),
        ("snapshot_context", snapshot_context),
        ("ba_specify", ba_specify),
        ("clarify_gate", clarify_gate),
        ("architect_design", architect_design),
        ("qa_critique_testplan", qa_critique_testplan),
        ("spec_approval_gate", spec_approval_gate),
        ("execution_plan", execution_plan),
        ("plan_approval_gate", plan_approval_gate),
        ("code_and_tdd", code_and_tdd),
        ("independent_verify", independent_verify),
        ("code_review", code_review),
        ("delivery_approval_gate", delivery_approval_gate),
        ("publish_pr", publish_pr),
        ("preview_and_release_gate", preview_and_release_gate),
        ("cleanup_and_settle", cleanup_and_settle),
    ]:
        builder.add_node(name, fn)

    def _route_after_intake(state: SdlcGraphState) -> str:
        stop = state.get("stop_reason")
        if stop in ("CANCELLED", "WAITING_BUDGET"):
            return "cleanup_and_settle"
        return "snapshot_context"

    def _route_or_cleanup(next_node: str) -> Callable[[SdlcGraphState], str]:
        def _inner(state: SdlcGraphState) -> str:
            if state.get("stop_reason") in (
                "CANCELLED",
                "REJECTED",
                "SPEC_CHANGE_REQUIRED",
                "BASELINE_STALE",
                "VERIFICATION_FAILED",
            ):
                return "cleanup_and_settle"
            return next_node

        return _inner

    def _route_after_verify(state: SdlcGraphState) -> str:
        if state.get("stop_reason") in (
            "CANCELLED",
            "REJECTED",
            "SPEC_CHANGE_REQUIRED",
            "VERIFICATION_FAILED",
        ):
            return "cleanup_and_settle"
        verdict = state.get("verification_verdict")
        attempts = int(state.get("implementation_attempts") or 0)
        max_rounds = int(state.get("max_repair_rounds") or 2)
        if verdict == "FAIL":
            if attempts < max_rounds:
                return "code_and_tdd"
            return "cleanup_and_settle"
        return "code_review"

    builder.add_edge(START, "intake_and_budget")
    builder.add_conditional_edges("intake_and_budget", _route_after_intake)
    builder.add_conditional_edges("snapshot_context", _route_or_cleanup("ba_specify"))
    builder.add_conditional_edges("ba_specify", _route_or_cleanup("clarify_gate"))
    builder.add_conditional_edges("clarify_gate", _route_or_cleanup("architect_design"))
    builder.add_conditional_edges("architect_design", _route_or_cleanup("qa_critique_testplan"))
    builder.add_conditional_edges("qa_critique_testplan", _route_or_cleanup("spec_approval_gate"))
    builder.add_conditional_edges("spec_approval_gate", _route_or_cleanup("execution_plan"))
    builder.add_conditional_edges("execution_plan", _route_or_cleanup("plan_approval_gate"))
    builder.add_conditional_edges("plan_approval_gate", _route_or_cleanup("code_and_tdd"))
    builder.add_conditional_edges("code_and_tdd", _route_or_cleanup("independent_verify"))
    builder.add_conditional_edges("independent_verify", _route_after_verify)
    builder.add_conditional_edges("code_review", _route_or_cleanup("delivery_approval_gate"))
    builder.add_conditional_edges("delivery_approval_gate", _route_or_cleanup("publish_pr"))
    builder.add_conditional_edges("publish_pr", _route_or_cleanup("preview_and_release_gate"))
    builder.add_edge("preview_and_release_gate", "cleanup_and_settle")
    builder.add_edge("cleanup_and_settle", END)

    return builder.compile(checkpointer=saver)


def worktree_path_for(settings: Settings, tenant_id: str, run_id: str) -> Path:
    return Path(settings.worktree_root) / "runs" / tenant_id / run_id
