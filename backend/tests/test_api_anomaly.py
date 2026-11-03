"""API-level tests for the Anomaly Detection Intelligence layer.

These exercise the real HTTP surface end-to-end against the real DB
(mirrors tests/test_api_timeseries.py's conventions) — not just the
app.anomaly.* pure-function unit tests, and not just app.services.
anomaly_service called directly. Deliberately includes refusal/warning
paths (insufficient reference data, non-trained detector, retrospective-
scoring guard, invalid status transitions, cross-org access, role
authorization) — a suite that only exercised the happy path would miss
exactly the failure modes this platform is supposed to handle honestly.
"""
import time
from datetime import datetime, timedelta, timezone

import numpy as np

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _make_asset_and_sensor(client, headers, *, asset_code="PUMP-ANOM", sensor_code="temp"):
    asset_resp = client.post("/api/v1/assets", headers=headers, json={"code": asset_code, "name": "Anomaly Test Pump", "asset_type": "pump"})
    assert asset_resp.status_code == 201, asset_resp.text
    sensor_resp = client.post(
        f"/api/v1/assets/{asset_resp.json()['id']}/sensors",
        headers=headers,
        json={"code": sensor_code, "name": "Temp", "measurement_type": "temperature", "unit": "celsius", "expected_interval_seconds": 300},
    )
    assert sensor_resp.status_code == 201, sensor_resp.text
    return asset_resp.json(), sensor_resp.json()


def _ingest(client, headers, *, asset_code, sensor_code, n, step_minutes=5, start=BASE, value_fn=None):
    value_fn = value_fn or (lambda i: 50.0 + (i % 5) * 0.1)
    records = [
        {"asset_code": asset_code, "sensor_code": sensor_code, "timestamp": _iso(start + timedelta(minutes=step_minutes * i)), "value": value_fn(i)}
        for i in range(n)
    ]
    resp = client.post("/api/v1/ingestion/measurements", headers=headers, json={"data_source_name": "anomaly-tests", "records": records})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ingest_with_spike(client, headers, *, asset_code, sensor_code, n_ref, n_score, spike_index, step_minutes=5, seed=0):
    rng = np.random.default_rng(seed)
    ref_values = [50.0 + float(rng.normal(0, 0.5)) for _ in range(n_ref)]
    _ingest(client, headers, asset_code=asset_code, sensor_code=sensor_code, n=n_ref, step_minutes=step_minutes, start=BASE, value_fn=lambda i: ref_values[i])

    score_start = BASE + timedelta(minutes=step_minutes * n_ref)
    score_values = [50.0 + float(rng.normal(0, 0.5)) for _ in range(n_score)]
    score_values[spike_index] += 20.0
    _ingest(
        client, headers, asset_code=asset_code, sensor_code=sensor_code, n=n_score, step_minutes=step_minutes,
        start=score_start, value_fn=lambda i: score_values[i],
    )
    reference_end = BASE + timedelta(minutes=step_minutes * (n_ref - 1))
    score_end = score_start + timedelta(minutes=step_minutes * (n_score - 1))
    return reference_end, score_start, score_end


def _viewer_headers(client, db_session, org, auth_service_module):
    from app.core.security import hash_password
    from app.models.user import User, UserRole

    viewer = User(
        organization_id=org.id, email="viewer@anomaly-tests.example", hashed_password=hash_password("ViewerPass123!"),
        full_name="Viewer", role=UserRole.VIEWER,
    )
    db_session.add(viewer)
    db_session.commit()
    token = auth_service_module.issue_token(viewer)
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Full end-to-end workflow: train -> detect -> investigate -> review
# ---------------------------------------------------------------------------


