"""Deterministic synthetic MULTIVARIATE anomaly scenarios — the same
engineering-validation role as app.anomaly.synthetic_scenarios, but for
`app.anomaly.mahalanobis_detector` specifically.

app.anomaly.synthetic_scenarios' scenarios are all single-sensor by
construction and cannot exercise what this detector exists for: a point
where every sensor is INDIVIDUALLY unremarkable but their relationship has
broken down. A separate, small scenario generator is the honest way to
validate that — reusing the univariate harness would either silently not
test the real capability, or require bending that module's contract.

Exactly one scenario is provided: `correlation_breakdown`. Two sensors
normally move together (sensor_b tracks sensor_a almost linearly, plus
noise). During the injected window, sensor_b's values are redrawn from
its OWN normal marginal distribution (same mean/std) but independently of
sensor_a — so neither sensor's own univariate statistics look unusual,
while the joint relationship clearly breaks. This is the textbook case a
Mahalanobis-distance detector is for and a single-sensor z-score detector
cannot catch by construction — verified directly by
`test_univariate_zscore_cannot_catch_this_scenario` alongside the
Mahalanobis evaluation itself, so the comparison is not just asserted in
prose.

As with app.anomaly.synthetic_scenarios: `is_synthetic=True` always, this
is a pipeline-correctness check (can the detector find a pattern it was
explicitly given to find), never a claim about real-world accuracy, and
every generation call is seeded (numpy Generator, no global random state)
for byte-identical reproducibility.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from app.anomaly.synthetic_scenarios import GroundTruthWindow

DEFAULT_STEP_SECONDS = 300.0
REFERENCE_FRACTION = 0.5  # the breakdown window sits well after this, same leakage-avoidance discipline as synthetic_scenarios


@dataclass
class MultivariateScenario:
    name: str
    description: str
    series_by_sensor: dict[str, pd.Series]
    ground_truth: list[GroundTruthWindow]

    def to_summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "sensor_ids": list(self.series_by_sensor.keys()),
            "observation_count": {sid: len(s) for sid, s in self.series_by_sensor.items()},
            "ground_truth": [g.to_dict() for g in self.ground_truth],
        }


def build_correlation_breakdown_scenario(
    *,
    start: datetime,
    seed: int,
    n: int = 600,
    step_seconds: float = DEFAULT_STEP_SECONDS,
    breakdown_fraction_start: float = 0.75,
    breakdown_fraction_end: float = 0.85,
) -> MultivariateScenario:
    rng = np.random.default_rng(seed)
    index = pd.DatetimeIndex([start + timedelta(seconds=step_seconds * i) for i in range(n)], tz="UTC")
    hours = np.array([(ts - index[0]).total_seconds() / 3600.0 for ts in index])

    # A real daily-cycle signal (same shape as synthetic_scenarios._clean_series)
    # with enough amplitude that the a->b relationship below genuinely
    # dominates sensor_b's noise — a flat or barely-varying sensor_a would
    # make "correlated" meaningless regardless of slope.
    sensor_a = 50.0 + 8.0 * np.sin(2 * np.pi * hours / 24.0) + rng.normal(0, 0.5, size=n)

    slope, intercept = 0.6, 10.0
    noise_b = rng.normal(0, 0.8, size=n)
    sensor_b_normal = intercept + slope * sensor_a + noise_b

    breakdown_start_idx = int(n * breakdown_fraction_start)
    breakdown_end_idx = int(n * breakdown_fraction_end)

    sensor_b = sensor_b_normal.copy()
    # Redraw sensor_b's breakdown-window values from ITS OWN marginal
    # distribution (same mean/std as its normal behavior) but with no
    # relationship to sensor_a at those timestamps — the defining property
    # of this scenario. np.random.permutation over the full normal series
    # guarantees the marginal distribution is exactly preserved (it's a
    # genuine resample of real generated values, not an invented range).
    decoupled_pool = rng.permutation(sensor_b_normal)
    sensor_b[breakdown_start_idx:breakdown_end_idx] = decoupled_pool[: breakdown_end_idx - breakdown_start_idx]

    series_a = pd.Series(sensor_a, index=index, dtype="float64")
    series_b = pd.Series(sensor_b, index=index, dtype="float64")

    ground_truth = [
        GroundTruthWindow(
            start=index[breakdown_start_idx].to_pydatetime().isoformat(),
            end=index[breakdown_end_idx - 1].to_pydatetime().isoformat(),
            kind="correlation_breakdown",
            description=(
                "sensor_b decoupled from sensor_a: values redrawn from sensor_b's own normal "
                "distribution, so neither sensor's own statistics are individually extreme."
            ),
        )
    ]

    return MultivariateScenario(
        name="correlation_breakdown",
        description=(
            "sensor_b normally tracks sensor_a (slope=0.6, intercept=10.0, plus noise). For a "
            "contiguous window, sensor_b's values are redrawn from its own normal marginal "
            "distribution but decoupled from sensor_a, breaking the joint relationship while "
            "leaving each sensor's own mean/variance unchanged."
        ),
        series_by_sensor={"sensor_a": series_a, "sensor_b": series_b},
        ground_truth=ground_truth,
    )


ALL_MULTIVARIATE_SCENARIOS = ("correlation_breakdown",)


def build_multivariate_scenario(name: str, *, start: datetime, seed: int) -> MultivariateScenario:
    if name == "correlation_breakdown":
        return build_correlation_breakdown_scenario(start=start, seed=seed)
    raise ValueError(f"Unknown multivariate scenario '{name}'.")


@dataclass
class MultivariateReferenceScoringSplit:
    series_by_sensor_reference: dict[str, pd.Series]
    series_by_sensor_scoring: dict[str, pd.Series]
    reference_start: datetime
    reference_end: datetime
    scoring_start: datetime
    scoring_end: datetime


def split_reference_and_scoring(
    series_by_sensor: dict[str, pd.Series], *, reference_fraction: float = REFERENCE_FRACTION
) -> MultivariateReferenceScoringSplit:
    """Splits every sensor's series at the SAME index cut (they share one
    index by construction here), mirroring
    app.anomaly.evaluation.split_reference_and_scoring's contract for the
    univariate case."""
    any_series = next(iter(series_by_sensor.values()))
    n = len(any_series)
    cut = max(1, int(n * reference_fraction))

    reference = {sid: s.iloc[:cut] for sid, s in series_by_sensor.items()}
    scoring = {sid: s.iloc[cut:] for sid, s in series_by_sensor.items()}
    ref_index = any_series.index[:cut]
    score_index = any_series.index[cut:]

    return MultivariateReferenceScoringSplit(
        series_by_sensor_reference=reference,
        series_by_sensor_scoring=scoring,
        reference_start=ref_index[0].to_pydatetime(),
        reference_end=ref_index[-1].to_pydatetime(),
        scoring_start=score_index[0].to_pydatetime(),
        scoring_end=score_index[-1].to_pydatetime(),
    )
