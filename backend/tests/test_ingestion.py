from datetime import datetime, timedelta, timezone

from app.models.data_quality import DataQualityIssue, DataQualityIssueType
from app.models.ingestion import IngestionStatus
from app.models.measurement import SensorMeasurement
from app.services import ingestion_service

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _record(asset_code="PUMP-01", sensor_code="temp", ts=BASE, value=42.0, unit="celsius"):
    return {"asset_code": asset_code, "sensor_code": sensor_code, "timestamp": ts.isoformat(), "value": value, "unit": unit}


def test_all_valid_records_accepted(db_session, org_and_admin, sample_asset_and_sensor):
    org, admin = org_and_admin
    result = ingestion_service.ingest_records(
        db_session,
        organization_id=org.id,
        data_source_name="test-source",
        raw_records=[_record(ts=BASE + timedelta(minutes=i * 5), value=50.0 + i) for i in range(5)],
        submitted_by_user_id=admin.id,
    )
    assert result.status == IngestionStatus.SUCCEEDED
    assert result.records_accepted == 5
    assert result.records_rejected == 0
    assert db_session.query(SensorMeasurement).count() == 5


def test_unknown_asset_is_rejected_not_fatal(db_session, org_and_admin, sample_asset_and_sensor):
    org, admin = org_and_admin
    records = [_record(), _record(asset_code="DOES-NOT-EXIST")]
    result = ingestion_service.ingest_records(
        db_session, organization_id=org.id, data_source_name="s", raw_records=records, submitted_by_user_id=admin.id
    )
    assert result.records_accepted == 1
    assert result.records_rejected == 1
    assert result.status == IngestionStatus.SUCCEEDED_WITH_ERRORS
    assert "Unknown asset_code" in result.errors[0].error


def test_unknown_sensor_is_rejected(db_session, org_and_admin, sample_asset_and_sensor):
    org, admin = org_and_admin
    result = ingestion_service.ingest_records(
        db_session,
        organization_id=org.id,
        data_source_name="s",
        raw_records=[_record(sensor_code="does-not-exist")],
        submitted_by_user_id=admin.id,
    )
    assert result.records_accepted == 0
    assert result.records_rejected == 1
    assert result.status == IngestionStatus.FAILED


def test_malformed_record_does_not_fail_whole_batch(db_session, org_and_admin, sample_asset_and_sensor):
    org, admin = org_and_admin
    good = _record(value=55.0)
    malformed = {"asset_code": "PUMP-01", "sensor_code": "temp"}  # missing timestamp and value
    result = ingestion_service.ingest_records(
        db_session, organization_id=org.id, data_source_name="s", raw_records=[good, malformed], submitted_by_user_id=admin.id
    )
    assert result.records_accepted == 1
    assert result.records_rejected == 1


def test_missing_value_creates_quality_issue(db_session, org_and_admin, sample_asset_and_sensor):
    org, admin = org_and_admin
    raw = {"asset_code": "PUMP-01", "sensor_code": "temp", "timestamp": BASE.isoformat(), "value": None}
    ingestion_service.ingest_records(
        db_session, organization_id=org.id, data_source_name="s", raw_records=[raw], submitted_by_user_id=admin.id
    )
    issues = db_session.query(DataQualityIssue).filter(DataQualityIssue.issue_type == DataQualityIssueType.MISSING_VALUE).all()
    assert len(issues) == 1


def test_naive_timestamp_rejected(db_session, org_and_admin, sample_asset_and_sensor):
    org, admin = org_and_admin
    raw = {"asset_code": "PUMP-01", "sensor_code": "temp", "timestamp": "2026-01-01T00:00:00", "value": 50.0}
    result = ingestion_service.ingest_records(
        db_session, organization_id=org.id, data_source_name="s", raw_records=[raw], submitted_by_user_id=admin.id
    )
    assert result.records_accepted == 0
    assert "timezone" in result.errors[0].error.lower()


def test_implausible_future_timestamp_rejected(db_session, org_and_admin, sample_asset_and_sensor):
    org, admin = org_and_admin
    far_future = datetime.now(timezone.utc) + timedelta(days=30)
    result = ingestion_service.ingest_records(
        db_session,
        organization_id=org.id,
        data_source_name="s",
        raw_records=[_record(ts=far_future)],
        submitted_by_user_id=admin.id,
    )
    assert result.records_accepted == 0
    assert "future" in result.errors[0].error.lower()


def test_mild_future_timestamp_accepted_and_flagged(db_session, org_and_admin, sample_asset_and_sensor):
    org, admin = org_and_admin
    soon = datetime.now(timezone.utc) + timedelta(minutes=20)
    result = ingestion_service.ingest_records(
        db_session,
        organization_id=org.id,
        data_source_name="s",
        raw_records=[_record(ts=soon)],
        submitted_by_user_id=admin.id,
    )
    assert result.records_accepted == 1
    issues = db_session.query(DataQualityIssue).filter(DataQualityIssue.issue_type == DataQualityIssueType.TIMESTAMP_FUTURE).all()
    assert len(issues) == 1


