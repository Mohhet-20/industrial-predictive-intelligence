# Closed-Loop Intelligence, Outcome Feedback, Continuous Evaluation & Decision Optimization

## Core principle

> PREDICTIONS ARE NOT TRUTH.
> HUMAN FEEDBACK IS NOT AUTOMATICALLY GROUND TRUTH.
> MAINTENANCE EVENTS ARE NOT AUTOMATICALLY FAILURES.
> OUTCOMES MUST HAVE PROVENANCE.

Every table, service function, and API route in this module is built to honor this. This is **not** uncontrolled online learning, **not** automatic retraining, and **not** a system that learns from every click. It is a governed pipeline: MODEL/DETECTOR OUTPUT → HUMAN INVESTIGATION → OPERATIONAL OUTCOME → OUTCOME VERIFICATION → MODEL EVALUATION → FEEDBACK → POTENTIAL FUTURE IMPROVEMENT, with an explicit human decision gate at every arrow.

## §0 — Mandatory audit finding

Currently, this platform has **zero real-world verified outcome data**.

- `app.models.intelligence.MaintenanceEvent` has existed as an explicitly documented stub ("PLANNED — no ingestion workflow exists yet") with zero rows and zero real code references anywhere outside its own definition.
- `app.models.investigation.InvestigationAnnotation` explicitly documents itself as distinct from "a verified outcome (a concept this platform does NOT implement)" previously.
- No CMMS/ERP/work-order integration exists anywhere in this codebase.

Everything is built from that honest starting point: a human can now report an outcome and later verify it, but there is no pre-existing historical outcome data to seed evaluation with, and no evaluation will report a metric until enough of that data is collected (see §13/§14).

## §1–§3 — Outcome model, source, and type

`app.models.outcome.Outcome` is the single new table for a human-reported operational event. `OutcomeType` (`maintenance_event`, `operational_event`, `inspection_result`, `failure_event`, `recovery_event`, `other`) is deliberately not a boolean "failure/no failure" — a maintenance event is not automatically a failure, and the type a human picks is recorded as-is, never reclassified by the platform.

`OutcomeSource` has exactly one member, `user_reported`. This is not a placeholder for a future integration — it is an honest statement of the only source that exists today.

**Two distinct clocks, never conflated:** `outcome_timestamp` is *when the real-world event occurred*, as the reporting human states it — it can be backdated (reporting today that something happened last week). `Outcome.created_at` (from `TimestampMixin`) is *when this report became known to the platform* — a DB-assigned insert time, set once by `app.services.feedback_service.report_outcome`, never backdated and never editable afterward. §10 below checks both independently, because a backdated `outcome_timestamp` sitting safely inside an evaluation period is not, by itself, proof this platform actually knew about the outcome before that evaluation run claims to have computed its result.

## §4–§6 — Verification as a separate state machine

Verification is a distinct, later, explicit human act. Reporting an outcome is never itself verification — every `Outcome` is created `UNVERIFIED`.

States: `UNVERIFIED`, `REVIEWED`, `VERIFIED`, `REJECTED`, `CONFLICTING`.

Enforced transitions (`app.services.feedback_service.VALID_VERIFICATION_TRANSITIONS`):

| From | Allowed to |
|---|---|
| UNVERIFIED | REVIEWED, VERIFIED, REJECTED |
| REVIEWED | VERIFIED, REJECTED, UNVERIFIED |
| VERIFIED | CONFLICTING, REJECTED |
| REJECTED | REVIEWED |
| CONFLICTING | VERIFIED, REJECTED, REVIEWED |

Every transition is recorded as a new, append-only `OutcomeVerification` row (never edited or deleted) carrying the reviewer, a **snapshot** of their role at decision time (a later role change must never rewrite what role actually made a past decision), and an `evidence_note` — required for any transition into `VERIFIED` or `REJECTED`. Only `ADMIN` may verify; `ADMIN`/`ENGINEER` may report outcomes and link/assess signals.

## §7–§9 — Outcome-signal linking, horizons, and signal assessment

`app.models.outcome.OutcomeSignalLink` connects one `Outcome` to one prior signal (`anomaly`, `degradation_signal`, `forecast_run`, or `scenario_run`). At link time:

- The signal's real `prediction_timestamp`, `sensor_id`, and (where a registry link exists) `component_id` are resolved from the real row — never guessed. See `app.services.feedback_service._resolve_signal_prediction_timestamp`.
- `horizon_seconds` is supplied explicitly by the caller (`MIN_HORIZON_SECONDS = 1`, `MAX_HORIZON_SECONDS` = 365 days — named, explicit bounds; never an arbitrary unbounded default) and frozen on the link row forever.
- `within_horizon` is computed **once**, by `app.feedback.matching.resolve_link_temporal_fields`, purely from `prediction_timestamp`, `horizon_seconds`, and the outcome's own `outcome_timestamp` — never re-derived later, never dependent on `linked_at` or "now". An out-of-horizon outcome is flagged, not rejected.
- `signal_assessment` (`not_yet_assessed`, `confirms`, `refutes`, `inconclusive`) is a **separate, explicit human judgment field** — deliberately not inferred automatically from `outcome_type` or `verification_status`. Assessing a link is blocked until its outcome has reached `VERIFIED` or `REJECTED` (§9: no automatic root-cause inference from an unverified report).

