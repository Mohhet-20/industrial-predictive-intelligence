"""Thin orchestration layer between the API and app.timeseries — resolves
sensors/organization scope, loads bounded data via app.timeseries.data_access,
and delegates every actual computation to the (DB-agnostic, unit-testable)
app.timeseries modules.
"""
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.core.errors import NotFoundError, ValidationFailedError
from app.models.asset import Asset
from app.models.sensor import Sensor
from app.timeseries import analysis, change_detection, multi_sensor, profiling, resampling
from app.timeseries.data_access import TooManyObservationsError, load_measurement_series, load_multi_sensor_series
from app.timeseries.resampling import AggregationMethod, InterpolationPolicy, TooManyBucketsError

DEFAULT_LOOKBACK_BUFFER = timedelta(days=1)


def get_sensor_in_org(db: Session, *, sensor_id: str, organization_id: str) -> Sensor:
    sensor = db.query(Sensor).join(Sensor.asset).filter(Sensor.id == sensor_id, Asset.organization_id == organization_id).first()
    if sensor is None:
        raise NotFoundError(f"Sensor '{sensor_id}' not found.")
    return sensor


def validate_time_range(start: datetime, end: datetime, *, max_days: int = 400) -> None:
    if end <= start:
        raise ValidationFailedError("end_time must be after start_time.")
    if (end - start) > timedelta(days=max_days):
        raise ValidationFailedError(f"Requested range exceeds the maximum of {max_days} days.")


def get_sensor_profile(db: Session, *, sensor_id: str, organization_id: str, start: datetime, end: datetime) -> dict[str, Any]:
    validate_time_range(start, end)
    sensor = get_sensor_in_org(db, sensor_id=sensor_id, organization_id=organization_id)
    try:
        series = load_measurement_series(db, sensor_id=sensor.id, start=start, end=end)
    except TooManyObservationsError as exc:
        raise ValidationFailedError(str(exc))
    return profiling.compute_profile(series, sensor_unit=sensor.unit, expected_interval_seconds=sensor.expected_interval_seconds, requested_start=start, requested_end=end)


def get_temporal_analysis(
    db: Session,
    *,
    sensor_id: str,
    organization_id: str,
    start: datetime,
    end: datetime,
    rolling_window_seconds: float,
    volatility_window_seconds: float,
    autocorr_grid_freq_seconds: Optional[float],
    autocorr_max_lag_steps: int,
) -> dict[str, Any]:
    validate_time_range(start, end)
    sensor = get_sensor_in_org(db, sensor_id=sensor_id, organization_id=organization_id)
    try:
        series = load_measurement_series(db, sensor_id=sensor.id, start=start, end=end)
    except TooManyObservationsError as exc:
        raise ValidationFailedError(str(exc))

    result: dict[str, Any] = {
        "sensor_id": sensor.id,
        "observation_count": len(series),
        "requested_range": {"start": start.isoformat(), "end": end.isoformat()},
        "rolling": rolling_statistics_or_none(series, rolling_window_seconds),
        "rate_of_change": analysis.rate_of_change(series),
        "volatility": analysis.volatility(series, window_seconds=volatility_window_seconds),
    }

    grid_freq = autocorr_grid_freq_seconds or sensor.expected_interval_seconds
    if grid_freq:
        result["autocorrelation"] = analysis.autocorrelation(series, grid_freq_seconds=grid_freq, max_lag_steps=autocorr_max_lag_steps)
    else:
        result["autocorrelation"] = {"lags": [], "note": "no expected_interval_seconds declared for this sensor and no grid_freq_seconds provided — autocorrelation requires a regular grid to resample onto."}

    return result


def rolling_statistics_or_none(series, window_seconds: float) -> dict[str, Any]:
    return analysis.rolling_statistics(series, window_seconds=window_seconds)


