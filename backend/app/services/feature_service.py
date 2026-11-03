"""Feature-definition registry, live feature preview, and dataset-build
orchestration. Kept separate from timeseries_service.py because that
module is read-only analysis over data that already exists, while this
module writes rows (feature_definitions, dataset_builds) and coordinates
with the background job runner.
"""
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.models.feature import DatasetBuild, FeatureDefinition
from app.models.job import JobType
from app.timeseries.data_access import TooManyObservationsError, load_multi_sensor_series
from app.timeseries.dataset_builder import (
    MAX_DATASET_ROWS_SYNC,
    MAX_DATASET_ROWS_TOTAL,
    FeatureSpec,
    TargetSpec,
    build_feature_matrix,
    grid_row_count,
)
from app.timeseries.features import FeatureKind, compute_feature_value
from app.services.timeseries_service import get_sensor_in_org, validate_time_range


# ---------------------------------------------------------------------------
# Feature definitions — versioned, immutable
# ---------------------------------------------------------------------------


def create_feature_definition(
    db: Session,
    *,
    organization_id: str,
    user_id: Optional[str],
    name: str,
    category: str,
    kind: str,
    window_seconds: Optional[float],
    params: dict[str, Any],
    missing_data_policy: str,
    description: Optional[str],
) -> FeatureDefinition:
    """Creates a new version of `name` — never updates an existing one.
    version = 1 + the highest existing version for this (org, name), so
    a dataset built against version 3 keeps meaning exactly what it meant
    when it was built even after someone adds version 4."""
    current_max = (
        db.query(func.max(FeatureDefinition.version))
        .filter(FeatureDefinition.organization_id == organization_id, FeatureDefinition.name == name)
        .scalar()
    )
    next_version = (current_max or 0) + 1

    definition = FeatureDefinition(
        organization_id=organization_id,
        name=name,
        version=next_version,
        category=category,
        kind=kind,
        window_seconds=window_seconds,
        params=params or None,
        missing_data_policy=missing_data_policy,
        description=description,
        created_by_user_id=user_id,
    )
    db.add(definition)
    db.commit()
    db.refresh(definition)
    return definition


def list_feature_definitions(
    db: Session, *, organization_id: str, name: Optional[str] = None, limit: int = 100, offset: int = 0
) -> tuple[list[FeatureDefinition], int]:
    query = db.query(FeatureDefinition).filter(FeatureDefinition.organization_id == organization_id)
    if name:
        query = query.filter(FeatureDefinition.name == name)
    query = query.order_by(FeatureDefinition.name.asc(), FeatureDefinition.version.desc())
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    return rows, total


def get_feature_definition(db: Session, *, organization_id: str, definition_id: str) -> FeatureDefinition:
    definition = (
        db.query(FeatureDefinition)
        .filter(FeatureDefinition.id == definition_id, FeatureDefinition.organization_id == organization_id)
        .first()
    )
    if definition is None:
        raise NotFoundError(f"Feature definition '{definition_id}' not found.")
    return definition


# ---------------------------------------------------------------------------
# Live feature preview — same compute_feature_value used by dataset builds,
# against currently-stored data, for a single as_of. Useful for a UI that
# lets someone try a feature definition before saving it.
# ---------------------------------------------------------------------------


def preview_features(
    db: Session, *, organization_id: str, as_of: datetime, specs: list[Any]
) -> list[dict[str, Any]]:
    sensor_ids = sorted({spec.sensor_id for spec in specs})
    for sid in sensor_ids:
        get_sensor_in_org(db, sensor_id=sid, organization_id=organization_id)

    # Bound the load window: features only ever look backward from as_of, so
    # nothing after as_of needs to be loaded, and a generous-but-bounded
    # lookback covers every window_seconds any spec could request.
    max_window = max((spec.window_seconds or 0) for spec in specs)
    lookback = timedelta(seconds=max(max_window * 2, 86400 * 30))
    try:
        series_by_sensor = load_multi_sensor_series(db, sensor_ids=sensor_ids, start=as_of - lookback, end=as_of)
    except TooManyObservationsError as exc:
        raise ValidationFailedError(str(exc))

    results: list[dict[str, Any]] = []
    for spec in specs:
        series = series_by_sensor.get(spec.sensor_id)
        if series is None or series.empty:
            results.append(
                {
                    "feature_name": spec.feature_name,
                    "result": {
                        "kind": spec.kind.value,
                        "category": None,
                        "value": None,
                        "computed": False,
                        "reason": "no observations available for this sensor at or before as_of",
                        "observation_count_used": 0,
                        "window_start": None,
                        "window_end": None,
                    },
                }
            )
            continue
        fr = compute_feature_value(series, kind=spec.kind, as_of=as_of, window_seconds=spec.window_seconds, params=spec.params)
        results.append({"feature_name": spec.feature_name, "result": fr.to_dict()})
    return results


# ---------------------------------------------------------------------------
# Dataset builds — config snapshot persisted, matrix recomputed on demand
# ---------------------------------------------------------------------------


def _specs_from_config(feature_specs: list[dict[str, Any]]) -> list[FeatureSpec]:
    return [
        FeatureSpec(
            feature_name=fs["feature_name"],
            sensor_id=fs["sensor_id"],
            kind=FeatureKind(fs["kind"]),
            window_seconds=fs.get("window_seconds"),
            params=fs.get("params") or {},
        )
        for fs in feature_specs
    ]


def _target_from_config(target: Optional[dict[str, Any]]) -> Optional[TargetSpec]:
    if not target:
        return None
    return TargetSpec(
        sensor_id=target["sensor_id"], horizon_seconds=target["horizon_seconds"], tolerance_seconds=target["tolerance_seconds"]
    )


