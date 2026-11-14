"""Payment provider adapter (Stripe-compatible) with webhook HMAC verification & out-of-order reconciliation."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_sdlc_saas.billing.ledger import ensure_trial_subscription, grant_or_refund_credits
from ai_sdlc_saas.db.engine import set_session_tenant
from ai_sdlc_saas.db.models import BillingEvent, Plan, Subscription
from ai_sdlc_saas.governance.audit import record_audit_event
from ai_sdlc_saas.security.crypto import verify_stripe_signature


class WebhookSignatureError(PermissionError):
    """Raised when a payment webhook fails signature verification."""


def create_checkout_session(
    session: Session,
    *,
    tenant_id: str,
    plan_id: str,
    actor_user_id: str,
) -> dict[str, Any]:
    """Create a Stripe-mode checkout session URL and return session metadata."""
    plan = session.get(Plan, plan_id)
    if plan is None:
        raise ValueError(f"Unknown plan_id={plan_id!r}")
    sub = ensure_trial_subscription(session, tenant_id)
    checkout_id = f"cs_test_{secrets.token_hex(12)}"
    record_audit_event(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="billing.checkout_created",
        target_type="subscription",
        target_id=sub.id,
        details={"plan_id": plan_id, "checkout_id": checkout_id},
    )
    return {
        "checkout_session_id": checkout_id,
        "plan_id": plan.id,
        "amount_cents": plan.monthly_price_cents,
        "included_credits_cents": plan.included_credits_cents,
        "checkout_url": f"https://checkout.stripe.example/pay/{checkout_id}",
        "mode": "sandbox",
    }


def apply_plan_change(
    session: Session,
    *,
    tenant_id: str,
    new_plan_id: str,
    actor_user_id: str | None,
    idempotency_key: str,
) -> Subscription:
    """Activate, upgrade, or downgrade a tenant subscription cleanly."""
    plan = session.get(Plan, new_plan_id)
    if plan is None:
        raise ValueError(f"Unknown plan_id={new_plan_id!r}")
    sub = ensure_trial_subscription(session, tenant_id)
    prev_plan = sub.plan_id
    sub.plan_id = plan.id
    sub.status = "active" if plan.monthly_price_cents > 0 else "trialing"
    sub.grace_until = None
    sub.current_period_end = datetime.now(UTC) + timedelta(days=30)

    if plan.included_credits_cents > 0:
        grant_or_refund_credits(
            session,
            tenant_id=tenant_id,
            amount_cents=plan.included_credits_cents,
            entry_type="grant",
            description=f"Monthly credit grant for plan {plan.name}",
            idempotency_key=f"plan-grant:{idempotency_key}",
        )
    record_audit_event(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="billing.plan_changed",
        target_type="subscription",
        target_id=sub.id,
        details={"from_plan": prev_plan, "to_plan": plan.id},
    )
    session.flush()
    return sub


def process_stripe_webhook(
    session: Session,
    *,
    raw_body: bytes,
    signature_header: str | None,
    webhook_secret: str,
) -> dict[str, Any]:
    """Verify signature, deduplicate event ID, and reconcile subscription/billing state safely."""
    if not verify_stripe_signature(raw_body, signature_header, webhook_secret):
        raise WebhookSignatureError("Invalid Stripe webhook signature.")

    payload = json.loads(raw_body.decode("utf-8"))
    event_id = str(payload.get("id") or "")
    event_type = str(payload.get("type") or "")
    event_created = int(payload.get("created") or 0)
    data_obj = payload.get("data", {}).get("object", {})
    tenant_id = str(data_obj.get("metadata", {}).get("tenant_id") or data_obj.get("tenant_id") or "")
    if not event_id or not event_type:
        raise ValueError("Webhook payload missing id or type.")

    existing = session.scalar(select(BillingEvent).where(BillingEvent.external_event_id == event_id))
    if existing is not None:
        return {"status": "duplicate_ignored", "event_id": event_id}

    if tenant_id:
        set_session_tenant(session, tenant_id)

    # Check out-of-order protection: ignore stale subscription state events older than latest processed event
    latest_for_tenant = (
        session.scalar(
            select(BillingEvent)
            .where(BillingEvent.tenant_id == tenant_id, BillingEvent.event_type.like("customer.subscription.%"))
            .order_by(BillingEvent.event_created_ts.desc())
        )
        if tenant_id
        else None
    )

    evt_row = BillingEvent(
        id=f"bevt_{secrets.token_hex(8)}",
        external_event_id=event_id,
        event_type=event_type,
        event_created_ts=event_created,
        tenant_id=tenant_id or None,
        status="processed",
        payload_json=payload,
    )
    session.add(evt_row)

    if not tenant_id:
        session.flush()
        return {"status": "ignored_no_tenant", "event_id": event_id}

    sub = ensure_trial_subscription(session, tenant_id)
    if event_type == "invoice.paid":
        plan_id = str(data_obj.get("metadata", {}).get("plan_id") or sub.plan_id)
        apply_plan_change(
            session,
            tenant_id=tenant_id,
            new_plan_id=plan_id,
            actor_user_id=None,
            idempotency_key=f"stripe-evt:{event_id}",
        )
    elif event_type == "invoice.payment_failed":
        sub.status = "past_due"
        sub.grace_until = datetime.now(UTC) + timedelta(days=3)
    elif event_type == "customer.subscription.updated":
        if latest_for_tenant is not None and event_created < latest_for_tenant.event_created_ts:
            evt_row.status = "stale_reconciled_noop"
        else:
            new_status = str(data_obj.get("status") or sub.status)
            if new_status in ("trialing", "active", "past_due", "canceled", "suspended"):
                sub.status = new_status
    elif event_type == "customer.subscription.deleted":
        if latest_for_tenant is not None and event_created < latest_for_tenant.event_created_ts:
            evt_row.status = "stale_reconciled_noop"
        else:
            sub.status = "canceled"
    elif event_type == "charge.refunded":
        refund_cents = int(data_obj.get("amount_refunded") or 0)
        if refund_cents > 0:
            grant_or_refund_credits(
                session,
                tenant_id=tenant_id,
                amount_cents=refund_cents,
                entry_type="refund",
                description=f"Stripe refund {event_id}",
                idempotency_key=f"refund:{event_id}",
            )

    session.flush()
    return {"status": evt_row.status, "event_id": event_id, "subscription_status": sub.status}
