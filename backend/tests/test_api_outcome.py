"""API-level tests for the closed-loop outcome/verification/evaluation
surface — exercises the real HTTP API against the real DB,
mirroring tests/test_api_scenario.py's conventions. Covers the full
report -> link -> verify -> assess -> evaluate workflow, the verification
state machine, horizon validation, scenario/real separation, the
retraining-candidate workflow (never auto-executing), prioritization
policy versioning, cross-org isolation, and original-evidence immutability.
"""
from datetime import datetime, timedelta, timezone

import numpy as np

from app.core.security import hash_password
from app.models.user import User, UserRole
from app.services import auth_service as auth_service_module

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _parse_iso_utc(value):
    """API responses may return a naive ISO string for a datetime that
    round-tripped through SQLite (see app.services.feedback_service's
    `_ensure_utc` docstring) — always UTC in practice. Normalize to
    tz-aware UTC so arithmetic/comparison with other tz-aware datetimes in
    these tests never raises."""
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _make_asset_and_sensor(client, headers, *, asset_code="PUMP-OUT", sensor_code="temp"):
    asset_resp = client.post("/api/v1/assets", headers=headers, json={"code": asset_code, "name": "Outcome Test Pump", "asset_type": "pump"})
    assert asset_resp.status_code == 201, asset_resp.text
    sensor_resp = client.post(
        f"/api/v1/assets/{asset_resp.json()['id']}/sensors",
        headers=headers,
        json={"code": sensor_code, "name": "Temp", "measurement_type": "temperature", "unit": "celsius", "expected_interval_seconds": 300},
    )
    assert sensor_resp.status_code == 201, sensor_resp.text
    return asset_resp.json(), sensor_resp.json()


def _ingest(client, headers, *, asset_code, sensor_code, n, step_minutes=5, start=BASE, value_fn=None):
    value_fn = value_fn or (lambda i: 50.0 + (i % 5) * 0.1)
    records = [
        {"asset_code": asset_code, "sensor_code": sensor_code, "timestamp": _iso(start + timedelta(minutes=step_minutes * i)), "value": value_fn(i)}
        for i in range(n)
    ]
    resp = client.post("/api/v1/ingestion/measurements", headers=headers, json={"data_source_name": "outcome-tests", "records": records})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ingest_with_spike(client, headers, *, asset_code, sensor_code, n_ref, n_score, spike_index, step_minutes=5, seed=0):
    rng = np.random.default_rng(seed)
    ref_values = [50.0 + float(rng.normal(0, 0.5)) for _ in range(n_ref)]
    _ingest(client, headers, asset_code=asset_code, sensor_code=sensor_code, n=n_ref, step_minutes=step_minutes, start=BASE, value_fn=lambda i: ref_values[i])

    score_start = BASE + timedelta(minutes=step_minutes * n_ref)
    score_values = [50.0 + float(rng.normal(0, 0.5)) for _ in range(n_score)]
    score_values[spike_index] += 20.0
    _ingest(
        client, headers, asset_code=asset_code, sensor_code=sensor_code, n=n_score, step_minutes=step_minutes,
        start=score_start, value_fn=lambda i: score_values[i],
    )
    reference_end = BASE + timedelta(minutes=step_minutes * (n_ref - 1))
    score_end = score_start + timedelta(minutes=step_minutes * (n_score - 1))
    return reference_end, score_start, score_end


