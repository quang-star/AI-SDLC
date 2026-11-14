"""Human-in-the-loop gate approval endpoints (spec, plan, delivery, release)."""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ai_sdlc_saas.api.deps import get_auth_context, get_db, require_permission
from ai_sdlc_saas.db.models import Membership, Tenant
from ai_sdlc_saas.identity.service import AuthContext
from ai_sdlc_saas.orchestrator.worker import list_task_approvals, submit_gate_decision

router = APIRouter(prefix="/api/v1/tasks/{task_id}/approvals", tags=["approvals"])


class GateApprovalRequest(BaseModel):
    gate_type: str
    decision: str = "approved"
    target_digest: str
    base_commit_sha: str
    policy_version: str = "policy-v1"
    comment: str = ""


@router.get("")
def api_list_approvals(
    task_id: str,
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("task:read")),
    db: Session = Depends(get_db, scope="function"),
) -> list[dict[str, Any]]:
    _ctx, tenant, _mem = perm
    rows = list_task_approvals(db, tenant_id=tenant.id, task_id=task_id)
    return [
        {
            "id": a.id,
            "gate_type": a.gate_type,
            "decision": a.decision,
            "target_digest": a.target_digest,
            "base_commit_sha": a.base_commit_sha,
            "policy_version": a.policy_version,
            "actor_user_id": a.actor_user_id,
            "comment": a.comment,
            "is_valid": a.is_valid,
            "invalidated_reason": a.invalidated_reason,
            "created_at": a.created_at.isoformat(),
        }
        for a in rows
    ]


@router.post("", status_code=201)
def api_submit_approval(
    task_id: str,
    body: GateApprovalRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    tenant, membership = ctx.require_tenant()
    key = (idempotency_key or "").strip() or secrets.token_hex(12)
    res = submit_gate_decision(
        db,
        tenant_id=tenant.id,
        actor_user_id=ctx.user.id,
        actor_role=membership.role,
        task_id=task_id,
        gate_type=body.gate_type,
        decision=body.decision,
        target_digest=body.target_digest,
        base_commit_sha=body.base_commit_sha,
        policy_version=body.policy_version,
        idempotency_key=key,
        comment=body.comment,
    )
    apr = res["approval"]
    job = res["job"]
    return {
        "approval_id": apr.id,
        "gate_type": apr.gate_type,
        "decision": apr.decision,
        "target_digest": apr.target_digest,
        "is_valid": apr.is_valid,
        "job_id": job.id if job else None,
    }
