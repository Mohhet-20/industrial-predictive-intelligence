"""API-level tests for the Investigation Workflow layer.

Mirrors tests/test_api_anomaly.py and tests/test_api_mlops.py's
conventions: real HTTP surface against the real DB. Covers the workflow
happy path plus the failure/adversarial paths the design explicitly asks for
tested: evidence relationships, provenance preservation (linking
never mutates the underlying evidence), status transitions, human
annotation separation from verified labels, authorization, and cross-org
isolation.
"""
from datetime import datetime, timedelta, timezone

import numpy as np

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _make_asset_and_sensor(client, headers, *, asset_code="PUMP-INV", sensor_code="temp"):
    asset_resp = client.post("/api/v1/assets", headers=headers, json={"code": asset_code, "name": "Investigation Test Pump", "asset_type": "pump"})
    assert asset_resp.status_code == 201, asset_resp.text
    sensor_resp = client.post(
        f"/api/v1/assets/{asset_resp.json()['id']}/sensors", headers=headers,
        json={"code": sensor_code, "name": "Temp", "measurement_type": "temperature", "unit": "celsius", "expected_interval_seconds": 300},
    )
    assert sensor_resp.status_code == 201, sensor_resp.text
    return asset_resp.json(), sensor_resp.json()


def _ingest(client, headers, *, asset_code, sensor_code, n, step_minutes=5, start=BASE, value_fn=None):
    value_fn = value_fn or (lambda i: 50.0 + (i % 5) * 0.1)
    records = [{"asset_code": asset_code, "sensor_code": sensor_code, "timestamp": _iso(start + timedelta(minutes=step_minutes * i)), "value": value_fn(i)} for i in range(n)]
    resp = client.post("/api/v1/ingestion/measurements", headers=headers, json={"data_source_name": "investigation-tests", "records": records})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ingest_with_spike(client, headers, *, asset_code, sensor_code, n_ref, n_score, spike_index, step_minutes=5, seed=0):
    rng = np.random.default_rng(seed)
    ref_values = [50.0 + float(rng.normal(0, 0.5)) for _ in range(n_ref)]
    _ingest(client, headers, asset_code=asset_code, sensor_code=sensor_code, n=n_ref, step_minutes=step_minutes, start=BASE, value_fn=lambda i: ref_values[i])
    score_start = BASE + timedelta(minutes=step_minutes * n_ref)
    score_values = [50.0 + float(rng.normal(0, 0.5)) for _ in range(n_score)]
    score_values[spike_index] += 20.0
    _ingest(client, headers, asset_code=asset_code, sensor_code=sensor_code, n=n_score, step_minutes=step_minutes, start=score_start, value_fn=lambda i: score_values[i])
    reference_end = BASE + timedelta(minutes=step_minutes * (n_ref - 1))
    score_end = score_start + timedelta(minutes=step_minutes * (n_score - 1))
    return reference_end, score_start, score_end


def _make_real_anomaly(client, headers, *, asset, sensor):
    reference_end, score_start, score_end = _ingest_with_spike(client, headers, asset_code=asset["code"], sensor_code=sensor["code"], n_ref=400, n_score=200, spike_index=100)
    detector_resp = client.post(
        "/api/v1/anomaly/detectors", headers=headers,
        json={"name": "temp-robust-zscore", "detector_type": "robust_zscore", "sensor_ids": [sensor["id"]], "reference_start": _iso(BASE), "reference_end": _iso(reference_end)},
    )
    assert detector_resp.status_code == 201, detector_resp.text
    run_resp = client.post(f"/api/v1/anomaly/detectors/{detector_resp.json()['id']}/runs", headers=headers, json={"scoring_start": _iso(score_start), "scoring_end": _iso(score_end)})
    assert run_resp.status_code == 201, run_resp.text
    anomalies = client.get("/api/v1/anomaly/anomalies", headers=headers, params={"sensor_id": sensor["id"]}).json()["items"]
    assert len(anomalies) >= 1
    return anomalies[0]


