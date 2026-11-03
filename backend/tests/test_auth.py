def test_register_organization(client):
    resp = client.post(
        "/api/v1/auth/register-organization",
        json={
            "organization_name": "Acme Industrial",
            "admin_email": "admin@acme.example",
            "admin_password": "SuperSecret123!",
            "admin_full_name": "Ada Admin",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["email"] == "admin@acme.example"
    assert body["role"] == "admin"


def test_register_duplicate_organization_conflicts(client):
    payload = {
        "organization_name": "Acme Industrial",
        "admin_email": "admin@acme.example",
        "admin_password": "SuperSecret123!",
        "admin_full_name": "Ada Admin",
    }
    first = client.post("/api/v1/auth/register-organization", json=payload)
    assert first.status_code == 201
    second = client.post("/api/v1/auth/register-organization", json=payload)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "conflict"


def test_login_success_and_failure(client):
    client.post(
        "/api/v1/auth/register-organization",
        json={
            "organization_name": "Acme Industrial",
            "admin_email": "admin@acme.example",
            "admin_password": "SuperSecret123!",
            "admin_full_name": "Ada Admin",
        },
    )
    good = client.post("/api/v1/auth/login", json={"email": "admin@acme.example", "password": "SuperSecret123!"})
    assert good.status_code == 200
    assert "access_token" in good.json()

    bad = client.post("/api/v1/auth/login", json={"email": "admin@acme.example", "password": "wrong"})
    assert bad.status_code == 401
    assert bad.json()["error"]["code"] == "authentication_failed"


def test_me_requires_auth(client):
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 401


def test_me_with_valid_token(client, auth_headers):
    resp = client.get("/api/v1/auth/me", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["email"] == "admin@test-org.example"


def test_no_error_details_leak_for_internal_errors(client):
    # Malformed auth header should not leak internals — just a stable error shape.
    resp = client.get("/api/v1/auth/me", headers={"Authorization": "not-a-bearer-token"})
    assert resp.status_code == 401
    body = resp.json()
    assert set(body["error"].keys()) >= {"code", "message", "request_id"}
