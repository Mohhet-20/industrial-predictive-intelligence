"""Equipment Condition Intelligence & Forecasting domain tables.

Built on top of the anomaly-detection tables (app.models.intelligence)
without modifying them. See docs/CONDITION_INTELLIGENCE.md §0 for the
evidence audit that determined which of this schema's capabilities are
actually implemented (Path C: unlabeled sensor data only — no verified
failure labels or maintenance history exist for this platform).

Design choices worth recording here, mirroring app.models.intelligence's
own conventions:

- `DegradationSignal.stage` can only ever reach VALIDATED_DEGRADATION
  through an explicit human review action (see
  app.services.condition_service.validate_degradation_signal) — nothing in
  this codebase's detection/classification logic sets it automatically.
  See app.condition.degradation's module docstring for why.
- `DegradationReview` is an APPEND-ONLY audit log, the same pattern as
  app.models.intelligence.AnomalyInvestigation: a DegradationSignal's
  original evidence/classification fields are written once, by the
  ConditionRun that created it, and never modified by a review action —
  only `status` (and separately, `stage`, via the dedicated validation
  action) change, and every prior state is preserved in the review history.
- `ForecastRun.points` is a bounded JSON array (capped at
  app.condition.forecasting.MAX_FORECAST_POINTS), not a per-timestamp
  table — the same "don't persist an unbounded low-value table" reasoning
  as ModelRun.metrics (see app.models.intelligence's module docstring).
- This module supersedes and replaces the empty, never-implemented
  `Prediction` table from the schema (see the migration that drops
  it) — `Prediction` was reserved for exactly this work and had zero rows
  in every environment this schema has run in.
"""
import enum
import uuid

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, utcnow


class ConditionRunStatus(str, enum.Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ConditionRun(Base):
    """One execution of the degradation/condition analysis (app.condition.*)
    against a sensor over an analysis window + a distinct reference window
    used for baseline deviation. Mirrors app.models.intelligence.ModelRun."""

    __tablename__ = "condition_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), nullable=False)
    sensor_id: Mapped[str] = mapped_column(ForeignKey("sensors.id", ondelete="CASCADE"), nullable=False)

    analysis_period_start: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    analysis_period_end: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    reference_period_start: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    reference_period_end: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)

    config: Mapped[dict] = mapped_column(JSON, nullable=False)  # sub_window_count, thresholds used — a replayable snapshot
    status: Mapped[ConditionRunStatus] = mapped_column(Enum(ConditionRunStatus, native_enum=False, length=20), nullable=False)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # bounded summary: evidence computed, signal produced or not

    started_at: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    signal: Mapped["DegradationSignal | None"] = relationship(back_populates="condition_run", uselist=False, cascade="all, delete-orphan")


class DegradationStage(str, enum.Enum):
    OBSERVED_TREND = "observed_trend"
    DEGRADATION_CANDIDATE = "degradation_candidate"
    VALIDATED_DEGRADATION = "validated_degradation"


class ConditionSignalStatus(str, enum.Enum):
    """Mirrors app.models.intelligence.AnomalyStatus exactly, for the same
    review-workflow reasons."""

    DETECTED = "detected"
    UNDER_REVIEW = "under_review"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    FALSE_POSITIVE = "false_positive"


class DegradationSignal(TimestampMixin, Base):
    """One degradation candidate/observed-trend produced by a ConditionRun.
    `status` and `stage` are the only fields a review action ever changes
    (see module docstring); `evidence` is written once and never modified."""

    __tablename__ = "degradation_signals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), nullable=False)
    sensor_id: Mapped[str] = mapped_column(ForeignKey("sensors.id", ondelete="CASCADE"), nullable=False)
    condition_run_id: Mapped[str] = mapped_column(ForeignKey("condition_runs.id", ondelete="CASCADE"), nullable=False)

    stage: Mapped[DegradationStage] = mapped_column(Enum(DegradationStage, native_enum=False, length=30), nullable=False)
    status: Mapped[ConditionSignalStatus] = mapped_column(Enum(ConditionSignalStatus, native_enum=False, length=20), nullable=False, default=ConditionSignalStatus.DETECTED)

    period_start: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)

    # Full evidence bundle: app.condition.evidence's five evidence types +
    # app.condition.degradation's classification rationale and sub-window
    # trends. Never modified after creation.
    evidence: Mapped[dict] = mapped_column(JSON, nullable=False)

    condition_run: Mapped["ConditionRun"] = relationship(back_populates="signal")
    reviews: Mapped[list["DegradationReview"]] = relationship(back_populates="signal", cascade="all, delete-orphan", order_by="DegradationReview.created_at")


