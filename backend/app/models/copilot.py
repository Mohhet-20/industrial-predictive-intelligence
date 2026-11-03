"""Copilot conversation persistence (Phase 9).

The copilot is an OPTIONAL, provider-dependent interface layered on top
of the platform's real intelligence — see docs/COPILOT.md. These two
tables exist so a conversation's evidence trail is durable and auditable
in the same append-only style as every other review/annotation table in
this project (AnomalyInvestigation, DegradationReview,
InvestigationAnnotation): a `CopilotMessage` is never edited after being
written, and its `grounding` payload is stored verbatim so a user (or a
later reviewer) can always see exactly which real records an answer was
based on — never just the generated text with no way to check it.

Nothing here stores provider secrets. `provider_used` records which LLM
provider produced an assistant message (or "none" when the platform
answered honestly that no provider is configured), never an API key or
raw request payload.
"""
import enum
import uuid

from sqlalchemy import DateTime, Enum, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, utcnow


class CopilotContextType(str, enum.Enum):
    """What the conversation is scoped to. GENERAL means no specific
    asset/investigation was selected — the copilot still only answers from
    retrieved evidence, it just has a wider (still bounded) search scope."""

    GENERAL = "general"
    ASSET = "asset"
    INVESTIGATION = "investigation"
    ANOMALY = "anomaly"
    DEGRADATION_SIGNAL = "degradation_signal"
    SCENARIO = "scenario"  # Phase 11 — a saved what-if Scenario/ScenarioRun; answers must state the data is simulated
    OUTCOME = "outcome"  # Phase 12 — a reported Outcome and its verification/links/evaluation evidence


class CopilotConversation(TimestampMixin, Base):
    __tablename__ = "copilot_conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)

    context_type: Mapped[CopilotContextType] = mapped_column(Enum(CopilotContextType, native_enum=False, length=30), nullable=False)
    # The real id this conversation is scoped to (asset_id / investigation_id
    # / anomaly_id / degradation_signal_id), re-validated for org ownership
    # on every message, never trusted from the client alone. Null for GENERAL.
    context_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    title: Mapped[str | None] = mapped_column(String(300), nullable=True)

    messages: Mapped[list["CopilotMessage"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", order_by="CopilotMessage.created_at"
    )


class CopilotMessageRole(str, enum.Enum):
    USER = "user"
    ASSISTANT = "assistant"


class CopilotMessage(Base):
    """Append-only. An assistant message's `grounding` field is the list of
    real EvidenceItem records (see app/copilot/grounding.py) it was built
    from — the durable answer of "what did this claim actually come from,"
    kept even if the underlying evidence is later reviewed/updated."""

    __tablename__ = "copilot_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    conversation_id: Mapped[str] = mapped_column(ForeignKey("copilot_conversations.id", ondelete="CASCADE"), nullable=False)

    role: Mapped[CopilotMessageRole] = mapped_column(Enum(CopilotMessageRole, native_enum=False, length=20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # List[dict] snapshot of the EvidenceItem objects grounding this answer.
    # Null for user messages.
    grounding: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # "anthropic" | "none" (no provider configured) | "unavailable" (provider
    # configured but the call failed, e.g. timeout) — never a secret.
    provider_used: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # True if the post-generation grounding validator (app/copilot/grounding.py)
    # found and stripped/flagged a claim it could not tie to retrieved
    # evidence. Kept visible rather than silently cleaned, so the safeguard
    # itself is auditable.
    flagged_ungrounded: Mapped[bool] = mapped_column(default=False, nullable=False)

    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    conversation: Mapped["CopilotConversation"] = relationship(back_populates="messages")
