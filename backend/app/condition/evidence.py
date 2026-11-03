"""Equipment Condition Intelligence — evidence, not a verdict (Phase 4 §2).

Computes a bundle of independent, explainable evidence about a sensor's
recent behavior from real stored data. Every piece of evidence states its
own methodology and is never combined into a single opaque number.

Five kinds of evidence:
  - trend_evidence: the same OLS linear trend app.timeseries.profiling
    already computes and validates (min-n gate, p-value, r-squared) —
    reused here rather than re-implemented, so "trend" means exactly one
    thing across this whole codebase.
  - persistent_anomaly_evidence: counts and timing of REAL detector-flagged
    Anomaly rows (Phase 3) touching this sensor in the window — genuine
    detection history, never re-derived or estimated.
  - deviation_from_baseline: current window's robust statistics compared
    against a declared reference-period baseline (app.anomaly.baseline's
    median/MAD, reused directly).
  - variance_change_evidence: variance-shift candidates within the window
    (app.timeseries.change_detection, reused directly — the same
    plain effect-size statistic, not a new model).
  - cross_sensor_association: Pearson correlation with sibling sensors on
    the same asset over the window (app.timeseries.multi_sensor, reused
    directly) — association, never causation, same caveat as Phase 2.

WHY NO COMPOSITE SCORE: a single blended "health score" combining a
p-value, a MAD ratio, a variance ratio, a correlation coefficient, and an
anomaly count requires weights this platform has no validated basis for
choosing. Publishing one would look authoritative while being an arbitrary
opinion dressed as measurement — exactly what Phase 4 forbids. Each piece
of evidence is reported on its own terms; a human reviewer combines them
using engineering judgment this platform does not have.
"""
from datetime import datetime
from typing import Any, Optional

import pandas as pd

from app.anomaly import baseline as baseline_mod
from app.timeseries import change_detection, multi_sensor, profiling

NO_COMPOSITE_SCORE_NOTE = (
    "This platform deliberately does not compute a single blended 'health score.' "
    "Each evidence type below is measured independently, with its own methodology — "
    "combining them into one number would require weights that have no validated basis. "
    "Review each evidence type on its own terms."
)

MIN_FOR_DEVIATION = 5
VARIANCE_CHANGE_WINDOW_SIZE = 15
VARIANCE_CHANGE_MIN_RATIO = 2.0


def summarize_anomaly_persistence(
    anomalies: list[dict[str, Any]], *, window_start: datetime, window_end: datetime
) -> dict[str, Any]:
    """Real evidence from Phase 3's Anomaly rows — never re-scored here.
    `anomalies` is the list of already-queried, already-authorized Anomaly
    rows (as plain dicts: severity_tier, status, period_start) for this
    sensor in the window; DB access happens in the service layer, not here."""
    if not anomalies:
        return {
            "anomaly_count": 0,
            "count_by_severity_tier": {},
            "count_by_status": {},
            "frequency_increasing": None,
            "most_recent_period_start": None,
            "note": "No detector-flagged anomalies for this sensor in the window (see /anomaly/* for detector setup).",
        }

    by_tier: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for a in anomalies:
        by_tier[a["severity_tier"]] = by_tier.get(a["severity_tier"], 0) + 1
        by_status[a["status"]] = by_status.get(a["status"], 0) + 1

    midpoint = window_start + (window_end - window_start) / 2
    first_half = sum(1 for a in anomalies if a["period_start"] < midpoint)
    second_half = len(anomalies) - first_half
    frequency_increasing: Optional[bool] = None
    if len(anomalies) >= 4:
        frequency_increasing = second_half > first_half

    most_recent = max(a["period_start"] for a in anomalies)

    return {
        "anomaly_count": len(anomalies),
        "count_by_severity_tier": by_tier,
        "count_by_status": by_status,
        "first_half_count": first_half,
        "second_half_count": second_half,
        "frequency_increasing": frequency_increasing,
        "most_recent_period_start": most_recent.isoformat(),
        "note": "Counts of real detector output (app.models.intelligence.Anomaly), not re-computed here.",
    }


