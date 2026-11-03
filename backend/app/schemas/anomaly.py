"""Pydantic schemas for the Anomaly Detection Intelligence API.

Detector config (feature specs, threshold overrides) is intentionally
loose (`dict[str, Any]`) in requests — it is validated by
app.anomaly.thresholds.validate_threshold_config and
app.services.anomaly_service, not by the schema layer, so the same
validation logic runs regardless of caller (API, tests, a future CLI).
Response schemas are fully typed: every field returned to a client is one
this phase's spec actually asks for, and nothing here exposes an internal
model object (an IsolationForest instance, a pandas DataFrame) — only
JSON-serializable summaries.
"""
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.models.intelligence import AnomalySeverityTier, AnomalyStatus, DetectorType, ModelRunStatus, ModelVersionStatus

# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class DetectorCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    detector_type: DetectorType
    sensor_ids: list[str] = Field(min_length=1, max_length=20)
    reference_start: datetime
    reference_end: datetime
    threshold_overrides: dict[str, Any] = Field(default_factory=dict)
    # isolation_forest only — omit for robust_zscore, which computes its
    # own reference statistics directly from raw values.
    feature_specs: Optional[list[dict[str, Any]]] = None
    step_seconds: Optional[float] = Field(default=None, gt=0)


class DetectionRunCreateRequest(BaseModel):
    scoring_start: datetime
    scoring_end: datetime
    allow_retrospective_scoring: bool = False


class AnomalyReviewRequest(BaseModel):
    to_status: AnomalyStatus
    notes: Optional[str] = Field(default=None, max_length=2000)


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class DetectorResponse(BaseModel):
    id: str
    organization_id: str
    name: str
    version: int
    detector_type: DetectorType
    detector_config: dict[str, Any]
    reference_period_start: datetime
    reference_period_end: datetime
    reference_observation_count: int
    feature_schema: Optional[dict[str, Any]]
    baseline_summary: Optional[dict[str, Any]]
    status: ModelVersionStatus
    training_error: Optional[str]
    trained_at: Optional[datetime]
    evaluation_results: Optional[dict[str, Any]]
    created_at: datetime
    created_by_user_id: Optional[str]

    model_config = {"from_attributes": True}


class DetectionRunResponse(BaseModel):
    id: str
    organization_id: str
    model_version_id: str
    status: ModelRunStatus
    scored_period_start: datetime
    scored_period_end: datetime
    background_job_id: Optional[str]
    error_message: Optional[str]
    metrics: Optional[dict[str, Any]]
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    created_at: datetime
    created_by_user_id: Optional[str]

    model_config = {"from_attributes": True}


class AnomalyResponse(BaseModel):
    id: str
    organization_id: str
    asset_id: str
    sensor_id: Optional[str]
    model_run_id: str
    status: AnomalyStatus
    period_start: datetime
    period_end: datetime
    summary: str
    detector_type: DetectorType
    raw_score: float
    normalized_score: Optional[float]
    threshold_used: float
    severity_tier: AnomalySeverityTier
    contributing_signals: Optional[dict[str, Any]]
    data_quality_context: Optional[dict[str, Any]]
    is_synthetic_scenario: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AnomalyContextResponse(BaseModel):
    """Loose envelope, same rationale as app.schemas.timeseries's
    AnalysisEnvelope — the shape genuinely varies (multivariate anomalies
    have no single time series; large ranges may fall back)."""

    data: dict[str, Any]


class AnomalyInvestigationResponse(BaseModel):
    id: str
    anomaly_id: str
    reviewed_by_user_id: Optional[str]
    from_status: AnomalyStatus
    to_status: AnomalyStatus
    notes: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}


class EvaluationResponse(BaseModel):
    """Loose envelope — see app.services.anomaly_service.run_synthetic_evaluation."""

    data: dict[str, Any]
