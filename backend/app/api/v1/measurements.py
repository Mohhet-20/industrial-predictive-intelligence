from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import organization_scope
from app.core.errors import AuthorizationError, NotFoundError, ValidationFailedError
from app.db.session import get_db
from app.models.measurement import SensorMeasurement
from app.models.sensor import Sensor
from app.schemas.common import Page
from app.schemas.measurement import MeasurementResponse, SensorSummaryStats
from app.services import stats_service

router = APIRouter(tags=["measurements"])

MAX_RANGE = timedelta(days=366)


def _assert_sensors_in_org(db: Session, sensor_ids: list[str], org_id: str) -> list[Sensor]:
    sensors = (
        db.query(Sensor)
        .join(Sensor.asset)
        .filter(Sensor.id.in_(sensor_ids))
        .all()
    )
    found_ids = {s.id for s in sensors}
    missing = set(sensor_ids) - found_ids
    if missing:
        raise NotFoundError(f"Unknown sensor id(s): {', '.join(sorted(missing))}")
    for s in sensors:
        if s.asset.organization_id != org_id:
            raise AuthorizationError("One or more sensors do not belong to your organization.")
    return sensors


@router.get("/measurements", response_model=Page[MeasurementResponse])
def query_measurements(
    sensor_ids: list[str] = Query(..., min_length=1, description="Repeat the param for multiple sensors."),
    start_time: datetime = Query(...),
    end_time: datetime = Query(...),
    limit: int = Query(default=5000, ge=1, le=50000),
    offset: int = Query(default=0, ge=0),
    org_id: str = Depends(organization_scope),
    db: Session = Depends(get_db),
) -> Page[MeasurementResponse]:
    """Time-range measurement retrieval. A range is REQUIRED — this
    foundation never serves an unbounded "everything for this sensor" query,
    per the storage design in ARCHITECTURE.md."""
    if end_time <= start_time:
        raise ValidationFailedError("end_time must be after start_time.")
    if end_time - start_time > MAX_RANGE:
        raise ValidationFailedError(f"Requested range exceeds the maximum of {MAX_RANGE.days} days.")

    _assert_sensors_in_org(db, sensor_ids, org_id)

    query = db.query(SensorMeasurement).filter(
        SensorMeasurement.sensor_id.in_(sensor_ids),
        SensorMeasurement.timestamp >= start_time,
        SensorMeasurement.timestamp <= end_time,
    )
    total = query.count()
    rows = query.order_by(SensorMeasurement.timestamp.asc()).offset(offset).limit(limit).all()
    items = [MeasurementResponse.model_validate(r) for r in rows]
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get("/stats/sensors/{sensor_id}", response_model=SensorSummaryStats)
def sensor_summary_stats(
    sensor_id: str,
    start_time: datetime | None = Query(default=None),
    end_time: datetime | None = Query(default=None),
    org_id: str = Depends(organization_scope),
    db: Session = Depends(get_db),
) -> SensorSummaryStats:
    _assert_sensors_in_org(db, [sensor_id], org_id)
    return stats_service.summary_stats_for_sensor(db, sensor_id=sensor_id, start_time=start_time, end_time=end_time)
