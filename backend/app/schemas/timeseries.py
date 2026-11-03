"""Pydantic schemas for the Time-Series Intelligence API.

Most analysis endpoints (profile, temporal analysis, resampling, change
candidates, correlation, missingness) return a bounded but variably-shaped
payload — the exact keys present depend on how much data was available
(insufficient_data, gated distribution/spectral features, etc.), which is
the whole point of this layer's explainability requirement: every result
says what it computed, over what window, and why it couldn't compute
something, rather than just returning a number. Modeling every possible
key as a strict field would either lose that nuance or require a schema
tree that duplicates app/timeseries/*.py's own docstrings. For these
endpoints this module defines a typed *envelope* (things every response
needs regardless of shape) around a `dict[str, Any]` payload built by the
already-tested app/timeseries functions — response_model is intentionally
loose here, not absent.

Feature definitions, dataset builds, feature previews, and split
utilities have a fixed, simple shape and are fully typed below.
"""
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.timeseries.features import FeatureCategory, FeatureKind
from app.timeseries.resampling import AggregationMethod, InterpolationPolicy

# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class ResampleRequest(BaseModel):
    start: datetime
    end: datetime
    freq_seconds: float = Field(gt=0)
    aggregation: AggregationMethod = AggregationMethod.MEAN
    interpolation: InterpolationPolicy = InterpolationPolicy.NONE


class FeaturePreviewSpec(BaseModel):
    feature_name: str
    sensor_id: str
    kind: FeatureKind
    window_seconds: Optional[float] = Field(default=None, gt=0)
    params: dict[str, Any] = Field(default_factory=dict)


class FeaturePreviewRequest(BaseModel):
    as_of: datetime
    specs: list[FeaturePreviewSpec] = Field(min_length=1, max_length=50)


class FeatureDefinitionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    category: FeatureCategory
    kind: FeatureKind
    window_seconds: Optional[float] = Field(default=None, gt=0)
    params: dict[str, Any] = Field(default_factory=dict)
    missing_data_policy: str = "strict"
    description: Optional[str] = Field(default=None, max_length=500)


class TargetSpecRequest(BaseModel):
    sensor_id: str
    horizon_seconds: float = Field(gt=0)
    tolerance_seconds: float = Field(gt=0)


class DatasetFeatureSpec(BaseModel):
    feature_name: str
    sensor_id: str
    kind: FeatureKind
    window_seconds: Optional[float] = Field(default=None, gt=0)
    params: dict[str, Any] = Field(default_factory=dict)


class DatasetBuildRequest(BaseModel):
    sensor_ids: list[str] = Field(min_length=1, max_length=20)
    feature_specs: list[DatasetFeatureSpec] = Field(min_length=1, max_length=50)
    grid_start: datetime
    grid_end: datetime
    step_seconds: float = Field(gt=0)
    target: Optional[TargetSpecRequest] = None


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class AnalysisEnvelope(BaseModel):
    """Generic envelope for the profile/temporal-analysis/resample/change-
    candidate/correlation/missingness endpoints — see module docstring."""

    data: dict[str, Any]


class FeaturePreviewResultItem(BaseModel):
    feature_name: str
    result: dict[str, Any]


class FeaturePreviewResponse(BaseModel):
    as_of: datetime
    results: list[FeaturePreviewResultItem]


class FeatureDefinitionResponse(BaseModel):
    id: str
    name: str
    version: int
    category: str
    kind: str
    window_seconds: Optional[float]
    params: Optional[dict[str, Any]]
    missing_data_policy: str
    description: Optional[str]
    created_at: datetime
    created_by_user_id: Optional[str]

    model_config = {"from_attributes": True}


class DatasetBuildResponse(BaseModel):
    id: str
    config: dict[str, Any]
    row_count_total: int
    null_feature_rate: Optional[float]
    background_job_id: Optional[str]
    created_at: datetime
    created_by_user_id: Optional[str]

    model_config = {"from_attributes": True}


class DatasetRowsResponse(BaseModel):
    rows: list[dict[str, Any]]
    row_count_total: int
    row_count_returned: int
    offset: int
    limit: int
    null_feature_rate: Optional[float]
    leakage_policy: str


class ChronologicalSplitResponse(BaseModel):
    train_start: str
    train_end: str
    val_start: Optional[str]
    val_end: Optional[str]
    test_start: str
    test_end: str
    leakage_safe: bool


class WindowFoldResponse(BaseModel):
    fold_index: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    leakage_safe: bool


class WindowSplitsResponse(BaseModel):
    folds: list[WindowFoldResponse]
    fold_count: int
    truncated: bool
