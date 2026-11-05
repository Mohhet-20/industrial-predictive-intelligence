"""Investigation and copilot integration tests for Scenario Simulation
. Confirms a scenario run can be attached to an
investigation as evidence WITHOUT altering the investigation's real
evidence, and that the copilot always labels a scenario result as
SIMULATED/HYPOTHETICAL — both when it's reached through an investigation's
evidence links and when it's asked about directly.
"""
from datetime import datetime, timedelta, timezone

from tests.test_api_scenario import _iso, _ingest, _make_asset_and_sensor, _seed_and_create_scenario

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _run_a_scenario(client, auth_headers, **kwargs):
    asset, sensor, scenario = _seed_and_create_scenario(client, auth_headers, **kwargs)
    client.post(f"/api/v1/scenarios/{scenario['id']}/validate", headers=auth_headers)
    run_resp = client.post(f"/api/v1/scenarios/{scenario['id']}/runs", headers=auth_headers, json={})
    assert run_resp.status_code == 201, run_resp.text
    return asset, sensor, scenario, run_resp.json()


class TestInvestigationEvidenceLink:
    def test_scenario_run_can_be_attached_to_an_investigation(self, client, auth_headers):
        asset, sensor, scenario, run = _run_a_scenario(client, auth_headers, asset_code="PUMP-INV-SCN")

        inv_resp = client.post("/api/v1/investigations", headers=auth_headers, json={"asset_id": asset["id"], "title": "Investigating a hypothetical", "primary_sensor_id": sensor["id"]})
        assert inv_resp.status_code == 201, inv_resp.text
        investigation = inv_resp.json()

        link_resp = client.post(
            f"/api/v1/investigations/{investigation['id']}/evidence", headers=auth_headers,
            json={"evidence_type": "scenario_result", "evidence_id": run["id"], "relation_note": "what-if for comparison"},
        )
        assert link_resp.status_code == 201, link_resp.text
        link = link_resp.json()
        assert link["evidence_type"] == "scenario_result"
        assert link["evidence_id"] == run["id"]

        list_resp = client.get(f"/api/v1/investigations/{investigation['id']}/evidence", headers=auth_headers)
        assert list_resp.status_code == 200
        assert any(e["evidence_id"] == run["id"] for e in list_resp.json())

    def test_attaching_scenario_evidence_never_touches_the_scenario_run(self, client, auth_headers):
        asset, sensor, scenario, run = _run_a_scenario(client, auth_headers, asset_code="PUMP-INV-SCN2")
        inv_resp = client.post("/api/v1/investigations", headers=auth_headers, json={"asset_id": asset["id"], "title": "Non-destructive link check"})
        investigation = inv_resp.json()
        client.post(f"/api/v1/investigations/{investigation['id']}/evidence", headers=auth_headers, json={"evidence_type": "scenario_result", "evidence_id": run["id"]})

        run_after = client.get(f"/api/v1/scenarios/runs/{run['id']}", headers=auth_headers).json()
        assert run_after["results"] == run["results"]
        assert run_after["status"] == run["status"]

    def test_scenario_result_requires_a_real_evidence_id(self, client, auth_headers):
        asset, sensor, scenario, run = _run_a_scenario(client, auth_headers, asset_code="PUMP-INV-SCN3")
        inv_resp = client.post("/api/v1/investigations", headers=auth_headers, json={"asset_id": asset["id"], "title": "Bad link"})
        investigation = inv_resp.json()
        resp = client.post(f"/api/v1/investigations/{investigation['id']}/evidence", headers=auth_headers, json={"evidence_type": "scenario_result", "evidence_id": "not-a-real-run-id"})
        assert resp.status_code == 404


class TestCopilotScenarioGrounding:
    def test_copilot_direct_scenario_context_labels_the_result_simulated(self, client, auth_headers):
        asset, sensor, scenario, run = _run_a_scenario(client, auth_headers, asset_code="PUMP-COP-SCN")

        conv_resp = client.post("/api/v1/copilot/conversations", headers=auth_headers, json={"context_type": "scenario", "context_id": scenario["id"]})
        assert conv_resp.status_code == 201, conv_resp.text
        conversation = conv_resp.json()

        msg_resp = client.post(
            f"/api/v1/copilot/conversations/{conversation['id']}/messages", headers=auth_headers,
            json={"question": "What did this scenario show?"},
        )
        assert msg_resp.status_code == 201, msg_resp.text
        message = msg_resp.json()
        grounding_text = " ".join(item.get("text", "") for item in (message.get("grounding") or []))
        assert "SIMULATED" in grounding_text or "simulated" in grounding_text.lower()
        assert "hypothetical" in grounding_text.lower() or "HYPOTHETICAL" in grounding_text

    def test_copilot_scenario_with_no_runs_does_not_invent_a_result(self, client, auth_headers):
        asset, sensor, scenario = _seed_and_create_scenario(client, auth_headers, asset_code="PUMP-COP-NORUN")
        # deliberately never validated/run

        conv_resp = client.post("/api/v1/copilot/conversations", headers=auth_headers, json={"context_type": "scenario", "context_id": scenario["id"]})
        conversation = conv_resp.json()
        msg_resp = client.post(f"/api/v1/copilot/conversations/{conversation['id']}/messages", headers=auth_headers, json={"question": "What happened in this scenario?"})
        assert msg_resp.status_code == 201
        grounding_text = " ".join(item.get("text", "") for item in (msg_resp.json().get("grounding") or []))
        assert "has not been run yet" in grounding_text

    def test_copilot_investigation_context_labels_linked_scenario_as_simulated(self, client, auth_headers):
        asset, sensor, scenario, run = _run_a_scenario(client, auth_headers, asset_code="PUMP-COP-INV-SCN")
        inv_resp = client.post("/api/v1/investigations", headers=auth_headers, json={"asset_id": asset["id"], "title": "Investigation with scenario evidence"})
        investigation = inv_resp.json()
        client.post(f"/api/v1/investigations/{investigation['id']}/evidence", headers=auth_headers, json={"evidence_type": "scenario_result", "evidence_id": run["id"]})

        conv_resp = client.post("/api/v1/copilot/conversations", headers=auth_headers, json={"context_type": "investigation", "context_id": investigation["id"]})
        conversation = conv_resp.json()
        msg_resp = client.post(f"/api/v1/copilot/conversations/{conversation['id']}/messages", headers=auth_headers, json={"question": "Summarize the evidence on this investigation."})
        assert msg_resp.status_code == 201
        grounding_text = " ".join(item.get("text", "") for item in (msg_resp.json().get("grounding") or []))
        assert "SIMULATED" in grounding_text
        assert "not a real observation" in grounding_text

    def test_evidence_selection_kind_scenario_resolves(self, client, auth_headers):
        asset, sensor, scenario, run = _run_a_scenario(client, auth_headers, asset_code="PUMP-COP-SEL")
        conv_resp = client.post("/api/v1/copilot/conversations", headers=auth_headers, json={"context_type": "general"})
        conversation = conv_resp.json()
        msg_resp = client.post(
            f"/api/v1/copilot/conversations/{conversation['id']}/messages", headers=auth_headers,
            json={"question": "Explain this scenario.", "evidence_selection": [{"kind": "scenario", "id": scenario["id"]}]},
        )
        assert msg_resp.status_code == 201, msg_resp.text
        grounding = msg_resp.json().get("grounding") or []
        assert any(item.get("source_type") in ("scenario", "scenario_run") for item in grounding)