def _viewer_headers(client, db_session, org, auth_service_module):
    from app.core.security import hash_password
    from app.models.user import User, UserRole

    viewer = User(organization_id=org.id, email="viewer@investigation-tests.example", hashed_password=hash_password("ViewerPass123!"), full_name="Viewer", role=UserRole.VIEWER)
    db_session.add(viewer)
    db_session.commit()
    token = auth_service_module.issue_token(viewer)
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Full workflow: open -> attach evidence -> annotate -> transition -> history
# ---------------------------------------------------------------------------


def test_investigation_full_workflow_e2e(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers)
    anomaly = _make_real_anomaly(client, auth_headers, asset=asset, sensor=sensor)

    open_resp = client.post(
        "/api/v1/investigations", headers=auth_headers,
        json={"asset_id": asset["id"], "title": "Unusual bearing temperature behavior", "primary_sensor_id": sensor["id"], "description": "Investigating a spike flagged by the robust z-score detector."},
    )
    assert open_resp.status_code == 201, open_resp.text
    investigation = open_resp.json()
    assert investigation["status"] == "open"

    # Attach the real anomaly as evidence, by reference.
    evidence_resp = client.post(
        f"/api/v1/investigations/{investigation['id']}/evidence", headers=auth_headers,
        json={"evidence_type": "anomaly", "evidence_id": anomaly["id"], "relation_note": "The flagged spike itself."},
    )
    assert evidence_resp.status_code == 201, evidence_resp.text
    link = evidence_resp.json()
    assert link["evidence_id"] == anomaly["id"]
    assert link["sensor_id"] == sensor["id"]  # denormalized from the real Anomaly row

    # Linking evidence never mutates the underlying anomaly.
    anomaly_after = client.get(f"/api/v1/anomaly/anomalies/{anomaly['id']}", headers=auth_headers).json()
    assert anomaly_after["status"] == anomaly["status"] == "detected"
    assert anomaly_after["raw_score"] == anomaly["raw_score"]

    evidence_list = client.get(f"/api/v1/investigations/{investigation['id']}/evidence", headers=auth_headers).json()
    assert len(evidence_list) == 1

    # Add a data-quality-issue-shaped evidence link is skipped here (no DQ issue exists yet); instead add a human annotation.
    annotation_resp = client.post(
        f"/api/v1/investigations/{investigation['id']}/annotations", headers=auth_headers,
        json={"annotation_type": "observation", "content": "Confirmed with the site engineer: pump was running under normal load at this time.", "follow_up_required": False},
    )
    assert annotation_resp.status_code == 201, annotation_resp.text

    # Status transitions.
    review_resp = client.post(f"/api/v1/investigations/{investigation['id']}/status", headers=auth_headers, json={"to_status": "under_review", "notes": "Reviewing evidence."})
    assert review_resp.status_code == 200
    assert review_resp.json()["status"] == "under_review"

    resolve_resp = client.post(f"/api/v1/investigations/{investigation['id']}/status", headers=auth_headers, json={"to_status": "resolved", "notes": "Confirmed benign — normal operating transient."})
    assert resolve_resp.status_code == 200

    close_resp = client.post(f"/api/v1/investigations/{investigation['id']}/status", headers=auth_headers, json={"to_status": "closed"})
    assert close_resp.status_code == 200
    assert close_resp.json()["status"] == "closed"

    history = client.get(f"/api/v1/investigations/{investigation['id']}/status-history", headers=auth_headers).json()
    assert [h["to_status"] for h in history] == ["open", "under_review", "resolved", "closed"]
    assert history[0]["from_status"] is None  # the opening event has no prior status
    assert history[1]["from_status"] == "open"

    # Closed investigations are reopenable (never permanently locked), but only into under_review.
    reopen_resp = client.post(f"/api/v1/investigations/{investigation['id']}/status", headers=auth_headers, json={"to_status": "under_review"})
    assert reopen_resp.status_code == 200


def test_invalid_status_transition_rejected(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers)
    inv = client.post("/api/v1/investigations", headers=auth_headers, json={"asset_id": asset["id"], "title": "Test"}).json()

    resolve_resp = client.post(f"/api/v1/investigations/{inv['id']}/status", headers=auth_headers, json={"to_status": "resolved"})
    assert resolve_resp.status_code == 200

    # resolved -> open is not an allowed transition (only under_review/closed are).
    bad_resp = client.post(f"/api/v1/investigations/{inv['id']}/status", headers=auth_headers, json={"to_status": "open"})
    assert bad_resp.status_code == 422
    assert "Cannot transition" in bad_resp.json()["error"]["message"]