def compute_deviation_from_baseline(
    window_series: pd.Series, *, reference_series: pd.Series, reference_start: datetime, reference_end: datetime
) -> dict[str, Any]:
    """Compares the analysis window's robust center against a baseline
    built from a distinct, declared reference period (reuses
    app.anomaly.baseline's median/MAD exactly as the anomaly detector
    does) — the SAME robust-statistics methodology, applied here to
    describe current standing rather than to flag individual points."""
    try:
        baseline = baseline_mod.compute_baseline(reference_series, reference_start=reference_start, reference_end=reference_end)
    except baseline_mod.InsufficientReferenceDataError as exc:
        return {"computed": False, "reason": str(exc)}

    n = len(window_series)
    if n < MIN_FOR_DEVIATION:
        return {"computed": False, "reason": f"insufficient data in analysis window (n={n} < {MIN_FOR_DEVIATION})", "baseline": baseline.to_dict()}

    window_median = float(window_series.median())
    deviation_mad_units = None
    if baseline.robust_std_estimate > 0:
        deviation_mad_units = (window_median - baseline.median) / baseline.robust_std_estimate

    return {
        "computed": True,
        "method": "median_mad_deviation",
        "baseline": baseline.to_dict(),
        "window_observation_count": n,
        "window_median": window_median,
        "deviation_mad_units": deviation_mad_units,
        "direction": (
            None if deviation_mad_units is None
            else ("above_baseline" if deviation_mad_units > 0 else "below_baseline" if deviation_mad_units < 0 else "at_baseline")
        ),
        "note": (
            "deviation_mad_units is (window median - baseline median) / (baseline MAD * 1.4826), the "
            "same robust-standard-deviation-equivalent scale app.anomaly.robust_zscore uses to flag "
            "individual points — here applied to the window's overall center, not a single observation."
        ),
    }


def compute_variance_change_evidence(window_series: pd.Series) -> dict[str, Any]:
    """Reuses app.timeseries.change_detection's variance-shift candidate
    detector unmodified — the same plain effect-size statistic used for
    Phase 2's change candidates, applied here as one input to condition
    evidence rather than as a standalone alert."""
    candidates = change_detection.detect_variance_shift_candidates(
        window_series, window_size=VARIANCE_CHANGE_WINDOW_SIZE, min_ratio=VARIANCE_CHANGE_MIN_RATIO
    )
    return {
        "method": "windowed_variance_ratio",
        "candidates": candidates,
        "candidate_count": len(candidates),
        "note": "Reuses app.timeseries.change_detection's variance-shift statistic (see docs/TEMPORAL_INTELLIGENCE.md §8).",
    }


def compute_cross_sensor_association(
    aligned: Optional[pd.DataFrame], *, sensor_id: str
) -> dict[str, Any]:
    if aligned is None or aligned.shape[1] < 2:
        return {"computed": False, "note": "No sibling sensors on the same asset with overlapping data in this window."}
    result = multi_sensor.correlation_matrix(aligned)
    relevant = [p for p in result["pairs"] if p["sensor_a"] == sensor_id or p["sensor_b"] == sensor_id]
    return {"computed": True, "method": result["method"], "pairs": relevant, "caveat": result["caveat"]}


def compute_condition_evidence(
    *,
    sensor_id: str,
    sensor_unit: str,
    expected_interval_seconds: Optional[float],
    window_series: pd.Series,
    window_start: datetime,
    window_end: datetime,
    reference_series: pd.Series,
    reference_start: datetime,
    reference_end: datetime,
    anomalies: list[dict[str, Any]],
    aligned_sibling_series: Optional[pd.DataFrame],
) -> dict[str, Any]:
    """Assembles all five evidence types for one sensor over one window.
    Pure function — every input is already-loaded data; DB access and
    organization scoping happen in app.services.condition_service."""
    profile = profiling.compute_profile(
        window_series, sensor_unit=sensor_unit, expected_interval_seconds=expected_interval_seconds,
        requested_start=window_start, requested_end=window_end,
    )

    deviation = compute_deviation_from_baseline(
        window_series, reference_series=reference_series, reference_start=reference_start, reference_end=reference_end
    )
    if reference_end > window_start:
        # This endpoint is a stateless, exploratory viewer (like Phase 2's
        # profiling/analysis endpoints) and does not reject an overlapping
        # reference period the way app.services.condition_service.
        # create_condition_run does for a persisted signal — but it must
        # never silently present an overlapping "baseline" as if it were an
        # independent comparison.
        deviation["reference_overlaps_window"] = True
        deviation["overlap_warning"] = (
            "The reference period overlaps or follows the analysis window — this is not an independent "
            "baseline comparison. For a causally-clean comparison, choose a reference period that ends "
            "at or before the analysis window starts."
        )

    return {
        "sensor_id": sensor_id,
        "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
        "observation_count": len(window_series),
        "trend_evidence": profile["trend"],
        "persistent_anomaly_evidence": summarize_anomaly_persistence(anomalies, window_start=window_start, window_end=window_end),
        "deviation_from_baseline": deviation,
        "variance_change_evidence": compute_variance_change_evidence(window_series),
        "cross_sensor_association": compute_cross_sensor_association(aligned_sibling_series, sensor_id=sensor_id),
        "composite_score": None,
        "composite_score_note": NO_COMPOSITE_SCORE_NOTE,
    }
