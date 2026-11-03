"""Unit tests for the pure, DB-agnostic app.anomaly.* modules — baseline
construction, threshold/severity derivation, the robust z-score detector's
scoring/merge logic, and the synthetic-scenario evaluation matcher. These
run independently of tests/test_api_anomaly.py's end-to-end HTTP flow and
are what let a bug in the underlying math surface without needing a full
DB + API round trip. Deliberately includes edge cases (insufficient data,
zero variability, boundary thresholds) rather than only typical inputs.
"""
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.anomaly import baseline as baseline_mod
from app.anomaly import evaluation as evaluation_mod
from app.anomaly import robust_zscore, synthetic_scenarios, thresholds
from app.models.intelligence import AnomalySeverityTier, DetectorType

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _series(values, *, start=BASE, step_minutes=5):
    index = pd.DatetimeIndex([start + timedelta(minutes=step_minutes * i) for i in range(len(values))], tz="UTC")
    return pd.Series(values, index=index, dtype="float64")


# ---------------------------------------------------------------------------
# app.anomaly.baseline
# ---------------------------------------------------------------------------


def test_compute_baseline_rejects_too_few_observations():
    series = _series([50.0] * 10)
    with pytest.raises(baseline_mod.InsufficientReferenceDataError):
        baseline_mod.compute_baseline(series, reference_start=series.index[0], reference_end=series.index[-1])


def test_compute_baseline_median_and_mad_are_correct():
    # 30 identical values plus one outlier — median and MAD should be
    # dominated by the 30 identical values, not dragged by the outlier.
    values = [50.0] * 30 + [1000.0]
    series = _series(values)
    result = baseline_mod.compute_baseline(series, reference_start=series.index[0], reference_end=series.index[-1])
    assert result.sample_count == 31
    assert result.median == 50.0
    assert result.mad == 0.0  # median of |x - 50| is 0 for the 30 identical points
    assert result.robust_std_estimate == 0.0
    assert result.mean > 50.0  # mean, unlike median, IS dragged by the outlier — recorded for reference only


def test_compute_baseline_restricts_to_the_declared_window():
    series = _series([50.0] * 40 + [999.0] * 40)  # second half is wildly different
    window_end = series.index[39]
    result = baseline_mod.compute_baseline(series, reference_start=series.index[0], reference_end=window_end)
    assert result.sample_count == 40
    assert result.median == 50.0  # the 999.0 half must not leak into a baseline whose window ends before it


# ---------------------------------------------------------------------------
# app.anomaly.robust_zscore
# ---------------------------------------------------------------------------


def _flat_baseline(median=50.0, robust_std=1.0):
    return baseline_mod.Baseline(
        method="median_mad", reference_start=BASE.isoformat(), reference_end=BASE.isoformat(), sample_count=100,
        median=median, mad=robust_std / baseline_mod.MAD_TO_STD_FACTOR, robust_std_estimate=robust_std,
        mean=median, std=robust_std, min=median - 5, max=median + 5, computed_at=BASE.isoformat(),
    )


def test_robust_zscore_detect_warns_rather_than_divides_by_zero_on_flat_baseline():
    flat_baseline = _flat_baseline(robust_std=0.0)
    series = _series([50.0, 51.0, 200.0])
    result = robust_zscore.detect(series, flat_baseline)
    assert result["candidates"] == []
    assert result["note"] is not None and "variability" in result["note"]


def test_robust_zscore_flags_only_points_past_threshold():
    flat_baseline = _flat_baseline(median=50.0, robust_std=1.0)
    series = _series([50.0, 50.5, 200.0, 49.5, -100.0])  # indices 2 and 4 are far outliers
    result = robust_zscore.detect(series, flat_baseline, threshold=3.5)
    assert result["points_scored"] == 5
    assert result["points_flagged"] == 2


