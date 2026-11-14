"""SaaS product contracts (v1.1), provenance envelopes, and schema migrations.

Extends `ai_sdlc.contracts` (v1.0) without breaking existing Spike 01/02 interfaces.
"""

from __future__ import annotations

import copy
import json
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Final, Literal

from ai_sdlc.contracts import (
    SCHEMA_VERSION as V1_0_SCHEMA_VERSION,
)
from ai_sdlc.contracts import (
    ACTraceabilityEntry,
    ApprovalRecord,
    ExecutionReport,
    RepoContext,
    TaskSpec,
    TestPlan,
    _require_non_empty,
    _require_sha256,
    canonical_json_bytes,
    sha256_hex,
    utc_now_iso,
)
from ai_sdlc.repo_policy import normalize_repo_rel_path

SAAS_SCHEMA_VERSION: Final[str] = "1.1"
SUPPORTED_SCHEMA_VERSIONS: Final[frozenset[str]] = frozenset({V1_0_SCHEMA_VERSION, SAAS_SCHEMA_VERSION})

VerificationStatus = Literal[
    "PASS",
    "FAIL",
    "NEEDS_REVIEW",
    "BLOCKED_ENVIRONMENT",
    "CANCELLED",
    "INSUFFICIENT_EVIDENCE",
]
ReviewVerdict = Literal["APPROVE", "REQUEST_CHANGES", "BLOCK"]
FindingSeverity = Literal["blocker", "major", "minor", "suggestion"]
FindingStatus = Literal["open", "resolved", "wont_fix", "needs_spec_change"]
ChangeRequestStatus = Literal["open", "accepted", "rejected", "superseded"]


class UnsupportedSchemaVersionError(ValueError):
    """Raised when an artifact payload carries an unsupported schema version."""


@dataclass(frozen=True)
class ArtifactProvenance:
    """Multi-tenant and run-level provenance attached to SaaS v1.1 contracts."""

    tenant_id: str
    project_id: str
    task_id: str
    run_id: str
    attempt_id: str
    policy_version: str = "policy-v1"
    schema_version: str = SAAS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "tenant_id", _require_non_empty(self.tenant_id, "ArtifactProvenance.tenant_id"))
        object.__setattr__(self, "project_id", _require_non_empty(self.project_id, "ArtifactProvenance.project_id"))
        object.__setattr__(self, "task_id", _require_non_empty(self.task_id, "ArtifactProvenance.task_id"))
        object.__setattr__(self, "run_id", _require_non_empty(self.run_id, "ArtifactProvenance.run_id"))
        object.__setattr__(self, "attempt_id", _require_non_empty(self.attempt_id, "ArtifactProvenance.attempt_id"))
        object.__setattr__(
            self, "policy_version", _require_non_empty(self.policy_version, "ArtifactProvenance.policy_version")
        )
        if self.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise UnsupportedSchemaVersionError(
                f"Unsupported schema_version {self.schema_version!r}; supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
            )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ArtifactProvenance:
        return cls(
            tenant_id=data["tenant_id"],
            project_id=data["project_id"],
            task_id=data["task_id"],
            run_id=data["run_id"],
            attempt_id=data["attempt_id"],
            policy_version=str(data.get("policy_version") or "policy-v1"),
            schema_version=str(data.get("schema_version") or SAAS_SCHEMA_VERSION),
        )


