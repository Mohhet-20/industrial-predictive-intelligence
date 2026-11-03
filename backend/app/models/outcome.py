"""Closed-loop outcome, verification, and evaluation domain tables
(Phase 12).

CORE PRINCIPLE, enforced throughout every table below:

    PREDICTIONS ARE NOT TRUTH.
    HUMAN FEEDBACK IS NOT AUTOMATICALLY GROUND TRUTH.
    MAINTENANCE EVENTS ARE NOT AUTOMATICALLY FAILURES.
    OUTCOMES MUST HAVE PROVENANCE.

MANDATORY AUDIT FINDING (Phase 12 §0), which this schema is built from:
as of this phase, this platform has ZERO real-world verified outcome
data. `app.models.intelligence.MaintenanceEvent` has existed since Phase 1
as an explicitly documented stub ("PLANNED — no ingestion workflow exists
yet") with zero rows and zero real code references anywhere outside its
own definition. `app.models.investigation.InvestigationAnnotation`
explicitly documents itself as distinct from "a verified outcome (a
concept this platform does NOT implement)" prior to this phase. No
CMMS/ERP/work-order integration exists. `OutcomeSource` below therefore has
exactly one member — not a placeholder for a future integration, an honest
statement of the only source that currently exists: a human typing a
report into this platform. Every table here is new and additive; nothing
about any existing table changes.

Table map:

- `Outcome` — a human-reported operational event (maintenance, inspection,
  failure, recovery, or other) — never forced into "failure."
- `OutcomeVerification` — append-only audit trail of verification state
  transitions. Verification is a separate, later, explicit human act —
  reporting an outcome is not the same as anyone confirming it.
- `OutcomeSignalLink` — links an Outcome to a specific prior signal
  (anomaly/degradation signal/forecast run/scenario run) with an explicit,
  frozen horizon and a separate human assessment of whether the outcome
  confirms or refutes that signal.
- `OutcomeEvaluationRun` — one computed evaluation snapshot. Additive-only:
  a re-evaluation is a new row, chained via `previous_evaluation_run_id`,
  never an edit to a past one.
- `RetrainingCandidate` — a proposal to consider retraining a component,
  reviewed by a human before it ever reaches
  `app.services.mlops_service.request_retraining` (Phase 5's existing,
  unmodified, human-initiated retraining workflow).
- `FeedbackDatasetBuild` — a frozen, versioned snapshot of verified+assessed
  rows, built for external use. Never includes unverified feedback.
- `PrioritizationPolicy` — a versioned, single-active-per-organization
  adjustment to signal prioritization. With no active policy (the default)
  the existing prioritization formula is unchanged.
"""
import enum
import uuid

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, utcnow


class OutcomeType(str, enum.Enum):
    """Deliberately not a single "failure/no failure" boolean — see module
    docstring's core principle. A maintenance event is not automatically a
    failure, and an inspection result is not automatically either."""

    MAINTENANCE_EVENT = "maintenance_event"
    OPERATIONAL_EVENT = "operational_event"
    INSPECTION_RESULT = "inspection_result"
    FAILURE_EVENT = "failure_event"
    RECOVERY_EVENT = "recovery_event"
    OTHER = "other"


class OutcomeSource(str, enum.Enum):
    """Exactly one member today — see the mandatory audit finding above.
    Not a placeholder; an honest statement of what actually exists."""

    USER_REPORTED = "user_reported"


class VerificationStatus(str, enum.Enum):
    """UNVERIFIED is the only state an Outcome can be created in. VERIFIED
    and REJECTED each require a separate, explicit, evidenced human
    decision — see app.services.feedback_service.VALID_VERIFICATION_TRANSITIONS
    for the enforced state machine. CONFLICTING preserves disagreement
    rather than letting the latest decision silently win."""

    UNVERIFIED = "unverified"
    REVIEWED = "reviewed"
    VERIFIED = "verified"
    REJECTED = "rejected"
    CONFLICTING = "conflicting"


