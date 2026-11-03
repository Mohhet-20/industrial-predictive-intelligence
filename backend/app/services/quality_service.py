"""Data-quality checks. These NEVER modify sensor_measurements — they only
ever produce DataQualityIssue rows that reference the data they're about.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.data_quality import DataQualityIssue, DataQualityIssueSeverity, DataQualityIssueType
from app.models.measurement import SensorMeasurement
from app.models.sensor import Sensor
from app.schemas.data_quality import DataQualitySummary

FUTURE_WARN_THRESHOLD = timedelta(minutes=5)
GAP_FACTOR = 3.0  # a gap is flagged when the interval exceeds N x the expected sampling interval


def build_issue(
    *,
    organization_id: str,
    asset_id: str,
    sensor_id: Optional[str],
    issue_type: DataQualityIssueType,
    severity: DataQualityIssueSeverity,
    period_start: datetime,
    period_end: datetime,
    description: str,
    details: Optional[dict] = None,
    ingestion_batch_id: Optional[str] = None,
) -> DataQualityIssue:
    return DataQualityIssue(
        organization_id=organization_id,
        asset_id=asset_id,
        sensor_id=sensor_id,
        issue_type=issue_type,
        severity=severity,
        period_start=period_start,
        period_end=period_end,
        description=description,
        details=details,
        ingestion_batch_id=ingestion_batch_id,
    )


def check_out_of_range(sensor: Sensor, value: float, timestamp: datetime) -> Optional[dict]:
    if sensor.expected_min is None and sensor.expected_max is None:
        return None
    if sensor.expected_min is not None and value < sensor.expected_min:
        return {
            "description": f"Value {value} is below the expected minimum {sensor.expected_min} {sensor.unit}.",
            "details": {"value": value, "expected_min": sensor.expected_min},
        }
    if sensor.expected_max is not None and value > sensor.expected_max:
        return {
            "description": f"Value {value} is above the expected maximum {sensor.expected_max} {sensor.unit}.",
            "details": {"value": value, "expected_max": sensor.expected_max},
        }
    return None


def check_future_timestamp(timestamp: datetime, now: datetime) -> Optional[dict]:
    skew = timestamp - now
    if skew > FUTURE_WARN_THRESHOLD:
        return {
            "description": f"Timestamp is {skew} ahead of ingestion time — possible clock skew.",
            "details": {"skew_seconds": skew.total_seconds()},
        }
    return None


def scan_gaps_for_sensor(
    db: Session, *, organization_id: str, sensor: Sensor, ingestion_batch_id: Optional[str] = None
) -> list[DataQualityIssue]:
    """Full-history gap scan for one sensor. Dedups against issues already
    recorded for the exact same period so repeated ingestion runs don't
    pile up duplicate gap rows."""
    if not sensor.expected_interval_seconds:
        return []

    timestamps = db.execute(
        select(SensorMeasurement.timestamp)
        .where(SensorMeasurement.sensor_id == sensor.id)
        .order_by(SensorMeasurement.timestamp.asc())
    ).scalars().all()

    if len(timestamps) < 2:
        return []

    threshold = timedelta(seconds=sensor.expected_interval_seconds * GAP_FACTOR)
    new_issues: list[DataQualityIssue] = []

    existing_periods = set(
        db.execute(
            select(DataQualityIssue.period_start, DataQualityIssue.period_end).where(
                DataQualityIssue.sensor_id == sensor.id,
                DataQualityIssue.issue_type == DataQualityIssueType.DATA_GAP,
            )
        ).all()
    )

    for prev_ts, next_ts in zip(timestamps, timestamps[1:]):
        gap = next_ts - prev_ts
        if gap > threshold:
            key = (prev_ts, next_ts)
            if key in existing_periods:
                continue
            new_issues.append(
                build_issue(
                    organization_id=organization_id,
                    asset_id=sensor.asset_id,
                    sensor_id=sensor.id,
                    issue_type=DataQualityIssueType.DATA_GAP,
                    severity=DataQualityIssueSeverity.WARNING,
                    period_start=prev_ts,
                    period_end=next_ts,
                    description=(
                        f"No data for {gap} between {prev_ts.isoformat()} and {next_ts.isoformat()} "
                        f"(expected a reading every {sensor.expected_interval_seconds:.0f}s)."
                    ),
                    details={"gap_seconds": gap.total_seconds(), "expected_interval_seconds": sensor.expected_interval_seconds},
                    ingestion_batch_id=ingestion_batch_id,
                )
            )
    return new_issues


def list_issues(
    db: Session,
    *,
    organization_id: str,
    asset_id: Optional[str] = None,
    sensor_id: Optional[str] = None,
    severity: Optional[DataQualityIssueSeverity] = None,
    limit: int = 200,
    offset: int = 0,
):
    query = db.query(DataQualityIssue).filter(DataQualityIssue.organization_id == organization_id)
    if asset_id:
        query = query.filter(DataQualityIssue.asset_id == asset_id)
    if sensor_id:
        query = query.filter(DataQualityIssue.sensor_id == sensor_id)
    if severity:
        query = query.filter(DataQualityIssue.severity == severity)
    total = query.count()
    items = (
        query.order_by(DataQualityIssue.detected_at.desc()).offset(offset).limit(limit).all()
    )
    return items, total


def summarize_for_asset(db: Session, *, organization_id: str, asset_id: str) -> DataQualitySummary:
    issues = (
        db.query(DataQualityIssue)
        .filter(DataQualityIssue.organization_id == organization_id, DataQualityIssue.asset_id == asset_id)
        .all()
    )
    by_type: dict[str, int] = {}
    critical = warning = info = 0
    for issue in issues:
        by_type[issue.issue_type.value] = by_type.get(issue.issue_type.value, 0) + 1
        if issue.severity == DataQualityIssueSeverity.CRITICAL:
            critical += 1
        elif issue.severity == DataQualityIssueSeverity.WARNING:
            warning += 1
        else:
            info += 1
    return DataQualitySummary(
        asset_id=asset_id,
        total_issues=len(issues),
        critical_count=critical,
        warning_count=warning,
        info_count=info,
        issues_by_type=by_type,
    )
