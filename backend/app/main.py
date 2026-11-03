from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import BodySizeLimitMiddleware, CorrelationIdMiddleware, SecurityHeadersMiddleware

settings = get_settings()
configure_logging(settings.log_level)
logger = get_logger("app.main")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    from app.jobs.handlers import register_job_handlers

    register_job_handlers()
    logger.info("Application startup complete", extra={"extra_fields": {"env": settings.app_env}})
    yield


app = FastAPI(
    lifespan=lifespan,
    title="Industrial AI Predictive Intelligence Platform",
    description=(
        "Industrial equipment monitoring and decision-support platform: "
        "ingestion, data quality, time-series intelligence, anomaly and "
        "condition detection, MLOps/governance, unified investigation, and "
        "production reliability/observability foundations. Every AI-derived "
        "output is reviewable evidence for a human, never an autonomous "
        "action — see /api/v1/health and /api/v1/system/metrics for "
        "operational status."
    ),
    version="0.7.0",
)

# Order (Starlette applies these outside-in for the request, inside-out for
# the response): body-size rejection happens before anything else does real
# work; security headers and CORS wrap every response including error
# responses; correlation-id/metrics wraps the actual handler so its timing
# reflects real handler duration.
app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(CorrelationIdMiddleware)

register_exception_handlers(app)
app.include_router(api_router, prefix=settings.api_v1_prefix)


@app.get("/")
def root() -> dict:
    return {
        "name": "Industrial AI Predictive Intelligence Platform",
        "environment": settings.app_env,
        "docs": "/docs",
        "health": "/api/v1/health",
        "readiness": "/api/v1/health/ready",
        "metrics": "/api/v1/system/metrics",
    }
