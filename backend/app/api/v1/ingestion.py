from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import organization_scope, require_roles
from app.core.errors import NotFoundError
from app.core.logging import get_correlation_id
from app.db.session import get_db
from app.jobs.runner import job_runner
from app.models.ingestion import IngestionBatch
from app.models.job import JobType
from app.models.user import User, UserRole
from app.schemas.common import Page
from app.schemas.ingestion import IngestionBatchResponse, IngestionRequest, IngestionResult
from app.services import ingestion_service

router = APIRouter(prefix="/ingestion", tags=["ingestion"])

# Batches at or below this size are processed synchronously — the caller
# gets a definitive result in the response. Above it, use the async job
# endpoint (background processing foundation) so a large upload doesn't
# hold an HTTP connection open indefinitely.
SYNC_INGESTION_LIMIT = 2000


@router.post("/measurements", response_model=IngestionResult)
def ingest_measurements(
    payload: IngestionRequest,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.ENGINEER)),
    db: Session = Depends(get_db),
) -> IngestionResult:
    """Synchronous ingestion for small-to-medium batches (<= 2000 records).
    For larger batches, use POST /ingestion/batches, which runs the same
    validation asynchronously via the background job runner."""
    if len(payload.records) > SYNC_INGESTION_LIMIT:
        from app.core.errors import ValidationFailedError

        raise ValidationFailedError(
            f"Batch of {len(payload.records)} records exceeds the synchronous limit of "
            f"{SYNC_INGESTION_LIMIT}. Use POST /ingestion/batches for larger uploads."
        )
    return ingestion_service.ingest_records(
        db,
        organization_id=org_id,
        data_source_name=payload.data_source_name,
        raw_records=payload.records,
        submitted_by_user_id=user.id,
    )


@router.post("/batches", status_code=202)
async def submit_batch_ingestion(
    payload: IngestionRequest,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.ENGINEER)),
) -> dict:
    """Async ingestion for batches larger than the synchronous limit. Runs
    on the background job runner (app.jobs) — poll GET /jobs/{job_id} for
    status/progress/result, exactly like any other background job.

    Declared `async def` (unlike the other routes here) specifically so this
    handler runs on the event loop thread rather than FastAPI's worker
    threadpool — `job_runner.submit` calls `asyncio.create_task`, which
    requires a running loop in the calling thread.
    """
    job_id = job_runner.submit(
        organization_id=org_id,
        job_type=JobType.BATCH_INGESTION,
        payload={
            "organization_id": org_id,
            "data_source_name": payload.data_source_name,
            "raw_records": payload.records,
            "submitted_by_user_id": user.id,
        },
        correlation_id=get_correlation_id(),
    )
    return {"job_id": job_id, "status_url": f"/api/v1/jobs/{job_id}"}


@router.get("/batches", response_model=Page[IngestionBatchResponse])
def list_batches(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    org_id: str = Depends(organization_scope),
    db: Session = Depends(get_db),
) -> Page[IngestionBatchResponse]:
    query = db.query(IngestionBatch).filter(IngestionBatch.organization_id == org_id).order_by(
        IngestionBatch.created_at.desc()
    )
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    return Page(items=[IngestionBatchResponse.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/batches/{batch_id}", response_model=IngestionBatchResponse)
def get_batch(batch_id: str, org_id: str = Depends(organization_scope), db: Session = Depends(get_db)) -> IngestionBatchResponse:
    batch = db.query(IngestionBatch).filter(IngestionBatch.id == batch_id, IngestionBatch.organization_id == org_id).first()
    if batch is None:
        raise NotFoundError(f"Ingestion batch '{batch_id}' not found.")
    return IngestionBatchResponse.model_validate(batch)