def test_robust_zscore_merges_nearby_flags_but_not_far_apart_ones():
    flat_baseline = _flat_baseline(median=50.0, robust_std=1.0)
    # two spikes 10 minutes apart (within DEFAULT_MERGE_SECONDS=1800) should
    # merge into one event; a third spike far later should stay separate.
    values = [50.0] * 41
    values[2] = 200.0
    values[4] = 200.0
    values[40] = 200.0
    series = _series(values)
    result = robust_zscore.detect(series, flat_baseline, threshold=3.5)
    assert len(result["candidates"]) == 2
    first = result["candidates"][0]
    assert first.point_count == 2  # the two nearby spikes merged
    second = result["candidates"][1]
    assert second.point_count == 1


def test_robust_zscore_signed_score_direction():
    flat_baseline = _flat_baseline(median=50.0, robust_std=1.0)
    # far apart in time (well beyond DEFAULT_MERGE_SECONDS) so they form two
    # separate events rather than merging into one with a single strongest score
    values = [50.0] * 51
    values[0] = 200.0
    values[50] = -200.0
    series = _series(values)
    result = robust_zscore.detect(series, flat_baseline, threshold=3.5)
    assert len(result["candidates"]) == 2
    scores = sorted(c.raw_score for c in result["candidates"])
    assert scores[0] < 0 < scores[1]  # below-median flag is negative, above-median is positive


# ---------------------------------------------------------------------------
# app.anomaly.thresholds
# ---------------------------------------------------------------------------


def test_derive_severity_tier_boundaries():
    config = thresholds.ThresholdConfig(threshold=10.0, candidate_multiplier=1.5, high_priority_multiplier=2.0)
    assert thresholds.derive_severity_tier(9.9, config) == AnomalySeverityTier.OBSERVED_UNUSUAL  # below threshold shouldn't normally reach here, but tier math still holds
    assert thresholds.derive_severity_tier(10.0, config) == AnomalySeverityTier.OBSERVED_UNUSUAL
    assert thresholds.derive_severity_tier(14.9, config) == AnomalySeverityTier.OBSERVED_UNUSUAL
    assert thresholds.derive_severity_tier(15.0, config) == AnomalySeverityTier.CANDIDATE_ANOMALY  # exactly at 1.5x
    assert thresholds.derive_severity_tier(19.9, config) == AnomalySeverityTier.CANDIDATE_ANOMALY
    assert thresholds.derive_severity_tier(20.0, config) == AnomalySeverityTier.HIGH_PRIORITY  # exactly at 2.0x
    assert thresholds.derive_severity_tier(-20.0, config) == AnomalySeverityTier.HIGH_PRIORITY  # magnitude, not sign


def test_derive_severity_tier_uses_magnitude_not_sign():
    config = thresholds.ThresholdConfig(threshold=5.0)
    assert thresholds.derive_severity_tier(-5.5, config) == thresholds.derive_severity_tier(5.5, config)


@pytest.mark.parametrize(
    "raw,error_fragment",
    [
        ({"threshold": 0}, "positive"),
        ({"threshold": -1}, "positive"),
        ({"candidate_multiplier": 1.0}, "candidate_multiplier"),
        ({"candidate_multiplier": 2.0, "high_priority_multiplier": 1.5}, "high_priority_multiplier"),
    ],
)
def test_validate_threshold_config_rejects_bad_input(raw, error_fragment):
    with pytest.raises(ValueError, match=error_fragment):
        thresholds.validate_threshold_config(DetectorType.ROBUST_ZSCORE, raw)


def test_validate_threshold_config_fills_in_detector_specific_defaults():
    config = thresholds.validate_threshold_config(DetectorType.ROBUST_ZSCORE, {})
    assert config.threshold == thresholds.DETECTOR_DEFAULT_THRESHOLD[DetectorType.ROBUST_ZSCORE]
    config2 = thresholds.validate_threshold_config(DetectorType.ISOLATION_FOREST, {})
    assert config2.threshold == thresholds.DETECTOR_DEFAULT_THRESHOLD[DetectorType.ISOLATION_FOREST]
    assert config.threshold != config2.threshold  # the two detector types are NOT on the same scale — see app.anomaly.scoring


# ---------------------------------------------------------------------------
# app.anomaly.evaluation — ground-truth matching
# ---------------------------------------------------------------------------