def _make_verified_anomaly(client, headers, *, asset_code="PUMP-OUT-A", sensor_code="temp"):
    """Trains a real robust_zscore detector, runs detection against a
    spiked scoring window, and returns (asset, sensor, detector, anomaly)
    for a REAL anomaly — never a fabricated row."""
    asset, sensor = _make_asset_and_sensor(client, headers, asset_code=asset_code, sensor_code=sensor_code)
    reference_end, score_start, score_end = _ingest_with_spike(
        client, headers, asset_code=asset["code"], sensor_code=sensor["code"], n_ref=400, n_score=200, spike_index=100
    )
    detector_resp = client.post(
        "/api/v1/anomaly/detectors",
        headers=headers,
        json={"name": f"{sensor_code}-detector", "detector_type": "robust_zscore", "sensor_ids": [sensor["id"]], "reference_start": _iso(BASE), "reference_end": _iso(reference_end)},
    )
    assert detector_resp.status_code == 201, detector_resp.text
    detector = detector_resp.json()

    run_resp = client.post(
        f"/api/v1/anomaly/detectors/{detector['id']}/runs", headers=headers, json={"scoring_start": _iso(score_start), "scoring_end": _iso(score_end)}
    )
    assert run_resp.status_code == 201, run_resp.text

    anomalies_resp = client.get("/api/v1/anomaly/anomalies", headers=headers, params={"sensor_id": sensor["id"]})
    assert anomalies_resp.status_code == 200
    anomalies = anomalies_resp.json()["items"]
    assert len(anomalies) >= 1
    return asset, sensor, detector, anomalies[0]


def _viewer_headers(db_session, org):
    viewer = User(organization_id=org.id, email="viewer@outcome-tests.example", hashed_password=hash_password("ViewerPass123!"), full_name="Viewer", role=UserRole.VIEWER)
    db_session.add(viewer)
    db_session.commit()
    token = auth_service_module.issue_token(viewer)
    return {"Authorization": f"Bearer {token}"}


def _engineer_headers(db_session, org):
    engineer = User(organization_id=org.id, email="engineer@outcome-tests.example", hashed_password=hash_password("EngineerPass123!"), full_name="Engineer", role=UserRole.ENGINEER)
    db_session.add(engineer)
    db_session.commit()
    token = auth_service_module.issue_token(engineer)
    return {"Authorization": f"Bearer {token}"}


def _register_component_for_detector(client, headers, *, detector):
    comp_resp = client.post(
        "/api/v1/mlops/components", headers=headers, json={"component_type": "statistical_detector", "name": detector["name"], "description": "test component"}
    )
    assert comp_resp.status_code == 201, comp_resp.text
    component = comp_resp.json()
    version_resp = client.post(
        f"/api/v1/mlops/components/{component['id']}/versions",
        headers=headers,
        json={"version_kind": "model_version", "model_version_id": detector["id"]},
    )
    assert version_resp.status_code == 201, version_resp.text
    return component


# ---------------------------------------------------------------------------
# Full closed-loop workflow
# ---------------------------------------------------------------------------


