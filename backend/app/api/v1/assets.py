from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, organization_scope, require_roles
from app.core.errors import ConflictError, NotFoundError
from app.db.session import get_db
from app.models.asset import Asset
from app.models.sensor import Sensor
from app.models.user import User, UserRole
from app.schemas.asset import AssetCreate, AssetResponse
from app.schemas.common import Page
from app.schemas.sensor import SensorCreate, SensorResponse

router = APIRouter(prefix="/assets", tags=["assets"])


def _to_asset_response(asset: Asset, sensor_count: int) -> AssetResponse:
    return AssetResponse(
        id=asset.id,
        code=asset.code,
        name=asset.name,
        asset_type=asset.asset_type,
        location=asset.location,
        description=asset.description,
        sensor_count=sensor_count,
    )


@router.get("", response_model=Page[AssetResponse])
def list_assets(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    org_id: str = Depends(organization_scope),
    db: Session = Depends(get_db),
) -> Page[AssetResponse]:
    query = db.query(Asset).filter(Asset.organization_id == org_id).order_by(Asset.code)
    total = query.count()
    assets = query.offset(offset).limit(limit).all()
    items = [_to_asset_response(a, len(a.sensors)) for a in assets]
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get("/{asset_id}", response_model=AssetResponse)
def get_asset(asset_id: str, org_id: str = Depends(organization_scope), db: Session = Depends(get_db)) -> AssetResponse:
    asset = db.query(Asset).filter(Asset.id == asset_id, Asset.organization_id == org_id).first()
    if asset is None:
        raise NotFoundError(f"Asset '{asset_id}' not found.")
    return _to_asset_response(asset, len(asset.sensors))


@router.post("", response_model=AssetResponse, status_code=201)
def create_asset(
    payload: AssetCreate,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.ENGINEER)),
    db: Session = Depends(get_db),
) -> AssetResponse:
    existing = db.query(Asset).filter(Asset.organization_id == org_id, Asset.code == payload.code).first()
    if existing:
        raise ConflictError(f"Asset code '{payload.code}' already exists in this organization.")
    asset = Asset(organization_id=org_id, **payload.model_dump())
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return _to_asset_response(asset, 0)


@router.get("/{asset_id}/sensors", response_model=Page[SensorResponse])
def list_sensors(
    asset_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    org_id: str = Depends(organization_scope),
    db: Session = Depends(get_db),
) -> Page[SensorResponse]:
    asset = db.query(Asset).filter(Asset.id == asset_id, Asset.organization_id == org_id).first()
    if asset is None:
        raise NotFoundError(f"Asset '{asset_id}' not found.")
    query = db.query(Sensor).filter(Sensor.asset_id == asset_id).order_by(Sensor.code)
    total = query.count()
    sensors = query.offset(offset).limit(limit).all()
    return Page(items=[SensorResponse.model_validate(s) for s in sensors], total=total, limit=limit, offset=offset)


@router.post("/{asset_id}/sensors", response_model=SensorResponse, status_code=201)
def create_sensor(
    asset_id: str,
    payload: SensorCreate,
    org_id: str = Depends(organization_scope),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.ENGINEER)),
    db: Session = Depends(get_db),
) -> SensorResponse:
    asset = db.query(Asset).filter(Asset.id == asset_id, Asset.organization_id == org_id).first()
    if asset is None:
        raise NotFoundError(f"Asset '{asset_id}' not found.")
    existing = db.query(Sensor).filter(Sensor.asset_id == asset_id, Sensor.code == payload.code).first()
    if existing:
        raise ConflictError(f"Sensor code '{payload.code}' already exists on asset '{asset_id}'.")
    sensor = Sensor(asset_id=asset_id, **payload.model_dump())
    db.add(sensor)
    db.commit()
    db.refresh(sensor)
    return SensorResponse.model_validate(sensor)
