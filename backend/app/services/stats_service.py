from datetime import datetime
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.measurement import SensorMeasurement
from app.schemas.measurement import SensorSummaryStats


def summary_stats_for_sensor(
    db: Session, *, sensor_id: str, start_time: Optional[datetime] = None, end_time: Optional[datetime] = None
) -> SensorSummaryStats:
    query = select(
        func.count(SensorMeasurement.id),
        func.min(SensorMeasurement.value),
        func.max(SensorMeasurement.value),
        func.avg(SensorMeasurement.value),
        func.min(SensorMeasurement.timestamp),
        func.max(SensorMeasurement.timestamp),
    ).where(SensorMeasurement.sensor_id == sensor_id)

    if start_time is not None:
        query = query.where(SensorMeasurement.timestamp >= start_time)
    if end_time is not None:
        query = query.where(SensorMeasurement.timestamp <= end_time)

    count, min_v, max_v, avg_v, first_ts, last_ts = db.execute(query).one()

    std_dev = None
    if count and count > 1:
        # SQLite has no native STDDEV; compute in Python for the values in
        # range. Fine at foundation scale — flagged as a scaling
        # consideration if sensor histories grow into the millions of rows.
        values_query = select(SensorMeasurement.value).where(SensorMeasurement.sensor_id == sensor_id)
        if start_time is not None:
            values_query = values_query.where(SensorMeasurement.timestamp >= start_time)
        if end_time is not None:
            values_query = values_query.where(SensorMeasurement.timestamp <= end_time)
        values = db.execute(values_query).scalars().all()
        if len(values) > 1:
            mean = sum(values) / len(values)
            variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
            std_dev = variance ** 0.5

    return SensorSummaryStats(
        sensor_id=sensor_id,
        count=count or 0,
        min_value=min_v,
        max_value=max_v,
        mean_value=avg_v,
        std_dev=std_dev,
        first_timestamp=first_ts,
        last_timestamp=last_ts,
    )
