"""Narrow, validated evidence-retrieval tools.

Every function here is the ONLY way the copilot orchestrator touches the
database — the LLM never sees a connection string, never gets to run
arbitrary SQL, and never receives more than the bounded evidence these
functions choose to return. Each function:

- takes `organization_id` (already authenticated/authorized by the API
  layer — see app/core/deps.py) and filters every query by it, the same
  defense-in-depth pattern every other service in this codebase uses;
- returns a bounded list of `EvidenceItem`s (see app/copilot/grounding.py),
  never a raw ORM object or an unbounded query result;
- raises the same `AppError` subclasses as the rest of the API
  (`NotFoundError`, `AuthorizationError`) when the referenced record
  doesn't exist or doesn't belong to this organization — the copilot API
  layer turns these into the same honest error responses as every other
  endpoint, never a fabricated "I couldn't find anything interesting";
- explicitly labels evidence as synthetic-demo data where the underlying
  organization/dataset is (see app/synthetic/generator.py's "(Synthetic
  Demo)" naming convention) so the copilot never has to guess.

This is deliberately a STRUCTURED retrieval layer, not a vector-search
layer — every source here (assets, anomalies, investigations...) is
already relational data with a real id and real filters, so a keyword/
vector index would add complexity without solving a problem structured
queries don't already solve better. Documentation search
(`search_documentation`) is the one source that's genuinely unstructured
text, and it uses plain keyword matching over the repository's own
markdown docs — good enough at 13 short documents, and honestly not
described as "semantic search."
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.copilot.grounding import EvidenceCategory, EvidenceItem
from app.core.errors import NotFoundError
from app.models.asset import Asset
from app.models.condition import DegradationSignal, ForecastModel, ForecastRun
from app.models.data_quality import DataQualityIssue
from app.models.intelligence import Anomaly
from app.models.investigation import Investigation, InvestigationEvidenceType
from app.models.outcome import Outcome, OutcomeSignalLink, OutcomeSignalType
from app.models.scenario import ScenarioRun
from app.models.sensor import Sensor
from app.services import anomaly_service, condition_service, feedback_service, investigation_service, orchestration_service, quality_service, scenario_service

_DOCS_DIR = Path(__file__).resolve().parents[3] / "docs"

MAX_ANOMALIES_PER_ASSET = 8
MAX_DQ_ISSUES = 8
MAX_TIMELINE_EVENTS = 15
MAX_DOC_HITS = 4
MAX_RECENT_ANOMALIES = 5
MAX_OUTCOME_LINKS = 6


def _asset_label(asset: Asset) -> str:
    return f"{asset.code} ({asset.name})"


def _org_synthetic_note(is_synthetic: bool) -> str:
    return " This organization's data is labeled synthetic/demo data — see docs/SYNTHETIC_DATA.md." if is_synthetic else ""


def _is_synthetic_org(db: Session, organization_id: str) -> bool:
    from app.models.organization import Organization

    org = db.get(Organization, organization_id)
    return bool(org and "synthetic" in org.name.lower())


# ---------------------------------------------------------------------------
# 1. Asset summary
# ---------------------------------------------------------------------------


def get_asset_summary(db: Session, *, organization_id: str, asset_id: str) -> list[EvidenceItem]:
    asset = db.query(Asset).filter(Asset.id == asset_id, Asset.organization_id == organization_id).first()
    if asset is None:
        raise NotFoundError(f"Asset '{asset_id}' was not found.")

    synthetic_note = _org_synthetic_note(_is_synthetic_org(db, organization_id))
    sensors = db.query(Sensor).filter(Sensor.asset_id == asset.id).all()
    sensor_lines = "\n".join(
        f"- {s.code} ({s.name}): {s.measurement_type}, unit={s.unit}"
        + (f", expected range [{s.expected_min}, {s.expected_max}]" if s.expected_min is not None else "")
        for s in sensors
    ) or "(no sensors registered on this asset)"

    items = [
        EvidenceItem(
            category=EvidenceCategory.OBSERVED_FACT,
            source_type="asset",
            source_id=asset.id,
            label=_asset_label(asset),
            text=(
                f"Asset {asset.code} — {asset.name}, type={asset.asset_type}"
                f"{', location=' + asset.location if asset.location else ''}. "
                f"{asset.description or ''} Sensors on this asset:\n{sensor_lines}.{synthetic_note}"
            ),
        )
    ]
    return items


# ---------------------------------------------------------------------------
# 2. Anomaly evidence (detection result + context + provenance)
# ---------------------------------------------------------------------------


def get_anomaly_evidence(db: Session, *, organization_id: str, anomaly_id: str) -> list[EvidenceItem]:
    anomaly = anomaly_service.get_anomaly(db, organization_id=organization_id, anomaly_id=anomaly_id)
    context = anomaly_service.get_anomaly_context(db, organization_id=organization_id, anomaly_id=anomaly_id)
    reviews = anomaly_service.list_anomaly_investigations(db, organization_id=organization_id, anomaly_id=anomaly_id)

    items = [
        EvidenceItem(
            category=EvidenceCategory.MODEL_OUTPUT,
            source_type="anomaly",
            source_id=anomaly.id,
            label=f"Anomaly {anomaly.id[:8]} ({anomaly.severity_tier.value})",
            period_start=anomaly.period_start,
            period_end=anomaly.period_end,
            method_version=f"{anomaly.detector_type.value} detector",
            text=(
                f"Detector flagged: {anomaly.summary} Status: {anomaly.status.value}. "
                f"Severity tier: {anomaly.severity_tier.value} (raw score {anomaly.raw_score:.3f}, "
                f"threshold {anomaly.threshold_used:.3f}). This is a statistical anomaly — a candidate for "
                f"human investigation, not a confirmed fault or a failure prediction."
                + (
                    " This specific anomaly was produced by a SYNTHETIC evaluation scenario (deliberately "
                    "injected/simulated data used to test detector behavior), not a detection on real sensor "
                    "readings — do not present it as evidence of a real equipment condition."
                    if anomaly.is_synthetic_scenario else ""
                )
            ),
        )
    ]

    before = context.get("data", {}).get("before")
    during = context.get("data", {}).get("during")
    after = context.get("data", {}).get("after")
    if before or during or after:
        items.append(
            EvidenceItem(
                category=EvidenceCategory.ANALYTICAL_RESULT,
                source_type="anomaly_context",
                source_id=anomaly.id,
                label="Before/during/after comparison",
                period_start=anomaly.period_start,
                period_end=anomaly.period_end,
                text=f"Observed statistics around this anomaly — before: {before}, during: {during}, after: {after}.",
            )
        )

    dq = anomaly.data_quality_context
    if dq:
        items.append(
            EvidenceItem(
                category=EvidenceCategory.ANALYTICAL_RESULT,
                source_type="data_quality_context",
                source_id=anomaly.id,
                label="Data-quality context for this anomaly",
                text=f"Data-quality context recorded at detection time: {dq}.",
            )
        )

    for review in reviews:
        items.append(
            EvidenceItem(
                category=EvidenceCategory.HUMAN_ANNOTATION,
                source_type="anomaly_investigation",
                source_id=review.id,
                label=f"Human review: {review.from_status.value} -> {review.to_status.value}",
                period_start=review.created_at,
                text=f"A human reviewer changed status from {review.from_status.value} to {review.to_status.value}."
                + (f" Notes: {review.notes}" if review.notes else " No notes were recorded."),
            )
        )

    try:
        prov = orchestration_service.get_evidence_provenance(
            db, organization_id=organization_id, evidence_type="anomaly", evidence_id=anomaly.id
        )
        summary = prov.get("summary", {})
        items.append(
            EvidenceItem(
                category=EvidenceCategory.MODEL_OUTPUT,
                source_type="provenance",
                source_id=anomaly.id,
                label="Method / governance provenance",
                method_version=summary.get("method"),
                text=(
                    f"Method: {summary.get('method')}. Governance status: {summary.get('governance_status')}. "
                    "This traces the anomaly to the exact detector/version that produced it."
                ),
            )
        )
    except Exception:
        pass  # provenance is best-effort context, never blocks the core anomaly evidence

    items += _outcome_link_evidence_for_signal(db, organization_id=organization_id, signal_type=OutcomeSignalType.ANOMALY, signal_id=anomaly.id)

    return items[:MAX_ANOMALIES_PER_ASSET]


# ---------------------------------------------------------------------------
# Closed-loop outcome/evaluation evidence. Every item below
# must distinguish a human REPORT from a human VERIFICATION — an unverified
# outcome is surfaced, but always labeled as such, never presented as
# confirmed ground truth. Nothing here ever infers a conclusion the
# platform doesn't already have recorded.
# ---------------------------------------------------------------------------


def _outcome_link_evidence_for_signal(db: Session, *, organization_id: str, signal_type: OutcomeSignalType, signal_id: str) -> list[EvidenceItem]:
    """Surfaces any outcomes a human has linked to this specific signal —
    called from get_anomaly_evidence (and available for the same purpose
    on other signal kinds) so an answer about a prediction also shows
    whatever real-world follow-up a human has recorded against it."""
    links = (
        db.query(OutcomeSignalLink)
        .filter(
            OutcomeSignalLink.organization_id == organization_id,
            OutcomeSignalLink.signal_type == signal_type,
            OutcomeSignalLink.signal_id == signal_id,
        )
        .order_by(OutcomeSignalLink.linked_at.desc())
        .limit(MAX_OUTCOME_LINKS)
        .all()
    )
    items: list[EvidenceItem] = []
    for link in links:
        outcome = db.get(Outcome, link.outcome_id)
        if outcome is None:
            continue
        is_terminal = outcome.verification_status.value in ("verified", "rejected")
        items.append(
            EvidenceItem(
                category=EvidenceCategory.HUMAN_ANNOTATION,
                source_type="outcome_signal_link",
                source_id=link.id,
                label=f"Reported outcome linked to this signal ({outcome.verification_status.value})",
                period_start=outcome.outcome_timestamp,
                text=(
                    f"A human reported an outcome of type '{outcome.outcome_type.value}' ({outcome.description}) and linked it to "
                    f"this {signal_type.value} signal (within_horizon={link.within_horizon}, signal_assessment={link.signal_assessment.value}). "
                    f"Verification status: {outcome.verification_status.value}."
                    + (
                        ""
                        if is_terminal
                        else " This outcome has NOT yet been independently verified or rejected by a human reviewer — treat it as "
                        "an unverified report, not confirmed ground truth."
                    )
                    + (
                        " SIMULATED SCENARIO RESULT — this link never counts toward real evaluation."
                        if signal_type == OutcomeSignalType.SCENARIO_RUN
                        else ""
                    )
                ),
            )
        )
    return items


def _outcome_link_evidence_for_outcome(db: Session, *, organization_id: str, outcome: Outcome) -> list[EvidenceItem]:
    links = feedback_service.list_links_for_outcome(db, organization_id=organization_id, outcome_id=outcome.id)
    items: list[EvidenceItem] = []
    for link in links:
        items.append(
            EvidenceItem(
                category=EvidenceCategory.HUMAN_ANNOTATION,
                source_type="outcome_signal_link",
                source_id=link.id,
                label=f"Linked signal: {link.signal_type.value} {link.signal_id[:8]}",
                period_start=link.prediction_timestamp,
                text=(
                    f"This outcome is linked to a {link.signal_type.value} signal (id {link.signal_id}). "
                    f"within_horizon={link.within_horizon}, signal_assessment={link.signal_assessment.value}, "
                    f"horizon_seconds={link.horizon_seconds}."
                ),
            )
        )
    return items


def get_outcome_evidence(db: Session, *, organization_id: str, outcome_id: str) -> list[EvidenceItem]:
    """Retrieves ONE reported outcome's full provenance: the report itself,
    its verification history (if any), and every signal it has been
    linked to. Never upgrades an UNVERIFIED outcome's status in how it is
    described here — the text always states the current verification_status
    plainly."""
    outcome = feedback_service.get_outcome(db, organization_id=organization_id, outcome_id=outcome_id)
    verifications = feedback_service.list_verifications_for_outcome(db, organization_id=organization_id, outcome_id=outcome_id)

    items = [
        EvidenceItem(
            category=EvidenceCategory.HUMAN_ANNOTATION,
            source_type="outcome",
            source_id=outcome.id,
            label=f"Reported outcome ({outcome.verification_status.value}): {outcome.outcome_type.value}",
            period_start=outcome.outcome_timestamp,
            text=(
                f"A human reported this outcome ({outcome.source.value}): '{outcome.description}'. "
                f"outcome_type={outcome.outcome_type.value}. Current verification_status={outcome.verification_status.value}. "
                "A maintenance or operational event report is NOT automatically a confirmed failure, and an unverified report "
                "is NOT automatically ground truth — both require an explicit human verification step."
            ),
        )
    ]
    for verification in verifications:
        items.append(
            EvidenceItem(
                category=EvidenceCategory.HUMAN_ANNOTATION,
                source_type="outcome_verification",
                source_id=verification.id,
                label=f"Verification: {verification.from_status.value} -> {verification.to_status.value}",
                period_start=verification.created_at,
                text=(
                    f"A reviewer ({verification.reviewer_role or 'unknown role'}) changed this outcome's status from "
                    f"{verification.from_status.value} to {verification.to_status.value}."
                    + (f" Evidence note: {verification.evidence_note}" if verification.evidence_note else " No evidence note was recorded.")
                ),
            )
        )
    items += _outcome_link_evidence_for_outcome(db, organization_id=organization_id, outcome=outcome)
    return items


def get_evaluation_evidence(db: Session, *, organization_id: str, run_id: str) -> list[EvidenceItem]:
    """Retrieves one computed OutcomeEvaluationRun's metrics, stated with
    its own `computed` flag intact — never upgraded to a confident number
    when the underlying run reports it did not have enough verified
    evidence."""
    run = feedback_service.get_evaluation_run(db, organization_id=organization_id, run_id=run_id)
    metrics = run.metrics or {}
    computed = metrics.get("computed", False)
    precision_text = (
        f"precision={metrics.get('precision')} (true_positive={metrics.get('true_positive_count')}, "
        f"false_positive={metrics.get('false_positive_count')})"
        if computed
        else f"NOT COMPUTED — {metrics.get('reason', 'insufficient verified evidence')}"
    )
    return [
        EvidenceItem(
            category=EvidenceCategory.ANALYTICAL_RESULT,
            source_type="outcome_evaluation_run",
            source_id=run.id,
            label=f"Evaluation run ({run.evaluation_kind.value}, {run.signal_type.value})",
            period_start=run.evaluation_period_start,
            period_end=run.evaluation_period_end,
            text=(
                f"A {run.evaluation_kind.value} evaluation run over {run.signal_type.value} signals, scope: "
                f"{run.scope_description}. Coverage classification: {metrics.get('coverage_classification', 'unknown')}. "
                f"Metric: {precision_text}. Recall/F1 are never reported — this platform cannot enumerate signals that "
                "should have fired but did not."
            ),
        )
    ]


# ---------------------------------------------------------------------------
# 2b. Recent anomalies for an asset (lighter-weight than get_anomaly_evidence
#     per-anomaly — used to give an asset-level question a starting point
#     without pulling full context/provenance for every anomaly on record).
# ---------------------------------------------------------------------------


def get_recent_anomalies_for_asset(db: Session, *, organization_id: str, asset_id: str) -> list[EvidenceItem]:
    asset = db.query(Asset).filter(Asset.id == asset_id, Asset.organization_id == organization_id).first()
    if asset is None:
        raise NotFoundError(f"Asset '{asset_id}' was not found.")

    rows, _total = anomaly_service.list_anomalies(
        db,
        organization_id=organization_id,
        asset_id=asset_id,
        sensor_id=None,
        status=None,
        severity_tier=None,
        start=None,
        end=None,
        limit=MAX_RECENT_ANOMALIES,
        offset=0,
    )
    items = []
    for anomaly in rows:
        items.append(
            EvidenceItem(
                category=EvidenceCategory.MODEL_OUTPUT,
                source_type="anomaly",
                source_id=anomaly.id,
                label=f"Anomaly {anomaly.id[:8]} ({anomaly.severity_tier.value}, {anomaly.status.value})",
                period_start=anomaly.period_start,
                period_end=anomaly.period_end,
                method_version=f"{anomaly.detector_type.value} detector",
                text=(
                    f"{anomaly.summary} Severity tier: {anomaly.severity_tier.value}, status: {anomaly.status.value}. "
                    "A statistical anomaly is a candidate for human investigation, not a confirmed fault."
                ),
            )
        )
    if not items:
        items.append(
            EvidenceItem(
                category=EvidenceCategory.INTERPRETATION_LIMITATION,
                source_type="anomaly",
                source_id=asset_id,
                label="No recent anomalies",
                text=f"No anomalies are on record for asset {asset.code} in this organization.",
            )
        )
    return items


# ---------------------------------------------------------------------------
# 3. Investigation timeline
# ---------------------------------------------------------------------------


def get_investigation_timeline(db: Session, *, organization_id: str, investigation_id: str) -> list[EvidenceItem]:
    investigation = investigation_service.get_investigation(db, organization_id=organization_id, investigation_id=investigation_id)
    history = investigation_service.list_status_history(db, organization_id=organization_id, investigation_id=investigation_id)
    evidence_links = investigation_service.list_evidence(db, organization_id=organization_id, investigation_id=investigation_id)
    annotations = investigation_service.list_annotations(db, organization_id=organization_id, investigation_id=investigation_id)

    items = [
        EvidenceItem(
            category=EvidenceCategory.OBSERVED_FACT,
            source_type="investigation",
            source_id=investigation.id,
            label=investigation.title,
            period_start=investigation.time_window_start,
            period_end=investigation.time_window_end,
            text=f"Investigation '{investigation.title}' — status {investigation.status.value}. {investigation.description or ''}",
        )
    ]
    for h in history:
        items.append(
            EvidenceItem(
                category=EvidenceCategory.HUMAN_ANNOTATION,
                source_type="investigation_status_event",
                source_id=h.id,
                label=f"Status change: {h.from_status.value if h.from_status else 'opened'} -> {h.to_status.value}",
                period_start=h.created_at,
                text=(f"Status moved from {h.from_status.value if h.from_status else 'opened'} to {h.to_status.value}.")
                + (f" Notes: {h.notes}" if h.notes else ""),
            )
        )
    for link in evidence_links:
        if link.evidence_type == InvestigationEvidenceType.SCENARIO_RESULT and link.evidence_id:
            #: a linked scenario is HYPOTHETICAL evidence, never
            # observed — the copilot must say so explicitly here, not just
            # rely on the scenario's own results carrying the label.
            run = db.query(ScenarioRun).filter(ScenarioRun.id == link.evidence_id, ScenarioRun.organization_id == organization_id).first()
            status_text = run.status.value if run else "unknown"
            items.append(
                EvidenceItem(
                    category=EvidenceCategory.ANALYTICAL_RESULT,
                    source_type="scenario_result",
                    source_id=link.id,
                    label=f"SIMULATED scenario result attached (run {link.evidence_id[:8]})",
                    text=(
                        f"This investigation references a hypothetical, SIMULATED scenario run (status={status_text}) as "
                        "supporting evidence — not a real observation. It explores what the platform's real detectors and "
                        "condition evidence would show under a declared what-if modification of real historical data. It "
                        "must never be presented as something that was actually observed."
                    )
                    + (f" Note: {link.relation_note}" if link.relation_note else ""),
                )
            )
            continue
        items.append(
            EvidenceItem(
                category=EvidenceCategory.ANALYTICAL_RESULT,
                source_type="evidence_link",
                source_id=link.id,
                label=f"Evidence attached: {link.evidence_type.value}",
                text=(
                    f"This investigation references a {link.evidence_type.value} "
                    f"(id {link.evidence_id or 'snapshot-only'}) as evidence."
                )
                + (f" Note: {link.relation_note}" if link.relation_note else ""),
            )
        )
    for a in annotations:
        items.append(
            EvidenceItem(
                category=EvidenceCategory.HUMAN_ANNOTATION,
                source_type="annotation",
                source_id=a.id,
                label=f"Human annotation ({a.annotation_type.value})",
                period_start=a.created_at,
                text=a.content,
            )
        )
    return items[:MAX_TIMELINE_EVENTS]


# ---------------------------------------------------------------------------
# 4. Data-quality context
# ---------------------------------------------------------------------------


def get_data_quality_context(db: Session, *, organization_id: str, asset_id: str) -> list[EvidenceItem]:
    asset = db.query(Asset).filter(Asset.id == asset_id, Asset.organization_id == organization_id).first()
    if asset is None:
        raise NotFoundError(f"Asset '{asset_id}' was not found.")

    summary = quality_service.summarize_for_asset(db, organization_id=organization_id, asset_id=asset_id)
    items = [
        EvidenceItem(
            category=EvidenceCategory.ANALYTICAL_RESULT,
            source_type="data_quality_summary",
            source_id=asset_id,
            label=f"Data-quality summary for {asset.code}",
            text=(
                f"{summary.total_issues} total data-quality issues on {asset.code} "
                f"({summary.critical_count} critical, {summary.warning_count} warning, {summary.info_count} info). "
                f"By type: {summary.issues_by_type}."
            ),
        )
    ]
    issues = (
        db.query(DataQualityIssue)
        .filter(DataQualityIssue.organization_id == organization_id, DataQualityIssue.asset_id == asset_id)
        .order_by(DataQualityIssue.detected_at.desc())
        .limit(MAX_DQ_ISSUES)
        .all()
    )
    for issue in issues:
        items.append(
            EvidenceItem(
                category=EvidenceCategory.ANALYTICAL_RESULT,
                source_type="data_quality_issue",
                source_id=issue.id,
                label=f"{issue.issue_type.value} ({issue.severity.value})",
                period_start=issue.period_start,
                period_end=issue.period_end,
                text=issue.description,
            )
        )
    return items


# ---------------------------------------------------------------------------
# 5. Condition / degradation / forecast history
# ---------------------------------------------------------------------------


def get_condition_history(db: Session, *, organization_id: str, sensor_id: str) -> list[EvidenceItem]:
    sensor = db.query(Sensor).join(Sensor.asset).filter(
        Sensor.id == sensor_id, Asset.organization_id == organization_id
    ).first()
    if sensor is None:
        raise NotFoundError(f"Sensor '{sensor_id}' was not found.")

    items: list[EvidenceItem] = []

    signals = (
        db.query(DegradationSignal)
        .filter(DegradationSignal.organization_id == organization_id, DegradationSignal.sensor_id == sensor_id)
        .order_by(DegradationSignal.created_at.desc())
        .limit(5)
        .all()
    )
    for s in signals:
        items.append(
            EvidenceItem(
                category=EvidenceCategory.ANALYTICAL_RESULT,
                source_type="degradation_signal",
                source_id=s.id,
                label=f"Degradation signal ({s.stage.value})",
                period_start=s.period_start,
                period_end=s.period_end,
                text=(
                    f"{s.summary} Stage: {s.stage.value}, status: {s.status.value}. "
                    "A degradation candidate/observed-trend is engineering evidence, not a confirmed failure — "
                    "the 'validated_degradation' stage is only ever reached by an explicit human review action."
                ),
            )
        )

    models = (
        db.query(ForecastModel)
        .filter(ForecastModel.organization_id == organization_id, ForecastModel.sensor_id == sensor_id)
        .order_by(ForecastModel.created_at.desc())
        .limit(3)
        .all()
    )
    for m in models:
        eval_summary = (m.evaluation_results or {}).get("reason") if m.evaluation_results else None
        items.append(
            EvidenceItem(
                category=EvidenceCategory.MODEL_OUTPUT,
                source_type="forecast_model",
                source_id=m.id,
                label=f"Forecast model {m.name} v{m.version} ({m.status.value})",
                method_version=f"{m.method.value} v{m.version}",
                text=(
                    f"Forecast fitting status: {m.status.value}. "
                    + (
                        "This method was NOT shown to beat naive baselines on a real backtest — no forecast "
                        "should be treated as reliable from this model."
                        if m.status.value == "not_justified"
                        else "This method beat naive baselines on a real backtest, so forecasting is permitted."
                        if m.status.value == "justified"
                        else "Fitting failed (see training_error)."
                    )
                    + (f" Reason: {eval_summary}" if eval_summary else "")
                ),
            )
        )
        if m.status.value == "justified":
            runs = (
                db.query(ForecastRun)
                .filter(ForecastRun.forecast_model_id == m.id)
                .order_by(ForecastRun.created_at.desc())
                .limit(1)
                .all()
            )
            for r in runs:
                n_points = len(r.points) if r.points else 0
                items.append(
                    EvidenceItem(
                        category=EvidenceCategory.MODEL_OUTPUT,
                        source_type="forecast_run",
                        source_id=r.id,
                        label=f"Forecast run ({r.status.value})",
                        period_start=r.as_of,
                        text=f"Forecast run status {r.status.value}, {n_points} forward-looking points generated, "
                        f"horizon {r.horizon_seconds}s. A forecast is a projection from a backtested model, not a guarantee.",
                    )
                )

    if not items:
        items.append(
            EvidenceItem(
                category=EvidenceCategory.INTERPRETATION_LIMITATION,
                source_type="condition_history",
                source_id=sensor_id,
                label="No condition evidence available",
                text=f"No degradation signals or forecast models exist yet for sensor {sensor.code}.",
            )
        )
    return items


# ---------------------------------------------------------------------------
# 5b. Scenario simulation evidence — ALWAYS labeled
# simulated. This is the only copilot tool that can return a hypothetical
# result rather than something observed or computed on real data; every
# EvidenceItem it produces says so explicitly, on top of
# app.copilot.grounding's SYSTEM_PROMPT_TEMPLATE rule #8-style caveats.
# ---------------------------------------------------------------------------

MAX_SCENARIO_RUNS = 5


def get_scenario_evidence(db: Session, *, organization_id: str, scenario_id: str) -> list[EvidenceItem]:
    """Retrieves a REAL, already-computed scenario and its most recent
    run(s) — never invents a hypothetical result, and never presents one as
    an observation. If asked to imagine a scenario that was never actually
    run, the orchestrator has nothing to retrieve here and the copilot must
    say so rather than describing a result that doesn't exist."""
    scenario = scenario_service.get_scenario(db, organization_id=organization_id, scenario_id=scenario_id)
    runs, _total = scenario_service.list_scenario_runs(db, organization_id=organization_id, scenario_id=scenario_id, limit=MAX_SCENARIO_RUNS, offset=0)

    items = [
        EvidenceItem(
            category=EvidenceCategory.ANALYTICAL_RESULT,
            source_type="scenario",
            source_id=scenario.id,
            label=f"HYPOTHETICAL scenario: {scenario.name} ({scenario.scenario_type.value})",
            period_start=scenario.baseline_window_start,
            period_end=scenario.baseline_window_end,
            text=(
                f"This is a SIMULATED, non-destructive what-if scenario ('{scenario.name}', type={scenario.scenario_type.value}, "
                f"status={scenario.status.value}) built from real historical data on sensor '{scenario.sensor_id}' — it is NOT a "
                "real observation, NOT a physical prediction, and NOT a guarantee of future behavior. "
                f"{scenario.description or ''}"
            ),
        )
    ]
    if not runs:
        items.append(
            EvidenceItem(
                category=EvidenceCategory.INTERPRETATION_LIMITATION,
                source_type="scenario",
                source_id=scenario.id,
                label="No scenario run exists yet",
                text="This scenario has not been run yet — there is no simulated result to describe. Do not invent one.",
            )
        )
        return items

    for run in runs:
        if run.status.value != "succeeded" or not run.results:
            items.append(
                EvidenceItem(
                    category=EvidenceCategory.INTERPRETATION_LIMITATION,
                    source_type="scenario_run",
                    source_id=run.id,
                    label=f"Scenario run {run.id[:8]} did not succeed",
                    text=f"This run's status is '{run.status.value}'" + (f": {run.error_message}" if run.error_message else "") + ". No simulated result is available from it.",
                )
            )
            continue
        comparison = (run.results or {}).get("comparison") or {}
        limitations = (run.results or {}).get("limitations") or []
        items.append(
            EvidenceItem(
                category=EvidenceCategory.ANALYTICAL_RESULT,
                source_type="scenario_run",
                source_id=run.id,
                label=f"SIMULATED SCENARIO RESULT — run {run.id[:8]} ({run.run_kind.value})",
                method_version="app.simulation pipeline (robust_zscore + condition evidence + change candidates)",
                text=(
                    f"SIMULATED SCENARIO RESULT for run {run.id[:8]} (scenario version {run.scenario_version}): "
                    f"{comparison if comparison else run.results.get('sensitivity', run.results.get('per_sensor', 'no comparison available'))}. "
                    "Every number above came from running this platform's real anomaly-detection and condition-evidence "
                    "code against a perturbed COPY of real historical data — it describes how this platform's detectors "
                    "would respond to the hypothesized change, not what will actually happen to the equipment. "
                    + " ".join(limitations[:2])
                ),
            )
        )
    return items[:MAX_SCENARIO_RUNS + 1]


