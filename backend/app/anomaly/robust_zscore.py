"""Robust statistical anomaly detector.

Method: for each observation in the scoring window, compute a robust
z-score against a pre-established Baseline (median/MAD from a reference
period — see app.anomaly.baseline):

    z = (value - baseline.median) / baseline.robust_std_estimate

Points with |z| >= threshold are flagged. Flagged points close together
in time are merged into a single anomaly-event candidate (an isolated
spike a minute long and a two-hour excursion are different things, and
reporting every flagged 5-minute reading in a two-hour excursion as 24
separate anomalies would be noise, not signal).

Assumptions & limitations (state these to the user, not just here):
- Assumes the reference period is representative of "normal" — a
  reference period that itself contains unusual behavior will bias the
  baseline and can suppress genuine detections during scoring.
- A single global baseline per sensor does not adapt to daily/seasonal
  cycles; a sensor with strong diurnal swings will show elevated z-scores
  at its natural daily extremes even when nothing is wrong. (The
  Isolation Forest detector, which uses rolling-window features rather
  than raw values, is less prone to this — see app.anomaly.isolation_forest_detector.)
- Requires MIN_FOR_BASELINE observations in the reference period
  (enforced by app.anomaly.baseline) and a reference period with nonzero
  variability.
"""
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import pandas as pd

from app.anomaly.baseline import Baseline

DEFAULT_THRESHOLD = 3.5
MIN_ROBUST_STD = 1e-9
DEFAULT_MERGE_SECONDS = 1800  # flagged points within 30 minutes of each other merge into one event


@dataclass
class ScoredPoint:
    timestamp: str
    value: float
    z_score: float
    flagged: bool


@dataclass
class AnomalyEventCandidate:
    period_start: str
    period_end: str
    raw_score: float
    point_count: int
    representative_timestamp: str
    representative_value: float
    explanation: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def score_points(series: pd.Series, baseline: Baseline) -> list[ScoredPoint]:
    """Scores every observation in `series` against `baseline`. Pure
    scoring step — no threshold/merge logic — kept separate so tests and
    the evaluation harness can inspect raw scores directly."""
    if baseline.robust_std_estimate < MIN_ROBUST_STD:
        return []
    scored = []
    for ts, value in series.items():
        z = (float(value) - baseline.median) / baseline.robust_std_estimate
        scored.append(ScoredPoint(timestamp=ts.to_pydatetime().isoformat(), value=float(value), z_score=z, flagged=False))
    return scored


def detect(
    series: pd.Series,
    baseline: Baseline,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    merge_seconds: float = DEFAULT_MERGE_SECONDS,
) -> dict[str, Any]:
    """Returns {"points_scored", "points_flagged", "candidates": [...],
    "note": Optional[str]} — `note` is set (and candidates empty) when the
    baseline has no usable variability."""
    if baseline.robust_std_estimate < MIN_ROBUST_STD:
        return {
            "points_scored": len(series),
            "points_flagged": 0,
            "candidates": [],
            "note": (
                "The reference period showed no variability (median absolute deviation is ~0), "
                "so a robust z-score is not meaningful here. Try a longer or different reference period."
            ),
        }

    scored = score_points(series, baseline)
    for p in scored:
        p.flagged = abs(p.z_score) >= threshold

    flagged = [p for p in scored if p.flagged]
    candidates = _merge_into_events(flagged, baseline=baseline, threshold=threshold, merge_seconds=merge_seconds)

    return {
        "points_scored": len(scored),
        "points_flagged": len(flagged),
        "candidates": candidates,
        "note": None,
    }


def _merge_into_events(
    flagged: list[ScoredPoint], *, baseline: Baseline, threshold: float, merge_seconds: float
) -> list[AnomalyEventCandidate]:
    if not flagged:
        return []

    flagged = sorted(flagged, key=lambda p: p.timestamp)
    groups: list[list[ScoredPoint]] = [[flagged[0]]]
    for p in flagged[1:]:
        prev = groups[-1][-1]
        gap = (datetime.fromisoformat(p.timestamp) - datetime.fromisoformat(prev.timestamp)).total_seconds()
        if gap <= merge_seconds:
            groups[-1].append(p)
        else:
            groups.append([p])

    candidates = []
    for group in groups:
        strongest = max(group, key=lambda p: abs(p.z_score))
        candidates.append(
            AnomalyEventCandidate(
                period_start=group[0].timestamp,
                period_end=group[-1].timestamp,
                raw_score=strongest.z_score,
                point_count=len(group),
                representative_timestamp=strongest.timestamp,
                representative_value=strongest.value,
                explanation={
                    "method": "robust_zscore",
                    "baseline_median": baseline.median,
                    "baseline_robust_std_estimate": baseline.robust_std_estimate,
                    "baseline_reference_period": {"start": baseline.reference_start, "end": baseline.reference_end},
                    "observed_value": strongest.value,
                    "z_score": strongest.z_score,
                    "threshold": threshold,
                    "description": (
                        f"Value {strongest.value:.4g} is {abs(strongest.z_score):.1f} robust standard deviations "
                        f"{'above' if strongest.z_score > 0 else 'below'} the reference median of {baseline.median:.4g}."
                    ),
                },
            )
        )
    return candidates
