"""BYOK credentials, model routes, capability probes, role routing policies, and usage metering."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_sdlc_saas.api.deps import get_db, get_runtime, require_permission
from ai_sdlc_saas.api.runtime import AppRuntime
from ai_sdlc_saas.db.models import Membership, ModelPolicy, RequestUsage, Tenant
from ai_sdlc_saas.gateway.service import (
    create_credential,
    create_model_route,
    list_credentials,
    list_model_routes,
    probe_model_route,
    revoke_credential,
    upsert_model_policy,
)

router = APIRouter(prefix="/api/v1", tags=["models"])


class CreateCredentialRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    provider_type: str = "openai_compatible"
    secret_value: str = Field(min_length=4)


class CreateRouteRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    provider: str = "openai_compatible"
    base_url: str
    model_id: str
    credential_ref_id: str | None = None
    rate_limit_rpm: int = Field(default=60, ge=1, le=5000)
    max_concurrency: int = Field(default=4, ge=1, le=64)
    input_cost_microcents_per_1k: int = Field(default=150, ge=1)
    output_cost_microcents_per_1k: int = Field(default=600, ge=1)


class UpsertPolicyRequest(BaseModel):
    project_id: str | None = None
    role_route_map: dict[str, str] = Field(default_factory=dict)
    compression_mode: str = "none"
    max_context_tokens: int = Field(default=32_000, ge=1024, le=1_000_000)
    max_output_tokens: int = Field(default=8_192, ge=256, le=65_536)
    per_task_budget_cents: int = Field(default=500, ge=1, le=100_000)


@router.get("/credentials")
def api_list_credentials(
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("model:read")),
    db: Session = Depends(get_db, scope="function"),
) -> list[dict[str, Any]]:
    _ctx, tenant, _mem = perm
    return [
        {
            "id": c.id,
            "name": c.name,
            "provider_type": c.provider_type,
            "masked_preview": c.masked_preview,
            "key_version": c.key_version,
            "revoked_at": c.revoked_at.isoformat() if c.revoked_at else None,
            "created_at": c.created_at.isoformat(),
        }
        for c in list_credentials(db, tenant_id=tenant.id)
    ]


@router.post("/credentials", status_code=201)
def api_create_credential(
    body: CreateCredentialRequest,
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("model:configure")),
    db: Session = Depends(get_db, scope="function"),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    ctx, tenant, _mem = perm
    cred = create_credential(
        db,
        tenant_id=tenant.id,
        actor_user_id=ctx.user.id,
        name=body.name,
        provider_type=body.provider_type,
        secret_value=body.secret_value,
        cipher=runtime.cipher,
    )
    return {
        "id": cred.id,
        "name": cred.name,
        "provider_type": cred.provider_type,
        "masked_preview": cred.masked_preview,
        "key_version": cred.key_version,
    }


@router.delete("/credentials/{credential_id}")
def api_revoke_credential(
    credential_id: str,
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("model:configure")),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    ctx, tenant, _mem = perm
    cred = revoke_credential(
        db,
        tenant_id=tenant.id,
        actor_user_id=ctx.user.id,
        credential_id=credential_id,
    )
    return {"id": cred.id, "revoked": True}


@router.get("/models/routes")
def api_list_routes(
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("model:read")),
    db: Session = Depends(get_db, scope="function"),
) -> list[dict[str, Any]]:
    _ctx, tenant, _mem = perm
    return [
        {
            "id": r.id,
            "name": r.name,
            "provider": r.provider,
            "base_url": r.base_url,
            "model_id": r.model_id,
            "credential_ref_id": r.credential_ref_id,
            "capabilities": r.capabilities_json,
            "is_enabled": r.is_enabled,
            "rate_limit_rpm": r.rate_limit_rpm,
            "max_concurrency": r.max_concurrency,
            "last_probed_at": r.last_probed_at.isoformat() if r.last_probed_at else None,
        }
        for r in list_model_routes(db, tenant_id=tenant.id)
    ]


@router.post("/models/routes", status_code=201)
def api_create_route(
    body: CreateRouteRequest,
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("model:configure")),
    db: Session = Depends(get_db, scope="function"),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    ctx, tenant, _mem = perm
    r = create_model_route(
        db,
        tenant_id=tenant.id,
        actor_user_id=ctx.user.id,
        name=body.name,
        provider=body.provider,
        base_url=body.base_url,
        model_id=body.model_id,
        credential_ref_id=body.credential_ref_id,
        allow_local_gateway=runtime.settings.allow_local_gateway,
        rate_limit_rpm=body.rate_limit_rpm,
        max_concurrency=body.max_concurrency,
        input_cost_microcents_per_1k=body.input_cost_microcents_per_1k,
        output_cost_microcents_per_1k=body.output_cost_microcents_per_1k,
    )
    return {
        "id": r.id,
        "name": r.name,
        "provider": r.provider,
        "base_url": r.base_url,
        "model_id": r.model_id,
        "is_enabled": r.is_enabled,
    }


@router.post("/models/routes/{route_id}/probe")
def api_probe_route(
    route_id: str,
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("model:configure")),
    db: Session = Depends(get_db, scope="function"),
    runtime: AppRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    _ctx, tenant, _mem = perm
    route = probe_model_route(
        db,
        tenant_id=tenant.id,
        route_id=route_id,
        cipher=runtime.cipher,
        allow_local_gateway=runtime.settings.allow_local_gateway,
    )
    return {
        "id": route.id,
        "capabilities": route.capabilities_json,
        "last_probed_at": route.last_probed_at.isoformat() if route.last_probed_at else None,
    }


@router.get("/models/policies")
def api_list_policies(
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("model:read")),
    db: Session = Depends(get_db, scope="function"),
) -> list[dict[str, Any]]:
    _ctx, tenant, _mem = perm
    rows = list(
        db.scalars(
            select(ModelPolicy)
            .where(ModelPolicy.tenant_id == tenant.id)
            .order_by(ModelPolicy.created_at.desc())
        )
    )
    return [
        {
            "id": p.id,
            "project_id": p.project_id,
            "role_route_map": p.role_route_map_json,
            "compression_mode": p.compression_mode,
            "max_context_tokens": p.max_context_tokens,
            "max_output_tokens": p.max_output_tokens,
            "per_task_budget_cents": p.per_task_budget_cents,
            "policy_digest": p.policy_digest,
        }
        for p in rows
    ]


@router.post("/models/policies", status_code=201)
def api_upsert_policy(
    body: UpsertPolicyRequest,
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("model:configure")),
    db: Session = Depends(get_db, scope="function"),
) -> dict[str, Any]:
    ctx, tenant, _mem = perm
    p = upsert_model_policy(
        db,
        tenant_id=tenant.id,
        actor_user_id=ctx.user.id,
        project_id=body.project_id,
        role_route_map=body.role_route_map,
        compression_mode=body.compression_mode,
        max_context_tokens=body.max_context_tokens,
        max_output_tokens=body.max_output_tokens,
        per_task_budget_cents=body.per_task_budget_cents,
    )
    return {
        "id": p.id,
        "project_id": p.project_id,
        "role_route_map": p.role_route_map_json,
        "compression_mode": p.compression_mode,
        "per_task_budget_cents": p.per_task_budget_cents,
        "policy_digest": p.policy_digest,
    }


@router.get("/usage/requests")
def api_list_request_usage(
    task_id: str | None = Query(default=None),
    perm: tuple[Any, Tenant, Membership] = Depends(require_permission("model:read")),
    db: Session = Depends(get_db, scope="function"),
) -> list[dict[str, Any]]:
    _ctx, tenant, _mem = perm
    stmt = select(RequestUsage).where(RequestUsage.tenant_id == tenant.id)
    if task_id:
        stmt = stmt.where(RequestUsage.task_id == task_id)
    rows = list(db.scalars(stmt.order_by(RequestUsage.created_at.desc())))
    return [
        {
            "id": u.id,
            "task_id": u.task_id,
            "run_id": u.run_id,
            "attempt_id": u.attempt_id,
            "role": u.role,
            "route_id": u.route_id,
            "actual_model_id": u.actual_model_id,
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cached_tokens": u.cached_tokens,
            "usage_status": u.usage_status,
            "upstream_cost_microcents": u.upstream_cost_microcents,
            "customer_cost_cents": u.customer_cost_cents,
        }
        for u in rows
    ]
