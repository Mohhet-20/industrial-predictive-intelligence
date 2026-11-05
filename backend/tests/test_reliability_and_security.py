"""Production reliability & security hardening tests.

Covers what app/core/config.py, app/core/middleware.py, app/core/rate_limit.py,
app/jobs/runner.py, and app/api/v1/system.py actually do, including failure
paths (a hung job, too many login attempts, an oversized request body, a
config that would be unsafe in production) — not just the happy path.
"""
import asyncio

import pytest
from pydantic import ValidationError

from app.core.config import _DEV_DEFAULT_JWT_SECRET, Settings
from app.core.rate_limit import check_and_record, reset_for_tests
from app.core.security import hash_password
from app.jobs.runner import DuplicateJobError, JobRunner
from app.models.job import BackgroundJob, JobStatus, JobType
from app.models.user import User, UserRole


# --------------------------------------------------------------------------
# — config/environment validation
# --------------------------------------------------------------------------

def test_production_config_rejects_default_jwt_secret():
    # jwt_secret_key is passed explicitly (rather than omitted) so this
    # test exercises the exact dev-default value regardless of whatever
    # JWT_SECRET_KEY the test process's own environment happens to have
    # set (conftest.py sets one, precisely so normal tests don't hit this
    # production check) — an explicit init kwarg always outranks the
    # environment in pydantic-settings' source precedence.
    with pytest.raises(ValidationError, match="JWT_SECRET_KEY is still the checked-in development default"):
        Settings(
            app_env="production",
            jwt_secret_key=_DEV_DEFAULT_JWT_SECRET,
            database_url="sqlite:////tmp/config-test.db",
            cors_origins="https://app.example.com",
        )


def test_production_config_rejects_wildcard_cors():
    with pytest.raises(ValidationError, match="CORS_ORIGINS contains"):
        Settings(
            app_env="production",
            jwt_secret_key="a-real-random-production-secret-value-not-the-default",
            database_url="sqlite:////tmp/config-test.db",
            cors_origins="*",
        )


def test_production_config_rejects_in_memory_database():
    with pytest.raises(ValidationError, match="in-memory SQLite database"):
        Settings(
            app_env="production",
            jwt_secret_key="a-real-random-production-secret-value-not-the-default",
            database_url="sqlite:///:memory:",
            cors_origins="https://app.example.com",
        )


def test_production_config_accepts_safe_values():
    settings = Settings(
        app_env="production",
        jwt_secret_key="a-real-random-production-secret-value-not-the-default",
        database_url="sqlite:////tmp/config-test.db",
        cors_origins="https://app.example.com",
    )
    assert settings.is_production is True


def test_development_config_is_unaffected_by_production_checks():
    # The whole rest of this test suite runs on exactly this: dev/test
    # settings with the default JWT secret must NOT raise.
    settings = Settings(app_env="development", database_url="sqlite:///:memory:")
    assert settings.is_production is False


def test_short_jwt_secret_always_rejected_regardless_of_environment():
    with pytest.raises(ValidationError, match="at least 16 characters"):
        Settings(app_env="development", jwt_secret_key="short")


# --------------------------------------------------------------------------
# — background job reliability: timeout, duplicate protection, no-handler
# --------------------------------------------------------------------------

def test_hung_job_times_out_and_is_marked_failed_not_left_running(db_session, org_and_admin):
    org, _ = org_and_admin
    runner = JobRunner(max_concurrency=2, default_timeout_seconds=1)

    async def _hangs_forever(job_id, report_progress, payload):
        await asyncio.sleep(10)
        return {"ok": True}  # pragma: no cover — never reached

    runner.register(JobType.DATA_QUALITY_SCAN, _hangs_forever)

    async def _drive():
        job_id = runner.submit(organization_id=org.id, job_type=JobType.DATA_QUALITY_SCAN, payload={})
        await asyncio.sleep(1.5)  # longer than the 1s timeout
        return job_id

    job_id = asyncio.run(_drive())

    job = db_session.get(BackgroundJob, job_id)
    assert job.status == JobStatus.FAILED
    assert "timeout" in job.error_message.lower()
    assert job.finished_at is not None


