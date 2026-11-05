"""Tests for app.evaluation.copilot_eval.

Runs the representative groundedness case battery end-to-end against the
real copilot API/service/orchestrator path and asserts each case's own
PASS/FAIL verdict, plus the aggregate run_all() scorecard.
"""
from app.evaluation import copilot_eval


def test_correct_evidence_retrieval(client, auth_headers):
    assert copilot_eval.case_correct_evidence_retrieval(client, auth_headers)["outcome"] == "PASS"


def test_correct_source_attribution(client, auth_headers):
    assert copilot_eval.case_correct_source_attribution(client, auth_headers)["outcome"] == "PASS"


def test_unsupported_question_handling(client, auth_headers):
    assert copilot_eval.case_unsupported_question_handling(client, auth_headers)["outcome"] == "PASS"


def test_insufficient_evidence_handling(client, auth_headers):
    assert copilot_eval.case_insufficient_evidence_handling(client, auth_headers)["outcome"] == "PASS"


def test_anomaly_vs_failure_distinction(client, auth_headers):
    assert copilot_eval.case_anomaly_vs_failure_distinction(client, auth_headers)["outcome"] == "PASS"


def test_synthetic_labeling(client, auth_headers, db_session, org_and_admin):
    org, _admin = org_and_admin
    asset_resp = client.post("/api/v1/assets", headers=auth_headers, json={"code": "PUMP-SYNTHLABEL", "name": "Synth Label Pump", "asset_type": "pump"})
    assert asset_resp.status_code == 201
    sensor_resp = client.post(f"/api/v1/assets/{asset_resp.json()['id']}/sensors", headers=auth_headers, json={"code": "temp", "name": "Temp", "measurement_type": "temperature", "unit": "celsius", "expected_interval_seconds": 300})
    assert sensor_resp.status_code == 201
    result = copilot_eval.case_synthetic_labeling(client, auth_headers, db_session, org.id, asset_resp.json()["id"], sensor_resp.json()["id"])
    assert result["outcome"] == "PASS", result["finding"]


def test_authz_boundary(client, auth_headers, db_session):
    assert copilot_eval.case_authz_boundary(client, auth_headers, db_session)["outcome"] == "PASS"


def test_provider_unavailable_behavior(client, auth_headers):
    assert copilot_eval.case_provider_unavailable_behavior(client, auth_headers)["outcome"] == "PASS"


def test_adversarial_question(client, auth_headers):
    assert copilot_eval.case_adversarial_question(client, auth_headers)["outcome"] == "PASS"


def test_run_all_scorecard(client, auth_headers, db_session, org_and_admin):
    org, _admin = org_and_admin
    result = copilot_eval.run_all(client, auth_headers, db_session, organization_id=org.id)
    assert len(result["cases"]) == 9
    failing = [c for c in result["cases"] if c["outcome"] != "PASS"]
    assert failing == [], f"Copilot groundedness cases failed: {failing}"
