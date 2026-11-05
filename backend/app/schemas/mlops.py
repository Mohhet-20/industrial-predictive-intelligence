"""Pydantic schemas for the MLOps / Intelligence Lifecycle & Governance API
. Mirrors app.schemas.condition's conventions: loose `dict[str,
Any]` for config/result/evidence payloads (validated by
app.services.mlops_service, not the schema layer), fully typed response
models for everything returned to a client."""
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.models.mlops import ComponentType, GovernanceEventType, LifecycleStage, RetrainingRequestStatus, VersionKind

# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class ComponentCreateRequest(BaseModel):
    component_type: ComponentType
    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=1000)


class VersionRegisterRequest(BaseModel):
    version_kind: VersionKind
    model_version_id: Optional[str] = None
    forecast_model_id: Optional[str] = None
    heuristic_code_version: Optional[str] = Field(default=None, max_length=200)


class EvaluateRequest(BaseModel):
    """No body fields today — evaluation always runs the real synthetic
    harness with its own defaults. A typed, empty model (rather than no
    body at all) so the endpoint can grow parameters later without an
    incompatible request shape change."""

    pass


class ValidateVersionRequest(BaseModel):
    notes: str = Field(min_length=1, max_length=2000)
    evidence: Optional[dict[str, Any]] = None


class ApproveVersionRequest(BaseModel):
    notes: Optional[str] = Field(default=None, max_length=2000)


class ActivateVersionRequest(BaseModel):
    component_version_id: str


class RollbackRequest(BaseModel):
    target_component_version_id: Optional[str] = None


class RetireVersionRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class RetrainingRequestCreateRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)
    config: dict[str, Any]


class ScoreRequest(BaseModel):
    """Loosely typed on purpose: the required fields differ by
    version_kind (scoring_start/scoring_end for a detector;
    as_of/horizon_seconds/step_seconds for a forecaster) — see
    app.services.mlops_service.score_via_active_component, which validates
    this explicitly and returns an actionable 422 if a field is missing,
    rather than silently defaulting anything."""

    scoring_start: Optional[datetime] = None
    scoring_end: Optional[datetime] = None
    allow_retrospective_scoring: bool = False
    as_of: Optional[datetime] = None
    horizon_seconds: Optional[float] = None
    step_seconds: Optional[float] = None


class ShiftAnalysisCreateRequest(BaseModel):
    sensor_id: str
    component_id: Optional[str] = None
    reference_start: datetime
    reference_end: datetime
    comparison_start: datetime
    comparison_end: datetime


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class ComponentResponse(BaseModel):
    id: str
    organization_id: str
    component_type: ComponentType
    name: str
    description: Optional[str]
    created_at: datetime
    created_by_user_id: Optional[str]

    model_config = {"from_attributes": True}


class ComponentVersionResponse(BaseModel):
    id: str
    organization_id: str
    component_id: str
    version_number: int
    version_kind: VersionKind
    model_version_id: Optional[str]
    forecast_model_id: Optional[str]
    heuristic_code_version: Optional[str]
    lifecycle_stage: LifecycleStage
    validation_evidence: Optional[dict[str, Any]]
    approval_notes: Optional[str]
    approved_at: Optional[datetime]
    approved_by_user_id: Optional[str]
    activated_at: Optional[datetime]
    activated_by_user_id: Optional[str]
    retired_at: Optional[datetime]
    retired_by_user_id: Optional[str]
    retirement_reason: Optional[str]
    created_at: datetime
    created_by_user_id: Optional[str]

    model_config = {"from_attributes": True}


class GovernanceEventResponse(BaseModel):
    id: str
    organization_id: str
    component_id: str
    component_version_id: Optional[str]
    event_type: GovernanceEventType
    from_stage: Optional[LifecycleStage]
    to_stage: Optional[LifecycleStage]
    actor_user_id: Optional[str]
    details: Optional[dict[str, Any]]
    created_at: datetime

    model_config = {"from_attributes": True}


class RetrainingRequestResponse(BaseModel):
    id: str
    organization_id: str
    component_id: str
    requested_by_user_id: Optional[str]
    reason: str
    config: dict[str, Any]
    status: RetrainingRequestStatus
    resulting_component_version_id: Optional[str]
    error_message: Optional[str]
    created_at: datetime
    started_at: Optional[datetime]
    finished_at: Optional[datetime]

    model_config = {"from_attributes": True}


class ShiftAnalysisRunResponse(BaseModel):
    id: str
    organization_id: str
    sensor_id: str
    component_id: Optional[str]
    reference_period_start: datetime
    reference_period_end: datetime
    comparison_period_start: datetime
    comparison_period_end: datetime
    method: str
    result: dict[str, Any]
    limitations: str
    created_at: datetime
    created_by_user_id: Optional[str]

    model_config = {"from_attributes": True}


class ScoreResponse(BaseModel):
    data: dict[str, Any]


class MonitoringResponse(BaseModel):
    data: dict[str, Any]
