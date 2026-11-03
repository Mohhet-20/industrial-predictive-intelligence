from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.deps import organization_scope, require_roles
from app.db.session import get_db
from app.models.user import User, UserRole
from app.services import system_service

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/metrics")
def get_metrics(
    org_id: str = Depends(organization_scope),
    _user: User = Depends(require_roles(UserRole.ADMIN, UserRole.ENGINEER)),
    db: Session = Depends(get_db),
) -> dict:
    """Real, measured operational metrics for this organization — request
    volume/latency, background job outcomes, ingestion/data-quality/anomaly
    counts. Never fabricated (see app.services.system_service). Restricted
    to admin/engineer: this is operational detail, not something a viewer
    role needs, and it is somewhat more revealing about system internals
    than the rest of the read-only API."""
    return system_service.get_operational_metrics(db, organization_id=org_id)
