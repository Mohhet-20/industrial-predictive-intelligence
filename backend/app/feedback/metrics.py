"""Classification of outcome-signal links and precision-only metric
computation, gated on a minimum verified-link sample size.

Deliberately reports precision only — never recall or F1. This platform has
no way to enumerate every fault that was never signaled in the first place
(there is no complete inventory of "things that should have been caught"),
so recall has no honest denominator here. Reporting a fabricated recall
number would violate Phase 12's core principle just as surely as inventing
a confidence score would.
"""
from __future__ import annotations

import dataclasses

from app.models.outcome import SignalAssessment, VerificationStatus

MIN_VERIFIED_LINKS_FOR_METRICS = 10
MIN_VERIFIED_LINKS_FOR_UPDATE = 5


@dataclasses.dataclass(frozen=True)
class ClassifiedLink:
    """The minimal, already-resolved fields `classify`/`compute_metrics`
    need. Built by the caller (`app.services.feedback_service`) from a real
    `OutcomeSignalLink` plus its parent `Outcome`'s verification status."""

    link_id: str
    signal_id: str
    within_horizon: bool
    verification_status: VerificationStatus
    signal_assessment: SignalAssessment
    detection_delay_seconds: float | None = None


def classify(links: list[ClassifiedLink]) -> dict[str, list[ClassifiedLink]]:
    """Buckets links into true_positive / false_positive / unresolved per
    the rules in `app.feedback.label_definitions.CLASSIFICATION_RULES`.
    """
    buckets: dict[str, list[ClassifiedLink]] = {
        "true_positive": [],
        "false_positive": [],
        "unresolved": [],
    }
    for link in links:
        if (
            link.verification_status == VerificationStatus.VERIFIED
            and link.signal_assessment == SignalAssessment.CONFIRMS
            and link.within_horizon
        ):
            buckets["true_positive"].append(link)
        elif (
            link.verification_status in (VerificationStatus.VERIFIED, VerificationStatus.REJECTED)
            and link.signal_assessment == SignalAssessment.REFUTES
        ):
            buckets["false_positive"].append(link)
        else:
            buckets["unresolved"].append(link)
    return buckets


@dataclasses.dataclass(frozen=True)
class EvaluationMetricsResult:
    """`computed=False` means there is not yet enough verified+assessed
    evidence to report a metric — this is an honest "not enough data" state,
    not a zero or a null pretending to be a real value."""

    computed: bool
    reason: str | None
    precision: float | None
    true_positive_count: int
    false_positive_count: int
    unresolved_count: int
    eligible_count: int
    min_required: int


def compute_metrics(
    links: list[ClassifiedLink],
    *,
    min_verified_links: int = MIN_VERIFIED_LINKS_FOR_METRICS,
) -> EvaluationMetricsResult:
    buckets = classify(links)
    tp = len(buckets["true_positive"])
    fp = len(buckets["false_positive"])
    unresolved = len(buckets["unresolved"])
    eligible = tp + fp

    if eligible < min_verified_links:
        return EvaluationMetricsResult(
            computed=False,
            reason=(
                f"only {eligible} verified+assessed link(s) are classified true/false positive; "
                f"at least {min_verified_links} are required before a metric is reported"
            ),
            precision=None,
            true_positive_count=tp,
            false_positive_count=fp,
            unresolved_count=unresolved,
            eligible_count=eligible,
            min_required=min_verified_links,
        )

    precision = (tp / eligible) if eligible > 0 else None
    return EvaluationMetricsResult(
        computed=True,
        reason=None,
        precision=precision,
        true_positive_count=tp,
        false_positive_count=fp,
        unresolved_count=unresolved,
        eligible_count=eligible,
        min_required=min_verified_links,
    )


def coverage_classification(*, eligible_link_count: int, total_link_count: int) -> str:
    """`no_data`: no links exist at all. `no_coverage`: links exist but none
    are eligible (verified+assessed). `partial_coverage`/`full_coverage`:
    split at 80% of links being eligible — an explicit, named threshold."""
    if total_link_count == 0:
        return "no_data"
    if eligible_link_count == 0:
        return "no_coverage"
    fraction = eligible_link_count / total_link_count
    return "full_coverage" if fraction >= 0.8 else "partial_coverage"
