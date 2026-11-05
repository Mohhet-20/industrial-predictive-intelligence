"""Closed-loop outcome, verification, evaluation, and feedback orchestration
.

CORE PRINCIPLE, enforced everywhere in this module:

    PREDICTIONS ARE NOT TRUTH.
    HUMAN FEEDBACK IS NOT AUTOMATICALLY GROUND TRUTH.
    MAINTENANCE EVENTS ARE NOT AUTOMATICALLY FAILURES.
    OUTCOMES MUST HAVE PROVENANCE.

This module is the only place that writes to app.models.outcome's tables.
It reuses, and never duplicates:

- app.feedback.matching for every temporal-integrity computation
  (`within_horizon`, leakage checks) — this module never re-derives those
  itself.
- app.feedback.metrics for classification and precision-only metrics.
- app.feedback.error_analysis for late-detection/duplicate/false-negative
  candidate surfacing.
- app.feedback.label_definitions for the governance-exposed definitions.
- app.services.mlops_service.request_retraining (the existing,
  unmodified, human-initiated retraining workflow) — `review_retraining_candidate`
  calls into it on approval and never calls `execute_retraining_request`;
  executing a request remains a separate, later, human action taken through
  the existing MLOps workflow, not something this module triggers.

Nothing here ever edits the original detection fields of an Anomaly,
DegradationSignal, ForecastRun, or ScenarioRun row — every function below
only reads those tables (to resolve a signal's prediction_timestamp/
sensor_id/component_id at link time) and writes exclusively to the new
outcome/verification/link/evaluation/candidate/dataset/policy tables.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.feedback import error_analysis as error_analysis_mod
from app.feedback import label_definitions as label_definitions_mod
from app.feedback.matching import evaluation_run_leakage_check, resolve_link_temporal_fields
from app.feedback.metrics import (
    MIN_VERIFIED_LINKS_FOR_METRICS,
    MIN_VERIFIED_LINKS_FOR_UPDATE,
    ClassifiedLink,
    classify,
    compute_metrics,
    coverage_classification,
)
from app.models.condition import DegradationSignal, ForecastModel, ForecastRun
from app.models.intelligence import Anomaly, ModelRun
from app.models.mlops import ComponentVersion, IntelligenceComponent, ShiftAnalysisRun
from app.models.outcome import (
    EvaluationKind,
    FeedbackDatasetBuild,
    FeedbackDatasetStatus,
    Outcome,
    OutcomeEvaluationRun,
    OutcomeSignalLink,
    OutcomeSignalType,
    OutcomeSource,
    OutcomeType,
    OutcomeVerification,
    PrioritizationPolicy,
    PrioritizationPolicyStatus,
    RetrainingCandidate,
    RetrainingCandidateStatus,
    SignalAssessment,
    VerificationStatus,
)
from app.models.scenario import Scenario, ScenarioRun
from app.models.user import User
from app.services import mlops_service

# Enforced state machine for Outcome.verification_status — see
# app.models.outcome.VerificationStatus for what each state means.
VALID_VERIFICATION_TRANSITIONS: dict[VerificationStatus, tuple[VerificationStatus, ...]] = {
    VerificationStatus.UNVERIFIED: (VerificationStatus.REVIEWED, VerificationStatus.VERIFIED, VerificationStatus.REJECTED),
    VerificationStatus.REVIEWED: (VerificationStatus.VERIFIED, VerificationStatus.REJECTED, VerificationStatus.UNVERIFIED),
    VerificationStatus.VERIFIED: (VerificationStatus.CONFLICTING, VerificationStatus.REJECTED),
    VerificationStatus.REJECTED: (VerificationStatus.REVIEWED,),
    VerificationStatus.CONFLICTING: (VerificationStatus.VERIFIED, VerificationStatus.REJECTED, VerificationStatus.REVIEWED),
}

# Which outcome types represent a real fault for false-negative-candidate
# surfacing. Deliberately narrow (not OPERATIONAL_EVENT/INSPECTION_RESULT) —
# see module docstring's core principle: a maintenance event is not
# automatically a failure, and only an explicit FAILURE_EVENT is treated as
# fault-like here.
_FAULT_LIKE_OUTCOME_TYPES = (OutcomeType.FAILURE_EVENT,)

_TERMINAL_VERIFICATION_STATES = (VerificationStatus.VERIFIED, VerificationStatus.REJECTED)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(value: Optional[datetime]) -> Optional[datetime]:
    """SQLite, even through a `DateTime(timezone=True)` column, does not
    preserve tzinfo across a write+reload — every datetime loaded off a
    model instance from the DB comes back naive (but is always UTC, since
    that's the only thing ever written). See
    app.services.anomaly_service._ensure_utc for the same, independently
    documented behavior. Every datetime this module reads off an ORM row
    is passed through this before it is compared with, subtracted from, or
    validated against a caller-supplied (and therefore possibly already
    tz-aware) datetime — which is exactly what app.feedback.matching's
    temporal-integrity functions do."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


def report_outcome(
    db: Session,
    *,
    organization_id: str,
    user: User,
    asset_id: str,
    outcome_type: OutcomeType,
    description: str,
    outcome_timestamp: datetime,
    sensor_id: Optional[str] = None,
    investigation_id: Optional[str] = None,
    observation_period_start: Optional[datetime] = None,
    observation_period_end: Optional[datetime] = None,
) -> Outcome:
    if not description or not description.strip():
        raise ValidationFailedError("A reported outcome requires a non-empty description.")
    outcome = Outcome(
        organization_id=organization_id,
        asset_id=asset_id,
        sensor_id=sensor_id,
        investigation_id=investigation_id,
        outcome_type=outcome_type,
        source=OutcomeSource.USER_REPORTED,
        description=description,
        observation_period_start=observation_period_start,
        observation_period_end=observation_period_end,
        outcome_timestamp=outcome_timestamp,
        verification_status=VerificationStatus.UNVERIFIED,
        reported_by_user_id=user.id,
    )
    db.add(outcome)
    db.commit()
    db.refresh(outcome)
    return outcome


def get_outcome(db: Session, *, organization_id: str, outcome_id: str) -> Outcome:
    outcome = db.query(Outcome).filter(Outcome.id == outcome_id, Outcome.organization_id == organization_id).first()
    if outcome is None:
        raise NotFoundError(f"Outcome '{outcome_id}' not found.")
    return outcome


def list_outcomes(
    db: Session,
    *,
    organization_id: str,
    asset_id: Optional[str] = None,
    sensor_id: Optional[str] = None,
    verification_status: Optional[VerificationStatus] = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Outcome], int]:
    query = db.query(Outcome).filter(Outcome.organization_id == organization_id)
    if asset_id:
        query = query.filter(Outcome.asset_id == asset_id)
    if sensor_id:
        query = query.filter(Outcome.sensor_id == sensor_id)
    if verification_status:
        query = query.filter(Outcome.verification_status == verification_status)
    total = query.count()
    items = query.order_by(Outcome.outcome_timestamp.desc()).offset(offset).limit(limit).all()
    return items, total


