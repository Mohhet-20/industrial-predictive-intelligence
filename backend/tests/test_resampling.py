from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.timeseries.resampling import (
    MAX_BUCKETS,
    AggregationMethod,
    InterpolationPolicy,
    TooManyBucketsError,
    resample_series,
    to_regular_grid_for_analysis,
)

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _series(pairs):
    """pairs: list of (minutes_offset, value)"""
    index = pd.DatetimeIndex([BASE + timedelta(minutes=m) for m, _ in pairs], tz="UTC")
    return pd.Series([v for _, v in pairs], index=index, dtype="float64")


def test_buckets_anchored_at_explicit_start_not_a_calendar_boundary():
    series = _series([(1, 10.0), (6, 20.0)])
    buckets = resample_series(series, start=BASE, end=BASE + timedelta(minutes=10), freq_seconds=300, aggregation=AggregationMethod.MEAN)
    assert buckets[0]["bucket_start"] == BASE.isoformat()
    # bucket 0 = [0,5) min -> observation at minute 1 only
    assert buckets[0]["value"] == 10.0
    assert buckets[0]["is_observed"] is True
    # bucket 1 = [5,10) min -> observation at minute 6
    assert buckets[1]["value"] == 20.0


def test_empty_bucket_stays_none_under_default_no_interpolation():
    series = _series([(1, 10.0), (16, 20.0)])  # gap in the middle
    buckets = resample_series(series, start=BASE, end=BASE + timedelta(minutes=20), freq_seconds=300)
    middle = buckets[1]  # [5,10) min: no observation
    assert middle["value"] is None
    assert middle["is_observed"] is False
    assert middle["is_interpolated"] is False


def test_empty_series_resamples_to_all_missing_never_raises():
    series = pd.Series(dtype="float64", index=pd.DatetimeIndex([], tz="UTC"))
    buckets = resample_series(series, start=BASE, end=BASE + timedelta(minutes=15), freq_seconds=300)
    assert len(buckets) == 4
    assert all(b["value"] is None and not b["is_observed"] for b in buckets)


def test_linear_interpolation_fills_and_flags_gaps_explicitly():
    series = _series([(1, 0.0), (16, 30.0)])  # buckets 0 and 3 observed; 1,2 missing
    buckets = resample_series(series, start=BASE, end=BASE + timedelta(minutes=20), freq_seconds=300, interpolation=InterpolationPolicy.LINEAR)
    assert buckets[0]["is_observed"] is True and buckets[0]["is_interpolated"] is False
    assert buckets[3]["is_observed"] is True
    # middle buckets get a filled value that is explicitly flagged as interpolated, never silently reported as observed
    for mid in buckets[1:3]:
        assert mid["value"] is not None
        assert mid["is_interpolated"] is True
        assert mid["is_observed"] is False


def test_forward_fill_carries_last_value_and_flags_it():
    series = _series([(1, 5.0)])
    buckets = resample_series(series, start=BASE, end=BASE + timedelta(minutes=15), freq_seconds=300, interpolation=InterpolationPolicy.FORWARD_FILL)
    assert buckets[0]["value"] == 5.0 and buckets[0]["is_observed"] is True
    assert buckets[1]["value"] == 5.0 and buckets[1]["is_interpolated"] is True and buckets[1]["is_observed"] is False


def test_too_many_buckets_raises_instead_of_oom():
    with pytest.raises(TooManyBucketsError):
        resample_series(pd.Series(dtype="float64"), start=BASE, end=BASE + timedelta(seconds=(MAX_BUCKETS + 10)), freq_seconds=1)


def test_out_of_order_input_is_still_bucketed_correctly():
    # deliberately construct an out-of-order index
    index = pd.DatetimeIndex([BASE + timedelta(minutes=6), BASE + timedelta(minutes=1)], tz="UTC")
    series = pd.Series([20.0, 10.0], index=index, dtype="float64")
    buckets = resample_series(series, start=BASE, end=BASE + timedelta(minutes=10), freq_seconds=300, aggregation=AggregationMethod.MEAN)
    assert buckets[0]["value"] == 10.0
    assert buckets[1]["value"] == 20.0


def test_to_regular_grid_drops_missing_buckets_and_reports_coverage():
    # start=BASE, end=BASE+15min, freq=5min -> buckets [0,5)[5,10)[10,15)[15,20): 4 buckets total
    series = _series([(1, 10.0), (16, 20.0)])  # bucket 0 (minute 1) and bucket 3 (minute 16) observed
    grid, info = to_regular_grid_for_analysis(series, freq_seconds=300, start=BASE, end=BASE + timedelta(minutes=15))
    assert len(grid) == 2  # only the two observed buckets remain
    assert info["buckets_total"] == 4
    assert info["buckets_observed"] == 2
    assert info["gaps_dropped"] == 2
    assert info["coverage_fraction"] == pytest.approx(2 / 4)


def test_window_boundary_observation_lands_in_correct_bucket():
    # An observation exactly at a bucket boundary belongs to the bucket that starts there.
    series = _series([(5, 99.0)])
    buckets = resample_series(series, start=BASE, end=BASE + timedelta(minutes=10), freq_seconds=300)
    assert buckets[0]["value"] is None  # [0,5) does not include minute 5
    assert buckets[1]["value"] == 99.0  # [5,10) does