def _candidate(start, end, score=5.0):
    return {"period_start": start.isoformat(), "period_end": end.isoformat(), "raw_score": score}


def _gt(start, end, kind="point_anomaly"):
    return synthetic_scenarios.GroundTruthWindow(start=start.isoformat(), end=end.isoformat(), kind=kind, description="test")


def test_match_candidates_exact_overlap_is_a_true_positive():
    t = BASE
    metrics = evaluation_mod.match_candidates_to_ground_truth([_candidate(t, t)], [_gt(t, t)])
    assert metrics.true_positives == 1
    assert metrics.false_positives == 0
    assert metrics.false_negatives == 0
    assert metrics.precision == 1.0
    assert metrics.recall == 1.0


def test_match_candidates_respects_tolerance_window():
    t = BASE
    near = t + timedelta(minutes=10)
    far = t + timedelta(hours=5)
    metrics_near = evaluation_mod.match_candidates_to_ground_truth([_candidate(near, near)], [_gt(t, t)], tolerance_seconds=900)
    assert metrics_near.true_positives == 1

    metrics_far = evaluation_mod.match_candidates_to_ground_truth([_candidate(far, far)], [_gt(t, t)], tolerance_seconds=900)
    assert metrics_far.true_positives == 0
    assert metrics_far.false_positives == 1
    assert metrics_far.false_negatives == 1


def test_match_candidates_unmatched_ground_truth_is_a_false_negative_not_silently_dropped():
    t = BASE
    metrics = evaluation_mod.match_candidates_to_ground_truth([], [_gt(t, t), _gt(t + timedelta(hours=1), t + timedelta(hours=1))])
    assert metrics.true_positives == 0
    assert metrics.false_negatives == 2
    assert metrics.precision is None  # no candidates at all -> precision undefined, not fabricated as 0
    assert metrics.recall == 0.0


def test_match_candidates_no_ground_truth_and_no_candidates_is_undefined_not_perfect():
    metrics = evaluation_mod.match_candidates_to_ground_truth([], [])
    assert metrics.precision is None
    assert metrics.recall is None
    assert metrics.f1 is None


# ---------------------------------------------------------------------------
# app.anomaly.synthetic_scenarios — determinism
# ---------------------------------------------------------------------------


def test_synthetic_scenario_generation_is_deterministic_for_a_fixed_seed():
    s1 = synthetic_scenarios.build_scenario("point_anomaly", start=BASE, seed=42)
    s2 = synthetic_scenarios.build_scenario("point_anomaly", start=BASE, seed=42)
    assert (s1.series.values == s2.series.values).all()
    assert [g.to_dict() for g in s1.ground_truth] == [g.to_dict() for g in s2.ground_truth]


def test_synthetic_scenario_different_seeds_produce_different_series():
    s1 = synthetic_scenarios.build_scenario("point_anomaly", start=BASE, seed=1)
    s2 = synthetic_scenarios.build_scenario("point_anomaly", start=BASE, seed=2)
    assert not np.allclose(s1.series.values, s2.series.values)


def test_synthetic_scenario_ground_truth_stays_out_of_the_reference_fraction():
    # every scenario's injected anomaly must fall at or after REFERENCE_FRACTION
    # of the series, so app.anomaly.evaluation's reference/scoring split never
    # accidentally trains a baseline on the very behavior it's meant to catch.
    for name in synthetic_scenarios.ALL_SCENARIOS:
        scenario = synthetic_scenarios.build_scenario(name, start=BASE, seed=7)
        if not scenario.ground_truth:
            continue
        cutoff = scenario.series.index[int(len(scenario.series) * synthetic_scenarios.REFERENCE_FRACTION)]
        for window in scenario.ground_truth:
            assert datetime.fromisoformat(window.start) >= cutoff, f"{name} leaks its injected anomaly into the reference period"


def test_unknown_scenario_name_raises():
    with pytest.raises(ValueError):
        synthetic_scenarios.build_scenario("not_a_real_scenario", start=BASE)
