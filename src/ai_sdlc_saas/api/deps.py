"""FastAPI dependencies for runtime access, DB session lifecycle, auth, and RBAC."""

from __future__ import annotations

from collections.abc import Callable, Iterator

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from ai_sdlc_saas.api.runtime import AppRuntime
from ai_sdlc_saas.db.engine import set_session_tenant
from ai_sdlc_saas.db.models import Membership, Tenant
from ai_sdlc_saas.errors import AuthError
from ai_sdlc_saas.identity.service import AuthContext, authenticate_jwt


def get_runtime(request: Request) -> AppRuntime:
    runtime: AppRuntime = request.app.state.runtime
    return runtime


def get_db(request: Request) -> Iterator[Session]:
    runtime = get_runtime(request)
    session = runtime.session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        try:
            set_session_tenant(session, None)
        except Exception:
            pass
        session.close()


def _extract_bearer_token(request: Request, *, allow_query_token: bool = False) -> str:
    auth_header = request.headers.get("Authorization", "").strip()
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        if token:
            return token
    if allow_query_token:
        q_token = request.query_params.get("token", "").strip()
        if q_token:
            return q_token
    raise AuthError("Missing bearer token.")


def get_auth_context(
    request: Request,
    db: Session = Depends(get_db, scope="function"),
    runtime: AppRuntime = Depends(get_runtime),
) -> AuthContext:
    allow_query = request.url.path.endswith("/events")
    token = _extract_bearer_token(request, allow_query_token=allow_query)
    ctx = authenticate_jwt(db, token=token, jwt_secret=runtime.settings.jwt_secret)
    request.state.auth_user_id = ctx.user.id
    if ctx.tenant is not None:
        request.state.auth_tenant_id = ctx.tenant.id
    return ctx


def require_permission(
    permission: str,
) -> Callable[[AuthContext], tuple[AuthContext, Tenant, Membership]]:
    def _checker(ctx: AuthContext = Depends(get_auth_context)) -> tuple[AuthContext, Tenant, Membership]:
        tenant, membership = ctx.require_permission(permission)
        return ctx, tenant, membership

    return _checker
