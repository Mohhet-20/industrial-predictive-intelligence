"""Pydantic schemas for the Investigation workflow API.

Mirrors app.schemas.anomaly/app.schemas.condition/app.schemas.mlops's
conventions: fully typed response models for fixed-shape rows, a loose
`dict[str, Any]` envelope only where the underlying payload genuinely
varies (evidence snapshots, whose shape depends on the evidence kind)."""
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.models.investigation import InvestigationAnnotationType, InvestigationEvidenceType, InvestigationStatus

# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class InvestigationCreateRequest(BaseModel):
    asset_id: str
    title: str = Field(min_length=1, max_length=300)
    description: Optional[str] = Field(default=None, max_length=5000)
    primary_sensor_id: Optional[str] = None
    time_window_start: Optional[datetime] = None
    time_window_end: Optional[datetime] = None


class InvestigationStatusTransitionRequest(BaseModel):
    to_status: InvestigationStatus
    notes: Optional[str] = Field(default=None, max_length=2000)


class EvidenceAttachRequest(BaseModel):
    evidence_type: InvestigationEvidenceType
    evidence_id: Optional[str] = Field(default=None, max_length=36)
    snapshot: Optional[dict[str, Any]] = None
    relation_note: Optional[str] = Field(default=None, max_length=500)


class AnnotationCreateRequest(BaseModel):
    annotation_type: InvestigationAnnotationType
    content: str = Field(min_length=1, max_length=5000)
    follow_up_required: bool = False


class RelatedInvestigationLinkRequest(BaseModel):
    related_investigation_id: str
    note: Optional[str] = Field(default=None, max_length=500)


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class InvestigationResponse(BaseModel):
    id: str
    organization_id: str
    asset_id: str
    primary_sensor_id: Optional[str]
    title: str
    description: Optional[str]
    time_window_start: Optional[datetime]
    time_window_end: Optional[datetime]
    status: InvestigationStatus
    opened_by_user_id: Optional[str]
    opened_at: datetime
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class InvestigationStatusEventResponse(BaseModel):
    id: str
    investigation_id: str
    changed_by_user_id: Optional[str]
    from_status: Optional[InvestigationStatus]
    to_status: InvestigationStatus
    notes: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}


class EvidenceLinkResponse(BaseModel):
    id: str
    investigation_id: str
    evidence_type: InvestigationEvidenceType
    evidence_id: Optional[str]
    sensor_id: Optional[str]
    snapshot: Optional[dict[str, Any]]
    relation_note: Optional[str]
    linked_by_user_id: Optional[str]
    linked_at: datetime

    model_config = {"from_attributes": True}


class AnnotationResponse(BaseModel):
    id: str
    investigation_id: str
    author_user_id: Optional[str]
    annotation_type: InvestigationAnnotationType
    content: str
    follow_up_required: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class RelatedInvestigationResponse(BaseModel):
    id: str
    investigation_id: str
    related_investigation_id: str
    note: Optional[str]
    linked_by_user_id: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}
