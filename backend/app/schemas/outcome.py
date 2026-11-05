"""Pydantic schemas for the closed-loop outcome/verification/evaluation
API. Response models use `from_attributes=True` throughout so
they can be built directly from the ORM rows returned by
app.services.feedback_service."""
from datetime import datetime
from typing import Any, Generic, Optional, TypeVar

from pydantic import BaseModel, Field

from app.models.outcome import (
    EvaluationKind,
    FeedbackDatasetStatus,
    OutcomeSignalType,
    OutcomeSource,
    OutcomeType,
    PrioritizationPolicyStatus,
    RetrainingCandidateStatus,
    SignalAssessment,
    VerificationStatus,
)

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class OutcomeCreateRequest(BaseModel):
    asset_id: str
    sensor_id: Optional[str] = None
    investigation_id: Optional[str] = None
    outcome_type: OutcomeType
    description: str = Field(min_length=1, max_length=4000)
    outcome_timestamp: datetime
    observation_period_start: Optional[datetime] = None
    observation_period_end: Optional[datetime] = None


class OutcomeVerifyRequest(BaseModel):
    to_status: VerificationStatus
    evidence_note: Optional[str] = Field(default=None, max_length=4000)


class OutcomeSignalLinkCreateRequest(BaseModel):
    signal_type: OutcomeSignalType
    signal_id: str
    horizon_seconds: float = Field(gt=0)
    notes: Optional[str] = Field(default=None, max_length=1000)


class SignalAssessmentRequest(BaseModel):
    signal_assessment: SignalAssessment


class EvaluationRunCreateRequest(BaseModel):
    signal_type: OutcomeSignalType
    evaluation_period_start: datetime
    evaluation_period_end: datetime
    component_id: Optional[str] = None
    sensor_id: Optional[str] = None
    scope_description: Optional[str] = Field(default=None, max_length=1000)


class RetrainingCandidateCreateRequest(BaseModel):
    component_id: str
    trigger_reason: str = Field(min_length=1, max_length=4000)
    supporting_evaluation_run_id: Optional[str] = None


class RetrainingCandidateReviewRequest(BaseModel):
    decision: RetrainingCandidateStatus
    review_notes: Optional[str] = Field(default=None, max_length=4000)


class FeedbackDatasetBuildCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    component_id: Optional[str] = None
    sensor_id: Optional[str] = None
    signal_type: Optional[OutcomeSignalType] = None


class PrioritizationPolicyCreateRequest(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)
    rationale: str = Field(min_length=1, max_length=4000)


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class OutcomeVerificationResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    outcome_id: str
    from_status: VerificationStatus
    to_status: VerificationStatus
    reviewer_user_id: Optional[str] = None
    reviewer_role: Optional[str] = None
    evidence_note: Optional[str] = None
    created_at: datetime


class OutcomeResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    asset_id: str
    sensor_id: Optional[str] = None
    investigation_id: Optional[str] = None
    outcome_type: OutcomeType
    source: OutcomeSource
    description: str
    observation_period_start: Optional[datetime] = None
    observation_period_end: Optional[datetime] = None
    outcome_timestamp: datetime
    verification_status: VerificationStatus
    reported_by_user_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class OutcomeSignalLinkResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    outcome_id: str
    signal_type: OutcomeSignalType
    signal_id: str
    sensor_id: Optional[str] = None
    component_id: Optional[str] = None
    prediction_timestamp: datetime
    horizon_seconds: float
    within_horizon: bool
    counts_toward_evaluation: bool
    signal_assessment: SignalAssessment
    assessed_by_user_id: Optional[str] = None
    assessed_at: Optional[datetime] = None
    linked_by_user_id: Optional[str] = None
    linked_at: datetime
    notes: Optional[str] = None


class OutcomeEvaluationRunResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    component_id: Optional[str] = None
    sensor_id: Optional[str] = None
    signal_type: OutcomeSignalType
    evaluation_kind: EvaluationKind
    previous_evaluation_run_id: Optional[str] = None
    scope_description: str
    evaluation_period_start: datetime
    evaluation_period_end: datetime
    computed_at: datetime
    total_signals_in_scope: int
    linked_outcome_count: int
    verified_outcome_count: int
    coverage_fraction: Optional[float] = None
    metrics: dict[str, Any]
    created_by_user_id: Optional[str] = None


class RetrainingCandidateResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    component_id: str
    supporting_evaluation_run_id: Optional[str] = None
    trigger_reason: str
    status: RetrainingCandidateStatus
    reviewed_by_user_id: Optional[str] = None
    review_notes: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    resulting_retraining_request_id: Optional[str] = None
    created_by_user_id: Optional[str] = None
    created_at: datetime


class FeedbackDatasetBuildResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    name: str
    definition: dict[str, Any]
    status: FeedbackDatasetStatus
    dataset_version: int
    row_count: int
    rows: list[dict[str, Any]]
    error_message: Optional[str] = None
    built_at: Optional[datetime] = None
    created_by_user_id: Optional[str] = None
    created_at: datetime


class FeedbackDatasetBuildSummaryResponse(BaseModel):
    """Same as FeedbackDatasetBuildResponse but omits `rows` — used for
    list endpoints so a listing never has to transfer every dataset's full
    row payload."""

    model_config = {"from_attributes": True}

    id: str
    name: str
    definition: dict[str, Any]
    status: FeedbackDatasetStatus
    dataset_version: int
    row_count: int
    error_message: Optional[str] = None
    built_at: Optional[datetime] = None
    created_by_user_id: Optional[str] = None
    created_at: datetime


class PrioritizationPolicyResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    version: int
    status: PrioritizationPolicyStatus
    config: dict[str, Any]
    rationale: str
    activated_at: Optional[datetime] = None
    activated_by_user_id: Optional[str] = None
    retired_at: Optional[datetime] = None
    created_by_user_id: Optional[str] = None
    created_at: datetime


class ListResponse(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class GenericDataResponse(BaseModel):
    data: dict[str, Any]
