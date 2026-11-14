"""Canonical shared data contracts for the AI SDLC pipeline.

Every artifact (`RepoContext`, `TaskSpec`, `TestPlan`, `ExecutionReport`) binds to
the repository commit/tree snapshot (`repo_commit_sha`, `repo_tree_sha256`) and
specification version/digest (`spec_id`, `spec_version`, `spec_sha256`) so BA,
QA, human approval, Dev, and independent verification operate on the same
requirement and baseline state.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Final, Literal

from ai_sdlc.repo_policy import (
    is_protected_config_path,
    is_valid_new_test_file_path,
    normalize_repo_rel_path,
)

SCHEMA_VERSION: Final[str] = "1.0"

CommitSource = Literal["git_head", "tree_snapshot"]
FileRole = Literal["source", "test", "config", "docs", "other"]
SymbolKind = Literal["class", "function", "async_function", "method", "async_method"]
TaskType = Literal["bugfix", "feature", "refactor", "chore"]
VerificationMethod = Literal["unit_test", "integration_test", "static_check", "manual"]
SpecStatus = Literal["draft", "qa_reviewed", "approved", "rejected", "superseded"]
ApprovalDecision = Literal["approved", "rejected"]
CritiqueVerdict = Literal["accept", "revise", "block"]
CritiqueSeverity = Literal["blocker", "warning", "suggestion"]
TestCaseType = Literal["unit", "regression", "edge_case", "negative"]
Phase2Verdict = Literal["PASS", "NEEDS_HUMAN_REVIEW", "REJECT"]
TraceabilityStatus = Literal["verified", "partial", "unverified", "failed"]

_AC_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
_TC_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def utc_now_iso() -> str:
    """Return an ISO-8601 UTC timestamp string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical_json_bytes(payload: Any) -> bytes:
    """Serialize `payload` deterministically for cryptographic hashing."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    """Return lowercase hex SHA-256 digest of `data`."""
    return hashlib.sha256(data).hexdigest()


def _require_non_empty(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"`{field_name}` must be a non-empty string.")
    return value.strip()


def _require_sha256(value: str, field_name: str) -> str:
    cleaned = _require_non_empty(value, field_name).lower()
    if not _SHA256_RE.fullmatch(cleaned):
        raise ValueError(f"`{field_name}` must be a 64-character hex SHA-256 digest, got {value!r}.")
    return cleaned


def _normalize_str_tuple(items: Iterable[str], *, strip: bool = True) -> tuple[str, ...]:
    out: list[str] = []
    for raw in items:
        if not isinstance(raw, str):
            raise ValueError(f"Expected string item, got {type(raw).__name__}")
        val = raw.strip() if strip else raw
        if val:
            out.append(val)
    return tuple(out)


def _normalize_path_tuple(paths: Iterable[str]) -> tuple[str, ...]:
    seen: list[str] = []
    for raw in paths:
        norm = normalize_repo_rel_path(raw)
        if norm not in seen:
            seen.append(norm)
    return tuple(seen)


def _slugify(text: str, max_len: int = 24) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if not cleaned:
        return "task"
    return cleaned[:max_len].strip("-") or "task"


@dataclass(frozen=True)
class SymbolInfo:
    """AST-extracted Python symbol summary within a repository file."""

    name: str
    kind: SymbolKind
    signature: str
    line_start: int
    line_end: int
    docstring_summary: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _require_non_empty(self.name, "SymbolInfo.name"))
        object.__setattr__(self, "signature", _require_non_empty(self.signature, "SymbolInfo.signature"))
        if self.line_start < 1 or self.line_end < self.line_start:
            raise ValueError(f"Invalid symbol line range: {self.line_start}..{self.line_end}")
        object.__setattr__(self, "docstring_summary", (self.docstring_summary or "").strip())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SymbolInfo":
        return cls(
            name=data["name"],
            kind=data["kind"],
            signature=data["signature"],
            line_start=int(data["line_start"]),
            line_end=int(data["line_end"]),
            docstring_summary=str(data.get("docstring_summary") or ""),
        )


@dataclass(frozen=True)
class RepoFileSummary:
    """Bounded metadata, symbol table, and safe excerpt for one tracked repository file."""

    path: str
    role: FileRole
    sha256: str
    line_count: int
    symbols: tuple[SymbolInfo, ...] = ()
    excerpt: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", normalize_repo_rel_path(self.path))
        object.__setattr__(self, "sha256", _require_sha256(self.sha256, "RepoFileSummary.sha256"))
        if self.line_count < 0:
            raise ValueError(f"line_count cannot be negative: {self.line_count}")
        object.__setattr__(self, "symbols", tuple(self.symbols))
        object.__setattr__(self, "excerpt", self.excerpt or "")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RepoFileSummary":
        return cls(
            path=data["path"],
            role=data["role"],
            sha256=data["sha256"],
            line_count=int(data["line_count"]),
            symbols=tuple(SymbolInfo.from_dict(item) for item in data.get("symbols", ())),
            excerpt=str(data.get("excerpt") or ""),
        )


@dataclass(frozen=True)
class TestRunnerInfo:
    """Detected test runner configuration in the target repository."""

    __test__ = False

    framework: str = "pytest"
    config_file: str | None = None
    pythonpath: tuple[str, ...] = ()
    testpaths: tuple[str, ...] = ()
    command_hint: str = "pytest -q"

    def __post_init__(self) -> None:
        object.__setattr__(self, "framework", _require_non_empty(self.framework, "TestRunnerInfo.framework"))
        if self.config_file is not None:
            object.__setattr__(self, "config_file", normalize_repo_rel_path(self.config_file))
        object.__setattr__(self, "pythonpath", _normalize_str_tuple(self.pythonpath))
        object.__setattr__(self, "testpaths", _normalize_str_tuple(self.testpaths))
        object.__setattr__(self, "command_hint", _require_non_empty(self.command_hint, "TestRunnerInfo.command_hint"))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TestRunnerInfo":
        return cls(
            framework=str(data.get("framework") or "pytest"),
            config_file=data.get("config_file"),
            pythonpath=tuple(data.get("pythonpath", ())),
            testpaths=tuple(data.get("testpaths", ())),
            command_hint=str(data.get("command_hint") or "pytest -q"),
        )


@dataclass(frozen=True)
class RepoContext:
    """Grounded repository snapshot consumed by BA, QA, and Dev phases."""

    context_id: str
    captured_at: str
    repo_name: str
    repo_source: str
    repo_commit_sha: str
    commit_source: CommitSource
    repo_tree_sha256: str
    is_dirty: bool
    language: str
    tracked_files: tuple[str, ...]
    existing_test_files: tuple[str, ...]
    protected_config_paths: tuple[str, ...]
    protected_config_hashes: dict[str, str]
    test_runner: TestRunnerInfo
    file_contexts: tuple[RepoFileSummary, ...] = ()
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _require_non_empty(self.schema_version, "RepoContext.schema_version"))
        object.__setattr__(self, "context_id", _require_non_empty(self.context_id, "RepoContext.context_id"))
        object.__setattr__(self, "captured_at", _require_non_empty(self.captured_at, "RepoContext.captured_at"))
        object.__setattr__(self, "repo_name", _require_non_empty(self.repo_name, "RepoContext.repo_name"))
        object.__setattr__(self, "repo_source", _require_non_empty(self.repo_source, "RepoContext.repo_source"))
        object.__setattr__(self, "repo_commit_sha", _require_non_empty(self.repo_commit_sha, "RepoContext.repo_commit_sha"))
        object.__setattr__(self, "repo_tree_sha256", _require_sha256(self.repo_tree_sha256, "RepoContext.repo_tree_sha256"))
        object.__setattr__(self, "language", _require_non_empty(self.language, "RepoContext.language"))
        object.__setattr__(self, "tracked_files", tuple(sorted(_normalize_path_tuple(self.tracked_files))))
        object.__setattr__(self, "existing_test_files", tuple(sorted(_normalize_path_tuple(self.existing_test_files))))
        object.__setattr__(self, "protected_config_paths", tuple(sorted(_normalize_path_tuple(self.protected_config_paths))))
        normalized_hashes = {
            normalize_repo_rel_path(k): _require_sha256(v, f"protected_config_hashes[{k!r}]")
            for k, v in self.protected_config_hashes.items()
        }
        object.__setattr__(self, "protected_config_hashes", dict(sorted(normalized_hashes.items())))
        object.__setattr__(self, "file_contexts", tuple(self.file_contexts))

    def symbol_names_by_file(self) -> dict[str, tuple[str, ...]]:
        """Map each file path to the tuple of AST symbol names discovered in it."""
        return {
            fc.path: tuple(sym.name for sym in fc.symbols)
            for fc in self.file_contexts
            if fc.symbols
        }

    def to_prompt_context(self, *, max_files: int = 25) -> str:
        """Render a deterministic, grounded repository summary for BA/QA prompts."""
        lines = [
            f"Repository: {self.repo_name} (source: {self.repo_source})",
            f"Commit Anchor: {self.repo_commit_sha} ({self.commit_source}, dirty={self.is_dirty})",
            f"Tree SHA-256: {self.repo_tree_sha256}",
            f"Language: {self.language}",
            (
                f"Test Runner: {self.test_runner.framework} | command={self.test_runner.command_hint} "
                f"| pythonpath={list(self.test_runner.pythonpath)} | testpaths={list(self.test_runner.testpaths)}"
            ),
            "",
            "Tracked Files:",
            *[f"- {path}" for path in self.tracked_files],
            "",
            "Existing Test Files (HARD GATE - DO NOT MODIFY):",
            *([f"- {path}" for path in self.existing_test_files] or ["- (none)"]),
            "",
            "Protected Config Paths (DO NOT MODIFY unless allow_config_changes=True):",
            *([f"- {path}" for path in self.protected_config_paths] or ["- (none)"]),
            "",
            "Repository File Details:",
        ]
        for summary in self.file_contexts[:max_files]:
            lines.append(f"### {summary.path} [{summary.role}, {summary.line_count} lines]")
            if summary.symbols:
                lines.append("Symbols:")
                for sym in summary.symbols:
                    doc = f" — {sym.docstring_summary}" if sym.docstring_summary else ""
                    lines.append(f"  - {sym.kind} {sym.signature} (L{sym.line_start}-L{sym.line_end}){doc}")
            if summary.excerpt:
                lines.append("```")
                lines.append(summary.excerpt.rstrip())
                lines.append("```")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RepoContext":
        return cls(
            schema_version=str(data.get("schema_version") or SCHEMA_VERSION),
            context_id=data["context_id"],
            captured_at=data["captured_at"],
            repo_name=data["repo_name"],
            repo_source=data["repo_source"],
            repo_commit_sha=data["repo_commit_sha"],
            commit_source=data["commit_source"],
            repo_tree_sha256=data["repo_tree_sha256"],
            is_dirty=bool(data["is_dirty"]),
            language=data["language"],
            tracked_files=tuple(data.get("tracked_files", ())),
            existing_test_files=tuple(data.get("existing_test_files", ())),
            protected_config_paths=tuple(data.get("protected_config_paths", ())),
            protected_config_hashes=dict(data.get("protected_config_hashes", {})),
            test_runner=TestRunnerInfo.from_dict(data["test_runner"]),
            file_contexts=tuple(RepoFileSummary.from_dict(item) for item in data.get("file_contexts", ())),
        )

    @classmethod
    def from_json(cls, raw: str) -> "RepoContext":
        return cls.from_dict(json.loads(raw))


@dataclass(frozen=True)
class AcceptanceCriterion:
    """Single testable acceptance criterion defined by the BA specification."""

    ac_id: str
    description: str
    expected_behavior: str
    verification_method: VerificationMethod = "unit_test"
    target_files: tuple[str, ...] = ()
    edge_cases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        cleaned_id = _require_non_empty(self.ac_id, "AcceptanceCriterion.ac_id")
        if not _AC_ID_RE.fullmatch(cleaned_id):
            raise ValueError(f"Invalid ac_id {self.ac_id!r}: expected alphanumeric/dash/underscore ID.")
        object.__setattr__(self, "ac_id", cleaned_id)
        object.__setattr__(self, "description", _require_non_empty(self.description, "AcceptanceCriterion.description"))
        object.__setattr__(
            self,
            "expected_behavior",
            _require_non_empty(self.expected_behavior, "AcceptanceCriterion.expected_behavior"),
        )
        object.__setattr__(self, "target_files", _normalize_path_tuple(self.target_files))
        object.__setattr__(self, "edge_cases", _normalize_str_tuple(self.edge_cases))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AcceptanceCriterion":
        return cls(
            ac_id=data["ac_id"],
            description=data["description"],
            expected_behavior=data["expected_behavior"],
            verification_method=data.get("verification_method", "unit_test"),
            target_files=tuple(data.get("target_files", ())),
            edge_cases=tuple(data.get("edge_cases", ())),
        )


@dataclass(frozen=True)
class ApprovalRecord:
    """Human review decision bound cryptographically to a specific TaskSpec SHA-256."""

    decision: ApprovalDecision
    reviewer: str
    decided_at: str
    approved_spec_sha256: str
    comment: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "reviewer", _require_non_empty(self.reviewer, "ApprovalRecord.reviewer"))
        object.__setattr__(self, "decided_at", _require_non_empty(self.decided_at, "ApprovalRecord.decided_at"))
        object.__setattr__(
            self,
            "approved_spec_sha256",
            _require_sha256(self.approved_spec_sha256, "ApprovalRecord.approved_spec_sha256"),
        )
        object.__setattr__(self, "comment", (self.comment or "").strip())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ApprovalRecord":
        return cls(
            decision=data["decision"],
            reviewer=data["reviewer"],
            decided_at=data["decided_at"],
            approved_spec_sha256=data["approved_spec_sha256"],
            comment=str(data.get("comment") or ""),
        )


@dataclass(frozen=True)
class TaskSpec:
    """BA specification & acceptance criteria bound to a repo commit and spec version."""

    spec_id: str
    spec_version: int
    created_at: str
    context_id: str
    repo_name: str
    repo_commit_sha: str
    repo_tree_sha256: str
    user_requirement: str
    task_type: TaskType
    title: str
    summary: str
    problem_statement: str
    scope_in: tuple[str, ...]
    scope_out: tuple[str, ...]
    target_files: tuple[str, ...]
    expected_new_test_files: tuple[str, ...]
    forbidden_files: tuple[str, ...]
    acceptance_criteria: tuple[AcceptanceCriterion, ...]
    constraints: tuple[str, ...]
    author_agent: str
    model_id: str
    spec_sha256: str
    expected_new_files: tuple[str, ...] = ()
    tdd_exempt: bool = False
    allow_config_changes: bool = False
    author_role: str = "ba"
    status: SpecStatus = "draft"
    approval: ApprovalRecord | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _require_non_empty(self.schema_version, "TaskSpec.schema_version"))
        object.__setattr__(self, "spec_id", _require_non_empty(self.spec_id, "TaskSpec.spec_id"))
        if isinstance(self.spec_version, bool) or not isinstance(self.spec_version, int) or self.spec_version < 1:
            raise ValueError(f"`spec_version` must be an int >= 1, got {self.spec_version!r}")
        object.__setattr__(self, "created_at", _require_non_empty(self.created_at, "TaskSpec.created_at"))
        object.__setattr__(self, "context_id", _require_non_empty(self.context_id, "TaskSpec.context_id"))
        object.__setattr__(self, "repo_name", _require_non_empty(self.repo_name, "TaskSpec.repo_name"))
        object.__setattr__(self, "repo_commit_sha", _require_non_empty(self.repo_commit_sha, "TaskSpec.repo_commit_sha"))
        object.__setattr__(self, "repo_tree_sha256", _require_sha256(self.repo_tree_sha256, "TaskSpec.repo_tree_sha256"))
        object.__setattr__(self, "user_requirement", _require_non_empty(self.user_requirement, "TaskSpec.user_requirement"))
        object.__setattr__(self, "title", _require_non_empty(self.title, "TaskSpec.title"))
        object.__setattr__(self, "summary", _require_non_empty(self.summary, "TaskSpec.summary"))
        object.__setattr__(self, "problem_statement", _require_non_empty(self.problem_statement, "TaskSpec.problem_statement"))
        object.__setattr__(self, "scope_in", _normalize_str_tuple(self.scope_in))
        object.__setattr__(self, "scope_out", _normalize_str_tuple(self.scope_out))
        object.__setattr__(self, "target_files", _normalize_path_tuple(self.target_files))
        object.__setattr__(self, "expected_new_files", _normalize_path_tuple(self.expected_new_files))
        object.__setattr__(self, "expected_new_test_files", _normalize_path_tuple(self.expected_new_test_files))
        object.__setattr__(self, "forbidden_files", tuple(sorted(_normalize_path_tuple(self.forbidden_files))))
        ac_tuple = tuple(self.acceptance_criteria)
        if not ac_tuple:
            raise ValueError("TaskSpec requires at least one acceptance criterion.")
        seen_ac_ids: set[str] = set()
        for ac in ac_tuple:
            if ac.ac_id in seen_ac_ids:
                raise ValueError(f"Duplicate acceptance criterion ID: {ac.ac_id!r}")
            seen_ac_ids.add(ac.ac_id)
        object.__setattr__(self, "acceptance_criteria", ac_tuple)
        object.__setattr__(self, "constraints", _normalize_str_tuple(self.constraints))
        object.__setattr__(self, "author_role", _require_non_empty(self.author_role, "TaskSpec.author_role"))
        object.__setattr__(self, "author_agent", _require_non_empty(self.author_agent, "TaskSpec.author_agent"))
        object.__setattr__(self, "model_id", _require_non_empty(self.model_id, "TaskSpec.model_id"))

        expected_sha = self.compute_spec_sha256()
        provided_sha = _require_sha256(self.spec_sha256, "TaskSpec.spec_sha256")
        if provided_sha != expected_sha:
            raise ValueError(
                f"TaskSpec integrity mismatch: spec_sha256={provided_sha} != computed={expected_sha}."
            )
        object.__setattr__(self, "spec_sha256", expected_sha)

        if self.approval is not None:
            if self.approval.approved_spec_sha256 != expected_sha:
                raise ValueError("ApprovalRecord.approved_spec_sha256 does not match TaskSpec.spec_sha256.")
            if self.approval.decision == "approved" and self.status != "approved":
                raise ValueError("TaskSpec with approved ApprovalRecord must have status='approved'.")
            if self.approval.decision == "rejected" and self.status != "rejected":
                raise ValueError("TaskSpec with rejected ApprovalRecord must have status='rejected'.")
        elif self.status == "approved":
            raise ValueError("TaskSpec with status='approved' requires an ApprovalRecord.")

    def canonical_content_payload(self) -> dict[str, Any]:
        """Return immutable specification payload covered by `spec_sha256`."""
        return {
            "schema_version": self.schema_version,
            "spec_id": self.spec_id,
            "spec_version": self.spec_version,
            "created_at": self.created_at,
            "context_id": self.context_id,
            "repo_name": self.repo_name,
            "repo_commit_sha": self.repo_commit_sha,
            "repo_tree_sha256": self.repo_tree_sha256,
            "user_requirement": self.user_requirement,
            "task_type": self.task_type,
            "title": self.title,
            "summary": self.summary,
            "problem_statement": self.problem_statement,
            "scope_in": list(self.scope_in),
            "scope_out": list(self.scope_out),
            "target_files": list(self.target_files),
            "expected_new_files": list(self.expected_new_files),
            "expected_new_test_files": list(self.expected_new_test_files),
            "forbidden_files": list(self.forbidden_files),
            "acceptance_criteria": [asdict(ac) for ac in self.acceptance_criteria],
            "constraints": list(self.constraints),
            "tdd_exempt": self.tdd_exempt,
            "allow_config_changes": self.allow_config_changes,
            "author_role": self.author_role,
            "author_agent": self.author_agent,
            "model_id": self.model_id,
        }

    def compute_spec_sha256(self) -> str:
        """Compute deterministic SHA-256 over the immutable TaskSpec payload."""
        return sha256_hex(canonical_json_bytes(self.canonical_content_payload()))

    @property
    def is_approved(self) -> bool:
        return (
            self.status == "approved"
            and self.approval is not None
            and self.approval.decision == "approved"
            and self.approval.approved_spec_sha256 == self.spec_sha256
        )

    @classmethod
    def create(
        cls,
        *,
        repo_context: RepoContext,
        user_requirement: str,
        task_type: TaskType,
        title: str,
        summary: str,
        problem_statement: str,
        scope_in: Sequence[str],
        scope_out: Sequence[str],
        target_files: Sequence[str],
        expected_new_test_files: Sequence[str],
        acceptance_criteria: Sequence[AcceptanceCriterion],
        constraints: Sequence[str] = (),
        expected_new_files: Sequence[str] = (),
        forbidden_files: Sequence[str] = (),
        tdd_exempt: bool = False,
        allow_config_changes: bool = False,
        author_role: str = "ba",
        author_agent: str = "metagpt:ProductManager",
        model_id: str,
        spec_id: str | None = None,
        spec_version: int = 1,
        created_at: str | None = None,
    ) -> "TaskSpec":
        """Create a draft `TaskSpec` bound to `repo_context` with auto-computed `spec_sha256`."""
        resolved_spec_id = spec_id or f"spec-{_slugify(title)}-{secrets.token_hex(4)}"
        resolved_created_at = created_at or utc_now_iso()
        merged_forbidden: set[str] = set(_normalize_path_tuple(forbidden_files))
        merged_forbidden.update(repo_context.existing_test_files)
        if not allow_config_changes:
            merged_forbidden.update(repo_context.protected_config_paths)

        draft_fields = {
            "schema_version": SCHEMA_VERSION,
            "spec_id": resolved_spec_id,
            "spec_version": spec_version,
            "created_at": resolved_created_at,
            "context_id": repo_context.context_id,
            "repo_name": repo_context.repo_name,
            "repo_commit_sha": repo_context.repo_commit_sha,
            "repo_tree_sha256": repo_context.repo_tree_sha256,
            "user_requirement": _require_non_empty(user_requirement, "user_requirement"),
            "task_type": task_type,
            "title": _require_non_empty(title, "title"),
            "summary": _require_non_empty(summary, "summary"),
            "problem_statement": _require_non_empty(problem_statement, "problem_statement"),
            "scope_in": list(_normalize_str_tuple(scope_in)),
            "scope_out": list(_normalize_str_tuple(scope_out)),
            "target_files": list(_normalize_path_tuple(target_files)),
            "expected_new_files": list(_normalize_path_tuple(expected_new_files)),
            "expected_new_test_files": list(_normalize_path_tuple(expected_new_test_files)),
            "forbidden_files": sorted(merged_forbidden),
            "acceptance_criteria": [asdict(ac) for ac in acceptance_criteria],
            "constraints": list(_normalize_str_tuple(constraints)),
            "tdd_exempt": bool(tdd_exempt),
            "allow_config_changes": bool(allow_config_changes),
            "author_role": _require_non_empty(author_role, "author_role"),
            "author_agent": _require_non_empty(author_agent, "author_agent"),
            "model_id": _require_non_empty(model_id, "model_id"),
        }
        digest = sha256_hex(canonical_json_bytes(draft_fields))
        return cls(
            spec_id=resolved_spec_id,
            spec_version=spec_version,
            created_at=resolved_created_at,
            context_id=repo_context.context_id,
            repo_name=repo_context.repo_name,
            repo_commit_sha=repo_context.repo_commit_sha,
            repo_tree_sha256=repo_context.repo_tree_sha256,
            user_requirement=user_requirement,
            task_type=task_type,
            title=title,
            summary=summary,
            problem_statement=problem_statement,
            scope_in=tuple(scope_in),
            scope_out=tuple(scope_out),
            target_files=tuple(target_files),
            expected_new_files=tuple(expected_new_files),
            expected_new_test_files=tuple(expected_new_test_files),
            forbidden_files=tuple(sorted(merged_forbidden)),
            acceptance_criteria=tuple(acceptance_criteria),
            constraints=tuple(constraints),
            tdd_exempt=bool(tdd_exempt),
            allow_config_changes=bool(allow_config_changes),
            author_role=author_role,
            author_agent=author_agent,
            model_id=model_id,
            spec_sha256=digest,
            status="draft",
            approval=None,
            schema_version=SCHEMA_VERSION,
        )

    def to_dev_task_prompt(self) -> str:
        """Format an approved `TaskSpec` into a concrete prompt for the Dev agent."""
        lines = [
            f"TaskSpec: {self.spec_id} v{self.spec_version} (sha256={self.spec_sha256[:12]})",
            f"Target Repo Commit: {self.repo_commit_sha} (tree={self.repo_tree_sha256[:12]})",
            f"Title: {self.title}",
            f"Summary: {self.summary}",
            "",
            "Problem Statement:",
            self.problem_statement,
            "",
            "Target Files to Modify:",
            *([f"- {p}" for p in self.target_files] or ["- (none)"]),
        ]
        if self.expected_new_files:
            lines.extend(["", "Expected New Files:", *[f"- {p}" for p in self.expected_new_files]])
        if self.expected_new_test_files:
            lines.extend(
                ["", "New Test File(s) to Add (NEVER edit existing tests):", *[f"- {p}" for p in self.expected_new_test_files]]
            )
        lines.extend(["", "Forbidden Paths (DO NOT MODIFY):", *([f"- {p}" for p in self.forbidden_files] or ["- (none)"])])
        lines.extend(["", "Acceptance Criteria:"])
        for ac in self.acceptance_criteria:
            files_part = f" [files: {', '.join(ac.target_files)}]" if ac.target_files else ""
            lines.append(f"- {ac.ac_id} ({ac.verification_method}){files_part}: {ac.description}")
            lines.append(f"  Expected: {ac.expected_behavior}")
            if ac.edge_cases:
                lines.append(f"  Edge cases: {'; '.join(ac.edge_cases)}")
        if self.constraints:
            lines.extend(["", "Constraints:", *[f"- {c}" for c in self.constraints]])
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskSpec":
        approval_raw = data.get("approval")
        return cls(
            schema_version=str(data.get("schema_version") or SCHEMA_VERSION),
            spec_id=data["spec_id"],
            spec_version=int(data["spec_version"]),
            created_at=data["created_at"],
            context_id=data["context_id"],
            repo_name=data["repo_name"],
            repo_commit_sha=data["repo_commit_sha"],
            repo_tree_sha256=data["repo_tree_sha256"],
            user_requirement=data["user_requirement"],
            task_type=data["task_type"],
            title=data["title"],
            summary=data["summary"],
            problem_statement=data["problem_statement"],
            scope_in=tuple(data.get("scope_in", ())),
            scope_out=tuple(data.get("scope_out", ())),
            target_files=tuple(data.get("target_files", ())),
            expected_new_files=tuple(data.get("expected_new_files", ())),
            expected_new_test_files=tuple(data.get("expected_new_test_files", ())),
            forbidden_files=tuple(data.get("forbidden_files", ())),
            acceptance_criteria=tuple(
                AcceptanceCriterion.from_dict(item) for item in data.get("acceptance_criteria", ())
            ),
            constraints=tuple(data.get("constraints", ())),
            tdd_exempt=bool(data.get("tdd_exempt", False)),
            allow_config_changes=bool(data.get("allow_config_changes", False)),
            author_role=str(data.get("author_role") or "ba"),
            author_agent=data["author_agent"],
            model_id=data["model_id"],
            status=data.get("status", "draft"),
            approval=ApprovalRecord.from_dict(approval_raw) if approval_raw is not None else None,
            spec_sha256=data["spec_sha256"],
        )

    @classmethod
    def from_json(cls, raw: str) -> "TaskSpec":
        return cls.from_dict(json.loads(raw))


def validate_task_spec_against_repo(spec: TaskSpec, repo_context: RepoContext) -> list[str]:
    """Validate that `spec` binds accurately to `repo_context` and obeys Hard Gate rules."""
    errors: list[str] = []
    if spec.context_id != repo_context.context_id:
        errors.append(
            f"context_id mismatch: spec={spec.context_id!r} != repo_context={repo_context.context_id!r}."
        )
    if spec.repo_commit_sha != repo_context.repo_commit_sha:
        errors.append(
            f"repo_commit_sha mismatch: spec={spec.repo_commit_sha!r} != repo_context={repo_context.repo_commit_sha!r}."
        )
    if spec.repo_tree_sha256 != repo_context.repo_tree_sha256:
        errors.append(
            f"repo_tree_sha256 mismatch: spec={spec.repo_tree_sha256!r} != repo_context={repo_context.repo_tree_sha256!r}."
        )
    if spec.spec_sha256 != spec.compute_spec_sha256():
        errors.append("TaskSpec spec_sha256 does not match its content digest.")

    tracked_set = set(repo_context.tracked_files)
    existing_tests = set(repo_context.existing_test_files)
    protected_configs = set(repo_context.protected_config_paths)

    if not spec.target_files and not spec.expected_new_files:
        errors.append("TaskSpec must specify at least one file in `target_files` or `expected_new_files`.")

    for path in spec.target_files:
        if path not in tracked_set:
            errors.append(f"target_files path does not exist in RepoContext: {path!r}.")
        if path in existing_tests:
            errors.append(f"target_files cannot modify existing test file: {path!r}.")
        if not spec.allow_config_changes and (path in protected_configs or is_protected_config_path(path)):
            errors.append(f"target_files cannot modify protected config file: {path!r}.")

    for path in spec.expected_new_files:
        if path in tracked_set:
            errors.append(f"expected_new_files path already exists in RepoContext: {path!r}.")
        if not spec.allow_config_changes and is_protected_config_path(path):
            errors.append(f"expected_new_files cannot create protected config file: {path!r}.")

    if not spec.tdd_exempt and not spec.expected_new_test_files:
        errors.append("TaskSpec with tdd_exempt=False must propose at least one file in `expected_new_test_files`.")

    for test_path in spec.expected_new_test_files:
        if test_path in tracked_set or test_path in existing_tests:
            errors.append(f"expected_new_test_files must not collide with existing repo file: {test_path!r}.")
        if not is_valid_new_test_file_path(test_path):
            errors.append(
                f"expected_new_test_files entry {test_path!r} must be a valid new test module "
                "(e.g. `tests/test_<name>.py`)."
            )

    for ac in spec.acceptance_criteria:
        for ac_path in ac.target_files:
            if ac_path not in tracked_set and ac_path not in spec.expected_new_files:
                errors.append(
                    f"AcceptanceCriterion {ac.ac_id!r} references unknown target file {ac_path!r}."
                )
            if ac_path in existing_tests:
                errors.append(
                    f"AcceptanceCriterion {ac.ac_id!r} targets existing test file {ac_path!r} (forbidden)."
                )

    for existing_test in existing_tests:
        if existing_test not in spec.forbidden_files:
            errors.append(f"Existing test file {existing_test!r} must be listed in TaskSpec.forbidden_files.")

    if not spec.allow_config_changes:
        for cfg in protected_configs:
            if cfg not in spec.forbidden_files:
                errors.append(f"Protected config file {cfg!r} must be listed in TaskSpec.forbidden_files.")

    return errors


def approve_task_spec(
    spec: TaskSpec,
    *,
    reviewer: str,
    comment: str = "",
    decided_at: str | None = None,
) -> TaskSpec:
    """Return an approved copy of `spec` bound to `spec.spec_sha256`."""
    if spec.status in ("rejected", "superseded"):
        raise ValueError(f"Cannot approve TaskSpec in status={spec.status!r}; create a revised spec version instead.")
    record = ApprovalRecord(
        decision="approved",
        reviewer=reviewer,
        decided_at=decided_at or utc_now_iso(),
        approved_spec_sha256=spec.spec_sha256,
        comment=comment,
    )
    return TaskSpec(
        schema_version=spec.schema_version,
        spec_id=spec.spec_id,
        spec_version=spec.spec_version,
        created_at=spec.created_at,
        context_id=spec.context_id,
        repo_name=spec.repo_name,
        repo_commit_sha=spec.repo_commit_sha,
        repo_tree_sha256=spec.repo_tree_sha256,
        user_requirement=spec.user_requirement,
        task_type=spec.task_type,
        title=spec.title,
        summary=spec.summary,
        problem_statement=spec.problem_statement,
        scope_in=spec.scope_in,
        scope_out=spec.scope_out,
        target_files=spec.target_files,
        expected_new_files=spec.expected_new_files,
        expected_new_test_files=spec.expected_new_test_files,
        forbidden_files=spec.forbidden_files,
        acceptance_criteria=spec.acceptance_criteria,
        constraints=spec.constraints,
        tdd_exempt=spec.tdd_exempt,
        allow_config_changes=spec.allow_config_changes,
        author_role=spec.author_role,
        author_agent=spec.author_agent,
        model_id=spec.model_id,
        status="approved",
        approval=record,
        spec_sha256=spec.spec_sha256,
    )


def reject_task_spec(
    spec: TaskSpec,
    *,
    reviewer: str,
    comment: str,
    decided_at: str | None = None,
) -> TaskSpec:
    """Return a rejected copy of `spec` with mandatory human feedback."""
    reason = _require_non_empty(comment, "comment")
    record = ApprovalRecord(
        decision="rejected",
        reviewer=reviewer,
        decided_at=decided_at or utc_now_iso(),
        approved_spec_sha256=spec.spec_sha256,
        comment=reason,
    )
    return TaskSpec(
        schema_version=spec.schema_version,
        spec_id=spec.spec_id,
        spec_version=spec.spec_version,
        created_at=spec.created_at,
        context_id=spec.context_id,
        repo_name=spec.repo_name,
        repo_commit_sha=spec.repo_commit_sha,
        repo_tree_sha256=spec.repo_tree_sha256,
        user_requirement=spec.user_requirement,
        task_type=spec.task_type,
        title=spec.title,
        summary=spec.summary,
        problem_statement=spec.problem_statement,
        scope_in=spec.scope_in,
        scope_out=spec.scope_out,
        target_files=spec.target_files,
        expected_new_files=spec.expected_new_files,
        expected_new_test_files=spec.expected_new_test_files,
        forbidden_files=spec.forbidden_files,
        acceptance_criteria=spec.acceptance_criteria,
        constraints=spec.constraints,
        tdd_exempt=spec.tdd_exempt,
        allow_config_changes=spec.allow_config_changes,
        author_role=spec.author_role,
        author_agent=spec.author_agent,
        model_id=spec.model_id,
        status="rejected",
        approval=record,
        spec_sha256=spec.spec_sha256,
    )


def revise_task_spec(
    previous: TaskSpec,
    *,
    repo_context: RepoContext,
    user_requirement: str | None = None,
    task_type: TaskType | None = None,
    title: str | None = None,
    summary: str | None = None,
    problem_statement: str | None = None,
    scope_in: Sequence[str] | None = None,
    scope_out: Sequence[str] | None = None,
    target_files: Sequence[str] | None = None,
    expected_new_files: Sequence[str] | None = None,
    expected_new_test_files: Sequence[str] | None = None,
    acceptance_criteria: Sequence[AcceptanceCriterion] | None = None,
    constraints: Sequence[str] | None = None,
    tdd_exempt: bool | None = None,
    allow_config_changes: bool | None = None,
    model_id: str | None = None,
    author_agent: str | None = None,
) -> TaskSpec:
    """Produce `spec_version + 1` preserving `spec_id` and clearing prior approval."""
    resolved_allow_cfg = (
        allow_config_changes if allow_config_changes is not None else previous.allow_config_changes
    )
    inherited_forbidden = () if resolved_allow_cfg != previous.allow_config_changes else previous.forbidden_files
    return TaskSpec.create(
        repo_context=repo_context,
        user_requirement=user_requirement if user_requirement is not None else previous.user_requirement,
        task_type=task_type if task_type is not None else previous.task_type,
        title=title if title is not None else previous.title,
        summary=summary if summary is not None else previous.summary,
        problem_statement=problem_statement if problem_statement is not None else previous.problem_statement,
        scope_in=scope_in if scope_in is not None else previous.scope_in,
        scope_out=scope_out if scope_out is not None else previous.scope_out,
        target_files=target_files if target_files is not None else previous.target_files,
        expected_new_files=expected_new_files if expected_new_files is not None else previous.expected_new_files,
        expected_new_test_files=(
            expected_new_test_files if expected_new_test_files is not None else previous.expected_new_test_files
        ),
        acceptance_criteria=acceptance_criteria if acceptance_criteria is not None else previous.acceptance_criteria,
        constraints=constraints if constraints is not None else previous.constraints,
        forbidden_files=inherited_forbidden,
        tdd_exempt=tdd_exempt if tdd_exempt is not None else previous.tdd_exempt,
        allow_config_changes=resolved_allow_cfg,
        author_role=previous.author_role,
        author_agent=author_agent if author_agent is not None else previous.author_agent,
        model_id=model_id if model_id is not None else previous.model_id,
        spec_id=previous.spec_id,
        spec_version=previous.spec_version + 1,
    )


def format_task_spec_for_review(spec: TaskSpec) -> str:
    """Render a human-readable Markdown view of `TaskSpec` for approval."""
    lines = [
        f"# TaskSpec: {spec.title}",
        "",
        f"- **Spec ID / Version:** `{spec.spec_id}` v`{spec.spec_version}` (`{spec.status}`)",
        f"- **Spec SHA-256:** `{spec.spec_sha256}`",
        f"- **Repo / Commit:** `{spec.repo_name}` @ `{spec.repo_commit_sha}` (tree `{spec.repo_tree_sha256[:12]}`)",
        f"- **Author / Model:** `{spec.author_agent}` via `{spec.model_id}`",
        f"- **Task Type:** `{spec.task_type}` | `tdd_exempt={spec.tdd_exempt}` | `allow_config_changes={spec.allow_config_changes}`",
        "",
        "## User Requirement",
        spec.user_requirement,
        "",
        "## Summary",
        spec.summary,
        "",
        "## Problem Statement",
        spec.problem_statement,
        "",
        "## Scope",
        "- **In scope:** " + ("; ".join(spec.scope_in) if spec.scope_in else "(none)"),
        "- **Out of scope:** " + ("; ".join(spec.scope_out) if spec.scope_out else "(none)"),
        "",
        "## Files",
        "- **Target files:** " + (", ".join(f"`{p}`" for p in spec.target_files) or "(none)"),
        "- **Expected new files:** " + (", ".join(f"`{p}`" for p in spec.expected_new_files) or "(none)"),
        "- **Expected new test files:** " + (", ".join(f"`{p}`" for p in spec.expected_new_test_files) or "(none)"),
        "- **Forbidden files:** " + (", ".join(f"`{p}`" for p in spec.forbidden_files) or "(none)"),
        "",
        "## Acceptance Criteria",
    ]
    for ac in spec.acceptance_criteria:
        files_text = ", ".join(f"`{p}`" for p in ac.target_files) or "(repo)"
        lines.append(f"- **{ac.ac_id}** (`{ac.verification_method}`, {files_text}): {ac.description}")
        lines.append(f"  - Expected: {ac.expected_behavior}")
        if ac.edge_cases:
            lines.append(f"  - Edge cases: {'; '.join(ac.edge_cases)}")
    if spec.constraints:
        lines.extend(["", "## Constraints", *[f"- {item}" for item in spec.constraints]])
    if spec.approval is not None:
        lines.extend(
            [
                "",
                "## Approval",
                f"- Decision: `{spec.approval.decision}` by `{spec.approval.reviewer}` at `{spec.approval.decided_at}`",
                f"- Comment: {spec.approval.comment or '(none)'}",
            ]
        )
    return "\n".join(lines)


@dataclass(frozen=True)
class CritiqueIssue:
    """Single QA critique finding on a `TaskSpec`."""

    severity: CritiqueSeverity
    message: str
    ac_id: str | None = None
    suggestion: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "message", _require_non_empty(self.message, "CritiqueIssue.message"))
        if self.ac_id is not None:
            cleaned = _require_non_empty(self.ac_id, "CritiqueIssue.ac_id")
            if not _AC_ID_RE.fullmatch(cleaned):
                raise ValueError(f"Invalid ac_id {self.ac_id!r}")
            object.__setattr__(self, "ac_id", cleaned)
        object.__setattr__(self, "suggestion", (self.suggestion or "").strip())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CritiqueIssue":
        return cls(
            severity=data["severity"],
            message=data["message"],
            ac_id=data.get("ac_id"),
            suggestion=str(data.get("suggestion") or ""),
        )


@dataclass(frozen=True)
class SpecCritique:
    """QA critique summary produced before/alongside `TestPlan`."""

    verdict: CritiqueVerdict
    summary: str
    issues: tuple[CritiqueIssue, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "summary", _require_non_empty(self.summary, "SpecCritique.summary"))
        object.__setattr__(self, "issues", tuple(self.issues))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SpecCritique":
        return cls(
            verdict=data["verdict"],
            summary=data["summary"],
            issues=tuple(CritiqueIssue.from_dict(item) for item in data.get("issues", ())),
        )


@dataclass(frozen=True)
class TestCaseSpec:
    """Single QA test case linked to one or more acceptance criteria."""

    __test__ = False

    test_case_id: str
    ac_ids: tuple[str, ...]
    title: str
    test_type: TestCaseType
    proposed_test_file: str
    steps_or_inputs: str
    expected_outcome: str
    target_symbol: str | None = None
    preconditions: tuple[str, ...] = ()
    must_fail_on_baseline: bool = True

    def __post_init__(self) -> None:
        cleaned_id = _require_non_empty(self.test_case_id, "TestCaseSpec.test_case_id")
        if not _TC_ID_RE.fullmatch(cleaned_id):
            raise ValueError(f"Invalid test_case_id {self.test_case_id!r}")
        object.__setattr__(self, "test_case_id", cleaned_id)
        ac_ids = _normalize_str_tuple(self.ac_ids)
        if not ac_ids:
            raise ValueError(f"TestCaseSpec {cleaned_id!r} must link to at least one ac_id.")
        object.__setattr__(self, "ac_ids", ac_ids)
        object.__setattr__(self, "title", _require_non_empty(self.title, "TestCaseSpec.title"))
        object.__setattr__(self, "proposed_test_file", normalize_repo_rel_path(self.proposed_test_file))
        object.__setattr__(self, "steps_or_inputs", _require_non_empty(self.steps_or_inputs, "TestCaseSpec.steps_or_inputs"))
        object.__setattr__(
            self,
            "expected_outcome",
            _require_non_empty(self.expected_outcome, "TestCaseSpec.expected_outcome"),
        )
        if self.target_symbol is not None:
            object.__setattr__(self, "target_symbol", _require_non_empty(self.target_symbol, "TestCaseSpec.target_symbol"))
        object.__setattr__(self, "preconditions", _normalize_str_tuple(self.preconditions))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TestCaseSpec":
        return cls(
            test_case_id=data["test_case_id"],
            ac_ids=tuple(data.get("ac_ids", ())),
            title=data["title"],
            test_type=data["test_type"],
            proposed_test_file=data["proposed_test_file"],
            steps_or_inputs=data["steps_or_inputs"],
            expected_outcome=data["expected_outcome"],
            target_symbol=data.get("target_symbol"),
            preconditions=tuple(data.get("preconditions", ())),
            must_fail_on_baseline=bool(data.get("must_fail_on_baseline", True)),
        )


@dataclass(frozen=True)
class TestPlan:
    """QA critique + test cases linked to a specific commit and `TaskSpec` version."""

    __test__ = False

    plan_id: str
    created_at: str
    repo_commit_sha: str
    repo_tree_sha256: str
    spec_id: str
    spec_version: int
    spec_sha256: str
    qa_agent: str
    model_id: str
    spec_critique: SpecCritique
    test_cases: tuple[TestCaseSpec, ...]
    qa_role: str = "qa"
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _require_non_empty(self.schema_version, "TestPlan.schema_version"))
        object.__setattr__(self, "plan_id", _require_non_empty(self.plan_id, "TestPlan.plan_id"))
        object.__setattr__(self, "created_at", _require_non_empty(self.created_at, "TestPlan.created_at"))
        object.__setattr__(self, "repo_commit_sha", _require_non_empty(self.repo_commit_sha, "TestPlan.repo_commit_sha"))
        object.__setattr__(self, "repo_tree_sha256", _require_sha256(self.repo_tree_sha256, "TestPlan.repo_tree_sha256"))
        object.__setattr__(self, "spec_id", _require_non_empty(self.spec_id, "TestPlan.spec_id"))
        if isinstance(self.spec_version, bool) or not isinstance(self.spec_version, int) or self.spec_version < 1:
            raise ValueError(f"`spec_version` must be an int >= 1, got {self.spec_version!r}")
        object.__setattr__(self, "spec_sha256", _require_sha256(self.spec_sha256, "TestPlan.spec_sha256"))
        object.__setattr__(self, "qa_role", _require_non_empty(self.qa_role, "TestPlan.qa_role"))
        object.__setattr__(self, "qa_agent", _require_non_empty(self.qa_agent, "TestPlan.qa_agent"))
        object.__setattr__(self, "model_id", _require_non_empty(self.model_id, "TestPlan.model_id"))
        cases = tuple(self.test_cases)
        seen_ids: set[str] = set()
        for tc in cases:
            if tc.test_case_id in seen_ids:
                raise ValueError(f"Duplicate test_case_id: {tc.test_case_id!r}")
            seen_ids.add(tc.test_case_id)
        object.__setattr__(self, "test_cases", cases)

    @classmethod
    def create(
        cls,
        *,
        task_spec: TaskSpec,
        spec_critique: SpecCritique,
        test_cases: Sequence[TestCaseSpec],
        qa_agent: str = "chatdev:QA",
        model_id: str,
        qa_role: str = "qa",
        plan_id: str | None = None,
        created_at: str | None = None,
    ) -> "TestPlan":
        return cls(
            plan_id=plan_id or f"plan-{task_spec.spec_id}-v{task_spec.spec_version}-{secrets.token_hex(3)}",
            created_at=created_at or utc_now_iso(),
            repo_commit_sha=task_spec.repo_commit_sha,
            repo_tree_sha256=task_spec.repo_tree_sha256,
            spec_id=task_spec.spec_id,
            spec_version=task_spec.spec_version,
            spec_sha256=task_spec.spec_sha256,
            qa_role=qa_role,
            qa_agent=qa_agent,
            model_id=model_id,
            spec_critique=spec_critique,
            test_cases=tuple(test_cases),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TestPlan":
        return cls(
            schema_version=str(data.get("schema_version") or SCHEMA_VERSION),
            plan_id=data["plan_id"],
            created_at=data["created_at"],
            repo_commit_sha=data["repo_commit_sha"],
            repo_tree_sha256=data["repo_tree_sha256"],
            spec_id=data["spec_id"],
            spec_version=int(data["spec_version"]),
            spec_sha256=data["spec_sha256"],
            qa_role=str(data.get("qa_role") or "qa"),
            qa_agent=data["qa_agent"],
            model_id=data["model_id"],
            spec_critique=SpecCritique.from_dict(data["spec_critique"]),
            test_cases=tuple(TestCaseSpec.from_dict(item) for item in data.get("test_cases", ())),
        )

    @classmethod
    def from_json(cls, raw: str) -> "TestPlan":
        return cls.from_dict(json.loads(raw))


def validate_test_plan_against_spec(
    plan: TestPlan,
    spec: TaskSpec,
    repo_context: RepoContext | None = None,
) -> list[str]:
    """Validate that `plan` binds to `spec` (and `repo_context`), with valid AC coverage."""
    errors: list[str] = []
    if plan.repo_commit_sha != spec.repo_commit_sha:
        errors.append(f"repo_commit_sha mismatch: plan={plan.repo_commit_sha!r} != spec={spec.repo_commit_sha!r}.")
    if plan.repo_tree_sha256 != spec.repo_tree_sha256:
        errors.append(f"repo_tree_sha256 mismatch: plan={plan.repo_tree_sha256!r} != spec={spec.repo_tree_sha256!r}.")
    if plan.spec_id != spec.spec_id:
        errors.append(f"spec_id mismatch: plan={plan.spec_id!r} != spec={spec.spec_id!r}.")
    if plan.spec_version != spec.spec_version:
        errors.append(f"spec_version mismatch: plan={plan.spec_version} != spec={spec.spec_version}.")
    if plan.spec_sha256 != spec.spec_sha256:
        errors.append(f"spec_sha256 mismatch: plan={plan.spec_sha256!r} != spec={spec.spec_sha256!r}.")

    if repo_context is not None:
        if plan.repo_commit_sha != repo_context.repo_commit_sha:
            errors.append("TestPlan repo_commit_sha does not match RepoContext.")
        if plan.repo_tree_sha256 != repo_context.repo_tree_sha256:
            errors.append("TestPlan repo_tree_sha256 does not match RepoContext.")

    spec_ac_ids = {ac.ac_id for ac in spec.acceptance_criteria}
    covered_ac_ids: set[str] = set()
    existing_tests = set(repo_context.existing_test_files) if repo_context is not None else set(spec.forbidden_files)

    if plan.spec_critique.verdict != "block" and not plan.test_cases:
        errors.append("Non-blocking TestPlan must define at least one TestCaseSpec.")

    for tc in plan.test_cases:
        unknown_acs = [ac_id for ac_id in tc.ac_ids if ac_id not in spec_ac_ids]
        if unknown_acs:
            errors.append(f"TestCase {tc.test_case_id!r} references unknown AC IDs: {unknown_acs!r}.")
        covered_ac_ids.update(ac_id for ac_id in tc.ac_ids if ac_id in spec_ac_ids)

        if tc.proposed_test_file in existing_tests:
            errors.append(
                f"TestCase {tc.test_case_id!r} targets existing test file {tc.proposed_test_file!r} (forbidden)."
            )
        if not is_valid_new_test_file_path(tc.proposed_test_file):
            errors.append(
                f"TestCase {tc.test_case_id!r} proposed_test_file {tc.proposed_test_file!r} "
                "must follow new test file rules (`tests/test_*.py`)."
            )

    if plan.spec_critique.verdict != "block":
        uncovered = sorted(spec_ac_ids - covered_ac_ids)
        if uncovered:
            errors.append(f"Acceptance criteria lack linked test cases: {', '.join(uncovered)}.")

    return errors


@dataclass(frozen=True)
class ACTraceabilityEntry:
    """Traceability row mapping an AcceptanceCriterion to QA tests and observed execution."""

    ac_id: str
    linked_test_case_ids: tuple[str, ...] = ()
    observed_test_ids: tuple[str, ...] = ()
    status: TraceabilityStatus = "unverified"
    notes: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "ac_id", _require_non_empty(self.ac_id, "ACTraceabilityEntry.ac_id"))
        object.__setattr__(self, "linked_test_case_ids", _normalize_str_tuple(self.linked_test_case_ids))
        object.__setattr__(self, "observed_test_ids", _normalize_str_tuple(self.observed_test_ids))
        object.__setattr__(self, "notes", (self.notes or "").strip())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ACTraceabilityEntry":
        return cls(
            ac_id=data["ac_id"],
            linked_test_case_ids=tuple(data.get("linked_test_case_ids", ())),
            observed_test_ids=tuple(data.get("observed_test_ids", ())),
            status=data.get("status", "unverified"),
            notes=str(data.get("notes") or ""),
        )


@dataclass(frozen=True)
class ExecutionReport:
    """Independent verification & traceability report produced after Dev runs in InstaBox."""

    run_id: str
    created_at: str
    repo_commit_sha: str
    repo_tree_sha256: str
    vm_base_commit_sha: str
    post_agent_head_sha: str
    spec_id: str
    spec_version: int
    spec_sha256: str
    dev_model_id: str
    agent_machine_id: str
    verdict: Phase2Verdict
    reasons: tuple[str, ...]
    changed_files: tuple[str, ...]
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
    patch_sha256: str
    test_plan_id: str | None = None
    verify_red_machine_id: str | None = None
    verify_machine_id: str | None = None
    pre_agent_snapshot_id: str | None = None
    red_exit_code: int | None = None
    post_exit_code: int | None = None
    tdd_exempt: bool = False
    tdd_exempt_evidence: str | None = None
    patch_file: str | None = None
    ac_traceability: tuple[ACTraceabilityEntry, ...] = ()
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _require_non_empty(self.schema_version, "ExecutionReport.schema_version"))
        object.__setattr__(self, "run_id", _require_non_empty(self.run_id, "ExecutionReport.run_id"))
        object.__setattr__(self, "created_at", _require_non_empty(self.created_at, "ExecutionReport.created_at"))
        object.__setattr__(self, "repo_commit_sha", _require_non_empty(self.repo_commit_sha, "ExecutionReport.repo_commit_sha"))
        object.__setattr__(self, "repo_tree_sha256", _require_sha256(self.repo_tree_sha256, "ExecutionReport.repo_tree_sha256"))
        object.__setattr__(self, "vm_base_commit_sha", _require_non_empty(self.vm_base_commit_sha, "ExecutionReport.vm_base_commit_sha"))
        object.__setattr__(self, "post_agent_head_sha", _require_non_empty(self.post_agent_head_sha, "ExecutionReport.post_agent_head_sha"))
        object.__setattr__(self, "spec_id", _require_non_empty(self.spec_id, "ExecutionReport.spec_id"))
        if isinstance(self.spec_version, bool) or not isinstance(self.spec_version, int) or self.spec_version < 1:
            raise ValueError(f"`spec_version` must be an int >= 1, got {self.spec_version!r}")
        object.__setattr__(self, "spec_sha256", _require_sha256(self.spec_sha256, "ExecutionReport.spec_sha256"))
        object.__setattr__(self, "dev_model_id", _require_non_empty(self.dev_model_id, "ExecutionReport.dev_model_id"))
        object.__setattr__(self, "agent_machine_id", _require_non_empty(self.agent_machine_id, "ExecutionReport.agent_machine_id"))
        object.__setattr__(self, "patch_sha256", _require_sha256(self.patch_sha256, "ExecutionReport.patch_sha256"))
        object.__setattr__(self, "changed_files", _normalize_path_tuple(self.changed_files))
        object.__setattr__(self, "new_test_files", _normalize_path_tuple(self.new_test_files))
        object.__setattr__(self, "modified_existing_test_files", _normalize_path_tuple(self.modified_existing_test_files))
        object.__setattr__(self, "touched_protected_configs", _normalize_path_tuple(self.touched_protected_configs))
        object.__setattr__(self, "reasons", _normalize_str_tuple(self.reasons))
        object.__setattr__(self, "new_test_ids", tuple(self.new_test_ids))
        object.__setattr__(self, "red_to_green_ids", tuple(self.red_to_green_ids))
        object.__setattr__(self, "regressed_ids", tuple(self.regressed_ids))
        object.__setattr__(self, "missing_baseline_ids", tuple(self.missing_baseline_ids))
        object.__setattr__(self, "post_failing_ids", tuple(self.post_failing_ids))
        object.__setattr__(self, "forbidden_mode_entries", tuple(self.forbidden_mode_entries))
        object.__setattr__(self, "baseline_counts", dict(self.baseline_counts))
        object.__setattr__(self, "post_counts", dict(self.post_counts))
        object.__setattr__(self, "ac_traceability", tuple(self.ac_traceability))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExecutionReport":
        return cls(
            schema_version=str(data.get("schema_version") or SCHEMA_VERSION),
            run_id=data["run_id"],
            created_at=data["created_at"],
            repo_commit_sha=data["repo_commit_sha"],
            repo_tree_sha256=data["repo_tree_sha256"],
            vm_base_commit_sha=data["vm_base_commit_sha"],
            post_agent_head_sha=data["post_agent_head_sha"],
            spec_id=data["spec_id"],
            spec_version=int(data["spec_version"]),
            spec_sha256=data["spec_sha256"],
            test_plan_id=data.get("test_plan_id"),
            dev_model_id=data["dev_model_id"],
            agent_machine_id=data["agent_machine_id"],
            verify_red_machine_id=data.get("verify_red_machine_id"),
            verify_machine_id=data.get("verify_machine_id"),
            pre_agent_snapshot_id=data.get("pre_agent_snapshot_id"),
            verdict=data["verdict"],
            reasons=tuple(data.get("reasons", ())),
            changed_files=tuple(data.get("changed_files", ())),
            new_test_files=tuple(data.get("new_test_files", ())),
            new_test_ids=tuple(data.get("new_test_ids", ())),
            red_to_green_ids=tuple(data.get("red_to_green_ids", ())),
            regressed_ids=tuple(data.get("regressed_ids", ())),
            missing_baseline_ids=tuple(data.get("missing_baseline_ids", ())),
            post_failing_ids=tuple(data.get("post_failing_ids", ())),
            modified_existing_test_files=tuple(data.get("modified_existing_test_files", ())),
            touched_protected_configs=tuple(data.get("touched_protected_configs", ())),
            forbidden_mode_entries=tuple(data.get("forbidden_mode_entries", ())),
            baseline_counts=dict(data.get("baseline_counts", {})),
            post_counts=dict(data.get("post_counts", {})),
            red_exit_code=data.get("red_exit_code"),
            post_exit_code=data.get("post_exit_code"),
            tdd_exempt=bool(data.get("tdd_exempt", False)),
            tdd_exempt_evidence=data.get("tdd_exempt_evidence"),
            patch_sha256=data["patch_sha256"],
            patch_file=data.get("patch_file"),
            ac_traceability=tuple(ACTraceabilityEntry.from_dict(item) for item in data.get("ac_traceability", ())),
        )

    @classmethod
    def from_json(cls, raw: str) -> "ExecutionReport":
        return cls.from_dict(json.loads(raw))


def build_execution_report_from_phase2(
    *,
    run_id: str,
    spec: TaskSpec,
    dev_model_id: str,
    agent_machine_id: str,
    vm_base_commit_sha: str,
    post_agent_head_sha: str,
    verification: Mapping[str, Any] | Any,
    patch_bytes: bytes,
    changed_files: Sequence[str] = (),
    test_plan: TestPlan | None = None,
    verify_red_machine_id: str | None = None,
    verify_machine_id: str | None = None,
    pre_agent_snapshot_id: str | None = None,
    patch_file: str | None = None,
    created_at: str | None = None,
) -> ExecutionReport:
    """Build an `ExecutionReport` from Phase 2 verification output bound to `spec` and optional `test_plan`."""
    verif_dict: Mapping[str, Any] = (
        verification.to_dict() if hasattr(verification, "to_dict") else verification
    )
    red_to_green = tuple(verif_dict.get("red_to_green_ids", ()))
    new_test_ids = tuple(verif_dict.get("new_test_ids", ()))
    post_failing = set(verif_dict.get("post_failing_ids", ()))
    verdict: Phase2Verdict = verif_dict["verdict"]

    ac_to_tcs: dict[str, list[str]] = {ac.ac_id: [] for ac in spec.acceptance_criteria}
    if test_plan is not None:
        for tc in test_plan.test_cases:
            for ac_id in tc.ac_ids:
                if ac_id in ac_to_tcs:
                    ac_to_tcs[ac_id].append(tc.test_case_id)

    traceability: list[ACTraceabilityEntry] = []
    for ac in spec.acceptance_criteria:
        linked_tcs = tuple(ac_to_tcs.get(ac.ac_id, ()))
        if verdict == "REJECT" and post_failing:
            status: TraceabilityStatus = "failed"
        elif verdict == "PASS" and (red_to_green or (spec.tdd_exempt and verif_dict.get("post_exit_code") == 0)):
            status = "verified" if linked_tcs or not test_plan else "partial"
        elif new_test_ids:
            status = "partial"
        else:
            status = "unverified"
        traceability.append(
            ACTraceabilityEntry(
                ac_id=ac.ac_id,
                linked_test_case_ids=linked_tcs,
                observed_test_ids=red_to_green or new_test_ids,
                status=status,
            )
        )

    return ExecutionReport(
        run_id=run_id,
        created_at=created_at or utc_now_iso(),
        repo_commit_sha=spec.repo_commit_sha,
        repo_tree_sha256=spec.repo_tree_sha256,
        vm_base_commit_sha=vm_base_commit_sha,
        post_agent_head_sha=post_agent_head_sha,
        spec_id=spec.spec_id,
        spec_version=spec.spec_version,
        spec_sha256=spec.spec_sha256,
        test_plan_id=test_plan.plan_id if test_plan is not None else None,
        dev_model_id=dev_model_id,
        agent_machine_id=agent_machine_id,
        verify_red_machine_id=verify_red_machine_id,
        verify_machine_id=verify_machine_id,
        pre_agent_snapshot_id=pre_agent_snapshot_id,
        verdict=verdict,
        reasons=tuple(verif_dict.get("reasons", ())),
        changed_files=tuple(changed_files),
        new_test_files=tuple(verif_dict.get("new_test_files", ())),
        new_test_ids=new_test_ids,
        red_to_green_ids=red_to_green,
        regressed_ids=tuple(verif_dict.get("regressed_ids", ())),
        missing_baseline_ids=tuple(verif_dict.get("missing_baseline_ids", ())),
        post_failing_ids=tuple(verif_dict.get("post_failing_ids", ())),
        modified_existing_test_files=tuple(verif_dict.get("modified_existing_test_files", ())),
        touched_protected_configs=tuple(verif_dict.get("touched_protected_configs", ())),
        forbidden_mode_entries=tuple(verif_dict.get("forbidden_mode_entries", ())),
        baseline_counts=dict(verif_dict.get("baseline_counts", {})),
        post_counts=dict(verif_dict.get("post_counts", {})),
        red_exit_code=verif_dict.get("red_exit_code"),
        post_exit_code=verif_dict.get("post_exit_code"),
        tdd_exempt=bool(verif_dict.get("tdd_exempt", False)),
        tdd_exempt_evidence=verif_dict.get("tdd_exempt_evidence"),
        patch_sha256=sha256_hex(patch_bytes),
        patch_file=patch_file,
        ac_traceability=tuple(traceability),
    )


def validate_execution_report_binding(
    report: ExecutionReport,
    spec: TaskSpec,
    test_plan: TestPlan | None = None,
) -> list[str]:
    """Verify that `report` matches the repo commit, tree hash, and spec/test plan versions."""
    errors: list[str] = []
    if not spec.is_approved:
        errors.append(f"TaskSpec {spec.spec_id!r} v{spec.spec_version} is not approved (status={spec.status!r}).")
    if report.repo_commit_sha != spec.repo_commit_sha:
        errors.append("ExecutionReport repo_commit_sha does not match TaskSpec.")
    if report.repo_tree_sha256 != spec.repo_tree_sha256:
        errors.append("ExecutionReport repo_tree_sha256 does not match TaskSpec.")
    if report.spec_id != spec.spec_id:
        errors.append("ExecutionReport spec_id does not match TaskSpec.")
    if report.spec_version != spec.spec_version:
        errors.append("ExecutionReport spec_version does not match TaskSpec.")
    if report.spec_sha256 != spec.spec_sha256:
        errors.append("ExecutionReport spec_sha256 does not match TaskSpec.")
    if test_plan is not None:
        if report.test_plan_id != test_plan.plan_id:
            errors.append("ExecutionReport test_plan_id does not match TestPlan.")
        if test_plan.spec_sha256 != spec.spec_sha256:
            errors.append("TestPlan spec_sha256 does not match TaskSpec.")
    return errors
