from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_db

router = APIRouter(tags=["system"])


@router.get("/health")
def liveness() -> dict:
    """Liveness: process is up and can respond. No dependency checks —
    that's what /health/ready is for."""
    return {"status": "ok"}


@router.get("/health/ready")
def readiness(db: Session = Depends(get_db)) -> dict:
    """Readiness: can the app actually serve requests right now? Checks the
    one real critical dependency this foundation has — the database — with
    a cheap `SELECT 1`, not an expensive query or an ML operation (the design
    is explicit that health checks must never do model loading/inference).
    Also reports the applied Alembic schema revision as a non-blocking
    diagnostic: a mismatch between this and the revision the running code
    expects is a common real deployment failure (migrations not run before
    the new code started) and is worth surfacing even though it does not by
    itself make the process "not ready" to answer non-schema-dependent
    requests."""
    settings = get_settings()
    checks: dict[str, str] = {}
    overall = "ok"

    try:
        db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["database"] = f"error: {exc}"
        overall = "degraded"

    schema_version: str | None = None
    try:
        row = db.execute(text("SELECT version_num FROM alembic_version")).first()
        schema_version = row[0] if row else None
        if schema_version is None:
            checks["schema_version"] = "no alembic_version row — migrations likely never applied"
            overall = "degraded"
        else:
            checks["schema_version"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["schema_version"] = f"error: {exc}"
        overall = "degraded"

    return {
        "status": overall,
        "environment": settings.app_env,
        "schema_version": schema_version,
        "checks": checks,
    }
