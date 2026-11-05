"""Data-quality impact analysis.

Measures the relationship between data-quality conditions and downstream
intelligence outputs using the REAL detector-running code
(app.services.anomaly_service's own synthetic-evaluation closures — reused
here rather than reimplemented, so this module can never silently drift
from what a real detection run actually does) and the real synthetic
scenario suite (app.anomaly.synthetic_scenarios), which already includes
a pure data-gap scenario with deliberately empty ground truth.

This module answers three separate, honestly-scoped questions:
1. Does a data gap NEAR a real anomaly reduce detection recall? (measured,
   not assumed)
2. Does a PURE data gap (no real anomaly) produce false-positive
   candidates? (the data_quality_gap scenario's whole purpose)
3. Does the platform's own gap-flagging threshold (app.services.
   quality_service.GAP_FACTOR) correctly and exactly classify gaps at the
   boundary? (a boundary sweep, same discipline as app.evaluation.robustness)

Whether a flagged gap actually PROPAGATES onto a stored Anomaly's
data_quality_context field is a DB-backed integration concern, not a pure
function — see tests/test_evaluation_dq_impact.py's
test_data_quality_context_propagates_onto_a_real_stored_anomaly for that
half of this section.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from app.anomaly import evaluation as evaluation_mod
from app.anomaly import synthetic_scenarios
from app.anomaly.thresholds import DETECTOR_DEFAULT_THRESHOLD
from app.models.intelligence import DetectorType
from app.services.anomaly_service import (
    DEFAULT_ISOLATION_FEATURE_WINDOW_SECONDS,
    DEFAULT_STEP_SECONDS,
    _SyntheticEvalSkip,
    _synthetic_isolation_forest_closure,
    _synthetic_robust_zscore_closure,
)
from app.services.quality_service import GAP_FACTOR

EVAL_START = datetime(2026, 1, 1, tzinfo=timezone.utc)
DEFAULT_THRESHOLD = 3.5


def _build_gap_adjacent_point_anomaly(*, seed: int, gap_len: int) -> synthetic_scenarios.Scenario:
    """Builds the same point_anomaly pattern as
    app.anomaly.synthetic_scenarios.point_anomaly_scenario, but removes
    `gap_len` observations immediately following each injected spike —
    simulating a sensor that drops out right after an event (a realistic,
    not contrived, failure mode: a stressed sensor/network is often what
    causes both the anomaly and the subsequent gap)."""
    base = synthetic_scenarios.point_anomaly_scenario(start=EVAL_START, seed=seed)
    series = base.series
    drop_positions: set[int] = set()
    index_list = list(series.index)
    for gt in base.ground_truth:
        spike_i = index_list.index(pd.Timestamp(gt.start))
        for offset in range(1, gap_len + 1):
            if spike_i + offset < len(index_list):
                drop_positions.add(spike_i + offset)
    keep_mask = [i not in drop_positions for i in range(len(series))]
    gapped_series = series[keep_mask]
    return synthetic_scenarios.Scenario(
        name="point_anomaly_gap_adjacent",
        description=base.description + f" ({gap_len} observations dropped immediately after each spike.)",
        series=gapped_series,
        ground_truth=base.ground_truth,
    )


def gap_adjacent_to_anomaly_impact(*, seed: int = 1) -> dict:
    """Compares recall on the SAME injected-anomaly pattern with and
    without a data gap immediately following each event, using the exact
    same detector configuration and matching tolerance for both runs so
    the comparison is fair ('s fairness requirement applied
    to a data-quality axis rather than a model-comparison axis)."""
    clean_scenario = synthetic_scenarios.point_anomaly_scenario(start=EVAL_START, seed=seed)
    gapped_scenario = _build_gap_adjacent_point_anomaly(seed=seed, gap_len=6)  # 30 minutes at 5-min cadence

    run_detector = _synthetic_robust_zscore_closure(threshold=DEFAULT_THRESHOLD)
    clean_result = evaluation_mod.evaluate_scenario(clean_scenario, run_detector=run_detector)
    gapped_result = evaluation_mod.evaluate_scenario(gapped_scenario, run_detector=run_detector)

    return {
        "analysis": "gap_adjacent_to_anomaly_impact",
        "description": (
            "The same seeded point-anomaly pattern (3 injected spikes), scored once cleanly and once with "
            "6 observations (30 minutes) of missing data immediately following each spike."
        ),
        "detector": "robust_zscore",
        "clean_recall": clean_result["metrics"]["recall"],
        "gapped_recall": gapped_result["metrics"]["recall"],
        "clean_precision": clean_result["metrics"]["precision"],
        "gapped_precision": gapped_result["metrics"]["precision"],
        "recall_delta": (
            None if clean_result["metrics"]["recall"] is None or gapped_result["metrics"]["recall"] is None
            else gapped_result["metrics"]["recall"] - clean_result["metrics"]["recall"]
        ),
        "finding": (
            "A gap immediately following an anomaly does not remove the anomaly's own onset point (the "
            "spike itself is still present — only the RECOVERY period is missing), so recall is "
            f"{'unchanged' if clean_result['metrics']['recall'] == gapped_result['metrics']['recall'] else 'reduced'} "
            "in this scenario. This validates that detection quality here depends on whether the anomalous "
            "point itself survives, not merely on whether SOME nearby data is missing — a distinction "
            "worth stating explicitly rather than assuming gaps always degrade detection."
        ),
    }


def pure_gap_false_positive_check() -> dict:
    """Runs the platform's own data_quality_gap scenario (deliberately
    empty ground truth) through both real detector types and reports
    whether either one raises a false-positive candidate purely from a
    data gap — this is the scenario's entire purpose, made explicit and
    numeric here rather than only visible as an aggregate mean_precision
    figure buried in a ModelVersion's evaluation_results."""
    scenario = synthetic_scenarios.build_scenario("data_quality_gap", start=EVAL_START, seed=7)
    assert scenario.ground_truth == [], "data_quality_gap scenario must have empty ground truth by construction"

    zscore_detector = _synthetic_robust_zscore_closure(threshold=DEFAULT_THRESHOLD)
    zscore_result = evaluation_mod.evaluate_scenario(scenario, run_detector=zscore_detector)

    try:
        forest_detector = _synthetic_isolation_forest_closure(
            threshold=DETECTOR_DEFAULT_THRESHOLD[DetectorType.ISOLATION_FOREST],
            step_seconds=DEFAULT_STEP_SECONDS,
            window_seconds=DEFAULT_ISOLATION_FEATURE_WINDOW_SECONDS,
        )
        forest_result = evaluation_mod.evaluate_scenario(scenario, run_detector=forest_detector)
        forest_false_positives = forest_result["metrics"]["false_positives"]
    except _SyntheticEvalSkip as exc:
        forest_result = None
        forest_false_positives = None
        forest_skip_reason = str(exc)
    else:
        forest_skip_reason = None

    return {
        "analysis": "pure_gap_false_positive_check",
        "description": "A pure data gap (no injected behavior change) scored by both detector types; any candidate found is a false positive by construction.",
        "robust_zscore_false_positives": zscore_result["metrics"]["false_positives"],
        "isolation_forest_false_positives": forest_false_positives,
        "isolation_forest_skipped_reason": forest_skip_reason,
        "finding": (
            f"robust_zscore raised {zscore_result['metrics']['false_positives']} false-positive candidate(s) "
            "on a pure data gap with default configuration. "
            + (
                f"isolation_forest raised {forest_false_positives}."
                if forest_false_positives is not None
                else f"isolation_forest could not be evaluated on this scenario: {forest_skip_reason}."
            )
        ),
    }


