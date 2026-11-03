"""Tests for app.timeseries.features — in particular, the causality
guarantee documented in the module docstring: `compute_feature_value`
must be blind to everything after `as_of`, no matter what the caller
passes in.
"""
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.timeseries.features import (
    MIN_FOR_DISTRIBUTION,
    FeatureCategory,
    FeatureKind,
    KIND_CATEGORY,
    WINDOWED_KINDS,
    compute_feature_value,
)

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
FUTURE_POISON_VALUE = 999_999.0


def _series_with_future_poison(n=60, *, step_minutes=5, as_of_index=40):
    """n normal points at [0, step*n) minutes, an `as_of` timestamp that
    lands exactly on point `as_of_index`, and one poisoned point one
    second after as_of with an enormous, unmistakable value."""
    index = pd.DatetimeIndex([BASE + timedelta(minutes=step_minutes * i) for i in range(n)], tz="UTC")
    values = [50.0 + (i % 7) for i in range(n)]  # bounded, mildly varying — never near FUTURE_POISON_VALUE
    as_of = index[as_of_index].to_pydatetime()
    poison_ts = as_of + timedelta(seconds=1)
    full_index = index.insert(as_of_index + 1, pd.Timestamp(poison_ts))
    full_values = values[: as_of_index + 1] + [FUTURE_POISON_VALUE] + values[as_of_index + 1 :]
    series = pd.Series(full_values, index=full_index, dtype="float64").sort_index()
    return series, as_of


@pytest.mark.parametrize("kind", list(FeatureKind))
def test_every_feature_kind_is_blind_to_data_after_as_of(kind):
    series, as_of = _series_with_future_poison()
    params = {}
    if kind == FeatureKind.QUANTILE:
        params = {"q": 0.5}
    if kind in (FeatureKind.LAG, FeatureKind.DIFF):
        params = {"lag_steps": 1}
    if kind == FeatureKind.EMA:
        params = {"span": 5}
    if kind in (FeatureKind.SPECTRAL_DOMINANT_PERIOD, FeatureKind.SPECTRAL_ENERGY):
        params = {"grid_freq_seconds": 300}

    window_seconds = 3600.0 if kind in WINDOWED_KINDS else None
    result = compute_feature_value(series, kind=kind, as_of=as_of, window_seconds=window_seconds, params=params)

    if result.computed:
        assert result.value is not None
        assert abs(result.value) < 10_000, (
            f"{kind.value} returned {result.value}, which is only possible if the poisoned "
            f"future observation ({FUTURE_POISON_VALUE}) leaked into the computation"
        )
    # window_end (or the causal cutoff for non-windowed kinds) must never be after as_of
    if result.window_end is not None:
        assert pd.Timestamp(result.window_end) <= pd.Timestamp(as_of).tz_convert("UTC")


def test_mean_excludes_observation_one_second_after_as_of():
    series, as_of = _series_with_future_poison(as_of_index=10)
    result = compute_feature_value(series, kind=FeatureKind.MEAN, as_of=as_of, window_seconds=3600 * 2)
    assert result.computed
    assert result.value < 100  # nowhere near FUTURE_POISON_VALUE


def test_lag_and_diff_use_only_prior_observations():
    series, as_of = _series_with_future_poison(as_of_index=10)
    lag = compute_feature_value(series, kind=FeatureKind.LAG, as_of=as_of, window_seconds=None, params={"lag_steps": 1})
    diff = compute_feature_value(series, kind=FeatureKind.DIFF, as_of=as_of, window_seconds=None, params={"lag_steps": 1})
    assert lag.computed and lag.value < 100
    assert diff.computed and abs(diff.value) < 100


def test_kind_category_mapping_is_exhaustive_and_domain_is_documented_empty():
    for kind in FeatureKind:
        assert kind in KIND_CATEGORY
    domain_kinds = [k for k, c in KIND_CATEGORY.items() if c == FeatureCategory.DOMAIN]
    assert domain_kinds == []  # intentional — see features.py module docstring


def test_std_requires_at_least_two_observations_in_window():
    index = pd.DatetimeIndex([BASE], tz="UTC")
    series = pd.Series([10.0], index=index, dtype="float64")
    result = compute_feature_value(series, kind=FeatureKind.STD, as_of=BASE, window_seconds=3600)
    assert result.computed is False
    assert "need >=2" in result.reason


def test_skewness_gated_below_min_for_distribution():
    n = MIN_FOR_DISTRIBUTION - 1
    index = pd.DatetimeIndex([BASE + timedelta(minutes=i) for i in range(n)], tz="UTC")
    series = pd.Series([50.0 + i for i in range(n)], index=index, dtype="float64")
    as_of = index[-1].to_pydatetime()
    result = compute_feature_value(series, kind=FeatureKind.SKEWNESS, as_of=as_of, window_seconds=3600 * 10)
    assert result.computed is False
    assert result.value is None


def test_feature_value_is_reproducible_for_identical_input():
    series, as_of = _series_with_future_poison(as_of_index=20)
    r1 = compute_feature_value(series, kind=FeatureKind.ROLLING_MEAN, as_of=as_of, window_seconds=3600)
    r2 = compute_feature_value(series, kind=FeatureKind.ROLLING_MEAN, as_of=as_of, window_seconds=3600)
    assert r1.to_dict() == r2.to_dict()


def test_as_of_exactly_on_an_observation_includes_that_observation():
    index = pd.DatetimeIndex([BASE, BASE + timedelta(minutes=5)], tz="UTC")
    series = pd.Series([10.0, 20.0], index=index, dtype="float64")
    result = compute_feature_value(series, kind=FeatureKind.MAX, as_of=BASE, window_seconds=3600)
    assert result.computed
    assert result.value == 10.0  # the point AT as_of counts; the later one must not
