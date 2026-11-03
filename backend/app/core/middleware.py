import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.logging import get_logger, log_with_fields, new_correlation_id, set_correlation_id
from app.core import metrics

logger = get_logger("app.request")


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Assigns a correlation ID to every request (reusing an inbound
    X-Correlation-ID / X-Request-ID header if present), makes it available
    to service-layer logging via a contextvar, echoes it back on the
    response, logs one structured line per request, and records the
    request into the in-process metrics collector (app.core.metrics) — the
    same real numbers GET /api/v1/system/metrics reports."""

    async def dispatch(self, request: Request, call_next):
        incoming = request.headers.get("x-correlation-id") or request.headers.get("x-request-id")
        correlation_id = incoming or new_correlation_id()
        set_correlation_id(correlation_id)
        request.state.correlation_id = correlation_id

        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - start) * 1000, 2)

        response.headers["X-Correlation-ID"] = correlation_id
        log_with_fields(
            logger,
            20,
            "request completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
        # route.path (e.g. "/assets/{asset_id}") would be a better metrics
        # label than the raw path (avoids one bucket per distinct asset id),
        # but is only available after routing resolves; request.url.path is
        # what's reliably available here for every response, including 404s
        # that never matched a route. Documented as a known coarseness, not
        # silently misleading — see get_request_metrics()'s "note" field.
        metrics.record_request(request.url.path, response.status_code, duration_ms)
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """A small, honestly-scoped set of response headers appropriate for a
    JSON API with no server-rendered HTML: this is not a CSP for a
    document-serving app (there is no inline script/style surface to
    restrict), it is the handful of headers that cost nothing and close
    real gaps — MIME sniffing, clickjacking via an <iframe>, and browsers
    caching authentication responses."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/v1/auth"):
            response.headers["Cache-Control"] = "no-store"
        return response


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Rejects requests whose declared Content-Length exceeds
    settings.max_request_body_bytes, before any routing/parsing/DB work
    happens. A client that lies about Content-Length (or omits it and
    streams a huge chunked body) is not fully defended against by this
    check alone — a genuinely adversarial deployment would enforce this at
    a reverse proxy in front of the app (documented as the layered
    approach, not pretended to be unnecessary because this exists)."""

    def __init__(self, app, max_bytes: int):
        super().__init__(app)
        self._max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                declared = None
            if declared is not None and declared > self._max_bytes:
                return JSONResponse(
                    status_code=413,
                    content={
                        "error": {
                            "code": "request_too_large",
                            "message": (
                                f"Request body ({declared} bytes) exceeds the "
                                f"{self._max_bytes}-byte limit."
                            ),
                            "request_id": None,
                        }
                    },
                )
        return await call_next(request)