class TestClosedLoopWorkflow:
    def test_full_lifecycle_signal_to_evaluation(self, client, auth_headers):
        asset, sensor, detector, anomaly = _make_verified_anomaly(client, auth_headers)
        anomaly_start = _parse_iso_utc(anomaly["period_start"])

        report_resp = client.post(
            "/api/v1/outcomes",
            headers=auth_headers,
            json={
                "asset_id": asset["id"], "sensor_id": sensor["id"], "outcome_type": "failure_event",
                "description": "Bearing failure observed during routine inspection.",
                "outcome_timestamp": _iso(anomaly_start + timedelta(hours=6)),
            },
        )
        assert report_resp.status_code == 201, report_resp.text
        outcome = report_resp.json()
        assert outcome["verification_status"] == "unverified"
        assert outcome["source"] == "user_reported"

        # Cannot link before... actually linking is allowed pre-verification;
        # assessing is what requires verification. Link now:
        link_resp = client.post(
            f"/api/v1/outcomes/{outcome['id']}/links",
            headers=auth_headers,
            json={"signal_type": "anomaly", "signal_id": anomaly["id"], "horizon_seconds": 86400.0},
        )
        assert link_resp.status_code == 201, link_resp.text
        link = link_resp.json()
        assert link["within_horizon"] is True
        assert link["counts_toward_evaluation"] is True
        assert link["signal_assessment"] == "not_yet_assessed"

        # Assessing before verification is blocked.
        premature_assess = client.post(f"/api/v1/outcomes/links/{link['id']}/assess", headers=auth_headers, json={"signal_assessment": "confirms"})
        assert premature_assess.status_code == 409

        # Verify requires evidence_note.
        missing_evidence = client.post(f"/api/v1/outcomes/{outcome['id']}/verify", headers=auth_headers, json={"to_status": "verified"})
        assert missing_evidence.status_code == 422

        verify_resp = client.post(
            f"/api/v1/outcomes/{outcome['id']}/verify",
            headers=auth_headers,
            json={"to_status": "verified", "evidence_note": "Confirmed via maintenance work order #4471."},
        )
        assert verify_resp.status_code == 200, verify_resp.text
        assert verify_resp.json()["verification_status"] == "verified"

        verifications_resp = client.get(f"/api/v1/outcomes/{outcome['id']}/verifications", headers=auth_headers)
        assert verifications_resp.status_code == 200
        assert verifications_resp.json()["total"] == 1

        # Now assessment succeeds.
        assess_resp = client.post(f"/api/v1/outcomes/links/{link['id']}/assess", headers=auth_headers, json={"signal_assessment": "confirms"})
        assert assess_resp.status_code == 200, assess_resp.text
        assert assess_resp.json()["signal_assessment"] == "confirms"

        # Insufficient sample size -> metrics not computed, but run still created (INITIAL).
        eval_resp = client.post(
            "/api/v1/feedback/evaluation-runs",
            headers=auth_headers,
            json={
                "signal_type": "anomaly", "component_id": None, "sensor_id": sensor["id"],
                "evaluation_period_start": _iso(BASE), "evaluation_period_end": _iso(anomaly_start + timedelta(days=2)),
            },
        )
        assert eval_resp.status_code == 201, eval_resp.text
        run = eval_resp.json()
        assert run["evaluation_kind"] == "initial"
        assert run["metrics"]["computed"] is False
        assert run["metrics"]["true_positive_count"] == 1

        # Re-running the same scope produces an UPDATED run chained to the first.
        eval_resp_2 = client.post(
            "/api/v1/feedback/evaluation-runs",
            headers=auth_headers,
            json={
                "signal_type": "anomaly", "sensor_id": sensor["id"],
                "evaluation_period_start": _iso(BASE), "evaluation_period_end": _iso(anomaly_start + timedelta(days=2)),
            },
        )
        assert eval_resp_2.status_code == 201
        run2 = eval_resp_2.json()
        assert run2["evaluation_kind"] == "updated"
        assert run2["previous_evaluation_run_id"] == run["id"]

        list_runs = client.get("/api/v1/feedback/evaluation-runs", headers=auth_headers, params={"sensor_id": sensor["id"]})
        assert list_runs.status_code == 200
        assert list_runs.json()["total"] == 2

        error_analysis_resp = client.get("/api/v1/feedback/error-analysis", headers=auth_headers, params={"sensor_id": sensor["id"]})
        assert error_analysis_resp.status_code == 200
        assert "note" in error_analysis_resp.json()["data"]

        analytics_resp = client.get("/api/v1/feedback/analytics", headers=auth_headers)
        assert analytics_resp.status_code == 200
        assert analytics_resp.json()["data"]["outcomes_by_verification_status"]["verified"] == 1

        dataset_resp = client.post("/api/v1/feedback/datasets", headers=auth_headers, json={"name": "test-dataset", "sensor_id": sensor["id"]})
        assert dataset_resp.status_code == 201, dataset_resp.text
        dataset = dataset_resp.json()
        assert dataset["row_count"] == 1
        assert dataset["rows"][0]["outcome_id"] == outcome["id"]

        label_defs_resp = client.get("/api/v1/feedback/label-definitions", headers=auth_headers)
        assert label_defs_resp.status_code == 200
        assert "verification_status" in label_defs_resp.json()["data"]


# ---------------------------------------------------------------------------
# Verification state machine
# ---------------------------------------------------------------------------