def verify_outcome(
    db: Session,
    *,
    organization_id: str,
    user: User,
    outcome_id: str,
    to_status: VerificationStatus,
    evidence_note: Optional[str] = None,
) -> Outcome:
    outcome = get_outcome(db, organization_id=organization_id, outcome_id=outcome_id)
    allowed = VALID_VERIFICATION_TRANSITIONS.get(outcome.verification_status, ())
    if to_status not in allowed:
        raise ConflictError(
            f"Cannot transition outcome from '{outcome.verification_status.value}' to '{to_status.value}'. "
            f"Allowed next states: {[s.value for s in allowed]}."
        )
    if to_status in _TERMINAL_VERIFICATION_STATES and not (evidence_note and evidence_note.strip()):
        raise ValidationFailedError(
            f"Transitioning to '{to_status.value}' requires a non-empty evidence_note explaining the basis for the decision."
        )

    verification = OutcomeVerification(
        organization_id=organization_id,
        outcome_id=outcome.id,
        from_status=outcome.verification_status,
        to_status=to_status,
        reviewer_user_id=user.id,
        reviewer_role=user.role.value,
        evidence_note=evidence_note,
    )
    db.add(verification)
    outcome.verification_status = to_status
    db.commit()
    db.refresh(outcome)
    return outcome


def list_verifications_for_outcome(db: Session, *, organization_id: str, outcome_id: str) -> list[OutcomeVerification]:
    get_outcome(db, organization_id=organization_id, outcome_id=outcome_id)  # existence + org check
    return (
        db.query(OutcomeVerification)
        .filter(OutcomeVerification.organization_id == organization_id, OutcomeVerification.outcome_id == outcome_id)
        .order_by(OutcomeVerification.created_at.asc())
        .all()
    )


# ---------------------------------------------------------------------------
# Outcome-signal linking
# ---------------------------------------------------------------------------


def _component_id_for_model_version(db: Session, *, organization_id: str, model_version_id: Optional[str]) -> Optional[str]:
    if not model_version_id:
        return None
    cv = (
        db.query(ComponentVersion)
        .filter(ComponentVersion.organization_id == organization_id, ComponentVersion.model_version_id == model_version_id)
        .first()
    )
    return cv.component_id if cv else None


def _component_id_for_forecast_model(db: Session, *, organization_id: str, forecast_model_id: Optional[str]) -> Optional[str]:
    if not forecast_model_id:
        return None
    cv = (
        db.query(ComponentVersion)
        .filter(ComponentVersion.organization_id == organization_id, ComponentVersion.forecast_model_id == forecast_model_id)
        .first()
    )
    return cv.component_id if cv else None


