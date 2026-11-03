"""Ingestion validation and persistence.

Design goals from the spec, made concrete here:
  - never silently corrupt data — every rejected record carries a specific
    reason, returned to the caller and stored on the IngestionBatch.
  - a malformed record never takes its well-formed siblings down with it —
    each record in a batch is parsed and validated independently.
  - duplicates (same sensor + timestamp) are detected and counted
    separately from rejections — they are not an error, they're expected
    when a client retries or re-sends overlapping windows.
  - out-of-order observations are accepted (a batch is not required to be
    chronologically sorted) — storage doesn't care about arrival order,
    only about the (sensor, timestamp) identity of each observation. A
    batch submitted out of chronological order is merely noted as an
    informational data-quality signal.
  - raw measurements are never mutated. This module only ever INSERTs into
    sensor_measurements; anything questionable about the data becomes a
    DataQualityIssue row alongside it, never an edit to the value itself.
"""
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_correlation_id, get_logger, log_with_fields
from app.models.asset import Asset
from app.models.data_quality import DataQualityIssue, DataQualityIssueSeverity, DataQualityIssueType
from app.models.data_source import DataSource, DataSourceType
from app.models.ingestion import IngestionBatch, IngestionStatus
from app.models.measurement import SensorMeasurement
from app.models.sensor import Sensor
from app.schemas.ingestion import IngestionResult, MeasurementRecordIn, RecordError
from app.services import quality_service

logger = get_logger("app.services.ingestion")

MAX_STORED_ERRORS = 200
MAX_FUTURE_SKEW = timedelta(days=1)  # beyond this, a timestamp is rejected outright as implausible
MAX_DUPLICATE_EXAMPLES = 20


def get_or_create_data_source(
    db: Session, *, organization_id: str, name: str, source_type: DataSourceType = DataSourceType.API_CLIENT
) -> DataSource:
    existing = (
        db.query(DataSource)
        .filter(DataSource.organization_id == organization_id, DataSource.name == name)
        .first()
    )
    if existing:
        return existing
    source = DataSource(organization_id=organization_id, name=name, source_type=source_type)
    db.add(source)
    db.flush()
    return source


def _load_asset_sensor_index(
    db: Session, organization_id: str
) -> tuple[dict[str, Asset], dict[tuple[str, str], Sensor]]:
    assets = db.query(Asset).filter(Asset.organization_id == organization_id).all()
    assets_by_code = {a.code: a for a in assets}

    asset_ids = [a.id for a in assets]
    sensors_by_asset_and_code: dict[tuple[str, str], Sensor] = {}
    if asset_ids:
        sensors = db.query(Sensor).filter(Sensor.asset_id.in_(asset_ids)).all()
        for s in sensors:
            sensors_by_asset_and_code[(s.asset_id, s.code)] = s
    return assets_by_code, sensors_by_asset_and_code


