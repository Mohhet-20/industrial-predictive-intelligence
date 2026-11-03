"""Auth dependencies. Authorization is enforced here, server-side, on every
route that needs it — never inferred from what the frontend chooses to
show or hide."""
from typing import Callable, Iterable

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from app.core.errors import AuthenticationError, AuthorizationError
from app.core.security import TokenError, decode_access_token
from app.db.session import get_db
from app.models.user import User, UserRole


def get_current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthenticationError("Missing or malformed Authorization header.")

    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = decode_access_token(token)
    except TokenError:
        raise AuthenticationError("Invalid or expired token.")

    user_id = payload.get("sub")
    if not user_id:
        raise AuthenticationError("Token missing subject claim.")

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("User not found or inactive.")
    return user


def require_roles(*allowed: UserRole) -> Callable[[User], User]:
    """Dependency factory: `Depends(require_roles(UserRole.ADMIN))`."""

    def _check(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed:
            raise AuthorizationError(
                f"Role '{user.role.value}' is not permitted to perform this action."
            )
        return user

    return _check


def organization_scope(user: User = Depends(get_current_user)) -> str:
    """Returns the organization_id every query in this request must be
    filtered by. Centralizing this makes the tenant-isolation boundary a
    single, auditable choke point instead of something repeated (and
    potentially forgotten) in every service function."""
    return user.organization_id
