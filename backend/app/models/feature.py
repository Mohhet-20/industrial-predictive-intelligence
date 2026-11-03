import uuid

from sqlalchemy import DateTime, Float, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow


class FeatureDefinition(Base):
    """A versioned, IMMUTABLE feature definition. There is no update
    endpoint for an existing (name, version) — changing what a feature
    means creates a new version row instead, so a dataset built from
    version 3 stays reproducible forever even after someone adds version 4.
    See app.timeseries.features for the kinds this can reference."""

    __tablename__ = "feature_definitions"
    __table_args__ = (UniqueConstraint("organization_id", "name", "version", name="uq_feature_def_org_name_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    category: Mapped[str] = mapped_column(String(30), nullable=False)  # statistical|temporal|distributional|frequency|domain
    kind: Mapped[str] = mapped_column(String(50), nullable=False)      # app.timeseries.features.FeatureKind value
    window_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    params: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    missing_data_policy: Mapped[str] = mapped_column(String(30), nullable=False, default="strict")
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class DatasetBuild(Base):
    """A record of one dataset-construction request: the exact config used
    (sensor ids, feature definitions BY VERSION, time grid, target spec) so
    the same rows can be regenerated deterministically later. The matrix
    itself is NOT persisted here (it can be large) — GET
    /timeseries/datasets/{id}/rows recomputes it on demand from this
    config, which is safe because feature definitions are immutable and
    raw measurements never change."""

    __tablename__ = "dataset_builds"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)

    config: Mapped[dict] = mapped_column(JSON, nullable=False)
    row_count_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    null_feature_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    background_job_id: Mapped[str | None] = mapped_column(ForeignKey("background_jobs.id"), nullable=True)

    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
