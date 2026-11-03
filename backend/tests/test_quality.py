from datetime import datetime, timedelta, timezone

from app.models.data_quality import DataQualityIssueType
from app.services import ingestion_service, quality_service

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _record(ts, value=50.0):
    return {"asset_code": "PUMP-01", "sensor_code": "temp", "timestamp": ts.isoformat(), "value": value, "unit": "celsius"}


def test_gap_detected_when_interval_exceeded(db_session, org_and_admin, sample_asset_and_sensor):
    org, admin = org_and_admin
    # sensor expects a reading every 300s; leave a ~2 hour gap between two readings.
    records = [_record(BASE), _record(BASE + timedelta(hours=2))]
    ingestion_service.ingest_records(
        db_session, organization_id=org.id, data_source_name="s", raw_records=records, submitted_by_user_id=admin.id
    )
    from app.models.data_quality import DataQualityIssue

    gaps = db_session.query(DataQualityIssue).filter(DataQualityIssue.issue_type == DataQualityIssueType.DATA_GAP).all()
    assert len(gaps) == 1
    assert gaps[0].details["gap_seconds"] == timedelta(hours=2).total_seconds()


def test_no_gap_flagged_for_normal_cadence(db_session, org_and_admin, sample_asset_and_sensor):
    org, admin = org_and_admin
    records = [_record(BASE + timedelta(minutes=i * 5)) for i in range(10)]
    ingestion_service.ingest_records(
        db_session, organization_id=org.id, data_source_name="s", raw_records=records, submitted_by_user_id=admin.id
    )
    from app.models.data_quality import DataQualityIssue

    gaps = db_session.query(DataQualityIssue).filter(DataQualityIssue.issue_type == DataQualityIssueType.DATA_GAP).all()
    assert len(gaps) == 0


def test_gap_scan_does_not_duplicate_on_rerun(db_session, org_and_admin, sample_asset_and_sensor):
    org, admin = org_and_admin
    asset, sensor = sample_asset_and_sensor
    records = [_record(BASE), _record(BASE + timedelta(hours=2))]
    ingestion_service.ingest_records(
        db_session, organization_id=org.id, data_source_name="s", raw_records=records, submitted_by_user_id=admin.id
    )
    # Re-running the gap scan directly (simulating a second ingestion touching this sensor) must not duplicate the issue.
    new_issues = quality_service.scan_gaps_for_sensor(db_session, organization_id=org.id, sensor=sensor)
    assert new_issues == []


def test_summary_for_asset_counts_by_severity(db_session, org_and_admin, sample_asset_and_sensor):
    org, admin = org_and_admin
    asset, sensor = sample_asset_and_sensor
    records = [_record(BASE, value=500.0)]  # way out of range -> WARNING issue
    ingestion_service.ingest_records(
        db_session, organization_id=org.id, data_source_name="s", raw_records=records, submitted_by_user_id=admin.id
    )
    summary = quality_service.summarize_for_asset(db_session, organization_id=org.id, asset_id=asset.id)
    assert summary.total_issues >= 1
    assert summary.warning_count >= 1
