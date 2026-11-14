"""P2 verification tests: auth lifecycle, sessions, RBAC, composite FKs, and tenant isolation."""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ai_sdlc_saas.api.app import create_app
from ai_sdlc_saas.config import Settings
from ai_sdlc_saas.db.engine import build_engine, create_session_factory, init_db_schema, set_session_tenant
from ai_sdlc_saas.db.models import Project, Repository, Tenant
from ai_sdlc_saas.storage.object_store import LocalObjectStore, build_artifact_key


def _make_client(tmp_path: Path) -> tuple[TestClient, Settings]:
    settings = Settings(
        env="test",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_dir=str(tmp_path / "objects"),
        worktree_root=str(tmp_path / "worktrees"),
        jwt_secret="test-jwt-secret-key-min-32-chars!!",
        encryption_key="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
        github_webhook_secret="gh-sec",
        stripe_webhook_secret="st-sec",
    )
    app = create_app(settings)
    return TestClient(app), settings


def test_auth_session_revocation_and_password_reset(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)

    reg = client.post(
        "/api/v1/auth/register",
        json={
            "email": "alice@example.com",
            "password": "Password123!",
            "full_name": "Alice Owner",
            "org_name": "Acme Corp",
        },
    )
    assert reg.status_code == 201, reg.text
    data = reg.json()
    token1 = data["access_token"]
    verif_tok = data["verification_token"]
    assert data["active_org"]["role"] == "owner"

    # Security headers present
    assert reg.headers.get("X-Content-Type-Options") == "nosniff"
    assert reg.headers.get("X-Frame-Options") == "DENY"

    # Verify email
    v = client.post("/api/v1/auth/verify-email", json={"token": verif_tok})
    assert v.status_code == 200
    assert v.json()["verified"] is True

    # Open a second session via login
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "alice@example.com", "password": "Password123!"},
    )
    assert login.status_code == 200
    token2 = login.json()["access_token"]

    sessions_resp = client.get("/api/v1/auth/sessions", headers={"Authorization": f"Bearer {token1}"})
    assert sessions_resp.status_code == 200
    assert len(sessions_resp.json()) == 2

    # Revoke session 2 using token1 -> token2 immediately fails
    sid2 = login.json()["session_id"]
    rev = client.delete(f"/api/v1/auth/sessions/{sid2}", headers={"Authorization": f"Bearer {token1}"})
    assert rev.status_code == 200
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token2}"}).status_code == 401

    # Password reset revokes all existing active sessions (including token1)
    rst_req = client.post("/api/v1/auth/password-reset/request", json={"email": "alice@example.com"})
    assert rst_req.status_code == 200
    rst_tok = rst_req.json()["reset_token"]

    rst_conf = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": rst_tok, "new_password": "NewPassword456!"},
    )
    assert rst_conf.status_code == 200
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token1}"}).status_code == 401

    # Old password rejected; new password succeeds
    assert (
        client.post(
            "/api/v1/auth/login",
            json={"email": "alice@example.com", "password": "Password123!"},
        ).status_code
        == 401
    )
    login2 = client.post(
        "/api/v1/auth/login",
        json={"email": "alice@example.com", "password": "NewPassword456!"},
    )
    assert login2.status_code == 200


def test_rbac_invitations_ownership_transfer_and_member_revocation(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)

    owner_reg = client.post(
        "/api/v1/auth/register",
        json={
            "email": "owner@org.com",
            "password": "Password123!",
            "full_name": "Org Owner",
            "org_name": "Alpha Org",
        },
    ).json()
    owner_tok = owner_reg["access_token"]
    owner_id = owner_reg["user"]["id"]

    # Viewer user signs up with own org, gets invited as viewer to Alpha Org
    viewer_reg = client.post(
        "/api/v1/auth/register",
        json={
            "email": "viewer@org.com",
            "password": "Password123!",
            "full_name": "Viewer User",
            "org_name": "Viewer Side Org",
        },
    ).json()
    viewer_tok = viewer_reg["access_token"]
    viewer_id = viewer_reg["user"]["id"]

    inv = client.post(
        "/api/v1/orgs/current/invitations",
        headers={"Authorization": f"Bearer {owner_tok}"},
        json={"email": "viewer@org.com", "role": "viewer"},
    )
    assert inv.status_code == 201
    inv_tok = inv.json()["invite_token"]

    acc = client.post(
        "/api/v1/orgs/invitations/accept",
        headers={"Authorization": f"Bearer {viewer_tok}"},
        json={"token": inv_tok},
    )
    assert acc.status_code == 200
    assert acc.json()["id"] == owner_reg["active_org"]["id"]
    assert acc.json()["role"] == "viewer"

    # Viewer cannot invite members or modify org settings (RBAC 403)
    assert (
        client.post(
            "/api/v1/orgs/current/invitations",
            headers={"Authorization": f"Bearer {viewer_tok}"},
            json={"email": "other@org.com", "role": "developer"},
        ).status_code
        == 403
    )
    assert (
        client.patch(
            "/api/v1/orgs/current",
            headers={"Authorization": f"Bearer {viewer_tok}"},
            json={"name": "Hijacked Name"},
        ).status_code
        == 403
    )

    # Cannot remove last active owner
    assert (
        client.delete(
            f"/api/v1/orgs/current/members/{owner_id}",
            headers={"Authorization": f"Bearer {owner_tok}"},
        ).status_code
        == 409
    )

    # Promote viewer -> admin, then transfer ownership atomically
    prom = client.patch(
        f"/api/v1/orgs/current/members/{viewer_id}/role",
        headers={"Authorization": f"Bearer {owner_tok}"},
        json={"role": "admin"},
    )
    assert prom.status_code == 200
    assert prom.json()["role"] == "admin"

    xfer = client.post(
        "/api/v1/orgs/current/transfer-ownership",
        headers={"Authorization": f"Bearer {owner_tok}"},
        json={"new_owner_user_id": viewer_id},
    )
    assert xfer.status_code == 200

    # Former owner is now admin; removing former owner succeeds & immediately kills their session
    rem = client.delete(
        f"/api/v1/orgs/current/members/{owner_id}",
        headers={"Authorization": f"Bearer {viewer_tok}"},
    )
    assert rem.status_code == 200
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {owner_tok}"}).status_code == 401


