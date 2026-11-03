"""Scenario Simulation / What-If Analysis API (Phase 11 §12, §18).

THIS IS SCENARIO ANALYSIS, NOT A PERFECT PHYSICAL DIGITAL TWIN — see
app.simulation's package docstring. Every route here is organization-scoped
(app.core.deps.organization_scope, the same tenant-isolation discipline as
every other router in this platform) and can NEVER alter a real
SensorMeasurement row — see app.services.scenario_service's module
docstring for the non-destructive guarantee. Creating, validating, and
running a scenario require ADMIN or ENGINEER; reading is open to any
authenticated org member, matching app.api.v1.condition's role split.

DELETE archives rather than hard-deletes (§12) — every prior run and its
results stay inspectable, the same "delete = archive" discipline this
platform uses everywhere else evidence must survive a cleanup action.
"""
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import organization_scope, require_roles
from app.core.logging import get_correlation_id
from app.db.session import get_db
from app.models.scenario import ScenarioStatus
from app.models.user import User, UserRole
from app.schemas.common import Page
from app.schemas.scenario import (
    ScenarioCompareRequest,
    ScenarioCompareResponse,
    ScenarioCreateRequest,
    ScenarioResponse,
    ScenarioRunCreateRequest,
    ScenarioRunResponse,
    SensitivitySweepRequest,
)
from app.services import scenario_service

router = APIRouter(prefix="/scenarios", tags=["scenarios"])

_WRITE_ROLES = (UserRole.ADMIN, UserRole.ENGINEER)


@router.post("", response_model=ScenarioResponse, status_code=201)
def create_scenario(
    payload: ScenarioCreateRequest,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    db: Session = Depends(get_db),
) -> ScenarioResponse:
    scenario = scenario_service.create_scenario(
        db, organization_id=org_id, user_id=user.id, asset_id=payload.asset_id, sensor_id=payload.sensor_id,
        sensor_scope=payload.sensor_scope, name=payload.name, description=payload.description,
        scenario_type=payload.scenario_type, baseline_window_start=payload.baseline_window_start,
        baseline_window_end=payload.baseline_window_end, reference_window_start=payload.reference_window_start,
        reference_window_end=payload.reference_window_end, modification_start=payload.modification_start,
        modification_duration_seconds=payload.modification_duration_seconds, magnitude=payload.magnitude,
        unit=payload.unit, parameters=payload.parameters,
    )
    return ScenarioResponse.model_validate(scenario)


