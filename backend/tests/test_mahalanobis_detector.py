"""Unit tests for app.anomaly.mahalanobis_detector and its companion
app.anomaly.multivariate_synthetic_scenarios — the pure, DB-agnostic
robust-covariance multivariate detector added to catch
relationship-breakdown anomalies that app.anomaly.robust_zscore (single
sensor) and app.anomaly.isolation_forest_detector (feature vectors, not
raw joint relationship) are not built to surface.

Deliberately includes the same edge-case discipline as
test_anomaly_detection_core.py: insufficient data, degenerate/collinear
input, determinism, and — the one most important test for a NEW
capability claim — direct, automated proof that a univariate detector
really does miss what this one catches, not just an assertion in prose.
"""
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.anomaly import baseline as baseline_mod
from app.anomaly import evaluation as evaluation_mod
from app.anomaly import mahalanobis_detector as md
from app.anomaly import multivariate_synthetic_scenarios as mvs
from app.anomaly import robust_zscore

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _correlated_series(n=300, *, slope=0.6, intercept=10.0, noise_std=0.8, amplitude=8.0, seed=1, step_minutes=5):
    rng = np.random.default_rng(seed)
    index = pd.DatetimeIndex([BASE + timedelta(minutes=step_minutes * i) for i in range(n)], tz="UTC")
    hours = np.array([(ts - index[0]).total_seconds() / 3600.0 for ts in index])
    a = 50.0 + amplitude * np.sin(2 * np.pi * hours / 24.0) + rng.normal(0, 0.5, size=n)
    b = intercept + slope * a + rng.normal(0, noise_std, size=n)
    return {"a": pd.Series(a, index=index, dtype="float64"), "b": pd.Series(b, index=index, dtype="float64")}


# ---------------------------------------------------------------------------
# app.anomaly.mahalanobis_detector — training/reference validation
# ---------------------------------------------------------------------------


def test_build_reference_summary_rejects_too_few_rows():
    series = _correlated_series(n=20)
    with pytest.raises(md.InsufficientReferenceDataError):
        md.build_reference_summary(
            series_by_sensor=series, sensor_ids=["a", "b"],
            reference_start=series["a"].index[0], reference_end=series["a"].index[-1], step_seconds=300,
        )


def test_build_reference_summary_enforces_samples_per_dimension_not_just_the_flat_floor():
    # 4 sensors (<= MAX_SENSORS) * MIN_SAMPLES_PER_DIMENSION(10) = 40 > MIN_REFERENCE_ROWS(30),
    # so the dimensionality rule — not the flat floor — must be the binding constraint here.
    n = 32  # above the flat floor(30) but below what 4 sensors require(40)
    series = {f"s{i}": _correlated_series(n=n, seed=i)["a"] for i in range(4)}
    with pytest.raises(md.InsufficientReferenceDataError) as exc_info:
        md.build_reference_summary(
            series_by_sensor=series, sensor_ids=list(series.keys()),
            reference_start=next(iter(series.values())).index[0], reference_end=next(iter(series.values())).index[-1], step_seconds=300,
        )
    assert exc_info.value.required_rows == 40  # 4 sensors * MIN_SAMPLES_PER_DIMENSION(10), not the flat floor


def test_too_many_sensors_rejected_before_any_fit_attempted():
    series = {f"s{i}": _correlated_series(n=100, seed=i)["a"] for i in range(md.MAX_SENSORS + 1)}
    with pytest.raises(md.TooManySensorsError):
        md.build_reference_summary(
            series_by_sensor=series, sensor_ids=list(series.keys()),
            reference_start=next(iter(series.values())).index[0], reference_end=next(iter(series.values())).index[-1], step_seconds=300,
        )


def test_build_reference_summary_reports_univariate_stats_per_sensor():
    series = _correlated_series(n=200)
    summary = md.build_reference_summary(
        series_by_sensor=series, sensor_ids=["a", "b"], reference_start=series["a"].index[0], reference_end=series["a"].index[150], step_seconds=300,
    )
    assert set(summary.univariate_robust_stats.keys()) == {"a", "b"}
    assert summary.reference_row_count > 0
    assert summary.reference_dropped_row_count == 0


