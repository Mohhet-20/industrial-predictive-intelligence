"""Tests for app.evaluation.dq_impact.

Covers both halves of the data-quality impact analysis: the pure,
DB-free scenario comparisons in app.evaluation.dq_impact itself, and one
DB-backed integration test proving that a real detected gap actually
propagates into a stored Anomaly's data_quality_context field — the
"is this warning correctly propagated to downstream workflows" question
the pure functions can't answer on their own.
"""
from datetime import datetime, timedelta, timezone

import numpy as np

from app.evaluation import dq_impact

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat()


def test_gap_adjacent_to_anomaly_impact_is_deterministic_and_bounded():
    first = dq_impact.gap_adjacent_to_anomaly_impact()
    second = dq_impact.gap_adjacent_to_anomaly_impact()
    assert first == second
    assert 0.0 <= first["clean_recall"] <= 1.0
    assert 0.0 <= first["gapped_recall"] <= 1.0
    # The scenario removes points AFTER each spike, never the spike itself,
    # so recall must not be HIGHER with the gap than without it.
    assert first["gapped_recall"] <= first["clean_recall"] + 1e-9


def test_pure_gap_false_positive_check_reports_a_real_number_not_a_guess():
    result = dq_impact.pure_gap_false_positive_check()
    assert isinstance(result["robust_zscore_false_positives"], int)
    assert result["robust_zscore_false_positives"] >= 0
    # isolation_forest is allowed to be skipped (insufficient reference
    # rows for this short scenario) but must say so honestly if it is.
    if result["isolation_forest_false_positives"] is None:
        assert result["isolation_forest_skipped_reason"]


def test_gap_threshold_boundary_sweep_matches_quality_service_exactly():
    result = dq_impact.gap_threshold_boundary_sweep()
    sweep = result["sweep"]
    assert sweep["just_under_threshold"]["flagged_as_data_gap"] is False
    assert sweep["exactly_at_threshold"]["flagged_as_data_gap"] is False  # strict '>', not '>='
    assert sweep["just_over_threshold"]["flagged_as_data_gap"] is True


def test_run_all_is_reproducible():
    first = dq_impact.run_all()
    second = dq_impact.run_all()
    assert first == second
    assert len(first["analyses"]) == 3
    assert first["evaluability"] == "SYNTHETICALLY EVALUABLE"


# ---------------------------------------------------------------------------
# DB-backed integration: does a real gap propagate into a stored anomaly's
# data_quality_context?
# ---------------------------------------------------------------------------


def test_data_quality_context_propagates_onto_a_real_stored_anomaly(client, auth_headers):
    asset_resp = client.post("/api/v1/assets", headers=auth_headers, json={"code": "PUMP-DQ-EVAL", "name": "DQ Impact Test Pump", "asset_type": "pump"})
    assert asset_resp.status_code == 201, asset_resp.text
    sensor_resp = client.post(
        f"/api/v1/assets/{asset_resp.json()['id']}/sensors", headers=auth_headers,
        json={"code": "temp", "name": "Temp", "measurement_type": "temperature", "unit": "celsius", "expected_interval_seconds": 300},
    )
    assert sensor_resp.status_code == 201, sensor_resp.text
    asset, sensor = asset_resp.json(), sensor_resp.json()

    # Reference period: clean, regular readings.
    rng = np.random.default_rng(42)
    ref_values = [50.0 + float(rng.normal(0, 0.5)) for _ in range(400)]
    ref_records = [
        {"asset_code": asset["code"], "sensor_code": sensor["code"], "timestamp": _iso(BASE + timedelta(minutes=5 * i)), "value": v}
        for i, v in enumerate(ref_values)
    ]
    ref_resp = client.post("/api/v1/ingestion/measurements", headers=auth_headers, json={"data_source_name": "dq-eval", "records": ref_records})
    assert ref_resp.status_code == 200, ref_resp.text
    reference_end = BASE + timedelta(minutes=5 * 399)

    # Scoring period: TWO spikes (indices 20 and 25) with the four
    # observations between them (21-24, a 20-minute/1500s gap) removed.
    # _data_quality_context profiles exactly [anomaly.period_start,
    # anomaly.period_end] (see app.services.anomaly_service._data_quality_context
    # and its call site in run_detection) — NOT the whole scoring window —
    # so for a gap to show up in a stored anomaly's data_quality_context, it
    # must fall *inside* that anomaly's own merged event period. Two flagged
    # points 25 minutes apart (within robust_zscore's 1800s merge window)
    # merge into one event spanning the gap between them, which is well
    # over quality_service's 900s (300s x GAP_FACTOR) gap-flagging threshold.
    score_start = reference_end + timedelta(minutes=5)
    score_values = [50.0 + float(rng.normal(0, 0.5)) for _ in range(200)]
    score_values[20] += 20.0  # first spike, anchors the merged event's period_start
    score_values[25] += 20.0  # second spike, 25 minutes later, anchors period_end
    score_records = []
    for i, v in enumerate(score_values):
        if 21 <= i <= 24:  # the gap: strictly between the two spikes, inside the merged event's period
            continue
        score_records.append({"asset_code": asset["code"], "sensor_code": sensor["code"], "timestamp": _iso(score_start + timedelta(minutes=5 * i)), "value": v})
    score_resp = client.post("/api/v1/ingestion/measurements", headers=auth_headers, json={"data_source_name": "dq-eval", "records": score_records})
    assert score_resp.status_code == 200, score_resp.text
    score_end = score_start + timedelta(minutes=5 * 199)

    detector_resp = client.post(
        "/api/v1/anomaly/detectors", headers=auth_headers,
        json={"name": "dq-eval-detector", "detector_type": "robust_zscore", "sensor_ids": [sensor["id"]], "reference_start": _iso(BASE), "reference_end": _iso(reference_end)},
    )
    assert detector_resp.status_code == 201, detector_resp.text
    run_resp = client.post(f"/api/v1/anomaly/detectors/{detector_resp.json()['id']}/runs", headers=auth_headers, json={"scoring_start": _iso(score_start), "scoring_end": _iso(score_end)})
    assert run_resp.status_code == 201, run_resp.text

    anomalies = client.get("/api/v1/anomaly/anomalies", headers=auth_headers, params={"sensor_id": sensor["id"]}).json()["items"]
    assert len(anomalies) >= 1, "the two injected spikes must have been detected for this test to be meaningful"

    anomaly = anomalies[0]
    dq_context = anomaly.get("data_quality_context")
    assert dq_context is not None, "a real anomaly must carry a real, computed data_quality_context, not null"
    assert dq_context["gap_count"] >= 1, (
        f"a real 20-minute gap existed inside this anomaly's own period_start..period_end window but was "
        f"not reflected in data_quality_context: {dq_context}"
    )
