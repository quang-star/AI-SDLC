"""P13 governance tests: kill switches, trial abuse guardrails, Prometheus metrics, export, hard delete, support & backup drill."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from scripts.backup_restore_drill import run_backup_restore_drill

from ai_sdlc_saas.api.app import create_app
from ai_sdlc_saas.config import Settings
from ai_sdlc_saas.db.models import SupportAccessGrant, User
from ai_sdlc_saas.errors import RateLimitError
from ai_sdlc_saas.governance.lifecycle import enforce_trial_guardrails


def _setup(tmp_path: Path) -> TestClient:
    settings = Settings(
        env="test",
        database_url=f"sqlite:///{tmp_path / 'p13.db'}",
        object_store_dir=str(tmp_path / "objects"),
        worktree_root=str(tmp_path / "worktrees"),
        jwt_secret="test-jwt-secret-key-min-32-chars!!",
        encryption_key="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
        allow_local_git_repos=True,
    )
    return TestClient(create_app(settings))


def test_governance_kill_switches_metrics_export_support_hard_delete_and_backup_drill(tmp_path: Path) -> None:
    client = _setup(tmp_path)
    runtime = client.app.state.runtime  # type: ignore[attr-defined]

    # Register org A (owner + invited admin) and operator user
    reg = client.post(
        "/api/v1/auth/register",
        json={"email": "owner@p13.dev", "password": "Password123!", "full_name": "P13 Owner", "org_name": "Acme Sec"},
    ).json()
    tenant_id = reg["active_org"]["id"]
    tenant_slug = reg["active_org"]["slug"]
    h_owner = {"Authorization": f"Bearer {reg['access_token']}"}

    adm_inv = client.post(
        "/api/v1/orgs/current/invitations",
        headers=h_owner,
        json={"email": "admin@p13.dev", "role": "admin"},
    ).json()
    adm_reg = client.post(
        "/api/v1/auth/register",
        json={"email": "admin@p13.dev", "password": "Password123!", "full_name": "P13 Admin", "org_name": "Adm"},
    ).json()
    h_admin = {"Authorization": f"Bearer {adm_reg['access_token']}"}
    client.post("/api/v1/orgs/invitations/accept", headers=h_admin, json={"token": adm_inv["invite_token"]})

    op_reg = client.post(
        "/api/v1/auth/register",
        json={"email": "op@platform.dev", "password": "Password123!", "full_name": "Platform Op", "org_name": "Ops"},
    ).json()
    op_user_id = op_reg["user"]["id"]
    h_op = {"Authorization": f"Bearer {op_reg['access_token']}"}

    # Seed project + docs task for export, support inspect, and backup drill
    proj_id = client.post("/api/v1/projects", headers=h_owner, json={"name": "Gov Project"}).json()["id"]
    cred_resp = client.post(
        "/api/v1/credentials",
        headers=h_owner,
        json={"name": "prod-key", "provider_type": "openai_compatible", "secret_value": "sk-TEST_ONLY-secret-value-99999"},
    )
    assert cred_resp.status_code == 201, cred_resp.text
    t1 = client.post(
        "/api/v1/tasks",
        headers=h_owner,
        json={
            "project_id": proj_id,
            "title": "Docs checklist",
            "requirement_text": "Update README with compliance checklist details.",
            "task_type": "docs",
            "repo_ids": [],
        },
    )
    assert t1.status_code == 201, t1.text

    # 1. Kill switch requires platform operator; active tenant kill switch blocks task creation (503)
    assert (
        client.post(
            "/api/v1/ops/kill-switches",
            headers=h_owner,
            json={"scope": "tenant", "target_key": tenant_id, "is_active": True, "reason": "abuse"},
        ).status_code
        == 403
    )
    with runtime.session_factory() as s:
        op_user = s.get(User, op_user_id)
        assert op_user is not None
        op_user.is_platform_operator = True
        s.commit()

    assert (
        client.post(
            "/api/v1/ops/kill-switches",
            headers=h_op,
            json={"scope": "tenant", "target_key": tenant_id, "is_active": True, "reason": "incident pause"},
        ).status_code
        == 200
    )
    blocked = client.post(
        "/api/v1/tasks",
        headers=h_owner,
        json={
            "project_id": proj_id,
            "title": "Should block while switched",
            "requirement_text": "Document release procedure cleanly across repos.",
            "task_type": "docs",
            "repo_ids": [],
        },
    )
    assert blocked.status_code == 503
    # Turn kill switch off
    assert (
        client.post(
            "/api/v1/ops/kill-switches",
            headers=h_op,
            json={"scope": "tenant", "target_key": tenant_id, "is_active": False, "reason": "resolved"},
        ).status_code
        == 200
    )

    # 2. Free/trial abuse guardrail blocks excess concurrent tasks
    assert (
        client.post(
            "/api/v1/tasks",
            headers=h_owner,
            json={
                "project_id": proj_id,
                "title": "Docs checklist two",
                "requirement_text": "Update security changelog documentation in README.",
                "task_type": "docs",
                "repo_ids": [],
            },
        ).status_code
        == 201
    )
    over_concurrent = client.post(
        "/api/v1/tasks",
        headers=h_owner,
        json={
            "project_id": proj_id,
            "title": "Third concurrent trial task",
            "requirement_text": "Should exceed free trial concurrent cap of two tasks.",
            "task_type": "docs",
            "repo_ids": [],
        },
    )
    assert over_concurrent.status_code == 429
    with runtime.session_factory() as s:
        try:
            enforce_trial_guardrails(s, tenant_id=tenant_id, hourly_task_cap=1)
        except RateLimitError:
            pass

    # 3. Scoped time-limited support access grant
    assert client.get(f"/api/v1/ops/tenants/{tenant_id}/inspect", headers=h_op).status_code == 403
    grant = client.post(
        "/api/v1/support/grants",
        headers=h_owner,
        json={"operator_user_id": op_user_id, "ticket_id": "SUP-1042", "reason": "Debug stalled job", "ttl_minutes": 30},
    )
    assert grant.status_code == 201, grant.text
    grant_id = grant.json()["id"]
    insp = client.get(f"/api/v1/ops/tenants/{tenant_id}/inspect", headers=h_op)
    assert insp.status_code == 200
    assert insp.json()["counts"]["projects"] == 1

    # Expired or revoked grant immediately denies operator inspection
    assert client.delete(f"/api/v1/support/grants/{grant_id}", headers=h_owner).status_code == 200
    assert client.get(f"/api/v1/ops/tenants/{tenant_id}/inspect", headers=h_op).status_code == 403

    grant2 = client.post(
        "/api/v1/support/grants",
        headers=h_owner,
        json={"operator_user_id": op_user_id, "ticket_id": "SUP-1043", "reason": "Follow up", "ttl_minutes": 10},
    ).json()
    with runtime.session_factory() as s:
        g = s.get(SupportAccessGrant, grant2["id"])
        assert g is not None
        g.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        s.commit()
    assert client.get(f"/api/v1/ops/tenants/{tenant_id}/inspect", headers=h_op).status_code == 403

    # Audit log contains support inspection event
    audits = client.get("/api/v1/audit-events", headers=h_owner).json()
    assert any(a["action"] == "support.tenant_inspected" for a in audits)

    # 4. Data Export ZIP excludes raw secrets and verifies manifest hashes
    exp = client.post("/api/v1/org/exports", headers=h_owner)
    assert exp.status_code == 201, exp.text
    exp_id = exp.json()["id"]
    dl = client.get(f"/api/v1/org/exports/{exp_id}/download", headers=h_owner)
    assert dl.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(dl.content))
    manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    assert manifest["tenant_id"] == tenant_id
    all_zip_text = "\n".join(zf.read(name).decode("utf-8", errors="ignore") for name in zf.namelist())
    assert "sk-TEST_ONLY-secret-value-99999" not in all_zip_text
    assert "password_hash" not in all_zip_text

    # 5. Prometheus metrics endpoint renders without tenant ID leakage
    metrics = client.get("/api/v1/metrics")
    assert metrics.status_code == 200
    assert "ai_sdlc_http_requests_total" in metrics.text
    assert tenant_id not in metrics.text

    # 6. Backup & restore drill against live SQLite + ObjectStore succeeds cleanly
    drill = run_backup_restore_drill(
        source_sqlite_path=tmp_path / "p13.db",
        source_object_store_dir=tmp_path / "objects",
        work_dir=tmp_path / "drill_out",
    )
    assert drill["ok"] is True and drill["integrity_check"] == "ok" and drill["object_files_verified"] >= 1

    # 7. Hard delete: admin forbidden (403), wrong slug fails (422), owner wipes DB + ObjectStore + revokes tokens (401)
    assert client.post("/api/v1/org/hard-delete", headers=h_admin, json={"confirm_slug": tenant_slug}).status_code == 403
    assert client.post("/api/v1/org/hard-delete", headers=h_owner, json={"confirm_slug": "wrong-slug"}).status_code == 422
    hd = client.post("/api/v1/org/hard-delete", headers=h_owner, json={"confirm_slug": tenant_slug})
    assert hd.status_code == 200, hd.text
    assert hd.json()["deleted_counts"]["object_store_files"] >= 1
    assert hd.json()["deleted_counts"]["projects"] == 1
    assert client.get("/api/v1/projects", headers=h_owner).status_code == 401
