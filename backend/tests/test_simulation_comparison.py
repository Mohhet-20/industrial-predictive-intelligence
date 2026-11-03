"""Unit tests for app.simulation.comparison (Phase 11 §5, §8, §9) — pure
diffing over already-computed pipeline bundles. Confirms no combined score
is ever fabricated and that a "not computed on one side" bundle degrades
to an honest, explained non-comparison rather than a crash or a fake 0."""
from app.simulation import comparison as c


def _value_summary(mean):
    return {"mean": mean, "median": mean, "std": 1.0, "min": mean - 2, "max": mean + 2}


class TestCompareValueSummary:
    def test_computes_absolute_and_relative_diffs(self):
        diff = c.compare_value_summary(_value_summary(50.0), _value_summary(55.0))
        assert diff["mean"]["absolute_diff"] == 5.0
        assert abs(diff["mean"]["relative_diff_pct"] - 10.0) < 1e-9

    def test_handles_zero_baseline_without_crashing(self):
        diff = c.compare_value_summary(_value_summary(0.0), _value_summary(5.0))
        assert diff["mean"]["relative_diff_pct"] is None  # division by zero avoided, not fabricated as inf/0


class TestCompareAnomalyDetection:
    def test_reports_counts_on_both_sides(self):
        baseline = {"computed": True, "candidates": [], "points_flagged": 0}
        simulated = {"computed": True, "candidates": [{"a": 1}, {"a": 2}], "points_flagged": 5}
        diff = c.compare_anomaly_detection(baseline, simulated)
        assert diff["computed"] is True
        assert diff["baseline_candidate_count"] == 0
        assert diff["simulated_candidate_count"] == 2
        assert diff["candidate_count_delta"] == 2

    def test_not_computed_on_either_side_degrades_honestly(self):
        diff = c.compare_anomaly_detection({"computed": False, "reason": "no reference"}, {"computed": True, "candidates": []})
        assert diff["computed"] is False
        assert "reason" in diff


class TestCompareCondition:
    def test_detects_a_stage_change(self):
        baseline = {"computed": True, "degradation_classification": {"stage": "observed_trend"}, "evidence": {"trend_evidence": {"direction": "increasing"}, "deviation_from_baseline": {"deviation_mad_units": 0.5}}}
        simulated = {"computed": True, "degradation_classification": {"stage": "degradation_candidate"}, "evidence": {"trend_evidence": {"direction": "increasing"}, "deviation_from_baseline": {"deviation_mad_units": 3.0}}}
        diff = c.compare_condition_intelligence(baseline, simulated)
        assert diff["stage_changed"] is True
        assert diff["baseline_stage"] == "observed_trend"
        assert diff["simulated_stage"] == "degradation_candidate"

    def test_no_stage_change_reported_correctly(self):
        bundle = {"computed": True, "degradation_classification": {"stage": "observed_trend"}, "evidence": {"trend_evidence": {"direction": "flat"}, "deviation_from_baseline": {"deviation_mad_units": 0.1}}}
        diff = c.compare_condition_intelligence(bundle, bundle)
        assert diff["stage_changed"] is False


class TestNoCompositeScore:
    def test_compare_bundles_never_produces_a_single_score_field(self):
        bundle = {
            "value_summary": _value_summary(50.0),
            "anomaly_detection": {"computed": True, "candidates": [], "points_flagged": 0},
            "condition_intelligence": {"computed": True, "degradation_classification": None, "evidence": {"trend_evidence": {"direction": None}, "deviation_from_baseline": {"deviation_mad_units": None}}},
            "change_candidates": {"candidates": []},
            "forecast_backtest": {"computed": False, "reason": "n/a"},
            "observation_count": 100,
        }
        result = c.compare_bundles(bundle, bundle)
        flat_keys = set()

        def _walk(d, prefix=""):
            for k, v in d.items():
                flat_keys.add(f"{prefix}{k}")
                if isinstance(v, dict):
                    _walk(v, f"{prefix}{k}.")

        _walk(result)
        forbidden = {"risk_score", "combined_score", "overall_score", "health_score", "composite_score"}
        assert not (flat_keys & forbidden)


class TestSensitivitySweep:
    def test_sorts_points_by_magnitude_and_never_fits_a_curve(self):
        points = [
            {"magnitude": 5.0, "comparison": {"anomaly_detection_diff": {"simulated_candidate_count": 1}}},
            {"magnitude": 1.0, "comparison": {"anomaly_detection_diff": {"simulated_candidate_count": 0}}},
            {"magnitude": 3.0, "comparison": {"anomaly_detection_diff": {"simulated_candidate_count": 1}}},
        ]
        summary = c.summarize_sensitivity_sweep(points)
        assert summary["magnitudes_tested"] == [1.0, 3.0, 5.0]
        assert "fit" not in summary["note"].lower() or "no trend line is fit" in summary["note"].lower()
        assert "slope" not in summary
        assert "fitted_curve" not in summary
