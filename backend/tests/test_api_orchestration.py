"""API-level tests for the Intelligence Orchestration layer:
unified timeline, evidence-based prioritization, cross-layer asset
summary, cross-sensor context, and evidence provenance.

Upstream evidence generation (detector training, degradation
classification, ingestion validation) is already exhaustively tested in
tests/test_api_anomaly.py, tests/test_api_condition.py, and
tests/test_ingestion.py; these tests focus on the orchestration layer
itself and use direct ORM inserts for auxiliary evidence rows (degradation
signals, data-quality issues) exactly the way tests/conftest.py's own
sample_asset_and_sensor fixture builds setup data, while still exercising
at least one fully real detector-training-and-detection-run path through
the actual HTTP API for the anomaly evidence.
"""
from datetime import datetime, timedelta, timezone

import numpy as np

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _make_asset_and_sensor(client, headers, *, asset_code="PUMP-ORCH", sensor_code="temp"):
    asset_resp = client.post("/api/v1/assets", headers=headers, json={"code": asset_code, "name": "Orchestration Test Pump", "asset_type": "pump"})
    assert asset_resp.status_code == 201, asset_resp.text
    sensor_resp = client.post(
        f"/api/v1/assets/{asset_resp.json()['id']}/sensors", headers=headers,
        json={"code": sensor_code, "name": "Temp", "measurement_type": "temperature", "unit": "celsius", "expected_interval_seconds": 300},
    )
    assert sensor_resp.status_code == 201, sensor_resp.text
    return asset_resp.json(), sensor_resp.json()


def _ingest(client, headers, *, asset_code, sensor_code, n, step_minutes=5, start=BASE, value_fn=None):
    value_fn = value_fn or (lambda i: 50.0 + (i % 5) * 0.1)
    records = [{"asset_code": asset_code, "sensor_code": sensor_code, "timestamp": _iso(start + timedelta(minutes=step_minutes * i)), "value": value_fn(i)} for i in range(n)]
    resp = client.post("/api/v1/ingestion/measurements", headers=headers, json={"data_source_name": "orchestration-tests", "records": records})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ingest_with_spike(client, headers, *, asset_code, sensor_code, n_ref, n_score, spike_index, step_minutes=5, seed=0):
    rng = np.random.default_rng(seed)
    ref_values = [50.0 + float(rng.normal(0, 0.5)) for _ in range(n_ref)]
    _ingest(client, headers, asset_code=asset_code, sensor_code=sensor_code, n=n_ref, step_minutes=step_minutes, start=BASE, value_fn=lambda i: ref_values[i])
    score_start = BASE + timedelta(minutes=step_minutes * n_ref)
    score_values = [50.0 + float(rng.normal(0, 0.5)) for _ in range(n_score)]
    score_values[spike_index] += 20.0
    _ingest(client, headers, asset_code=asset_code, sensor_code=sensor_code, n=n_score, step_minutes=step_minutes, start=score_start, value_fn=lambda i: score_values[i])
    reference_end = BASE + timedelta(minutes=step_minutes * (n_ref - 1))
    score_end = score_start + timedelta(minutes=step_minutes * (n_score - 1))
    return reference_end, score_start, score_end


def _make_real_anomaly(client, headers, *, asset, sensor, detector_type="robust_zscore", name="temp-robust-zscore", seed=0):
    reference_end, score_start, score_end = _ingest_with_spike(client, headers, asset_code=asset["code"], sensor_code=sensor["code"], n_ref=400, n_score=200, spike_index=100, seed=seed)
    detector_resp = client.post(
        "/api/v1/anomaly/detectors", headers=headers,
        json={"name": name, "detector_type": detector_type, "sensor_ids": [sensor["id"]], "reference_start": _iso(BASE), "reference_end": _iso(reference_end)},
    )
    assert detector_resp.status_code == 201, detector_resp.text
    run_resp = client.post(f"/api/v1/anomaly/detectors/{detector_resp.json()['id']}/runs", headers=headers, json={"scoring_start": _iso(score_start), "scoring_end": _iso(score_end)})
    assert run_resp.status_code == 201, run_resp.text
    anomalies = client.get("/api/v1/anomaly/anomalies", headers=headers, params={"sensor_id": sensor["id"]}).json()["items"]
    assert len(anomalies) >= 1
    return anomalies[0], detector_resp.json()


