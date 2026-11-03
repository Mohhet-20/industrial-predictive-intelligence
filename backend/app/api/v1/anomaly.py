"""Anomaly Detection Intelligence API.

Every route is organization-scoped (org_id = Depends(organization_scope))
and every service call re-validates that scope server-side — the same
tenant-isolation discipline as app.api.v1.timeseries. Detector training
and detection-run creation require ADMIN or ENGINEER (they train models
and create persistent Anomaly rows); human review actions require the
same, since a VIEWER should be able to see and investigate anomalies but
not change their reviewed status. Everything else is read-only for any
authenticated org member.

Detection runs follow the same sync-for-small/async-for-large split as
POST /timeseries/datasets (see app.services.anomaly_service.
create_detection_run and app.services.feature_service.create_dataset_build)
— the route is declared `async def` for the same reason documented there.

No route here returns an internal model object (an IsolationForest
instance, a pandas DataFrame, a raw sklearn score array) — only the
JSON-serializable fields the response schemas declare.
"""
from fastapi import APIRouter, Depends, Query

from sqlalchemy.orm import Session

from app.core.deps import organization_scope, require_roles
from app.core.logging import get_correlation_id
from app.db.session import get_db
from app.models.intelligence import AnomalySeverityTier, AnomalyStatus
from app.models.user import User, UserRole
from app.schemas.anomaly import (
    AnomalyContextResponse,
    AnomalyInvestigationResponse,
    AnomalyReviewRequest,
    AnomalyResponse,
    DetectionRunCreateRequest,
    DetectionRunResponse,
    DetectorCreateRequest,
    DetectorResponse,
    EvaluationResponse,
)
from app.schemas.common import Page
from app.schemas.timeseries import AnalysisEnvelope
from app.services import anomaly_service
from datetime import datetime, timedelta
from typing import Optional

router = APIRouter(prefix="/anomaly", tags=["anomaly"])

_WRITE_ROLES = (UserRole.ADMIN, UserRole.ENGINEER)


# ---------------------------------------------------------------------------
# Detectors (ModelVersion) — train, list, inspect, evaluate
# ---------------------------------------------------------------------------


@router.post("/detectors", response_model=DetectorResponse, status_code=201)
def create_detector(
    payload: DetectorCreateRequest,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    db: Session = Depends(get_db),
) -> DetectorResponse:
    """Trains synchronously (mirrors POST /timeseries/features — computing
    a baseline/reference summary over a bounded reference period is fast
    enough not to need the job runner). A FAILED status in the response
    (not an HTTP error) means training ran but the reference period didn't
    support it — e.g. too few observations; see `training_error`."""
    detector = anomaly_service.create_model_version(
        db,
        organization_id=org_id,
        user_id=user.id,
        name=payload.name,
        detector_type=payload.detector_type,
        sensor_ids=payload.sensor_ids,
        reference_start=payload.reference_start,
        reference_end=payload.reference_end,
        threshold_overrides=payload.threshold_overrides,
        feature_specs_raw=payload.feature_specs,
        step_seconds=payload.step_seconds,
    )
    return DetectorResponse.model_validate(detector)


