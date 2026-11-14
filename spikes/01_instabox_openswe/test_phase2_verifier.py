"""Offline unit & real-git/real-pytest tests for `phase2_verifier.py`."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from phase2_verifier import (  # noqa: E402
    ChangedFileEntry,
    collect_existing_test_files,
    collect_protected_config_paths,
    evaluate_phase2_verification,
    is_protected_config_path,
    is_test_path,
    merge_git_changes,
    normalize_repo_rel_path,
    parse_git_name_status_z,
    parse_junit_xml,
)


def _junit_xml(cases: list[tuple[str, str, str]]) -> bytes:
    body_parts: list[str] = []
    for classname, name, status in cases:
        if status == "passed":
            body_parts.append(f'<testcase classname="{classname}" name="{name}" time="0.01" />')
        elif status == "failed":
            body_parts.append(
                f'<testcase classname="{classname}" name="{name}" time="0.01"><failure message="boom" /></testcase>'
            )
        elif status == "error":
            body_parts.append(
                f'<testcase classname="{classname}" name="{name}" time="0.01"><error message="err" /></testcase>'
            )
        elif status == "skipped":
            body_parts.append(
                f'<testcase classname="{classname}" name="{name}" time="0.01"><skipped message="skip" /></testcase>'
            )
    return (
        f'<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" tests="{len(cases)}">'
        + "".join(body_parts)
        + "</testsuite></testsuites>"
    ).encode("utf-8")


def test_path_classification_and_normalization() -> None:
    assert normalize_repo_rel_path(r"src\mini_ledger\billing.py") == "src/mini_ledger/billing.py"
    with pytest.raises(ValueError):
        normalize_repo_rel_path("/abs/path.py")
    with pytest.raises(ValueError):
        normalize_repo_rel_path("../escape.py")

    for protected in (
        "pyproject.toml",
        "pytest.ini",
        "setup.cfg",
        "tox.ini",
        "conftest.py",
        "uv.lock",
        "requirements-dev.txt",
        ".github/workflows/ci.yml",
        "ci/test.sh",
    ):
        assert is_protected_config_path(protected) is True

    assert is_protected_config_path("tests/conftest.py") is True
    assert is_protected_config_path("tests/unit/conftest.py") is True
    assert is_protected_config_path("src/pkg/conftest.py") is True
    assert is_protected_config_path("src/mini_ledger/billing.py") is False

    assert is_test_path("tests/test_billing.py") is True
    assert is_test_path("tests/conftest.py") is False
    assert is_test_path("pkg/sub_test.py") is True
    assert is_test_path("conftest.py") is False
    assert is_test_path("src/mini_ledger/billing.py") is False

    repo_files = [
        "pyproject.toml",
        "src/mini_ledger/billing.py",
        "tests/test_billing.py",
        "tests/conftest.py",
    ]
    assert collect_existing_test_files(repo_files) == ("tests/test_billing.py",)
    assert collect_protected_config_paths(repo_files) == ("pyproject.toml", "tests/conftest.py")


def test_junit_xml_parsing_handles_duplicates_and_errors() -> None:
    xml = _junit_xml(
        [
            ("tests.test_a", "test_ok", "passed"),
            ("tests.test_a", "test_dup", "passed"),
            ("tests.test_a", "test_dup", "failed"),
            ("tests.test_a", "test_skip", "skipped"),
            ("tests.test_b", "test_err", "error"),
        ]
    )
    parsed = parse_junit_xml(xml)
    assert parsed == {
        "tests.test_a::test_ok": "passed",
        "tests.test_a::test_dup": "failed",
        "tests.test_a::test_skip": "skipped",
        "tests.test_b::test_err": "error",
    }

    with pytest.raises(ValueError, match="empty"):
        parse_junit_xml(b"")
    with pytest.raises(ValueError, match="Malformed"):
        parse_junit_xml(b"<not-closed>")


def test_git_z_parsing_handles_renames_spaces_and_symlinks() -> None:
    ns_raw = b"M\x00src/app.py\x00R100\x00tests/old name.py\x00tests/new name.py\x00A\x00evil link\x00"
    raw_modes = (
        b":100644 100644 aaa bbb M\x00src/app.py\x00"
        b":000000 120000 000 ccc A\x00evil link\x00"
    )
    parsed_ns = parse_git_name_status_z(ns_raw)
    assert len(parsed_ns) == 3
    assert parsed_ns[1].old_path == "tests/old name.py"
    assert parsed_ns[1].path == "tests/new name.py"

    merged = merge_git_changes(ns_raw, raw_modes)
    assert merged[0].new_mode == "100644"
    assert merged[2].new_mode == "120000"


def test_evaluate_verdicts_across_hard_gates() -> None:
    base_status = {"tests.test_billing::test_ok": "passed"}
    good_changes = [
        ChangedFileEntry("M", "src/mini_ledger/billing.py", new_mode="100644"),
        ChangedFileEntry("A", "tests/test_regression.py", new_mode="100644"),
    ]
    red_status = {
        "tests.test_billing::test_ok": "passed",
        "tests.test_regression::test_rounding": "failed",
    }
    post_status = {
        "tests.test_billing::test_ok": "passed",
        "tests.test_regression::test_rounding": "passed",
    }

    # 1. Valid bugfix + TDD Red->Green -> PASS
    res_pass = evaluate_phase2_verification(
        existing_test_files=("tests/test_billing.py",),
        changed_entries=good_changes,
        patch_bytes=b"diff --git ...",
        baseline_status=base_status,
        red_status=red_status,
        post_status=post_status,
        baseline_config_hashes={"pyproject.toml": "h1"},
        post_agent_config_hashes={"pyproject.toml": "h1"},
    )
    assert res_pass.verdict == "PASS"
    assert res_pass.red_to_green_ids == ("tests.test_regression::test_rounding",)

    # 2. Existing test file edited -> NEEDS_HUMAN_REVIEW
    res_existing_edit = evaluate_phase2_verification(
        existing_test_files=("tests/test_billing.py",),
        changed_entries=[
            *good_changes,
            ChangedFileEntry("M", "tests/test_billing.py", new_mode="100644"),
        ],
        patch_bytes=b"diff --git ...",
        baseline_status=base_status,
        red_status=red_status,
        post_status=post_status,
    )
    assert res_existing_edit.verdict == "NEEDS_HUMAN_REVIEW"
    assert res_existing_edit.modified_existing_test_files == ("tests/test_billing.py",)

    # 3. Protected config modified without authorization -> REJECT
    res_cfg_reject = evaluate_phase2_verification(
        existing_test_files=("tests/test_billing.py",),
        changed_entries=[
            *good_changes,
            ChangedFileEntry("M", "pyproject.toml", new_mode="100644"),
        ],
        patch_bytes=b"diff --git ...",
        baseline_status=base_status,
        red_status=red_status,
        post_status=post_status,
    )
    assert res_cfg_reject.verdict == "REJECT"
    assert res_cfg_reject.touched_protected_configs == ("pyproject.toml",)

    # 4. Protected config hash changed quietly with allow_config_changes=True -> NEEDS_HUMAN_REVIEW
    res_cfg_review = evaluate_phase2_verification(
        existing_test_files=("tests/test_billing.py",),
        changed_entries=good_changes,
        patch_bytes=b"diff --git ...",
        baseline_status=base_status,
        red_status=red_status,
        post_status=post_status,
        baseline_config_hashes={"pyproject.toml": "h1"},
        post_agent_config_hashes={"pyproject.toml": "h2"},
        allow_config_changes=True,
    )
    assert res_cfg_review.verdict == "NEEDS_HUMAN_REVIEW"

    # 5. Symlink introduced -> REJECT
    res_symlink = evaluate_phase2_verification(
        existing_test_files=("tests/test_billing.py",),
        changed_entries=[*good_changes, ChangedFileEntry("A", "src/link", new_mode="120000")],
        patch_bytes=b"diff --git ...",
        baseline_status=base_status,
        red_status=red_status,
        post_status=post_status,
    )
    assert res_symlink.verdict == "REJECT"
    assert res_symlink.forbidden_mode_entries

    # 6. Regression (baseline test disappeared) -> REJECT
    res_missing = evaluate_phase2_verification(
        existing_test_files=("tests/test_billing.py",),
        changed_entries=good_changes,
        patch_bytes=b"diff --git ...",
        baseline_status=base_status,
        red_status=red_status,
        post_status={"tests.test_regression::test_rounding": "passed"},
    )
    assert res_missing.verdict == "REJECT"
    assert res_missing.missing_baseline_ids == ("tests.test_billing::test_ok",)

    # 7. New test didn't fail in Red (passed already on baseline) -> REJECT
    res_no_red = evaluate_phase2_verification(
        existing_test_files=("tests/test_billing.py",),
        changed_entries=good_changes,
        patch_bytes=b"diff --git ...",
        baseline_status=base_status,
        red_status={"tests.test_billing::test_ok": "passed", "tests.test_regression::test_rounding": "passed"},
        post_status=post_status,
    )
    assert res_no_red.verdict == "REJECT"

    # 8. Collection error in Red (not actual test assertion failure) -> REJECT
    res_red_err = evaluate_phase2_verification(
        existing_test_files=("tests/test_billing.py",),
        changed_entries=good_changes,
        patch_bytes=b"diff --git ...",
        baseline_status=base_status,
        red_status={"tests.test_billing::test_ok": "passed", "tests.test_regression": "error"},
        post_status=post_status,
    )
    assert res_red_err.verdict == "REJECT"

    # 9. tdd_exempt=True allows no Red run when evidence provided, but still rejects regressions
    res_exempt_ok = evaluate_phase2_verification(
        existing_test_files=("tests/test_billing.py",),
        changed_entries=[ChangedFileEntry("M", "src/mini_ledger/billing.py", new_mode="100644")],
        patch_bytes=b"diff --git ...",
        baseline_status=base_status,
        red_status=None,
        post_status=base_status,
        tdd_exempt=True,
        tdd_exempt_evidence="Post suite passed cleanly.",
    )
    assert res_exempt_ok.verdict == "PASS"

    res_exempt_regress = evaluate_phase2_verification(
        existing_test_files=("tests/test_billing.py",),
        changed_entries=[ChangedFileEntry("M", "src/mini_ledger/billing.py", new_mode="100644")],
        patch_bytes=b"diff --git ...",
        baseline_status=base_status,
        red_status=None,
        post_status={"tests.test_billing::test_ok": "failed"},
        tdd_exempt=True,
        tdd_exempt_evidence="Post suite failed.",
    )
    assert res_exempt_regress.verdict == "REJECT"

    # 10. Nested conftest.py added or edited -> REJECT (or NEEDS_HUMAN_REVIEW if allow_config_changes=True)
    res_nested_conftest = evaluate_phase2_verification(
        existing_test_files=("tests/test_billing.py",),
        changed_entries=[*good_changes, ChangedFileEntry("A", "tests/conftest.py", new_mode="100644")],
        patch_bytes=b"diff --git ...",
        baseline_status=base_status,
        red_status=red_status,
        post_status=post_status,
    )
    assert res_nested_conftest.verdict == "REJECT"
    assert res_nested_conftest.touched_protected_configs == ("tests/conftest.py",)

    res_nested_conftest_review = evaluate_phase2_verification(
        existing_test_files=("tests/test_billing.py",),
        changed_entries=[*good_changes, ChangedFileEntry("A", "tests/sub/conftest.py", new_mode="100644")],
        patch_bytes=b"diff --git ...",
        baseline_status=base_status,
        red_status=red_status,
        post_status=post_status,
        allow_config_changes=True,
    )
    assert res_nested_conftest_review.verdict == "NEEDS_HUMAN_REVIEW"
    assert res_nested_conftest_review.touched_protected_configs == ("tests/sub/conftest.py",)

    # 11. Non-zero post pytest exit code (interrupted/collection/internal/empty) MUST REJECT even with passing XML
    for bad_post_code in (1, 2, 3, 4, 5):
        res_bad_post_exit = evaluate_phase2_verification(
            existing_test_files=("tests/test_billing.py",),
            changed_entries=good_changes,
            patch_bytes=b"diff --git ...",
            baseline_status=base_status,
            red_status=red_status,
            red_exit_code=1,
            post_status=post_status,
            post_exit_code=bad_post_code,
        )
        assert res_bad_post_exit.verdict == "REJECT"
        assert res_bad_post_exit.post_exit_code == bad_post_code

    # 12. Unexpected Red pytest exit code (e.g., 2=collection error or 0=all passed) MUST REJECT
    for bad_red_code in (0, 2, 3, 5):
        res_bad_red_exit = evaluate_phase2_verification(
            existing_test_files=("tests/test_billing.py",),
            changed_entries=good_changes,
            patch_bytes=b"diff --git ...",
            baseline_status=base_status,
            red_status=red_status,
            red_exit_code=bad_red_code,
            post_status=post_status,
            post_exit_code=0,
        )
        assert res_bad_red_exit.verdict == "REJECT"
        assert res_bad_red_exit.red_exit_code == bad_red_code


def test_real_git_and_pytest_end_to_end_on_mini_bug_fixture(tmp_path: Path) -> None:
    """Exercise real git diff extraction + clean checkout + Red->Green JUnit XML on `mini_bug_repo`."""
    if shutil.which("git") is None:
        pytest.skip("git executable not available")

    fixture_src = _THIS_DIR / "fixtures" / "mini_bug_repo"
    agent_repo = tmp_path / "agent_repo"
    shutil.copytree(fixture_src, agent_repo)

    def run_git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> bytes:
        full_env = {**os.environ, **(env or {})}
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=full_env,
            capture_output=True,
            check=True,
        )
        return proc.stdout

    def run_pytest(cwd: Path, xml_out: Path) -> tuple[int, dict[str, str]]:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                f"--junitxml={xml_out}",
                "-q",
            ],
            cwd=cwd,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
        )
        return proc.returncode, parse_junit_xml(xml_out.read_bytes())

    run_git(agent_repo, "init", "-q")
    run_git(agent_repo, "config", "user.name", "Test Bot")
    run_git(agent_repo, "config", "user.email", "test@example.invalid")
    run_git(agent_repo, "add", "-A")
    run_git(agent_repo, "commit", "-q", "-m", "baseline")
    base_sha = run_git(agent_repo, "rev-parse", "HEAD").decode().strip()

    # Baseline capture before edits
    tracked = [p for p in run_git(agent_repo, "ls-files", "-z").decode("utf-8").split("\x00") if p]
    existing_tests = collect_existing_test_files(tracked)
    assert existing_tests == ("tests/test_billing.py",)

    base_code, baseline_status = run_pytest(agent_repo, tmp_path / "base.xml")
    assert base_code == 0
    assert len(baseline_status) == 4

    # Simulate agent fixing billing.py (committed) + adding regression test (untracked!)
    billing_path = agent_repo / "src" / "mini_ledger" / "billing.py"
    billing_text = billing_path.read_text(encoding="utf-8")
    billing_text = billing_text.replace(
        "if isinstance(discount_percent, bool) or not isinstance(discount_percent, int) or discount_percent < 0:",
        "if isinstance(discount_percent, bool) or not isinstance(discount_percent, int) or discount_percent < 0 or discount_percent > 100:",
    ).replace(
        "discount = (subtotal_cents * discount_percent) // 100",
        "discount = (subtotal_cents * discount_percent + 50) // 100",
    )
    billing_path.write_text(billing_text, encoding="utf-8")
    run_git(agent_repo, "add", "src/mini_ledger/billing.py")
    run_git(agent_repo, "commit", "-q", "-m", "fix discount calculation")

    new_test_path = agent_repo / "tests" / "test_discount_regression.py"
    new_test_path.write_text(
        "import pytest\n"
        "from mini_ledger.billing import apply_discount_cents\n\n"
        "def test_discount_rejects_over_100() -> None:\n"
        "    with pytest.raises(ValueError):\n"
        "        apply_discount_cents(1000, 101)\n\n"
        "def test_discount_half_up_rounding() -> None:\n"
        "    assert apply_discount_cents(105, 10) == 94\n",
        encoding="utf-8",
    )

    # Extract patch with temp index (covering committed edit + untracked test)
    idx_file = str(tmp_path / "extract_idx")
    idx_env = {"GIT_INDEX_FILE": idx_file}
    run_git(agent_repo, "read-tree", "HEAD", env=idx_env)
    run_git(agent_repo, "add", "-A", env=idx_env)
    ns_raw = run_git(agent_repo, "diff", "--cached", "--name-status", "-M", "--no-ext-diff", "-z", base_sha, env=idx_env)
    modes_raw = run_git(agent_repo, "diff", "--cached", "--raw", "--no-renames", "-z", base_sha, env=idx_env)
    full_patch = run_git(agent_repo, "diff", "--cached", "--binary", "--no-ext-diff", "--full-index", base_sha, env=idx_env)
    changes = merge_git_changes(ns_raw, modes_raw)

    tests_only_patch = run_git(
        agent_repo,
        "diff",
        "--cached",
        "--binary",
        "--no-ext-diff",
        "--full-index",
        base_sha,
        "--",
        "tests/test_discount_regression.py",
        env=idx_env,
    )

    # Clean verify workspace restored to base_sha (simulating pre_agent_snapshot_id)
    verify_repo = tmp_path / "verify_repo"
    shutil.copytree(fixture_src, verify_repo)
    run_git(verify_repo, "init", "-q")
    run_git(verify_repo, "config", "user.name", "Verify Bot")
    run_git(verify_repo, "config", "user.email", "verify@example.invalid")
    run_git(verify_repo, "add", "-A")
    run_git(verify_repo, "commit", "-q", "-m", "baseline")

    # Apply tests-only patch -> Red must fail cleanly
    (tmp_path / "red.patch").write_bytes(tests_only_patch)
    run_git(verify_repo, "apply", str(tmp_path / "red.patch"))
    red_code, red_status = run_pytest(verify_repo, tmp_path / "red.xml")
    assert red_code == 1

    # Reset clean and apply full patch -> Green must pass
    run_git(verify_repo, "reset", "-q", "--hard", "HEAD")
    run_git(verify_repo, "clean", "-fdxq")
    (tmp_path / "full.patch").write_bytes(full_patch)
    run_git(verify_repo, "apply", str(tmp_path / "full.patch"))
    post_code, post_status = run_pytest(verify_repo, tmp_path / "post.xml")
    assert post_code == 0

    result = evaluate_phase2_verification(
        existing_test_files=existing_tests,
        changed_entries=changes,
        patch_bytes=full_patch,
        baseline_status=baseline_status,
        red_status=red_status,
        post_status=post_status,
    )
    assert result.verdict == "PASS"
    assert result.red_to_green_ids == (
        "tests.test_discount_regression::test_discount_half_up_rounding",
        "tests.test_discount_regression::test_discount_rejects_over_100",
    )
    assert result.post_counts["passed"] == 6
