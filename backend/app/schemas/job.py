from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel

from app.models.job import JobStatus, JobType


class JobResponse(BaseModel):
    id: str
    job_type: JobType
    status: JobStatus
    progress: float
    result: Optional[dict[str, Any]]
    error_message: Optional[str]
    correlation_id: Optional[str]
    created_at: datetime
    started_at: Optional[datetime]
    finished_at: Optional[datetime]

    model_config = {"from_attributes": True}
