"""Workflow state rules, fast-track risk guardrails, and approval invalidation."""

from __future__ import annotations

import secrets
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_sdlc_saas.db.models import Approval, TaskRun
from ai_sdlc_saas.errors import ConflictError, ForbiddenError, ValidationError
from ai_sdlc_saas.governance.audit import record_audit_event
from ai_sdlc_saas.identity.service import verify_active_actor_access
from ai_sdlc_saas.security.rbac import has_permission

VALID_TASK_TYPES = ("bugfix", "feature", "refactor", "tests", "docs", "chore")
VALID_RISK_LEVELS = ("low", "standard", "high")

GATE_PERMISSION_MAP: dict[str, str] = {
    "spec": "spec:approve",
    "plan": "plan:approve",
    "delivery": "delivery:approve",
    "release": "release:approve",
}

_HIGH_RISK_KEYWORDS = ("auth", "oauth", "jwt", "rbac", "migration", "sql", "security", "billing", "payment", "infra", "workflow")


def resolve_effective_risk(
    *,
    task_type: str,
    requested_risk: str,
    requirement_text: str,
    repo_count: int,
    requested_fast_track: bool,
) -> tuple[str, bool]:
    """Prevent high-risk or multi-repo tasks from self-downgrading into `fast_track`."""
    if task_type not in VALID_TASK_TYPES:
        raise ValidationError(f"Unsupported task_type {task_type!r}.")
    if requested_risk not in VALID_RISK_LEVELS:
        raise ValidationError(f"Unsupported risk_level {requested_risk!r}.")

    lowered = requirement_text.lower()
    inferred_high = repo_count > 1 or any(kw in lowered for kw in _HIGH_RISK_KEYWORDS)
    effective_risk = "high" if (requested_risk == "high" or inferred_high) else requested_risk

    if effective_risk == "high" or task_type == "feature":
        # High-risk tasks & full features require full spec/plan gates; no silent fast-track bypass.
        fast_track = False
    elif requested_fast_track and effective_risk == "low" and task_type in ("bugfix", "docs", "tests", "chore"):
        fast_track = True
    else:
        fast_track = False

    return effective_risk, fast_track


def expected_digest_for_gate(run: TaskRun, gate_type: str) -> str:
    if gate_type == "spec":
        if not run.spec_sha256:
            raise ConflictError("Spec artifact is not yet generated for approval.")
        return run.spec_sha256
    if gate_type == "plan":
        if not run.plan_sha256:
            raise ConflictError("ExecutionPlan artifact is not yet generated for approval.")
        return run.plan_sha256
    if gate_type in ("delivery", "release"):
        if not run.patch_sha256:
            raise ConflictError("Verified patch digest is not yet available for approval.")
        return run.patch_sha256
    raise ValidationError(f"Unsupported gate_type {gate_type!r}.")