def _summarize_validation_error(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", []))
        parts.append(f"{loc}: {err.get('msg')}")
    return "; ".join(parts) or "Record does not match the expected schema."


def ingest_records(
    db: Session,
    *,
    organization_id: str,
    data_source_name: str,
    raw_records: list[dict[str, Any]],
    submitted_by_user_id: str | None = None,
) -> IngestionResult:
    correlation_id = get_correlation_id()
    source = get_or_create_data_source(db, organization_id=organization_id, name=data_source_name)

    batch = IngestionBatch(
        organization_id=organization_id,
        data_source_id=source.id,
        submitted_by_user_id=submitted_by_user_id,
        status=IngestionStatus.RUNNING,
        records_submitted=len(raw_records),
        correlation_id=correlation_id,
    )
    db.add(batch)
    db.flush()

    assets_by_code, sensors_by_asset_and_code = _load_asset_sensor_index(db, organization_id)

    now = datetime.now(timezone.utc)
    errors: list[RecordError] = []
    quality_issues: list[DataQualityIssue] = []
    duplicate_examples: list[dict] = []
    seen_in_batch: set[tuple[str, datetime]] = set()
    duplicate_count = 0

    candidates_by_sensor: dict[str, list[datetime]] = defaultdict(list)
    validated: list[tuple[int, MeasurementRecordIn, Sensor]] = []
    # Tracks submission-order timestamps per sensor to detect an out-of-order batch.
    submission_order_by_sensor: dict[str, list[datetime]] = defaultdict(list)

    for idx, raw in enumerate(raw_records):
        try:
            record = MeasurementRecordIn.model_validate(raw)
        except ValidationError as exc:
            errors.append(RecordError(index=idx, error=_summarize_validation_error(exc), record=raw))
            # Best-effort: if we can still resolve an asset, surface a
            # MISSING_VALUE quality issue when the value was explicitly
            # null/absent — more actionable than a bare parse error.
            asset = assets_by_code.get(raw.get("asset_code"))
            if asset is not None and ("value" not in raw or raw.get("value") is None):
                sensor = sensors_by_asset_and_code.get((asset.id, raw.get("sensor_code")))
                try:
                    period = datetime.fromisoformat(str(raw.get("timestamp"))) if raw.get("timestamp") else now
                    if period.tzinfo is None:
                        period = period.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    period = now
                quality_issues.append(
                    quality_service.build_issue(
                        organization_id=organization_id,
                        asset_id=asset.id,
                        sensor_id=sensor.id if sensor else None,
                        issue_type=DataQualityIssueType.MISSING_VALUE,
                        severity=DataQualityIssueSeverity.WARNING,
                        period_start=period,
                        period_end=period,
                        description=f"Record for sensor '{raw.get('sensor_code')}' was submitted with no value.",
                        ingestion_batch_id=batch.id,
                    )
                )
            continue

        asset = assets_by_code.get(record.asset_code)
        if asset is None:
            errors.append(RecordError(index=idx, error=f"Unknown asset_code '{record.asset_code}'.", record=raw))
            continue

        sensor = sensors_by_asset_and_code.get((asset.id, record.sensor_code))
        if sensor is None:
            errors.append(
                RecordError(
                    index=idx,
                    error=f"Unknown sensor_code '{record.sensor_code}' for asset '{record.asset_code}'.",
                    record=raw,
                )
            )
            continue

        if record.unit is not None and record.unit != sensor.unit:
            errors.append(
                RecordError(
                    index=idx,
                    error=f"Unit mismatch: record has '{record.unit}', sensor '{sensor.code}' expects '{sensor.unit}'.",
                    record=raw,
                )
            )
            quality_issues.append(
                quality_service.build_issue(
                    organization_id=organization_id,
                    asset_id=asset.id,
                    sensor_id=sensor.id,
                    issue_type=DataQualityIssueType.UNSUPPORTED_UNIT,
                    severity=DataQualityIssueSeverity.CRITICAL,
                    period_start=record.timestamp if record.timestamp.tzinfo else record.timestamp.replace(tzinfo=timezone.utc),
                    period_end=record.timestamp if record.timestamp.tzinfo else record.timestamp.replace(tzinfo=timezone.utc),
                    description=f"Submitted unit '{record.unit}' does not match sensor unit '{sensor.unit}'; record rejected.",
                    ingestion_batch_id=batch.id,
                )
            )
            continue

        if not math.isfinite(record.value):
            errors.append(RecordError(index=idx, error="Value is not a finite number.", record=raw))
            continue

        ts = record.timestamp
        if ts.tzinfo is None:
            errors.append(RecordError(index=idx, error="Timestamp is missing timezone information.", record=raw))
            continue
        ts = ts.astimezone(timezone.utc)

        if ts - now > MAX_FUTURE_SKEW:
            errors.append(
                RecordError(index=idx, error=f"Timestamp {ts.isoformat()} is implausibly far in the future.", record=raw)
            )
            continue

        dedup_key = (sensor.id, ts)
        if dedup_key in seen_in_batch:
            duplicate_count += 1
            if len(duplicate_examples) < MAX_DUPLICATE_EXAMPLES:
                duplicate_examples.append({"sensor_code": sensor.code, "timestamp": ts.isoformat()})
            continue
        seen_in_batch.add(dedup_key)

        submission_order_by_sensor[sensor.id].append(ts)
        candidates_by_sensor[sensor.id].append(ts)
        validated.append((idx, record, sensor))

        # Out-of-range check runs on every accepted-so-far record regardless
        # of DB duplicate status (checked below) — a value can be both a
        # duplicate and out of range; duplicates are simply not persisted.
        range_issue = quality_service.check_out_of_range(sensor, record.value, ts)
        if range_issue:
            quality_issues.append(
                quality_service.build_issue(
                    organization_id=organization_id,
                    asset_id=asset.id,
                    sensor_id=sensor.id,
                    issue_type=DataQualityIssueType.OUT_OF_RANGE,
                    severity=DataQualityIssueSeverity.WARNING,
                    period_start=ts,
                    period_end=ts,
                    description=range_issue["description"],
                    details=range_issue["details"],
                    ingestion_batch_id=batch.id,
                )
            )

        future_issue = quality_service.check_future_timestamp(ts, now)
        if future_issue:
            quality_issues.append(
                quality_service.build_issue(
                    organization_id=organization_id,
                    asset_id=asset.id,
                    sensor_id=sensor.id,
                    issue_type=DataQualityIssueType.TIMESTAMP_FUTURE,
                    severity=DataQualityIssueSeverity.WARNING,
                    period_start=ts,
                    period_end=ts,
                    description=future_issue["description"],
                    details=future_issue["details"],
                    ingestion_batch_id=batch.id,
                )
            )

    # Check for duplicates already persisted from a previous ingestion.
    already_stored: set[tuple[str, datetime]] = set()
    for sensor_id, timestamps in candidates_by_sensor.items():
        if not timestamps:
            continue
        rows = db.execute(
            select(SensorMeasurement.sensor_id, SensorMeasurement.timestamp).where(
                SensorMeasurement.sensor_id == sensor_id,
                SensorMeasurement.timestamp.in_(timestamps),
            )
        ).all()
        for row_sensor_id, row_ts in rows:
            ts_normalized = row_ts if row_ts.tzinfo else row_ts.replace(tzinfo=timezone.utc)
            already_stored.add((row_sensor_id, ts_normalized))

    accepted_rows: list[dict] = []
    for idx, record, sensor in validated:
        ts = record.timestamp.astimezone(timezone.utc)
        if (sensor.id, ts) in already_stored:
            duplicate_count += 1
            if len(duplicate_examples) < MAX_DUPLICATE_EXAMPLES:
                duplicate_examples.append({"sensor_code": sensor.code, "timestamp": ts.isoformat(), "previously_ingested": True})
            continue
        accepted_rows.append(
            {
                "asset_id": sensor.asset_id,
                "sensor_id": sensor.id,
                "timestamp": ts,
                "value": record.value,
                "ingestion_batch_id": batch.id,
            }
        )

    if accepted_rows:
        db.bulk_insert_mappings(SensorMeasurement, accepted_rows)

    # Out-of-order-within-submission signal: once per sensor, informational only.
    for sensor_id, ts_list in submission_order_by_sensor.items():
        if ts_list != sorted(ts_list):
            sensor = next((s for (_, s) in [(k, v) for k, v in sensors_by_asset_and_code.items()] if s.id == sensor_id), None)
            if sensor is None:
                continue
            quality_issues.append(
                quality_service.build_issue(
                    organization_id=organization_id,
                    asset_id=sensor.asset_id,
                    sensor_id=sensor_id,
                    issue_type=DataQualityIssueType.TIMESTAMP_OUT_OF_ORDER,
                    severity=DataQualityIssueSeverity.INFO,
                    period_start=min(ts_list),
                    period_end=max(ts_list),
                    description=f"Batch submitted {len(ts_list)} readings for this sensor out of chronological order.",
                    ingestion_batch_id=batch.id,
                )
            )

    if duplicate_count > 0:
        # One summary issue per batch rather than one row per duplicate.
        any_sensor = next(iter(candidates_by_sensor), None)
        if any_sensor:
            sensor = next((v for k, v in sensors_by_asset_and_code.items() if v.id == any_sensor), None)
            if sensor:
                quality_issues.append(
                    quality_service.build_issue(
                        organization_id=organization_id,
                        asset_id=sensor.asset_id,
                        sensor_id=None,
                        issue_type=DataQualityIssueType.DUPLICATE_OBSERVATION,
                        severity=DataQualityIssueSeverity.INFO,
                        period_start=now,
                        period_end=now,
                        description=f"{duplicate_count} record(s) in this batch duplicated an existing or in-batch observation.",
                        details={"duplicate_count": duplicate_count, "examples": duplicate_examples},
                        ingestion_batch_id=batch.id,
                    )
                )

    if quality_issues:
        db.add_all(quality_issues)

    batch.records_accepted = len(accepted_rows)
    batch.records_rejected = len(errors)
    batch.records_duplicate = duplicate_count
    batch.errors = [e.model_dump() for e in errors[:MAX_STORED_ERRORS]]
    batch.completed_at = datetime.now(timezone.utc)

    if errors and not accepted_rows:
        batch.status = IngestionStatus.FAILED
    elif errors:
        batch.status = IngestionStatus.SUCCEEDED_WITH_ERRORS
    else:
        batch.status = IngestionStatus.SUCCEEDED

    db.commit()
    db.refresh(batch)

    # Gap detection needs the full, now-committed history per sensor.
    gap_issues: list[DataQualityIssue] = []
    for sensor_id in candidates_by_sensor:
        sensor = next((v for k, v in sensors_by_asset_and_code.items() if v.id == sensor_id), None)
        if sensor:
            gap_issues.extend(
                quality_service.scan_gaps_for_sensor(
                    db, organization_id=organization_id, sensor=sensor, ingestion_batch_id=batch.id
                )
            )
    if gap_issues:
        db.add_all(gap_issues)
        db.commit()

    log_with_fields(
        logger,
        20,
        "ingestion batch completed",
        batch_id=batch.id,
        accepted=batch.records_accepted,
        rejected=batch.records_rejected,
        duplicate=batch.records_duplicate,
        quality_issues=len(quality_issues) + len(gap_issues),
    )

    return IngestionResult(
        batch_id=batch.id,
        status=batch.status,
        records_submitted=batch.records_submitted,
        records_accepted=batch.records_accepted,
        records_rejected=batch.records_rejected,
        records_duplicate=batch.records_duplicate,
        errors=errors[:MAX_STORED_ERRORS],
    )
