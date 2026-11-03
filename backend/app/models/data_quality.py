import enum
import uuid

from sqlalchemy import DateTime, Enum, ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, utcnow


class DataQualityIssueType(str, enum.Enum):
    MISSING_VALUE = "missing_value"
    OUT_OF_RANGE = "out_of_range"
    DUPLICATE_OBSERVATION = "duplicate_observation"
    TIMESTAMP_OUT_OF_ORDER = "timestamp_out_of_order"
    TIMESTAMP_FUTURE = "timestamp_future"
    DATA_GAP = "data_gap"
    UNSUPPORTED_UNIT = "unsupported_unit"
    MALFORMED_RECORD = "malformed_record"


class DataQualityIssueSeverity(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class DataQualityIssue(Base):
    """A derived finding about data quality. Never mutates the underlying
    measurement — this is evidence *about* the data, stored alongside it,
    not a correction applied to it.
    """

    __tablename__ = "data_quality_issues"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True)
    sensor_id: Mapped[str | None] = mapped_column(ForeignKey("sensors.id", ondelete="CASCADE"), nullable=True, index=True)

    issue_type: Mapped[DataQualityIssueType] = mapped_column(
        Enum(DataQualityIssueType, native_enum=False, length=40), nullable=False
    )
    severity: Mapped[DataQualityIssueSeverity] = mapped_column(
        Enum(DataQualityIssueSeverity, native_enum=False, length=20), nullable=False
    )

    # Time range the issue pertains to (a gap spans a range; a single bad
    # reading has period_start == period_end).
    period_start: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)

    description: Mapped[str] = mapped_column(String(500), nullable=False)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    ingestion_batch_id: Mapped[str | None] = mapped_column(
        ForeignKey("ingestion_batches.id", ondelete="SET NULL"), nullable=True
    )

    detected_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