def record_gate_approval(
    session: Session,
    *,
    tenant_id: str,
    actor_user_id: str,
    actor_role: str,
    run: TaskRun,
    gate_type: str,
    decision: str,
    target_digest: str,
    base_commit_sha: str,
    policy_version: str,
    idempotency_key: str,
    comment: str = "",
) -> Approval:
    """Record a bound gate decision after RBAC + exact digest/baseline/policy freshness checks."""
    req_perm = GATE_PERMISSION_MAP.get(gate_type)
    if req_perm is None:
        raise ValidationError(f"Unsupported gate_type {gate_type!r}.")
    if not has_permission(actor_role, req_perm):
        raise ForbiddenError(f"Role {actor_role!r} lacks permission {req_perm!r} for {gate_type} gate.")
    if decision not in ("approved", "rejected", "changes_requested"):
        raise ValidationError(f"Unsupported decision {decision!r}.")

    existing = session.scalar(
        select(Approval).where(Approval.tenant_id == tenant_id, Approval.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return existing

    current_digest = expected_digest_for_gate(run, gate_type)
    if target_digest != current_digest:
        raise ConflictError(
            f"Stale {gate_type} approval: target_digest {target_digest[:12]} does not match current run artifact {current_digest[:12]}.",
            details={"expected_digest": current_digest, "submitted_digest": target_digest},
        )

    known_bases = set((run.base_commits_json or {}).values())
    if known_bases and base_commit_sha not in known_bases and base_commit_sha != "docs-base":
        raise ConflictError(
            f"Stale baseline commit SHA {base_commit_sha!r} does not match current run commits.",
            details={"known_base_commits": sorted(known_bases), "submitted_base_commit": base_commit_sha},
        )

    active_policy_ver = str((run.frozen_policy_json or {}).get("policy_digest") or "policy-v1")
    if policy_version and policy_version not in (active_policy_ver, "policy-v1"):
        raise ConflictError(
            "Policy version mismatch on gate approval.",
            details={"expected_policy_version": active_policy_ver, "submitted": policy_version},
        )

    approval = Approval(
        id=f"apr_{secrets.token_hex(8)}",
        tenant_id=tenant_id,
        task_id=run.task_id,
        run_id=run.id,
        gate_type=gate_type,
        decision=decision,
        target_digest=target_digest,
        base_commit_sha=base_commit_sha,
        policy_version=active_policy_ver,
        actor_user_id=actor_user_id,
        comment=comment[:2000],
        idempotency_key=idempotency_key,
        is_valid=True,
    )
    session.add(approval)
    record_audit_event(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action=f"gate.{gate_type}.{decision}",
        target_type="task_run",
        target_id=run.id,
        details={"task_id": run.task_id, "target_digest": target_digest, "comment": comment[:200]},
    )
    session.flush()
    return approval


def invalidate_run_approvals(
    session: Session,
    *,
    tenant_id: str,
    task_id: str,
    reason: str,
    gate_types: tuple[str, ...] | None = None,
) -> int:
    stmt = select(Approval).where(
        Approval.tenant_id == tenant_id,
        Approval.task_id == task_id,
        Approval.is_valid.is_(True),
    )
    if gate_types:
        stmt = stmt.where(Approval.gate_type.in_(gate_types))
    rows = list(session.scalars(stmt))
    for row in rows:
        row.is_valid = False
        row.invalidated_reason = reason[:250]
    session.flush()
    return len(rows)


def assert_approval_valid_for_resume(
    session: Session,
    *,
    tenant_id: str,
    run: TaskRun,
    gate_type: str,
) -> Approval:
    """Re-verify valid approval AND active actor membership/role before resuming past a human gate."""
    current_digest = expected_digest_for_gate(run, gate_type)
    approval = session.scalar(
        select(Approval)
        .where(
            Approval.tenant_id == tenant_id,
            Approval.run_id == run.id,
            Approval.gate_type == gate_type,
            Approval.decision == "approved",
            Approval.is_valid.is_(True),
            Approval.target_digest == current_digest,
        )
        .order_by(Approval.created_at.desc())
    )
    if approval is None:
        raise ConflictError(f"No valid {gate_type} approval bound to current digest {current_digest[:12]}.")

    req_perm = GATE_PERMISSION_MAP[gate_type]
    verify_active_actor_access(
        session,
        tenant_id=tenant_id,
        user_id=approval.actor_user_id,
        required_permission=req_perm,
    )
    return approval


def serialize_gate_prompt(run: TaskRun, gate_type: str) -> dict[str, Any]:
    digest = ""
    if gate_type == "spec":
        digest = run.spec_sha256 or ""
    elif gate_type == "plan":
        digest = run.plan_sha256 or ""
    else:
        digest = run.patch_sha256 or ""
    base_commits = run.base_commits_json or {}
    first_base = next(iter(base_commits.values()), "docs-base")
    return {
        "gate": gate_type,
        "task_id": run.task_id,
        "run_id": run.id,
        "target_digest": digest,
        "base_commit_sha": first_base,
        "policy_version": str((run.frozen_policy_json or {}).get("policy_digest") or "policy-v1"),
    }
