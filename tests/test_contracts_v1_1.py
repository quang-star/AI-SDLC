"""Tests for P1 SaaS v1.1 contracts, digest determinism, schema migration, and TS/JS RepoContext."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_sdlc import (
    AcceptanceCriterion,
    ACTraceabilityEntry,
    ArtifactProvenance,
    CommandCheckResult,
    CritiqueIssue,
    ExecutionPlan,
    RepoLimitExceededError,
    ReviewFinding,
    ReviewReport,
    SpecChangeRequest,
    SpecCritique,
    SubtaskSpec,
    TaskSpec,
    TestCaseSpec,
    TestPlan,
    UnsupportedSchemaVersionError,
    VerificationReport,
    approve_execution_plan,
    approve_task_spec,
    build_repo_context,
    migrate_contract_payload,
)
from ai_sdlc.contracts import sha256_hex

_MINI_BUG_REPO = Path(__file__).resolve().parents[1] / "spikes" / "01_instabox_openswe" / "fixtures" / "mini_bug_repo"


def test_execution_plan_digest_determinism_and_tamper_detection() -> None:
    ctx = build_repo_context(_MINI_BUG_REPO)
    spec = approve_task_spec(
        TaskSpec.create(
            repo_context=ctx,
            user_requirement="Fix billing rounding",
            task_type="bugfix",
            title="Fix billing rounding",
            summary="Fix half-up rounding",
            problem_statement="Truncates half cents",
            scope_in=("Fix rounding",),
            scope_out=("Do not edit tests",),
            target_files=("src/mini_ledger/billing.py",),
            expected_new_test_files=("tests/test_rounding_fix.py",),
            acceptance_criteria=(
                AcceptanceCriterion(
                    ac_id="AC-1",
                    description="Round half-up",
                    expected_behavior="105 at 10% -> 94",
                    target_files=("src/mini_ledger/billing.py",),
                ),
            ),
            model_id="9router:ba",
            spec_id="spec-round",
            created_at="2026-11-13T00:00:00Z",
        ),
        reviewer="owner-1",
        decided_at="2026-11-13T00:01:00Z",
    )
    tplan = TestPlan.create(
        task_spec=spec,
        spec_critique=SpecCritique(verdict="accept", summary="Clear", issues=(CritiqueIssue(severity="suggestion", message="Ok"),)),
        test_cases=(
            TestCaseSpec(
                test_case_id="TC-1",
                ac_ids=("AC-1",),
                title="Half-up check",
                test_type="regression",
                proposed_test_file="tests/test_rounding_fix.py",
                steps_or_inputs="apply_discount_cents(105, 10)",
                expected_outcome="94",
            ),
        ),
        model_id="9router:qa",
        plan_id="tplan-1",
        created_at="2026-11-13T00:02:00Z",
    )
    prov = ArtifactProvenance(
        tenant_id="ten_a",
        project_id="proj_1",
        task_id="task_1",
        run_id="run_1",
        attempt_id="att_1",
    )
    sub = SubtaskSpec(
        subtask_id="sub-1",
        repo_id="repo_1",
        repo_name="mini-ledger",
        base_commit_sha=ctx.repo_commit_sha,
        target_branch="ai/task-1",
        title="Patch billing.py",
        description="Apply half-up rounding fix",
        target_files=("src/mini_ledger/billing.py",),
        expected_new_test_files=("tests/test_rounding_fix.py",),
        ac_ids=("AC-1",),
    )
    p1 = ExecutionPlan.create(
        spec=spec,
        test_plan=tplan,
        model_policy_digest=sha256_hex(b"policy-v1"),
        estimated_cost_cents=45,
        subtasks=(sub,),
        provenance=prov,
        plan_id="plan-1",
        created_at="2026-11-13T00:03:00Z",
    )
    p2 = ExecutionPlan.create(
        spec=spec,
        test_plan=tplan,
        model_policy_digest=sha256_hex(b"policy-v1"),
        estimated_cost_cents=45,
        subtasks=(sub,),
        provenance=prov,
        plan_id="plan-1",
        created_at="2026-11-13T00:03:00Z",
    )
    assert p1.plan_sha256 == p2.plan_sha256

    approved = approve_execution_plan(p1, reviewer="reviewer-1")
    assert approved.status == "approved"

    tampered = approved.to_dict()
    tampered["estimated_cost_cents"] = 9999
    with pytest.raises(ValueError, match="integrity mismatch"):
        ExecutionPlan.from_dict(tampered)


def test_schema_migration_and_version_rejection() -> None:
    ctx = build_repo_context(_MINI_BUG_REPO)
    prov = ArtifactProvenance(
        tenant_id="ten_a",
        project_id="proj_1",
        task_id="task_1",
        run_id="run_1",
        attempt_id="att_1",
    )
    migrated = migrate_contract_payload("RepoContext", ctx.to_dict(), provenance=prov)
    assert migrated["schema_version"] == "1.1"
    assert migrated["source_schema_version"] == "1.0"
    assert migrated["provenance"]["tenant_id"] == "ten_a"

    bad = ctx.to_dict()
    bad["schema_version"] = "99.0"
    with pytest.raises(UnsupportedSchemaVersionError):
        migrate_contract_payload("RepoContext", bad)


def test_verification_review_and_spec_change_contracts() -> None:
    vrep = VerificationReport.create(
        report_id="ver-1",
        repo_id="repo_1",
        repo_name="mini-ledger",
        profile_id="python-pytest",
        base_commit_sha="abc123",
        spec_id="spec-1",
        spec_version=1,
        spec_sha256=sha256_hex(b"spec"),
        plan_id="plan-1",
        plan_sha256=sha256_hex(b"plan"),
        patch_sha256=sha256_hex(b"patch"),
        reapplied_tree_sha256=sha256_hex(b"tree"),
        verdict="PASS",
        changed_files=("src/app.py",),
        new_test_files=("tests/test_app.py",),
        red_to_green_ids=("tests.test_app::test_fix",),
        checks=(CommandCheckResult(name="pytest", command="pytest -q", exit_code=0, duration_ms=120, passed=True),),
        ac_traceability=(
            ACTraceabilityEntry(
                ac_id="AC-1",
                linked_test_case_ids=("TC-1",),
                observed_test_ids=("tests.test_app::test_fix",),
                status="verified",
            ),
        ),
    )
    assert VerificationReport.from_dict(vrep.to_dict()) == vrep
    tampered_v = vrep.to_dict()
    tampered_v["verdict"] = "FAIL"
    with pytest.raises(ValueError, match="integrity mismatch"):
        VerificationReport.from_dict(tampered_v)

    rrep = ReviewReport(
        review_id="rev-1",
        created_at="2026-11-13T00:05:00Z",
        reviewer_agent="chatdev:CodeReviewer",
        reviewer_model_id="9router:reviewer",
        spec_sha256=sha256_hex(b"spec"),
        patch_sha256=sha256_hex(b"patch"),
        verification_report_sha256=vrep.report_sha256,
        verdict="APPROVE",
        summary="Clean diff.",
        findings=(
            ReviewFinding(
                finding_id="F-1",
                severity="suggestion",
                file_path="src/app.py",
                line_number=12,
                title="Minor naming clarity",
                detail="Consider clearer helper docstring.",
            ),
        ),
    )
    assert ReviewReport.from_dict(rrep.to_dict()) == rrep

    scr = SpecChangeRequest(
        request_id="scr-1",
        created_at="2026-11-13T00:06:00Z",
        spec_id="spec-1",
        spec_version=1,
        spec_sha256=sha256_hex(b"spec"),
        raised_by_role="dev",
        reason="AC-1 conflicts with legacy API signature",
        proposed_changes=("Clarify backwards compatibility fallback",),
        blocking_ac_ids=("AC-1",),
    )
    assert SpecChangeRequest.from_dict(scr.to_dict()) == scr


def test_typescript_repo_context_and_limits(tmp_path: Path) -> None:
    ts_repo = tmp_path / "ts-app"
    (ts_repo / "src").mkdir(parents=True)
    (ts_repo / "tests").mkdir()
    (ts_repo / "package.json").write_text(
        '{"name": "ts-billing", "devDependencies": {"vitest": "^2.0.0"}, "scripts": {"test": "vitest run"}}',
        encoding="utf-8",
    )
    (ts_repo / "tsconfig.json").write_text('{"compilerOptions": {"strict": true}}', encoding="utf-8")
    (ts_repo / "src" / "math.ts").write_text(
        "export interface Invoice { total: number }\nexport function sumCents(a: number, b: number): number {\n  return a + b;\n}\n",
        encoding="utf-8",
    )
    (ts_repo / "tests" / "math.test.ts").write_text("import { expect, test } from 'vitest';\n", encoding="utf-8")

    ctx = build_repo_context(ts_repo)
    assert ctx.language == "typescript"
    assert ctx.repo_name == "ts-billing"
    assert ctx.test_runner.framework == "vitest"
    assert "package.json" in ctx.protected_config_paths
    assert "tsconfig.json" in ctx.protected_config_paths
    assert "tests/math.test.ts" in ctx.existing_test_files
    assert ctx.symbol_names_by_file()["src/math.ts"] == ("Invoice", "sumCents")

    with pytest.raises(RepoLimitExceededError):
        build_repo_context(ts_repo, max_tracked_files=2)
