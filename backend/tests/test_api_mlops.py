"""API-level tests for Intelligence Lifecycle, Registry & Governance
(Phase 5) — exercises the real HTTP surface against the real DB, mirrors
tests/test_api_anomaly.py and tests/test_api_condition.py's conventions.

The centerpiece is test_isolation_forest_full_lifecycle_e2e: it takes one
REAL intelligence component (the Isolation Forest detector — the one
genuinely "trained ML model" in this codebase) through the entire
traceable lifecycle the spec's stop condition requires: register the
component -> train the real underlying detector via the existing /anomaly
API -> register that trained version -> record a real synthetic
evaluation -> validate with human evidence -> approve -> activate -> score
through the active version -> verify the output's provenance points back
to the exact version that produced it -> retrain -> activate the new
version -> roll back -> verify the original version is active again.

Everything else here is deliberately adversarial: authorization,
cross-org isolation, wrong-stage transitions, missing-evidence gates, and
the honest "nothing to score" / "not justified" / "insufficient data"
paths.
"""
from datetime import datetime, timedelta, timezone

import numpy as np

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _make_asset_and_sensor(client, headers, *, asset_code="PUMP-MLOPS", sensor_code="temp"):
    asset_resp = client.post("/api/v1/assets", headers=headers, json={"code": asset_code, "name": "MLOps Test Pump", "asset_type": "pump"})
    assert asset_resp.status_code == 201, asset_resp.text
    sensor_resp = client.post(
        f"/api/v1/assets/{asset_resp.json()['id']}/sensors", headers=headers,
        json={"code": sensor_code, "name": "Temp", "measurement_type": "temperature", "unit": "celsius", "expected_interval_seconds": 300},
    )
    assert sensor_resp.status_code == 201, sensor_resp.text
    return asset_resp.json(), sensor_resp.json()


_SYNC_INGEST_CHUNK = 1500


def _ingest(client, headers, *, asset_code, sensor_code, n, step_minutes=5, start=BASE, value_fn):
    records = [{"asset_code": asset_code, "sensor_code": sensor_code, "timestamp": _iso(start + timedelta(minutes=step_minutes * i)), "value": value_fn(i)} for i in range(n)]
    last = None
    for chunk_start in range(0, len(records), _SYNC_INGEST_CHUNK):
        chunk = records[chunk_start : chunk_start + _SYNC_INGEST_CHUNK]
        resp = client.post("/api/v1/ingestion/measurements", headers=headers, json={"data_source_name": "mlops-tests", "records": chunk})
        assert resp.status_code == 200, resp.text
        last = resp.json()
    return last


def _daily_cycle_value(i, *, step_minutes=5, baseline=58.0, amplitude=3.0, drift_per_day=0.0, noise_std=0.6, rng=None):
    t_hours = (i * step_minutes) / 60.0
    noise = float(rng.normal(0, noise_std)) if rng is not None else 0.0
    return baseline + amplitude * np.sin(2 * 3.141592653589793 * t_hours / 24.0) + drift_per_day * (t_hours / 24.0) + noise


def _viewer_headers(client, db_session, org, auth_service_module):
    from app.core.security import hash_password
    from app.models.user import User, UserRole

    viewer = User(organization_id=org.id, email="viewer@mlops-tests.example", hashed_password=hash_password("ViewerPass123!"), full_name="Viewer", role=UserRole.VIEWER)
    db_session.add(viewer)
    db_session.commit()
    token = auth_service_module.issue_token(viewer)
    return {"Authorization": f"Bearer {token}"}


def _train_isolation_forest(client, headers, *, asset, sensor, name="pump-isoforest"):
    n = 600
    reference_end = BASE + timedelta(minutes=5 * (n - 1))
    _ingest(client, headers, asset_code=asset["code"], sensor_code=sensor["code"], n=n, value_fn=lambda i: 50.0 + (i % 3) * 0.05)
    resp = client.post(
        "/api/v1/anomaly/detectors", headers=headers,
        json={"name": name, "detector_type": "isolation_forest", "sensor_ids": [sensor["id"]], "reference_start": _iso(BASE), "reference_end": _iso(reference_end), "step_seconds": 1800},
    )
    assert resp.status_code == 201, resp.text
    detector = resp.json()
    assert detector["status"] == "trained"
    return detector, reference_end


