"""Evaluation against the synthetic scenario suite (app.anomaly.synthetic_scenarios).

No real labeled fault data exists for this platform (see docs/SYNTHETIC_DATA.md
from the start — the demo dataset's injected spikes/step-changes are not
annotated ground truth). By design: when no
reliable labels exist, do not fabricate accuracy metrics. What CAN be
computed honestly here is precision/recall/F1 against the synthetic
scenarios' own deterministic, code-defined ground truth — which is a
genuine, reproducible measurement, but of PIPELINE correctness (can the
detector find a pattern it was explicitly given to find), not of
real-world detection accuracy. Every result this module produces is
labeled `is_synthetic: True` and every place it's surfaced says so.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import pandas as pd

from app.anomaly.synthetic_scenarios import GroundTruthWindow, REFERENCE_FRACTION, Scenario

DEFAULT_MATCH_TOLERANCE_SECONDS = 900  # a candidate within 15 minutes of a ground-truth window counts as a match


@dataclass
class ReferenceScoringSplit:
    reference_series: pd.Series
    scoring_series: pd.Series
    reference_start: datetime
    reference_end: datetime
    scoring_start: datetime
    scoring_end: datetime


def split_reference_and_scoring(series: pd.Series, *, reference_fraction: float = REFERENCE_FRACTION) -> ReferenceScoringSplit:
    n = len(series)
    cut = max(1, int(n * reference_fraction))
    reference = series.iloc[:cut]
    scoring = series.iloc[cut:]
    return ReferenceScoringSplit(
        reference_series=reference,
        scoring_series=scoring,
        reference_start=reference.index[0].to_pydatetime(),
        reference_end=reference.index[-1].to_pydatetime(),
        scoring_start=scoring.index[0].to_pydatetime(),
        scoring_end=scoring.index[-1].to_pydatetime(),
    )


@dataclass
class EvaluationMetrics:
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: Optional[float]
    recall: Optional[float]
    f1: Optional[float]
    ground_truth_window_count: int
    candidate_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _overlaps(cand_start: datetime, cand_end: datetime, gt_start: datetime, gt_end: datetime, tolerance_seconds: float) -> bool:
    from datetime import timedelta

    tol = timedelta(seconds=tolerance_seconds)
    return (cand_start - tol) <= gt_end and (cand_end + tol) >= gt_start


def match_candidates_to_ground_truth(
    candidates: list[dict[str, Any]], ground_truth: list[GroundTruthWindow], *, tolerance_seconds: float = DEFAULT_MATCH_TOLERANCE_SECONDS
) -> EvaluationMetrics:
    gt_matched = [False] * len(ground_truth)
    cand_matched = [False] * len(candidates)

    for ci, cand in enumerate(candidates):
        cand_start = datetime.fromisoformat(cand["period_start"])
        cand_end = datetime.fromisoformat(cand["period_end"])
        for gi, gt in enumerate(ground_truth):
            gt_start = datetime.fromisoformat(gt.start)
            gt_end = datetime.fromisoformat(gt.end)
            if _overlaps(cand_start, cand_end, gt_start, gt_end, tolerance_seconds):
                cand_matched[ci] = True
                gt_matched[gi] = True

    true_positives = sum(gt_matched)  # count ground-truth windows that were found at least once
    false_negatives = len(ground_truth) - true_positives
    false_positives = sum(1 for m in cand_matched if not m)

    precision = true_positives / (true_positives + false_positives) if (true_positives + false_positives) > 0 else None
    recall = true_positives / (true_positives + false_negatives) if (true_positives + false_negatives) > 0 else None
    f1 = (2 * precision * recall / (precision + recall)) if precision is not None and recall is not None and (precision + recall) > 0 else None

    return EvaluationMetrics(
        true_positives=true_positives,
        false_positives=false_positives,
        false_negatives=false_negatives,
        precision=precision,
        recall=recall,
        f1=f1,
        ground_truth_window_count=len(ground_truth),
        candidate_count=len(candidates),
    )


def evaluate_scenario(
    scenario: Scenario, *, run_detector: Callable[[ReferenceScoringSplit], list[dict[str, Any]]], tolerance_seconds: float = DEFAULT_MATCH_TOLERANCE_SECONDS
) -> dict[str, Any]:
    """Splits the scenario's series into a clean reference period and a
    scoring period (see REFERENCE_FRACTION), calls `run_detector` (a
    closure the caller builds — this module stays detector-agnostic), and
    scores the result against the scenario's own ground truth."""
    split = split_reference_and_scoring(scenario.series)
    candidates = run_detector(split)
    metrics = match_candidates_to_ground_truth(candidates, scenario.ground_truth, tolerance_seconds=tolerance_seconds)
    return {
        "scenario": scenario.name,
        "is_synthetic": True,
        "description": scenario.description,
        "reference_period": {"start": split.reference_start.isoformat(), "end": split.reference_end.isoformat()},
        "scoring_period": {"start": split.scoring_start.isoformat(), "end": split.scoring_end.isoformat()},
        "metrics": metrics.to_dict(),
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }
