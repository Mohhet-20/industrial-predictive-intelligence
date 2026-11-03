import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-not-for-production-use-only")

import pytest
from fastapi.testclient import TestClient

from app.db.base import Base
import app.models  # noqa: F401 registers all models
from app.db.session import SessionLocal, engine, get_db
from app.main import app
from app.models.asset import Asset
from app.models.sensor import Sensor
from app.services import auth_service

# Deliberately reuse app.db.session's own engine/SessionLocal (StaticPool'd
# to a single in-memory connection — see app/db/session.py) rather than
# building a second, parallel test engine. Background jobs (app.jobs.runner)
# open sessions directly via SessionLocal, bypassing FastAPI's
# Depends(get_db); a separate test engine would leave the job runner
# writing into a database the tests can't see (and that has no schema).
# Sharing one engine means API requests, direct db_session fixture use, and
# background-job sessions all see the same data, exactly as they do against
# a real file-based or Postgres database in production.


def _stamp_alembic_head() -> None:
    """Tests build schema via Base.metadata.create_all (fast, no migration
    replay needed) rather than `alembic upgrade head` — but that means the
    test DB has no alembic_version row, which would make GET /health/ready
    report "degraded" for a reason that has nothing to do with what any
    given test is checking. Stamp the real current head (read from the
    actual migration chain, not hardcoded) so the test DB honestly reflects
    "migrations applied", matching what a real deployment looks like after
    `alembic upgrade head`."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    backend_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = Config(os.path.join(backend_root, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(backend_root, "alembic"))
    head_revision = ScriptDirectory.from_config(cfg).get_current_head()

    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        )
        conn.exec_driver_sql("DELETE FROM alembic_version")
        conn.exec_driver_sql(f"INSERT INTO alembic_version (version_num) VALUES ('{head_revision}')")


@pytest.fixture(autouse=True)
def _reset_schema():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    _stamp_alembic_head()
    yield


@pytest.fixture(autouse=True)
def _reset_rate_limit_state():
    # app.core.rate_limit keeps its attempt counters in a module-level dict
    # (real, deliberately in-process state — see that module's docstring),
    # which would otherwise leak login-attempt counts between tests that
    # happen to reuse the same admin email (most fixtures do:
    # admin@test-org.example) and the same TestClient "IP" (always
    # "testclient"). Reset it before every test so tests are independent.
    from app.core import rate_limit as _rate_limit_module

    _rate_limit_module.reset_for_tests()
    yield


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db_session):
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def org_and_admin(db_session):
    org, admin = auth_service.register_organization(
        db_session,
        organization_name="Test Org",
        admin_email="admin@test-org.example",
        admin_password="AdminPass123!",
        admin_full_name="Test Admin",
    )
    return org, admin


@pytest.fixture
def auth_headers(org_and_admin):
    org, admin = org_and_admin
    token = auth_service.issue_token(admin)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def sample_asset_and_sensor(db_session, org_and_admin):
    org, _ = org_and_admin
    asset = Asset(organization_id=org.id, code="PUMP-01", name="Test Pump", asset_type="pump", location="Test Site")
    db_session.add(asset)
    db_session.flush()
    sensor = Sensor(
        asset_id=asset.id,
        code="temp",
        name="Temperature",
        measurement_type="temperature",
        unit="celsius",
        expected_min=0.0,
        expected_max=100.0,
        expected_interval_seconds=300.0,
    )
    db_session.add(sensor)
    db_session.commit()
    db_session.refresh(asset)
    db_session.refresh(sensor)
    return asset, sensor