# ---------------------------------------------------------------------------
# The flagship end-to-end workflow (§21 / stop condition)
# ---------------------------------------------------------------------------


def test_isolation_forest_full_lifecycle_e2e(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers)
    detector, reference_end = _train_isolation_forest(client, auth_headers, asset=asset, sensor=sensor)

    # 1. Register the logical component (TRAINED_ML_MODEL — never "detector" or "model" ambiguously)
    comp_resp = client.post("/api/v1/mlops/components", headers=auth_headers, json={"component_type": "trained_ml_model", "name": detector["name"], "description": "Isolation Forest bearing temp detector"})
    assert comp_resp.status_code == 201, comp_resp.text
    component = comp_resp.json()
    assert component["component_type"] == "trained_ml_model"

    # 2. Register the already-trained detector as component version 1
    ver_resp = client.post(f"/api/v1/mlops/components/{component['id']}/versions", headers=auth_headers, json={"version_kind": "model_version", "model_version_id": detector["id"]})
    assert ver_resp.status_code == 201, ver_resp.text
    version = ver_resp.json()
    assert version["version_number"] == 1
    assert version["lifecycle_stage"] == "trained"

    # Re-registering the same underlying detector under a version is rejected — no duplicate identities.
    dup_resp = client.post(f"/api/v1/mlops/components/{component['id']}/versions", headers=auth_headers, json={"version_kind": "model_version", "model_version_id": detector["id"]})
    assert dup_resp.status_code == 409

    # Cannot validate before evaluating.
    early_validate = client.post(f"/api/v1/mlops/versions/{version['id']}/validate", headers=auth_headers, json={"notes": "looks fine"})
    assert early_validate.status_code == 409

    # 3. Record a REAL synthetic evaluation (the actual harness, not a stand-in)
    eval_resp = client.post(f"/api/v1/mlops/versions/{version['id']}/evaluate", headers=auth_headers, json={})
    assert eval_resp.status_code == 200, eval_resp.text
    assert eval_resp.json()["lifecycle_stage"] == "evaluated"

    # Cannot activate before validated+approved.
    premature_activate = client.post(f"/api/v1/mlops/components/{component['id']}/activate", headers=auth_headers, json={"component_version_id": version["id"]})
    assert premature_activate.status_code == 409

    # 4. Human validation — empty notes rejected, then a real validation
    bad_validate = client.post(f"/api/v1/mlops/versions/{version['id']}/validate", headers=auth_headers, json={"notes": ""})
    assert bad_validate.status_code == 422
    validate_resp = client.post(f"/api/v1/mlops/versions/{version['id']}/validate", headers=auth_headers, json={"notes": "Reviewed synthetic evaluation results; detector behaves as expected on injected scenarios."})
    assert validate_resp.status_code == 200, validate_resp.text
    assert validate_resp.json()["lifecycle_stage"] == "validated"
    assert validate_resp.json()["validation_evidence"]["synthetic_evaluation_summary"]["is_synthetic"] is True

    # 5. Approval
    approve_resp = client.post(f"/api/v1/mlops/versions/{version['id']}/approve", headers=auth_headers, json={"notes": "Approved for activation."})
    assert approve_resp.status_code == 200, approve_resp.text
    assert approve_resp.json()["lifecycle_stage"] == "approved"

    # 6. Activation
    activate_resp = client.post(f"/api/v1/mlops/components/{component['id']}/activate", headers=auth_headers, json={"component_version_id": version["id"]})
    assert activate_resp.status_code == 200, activate_resp.text
    assert activate_resp.json()["lifecycle_stage"] == "active"

    # 7. Score through the active version — never referencing model_version_id directly
    score_start = reference_end + timedelta(minutes=5)
    score_end = score_start + timedelta(hours=6)
    score_resp = client.post(f"/api/v1/mlops/components/{component['id']}/score", headers=auth_headers, json={"scoring_start": _iso(score_start), "scoring_end": _iso(score_end)})
    assert score_resp.status_code == 200, score_resp.text
    result = score_resp.json()["data"]
    assert result["provenance"]["component_id"] == component["id"]
    assert result["provenance"]["component_version_id"] == version["id"]
    assert result["provenance"]["version_number"] == 1
    assert result["result_type"] == "model_run"
    run_id = result["model_run_id"]
    run_check = client.get(f"/api/v1/anomaly/runs/{run_id}", headers=auth_headers)
    assert run_check.status_code == 200
    assert run_check.json()["status"] == "succeeded"

    # 8. Retrain — a REAL new detector via the same production training code, never simulated
    retrain_req = client.post(
        f"/api/v1/mlops/components/{component['id']}/retraining-requests", headers=auth_headers,
        json={"reason": "Periodic refresh of reference statistics.", "config": {"sensor_ids": [sensor["id"]], "reference_start": _iso(BASE), "reference_end": _iso(reference_end), "step_seconds": 1800}},
    )
    assert retrain_req.status_code == 201, retrain_req.text
    request_id = retrain_req.json()["id"]
    assert retrain_req.json()["status"] == "requested"

    exec_resp = client.post(f"/api/v1/mlops/retraining-requests/{request_id}/execute", headers=auth_headers)
    assert exec_resp.status_code == 200, exec_resp.text
    executed = exec_resp.json()
    assert executed["status"] == "evaluated"
    v2_id = executed["resulting_component_version_id"]
    assert v2_id is not None

    # Retraining alone must NEVER touch what's active.
    still_v1 = client.get(f"/api/v1/mlops/versions/{version['id']}", headers=auth_headers).json()
    assert still_v1["lifecycle_stage"] == "active"
    v2 = client.get(f"/api/v1/mlops/versions/{v2_id}", headers=auth_headers).json()
    assert v2["lifecycle_stage"] == "trained"
    assert v2["version_number"] == 2

    # Push v2 through the same gate and activate it.
    client.post(f"/api/v1/mlops/versions/{v2_id}/evaluate", headers=auth_headers, json={})
    client.post(f"/api/v1/mlops/versions/{v2_id}/validate", headers=auth_headers, json={"notes": "Reviewed retrained version's synthetic evaluation."})
    client.post(f"/api/v1/mlops/versions/{v2_id}/approve", headers=auth_headers, json={"notes": "ok"})
    activate_v2 = client.post(f"/api/v1/mlops/components/{component['id']}/activate", headers=auth_headers, json={"component_version_id": v2_id})
    assert activate_v2.status_code == 200, activate_v2.text
    assert activate_v2.json()["lifecycle_stage"] == "active"

    v1_after = client.get(f"/api/v1/mlops/versions/{version['id']}", headers=auth_headers).json()
    assert v1_after["lifecycle_stage"] == "approved"  # demoted, never deleted

    # 9. Rollback — no target specified, should return to v1
    rollback_resp = client.post(f"/api/v1/mlops/components/{component['id']}/rollback", headers=auth_headers, json={})
    assert rollback_resp.status_code == 200, rollback_resp.text
    assert rollback_resp.json()["id"] == version["id"]
    assert rollback_resp.json()["lifecycle_stage"] == "active"

    v2_after_rollback = client.get(f"/api/v1/mlops/versions/{v2_id}", headers=auth_headers).json()
    assert v2_after_rollback["lifecycle_stage"] == "approved"

    # 10. Governance audit trail is non-empty and covers the full sequence
    events_resp = client.get("/api/v1/mlops/governance-events", headers=auth_headers, params={"component_id": component["id"], "limit": 100})
    assert events_resp.status_code == 200
    event_types = {e["event_type"] for e in events_resp.json()["items"]}
    for expected in ("component_registered", "version_registered", "evaluation_recorded", "validation_decision", "approval_decision", "activated", "run_completed", "retraining_requested", "rolled_back"):
        assert expected in event_types, f"missing governance event type: {expected}"