def _make_degradation_signal(db_session, *, organization_id, asset_id, sensor_id, period_start, period_end, stage="degradation_candidate", status="detected"):
    from app.models.condition import ConditionRun, ConditionRunStatus, DegradationSignal, DegradationStage, ConditionSignalStatus

    run = ConditionRun(
        organization_id=organization_id, asset_id=asset_id, sensor_id=sensor_id,
        analysis_period_start=period_start, analysis_period_end=period_end,
        reference_period_start=period_start - timedelta(days=7), reference_period_end=period_start,
        config={"sub_window_count": 3}, status=ConditionRunStatus.SUCCEEDED, metrics={"signal_produced": True},
    )
    db_session.add(run)
    db_session.flush()
    signal = DegradationSignal(
        organization_id=organization_id, asset_id=asset_id, sensor_id=sensor_id, condition_run_id=run.id,
        stage=DegradationStage(stage), status=ConditionSignalStatus(status),
        period_start=period_start, period_end=period_end, summary="Sustained upward trend across sub-windows.",
        evidence={"classification_rationale": "trend persisted across 3 of 3 sub-windows", "sub_window_trends": [0.1, 0.12, 0.15]},
    )
    db_session.add(signal)
    db_session.commit()
    db_session.refresh(signal)
    return signal


def _make_data_quality_issue(db_session, *, organization_id, asset_id, sensor_id, period_start, period_end, severity="warning"):
    from app.models.data_quality import DataQualityIssue, DataQualityIssueSeverity, DataQualityIssueType

    issue = DataQualityIssue(
        organization_id=organization_id, asset_id=asset_id, sensor_id=sensor_id,
        issue_type=DataQualityIssueType.DATA_GAP, severity=DataQualityIssueSeverity(severity),
        period_start=period_start, period_end=period_end, description="No data for 2 hours.",
        details={"gap_seconds": 7200},
    )
    db_session.add(issue)
    db_session.commit()
    db_session.refresh(issue)
    return issue


# ---------------------------------------------------------------------------
# — Unified timeline
# ---------------------------------------------------------------------------


def test_timeline_combines_real_events_across_layers_and_orders_desc(client, auth_headers, db_session, org_and_admin):
    org, _ = org_and_admin
    asset, sensor = _make_asset_and_sensor(client, auth_headers)
    anomaly, _ = _make_real_anomaly(client, auth_headers, asset=asset, sensor=sensor)
    signal = _make_degradation_signal(db_session, organization_id=org.id, asset_id=asset["id"], sensor_id=sensor["id"], period_start=BASE + timedelta(days=2), period_end=BASE + timedelta(days=2, hours=6))
    _make_data_quality_issue(db_session, organization_id=org.id, asset_id=asset["id"], sensor_id=sensor["id"], period_start=BASE + timedelta(days=1), period_end=BASE + timedelta(days=1, hours=2))

    inv = client.post("/api/v1/investigations", headers=auth_headers, json={"asset_id": asset["id"], "title": "Timeline test investigation"}).json()
    client.post(f"/api/v1/investigations/{inv['id']}/annotations", headers=auth_headers, json={"annotation_type": "observation", "content": "Noted for timeline test."})

    resp = client.get(
        f"/api/v1/orchestration/assets/{asset['id']}/timeline", headers=auth_headers,
        # Investigation events are timestamped at real wall-clock "now" (not BASE), so the window must span both.
        params={"start": _iso(BASE - timedelta(days=1)), "end": _iso(datetime.now(timezone.utc) + timedelta(days=1)), "limit": 200},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]

    event_types_seen = {e["event_type"] for e in data["events"]}
    assert "data_quality_issue" in event_types_seen
    assert "anomaly_detected" in event_types_seen
    assert "degradation_signal_detected" in event_types_seen
    assert "investigation_opened" in event_types_seen
    assert "human_annotation" in event_types_seen

    categories_seen = {e["category"] for e in data["events"]}
    assert categories_seen <= {"observed_data", "analytical_signal", "model_output", "human_interpretation", "governance"}
    # Never conflates categories: a data-quality issue is observed data, not a model output.
    dq_events = [e for e in data["events"] if e["event_type"] == "data_quality_issue"]
    assert all(e["category"] == "observed_data" for e in dq_events)
    anomaly_events = [e for e in data["events"] if e["event_type"] == "anomaly_detected"]
    assert all(e["category"] == "model_output" for e in anomaly_events)

    timestamps = [e["timestamp"] for e in data["events"]]
    assert timestamps == sorted(timestamps, reverse=True)

    # Raw measurements are never included as timeline events.
    assert "measurement" not in {e["event_type"] for e in data["events"]}


