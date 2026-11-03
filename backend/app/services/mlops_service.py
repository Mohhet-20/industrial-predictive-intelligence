"""Orchestration layer for the intelligence lifecycle, registry, and
governance work (Phase 5). Mirrors app.services.anomaly_service and
app.services.condition_service's conventions throughout: every function
re-validates organization scope, every persisted row is written once and
then only ever transitions through explicit, logged actions, and every
"training" or "scoring" action here calls the REAL Phase 3/4 functions
(app.services.anomaly_service.create_model_version /
app.services.condition_service.create_forecast_model /
app.services.anomaly_service.create_detection_run /
app.services.condition_service.create_forecast_run) rather than
re-implementing or simulating them. See app.models.mlops's module
docstring for the registry design, and docs/MLOPS_GOVERNANCE.md for the
full reasoning.

Lifecycle stage transitions, per app.models.mlops.LifecycleStage, and which
functions legally perform them:

    EXPERIMENTING -> (not reachable via this module yet; reserved for a
                       future phase's experiment-tracking UI — nothing
                       here creates a ComponentVersion this early)
    (created)     -> TRAINED | EVALUATED | FAILED   register_version()
    TRAINED       -> EVALUATED | FAILED             record_evaluation()
    EVALUATED     -> VALIDATED                      validate_version()
    VALIDATED     -> APPROVED                       approve_version()
    APPROVED      -> ACTIVE                         activate_version()
    ACTIVE        -> APPROVED (superseded)           activate_version() on a sibling, or rollback_component()
    (any)         -> RETIRED                        retire_version()

A ComponentVersion registered from a ForecastModel or a heuristic method
starts at EVALUATED, not TRAINED — see register_version()'s docstring for
why: their "evaluation" (the mandatory backtest, or a cited deterministic
test suite) already happened before this module ever sees them, so there
is no separate TRAINED->EVALUATED step to model for those two kinds. This
is exactly the "adapted per component type, not forced uniformly"
requirement — see docs/MLOPS_GOVERNANCE.md §1.
"""
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.models.condition import ForecastModel, ForecastModelStatus
from app.models.intelligence import DetectorType, ModelVersion, ModelVersionStatus
from app.models.mlops import (
    ComponentType,
    ComponentVersion,
    GovernanceEvent,
    GovernanceEventType,
    IntelligenceComponent,
    LifecycleStage,
    RetrainingRequest,
    RetrainingRequestStatus,
    ShiftAnalysisRun,
    VersionKind,
)
from app.models.user import User
from app.services import anomaly_service, condition_service
from app.services.timeseries_service import get_sensor_in_org, validate_time_range
from app.timeseries.data_access import TooManyObservationsError, load_measurement_series
from app.timeseries import profiling