# ---------------------------------------------------------------------------
# Mahalanobis (Phase 14) — proves it shares TRAINED_ML_MODEL with
# Isolation Forest (both are "a real sklearn estimator fit on data" per
# app.models.mlops.ComponentType's own docstring), not a bespoke
# component_type, and that retraining resolves the right detector_type
# automatically from the component's existing version.
# ---------------------------------------------------------------------------


def _train_mahalanobis(client, headers, *, asset, sensor_a, sensor_b, name="pump-mahalanobis"):
    n = 300
    reference_end = BASE + timedelta(minutes=5 * (n - 1))
    rng = np.random.default_rng(7)
    a_vals = [50.0 + float(rng.normal(0, 1.0)) for _ in range(n)]
    b_vals = [10.0 + 0.6 * a_vals[i] + float(rng.normal(0, 0.3)) for i in range(n)]
    _ingest(client, headers, asset_code=asset["code"], sensor_code=sensor_a["code"], n=n, value_fn=lambda i: a_vals[i])
    _ingest(client, headers, asset_code=asset["code"], sensor_code=sensor_b["code"], n=n, value_fn=lambda i: b_vals[i])
    resp = client.post(
        "/api/v1/anomaly/detectors", headers=headers,
        json={"name": name, "detector_type": "mahalanobis", "sensor_ids": [sensor_a["id"], sensor_b["id"]], "reference_start": _iso(BASE), "reference_end": _iso(reference_end), "step_seconds": 300},
    )
    assert resp.status_code == 201, resp.text
    detector = resp.json()
    assert detector["status"] == "trained"
    return detector, reference_end


