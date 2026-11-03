from typing import Optional

from pydantic import BaseModel, Field


class AssetCreate(BaseModel):
    code: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    asset_type: str = Field(min_length=1, max_length=100)
    location: Optional[str] = Field(default=None, max_length=200)
    description: Optional[str] = None


class AssetResponse(BaseModel):
    id: str
    code: str
    name: str
    asset_type: str
    location: Optional[str]
    description: Optional[str]
    sensor_count: int = 0

    model_config = {"from_attributes": True}
