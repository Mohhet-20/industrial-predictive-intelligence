"""Deeper, parameterized temporal analysis — rolling behavior, rate of
change, volatility, and autocorrelation with an explicit periodicity scan.

This is deliberately a separate module from `profiling` (which returns one
fixed, cheap overview). Analysis here lets the caller choose a rolling
window and lag range, which the profile intentionally does not expose —
keeping the "quick overview" endpoint fast and the "dig deeper" endpoint
configurable.

Every result states its method and, where relevant, a strength/confidence
qualifier — this module never returns a bare number without saying how
it was produced or how much data backed it.
"""
from typing import Any

import numpy as np
import pandas as pd

from app.timeseries.resampling import to_regular_grid_for_analysis

MIN_FOR_AUTOCORRELATION = 20
MAX_SERIES_POINTS_RETURNED = 500


def _downsample(indexable, max_points: int = MAX_SERIES_POINTS_RETURNED):
    """Downsamples to AT MOST max_points rows. Plain floor-division step
    sizing (`len // max_points`) silently fails to bound anything when
    max_points < len <= 2*max_points (step rounds down to 1), so this uses
    ceiling division for the stride and then hard-caps the result — the
    bound in the name is an actual guarantee, not a best-effort."""
    n = len(indexable)
    if n <= max_points:
        return indexable
    step = -(-n // max_points)  # ceiling division
    return indexable.iloc[::step][:max_points]


def rolling_statistics(series: pd.Series, *, window_seconds: float) -> dict[str, Any]:
    n = len(series)
    if n < 3:
        return {"method": "time_based_rolling_window", "window_seconds": window_seconds, "points": [], "note": f"insufficient data (n={n} < 3)"}

    window = pd.Timedelta(seconds=window_seconds)
    ordered = series.sort_index()
    rolling = ordered.rolling(window, min_periods=1)
    result = pd.DataFrame({"mean": rolling.mean(), "std": rolling.std(), "min": rolling.min(), "max": rolling.max()})
    result = _downsample(result)

    points = [
        {
            "timestamp": ts.to_pydatetime().isoformat(),
            "rolling_mean": None if pd.isna(row["mean"]) else float(row["mean"]),
            "rolling_std": None if pd.isna(row["std"]) else float(row["std"]),
            "rolling_min": None if pd.isna(row["min"]) else float(row["min"]),
            "rolling_max": None if pd.isna(row["max"]) else float(row["max"]),
        }
        for ts, row in result.iterrows()
    ]
    return {"method": "time_based_rolling_window", "window_seconds": window_seconds, "points": points, "note": None}


def rate_of_change(series: pd.Series) -> dict[str, Any]:
    n = len(series)
    if n < 2:
        return {"points": [], "note": f"insufficient data (n={n} < 2)"}
    ordered = series.sort_index()
    diffs = ordered.diff()
    dt_hours = pd.Series(ordered.index).diff().dt.total_seconds().values / 3600.0
    rate = diffs.values / np.where(dt_hours == 0, np.nan, dt_hours)

    df = pd.DataFrame({"rate_per_hour": rate}, index=ordered.index).dropna()
    df = _downsample(df)
    points = [{"timestamp": ts.to_pydatetime().isoformat(), "rate_per_hour": float(v)} for ts, v in df["rate_per_hour"].items()]
    return {"points": points, "note": None}


def volatility(series: pd.Series, *, window_seconds: float) -> dict[str, Any]:
    """Rolling coefficient of variation (std/|mean|) — dimensionless, so it
    is comparable across sensors with different units and magnitudes,
    unlike raw rolling std."""
    n = len(series)
    if n < 5:
        return {"method": "rolling_coefficient_of_variation", "window_seconds": window_seconds, "points": [], "note": f"insufficient data (n={n} < 5)"}

    window = pd.Timedelta(seconds=window_seconds)
    ordered = series.sort_index()
    rolling_mean = ordered.rolling(window, min_periods=3).mean()
    rolling_std = ordered.rolling(window, min_periods=3).std()
    cv = (rolling_std / rolling_mean.abs()).replace([np.inf, -np.inf], np.nan)

    df = pd.DataFrame({"cv": cv}).dropna()
    df = _downsample(df)
    points = [{"timestamp": ts.to_pydatetime().isoformat(), "coefficient_of_variation": float(v)} for ts, v in df["cv"].items()]
    return {"method": "rolling_coefficient_of_variation", "window_seconds": window_seconds, "points": points, "note": None}


def autocorrelation(
    series: pd.Series, *, grid_freq_seconds: float, max_lag_steps: int
) -> dict[str, Any]:
    """Autocorrelation requires evenly spaced samples, which raw
    measurements rarely are. This resamples onto a regular grid first
    (dropping — never inventing — buckets with no observation) via
    `resampling.to_regular_grid_for_analysis`, and reports the resulting
    coverage so the caller can judge how much to trust the result: ACF on
    60%-coverage data is a much weaker signal than ACF on 98%-coverage data.
    """
    grid_series, coverage_info = to_regular_grid_for_analysis(series, freq_seconds=grid_freq_seconds)
    n = len(grid_series)

    if n < MIN_FOR_AUTOCORRELATION:
        return {
            "grid_freq_seconds": grid_freq_seconds,
            "coverage": coverage_info,
            "lags": [],
            "note": f"insufficient regularly-sampled data for autocorrelation (n={n} < {MIN_FOR_AUTOCORRELATION} after gridding)",
        }

    max_lag_steps = min(max_lag_steps, n // 2)
    values = grid_series.values.astype("float64")
    values = values - values.mean()
    denom = np.dot(values, values)

    lags = []
    for lag in range(1, max_lag_steps + 1):
        if denom == 0:
            corr = 0.0
        else:
            corr = float(np.dot(values[:-lag], values[lag:]) / denom)
        lags.append({"lag_steps": lag, "lag_seconds": lag * grid_freq_seconds, "correlation": corr})

    note = None
    coverage_fraction = coverage_info.get("coverage_fraction")
    if coverage_fraction is not None and coverage_fraction < 0.8:
        note = f"only {coverage_fraction:.0%} of the analysis grid had observed data — treat this ACF as indicative, not precise."

    return {"grid_freq_seconds": grid_freq_seconds, "coverage": coverage_info, "lags": lags, "note": note}
