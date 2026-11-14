"""Projects, repositories, GitHub installations, starter templates, and snapshot endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_sdlc_saas.adapters.chatdev_service import (
    approve_tenant_memory,
    create_tenant_memory,
    delete_tenant_memory,
    list_tenant_memories,
)
from ai_sdlc_saas.api.deps import get_db, get_runtime, require_permission
from ai_sdlc_saas.api.runtime import AppRuntime
from ai_sdlc_saas.db.models import GitHubInstallation, Membership, Tenant
from ai_sdlc_saas.repos.github_app import bind_github_installation
from ai_sdlc_saas.repos.service import (
    connect_repository,
    create_project,
    list_projects,
    list_repositories,
    load_snapshot_repo_context,
    refresh_repository_snapshot,
    scaffold_from_template,
)

router = APIRouter(prefix="/api/v1", tags=["repos"])


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=2, max_length=150)
    slug: str | None = Field(default=None, max_length=64)
    description: str = ""
    policy_json: dict[str, Any] | None = None


class BindInstallationRequest(BaseModel):
    external_installation_id: str
    account_login: str
    ownership_proof: str
    access_token: str | None = None
    permissions: dict[str, Any] | None = None


class ConnectRepoRequest(BaseModel):
    project_id: str
    provider: str = "github_cloud"
    full_name: str
    clone_url_or_path: str
    default_branch: str = "main"
    installation_id: str | None = None


class ScaffoldTemplateRequest(BaseModel):
    project_id: str
    template_id: str
    repo_name: str
    run_baseline: bool = True


@router.get("/projects")
def api_list_projects(
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("repo:read")),
    db: Session = Depends(get_db, scope="function"),
) -> list[dict[str, Any]]:
    _ctx, tenant, _mem = perm
    return [
        {
            "id": p.id,
            "slug": p.slug,
            "name": p.name,
            "description": p.description,
            "policy_version": p.policy_version,
            "policy_json": p.policy_json,
            "created_at": p.created_at.isoformat(),
        }
        for p in list_projects(db, tenant_id=tenant.id)
    ]


@router.post("/projects", status_code=201)
def api_create_project(
    body: CreateProjectRequest,
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("repo:connect")),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    ctx, tenant, _mem = perm
    proj = create_project(
        db,
        tenant_id=tenant.id,
        actor_user_id=ctx.user.id,
        name=body.name,
        slug=body.slug,
        description=body.description,
        policy_json=body.policy_json,
    )
    return {
        "id": proj.id,
        "slug": proj.slug,
        "name": proj.name,
        "description": proj.description,
        "policy_version": proj.policy_version,
        "policy_json": proj.policy_json,
    }


@router.get("/installations")
def api_list_installations(
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("repo:read")),
    db: Session = Depends(get_db, scope="function"),
) -> list[dict[str, Any]]:
    _ctx, tenant, _mem = perm
    rows = list(
        db.scalars(
            select(GitHubInstallation).where(GitHubInstallation.tenant_id == tenant.id)
        )
    )
    return [
        {
            "id": r.id,
            "external_installation_id": r.external_installation_id,
            "account_login": r.account_login,
            "status": r.status,
            "permissions": r.permissions_json,
        }
        for r in rows
    ]


@router.post("/installations/github", status_code=201)
def api_bind_github_installation(
    body: BindInstallationRequest,
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("repo:connect")),
    db: Session = Depends(get_db, scope="function"),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    ctx, tenant, _mem = perm
    inst = bind_github_installation(
        db,
        tenant_id=tenant.id,
        actor_user_id=ctx.user.id,
        external_installation_id=body.external_installation_id,
        account_login=body.account_login,
        ownership_proof=body.ownership_proof,
        access_token=body.access_token,
        cipher=runtime.cipher,
        permissions=body.permissions,
    )
    return {
        "id": inst.id,
        "external_installation_id": inst.external_installation_id,
        "account_login": inst.account_login,
        "status": inst.status,
    }


@router.get("/repos")
def api_list_repos(
    project_id: str | None = Query(default=None),
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("repo:read")),
    db: Session = Depends(get_db, scope="function"),
) -> list[dict[str, Any]]:
    _ctx, tenant, _mem = perm
    return [
        {
            "id": r.id,
            "project_id": r.project_id,
            "installation_id": r.installation_id,
            "provider": r.provider,
            "full_name": r.full_name,
            "default_branch": r.default_branch,
            "support_profile": r.support_profile,
            "status": r.status,
            "validation_report": r.validation_report_json,
        }
        for r in list_repositories(db, tenant_id=tenant.id, project_id=project_id)
    ]


@router.post("/repos", status_code=201)
def api_connect_repo(
    body: ConnectRepoRequest,
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("repo:connect")),
    db: Session = Depends(get_db, scope="function"),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    ctx, tenant, _mem = perm
    res = connect_repository(
        db,
        tenant_id=tenant.id,
        actor_user_id=ctx.user.id,
        project_id=body.project_id,
        provider=body.provider,
        full_name=body.full_name,
        clone_url_or_path=body.clone_url_or_path,
        default_branch=body.default_branch,
        installation_id=body.installation_id,
        allow_local_repos=runtime.settings.allow_local_git_repos,
        object_store=runtime.object_store,
        worktree_root=Path(runtime.settings.worktree_root),
    )
    repo = res["repository"]
    snap = res["snapshot"]
    return {
        "id": repo.id,
        "project_id": repo.project_id,
        "full_name": repo.full_name,
        "support_profile": repo.support_profile,
        "status": repo.status,
        "snapshot_id": snap.id,
        "commit_sha": snap.commit_sha,
        "report": res["report"],
    }


@router.post("/repos/from-template", status_code=201)
def api_scaffold_template(
    body: ScaffoldTemplateRequest,
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("repo:connect")),
    db: Session = Depends(get_db, scope="function"),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    ctx, tenant, _mem = perm
    res = scaffold_from_template(
        db,
        tenant_id=tenant.id,
        actor_user_id=ctx.user.id,
        project_id=body.project_id,
        template_id=body.template_id,
        repo_name=body.repo_name,
        object_store=runtime.object_store,
        worktree_root=Path(runtime.settings.worktree_root),
        run_baseline=body.run_baseline,
    )
    repo = res["repository"]
    snap = res["snapshot"]
    return {
        "id": repo.id,
        "project_id": repo.project_id,
        "full_name": repo.full_name,
        "support_profile": repo.support_profile,
        "status": repo.status,
        "snapshot_id": snap.id,
        "commit_sha": snap.commit_sha,
        "report": res["report"],
        "baseline": res["baseline"],
    }


@router.post("/repos/{repo_id}/refresh")
def api_refresh_repo_snapshot(
    repo_id: str,
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("repo:connect")),
    db: Session = Depends(get_db, scope="function"),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    _ctx, tenant, _mem = perm
    res = refresh_repository_snapshot(
        db,
        tenant_id=tenant.id,
        repo_id=repo_id,
        allow_local_repos=runtime.settings.allow_local_git_repos,
        object_store=runtime.object_store,
        worktree_root=Path(runtime.settings.worktree_root),
    )
    snap = res["snapshot"]
    return {
        "repo_id": repo_id,
        "snapshot_id": snap.id,
        "commit_sha": snap.commit_sha,
        "tree_sha256": snap.tree_sha256,
    }


@router.get("/repos/{repo_id}/context")
def api_get_repo_context(
    repo_id: str,
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("repo:read")),
    db: Session = Depends(get_db, scope="function"),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    _ctx, tenant, _mem = perm
    snap, ctx_data = load_snapshot_repo_context(
        db,
        tenant_id=tenant.id,
        repo_id=repo_id,
        object_store=runtime.object_store,
    )
    return {"snapshot_id": snap.id, "commit_sha": snap.commit_sha, "repo_context": ctx_data}


class CreateMemoryRequest(BaseModel):
    lesson_text: str = Field(min_length=5, max_length=2000)
    source_run_id: str = "manual"
    auto_approve: bool = False


@router.get("/projects/{project_id}/memories")
def api_list_project_memories(
    project_id: str,
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("repo:read")),
    db: Session = Depends(get_db, scope="function"),
) -> list[dict[str, Any]]:
    _ctx, tenant, _mem = perm
    return [
        {
            "id": m.id,
            "project_id": m.project_id,
            "lesson_text": m.lesson_text,
            "source_run_id": m.source_run_id,
            "approved_by_user_id": m.approved_by_user_id,
            "created_at": m.created_at.isoformat(),
        }
        for m in list_tenant_memories(db, tenant_id=tenant.id, project_id=project_id)
    ]


@router.post("/projects/{project_id}/memories", status_code=201)
def api_create_project_memory(
    project_id: str,
    body: CreateMemoryRequest,
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("task:create")),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    ctx, tenant, _mem = perm
    m = create_tenant_memory(
        db,
        tenant_id=tenant.id,
        project_id=project_id,
        source_run_id=body.source_run_id,
        lesson_text=body.lesson_text,
        approved_by_user_id=ctx.user.id if body.auto_approve else None,
    )
    return {
        "id": m.id,
        "project_id": m.project_id,
        "lesson_text": m.lesson_text,
        "approved_by_user_id": m.approved_by_user_id,
    }


@router.post("/projects/{project_id}/memories/{memory_id}/approve")
def api_approve_project_memory(
    project_id: str,
    memory_id: str,
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("spec:approve")),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    ctx, tenant, _mem = perm
    m = approve_tenant_memory(
        db,
        tenant_id=tenant.id,
        project_id=project_id,
        memory_id=memory_id,
        actor_user_id=ctx.user.id,
    )
    return {"id": m.id, "approved_by_user_id": m.approved_by_user_id}


@router.delete("/projects/{project_id}/memories/{memory_id}")
def api_delete_project_memory(
    project_id: str,
    memory_id: str,
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("org:manage")),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, bool]:
    ctx, tenant, _mem = perm
    delete_tenant_memory(
        db,
        tenant_id=tenant.id,
        project_id=project_id,
        memory_id=memory_id,
        actor_user_id=ctx.user.id,
    )
    return {"deleted": True}
