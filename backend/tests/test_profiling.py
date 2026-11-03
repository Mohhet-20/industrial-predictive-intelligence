from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from app.timeseries.profiling import MIN_FOR_BASIC_STATS, MIN_FOR_DISTRIBUTION, MIN_FOR_TREND, compute_profile

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _series(n, *, start=BASE, step_minutes=5, values=None):
    index = pd.DatetimeIndex([start + timedelta(minutes=step_minutes * i) for i in range(n)], tz="UTC")
    if values is None:
        values = [50.0 + i * 0.1 for i in range(n)]
    return pd.Series(values, index=index, dtype="float64")


def test_empty_series_reports_insufficient_data_not_a_crash():
    series = pd.Series(dtype="float64", index=pd.DatetimeIndex([], tz="UTC"))
    profile = compute_profile(series, sensor_unit="celsius", expected_interval_seconds=300, requested_start=BASE, requested_end=BASE + timedelta(hours=1))
    assert profile["insufficient_data"] is True
    assert profile["observation_count"] == 0
    assert "No measurements" in profile["insufficient_data_reason"]
    assert profile["descriptive_stats"] is None


def test_single_observation_below_min_for_basic_stats():
    series = _series(MIN_FOR_BASIC_STATS - 1)
    profile = compute_profile(series, sensor_unit="celsius", expected_interval_seconds=300, requested_start=BASE, requested_end=BASE + timedelta(hours=1))
    assert profile["insufficient_data"] is True
    assert profile["descriptive_stats"] is None


def test_below_distribution_threshold_reports_none_not_a_number():
    series = _series(MIN_FOR_DISTRIBUTION - 1)
    profile = compute_profile(series, sensor_unit="celsius", expected_interval_seconds=300, requested_start=BASE, requested_end=BASE + timedelta(hours=2))
    assert profile["distribution"]["skewness"] is None
    assert profile["distribution"]["kurtosis"] is None
    assert "insufficient data" in profile["distribution"]["note"]


def test_at_or_above_distribution_threshold_computes_real_numbers():
    rng = np.random.default_rng(42)
    values = list(rng.normal(50, 5, size=MIN_FOR_DISTRIBUTION))
    series = _series(MIN_FOR_DISTRIBUTION, values=values)
    profile = compute_profile(series, sensor_unit="celsius", expected_interval_seconds=300, requested_start=BASE, requested_end=BASE + timedelta(hours=3))
    assert profile["distribution"]["skewness"] is not None
    assert isinstance(profile["distribution"]["kurtosis"], float)


def test_below_trend_threshold_reports_no_trend_line():
    series = _series(MIN_FOR_TREND - 1)
    profile = compute_profile(series, sensor_unit="celsius", expected_interval_seconds=300, requested_start=BASE, requested_end=BASE + timedelta(hours=1))
    assert profile["trend"]["direction"] is None
    assert profile["trend"]["slope_per_hour"] is None


def test_clear_increasing_trend_is_detected():
    n = 50
    values = [10.0 + i * 2.0 for i in range(n)]  # strong, clean linear increase
    series = _series(n, values=values)
    profile = compute_profile(series, sensor_unit="celsius", expected_interval_seconds=300, requested_start=BASE, requested_end=BASE + timedelta(hours=5))
    assert profile["trend"]["direction"] == "increasing"
    assert profile["trend"]["r_squared"] > 0.9


def test_flat_series_reports_no_significant_trend():
    n = 50
    series = _series(n, values=[42.0] * n)
    profile = compute_profile(series, sensor_unit="celsius", expected_interval_seconds=300, requested_start=BASE, requested_end=BASE + timedelta(hours=5))
    assert profile["trend"]["direction"] == "no_significant_trend"


def test_gap_detection_flags_interval_exceeding_gap_factor():
    # readings every 5 minutes, then one big 3-hour gap
    index = pd.DatetimeIndex(
        [BASE + timedelta(minutes=5 * i) for i in range(15)] + [BASE + timedelta(hours=3, minutes=5 * 15)], tz="UTC"
    )
    series = pd.Series([50.0] * 16, index=index, dtype="float64")
    profile = compute_profile(series, sensor_unit="celsius", expected_interval_seconds=300, requested_start=BASE, requested_end=index[-1].to_pydatetime())
    assert profile["gap_count"] == 1
    assert profile["gaps"][0]["duration_seconds"] > 3000


def test_no_expected_interval_means_no_gap_detection_attempted():
    series = _series(20)
    profile = compute_profile(series, sensor_unit="celsius", expected_interval_seconds=None, requested_start=BASE, requested_end=BASE + timedelta(hours=2))
    assert profile["gaps"] == []
    assert profile["missingness"] is None


def test_profile_is_reproducible_for_identical_input():
    series = _series(60)
    p1 = compute_profile(series, sensor_unit="celsius", expected_interval_seconds=300, requested_start=BASE, requested_end=BASE + timedelta(hours=6))
    p2 = compute_profile(series, sensor_unit="celsius", expected_interval_seconds=300, requested_start=BASE, requested_end=BASE + timedelta(hours=6))
    assert p1 == p2
