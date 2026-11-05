"""Intelligence Lifecycle, Registry & Governance API.

Every route is organization-scoped and every service call re-validates
that scope server-side, the same tenant-isolation discipline as
app.api.v1.anomaly / app.api.v1.condition. Registration, evaluation,
validation, approval, activation, rollback, retirement, retraining, and
scoring all require ADMIN or ENGINEER; approval and activation additionally
require ADMIN, since those are the steps that put a version into (or take
it out of) production use. Everything else is read-only for any
authenticated org member.
"""
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import organization_scope, require_roles
from app.db.session import get_db
from app.models.mlops import ComponentType
from app.models.user import User, UserRole
from app.schemas.common import Page
from app.schemas.mlops import (
    ActivateVersionRequest,
    ApproveVersionRequest,
    ComponentCreateRequest,
    ComponentResponse,
    ComponentVersionResponse,
    EvaluateRequest,
    GovernanceEventResponse,
    MonitoringResponse,
    RetireVersionRequest,
    RetrainingRequestCreateRequest,
    RetrainingRequestResponse,
    RollbackRequest,
    ScoreRequest,
    ScoreResponse,
    ShiftAnalysisCreateRequest,
    ShiftAnalysisRunResponse,
    ValidateVersionRequest,
    VersionRegisterRequest,
)
from app.services import mlops_service

router = APIRouter(prefix="/mlops", tags=["mlops"])

_WRITE_ROLES = (UserRole.ADMIN, UserRole.ENGINEER)
_PRODUCTION_ROLES = (UserRole.ADMIN,)


# ---------------------------------------------------------------------------
# Components & versions
# ---------------------------------------------------------------------------


@router.post("/components", response_model=ComponentResponse, status_code=201)
def register_component(
    payload: ComponentCreateRequest, org_id: str = Depends(organization_scope), user: User = Depends(require_roles(*_WRITE_ROLES)), db: Session = Depends(get_db),
) -> ComponentResponse:
    component = mlops_service.register_component(db, organization_id=org_id, user=user, component_type=payload.component_type, name=payload.name, description=payload.description)
    return ComponentResponse.model_validate(component)