## §10 — No backward leakage

`app.feedback.matching.evaluation_run_leakage_check` is called for every link an evaluation run considers, and rejects the whole run if:

1. The outcome's own timestamp is after the evaluation period it's being used to grade (future information), or
2. The outcome's verification happened after this run's own `computed_at` (the verification used did not exist yet at compute time), or
3. **** The outcome's *report* — `Outcome.created_at`, i.e. when it became known to the platform, passed as `outcome_reported_at` — happened after this run's own `computed_at`. This is a distinct leakage shape from (1): a human reporting *today* that an event happened *last week* produces an `outcome_timestamp` safely inside an old evaluation period while the report itself didn't exist in this system until today. Check (1) alone cannot catch that, because it only ever looks at when the real event happened, never at when this platform found out about it. `outcome_reported_at` is an optional parameter (existing callers that only have the first two timestamps are unaffected), but `app.services.feedback_service.run_evaluation` — the only real caller — always supplies it.

Both checks, and `within_horizon` itself, are pure functions of frozen timestamps with no `now`/`linked_at` parameter — enforced structurally (see `tests/test_feedback_matching.py::test_linked_at_is_never_an_input`, which inspects the function signature).

## §11–§14 — Evaluation runs, coverage, and metric gating

`app.models.outcome.OutcomeEvaluationRun` is **additive-only**: a re-evaluation of the same scope (component/sensor/signal_type) is a new row, chained via `previous_evaluation_run_id`, `evaluation_kind` = `initial` (no prior run for this scope) or `updated` (a prior run exists). A past run is never edited.

Metrics (`app.feedback.metrics.compute_metrics`) report **precision only** — never recall or F1. This platform cannot enumerate signals that should have fired but did not, so recall has no honest denominator. Below `MIN_VERIFIED_LINKS_FOR_METRICS = 10` eligible (verified+assessed, true/false-positive-classified) links, the run is still created (so the attempt and its honest "not enough data" reason are preserved), but `metrics.computed = False` with a `reason` string — never a fabricated number.

Coverage (`app.feedback.metrics.coverage_classification`) reports `no_data` / `no_coverage` / `partial_coverage` / `full_coverage` (≥80% of in-scope signals linked+eligible), distinguishing "nothing happened yet" from "a lot happened but we haven't reviewed it" from "we have real coverage."

`/feedback/performance-over-time` returns every run in a scope with its own `metrics.computed` flag intact — it never synthesizes a trend line by treating an uncomputed run as a data point.

## §15 — Error analysis (candidates, never automatic conclusions)

`app.feedback.error_analysis` surfaces three kinds of finding, every one framed as a **candidate for human review**:

- **Late detections** — true positives whose detection delay exceeded `LATE_DETECTION_THRESHOLD_SECONDS` (24h). Late, but still correct.
- **Duplicate candidates** — multiple signals of the same type on the same sensor within `DUPLICATE_WINDOW_SECONDS` (1h) of each other.
- **False-negative candidates** — a verified `FAILURE_EVENT` outcome with no linked signal of any type. Never asserted as a confirmed missed detection — this platform cannot rule out a non-monitored cause.

No automatic root-cause inference happens anywhere in this module.

## §16 — Drift ↔ outcome correlation, without equating shift with degradation

`/feedback/drift-outcome-correlation` presents `ShiftAnalysisRun` rows (the existing drift detection) and `OutcomeEvaluationRun` rows for the same sensor side by side, in time order, with an explicit `note` stating a detected shift is **not** automatically equated with measured performance degradation — a human must independently assess both.

## §17 — Controlled retraining-candidate workflow (never auto-deploys)

`app.models.outcome.RetrainingCandidate` is a thin, evidence-carrying proposal: `trigger_reason`, optional `supporting_evaluation_run_id`, status `proposed` → `approved`/`dismissed` by a human (`review_retraining_candidate`). On `approved`, it calls the **existing, unmodified** `app.services.mlops_service.request_retraining` — which only creates a `REQUESTED` `RetrainingRequest` row. This module never calls `execute_retraining_request` or `activate_version`; actually retraining and activating a resulting version remain separate, later, human-initiated actions through the existing MLOps workflow. Re-reviewing an already-reviewed candidate is a 409 conflict, not a silent overwrite.

## §18 — Explicit label governance

`app.feedback.label_definitions.get_label_definitions()` (served at `/feedback/label-definitions`) is the versioned, human-readable definition of every verification status, signal assessment, and classification rule this platform uses — so "what does VERIFIED mean here" or "what counts as a false positive" is always an answerable, auditable question, not something only readable in code.

## §19 — Feedback dataset builds (frozen, provenance-preserving)

