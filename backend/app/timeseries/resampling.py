"""Resampling & alignment utilities.

The one rule this module never breaks: **it never invents an observation
without being told to.** A bucket with zero raw observations in it is
reported as missing (`value: None, is_observed: False, is_interpolated:
False`) unless the caller explicitly asks for an interpolation policy —
and even then, every interpolated point is labeled as such, distinguishing
it from `is_observed=True` aggregated buckets. Downstream code (features,
correlation, charts) can filter on these flags instead of silently trusting
a number that was made up.
"""
from datetime import datetime
from enum import Enum
from typing import Any, Optional

import numpy as np
import pandas as pd


class AggregationMethod(str, Enum):
    MEAN = "mean"
    MEDIAN = "median"
    MIN = "min"
    MAX = "max"
    LAST = "last"
    SUM = "sum"
    COUNT = "count"


class InterpolationPolicy(str, Enum):
    NONE = "none"          # missing buckets stay missing — the default, and the only "safe by default" choice
    LINEAR = "linear"      # linear interpolation between the nearest observed/aggregated buckets
    FORWARD_FILL = "ffill"  # carry the last known aggregated value forward


MAX_BUCKETS = 20_000


class TooManyBucketsError(Exception):
    def __init__(self, count: int):
        self.count = count
        super().__init__(f"Requested resampling would produce {count} buckets (> {MAX_BUCKETS}). Widen the bucket size or narrow the range.")


def resample_series(
    series: pd.Series,
    *,
    start: datetime,
    end: datetime,
    freq_seconds: float,
    aggregation: AggregationMethod = AggregationMethod.MEAN,
    interpolation: InterpolationPolicy = InterpolationPolicy.NONE,
) -> list[dict[str, Any]]:
    """Buckets `series` into fixed [start, start+freq), [start+freq, ...)
    windows and aggregates each. Buckets are always anchored at `start` —
    not at whatever midnight boundary pandas would otherwise pick — so two
    resample calls with the same (start, freq_seconds) always align.

    Returns a list of dicts, one per bucket, in chronological order. Never
    raises on empty input — a sensor with no data in range resamples to an
    all-missing (or, under an interpolation policy, all-flagged) sequence.
    """
    if freq_seconds <= 0:
        raise ValueError("freq_seconds must be positive")

    n_buckets = int((end - start).total_seconds() // freq_seconds) + 1
    if n_buckets > MAX_BUCKETS:
        raise TooManyBucketsError(n_buckets)

    start_ts = pd.Timestamp(start).tz_convert("UTC") if pd.Timestamp(start).tzinfo else pd.Timestamp(start).tz_localize("UTC")
    freq = pd.Timedelta(seconds=freq_seconds)
    bucket_starts = pd.DatetimeIndex([start_ts + i * freq for i in range(n_buckets)])

    if series.empty:
        agg_values = pd.Series([np.nan] * n_buckets, index=bucket_starts)
        counts = pd.Series([0] * n_buckets, index=bucket_starts)
    else:
        # Assign each observation to its bucket by integer division on elapsed time.
        elapsed = (series.index - start_ts).total_seconds()
        bucket_idx = np.floor(elapsed / freq_seconds).astype("int64")
        valid = (bucket_idx >= 0) & (bucket_idx < n_buckets)
        grouped = pd.DataFrame({"value": series.values[valid], "bucket": bucket_idx[valid]})

        agg_fn = {
            AggregationMethod.MEAN: "mean",
            AggregationMethod.MEDIAN: "median",
            AggregationMethod.MIN: "min",
            AggregationMethod.MAX: "max",
            AggregationMethod.LAST: "last",
            AggregationMethod.SUM: "sum",
            AggregationMethod.COUNT: "count",
        }[aggregation]

        counts_by_bucket = grouped.groupby("bucket")["value"].count()
        values_by_bucket = grouped.groupby("bucket")["value"].agg(agg_fn)

        agg_values = pd.Series([np.nan] * n_buckets, index=range(n_buckets))
        counts = pd.Series([0] * n_buckets, index=range(n_buckets))
        agg_values.loc[values_by_bucket.index] = values_by_bucket.values
        counts.loc[counts_by_bucket.index] = counts_by_bucket.values
        agg_values.index = bucket_starts
        counts.index = bucket_starts

    is_observed = counts > 0
    final_values = agg_values.copy()
    is_interpolated = pd.Series(False, index=bucket_starts)

    if interpolation == InterpolationPolicy.LINEAR:
        interpolated = agg_values.interpolate(method="linear", limit_direction=None)
        newly_filled = is_observed.copy()
        newly_filled[:] = False
        fill_mask = agg_values.isna() & interpolated.notna()
        final_values = interpolated
        is_interpolated = fill_mask
    elif interpolation == InterpolationPolicy.FORWARD_FILL:
        filled = agg_values.ffill()
        fill_mask = agg_values.isna() & filled.notna()
        final_values = filled
        is_interpolated = fill_mask

    results = []
    for i, bucket_start in enumerate(bucket_starts):
        bucket_end = bucket_start + freq
        value = final_values.iloc[i]
        results.append(
            {
                "bucket_start": bucket_start.to_pydatetime().isoformat(),
                "bucket_end": bucket_end.to_pydatetime().isoformat(),
                "value": None if pd.isna(value) else float(value),
                "observation_count": int(counts.iloc[i]),
                "is_observed": bool(is_observed.iloc[i]),
                "is_interpolated": bool(is_interpolated.iloc[i]),
            }
        )
    return results


def to_regular_grid_for_analysis(
    series: pd.Series, *, freq_seconds: float, start: Optional[datetime] = None, end: Optional[datetime] = None
) -> tuple[pd.Series, dict[str, Any]]:
    """Produces a strictly regular (no gaps) pandas Series suitable for
    autocorrelation/FFT, which require evenly-spaced samples. Buckets with
    no observation are DROPPED (not interpolated) — dropping preserves
    "only real data" but breaks strict even-spacing, which is disclosed to
    the caller via the returned metadata rather than hidden.
    """
    if series.empty:
        return series, {"buckets_total": 0, "buckets_observed": 0, "coverage_fraction": None, "gaps_dropped": 0}

    start = start or series.index[0].to_pydatetime()
    end = end or series.index[-1].to_pydatetime()
    buckets = resample_series(series, start=start, end=end, freq_seconds=freq_seconds, aggregation=AggregationMethod.MEAN, interpolation=InterpolationPolicy.NONE)

    observed = [b for b in buckets if b["is_observed"]]
    coverage = len(observed) / len(buckets) if buckets else None

    if not observed:
        return pd.Series(dtype="float64"), {"buckets_total": len(buckets), "buckets_observed": 0, "coverage_fraction": coverage, "gaps_dropped": len(buckets)}

    index = pd.DatetimeIndex([pd.Timestamp(b["bucket_start"]) for b in observed])
    values = [b["value"] for b in observed]
    return pd.Series(values, index=index), {
        "buckets_total": len(buckets),
        "buckets_observed": len(observed),
        "coverage_fraction": coverage,
        "gaps_dropped": len(buckets) - len(observed),
    }
