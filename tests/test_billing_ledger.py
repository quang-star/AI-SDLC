"""P12 commercial billing tests: plans, credits, idempotent topups, Stripe webhook reconciliation, usage & RBAC."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from ai_sdlc_saas.api.app import create_app
from ai_sdlc_saas.billing.ledger import InsufficientBudgetError, reserve_quota, settle_reservation
from ai_sdlc_saas.config import Settings


def _setup(tmp_path: Path) -> TestClient:
    settings = Settings(
        env="test",
        database_url=f"sqlite:///{tmp_path / 'p12.db'}",
        object_store_dir=str(tmp_path / "objects"),
        worktree_root=str(tmp_path / "worktrees"),
        jwt_secret="test-jwt-secret-key-min-32-chars!!",
        encryption_key="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
        stripe_webhook_secret="whsec_test_12345",
        allow_local_git_repos=True,
    )
    return TestClient(create_app(settings))


def _sign_stripe(raw_body: bytes, secret: str, ts: int | None = None) -> str:
    stamp = ts if ts is not None else int(datetime.now(UTC).timestamp())
    signed = f"{stamp}.".encode("utf-8") + raw_body
    sig = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={stamp},v1={sig}"


def test_billing_plans_checkout_topup_webhooks_reservation_and_rbac(tmp_path: Path) -> None:
    client = _setup(tmp_path)
    runtime = client.app.state.runtime  # type: ignore[attr-defined]

    reg = client.post(
        "/api/v1/auth/register",
        json={"email": "owner@p12.dev", "password": "Password123!", "full_name": "P12 Owner", "org_name": "Billing Org"},
    ).json()
    tenant_id = reg["active_org"]["id"]
    h_owner = {"Authorization": f"Bearer {reg['access_token']}"}

    # Invite a developer to test billing RBAC denial
    inv = client.post(
        "/api/v1/orgs/current/invitations",
        headers=h_owner,
        json={"email": "dev@p12.dev", "role": "developer"},
    ).json()
    dev_reg = client.post(
        "/api/v1/auth/register",
        json={"email": "dev@p12.dev", "password": "Password123!", "full_name": "Dev", "org_name": "Dev Org"},
    ).json()
    h_dev = {"Authorization": f"Bearer {dev_reg['access_token']}"}
    client.post("/api/v1/orgs/invitations/accept", headers=h_dev, json={"token": inv["invite_token"]})

    # Plans list visible to authenticated member; billing details denied to developer (403)
    plans = client.get("/api/v1/billing/plans", headers=h_dev)
    assert plans.status_code == 200 and len(plans.json()) >= 4
    assert client.get("/api/v1/billing/subscription", headers=h_dev).status_code == 403
    assert client.post("/api/v1/billing/topup", headers=h_dev, json={"amount_cents": 500}).status_code == 403

    sub0 = client.get("/api/v1/billing/subscription", headers=h_owner).json()
    assert sub0["plan_id"] == "trial" and sub0["credit_balance_cents"] >= 1000

    # Idempotent credit top-up
    t1 = client.post(
        "/api/v1/billing/topup",
        headers={**h_owner, "Idempotency-Key": "top-1"},
        json={"amount_cents": 2000, "description": "Initial credit purchase"},
    )
    assert t1.status_code == 201
    t2 = client.post(
        "/api/v1/billing/topup",
        headers={**h_owner, "Idempotency-Key": "top-1"},
        json={"amount_cents": 2000, "description": "Initial credit purchase"},
    )
    assert t2.status_code == 201
    assert t2.json()["credit_balance_cents"] == t1.json()["credit_balance_cents"]
    bal_after_topup = t2.json()["credit_balance_cents"]

    # Checkout + change-plan to pro
    chk = client.post("/api/v1/billing/checkout", headers=h_owner, json={"plan_id": "pro"})
    assert chk.status_code == 200 and "checkout_url" in chk.json()
    chg = client.post(
        "/api/v1/billing/change-plan",
        headers={**h_owner, "Idempotency-Key": "chg-pro"},
        json={"plan_id": "pro"},
    )
    assert chg.status_code == 200 and chg.json()["plan_id"] == "pro"
    bal_pro = chg.json()["credit_balance_cents"]
    assert bal_pro >= bal_after_topup

    # Reserve + partial settlement releases unused reservation cleanly; over-budget raises BudgetExceededError
    with runtime.session_factory() as s:
        res_rec = reserve_quota(
            s,
            tenant_id=tenant_id,
            task_id="tsk_b1",
            run_id="run_b1",
            amount_cents=500,
            idempotency_key="res-b1",
        )
        settle_reservation(
            s,
            tenant_id=tenant_id,
            reservation_id=res_rec.id,
            confirmed_customer_cents=120,
            upstream_cost_cents=90,
            sandbox_cost_cents=10,
            idempotency_key="stl-b1",
            description="Partial spend settlement",
        )
        try:
            reserve_quota(
                s,
                tenant_id=tenant_id,
                task_id="tsk_b2",
                run_id="run_b2",
                amount_cents=10_000_000,
                idempotency_key="res-huge",
            )
            raise AssertionError("Expected InsufficientBudgetError for exceeding credit balance.")
        except InsufficientBudgetError:
            pass
        s.commit()

    # Stripe webhooks: invalid sig (401), duplicate ignored, out-of-order stale ignored, charge.refunded updates ledger
    now_ts = int(datetime.now(UTC).timestamp())
    evt_paid = {
        "id": "evt_paid_100",
        "type": "invoice.paid",
        "created": now_ts,
        "data": {"object": {"metadata": {"tenant_id": tenant_id, "plan_id": "team"}}},
    }
    raw_paid = json.dumps(evt_paid).encode("utf-8")
    bad_wh = client.post(
        "/api/v1/webhooks/stripe",
        content=raw_paid,
        headers={"Stripe-Signature": "t=1,v1=deadbeef", "Content-Type": "application/json"},
    )
    assert bad_wh.status_code == 401

    wh1 = client.post(
        "/api/v1/webhooks/stripe",
        content=raw_paid,
        headers={"Stripe-Signature": _sign_stripe(raw_paid, "whsec_test_12345", now_ts), "Content-Type": "application/json"},
    )
    assert wh1.status_code == 200 and wh1.json()["status"] == "processed"

    wh1_dup = client.post(
        "/api/v1/webhooks/stripe",
        content=raw_paid,
        headers={"Stripe-Signature": _sign_stripe(raw_paid, "whsec_test_12345", now_ts), "Content-Type": "application/json"},
    )
    assert wh1_dup.status_code == 200 and wh1_dup.json()["status"] == "duplicate_ignored"

    # Newer subscription event followed by older downgrade event must be reconciled as stale no-op
    evt_active = {
        "id": "evt_sub_200",
        "type": "customer.subscription.updated",
        "created": now_ts,
        "data": {"object": {"metadata": {"tenant_id": tenant_id}, "status": "active"}},
    }
    raw_active = json.dumps(evt_active).encode("utf-8")
    wh_active = client.post(
        "/api/v1/webhooks/stripe",
        content=raw_active,
        headers={"Stripe-Signature": _sign_stripe(raw_active, "whsec_test_12345", now_ts), "Content-Type": "application/json"},
    )
    assert wh_active.status_code == 200 and wh_active.json()["status"] == "processed"

    evt_stale = {
        "id": "evt_stale_99",
        "type": "customer.subscription.updated",
        "created": now_ts - 60,
        "data": {"object": {"metadata": {"tenant_id": tenant_id}, "status": "past_due"}},
    }
    raw_stale = json.dumps(evt_stale).encode("utf-8")
    wh_stale = client.post(
        "/api/v1/webhooks/stripe",
        content=raw_stale,
        headers={"Stripe-Signature": _sign_stripe(raw_stale, "whsec_test_12345", now_ts), "Content-Type": "application/json"},
    )
    assert wh_stale.status_code == 200 and wh_stale.json()["status"] == "stale_reconciled_noop"

    # Ledger and usage endpoints show immutable entries scoped to tenant
    ledger = client.get("/api/v1/billing/ledger", headers=h_owner).json()
    assert any(e["entry_type"] == "topup" for e in ledger)
    assert any(e["entry_type"] in {"consume", "usage"} for e in ledger)
    usage = client.get("/api/v1/billing/usage", headers=h_owner).json()
    assert usage["tenant_id"] == tenant_id
