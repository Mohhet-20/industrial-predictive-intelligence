import re

from sqlalchemy.orm import Session

from app.core.errors import AuthenticationError, ConflictError
from app.core.security import create_access_token, hash_password, verify_password
from app.models.organization import Organization
from app.models.user import User, UserRole


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "org"


def register_organization(
    db: Session, *, organization_name: str, admin_email: str, admin_password: str, admin_full_name: str
) -> tuple[Organization, User]:
    base_slug = _slugify(organization_name)
    slug = base_slug
    suffix = 1
    while db.query(Organization).filter(Organization.slug == slug).first() is not None:
        suffix += 1
        slug = f"{base_slug}-{suffix}"

    if db.query(Organization).filter(Organization.name == organization_name).first() is not None:
        raise ConflictError(f"An organization named '{organization_name}' already exists.")

    org = Organization(name=organization_name, slug=slug)
    db.add(org)
    db.flush()

    user = User(
        organization_id=org.id,
        email=admin_email.lower(),
        hashed_password=hash_password(admin_password),
        full_name=admin_full_name,
        role=UserRole.ADMIN,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(org)
    db.refresh(user)
    return org, user


# A fixed, precomputed bcrypt hash of a value nobody will ever enter,
# hashed once at import time. Used only so that a lookup for a nonexistent
# email still pays the same bcrypt cost a real password check would —
# without it, "user not found" returns measurably faster than "wrong
# password", which is a timing side-channel that lets an attacker enumerate
# valid emails purely from response latency. Phase 7 §10 fix.
_DUMMY_HASH = hash_password("not-a-real-password-used-only-for-timing-equalization")


def authenticate(db: Session, *, email: str, password: str) -> User:
    user = db.query(User).filter(User.email == email.lower()).first()
    if user is None:
        verify_password(password, _DUMMY_HASH)  # pay the same bcrypt cost either way
        raise AuthenticationError("Incorrect email or password.")
    if not user.is_active or not verify_password(password, user.hashed_password):
        raise AuthenticationError("Incorrect email or password.")
    return user


def issue_token(user: User) -> str:
    return create_access_token(subject=user.id, extra_claims={"org": user.organization_id, "role": user.role.value})
