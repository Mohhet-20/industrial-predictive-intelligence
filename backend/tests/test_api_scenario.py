"""API-level tests for Scenario Simulation / What-If Analysis (Phase 11)
— exercises the real HTTP surface against the real DB, mirroring
tests/test_api_condition.py's conventions. Covers the full create ->
validate -> run -> compare -> inspect workflow, the non-destructive
guarantee (real measurements are byte-identical after a run), multi-sensor
scenarios, sensitivity sweeps, authz, and cross-org isolation.
"""
from datetime import datetime, timedelta, timezone

import numpy as np

from app.core.security import hash_password
from app.models.measurement import SensorMeasurement
from app.models.sensor import Sensor
from app.models.user import User, UserRole
from app.services import auth_service as auth_service_module

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _make_asset_and_sensor(client, headers, *, asset_code="PUMP-SCN", sensor_code="temp", unit="celsius"):
    asset_resp = client.post("/api/v1/assets", headers=headers, json={"code": asset_code, "name": "Scenario Test Pump", "asset_type": "pump"})
    assert asset_resp.status_code == 201, asset_resp.text
    sensor_resp = client.post(
        f"/api/v1/assets/{asset_resp.json()['id']}/sensors",
        headers=headers,
        json={"code": sensor_code, "name": "Temp", "measurement_type": "temperature", "unit": unit, "expected_interval_seconds": 300},
    )
    assert sensor_resp.status_code == 201, sensor_resp.text
    return asset_resp.json(), sensor_resp.json()


_SYNC_INGEST_CHUNK = 1500


def _ingest(client, headers, *, asset_code, sensor_code, n, step_minutes=5, start=BASE, value_fn):
    records = [
        {"asset_code": asset_code, "sensor_code": sensor_code, "timestamp": _iso(start + timedelta(minutes=step_minutes * i)), "value": value_fn(i)}
        for i in range(n)
    ]
    for chunk_start in range(0, len(records), _SYNC_INGEST_CHUNK):
        chunk = records[chunk_start : chunk_start + _SYNC_INGEST_CHUNK]
        resp = client.post("/api/v1/ingestion/measurements", headers=headers, json={"data_source_name": "scenario-tests", "records": chunk})
        assert resp.status_code == 200, resp.text


def _stable_value(i, *, baseline=50.0, amplitude=2.0, seed=11, noise_std=0.2):
    rng = np.random.default_rng(seed + i)  # deterministic per-index, but varied enough to have real spread
    return baseline + amplitude * np.sin(i / 20.0) + float(rng.normal(0, noise_std))


def _viewer_headers(db_session, org):
    viewer = User(organization_id=org.id, email="viewer@scenario-tests.example", hashed_password=hash_password("ViewerPass123!"), full_name="Viewer", role=UserRole.VIEWER)
    db_session.add(viewer)
    db_session.commit()
    token = auth_service_module.issue_token(viewer)
    return {"Authorization": f"Bearer {token}"}


def _seed_and_create_scenario(client, auth_headers, *, asset_code="PUMP-SCN", sensor_code="temp", n=400, scenario_type="level_shift", magnitude=5.0, modification_duration_seconds=None, extra_payload=None):
    asset, sensor = _make_asset_and_sensor(client, auth_headers, asset_code=asset_code, sensor_code=sensor_code)
    _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor["code"], n=n, value_fn=_stable_value)

    baseline_start = BASE + timedelta(minutes=5 * (n - 150))
    baseline_end = BASE + timedelta(minutes=5 * (n - 1))
    modification_start = BASE + timedelta(minutes=5 * (n - 100))

    payload = {
        "asset_id": asset["id"], "sensor_id": sensor["id"], "name": "Test scenario",
        "scenario_type": scenario_type, "baseline_window_start": _iso(baseline_start), "baseline_window_end": _iso(baseline_end),
        "modification_start": _iso(modification_start), "modification_duration_seconds": modification_duration_seconds,
        "magnitude": magnitude, "unit": "celsius", "parameters": {},
    }
    if extra_payload:
        payload.update(extra_payload)
    resp = client.post("/api/v1/scenarios", headers=auth_headers, json=payload)
    assert resp.status_code == 201, resp.text
    return asset, sensor, resp.json()


