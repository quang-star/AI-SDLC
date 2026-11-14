"""Live PostgreSQL 16 FORCE RLS (NOBYPASSRLS), Redis rate limiter, and pg_dump/pg_restore E2E tests."""

from __future__ import annotations

import os
import secrets
import socket
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pytest
from fastapi.testclient import TestClient
from scripts.backup_restore_drill import run_postgres_backup_restore_drill
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ai_sdlc_saas.api.app import create_app
from ai_sdlc_saas.api.runtime import SlidingWindowRateLimiter
from ai_sdlc_saas.billing.plans import seed_default_plans
from ai_sdlc_saas.config import Settings
from ai_sdlc_saas.db.engine import postgres_rls_statements, set_session_tenant
from ai_sdlc_saas.db.models import TENANT_SCOPED_MODELS, Base
from ai_sdlc_saas.errors import RateLimitError
from ai_sdlc_saas.orchestrator.executors import build_production_executors
from ai_sdlc_saas.orchestrator.janitor import janitor_sweep
from ai_sdlc_saas.orchestrator.worker import drain_ready_jobs

PG_OWNER_BASE_URL = os.getenv(
    "AI_SDLC_TEST_PG_OWNER_URL",
    "postgresql+psycopg://ai_sdlc_owner:ai_sdlc_owner_dev_PLACEHOLDER@127.0.0.1:5432/ai_sdlc",
)
PG_APP_BASE_URL = os.getenv(
    "AI_SDLC_TEST_PG_APP_URL",
    "postgresql+psycopg://ai_sdlc_app:ai_sdlc_app_dev_password@127.0.0.1:5432/ai_sdlc",
)
REDIS_LIVE_URL = os.getenv("AI_SDLC_TEST_REDIS_URL", "redis://127.0.0.1:16379/0")