def _resolve_signal_prediction_timestamp(
    db: Session, *, organization_id: str, signal_type: OutcomeSignalType, signal_id: str
) -> dict[str, Any]:
    """Returns {"prediction_timestamp", "sensor_id", "component_id"} for the
    given signal, resolved from the REAL signal row — never guessed, never
    defaulted. Raises NotFoundError if the signal does not exist in this
    organization."""
    if signal_type == OutcomeSignalType.ANOMALY:
        anomaly = db.query(Anomaly).filter(Anomaly.id == signal_id, Anomaly.organization_id == organization_id).first()
        if anomaly is None:
            raise NotFoundError(f"Anomaly '{signal_id}' not found.")
        model_run = db.get(ModelRun, anomaly.model_run_id)
        component_id = _component_id_for_model_version(
            db, organization_id=organization_id, model_version_id=model_run.model_version_id if model_run else None
        )
        return {"prediction_timestamp": _ensure_utc(anomaly.period_start), "sensor_id": anomaly.sensor_id, "component_id": component_id}

    if signal_type == OutcomeSignalType.DEGRADATION_SIGNAL:
        signal = (
            db.query(DegradationSignal)
            .filter(DegradationSignal.id == signal_id, DegradationSignal.organization_id == organization_id)
            .first()
        )
        if signal is None:
            raise NotFoundError(f"Degradation signal '{signal_id}' not found.")
        return {"prediction_timestamp": _ensure_utc(signal.period_start), "sensor_id": signal.sensor_id, "component_id": None}

    if signal_type == OutcomeSignalType.FORECAST_RUN:
        run = db.query(ForecastRun).filter(ForecastRun.id == signal_id, ForecastRun.organization_id == organization_id).first()
        if run is None:
            raise NotFoundError(f"Forecast run '{signal_id}' not found.")
        forecast_model = db.get(ForecastModel, run.forecast_model_id)
        component_id = _component_id_for_forecast_model(
            db, organization_id=organization_id, forecast_model_id=run.forecast_model_id
        )
        return {
            "prediction_timestamp": _ensure_utc(run.as_of),
            "sensor_id": forecast_model.sensor_id if forecast_model else None,
            "component_id": component_id,
        }

    if signal_type == OutcomeSignalType.SCENARIO_RUN:
        run = db.query(ScenarioRun).filter(ScenarioRun.id == signal_id, ScenarioRun.organization_id == organization_id).first()
        if run is None:
            raise NotFoundError(f"Scenario run '{signal_id}' not found.")
        scenario = db.get(Scenario, run.scenario_id)
        return {
            "prediction_timestamp": _ensure_utc(run.created_at),
            "sensor_id": scenario.sensor_id if scenario else None,
            "component_id": None,
        }

    raise ValidationFailedError(f"Unknown signal_type '{signal_type}'.")