@router.get("/detectors", response_model=Page[DetectorResponse])
def list_detectors(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    org_id: str = Depends(organization_scope),
    db: Session = Depends(get_db),
) -> Page[DetectorResponse]:
    rows, total = anomaly_service.list_model_versions(db, organization_id=org_id, limit=limit, offset=offset)
    return Page(items=[DetectorResponse.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/detectors/{detector_id}", response_model=DetectorResponse)
def get_detector(detector_id: str, org_id: str = Depends(organization_scope), db: Session = Depends(get_db)) -> DetectorResponse:
    detector = anomaly_service.get_model_version(db, organization_id=org_id, model_version_id=detector_id)
    return DetectorResponse.model_validate(detector)


@router.post("/detectors/{detector_id}/evaluate", response_model=EvaluationResponse)
def evaluate_detector(
    detector_id: str,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    db: Session = Depends(get_db),
) -> EvaluationResponse:
    """Runs the deterministic synthetic scenario suite through a detector
    configured like this one and stores the result on the detector
    (GET /detectors/{id} then includes it as `evaluation_results`). See
    app.services.anomaly_service.run_synthetic_evaluation's docstring —
    this validates the detection PIPELINE against seeded synthetic data,
    never real-world accuracy."""
    result = anomaly_service.run_synthetic_evaluation(db, organization_id=org_id, model_version_id=detector_id)
    return EvaluationResponse(data=result)


# ---------------------------------------------------------------------------
# Detection runs (ModelRun)
# ---------------------------------------------------------------------------


@router.post("/detectors/{detector_id}/runs", response_model=DetectionRunResponse, status_code=201)
async def create_detection_run(
    detector_id: str,
    payload: DetectionRunCreateRequest,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    db: Session = Depends(get_db),
) -> DetectionRunResponse:
    run = anomaly_service.create_detection_run(
        db,
        organization_id=org_id,
        user_id=user.id,
        model_version_id=detector_id,
        scoring_start=payload.scoring_start,
        scoring_end=payload.scoring_end,
        allow_retrospective_scoring=payload.allow_retrospective_scoring,
        correlation_id=get_correlation_id(),
    )
    return DetectionRunResponse.model_validate(run)


@router.get("/runs", response_model=Page[DetectionRunResponse])
def list_detection_runs(
    detector_id: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    org_id: str = Depends(organization_scope),
    db: Session = Depends(get_db),
) -> Page[DetectionRunResponse]:
    rows, total = anomaly_service.list_model_runs(db, organization_id=org_id, model_version_id=detector_id, limit=limit, offset=offset)
    return Page(items=[DetectionRunResponse.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/runs/{run_id}", response_model=DetectionRunResponse)
def get_detection_run(run_id: str, org_id: str = Depends(organization_scope), db: Session = Depends(get_db)) -> DetectionRunResponse:
    run = anomaly_service.get_model_run(db, organization_id=org_id, run_id=run_id)
    return DetectionRunResponse.model_validate(run)


# ---------------------------------------------------------------------------
# Anomalies — list/filter, detail, temporal context, review, audit history
# ---------------------------------------------------------------------------


@router.get("/anomalies", response_model=Page[AnomalyResponse])
def list_anomalies(
    asset_id: Optional[str] = Query(default=None),
    sensor_id: Optional[str] = Query(default=None),
    status: Optional[AnomalyStatus] = Query(default=None),
    severity_tier: Optional[AnomalySeverityTier] = Query(default=None),
    start: Optional[datetime] = Query(default=None),
    end: Optional[datetime] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    org_id: str = Depends(organization_scope),
    db: Session = Depends(get_db),
) -> Page[AnomalyResponse]:
    rows, total = anomaly_service.list_anomalies(
        db, organization_id=org_id, asset_id=asset_id, sensor_id=sensor_id, status=status, severity_tier=severity_tier,
        start=start, end=end, limit=limit, offset=offset,
    )
    return Page(items=[AnomalyResponse.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/anomalies/{anomaly_id}", response_model=AnomalyResponse)
def get_anomaly(anomaly_id: str, org_id: str = Depends(organization_scope), db: Session = Depends(get_db)) -> AnomalyResponse:
    anomaly = anomaly_service.get_anomaly(db, organization_id=org_id, anomaly_id=anomaly_id)
    return AnomalyResponse.model_validate(anomaly)


@router.get("/anomalies/{anomaly_id}/context", response_model=AnomalyContextResponse)
def get_anomaly_context(
    anomaly_id: str,
    context_hours: float = Query(default=6.0, gt=0, le=168),
    org_id: str = Depends(organization_scope),
    db: Session = Depends(get_db),
) -> AnomalyContextResponse:
    data = anomaly_service.get_anomaly_context(
        db, organization_id=org_id, anomaly_id=anomaly_id, context_buffer=timedelta(hours=context_hours)
    )
    return AnomalyContextResponse(data=data)


@router.post("/anomalies/{anomaly_id}/review", response_model=AnomalyResponse)
def review_anomaly(
    anomaly_id: str,
    payload: AnomalyReviewRequest,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    db: Session = Depends(get_db),
) -> AnomalyResponse:
    anomaly = anomaly_service.transition_anomaly_status(
        db, organization_id=org_id, anomaly_id=anomaly_id, user=user, to_status=payload.to_status, notes=payload.notes
    )
    return AnomalyResponse.model_validate(anomaly)


@router.get("/anomalies/{anomaly_id}/investigations", response_model=list[AnomalyInvestigationResponse])
def list_anomaly_investigations(
    anomaly_id: str, org_id: str = Depends(organization_scope), db: Session = Depends(get_db)
) -> list[AnomalyInvestigationResponse]:
    rows = anomaly_service.list_anomaly_investigations(db, organization_id=org_id, anomaly_id=anomaly_id)
    return [AnomalyInvestigationResponse.model_validate(r) for r in rows]


# ---------------------------------------------------------------------------
# Minimal detector/model monitoring (spec section 18)
# ---------------------------------------------------------------------------


@router.get("/monitoring", response_model=AnalysisEnvelope)
def get_monitoring_summary(org_id: str = Depends(organization_scope), db: Session = Depends(get_db)) -> AnalysisEnvelope:
    return AnalysisEnvelope(data=anomaly_service.get_monitoring_summary(db, organization_id=org_id))
