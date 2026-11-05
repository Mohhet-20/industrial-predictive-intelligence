"""Deterministic suggested questions. Not LLM-generated —
a fixed, hand-written list per context type, filtered by nothing but the
context type itself. This is intentional: suggested questions are a UX
affordance to show the copilot's actual scope, not a place to introduce
another source of unreviewed generated text. See docs/COPILOT.md."""
from app.models.copilot import CopilotContextType

_GENERAL_QUESTIONS = [
    "What can you help me investigate?",
    "What data quality issues exist across my assets right now?",
    "What is the difference between an anomaly and a validated degradation signal on this platform?",
    "Can this platform predict equipment failure in advance?",
]

_ASSET_QUESTIONS = [
    "What sensors are on this asset and what do they measure?",
    "What data-quality issues have been detected on this asset?",
    "What anomalies have been detected on this asset recently?",
    "Is there any human-reviewed context on this asset's recent anomalies?",
]

_INVESTIGATION_QUESTIONS = [
    "Summarize the timeline of this investigation so far.",
    "What evidence has been attached to this investigation?",
    "What annotations have reviewers added to this investigation?",
    "Has this investigation's status changed, and why?",
]

_ANOMALY_QUESTIONS = [
    "What does this anomaly's severity tier and score actually mean?",
    "What did the data look like before, during, and after this anomaly?",
    "What detector and method version flagged this anomaly?",
    "Has a human reviewed this anomaly, and what did they conclude?",
    "Has any real-world outcome been reported and linked to this anomaly, and has it been verified?",
]

_DEGRADATION_SIGNAL_QUESTIONS = [
    "What degradation signals exist for this sensor?",
    "Has any forecast model for this sensor been validated against a real backtest?",
    "What does 'validated_degradation' mean, and has this signal reached it?",
    "What would a human reviewer need to check before treating this as real degradation?",
]

_OUTCOME_QUESTIONS = [
    "What did the human who reported this outcome say happened?",
    "Has this outcome been verified, rejected, or is it still unverified?",
    "What signals has this outcome been linked to, and do they confirm or refute them?",
    "Is this outcome real, or did it come from a simulated Scenario Lab run?",
]

_BY_CONTEXT: dict[CopilotContextType, list[str]] = {
    CopilotContextType.GENERAL: _GENERAL_QUESTIONS,
    CopilotContextType.ASSET: _ASSET_QUESTIONS,
    CopilotContextType.INVESTIGATION: _INVESTIGATION_QUESTIONS,
    CopilotContextType.ANOMALY: _ANOMALY_QUESTIONS,
    CopilotContextType.DEGRADATION_SIGNAL: _DEGRADATION_SIGNAL_QUESTIONS,
    CopilotContextType.OUTCOME: _OUTCOME_QUESTIONS,
}


def suggested_questions(context_type: CopilotContextType) -> list[str]:
    return list(_BY_CONTEXT.get(context_type, _GENERAL_QUESTIONS))
