"""Concurrency-safe atomic quota reservations and immutable integer-cents usage ledger."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session

from ai_sdlc_saas.db.models import Plan, QuotaReservation, Subscription, UsageLedgerEntry


class InsufficientBudgetError(RuntimeError):
    """Raised when tenant available credits or subscription status forbids dispatch."""


def ensure_trial_subscription(session: Session, tenant_id: str) -> Subscription:
    """Create a Free Trial subscription and initial credit grant for a new tenant if missing."""
    sub = session.scalar(select(Subscription).where(Subscription.tenant_id == tenant_id))
    if sub is not None:
        return sub
    trial = session.get(Plan, "trial")
    grant_cents = trial.included_credits_cents if trial is not None else 1_000
    sub = Subscription(
        id=f"sub_{secrets.token_hex(8)}",
        tenant_id=tenant_id,
        plan_id="trial",
        status="trialing",
        credit_balance_cents=grant_cents,
        reserved_cents=0,
        current_period_end=datetime.now(UTC) + timedelta(days=14),
    )
    session.add(sub)
    session.add(
        UsageLedgerEntry(
            id=f"led_{secrets.token_hex(8)}",
            tenant_id=tenant_id,
            entry_type="grant",
            amount_cents=grant_cents,
            description="Initial Free Trial credit grant",
            idempotency_key=f"grant:initial:{tenant_id}",
        )
    )
    session.flush()
    return sub


def get_available_credits_cents(session: Session, tenant_id: str) -> int:
    sub = session.scalar(select(Subscription).where(Subscription.tenant_id == tenant_id))
    if sub is None:
        return 0
    return max(0, sub.credit_balance_cents - sub.reserved_cents)


def reserve_quota(
    session: Session,
    *,
    tenant_id: str,
    task_id: str,
    run_id: str,
    amount_cents: int,
    idempotency_key: str,
) -> QuotaReservation:
    """Atomically reserve `amount_cents` from tenant's active/trialing subscription."""
    if amount_cents <= 0:
        raise ValueError("Reservation amount_cents must be > 0.")

    existing = session.scalar(
        select(QuotaReservation).where(
            QuotaReservation.tenant_id == tenant_id,
            QuotaReservation.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        return existing

    # Atomic conditional update prevents concurrent over-reservation races.
    stmt = (
        update(Subscription)
        .where(
            Subscription.tenant_id == tenant_id,
            Subscription.status.in_(("trialing", "active")),
            (Subscription.credit_balance_cents - Subscription.reserved_cents) >= amount_cents,
        )
        .values(reserved_cents=Subscription.reserved_cents + amount_cents)
    )
    res = cast(CursorResult[Any], session.execute(stmt))
    if res.rowcount != 1:
        sub = session.scalar(select(Subscription).where(Subscription.tenant_id == tenant_id))
        if sub is None:
            raise InsufficientBudgetError("Tenant has no active subscription.")
        if sub.status not in ("trialing", "active"):
            raise InsufficientBudgetError(f"Subscription status is {sub.status!r}; cannot reserve budget.")
        avail = sub.credit_balance_cents - sub.reserved_cents
        raise InsufficientBudgetError(
            f"Insufficient available credits: need {amount_cents} cents, available {avail} cents."
        )

    reservation = QuotaReservation(
        id=f"qres_{secrets.token_hex(8)}",
        tenant_id=tenant_id,
        task_id=task_id,
        run_id=run_id,
        reserved_cents=amount_cents,
        settled_cents=0,
        status="held",
        idempotency_key=idempotency_key,
    )
    session.add(reservation)
    session.add(
        UsageLedgerEntry(
            id=f"led_{secrets.token_hex(8)}",
            tenant_id=tenant_id,
            entry_type="reserve",
            amount_cents=-amount_cents,
            task_id=task_id,
            run_id=run_id,
            description=f"Budget reservation for run {run_id}",
            idempotency_key=f"reserve:{idempotency_key}",
        )
    )
    session.flush()
    return reservation


def settle_reservation(
    session: Session,
    *,
    tenant_id: str,
    reservation_id: str,
    confirmed_customer_cents: int,
    upstream_cost_cents: int = 0,
    sandbox_cost_cents: int = 0,
    unit_price_snapshot: dict[str, Any] | None = None,
    idempotency_key: str,
    description: str = "Confirmed run usage settlement",
) -> QuotaReservation:
    """Settle a held reservation: release held lock, debit actual confirmed cost, record immutable ledger entry."""
    if confirmed_customer_cents < 0:
        raise ValueError("confirmed_customer_cents cannot be negative")

    existing_led = session.scalar(
        select(UsageLedgerEntry).where(
            UsageLedgerEntry.tenant_id == tenant_id,
            UsageLedgerEntry.idempotency_key == f"consume:{idempotency_key}",
        )
    )
    qres = session.scalar(
        select(QuotaReservation).where(
            QuotaReservation.tenant_id == tenant_id,
            QuotaReservation.id == reservation_id,
        )
    )
    if qres is None:
        raise ValueError(f"QuotaReservation {reservation_id!r} not found for tenant {tenant_id!r}.")
    if existing_led is not None:
        return qres

    if qres.status != "held":
        return qres

    release_amount = qres.reserved_cents
    # Ensure credit_balance_cents never goes negative outside explicit documented overdraft; clamp debit at balance.
    sub = session.scalar(select(Subscription).where(Subscription.tenant_id == tenant_id))
    if sub is None:
        raise ValueError(f"Subscription missing for tenant {tenant_id!r}")

    actual_debit = min(confirmed_customer_cents, sub.credit_balance_cents)
    new_reserved = max(0, sub.reserved_cents - release_amount)
    new_balance = max(0, sub.credit_balance_cents - actual_debit)
    sub.reserved_cents = new_reserved
    sub.credit_balance_cents = new_balance

    qres.settled_cents = confirmed_customer_cents
    qres.status = "settled"

    unused = max(0, release_amount - actual_debit)
    if unused > 0:
        session.add(
            UsageLedgerEntry(
                id=f"led_{secrets.token_hex(8)}",
                tenant_id=tenant_id,
                entry_type="release",
                amount_cents=unused,
                task_id=qres.task_id,
                run_id=qres.run_id,
                description=f"Release unused reservation {qres.id}",
                idempotency_key=f"release:{idempotency_key}",
            )
        )
    session.add(
        UsageLedgerEntry(
            id=f"led_{secrets.token_hex(8)}",
            tenant_id=tenant_id,
            entry_type="consume",
            amount_cents=-actual_debit,
            upstream_cost_cents=upstream_cost_cents,
            sandbox_cost_cents=sandbox_cost_cents,
            task_id=qres.task_id,
            run_id=qres.run_id,
            unit_price_snapshot_json=dict(unit_price_snapshot or {}),
            description=description,
            idempotency_key=f"consume:{idempotency_key}",
        )
    )
    session.flush()
    return qres


def grant_or_refund_credits(
    session: Session,
    *,
    tenant_id: str,
    amount_cents: int,
    entry_type: str = "refund",
    description: str,
    idempotency_key: str,
    task_id: str | None = None,
    run_id: str | None = None,
) -> UsageLedgerEntry:
    """Idempotently credit tenant balance (grant, adjust, or refund)."""
    if amount_cents <= 0:
        raise ValueError("Credit grant/refund amount_cents must be > 0.")
    existing = session.scalar(
        select(UsageLedgerEntry).where(
            UsageLedgerEntry.tenant_id == tenant_id,
            UsageLedgerEntry.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        return existing

    session.execute(
        update(Subscription)
        .where(Subscription.tenant_id == tenant_id)
        .values(credit_balance_cents=Subscription.credit_balance_cents + amount_cents)
    )
    entry = UsageLedgerEntry(
        id=f"led_{secrets.token_hex(8)}",
        tenant_id=tenant_id,
        entry_type=entry_type,
        amount_cents=amount_cents,
        task_id=task_id,
        run_id=run_id,
        description=description,
        idempotency_key=idempotency_key,
    )
    session.add(entry)
    session.flush()
    return entry
