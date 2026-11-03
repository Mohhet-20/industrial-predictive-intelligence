"""Baseline vs. scenario comparison and sensitivity analysis (Phase 11 §5,
§8, §9). Pure functions over already-computed pipeline bundles (see
app.simulation.pipeline) — no DB access, no re-computation of anything the
pipeline already produced.

No fabricated combined "future risk" number is ever computed here (§8) —
every comparison reports its own real, independently-computed delta on its
own terms, the same "no composite score" discipline
app.condition.evidence's module docstring establishes for real condition
evidence. A sensitivity sweep (§9) reports what the pipeline's real outputs
actually did across a range of magnitudes; it is never interpreted as a
physical dose-response relationship — the mandatory note on every sweep
result says so explicitly.
"""
from __future__ import annotations

from typing import Any, Optional

MANDATORY_LIMITATIONS = [
    "This is a hypothetical, non-destructive SCENARIO — a what-if exploration of real historical data with a "
    "declared modification applied, not a physical simulation and not a prediction of what will actually happen.",
    "No claim is made that this platform's statistical methods (robust z-score, OLS trend, windowed variance) "
    "capture the true physical behavior of this equipment under the hypothesized condition.",
    "Any relationship between sensors implied by a MULTI_SENSOR scenario was set explicitly by the person who "
    "configured it — this platform never infers or fabricates a causal or physical relationship between sensors.",
    "Uncertainty bands (where shown) come from in-sample residual spread on real data, not a calibrated "
    "probability of any real-world outcome.",
]


def _pct_change(baseline: Optional[float], simulated: Optional[float]) -> Optional[float]:
    if baseline is None or simulated is None or baseline == 0:
        return None
    return (simulated - baseline) / abs(baseline) * 100.0


def compare_value_summary(baseline_summary: dict[str, Any], simulated_summary: dict[str, Any]) -> dict[str, Any]:
    diff = {}
    for key in ("mean", "median", "std", "min", "max"):
        b, s = baseline_summary.get(key), simulated_summary.get(key)
        diff[key] = {
            "baseline": b, "simulated": s,
            "absolute_diff": (s - b) if (b is not None and s is not None) else None,
            "relative_diff_pct": _pct_change(b, s),
        }
    return diff


def compare_anomaly_detection(baseline: dict[str, Any], simulated: dict[str, Any]) -> dict[str, Any]:
    if not baseline.get("computed") or not simulated.get("computed"):
        return {
            "computed": False,
            "reason": baseline.get("reason") or simulated.get("reason") or "Anomaly detection was not computed for one or both series.",
        }
    b_candidates = baseline.get("candidates", [])
    s_candidates = simulated.get("candidates", [])
    return {
        "computed": True,
        "baseline_candidate_count": len(b_candidates),
        "simulated_candidate_count": len(s_candidates),
        "candidate_count_delta": len(s_candidates) - len(b_candidates),
        "baseline_points_flagged": baseline.get("points_flagged", 0),
        "simulated_points_flagged": simulated.get("points_flagged", 0),
        "simulated_candidates": s_candidates,
        "note": (
            f"{len(s_candidates)} candidate(s) on the SIMULATED series vs. {len(b_candidates)} on the real "
            "baseline series, using the identical robust z-score baseline+threshold on both sides."
        ),
    }


def compare_condition_intelligence(baseline: dict[str, Any], simulated: dict[str, Any]) -> dict[str, Any]:
    if not baseline.get("computed") or not simulated.get("computed"):
        return {
            "computed": False,
            "reason": baseline.get("reason") or simulated.get("reason") or "Condition evidence was not computed for one or both series.",
        }
    b_class = baseline.get("degradation_classification")
    s_class = simulated.get("degradation_classification")
    return {
        "computed": True,
        "baseline_stage": (b_class or {}).get("stage"),
        "simulated_stage": (s_class or {}).get("stage"),
        "stage_changed": (b_class or {}).get("stage") != (s_class or {}).get("stage"),
        "baseline_trend_direction": (baseline["evidence"]["trend_evidence"] or {}).get("direction"),
        "simulated_trend_direction": (simulated["evidence"]["trend_evidence"] or {}).get("direction"),
        "baseline_deviation_mad_units": (baseline["evidence"]["deviation_from_baseline"] or {}).get("deviation_mad_units"),
        "simulated_deviation_mad_units": (simulated["evidence"]["deviation_from_baseline"] or {}).get("deviation_mad_units"),
    }


