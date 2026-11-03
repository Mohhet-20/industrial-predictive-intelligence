from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, Field

from app.models.ingestion import IngestionStatus


class MeasurementRecordIn(BaseModel):
    """Canonical single-record ingestion format. Every field is required
    except unit — unit is optional per-record because it is usually fixed
    per-sensor, but when present it is validated against the sensor's
    declared unit (a mismatch is a rejected record, not a silent coercion).
    """

    asset_code: str = Field(min_length=1, max_length=100)
    sensor_code: str = Field(min_length=1, max_length=100)
    timestamp: datetime
    value: float
    unit: Optional[str] = None


class IngestionRequest(BaseModel):
    """`records` is intentionally `list[dict]`, not `list[MeasurementRecordIn]`.

    If we required every record to already match MeasurementRecordIn, a
    single malformed record (wrong type, missing field) would fail request
    validation and reject the ENTIRE batch with a 422 before the service
    layer ever saw it — silently discarding the good records alongside the
    bad one. Instead the service parses each record individually so a
    malformed record becomes one reported error, and every well-formed
    sibling in the same batch still gets ingested.
    """

    data_source_name: str = Field(min_length=1, max_length=200)
    records: List[dict] = Field(min_length=1, max_length=20000)


class RecordError(BaseModel):
    index: int
    error: str
    record: Any


class IngestionResult(BaseModel):
    batch_id: str
    status: IngestionStatus
    records_submitted: int
    records_accepted: int
    records_rejected: int
    records_duplicate: int
    errors: List[RecordError] = Field(default_factory=list)


class IngestionBatchResponse(BaseModel):
    id: str
    status: IngestionStatus
    data_source_id: str
    records_submitted: int
    records_accepted: int
    records_rejected: int
    records_duplicate: int
    created_at: datetime
    completed_at: Optional[datetime]

    model_config = {"from_attributes": True}
