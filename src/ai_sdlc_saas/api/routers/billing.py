"""Commercial plans, subscriptions, credit topups, usage breakdown, and ledger endpoints."""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_sdlc_saas.api.deps import get_auth_context, get_db, require_permission
from ai_sdlc_saas.billing.ledger import ensure_trial_subscription, grant_or_refund_credits
from ai_sdlc_saas.billing.plans import seed_default_plans
from ai_sdlc_saas.billing.stripe_adapter import apply_plan_change, create_checkout_session
from ai_sdlc_saas.db.models import (
    Membership,
    Plan,
    RequestUsage,
    Subscription,
    Tenant,
    UsageLedgerEntry,
)
from ai_sdlc_saas.errors import ValidationError
from ai_sdlc_saas.identity.service import AuthContext

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])


class PlanRequest(BaseModel):
    plan_id: str = Field(..., min_length=2, max_length=64)


class TopupRequest(BaseModel):
    amount_cents: int = Field(..., ge=100, le=10_000_000)
    description: str = Field(default="Manual credit top-up", max_length=300)


@router.get("/plans")
def api_list_plans(
    _ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db, scope="function"),
) -> list[dict[str, Any]]:
    seed_default_plans(db)
    plans = list(db.scalars(select(Plan).order_by(Plan.monthly_price_cents.asc())))
    return [
        {
            "id": p.id,
            "name": p.name,
            "monthly_price_cents": p.monthly_price_cents,
            "included_credits_cents": p.included_credits_cents,
            "max_seats": p.max_seats,
            "max_repos": p.max_repos,
            "max_concurrent_tasks": p.max_concurrent_tasks,
            "allowed_model_tiers": p.allowed_model_tiers_json,
        }
        for p in plans
    ]


@router.get("/subscription")
def api_get_subscription(
    perm: tuple[AuthContext, Tenant, Membership] = Depends(require_permission("billing:read")),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    _ctx, tenant, _mem = perm
    seed_default_plans(db)
    sub = ensure_trial_subscription(db, tenant.id)
    plan = db.get(Plan, sub.plan_id)
    return {
        "id": sub.id,
        "tenant_id": sub.tenant_id,
        "plan_id": sub.plan_id,
        "plan_name": plan.name if plan else sub.plan_id,
        "status": sub.status,
        "credit_balance_cents": sub.credit_balance_cents,
        "reserved_cents": sub.reserved_cents,
        "available_cents": sub.credit_balance_cents - sub.reserved_cents,
        "max_seats": plan.max_seats if plan else 5,
        "max_repos": plan.max_repos if plan else 2,
        "max_concurrent_tasks": plan.max_concurrent_tasks if plan else 2,
        "allowed_model_tiers": plan.allowed_model_tiers_json if plan else ["cheap", "standard"],
        "grace_until": sub.grace_until.isoformat() if sub.grace_until else None,
        "current_period_end": sub.current_period_end.isoformat() if sub.current_period_end else None,
    }


@router.post("/checkout")
def api_create_checkout(
    body: PlanRequest,
    perm: tuple[AuthContext, Tenant, Membership] = Depends(require_permission("billing:manage")),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    ctx, tenant, _mem = perm
    seed_default_plans(db)
    return create_checkout_session(
        db,
        tenant_id=tenant.id,
        plan_id=body.plan_id,
        actor_user_id=ctx.user.id,
    )


@router.post("/change-plan")
def api_change_plan(
    body: PlanRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    perm: tuple[AuthContext, Tenant, Membership] = Depends(require_permission("billing:manage")),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    ctx, tenant, _mem = perm
    seed_default_plans(db)
    key = (idempotency_key or "").strip() or f"chg_{secrets.token_hex(8)}"
    sub: Subscription = apply_plan_change(
        db,
        tenant_id=tenant.id,
        new_plan_id=body.plan_id,
        actor_user_id=ctx.user.id,
        idempotency_key=key,
    )
    return {
        "tenant_id": sub.tenant_id,
        "plan_id": sub.plan_id,
        "status": sub.status,
        "credit_balance_cents": sub.credit_balance_cents,
        "reserved_cents": sub.reserved_cents,
    }


@router.post("/topup", status_code=201)
def api_topup_credits(
    body: TopupRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    perm: tuple[AuthContext, Tenant, Membership] = Depends(require_permission("billing:manage")),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    _ctx, tenant, _mem = perm
    if body.amount_cents <= 0:
        raise ValidationError("amount_cents must be positive.")
    seed_default_plans(db)
    ensure_trial_subscription(db, tenant.id)
    key = (idempotency_key or "").strip() or f"topup_{secrets.token_hex(8)}"
    entry = grant_or_refund_credits(
        db,
        tenant_id=tenant.id,
        amount_cents=body.amount_cents,
        entry_type="topup",
        description=body.description,
        idempotency_key=key,
    )
    sub = ensure_trial_subscription(db, tenant.id)
    return {
        "entry_id": entry.id,
        "amount_cents": entry.amount_cents,
        "credit_balance_cents": sub.credit_balance_cents,
        "reserved_cents": sub.reserved_cents,
    }


@router.get("/ledger")
def api_get_ledger(
    perm: tuple[AuthContext, Tenant, Membership] = Depends(require_permission("billing:read")),
    db: Session = Depends(get_db, scope="function"),
) -> list[dict[str, Any]]:
    _ctx, tenant, _mem = perm
    rows = list(
        db.scalars(
            select(UsageLedgerEntry)
            .where(UsageLedgerEntry.tenant_id == tenant.id)
            .order_by(UsageLedgerEntry.created_at.desc())
            .limit(200)
        )
    )
    return [
        {
            "id": e.id,
            "entry_type": e.entry_type,
            "amount_cents": e.amount_cents,
            "upstream_cost_cents": e.upstream_cost_cents,
            "sandbox_cost_cents": e.sandbox_cost_cents,
            "task_id": e.task_id,
            "run_id": e.run_id,
            "resource_id": e.resource_id,
            "description": e.description,
            "created_at": e.created_at.isoformat(),
        }
        for e in rows
    ]


@router.get("/usage")
def api_get_usage(
    perm: tuple[AuthContext, Tenant, Membership] = Depends(require_permission("billing:read")),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    _ctx, tenant, _mem = perm
    rows = list(
        db.scalars(
            select(RequestUsage)
            .where(RequestUsage.tenant_id == tenant.id)
            .order_by(RequestUsage.created_at.desc())
            .limit(500)
        )
    )
    by_role: dict[str, dict[str, int]] = {}
    total_customer_cents = 0
    total_input = 0
    total_output = 0
    for r in rows:
        bucket = by_role.setdefault(
            r.role,
            {"calls": 0, "input_tokens": 0, "output_tokens": 0, "customer_cost_cents": 0},
        )
        bucket["calls"] += 1
        bucket["input_tokens"] += int(r.input_tokens)
        bucket["output_tokens"] += int(r.output_tokens)
        bucket["customer_cost_cents"] += int(r.customer_cost_cents)
        total_customer_cents += int(r.customer_cost_cents)
        total_input += int(r.input_tokens)
        total_output += int(r.output_tokens)

    return {
        "tenant_id": tenant.id,
        "total_calls": len(rows),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_customer_cost_cents": total_customer_cents,
        "by_role": by_role,
        "recent": [
            {
                "id": r.id,
                "task_id": r.task_id,
                "role": r.role,
                "actual_model_id": r.actual_model_id,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "customer_cost_cents": r.customer_cost_cents,
                "usage_status": r.usage_status,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows[:50]
        ],
    }