def compare_change_candidates(baseline: dict[str, Any], simulated: dict[str, Any]) -> dict[str, Any]:
    b = baseline.get("candidates", [])
    s = simulated.get("candidates", [])
    return {
        "baseline_candidate_count": len(b),
        "simulated_candidate_count": len(s),
        "candidate_count_delta": len(s) - len(b),
        "simulated_candidates": s,
    }


def compare_forecast_backtest(baseline: dict[str, Any], simulated: dict[str, Any]) -> dict[str, Any]:
    if not baseline.get("computed") or not simulated.get("computed"):
        return {
            "computed": False,
            "reason": baseline.get("reason") or simulated.get("reason") or "Forecast backtest was not computed for one or both series.",
        }
    return {
        "computed": True,
        "baseline_is_justified": baseline.get("is_justified"),
        "simulated_is_justified": simulated.get("is_justified"),
        "baseline_linear_trend_mae": baseline.get("evaluations", {}).get("linear_trend", {}).get("mae"),
        "simulated_linear_trend_mae": simulated.get("evaluations", {}).get("linear_trend", {}).get("mae"),
        "note": (
            "Compares each series' own leak-free backtest — whether a linear trend earns its keep against naive "
            "baselines changed (or didn't) between the real and simulated data, not a forecast of a real future."
        ),
    }


def compare_bundles(baseline_bundle: dict[str, Any], simulated_bundle: dict[str, Any]) -> dict[str, Any]:
    """Assembles every real, independently-computed diff into one
    dict — never a single blended score (§8). Both bundles must come from
    app.simulation.pipeline.run_series_through_pipeline on the SAME sensor
    (same unit by construction, since both series came from that sensor's
    own real measurements)."""
    return {
        "value_summary_diff": compare_value_summary(baseline_bundle["value_summary"], simulated_bundle["value_summary"]),
        "anomaly_detection_diff": compare_anomaly_detection(baseline_bundle["anomaly_detection"], simulated_bundle["anomaly_detection"]),
        "condition_intelligence_diff": compare_condition_intelligence(baseline_bundle["condition_intelligence"], simulated_bundle["condition_intelligence"]),
        "change_candidates_diff": compare_change_candidates(baseline_bundle["change_candidates"], simulated_bundle["change_candidates"]),
        "forecast_backtest_diff": compare_forecast_backtest(baseline_bundle.get("forecast_backtest", {}), simulated_bundle.get("forecast_backtest", {})),
        "observation_count": {"baseline": baseline_bundle["observation_count"], "simulated": simulated_bundle["observation_count"]},
    }


def summarize_sensitivity_sweep(sweep_points: list[dict[str, Any]]) -> dict[str, Any]:
    """`sweep_points` is [{"magnitude": float, "comparison": <compare_bundles output>}, ...],
    one per magnitude already run through the real pipeline. This function
    only sorts and packages — it never fits a curve, never extrapolates
    beyond the tested magnitudes, and never claims the relationship between
    magnitude and outcome is linear, monotonic, or physically caused (§9)."""
    ordered = sorted(sweep_points, key=lambda p: p["magnitude"])
    return {
        "magnitudes_tested": [p["magnitude"] for p in ordered],
        "points": ordered,
        "note": (
            "Each point reruns the real analytical pipeline at a different magnitude against the same real "
            "baseline data. This shows how THIS platform's detectors and evidence respond as magnitude changes — "
            "it is not a physical sensitivity analysis and must not be read as 'this equipment would actually "
            "behave this way at this magnitude.' No trend line is fit across these points by this platform."
        ),
    }
