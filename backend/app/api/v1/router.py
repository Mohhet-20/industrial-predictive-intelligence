from fastapi import APIRouter

from app.api.v1 import anomaly, assets, auth, condition, copilot, data_quality, health, ingestion, investigations, jobs, measurements, mlops, orchestration, outcome, scenario, system, timeseries

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(system.router)
api_router.include_router(auth.router)
api_router.include_router(assets.router)
api_router.include_router(measurements.router)
api_router.include_router(ingestion.router)
api_router.include_router(data_quality.router)
api_router.include_router(jobs.router)
api_router.include_router(timeseries.router)
api_router.include_router(anomaly.router)
api_router.include_router(condition.router)
api_router.include_router(mlops.router)
api_router.include_router(investigations.router)
api_router.include_router(orchestration.router)
api_router.include_router(copilot.router)
api_router.include_router(scenario.router)
api_router.include_router(outcome.router)
api_router.include_router(outcome.feedback_router)
