"""POST /auth/guest: public-demo guest sessions. Off by default, never an administrator, no password involved."""

import pytest

from app.core.config import get_settings
from app.core.security import hash_password
from app.models.user import User, UserRole


@pytest.fixture
def guest_enabled(monkeypatch):
    monkeypatch.setenv("GUEST_ACCESS_ENABLED", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("GUEST_ACCESS_ENABLED")
    get_settings.cache_clear()


def _add_user(db_session, org, email, role):
    db_session.add(
        User(
            organization_id=org.id,
            email=email,
            hashed_password=hash_password("Unused-Random-Pass-1!"),
            full_name="Guest",
            role=role,
            is_active=True,
        )
    )
    db_session.commit()


def test_guest_access_is_off_by_default(client, db_session, org_and_admin):
    _add_user(db_session, org_and_admin[0], "guest@riverbend-demo.io", UserRole.ENGINEER)
    assert client.post("/api/v1/auth/guest").status_code == 404


def test_guest_session_is_an_engineer_with_no_password(client, db_session, org_and_admin, guest_enabled):
    _add_user(db_session, org_and_admin[0], "guest@riverbend-demo.io", UserRole.ENGINEER)
    r = client.post("/api/v1/auth/guest")
    assert r.status_code == 200, r.text
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"})
    assert me.status_code == 200
    assert me.json()["email"] == "guest@riverbend-demo.io" and me.json()["role"] == "engineer"


def test_guest_access_refuses_an_admin_account(client, db_session, org_and_admin, guest_enabled):
    _add_user(db_session, org_and_admin[0], "guest@riverbend-demo.io", UserRole.ADMIN)
    assert client.post("/api/v1/auth/guest").status_code == 404


def test_guest_access_with_no_guest_account_is_404(client, org_and_admin, guest_enabled):
    assert client.post("/api/v1/auth/guest").status_code == 404