def test_robust_zscore_full_workflow_e2e(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers)
    reference_end, score_start, score_end = _ingest_with_spike(
        client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n_ref=400, n_score=200, spike_index=100
    )

    detector_resp = client.post(
        "/api/v1/anomaly/detectors",
        headers=auth_headers,
        json={
            "name": "temp-robust-zscore", "detector_type": "robust_zscore", "sensor_ids": [sensor["id"]],
            "reference_start": _iso(BASE), "reference_end": _iso(reference_end),
        },
    )
    assert detector_resp.status_code == 201, detector_resp.text
    detector = detector_resp.json()
    assert detector["status"] == "trained"
    assert detector["reference_observation_count"] == 400
    assert detector["baseline_summary"]["method"] == "median_mad"

    # detector detail + list both work
    get_resp = client.get(f"/api/v1/anomaly/detectors/{detector['id']}", headers=auth_headers)
    assert get_resp.status_code == 200
    list_resp = client.get("/api/v1/anomaly/detectors", headers=auth_headers)
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] == 1

    run_resp = client.post(
        f"/api/v1/anomaly/detectors/{detector['id']}/runs",
        headers=auth_headers,
        json={"scoring_start": _iso(score_start), "scoring_end": _iso(score_end)},
    )
    assert run_resp.status_code == 201, run_resp.text
    run = run_resp.json()
    assert run["status"] == "succeeded"  # small window -> synchronous path
    assert run["background_job_id"] is None
    assert run["metrics"]["anomalies_created"] >= 1
    assert run["metrics"]["retrospective"] is False

    anomalies_resp = client.get("/api/v1/anomaly/anomalies", headers=auth_headers, params={"sensor_id": sensor["id"]})
    assert anomalies_resp.status_code == 200
    anomalies = anomalies_resp.json()["items"]
    assert len(anomalies) >= 1
    top = anomalies[0]
    assert top["detector_type"] == "robust_zscore"
    assert top["severity_tier"] in ("observed_unusual_behavior", "candidate_anomaly", "high_priority_anomaly")
    assert top["is_synthetic_scenario"] is False
    assert top["contributing_signals"]["method"] == "robust_zscore"

    detail_resp = client.get(f"/api/v1/anomaly/anomalies/{top['id']}", headers=auth_headers)
    assert detail_resp.status_code == 200
    assert detail_resp.json()["raw_score"] == top["raw_score"]

    context_resp = client.get(f"/api/v1/anomaly/anomalies/{top['id']}/context", headers=auth_headers)
    assert context_resp.status_code == 200
    ctx = context_resp.json()["data"]
    assert ctx["during"] is not None
    assert ctx["before"] is not None or ctx["after"] is not None

    # human review lifecycle: DETECTED -> UNDER_REVIEW -> ACKNOWLEDGED
    review1 = client.post(
        f"/api/v1/anomaly/anomalies/{top['id']}/review", headers=auth_headers,
        json={"to_status": "under_review", "notes": "Looking into it."},
    )
    assert review1.status_code == 200, review1.text
    assert review1.json()["status"] == "under_review"
    assert review1.json()["raw_score"] == top["raw_score"]  # original ML evidence untouched

    review2 = client.post(
        f"/api/v1/anomaly/anomalies/{top['id']}/review", headers=auth_headers,
        json={"to_status": "acknowledged", "notes": "Confirmed a real spike."},
    )
    assert review2.status_code == 200
    assert review2.json()["status"] == "acknowledged"

    investigations_resp = client.get(f"/api/v1/anomaly/anomalies/{top['id']}/investigations", headers=auth_headers)
    assert investigations_resp.status_code == 200
    investigations = investigations_resp.json()
    assert len(investigations) == 2
    assert investigations[0]["from_status"] == "detected"
    assert investigations[0]["to_status"] == "under_review"
    assert investigations[1]["from_status"] == "under_review"
    assert investigations[1]["to_status"] == "acknowledged"
    assert investigations[0]["notes"] == "Looking into it."