class TestVerificationStateMachine:
    def test_maintenance_event_does_not_require_failure_type(self, client, auth_headers, sample_asset_and_sensor):
        asset, sensor = sample_asset_and_sensor
        resp = client.post(
            "/api/v1/outcomes",
            headers=auth_headers,
            json={"asset_id": asset.id, "outcome_type": "maintenance_event", "description": "Routine lubrication performed.", "outcome_timestamp": _iso(BASE)},
        )
        assert resp.status_code == 201
        assert resp.json()["outcome_type"] == "maintenance_event"

    def test_invalid_transition_is_rejected(self, client, auth_headers, sample_asset_and_sensor):
        asset, sensor = sample_asset_and_sensor
        outcome = client.post(
            "/api/v1/outcomes", headers=auth_headers,
            json={"asset_id": asset.id, "outcome_type": "operational_event", "description": "Shift change note.", "outcome_timestamp": _iso(BASE)},
        ).json()
        # UNVERIFIED -> CONFLICTING is not a legal transition.
        resp = client.post(f"/api/v1/outcomes/{outcome['id']}/verify", headers=auth_headers, json={"to_status": "conflicting", "evidence_note": "x"})
        assert resp.status_code == 409

    def test_rejected_can_only_reopen_to_reviewed(self, client, auth_headers, sample_asset_and_sensor):
        asset, sensor = sample_asset_and_sensor
        outcome = client.post(
            "/api/v1/outcomes", headers=auth_headers,
            json={"asset_id": asset.id, "outcome_type": "other", "description": "Unclear report.", "outcome_timestamp": _iso(BASE)},
        ).json()
        reject_resp = client.post(f"/api/v1/outcomes/{outcome['id']}/verify", headers=auth_headers, json={"to_status": "rejected", "evidence_note": "Could not substantiate."})
        assert reject_resp.status_code == 200

        bad_resp = client.post(f"/api/v1/outcomes/{outcome['id']}/verify", headers=auth_headers, json={"to_status": "verified", "evidence_note": "x"})
        assert bad_resp.status_code == 409

        good_resp = client.post(f"/api/v1/outcomes/{outcome['id']}/verify", headers=auth_headers, json={"to_status": "reviewed"})
        assert good_resp.status_code == 200

    def test_viewer_cannot_verify(self, client, auth_headers, db_session, org_and_admin, sample_asset_and_sensor):
        org, _ = org_and_admin
        asset, sensor = sample_asset_and_sensor
        outcome = client.post(
            "/api/v1/outcomes", headers=auth_headers,
            json={"asset_id": asset.id, "outcome_type": "other", "description": "x", "outcome_timestamp": _iso(BASE)},
        ).json()
        viewer_headers = _viewer_headers(db_session, org)
        resp = client.post(f"/api/v1/outcomes/{outcome['id']}/verify", headers=viewer_headers, json={"to_status": "reviewed"})
        assert resp.status_code == 403

    def test_engineer_cannot_verify_but_can_report(self, client, auth_headers, db_session, org_and_admin, sample_asset_and_sensor):
        org, _ = org_and_admin
        asset, sensor = sample_asset_and_sensor
        engineer_headers = _engineer_headers(db_session, org)
        outcome_resp = client.post(
            "/api/v1/outcomes", headers=engineer_headers,
            json={"asset_id": asset.id, "outcome_type": "other", "description": "x", "outcome_timestamp": _iso(BASE)},
        )
        assert outcome_resp.status_code == 201
        verify_resp = client.post(f"/api/v1/outcomes/{outcome_resp.json()['id']}/verify", headers=engineer_headers, json={"to_status": "reviewed"})
        assert verify_resp.status_code == 403


# ---------------------------------------------------------------------------
# Signal-outcome linking
# ---------------------------------------------------------------------------


