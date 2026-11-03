def test_create_and_list_assets(client, auth_headers):
    resp = client.post(
        "/api/v1/assets",
        headers=auth_headers,
        json={"code": "PUMP-99", "name": "Test Pump", "asset_type": "pump", "location": "Site A"},
    )
    assert resp.status_code == 201
    asset = resp.json()
    assert asset["code"] == "PUMP-99"
    assert asset["sensor_count"] == 0

    listing = client.get("/api/v1/assets", headers=auth_headers)
    assert listing.status_code == 200
    assert listing.json()["total"] == 1


def test_duplicate_asset_code_conflicts(client, auth_headers):
    payload = {"code": "PUMP-99", "name": "Test Pump", "asset_type": "pump"}
    client.post("/api/v1/assets", headers=auth_headers, json=payload)
    second = client.post("/api/v1/assets", headers=auth_headers, json=payload)
    assert second.status_code == 409


def test_create_asset_requires_auth(client):
    resp = client.post("/api/v1/assets", json={"code": "X", "name": "X", "asset_type": "x"})
    assert resp.status_code == 401


def test_viewer_role_cannot_create_asset(client, db_session, org_and_admin):
    from app.core.security import hash_password
    from app.models.user import User, UserRole
    from app.services import auth_service

    org, _ = org_and_admin
    viewer = User(
        organization_id=org.id,
        email="viewer@test-org.example",
        hashed_password=hash_password("ViewerPass123!"),
        full_name="Viewer",
        role=UserRole.VIEWER,
    )
    db_session.add(viewer)
    db_session.commit()
    token = auth_service.issue_token(viewer)

    resp = client.post(
        "/api/v1/assets",
        headers={"Authorization": f"Bearer {token}"},
        json={"code": "PUMP-01", "name": "Pump", "asset_type": "pump"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "not_authorized"


def test_create_and_list_sensors(client, auth_headers):
    asset_resp = client.post(
        "/api/v1/assets", headers=auth_headers, json={"code": "PUMP-05", "name": "Pump", "asset_type": "pump"}
    )
    asset_id = asset_resp.json()["id"]

    sensor_resp = client.post(
        f"/api/v1/assets/{asset_id}/sensors",
        headers=auth_headers,
        json={
            "code": "temp",
            "name": "Temperature",
            "measurement_type": "temperature",
            "unit": "celsius",
            "expected_min": 0,
            "expected_max": 100,
        },
    )
    assert sensor_resp.status_code == 201

    listing = client.get(f"/api/v1/assets/{asset_id}/sensors", headers=auth_headers)
    assert listing.status_code == 200
    assert listing.json()["total"] == 1


def test_sensor_invalid_range_rejected(client, auth_headers):
    asset_resp = client.post(
        "/api/v1/assets", headers=auth_headers, json={"code": "PUMP-06", "name": "Pump", "asset_type": "pump"}
    )
    asset_id = asset_resp.json()["id"]
    resp = client.post(
        f"/api/v1/assets/{asset_id}/sensors",
        headers=auth_headers,
        json={"code": "temp", "name": "Temp", "measurement_type": "temperature", "unit": "celsius", "expected_min": 100, "expected_max": 0},
    )
    assert resp.status_code == 422


def test_get_unknown_asset_404s(client, auth_headers):
    resp = client.get("/api/v1/assets/does-not-exist", headers=auth_headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
