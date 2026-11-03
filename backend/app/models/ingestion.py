import enum
import uuid

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, utcnow


class IngestionStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"          # all records accepted
    SUCCEEDED_WITH_ERRORS = "succeeded_with_errors"  # some records rejected, batch still processed
    FAILED = "failed"                # the batch could not be processed at all


class IngestionBatch(Base):
    """One ingestion attempt. Tracks provenance and outcome counts so a
    user can answer "what happened to the file I uploaded" without digging
    through logs."""

    __tablename__ = "ingestion_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    data_source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.id"), nullable=False)
    submitted_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    status: Mapped[IngestionStatus] = mapped_column(
        Enum(IngestionStatus, native_enum=False, length=30), nullable=False, default=IngestionStatus.PENDING
    )

    records_submitted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_accepted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_duplicate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # List of {index, error, record} for rejected records — capped in the
    # service layer so a catastrophically malformed file doesn't produce a
    # multi-megabyte error blob.
    errors: Mapped[list | None] = mapped_column(JSON, nullable=True)

    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)
