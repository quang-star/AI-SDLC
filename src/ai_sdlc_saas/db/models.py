"""SQLAlchemy 2.0 relational schema for AI SDLC SaaS (all 8 Roadmap data groups).

Every tenant-owned table carries `tenant_id` and composite unique/foreign-key
constraints `(tenant_id, <parent_id>)` so cross-tenant joins/links are rejected
at the database integrity layer on both PostgreSQL and SQLite (`foreign_keys=ON`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for all SaaS ORM models."""


class TenantOwnedMixin:
    """Marker mixin for tables strictly scoped to a tenant (`tenant_id`)."""

    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )


# ---------------------------------------------------------------------------
# 1. Identity & Tenancy
# ---------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(128), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_platform_operator: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    verification_token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reset_token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reset_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    jti_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    active_tenant_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True
    )
    user_agent: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    data_region: Mapped[str] = mapped_column(String(32), default="us-east", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)  # active | suspended | deleting | deleted
    retention_days: Mapped[int] = mapped_column(Integer, default=90, nullable=False)
    encryption_key_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Membership(TenantOwnedMixin, Base):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_membership_tenant_user"),
        UniqueConstraint("tenant_id", "id", name="uq_membership_tenant_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)  # owner | admin | developer | reviewer | viewer
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)  # active | suspended | revoked
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Invitation(TenantOwnedMixin, Base):
    __tablename__ = "invitations"
    __table_args__ = (UniqueConstraint("tenant_id", "id", name="uq_invitation_tenant_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    invited_by_user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)  # pending | accepted | revoked | expired
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# ---------------------------------------------------------------------------
# 2. Source & Repository Connections
# ---------------------------------------------------------------------------


class GitHubInstallation(TenantOwnedMixin, Base):
    __tablename__ = "installations"
    __table_args__ = (UniqueConstraint("tenant_id", "id", name="uq_installation_tenant_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    external_installation_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    account_login: Mapped[str] = mapped_column(String(128), nullable=False)
    ownership_proof_digest: Mapped[str] = mapped_column(String(128), nullable=False)
    encrypted_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)  # active | suspended | uninstalled
    permissions_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Project(TenantOwnedMixin, Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_project_tenant_id"),
        UniqueConstraint("tenant_id", "slug", name="uq_project_tenant_slug"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), default="policy-v1", nullable=False)
    policy_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Repository(TenantOwnedMixin, Base):
    __tablename__ = "repositories"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_repo_tenant_id"),
        ForeignKeyConstraint(["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["tenant_id", "installation_id"], ["installations.tenant_id", "installations.id"], ondelete="SET NULL"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    installation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider: Mapped[str] = mapped_column(String(32), default="local_git", nullable=False)  # github_cloud | local_git
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    clone_url_or_path: Mapped[str] = mapped_column(Text, nullable=False)
    default_branch: Mapped[str] = mapped_column(String(120), default="main", nullable=False)
    support_profile: Mapped[str] = mapped_column(String(64), default="python-pytest", nullable=False)
    branch_protection_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)  # active | disconnected | unsupported
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    validation_report_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class RepoProfile(TenantOwnedMixin, Base):
    __tablename__ = "repo_profiles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_repo_profile_tenant_id"),
        ForeignKeyConstraint(["tenant_id", "repo_id"], ["repositories.tenant_id", "repositories.id"], ondelete="CASCADE"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    repo_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    profile_id: Mapped[str] = mapped_column(String(64), nullable=False)
    install_cmd: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    test_cmd: Mapped[str] = mapped_column(String(255), nullable=False)
    lint_cmd: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    typecheck_cmd: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    build_cmd: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    ephemeral_services_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class RepoSnapshot(TenantOwnedMixin, Base):
    __tablename__ = "repo_snapshots"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_repo_snapshot_tenant_id"),
        ForeignKeyConstraint(["tenant_id", "repo_id"], ["repositories.tenant_id", "repositories.id"], ondelete="CASCADE"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    repo_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    tree_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    context_object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# ---------------------------------------------------------------------------
# 3. LLM Gateway, Routes & Credentials
# ---------------------------------------------------------------------------


class CredentialRef(TenantOwnedMixin, Base):
    __tablename__ = "credential_refs"
    __table_args__ = (UniqueConstraint("tenant_id", "id", name="uq_credential_ref_tenant_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_type: Mapped[str] = mapped_column(String(32), nullable=False)  # 9router | openai_compatible | github_pat
    encrypted_secret: Mapped[str] = mapped_column(Text, nullable=False)
    masked_preview: Mapped[str] = mapped_column(String(64), nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ModelRoute(TenantOwnedMixin, Base):
    __tablename__ = "model_routes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_model_route_tenant_id"),
        ForeignKeyConstraint(["tenant_id", "credential_ref_id"], ["credential_refs.tenant_id", "credential_refs.id"], ondelete="SET NULL"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)  # 9router | openai_compatible | mock
    base_url: Mapped[str] = mapped_column(String(255), nullable=False)
    model_id: Mapped[str] = mapped_column(String(160), nullable=False)
    credential_ref_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    capabilities_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    rate_limit_rpm: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=4, nullable=False)
    input_cost_microcents_per_1k: Mapped[int] = mapped_column(Integer, default=150, nullable=False)
    output_cost_microcents_per_1k: Mapped[int] = mapped_column(Integer, default=600, nullable=False)
    last_probed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ModelPolicy(TenantOwnedMixin, Base):
    __tablename__ = "model_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_model_policy_tenant_id"),
        ForeignKeyConstraint(["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"], ondelete="CASCADE"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    role_route_map_json: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    compression_mode: Mapped[str] = mapped_column(String(32), default="none", nullable=False)  # none | rtk | headroom
    max_context_tokens: Mapped[int] = mapped_column(Integer, default=32000, nullable=False)
    max_output_tokens: Mapped[int] = mapped_column(Integer, default=8192, nullable=False)
    per_task_budget_cents: Mapped[int] = mapped_column(Integer, default=500, nullable=False)
    policy_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# ---------------------------------------------------------------------------
# 4. Workflow, Tasks, Runs, Approvals & Checkpoints
# ---------------------------------------------------------------------------


class Task(TenantOwnedMixin, Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_task_tenant_id"),
        ForeignKeyConstraint(["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"], ondelete="CASCADE"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_by_user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    requirement_text: Mapped[str] = mapped_column(Text, nullable=False)
    task_type: Mapped[str] = mapped_column(String(32), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(32), default="standard", nullable=False)
    status: Mapped[str] = mapped_column(String(48), default="CREATED", nullable=False, index=True)
    current_stage: Mapped[str] = mapped_column(String(64), default="intake", nullable=False)
    cleanup_status: Mapped[str] = mapped_column(String(32), default="PENDING", nullable=False)
    repo_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    budget_cap_cents: Mapped[int] = mapped_column(Integer, default=500, nullable=False)
    spent_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class TaskRun(TenantOwnedMixin, Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_run_tenant_id"),
        ForeignKeyConstraint(["tenant_id", "task_id"], ["tasks.tenant_id", "tasks.id"], ondelete="CASCADE"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    thread_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(48), default="RUNNING", nullable=False)
    current_node: Mapped[str] = mapped_column(String(64), default="intake", nullable=False)
    frozen_policy_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    base_commits_json: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    spec_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    spec_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    spec_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plan_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plan_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    patch_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verification_verdict: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RunAttempt(TenantOwnedMixin, Base):
    __tablename__ = "attempts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_attempt_tenant_id"),
        ForeignKeyConstraint(["tenant_id", "run_id"], ["runs.tenant_id", "runs.id"], ondelete="CASCADE"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="running", nullable=False)
    failure_class: Mapped[str | None] = mapped_column(String(48), nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SubtaskRecord(TenantOwnedMixin, Base):
    __tablename__ = "subtasks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_subtask_tenant_id"),
        ForeignKeyConstraint(["tenant_id", "run_id"], ["runs.tenant_id", "runs.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["tenant_id", "repo_id"], ["repositories.tenant_id", "repositories.id"], ondelete="CASCADE"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    repo_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    base_commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    target_branch: Mapped[str] = mapped_column(String(120), nullable=False)
    depends_on_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    patch_object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    patch_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ArtifactVersion(TenantOwnedMixin, Base):
    __tablename__ = "artifact_versions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_artifact_tenant_id"),
        ForeignKeyConstraint(["tenant_id", "task_id"], ["tasks.tenant_id", "tasks.id"], ondelete="CASCADE"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    artifact_type: Mapped[str] = mapped_column(String(64), nullable=False)  # RepoContext | TaskSpec | TestPlan | ExecutionPlan | Patch | VerificationReport | ReviewReport
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), default="1.1", nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Approval(TenantOwnedMixin, Base):
    __tablename__ = "approvals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_approval_tenant_id"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_approval_idempotency"),
        ForeignKeyConstraint(["tenant_id", "task_id"], ["tasks.tenant_id", "tasks.id"], ondelete="CASCADE"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    gate_type: Mapped[str] = mapped_column(String(32), nullable=False)  # spec | plan | delivery | release
    decision: Mapped[str] = mapped_column(String(32), nullable=False)  # approved | rejected | changes_requested
    target_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    base_commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    comment: Mapped[str] = mapped_column(Text, default="", nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    invalidated_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class SpecChangeRequestRecord(TenantOwnedMixin, Base):
    __tablename__ = "spec_change_requests"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_scr_tenant_id"),
        ForeignKeyConstraint(["tenant_id", "task_id"], ["tasks.tenant_id", "tasks.id"], ondelete="CASCADE"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    spec_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    raised_by_role: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class GraphCheckpoint(TenantOwnedMixin, Base):
    __tablename__ = "graph_checkpoints"
    __table_args__ = (
        UniqueConstraint("tenant_id", "thread_id", "checkpoint_ns", "checkpoint_id", name="uq_checkpoint_key"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    checkpoint_ns: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    checkpoint_id: Mapped[str] = mapped_column(String(128), nullable=False)
    parent_checkpoint_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    checkpoint_blob: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    metadata_blob: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    writes_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# ---------------------------------------------------------------------------
# 5. Runtime: Jobs, Outbox, Leases, Sandbox Resources & SSE Events
# ---------------------------------------------------------------------------


class Job(TenantOwnedMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_job_tenant_id"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_job_idempotency"),
        Index("ix_jobs_status_avail", "status", "available_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)  # queued | leased | succeeded | dead_letter | cancelled
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class OutboxEvent(TenantOwnedMixin, Base):
    __tablename__ = "outbox_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class WorkerLease(Base):
    __tablename__ = "worker_leases"

    worker_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    hostname: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="alive", nullable=False)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class SandboxResource(TenantOwnedMixin, Base):
    __tablename__ = "sandbox_resources"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_sandbox_res_tenant_id"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_sandbox_res_idemp"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)  # vm | snapshot | worktree | preview | token
    provider: Mapped[str] = mapped_column(String(32), nullable=False)  # instabox | local_worktree | preview
    external_resource_id: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="creating", nullable=False)  # creating | active | cleaning | deleted | failed_cleanup
    ttl_seconds: Mapped[int] = mapped_column(Integer, default=3600, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CleanupJob(TenantOwnedMixin, Base):
    __tablename__ = "cleanup_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    resource_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TaskEvent(TenantOwnedMixin, Base):
    __tablename__ = "task_events"
    __table_args__ = (Index("ix_task_events_tenant_task_seq", "tenant_id", "task_id", "seq"),)

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# ---------------------------------------------------------------------------
# 6. Review, Verification, Pull Requests & Previews
# ---------------------------------------------------------------------------


class TestPlanRecord(TenantOwnedMixin, Base):
    __tablename__ = "test_plans"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    spec_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class VerificationReportRecord(TenantOwnedMixin, Base):
    __tablename__ = "verification_reports"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    repo_id: Mapped[str] = mapped_column(String(64), nullable=False)
    patch_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    report_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class TraceabilityEntryRecord(TenantOwnedMixin, Base):
    __tablename__ = "traceability_entries"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    report_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    ac_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    linked_test_cases_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    observed_test_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    manual_confirmed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ReviewFindingRecord(TenantOwnedMixin, Base):
    __tablename__ = "findings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    file_path: Mapped[str] = mapped_column(String(512), nullable=False)
    line_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    resolved_by_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class PullRequestRecord(TenantOwnedMixin, Base):
    __tablename__ = "pull_requests"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_pr_idempotency"),
        ForeignKeyConstraint(["tenant_id", "repo_id"], ["repositories.tenant_id", "repositories.id"], ondelete="CASCADE"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    repo_id: Mapped[str] = mapped_column(String(64), nullable=False)
    change_set_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    branch_name: Mapped[str] = mapped_column(String(160), nullable=False)
    base_branch: Mapped[str] = mapped_column(String(120), nullable=False)
    base_commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    patch_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    external_pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    external_pr_url: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)  # draft | open | ci_failed | merged | closed
    ci_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    release_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class PreviewEnvironment(TenantOwnedMixin, Base):
    __tablename__ = "preview_environments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    access_token_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    preview_url: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="running", nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class TenantMemory(TenantOwnedMixin, Base):
    __tablename__ = "tenant_memories"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"], ondelete="CASCADE"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    lesson_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_by_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# ---------------------------------------------------------------------------
# 7. Commercial: Plans, Subscriptions, Reservations, Usage Ledger & Billing
# ---------------------------------------------------------------------------


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # trial | pro | team | enterprise
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    monthly_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    included_credits_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    max_seats: Mapped[int] = mapped_column(Integer, nullable=False)
    max_repos: Mapped[int] = mapped_column(Integer, nullable=False)
    max_concurrent_tasks: Mapped[int] = mapped_column(Integer, nullable=False)
    allowed_model_tiers_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)


class Subscription(TenantOwnedMixin, Base):
    __tablename__ = "subscriptions"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_subscription_tenant"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plan_id: Mapped[str] = mapped_column(String(64), ForeignKey("plans.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="trialing", nullable=False)  # trialing | active | past_due | canceled | suspended
    external_customer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    external_subscription_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    credit_balance_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reserved_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    grace_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class QuotaReservation(TenantOwnedMixin, Base):
    __tablename__ = "quota_reservations"
    __table_args__ = (UniqueConstraint("tenant_id", "idempotency_key", name="uq_qres_idempotency"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    reserved_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    settled_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="held", nullable=False)  # held | settled | released
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class RequestUsage(TenantOwnedMixin, Base):
    __tablename__ = "request_usage"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_id: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    route_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actual_model_id: Mapped[str] = mapped_column(String(160), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    usage_status: Mapped[str] = mapped_column(String(32), default="CONFIRMED", nullable=False)  # CONFIRMED | ESTIMATED_MISSING
    upstream_cost_microcents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    customer_cost_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class UsageLedgerEntry(TenantOwnedMixin, Base):
    __tablename__ = "usage_ledger"
    __table_args__ = (UniqueConstraint("tenant_id", "idempotency_key", name="uq_ledger_idempotency"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    entry_type: Mapped[str] = mapped_column(String(32), nullable=False)  # grant | reserve | consume | release | adjust | refund
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)  # signed integer cents
    upstream_cost_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sandbox_cost_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    unit_price_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class BillingEvent(Base):
    __tablename__ = "billing_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    external_event_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    event_created_ts: Mapped[int] = mapped_column(Integer, nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="processed", nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# ---------------------------------------------------------------------------
# 8. Governance: Audit, Retention, Exports, Support, Webhooks & Kill Switches
# ---------------------------------------------------------------------------


class AuditEvent(TenantOwnedMixin, Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class DataExportJob(TenantOwnedMixin, Base):
    __tablename__ = "exports"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    requested_by_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="completed", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class RetentionJob(TenantOwnedMixin, Base):
    __tablename__ = "retention_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_kind: Mapped[str] = mapped_column(String(32), nullable=False)  # retention_sweep | hard_delete
    requested_by_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="completed", nullable=False)
    deleted_counts_json: Mapped[dict[str, int]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class SupportAccessGrant(Base):
    __tablename__ = "support_access"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    operator_user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    ticket_id: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class KillSwitch(Base):
    __tablename__ = "kill_switches"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)  # global | provider | tenant | workflow
    target_key: Mapped[str] = mapped_column(String(128), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class NotificationRecord(TenantOwnedMixin, Base):
    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    channel: Mapped[str] = mapped_column(String(32), default="in_app", nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    delivery_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # github | stripe
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


TENANT_SCOPED_MODELS: tuple[type[Any], ...] = (
    Membership,
    Invitation,
    GitHubInstallation,
    Project,
    Repository,
    RepoProfile,
    RepoSnapshot,
    CredentialRef,
    ModelRoute,
    ModelPolicy,
    Task,
    TaskRun,
    RunAttempt,
    SubtaskRecord,
    ArtifactVersion,
    Approval,
    SpecChangeRequestRecord,
    GraphCheckpoint,
    Job,
    OutboxEvent,
    SandboxResource,
    CleanupJob,
    TaskEvent,
    TestPlanRecord,
    VerificationReportRecord,
    TraceabilityEntryRecord,
    ReviewFindingRecord,
    PullRequestRecord,
    PreviewEnvironment,
    TenantMemory,
    Subscription,
    QuotaReservation,
    RequestUsage,
    UsageLedgerEntry,
    AuditEvent,
    DataExportJob,
    RetentionJob,
    NotificationRecord,
)
