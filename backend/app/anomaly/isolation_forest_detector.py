"""Isolation Forest anomaly detector.

Builds a per-grid-point FEATURE VECTOR (not raw values) for one or more
sensors using `app.timeseries.dataset_builder.build_feature_matrix` — the
exact same causal, leakage-safe feature computation the
feature/dataset-builder layer uses elsewhere (`compute_feature_value`
internally re-filters to `timestamp <= as_of`, so nothing here needs to
re-implement or re-verify that guarantee). An `sklearn.ensemble.
IsolationForest` is fit on the reference period's feature vectors, then
used to score the scoring period's feature vectors.

Why Isolation Forest, specifically: it makes no distributional
assumption (unlike the z-score detector's implicit near-normality),
handles multivariate input naturally — useful for "multivariate mode"
below — and trains fast enough on the data volumes this platform handles
to run synchronously. It is explicitly NOT a deep-learning model: nothing
about a few thousand feature rows per sensor justifies one, and the design
this was built against is explicit that depth must be justified by data,
not used for appearance.

Multivariate mode: when a detector's config lists more than one sensor,
that sensor's features are concatenated into one wide vector per grid
point, so the model can learn relationships BETWEEN sensors (e.g. "this
pump's vibration is unremarkable alone, but is unusual given its current
temperature") rather than only per-sensor extremes. This is association
context, not causal reasoning — see app.timeseries.multi_sensor's caveat,
which applies here too.

Reproducibility without persisting a trained model object: rather than
pickling the fitted IsolationForest (a real security/complexity cost for
marginal benefit here), a ModelVersion for this detector type stores only
its JSON-serializable config (sensor ids, feature specs, reference
period) plus summary statistics. Every detection run re-fits the forest
from scratch against that same immutable config with a FIXED random_state
— given the same reference data (raw measurements are immutable, so it
is the same data), this reproduces an identical model deterministically.
This mirrors exactly how app.timeseries.dataset_builder.DatasetBuild
recomputes its matrix on demand instead of persisting it.

Explainability: sklearn's IsolationForest gives a single decision_function
score with no built-in per-feature attribution. This module approximates
"what stood out" by computing each feature's own robust-ish z-score
(reference mean/std) and reporting the features with the largest |z| as
`contributing_features` — an honest, documented approximation, not a
claim of SHAP-quality attribution.
"""
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import pandas as pd
from sklearn.ensemble import IsolationForest

from app.timeseries.dataset_builder import FeatureSpec, build_feature_matrix

MIN_REFERENCE_ROWS = 30
DEFAULT_N_ESTIMATORS = 100
DEFAULT_CONTAMINATION = "auto"
RANDOM_STATE = 42
TOP_CONTRIBUTING_FEATURES = 3


class InsufficientFeatureDataError(Exception):
    def __init__(self, usable_rows: int, total_rows: int, minimum: int = MIN_REFERENCE_ROWS):
        self.usable_rows = usable_rows
        self.total_rows = total_rows
        self.minimum = minimum
        super().__init__(
            f"Only {usable_rows} of {total_rows} reference grid points had every feature computed "
            f"(need >= {minimum}). Widen the reference period, shrink the feature windows, or reduce "
            "the number of required features."
        )


def _feature_matrix_to_frame(matrix: dict[str, Any], feature_names: list[str]) -> tuple[pd.DataFrame, int]:
    """Converts build_feature_matrix's row-dict output into a numeric
    DataFrame, dropping any row where at least one requested feature
    wasn't computed (IsolationForest cannot take NaN input). Returns the
    frame plus how many rows were dropped, so callers can report the
    coverage honestly rather than silently losing rows."""
    records = []
    for row in matrix["rows"]:
        values = [row["features"][name]["value"] for name in feature_names]
        if any(v is None for v in values):
            continue
        records.append({"as_of": row["as_of"], **{name: v for name, v in zip(feature_names, values)}})
    dropped = matrix["row_count_returned"] - len(records)
    frame = pd.DataFrame.from_records(records)
    return frame, dropped


@dataclass
class TrainedReferenceSummary:
    feature_names: list[str]
    reference_row_count: int
    reference_dropped_row_count: int
    feature_means: dict[str, float]
    feature_stds: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_reference_summary(
    *,
    series_by_sensor: dict[str, pd.Series],
    feature_specs: list[FeatureSpec],
    reference_start: datetime,
    reference_end: datetime,
    step_seconds: float,
) -> TrainedReferenceSummary:
    """Computes the reference-period feature matrix and summarizes it —
    this is what gets persisted on the ModelVersion (JSON-serializable,
    small). Raises InsufficientFeatureDataError if too few grid points had
    every feature computed."""
    feature_names = [s.feature_name for s in feature_specs]
    grid_rows = int((reference_end - reference_start).total_seconds() / step_seconds) + 1
    matrix = build_feature_matrix(
        series_by_sensor=series_by_sensor,
        feature_specs=feature_specs,
        grid_start=reference_start,
        grid_end=reference_end,
        step_seconds=step_seconds,
        limit=grid_rows,
        offset=0,
    )
    frame, dropped = _feature_matrix_to_frame(matrix, feature_names)
    if len(frame) < MIN_REFERENCE_ROWS:
        raise InsufficientFeatureDataError(len(frame), matrix["row_count_returned"])

    return TrainedReferenceSummary(
        feature_names=feature_names,
        reference_row_count=len(frame),
        reference_dropped_row_count=dropped,
        feature_means={name: float(frame[name].mean()) for name in feature_names},
        feature_stds={name: float(frame[name].std(ddof=1)) if len(frame) > 1 else 0.0 for name in feature_names},
    )


