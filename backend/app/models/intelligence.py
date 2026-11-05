"""Anomaly detection domain tables.

Status: ModelVersion, ModelRun, Anomaly, and
AnomalyInvestigation are IMPLEMENTED and backed by real detection logic
(see app/anomaly/* and app/services/anomaly_service.py).

Status: the `Prediction` table described in earlier
revisions of this docstring (reserved for, but never populated by, any
forecasting work) has been DROPPED and replaced by
app.models.condition.ForecastModel/ForecastRun, which carry a real,
backtested, honesty-gated forecasting implementation — see
docs/CONDITION_INTELLIGENCE.md. `MaintenanceEvent` below remains PLANNED:
the evidence audit (docs/CONDITION_INTELLIGENCE.md §0)
re-confirmed it still has no ingestion workflow and zero rows in every
environment this schema has run in.

Design choices worth recording here:

- A detector's raw per-point/per-window scores are NOT persisted for every
  scored observation — only points that cross a threshold become an
  `Anomaly` row. Persisting every score for a 5-minute-cadence sensor over
  a multi-week reference+scoring run would be an unbounded, low-value
  table. `ModelRun.metrics` carries a bounded summary (points scored,
  anomalies found, score distribution) instead.
- `AnomalyInvestigation` rows are an APPEND-ONLY audit log of status
  transitions, not an editable annotation. Each row records the
  transition (from_status -> to_status) plus who made it and when.
  `Anomaly.status` is a denormalized "current status" for fast filtering,
  but the original detection fields on `Anomaly` itself (raw_score,
  detector identity, period, contributing_signals, data_quality_context)
  are never modified by a review action — only `status` (and
  `updated_at`) change, and every prior state is still visible via the
  investigation history. See app/services/anomaly_service.py::transition_anomaly_status.
"""
import enum
import uuid

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, utcnow


class DetectorType(str, enum.Enum):
    ROBUST_ZSCORE = "robust_zscore"
    ISOLATION_FOREST = "isolation_forest"
    MAHALANOBIS = "mahalanobis"


class ModelVersionStatus(str, enum.Enum):
    TRAINED = "trained"
    FAILED = "failed"
    ARCHIVED = "archived"


class ModelVersion(TimestampMixin, Base):
    """One trained (or attempted) detector instance — a specific detector
    type + config + reference period, never mutated in place. Retraining
    with different config/data creates a new ModelVersion row (version
    numbers are per (organization, name), same immutable-version pattern
    as app.models.feature.FeatureDefinition), so a Anomaly referencing an
    old ModelVersion keeps meaning exactly what it meant when it was
    detected."""

    __tablename__ = "model_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    detector_type: Mapped[DetectorType] = mapped_column(Enum(DetectorType, native_enum=False, length=30), nullable=False)

    # What the detector watches and how it was configured — sensor scope,
    # feature specs (Isolation Forest only), window sizes, threshold
    # config. A full, replayable snapshot (mirrors DatasetBuild.config).
    detector_config: Mapped[dict] = mapped_column(JSON, nullable=False)

    reference_period_start: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    reference_period_end: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    reference_observation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Non-null only for isolation_forest — the ordered list of feature
    # names the model was trained on, so scoring later can't silently pass
    # a differently-shaped vector.
    feature_schema: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    baseline_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # median/MAD or mean/std, per app.anomaly.baseline
    status: Mapped[ModelVersionStatus] = mapped_column(Enum(ModelVersionStatus, native_enum=False, length=20), nullable=False)
    training_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    trained_at: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)

    # Populated only by the synthetic evaluation harness (app.anomaly.evaluation)
    # — precision/recall/etc. against deterministic, clearly-synthetic
    # injected scenarios. Never a claim about real-world accuracy.
    evaluation_results: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    runs: Mapped[list["ModelRun"]] = relationship(back_populates="model_version", cascade="all, delete-orphan")


class ModelRunStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ModelRun(Base):
    """One execution of a ModelVersion's detector against a scored time
    range — i.e. one detection run. Anomaly rows reference the run that
    produced them."""

    __tablename__ = "model_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    model_version_id: Mapped[str] = mapped_column(ForeignKey("model_versions.id", ondelete="CASCADE"), nullable=False)

    status: Mapped[ModelRunStatus] = mapped_column(Enum(ModelRunStatus, native_enum=False, length=20), nullable=False, default=ModelRunStatus.QUEUED)
    scored_period_start: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    scored_period_end: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)

    background_job_id: Mapped[str | None] = mapped_column(ForeignKey("background_jobs.id"), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # Bounded summary only — see module docstring for why individual scores
    # aren't persisted per observation.
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    started_at: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    model_version: Mapped["ModelVersion"] = relationship(back_populates="runs")
    anomalies: Mapped[list["Anomaly"]] = relationship(back_populates="model_run")


class AnomalyStatus(str, enum.Enum):
    DETECTED = "detected"
    UNDER_REVIEW = "under_review"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    FALSE_POSITIVE = "false_positive"


class AnomalySeverityTier(str, enum.Enum):
    """Explainable, threshold-derived tiers — never an opaque
    "High Risk" label. See app.anomaly.thresholds for the (documented,
    configurable) cut points that produce these."""

    OBSERVED_UNUSUAL = "observed_unusual_behavior"
    CANDIDATE_ANOMALY = "candidate_anomaly"
    HIGH_PRIORITY = "high_priority_anomaly"


class Anomaly(TimestampMixin, Base):
    """A detector-flagged period. `status` is denormalized current state;
    the full transition history lives in AnomalyInvestigation rows and is
    never lost. The detection fields below (everything except `status`)
    are written once, by the detection run that created this row, and are
    never modified afterward — see module docstring."""

    __tablename__ = "anomalies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), nullable=False)
    sensor_id: Mapped[str | None] = mapped_column(ForeignKey("sensors.id", ondelete="CASCADE"), nullable=True)
    model_run_id: Mapped[str] = mapped_column(ForeignKey("model_runs.id", ondelete="CASCADE"), nullable=False)

    status: Mapped[AnomalyStatus] = mapped_column(Enum(AnomalyStatus, native_enum=False, length=20), nullable=False, default=AnomalyStatus.DETECTED)

    period_start: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)

    detector_type: Mapped[DetectorType] = mapped_column(Enum(DetectorType, native_enum=False, length=30), nullable=False)
    raw_score: Mapped[float] = mapped_column(Float, nullable=False)
    normalized_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold_used: Mapped[float] = mapped_column(Float, nullable=False)
    severity_tier: Mapped[AnomalySeverityTier] = mapped_column(Enum(AnomalySeverityTier, native_enum=False, length=30), nullable=False)

    # What was unusual and why — per-method explanation payload. See
    # app.anomaly.scoring for the exact shape per detector_type.
    contributing_signals: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Missingness/gap context for this window, so a viewer can see whether
    # a flagged point sits in a low-data-quality region before trusting it.
    data_quality_context: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    is_synthetic_scenario: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    model_run: Mapped["ModelRun"] = relationship(back_populates="anomalies")
    investigations: Mapped[list["AnomalyInvestigation"]] = relationship(back_populates="anomaly", cascade="all, delete-orphan", order_by="AnomalyInvestigation.created_at")


class AnomalyInvestigation(Base):
    """One append-only human review action against an Anomaly — a status
    transition plus optional notes. Never updated or deleted; the full
    sequence of rows for one anomaly IS its audit trail."""

    __tablename__ = "anomaly_investigations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    anomaly_id: Mapped[str] = mapped_column(ForeignKey("anomalies.id", ondelete="CASCADE"), nullable=False)
    reviewed_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    from_status: Mapped[AnomalyStatus] = mapped_column(Enum(AnomalyStatus, native_enum=False, length=20), nullable=False)
    to_status: Mapped[AnomalyStatus] = mapped_column(Enum(AnomalyStatus, native_enum=False, length=20), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    anomaly: Mapped["Anomaly"] = relationship(back_populates="investigations")


class MaintenanceEvent(TimestampMixin, Base):
    """PLANNED — historical maintenance record, kept distinct from model
    output and from raw sensor data. No ingestion workflow exists yet."""

    __tablename__ = "maintenance_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)  # e.g. "inspection", "repair", "replacement"
    occurred_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
