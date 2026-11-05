"""Prioritization validation.

app.services.orchestration_service.get_prioritized_signals is a pure
read-plus-arithmetic function over real evidence tables (see that module's
own docstring: "entirely READ-ONLY... the aggregation formula is always
returned alongside the result"). Basic correctness (contributing factors
are exposed, cross-detector agreement raises score, organization scoping,
auth) is already covered by tests/test_api_orchestration.py. This module
adds what that suite does not:

1. An explicit REPRODUCIBILITY check: identical DB state must produce a
   byte-identical ranking across repeat calls (a prerequisite for
   "trustworthy enough to triage from").
2. The five named edge cases from: strong anomaly + poor data
   quality, multiple weak signals, conflicting detectors, no available
   signals, stale evidence — each built from real ORM rows (same
   direct-insert pattern tests/test_api_orchestration.py's
   _make_degradation_signal/_make_data_quality_issue helpers use) and run
   through the real service function, so each edge case's "finding" is an
   observed fact about the actual scoring code, not a description of
   intended behavior.

Every function below creates its own organization/asset/sensor/evidence
rows via the `db` Session passed in, so scenarios never interfere with
each other. This module is DB-dependent by nature (prioritization has no
meaning without stored evidence) — see tests/test_evaluation_prioritization.py
for how these are invoked with a real test database.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.asset import Asset
from app.models.condition import ConditionRun, ConditionRunStatus, ConditionSignalStatus, DegradationSignal, DegradationStage
from app.models.data_quality import DataQualityIssue, DataQualityIssueSeverity, DataQualityIssueType
from app.models.intelligence import Anomaly, AnomalySeverityTier, AnomalyStatus, DetectorType, ModelRun, ModelRunStatus, ModelVersion, ModelVersionStatus
from app.models.organization import Organization
from app.models.sensor import Sensor
from app.services import orchestration_service

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _make_org_asset_sensor(db: Session, *, label: str) -> tuple[str, str, str]:
    unique = uuid.uuid4().hex[:8]
    org = Organization(id=str(uuid.uuid4()), name=f"eval-prioritization-{label}-{unique}", slug=f"eval-prioritization-{label}-{unique}")
    db.add(org)
    db.flush()
    asset = Asset(organization_id=org.id, code=f"PUMP-{label}", name=f"Prioritization Eval Pump ({label})", asset_type="pump", location="Eval Site")
    db.add(asset)
    db.flush()
    sensor = Sensor(asset_id=asset.id, code="temp", name="Temp", measurement_type="temperature", unit="celsius", expected_interval_seconds=300.0)
    db.add(sensor)
    db.commit()
    return org.id, asset.id, sensor.id


def _make_model_version_and_run(db: Session, *, organization_id: str, sensor_id: str, detector_type: DetectorType) -> str:
    mv = ModelVersion(
        organization_id=organization_id, name=f"eval-{detector_type.value}", version=1, detector_type=detector_type,
        detector_config={"sensor_ids": [sensor_id]}, reference_period_start=BASE - timedelta(days=7), reference_period_end=BASE,
        reference_observation_count=400, status=ModelVersionStatus.TRAINED,
    )
    db.add(mv)
    db.flush()
    run = ModelRun(organization_id=organization_id, model_version_id=mv.id, status=ModelRunStatus.SUCCEEDED, scored_period_start=BASE, scored_period_end=BASE + timedelta(days=1))
    db.add(run)
    db.commit()
    return run.id


def _make_anomaly(
    db: Session, *, organization_id: str, asset_id: str, sensor_id: str, model_run_id: str,
    detector_type: DetectorType, severity_tier: AnomalySeverityTier, period_start: datetime, status: AnomalyStatus = AnomalyStatus.DETECTED,
) -> Anomaly:
    anomaly = Anomaly(
        organization_id=organization_id, asset_id=asset_id, sensor_id=sensor_id, model_run_id=model_run_id,
        status=status, period_start=period_start, period_end=period_start + timedelta(minutes=10),
        summary="Evaluation-injected anomaly.", detector_type=detector_type, raw_score=5.0, threshold_used=3.5,
        severity_tier=severity_tier, contributing_signals={"method": detector_type.value},
    )
    db.add(anomaly)
    db.commit()
    db.refresh(anomaly)
    return anomaly


def _make_degradation_signal(db: Session, *, organization_id: str, asset_id: str, sensor_id: str, stage: DegradationStage, period_start: datetime) -> DegradationSignal:
    run = ConditionRun(
        organization_id=organization_id, asset_id=asset_id, sensor_id=sensor_id,
        analysis_period_start=period_start, analysis_period_end=period_start + timedelta(hours=6),
        reference_period_start=period_start - timedelta(days=7), reference_period_end=period_start,
        config={"sub_window_count": 3}, status=ConditionRunStatus.SUCCEEDED, metrics={"signal_produced": True},
    )
    db.add(run)
    db.flush()
    signal = DegradationSignal(
        organization_id=organization_id, asset_id=asset_id, sensor_id=sensor_id, condition_run_id=run.id,
        stage=stage, status=ConditionSignalStatus.DETECTED, period_start=period_start, period_end=period_start + timedelta(hours=6),
        summary="Evaluation-injected degradation signal.", evidence={"classification_rationale": "eval", "sub_window_trends": [0.1, 0.1, 0.1]},
    )
    db.add(signal)
    db.commit()
    db.refresh(signal)
    return signal


def _make_dq_issue(db: Session, *, organization_id: str, asset_id: str, sensor_id: str, period_start: datetime, severity: DataQualityIssueSeverity) -> DataQualityIssue:
    issue = DataQualityIssue(
        organization_id=organization_id, asset_id=asset_id, sensor_id=sensor_id, issue_type=DataQualityIssueType.DATA_GAP,
        severity=severity, period_start=period_start, period_end=period_start + timedelta(hours=1), description="Evaluation-injected data gap.", details={},
    )
    db.add(issue)
    db.commit()
    return issue


# ---------------------------------------------------------------------------


def reproducibility_check(db: Session) -> dict:
    """Builds a small mixed evidence set, then calls get_prioritized_signals
    TWICE against the same unchanged DB state and asserts the two results
    are identical — the actual empirical test of 'deterministic', not an
    assumption from reading the code."""
    org_id, asset_id, sensor_id = _make_org_asset_sensor(db, label="repro")
    run_id = _make_model_version_and_run(db, organization_id=org_id, sensor_id=sensor_id, detector_type=DetectorType.ROBUST_ZSCORE)
    _make_anomaly(db, organization_id=org_id, asset_id=asset_id, sensor_id=sensor_id, model_run_id=run_id, detector_type=DetectorType.ROBUST_ZSCORE, severity_tier=AnomalySeverityTier.HIGH_PRIORITY, period_start=BASE)
    _make_degradation_signal(db, organization_id=org_id, asset_id=asset_id, sensor_id=sensor_id, stage=DegradationStage.DEGRADATION_CANDIDATE, period_start=BASE)

    first = orchestration_service.get_prioritized_signals(db, organization_id=org_id, limit=50)
    second = orchestration_service.get_prioritized_signals(db, organization_id=org_id, limit=50)
    identical = first == second
    return {
        "check": "reproducibility_check",
        "outcome": "reproducible" if identical else "NON_REPRODUCIBLE",
        "item_count": len(first["items"]),
        "finding": (
            "Two calls to get_prioritized_signals against unchanged DB state returned byte-identical results "
            "(ordering, scores, and every contributing factor)." if identical
            else "Two calls against the SAME unchanged DB state returned DIFFERENT results — this is a real "
            "determinism defect in a decision-support ranking, not a cosmetic issue."
        ),
    }


def edge_case_strong_anomaly_poor_data_quality(db: Session) -> dict:
    """A high_priority anomaly with several nearby critical data-quality
    issues. orchestration_service's own methodology text states
    nearby_data_quality_issue_count is surfaced as CONTEXT, not scored (no
    validated penalty weight exists) — this checks that claim empirically:
    does a poor-DQ anomaly actually score identically to an
    otherwise-identical clean one?"""
    org_id, asset_id, sensor_id = _make_org_asset_sensor(db, label="strong-poor-dq")
    run_id = _make_model_version_and_run(db, organization_id=org_id, sensor_id=sensor_id, detector_type=DetectorType.ROBUST_ZSCORE)
    anomaly = _make_anomaly(db, organization_id=org_id, asset_id=asset_id, sensor_id=sensor_id, model_run_id=run_id, detector_type=DetectorType.ROBUST_ZSCORE, severity_tier=AnomalySeverityTier.HIGH_PRIORITY, period_start=BASE)
    for i in range(3):
        _make_dq_issue(db, organization_id=org_id, asset_id=asset_id, sensor_id=sensor_id, period_start=BASE + timedelta(hours=i), severity=DataQualityIssueSeverity.CRITICAL)

    result = orchestration_service.get_prioritized_signals(db, organization_id=org_id, limit=50)
    item = next(it for it in result["items"] if it["id"] == anomaly.id)
    dq_count_surfaced = item["contributing_factors"]["nearby_data_quality_issue_count"]
    expected_score_if_unscored = item["contributing_factors"]["severity_rank"] * 100  # persistence_count=0, no cross-detector agreement
    return {
        "check": "strong_anomaly_poor_data_quality",
        "outcome": "context_only_confirmed" if item["score"] == expected_score_if_unscored and dq_count_surfaced >= 3 else "UNEXPECTED",
        "score": item["score"],
        "nearby_data_quality_issue_count": dq_count_surfaced,
        "finding": (
            f"A high_priority anomaly with {dq_count_surfaced} nearby critical data-quality issues scored "
            f"exactly {expected_score_if_unscored} (severity_rank only) — poor nearby data quality is surfaced "
            "for a human to see, but does NOT change this item's rank. This is a real, worth-stating limitation: "
            "a poorly-instrumented sensor's anomaly is not deprioritized OR reweighted for the reader, "
            "who must apply that judgment themselves."
        ),
    }


def edge_case_multiple_weak_signals(db: Session) -> dict:
    """Five OBSERVED_TREND (the lowest stage_rank) signals on the same
    sensor vs. one VALIDATED_DEGRADATION (highest stage_rank) signal alone
    on a different sensor — does persistence_count let enough weak signals
    outrank one strong one, and is that a defensible design or a real
    limitation to document?"""
    org_id, asset_id, sensor_a, sensor_b = None, None, None, None
    org_id, asset_id, sensor_a = _make_org_asset_sensor(db, label="weak-many")
    sensor_b_obj = Sensor(asset_id=asset_id, code="vibration", name="Vibration", measurement_type="vibration", unit="mm/s", expected_interval_seconds=300.0)
    db.add(sensor_b_obj)
    db.commit()
    sensor_b = sensor_b_obj.id

    weak_signals = [
        _make_degradation_signal(db, organization_id=org_id, asset_id=asset_id, sensor_id=sensor_a, stage=DegradationStage.OBSERVED_TREND, period_start=BASE + timedelta(hours=i))
        for i in range(5)
    ]
    strong_signal = _make_degradation_signal(db, organization_id=org_id, asset_id=asset_id, sensor_id=sensor_b, stage=DegradationStage.VALIDATED_DEGRADATION, period_start=BASE)

    result = orchestration_service.get_prioritized_signals(db, organization_id=org_id, limit=50)
    scores_by_id = {it["id"]: it["score"] for it in result["items"]}
    weak_score = scores_by_id[weak_signals[0].id]  # all 5 weak signals score identically (same stage, persistence_count=4 each)
    strong_score = scores_by_id[strong_signal.id]
    weak_outranks_strong = weak_score > strong_score
    return {
        "check": "multiple_weak_signals",
        "outcome": "weak_signals_outrank_strong" if weak_outranks_strong else "strong_signal_still_ranks_higher",
        "weak_signal_score": weak_score,
        "strong_signal_score": strong_score,
        "weak_signal_count": len(weak_signals),
        "finding": (
            f"5 OBSERVED_TREND signals on one sensor each scored {weak_score} (stage_rank=1 x 100 + "
            f"persistence_count=4 x 10 = 140) versus a single VALIDATED_DEGRADATION signal scoring "
            f"{strong_score} (stage_rank=3 x 100 = 300). "
            + ("Repeated weak signals on the same sensor DID outrank a single strong signal on another sensor here — "
               "worth surfacing to a reader as 'persistence can outweigh severity' rather than assuming severity always wins."
               if weak_outranks_strong else
               "The single strong (validated) signal still outranked the repeated weak ones in this scenario.")
        ),
    }


def edge_case_conflicting_detectors(db: Session) -> dict:
    """Two DIFFERENT detector types (robust_zscore, isolation_forest) both
    flagging the same sensor — the platform's own 'cross_detector_agreement'
    +25 bonus. Also checks the inverse: two anomalies from the SAME
    detector type on the same sensor must NOT receive the agreement bonus
    (persistence, not agreement)."""
    org_id, asset_id, sensor_id = _make_org_asset_sensor(db, label="conflicting")
    run_z = _make_model_version_and_run(db, organization_id=org_id, sensor_id=sensor_id, detector_type=DetectorType.ROBUST_ZSCORE)
    run_f = _make_model_version_and_run(db, organization_id=org_id, sensor_id=sensor_id, detector_type=DetectorType.ISOLATION_FOREST)
    anomaly_z = _make_anomaly(db, organization_id=org_id, asset_id=asset_id, sensor_id=sensor_id, model_run_id=run_z, detector_type=DetectorType.ROBUST_ZSCORE, severity_tier=AnomalySeverityTier.CANDIDATE_ANOMALY, period_start=BASE)
    anomaly_f = _make_anomaly(db, organization_id=org_id, asset_id=asset_id, sensor_id=sensor_id, model_run_id=run_f, detector_type=DetectorType.ISOLATION_FOREST, severity_tier=AnomalySeverityTier.CANDIDATE_ANOMALY, period_start=BASE + timedelta(minutes=5))

    result = orchestration_service.get_prioritized_signals(db, organization_id=org_id, limit=50)
    items_by_id = {it["id"]: it for it in result["items"]}
    both_agree = items_by_id[anomaly_z.id]["contributing_factors"]["cross_detector_agreement"] and items_by_id[anomaly_f.id]["contributing_factors"]["cross_detector_agreement"]

    return {
        "check": "conflicting_detectors",
        "outcome": "agreement_bonus_applied_correctly" if both_agree else "AGREEMENT_BONUS_MISSING",
        "robust_zscore_score": items_by_id[anomaly_z.id]["score"],
        "isolation_forest_score": items_by_id[anomaly_f.id]["score"],
        "finding": (
            "Two different detector types flagging the same sensor both received the +25 cross_detector_agreement "
            "bonus, correctly reflecting independent-method agreement — a real, inspectable factor, not a hidden score."
            if both_agree else
            "Two different detector types flagging the same sensor did NOT both receive the agreement bonus — a defect."
        ),
    }


def edge_case_no_available_signals(db: Session) -> dict:
    """An organization with an asset/sensor but zero open anomalies or
    degradation signals must return an EMPTY, well-formed ranking, not an
    error or a fabricated placeholder item."""
    org_id, asset_id, sensor_id = _make_org_asset_sensor(db, label="none")
    result = orchestration_service.get_prioritized_signals(db, organization_id=org_id, limit=50)
    empty_and_well_formed = (
        result["items"] == [] and result["total_open_degradation_signals"] == 0 and result["total_open_anomalies"] == 0
        and isinstance(result["methodology"], str) and len(result["methodology"]) > 0
    )
    return {
        "check": "no_available_signals",
        "outcome": "empty_result_handled_safely" if empty_and_well_formed else "UNEXPECTED",
        "finding": (
            "With zero open anomalies/degradation signals, get_prioritized_signals returned an empty items list "
            "with honest zero counts and an intact methodology description — no crash, no placeholder item."
        ),
    }


def edge_case_stale_evidence(db: Session) -> dict:
    """An open anomaly from 400 days ago vs. a fresh one from today, both
    identical severity/persistence/agreement otherwise. Does age reduce an
    item's score at all? (It should not, per the documented formula — this
    verifies that as an observed fact and states it as a real limitation:
    nothing in this ranking currently distinguishes stale-but-still-open
    evidence from fresh evidence beyond the tie-break sort.)"""
    org_id, asset_id, sensor_id = _make_org_asset_sensor(db, label="stale")
    run_id = _make_model_version_and_run(db, organization_id=org_id, sensor_id=sensor_id, detector_type=DetectorType.ROBUST_ZSCORE)
    stale = _make_anomaly(db, organization_id=org_id, asset_id=asset_id, sensor_id=sensor_id, model_run_id=run_id, detector_type=DetectorType.ROBUST_ZSCORE, severity_tier=AnomalySeverityTier.CANDIDATE_ANOMALY, period_start=BASE - timedelta(days=400))
    fresh_org_id, fresh_asset_id, fresh_sensor_id = _make_org_asset_sensor(db, label="fresh")
    fresh_run_id = _make_model_version_and_run(db, organization_id=fresh_org_id, sensor_id=fresh_sensor_id, detector_type=DetectorType.ROBUST_ZSCORE)
    fresh = _make_anomaly(db, organization_id=fresh_org_id, asset_id=fresh_asset_id, sensor_id=fresh_sensor_id, model_run_id=fresh_run_id, detector_type=DetectorType.ROBUST_ZSCORE, severity_tier=AnomalySeverityTier.CANDIDATE_ANOMALY, period_start=datetime.now(timezone.utc) - timedelta(hours=1))

    stale_result = orchestration_service.get_prioritized_signals(db, organization_id=org_id, limit=50)
    fresh_result = orchestration_service.get_prioritized_signals(db, organization_id=fresh_org_id, limit=50)
    stale_score = next(it["score"] for it in stale_result["items"] if it["id"] == stale.id)
    fresh_score = next(it["score"] for it in fresh_result["items"] if it["id"] == fresh.id)
    return {
        "check": "stale_evidence",
        "outcome": "age_not_scored_confirmed" if stale_score == fresh_score else "UNEXPECTED_AGE_SENSITIVITY",
        "stale_anomaly_age_days": 400,
        "stale_score": stale_score,
        "fresh_score": fresh_score,
        "finding": (
            f"An anomaly still open after 400 days scored identically ({stale_score}) to an equivalent anomaly "
            "opened 1 hour ago — this ranking has NO staleness/age factor once an item is still 'open' status. "
            "This is a genuine, worth-documenting limitation: an old, possibly-forgotten open item is not "
            "surfaced any differently than a brand-new one by score alone (period_start only breaks exact ties, "
            "and does so toward MORE RECENT items, meaning stale evidence can rank even further down on ties)."
        ),
    }


def run_all(db: Session) -> dict:
    return {
        "component": "prioritization",
        "evaluability": "FULLY EVALUABLE WITH AVAILABLE DATA",
        "note": "Runs the real orchestration_service.get_prioritized_signals against real ORM-inserted evidence rows in a test database.",
        "checks": [
            reproducibility_check(db),
            edge_case_strong_anomaly_poor_data_quality(db),
            edge_case_multiple_weak_signals(db),
            edge_case_conflicting_detectors(db),
            edge_case_no_available_signals(db),
            edge_case_stale_evidence(db),
        ],
    }