def _fit_forest(reference_frame: pd.DataFrame, feature_names: list[str]) -> IsolationForest:
    model = IsolationForest(
        n_estimators=DEFAULT_N_ESTIMATORS, contamination=DEFAULT_CONTAMINATION, random_state=RANDOM_STATE
    )
    model.fit(reference_frame[feature_names].values)
    return model


def detect(
    *,
    series_by_sensor: dict[str, pd.Series],
    feature_specs: list[FeatureSpec],
    reference_start: datetime,
    reference_end: datetime,
    scoring_start: datetime,
    scoring_end: datetime,
    step_seconds: float,
    threshold: float,
) -> dict[str, Any]:
    """Re-fits IsolationForest on the reference window (deterministically —
    see module docstring), scores the scoring window, and returns
    candidates for scoring-window grid points whose (negated)
    decision_function score exceeds `threshold`."""
    feature_names = [s.feature_name for s in feature_specs]

    reference_grid_rows = int((reference_end - reference_start).total_seconds() / step_seconds) + 1
    reference_matrix = build_feature_matrix(
        series_by_sensor=series_by_sensor, feature_specs=feature_specs, grid_start=reference_start, grid_end=reference_end,
        step_seconds=step_seconds, limit=reference_grid_rows, offset=0,
    )
    reference_frame, reference_dropped = _feature_matrix_to_frame(reference_matrix, feature_names)
    if len(reference_frame) < MIN_REFERENCE_ROWS:
        raise InsufficientFeatureDataError(len(reference_frame), reference_matrix["row_count_returned"])

    scoring_grid_rows = int((scoring_end - scoring_start).total_seconds() / step_seconds) + 1
    scoring_matrix = build_feature_matrix(
        series_by_sensor=series_by_sensor, feature_specs=feature_specs, grid_start=scoring_start, grid_end=scoring_end,
        step_seconds=step_seconds, limit=scoring_grid_rows, offset=0,
    )
    scoring_frame, scoring_dropped = _feature_matrix_to_frame(scoring_matrix, feature_names)

    if scoring_frame.empty:
        return {
            "points_scored": 0,
            "points_flagged": 0,
            "reference_row_count": len(reference_frame),
            "reference_dropped_row_count": reference_dropped,
            "scoring_dropped_row_count": scoring_dropped,
            "candidates": [],
            "note": "No scoring-window grid point had every requested feature computed — nothing could be scored.",
        }

    model = _fit_forest(reference_frame, feature_names)
    # sklearn convention: decision_function is HIGHER for normal points, more
    # negative for outliers. Negate so this detector's raw_score follows the
    # same "higher magnitude = more anomalous" convention used elsewhere.
    raw_scores = -model.decision_function(scoring_frame[feature_names].values)
    scoring_frame = scoring_frame.assign(raw_score=raw_scores)

    # normalized_score = this candidate's percentile rank against the
    # REFERENCE period's own score distribution (scored by the same
    # model) — "more anomalous than N% of what this model considers
    # normal." Justified because both sides come from the same fitted
    # model; it is NOT meaningful to compare this percentile against a
    # different detector's normalized_score.
    reference_scores = -model.decision_function(reference_frame[feature_names].values)

    feature_means = {name: float(reference_frame[name].mean()) for name in feature_names}
    feature_stds = {name: float(reference_frame[name].std(ddof=1)) if len(reference_frame) > 1 else 0.0 for name in feature_names}

    flagged = scoring_frame[scoring_frame["raw_score"] >= threshold].sort_values("raw_score", ascending=False)
    candidates = []
    for _, row in flagged.iterrows():
        contributing = _top_contributing_features(row, feature_names, feature_means, feature_stds)
        normalized_score = float(100.0 * (reference_scores < row["raw_score"]).mean())
        candidates.append(
            {
                "period_start": row["as_of"],
                "period_end": row["as_of"],
                "raw_score": float(row["raw_score"]),
                "normalized_score": normalized_score,
                "point_count": 1,
                "representative_timestamp": row["as_of"],
                "representative_value": None,  # multivariate/feature-based — no single "the value"
                "explanation": {
                    "method": "isolation_forest",
                    "feature_names": feature_names,
                    "threshold": threshold,
                    "contributing_features": contributing,
                    "description": (
                        "Flagged by an Isolation Forest trained on "
                        f"{len(reference_frame)} reference feature vectors "
                        f"({reference_start.isoformat()} to {reference_end.isoformat()}). "
                        "Most-deviating feature: "
                        + (contributing[0]["feature_name"] if contributing else "n/a")
                        + "."
                    ),
                },
            }
        )

    return {
        "points_scored": len(scoring_frame),
        "points_flagged": len(candidates),
        "reference_row_count": len(reference_frame),
        "reference_dropped_row_count": reference_dropped,
        "scoring_dropped_row_count": scoring_dropped,
        "candidates": candidates,
        "note": None,
    }


def _top_contributing_features(
    row: "pd.Series", feature_names: list[str], feature_means: dict[str, float], feature_stds: dict[str, float]
) -> list[dict[str, Any]]:
    deviations = []
    for name in feature_names:
        std = feature_stds.get(name, 0.0)
        mean = feature_means.get(name, 0.0)
        value = float(row[name])
        z = (value - mean) / std if std > 1e-9 else 0.0
        deviations.append({"feature_name": name, "observed_value": value, "reference_mean": mean, "z_score": float(z)})
    deviations.sort(key=lambda d: abs(d["z_score"]), reverse=True)
    return deviations[:TOP_CONTRIBUTING_FEATURES]