def test_timeline_filters_by_event_type(client, auth_headers, db_session, org_and_admin):
    org, _ = org_and_admin
    asset, sensor = _make_asset_and_sensor(client, auth_headers)
    _make_data_quality_issue(db_session, organization_id=org.id, asset_id=asset["id"], sensor_id=sensor["id"], period_start=BASE, period_end=BASE + timedelta(hours=1))
    _make_degradation_signal(db_session, organization_id=org.id, asset_id=asset["id"], sensor_id=sensor["id"], period_start=BASE + timedelta(days=1), period_end=BASE + timedelta(days=1, hours=1))

    resp = client.get(
        f"/api/v1/orchestration/assets/{asset['id']}/timeline", headers=auth_headers,
        params={"start": _iso(BASE - timedelta(days=1)), "end": _iso(BASE + timedelta(days=5)), "event_types": ["data_quality_issue"]},
    )
    data = resp.json()["data"]
    assert len(data["events"]) == 1
    assert data["events"][0]["event_type"] == "data_quality_issue"


def test_timeline_time_range_excludes_events_outside_window(client, auth_headers, db_session, org_and_admin):
    org, _ = org_and_admin
    asset, sensor = _make_asset_and_sensor(client, auth_headers)
    _make_data_quality_issue(db_session, organization_id=org.id, asset_id=asset["id"], sensor_id=sensor["id"], period_start=BASE - timedelta(days=30), period_end=BASE - timedelta(days=30) + timedelta(hours=1))

    resp = client.get(
        f"/api/v1/orchestration/assets/{asset['id']}/timeline", headers=auth_headers,
        params={"start": _iso(BASE), "end": _iso(BASE + timedelta(days=5))},
    )
    assert resp.json()["data"]["events"] == []


def test_timeline_rejects_invalid_range_and_unknown_asset(client, auth_headers):
    asset, _ = _make_asset_and_sensor(client, auth_headers)
    bad_range = client.get(f"/api/v1/orchestration/assets/{asset['id']}/timeline", headers=auth_headers, params={"start": _iso(BASE), "end": _iso(BASE - timedelta(days=1))})
    assert bad_range.status_code == 422

    not_found = client.get("/api/v1/orchestration/assets/00000000-0000-0000-0000-000000000000/timeline", headers=auth_headers, params={"start": _iso(BASE), "end": _iso(BASE + timedelta(days=1))})
    assert not_found.status_code == 404


# ---------------------------------------------------------------------------
# — Evidence-based prioritization
# ---------------------------------------------------------------------------


