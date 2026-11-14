"""Shared domain exception hierarchy mapped cleanly to FastAPI HTTP responses."""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base application error carrying stable error code and HTTP status."""

    status_code: int = 400
    code: str = "app_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.details = details or {}


class AuthError(AppError):
    status_code = 401
    code = "unauthorized"


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class ValidationError(AppError):
    status_code = 422
    code = "validation_error"


class RateLimitError(AppError):
    status_code = 429
    code = "rate_limited"


class BudgetExceededError(AppError):
    status_code = 402
    code = "budget_exceeded"


class UnsupportedProfileError(AppError):
    status_code = 422
    code = "unsupported_profile"


class BaselineStaleError(AppError):
    status_code = 409
    code = "baseline_stale"


class ServiceUnavailableError(AppError):
    status_code = 503
    code = "service_unavailable"
