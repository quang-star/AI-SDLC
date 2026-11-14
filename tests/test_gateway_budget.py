"""P4 tests: BYOK masking, route fallback recording, missing usage metering, policy freeze, and concurrency race."""

from __future__ import annotations

import secrets
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from ai_sdlc_saas.api.app import create_app
from ai_sdlc_saas.billing.ledger import (
    InsufficientBudgetError,
    ensure_trial_subscription,
    get_available_credits_cents,
    reserve_quota,
)
from ai_sdlc_saas.config import Settings
from ai_sdlc_saas.db.engine import build_engine, create_session_factory, init_db_schema
from ai_sdlc_saas.db.models import RequestUsage, Tenant
from ai_sdlc_saas.errors import BudgetExceededError
from ai_sdlc_saas.gateway.adapters import DeterministicMockAdapter, GatewayResponse, OpenAICompatibleAdapter
from ai_sdlc_saas.gateway.service import (
    freeze_policy_snapshot,
    invoke_role_model,
    upsert_model_policy,
)


def _setup(tmp_path: Path) -> tuple[TestClient, Settings]:
    settings = Settings(
        env="test",
        database_url=f"sqlite:///{tmp_path / 'gw.db'}",
        object_store_dir=str(tmp_path / "objects"),
        worktree_root=str(tmp_path / "worktrees"),
        jwt_secret="test-jwt-secret-key-min-32-chars!!",
        encryption_key="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
        github_webhook_secret="gh-sec",
        stripe_webhook_secret="st-sec",
        allow_local_gateway=True,
    )
    return TestClient(create_app(settings)), settings


def _reg(client: TestClient, email: str, org_name: str) -> tuple[str, str]:
    r = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "Password123!", "full_name": "Admin", "org_name": org_name},
    )
    assert r.status_code == 201
    data = r.json()
    return str(data["access_token"]), str(data["active_org"]["id"])


