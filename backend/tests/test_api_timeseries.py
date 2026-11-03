import time
from datetime import datetime, timedelta, timezone

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _make_asset_and_sensor(client, headers, *, asset_code="PUMP-TS", sensor_code="temp"):
    asset_resp = client.post("/api/v1/assets", headers=headers, json={"code": asset_code, "name": "TS Pump", "asset_type": "pump"})
    assert asset_resp.status_code == 201, asset_resp.text
    sensor_resp = client.post(
        f"/api/v1/assets/{asset_resp.json()['id']}/sensors",
        headers=headers,
        json={"code": sensor_code, "name": "Temp", "measurement_type": "temperature", "unit": "celsius", "expected_interval_seconds": 300},
    )
    assert sensor_resp.status_code == 201, sensor_resp.text
    return asset_resp.json(), sensor_resp.json()


def _ingest(client, headers, *, asset_code, sensor_code, n=120, step_minutes=5, start=BASE, value_fn=None):
    value_fn = value_fn or (lambda i: 50.0 + (i % 7))
    records = [
        {"asset_code": asset_code, "sensor_code": sensor_code, "timestamp": _iso(start + timedelta(minutes=step_minutes * i)), "value": value_fn(i)}
        for i in range(n)
    ]
    resp = client.post("/api/v1/ingestion/measurements", headers=headers, json={"data_source_name": "ts-tests", "records": records})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_profile_endpoint_end_to_end(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers)
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n=60)

    resp = client.get(
        f"/api/v1/timeseries/sensors/{sensor['id']}/profile",
        headers=auth_headers,
        params={"start": _iso(BASE), "end": _iso(BASE + timedelta(hours=6))},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["observation_count"] == 60
    assert data["descriptive_stats"] is not None


def test_profile_endpoint_rejects_sensor_from_other_org(client, auth_headers, db_session):
    from app.services import auth_service

    asset, sensor = _make_asset_and_sensor(client, auth_headers)
    other_org, other_admin = auth_service.register_organization(
        db_session, organization_name="Other Org", admin_email="admin@other-org.example", admin_password="OtherPass123!", admin_full_name="Other Admin"
    )
    other_token = auth_service.issue_token(other_admin)
    resp = client.get(
        f"/api/v1/timeseries/sensors/{sensor['id']}/profile",
        headers={"Authorization": f"Bearer {other_token}"},
        params={"start": _iso(BASE), "end": _iso(BASE + timedelta(hours=1))},
    )
    assert resp.status_code == 404


def test_profile_endpoint_rejects_invalid_range(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers)
    resp = client.get(
        f"/api/v1/timeseries/sensors/{sensor['id']}/profile",
        headers=auth_headers,
        params={"start": _iso(BASE + timedelta(hours=1)), "end": _iso(BASE)},  # end before start
    )
    assert resp.status_code == 422


def test_temporal_analysis_endpoint(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers)
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n=100)
    resp = client.get(
        f"/api/v1/timeseries/sensors/{sensor['id']}/analysis",
        headers=auth_headers,
        params={"start": _iso(BASE), "end": _iso(BASE + timedelta(hours=10)), "rolling_window_seconds": 3600, "volatility_window_seconds": 3600, "autocorr_max_lag_steps": 5},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert "rolling" in data and "rate_of_change" in data and "autocorrelation" in data


def test_resample_endpoint_bounds_and_flags(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers)
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n=20)
    resp = client.get(
        f"/api/v1/timeseries/sensors/{sensor['id']}/resample",
        headers=auth_headers,
        params={"start": _iso(BASE), "end": _iso(BASE + timedelta(hours=2)), "freq_seconds": 600, "aggregation": "mean", "interpolation": "none"},
    )
    assert resp.status_code == 200, resp.text
    buckets = resp.json()["data"]["buckets"]
    assert len(buckets) > 0
    assert all("is_observed" in b and "is_interpolated" in b for b in buckets)


def test_change_candidates_endpoint(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers)
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n=80, value_fn=lambda i: 50.0 if i < 40 else 90.0)
    resp = client.get(
        f"/api/v1/timeseries/sensors/{sensor['id']}/change-candidates",
        headers=auth_headers,
        params={"start": _iso(BASE), "end": _iso(BASE + timedelta(hours=7)), "window_size": 15},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data["candidates"]) >= 1
    assert "not confirmed" in data["note"] or "CANDIDATE" in data["note"].upper()