@router.get("/components", response_model=Page[ComponentResponse])
def list_components(
    component_type: Optional[ComponentType] = Query(default=None), limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0),
    org_id: str = Depends(organization_scope), db: Session = Depends(get_db),
) -> Page[ComponentResponse]:
    rows, total = mlops_service.list_components(db, organization_id=org_id, component_type=component_type, limit=limit, offset=offset)
    return Page(items=[ComponentResponse.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/components/{component_id}", response_model=ComponentResponse)
def get_component(component_id: str, org_id: str = Depends(organization_scope), db: Session = Depends(get_db)) -> ComponentResponse:
    return ComponentResponse.model_validate(mlops_service.get_component(db, organization_id=org_id, component_id=component_id))


@router.post("/components/{component_id}/versions", response_model=ComponentVersionResponse, status_code=201)
def register_version(
    component_id: str, payload: VersionRegisterRequest, org_id: str = Depends(organization_scope), user: User = Depends(require_roles(*_WRITE_ROLES)), db: Session = Depends(get_db),
) -> ComponentVersionResponse:
    version = mlops_service.register_version(
        db, organization_id=org_id, user=user, component_id=component_id, version_kind=payload.version_kind,
        model_version_id=payload.model_version_id, forecast_model_id=payload.forecast_model_id, heuristic_code_version=payload.heuristic_code_version,
    )
    return ComponentVersionResponse.model_validate(version)


@router.get("/components/{component_id}/versions", response_model=Page[ComponentVersionResponse])
def list_component_versions(
    component_id: str, limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0), org_id: str = Depends(organization_scope), db: Session = Depends(get_db),
) -> Page[ComponentVersionResponse]:
    rows, total = mlops_service.list_component_versions(db, organization_id=org_id, component_id=component_id, limit=limit, offset=offset)
    return Page(items=[ComponentVersionResponse.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/versions/{component_version_id}", response_model=ComponentVersionResponse)
def get_component_version(component_version_id: str, org_id: str = Depends(organization_scope), db: Session = Depends(get_db)) -> ComponentVersionResponse:
    return ComponentVersionResponse.model_validate(mlops_service.get_component_version(db, organization_id=org_id, component_version_id=component_version_id))


# ---------------------------------------------------------------------------
# Evaluation & validation gates
# ---------------------------------------------------------------------------


@router.post("/versions/{component_version_id}/evaluate", response_model=ComponentVersionResponse)
def record_evaluation(
    component_version_id: str, payload: EvaluateRequest, org_id: str = Depends(organization_scope), user: User = Depends(require_roles(*_WRITE_ROLES)), db: Session = Depends(get_db),
) -> ComponentVersionResponse:
    return ComponentVersionResponse.model_validate(mlops_service.record_evaluation(db, organization_id=org_id, user=user, component_version_id=component_version_id))


@router.post("/versions/{component_version_id}/validate", response_model=ComponentVersionResponse)
def validate_version(
    component_version_id: str, payload: ValidateVersionRequest, org_id: str = Depends(organization_scope), user: User = Depends(require_roles(*_WRITE_ROLES)), db: Session = Depends(get_db),
) -> ComponentVersionResponse:
    return ComponentVersionResponse.model_validate(mlops_service.validate_version(db, organization_id=org_id, user=user, component_version_id=component_version_id, notes=payload.notes, evidence=payload.evidence))


@router.post("/versions/{component_version_id}/approve", response_model=ComponentVersionResponse)
def approve_version(
    component_version_id: str, payload: ApproveVersionRequest, org_id: str = Depends(organization_scope), user: User = Depends(require_roles(*_PRODUCTION_ROLES)), db: Session = Depends(get_db),
) -> ComponentVersionResponse:
    return ComponentVersionResponse.model_validate(mlops_service.approve_version(db, organization_id=org_id, user=user, component_version_id=component_version_id, notes=payload.notes))


# ---------------------------------------------------------------------------
# Activation & rollback
# ---------------------------------------------------------------------------


@router.post("/components/{component_id}/activate", response_model=ComponentVersionResponse)
def activate_version(
    component_id: str, payload: ActivateVersionRequest, org_id: str = Depends(organization_scope), user: User = Depends(require_roles(*_PRODUCTION_ROLES)), db: Session = Depends(get_db),
) -> ComponentVersionResponse:
    return ComponentVersionResponse.model_validate(mlops_service.activate_version(db, organization_id=org_id, user=user, component_id=component_id, component_version_id=payload.component_version_id))


@router.post("/components/{component_id}/rollback", response_model=ComponentVersionResponse)
def rollback_component(
    component_id: str, payload: RollbackRequest, org_id: str = Depends(organization_scope), user: User = Depends(require_roles(*_PRODUCTION_ROLES)), db: Session = Depends(get_db),
) -> ComponentVersionResponse:
    return ComponentVersionResponse.model_validate(mlops_service.rollback_component(db, organization_id=org_id, user=user, component_id=component_id, target_component_version_id=payload.target_component_version_id))


@router.post("/versions/{component_version_id}/retire", response_model=ComponentVersionResponse)
def retire_version(
    component_version_id: str, payload: RetireVersionRequest, org_id: str = Depends(organization_scope), user: User = Depends(require_roles(*_PRODUCTION_ROLES)), db: Session = Depends(get_db),
) -> ComponentVersionResponse:
    return ComponentVersionResponse.model_validate(mlops_service.retire_version(db, organization_id=org_id, user=user, component_version_id=component_version_id, reason=payload.reason))


# ---------------------------------------------------------------------------
# Safe scoring inference contract
# ---------------------------------------------------------------------------


@router.post("/components/{component_id}/score", response_model=ScoreResponse)
def score_via_active_component(
    component_id: str, payload: ScoreRequest, org_id: str = Depends(organization_scope), user: User = Depends(require_roles(*_WRITE_ROLES)), db: Session = Depends(get_db),
) -> ScoreResponse:
    kwargs = {k: v for k, v in payload.model_dump().items() if v is not None}
    result = mlops_service.score_via_active_component(db, organization_id=org_id, user=user, component_id=component_id, request_kwargs=kwargs)
    return ScoreResponse(data=result)


# ---------------------------------------------------------------------------
# Controlled retraining workflow
# ---------------------------------------------------------------------------


@router.post("/components/{component_id}/retraining-requests", response_model=RetrainingRequestResponse, status_code=201)
def request_retraining(
    component_id: str, payload: RetrainingRequestCreateRequest, org_id: str = Depends(organization_scope), user: User = Depends(require_roles(*_WRITE_ROLES)), db: Session = Depends(get_db),
) -> RetrainingRequestResponse:
    return RetrainingRequestResponse.model_validate(mlops_service.request_retraining(db, organization_id=org_id, user=user, component_id=component_id, reason=payload.reason, config=payload.config))


@router.post("/retraining-requests/{request_id}/execute", response_model=RetrainingRequestResponse)
def execute_retraining_request(
    request_id: str, org_id: str = Depends(organization_scope), user: User = Depends(require_roles(*_WRITE_ROLES)), db: Session = Depends(get_db),
) -> RetrainingRequestResponse:
    return RetrainingRequestResponse.model_validate(mlops_service.execute_retraining_request(db, organization_id=org_id, user=user, request_id=request_id))


@router.get("/retraining-requests", response_model=Page[RetrainingRequestResponse])
def list_retraining_requests(
    component_id: Optional[str] = Query(default=None), limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0),
    org_id: str = Depends(organization_scope), db: Session = Depends(get_db),
) -> Page[RetrainingRequestResponse]:
    rows, total = mlops_service.list_retraining_requests(db, organization_id=org_id, component_id=component_id, limit=limit, offset=offset)
    return Page(items=[RetrainingRequestResponse.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/retraining-requests/{request_id}", response_model=RetrainingRequestResponse)
def get_retraining_request(request_id: str, org_id: str = Depends(organization_scope), db: Session = Depends(get_db)) -> RetrainingRequestResponse:
    return RetrainingRequestResponse.model_validate(mlops_service.get_retraining_request(db, organization_id=org_id, request_id=request_id))


# ---------------------------------------------------------------------------
# Shift analysis
# ---------------------------------------------------------------------------


@router.post("/shift-analysis", response_model=ShiftAnalysisRunResponse, status_code=201)
def run_shift_analysis(
    payload: ShiftAnalysisCreateRequest, org_id: str = Depends(organization_scope), user: User = Depends(require_roles(*_WRITE_ROLES)), db: Session = Depends(get_db),
) -> ShiftAnalysisRunResponse:
    run = mlops_service.run_shift_analysis(
        db, organization_id=org_id, user=user, sensor_id=payload.sensor_id, component_id=payload.component_id,
        reference_start=payload.reference_start, reference_end=payload.reference_end, comparison_start=payload.comparison_start, comparison_end=payload.comparison_end,
    )
    return ShiftAnalysisRunResponse.model_validate(run)


@router.get("/shift-analysis", response_model=Page[ShiftAnalysisRunResponse])
def list_shift_analysis_runs(
    sensor_id: Optional[str] = Query(default=None), limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0),
    org_id: str = Depends(organization_scope), db: Session = Depends(get_db),
) -> Page[ShiftAnalysisRunResponse]:
    rows, total = mlops_service.list_shift_analysis_runs(db, organization_id=org_id, sensor_id=sensor_id, limit=limit, offset=offset)
    return Page(items=[ShiftAnalysisRunResponse.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/shift-analysis/{run_id}", response_model=ShiftAnalysisRunResponse)
def get_shift_analysis_run(run_id: str, org_id: str = Depends(organization_scope), db: Session = Depends(get_db)) -> ShiftAnalysisRunResponse:
    return ShiftAnalysisRunResponse.model_validate(mlops_service.get_shift_analysis_run(db, organization_id=org_id, run_id=run_id))


# ---------------------------------------------------------------------------
# Governance audit log & monitoring
# ---------------------------------------------------------------------------


@router.get("/governance-events", response_model=Page[GovernanceEventResponse])
def list_governance_events(
    component_id: Optional[str] = Query(default=None), limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0),
    org_id: str = Depends(organization_scope), db: Session = Depends(get_db),
) -> Page[GovernanceEventResponse]:
    rows, total = mlops_service.list_governance_events(db, organization_id=org_id, component_id=component_id, limit=limit, offset=offset)
    return Page(items=[GovernanceEventResponse.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/monitoring", response_model=MonitoringResponse)
def get_mlops_monitoring(org_id: str = Depends(organization_scope), db: Session = Depends(get_db)) -> MonitoringResponse:
    return MonitoringResponse(data=mlops_service.get_mlops_monitoring_summary(db, organization_id=org_id))