class TestHappyPathWorkflow:
    def test_create_validate_run_inspect(self, client, auth_headers):
        asset, sensor, scenario = _seed_and_create_scenario(client, auth_headers)
        assert scenario["status"] == "draft"

        validate_resp = client.post(f"/api/v1/scenarios/{scenario['id']}/validate", headers=auth_headers)
        assert validate_resp.status_code == 200, validate_resp.text
        validated = validate_resp.json()
        assert validated["status"] == "validated"
        assert validated["validation_result"]["ok"] is True

        run_resp = client.post(f"/api/v1/scenarios/{scenario['id']}/runs", headers=auth_headers, json={"include_forecast_comparison": True})
        assert run_resp.status_code == 201, run_resp.text
        run = run_resp.json()
        assert run["status"] == "succeeded"
        assert run["scenario_version"] == 1

        results = run["results"]
        assert results["baseline"]["label"] == "OBSERVED (real sensor data)"
        assert results["simulated"]["label"] == "SIMULATED SCENARIO RESULT"
        assert results["simulated"]["is_simulated"] is True
        assert "comparison" in results
        assert "limitations" in results and len(results["limitations"]) > 0
        assert "provenance" in results

        get_resp = client.get(f"/api/v1/scenarios/runs/{run['id']}", headers=auth_headers)
        assert get_resp.status_code == 200
        assert get_resp.json()["id"] == run["id"]

        list_resp = client.get(f"/api/v1/scenarios/{scenario['id']}/runs", headers=auth_headers)
        assert list_resp.status_code == 200
        assert list_resp.json()["total"] == 1

    def test_run_before_validate_is_refused(self, client, auth_headers):
        asset, sensor, scenario = _seed_and_create_scenario(client, auth_headers)
        resp = client.post(f"/api/v1/scenarios/{scenario['id']}/runs", headers=auth_headers, json={})
        assert resp.status_code == 409, resp.text

    def test_level_shift_produces_the_configured_mean_difference(self, client, auth_headers):
        # n=400: baseline window is indices [250, 399] (150 points); modification_start is
        # index 300, so 100 of those 150 baseline points are inside the shifted window —
        # the whole-window mean shift is exactly magnitude * (100/150), not the raw magnitude.
        _asset, _sensor, scenario = _seed_and_create_scenario(client, auth_headers, magnitude=8.0)
        client.post(f"/api/v1/scenarios/{scenario['id']}/validate", headers=auth_headers)
        run = client.post(f"/api/v1/scenarios/{scenario['id']}/runs", headers=auth_headers, json={}).json()
        mean_diff = run["results"]["comparison"]["value_summary_diff"]["mean"]["absolute_diff"]
        expected = 8.0 * (100 / 150)
        assert abs(mean_diff - expected) < 0.05


