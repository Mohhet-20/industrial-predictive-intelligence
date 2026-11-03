from pydantic import BaseModel, EmailStr, Field

from app.models.user import UserRole


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in_minutes: int


class UserResponse(BaseModel):
    id: str
    email: str
    full_name: str
    role: UserRole
    organization_id: str
    organization_name: str

    model_config = {"from_attributes": True}


class RegisterOrganizationRequest(BaseModel):
    """Bootstraps a brand-new organization with its first admin user.
    Subsequent users are created by an admin, not via open self-registration."""

    organization_name: str = Field(min_length=2, max_length=200)
    admin_email: EmailStr
    admin_password: str = Field(min_length=8, max_length=128)
    admin_full_name: str = Field(min_length=1, max_length=200)
