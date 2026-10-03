"""Application error types.

Every user-facing failure is expressed as an AppError subclass and rendered
by a single FastAPI exception handler as:

    {"error": {"code": "...", "message": "...", "field": "...", "details": [...]}}
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    status_code = 400
    code = "app_error"

    def __init__(
        self,
        message: str,
        *,
        field: str | None = None,
        details: list[dict[str, Any]] | None = None,
        code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.field = field
        self.details = details or []
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code


class ValidationAppError(AppError):
    """Invalid input from the client (HTTP 422, FastAPI convention)."""

    status_code = 422
    code = "validation_error"


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class GoneError(AppError):
    """The clip file has expired (retention) although the record persists."""

    status_code = 410
    code = "clip_expired"


class RateLimitError(AppError):
    status_code = 429
    code = "rate_limited"


class PayloadTooLargeError(AppError):
    status_code = 413
    code = "payload_too_large"
