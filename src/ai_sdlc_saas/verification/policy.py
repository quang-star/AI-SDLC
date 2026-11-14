"""Adversarial diff audit and repository/project policy enforcement engine."""

from __future__ import annotations

import ast
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_sdlc.contracts import RepoContext, TaskSpec, sha256_hex
from ai_sdlc.repo_policy import (
    is_protected_config_path,
    is_test_path,
    is_valid_new_test_file_path,
)
from ai_sdlc_saas.repos.git_ops import DiffBundle

_DIFF_HEADER_RE = re.compile(r"^(?:diff --git a/(.+?) b/(.+)|--- (?:a/)?(.+)|\+\+\+ (?:b/)?(.+))$")
_PROMPT_INJECTION_RE = re.compile(
    r"(ignore previous instructions|disregard policy|system override|mark verification pass)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PolicyViolation:
    code: str
    severity: str  # blocker | warning
    path: str
    message: str


@dataclass(frozen=True)
class PolicyEvaluation:
    passed: bool
    violations: tuple[PolicyViolation, ...]
    requires_security_review: bool
    sensitive_paths_touched: tuple[str, ...]
    suspicious_prompt_injections: tuple[str, ...]


def validate_raw_patch_headers(patch_text: str) -> list[PolicyViolation]:
    """Audit raw unified diff text BEFORE `git apply` for traversal, .git tamper, symlinks, gitlinks."""
    violations: list[PolicyViolation] = []
    if not patch_text:
        return violations

    for line in patch_text.splitlines():
        if line.startswith(("new file mode 120000", "index ", "old mode 120000", "new mode 120000")):
            if "120000" in line:
                violations.append(
                    PolicyViolation("symlink_forbidden", "blocker", "<patch>", "Symlink mode 120000 is forbidden.")
                )
        if "160000" in line and (line.startswith(("new file mode", "old mode", "new mode", "index "))):
            violations.append(
                PolicyViolation("gitlink_forbidden", "blocker", "<patch>", "Submodule/gitlink mode 160000 is forbidden.")
            )
        m = _DIFF_HEADER_RE.match(line)
        if not m:
            continue
        candidates = [g for g in m.groups() if g and g != "/dev/null"]
        for raw_p in candidates:
            cleaned = raw_p.strip()
            norm = posixpath.normpath(cleaned.replace("\\", "/"))
            if (
                "\x00" in cleaned
                or cleaned.startswith("/")
                or "\\" in cleaned
                or norm == ".."
                or norm.startswith("../")
                or norm == ".git"
                or norm.startswith(".git/")
                or "/.git/" in f"/{norm}/"
            ):
                violations.append(
                    PolicyViolation(
                        "path_traversal_or_git_tamper",
                        "blocker",
                        cleaned[:200],
                        f"Forbidden unsafe path in patch header: {cleaned!r}",
                    )
                )
    return violations


def _has_meaningful_python_assertions(py_source: str, path: str) -> list[PolicyViolation]:
    """Reject tests containing only `pass`, `assert True`, or zero assertions."""
    violations: list[PolicyViolation] = []
    try:
        tree = ast.parse(py_source, filename=path)
    except SyntaxError as exc:
        violations.append(PolicyViolation("test_syntax_error", "blocker", path, f"Invalid Python syntax: {exc}"))
        return violations

    test_funcs = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
    ]
    if not test_funcs:
        violations.append(
            PolicyViolation("empty_test_module", "blocker", path, "New test module defines no `test_*` functions.")
        )
        return violations

    for fn in test_funcs:
        asserts = [n for n in ast.walk(fn) if isinstance(n, ast.Assert)]
        calls = [
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and (
                (isinstance(n.func, ast.Attribute) and n.func.attr in ("raises", "warns", "fail"))
                or (isinstance(n.func, ast.Name) and n.func.id in ("assert_that",))
            )
        ]
        # Detect pytest.raises usage via `with pytest.raises(...)` or `assert ...`
        nontrivial_assert = False
        for a in asserts:
            if isinstance(a.test, ast.Constant) and bool(a.test.value):
                continue
            if (
                isinstance(a.test, ast.Compare)
                and len(a.test.ops) == 1
                and isinstance(a.test.ops[0], ast.Eq)
                and len(a.test.comparators) == 1
                and isinstance(a.test.left, ast.Constant)
                and isinstance(a.test.comparators[0], ast.Constant)
                and a.test.left.value == a.test.comparators[0].value
            ):
                continue
            nontrivial_assert = True
            break
        if not nontrivial_assert and not calls:
            violations.append(
                PolicyViolation(
                    "trivial_or_noop_test",
                    "blocker",
                    path,
                    f"Test `{fn.name}` has no meaningful assertions (`assert True` / empty no-op rejected).",
                )
            )
    return violations


def _patch_deletes_assertions(patch_text: str) -> list[PolicyViolation]:
    """Flag removed `assert ` lines in any test file within the unified patch."""
    violations: list[PolicyViolation] = []
    current_file = ""
    for line in patch_text.splitlines():
        if line.startswith("+++ b/") or line.startswith("--- a/"):
            p = line[6:].strip()
            if p != "/dev/null":
                current_file = p
            continue
        if line.startswith("-") and not line.startswith("---"):
            removed = line[1:].strip()
            if current_file and is_test_path(current_file) and (
                removed.startswith("assert ") or "expect(" in removed or "assert." in removed
            ):
                violations.append(
                    PolicyViolation(
                        "assertion_deleted",
                        "blocker",
                        current_file,
                        f"Deleted assertion from test file: {removed[:120]}",
                    )
                )
    return violations


