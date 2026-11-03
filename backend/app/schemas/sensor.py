from typing import Optional

from pydantic import BaseModel, Field, model_validator


class SensorCreate(BaseModel):
    code: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    measurement_type: str = Field(min_length=1, max_length=100)
    unit: str = Field(min_length=1, max_length=50)
    expected_min: Optional[float] = None
    expected_max: Optional[float] = None
    expected_interval_seconds: Optional[float] = Field(default=None, gt=0)

    @model_validator(mode="after")
    def check_range(self) -> "SensorCreate":
        if self.expected_min is not None and self.expected_max is not None:
            if self.expected_min >= self.expected_max:
                raise ValueError("expected_min must be less than expected_max")
        return self


class SensorResponse(BaseModel):
    id: str
    asset_id: str
    code: str
    name: str
    measurement_type: str
    unit: str
    expected_min: Optional[float]
    expected_max: Optional[float]
    expected_interval_seconds: Optional[float]

    model_config = {"from_attributes": True}
