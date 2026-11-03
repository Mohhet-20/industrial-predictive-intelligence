"""Evidence-grounded AI copilot API (Phase 9).

Every route here requires authentication (organization_scope, same as the
rest of the platform) — there is no anonymous or cross-tenant access to
copilot conversations. No route in this router can control equipment,
change a schedule, or modify safety configuration: the copilot is
strictly read/investigate, never act (Phase 9 §10 — see
docs/COPILOT.md's "industrial safety boundaries" section). Any VIEWER,
ENGINEER, or ADMIN user may use the copilot to ask about evidence they
already have read access to; no additional write-role is required
because posting a question never mutates platform intelligence data,
only the copilot's own conversation history.
"""
from fastapi import APIRouter, Depends, Query

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.deps import get_current_user, organization_scope
from app.db.session import get_db
from app.models.copilot import CopilotContextType
from app.models.user import User
from app.schemas.common import Page
from app.schemas.copilot import (
    ConversationCreateRequest,
    ConversationResponse,
    CopilotStatusResponse,
    MessageCreateRequest,
    MessageResponse,
    SuggestedQuestionsResponse,
)
from app.copilot.questions import suggested_questions
from app.services import copilot_service

router = APIRouter(prefix="/copilot", tags=["copilot"])


@router.get("/status", response_model=CopilotStatusResponse)
def get_status(user: User = Depends(get_current_user)) -> CopilotStatusResponse:
    """Deliberately requires only authentication, not organization_scope —
    every organization sees the same deployment-level provider status.
    Never reveals whether a configured key is actually valid, only
    whether one is present, so this can't be used to probe credentials."""
    settings = get_settings()
    if settings.copilot_configured:
        return CopilotStatusResponse(
            provider=settings.copilot_llm_provider,
            configured=True,
            model=settings.copilot_llm_model,
            note="An AI provider is configured. Answers are still restricted to retrieved platform evidence.",
        )
    return CopilotStatusResponse(
        provider="none",
        configured=False,
        model=None,
        note=(
            "No AI provider is configured for this deployment. The copilot can still retrieve and display "
            "real platform evidence for your question; it just won't generate natural-language prose from it."
        ),
    )


@router.get("/suggested-questions", response_model=SuggestedQuestionsResponse)
def get_suggested_questions(
    context_type: CopilotContextType = Query(default=CopilotContextType.GENERAL),
    context_id: str | None = Query(default=None),
    user: User = Depends(get_current_user),
) -> SuggestedQuestionsResponse:
    """Phase 16 fix: this previously had no auth dependency at all,
    contradicting this module's own docstring claim that "every route here
    requires authentication." Deliberately only `get_current_user`, not
    `organization_scope` — like `/status` above, the suggested-question
    text is static per context_type and carries no tenant data, so there is
    nothing to scope by organization; the fix is requiring a valid session,
    not adding an org filter that would have no real target."""
    return SuggestedQuestionsResponse(
        context_type=context_type, context_id=context_id, questions=suggested_questions(context_type)
    )


@router.post("/conversations", response_model=ConversationResponse, status_code=201)
def create_conversation(
    payload: ConversationCreateRequest,
    org_id: str = Depends(organization_scope),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ConversationResponse:
    conversation = copilot_service.create_conversation(
        db, organization_id=org_id, user=user, context_type=payload.context_type, context_id=payload.context_id
    )
    return ConversationResponse.model_validate(conversation)


@router.get("/conversations", response_model=Page[ConversationResponse])
def list_conversations(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    org_id: str = Depends(organization_scope),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Page[ConversationResponse]:
    rows, total = copilot_service.list_conversations(db, organization_id=org_id, user=user, limit=limit, offset=offset)
    return Page(items=[ConversationResponse.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/conversations/{conversation_id}", response_model=ConversationResponse)
def get_conversation(
    conversation_id: str,
    org_id: str = Depends(organization_scope),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ConversationResponse:
    conversation = copilot_service.get_conversation(db, organization_id=org_id, user=user, conversation_id=conversation_id)
    return ConversationResponse.model_validate(conversation)


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageResponse])
def list_messages(
    conversation_id: str,
    org_id: str = Depends(organization_scope),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[MessageResponse]:
    conversation = copilot_service.get_conversation(db, organization_id=org_id, user=user, conversation_id=conversation_id)
    return [MessageResponse.model_validate(m) for m in conversation.messages]


@router.post("/conversations/{conversation_id}/messages", response_model=MessageResponse, status_code=201)
def post_message(
    conversation_id: str,
    payload: MessageCreateRequest,
    org_id: str = Depends(organization_scope),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MessageResponse:
    settings = get_settings()
    message = copilot_service.post_message(
        db,
        organization_id=org_id,
        settings=settings,
        user=user,
        conversation_id=conversation_id,
        question=payload.question,
        evidence_selection=payload.evidence_selection,
    )
    return MessageResponse.model_validate(message)
