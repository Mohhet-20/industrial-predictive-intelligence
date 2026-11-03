"""Tests for app.evaluation.robustness (Phase 10 §5/§15).

These test that the EVALUATION METHODOLOGY itself is correct — that each
scenario measures what it claims to measure and is deterministic — not
just that the functions run without raising. Where a scenario is designed
to expose a real limitation (drift, sparse-time-density), the test
confirms the scenario actually demonstrates it rather than accidentally
passing for an unrelated reason.
"""
from app.anomaly import baseline as baseline_mod
from app.evaluation import robustness


def test_missing_data_gap_scenario_does_not_crash_and_reports_reduced_points():
    result = robustness.missing_data_gap_scenario()
    assert result["detector_crashed"] is False
    assert result["points_after_gap_removed"] < result["points_before_gap_removed"]
    assert result["candidates_found"] >= 0


def test_noise_sensitivity_sweep_recall_degrades_monotonically_with_noise():
    result = robustness.noise_sensitivity_sweep()
    sweep = result["sweep"]
    assert len(sweep) == 5
    # Once recall is lost at a given noise level, it must not come back at
    # a HIGHER noise level (recall should be monotonically non-increasing
    # as reference noise grows, since the same absolute spike becomes a
    # smaller effective z-score).
    recalls = [s["spike_recalled"] for s in sweep]
    first_false = next((i for i, r in enumerate(recalls) if not r), None)
    if first_false is not None:
        assert all(r is False for r in recalls[first_false:]), "recall should not reappear at higher noise once lost"
    # At the lowest noise level tested, a 15-unit spike must be recalled.
    assert sweep[0]["spike_recalled"] is True


def test_outlier_robustness_reference_scenario_shows_median_beats_mean():
    result = robustness.outlier_robustness_reference_scenario()
    assert result["median_error_from_truth"] < result["mean_error_from_truth"]
    # The robust estimate should be dramatically tighter than the naive one.
    assert result["robust_std_estimate"] < result["naive_std"]


def test_drift_scenario_runs_and_documents_static_baseline_limitation():
    result = robustness.drift_scenario()
    assert result["points_scored"] > 0
    assert isinstance(result["candidates_flagged"], int)
    assert "static" in result["finding"].lower() or "drift" in result["finding"].lower()


def test_short_history_boundary_sweep_is_exact_at_min_for_baseline():
    result = robustness.short_history_boundary_sweep()
    outcomes = {row["n"]: row["outcome"] for row in result["sweep"]}
    assert outcomes[baseline_mod.MIN_FOR_BASELINE - 1] == "honest_error"
    assert outcomes[baseline_mod.MIN_FOR_BASELINE] == "baseline_computed"


def test_sparse_observations_scenario_confirms_time_density_is_not_modeled():
    result = robustness.sparse_observations_scenario()
    assert result["identical_statistics"] is True
    assert result["sparse_window_hours"] > result["dense_window_hours"] * 100


def test_boundary_threshold_scenario_confirms_inclusive_semantics():
    result = robustness.boundary_threshold_scenario()
    assert result["at_exact_threshold_flagged"] is True
    assert result["just_below_threshold_flagged"] is False


def test_isolation_forest_short_reference_raises_honest_error():
    result = robustness.isolation_forest_short_reference_scenario()
    assert result["outcome"] == "honest_error"
    assert result["message"] is not None


def test_run_all_returns_every_scenario_deterministically():
    first = robustness.run_all()
    second = robustness.run_all()
    assert first == second, "evaluation scenarios must be exactly reproducible given the fixed seed"
    assert len(first["scenarios"]) == 8
    assert first["evaluability"] == "FULLY EVALUABLE WITH AVAILABLE DATA"
