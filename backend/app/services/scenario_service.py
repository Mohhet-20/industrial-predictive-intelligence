"""Scenario simulation what-if orchestration (
). Resolves organization/sensor scope, loads bounded REAL
data, delegates every perturbation/pipeline/comparison computation to the
DB-agnostic app.simulation.* modules, and persists results. Mirrors
app.services.condition_service and app.services.anomaly_service's
structure and conventions throughout — including the sync/async split
below, copied from app.services.anomaly_service.create_detection_run.

SECURITY NON-DESTRUCTIVE GUARANTEE: nothing in this module ever
writes to app.models.measurement.SensorMeasurement. Every read of real data
goes through app.timeseries.data_access (bounded, organization-scoped,
read-only); every perturbation is applied to an in-memory copy
(app.simulation.perturbations never mutates its input); and
_execute_scenario_run re-verifies the non-destructive guarantee
(app.simulation.perturbations.verify_outside_window_unchanged) on the
simulated series before any result is persisted, as a defense-in-depth
check beyond app.simulation.validation's pre-flight rejection.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.anomaly import baseline as baseline_mod
from app.anomaly.robust_zscore import DEFAULT_THRESHOLD as DEFAULT_ANOMALY_THRESHOLD
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.models.asset import Asset
from app.models.intelligence import Anomaly, DetectorType, ModelVersion, ModelVersionStatus
from app.models.job import JobType
from app.models.scenario import Scenario, ScenarioRun, ScenarioRunKind, ScenarioRunStatus, ScenarioStatus, ScenarioType
from app.models.sensor import Sensor
from app.services.timeseries_service import get_sensor_in_org, validate_time_range
from app.simulation import comparison as comparison_mod
from app.simulation import perturbations as perturbations_mod
from app.simulation import pipeline as pipeline_mod
from app.simulation import validation as validation_mod
from app.timeseries.data_access import TooManyObservationsError, load_measurement_series

DEFAULT_EXPECTED_INTERVAL_SECONDS = 300.0
DEFAULT_SEASONAL_PERIOD_SECONDS = 86400.0
MAX_SCENARIO_ROWS_SYNC = 3000  # mirrors app.services.anomaly_service.MAX_DETECTION_ROWS_SYNC
MAX_SENSITIVITY_SWEEP_POINTS = 8  # bounded compute — each point re-runs the full pipeline


def _ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """See app.services.anomaly_service._ensure_utc's docstring — SQLite
    loses tzinfo on round-trip; every other module in this codebase that
    reads a DateTime column back from the DB carries this same guard."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _get_asset_in_org(db: Session, *, asset_id: str, organization_id: str) -> Asset:
    asset = db.query(Asset).filter(Asset.id == asset_id, Asset.organization_id == organization_id).first()
    if asset is None:
        raise NotFoundError(f"Asset '{asset_id}' not found.")
    return asset


