"""Tests for app.evaluation.adversarial (Phase 10 §10/§15).

Each test asserts the SUBSTANCE of its scenario's safety/provenance
property, not just that the function ran without raising.
"""
from app.evaluation import adversarial


def test_incompatible_model_artifact_fails_safely(client, auth_headers, db_session):
    result = adversarial.incompatible_model_artifact(client, auth_headers, db_session)
    assert result["outcome"] == "safe_failure", result["finding"]


def test_malformed_timestamp_is_quarantined_per_record(client, auth_headers):
    result = adversarial.malformed_timestamp_is_quarantined_per_record(client, auth_headers)
    assert result["outcome"] == "safe_and_documented", result["finding"]


def test_extreme_value_scoring_does_not_crash(client, auth_headers):
    result = adversarial.extreme_value_scoring_does_not_crash(client, auth_headers)
    assert result["outcome"] == "safe", result["finding"]


def test_annotation_vs_model_conflict_preserves_both(client, auth_headers):
    result = adversarial.annotation_vs_model_conflict_preserves_both(client, auth_headers)
    assert result["outcome"] == "provenance_preserved", result["finding"]


def test_disagreeing_detectors_no_forced_consensus(client, auth_headers):
    result = adversarial.disagreeing_detectors_no_forced_consensus(client, auth_headers)
    assert result["outcome"] == "disagreement_preserved", result["finding"]


def test_run_all_covers_the_five_new_checks(client, auth_headers, db_session):
    result = adversarial.run_all(client, auth_headers, db_session)
    assert len(result["checks"]) == 5
    failing = [c for c in result["checks"] if c["outcome"] not in ("safe_failure", "safe_and_documented", "safe", "provenance_preserved", "disagreement_preserved")]
    assert failing == [], f"Adversarial checks failed: {failing}"
