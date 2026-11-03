"""Orchestration layer for Equipment Condition Intelligence & Forecasting
(Phase 4): resolves organization/sensor scope, loads bounded data,
delegates all actual computation to the DB-agnostic app.condition.*
modules, and persists results. Mirrors app.services.anomaly_service's
structure and conventions throughout.

Everything in this module is synchronous — condition/degradation analysis
and forecast fitting both operate on a single sensor's bounded time range
(same MAX_ROWS enforcement as every other timeseries/anomaly read) and are
closed-form/O(n) computations, not iterative training. There is no
analogue to Phase 3's MAX_DETECTION_ROWS_SYNC async split here because
nothing in this module's compute path scales the way a multi-sensor
Isolation Forest fit or a multi-week detection scan can.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.condition import degradation as degradation_mod
from app.condition import evidence as evidence_mod
from app.condition import forecasting as forecasting_mod
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.models.asset import Asset
from app.models.condition import (
    ConditionRun,
    ConditionRunStatus,
    ConditionSignalStatus,
    DegradationReview,
    DegradationSignal,
    DegradationStage,
    ForecastMethod,
    ForecastModel,
    ForecastModelStatus,
    ForecastRun,
    ForecastRunStatus,
)
from app.models.intelligence import Anomaly
from app.models.sensor import Sensor
from app.models.user import User
from app.services.timeseries_service import get_sensor_in_org, validate_time_range
from app.timeseries.data_access import TooManyObservationsError, load_measurement_series, load_multi_sensor_series
from app.timeseries import multi_sensor


def _ensure_utc(dt: datetime) -> datetime:
    """See app.services.anomaly_service._ensure_utc's docstring — the same
    SQLite tzinfo-loss behavior applies here."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


DEFAULT_SEASONAL_PERIOD_SECONDS = 86400.0  # daily seasonality, matching the fleet's actual cadence
MIN_ANALYSIS_OBSERVATIONS = degradation_mod.MIN_OBSERVATIONS_PER_SUB_WINDOW * degradation_mod.SUB_WINDOW_COUNT

CONDITION_ALLOWED_TRANSITIONS: dict[ConditionSignalStatus, set[ConditionSignalStatus]] = {
    ConditionSignalStatus.DETECTED: {ConditionSignalStatus.UNDER_REVIEW, ConditionSignalStatus.ACKNOWLEDGED, ConditionSignalStatus.FALSE_POSITIVE, ConditionSignalStatus.RESOLVED},
    ConditionSignalStatus.UNDER_REVIEW: {ConditionSignalStatus.ACKNOWLEDGED, ConditionSignalStatus.FALSE_POSITIVE, ConditionSignalStatus.RESOLVED, ConditionSignalStatus.DETECTED},
    ConditionSignalStatus.ACKNOWLEDGED: {ConditionSignalStatus.RESOLVED, ConditionSignalStatus.FALSE_POSITIVE, ConditionSignalStatus.UNDER_REVIEW},
    ConditionSignalStatus.RESOLVED: {ConditionSignalStatus.UNDER_REVIEW},
    ConditionSignalStatus.FALSE_POSITIVE: {ConditionSignalStatus.UNDER_REVIEW},
}


# ---------------------------------------------------------------------------
# Condition evidence (§2) — stateless, computed fresh on every call, never
# persisted. Mirrors the read pattern of GET /timeseries/sensors/{id}/profile.
# ---------------------------------------------------------------------------


def get_condition_evidence(
    db: Session, *, organization_id: str, sensor_id: str, window_start: datetime, window_end: datetime,
    reference_start: datetime, reference_end: datetime,
) -> dict[str, Any]:
    validate_time_range(window_start, window_end)
    validate_time_range(reference_start, reference_end)
    sensor = get_sensor_in_org(db, sensor_id=sensor_id, organization_id=organization_id)

    try:
        window_series = load_measurement_series(db, sensor_id=sensor.id, start=window_start, end=window_end)
        reference_series = load_measurement_series(db, sensor_id=sensor.id, start=reference_start, end=reference_end)
    except TooManyObservationsError as exc:
        raise ValidationFailedError(str(exc))

    sibling_ids = [s.id for s in db.query(Sensor).filter(Sensor.asset_id == sensor.asset_id, Sensor.id != sensor.id).all()]
    aligned = None
    if sibling_ids:
        try:
            series_by_sensor = load_multi_sensor_series(db, sensor_ids=[sensor.id, *sibling_ids], start=window_start, end=window_end)
            span_seconds = max((window_end - window_start).total_seconds() / 200.0, 60.0)
            aligned = multi_sensor.align_sensors_on_grid(series_by_sensor, start=window_start, end=window_end, freq_seconds=span_seconds)
        except TooManyObservationsError:
            aligned = None

    anomalies = _recent_anomalies_as_dicts(db, organization_id=organization_id, sensor_id=sensor.id, start=window_start, end=window_end)

    return evidence_mod.compute_condition_evidence(
        sensor_id=sensor.id, sensor_unit=sensor.unit, expected_interval_seconds=sensor.expected_interval_seconds,
        window_series=window_series, window_start=window_start, window_end=window_end,
        reference_series=reference_series, reference_start=reference_start, reference_end=reference_end,
        anomalies=anomalies, aligned_sibling_series=aligned,
    )