class Outcome(TimestampMixin, Base):
    """A human-reported operational event. `verification_status` always
    starts UNVERIFIED (enforced in app.services.feedback_service, not here)
    — reporting an outcome is never itself verification.

    Two distinct clocks (Phase 15 Phase I), never conflated: `outcome_timestamp`
    is WHEN THE REAL-WORLD EVENT OCCURRED, as the reporting human states it —
    it can be backdated (reporting today that something happened last week).
    `created_at` (from TimestampMixin) is WHEN THIS REPORT BECAME KNOWN TO THE
    PLATFORM — a DB-assigned insert time, set once by
    app.services.feedback_service.report_outcome, never backdated and never
    editable afterward. app.feedback.matching.evaluation_run_leakage_check
    checks both independently against an evaluation run's own
    evaluation_period_end/computed_at — a backdated outcome_timestamp inside
    the evaluation period does not, by itself, mean this platform actually
    knew about it in time."""

    __tablename__ = "outcomes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True)
    sensor_id: Mapped[str | None] = mapped_column(ForeignKey("sensors.id", ondelete="CASCADE"), nullable=True, index=True)
    investigation_id: Mapped[str | None] = mapped_column(ForeignKey("investigations.id", ondelete="SET NULL"), nullable=True, index=True)

    outcome_type: Mapped[OutcomeType] = mapped_column(Enum(OutcomeType, native_enum=False, length=30), nullable=False)
    source: Mapped[OutcomeSource] = mapped_column(Enum(OutcomeSource, native_enum=False, length=20), nullable=False, default=OutcomeSource.USER_REPORTED)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    # The real-world period this outcome's observation covers, where known
    # (e.g. an inspection window) — distinct from outcome_timestamp (when
    # the event/observation itself is dated).
    observation_period_start: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)
    observation_period_end: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome_timestamp: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    verification_status: Mapped[VerificationStatus] = mapped_column(Enum(VerificationStatus, native_enum=False, length=20), nullable=False, default=VerificationStatus.UNVERIFIED, index=True)
    reported_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    verifications: Mapped[list["OutcomeVerification"]] = relationship(back_populates="outcome", cascade="all, delete-orphan", order_by="OutcomeVerification.created_at")
    links: Mapped[list["OutcomeSignalLink"]] = relationship(back_populates="outcome", cascade="all, delete-orphan", order_by="OutcomeSignalLink.linked_at")


class OutcomeVerification(Base):
    """Append-only audit trail of every verification state transition.
    `reviewer_role` is a SNAPSHOT at the time of the decision, not a live
    join to app.models.user.User — a later role change must never rewrite
    what role actually made a past decision."""

    __tablename__ = "outcome_verifications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    outcome_id: Mapped[str] = mapped_column(ForeignKey("outcomes.id", ondelete="CASCADE"), nullable=False)

    from_status: Mapped[VerificationStatus] = mapped_column(Enum(VerificationStatus, native_enum=False, length=20), nullable=False)
    to_status: Mapped[VerificationStatus] = mapped_column(Enum(VerificationStatus, native_enum=False, length=20), nullable=False)
    reviewer_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reviewer_role: Mapped[str | None] = mapped_column(String(20), nullable=True)
    evidence_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    outcome: Mapped["Outcome"] = relationship(back_populates="verifications")


class OutcomeSignalType(str, enum.Enum):
    """The four kinds of prior signal an outcome can be linked to.
    SCENARIO_RUN exists so a Scenario Lab (Phase 11) result can be
    compared against a real outcome for a human's own benefit — see
    OutcomeSignalLink.counts_toward_evaluation, which is server-computed
    False for this signal_type and cannot be overridden by a client."""

    ANOMALY = "anomaly"
    DEGRADATION_SIGNAL = "degradation_signal"
    FORECAST_RUN = "forecast_run"
    SCENARIO_RUN = "scenario_run"


class SignalAssessment(str, enum.Enum):
    """A SEPARATE, explicit human judgment field — deliberately not
    inferred automatically from Outcome.outcome_type or
    Outcome.verification_status. Without this field, computing a
    false-positive/false-negative count would require guessing root cause
    from the outcome's category alone, which Phase 12 §9 explicitly
    prohibits ("do not automatically infer root cause")."""

    NOT_YET_ASSESSED = "not_yet_assessed"
    CONFIRMS = "confirms"
    REFUTES = "refutes"
    INCONCLUSIVE = "inconclusive"


