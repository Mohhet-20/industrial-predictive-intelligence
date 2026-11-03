"""Closed-loop outcome, verification, evaluation, and feedback API
(Phase 12). Two routers:

- `router` (/outcomes) — reporting, verification, and signal linking.
- `feedback_router` (/feedback) — evaluation runs, error analysis,
  retraining candidates, dataset builds, prioritization policy, analytics,
  and label definitions.

Verifying an outcome (the act that can make an outcome count as ground
truth) is restricted to ADMIN only — reporting, linking, and assessing are
open to ADMIN and ENGINEER, matching every other write-role split in this
platform. Reading is open to any authenticated org member.
"""
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import organization_scope, require_roles
from app.db.session import get_db
from app.feedback.label_definitions import get_label_definitions
from app.models.outcome import OutcomeSignalType, RetrainingCandidateStatus, VerificationStatus
from app.models.user import User, UserRole
from app.schemas.outcome import (
    EvaluationRunCreateRequest,
    FeedbackDatasetBuildCreateRequest,
    FeedbackDatasetBuildResponse,
    FeedbackDatasetBuildSummaryResponse,
    GenericDataResponse,
    ListResponse,
    OutcomeCreateRequest,
    OutcomeEvaluationRunResponse,
    OutcomeResponse,
    OutcomeSignalLinkCreateRequest,
    OutcomeSignalLinkResponse,
    OutcomeVerificationResponse,
    OutcomeVerifyRequest,
    PrioritizationPolicyCreateRequest,
    PrioritizationPolicyResponse,
    RetrainingCandidateCreateRequest,
    RetrainingCandidateResponse,
    RetrainingCandidateReviewRequest,
    SignalAssessmentRequest,
)
from app.services import feedback_service

router = APIRouter(prefix="/outcomes", tags=["outcomes"])
feedback_router = APIRouter(prefix="/feedback", tags=["feedback"])

_WRITE_ROLES = (UserRole.ADMIN, UserRole.ENGINEER)
_GOVERNANCE_ROLES = (UserRole.ADMIN,)


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


@router.post("", response_model=OutcomeResponse, status_code=201)
def report_outcome(
    payload: OutcomeCreateRequest,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    db: Session = Depends(get_db),
) -> OutcomeResponse:
    outcome = feedback_service.report_outcome(
        db,
        organization_id=org_id,
        user=user,
        asset_id=payload.asset_id,
        sensor_id=payload.sensor_id,
        investigation_id=payload.investigation_id,
        outcome_type=payload.outcome_type,
        description=payload.description,
        outcome_timestamp=payload.outcome_timestamp,
        observation_period_start=payload.observation_period_start,
        observation_period_end=payload.observation_period_end,
    )
    return OutcomeResponse.model_validate(outcome)


@router.get("", response_model=ListResponse[OutcomeResponse])
def list_outcomes(
    asset_id: Optional[str] = None,
    sensor_id: Optional[str] = None,
    verification_status: Optional[VerificationStatus] = None,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.ENGINEER, UserRole.VIEWER)),
    db: Session = Depends(get_db),
) -> ListResponse[OutcomeResponse]:
    items, total = feedback_service.list_outcomes(
        db, organization_id=org_id, asset_id=asset_id, sensor_id=sensor_id, verification_status=verification_status, limit=limit, offset=offset
    )
    return ListResponse(items=[OutcomeResponse.model_validate(i) for i in items], total=total, limit=limit, offset=offset)


@router.get("/{outcome_id}", response_model=OutcomeResponse)
def get_outcome(
    outcome_id: str,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.ENGINEER, UserRole.VIEWER)),
    db: Session = Depends(get_db),
) -> OutcomeResponse:
    outcome = feedback_service.get_outcome(db, organization_id=org_id, outcome_id=outcome_id)
    return OutcomeResponse.model_validate(outcome)


@router.post("/{outcome_id}/verify", response_model=OutcomeResponse)
def verify_outcome(
    outcome_id: str,
    payload: OutcomeVerifyRequest,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(*_GOVERNANCE_ROLES)),
    db: Session = Depends(get_db),
) -> OutcomeResponse:
    outcome = feedback_service.verify_outcome(
        db, organization_id=org_id, user=user, outcome_id=outcome_id, to_status=payload.to_status, evidence_note=payload.evidence_note
    )
    return OutcomeResponse.model_validate(outcome)