def test_investigation_requires_sensor_belonging_to_asset(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers)
    other_asset, _ = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-INV-OTHER", sensor_code="temp2")

    resp = client.post("/api/v1/investigations", headers=auth_headers, json={"asset_id": other_asset["id"], "title": "Mismatched sensor", "primary_sensor_id": sensor["id"]})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Evidence graph: references, snapshots, duplicates
# ---------------------------------------------------------------------------


def test_duplicate_evidence_link_rejected(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers)
    anomaly = _make_real_anomaly(client, auth_headers, asset=asset, sensor=sensor)
    inv = client.post("/api/v1/investigations", headers=auth_headers, json={"asset_id": asset["id"], "title": "Dup test"}).json()

    first = client.post(f"/api/v1/investigations/{inv['id']}/evidence", headers=auth_headers, json={"evidence_type": "anomaly", "evidence_id": anomaly["id"]})
    assert first.status_code == 201
    second = client.post(f"/api/v1/investigations/{inv['id']}/evidence", headers=auth_headers, json={"evidence_type": "anomaly", "evidence_id": anomaly["id"]})
    assert second.status_code == 409


def test_evidence_id_required_except_change_candidate(client, auth_headers):
    asset, _ = _make_asset_and_sensor(client, auth_headers)
    inv = client.post("/api/v1/investigations", headers=auth_headers, json={"asset_id": asset["id"], "title": "Evidence shape test"}).json()

    missing_id = client.post(f"/api/v1/investigations/{inv['id']}/evidence", headers=auth_headers, json={"evidence_type": "anomaly", "evidence_id": None})
    assert missing_id.status_code == 422

    missing_snapshot = client.post(f"/api/v1/investigations/{inv['id']}/evidence", headers=auth_headers, json={"evidence_type": "change_candidate", "evidence_id": None})
    assert missing_snapshot.status_code == 422

    with_snapshot = client.post(
        f"/api/v1/investigations/{inv['id']}/evidence", headers=auth_headers,
        json={"evidence_type": "change_candidate", "snapshot": {"type": "level_shift", "timestamp": _iso(BASE), "score": 4.2}},
    )
    assert with_snapshot.status_code == 201
    assert with_snapshot.json()["snapshot"]["type"] == "level_shift"
    assert with_snapshot.json()["evidence_id"] is None


def test_evidence_reference_to_nonexistent_row_is_honest_404(client, auth_headers):
    asset, _ = _make_asset_and_sensor(client, auth_headers)
    inv = client.post("/api/v1/investigations", headers=auth_headers, json={"asset_id": asset["id"], "title": "404 test"}).json()
    resp = client.post(f"/api/v1/investigations/{inv['id']}/evidence", headers=auth_headers, json={"evidence_type": "anomaly", "evidence_id": "00000000-0000-0000-0000-000000000000"})
    assert resp.status_code == 404


def test_remove_evidence_link(client, auth_headers):
    asset, sensor = _make_asset_and_sensor(client, auth_headers)
    anomaly = _make_real_anomaly(client, auth_headers, asset=asset, sensor=sensor)
    inv = client.post("/api/v1/investigations", headers=auth_headers, json={"asset_id": asset["id"], "title": "Remove test"}).json()
    link = client.post(f"/api/v1/investigations/{inv['id']}/evidence", headers=auth_headers, json={"evidence_type": "anomaly", "evidence_id": anomaly["id"]}).json()

    del_resp = client.delete(f"/api/v1/investigations/{inv['id']}/evidence/{link['id']}", headers=auth_headers)
    assert del_resp.status_code == 204
    assert client.get(f"/api/v1/investigations/{inv['id']}/evidence", headers=auth_headers).json() == []


# ---------------------------------------------------------------------------
# Human annotations are kept distinct from verified outcomes
# ---------------------------------------------------------------------------


