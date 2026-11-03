import uuid

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class Asset(TimestampMixin, Base):
    """A piece of monitored equipment (a pump, a compressor, a turbine...)."""

    __tablename__ = "assets"
    __table_args__ = (UniqueConstraint("organization_id", "code", name="uq_asset_org_code"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)

    code: Mapped[str] = mapped_column(String(100), nullable=False)  # short stable identifier, e.g. "PUMP-07"
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(100), nullable=False)  # e.g. "centrifugal_pump"
    location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    organization: Mapped["Organization"] = relationship(back_populates="assets")
    sensors: Mapped[list["Sensor"]] = relationship(back_populates="asset", cascade="all, delete-orphan")