def test_cross_tenant_composite_fk_orm_criteria_and_object_store(tmp_path: Path) -> None:
    db_path = tmp_path / "iso.db"
    engine = build_engine(f"sqlite:///{db_path}")
    init_db_schema(engine)
    factory = create_session_factory(engine)

    with factory() as s:
        t1 = Tenant(id="org_1", slug="org-one", name="Org One")
        t2 = Tenant(id="org_2", slug="org-two", name="Org Two")
        s.add_all([t1, t2])
        s.flush()
        p1 = Project(id="prj_1", tenant_id="org_1", slug="p1", name="P1")
        s.add(p1)
        s.commit()

    # 1. Cross-tenant composite FK violation rejected by DB
    with factory() as s:
        bad_repo = Repository(
            id=f"rep_{secrets.token_hex(4)}",
            tenant_id="org_2",
            project_id="prj_1",  # Belongs to org_1!
            provider="local_git",
            full_name="evil/cross-link",
            clone_url_or_path="https://github.com/evil/cross-link.git",
        )
        s.add(bad_repo)
        with pytest.raises(IntegrityError):
            s.commit()
        s.rollback()

    # 2. Automatic ORM loader criteria filters SELECT queries to active session tenant
    with factory() as s:
        p2 = Project(id="prj_2", tenant_id="org_2", slug="p2", name="P2")
        s.add(p2)
        s.commit()

    with factory() as s:
        set_session_tenant(s, "org_1")
        rows = list(s.scalars(select(Project)))
        assert [r.id for r in rows] == ["prj_1"]
        set_session_tenant(s, "org_2")
        rows2 = list(s.scalars(select(Project)))
        assert [r.id for r in rows2] == ["prj_2"]

    # 3. Object store rejects cross-tenant key access or path traversal
    store = LocalObjectStore(tmp_path / "obj")
    key = build_artifact_key("org_1", "prj_1", "task_1", "spec.json")
    stored = store.put_bytes("org_1", key, b'{"ok":true}', content_type="application/json")
    assert stored.key == key
    assert store.get_bytes("org_1", key) == b'{"ok":true}'
    with pytest.raises(PermissionError):
        store.get_bytes("org_2", key)
    with pytest.raises(PermissionError):
        store.put_bytes("org_1", "tenants/org_1/../../org_2/escape.txt", b"bad")


def test_org_switch_and_forged_tenant_header_ignored(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)

    u1 = client.post(
        "/api/v1/auth/register",
        json={
            "email": "multi@org.com",
            "password": "Password123!",
            "full_name": "Multi Org User",
            "org_name": "Org Alpha",
        },
    ).json()
    tok = u1["access_token"]
    org1_id = u1["active_org"]["id"]

    org2 = client.post(
        "/api/v1/orgs",
        headers={"Authorization": f"Bearer {tok}"},
        json={"name": "Org Beta"},
    )
    assert org2.status_code == 201
    org2_id = org2.json()["id"]

    # Victim org created separately
    victim = client.post(
        "/api/v1/auth/register",
        json={
            "email": "victim@org.com",
            "password": "Password123!",
            "full_name": "Victim Owner",
            "org_name": "Victim Corp",
        },
    ).json()
    victim_org_id = victim["active_org"]["id"]

    # Forged X-Tenant-Id header must never hijack session tenant or switch without membership
    me = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {tok}", "X-Tenant-Id": victim_org_id},
    )
    assert me.status_code == 200
    assert me.json()["active_org"]["id"] == org2_id

    assert (
        client.post(
            "/api/v1/orgs/switch",
            headers={"Authorization": f"Bearer {tok}"},
            json={"tenant_id": victim_org_id},
        ).status_code
        == 403
    )

    sw = client.post(
        "/api/v1/orgs/switch",
        headers={"Authorization": f"Bearer {tok}"},
        json={"tenant_id": org1_id},
    )
    assert sw.status_code == 200
    assert sw.json()["id"] == org1_id

