import uuid

from sqlalchemy import Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class Sensor(TimestampMixin, Base):
    """A single measured channel on an Asset (e.g. bearing temperature)."""

    __tablename__ = "sensors"
    __table_args__ = (UniqueConstraint("asset_id", "code", name="uq_sensor_asset_code"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True)

    code: Mapped[str] = mapped_column(String(100), nullable=False)  # e.g. "bearing_temp_de"
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    measurement_type: Mapped[str] = mapped_column(String(100), nullable=False)  # e.g. "temperature", "vibration"
    unit: Mapped[str] = mapped_column(String(50), nullable=False)  # e.g. "celsius", "mm_s", "bar"

    # Declared valid operating range, used by the data-quality service to
    # flag out-of-range readings. Optional: not every sensor has a known
    # engineering range at demo time.
    expected_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_max: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Expected sampling cadence in seconds, used by the gap detector.
    # Optional — absent means gap detection is skipped for this sensor.
    expected_interval_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    asset: Mapped["Asset"] = relationship(back_populates="sensors")
