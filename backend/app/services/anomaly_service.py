"""Orchestration layer for anomaly detection: resolves organization/sensor
scope, loads bounded data, delegates to the DB-agnostic app.anomaly.*
modules, and persists results.

Causality boundary for detection (see also app.anomaly.baseline's module
docstring): a ModelVersion's reference period is fixed at training time.
By default, `create_detection_run` REFUSES to score a period that starts
before the model's reference period ends — scoring data 2023 with a
baseline built partly from 2024 would be using future information to
judge the past. `allow_retrospective_scoring=True` lifts this for
deliberate retrospective/exploratory analysis, and every such run is
labeled as retrospective in its stored metrics so nobody mistakes it for
a causally-clean detection replay.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pandas as pd
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.anomaly import baseline as baseline_mod
from app.anomaly import evaluation as evaluation_mod
from app.anomaly import isolation_forest_detector, mahalanobis_detector, robust_zscore, scoring, synthetic_scenarios
from app.anomaly import multivariate_synthetic_scenarios
from app.anomaly.thresholds import validate_threshold_config
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.models.job import JobType
from app.models.intelligence import (
    Anomaly,
    AnomalyInvestigation,
    AnomalySeverityTier,
    AnomalyStatus,
    DetectorType,
    ModelRun,
    ModelRunStatus,
    ModelVersion,
    ModelVersionStatus,
)
from app.models.sensor import Sensor
from app.models.user import User
from app.services.timeseries_service import get_sensor_in_org, validate_time_range
from app.timeseries.data_access import TooManyObservationsError, load_measurement_series, load_multi_sensor_series
from app.timeseries.dataset_builder import FeatureSpec
from app.timeseries.features import FeatureKind
from app.timeseries import profiling

def _ensure_utc(dt: datetime) -> datetime:
    """Normalizes a datetime to UTC-aware before any comparison/arithmetic
    that might mix it with another datetime of unknown awareness.

    Verified empirically (see test setup used during development): SQLite,
    even through a `DateTime(timezone=True)` column, does not preserve
    tzinfo across a write+reload — every datetime loaded off a model
    instance from the DB comes back naive (but is always UTC, since that's
    the only thing ever written). A datetime supplied by an API caller may
    already be tz-aware. Comparing or subtracting a naive and an aware
    datetime raises TypeError, so anywhere this module mixes a DB-loaded
    datetime with a caller-supplied one, both are passed through this
    first.
    """
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


DEFAULT_STEP_SECONDS = 1800.0
DEFAULT_ISOLATION_FEATURE_WINDOW_SECONDS = 3600.0
DEFAULT_EXPECTED_INTERVAL_SECONDS = 300.0  # fallback when a sensor declares no expected_interval_seconds
MAX_DETECTION_ROWS_SYNC = 3000  # mirrors app.timeseries.dataset_builder.MAX_DATASET_ROWS_SYNC
ALLOWED_TRANSITIONS: dict[AnomalyStatus, set[AnomalyStatus]] = {
    AnomalyStatus.DETECTED: {AnomalyStatus.UNDER_REVIEW, AnomalyStatus.ACKNOWLEDGED, AnomalyStatus.FALSE_POSITIVE, AnomalyStatus.RESOLVED},
    AnomalyStatus.UNDER_REVIEW: {AnomalyStatus.ACKNOWLEDGED, AnomalyStatus.FALSE_POSITIVE, AnomalyStatus.RESOLVED, AnomalyStatus.DETECTED},
    AnomalyStatus.ACKNOWLEDGED: {AnomalyStatus.RESOLVED, AnomalyStatus.FALSE_POSITIVE, AnomalyStatus.UNDER_REVIEW},
    AnomalyStatus.RESOLVED: {AnomalyStatus.UNDER_REVIEW},  # reopenable, e.g. the issue recurred
    AnomalyStatus.FALSE_POSITIVE: {AnomalyStatus.UNDER_REVIEW},  # reopenable if a reviewer changes their mind
}


def _default_feature_specs(sensor_ids: list[str], *, window_seconds: float) -> list[dict[str, Any]]:
    kinds = [FeatureKind.MEAN, FeatureKind.STD, FeatureKind.RATE_OF_CHANGE]
    specs = []
    for sid in sensor_ids:
        for kind in kinds:
            specs.append({"feature_name": f"{sid}:{kind.value}", "sensor_id": sid, "kind": kind.value, "window_seconds": window_seconds, "params": {}})
    return specs


def _feature_specs_from_config(raw: list[dict[str, Any]]) -> list[FeatureSpec]:
    return [FeatureSpec(feature_name=s["feature_name"], sensor_id=s["sensor_id"], kind=FeatureKind(s["kind"]), window_seconds=s.get("window_seconds"), params=s.get("params") or {}) for s in raw]


def _resolve_sensors(db: Session, *, sensor_ids: list[str], organization_id: str) -> list[Sensor]:
    sensors = [get_sensor_in_org(db, sensor_id=sid, organization_id=organization_id) for sid in sensor_ids]
    asset_ids = {s.asset_id for s in sensors}
    if len(asset_ids) > 1:
        raise ValidationFailedError("All sensors in one detector must belong to the same asset (multi-sensor context, not cross-asset).")
    return sensors


def create_model_version(
    db: Session,
    *,
    organization_id: str,
    user_id: Optional[str],
    name: str,
    detector_type: DetectorType,
    sensor_ids: list[str],
    reference_start: datetime,
    reference_end: datetime,
    threshold_overrides: dict[str, Any],
    feature_specs_raw: Optional[list[dict[str, Any]]],
    step_seconds: Optional[float],
) -> ModelVersion:
    validate_time_range(reference_start, reference_end)
    sensors = _resolve_sensors(db, sensor_ids=sensor_ids, organization_id=organization_id)

    try:
        threshold_config = validate_threshold_config(detector_type, threshold_overrides)
    except ValueError as exc:
        raise ValidationFailedError(str(exc))

    if detector_type == DetectorType.ROBUST_ZSCORE and len(sensor_ids) != 1:
        raise ValidationFailedError("robust_zscore detectors watch exactly one sensor. Use isolation_forest or mahalanobis for multi-sensor context.")
    if detector_type == DetectorType.MAHALANOBIS and len(sensor_ids) < 2:
        raise ValidationFailedError("mahalanobis detectors need at least 2 sensors — it measures whether their relationship is unusual, which is undefined for one sensor alone. Use robust_zscore or isolation_forest for a single sensor.")

    next_version = (
        db.query(ModelVersion).filter(ModelVersion.organization_id == organization_id, ModelVersion.name == name).count() + 1
    )

    detector_config: dict[str, Any] = {
        "sensor_ids": sensor_ids,
        "threshold": threshold_config.to_dict(),
    }

    status = ModelVersionStatus.TRAINED
    training_error = None
    baseline_summary: Optional[dict[str, Any]] = None
    feature_schema: Optional[dict[str, Any]] = None
    reference_observation_count = 0

    try:
        if detector_type == DetectorType.ROBUST_ZSCORE:
            try:
                series = load_measurement_series(db, sensor_id=sensors[0].id, start=reference_start, end=reference_end)
            except TooManyObservationsError as exc:
                raise ValidationFailedError(str(exc))
            baseline = baseline_mod.compute_baseline(series, reference_start=reference_start, reference_end=reference_end)
            baseline_summary = baseline.to_dict()
            reference_observation_count = baseline.sample_count

        elif detector_type == DetectorType.ISOLATION_FOREST:
            step = step_seconds or DEFAULT_STEP_SECONDS
            feature_specs_raw = feature_specs_raw or _default_feature_specs(sensor_ids, window_seconds=DEFAULT_ISOLATION_FEATURE_WINDOW_SECONDS)
            detector_config["feature_specs"] = feature_specs_raw
            detector_config["step_seconds"] = step
            try:
                series_by_sensor = load_multi_sensor_series(db, sensor_ids=sensor_ids, start=reference_start, end=reference_end)
            except TooManyObservationsError as exc:
                raise ValidationFailedError(str(exc))
            summary = isolation_forest_detector.build_reference_summary(
                series_by_sensor=series_by_sensor,
                feature_specs=_feature_specs_from_config(feature_specs_raw),
                reference_start=reference_start,
                reference_end=reference_end,
                step_seconds=step,
            )
            baseline_summary = summary.to_dict()
            feature_schema = {"feature_names": summary.feature_names}
            reference_observation_count = summary.reference_row_count

        elif detector_type == DetectorType.MAHALANOBIS:
            step = step_seconds or DEFAULT_STEP_SECONDS
            detector_config["step_seconds"] = step
            try:
                series_by_sensor = load_multi_sensor_series(db, sensor_ids=sensor_ids, start=reference_start, end=reference_end)
            except TooManyObservationsError as exc:
                raise ValidationFailedError(str(exc))
            summary = mahalanobis_detector.build_reference_summary(
                series_by_sensor=series_by_sensor, sensor_ids=sensor_ids, reference_start=reference_start, reference_end=reference_end, step_seconds=step,
            )
            baseline_summary = summary.to_dict()
            reference_observation_count = summary.reference_row_count
        else:
            raise ValidationFailedError(f"Unknown detector_type '{detector_type}'.")
    except (baseline_mod.InsufficientReferenceDataError, isolation_forest_detector.InsufficientFeatureDataError, mahalanobis_detector.InsufficientReferenceDataError) as exc:
        status = ModelVersionStatus.FAILED
        training_error = str(exc)
    except mahalanobis_detector.TooManySensorsError as exc:
        raise ValidationFailedError(str(exc))

    model_version = ModelVersion(
        organization_id=organization_id,
        name=name,
        version=next_version,
        detector_type=detector_type,
        detector_config=detector_config,
        reference_period_start=reference_start,
        reference_period_end=reference_end,
        reference_observation_count=reference_observation_count,
        feature_schema=feature_schema,
        baseline_summary=baseline_summary,
        status=status,
        training_error=training_error,
        trained_at=datetime.now(timezone.utc),
        created_by_user_id=user_id,
    )
    db.add(model_version)
    db.commit()
    db.refresh(model_version)
    return model_version


def get_model_version(db: Session, *, organization_id: str, model_version_id: str) -> ModelVersion:
    mv = db.query(ModelVersion).filter(ModelVersion.id == model_version_id, ModelVersion.organization_id == organization_id).first()
    if mv is None:
        raise NotFoundError(f"Detector '{model_version_id}' not found.")
    return mv


def list_model_versions(db: Session, *, organization_id: str, limit: int, offset: int):
    query = db.query(ModelVersion).filter(ModelVersion.organization_id == organization_id).order_by(ModelVersion.created_at.desc())
    total = query.count()
    return query.offset(offset).limit(limit).all(), total


def _data_quality_context(db: Session, *, sensor: Sensor, organization_id: str, start: datetime, end: datetime) -> dict[str, Any]:
    try:
        series = load_measurement_series(db, sensor_id=sensor.id, start=start, end=end)
    except TooManyObservationsError:
        return {"note": "range too large to summarize data quality inline"}
    profile = profiling.compute_profile(series, sensor_unit=sensor.unit, expected_interval_seconds=sensor.expected_interval_seconds, requested_start=start, requested_end=end)
    return {
        "observation_count": profile["observation_count"],
        "missing_fraction": profile["missingness"]["missing_fraction"] if profile["missingness"] else None,
        "gap_count": profile["gap_count"],
        "warnings": profile["data_quality_warnings"],
    }


def _estimate_scoring_row_count(model_version: ModelVersion, *, scoring_start: datetime, scoring_end: datetime) -> int:
    """A quick, deliberately-approximate row-count estimate used only to
    decide the sync/async threshold below — NOT used for anything that
    needs to be exact (the detectors themselves bound their own actual
    row counts via TooManyObservationsError / grid_row_count)."""
    span_seconds = (scoring_end - scoring_start).total_seconds()
    if model_version.detector_type in (DetectorType.ISOLATION_FOREST, DetectorType.MAHALANOBIS):
        step = model_version.detector_config.get("step_seconds") or DEFAULT_STEP_SECONDS
    else:
        step = DEFAULT_EXPECTED_INTERVAL_SECONDS
    return max(1, int(span_seconds / step) + 1)


def create_detection_run(
    db: Session,
    *,
    organization_id: str,
    user_id: Optional[str],
    model_version_id: str,
    scoring_start: datetime,
    scoring_end: datetime,
    allow_retrospective_scoring: bool,
    correlation_id: Optional[str] = None,
) -> ModelRun:
    """Creates a ModelRun row, then either executes detection inline
    (small scoring windows — mirrors app.services.feature_service.
    create_dataset_build's MAX_DATASET_ROWS_SYNC split) or hands it to
    the background job runner (JobType.ANOMALY_DETECTION_RUN, handled by
    app.jobs.handlers._run_anomaly_detection) for larger ones. Either way
    the caller gets back a ModelRun whose `status` honestly reflects
    what actually happened — QUEUED (async, not started yet), RUNNING,
    SUCCEEDED, or FAILED — never a fake "in progress" placeholder."""
    validate_time_range(scoring_start, scoring_end)
    model_version = get_model_version(db, organization_id=organization_id, model_version_id=model_version_id)
    if model_version.status != ModelVersionStatus.TRAINED:
        raise ConflictError(f"Detector '{model_version_id}' is not in a trained state (status={model_version.status.value}); cannot run detection.")

    if _ensure_utc(scoring_start) < _ensure_utc(model_version.reference_period_end) and not allow_retrospective_scoring:
        raise ValidationFailedError(
            "Scoring period starts before this detector's reference period ends — scoring would implicitly use "
            "data the baseline was never meant to see ahead of. Choose a scoring period that starts at or after "
            f"{model_version.reference_period_end.isoformat()}, or pass allow_retrospective_scoring=true for a "
            "deliberate retrospective/exploratory run (labeled as such in the run's metrics)."
        )

    run = ModelRun(
        organization_id=organization_id,
        model_version_id=model_version.id,
        status=ModelRunStatus.QUEUED,
        scored_period_start=scoring_start,
        scored_period_end=scoring_end,
        created_by_user_id=user_id,
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    estimated_rows = _estimate_scoring_row_count(model_version, scoring_start=scoring_start, scoring_end=scoring_end)
    retrospective = _ensure_utc(scoring_start) < _ensure_utc(model_version.reference_period_end)

    if estimated_rows <= MAX_DETECTION_ROWS_SYNC:
        execute_detection_run(db, run_id=run.id, retrospective=retrospective)
        db.refresh(run)
        return run

    from app.jobs.runner import DuplicateJobError, job_runner

    try:
        job_id = job_runner.submit(
            organization_id=organization_id,
            job_type=JobType.ANOMALY_DETECTION_RUN,
            payload={"run_id": run.id, "retrospective": retrospective},
            correlation_id=correlation_id,
            dedup_key=run.id,
        )
    except DuplicateJobError as exc:
        raise ConflictError(
            "A detection job is already running for this model run.",
            details={"existing_job_id": exc.existing_job_id},
        ) from exc
    run.background_job_id = job_id
    db.commit()
    db.refresh(run)
    return run


def execute_detection_run(db: Session, *, run_id: str, is_synthetic_scenario: bool = False, retrospective: bool = False) -> dict[str, Any]:
    """The actual detection work — called from the background job handler
    (app.jobs.handlers._run_anomaly_detection) with its own DB session, so
    this function takes a session rather than assuming a request-scoped
    one. Returns a summary dict; also mutates ModelRun and creates Anomaly
    rows."""
    run = db.get(ModelRun, run_id)
    if run is None:
        raise NotFoundError(f"Detection run '{run_id}' not found.")
    model_version = db.get(ModelVersion, run.model_version_id)

    run.status = ModelRunStatus.RUNNING
    run.started_at = datetime.now(timezone.utc)
    db.commit()

    try:
        threshold_config = validate_threshold_config(model_version.detector_type, model_version.detector_config.get("threshold", {}))
        sensor_ids = model_version.detector_config["sensor_ids"]
        sensors = {s.id: s for s in [get_sensor_in_org(db, sensor_id=sid, organization_id=model_version.organization_id) for sid in sensor_ids]}
        primary_sensor = sensors[sensor_ids[0]]

        if model_version.detector_type == DetectorType.ROBUST_ZSCORE:
            try:
                series = load_measurement_series(db, sensor_id=sensor_ids[0], start=run.scored_period_start, end=run.scored_period_end)
            except TooManyObservationsError as exc:
                raise ValidationFailedError(str(exc))
            baseline = baseline_mod.Baseline(**model_version.baseline_summary)
            result = robust_zscore.detect(series, baseline, threshold=threshold_config.threshold)
            candidates = [c.to_dict() if hasattr(c, "to_dict") else c for c in result["candidates"]]
            points_scored, points_flagged = result["points_scored"], result["points_flagged"]
            run_note = result["note"]

        elif model_version.detector_type == DetectorType.ISOLATION_FOREST:
            feature_specs_raw = model_version.detector_config["feature_specs"]
            step_seconds = model_version.detector_config["step_seconds"]
            try:
                series_by_sensor = load_multi_sensor_series(
                    db, sensor_ids=sensor_ids,
                    start=min(_ensure_utc(model_version.reference_period_start), _ensure_utc(run.scored_period_start)),
                    end=run.scored_period_end,
                )
            except TooManyObservationsError as exc:
                raise ValidationFailedError(str(exc))
            result = isolation_forest_detector.detect(
                series_by_sensor=series_by_sensor,
                feature_specs=_feature_specs_from_config(feature_specs_raw),
                reference_start=model_version.reference_period_start,
                reference_end=model_version.reference_period_end,
                scoring_start=run.scored_period_start,
                scoring_end=run.scored_period_end,
                step_seconds=step_seconds,
                threshold=threshold_config.threshold,
            )
            candidates = result["candidates"]
            points_scored, points_flagged = result["points_scored"], result["points_flagged"]
            run_note = result["note"]

        elif model_version.detector_type == DetectorType.MAHALANOBIS:
            step_seconds = model_version.detector_config["step_seconds"]
            try:
                series_by_sensor = load_multi_sensor_series(
                    db, sensor_ids=sensor_ids,
                    start=min(_ensure_utc(model_version.reference_period_start), _ensure_utc(run.scored_period_start)),
                    end=run.scored_period_end,
                )
            except TooManyObservationsError as exc:
                raise ValidationFailedError(str(exc))
            result = mahalanobis_detector.detect(
                series_by_sensor=series_by_sensor,
                sensor_ids=sensor_ids,
                reference_start=model_version.reference_period_start,
                reference_end=model_version.reference_period_end,
                scoring_start=run.scored_period_start,
                scoring_end=run.scored_period_end,
                step_seconds=step_seconds,
                threshold=threshold_config.threshold,
            )
            candidates = result["candidates"]
            points_scored, points_flagged = result["points_scored"], result["points_flagged"]
            run_note = result["note"]
        else:
            raise ValidationFailedError(f"Unknown detector_type '{model_version.detector_type}'.")

        created_anomalies = []
        for cand in candidates:
            fields = scoring.build_anomaly_fields(cand, detector_type=model_version.detector_type, threshold_config=threshold_config)
            dq_context = _data_quality_context(
                db, sensor=primary_sensor, organization_id=model_version.organization_id,
                start=datetime.fromisoformat(fields["period_start"]), end=datetime.fromisoformat(fields["period_end"]),
            )
            anomaly = Anomaly(
                organization_id=model_version.organization_id,
                asset_id=primary_sensor.asset_id,
                sensor_id=primary_sensor.id if len(sensor_ids) == 1 else None,
                model_run_id=run.id,
                status=AnomalyStatus.DETECTED,
                period_start=datetime.fromisoformat(fields["period_start"]),
                period_end=datetime.fromisoformat(fields["period_end"]),
                summary=fields["summary"],
                detector_type=model_version.detector_type,
                raw_score=fields["raw_score"],
                normalized_score=fields["normalized_score"],
                threshold_used=fields["threshold_used"],
                severity_tier=fields["severity_tier"],
                contributing_signals=fields["contributing_signals"],
                data_quality_context=dq_context,
                is_synthetic_scenario=is_synthetic_scenario,
            )
            db.add(anomaly)
            created_anomalies.append(anomaly)

        run.status = ModelRunStatus.SUCCEEDED
        run.metrics = {
            "points_scored": points_scored,
            "points_flagged": points_flagged,
            "anomalies_created": len(created_anomalies),
            "note": run_note,
            "retrospective": retrospective,
        }
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        return {"run_id": run.id, "status": "succeeded", "anomalies_created": len(created_anomalies), "points_scored": points_scored}

    except Exception as exc:  # noqa: BLE001
        db.rollback()
        run = db.get(ModelRun, run_id)
        run.status = ModelRunStatus.FAILED
        run.error_message = str(exc)[:1000]
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        raise


def get_model_run(db: Session, *, organization_id: str, run_id: str) -> ModelRun:
    run = db.query(ModelRun).filter(ModelRun.id == run_id, ModelRun.organization_id == organization_id).first()
    if run is None:
        raise NotFoundError(f"Detection run '{run_id}' not found.")
    return run


def list_model_runs(db: Session, *, organization_id: str, model_version_id: Optional[str], limit: int, offset: int):
    query = db.query(ModelRun).filter(ModelRun.organization_id == organization_id)
    if model_version_id:
        query = query.filter(ModelRun.model_version_id == model_version_id)
    query = query.order_by(ModelRun.created_at.desc())
    total = query.count()
    return query.offset(offset).limit(limit).all(), total


def list_anomalies(
    db: Session,
    *,
    organization_id: str,
    asset_id: Optional[str],
    sensor_id: Optional[str],
    status: Optional[AnomalyStatus],
    severity_tier: Optional[AnomalySeverityTier],
    start: Optional[datetime],
    end: Optional[datetime],
    limit: int,
    offset: int,
):
    query = db.query(Anomaly).filter(Anomaly.organization_id == organization_id)
    if asset_id:
        query = query.filter(Anomaly.asset_id == asset_id)
    if sensor_id:
        query = query.filter(Anomaly.sensor_id == sensor_id)
    if status:
        query = query.filter(Anomaly.status == status)
    if severity_tier:
        query = query.filter(Anomaly.severity_tier == severity_tier)
    if start:
        query = query.filter(Anomaly.period_end >= start)
    if end:
        query = query.filter(Anomaly.period_start <= end)
    query = query.order_by(Anomaly.period_start.desc())
    total = query.count()
    return query.offset(offset).limit(limit).all(), total


def get_anomaly(db: Session, *, organization_id: str, anomaly_id: str) -> Anomaly:
    anomaly = db.query(Anomaly).filter(Anomaly.id == anomaly_id, Anomaly.organization_id == organization_id).first()
    if anomaly is None:
        raise NotFoundError(f"Anomaly '{anomaly_id}' not found.")
    return anomaly


def get_anomaly_context(db: Session, *, organization_id: str, anomaly_id: str, context_buffer: timedelta = timedelta(hours=6)) -> dict[str, Any]:
    """Temporal context for one anomaly: before/anomalous/after windows
    from the same sensor, per spec section 7 ("what changed", not just a
    score). Falls back gracefully when the sensor has too much data to
    load inline (reuses the same TooManyObservationsError bound as
    everywhere else)."""
    anomaly = get_anomaly(db, organization_id=organization_id, anomaly_id=anomaly_id)
    if anomaly.sensor_id is None:
        return {"anomaly_id": anomaly.id, "note": "This anomaly spans multiple sensors (multivariate detection) — see contributing_signals for per-feature detail rather than a single time series."}

    sensor = get_sensor_in_org(db, sensor_id=anomaly.sensor_id, organization_id=organization_id)
    # load_measurement_series (app.timeseries.data_access) always returns a
    # UTC tz-aware index regardless of storage, so normalize the DB-loaded
    # anomaly timestamps the same way (see _ensure_utc) before comparing.
    period_start = _ensure_utc(anomaly.period_start)
    period_end = _ensure_utc(anomaly.period_end)
    context_start = period_start - context_buffer
    context_end = period_end + context_buffer
    try:
        series = load_measurement_series(db, sensor_id=sensor.id, start=context_start, end=context_end)
    except TooManyObservationsError as exc:
        raise ValidationFailedError(str(exc))

    before = series[series.index < period_start]
    during = series[(series.index >= period_start) & (series.index <= period_end)]
    after = series[series.index > period_end]

    def _summarize(s):
        if len(s) == 0:
            return None
        return {"observation_count": len(s), "mean": float(s.mean()), "min": float(s.min()), "max": float(s.max())}

    return {
        "anomaly_id": anomaly.id,
        "sensor_id": sensor.id,
        "context_window": {"start": context_start.isoformat(), "end": context_end.isoformat()},
        "before": _summarize(before),
        "during": _summarize(during),
        "after": _summarize(after),
        "series": [{"timestamp": ts.to_pydatetime().isoformat(), "value": float(v)} for ts, v in series.items()],
    }


def transition_anomaly_status(
    db: Session, *, organization_id: str, anomaly_id: str, user: User, to_status: AnomalyStatus, notes: Optional[str]
) -> Anomaly:
    anomaly = get_anomaly(db, organization_id=organization_id, anomaly_id=anomaly_id)
    allowed = ALLOWED_TRANSITIONS.get(anomaly.status, set())
    if to_status not in allowed:
        raise ValidationFailedError(f"Cannot transition an anomaly from '{anomaly.status.value}' to '{to_status.value}'. Allowed: {sorted(s.value for s in allowed)}")

    investigation = AnomalyInvestigation(
        anomaly_id=anomaly.id, reviewed_by_user_id=user.id, from_status=anomaly.status, to_status=to_status, notes=notes
    )
    db.add(investigation)
    anomaly.status = to_status  # the ONLY field on Anomaly a review action ever changes — see model docstring
    db.commit()
    db.refresh(anomaly)
    return anomaly


def list_anomaly_investigations(db: Session, *, organization_id: str, anomaly_id: str) -> list[AnomalyInvestigation]:
    anomaly = get_anomaly(db, organization_id=organization_id, anomaly_id=anomaly_id)
    return list(anomaly.investigations)


# ---------------------------------------------------------------------------
# Synthetic scenario evaluation (spec section 12/13) — pipeline-correctness
# validation against deterministic, seeded, code-defined ground truth. This
# is NEVER a claim about real-world detection accuracy: no labeled fault
# data exists for this platform (see docs/SYNTHETIC_DATA.md).
# Every result here carries is_synthetic=True at every level, and the
# aggregate stored on ModelVersion.evaluation_results is labeled the same
# way (see docs/ANOMALY_DETECTION.md for how this should and shouldn't be
# read).
# ---------------------------------------------------------------------------

SYNTHETIC_EVAL_START = datetime(2020, 1, 1, tzinfo=timezone.utc)  # fixed, arbitrary — deterministic regardless of when this runs
SYNTHETIC_EVAL_SEED_BASE = 9000  # offset from synthetic_scenarios' own default seeds, so evaluation runs never collide with a demo seed


def _synthetic_robust_zscore_closure(*, threshold: float):
    def run_detector(split: evaluation_mod.ReferenceScoringSplit) -> list[dict[str, Any]]:
        try:
            baseline = baseline_mod.compute_baseline(
                split.reference_series, reference_start=split.reference_start, reference_end=split.reference_end
            )
        except baseline_mod.InsufficientReferenceDataError as exc:
            raise _SyntheticEvalSkip(str(exc)) from exc
        result = robust_zscore.detect(split.scoring_series, baseline, threshold=threshold)
        return [c.to_dict() if hasattr(c, "to_dict") else c for c in result["candidates"]]

    return run_detector


def _synthetic_isolation_forest_closure(*, threshold: float, step_seconds: float, window_seconds: float):
    def run_detector(split: evaluation_mod.ReferenceScoringSplit) -> list[dict[str, Any]]:
        full_series = pd.concat([split.reference_series, split.scoring_series]).sort_index()
        series_by_sensor = {"synthetic": full_series}
        feature_specs = _feature_specs_from_config(_default_feature_specs(["synthetic"], window_seconds=window_seconds))
        try:
            result = isolation_forest_detector.detect(
                series_by_sensor=series_by_sensor,
                feature_specs=feature_specs,
                reference_start=split.reference_start,
                reference_end=split.reference_end,
                scoring_start=split.scoring_start,
                scoring_end=split.scoring_end,
                step_seconds=step_seconds,
                threshold=threshold,
            )
        except isolation_forest_detector.InsufficientFeatureDataError as exc:
            raise _SyntheticEvalSkip(str(exc)) from exc
        return result["candidates"]

    return run_detector


class _SyntheticEvalSkip(Exception):
    """Raised by a synthetic-evaluation detector closure when the scenario's
    reference period is too short for this detector's own configuration —
    an honest "cannot be evaluated" outcome, not a crash and not a silent
    zero-recall result that would misleadingly look like a real miss."""


def _run_mahalanobis_synthetic_evaluation(db: Session, *, model_version: ModelVersion, threshold_config) -> dict[str, Any]:
    """Mirrors run_synthetic_evaluation's structure but against
    app.anomaly.multivariate_synthetic_scenarios — the univariate
    scenarios in app.anomaly.synthetic_scenarios cannot exercise a
    relationship-breakdown anomaly (every one of them is a single
    series), so this detector type gets its own small, honestly-labeled
    scenario set rather than being silently skipped or forced through a
    harness that can't test what it actually does."""
    step_seconds = model_version.detector_config.get("step_seconds", DEFAULT_STEP_SECONDS)
    scenario_results = []
    for i, name in enumerate(multivariate_synthetic_scenarios.ALL_MULTIVARIATE_SCENARIOS):
        scenario = multivariate_synthetic_scenarios.build_multivariate_scenario(name, start=SYNTHETIC_EVAL_START, seed=SYNTHETIC_EVAL_SEED_BASE + i)
        split = multivariate_synthetic_scenarios.split_reference_and_scoring(scenario.series_by_sensor)
        sensor_ids = list(scenario.series_by_sensor.keys())
        try:
            result = mahalanobis_detector.detect(
                series_by_sensor=scenario.series_by_sensor,
                sensor_ids=sensor_ids,
                reference_start=split.reference_start,
                reference_end=split.reference_end,
                scoring_start=split.scoring_start,
                scoring_end=split.scoring_end,
                step_seconds=step_seconds,
                threshold=threshold_config.threshold,
            )
            metrics = evaluation_mod.match_candidates_to_ground_truth(result["candidates"], scenario.ground_truth).to_dict()
            scenario_results.append({
                "scenario": name, "is_synthetic": True, "description": scenario.description,
                "reference_period": {"start": split.reference_start.isoformat(), "end": split.reference_end.isoformat()},
                "scoring_period": {"start": split.scoring_start.isoformat(), "end": split.scoring_end.isoformat()},
                "metrics": metrics, "evaluated_at": datetime.now(timezone.utc).isoformat(),
            })
        except mahalanobis_detector.InsufficientReferenceDataError as exc:
            scenario_results.append({"scenario": name, "is_synthetic": True, "description": scenario.description, "metrics": None, "skipped_reason": str(exc)})

    evaluated = [r for r in scenario_results if r.get("metrics") is not None]
    precisions = [r["metrics"]["precision"] for r in evaluated if r["metrics"]["precision"] is not None]
    recalls = [r["metrics"]["recall"] for r in evaluated if r["metrics"]["recall"] is not None]

    return {
        "is_synthetic": True,
        "detector_type": model_version.detector_type.value,
        "threshold_config": threshold_config.to_dict(),
        "scenario_count": len(scenario_results),
        "scenario_count_evaluated": len(evaluated),
        "scenario_count_skipped": len(scenario_results) - len(evaluated),
        "mean_precision": (sum(precisions) / len(precisions)) if precisions else None,
        "mean_recall": (sum(recalls) / len(recalls)) if recalls else None,
        "scenarios": scenario_results,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "interpretation_note": (
            "These figures measure whether this detector configuration can recover a deterministic, "
            "seeded, code-injected RELATIONSHIP-BREAKDOWN pattern (app.anomaly.multivariate_synthetic_scenarios) "
            "that neither sensor's own univariate statistics would flag. This validates the detection "
            "PIPELINE and the specific multivariate capability this detector exists for, not real-world "
            "accuracy — no labeled real fault data exists for this platform. See docs/ANOMALY_DETECTION.md."
        ),
    }


def run_synthetic_evaluation(db: Session, *, organization_id: str, model_version_id: str) -> dict[str, Any]:
    """Runs every scenario in app.anomaly.synthetic_scenarios.ALL_SCENARIOS
    through a detector configured exactly like `model_version` (same
    detector_type + threshold config — reusing the model's own
    detector_config, e.g. isolation_forest's step_seconds/feature window,
    where applicable), scores each against that scenario's own deterministic
    ground truth (app.anomaly.evaluation), and persists the full breakdown
    plus an aggregate onto ModelVersion.evaluation_results. Never touches
    real stored sensor data — the synthetic series are generated fresh."""
    model_version = get_model_version(db, organization_id=organization_id, model_version_id=model_version_id)
    if model_version.status != ModelVersionStatus.TRAINED:
        raise ConflictError(f"Detector '{model_version_id}' is not in a trained state (status={model_version.status.value}); cannot evaluate.")

    threshold_config = validate_threshold_config(model_version.detector_type, model_version.detector_config.get("threshold", {}))

    if model_version.detector_type == DetectorType.MAHALANOBIS:
        evaluation_results = _run_mahalanobis_synthetic_evaluation(db, model_version=model_version, threshold_config=threshold_config)
        model_version.evaluation_results = evaluation_results
        db.commit()
        db.refresh(model_version)
        return evaluation_results

    if model_version.detector_type == DetectorType.ROBUST_ZSCORE:
        run_detector = _synthetic_robust_zscore_closure(threshold=threshold_config.threshold)
    elif model_version.detector_type == DetectorType.ISOLATION_FOREST:
        run_detector = _synthetic_isolation_forest_closure(
            threshold=threshold_config.threshold,
            step_seconds=model_version.detector_config.get("step_seconds", DEFAULT_STEP_SECONDS),
            window_seconds=DEFAULT_ISOLATION_FEATURE_WINDOW_SECONDS,
        )
    else:
        raise ValidationFailedError(f"Unknown detector_type '{model_version.detector_type}'.")

    scenario_results = []
    for i, name in enumerate(synthetic_scenarios.ALL_SCENARIOS):
        scenario = synthetic_scenarios.build_scenario(name, start=SYNTHETIC_EVAL_START, seed=SYNTHETIC_EVAL_SEED_BASE + i)
        try:
            scenario_results.append(evaluation_mod.evaluate_scenario(scenario, run_detector=run_detector))
        except _SyntheticEvalSkip as exc:
            scenario_results.append(
                {
                    "scenario": name,
                    "is_synthetic": True,
                    "description": scenario.description,
                    "metrics": None,
                    "skipped_reason": str(exc),
                }
            )

    evaluated = [r for r in scenario_results if r.get("metrics") is not None]
    precisions = [r["metrics"]["precision"] for r in evaluated if r["metrics"]["precision"] is not None]
    recalls = [r["metrics"]["recall"] for r in evaluated if r["metrics"]["recall"] is not None]

    evaluation_results = {
        "is_synthetic": True,
        "detector_type": model_version.detector_type.value,
        "threshold_config": threshold_config.to_dict(),
        "scenario_count": len(scenario_results),
        "scenario_count_evaluated": len(evaluated),
        "scenario_count_skipped": len(scenario_results) - len(evaluated),
        "mean_precision": (sum(precisions) / len(precisions)) if precisions else None,
        "mean_recall": (sum(recalls) / len(recalls)) if recalls else None,
        "scenarios": scenario_results,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "interpretation_note": (
            "These figures measure whether this detector configuration can recover deterministic, "
            "seeded, code-injected patterns in synthetic data. They validate the detection PIPELINE, "
            "not real-world accuracy — no labeled real fault data exists for this platform. See "
            "docs/ANOMALY_DETECTION.md."
        ),
    }
    model_version.evaluation_results = evaluation_results
    db.commit()
    db.refresh(model_version)
    return evaluation_results


# ---------------------------------------------------------------------------
# Minimal detector/model monitoring (spec section 18) — real, measurable
# signals only: run outcomes, detector version usage, scoring volume,
# recent failures with their actual error messages. No score-distribution-
# drift detection is implemented (that would need per-observation score
# history, which app.models.intelligence's docstring explains this schema
# deliberately does not persist) — this is stated explicitly in the
# response rather than faked.
# ---------------------------------------------------------------------------

RECENT_FAILURE_LIMIT = 10


def get_monitoring_summary(db: Session, *, organization_id: str) -> dict[str, Any]:
    run_status_counts = dict(
        db.query(ModelRun.status, func.count(ModelRun.id))
        .filter(ModelRun.organization_id == organization_id)
        .group_by(ModelRun.status)
        .all()
    )
    run_status_counts = {status.value: count for status, count in run_status_counts.items()}

    detector_usage = (
        db.query(ModelVersion.id, ModelVersion.name, ModelVersion.version, ModelVersion.detector_type, func.count(ModelRun.id))
        .join(ModelRun, ModelRun.model_version_id == ModelVersion.id)
        .filter(ModelVersion.organization_id == organization_id)
        .group_by(ModelVersion.id)
        .all()
    )
    detector_usage_list = [
        {"model_version_id": mv_id, "name": name, "version": version, "detector_type": detector_type.value, "run_count": count}
        for mv_id, name, version, detector_type, count in detector_usage
    ]

    anomaly_severity_counts = dict(
        db.query(Anomaly.severity_tier, func.count(Anomaly.id))
        .filter(Anomaly.organization_id == organization_id)
        .group_by(Anomaly.severity_tier)
        .all()
    )
    anomaly_severity_counts = {tier.value: count for tier, count in anomaly_severity_counts.items()}

    anomaly_status_counts = dict(
        db.query(Anomaly.status, func.count(Anomaly.id))
        .filter(Anomaly.organization_id == organization_id)
        .group_by(Anomaly.status)
        .all()
    )
    anomaly_status_counts = {status.value: count for status, count in anomaly_status_counts.items()}

    recent_failures = (
        db.query(ModelRun)
        .filter(ModelRun.organization_id == organization_id, ModelRun.status == ModelRunStatus.FAILED)
        .order_by(ModelRun.created_at.desc())
        .limit(RECENT_FAILURE_LIMIT)
        .all()
    )

    total_points_scored = (
        db.query(func.sum(func.json_extract(ModelRun.metrics, "$.points_scored")))
        .filter(ModelRun.organization_id == organization_id, ModelRun.status == ModelRunStatus.SUCCEEDED)
        .scalar()
    )

    detector_count_by_status = dict(
        db.query(ModelVersion.status, func.count(ModelVersion.id))
        .filter(ModelVersion.organization_id == organization_id)
        .group_by(ModelVersion.status)
        .all()
    )
    detector_count_by_status = {status.value: count for status, count in detector_count_by_status.items()}

    return {
        "detector_count_by_status": detector_count_by_status,
        "run_count_by_status": run_status_counts,
        "detector_usage": detector_usage_list,
        "anomaly_count_by_severity_tier": anomaly_severity_counts,
        "anomaly_count_by_status": anomaly_status_counts,
        "total_points_scored_succeeded_runs": int(total_points_scored) if total_points_scored is not None else 0,
        "recent_failed_runs": [
            {
                "run_id": r.id,
                "model_version_id": r.model_version_id,
                "scored_period_start": r.scored_period_start.isoformat() if r.scored_period_start else None,
                "scored_period_end": r.scored_period_end.isoformat() if r.scored_period_end else None,
                "error_message": r.error_message,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            }
            for r in recent_failures
        ],
        "not_implemented": [
            "score distribution drift over time — would require persisting per-observation scores, "
            "which this schema deliberately does not do (see app.models.intelligence's module docstring)"
        ],
    }