def test_job_with_no_registered_handler_fails_explicitly_not_silently(db_session, org_and_admin):
    org, _ = org_and_admin
    runner = JobRunner(max_concurrency=2, default_timeout_seconds=5)
    # Deliberately register nothing for DETECTOR_TRAINING.

    async def _drive():
        job_id = runner.submit(organization_id=org.id, job_type=JobType.DETECTOR_TRAINING, payload={})
        await asyncio.sleep(0.2)
        return job_id

    job_id = asyncio.run(_drive())
    job = db_session.get(BackgroundJob, job_id)
    assert job.status == JobStatus.FAILED
    assert "No handler registered" in job.error_message


def test_duplicate_dedup_key_rejected_while_active_then_allowed_after_completion(db_session, org_and_admin):
    org, _ = org_and_admin
    runner = JobRunner(max_concurrency=2, default_timeout_seconds=5)

    async def _slow_but_finite(job_id, report_progress, payload):
        await asyncio.sleep(0.4)
        return {"ok": True}

    runner.register(JobType.DATA_QUALITY_SCAN, _slow_but_finite)

    async def _drive():
        first_id = runner.submit(
            organization_id=org.id, job_type=JobType.DATA_QUALITY_SCAN, payload={}, dedup_key="scan-asset-1"
        )
        with pytest.raises(DuplicateJobError) as exc_info:
            runner.submit(
                organization_id=org.id, job_type=JobType.DATA_QUALITY_SCAN, payload={}, dedup_key="scan-asset-1"
            )
        assert exc_info.value.existing_job_id == first_id

        # A different dedup_key for the same org/type is NOT a duplicate.
        other_id = runner.submit(
            organization_id=org.id, job_type=JobType.DATA_QUALITY_SCAN, payload={}, dedup_key="scan-asset-2"
        )
        assert other_id != first_id

        await asyncio.sleep(0.7)  # let both finish

        # Once the first job has completed, its dedup_key is free again.
        third_id = runner.submit(
            organization_id=org.id, job_type=JobType.DATA_QUALITY_SCAN, payload={}, dedup_key="scan-asset-1"
        )
        await asyncio.sleep(0.6)
        return first_id, third_id

    first_id, third_id = asyncio.run(_drive())
    assert first_id != third_id
    assert db_session.get(BackgroundJob, first_id).status == JobStatus.SUCCEEDED
    assert db_session.get(BackgroundJob, third_id).status == JobStatus.SUCCEEDED


# --------------------------------------------------------------------------
# — security hardening: rate limiting, headers, timing, body size
# --------------------------------------------------------------------------

def test_rate_limiter_blocks_after_max_attempts_and_recovers_with_a_new_key():
    reset_for_tests()
    for _ in range(5):
        allowed, _ = check_and_record("1.2.3.4:someone@example.com", max_attempts=5, window_seconds=60)
        assert allowed is True
    blocked, retry_after = check_and_record("1.2.3.4:someone@example.com", max_attempts=5, window_seconds=60)
    assert blocked is False
    assert retry_after > 0
    # A different key (different IP or email) is unaffected.
    allowed, _ = check_and_record("9.9.9.9:someone-else@example.com", max_attempts=5, window_seconds=60)
    assert allowed is True


def test_login_endpoint_rate_limited_after_repeated_failed_attempts(client, org_and_admin):
    org, admin = org_and_admin
    for _ in range(5):
        resp = client.post("/api/v1/auth/login", json={"email": admin.email, "password": "wrong-password"})
        assert resp.status_code == 401
    sixth = client.post("/api/v1/auth/login", json={"email": admin.email, "password": "wrong-password"})
    assert sixth.status_code == 429
    body = sixth.json()
    assert body["error"]["code"] == "rate_limited"
    assert "retry_after_seconds" in body["error"]["details"]

    # A correct password is ALSO blocked once the window is exhausted —
    # the limiter guards login attempts, not just failures, which is the
    # correct anti-brute-force behavior (an attacker who eventually finds
    # the right password after N wrong guesses must not be let through).
    seventh = client.post("/api/v1/auth/login", json={"email": admin.email, "password": "AdminPass123!"})
    assert seventh.status_code == 429


