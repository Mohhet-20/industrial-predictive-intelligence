"""Non-destructive scenario perturbations.

Six scenario types, each a PURE function: `series in -> new series out`.
None of these ever mutates its input `pd.Series` (every function returns a
fresh copy) and none of them uses randomness — every perturbation is a
deterministic function of the real observed values, the real timestamps,
and the caller's explicit config. That means reproducibility is
free: the same (series, config) always produces byte-identical output, on
any machine, forever — there is no seed to record because there is nothing
stochastic to seed.

Every function guarantees, and app.simulation.pipeline / tests verify, ONE
universal invariant: every timestamp OUTSIDE [modification_start,
modification_end] is numerically IDENTICAL to the input series at that
timestamp — where modification_end = modification_start +
modification_duration_seconds if a duration was given, else the last
timestamp in `series` (i.e. "to the end of the baseline window"). This
holds for every one of the six types, with no exceptions: a bounded
level shift, spike, or variability change reverts to the real observed
value once its window ends, and a trend_change/drift given an explicit
duration shorter than the baseline window likewise reverts after that
duration — it does not "hold" the accumulated change indefinitely. A
scenario that wants a change to persist for the rest of the analyzed
window simply leaves modification_duration_seconds unset (or sets it to
span the remaining window) rather than relying on any special-cased
"permanent" behavior. This single, exception-free rule is what makes the
non-destructive guarantee simple to state, simple to verify, and
impossible to get subtly wrong for one type but not another.

Also guaranteed:
  - the input Series is never modified in place — every function returns a
    fresh copy;
  - the set of timestamps is unchanged (a perturbation changes VALUES, not
    the sampling grid — see module docstring's "preserve original data").

None of this is a physical model. A "level shift" does not know or claim
anything about bearing wear, motor load, or any other physical cause of a
level shift in this sensor's real readings — it is arithmetic (add a
constant) applied to a real historical Series, honestly labeled as a
hypothesis a human is choosing to explore, not a prediction of what will
actually happen. See docs/SCENARIO_SIMULATION.md for the full disclosure
this package requires everywhere its output is surfaced.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import numpy as np
import pandas as pd

DEFAULT_VARIABILITY_ROLLING_WINDOW = 15  # observations — matches app.condition.evidence's VARIANCE_CHANGE_WINDOW_SIZE convention
DEFAULT_SPIKE_SHAPE = "rectangular"
SPIKE_SHAPES = ("rectangular", "triangular")


class PerturbationError(Exception):
    """Raised for a config that is internally inconsistent in a way the
    perturbation function itself must catch (e.g. end before start) —
    distinct from app.simulation.validation's pre-flight checks, which run
    BEFORE a perturbation is ever invoked. This is a last-line defensive
    check, not the primary validation path."""


def _ensure_utc(ts: datetime) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _resolve_window(
    series: pd.Series, *, modification_start: datetime, modification_duration_seconds: Optional[float]
) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = _ensure_utc(modification_start)
    if series.index.tz is None:
        # Test/synthetic series are sometimes tz-naive; compare in the same space.
        start = start.tz_localize(None)
    window_end = series.index.max() if len(series) else start
    end = start + pd.Timedelta(seconds=modification_duration_seconds) if modification_duration_seconds is not None else window_end
    if end < start:
        raise PerturbationError("modification_end resolved before modification_start.")
    return start, end


def _hours_since(index: pd.DatetimeIndex, start: pd.Timestamp) -> np.ndarray:
    return np.array([(ts - start).total_seconds() / 3600.0 for ts in index], dtype="float64")


def apply_level_shift(
    series: pd.Series, *, modification_start: datetime, modification_duration_seconds: Optional[float], magnitude: float, **_: Any
) -> pd.Series:
    """Adds a constant `magnitude` (in the sensor's own units) to every
    observation within [modification_start, modification_end] — "the
    average shifts to a new level." Leave modification_duration_seconds
    unset for a shift that holds for the rest of the baseline window;
    provide one for a bounded shift that reverts to the real observed
    values afterward (see module docstring's universal window invariant)."""
    out = series.copy()
    start, end = _resolve_window(series, modification_start=modification_start, modification_duration_seconds=modification_duration_seconds)
    mask = (out.index >= start) & (out.index <= end)
    out.loc[mask] = out.loc[mask] + magnitude
    return out


def apply_trend_change(
    series: pd.Series, *, modification_start: datetime, modification_duration_seconds: Optional[float], magnitude: float, **_: Any
) -> pd.Series:
    """Adds a linear ramp of rate `magnitude` (sensor units PER HOUR) on
    top of the real observed values, within [modification_start,
    modification_end] — "the underlying rate of change itself changes
    starting now." Leave modification_duration_seconds unset for a ramp
    that runs to the end of the baseline window; provide one for a ramp
    bounded to that sub-window, reverting to the real observed values
    afterward (module docstring's universal window invariant — this
    platform never silently "holds" a change past its declared window)."""
    out = series.copy()
    start, end = _resolve_window(series, modification_start=modification_start, modification_duration_seconds=modification_duration_seconds)
    mask = (out.index >= start) & (out.index <= end)
    if mask.any():
        hours = _hours_since(out.index[mask], start)
        out.loc[mask] = out.loc[mask] + magnitude * hours
    return out


def apply_variability_change(
    series: pd.Series,
    *,
    modification_start: datetime,
    modification_duration_seconds: Optional[float],
    magnitude: float,
    rolling_window_observations: int = DEFAULT_VARIABILITY_ROLLING_WINDOW,
    **_: Any,
) -> pd.Series:
    """Rescales each observation's deviation from a ROLLING MEDIAN by
    `magnitude` within the modification window — magnitude=2.0 means "twice
    as noisy," magnitude=0.5 means "half as noisy." No new randomness is
    introduced: this amplifies/dampens the real, already-observed
    deviations rather than drawing new synthetic noise, which is what keeps
    this perturbation fully deterministic and reproducible."""
    if magnitude < 0:
        raise PerturbationError("variability_change magnitude must be >= 0 (it scales existing deviation, it cannot invert it).")
    out = series.copy()
    start, end = _resolve_window(series, modification_start=modification_start, modification_duration_seconds=modification_duration_seconds)
    mask = (out.index >= start) & (out.index <= end)
    if not mask.any():
        return out

    rolling_median = series.rolling(window=max(1, rolling_window_observations), min_periods=1, center=True).median()
    deviation = series - rolling_median
    out.loc[mask] = (rolling_median.loc[mask] + magnitude * deviation.loc[mask]).values
    return out


def apply_temporary_spike(
    series: pd.Series,
    *,
    modification_start: datetime,
    modification_duration_seconds: float,
    magnitude: float,
    shape: str = DEFAULT_SPIKE_SHAPE,
    **_: Any,
) -> pd.Series:
    """Adds a bounded bump of peak height `magnitude` entirely within
    [modification_start, modification_start + duration] — outside that
    window, every value is byte-identical to the input (: "a temporary
    spike affects only its declared window"). `shape="rectangular"` adds
    the full magnitude for the whole window; `shape="triangular"` ramps
    linearly up to `magnitude` at the window's midpoint and back down to 0
    at its end — both are simple, disclosed, inspectable shapes, not a
    physical pulse model of any real fault mode."""
    if modification_duration_seconds is None or modification_duration_seconds <= 0:
        raise PerturbationError("temporary_spike requires a positive modification_duration_seconds.")
    if shape not in SPIKE_SHAPES:
        raise PerturbationError(f"Unknown spike shape '{shape}'. Supported: {SPIKE_SHAPES}.")

    out = series.copy()
    start, end = _resolve_window(series, modification_start=modification_start, modification_duration_seconds=modification_duration_seconds)
    mask = (out.index >= start) & (out.index <= end)
    if not mask.any():
        return out

    if shape == "rectangular":
        out.loc[mask] = out.loc[mask] + magnitude
    else:  # triangular
        total_seconds = (end - start).total_seconds() or 1.0
        midpoint_seconds = total_seconds / 2.0
        offsets = np.array([(ts - start).total_seconds() for ts in out.index[mask]], dtype="float64")
        # 0 at the edges, `magnitude` at the midpoint — a simple tent function.
        fraction = 1.0 - np.abs(offsets - midpoint_seconds) / midpoint_seconds if midpoint_seconds > 0 else np.ones_like(offsets)
        fraction = np.clip(fraction, 0.0, 1.0)
        out.loc[mask] = out.loc[mask] + magnitude * fraction
    return out


def apply_drift(
    series: pd.Series, *, modification_start: datetime, modification_duration_seconds: Optional[float], magnitude: float, **_: Any
) -> pd.Series:
    """Linearly accumulates a TOTAL drift of `magnitude` (sensor units,
    reached exactly at modification_end) within [modification_start,
    modification_end] — "this sensor will have drifted by this much by the
    end of this window." Leave modification_duration_seconds unset for a
    drift that accumulates smoothly across the whole rest of the baseline
    window; provide one for a drift bounded to that sub-window, reverting
    to the real observed values afterward (module docstring's universal
    window invariant). Distinction from trend_change: trend_change's
    `magnitude` is a RATE (units/hour); drift's `magnitude` is a fixed
    TOTAL amount reached by modification_end."""
    out = series.copy()
    start, end = _resolve_window(series, modification_start=modification_start, modification_duration_seconds=modification_duration_seconds)
    mask = (out.index >= start) & (out.index <= end)
    if not mask.any():
        return out

    duration_seconds = (end - start).total_seconds() or 1.0
    elapsed = np.array([(ts - start).total_seconds() for ts in out.index[mask]], dtype="float64")
    out.loc[mask] = out.loc[mask] + magnitude * (elapsed / duration_seconds)
    return out


def apply_missing_data_gap(
    series: pd.Series,
    *,
    modification_start: datetime,
    modification_duration_seconds: Optional[float],
    magnitude: Optional[float] = 1.0,
    **_: Any,
) -> pd.Series:
    """Sets observations within [modification_start, modification_end] to
    NaN ("this sensor stopped reporting for a while") rather than changing
    their values — the one perturbation type that removes data instead of
    distorting it ("missing-data interval").

    `magnitude` means something different here than for every other
    perturbation: it is a DROP FRACTION in [0, 1] of the real observations
    that fall inside the modification window, not a sensor-units value.
    1.0 (the default, and what `None` is treated as — see
    app.simulation.validation's missing_data_gap special case) drops every
    observation in the window, i.e. a clean gap. A fraction below 1.0 drops
    only that share of the in-window points, chosen deterministically by
    evenly-spaced RANK within the window — never a random sample — so this
    stays exactly as reproducible as every other perturbation (module
    docstring): the same (series, config) always drops the same points.

    Outside the window, every value is left byte-identical to the input,
    exactly like every other perturbation here (universal window
    invariant) — a gap never "bleeds" past its declared duration.
    """
    if modification_duration_seconds is None or modification_duration_seconds <= 0:
        raise PerturbationError("missing_data_gap requires a positive modification_duration_seconds.")
    effective_magnitude = 1.0 if magnitude is None else magnitude
    if not (0 <= effective_magnitude <= 1.0):
        raise PerturbationError("missing_data_gap magnitude must be a fraction in [0, 1] of the in-window observations to drop.")

    out = series.copy()
    start, end = _resolve_window(series, modification_start=modification_start, modification_duration_seconds=modification_duration_seconds)
    mask = (out.index >= start) & (out.index <= end)
    in_window_positions = np.flatnonzero(mask)
    if in_window_positions.size == 0 or effective_magnitude == 0:
        return out

    if effective_magnitude >= 1.0:
        drop_positions = in_window_positions
    else:
        n_drop = max(1, min(in_window_positions.size, int(round(effective_magnitude * in_window_positions.size))))
        # Deterministic, evenly-spaced-by-RANK selection within the window —
        # never random sampling (module docstring: no perturbation here ever
        # draws from a random distribution).
        rank_positions = np.linspace(0, in_window_positions.size - 1, num=n_drop)
        drop_idx = np.unique(np.round(rank_positions).astype(int))
        drop_positions = in_window_positions[drop_idx]

    out.iloc[drop_positions] = np.nan
    return out


_APPLIERS = {
    "level_shift": apply_level_shift,
    "trend_change": apply_trend_change,
    "variability_change": apply_variability_change,
    "temporary_spike": apply_temporary_spike,
    "drift": apply_drift,
    "missing_data_gap": apply_missing_data_gap,
}

SINGLE_SENSOR_SCENARIO_TYPES = tuple(_APPLIERS.keys())
ALL_SCENARIO_TYPES = SINGLE_SENSOR_SCENARIO_TYPES + ("multi_sensor",)


def apply_perturbation(series: pd.Series, *, scenario_type: str, modification_start: datetime, modification_duration_seconds: Optional[float], magnitude: float, parameters: Optional[dict[str, Any]] = None) -> pd.Series:
    """Single dispatch point used by app.simulation.pipeline and every
    caller — routes to the right pure function above by name. `multi_sensor`
    is NOT handled here (it has no single series — see
    app.simulation.pipeline.run_multi_sensor_scenario, which calls this
    function once per sensor with that sensor's own sub-config)."""
    applier = _APPLIERS.get(scenario_type)
    if applier is None:
        raise PerturbationError(f"Unknown or unsupported single-sensor scenario_type '{scenario_type}'.")
    params = dict(parameters or {})
    return applier(
        series, modification_start=modification_start, modification_duration_seconds=modification_duration_seconds, magnitude=magnitude, **params
    )


def verify_outside_window_unchanged(original: pd.Series, simulated: pd.Series, *, modification_start: datetime, modification_end: Optional[datetime]) -> bool:
    """Non-destructive-guarantee check: every timestamp strictly
    outside the declared modification window must be numerically identical
    between `original` and `simulated`. Used by tests, and by
    app.simulation.pipeline as a defensive assertion before a result is
    ever persisted — a perturbation that fails this is a bug, not a
    permissible scenario outcome."""
    start = _ensure_utc(modification_start)
    if original.index.tz is None:
        start = start.tz_localize(None)
    end = _ensure_utc(modification_end) if modification_end is not None else original.index.max()
    if original.index.tz is None and modification_end is not None:
        end = end.tz_localize(None)

    outside_mask = (original.index < start) | (original.index > end)
    if not outside_mask.any():
        return True
    return bool(np.allclose(original.loc[outside_mask].values, simulated.loc[outside_mask].values, equal_nan=True))
