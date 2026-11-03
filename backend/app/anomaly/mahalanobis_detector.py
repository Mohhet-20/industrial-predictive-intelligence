"""Robust-covariance (Mahalanobis distance) multivariate anomaly detector.

Both existing detectors in this package are blind to one real failure
mode: a point where EVERY sensor is individually unremarkable, but the
*relationship between them* has broken down (e.g. vibration normally
tracks load, and on this reading it no longer does, even though neither
value alone is extreme). `app.anomaly.robust_zscore` only ever looks at
one sensor. `app.anomaly.isolation_forest_detector` can take multiple
sensors, but its score comes from an uninterpretable ensemble of random
trees with no closed-form statistical meaning. This module fills that
specific, named gap with a transparent, textbook multivariate method:

    1. Align every sensor in the detector's scope onto the same time grid
       (app.timeseries.multi_sensor.align_sensors_on_grid — mean
       aggregation, no interpolation; a bucket with no observation from
       every sensor is dropped, never fabricated).
    2. Fit a robust covariance estimate (MinCovDet — the Minimum
       Covariance Determinant estimator) on the reference period's
       complete rows. MinCovDet is used instead of the plain sample
       covariance for the same reason app.anomaly.baseline uses median/MAD
       instead of mean/std: a handful of outliers in the reference period
       (exactly what a real reference window might contain) can otherwise
       drag the estimate far enough to mask genuine anomalies during
       scoring.
    3. Score each scoring-window row by its squared Mahalanobis distance
       to the robust location under the robust covariance
       (`MinCovDet.mahalanobis`, sklearn's own, well-tested implementation
       — this module does not hand-invert matrices). Distance is reported
       as sqrt(squared distance) for an intuitive "robust standard
       deviations away, accounting for how the sensors normally move
       together" scale.

Explicit, stated assumptions (surfaced to the caller, not just here):
  - This assumes the sensors' joint relationship is approximately stable
    and elliptical (the same assumption any covariance-based method
    makes). It is a statistical association measure, not a causal model —
    the same caveat app.timeseries.multi_sensor states for correlation.
  - Requires at least MIN_SAMPLES_PER_DIMENSION complete rows per sensor
    dimension, in addition to the flat MIN_REFERENCE_ROWS floor, because a
    covariance matrix fit on too few samples relative to its dimensionality
    is not a reliable estimate (Phase E's own "only when its statistical
    assumptions and dimensionality are reasonable" boundary, enforced in
    code rather than left to the caller to remember).
  - Bounded to MAX_SENSORS dimensions for the same reason, and because
    this platform's assets have, at most, a handful of related sensors —
    nothing here is built or validated for high-dimensional sensor
    arrays.

Reproducibility: exactly like app.anomaly.isolation_forest_detector, no
fitted estimator is pickled. A ModelVersion stores only JSON-serializable
summary statistics; every detection run re-fits MinCovDet from the
reference window's immutable raw measurements with a fixed random_state,
which reproduces an identical fit deterministically.

Explainability: a joint Mahalanobis distance has no single "cause," but
this module also reports each sensor's own univariate robust z-score
(median/MAD, same convention as app.anomaly.baseline) alongside the joint
score. When every individual |z| is small (< INDIVIDUALLY_UNREMARKABLE_Z)
despite a flagged joint distance, that is called out explicitly — it is
precisely the case this detector exists to catch and the other two
detectors cannot.
"""
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
from sklearn.covariance import MinCovDet

from app.timeseries.multi_sensor import align_sensors_on_grid

MIN_REFERENCE_ROWS = 30
MIN_SAMPLES_PER_DIMENSION = 10  # reference rows must be >= this * n_sensors
MAX_SENSORS = 6
DEFAULT_THRESHOLD = 4.0  # robust Mahalanobis distance (sqrt of squared distance); heuristic, override via config
RANDOM_STATE = 42
TOP_CONTRIBUTING_SENSORS = 3
INDIVIDUALLY_UNREMARKABLE_Z = 2.0

ASSOCIATION_CAVEAT = (
    "This is a statistical measure of how unusual this combination of readings is relative to "
    "how these sensors have historically moved together. It assumes that relationship is "
    "approximately stable and elliptical. It is not evidence of a causal relationship between "
    "sensors, and a high score does not identify which sensor, if any, is physically at fault."
)


class InsufficientReferenceDataError(Exception):
    def __init__(self, usable_rows: int, required_rows: int, n_sensors: int):
        self.usable_rows = usable_rows
        self.required_rows = required_rows
        self.n_sensors = n_sensors
        super().__init__(
            f"Only {usable_rows} reference grid point(s) had every sensor observed (need >= "
            f"{required_rows} for {n_sensors} sensor(s) — the greater of {MIN_REFERENCE_ROWS} and "
            f"{MIN_SAMPLES_PER_DIMENSION}x the dimensionality). Widen the reference period, use fewer "
            "sensors, or use a coarser grid step."
        )


