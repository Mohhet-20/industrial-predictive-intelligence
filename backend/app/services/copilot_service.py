"""Copilot conversation persistence + orchestration glue.

Conversations and messages are scoped by BOTH organization_id (the usual
tenant-isolation boundary) AND user_id — a copilot conversation is a
personal investigative thread, not organization-wide content like an
Investigation or Anomaly, so one user cannot read another user's copilot
history even within the same organization. This is a deliberately
narrower boundary than the rest of the platform, and is documented as
such in docs/COPILOT.md.

Messages are append-only, mirroring every other audit-relevant table in
this codebase (AnomalyInvestigation, DegradationReview,
InvestigationAnnotation): nothing here is ever edited or deleted after
creation. An assistant message's `grounding` snapshot is preserved even
if the underlying evidence changes later — the message shows what the
copilot was told AT THE TIME, not a live-recomputed view.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.copilot.orchestrator import answer_question
from app.copilot.providers import get_provider
from app.core.config import Settings
from app.core.errors import NotFoundError
from app.models.copilot import CopilotContextType, CopilotConversation, CopilotMessage, CopilotMessageRole
from app.models.user import User


def create_conversation(
    db: Session,
    *,
    organization_id: str,
    user: User,
    context_type: CopilotContextType,
    context_id: Optional[str],
) -> CopilotConversation:
    conversation = CopilotConversation(
        organization_id=organization_id,
        user_id=user.id,
        context_type=context_type,
        context_id=context_id,
    )
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


def get_conversation(db: Session, *, organization_id: str, user: User, conversation_id: str) -> CopilotConversation:
    conversation = (
        db.query(CopilotConversation)
        .filter(
            CopilotConversation.id == conversation_id,
            CopilotConversation.organization_id == organization_id,
            CopilotConversation.user_id == user.id,
        )
        .first()
    )
    if conversation is None:
        raise NotFoundError(f"Conversation '{conversation_id}' was not found.")
    return conversation


def list_conversations(db: Session, *, organization_id: str, user: User, limit: int, offset: int):
    query = (
        db.query(CopilotConversation)
        .filter(CopilotConversation.organization_id == organization_id, CopilotConversation.user_id == user.id)
        .order_by(CopilotConversation.created_at.desc())
    )
    total = query.count()
    return query.offset(offset).limit(limit).all(), total


def post_message(
    db: Session,
    *,
    organization_id: str,
    settings: Settings,
    user: User,
    conversation_id: str,
    question: str,
    evidence_selection: Optional[list[dict[str, str]]],
) -> CopilotMessage:
    """Persists the user's question, runs the orchestrator, persists and
    returns the assistant's reply. If the orchestrator raises (e.g. the
    question references an asset/anomaly/investigation that doesn't exist
    or isn't in this organization), the user message is still persisted —
    the conversation shows what was actually asked — but no assistant
    message is created, and the error propagates to the API layer's usual
    error handling (the same NotFoundError/ValidationFailedError contract
    every other endpoint uses)."""
    conversation = get_conversation(db, organization_id=organization_id, user=user, conversation_id=conversation_id)

    user_message = CopilotMessage(conversation_id=conversation.id, role=CopilotMessageRole.USER, content=question)
    db.add(user_message)
    db.commit()

    provider = get_provider(settings)
    result = answer_question(
        db,
        organization_id=organization_id,
        settings=settings,
        provider=provider,
        context_type=conversation.context_type,
        context_id=conversation.context_id,
        question=question,
        evidence_selection=evidence_selection,
    )

    assistant_message = CopilotMessage(
        conversation_id=conversation.id,
        role=CopilotMessageRole.ASSISTANT,
        content=result.answer_text,
        grounding=[item.as_dict() for item in result.evidence],
        provider_used=result.provider_used,
        flagged_ungrounded=result.flagged_ungrounded,
    )
    db.add(assistant_message)
    if conversation.title is None:
        conversation.title = question[:120]
    db.commit()
    db.refresh(assistant_message)
    return assistant_message
