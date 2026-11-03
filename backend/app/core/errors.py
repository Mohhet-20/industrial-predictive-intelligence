"""Consistent error contract for the whole API.

Clients always get `{"error": {"code", "message", "request_id", "details"?}}`.
Internal exceptions (bugs, DB errors) are logged with full detail server-side
and returned to the client as a generic 500 — no stack traces, no internal
messages leak over the wire.
"""
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_correlation_id, get_logger

logger = get_logger("app.errors")


class AppError(Exception):
    """Base class for domain/application errors with a stable error code."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "app_error"

    def __init__(self, message: str, *, details: Optional[Any] = None):
        super().__init__(message)
        self.message = message
        self.details = details


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class ValidationFailedError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "validation_failed"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class AuthenticationError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "authentication_failed"


class AuthorizationError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "not_authorized"


class RateLimitedError(AppError):
    """Too many requests in the configured window (currently only used for
    /auth/login's brute-force guard). Distinct from a generic 400 so a
    client can tell "try again later" apart from "the request was wrong"."""

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"


class NotImplementedYetError(AppError):
    """Used for domain concepts that exist in the schema but have no
    working implementation yet (e.g. anomaly detection). Distinct from a
    generic 404 so the frontend can render an honest "not yet available"
    state instead of treating it as a broken request."""

    status_code = status.HTTP_501_NOT_IMPLEMENTED
    code = "not_yet_available"


def _error_body(code: str, message: str, details: Optional[Any] = None) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
            "request_id": get_correlation_id(),
        }
    }
    if details is not None:
        body["error"]["details"] = jsonable_encoder(details)
    return body


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=_error_body(
                "request_validation_failed",
                "The request did not match the expected schema.",
                details=exc.errors(),
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body("http_error", str(exc.detail)),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "Unhandled exception",
            extra={"extra_fields": {"path": str(request.url), "correlation_id": get_correlation_id()}},
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_error_body(
                "internal_error", "An unexpected error occurred. It has been logged."
            ),
        )