def test_unknown_email_and_wrong_password_return_identical_error_shape(client):
    unknown = client.post("/api/v1/auth/login", json={"email": "nobody@nowhere.example", "password": "x"})
    assert unknown.status_code == 401
    assert unknown.json()["error"]["code"] == "authentication_failed"
    assert unknown.json()["error"]["message"] == "Incorrect email or password."


def test_security_headers_present_on_every_response(client):
    resp = client.get("/api/v1/health")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "no-referrer"


def test_auth_responses_are_not_cached(client):
    resp = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401
    assert resp.headers.get("cache-control") == "no-store"


def test_oversized_request_body_rejected_with_413(client):
    # ~11MB password value, comfortably over the 10MB default limit.
    huge_payload = {"email": "someone@example.com", "password": "x" * (11 * 1024 * 1024)}
    resp = client.post("/api/v1/auth/login", json=huge_payload)
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "request_too_large"


# --------------------------------------------------------------------------
# — observability: GET /system/metrics is real, authorized, and bounded
# --------------------------------------------------------------------------

def test_system_metrics_reflects_real_recorded_requests(client, auth_headers):
    client.get("/api/v1/health")  # generate at least one real recorded request
    resp = client.get("/api/v1/system/metrics", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["request_metrics"]["total_requests_recorded"] >= 1
    assert "background_jobs" in body
    assert "data_quality" in body
    assert "anomaly_detection" in body
    assert "ingestion" in body


def test_system_metrics_denied_to_viewer_role(client, db_session, org_and_admin):
    from app.services import auth_service as auth_service_module

    org, _ = org_and_admin
    viewer = User(
        organization_id=org.id,
        email="viewer@reliability-tests.example",
        hashed_password=hash_password("ViewerPass123!"),
        full_name="Viewer",
        role=UserRole.VIEWER,
    )
    db_session.add(viewer)
    db_session.commit()
    viewer_headers = {"Authorization": f"Bearer {auth_service_module.issue_token(viewer)}"}

    resp = client.get("/api/v1/system/metrics", headers=viewer_headers)
    assert resp.status_code == 403


def test_system_metrics_requires_authentication(client):
    resp = client.get("/api/v1/system/metrics")
    assert resp.status_code == 401


# --------------------------------------------------------------------------
# — readiness must report real, non-blocking diagnostics
# --------------------------------------------------------------------------

def test_readiness_reports_ok_and_schema_version_on_a_healthy_db(client):
    resp = client.get("/api/v1/health/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["schema_version"] is not None
    assert body["checks"]["database"] == "ok"


def test_liveness_is_unaffected_by_a_broken_database_but_readiness_reports_it(client):
    # Liveness must answer even if the DB dependency would fail — that is
    # exactly the distinction between liveness and readiness. Break
    # the get_db dependency the same way the `client` fixture wires it, and
    # confirm liveness (no DB dependency at all) is unaffected while
    # readiness (which explicitly depends on get_db) reports the failure
    # rather than lying about being ready.
    from fastapi.testclient import TestClient

    from app.db.session import get_db
    from app.main import app

    def _broken_get_db():
        raise RuntimeError("simulated database connection failure")
        yield  # pragma: no cover — unreachable, keeps this a generator

    app.dependency_overrides[get_db] = _broken_get_db
    try:
        live_resp = client.get("/api/v1/health")
        assert live_resp.status_code == 200
        assert live_resp.json() == {"status": "ok"}

        # A real client (browser, curl, a load balancer's health probe)
        # gets a 500 JSON response here, not a raised Python exception —
        # use raise_server_exceptions=False so this test observes exactly
        # that response instead of TestClient's default debugging
        # behavior of re-raising the underlying exception in-process.
        with TestClient(app, raise_server_exceptions=False) as strict_client:
            ready_resp = strict_client.get("/api/v1/health/ready")
        assert ready_resp.status_code == 500
        assert ready_resp.json()["error"]["code"] == "internal_error"
    finally:
        # dependency_overrides is process-global; restore it so later tests
        # in this session aren't affected (the `client` fixture's own
        # teardown also clears it, but don't rely on ordering here).
        app.dependency_overrides.pop(get_db, None)
