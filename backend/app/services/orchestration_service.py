"""Intelligence orchestration: unified timeline, evidence-based
prioritization, cross-layer synthesis, cross-sensor context, and
model/method provenance (Phase 6, spec §3/§4/§5/§8/§11).

CORE DISCIPLINE (spec's own words): CONNECT evidence, PRESERVE provenance,
SURFACE relevant context, HELP humans prioritize. Nothing in this module
computes a fabricated composite "health score", asserts causality between
sensors, or claims a probability of failure. Every function below either
reads real rows from Phases 1-5's tables directly, or performs a plainly
documented, inspectable aggregation over them — the aggregation formula is
always returned alongside the result, never hidden inside a single number.

This module is entirely READ-ONLY against the underlying evidence tables —
it never writes to Anomaly, DegradationSignal, DataQualityIssue,
ForecastRun/ForecastModel, ComponentVersion, or GovernanceEvent. The only
tables this module's write paths touch are in app.models.investigation
(see app.services.investigation_service).
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError, ValidationFailedError
from app.models.asset import Asset
from app.models.condition import (
    ConditionSignalStatus,
    DegradationSignal,
    DegradationStage,
    ForecastModel,
    ForecastModelStatus,
    ForecastRun,
)
from app.models.data_quality import DataQualityIssue
from app.models.intelligence import Anomaly, AnomalySeverityTier, AnomalyStatus, ModelVersion
from app.models.investigation import Investigation, InvestigationAnnotation, InvestigationStatusEvent
from app.models.mlops import ComponentVersion, GovernanceEvent, GovernanceEventType, IntelligenceComponent, LifecycleStage
from app.models.sensor import Sensor
from app.timeseries import multi_sensor
from app.timeseries.data_access import TooManyObservationsError, load_multi_sensor_series

MAX_PER_SOURCE = 300  # bounded fetch per evidence source, mirrors app.services.condition_service.get_attention_list's cap
PRIORITIZATION_METHOD_VERSION = "orchestration_priority_v1"
_STAGE_RANK = {DegradationStage.VALIDATED_DEGRADATION: 3, DegradationStage.DEGRADATION_CANDIDATE: 2, DegradationStage.OBSERVED_TREND: 1}
_SEVERITY_RANK = {AnomalySeverityTier.HIGH_PRIORITY: 3, AnomalySeverityTier.CANDIDATE_ANOMALY: 2, AnomalySeverityTier.OBSERVED_UNUSUAL: 1}
_OPEN_ANOMALY_STATUSES = [AnomalyStatus.DETECTED, AnomalyStatus.UNDER_REVIEW, AnomalyStatus.ACKNOWLEDGED]
_OPEN_SIGNAL_STATUSES = [ConditionSignalStatus.DETECTED, ConditionSignalStatus.UNDER_REVIEW, ConditionSignalStatus.ACKNOWLEDGED]


def _ensure_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _get_asset(db: Session, *, organization_id: str, asset_id: str) -> Asset:
    asset = db.query(Asset).filter(Asset.id == asset_id, Asset.organization_id == organization_id).first()
    if asset is None:
        raise NotFoundError(f"Asset '{asset_id}' not found.")
    return asset


def _sensor_ids_for_asset(db: Session, *, organization_id: str, asset_id: str) -> list[str]:
    return [s.id for s in db.query(Sensor.id).filter(Sensor.asset_id == asset_id).all()]


# ---------------------------------------------------------------------------
# §3 — Unified asset intelligence timeline
# ---------------------------------------------------------------------------

TIMELINE_CATEGORIES = {
    "data_quality_issue": "observed_data",
    "anomaly_detected": "model_output",
    "anomaly_status_changed": "human_interpretation",
    "degradation_signal_detected": "analytical_signal",
    "degradation_review": "human_interpretation",
    "forecast_run": "model_output",
    "component_lifecycle_event": "governance",
    "investigation_opened": "human_interpretation",
    "investigation_status_changed": "human_interpretation",
    "human_annotation": "human_interpretation",
}
"""Every timeline event is tagged with exactly one of five categories (spec
§5 asks for four — OBSERVED DATA / ANALYTICAL SIGNAL / MODEL OUTPUT / HUMAN
INTERPRETATION; this platform adds a fifth, GOVERNANCE, for component
activation/rollback/retirement events, because those are neither raw data,
a statistical signal about data, a model's output about data, nor a
human's interpretation of evidence — they are a system/process event about
which VERSION of a method was in effect. Collapsing them into
'model_output' would misrepresent what they are, so this module documents
the deliberate 5th category rather than forcing the spec's four)."""


def _components_covering_sensor(db: Session, *, organization_id: str, sensor_id: str) -> list[str]:
    """Best-effort resolution of which registered IntelligenceComponents
    are relevant to one sensor: a MODEL_VERSION-kind component whose
    underlying ModelVersion.detector_config['sensor_ids'] includes this
    sensor, or a FORECAST_MODEL-kind component whose ForecastModel.sensor_id
    matches. This is a real join through a JSON column, not a guess — but
    it can only find components that were actually registered in the
    Phase 5 governance layer; an un-registered ModelVersion/ForecastModel
    produces no timeline governance events, which is correct (there is no
    governance history for something never registered)."""
    component_ids: list[str] = []
    versions = (
        db.query(ComponentVersion)
        .filter(ComponentVersion.organization_id == organization_id)
        .filter(or_(ComponentVersion.model_version_id.isnot(None), ComponentVersion.forecast_model_id.isnot(None)))
        .all()
    )
    for v in versions:
        if v.model_version_id:
            mv = db.query(ModelVersion).filter(ModelVersion.id == v.model_version_id).first()
            if mv and sensor_id in (mv.detector_config or {}).get("sensor_ids", []):
                component_ids.append(v.component_id)
        elif v.forecast_model_id:
            fm = db.query(ForecastModel).filter(ForecastModel.id == v.forecast_model_id).first()
            if fm and fm.sensor_id == sensor_id:
                component_ids.append(v.component_id)
    return sorted(set(component_ids))


def get_asset_intelligence_timeline(
    db: Session,
    *,
    organization_id: str,
    asset_id: str,
    start: datetime,
    end: datetime,
    event_types: Optional[list[str]],
    limit: int,
    offset: int,
) -> dict[str, Any]:
    asset = _get_asset(db, organization_id=organization_id, asset_id=asset_id)
    start, end = _ensure_utc(start), _ensure_utc(end)
    if start >= end:
        raise ValidationFailedError("start must be before end.")
    sensor_ids = _sensor_ids_for_asset(db, organization_id=organization_id, asset_id=asset_id)

    events: list[dict[str, Any]] = []

    # Data-quality issues — OBSERVED DATA
    issues = (
        db.query(DataQualityIssue)
        .filter(DataQualityIssue.organization_id == organization_id, DataQualityIssue.asset_id == asset_id)
        .filter(DataQualityIssue.period_end >= start, DataQualityIssue.period_start <= end)
        .order_by(DataQualityIssue.detected_at.desc()).limit(MAX_PER_SOURCE).all()
    )
    for i in issues:
        events.append({
            "event_type": "data_quality_issue", "category": TIMELINE_CATEGORIES["data_quality_issue"],
            "timestamp": i.period_start.isoformat(), "sensor_id": i.sensor_id, "summary": i.description,
            "source_type": "data_quality_issue", "source_id": i.id,
            "details": {"issue_type": i.issue_type.value, "severity": i.severity.value, "period_end": i.period_end.isoformat()},
        })

    # Anomalies — MODEL OUTPUT, plus their review history — HUMAN INTERPRETATION
    anomalies = (
        db.query(Anomaly).filter(Anomaly.organization_id == organization_id, Anomaly.asset_id == asset_id)
        .filter(Anomaly.period_end >= start, Anomaly.period_start <= end)
        .order_by(Anomaly.period_start.desc()).limit(MAX_PER_SOURCE).all()
    )
    for a in anomalies:
        events.append({
            "event_type": "anomaly_detected", "category": TIMELINE_CATEGORIES["anomaly_detected"],
            "timestamp": a.period_start.isoformat(), "sensor_id": a.sensor_id, "summary": a.summary,
            "source_type": "anomaly", "source_id": a.id,
            "details": {"detector_type": a.detector_type.value, "severity_tier": a.severity_tier.value, "status": a.status.value, "raw_score": a.raw_score},
        })
        for review in a.investigations:
            events.append({
                "event_type": "anomaly_status_changed", "category": TIMELINE_CATEGORIES["anomaly_status_changed"],
                "timestamp": review.created_at.isoformat(), "sensor_id": a.sensor_id, "summary": f"Anomaly reviewed: {review.from_status.value} -> {review.to_status.value}",
                "source_type": "anomaly_investigation", "source_id": review.id,
                "details": {"anomaly_id": a.id, "notes": review.notes},
            })

    # Degradation signals — ANALYTICAL SIGNAL, plus reviews — HUMAN INTERPRETATION
    signals = (
        db.query(DegradationSignal).filter(DegradationSignal.organization_id == organization_id, DegradationSignal.asset_id == asset_id)
        .filter(DegradationSignal.period_end >= start, DegradationSignal.period_start <= end)
        .order_by(DegradationSignal.period_start.desc()).limit(MAX_PER_SOURCE).all()
    )
    for s in signals:
        events.append({
            "event_type": "degradation_signal_detected", "category": TIMELINE_CATEGORIES["degradation_signal_detected"],
            "timestamp": s.period_start.isoformat(), "sensor_id": s.sensor_id, "summary": s.summary,
            "source_type": "degradation_signal", "source_id": s.id,
            "details": {"stage": s.stage.value, "status": s.status.value},
        })
        for review in s.reviews:
            events.append({
                "event_type": "degradation_review", "category": TIMELINE_CATEGORIES["degradation_review"],
                "timestamp": review.created_at.isoformat(), "sensor_id": s.sensor_id,
                "summary": f"Degradation signal reviewed: {review.from_status.value} -> {review.to_status.value}" + (f" (stage {review.from_stage.value} -> {review.to_stage.value})" if review.from_stage != review.to_stage else ""),
                "source_type": "degradation_review", "source_id": review.id,
                "details": {"signal_id": s.id, "notes": review.notes},
            })

    # Forecast runs on this asset's sensors — MODEL OUTPUT
    if sensor_ids:
        forecast_runs = (
            db.query(ForecastRun).join(ForecastModel, ForecastRun.forecast_model_id == ForecastModel.id)
            .filter(ForecastRun.organization_id == organization_id, ForecastModel.sensor_id.in_(sensor_ids))
            .filter(ForecastRun.created_at >= start, ForecastRun.created_at <= end)
            .order_by(ForecastRun.created_at.desc()).limit(MAX_PER_SOURCE).all()
        )
        for r in forecast_runs:
            events.append({
                "event_type": "forecast_run", "category": TIMELINE_CATEGORIES["forecast_run"],
                "timestamp": r.created_at.isoformat(), "sensor_id": r.forecast_model.sensor_id, "summary": f"Forecast generated (as_of {r.as_of.isoformat()}, horizon {r.horizon_seconds:.0f}s, status {r.status.value}).",
                "source_type": "forecast_run", "source_id": r.id,
                "details": {"forecast_model_id": r.forecast_model_id, "status": r.status.value},
            })

    # Component lifecycle events relevant to this asset's sensors — a 5th
    # category (GOVERNANCE) beyond the spec's four — see TIMELINE_CATEGORIES.
    component_ids: set[str] = set()
    for sid in sensor_ids:
        component_ids.update(_components_covering_sensor(db, organization_id=organization_id, sensor_id=sid))
    if component_ids:
        gov_events = (
            db.query(GovernanceEvent)
            .filter(GovernanceEvent.organization_id == organization_id, GovernanceEvent.component_id.in_(component_ids))
            .filter(GovernanceEvent.event_type.in_([GovernanceEventType.ACTIVATED, GovernanceEventType.ROLLED_BACK, GovernanceEventType.RETIRED]))
            .filter(GovernanceEvent.created_at >= start, GovernanceEvent.created_at <= end)
            .order_by(GovernanceEvent.created_at.desc()).limit(MAX_PER_SOURCE).all()
        )
        for g in gov_events:
            component = db.query(IntelligenceComponent).filter(IntelligenceComponent.id == g.component_id).first()
            events.append({
                "event_type": "component_lifecycle_event", "category": TIMELINE_CATEGORIES["component_lifecycle_event"],
                "timestamp": g.created_at.isoformat(), "sensor_id": None,
                "summary": f"{component.name if component else g.component_id}: {g.event_type.value}" + (f" ({g.from_stage.value} -> {g.to_stage.value})" if g.from_stage and g.to_stage else ""),
                "source_type": "governance_event", "source_id": g.id,
                "details": {"component_id": g.component_id, "component_version_id": g.component_version_id, "event_type": g.event_type.value},
            })

    # Investigations opened + status history — HUMAN INTERPRETATION
    investigations = db.query(Investigation).filter(Investigation.organization_id == organization_id, Investigation.asset_id == asset_id).limit(MAX_PER_SOURCE).all()
    for inv in investigations:
        for status_event in inv.status_events:
            if not (start <= _ensure_utc(status_event.created_at) <= end):
                continue
            event_type = "investigation_opened" if status_event.from_status is None else "investigation_status_changed"
            events.append({
                "event_type": event_type, "category": TIMELINE_CATEGORIES[event_type],
                "timestamp": status_event.created_at.isoformat(), "sensor_id": inv.primary_sensor_id,
                "summary": inv.title if status_event.from_status is None else f"Investigation '{inv.title}' status: {status_event.from_status.value} -> {status_event.to_status.value}",
                "source_type": "investigation_status_event", "source_id": status_event.id,
                "details": {"investigation_id": inv.id, "notes": status_event.notes},
            })
        for annotation in inv.annotations:
            if not (start <= _ensure_utc(annotation.created_at) <= end):
                continue
            events.append({
                "event_type": "human_annotation", "category": TIMELINE_CATEGORIES["human_annotation"],
                "timestamp": annotation.created_at.isoformat(), "sensor_id": inv.primary_sensor_id,
                "summary": f"[{annotation.annotation_type.value}] {annotation.content[:200]}",
                "source_type": "investigation_annotation", "source_id": annotation.id,
                "details": {"investigation_id": inv.id, "annotation_type": annotation.annotation_type.value, "follow_up_required": annotation.follow_up_required},
            })

    if event_types:
        events = [e for e in events if e["event_type"] in event_types]

    events.sort(key=lambda e: e["timestamp"], reverse=True)
    total = len(events)
    page = events[offset : offset + limit]

    return {
        "asset_id": asset.id,
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "events": page,
        "total": total,
        "limit": limit,
        "offset": offset,
        "available_event_types": sorted(TIMELINE_CATEGORIES.keys()),
        "note": (
            "Raw measurements are intentionally excluded from this timeline to avoid overwhelming detail — "
            "use Data Explorer / Temporal Analysis for the underlying series. Change/behavior candidates "
            "(app.timeseries.change_detection) are also excluded here because they are computed on demand and "
            "have no stored timest-scoped row to query in bulk across a whole asset; they remain fully "
            "explorable per-sensor via Temporal Analysis and attachable to an investigation as evidence."
        ),
    }


# ---------------------------------------------------------------------------
# §4 — Evidence-based prioritization
# ---------------------------------------------------------------------------


def get_prioritized_signals(db: Session, *, organization_id: str, limit: int) -> dict[str, Any]:
    """A transparent decision-support RANKING, not a probability of failure
    and not a fabricated composite health score. Every item exposes the
    exact factors that produced its position. Reuses the same open-status
    definitions as app.services.condition_service.get_attention_list (which
    remains available unchanged — this is a richer, orchestration-layer
    ranking that adds persistence/cross-detector-agreement/data-quality
    context on top, not a replacement)."""
    signals = (
        db.query(DegradationSignal)
        .filter(DegradationSignal.organization_id == organization_id, DegradationSignal.status.in_(_OPEN_SIGNAL_STATUSES))
        .order_by(DegradationSignal.period_start.desc()).limit(500).all()
    )
    anomalies = (
        db.query(Anomaly)
        .filter(Anomaly.organization_id == organization_id, Anomaly.status.in_(_OPEN_ANOMALY_STATUSES))
        .order_by(Anomaly.period_start.desc()).limit(500).all()
    )

    items: list[dict[str, Any]] = []

    for s in signals:
        persistence_count = sum(1 for other in signals if other.sensor_id == s.sensor_id and other.id != s.id)
        dq_count = (
            db.query(DataQualityIssue)
            .filter(DataQualityIssue.organization_id == organization_id, DataQualityIssue.sensor_id == s.sensor_id)
            .filter(DataQualityIssue.period_end >= s.period_start - timedelta(days=7), DataQualityIssue.period_start <= s.period_end + timedelta(days=7))
            .count()
        )
        factors = {
            "stage_rank": _STAGE_RANK.get(s.stage, 0),
            "severity_rank": 0,
            "persistence_count": min(persistence_count, 5),
            "cross_detector_agreement": False,
            "nearby_data_quality_issue_count": dq_count,
            "human_reviewed": s.status != ConditionSignalStatus.DETECTED,
        }
        score = factors["stage_rank"] * 100 + factors["persistence_count"] * 10
        items.append({
            "item_type": "degradation_signal", "id": s.id, "asset_id": s.asset_id, "sensor_id": s.sensor_id,
            "summary": s.summary, "period_start": s.period_start.isoformat(), "status": s.status.value,
            "score": score, "contributing_factors": factors,
        })

    for a in anomalies:
        related = [o for o in anomalies if o.sensor_id == a.sensor_id and o.id != a.id]
        persistence_count = len(related)
        cross_detector = any(o.detector_type != a.detector_type for o in related)
        dq_count = (
            db.query(DataQualityIssue)
            .filter(DataQualityIssue.organization_id == organization_id, DataQualityIssue.sensor_id == a.sensor_id)
            .filter(DataQualityIssue.period_end >= a.period_start - timedelta(days=7), DataQualityIssue.period_start <= a.period_end + timedelta(days=7))
            .count()
        )
        factors = {
            "stage_rank": 0,
            "severity_rank": _SEVERITY_RANK.get(a.severity_tier, 0),
            "persistence_count": min(persistence_count, 5),
            "cross_detector_agreement": cross_detector,
            "nearby_data_quality_issue_count": dq_count,
            "human_reviewed": a.status != AnomalyStatus.DETECTED,
        }
        score = factors["severity_rank"] * 100 + factors["persistence_count"] * 10 + (25 if cross_detector else 0)
        items.append({
            "item_type": "anomaly", "id": a.id, "asset_id": a.asset_id, "sensor_id": a.sensor_id,
            "summary": a.summary, "period_start": a.period_start.isoformat(), "status": a.status.value,
            "score": score, "contributing_factors": factors,
        })

    items.sort(key=lambda it: (it["score"], it["period_start"]), reverse=True)
    return {
        "items": items[:limit],
        "total_open_degradation_signals": len(signals),
        "total_open_anomalies": len(anomalies),
        "methodology_version": PRIORITIZATION_METHOD_VERSION,
        "methodology": (
            "score = (stage_rank OR severity_rank) * 100 + min(persistence_count, 5) * 10 + (25 if two different "
            "detector types independently flagged the same sensor). stage_rank: validated_degradation=3, "
            "degradation_candidate=2, observed_trend=1. severity_rank: high_priority_anomaly=3, candidate_anomaly=2, "
            "observed_unusual_behavior=1. persistence_count: number of OTHER open signals/anomalies on the same "
            "sensor (a proxy for how repeatedly this sensor has been flagged, capped at 5). "
            "nearby_data_quality_issue_count is surfaced as context, not scored — there is no validated basis for "
            "a specific penalty weight (the same honesty this platform applies to not implementing PSI in Phase 5's "
            "shift analysis). This is a transparent priority ORDERING for human triage, not a probability of failure "
            "and not a fabricated composite health score — no likelihood-of-failure number is computed anywhere in "
            "this platform."
        ),
    }


# ---------------------------------------------------------------------------
# §5 — Cross-layer intelligence synthesis: asset intelligence summary
# ---------------------------------------------------------------------------


def get_asset_intelligence_summary(db: Session, *, organization_id: str, asset_id: str) -> dict[str, Any]:
    """Evidence AGGREGATION, not fabricated reasoning: every number below is
    a real count or a real reference, grouped into the four categories spec
    §5 requires kept distinct. No field here combines unrelated signals
    into a single score."""
    asset = _get_asset(db, organization_id=organization_id, asset_id=asset_id)
    sensor_ids = _sensor_ids_for_asset(db, organization_id=organization_id, asset_id=asset_id)

    issues = db.query(DataQualityIssue).filter(DataQualityIssue.organization_id == organization_id, DataQualityIssue.asset_id == asset_id).all()
    issues_by_severity = {"critical": 0, "warning": 0, "info": 0}
    for i in issues:
        issues_by_severity[i.severity.value] += 1

    open_signals = db.query(DegradationSignal).filter(DegradationSignal.organization_id == organization_id, DegradationSignal.asset_id == asset_id, DegradationSignal.status.in_(_OPEN_SIGNAL_STATUSES)).all()
    stage_breakdown: dict[str, int] = {}
    for s in open_signals:
        stage_breakdown[s.stage.value] = stage_breakdown.get(s.stage.value, 0) + 1

    open_anomalies = db.query(Anomaly).filter(Anomaly.organization_id == organization_id, Anomaly.asset_id == asset_id, Anomaly.status.in_(_OPEN_ANOMALY_STATUSES)).all()
    severity_breakdown: dict[str, int] = {}
    detector_types_seen: set[str] = set()
    for a in open_anomalies:
        severity_breakdown[a.severity_tier.value] = severity_breakdown.get(a.severity_tier.value, 0) + 1
        detector_types_seen.add(a.detector_type.value)

    forecast_models = db.query(ForecastModel).filter(ForecastModel.organization_id == organization_id, ForecastModel.sensor_id.in_(sensor_ids)).all() if sensor_ids else []
    justified_forecasts = [f for f in forecast_models if f.status == ForecastModelStatus.JUSTIFIED]

    investigations = db.query(Investigation).filter(Investigation.organization_id == organization_id, Investigation.asset_id == asset_id).all()
    investigations_by_status: dict[str, int] = {}
    for inv in investigations:
        investigations_by_status[inv.status.value] = investigations_by_status.get(inv.status.value, 0) + 1
    annotation_count = db.query(InvestigationAnnotation).join(Investigation, InvestigationAnnotation.investigation_id == Investigation.id).filter(Investigation.asset_id == asset_id, Investigation.organization_id == organization_id).count()

    unreviewed_anomalies = sum(1 for a in open_anomalies if a.status == AnomalyStatus.DETECTED)
    unreviewed_signals = sum(1 for s in open_signals if s.status == ConditionSignalStatus.DETECTED)

    limitations: list[str] = []
    if not justified_forecasts:
        limitations.append("No forecasting model is currently justified (backtest-validated) for any sensor on this asset — see Condition Intelligence for details.")
    if unreviewed_anomalies:
        limitations.append(f"{unreviewed_anomalies} open anomaly(ies) have not yet been reviewed by a human.")
    if unreviewed_signals:
        limitations.append(f"{unreviewed_signals} open degradation signal(s) have not yet been reviewed by a human.")
    if issues_by_severity["critical"] > 0:
        limitations.append(f"{issues_by_severity['critical']} critical data-quality issue(s) may limit confidence in signals derived from affected sensors.")
    if not sensor_ids:
        limitations.append("This asset has no sensors configured.")

    return {
        "asset_id": asset.id, "asset_code": asset.code, "asset_name": asset.name,
        "sensor_count": len(sensor_ids),
        "observed_data": {
            "total_data_quality_issues": len(issues),
            "issues_by_severity": issues_by_severity,
        },
        "analytical_signals": {
            "open_degradation_signal_count": len(open_signals),
            "degradation_stage_breakdown": stage_breakdown,
            "change_candidate_note": "Change/behavior candidates are computed on demand per sensor (Temporal Analysis) and are not aggregated into this summary — see the asset timeline note.",
        },
        "model_outputs": {
            "open_anomaly_count": len(open_anomalies),
            "anomaly_severity_breakdown": severity_breakdown,
            "detector_types_active": sorted(detector_types_seen),
            "forecast_models_fitted": len(forecast_models),
            "forecast_models_justified": len(justified_forecasts),
        },
        "human_interpretations": {
            "investigation_count": len(investigations),
            "investigations_by_status": investigations_by_status,
            "annotation_count": annotation_count,
            "unreviewed_anomaly_count": unreviewed_anomalies,
            "unreviewed_degradation_signal_count": unreviewed_signals,
        },
        "limitations": limitations,
    }


# ---------------------------------------------------------------------------
# §8 — Contextual cross-sensor investigation
# ---------------------------------------------------------------------------


def get_cross_sensor_context(
    db: Session, *, organization_id: str, asset_id: str, sensor_ids: list[str], start: datetime, end: datetime, freq_seconds: float
) -> dict[str, Any]:
    """Real correlation (app.timeseries.multi_sensor, built in Phase 2) plus
    an overlay of real open anomalies/degradation signals on each sensor in
    the window. Language is deliberately non-causal throughout — see the
    `note` field and every per-pair description below. NEVER says "caused
    by"."""
    asset = _get_asset(db, organization_id=organization_id, asset_id=asset_id)
    valid_sensor_ids = set(_sensor_ids_for_asset(db, organization_id=organization_id, asset_id=asset_id))
    for sid in sensor_ids:
        if sid not in valid_sensor_ids:
            raise ValidationFailedError(f"Sensor '{sid}' does not belong to asset '{asset_id}'.")
    if len(sensor_ids) < 2:
        raise ValidationFailedError("At least 2 sensor_ids (belonging to this asset) are required for cross-sensor context.")

    start, end = _ensure_utc(start), _ensure_utc(end)
    try:
        series_by_sensor = load_multi_sensor_series(db, sensor_ids=sensor_ids, start=start, end=end)
    except TooManyObservationsError as exc:
        raise ValidationFailedError(str(exc))
    aligned = multi_sensor.align_sensors_on_grid(series_by_sensor, start=start, end=end, freq_seconds=freq_seconds)
    correlation = multi_sensor.correlation_matrix(aligned)

    per_sensor_signals: dict[str, Any] = {}
    for sid in sensor_ids:
        anomalies = (
            db.query(Anomaly).filter(Anomaly.organization_id == organization_id, Anomaly.sensor_id == sid, Anomaly.status.in_(_OPEN_ANOMALY_STATUSES))
            .filter(Anomaly.period_end >= start, Anomaly.period_start <= end).all()
        )
        signals = (
            db.query(DegradationSignal).filter(DegradationSignal.organization_id == organization_id, DegradationSignal.sensor_id == sid, DegradationSignal.status.in_(_OPEN_SIGNAL_STATUSES))
            .filter(DegradationSignal.period_end >= start, DegradationSignal.period_start <= end).all()
        )
        per_sensor_signals[sid] = {
            "open_anomaly_count": len(anomalies),
            "open_degradation_signal_count": len(signals),
            "anomaly_periods": [{"id": a.id, "period_start": a.period_start.isoformat(), "period_end": a.period_end.isoformat(), "severity_tier": a.severity_tier.value} for a in anomalies],
        }

    associations: list[str] = []
    for sid, other_signals in per_sensor_signals.items():
        if other_signals["open_anomaly_count"] > 0:
            for other_sid, s in per_sensor_signals.items():
                if other_sid != sid and s["open_anomaly_count"] > 0:
                    associations.append(
                        f"Sensor {sid} and sensor {other_sid} both show open anomalies during the same window — a "
                        f"temporal association only; this is not evidence that one caused the other."
                    )

    return {
        "asset_id": asset.id,
        "sensor_ids": sensor_ids,
        "window": {"start": start.isoformat(), "end": end.isoformat(), "freq_seconds": freq_seconds},
        "correlation": correlation,
        "per_sensor_signals": per_sensor_signals,
        "observed_associations": sorted(set(associations)),
        "note": (
            "Correlation and co-occurrence describe association, not causation. Language such as 'changed during "
            "the same period' or 'associated pattern observed' describes what the data shows; this platform never "
            "asserts that one sensor's behavior caused another's without independent engineering evidence, which "
            "it cannot supply."
        ),
    }


# ---------------------------------------------------------------------------
# §11 — Model and method provenance in user workflows
# ---------------------------------------------------------------------------


def get_evidence_provenance(db: Session, *, organization_id: str, evidence_type: str, evidence_id: str) -> dict[str, Any]:
    """Signal -> Method/Detector/Model -> Version -> Configuration ->
    Feature/Data Definition -> Training/Reference Context -> Evaluation
    Evidence, per spec §11. Returns a `summary` level (what a normal user
    needs) and a `technical` level (full config/evaluation payload) for
    progressive disclosure — the frontend decides how much to show by
    default."""
    if evidence_type == "anomaly":
        anomaly = db.query(Anomaly).filter(Anomaly.id == evidence_id, Anomaly.organization_id == organization_id).first()
        if anomaly is None:
            raise NotFoundError(f"Anomaly '{evidence_id}' not found.")
        model_run = anomaly.model_run
        model_version = model_run.model_version if model_run else None
        governance = None
        if model_version:
            cv = db.query(ComponentVersion).filter(ComponentVersion.model_version_id == model_version.id).first()
            if cv:
                component = db.query(IntelligenceComponent).filter(IntelligenceComponent.id == cv.component_id).first()
                governance = {
                    "component_id": cv.component_id, "component_name": component.name if component else None,
                    "component_type": component.component_type.value if component else None,
                    "version_number": cv.version_number, "lifecycle_stage": cv.lifecycle_stage.value,
                    "is_active": cv.lifecycle_stage == LifecycleStage.ACTIVE,
                }
        return {
            "evidence_type": "anomaly", "evidence_id": anomaly.id,
            "summary": {
                "what_happened": anomaly.summary,
                "when": {"period_start": anomaly.period_start.isoformat(), "period_end": anomaly.period_end.isoformat()},
                "method": f"{anomaly.detector_type.value} detector" if not model_version else f"{anomaly.detector_type.value} detector '{model_version.name}' (v{model_version.version})",
                "severity_tier": anomaly.severity_tier.value,
                "governance_status": governance["lifecycle_stage"] if governance else "not registered in the intelligence lifecycle registry",
            },
            "technical": {
                "detector_type": anomaly.detector_type.value,
                "raw_score": anomaly.raw_score, "normalized_score": anomaly.normalized_score, "threshold_used": anomaly.threshold_used,
                "contributing_signals": anomaly.contributing_signals,
                "data_quality_context": anomaly.data_quality_context,
                "model_version": ({
                    "id": model_version.id, "name": model_version.name, "version": model_version.version,
                    "detector_config": model_version.detector_config, "feature_schema": model_version.feature_schema,
                    "reference_period": {"start": model_version.reference_period_start.isoformat(), "end": model_version.reference_period_end.isoformat()},
                    "reference_observation_count": model_version.reference_observation_count,
                    "baseline_summary": model_version.baseline_summary,
                    "evaluation_results": model_version.evaluation_results,
                } if model_version else None),
                "model_run": ({"id": model_run.id, "scored_period": {"start": model_run.scored_period_start.isoformat(), "end": model_run.scored_period_end.isoformat()}, "metrics": model_run.metrics} if model_run else None),
                "governance": governance,
            },
        }

    if evidence_type == "degradation_signal":
        signal = db.query(DegradationSignal).filter(DegradationSignal.id == evidence_id, DegradationSignal.organization_id == organization_id).first()
        if signal is None:
            raise NotFoundError(f"Degradation signal '{evidence_id}' not found.")
        run = signal.condition_run
        return {
            "evidence_type": "degradation_signal", "evidence_id": signal.id,
            "summary": {
                "what_happened": signal.summary,
                "when": {"period_start": signal.period_start.isoformat(), "period_end": signal.period_end.isoformat()},
                "method": "heuristic degradation classification (deterministic rules over sub-window evidence trends — not a fitted/trained model)",
                "stage": signal.stage.value,
                "governance_status": (
                    "Heuristic methods have no per-signal linking id in this platform's governance schema "
                    "(app.models.mlops.ComponentVersion.heuristic_code_version is a free-text label, not a "
                    "foreign key) — see docs/MLOPS_GOVERNANCE.md and docs/INTELLIGENCE_ORCHESTRATION.md §11 "
                    "for why this specific traceability gap is a documented, honest limitation rather than a bug."
                ),
            },
            "technical": {
                "evidence": signal.evidence,
                "condition_run": ({
                    "id": run.id, "config": run.config,
                    "analysis_period": {"start": run.analysis_period_start.isoformat(), "end": run.analysis_period_end.isoformat()},
                    "reference_period": {"start": run.reference_period_start.isoformat(), "end": run.reference_period_end.isoformat()},
                    "metrics": run.metrics,
                } if run else None),
            },
        }

    raise ValidationFailedError(f"Provenance lookup is only implemented for evidence_type in ('anomaly', 'degradation_signal'), not '{evidence_type}'.")