class TooManySensorsError(Exception):
    def __init__(self, n_sensors: int):
        self.n_sensors = n_sensors
        super().__init__(
            f"{n_sensors} sensors requested; this detector is bounded to {MAX_SENSORS}. A robust "
            "covariance fit on too many dimensions relative to this platform's realistic reference "
            "window sizes is not a reliable estimate."
        )


def _required_reference_rows(n_sensors: int) -> int:
    return max(MIN_REFERENCE_ROWS, MIN_SAMPLES_PER_DIMENSION * n_sensors)


def _aligned_complete_rows(
    series_by_sensor: dict[str, pd.Series], *, sensor_ids: list[str], start: datetime, end: datetime, step_seconds: float
) -> tuple[pd.DataFrame, int]:
    """Aligns every requested sensor onto one grid and drops any row where
    at least one sensor has no observation. Returns (complete_rows_frame,
    dropped_row_count)."""
    aligned = align_sensors_on_grid(series_by_sensor, start=start, end=end, freq_seconds=step_seconds)
    aligned = aligned[sensor_ids]  # fixed, deterministic column order
    total = len(aligned)
    complete = aligned.dropna()
    return complete, total - len(complete)


def _univariate_robust_stats(complete_rows: pd.DataFrame, sensor_ids: list[str]) -> dict[str, dict[str, float]]:
    stats: dict[str, dict[str, float]] = {}
    for sid in sensor_ids:
        values = complete_rows[sid].values.astype("float64")
        median = float(np.median(values))
        mad = float(np.median(np.abs(values - median)))
        stats[sid] = {"median": median, "mad": mad, "robust_std_estimate": mad * 1.4826}
    return stats