class TestSignalOutcomeLinking:
    def test_horizon_seconds_is_required(self, client, auth_headers):
        asset, sensor, detector, anomaly = _make_verified_anomaly(client, auth_headers, asset_code="PUMP-H1")
        outcome = client.post(
            "/api/v1/outcomes", headers=auth_headers,
            json={"asset_id": asset["id"], "outcome_type": "failure_event", "description": "x", "outcome_timestamp": _iso(BASE + timedelta(days=2))},
        ).json()
        resp = client.post(f"/api/v1/outcomes/{outcome['id']}/links", headers=auth_headers, json={"signal_type": "anomaly", "signal_id": anomaly["id"]})
        assert resp.status_code == 422

    def test_duplicate_link_is_rejected(self, client, auth_headers):
        asset, sensor, detector, anomaly = _make_verified_anomaly(client, auth_headers, asset_code="PUMP-H2")
        anomaly_start = _parse_iso_utc(anomaly["period_start"])
        outcome = client.post(
            "/api/v1/outcomes", headers=auth_headers,
            json={"asset_id": asset["id"], "outcome_type": "failure_event", "description": "x", "outcome_timestamp": _iso(anomaly_start + timedelta(hours=1))},
        ).json()
        first = client.post(f"/api/v1/outcomes/{outcome['id']}/links", headers=auth_headers, json={"signal_type": "anomaly", "signal_id": anomaly["id"], "horizon_seconds": 86400.0})
        assert first.status_code == 201
        second = client.post(f"/api/v1/outcomes/{outcome['id']}/links", headers=auth_headers, json={"signal_type": "anomaly", "signal_id": anomaly["id"], "horizon_seconds": 86400.0})
        assert second.status_code == 409

    def test_out_of_horizon_link_is_flagged_not_rejected(self, client, auth_headers):
        asset, sensor, detector, anomaly = _make_verified_anomaly(client, auth_headers, asset_code="PUMP-H3")
        anomaly_start = _parse_iso_utc(anomaly["period_start"])
        outcome = client.post(
            "/api/v1/outcomes", headers=auth_headers,
            json={"asset_id": asset["id"], "outcome_type": "failure_event", "description": "x", "outcome_timestamp": _iso(anomaly_start + timedelta(days=10))},
        ).json()
        resp = client.post(f"/api/v1/outcomes/{outcome['id']}/links", headers=auth_headers, json={"signal_type": "anomaly", "signal_id": anomaly["id"], "horizon_seconds": 3600.0})
        assert resp.status_code == 201
        assert resp.json()["within_horizon"] is False


# ---------------------------------------------------------------------------
# Scenario separation
# ---------------------------------------------------------------------------


class TestScenarioSeparation:
    def test_scenario_run_link_never_counts_toward_evaluation(self, client, auth_headers, sample_asset_and_sensor):
        asset, sensor = sample_asset_and_sensor
        n = 300
        rng = np.random.default_rng(3)
        values = [50.0 + float(rng.normal(0, 0.5)) for _ in range(n)]
        _ingest(client, auth_headers, asset_code=asset.code, sensor_code=sensor.code, n=n, value_fn=lambda i: values[i])

        scenario_resp = client.post(
            "/api/v1/scenarios",
            headers=auth_headers,
            json={
                "asset_id": asset.id, "sensor_id": sensor.id, "name": "separation-test", "scenario_type": "level_shift",
                "baseline_window_start": _iso(BASE), "baseline_window_end": _iso(BASE + timedelta(minutes=5 * (n - 1))),
                "modification_start": _iso(BASE + timedelta(minutes=5 * 100)), "modification_duration_seconds": 3600.0 * 10,
                "magnitude": 1.0, "unit": "celsius",
            },
        )
        assert scenario_resp.status_code == 201, scenario_resp.text
        scenario = scenario_resp.json()
        validate_resp = client.post(f"/api/v1/scenarios/{scenario['id']}/validate", headers=auth_headers)
        assert validate_resp.status_code == 200, validate_resp.text

        run_resp = client.post(f"/api/v1/scenarios/{scenario['id']}/runs", headers=auth_headers, json={})
        assert run_resp.status_code == 201, run_resp.text
        scenario_run = run_resp.json()

        outcome = client.post(
            "/api/v1/outcomes", headers=auth_headers,
            json={"asset_id": asset.id, "outcome_type": "other", "description": "Comparing simulated result to intuition.", "outcome_timestamp": _iso(BASE + timedelta(minutes=5 * 150))},
        ).json()

        link_resp = client.post(
            f"/api/v1/outcomes/{outcome['id']}/links", headers=auth_headers,
            json={"signal_type": "scenario_run", "signal_id": scenario_run["id"], "horizon_seconds": 86400.0},
        )
        assert link_resp.status_code == 201, link_resp.text
        assert link_resp.json()["counts_toward_evaluation"] is False

        summary_resp = client.get("/api/v1/feedback/scenario-separation", headers=auth_headers)
        assert summary_resp.status_code == 200
        data = summary_resp.json()["data"]
        assert data["scenario_links_total"] == 1
        assert data["scenario_links_counting_toward_evaluation"] == 0
        assert data["invariant_holds"] is True