def _recent_anomalies_as_dicts(db: Session, *, organization_id: str, sensor_id: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    rows = (
        db.query(Anomaly)
        .filter(Anomaly.organization_id == organization_id, Anomaly.sensor_id == sensor_id, Anomaly.period_end >= start, Anomaly.period_start <= end)
        .order_by(Anomaly.period_start.asc())
        .all()
    )
    return [
        {"severity_tier": a.severity_tier.value, "status": a.status.value, "period_start": _ensure_utc(a.period_start)}
        for a in rows
    ]


# ---------------------------------------------------------------------------
# Degradation & long-term change analysis (§3) — persisted, reviewable.
# ---------------------------------------------------------------------------


def create_condition_run(
    db: Session, *, organization_id: str, user_id: Optional[str], sensor_id: str,
    analysis_start: datetime, analysis_end: datetime, reference_start: datetime, reference_end: datetime,
) -> ConditionRun:
    validate_time_range(analysis_start, analysis_end)
    validate_time_range(reference_start, reference_end)
    if _ensure_utc(reference_end) > _ensure_utc(analysis_start):
        raise ValidationFailedError(
            "The reference period must end at or before the analysis period starts — a baseline built "
            "partly from the period being analyzed would not be an independent comparison."
        )
    sensor = get_sensor_in_org(db, sensor_id=sensor_id, organization_id=organization_id)

    run = ConditionRun(
        organization_id=organization_id, asset_id=sensor.asset_id, sensor_id=sensor.id,
        analysis_period_start=analysis_start, analysis_period_end=analysis_end,
        reference_period_start=reference_start, reference_period_end=reference_end,
        config={"sub_window_count": degradation_mod.SUB_WINDOW_COUNT, "majority_sub_windows": degradation_mod.MAJORITY_SUB_WINDOWS, "corroboration_mad_threshold": degradation_mod.CORROBORATION_MAD_THRESHOLD},
        status=ConditionRunStatus.SUCCEEDED,
        created_by_user_id=user_id,
        started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    # Committed (not just flushed) before the try block below, so that if an
    # exception occurs mid-analysis, `db.rollback()` in the except clause
    # only undoes the analysis work-in-progress, never this row itself —
    # otherwise the subsequent db.get(ConditionRun, run.id) would return
    # None and mask the real error. Mirrors
    # app.services.anomaly_service.create_detection_run's commit-before-
    # execute pattern.
    db.commit()
    db.refresh(run)

    try:
        try:
            window_series = load_measurement_series(db, sensor_id=sensor.id, start=analysis_start, end=analysis_end)
            reference_series = load_measurement_series(db, sensor_id=sensor.id, start=reference_start, end=reference_end)
        except TooManyObservationsError as exc:
            raise ValidationFailedError(str(exc))

        if len(window_series) < MIN_ANALYSIS_OBSERVATIONS:
            run.status = ConditionRunStatus.SUCCEEDED
            run.metrics = {"signal_produced": False, "reason": f"insufficient analysis-window observations (n={len(window_series)} < {MIN_ANALYSIS_OBSERVATIONS})"}
            run.finished_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(run)
            return run

        anomalies = _recent_anomalies_as_dicts(db, organization_id=organization_id, sensor_id=sensor.id, start=analysis_start, end=analysis_end)
        deviation_evidence = evidence_mod.compute_deviation_from_baseline(window_series, reference_series=reference_series, reference_start=reference_start, reference_end=reference_end)
        anomaly_evidence = evidence_mod.summarize_anomaly_persistence(anomalies, window_start=analysis_start, window_end=analysis_end)
        sub_window_trends = degradation_mod.compute_sub_window_trends(window_series, window_start=analysis_start, window_end=analysis_end)
        classification = degradation_mod.classify_degradation(sub_window_trends=sub_window_trends, deviation_evidence=deviation_evidence, anomaly_evidence=anomaly_evidence)

        run.status = ConditionRunStatus.SUCCEEDED
        run.finished_at = datetime.now(timezone.utc)

        if classification["stage"] is None:
            run.metrics = {"signal_produced": False, "reason": classification["rationale"]}
            db.commit()
            db.refresh(run)
            return run

        variance_evidence = evidence_mod.compute_variance_change_evidence(window_series)
        full_evidence = {
            "classification": classification,
            "deviation_from_baseline": deviation_evidence,
            "persistent_anomaly_evidence": anomaly_evidence,
            "variance_change_evidence": variance_evidence,
            "observation_count": len(window_series),
        }
        stage_enum = DegradationStage(classification["stage"])
        signal = DegradationSignal(
            organization_id=organization_id, asset_id=sensor.asset_id, sensor_id=sensor.id, condition_run_id=run.id,
            stage=stage_enum, status=ConditionSignalStatus.DETECTED,
            period_start=analysis_start, period_end=analysis_end,
            summary=classification["rationale"][:500],
            evidence=full_evidence,
        )
        db.add(signal)
        run.metrics = {"signal_produced": True, "stage": classification["stage"]}
        db.commit()
        db.refresh(run)
        return run

    except Exception as exc:  # noqa: BLE001
        db.rollback()
        run = db.get(ConditionRun, run.id)
        run.status = ConditionRunStatus.FAILED
        run.error_message = str(exc)[:1000]
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        raise


def get_condition_run(db: Session, *, organization_id: str, run_id: str) -> ConditionRun:
    run = db.query(ConditionRun).filter(ConditionRun.id == run_id, ConditionRun.organization_id == organization_id).first()
    if run is None:
        raise NotFoundError(f"Condition run '{run_id}' not found.")
    return run


def list_condition_runs(db: Session, *, organization_id: str, sensor_id: Optional[str], limit: int, offset: int):
    query = db.query(ConditionRun).filter(ConditionRun.organization_id == organization_id)
    if sensor_id:
        query = query.filter(ConditionRun.sensor_id == sensor_id)
    query = query.order_by(ConditionRun.created_at.desc())
    total = query.count()
    return query.offset(offset).limit(limit).all(), total


def list_degradation_signals(
    db: Session, *, organization_id: str, asset_id: Optional[str], sensor_id: Optional[str],
    stage: Optional[DegradationStage], status: Optional[ConditionSignalStatus], limit: int, offset: int,
):
    query = db.query(DegradationSignal).filter(DegradationSignal.organization_id == organization_id)
    if asset_id:
        query = query.filter(DegradationSignal.asset_id == asset_id)
    if sensor_id:
        query = query.filter(DegradationSignal.sensor_id == sensor_id)
    if stage:
        query = query.filter(DegradationSignal.stage == stage)
    if status:
        query = query.filter(DegradationSignal.status == status)
    query = query.order_by(DegradationSignal.period_start.desc())
    total = query.count()
    return query.offset(offset).limit(limit).all(), total


def get_degradation_signal(db: Session, *, organization_id: str, signal_id: str) -> DegradationSignal:
    signal = db.query(DegradationSignal).filter(DegradationSignal.id == signal_id, DegradationSignal.organization_id == organization_id).first()
    if signal is None:
        raise NotFoundError(f"Degradation signal '{signal_id}' not found.")
    return signal


def transition_degradation_status(
    db: Session, *, organization_id: str, signal_id: str, user: User, to_status: ConditionSignalStatus, notes: Optional[str]
) -> DegradationSignal:
    signal = get_degradation_signal(db, organization_id=organization_id, signal_id=signal_id)
    allowed = CONDITION_ALLOWED_TRANSITIONS.get(signal.status, set())
    if to_status not in allowed:
        raise ValidationFailedError(f"Cannot transition a degradation signal from '{signal.status.value}' to '{to_status.value}'. Allowed: {sorted(s.value for s in allowed)}")

    review = DegradationReview(
        degradation_signal_id=signal.id, reviewed_by_user_id=user.id,
        from_status=signal.status, to_status=to_status, from_stage=signal.stage, to_stage=signal.stage, notes=notes,
    )
    db.add(review)
    signal.status = to_status
    db.commit()
    db.refresh(signal)
    return signal


def validate_degradation_signal(
    db: Session, *, organization_id: str, signal_id: str, user: User, notes: str
) -> DegradationSignal:
    """The ONLY path by which stage can reach VALIDATED_DEGRADATION — see
    app.condition.degradation's module docstring for why this can never be
    automatic. `notes` (the engineering justification) is mandatory."""
    if not notes or not notes.strip():
        raise ValidationFailedError("Validating a degradation signal requires a non-empty justification in `notes` — this platform has no maintenance-outcome data to confirm it automatically.")
    signal = get_degradation_signal(db, organization_id=organization_id, signal_id=signal_id)
    if signal.stage != DegradationStage.DEGRADATION_CANDIDATE:
        raise ConflictError(f"Only a signal in stage 'degradation_candidate' can be validated (current stage: '{signal.stage.value}').")

    review = DegradationReview(
        degradation_signal_id=signal.id, reviewed_by_user_id=user.id,
        from_status=signal.status, to_status=signal.status,
        from_stage=signal.stage, to_stage=DegradationStage.VALIDATED_DEGRADATION, notes=notes,
    )
    db.add(review)
    signal.stage = DegradationStage.VALIDATED_DEGRADATION
    db.commit()
    db.refresh(signal)
    return signal


def list_degradation_reviews(db: Session, *, organization_id: str, signal_id: str) -> list[DegradationReview]:
    signal = get_degradation_signal(db, organization_id=organization_id, signal_id=signal_id)
    return list(signal.reviews)


# ---------------------------------------------------------------------------
# Forecasting (§4) — honesty-gated: fitting ALWAYS backtests against naive
# baselines; a forecast run is only permitted when the backtest showed the
# candidate method actually earns its keep.
# ---------------------------------------------------------------------------


def create_forecast_model(
    db: Session, *, organization_id: str, user_id: Optional[str], sensor_id: str, name: str,
    fit_start: datetime, fit_end: datetime, test_end: datetime, seasonal_period_seconds: Optional[float],
) -> ForecastModel:
    validate_time_range(fit_start, fit_end)
    validate_time_range(fit_end, test_end)
    sensor = get_sensor_in_org(db, sensor_id=sensor_id, organization_id=organization_id)
    seasonal_period = seasonal_period_seconds or DEFAULT_SEASONAL_PERIOD_SECONDS

    try:
        full_series = load_measurement_series(db, sensor_id=sensor.id, start=fit_start, end=test_end)
    except TooManyObservationsError as exc:
        raise ValidationFailedError(str(exc))

    backtest = forecasting_mod.backtest_and_compare(
        full_series, fit_start=fit_start, fit_end=fit_end, test_end=test_end, seasonal_period_seconds=seasonal_period,
    )

    next_version = db.query(ForecastModel).filter(ForecastModel.organization_id == organization_id, ForecastModel.name == name).count() + 1

    if not backtest["computed"]:
        model = ForecastModel(
            organization_id=organization_id, sensor_id=sensor.id, name=name, version=next_version,
            method=ForecastMethod.LINEAR_TREND,
            config={"fit_start": fit_start.isoformat(), "fit_end": fit_end.isoformat(), "test_end": test_end.isoformat(), "seasonal_period_seconds": seasonal_period},
            fit_period_start=fit_start, fit_period_end=fit_end,
            fit_observation_count=backtest.get("fit_observation_count", 0),
            status=ForecastModelStatus.FAILED,
            training_error=backtest["reason"],
            created_by_user_id=user_id,
        )
        db.add(model)
        db.commit()
        db.refresh(model)
        return model

    status = ForecastModelStatus.JUSTIFIED if backtest["is_justified"] else ForecastModelStatus.NOT_JUSTIFIED
    model = ForecastModel(
        organization_id=organization_id, sensor_id=sensor.id, name=name, version=next_version,
        method=ForecastMethod.LINEAR_TREND,
        config={"fit_start": fit_start.isoformat(), "fit_end": fit_end.isoformat(), "test_end": test_end.isoformat(), "seasonal_period_seconds": seasonal_period},
        fit_period_start=fit_start, fit_period_end=fit_end,
        fit_observation_count=backtest["fit_observation_count"],
        status=status,
        fitted_at=datetime.now(timezone.utc),
        evaluation_results=backtest,
        created_by_user_id=user_id,
    )
    db.add(model)
    db.commit()
    db.refresh(model)
    return model


def get_forecast_model(db: Session, *, organization_id: str, model_id: str) -> ForecastModel:
    model = db.query(ForecastModel).filter(ForecastModel.id == model_id, ForecastModel.organization_id == organization_id).first()
    if model is None:
        raise NotFoundError(f"Forecast model '{model_id}' not found.")
    return model


def list_forecast_models(db: Session, *, organization_id: str, sensor_id: Optional[str], limit: int, offset: int):
    query = db.query(ForecastModel).filter(ForecastModel.organization_id == organization_id)
    if sensor_id:
        query = query.filter(ForecastModel.sensor_id == sensor_id)
    query = query.order_by(ForecastModel.created_at.desc())
    total = query.count()
    return query.offset(offset).limit(limit).all(), total


def create_forecast_run(
    db: Session, *, organization_id: str, user_id: Optional[str], forecast_model_id: str,
    as_of: datetime, horizon_seconds: float, step_seconds: float,
) -> ForecastRun:
    model = get_forecast_model(db, organization_id=organization_id, model_id=forecast_model_id)
    if model.status != ForecastModelStatus.JUSTIFIED:
        raise ConflictError(
            f"Forecast model '{forecast_model_id}' is not justified (status={model.status.value}) — "
            "its backtest did not show it beating naive baselines, so no forecast run is permitted. "
            "See evaluation_results.justification_note for why."
        )
    if step_seconds <= 0 or horizon_seconds <= 0:
        raise ValidationFailedError("horizon_seconds and step_seconds must both be positive.")

    run = ForecastRun(
        organization_id=organization_id, forecast_model_id=model.id, status=ForecastRunStatus.SUCCEEDED,
        as_of=as_of, horizon_seconds=horizon_seconds, step_seconds=step_seconds,
        created_by_user_id=user_id, started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    # Committed (not just flushed) before point generation — see
    # create_condition_run's identical comment for why this matters on the
    # exception path below.
    db.commit()
    db.refresh(run)

    try:
        try:
            full_series = load_measurement_series(db, sensor_id=model.sensor_id, start=_ensure_utc(model.fit_period_start), end=as_of)
        except TooManyObservationsError as exc:
            raise ValidationFailedError(str(exc))
        points = forecasting_mod.generate_forecast_points(full_series, as_of=as_of, horizon_seconds=horizon_seconds, step_seconds=step_seconds)
        run.points = points
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(run)
        return run
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        run = db.get(ForecastRun, run.id)
        run.status = ForecastRunStatus.FAILED
        run.error_message = str(exc)[:1000]
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        raise


def get_forecast_run(db: Session, *, organization_id: str, run_id: str) -> ForecastRun:
    run = db.query(ForecastRun).filter(ForecastRun.id == run_id, ForecastRun.organization_id == organization_id).first()
    if run is None:
        raise NotFoundError(f"Forecast run '{run_id}' not found.")
    return run


def list_forecast_runs(db: Session, *, organization_id: str, forecast_model_id: Optional[str], limit: int, offset: int):
    query = db.query(ForecastRun).filter(ForecastRun.organization_id == organization_id)
    if forecast_model_id:
        query = query.filter(ForecastRun.forecast_model_id == forecast_model_id)
    query = query.order_by(ForecastRun.created_at.desc())
    total = query.count()
    return query.offset(offset).limit(limit).all(), total


# ---------------------------------------------------------------------------
# Risk prioritization (§11) — a transparent, non-probabilistic attention
# list. The sort key is fully exposed in every item, not hidden inside a
# single fabricated "risk score."
# ---------------------------------------------------------------------------

_STAGE_RANK = {DegradationStage.VALIDATED_DEGRADATION: 3, DegradationStage.DEGRADATION_CANDIDATE: 2, DegradationStage.OBSERVED_TREND: 1}
_SEVERITY_RANK = {"high_priority_anomaly": 3, "candidate_anomaly": 2, "observed_unusual_behavior": 1}


def get_attention_list(db: Session, *, organization_id: str, limit: int) -> dict[str, Any]:
    """Combines OPEN degradation signals and OPEN real anomalies into one
    list, sorted by an explicit, documented rank tuple — never a single
    invented probability. Every item carries the rank components that
    produced its position, so the ordering is auditable, not asserted."""
    open_statuses = [ConditionSignalStatus.DETECTED, ConditionSignalStatus.UNDER_REVIEW, ConditionSignalStatus.ACKNOWLEDGED]
    signals = (
        db.query(DegradationSignal)
        .filter(DegradationSignal.organization_id == organization_id, DegradationSignal.status.in_(open_statuses))
        .order_by(DegradationSignal.period_start.desc())
        .limit(500)
        .all()
    )
    from app.models.intelligence import AnomalyStatus
    anomaly_open_statuses = [AnomalyStatus.DETECTED, AnomalyStatus.UNDER_REVIEW, AnomalyStatus.ACKNOWLEDGED]
    anomalies = (
        db.query(Anomaly)
        .filter(Anomaly.organization_id == organization_id, Anomaly.status.in_(anomaly_open_statuses))
        .order_by(Anomaly.period_start.desc())
        .limit(500)
        .all()
    )

    items: list[dict[str, Any]] = []
    for s in signals:
        items.append({
            "item_type": "degradation_signal", "id": s.id, "asset_id": s.asset_id, "sensor_id": s.sensor_id,
            "summary": s.summary, "period_start": s.period_start.isoformat(),
            "rank": {"stage_rank": _STAGE_RANK.get(s.stage, 0), "severity_rank": 0},
        })
    for a in anomalies:
        items.append({
            "item_type": "anomaly", "id": a.id, "asset_id": a.asset_id, "sensor_id": a.sensor_id,
            "summary": a.summary, "period_start": a.period_start.isoformat(),
            "rank": {"stage_rank": 0, "severity_rank": _SEVERITY_RANK.get(a.severity_tier.value, 0)},
        })

    items.sort(key=lambda it: (it["rank"]["stage_rank"], it["rank"]["severity_rank"], it["period_start"]), reverse=True)
    return {
        "items": items[:limit],
        "total_open_degradation_signals": len(signals),
        "total_open_anomalies": len(anomalies),
        "sort_methodology": (
            "Sorted descending by (stage_rank, severity_rank, period_start) — a validated_degradation "
            "signal ranks above a degradation_candidate, which ranks above any anomaly severity tier; "
            "within anomalies, high_priority ranks above candidate ranks above observed_unusual. This is "
            "a transparent priority ORDERING, not a probability or a risk score — no likelihood-of-"
            "failure number is computed anywhere in this platform."
        ),
    }


# ---------------------------------------------------------------------------
# Monitoring (§16) — real, measurable signals only, mirroring
# app.services.anomaly_service.get_monitoring_summary.
# ---------------------------------------------------------------------------


def get_condition_monitoring_summary(db: Session, *, organization_id: str) -> dict[str, Any]:
    signal_stage_counts = dict(
        db.query(DegradationSignal.stage, func.count(DegradationSignal.id))
        .filter(DegradationSignal.organization_id == organization_id)
        .group_by(DegradationSignal.stage).all()
    )
    signal_stage_counts = {k.value: v for k, v in signal_stage_counts.items()}

    signal_status_counts = dict(
        db.query(DegradationSignal.status, func.count(DegradationSignal.id))
        .filter(DegradationSignal.organization_id == organization_id)
        .group_by(DegradationSignal.status).all()
    )
    signal_status_counts = {k.value: v for k, v in signal_status_counts.items()}

    condition_run_status_counts = dict(
        db.query(ConditionRun.status, func.count(ConditionRun.id))
        .filter(ConditionRun.organization_id == organization_id)
        .group_by(ConditionRun.status).all()
    )
    condition_run_status_counts = {k.value: v for k, v in condition_run_status_counts.items()}

    forecast_model_status_counts = dict(
        db.query(ForecastModel.status, func.count(ForecastModel.id))
        .filter(ForecastModel.organization_id == organization_id)
        .group_by(ForecastModel.status).all()
    )
    forecast_model_status_counts = {k.value: v for k, v in forecast_model_status_counts.items()}

    return {
        "degradation_signal_count_by_stage": signal_stage_counts,
        "degradation_signal_count_by_status": signal_status_counts,
        "condition_run_count_by_status": condition_run_status_counts,
        "forecast_model_count_by_status": forecast_model_status_counts,
        "not_implemented": [
            "supervised failure prediction / remaining-useful-life estimation — no verified failure "
            "labels or maintenance history exist for this platform (see docs/CONDITION_INTELLIGENCE.md §0)",
        ],
    }