def get_resampled_series(
    db: Session,
    *,
    sensor_id: str,
    organization_id: str,
    start: datetime,
    end: datetime,
    freq_seconds: float,
    aggregation: AggregationMethod,
    interpolation: InterpolationPolicy,
) -> dict[str, Any]:
    validate_time_range(start, end)
    sensor = get_sensor_in_org(db, sensor_id=sensor_id, organization_id=organization_id)
    try:
        series = load_measurement_series(db, sensor_id=sensor.id, start=start, end=end)
    except TooManyObservationsError as exc:
        raise ValidationFailedError(str(exc))
    try:
        buckets = resampling.resample_series(series, start=start, end=end, freq_seconds=freq_seconds, aggregation=aggregation, interpolation=interpolation)
    except TooManyBucketsError as exc:
        raise ValidationFailedError(str(exc))
    return {"sensor_id": sensor.id, "aggregation": aggregation.value, "interpolation": interpolation.value, "freq_seconds": freq_seconds, "buckets": buckets}


def get_change_candidates(
    db: Session,
    *,
    sensor_id: str,
    organization_id: str,
    start: datetime,
    end: datetime,
    window_size: int,
    min_level_score: float,
    min_variance_ratio: float,
) -> dict[str, Any]:
    validate_time_range(start, end)
    sensor = get_sensor_in_org(db, sensor_id=sensor_id, organization_id=organization_id)
    try:
        series = load_measurement_series(db, sensor_id=sensor.id, start=start, end=end)
    except TooManyObservationsError as exc:
        raise ValidationFailedError(str(exc))
    result = change_detection.detect_change_candidates(series, window_size=window_size, min_level_score=min_level_score, min_variance_ratio=min_variance_ratio)
    result["sensor_id"] = sensor.id
    return result


def get_correlation_matrix(
    db: Session, *, sensor_ids: list[str], organization_id: str, start: datetime, end: datetime, freq_seconds: float
) -> dict[str, Any]:
    validate_time_range(start, end)
    if len(sensor_ids) < 2:
        raise ValidationFailedError("At least 2 sensor_ids are required for correlation analysis.")
    for sid in sensor_ids:
        get_sensor_in_org(db, sensor_id=sid, organization_id=organization_id)
    try:
        series_by_sensor = load_multi_sensor_series(db, sensor_ids=sensor_ids, start=start, end=end)
    except TooManyObservationsError as exc:
        raise ValidationFailedError(str(exc))
    aligned = multi_sensor.align_sensors_on_grid(series_by_sensor, start=start, end=end, freq_seconds=freq_seconds)
    return multi_sensor.correlation_matrix(aligned)


def get_lagged_correlation(
    db: Session,
    *,
    sensor_a_id: str,
    sensor_b_id: str,
    organization_id: str,
    start: datetime,
    end: datetime,
    freq_seconds: float,
    max_lag_steps: int,
) -> dict[str, Any]:
    validate_time_range(start, end)
    get_sensor_in_org(db, sensor_id=sensor_a_id, organization_id=organization_id)
    get_sensor_in_org(db, sensor_id=sensor_b_id, organization_id=organization_id)
    try:
        series_by_sensor = load_multi_sensor_series(db, sensor_ids=[sensor_a_id, sensor_b_id], start=start, end=end)
    except TooManyObservationsError as exc:
        raise ValidationFailedError(str(exc))
    aligned = multi_sensor.align_sensors_on_grid(series_by_sensor, start=start, end=end, freq_seconds=freq_seconds)
    return multi_sensor.lagged_cross_correlation(aligned, sensor_a=sensor_a_id, sensor_b=sensor_b_id, max_lag_steps=max_lag_steps, freq_seconds=freq_seconds)


def get_missingness_comparison(db: Session, *, sensor_ids: list[str], organization_id: str, start: datetime, end: datetime) -> dict[str, Any]:
    validate_time_range(start, end)
    fractions: dict[str, Optional[float]] = {}
    for sid in sensor_ids:
        profile = get_sensor_profile(db, sensor_id=sid, organization_id=organization_id, start=start, end=end)
        missingness = profile.get("missingness")
        fractions[sid] = missingness["missing_fraction"] if missingness else None
    return multi_sensor.compare_missingness(fractions)