def test_prioritized_signals_exposes_contributing_factors_and_methodology(client, auth_headers, db_session, org_and_admin):
    org, _ = org_and_admin
    asset, sensor = _make_asset_and_sensor(client, auth_headers)
    anomaly, _ = _make_real_anomaly(client, auth_headers, asset=asset, sensor=sensor, detector_type="robust_zscore", name="rz")

    resp = client.get("/api/v1/orchestration/prioritized-signals", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["methodology_version"] == "orchestration_priority_v1"
    assert "score" in data["items"][0]
    assert "contributing_factors" in data["items"][0]
    factors = data["items"][0]["contributing_factors"]
    assert set(factors.keys()) >= {"severity_rank", "persistence_count", "cross_detector_agreement", "nearby_data_quality_issue_count", "human_reviewed"}
    # Never a fabricated probability of failure.
    assert "probability" not in data
    assert all("probability" not in item for item in data["items"])


def test_prioritization_cross_detector_agreement_raises_score(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers)
    _make_real_anomaly(client, auth_headers, asset=asset, sensor=sensor, detector_type="robust_zscore", name="rz-agree", seed=1)
    _make_real_anomaly(client, auth_headers, asset=asset, sensor=sensor, detector_type="isolation_forest", name="if-agree", seed=2)

    data = client.get("/api/v1/orchestration/prioritized-signals", headers=auth_headers).json()["data"]
    this_sensor_items = [it for it in data["items"] if it["sensor_id"] == sensor["id"]]
    assert any(it["contributing_factors"]["cross_detector_agreement"] for it in this_sensor_items)


def test_prioritized_signals_is_organization_scoped(client, auth_headers, db_session):
    from app.services import auth_service as auth_service_module

    asset, sensor = _make_asset_and_sensor(client, auth_headers)
    _make_real_anomaly(client, auth_headers, asset=asset, sensor=sensor)

    other_org, other_admin = auth_service_module.register_organization(db_session, organization_name="Other Org Prio", admin_email="admin@other-prio.example", admin_password="AdminPass123!", admin_full_name="Other Admin")
    other_token = auth_service_module.issue_token(other_admin)
    other_headers = {"Authorization": f"Bearer {other_token}"}

    other_data = client.get("/api/v1/orchestration/prioritized-signals", headers=other_headers).json()["data"]
    assert other_data["items"] == []
    assert other_data["total_open_anomalies"] == 0


# ---------------------------------------------------------------------------
# — Cross-layer asset intelligence summary
# ---------------------------------------------------------------------------


def test_asset_summary_distinguishes_categories(client, auth_headers, db_session, org_and_admin):
    org, _ = org_and_admin
    asset, sensor = _make_asset_and_sensor(client, auth_headers)
    _make_real_anomaly(client, auth_headers, asset=asset, sensor=sensor)
    _make_degradation_signal(db_session, organization_id=org.id, asset_id=asset["id"], sensor_id=sensor["id"], period_start=BASE, period_end=BASE + timedelta(hours=1))
    _make_data_quality_issue(db_session, organization_id=org.id, asset_id=asset["id"], sensor_id=sensor["id"], period_start=BASE, period_end=BASE + timedelta(hours=1), severity="critical")

    data = client.get(f"/api/v1/orchestration/assets/{asset['id']}/summary", headers=auth_headers).json()["data"]
    assert set(data.keys()) >= {"observed_data", "analytical_signals", "model_outputs", "human_interpretations", "limitations"}
    assert data["observed_data"]["issues_by_severity"]["critical"] == 1
    assert data["analytical_signals"]["open_degradation_signal_count"] == 1
    assert data["model_outputs"]["open_anomaly_count"] >= 1
    assert any("critical data-quality" in l for l in data["limitations"])
    assert any("not yet been reviewed" in l for l in data["limitations"])


def test_asset_summary_honest_about_no_sensors(client, auth_headers):
    asset_resp = client.post("/api/v1/assets", headers=auth_headers, json={"code": "EMPTY-ASSET", "name": "No Sensors", "asset_type": "pump"})
    asset = asset_resp.json()
    data = client.get(f"/api/v1/orchestration/assets/{asset['id']}/summary", headers=auth_headers).json()["data"]
    assert data["sensor_count"] == 0
    assert data["model_outputs"]["open_anomaly_count"] == 0
    assert data["analytical_signals"]["open_degradation_signal_count"] == 0
    assert any("no sensors" in l.lower() for l in data["limitations"])


def test_asset_summary_not_found(client, auth_headers):
    resp = client.get("/api/v1/orchestration/assets/00000000-0000-0000-0000-000000000000/summary", headers=auth_headers)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# — Cross-sensor context (non-causal language)
# ---------------------------------------------------------------------------


def test_cross_sensor_context_requires_two_sensors_on_the_same_asset(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers)
    other_asset, other_sensor = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-ORCH-OTHER", sensor_code="temp2")

    one_sensor = client.get(f"/api/v1/orchestration/assets/{asset['id']}/cross-sensor-context", headers=auth_headers, params={"sensor_ids": [sensor["id"]], "start": _iso(BASE), "end": _iso(BASE + timedelta(hours=1))})
    assert one_sensor.status_code == 422

    mismatched = client.get(
        f"/api/v1/orchestration/assets/{asset['id']}/cross-sensor-context", headers=auth_headers,
        params={"sensor_ids": [sensor["id"], other_sensor["id"]], "start": _iso(BASE), "end": _iso(BASE + timedelta(hours=1))},
    )
    assert mismatched.status_code == 422


def test_cross_sensor_context_uses_non_causal_language_and_real_correlation(client, auth_headers):
    asset, sensor_a = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-ORCH-XS", sensor_code="temp-a")
    sensor_b_resp = client.post(f"/api/v1/assets/{asset['id']}/sensors", headers=auth_headers, json={"code": "temp-b", "name": "Temp B", "measurement_type": "temperature", "unit": "celsius", "expected_interval_seconds": 300})
    sensor_b = sensor_b_resp.json()

    n = 300
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor_a["code"], n=n, value_fn=lambda i: 50.0 + (i % 10) * 0.3)
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor_b["code"], n=n, value_fn=lambda i: 50.0 + (i % 10) * 0.3)  # perfectly correlated by construction

    resp = client.get(
        f"/api/v1/orchestration/assets/{asset['id']}/cross-sensor-context", headers=auth_headers,
        params={"sensor_ids": [sensor_a["id"], sensor_b["id"]], "start": _iso(BASE), "end": _iso(BASE + timedelta(minutes=5 * (n - 1))), "freq_seconds": 300},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert "correlation" in data
    assert "not causation" in data["note"]
    assert "never asserts that one sensor's behavior caused another's" in data["note"]
    for association in data["observed_associations"]:
        assert "not evidence" in association


# ---------------------------------------------------------------------------
# — Evidence provenance
# ---------------------------------------------------------------------------


def test_anomaly_provenance_without_governance_registration(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers)
    anomaly, detector = _make_real_anomaly(client, auth_headers, asset=asset, sensor=sensor)

    resp = client.get(f"/api/v1/orchestration/evidence/anomaly/{anomaly['id']}/provenance", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["summary"]["governance_status"] == "not registered in the intelligence lifecycle registry"
    assert data["technical"]["model_version"]["id"] == detector["id"]
    assert data["technical"]["model_version"]["detector_config"] is not None


def test_anomaly_provenance_with_active_governance_registration(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers)
    anomaly, detector = _make_real_anomaly(client, auth_headers, asset=asset, sensor=sensor, detector_type="robust_zscore", name="rz-gov")

    comp = client.post("/api/v1/mlops/components", headers=auth_headers, json={"component_type": "statistical_detector", "name": detector["name"], "description": "test"}).json()
    version = client.post(f"/api/v1/mlops/components/{comp['id']}/versions", headers=auth_headers, json={"version_kind": "model_version", "model_version_id": detector["id"]}).json()
    client.post(f"/api/v1/mlops/versions/{version['id']}/evaluate", headers=auth_headers, json={})
    client.post(f"/api/v1/mlops/versions/{version['id']}/validate", headers=auth_headers, json={"notes": "reviewed"})
    client.post(f"/api/v1/mlops/versions/{version['id']}/approve", headers=auth_headers, json={"notes": "approved"})
    client.post(f"/api/v1/mlops/components/{comp['id']}/activate", headers=auth_headers, json={"component_version_id": version["id"]})

    data = client.get(f"/api/v1/orchestration/evidence/anomaly/{anomaly['id']}/provenance", headers=auth_headers).json()["data"]
    assert data["summary"]["governance_status"] == "active"
    assert data["technical"]["governance"]["component_name"] == detector["name"]
    assert data["technical"]["governance"]["is_active"] is True


def test_degradation_signal_provenance(client, auth_headers, db_session, org_and_admin):
    org, _ = org_and_admin
    asset, sensor = _make_asset_and_sensor(client, auth_headers)
    signal = _make_degradation_signal(db_session, organization_id=org.id, asset_id=asset["id"], sensor_id=sensor["id"], period_start=BASE, period_end=BASE + timedelta(hours=1))

    data = client.get(f"/api/v1/orchestration/evidence/degradation_signal/{signal.id}/provenance", headers=auth_headers).json()["data"]
    assert "heuristic" in data["summary"]["method"]
    assert data["technical"]["evidence"]["classification_rationale"]


def test_provenance_unsupported_evidence_type_and_not_found(client, auth_headers):
    unsupported = client.get("/api/v1/orchestration/evidence/data_quality_issue/some-id/provenance", headers=auth_headers)
    assert unsupported.status_code == 422

    not_found = client.get("/api/v1/orchestration/evidence/anomaly/00000000-0000-0000-0000-000000000000/provenance", headers=auth_headers)
    assert not_found.status_code == 404


# ---------------------------------------------------------------------------
# Authorization: read-only orchestration routes are open to any authenticated role
# ---------------------------------------------------------------------------


def test_orchestration_routes_require_authentication(client):
    resp = client.get("/api/v1/orchestration/prioritized-signals")
    assert resp.status_code == 401


def test_viewer_can_read_orchestration_endpoints(client, auth_headers, db_session, org_and_admin):
    org, _ = org_and_admin
    from app.core.security import hash_password
    from app.models.user import User, UserRole
    from app.services import auth_service as auth_service_module

    asset, sensor = _make_asset_and_sensor(client, auth_headers)
    viewer = User(organization_id=org.id, email="viewer@orch-tests.example", hashed_password=hash_password("ViewerPass123!"), full_name="Viewer", role=UserRole.VIEWER)
    db_session.add(viewer)
    db_session.commit()
    viewer_headers = {"Authorization": f"Bearer {auth_service_module.issue_token(viewer)}"}

    assert client.get("/api/v1/orchestration/prioritized-signals", headers=viewer_headers).status_code == 200
    assert client.get(f"/api/v1/orchestration/assets/{asset['id']}/summary", headers=viewer_headers).status_code == 200
