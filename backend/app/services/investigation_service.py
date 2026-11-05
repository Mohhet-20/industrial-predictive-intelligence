"""Investigation workflow service.

This is a HUMAN-ORIENTED ORGANIZATIONAL layer. It never writes to, nor
reads for the purpose of overwriting, any of the real evidence tables it
references (Anomaly, DegradationSignal, DataQualityIssue, ForecastRun,
ComponentVersion, GovernanceEvent, ShiftAnalysisRun) — every function here
either creates rows in the investigation_* tables or performs a read-only
existence/ownership check against another table before linking to it.

Status transitions never happen automatically — every InvestigationStatusEvent
is the direct result of an explicit, authenticated API call. Nothing in this
module (or anywhere else in this codebase) closes or resolves an
investigation on a timer, a webhook, or a background job.
"""
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.models.condition import DegradationSignal, ForecastRun
from app.models.data_quality import DataQualityIssue
from app.models.intelligence import Anomaly
from app.models.investigation import (
    Investigation,
    InvestigationAnnotation,
    InvestigationAnnotationType,
    InvestigationEvidenceLink,
    InvestigationEvidenceType,
    InvestigationRelation,
    InvestigationStatus,
    InvestigationStatusEvent,
)
from app.models.mlops import ComponentVersion, GovernanceEvent, ShiftAnalysisRun
from app.models.scenario import ScenarioRun
from app.models.sensor import Sensor
from app.models.user import User
from app.services.timeseries_service import get_sensor_in_org

# ---------------------------------------------------------------------------
# Status lifecycle — mirrors app.services.anomaly_service.ALLOWED_TRANSITIONS
# and app.services.condition_service.CONDITION_ALLOWED_TRANSITIONS exactly in
# spirit: an explicit table, reachable only through transition_status below,
# never inferred or defaulted anywhere else.
# ---------------------------------------------------------------------------
INVESTIGATION_ALLOWED_TRANSITIONS: dict[InvestigationStatus, set[InvestigationStatus]] = {
    InvestigationStatus.OPEN: {InvestigationStatus.UNDER_REVIEW, InvestigationStatus.AWAITING_CONTEXT, InvestigationStatus.RESOLVED, InvestigationStatus.CLOSED},
    InvestigationStatus.UNDER_REVIEW: {InvestigationStatus.AWAITING_CONTEXT, InvestigationStatus.RESOLVED, InvestigationStatus.CLOSED, InvestigationStatus.OPEN},
    InvestigationStatus.AWAITING_CONTEXT: {InvestigationStatus.UNDER_REVIEW, InvestigationStatus.RESOLVED, InvestigationStatus.CLOSED, InvestigationStatus.OPEN},
    InvestigationStatus.RESOLVED: {InvestigationStatus.UNDER_REVIEW, InvestigationStatus.CLOSED},  # reopenable
    InvestigationStatus.CLOSED: {InvestigationStatus.UNDER_REVIEW},  # reopenable, mirrors AnomalyStatus.FALSE_POSITIVE -> UNDER_REVIEW
}

# evidence_type -> (model class, whether the row has its own organization_id
# column to filter on). Every referenceable kind used to validate a link
# before it is created — see attach_evidence.
_EVIDENCE_MODELS: dict[InvestigationEvidenceType, type] = {
    InvestigationEvidenceType.ANOMALY: Anomaly,
    InvestigationEvidenceType.DEGRADATION_SIGNAL: DegradationSignal,
    InvestigationEvidenceType.DATA_QUALITY_ISSUE: DataQualityIssue,
    InvestigationEvidenceType.FORECAST_RUN: ForecastRun,
    InvestigationEvidenceType.COMPONENT_VERSION: ComponentVersion,
    InvestigationEvidenceType.GOVERNANCE_EVENT: GovernanceEvent,
    InvestigationEvidenceType.SHIFT_ANALYSIS_RUN: ShiftAnalysisRun,
    InvestigationEvidenceType.SENSOR: Sensor,
    # A ScenarioRun IS a durable, id-addressable row — linked by
    # reference like every other kind above, never snapshotted. Its
    # organization_id is real; it has no sensor_id column of its own (a
    # MULTI_SENSOR run touches several sensors — see its `results.per_sensor`
    # / `results.provenance`), so _validate_evidence_reference's
    # getattr(row, "sensor_id", None) fallback correctly returns None here,
    # the same as it does for ComponentVersion/GovernanceEvent/ShiftAnalysisRun.
    InvestigationEvidenceType.SCENARIO_RESULT: ScenarioRun,
}