@dataclass
class TrainedMahalanobisSummary:
    sensor_ids: list[str]
    reference_row_count: int
    reference_dropped_row_count: int
    univariate_robust_stats: dict[str, dict[str, float]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_reference_summary(
    *, series_by_sensor: dict[str, pd.Series], sensor_ids: list[str], reference_start: datetime, reference_end: datetime, step_seconds: float
) -> TrainedMahalanobisSummary:
    """Computes and validates the reference period's complete-row count
    and per-sensor univariate stats — what gets persisted on the
    ModelVersion. Does NOT persist the fitted covariance itself (see
    module docstring); every detection run re-fits it from raw data."""
    if len(sensor_ids) > MAX_SENSORS:
        raise TooManySensorsError(len(sensor_ids))

    complete, dropped = _aligned_complete_rows(series_by_sensor, sensor_ids=sensor_ids, start=reference_start, end=reference_end, step_seconds=step_seconds)
    required = _required_reference_rows(len(sensor_ids))
    if len(complete) < required:
        raise InsufficientReferenceDataError(len(complete), required, len(sensor_ids))

    return TrainedMahalanobisSummary(
        sensor_ids=sensor_ids,
        reference_row_count=len(complete),
        reference_dropped_row_count=dropped,
        univariate_robust_stats=_univariate_robust_stats(complete, sensor_ids),
    )


def _fit_mcd(reference_rows: pd.DataFrame, sensor_ids: list[str]) -> MinCovDet:
    return MinCovDet(random_state=RANDOM_STATE).fit(reference_rows[sensor_ids].values)


def _top_contributing_sensors(row: pd.Series, sensor_ids: list[str], univariate_stats: dict[str, dict[str, float]]) -> list[dict[str, Any]]:
    deviations = []
    for sid in sensor_ids:
        s = univariate_stats[sid]
        std = s["robust_std_estimate"]
        value = float(row[sid])
        z = (value - s["median"]) / std if std > 1e-9 else 0.0
        deviations.append({"sensor_id": sid, "observed_value": value, "reference_median": s["median"], "z_score": float(z)})
    deviations.sort(key=lambda d: abs(d["z_score"]), reverse=True)
    return deviations[:TOP_CONTRIBUTING_SENSORS]


def detect(
    *,
    series_by_sensor: dict[str, pd.Series],
    sensor_ids: list[str],
    reference_start: datetime,
    reference_end: datetime,
    scoring_start: datetime,
    scoring_end: datetime,
    step_seconds: float,
    threshold: float,
) -> dict[str, Any]:
    """Re-fits MinCovDet on the reference window (deterministically — see
    module docstring), scores the scoring window's complete rows, and
    returns candidates whose robust Mahalanobis distance exceeds
    `threshold`."""
    if len(sensor_ids) > MAX_SENSORS:
        raise TooManySensorsError(len(sensor_ids))

    reference_rows, reference_dropped = _aligned_complete_rows(series_by_sensor, sensor_ids=sensor_ids, start=reference_start, end=reference_end, step_seconds=step_seconds)
    required = _required_reference_rows(len(sensor_ids))
    if len(reference_rows) < required:
        raise InsufficientReferenceDataError(len(reference_rows), required, len(sensor_ids))

    scoring_rows, scoring_dropped = _aligned_complete_rows(series_by_sensor, sensor_ids=sensor_ids, start=scoring_start, end=scoring_end, step_seconds=step_seconds)

    if scoring_rows.empty:
        return {
            "points_scored": 0,
            "points_flagged": 0,
            "reference_row_count": len(reference_rows),
            "reference_dropped_row_count": reference_dropped,
            "scoring_dropped_row_count": scoring_dropped,
            "candidates": [],
            "note": "No scoring-window grid point had every sensor observed — nothing could be scored.",
        }

    mcd = _fit_mcd(reference_rows, sensor_ids)
    scoring_sq_dist = mcd.mahalanobis(scoring_rows[sensor_ids].values)
    scoring_dist = np.sqrt(np.clip(scoring_sq_dist, 0, None))
    scoring_rows = scoring_rows.assign(_raw_score=scoring_dist)

    reference_sq_dist = mcd.mahalanobis(reference_rows[sensor_ids].values)
    reference_dist = np.sqrt(np.clip(reference_sq_dist, 0, None))

    univariate_stats = _univariate_robust_stats(reference_rows, sensor_ids)

    flagged = scoring_rows[scoring_rows["_raw_score"] >= threshold].sort_index()
    points_flagged = len(flagged)
    flagged_points = []
    for ts, row in flagged.iterrows():
        contributing = _top_contributing_sensors(row, sensor_ids, univariate_stats)
        normalized_score = float(100.0 * (reference_dist < row["_raw_score"]).mean())
        max_individual_z = max((abs(c["z_score"]) for c in contributing), default=0.0)
        flagged_points.append(
            {
                "timestamp": ts,
                "raw_score": float(row["_raw_score"]),
                "normalized_score": normalized_score,
                "contributing_sensors": contributing,
                "individually_unremarkable": max_individual_z < INDIVIDUALLY_UNREMARKABLE_Z,
            }
        )

    # Merge flagged points within MERGE_SECONDS of each other into one
    # event candidate — the same discipline app.anomaly.robust_zscore
    # applies, so a sustained relationship breakdown is reported as one
    # finding, not one per grid point.
    candidates = _merge_into_events(
        flagged_points, sensor_ids=sensor_ids, threshold=threshold, reference_row_count=len(reference_rows),
        reference_start=reference_start, reference_end=reference_end,
    )

    return {
        "points_scored": len(scoring_rows),
        "points_flagged": points_flagged,
        "reference_row_count": len(reference_rows),
        "reference_dropped_row_count": reference_dropped,
        "scoring_dropped_row_count": scoring_dropped,
        "candidates": candidates,
        "note": None,
    }


MERGE_SECONDS = 1800  # flagged points within 30 minutes of each other merge into one event — same convention as robust_zscore


def _merge_into_events(
    flagged_points: list[dict[str, Any]], *, sensor_ids: list[str], threshold: float, reference_row_count: int, reference_start: datetime, reference_end: datetime
) -> list[dict[str, Any]]:
    if not flagged_points:
        return []

    groups: list[list[dict[str, Any]]] = [[flagged_points[0]]]
    for p in flagged_points[1:]:
        gap = (p["timestamp"] - groups[-1][-1]["timestamp"]).total_seconds()
        if gap <= MERGE_SECONDS:
            groups[-1].append(p)
        else:
            groups.append([p])

    events = []
    for group in groups:
        strongest = max(group, key=lambda p: p["raw_score"])
        individually_unremarkable = all(p["individually_unremarkable"] for p in group)
        description = (
            f"Joint reading across {len(sensor_ids)} sensors reached {strongest['raw_score']:.2f} robust "
            f"Mahalanobis distance units from the reference relationship (peak of {len(group)} consecutive "
            f"flagged point(s)), fit on {reference_row_count} reference points "
            f"({reference_start.isoformat()} to {reference_end.isoformat()})."
        )
        if individually_unremarkable:
            description += (
                " Every individual sensor stayed within its own normal range (|z| < "
                f"{INDIVIDUALLY_UNREMARKABLE_Z:g}) throughout — this is a relationship-level deviation "
                "that a single-sensor detector would not catch."
            )
        period_start_iso = group[0]["timestamp"].to_pydatetime().isoformat()
        period_end_iso = group[-1]["timestamp"].to_pydatetime().isoformat()
        representative_iso = strongest["timestamp"].to_pydatetime().isoformat()
        events.append(
            {
                "period_start": period_start_iso,
                "period_end": period_end_iso,
                "raw_score": strongest["raw_score"],
                "normalized_score": strongest["normalized_score"],
                "point_count": len(group),
                "representative_timestamp": representative_iso,
                "representative_value": None,  # multivariate — no single "the value"
                "explanation": {
                    "method": "mahalanobis",
                    "sensor_ids": sensor_ids,
                    "threshold": threshold,
                    "contributing_sensors": strongest["contributing_sensors"],
                    "individually_unremarkable": individually_unremarkable,
                    "description": description,
                    "caveat": ASSOCIATION_CAVEAT,
                },
            }
        )

    events.sort(key=lambda e: e["raw_score"], reverse=True)
    return events