def _ensure_utc(dt: datetime) -> datetime:
    """See app.services.anomaly_service._ensure_utc's docstring — the same
    SQLite tzinfo-loss behavior applies here."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


_COMPONENT_TYPE_TO_VERSION_KIND: dict[ComponentType, VersionKind] = {
    ComponentType.TRAINED_ML_MODEL: VersionKind.MODEL_VERSION,
    ComponentType.STATISTICAL_DETECTOR: VersionKind.MODEL_VERSION,
    ComponentType.FORECASTING_MODEL: VersionKind.FORECAST_MODEL,
    ComponentType.HEURISTIC_SIGNAL_METHOD: VersionKind.HEURISTIC,
}
# A component_type can legally hold more than one detector_type: both
# ISOLATION_FOREST and MAHALANOBIS are "a real sklearn estimator fit on
# data" (app.models.mlops.ComponentType's own docstring definition of
# TRAINED_ML_MODEL), so Phase 14's new Mahalanobis detector belongs in
# the same bucket as Isolation Forest, not a new component_type of its
# own. ROBUST_ZSCORE remains the only STATISTICAL_DETECTOR member — it is
# closed-form, not a fitted model.
_COMPONENT_TYPE_TO_DETECTOR_TYPES: dict[ComponentType, frozenset[DetectorType]] = {
    ComponentType.TRAINED_ML_MODEL: frozenset({DetectorType.ISOLATION_FOREST, DetectorType.MAHALANOBIS}),
    ComponentType.STATISTICAL_DETECTOR: frozenset({DetectorType.ROBUST_ZSCORE}),
}
MIN_SHIFT_OBSERVATIONS = 10
GOVERNANCE_EVENT_LIST_LIMIT_MAX = 200


def _log_event(
    db: Session, *, organization_id: str, component_id: str, component_version_id: Optional[str],
    event_type: GovernanceEventType, actor_user_id: Optional[str],
    from_stage: Optional[LifecycleStage], to_stage: Optional[LifecycleStage], details: Optional[dict[str, Any]] = None,
) -> None:
    """Append-only. Never called with a `details` payload containing a
    token, password, or raw config value — only small, already-public
    summary fields (ids, stage names, counts, short reasons)."""
    db.add(GovernanceEvent(
        organization_id=organization_id, component_id=component_id, component_version_id=component_version_id,
        event_type=event_type, from_stage=from_stage, to_stage=to_stage, actor_user_id=actor_user_id, details=details,
    ))


# ---------------------------------------------------------------------------
# Component & version registry (§5)
# ---------------------------------------------------------------------------


def register_component(
    db: Session, *, organization_id: str, user: User, component_type: ComponentType, name: str, description: Optional[str],
) -> IntelligenceComponent:
    existing = (
        db.query(IntelligenceComponent)
        .filter(IntelligenceComponent.organization_id == organization_id, IntelligenceComponent.component_type == component_type, IntelligenceComponent.name == name)
        .first()
    )
    if existing is not None:
        raise ConflictError(f"A component named '{name}' of type '{component_type.value}' is already registered (id={existing.id}).")

    component = IntelligenceComponent(
        organization_id=organization_id, component_type=component_type, name=name, description=description, created_by_user_id=user.id,
    )
    db.add(component)
    db.commit()
    db.refresh(component)
    _log_event(db, organization_id=organization_id, component_id=component.id, component_version_id=None, event_type=GovernanceEventType.COMPONENT_REGISTERED, actor_user_id=user.id, from_stage=None, to_stage=None, details={"component_type": component_type.value, "name": name})
    db.commit()
    return component


def get_component(db: Session, *, organization_id: str, component_id: str) -> IntelligenceComponent:
    component = db.query(IntelligenceComponent).filter(IntelligenceComponent.id == component_id, IntelligenceComponent.organization_id == organization_id).first()
    if component is None:
        raise NotFoundError(f"Intelligence component '{component_id}' not found.")
    return component


def list_components(db: Session, *, organization_id: str, component_type: Optional[ComponentType], limit: int, offset: int):
    query = db.query(IntelligenceComponent).filter(IntelligenceComponent.organization_id == organization_id)
    if component_type:
        query = query.filter(IntelligenceComponent.component_type == component_type)
    query = query.order_by(IntelligenceComponent.created_at.desc())
    total = query.count()
    return query.offset(offset).limit(limit).all(), total


def get_active_version(db: Session, *, organization_id: str, component_id: str) -> Optional[ComponentVersion]:
    return (
        db.query(ComponentVersion)
        .filter(ComponentVersion.organization_id == organization_id, ComponentVersion.component_id == component_id, ComponentVersion.lifecycle_stage == LifecycleStage.ACTIVE)
        .first()
    )


def _initial_stage_for_model_version(model_version: ModelVersion) -> LifecycleStage:
    return LifecycleStage.TRAINED if model_version.status == ModelVersionStatus.TRAINED else LifecycleStage.FAILED


def _initial_stage_for_forecast_model(forecast_model: ForecastModel) -> LifecycleStage:
    # A ForecastModel's mandatory backtest already ran during fitting (see
    # app.models.condition.ForecastModel's own docstring) — there is no
    # separate "trained but not yet evaluated" moment to represent, so a
    # successfully-fit ForecastModel (JUSTIFIED or the equally-honest
    # NOT_JUSTIFIED outcome) enters this registry already EVALUATED.
    return LifecycleStage.FAILED if forecast_model.status == ForecastModelStatus.FAILED else LifecycleStage.EVALUATED


def register_version(
    db: Session, *, organization_id: str, user: User, component_id: str, version_kind: VersionKind,
    model_version_id: Optional[str] = None, forecast_model_id: Optional[str] = None, heuristic_code_version: Optional[str] = None,
) -> ComponentVersion:
    component = get_component(db, organization_id=organization_id, component_id=component_id)
    expected_kind = _COMPONENT_TYPE_TO_VERSION_KIND[component.component_type]
    if version_kind != expected_kind:
        raise ValidationFailedError(
            f"Component '{component.name}' is type '{component.component_type.value}', which registers "
            f"'{expected_kind.value}' versions, not '{version_kind.value}'."
        )

    model_version: Optional[ModelVersion] = None
    forecast_model: Optional[ForecastModel] = None

    if version_kind == VersionKind.MODEL_VERSION:
        if not model_version_id:
            raise ValidationFailedError("model_version_id is required to register a model_version-kind version.")
        model_version = db.query(ModelVersion).filter(ModelVersion.id == model_version_id, ModelVersion.organization_id == organization_id).first()
        if model_version is None:
            raise NotFoundError(f"Detector version '{model_version_id}' not found.")
        expected_detector_types = _COMPONENT_TYPE_TO_DETECTOR_TYPES[component.component_type]
        if model_version.detector_type not in expected_detector_types:
            allowed = ", ".join(sorted(t.value for t in expected_detector_types))
            raise ValidationFailedError(
                f"Component '{component.name}' ({component.component_type.value}) expects a detector type in "
                f"[{allowed}], but '{model_version_id}' is '{model_version.detector_type.value}'."
            )
        if model_version.name != component.name:
            # Enforced for two reasons, not just tidiness: (1) ModelVersion's
            # own version numbers are per (organization, name) — reusing
            # model_version.version as this row's version_number (see
            # below) would silently collide with another differently-named
            # detector's v1/v2/... under the same component, tripping the
            # (component_id, version_number) unique constraint as an opaque
            # 500 instead of an honest 409/422; (2) a component is meant to
            # be the stable identity of ONE capability — registering an
            # unrelated detector under it would make "component X's active
            # version" meaningless.
            raise ValidationFailedError(f"Detector '{model_version_id}' is named '{model_version.name}', not '{component.name}' — a component can only register versions of the detector it was registered for.")
        already = db.query(ComponentVersion).filter(ComponentVersion.model_version_id == model_version_id).first()
        if already is not None:
            raise ConflictError(f"Detector version '{model_version_id}' is already registered as component_version '{already.id}'.")
        version_number = model_version.version
        initial_stage = _initial_stage_for_model_version(model_version)

    elif version_kind == VersionKind.FORECAST_MODEL:
        if not forecast_model_id:
            raise ValidationFailedError("forecast_model_id is required to register a forecast_model-kind version.")
        forecast_model = db.query(ForecastModel).filter(ForecastModel.id == forecast_model_id, ForecastModel.organization_id == organization_id).first()
        if forecast_model is None:
            raise NotFoundError(f"Forecast model '{forecast_model_id}' not found.")
        if forecast_model.name != component.name:
            raise ValidationFailedError(f"Forecast model '{forecast_model_id}' is named '{forecast_model.name}', not '{component.name}' — a component can only register versions of the forecaster it was registered for.")
        already = db.query(ComponentVersion).filter(ComponentVersion.forecast_model_id == forecast_model_id).first()
        if already is not None:
            raise ConflictError(f"Forecast model '{forecast_model_id}' is already registered as component_version '{already.id}'.")
        version_number = forecast_model.version
        initial_stage = _initial_stage_for_forecast_model(forecast_model)

    else:  # HEURISTIC
        if not heuristic_code_version or not heuristic_code_version.strip():
            raise ValidationFailedError("heuristic_code_version (e.g. 'degradation_classifier@1') is required to register a heuristic-kind version.")
        version_number = (
            db.query(func.max(ComponentVersion.version_number)).filter(ComponentVersion.component_id == component_id).scalar() or 0
        ) + 1
        # No training/fitting step exists for a heuristic method — the
        # deterministic function IS the artifact. It enters the registry
        # already EVALUATED on the strength of this codebase's existing
        # test suite (tests/test_condition_core.py and friends), which the
        # human validator is expected to cite in validation_evidence at the
        # VALIDATED step — see validate_version().
        initial_stage = LifecycleStage.EVALUATED

    version = ComponentVersion(
        organization_id=organization_id, component_id=component.id, version_number=version_number, version_kind=version_kind,
        model_version_id=model_version.id if model_version else None,
        forecast_model_id=forecast_model.id if forecast_model else None,
        heuristic_code_version=heuristic_code_version if version_kind == VersionKind.HEURISTIC else None,
        lifecycle_stage=initial_stage, created_by_user_id=user.id,
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    _log_event(db, organization_id=organization_id, component_id=component.id, component_version_id=version.id, event_type=GovernanceEventType.VERSION_REGISTERED, actor_user_id=user.id, from_stage=None, to_stage=initial_stage, details={"version_number": version_number, "version_kind": version_kind.value})
    db.commit()
    return version


def get_component_version(db: Session, *, organization_id: str, component_version_id: str) -> ComponentVersion:
    version = db.query(ComponentVersion).filter(ComponentVersion.id == component_version_id, ComponentVersion.organization_id == organization_id).first()
    if version is None:
        raise NotFoundError(f"Component version '{component_version_id}' not found.")
    return version


def list_component_versions(db: Session, *, organization_id: str, component_id: str, limit: int, offset: int):
    get_component(db, organization_id=organization_id, component_id=component_id)  # 404s if wrong org
    query = db.query(ComponentVersion).filter(ComponentVersion.organization_id == organization_id, ComponentVersion.component_id == component_id).order_by(ComponentVersion.version_number.desc())
    total = query.count()
    return query.offset(offset).limit(limit).all(), total


# ---------------------------------------------------------------------------
# Evaluation & validation gates (§7, §8) — task-specific, never an
# arbitrary universal accuracy threshold.
# ---------------------------------------------------------------------------


def record_evaluation(db: Session, *, organization_id: str, user: User, component_version_id: str) -> ComponentVersion:
    """Only meaningful for MODEL_VERSION-kind versions — a ForecastModel or
    heuristic is already EVALUATED at registration (see register_version).
    Runs the REAL synthetic evaluation harness
    (app.services.anomaly_service.run_synthetic_evaluation) against the
    underlying ModelVersion — this is genuine pipeline-correctness
    evidence, not a fabricated number, and it is explicitly NOT a
    real-world accuracy claim (see that function's own docstring and
    docs/ANOMALY_DETECTION.md)."""
    version = get_component_version(db, organization_id=organization_id, component_version_id=component_version_id)
    if version.version_kind != VersionKind.MODEL_VERSION:
        raise ConflictError(f"Component version '{component_version_id}' is kind '{version.version_kind.value}' — it was already evaluated at registration; there is no separate evaluation step for this kind.")
    if version.lifecycle_stage != LifecycleStage.TRAINED:
        raise ConflictError(f"Component version '{component_version_id}' must be in stage 'trained' to evaluate (current: '{version.lifecycle_stage.value}').")

    results = anomaly_service.run_synthetic_evaluation(db, organization_id=organization_id, model_version_id=version.model_version_id)
    version.lifecycle_stage = LifecycleStage.EVALUATED
    db.commit()
    db.refresh(version)
    _log_event(
        db, organization_id=organization_id, component_id=version.component_id, component_version_id=version.id,
        event_type=GovernanceEventType.EVALUATION_RECORDED, actor_user_id=user.id,
        from_stage=LifecycleStage.TRAINED, to_stage=LifecycleStage.EVALUATED,
        details={"scenario_count_evaluated": results["scenario_count_evaluated"], "mean_precision": results["mean_precision"], "mean_recall": results["mean_recall"], "is_synthetic": True},
    )
    db.commit()
    return version


def validate_version(
    db: Session, *, organization_id: str, user: User, component_version_id: str, notes: str, evidence: Optional[dict[str, Any]] = None,
) -> ComponentVersion:
    """The human validation gate. `notes` is always mandatory — a written
    attestation of what was reviewed. What ELSE is required depends on
    version_kind, because this platform has no universal accuracy metric
    that applies to all of them (spec §8):

      - MODEL_VERSION: the underlying detector must already have a
        recorded synthetic evaluation (record_evaluation()) showing at
        least one scenario was actually evaluated — proof the pipeline was
        exercised, not just declared trained.
      - FORECAST_MODEL: the underlying ForecastModel must be JUSTIFIED —
        i.e. it already demonstrably beat naive baselines on a real
        holdout backtest. A NOT_JUSTIFIED forecast can never be validated;
        that is not a bug, it is the gate working as intended.
      - HEURISTIC: there is no trained artifact to evaluate at all, so
        `evidence['reviewed_reference']` (e.g. a doc section or test file)
        citing what was actually reviewed is mandatory instead.
    """
    if not notes or not notes.strip():
        raise ValidationFailedError("Validating a component version requires non-empty `notes` describing what was reviewed.")
    version = get_component_version(db, organization_id=organization_id, component_version_id=component_version_id)
    if version.lifecycle_stage != LifecycleStage.EVALUATED:
        raise ConflictError(f"Component version '{component_version_id}' must be in stage 'evaluated' to validate (current: '{version.lifecycle_stage.value}').")

    evidence = dict(evidence or {})
    if version.version_kind == VersionKind.MODEL_VERSION:
        model_version = db.get(ModelVersion, version.model_version_id)
        if not model_version.evaluation_results or not model_version.evaluation_results.get("scenario_count_evaluated"):
            raise ValidationFailedError("Cannot validate: no synthetic evaluation has been recorded for this detector version yet (see POST .../evaluate).")
        evidence.setdefault("synthetic_evaluation_summary", {"mean_precision": model_version.evaluation_results.get("mean_precision"), "mean_recall": model_version.evaluation_results.get("mean_recall"), "scenario_count_evaluated": model_version.evaluation_results.get("scenario_count_evaluated"), "is_synthetic": True})
    elif version.version_kind == VersionKind.FORECAST_MODEL:
        forecast_model = db.get(ForecastModel, version.forecast_model_id)
        if forecast_model.status != ForecastModelStatus.JUSTIFIED:
            raise ConflictError(f"Cannot validate: forecast model status is '{forecast_model.status.value}', not 'justified' — its backtest did not demonstrably beat naive baselines.")
        evidence.setdefault("backtest_summary", {"is_justified": True, "fit_observation_count": forecast_model.fit_observation_count})
    else:  # HEURISTIC
        if not evidence.get("reviewed_reference") or not str(evidence.get("reviewed_reference")).strip():
            raise ValidationFailedError("Validating a heuristic-kind version requires evidence.reviewed_reference — a citation of what was actually reviewed (e.g. a doc section or test file), since there is no trained artifact to evaluate.")

    evidence["notes"] = notes
    version.validation_evidence = evidence
    version.lifecycle_stage = LifecycleStage.VALIDATED
    db.commit()
    db.refresh(version)
    _log_event(db, organization_id=organization_id, component_id=version.component_id, component_version_id=version.id, event_type=GovernanceEventType.VALIDATION_DECISION, actor_user_id=user.id, from_stage=LifecycleStage.EVALUATED, to_stage=LifecycleStage.VALIDATED, details={"notes": notes[:300]})
    db.commit()
    return version


def approve_version(db: Session, *, organization_id: str, user: User, component_version_id: str, notes: Optional[str]) -> ComponentVersion:
    version = get_component_version(db, organization_id=organization_id, component_version_id=component_version_id)
    if version.lifecycle_stage != LifecycleStage.VALIDATED:
        raise ConflictError(f"Component version '{component_version_id}' must be in stage 'validated' to approve (current: '{version.lifecycle_stage.value}').")
    version.lifecycle_stage = LifecycleStage.APPROVED
    version.approved_at = datetime.now(timezone.utc)
    version.approved_by_user_id = user.id
    version.approval_notes = notes
    db.commit()
    db.refresh(version)
    _log_event(db, organization_id=organization_id, component_id=version.component_id, component_version_id=version.id, event_type=GovernanceEventType.APPROVAL_DECISION, actor_user_id=user.id, from_stage=LifecycleStage.VALIDATED, to_stage=LifecycleStage.APPROVED, details={"notes": (notes or "")[:300]})
    db.commit()
    return version


# ---------------------------------------------------------------------------
# Activation & rollback (§9) — explicit, audited, never automatic; older
# versions are demoted, never deleted.
# ---------------------------------------------------------------------------


def _check_activation_compatibility(db: Session, *, organization_id: str, version: ComponentVersion) -> None:
    """Pre-activation compatibility validation (§9): the version must
    still reference data this organization can actually resolve right now.
    Fails loudly and explicitly rather than activating something that
    would then fail — or worse, silently misbehave — at scoring time."""
    if version.version_kind == VersionKind.MODEL_VERSION:
        model_version = db.get(ModelVersion, version.model_version_id)
        if model_version is None or model_version.status != ModelVersionStatus.TRAINED:
            raise ValidationFailedError("Cannot activate: the underlying detector version is missing or not in a trained state.")
        for sensor_id in model_version.detector_config.get("sensor_ids", []):
            try:
                get_sensor_in_org(db, sensor_id=sensor_id, organization_id=organization_id)
            except NotFoundError:
                raise ValidationFailedError(f"Cannot activate: sensor '{sensor_id}' this detector depends on no longer resolves in this organization.")
    elif version.version_kind == VersionKind.FORECAST_MODEL:
        forecast_model = db.get(ForecastModel, version.forecast_model_id)
        if forecast_model is None or forecast_model.status != ForecastModelStatus.JUSTIFIED:
            raise ValidationFailedError("Cannot activate: the underlying forecast model is missing or not justified.")
        try:
            get_sensor_in_org(db, sensor_id=forecast_model.sensor_id, organization_id=organization_id)
        except NotFoundError:
            raise ValidationFailedError(f"Cannot activate: sensor '{forecast_model.sensor_id}' this forecast model depends on no longer resolves in this organization.")
    # HEURISTIC: pure code, nothing external to validate.


def activate_version(db: Session, *, organization_id: str, user: User, component_id: str, component_version_id: str) -> ComponentVersion:
    component = get_component(db, organization_id=organization_id, component_id=component_id)
    version = get_component_version(db, organization_id=organization_id, component_version_id=component_version_id)
    if version.component_id != component.id:
        raise ValidationFailedError(f"Component version '{component_version_id}' does not belong to component '{component_id}'.")
    if version.lifecycle_stage != LifecycleStage.APPROVED:
        raise ConflictError(f"Component version '{component_version_id}' must be in stage 'approved' to activate (current: '{version.lifecycle_stage.value}').")

    _check_activation_compatibility(db, organization_id=organization_id, version=version)

    previously_active = get_active_version(db, organization_id=organization_id, component_id=component_id)
    superseded_details = None
    if previously_active is not None and previously_active.id != version.id:
        previously_active.lifecycle_stage = LifecycleStage.APPROVED  # demoted, never deleted — still eligible for rollback
        superseded_details = {"superseded_version_id": previously_active.id, "superseded_version_number": previously_active.version_number}

    version.lifecycle_stage = LifecycleStage.ACTIVE
    version.activated_at = datetime.now(timezone.utc)
    version.activated_by_user_id = user.id
    db.commit()
    db.refresh(version)
    _log_event(db, organization_id=organization_id, component_id=component.id, component_version_id=version.id, event_type=GovernanceEventType.ACTIVATED, actor_user_id=user.id, from_stage=LifecycleStage.APPROVED, to_stage=LifecycleStage.ACTIVE, details=superseded_details)
    db.commit()
    return version


def rollback_component(db: Session, *, organization_id: str, user: User, component_id: str, target_component_version_id: Optional[str] = None) -> ComponentVersion:
    component = get_component(db, organization_id=organization_id, component_id=component_id)
    current_active = get_active_version(db, organization_id=organization_id, component_id=component_id)
    if current_active is None:
        raise ConflictError(f"Component '{component.name}' has no active version to roll back from.")

    if target_component_version_id:
        target = get_component_version(db, organization_id=organization_id, component_version_id=target_component_version_id)
        if target.component_id != component.id:
            raise ValidationFailedError(f"Component version '{target_component_version_id}' does not belong to component '{component_id}'.")
        if target.id == current_active.id:
            raise ConflictError("The requested rollback target is already the active version.")
        if target.lifecycle_stage != LifecycleStage.APPROVED:
            raise ConflictError(f"Rollback target must be in stage 'approved' (a previously-activated, never-deleted version); current stage: '{target.lifecycle_stage.value}'.")
    else:
        target = (
            db.query(ComponentVersion)
            .filter(ComponentVersion.component_id == component_id, ComponentVersion.lifecycle_stage == LifecycleStage.APPROVED, ComponentVersion.activated_at.isnot(None), ComponentVersion.id != current_active.id)
            .order_by(ComponentVersion.activated_at.desc())
            .first()
        )
        if target is None:
            raise ConflictError(f"Component '{component.name}' has no previously-active version available to roll back to.")

    _check_activation_compatibility(db, organization_id=organization_id, version=target)

    current_active.lifecycle_stage = LifecycleStage.APPROVED
    target.lifecycle_stage = LifecycleStage.ACTIVE
    target.activated_at = datetime.now(timezone.utc)
    target.activated_by_user_id = user.id
    db.commit()
    db.refresh(target)
    _log_event(
        db, organization_id=organization_id, component_id=component.id, component_version_id=target.id,
        event_type=GovernanceEventType.ROLLED_BACK, actor_user_id=user.id, from_stage=LifecycleStage.ACTIVE, to_stage=LifecycleStage.ACTIVE,
        details={"rolled_back_from_version_id": current_active.id, "rolled_back_from_version_number": current_active.version_number, "rolled_back_to_version_number": target.version_number},
    )
    db.commit()
    return target


def retire_version(db: Session, *, organization_id: str, user: User, component_version_id: str, reason: str) -> ComponentVersion:
    if not reason or not reason.strip():
        raise ValidationFailedError("Retiring a component version requires a non-empty `reason`.")
    version = get_component_version(db, organization_id=organization_id, component_version_id=component_version_id)
    if version.lifecycle_stage == LifecycleStage.RETIRED:
        raise ConflictError("This component version is already retired.")
    from_stage = version.lifecycle_stage
    version.lifecycle_stage = LifecycleStage.RETIRED
    version.retired_at = datetime.now(timezone.utc)
    version.retired_by_user_id = user.id
    version.retirement_reason = reason
    db.commit()
    db.refresh(version)
    _log_event(db, organization_id=organization_id, component_id=version.component_id, component_version_id=version.id, event_type=GovernanceEventType.RETIRED, actor_user_id=user.id, from_stage=from_stage, to_stage=LifecycleStage.RETIRED, details={"reason": reason[:300]})
    db.commit()
    return version


# ---------------------------------------------------------------------------
# Safe scoring / inference contract (§10)
# ---------------------------------------------------------------------------


def score_via_active_component(
    db: Session, *, organization_id: str, user: User, component_id: str, request_kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Resolves the component's currently ACTIVE version and dispatches to
    the real scoring function for its kind, never a stale/default/fabricated
    result. Refuses explicitly (ConflictError/ValidationFailedError) rather
    than guessing when there is no active version, when the underlying
    artifact fails its own checks, or when the request doesn't match what
    that kind of component needs — and every successful result carries the
    exact component/version identity that produced it."""
    component = get_component(db, organization_id=organization_id, component_id=component_id)
    active = get_active_version(db, organization_id=organization_id, component_id=component_id)
    if active is None:
        _log_event(db, organization_id=organization_id, component_id=component.id, component_version_id=None, event_type=GovernanceEventType.SCORING_REFUSED, actor_user_id=user.id, from_stage=None, to_stage=None, details={"reason": "no_active_version"})
        db.commit()
        raise ConflictError(f"Component '{component.name}' has no active version — nothing to score with. Activate a version first.")

    provenance = {
        "component_id": component.id, "component_name": component.name, "component_type": component.component_type.value,
        "component_version_id": active.id, "version_number": active.version_number, "version_kind": active.version_kind.value,
        "activated_at": active.activated_at.isoformat() if active.activated_at else None,
    }

    try:
        if active.version_kind == VersionKind.MODEL_VERSION:
            required = {"scoring_start", "scoring_end"}
            missing = required - request_kwargs.keys()
            if missing:
                raise ValidationFailedError(f"Missing required fields for this component's scoring contract: {sorted(missing)}.")
            run = anomaly_service.create_detection_run(
                db, organization_id=organization_id, user_id=user.id, model_version_id=active.model_version_id,
                scoring_start=request_kwargs["scoring_start"], scoring_end=request_kwargs["scoring_end"],
                allow_retrospective_scoring=bool(request_kwargs.get("allow_retrospective_scoring", False)),
            )
            result = {"result_type": "model_run", "model_run_id": run.id, "status": run.status.value}
        elif active.version_kind == VersionKind.FORECAST_MODEL:
            required = {"as_of", "horizon_seconds", "step_seconds"}
            missing = required - request_kwargs.keys()
            if missing:
                raise ValidationFailedError(f"Missing required fields for this component's scoring contract: {sorted(missing)}.")
            run = condition_service.create_forecast_run(
                db, organization_id=organization_id, user_id=user.id, forecast_model_id=active.forecast_model_id,
                as_of=request_kwargs["as_of"], horizon_seconds=request_kwargs["horizon_seconds"], step_seconds=request_kwargs["step_seconds"],
            )
            result = {"result_type": "forecast_run", "forecast_run_id": run.id, "status": run.status.value}
        else:  # HEURISTIC
            raise ValidationFailedError(
                "Heuristic-kind components (e.g. degradation classification) have no separate scoring "
                "endpoint here — they are invoked directly via their own API (POST /condition/runs). This "
                "registry entry exists for lifecycle/version tracking and visibility, not as a scoring proxy."
            )
    except Exception as exc:  # noqa: BLE001
        _log_event(db, organization_id=organization_id, component_id=component.id, component_version_id=active.id, event_type=GovernanceEventType.RUN_FAILED, actor_user_id=user.id, from_stage=LifecycleStage.ACTIVE, to_stage=LifecycleStage.ACTIVE, details={"error": str(exc)[:300]})
        db.commit()
        raise

    _log_event(db, organization_id=organization_id, component_id=component.id, component_version_id=active.id, event_type=GovernanceEventType.RUN_COMPLETED, actor_user_id=user.id, from_stage=LifecycleStage.ACTIVE, to_stage=LifecycleStage.ACTIVE, details=result)
    db.commit()
    return {"provenance": provenance, **result}