# ---------------------------------------------------------------------------
# 6. Method / provenance explanation
# ---------------------------------------------------------------------------


def get_method_explanation(db: Session, *, organization_id: str, evidence_type: str, evidence_id: str) -> list[EvidenceItem]:
    try:
        prov = orchestration_service.get_evidence_provenance(
            db, organization_id=organization_id, evidence_type=evidence_type, evidence_id=evidence_id
        )
    except Exception as exc:
        raise NotFoundError(
            f"No provenance information is available for {evidence_type} '{evidence_id}': {exc}"
        )
    summary = prov.get("summary", {})
    return [
        EvidenceItem(
            category=EvidenceCategory.MODEL_OUTPUT,
            source_type="provenance",
            source_id=evidence_id,
            label=f"Provenance for {evidence_type} {evidence_id[:8]}",
            method_version=summary.get("method"),
            text=str(summary),
        )
    ]


# ---------------------------------------------------------------------------
# 7. Documentation search (plain keyword match, not vector search)
# ---------------------------------------------------------------------------


def search_documentation(query: str) -> list[EvidenceItem]:
    terms = [t.lower() for t in re.findall(r"[a-zA-Z0-9_]+", query) if len(t) > 2]
    if not terms or not _DOCS_DIR.exists():
        return []

    hits: list[tuple[int, str, str, str]] = []  # (score, filename, heading, excerpt)
    for md_file in sorted(_DOCS_DIR.glob("*.md")):
        text = md_file.read_text(errors="ignore")
        sections = re.split(r"\n(?=## )", text)
        for section in sections:
            lower = section.lower()
            score = sum(lower.count(t) for t in terms)
            if score > 0:
                heading_match = re.match(r"#{1,2} (.+)", section)
                heading = heading_match.group(1) if heading_match else md_file.stem
                excerpt = " ".join(section.split())[:500]
                hits.append((score, md_file.name, heading, excerpt))

    hits.sort(key=lambda h: h[0], reverse=True)
    items = [
        EvidenceItem(
            category=EvidenceCategory.INTERPRETATION_LIMITATION,
            source_type="documentation",
            source_id=f"{fname}#{heading}",
            label=f"{fname} — {heading}",
            text=excerpt,
        )
        for _, fname, heading, excerpt in hits[:MAX_DOC_HITS]
    ]
    return items
