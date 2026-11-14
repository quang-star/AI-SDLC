"""P5 fault-injection, checkpoint recovery, gate RBAC/staleness, janitor, and SSE replay tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from ai_sdlc_saas.api.app import create_app
from ai_sdlc_saas.config import Settings
from ai_sdlc_saas.db.models import CleanupJob, SandboxResource, Task
from ai_sdlc_saas.orchestrator.graph import SdlcGraphState, WorkerCrashError
from ai_sdlc_saas.orchestrator.janitor import cleanup_task_resources, janitor_sweep, register_sandbox_resource
from ai_sdlc_saas.orchestrator.worker import drain_ready_jobs, process_one_job


def _setup(tmp_path: Path) -> tuple[TestClient, Settings]:
    settings = Settings(
        env="test",
        database_url=f"sqlite:///{tmp_path / 'orch.db'}",
        object_store_dir=str(tmp_path / "objects"),
        worktree_root=str(tmp_path / "worktrees"),
        jwt_secret="test-jwt-secret-key-min-32-chars!!",
        encryption_key="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
        github_webhook_secret="gh-sec",
        stripe_webhook_secret="st-sec",
        allow_local_git_repos=True,
    )
    return TestClient(create_app(settings)), settings


def _bootstrap_repo(client: TestClient, email: str, org_name: str) -> tuple[str, str, str, str]:
    reg = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "Password123!", "full_name": "Owner", "org_name": org_name},
    ).json()
    tok = reg["access_token"]
    org_id = reg["active_org"]["id"]
    h = {"Authorization": f"Bearer {tok}"}
    proj_id = client.post("/api/v1/projects", headers=h, json={"name": "Core Platform"}).json()["id"]
    repo = client.post(
        "/api/v1/repos/from-template",
        headers=h,
        json={"project_id": proj_id, "template_id": "python-fastapi-starter", "repo_name": "app", "run_baseline": False},
    ).json()
    return tok, org_id, proj_id, repo["id"]


def test_full_interrupt_resume_double_click_and_fault_recovery(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    tok, _org_id, proj_id, repo_id = _bootstrap_repo(client, "o@fault.com", "Fault Org")
    h = {"Authorization": f"Bearer {tok}"}

    # Double-click Idempotency-Key creates only 1 task
    body = {
        "project_id": proj_id,
        "title": "Fix calculation bug",
        "requirement_text": "Update add_numbers carefully",
        "task_type": "bugfix",
        "repo_ids": [repo_id],
        "risk_level": "standard",
    }
    t1 = client.post("/api/v1/tasks", headers={**h, "Idempotency-Key": "create-1"}, json=body)
    t2 = client.post("/api/v1/tasks", headers={**h, "Idempotency-Key": "create-1"}, json=body)
    assert t1.status_code == 201 and t2.status_code == 201
    assert t1.json()["id"] == t2.json()["id"]
    assert t2.json()["deduplicated"] is True
    task_id = t1.json()["id"]

    # Inject crash once at every critical point (model_call, approval_wait, vm_create, publish_intent)
    fired: set[str] = set()

    def crash_once(stage: str, _state: SdlcGraphState) -> None:
        if stage in ("model_call", "approval_wait", "vm_create", "publish_intent") and stage not in fired:
            fired.add(stage)
            raise WorkerCrashError(f"Simulated crash at {stage}")

    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    # Run until model_call crashes once, then recovers to WAITING_APPROVAL (spec)
    drain_ready_jobs(runtime, max_jobs=5, fault_hook=crash_once)
    assert {"model_call", "approval_wait"} <= fired

    detail = client.get(f"/api/v1/tasks/{task_id}", headers=h).json()
    assert detail["status"] == "WAITING_APPROVAL"
    base_sha = next(iter(detail["run"]["base_commits"].values()))
    policy_ver = detail["run"]["policy_version"]

    # Stale digest -> 409 Conflict
    stale = client.post(
        f"/api/v1/tasks/{task_id}/approvals",
        headers=h,
        json={
            "gate_type": "spec",
            "decision": "approved",
            "target_digest": "deadbeef" * 8,
            "base_commit_sha": base_sha,
            "policy_version": policy_ver,
        },
    )
    assert stale.status_code == 409

    # Valid spec approval -> resumes to plan gate
    apr_spec = client.post(
        f"/api/v1/tasks/{task_id}/approvals",
        headers={**h, "Idempotency-Key": "apr-spec-1"},
        json={
            "gate_type": "spec",
            "decision": "approved",
            "target_digest": detail["run"]["spec_sha256"],
            "base_commit_sha": base_sha,
            "policy_version": policy_ver,
        },
    )
    assert apr_spec.status_code == 201
    drain_ready_jobs(runtime, max_jobs=5, fault_hook=crash_once)

    detail2 = client.get(f"/api/v1/tasks/{task_id}", headers=h).json()
    assert detail2["current_stage"] == "plan_approval_gate"

    # Valid plan approval -> crashes once at vm_create, recovers cleanly, lands on delivery gate
    client.post(
        f"/api/v1/tasks/{task_id}/approvals",
        headers=h,
        json={
            "gate_type": "plan",
            "decision": "approved",
            "target_digest": detail2["run"]["plan_sha256"],
            "base_commit_sha": base_sha,
            "policy_version": policy_ver,
        },
    )
    drain_ready_jobs(runtime, max_jobs=5, fault_hook=crash_once)
    assert "vm_create" in fired

    detail3 = client.get(f"/api/v1/tasks/{task_id}", headers=h).json()
    assert detail3["current_stage"] == "delivery_approval_gate"

    # Valid delivery approval -> crashes once at publish_intent, recovers & settles cleanly
    client.post(
        f"/api/v1/tasks/{task_id}/approvals",
        headers=h,
        json={
            "gate_type": "delivery",
            "decision": "approved",
            "target_digest": detail3["run"]["patch_sha256"],
            "base_commit_sha": base_sha,
            "policy_version": policy_ver,
        },
    )
    drain_ready_jobs(runtime, max_jobs=5, fault_hook=crash_once)
    assert "publish_intent" in fired

    final_detail = client.get(f"/api/v1/tasks/{task_id}", headers=h).json()
    assert final_detail["status"] == "DELIVERED"
    assert final_detail["cleanup_status"] == "CLEANED"


def test_fast_track_policy_and_revoked_actor_resume_block(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    tok_owner, _org, proj_id, repo_id = _bootstrap_repo(client, "own@sec.com", "Sec Org")
    h_own = {"Authorization": f"Bearer {tok_owner}"}

    # 1. Auth requirement cannot self-downgrade to fast_track
    high_t = client.post(
        "/api/v1/tasks",
        headers=h_own,
        json={
            "project_id": proj_id,
            "title": "Update JWT authentication middleware",
            "requirement_text": "Modify auth token validation in auth/jwt.py",
            "task_type": "bugfix",
            "repo_ids": [repo_id],
            "risk_level": "low",
            "fast_track": True,
        },
    )
    assert high_t.status_code == 201
    assert high_t.json()["risk_level"] == "high"
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    drain_ready_jobs(runtime, max_jobs=5)
    assert client.get(f"/api/v1/tasks/{high_t.json()['id']}", headers=h_own).json()["status"] == "WAITING_APPROVAL"

    # 2. Invite reviewer, let reviewer approve spec, then revoke reviewer BEFORE worker resumes
    rev_reg = client.post(
        "/api/v1/auth/register",
        json={"email": "rev@sec.com", "password": "Password123!", "full_name": "Reviewer", "org_name": "Side"},
    ).json()
    rev_tok = rev_reg["access_token"]
    rev_uid = rev_reg["user"]["id"]
    inv_tok = client.post(
        "/api/v1/orgs/current/invitations",
        headers=h_own,
        json={"email": "rev@sec.com", "role": "reviewer"},
    ).json()["invite_token"]
    client.post("/api/v1/orgs/invitations/accept", headers={"Authorization": f"Bearer {rev_tok}"}, json={"token": inv_tok})

    detail = client.get(f"/api/v1/tasks/{high_t.json()['id']}", headers=h_own).json()
    base_sha = next(iter(detail["run"]["base_commits"].values()))
    apr = client.post(
        f"/api/v1/tasks/{high_t.json()['id']}/approvals",
        headers={"Authorization": f"Bearer {rev_tok}"},
        json={
            "gate_type": "spec",
            "decision": "approved",
            "target_digest": detail["run"]["spec_sha256"],
            "base_commit_sha": base_sha,
            "policy_version": detail["run"]["policy_version"],
        },
    )
    assert apr.status_code == 201

    # Revoke reviewer membership before worker runs!
    client.delete(f"/api/v1/orgs/current/members/{rev_uid}", headers=h_own)
    res = process_one_job(runtime)
    assert res is not None and "revoked" in res.get("rejected_reason", "").lower()

    after = client.get(f"/api/v1/tasks/{high_t.json()['id']}", headers=h_own).json()
    assert after["status"] == "WAITING_APPROVAL"
    assert after["error_code"] == "actor_revoked"


def test_janitor_failed_cleanup_tracking_and_sse_cursor_isolation(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    tok_a, org_a, proj_a, repo_a = _bootstrap_repo(client, "a@sse.com", "SSE A")
    tok_b, _org_b, _proj_b, _repo_b = _bootstrap_repo(client, "b@sse.com", "SSE B")
    h_a = {"Authorization": f"Bearer {tok_a}"}
    h_b = {"Authorization": f"Bearer {tok_b}"}

    task = client.post(
        "/api/v1/tasks",
        headers=h_a,
        json={
            "project_id": proj_a,
            "title": "Typo fix in docs",
            "requirement_text": "Clarify docstring typo",
            "task_type": "docs",
            "repo_ids": [repo_a],
            "risk_level": "low",
            "fast_track": True,
        },
    ).json()
    tid = task["id"]
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    drain_ready_jobs(runtime, max_jobs=5)

    # Full SSE stream & Last-Event-ID replay
    full_sse = client.get(f"/api/v1/tasks/{tid}/events", headers=h_a)
    assert full_sse.status_code == 200
    assert "text/event-stream" in full_sse.headers["content-type"]
    lines = [ln for ln in full_sse.text.splitlines() if ln.startswith("id: ")]
    assert len(lines) >= 3
    first_seq = int(lines[0].split(": ")[1])

    replay = client.get(f"/api/v1/tasks/{tid}/events", headers={**h_a, "Last-Event-ID": str(first_seq)})
    assert f"id: {first_seq}\n" not in replay.text
    assert f"id: {first_seq + 1}" in replay.text

    # Cross-tenant SSE blocked cleanly (404)
    assert client.get(f"/api/v1/tasks/{tid}/events", headers=h_b).status_code == 404

    # Orphan sandbox resource + failed cleanup visibility + janitor sweep recovery
    with runtime.session_factory() as s:
        res = register_sandbox_resource(
            s,
            tenant_id=org_a,
            task_id=tid,
            run_id=task["run_id"],
            resource_type="worktree",
            provider="local_worktree",
            external_resource_id=str(tmp_path / "orphan_wt"),
            idempotency_key="orphan-1",
            ttl_seconds=10,
        )
        res.expires_at = datetime.now(UTC) - timedelta(minutes=5)
        s.commit()

        def failing_del(_r: SandboxResource) -> None:
            raise OSError("Simulated provider timeout")

        status = cleanup_task_resources(s, tenant_id=org_a, task_id=tid, deleter=failing_del)
        s.commit()
        assert status == "FAILED"
        assert s.get(Task, tid).cleanup_status == "FAILED"  # type: ignore[union-attr]
        assert s.query(CleanupJob).count() == 1

    # Janitor retries and marks CLEANED once provider recovers
    with runtime.session_factory() as s:
        summary = janitor_sweep(s)
        s.commit()
        assert summary["cleaned"] >= 1
        assert s.get(Task, tid).cleanup_status == "CLEANED"  # type: ignore[union-attr]
