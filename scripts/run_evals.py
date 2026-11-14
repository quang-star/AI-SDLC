"""Run AI SDLC SaaS evaluation suite (dev + holdout splits) across BA grounding & adversarial verifier policy."""

from __future__ import annotations

import json
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

from ai_sdlc.contracts import (
    SCHEMA_VERSION,
    AcceptanceCriterion,
    RepoContext,
    TaskSpec,
    TestRunnerInfo,
    canonical_json_bytes,
    sha256_hex,
)
from ai_sdlc_saas.adapters.metagpt_service import analyze_requirement_gaps
from ai_sdlc_saas.repos.git_ops import NameStatusEntry, RawDiffEntry
from ai_sdlc_saas.verification.policy import DiffBundle, evaluate_diff_against_policy

ROOT = Path(__file__).resolve().parents[1]
SUITE_PATH = ROOT / "evals" / "suite.json"


def _build_repo_context(tracked_files: list[str]) -> RepoContext:
    test_files = tuple(f for f in tracked_files if f.startswith("tests/") or ".test." in f)
    return RepoContext(
        context_id="ctx_eval",
        captured_at="2026-11-13T00:00:00+00:00",
        repo_name="eval/repo",
        repo_source="local",
        repo_commit_sha="a" * 40,
        commit_source="HEAD",
        repo_tree_sha256="b" * 64,
        is_dirty=False,
        language="python",
        tracked_files=tuple(tracked_files),
        existing_test_files=test_files,
        protected_config_paths=(),
        protected_config_hashes={},
        test_runner=TestRunnerInfo(framework="pytest", command_hint="pytest -q"),
    )


