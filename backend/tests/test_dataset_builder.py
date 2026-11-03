from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.timeseries.dataset_builder import (
    MAX_DATASET_ROWS_TOTAL,
    FeatureSpec,
    TargetSpec,
    build_feature_matrix,
    grid_row_count,
)
from app.timeseries.features import FeatureKind

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
FUTURE_POISON_VALUE = 999_999.0


def _clean_series(n=120, step_minutes=5, start_value=50.0):
    index = pd.DatetimeIndex([BASE + timedelta(minutes=step_minutes * i) for i in range(n)], tz="UTC")
    return pd.Series([start_value + (i % 5) for i in range(n)], index=index, dtype="float64")


def test_target_never_leaks_into_features():
    """Constructs a case where the target sensor and a feature sensor are
    literally the SAME series. If the causal-filter/target-filter
    separation were ever violated (e.g. a shared unfiltered reference
    leaking through), the feature for a row would end up seeing the value
    the target is supposed to predict. This asserts it never does, using a
    distinctive, unmistakable planted value at the target horizon."""
    n = 40
    index = pd.DatetimeIndex([BASE + timedelta(minutes=5 * i) for i in range(n)], tz="UTC")
    values = [50.0] * n
    series = pd.Series(values, index=index, dtype="float64")
    series_by_sensor = {"sensor-a": series}

    grid_start = index[10].to_pydatetime()
    grid_end = index[10].to_pydatetime()  # single row
    step_seconds = 300

    # Plant a poisoned value at the future timestamp the target for this
    # single row will resolve to (as_of + horizon).
    horizon_seconds = 600  # 2 steps ahead
    poison_ts = pd.Timestamp(grid_start) + timedelta(seconds=horizon_seconds)
    poisoned = series.copy()
    if poison_ts in poisoned.index:
        poisoned.loc[poison_ts] = FUTURE_POISON_VALUE
    else:
        poisoned = pd.concat([poisoned, pd.Series([FUTURE_POISON_VALUE], index=pd.DatetimeIndex([poison_ts]))]).sort_index()
    series_by_sensor = {"sensor-a": poisoned}

    feature_specs = [FeatureSpec(feature_name="mean_1h", sensor_id="sensor-a", kind=FeatureKind.MEAN, window_seconds=3600, params={})]
    target_spec = TargetSpec(sensor_id="sensor-a", horizon_seconds=horizon_seconds, tolerance_seconds=60)

    result = build_feature_matrix(
        series_by_sensor=series_by_sensor,
        feature_specs=feature_specs,
        grid_start=grid_start,
        grid_end=grid_end,
        step_seconds=step_seconds,
        target_spec=target_spec,
        limit=10,
        offset=0,
    )

    row = result["rows"][0]
    # The target SHOULD see the poisoned future value — that's its job.
    assert row["target"]["computed"] is True
    assert row["target"]["value"] == FUTURE_POISON_VALUE
    # The feature must NOT — it only sees observations at/before as_of.
    assert row["features"]["mean_1h"]["value"] == 50.0


def test_grid_row_count_matches_generated_rows():
    start = BASE
    end = BASE + timedelta(hours=1)
    count = grid_row_count(start, end, step_seconds=300)
    series = _clean_series(n=20)
    result = build_feature_matrix(
        series_by_sensor={"s": series},
        feature_specs=[FeatureSpec("m", "s", FeatureKind.MEAN, 3600, {})],
        grid_start=start,
        grid_end=end,
        step_seconds=300,
        limit=count,
        offset=0,
    )
    assert result["row_count_total"] == count
    assert result["row_count_returned"] == count


def test_grid_row_count_rejects_non_positive_step():
    with pytest.raises(ValueError):
        grid_row_count(BASE, BASE + timedelta(hours=1), step_seconds=0)


def test_pagination_offset_and_limit_are_honored():
    series = _clean_series(n=50)
    grid_start = BASE
    grid_end = BASE + timedelta(hours=2)
    total = grid_row_count(grid_start, grid_end, step_seconds=300)
    specs = [FeatureSpec("m", "s", FeatureKind.MEAN, 3600, {})]

    page1 = build_feature_matrix(series_by_sensor={"s": series}, feature_specs=specs, grid_start=grid_start, grid_end=grid_end, step_seconds=300, limit=3, offset=0)
    page2 = build_feature_matrix(series_by_sensor={"s": series}, feature_specs=specs, grid_start=grid_start, grid_end=grid_end, step_seconds=300, limit=3, offset=3)

    assert page1["row_count_total"] == total == page2["row_count_total"]
    assert len(page1["rows"]) == 3
    assert page1["rows"][0]["as_of"] != page2["rows"][0]["as_of"]


def test_missing_sensor_series_produces_null_feature_not_a_crash():
    result = build_feature_matrix(
        series_by_sensor={},  # sensor referenced by the spec was never loaded
        feature_specs=[FeatureSpec("m", "missing-sensor", FeatureKind.MEAN, 3600, {})],
        grid_start=BASE,
        grid_end=BASE + timedelta(minutes=10),
        step_seconds=300,
        limit=10,
        offset=0,
    )
    assert result["null_feature_rate"] == 1.0
    assert result["rows"][0]["features"]["m"]["computed"] is False


def test_no_future_observation_within_tolerance_reports_target_not_computed():
    series = _clean_series(n=5, step_minutes=5)  # only ~20 minutes of data
    result = build_feature_matrix(
        series_by_sensor={"s": series},
        feature_specs=[FeatureSpec("m", "s", FeatureKind.MEAN, 3600, {})],
        grid_start=BASE,
        grid_end=BASE,
        step_seconds=300,
        target_spec=TargetSpec(sensor_id="s", horizon_seconds=3600 * 5, tolerance_seconds=60),  # far beyond available data
        limit=5,
        offset=0,
    )
    assert result["rows"][0]["target"]["computed"] is False
    assert result["rows"][0]["target"]["value"] is None


def test_build_feature_matrix_rejects_grid_over_the_total_row_ceiling():
    huge_end = BASE + timedelta(seconds=(MAX_DATASET_ROWS_TOTAL + 10))
    with pytest.raises(ValueError):
        build_feature_matrix(
            series_by_sensor={"s": _clean_series(n=5)},
            feature_specs=[FeatureSpec("m", "s", FeatureKind.MEAN, 3600, {})],
            grid_start=BASE,
            grid_end=huge_end,
            step_seconds=1,
            limit=10,
            offset=0,
        )


def test_leakage_policy_string_is_present_and_describes_the_boundary():
    series = _clean_series(n=10)
    result = build_feature_matrix(
        series_by_sensor={"s": series},
        feature_specs=[FeatureSpec("m", "s", FeatureKind.MEAN, 3600, {})],
        grid_start=BASE,
        grid_end=BASE + timedelta(minutes=10),
        step_seconds=300,
        limit=5,
        offset=0,
    )
    assert "at or before" in result["leakage_policy"]
    assert "strictly after" in result["leakage_policy"]