def test_mahalanobis_component_registers_under_trained_ml_model_and_retrains(client, auth_headers):
    asset, sensor_a = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-MCDLIFE", sensor_code="a")
    sensor_b_resp = client.post(f"/api/v1/assets/{asset['id']}/sensors", headers=auth_headers, json={"code": "b", "name": "B", "measurement_type": "pressure", "unit": "bar", "expected_interval_seconds": 300})
    assert sensor_b_resp.status_code == 201
    sensor_b = sensor_b_resp.json()
    detector, reference_end = _train_mahalanobis(client, auth_headers, asset=asset, sensor_a=sensor_a, sensor_b=sensor_b)

    # Registers under the SAME component_type (trained_ml_model) Isolation
    # Forest uses — the fix this test exists to prove: before Phase 14,
    # trained_ml_model accepted only isolation_forest.
    comp_resp = client.post("/api/v1/mlops/components", headers=auth_headers, json={"component_type": "trained_ml_model", "name": detector["name"]})
    assert comp_resp.status_code == 201, comp_resp.text
    component = comp_resp.json()
    ver_resp = client.post(f"/api/v1/mlops/components/{component['id']}/versions", headers=auth_headers, json={"version_kind": "model_version", "model_version_id": detector["id"]})
    assert ver_resp.status_code == 201, ver_resp.text
    version = ver_resp.json()

    client.post(f"/api/v1/mlops/versions/{version['id']}/evaluate", headers=auth_headers, json={})
    client.post(f"/api/v1/mlops/versions/{version['id']}/validate", headers=auth_headers, json={"notes": "Reviewed synthetic evaluation for the Mahalanobis detector."})
    client.post(f"/api/v1/mlops/versions/{version['id']}/approve", headers=auth_headers, json={"notes": "ok"})
    activate_resp = client.post(f"/api/v1/mlops/components/{component['id']}/activate", headers=auth_headers, json={"component_version_id": version["id"]})
    assert activate_resp.status_code == 200, activate_resp.text

    score_start = reference_end + timedelta(minutes=5)
    score_end = score_start + timedelta(hours=6)
    score_resp = client.post(f"/api/v1/mlops/components/{component['id']}/score", headers=auth_headers, json={"scoring_start": _iso(score_start), "scoring_end": _iso(score_end)})
    assert score_resp.status_code == 200, score_resp.text
    assert score_resp.json()["data"]["provenance"]["component_version_id"] == version["id"]

    # Retraining must infer detector_type=mahalanobis from this component's
    # existing version (its config carries no detector_type itself) — the
    # _resolve_retraining_detector_type fallback this test exercises.
    retrain_req = client.post(
        f"/api/v1/mlops/components/{component['id']}/retraining-requests", headers=auth_headers,
        json={"reason": "Periodic refresh.", "config": {"sensor_ids": [sensor_a["id"], sensor_b["id"]], "reference_start": _iso(BASE), "reference_end": _iso(reference_end), "step_seconds": 300}},
    )
    assert retrain_req.status_code == 201, retrain_req.text
    exec_resp = client.post(f"/api/v1/mlops/retraining-requests/{retrain_req.json()['id']}/execute", headers=auth_headers)
    assert exec_resp.status_code == 200, exec_resp.text
    v2 = client.get(f"/api/v1/mlops/versions/{exec_resp.json()['resulting_component_version_id']}", headers=auth_headers).json()
    assert v2["version_number"] == 2
    detector_v2 = client.get(f"/api/v1/anomaly/detectors/{v2['model_version_id']}", headers=auth_headers).json()
    assert detector_v2["detector_type"] == "mahalanobis"


