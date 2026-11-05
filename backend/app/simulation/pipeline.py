"""Runs the SAME analytical pipeline on simulated data that production
runs on real data — feature/trend computation, anomaly
detection, condition evidence, degradation classification, change-candidate
detection, and (when enough data exists) forecast backtesting.

Every function this module calls is imported unmodified from
app.anomaly.* / app.condition.* / app.timeseries.* — the exact same code
app.services.anomaly_service and app.services.condition_service call for
real sensor data. Nothing here re-implements or approximates any of that
logic; this module only decides WHICH series (real baseline vs. simulated)
each call receives, and labels the simulated side's output so it can never
be mistaken for a real detection/signal/forecast.

Scope, stated honestly: this module reuses the robust z-score detector
(app.anomaly.robust_zscore), condition evidence and degradation
classification (app.condition.evidence / app.condition.degradation),
change-candidate detection (app.timeseries.change_detection), and
short-horizon forecasting (app.condition.forecasting) — every one of these
operates on a single sensor's Series, so a simulated Series is a drop-in
substitute for a real one with no adapter needed. Isolation Forest
(app.anomaly.isolation_forest_detector) is NOT reused here: it is trained
against a real, already-persisted ModelVersion's multivariate reference
data, and there is no honest way to substitute a single perturbed sensor's
series into a multivariate feature vector without either silently
changing what the trained model represents or fabricating sibling-sensor
behavior this platform has no basis for. This is a documented scope
boundary (see docs/SCENARIO_SIMULATION.md), not a silent omission.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import pandas as pd

from app.anomaly import baseline as baseline_mod
from app.anomaly import robust_zscore
from app.condition import degradation as degradation_mod
from app.condition import evidence as evidence_mod
from app.condition import forecasting as forecasting_mod
from app.timeseries import change_detection

SIMULATED_LABEL = "SIMULATED SCENARIO RESULT"
MIN_FORECAST_FIT_FRACTION = 0.6  # of the baseline window given to the forecast fit split; the rest is the test split


def _run_anomaly_detection(
    series: pd.Series, *, reference_series: Optional[pd.Series], reference_start: Optional[datetime], reference_end: Optional[datetime], threshold: float
) -> dict[str, Any]:
    """Ad-hoc robust z-score detection: exactly
    app.anomaly.baseline.compute_baseline + app.anomaly.robust_zscore.detect,
    the same two functions app.services.anomaly_service calls for a real
    ROBUST_ZSCORE detector — just not tied to a saved ModelVersion row."""
    if reference_series is None or reference_start is None or reference_end is None:
        return {"computed": False, "reason": "No independent reference period was available to build a baseline for anomaly detection."}
    try:
        baseline = baseline_mod.compute_baseline(reference_series, reference_start=reference_start, reference_end=reference_end)
    except baseline_mod.InsufficientReferenceDataError as exc:
        return {"computed": False, "reason": str(exc)}

    result = robust_zscore.detect(series, baseline, threshold=threshold)
    return {
        "computed": True,
        "method": "robust_zscore",
        "baseline": baseline.to_dict(),
        "threshold": threshold,
        "points_scored": result["points_scored"],
        "points_flagged": result["points_flagged"],
        "candidates": [c.to_dict() if hasattr(c, "to_dict") else c for c in result["candidates"]],
        "note": result["note"],
    }


def _run_condition_evidence(
    series: pd.Series, *, sensor_id: str, sensor_unit: str, expected_interval_seconds: Optional[float],
    window_start: datetime, window_end: datetime,
    reference_series: Optional[pd.Series], reference_start: Optional[datetime], reference_end: Optional[datetime],
    anomalies: list[dict[str, Any]],
) -> dict[str, Any]:
    if reference_series is None or reference_start is None or reference_end is None:
        return {"computed": False, "reason": "No independent reference period was available for condition evidence."}
    evidence = evidence_mod.compute_condition_evidence(
        sensor_id=sensor_id, sensor_unit=sensor_unit, expected_interval_seconds=expected_interval_seconds,
        window_series=series, window_start=window_start, window_end=window_end,
        reference_series=reference_series, reference_start=reference_start, reference_end=reference_end,
        anomalies=anomalies, aligned_sibling_series=None,
    )
    evidence["cross_sensor_association"] = {
        "computed": False,
        "note": "Cross-sensor association is not computed for scenario simulation — this platform never fabricates how a sibling sensor would respond to a hypothetical change on this one (see app.simulation's package docstring).",
    }

    min_obs = degradation_mod.MIN_OBSERVATIONS_PER_SUB_WINDOW * degradation_mod.SUB_WINDOW_COUNT
    degradation_classification = None
    if len(series) >= min_obs:
        sub_window_trends = degradation_mod.compute_sub_window_trends(series, window_start=window_start, window_end=window_end)
        degradation_classification = degradation_mod.classify_degradation(
            sub_window_trends=sub_window_trends,
            deviation_evidence=evidence["deviation_from_baseline"],
            anomaly_evidence=evidence["persistent_anomaly_evidence"],
        )
    return {"computed": True, "evidence": evidence, "degradation_classification": degradation_classification}


def _run_change_detection(series: pd.Series) -> dict[str, Any]:
    return change_detection.detect_change_candidates(series)


def _run_forecast_backtest(series: pd.Series, *, seasonal_period_seconds: float) -> dict[str, Any]:
    n = len(series)
    if n < 2:
        return {"computed": False, "reason": "Fewer than 2 observations in the baseline window."}
    ordered = series.sort_index()
    fit_end_idx = max(1, int(n * MIN_FORECAST_FIT_FRACTION))
    fit_end = ordered.index[fit_end_idx - 1].to_pydatetime()
    fit_start = ordered.index[0].to_pydatetime()
    test_end = ordered.index[-1].to_pydatetime()
    if fit_end >= test_end:
        return {"computed": False, "reason": "The baseline window is too short to split into a fit and a held-out test portion."}
    return forecasting_mod.backtest_and_compare(ordered, fit_start=fit_start, fit_end=fit_end, test_end=test_end, seasonal_period_seconds=seasonal_period_seconds)


def run_series_through_pipeline(
    series: pd.Series,
    *,
    sensor_id: str,
    sensor_unit: str,
    expected_interval_seconds: Optional[float],
    window_start: datetime,
    window_end: datetime,
    reference_series: Optional[pd.Series],
    reference_start: Optional[datetime],
    reference_end: Optional[datetime],
    anomalies: list[dict[str, Any]],
    anomaly_threshold: float,
    seasonal_period_seconds: float,
    include_forecast: bool,
    is_simulated: bool,
) -> dict[str, Any]:
    """Runs every reused analytical component against ONE series (either
    the real baseline series or its perturbed counterpart) and returns a
    bundle labeled accordingly. This is the single place that decides which
    real functions get called — app.services.scenario_service never calls
    app.anomaly/app.condition directly, mirroring how
    app.services.condition_service is the only caller of app.condition.*."""
    # A missing_data_gap scenario (app.simulation.perturbations.
    # apply_missing_data_gap) represents "no reading was taken" as NaN
    # values rather than removed rows, because the universal window
    # invariant every perturbation honors (module docstring) requires
    # preserving the input's timestamp grid. A real missing interval,
    # though, never shows up to this pipeline as a placeholder value — it
    # shows up as fewer rows. Dropping NaNs here, once, at the single
    # choke point every reused function flows through, is what makes a
    # simulated gap look to app.anomaly/app.condition/app.timeseries
    # exactly like a real one would: a real observation_count has NOT
    # fabricated any interpolated value for the pipeline to analyze.
    series = series.dropna()
    bundle: dict[str, Any] = {
        "label": SIMULATED_LABEL if is_simulated else "OBSERVED (real sensor data)",
        "is_simulated": is_simulated,
        "observation_count": len(series),
        "value_summary": {
            "mean": float(series.mean()) if len(series) else None,
            "median": float(series.median()) if len(series) else None,
            "std": float(series.std(ddof=1)) if len(series) > 1 else None,
            "min": float(series.min()) if len(series) else None,
            "max": float(series.max()) if len(series) else None,
        },
        "anomaly_detection": _run_anomaly_detection(
            series, reference_series=reference_series, reference_start=reference_start, reference_end=reference_end, threshold=anomaly_threshold
        ),
        "condition_intelligence": _run_condition_evidence(
            series, sensor_id=sensor_id, sensor_unit=sensor_unit, expected_interval_seconds=expected_interval_seconds,
            window_start=window_start, window_end=window_end,
            reference_series=reference_series, reference_start=reference_start, reference_end=reference_end,
            anomalies=anomalies,
        ),
        "change_candidates": _run_change_detection(series),
    }
    if include_forecast:
        bundle["forecast_backtest"] = _run_forecast_backtest(series, seasonal_period_seconds=seasonal_period_seconds)
    else:
        bundle["forecast_backtest"] = {"computed": False, "reason": "Forecast comparison was not requested for this run."}
    return bundle


PIPELINE_FUNCTION_PROVENANCE = {
    "anomaly_detection": "app.anomaly.baseline.compute_baseline + app.anomaly.robust_zscore.detect (unmodified)",
    "condition_intelligence": "app.condition.evidence.compute_condition_evidence + app.condition.degradation.classify_degradation (unmodified)",
    "change_candidates": "app.timeseries.change_detection.detect_change_candidates (unmodified)",
    "forecast_backtest": "app.condition.forecasting.backtest_and_compare (unmodified)",
    "not_reused": "app.anomaly.isolation_forest_detector — see module docstring for why this is a documented scope boundary, not an oversight.",
}