# ---------------------------------------------------------------------------
# app.anomaly.mahalanobis_detector — detection / scoring
# ---------------------------------------------------------------------------


def test_detect_flags_nothing_on_a_well_behaved_relationship():
    """No injected breakdown anywhere — a correctly-fit detector on
    genuinely stable data should flag at most a small handful of points at
    a conservative threshold, never a large fraction of the scoring
    window. (A regression guard against the exact bug found and fixed
    during its own development: an under-varying sensor_a made
    the fit meaningless and over-flagged everything.)

    Uses amplitude=0 (no daily-cycle component) deliberately: with only
    ~400 points at 5-minute steps (~33 hours), a reference window shorter
    than one full 24h cycle would not see the same phase range the scoring
    window does, which would flag genuinely novel (but still relationship-
    consistent) phase values — a reference-coverage issue, not a
    relationship breakdown. Holding sensor_a stationary isolates the
    property this test actually checks: a STABLE joint relationship
    should not be flagged."""
    series = _correlated_series(n=400, seed=5, amplitude=0.0)
    ref_end_idx = 200
    result = md.detect(
        series_by_sensor=series, sensor_ids=["a", "b"],
        reference_start=series["a"].index[0], reference_end=series["a"].index[ref_end_idx],
        scoring_start=series["a"].index[ref_end_idx + 1], scoring_end=series["a"].index[-1],
        step_seconds=300, threshold=md.DEFAULT_THRESHOLD,
    )
    assert result["points_scored"] > 100
    assert result["points_flagged"] < result["points_scored"] * 0.05  # well under 5% at the default threshold


def test_detect_merges_consecutive_flagged_points_into_one_event():
    scenario = mvs.build_correlation_breakdown_scenario(start=BASE, seed=9000)
    split = mvs.split_reference_and_scoring(scenario.series_by_sensor)
    result = md.detect(
        series_by_sensor=scenario.series_by_sensor, sensor_ids=["sensor_a", "sensor_b"],
        reference_start=split.reference_start, reference_end=split.reference_end,
        scoring_start=split.scoring_start, scoring_end=split.scoring_end,
        step_seconds=300, threshold=md.DEFAULT_THRESHOLD,
    )
    assert result["points_flagged"] > 1  # multiple raw points were flagged...
    assert len(result["candidates"]) == 1  # ...but merged into a single event, not one row per point
    assert result["candidates"][0]["point_count"] == result["points_flagged"]


def test_detect_is_deterministic_across_repeated_calls():
    series = _correlated_series(n=300, seed=7)
    kwargs = dict(
        series_by_sensor=series, sensor_ids=["a", "b"],
        reference_start=series["a"].index[0], reference_end=series["a"].index[150],
        scoring_start=series["a"].index[151], scoring_end=series["a"].index[-1],
        step_seconds=300, threshold=md.DEFAULT_THRESHOLD,
    )
    first = md.detect(**kwargs)
    second = md.detect(**kwargs)
    assert [c["raw_score"] for c in first["candidates"]] == [c["raw_score"] for c in second["candidates"]]


def test_detect_handles_near_collinear_sensors_without_crashing():
    """Two sensors that are nearly perfectly collinear push the covariance
    matrix toward singular. MinCovDet must not crash the request; if it
    can still produce a fit, detection should complete normally."""
    rng = np.random.default_rng(3)
    n = 200
    index = pd.DatetimeIndex([BASE + timedelta(minutes=5 * i) for i in range(n)], tz="UTC")
    a = pd.Series(50.0 + rng.normal(0, 1.0, size=n), index=index)
    b = pd.Series(a.values * 2.0 + 1e-9, index=index)  # near-exact linear dependence
    series = {"a": a, "b": b}
    result = md.detect(
        series_by_sensor=series, sensor_ids=["a", "b"],
        reference_start=index[0], reference_end=index[100],
        scoring_start=index[101], scoring_end=index[-1],
        step_seconds=300, threshold=md.DEFAULT_THRESHOLD,
    )
    assert result["points_scored"] > 0