def test_robust_zscore_still_rejected_under_trained_ml_model(client, auth_headers):
    """Guards the OTHER direction of the same fix: widening
    trained_ml_model to accept mahalanobis must not also accidentally
    widen it to accept robust_zscore (a closed-form statistic, not a
    fitted sklearn estimator — it belongs only under statistical_detector,
    per app.models.mlops.ComponentType's own docstring)."""
    asset, sensor = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-RZREJECT", sensor_code="temp")
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n=400, value_fn=lambda i: 50.0 + (i % 5) * 0.1)
    detector_resp = client.post(
        "/api/v1/anomaly/detectors", headers=auth_headers,
        json={"name": "rz-reject", "detector_type": "robust_zscore", "sensor_ids": [sensor["id"]], "reference_start": _iso(BASE), "reference_end": _iso(BASE + timedelta(minutes=5 * 399))},
    )
    detector = detector_resp.json()
    comp_resp = client.post("/api/v1/mlops/components", headers=auth_headers, json={"component_type": "trained_ml_model", "name": "rz-reject"})
    component = comp_resp.json()
    resp = client.post(f"/api/v1/mlops/components/{component['id']}/versions", headers=auth_headers, json={"version_kind": "model_version", "model_version_id": detector["id"]})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Forecasting-model lifecycle — the honesty gate must block an unjustified model
# ---------------------------------------------------------------------------


def test_forecasting_component_validate_blocked_when_not_justified(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-FCVAL", sensor_code="temp")
    rng = np.random.default_rng(11)
    n = 21 * 24 * 12
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n=n, value_fn=lambda i: _daily_cycle_value(i, amplitude=3.0, drift_per_day=0.0, noise_std=0.6, rng=rng))
    fit_end = BASE + timedelta(days=14)
    test_end = BASE + timedelta(days=21)
    fm_resp = client.post("/api/v1/condition/forecast-models", headers=auth_headers, json={"sensor_id": sensor["id"], "name": "temp-forecast", "fit_start": _iso(BASE), "fit_end": _iso(fit_end), "test_end": _iso(test_end)})
    assert fm_resp.status_code == 201
    forecast_model = fm_resp.json()
    assert forecast_model["status"] == "not_justified"

    comp_resp = client.post("/api/v1/mlops/components", headers=auth_headers, json={"component_type": "forecasting_model", "name": "temp-forecast"})
    component = comp_resp.json()
    ver_resp = client.post(f"/api/v1/mlops/components/{component['id']}/versions", headers=auth_headers, json={"version_kind": "forecast_model", "forecast_model_id": forecast_model["id"]})
    assert ver_resp.status_code == 201
    version = ver_resp.json()
    assert version["lifecycle_stage"] == "evaluated"  # the mandatory backtest already ran

    blocked = client.post(f"/api/v1/mlops/versions/{version['id']}/validate", headers=auth_headers, json={"notes": "attempting anyway"})
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "conflict"


