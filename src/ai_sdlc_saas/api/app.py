"""FastAPI application factory with middleware, security headers, and error envelopes."""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from ai_sdlc_saas.api.routers import (
    approvals,
    auth,
    billing,
    delivery,
    governance,
    health,
    models,
    orgs,
    repos,
    sse,
    support,
    tasks,
    webhooks,
)
from ai_sdlc_saas.api.runtime import AppRuntime
from ai_sdlc_saas.config import Settings
from ai_sdlc_saas.errors import AppError
from ai_sdlc_saas.governance.observability import record_http_metric
from ai_sdlc_saas.security.redaction import redact_payload, redact_text

logger = logging.getLogger("ai_sdlc_saas.api")


def create_app(
    settings: Settings | None = None,
    *,
    runtime: AppRuntime | None = None,
    web_dist_dir: Path | None = None,
) -> FastAPI:
    """Create and configure the AI SDLC SaaS FastAPI application."""
    resolved_runtime = runtime or AppRuntime.from_settings(settings)
    app = FastAPI(
        title="AI SDLC SaaS Control Plane",
        version="0.1.0",
        description="Multi-tenant control plane for autonomous SDLC specification, critique, coding, verification, and PR delivery.",
    )
    app.state.runtime = resolved_runtime

    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_runtime.settings.parsed_cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_context_and_security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        req_id = request.headers.get("X-Request-Id") or f"req_{secrets.token_hex(8)}"
        request.state.request_id = req_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
            logger.exception(
                "unhandled_error",
                extra={"request_id": req_id, "path": request.url.path, "duration_ms": duration_ms},
            )
            return JSONResponse(
                status_code=500,
                content={
                    "error": {
                        "code": "internal_error",
                        "message": "An unexpected internal error occurred.",
                        "details": {},
                        "request_id": req_id,
                    }
                },
                headers={"X-Request-Id": req_id},
            )

        duration_seconds = time.perf_counter() - started
        route_obj = request.scope.get("route")
        route_tpl = getattr(route_obj, "path", request.url.path.split("/")[1] if "/" in request.url.path else "other")
        record_http_metric(
            method=request.method,
            route=route_tpl,
            status_code=response.status_code,
            duration_seconds=duration_seconds,
        )
        response.headers["X-Request-Id"] = req_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        req_id = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": redact_text(exc.message),
                    "details": redact_payload(exc.details),
                    "request_id": req_id,
                }
            },
            headers={"X-Request-Id": req_id} if req_id else None,
        )

    app.include_router(health.router)
    app.include_router(health.router, prefix="/api/v1")
    app.include_router(auth.router)
    app.include_router(orgs.router)
    app.include_router(repos.router)
    app.include_router(models.router)
    app.include_router(tasks.router)
    app.include_router(approvals.router)
    app.include_router(sse.router)
    app.include_router(webhooks.router)
    app.include_router(delivery.router)
    app.include_router(billing.router)
    app.include_router(governance.router)
    app.include_router(support.router)

    if web_dist_dir and web_dist_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(web_dist_dir), html=True), name="web")

    return app


def run_cli() -> None:
    """Console script entrypoint for running the AI SDLC FastAPI control plane."""
    import os
    from pathlib import Path

    import uvicorn

    dist = Path("apps/web/dist")
    uvicorn.run(
        create_app(web_dist_dir=dist if dist.is_dir() else None),
        host=os.getenv("AI_SDLC_HOST", "0.0.0.0"),
        port=int(os.getenv("AI_SDLC_PORT", "8000")),
    )
