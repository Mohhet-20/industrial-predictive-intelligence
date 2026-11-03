"""Unit tests for app.feedback.error_analysis — late detections, duplicate
candidates, and false-negative candidates. No database."""
import datetime as dt

from app.feedback.error_analysis import (
    DUPLICATE_WINDOW_SECONDS,
    LATE_DETECTION_THRESHOLD_SECONDS,
    build_error_analysis_report,
    find_duplicate_candidates,
    find_false_negative_candidates,
    find_late_detections,
)
from app.feedback.metrics import ClassifiedLink
from app.models.outcome import SignalAssessment, VerificationStatus

BASE = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def _tp_link(link_id, delay_seconds):
    return ClassifiedLink(
        link_id=link_id, signal_id=f"signal-{link_id}", within_horizon=True,
        verification_status=VerificationStatus.VERIFIED, signal_assessment=SignalAssessment.CONFIRMS,
        detection_delay_seconds=delay_seconds,
    )


class TestLateDetections:
    def test_delay_under_threshold_is_not_late(self):
        links = [_tp_link("a", 3600.0)]
        assert find_late_detections(links) == []

    def test_delay_over_threshold_is_late(self):
        links = [_tp_link("a", LATE_DETECTION_THRESHOLD_SECONDS + 1)]
        result = find_late_detections(links)
        assert len(result) == 1
        assert result[0].link_id == "a"

    def test_delay_exactly_at_threshold_is_not_late(self):
        links = [_tp_link("a", LATE_DETECTION_THRESHOLD_SECONDS)]
        assert find_late_detections(links) == []

    def test_none_delay_is_skipped(self):
        link = ClassifiedLink(link_id="a", signal_id="s", within_horizon=True, verification_status=VerificationStatus.VERIFIED, signal_assessment=SignalAssessment.CONFIRMS, detection_delay_seconds=None)
        assert find_late_detections([link]) == []


class TestDuplicateCandidates:
    def test_two_signals_within_window_are_duplicate_candidates(self):
        signals = [
            ("sig-1", "sensor-1", "anomaly", BASE),
            ("sig-2", "sensor-1", "anomaly", BASE + dt.timedelta(minutes=10)),
        ]
        result = find_duplicate_candidates(signals, window_seconds=DUPLICATE_WINDOW_SECONDS)
        assert len(result) == 1
        assert sorted(result[0].signal_ids) == ["sig-1", "sig-2"]

    def test_signals_far_apart_are_not_duplicates(self):
        signals = [
            ("sig-1", "sensor-1", "anomaly", BASE),
            ("sig-2", "sensor-1", "anomaly", BASE + dt.timedelta(hours=5)),
        ]
        assert find_duplicate_candidates(signals, window_seconds=DUPLICATE_WINDOW_SECONDS) == []

    def test_different_sensors_are_never_grouped_together(self):
        signals = [
            ("sig-1", "sensor-1", "anomaly", BASE),
            ("sig-2", "sensor-2", "anomaly", BASE + dt.timedelta(minutes=1)),
        ]
        assert find_duplicate_candidates(signals) == []

    def test_different_signal_types_on_same_sensor_are_not_grouped(self):
        signals = [
            ("sig-1", "sensor-1", "anomaly", BASE),
            ("sig-2", "sensor-1", "degradation_signal", BASE + dt.timedelta(minutes=1)),
        ]
        assert find_duplicate_candidates(signals) == []

    def test_single_signal_is_never_a_duplicate_candidate(self):
        signals = [("sig-1", "sensor-1", "anomaly", BASE)]
        assert find_duplicate_candidates(signals) == []

    def test_three_signals_in_a_chain_cluster_together(self):
        signals = [
            ("sig-1", "sensor-1", "anomaly", BASE),
            ("sig-2", "sensor-1", "anomaly", BASE + dt.timedelta(minutes=30)),
            ("sig-3", "sensor-1", "anomaly", BASE + dt.timedelta(minutes=60)),
        ]
        result = find_duplicate_candidates(signals, window_seconds=3600.0)
        assert len(result) == 1
        assert len(result[0].signal_ids) == 3


class TestFalseNegativeCandidates:
    def test_unlinked_fault_like_outcome_is_a_candidate(self):
        result = find_false_negative_candidates([("outcome-1", "sensor-1")], linked_outcome_ids=set())
        assert len(result) == 1
        assert result[0].outcome_id == "outcome-1"

    def test_linked_fault_like_outcome_is_not_a_candidate(self):
        result = find_false_negative_candidates([("outcome-1", "sensor-1")], linked_outcome_ids={"outcome-1"})
        assert result == []

    def test_no_fault_like_outcomes_gives_no_candidates(self):
        assert find_false_negative_candidates([], linked_outcome_ids=set()) == []


class TestBuildErrorAnalysisReport:
    def test_report_aggregates_all_three_finding_types(self):
        true_positive_links = [_tp_link("late-1", LATE_DETECTION_THRESHOLD_SECONDS + 10)]
        signals_for_duplicate_check = [
            ("sig-a", "sensor-1", "anomaly", BASE),
            ("sig-b", "sensor-1", "anomaly", BASE + dt.timedelta(minutes=5)),
        ]
        fault_like_outcomes = [("outcome-missed", "sensor-2")]
        linked_outcome_ids: set[str] = set()

        report = build_error_analysis_report(
            true_positive_links=true_positive_links,
            signals_for_duplicate_check=signals_for_duplicate_check,
            fault_like_outcomes=fault_like_outcomes,
            linked_outcome_ids=linked_outcome_ids,
        )
        payload = report.to_dict()
        assert payload["late_detection_count"] == 1
        assert payload["duplicate_candidate_count"] == 1
        assert payload["false_negative_candidate_count"] == 1

    def test_empty_inputs_produce_empty_report(self):
        report = build_error_analysis_report(
            true_positive_links=[], signals_for_duplicate_check=[], fault_like_outcomes=[], linked_outcome_ids=set()
        )
        payload = report.to_dict()
        assert payload["late_detection_count"] == 0
        assert payload["duplicate_candidate_count"] == 0
        assert payload["false_negative_candidate_count"] == 0