# ---------------------------------------------------------------------------
# Controlled retraining workflow (§13): REQUEST -> (explicit) EXECUTE ->
# real TRAIN/EVALUATE -> a new ComponentVersion at TRAINED/EVALUATED/FAILED.
# Never activates anything automatically.
# ---------------------------------------------------------------------------


def request_retraining(db: Session, *, organization_id: str, user: User, component_id: str, reason: str, config: dict[str, Any]) -> RetrainingRequest:
    if not reason or not reason.strip():
        raise ValidationFailedError("A retraining request requires a non-empty `reason`.")
    component = get_component(db, organization_id=organization_id, component_id=component_id)
    if component.component_type == ComponentType.HEURISTIC_SIGNAL_METHOD:
        raise ConflictError("Heuristic methods have no trainable parameters — there is nothing to retrain. Register a new heuristic_code_version directly if the method's logic or constants changed.")

    request = RetrainingRequest(
        organization_id=organization_id, component_id=component.id, requested_by_user_id=user.id, reason=reason, config=config,
        status=RetrainingRequestStatus.REQUESTED,
    )
    db.add(request)
    db.commit()
    db.refresh(request)
    _log_event(db, organization_id=organization_id, component_id=component.id, component_version_id=None, event_type=GovernanceEventType.RETRAINING_REQUESTED, actor_user_id=user.id, from_stage=None, to_stage=None, details={"reason": reason[:300], "retraining_request_id": request.id})
    db.commit()
    return request


