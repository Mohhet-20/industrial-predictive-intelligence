from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core import rate_limit
from app.core.deps import get_current_user
from app.core.errors import RateLimitedError
from app.db.session import get_db
from app.models.user import User
from app.schemas.auth import LoginRequest, RegisterOrganizationRequest, TokenResponse, UserResponse
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register-organization", response_model=UserResponse, status_code=201)
def register_organization(payload: RegisterOrganizationRequest, db: Session = Depends(get_db)) -> UserResponse:
    org, user = auth_service.register_organization(
        db,
        organization_name=payload.organization_name,
        admin_email=payload.admin_email,
        admin_password=payload.admin_password,
        admin_full_name=payload.admin_full_name,
    )
    return UserResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        organization_id=org.id,
        organization_name=org.name,
    )


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)) -> TokenResponse:
    from app.core.config import get_settings

    settings = get_settings()
    # Keyed by client IP + attempted email, not email alone: an attacker
    # spraying many emails from one IP is still rate-limited per-email
    # (credential stuffing), while one IP's own repeated wrong-password
    # attempts against a single account are also capped (a plain per-IP
    # limit would let a single very-active office network lock out
    # everyone behind it; this keying avoids that failure mode).
    client_ip = request.client.host if request.client else "unknown"
    rate_key = f"{client_ip}:{payload.email.lower()}"
    allowed, retry_after = rate_limit.check_and_record(
        rate_key,
        max_attempts=settings.login_rate_limit_attempts,
        window_seconds=settings.login_rate_limit_window_seconds,
    )
    if not allowed:
        raise RateLimitedError(
            f"Too many login attempts. Try again in {retry_after} seconds.",
            details={"retry_after_seconds": retry_after},
        )

    user = auth_service.authenticate(db, email=payload.email, password=payload.password)
    token = auth_service.issue_token(user)
    return TokenResponse(
        access_token=token, expires_in_minutes=settings.jwt_access_token_expire_minutes
    )


@router.get("/me", response_model=UserResponse)
def me(user: User = Depends(get_current_user)) -> UserResponse:
    return UserResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        organization_id=user.organization_id,
        organization_name=user.organization.name,
    )