def test_byok_masking_cross_tenant_block_and_fallback_metering(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    tok_a, org_a = _reg(client, "a@gw.com", "Org A")
    tok_b, _org_b = _reg(client, "b@gw.com", "Org B")
    h_a = {"Authorization": f"Bearer {tok_a}"}
    h_b = {"Authorization": f"Bearer {tok_b}"}

    raw_secret = "sk-TEST_ONLY-secret-key-987654321"
    cred = client.post(
        "/api/v1/credentials",
        headers=h_a,
        json={"name": "Tenant A Key", "provider_type": "openai_compatible", "secret_value": raw_secret},
    )
    assert cred.status_code == 201
    cred_id = cred.json()["id"]
    assert raw_secret not in cred.text

    listed = client.get("/api/v1/credentials", headers=h_a)
    assert listed.status_code == 200
    assert raw_secret not in listed.text
    assert "*" in listed.json()[0]["masked_preview"]

    # Cross-tenant attach rejected
    bad_route = client.post(
        "/api/v1/models/routes",
        headers=h_b,
        json={
            "name": "Hijack",
            "provider": "openai_compatible",
            "base_url": "https://api.openai.com/v1",
            "model_id": "gpt-4o-mini",
            "credential_ref_id": cred_id,
        },
    )
    assert bad_route.status_code == 403

    # SSRF localhost rejected if allow_local_gateway disabled, allowed here, test mock routes directly
    r_prim = client.post(
        "/api/v1/models/routes",
        headers=h_a,
        json={
            "name": "Primary Route",
            "provider": "mock",
            "base_url": "mock://primary",
            "model_id": "primary-model-x",
            "credential_ref_id": cred_id,
        },
    ).json()
    r_fb = client.post(
        "/api/v1/models/routes",
        headers=h_a,
        json={
            "name": "Fallback Route",
            "provider": "mock",
            "base_url": "mock://fallback",
            "model_id": "fallback-model-y",
        },
    ).json()

    pol = client.post(
        "/api/v1/models/policies",
        headers=h_a,
        json={
            "role_route_map": {"ba": r_prim["id"], "ba_fallback": r_fb["id"]},
            "compression_mode": "rtk",
            "per_task_budget_cents": 2,
        },
    )
    assert pol.status_code == 201

    # Invoke role with failing primary mock -> triggers fallback and records actual fallback model
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    overrides = {
        r_prim["id"]: DeterministicMockAdapter(fail_error="Simulated 429 upstream"),
        r_fb["id"]: DeterministicMockAdapter(
            handler=lambda _msgs, mid: GatewayResponse(
                content='{"ok": true}',
                actual_model_id=f"{mid}-real",
                input_tokens=120,
                output_tokens=60,
                cached_tokens=0,
                usage_status="CONFIRMED",
            )
        ),
    }

    with runtime.session_factory() as db:
        snap = freeze_policy_snapshot(db, tenant_id=org_a, project_id=None)

        # Updating policy AFTER freeze must not change `snap`
        upsert_model_policy(
            db,
            tenant_id=org_a,
            actor_user_id="usr_dummy",
            project_id=None,
            role_route_map={"ba": r_fb["id"]},
            per_task_budget_cents=500,
        )

        res = invoke_role_model(
            db,
            tenant_id=org_a,
            task_id="tsk_1",
            run_id="run_1",
            attempt_id="att_1",
            role="ba",
            messages=[{"role": "system", "content": "You are BA."}, {"role": "user", "content": "Add health endpoint."}],
            cipher=runtime.cipher,
            allow_local_gateway=True,
            policy_snapshot=snap,
            override_adapters=overrides,
        )
        assert res["route_id"] == r_fb["id"]
        assert res["actual_model_id"] == "fallback-model-y-real"
        assert res["usage"].customer_cost_cents >= 1

        # Second call pushes task spend to >= 2 cents -> 3rd call hits frozen cap of 2 cents!
        invoke_role_model(
            db,
            tenant_id=org_a,
            task_id="tsk_1",
            run_id="run_1",
            attempt_id="att_2",
            role="ba",
            messages=[{"role": "user", "content": "Another step"}],
            cipher=runtime.cipher,
            allow_local_gateway=True,
            policy_snapshot=snap,
            override_adapters=overrides,
        )
        with pytest.raises(BudgetExceededError):
            invoke_role_model(
                db,
                tenant_id=org_a,
                task_id="tsk_1",
                run_id="run_1",
                attempt_id="att_3",
                role="ba",
                messages=[{"role": "user", "content": "Over budget step"}],
                cipher=runtime.cipher,
                allow_local_gateway=True,
                policy_snapshot=snap,
                override_adapters=overrides,
            )
        db.commit()

    usages = client.get("/api/v1/usage/requests?task_id=tsk_1", headers=h_a)
    assert usages.status_code == 200
    assert len(usages.json()) == 2
    assert usages.json()[0]["actual_model_id"] == "fallback-model-y-real"


def test_missing_provider_usage_flagged_estimated_and_never_free(tmp_path: Path) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "id": "cmpl-1",
                "model": "gpt-custom-no-usage",
                "choices": [{"message": {"role": "assistant", "content": "Hello engineered response"}, "finish_reason": "stop"}],
                # Provider omits "usage" block!
            },
        )
    )
    adapter = OpenAICompatibleAdapter(
        base_url="https://api.example.com/v1",
        api_key="sk-test",
        transport=transport,
    )
    resp = adapter.chat_completion(
        messages=[{"role": "user", "content": "Explain the fix."}],
        model_id="gpt-custom-no-usage",
    )
    assert resp.usage_status == "ESTIMATED_MISSING"
    assert resp.input_tokens >= 1
    assert resp.output_tokens >= 1

    engine = build_engine(f"sqlite:///{tmp_path / 'est.db'}")
    init_db_schema(engine)
    factory = create_session_factory(engine)
    from ai_sdlc_saas.config import Settings as Cfg
    from ai_sdlc_saas.gateway.service import create_model_route
    from ai_sdlc_saas.security.crypto import EnvelopeCipher

    cfg = Cfg(env="test", allow_local_gateway=True)
    cipher = EnvelopeCipher(cfg.resolved_fernet_keys())
    with factory() as s:
        t = Tenant(id="org_est", slug="est", name="Est")
        s.add(t)
        s.flush()
        route = create_model_route(
            s,
            tenant_id=t.id,
            actor_user_id="usr_1",
            name="EstRoute",
            provider="openai_compatible",
            base_url="https://api.example.com/v1",
            model_id="gpt-custom-no-usage",
        )
        out = invoke_role_model(
            s,
            tenant_id=t.id,
            task_id="tsk_e",
            run_id="run_e",
            attempt_id="att_e",
            role="ba",
            messages=[{"role": "user", "content": "Ping"}],
            cipher=cipher,
            override_adapters={route.id: adapter},
        )
        usage: RequestUsage = out["usage"]
        assert usage.usage_status == "ESTIMATED_MISSING"
        assert usage.customer_cost_cents >= 1


def test_concurrent_quota_reservation_never_overcommits(tmp_path: Path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'race.db'}")
    init_db_schema(engine)
    factory = create_session_factory(engine)

    with factory() as s:
        s.add(Tenant(id="org_race", slug="race", name="Race Org"))
        s.flush()
        sub = ensure_trial_subscription(s, "org_race")  # 1000 cents trial
        assert sub.credit_balance_cents == 1000
        s.commit()

    def _try_reserve(idx: int) -> bool:
        with factory() as s:
            try:
                reserve_quota(
                    s,
                    tenant_id="org_race",
                    task_id=f"tsk_{idx}",
                    run_id=f"run_{idx}",
                    amount_cents=250,
                    idempotency_key=f"race:{idx}:{secrets.token_hex(4)}",
                )
                s.commit()
                return True
            except InsufficientBudgetError:
                s.rollback()
                return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(_try_reserve, range(8)))

    assert sum(1 for ok in outcomes if ok) == 4
    assert sum(1 for ok in outcomes if not ok) == 4
    with factory() as s:
        assert get_available_credits_cents(s, "org_race") == 0
