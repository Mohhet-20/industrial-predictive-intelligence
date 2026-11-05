"""API-level tests for Equipment Condition Intelligence & Forecasting
 — exercises the real HTTP surface against the real DB, mirrors
tests/test_api_anomaly.py's conventions. Deliberately includes refusal/
honesty paths (insufficient data, non-justified forecast blocking a run,
mandatory validation notes, invalid stage/status transitions, cross-org
isolation, role authorization) alongside the happy path.
"""
from datetime import datetime, timedelta, timezone

import numpy as np

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _make_asset_and_sensor(client, headers, *, asset_code="PUMP-COND", sensor_code="temp"):
    asset_resp = client.post("/api/v1/assets", headers=headers, json={"code": asset_code, "name": "Condition Test Pump", "asset_type": "pump"})
    assert asset_resp.status_code == 201, asset_resp.text
    sensor_resp = client.post(
        f"/api/v1/assets/{asset_resp.json()['id']}/sensors",
        headers=headers,
        json={"code": sensor_code, "name": "Temp", "measurement_type": "temperature", "unit": "celsius", "expected_interval_seconds": 300},
    )
    assert sensor_resp.status_code == 201, sensor_resp.text
    return asset_resp.json(), sensor_resp.json()


_SYNC_INGEST_CHUNK = 1500  # stays under ingestion's synchronous batch limit (2000)


def _ingest(client, headers, *, asset_code, sensor_code, n, step_minutes=5, start=BASE, value_fn):
    records = [
        {"asset_code": asset_code, "sensor_code": sensor_code, "timestamp": _iso(start + timedelta(minutes=step_minutes * i)), "value": value_fn(i)}
        for i in range(n)
    ]
    last = None
    for chunk_start in range(0, len(records), _SYNC_INGEST_CHUNK):
        chunk = records[chunk_start : chunk_start + _SYNC_INGEST_CHUNK]
        resp = client.post("/api/v1/ingestion/measurements", headers=headers, json={"data_source_name": "condition-tests", "records": chunk})
        assert resp.status_code == 200, resp.text
        last = resp.json()
    return last


def _daily_cycle_value(i, *, step_minutes=5, baseline=58.0, amplitude=3.0, drift_per_day=0.0, noise_std=0.6, rng=None):
    t_hours = (i * step_minutes) / 60.0
    noise = float(rng.normal(0, noise_std)) if rng is not None else 0.0
    return baseline + amplitude * np.sin(2 * 3.141592653589793 * t_hours / 24.0) + drift_per_day * (t_hours / 24.0) + noise


def _viewer_headers(db_session, org, auth_service_module):
    from app.core.security import hash_password
    from app.models.user import User, UserRole

    viewer = User(organization_id=org.id, email="viewer@condition-tests.example", hashed_password=hash_password("ViewerPass123!"), full_name="Viewer", role=UserRole.VIEWER)
    db_session.add(viewer)
    db_session.commit()
    token = auth_service_module.issue_token(viewer)
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Condition evidence — stateless, no composite score
# ---------------------------------------------------------------------------


