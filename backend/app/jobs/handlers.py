"""Registers the concrete work functions the job runner can execute.
Kept separate from app.jobs.runner so the runner stays a generic,
domain-agnostic primitive."""
from typing import Any, Callable

from datetime import datetime

from app.db.session import SessionLocal
from app.jobs.runner import job_runner
from app.models.feature import DatasetBuild
from app.models.job import JobType
from app.services import anomaly_service, ingestion_service, scenario_service


async def _run_batch_ingestion(job_id: str, report_progress: Callable[[float], None], payload: dict[str, Any]) -> dict[str, Any]:
    db = SessionLocal()
    try:
        report_progress(0.05)
        result = ingestion_service.ingest_records(
            db,
            organization_id=payload["organization_id"],
            data_source_name=payload["data_source_name"],
            raw_records=payload["raw_records"],
            submitted_by_user_id=payload.get("submitted_by_user_id"),
        )
        report_progress(1.0)
        return result.model_dump(mode="json")
    finally:
        db.close()


async def _run_dataset_build(job_id: str, report_progress: Callable[[float], None], payload: dict[str, Any]) -> dict[str, Any]:
    """Recomputes the full feature matrix once (bounded by
    MAX_DATASET_ROWS_TOTAL) purely to derive row_count_total/null_feature_rate
    for the DatasetBuild row, then discards the rows — the matrix itself is
    never persisted; GET /timeseries/datasets/{id}/rows recomputes pages of
    it on demand from the same immutable config."""
    from app.services.feature_service import _load_series_for_build, _specs_from_config, _target_from_config
    from app.timeseries.dataset_builder import build_feature_matrix

    db = SessionLocal()
    try:
        report_progress(0.05)
        build_id = payload["dataset_build_id"]
        config = payload["config"]
        build = db.get(DatasetBuild, build_id)
        if build is None:
            raise ValueError(f"DatasetBuild '{build_id}' no longer exists.")

        series_by_sensor = _load_series_for_build(db, config=config, organization_id=payload["organization_id"])
        report_progress(0.4)

        result = build_feature_matrix(
            series_by_sensor=series_by_sensor,
            feature_specs=_specs_from_config(config["feature_specs"]),
            grid_start=datetime.fromisoformat(config["grid_start"]),
            grid_end=datetime.fromisoformat(config["grid_end"]),
            step_seconds=config["step_seconds"],
            target_spec=_target_from_config(config.get("target")),
            limit=build.row_count_total,
            offset=0,
        )
        report_progress(0.9)

        build.null_feature_rate = result["null_feature_rate"]
        db.commit()

        report_progress(1.0)
        return {
            "dataset_build_id": build_id,
            "row_count_total": result["row_count_total"],
            "null_feature_rate": result["null_feature_rate"],
        }
    finally:
        db.close()


async def _run_anomaly_detection(job_id: str, report_progress: Callable[[float], None], payload: dict[str, Any]) -> dict[str, Any]:
    """Executes a queued ModelRun (app.services.anomaly_service.
    execute_detection_run) for scoring windows too large for the sync
    path in create_detection_run. report_progress is coarse (there is no
    natural mid-point inside a single detect() call) but still reflects
    real state, not a fabricated animation."""
    db = SessionLocal()
    try:
        report_progress(0.05)
        result = anomaly_service.execute_detection_run(
            db, run_id=payload["run_id"], retrospective=payload.get("retrospective", False)
        )
        report_progress(1.0)
        return result
    finally:
        db.close()


async def _run_scenario_simulation(job_id: str, report_progress: Callable[[float], None], payload: dict[str, Any]) -> dict[str, Any]:
    """Executes a queued ScenarioRun (app.services.scenario_service.
    execute_scenario_run) for baseline windows too large for the sync path
    in create_scenario_run. Mirrors _run_anomaly_detection above exactly."""
    db = SessionLocal()
    try:
        report_progress(0.05)
        result = scenario_service.execute_scenario_run(db, run_id=payload["run_id"])
        report_progress(1.0)
        return result
    finally:
        db.close()


def register_job_handlers() -> None:
    job_runner.register(JobType.BATCH_INGESTION, _run_batch_ingestion)
    job_runner.register(JobType.DATASET_BUILD, _run_dataset_build)
    job_runner.register(JobType.ANOMALY_DETECTION_RUN, _run_anomaly_detection)
    job_runner.register(JobType.SCENARIO_SIMULATION_RUN, _run_scenario_simulation)