def test_correlation_endpoint_requires_two_sensors(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers)
    resp = client.get(
        "/api/v1/timeseries/correlation",
        headers=auth_headers,
        params={"sensor_ids": [sensor["id"]], "start": _iso(BASE), "end": _iso(BASE + timedelta(hours=1)), "freq_seconds": 300},
    )
    assert resp.status_code == 422


def test_correlation_endpoint_end_to_end(client, auth_headers):
    asset, sensor_a = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-CORR", sensor_code="temp")
    sensor_b_resp = client.post(
        f"/api/v1/assets/{asset['id']}/sensors", headers=auth_headers, json={"code": "vib", "name": "Vibration", "measurement_type": "vibration", "unit": "mm_s"}
    )
    sensor_b = sensor_b_resp.json()
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code="temp", n=40)
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code="vib", n=40)

    resp = client.get(
        "/api/v1/timeseries/correlation",
        headers=auth_headers,
        params={"sensor_ids": [sensor_a["id"], sensor_b["id"]], "start": _iso(BASE), "end": _iso(BASE + timedelta(hours=4)), "freq_seconds": 300},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert "association" in data["caveat"].lower()


def test_feature_preview_endpoint(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers)
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n=60)
    as_of = _iso(BASE + timedelta(hours=4))
    resp = client.post(
        "/api/v1/timeseries/feature-preview",
        headers=auth_headers,
        json={"as_of": as_of, "specs": [{"feature_name": "mean_1h", "sensor_id": sensor["id"], "kind": "mean", "window_seconds": 3600}]},
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()["results"][0]
    assert result["feature_name"] == "mean_1h"
    assert result["result"]["computed"] is True


def test_feature_definition_versioning(client, auth_headers):
    payload = {"name": "hourly_mean", "category": "statistical", "kind": "mean", "window_seconds": 3600}
    v1 = client.post("/api/v1/timeseries/feature-definitions", headers=auth_headers, json=payload)
    assert v1.status_code == 201, v1.text
    assert v1.json()["version"] == 1

    v2 = client.post("/api/v1/timeseries/feature-definitions", headers=auth_headers, json=payload)
    assert v2.status_code == 201
    assert v2.json()["version"] == 2  # never updates — always a new version
    assert v1.json()["id"] != v2.json()["id"]

    listing = client.get("/api/v1/timeseries/feature-definitions", headers=auth_headers, params={"name": "hourly_mean"})
    assert listing.status_code == 200
    assert listing.json()["total"] == 2

    fetched = client.get(f"/api/v1/timeseries/feature-definitions/{v1.json()['id']}", headers=auth_headers)
    assert fetched.status_code == 200
    assert fetched.json()["version"] == 1  # v1's own row is untouched by creating v2


def test_dataset_build_sync_path_and_rows(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers)
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n=60)

    build_resp = client.post(
        "/api/v1/timeseries/datasets",
        headers=auth_headers,
        json={
            "sensor_ids": [sensor["id"]],
            "feature_specs": [{"feature_name": "mean_1h", "sensor_id": sensor["id"], "kind": "mean", "window_seconds": 3600}],
            "grid_start": _iso(BASE + timedelta(hours=1)),
            "grid_end": _iso(BASE + timedelta(hours=2)),
            "step_seconds": 600,
        },
    )
    assert build_resp.status_code == 201, build_resp.text
    build = build_resp.json()
    assert build["background_job_id"] is None  # small enough to build synchronously
    assert build["null_feature_rate"] is not None

    rows_resp = client.get(f"/api/v1/timeseries/datasets/{build['id']}/rows", headers=auth_headers, params={"limit": 3, "offset": 0})
    assert rows_resp.status_code == 200, rows_resp.text
    rows_data = rows_resp.json()
    assert rows_data["row_count_returned"] == 3
    assert "at or before" in rows_data["leakage_policy"]


