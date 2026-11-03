"""Feature engineering framework.

## The causality guarantee (read this before touching this file)

`compute_feature_value` is the single choke point through which every
feature — in a live preview, in a training dataset row, anywhere — gets
computed. It takes the FULL sensor series (which may well contain
observations after `as_of`) and, as its very first step, slices out
everything at or before `as_of` (and, for windowed features, everything
before `as_of - window`) before any statistic touches the data:

    windowed = full_series[(full_series.index > window_start) & (full_series.index <= as_of)]

This means a caller cannot accidentally leak future data by forgetting to
pre-filter — the enforcement lives in the one function every feature
computation goes through, not in caller discipline. `tests/test_features.py`
proves this directly: it plants a distinctive value one second after
`as_of` and asserts every feature kind is blind to it.

## Feature categories implemented here

- STATISTICAL: mean, median, std, variance, min, max, quantile, iqr
- TEMPORAL: lag, rolling_mean, rolling_std, rate_of_change, diff, ema
- DISTRIBUTIONAL: skewness, kurtosis (gated on n >= 30, same threshold as profiling)
- FREQUENCY: spectral_dominant_period, spectral_energy — gated behind a
  regular-sampling check (see `_spectral_feature`); most sensor/window
  combinations will not qualify, and the feature reports why rather than
  guessing.
- DOMAIN: not implemented. No verified domain-specific feature definition
  exists for this generic synthetic equipment fleet yet (a "duty cycle" or
  "load factor" feature needs a real operational definition this platform
  doesn't have). The category and registry hook exist so one can be added
  without restructuring anything else.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Optional

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

MIN_FOR_DISTRIBUTION = 30
MIN_FOR_SPECTRAL = 32
SPECTRAL_MIN_COVERAGE = 0.7


class FeatureCategory(str, Enum):
    STATISTICAL = "statistical"
    TEMPORAL = "temporal"
    DISTRIBUTIONAL = "distributional"
    FREQUENCY = "frequency"
    DOMAIN = "domain"


class FeatureKind(str, Enum):
    MEAN = "mean"
    MEDIAN = "median"
    STD = "std"
    VARIANCE = "variance"
    MIN = "min"
    MAX = "max"
    QUANTILE = "quantile"
    IQR = "iqr"
    LAG = "lag"
    ROLLING_MEAN = "rolling_mean"
    ROLLING_STD = "rolling_std"
    RATE_OF_CHANGE = "rate_of_change"
    DIFF = "diff"
    EMA = "ema"
    SKEWNESS = "skewness"
    KURTOSIS = "kurtosis"
    SPECTRAL_DOMINANT_PERIOD = "spectral_dominant_period"
    SPECTRAL_ENERGY = "spectral_energy"


KIND_CATEGORY: dict[FeatureKind, FeatureCategory] = {
    FeatureKind.MEAN: FeatureCategory.STATISTICAL,
    FeatureKind.MEDIAN: FeatureCategory.STATISTICAL,
    FeatureKind.STD: FeatureCategory.STATISTICAL,
    FeatureKind.VARIANCE: FeatureCategory.STATISTICAL,
    FeatureKind.MIN: FeatureCategory.STATISTICAL,
    FeatureKind.MAX: FeatureCategory.STATISTICAL,
    FeatureKind.QUANTILE: FeatureCategory.STATISTICAL,
    FeatureKind.IQR: FeatureCategory.STATISTICAL,
    FeatureKind.LAG: FeatureCategory.TEMPORAL,
    FeatureKind.ROLLING_MEAN: FeatureCategory.TEMPORAL,
    FeatureKind.ROLLING_STD: FeatureCategory.TEMPORAL,
    FeatureKind.RATE_OF_CHANGE: FeatureCategory.TEMPORAL,
    FeatureKind.DIFF: FeatureCategory.TEMPORAL,
    FeatureKind.EMA: FeatureCategory.TEMPORAL,
    FeatureKind.SKEWNESS: FeatureCategory.DISTRIBUTIONAL,
    FeatureKind.KURTOSIS: FeatureCategory.DISTRIBUTIONAL,
    FeatureKind.SPECTRAL_DOMINANT_PERIOD: FeatureCategory.FREQUENCY,
    FeatureKind.SPECTRAL_ENERGY: FeatureCategory.FREQUENCY,
}

# Kinds that operate over a bounded [as_of - window, as_of] window. Kinds
# NOT in this set (lag, diff) instead look back a fixed number of discrete
# observations rather than a time window.
WINDOWED_KINDS = {
    FeatureKind.MEAN, FeatureKind.MEDIAN, FeatureKind.STD, FeatureKind.VARIANCE,
    FeatureKind.MIN, FeatureKind.MAX, FeatureKind.QUANTILE, FeatureKind.IQR,
    FeatureKind.ROLLING_MEAN, FeatureKind.ROLLING_STD, FeatureKind.RATE_OF_CHANGE,
    FeatureKind.EMA, FeatureKind.SKEWNESS, FeatureKind.KURTOSIS,
    FeatureKind.SPECTRAL_DOMINANT_PERIOD, FeatureKind.SPECTRAL_ENERGY,
}


@dataclass
class FeatureResult:
    kind: str
    category: str
    value: Optional[float]
    computed: bool
    reason: Optional[str]
    observation_count_used: int
    window_start: Optional[str]
    window_end: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "category": self.category,
            "value": self.value,
            "computed": self.computed,
            "reason": self.reason,
            "observation_count_used": self.observation_count_used,
            "window_start": self.window_start,
            "window_end": self.window_end,
        }


def _as_utc(ts: datetime) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _spectral_feature(windowed: pd.Series, kind: FeatureKind, params: dict[str, Any]) -> tuple[Optional[float], Optional[str], int]:
    from app.timeseries.resampling import resample_series, AggregationMethod, InterpolationPolicy

    grid_freq_seconds = float(params.get("grid_freq_seconds", 300))
    if len(windowed) < MIN_FOR_SPECTRAL:
        return None, f"insufficient data for spectral analysis (n={len(windowed)} < {MIN_FOR_SPECTRAL})", len(windowed)

    buckets = resample_series(
        windowed, start=windowed.index[0].to_pydatetime(), end=windowed.index[-1].to_pydatetime(),
        freq_seconds=grid_freq_seconds, aggregation=AggregationMethod.MEAN, interpolation=InterpolationPolicy.NONE,
    )
    observed = [b for b in buckets if b["is_observed"]]
    coverage = len(observed) / len(buckets) if buckets else 0.0
    if len(observed) < MIN_FOR_SPECTRAL or coverage < SPECTRAL_MIN_COVERAGE:
        return None, f"data too sparse/irregular for spectral analysis (coverage={coverage:.0%}, need >= {SPECTRAL_MIN_COVERAGE:.0%})", len(windowed)

    values = np.array([b["value"] for b in observed], dtype="float64")
    values = values - values.mean()
    power = np.abs(np.fft.rfft(values)) ** 2
    freqs = np.fft.rfftfreq(len(values), d=grid_freq_seconds)

    if len(power) <= 1:
        return None, "insufficient frequency resolution for spectral analysis", len(observed)

    total_energy = float(np.sum(power[1:]))  # exclude DC component
    if kind == FeatureKind.SPECTRAL_ENERGY:
        return total_energy, None, len(observed)

    dominant_bin = int(np.argmax(power[1:])) + 1
    dominant_freq = freqs[dominant_bin]
    if dominant_freq <= 0:
        return None, "no meaningful dominant frequency found", len(observed)
    dominant_period_hours = (1.0 / dominant_freq) / 3600.0
    return float(dominant_period_hours), None, len(observed)


def compute_feature_value(
    full_series: pd.Series,
    *,
    kind: FeatureKind,
    as_of: datetime,
    window_seconds: Optional[float],
    params: Optional[dict[str, Any]] = None,
) -> FeatureResult:
    """THE causality boundary. See module docstring."""
    params = params or {}
    category = KIND_CATEGORY[kind].value
    as_of_ts = _as_utc(as_of)

    # Strict causal filter: nothing after as_of is ever visible past this line.
    causal = full_series[full_series.index <= as_of_ts]

    if kind in WINDOWED_KINDS and window_seconds is not None:
        window_start_ts = as_of_ts - timedelta(seconds=window_seconds)
        windowed = causal[causal.index > window_start_ts]
        window_start_iso, window_end_iso = window_start_ts.isoformat(), as_of_ts.isoformat()
    else:
        windowed = causal
        window_start_iso, window_end_iso = None, as_of_ts.isoformat()

    n = len(windowed)

    def result(value: Optional[float], computed: bool, reason: Optional[str], used: int) -> FeatureResult:
        return FeatureResult(kind.value, category, value, computed, reason, used, window_start_iso, window_end_iso)

    if kind == FeatureKind.MEAN:
        return result(float(windowed.mean()), True, None, n) if n >= 1 else result(None, False, "no observations in window", 0)
    if kind == FeatureKind.MEDIAN:
        return result(float(windowed.median()), True, None, n) if n >= 1 else result(None, False, "no observations in window", 0)
    if kind == FeatureKind.MIN:
        return result(float(windowed.min()), True, None, n) if n >= 1 else result(None, False, "no observations in window", 0)
    if kind == FeatureKind.MAX:
        return result(float(windowed.max()), True, None, n) if n >= 1 else result(None, False, "no observations in window", 0)
    if kind == FeatureKind.STD:
        return result(float(windowed.std(ddof=1)), True, None, n) if n >= 2 else result(None, False, f"need >=2 observations, got {n}", n)
    if kind == FeatureKind.VARIANCE:
        return result(float(windowed.var(ddof=1)), True, None, n) if n >= 2 else result(None, False, f"need >=2 observations, got {n}", n)
    if kind == FeatureKind.QUANTILE:
        q = float(params.get("q", 0.5))
        return result(float(windowed.quantile(q)), True, None, n) if n >= 1 else result(None, False, "no observations in window", 0)
    if kind == FeatureKind.IQR:
        if n < 4:
            return result(None, False, f"need >=4 observations for a robust IQR, got {n}", n)
        return result(float(windowed.quantile(0.75) - windowed.quantile(0.25)), True, None, n)
    if kind in (FeatureKind.ROLLING_MEAN, FeatureKind.ROLLING_STD):
        # Same causal window as MEAN/STD — named separately because in a
        # temporal-feature context "rolling mean over the last N minutes"
        # is the intended reading, distinct from a one-off summary stat.
        if kind == FeatureKind.ROLLING_MEAN:
            return result(float(windowed.mean()), True, None, n) if n >= 1 else result(None, False, "no observations in window", 0)
        return result(float(windowed.std(ddof=1)), True, None, n) if n >= 2 else result(None, False, f"need >=2 observations, got {n}", n)
    if kind == FeatureKind.RATE_OF_CHANGE:
        if n < 2:
            return result(None, False, f"need >=2 observations in window, got {n}", n)
        span_hours = (windowed.index[-1] - windowed.index[0]).total_seconds() / 3600.0
        if span_hours <= 0:
            return result(None, False, "window span is zero", n)
        return result(float((windowed.iloc[-1] - windowed.iloc[0]) / span_hours), True, None, n)
    if kind == FeatureKind.EMA:
        span = int(params.get("span", 10))
        if n < 3:
            return result(None, False, f"need >=3 observations, got {n}", n)
        return result(float(windowed.ewm(span=span, adjust=False).mean().iloc[-1]), True, None, n)
    if kind == FeatureKind.LAG:
        lag_steps = int(params.get("lag_steps", 1))
        if len(causal) <= lag_steps:
            return result(None, False, f"need >={lag_steps + 1} prior observations, got {len(causal)}", len(causal))
        return result(float(causal.iloc[-1 - lag_steps]), True, None, len(causal))
    if kind == FeatureKind.DIFF:
        lag_steps = int(params.get("lag_steps", 1))
        if len(causal) <= lag_steps:
            return result(None, False, f"need >={lag_steps + 1} prior observations, got {len(causal)}", len(causal))
        return result(float(causal.iloc[-1] - causal.iloc[-1 - lag_steps]), True, None, len(causal))
    if kind == FeatureKind.SKEWNESS:
        if n < MIN_FOR_DISTRIBUTION:
            return result(None, False, f"need >={MIN_FOR_DISTRIBUTION} observations for skewness, got {n}", n)
        return result(float(scipy_stats.skew(windowed.values)), True, None, n)
    if kind == FeatureKind.KURTOSIS:
        if n < MIN_FOR_DISTRIBUTION:
            return result(None, False, f"need >={MIN_FOR_DISTRIBUTION} observations for kurtosis, got {n}", n)
        return result(float(scipy_stats.kurtosis(windowed.values)), True, None, n)
    if kind in (FeatureKind.SPECTRAL_DOMINANT_PERIOD, FeatureKind.SPECTRAL_ENERGY):
        value, reason, used = _spectral_feature(windowed, kind, params)
        return result(value, value is not None, reason, used)

    raise ValueError(f"Unknown feature kind: {kind}")
