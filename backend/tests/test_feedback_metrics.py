"""Unit tests for app.feedback.metrics — classification and precision-only
metrics, gated on a minimum verified-evidence sample size. No database."""
from app.feedback.metrics import (
    ClassifiedLink,
    classify,
    compute_metrics,
    coverage_classification,
)
from app.models.outcome import SignalAssessment, VerificationStatus


def _link(link_id, *, within_horizon=True, verification_status=VerificationStatus.VERIFIED, signal_assessment=SignalAssessment.CONFIRMS, delay=100.0):
    return ClassifiedLink(
        link_id=link_id, signal_id=f"signal-{link_id}", within_horizon=within_horizon,
        verification_status=verification_status, signal_assessment=signal_assessment, detection_delay_seconds=delay,
    )


class TestClassify:
    def test_verified_confirms_within_horizon_is_true_positive(self):
        link = _link("a", within_horizon=True, verification_status=VerificationStatus.VERIFIED, signal_assessment=SignalAssessment.CONFIRMS)
        buckets = classify([link])
        assert buckets["true_positive"] == [link]
        assert buckets["false_positive"] == []
        assert buckets["unresolved"] == []

    def test_verified_confirms_outside_horizon_is_unresolved_not_true_positive(self):
        link = _link("a", within_horizon=False, verification_status=VerificationStatus.VERIFIED, signal_assessment=SignalAssessment.CONFIRMS)
        buckets = classify([link])
        assert buckets["true_positive"] == []
        assert buckets["unresolved"] == [link]

    def test_verified_refutes_is_false_positive(self):
        link = _link("a", verification_status=VerificationStatus.VERIFIED, signal_assessment=SignalAssessment.REFUTES)
        buckets = classify([link])
        assert buckets["false_positive"] == [link]

    def test_rejected_refutes_is_also_false_positive(self):
        link = _link("a", verification_status=VerificationStatus.REJECTED, signal_assessment=SignalAssessment.REFUTES)
        buckets = classify([link])
        assert buckets["false_positive"] == [link]

    def test_not_yet_assessed_is_unresolved(self):
        link = _link("a", verification_status=VerificationStatus.VERIFIED, signal_assessment=SignalAssessment.NOT_YET_ASSESSED)
        buckets = classify([link])
        assert buckets["unresolved"] == [link]

    def test_inconclusive_is_unresolved(self):
        link = _link("a", verification_status=VerificationStatus.VERIFIED, signal_assessment=SignalAssessment.INCONCLUSIVE)
        buckets = classify([link])
        assert buckets["unresolved"] == [link]

    def test_unverified_outcome_is_unresolved_even_if_assessed(self):
        link = _link("a", verification_status=VerificationStatus.UNVERIFIED, signal_assessment=SignalAssessment.CONFIRMS)
        buckets = classify([link])
        assert buckets["unresolved"] == [link]

    def test_conflicting_outcome_is_unresolved(self):
        link = _link("a", verification_status=VerificationStatus.CONFLICTING, signal_assessment=SignalAssessment.CONFIRMS)
        buckets = classify([link])
        assert buckets["unresolved"] == [link]


class TestComputeMetrics:
    def test_below_minimum_sample_size_is_not_computed(self):
        links = [_link(str(i)) for i in range(5)]
        result = compute_metrics(links, min_verified_links=10)
        assert result.computed is False
        assert result.precision is None
        assert result.reason is not None

    def test_at_minimum_sample_size_computes_precision(self):
        tp_links = [_link(str(i), verification_status=VerificationStatus.VERIFIED, signal_assessment=SignalAssessment.CONFIRMS) for i in range(8)]
        fp_links = [_link(f"fp-{i}", verification_status=VerificationStatus.VERIFIED, signal_assessment=SignalAssessment.REFUTES) for i in range(2)]
        result = compute_metrics(tp_links + fp_links, min_verified_links=10)
        assert result.computed is True
        assert result.precision == 0.8
        assert result.true_positive_count == 8
        assert result.false_positive_count == 2

    def test_unresolved_links_do_not_count_toward_eligible(self):
        tp_links = [_link(str(i)) for i in range(10)]
        unresolved_links = [_link(f"u-{i}", signal_assessment=SignalAssessment.NOT_YET_ASSESSED) for i in range(50)]
        result = compute_metrics(tp_links + unresolved_links, min_verified_links=10)
        assert result.computed is True
        assert result.eligible_count == 10
        assert result.precision == 1.0

    def test_all_false_positive_gives_zero_precision(self):
        fp_links = [_link(str(i), verification_status=VerificationStatus.VERIFIED, signal_assessment=SignalAssessment.REFUTES) for i in range(10)]
        result = compute_metrics(fp_links, min_verified_links=10)
        assert result.computed is True
        assert result.precision == 0.0

    def test_default_min_verified_links_constant_is_used(self):
        links = [_link(str(i)) for i in range(5)]
        result = compute_metrics(links)
        assert result.min_required == 10


class TestCoverageClassification:
    def test_no_links_at_all_is_no_data(self):
        assert coverage_classification(eligible_link_count=0, total_link_count=0) == "no_data"

    def test_links_exist_but_none_eligible_is_no_coverage(self):
        assert coverage_classification(eligible_link_count=0, total_link_count=20) == "no_coverage"

    def test_below_eighty_percent_is_partial_coverage(self):
        assert coverage_classification(eligible_link_count=5, total_link_count=20) == "partial_coverage"

    def test_at_or_above_eighty_percent_is_full_coverage(self):
        assert coverage_classification(eligible_link_count=16, total_link_count=20) == "full_coverage"

    def test_all_eligible_is_full_coverage(self):
        assert coverage_classification(eligible_link_count=20, total_link_count=20) == "full_coverage"