def get_asset_in_org(db: Session, *, asset_id: str, organization_id: str):
    from app.models.asset import Asset

    asset = db.query(Asset).filter(Asset.id == asset_id, Asset.organization_id == organization_id).first()
    if asset is None:
        raise NotFoundError(f"Asset '{asset_id}' not found.")
    return asset


def open_investigation(
    db: Session,
    *,
    organization_id: str,
    user: User,
    asset_id: str,
    title: str,
    description: Optional[str],
    primary_sensor_id: Optional[str],
    time_window_start: Optional[datetime],
    time_window_end: Optional[datetime],
) -> Investigation:
    get_asset_in_org(db, asset_id=asset_id, organization_id=organization_id)
    if primary_sensor_id:
        sensor = get_sensor_in_org(db, sensor_id=primary_sensor_id, organization_id=organization_id)
        if sensor.asset_id != asset_id:
            raise ValidationFailedError(f"Sensor '{primary_sensor_id}' does not belong to asset '{asset_id}'.")
    if time_window_start and time_window_end and time_window_start >= time_window_end:
        raise ValidationFailedError("time_window_start must be before time_window_end.")

    investigation = Investigation(
        organization_id=organization_id,
        asset_id=asset_id,
        primary_sensor_id=primary_sensor_id,
        title=title,
        description=description,
        time_window_start=time_window_start,
        time_window_end=time_window_end,
        status=InvestigationStatus.OPEN,
        opened_by_user_id=user.id,
    )
    db.add(investigation)
    db.flush()
    db.add(InvestigationStatusEvent(investigation_id=investigation.id, changed_by_user_id=user.id, from_status=None, to_status=InvestigationStatus.OPEN, notes="Investigation opened."))
    db.commit()
    db.refresh(investigation)
    return investigation


def get_investigation(db: Session, *, organization_id: str, investigation_id: str) -> Investigation:
    investigation = db.query(Investigation).filter(Investigation.id == investigation_id, Investigation.organization_id == organization_id).first()
    if investigation is None:
        raise NotFoundError(f"Investigation '{investigation_id}' not found.")
    return investigation


def list_investigations(
    db: Session,
    *,
    organization_id: str,
    asset_id: Optional[str],
    status: Optional[InvestigationStatus],
    limit: int,
    offset: int,
):
    query = db.query(Investigation).filter(Investigation.organization_id == organization_id)
    if asset_id:
        query = query.filter(Investigation.asset_id == asset_id)
    if status:
        query = query.filter(Investigation.status == status)
    query = query.order_by(Investigation.opened_at.desc())
    total = query.count()
    return query.offset(offset).limit(limit).all(), total


def transition_status(
    db: Session, *, organization_id: str, investigation_id: str, user: User, to_status: InvestigationStatus, notes: Optional[str]
) -> Investigation:
    investigation = get_investigation(db, organization_id=organization_id, investigation_id=investigation_id)
    allowed = INVESTIGATION_ALLOWED_TRANSITIONS.get(investigation.status, set())
    if to_status not in allowed:
        raise ValidationFailedError(
            f"Cannot transition an investigation from '{investigation.status.value}' to '{to_status.value}'. "
            f"Allowed: {sorted(s.value for s in allowed)}"
        )
    db.add(InvestigationStatusEvent(investigation_id=investigation.id, changed_by_user_id=user.id, from_status=investigation.status, to_status=to_status, notes=notes))
    investigation.status = to_status  # the only field a status transition ever changes on the Investigation row itself
    db.commit()
    db.refresh(investigation)
    return investigation


