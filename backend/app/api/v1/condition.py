"""Equipment Condition Intelligence & Forecasting API (Phase 4).

Supersedes and replaces the old `/intelligence/predictions` 501 stub
(app.api.v1.intelligence, removed in this phase — Phase 3's anomaly
detection had already superseded that router's other routes, and Phase
4's forecasting work is what the `Prediction` table and this stub were
always reserved for; see app.models.condition's module docstring). Every
route here is organization-scoped and every service call
re-validates that scope server-side, the same tenant-isolation discipline
as app.api.v1.anomaly. Condition-run creation, forecast fitting/running,
and review actions require ADMIN or ENGINEER; everything else is
read-only for any authenticated org member.

All condition/degradation/forecast computation here is synchronous — see
app.services.condition_service's module docstring for why no background-
job split is needed, unlike POST /anomaly/detectors/{id}/runs.
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import organization_scope, require_roles
from app.db.session import get_db
from app.models.condition import ConditionSignalStatus, DegradationStage
from app.models.user import User, UserRole
from app.schemas.condition import (
    AttentionListResponse,
    ConditionEvidenceResponse,
    ConditionMonitoringResponse,
    ConditionReviewRequest,
    ConditionRunCreateRequest,
    ConditionRunResponse,
    ConditionValidateRequest,
    DegradationReviewResponse,
    DegradationSignalResponse,
    ForecastModelCreateRequest,
    ForecastModelResponse,
    ForecastRunCreateRequest,
    ForecastRunResponse,
)
from app.schemas.common import Page
from app.services import condition_service

router = APIRouter(prefix="/condition", tags=["condition"])

_WRITE_ROLES = (UserRole.ADMIN, UserRole.ENGINEER)


# ---------------------------------------------------------------------------
# Condition evidence (§2) — stateless
# ---------------------------------------------------------------------------


@router.get("/sensors/{sensor_id}/evidence", response_model=ConditionEvidenceResponse)
def get_condition_evidence(
    sensor_id: str,
    window_start: datetime = Query(...),
    window_end: datetime = Query(...),
    reference_start: datetime = Query(...),
    reference_end: datetime = Query(...),
    org_id: str = Depends(organization_scope),
    db: Session = Depends(get_db),
) -> ConditionEvidenceResponse:
    data = condition_service.get_condition_evidence(
        db, organization_id=org_id, sensor_id=sensor_id, window_start=window_start, window_end=window_end,
        reference_start=reference_start, reference_end=reference_end,
    )
    return ConditionEvidenceResponse(data=data)


# ---------------------------------------------------------------------------
# Degradation analysis runs & signals (§3, §12)
# ---------------------------------------------------------------------------


@router.post("/runs", response_model=ConditionRunResponse, status_code=201)
def create_condition_run(
    payload: ConditionRunCreateRequest,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    db: Session = Depends(get_db),
) -> ConditionRunResponse:
    run = condition_service.create_condition_run(
        db, organization_id=org_id, user_id=user.id, sensor_id=payload.sensor_id,
        analysis_start=payload.analysis_start, analysis_end=payload.analysis_end,
        reference_start=payload.reference_start, reference_end=payload.reference_end,
    )
    return ConditionRunResponse.model_validate(run)


@router.get("/runs", response_model=Page[ConditionRunResponse])
def list_condition_runs(
    sensor_id: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    org_id: str = Depends(organization_scope),
    db: Session = Depends(get_db),
) -> Page[ConditionRunResponse]:
    rows, total = condition_service.list_condition_runs(db, organization_id=org_id, sensor_id=sensor_id, limit=limit, offset=offset)
    return Page(items=[ConditionRunResponse.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/runs/{run_id}", response_model=ConditionRunResponse)
def get_condition_run(run_id: str, org_id: str = Depends(organization_scope), db: Session = Depends(get_db)) -> ConditionRunResponse:
    run = condition_service.get_condition_run(db, organization_id=org_id, run_id=run_id)
    return ConditionRunResponse.model_validate(run)


@router.get("/signals", response_model=Page[DegradationSignalResponse])
def list_degradation_signals(
    asset_id: Optional[str] = Query(default=None),
    sensor_id: Optional[str] = Query(default=None),
    stage: Optional[DegradationStage] = Query(default=None),
    status: Optional[ConditionSignalStatus] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    org_id: str = Depends(organization_scope),
    db: Session = Depends(get_db),
) -> Page[DegradationSignalResponse]:
    rows, total = condition_service.list_degradation_signals(
        db, organization_id=org_id, asset_id=asset_id, sensor_id=sensor_id, stage=stage, status=status, limit=limit, offset=offset,
    )
    return Page(items=[DegradationSignalResponse.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/signals/{signal_id}", response_model=DegradationSignalResponse)
def get_degradation_signal(signal_id: str, org_id: str = Depends(organization_scope), db: Session = Depends(get_db)) -> DegradationSignalResponse:
    signal = condition_service.get_degradation_signal(db, organization_id=org_id, signal_id=signal_id)
    return DegradationSignalResponse.model_validate(signal)


@router.post("/signals/{signal_id}/review", response_model=DegradationSignalResponse)
def review_degradation_signal(
    signal_id: str,
    payload: ConditionReviewRequest,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    db: Session = Depends(get_db),
) -> DegradationSignalResponse:
    signal = condition_service.transition_degradation_status(
        db, organization_id=org_id, signal_id=signal_id, user=user, to_status=payload.to_status, notes=payload.notes
    )
    return DegradationSignalResponse.model_validate(signal)


@router.post("/signals/{signal_id}/validate", response_model=DegradationSignalResponse)
def validate_degradation_signal(
    signal_id: str,
    payload: ConditionValidateRequest,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    db: Session = Depends(get_db),
) -> DegradationSignalResponse:
    """The only endpoint that can move a signal to VALIDATED_DEGRADATION —
    requires a non-empty engineering justification. See
    app.services.condition_service.validate_degradation_signal."""
    signal = condition_service.validate_degradation_signal(db, organization_id=org_id, signal_id=signal_id, user=user, notes=payload.notes)
    return DegradationSignalResponse.model_validate(signal)


@router.get("/signals/{signal_id}/reviews", response_model=list[DegradationReviewResponse])
def list_degradation_reviews(signal_id: str, org_id: str = Depends(organization_scope), db: Session = Depends(get_db)) -> list[DegradationReviewResponse]:
    rows = condition_service.list_degradation_reviews(db, organization_id=org_id, signal_id=signal_id)
    return [DegradationReviewResponse.model_validate(r) for r in rows]


# ---------------------------------------------------------------------------
# Forecasting (§4, §5-refusal) — fit always backtests; runs are gated
# ---------------------------------------------------------------------------


@router.post("/forecast-models", response_model=ForecastModelResponse, status_code=201)
def create_forecast_model(
    payload: ForecastModelCreateRequest,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    db: Session = Depends(get_db),
) -> ForecastModelResponse:
    """Always runs a leak-free holdout backtest against naive baselines as
    part of fitting (see app.condition.forecasting.backtest_and_compare).
    A NOT_JUSTIFIED status is a valid, honest outcome, not an error — the
    response's evaluation_results.justification_note explains why."""
    model = condition_service.create_forecast_model(
        db, organization_id=org_id, user_id=user.id, sensor_id=payload.sensor_id, name=payload.name,
        fit_start=payload.fit_start, fit_end=payload.fit_end, test_end=payload.test_end,
        seasonal_period_seconds=payload.seasonal_period_seconds,
    )
    return ForecastModelResponse.model_validate(model)


