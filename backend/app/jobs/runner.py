"""A practical in-process background job runner.

This is deliberately NOT Celery/RQ/a message broker: the async work in this
foundation (batch ingestion of a few thousand records, a bounded detection
run) finishes in seconds to low minutes, and every job's outcome only needs
to be visible within this one process's database. Running a broker for that
would be the "distributed infrastructure to look advanced" the design
explicitly warns against.

What it does provide, for real: a queryable BackgroundJob row per unit of
work with genuine status/progress/result/error, a bounded worker pool so
jobs don't all run at once, correlation-ID propagation into job logs, a
per-job execution timeout so a hung job fails explicitly instead
of leaving a BackgroundJob stuck RUNNING forever, and duplicate-submission
protection keyed by an optional caller-supplied dedup_key.

On retries: BackgroundJob has retry_count/max_retries columns, but this
runner does not implement blind automatic retry. Retrying is only safe when
a job's side effects are idempotent, and none of the three registered job
types are: batch ingestion could double-count records if the first attempt
partially wrote before failing (ingestion's own dedup logic guards against
duplicate *measurements*, not duplicate *batches*), a dataset build recomputes
a specific DatasetBuild row, and a detection run scores a specific ModelRun.
Automatically resubmitting any of these on failure risks masking a real,
recurring problem (e.g. a corrupt sensor config) as transient. The safe,
honest retry path is a human or the calling API resubmitting a fresh
request after seeing the FAILED status and error_message — which is what
duplicate-protection below is actually for: making a resubmission-after-
failure safe rather than silently racing an in-flight one.

The upgrade path (documented in ARCHITECTURE.md) is to swap this runner for
Celery/RQ behind the same `JobService` interface once job volume or
duration justifies a broker — nothing above this module would need to
change.
"""
import asyncio
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import and_

from app.core.logging import get_logger, log_with_fields, set_correlation_id
from app.db.session import SessionLocal
from app.models.job import BackgroundJob, JobStatus, JobType

logger = get_logger("app.jobs.runner")

JobFn = Callable[[str, Callable[[float], None], dict[str, Any]], Awaitable[dict[str, Any]]]

_ACTIVE_STATUSES = (JobStatus.QUEUED, JobStatus.RUNNING)


@dataclass
class _Registration:
    fn: JobFn


class DuplicateJobError(Exception):
    """Raised (and translated to a 409 by the API layer) when submit() is
    called with a dedup_key that already has an active job."""

    def __init__(self, existing_job_id: str):
        super().__init__(f"An active job already exists for this work: {existing_job_id}")
        self.existing_job_id = existing_job_id


class JobRunner:
    def __init__(self, max_concurrency: int = 4, default_timeout_seconds: int = 300):
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._registry: dict[JobType, _Registration] = {}
        self._tasks: set[asyncio.Task] = set()
        self._default_timeout_seconds = default_timeout_seconds

    def register(self, job_type: JobType, fn: JobFn) -> None:
        self._registry[job_type] = _Registration(fn=fn)

    def submit(
        self,
        *,
        organization_id: str,
        job_type: JobType,
        payload: Optional[dict[str, Any]] = None,
        correlation_id: Optional[str] = None,
        dedup_key: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
    ) -> str:
        db = SessionLocal()
        try:
            if dedup_key is not None:
                existing = (
                    db.query(BackgroundJob)
                    .filter(
                        and_(
                            BackgroundJob.organization_id == organization_id,
                            BackgroundJob.job_type == job_type,
                            BackgroundJob.dedup_key == dedup_key,
                            BackgroundJob.status.in_(_ACTIVE_STATUSES),
                        )
                    )
                    .order_by(BackgroundJob.created_at.desc())
                    .first()
                )
                if existing is not None:
                    raise DuplicateJobError(existing.id)

            job = BackgroundJob(
                organization_id=organization_id,
                job_type=job_type,
                status=JobStatus.QUEUED,
                correlation_id=correlation_id,
                dedup_key=dedup_key,
            )
            db.add(job)
            db.commit()
            db.refresh(job)
            job_id = job.id
        finally:
            db.close()

        task = asyncio.create_task(
            self._run(job_id, job_type, payload or {}, correlation_id, timeout_seconds or self._default_timeout_seconds)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job_id

    async def _run(
        self,
        job_id: str,
        job_type: JobType,
        payload: dict[str, Any],
        correlation_id: Optional[str],
        timeout_seconds: int,
    ) -> None:
        registration = self._registry.get(job_type)
        if registration is None:
            self._mark_failed(job_id, f"No handler registered for job type '{job_type.value}'.")
            return

        async with self._semaphore:
            if correlation_id:
                set_correlation_id(correlation_id)
            self._mark_running(job_id)

            def report_progress(fraction: float) -> None:
                self._update_progress(job_id, fraction)

            try:
                result = await asyncio.wait_for(
                    registration.fn(job_id, report_progress, payload), timeout=timeout_seconds
                )
                self._mark_succeeded(job_id, result)
            except asyncio.TimeoutError:
                log_with_fields(
                    logger, 40, "job timed out", job_id=job_id, job_type=job_type.value, timeout_seconds=timeout_seconds
                )
                self._mark_failed(
                    job_id,
                    f"Job exceeded its {timeout_seconds}s timeout and was force-failed. "
                    "No partial result was committed by this runner, but check the job's own "
                    "domain row (ModelRun/DatasetBuild/etc.) for any state it wrote before timing out.",
                )
            except Exception as exc:  # noqa: BLE001
                log_with_fields(logger, 40, "job failed", job_id=job_id, error=str(exc))
                self._mark_failed(job_id, f"{exc}\n{traceback.format_exc(limit=3)}")

    def _mark_running(self, job_id: str) -> None:
        db = SessionLocal()
        try:
            job = db.get(BackgroundJob, job_id)
            if job:
                job.status = JobStatus.RUNNING
                job.started_at = datetime.now(timezone.utc)
                db.commit()
        finally:
            db.close()

    def _update_progress(self, job_id: str, fraction: float) -> None:
        db = SessionLocal()
        try:
            job = db.get(BackgroundJob, job_id)
            if job:
                job.progress = max(0.0, min(1.0, fraction))
                db.commit()
        finally:
            db.close()

    def _mark_succeeded(self, job_id: str, result: dict[str, Any]) -> None:
        db = SessionLocal()
        try:
            job = db.get(BackgroundJob, job_id)
            if job:
                job.status = JobStatus.SUCCEEDED
                job.progress = 1.0
                job.result = result
                job.finished_at = datetime.now(timezone.utc)
                db.commit()
        finally:
            db.close()

    def _mark_failed(self, job_id: str, error_message: str) -> None:
        db = SessionLocal()
        try:
            job = db.get(BackgroundJob, job_id)
            if job:
                job.status = JobStatus.FAILED
                job.error_message = error_message[:1000]
                job.finished_at = datetime.now(timezone.utc)
                db.commit()
        finally:
            db.close()


def _build_default_runner() -> JobRunner:
    # Imported lazily (not at module top) to avoid a circular import — a
    # handful of service modules import from app.jobs.runner, and
    # app.core.config has no reason to depend on the jobs package.
    from app.core.config import get_settings

    return JobRunner(default_timeout_seconds=get_settings().job_timeout_seconds)


job_runner = _build_default_runner()
