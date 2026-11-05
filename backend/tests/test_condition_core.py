"""Pure-function tests for app.condition.* — no database, no FastAPI. See
tests/test_anomaly_detection_core.py for the equivalent original coverage
this mirrors."""
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.condition import degradation, evidence, forecasting

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _make_series(n: int, *, freq_seconds: float = 300.0, values=None, start=START) -> pd.Series:
    idx = [start + timedelta(seconds=freq_seconds * i) for i in range(n)]
    if values is None:
        values = np.zeros(n)
    return pd.Series(values, index=pd.DatetimeIndex(idx))


def _daily_cycle_series(days: float, *, amplitude=3.0, baseline=58.0, noise_std=0.6, drift_per_day=0.0, seed=1, freq_seconds=300.0) -> pd.Series:
    rng = np.random.default_rng(seed)
    n = int(days * 86400 / freq_seconds)
    idx = [START + timedelta(seconds=freq_seconds * i) for i in range(n)]
    t_hours = np.array([(ts - START).total_seconds() / 3600.0 for ts in idx])
    values = baseline + amplitude * np.sin(2 * np.pi * t_hours / 24.0) + drift_per_day * (t_hours / 24.0) + rng.normal(0, noise_std, n)
    return pd.Series(values, index=pd.DatetimeIndex(idx))


# ---------------------------------------------------------------------------
# app.condition.evidence
# ---------------------------------------------------------------------------


def test_evidence_warns_when_reference_overlaps_window():
    series = _daily_cycle_series(14)
    overlapping_reference_end = START + timedelta(days=10)  # inside the analysis window below
    ev = evidence.compute_condition_evidence(
        sensor_id="s1", sensor_unit="celsius", expected_interval_seconds=300.0,
        window_series=series[series.index >= pd.Timestamp(START + timedelta(days=7))],
        window_start=START + timedelta(days=7), window_end=START + timedelta(days=14),
        reference_series=series, reference_start=START, reference_end=overlapping_reference_end,
        anomalies=[], aligned_sibling_series=None,
    )
    assert ev["deviation_from_baseline"]["reference_overlaps_window"] is True
    assert "not an independent baseline" in ev["deviation_from_baseline"]["overlap_warning"]


def test_evidence_no_warning_when_reference_precedes_window():
    series = _daily_cycle_series(14)
    ev = evidence.compute_condition_evidence(
        sensor_id="s1", sensor_unit="celsius", expected_interval_seconds=300.0,
        window_series=series[series.index >= pd.Timestamp(START + timedelta(days=7))],
        window_start=START + timedelta(days=7), window_end=START + timedelta(days=14),
        reference_series=series[series.index < pd.Timestamp(START + timedelta(days=7))],
        reference_start=START, reference_end=START + timedelta(days=7),
        anomalies=[], aligned_sibling_series=None,
    )
    assert "reference_overlaps_window" not in ev["deviation_from_baseline"]


def test_no_composite_score_is_ever_produced():
    series = _daily_cycle_series(7)
    ev = evidence.compute_condition_evidence(
        sensor_id="s1", sensor_unit="celsius", expected_interval_seconds=300.0,
        window_series=series, window_start=START, window_end=START + timedelta(days=7),
        reference_series=series, reference_start=START, reference_end=START + timedelta(days=7),
        anomalies=[], aligned_sibling_series=None,
    )
    assert ev["composite_score"] is None
    assert "does not compute a single blended" in ev["composite_score_note"]


def test_deviation_from_baseline_insufficient_reference_data_is_honest():
    reference = _make_series(5)  # far below MIN_FOR_BASELINE=30
    window = _make_series(10, start=START + timedelta(days=1))
    result = evidence.compute_deviation_from_baseline(window, reference_series=reference, reference_start=START, reference_end=START + timedelta(hours=1))
    assert result["computed"] is False
    assert "at least" in result["reason"]


def test_deviation_from_baseline_direction_and_scale():
    rng = np.random.default_rng(0)
    reference = pd.Series(rng.normal(0, 1.0, 200), index=[START + timedelta(minutes=i) for i in range(200)])
    # window sits 10 robust-std-equivalents above the reference median
    window = pd.Series(np.full(20, 10.0), index=[START + timedelta(days=1, minutes=i) for i in range(20)])
    result = evidence.compute_deviation_from_baseline(window, reference_series=reference, reference_start=reference.index[0], reference_end=reference.index[-1])
    assert result["computed"] is True
    assert result["direction"] == "above_baseline"
    assert result["deviation_mad_units"] > 5  # clearly, robustly above


