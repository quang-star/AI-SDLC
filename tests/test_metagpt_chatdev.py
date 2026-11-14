"""P6 & P7 tests: MetaGPT BA clarification/evals/spec-diff, ChatDev YAML read-only review & memory."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_sdlc.contracts import RepoContext
from ai_sdlc_saas.adapters.chatdev_service import (
    chatdev_code_review_stage,
    chatdev_qa_critique_stage,
    load_and_validate_workflow,
)
from ai_sdlc_saas.adapters.metagpt_service import (
    analyze_requirement_gaps,
    metagpt_architect_stage,
    metagpt_ba_stage,
)
from ai_sdlc_saas.api.app import create_app
from ai_sdlc_saas.config import Settings
from ai_sdlc_saas.errors import ForbiddenError
from ai_sdlc_saas.orchestrator.graph import StageExecutors
from ai_sdlc_saas.orchestrator.worker import drain_ready_jobs


def _setup(tmp_path: Path) -> tuple[TestClient, Settings]:
    settings = Settings(
        env="test",
        database_url=f"sqlite:///{tmp_path / 'p6p7.db'}",
        object_store_dir=str(tmp_path / "objects"),
        worktree_root=str(tmp_path / "worktrees"),
        jwt_secret="test-jwt-secret-key-min-32-chars!!",
        encryption_key="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
        github_webhook_secret="gh-sec",
        stripe_webhook_secret="st-sec",
        allow_local_git_repos=True,
    )
    return TestClient(create_app(settings)), settings


def _bootstrap(client: TestClient, email: str, org: str) -> tuple[str, str, str]:
    reg = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "Password123!", "full_name": "Lead", "org_name": org},
    ).json()
    tok = reg["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    proj_id = client.post("/api/v1/projects", headers=h, json={"name": "Service App"}).json()["id"]
    repo_id = client.post(
        "/api/v1/repos/from-template",
        headers=h,
        json={"project_id": proj_id, "template_id": "python-fastapi-starter", "repo_name": "svc", "run_baseline": False},
    ).json()["id"]
    return tok, proj_id, repo_id


def test_ba_eval_cases_against_repo_context(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    tok, _proj, repo_id = _bootstrap(client, "ba@eval.com", "Eval Org")
    raw_ctx = client.get(f"/api/v1/repos/{repo_id}/context", headers={"Authorization": f"Bearer {tok}"}).json()["repo_context"]
    repo_ctx = RepoContext.from_dict(raw_ctx)

    eval_cases = json.loads(Path("evals/ba_eval_cases.json").read_text(encoding="utf-8"))
    for case in eval_cases:
        questions = analyze_requirement_gaps(case["requirement_text"], repo_ctx)
        if case["expect_clarifications"]:
            assert len(questions) >= 1, f"Expected clarifications for case {case['id']}"
        else:
            assert questions == [], f"Unexpected clarifications for grounded case {case['id']}: {questions}"


def test_clarification_spec_edit_invalidation_and_chatdev_qa_review(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    tok, proj_id, repo_id = _bootstrap(client, "lead@p67.com", "P67 Org")
    tok_b, proj_b, _ = _bootstrap(client, "other@b.com", "Org B")
    h = {"Authorization": f"Bearer {tok}"}
    h_b = {"Authorization": f"Bearer {tok_b}"}

    # Tenant A stores & approves a project lesson memory
    mem = client.post(
        f"/api/v1/projects/{proj_id}/memories",
        headers=h,
        json={"lesson_text": "Always keep add_numbers pure and side-effect free.", "auto_approve": True},
    )
    assert mem.status_code == 201
    mem_id = mem.json()["id"]

    # Tenant B cannot read or delete Tenant A memory
    assert client.get(f"/api/v1/projects/{proj_id}/memories", headers=h_b).json() == []
    assert client.delete(f"/api/v1/projects/{proj_id}/memories/{mem_id}", headers=h_b).status_code == 404
    assert client.get(f"/api/v1/projects/{proj_b}/memories", headers=h_b).json() == []

    executors = StageExecutors(
        ba_specify=metagpt_ba_stage,
        ba_refine=metagpt_ba_stage,
        architect_design=metagpt_architect_stage,
        qa_critique=chatdev_qa_critique_stage,
        code_review=chatdev_code_review_stage,
    )

    # Ambiguous prompt enters WAITING_INPUT
    task = client.post(
        "/api/v1/tasks",
        headers=h,
        json={
            "project_id": proj_id,
            "title": "Improve app behavior",
            "requirement_text": "Improve the app etc.",
            "task_type": "bugfix",
            "repo_ids": [repo_id],
        },
    ).json()
    tid = task["id"]
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    drain_ready_jobs(runtime, max_jobs=5, executors=executors)

    d1 = client.get(f"/api/v1/tasks/{tid}", headers=h).json()
    assert d1["status"] == "WAITING_INPUT"

    # Answer clarification -> produces Spec v2 + ArchitectDesign + ChatDev TestPlan -> WAITING_APPROVAL (spec)
    clar = client.post(
        f"/api/v1/tasks/{tid}/clarify",
        headers=h,
        json={"answers": {"q1": "Validate non-negative integer inputs in add_numbers in src/app/main.py"}},
    )
    assert clar.status_code == 200
    drain_ready_jobs(runtime, max_jobs=5, executors=executors)

    d2 = client.get(f"/api/v1/tasks/{tid}", headers=h).json()
    assert d2["status"] == "WAITING_APPROVAL"
    assert d2["run"]["spec_version"] == 2
    tp = client.get(f"/api/v1/tasks/{tid}/test-plan", headers=h)
    assert tp.status_code == 200
    assert tp.json()["verdict"] == "READY"
    assert len(tp.json()["test_plan"]["test_cases"]) >= 1

    # Approve Spec v2
    base_sha = next(iter(d2["run"]["base_commits"].values()))
    apr = client.post(
        f"/api/v1/tasks/{tid}/approvals",
        headers=h,
        json={
            "gate_type": "spec",
            "decision": "approved",
            "target_digest": d2["run"]["spec_sha256"],
            "base_commit_sha": base_sha,
            "policy_version": d2["run"]["policy_version"],
        },
    )
    assert apr.status_code == 201

    # Edit Spec after approval -> creates Spec v3 with diff & invalidates prior approval!
    edited = client.put(
        f"/api/v1/tasks/{tid}/spec",
        headers=h,
        json={"title": "Validate inputs cleanly in main.py"},
    )
    assert edited.status_code == 200
    assert edited.json()["spec_version"] == 3
    assert edited.json()["invalidated_approvals"] == 1
    assert "Validate inputs cleanly in main.py" in edited.json()["diff"]

    # Stale worker resume using invalidated approval cannot pass spec gate; re-approve v3 -> delivers
    drain_ready_jobs(runtime, max_jobs=5, executors=executors)
    d3 = client.get(f"/api/v1/tasks/{tid}", headers=h).json()
    assert d3["run"]["spec_version"] == 3
    apprs = client.get(f"/api/v1/tasks/{tid}/approvals", headers=h).json()
    assert apprs[0]["is_valid"] is False

    # Re-approve Spec v3 -> progresses cleanly through Plan -> Coding -> Review -> Delivery
    client.post(
        f"/api/v1/tasks/{tid}/approvals",
        headers=h,
        json={
            "gate_type": "spec",
            "decision": "approved",
            "target_digest": d3["run"]["spec_sha256"],
            "base_commit_sha": base_sha,
            "policy_version": d3["run"]["policy_version"],
        },
    )
    drain_ready_jobs(runtime, max_jobs=5, executors=executors)
    d4 = client.get(f"/api/v1/tasks/{tid}", headers=h).json()
    assert d4["current_stage"] == "plan_approval_gate"

    client.post(
        f"/api/v1/tasks/{tid}/approvals",
        headers=h,
        json={
            "gate_type": "plan",
            "decision": "approved",
            "target_digest": d4["run"]["plan_sha256"],
            "base_commit_sha": base_sha,
            "policy_version": d4["run"]["policy_version"],
        },
    )
    drain_ready_jobs(runtime, max_jobs=5, executors=executors)
    d5 = client.get(f"/api/v1/tasks/{tid}", headers=h).json()
    assert d5["current_stage"] == "delivery_approval_gate"
    art_types = [a["artifact_type"] for a in d5["artifacts"]]
    assert "ReviewReport" in art_types


def test_chatdev_yaml_forbids_write_tools_and_sidecar_apps(tmp_path: Path) -> None:
    bad_yaml = tmp_path / "bad_wf.yaml"
    bad_yaml.write_text(
        "name: evil\nmax_rounds: 2\nallowed_tools:\n  - read_repo_context\n  - shell\n",
        encoding="utf-8",
    )
    with pytest.raises(ForbiddenError):
        load_and_validate_workflow(bad_yaml)

    # Sidecar FastAPI apps work standalone
    from apps.chatdev_service.app import app as chatdev_app
    from apps.metagpt_service.app import app as metagpt_app

    c_mg = TestClient(metagpt_app)
    assert c_mg.get("/healthz").json()["service"] == "metagpt-ba"

    c_cd = TestClient(chatdev_app)
    assert c_cd.get("/healthz").json()["service"] == "chatdev-qa-review"
    wf_check = c_cd.post("/validate-workflow", json={"workflow_name": "code_review"})
    assert wf_check.status_code == 200
    assert wf_check.json()["valid"] is True
