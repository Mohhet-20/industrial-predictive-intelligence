from datetime import datetime, timedelta, timezone

import pandas as pd

from app.timeseries.change_detection import detect_change_candidates

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _series(values, step_minutes=5):
    index = pd.DatetimeIndex([BASE + timedelta(minutes=step_minutes * i) for i in range(len(values))], tz="UTC")
    return pd.Series(values, index=index, dtype="float64")


def test_level_shift_is_detected_as_a_candidate_not_an_anomaly():
    values = [50.0] * 40 + [80.0] * 40  # a clean, large step
    series = _series(values)
    result = detect_change_candidates(series, window_size=15)
    assert len(result["candidates"]) >= 1
    assert result["candidates"][0]["type"] == "level_shift"
    assert "candidate" in result["note"].lower() or "CANDIDATE" in result["note"]
    assert "not confirmed" in result["note"] or "not a" in result["note"].lower()


def test_flat_series_produces_no_false_candidates():
    values = [50.0] * 80
    series = _series(values)
    result = detect_change_candidates(series, window_size=15)
    assert result["candidates"] == []


def test_variance_shift_is_detected_independently_of_mean():
    import random

    random.seed(7)
    quiet = [50.0 + (0.01 if i % 2 == 0 else -0.01) for i in range(40)]
    noisy = [50.0 + ((-1) ** i) * (i % 10) for i in range(40)]
    series = _series(quiet + noisy)
    result = detect_change_candidates(series, window_size=15, min_variance_ratio=2.0)
    types = {c["type"] for c in result["candidates"]}
    assert "variance_shift" in types


def test_insufficient_data_reports_reason_not_empty_silence():
    series = _series([50.0] * 5, step_minutes=5)
    result = detect_change_candidates(series, window_size=20)
    assert result["candidates"] == []
    assert "insufficient data" in result["note"]


def test_candidates_are_bounded_and_sorted_by_strength():
    # many alternating shifts to try to generate more than MAX_CANDIDATES_RETURNED
    values = []
    for i in range(30):
        values.extend([50.0] * 10 if i % 2 == 0 else [90.0] * 10)
    series = _series(values)
    result = detect_change_candidates(series, window_size=8, min_level_score=2.0)
    scores = [abs(c["score"]) for c in result["candidates"]]
    assert scores == sorted(scores, reverse=True)
    assert len(result["candidates"]) <= 20


def test_change_detection_is_reproducible_for_identical_input():
    values = [50.0] * 40 + [80.0] * 40
    series = _series(values)
    r1 = detect_change_candidates(series, window_size=15)
    r2 = detect_change_candidates(series, window_size=15)
    assert r1 == r2
