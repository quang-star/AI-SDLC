"""Independent pre/post verification and Hard Gate enforcement for Spike 01 Phase 2.

All classification and diff-verification helpers in this module are pure and
operate strictly on data extracted outside the coding agent.
"""

from __future__ import annotations

import posixpath
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

_REPO_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))

from ai_sdlc.repo_policy import (  # noqa: E402
    TEST_FILE_RE as _TEST_FILE_RE,
    collect_existing_test_files,
    collect_protected_config_paths,
    is_protected_config_path,
    is_test_path,
    normalize_repo_rel_path,
)

TestOutcome = Literal["passed", "failed", "error", "skipped"]
Phase2Verdict = Literal["PASS", "NEEDS_HUMAN_REVIEW", "REJECT"]

_OUTCOME_PRIORITY: dict[TestOutcome, int] = {
    "error": 4,
    "failed": 3,
    "skipped": 2,
    "passed": 1,
}

_FORBIDDEN_GIT_MODES: frozenset[str] = frozenset({"120000", "160000"})


@dataclass(frozen=True)
class ChangedFileEntry:
    """One file-level change computed between `base_commit_sha` and post-agent state."""

    status: str
    path: str
    old_path: str | None = None
    old_mode: str | None = None
    new_mode: str | None = None


@dataclass(frozen=True)
class Phase2VerificationResult:
    """Structured result from independent Phase 2 Hard Gate & snapshot verification."""

    verdict: Phase2Verdict
    reasons: tuple[str, ...]
    new_test_files: tuple[str, ...]
    new_test_ids: tuple[str, ...]
    red_to_green_ids: tuple[str, ...]
    regressed_ids: tuple[str, ...]
    missing_baseline_ids: tuple[str, ...]
    post_failing_ids: tuple[str, ...]
    modified_existing_test_files: tuple[str, ...]
    touched_protected_configs: tuple[str, ...]
    forbidden_mode_entries: tuple[str, ...]
    baseline_counts: dict[str, int]
    post_counts: dict[str, int]
    tdd_exempt: bool
    tdd_exempt_evidence: str | None
    red_exit_code: int | None = None
    post_exit_code: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dictionary."""
        return asdict(self)


def parse_junit_xml(xml_content: bytes | str) -> dict[str, TestOutcome]:
    """Parse pytest `--junitxml` output into `{test_id: outcome}` preserving worst failure on duplicates."""
    raw_text = xml_content.decode("utf-8", errors="strict") if isinstance(xml_content, (bytes, bytearray)) else xml_content
    if not raw_text or not raw_text.strip():
        raise ValueError("JUnit XML content is empty")
    try:
        root = ET.fromstring(raw_text)
    except ET.ParseError as exc:
        raise ValueError(f"Malformed JUnit XML: {exc}") from exc

    if root.tag not in ("testsuite", "testsuites"):
        raise ValueError(f"Unexpected JUnit XML root element: {root.tag!r}")

    suites = [root] if root.tag == "testsuite" else list(root.findall(".//testsuite"))
    if not suites and root.tag == "testsuites":
        # Empty `<testsuites />` without a single `<testsuite>` node means invalid/uncollected run.
        raise ValueError("JUnit XML contains no <testsuite> elements")

    outcomes: dict[str, TestOutcome] = {}
    for case in root.findall(".//testcase"):
        classname = (case.attrib.get("classname") or "").strip()
        name = (case.attrib.get("name") or "").strip()
        test_id = f"{classname}::{name}" if classname and name else (classname or name or "<unnamed>")

        if case.find("error") is not None:
            outcome: TestOutcome = "error"
        elif case.find("failure") is not None:
            outcome = "failed"
        elif case.find("skipped") is not None:
            outcome = "skipped"
        else:
            outcome = "passed"

        prev = outcomes.get(test_id)
        if prev is None or _OUTCOME_PRIORITY[outcome] > _OUTCOME_PRIORITY[prev]:
            outcomes[test_id] = outcome
    return outcomes


def parse_git_name_status_z(raw: bytes | str) -> list[ChangedFileEntry]:
    """Parse `git diff --name-status -M -z` output into structured file change records."""
    text = raw.decode("utf-8", errors="surrogateescape") if isinstance(raw, (bytes, bytearray)) else raw
    if not text:
        return []
    tokens = text.split("\x00")
    entries: list[ChangedFileEntry] = []
    idx = 0
    while idx < len(tokens):
        status_tok = tokens[idx]
        idx += 1
        if not status_tok:
            continue
        code = status_tok[0]
        if code in ("R", "C"):
            if idx + 1 >= len(tokens):
                raise ValueError(f"Truncated git name-status -z record for {status_tok!r}")
            old_path = normalize_repo_rel_path(tokens[idx])
            new_path = normalize_repo_rel_path(tokens[idx + 1])
            idx += 2
            entries.append(ChangedFileEntry(status=status_tok, path=new_path, old_path=old_path))
        else:
            if idx >= len(tokens):
                raise ValueError(f"Truncated git name-status -z record for {status_tok!r}")
            path = normalize_repo_rel_path(tokens[idx])
            idx += 1
            entries.append(ChangedFileEntry(status=status_tok, path=path))
    return entries


def parse_git_raw_modes_z(raw: bytes | str) -> dict[str, tuple[str, str]]:
    """Parse `git diff --raw --no-renames -z` output into `{path: (old_mode, new_mode)}`."""
    text = raw.decode("utf-8", errors="surrogateescape") if isinstance(raw, (bytes, bytearray)) else raw
    if not text:
        return {}
    tokens = text.split("\x00")
    modes: dict[str, tuple[str, str]] = {}
    idx = 0
    while idx < len(tokens):
        meta = tokens[idx]
        idx += 1
        if not meta:
            continue
        if not meta.startswith(":"):
            raise ValueError(f"Unexpected git --raw metadata token: {meta!r}")
        fields = meta[1:].split()
        if len(fields) < 5:
            raise ValueError(f"Malformed git --raw metadata token: {meta!r}")
        if idx >= len(tokens):
            raise ValueError(f"Truncated git --raw -z record for {meta!r}")
        path = normalize_repo_rel_path(tokens[idx])
        idx += 1
        modes[path] = (fields[0], fields[1])
    return modes


def merge_git_changes(
    name_status_raw: bytes | str,
    raw_modes_raw: bytes | str = "",
) -> list[ChangedFileEntry]:
    """Combine `--name-status -M -z` entries with `--raw --no-renames -z` git file modes."""
    entries = parse_git_name_status_z(name_status_raw)
    modes = parse_git_raw_modes_z(raw_modes_raw)
    merged: list[ChangedFileEntry] = []
    for entry in entries:
        old_mode: str | None = None
        new_mode: str | None = None
        if entry.path in modes:
            old_mode, new_mode = modes[entry.path]
        if entry.old_path and entry.old_path in modes:
            old_m_src, _ = modes[entry.old_path]
            old_mode = old_mode or old_m_src
        merged.append(
            ChangedFileEntry(
                status=entry.status,
                path=entry.path,
                old_path=entry.old_path,
                old_mode=old_mode,
                new_mode=new_mode,
            )
        )
    return merged


def _candidate_module_names(rel_path: str) -> tuple[str, ...]:
    norm = normalize_repo_rel_path(rel_path)
    if not norm.endswith(".py"):
        return ()
    no_ext = norm[:-3]
    if no_ext.endswith("/__init__"):
        no_ext = no_ext[:-9]
    dotted = no_ext.replace("/", ".")
    candidates = [dotted]
    if dotted.startswith("tests."):
        candidates.append(dotted[len("tests."):])
    elif dotted.startswith("test."):
        candidates.append(dotted[len("test."):])
    return tuple(dict.fromkeys(c for c in candidates if c))


def _test_id_belongs_to_paths(test_id: str, target_paths: Sequence[str], existing_paths: Iterable[str]) -> bool:
    if "::" not in test_id:
        return False
    classname = test_id.split("::", 1)[0].strip()
    if not classname:
        return False

    # Avoid short `tests.`-stripped ambiguity if an existing test shares the exact module suffix.
    existing_full_modules: set[str] = set()
    for p in existing_paths:
        cands = _candidate_module_names(p)
        if cands:
            existing_full_modules.add(cands[0])

    for path in target_paths:
        cands = _candidate_module_names(path)
        if not cands:
            continue
        full_mod = cands[0]
        if classname == full_mod or classname.startswith(f"{full_mod}."):
            return True
        for stripped_mod in cands[1:]:
            if classname == stripped_mod or classname.startswith(f"{stripped_mod}."):
                if not any(m.endswith(f".{stripped_mod}") for m in existing_full_modules):
                    return True
    return False


def _count_outcomes(status_map: Mapping[str, TestOutcome]) -> dict[str, int]:
    counts = {"passed": 0, "failed": 0, "error": 0, "skipped": 0, "total": len(status_map)}
    for outcome in status_map.values():
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def evaluate_static_gates(
    *,
    existing_test_files: Iterable[str],
    changed_entries: Sequence[ChangedFileEntry],
    patch_bytes: bytes,
    baseline_config_hashes: Mapping[str, str] | None = None,
    post_agent_config_hashes: Mapping[str, str] | None = None,
    allow_config_changes: bool = False,
) -> tuple[
    list[str],
    list[str],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    """Evaluate static diff & Hard Gate rules prior to or during verification.

    Returns:
        `(reject_reasons, review_reasons, new_test_files, modified_existing_tests, touched_protected_configs, forbidden_mode_entries)`
    """
    reject_reasons: list[str] = []
    review_reasons: list[str] = []
    existing_set = {normalize_repo_rel_path(p) for p in existing_test_files}

    if not patch_bytes or not patch_bytes.strip() or not changed_entries:
        reject_reasons.append("Empty patch: agent produced no repository changes.")
        return reject_reasons, review_reasons, (), (), (), ()

    forbidden_modes: list[str] = []
    touched_configs: set[str] = set()
    modified_existing: set[str] = set()
    new_test_files: list[str] = []

    for entry in changed_entries:
        norm_path = normalize_repo_rel_path(entry.path)
        norm_old = normalize_repo_rel_path(entry.old_path) if entry.old_path else None

        if entry.new_mode in _FORBIDDEN_GIT_MODES or entry.old_mode in _FORBIDDEN_GIT_MODES:
            forbidden_modes.append(f"{norm_path} ({entry.old_mode or '?'} -> {entry.new_mode or '?'})")

        if is_protected_config_path(norm_path):
            touched_configs.add(norm_path)
        if norm_old and is_protected_config_path(norm_old):
            touched_configs.add(norm_old)

        status_code = entry.status[0]
        if norm_old and norm_old in existing_set:
            modified_existing.add(norm_old)
        elif norm_path in existing_set and status_code != "A":
            modified_existing.add(norm_path)
        elif status_code == "A" and norm_path not in existing_set and is_test_path(norm_path):
            if _TEST_FILE_RE.match(posixpath.basename(norm_path)):
                new_test_files.append(norm_path)

    if baseline_config_hashes is not None and post_agent_config_hashes is not None:
        base_cfgs = {normalize_repo_rel_path(k): v for k, v in baseline_config_hashes.items()}
        post_cfgs = {normalize_repo_rel_path(k): v for k, v in post_agent_config_hashes.items()}
        for cfg_path in sorted(set(base_cfgs) | set(post_cfgs)):
            if base_cfgs.get(cfg_path) != post_cfgs.get(cfg_path):
                touched_configs.add(cfg_path)

    if forbidden_modes:
        reject_reasons.append(
            f"Forbidden git entry modes (symlink/gitlink): {', '.join(sorted(forbidden_modes))}"
        )

    sorted_configs = tuple(sorted(touched_configs))
    if sorted_configs:
        msg = f"Protected config/CI paths modified: {', '.join(sorted_configs)}"
        if allow_config_changes:
            review_reasons.append(f"{msg} (allow_config_changes=True -> NEEDS_HUMAN_REVIEW)")
        else:
            reject_reasons.append(f"{msg} (forbidden_paths -> REJECT)")

    sorted_existing = tuple(sorted(modified_existing))
    if sorted_existing:
        review_reasons.append(
            f"Existing test files modified/deleted/renamed: {', '.join(sorted_existing)} (NEEDS_HUMAN_REVIEW)"
        )

    return (
        reject_reasons,
        review_reasons,
        tuple(sorted(set(new_test_files))),
        sorted_existing,
        sorted_configs,
        tuple(sorted(forbidden_modes)),
    )


def evaluate_phase2_verification(
    *,
    existing_test_files: Iterable[str],
    changed_entries: Sequence[ChangedFileEntry],
    patch_bytes: bytes,
    baseline_status: Mapping[str, TestOutcome],
    post_status: Mapping[str, TestOutcome] | None,
    red_status: Mapping[str, TestOutcome] | None = None,
    red_exit_code: int | None = None,
    post_exit_code: int | None = None,
    baseline_config_hashes: Mapping[str, str] | None = None,
    post_agent_config_hashes: Mapping[str, str] | None = None,
    allow_config_changes: bool = False,
    tdd_exempt: bool = False,
    tdd_exempt_evidence: str | None = None,
) -> Phase2VerificationResult:
    """Apply Hard Gates, No-Regression checks, pytest exit-code checks, and post-hoc baseline Red->Green checks.

    Note: The Red step replays newly added tests against the clean baseline after the agent finishes;
    it proves those tests detect the bug on baseline (and pass with the full patch), not that the
    agent itself authored tests chronologically before edits.
    """
    existing_tuple = tuple(sorted(normalize_repo_rel_path(p) for p in existing_test_files))
    (
        reject_reasons,
        review_reasons,
        new_test_files,
        modified_existing_tests,
        touched_protected_configs,
        forbidden_mode_entries,
    ) = evaluate_static_gates(
        existing_test_files=existing_tuple,
        changed_entries=changed_entries,
        patch_bytes=patch_bytes,
        baseline_config_hashes=baseline_config_hashes,
        post_agent_config_hashes=post_agent_config_hashes,
        allow_config_changes=allow_config_changes,
    )

    if not baseline_status:
        reject_reasons.append("Baseline test suite collected zero test cases.")

    missing_baseline: list[str] = []
    regressed: list[str] = []
    red_to_green_ids: list[str] = []
    new_test_ids: tuple[str, ...] = ()
    post_failing: tuple[str, ...] = ()

    if post_status is None:
        if not reject_reasons:
            reject_reasons.append("Post-verification test suite was not executed.")
        post_counts: dict[str, int] = {}
    else:
        if post_exit_code is not None and post_exit_code != 0:
            reject_reasons.append(
                f"Post-verification pytest exited with non-zero code {post_exit_code} "
                "(expected 0; collection, internal, interrupted, or test failures are rejected)."
            )

        if not post_status:
            reject_reasons.append("Post-verification test suite collected zero test cases.")

        for tid, base_outcome in sorted(baseline_status.items()):
            if tid not in post_status:
                missing_baseline.append(tid)
                continue
            post_outcome = post_status[tid]
            if base_outcome == "passed" and post_outcome != "passed":
                regressed.append(f"{tid} ({base_outcome} -> {post_outcome})")
            elif base_outcome != "failed" and base_outcome != "error" and post_outcome in ("failed", "error"):
                regressed.append(f"{tid} ({base_outcome} -> {post_outcome})")

        if missing_baseline:
            reject_reasons.append(
                f"Baseline test IDs disappeared from post report: {', '.join(missing_baseline)}"
            )
        if regressed:
            reject_reasons.append(f"Test regressions detected: {', '.join(regressed)}")

        new_test_ids = tuple(sorted(tid for tid in post_status if tid not in baseline_status))
        post_failing = tuple(
            sorted(
                tid
                for tid, outcome in post_status.items()
                if outcome in ("failed", "error")
                and (tid not in baseline_status or baseline_status[tid] not in ("failed", "error"))
            )
        )
        if post_failing:
            reject_reasons.append(f"Failing/error tests in post verification: {', '.join(post_failing)}")

        if not tdd_exempt:
            if not new_test_files:
                reject_reasons.append(
                    "Baseline-fail (Red) gate failed: no newly added test file (`tests/...` / `test_*.py`) outside existing tests."
                )
            elif red_status is None:
                reject_reasons.append("Baseline-fail (Red) gate failed: Red verification run was not executed.")
            else:
                if red_exit_code is not None and red_exit_code != 1:
                    reject_reasons.append(
                        f"Red baseline-check pytest exited with unexpected code {red_exit_code} "
                        "(expected 1 from test assertion failures, not 0/interrupted/collection/internal error)."
                    )
                for tid in new_test_ids:
                    if (
                        _test_id_belongs_to_paths(tid, new_test_files, existing_tuple)
                        and red_status.get(tid) == "failed"
                        and post_status.get(tid) == "passed"
                    ):
                        red_to_green_ids.append(tid)
                if not red_to_green_ids:
                    reject_reasons.append(
                        "Baseline-fail (Red) gate failed: no new test ID in a newly added test file transitioned `failed` (baseline) -> `passed` (full patch) on the pre-agent snapshot."
                    )
        else:
            if not tdd_exempt_evidence or not tdd_exempt_evidence.strip():
                reject_reasons.append(
                    "tdd_exempt=True requires non-empty `tdd_exempt_evidence` documenting post-verification evidence."
                )
        post_counts = _count_outcomes(post_status)

    reasons = [*reject_reasons, *review_reasons]
    if reject_reasons:
        verdict: Phase2Verdict = "REJECT"
    elif review_reasons:
        verdict = "NEEDS_HUMAN_REVIEW"
    else:
        verdict = "PASS"

    return Phase2VerificationResult(
        verdict=verdict,
        reasons=tuple(reasons),
        new_test_files=new_test_files,
        new_test_ids=new_test_ids,
        red_to_green_ids=tuple(sorted(red_to_green_ids)),
        regressed_ids=tuple(regressed),
        missing_baseline_ids=tuple(missing_baseline),
        post_failing_ids=post_failing,
        modified_existing_test_files=modified_existing_tests,
        touched_protected_configs=touched_protected_configs,
        forbidden_mode_entries=forbidden_mode_entries,
        baseline_counts=_count_outcomes(baseline_status),
        post_counts=post_counts,
        tdd_exempt=tdd_exempt,
        tdd_exempt_evidence=tdd_exempt_evidence.strip() if tdd_exempt_evidence else None,
        red_exit_code=red_exit_code,
        post_exit_code=post_exit_code,
    )
