"""Unified scoring glue — turns a detector's raw candidate dict (from
app.anomaly.robust_zscore or app.anomaly.isolation_forest_detector) into
the fields an Anomaly row needs, applying threshold/severity logic
consistently across both detectors.

The two detectors' `raw_score` values are on DIFFERENT, NON-COMPARABLE
scales: a robust z-score of 4.0 and an Isolation Forest score of 0.2 do
not mean "the same amount of anomalous." Nothing in this module (or
anywhere else in the codebase) ranks or averages scores across detector
types — severity is always derived from a candidate's own detector's own
threshold (see app.anomaly.thresholds), and any UI/API sort that mixes
detector types sorts by severity_tier, never by raw_score, for exactly
this reason.
"""
from typing import Any

from app.anomaly.thresholds import ThresholdConfig, derive_severity_tier
from app.models.intelligence import DetectorType

DETECTOR_SCORE_DESCRIPTIONS = {
    DetectorType.ROBUST_ZSCORE: (
        "Robust z-score: (value - reference median) / (reference MAD x 1.4826). "
        "Signed — positive means above the reference median, negative means below. "
        "Magnitude is in 'robust standard deviations from the reference period.'"
    ),
    DetectorType.ISOLATION_FOREST: (
        "Negated Isolation Forest decision_function, evaluated on causally-computed "
        "feature vectors. Higher (more positive) means more isolated from the reference "
        "period's feature distribution. Not on a bounded or standardized scale by itself — "
        "see normalized_score for this candidate's percentile rank within its own model's "
        "reference-period scores."
    ),
    DetectorType.MAHALANOBIS: (
        "Robust Mahalanobis distance (sqrt of squared distance from a Minimum-Covariance-"
        "Determinant fit) between this point's sensor readings and how those sensors "
        "normally move together in the reference period. A statistical association measure, "
        "not causal — see normalized_score for this candidate's percentile rank within its "
        "own model's reference-period distances."
    ),
}


def build_anomaly_fields(candidate: dict[str, Any], *, detector_type: DetectorType, threshold_config: ThresholdConfig) -> dict[str, Any]:
    """candidate is one dict from a detector's `candidates` list (both
    detectors share the same shape: period_start/end, raw_score,
    optional normalized_score, explanation, ...). Returns the subset of
    fields needed to construct an Anomaly ORM row, plus a human summary."""
    raw_score = candidate["raw_score"]
    severity_tier = derive_severity_tier(raw_score, threshold_config)
    explanation = candidate.get("explanation", {})
    return {
        "period_start": candidate["period_start"],
        "period_end": candidate["period_end"],
        "raw_score": raw_score,
        "normalized_score": candidate.get("normalized_score"),
        "threshold_used": threshold_config.threshold,
        "severity_tier": severity_tier,
        "contributing_signals": explanation,
        "summary": explanation.get("description", f"{detector_type.value} flagged this period (score {raw_score:.3g})."),
    }