class TestValidationRejection:
    def test_magnitude_beyond_safety_bound_is_rejected_not_clamped(self, client, auth_headers):
        _asset, _sensor, scenario = _seed_and_create_scenario(client, auth_headers, magnitude=100_000.0)
        resp = client.post(f"/api/v1/scenarios/{scenario['id']}/validate", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "draft"  # stayed draft — never silently promoted
        assert body["validation_result"]["ok"] is False
        assert any("exceeds the configured safety bound" in e for e in body["validation_result"]["errors"])

    def test_unit_mismatch_is_rejected(self, client, auth_headers):
        _asset, _sensor, scenario = _seed_and_create_scenario(client, auth_headers, extra_payload={"unit": "fahrenheit"})
        resp = client.post(f"/api/v1/scenarios/{scenario['id']}/validate", headers=auth_headers)
        body = resp.json()
        assert body["validation_result"]["ok"] is False


class TestMissingDataGapScenario:
    """Phase 15 Phase H: the 'missing-data interval' perturbation type —
    observations are DROPPED (not changed) within the modification window,
    and that drop must (a) never touch the real stored measurements and
    (b) show up honestly in the simulated bundle's observation_count,
    distinct from every other scenario type's sensor-units magnitude."""

    def test_full_gap_reduces_simulated_observation_count_without_touching_real_data(self, client, auth_headers, db_session):
        asset, sensor, scenario = _seed_and_create_scenario(
            client, auth_headers, scenario_type="missing_data_gap", magnitude=1.0, modification_duration_seconds=7200,
        )
        before = {
            (m.timestamp.isoformat(), round(m.value, 10))
            for m in db_session.query(SensorMeasurement).filter(SensorMeasurement.sensor_id == sensor["id"]).all()
        }

        validate_resp = client.post(f"/api/v1/scenarios/{scenario['id']}/validate", headers=auth_headers)
        assert validate_resp.status_code == 200, validate_resp.text
        assert validate_resp.json()["validation_result"]["ok"] is True

        run_resp = client.post(f"/api/v1/scenarios/{scenario['id']}/runs", headers=auth_headers, json={})
        assert run_resp.status_code == 201, run_resp.text
        results = run_resp.json()["results"]

        baseline_count = results["baseline"]["observation_count"]
        simulated_count = results["simulated"]["observation_count"]
        assert simulated_count < baseline_count  # the gap actually removed observations from the analyzed series
        assert results["comparison"]["observation_count"] == {"baseline": baseline_count, "simulated": simulated_count}
        assert results["simulated"]["label"] == "SIMULATED SCENARIO RESULT"

        after = {
            (m.timestamp.isoformat(), round(m.value, 10))
            for m in db_session.query(SensorMeasurement).filter(SensorMeasurement.sensor_id == sensor["id"]).all()
        }
        assert before == after  # the real stored measurements are untouched — only the simulated copy lost points

    def test_fraction_magnitude_is_rejected_above_one_unlike_other_types_sensor_unit_magnitude(self, client, auth_headers):
        _asset, _sensor, scenario = _seed_and_create_scenario(
            client, auth_headers, scenario_type="missing_data_gap", magnitude=5.0, modification_duration_seconds=7200,
        )
        resp = client.post(f"/api/v1/scenarios/{scenario['id']}/validate", headers=auth_headers)
        body = resp.json()
        assert body["validation_result"]["ok"] is False
        assert any("must be a fraction in [0, 1]" in e for e in body["validation_result"]["errors"])

    def test_missing_duration_is_rejected(self, client, auth_headers):
        _asset, _sensor, scenario = _seed_and_create_scenario(
            client, auth_headers, scenario_type="missing_data_gap", magnitude=1.0, modification_duration_seconds=None,
        )
        resp = client.post(f"/api/v1/scenarios/{scenario['id']}/validate", headers=auth_headers)
        body = resp.json()
        assert body["validation_result"]["ok"] is False
        assert any("requires modification_duration_seconds" in e for e in body["validation_result"]["errors"])


class TestNonDestructiveGuarantee:
    def test_real_measurements_are_byte_identical_after_a_run(self, client, auth_headers, db_session):
        asset, sensor, scenario = _seed_and_create_scenario(client, auth_headers, scenario_type="drift", magnitude=15.0, modification_duration_seconds=3600)
        before = {
            (m.timestamp.isoformat(), round(m.value, 10))
            for m in db_session.query(SensorMeasurement).filter(SensorMeasurement.sensor_id == sensor["id"]).all()
        }

        client.post(f"/api/v1/scenarios/{scenario['id']}/validate", headers=auth_headers)
        run_resp = client.post(f"/api/v1/scenarios/{scenario['id']}/runs", headers=auth_headers, json={})
        assert run_resp.status_code == 201, run_resp.text
        assert run_resp.json()["status"] == "succeeded"

        after = {
            (m.timestamp.isoformat(), round(m.value, 10))
            for m in db_session.query(SensorMeasurement).filter(SensorMeasurement.sensor_id == sensor["id"]).all()
        }
        assert before == after
        assert len(after) > 0


class TestArchiveAndCompare:
    def test_archive_soft_deletes_and_preserves_runs(self, client, auth_headers):
        asset, sensor, scenario = _seed_and_create_scenario(client, auth_headers)
        client.post(f"/api/v1/scenarios/{scenario['id']}/validate", headers=auth_headers)
        run = client.post(f"/api/v1/scenarios/{scenario['id']}/runs", headers=auth_headers, json={}).json()

        archive_resp = client.delete(f"/api/v1/scenarios/{scenario['id']}", headers=auth_headers)
        assert archive_resp.status_code == 200
        assert archive_resp.json()["status"] == "archived"

        run_get = client.get(f"/api/v1/scenarios/runs/{run['id']}", headers=auth_headers)
        assert run_get.status_code == 200  # the run's history survives archival

        revalidate = client.post(f"/api/v1/scenarios/{scenario['id']}/validate", headers=auth_headers)
        assert revalidate.status_code == 409  # an archived scenario can never be validated/run again

    def test_compare_two_runs_side_by_side(self, client, auth_headers):
        asset, sensor, scenario_a = _seed_and_create_scenario(client, auth_headers, asset_code="PUMP-CMP-A", magnitude=3.0)
        client.post(f"/api/v1/scenarios/{scenario_a['id']}/validate", headers=auth_headers)
        run_a = client.post(f"/api/v1/scenarios/{scenario_a['id']}/runs", headers=auth_headers, json={}).json()

        _asset2, _sensor2, scenario_b = _seed_and_create_scenario(client, auth_headers, asset_code="PUMP-CMP-B", magnitude=9.0)
        client.post(f"/api/v1/scenarios/{scenario_b['id']}/validate", headers=auth_headers)
        run_b = client.post(f"/api/v1/scenarios/{scenario_b['id']}/runs", headers=auth_headers, json={}).json()

        compare_resp = client.post("/api/v1/scenarios/compare", headers=auth_headers, json={"scenario_run_ids": [run_a["id"], run_b["id"]]})
        assert compare_resp.status_code == 200, compare_resp.text
        data = compare_resp.json()["data"]
        assert len(data["runs"]) == 2
        assert "note" in data
        assert "combined" in data["note"].lower() or "no combined" in data["note"].lower()

    def test_compare_requires_at_least_two_runs(self, client, auth_headers):
        _asset, _sensor, scenario = _seed_and_create_scenario(client, auth_headers)
        client.post(f"/api/v1/scenarios/{scenario['id']}/validate", headers=auth_headers)
        run = client.post(f"/api/v1/scenarios/{scenario['id']}/runs", headers=auth_headers, json={}).json()
        resp = client.post("/api/v1/scenarios/compare", headers=auth_headers, json={"scenario_run_ids": [run["id"]]})
        assert resp.status_code == 422


class TestSensitivitySweep:
    def test_sweep_runs_the_pipeline_at_each_magnitude(self, client, auth_headers):
        _asset, _sensor, scenario = _seed_and_create_scenario(client, auth_headers, magnitude=1.0)
        client.post(f"/api/v1/scenarios/{scenario['id']}/validate", headers=auth_headers)
        resp = client.post(f"/api/v1/scenarios/{scenario['id']}/sensitivity", headers=auth_headers, json={"magnitudes": [1.0, 4.0, 8.0]})
        assert resp.status_code == 201, resp.text
        run = resp.json()
        assert run["run_kind"] == "sensitivity_sweep"
        assert run["status"] == "succeeded"
        assert run["results"]["sensitivity"]["magnitudes_tested"] == [1.0, 4.0, 8.0]

    def test_sweep_bounded_to_max_points(self, client, auth_headers):
        _asset, _sensor, scenario = _seed_and_create_scenario(client, auth_headers)
        client.post(f"/api/v1/scenarios/{scenario['id']}/validate", headers=auth_headers)
        resp = client.post(f"/api/v1/scenarios/{scenario['id']}/sensitivity", headers=auth_headers, json={"magnitudes": [float(i) for i in range(1, 12)]})
        assert resp.status_code == 422


class TestMultiSensorScenario:
    def test_two_sensors_run_independently(self, client, auth_headers):
        asset, sensor1 = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-MULTI", sensor_code="temp", unit="celsius")
        sensor2_resp = client.post(
            f"/api/v1/assets/{asset['id']}/sensors", headers=auth_headers,
            json={"code": "vib", "name": "Vibration", "measurement_type": "vibration", "unit": "mm/s", "expected_interval_seconds": 300},
        )
        assert sensor2_resp.status_code == 201
        sensor2 = sensor2_resp.json()

        _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor1["code"], n=400, value_fn=_stable_value)
        _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor2["code"], n=400, value_fn=lambda i: _stable_value(i, baseline=3.0, amplitude=0.5, seed=99, noise_std=0.05))

        baseline_start = BASE + timedelta(minutes=5 * 250)
        baseline_end = BASE + timedelta(minutes=5 * 399)
        mod_start = BASE + timedelta(minutes=5 * 300)

        payload = {
            "asset_id": asset["id"], "sensor_id": sensor1["id"], "sensor_scope": [sensor2["id"]],
            "name": "Multi-sensor scenario", "scenario_type": "multi_sensor",
            "baseline_window_start": _iso(baseline_start), "baseline_window_end": _iso(baseline_end),
            "parameters": {
                "per_sensor": [
                    {"sensor_id": sensor1["id"], "scenario_type": "level_shift", "modification_start": _iso(mod_start), "magnitude": 5.0, "unit": "celsius"},
                    {"sensor_id": sensor2["id"], "scenario_type": "temporary_spike", "modification_start": _iso(mod_start), "modification_duration_seconds": 1800, "magnitude": 1.0, "unit": "mm/s"},
                ]
            },
        }
        create_resp = client.post("/api/v1/scenarios", headers=auth_headers, json=payload)
        assert create_resp.status_code == 201, create_resp.text
        scenario = create_resp.json()
        assert set(scenario["sensor_scope"]) == {sensor1["id"], sensor2["id"]}

        validate_resp = client.post(f"/api/v1/scenarios/{scenario['id']}/validate", headers=auth_headers)
        assert validate_resp.json()["validation_result"]["ok"] is True

        run_resp = client.post(f"/api/v1/scenarios/{scenario['id']}/runs", headers=auth_headers, json={})
        assert run_resp.status_code == 201, run_resp.text
        run = run_resp.json()
        assert run["results"]["run_type"] == "multi_sensor"
        per_sensor = run["results"]["per_sensor"]
        assert set(per_sensor.keys()) == {sensor1["id"], sensor2["id"]}
        assert any("independently" in note for note in run["results"]["limitations"])

    def test_multi_sensor_rejects_single_sub_config(self, client, auth_headers):
        asset, sensor1 = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-MULTI2", sensor_code="temp")
        _ingest(client, auth_headers, asset_code=asset["code"], sensor_code=sensor1["code"], n=200, value_fn=_stable_value)
        payload = {
            "asset_id": asset["id"], "sensor_id": sensor1["id"], "name": "bad multi", "scenario_type": "multi_sensor",
            "baseline_window_start": _iso(BASE + timedelta(minutes=5*100)), "baseline_window_end": _iso(BASE + timedelta(minutes=5*199)),
            "parameters": {"per_sensor": [{"sensor_id": sensor1["id"], "scenario_type": "level_shift", "modification_start": _iso(BASE + timedelta(minutes=5*150)), "magnitude": 1.0, "unit": "celsius"}]},
        }
        create_resp = client.post("/api/v1/scenarios", headers=auth_headers, json=payload)
        scenario = create_resp.json()
        validate_resp = client.post(f"/api/v1/scenarios/{scenario['id']}/validate", headers=auth_headers)
        assert validate_resp.json()["validation_result"]["ok"] is False


