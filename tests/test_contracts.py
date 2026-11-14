"""Offline unit & integration tests for shared AI SDLC contracts and RepoContext."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_REPO_SRC = _REPO_ROOT / "src"
if str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))

from ai_sdlc.contracts import (  # noqa: E402
    AcceptanceCriterion,
    CritiqueIssue,
    ExecutionReport,
    RepoContext,
    SpecCritique,
    TaskSpec,
    TestCaseSpec,
    TestPlan,
    approve_task_spec,
    build_execution_report_from_phase2,
    format_task_spec_for_review,
    reject_task_spec,
    revise_task_spec,
    validate_execution_report_binding,
    validate_task_spec_against_repo,
    validate_test_plan_against_spec,
)
from ai_sdlc.model_routing import resolve_role_router_config  # noqa: E402
from ai_sdlc.repo_context import build_repo_context  # noqa: E402

_MINI_BUG_REPO = _REPO_ROOT / "spikes" / "01_instabox_openswe" / "fixtures" / "mini_bug_repo"


def _make_sample_spec(repo_ctx: RepoContext, *, spec_version: int = 1) -> TaskSpec:
    return TaskSpec.create(
        repo_context=repo_ctx,
        user_requirement="Fix apply_discount_cents bounds > 100 and half-up rounding.",
        task_type="bugfix",
        title="Fix discount bounds and half-up rounding in billing",
        summary="Enforce 0..100 discount_percent validation and half-up cent rounding in apply_discount_cents.",
        problem_statement=(
            "In src/mini_ledger/billing.py, apply_discount_cents currently allows discount_percent > 100 "
            "and truncates discount instead of applying (subtotal_cents * discount_percent + 50) // 100."
        ),
        scope_in=("Validate discount_percent <= 100", "Round half-cent discounts half-up"),
        scope_out=("Do not alter compute_subtotal_cents", "Do not modify existing tests"),
        target_files=("src/mini_ledger/billing.py",),
        expected_new_test_files=("tests/test_billing_discount_regressions.py",),
        acceptance_criteria=(
            AcceptanceCriterion(
                ac_id="AC-1",
                description="Reject discount_percent > 100 with ValueError.",
                expected_behavior="apply_discount_cents(1000, 101) raises ValueError.",
                target_files=("src/mini_ledger/billing.py",),
                edge_cases=("discount_percent == 100 passes", "discount_percent == 101 raises"),
            ),
            AcceptanceCriterion(
                ac_id="AC-2",
                description="Round half-cent discount remainders half-up.",
                expected_behavior="(subtotal_cents * discount_percent + 50) // 100 is subtracted.",
                target_files=("src/mini_ledger/billing.py",),
                edge_cases=("apply_discount_cents(1, 50) == 0", "apply_discount_cents(105, 10) == 94"),
            ),
        ),
        constraints=("Do not modify tests/test_billing.py or pyproject.toml.",),
        author_agent="metagpt:ProductManager",
        model_id="9router:ag/gemini-3.8-flash",
        spec_id="spec-billing-fix",
        spec_version=spec_version,
    )


def test_build_repo_context_on_mini_bug_repo() -> None:
    ctx = build_repo_context(_MINI_BUG_REPO, repo_source="fixtures/mini_bug_repo")
    assert ctx.repo_name == "mini-ledger"
    assert ctx.commit_source == "tree_snapshot"
    assert ctx.repo_commit_sha.startswith("tree-")
    assert len(ctx.repo_tree_sha256) == 64
    assert ctx.tracked_files == (
        ".gitignore",
        "pyproject.toml",
        "src/mini_ledger/__init__.py",
        "src/mini_ledger/billing.py",
        "tests/test_billing.py",
    )
    assert ctx.existing_test_files == ("tests/test_billing.py",)
    assert ctx.protected_config_paths == ("pyproject.toml",)
    assert "pyproject.toml" in ctx.protected_config_hashes
    assert ctx.test_runner.framework == "pytest"
    assert ctx.test_runner.pythonpath == ("src",)
    assert ctx.test_runner.testpaths == ("tests",)

    symbols = ctx.symbol_names_by_file()
    assert symbols["src/mini_ledger/billing.py"] == ("compute_subtotal_cents", "apply_discount_cents")
    assert "tests/test_billing.py" in symbols

    prompt = ctx.to_prompt_context()
    assert "apply_discount_cents" in prompt
    assert "tests/test_billing.py" in prompt

    restored = RepoContext.from_json(ctx.to_json())
    assert restored == ctx


def test_build_repo_context_detects_git_head_and_blocks_secrets(tmp_path: Path) -> None:
    repo = tmp_path / "git_repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src" / "app.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")
    (repo / "tests" / "test_app.py").write_text("def test_add() -> None:\n    assert True\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".env\n*.local.json\n", encoding="utf-8")
    (repo / ".env").write_text("SECRET=should_not_appear\n", encoding="utf-8")
    (repo / "models.local.json").write_text('{"ba":"secret"}\n', encoding="utf-8")

    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test Bot"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", ".gitignore", "src/app.py", "tests/test_app.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
    head_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()

    ctx = build_repo_context(repo)
    assert ctx.commit_source == "git_head"
    assert ctx.repo_commit_sha == head_sha
    assert ".env" not in ctx.tracked_files
    assert "models.local.json" not in ctx.tracked_files
    assert "SECRET=should_not_appear" not in ctx.to_prompt_context()
    assert ctx.is_dirty is False

    # Dirty change should flip is_dirty=True
    (repo / "src" / "app.py").write_text("def add(a: int, b: int) -> int:\n    return a + b + 1\n", encoding="utf-8")
    dirty_ctx = build_repo_context(repo)
    assert dirty_ctx.is_dirty is True
    assert dirty_ctx.repo_tree_sha256 != ctx.repo_tree_sha256


def test_task_spec_validation_approval_revision_and_tamper_detection() -> None:
    ctx = build_repo_context(_MINI_BUG_REPO)
    spec = _make_sample_spec(ctx)

    assert validate_task_spec_against_repo(spec, ctx) == []
    assert spec.is_approved is False
    assert "tests/test_billing.py" in spec.forbidden_files
    assert "pyproject.toml" in spec.forbidden_files

    approved = approve_task_spec(spec, reviewer="alice", comment="LGTM")
    assert approved.is_approved is True
    assert approved.spec_sha256 == spec.spec_sha256
    assert TaskSpec.from_json(approved.to_json()) == approved
    assert "## Acceptance Criteria" in format_task_spec_for_review(approved)

    # Tampering with serialized TaskSpec fails integrity check immediately.
    tampered = approved.to_dict()
    tampered["title"] = "Tampered title"
    with pytest.raises(ValueError, match="integrity mismatch"):
        TaskSpec.from_dict(tampered)

    # Reject + revise increments spec_version while keeping spec_id.
    rejected = reject_task_spec(spec, reviewer="alice", comment="Add negative qty note")
    assert rejected.status == "rejected"
    assert rejected.is_approved is False
    with pytest.raises(ValueError, match="Cannot approve"):
        approve_task_spec(rejected, reviewer="alice")

    v2 = revise_task_spec(rejected, repo_context=ctx, summary="Updated summary in v2.")
    assert v2.spec_id == spec.spec_id
    assert v2.spec_version == 2
    assert v2.status == "draft"
    assert v2.approval is None
    assert v2.spec_sha256 != spec.spec_sha256


def test_task_spec_rejects_ungrounded_or_forbidden_targets() -> None:
    ctx = build_repo_context(_MINI_BUG_REPO)
    bad_spec = TaskSpec.create(
        repo_context=ctx,
        user_requirement="Fix billing",
        task_type="bugfix",
        title="Bad spec",
        summary="Bad targets",
        problem_statement="Targets missing file and existing test.",
        scope_in=("Fix",),
        scope_out=(),
        target_files=("src/nonexistent.py", "tests/test_billing.py", "pyproject.toml"),
        expected_new_test_files=("tests/test_billing.py", "src/helper.py"),
        acceptance_criteria=(
            AcceptanceCriterion(
                ac_id="AC-1",
                description="Do something",
                expected_behavior="Works",
                target_files=("tests/test_billing.py",),
            ),
        ),
        model_id="9router:ag/gemini-3.8-flash",
    )
    errors = validate_task_spec_against_repo(bad_spec, ctx)
    joined = "\n".join(errors)
    assert "nonexistent.py" in joined
    assert "existing test file" in joined
    assert "protected config file" in joined
    assert "collide with existing repo file" in joined
    assert "helper.py" in joined


def test_test_plan_and_execution_report_binding_and_traceability() -> None:
    ctx = build_repo_context(_MINI_BUG_REPO)
    spec = approve_task_spec(_make_sample_spec(ctx), reviewer="user")

    plan = TestPlan.create(
        task_spec=spec,
        model_id="9router:glm/glm-5.3-flash",
        spec_critique=SpecCritique(
            verdict="accept",
            summary="Acceptance criteria are clear and testable.",
            issues=(
                CritiqueIssue(
                    severity="suggestion",
                    ac_id="AC-1",
                    message="Also verify bool is rejected.",
                    suggestion="Add True as discount_percent case.",
                ),
            ),
        ),
        test_cases=(
            TestCaseSpec(
                test_case_id="TC-1",
                ac_ids=("AC-1",),
                title="Reject discount_percent > 100",
                test_type="negative",
                proposed_test_file="tests/test_billing_discount_regressions.py",
                target_symbol="apply_discount_cents",
                steps_or_inputs="Call apply_discount_cents(1000, 101)",
                expected_outcome="Raises ValueError",
            ),
            TestCaseSpec(
                test_case_id="TC-2",
                ac_ids=("AC-2",),
                title="Round half-cent discounts half-up",
                test_type="regression",
                proposed_test_file="tests/test_billing_discount_regressions.py",
                target_symbol="apply_discount_cents",
                steps_or_inputs="Call apply_discount_cents(1, 50) and (105, 10)",
                expected_outcome="Returns 0 and 94 respectively",
            ),
        ),
    )
    assert validate_test_plan_against_spec(plan, spec, ctx) == []
    assert TestPlan.from_json(plan.to_json()) == plan

    # Stale v1 plan against revised v2 spec must be caught.
    v2_spec = approve_task_spec(revise_task_spec(spec, repo_context=ctx, summary="v2"), reviewer="user")
    stale_errors = validate_test_plan_against_spec(plan, v2_spec, ctx)
    assert any("spec_version mismatch" in err for err in stale_errors)
    assert any("spec_sha256 mismatch" in err for err in stale_errors)

    saved_report = json.loads(
        (
            _REPO_ROOT
            / "spikes"
            / "01_instabox_openswe"
            / "artifacts"
            / "phase2_run"
            / "verification_report.json"
        ).read_text(encoding="utf-8")
    )
    patch_bytes = (
        _REPO_ROOT / "spikes" / "01_instabox_openswe" / "artifacts" / "phase2_run" / "patch.diff"
    ).read_bytes()

    exec_report = build_execution_report_from_phase2(
        run_id=saved_report["run_id"],
        spec=spec,
        test_plan=plan,
        dev_model_id="9router:cx/gpt-6.1-sol",
        agent_machine_id=saved_report["agent_machine_id"],
        verify_red_machine_id=saved_report["verify_red_machine_id"],
        verify_machine_id=saved_report["verify_machine_id"],
        pre_agent_snapshot_id=saved_report["pre_agent_snapshot_id"],
        vm_base_commit_sha=saved_report["base_commit_sha"],
        post_agent_head_sha=saved_report["post_agent_head_sha"],
        verification=saved_report["verification"],
        patch_bytes=patch_bytes,
        changed_files=("src/mini_ledger/billing.py", "tests/test_billing_discount_regressions.py"),
        patch_file="patch.diff",
    )
    assert validate_execution_report_binding(exec_report, spec, plan) == []
    assert ExecutionReport.from_json(exec_report.to_json()) == exec_report
    assert [row.ac_id for row in exec_report.ac_traceability] == ["AC-1", "AC-2"]
    assert all(row.status == "verified" for row in exec_report.ac_traceability)
    assert exec_report.verdict == "PASS"


def test_resolve_role_router_config_for_ba() -> None:
    env = {
        "NINE_ROUTER_API_KEY": "router-secret",
        "NINE_ROUTER_BASE_URL": "http://127.0.0.1:20128/v1/",
        "NINE_ROUTER_ROLE_MODELS": json.dumps({"ba": "ag/gemini-3.8-flash", "dev": "cx/gpt-6.1-sol"}),
    }
    cfg = resolve_role_router_config(role="ba", env=env)
    assert cfg.base_url == "http://127.0.0.1:20128/v1"
    assert cfg.model_id == "ag/gemini-3.8-flash"
    assert cfg.api_key == "router-secret"
    assert "router-secret" not in repr(cfg)