def test_detect_reports_no_scoring_data_honestly_rather_than_failing_silently():
    series = _correlated_series(n=200)
    result = md.detect(
        series_by_sensor=series, sensor_ids=["a", "b"],
        reference_start=series["a"].index[0], reference_end=series["a"].index[150],
        scoring_start=series["a"].index[-1] + timedelta(days=5),
        scoring_end=series["a"].index[-1] + timedelta(days=6),
        step_seconds=300, threshold=md.DEFAULT_THRESHOLD,
    )
    assert result["points_scored"] == 0
    assert result["note"] is not None


# ---------------------------------------------------------------------------
# The core new-capability claim: a univariate detector cannot catch this,
# the multivariate one can. Asserted directly, not just described.
# ---------------------------------------------------------------------------


def test_mahalanobis_catches_what_univariate_zscore_cannot():
    scenario = mvs.build_correlation_breakdown_scenario(start=BASE, seed=9000)
    split = mvs.split_reference_and_scoring(scenario.series_by_sensor)

    mv_result = md.detect(
        series_by_sensor=scenario.series_by_sensor, sensor_ids=["sensor_a", "sensor_b"],
        reference_start=split.reference_start, reference_end=split.reference_end,
        scoring_start=split.scoring_start, scoring_end=split.scoring_end,
        step_seconds=300, threshold=md.DEFAULT_THRESHOLD,
    )
    mv_metrics = evaluation_mod.match_candidates_to_ground_truth(mv_result["candidates"], scenario.ground_truth)
    assert mv_metrics.recall == 1.0
    assert mv_metrics.precision == 1.0
    assert mv_result["candidates"][0]["explanation"]["individually_unremarkable"] is True

    for sensor_id in ("sensor_a", "sensor_b"):
        baseline = baseline_mod.compute_baseline(
            split.series_by_sensor_reference[sensor_id], reference_start=split.reference_start, reference_end=split.reference_end,
        )
        uni_result = robust_zscore.detect(split.series_by_sensor_scoring[sensor_id], baseline, threshold=3.5)
        uni_metrics = evaluation_mod.match_candidates_to_ground_truth(uni_result["candidates"], scenario.ground_truth)
        assert uni_metrics.recall == 0.0, f"univariate z-score on {sensor_id} alone should NOT recover this relationship-level anomaly"


# ---------------------------------------------------------------------------
# app.anomaly.multivariate_synthetic_scenarios
# ---------------------------------------------------------------------------


def test_multivariate_scenario_generation_is_deterministic_for_a_fixed_seed():
    s1 = mvs.build_correlation_breakdown_scenario(start=BASE, seed=42)
    s2 = mvs.build_correlation_breakdown_scenario(start=BASE, seed=42)
    assert (s1.series_by_sensor["sensor_a"] == s2.series_by_sensor["sensor_a"]).all()
    assert (s1.series_by_sensor["sensor_b"] == s2.series_by_sensor["sensor_b"]).all()
    assert s1.ground_truth[0].to_dict() == s2.ground_truth[0].to_dict()


def test_multivariate_scenario_different_seeds_produce_different_series():
    s1 = mvs.build_correlation_breakdown_scenario(start=BASE, seed=1)
    s2 = mvs.build_correlation_breakdown_scenario(start=BASE, seed=2)
    assert not (s1.series_by_sensor["sensor_b"] == s2.series_by_sensor["sensor_b"]).all()


def test_multivariate_scenario_ground_truth_stays_after_the_reference_fraction():
    scenario = mvs.build_correlation_breakdown_scenario(start=BASE, seed=9000)
    split = mvs.split_reference_and_scoring(scenario.series_by_sensor)
    gt_start = datetime.fromisoformat(scenario.ground_truth[0].start)
    assert gt_start >= split.reference_end


def test_unknown_multivariate_scenario_name_raises():
    with pytest.raises(ValueError):
        mvs.build_multivariate_scenario("does-not-exist", start=BASE, seed=1)