class DegradationReview(Base):
    """Append-only audit log of status transitions AND stage promotions
    (the ONLY way stage can reach VALIDATED_DEGRADATION — see module
    docstring). Mirrors app.models.intelligence.AnomalyInvestigation."""

    __tablename__ = "degradation_reviews"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    degradation_signal_id: Mapped[str] = mapped_column(ForeignKey("degradation_signals.id", ondelete="CASCADE"), nullable=False)
    reviewed_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    from_status: Mapped[ConditionSignalStatus] = mapped_column(Enum(ConditionSignalStatus, native_enum=False, length=20), nullable=False)
    to_status: Mapped[ConditionSignalStatus] = mapped_column(Enum(ConditionSignalStatus, native_enum=False, length=20), nullable=False)
    from_stage: Mapped[DegradationStage] = mapped_column(Enum(DegradationStage, native_enum=False, length=30), nullable=False)
    to_stage: Mapped[DegradationStage] = mapped_column(Enum(DegradationStage, native_enum=False, length=30), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    signal: Mapped["DegradationSignal"] = relationship(back_populates="reviews")


class ForecastMethod(str, enum.Enum):
    LINEAR_TREND = "linear_trend"


class ForecastModelStatus(str, enum.Enum):
    JUSTIFIED = "justified"          # backtest showed the method earns its keep — POST .../runs is allowed
    NOT_JUSTIFIED = "not_justified"  # backtest ran successfully but did not beat baselines — an honest negative result, not an error
    FAILED = "failed"                # could not even run the backtest (insufficient data)


class ForecastModel(TimestampMixin, Base):
    """One fit-and-backtest attempt for one sensor. Unlike
    app.models.intelligence.ModelVersion (whose evaluation is a separate,
    optional POST .../evaluate call against synthetic data), the backtest
    here runs INSIDE fitting, because status/is_justified gates whether any
    forecast run is even allowed — see app.services.condition_service."""

    __tablename__ = "forecast_models"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    sensor_id: Mapped[str] = mapped_column(ForeignKey("sensors.id", ondelete="CASCADE"), nullable=False)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    method: Mapped[ForecastMethod] = mapped_column(Enum(ForecastMethod, native_enum=False, length=30), nullable=False)

    config: Mapped[dict] = mapped_column(JSON, nullable=False)  # fit/test windows, seasonal_period_seconds — a replayable snapshot
    fit_period_start: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    fit_period_end: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    fit_observation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    status: Mapped[ForecastModelStatus] = mapped_column(Enum(ForecastModelStatus, native_enum=False, length=20), nullable=False)
    training_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    fitted_at: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)

    # ALWAYS populated (never optional, unlike anomaly evaluation) — see
    # app.condition.forecasting.backtest_and_compare. Holds every baseline's
    # MAE/RMSE alongside the candidate's, plus is_justified + the reason.
    evaluation_results: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    runs: Mapped[list["ForecastRun"]] = relationship(back_populates="forecast_model", cascade="all, delete-orphan")


class ForecastRunStatus(str, enum.Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ForecastRun(Base):
    """One execution producing forward-looking points from an already-
    JUSTIFIED ForecastModel. `points` is a bounded JSON array — see module
    docstring on why this isn't a per-point table."""

    __tablename__ = "forecast_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    forecast_model_id: Mapped[str] = mapped_column(ForeignKey("forecast_models.id", ondelete="CASCADE"), nullable=False)

    status: Mapped[ForecastRunStatus] = mapped_column(Enum(ForecastRunStatus, native_enum=False, length=20), nullable=False)
    as_of: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    horizon_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    step_seconds: Mapped[float] = mapped_column(Float, nullable=False)

    points: Mapped[list | None] = mapped_column(JSON, nullable=True)  # [{target_timestamp, predicted_value, lower_bound, upper_bound}, ...]
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    started_at: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    forecast_model: Mapped["ForecastModel"] = relationship(back_populates="runs")