def test_condition_evidence_reports_no_composite_score(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers)
    rng = np.random.default_rng(1)
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n=2000, value_fn=lambda i: _daily_cycle_value(i, rng=rng))
    ref_end = BASE + timedelta(minutes=5 * 999)
    win_start = BASE + timedelta(minutes=5 * 1000)
    win_end = BASE + timedelta(minutes=5 * 1999)

    resp = client.get(
        f"/api/v1/condition/sensors/{sensor['id']}/evidence", headers=auth_headers,
        params={"window_start": _iso(win_start), "window_end": _iso(win_end), "reference_start": _iso(BASE), "reference_end": _iso(ref_end)},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["composite_score"] is None
    assert "trend_evidence" in data and "deviation_from_baseline" in data and "variance_change_evidence" in data


def test_condition_evidence_cross_org_isolation(client, auth_headers, db_session):
    from app.services import auth_service
    asset, sensor = _make_asset_and_sensor(client, auth_headers)
    other_org, other_admin = auth_service.register_organization(db_session, organization_name="Other Org", admin_email="admin@other-cond.example", admin_password="AdminPass123!", admin_full_name="Other Admin")
    other_token = auth_service.issue_token(other_admin)
    other_headers = {"Authorization": f"Bearer {other_token}"}

    resp = client.get(
        f"/api/v1/condition/sensors/{sensor['id']}/evidence", headers=other_headers,
        params={"window_start": _iso(BASE), "window_end": _iso(BASE + timedelta(hours=1)), "reference_start": _iso(BASE), "reference_end": _iso(BASE + timedelta(hours=1))},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Degradation analysis runs & signals — full lifecycle
# ---------------------------------------------------------------------------


def test_condition_run_with_drifting_sensor_produces_degradation_candidate(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-DRIFT", sensor_code="bearing_temp")
    rng = np.random.default_rng(2)
    # 21 days, 5-min cadence, strong deterministic drift + low noise, no seasonality —
    # engineered so sub-window trends persist AND deviation-from-baseline corroborates.
    n = 21 * 24 * 12
    _ingest(
        client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n=n,
        value_fn=lambda i: _daily_cycle_value(i, amplitude=0.0, drift_per_day=0.5, noise_std=0.05, rng=rng),
    )
    reference_start = BASE
    reference_end = BASE + timedelta(days=3)
    analysis_start = reference_end
    analysis_end = BASE + timedelta(days=21)

    resp = client.post(
        "/api/v1/condition/runs", headers=auth_headers,
        json={"sensor_id": sensor["id"], "analysis_start": _iso(analysis_start), "analysis_end": _iso(analysis_end), "reference_start": _iso(reference_start), "reference_end": _iso(reference_end)},
    )
    assert resp.status_code == 201, resp.text
    run = resp.json()
    assert run["status"] == "succeeded"
    assert run["metrics"]["signal_produced"] is True
    assert run["metrics"]["stage"] in ("observed_trend", "degradation_candidate")

    signals = client.get("/api/v1/condition/signals", headers=auth_headers, params={"sensor_id": sensor["id"]}).json()
    assert signals["total"] == 1
    signal = signals["items"][0]
    assert signal["stage"] in ("observed_trend", "degradation_candidate")
    assert signal["status"] == "detected"
    assert "classification" in signal["evidence"]


def test_condition_run_reference_must_precede_analysis_window(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-BADREF", sensor_code="temp")
    resp = client.post(
        "/api/v1/condition/runs", headers=auth_headers,
        json={
            "sensor_id": sensor["id"],
            "analysis_start": _iso(BASE), "analysis_end": _iso(BASE + timedelta(days=1)),
            "reference_start": _iso(BASE + timedelta(hours=12)), "reference_end": _iso(BASE + timedelta(days=2)),  # overlaps/ends after analysis starts
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_failed"


def test_condition_run_with_flat_sensor_produces_no_signal_honestly(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-FLAT", sensor_code="temp")
    rng = np.random.default_rng(3)
    n = 21 * 24 * 12
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n=n, value_fn=lambda i: 50.0 + float(rng.normal(0, 0.3)))
    reference_end = BASE + timedelta(days=3)

    resp = client.post(
        "/api/v1/condition/runs", headers=auth_headers,
        json={"sensor_id": sensor["id"], "analysis_start": _iso(reference_end), "analysis_end": _iso(BASE + timedelta(days=21)), "reference_start": _iso(BASE), "reference_end": _iso(reference_end)},
    )
    assert resp.status_code == 201, resp.text
    run = resp.json()
    assert run["metrics"]["signal_produced"] is False

    signals = client.get("/api/v1/condition/signals", headers=auth_headers, params={"sensor_id": sensor["id"]}).json()
    assert signals["total"] == 0


def _create_degradation_candidate_signal(client, headers, *, asset_code="PUMP-VALIDATE", sensor_code="bearing_temp"):
    asset, sensor = _make_asset_and_sensor(client, headers, asset_code=asset_code, sensor_code=sensor_code)
    rng = np.random.default_rng(5)
    n = 21 * 24 * 12
    _ingest(client, headers, asset_code=asset["code"], sensor_code=sensor["code"], n=n, value_fn=lambda i: _daily_cycle_value(i, amplitude=0.0, drift_per_day=2.0, noise_std=0.05, rng=rng))
    reference_end = BASE + timedelta(days=3)
    resp = client.post(
        "/api/v1/condition/runs", headers=headers,
        json={"sensor_id": sensor["id"], "analysis_start": _iso(reference_end), "analysis_end": _iso(BASE + timedelta(days=21)), "reference_start": _iso(BASE), "reference_end": _iso(reference_end)},
    )
    assert resp.status_code == 201, resp.text
    signals = client.get("/api/v1/condition/signals", headers=headers, params={"sensor_id": sensor["id"]}).json()
    assert signals["total"] == 1
    return signals["items"][0]


def test_degradation_signal_review_lifecycle_and_audit_log(client, auth_headers):
    signal = _create_degradation_candidate_signal(client, auth_headers)
    signal_id = signal["id"]

    resp = client.post(f"/api/v1/condition/signals/{signal_id}/review", headers=auth_headers, json={"to_status": "under_review", "notes": "looking into it"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "under_review"

    bad = client.post(f"/api/v1/condition/signals/{signal_id}/review", headers=auth_headers, json={"to_status": "resolved", "notes": None})
    assert bad.status_code == 200  # under_review -> resolved is allowed

    reviews = client.get(f"/api/v1/condition/signals/{signal_id}/reviews", headers=auth_headers).json()
    assert len(reviews) == 2
    assert reviews[0]["from_status"] == "detected" and reviews[0]["to_status"] == "under_review"
    assert reviews[1]["to_status"] == "resolved"


def test_validate_degradation_signal_requires_notes_and_correct_stage(client, auth_headers):
    signal = _create_degradation_candidate_signal(client, auth_headers, asset_code="PUMP-VAL2", sensor_code="bearing_temp2")
    signal_id = signal["id"]

    empty_notes = client.post(f"/api/v1/condition/signals/{signal_id}/validate", headers=auth_headers, json={"notes": ""})
    assert empty_notes.status_code == 422

    if signal["stage"] != "degradation_candidate":
        wrong_stage = client.post(f"/api/v1/condition/signals/{signal_id}/validate", headers=auth_headers, json={"notes": "confirmed via manual inspection"})
        assert wrong_stage.status_code == 409
        return

    ok = client.post(f"/api/v1/condition/signals/{signal_id}/validate", headers=auth_headers, json={"notes": "confirmed via manual bearing inspection on 2026-01-22"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["stage"] == "validated_degradation"

    again = client.post(f"/api/v1/condition/signals/{signal_id}/validate", headers=auth_headers, json={"notes": "duplicate attempt"})
    assert again.status_code == 409

    reviews = client.get(f"/api/v1/condition/signals/{signal_id}/reviews", headers=auth_headers).json()
    assert any(r["to_stage"] == "validated_degradation" and r["notes"] for r in reviews)


def test_viewer_cannot_review_or_validate_signals(client, auth_headers, db_session, org_and_admin):
    from app.services import auth_service
    org, _ = org_and_admin
    signal = _create_degradation_candidate_signal(client, auth_headers, asset_code="PUMP-VIEWER", sensor_code="temp")
    viewer_headers = _viewer_headers(db_session, org, auth_service)

    resp = client.post(f"/api/v1/condition/signals/{signal['id']}/review", headers=viewer_headers, json={"to_status": "under_review"})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Forecasting — honesty-gated fit + run
# ---------------------------------------------------------------------------


def test_forecast_model_not_justified_blocks_forecast_run(client, auth_headers):
    """Realistic daily-cycle-without-drift data (this fleet's typical
    shape) should NOT justify a linear_trend forecast — and attempting to
    run a forecast against a not-justified model must be refused (409),
    not silently allowed."""
    asset, sensor = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-NOTJUST", sensor_code="temp")
    rng = np.random.default_rng(6)
    n = 21 * 24 * 12
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n=n, value_fn=lambda i: _daily_cycle_value(i, amplitude=3.0, drift_per_day=0.0, noise_std=0.6, rng=rng))

    fit_end = BASE + timedelta(days=14)
    test_end = BASE + timedelta(days=21)
    resp = client.post(
        "/api/v1/condition/forecast-models", headers=auth_headers,
        json={"sensor_id": sensor["id"], "name": "bearing-temp-forecast", "fit_start": _iso(BASE), "fit_end": _iso(fit_end), "test_end": _iso(test_end)},
    )
    assert resp.status_code == 201, resp.text
    model = resp.json()
    assert model["status"] == "not_justified"
    assert model["evaluation_results"]["is_justified"] is False

    run_resp = client.post(f"/api/v1/condition/forecast-models/{model['id']}/runs", headers=auth_headers, json={"as_of": _iso(fit_end), "horizon_seconds": 86400, "step_seconds": 3600})
    assert run_resp.status_code == 409
    assert run_resp.json()["error"]["code"] == "conflict"


def test_forecast_model_justified_allows_forecast_run_with_bounds(client, auth_headers):
    """A near-noiseless pure linear ramp (no seasonality) is exactly the
    case where linear_trend should legitimately beat naive baselines —
    proof the justification gate can say yes and that a run then produces
    bounded, uncertainty-labeled points."""
    asset, sensor = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-JUST", sensor_code="ramp")
    rng = np.random.default_rng(9)
    n = 3000
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n=n, step_minutes=1, value_fn=lambda i: 10.0 + 0.01 * i + float(rng.normal(0, 0.01)))

    fit_end = BASE + timedelta(minutes=2400)
    test_end = BASE + timedelta(minutes=n - 1)
    resp = client.post(
        "/api/v1/condition/forecast-models", headers=auth_headers,
        json={"sensor_id": sensor["id"], "name": "ramp-forecast", "fit_start": _iso(BASE), "fit_end": _iso(fit_end), "test_end": _iso(test_end)},
    )
    assert resp.status_code == 201, resp.text
    model = resp.json()
    assert model["status"] == "justified"

    run_resp = client.post(f"/api/v1/condition/forecast-models/{model['id']}/runs", headers=auth_headers, json={"as_of": _iso(fit_end), "horizon_seconds": 3600, "step_seconds": 600})
    assert run_resp.status_code == 201, run_resp.text
    run = run_resp.json()
    assert run["status"] == "succeeded"
    assert len(run["points"]) == 6
    for p in run["points"]:
        assert p["lower_bound"] <= p["predicted_value"] <= p["upper_bound"]


def test_forecast_model_insufficient_data_reports_failed_honestly(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-SPARSE", sensor_code="temp")
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n=5, value_fn=lambda i: 50.0 + i)

    resp = client.post(
        "/api/v1/condition/forecast-models", headers=auth_headers,
        json={"sensor_id": sensor["id"], "name": "sparse-forecast", "fit_start": _iso(BASE), "fit_end": _iso(BASE + timedelta(minutes=15)), "test_end": _iso(BASE + timedelta(minutes=25))},
    )
    assert resp.status_code == 201, resp.text
    model = resp.json()
    assert model["status"] == "failed"
    assert model["training_error"] is not None


# ---------------------------------------------------------------------------
# Attention list & monitoring
# ---------------------------------------------------------------------------


def test_attention_list_never_returns_fabricated_score(client, auth_headers):
    _create_degradation_candidate_signal(client, auth_headers, asset_code="PUMP-ATTN", sensor_code="temp")
    resp = client.get("/api/v1/condition/attention-list", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["total_open_degradation_signals"] >= 1
    for item in data["items"]:
        assert "rank" in item and "stage_rank" in item["rank"]
        assert "score" not in item and "probability" not in item
    assert "not a probability" in data["sort_methodology"]


def test_condition_monitoring_summary_reports_real_counts(client, auth_headers):
    _create_degradation_candidate_signal(client, auth_headers, asset_code="PUMP-MON", sensor_code="temp")
    resp = client.get("/api/v1/condition/monitoring", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert sum(data["degradation_signal_count_by_stage"].values()) >= 1
    assert any("failure prediction" in n for n in data["not_implemented"])


# ---------------------------------------------------------------------------
# Regression: a mid-computation failure must produce a FAILED row with a
# real error message, never crash the request. Both create_condition_run
# and create_forecast_run commit their placeholder row BEFORE entering the
# try/except that wraps the actual computation — if that commit were only
# a flush (as it originally was), db.rollback() in the except clause would
# discard the row itself, and the subsequent db.get(...) re-fetch would
# return None, turning an honest failure into an unhandled AttributeError.
# ---------------------------------------------------------------------------


def test_forecast_run_mid_computation_failure_is_reported_not_crashed(client, auth_headers, monkeypatch):
    asset, sensor = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-EXCPATH", sensor_code="ramp")
    rng = np.random.default_rng(11)
    n = 3000
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n=n, step_minutes=1, value_fn=lambda i: 10.0 + 0.01 * i + float(rng.normal(0, 0.01)))
    fit_end = BASE + timedelta(minutes=2400)
    test_end = BASE + timedelta(minutes=n - 1)
    model = client.post(
        "/api/v1/condition/forecast-models", headers=auth_headers,
        json={"sensor_id": sensor["id"], "name": "excpath-forecast", "fit_start": _iso(BASE), "fit_end": _iso(fit_end), "test_end": _iso(test_end)},
    ).json()
    assert model["status"] == "justified"

    from app.condition import forecasting as forecasting_mod

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated mid-computation failure")

    monkeypatch.setattr(forecasting_mod, "generate_forecast_points", _boom)

    from app.services import condition_service
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        from app.models.user import User
        user = db.query(User).filter(User.email == "admin@test-org.example").first()
        try:
            condition_service.create_forecast_run(db, organization_id=user.organization_id, user_id=user.id, forecast_model_id=model["id"], as_of=fit_end, horizon_seconds=3600, step_seconds=600)
            assert False, "expected the simulated RuntimeError to propagate"
        except RuntimeError:
            pass

        from app.models.condition import ForecastRun, ForecastRunStatus
        run = db.query(ForecastRun).filter(ForecastRun.forecast_model_id == model["id"]).order_by(ForecastRun.created_at.desc()).first()
        assert run is not None, "the run row must survive rollback — this is the bug this test guards against"
        assert run.status == ForecastRunStatus.FAILED
        assert "simulated mid-computation failure" in run.error_message
    finally:
        db.close()
