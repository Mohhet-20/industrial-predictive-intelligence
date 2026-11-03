"""Investigation Workflow API (Phase 6).

Same tenant-isolation and RBAC discipline as every other module in this
platform: every route is organization-scoped
(org_id = Depends(organization_scope)) and every service call re-validates
that scope server-side. Opening an investigation, transitioning its
status, attaching/removing evidence, and adding annotations require ADMIN
or ENGINEER — a VIEWER can read and follow the evidence trail but not
change investigation state, the same boundary app.api.v1.anomaly and
app.api.v1.condition already draw for their review actions."""
from fastapi import APIRouter, Depends, Query

from sqlalchemy.orm import Session

from app.core.deps import organization_scope, require_roles
from app.db.session import get_db
from app.models.investigation import InvestigationStatus
from app.models.user import User, UserRole
from app.schemas.common import Page
from app.schemas.investigation import (
    AnnotationCreateRequest,
    AnnotationResponse,
    EvidenceAttachRequest,
    EvidenceLinkResponse,
    InvestigationCreateRequest,
    InvestigationResponse,
    InvestigationStatusEventResponse,
    InvestigationStatusTransitionRequest,
    RelatedInvestigationLinkRequest,
    RelatedInvestigationResponse,
)
from app.services import investigation_service
from typing import Optional

router = APIRouter(prefix="/investigations", tags=["investigations"])

_WRITE_ROLES = (UserRole.ADMIN, UserRole.ENGINEER)


@router.post("", response_model=InvestigationResponse, status_code=201)
def create_investigation(
    payload: InvestigationCreateRequest,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    db: Session = Depends(get_db),
) -> InvestigationResponse:
    investigation = investigation_service.open_investigation(
        db, organization_id=org_id, user=user, asset_id=payload.asset_id, title=payload.title,
        description=payload.description, primary_sensor_id=payload.primary_sensor_id,
        time_window_start=payload.time_window_start, time_window_end=payload.time_window_end,
    )
    return InvestigationResponse.model_validate(investigation)


@router.get("", response_model=Page[InvestigationResponse])
def list_investigations(
    asset_id: Optional[str] = Query(default=None),
    status: Optional[InvestigationStatus] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    org_id: str = Depends(organization_scope),
    db: Session = Depends(get_db),
) -> Page[InvestigationResponse]:
    rows, total = investigation_service.list_investigations(db, organization_id=org_id, asset_id=asset_id, status=status, limit=limit, offset=offset)
    return Page(items=[InvestigationResponse.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/{investigation_id}", response_model=InvestigationResponse)
def get_investigation(investigation_id: str, org_id: str = Depends(organization_scope), db: Session = Depends(get_db)) -> InvestigationResponse:
    investigation = investigation_service.get_investigation(db, organization_id=org_id, investigation_id=investigation_id)
    return InvestigationResponse.model_validate(investigation)


@router.post("/{investigation_id}/status", response_model=InvestigationResponse)
def transition_status(
    investigation_id: str,
    payload: InvestigationStatusTransitionRequest,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    db: Session = Depends(get_db),
) -> InvestigationResponse:
    investigation = investigation_service.transition_status(db, organization_id=org_id, investigation_id=investigation_id, user=user, to_status=payload.to_status, notes=payload.notes)
    return InvestigationResponse.model_validate(investigation)


@router.get("/{investigation_id}/status-history", response_model=list[InvestigationStatusEventResponse])
def list_status_history(investigation_id: str, org_id: str = Depends(organization_scope), db: Session = Depends(get_db)) -> list[InvestigationStatusEventResponse]:
    rows = investigation_service.list_status_history(db, organization_id=org_id, investigation_id=investigation_id)
    return [InvestigationStatusEventResponse.model_validate(r) for r in rows]


@router.post("/{investigation_id}/evidence", response_model=EvidenceLinkResponse, status_code=201)
def attach_evidence(
    investigation_id: str,
    payload: EvidenceAttachRequest,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    db: Session = Depends(get_db),
) -> EvidenceLinkResponse:
    link = investigation_service.attach_evidence(
        db, organization_id=org_id, user=user, investigation_id=investigation_id, evidence_type=payload.evidence_type,
        evidence_id=payload.evidence_id, snapshot=payload.snapshot, relation_note=payload.relation_note,
    )
    return EvidenceLinkResponse.model_validate(link)


@router.get("/{investigation_id}/evidence", response_model=list[EvidenceLinkResponse])
def list_evidence(investigation_id: str, org_id: str = Depends(organization_scope), db: Session = Depends(get_db)) -> list[EvidenceLinkResponse]:
    rows = investigation_service.list_evidence(db, organization_id=org_id, investigation_id=investigation_id)
    return [EvidenceLinkResponse.model_validate(r) for r in rows]


@router.delete("/{investigation_id}/evidence/{link_id}", status_code=204)
def remove_evidence(
    investigation_id: str,
    link_id: str,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    db: Session = Depends(get_db),
) -> None:
    investigation_service.remove_evidence(db, organization_id=org_id, investigation_id=investigation_id, link_id=link_id)


@router.post("/{investigation_id}/annotations", response_model=AnnotationResponse, status_code=201)
def add_annotation(
    investigation_id: str,
    payload: AnnotationCreateRequest,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    db: Session = Depends(get_db),
) -> AnnotationResponse:
    annotation = investigation_service.add_annotation(
        db, organization_id=org_id, user=user, investigation_id=investigation_id,
        annotation_type=payload.annotation_type, content=payload.content, follow_up_required=payload.follow_up_required,
    )
    return AnnotationResponse.model_validate(annotation)


@router.get("/{investigation_id}/annotations", response_model=list[AnnotationResponse])
def list_annotations(investigation_id: str, org_id: str = Depends(organization_scope), db: Session = Depends(get_db)) -> list[AnnotationResponse]:
    rows = investigation_service.list_annotations(db, organization_id=org_id, investigation_id=investigation_id)
    return [AnnotationResponse.model_validate(r) for r in rows]


@router.post("/{investigation_id}/related", response_model=RelatedInvestigationResponse, status_code=201)
def link_related_investigation(
    investigation_id: str,
    payload: RelatedInvestigationLinkRequest,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    db: Session = Depends(get_db),
) -> RelatedInvestigationResponse:
    relation = investigation_service.link_related_investigation(
        db, organization_id=org_id, user=user, investigation_id=investigation_id, related_investigation_id=payload.related_investigation_id, note=payload.note,
    )
    return RelatedInvestigationResponse.model_validate(relation)


@router.get("/{investigation_id}/related", response_model=list[RelatedInvestigationResponse])
def list_related_investigations(investigation_id: str, org_id: str = Depends(organization_scope), db: Session = Depends(get_db)) -> list[RelatedInvestigationResponse]:
    rows = investigation_service.list_related_investigations(db, organization_id=org_id, investigation_id=investigation_id)
    return [RelatedInvestigationResponse.model_validate(r) for r in rows]
