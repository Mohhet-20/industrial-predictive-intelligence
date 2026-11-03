"""Time-Series Intelligence API.

Every route here is read-bounded (the underlying app.timeseries functions
enforce MAX_ROWS/MAX_BUCKETS/etc and raise ValidationFailedError rather
than silently truncating or loading unbounded data) and organization-scoped
(every sensor lookup goes through get_sensor_in_org, which joins through
Asset.organization_id). Analysis endpoints (profile, temporal analysis,
resampling, change candidates, correlation) are GETs with query parameters
— they compute from stored data but create nothing. Feature preview,
feature-definition creation, and dataset-build creation are POSTs because
they either persist a row or represent a heavier one-off computation.
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, organization_scope
from app.core.errors import ValidationFailedError
from app.core.logging import get_correlation_id
from app.db.session import get_db
from app.models.user import User
from app.schemas.common import Page
from app.schemas.timeseries import (
    AnalysisEnvelope,
    ChronologicalSplitResponse,
    DatasetBuildRequest,
    DatasetBuildResponse,
    DatasetRowsResponse,
    FeatureDefinitionCreate,
    FeatureDefinitionResponse,
    FeaturePreviewRequest,
    FeaturePreviewResponse,
    WindowFoldResponse,
    WindowSplitsResponse,
)
from app.services import feature_service, timeseries_service
from app.timeseries import splitting
from app.timeseries.resampling import AggregationMethod, InterpolationPolicy

router = APIRouter(prefix="/timeseries", tags=["timeseries"])

MAX_SPLIT_SPAN_STEPS = 5000  # safety bound on fold generation before we even call splitting.*


# ---------------------------------------------------------------------------
# Profiling & temporal analysis
# ---------------------------------------------------------------------------


@router.get("/sensors/{sensor_id}/profile", response_model=AnalysisEnvelope)
def sensor_profile(
    sensor_id: str,
    start: datetime = Query(...),
    end: datetime = Query(...),
    org_id: str = Depends(organization_scope),
    db: Session = Depends(get_db),
) -> AnalysisEnvelope:
    return AnalysisEnvelope(data=timeseries_service.get_sensor_profile(db, sensor_id=sensor_id, organization_id=org_id, start=start, end=end))


@router.get("/sensors/{sensor_id}/analysis", response_model=AnalysisEnvelope)
def sensor_temporal_analysis(
    sensor_id: str,
    start: datetime = Query(...),
    end: datetime = Query(...),
    rolling_window_seconds: float = Query(default=3600, gt=0),
    volatility_window_seconds: float = Query(default=3600, gt=0),
    autocorr_grid_freq_seconds: Optional[float] = Query(default=None, gt=0),
    autocorr_max_lag_steps: int = Query(default=20, ge=1, le=200),
    org_id: str = Depends(organization_scope),
    db: Session = Depends(get_db),
) -> AnalysisEnvelope:
    data = timeseries_service.get_temporal_analysis(
        db,
        sensor_id=sensor_id,
        organization_id=org_id,
        start=start,
        end=end,
        rolling_window_seconds=rolling_window_seconds,
        volatility_window_seconds=volatility_window_seconds,
        autocorr_grid_freq_seconds=autocorr_grid_freq_seconds,
        autocorr_max_lag_steps=autocorr_max_lag_steps,
    )
    return AnalysisEnvelope(data=data)


@router.get("/sensors/{sensor_id}/resample", response_model=AnalysisEnvelope)
def sensor_resample(
    sensor_id: str,
    start: datetime = Query(...),
    end: datetime = Query(...),
    freq_seconds: float = Query(gt=0),
    aggregation: AggregationMethod = Query(default=AggregationMethod.MEAN),
    interpolation: InterpolationPolicy = Query(default=InterpolationPolicy.NONE),
    org_id: str = Depends(organization_scope),
    db: Session = Depends(get_db),
) -> AnalysisEnvelope:
    data = timeseries_service.get_resampled_series(
        db, sensor_id=sensor_id, organization_id=org_id, start=start, end=end, freq_seconds=freq_seconds, aggregation=aggregation, interpolation=interpolation
    )
    return AnalysisEnvelope(data=data)


@router.get("/sensors/{sensor_id}/change-candidates", response_model=AnalysisEnvelope)
def sensor_change_candidates(
    sensor_id: str,
    start: datetime = Query(...),
    end: datetime = Query(...),
    window_size: int = Query(default=20, ge=5, le=200),
    min_level_score: float = Query(default=3.0, gt=0),
    min_variance_ratio: float = Query(default=2.5, gt=1),
    org_id: str = Depends(organization_scope),
    db: Session = Depends(get_db),
) -> AnalysisEnvelope:
    data = timeseries_service.get_change_candidates(
        db, sensor_id=sensor_id, organization_id=org_id, start=start, end=end, window_size=window_size, min_level_score=min_level_score, min_variance_ratio=min_variance_ratio
    )
    return AnalysisEnvelope(data=data)


# ---------------------------------------------------------------------------
# Multi-sensor temporal context
# ---------------------------------------------------------------------------


@router.get("/correlation", response_model=AnalysisEnvelope)
def correlation_matrix(
    sensor_ids: list[str] = Query(..., min_length=2, max_length=10),
    start: datetime = Query(...),
    end: datetime = Query(...),
    freq_seconds: float = Query(gt=0),
    org_id: str = Depends(organization_scope),
    db: Session = Depends(get_db),
) -> AnalysisEnvelope:
    data = timeseries_service.get_correlation_matrix(db, sensor_ids=sensor_ids, organization_id=org_id, start=start, end=end, freq_seconds=freq_seconds)
    return AnalysisEnvelope(data=data)


@router.get("/lagged-correlation", response_model=AnalysisEnvelope)
def lagged_correlation(
    sensor_a_id: str = Query(...),
    sensor_b_id: str = Query(...),
    start: datetime = Query(...),
    end: datetime = Query(...),
    freq_seconds: float = Query(gt=0),
    max_lag_steps: int = Query(default=10, ge=1, le=100),
    org_id: str = Depends(organization_scope),
    db: Session = Depends(get_db),
) -> AnalysisEnvelope:
    data = timeseries_service.get_lagged_correlation(
        db, sensor_a_id=sensor_a_id, sensor_b_id=sensor_b_id, organization_id=org_id, start=start, end=end, freq_seconds=freq_seconds, max_lag_steps=max_lag_steps
    )
    return AnalysisEnvelope(data=data)


@router.get("/missingness-comparison", response_model=AnalysisEnvelope)
def missingness_comparison(
    sensor_ids: list[str] = Query(..., min_length=2, max_length=10),
    start: datetime = Query(...),
    end: datetime = Query(...),
    org_id: str = Depends(organization_scope),
    db: Session = Depends(get_db),
) -> AnalysisEnvelope:
    data = timeseries_service.get_missingness_comparison(db, sensor_ids=sensor_ids, organization_id=org_id, start=start, end=end)
    return AnalysisEnvelope(data=data)


# ---------------------------------------------------------------------------
# Feature preview & feature definitions
# ---------------------------------------------------------------------------


@router.post("/feature-preview", response_model=FeaturePreviewResponse)
def feature_preview(
    payload: FeaturePreviewRequest,
    org_id: str = Depends(organization_scope),
    db: Session = Depends(get_db),
) -> FeaturePreviewResponse:
    results = feature_service.preview_features(db, organization_id=org_id, as_of=payload.as_of, specs=payload.specs)
    return FeaturePreviewResponse(as_of=payload.as_of, results=results)


@router.post("/feature-definitions", response_model=FeatureDefinitionResponse, status_code=201)
def create_feature_definition(
    payload: FeatureDefinitionCreate,
    org_id: str = Depends(organization_scope),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FeatureDefinitionResponse:
    definition = feature_service.create_feature_definition(
        db,
        organization_id=org_id,
        user_id=user.id,
        name=payload.name,
        category=payload.category.value,
        kind=payload.kind.value,
        window_seconds=payload.window_seconds,
        params=payload.params,
        missing_data_policy=payload.missing_data_policy,
        description=payload.description,
    )
    return FeatureDefinitionResponse.model_validate(definition)


@router.get("/feature-definitions", response_model=Page[FeatureDefinitionResponse])
def list_feature_definitions(
    name: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    org_id: str = Depends(organization_scope),
    db: Session = Depends(get_db),
) -> Page[FeatureDefinitionResponse]:
    rows, total = feature_service.list_feature_definitions(db, organization_id=org_id, name=name, limit=limit, offset=offset)
    return Page(items=[FeatureDefinitionResponse.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/feature-definitions/{definition_id}", response_model=FeatureDefinitionResponse)
def get_feature_definition(
    definition_id: str,
    org_id: str = Depends(organization_scope),
    db: Session = Depends(get_db),
) -> FeatureDefinitionResponse:
    definition = feature_service.get_feature_definition(db, organization_id=org_id, definition_id=definition_id)
    return FeatureDefinitionResponse.model_validate(definition)


# ---------------------------------------------------------------------------
# Training dataset builds — config snapshot persisted, matrix recomputed
# on demand (never persisted in full — see DatasetBuild's docstring).
# ---------------------------------------------------------------------------


@router.post("/datasets", response_model=DatasetBuildResponse, status_code=201)
async def create_dataset_build(
    payload: DatasetBuildRequest,
    org_id: str = Depends(organization_scope),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DatasetBuildResponse:
    """Declared `async def` (like POST /ingestion/batches) specifically so
    this handler runs on the event loop thread rather than FastAPI's worker
    threadpool — the async build path below calls job_runner.submit(),
    which calls asyncio.create_task() and requires a running loop in the
    calling thread. The DB work here is still synchronous SQLAlchemy calls;
    for this foundation's scale that briefly blocking the loop is an
    acceptable tradeoff for reusing the exact same service function on
    both the sync and async build paths."""
    build = feature_service.create_dataset_build(
        db,
        organization_id=org_id,
        user_id=user.id,
        sensor_ids=payload.sensor_ids,
        feature_specs=[fs.model_dump(mode="json") for fs in payload.feature_specs],
        grid_start=payload.grid_start,
        grid_end=payload.grid_end,
        step_seconds=payload.step_seconds,
        target=payload.target.model_dump(mode="json") if payload.target else None,
        correlation_id=get_correlation_id(),
    )
    return DatasetBuildResponse.model_validate(build)


@router.get("/datasets", response_model=Page[DatasetBuildResponse])
def list_dataset_builds(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    org_id: str = Depends(organization_scope),
    db: Session = Depends(get_db),
) -> Page[DatasetBuildResponse]:
    rows, total = feature_service.list_dataset_builds(db, organization_id=org_id, limit=limit, offset=offset)
    return Page(items=[DatasetBuildResponse.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/datasets/{build_id}", response_model=DatasetBuildResponse)
def get_dataset_build(
    build_id: str,
    org_id: str = Depends(organization_scope),
    db: Session = Depends(get_db),
) -> DatasetBuildResponse:
    build = feature_service.get_dataset_build(db, organization_id=org_id, build_id=build_id)
    return DatasetBuildResponse.model_validate(build)


@router.get("/datasets/{build_id}/rows", response_model=DatasetRowsResponse)
def get_dataset_rows(
    build_id: str,
    limit: int = Query(default=200, ge=1, le=3000),
    offset: int = Query(default=0, ge=0),
    org_id: str = Depends(organization_scope),
    db: Session = Depends(get_db),
) -> DatasetRowsResponse:
    result = feature_service.get_dataset_rows(db, organization_id=org_id, build_id=build_id, limit=limit, offset=offset)
    return DatasetRowsResponse(**result)


# ---------------------------------------------------------------------------
# Time-aware split utilities — pure computation, no stored data, reusable
# by any future model-training work. Still behind auth for consistency.
# ---------------------------------------------------------------------------


@router.get("/splits/chronological", response_model=ChronologicalSplitResponse)
def chronological_split(
    start: datetime = Query(...),
    end: datetime = Query(...),
    train_fraction: float = Query(default=0.7, gt=0, lt=1),
    val_fraction: float = Query(default=0.15, ge=0, lt=1),
    org_id: str = Depends(organization_scope),
) -> ChronologicalSplitResponse:
    try:
        split = splitting.chronological_split(start=start, end=end, train_fraction=train_fraction, val_fraction=val_fraction)
    except ValueError as exc:
        raise ValidationFailedError(str(exc))
    return ChronologicalSplitResponse(**split.to_dict())


def _bounded_folds(*, span_seconds: float, step_seconds: float) -> None:
    if step_seconds <= 0:
        raise ValidationFailedError("step_seconds must be positive.")
    estimated = int(span_seconds / step_seconds) + 2
    if estimated > MAX_SPLIT_SPAN_STEPS:
        raise ValidationFailedError(
            f"This range/step combination would generate roughly {estimated} folds "
            f"(> {MAX_SPLIT_SPAN_STEPS}). Widen step_seconds or narrow the range."
        )


@router.get("/splits/rolling-window", response_model=WindowSplitsResponse)
def rolling_window_splits(
    start: datetime = Query(...),
    end: datetime = Query(...),
    train_window_seconds: float = Query(gt=0),
    test_window_seconds: float = Query(gt=0),
    step_seconds: float = Query(gt=0),
    max_folds: int = Query(default=50, ge=1, le=500),
    org_id: str = Depends(organization_scope),
) -> WindowSplitsResponse:
    _bounded_folds(span_seconds=(end - start).total_seconds(), step_seconds=step_seconds)
    try:
        folds = splitting.rolling_window_splits(
            start=start, end=end, train_window_seconds=train_window_seconds, test_window_seconds=test_window_seconds, step_seconds=step_seconds
        )
    except ValueError as exc:
        raise ValidationFailedError(str(exc))
    truncated = len(folds) > max_folds
    page = folds[:max_folds]
    return WindowSplitsResponse(folds=[WindowFoldResponse(**f.to_dict()) for f in page], fold_count=len(folds), truncated=truncated)


@router.get("/splits/expanding-window", response_model=WindowSplitsResponse)
def expanding_window_splits(
    start: datetime = Query(...),
    end: datetime = Query(...),
    initial_train_window_seconds: float = Query(gt=0),
    test_window_seconds: float = Query(gt=0),
    step_seconds: float = Query(gt=0),
    max_folds: int = Query(default=50, ge=1, le=500),
    org_id: str = Depends(organization_scope),
) -> WindowSplitsResponse:
    _bounded_folds(span_seconds=(end - start).total_seconds(), step_seconds=step_seconds)
    try:
        folds = splitting.expanding_window_splits(
            start=start, end=end, initial_train_window_seconds=initial_train_window_seconds, test_window_seconds=test_window_seconds, step_seconds=step_seconds
        )
    except ValueError as exc:
        raise ValidationFailedError(str(exc))
    truncated = len(folds) > max_folds
    page = folds[:max_folds]
    return WindowSplitsResponse(folds=[WindowFoldResponse(**f.to_dict()) for f in page], fold_count=len(folds), truncated=truncated)
