"""Deterministic synthetic anomaly scenarios for pipeline validation.

These exist to validate the ENGINEERING PIPELINE — that a detector can
actually find a labeled, deliberately-injected pattern, that thresholds
behave sensibly, and that data-quality artifacts (a gap) don't masquerade
as a real anomaly. They are NOT evidence of real-world detection accuracy:
industrial failure signatures are not sine waves with a spike added, and
nothing in this module should be read as a claim about production
performance. Every scenario name and every API response that uses one is
labeled synthetic (`is_synthetic_scenario=True` on the resulting Anomaly
rows — see app.models.intelligence.Anomaly).

All generation is seeded (numpy Generator, not global random state) so a
given (seed, scenario) pair always produces byte-identical series and
ground truth — required for reproducible evaluation.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

DEFAULT_STEP_SECONDS = 300.0

# Every scenario below keeps its injected anomaly at or after this fraction
# of the series, so a reference/baseline period drawn from the first
# REFERENCE_FRACTION of the series (see app.anomaly.evaluation) is always
# clean — evaluation never trains a detector's baseline on the very
# behavior it will be scored against.
REFERENCE_FRACTION = 0.3


@dataclass
class GroundTruthWindow:
    start: str
    end: str
    kind: str
    description: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Scenario:
    name: str
    description: str
    series: pd.Series
    ground_truth: list[GroundTruthWindow]

    def to_summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "observation_count": len(self.series),
            "ground_truth": [g.to_dict() for g in self.ground_truth],
        }


def _clean_series(*, start: datetime, n: int, seed: int, step_seconds: float = DEFAULT_STEP_SECONDS, base_value: float = 50.0, daily_amplitude: float = 3.0, noise_std: float = 0.4) -> pd.Series:
    rng = np.random.default_rng(seed)
    index = pd.DatetimeIndex([start + timedelta(seconds=step_seconds * i) for i in range(n)], tz="UTC")
    hours = np.array([(ts - index[0]).total_seconds() / 3600.0 for ts in index])
    seasonal = daily_amplitude * np.sin(2 * np.pi * hours / 24.0)
    noise = rng.normal(0, noise_std, size=n)
    values = base_value + seasonal + noise
    return pd.Series(values, index=index, dtype="float64")


def _window(index: pd.DatetimeIndex, start_i: int, end_i: int, kind: str, description: str) -> GroundTruthWindow:
    return GroundTruthWindow(start=index[start_i].to_pydatetime().isoformat(), end=index[min(end_i, len(index) - 1)].to_pydatetime().isoformat(), kind=kind, description=description)


def point_anomaly_scenario(*, start: datetime, seed: int = 1, n: int = 500) -> Scenario:
    series = _clean_series(start=start, n=n, seed=seed)
    rng = np.random.default_rng(seed + 1000)
    safe_start = max(50, int(n * REFERENCE_FRACTION) + 20)
    spike_indices = rng.choice(np.arange(safe_start, n - 20), size=3, replace=False)
    values = series.values.copy()
    windows = []
    for i in sorted(spike_indices):
        values[i] += 15.0 * (1 if rng.random() > 0.5 else -1)
        windows.append(_window(series.index, i, i, "point_anomaly", f"Single-observation spike injected at index {i}."))
    return Scenario("point_anomaly", "Isolated single-point spikes far outside normal range.", pd.Series(values, index=series.index), windows)


def level_shift_scenario(*, start: datetime, seed: int = 2, n: int = 500) -> Scenario:
    series = _clean_series(start=start, n=n, seed=seed)
    shift_at = n // 2
    values = series.values.copy()
    values[shift_at:] += 12.0
    window = _window(series.index, shift_at, n - 1, "level_shift", "Sudden, sustained level shift of +12 starting mid-series.")
    return Scenario("level_shift", "A sudden sustained shift in mean value that persists.", pd.Series(values, index=series.index), [window])


def variance_change_scenario(*, start: datetime, seed: int = 3, n: int = 500) -> Scenario:
    series = _clean_series(start=start, n=n, seed=seed)
    rng = np.random.default_rng(seed + 2000)
    change_start, change_end = n // 2, n // 2 + 60
    values = series.values.copy()
    values[change_start:change_end] += rng.normal(0, 6.0, size=change_end - change_start)
    window = _window(series.index, change_start, change_end - 1, "variance_change", "A burst of much higher variance (noisier readings) without a mean shift.")
    return Scenario("variance_change", "A period of sharply increased variance with the same mean.", pd.Series(values, index=series.index), [window])


def gradual_drift_scenario(*, start: datetime, seed: int = 4, n: int = 900) -> Scenario:
    series = _clean_series(start=start, n=n, seed=seed)
    drift_start = int(n * (REFERENCE_FRACTION + 0.1))
    values = series.values.copy()
    ramp = np.linspace(0, 20.0, n - drift_start)
    values[drift_start:] += ramp
    window = _window(series.index, drift_start, n - 1, "gradual_drift", "A slow linear drift upward over the back two-thirds of the series (e.g. simulating gradual wear).")
    return Scenario("gradual_drift", "Slow, sustained linear drift away from baseline.", pd.Series(values, index=series.index), [window])


def contextual_anomaly_scenario(*, start: datetime, seed: int = 5, n: int = 900) -> Scenario:
    """A value that is unremarkable in the series' overall range but is
    unusual FOR ITS TIME OF DAY (an otherwise-nighttime-low reading
    appearing at its usual daytime-high level, or vice versa) — the kind
    of anomaly a plain global z-score against overall mean/std would
    likely miss but a baseline computed against a well-chosen reference
    can still catch since the daily swing itself is only +-3, not the
    dominant source of variance here."""
    series = _clean_series(start=start, n=n, seed=seed, daily_amplitude=8.0, noise_std=0.5)
    # pick a point near a trough and push it to the peak's typical level, ~2 days in
    target_i = int(n * 0.6)
    values = series.values.copy()
    values[target_i] = float(np.max(values)) + 3.0
    window = _window(series.index, target_i, target_i, "contextual_anomaly", "A value typical of the daily peak occurring at a time when the daily trough is expected.")
    return Scenario("contextual_anomaly", "A value unusual for its context (time of day) though not extreme overall.", pd.Series(values, index=series.index), [window])


def multi_sensor_scenario(*, start: datetime, seed: int = 6, n: int = 500) -> dict[str, Any]:
    """Two correlated sensors (b tracks a) where, during one window,
    their relationship breaks — a's value is normal, but b stops
    following it. Returns both series plus ground truth, for detectors
    that operate in multivariate mode."""
    a = _clean_series(start=start, n=n, seed=seed, base_value=50.0)
    rng = np.random.default_rng(seed + 4000)
    b_values = a.values * 0.6 + 10.0 + rng.normal(0, 0.3, size=n)
    break_start, break_end = n // 2, n // 2 + 40
    b_values[break_start:break_end] = 10.0 + rng.normal(0, 0.3, size=break_end - break_start)  # b stops tracking a
    b = pd.Series(b_values, index=a.index, dtype="float64")
    window = _window(a.index, break_start, break_end - 1, "multi_sensor_pattern", "Sensor B stops tracking its usual relationship with Sensor A.")
    return {"series_by_sensor": {"sensor_a": a, "sensor_b": b}, "ground_truth": [window], "description": "Two normally-correlated sensors whose relationship breaks down for a period."}


def data_quality_false_signal_scenario(*, start: datetime, seed: int = 7, n: int = 500) -> Scenario:
    """A data GAP (missing observations), not a real behavior change.
    A well-behaved detector's candidates, if any, should be interpretable
    via data_quality_context rather than presented as a confident
    anomaly — this scenario has NO ground-truth anomaly windows on
    purpose, so any candidate a detector raises here is evaluated as a
    potential false positive."""
    series = _clean_series(start=start, n=n, seed=seed)
    gap_start, gap_end = n // 2, n // 2 + 20
    kept = series.drop(series.index[gap_start:gap_end])
    return Scenario("data_quality_gap", "A pure data gap with no real behavior change — should not be reported as a confident anomaly.", kept, [])


ALL_SCENARIOS = ["point_anomaly", "level_shift", "variance_change", "gradual_drift", "contextual_anomaly", "data_quality_gap"]


def build_scenario(name: str, *, start: datetime, seed: int = 0) -> Scenario:
    builders = {
        "point_anomaly": point_anomaly_scenario,
        "level_shift": level_shift_scenario,
        "variance_change": variance_change_scenario,
        "gradual_drift": gradual_drift_scenario,
        "contextual_anomaly": contextual_anomaly_scenario,
        "data_quality_gap": data_quality_false_signal_scenario,
    }
    if name not in builders:
        raise ValueError(f"Unknown scenario '{name}'. Known: {ALL_SCENARIOS}")
    return builders[name](start=start, seed=seed)
