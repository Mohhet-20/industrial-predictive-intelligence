import uuid

from sqlalchemy import DateTime, Float, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, utcnow


class SensorMeasurement(Base):
    """A single raw observation. Immutable once written: nothing in this
    codebase updates a measurement's value after ingestion. Corrections, if
    ever needed, would arrive as a new ingestion batch with clear
    provenance, never as an in-place edit — this preserves an honest audit
    trail of what the sensor actually reported.

    A UNIQUE constraint on (sensor_id, timestamp) is the dedup mechanism:
    re-ingesting the same (sensor, timestamp) pair is treated as a duplicate
    (see IngestionService), not a silent overwrite.
    """

    __tablename__ = "sensor_measurements"
    __table_args__ = (
        UniqueConstraint("sensor_id", "timestamp", name="uq_measurement_sensor_ts"),
        Index("ix_measurement_sensor_ts", "sensor_id", "timestamp"),
        Index("ix_measurement_asset_ts", "asset_id", "timestamp"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # Denormalized asset_id alongside sensor_id: lets asset-level time-range
    # queries avoid a join against millions of rows. Set from sensor.asset_id
    # at write time and never changed independently.
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), nullable=False)
    sensor_id: Mapped[str] = mapped_column(ForeignKey("sensors.id", ondelete="CASCADE"), nullable=False)

    timestamp: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)

    ingestion_batch_id: Mapped[str | None] = mapped_column(
        ForeignKey("ingestion_batches.id", ondelete="SET NULL"), nullable=True, index=True
    )
    ingested_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
