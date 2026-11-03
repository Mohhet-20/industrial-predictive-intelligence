"""Intelligence Orchestration API (Phase 6): unified timeline, evidence-
based prioritization, cross-layer asset summary, cross-sensor context, and
model/method provenance. Entirely read-only — every route here is a GET,
open to any authenticated organization member (the same read boundary as
app.api.v1.anomaly/condition/mlops draw for their own read-only routes);
nothing about this layer's own state can be written through this router
(investigation state is written through app.api.v1.investigations)."""
from datetime import datetime

from fastapi import APIRouter, Depends, Query

from sqlalchemy.orm import Session

from app.core.deps import organization_scope
from app.db.session import get_db
from app.schemas.orchestration import OrchestrationEnvelope
from app.services import orchestration_service
from typing import Optional

router = APIRouter(prefix="/orchestration", tags=["orchestration"])


@router.get("/assets/{asset_id}/timeline", response_model=OrchestrationEnvelope)
def get_asset_timeline(
    asset_id: str,
    start: datetime = Query(...),
    end: datetime = Query(...),
    event_types: Optional[list[str]] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    org_id: str = Depends(organization_scope),
    db: Session = Depends(get_db),
) -> OrchestrationEnvelope:
    data = orchestration_service.get_asset_intelligence_timeline(
        db, organization_id=org_id, asset_id=asset_id, start=start, end=end, event_types=event_types, limit=limit, offset=offset
    )
    return OrchestrationEnvelope(data=data)


@router.get("/prioritized-signals", response_model=OrchestrationEnvelope)
def get_prioritized_signals(
    limit: int = Query(default=50, ge=1, le=200),
    org_id: str = Depends(organization_scope),
    db: Session = Depends(get_db),
) -> OrchestrationEnvelope:
    data = orchestration_service.get_prioritized_signals(db, organization_id=org_id, limit=limit)
    return OrchestrationEnvelope(data=data)


@router.get("/assets/{asset_id}/summary", response_model=OrchestrationEnvelope)
def get_asset_summary(asset_id: str, org_id: str = Depends(organization_scope), db: Session = Depends(get_db)) -> OrchestrationEnvelope:
    data = orchestration_service.get_asset_intelligence_summary(db, organization_id=org_id, asset_id=asset_id)
    return OrchestrationEnvelope(data=data)


@router.get("/assets/{asset_id}/cross-sensor-context", response_model=OrchestrationEnvelope)
def get_cross_sensor_context(
    asset_id: str,
    sensor_ids: list[str] = Query(...),
    start: datetime = Query(...),
    end: datetime = Query(...),
    freq_seconds: float = Query(default=300.0, gt=0),
    org_id: str = Depends(organization_scope),
    db: Session = Depends(get_db),
) -> OrchestrationEnvelope:
    data = orchestration_service.get_cross_sensor_context(
        db, organization_id=org_id, asset_id=asset_id, sensor_ids=sensor_ids, start=start, end=end, freq_seconds=freq_seconds
    )
    return OrchestrationEnvelope(data=data)


@router.get("/evidence/{evidence_type}/{evidence_id}/provenance", response_model=OrchestrationEnvelope)
def get_evidence_provenance(
    evidence_type: str, evidence_id: str, org_id: str = Depends(organization_scope), db: Session = Depends(get_db)
) -> OrchestrationEnvelope:
    data = orchestration_service.get_evidence_provenance(db, organization_id=org_id, evidence_type=evidence_type, evidence_id=evidence_id)
    return OrchestrationEnvelope(data=data)
