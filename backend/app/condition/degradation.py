"""Degradation & long-term change classification.

THREE-WAY DISTINCTION, deliberately conservative:

  observed_trend        A statistically significant linear trend exists
                         somewhere in the analysis window (p < 0.05,
                         r-squared >= 0.1 — the same gate
                         app.timeseries.profiling uses). Descriptive only.
                         Many "observed trends" are transient, seasonal, or
                         noise that happened to fit a line over one window.

  degradation_candidate The trend PERSISTS across independent, non-
                         overlapping sub-windows of the analysis period
                         (not just one fitted line over the whole span) AND
                         is corroborated by at least one independent line
                         of evidence — meaningful deviation from a declared
                         baseline, or a rising rate of detector-flagged
                         anomalies. Two independent statistics agreeing is
                         real evidence; one fitted line is not.

  validated_degradation Reachable ONLY through an explicit human review
                         action (app.services.condition_service.
                         validate_degradation_signal) — never set
                         automatically. This platform has no maintenance-
                         outcome data (see the evidence audit in
                         docs/CONDITION_INTELLIGENCE.md: MaintenanceEvent
                         has no ingestion workflow and holds zero rows) to
                         confirm a candidate against. Auto-promoting to
                         "validated" would be exactly the unearned
                         confidence explicitly forbids.

None of this is a trained model. It is a documented, deterministic,
inspectable rule set over real statistics computed by app.timeseries and
app.condition.evidence — every promotion decision traces to specific
numbers in its `evidence` payload, and can be explained in one sentence.
"""
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Optional

import pandas as pd

from app.timeseries import profiling
from app.timeseries.splitting import chronological_split

SUB_WINDOW_COUNT = 3  # analysis window is split into this many equal, non-overlapping sub-windows
MAJORITY_SUB_WINDOWS = 2  # of SUB_WINDOW_COUNT, how many must agree for "persistent"
CORROBORATION_MAD_THRESHOLD = 2.0  # |deviation_mad_units| at/above this counts as corroborating evidence
MIN_OBSERVATIONS_PER_SUB_WINDOW = profiling.MIN_FOR_TREND


@dataclass
class SubWindowTrend:
    start: str
    end: str
    observation_count: int
    direction: Optional[str]
    slope_per_hour: Optional[float]
    r_squared: Optional[float]
    p_value: Optional[float]
    significant: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_sub_window_trends(series: pd.Series, *, window_start: datetime, window_end: datetime) -> list[SubWindowTrend]:
    """Splits [window_start, window_end] into SUB_WINDOW_COUNT equal,
    non-overlapping, chronologically-ordered sub-windows (reusing
    app.timeseries.splitting.chronological_split with equal fractions —
    the same leakage-verified boundary machinery built, applied
    here to get independent sub-windows rather than train/test splits) and
    fits an independent OLS trend in each."""
    fraction = 1.0 / SUB_WINDOW_COUNT
    split = chronological_split(start=window_start, end=window_end, train_fraction=fraction, val_fraction=fraction)
    bounds = [
        (datetime.fromisoformat(split.train_start), datetime.fromisoformat(split.train_end)),
        (datetime.fromisoformat(split.val_start), datetime.fromisoformat(split.val_end)),
        (datetime.fromisoformat(split.test_start), datetime.fromisoformat(split.test_end)),
    ]

    results: list[SubWindowTrend] = []
    for start, end in bounds:
        sub_series = series[(series.index >= pd.Timestamp(start)) & (series.index < pd.Timestamp(end))]
        n = len(sub_series)
        if n < MIN_OBSERVATIONS_PER_SUB_WINDOW:
            results.append(SubWindowTrend(start.isoformat(), end.isoformat(), n, None, None, None, None, False))
            continue
        trend = profiling.compute_profile(
            sub_series, sensor_unit="", expected_interval_seconds=None, requested_start=start, requested_end=end
        )["trend"]
        significant = trend["direction"] not in (None, "no_significant_trend")
        results.append(
            SubWindowTrend(
                start=start.isoformat(), end=end.isoformat(), observation_count=n,
                direction=trend["direction"], slope_per_hour=trend["slope_per_hour"],
                r_squared=trend["r_squared"], p_value=trend["p_value"], significant=significant,
            )
        )
    return results


def _dominant_direction(sub_window_trends: list[SubWindowTrend]) -> tuple[Optional[str], int]:
    significant = [t for t in sub_window_trends if t.significant]
    if not significant:
        return None, 0
    counts: dict[str, int] = {}
    for t in significant:
        counts[t.direction] = counts.get(t.direction, 0) + 1
    direction = max(counts, key=counts.get)
    return direction, counts[direction]


def classify_degradation(
    *,
    sub_window_trends: list[SubWindowTrend],
    deviation_evidence: dict[str, Any],
    anomaly_evidence: dict[str, Any],
) -> dict[str, Any]:
    """Applies the three-way rule set above to already-computed evidence.
    Pure function, fully deterministic, no DB access — every input is a
    plain dict/dataclass produced by app.condition.evidence and this
    module's own compute_sub_window_trends."""
    any_significant = any(t.significant for t in sub_window_trends)
    direction, agree_count = _dominant_direction(sub_window_trends)
    persistent = agree_count >= MAJORITY_SUB_WINDOWS

    deviation_mad = deviation_evidence.get("deviation_mad_units")
    corroborated_by_deviation = deviation_mad is not None and abs(deviation_mad) >= CORROBORATION_MAD_THRESHOLD
    corroborated_by_anomalies = anomaly_evidence.get("frequency_increasing") is True
    corroborated = corroborated_by_deviation or corroborated_by_anomalies

    if not any_significant:
        stage = None
        rationale = "No sub-window shows a statistically significant trend (p < 0.05 and r-squared >= 0.1) — no degradation signal to report."
    elif persistent and corroborated:
        stage = "degradation_candidate"
        reasons = []
        if corroborated_by_deviation:
            reasons.append(f"deviation from baseline is {deviation_mad:.2f} MAD-equivalent units")
        if corroborated_by_anomalies:
            reasons.append("detector-flagged anomaly frequency is increasing within the window")
        rationale = (
            f"{agree_count}/{len(sub_window_trends)} sub-windows show a consistent '{direction}' trend, "
            f"corroborated by independent evidence: {'; '.join(reasons)}."
        )
    else:
        stage = "observed_trend"
        if not persistent:
            rationale = f"Only {agree_count}/{len(sub_window_trends)} sub-windows agree on a significant trend direction — not yet persistent enough to call a degradation candidate."
        else:
            rationale = f"{agree_count}/{len(sub_window_trends)} sub-windows show a consistent '{direction}' trend, but no independent corroborating evidence (deviation or rising anomaly frequency) was found."

    return {
        "stage": stage,
        "dominant_direction": direction,
        "sub_windows_agreeing": agree_count,
        "sub_window_count": len(sub_window_trends),
        "persistent": persistent,
        "corroborated": corroborated,
        "corroborated_by_deviation": corroborated_by_deviation,
        "corroborated_by_anomalies": corroborated_by_anomalies,
        "rationale": rationale,
        "sub_window_trends": [t.to_dict() for t in sub_window_trends],
    }
