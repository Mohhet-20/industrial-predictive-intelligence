"""Tests for app.evaluation.prioritization.

Basic prioritization correctness (contributing factors exposed,
cross-detector agreement raises score, org scoping, auth) is already
covered by tests/test_api_orchestration.py. These tests assert on the
SUBSTANCE of each edge case's outcome, not just that the
functions run without raising.
"""
from app.evaluation import prioritization


def test_reproducibility_check_is_actually_reproducible(db_session):
    result = prioritization.reproducibility_check(db_session)
    assert result["outcome"] == "reproducible"
    assert result["item_count"] >= 1


def test_strong_anomaly_poor_data_quality_is_surfaced_not_scored(db_session):
    result = prioritization.edge_case_strong_anomaly_poor_data_quality(db_session)
    assert result["outcome"] == "context_only_confirmed"
    assert result["nearby_data_quality_issue_count"] >= 3


def test_multiple_weak_signals_edge_case_reports_a_real_comparison(db_session):
    result = prioritization.edge_case_multiple_weak_signals(db_session)
    assert result["outcome"] in ("weak_signals_outrank_strong", "strong_signal_still_ranks_higher")
    assert result["weak_signal_score"] > 0
    assert result["strong_signal_score"] > 0


def test_conflicting_detectors_both_receive_agreement_bonus(db_session):
    result = prioritization.edge_case_conflicting_detectors(db_session)
    assert result["outcome"] == "agreement_bonus_applied_correctly"
    assert result["robust_zscore_score"] >= 100 + 25
    assert result["isolation_forest_score"] >= 100 + 25


def test_no_available_signals_returns_empty_not_an_error(db_session):
    result = prioritization.edge_case_no_available_signals(db_session)
    assert result["outcome"] == "empty_result_handled_safely"


def test_stale_evidence_is_not_penalized_or_boosted(db_session):
    result = prioritization.edge_case_stale_evidence(db_session)
    assert result["outcome"] == "age_not_scored_confirmed"
    assert result["stale_score"] == result["fresh_score"]


def test_run_all_covers_every_check(db_session):
    result = prioritization.run_all(db_session)
    assert len(result["checks"]) == 6
    assert result["evaluability"] == "FULLY EVALUABLE WITH AVAILABLE DATA"
    outcomes = {c["check"]: c["outcome"] for c in result["checks"]}
    assert outcomes["reproducibility_check"] == "reproducible"
    assert outcomes["no_available_signals"] == "empty_result_handled_safely"