def _recent_anomalies_as_dicts(db: Session, *, organization_id: str, sensor_id: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    rows = (
        db.query(Anomaly)
        .filter(Anomaly.organization_id == organization_id, Anomaly.sensor_id == sensor_id, Anomaly.period_end >= start, Anomaly.period_start <= end, Anomaly.is_synthetic_scenario.is_(False))
        .order_by(Anomaly.period_start.asc())
        .all()
    )
    return [{"severity_tier": a.severity_tier.value, "status": a.status.value, "period_start": _ensure_utc(a.period_start)} for a in rows]


# ---------------------------------------------------------------------------
# Scenario CRUD
# ---------------------------------------------------------------------------


def create_scenario(
    db: Session, *, organization_id: str, user_id: Optional[str], asset_id: str, sensor_id: str,
    sensor_scope: Optional[list[str]], name: str, description: Optional[str], scenario_type: ScenarioType,
    baseline_window_start: datetime, baseline_window_end: datetime,
    reference_window_start: Optional[datetime], reference_window_end: Optional[datetime],
    modification_start: Optional[datetime], modification_duration_seconds: Optional[float],
    magnitude: Optional[float], unit: Optional[str], parameters: Optional[dict[str, Any]],
) -> Scenario:
    validate_time_range(baseline_window_start, baseline_window_end)
    if reference_window_start and reference_window_end:
        validate_time_range(reference_window_start, reference_window_end)
        if _ensure_utc(reference_window_end) > _ensure_utc(baseline_window_start):
            raise ValidationFailedError(
                "reference_window_end must be at or before baseline_window_start — an independent baseline "
                "cannot overlap the window being simulated (same rule app.services.condition_service enforces)."
            )

    asset = _get_asset_in_org(db, asset_id=asset_id, organization_id=organization_id)
    sensor = get_sensor_in_org(db, sensor_id=sensor_id, organization_id=organization_id)
    if sensor.asset_id != asset.id:
        raise ValidationFailedError(f"Sensor '{sensor_id}' does not belong to asset '{asset_id}'.")

    scope = list(dict.fromkeys([sensor_id, *(sensor_scope or [])]))  # de-dupe, sensor_id always first/included
    if scenario_type == ScenarioType.MULTI_SENSOR:
        for sid in scope:
            other = get_sensor_in_org(db, sensor_id=sid, organization_id=organization_id)
            if other.asset_id != asset.id:
                raise ValidationFailedError(f"MULTI_SENSOR scenario sensor '{sid}' does not belong to asset '{asset_id}' — every sensor in a scenario's scope must be on the same asset.")
    elif len(scope) > 1:
        raise ValidationFailedError("sensor_scope may only contain more than one sensor for scenario_type='multi_sensor'.")

    scenario = Scenario(
        organization_id=organization_id, asset_id=asset.id, sensor_id=sensor.id, sensor_scope=scope,
        name=name, description=description, scenario_type=scenario_type,
        baseline_window_start=baseline_window_start, baseline_window_end=baseline_window_end,
        reference_window_start=reference_window_start, reference_window_end=reference_window_end,
        modification_start=modification_start, modification_duration_seconds=modification_duration_seconds,
        magnitude=magnitude, unit=unit, parameters=parameters or {},
        status=ScenarioStatus.DRAFT, version=1, created_by_user_id=user_id,
    )
    db.add(scenario)
    db.commit()
    db.refresh(scenario)
    return scenario


def get_scenario(db: Session, *, organization_id: str, scenario_id: str) -> Scenario:
    scenario = db.query(Scenario).filter(Scenario.id == scenario_id, Scenario.organization_id == organization_id).first()
    if scenario is None:
        raise NotFoundError(f"Scenario '{scenario_id}' not found.")
    return scenario


def list_scenarios(
    db: Session, *, organization_id: str, asset_id: Optional[str], sensor_id: Optional[str],
    status: Optional[ScenarioStatus], limit: int, offset: int,
) -> tuple[list[Scenario], int]:
    query = db.query(Scenario).filter(Scenario.organization_id == organization_id)
    if asset_id:
        query = query.filter(Scenario.asset_id == asset_id)
    if sensor_id:
        query = query.filter(Scenario.sensor_id == sensor_id)
    if status:
        query = query.filter(Scenario.status == status)
    query = query.order_by(Scenario.created_at.desc())
    total = query.count()
    return query.offset(offset).limit(limit).all(), total


def archive_scenario(db: Session, *, organization_id: str, scenario_id: str) -> Scenario:
    """: 'delete' archives, it never hard-deletes — every prior
    ScenarioRun and its results stay fully inspectable."""
    scenario = get_scenario(db, organization_id=organization_id, scenario_id=scenario_id)
    scenario.status = ScenarioStatus.ARCHIVED
    db.commit()
    db.refresh(scenario)
    return scenario


# ---------------------------------------------------------------------------
# Validation — must pass before a scenario can be run
# ---------------------------------------------------------------------------


def _sensor_context(db: Session, *, sensor: Sensor, baseline_window_start: datetime, baseline_window_end: datetime) -> validation_mod.SensorContext:
    try:
        series = load_measurement_series(db, sensor_id=sensor.id, start=baseline_window_start, end=baseline_window_end)
    except TooManyObservationsError as exc:
        raise ValidationFailedError(str(exc))
    n = len(series)
    median = robust_std = bmin = bmax = None
    if n >= 2:
        median = float(series.median())
        mad = float((series - median).abs().median())
        robust_std = mad * baseline_mod.MAD_TO_STD_FACTOR
        bmin, bmax = float(series.min()), float(series.max())
    return validation_mod.SensorContext(
        sensor_id=sensor.id, unit=sensor.unit, expected_interval_seconds=sensor.expected_interval_seconds,
        baseline_observation_count=n, baseline_median=median, baseline_robust_std=robust_std, baseline_min=bmin, baseline_max=bmax,
    )


def validate_scenario(db: Session, *, organization_id: str, scenario_id: str) -> Scenario:
    scenario = get_scenario(db, organization_id=organization_id, scenario_id=scenario_id)
    if scenario.status == ScenarioStatus.ARCHIVED:
        raise ConflictError(f"Scenario '{scenario_id}' is archived and cannot be validated or run again.")

    baseline_start, baseline_end = _ensure_utc(scenario.baseline_window_start), _ensure_utc(scenario.baseline_window_end)

    if scenario.scenario_type == ScenarioType.MULTI_SENSOR:
        sub_configs = (scenario.parameters or {}).get("per_sensor", [])
        sensors_by_id = {}
        for sid in scenario.sensor_scope:
            sensor = get_sensor_in_org(db, sensor_id=sid, organization_id=organization_id)
            sensors_by_id[sid] = _sensor_context(db, sensor=sensor, baseline_window_start=baseline_start, baseline_window_end=baseline_end)
        result = validation_mod.validate_multi_sensor_scenario(
            sub_configs=sub_configs, sensor_scope=scenario.sensor_scope, sensors_by_id=sensors_by_id,
            baseline_window_start=baseline_start, baseline_window_end=baseline_end,
        )
    else:
        sensor = get_sensor_in_org(db, sensor_id=scenario.sensor_id, organization_id=organization_id)
        ctx = _sensor_context(db, sensor=sensor, baseline_window_start=baseline_start, baseline_window_end=baseline_end)
        result = validation_mod.validate_single_sensor_scenario(
            scenario_type=scenario.scenario_type.value, sensor=ctx, baseline_window_start=baseline_start, baseline_window_end=baseline_end,
            modification_start=_ensure_utc(scenario.modification_start), modification_duration_seconds=scenario.modification_duration_seconds,
            magnitude=scenario.magnitude, unit=scenario.unit, parameters=scenario.parameters,
        )

    result.checked_at = datetime.now(timezone.utc).isoformat()
    scenario.validation_result = result.to_dict()
    scenario.status = ScenarioStatus.VALIDATED if result.ok else ScenarioStatus.DRAFT
    db.commit()
    db.refresh(scenario)
    return scenario


# ---------------------------------------------------------------------------
# Model compatibility — never force an incompatible model; record why
# ---------------------------------------------------------------------------


def _resolve_model_compatibility(db: Session, *, organization_id: str, sensor_id: str, model_version_id: Optional[str]) -> tuple[Optional[ModelVersion], list[str]]:
    limitations: list[str] = []
    if not model_version_id:
        return None, limitations
    model_version = db.query(ModelVersion).filter(ModelVersion.id == model_version_id, ModelVersion.organization_id == organization_id).first()
    if model_version is None:
        limitations.append(f"parameters.model_version_id '{model_version_id}' was not found in this organization — using an ad-hoc baseline computed from the reference window instead.")
        return None, limitations
    if model_version.detector_type != DetectorType.ROBUST_ZSCORE:
        limitations.append(
            f"Referenced model '{model_version_id}' is a {model_version.detector_type.value} detector — scenario "
            "simulation only reuses robust_zscore models against simulated data (Isolation Forest is a documented "
            "scope boundary; see app.simulation.pipeline). Using an ad-hoc baseline instead."
        )
        return None, limitations
    if model_version.status != ModelVersionStatus.TRAINED:
        limitations.append(f"Referenced model '{model_version_id}' is not in a trained state (status={model_version.status.value}) — using an ad-hoc baseline instead.")
        return None, limitations
    if sensor_id not in (model_version.detector_config.get("sensor_ids") or []):
        limitations.append(f"Referenced model '{model_version_id}' was not trained on sensor '{sensor_id}' — using an ad-hoc baseline instead.")
        return None, limitations
    return model_version, limitations


def _auto_reference_window(baseline_start: datetime, baseline_end: datetime) -> tuple[datetime, datetime]:
    span = baseline_end - baseline_start
    return baseline_start - span, baseline_start


# ---------------------------------------------------------------------------
# Pipeline execution for ONE sensor — shared by
# single-sensor scenarios and each sensor in a MULTI_SENSOR scenario.
# ---------------------------------------------------------------------------


def _run_one_sensor_scenario(
    db: Session, *, organization_id: str, sensor: Sensor, baseline_start: datetime, baseline_end: datetime,
    reference_start_cfg: Optional[datetime], reference_end_cfg: Optional[datetime],
    scenario_type: str, modification_start: datetime, modification_duration_seconds: Optional[float],
    magnitude: float, parameters: Optional[dict[str, Any]], model_version_id: Optional[str], include_forecast: bool,
) -> dict[str, Any]:
    try:
        baseline_series = load_measurement_series(db, sensor_id=sensor.id, start=baseline_start, end=baseline_end)
    except TooManyObservationsError as exc:
        raise ValidationFailedError(str(exc))
    if len(baseline_series) < validation_mod.MIN_BASELINE_OBSERVATIONS:
        raise ValidationFailedError(
            f"Baseline window now has only {len(baseline_series)} real observation(s) for sensor '{sensor.id}' "
            f"(minimum {validation_mod.MIN_BASELINE_OBSERVATIONS}) — has data changed since this scenario was validated?"
        )

    limitations = list(comparison_mod.MANDATORY_LIMITATIONS)
    model_version, compat_limitations = _resolve_model_compatibility(db, organization_id=organization_id, sensor_id=sensor.id, model_version_id=model_version_id)
    limitations += compat_limitations

    if model_version is not None:
        reference_start = _ensure_utc(model_version.reference_period_start)
        reference_end = _ensure_utc(model_version.reference_period_end)
        anomaly_threshold = (model_version.detector_config.get("threshold") or {}).get("threshold", DEFAULT_ANOMALY_THRESHOLD)
    else:
        if reference_start_cfg and reference_end_cfg:
            reference_start, reference_end = _ensure_utc(reference_start_cfg), _ensure_utc(reference_end_cfg)
        else:
            reference_start, reference_end = _auto_reference_window(baseline_start, baseline_end)
            limitations.append(
                f"No reference_window was configured — an equal-length period immediately before the baseline "
                f"window ({reference_start.isoformat()} to {reference_end.isoformat()}) was used automatically."
            )
        anomaly_threshold = DEFAULT_ANOMALY_THRESHOLD

    try:
        reference_series = load_measurement_series(db, sensor_id=sensor.id, start=reference_start, end=reference_end)
    except TooManyObservationsError as exc:
        limitations.append(f"Reference period had too many observations to load ({exc}) — anomaly detection and condition evidence were skipped.")
        reference_series = None

    simulated_series = perturbations_mod.apply_perturbation(
        baseline_series, scenario_type=scenario_type, modification_start=modification_start,
        modification_duration_seconds=modification_duration_seconds, magnitude=magnitude, parameters=parameters,
    )

    modification_end = modification_start + timedelta(seconds=modification_duration_seconds) if modification_duration_seconds is not None else None
    if not perturbations_mod.verify_outside_window_unchanged(baseline_series, simulated_series, modification_start=modification_start, modification_end=modification_end):
        raise ValidationFailedError(
            "Internal consistency check failed: the simulated series changed a value outside its declared "
            "modification window. This run was refused rather than persisted — please report this as a bug."
        )

    anomalies = _recent_anomalies_as_dicts(db, organization_id=organization_id, sensor_id=sensor.id, start=baseline_start, end=baseline_end)

    common_kwargs = dict(
        sensor_id=sensor.id, sensor_unit=sensor.unit, expected_interval_seconds=sensor.expected_interval_seconds,
        window_start=baseline_start, window_end=baseline_end, reference_series=reference_series,
        reference_start=reference_start if reference_series is not None else None,
        reference_end=reference_end if reference_series is not None else None,
        anomalies=anomalies, anomaly_threshold=anomaly_threshold, seasonal_period_seconds=DEFAULT_SEASONAL_PERIOD_SECONDS,
        include_forecast=include_forecast,
    )
    baseline_bundle = pipeline_mod.run_series_through_pipeline(baseline_series, is_simulated=False, **common_kwargs)
    simulated_bundle = pipeline_mod.run_series_through_pipeline(simulated_series, is_simulated=True, **common_kwargs)
    comparison = comparison_mod.compare_bundles(baseline_bundle, simulated_bundle)

    provenance = {
        "sensor_id": sensor.id,
        "baseline_window": {"start": baseline_start.isoformat(), "end": baseline_end.isoformat()},
        "reference_window": {"start": reference_start.isoformat(), "end": reference_end.isoformat()} if reference_series is not None else None,
        "modification_window": {"start": modification_start.isoformat(), "end": modification_end.isoformat() if modification_end else baseline_end.isoformat()},
        "scenario_type": scenario_type,
        "magnitude": magnitude,
        "model_version_used": {"id": model_version.id, "version": model_version.version} if model_version else None,
        "executed_at": datetime.now(timezone.utc).isoformat(),
    }

    return {"baseline": baseline_bundle, "simulated": simulated_bundle, "comparison": comparison, "limitations": limitations, "provenance": provenance}


def _scenario_config_snapshot(scenario: Scenario) -> dict[str, Any]:
    return {
        "scenario_id": scenario.id, "version": scenario.version, "name": scenario.name,
        "scenario_type": scenario.scenario_type.value, "sensor_id": scenario.sensor_id, "sensor_scope": scenario.sensor_scope,
        "baseline_window_start": scenario.baseline_window_start.isoformat(), "baseline_window_end": scenario.baseline_window_end.isoformat(),
        "reference_window_start": scenario.reference_window_start.isoformat() if scenario.reference_window_start else None,
        "reference_window_end": scenario.reference_window_end.isoformat() if scenario.reference_window_end else None,
        "modification_start": scenario.modification_start.isoformat() if scenario.modification_start else None,
        "modification_duration_seconds": scenario.modification_duration_seconds,
        "magnitude": scenario.magnitude, "unit": scenario.unit, "parameters": scenario.parameters,
    }


def _estimate_scenario_rows(scenario: Scenario) -> int:
    span_seconds = (_ensure_utc(scenario.baseline_window_end) - _ensure_utc(scenario.baseline_window_start)).total_seconds()
    n_sensors = len(scenario.sensor_scope) if scenario.scenario_type == ScenarioType.MULTI_SENSOR else 1
    return max(1, int(span_seconds / DEFAULT_EXPECTED_INTERVAL_SECONDS) + 1) * n_sensors * 2  # *2 for the reference window, roughly


# ---------------------------------------------------------------------------
# Run creation & execution
# ---------------------------------------------------------------------------


def create_scenario_run(
    db: Session, *, organization_id: str, user_id: Optional[str], scenario_id: str,
    include_forecast_comparison: bool = False, correlation_id: Optional[str] = None,
) -> ScenarioRun:
    scenario = get_scenario(db, organization_id=organization_id, scenario_id=scenario_id)
    if scenario.status != ScenarioStatus.VALIDATED:
        raise ConflictError(
            f"Scenario '{scenario_id}' is not validated (status={scenario.status.value}) — call POST "
            ".../validate first. This platform never runs an unvalidated scenario."
        )

    run = ScenarioRun(
        organization_id=organization_id, scenario_id=scenario.id, scenario_version=scenario.version,
        run_kind=ScenarioRunKind.SINGLE, status=ScenarioRunStatus.QUEUED,
        config_snapshot={**_scenario_config_snapshot(scenario), "include_forecast_comparison": include_forecast_comparison},
        pipeline_versions={}, created_by_user_id=user_id,
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    estimated_rows = _estimate_scenario_rows(scenario)
    if estimated_rows <= MAX_SCENARIO_ROWS_SYNC:
        execute_scenario_run(db, run_id=run.id)
        db.refresh(run)
        return run

    from app.jobs.runner import DuplicateJobError, job_runner

    try:
        job_id = job_runner.submit(
            organization_id=organization_id, job_type=JobType.SCENARIO_SIMULATION_RUN,
            payload={"run_id": run.id}, correlation_id=correlation_id, dedup_key=run.id,
        )
    except DuplicateJobError as exc:
        raise ConflictError("A scenario simulation job is already running for this run.", details={"existing_job_id": exc.existing_job_id}) from exc
    run.background_job_id = job_id
    db.commit()
    db.refresh(run)
    return run


def execute_scenario_run(db: Session, *, run_id: str) -> dict[str, Any]:
    """The actual simulation work — called synchronously from
    create_scenario_run for bounded runs, or from
    app.jobs.handlers._run_scenario_simulation for larger ones. Takes a
    session rather than assuming a request-scoped one, mirroring
    app.services.anomaly_service.execute_detection_run exactly."""
    run = db.get(ScenarioRun, run_id)
    if run is None:
        raise NotFoundError(f"Scenario run '{run_id}' not found.")
    scenario = db.get(Scenario, run.scenario_id)

    run.status = ScenarioRunStatus.RUNNING
    run.started_at = datetime.now(timezone.utc)
    db.commit()

    try:
        cfg = run.config_snapshot
        include_forecast = bool(cfg.get("include_forecast_comparison", False))
        baseline_start, baseline_end = _ensure_utc(scenario.baseline_window_start), _ensure_utc(scenario.baseline_window_end)
        model_version_id = (scenario.parameters or {}).get("model_version_id")

        if scenario.scenario_type == ScenarioType.MULTI_SENSOR:
            sub_configs = (scenario.parameters or {}).get("per_sensor", [])
            per_sensor: dict[str, Any] = {}
            for sub in sub_configs:
                sensor = get_sensor_in_org(db, sensor_id=sub["sensor_id"], organization_id=scenario.organization_id)
                per_sensor[sub["sensor_id"]] = _run_one_sensor_scenario(
                    db, organization_id=scenario.organization_id, sensor=sensor, baseline_start=baseline_start, baseline_end=baseline_end,
                    reference_start_cfg=scenario.reference_window_start, reference_end_cfg=scenario.reference_window_end,
                    scenario_type=sub["scenario_type"], modification_start=_ensure_utc(datetime.fromisoformat(sub["modification_start"])),
                    modification_duration_seconds=sub.get("modification_duration_seconds"), magnitude=sub["magnitude"],
                    parameters=sub.get("parameters"), model_version_id=sub.get("model_version_id"), include_forecast=include_forecast,
                )
            results = {
                "run_type": "multi_sensor",
                "per_sensor": per_sensor,
                "limitations": comparison_mod.MANDATORY_LIMITATIONS + [
                    "MULTI_SENSOR runs each sensor's scenario completely independently — no relationship between "
                    "sensors is inferred, modeled, or combined into a single result."
                ],
                "provenance": {"scenario_id": scenario.id, "scenario_version": scenario.version, "sensor_scope": scenario.sensor_scope, "executed_at": datetime.now(timezone.utc).isoformat()},
            }
        else:
            sensor = get_sensor_in_org(db, sensor_id=scenario.sensor_id, organization_id=scenario.organization_id)
            single = _run_one_sensor_scenario(
                db, organization_id=scenario.organization_id, sensor=sensor, baseline_start=baseline_start, baseline_end=baseline_end,
                reference_start_cfg=scenario.reference_window_start, reference_end_cfg=scenario.reference_window_end,
                scenario_type=scenario.scenario_type.value, modification_start=_ensure_utc(scenario.modification_start),
                modification_duration_seconds=scenario.modification_duration_seconds, magnitude=scenario.magnitude,
                parameters=scenario.parameters, model_version_id=model_version_id, include_forecast=include_forecast,
            )
            results = {"run_type": "single", **single}

        run.results = results
        run.pipeline_versions = dict(pipeline_mod.PIPELINE_FUNCTION_PROVENANCE)
        run.status = ScenarioRunStatus.SUCCEEDED
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(run)
        return {"scenario_run_id": run.id, "status": run.status.value}
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        run = db.get(ScenarioRun, run_id)
        run.status = ScenarioRunStatus.FAILED
        run.error_message = str(exc)[:2000]
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        raise


def get_scenario_run(db: Session, *, organization_id: str, run_id: str) -> ScenarioRun:
    run = db.query(ScenarioRun).filter(ScenarioRun.id == run_id, ScenarioRun.organization_id == organization_id).first()
    if run is None:
        raise NotFoundError(f"Scenario run '{run_id}' not found.")
    return run


def list_scenario_runs(db: Session, *, organization_id: str, scenario_id: Optional[str], limit: int, offset: int) -> tuple[list[ScenarioRun], int]:
    query = db.query(ScenarioRun).filter(ScenarioRun.organization_id == organization_id)
    if scenario_id:
        query = query.filter(ScenarioRun.scenario_id == scenario_id)
    query = query.order_by(ScenarioRun.created_at.desc())
    total = query.count()
    return query.offset(offset).limit(limit).all(), total


# ---------------------------------------------------------------------------
# Multi-run multi-scenario comparison — packages real per-run
# summaries side by side. No combined score is ever computed.
# ---------------------------------------------------------------------------


def compare_scenario_runs(db: Session, *, organization_id: str, run_ids: list[str]) -> dict[str, Any]:
    if len(run_ids) < 2:
        raise ValidationFailedError("compare requires at least 2 scenario_run_ids.")
    if len(run_ids) > 10:
        raise ValidationFailedError("compare supports at most 10 scenario_run_ids at once (bounded compute).")

    runs = []
    for run_id in run_ids:
        run = get_scenario_run(db, organization_id=organization_id, run_id=run_id)
        if run.status != ScenarioRunStatus.SUCCEEDED:
            raise ConflictError(f"Scenario run '{run_id}' has status={run.status.value}; only SUCCEEDED runs can be compared.")
        runs.append(run)

    return {
        "runs": [
            {
                "scenario_run_id": r.id, "scenario_id": r.scenario_id, "scenario_version": r.scenario_version,
                "run_kind": r.run_kind.value, "results": r.results, "created_at": r.created_at.isoformat(),
            }
            for r in runs
        ],
        "note": (
            "This lists each run's independently-computed real result side by side. No combined 'future risk' "
            "number is computed across scenarios — see app.simulation.comparison's module docstring."
        ),
    }


# ---------------------------------------------------------------------------
# Sensitivity analysis — a magnitude sweep, single-sensor scenarios only
# ---------------------------------------------------------------------------


def run_sensitivity_sweep(db: Session, *, organization_id: str, user_id: Optional[str], scenario_id: str, magnitudes: list[float]) -> ScenarioRun:
    scenario = get_scenario(db, organization_id=organization_id, scenario_id=scenario_id)
    if scenario.status != ScenarioStatus.VALIDATED:
        raise ConflictError(f"Scenario '{scenario_id}' is not validated (status={scenario.status.value}) — call POST .../validate first.")
    if scenario.scenario_type == ScenarioType.MULTI_SENSOR:
        raise ValidationFailedError("Sensitivity sweeps are only supported for single-sensor scenario types — a multi-sensor sweep would require inventing a combined magnitude, which this platform never does.")
    if not magnitudes:
        raise ValidationFailedError("magnitudes must be a non-empty list.")
    if len(magnitudes) > MAX_SENSITIVITY_SWEEP_POINTS:
        raise ValidationFailedError(f"A sensitivity sweep supports at most {MAX_SENSITIVITY_SWEEP_POINTS} magnitude points per run (bounded compute).")

    run = ScenarioRun(
        organization_id=organization_id, scenario_id=scenario.id, scenario_version=scenario.version,
        run_kind=ScenarioRunKind.SENSITIVITY_SWEEP, status=ScenarioRunStatus.RUNNING,
        config_snapshot={**_scenario_config_snapshot(scenario), "magnitudes": magnitudes},
        pipeline_versions={}, created_by_user_id=user_id, started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    try:
        sensor = get_sensor_in_org(db, sensor_id=scenario.sensor_id, organization_id=organization_id)
        baseline_start, baseline_end = _ensure_utc(scenario.baseline_window_start), _ensure_utc(scenario.baseline_window_end)
        model_version_id = (scenario.parameters or {}).get("model_version_id")

        sweep_points = []
        for magnitude in magnitudes:
            single = _run_one_sensor_scenario(
                db, organization_id=organization_id, sensor=sensor, baseline_start=baseline_start, baseline_end=baseline_end,
                reference_start_cfg=scenario.reference_window_start, reference_end_cfg=scenario.reference_window_end,
                scenario_type=scenario.scenario_type.value, modification_start=_ensure_utc(scenario.modification_start),
                modification_duration_seconds=scenario.modification_duration_seconds, magnitude=magnitude,
                parameters=scenario.parameters, model_version_id=model_version_id, include_forecast=False,
            )
            sweep_points.append({"magnitude": magnitude, "comparison": single["comparison"]})

        sweep = comparison_mod.summarize_sensitivity_sweep(sweep_points)
        run.results = {
            "run_type": "sensitivity_sweep", "sensitivity": sweep,
            "limitations": comparison_mod.MANDATORY_LIMITATIONS,
            "provenance": {"scenario_id": scenario.id, "scenario_version": scenario.version, "sensor_id": sensor.id, "executed_at": datetime.now(timezone.utc).isoformat()},
        }
        run.pipeline_versions = dict(pipeline_mod.PIPELINE_FUNCTION_PROVENANCE)
        run.status = ScenarioRunStatus.SUCCEEDED
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(run)
        return run
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        run = db.get(ScenarioRun, run.id)
        run.status = ScenarioRunStatus.FAILED
        run.error_message = str(exc)[:2000]
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        raise
