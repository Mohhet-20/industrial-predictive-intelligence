"""Copilot orchestration (Phase 9 §3/§6/§7): ties evidence retrieval,
grounding, the LLM provider, and the post-generation safeguard together
into one answer. This is the ONLY place those pieces are wired together —
the API layer never calls app/copilot/tools.py or app/copilot/providers.py
directly, so this module is the single choke point for the "evidence
first, language second" contract.

Design principle carried over from app/copilot/providers.py's own
docstring: a `NullProvider` response must still be genuinely useful. When
no LLM is configured, this module does NOT return a bare error — it
returns the real, retrieved evidence formatted as plain text, so a user
without an API key configured can still see exactly what the platform
would have based an answer on. The copilot degrades gracefully to "show
me the evidence" rather than failing closed to "nothing works."
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from app.copilot import tools
from app.copilot.grounding import (
    EvidenceItem,
    assign_refs,
    build_system_prompt,
    validate_response_grounding,
)
from app.copilot.providers import LLMProvider
from app.core.config import Settings
from app.core.errors import ValidationFailedError
from app.models.copilot import CopilotContextType

# Evidence-selection "kind" -> tool function, each already organization-scoped
# and already bounded. Anything not in this map is rejected explicitly
# rather than silently ignored, so a frontend bug surfaces immediately.
_SELECTION_RESOLVERS = {
    "asset": lambda db, org_id, item_id: tools.get_asset_summary(db, organization_id=org_id, asset_id=item_id),
    "anomaly": lambda db, org_id, item_id: tools.get_anomaly_evidence(db, organization_id=org_id, anomaly_id=item_id),
    "investigation": lambda db, org_id, item_id: tools.get_investigation_timeline(db, organization_id=org_id, investigation_id=item_id),
    "sensor": lambda db, org_id, item_id: tools.get_condition_history(db, organization_id=org_id, sensor_id=item_id),
    "data_quality": lambda db, org_id, item_id: tools.get_data_quality_context(db, organization_id=org_id, asset_id=item_id),
    "scenario": lambda db, org_id, item_id: tools.get_scenario_evidence(db, organization_id=org_id, scenario_id=item_id),
    "outcome": lambda db, org_id, item_id: tools.get_outcome_evidence(db, organization_id=org_id, outcome_id=item_id),
    "evaluation": lambda db, org_id, item_id: tools.get_evaluation_evidence(db, organization_id=org_id, run_id=item_id),
}


@dataclass
class OrchestratorResult:
    answer_text: str
    evidence: list[EvidenceItem]
    provider_used: str
    flagged_ungrounded: bool
    provider_ok: bool
    provider_error: Optional[str] = None


def _gather_context_evidence(
    db: Session, *, organization_id: str, context_type: CopilotContextType, context_id: Optional[str]
) -> list[EvidenceItem]:
    """Resolves the conversation's context into evidence. A context_type
    that needs a context_id but doesn't have one degrades to no
    context-specific evidence (still allows a general/documentation-backed
    answer) rather than erroring — the copilot should never crash a
    question just because the frontend didn't pass an id."""
    if not context_id:
        return []

    if context_type == CopilotContextType.ASSET:
        items = tools.get_asset_summary(db, organization_id=organization_id, asset_id=context_id)
        items += tools.get_data_quality_context(db, organization_id=organization_id, asset_id=context_id)
        items += tools.get_recent_anomalies_for_asset(db, organization_id=organization_id, asset_id=context_id)
        return items
    if context_type == CopilotContextType.ANOMALY:
        return tools.get_anomaly_evidence(db, organization_id=organization_id, anomaly_id=context_id)
    if context_type == CopilotContextType.INVESTIGATION:
        return tools.get_investigation_timeline(db, organization_id=organization_id, investigation_id=context_id)
    if context_type == CopilotContextType.DEGRADATION_SIGNAL:
        # context_id is a sensor id here (see docs/COPILOT.md — the copilot
        # answers about a sensor's condition history, not a single signal
        # row in isolation, since a signal is only ever one point in that
        # history and the surrounding context is usually what's needed).
        return tools.get_condition_history(db, organization_id=organization_id, sensor_id=context_id)
    if context_type == CopilotContextType.SCENARIO:
        # Phase 11 §15 — context_id is a scenario id. get_scenario_evidence
        # is the only tool that can surface a hypothetical result, and it
        # labels every one of its items as simulated (never observed).
        return tools.get_scenario_evidence(db, organization_id=organization_id, scenario_id=context_id)
    if context_type == CopilotContextType.OUTCOME:
        # Phase 12 — context_id is an outcome id.
        return tools.get_outcome_evidence(db, organization_id=organization_id, outcome_id=context_id)
    return []