class TestAuthzAndIsolation:
    def test_viewer_can_read_but_not_create(self, client, auth_headers, db_session, org_and_admin):
        org, _admin = org_and_admin
        _asset, _sensor, scenario = _seed_and_create_scenario(client, auth_headers)
        viewer_headers = _viewer_headers(db_session, org)

        get_resp = client.get(f"/api/v1/scenarios/{scenario['id']}", headers=viewer_headers)
        assert get_resp.status_code == 200

        create_resp = client.post(
            "/api/v1/scenarios", headers=viewer_headers,
            json={"asset_id": scenario["asset_id"], "sensor_id": scenario["sensor_id"], "name": "nope", "scenario_type": "level_shift", "baseline_window_start": _iso(BASE), "baseline_window_end": _iso(BASE + timedelta(days=1)), "parameters": {}},
        )
        assert create_resp.status_code == 403

        validate_resp = client.post(f"/api/v1/scenarios/{scenario['id']}/validate", headers=viewer_headers)
        assert validate_resp.status_code == 403

    def test_scenario_from_another_org_is_not_found(self, client, auth_headers, db_session):
        _asset, _sensor, scenario = _seed_and_create_scenario(client, auth_headers)

        other_org, other_admin = auth_service_module.register_organization(
            db_session, organization_name="Other Scenario Org", admin_email="other-admin@scenario-tests.example",
            admin_password="OtherPass123!", admin_full_name="Other Admin",
        )
        other_headers = {"Authorization": f"Bearer {auth_service_module.issue_token(other_admin)}"}

        resp = client.get(f"/api/v1/scenarios/{scenario['id']}", headers=other_headers)
        assert resp.status_code == 404

    def test_unauthenticated_request_is_rejected(self, client):
        resp = client.get("/api/v1/scenarios")
        assert resp.status_code in (401, 403)