@dataclass(frozen=True)
class SubtaskSpec:
    """Single decomposed implementation unit bound to a specific repo & baseline commit."""

    subtask_id: str
    repo_id: str
    repo_name: str
    base_commit_sha: str
    target_branch: str
    title: str
    description: str
    target_files: tuple[str, ...]
    expected_new_test_files: tuple[str, ...]
    depends_on: tuple[str, ...] = ()
    ac_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "subtask_id", _require_non_empty(self.subtask_id, "SubtaskSpec.subtask_id"))
        object.__setattr__(self, "repo_id", _require_non_empty(self.repo_id, "SubtaskSpec.repo_id"))
        object.__setattr__(self, "repo_name", _require_non_empty(self.repo_name, "SubtaskSpec.repo_name"))
        object.__setattr__(
            self, "base_commit_sha", _require_non_empty(self.base_commit_sha, "SubtaskSpec.base_commit_sha")
        )
        object.__setattr__(self, "target_branch", _require_non_empty(self.target_branch, "SubtaskSpec.target_branch"))
        object.__setattr__(self, "title", _require_non_empty(self.title, "SubtaskSpec.title"))
        object.__setattr__(self, "description", _require_non_empty(self.description, "SubtaskSpec.description"))
        object.__setattr__(self, "target_files", tuple(normalize_repo_rel_path(p) for p in self.target_files))
        object.__setattr__(
            self, "expected_new_test_files", tuple(normalize_repo_rel_path(p) for p in self.expected_new_test_files)
        )
        object.__setattr__(self, "depends_on", tuple(str(x).strip() for x in self.depends_on if str(x).strip()))
        object.__setattr__(self, "ac_ids", tuple(str(x).strip() for x in self.ac_ids if str(x).strip()))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SubtaskSpec:
        return cls(
            subtask_id=data["subtask_id"],
            repo_id=data["repo_id"],
            repo_name=data["repo_name"],
            base_commit_sha=data["base_commit_sha"],
            target_branch=data["target_branch"],
            title=data["title"],
            description=data["description"],
            target_files=tuple(data.get("target_files", ())),
            expected_new_test_files=tuple(data.get("expected_new_test_files", ())),
            depends_on=tuple(data.get("depends_on", ())),
            ac_ids=tuple(data.get("ac_ids", ())),
        )


