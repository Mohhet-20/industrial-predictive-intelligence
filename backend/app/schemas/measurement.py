from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class MeasurementResponse(BaseModel):
    id: str
    asset_id: str
    sensor_id: str
    timestamp: datetime
    value: float
    ingestion_batch_id: Optional[str]

    model_config = {"from_attributes": True}


class MeasurementQuery(BaseModel):
    sensor_ids: list[str] = Field(min_length=1)
    start_time: datetime
    end_time: datetime
    limit: int = Field(default=5000, ge=1, le=50000)
    offset: int = Field(default=0, ge=0)


class SensorSummaryStats(BaseModel):
    sensor_id: str
    count: int
    min_value: Optional[float]
    max_value: Optional[float]
    mean_value: Optional[float]
    std_dev: Optional[float]
    first_timestamp: Optional[datetime]
    last_timestamp: Optional[datetime]
