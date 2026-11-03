import enum
import uuid

from sqlalchemy import Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class DataSourceType(str, enum.Enum):
    SYNTHETIC = "synthetic"
    FILE_UPLOAD = "file_upload"
    API_CLIENT = "api_client"
    SIMULATOR = "simulator"


class DataSource(TimestampMixin, Base):
    """Where an ingestion batch's data actually came from — required
    provenance metadata, not optional."""

    __tablename__ = "data_sources"
    __table_args__ = (UniqueConstraint("organization_id", "name", name="uq_source_org_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    source_type: Mapped[DataSourceType] = mapped_column(
        Enum(DataSourceType, native_enum=False, length=30), nullable=False
    )
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