def get_retraining_request(db: Session, *, organization_id: str, request_id: str) -> RetrainingRequest:
    request = db.query(RetrainingRequest).filter(RetrainingRequest.id == request_id, RetrainingRequest.organization_id == organization_id).first()
    if request is None:
        raise NotFoundError(f"Retraining request '{request_id}' not found.")
    return request


def list_retraining_requests(db: Session, *, organization_id: str, component_id: Optional[str], limit: int, offset: int):
    query = db.query(RetrainingRequest).filter(RetrainingRequest.organization_id == organization_id)
    if component_id:
        query = query.filter(RetrainingRequest.component_id == component_id)
    query = query.order_by(RetrainingRequest.created_at.desc())
    total = query.count()
    return query.offset(offset).limit(limit).all(), total


def _resolve_retraining_detector_type(db: Session, *, component: IntelligenceComponent, config: dict[str, Any]) -> DetectorType:
    """A TRAINED_ML_MODEL component can now hold either ISOLATION_FOREST or
    MAHALANOBIS versions (see _COMPONENT_TYPE_TO_DETECTOR_TYPES above), so
    retraining can no longer assume a single fixed detector_type for that
    component_type the way it could before Phase 14. Resolution order:
      1. An explicit config["detector_type"], if given — validated against
         what this component_type allows.
      2. Otherwise, whatever detector_type the component's most recent
         existing version already used — retraining must reproduce the
         same detector, never silently switch it.
      3. Otherwise (a component_type with exactly one allowed detector
         type, e.g. STATISTICAL_DETECTOR, and no prior version), that one
         type.
    Raises ValidationFailedError if none of these can determine it (a
    fresh, never-trained TRAINED_ML_MODEL component retraining-requested
    without specifying detector_type)."""
    allowed = _COMPONENT_TYPE_TO_DETECTOR_TYPES[component.component_type]

    requested = config.get("detector_type")
    if requested:
        try:
            requested_type = DetectorType(requested)
        except ValueError:
            raise ValidationFailedError(f"Unknown detector_type '{requested}'.")
        if requested_type not in allowed:
            allowed_names = ", ".join(sorted(t.value for t in allowed))
            raise ValidationFailedError(f"Component '{component.name}' ({component.component_type.value}) only allows detector_type in [{allowed_names}], not '{requested}'.")
        return requested_type

    latest_version = (
        db.query(ComponentVersion)
        .filter(ComponentVersion.component_id == component.id, ComponentVersion.model_version_id.isnot(None))
        .order_by(ComponentVersion.version_number.desc())
        .first()
    )
    if latest_version is not None:
        existing_model_version = db.query(ModelVersion).filter(ModelVersion.id == latest_version.model_version_id).first()
        if existing_model_version is not None:
            return existing_model_version.detector_type

    if len(allowed) == 1:
        return next(iter(allowed))

    allowed_names = ", ".join(sorted(t.value for t in allowed))
    raise ValidationFailedError(
        f"Component '{component.name}' ({component.component_type.value}) has no prior version to infer a detector_type "
        f"from. Specify config.detector_type (one of [{allowed_names}]) in the retraining request."
    )


