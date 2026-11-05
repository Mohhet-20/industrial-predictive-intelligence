"""Intelligence lifecycle, registry, and governance domain tables.

WHAT THIS MODULE DOES NOT DO: it does not re-implement training, detection,
or forecasting. Every real computation still lives where put it
(app.anomaly.*, app.condition.*, app.timeseries.*) and every real trained
artifact still lives in app.models.intelligence.ModelVersion or
app.models.condition.ForecastModel. This module adds a thin, honest
GOVERNANCE LAYER on top of those existing tables: a stable logical identity
per capability (IntelligenceComponent), a lifecycle stage per concrete
version of that capability (ComponentVersion), an append-only audit trail
of every lifecycle action (GovernanceEvent), plus two genuinely new
capabilities never built: a controlled retraining-request
workflow (RetrainingRequest) and a data/feature shift-analysis record
(ShiftAnalysisRun).

Why a separate registry rather than adding "lifecycle_status" columns
directly to ModelVersion/ForecastModel: those tables are already the
record of what a detector/forecaster IS (its config, its data, its
result). Registry concerns (is this version approved for use right now?
who approved it? when was it activated?) are a different axis that also
needs to apply, one day, to component kinds that have no such table at all
(a heuristic method like app.condition.degradation, which is pure code with
no persisted "training" row). Layering the registry on top — referencing
the real row by id rather than duplicating its fields — keeps ModelVersion/
ForecastModel exactly as trustworthy as they were (nothing
about their own columns changes) while giving every component kind,
trainable or not, the same lifecycle vocabulary. See
docs/MLOPS_GOVERNANCE.md §0 for the full capability audit this design is
built from.

VersionKind.HEURISTIC has no id in ModelVersion or ForecastModel at all —
`heuristic_code_version` is a plain string (e.g. "degradation_classifier@1")
naming the deterministic function + the module-level constants it currently
runs with. There is nothing to "activate" in the sense of swapping a
stored artifact; registering and activating a heuristic version is a
governance/documentation act (this is the version of the method currently
in effect, reviewed and approved) not a code deployment.

Only one ComponentVersion per IntelligenceComponent may be at
lifecycle_stage=ACTIVE at any time. Nothing in this schema enforces that
with a DB constraint (conditional/partial-unique constraints are not
portably expressible against SQLite) — it is enforced transactionally in
app.services.mlops_service.activate_version, which always demotes any
currently-active version in the same commit before promoting the new one.
"Currently active" is therefore always looked up by querying
ComponentVersion, never by a pointer stored on IntelligenceComponent — see
that decision recorded here so it isn't "rediscovered" as a bug later.
"""
import enum
import uuid

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, utcnow


class ComponentType(str, enum.Enum):
    """The four honest categories this platform can actually produce.
    Never call a STATISTICAL_DETECTOR or HEURISTIC_SIGNAL_METHOD "a machine
    learning model" anywhere in the API or frontend — see
    docs/MLOPS_GOVERNANCE.md §5."""

    TRAINED_ML_MODEL = "trained_ml_model"                # e.g. the Isolation Forest detector — a real sklearn estimator fit on data
    STATISTICAL_DETECTOR = "statistical_detector"          # e.g. the robust z-score detector — closed-form statistics, not a fitted model
    FORECASTING_MODEL = "forecasting_model"                 # the honesty-gated linear-trend forecaster
    HEURISTIC_SIGNAL_METHOD = "heuristic_signal_method"     # e.g. degradation classification, change-candidate detection — deterministic rules, no fitting at all


class VersionKind(str, enum.Enum):
    MODEL_VERSION = "model_version"      # links to app.models.intelligence.ModelVersion.id
    FORECAST_MODEL = "forecast_model"    # links to app.models.condition.ForecastModel.id
    HEURISTIC = "heuristic"              # no linked row — heuristic_code_version names it instead


class LifecycleStage(str, enum.Enum):
    """Adapted per component type rather than forced uniformly —
    see app.services.mlops_service for exactly which transitions are legal
    for which ComponentType, and docs/MLOPS_GOVERNANCE.md §1 for the
    reasoning (e.g. a HEURISTIC version has no TRAINED step to pass
    through; it starts at EVALUATED because there was never anything to
    train)."""

    EXPERIMENTING = "experimenting"
    TRAINED = "trained"
    EVALUATED = "evaluated"
    VALIDATED = "validated"
    APPROVED = "approved"
    ACTIVE = "active"
    RETIRED = "retired"
    FAILED = "failed"


