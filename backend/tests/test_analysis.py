from datetime import datetime, timedelta, timezone

import pandas as pd

from app.timeseries.analysis import MIN_FOR_AUTOCORRELATION, autocorrelation, rate_of_change, rolling_statistics, volatility

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _series(n, step_minutes=5, fn=lambda i: 50.0 + (i % 5)):
    index = pd.DatetimeIndex([BASE + timedelta(minutes=step_minutes * i) for i in range(n)], tz="UTC")
    return pd.Series([fn(i) for i in range(n)], index=index, dtype="float64")


def test_rolling_statistics_insufficient_data_reports_reason():
    series = _series(2)
    result = rolling_statistics(series, window_seconds=3600)
    assert result["points"] == []
    assert "insufficient data" in result["note"]


def test_rolling_statistics_produces_points_with_bounded_output_size():
    series = _series(600)
    result = rolling_statistics(series, window_seconds=3600)
    assert len(result["points"]) <= 500
    assert result["points"][0]["rolling_mean"] is not None


def test_rate_of_change_sign_matches_direction_of_series():
    series = _series(20, fn=lambda i: float(i))  # steadily increasing
    result = rate_of_change(series)
    assert all(p["rate_per_hour"] > 0 for p in result["points"])


def test_volatility_is_dimensionless_coefficient_of_variation():
    series = _series(30)
    result = volatility(series, window_seconds=3600)
    assert result["method"] == "rolling_coefficient_of_variation"
    for p in result["points"]:
        assert isinstance(p["coefficient_of_variation"], float)


def test_autocorrelation_below_minimum_reports_reason_not_fabricated_lags():
    series = _series(MIN_FOR_AUTOCORRELATION - 5)
    result = autocorrelation(series, grid_freq_seconds=300, max_lag_steps=10)
    assert result["lags"] == []
    assert "insufficient" in result["note"]


def test_autocorrelation_detects_periodic_signal():
    import math

    n = 100
    index = pd.DatetimeIndex([BASE + timedelta(minutes=5 * i) for i in range(n)], tz="UTC")
    values = [10.0 * math.sin(2 * math.pi * i / 10) for i in range(n)]  # period of 10 steps
    series = pd.Series(values, index=index, dtype="float64")
    result = autocorrelation(series, grid_freq_seconds=300, max_lag_steps=20)
    lag_10 = next(l for l in result["lags"] if l["lag_steps"] == 10)
    assert lag_10["correlation"] > 0.8  # strong autocorrelation at the true period


def test_autocorrelation_low_coverage_adds_a_caveat_note():
    # sparse, irregular series after gridding -> coverage < 80%
    index = pd.DatetimeIndex([BASE + timedelta(minutes=17 * i) for i in range(40)], tz="UTC")
    series = pd.Series([50.0 + (i % 3) for i in range(40)], index=index, dtype="float64")
    result = autocorrelation(series, grid_freq_seconds=300, max_lag_steps=10)
    if result["coverage"]["coverage_fraction"] is not None and result["coverage"]["coverage_fraction"] < 0.8:
        assert result["note"] is not None and "indicative" in result["note"]