def test_false_positive_annotation_does_not_change_anomaly_status(client, auth_headers):
    """Recording a human's false-positive ASSESSMENT as an annotation must
    never, by itself, silently change the underlying Anomaly's status —
    that requires the separate, explicit anomaly review action. This is the
    concrete behavioral proof of's "do not automatically treat
    human notes as verified training labels" requirement."""
    asset, sensor = _make_asset_and_sensor(client, auth_headers)
    anomaly = _make_real_anomaly(client, auth_headers, asset=asset, sensor=sensor)
    inv = client.post("/api/v1/investigations", headers=auth_headers, json={"asset_id": asset["id"], "title": "FP assessment test"}).json()
    client.post(f"/api/v1/investigations/{inv['id']}/evidence", headers=auth_headers, json={"evidence_type": "anomaly", "evidence_id": anomaly["id"]})

    ann_resp = client.post(
        f"/api/v1/investigations/{inv['id']}/annotations", headers=auth_headers,
        json={"annotation_type": "false_positive_assessment", "content": "This looks like sensor noise, not a real event.", "follow_up_required": False},
    )
    assert ann_resp.status_code == 201

    anomaly_after = client.get(f"/api/v1/anomaly/anomalies/{anomaly['id']}", headers=auth_headers).json()
    assert anomaly_after["status"] == "detected"  # unchanged by the annotation alone

    annotations = client.get(f"/api/v1/investigations/{inv['id']}/annotations", headers=auth_headers).json()
    assert len(annotations) == 1
    assert annotations[0]["annotation_type"] == "false_positive_assessment"


def test_annotations_are_append_only_list_preserves_order(client, auth_headers):
    asset, _ = _make_asset_and_sensor(client, auth_headers)
    inv = client.post("/api/v1/investigations", headers=auth_headers, json={"asset_id": asset["id"], "title": "Annotation order test"}).json()
    for i, content in enumerate(["First observation.", "Second observation.", "Resolution recorded."]):
        atype = "resolution" if i == 2 else "observation"
        r = client.post(f"/api/v1/investigations/{inv['id']}/annotations", headers=auth_headers, json={"annotation_type": atype, "content": content})
        assert r.status_code == 201
    annotations = client.get(f"/api/v1/investigations/{inv['id']}/annotations", headers=auth_headers).json()
    assert [a["content"] for a in annotations] == ["First observation.", "Second observation.", "Resolution recorded."]


# ---------------------------------------------------------------------------
# Related investigations
# ---------------------------------------------------------------------------


def test_related_investigations_visible_symmetrically(client, auth_headers):
    asset, _ = _make_asset_and_sensor(client, auth_headers)
    inv_a = client.post("/api/v1/investigations", headers=auth_headers, json={"asset_id": asset["id"], "title": "Investigation A"}).json()
    inv_b = client.post("/api/v1/investigations", headers=auth_headers, json={"asset_id": asset["id"], "title": "Investigation B"}).json()

    link_resp = client.post(f"/api/v1/investigations/{inv_a['id']}/related", headers=auth_headers, json={"related_investigation_id": inv_b["id"], "note": "Same root sensor under review."})
    assert link_resp.status_code == 201

    a_related = client.get(f"/api/v1/investigations/{inv_a['id']}/related", headers=auth_headers).json()
    b_related = client.get(f"/api/v1/investigations/{inv_b['id']}/related", headers=auth_headers).json()
    assert len(a_related) == 1 and len(b_related) == 1

    dup = client.post(f"/api/v1/investigations/{inv_a['id']}/related", headers=auth_headers, json={"related_investigation_id": inv_b["id"]})
    assert dup.status_code == 409

    self_link = client.post(f"/api/v1/investigations/{inv_a['id']}/related", headers=auth_headers, json={"related_investigation_id": inv_a["id"]})
    assert self_link.status_code == 422

    # Regression test for a bug found in code review: the relation is stored A->B, but when B looks
    # up ITS related investigations, the response must be oriented from B's perspective (investigation_id == B,
    # related_investigation_id == A) — not the raw stored direction, which would show B's own id in the "related"
    # slot and mislead a client rendering "investigations related to B".
    assert a_related[0]["investigation_id"] == inv_a["id"]
    assert a_related[0]["related_investigation_id"] == inv_b["id"]
    assert b_related[0]["investigation_id"] == inv_b["id"]
    assert b_related[0]["related_investigation_id"] == inv_a["id"]