def _run_ba_case(case: dict[str, Any]) -> dict[str, Any]:
    t0 = time.perf_counter()
    tracked = list(case["tracked_files"])
    ctx = _build_repo_context(tracked)
    questions = analyze_requirement_gaps(
        case["requirement_text"],
        ctx,
        all_tracked_files=set(tracked),
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    got_clarify = len(questions) > 0
    passed = got_clarify == bool(case["expect_clarifications"])
    return {
        "id": case["id"],
        "suite": "ba_clarify",
        "category": case["expect_reason_category"],
        "expected": bool(case["expect_clarifications"]),
        "observed": got_clarify,
        "question_count": len(questions),
        "elapsed_ms": round(elapsed_ms, 3),
        "passed": passed,
    }


def _run_verifier_case(case: dict[str, Any]) -> dict[str, Any]:
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="sdlc_eval_") as tmp:
        wt = Path(tmp)
        changed: list[str] = []
        raw_entries: list[RawDiffEntry] = []
        name_status: list[NameStatusEntry] = []

        if not case["tracked_code_edited"]:
            rel = "tests/test_fake.py" if "test_fake.py" in case["patch_diff"] else "tests/test_fake2.py"
            fp = wt / rel
            fp.parent.mkdir(parents=True, exist_ok=True)
            body = "def test_always_true():\n    assert True\n" if "True" in case["patch_diff"] else "def test_trivial():\n    assert 1 == 1\n"
            fp.write_text(body, encoding="utf-8")
            changed.append(rel)
            raw_entries.append(RawDiffEntry("000000", "100644", "A", rel))
            name_status.append(NameStatusEntry("A", rel))
            tracked = ["src/app/main.py"]
            target_files = ("src/app/main.py",)
            expected_tests = (rel,)
        else:
            is_ts = ".ts" in case["patch_diff"]
            rel = "src/server.ts" if is_ts else "src/app/main.py"
            test_rel = "tests/server.test.ts" if is_ts else "tests/test_subtract.py"
            fp = wt / rel
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text("export const x = 1;\n" if is_ts else "def subtract_numbers(a: int, b: int) -> int:\n    return a - b\n", encoding="utf-8")
            tfp = wt / test_rel
            tfp.parent.mkdir(parents=True, exist_ok=True)
            tfp.write_text(
                "import { strict as assert } from 'assert'; assert.equal(2 * 3, 6);\n"
                if is_ts
                else "from app.main import subtract_numbers\n\ndef test_subtract_numbers():\n    assert subtract_numbers(5, 2) == 3\n",
                encoding="utf-8",
            )
            changed = [rel, test_rel]
            raw_entries.append(RawDiffEntry("100644", "100644", "M", rel))
            name_status.append(NameStatusEntry("M", rel))
            raw_entries.append(RawDiffEntry("000000", "100644", "A", test_rel))
            name_status.append(NameStatusEntry("A", test_rel))
            tracked = [rel]
            target_files = (rel,)
            expected_tests = (test_rel,)

        ctx = _build_repo_context(tracked)
        ac = AcceptanceCriterion(
            ac_id="AC-1",
            description="Behavioral check",
            expected_behavior="Returns expected value",
            target_files=target_files,
        )
        canonical_spec_payload = {
            "schema_version": SCHEMA_VERSION,
            "spec_id": "spc_eval",
            "spec_version": 1,
            "created_at": "2026-11-13T00:00:00+00:00",
            "context_id": "ctx_eval",
            "repo_name": "eval/repo",
            "repo_commit_sha": "a" * 40,
            "repo_tree_sha256": "b" * 64,
            "user_requirement": "Eval requirement",
            "task_type": "feature",
            "title": "Eval task",
            "summary": "Eval summary",
            "problem_statement": "Eval problem",
            "scope_in": list(target_files),
            "scope_out": [],
            "target_files": list(target_files),
            "expected_new_files": [],
            "expected_new_test_files": list(expected_tests),
            "forbidden_files": [],
            "acceptance_criteria": [
                {
                    "ac_id": ac.ac_id,
                    "description": ac.description,
                    "expected_behavior": ac.expected_behavior,
                    "verification_method": ac.verification_method,
                    "target_files": list(ac.target_files),
                    "edge_cases": list(ac.edge_cases),
                }
            ],
            "constraints": [],
            "tdd_exempt": False,
            "allow_config_changes": False,
            "author_role": "ba",
            "author_agent": "eval",
            "model_id": "mock",
        }
        spec = TaskSpec(
            spec_id="spc_eval",
            spec_version=1,
            created_at="2026-11-13T00:00:00+00:00",
            context_id="ctx_eval",
            repo_name="eval/repo",
            repo_commit_sha="a" * 40,
            repo_tree_sha256="b" * 64,
            user_requirement="Eval requirement",
            task_type="feature",
            title="Eval task",
            summary="Eval summary",
            problem_statement="Eval problem",
            scope_in=target_files,
            scope_out=(),
            target_files=target_files,
            expected_new_test_files=expected_tests,
            forbidden_files=(),
            acceptance_criteria=(ac,),
            constraints=(),
            author_agent="eval",
            model_id="mock",
            spec_sha256=sha256_hex(canonical_json_bytes(canonical_spec_payload)),
        )
        bundle = DiffBundle(
            base_commit="a" * 40,
            patch_text=case["patch_diff"],
            patch_sha256=sha256_hex(case["patch_diff"].encode("utf-8")),
            name_status=tuple(name_status),
            raw_entries=tuple(raw_entries),
            untracked_files=(),
            symlink_paths=(),
            submodule_paths=(),
            changed_paths=tuple(changed),
        )
        ev = evaluate_diff_against_policy(worktree_dir=wt, bundle=bundle, repo_context=ctx, task_spec=spec)
        verdict = "PASS" if ev.passed else "FAIL"
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    return {
        "id": case["id"],
        "suite": "verifier_adversarial",
        "expected": case["expect_verdict"],
        "observed": verdict,
        "violations": [v.code for v in ev.violations],
        "elapsed_ms": round(elapsed_ms, 3),
        "passed": verdict == case["expect_verdict"],
    }


def run_eval_suite(suite_path: Path = SUITE_PATH) -> dict[str, Any]:
    raw = json.loads(suite_path.read_text(encoding="utf-8"))
    splits = raw["splits"]
    dev_ids = {c["id"] for c in splits["dev"]}
    holdout_ids = {c["id"] for c in splits["holdout"]}
    assert not (dev_ids & holdout_ids), "Holdout split leakage detected!"

    split_reports: dict[str, Any] = {}
    all_latencies: list[float] = []
    for split_name, cases in splits.items():
        results: list[dict[str, Any]] = []
        for c in cases:
            res = _run_ba_case(c) if c["suite"] == "ba_clarify" else _run_verifier_case(c)
            results.append(res)
            all_latencies.append(float(res["elapsed_ms"]))
        passed = sum(1 for r in results if r["passed"])
        total = len(results)
        split_reports[split_name] = {
            "total": total,
            "passed": passed,
            "accuracy": round(passed / max(1, total), 4),
            "cases": results,
        }

    overall_total = sum(s["total"] for s in split_reports.values())
    overall_passed = sum(s["passed"] for s in split_reports.values())
    return {
        "ok": overall_passed == overall_total,
        "total_cases": overall_total,
        "passed_cases": overall_passed,
        "overall_accuracy": round(overall_passed / max(1, overall_total), 4),
        "median_latency_ms": round(statistics.median(all_latencies), 3) if all_latencies else 0.0,
        "splits": split_reports,
    }


def main() -> int:
    report = run_eval_suite()
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