def link_outcome_to_signal(
    db: Session,
    *,
    organization_id: str,
    user: User,
    outcome_id: str,
    signal_type: OutcomeSignalType,
    signal_id: str,
    horizon_seconds: float,
    notes: Optional[str] = None,
) -> OutcomeSignalLink:
    outcome = get_outcome(db, organization_id=organization_id, outcome_id=outcome_id)

    existing = (
        db.query(OutcomeSignalLink)
        .filter(
            OutcomeSignalLink.organization_id == organization_id,
            OutcomeSignalLink.outcome_id == outcome_id,
            OutcomeSignalLink.signal_type == signal_type,
            OutcomeSignalLink.signal_id == signal_id,
        )
        .first()
    )
    if existing is not None:
        raise ConflictError("This outcome is already linked to this signal.")

    resolved = _resolve_signal_prediction_timestamp(db, organization_id=organization_id, signal_type=signal_type, signal_id=signal_id)

    temporal = resolve_link_temporal_fields(
        prediction_timestamp=resolved["prediction_timestamp"],
        horizon_seconds=horizon_seconds,
        outcome_timestamp=_ensure_utc(outcome.outcome_timestamp),
    )
    if temporal.errors:
        raise ValidationFailedError("Invalid horizon/timestamp for this link.", details=temporal.errors)

    link = OutcomeSignalLink(
        organization_id=organization_id,
        outcome_id=outcome.id,
        signal_type=signal_type,
        signal_id=signal_id,
        sensor_id=resolved["sensor_id"],
        component_id=resolved["component_id"],
        prediction_timestamp=resolved["prediction_timestamp"],
        horizon_seconds=horizon_seconds,
        within_horizon=temporal.within_horizon,
        counts_toward_evaluation=(signal_type != OutcomeSignalType.SCENARIO_RUN),
        signal_assessment=SignalAssessment.NOT_YET_ASSESSED,
        linked_by_user_id=user.id,
        notes=notes,
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    return link


def assess_link(
    db: Session, *, organization_id: str, user: User, link_id: str, signal_assessment: SignalAssessment
) -> OutcomeSignalLink:
    link = (
        db.query(OutcomeSignalLink)
        .filter(OutcomeSignalLink.id == link_id, OutcomeSignalLink.organization_id == organization_id)
        .first()
    )
    if link is None:
        raise NotFoundError(f"Outcome-signal link '{link_id}' not found.")
    outcome = db.get(Outcome, link.outcome_id)
    if outcome is None or outcome.verification_status not in _TERMINAL_VERIFICATION_STATES:
        raise ConflictError(
            "This link cannot be assessed until its outcome has been VERIFIED or REJECTED by a human reviewer."
        )
    link.signal_assessment = signal_assessment
    link.assessed_by_user_id = user.id
    link.assessed_at = _utcnow()
    db.commit()
    db.refresh(link)
    return link


def list_links_for_outcome(db: Session, *, organization_id: str, outcome_id: str) -> list[OutcomeSignalLink]:
    get_outcome(db, organization_id=organization_id, outcome_id=outcome_id)
    return (
        db.query(OutcomeSignalLink)
        .filter(OutcomeSignalLink.organization_id == organization_id, OutcomeSignalLink.outcome_id == outcome_id)
        .order_by(OutcomeSignalLink.linked_at.asc())
        .all()
    )


def _latest_verification(outcome: Outcome) -> Optional[OutcomeVerification]:
    return outcome.verifications[-1] if outcome.verifications else None


def _build_classified_link(link: OutcomeSignalLink, outcome: Outcome) -> ClassifiedLink:
    delay = (_ensure_utc(outcome.outcome_timestamp) - _ensure_utc(link.prediction_timestamp)).total_seconds()
    return ClassifiedLink(
        link_id=link.id,
        signal_id=link.signal_id,
        within_horizon=link.within_horizon,
        verification_status=outcome.verification_status,
        signal_assessment=link.signal_assessment,
        detection_delay_seconds=delay,
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def run_evaluation(
    db: Session,
    *,
    organization_id: str,
    user: User,
    signal_type: OutcomeSignalType,
    evaluation_period_start: datetime,
    evaluation_period_end: datetime,
    component_id: Optional[str] = None,
    sensor_id: Optional[str] = None,
    scope_description: Optional[str] = None,
) -> OutcomeEvaluationRun:
    if evaluation_period_end <= evaluation_period_start:
        raise ValidationFailedError("evaluation_period_end must be after evaluation_period_start.")

    computed_at = _utcnow()

    query = db.query(OutcomeSignalLink).filter(
        OutcomeSignalLink.organization_id == organization_id,
        OutcomeSignalLink.signal_type == signal_type,
        OutcomeSignalLink.counts_toward_evaluation.is_(True),
        OutcomeSignalLink.prediction_timestamp >= evaluation_period_start,
        OutcomeSignalLink.prediction_timestamp <= evaluation_period_end,
    )
    if component_id:
        query = query.filter(OutcomeSignalLink.component_id == component_id)
    if sensor_id:
        query = query.filter(OutcomeSignalLink.sensor_id == sensor_id)
    links = query.all()

    classified: list[ClassifiedLink] = []
    verified_outcome_ids: set[str] = set()
    for link in links:
        outcome = db.get(Outcome, link.outcome_id)
        if outcome is None:
            continue
        latest_verification = _latest_verification(outcome)
        outcome_verified_at = (
            _ensure_utc(latest_verification.created_at)
            if latest_verification is not None and outcome.verification_status in _TERMINAL_VERIFICATION_STATES
            else None
        )
        leak = evaluation_run_leakage_check(
            evaluation_period_end=evaluation_period_end,
            computed_at=computed_at,
            outcome_verified_at=outcome_verified_at,
            outcome_timestamp=_ensure_utc(outcome.outcome_timestamp),
            # Outcome.created_at is set once, at report time, by
            # report_outcome() below — it is "when this outcome became
            # known to the platform," distinct from outcome_timestamp
            # ("when the event itself occurred," which a human can
            # backdate). See app.feedback.matching's module docstring.
            outcome_reported_at=_ensure_utc(outcome.created_at),
        )
        if leak:
            raise ConflictError(f"Evaluation run rejected: {leak}")
        if outcome.verification_status in _TERMINAL_VERIFICATION_STATES or outcome.verification_status == VerificationStatus.CONFLICTING:
            verified_outcome_ids.add(outcome.id)
        classified.append(_build_classified_link(link, outcome))

    metrics_result = compute_metrics(classified, min_verified_links=MIN_VERIFIED_LINKS_FOR_METRICS)
    total_signals_in_scope = len(links)
    linked_outcome_count = len({l.outcome_id for l in links})
    coverage_fraction = (metrics_result.eligible_count / total_signals_in_scope) if total_signals_in_scope else None
    coverage_label = coverage_classification(eligible_link_count=metrics_result.eligible_count, total_link_count=total_signals_in_scope)

    previous_run = (
        db.query(OutcomeEvaluationRun)
        .filter(
            OutcomeEvaluationRun.organization_id == organization_id,
            OutcomeEvaluationRun.signal_type == signal_type,
            OutcomeEvaluationRun.component_id == component_id,
            OutcomeEvaluationRun.sensor_id == sensor_id,
        )
        .order_by(OutcomeEvaluationRun.computed_at.desc())
        .first()
    )
    evaluation_kind = EvaluationKind.UPDATED if previous_run is not None else EvaluationKind.INITIAL

    run = OutcomeEvaluationRun(
        organization_id=organization_id,
        component_id=component_id,
        sensor_id=sensor_id,
        signal_type=signal_type,
        evaluation_kind=evaluation_kind,
        previous_evaluation_run_id=previous_run.id if previous_run else None,
        scope_description=scope_description or f"{signal_type.value} evaluation for component={component_id or 'any'}, sensor={sensor_id or 'any'}",
        evaluation_period_start=evaluation_period_start,
        evaluation_period_end=evaluation_period_end,
        computed_at=computed_at,
        total_signals_in_scope=total_signals_in_scope,
        linked_outcome_count=linked_outcome_count,
        verified_outcome_count=len(verified_outcome_ids),
        coverage_fraction=coverage_fraction,
        metrics={
            "computed": metrics_result.computed,
            "reason": metrics_result.reason,
            "precision": metrics_result.precision,
            "true_positive_count": metrics_result.true_positive_count,
            "false_positive_count": metrics_result.false_positive_count,
            "unresolved_count": metrics_result.unresolved_count,
            "eligible_count": metrics_result.eligible_count,
            "min_required": metrics_result.min_required,
            "coverage_classification": coverage_label,
            "recall_note": "Recall/F1 are not reported — this platform cannot enumerate signals that should have fired but did not.",
        },
        created_by_user_id=user.id,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def get_evaluation_run(db: Session, *, organization_id: str, run_id: str) -> OutcomeEvaluationRun:
    run = (
        db.query(OutcomeEvaluationRun)
        .filter(OutcomeEvaluationRun.id == run_id, OutcomeEvaluationRun.organization_id == organization_id)
        .first()
    )
    if run is None:
        raise NotFoundError(f"Evaluation run '{run_id}' not found.")
    return run


def list_evaluation_runs(
    db: Session,
    *,
    organization_id: str,
    component_id: Optional[str] = None,
    sensor_id: Optional[str] = None,
    signal_type: Optional[OutcomeSignalType] = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[OutcomeEvaluationRun], int]:
    query = db.query(OutcomeEvaluationRun).filter(OutcomeEvaluationRun.organization_id == organization_id)
    if component_id:
        query = query.filter(OutcomeEvaluationRun.component_id == component_id)
    if sensor_id:
        query = query.filter(OutcomeEvaluationRun.sensor_id == sensor_id)
    if signal_type:
        query = query.filter(OutcomeEvaluationRun.signal_type == signal_type)
    total = query.count()
    items = query.order_by(OutcomeEvaluationRun.computed_at.desc()).offset(offset).limit(limit).all()
    return items, total


def performance_over_time(
    db: Session,
    *,
    organization_id: str,
    component_id: Optional[str] = None,
    sensor_id: Optional[str] = None,
    signal_type: Optional[OutcomeSignalType] = None,
) -> list[dict]:
    """Returns every evaluation run in this scope, oldest first, with its
    own metrics payload untouched — including `metrics.computed = False`
    runs. This is a deliberate refusal to synthesize a trend line from
    runs that didn't have enough verified evidence to report a number;
    the caller (API/frontend) must respect `computed` per point rather
    than treating every run as equally meaningful."""
    query = db.query(OutcomeEvaluationRun).filter(OutcomeEvaluationRun.organization_id == organization_id)
    if component_id:
        query = query.filter(OutcomeEvaluationRun.component_id == component_id)
    if sensor_id:
        query = query.filter(OutcomeEvaluationRun.sensor_id == sensor_id)
    if signal_type:
        query = query.filter(OutcomeEvaluationRun.signal_type == signal_type)
    runs = query.order_by(OutcomeEvaluationRun.computed_at.asc()).all()
    return [
        {
            "evaluation_run_id": run.id,
            "computed_at": run.computed_at,
            "evaluation_kind": run.evaluation_kind.value,
            "coverage_fraction": run.coverage_fraction,
            "metrics": run.metrics,
        }
        for run in runs
    ]


def drift_outcome_correlation(db: Session, *, organization_id: str, sensor_id: str, component_id: Optional[str] = None) -> dict:
    """Presents shift-analysis runs and outcome-evaluation runs for the
    same sensor side by side, in time order. This is NOT a claim that a
    detected shift caused any change in measured performance — see the
    `note` field, which must accompany this data wherever it is rendered."""
    shift_query = db.query(ShiftAnalysisRun).filter(
        ShiftAnalysisRun.organization_id == organization_id, ShiftAnalysisRun.sensor_id == sensor_id
    )
    if component_id:
        shift_query = shift_query.filter(ShiftAnalysisRun.component_id == component_id)
    shift_runs = shift_query.order_by(ShiftAnalysisRun.created_at.asc()).all()

    eval_query = db.query(OutcomeEvaluationRun).filter(
        OutcomeEvaluationRun.organization_id == organization_id, OutcomeEvaluationRun.sensor_id == sensor_id
    )
    if component_id:
        eval_query = eval_query.filter(OutcomeEvaluationRun.component_id == component_id)
    eval_runs = eval_query.order_by(OutcomeEvaluationRun.computed_at.asc()).all()

    return {
        "shift_analysis_runs": [
            {"id": r.id, "created_at": r.created_at, "method": r.method, "result": r.result}
            for r in shift_runs
        ],
        "evaluation_runs": [
            {"id": r.id, "computed_at": r.computed_at, "evaluation_kind": r.evaluation_kind.value, "metrics": r.metrics}
            for r in eval_runs
        ],
        "note": (
            "This is a side-by-side temporal view only. A detected distribution shift is NOT automatically "
            "equated with model performance degradation — a human must independently assess both before "
            "drawing that conclusion."
        ),
    }


def get_error_analysis(
    db: Session,
    *,
    organization_id: str,
    component_id: Optional[str] = None,
    sensor_id: Optional[str] = None,
    signal_type: Optional[OutcomeSignalType] = None,
) -> dict:
    link_query = db.query(OutcomeSignalLink).filter(OutcomeSignalLink.organization_id == organization_id)
    if component_id:
        link_query = link_query.filter(OutcomeSignalLink.component_id == component_id)
    if sensor_id:
        link_query = link_query.filter(OutcomeSignalLink.sensor_id == sensor_id)
    if signal_type:
        link_query = link_query.filter(OutcomeSignalLink.signal_type == signal_type)
    links = link_query.all()

    classified = []
    signals_for_duplicate_check: list[tuple] = []
    linked_outcome_ids: set[str] = set()
    for link in links:
        outcome = db.get(Outcome, link.outcome_id)
        if outcome is None:
            continue
        linked_outcome_ids.add(outcome.id)
        classified.append(_build_classified_link(link, outcome))
        if link.sensor_id:
            signals_for_duplicate_check.append((link.signal_id, link.sensor_id, link.signal_type.value, link.prediction_timestamp))

    buckets = classify(classified)

    fault_query = db.query(Outcome).filter(
        Outcome.organization_id == organization_id,
        Outcome.outcome_type.in_(_FAULT_LIKE_OUTCOME_TYPES),
        Outcome.verification_status == VerificationStatus.VERIFIED,
    )
    if sensor_id:
        fault_query = fault_query.filter(Outcome.sensor_id == sensor_id)
    fault_like_outcomes = [(o.id, o.sensor_id) for o in fault_query.all()]

    report = error_analysis_mod.build_error_analysis_report(
        true_positive_links=buckets["true_positive"],
        signals_for_duplicate_check=signals_for_duplicate_check,
        fault_like_outcomes=fault_like_outcomes,
        linked_outcome_ids=linked_outcome_ids,
    )
    result = report.to_dict()
    result["note"] = "Every finding below is a candidate for human review, not an automatically asserted root cause."
    return result


# ---------------------------------------------------------------------------
# Retraining candidates (never auto-executes, never auto-activates)
# ---------------------------------------------------------------------------


def propose_retraining_candidate(
    db: Session,
    *,
    organization_id: str,
    user: User,
    component_id: str,
    trigger_reason: str,
    supporting_evaluation_run_id: Optional[str] = None,
) -> RetrainingCandidate:
    component = (
        db.query(IntelligenceComponent)
        .filter(IntelligenceComponent.id == component_id, IntelligenceComponent.organization_id == organization_id)
        .first()
    )
    if component is None:
        raise NotFoundError(f"Component '{component_id}' not found.")
    if not trigger_reason or not trigger_reason.strip():
        raise ValidationFailedError("A retraining candidate requires a non-empty trigger_reason.")
    if supporting_evaluation_run_id:
        get_evaluation_run(db, organization_id=organization_id, run_id=supporting_evaluation_run_id)

    candidate = RetrainingCandidate(
        organization_id=organization_id,
        component_id=component_id,
        supporting_evaluation_run_id=supporting_evaluation_run_id,
        trigger_reason=trigger_reason,
        status=RetrainingCandidateStatus.PROPOSED,
        created_by_user_id=user.id,
    )
    db.add(candidate)
    db.commit()
    db.refresh(candidate)
    return candidate


def list_retraining_candidates(
    db: Session,
    *,
    organization_id: str,
    component_id: Optional[str] = None,
    status: Optional[RetrainingCandidateStatus] = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[RetrainingCandidate], int]:
    query = db.query(RetrainingCandidate).filter(RetrainingCandidate.organization_id == organization_id)
    if component_id:
        query = query.filter(RetrainingCandidate.component_id == component_id)
    if status:
        query = query.filter(RetrainingCandidate.status == status)
    total = query.count()
    items = query.order_by(RetrainingCandidate.created_at.desc()).offset(offset).limit(limit).all()
    return items, total


def review_retraining_candidate(
    db: Session,
    *,
    organization_id: str,
    user: User,
    candidate_id: str,
    decision: RetrainingCandidateStatus,
    review_notes: Optional[str] = None,
) -> RetrainingCandidate:
    candidate = (
        db.query(RetrainingCandidate)
        .filter(RetrainingCandidate.id == candidate_id, RetrainingCandidate.organization_id == organization_id)
        .first()
    )
    if candidate is None:
        raise NotFoundError(f"Retraining candidate '{candidate_id}' not found.")
    if decision not in (RetrainingCandidateStatus.APPROVED, RetrainingCandidateStatus.DISMISSED):
        raise ValidationFailedError("decision must be 'approved' or 'dismissed'.")
    if candidate.status != RetrainingCandidateStatus.PROPOSED:
        raise ConflictError(f"Retraining candidate '{candidate_id}' has already been reviewed (status: '{candidate.status.value}').")

    candidate.status = decision
    candidate.reviewed_by_user_id = user.id
    candidate.review_notes = review_notes
    candidate.reviewed_at = _utcnow()

    if decision == RetrainingCandidateStatus.APPROVED:
        reason = candidate.trigger_reason
        if review_notes:
            reason = f"{reason} | reviewer notes: {review_notes}"
        # Calls the EXISTING lifecycle workflow — this only creates a
        # REQUESTED RetrainingRequest row. Executing it (which actually
        # retrains) and activating any resulting version remain separate,
        # later, human-initiated actions through the MLOps workflow — this
        # function never calls execute_retraining_request or activate_version.
        request = mlops_service.request_retraining(
            db, organization_id=organization_id, user=user, component_id=candidate.component_id, reason=reason, config={}
        )
        candidate.resulting_retraining_request_id = request.id

    db.commit()
    db.refresh(candidate)
    return candidate


# ---------------------------------------------------------------------------
# Feedback dataset builds (frozen, versioned, provenance-preserving)
# ---------------------------------------------------------------------------


def build_feedback_dataset(
    db: Session,
    *,
    organization_id: str,
    user: User,
    name: str,
    component_id: Optional[str] = None,
    sensor_id: Optional[str] = None,
    signal_type: Optional[OutcomeSignalType] = None,
) -> FeedbackDatasetBuild:
    if not name or not name.strip():
        raise ValidationFailedError("A feedback dataset build requires a non-empty name.")

    query = (
        db.query(OutcomeSignalLink)
        .join(Outcome, OutcomeSignalLink.outcome_id == Outcome.id)
        .filter(
            OutcomeSignalLink.organization_id == organization_id,
            Outcome.verification_status.in_(_TERMINAL_VERIFICATION_STATES),
            OutcomeSignalLink.signal_assessment.in_((SignalAssessment.CONFIRMS, SignalAssessment.REFUTES)),
        )
    )
    if component_id:
        query = query.filter(OutcomeSignalLink.component_id == component_id)
    if sensor_id:
        query = query.filter(OutcomeSignalLink.sensor_id == sensor_id)
    if signal_type:
        query = query.filter(OutcomeSignalLink.signal_type == signal_type)
    links = query.all()

    rows: list[dict] = []
    for link in links:
        outcome = db.get(Outcome, link.outcome_id)
        if outcome is None:
            continue
        rows.append(
            {
                "outcome_id": outcome.id,
                "outcome_type": outcome.outcome_type.value,
                "verification_status": outcome.verification_status.value,
                "outcome_timestamp": _ensure_utc(outcome.outcome_timestamp).isoformat(),
                "reported_by_user_id": outcome.reported_by_user_id,
                "link_id": link.id,
                "signal_type": link.signal_type.value,
                "signal_id": link.signal_id,
                "sensor_id": link.sensor_id,
                "component_id": link.component_id,
                "signal_assessment": link.signal_assessment.value,
                "within_horizon": link.within_horizon,
                "prediction_timestamp": _ensure_utc(link.prediction_timestamp).isoformat(),
                "horizon_seconds": link.horizon_seconds,
                "assessed_by_user_id": link.assessed_by_user_id,
            }
        )

    existing_count = db.query(FeedbackDatasetBuild).filter(FeedbackDatasetBuild.organization_id == organization_id, FeedbackDatasetBuild.name == name).count()

    build = FeedbackDatasetBuild(
        organization_id=organization_id,
        name=name,
        definition={
            "component_id": component_id,
            "sensor_id": sensor_id,
            "signal_type": signal_type.value if signal_type else None,
            "inclusion_rule": "outcome.verification_status in (verified, rejected) AND link.signal_assessment in (confirms, refutes)",
        },
        status=FeedbackDatasetStatus.BUILT,
        dataset_version=existing_count + 1,
        row_count=len(rows),
        rows=rows,
        built_at=_utcnow(),
        created_by_user_id=user.id,
    )
    db.add(build)
    db.commit()
    db.refresh(build)
    return build


def get_feedback_dataset_build(db: Session, *, organization_id: str, build_id: str) -> FeedbackDatasetBuild:
    build = (
        db.query(FeedbackDatasetBuild)
        .filter(FeedbackDatasetBuild.id == build_id, FeedbackDatasetBuild.organization_id == organization_id)
        .first()
    )
    if build is None:
        raise NotFoundError(f"Feedback dataset build '{build_id}' not found.")
    return build


def list_feedback_dataset_builds(
    db: Session, *, organization_id: str, limit: int = 50, offset: int = 0
) -> tuple[list[FeedbackDatasetBuild], int]:
    query = db.query(FeedbackDatasetBuild).filter(FeedbackDatasetBuild.organization_id == organization_id)
    total = query.count()
    items = query.order_by(FeedbackDatasetBuild.created_at.desc()).offset(offset).limit(limit).all()
    return items, total


# ---------------------------------------------------------------------------
# Prioritization policy (versioned, single-active, opt-in)
# ---------------------------------------------------------------------------


def create_prioritization_policy(
    db: Session, *, organization_id: str, user: User, config: dict, rationale: str
) -> PrioritizationPolicy:
    if not rationale or not rationale.strip():
        raise ValidationFailedError("A prioritization policy requires a non-empty rationale.")
    max_version = (
        db.query(PrioritizationPolicy.version)
        .filter(PrioritizationPolicy.organization_id == organization_id)
        .order_by(PrioritizationPolicy.version.desc())
        .first()
    )
    next_version = (max_version[0] + 1) if max_version else 1

    policy = PrioritizationPolicy(
        organization_id=organization_id,
        version=next_version,
        status=PrioritizationPolicyStatus.DRAFT,
        config=config,
        rationale=rationale,
        created_by_user_id=user.id,
    )
    db.add(policy)
    db.commit()
    db.refresh(policy)
    return policy


def activate_prioritization_policy(db: Session, *, organization_id: str, user: User, policy_id: str) -> PrioritizationPolicy:
    policy = (
        db.query(PrioritizationPolicy)
        .filter(PrioritizationPolicy.id == policy_id, PrioritizationPolicy.organization_id == organization_id)
        .first()
    )
    if policy is None:
        raise NotFoundError(f"Prioritization policy '{policy_id}' not found.")
    if policy.status == PrioritizationPolicyStatus.RETIRED:
        raise ConflictError("A retired prioritization policy cannot be reactivated. Create a new policy instead.")

    currently_active = (
        db.query(PrioritizationPolicy)
        .filter(PrioritizationPolicy.organization_id == organization_id, PrioritizationPolicy.status == PrioritizationPolicyStatus.ACTIVE)
        .first()
    )
    now = _utcnow()
    if currently_active is not None and currently_active.id != policy.id:
        currently_active.status = PrioritizationPolicyStatus.RETIRED
        currently_active.retired_at = now

    policy.status = PrioritizationPolicyStatus.ACTIVE
    policy.activated_at = now
    policy.activated_by_user_id = user.id
    db.commit()
    db.refresh(policy)
    return policy


def get_active_prioritization_policy(db: Session, *, organization_id: str) -> Optional[PrioritizationPolicy]:
    return (
        db.query(PrioritizationPolicy)
        .filter(PrioritizationPolicy.organization_id == organization_id, PrioritizationPolicy.status == PrioritizationPolicyStatus.ACTIVE)
        .first()
    )


def list_prioritization_policies(
    db: Session, *, organization_id: str, limit: int = 50, offset: int = 0
) -> tuple[list[PrioritizationPolicy], int]:
    query = db.query(PrioritizationPolicy).filter(PrioritizationPolicy.organization_id == organization_id)
    total = query.count()
    items = query.order_by(PrioritizationPolicy.version.desc()).offset(offset).limit(limit).all()
    return items, total


# ---------------------------------------------------------------------------
# Closed-loop analytics & governance summaries
# ---------------------------------------------------------------------------


def closed_loop_analytics(db: Session, *, organization_id: str) -> dict:
    """Deliberately a bundle of process counts, never a single "accuracy"
    number — see docs/CLOSED_LOOP_FEEDBACK.md for why a single KPI here
    would be meaningless given how few organizations will have enough
    verified evidence for a trustworthy metric."""
    outcomes_by_status: dict[str, int] = {}
    for status in VerificationStatus:
        outcomes_by_status[status.value] = (
            db.query(Outcome)
            .filter(Outcome.organization_id == organization_id, Outcome.verification_status == status)
            .count()
        )

    links_by_assessment: dict[str, int] = {}
    for assessment in SignalAssessment:
        links_by_assessment[assessment.value] = (
            db.query(OutcomeSignalLink)
            .filter(OutcomeSignalLink.organization_id == organization_id, OutcomeSignalLink.signal_assessment == assessment)
            .count()
        )

    candidates_by_status: dict[str, int] = {}
    for status in RetrainingCandidateStatus:
        candidates_by_status[status.value] = (
            db.query(RetrainingCandidate)
            .filter(RetrainingCandidate.organization_id == organization_id, RetrainingCandidate.status == status)
            .count()
        )

    active_policy = get_active_prioritization_policy(db, organization_id=organization_id)

    return {
        "outcomes_by_verification_status": outcomes_by_status,
        "links_by_signal_assessment": links_by_assessment,
        "evaluation_run_count": db.query(OutcomeEvaluationRun).filter(OutcomeEvaluationRun.organization_id == organization_id).count(),
        "retraining_candidates_by_status": candidates_by_status,
        "feedback_dataset_build_count": db.query(FeedbackDatasetBuild).filter(FeedbackDatasetBuild.organization_id == organization_id).count(),
        "active_prioritization_policy_id": active_policy.id if active_policy else None,
        "note": (
            "These are closed-loop process counts, not a single accuracy score. See per-component/per-sensor "
            "evaluation runs for actual precision metrics, each gated on a minimum verified-evidence sample size."
        ),
    }


def get_scenario_separation_summary(db: Session, *, organization_id: str) -> dict:
    """An explicit, queryable invariant check: a scenario-run link must
    NEVER count toward evaluation. If `invariant_holds` is ever False here,
    that is a serious bug, not a configuration choice."""
    scenario_links_total = (
        db.query(OutcomeSignalLink)
        .filter(OutcomeSignalLink.organization_id == organization_id, OutcomeSignalLink.signal_type == OutcomeSignalType.SCENARIO_RUN)
        .count()
    )
    scenario_links_counting_toward_evaluation = (
        db.query(OutcomeSignalLink)
        .filter(
            OutcomeSignalLink.organization_id == organization_id,
            OutcomeSignalLink.signal_type == OutcomeSignalType.SCENARIO_RUN,
            OutcomeSignalLink.counts_toward_evaluation.is_(True),
        )
        .count()
    )
    real_links_total = (
        db.query(OutcomeSignalLink)
        .filter(OutcomeSignalLink.organization_id == organization_id, OutcomeSignalLink.signal_type != OutcomeSignalType.SCENARIO_RUN)
        .count()
    )
    return {
        "scenario_links_total": scenario_links_total,
        "scenario_links_counting_toward_evaluation": scenario_links_counting_toward_evaluation,
        "real_links_total": real_links_total,
        "invariant_holds": scenario_links_counting_toward_evaluation == 0,
        "note": "Scenario Lab results are simulated — they must never become, or count toward, a real evaluation metric.",
    }