def test_anomaly_persistence_frequency_increasing_detected():
    window_start = START
    window_end = START + timedelta(days=10)
    anomalies = [
        {"severity_tier": "candidate_anomaly", "status": "detected", "period_start": window_start + timedelta(days=1)},
        {"severity_tier": "candidate_anomaly", "status": "detected", "period_start": window_start + timedelta(days=8)},
        {"severity_tier": "high_priority_anomaly", "status": "detected", "period_start": window_start + timedelta(days=8.5)},
        {"severity_tier": "high_priority_anomaly", "status": "detected", "period_start": window_start + timedelta(days=9)},
    ]
    result = evidence.summarize_anomaly_persistence(anomalies, window_start=window_start, window_end=window_end)
    assert result["anomaly_count"] == 4
    assert result["frequency_increasing"] is True
    assert result["count_by_severity_tier"]["high_priority_anomaly"] == 2


def test_anomaly_persistence_empty_is_honest_not_fabricated():
    result = evidence.summarize_anomaly_persistence([], window_start=START, window_end=START + timedelta(days=1))
    assert result["anomaly_count"] == 0
    assert result["frequency_increasing"] is None


# ---------------------------------------------------------------------------
# app.condition.degradation
# ---------------------------------------------------------------------------


def test_sub_window_trends_are_leakage_safe_and_non_overlapping():
    series = _daily_cycle_series(21, drift_per_day=0.3, amplitude=0.0, noise_std=0.05)  # strong monotonic drift, low noise
    trends = degradation.compute_sub_window_trends(series, window_start=START, window_end=START + timedelta(days=21))
    assert len(trends) == degradation.SUB_WINDOW_COUNT
    # sub-windows must be strictly chronologically ordered and non-overlapping
    for a, b in zip(trends, trends[1:]):
        assert datetime.fromisoformat(a.end) <= datetime.fromisoformat(b.start)
    # a strong, low-noise monotonic drift should be significant in most sub-windows
    assert sum(1 for t in trends if t.significant) >= degradation.MAJORITY_SUB_WINDOWS


def test_classify_degradation_no_signal_when_no_significant_trend():
    flat_trends = degradation.compute_sub_window_trends(_daily_cycle_series(21, amplitude=0.0, drift_per_day=0.0, noise_std=1.0), window_start=START, window_end=START + timedelta(days=21))
    result = degradation.classify_degradation(
        sub_window_trends=flat_trends,
        deviation_evidence={"deviation_mad_units": None},
        anomaly_evidence={"frequency_increasing": None},
    )
    assert result["stage"] is None
    assert "no degradation signal" in result["rationale"].lower()


def test_classify_degradation_observed_trend_without_corroboration():
    series = _daily_cycle_series(21, drift_per_day=0.3, amplitude=0.0, noise_std=0.05)
    trends = degradation.compute_sub_window_trends(series, window_start=START, window_end=START + timedelta(days=21))
    result = degradation.classify_degradation(
        sub_window_trends=trends,
        deviation_evidence={"deviation_mad_units": 0.1},  # below CORROBORATION_MAD_THRESHOLD
        anomaly_evidence={"frequency_increasing": False},
    )
    assert result["stage"] == "observed_trend"
    assert result["persistent"] is True
    assert result["corroborated"] is False


def test_classify_degradation_promotes_to_candidate_with_corroboration():
    series = _daily_cycle_series(21, drift_per_day=0.3, amplitude=0.0, noise_std=0.05)
    trends = degradation.compute_sub_window_trends(series, window_start=START, window_end=START + timedelta(days=21))
    result = degradation.classify_degradation(
        sub_window_trends=trends,
        deviation_evidence={"deviation_mad_units": 3.0},  # above CORROBORATION_MAD_THRESHOLD=2.0
        anomaly_evidence={"frequency_increasing": False},
    )
    assert result["stage"] == "degradation_candidate"
    assert result["corroborated_by_deviation"] is True


def test_classify_degradation_never_reaches_validated_automatically():
    """No combination of evidence this module can produce should ever
    output 'validated_degradation' — that stage is only reachable through
    app.services.condition_service.validate_degradation_signal, a human
    action. This is a structural guarantee, not just a convention."""
    series = _daily_cycle_series(21, drift_per_day=1.0, amplitude=0.0, noise_std=0.01)
    trends = degradation.compute_sub_window_trends(series, window_start=START, window_end=START + timedelta(days=21))
    result = degradation.classify_degradation(
        sub_window_trends=trends,
        deviation_evidence={"deviation_mad_units": 50.0},
        anomaly_evidence={"frequency_increasing": True},
    )
    assert result["stage"] in (None, "observed_trend", "degradation_candidate")
    assert result["stage"] != "validated_degradation"


