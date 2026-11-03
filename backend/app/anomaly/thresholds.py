"""Threshold configuration and severity tiering.

Every detector produces one numeric score per candidate; a single
`threshold` decides whether it's reported at all. Above that, this module
derives one of three EXPLAINABLE tiers by how far past the threshold the
score is — never an opaque "High Risk" label with no stated methodology:

- OBSERVED_UNUSUAL_BEHAVIOR: score is at or just past the threshold.
- CANDIDATE_ANOMALY: score is meaningfully past it (>= candidate_multiplier x threshold).
- HIGH_PRIORITY_ANOMALY: score is far past it (>= high_priority_multiplier x threshold).

The multipliers are themselves configuration, with documented defaults —
nothing here is a hidden magic number. Tiers are derived independently
per detector run from that run's own threshold; a tier is never compared
across two different detector types' raw score scales (see
app.anomaly.scoring's module docstring for why scores aren't
cross-comparable).
"""
from dataclasses import asdict, dataclass
from typing import Any

from app.models.intelligence import AnomalySeverityTier, DetectorType

DEFAULT_CANDIDATE_MULTIPLIER = 1.3
DEFAULT_HIGH_PRIORITY_MULTIPLIER = 1.8

DETECTOR_DEFAULT_THRESHOLD = {
    DetectorType.ROBUST_ZSCORE: 3.5,
    DetectorType.ISOLATION_FOREST: 0.15,
    DetectorType.MAHALANOBIS: 4.0,
}


@dataclass
class ThresholdConfig:
    threshold: float
    candidate_multiplier: float = DEFAULT_CANDIDATE_MULTIPLIER
    high_priority_multiplier: float = DEFAULT_HIGH_PRIORITY_MULTIPLIER

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_threshold_config(detector_type: DetectorType, raw: dict[str, Any]) -> ThresholdConfig:
    threshold = raw.get("threshold", DETECTOR_DEFAULT_THRESHOLD[detector_type])
    candidate_multiplier = raw.get("candidate_multiplier", DEFAULT_CANDIDATE_MULTIPLIER)
    high_priority_multiplier = raw.get("high_priority_multiplier", DEFAULT_HIGH_PRIORITY_MULTIPLIER)

    if not isinstance(threshold, (int, float)) or threshold <= 0:
        raise ValueError("threshold must be a positive number.")
    if not (candidate_multiplier > 1):
        raise ValueError("candidate_multiplier must be greater than 1.")
    if not (high_priority_multiplier > candidate_multiplier):
        raise ValueError("high_priority_multiplier must be greater than candidate_multiplier.")

    return ThresholdConfig(threshold=float(threshold), candidate_multiplier=float(candidate_multiplier), high_priority_multiplier=float(high_priority_multiplier))


def derive_severity_tier(raw_score: float, config: ThresholdConfig) -> AnomalySeverityTier:
    magnitude = abs(raw_score)
    if magnitude >= config.threshold * config.high_priority_multiplier:
        return AnomalySeverityTier.HIGH_PRIORITY
    if magnitude >= config.threshold * config.candidate_multiplier:
        return AnomalySeverityTier.CANDIDATE_ANOMALY
    return AnomalySeverityTier.OBSERVED_UNUSUAL