def _can_connect(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _replace_db(url: str, db_name: str) -> str:
    parsed = urlparse(url)
    return urlunparse(parsed._replace(path=f"/{db_name}"))


@pytest.fixture()
def isolated_pg_urls() -> Iterator[tuple[str, str, Engine, Engine]]:
    parsed_owner = urlparse(PG_OWNER_BASE_URL)
    pg_host = parsed_owner.hostname or "127.0.0.1"
    pg_port = parsed_owner.port or 5432
    if not _can_connect(pg_host, pg_port):
        pytest.skip(f"PostgreSQL not reachable at {pg_host}:{pg_port}")

    test_db = f"ai_sdlc_rls_test_{secrets.token_hex(4)}"
    admin_url = _replace_db(PG_OWNER_BASE_URL, "postgres")
    owner_url = _replace_db(PG_OWNER_BASE_URL, test_db)
    app_url = _replace_db(PG_APP_BASE_URL, test_db)

    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT", future=True)
    with admin_engine.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{test_db}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{test_db}" OWNER ai_sdlc_owner'))

    owner_engine = create_engine(owner_url, future=True)
    app_engine = create_engine(app_url, future=True)
    try:
        with owner_engine.begin() as conn:
            Base.metadata.create_all(bind=conn)
            for stmt in postgres_rls_statements():
                conn.execute(text(stmt))
            conn.execute(text("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO ai_sdlc_app;"))
            conn.execute(text("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ai_sdlc_app;"))
        with Session(owner_engine) as s:
            seed_default_plans(s)
            s.commit()

        yield owner_url, app_url, owner_engine, app_engine
    finally:
        app_engine.dispose()
        owner_engine.dispose()
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{test_db}" WITH (FORCE)'))
        admin_engine.dispose()


def _make_pg_client(tmp_path: Path, app_url: str, *, redis_url: str = "") -> TestClient:
    settings = Settings(
        env="test",
        database_url=app_url,
        redis_url=redis_url,
        object_store_dir=str(tmp_path / "objects"),
        worktree_root=str(tmp_path / "worktrees"),
        jwt_secret="pg-live-jwt-secret-key-min-32-chars!!",
        encryption_key="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
        github_webhook_secret="gh-sec",
        stripe_webhook_secret="st-sec",
        allow_local_git_repos=True,
    )
    return TestClient(create_app(settings))


def test_postgres_rls_role_and_all_38_tables_enforced(
    isolated_pg_urls: tuple[str, str, Engine, Engine],
) -> None:
    _owner_url, _app_url, owner_engine, _app_engine = isolated_pg_urls
    expected_tables = [model.__tablename__ for model in TENANT_SCOPED_MODELS]
    assert len(expected_tables) == 38

    with Session(owner_engine) as s:
        role_row = s.execute(
            text(
                "SELECT rolsuper, rolbypassrls, rolcanlogin "
                "FROM pg_roles WHERE rolname = 'ai_sdlc_app'"
            )
        ).one()
        assert role_row[0] is False, "ai_sdlc_app must NOT be superuser"
        assert role_row[1] is False, "ai_sdlc_app must have NOBYPASSRLS"
        assert role_row[2] is True

        class_rows = s.execute(
            text(
                "SELECT relname, relrowsecurity, relforcerowsecurity "
                "FROM pg_class WHERE relnamespace = 'public'::regnamespace "
                "AND relname = ANY(:names)"
            ),
            {"names": expected_tables},
        ).all()
        rls_by_table = {r[0]: (bool(r[1]), bool(r[2])) for r in class_rows}
        assert set(rls_by_table.keys()) == set(expected_tables)
        for tbl, (rls_on, force_on) in rls_by_table.items():
            assert rls_on is True, f"{tbl} missing ENABLE ROW LEVEL SECURITY"
            assert force_on is True, f"{tbl} missing FORCE ROW LEVEL SECURITY"

        policy_rows = s.execute(
            text("SELECT tablename, policyname FROM pg_policies WHERE schemaname = 'public'")
        ).all()
        policy_pairs = {(r[0], r[1]) for r in policy_rows}
        for tbl in expected_tables:
            assert (tbl, f"{tbl}_tenant_isolation") in policy_pairs, f"{tbl} missing RLS policy"


def test_postgres_raw_sql_negative_and_positive_rls_isolation(
    tmp_path: Path,
    isolated_pg_urls: tuple[str, str, Engine, Engine],
) -> None:
    _owner_url, app_url, _owner_engine, _app_engine = isolated_pg_urls
    client = _make_pg_client(tmp_path, app_url)

    reg_a = client.post(
        "/api/v1/auth/register",
        json={"email": "pg_a@example.com", "password": "Password123!", "full_name": "PG A", "org_name": "PG Org A"},
    ).json()
    reg_b = client.post(
        "/api/v1/auth/register",
        json={"email": "pg_b@example.com", "password": "Password123!", "full_name": "PG B", "org_name": "PG Org B"},
    ).json()
    t_a = reg_a["active_org"]["id"]
    t_b = reg_b["active_org"]["id"]
    h_a = {"Authorization": f"Bearer {reg_a['access_token']}"}
    h_b = {"Authorization": f"Bearer {reg_b['access_token']}"}

    proj_a = client.post("/api/v1/projects", headers=h_a, json={"name": "Alpha PG"}).json()["id"]
    proj_b = client.post("/api/v1/projects", headers=h_b, json={"name": "Beta PG"}).json()["id"]
    task_a = client.post(
        "/api/v1/tasks",
        headers=h_a,
        json={
            "project_id": proj_a,
            "title": "RLS Check Task",
            "requirement_text": "Document row level security policies cleanly.",
            "task_type": "docs",
            "repo_ids": [],
        },
    ).json()["id"]

    runtime = client.app.state.runtime  # type: ignore[attr-defined]

    # 1. Unbound session must see 0 rows in strict AND system-routing tables, and reject writes
    with runtime.session_factory() as s:
        assert s.execute(text("SELECT count(*) FROM projects")).scalar_one() == 0
        assert s.execute(text("SELECT count(*) FROM tasks WHERE id = :id"), {"id": task_a}).scalar_one() == 0
        with pytest.raises(SQLAlchemyError):
            s.execute(
                text(
                    "INSERT INTO projects (id, tenant_id, slug, name, created_at, updated_at) "
                    "VALUES ('prj_rls_unb', :tid, 'unb', 'Unbound', now(), now())"
                ),
                {"tid": t_a},
            )
        s.rollback()

    # 2. Session bound to Tenant B sees only Tenant B, cannot update/delete/insert Tenant A rows
    with runtime.session_factory() as s:
        set_session_tenant(s, t_b)
        assert s.execute(text("SELECT count(*) FROM projects WHERE id = :id"), {"id": proj_a}).scalar_one() == 0
        assert s.execute(text("SELECT count(*) FROM projects WHERE id = :id"), {"id": proj_b}).scalar_one() == 1
        upd = s.execute(text("UPDATE projects SET name = 'tamper' WHERE id = :id"), {"id": proj_a})
        assert upd.rowcount == 0
        dlt = s.execute(text("DELETE FROM projects WHERE id = :id"), {"id": proj_a})
        assert dlt.rowcount == 0
        with pytest.raises(SQLAlchemyError):
            s.execute(
                text(
                    "INSERT INTO projects (id, tenant_id, slug, name, created_at, updated_at) "
                    "VALUES ('prj_cross', :tid, 'cross', 'Cross', now(), now())"
                ),
                {"tid": t_a},
            )
        s.rollback()

    # 3. Session bound to Tenant A sees Tenant A cleanly
    with runtime.session_factory() as s:
        set_session_tenant(s, t_a)
        assert s.execute(text("SELECT count(*) FROM projects WHERE id = :id"), {"id": proj_a}).scalar_one() == 1
        assert s.execute(text("SELECT count(*) FROM projects WHERE id = :id"), {"id": proj_b}).scalar_one() == 0


def test_postgres_full_workflow_rls_end_to_end(
    tmp_path: Path,
    isolated_pg_urls: tuple[str, str, Engine, Engine],
) -> None:
    _owner_url, app_url, _owner_engine, _app_engine = isolated_pg_urls
    client = _make_pg_client(tmp_path, app_url)
    runtime = client.app.state.runtime  # type: ignore[attr-defined]

    reg_a = client.post(
        "/api/v1/auth/register",
        json={"email": "owner_e2e@example.com", "password": "Password123!", "full_name": "E2E Owner", "org_name": "E2E Org A"},
    ).json()
    tenant_a = reg_a["active_org"]["id"]
    slug_a = reg_a["active_org"]["slug"]

    # Login exercises system_routing_scope for cross-tenant membership lookup
    login_a = client.post("/api/v1/auth/login", json={"email": "owner_e2e@example.com", "password": "Password123!"})
    assert login_a.status_code == 200, login_a.text
    h_a = {"Authorization": f"Bearer {login_a.json()['access_token']}"}

    reg_b = client.post(
        "/api/v1/auth/register",
        json={"email": "other_e2e@example.com", "password": "Password123!", "full_name": "Other User", "org_name": "E2E Org B"},
    ).json()
    h_b = {"Authorization": f"Bearer {reg_b['access_token']}"}

    # Create project + template repo + task, then drain worker jobs under ai_sdlc_app RLS
    proj_a = client.post("/api/v1/projects", headers=h_a, json={"name": "RLS E2E Project"}).json()["id"]
    repo_resp = client.post(
        "/api/v1/repos/from-template",
        headers=h_a,
        json={"project_id": proj_a, "template_id": "python-fastapi-starter", "repo_name": "pg-svc", "run_baseline": False},
    )
    assert repo_resp.status_code == 201, repo_resp.text

    task_resp = client.post(
        "/api/v1/tasks",
        headers=h_a,
        json={
            "project_id": proj_a,
            "title": "PostgreSQL RLS docs",
            "requirement_text": "Document PostgreSQL row level security enforcement and operational recovery.",
            "task_type": "docs",
            "repo_ids": [repo_resp.json()["id"]],
        },
    )
    assert task_resp.status_code == 201, task_resp.text
    task_id = task_resp.json()["id"]

    processed = drain_ready_jobs(runtime, max_jobs=10, executors=build_production_executors())
    assert len(processed) >= 1
    task_state = client.get(f"/api/v1/tasks/{task_id}", headers=h_a)
    assert task_state.status_code == 200
    assert task_state.json()["status"] in ("WAITING_INPUT", "WAITING_APPROVAL", "DELIVERED")

    # Cross-tenant HTTP isolation check
    assert client.get(f"/api/v1/projects/{proj_a}", headers=h_b).status_code in (403, 404)

    # Invite & accept & switch active org under RLS
    inv = client.post(
        "/api/v1/orgs/current/invitations",
        headers=h_a,
        json={"email": "other_e2e@example.com", "role": "developer"},
    )
    assert inv.status_code == 201, inv.text
    acc = client.post("/api/v1/orgs/invitations/accept", headers=h_b, json={"token": inv.json()["invite_token"]})
    assert acc.status_code == 200, acc.text
    sw = client.post("/api/v1/orgs/switch", headers=h_b, json={"tenant_id": tenant_a})
    assert sw.status_code == 200, sw.text

    # Janitor sweep + metrics + export + hard-delete
    with runtime.session_factory() as s:
        janitor_sweep(s)
        s.commit()

    metrics = client.get("/api/v1/metrics")
    assert metrics.status_code == 200
    assert "ai_sdlc_http_requests_total" in metrics.text

    exp = client.post("/api/v1/org/exports", headers=h_a)
    assert exp.status_code == 201, exp.text
    assert client.get(f"/api/v1/org/exports/{exp.json()['id']}/download", headers=h_a).status_code == 200

    hd = client.post("/api/v1/org/hard-delete", headers=h_a, json={"confirm_slug": slug_a})
    assert hd.status_code == 200, hd.text
    assert client.get("/api/v1/projects", headers=h_a).status_code == 401


def test_redis_rate_limiter_live() -> None:
    parsed = urlparse(REDIS_LIVE_URL)
    r_host = parsed.hostname or "127.0.0.1"
    r_port = parsed.port or 6379
    if not _can_connect(r_host, r_port):
        pytest.skip(f"Redis not reachable at {r_host}:{r_port}")

    key = f"rl_live_{secrets.token_hex(6)}"
    limiter_a = SlidingWindowRateLimiter(max_requests=3, window_seconds=30, redis_url=REDIS_LIVE_URL)
    limiter_b = SlidingWindowRateLimiter(max_requests=3, window_seconds=30, redis_url=REDIS_LIVE_URL)

    limiter_a.check(key)
    limiter_b.check(key)
    limiter_a.check(key)
    with pytest.raises(RateLimitError) as exc_info:
        limiter_b.check(key)
    assert exc_info.value.details["backend"] == "redis"

    # Unreachable Redis falls back cleanly to in-memory
    fallback = SlidingWindowRateLimiter(max_requests=2, window_seconds=30, redis_url="redis://127.0.0.1:19999/0")
    fallback.check(key)
    fallback.check(key)
    with pytest.raises(RateLimitError):
        fallback.check(key)


def test_postgres_backup_restore_drill_live(tmp_path: Path) -> None:
    parsed = urlparse(PG_OWNER_BASE_URL)
    pg_host = parsed.hostname or "127.0.0.1"
    pg_port = parsed.port or 5432
    if not _can_connect(pg_host, pg_port):
        pytest.skip(f"PostgreSQL not reachable at {pg_host}:{pg_port}")

    report = run_postgres_backup_restore_drill(database_url=PG_OWNER_BASE_URL, work_dir=tmp_path / "pg_drill")
    assert report["ok"] is True
    assert report["mode"] == "postgres"
    assert report["dump_bytes"] > 0
    assert report["rls_verified_in_restore"]["projects"] == {"rls": True, "force_rls": True}
    assert report["rls_verified_in_restore"]["tasks"] == {"rls": True, "force_rls": True}
