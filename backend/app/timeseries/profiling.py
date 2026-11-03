"""Time-series profiling engine.

Computes a bounded, explainable summary of a sensor's behavior over a
requested time range from ACTUAL stored measurements — no fabricated
statistics, no metric computed on too little data without saying so.

Every threshold below is deliberate and documented: this module refuses to
report skewness on 5 points or a trend line on 3 points. When data is
insufficient for a given statistic, the field is `None` and a warning
explains why, rather than a class="looks like ML" number backed by noise.
"""
from datetime import datetime, timedelta
from typing import Any, Optional

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

# Minimum observations required before a given statistic is computed at all.
MIN_FOR_BASIC_STATS = 2
MIN_FOR_DISTRIBUTION = 30
MIN_FOR_TREND = 10
MIN_FOR_ROLLING = 10
MIN_FOR_AUTOCORRELATION = 20

GAP_FACTOR = 3.0
MAX_GAPS_REPORTED = 20
MAX_ROLLING_POINTS = 200


def _iso(ts: Optional[pd.Timestamp]) -> Optional[str]:
    if ts is None or pd.isna(ts):
        return None
    return ts.to_pydatetime().isoformat()


def _quantiles(series: pd.Series) -> dict[str, float]:
    qs = series.quantile([0.10, 0.25, 0.50, 0.75, 0.90])
    return {"p10": float(qs[0.10]), "p25": float(qs[0.25]), "p50": float(qs[0.50]), "p75": float(qs[0.75]), "p90": float(qs[0.90])}


def _sampling_interval_stats(index: pd.DatetimeIndex) -> dict[str, Optional[float]]:
    if len(index) < 2:
        return {"median_observed_seconds": None, "mean_observed_seconds": None, "std_observed_seconds": None, "irregularity_ratio": None}
    diffs = np.diff(index.values).astype("timedelta64[ns]").astype("float64") / 1e9
    mean = float(np.mean(diffs))
    std = float(np.std(diffs, ddof=1)) if len(diffs) > 1 else 0.0
    return {
        "median_observed_seconds": float(np.median(diffs)),
        "mean_observed_seconds": mean,
        "std_observed_seconds": std,
        # coefficient of variation of the inter-sample interval: 0 = perfectly
        # regular cadence, higher = increasingly irregular
        "irregularity_ratio": (std / mean) if mean > 0 else None,
    }


def _find_gaps(index: pd.DatetimeIndex, expected_interval_seconds: Optional[float]) -> list[dict[str, Any]]:
    if expected_interval_seconds is None or len(index) < 2:
        return []
    threshold = timedelta(seconds=expected_interval_seconds * GAP_FACTOR)
    gaps = []
    for prev, nxt in zip(index[:-1], index[1:]):
        gap = (nxt - prev).to_pytimedelta()
        if gap > threshold:
            gaps.append(
                {
                    "start": _iso(prev),
                    "end": _iso(nxt),
                    "duration_seconds": gap.total_seconds(),
                }
            )
    gaps.sort(key=lambda g: g["duration_seconds"], reverse=True)
    return gaps[:MAX_GAPS_REPORTED]


def _trend(series: pd.Series) -> dict[str, Any]:
    n = len(series)
    if n < MIN_FOR_TREND:
        return {
            "method": "ordinary_least_squares",
            "slope_per_hour": None,
            "r_squared": None,
            "p_value": None,
            "direction": None,
            "strength": None,
            "note": f"insufficient data for trend estimation (n={n} < {MIN_FOR_TREND})",
        }
    t0 = series.index[0]
    x_hours = np.array([(ts - t0).total_seconds() / 3600.0 for ts in series.index])
    y = series.values.astype("float64")
    slope, intercept, r_value, p_value, std_err = scipy_stats.linregress(x_hours, y)
    r_squared = float(r_value**2)

    if p_value > 0.05 or r_squared < 0.1:
        direction = "no_significant_trend"
        strength = "weak"
    else:
        direction = "increasing" if slope > 0 else "decreasing"
        strength = "strong" if r_squared >= 0.5 else "moderate"

    return {
        "method": "ordinary_least_squares",
        "slope_per_hour": float(slope),
        "r_squared": r_squared,
        "p_value": float(p_value),
        "direction": direction,
        "strength": strength,
        "note": (
            "Linear trend over the requested window only — not a forecast, and a "
            "low R² means the line explains little of the variation even when p < 0.05."
        ),
    }


def _distribution(series: pd.Series) -> dict[str, Any]:
    n = len(series)
    if n < MIN_FOR_DISTRIBUTION:
        return {"skewness": None, "kurtosis": None, "note": f"insufficient data for distribution shape (n={n} < {MIN_FOR_DISTRIBUTION})"}
    return {
        "skewness": float(scipy_stats.skew(series.values)),
        "kurtosis": float(scipy_stats.kurtosis(series.values)),  # excess kurtosis (0 = normal-like tails)
        "note": None,
    }