def _resolve_evidence_selection(
    db: Session, *, organization_id: str, selection: Optional[list[dict[str, str]]]
) -> list[EvidenceItem]:
    if not selection:
        return []
    items: list[EvidenceItem] = []
    for entry in selection:
        kind = entry.get("kind")
        item_id = entry.get("id")
        if not kind or not item_id:
            raise ValidationFailedError("Each evidence_selection entry needs a 'kind' and an 'id'.")
        resolver = _SELECTION_RESOLVERS.get(kind)
        if resolver is None:
            raise ValidationFailedError(
                f"Unknown evidence_selection kind '{kind}'. Supported: {sorted(_SELECTION_RESOLVERS)}."
            )
        items += resolver(db, organization_id, item_id)
    return items


def _dedupe(items: list[EvidenceItem]) -> list[EvidenceItem]:
    seen: set[tuple] = set()
    out = []
    for item in items:
        key = (item.source_type, item.source_id, item.label)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def gather_evidence(
    db: Session,
    *,
    organization_id: str,
    context_type: CopilotContextType,
    context_id: Optional[str],
    question: str,
    evidence_selection: Optional[list[dict[str, str]]],
    max_items: int,
) -> list[EvidenceItem]:
    """The one place evidence assembly happens for a question — always
    bounded to `max_items`, always deduplicated, always ref-assigned last
    so refs are stable for exactly the evidence actually sent to the
    model. Raises NotFoundError/ValidationFailedError from the underlying
    tools unchanged — the API layer turns those into the same honest error
    responses every other endpoint uses (Phase 9 §3)."""
    items = _gather_context_evidence(db, organization_id=organization_id, context_type=context_type, context_id=context_id)
    items += _resolve_evidence_selection(db, organization_id=organization_id, selection=evidence_selection)
    items = _dedupe(items)

    if len(items) < max_items:
        remaining = max_items - len(items)
        doc_hits = tools.search_documentation(question)[:remaining]
        items += doc_hits

    items = items[:max_items]
    return assign_refs(items)


def _format_evidence_only_answer(evidence: list[EvidenceItem]) -> str:
    """Used whenever no LLM provider is configured/available. Not an error
    message — a genuinely useful degraded response: the real evidence,
    plainly listed, exactly as it would have been handed to a model."""
    if not evidence:
        return (
            "No AI provider is configured for this deployment, and no evidence was retrieved for this "
            "question. Set ANTHROPIC_API_KEY to enable natural-language answers, or browse the platform "
            "directly for this asset/investigation."
        )
    lines = [
        "No AI provider is configured for this deployment, so this is the retrieved evidence itself "
        "rather than a generated answer. Set ANTHROPIC_API_KEY to enable natural-language answers.",
        "",
    ]
    for item in evidence:
        period = ""
        if item.period_start or item.period_end:
            period = f" ({item.period_start.isoformat() if item.period_start else '?'} to {item.period_end.isoformat() if item.period_end else '?'})"
        lines.append(f"[{item.ref}] {item.label}{period} — {item.text}")
    return "\n".join(lines)


def answer_question(
    db: Session,
    *,
    organization_id: str,
    settings: Settings,
    provider: LLMProvider,
    context_type: CopilotContextType,
    context_id: Optional[str],
    question: str,
    evidence_selection: Optional[list[dict[str, str]]],
) -> OrchestratorResult:
    evidence = gather_evidence(
        db,
        organization_id=organization_id,
        context_type=context_type,
        context_id=context_id,
        question=question,
        evidence_selection=evidence_selection,
        max_items=settings.copilot_max_evidence_items,
    )

    if provider.name == "none":
        result = provider.generate(system_prompt="", user_message=question)  # NullProvider — always ok=False
        return OrchestratorResult(
            answer_text=_format_evidence_only_answer(evidence),
            evidence=evidence,
            provider_used="none",
            flagged_ungrounded=False,
            provider_ok=False,
            provider_error=result.error,
        )

    system_prompt = build_system_prompt(evidence)
    result = provider.generate(system_prompt=system_prompt, user_message=question)

    if not result.ok:
        # A configured provider that failed at call time (timeout, network,
        # non-200) degrades the same honest way as "not configured" — the
        # user still gets the real evidence, never a fabricated fallback.
        return OrchestratorResult(
            answer_text=_format_evidence_only_answer(evidence),
            evidence=evidence,
            provider_used=result.provider_name,
            flagged_ungrounded=False,
            provider_ok=False,
            provider_error=result.error,
        )

    validation = validate_response_grounding(result.text, evidence)
    return OrchestratorResult(
        answer_text=validation.cleaned_text,
        evidence=evidence,
        provider_used=result.provider_name,
        flagged_ungrounded=validation.flagged,
        provider_ok=True,
        provider_error=None,
    )