def test_forecasting_component_full_lifecycle_when_justified(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-FCJUST", sensor_code="ramp")
    rng = np.random.default_rng(13)
    n = 3000
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n=n, step_minutes=1, value_fn=lambda i: 10.0 + 0.01 * i + float(rng.normal(0, 0.01)))
    fit_end = BASE + timedelta(minutes=2400)
    test_end = BASE + timedelta(minutes=n - 1)
    fm_resp = client.post("/api/v1/condition/forecast-models", headers=auth_headers, json={"sensor_id": sensor["id"], "name": "ramp-forecast", "fit_start": _iso(BASE), "fit_end": _iso(fit_end), "test_end": _iso(test_end)})
    forecast_model = fm_resp.json()
    assert forecast_model["status"] == "justified"

    comp_resp = client.post("/api/v1/mlops/components", headers=auth_headers, json={"component_type": "forecasting_model", "name": "ramp-forecast"})
    component = comp_resp.json()
    ver_resp = client.post(f"/api/v1/mlops/components/{component['id']}/versions", headers=auth_headers, json={"version_kind": "forecast_model", "forecast_model_id": forecast_model["id"]})
    version = ver_resp.json()

    client.post(f"/api/v1/mlops/versions/{version['id']}/validate", headers=auth_headers, json={"notes": "Backtest beats naive baselines by a wide margin on a near-noiseless ramp."})
    client.post(f"/api/v1/mlops/versions/{version['id']}/approve", headers=auth_headers, json={})
    activate_resp = client.post(f"/api/v1/mlops/components/{component['id']}/activate", headers=auth_headers, json={"component_version_id": version["id"]})
    assert activate_resp.status_code == 200, activate_resp.text

    score_resp = client.post(f"/api/v1/mlops/components/{component['id']}/score", headers=auth_headers, json={"as_of": _iso(fit_end), "horizon_seconds": 3600, "step_seconds": 600})
    assert score_resp.status_code == 200, score_resp.text
    result = score_resp.json()["data"]
    assert result["result_type"] == "forecast_run"
    assert result["provenance"]["version_kind"] == "forecast_model"


# ---------------------------------------------------------------------------
# Heuristic-kind components — no trainable artifact, evidence-only gate
# ---------------------------------------------------------------------------


def test_heuristic_component_requires_reviewed_reference_to_validate(client, auth_headers):
    comp_resp = client.post("/api/v1/mlops/components", headers=auth_headers, json={"component_type": "heuristic_signal_method", "name": "degradation_classifier", "description": "Three-stage degradation classification (app.condition.degradation)"})
    component = comp_resp.json()
    ver_resp = client.post(f"/api/v1/mlops/components/{component['id']}/versions", headers=auth_headers, json={"version_kind": "heuristic", "heuristic_code_version": "degradation_classifier@1"})
    assert ver_resp.status_code == 201
    version = ver_resp.json()
    assert version["lifecycle_stage"] == "evaluated"  # no training step exists for a heuristic

    missing_evidence = client.post(f"/api/v1/mlops/versions/{version['id']}/validate", headers=auth_headers, json={"notes": "reviewed the logic"})
    assert missing_evidence.status_code == 422

    ok = client.post(f"/api/v1/mlops/versions/{version['id']}/validate", headers=auth_headers, json={"notes": "reviewed the logic and its test coverage", "evidence": {"reviewed_reference": "app.condition.degradation + tests/test_condition_core.py"}})
    assert ok.status_code == 200
    client.post(f"/api/v1/mlops/versions/{version['id']}/approve", headers=auth_headers, json={})
    activate_resp = client.post(f"/api/v1/mlops/components/{component['id']}/activate", headers=auth_headers, json={"component_version_id": version["id"]})
    assert activate_resp.status_code == 200

    # Heuristic components are not scored through this contract — honest refusal, not a fake result.
    score_resp = client.post(f"/api/v1/mlops/components/{component['id']}/score", headers=auth_headers, json={})
    assert score_resp.status_code == 422


# ---------------------------------------------------------------------------
# Refusal / honesty paths
# ---------------------------------------------------------------------------