def test_isolation_forest_multivariate_e2e(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-IF", sensor_code="temp")
    sensor2_resp = client.post(
        f"/api/v1/assets/{asset['id']}/sensors", headers=auth_headers,
        json={"code": "vib", "name": "Vibration", "measurement_type": "vibration", "unit": "mm/s", "expected_interval_seconds": 300},
    )
    assert sensor2_resp.status_code == 201
    sensor2 = sensor2_resp.json()

    n = 600
    reference_end = BASE + timedelta(minutes=5 * (n - 1))
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n=n, value_fn=lambda i: 50.0 + (i % 3) * 0.05)
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor2["code"], n=n, value_fn=lambda i: 2.0 + (i % 3) * 0.01)

    detector_resp = client.post(
        "/api/v1/anomaly/detectors",
        headers=auth_headers,
        json={
            "name": "pump-isoforest", "detector_type": "isolation_forest", "sensor_ids": [sensor["id"], sensor2["id"]],
            "reference_start": _iso(BASE), "reference_end": _iso(reference_end), "step_seconds": 1800,
        },
    )
    assert detector_resp.status_code == 201, detector_resp.text
    detector = detector_resp.json()
    assert detector["status"] == "trained"
    assert detector["feature_schema"] is not None
    assert len(detector["feature_schema"]["feature_names"]) == 6  # 2 sensors x 3 kinds (mean/std/rate_of_change)

    score_start = reference_end + timedelta(minutes=5)
    score_end = score_start + timedelta(hours=6)
    run_resp = client.post(
        f"/api/v1/anomaly/detectors/{detector['id']}/runs", headers=auth_headers,
        json={"scoring_start": _iso(score_start), "scoring_end": _iso(score_end)},
    )
    assert run_resp.status_code == 201, run_resp.text
    assert run_resp.json()["status"] == "succeeded"


def test_mahalanobis_multivariate_e2e(client, auth_headers):
    """End-to-end proof, through the real HTTP API and real DB (not just
    the pure-function unit tests in test_mahalanobis_detector.py), that the
    new Phase 14 detector catches a relationship-breakdown anomaly: two
    sensors that normally move together, where a scoring-window segment
    decouples sensor_b from sensor_a while leaving each sensor's own
    univariate range unremarkable — exactly the gap the module docstring
    (app.anomaly.mahalanobis_detector) says robust_zscore and
    isolation_forest cannot cover."""
    asset, sensor = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-MCD", sensor_code="a")
    sensor2_resp = client.post(
        f"/api/v1/assets/{asset['id']}/sensors", headers=auth_headers,
        json={"code": "b", "name": "Sensor B", "measurement_type": "pressure", "unit": "bar", "expected_interval_seconds": 300},
    )
    assert sensor2_resp.status_code == 201
    sensor2 = sensor2_resp.json()

    # noise_std=0.2 against slope=0.6 over a_ref's std=1.0 gives a strong
    # (~0.95) reference-period correlation — needed so that decoupling a
    # contiguous window below produces a joint-distance anomaly that
    # clears the default threshold, the same consideration documented and
    # tested in app.anomaly.multivariate_synthetic_scenarios (a weak
    # relationship was this module's own self-discovered bug during
    # development; see that module's docstring).
    rng = np.random.default_rng(96)
    n_ref, n_score = 300, 200
    a_ref = [50.0 + float(rng.normal(0, 1.0)) for _ in range(n_ref)]
    b_ref = [10.0 + 0.6 * a_ref[i] + float(rng.normal(0, 0.2)) for i in range(n_ref)]
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code="a", n=n_ref, value_fn=lambda i: a_ref[i])
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code="b", n=n_ref, value_fn=lambda i: b_ref[i])
    reference_end = BASE + timedelta(minutes=5 * (n_ref - 1))

    score_start = BASE + timedelta(minutes=5 * n_ref)
    a_score = [50.0 + float(rng.normal(0, 1.0)) for _ in range(n_score)]
    b_score_normal = [10.0 + 0.6 * a_score[i] + float(rng.normal(0, 0.2)) for i in range(n_score)]
    # Decouple b from a for a contiguous window, resampling b's own values
    # from its own normal marginal so neither sensor looks extreme alone.
    decoupled_start, decoupled_end = 80, 110
    pool = rng.permutation(b_score_normal)
    b_score = list(b_score_normal)
    b_score[decoupled_start:decoupled_end] = list(pool[: decoupled_end - decoupled_start])
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code="a", n=n_score, start=score_start, value_fn=lambda i: a_score[i])
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code="b", n=n_score, start=score_start, value_fn=lambda i: b_score[i])
    score_end = score_start + timedelta(minutes=5 * (n_score - 1))

    detector_resp = client.post(
        "/api/v1/anomaly/detectors", headers=auth_headers,
        json={
            "name": "pump-mahalanobis", "detector_type": "mahalanobis", "sensor_ids": [sensor["id"], sensor2["id"]],
            "reference_start": _iso(BASE), "reference_end": _iso(reference_end), "step_seconds": 300,
        },
    )
    assert detector_resp.status_code == 201, detector_resp.text
    detector = detector_resp.json()
    assert detector["status"] == "trained"
    assert detector["detector_type"] == "mahalanobis"

    run_resp = client.post(
        f"/api/v1/anomaly/detectors/{detector['id']}/runs", headers=auth_headers,
        json={"scoring_start": _iso(score_start), "scoring_end": _iso(score_end)},
    )
    assert run_resp.status_code == 201, run_resp.text
    run = run_resp.json()
    assert run["status"] == "succeeded"
    assert run["metrics"]["anomalies_created"] >= 1

    # A multi-sensor detector's anomalies have sensor_id=None (there is no
    # single "the" sensor) — filter by asset_id instead, which is always set.
    anomalies_resp = client.get("/api/v1/anomaly/anomalies", headers=auth_headers, params={"asset_id": asset["id"]})
    assert anomalies_resp.status_code == 200
    anomalies = anomalies_resp.json()["items"]
    assert len(anomalies) >= 1
    top = anomalies[0]
    assert top["sensor_id"] is None
    assert top["detector_type"] == "mahalanobis"
    assert top["contributing_signals"]["method"] == "mahalanobis"
    # The entire point of this detector: the breakdown is a relationship
    # anomaly, not a single-sensor extreme.
    assert top["contributing_signals"]["individually_unremarkable"] is True
    assert "caveat" in top["contributing_signals"]  # non-causal association caveat always surfaced