@router.get("/{outcome_id}/verifications", response_model=ListResponse[OutcomeVerificationResponse])
def list_verifications(
    outcome_id: str,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.ENGINEER, UserRole.VIEWER)),
    db: Session = Depends(get_db),
) -> ListResponse[OutcomeVerificationResponse]:
    items = feedback_service.list_verifications_for_outcome(db, organization_id=org_id, outcome_id=outcome_id)
    return ListResponse(items=[OutcomeVerificationResponse.model_validate(i) for i in items], total=len(items), limit=len(items), offset=0)


@router.post("/{outcome_id}/links", response_model=OutcomeSignalLinkResponse, status_code=201)
def link_outcome_to_signal(
    outcome_id: str,
    payload: OutcomeSignalLinkCreateRequest,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    db: Session = Depends(get_db),
) -> OutcomeSignalLinkResponse:
    link = feedback_service.link_outcome_to_signal(
        db,
        organization_id=org_id,
        user=user,
        outcome_id=outcome_id,
        signal_type=payload.signal_type,
        signal_id=payload.signal_id,
        horizon_seconds=payload.horizon_seconds,
        notes=payload.notes,
    )
    return OutcomeSignalLinkResponse.model_validate(link)


@router.get("/{outcome_id}/links", response_model=ListResponse[OutcomeSignalLinkResponse])
def list_links_for_outcome(
    outcome_id: str,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.ENGINEER, UserRole.VIEWER)),
    db: Session = Depends(get_db),
) -> ListResponse[OutcomeSignalLinkResponse]:
    items = feedback_service.list_links_for_outcome(db, organization_id=org_id, outcome_id=outcome_id)
    return ListResponse(items=[OutcomeSignalLinkResponse.model_validate(i) for i in items], total=len(items), limit=len(items), offset=0)


@router.post("/links/{link_id}/assess", response_model=OutcomeSignalLinkResponse)
def assess_link(
    link_id: str,
    payload: SignalAssessmentRequest,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    db: Session = Depends(get_db),
) -> OutcomeSignalLinkResponse:
    link = feedback_service.assess_link(db, organization_id=org_id, user=user, link_id=link_id, signal_assessment=payload.signal_assessment)
    return OutcomeSignalLinkResponse.model_validate(link)


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


@feedback_router.post("/evaluation-runs", response_model=OutcomeEvaluationRunResponse, status_code=201)
def run_evaluation(
    payload: EvaluationRunCreateRequest,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(*_GOVERNANCE_ROLES)),
    db: Session = Depends(get_db),
) -> OutcomeEvaluationRunResponse:
    run = feedback_service.run_evaluation(
        db,
        organization_id=org_id,
        user=user,
        signal_type=payload.signal_type,
        evaluation_period_start=payload.evaluation_period_start,
        evaluation_period_end=payload.evaluation_period_end,
        component_id=payload.component_id,
        sensor_id=payload.sensor_id,
        scope_description=payload.scope_description,
    )
    return OutcomeEvaluationRunResponse.model_validate(run)


@feedback_router.get("/evaluation-runs", response_model=ListResponse[OutcomeEvaluationRunResponse])
def list_evaluation_runs(
    component_id: Optional[str] = None,
    sensor_id: Optional[str] = None,
    signal_type: Optional[OutcomeSignalType] = None,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.ENGINEER, UserRole.VIEWER)),
    db: Session = Depends(get_db),
) -> ListResponse[OutcomeEvaluationRunResponse]:
    items, total = feedback_service.list_evaluation_runs(
        db, organization_id=org_id, component_id=component_id, sensor_id=sensor_id, signal_type=signal_type, limit=limit, offset=offset
    )
    return ListResponse(items=[OutcomeEvaluationRunResponse.model_validate(i) for i in items], total=total, limit=limit, offset=offset)


