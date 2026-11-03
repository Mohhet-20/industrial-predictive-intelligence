from fastapi import APIRouter, Depends, Query

from sqlalchemy.orm import Session

from app.core.deps import organization_scope
from app.db.session import get_db
from app.models.data_quality import DataQualityIssueSeverity
from app.schemas.common import Page
from app.schemas.data_quality import DataQualityIssueResponse, DataQualitySummary
from app.services import quality_service

router = APIRouter(prefix="/data-quality", tags=["data-quality"])


@router.get("/issues", response_model=Page[DataQualityIssueResponse])
def list_issues(
    asset_id: str | None = Query(default=None),
    sensor_id: str | None = Query(default=None),
    severity: DataQualityIssueSeverity | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    org_id: str = Depends(organization_scope),
    db: Session = Depends(get_db),
) -> Page[DataQualityIssueResponse]:
    items, total = quality_service.list_issues(
        db, organization_id=org_id, asset_id=asset_id, sensor_id=sensor_id, severity=severity, limit=limit, offset=offset
    )
    return Page(items=[DataQualityIssueResponse.model_validate(i) for i in items], total=total, limit=limit, offset=offset)


@router.get("/summary/{asset_id}", response_model=DataQualitySummary)
def summary_for_asset(asset_id: str, org_id: str = Depends(organization_scope), db: Session = Depends(get_db)) -> DataQualitySummary:
    return quality_service.summarize_for_asset(db, organization_id=org_id, asset_id=asset_id)
