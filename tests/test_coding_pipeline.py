"""P8 integration tests: ExecutionPlan DAG, chronological Red->Green TDD, repair loops, SpecChangeRequest & branch conflict."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from ai_sdlc_saas.api.app import create_app
from ai_sdlc_saas.config import Settings
from ai_sdlc_saas.db.engine import set_session_tenant
from ai_sdlc_saas.db.models import (
    Approval,
    ArtifactVersion,
    PullRequestRecord,
    Repository,
    SandboxResource,
    SpecChangeRequestRecord,
    SubtaskRecord,
    TaskRun,
    VerificationReportRecord,
)
from ai_sdlc_saas.gateway.adapters import DeterministicMockAdapter, GatewayResponse
from ai_sdlc_saas.orchestrator.executors import build_production_executors
from ai_sdlc_saas.orchestrator.worker import drain_ready_jobs


def _setup(tmp_path: Path) -> tuple[TestClient, Settings]:
    settings = Settings(
        env="test",
        database_url=f"sqlite:///{tmp_path / 'p8.db'}",
        object_store_dir=str(tmp_path / "objects"),
        worktree_root=str(tmp_path / "worktrees"),
        jwt_secret="test-jwt-secret-key-min-32-chars!!",
        encryption_key="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
        github_webhook_secret="gh-sec",
        stripe_webhook_secret="st-sec",
        allow_local_git_repos=True,
    )
    return TestClient(create_app(settings)), settings


def _bootstrap_org_with_repos(client: TestClient, email: str = "lead@p8.dev") -> tuple[str, dict[str, str], str, str, str]:
    reg = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "Password123!", "full_name": "P8 Lead", "org_name": "P8 Corp"},
    ).json()
    tok = reg["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    proj_id = client.post("/api/v1/projects", headers=h, json={"name": "P8 Service"}).json()["id"]
    py_repo_id = client.post(
        "/api/v1/repos/from-template",
        headers=h,
        json={"project_id": proj_id, "template_id": "python-fastapi-starter", "repo_name": "py-backend", "run_baseline": False},
    ).json()["id"]
    ts_repo_id = client.post(
        "/api/v1/repos/from-template",
        headers=h,
        json={"project_id": proj_id, "template_id": "node-ts-vite-starter", "repo_name": "ts-frontend", "run_baseline": False},
    ).json()["id"]
    return reg["active_org"]["id"], h, proj_id, py_repo_id, ts_repo_id


def _approve_current_gate(client: TestClient, h: dict[str, str], task_id: str, gate_type: str) -> None:
    detail = client.get(f"/api/v1/tasks/{task_id}", headers=h).json()
    run = detail["run"]
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


def _make_coder_adapter(mode: str = "pass_first") -> DeterministicMockAdapter:
    calls = {"coder": 0}

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
            payload = {"verdict": "APPROVE", "findings": [], "summary": "Clean subtraction feature."}
        elif "Coding Agent inside a sandboxed worktree" in joined:
            calls["coder"] += 1
            if mode == "spec_change":
                payload = {
                    "spec_change_request": {
                        "reason": "Database schema lacks multi-currency table required by AC-1.",
                        "proposed_changes": ["Add currency column to billing table or scope AC-1 to USD only."],
                        "blocking_ac_ids": ["AC-1"],
                    }
                }
            elif mode == "fail_then_fix" and calls["coder"] == 1:
                payload = {
                    "test_files": {
                        "tests/test_subtract.py": (
                            "from app.main import subtract_numbers\n\n\n"
                            "def test_subtract_numbers() -> None:\n"
                            "    assert subtract_numbers(10, 3) == 7\n"
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
                            "    return a + b  # intentional bug on attempt 1\n"
                        )
                    },
                }
            elif mode == "always_fail":
                payload = {
                    "test_files": {
                        "tests/test_subtract.py": (
                            "from app.main import subtract_numbers\n\n\n"
                            "def test_subtract_numbers() -> None:\n"
                            "    assert subtract_numbers(10, 3) == 7\n"
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
                            "    return 0\n"
                        )
                    },
                }
            elif mode == "multi_repo":
                if "Repo profile: node-ts" in joined:
                    payload = {
                        "test_files": {
                            "tests/multiply.test.ts": (
                                "import { describe, expect, it } from 'vitest';\n"
                                "import { multiplyNumbers } from '../src/math';\n"
                                "describe('multiply', () => { it('works', () => expect(multiplyNumbers(3, 4)).toBe(12)); });\n"
                            )
                        },
                        "code_files": {
                            "src/math.ts": (
                                "export function addNumbers(a: number, b: number): number {\n  return a + b;\n}\n"
                                "export function multiplyNumbers(a: number, b: number): number {\n  return a * b;\n}\n"
                            )
                        },
                    }
                else:
                    payload = {
                        "test_files": {
                            "tests/test_subtract.py": (
                                "from app.main import subtract_numbers\n\n\n"
                                "def test_subtract_numbers() -> None:\n"
                                "    assert subtract_numbers(10, 3) == 7\n"
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
                payload = {
                    "test_files": {
                        "tests/test_subtract.py": (
                            "from app.main import subtract_numbers\n\n\n"
                            "def test_subtract_numbers() -> None:\n"
                            "    assert subtract_numbers(10, 3) == 7\n"
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

        text = f"[CONTENT]{json.dumps(payload)}[/CONTENT]"
        return GatewayResponse(
            content=text,
            actual_model_id=model_id,
            input_tokens=40,
            output_tokens=60,
            cached_tokens=0,
            usage_status="CONFIRMED",
        )

    return DeterministicMockAdapter(handler=_handler)


def test_single_repo_tdd_chronological_and_real_git_pr(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    tenant_id, h, proj_id, py_repo_id, _ts = _bootstrap_org_with_repos(client)
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    runtime.extra["override_adapters"] = {"default": _make_coder_adapter("pass_first")}
    ex = build_production_executors()

    created = client.post(
        "/api/v1/tasks",
        headers=h,
        json={
            "project_id": proj_id,
            "title": "Add subtract_numbers helper",
            "requirement_text": "Implement subtract_numbers in src/app/main.py returning a - b with unit tests.",
            "task_type": "feature",
            "repo_ids": [py_repo_id],
        },
    ).json()
    task_id = created["id"]

    # Reach spec gate
    drain_ready_jobs(runtime, executors=ex, override_adapters=runtime.extra["override_adapters"])
    _approve_current_gate(client, h, task_id, "spec")
    drain_ready_jobs(runtime, executors=ex, override_adapters=runtime.extra["override_adapters"])
    _approve_current_gate(client, h, task_id, "plan")
    drain_ready_jobs(runtime, executors=ex, override_adapters=runtime.extra["override_adapters"])

    # Verify chronological Red -> Green TDD evidence & clean verifier PASS before delivery gate
    with runtime.session_factory() as s:
        set_session_tenant(s, tenant_id)
        patch_art = s.scalar(
            select(ArtifactVersion)
            .where(ArtifactVersion.tenant_id == tenant_id, ArtifactVersion.task_id == task_id, ArtifactVersion.artifact_type == "Patch")
            .order_by(ArtifactVersion.version.desc())
        )
        assert patch_art is not None
        patch_data = runtime.object_store.get_json(tenant_id, patch_art.object_key)
        repo_tdd = patch_data["tdd_evidence"][py_repo_id]
        assert repo_tdd["red_phase"]["passed_expected_fail"] is True
        assert repo_tdd["red_phase"]["exit_code"] != 0
        assert repo_tdd["green_phase"]["passed"] is True
        assert datetime.fromisoformat(repo_tdd["red_phase"]["finished_at"]) <= datetime.fromisoformat(repo_tdd["green_phase"]["started_at"])

        ver = s.scalar(
            select(VerificationReportRecord).where(VerificationReportRecord.tenant_id == tenant_id, VerificationReportRecord.task_id == task_id)
        )
        assert ver is not None and ver.verdict == "PASS"

    # Approve delivery -> publish real branch to local bare repo
    _approve_current_gate(client, h, task_id, "delivery")
    drain_ready_jobs(runtime, executors=ex, override_adapters=runtime.extra["override_adapters"])

    final_detail = client.get(f"/api/v1/tasks/{task_id}", headers=h).json()
    assert final_detail["status"] == "DELIVERED"

    with runtime.session_factory() as s:
        set_session_tenant(s, tenant_id)
        pr = s.scalar(select(PullRequestRecord).where(PullRequestRecord.tenant_id == tenant_id, PullRequestRecord.task_id == task_id))
        assert pr is not None and pr.status == "open"
        repo = s.get(Repository, py_repo_id)
        assert repo is not None
        ls_proc = subprocess.run(["git", "ls-remote", repo.clone_url_or_path, f"refs/heads/{pr.branch_name}"], capture_output=True, text=True)
        assert pr.branch_name in ls_proc.stdout
        # Confirm sandbox worktree cleaned
        active_res = list(
            s.scalars(
                select(SandboxResource).where(
                    SandboxResource.tenant_id == tenant_id, SandboxResource.task_id == task_id, SandboxResource.status != "deleted"
                )
            )
        )
        assert active_res == []


def test_bounded_repair_loop_recovers_then_passes(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    tenant_id, h, proj_id, py_repo_id, _ts = _bootstrap_org_with_repos(client, email="repair@p8.dev")
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    adapters = {"default": _make_coder_adapter("fail_then_fix")}
    ex = build_production_executors()

    task_id = client.post(
        "/api/v1/tasks",
        headers=h,
        json={
            "project_id": proj_id,
            "title": "Repair loop subtract fix",
            "requirement_text": "Implement subtract_numbers in src/app/main.py returning a - b cleanly.",
            "task_type": "feature",
            "repo_ids": [py_repo_id],
        },
    ).json()["id"]

    drain_ready_jobs(runtime, executors=ex, override_adapters=adapters)
    _approve_current_gate(client, h, task_id, "spec")
    drain_ready_jobs(runtime, executors=ex, override_adapters=adapters)
    _approve_current_gate(client, h, task_id, "plan")
    drain_ready_jobs(runtime, executors=ex, override_adapters=adapters)

    with runtime.session_factory() as s:
        set_session_tenant(s, tenant_id)
        reports = list(
            s.scalars(
                select(VerificationReportRecord)
                .where(VerificationReportRecord.tenant_id == tenant_id, VerificationReportRecord.task_id == task_id)
                .order_by(VerificationReportRecord.created_at.asc())
            )
        )
        assert [r.verdict for r in reports] == ["FAIL", "PASS"]


def test_exhausted_repair_rounds_fails_and_cleans_up(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    tenant_id, h, proj_id, py_repo_id, _ts = _bootstrap_org_with_repos(client, email="exhaust@p8.dev")
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    adapters = {"default": _make_coder_adapter("always_fail")}
    ex = build_production_executors()

    task_id = client.post(
        "/api/v1/tasks",
        headers=h,
        json={
            "project_id": proj_id,
            "title": "Broken implementation exhausts rounds",
            "requirement_text": "Implement subtract_numbers in src/app/main.py returning a - b with unit tests.",
            "task_type": "feature",
            "repo_ids": [py_repo_id],
        },
    ).json()["id"]

    drain_ready_jobs(runtime, executors=ex, override_adapters=adapters)
    _approve_current_gate(client, h, task_id, "spec")
    drain_ready_jobs(runtime, executors=ex, override_adapters=adapters)
    _approve_current_gate(client, h, task_id, "plan")
    drain_ready_jobs(runtime, executors=ex, override_adapters=adapters)

    detail = client.get(f"/api/v1/tasks/{task_id}", headers=h).json()
    assert detail["status"] == "FAILED"
    assert detail["cleanup_status"] == "CLEANED"


def test_multi_repo_dag_coordinated_changeset(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    tenant_id, h, proj_id, py_repo_id, ts_repo_id = _bootstrap_org_with_repos(client, email="multi@p8.dev")
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    adapters = {"default": _make_coder_adapter("multi_repo")}
    ex = build_production_executors()

    task_id = client.post(
        "/api/v1/tasks",
        headers=h,
        json={
            "project_id": proj_id,
            "title": "Multi-repo math sync",
            "requirement_text": "Update Python src/app/main.py subtract_numbers and TypeScript src/math.ts multiplyNumbers.",
            "task_type": "feature",
            "repo_ids": [ts_repo_id, py_repo_id],  # intentionally reversed; planner orders python before node!
        },
    ).json()["id"]

    drain_ready_jobs(runtime, executors=ex, override_adapters=adapters)
    _approve_current_gate(client, h, task_id, "spec")
    drain_ready_jobs(runtime, executors=ex, override_adapters=adapters)

    with runtime.session_factory() as s:
        set_session_tenant(s, tenant_id)
        subs = list(
            s.scalars(select(SubtaskRecord).where(SubtaskRecord.tenant_id == tenant_id).order_by(SubtaskRecord.id.asc()))
        )
        assert len(subs) == 2
        assert subs[0].repo_id == py_repo_id and subs[0].depends_on_json == []
        assert subs[1].repo_id == ts_repo_id and subs[1].depends_on_json == ["sub-1"]

    _approve_current_gate(client, h, task_id, "plan")
    drain_ready_jobs(runtime, executors=ex, override_adapters=adapters)
    _approve_current_gate(client, h, task_id, "delivery")
    drain_ready_jobs(runtime, executors=ex, override_adapters=adapters)

    with runtime.session_factory() as s:
        set_session_tenant(s, tenant_id)
        prs = list(s.scalars(select(PullRequestRecord).where(PullRequestRecord.tenant_id == tenant_id, PullRequestRecord.task_id == task_id)))
        assert len(prs) == 2
        assert len({p.change_set_id for p in prs}) == 1


def test_spec_change_request_invalidates_approvals_and_pauses(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    tenant_id, h, proj_id, py_repo_id, _ts = _bootstrap_org_with_repos(client, email="scr@p8.dev")
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    adapters = {"default": _make_coder_adapter("spec_change")}
    ex = build_production_executors()

    task_id = client.post(
        "/api/v1/tasks",
        headers=h,
        json={
            "project_id": proj_id,
            "title": "Impossible currency change",
            "requirement_text": "Implement multi-currency conversion on src/app/main.py.",
            "task_type": "feature",
            "repo_ids": [py_repo_id],
        },
    ).json()["id"]

    drain_ready_jobs(runtime, executors=ex, override_adapters=adapters)
    _approve_current_gate(client, h, task_id, "spec")
    drain_ready_jobs(runtime, executors=ex, override_adapters=adapters)
    _approve_current_gate(client, h, task_id, "plan")
    drain_ready_jobs(runtime, executors=ex, override_adapters=adapters)

    detail = client.get(f"/api/v1/tasks/{task_id}", headers=h).json()
    assert detail["status"] == "SPEC_CHANGE_REQUIRED"

    with runtime.session_factory() as s:
        set_session_tenant(s, tenant_id)
        scrs = list(s.scalars(select(SpecChangeRequestRecord).where(SpecChangeRequestRecord.task_id == task_id)))
        assert len(scrs) == 1 and scrs[0].status == "open"
        valid_aprs = list(s.scalars(select(Approval).where(Approval.task_id == task_id, Approval.is_valid.is_(True))))
        assert valid_aprs == []


def test_branch_conflict_guard_blocks_overlapping_target_branch(tmp_path: Path) -> None:
    client, _ = _setup(tmp_path)
    tenant_id, h, proj_id, py_repo_id, _ts = _bootstrap_org_with_repos(client, email="conflict@p8.dev")
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    adapters = {"default": _make_coder_adapter("pass_first")}
    ex = build_production_executors()

    t1 = client.post(
        "/api/v1/tasks",
        headers=h,
        json={
            "project_id": proj_id,
            "title": "Task one on shared branch",
            "requirement_text": "Implement subtract_numbers in src/app/main.py returning a - b cleanly.",
            "task_type": "feature",
            "repo_ids": [py_repo_id],
        },
    ).json()["id"]
    t2 = client.post(
        "/api/v1/tasks",
        headers=h,
        json={
            "project_id": proj_id,
            "title": "Task two on same shared branch",
            "requirement_text": "Also update subtract_numbers in src/app/main.py with input checks.",
            "task_type": "feature",
            "repo_ids": [py_repo_id],
        },
    ).json()["id"]

    with runtime.session_factory() as s:
        set_session_tenant(s, tenant_id)
        for tid in (t1, t2):
            run = s.scalar(select(TaskRun).where(TaskRun.task_id == tid))
            assert run is not None
            run.frozen_policy_json = {"target_branch": "ai-sdlc/shared-feature"}
        s.commit()

    # Drive task 1 to plan approval so it holds subtask lock on ai-sdlc/shared-feature
    drain_ready_jobs(runtime, max_jobs=1, executors=ex, override_adapters=adapters)
    drain_ready_jobs(runtime, max_jobs=1, executors=ex, override_adapters=adapters)  # task 2 pauses at spec gate
    _approve_current_gate(client, h, t1, "spec")
    drain_ready_jobs(runtime, max_jobs=1, executors=ex, override_adapters=adapters)  # t1 runs execution_plan -> locks branch

    _approve_current_gate(client, h, t2, "spec")
    res = drain_ready_jobs(runtime, max_jobs=1, executors=ex, override_adapters=adapters)
    assert any("already holds target branch" in str(item.get("error", "")) for item in res)
    assert client.get(f"/api/v1/tasks/{t1}", headers=h).json()["status"] == "WAITING_APPROVAL"