@feedback_router.get("/evaluation-runs/{run_id}", response_model=OutcomeEvaluationRunResponse)
def get_evaluation_run(
    run_id: str,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.ENGINEER, UserRole.VIEWER)),
    db: Session = Depends(get_db),
) -> OutcomeEvaluationRunResponse:
    run = feedback_service.get_evaluation_run(db, organization_id=org_id, run_id=run_id)
    return OutcomeEvaluationRunResponse.model_validate(run)


@feedback_router.get("/performance-over-time", response_model=GenericDataResponse)
def performance_over_time(
    component_id: Optional[str] = None,
    sensor_id: Optional[str] = None,
    signal_type: Optional[OutcomeSignalType] = None,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.ENGINEER, UserRole.VIEWER)),
    db: Session = Depends(get_db),
) -> GenericDataResponse:
    points = feedback_service.performance_over_time(db, organization_id=org_id, component_id=component_id, sensor_id=sensor_id, signal_type=signal_type)
    return GenericDataResponse(data={"points": points})


@feedback_router.get("/drift-outcome-correlation", response_model=GenericDataResponse)
def drift_outcome_correlation(
    sensor_id: str,
    component_id: Optional[str] = None,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.ENGINEER, UserRole.VIEWER)),
    db: Session = Depends(get_db),
) -> GenericDataResponse:
    result = feedback_service.drift_outcome_correlation(db, organization_id=org_id, sensor_id=sensor_id, component_id=component_id)
    return GenericDataResponse(data=result)


@feedback_router.get("/error-analysis", response_model=GenericDataResponse)
def get_error_analysis(
    component_id: Optional[str] = None,
    sensor_id: Optional[str] = None,
    signal_type: Optional[OutcomeSignalType] = None,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.ENGINEER, UserRole.VIEWER)),
    db: Session = Depends(get_db),
) -> GenericDataResponse:
    result = feedback_service.get_error_analysis(db, organization_id=org_id, component_id=component_id, sensor_id=sensor_id, signal_type=signal_type)
    return GenericDataResponse(data=result)


# ---------------------------------------------------------------------------
# Retraining candidates
# ---------------------------------------------------------------------------


@feedback_router.post("/retraining-candidates", response_model=RetrainingCandidateResponse, status_code=201)
def propose_retraining_candidate(
    payload: RetrainingCandidateCreateRequest,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(*_GOVERNANCE_ROLES)),
    db: Session = Depends(get_db),
) -> RetrainingCandidateResponse:
    candidate = feedback_service.propose_retraining_candidate(
        db,
        organization_id=org_id,
        user=user,
        component_id=payload.component_id,
        trigger_reason=payload.trigger_reason,
        supporting_evaluation_run_id=payload.supporting_evaluation_run_id,
    )
    return RetrainingCandidateResponse.model_validate(candidate)


@feedback_router.get("/retraining-candidates", response_model=ListResponse[RetrainingCandidateResponse])
def list_retraining_candidates(
    component_id: Optional[str] = None,
    status: Optional[RetrainingCandidateStatus] = None,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.ENGINEER, UserRole.VIEWER)),
    db: Session = Depends(get_db),
) -> ListResponse[RetrainingCandidateResponse]:
    items, total = feedback_service.list_retraining_candidates(db, organization_id=org_id, component_id=component_id, status=status, limit=limit, offset=offset)
    return ListResponse(items=[RetrainingCandidateResponse.model_validate(i) for i in items], total=total, limit=limit, offset=offset)


@feedback_router.post("/retraining-candidates/{candidate_id}/review", response_model=RetrainingCandidateResponse)
def review_retraining_candidate(
    candidate_id: str,
    payload: RetrainingCandidateReviewRequest,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(*_GOVERNANCE_ROLES)),
    db: Session = Depends(get_db),
) -> RetrainingCandidateResponse:
    candidate = feedback_service.review_retraining_candidate(
        db, organization_id=org_id, user=user, candidate_id=candidate_id, decision=payload.decision, review_notes=payload.review_notes
    )
    return RetrainingCandidateResponse.model_validate(candidate)


# ---------------------------------------------------------------------------
# Feedback dataset builds
# ---------------------------------------------------------------------------