def list_status_history(db: Session, *, organization_id: str, investigation_id: str) -> list[InvestigationStatusEvent]:
    investigation = get_investigation(db, organization_id=organization_id, investigation_id=investigation_id)
    return list(investigation.status_events)


# ---------------------------------------------------------------------------
# Evidence graph
# ---------------------------------------------------------------------------


def _validate_evidence_reference(db: Session, *, organization_id: str, evidence_type: InvestigationEvidenceType, evidence_id: str) -> Optional[str]:
    """Confirms the referenced row exists in this organization and returns
    its sensor_id where the model has one (for denormalization), else None.
    Raises NotFoundError if the row doesn't exist or belongs to another org
    — this is the enforcement point that stops an investigation from ever
    linking to another tenant's data."""
    model = _EVIDENCE_MODELS[evidence_type]
    row = db.query(model).filter(model.id == evidence_id, model.organization_id == organization_id).first()
    if row is None:
        raise NotFoundError(f"{evidence_type.value} '{evidence_id}' not found in this organization.")
    return getattr(row, "sensor_id", None) if evidence_type != InvestigationEvidenceType.SENSOR else row.id


def attach_evidence(
    db: Session,
    *,
    organization_id: str,
    user: User,
    investigation_id: str,
    evidence_type: InvestigationEvidenceType,
    evidence_id: Optional[str],
    snapshot: Optional[dict[str, Any]],
    relation_note: Optional[str],
) -> InvestigationEvidenceLink:
    investigation = get_investigation(db, organization_id=organization_id, investigation_id=investigation_id)

    if evidence_type == InvestigationEvidenceType.CHANGE_CANDIDATE:
        if evidence_id is not None:
            raise ValidationFailedError("change_candidate evidence has no durable id — pass evidence_id=null and a snapshot instead.")
        if not snapshot:
            raise ValidationFailedError("change_candidate evidence requires a snapshot (the candidate payload from GET /timeseries/sensors/{id}/change-candidates).")
        sensor_id = snapshot.get("sensor_id")
        if sensor_id:
            get_sensor_in_org(db, sensor_id=sensor_id, organization_id=organization_id)
    else:
        if not evidence_id:
            raise ValidationFailedError(f"evidence_id is required for evidence_type '{evidence_type.value}'.")
        if snapshot is not None:
            raise ValidationFailedError(f"snapshot is only accepted for evidence_type 'change_candidate', not '{evidence_type.value}' — that evidence is looked up live by reference instead.")
        existing = (
            db.query(InvestigationEvidenceLink)
            .filter(
                InvestigationEvidenceLink.investigation_id == investigation.id,
                InvestigationEvidenceLink.evidence_type == evidence_type,
                InvestigationEvidenceLink.evidence_id == evidence_id,
            )
            .first()
        )
        if existing is not None:
            raise ConflictError(f"This {evidence_type.value} is already linked to this investigation.")
        sensor_id = _validate_evidence_reference(db, organization_id=organization_id, evidence_type=evidence_type, evidence_id=evidence_id)

    link = InvestigationEvidenceLink(
        organization_id=organization_id,
        investigation_id=investigation.id,
        evidence_type=evidence_type,
        evidence_id=evidence_id,
        sensor_id=sensor_id,
        snapshot=snapshot if evidence_type == InvestigationEvidenceType.CHANGE_CANDIDATE else None,
        relation_note=relation_note,
        linked_by_user_id=user.id,
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    return link


def list_evidence(db: Session, *, organization_id: str, investigation_id: str) -> list[InvestigationEvidenceLink]:
    investigation = get_investigation(db, organization_id=organization_id, investigation_id=investigation_id)
    return list(investigation.evidence_links)


def remove_evidence(db: Session, *, organization_id: str, investigation_id: str, link_id: str) -> None:
    investigation = get_investigation(db, organization_id=organization_id, investigation_id=investigation_id)
    link = db.query(InvestigationEvidenceLink).filter(InvestigationEvidenceLink.id == link_id, InvestigationEvidenceLink.investigation_id == investigation.id).first()
    if link is None:
        raise NotFoundError(f"Evidence link '{link_id}' not found on this investigation.")
    db.delete(link)
    db.commit()


# ---------------------------------------------------------------------------
# Human-in-the-loop knowledge capture
# ---------------------------------------------------------------------------


def add_annotation(
    db: Session,
    *,
    organization_id: str,
    user: User,
    investigation_id: str,
    annotation_type: InvestigationAnnotationType,
    content: str,
    follow_up_required: bool,
) -> InvestigationAnnotation:
    investigation = get_investigation(db, organization_id=organization_id, investigation_id=investigation_id)
    annotation = InvestigationAnnotation(
        organization_id=organization_id,
        investigation_id=investigation.id,
        author_user_id=user.id,
        annotation_type=annotation_type,
        content=content,
        follow_up_required=follow_up_required,
    )
    db.add(annotation)
    db.commit()
    db.refresh(annotation)
    return annotation


def list_annotations(db: Session, *, organization_id: str, investigation_id: str) -> list[InvestigationAnnotation]:
    investigation = get_investigation(db, organization_id=organization_id, investigation_id=investigation_id)
    return list(investigation.annotations)


# ---------------------------------------------------------------------------
# Related investigations
# ---------------------------------------------------------------------------


def link_related_investigation(
    db: Session, *, organization_id: str, user: User, investigation_id: str, related_investigation_id: str, note: Optional[str]
) -> InvestigationRelation:
    if investigation_id == related_investigation_id:
        raise ValidationFailedError("An investigation cannot be related to itself.")
    investigation = get_investigation(db, organization_id=organization_id, investigation_id=investigation_id)
    related = get_investigation(db, organization_id=organization_id, investigation_id=related_investigation_id)
    existing = (
        db.query(InvestigationRelation)
        .filter(InvestigationRelation.investigation_id == investigation.id, InvestigationRelation.related_investigation_id == related.id)
        .first()
    )
    if existing is not None:
        raise ConflictError("These investigations are already linked.")
    relation = InvestigationRelation(organization_id=organization_id, investigation_id=investigation.id, related_investigation_id=related.id, note=note, linked_by_user_id=user.id)
    db.add(relation)
    db.commit()
    db.refresh(relation)
    return relation


def list_related_investigations(db: Session, *, organization_id: str, investigation_id: str) -> list[dict[str, Any]]:
    """Symmetric read: returns relations in either storage direction so a
    relation recorded from A->B is visible from B's investigation page too
    — normalized so that, from the caller's point of view, `investigation_id`
    is always THIS investigation and `related_investigation_id` is always
    the OTHER one, regardless of which direction it was originally stored
    in. (A bug caught in code review: returning the raw rows
    for a "backward" relation would show investigation_id/related_
    investigation_id swapped relative to what was actually asked for —
    correct in the database, misleading to a client rendering "related
    investigations of B".)"""
    investigation = get_investigation(db, organization_id=organization_id, investigation_id=investigation_id)
    forward = db.query(InvestigationRelation).filter(InvestigationRelation.investigation_id == investigation.id).all()
    backward = db.query(InvestigationRelation).filter(InvestigationRelation.related_investigation_id == investigation.id).all()

    normalized: list[dict[str, Any]] = []
    for r in forward:
        normalized.append({"id": r.id, "investigation_id": r.investigation_id, "related_investigation_id": r.related_investigation_id, "note": r.note, "linked_by_user_id": r.linked_by_user_id, "created_at": r.created_at})
    for r in backward:
        normalized.append({"id": r.id, "investigation_id": r.related_investigation_id, "related_investigation_id": r.investigation_id, "note": r.note, "linked_by_user_id": r.linked_by_user_id, "created_at": r.created_at})
    return normalized
