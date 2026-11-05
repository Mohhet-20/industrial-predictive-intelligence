"""Pydantic schemas for the Scenario Simulation API. Mirrors
app.schemas.condition's conventions: loose `dict[str, Any]` for
validation/results payloads (validated by app.simulation.* and
app.services.scenario_service, not the schema layer), fully typed response
models for everything returned to a client."""
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.models.scenario import ScenarioRunKind, ScenarioRunStatus, ScenarioStatus, ScenarioType

# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class ScenarioCreateRequest(BaseModel):
    asset_id: str
    sensor_id: str
    sensor_scope: Optional[list[str]] = None  # additional sensors, MULTI_SENSOR only
    name: str = Field(min_length=1, max_length=300)
    description: Optional[str] = Field(default=None, max_length=4000)
    scenario_type: ScenarioType
    baseline_window_start: datetime
    baseline_window_end: datetime
    reference_window_start: Optional[datetime] = None
    reference_window_end: Optional[datetime] = None
    modification_start: Optional[datetime] = None
    modification_duration_seconds: Optional[float] = Field(default=None, gt=0)
    magnitude: Optional[float] = None
    unit: Optional[str] = None
    # Type-specific extras: rolling_window_observations, shape, per_sensor
    # (list of {sensor_id, scenario_type, modification_start,
    # modification_duration_seconds, magnitude, unit, parameters} for
    # MULTI_SENSOR), model_version_id, include_forecast_comparison.
    parameters: dict[str, Any] = Field(default_factory=dict)


class ScenarioRunCreateRequest(BaseModel):
    include_forecast_comparison: bool = False


class SensitivitySweepRequest(BaseModel):
    magnitudes: list[float] = Field(min_length=1, max_length=8)


class ScenarioCompareRequest(BaseModel):
    scenario_run_ids: list[str] = Field(min_length=2, max_length=10)


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class ScenarioResponse(BaseModel):
    id: str
    organization_id: str
    asset_id: str
    sensor_id: str
    sensor_scope: list[str]
    name: str
    description: Optional[str]
    scenario_type: ScenarioType
    baseline_window_start: datetime
    baseline_window_end: datetime
    reference_window_start: Optional[datetime]
    reference_window_end: Optional[datetime]
    modification_start: Optional[datetime]
    modification_duration_seconds: Optional[float]
    magnitude: Optional[float]
    unit: Optional[str]
    parameters: dict[str, Any]
    status: ScenarioStatus
    version: int
    validation_result: Optional[dict[str, Any]]
    created_by_user_id: Optional[str]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ScenarioRunResponse(BaseModel):
    id: str
    organization_id: str
    scenario_id: str
    scenario_version: int
    run_kind: ScenarioRunKind
    status: ScenarioRunStatus
    config_snapshot: dict[str, Any]
    pipeline_versions: dict[str, Any]
    results: Optional[dict[str, Any]]
    error_message: Optional[str]
    background_job_id: Optional[str]
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    created_at: datetime
    created_by_user_id: Optional[str]

    model_config = {"from_attributes": True}


class ScenarioCompareResponse(BaseModel):
    data: dict[str, Any]
