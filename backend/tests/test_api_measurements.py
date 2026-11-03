from datetime import datetime, timedelta, timezone

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _setup_asset_sensor(client, auth_headers):
    asset_resp = client.post(
        "/api/v1/assets", headers=auth_headers, json={"code": "PUMP-01", "name": "Pump", "asset_type": "pump"}
    )
    asset_id = asset_resp.json()["id"]
    sensor_resp = client.post(
        f"/api/v1/assets/{asset_id}/sensors",
        headers=auth_headers,
        json={"code": "temp", "name": "Temp", "measurement_type": "temperature", "unit": "celsius", "expected_min": 0, "expected_max": 100},
    )
    return asset_id, sensor_resp.json()["id"]


def test_ingest_and_query_measurements_roundtrip(client, auth_headers):
    asset_id, sensor_id = _setup_asset_sensor(client, auth_headers)

    records = [
        {
            "asset_code": "PUMP-01",
            "sensor_code": "temp",
            "timestamp": (BASE + timedelta(minutes=i * 5)).isoformat(),
            "value": 50.0 + i,
            "unit": "celsius",
        }
        for i in range(10)
    ]
    ingest_resp = client.post(
        "/api/v1/ingestion/measurements",
        headers=auth_headers,
        json={"data_source_name": "test-upload", "records": records},
    )
    assert ingest_resp.status_code == 200
    body = ingest_resp.json()
    assert body["records_accepted"] == 10

    query_resp = client.get(
        "/api/v1/measurements",
        headers=auth_headers,
        params={
            "sensor_ids": [sensor_id],
            "start_time": BASE.isoformat(),
            "end_time": (BASE + timedelta(hours=1)).isoformat(),
        },
    )
    assert query_resp.status_code == 200
    payload = query_resp.json()
    assert payload["total"] == 10
    assert payload["items"][0]["value"] == 50.0


def test_measurement_query_requires_time_range(client, auth_headers):
    _, sensor_id = _setup_asset_sensor(client, auth_headers)
    resp = client.get("/api/v1/measurements", headers=auth_headers, params={"sensor_ids": [sensor_id]})
    assert resp.status_code == 422


def test_measurement_query_end_before_start_rejected(client, auth_headers):
    _, sensor_id = _setup_asset_sensor(client, auth_headers)
    resp = client.get(
        "/api/v1/measurements",
        headers=auth_headers,
        params={"sensor_ids": [sensor_id], "start_time": BASE.isoformat(), "end_time": (BASE - timedelta(hours=1)).isoformat()},
    )
    assert resp.status_code == 422


def test_stats_endpoint(client, auth_headers):
    asset_id, sensor_id = _setup_asset_sensor(client, auth_headers)
    records = [
        {"asset_code": "PUMP-01", "sensor_code": "temp", "timestamp": (BASE + timedelta(minutes=i * 5)).isoformat(), "value": float(i), "unit": "celsius"}
        for i in range(5)
    ]
    client.post("/api/v1/ingestion/measurements", headers=auth_headers, json={"data_source_name": "s", "records": records})
    resp = client.get(f"/api/v1/stats/sensors/{sensor_id}", headers=auth_headers)
    assert resp.status_code == 200
    stats = resp.json()
    assert stats["count"] == 5
    assert stats["min_value"] == 0.0
    assert stats["max_value"] == 4.0


def test_ingestion_batch_history_visible(client, auth_headers):
    _setup_asset_sensor(client, auth_headers)
    client.post(
        "/api/v1/ingestion/measurements",
        headers=auth_headers,
        json={
            "data_source_name": "s",
            "records": [{"asset_code": "PUMP-01", "sensor_code": "temp", "timestamp": BASE.isoformat(), "value": 1.0, "unit": "celsius"}],
        },
    )
    resp = client.get("/api/v1/ingestion/batches", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


def test_data_quality_visible_after_ingestion(client, auth_headers):
    asset_id, _ = _setup_asset_sensor(client, auth_headers)
    client.post(
        "/api/v1/ingestion/measurements",
        headers=auth_headers,
        json={
            "data_source_name": "s",
            "records": [{"asset_code": "PUMP-01", "sensor_code": "temp", "timestamp": BASE.isoformat(), "value": 999.0, "unit": "celsius"}],
        },
    )
    resp = client.get("/api/v1/data-quality/issues", headers=auth_headers, params={"asset_id": asset_id})
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1

    summary = client.get(f"/api/v1/data-quality/summary/{asset_id}", headers=auth_headers)
    assert summary.status_code == 200
    assert summary.json()["total_issues"] >= 1


def test_old_intelligence_predictions_stub_route_removed(client, auth_headers):
    # Phase 3 stubbed predictive maintenance at /intelligence/predictions
    # (honest 501 "not yet available"). Phase 4 replaced it with a real,
    # honesty-gated forecasting implementation at /condition/forecast-models
    # — see tests/test_api_condition.py for its refusal-path coverage
    # (NOT_JUSTIFIED/FAILED models correctly blocking forecast runs). The
    # old stub route no longer exists at all.
    resp = client.get("/api/v1/intelligence/predictions", headers=auth_headers)
    assert resp.status_code == 404
