import enum
import uuid

from sqlalchemy import DateTime, Enum, Float, ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class JobType(str, enum.Enum):
    BATCH_INGESTION = "batch_ingestion"
    DATA_QUALITY_SCAN = "data_quality_scan"
    DATASET_BUILD = "dataset_build"
    DETECTOR_TRAINING = "detector_training"
    ANOMALY_DETECTION_RUN = "anomaly_detection_run"
    SCENARIO_SIMULATION_RUN = "scenario_simulation_run"


class BackgroundJob(Base):
    """A real, queryable unit of async work. status/progress/result/error
    are all genuine fields updated by the job runner — the System Health
    and Jobs UI read this table directly, nothing is fabricated."""

    __tablename__ = "background_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)

    job_type: Mapped[JobType] = mapped_column(Enum(JobType, native_enum=False, length=40), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False, length=20), nullable=False, default=JobStatus.QUEUED
    )

    progress: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)  # 0.0–1.0
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retry_count: Mapped[int] = mapped_column(default=0, nullable=False)
    max_retries: Mapped[int] = mapped_column(default=0, nullable=False)

    # An optional caller-supplied key (e.g. a ModelRun or
    # DatasetBuild id) identifying the underlying unit of work. submit()
    # uses this to detect an already-QUEUED/RUNNING job for the same
    # (organization_id, job_type, dedup_key) and return its id instead of
    # starting a second, redundant execution — see app/jobs/runner.py.
    # retry_count/max_retries above are schema fields only: no code path
    # currently auto-retries a failed job (see ARCHITECTURE note in
    # app/jobs/runner.py for why blind retry is not implemented here).
    dedup_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)
