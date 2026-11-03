"""Real, measured operational data for GET /system/metrics.

Every number here comes from a real table this organization actually has
rows in (or the in-process request counters in app.core.metrics) — nothing
is a placeholder, a random number, or an "example" value. Where a
measurement isn't meaningful (e.g. average job duration when zero jobs have
finished), the field is `None`, not a fabricated 0.
"""
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core import metrics
from app.models.data_quality import DataQualityIssue
from app.models.ingestion import IngestionBatch, IngestionStatus
from app.models.intelligence import Anomaly, AnomalySeverityTier, ModelRun, ModelRunStatus
from app.models.job import BackgroundJob, JobStatus


def _ensure_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def get_operational_metrics(db: Session, *, organization_id: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    lookback = now - timedelta(days=7)

    # --- background jobs (real BackgroundJob rows) ---
    job_rows = (
        db.query(BackgroundJob.job_type, BackgroundJob.status, func.count(BackgroundJob.id))
        .filter(BackgroundJob.organization_id == organization_id, BackgroundJob.created_at >= lookback)
        .group_by(BackgroundJob.job_type, BackgroundJob.status)
        .all()
    )
    jobs_by_type: dict[str, dict[str, int]] = {}
    for job_type, status, count in job_rows:
        jobs_by_type.setdefault(job_type.value, {})[status.value] = count

    finished_jobs = (
        db.query(BackgroundJob)
        .filter(
            BackgroundJob.organization_id == organization_id,
            BackgroundJob.created_at >= lookback,
            BackgroundJob.status == JobStatus.SUCCEEDED,
            BackgroundJob.started_at.isnot(None),
            BackgroundJob.finished_at.isnot(None),
        )
        .all()
    )
    durations = [
        (_ensure_utc(j.finished_at) - _ensure_utc(j.started_at)).total_seconds() for j in finished_jobs
    ]
    avg_job_duration = round(sum(durations) / len(durations), 2) if durations else None

    # --- ingestion (real IngestionBatch rows) ---
    ingestion_rows = (
        db.query(IngestionBatch.status, func.count(IngestionBatch.id))
        .filter(IngestionBatch.organization_id == organization_id, IngestionBatch.created_at >= lookback)
        .group_by(IngestionBatch.status)
        .all()
    )
    ingestion_by_status = {status.value: count for status, count in ingestion_rows}

    # --- data quality (real DataQualityIssue rows) ---
    dq_rows = (
        db.query(DataQualityIssue.severity, func.count(DataQualityIssue.id))
        .filter(DataQualityIssue.organization_id == organization_id, DataQualityIssue.detected_at >= lookback)
        .group_by(DataQualityIssue.severity)
        .all()
    )
    dq_by_severity = {severity.value: count for severity, count in dq_rows}

    # --- anomaly detection (real ModelRun / Anomaly rows) ---
    run_rows = (
        db.query(ModelRun.status, func.count(ModelRun.id))
        .filter(ModelRun.organization_id == organization_id, ModelRun.created_at >= lookback)
        .group_by(ModelRun.status)
        .all()
    )
    runs_by_status = {status.value: count for status, count in run_rows}
    failed_runs = db.query(ModelRun).filter(
        ModelRun.organization_id == organization_id,
        ModelRun.created_at >= lookback,
        ModelRun.status == ModelRunStatus.FAILED,
    ).count()

    anomaly_rows = (
        db.query(Anomaly.severity_tier, func.count(Anomaly.id))
        .join(ModelRun, Anomaly.model_run_id == ModelRun.id)
        .filter(Anomaly.organization_id == organization_id, Anomaly.created_at >= lookback)
        .group_by(Anomaly.severity_tier)
        .all()
    )
    anomalies_by_severity = {tier.value: count for tier, count in anomaly_rows}

    return {
        "window": {"lookback_days": 7, "as_of": now.isoformat()},
        "request_metrics": metrics.get_request_metrics(),
        "process": {"uptime_seconds": metrics.get_process_uptime_seconds()},
        "background_jobs": {
            "by_type_and_status": jobs_by_type,
            "avg_duration_seconds_succeeded": avg_job_duration,
        },
        "ingestion": {"batches_by_status": ingestion_by_status},
        "data_quality": {"issues_detected_by_severity": dq_by_severity},
        "anomaly_detection": {
            "runs_by_status": runs_by_status,
            "failed_runs": failed_runs,
            "anomalies_by_severity": anomalies_by_severity,
        },
        "note": (
            "All figures are computed from this organization's real rows over the "
            "trailing 7 days, plus this process's own in-memory request counters. "
            "No synthetic or example values. A count of 0 or a missing status key "
            "means exactly that: zero real rows matched, not 'not measured'."
        ),
    }