def execute_retraining_request(db: Session, *, organization_id: str, user: User, request_id: str) -> RetrainingRequest:
    """Executes exactly one retraining request by calling the SAME real
    training functions Phases 3-4 built. Committing the RUNNING state
    before entering the try block (not just flushing) is deliberate — see
    app.services.condition_service.create_condition_run's identical
    comment for the exact bug this pattern prevents: without a hard
    commit here, a mid-training exception's `db.rollback()` would discard
    this placeholder row too, and the subsequent `db.get(...)` used to
    mark it FAILED would return None and crash instead of reporting the
    real error."""
    request = get_retraining_request(db, organization_id=organization_id, request_id=request_id)
    if request.status != RetrainingRequestStatus.REQUESTED:
        raise ConflictError(f"Retraining request '{request_id}' is not in status 'requested' (current: '{request.status.value}').")

    component = get_component(db, organization_id=organization_id, component_id=request.component_id)
    request.status = RetrainingRequestStatus.RUNNING
    request.started_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(request)

    try:
        config = request.config
        if component.component_type in (ComponentType.TRAINED_ML_MODEL, ComponentType.STATISTICAL_DETECTOR):
            detector_type = _resolve_retraining_detector_type(db, component=component, config=config)
            model_version = anomaly_service.create_model_version(
                db, organization_id=organization_id, user_id=user.id, name=component.name, detector_type=detector_type,
                sensor_ids=config["sensor_ids"],
                reference_start=datetime.fromisoformat(config["reference_start"]), reference_end=datetime.fromisoformat(config["reference_end"]),
                threshold_overrides=config.get("threshold_overrides", {}), feature_specs_raw=config.get("feature_specs"), step_seconds=config.get("step_seconds"),
            )
            version = ComponentVersion(
                organization_id=organization_id, component_id=component.id, version_number=model_version.version, version_kind=VersionKind.MODEL_VERSION,
                model_version_id=model_version.id, lifecycle_stage=_initial_stage_for_model_version(model_version), created_by_user_id=user.id,
            )
        elif component.component_type == ComponentType.FORECASTING_MODEL:
            forecast_model = condition_service.create_forecast_model(
                db, organization_id=organization_id, user_id=user.id, sensor_id=config["sensor_id"], name=component.name,
                fit_start=datetime.fromisoformat(config["fit_start"]), fit_end=datetime.fromisoformat(config["fit_end"]),
                test_end=datetime.fromisoformat(config["test_end"]), seasonal_period_seconds=config.get("seasonal_period_seconds"),
            )
            version = ComponentVersion(
                organization_id=organization_id, component_id=component.id, version_number=forecast_model.version, version_kind=VersionKind.FORECAST_MODEL,
                forecast_model_id=forecast_model.id, lifecycle_stage=_initial_stage_for_forecast_model(forecast_model), created_by_user_id=user.id,
            )
        else:
            raise ConflictError("Heuristic methods have no trainable parameters.")

        db.add(version)
        db.commit()
        db.refresh(version)
        _log_event(db, organization_id=organization_id, component_id=component.id, component_version_id=version.id, event_type=GovernanceEventType.VERSION_REGISTERED, actor_user_id=user.id, from_stage=None, to_stage=version.lifecycle_stage, details={"via_retraining_request_id": request.id, "version_number": version.version_number})

        request.status = RetrainingRequestStatus.FAILED if version.lifecycle_stage == LifecycleStage.FAILED else RetrainingRequestStatus.EVALUATED
        request.resulting_component_version_id = version.id
        request.finished_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(request)
        _log_event(
            db, organization_id=organization_id, component_id=component.id, component_version_id=version.id,
            event_type=GovernanceEventType.RUN_COMPLETED if request.status == RetrainingRequestStatus.EVALUATED else GovernanceEventType.RUN_FAILED,
            actor_user_id=user.id, from_stage=None, to_stage=version.lifecycle_stage, details={"retraining_request_id": request.id},
        )
        db.commit()
        return request

    except Exception as exc:  # noqa: BLE001
        db.rollback()
        request = db.get(RetrainingRequest, request_id)
        request.status = RetrainingRequestStatus.FAILED
        request.error_message = str(exc)[:1000]
        request.finished_at = datetime.now(timezone.utc)
        db.commit()
        raise