def test_duplicate_within_batch_detected(db_session, org_and_admin, sample_asset_and_sensor):
    org, admin = org_and_admin
    records = [_record(value=50.0), _record(value=99.0)]  # same sensor+timestamp, different value
    result = ingestion_service.ingest_records(
        db_session, organization_id=org.id, data_source_name="s", raw_records=records, submitted_by_user_id=admin.id
    )
    assert result.records_accepted == 1
    assert result.records_duplicate == 1
    assert db_session.query(SensorMeasurement).count() == 1


def test_duplicate_against_previously_stored_detected(db_session, org_and_admin, sample_asset_and_sensor):
    org, admin = org_and_admin
    ingestion_service.ingest_records(
        db_session, organization_id=org.id, data_source_name="s", raw_records=[_record(value=50.0)], submitted_by_user_id=admin.id
    )
    result = ingestion_service.ingest_records(
        db_session, organization_id=org.id, data_source_name="s", raw_records=[_record(value=51.0)], submitted_by_user_id=admin.id
    )
    assert result.records_accepted == 0
    assert result.records_duplicate == 1
    # The original value must be preserved — duplicates never overwrite.
    stored = db_session.query(SensorMeasurement).one()
    assert stored.value == 50.0


def test_out_of_order_batch_flagged_but_all_accepted(db_session, org_and_admin, sample_asset_and_sensor):
    org, admin = org_and_admin
    records = [
        _record(ts=BASE + timedelta(minutes=10), value=51.0),
        _record(ts=BASE, value=50.0),  # earlier timestamp submitted second
    ]
    result = ingestion_service.ingest_records(
        db_session, organization_id=org.id, data_source_name="s", raw_records=records, submitted_by_user_id=admin.id
    )
    assert result.records_accepted == 2
    issues = db_session.query(DataQualityIssue).filter(
        DataQualityIssue.issue_type == DataQualityIssueType.TIMESTAMP_OUT_OF_ORDER
    ).all()
    assert len(issues) == 1


def test_out_of_range_value_accepted_and_flagged(db_session, org_and_admin, sample_asset_and_sensor):
    # sample sensor's expected range is [0, 100]
    org, admin = org_and_admin
    result = ingestion_service.ingest_records(
        db_session,
        organization_id=org.id,
        data_source_name="s",
        raw_records=[_record(value=150.0)],
        submitted_by_user_id=admin.id,
    )
    assert result.records_accepted == 1  # out-of-range is stored, not rejected
    issues = db_session.query(DataQualityIssue).filter(DataQualityIssue.issue_type == DataQualityIssueType.OUT_OF_RANGE).all()
    assert len(issues) == 1
    assert issues[0].severity.value == "warning"


def test_unit_mismatch_rejected_and_flagged(db_session, org_and_admin, sample_asset_and_sensor):
    org, admin = org_and_admin
    result = ingestion_service.ingest_records(
        db_session,
        organization_id=org.id,
        data_source_name="s",
        raw_records=[_record(unit="fahrenheit")],
        submitted_by_user_id=admin.id,
    )
    assert result.records_accepted == 0
    assert result.records_rejected == 1
    issues = db_session.query(DataQualityIssue).filter(DataQualityIssue.issue_type == DataQualityIssueType.UNSUPPORTED_UNIT).all()
    assert len(issues) == 1


def test_non_finite_value_rejected(db_session, org_and_admin, sample_asset_and_sensor):
    org, admin = org_and_admin
    raw = {"asset_code": "PUMP-01", "sensor_code": "temp", "timestamp": BASE.isoformat(), "value": float("inf")}
    result = ingestion_service.ingest_records(
        db_session, organization_id=org.id, data_source_name="s", raw_records=[raw], submitted_by_user_id=admin.id
    )
    assert result.records_accepted == 0
    assert "finite" in result.errors[0].error.lower()


def test_raw_measurements_never_mutated_by_requery(db_session, org_and_admin, sample_asset_and_sensor):
    """A second ingestion attempt covering the same (sensor, timestamp)
    with a different value must not change what's stored — this is the
    immutability guarantee the domain model depends on."""
    org, admin = org_and_admin
    ingestion_service.ingest_records(
        db_session, organization_id=org.id, data_source_name="s", raw_records=[_record(value=10.0)], submitted_by_user_id=admin.id
    )
    ingestion_service.ingest_records(
        db_session, organization_id=org.id, data_source_name="s", raw_records=[_record(value=999.0)], submitted_by_user_id=admin.id
    )
    values = [m.value for m in db_session.query(SensorMeasurement).all()]
    assert values == [10.0]