def evaluate_diff_against_policy(
    *,
    worktree_dir: Path,
    bundle: DiffBundle,
    repo_context: RepoContext,
    task_spec: TaskSpec,
    project_policy: dict[str, Any] | None = None,
) -> PolicyEvaluation:
    """Strictly evaluate git-observed `DiffBundle` against Hard Gates & tenant ProjectPolicy."""
    violations: list[PolicyViolation] = []
    violations.extend(validate_raw_patch_headers(bundle.patch_text))

    for sym in bundle.symlink_paths:
        violations.append(PolicyViolation("symlink_forbidden", "blocker", sym, "Symlink changes are forbidden."))
    for sub in bundle.submodule_paths:
        violations.append(PolicyViolation("gitlink_forbidden", "blocker", sub, "Git submodule changes are forbidden."))

    policy = project_policy or {}
    forbidden_prefixes = tuple(str(p) for p in (policy.get("forbidden_paths") or (".github/workflows/", "infra/", "deploy/")))
    sensitive_prefixes = tuple(str(p) for p in (policy.get("sensitive_path_prefixes") or ("auth/", "security/", "migrations/")))
    exceptions = {str(p) for p in (policy.get("approved_exceptions") or ())}

    existing_tests = set(repo_context.existing_test_files)
    forbidden_exact = set(task_spec.forbidden_files)
    allowed_new_tests = set(task_spec.expected_new_test_files)

    # Collect every touched path including rename sources (`old_path`)!
    touched_all: list[str] = list(bundle.changed_paths)
    for ns in bundle.name_status:
        if ns.old_path and ns.old_path not in touched_all:
            touched_all.append(ns.old_path)

    sensitive_touched: list[str] = []
    prompt_injections: list[str] = []

    for path in sorted(set(touched_all)):
        norm = posixpath.normpath(path.replace("\\", "/"))
        if norm.startswith(".git/") or norm == ".git":
            violations.append(PolicyViolation("git_metadata_tamper", "blocker", path, "Modifications inside .git are forbidden."))
            continue

        # Nested conftest.py anywhere in repo is a protected test runner config!
        if posixpath.basename(norm) == "conftest.py" and not task_spec.allow_config_changes and norm not in exceptions:
            violations.append(
                PolicyViolation("nested_conftest_tamper", "blocker", norm, "Modifying or adding conftest.py is forbidden.")
            )

        if norm in existing_tests:
            violations.append(
                PolicyViolation("existing_test_tamper", "blocker", norm, "Existing test files cannot be modified or deleted.")
            )
        elif is_test_path(norm) and norm not in allowed_new_tests:
            # Allow only if listed in expected_new_test_files OR explicitly task_spec target
            if not is_valid_new_test_file_path(norm):
                violations.append(
                    PolicyViolation("invalid_test_path", "blocker", norm, "Test file path violates new test path policy.")
                )

        if not task_spec.allow_config_changes and (is_protected_config_path(norm) or norm in forbidden_exact):
            if norm not in allowed_new_tests and norm not in exceptions:
                violations.append(
                    PolicyViolation(
                        "protected_config_or_forbidden_file",
                        "blocker",
                        norm,
                        f"Protected or forbidden file modified: {norm!r}",
                    )
                )

        for fpref in forbidden_prefixes:
            if norm.startswith(fpref) and norm not in exceptions:
                violations.append(
                    PolicyViolation(
                        "project_forbidden_path",
                        "blocker",
                        norm,
                        f"Path {norm!r} falls under project forbidden prefix {fpref!r}.",
                    )
                )

        if any(norm.startswith(sp) for sp in sensitive_prefixes):
            sensitive_touched.append(norm)

        # Inspect file contents on disk cleanly for trivial tests & prompt injections
        abs_p = (worktree_dir / posixpath.normpath(norm)).resolve()
        if abs_p.is_file():
            try:
                text = abs_p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                text = ""
            if norm.endswith(".py") and is_test_path(norm) and norm not in existing_tests:
                violations.extend(_has_meaningful_python_assertions(text, norm))
            if _PROMPT_INJECTION_RE.search(text):
                prompt_injections.append(norm)

    # Check protected config hashes on clean base haven't changed on disk outside git diff
    for cfg_path, expected_sha in (repo_context.protected_config_hashes or {}).items():
        if cfg_path in exceptions or task_spec.allow_config_changes:
            continue
        on_disk = worktree_dir / posixpath.normpath(cfg_path)
        if not on_disk.is_file():
            violations.append(
                PolicyViolation("protected_config_missing", "blocker", cfg_path, f"Protected config missing: {cfg_path}")
            )
        else:
            actual_sha = sha256_hex(on_disk.read_bytes())
            if actual_sha != expected_sha:
                violations.append(
                    PolicyViolation(
                        "protected_config_hash_mismatch",
                        "blocker",
                        cfg_path,
                        f"Protected config hash changed for {cfg_path!r}.",
                    )
                )

    violations.extend(_patch_deletes_assertions(bundle.patch_text))

    passed = not any(v.severity == "blocker" for v in violations)
    return PolicyEvaluation(
        passed=passed,
        violations=tuple(violations),
        requires_security_review=bool(sensitive_touched),
        sensitive_paths_touched=tuple(sorted(sensitive_touched)),
        suspicious_prompt_injections=tuple(sorted(prompt_injections)),
    )
