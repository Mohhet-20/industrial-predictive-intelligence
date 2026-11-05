"""Error-pattern surfacing: late detections, duplicate candidates, and
false-negative candidates.

Every finding in this module is framed explicitly as a *candidate* for a
human to review — never as an automatically asserted root cause (by design:
"do not automatically infer root cause"). A `FalseNegativeCandidate`
is a flag, not an accusation that detection failed.
"""
from __future__ import annotations

import dataclasses

LATE_DETECTION_THRESHOLD_SECONDS = 86400.0  # 24 hours — named, explicit
DUPLICATE_WINDOW_SECONDS = 3600.0  # 1 hour — named, explicit


@dataclasses.dataclass(frozen=True)
class GroupedCount:
    """A generic (key -> member ids) histogram bucket, used by both the
    duplicate-candidate and late-detection grouping helpers below."""

    key: tuple
    member_ids: list[str]

    @property
    def count(self) -> int:
        return len(self.member_ids)


def _group(items: list[tuple[str, tuple]]) -> list[GroupedCount]:
    """`items` is a list of (member_id, key) pairs. Groups by key, preserving
    first-seen order of keys."""
    order: list[tuple] = []
    buckets: dict[tuple, list[str]] = {}
    for member_id, key in items:
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(member_id)
    return [GroupedCount(key=key, member_ids=buckets[key]) for key in order]


@dataclasses.dataclass(frozen=True)
class LateDetection:
    """A true-positive link whose detection delay (outcome_timestamp minus
    the signal's prediction_timestamp) exceeded the late-detection
    threshold. Late, but still correct — this is a timeliness finding, not
    a correctness finding."""

    link_id: str
    signal_id: str
    detection_delay_seconds: float


def find_late_detections(
    true_positive_links: list,
    *,
    threshold_seconds: float = LATE_DETECTION_THRESHOLD_SECONDS,
) -> list[LateDetection]:
    """`true_positive_links` is the `true_positive` bucket from
    `app.feedback.metrics.classify` — objects with `.link_id`, `.signal_id`,
    `.detection_delay_seconds`."""
    results: list[LateDetection] = []
    for link in true_positive_links:
        delay = getattr(link, "detection_delay_seconds", None)
        if delay is not None and delay > threshold_seconds:
            results.append(LateDetection(link_id=link.link_id, signal_id=link.signal_id, detection_delay_seconds=delay))
    return results


@dataclasses.dataclass(frozen=True)
class DuplicateCandidate:
    """Multiple signals of the same type, on the same sensor, within
    `DUPLICATE_WINDOW_SECONDS` of each other — a candidate for "this is one
    event the detector reported several times," not an asserted duplicate."""

    sensor_id: str
    signal_type: str
    signal_ids: list[str]


def find_duplicate_candidates(
    signals_for_duplicate_check: list[tuple],
    *,
    window_seconds: float = DUPLICATE_WINDOW_SECONDS,
) -> list[DuplicateCandidate]:
    """`signals_for_duplicate_check` is a list of
    (signal_id, sensor_id, signal_type, timestamp) tuples, already restricted
    to the scope the caller wants checked (e.g. one evaluation run's scope).
    Within each (sensor_id, signal_type) group, timestamps are sorted and
    walked once; any signal within `window_seconds` of the previous one in
    the same walk joins the same duplicate-candidate cluster.
    """
    by_key: dict[tuple, list[tuple]] = {}
    order: list[tuple] = []
    for signal_id, sensor_id, signal_type, timestamp in signals_for_duplicate_check:
        key = (sensor_id, signal_type)
        if key not in by_key:
            by_key[key] = []
            order.append(key)
        by_key[key].append((timestamp, signal_id))

    results: list[DuplicateCandidate] = []
    for key in order:
        sensor_id, signal_type = key
        entries = sorted(by_key[key], key=lambda e: e[0])
        cluster: list[str] = []
        cluster_start = None
        for timestamp, signal_id in entries:
            if cluster_start is None or (timestamp - cluster_start).total_seconds() <= window_seconds:
                if cluster_start is None:
                    cluster_start = timestamp
                cluster.append(signal_id)
            else:
                if len(cluster) > 1:
                    results.append(DuplicateCandidate(sensor_id=sensor_id, signal_type=signal_type, signal_ids=list(cluster)))
                cluster = [signal_id]
                cluster_start = timestamp
        if len(cluster) > 1:
            results.append(DuplicateCandidate(sensor_id=sensor_id, signal_type=signal_type, signal_ids=list(cluster)))
    return results


@dataclasses.dataclass(frozen=True)
class FalseNegativeCandidate:
    """A verified, fault-like outcome with no outcome-signal link pointing
    to it. A candidate for human review of why nothing flagged it — never
    an asserted missed detection."""

    outcome_id: str
    sensor_id: str | None
    reason: str


def find_false_negative_candidates(
    fault_like_outcomes: list[tuple],
    linked_outcome_ids: set[str],
) -> list[FalseNegativeCandidate]:
    """`fault_like_outcomes` is a list of (outcome_id, sensor_id) tuples for
    VERIFIED outcomes whose outcome_type represents a real fault (e.g.
    FAILURE_EVENT). `linked_outcome_ids` is the set of outcome ids that have
    at least one `OutcomeSignalLink`."""
    results: list[FalseNegativeCandidate] = []
    for outcome_id, sensor_id in fault_like_outcomes:
        if outcome_id not in linked_outcome_ids:
            results.append(
                FalseNegativeCandidate(
                    outcome_id=outcome_id,
                    sensor_id=sensor_id,
                    reason="verified fault-like outcome has no linked signal of any type — candidate for review, not an asserted missed detection",
                )
            )
    return results


@dataclasses.dataclass(frozen=True)
class ErrorAnalysisReport:
    late_detections: list[LateDetection]
    duplicate_candidates: list[DuplicateCandidate]
    false_negative_candidates: list[FalseNegativeCandidate]

    def to_dict(self) -> dict:
        return {
            "late_detections": [dataclasses.asdict(d) for d in self.late_detections],
            "duplicate_candidates": [dataclasses.asdict(d) for d in self.duplicate_candidates],
            "false_negative_candidates": [dataclasses.asdict(d) for d in self.false_negative_candidates],
            "late_detection_count": len(self.late_detections),
            "duplicate_candidate_count": len(self.duplicate_candidates),
            "false_negative_candidate_count": len(self.false_negative_candidates),
        }


def build_error_analysis_report(
    *,
    true_positive_links: list,
    signals_for_duplicate_check: list[tuple],
    fault_like_outcomes: list[tuple],
    linked_outcome_ids: set[str],
) -> ErrorAnalysisReport:
    return ErrorAnalysisReport(
        late_detections=find_late_detections(true_positive_links),
        duplicate_candidates=find_duplicate_candidates(signals_for_duplicate_check),
        false_negative_candidates=find_false_negative_candidates(fault_like_outcomes, linked_outcome_ids),
    )
