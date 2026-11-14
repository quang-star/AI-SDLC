"""Liveness and readiness probes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from ai_sdlc_saas.api.deps import get_db, get_runtime
from ai_sdlc_saas.api.runtime import AppRuntime
from ai_sdlc_saas.api.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=HealthResponse)
def healthz(runtime: AppRuntime = Depends(get_runtime)) -> HealthResponse:
    return HealthResponse(
        status="ok",
        env=runtime.settings.env,
        database="unverified",
        object_store="unverified",
    )


@router.get("/readyz", response_model=HealthResponse)
def readyz(
    runtime: AppRuntime = Depends(get_runtime),
    db: Session = Depends(get_db, scope="function"),
) -> HealthResponse:
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Database not ready.") from exc

    store_ok = runtime.object_store.base_dir.exists()
    if not store_ok:
        raise HTTPException(status_code=503, detail="Object store directory not ready.")

    return HealthResponse(
        status="ready",
        env=runtime.settings.env,
        database="ok",
        object_store="ok",
    )
