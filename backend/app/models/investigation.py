"""Intelligence orchestration domain tables (Phase 6).

WHAT THIS MODULE DOES NOT DO: it does not re-implement anomaly detection,
degradation classification, forecasting, or MLOps governance. Every real
signal still lives exactly where Phases 3-5 put it
(app.models.intelligence, app.models.condition, app.models.mlops) and
nothing here ever mutates those rows. This module adds a human-oriented
ORGANIZATIONAL layer on top of that real evidence:

- `Investigation` — a case a human opens to look into changing equipment
  behavior. Asset-scoped (an investigation is about one piece of
  equipment), optionally sensor/time-window-scoped.
- `InvestigationEvidenceLink` — connects an investigation to real evidence
  elsewhere in the schema. See "Reference vs. snapshot" below.
- `InvestigationStatusEvent` — append-only status-transition audit log,
  mirroring app.models.intelligence.AnomalyInvestigation and
  app.models.condition.DegradationReview exactly.
- `InvestigationAnnotation` — append-only human notes. Explicitly NOT a
  verified training label — see docs/INTELLIGENCE_ORCHESTRATION.md §9 for
  why this platform keeps "human annotation" and "verified outcome" as
  distinct concepts and implements only the former (no verification
  workflow, no outcome-labeling pipeline, exists anywhere in this
  codebase).
- `InvestigationRelation` — an optional, symmetric-in-practice link between
  two investigations a human judges related (e.g. two investigations on
  sensors that share a common upstream cause under review).

Reference vs. snapshot (see InvestigationEvidenceLink docstring): every
evidence kind this platform can actually produce as a stable, durably-
identified row (Anomaly, DegradationSignal, DataQualityIssue, ForecastRun,
ComponentVersion, GovernanceEvent, ShiftAnalysisRun) is linked by
reference — evidence_type + evidence_id — never duplicated. Exactly one
evidence kind, a change/behavior candidate from
app.timeseries.change_detection, is computed on demand and was never
assigned a durable id at all (see that module's docstring — it operates on
an in-memory pandas Series and returns a plain list). There is nothing to
reference for that kind, so `snapshot` carries exactly what was observed
at link time, honestly labeled as a point-in-time snapshot rather than a
live reference. This is a fact about what change-candidate detection
actually is (a stateless computation), not a design shortcut.
"""
import enum
import uuid

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, utcnow


class InvestigationStatus(str, enum.Enum):
    OPEN = "open"
    UNDER_REVIEW = "under_review"
    AWAITING_CONTEXT = "awaiting_context"
    RESOLVED = "resolved"
    CLOSED = "closed"


class InvestigationEvidenceType(str, enum.Enum):
    """What kind of real record `InvestigationEvidenceLink.evidence_id`
    points at (app.models.intelligence / app.models.condition /
    app.models.mlops / app.models.data_quality, per the table name in the
    comment). CHANGE_CANDIDATE is the one snapshot-only kind — see module
    docstring."""

    ANOMALY = "anomaly"                          # app.models.intelligence.Anomaly
    DEGRADATION_SIGNAL = "degradation_signal"     # app.models.condition.DegradationSignal
    DATA_QUALITY_ISSUE = "data_quality_issue"     # app.models.data_quality.DataQualityIssue
    FORECAST_RUN = "forecast_run"                 # app.models.condition.ForecastRun
    COMPONENT_VERSION = "component_version"       # app.models.mlops.ComponentVersion
    GOVERNANCE_EVENT = "governance_event"         # app.models.mlops.GovernanceEvent
    SHIFT_ANALYSIS_RUN = "shift_analysis_run"     # app.models.mlops.ShiftAnalysisRun
    SENSOR = "sensor"                             # app.models.sensor.Sensor — "this sensor is relevant context"
    CHANGE_CANDIDATE = "change_candidate"         # snapshot-only — no stable id exists anywhere in this schema
    SCENARIO_RESULT = "scenario_result"           # app.models.scenario.ScenarioRun (Phase 11) — a HYPOTHETICAL, never observed evidence


class InvestigationAnnotationType(str, enum.Enum):
    OBSERVATION = "observation"
    OPERATIONAL_CONTEXT = "operational_context"
    RESOLUTION = "resolution"
    FALSE_POSITIVE_ASSESSMENT = "false_positive_assessment"
    FOLLOW_UP_REQUIRED = "follow_up_required"