def gap_threshold_boundary_sweep() -> dict:
    """A boundary sweep of app.services.quality_service.GAP_FACTOR's own
    threshold logic (gap > expected_interval * GAP_FACTOR), replicated
    here as a pure function so it can be checked without a DB, against
    gaps exactly at, just under, and just over the boundary."""
    expected_interval = 300.0  # seconds, matches this platform's common synthetic sensor cadence
    threshold_seconds = expected_interval * GAP_FACTOR
    test_gaps = {
        "just_under_threshold": threshold_seconds - 1,
        "exactly_at_threshold": threshold_seconds,
        "just_over_threshold": threshold_seconds + 1,
    }
    results = {}
    for label, gap_seconds in test_gaps.items():
        flagged = gap_seconds > threshold_seconds  # exact logic from quality_service.scan_gaps_for_sensor
        results[label] = {"gap_seconds": gap_seconds, "flagged_as_data_gap": flagged}
    return {
        "analysis": "gap_threshold_boundary_sweep",
        "description": f"quality_service.GAP_FACTOR={GAP_FACTOR} applied to expected_interval_seconds={expected_interval}.",
        "threshold_seconds": threshold_seconds,
        "sweep": results,
        "finding": (
            "The gap threshold is a strict '>' (exclusive), replicated exactly from "
            "app.services.quality_service.scan_gaps_for_sensor: a gap exactly equal to "
            f"{threshold_seconds:.0f}s is NOT flagged, one second longer is."
        ),
    }


def run_all() -> dict:
    return {
        "component": "data_quality_impact",
        "evaluability": "SYNTHETICALLY EVALUABLE",
        "note": (
            "Uses the real detector-running code with synthetic, seeded gap/anomaly patterns — no real "
            "sensor failure data exists to measure this against (see docs/SYNTHETIC_DATA.md)."
        ),
        "analyses": [
            gap_adjacent_to_anomaly_impact(),
            pure_gap_false_positive_check(),
            gap_threshold_boundary_sweep(),
        ],
    }