# ---------------------------------------------------------------------------
# Retraining candidate workflow
# ---------------------------------------------------------------------------


class TestRetrainingCandidateWorkflow:
    def test_propose_and_approve_calls_existing_retraining_workflow_but_never_executes(self, client, auth_headers):
        asset, sensor, detector, anomaly = _make_verified_anomaly(client, auth_headers, asset_code="PUMP-RT1")
        component = _register_component_for_detector(client, auth_headers, detector=detector)

        propose_resp = client.post(
            "/api/v1/feedback/retraining-candidates",
            headers=auth_headers,
            json={"component_id": component["id"], "trigger_reason": "Elevated false-positive rate observed this quarter."},
        )
        assert propose_resp.status_code == 201, propose_resp.text
        candidate = propose_resp.json()
        assert candidate["status"] == "proposed"
        assert candidate["resulting_retraining_request_id"] is None

        review_resp = client.post(
            f"/api/v1/feedback/retraining-candidates/{candidate['id']}/review",
            headers=auth_headers,
            json={"decision": "approved", "review_notes": "Approved for retraining."},
        )
        assert review_resp.status_code == 200, review_resp.text
        reviewed = review_resp.json()
        assert reviewed["status"] == "approved"
        assert reviewed["resulting_retraining_request_id"] is not None

        # The resulting RetrainingRequest exists but is only REQUESTED — never executed/activated by this workflow.
        request_resp = client.get(f"/api/v1/mlops/retraining-requests/{reviewed['resulting_retraining_request_id']}", headers=auth_headers)
        assert request_resp.status_code == 200
        assert request_resp.json()["status"] == "requested"

        # Re-reviewing an already-reviewed candidate is a conflict.
        re_review = client.post(f"/api/v1/feedback/retraining-candidates/{candidate['id']}/review", headers=auth_headers, json={"decision": "dismissed"})
        assert re_review.status_code == 409

    def test_dismissed_candidate_never_calls_retraining(self, client, auth_headers):
        asset, sensor, detector, anomaly = _make_verified_anomaly(client, auth_headers, asset_code="PUMP-RT2")
        component = _register_component_for_detector(client, auth_headers, detector=detector)
        candidate = client.post(
            "/api/v1/feedback/retraining-candidates", headers=auth_headers, json={"component_id": component["id"], "trigger_reason": "x"}
        ).json()
        resp = client.post(f"/api/v1/feedback/retraining-candidates/{candidate['id']}/review", headers=auth_headers, json={"decision": "dismissed"})
        assert resp.status_code == 200
        assert resp.json()["resulting_retraining_request_id"] is None


# ---------------------------------------------------------------------------
# Prioritization policy
# ---------------------------------------------------------------------------