`app.models.outcome.FeedbackDatasetBuild` is a frozen, versioned (`dataset_version`, auto-incrementing per name) snapshot. Inclusion rule, recorded on every build: `Outcome.verification_status IN (verified, rejected) AND OutcomeSignalLink.signal_assessment IN (confirms, refutes)`. **Never** includes an `UNVERIFIED`, `REVIEWED`, or `CONFLICTING` outcome, and never a `not_yet_assessed`/`inconclusive` link. Every row carries full provenance (outcome id, type, verification status, reporter, link id, signal type/id, assessment, horizon fields, assessor).

## §20 — Human feedback loop routing

Nothing in this module writes to any training pipeline directly. A human's feedback flows only into: evaluation runs (read-only aggregation), investigation/error-analysis surfacing (candidates for review), and feedback dataset builds (frozen exports). The only path from feedback to an actual model change is the retraining-candidate workflow above, and that always ends at a `REQUESTED` row a human must separately execute and separately activate.

## §21 — Scenario Lab separation

`OutcomeSignalType.SCENARIO_RUN` links are **architecturally incapable** of counting toward evaluation: `counts_toward_evaluation` is computed server-side as `signal_type != SCENARIO_RUN` and is not a client-settable field. `/feedback/scenario-separation` is an explicit, queryable invariant check (`scenario_links_counting_toward_evaluation` must always be `0`) — covered by `tests/test_api_outcome.py::TestScenarioSeparation` and asserted again in every full-suite run. A simulated result can be compared against a real outcome for a human's own benefit, and the evidence text always states "SIMULATED SCENARIO RESULT."

## §22 — Prioritization policy (opt-in, versioned, never silent)

`app.models.outcome.PrioritizationPolicy` mirrors `ComponentVersion`'s exact pattern: versioned, single-active-per-organization (transactionally enforced in `activate_prioritization_policy`, which retires the previous active policy in the same commit), explicit activation. With **no active policy** — the default — `app.services.orchestration_service.get_prioritized_signals`'s existing formula is completely unchanged. Nothing about this module silently alters ranking; a policy only ever takes effect once a human explicitly activates it.

## §23 — Closed-loop analytics (no meaningless single KPI)

`/feedback/analytics` returns a bundle of process counts (outcomes by verification status, links by assessment, evaluation run count, retraining candidates by status, dataset build count, active policy id) with an explicit note that this is **not** a single accuracy score. A trustworthy accuracy number requires per-component/per-sensor evaluation runs, each gated on its own minimum sample size (§13/§14).

## §24 — API surface

Two routers: `/outcomes` (report/verify/link/assess) and `/feedback` (evaluation, error analysis, retraining candidates, dataset builds, prioritization policy, label definitions, analytics, scenario separation). Verifying is `ADMIN`-only; reporting/linking/assessing is `ADMIN`/`ENGINEER`; reading is any authenticated org member. Every route is organization-scoped through `app.core.deps.organization_scope`. No route ever edits an `Anomaly`, `DegradationSignal`, `ForecastRun`, or `ScenarioRun`'s original detection fields — see `tests/test_api_outcome.py::TestOriginalEvidenceImmutability`.

## §25 — Frontend: Outcome & Learning Center

`frontend/src/pages/OutcomeLearningCenterPage.tsx` — six tabs (Outcomes, Evaluation & Error Analysis, Retraining Candidates, Feedback Datasets, Prioritization Policy, Analytics & Governance). Deliberately **no single accuracy card** anywhere on this page — every metric is shown with its `computed`/coverage context, and every outcome/link shows its full verification lineage.

## §26 — Copilot integration

`CopilotContextType.OUTCOME` plus two new evidence tools (`get_outcome_evidence`, `get_evaluation_evidence`) and an addition to `get_anomaly_evidence` (`_outcome_link_evidence_for_signal`) so an anomaly answer also shows whatever outcome a human has linked to it — always labeled with its current verification status, and explicitly caveated as unverified when it is. A scenario-run-linked outcome's evidence text always states it is a simulated result. See `tests/test_copilot_outcome_evidence.py`.

## What is explicitly out of scope / foundation only

- **Automatic retraining or automatic model activation** — never implemented here, and would require independent justification and safety work beyond the current scope.
- **Recall/F1** — not computable honestly with this platform's data (no enumeration of un-signaled faults), so never reported.
- **Root-cause inference** — every error-analysis finding is a candidate for human review, never an automatic conclusion.
- **A CMMS/ERP/work-order integration** — `OutcomeSource` has exactly one member today; a real integration is future work, not simulated here.

## What is ready

The full lifecycle — INTELLIGENCE OUTPUT → HUMAN INVESTIGATION → OUTCOME → VERIFICATION → EVALUATION → FUTURE LEARNING CANDIDATE — is preserved end-to-end, with leakage tests, scenario separation, and original-evidence immutability all covered by the test suite (`tests/test_feedback_matching.py`, `test_feedback_metrics.py`, `test_feedback_error_analysis.py`, `test_api_outcome.py`, `test_copilot_outcome_evidence.py`). A later work building an actual training pipeline on top of `FeedbackDatasetBuild` rows, or a governed auto-retraining trigger on top of `RetrainingCandidate`, has a correctly-separated, provenance-preserving foundation to build from — neither of those is implemented here.