# ---------------------------------------------------------------------------
# Data / feature shift analysis (§12) — genuinely new; never auto-retrains.
# ---------------------------------------------------------------------------


def run_shift_analysis(
    db: Session, *, organization_id: str, user: User, sensor_id: str, component_id: Optional[str],
    reference_start: datetime, reference_end: datetime, comparison_start: datetime, comparison_end: datetime,
) -> ShiftAnalysisRun:
    validate_time_range(reference_start, reference_end)
    validate_time_range(comparison_start, comparison_end)
    sensor = get_sensor_in_org(db, sensor_id=sensor_id, organization_id=organization_id)
    if component_id:
        get_component(db, organization_id=organization_id, component_id=component_id)  # 404s if wrong org/missing

    try:
        reference_series = load_measurement_series(db, sensor_id=sensor.id, start=reference_start, end=reference_end)
        comparison_series = load_measurement_series(db, sensor_id=sensor.id, start=comparison_start, end=comparison_end)
    except TooManyObservationsError as exc:
        raise ValidationFailedError(str(exc))

    def _period_summary(series: pd.Series, *, start: datetime, end: datetime) -> dict[str, Any]:
        profile = profiling.compute_profile(series, sensor_unit=sensor.unit, expected_interval_seconds=sensor.expected_interval_seconds, requested_start=start, requested_end=end)
        summary: dict[str, Any] = {
            "observation_count": len(series),
            "missing_fraction": profile["missingness"]["missing_fraction"] if profile["missingness"] else None,
        }
        if len(series) > 0:
            summary.update({"mean": float(series.mean()), "std": float(series.std(ddof=1)) if len(series) > 1 else 0.0, "median": float(series.median()), "min": float(series.min()), "max": float(series.max())})
        return summary

    reference_summary = _period_summary(reference_series, start=reference_start, end=reference_end)
    comparison_summary = _period_summary(comparison_series, start=comparison_start, end=comparison_end)

    limitations = (
        "The statistical tests below assume independent, identically-distributed samples. Sensor "
        "time-series observations are typically autocorrelated, so these p-values are indicative of a "
        "distributional difference worth a human look, not a formally calibrated significance level. "
        "PSI (population stability index) is deliberately not computed here — it requires pre-defined, "
        "stable bins from a trusted reference distribution that this platform has no validated basis for "
        "choosing yet; adding it would be exactly the invented-precision this project avoids."
    )

    if len(reference_series) < MIN_SHIFT_OBSERVATIONS or len(comparison_series) < MIN_SHIFT_OBSERVATIONS:
        method = "summary_statistics_only_insufficient_for_test"
        result = {
            "reference": reference_summary, "comparison": comparison_summary,
            "test_performed": False,
            "reason": f"At least {MIN_SHIFT_OBSERVATIONS} observations are required in both periods for a statistical test (reference n={len(reference_series)}, comparison n={len(comparison_series)}).",
        }
    else:
        from scipy import stats as scipy_stats

        t_stat, t_pvalue = scipy_stats.ttest_ind(reference_series.values, comparison_series.values, equal_var=False)
        ks_stat, ks_pvalue = scipy_stats.ks_2samp(reference_series.values, comparison_series.values)
        method = "summary_statistics_welch_t_test_ks_2samp"
        result = {
            "reference": reference_summary, "comparison": comparison_summary,
            "test_performed": True,
            "mean_difference": comparison_summary["mean"] - reference_summary["mean"],
            "missingness_difference": (
                (comparison_summary["missing_fraction"] or 0.0) - (reference_summary["missing_fraction"] or 0.0)
                if comparison_summary["missing_fraction"] is not None and reference_summary["missing_fraction"] is not None else None
            ),
            "welch_t_test": {"statistic": float(t_stat), "p_value": float(t_pvalue), "tests": "difference in mean"},
            "ks_2samp_test": {"statistic": float(ks_stat), "p_value": float(ks_pvalue), "tests": "difference in overall distribution shape"},
        }

    run = ShiftAnalysisRun(
        organization_id=organization_id, sensor_id=sensor.id, component_id=component_id,
        reference_period_start=reference_start, reference_period_end=reference_end,
        comparison_period_start=comparison_start, comparison_period_end=comparison_end,
        method=method, result=result, limitations=limitations, created_by_user_id=user.id,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    if component_id:
        _log_event(db, organization_id=organization_id, component_id=component_id, component_version_id=None, event_type=GovernanceEventType.SHIFT_ANALYSIS_RECORDED, actor_user_id=user.id, from_stage=None, to_stage=None, details={"sensor_id": sensor.id, "shift_analysis_run_id": run.id, "test_performed": result.get("test_performed")})
        db.commit()
    return run


def get_shift_analysis_run(db: Session, *, organization_id: str, run_id: str) -> ShiftAnalysisRun:
    run = db.query(ShiftAnalysisRun).filter(ShiftAnalysisRun.id == run_id, ShiftAnalysisRun.organization_id == organization_id).first()
    if run is None:
        raise NotFoundError(f"Shift analysis run '{run_id}' not found.")
    return run


def list_shift_analysis_runs(db: Session, *, organization_id: str, sensor_id: Optional[str], limit: int, offset: int):
    query = db.query(ShiftAnalysisRun).filter(ShiftAnalysisRun.organization_id == organization_id)
    if sensor_id:
        query = query.filter(ShiftAnalysisRun.sensor_id == sensor_id)
    query = query.order_by(ShiftAnalysisRun.created_at.desc())
    total = query.count()
    return query.offset(offset).limit(limit).all(), total


# ---------------------------------------------------------------------------
# Governance audit log (§14) — read access
# ---------------------------------------------------------------------------


def list_governance_events(db: Session, *, organization_id: str, component_id: Optional[str], limit: int, offset: int):
    query = db.query(GovernanceEvent).filter(GovernanceEvent.organization_id == organization_id)
    if component_id:
        query = query.filter(GovernanceEvent.component_id == component_id)
    query = query.order_by(GovernanceEvent.created_at.desc())
    total = query.count()
    return query.offset(offset).limit(limit).all(), total


# ---------------------------------------------------------------------------
# Monitoring (§11) — OPERATIONAL / DATA / MODEL PERFORMANCE, kept distinct.
# Reuses Phase 3/4's real monitoring summaries rather than duplicating
# their queries; never calls a distribution comparison "drift".
# ---------------------------------------------------------------------------


def get_mlops_monitoring_summary(db: Session, *, organization_id: str) -> dict[str, Any]:
    component_count_by_type = dict(
        db.query(IntelligenceComponent.component_type, func.count(IntelligenceComponent.id)).filter(IntelligenceComponent.organization_id == organization_id).group_by(IntelligenceComponent.component_type).all()
    )
    component_count_by_type = {k.value: v for k, v in component_count_by_type.items()}

    version_count_by_stage = dict(
        db.query(ComponentVersion.lifecycle_stage, func.count(ComponentVersion.id)).filter(ComponentVersion.organization_id == organization_id).group_by(ComponentVersion.lifecycle_stage).all()
    )
    version_count_by_stage = {k.value: v for k, v in version_count_by_stage.items()}

    retraining_count_by_status = dict(
        db.query(RetrainingRequest.status, func.count(RetrainingRequest.id)).filter(RetrainingRequest.organization_id == organization_id).group_by(RetrainingRequest.status).all()
    )
    retraining_count_by_status = {k.value: v for k, v in retraining_count_by_status.items()}

    governance_event_count_by_type = dict(
        db.query(GovernanceEvent.event_type, func.count(GovernanceEvent.id)).filter(GovernanceEvent.organization_id == organization_id).group_by(GovernanceEvent.event_type).all()
    )
    governance_event_count_by_type = {k.value: v for k, v in governance_event_count_by_type.items()}

    active_components = (
        db.query(IntelligenceComponent.id, IntelligenceComponent.name, ComponentVersion.version_number)
        .join(ComponentVersion, ComponentVersion.component_id == IntelligenceComponent.id)
        .filter(IntelligenceComponent.organization_id == organization_id, ComponentVersion.lifecycle_stage == LifecycleStage.ACTIVE)
        .all()
    )

    shift_run_count = db.query(func.count(ShiftAnalysisRun.id)).filter(ShiftAnalysisRun.organization_id == organization_id).scalar() or 0

    return {
        "operational": {
            "component_count_by_type": component_count_by_type,
            "component_version_count_by_lifecycle_stage": version_count_by_stage,
            "retraining_request_count_by_status": retraining_count_by_status,
            "governance_event_count_by_type": governance_event_count_by_type,
            "active_components": [{"component_id": cid, "name": name, "active_version_number": v} for cid, name, v in active_components],
        },
        "data": {
            "shift_analysis_run_count": shift_run_count,
            "note": "See GET /mlops/shift-analysis for per-sensor reference-vs-comparison statistical summaries. This platform does not compute a continuous drift score — every shift analysis is a discrete, explicitly-requested comparison between two stated periods.",
        },
        "model_performance": {
            "anomaly_detection": anomaly_service.get_monitoring_summary(db, organization_id=organization_id),
            "condition_and_forecasting": condition_service.get_condition_monitoring_summary(db, organization_id=organization_id),
            "note": "Both summaries above are real counts of runs/detectors/signals by outcome — not accuracy metrics. No delayed real-world performance monitoring exists because no verified failure/maintenance-outcome labels exist for this platform (see docs/CONDITION_INTELLIGENCE.md §0).",
        },
        "not_implemented": [
            "continuous/automated data drift scoring (only discrete, on-demand shift analysis is implemented — see the 'data' section above)",
            "delayed model-performance monitoring against real outcomes (no ground-truth labels exist)",
        ],
    }