class OutcomeSignalLink(Base):
    """Links one Outcome to one signal. `prediction_timestamp` and
    `horizon_seconds` are frozen at link-creation time — see
    app.feedback.matching's module docstring for why this is the choke
    point that makes backward information leakage structurally
    impossible: `within_horizon` is computed once, purely from these two
    fields plus the outcome's own timestamp, never re-derived later and
    never dependent on `linked_at` or "now"."""

    __tablename__ = "outcome_signal_links"
    __table_args__ = (UniqueConstraint("outcome_id", "signal_type", "signal_id", name="uq_outcome_signal_link"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    outcome_id: Mapped[str] = mapped_column(ForeignKey("outcomes.id", ondelete="CASCADE"), nullable=False)

    signal_type: Mapped[OutcomeSignalType] = mapped_column(Enum(OutcomeSignalType, native_enum=False, length=30), nullable=False)
    signal_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # Denormalized for filtering (app.services.feedback_service's evaluation
    # queries) — resolved once from the real signal row at link time, never
    # written by a client.
    sensor_id: Mapped[str | None] = mapped_column(ForeignKey("sensors.id", ondelete="SET NULL"), nullable=True)
    component_id: Mapped[str | None] = mapped_column(ForeignKey("intelligence_components.id", ondelete="SET NULL"), nullable=True)

    prediction_timestamp: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    horizon_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    within_horizon: Mapped[bool] = mapped_column(Boolean, nullable=False)
    counts_toward_evaluation: Mapped[bool] = mapped_column(Boolean, nullable=False)

    signal_assessment: Mapped[SignalAssessment] = mapped_column(Enum(SignalAssessment, native_enum=False, length=20), nullable=False, default=SignalAssessment.NOT_YET_ASSESSED)
    assessed_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    assessed_at: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)

    linked_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    linked_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    notes: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    outcome: Mapped["Outcome"] = relationship(back_populates="links")


class EvaluationKind(str, enum.Enum):
    INITIAL = "initial"
    UPDATED = "updated"


class OutcomeEvaluationRun(Base):
    """One computed evaluation snapshot. Additive-only — a re-evaluation
    of the same scope is a new row chained via `previous_evaluation_run_id`,
    never an edit to a past row, mirroring every other audit-trail table
    in this platform (GovernanceEvent, DegradationReview, ...)."""

    __tablename__ = "outcome_evaluation_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    component_id: Mapped[str | None] = mapped_column(ForeignKey("intelligence_components.id", ondelete="SET NULL"), nullable=True)
    sensor_id: Mapped[str | None] = mapped_column(ForeignKey("sensors.id", ondelete="SET NULL"), nullable=True)
    signal_type: Mapped[OutcomeSignalType] = mapped_column(Enum(OutcomeSignalType, native_enum=False, length=30), nullable=False)

    evaluation_kind: Mapped[EvaluationKind] = mapped_column(Enum(EvaluationKind, native_enum=False, length=20), nullable=False)
    previous_evaluation_run_id: Mapped[str | None] = mapped_column(ForeignKey("outcome_evaluation_runs.id"), nullable=True)

    scope_description: Mapped[str] = mapped_column(Text, nullable=False)
    evaluation_period_start: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    evaluation_period_end: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    computed_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    total_signals_in_scope: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    linked_outcome_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    verified_outcome_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    coverage_fraction: Mapped[float | None] = mapped_column(Float, nullable=True)

    # app.feedback.metrics.EvaluationMetricsResult.to_dict() plus
    # coverage_classification and label_definitions_version — see
    # app.services.feedback_service.run_evaluation.
    metrics: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class RetrainingCandidateStatus(str, enum.Enum):
    PROPOSED = "proposed"
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    DISMISSED = "dismissed"


class RetrainingCandidate(Base):
    """A proposal to consider retraining — never itself trains or
    activates anything. Approval calls the existing, unmodified
    app.services.mlops_service.request_retraining (Phase 5); this table
    only records the proposal and the human decision on it."""

    __tablename__ = "retraining_candidates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    component_id: Mapped[str] = mapped_column(ForeignKey("intelligence_components.id", ondelete="CASCADE"), nullable=False)
    supporting_evaluation_run_id: Mapped[str | None] = mapped_column(ForeignKey("outcome_evaluation_runs.id"), nullable=True)

    trigger_reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[RetrainingCandidateStatus] = mapped_column(Enum(RetrainingCandidateStatus, native_enum=False, length=20), nullable=False, default=RetrainingCandidateStatus.PROPOSED)

    reviewed_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)
    resulting_retraining_request_id: Mapped[str | None] = mapped_column(ForeignKey("retraining_requests.id"), nullable=True)

    created_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class FeedbackDatasetStatus(str, enum.Enum):
    BUILT = "built"
    FAILED = "failed"


class FeedbackDatasetBuild(TimestampMixin, Base):
    """A frozen, versioned snapshot of verified+assessed rows. Never
    includes unverified feedback — see
    app.services.feedback_service.build_feedback_dataset's query filter."""

    __tablename__ = "feedback_dataset_builds"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    definition: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[FeedbackDatasetStatus] = mapped_column(Enum(FeedbackDatasetStatus, native_enum=False, length=20), nullable=False, default=FeedbackDatasetStatus.BUILT)
    dataset_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    built_at: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class PrioritizationPolicyStatus(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    RETIRED = "retired"


class PrioritizationPolicy(TimestampMixin, Base):
    """A versioned, single-active-per-organization adjustment to signal
    prioritization — mirrors app.models.mlops.ComponentVersion's
    single-active, explicit-activation, versioned pattern exactly. With no
    active policy (the default state), app.services.orchestration_service
    .get_prioritized_signals's existing formula runs completely
    unchanged."""

    __tablename__ = "prioritization_policies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[PrioritizationPolicyStatus] = mapped_column(Enum(PrioritizationPolicyStatus, native_enum=False, length=20), nullable=False, default=PrioritizationPolicyStatus.DRAFT)
    config: Mapped[dict] = mapped_column(JSON, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)

    activated_at: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    retired_at: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)

    created_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
