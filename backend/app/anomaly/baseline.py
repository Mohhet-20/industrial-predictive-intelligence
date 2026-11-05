"""Baseline construction — establishing "expected behavior" for a sensor
over a reference (training) period.

Uses robust statistics (median + MAD — median absolute deviation) rather
than mean/std as the primary baseline: a handful of extreme values (which
is exactly what an anomaly detector's reference period might contain,
since real data is never perfectly clean) can drag a mean/std baseline
far enough to mask genuine anomalies. MAD is far less sensitive to a small
number of outliers. Mean/std are still recorded alongside for reference
and for detectors (none) that might need them.

The baseline is a static summary of the WHOLE reference period, not a
per-point rolling calculation — this is what makes the causality guarantee
in app.anomaly.scoring simple to state and verify: as long as the
reference period ends at or before the scoring period begins (enforced by
app.services.anomaly_service), no observation used to build the baseline
was ever in the future relative to any point it is used to score.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

MIN_FOR_BASELINE = 30
MAD_TO_STD_FACTOR = 1.4826  # scales MAD to be a consistent estimator of std dev under a normal distribution


class InsufficientReferenceDataError(Exception):
    def __init__(self, count: int, minimum: int = MIN_FOR_BASELINE):
        self.count = count
        self.minimum = minimum
        super().__init__(
            f"Reference period has only {count} observation(s); at least {minimum} are required "
            "to establish a reliable baseline."
        )


@dataclass
class Baseline:
    method: str
    reference_start: str
    reference_end: str
    sample_count: int
    median: float
    mad: float
    robust_std_estimate: float  # mad * MAD_TO_STD_FACTOR — comparable in scale to a normal std dev
    mean: float
    std: float
    min: float
    max: float
    computed_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _as_utc(ts: datetime) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def compute_baseline(series: pd.Series, *, reference_start: datetime, reference_end: datetime) -> Baseline:
    """Computes a robust baseline from `series` restricted to
    [reference_start, reference_end]. `series` is assumed tz-aware UTC
    (the convention every app.timeseries/app.anomaly module shares — see
    app.timeseries.data_access). Raises InsufficientReferenceDataError
    rather than silently building a baseline from too few points — a
    baseline from 5 observations is not a trustworthy "normal.\""""
    start_ts, end_ts = _as_utc(reference_start), _as_utc(reference_end)
    windowed = series[(series.index >= start_ts) & (series.index <= end_ts)]
    n = len(windowed)
    if n < MIN_FOR_BASELINE:
        raise InsufficientReferenceDataError(n)

    values = windowed.values.astype("float64")
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    robust_std_estimate = mad * MAD_TO_STD_FACTOR

    return Baseline(
        method="median_mad",
        reference_start=reference_start.isoformat(),
        reference_end=reference_end.isoformat(),
        sample_count=n,
        median=median,
        mad=mad,
        robust_std_estimate=robust_std_estimate,
        mean=float(np.mean(values)),
        std=float(np.std(values, ddof=1)) if n > 1 else 0.0,
        min=float(np.min(values)),
        max=float(np.max(values)),
        computed_at=datetime.now(timezone.utc).isoformat(),
    )
