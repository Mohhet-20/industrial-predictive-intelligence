"""Shared read path from sensor_measurements into pandas for the
time-series intelligence layer.

Every function in this package takes a `pandas.Series` (DatetimeIndex,
tz-aware UTC, float values) rather than talking to the database directly —
this keeps profiling/trend/feature/split logic testable with plain
in-memory series and independent of SQLAlchemy. `data_access` is the one
module that touches the DB.
"""
from datetime import datetime

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.measurement import SensorMeasurement

# Hard ceiling on rows pulled into memory for a single analysis call. This
# is the concrete backstop for "do not load years of data into memory by
# default" — callers that need more must resample/aggregate server-side or
# page through /measurements instead of asking the intelligence layer to
# swallow everything at once.
MAX_ROWS = 200_000


class TooManyObservationsError(Exception):
    """Raised when a requested range would pull more than MAX_ROWS
    measurements into memory. Callers should narrow the time range or
    resample instead."""

    def __init__(self, count_estimate: int):
        self.count_estimate = count_estimate
        super().__init__(
            f"Requested range contains too many observations ({count_estimate} > {MAX_ROWS}). "
            "Narrow the time range or use the resampling API instead."
        )


def count_measurements(db: Session, *, sensor_id: str, start: datetime, end: datetime) -> int:
    from sqlalchemy import func

    return db.execute(
        select(func.count(SensorMeasurement.id)).where(
            SensorMeasurement.sensor_id == sensor_id,
            SensorMeasurement.timestamp >= start,
            SensorMeasurement.timestamp <= end,
        )
    ).scalar_one()


def load_measurement_series(
    db: Session, *, sensor_id: str, start: datetime, end: datetime, enforce_bound: bool = True
) -> pd.Series:
    """Loads raw measurements for one sensor in [start, end] as a
    pandas.Series indexed by UTC timestamp, sorted ascending.

    Raises TooManyObservationsError rather than silently truncating —
    truncating a time-series without saying so would make every downstream
    statistic quietly wrong.
    """
    if enforce_bound:
        estimate = count_measurements(db, sensor_id=sensor_id, start=start, end=end)
        if estimate > MAX_ROWS:
            raise TooManyObservationsError(estimate)

    rows = db.execute(
        select(SensorMeasurement.timestamp, SensorMeasurement.value)
        .where(
            SensorMeasurement.sensor_id == sensor_id,
            SensorMeasurement.timestamp >= start,
            SensorMeasurement.timestamp <= end,
        )
        .order_by(SensorMeasurement.timestamp.asc())
    ).all()

    if not rows:
        return pd.Series(dtype="float64", index=pd.DatetimeIndex([], tz="UTC"))

    index = pd.DatetimeIndex([r.timestamp for r in rows])
    if index.tz is None:
        index = index.tz_localize("UTC")
    else:
        index = index.tz_convert("UTC")
    return pd.Series([r.value for r in rows], index=index, dtype="float64")


def load_multi_sensor_series(
    db: Session, *, sensor_ids: list[str], start: datetime, end: datetime
) -> dict[str, pd.Series]:
    total = sum(count_measurements(db, sensor_id=sid, start=start, end=end) for sid in sensor_ids)
    if total > MAX_ROWS:
        raise TooManyObservationsError(total)
    return {
        sensor_id: load_measurement_series(db, sensor_id=sensor_id, start=start, end=end, enforce_bound=False)
        for sensor_id in sensor_ids
    }