class TestPrioritizationPolicy:
    def test_no_active_policy_by_default(self, client, auth_headers):
        resp = client.get("/api/v1/feedback/prioritization-policies", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_create_and_activate_retires_previous(self, client, auth_headers):
        p1 = client.post("/api/v1/feedback/prioritization-policies", headers=auth_headers, json={"config": {"weight": 1}, "rationale": "first"}).json()
        act1 = client.post(f"/api/v1/feedback/prioritization-policies/{p1['id']}/activate", headers=auth_headers)
        assert act1.status_code == 200
        assert act1.json()["status"] == "active"

        p2 = client.post("/api/v1/feedback/prioritization-policies", headers=auth_headers, json={"config": {"weight": 2}, "rationale": "second"}).json()
        act2 = client.post(f"/api/v1/feedback/prioritization-policies/{p2['id']}/activate", headers=auth_headers)
        assert act2.status_code == 200

        p1_check = client.get("/api/v1/feedback/prioritization-policies", headers=auth_headers).json()
        statuses = {item["id"]: item["status"] for item in p1_check["items"]}
        assert statuses[p1["id"]] == "retired"
        assert statuses[p2["id"]] == "active"


# ---------------------------------------------------------------------------
# Original evidence immutability
# ---------------------------------------------------------------------------


class TestOriginalEvidenceImmutability:
    def test_anomaly_fields_are_unchanged_after_full_closed_loop_cycle(self, client, auth_headers):
        asset, sensor, detector, anomaly = _make_verified_anomaly(client, auth_headers, asset_code="PUMP-IMM")
        anomaly_start = _parse_iso_utc(anomaly["period_start"])

        outcome = client.post(
            "/api/v1/outcomes", headers=auth_headers,
            json={"asset_id": asset["id"], "sensor_id": sensor["id"], "outcome_type": "failure_event", "description": "x", "outcome_timestamp": _iso(anomaly_start + timedelta(hours=2))},
        ).json()
        link = client.post(f"/api/v1/outcomes/{outcome['id']}/links", headers=auth_headers, json={"signal_type": "anomaly", "signal_id": anomaly["id"], "horizon_seconds": 86400.0}).json()
        client.post(f"/api/v1/outcomes/{outcome['id']}/verify", headers=auth_headers, json={"to_status": "verified", "evidence_note": "confirmed"})
        client.post(f"/api/v1/outcomes/links/{link['id']}/assess", headers=auth_headers, json={"signal_assessment": "confirms"})

        after = client.get(f"/api/v1/anomaly/anomalies/{anomaly['id']}", headers=auth_headers).json()
        for field in ("period_start", "period_end", "raw_score", "detector_type", "threshold_used", "severity_tier", "summary"):
            assert after[field] == anomaly[field], f"{field} changed after closed-loop cycle"

    def test_engineer_can_link_and_assess_but_cannot_verify(self, client, auth_headers, db_session, org_and_admin):
        org, _ = org_and_admin
        engineer_headers = _engineer_headers(db_session, org)
        asset, sensor, detector, anomaly = _make_verified_anomaly(client, auth_headers, asset_code="PUMP-ENG")
        anomaly_start = _parse_iso_utc(anomaly["period_start"])

        outcome = client.post(
            "/api/v1/outcomes", headers=engineer_headers,
            json={"asset_id": asset["id"], "outcome_type": "failure_event", "description": "x", "outcome_timestamp": _iso(anomaly_start + timedelta(hours=2))},
        )
        assert outcome.status_code == 201
        outcome = outcome.json()

        link_resp = client.post(f"/api/v1/outcomes/{outcome['id']}/links", headers=engineer_headers, json={"signal_type": "anomaly", "signal_id": anomaly["id"], "horizon_seconds": 86400.0})
        assert link_resp.status_code == 201
        link = link_resp.json()

        verify_blocked = client.post(f"/api/v1/outcomes/{outcome['id']}/verify", headers=engineer_headers, json={"to_status": "verified", "evidence_note": "x"})
        assert verify_blocked.status_code == 403

        # Admin verifies, then engineer can assess.
        verify_ok = client.post(f"/api/v1/outcomes/{outcome['id']}/verify", headers=auth_headers, json={"to_status": "verified", "evidence_note": "x"})
        assert verify_ok.status_code == 200

        assess_resp = client.post(f"/api/v1/outcomes/links/{link['id']}/assess", headers=engineer_headers, json={"signal_assessment": "confirms"})
        assert assess_resp.status_code == 200


# ---------------------------------------------------------------------------
# Cross-org isolation
# ---------------------------------------------------------------------------


class TestCrossOrgIsolation:
    def test_outcome_from_another_org_is_not_visible(self, client, auth_headers, sample_asset_and_sensor, db_session):
        asset, sensor = sample_asset_and_sensor
        outcome = client.post(
            "/api/v1/outcomes", headers=auth_headers,
            json={"asset_id": asset.id, "outcome_type": "other", "description": "x", "outcome_timestamp": _iso(BASE)},
        ).json()

        other_org, other_admin = auth_service_module.register_organization(
            db_session, organization_name="Other Org", admin_email="admin@other-org.example", admin_password="AdminPass123!", admin_full_name="Other Admin"
        )
        other_token = auth_service_module.issue_token(other_admin)
        other_headers = {"Authorization": f"Bearer {other_token}"}

        resp = client.get(f"/api/v1/outcomes/{outcome['id']}", headers=other_headers)
        assert resp.status_code == 404
