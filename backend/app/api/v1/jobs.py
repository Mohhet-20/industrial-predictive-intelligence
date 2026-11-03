from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import organization_scope
from app.core.errors import NotFoundError
from app.db.session import get_db
from app.models.job import BackgroundJob
from app.schemas.common import Page
from app.schemas.job import JobResponse

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("", response_model=Page[JobResponse])
def list_jobs(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    org_id: str = Depends(organization_scope),
    db: Session = Depends(get_db),
) -> Page[JobResponse]:
    query = db.query(BackgroundJob).filter(BackgroundJob.organization_id == org_id).order_by(BackgroundJob.created_at.desc())
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    return Page(items=[JobResponse.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/{job_id}", response_model=JobResponse)
def get_job(job_id: str, org_id: str = Depends(organization_scope), db: Session = Depends(get_db)) -> JobResponse:
    job = db.query(BackgroundJob).filter(BackgroundJob.id == job_id, BackgroundJob.organization_id == org_id).first()
    if job is None:
        raise NotFoundError(f"Job '{job_id}' not found.")
    return JobResponse.model_validate(job)
