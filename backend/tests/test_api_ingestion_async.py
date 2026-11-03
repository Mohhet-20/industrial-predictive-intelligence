import time


def test_async_batch_ingestion_job_runs_to_completion(client, auth_headers):
    """Regression test: the /ingestion/batches endpoint must be declared
    `async def` — job_runner.submit() calls asyncio.create_task(), which
    raises RuntimeError('no running event loop') if the route runs on
    FastAPI's sync worker threadpool instead of the event loop thread."""
    asset_resp = client.post(
        "/api/v1/assets", headers=auth_headers, json={"code": "PUMP-ASYNC", "name": "Async Pump", "asset_type": "pump"}
    )
    assert asset_resp.status_code == 201
    sensor_resp = client.post(
        "/api/v1/assets/" + asset_resp.json()["id"] + "/sensors",
        headers=auth_headers,
        json={"code": "temp", "name": "Temp", "measurement_type": "temperature", "unit": "celsius"},
    )
    assert sensor_resp.status_code == 201

    resp = client.post(
        "/api/v1/ingestion/batches",
        headers=auth_headers,
        json={
            "data_source_name": "async-test",
            "records": [
                {"asset_code": "PUMP-ASYNC", "sensor_code": "temp", "timestamp": "2026-01-01T00:00:00Z", "value": 10.0},
                {"asset_code": "PUMP-ASYNC", "sensor_code": "temp", "timestamp": "2026-01-01T00:05:00Z", "value": 11.0},
            ],
        },
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    # The job runs on a background asyncio task against a different DB
    # session than the test's db_session fixture, so poll the API rather
    # than the fixture session directly.
    final_status = None
    for _ in range(50):
        job_resp = client.get(f"/api/v1/jobs/{job_id}", headers=auth_headers)
        assert job_resp.status_code == 200
        final_status = job_resp.json()
        if final_status["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.05)

    assert final_status is not None
    assert final_status["status"] == "succeeded", final_status
    assert final_status["result"]["records_accepted"] == 2