def test_scoring_refused_when_no_active_version(client, auth_headers):
    comp_resp = client.post("/api/v1/mlops/components", headers=auth_headers, json={"component_type": "trained_ml_model", "name": "unused-detector"})
    component = comp_resp.json()
    score_resp = client.post(f"/api/v1/mlops/components/{component['id']}/score", headers=auth_headers, json={"scoring_start": _iso(BASE), "scoring_end": _iso(BASE + timedelta(hours=1))})
    assert score_resp.status_code == 409
    events = client.get("/api/v1/mlops/governance-events", headers=auth_headers, params={"component_id": component["id"]}).json()["items"]
    assert any(e["event_type"] == "scoring_refused" for e in events)


def test_registering_a_differently_named_detector_is_rejected(client, auth_headers):
    """A component's name must match the underlying detector/forecaster's
    name — otherwise ComponentVersion.version_number (reused directly from
    the real ModelVersion.version, which is unique per (org, name) but NOT
    globally unique) could collide across two unrelated detectors
    registered under one component, and "component X's active version"
    would stop meaning anything coherent. See
    app.services.mlops_service.register_version's comment for the full
    reasoning — this was caught and fixed during Phase 5's self-review."""
    asset, sensor = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-NAMECHECK", sensor_code="temp")
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n=400, value_fn=lambda i: 50.0 + (i % 5) * 0.1)
    detector_resp = client.post(
        "/api/v1/anomaly/detectors", headers=auth_headers,
        json={"name": "actual-detector-name", "detector_type": "robust_zscore", "sensor_ids": [sensor["id"]], "reference_start": _iso(BASE), "reference_end": _iso(BASE + timedelta(minutes=5 * 399))},
    )
    detector = detector_resp.json()

    comp_resp = client.post("/api/v1/mlops/components", headers=auth_headers, json={"component_type": "statistical_detector", "name": "a-totally-different-component-name"})
    component = comp_resp.json()
    resp = client.post(f"/api/v1/mlops/components/{component['id']}/versions", headers=auth_headers, json={"version_kind": "model_version", "model_version_id": detector["id"]})
    assert resp.status_code == 422
    assert "not 'a-totally-different-component-name'" in resp.json()["error"]["message"]


def test_component_type_and_version_kind_mismatch_rejected(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-MISMATCH", sensor_code="temp")
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n=400, value_fn=lambda i: 50.0 + (i % 5) * 0.1)
    detector_resp = client.post(
        "/api/v1/anomaly/detectors", headers=auth_headers,
        json={"name": "temp-rz", "detector_type": "robust_zscore", "sensor_ids": [sensor["id"]], "reference_start": _iso(BASE), "reference_end": _iso(BASE + timedelta(minutes=5 * 399))},
    )
    detector = detector_resp.json()

    comp_resp = client.post("/api/v1/mlops/components", headers=auth_headers, json={"component_type": "trained_ml_model", "name": "wrong-kind-component"})
    component = comp_resp.json()
    mismatch = client.post(f"/api/v1/mlops/components/{component['id']}/versions", headers=auth_headers, json={"version_kind": "model_version", "model_version_id": detector["id"]})
    assert mismatch.status_code == 422  # robust_zscore cannot register under a trained_ml_model (isolation_forest-only) component


def test_viewer_cannot_perform_write_actions(client, auth_headers, db_session, org_and_admin):
    from app.services import auth_service
    org, _ = org_and_admin
    viewer_headers = _viewer_headers(client, db_session, org, auth_service)

    create_resp = client.post("/api/v1/mlops/components", headers=viewer_headers, json={"component_type": "trained_ml_model", "name": "viewer-blocked"})
    assert create_resp.status_code == 403

    comp_resp = client.post("/api/v1/mlops/components", headers=auth_headers, json={"component_type": "heuristic_signal_method", "name": "viewer-approve-test"})
    component = comp_resp.json()
    ver_resp = client.post(f"/api/v1/mlops/components/{component['id']}/versions", headers=auth_headers, json={"version_kind": "heuristic", "heuristic_code_version": "x@1"})
    version = ver_resp.json()
    client.post(f"/api/v1/mlops/versions/{version['id']}/validate", headers=auth_headers, json={"notes": "n", "evidence": {"reviewed_reference": "r"}})
    approve_denied = client.post(f"/api/v1/mlops/versions/{version['id']}/approve", headers=viewer_headers, json={})
    assert approve_denied.status_code == 403

    approve_ok = client.post(f"/api/v1/mlops/versions/{version['id']}/approve", headers=auth_headers, json={})
    assert approve_ok.status_code == 200
    activate_denied = client.post(f"/api/v1/mlops/components/{component['id']}/activate", headers=viewer_headers, json={"component_version_id": version["id"]})
    assert activate_denied.status_code == 403  # ENGINEER-level write is not enough for activation — ADMIN only


