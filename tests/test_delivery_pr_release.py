"""P10 delivery tests: PR creation, webhook CI rework loop, private previews, traceability manual confirm & release."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from ai_sdlc_saas.api.app import create_app
from ai_sdlc_saas.config import Settings
from ai_sdlc_saas.db.engine import set_session_tenant
from ai_sdlc_saas.db.models import (
    GitHubInstallation,
    PreviewEnvironment,
    Repository,
    Task,
    TaskRun,
    TraceabilityEntryRecord,
    VerificationReportRecord,
)
from ai_sdlc_saas.gateway.adapters import DeterministicMockAdapter, GatewayResponse
from ai_sdlc_saas.orchestrator.executors import build_production_executors
from ai_sdlc_saas.orchestrator.worker import drain_ready_jobs


def _setup(tmp_path: Path) -> TestClient:
    settings = Settings(
        env="test",
        database_url=f"sqlite:///{tmp_path / 'p10.db'}",
        object_store_dir=str(tmp_path / "objects"),
        worktree_root=str(tmp_path / "worktrees"),
        jwt_secret="test-jwt-secret-key-min-32-chars!!",
        encryption_key="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
        github_webhook_secret="gh-sec",
        stripe_webhook_secret="st-sec",
        allow_local_git_repos=True,
    )
    return TestClient(create_app(settings))


def _pass_adapter() -> DeterministicMockAdapter:
    def _handler(messages: list[dict[str, str]], model_id: str) -> GatewayResponse:
        joined = "\n".join(m.get("content", "") for m in messages)
        if "MetaGPT ProductManager" in joined:
            payload = {
                "clarification_questions": [],
                "target_files": ["src/app/main.py"],
                "expected_new_test_files": ["tests/test_subtract.py"],
            }
        elif "ChatDev" in joined and "Critique" in joined:
            payload = {"verdict": "accept", "issues": []}
        elif "ChatDev" in joined and "Review patch diff" in joined:
            payload = {"verdict": "APPROVE", "findings": [], "summary": "Approved"}
        elif "Coding Agent inside a sandboxed worktree" in joined:
            payload = {
                "test_files": {
                    "tests/test_subtract.py": (
                        "from app.main import subtract_numbers\n\n\n"
                        "def test_subtract_numbers() -> None:\n"
                        "    assert subtract_numbers(9, 4) == 5\n"
                    )
                },
                "code_files": {
                    "src/app/main.py": (
                        "from __future__ import annotations\n\n"
                        "from fastapi import FastAPI\n\n"
                        "app = FastAPI(title='Starter App')\n\n\n"
                        "@app.get('/health')\n"
                        "def health() -> dict[str, str]:\n"
                        "    return {'status': 'ok'}\n\n\n"
                        "def add_numbers(a: int, b: int) -> int:\n"
                        "    return a + b\n\n\n"
                        "def subtract_numbers(a: int, b: int) -> int:\n"
                        "    return a - b\n"
                    )
                },
            }
        else:
            payload = {"ok": True}
        return GatewayResponse(
            content=f"[CONTENT]{json.dumps(payload)}[/CONTENT]",
            actual_model_id=model_id,
            input_tokens=30,
            output_tokens=50,
            cached_tokens=0,
            usage_status="CONFIRMED",
        )

    return DeterministicMockAdapter(handler=_handler)


def _approve(client: TestClient, h: dict[str, str], task_id: str, gate_type: str) -> None:
    run = client.get(f"/api/v1/tasks/{task_id}", headers=h).json()["run"]
    digest = run["spec_sha256"] if gate_type == "spec" else (run["plan_sha256"] if gate_type == "plan" else run["patch_sha256"])
    base_sha = next(iter((run["base_commits"] or {}).values()), "docs-base")
    resp = client.post(
        f"/api/v1/tasks/{task_id}/approvals",
        headers={**h, "Idempotency-Key": f"apr-{task_id}-{gate_type}-{digest[:8]}"},
        json={
            "gate_type": gate_type,
            "decision": "approved",
            "target_digest": digest,
            "base_commit_sha": base_sha,
            "policy_version": str((run.get("frozen_policy") or {}).get("policy_digest") or "policy-v1"),
        },
    )
    assert resp.status_code == 201, resp.text


def test_pr_webhook_ci_rework_private_preview_and_release_promotion(tmp_path: Path) -> None:
    client = _setup(tmp_path)
    reg = client.post(
        "/api/v1/auth/register",
        json={"email": "owner@p10.dev", "password": "Password123!", "full_name": "P10 Owner", "org_name": "P10 Corp"},
    ).json()
    tenant_id = reg["active_org"]["id"]
    h = {"Authorization": f"Bearer {reg['access_token']}"}

    # Invite a developer to test release:approve & delivery:approve RBAC enforcement
    inv = client.post(
        "/api/v1/orgs/current/invitations",
        headers=h,
        json={"email": "dev@p10.dev", "role": "developer"},
    )
    assert inv.status_code == 201, inv.text
    inv_tok = inv.json()["invite_token"]
    dev_reg = client.post(
        "/api/v1/auth/register",
        json={"email": "dev@p10.dev", "password": "Password123!", "full_name": "P10 Dev", "org_name": "Other Org"},
    ).json()
    dev_h = {"Authorization": f"Bearer {dev_reg['access_token']}"}
    assert client.post("/api/v1/orgs/invitations/accept", headers=dev_h, json={"token": inv_tok}).status_code == 200

    # Separate tenant B for cross-tenant preview isolation check
    b_reg = client.post(
        "/api/v1/auth/register",
        json={"email": "b@evil.dev", "password": "Password123!", "full_name": "Evil B", "org_name": "Evil Corp"},
    ).json()
    b_h = {"Authorization": f"Bearer {b_reg['access_token']}"}

    proj_id = client.post("/api/v1/projects", headers=h, json={"name": "P10 Service"}).json()["id"]
    repo_id = client.post(
        "/api/v1/repos/from-template",
        headers=h,
        json={"project_id": proj_id, "template_id": "python-fastapi-starter", "repo_name": "p10-api", "run_baseline": False},
    ).json()["id"]

    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    adapters = {"default": _pass_adapter()}
    ex = build_production_executors()

    task_id = client.post(
        "/api/v1/tasks",
        headers=h,
        json={
            "project_id": proj_id,
            "title": "Add subtract_numbers with release flow",
            "requirement_text": "Implement subtract_numbers in src/app/main.py returning a - b with unit tests.",
            "task_type": "feature",
            "repo_ids": [repo_id],
        },
    ).json()["id"]

    drain_ready_jobs(runtime, executors=ex, override_adapters=adapters)
    _approve(client, h, task_id, "spec")
    drain_ready_jobs(runtime, executors=ex, override_adapters=adapters)
    _approve(client, h, task_id, "plan")
    drain_ready_jobs(runtime, executors=ex, override_adapters=adapters)

    # Verification endpoint returns PASS report + traceability
    ver_resp = client.get(f"/api/v1/tasks/{task_id}/verification", headers=h)
    assert ver_resp.status_code == 200
    assert ver_resp.json()["reports"][0]["verdict"] == "PASS"

    _approve(client, h, task_id, "delivery")
    drain_ready_jobs(runtime, executors=ex, override_adapters=adapters)

    prs = client.get(f"/api/v1/tasks/{task_id}/prs", headers=h).json()
    assert len(prs) == 1
    pr = prs[0]
    assert pr["status"] == "open"

    # Private preview access control: unauth=401, cross-tenant=403, valid token/same-tenant=200, destroyed=403
    prv_resp = client.post(f"/api/v1/tasks/{task_id}/previews", headers=h, json={"ttl_seconds": 300})
    assert prv_resp.status_code == 201, prv_resp.text
    prv = prv_resp.json()
    view_url = prv["preview_url"]
    assert client.get(view_url).status_code == 401
    assert client.get(view_url, headers=b_h).status_code == 403
    ok_tok = client.get(f"{view_url}?token={prv['preview_token']}")
    assert ok_tok.status_code == 200 and ok_tok.json()["task_id"] == task_id
    assert client.get(view_url, headers=h).status_code == 200
    assert client.delete(f"/api/v1/previews/{prv['preview_id']}", headers=h).status_code == 200
    assert client.get(f"{view_url}?token={prv['preview_token']}").status_code == 403

    # Expired preview also fails with 403
    prv2 = client.post(f"/api/v1/tasks/{task_id}/previews", headers=h, json={"ttl_seconds": 60}).json()
    with runtime.session_factory() as s:
        row = s.get(PreviewEnvironment, prv2["preview_id"])
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(minutes=5)
        s.commit()
    assert client.get(f"{prv2['preview_url']}?token={prv2['preview_token']}").status_code == 403

    # Developer CANNOT promote release (403); Owner CAN promote release -> release notes & notification created
    rn_resp = client.get(f"/api/v1/tasks/{task_id}/release-notes", headers=h)
    assert rn_resp.status_code == 200 and "Rollback Plan" in rn_resp.json()["release_notes_markdown"]
    assert client.post(f"/api/v1/tasks/{task_id}/release", headers=dev_h, json={"comment": "dev try"}).status_code == 403
    rel_resp = client.post(
        f"/api/v1/tasks/{task_id}/release",
        headers={**h, "Idempotency-Key": "rel-1"},
        json={"comment": "LGTM ship"},
    )
    assert rel_resp.status_code == 200, rel_resp.text
    assert rel_resp.json()["status"] == "promoted"

    notifs = client.get("/api/v1/notifications", headers=h).json()
    assert any(n["event_type"] == "release.promoted" for n in notifs)

    # GitHub webhook CI feedback routing (`check_suite` failure -> bounded CI rework up to 2 rounds)
    with runtime.session_factory() as s:
        repo = s.get(Repository, repo_id)
        assert repo is not None
        repo_full = repo.full_name
        s.add(
            GitHubInstallation(
                id="inst_p10",
                tenant_id=tenant_id,
                external_installation_id="77701",
                account_login="p10-corp",
                ownership_proof_digest="proof-sha",
            )
        )
        s.commit()

    for round_no in (1, 2, 3):
        gh_payload = {
            "installation": {"id": 77701},
            "repository": {"full_name": repo_full},
            "check_suite": {"conclusion": "failure", "pull_requests": [{"number": pr["external_pr_number"]}]},
        }
        raw_b = json.dumps(gh_payload).encode("utf-8")
        sig = "sha256=" + hmac.new(b"gh-sec", raw_b, hashlib.sha256).hexdigest()
        wh = client.post(
            "/api/v1/webhooks/github",
            content=raw_b,
            headers={
                "X-GitHub-Event": "check_suite",
                "X-GitHub-Delivery": f"deliv-ci-{round_no}",
                "X-Hub-Signature-256": sig,
                "Content-Type": "application/json",
            },
        )
        assert wh.status_code == 200, wh.text
        ci_fb = wh.json().get("ci_feedback", {})
        if round_no <= 2:
            assert ci_fb["rework_triggered"] is True
        else:
            # Cap = 2 stops infinite CI rework loops
            assert ci_fb["rework_triggered"] is False

    # Manual traceability confirmation endpoint enforces `delivery:approve`
    with runtime.session_factory() as s:
        set_session_tenant(s, tenant_id)
        t_rec = s.get(Task, task_id)
        run_rec = s.get(TaskRun, t_rec.active_run_id)  # type: ignore[union-attr]
        assert run_rec is not None
        rep_manual = VerificationReportRecord(
            id="rep_manual_test",
            tenant_id=tenant_id,
            task_id=task_id,
            run_id=run_rec.id,
            repo_id=repo_id,
            patch_sha256="b" * 64,
            report_sha256="c" * 64,
            verdict="NEEDS_REVIEW",
            payload_json={"verdict": "NEEDS_REVIEW", "checks": [], "traceability": [{"ac_id": "AC-9", "status": "pending_manual"}]},
        )
        s.add(rep_manual)
        s.flush()
        s.add(
            TraceabilityEntryRecord(
                id="trc_manual_test",
                tenant_id=tenant_id,
                report_id=rep_manual.id,
                ac_id="AC-9",
                status="pending_manual",
                linked_test_cases_json=[],
                observed_test_ids_json=[],
            )
        )
        s.commit()

    assert (
        client.post("/api/v1/verification-reports/rep_manual_test/traceability/AC-9/confirm", headers=dev_h).status_code
        == 403
    )
    ok_confirm = client.post("/api/v1/verification-reports/rep_manual_test/traceability/AC-9/confirm", headers=h)
    assert ok_confirm.status_code == 200 and ok_confirm.json()["verdict"] == "PASS"