@router.get("/forecast-models", response_model=Page[ForecastModelResponse])
def list_forecast_models(
    sensor_id: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    org_id: str = Depends(organization_scope),
    db: Session = Depends(get_db),
) -> Page[ForecastModelResponse]:
    rows, total = condition_service.list_forecast_models(db, organization_id=org_id, sensor_id=sensor_id, limit=limit, offset=offset)
    return Page(items=[ForecastModelResponse.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/forecast-models/{model_id}", response_model=ForecastModelResponse)
def get_forecast_model(model_id: str, org_id: str = Depends(organization_scope), db: Session = Depends(get_db)) -> ForecastModelResponse:
    model = condition_service.get_forecast_model(db, organization_id=org_id, model_id=model_id)
    return ForecastModelResponse.model_validate(model)


@router.post("/forecast-models/{model_id}/runs", response_model=ForecastRunResponse, status_code=201)
def create_forecast_run(
    model_id: str,
    payload: ForecastRunCreateRequest,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    db: Session = Depends(get_db),
) -> ForecastRunResponse:
    """Refuses (409) if the forecast model's backtest was not JUSTIFIED —
    see app.services.condition_service.create_forecast_run."""
    run = condition_service.create_forecast_run(
        db, organization_id=org_id, user_id=user.id, forecast_model_id=model_id,
        as_of=payload.as_of, horizon_seconds=payload.horizon_seconds, step_seconds=payload.step_seconds,
    )
    return ForecastRunResponse.model_validate(run)


@router.get("/forecast-runs", response_model=Page[ForecastRunResponse])
def list_forecast_runs(
    forecast_model_id: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    org_id: str = Depends(organization_scope),
    db: Session = Depends(get_db),
) -> Page[ForecastRunResponse]:
    rows, total = condition_service.list_forecast_runs(db, organization_id=org_id, forecast_model_id=forecast_model_id, limit=limit, offset=offset)
    return Page(items=[ForecastRunResponse.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/forecast-runs/{run_id}", response_model=ForecastRunResponse)
def get_forecast_run(run_id: str, org_id: str = Depends(organization_scope), db: Session = Depends(get_db)) -> ForecastRunResponse:
    run = condition_service.get_forecast_run(db, organization_id=org_id, run_id=run_id)
    return ForecastRunResponse.model_validate(run)


# ---------------------------------------------------------------------------
# Risk prioritization (§11) & monitoring (§16)
# ---------------------------------------------------------------------------


@router.get("/attention-list", response_model=AttentionListResponse)
def get_attention_list(
    limit: int = Query(default=50, ge=1, le=200), org_id: str = Depends(organization_scope), db: Session = Depends(get_db)
) -> AttentionListResponse:
    return AttentionListResponse(data=condition_service.get_attention_list(db, organization_id=org_id, limit=limit))


@router.get("/monitoring", response_model=ConditionMonitoringResponse)
def get_condition_monitoring(org_id: str = Depends(organization_scope), db: Session = Depends(get_db)) -> ConditionMonitoringResponse:
    return ConditionMonitoringResponse(data=condition_service.get_condition_monitoring_summary(db, organization_id=org_id))