# ---------------------------------------------------------------------------
# app.condition.forecasting
# ---------------------------------------------------------------------------


def test_backtest_refuses_on_insufficient_fit_data():
    series = _daily_cycle_series(1)  # far too little for MIN_FIT_OBSERVATIONS
    result = forecasting.backtest_and_compare(series, fit_start=START, fit_end=START + timedelta(hours=2), test_end=START + timedelta(hours=4))
    assert result["computed"] is False
    assert "insufficient" in result["reason"]


def test_backtest_fit_is_unaffected_by_future_corruption():
    """Corrupting every value strictly after fit_end must not change the
    fit (proof the fit step is structurally blind to future data, not
    just an assertion about the code's intent) — the leak-free analogue of
    tests/test_features.py's planted-future-value tests."""
    series = _daily_cycle_series(21, drift_per_day=0.1)
    fit_end = START + timedelta(days=14)
    test_end = START + timedelta(days=21)

    original = forecasting.backtest_and_compare(series, fit_start=START, fit_end=fit_end, test_end=test_end)

    corrupted = series.copy()
    corrupted[corrupted.index >= pd.Timestamp(fit_end)] = 1e9  # implausible future values
    after = forecasting.backtest_and_compare(corrupted, fit_start=START, fit_end=fit_end, test_end=test_end)

    assert original["trend_fit_summary"]["slope_per_hour"] == pytest.approx(after["trend_fit_summary"]["slope_per_hour"])


def test_strong_deterministic_linear_trend_beats_baselines_and_is_justified():
    """A pure, near-noiseless linear ramp with NO seasonality is exactly
    the case where linear_trend should legitimately win — proof the
    justification gate can say yes, not just refuse."""
    n = 3000
    idx = [START + timedelta(minutes=i) for i in range(n)]
    values = 10.0 + 0.01 * np.arange(n) + np.random.default_rng(3).normal(0, 0.01, n)
    series = pd.Series(values, index=pd.DatetimeIndex(idx))
    fit_end = idx[2400]
    test_end = idx[-1]
    result = forecasting.backtest_and_compare(series, fit_start=idx[0], fit_end=fit_end, test_end=test_end, seasonal_period_seconds=86400.0)
    assert result["computed"] is True
    assert result["is_justified"] is True
    assert result["evaluations"]["linear_trend"]["mae"] < result["evaluations"]["naive_last_value"]["mae"]


def test_seasonal_series_without_drift_is_honestly_not_justified():
    """A realistic sensor series with strong daily seasonality and NO real
    drift (this platform's actual synthetic fleet shape for most sensors —
    see app.synthetic.generator) must not be reported as a justified
    linear_trend forecast: a straight line ignores the daily cycle
    entirely and loses to simple persistence. This is the honest-refusal
    path requires, exercised against the platform's own realistic data
    shape rather than a contrived case."""
    series = _daily_cycle_series(21, amplitude=3.0, noise_std=0.6, drift_per_day=0.0, seed=7)
    result = forecasting.backtest_and_compare(series, fit_start=START, fit_end=START + timedelta(days=14), test_end=START + timedelta(days=21))
    assert result["computed"] is True
    assert result["is_justified"] is False
    assert "does not beat baseline" in result["justification_note"]


def test_generate_forecast_points_bounded_and_leak_free():
    series = _daily_cycle_series(21, drift_per_day=0.3, amplitude=0.0, noise_std=0.01)
    as_of = START + timedelta(days=14)
    points = forecasting.generate_forecast_points(series, as_of=as_of, horizon_seconds=86400.0, step_seconds=3600.0)
    assert len(points) == 24
    assert all(datetime.fromisoformat(p["target_timestamp"]) > as_of for p in points)
    assert all(p["lower_bound"] <= p["predicted_value"] <= p["upper_bound"] for p in points)


def test_generate_forecast_points_capped_at_max():
    series = _daily_cycle_series(21, drift_per_day=0.1, amplitude=0.0, noise_std=0.01)
    as_of = START + timedelta(days=14)
    points = forecasting.generate_forecast_points(series, as_of=as_of, horizon_seconds=30 * 86400.0, step_seconds=60.0)
    assert len(points) == forecasting.MAX_FORECAST_POINTS