@router.get("", response_model=Page[ScenarioResponse])
def list_scenarios(
    asset_id: Optional[str] = Query(default=None),
    sensor_id: Optional[str] = Query(default=None),
    status: Optional[ScenarioStatus] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    org_id: str = Depends(organization_scope),
    db: Session = Depends(get_db),
) -> Page[ScenarioResponse]:
    rows, total = scenario_service.list_scenarios(db, organization_id=org_id, asset_id=asset_id, sensor_id=sensor_id, status=status, limit=limit, offset=offset)
    return Page(items=[ScenarioResponse.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/{scenario_id}", response_model=ScenarioResponse)
def get_scenario(scenario_id: str, org_id: str = Depends(organization_scope), db: Session = Depends(get_db)) -> ScenarioResponse:
    scenario = scenario_service.get_scenario(db, organization_id=org_id, scenario_id=scenario_id)
    return ScenarioResponse.model_validate(scenario)


@router.delete("/{scenario_id}", response_model=ScenarioResponse)
def archive_scenario(
    scenario_id: str, org_id: str = Depends(organization_scope), user: User = Depends(require_roles(*_WRITE_ROLES)), db: Session = Depends(get_db)
) -> ScenarioResponse:
    """Archives (never hard-deletes) — see module docstring."""
    scenario = scenario_service.archive_scenario(db, organization_id=org_id, scenario_id=scenario_id)
    return ScenarioResponse.model_validate(scenario)


@router.post("/{scenario_id}/validate", response_model=ScenarioResponse)
def validate_scenario(
    scenario_id: str, org_id: str = Depends(organization_scope), user: User = Depends(require_roles(*_WRITE_ROLES)), db: Session = Depends(get_db)
) -> ScenarioResponse:
    """Runs every app.simulation.validation check and records the full
    result (errors AND warnings) on the scenario — never silently clamps.
    A scenario with any error stays DRAFT and cannot be run."""
    scenario = scenario_service.validate_scenario(db, organization_id=org_id, scenario_id=scenario_id)
    return ScenarioResponse.model_validate(scenario)


@router.post("/{scenario_id}/runs", response_model=ScenarioRunResponse, status_code=201)
async def create_scenario_run(
    scenario_id: str, payload: ScenarioRunCreateRequest,
    org_id: str = Depends(organization_scope), user: User = Depends(require_roles(*_WRITE_ROLES)), db: Session = Depends(get_db),
) -> ScenarioRunResponse:
    """Refuses (409) unless the scenario is VALIDATED. Runs synchronously
    for bounded data volumes, or as a background job for larger ones — see
    app.services.scenario_service.create_scenario_run.

    Declared `async def` (matching app.api.v1.anomaly.create_detection_run's
    identical pattern) so this coroutine executes directly on the event loop
    thread rather than FastAPI's sync threadpool — the large-run branch of
    create_scenario_run calls app.jobs.runner.job_runner.submit(), which
    calls asyncio.create_task() and requires a running event loop in the
    calling thread. A plain `def` here would raise 'no running event loop'
    the moment a scenario's estimated row count crosses MAX_SCENARIO_ROWS_SYNC."""
    run = scenario_service.create_scenario_run(
        db, organization_id=org_id, user_id=user.id, scenario_id=scenario_id,
        include_forecast_comparison=payload.include_forecast_comparison, correlation_id=get_correlation_id(),
    )
    return ScenarioRunResponse.model_validate(run)


@router.post("/{scenario_id}/sensitivity", response_model=ScenarioRunResponse, status_code=201)
def run_sensitivity_sweep(
    scenario_id: str, payload: SensitivitySweepRequest,
    org_id: str = Depends(organization_scope), user: User = Depends(require_roles(*_WRITE_ROLES)), db: Session = Depends(get_db),
) -> ScenarioRunResponse:
    """Sweeps magnitude across the given values, re-running the real
    pipeline at each one against the same real baseline data (§9). Reports
    what actually happened at each magnitude — never fits or extrapolates a
    trend, and never claims a physical dose-response relationship."""
    run = scenario_service.run_sensitivity_sweep(db, organization_id=org_id, user_id=user.id, scenario_id=scenario_id, magnitudes=payload.magnitudes)
    return ScenarioRunResponse.model_validate(run)


@router.get("/{scenario_id}/runs", response_model=Page[ScenarioRunResponse])
def list_scenario_runs(
    scenario_id: str, limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0),
    org_id: str = Depends(organization_scope), db: Session = Depends(get_db),
) -> Page[ScenarioRunResponse]:
    rows, total = scenario_service.list_scenario_runs(db, organization_id=org_id, scenario_id=scenario_id, limit=limit, offset=offset)
    return Page(items=[ScenarioRunResponse.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/runs/{run_id}", response_model=ScenarioRunResponse)
def get_scenario_run(run_id: str, org_id: str = Depends(organization_scope), db: Session = Depends(get_db)) -> ScenarioRunResponse:
    run = scenario_service.get_scenario_run(db, organization_id=org_id, run_id=run_id)
    return ScenarioRunResponse.model_validate(run)


@router.post("/compare", response_model=ScenarioCompareResponse)
def compare_scenario_runs(
    payload: ScenarioCompareRequest, org_id: str = Depends(organization_scope), db: Session = Depends(get_db)
) -> ScenarioCompareResponse:
    """Multi-scenario comparison (§8) — lists each run's real result side
    by side. No fabricated combined risk score is ever computed."""
    data = scenario_service.compare_scenario_runs(db, organization_id=org_id, run_ids=payload.scenario_run_ids)
    return ScenarioCompareResponse(data=data)