class IntelligenceComponent(TimestampMixin, Base):
    """The stable, logical identity of one intelligence capability — e.g.
    "bearing_temp_de isolation forest detector". Independent of any one
    trained/fitted version; a component accumulates ComponentVersion rows
    over its lifetime, and this row never changes once registered except
    its description."""

    __tablename__ = "intelligence_components"
    __table_args__ = (UniqueConstraint("organization_id", "component_type", "name", name="uq_component_org_type_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)

    component_type: Mapped[ComponentType] = mapped_column(Enum(ComponentType, native_enum=False, length=30), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    versions: Mapped[list["ComponentVersion"]] = relationship(back_populates="component", cascade="all, delete-orphan", order_by="ComponentVersion.version_number")


class ComponentVersion(TimestampMixin, Base):
    """One concrete version registered under a component. Points at the
    REAL underlying row (ModelVersion or ForecastModel) that actually
    holds the config/data/results, or names a heuristic's code version
    when there is no such row. `lifecycle_stage` is the only field this
    table ever changes after creation (plus the activation/retirement
    bookkeeping fields below, which are just a record of when/who) —
    `validation_evidence` is written once, at the VALIDATED transition,
    and never edited afterward, the same append-only discipline as
    the investigation/review tables."""

    __tablename__ = "component_versions"
    __table_args__ = (UniqueConstraint("component_id", "version_number", name="uq_component_version_number"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    component_id: Mapped[str] = mapped_column(ForeignKey("intelligence_components.id", ondelete="CASCADE"), nullable=False)

    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    version_kind: Mapped[VersionKind] = mapped_column(Enum(VersionKind, native_enum=False, length=20), nullable=False)

    model_version_id: Mapped[str | None] = mapped_column(ForeignKey("model_versions.id", ondelete="CASCADE"), nullable=True)
    forecast_model_id: Mapped[str | None] = mapped_column(ForeignKey("forecast_models.id", ondelete="CASCADE"), nullable=True)
    heuristic_code_version: Mapped[str | None] = mapped_column(String(200), nullable=True)

    lifecycle_stage: Mapped[LifecycleStage] = mapped_column(Enum(LifecycleStage, native_enum=False, length=20), nullable=False)

    # Human-supplied at the VALIDATED transition — see
    # app.services.mlops_service.validate_version for the task-specific
    # gate each ComponentType must satisfy before this can be set.
    validation_evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    approval_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_at: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    activated_at: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    retired_at: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    retirement_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    component: Mapped["IntelligenceComponent"] = relationship(back_populates="versions")


class GovernanceEventType(str, enum.Enum):
    COMPONENT_REGISTERED = "component_registered"
    VERSION_REGISTERED = "version_registered"
    TRAINING_INITIATED = "training_initiated"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    EVALUATION_RECORDED = "evaluation_recorded"
    VALIDATION_DECISION = "validation_decision"
    APPROVAL_DECISION = "approval_decision"
    ACTIVATED = "activated"
    ROLLED_BACK = "rolled_back"
    RETIRED = "retired"
    RETRAINING_REQUESTED = "retraining_requested"
    SCORING_REFUSED = "scoring_refused"
    SHIFT_ANALYSIS_RECORDED = "shift_analysis_recorded"


class GovernanceEvent(Base):
    """Append-only audit log of every lifecycle action this module takes.
    Never updated or deleted. `details` is a small JSON payload of
    non-secret context (e.g. the reason string, evaluation summary
    numbers) — never a token, password, or raw config that might carry
    one; see app.services.mlops_service for what's deliberately excluded."""

    __tablename__ = "governance_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    component_id: Mapped[str] = mapped_column(ForeignKey("intelligence_components.id", ondelete="CASCADE"), nullable=False)
    component_version_id: Mapped[str | None] = mapped_column(ForeignKey("component_versions.id", ondelete="SET NULL"), nullable=True)

    event_type: Mapped[GovernanceEventType] = mapped_column(Enum(GovernanceEventType, native_enum=False, length=30), nullable=False)
    from_stage: Mapped[LifecycleStage | None] = mapped_column(Enum(LifecycleStage, native_enum=False, length=20), nullable=True)
    to_stage: Mapped[LifecycleStage | None] = mapped_column(Enum(LifecycleStage, native_enum=False, length=20), nullable=True)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ShiftAnalysisRun(Base):
    """A recorded comparison between a reference period and a comparison
    period for one sensor — summary statistics, missingness, and (where
    both samples are large enough for it to mean anything) a two-sample
    statistical test. Never auto-triggers retraining; see
    app.services.mlops_service.run_shift_analysis's module-level note on
    why PSI is deliberately not implemented here."""

    __tablename__ = "shift_analysis_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    sensor_id: Mapped[str] = mapped_column(ForeignKey("sensors.id", ondelete="CASCADE"), nullable=False)
    component_id: Mapped[str | None] = mapped_column(ForeignKey("intelligence_components.id", ondelete="SET NULL"), nullable=True)

    reference_period_start: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    reference_period_end: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    comparison_period_start: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    comparison_period_end: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)

    method: Mapped[str] = mapped_column(String(100), nullable=False)
    result: Mapped[dict] = mapped_column(JSON, nullable=False)
    limitations: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class RetrainingRequestStatus(str, enum.Enum):
    REQUESTED = "requested"
    RUNNING = "running"
    EVALUATED = "evaluated"
    FAILED = "failed"
    REJECTED = "rejected"


class RetrainingRequest(Base):
    """A controlled, always-explicit request to retrain/refit the real
    underlying detector/forecaster behind one component. Executing a
    request calls the SAME production training functions
    already built (app.services.anomaly_service.create_model_version /
    app.services.condition_service.create_forecast_model) — never a
    simulated or fabricated result — and produces a new ComponentVersion
    at TRAINED or FAILED. It never activates that version automatically;
    see app.services.mlops_service.execute_retraining_request."""

    __tablename__ = "retraining_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    component_id: Mapped[str] = mapped_column(ForeignKey("intelligence_components.id", ondelete="CASCADE"), nullable=False)

    requested_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict] = mapped_column(JSON, nullable=False)

    status: Mapped[RetrainingRequestStatus] = mapped_column(Enum(RetrainingRequestStatus, native_enum=False, length=20), nullable=False, default=RetrainingRequestStatus.REQUESTED)
    resulting_component_version_id: Mapped[str | None] = mapped_column(ForeignKey("component_versions.id", ondelete="SET NULL"), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)
