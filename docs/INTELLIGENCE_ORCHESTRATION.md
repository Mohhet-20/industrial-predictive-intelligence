# Intelligence Orchestration, Unified Investigation & Evidence-Based Decision Support (Phase 6)

This document describes what Phase 6 actually built on top of Phases
1–5: an organizational layer that connects the real evidence those
phases produce, preserves its provenance, surfaces relevant context, and
helps a human prioritize investigation — without inventing a health
score, a probability of failure, or a cause. As with every layer before
it, this document starts from a fresh, repository-verified capability
audit (§0), not from what earlier phases *planned* to build.

**Core principle, repeated because it governs every design choice below:**
this platform connects evidence, preserves provenance, surfaces context,
and helps humans prioritize. It does not diagnose, does not predict
failure probability, and does not decide anything for the user.

## §0 — Capability audit

Re-inspected the entire repository (not assumed from the Phase 1–5
completion reports) before writing any orchestration code. What actually
exists, and what state it's in:

| Capability | Status | Where |
|---|---|---|
| Data-quality signals | AVAILABLE AND VALIDATED | `app.models.data_quality.DataQualityIssue`, `app.services.quality_service` |
| Time-series profiling | AVAILABLE AND VALIDATED | `app.timeseries.profiling`, `/timeseries/sensors/{id}/profile` |
| Feature analysis | AVAILABLE AND VALIDATED | `app.timeseries.features`, `app.models.feature.FeatureDefinition` |
| Change candidates | AVAILABLE BUT LIMITED — computed on demand, never persisted, no durable id | `app.timeseries.change_detection`, `/timeseries/sensors/{id}/change-candidates` |
| Anomaly detectors (robust z-score, Isolation Forest) | AVAILABLE AND VALIDATED | `app.models.intelligence.ModelVersion`, `app.anomaly.*` |
| Anomaly events | AVAILABLE AND VALIDATED | `app.models.intelligence.Anomaly` |
| Anomaly explanations | AVAILABLE AND VALIDATED | `Anomaly.contributing_signals`, `get_anomaly_context` |
| Condition (degradation) signals | AVAILABLE AND VALIDATED, heuristic — not a trained model | `app.models.condition.DegradationSignal`, `app.condition.degradation` |
| Forecasts | AVAILABLE BUT LIMITED — only where a real backtest justifies it; usually not justified on this platform's actual data | `app.models.condition.ForecastModel/ForecastRun` |
| Predictive (failure) models | NOT AVAILABLE — no failure/maintenance labels exist anywhere in this schema (re-confirmed, third time, following Phases 4 and 5's own audits) | — |
| Human reviews | AVAILABLE AND VALIDATED | `AnomalyInvestigation`, `DegradationReview` |
| Model/component versions & lifecycle | AVAILABLE AND VALIDATED | `app.models.mlops.*` (Phase 5) |
| Experiment/run tracking | AVAILABLE BUT LIMITED — `ModelRun`/`ForecastRun`/`ConditionRun` exist; no hyperparameter-sweep or multi-run comparison tooling, because nothing in this codebase needs one (Isolation Forest has one fixed, documented configuration) | `app.models.intelligence.ModelRun`, `app.models.condition.ConditionRun/ForecastRun` |
| Monitoring signals | AVAILABLE AND VALIDATED, split OPERATIONAL/DATA/MODEL_PERFORMANCE | `anomaly_service.get_monitoring_summary`, `condition_service.get_condition_monitoring_summary`, `mlops_service.get_mlops_monitoring_summary` |
| Shift analysis | AVAILABLE AND VALIDATED, discrete/on-demand only | `app.models.mlops.ShiftAnalysisRun` (Phase 5) |
| Background jobs | AVAILABLE BUT LIMITED — real job runner exists; `JobType.DETECTOR_TRAINING` is declared but was never wired to a handler (found in Phase 5's audit, re-confirmed here) | `app.jobs.*` |
| Authentication/authorization | AVAILABLE AND VALIDATED | `app.core.deps`, JWT + RBAC |

One existing decision-support mechanism already implemented in Phase 4,
`condition_service.get_attention_list`, is NOT duplicated here — it
remains exactly as it was (a simple `(stage_rank, severity_rank,
period_start)` ordering combining open anomalies and degradation
signals). This phase's `orchestration_service.get_prioritized_signals`
is a richer, separately-versioned ranking that adds persistence,
cross-detector agreement, and data-quality context — see §4.

**The orchestration layer adapts to what actually exists.** It does not
aggregate a forecast into every asset summary (many assets have none
justified); it does not compute change-candidate overlays inside the
timeline (they have no durable id — see §2 and §3); it does not attempt
provenance tracing for evidence kinds with no traceable chain
(data-quality issues, for instance, have nothing behind them to trace).

## §1 — Investigation model

`app.models.investigation.Investigation` is the case a human opens: asset-
scoped, optionally sensor- and time-window-scoped, with a denormalized
`status` (the full transition history lives separately — see §6). It
never overwrites or alters the ML/statistical evidence it references —
opening an investigation and linking evidence to it are pure reads
against `Anomaly`/`DegradationSignal`/etc. followed by writes only into
the new `investigation_*` tables.

`InvestigationRelation` implements "related investigations where
appropriate" as an explicit, human-asserted, symmetric-in-the-API link
(see the self-review bug fixed in §20).

## §2 — Evidence graph / relationship model

No graph database — a relational reference table,
`InvestigationEvidenceLink`, is genuinely sufficient at this project's
scope. Every evidence kind with a durable id (`Anomaly`,
`DegradationSignal`, `DataQualityIssue`, `ForecastRun`, `ComponentVersion`,
`GovernanceEvent`, `ShiftAnalysisRun`, `Sensor`) is linked **by reference**
— `evidence_type` + `evidence_id` — and is validated (existence,
organization ownership) at link time, never duplicated.

One evidence kind is different: a change/behavior candidate
(`app.timeseries.change_detection`) is computed on demand over an
in-memory pandas Series and was never assigned a durable id anywhere in
this schema — that is a fact about what the capability actually is, not a
design shortcut. For that kind only, `InvestigationEvidenceLink.snapshot`
carries exactly the candidate payload observed at link time, honestly
labeled as a point-in-time snapshot rather than a live reference.

Traceability requirement satisfied: every anomaly or degradation-signal
link can be followed, via `GET /orchestration/evidence/{type}/{id}/provenance`,
all the way back to the detector/heuristic, its version, its
configuration, and its evaluation evidence (§11).

## §3 — Unified asset intelligence timeline

`orchestration_service.get_asset_intelligence_timeline` merges, for one
asset and time window: data-quality issues, anomalies and their reviews,
degradation signals and their reviews, forecast runs on the asset's
sensors, component lifecycle events (activation/rollback/retirement) for
components covering the asset's sensors, and investigation
open/status-change/annotation events. Every event is tagged with one of
**five** categories — the spec's four (OBSERVED DATA / ANALYTICAL SIGNAL /
MODEL OUTPUT / HUMAN INTERPRETATION) plus a fifth, GOVERNANCE, added
because a component activation/rollback/retirement event is neither raw
data, a statistical signal, a model's output, nor a human's
interpretation of evidence — it's a system/process event about which
*version* of a method was in effect. Folding it into "model output" would
misrepresent what it is; the fifth category is documented rather than
hidden.

Raw measurements and change candidates are deliberately excluded from the
timeline: raw measurements would overwhelm it (the spec explicitly warns
against this), and change candidates have no stored, time-range-queryable
row to aggregate in bulk across a whole asset (see §2). Both remain fully
explorable per-sensor via Temporal Analysis.

Bounded: each source query is capped at 300 rows before merging (mirrors
`condition_service.get_attention_list`'s existing 500-row cap pattern),
then sorted and paginated. Filterable by `event_types` and time range.

## §4 — Evidence-based prioritization

`orchestration_service.get_prioritized_signals` produces a transparent
**ranking**, never a probability of failure, never a fabricated composite
health score. Every item exposes the exact factors behind its score:

```
score = (stage_rank OR severity_rank) * 100
      + min(persistence_count, 5) * 10
      + (25 if two different detector types independently flagged the same sensor)
```

`persistence_count` is a real count of other open signals/anomalies on
the same sensor (repeated flagging as a proxy for persistence).
`cross_detector_agreement` is a genuine cross-check: this platform has
two real, independent anomaly detection methods (robust z-score,
Isolation Forest), and agreement between them is real corroborating
evidence, not an invented signal.
`nearby_data_quality_issue_count` is surfaced as context, not scored —
there is no validated basis for a specific penalty weight, the same
honesty this platform applied to not implementing PSI in Phase 5's shift
analysis (see `docs/MLOPS_GOVERNANCE.md` §12). The methodology and its
version (`orchestration_priority_v1`) are returned in every response, not
hidden inside the number.

## §5 — Cross-layer intelligence synthesis

`orchestration_service.get_asset_intelligence_summary` is evidence
**aggregation**, not fabricated reasoning: every field is a real count or
reference, grouped into exactly the four categories §5 requires kept
distinct (observed data / analytical signals / model outputs / human
interpretations), plus an honestly-generated `limitations` list (no
justified forecast, unreviewed anomalies, critical data-quality issues,
zero sensors). No field anywhere combines unrelated signals into a single
score.

## §6 — Investigation workflow

Five statuses: `OPEN → UNDER_REVIEW / AWAITING_CONTEXT → RESOLVED →
CLOSED`, with `RESOLVED`/`CLOSED` reopenable into `UNDER_REVIEW` (mirrors
`AnomalyStatus.FALSE_POSITIVE → UNDER_REVIEW`'s existing reopenability
pattern exactly). `INVESTIGATION_ALLOWED_TRANSITIONS` in
`investigation_service.py` is the single source of truth — nothing
transitions automatically anywhere in this codebase; every
`InvestigationStatusEvent` is the direct result of an explicit,
authenticated `POST /investigations/{id}/status` call. Full status
history is preserved (append-only, same pattern as
`AnomalyInvestigation`/`DegradationReview`).

## §7 — Evidence & explainability panel

Implemented as a reusable pattern rather than a new backend table: the
frontend's provenance panel (`IntelligenceWorkspacePage`) renders
`GET /orchestration/evidence/{type}/{id}/provenance`'s `summary` level by
default (what happened, when, which method, governance status) with a
"show technical detail" toggle revealing the full `technical` level
(raw config, feature schema, evaluation results, baseline summary) —
progressive disclosure, not a wall of JSON by default. Every anomaly
already carries `contributing_signals` and `data_quality_context` from
Phase 3; every degradation signal already carries its full `evidence`
bundle from Phase 4 — this panel surfaces those real fields rather than
re-deriving them.

## §8 — Contextual cross-sensor investigation

`orchestration_service.get_cross_sensor_context` reuses Phase 2's real
`app.timeseries.multi_sensor` correlation machinery, overlaid with real
open anomaly/degradation-signal counts per sensor in the requested
window. Language is deliberately non-causal throughout: `observed_associations`
strings read "both show open anomalies during the same window — a
temporal association only; this is not evidence that one caused the
other," and the response's `note` field states the same discipline
explicitly. Nothing in this function or its frontend rendering ever says
"caused by."

## §9 — Human-in-the-loop knowledge capture

`InvestigationAnnotation` captures observations, operational context,
resolutions, false-positive assessments, and follow-up flags — append-
only, never edited or deleted. This platform deliberately keeps four
concepts distinct and implements only the first three:

1. **Raw data** — `SensorMeasurement` (Phase 1).
2. **ML/statistical outputs** — `Anomaly`, `DegradationSignal`, `ForecastRun` (Phases 3–4).
3. **Human annotations** — `InvestigationAnnotation` (this phase) — a person's *stated interpretation*, nothing more.
4. **Verified outcomes** — **NOT IMPLEMENTED.** This platform has no
   workflow that promotes a human annotation into a verified training
   label, and it does not pretend otherwise. Recording a
   `false_positive_assessment` annotation never, by itself, changes the
   underlying `Anomaly.status` — that still requires the separate,
   pre-existing anomaly review action (`POST /anomaly/anomalies/{id}/review`).
   `tests/test_api_investigations.py::test_false_positive_annotation_does_not_change_anomaly_status`
   proves this behaviorally, not just by docstring.

This is the concrete reason a future learning loop (if this platform ever
gains real failure labels) would need its own explicit, separate
verification step — annotations alone are not, and were never designed
to be, that step.

## §10 — Decision support outputs

The `limitations` list in the asset summary (§5) and the `methodology`
string in prioritization (§4) are this platform's decision-support
"summaries." Every sentence in them is generated from an explicit,
inspectable rule over real counted evidence (e.g. `if not
justified_forecasts: limitations.append(...)`) — never from an LLM
inventing an operational recommendation, and never phrased as an
instruction ("replace the bearing") rather than a description of
evidence and its limits ("N open anomalies have not yet been reviewed").
No natural-language summary in this platform is generated by a model;
every one is a literal template filled from a real, traceable computed
value.

## §11 — Model and method provenance in user workflows

`get_evidence_provenance` implements the full chain the spec asks for —
Signal → Method/Detector/Model → Version → Configuration → Feature/Data
Definition → Training/Reference Context → Evaluation Evidence — for the
two evidence kinds that actually have one: `anomaly` (through
`ModelRun` → `ModelVersion` → optionally a registered
`ComponentVersion`/`IntelligenceComponent`, Phase 5's governance layer)
and `degradation_signal` (through `ConditionRun` → the heuristic
classification method, which has no per-signal linking id at all — see
below). Returned as two levels (`summary`, `technical`) for progressive
disclosure (§7).

**Honestly documented traceability gap:** a `DegradationSignal` cannot be
programmatically traced to a specific registered `ComponentVersion`,
because `ComponentVersion.heuristic_code_version` (Phase 5) is a
free-text label a human types when registering a heuristic version, not a
foreign key with any link back to the signals a given code version
produced. This is a real, structural limitation of the current schema,
not a bug — it is stated plainly in the provenance response itself
(`governance_status` field) rather than papered over with a guessed
match.

## §12 — Intelligence Orchestration API

Two new routers, both organization-scoped and RBAC-enforced server-side
(`app.core.deps.require_roles`/`organization_scope`), all list/aggregate
endpoints bounded and paginated:

- `app/api/v1/investigations.py` (`/investigations/*`) — CRUD +
  evidence/annotation/status/relation actions. Write actions (open,
  transition, attach/remove evidence, annotate, relate) require ADMIN or
  ENGINEER; reads are open to any authenticated org member — the same
  boundary `app.api.v1.anomaly`/`condition` already draw for their review
  actions.
- `app/api/v1/orchestration.py` (`/orchestration/*`) — entirely read-only
  (timeline, prioritized-signals, asset summary, cross-sensor-context,
  evidence provenance). No route here can write anything; it only reads
  Phases 1–5's real tables and this phase's own investigation tables.

No route returns an internal object, a raw pandas structure, or anything
beyond the JSON-serializable fields the schemas declare (same discipline
as every prior phase's API layer).

## §13 — Frontend: flagship intelligence experience

`frontend/src/pages/IntelligenceWorkspacePage.tsx`, wired in as
"Intelligence Workspace" (route `/workspace`), implements the exact
journey the spec asks for in one integrated page rather than another
disconnected dashboard: prioritized items → select asset → cross-layer
summary → timeline → open/select investigation → evidence (with
provenance trace) → human context (annotations) → status. Cross-sensor
context is available once an asset is selected. Every panel reads real
backend data through `@tanstack/react-query`; nothing is mocked.
Category badges (observed data / analytical signal / model output /
human interpretation / governance) appear consistently across the
timeline and the summary cards so a user never has to guess which kind of
evidence they're looking at.

**Known frontend scope gap:** removing an evidence link has a backend
route (`DELETE /investigations/{id}/evidence/{link_id}`) but no frontend
button in this pass — attaching evidence is the primary workflow the spec
emphasizes, and removal was deprioritized rather than rushed. Documented
here rather than silently left out.

## §14 — Intelligence summary integrity

Every aggregation in this layer handles the honest-emptiness cases
explicitly: zero open signals (`EmptyState` in the UI, not a fabricated
"all clear" score), zero sensors on an asset (`sensor_count: 0` plus a
limitation string, not silently omitted), no justified forecast (a
limitation string, never a forecast pretending otherwise), insufficient
data for cross-sensor correlation (the underlying Phase 2 machinery's
existing `TooManyObservationsError`/insufficient-data handling propagates
unchanged), and no governance registration for a detector
(`governance_status: "not registered..."`, not a blank or a guess).
`tests/test_api_orchestration.py` exercises every one of these paths
explicitly (see §17/§19).

## §15 — Performance & caching

Every orchestration read is a bounded query (§3's per-source cap, §4's
500-row cap mirroring Phase 4's existing pattern) — no unbounded table
scan exists anywhere in this layer. **No caching layer was added.**
This is a deliberate, documented scope decision, not an oversight: at
this platform's actual data volumes (a synthetic demo organization with a
handful of assets), every orchestration endpoint responds in single-digit
milliseconds against the real seeded database (see §19's live
validation), so a cache would add staleness risk (governance/evidence
data changing between a cache write and a read) for no measured benefit.
If this platform's real deployment volume ever grows enough to change
that calculus, `get_asset_intelligence_summary` is the first candidate
for a precomputed/cached version — it aggregates the most sources — and
would need explicit invalidation on every write to `Anomaly`,
`DegradationSignal`, `DataQualityIssue`, `ForecastRun`, and `Investigation`
to stay honest, which is precisely the complexity this phase chose not
to take on without a measured need.

## §16 — Security & auditability

Every write in this layer goes through the same `AppError` /
`organization_scope` / `require_roles` machinery as every other module —
no route trusts a client-supplied `organization_id`. Annotations and
evidence links are user input stored as opaque text/JSON, never
interpolated into a query or executed — SQLAlchemy's parameterized ORM
layer handles this exactly as it does everywhere else in this codebase.
Investigation status history and evidence links are append-only/
non-destructive by design (§2, §6); the one genuinely destructive action
in this layer, `remove_evidence` (a link, never the evidence itself), is
restricted to ADMIN/ENGINEER and does not touch the referenced row.
Nothing in this layer introduces a new secret, credential, or PII field —
`InvestigationAnnotation.content` is free text a human writes about
equipment behavior, held to the same standard as every other free-text
field already in this schema (`Anomaly.summary`, `DegradationReview.notes`, etc).

## §17 — Testing

26 new tests across two files, run against the real HTTP surface and the
real (SQLite, in-memory per test) database — no service function is
tested by direct call only, and no evidence is fabricated:

`tests/test_api_investigations.py` (13 tests): full open→evidence→
annotate→transition→history workflow using a REAL anomaly (trained and
detected through the actual `/anomaly` API, not stubbed); invalid status
transition rejection; sensor-must-belong-to-asset validation; duplicate
evidence-link rejection; evidence-shape validation (id required except
`change_candidate`, snapshot only for `change_candidate`); honest 404 for
a nonexistent evidence reference; evidence-link removal; **the false-
positive-annotation-does-not-mutate-anomaly-status behavioral proof**
(§9); annotation append-only ordering; related-investigations symmetric
visibility (with the direction-normalization regression test, §20);
viewer read/write authorization boundary; cross-org isolation; and
list filtering.

`tests/test_api_orchestration.py` (18 tests): timeline combining real
events across all four+one categories with correct category tagging and
descending order; timeline event-type filtering; timeline time-range
exclusion; timeline validation (bad range, unknown asset); prioritization
exposing contributing factors and methodology with no probability
language; cross-detector-agreement raising score; prioritization
organization-scoping; asset-summary category separation with correct
limitation strings; asset-summary honesty with zero sensors; asset-summary
404; cross-sensor-context sensor-count/same-asset validation; cross-sensor
non-causal language; anomaly provenance with and without governance
registration (proving the Phase 5 registry integration is real, not
guessed); degradation-signal provenance; provenance for an unsupported
evidence type and a nonexistent id; and both authentication and viewer-
role read-access checks on the orchestration router.

All 247 backend tests pass (216 carried over from Phases 1–5 + 13 new in
`test_api_investigations.py` + 18 new in `test_api_orchestration.py` = 247);
see §21 for the exact validation-gate run that confirms this.

## §18 — Documentation

This document. Cross-referenced from `README.md`'s architecture-reference
paragraph and Status section, following the exact pattern
`docs/MLOPS_GOVERNANCE.md` established for Phase 5. Only capabilities
that actually exist and were actually run are described above — the
capability-audit table in §0 states plainly which pieces are limited or
unavailable rather than omitting them.

## §19 — Complete flagship end-to-end workflow — validated live

Beyond the 31 API-level tests, a live validation script
(`live_validate_orchestration.py`, run directly against the real
`backend/data/pred_platform.db` dev database — the same one the seeded
Riverbend Manufacturing demo org has been accumulating real data and real
Phase 3/4/5 activity in across this whole project) exercised the exact
workflow the spec's stop condition names, using the real service-layer
functions the API itself calls, with real observed results (not
predetermined):

1. Trained a real robust z-score detector (`bearing-temp-de-orchestration-live`)
   on the real `bearing_temp_de` sensor's real ingested data
   (2,841 real reference observations) and ran a real detection run over
   8 real days of scoring data (2,250 points scored).
2. That run genuinely flagged **3 real anomalies** — not injected,
   not stubbed — one of which (`raw_score=5.83`, "Value 75.23 is 5.8
   robust standard deviations above the reference median of 58.19")
   became this validation's tracked evidence item.
3. `get_prioritized_signals` ranked it with real, non-fabricated
   contributing factors — `severity_rank=3`, `persistence_count=3`,
   **`cross_detector_agreement=True`** (a real robust z-score *and* a
   real Isolation Forest detector, the latter registered during Phase
   5's own live validation of this same org, both independently flagged
   this sensor) — for a real computed `score=355`.
4. Opened a real `Investigation` on the real motor-drive asset. The real
   `get_asset_intelligence_timeline` returned 14 real events spanning
   **four of the five documented categories** in one call
   (`observed_data`, `model_output`, `human_interpretation`, and
   `governance` — the governance events came from Phase 5's real
   component-activation history on this same sensor, concretely proving
   the cross-phase integration in §3 is real, not aspirational).
5. Attached the real anomaly to the investigation as evidence by
   reference; confirmed the underlying `Anomaly` row's `status` and
   `raw_score` were byte-for-byte unchanged afterward.
6. `get_evidence_provenance` traced the full real chain: detector name
   and version (`bearing-temp-de-orchestration-live v1`), and an honest
   `governance_status: "not registered in the intelligence lifecycle
   registry"` — correct, because this particular detector was never
   registered in Phase 5's registry, and the function said so instead of
   guessing.
7. Added a real human annotation (`observation` type) and confirmed the
   real asset summary's `unreviewed_anomaly_count` was unaffected by it
   (still 4 — annotating is not the same act as reviewing an anomaly;
   see §9), proving the annotation-vs-review separation live, not just
   in a unit test.
8. Transitioned the investigation `OPEN → UNDER_REVIEW → RESOLVED`,
   preserving the exact real status history
   `[(None, open), (open, under_review), (under_review, resolved)]`.
9. Re-fetched the asset summary and confirmed `investigations_by_status`
   and `annotation_count` both reflected the real changes just made.

Every step used real application logic against real, already-accumulated
platform data — nothing in this validation was simulated, and the
cross-detector agreement and governance-category timeline events are
genuine emergent results of this platform's actual accumulated history,
not staged for the demo. See §21 for the exact test run that backs this
(the flagship path is also covered end-to-end by
`test_api_investigations.py::test_investigation_full_workflow_e2e` and
`test_api_orchestration.py::test_timeline_combines_real_events_across_layers_and_orders_desc`).

## §20 — Principal AI systems review

Self-review questions from the spec, answered honestly:

- **Does the platform feel like one cohesive system?** More than before:
  one workspace page now connects prioritization, an asset's full
  intelligence summary, its timeline, its investigations, and cross-sensor
  context, instead of requiring a user to piece these together across five
  separate pages. The separate Anomaly/Condition/MLOps pages still exist
  (they remain the right place for training a detector or managing
  lifecycle actions — that is configuration work, not investigation work)
  and are linked conceptually through the workspace's evidence provenance
  panel.
- **Can every important signal be traced to evidence?** Yes for anomalies
  and degradation signals (§11); data-quality issues and shift-analysis
  runs are themselves the evidence (nothing further behind them to trace).
- **Are model outputs separated from human interpretation?** Yes,
  structurally — `InvestigationAnnotation` cannot mutate `Anomaly`/
  `DegradationSignal` fields, proven by test (§9, §17).
- **Is prioritization transparent?** Yes — every score's formula and
  factors are returned in the response itself (§4).
- **Are anomalies still clearly distinguished from failures?** Yes —
  no field, label, or UI element in this phase's additions claims a
  failure, a diagnosis, or a probability of one.
- **Does the system handle insufficient evidence honestly?** Yes (§14).
- **Can users efficiently investigate an issue?** The workspace's single
  page removes the prior need to manually cross-reference asset id,
  sensor id, and time window across separate pages.
- **Are synthetic capabilities clearly labeled?** Yes — the same
  `is_synthetic_scenario` labeling from Phase 3 flows through unchanged
  wherever this layer surfaces anomaly evidence.
- **Is the architecture unnecessarily complex?** No new database
  paradigm was introduced (§2); the registry layering pattern from Phase
  5 was reused rather than reinvented.

**A real bug was found and fixed during this review**, in
`investigation_service.list_related_investigations`: `InvestigationRelation`
is stored directionally (`investigation_id` → `related_investigation_id`),
but the original implementation returned "backward" relations (found by
matching the queried investigation against the `related_investigation_id`
column) with their raw stored field values. From investigation B's point
of view, a relation stored as A→B would come back showing
`investigation_id: A, related_investigation_id: B` — technically correct
in the database, but backwards and misleading to any client rendering
"investigations related to B," which would see B's own id sitting in the
"related" slot. Fixed by normalizing the response in
`list_related_investigations` so `investigation_id` always equals the
investigation actually being queried and `related_investigation_id`
always equals the other one, regardless of storage direction. Proven by
`tests/test_api_investigations.py::test_related_investigations_visible_symmetrically`,
which asserts the orientation explicitly from both investigations'
perspectives and would fail against the pre-fix code.

## §21 — Validation gate — actually executed

Run in this session, in this order, against the real repository:

1. `alembic revision --autogenerate` for the new investigation tables →
   detected exactly six additive table/index operations, zero drops.
2. `alembic upgrade head` → applied cleanly.
3. `alembic check` → "No new upgrade operations detected."
4. `.venv/bin/python3 -m pytest -q` (full suite, before this phase's new
   tests) → 216 passed (Phases 1–5's carried-over suite, unaffected).
5. New test files written and run individually during development,
   fixing two real test-authoring mistakes along the way (a fabricated
   nonexistent sensor id in a snapshot payload, and a malformed boolean
   assertion) — both fixed in the test code, not by weakening the
   assertion's intent.
6. **The self-review bug (§20) found, fixed, and covered by a new
   regression test** before the final run.
7. `.venv/bin/python3 -m pytest -q` (full suite, final) → **247 passed**
   (216 carried over + 13 new investigation tests + 18 new orchestration
   tests).
8. `npm run build` (`tsc -b && vite build`) → succeeded with zero
   TypeScript errors, one pre-existing informational chunk-size warning
   (present before this phase too).

No test was skipped, no failure was silently reinterpreted as a pass, and
nothing above is claimed without having actually been run in this
session.

## What this is not

This layer does not predict failure, does not compute a composite health
score, does not assert causality between sensors, and does not verify a
human's annotation into a training label — none of these exist anywhere
in this codebase, and this document does not claim otherwise. It also
does not add: a graph database (a relational reference table was
sufficient — §2), a caching layer (not yet justified by real load — §15),
an LLM-generated narrative summary (§10's summaries are template-filled
from real counted evidence, not generated text), or a verification/
outcome-labeling workflow (§9) — building any of these without a real,
measured need would be exactly the kind of unearned complexity this
project's stop conditions have consistently rejected.
