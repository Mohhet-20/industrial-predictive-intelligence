"""Short-horizon trend forecasting — implemented ONLY where a real,
time-aware, leak-free holdout evaluation shows it beats naive baselines
(Phase 4 §4).

This is deliberately NOT failure prediction, NOT remaining-useful-life
estimation, and NOT a general-purpose forecaster. It is a narrow,
honestly-evaluated extrapolation of a sensor's own recent statistical
behavior a short distance into the future, with an uncertainty band drawn
from the in-sample residual distribution — not a calibrated probability.

Why a short-horizon statistical forecast is defensible here where general
predictive maintenance is not (see docs/CONDITION_INTELLIGENCE.md's
Phase-4 evidence audit — no failure labels, no maintenance history exist
for this platform): the forecast target is the sensor's OWN held-out
future value, the same self-supervised target every baseline below is
also evaluated against. No failure label is required to score it
honestly, and it is never surfaced as a maintenance recommendation — only
as a labeled, baseline-compared statistical projection a reviewer can take
or leave.

Baselines (always computed, always reported alongside any candidate —
Phase 4 §7's mandatory baseline comparison):
  naive_last_value   forecast(t) = the last value observed at/before the
                      fit cutoff. The standard, famously hard-to-beat
                      baseline for slow-moving industrial sensor data.
  seasonal_naive      forecast(t) = the value observed exactly
                      `seasonal_period_seconds` before t, if it falls
                      within the fit window. Appropriate here because this
                      fleet has real daily seasonality (see
                      app.synthetic.generator) and industrial equipment
                      commonly does too — a method that ignores this will
                      lose fairly to a method that captures it.

Candidate:
  linear_trend        Ordinary least squares over the fit window (the
                      exact method app.timeseries.profiling uses for its
                      descriptive trend line — scipy.stats.linregress),
                      extrapolated forward. Prediction interval = the
                      in-sample residual standard deviation times a fixed
                      z-multiplier — a rough, honestly-labeled spread, not
                      a fitted or calibrated confidence interval.

Evaluation method (leak-free): fit uses only observations at or before
`fit_end`. Every held-out point evaluated is a REAL observation strictly
after `fit_end` (never an invented or interpolated one) — each method
predicts that timestamp using only fit-window information, exactly as it
would need to if deployed. MAE/RMSE are computed only over points a method
could actually produce a prediction for (e.g. seasonal_naive abstains when
the lookback point isn't in the fit window), and the count evaluated is
always reported alongside the metric.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

MIN_FIT_OBSERVATIONS = 40
MIN_TEST_POINTS = 10
MIN_IMPROVEMENT_FRACTION = 0.10  # candidate must beat EVERY baseline's MAE by at least this fraction
PREDICTION_INTERVAL_Z = 1.645  # ~90% of a normal distribution's mass — stated, not implied precise
MAX_FORECAST_POINTS = 200

BASELINE_METHODS = ("naive_last_value", "seasonal_naive")
CANDIDATE_METHOD = "linear_trend"


@dataclass
class ForecastEvaluation:
    method: str
    mae: Optional[float]
    rmse: Optional[float]
    points_evaluated: int
    points_total: int
    note: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _fit_linear_trend(series: pd.Series) -> Optional[dict[str, Any]]:
    n = len(series)
    if n < 2:
        return None
    t0 = series.index[0]
    x_hours = np.array([(ts - t0).total_seconds() / 3600.0 for ts in series.index])
    y = series.values.astype("float64")
    slope, intercept, r_value, p_value, std_err = scipy_stats.linregress(x_hours, y)
    residuals = y - (slope * x_hours + intercept)
    residual_std = float(np.std(residuals, ddof=1)) if n > 2 else float(np.std(residuals))
    return {"t0": t0, "slope": float(slope), "intercept": float(intercept), "r_squared": float(r_value**2), "p_value": float(p_value), "residual_std": residual_std}


def _predict_linear_trend(fit: dict[str, Any], timestamp: pd.Timestamp) -> tuple[float, float, float]:
    x_hours = (timestamp - fit["t0"]).total_seconds() / 3600.0
    value = fit["slope"] * x_hours + fit["intercept"]
    spread = PREDICTION_INTERVAL_Z * fit["residual_std"]
    return value, value - spread, value + spread


def _predict_seasonal_naive(fit_series: pd.Series, timestamp: pd.Timestamp, *, seasonal_period_seconds: float, tolerance_seconds: float) -> Optional[float]:
    target = timestamp - pd.Timedelta(seconds=seasonal_period_seconds)
    if target < fit_series.index[0] or target > fit_series.index[-1]:
        return None
    idx = fit_series.index.get_indexer([target], method="nearest")[0]
    nearest_ts = fit_series.index[idx]
    if abs((nearest_ts - target).total_seconds()) > tolerance_seconds:
        return None
    return float(fit_series.iloc[idx])


def _mae_rmse(errors: list[float]) -> tuple[Optional[float], Optional[float]]:
    if not errors:
        return None, None
    arr = np.array(errors)
    return float(np.mean(np.abs(arr))), float(np.sqrt(np.mean(arr**2)))


def backtest_and_compare(
    full_series: pd.Series,
    *,
    fit_start: datetime,
    fit_end: datetime,
    test_end: datetime,
    seasonal_period_seconds: float = 86400.0,
    grid_tolerance_seconds: float = 900.0,
) -> dict[str, Any]:
    """Fits on [fit_start, fit_end) and evaluates every method against
    every REAL observation strictly after fit_end up to test_end. Leak-free
    by construction: no fitting step ever sees a timestamp > fit_end."""
    fit_series = full_series[(full_series.index >= pd.Timestamp(fit_start)) & (full_series.index < pd.Timestamp(fit_end))].sort_index()
    test_series = full_series[(full_series.index >= pd.Timestamp(fit_end)) & (full_series.index <= pd.Timestamp(test_end))].sort_index()

    n_fit, n_test = len(fit_series), len(test_series)
    if n_fit < MIN_FIT_OBSERVATIONS:
        return {"computed": False, "reason": f"insufficient fit-window observations (n={n_fit} < {MIN_FIT_OBSERVATIONS})", "fit_observation_count": n_fit, "test_observation_count": n_test}
    if n_test < MIN_TEST_POINTS:
        return {"computed": False, "reason": f"insufficient held-out test observations (n={n_test} < {MIN_TEST_POINTS})", "fit_observation_count": n_fit, "test_observation_count": n_test}

    last_fit_value = float(fit_series.iloc[-1])
    trend_fit = _fit_linear_trend(fit_series)

    naive_errors, seasonal_errors, trend_errors = [], [], []
    seasonal_evaluated = 0
    for ts, actual in test_series.items():
        naive_errors.append(last_fit_value - float(actual))

        seasonal_pred = _predict_seasonal_naive(fit_series, ts, seasonal_period_seconds=seasonal_period_seconds, tolerance_seconds=grid_tolerance_seconds)
        if seasonal_pred is not None:
            seasonal_errors.append(seasonal_pred - float(actual))
            seasonal_evaluated += 1

        trend_pred, _, _ = _predict_linear_trend(trend_fit, ts)
        trend_errors.append(trend_pred - float(actual))

    naive_mae, naive_rmse = _mae_rmse(naive_errors)
    seasonal_mae, seasonal_rmse = _mae_rmse(seasonal_errors)
    trend_mae, trend_rmse = _mae_rmse(trend_errors)

    evaluations = {
        "naive_last_value": ForecastEvaluation("naive_last_value", naive_mae, naive_rmse, len(naive_errors), n_test).to_dict(),
        "seasonal_naive": ForecastEvaluation(
            "seasonal_naive", seasonal_mae, seasonal_rmse, seasonal_evaluated, n_test,
            note=None if seasonal_evaluated == n_test else f"only {seasonal_evaluated}/{n_test} test points had a lookback observation within the fit window",
        ).to_dict(),
        "linear_trend": ForecastEvaluation("linear_trend", trend_mae, trend_rmse, len(trend_errors), n_test).to_dict(),
    }

    is_justified, justification_note = _is_justified(evaluations)

    return {
        "computed": True,
        "fit_observation_count": n_fit,
        "test_observation_count": n_test,
        "fit_period": {"start": fit_start.isoformat(), "end": fit_end.isoformat()},
        "test_period": {"start": fit_end.isoformat(), "end": test_end.isoformat()},
        "evaluations": evaluations,
        "is_justified": is_justified,
        "justification_note": justification_note,
        "trend_fit_summary": {"slope_per_hour": trend_fit["slope"], "r_squared": trend_fit["r_squared"], "p_value": trend_fit["p_value"]},
        "note": (
            "MAE/RMSE are prediction errors on REAL held-out observations strictly after the fit "
            "cutoff — never on interpolated or synthetic points. Lower is better. This validates "
            "whether linear_trend earns its keep against naive_last_value and seasonal_naive; it is "
            "not a claim about failure prediction."
        ),
    }


def _is_justified(evaluations: dict[str, dict[str, Any]]) -> tuple[bool, str]:
    candidate = evaluations[CANDIDATE_METHOD]
    if candidate["mae"] is None:
        return False, "linear_trend produced no evaluable predictions."

    comparisons = []
    for name in BASELINE_METHODS:
        baseline = evaluations[name]
        if baseline["mae"] is None or baseline["points_evaluated"] < MIN_TEST_POINTS:
            return False, f"baseline '{name}' had too few evaluable points to compare against ({baseline['points_evaluated']})."
        if baseline["mae"] == 0:
            return False, f"baseline '{name}' had zero error on held-out data — no room to improve on, and likely a degenerate test window."
        improvement = (baseline["mae"] - candidate["mae"]) / baseline["mae"]
        comparisons.append((name, improvement))
        if improvement < MIN_IMPROVEMENT_FRACTION:
            return False, (
                f"linear_trend (MAE={candidate['mae']:.4g}) does not beat baseline '{name}' "
                f"(MAE={baseline['mae']:.4g}) by the required {MIN_IMPROVEMENT_FRACTION:.0%} margin "
                f"(actual improvement: {improvement:.1%}). The sensor's future behavior in this window "
                "is not better explained by a linear trend than by simple persistence/seasonality."
            )

    detail = "; ".join(f"{name}: {imp:.1%} improvement" for name, imp in comparisons)
    return True, f"linear_trend beats every baseline by at least {MIN_IMPROVEMENT_FRACTION:.0%} ({detail})."


def generate_forecast_points(
    full_series: pd.Series, *, as_of: datetime, horizon_seconds: float, step_seconds: float
) -> list[dict[str, Any]]:
    """Refits linear_trend on all data at/before `as_of` and produces
    points forward. Only meaningful to call after backtest_and_compare has
    confirmed is_justified=True for this sensor/window — the caller
    (app.services.condition_service) enforces that gate, not this
    function, so this stays a pure, independently-testable computation."""
    fit_series = full_series[full_series.index <= pd.Timestamp(as_of)].sort_index()
    if len(fit_series) < 2:
        return []
    fit = _fit_linear_trend(fit_series)

    n_points = min(MAX_FORECAST_POINTS, max(1, int(horizon_seconds // step_seconds)))
    points = []
    for i in range(1, n_points + 1):
        ts = pd.Timestamp(as_of) + pd.Timedelta(seconds=i * step_seconds)
        value, lower, upper = _predict_linear_trend(fit, ts)
        points.append({
            "target_timestamp": ts.to_pydatetime().isoformat(),
            "predicted_value": value,
            "lower_bound": lower,
            "upper_bound": upper,
        })
    return points
