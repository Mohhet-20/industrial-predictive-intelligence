"""Pydantic schemas for the Condition Intelligence & Forecasting API
. Mirrors app.schemas.anomaly's conventions: loose `dict[str,
Any]` payloads for evidence/evaluation bundles (validated by
app.condition.* and app.services.condition_service, not the schema layer),
fully typed response models for everything returned to a client."""
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.models.condition import ConditionRunStatus, ConditionSignalStatus, DegradationStage, ForecastMethod, ForecastModelStatus, ForecastRunStatus

# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class ConditionEvidenceQuery(BaseModel):
    window_start: datetime
    window_end: datetime
    reference_start: datetime
    reference_end: datetime


class ConditionRunCreateRequest(BaseModel):
    sensor_id: str
    analysis_start: datetime
    analysis_end: datetime
    reference_start: datetime
    reference_end: datetime


class ConditionReviewRequest(BaseModel):
    to_status: ConditionSignalStatus
    notes: Optional[str] = Field(default=None, max_length=2000)


class ConditionValidateRequest(BaseModel):
    notes: str = Field(min_length=1, max_length=2000)


class ForecastModelCreateRequest(BaseModel):
    sensor_id: str
    name: str = Field(min_length=1, max_length=200)
    fit_start: datetime
    fit_end: datetime
    test_end: datetime
    seasonal_period_seconds: Optional[float] = Field(default=None, gt=0)


class ForecastRunCreateRequest(BaseModel):
    as_of: datetime
    horizon_seconds: float = Field(gt=0, le=2_592_000)  # capped at 30 days
    step_seconds: float = Field(gt=0)


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class ConditionEvidenceResponse(BaseModel):
    data: dict[str, Any]


class ConditionRunResponse(BaseModel):
    id: str
    organization_id: str
    asset_id: str
    sensor_id: str
    analysis_period_start: datetime
    analysis_period_end: datetime
    reference_period_start: datetime
    reference_period_end: datetime
    config: dict[str, Any]
    status: ConditionRunStatus
    error_message: Optional[str]
    metrics: Optional[dict[str, Any]]
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    created_at: datetime
    created_by_user_id: Optional[str]

    model_config = {"from_attributes": True}


class DegradationSignalResponse(BaseModel):
    id: str
    organization_id: str
    asset_id: str
    sensor_id: str
    condition_run_id: str
    stage: DegradationStage
    status: ConditionSignalStatus
    period_start: datetime
    period_end: datetime
    summary: str
    evidence: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DegradationReviewResponse(BaseModel):
    id: str
    degradation_signal_id: str
    reviewed_by_user_id: Optional[str]
    from_status: ConditionSignalStatus
    to_status: ConditionSignalStatus
    from_stage: DegradationStage
    to_stage: DegradationStage
    notes: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}


class ForecastModelResponse(BaseModel):
    id: str
    organization_id: str
    sensor_id: str
    name: str
    version: int
    method: ForecastMethod
    config: dict[str, Any]
    fit_period_start: datetime
    fit_period_end: datetime
    fit_observation_count: int
    status: ForecastModelStatus
    training_error: Optional[str]
    fitted_at: Optional[datetime]
    evaluation_results: Optional[dict[str, Any]]
    created_at: datetime
    created_by_user_id: Optional[str]

    model_config = {"from_attributes": True}


class ForecastRunResponse(BaseModel):
    id: str
    organization_id: str
    forecast_model_id: str
    status: ForecastRunStatus
    as_of: datetime
    horizon_seconds: float
    step_seconds: float
    points: Optional[list[dict[str, Any]]]
    error_message: Optional[str]
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    created_at: datetime
    created_by_user_id: Optional[str]

    model_config = {"from_attributes": True}


class AttentionListResponse(BaseModel):
    data: dict[str, Any]


class ConditionMonitoringResponse(BaseModel):
    data: dict[str, Any]