def _rolling_behavior(series: pd.Series, expected_interval_seconds: Optional[float]) -> Optional[dict[str, Any]]:
    n = len(series)
    if n < MIN_FOR_ROLLING:
        return None

    # Pick a rolling window covering roughly 1/10th of the series (at least
    # 5 samples), so "rolling behavior" means something at any density.
    window = max(5, n // 10)
    rolling_mean = series.rolling(window=window, min_periods=max(2, window // 2)).mean()
    rolling_std = series.rolling(window=window, min_periods=max(2, window // 2)).std()

    combined = pd.DataFrame({"mean": rolling_mean, "std": rolling_std}).dropna(how="all")
    if combined.empty:
        return None

    # Downsample for transport — the UI needs a shape, not every point.
    # Ceiling division for the stride (not floor) plus a hard cap: floor
    # division rounds to a step of 1 (no-op) whenever
    # MAX_ROLLING_POINTS < len <= 2*MAX_ROLLING_POINTS, silently failing to
    # bound the output in that range.
    if len(combined) > MAX_ROLLING_POINTS:
        step = -(-len(combined) // MAX_ROLLING_POINTS)
        combined = combined.iloc[::step][:MAX_ROLLING_POINTS]

    points = [
        {
            "timestamp": _iso(ts),
            "rolling_mean": None if pd.isna(row["mean"]) else float(row["mean"]),
            "rolling_std": None if pd.isna(row["std"]) else float(row["std"]),
        }
        for ts, row in combined.iterrows()
    ]
    return {"window_observation_count": window, "points": points}


def _change_rate(series: pd.Series) -> Optional[dict[str, Any]]:
    if len(series) < 2:
        return None
    diffs = series.diff().dropna()
    time_diffs_hours = pd.Series(series.index).diff().dt.total_seconds().dropna() / 3600.0
    if diffs.empty:
        return None
    mean_abs_diff = float(diffs.abs().mean())
    rate_per_hour = None
    nonzero = time_diffs_hours[time_diffs_hours > 0]
    if not nonzero.empty:
        per_hour_rates = diffs.abs().values[: len(nonzero)] / nonzero.values
        rate_per_hour = float(np.mean(per_hour_rates))
    return {"mean_absolute_step_change": mean_abs_diff, "mean_absolute_change_per_hour": rate_per_hour}


def compute_profile(
    series: pd.Series,
    *,
    sensor_unit: str,
    expected_interval_seconds: Optional[float],
    requested_start: datetime,
    requested_end: datetime,
) -> dict[str, Any]:
    n = len(series)
    warnings: list[str] = []

    profile: dict[str, Any] = {
        "requested_range": {"start": requested_start.isoformat(), "end": requested_end.isoformat()},
        "unit": sensor_unit,
        "observation_count": n,
        "insufficient_data": n < MIN_FOR_BASIC_STATS,
        "insufficient_data_reason": None,
        "observed_range": None,
        "sampling_interval": None,
        "missingness": None,
        "gaps": [],
        "gap_count": 0,
        "descriptive_stats": None,
        "distribution": None,
        "trend": None,
        "rolling_behavior": None,
        "change_rate": None,
        "data_quality_warnings": warnings,
    }

    if n == 0:
        profile["insufficient_data_reason"] = "No measurements were found in the requested time range."
        return profile
    if n < MIN_FOR_BASIC_STATS:
        profile["insufficient_data_reason"] = f"Only {n} observation(s) in range; at least {MIN_FOR_BASIC_STATS} are required for any statistic."
        profile["observed_range"] = {"first_timestamp": _iso(series.index[0]), "last_timestamp": _iso(series.index[-1])}
        return profile

    profile["observed_range"] = {"first_timestamp": _iso(series.index[0]), "last_timestamp": _iso(series.index[-1])}
    profile["sampling_interval"] = {"expected_seconds": expected_interval_seconds, **_sampling_interval_stats(series.index)}

    if expected_interval_seconds:
        range_seconds = (requested_end - requested_start).total_seconds()
        expected_count = int(range_seconds // expected_interval_seconds)
        missing = max(expected_count - n, 0)
        profile["missingness"] = {
            "expected_count": expected_count,
            "missing_count": missing,
            "missing_fraction": (missing / expected_count) if expected_count > 0 else None,
        }
        if expected_count > 0 and missing / expected_count > 0.2:
            warnings.append(f"{missing} of ~{expected_count} expected readings are missing ({missing / expected_count:.0%}).")

    gaps = _find_gaps(series.index, expected_interval_seconds)
    profile["gaps"] = gaps
    profile["gap_count"] = len(gaps)
    if gaps:
        warnings.append(f"{len(gaps)} data gap(s) detected, longest {gaps[0]['duration_seconds'] / 3600:.1f}h.")

    profile["descriptive_stats"] = {
        "min": float(series.min()),
        "max": float(series.max()),
        "mean": float(series.mean()),
        "median": float(series.median()),
        "std": float(series.std(ddof=1)) if n > 1 else 0.0,
        "variance": float(series.var(ddof=1)) if n > 1 else 0.0,
        "quantiles": _quantiles(series),
    }

    profile["distribution"] = _distribution(series)
    if profile["distribution"]["note"]:
        warnings.append(profile["distribution"]["note"])

    profile["trend"] = _trend(series)
    if profile["trend"]["note"] and profile["trend"]["direction"] is None:
        warnings.append(profile["trend"]["note"])

    profile["rolling_behavior"] = _rolling_behavior(series, expected_interval_seconds)
    if profile["rolling_behavior"] is None and n < MIN_FOR_ROLLING:
        warnings.append(f"insufficient data for rolling behavior (n={n} < {MIN_FOR_ROLLING})")

    profile["change_rate"] = _change_rate(series)

    return profile