def _sensor_ids_for_config(config: dict[str, Any]) -> list[str]:
    ids = {fs["sensor_id"] for fs in config["feature_specs"]}
    if config.get("target"):
        ids.add(config["target"]["sensor_id"])
    return sorted(ids)


def _load_series_for_build(db: Session, *, config: dict[str, Any], organization_id: str) -> dict[str, Any]:
    sensor_ids = _sensor_ids_for_config(config)
    grid_start = datetime.fromisoformat(config["grid_start"])
    grid_end = datetime.fromisoformat(config["grid_end"])
    horizon = config.get("target", {}).get("horizon_seconds", 0) if config.get("target") else 0
    load_start = grid_start
    load_end = grid_end + timedelta(seconds=horizon) if horizon else grid_end
    try:
        return load_multi_sensor_series(db, sensor_ids=sensor_ids, start=load_start, end=load_end)
    except TooManyObservationsError as exc:
        raise ValidationFailedError(str(exc))


def create_dataset_build(
    db: Session,
    *,
    organization_id: str,
    user_id: Optional[str],
    sensor_ids: list[str],
    feature_specs: list[dict[str, Any]],
    grid_start: datetime,
    grid_end: datetime,
    step_seconds: float,
    target: Optional[dict[str, Any]],
    correlation_id: Optional[str],
) -> DatasetBuild:
    validate_time_range(grid_start, grid_end)
    for sid in sensor_ids:
        get_sensor_in_org(db, sensor_id=sid, organization_id=organization_id)

    try:
        total_rows = grid_row_count(grid_start, grid_end, step_seconds)
    except ValueError as exc:
        raise ValidationFailedError(str(exc))
    if total_rows > MAX_DATASET_ROWS_TOTAL:
        raise ValidationFailedError(
            f"Grid of {total_rows} rows exceeds the maximum of {MAX_DATASET_ROWS_TOTAL}. "
            f"Narrow grid_start/grid_end or widen step_seconds."
        )

    config = {
        "sensor_ids": sensor_ids,
        "feature_specs": feature_specs,
        "grid_start": grid_start.isoformat(),
        "grid_end": grid_end.isoformat(),
        "step_seconds": step_seconds,
        "target": target,
    }

    build = DatasetBuild(
        organization_id=organization_id,
        config=config,
        row_count_total=total_rows,
        null_feature_rate=None,
        created_by_user_id=user_id,
    )
    db.add(build)
    db.commit()
    db.refresh(build)

    if total_rows <= MAX_DATASET_ROWS_SYNC:
        series_by_sensor = _load_series_for_build(db, config=config, organization_id=organization_id)
        result = build_feature_matrix(
            series_by_sensor=series_by_sensor,
            feature_specs=_specs_from_config(feature_specs),
            grid_start=grid_start,
            grid_end=grid_end,
            step_seconds=step_seconds,
            target_spec=_target_from_config(target),
            limit=total_rows,
            offset=0,
        )
        build.null_feature_rate = result["null_feature_rate"]
        db.commit()
        db.refresh(build)
        return build

    # Async path: hand the config off to the background job runner. The
    # handler recomputes the full matrix once (bounded by
    # MAX_DATASET_ROWS_TOTAL) purely to derive null_feature_rate — it does
    # NOT persist the rows themselves (see DatasetBuild's docstring).
    from app.jobs.runner import DuplicateJobError, job_runner

    try:
        job_id = job_runner.submit(
            organization_id=organization_id,
            job_type=JobType.DATASET_BUILD,
            payload={"organization_id": organization_id, "dataset_build_id": build.id, "config": config},
            correlation_id=correlation_id,
            # build.id is freshly created above (never reused), so this can
            # only collide if this exact function is somehow called twice
            # for the same build — defensive, not expected to fire in
            # normal use, but makes that scenario a clean 409 instead of a
            # second worker racing the first to write the same row.
            dedup_key=build.id,
        )
    except DuplicateJobError as exc:
        raise ConflictError(
            "A build job is already running for this dataset build.",
            details={"existing_job_id": exc.existing_job_id},
        ) from exc
    build.background_job_id = job_id
    db.commit()
    db.refresh(build)
    return build


def list_dataset_builds(db: Session, *, organization_id: str, limit: int = 50, offset: int = 0) -> tuple[list[DatasetBuild], int]:
    query = db.query(DatasetBuild).filter(DatasetBuild.organization_id == organization_id).order_by(DatasetBuild.created_at.desc())
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    return rows, total


def get_dataset_build(db: Session, *, organization_id: str, build_id: str) -> DatasetBuild:
    build = db.query(DatasetBuild).filter(DatasetBuild.id == build_id, DatasetBuild.organization_id == organization_id).first()
    if build is None:
        raise NotFoundError(f"Dataset build '{build_id}' not found.")
    return build


def get_dataset_rows(
    db: Session, *, organization_id: str, build_id: str, limit: int, offset: int
) -> dict[str, Any]:
    build = get_dataset_build(db, organization_id=organization_id, build_id=build_id)
    config = build.config
    series_by_sensor = _load_series_for_build(db, config=config, organization_id=organization_id)
    return build_feature_matrix(
        series_by_sensor=series_by_sensor,
        feature_specs=_specs_from_config(config["feature_specs"]),
        grid_start=datetime.fromisoformat(config["grid_start"]),
        grid_end=datetime.fromisoformat(config["grid_end"]),
        step_seconds=config["step_seconds"],
        target_spec=_target_from_config(config.get("target")),
        limit=limit,
        offset=offset,
    )
