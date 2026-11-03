"""Training dataset builder — turns stored measurements + feature
definitions + a time grid into an ML-ready feature matrix, without
temporal leakage.

## Leakage boundary

For a row anchored at `as_of`:
  - every FEATURE value is computed by `features.compute_feature_value`,
    which causally filters to `timestamp <= as_of` (see features.py's
    module docstring for the enforcement detail).
  - the TARGET (when requested) is computed from `timestamp > as_of`,
    specifically the observation nearest `as_of + horizon`. This is
    supposed to be future data — it's the label a real-time model would be
    trying to predict — but it is computed by a function that touches
    ONLY the target sensor's series filtered to strictly-after-as_of, kept
    structurally separate from the feature computation path, so a target
    value can never accidentally end up inside a feature.

`tests/test_dataset_builder.py::test_target_never_leaks_into_features`
constructs a case where the target and a feature would collide if this
separation were violated, and asserts they don't.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

import numpy as np
import pandas as pd

from app.timeseries.features import FeatureKind, compute_feature_value

MAX_DATASET_ROWS_SYNC = 3000
MAX_DATASET_ROWS_TOTAL = 50_000


@dataclass
class FeatureSpec:
    feature_name: str
    sensor_id: str
    kind: FeatureKind
    window_seconds: Optional[float]
    params: dict[str, Any]


@dataclass
class TargetSpec:
    sensor_id: str
    horizon_seconds: float
    tolerance_seconds: float


def grid_row_count(start: datetime, end: datetime, step_seconds: float) -> int:
    """Row count a build with these bounds would produce, without
    materializing the grid — used to decide sync-vs-async and to validate
    against MAX_DATASET_ROWS_TOTAL before doing any actual work."""
    if step_seconds <= 0:
        raise ValueError("step_seconds must be positive")
    if end <= start:
        raise ValueError("end must be after start")
    return int((end - start).total_seconds() / step_seconds) + 1


def _generate_grid(start: datetime, end: datetime, step_seconds: float) -> list[pd.Timestamp]:
    start_ts = pd.Timestamp(start).tz_convert("UTC") if pd.Timestamp(start).tzinfo else pd.Timestamp(start).tz_localize("UTC")
    end_ts = pd.Timestamp(end).tz_convert("UTC") if pd.Timestamp(end).tzinfo else pd.Timestamp(end).tz_localize("UTC")
    step = pd.Timedelta(seconds=step_seconds)
    n = int((end_ts - start_ts) / step) + 1
    return [start_ts + i * step for i in range(n)]


def _compute_target(series: pd.Series, *, as_of: pd.Timestamp, horizon_seconds: float, tolerance_seconds: float) -> tuple[Optional[float], bool, Optional[str]]:
    """Looks ONLY at observations strictly after as_of — this function
    never sees, and cannot see, the causally-filtered series used for
    features (they are different pandas objects passed independently by
    the caller)."""
    target_time = as_of + timedelta(seconds=horizon_seconds)
    future = series[series.index > as_of]
    if future.empty:
        return None, False, "no future observations available"
    candidate_idx = int(np.argmin(np.abs((future.index - target_time).values)))
    candidate_time = future.index[candidate_idx]
    if abs((candidate_time - target_time).total_seconds()) > tolerance_seconds:
        return None, False, f"no observation within {tolerance_seconds}s of target horizon"
    return float(future.iloc[candidate_idx]), True, None


def build_feature_matrix(
    *,
    series_by_sensor: dict[str, pd.Series],
    feature_specs: list[FeatureSpec],
    grid_start: datetime,
    grid_end: datetime,
    step_seconds: float,
    target_spec: Optional[TargetSpec] = None,
    limit: int,
    offset: int = 0,
) -> dict[str, Any]:
    full_grid = _generate_grid(grid_start, grid_end, step_seconds)
    total_rows = len(full_grid)
    if total_rows > MAX_DATASET_ROWS_TOTAL:
        raise ValueError(f"Grid would produce {total_rows} rows (> {MAX_DATASET_ROWS_TOTAL}). Narrow the range or widen step_seconds.")

    page = full_grid[offset : offset + limit]

    rows: list[dict[str, Any]] = []
    total_feature_values = 0
    null_feature_values = 0

    for as_of in page:
        feature_values: dict[str, Any] = {}
        for spec in feature_specs:
            series = series_by_sensor.get(spec.sensor_id)
            if series is None:
                feature_values[spec.feature_name] = {"value": None, "computed": False, "reason": "sensor series not loaded"}
                total_feature_values += 1
                null_feature_values += 1
                continue
            fr = compute_feature_value(series, kind=spec.kind, as_of=as_of.to_pydatetime(), window_seconds=spec.window_seconds, params=spec.params)
            feature_values[spec.feature_name] = {"value": fr.value, "computed": fr.computed, "reason": fr.reason}
            total_feature_values += 1
            if not fr.computed:
                null_feature_values += 1

        row: dict[str, Any] = {"as_of": as_of.to_pydatetime().isoformat(), "features": feature_values}

        if target_spec:
            target_series = series_by_sensor.get(target_spec.sensor_id)
            if target_series is None:
                row["target"] = {"value": None, "computed": False, "reason": "target sensor series not loaded"}
            else:
                value, computed, reason = _compute_target(
                    target_series, as_of=as_of, horizon_seconds=target_spec.horizon_seconds, tolerance_seconds=target_spec.tolerance_seconds
                )
                row["target"] = {"value": value, "computed": computed, "reason": reason}

        rows.append(row)

    return {
        "rows": rows,
        "row_count_total": total_rows,
        "row_count_returned": len(rows),
        "offset": offset,
        "limit": limit,
        "null_feature_rate": (null_feature_values / total_feature_values) if total_feature_values else None,
        "leakage_policy": (
            "Every feature value uses only observations at or before its row's as_of timestamp "
            "(and, for windowed features, no earlier than as_of - window). Target values (when "
            "requested) use only observations strictly after as_of. See docs/TEMPORAL_INTELLIGENCE.md."
        ),
    }