class Investigation(TimestampMixin, Base):
    """A human-opened case investigating changing equipment behavior on one
    asset. `status` is denormalized current state, same pattern as
    Anomaly.status/DegradationSignal.status — the full transition history
    lives in InvestigationStatusEvent and is never lost. Opening an
    investigation and linking evidence to it NEVER modifies the evidence
    it references (see InvestigationEvidenceLink)."""

    __tablename__ = "investigations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True)
    primary_sensor_id: Mapped[str | None] = mapped_column(ForeignKey("sensors.id", ondelete="SET NULL"), nullable=True)

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The period of equipment behavior under investigation — informational
    # scoping for the timeline/evidence UI, not enforced against linked
    # evidence (a human may reasonably link evidence just outside this
    # window, e.g. an earlier data-quality issue providing context).
    time_window_start: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)
    time_window_end: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[InvestigationStatus] = mapped_column(Enum(InvestigationStatus, native_enum=False, length=20), nullable=False, default=InvestigationStatus.OPEN)

    opened_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    opened_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    evidence_links: Mapped[list["InvestigationEvidenceLink"]] = relationship(back_populates="investigation", cascade="all, delete-orphan", order_by="InvestigationEvidenceLink.linked_at")
    status_events: Mapped[list["InvestigationStatusEvent"]] = relationship(back_populates="investigation", cascade="all, delete-orphan", order_by="InvestigationStatusEvent.created_at")
    annotations: Mapped[list["InvestigationAnnotation"]] = relationship(back_populates="investigation", cascade="all, delete-orphan", order_by="InvestigationAnnotation.created_at")


class InvestigationEvidenceLink(Base):
    """One edge in the evidence graph: this investigation is connected to
    one piece of real evidence, by reference wherever the evidence has a
    durable id, or by a labeled snapshot where it doesn't (CHANGE_CANDIDATE
    only — see module docstring). Never stores a copy of evidence that
    already has a stable id elsewhere; `evidence_type` + `evidence_id` is
    the only representation for those kinds, so the linked-to row stays
    the single source of truth and can be navigated to directly."""

    __tablename__ = "investigation_evidence_links"
    __table_args__ = (
        UniqueConstraint("investigation_id", "evidence_type", "evidence_id", name="uq_investigation_evidence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id", ondelete="CASCADE"), nullable=False)

    evidence_type: Mapped[InvestigationEvidenceType] = mapped_column(Enum(InvestigationEvidenceType, native_enum=False, length=30), nullable=False)
    evidence_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # null only for CHANGE_CANDIDATE
    sensor_id: Mapped[str | None] = mapped_column(ForeignKey("sensors.id", ondelete="SET NULL"), nullable=True)  # denormalized for cheap filtering

    # Populated only for CHANGE_CANDIDATE — the exact candidate payload
    # app.timeseries.change_detection returned at link time. Never used for
    # any other evidence_type (those are always looked up live from their
    # real table so their current status/data is never stale).
    snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    relation_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    linked_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    linked_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    investigation: Mapped["Investigation"] = relationship(back_populates="evidence_links")


class InvestigationStatusEvent(Base):
    """Append-only audit log of status transitions, mirroring
    app.models.intelligence.AnomalyInvestigation exactly. The row created
    when an investigation is opened has from_status=NULL (there was no
    prior status); every later row has both."""

    __tablename__ = "investigation_status_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id", ondelete="CASCADE"), nullable=False)
    changed_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    from_status: Mapped[InvestigationStatus | None] = mapped_column(Enum(InvestigationStatus, native_enum=False, length=20), nullable=True)
    to_status: Mapped[InvestigationStatus] = mapped_column(Enum(InvestigationStatus, native_enum=False, length=20), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    investigation: Mapped["Investigation"] = relationship(back_populates="status_events")


class InvestigationAnnotation(Base):
    """One append-only human note on an investigation — never edited or
    deleted, the same discipline as every other audit table in this
    platform. This is a HUMAN INTERPRETATION, kept explicitly distinct
    from raw data, ML/statistical outputs, and (a concept this platform
    does NOT implement) a verified outcome — see module docstring and
    docs/INTELLIGENCE_ORCHESTRATION.md §9. Nothing in this codebase reads
    `annotation_type == FALSE_POSITIVE_ASSESSMENT` back into any detector,
    forecaster, or training workflow; recording one here does not, by
    itself, change the status of the anomaly/signal it discusses — that
    requires the reviewer to also call the existing, separate
    anomaly/degradation-signal review action."""

    __tablename__ = "investigation_annotations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id", ondelete="CASCADE"), nullable=False)
    author_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    annotation_type: Mapped[InvestigationAnnotationType] = mapped_column(Enum(InvestigationAnnotationType, native_enum=False, length=30), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    follow_up_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    investigation: Mapped["Investigation"] = relationship(back_populates="annotations")


class InvestigationRelation(Base):
    """A human-asserted link between two investigations judged related
    (spec §1: "Related investigations where appropriate"). Directional in
    storage (investigation_id -> related_investigation_id) but intended to
    be read symmetrically by the API — see
    app.services.investigation_service.list_related_investigations."""

    __tablename__ = "investigation_relations"
    __table_args__ = (
        UniqueConstraint("investigation_id", "related_investigation_id", name="uq_investigation_relation"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id", ondelete="CASCADE"), nullable=False)
    related_investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id", ondelete="CASCADE"), nullable=False)

    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    linked_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