def test_dataset_build_with_target_never_leaks_end_to_end(client, auth_headers):
    """API-level companion to test_dataset_builder.py's unit-level leakage
    test: builds a real dataset through the HTTP API with a target and
    confirms the response never puts a future value into a feature."""
    asset, sensor = _make_asset_and_sensor(client, auth_headers)
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n=100, value_fn=lambda i: 50.0 + i * 0.01)

    build_resp = client.post(
        "/api/v1/timeseries/datasets",
        headers=auth_headers,
        json={
            "sensor_ids": [sensor["id"]],
            "feature_specs": [{"feature_name": "last_1h_mean", "sensor_id": sensor["id"], "kind": "mean", "window_seconds": 3600}],
            "grid_start": _iso(BASE + timedelta(hours=2)),
            "grid_end": _iso(BASE + timedelta(hours=3)),
            "step_seconds": 1800,
            "target": {"sensor_id": sensor["id"], "horizon_seconds": 1800, "tolerance_seconds": 300},
        },
    )
    assert build_resp.status_code == 201, build_resp.text
    build = build_resp.json()

    rows = client.get(f"/api/v1/timeseries/datasets/{build['id']}/rows", headers=auth_headers, params={"limit": 10}).json()["rows"]
    for row in rows:
        feature_val = row["features"]["last_1h_mean"]["value"]
        target_val = row["target"]["value"]
        if feature_val is not None and target_val is not None:
            # the series is monotonically increasing, so a leaked future value
            # in the feature would make it >= the target it's supposed to predict
            assert feature_val < target_val


def test_dataset_build_async_path_runs_via_job_runner(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers)
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n=200, step_minutes=1)

    from app.services import feature_service

    original = feature_service.MAX_DATASET_ROWS_SYNC
    feature_service.MAX_DATASET_ROWS_SYNC = 1  # force the async path without needing a huge grid
    try:
        build_resp = client.post(
            "/api/v1/timeseries/datasets",
            headers=auth_headers,
            json={
                "sensor_ids": [sensor["id"]],
                "feature_specs": [{"feature_name": "mean_10m", "sensor_id": sensor["id"], "kind": "mean", "window_seconds": 600}],
                "grid_start": _iso(BASE),
                "grid_end": _iso(BASE + timedelta(minutes=30)),
                "step_seconds": 300,
            },
        )
    finally:
        feature_service.MAX_DATASET_ROWS_SYNC = original

    assert build_resp.status_code == 201, build_resp.text
    build = build_resp.json()
    assert build["background_job_id"] is not None
    assert build["null_feature_rate"] is None  # not yet computed

    final_job = None
    for _ in range(50):
        job_resp = client.get(f"/api/v1/jobs/{build['background_job_id']}", headers=auth_headers)
        final_job = job_resp.json()
        if final_job["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.05)
    assert final_job["status"] == "succeeded", final_job

    build_after = client.get(f"/api/v1/timeseries/datasets/{build['id']}", headers=auth_headers).json()
    assert build_after["null_feature_rate"] is not None


def test_split_endpoints(client, auth_headers):
    resp = client.get(
        "/api/v1/timeseries/splits/chronological",
        headers=auth_headers,
        params={"start": _iso(BASE), "end": _iso(BASE + timedelta(days=10)), "train_fraction": 0.7, "val_fraction": 0.15},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["leakage_safe"] is True

    rolling_resp = client.get(
        "/api/v1/timeseries/splits/rolling-window",
        headers=auth_headers,
        params={"start": _iso(BASE), "end": _iso(BASE + timedelta(days=10)), "train_window_seconds": 86400 * 3, "test_window_seconds": 86400, "step_seconds": 86400},
    )
    assert rolling_resp.status_code == 200, rolling_resp.text
    assert len(rolling_resp.json()["folds"]) > 0

    expanding_resp = client.get(
        "/api/v1/timeseries/splits/expanding-window",
        headers=auth_headers,
        params={"start": _iso(BASE), "end": _iso(BASE + timedelta(days=10)), "initial_train_window_seconds": 86400 * 2, "test_window_seconds": 86400, "step_seconds": 86400},
    )
    assert expanding_resp.status_code == 200, expanding_resp.text
    assert all(f["train_start"] == _iso(BASE) for f in expanding_resp.json()["folds"])


def test_oversized_dataset_grid_is_rejected(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers)
    resp = client.post(
        "/api/v1/timeseries/datasets",
        headers=auth_headers,
        json={
            "sensor_ids": [sensor["id"]],
            "feature_specs": [{"feature_name": "m", "sensor_id": sensor["id"], "kind": "mean", "window_seconds": 3600}],
            "grid_start": _iso(BASE),
            "grid_end": _iso(BASE + timedelta(days=365 * 5)),
            "step_seconds": 1,  # would produce an enormous number of rows
        },
    )
    assert resp.status_code == 422
