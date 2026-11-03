from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.models.copilot import CopilotContextType, CopilotMessageRole


class ConversationCreateRequest(BaseModel):
    context_type: CopilotContextType = CopilotContextType.GENERAL
    context_id: Optional[str] = Field(default=None, max_length=36)


class ConversationResponse(BaseModel):
    id: str
    organization_id: str
    user_id: str
    context_type: CopilotContextType
    context_id: Optional[str]
    title: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}


class EvidenceItemResponse(BaseModel):
    """One grounded piece of evidence behind an answer — see
    app/copilot/grounding.py for the source of truth. `category`
    distinguishes what KIND of claim this evidence supports (never blurred
    together, per docs/COPILOT.md)."""

    ref: str  # short citation id used in the answer text, e.g. "E1"
    category: str  # observed_fact | analytical_result | model_output | human_annotation | interpretation_limitation
    source_type: str  # measurement | data_quality_issue | anomaly | degradation_signal | forecast_run | investigation | annotation | documentation | ...
    source_id: Optional[str]
    label: str
    period_start: Optional[datetime]
    period_end: Optional[datetime]
    method_version: Optional[str]
    text: str


class MessageCreateRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    # Explicit evidence the user selected in the UI (e.g. "explain this
    # anomaly") — always takes priority over broader context-driven search.
    evidence_selection: Optional[list[dict[str, str]]] = None


class MessageResponse(BaseModel):
    id: str
    conversation_id: str
    role: CopilotMessageRole
    content: str
    grounding: Optional[list[EvidenceItemResponse]]
    provider_used: Optional[str]
    flagged_ungrounded: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class CopilotStatusResponse(BaseModel):
    """What the frontend needs to render an honest empty/configured state —
    never leaks whether a key is valid, only whether one is present."""

    provider: str  # "none" | "anthropic"
    configured: bool
    model: Optional[str]
    note: str


class SuggestedQuestionsResponse(BaseModel):
    context_type: CopilotContextType
    context_id: Optional[str]
    questions: list[str]