def test_mahalanobis_requires_at_least_two_sensors(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-MCDSINGLE", sensor_code="temp")
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n=100)
    resp = client.post(
        "/api/v1/anomaly/detectors", headers=auth_headers,
        json={
            "name": "single-sensor-mcd", "detector_type": "mahalanobis", "sensor_ids": [sensor["id"]],
            "reference_start": _iso(BASE), "reference_end": _iso(BASE + timedelta(minutes=5 * 99)), "step_seconds": 300,
        },
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Refusal / warning paths — these matter as much as the happy path
# ---------------------------------------------------------------------------


def test_training_with_insufficient_reference_data_reports_failed_not_500(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-SMALL", sensor_code="temp")
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n=5)  # far below MIN_FOR_BASELINE=30

    resp = client.post(
        "/api/v1/anomaly/detectors", headers=auth_headers,
        json={
            "name": "too-small", "detector_type": "robust_zscore", "sensor_ids": [sensor["id"]],
            "reference_start": _iso(BASE), "reference_end": _iso(BASE + timedelta(minutes=20)),
        },
    )
    assert resp.status_code == 201  # training itself is not an HTTP error...
    detector = resp.json()
    assert detector["status"] == "failed"  # ...but the outcome honestly reports failure
    assert detector["training_error"] is not None
    assert "30" in detector["training_error"] or "observation" in detector["training_error"]


def test_robust_zscore_rejects_more_than_one_sensor(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-MULTI", sensor_code="temp")
    sensor2_resp = client.post(
        f"/api/v1/assets/{asset['id']}/sensors", headers=auth_headers,
        json={"code": "vib", "name": "Vibration", "measurement_type": "vibration", "unit": "mm/s"},
    )
    assert sensor2_resp.status_code == 201

    resp = client.post(
        "/api/v1/anomaly/detectors", headers=auth_headers,
        json={
            "name": "bad-multi", "detector_type": "robust_zscore", "sensor_ids": [sensor["id"], sensor2_resp.json()["id"]],
            "reference_start": _iso(BASE), "reference_end": _iso(BASE + timedelta(hours=1)),
        },
    )
    assert resp.status_code == 422


def test_detection_run_rejects_non_trained_detector(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-FAIL", sensor_code="temp")
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n=5)

    detector_resp = client.post(
        "/api/v1/anomaly/detectors", headers=auth_headers,
        json={
            "name": "failed-detector", "detector_type": "robust_zscore", "sensor_ids": [sensor["id"]],
            "reference_start": _iso(BASE), "reference_end": _iso(BASE + timedelta(minutes=20)),
        },
    )
    detector = detector_resp.json()
    assert detector["status"] == "failed"

    run_resp = client.post(
        f"/api/v1/anomaly/detectors/{detector['id']}/runs", headers=auth_headers,
        json={"scoring_start": _iso(BASE), "scoring_end": _iso(BASE + timedelta(hours=1))},
    )
    assert run_resp.status_code == 409


def test_detection_run_blocks_retrospective_scoring_by_default(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-RETRO", sensor_code="temp")
    n = 200
    reference_end = BASE + timedelta(minutes=5 * (n - 1))
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n=n)

    detector_resp = client.post(
        "/api/v1/anomaly/detectors", headers=auth_headers,
        json={
            "name": "retro-guard", "detector_type": "robust_zscore", "sensor_ids": [sensor["id"]],
            "reference_start": _iso(BASE), "reference_end": _iso(reference_end),
        },
    )
    detector = detector_resp.json()
    assert detector["status"] == "trained"

    # scoring_start is INSIDE the reference period — should be refused by default
    run_resp = client.post(
        f"/api/v1/anomaly/detectors/{detector['id']}/runs", headers=auth_headers,
        json={"scoring_start": _iso(BASE), "scoring_end": _iso(BASE + timedelta(hours=2))},
    )
    assert run_resp.status_code == 422
    assert "future" in run_resp.json()["error"]["message"] or "reference period" in run_resp.json()["error"]["message"]

    # explicit opt-in works and is labeled retrospective in the run's metrics
    allowed_resp = client.post(
        f"/api/v1/anomaly/detectors/{detector['id']}/runs", headers=auth_headers,
        json={"scoring_start": _iso(BASE), "scoring_end": _iso(BASE + timedelta(hours=2)), "allow_retrospective_scoring": True},
    )
    assert allowed_resp.status_code == 201, allowed_resp.text
    assert allowed_resp.json()["metrics"]["retrospective"] is True


def test_invalid_status_transition_rejected(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-TRANS", sensor_code="temp")
    reference_end, score_start, score_end = _ingest_with_spike(
        client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n_ref=400, n_score=200, spike_index=50
    )
    detector = client.post(
        "/api/v1/anomaly/detectors", headers=auth_headers,
        json={"name": "trans-test", "detector_type": "robust_zscore", "sensor_ids": [sensor["id"]], "reference_start": _iso(BASE), "reference_end": _iso(reference_end)},
    ).json()
    run = client.post(
        f"/api/v1/anomaly/detectors/{detector['id']}/runs", headers=auth_headers,
        json={"scoring_start": _iso(score_start), "scoring_end": _iso(score_end)},
    ).json()
    assert run["metrics"]["anomalies_created"] >= 1
    anomaly = client.get("/api/v1/anomaly/anomalies", headers=auth_headers, params={"sensor_id": sensor["id"]}).json()["items"][0]
    assert anomaly["status"] == "detected"

    # DETECTED -> RESOLVED is allowed directly per ALLOWED_TRANSITIONS, but
    # a status that doesn't exist in the enum is rejected by request validation
    bad_resp = client.post(
        f"/api/v1/anomaly/anomalies/{anomaly['id']}/review", headers=auth_headers, json={"to_status": "not_a_real_status"}
    )
    assert bad_resp.status_code == 422

    # move to RESOLVED then try an unsupported RESOLVED -> DETECTED jump
    resolve_resp = client.post(f"/api/v1/anomaly/anomalies/{anomaly['id']}/review", headers=auth_headers, json={"to_status": "resolved"})
    assert resolve_resp.status_code == 200
    bad_transition = client.post(f"/api/v1/anomaly/anomalies/{anomaly['id']}/review", headers=auth_headers, json={"to_status": "detected"})
    assert bad_transition.status_code == 422
    assert bad_transition.json()["error"]["code"] == "validation_failed"


def test_anomaly_detectors_are_organization_scoped(client, auth_headers, db_session):
    from app.services import auth_service

    asset, sensor = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-ORG", sensor_code="temp")
    n = 200
    reference_end = BASE + timedelta(minutes=5 * (n - 1))
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n=n)
    detector = client.post(
        "/api/v1/anomaly/detectors", headers=auth_headers,
        json={"name": "org-scope-test", "detector_type": "robust_zscore", "sensor_ids": [sensor["id"]], "reference_start": _iso(BASE), "reference_end": _iso(reference_end)},
    ).json()

    other_org, other_admin = auth_service.register_organization(
        db_session, organization_name="Other Anomaly Org", admin_email="admin@other-anomaly.example", admin_password="OtherPass123!", admin_full_name="Other Admin"
    )
    other_token = auth_service.issue_token(other_admin)
    other_headers = {"Authorization": f"Bearer {other_token}"}

    resp = client.get(f"/api/v1/anomaly/detectors/{detector['id']}", headers=other_headers)
    assert resp.status_code == 404


def test_viewer_role_cannot_train_or_review_but_can_read(client, auth_headers, db_session, org_and_admin):
    from app.services import auth_service

    org, _ = org_and_admin
    viewer_headers = _viewer_headers(client, db_session, org, auth_service)

    asset, sensor = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-VIEWER", sensor_code="temp")
    n = 200
    reference_end = BASE + timedelta(minutes=5 * (n - 1))
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n=n)

    train_resp = client.post(
        "/api/v1/anomaly/detectors", headers=viewer_headers,
        json={"name": "viewer-blocked", "detector_type": "robust_zscore", "sensor_ids": [sensor["id"]], "reference_start": _iso(BASE), "reference_end": _iso(reference_end)},
    )
    assert train_resp.status_code == 403
    assert train_resp.json()["error"]["code"] == "not_authorized"

    # an admin-trained detector is still readable by the viewer
    detector = client.post(
        "/api/v1/anomaly/detectors", headers=auth_headers,
        json={"name": "admin-trained", "detector_type": "robust_zscore", "sensor_ids": [sensor["id"]], "reference_start": _iso(BASE), "reference_end": _iso(reference_end)},
    ).json()
    read_resp = client.get(f"/api/v1/anomaly/detectors/{detector['id']}", headers=viewer_headers)
    assert read_resp.status_code == 200

    run_resp = client.post(
        f"/api/v1/anomaly/detectors/{detector['id']}/runs", headers=viewer_headers,
        json={"scoring_start": _iso(reference_end + timedelta(minutes=5)), "scoring_end": _iso(reference_end + timedelta(hours=1))},
    )
    assert run_resp.status_code == 403


# ---------------------------------------------------------------------------
# Background job path for large scoring windows
# ---------------------------------------------------------------------------


def test_async_detection_run_path_runs_via_job_runner(client, auth_headers):
    from app.services import anomaly_service

    asset, sensor = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-ASYNC", sensor_code="temp")
    n = 200
    reference_end = BASE + timedelta(minutes=5 * (n - 1))
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n=n)
    detector = client.post(
        "/api/v1/anomaly/detectors", headers=auth_headers,
        json={"name": "async-test", "detector_type": "robust_zscore", "sensor_ids": [sensor["id"]], "reference_start": _iso(BASE), "reference_end": _iso(reference_end)},
    ).json()
    score_start = reference_end + timedelta(minutes=5)
    score_end = score_start + timedelta(hours=2)
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n=24, step_minutes=5, start=score_start)

    original = anomaly_service.MAX_DETECTION_ROWS_SYNC
    anomaly_service.MAX_DETECTION_ROWS_SYNC = 1  # force the async path without needing a huge window
    try:
        run_resp = client.post(
            f"/api/v1/anomaly/detectors/{detector['id']}/runs", headers=auth_headers,
            json={"scoring_start": _iso(score_start), "scoring_end": _iso(score_end)},
        )
    finally:
        anomaly_service.MAX_DETECTION_ROWS_SYNC = original

    assert run_resp.status_code == 201, run_resp.text
    run = run_resp.json()
    assert run["background_job_id"] is not None
    assert run["status"] in ("queued", "running", "succeeded")

    final_run = None
    for _ in range(50):
        poll = client.get(f"/api/v1/anomaly/runs/{run['id']}", headers=auth_headers)
        final_run = poll.json()
        if final_run["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.05)
    assert final_run["status"] == "succeeded", final_run

    job_resp = client.get(f"/api/v1/jobs/{run['background_job_id']}", headers=auth_headers)
    assert job_resp.status_code == 200
    assert job_resp.json()["status"] == "succeeded"


# ---------------------------------------------------------------------------
# Synthetic evaluation + monitoring
# ---------------------------------------------------------------------------


def test_evaluate_detector_against_synthetic_scenarios(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-EVAL", sensor_code="temp")
    n = 400
    reference_end = BASE + timedelta(minutes=5 * (n - 1))
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n=n)
    detector = client.post(
        "/api/v1/anomaly/detectors", headers=auth_headers,
        json={"name": "eval-test", "detector_type": "robust_zscore", "sensor_ids": [sensor["id"]], "reference_start": _iso(BASE), "reference_end": _iso(reference_end)},
    ).json()

    eval_resp = client.post(f"/api/v1/anomaly/detectors/{detector['id']}/evaluate", headers=auth_headers)
    assert eval_resp.status_code == 200, eval_resp.text
    data = eval_resp.json()["data"]
    assert data["is_synthetic"] is True
    assert data["scenario_count"] == 6
    assert len(data["scenarios"]) == 6
    assert all(s["is_synthetic"] for s in data["scenarios"] if "is_synthetic" in s)
    assert "interpretation_note" in data

    # persisted on the detector too
    detector_after = client.get(f"/api/v1/anomaly/detectors/{detector['id']}", headers=auth_headers).json()
    assert detector_after["evaluation_results"]["scenario_count"] == 6


def test_monitoring_summary_reflects_real_activity(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-MON", sensor_code="temp")
    reference_end, score_start, score_end = _ingest_with_spike(
        client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n_ref=400, n_score=200, spike_index=30
    )
    detector = client.post(
        "/api/v1/anomaly/detectors", headers=auth_headers,
        json={"name": "mon-test", "detector_type": "robust_zscore", "sensor_ids": [sensor["id"]], "reference_start": _iso(BASE), "reference_end": _iso(reference_end)},
    ).json()
    client.post(
        f"/api/v1/anomaly/detectors/{detector['id']}/runs", headers=auth_headers,
        json={"scoring_start": _iso(score_start), "scoring_end": _iso(score_end)},
    )

    resp = client.get("/api/v1/anomaly/monitoring", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["run_count_by_status"].get("succeeded", 0) >= 1
    assert data["detector_count_by_status"].get("trained", 0) >= 1
    assert isinstance(data["detector_usage"], list)
    assert data["total_points_scored_succeeded_runs"] >= 1
    assert "not_implemented" in data  # honestly documents what this monitoring view does NOT cover
