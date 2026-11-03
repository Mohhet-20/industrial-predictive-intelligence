"""Multi-sensor temporal context: alignment, correlation exploration, and
lagged relationship exploration across sensors.

Everything here is labeled, deliberately and repeatedly, as ASSOCIATION —
never causation. Two sensors moving together can mean a shared physical
cause, a shared measurement artifact, a coincidence over a short window, or
one genuinely driving the other; distinguishing those requires domain
knowledge and experiment design this platform doesn't have. The API and
frontend surface the same caveat every time a correlation number appears.
"""
from datetime import datetime
from typing import Any, Optional

import pandas as pd

from app.timeseries.resampling import AggregationMethod, InterpolationPolicy, resample_series

MIN_OVERLAPPING_OBSERVATIONS = 10
ASSOCIATION_CAVEAT = (
    "This measures statistical association only. It is not evidence of a causal "
    "relationship — shared timing, a common upstream cause, or coincidence can all "
    "produce a strong correlation between two unrelated sensors."
)


def align_sensors_on_grid(
    series_by_sensor: dict[str, pd.Series], *, start: datetime, end: datetime, freq_seconds: float
) -> pd.DataFrame:
    """Resamples every sensor onto the SAME anchored grid (mean aggregation,
    no interpolation — see resampling.py) and returns a DataFrame with one
    column per sensor. Buckets with no observation are NaN, never a
    fabricated value."""
    columns: dict[str, list[Optional[float]]] = {}
    index: Optional[pd.DatetimeIndex] = None
    for sensor_id, series in series_by_sensor.items():
        buckets = resample_series(series, start=start, end=end, freq_seconds=freq_seconds, aggregation=AggregationMethod.MEAN, interpolation=InterpolationPolicy.NONE)
        columns[sensor_id] = [b["value"] for b in buckets]
        if index is None:
            index = pd.DatetimeIndex([pd.Timestamp(b["bucket_start"]) for b in buckets])
    return pd.DataFrame(columns, index=index)


def correlation_matrix(aligned: pd.DataFrame, *, method: str = "pearson") -> dict[str, Any]:
    sensor_ids = list(aligned.columns)
    pairs: list[dict[str, Any]] = []
    for i, a in enumerate(sensor_ids):
        for b in sensor_ids[i + 1 :]:
            pair_df = aligned[[a, b]].dropna()
            n = len(pair_df)
            if n < MIN_OVERLAPPING_OBSERVATIONS:
                pairs.append({"sensor_a": a, "sensor_b": b, "correlation": None, "overlapping_observations": n, "note": f"insufficient overlapping data (n={n} < {MIN_OVERLAPPING_OBSERVATIONS})"})
                continue
            corr = pair_df[a].corr(pair_df[b], method=method)
            pairs.append({"sensor_a": a, "sensor_b": b, "correlation": None if pd.isna(corr) else float(corr), "overlapping_observations": n, "note": None})
    return {"method": method, "pairs": pairs, "caveat": ASSOCIATION_CAVEAT}


def lagged_cross_correlation(
    aligned: pd.DataFrame, *, sensor_a: str, sensor_b: str, max_lag_steps: int, freq_seconds: float
) -> dict[str, Any]:
    """Correlates sensor_a[t] against sensor_b[t - lag] for lag in
    [-max_lag_steps, max_lag_steps] (positive lag = sensor_b lags behind
    sensor_a). Reports the strongest correlation and at which lag, plus
    every lag's value so the caller can see the whole shape rather than
    trusting one cherry-picked number."""
    if sensor_a not in aligned.columns or sensor_b not in aligned.columns:
        raise ValueError("sensor_a and sensor_b must both be columns of the aligned DataFrame")

    a = aligned[sensor_a]
    b = aligned[sensor_b]
    results = []
    for lag in range(-max_lag_steps, max_lag_steps + 1):
        shifted_b = b.shift(lag)
        pair = pd.DataFrame({"a": a, "b": shifted_b}).dropna()
        n = len(pair)
        if n < MIN_OVERLAPPING_OBSERVATIONS:
            results.append({"lag_steps": lag, "lag_seconds": lag * freq_seconds, "correlation": None, "overlapping_observations": n})
            continue
        corr = pair["a"].corr(pair["b"])
        results.append({"lag_steps": lag, "lag_seconds": lag * freq_seconds, "correlation": None if pd.isna(corr) else float(corr), "overlapping_observations": n})

    valid = [r for r in results if r["correlation"] is not None]
    best = max(valid, key=lambda r: abs(r["correlation"])) if valid else None

    return {
        "sensor_a": sensor_a,
        "sensor_b": sensor_b,
        "lags": results,
        "strongest_lag": best,
        "caveat": ASSOCIATION_CAVEAT + " A nonzero best lag is a timing pattern in this window, not proof one sensor drives the other.",
    }


def compare_missingness(missing_fractions: dict[str, Optional[float]]) -> dict[str, Any]:
    """Thin formatter over per-sensor missing_fraction values (as computed
    by the profiling engine for each sensor) — ranks sensors by data
    completeness over the same requested window."""
    ranked = sorted(
        [{"sensor_id": sid, "missing_fraction": frac} for sid, frac in missing_fractions.items()],
        key=lambda r: (r["missing_fraction"] is None, r["missing_fraction"] or 0),
        reverse=True,
    )
    return {"sensors": ranked, "note": "Missingness is computed independently per sensor against its own declared sampling cadence; sensors with no declared cadence report null."}
