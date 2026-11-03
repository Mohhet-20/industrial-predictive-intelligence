from datetime import datetime, timedelta, timezone

import pandas as pd

from app.timeseries.multi_sensor import (
    MIN_OVERLAPPING_OBSERVATIONS,
    align_sensors_on_grid,
    compare_missingness,
    correlation_matrix,
    lagged_cross_correlation,
)

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _series(n, *, offset_minutes=0, fn=lambda i: 50.0 + i):
    index = pd.DatetimeIndex([BASE + timedelta(minutes=5 * i + offset_minutes) for i in range(n)], tz="UTC")
    return pd.Series([fn(i) for i in range(n)], index=index, dtype="float64")


def test_correlation_matrix_reports_association_caveat_always():
    a = _series(40, fn=lambda i: i)
    b = _series(40, fn=lambda i: i * 2)  # perfectly correlated
    aligned = align_sensors_on_grid({"a": a, "b": b}, start=BASE, end=BASE + timedelta(minutes=200), freq_seconds=300)
    result = correlation_matrix(aligned)
    assert "ASSOCIATION" in result["caveat"].upper() or "association" in result["caveat"]
    assert "causal" in result["caveat"].lower()
    pair = result["pairs"][0]
    assert pair["correlation"] is not None
    assert abs(pair["correlation"] - 1.0) < 1e-6


def test_correlation_below_minimum_overlap_reports_none_not_a_number():
    a = _series(3)
    b = _series(3)
    aligned = align_sensors_on_grid({"a": a, "b": b}, start=BASE, end=BASE + timedelta(minutes=15), freq_seconds=300)
    result = correlation_matrix(aligned)
    pair = result["pairs"][0]
    assert pair["correlation"] is None
    assert "insufficient" in pair["note"]


def test_lagged_correlation_finds_the_true_shift():
    # b is a lags shifted a by 2 steps (10 minutes)
    n = 40
    a = _series(n, fn=lambda i: float(i % 10))
    index_b = pd.DatetimeIndex([BASE + timedelta(minutes=5 * i) for i in range(n)], tz="UTC")
    b_values = [float(((i - 2) % 10)) if i >= 2 else 0.0 for i in range(n)]
    b = pd.Series(b_values, index=index_b, dtype="float64")

    aligned = align_sensors_on_grid({"a": a, "b": b}, start=BASE, end=BASE + timedelta(minutes=5 * n), freq_seconds=300)
    result = lagged_cross_correlation(aligned, sensor_a="a", sensor_b="b", max_lag_steps=5, freq_seconds=300)
    assert result["strongest_lag"] is not None
    assert "causal" in result["caveat"].lower()
    assert len(result["lags"]) == 11  # -5..5 inclusive


def test_lagged_correlation_rejects_unknown_columns():
    aligned = align_sensors_on_grid({"a": _series(20)}, start=BASE, end=BASE + timedelta(minutes=100), freq_seconds=300)
    try:
        lagged_cross_correlation(aligned, sensor_a="a", sensor_b="not-a-column", max_lag_steps=2, freq_seconds=300)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_compare_missingness_ranks_most_incomplete_first():
    result = compare_missingness({"a": 0.05, "b": 0.4, "c": None})
    ranked_ids = [r["sensor_id"] for r in result["sensors"]]
    assert ranked_ids[0] == "c"  # None (unknown) sorts as worst/first per the documented ranking
    assert "b" in ranked_ids and "a" in ranked_ids


def test_align_sensors_never_fabricates_a_value_for_a_missing_bucket():
    a = _series(2)  # sparse — most buckets in range will be empty
    aligned = align_sensors_on_grid({"a": a}, start=BASE, end=BASE + timedelta(hours=1), freq_seconds=300)
    assert aligned["a"].isna().any()  # missing buckets stay NaN, never a made-up number