@feedback_router.post("/datasets", response_model=FeedbackDatasetBuildResponse, status_code=201)
def build_feedback_dataset(
    payload: FeedbackDatasetBuildCreateRequest,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(*_GOVERNANCE_ROLES)),
    db: Session = Depends(get_db),
) -> FeedbackDatasetBuildResponse:
    build = feedback_service.build_feedback_dataset(
        db, organization_id=org_id, user=user, name=payload.name, component_id=payload.component_id, sensor_id=payload.sensor_id, signal_type=payload.signal_type
    )
    return FeedbackDatasetBuildResponse.model_validate(build)


@feedback_router.get("/datasets", response_model=ListResponse[FeedbackDatasetBuildSummaryResponse])
def list_feedback_dataset_builds(
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.ENGINEER, UserRole.VIEWER)),
    db: Session = Depends(get_db),
) -> ListResponse[FeedbackDatasetBuildSummaryResponse]:
    items, total = feedback_service.list_feedback_dataset_builds(db, organization_id=org_id, limit=limit, offset=offset)
    return ListResponse(items=[FeedbackDatasetBuildSummaryResponse.model_validate(i) for i in items], total=total, limit=limit, offset=offset)


@feedback_router.get("/datasets/{build_id}", response_model=FeedbackDatasetBuildResponse)
def get_feedback_dataset_build(
    build_id: str,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.ENGINEER, UserRole.VIEWER)),
    db: Session = Depends(get_db),
) -> FeedbackDatasetBuildResponse:
    build = feedback_service.get_feedback_dataset_build(db, organization_id=org_id, build_id=build_id)
    return FeedbackDatasetBuildResponse.model_validate(build)


# ---------------------------------------------------------------------------
# Prioritization policy
# ---------------------------------------------------------------------------


@feedback_router.post("/prioritization-policies", response_model=PrioritizationPolicyResponse, status_code=201)
def create_prioritization_policy(
    payload: PrioritizationPolicyCreateRequest,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(*_GOVERNANCE_ROLES)),
    db: Session = Depends(get_db),
) -> PrioritizationPolicyResponse:
    policy = feedback_service.create_prioritization_policy(db, organization_id=org_id, user=user, config=payload.config, rationale=payload.rationale)
    return PrioritizationPolicyResponse.model_validate(policy)


@feedback_router.get("/prioritization-policies", response_model=ListResponse[PrioritizationPolicyResponse])
def list_prioritization_policies(
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.ENGINEER, UserRole.VIEWER)),
    db: Session = Depends(get_db),
) -> ListResponse[PrioritizationPolicyResponse]:
    items, total = feedback_service.list_prioritization_policies(db, organization_id=org_id, limit=limit, offset=offset)
    return ListResponse(items=[PrioritizationPolicyResponse.model_validate(i) for i in items], total=total, limit=limit, offset=offset)


@feedback_router.post("/prioritization-policies/{policy_id}/activate", response_model=PrioritizationPolicyResponse)
def activate_prioritization_policy(
    policy_id: str,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(*_GOVERNANCE_ROLES)),
    db: Session = Depends(get_db),
) -> PrioritizationPolicyResponse:
    policy = feedback_service.activate_prioritization_policy(db, organization_id=org_id, user=user, policy_id=policy_id)
    return PrioritizationPolicyResponse.model_validate(policy)


# ---------------------------------------------------------------------------
# Label definitions, analytics, scenario separation
# ---------------------------------------------------------------------------


@feedback_router.get("/label-definitions", response_model=GenericDataResponse)
def label_definitions(
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.ENGINEER, UserRole.VIEWER)),
) -> GenericDataResponse:
    return GenericDataResponse(data=get_label_definitions())


@feedback_router.get("/analytics", response_model=GenericDataResponse)
def closed_loop_analytics(
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.ENGINEER, UserRole.VIEWER)),
    db: Session = Depends(get_db),
) -> GenericDataResponse:
    return GenericDataResponse(data=feedback_service.closed_loop_analytics(db, organization_id=org_id))


@feedback_router.get("/scenario-separation", response_model=GenericDataResponse)
def scenario_separation(
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.ENGINEER, UserRole.VIEWER)),
    db: Session = Depends(get_db),
) -> GenericDataResponse:
    return GenericDataResponse(data=feedback_service.get_scenario_separation_summary(db, organization_id=org_id))