@dataclass(frozen=True)
class ExecutionPlan:
    """Versioned, digest-bound implementation plan requiring approval before coding dispatch."""

    plan_id: str
    plan_version: int
    created_at: str
    spec_id: str
    spec_version: int
    spec_sha256: str
    test_plan_id: str
    model_policy_digest: str
    estimated_cost_cents: int
    sensitive_paths: tuple[str, ...]
    subtasks: tuple[SubtaskSpec, ...]
    allowed_tools: tuple[str, ...]
    plan_sha256: str
    status: Literal["draft", "approved", "rejected", "superseded"] = "draft"
    approval: ApprovalRecord | None = None
    provenance: ArtifactProvenance | None = None
    schema_version: str = SAAS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise UnsupportedSchemaVersionError(f"Unsupported ExecutionPlan schema_version={self.schema_version!r}")
        object.__setattr__(self, "plan_id", _require_non_empty(self.plan_id, "ExecutionPlan.plan_id"))
        if self.plan_version < 1:
            raise ValueError("ExecutionPlan.plan_version must be >= 1")
        object.__setattr__(self, "spec_id", _require_non_empty(self.spec_id, "ExecutionPlan.spec_id"))
        object.__setattr__(self, "spec_sha256", _require_sha256(self.spec_sha256, "ExecutionPlan.spec_sha256"))
        object.__setattr__(self, "test_plan_id", _require_non_empty(self.test_plan_id, "ExecutionPlan.test_plan_id"))
        object.__setattr__(
            self, "model_policy_digest", _require_sha256(self.model_policy_digest, "ExecutionPlan.model_policy_digest")
        )
        if self.estimated_cost_cents < 0:
            raise ValueError("estimated_cost_cents cannot be negative")
        object.__setattr__(self, "sensitive_paths", tuple(normalize_repo_rel_path(p) for p in self.sensitive_paths))
        subtasks = tuple(self.subtasks)
        if not subtasks:
            raise ValueError("ExecutionPlan requires at least one SubtaskSpec.")
        object.__setattr__(self, "subtasks", subtasks)
        object.__setattr__(self, "allowed_tools", tuple(str(t).strip() for t in self.allowed_tools if str(t).strip()))

        expected_sha = self.compute_plan_sha256()
        provided_sha = _require_sha256(self.plan_sha256, "ExecutionPlan.plan_sha256")
        if provided_sha != expected_sha:
            raise ValueError(f"ExecutionPlan integrity mismatch: {provided_sha} != {expected_sha}")
        object.__setattr__(self, "plan_sha256", expected_sha)

        if self.approval is not None and self.approval.approved_spec_sha256 != expected_sha:
            raise ValueError("ApprovalRecord digest does not match ExecutionPlan.plan_sha256")
        if self.status == "approved" and (self.approval is None or self.approval.decision != "approved"):
            raise ValueError("ExecutionPlan with status='approved' requires an approved ApprovalRecord.")

    def canonical_content_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "created_at": self.created_at,
            "spec_id": self.spec_id,
            "spec_version": self.spec_version,
            "spec_sha256": self.spec_sha256,
            "test_plan_id": self.test_plan_id,
            "model_policy_digest": self.model_policy_digest,
            "estimated_cost_cents": self.estimated_cost_cents,
            "sensitive_paths": list(self.sensitive_paths),
            "subtasks": [asdict(s) for s in self.subtasks],
            "allowed_tools": list(self.allowed_tools),
            "provenance": asdict(self.provenance) if self.provenance is not None else None,
        }

    def compute_plan_sha256(self) -> str:
        return sha256_hex(canonical_json_bytes(self.canonical_content_payload()))

    @classmethod
    def create(
        cls,
        *,
        spec: TaskSpec,
        test_plan: TestPlan,
        model_policy_digest: str,
        estimated_cost_cents: int,
        subtasks: Sequence[SubtaskSpec],
        sensitive_paths: Sequence[str] = (),
        allowed_tools: Sequence[str] = ("read_file", "edit_file", "write_file", "execute"),
        provenance: ArtifactProvenance | None = None,
        plan_id: str | None = None,
        plan_version: int = 1,
        created_at: str | None = None,
    ) -> ExecutionPlan:
        pid = plan_id or f"execplan-{spec.spec_id}-v{plan_version}-{secrets.token_hex(3)}"
        ts = created_at or utc_now_iso()
        norm_sens = tuple(normalize_repo_rel_path(p) for p in sensitive_paths)
        sub_tuple = tuple(subtasks)
        tools_tuple = tuple(str(t).strip() for t in allowed_tools if str(t).strip())
        payload = {
            "schema_version": SAAS_SCHEMA_VERSION,
            "plan_id": pid,
            "plan_version": plan_version,
            "created_at": ts,
            "spec_id": spec.spec_id,
            "spec_version": spec.spec_version,
            "spec_sha256": spec.spec_sha256,
            "test_plan_id": test_plan.plan_id,
            "model_policy_digest": model_policy_digest,
            "estimated_cost_cents": estimated_cost_cents,
            "sensitive_paths": list(norm_sens),
            "subtasks": [asdict(s) for s in sub_tuple],
            "allowed_tools": list(tools_tuple),
            "provenance": asdict(provenance) if provenance is not None else None,
        }
        digest = sha256_hex(canonical_json_bytes(payload))
        return cls(
            plan_id=pid,
            plan_version=plan_version,
            created_at=ts,
            spec_id=spec.spec_id,
            spec_version=spec.spec_version,
            spec_sha256=spec.spec_sha256,
            test_plan_id=test_plan.plan_id,
            model_policy_digest=model_policy_digest,
            estimated_cost_cents=estimated_cost_cents,
            sensitive_paths=norm_sens,
            subtasks=sub_tuple,
            allowed_tools=tools_tuple,
            plan_sha256=digest,
            status="draft",
            approval=None,
            provenance=provenance,
            schema_version=SAAS_SCHEMA_VERSION,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ExecutionPlan:
        prov_raw = data.get("provenance")
        appr_raw = data.get("approval")
        return cls(
            schema_version=str(data.get("schema_version") or SAAS_SCHEMA_VERSION),
            plan_id=data["plan_id"],
            plan_version=int(data["plan_version"]),
            created_at=data["created_at"],
            spec_id=data["spec_id"],
            spec_version=int(data["spec_version"]),
            spec_sha256=data["spec_sha256"],
            test_plan_id=data["test_plan_id"],
            model_policy_digest=data["model_policy_digest"],
            estimated_cost_cents=int(data["estimated_cost_cents"]),
            sensitive_paths=tuple(data.get("sensitive_paths", ())),
            subtasks=tuple(SubtaskSpec.from_dict(item) for item in data.get("subtasks", ())),
            allowed_tools=tuple(data.get("allowed_tools", ())),
            plan_sha256=data["plan_sha256"],
            status=data.get("status", "draft"),
            approval=ApprovalRecord.from_dict(appr_raw) if appr_raw is not None else None,
            provenance=ArtifactProvenance.from_dict(prov_raw) if prov_raw is not None else None,
        )


def approve_execution_plan(
    plan: ExecutionPlan, *, reviewer: str, comment: str = "", decided_at: str | None = None
) -> ExecutionPlan:
    """Return an approved ExecutionPlan bound to `plan.plan_sha256`."""
    if plan.status in ("rejected", "superseded"):
        raise ValueError(f"Cannot approve ExecutionPlan in status={plan.status!r}")
    record = ApprovalRecord(
        decision="approved",
        reviewer=reviewer,
        decided_at=decided_at or utc_now_iso(),
        approved_spec_sha256=plan.plan_sha256,
        comment=comment,
    )
    data = plan.to_dict()
    data["status"] = "approved"
    data["approval"] = asdict(record)
    return ExecutionPlan.from_dict(data)


@dataclass(frozen=True)
class SpecChangeRequest:
    """Raised by Dev/QA/Verifier when the approved spec proves infeasible or contradictory."""

    request_id: str
    created_at: str
    spec_id: str
    spec_version: int
    spec_sha256: str
    raised_by_role: str
    reason: str
    proposed_changes: tuple[str, ...]
    blocking_ac_ids: tuple[str, ...] = ()
    status: ChangeRequestStatus = "open"
    provenance: ArtifactProvenance | None = None
    schema_version: str = SAAS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise UnsupportedSchemaVersionError(f"Unsupported SpecChangeRequest schema_version={self.schema_version!r}")
        object.__setattr__(self, "request_id", _require_non_empty(self.request_id, "SpecChangeRequest.request_id"))
        object.__setattr__(self, "spec_id", _require_non_empty(self.spec_id, "SpecChangeRequest.spec_id"))
        object.__setattr__(self, "spec_sha256", _require_sha256(self.spec_sha256, "SpecChangeRequest.spec_sha256"))
        object.__setattr__(self, "reason", _require_non_empty(self.reason, "SpecChangeRequest.reason"))
        object.__setattr__(
            self, "proposed_changes", tuple(str(x).strip() for x in self.proposed_changes if str(x).strip())
        )
        object.__setattr__(
            self, "blocking_ac_ids", tuple(str(x).strip() for x in self.blocking_ac_ids if str(x).strip())
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SpecChangeRequest:
        prov_raw = data.get("provenance")
        return cls(
            schema_version=str(data.get("schema_version") or SAAS_SCHEMA_VERSION),
            request_id=data["request_id"],
            created_at=data["created_at"],
            spec_id=data["spec_id"],
            spec_version=int(data["spec_version"]),
            spec_sha256=data["spec_sha256"],
            raised_by_role=data["raised_by_role"],
            reason=data["reason"],
            proposed_changes=tuple(data.get("proposed_changes", ())),
            blocking_ac_ids=tuple(data.get("blocking_ac_ids", ())),
            status=data.get("status", "open"),
            provenance=ArtifactProvenance.from_dict(prov_raw) if prov_raw is not None else None,
        )


@dataclass(frozen=True)
class CommandCheckResult:
    """Recorded execution evidence of one independent check command (pytest, vitest, ruff, mypy, tsc, build)."""

    name: str
    command: str
    exit_code: int
    duration_ms: int
    passed: bool
    summary: str = ""
    artifact_key: str | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CommandCheckResult:
        return cls(
            name=str(data["name"]),
            command=str(data["command"]),
            exit_code=int(data["exit_code"]),
            duration_ms=int(data.get("duration_ms", 0)),
            passed=bool(data["passed"]),
            summary=str(data.get("summary") or ""),
            artifact_key=data.get("artifact_key"),
        )


@dataclass(frozen=True)
class VerificationReport:
    """Product-grade independent multi-stack verification report with tamper-proof digest."""

    report_id: str
    created_at: str
    repo_id: str
    repo_name: str
    profile_id: str
    base_commit_sha: str
    spec_id: str
    spec_version: int
    spec_sha256: str
    plan_id: str
    plan_sha256: str
    patch_sha256: str
    reapplied_tree_sha256: str
    verdict: VerificationStatus
    reasons: tuple[str, ...]
    changed_files: tuple[str, ...]
    new_test_files: tuple[str, ...]
    red_to_green_ids: tuple[str, ...]
    regressed_ids: tuple[str, ...]
    missing_baseline_ids: tuple[str, ...]
    post_failing_ids: tuple[str, ...]
    modified_existing_test_files: tuple[str, ...]
    touched_protected_configs: tuple[str, ...]
    forbidden_mode_entries: tuple[str, ...]
    checks: tuple[CommandCheckResult, ...]
    ac_traceability: tuple[ACTraceabilityEntry, ...]
    manual_checks_pending: tuple[str, ...]
    report_sha256: str
    provenance: ArtifactProvenance | None = None
    schema_version: str = SAAS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise UnsupportedSchemaVersionError(f"Unsupported VerificationReport schema_version={self.schema_version!r}")
        object.__setattr__(self, "report_id", _require_non_empty(self.report_id, "VerificationReport.report_id"))
        object.__setattr__(self, "spec_sha256", _require_sha256(self.spec_sha256, "VerificationReport.spec_sha256"))
        object.__setattr__(self, "plan_sha256", _require_sha256(self.plan_sha256, "VerificationReport.plan_sha256"))
        object.__setattr__(self, "patch_sha256", _require_sha256(self.patch_sha256, "VerificationReport.patch_sha256"))
        object.__setattr__(
            self,
            "reapplied_tree_sha256",
            _require_sha256(self.reapplied_tree_sha256, "VerificationReport.reapplied_tree_sha256"),
        )
        expected = self.compute_report_sha256()
        provided = _require_sha256(self.report_sha256, "VerificationReport.report_sha256")
        if provided != expected:
            raise ValueError(f"VerificationReport integrity mismatch: {provided} != {expected}")
        object.__setattr__(self, "report_sha256", expected)

    def canonical_content_payload(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("report_sha256", None)
        return d

    def compute_report_sha256(self) -> str:
        return sha256_hex(canonical_json_bytes(self.canonical_content_payload()))

    @classmethod
    def create(cls, **kwargs: Any) -> VerificationReport:
        payload = dict(kwargs)
        payload.setdefault("schema_version", SAAS_SCHEMA_VERSION)
        payload.setdefault("created_at", utc_now_iso())
        checks = tuple(
            c if isinstance(c, CommandCheckResult) else CommandCheckResult.from_dict(c)
            for c in payload.get("checks", ())
        )
        trace = tuple(
            t if isinstance(t, ACTraceabilityEntry) else ACTraceabilityEntry.from_dict(t)
            for t in payload.get("ac_traceability", ())
        )
        payload["checks"] = checks
        payload["ac_traceability"] = trace
        for tuple_key in (
            "reasons",
            "changed_files",
            "new_test_files",
            "red_to_green_ids",
            "regressed_ids",
            "missing_baseline_ids",
            "post_failing_ids",
            "modified_existing_test_files",
            "touched_protected_configs",
            "forbidden_mode_entries",
            "manual_checks_pending",
        ):
            payload[tuple_key] = tuple(payload.get(tuple_key, ()))
        prov = payload.get("provenance")
        canonical = {
            **payload,
            "checks": [asdict(c) for c in checks],
            "ac_traceability": [asdict(t) for t in trace],
            "provenance": asdict(prov) if prov is not None else None,
        }
        for tuple_key in (
            "reasons",
            "changed_files",
            "new_test_files",
            "red_to_green_ids",
            "regressed_ids",
            "missing_baseline_ids",
            "post_failing_ids",
            "modified_existing_test_files",
            "touched_protected_configs",
            "forbidden_mode_entries",
            "manual_checks_pending",
        ):
            canonical[tuple_key] = list(payload[tuple_key])
        digest = sha256_hex(canonical_json_bytes(canonical))
        return cls(**payload, report_sha256=digest)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> VerificationReport:
        prov_raw = data.get("provenance")
        return cls(
            schema_version=str(data.get("schema_version") or SAAS_SCHEMA_VERSION),
            report_id=data["report_id"],
            created_at=data["created_at"],
            repo_id=data["repo_id"],
            repo_name=data["repo_name"],
            profile_id=data["profile_id"],
            base_commit_sha=data["base_commit_sha"],
            spec_id=data["spec_id"],
            spec_version=int(data["spec_version"]),
            spec_sha256=data["spec_sha256"],
            plan_id=data["plan_id"],
            plan_sha256=data["plan_sha256"],
            patch_sha256=data["patch_sha256"],
            reapplied_tree_sha256=data["reapplied_tree_sha256"],
            verdict=data["verdict"],
            reasons=tuple(data.get("reasons", ())),
            changed_files=tuple(data.get("changed_files", ())),
            new_test_files=tuple(data.get("new_test_files", ())),
            red_to_green_ids=tuple(data.get("red_to_green_ids", ())),
            regressed_ids=tuple(data.get("regressed_ids", ())),
            missing_baseline_ids=tuple(data.get("missing_baseline_ids", ())),
            post_failing_ids=tuple(data.get("post_failing_ids", ())),
            modified_existing_test_files=tuple(data.get("modified_existing_test_files", ())),
            touched_protected_configs=tuple(data.get("touched_protected_configs", ())),
            forbidden_mode_entries=tuple(data.get("forbidden_mode_entries", ())),
            checks=tuple(CommandCheckResult.from_dict(c) for c in data.get("checks", ())),
            ac_traceability=tuple(ACTraceabilityEntry.from_dict(t) for t in data.get("ac_traceability", ())),
            manual_checks_pending=tuple(data.get("manual_checks_pending", ())),
            report_sha256=data["report_sha256"],
            provenance=ArtifactProvenance.from_dict(prov_raw) if prov_raw is not None else None,
        )


@dataclass(frozen=True)
class ReviewFinding:
    """File/line-anchored code review finding produced by ChatDev Reviewer or human."""

    finding_id: str
    severity: FindingSeverity
    file_path: str
    line_number: int | None
    title: str
    detail: str
    ac_id: str | None = None
    status: FindingStatus = "open"
    resolved_by: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "finding_id", _require_non_empty(self.finding_id, "ReviewFinding.finding_id"))
        object.__setattr__(self, "file_path", normalize_repo_rel_path(self.file_path))
        object.__setattr__(self, "title", _require_non_empty(self.title, "ReviewFinding.title"))
        object.__setattr__(self, "detail", _require_non_empty(self.detail, "ReviewFinding.detail"))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReviewFinding:
        return cls(
            finding_id=data["finding_id"],
            severity=data["severity"],
            file_path=data["file_path"],
            line_number=data.get("line_number"),
            title=data["title"],
            detail=data["detail"],
            ac_id=data.get("ac_id"),
            status=data.get("status", "open"),
            resolved_by=data.get("resolved_by"),
        )


@dataclass(frozen=True)
class ReviewReport:
    """Diff-based code review output bound to `patch_sha256` and `verification_report_sha256`."""

    review_id: str
    created_at: str
    reviewer_agent: str
    reviewer_model_id: str
    spec_sha256: str
    patch_sha256: str
    verification_report_sha256: str
    verdict: ReviewVerdict
    summary: str
    findings: tuple[ReviewFinding, ...]
    provenance: ArtifactProvenance | None = None
    schema_version: str = SAAS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise UnsupportedSchemaVersionError(f"Unsupported ReviewReport schema_version={self.schema_version!r}")
        object.__setattr__(self, "review_id", _require_non_empty(self.review_id, "ReviewReport.review_id"))
        object.__setattr__(self, "spec_sha256", _require_sha256(self.spec_sha256, "ReviewReport.spec_sha256"))
        object.__setattr__(self, "patch_sha256", _require_sha256(self.patch_sha256, "ReviewReport.patch_sha256"))
        object.__setattr__(
            self,
            "verification_report_sha256",
            _require_sha256(self.verification_report_sha256, "ReviewReport.verification_report_sha256"),
        )
        object.__setattr__(self, "summary", _require_non_empty(self.summary, "ReviewReport.summary"))
        object.__setattr__(self, "findings", tuple(self.findings))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReviewReport:
        prov_raw = data.get("provenance")
        return cls(
            schema_version=str(data.get("schema_version") or SAAS_SCHEMA_VERSION),
            review_id=data["review_id"],
            created_at=data["created_at"],
            reviewer_agent=data["reviewer_agent"],
            reviewer_model_id=data["reviewer_model_id"],
            spec_sha256=data["spec_sha256"],
            patch_sha256=data["patch_sha256"],
            verification_report_sha256=data["verification_report_sha256"],
            verdict=data["verdict"],
            summary=data["summary"],
            findings=tuple(ReviewFinding.from_dict(f) for f in data.get("findings", ())),
            provenance=ArtifactProvenance.from_dict(prov_raw) if prov_raw is not None else None,
        )


def migrate_contract_payload(
    contract_type: Literal[
        "RepoContext",
        "TaskSpec",
        "TestPlan",
        "ExecutionReport",
        "ExecutionPlan",
        "SpecChangeRequest",
        "VerificationReport",
        "ReviewReport",
    ],
    payload: Mapping[str, Any],
    *,
    provenance: ArtifactProvenance | None = None,
) -> dict[str, Any]:
    """Validate version compatibility and migrate v1.0 payloads cleanly toward v1.1 SaaS format.

    Raises `UnsupportedSchemaVersionError` for unrecognized versions.
    """
    data = copy.deepcopy(dict(payload))
    version = str(data.get("schema_version") or V1_0_SCHEMA_VERSION)
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise UnsupportedSchemaVersionError(
            f"Cannot migrate {contract_type} from unsupported schema_version={version!r}."
        )

    # Validate structural integrity on source payload before upgrading envelope metadata.
    if contract_type == "RepoContext":
        RepoContext.from_dict(data)
    elif contract_type == "TaskSpec":
        TaskSpec.from_dict(data)
    elif contract_type == "TestPlan":
        TestPlan.from_dict(data)
    elif contract_type == "ExecutionReport":
        ExecutionReport.from_dict(data)
    elif contract_type == "ExecutionPlan":
        ExecutionPlan.from_dict(data)
    elif contract_type == "SpecChangeRequest":
        SpecChangeRequest.from_dict(data)
    elif contract_type == "VerificationReport":
        VerificationReport.from_dict(data)
    elif contract_type == "ReviewReport":
        ReviewReport.from_dict(data)

    # Note: Digest-bound v1.0 artifacts (TaskSpec, VerificationReport) preserve internal content digest;
    # SaaS provenance metadata is attached cleanly as an outer/optional envelope when upgrading.
    migrated = {
        "schema_version": SAAS_SCHEMA_VERSION if provenance is not None else version,
        "source_schema_version": version,
        "contract_type": contract_type,
        "payload": data,
        "provenance": asdict(provenance) if provenance is not None else data.get("provenance"),
    }
    return migrated