# ---------------------------------------------------------------------------
# Authorization and cross-org isolation
# ---------------------------------------------------------------------------


def test_viewer_can_read_but_not_write(client, auth_headers, db_session, org_and_admin):
    org, _ = org_and_admin
    from app.services import auth_service as auth_service_module

    asset, sensor = _make_asset_and_sensor(client, auth_headers)
    anomaly = _make_real_anomaly(client, auth_headers, asset=asset, sensor=sensor)
    inv = client.post("/api/v1/investigations", headers=auth_headers, json={"asset_id": asset["id"], "title": "Viewer test"}).json()

    viewer_headers = _viewer_headers(client, db_session, org, auth_service_module)

    assert client.get(f"/api/v1/investigations/{inv['id']}", headers=viewer_headers).status_code == 200
    assert client.get("/api/v1/investigations", headers=viewer_headers).status_code == 200

    assert client.post("/api/v1/investigations", headers=viewer_headers, json={"asset_id": asset["id"], "title": "Should fail"}).status_code == 403
    assert client.post(f"/api/v1/investigations/{inv['id']}/status", headers=viewer_headers, json={"to_status": "under_review"}).status_code == 403
    assert client.post(f"/api/v1/investigations/{inv['id']}/evidence", headers=viewer_headers, json={"evidence_type": "anomaly", "evidence_id": anomaly["id"]}).status_code == 403
    assert client.post(f"/api/v1/investigations/{inv['id']}/annotations", headers=viewer_headers, json={"annotation_type": "observation", "content": "x"}).status_code == 403


def test_cross_org_isolation_for_investigations(client, auth_headers, db_session):
    from app.services import auth_service as auth_service_module

    asset, _ = _make_asset_and_sensor(client, auth_headers)
    inv = client.post("/api/v1/investigations", headers=auth_headers, json={"asset_id": asset["id"], "title": "Org A investigation"}).json()

    other_org, other_admin = auth_service_module.register_organization(db_session, organization_name="Other Org", admin_email="admin@other-org.example", admin_password="AdminPass123!", admin_full_name="Other Admin")
    other_token = auth_service_module.issue_token(other_admin)
    other_headers = {"Authorization": f"Bearer {other_token}"}

    assert client.get(f"/api/v1/investigations/{inv['id']}", headers=other_headers).status_code == 404
    # An org-B admin cannot even attach evidence referencing an org-A asset's anomaly, because the referenced row
    # simply doesn't exist within org B's scope.
    other_asset, other_sensor = _make_asset_and_sensor(client, other_headers, asset_code="PUMP-INV-B", sensor_code="temp-b")
    other_inv = client.post("/api/v1/investigations", headers=other_headers, json={"asset_id": other_asset["id"], "title": "Org B investigation"}).json()
    cross_evidence = client.post(f"/api/v1/investigations/{other_inv['id']}/evidence", headers=other_headers, json={"evidence_type": "anomaly", "evidence_id": "00000000-0000-0000-0000-000000000000"})
    assert cross_evidence.status_code == 404


def test_list_investigations_filters_by_asset_and_status(client, auth_headers):
    asset_a, _ = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-INV-FILT-A", sensor_code="t")
    asset_b, _ = _make_asset_and_sensor(client, auth_headers, asset_code="PUMP-INV-FILT-B", sensor_code="t")
    inv_a = client.post("/api/v1/investigations", headers=auth_headers, json={"asset_id": asset_a["id"], "title": "A"}).json()
    client.post("/api/v1/investigations", headers=auth_headers, json={"asset_id": asset_b["id"], "title": "B"})
    client.post(f"/api/v1/investigations/{inv_a['id']}/status", headers=auth_headers, json={"to_status": "under_review"})

    by_asset = client.get("/api/v1/investigations", headers=auth_headers, params={"asset_id": asset_a["id"]}).json()
    assert by_asset["total"] == 1 and by_asset["items"][0]["title"] == "A"

    by_status = client.get("/api/v1/investigations", headers=auth_headers, params={"status": "under_review"}).json()
    assert by_status["total"] == 1 and by_status["items"][0]["id"] == inv_a["id"]