def test_cross_org_isolation_for_components(client, auth_headers, db_session):
    from app.services import auth_service
    comp_resp = client.post("/api/v1/mlops/components", headers=auth_headers, json={"component_type": "trained_ml_model", "name": "org-a-component"})
    component = comp_resp.json()

    other_org, other_admin = auth_service.register_organization(db_session, organization_name="Other MLOps Org", admin_email="admin@other-mlops.example", admin_password="AdminPass123!", admin_full_name="Other Admin")
    other_headers = {"Authorization": f"Bearer {auth_service.issue_token(other_admin)}"}

    get_resp = client.get(f"/api/v1/mlops/components/{component['id']}", headers=other_headers)
    assert get_resp.status_code == 404


# ---------------------------------------------------------------------------
# Shift analysis (§12)
# ---------------------------------------------------------------------------


def test_shift_analysis_insufficient_data_is_honest(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-SHIFTSMALL", sensor_code="temp")
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n=6, value_fn=lambda i: 50.0 + i)
    resp = client.post(
        "/api/v1/mlops/shift-analysis", headers=auth_headers,
        json={"sensor_id": sensor["id"], "reference_start": _iso(BASE), "reference_end": _iso(BASE + timedelta(minutes=10)), "comparison_start": _iso(BASE + timedelta(minutes=15)), "comparison_end": _iso(BASE + timedelta(minutes=25))},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["result"]["test_performed"] is False
    assert "insufficient" in data["method"]
    assert "PSI" in data["limitations"]


def test_shift_analysis_detects_a_real_mean_shift(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-SHIFTBIG", sensor_code="temp")
    rng = np.random.default_rng(21)
    n_ref, n_cmp = 200, 200
    ref_values = [50.0 + float(rng.normal(0, 0.5)) for _ in range(n_ref)]
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n=n_ref, value_fn=lambda i: ref_values[i])
    cmp_start = BASE + timedelta(minutes=5 * n_ref)
    cmp_values = [65.0 + float(rng.normal(0, 0.5)) for _ in range(n_cmp)]  # a clear +15 shift
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n=n_cmp, start=cmp_start, value_fn=lambda i: cmp_values[i])

    resp = client.post(
        "/api/v1/mlops/shift-analysis", headers=auth_headers,
        json={
            "sensor_id": sensor["id"], "reference_start": _iso(BASE), "reference_end": _iso(BASE + timedelta(minutes=5 * (n_ref - 1))),
            "comparison_start": _iso(cmp_start), "comparison_end": _iso(cmp_start + timedelta(minutes=5 * (n_cmp - 1))),
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["result"]["test_performed"] is True
    assert data["result"]["mean_difference"] > 10.0
    assert data["result"]["welch_t_test"]["p_value"] < 0.01
    assert data["result"]["ks_2samp_test"]["p_value"] < 0.01

    list_resp = client.get("/api/v1/mlops/shift-analysis", headers=auth_headers, params={"sensor_id": sensor["id"]})
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] == 1


# ---------------------------------------------------------------------------
# Monitoring (§11)
# ---------------------------------------------------------------------------


def test_mlops_monitoring_summary_reports_real_structure(client, auth_headers):
    resp = client.get("/api/v1/mlops/monitoring", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert set(data.keys()) >= {"operational", "data", "model_performance", "not_implemented"}
    assert "component_count_by_type" in data["operational"]
    assert "anomaly_detection" in data["model_performance"]
    assert "condition_and_forecasting" in data["model_performance"]
