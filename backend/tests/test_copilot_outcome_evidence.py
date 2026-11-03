"""Tests that the copilot's evidence-retrieval layer (Phase 9) correctly
surfaces Phase 12 closed-loop outcome evidence — grounded in real
reported outcomes and their verification/link state, never upgrading an
unverified report to confirmed ground truth, and always labeling a
scenario-linked outcome as simulated.
"""
from datetime import timedelta

import numpy as np

from app.copilot import tools as copilot_tools
from tests.test_api_outcome import BASE, _ingest, _iso, _make_asset_and_sensor, _make_verified_anomaly, _parse_iso_utc


class TestOutcomeGroundingInAnomalyContext:
    def test_anomaly_evidence_includes_unverified_outcome_link_with_explicit_caveat(self, client, auth_headers, db_session, org_and_admin):
        org, _ = org_and_admin
        asset, sensor, detector, anomaly = _make_verified_anomaly(client, auth_headers, asset_code="PUMP-CPOUT1")
        anomaly_start = _parse_iso_utc(anomaly["period_start"])

        outcome = client.post(
            "/api/v1/outcomes", headers=auth_headers,
            json={"asset_id": asset["id"], "sensor_id": sensor["id"], "outcome_type": "failure_event", "description": "Reported but not yet reviewed.", "outcome_timestamp": _iso(anomaly_start + timedelta(hours=3))},
        ).json()
        client.post(
            f"/api/v1/outcomes/{outcome['id']}/links", headers=auth_headers,
            json={"signal_type": "anomaly", "signal_id": anomaly["id"], "horizon_seconds": 86400.0},
        )

        evidence = copilot_tools.get_anomaly_evidence(db_session, organization_id=org.id, anomaly_id=anomaly["id"])
        outcome_items = [e for e in evidence if e.source_type == "outcome_signal_link"]
        assert len(outcome_items) == 1
        assert "NOT yet" in outcome_items[0].text or "not yet" in outcome_items[0].text.lower()
        assert "unverified" in outcome_items[0].text.lower()

    def test_anomaly_evidence_reflects_verified_outcome_without_the_unverified_caveat(self, client, auth_headers, db_session, org_and_admin):
        org, _ = org_and_admin
        asset, sensor, detector, anomaly = _make_verified_anomaly(client, auth_headers, asset_code="PUMP-CPOUT2")
        anomaly_start = _parse_iso_utc(anomaly["period_start"])

        outcome = client.post(
            "/api/v1/outcomes", headers=auth_headers,
            json={"asset_id": asset["id"], "sensor_id": sensor["id"], "outcome_type": "failure_event", "description": "Confirmed via inspection.", "outcome_timestamp": _iso(anomaly_start + timedelta(hours=3))},
        ).json()
        client.post(
            f"/api/v1/outcomes/{outcome['id']}/links", headers=auth_headers,
            json={"signal_type": "anomaly", "signal_id": anomaly["id"], "horizon_seconds": 86400.0},
        )
        client.post(f"/api/v1/outcomes/{outcome['id']}/verify", headers=auth_headers, json={"to_status": "verified", "evidence_note": "Inspected and confirmed."})

        evidence = copilot_tools.get_anomaly_evidence(db_session, organization_id=org.id, anomaly_id=anomaly["id"])
        outcome_items = [e for e in evidence if e.source_type == "outcome_signal_link"]
        assert len(outcome_items) == 1
        assert "verified" in outcome_items[0].text.lower()
        assert "not yet" not in outcome_items[0].text.lower()

    def test_outcome_evidence_includes_verification_history_and_links(self, client, auth_headers, db_session, org_and_admin):
        org, _ = org_and_admin
        asset, sensor, detector, anomaly = _make_verified_anomaly(client, auth_headers, asset_code="PUMP-CPOUT3")
        anomaly_start = _parse_iso_utc(anomaly["period_start"])

        outcome = client.post(
            "/api/v1/outcomes", headers=auth_headers,
            json={"asset_id": asset["id"], "sensor_id": sensor["id"], "outcome_type": "failure_event", "description": "x", "outcome_timestamp": _iso(anomaly_start + timedelta(hours=3))},
        ).json()
        link_resp = client.post(
            f"/api/v1/outcomes/{outcome['id']}/links", headers=auth_headers,
            json={"signal_type": "anomaly", "signal_id": anomaly["id"], "horizon_seconds": 86400.0},
        )
        client.post(f"/api/v1/outcomes/{outcome['id']}/verify", headers=auth_headers, json={"to_status": "verified", "evidence_note": "Confirmed."})

        evidence = copilot_tools.get_outcome_evidence(db_session, organization_id=org.id, outcome_id=outcome["id"])
        kinds = {e.source_type for e in evidence}
        assert "outcome" in kinds
        assert "outcome_verification" in kinds
        assert "outcome_signal_link" in kinds

        verification_item = next(e for e in evidence if e.source_type == "outcome_verification")
        assert "Confirmed." in verification_item.text


class TestScenarioLinkedOutcomeIsLabeledSimulated:
    def test_scenario_run_linked_outcome_is_labeled_simulated_in_evidence(self, client, auth_headers, sample_asset_and_sensor, db_session, org_and_admin):
        org, _ = org_and_admin
        asset, sensor = sample_asset_and_sensor
        n = 300
        rng = np.random.default_rng(5)
        values = [50.0 + float(rng.normal(0, 0.5)) for _ in range(n)]
        _ingest(client, auth_headers, asset_code=asset.code, sensor_code=sensor.code, n=n, value_fn=lambda i: values[i])

        scenario = client.post(
            "/api/v1/scenarios", headers=auth_headers,
            json={
                "asset_id": asset.id, "sensor_id": sensor.id, "name": "copilot-separation-test", "scenario_type": "level_shift",
                "baseline_window_start": _iso(BASE), "baseline_window_end": _iso(BASE + timedelta(minutes=5 * (n - 1))),
                "modification_start": _iso(BASE + timedelta(minutes=5 * 100)), "modification_duration_seconds": 3600.0 * 10,
                "magnitude": 1.0, "unit": "celsius",
            },
        ).json()
        client.post(f"/api/v1/scenarios/{scenario['id']}/validate", headers=auth_headers)
        scenario_run = client.post(f"/api/v1/scenarios/{scenario['id']}/runs", headers=auth_headers, json={}).json()

        outcome = client.post(
            "/api/v1/outcomes", headers=auth_headers,
            json={"asset_id": asset.id, "outcome_type": "other", "description": "Comparing simulated result.", "outcome_timestamp": _iso(BASE + timedelta(minutes=5 * 150))},
        ).json()
        client.post(
            f"/api/v1/outcomes/{outcome['id']}/links", headers=auth_headers,
            json={"signal_type": "scenario_run", "signal_id": scenario_run["id"], "horizon_seconds": 86400.0},
        )

        evidence = copilot_tools.get_outcome_evidence(db_session, organization_id=org.id, outcome_id=outcome["id"])
        link_items = [e for e in evidence if e.source_type == "outcome_signal_link"]
        assert len(link_items) == 1
        assert "scenario_run" in link_items[0].text
