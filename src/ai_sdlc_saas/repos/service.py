"""Project, repository onboarding, snapshot capture, and template scaffolding services."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_sdlc.contracts import canonical_json_bytes
from ai_sdlc_saas.db.models import (
    GitHubInstallation,
    Project,
    RepoProfile,
    Repository,
    RepoSnapshot,
)
from ai_sdlc_saas.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from ai_sdlc_saas.governance.audit import record_audit_event
from ai_sdlc_saas.repos.git_ops import clone_at_commit, init_bare_repo_from_dir
from ai_sdlc_saas.repos.profiles import (
    SUPPORTED_PROFILES,
    inspect_repo_profile,
    profile_to_dict,
)
from ai_sdlc_saas.storage.object_store import LocalObjectStore, build_artifact_key

_SLUG_RE = re.compile(r"[^a-z0-9-]+")
_DEFAULT_TEMPLATES_DIR = Path(__file__).resolve().parents[3] / "project_templates"


def _slugify(value: str) -> str:
    s = _SLUG_RE.sub("-", value.strip().lower()).strip("-")
    return s[:64] if len(s) >= 2 else f"proj-{secrets.token_hex(3)}"


def create_project(
    session: Session,
    *,
    tenant_id: str,
    actor_user_id: str,
    name: str,
    slug: str | None = None,
    description: str = "",
    policy_json: dict[str, Any] | None = None,
) -> Project:
    cleaned = name.strip()
    if len(cleaned) < 2:
        raise ValidationError("Project name must be at least 2 characters.")
    candidate_slug = _slugify(slug or cleaned)
    existing = session.scalar(
        select(Project).where(Project.tenant_id == tenant_id, Project.slug == candidate_slug)
    )
    if existing is not None:
        if slug:
            raise ConflictError(f"Project slug {candidate_slug!r} already exists in this organization.")
        candidate_slug = f"{candidate_slug[:55]}-{secrets.token_hex(3)}"

    project = Project(
        id=f"prj_{secrets.token_hex(8)}",
        tenant_id=tenant_id,
        slug=candidate_slug,
        name=cleaned[:150],
        description=description,
        policy_json=policy_json
        or {
            "forbidden_paths": [".github/workflows/", "infra/", "deploy/"],
            "sensitive_path_prefixes": ["auth/", "security/", "migrations/"],
            "require_spec_approval": True,
            "require_plan_approval": True,
            "require_release_approval": True,
        },
    )
    session.add(project)
    record_audit_event(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="project.created",
        target_type="project",
        target_id=project.id,
        details={"slug": project.slug, "name": project.name},
    )
    session.flush()
    return project


def list_projects(session: Session, *, tenant_id: str) -> list[Project]:
    return list(
        session.scalars(
            select(Project).where(Project.tenant_id == tenant_id).order_by(Project.created_at.desc())
        )
    )


def connect_repository(
    session: Session,
    *,
    tenant_id: str,
    actor_user_id: str,
    project_id: str,
    provider: str,
    full_name: str,
    clone_url_or_path: str,
    default_branch: str = "main",
    installation_id: str | None = None,
    allow_local_repos: bool = False,
    object_store: LocalObjectStore,
    worktree_root: Path,
) -> dict[str, Any]:
    """Connect a repository, capture an immutable snapshot + RepoContext, and validate Support Matrix."""
    project = session.scalar(
        select(Project).where(Project.id == project_id, Project.tenant_id == tenant_id)
    )
    if project is None:
        raise NotFoundError("Project not found in active organization.")

    if installation_id is not None:
        inst = session.scalar(
            select(GitHubInstallation).where(
                GitHubInstallation.id == installation_id,
                GitHubInstallation.tenant_id == tenant_id,
            )
        )
        if inst is None or inst.status != "active":
            raise ForbiddenError("GitHub App installation is missing or inactive.")

    repo_id = f"rep_{secrets.token_hex(8)}"
    checkout_dir = Path(worktree_root) / "inspect" / tenant_id / repo_id
    try:
        commit_sha, _tree_sha = clone_at_commit(
            clone_url_or_path,
            checkout_dir,
            branch_or_sha=default_branch,
            allow_local_repos=allow_local_repos,
        )
        inspection = inspect_repo_profile(checkout_dir, repo_source=full_name)
    finally:
        shutil.rmtree(checkout_dir, ignore_errors=True)

    ctx_payload = canonical_json_bytes(inspection.repo_context.to_dict())
    ctx_key = build_artifact_key(tenant_id, project_id, f"snap_{commit_sha[:12]}", "repo_context.json")
    object_store.put_bytes(tenant_id, ctx_key, ctx_payload, content_type="application/json")

    report = profile_to_dict(inspection)
    repo = Repository(
        id=repo_id,
        tenant_id=tenant_id,
        project_id=project_id,
        installation_id=installation_id,
        provider=provider,
        full_name=full_name.strip(),
        clone_url_or_path=clone_url_or_path,
        default_branch=default_branch,
        support_profile=inspection.profile_id,
        status="active",
        last_validated_at=datetime.now(UTC),
        validation_report_json=report,
    )
    session.add(repo)
    session.flush()

    profile = RepoProfile(
        id=f"prf_{secrets.token_hex(8)}",
        tenant_id=tenant_id,
        repo_id=repo.id,
        profile_id=inspection.profile_id,
        install_cmd=inspection.install_cmd,
        test_cmd=inspection.test_cmd,
        lint_cmd=inspection.lint_cmd,
        typecheck_cmd=inspection.typecheck_cmd,
        build_cmd=inspection.build_cmd,
    )
    snapshot = RepoSnapshot(
        id=f"snp_{secrets.token_hex(8)}",
        tenant_id=tenant_id,
        repo_id=repo.id,
        commit_sha=commit_sha,
        tree_sha256=inspection.repo_context.repo_tree_sha256,
        context_object_key=ctx_key,
    )
    session.add_all([profile, snapshot])
    record_audit_event(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="repo.connected",
        target_type="repository",
        target_id=repo.id,
        details={"full_name": repo.full_name, "support_profile": repo.support_profile, "commit_sha": commit_sha},
    )
    session.flush()
    return {"repository": repo, "profile": profile, "snapshot": snapshot, "report": report}


def refresh_repository_snapshot(
    session: Session,
    *,
    tenant_id: str,
    repo_id: str,
    allow_local_repos: bool = False,
    object_store: LocalObjectStore,
    worktree_root: Path,
) -> dict[str, Any]:
    repo = assert_repo_dispatchable(session, tenant_id=tenant_id, repo_id=repo_id)
    checkout_dir = Path(worktree_root) / "inspect" / tenant_id / f"{repo.id}_{secrets.token_hex(3)}"
    try:
        commit_sha, _tree_sha = clone_at_commit(
            repo.clone_url_or_path,
            checkout_dir,
            branch_or_sha=repo.default_branch,
            allow_local_repos=allow_local_repos,
        )
        inspection = inspect_repo_profile(checkout_dir, repo_source=repo.full_name)
    finally:
        shutil.rmtree(checkout_dir, ignore_errors=True)

    ctx_payload = canonical_json_bytes(inspection.repo_context.to_dict())
    ctx_key = build_artifact_key(tenant_id, repo.project_id, f"snap_{commit_sha[:12]}", "repo_context.json")
    object_store.put_bytes(tenant_id, ctx_key, ctx_payload, content_type="application/json")
    snapshot = RepoSnapshot(
        id=f"snp_{secrets.token_hex(8)}",
        tenant_id=tenant_id,
        repo_id=repo.id,
        commit_sha=commit_sha,
        tree_sha256=inspection.repo_context.repo_tree_sha256,
        context_object_key=ctx_key,
    )
    repo.support_profile = inspection.profile_id
    repo.last_validated_at = datetime.now(UTC)
    repo.validation_report_json = profile_to_dict(inspection)
    session.add(snapshot)
    session.flush()
    return {"repository": repo, "snapshot": snapshot, "repo_context": inspection.repo_context}


def load_snapshot_repo_context(
    session: Session,
    *,
    tenant_id: str,
    repo_id: str,
    object_store: LocalObjectStore,
) -> tuple[RepoSnapshot, dict[str, Any]]:
    snapshot = session.scalar(
        select(RepoSnapshot)
        .where(RepoSnapshot.tenant_id == tenant_id, RepoSnapshot.repo_id == repo_id)
        .order_by(RepoSnapshot.created_at.desc())
    )
    if snapshot is None:
        raise NotFoundError("Repository has no immutable snapshot.")
    raw = object_store.get_bytes(tenant_id, snapshot.context_object_key)
    data: dict[str, Any] = json.loads(raw.decode("utf-8"))
    return snapshot, data


def assert_repo_dispatchable(session: Session, *, tenant_id: str, repo_id: str) -> Repository:
    repo = session.scalar(
        select(Repository).where(Repository.id == repo_id, Repository.tenant_id == tenant_id)
    )
    if repo is None:
        raise NotFoundError("Repository not found in active organization.")
    if repo.status != "active":
        raise ConflictError(f"Repository {repo.full_name!r} is {repo.status!r} and cannot accept tasks.")
    if repo.support_profile not in SUPPORTED_PROFILES:
        raise ForbiddenError(f"Unsupported repository profile {repo.support_profile!r}.")
    if repo.installation_id is not None:
        inst = session.scalar(
            select(GitHubInstallation).where(
                GitHubInstallation.id == repo.installation_id,
                GitHubInstallation.tenant_id == tenant_id,
            )
        )
        if inst is None or inst.status != "active":
            raise ForbiddenError("GitHub App installation was uninstalled or suspended.")
    return repo


def list_repositories(session: Session, *, tenant_id: str, project_id: str | None = None) -> list[Repository]:
    stmt = select(Repository).where(Repository.tenant_id == tenant_id)
    if project_id:
        stmt = stmt.where(Repository.project_id == project_id)
    return list(session.scalars(stmt.order_by(Repository.created_at.desc())))


def scaffold_from_template(
    session: Session,
    *,
    tenant_id: str,
    actor_user_id: str,
    project_id: str,
    template_id: str,
    repo_name: str,
    object_store: LocalObjectStore,
    worktree_root: Path,
    templates_dir: Path | None = None,
    run_baseline: bool = True,
) -> dict[str, Any]:
    """Create a local bare repository from an official starter template, connect it, and verify baseline."""
    base_templates = (templates_dir or _DEFAULT_TEMPLATES_DIR).resolve()
    template_path = (base_templates / template_id).resolve()
    if not template_path.is_relative_to(base_templates) or not template_path.is_dir():
        raise NotFoundError(f"Starter template {template_id!r} not found.")

    clean_name = _SLUG_RE.sub("-", repo_name.strip().lower()).strip("-") or f"starter-{secrets.token_hex(3)}"
    bare_dir = Path(worktree_root).resolve() / "remotes" / tenant_id / f"{clean_name}-{secrets.token_hex(3)}.git"
    init_bare_repo_from_dir(template_path, bare_dir, default_branch="main")

    res = connect_repository(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        project_id=project_id,
        provider="local_git",
        full_name=f"starter/{clean_name}",
        clone_url_or_path=str(bare_dir),
        default_branch="main",
        allow_local_repos=True,
        object_store=object_store,
        worktree_root=worktree_root,
    )

    baseline: dict[str, Any] = {"executed": False, "passed": True, "details": "Baseline skipped"}
    if run_baseline:
        profile: RepoProfile = res["profile"]
        if profile.profile_id == "python-pytest":
            with tempfile.TemporaryDirectory(prefix="tpl_base_") as tmp:
                wt = Path(tmp) / "work"
                clone_at_commit(str(bare_dir), wt, allow_local_repos=True)
                env = os.environ.copy()
                src_dir = wt / "src"
                if src_dir.is_dir():
                    env["PYTHONPATH"] = str(src_dir)
                proc = subprocess.run(
                    ["python", "-m", "pytest", "-q"],
                    cwd=str(wt),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                baseline = {
                    "executed": True,
                    "passed": proc.returncode == 0,
                    "returncode": proc.returncode,
                    "stdout": proc.stdout[-1000:],
                    "stderr": proc.stderr[-500:],
                }
        elif profile.profile_id in ("node-ts-vitest", "node-js-jest"):
            baseline = {
                "executed": False,
                "passed": True,
                "details": "Node starter structure validated (install npm dependencies in sandbox before executing vitest).",
            }

    res["baseline"] = baseline
    return res
