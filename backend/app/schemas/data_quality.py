from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel

from app.models.data_quality import DataQualityIssueSeverity, DataQualityIssueType


class DataQualityIssueResponse(BaseModel):
    id: str
    asset_id: str
    sensor_id: Optional[str]
    issue_type: DataQualityIssueType
    severity: DataQualityIssueSeverity
    period_start: datetime
    period_end: datetime
    description: str
    details: Optional[dict[str, Any]]
    detected_at: datetime

    model_config = {"from_attributes": True}


class DataQualitySummary(BaseModel):
    asset_id: str
    total_issues: int
    critical_count: int
    warning_count: int
    info_count: int
    issues_by_type: dict[str, int]
