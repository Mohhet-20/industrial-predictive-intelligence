"""Change & behavior candidate detection.

Everything this module returns is a CANDIDATE — a point worth a human
(or, later, a properly validated anomaly-detection model) looking at, not
a confirmed anomaly and not a fault diagnosis. The scoring is a plain,
inspectable effect-size statistic (a standardized mean/variance
difference between two adjacent windows), not a black-box model, so every
candidate can be explained in one sentence: "the average shifted from X to
Y between these two timestamps."

Method, in full: slide a comparison point across the series (bounded to a
manageable number of candidate points for large series); at each point,
compare a `window_size`-observation window immediately before it to a
`window_size`-observation window immediately after it; score the
difference; keep points whose score exceeds `min_score`; merge candidates
that fall within `merge_distance` observations of each other, keeping the
strongest. This is deliberately simple and auditable rather than a fitted
model — see docs/TEMPORAL_INTELLIGENCE.md for why.
"""
from typing import Any

import numpy as np
import pandas as pd

MAX_CANDIDATE_POINTS = 400  # cap on how many split points we evaluate, for bounded compute
MAX_CANDIDATES_RETURNED = 20


def _strength_label(score: float) -> str:
    abs_score = abs(score)
    if abs_score >= 5.0:
        return "strong"
    if abs_score >= 3.0:
        return "moderate"
    return "weak"


def _merge_nearby(candidates: list[dict[str, Any]], merge_distance: int) -> list[dict[str, Any]]:
    if not candidates:
        return candidates
    candidates = sorted(candidates, key=lambda c: c["_index"])
    merged: list[dict[str, Any]] = [candidates[0]]
    for c in candidates[1:]:
        if c["_index"] - merged[-1]["_index"] <= merge_distance:
            if abs(c["score"]) > abs(merged[-1]["score"]):
                merged[-1] = c
        else:
            merged.append(c)
    return merged


def detect_level_shift_candidates(
    series: pd.Series, *, window_size: int = 20, min_score: float = 3.0
) -> list[dict[str, Any]]:
    """Candidates where the mean of the window after a point differs
    sharply (in pooled-standard-deviation units) from the window before
    it — a plain-language "the average jumped" detector."""
    n = len(series)
    if n < window_size * 2 + 1:
        return []

    ordered = series.sort_index()
    values = ordered.values.astype("float64")
    step = max(1, n // MAX_CANDIDATE_POINTS)

    candidates: list[dict[str, Any]] = []
    for i in range(window_size, n - window_size, step):
        before = values[i - window_size : i]
        after = values[i : i + window_size]
        pooled_std = np.sqrt((before.var(ddof=1) + after.var(ddof=1)) / 2.0)
        if pooled_std == 0 or np.isnan(pooled_std):
            continue
        score = (after.mean() - before.mean()) / pooled_std
        if abs(score) >= min_score:
            candidates.append(
                {
                    "_index": i,
                    "type": "level_shift",
                    "timestamp": ordered.index[i].to_pydatetime().isoformat(),
                    "score": float(score),
                    "strength": _strength_label(score),
                    "mean_before": float(before.mean()),
                    "mean_after": float(after.mean()),
                    "window_observation_count": window_size,
                    "description": (
                        f"Mean shifted from {before.mean():.3g} to {after.mean():.3g} "
                        f"across {window_size}-observation windows on either side of this point."
                    ),
                }
            )

    merged = _merge_nearby(candidates, merge_distance=window_size)
    merged.sort(key=lambda c: abs(c["score"]), reverse=True)
    for c in merged:
        del c["_index"]
    return merged[:MAX_CANDIDATES_RETURNED]


def detect_variance_shift_candidates(
    series: pd.Series, *, window_size: int = 20, min_ratio: float = 2.5
) -> list[dict[str, Any]]:
    """Candidates where the spread (variance) of the window after a point
    differs sharply from the window before it — catches a sensor becoming
    noisier or suddenly quieter, which a mean-based detector would miss."""
    n = len(series)
    if n < window_size * 2 + 1:
        return []

    ordered = series.sort_index()
    values = ordered.values.astype("float64")
    step = max(1, n // MAX_CANDIDATE_POINTS)

    candidates: list[dict[str, Any]] = []
    for i in range(window_size, n - window_size, step):
        before = values[i - window_size : i]
        after = values[i : i + window_size]
        var_before, var_after = before.var(ddof=1), after.var(ddof=1)
        if var_before <= 1e-12:
            continue
        ratio = var_after / var_before
        if ratio >= min_ratio or ratio <= (1 / min_ratio):
            score = np.log(ratio) if ratio > 0 else 0.0
            candidates.append(
                {
                    "_index": i,
                    "type": "variance_shift",
                    "timestamp": ordered.index[i].to_pydatetime().isoformat(),
                    "score": float(score),
                    "strength": _strength_label(score * 2),  # log-ratio scale differs from z-score scale
                    "variance_before": float(var_before),
                    "variance_after": float(var_after),
                    "variance_ratio": float(ratio),
                    "window_observation_count": window_size,
                    "description": (
                        f"Variance changed by {ratio:.1f}x across {window_size}-observation windows "
                        f"on either side of this point."
                    ),
                }
            )

    merged = _merge_nearby(candidates, merge_distance=window_size)
    merged.sort(key=lambda c: abs(c["score"]), reverse=True)
    for c in merged:
        del c["_index"]
    return merged[:MAX_CANDIDATES_RETURNED]


def detect_change_candidates(
    series: pd.Series, *, window_size: int = 20, min_level_score: float = 3.0, min_variance_ratio: float = 2.5
) -> dict[str, Any]:
    n = len(series)
    if n < window_size * 2 + 1:
        return {
            "candidates": [],
            "methodology": "windowed mean/variance comparison",
            "window_observation_count": window_size,
            "note": f"insufficient data for change detection (n={n} < {window_size * 2 + 1} required for window_size={window_size})",
        }

    level_candidates = detect_level_shift_candidates(series, window_size=window_size, min_score=min_level_score)
    variance_candidates = detect_variance_shift_candidates(series, window_size=window_size, min_ratio=min_variance_ratio)

    combined = sorted(level_candidates + variance_candidates, key=lambda c: abs(c["score"]), reverse=True)
    return {
        "candidates": combined[:MAX_CANDIDATES_RETURNED],
        "methodology": "windowed mean/variance comparison (not a fitted model — see docs/TEMPORAL_INTELLIGENCE.md)",
        "window_observation_count": window_size,
        "note": (
            "These are statistical CANDIDATES for human review, not confirmed anomalies or fault "
            "diagnoses. A rigorous, validated anomaly-detection layer is planned for a later phase."
        ),
    }
