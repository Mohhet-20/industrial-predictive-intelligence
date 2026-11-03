# Intelligence Lifecycle, Registry & Governance (MLOps) — What This Layer Does and Doesn't Do

Phase 5 builds the layer that manages every intelligence capability Phases
1-4 actually built: a stable registry, an explicit lifecycle (train →
evaluate → validate → approve → activate → retire), append-only governance
history, controlled retraining, data/feature shift analysis, and a safe
scoring contract with full provenance. It does **not** re-implement
detection, condition analysis, or forecasting — every real computation
still lives in `app.anomaly.*` and `app.condition.*`, exactly where Phases
3-4 put it. This document explains what was actually found in the
repository, what was built on top of it, and — as with every prior
phase's documentation — where the honest limits are.

## 0. The capability audit that determined what could be governed

Before writing any registry code, the entire repository was re-audited
(not from memory) to catalogue every real intelligence capability. The
platform's own principle — "manage what actually exists" — meant this
table had to come first and the architecture had to fit it, not the other
way around.

| Component | Type | Trainable? | Current versioning (pre-Phase 5) | Input data | Feature dependencies | Output type | Evaluation status | Validation evidence | Deployment/runtime status |
|---|---|---|---|---|---|---|---|---|---|
| Robust z-score detector (`app.anomaly.robust_zscore`) | Statistical detector | Non-trainable — closed-form median/MAD, no iterative fitting | `ModelVersion`, per-(org, name) auto-increment, immutable | One sensor's raw measurement series | None (raw values only) | Per-point anomaly candidates (raw/normalized score, severity tier) | Optional: `POST /anomaly/detectors/{id}/evaluate` against synthetic scenarios, `is_synthetic=true` | None — any `TRAINED` version was directly scorable | Synchronous; no "current/active" concept — every `model_version_id` usable forever |
| Isolation Forest detector (`app.anomaly.isolation_forest_detector`) | **Trained ML model** — a real `sklearn.IsolationForest` fit in this codebase | Trainable — refit deterministically each run (`random_state=42`) from immutable stored config; never pickled/persisted | `ModelVersion`, same pattern | 1+ sensor series | `app.timeseries.dataset_builder` feature specs (mean/std/rate_of_change, window_seconds) | Per-grid-point candidates with `contributing_features` | Same synthetic harness | None | Sync or async (`JobType.ANOMALY_DETECTION_RUN`) depending on `MAX_DETECTION_ROWS_SYNC` |
| Mahalanobis (robust covariance) detector (`app.anomaly.mahalanobis_detector`) — **added Phase 14** | **Trained ML model** — a real `sklearn.covariance.MinCovDet` fit in this codebase | Trainable — refit deterministically each run (`random_state=42`) from immutable stored reference-period data; never pickled/persisted | `ModelVersion`, same pattern, same `TRAINED_ML_MODEL` component type as Isolation Forest | 2-6 sensor series aligned onto one grid (`align_sensors_on_grid`) | None (raw aligned values; no feature engineering step) | Merged per-event candidates with per-sensor `contributing_sensors` and an `individually_unremarkable` flag | Same synthetic harness, via a dedicated multivariate scenario generator (`app.anomaly.multivariate_synthetic_scenarios`) — single-sensor scenarios cannot exercise a relationship-breakdown detector | None | Sync or async, identical split to Isolation Forest |
| Linear-trend forecaster (`app.condition.forecasting`) | Forecasting model | Trainable in a closed-form sense (OLS fit, not iterative) | `ForecastModel`, same per-(org, name) pattern | One sensor series, fit/test windows | None (raw values + time index) | Forecast points with uncertainty bounds | **Always** backtested inline against `naive_last_value`/`seasonal_naive`; `JUSTIFIED` only if it beats both by ≥10% MAE | The backtest itself, but no human sign-off existed | Synchronous; gated — `POST .../runs` refuses unless `status=justified` |
| Degradation classification (`app.condition.degradation`) | Heuristic signal method | Non-trainable — fixed constants (`SUB_WINDOW_COUNT=3`, `CORROBORATION_MAD_THRESHOLD=2.0`) | None — pure code, no version row at all | One sensor series split into 3 sub-windows | Reuses `app.timeseries.profiling`'s OLS trend | Stage classification; `validated_degradation` only via explicit human action | Only `tests/test_condition_core.py` (18 tests) — no formal evaluation harness | Human validation exists per-signal (`validate_degradation_signal`), never per-method-version | Always active; no method-level versioning |
| Change-candidate detection (`app.timeseries.change_detection`) | Heuristic signal method | Non-trainable | None | One sensor series | None | Variance/mean-shift candidates | `tests/test_change_detection.py` unit tests only | None | Invoked inline by Temporal Analysis and Condition Evidence; no lifecycle concept |
| Condition evidence bundle (`app.condition.evidence`) | Orchestration/assembler, not independently trainable | N/A | N/A | Composite of the above | N/A | Five independent evidence types, `composite_score: null` | N/A | N/A | Not independently registrable — it composes other components, so it is out of scope for individual registry entries |
| `FeatureDefinition` (`app.models.feature`) | Config artifact, not a scoring component | Non-trainable | Already immutable, versioned per (org, name) — predates Phase 5 and is **not duplicated** by the new registry | N/A | N/A | Feature computation spec | N/A | N/A | Feeds Isolation Forest / dataset builder |
| `DatasetBuild` (`app.timeseries.dataset_builder`) | Dataset construction record | N/A | Append-only, one row per build request; no "version number" | Sensor series + feature specs + time grid | N/A | Reproducible config snapshot (matrix recomputed on demand, never persisted) | N/A | N/A | Used by feature engineering; no supervised training consumes it yet |
| Supervised fault classifier / RUL estimator | — | — | — | — | — | — | — | — | **Not implemented.** `MaintenanceEvent` (planned in Phase 1, re-confirmed in Phase 4) still has zero rows and no ingestion workflow in every environment this schema has run in, including the real seeded demo database checked again for this phase. No failure or maintenance-outcome labels exist anywhere in this platform. |
| Background jobs (`app.jobs.runner`, `JobType`) | Infrastructure | N/A | `BATCH_INGESTION`, `DATA_QUALITY_SCAN`, and `DATASET_BUILD` and `ANOMALY_DETECTION_RUN` are registered and used; **`DETECTOR_TRAINING` is declared in the `JobType` enum but has no registered handler** — detector training (`create_model_version`) has always run synchronously, in every phase, including this one | — | — | — | — | — | Confirmed by re-reading `app/jobs/handlers.py`: `register_job_handlers()` only registers three handlers | — |
| Existing model metadata | — | — | `ModelVersion.evaluation_results` (synthetic only), `ForecastModel.evaluation_results` (real backtest, always populated) | — | — | — | — | — | These are genuine existing "experiment-adjacent" metadata this phase builds a registry **on top of**, not a replacement for |
| Existing monitoring | — | — | `anomaly_service.get_monitoring_summary`, `condition_service.get_condition_monitoring_summary` — both real, count-based, explicitly not drift detection | — | — | — | — | — | Reused directly by the new `mlops_service.get_mlops_monitoring_summary`, not re-implemented |
| Existing tests | — | — | 204 backend tests (172 Phases 1-3 + 32 Phase 4), all passing at the start of this phase | — | — | — | — | — | — |

**What this audit ruled out up front:** there is no second trainable model
to compare against, no labeled outcome data for a real accuracy gate, and
no existing hot-deployment infrastructure. The registry below was designed
around exactly these four component kinds — one genuinely trainable ML
model, one statistical detector, one forecasting model, and a family of
heuristic methods — never assuming a fifth kind that doesn't exist.

## 1. Lifecycle design — adapted per component type, not forced uniformly

`app.models.mlops.LifecycleStage`: `EXPERIMENTING → TRAINED → EVALUATED →
VALIDATED → APPROVED → ACTIVE → RETIRED`, with `FAILED` reachable from the
training/evaluation step. `EXPERIMENTING` is defined but not yet reachable
through this module — there is no experiment-tracking UI in this phase
that would create a `ComponentVersion` before training completes; it is
reserved rather than faked.

The remaining six stages are real and mean something different per kind:

- **`TRAINED_ML_MODEL` / `STATISTICAL_DETECTOR`** (both backed by
  `ModelVersion`): register at `TRAINED` (or `FAILED`, honestly, if the
  underlying training failed) → a human-triggered
  `record_evaluation()` runs the real synthetic harness and moves it to
  `EVALUATED` → `VALIDATED` requires that evaluation actually produced a
  scenario result → `APPROVED` → `ACTIVE`.
- **`FORECASTING_MODEL`** (backed by `ForecastModel`): its mandatory
  backtest already ran during fitting (Phase 4's design), so it enters
  the registry **already at `EVALUATED`** — there is no separate
  `TRAINED` moment to represent. `VALIDATED` is only reachable if the
  underlying model's status is `JUSTIFIED`; a `NOT_JUSTIFIED` forecast can
  never be validated, by design, not by omission.
- **`HEURISTIC_SIGNAL_METHOD`** (no underlying trained row at all):
  registers directly at `EVALUATED` on the strength of this codebase's
  existing deterministic test suite. `VALIDATED` requires a human to cite
  what was actually reviewed (`evidence.reviewed_reference`) since there
  is no artifact to run a synthetic harness against.

Every transition is a named function in `app.services.mlops_service`
(`register_version`, `record_evaluation`, `validate_version`,
`approve_version`, `activate_version`, `rollback_component`,
`retire_version`) — never an implicit side effect of an unrelated action.

## 2. Experiment / run tracking

This phase does not introduce a second, parallel run-tracking table.
Phases 3-4 already built real run records — `ModelRun` (detection runs),
`ForecastRun` (forecast generation) — that carry config, time range,
status, timestamps, and bounded real metrics, and never overwrite history.
`RetrainingRequest` (new in this phase) is the closest analogue to a
"training run" record: it carries the exact config used, `reason`
(mandatory — no anonymous retraining), `status`
(`requested → running → evaluated/failed`), `started_at`/`finished_at`,
and a link to the `ComponentVersion` it produced. Two requests against the
same component are directly comparable by reading their `config` and
`resulting_component_version_id`'s outcome side by side — there was no
need to invent a fifth table duplicating what `ModelRun`/`ForecastRun`
already do well.

## 3. Dataset & feature provenance

`RetrainingRequest.config` is a full, replayable snapshot (sensor ids,
reference period, step size, threshold overrides) — the same "config
snapshot" discipline `DatasetBuild.config` and `ModelVersion.detector_config`
already established in Phases 2-3. No new fingerprinting scheme was
added: `ModelVersion`/`ForecastModel`/`FeatureDefinition` IDs are already
stable, real identifiers, and inventing a checksum over already-immutable
rows would add complexity without adding trust. Where "reference period"
and "comparison period" matter for a genuinely new capability (shift
analysis, §12), both periods are recorded explicitly on
`ShiftAnalysisRun`, never inferred.

## 4. Feature contracts & compatibility

Phase 3 already enforces this at the point that matters: `ModelVersion.
feature_schema` (isolation_forest only) names the ordered feature list a
detector was trained on, and `execute_detection_run` rebuilds the scoring
matrix from that exact stored config — there is no path to silently score
a differently-shaped vector. This phase adds one more layer, at
activation time: `_check_activation_compatibility` in `mlops_service`
re-resolves every sensor id the active-candidate version depends on
(`get_sensor_in_org`) and refuses activation (422, with the specific
sensor id named) if any no longer exists in the organization — a
component cannot be activated into a state that would fail at the very
next scoring call. This is deliberately a pre-activation check, not a
scoring-time guess: it fails at the moment a human takes the action, with
an actionable message, rather than failing mysteriously later.

## 5. Registry — honest categories, not "everything is a model"

`app.models.mlops.ComponentType` has exactly four values:
`TRAINED_ML_MODEL`, `STATISTICAL_DETECTOR`, `FORECASTING_MODEL`,
`HEURISTIC_SIGNAL_METHOD`. `register_version` enforces that a component's
declared type matches both the `VersionKind` being registered
(`model_version`/`forecast_model`/`heuristic`) **and**, for
`ModelVersion`-backed components, the underlying `detector_type` — checked
against a *set* of allowed detector types per component type
(`_COMPONENT_TYPE_TO_DETECTOR_TYPES`), not a single fixed one:
`TRAINED_ML_MODEL` accepts `isolation_forest` or `mahalanobis` (both are
real `sklearn` estimators fit on data — Phase 14 added the Mahalanobis
detector to this same bucket rather than inventing a fifth component
type for it), `robust_zscore` only under `STATISTICAL_DETECTOR` (closed-
form, never fitted) — verified by
`test_component_type_and_version_kind_mismatch_rejected` and, for the
Phase 14 addition specifically,
`test_mahalanobis_component_registers_under_trained_ml_model_and_retrains`
/ `test_robust_zscore_still_rejected_under_trained_ml_model`. Retraining a
`TRAINED_ML_MODEL`/`STATISTICAL_DETECTOR` component (§13) resolves which
detector_type to retrain as from an explicit `config.detector_type`, or
failing that from the component's own most recent existing version — it
never assumes the component_type implies a single detector_type anymore
(`mlops_service._resolve_retraining_detector_type`). Full history is
preserved: `ComponentVersion` rows are never deleted, only their
`lifecycle_stage` changes, and a demoted-from-active version sits at
`APPROVED`, still inspectable and still eligible for rollback.

**A design gap found and fixed during self-review (§22):** the first draft
let a `ComponentVersion`'s `version_number` be reused directly from the
underlying `ModelVersion.version`/`ForecastModel.version` (which are only
unique per `(organization, name)`) without checking that the component's
own `name` matched the underlying artifact's name. Two differently-named
detectors registered under one component could have collided on
`(component_id, version_number)`, tripping the database's own unique
constraint as an opaque 500 instead of an honest, actionable error — and
even without a collision, "component X's active version" would stop
meaning anything coherent. Fixed by requiring `model_version.name ==
component.name` (and the same for forecast models) before registration is
allowed, with a regression test
(`test_registering_a_differently_named_detector_is_rejected`) proving the
422 and its message.

## 6. Artifact management

No new artifact store was built. The only "artifact" a trained model in
this codebase produces is the JSON-serializable config/summary already
stored on `ModelVersion`/`ForecastModel` (Phase 3's isolation forest is
deliberately **never pickled** — see that module's docstring — it is
re-fit deterministically from stored config with a fixed seed instead).
There is therefore nothing to checksum that isn't already an immutable
database row with its own primary key, and no internal storage path is
ever exposed — every API response returns ids and JSON summaries, never a
filesystem or serialization path. Building a separate blob store for
artifacts that don't exist would be exactly the "infrastructure added
because it's popular" this project's principles forbid.

## 7. Reproducibility

- **Isolation Forest**: `random_state=42`, fit fresh from immutable stored
  config every time — reproducible given the same measurement data, which
  is itself immutable once ingested. Verified again in this phase's live
  validation (§21): the same real detector, retrained on the same real
  reference period, produced structurally identical output.
- **Robust z-score / linear-trend forecaster**: closed-form statistics —
  deterministic by construction, no seed needed.
- **Degradation classification / change detection**: pure functions over
  fixed constants — deterministic by construction.
- **Limitation, stated honestly**: this platform does not pin or record
  library versions (`scikit-learn`, `scipy`, `numpy`) per training run.
  `requirements.txt` pins exact versions for the whole application, and
  every run in this session used that one pinned environment, but a
  `ComponentVersion` does not itself carry an environment fingerprint. If
  this platform ever runs against multiple deployed environments with
  drifted dependency versions, "reproducible" would need that fingerprint
  added — it is not claimed here because it is not built.

## 8. Evaluation gates — task-specific, never a universal accuracy threshold

There is exactly one place in this platform where a genuine accuracy-style
gate could be built honestly, and it already existed before this phase:
`ForecastModel.status = JUSTIFIED` requires beating two naive baselines by
≥10% MAE on a real leak-free holdout — an evidence-based, non-arbitrary
gate. `validate_version` **reuses this gate directly**: a
`NOT_JUSTIFIED`/`FAILED` forecast model can never be validated (409,
tested by `test_forecasting_component_validate_blocked_when_not_justified`).
For the anomaly detectors, no real accuracy gate is possible (no labels),
so the gate is evidence-of-evaluation, not evidence-of-accuracy: `validate_
version` requires that `record_evaluation()` actually ran the synthetic
harness and produced a scenario result before a human can sign off — it
checks that the pipeline was genuinely exercised, never a manufactured
precision/recall threshold. For heuristics, the gate is a mandatory human
citation of what was reviewed. Three different gates for three different
kinds of evidence — never one universal number applied where it wouldn't
mean anything.

## 9. Activation & rollback

`activate_version` requires `APPROVED` and re-validates activation
compatibility (§4) before promoting a version to `ACTIVE`; any
currently-active sibling is demoted to `APPROVED` in the same transaction
— never deleted, never silently ignored. `rollback_component` reactivates
either an explicitly named target (must itself be `APPROVED`) or, with no
target given, the most-recently-demoted version, by the same
compatibility-checked path. Both actions are logged
(`GovernanceEventType.ACTIVATED` / `ROLLED_BACK`) with the superseded
version's id and number recorded in `details`. Nothing in this codebase
ever calls `activate_version` automatically — not `execute_retraining_
request`, not any job handler. "True hot deployment" (swapping a live
serving process without a restart) is out of scope and not claimed: this
platform's "activation" is an application-level state change — the next
call to `score_via_active_component` picks up the newly active version
immediately, because every score request re-reads `ComponentVersion` from
the database rather than caching an in-process reference — but there is no
separate serving tier to hot-swap.

## 10. Safe scoring / inference contract

`mlops_service.score_via_active_component` is the single entry point:

1. Resolve the component; if it has no `ACTIVE` version, refuse (409) and
   log `SCORING_REFUSED` — never silently fall back to an old or default
   version.
2. Dispatch by `version_kind` to the real scoring function
   (`anomaly_service.create_detection_run` /
   `condition_service.create_forecast_run`), which already validate
   detector/model status, time-range sanity, and data availability.
3. Validate the request shape up front — missing `scoring_start`/
   `scoring_end` (detector) or `as_of`/`horizon_seconds`/`step_seconds`
   (forecaster) is a 422 naming exactly which fields are missing, not a
   silent default.
4. Return a `provenance` envelope on every success —
   `component_id`, `component_version_id`, `version_number`,
   `version_kind`, `activated_at` — so any output can be traced back to
   the exact version that produced it, and log `RUN_COMPLETED`/
   `RUN_FAILED` either way.

Heuristic-kind components are explicitly **not** scored through this
contract (422, with an explanation) — `app.condition.degradation` is
already invoked directly by `POST /condition/runs` with its own full
evidence payload; wrapping it a second time here would be a redundant
proxy, not a safety improvement.

## 11. Monitoring — OPERATIONAL / DATA / MODEL PERFORMANCE, kept distinct

`GET /mlops/monitoring` returns three separately labeled sections, never
merged into one score:

- **operational**: real counts — components by type, versions by
  lifecycle stage, retraining requests by status, governance events by
  type, and the list of components with an active version right now.
- **data**: the count of recorded shift-analysis runs and an explicit note
  that this platform computes no continuous/automated drift score — only
  discrete, on-demand comparisons (§12).
- **model_performance**: `anomaly_service.get_monitoring_summary` and
  `condition_service.get_condition_monitoring_summary`, reused verbatim
  (not re-implemented) — both are real run/anomaly/signal counts by
  outcome, not accuracy metrics, with an explicit note that no delayed
  real-world performance monitoring exists because no ground-truth labels
  exist.

## 12. Data / feature shift analysis

Genuinely new in this phase. `run_shift_analysis` compares a reference
period against a comparison period for one sensor: observation counts,
missingness, mean/std/median/min/max for both, and — only when both
periods have at least `MIN_SHIFT_OBSERVATIONS=10` real observations — a
Welch's t-test (mean difference, robust to unequal variance) and a
two-sample Kolmogorov-Smirnov test (overall distribution-shape
difference). Below that threshold, the result honestly reports
`test_performed: false` and why, rather than computing a test on data too
thin to mean anything (verified live against the real seeded database,
§21, and by `test_shift_analysis_insufficient_data_is_honest` /
`test_shift_analysis_detects_a_real_mean_shift`).

**PSI (population stability index) was deliberately not implemented.** It
requires pre-defined, stable bins drawn from a trusted reference
distribution — this platform has no validated basis for choosing those
bins yet, and a PSI number computed against arbitrary bins would look
precise while being an unearned invention, exactly what this project's
principles forbid. The recorded `limitations` field says so on every run,
along with the honest caveat that the t-test/KS-test p-values assume i.i.d.
samples, which autocorrelated sensor time series are not — they are
indicative of a real distributional difference worth a human look, not a
formally calibrated significance level. **No shift analysis ever triggers
retraining automatically** — it is a recorded observation, full stop.

## 13. Controlled retraining workflow

`RetrainingRequest`: `REQUESTED` (created only with a mandatory, non-empty
`reason` — no anonymous retraining) → `execute_retraining_request`
(explicit, separate call) sets `RUNNING`, commits that state **before**
entering the training call (the exact `db.commit()`-before-`try` pattern
Phase 4's self-review established, to guarantee a mid-training exception
can still mark the request `FAILED` with the real error rather than
crashing on a `None` row — see that phase's `condition_service` fix) →
calls the real `anomaly_service.create_model_version` or
`condition_service.create_forecast_model` with the request's config → on
success, registers a new `ComponentVersion` (at `TRAINED`/`EVALUATED`/
`FAILED` per the same per-kind rule as manual registration, §1) → sets the
request to `EVALUATED` or `FAILED`. **It never calls `activate_version`.**
A retrained component sits at `TRAINED`/`EVALUATED` next to the still-
active prior version until a human explicitly evaluates (if applicable),
validates, approves, and activates it — verified in both the API test
suite (`test_isolation_forest_full_lifecycle_e2e`'s retraining section)
and the live validation against real data (§21, step 7): the original
active version was confirmed still active immediately after retraining.

## 14. Governance & auditability

`GovernanceEvent` is append-only — never updated, never deleted — and
records `component_registered`, `version_registered`,
`evaluation_recorded`, `validation_decision`, `approval_decision`,
`activated`, `rolled_back`, `retired`, `retraining_requested`,
`run_completed`/`run_failed`, `scoring_refused`, and
`shift_analysis_recorded`, each with `actor_user_id` (real, from the
authenticated request), `from_stage`/`to_stage`, and a small `details`
JSON payload of already-public summary values (ids, counts, short
reasons). `details` is never populated with a token, password, or raw
credential — the only things ever written into it are values already
returned elsewhere in the API (evaluation summary numbers, stage names,
truncated free-text reasons/notes).

## 15. API surface (`/api/v1/mlops/*`)

| Route | Method | Purpose | Roles |
|---|---|---|---|
| `/mlops/components` | GET, POST | List / register components | read: any; write: ADMIN/ENGINEER |
| `/mlops/components/{id}` | GET | Component detail | any |
| `/mlops/components/{id}/versions` | GET, POST | List / register versions | read: any; write: ADMIN/ENGINEER |
| `/mlops/versions/{id}` | GET | Version detail | any |
| `/mlops/versions/{id}/evaluate` | POST | Record a real synthetic evaluation | ADMIN/ENGINEER |
| `/mlops/versions/{id}/validate` | POST | Human validation gate | ADMIN/ENGINEER |
| `/mlops/versions/{id}/approve` | POST | Explicit approval | **ADMIN only** |
| `/mlops/components/{id}/activate` | POST | Activate an approved version | **ADMIN only** |
| `/mlops/components/{id}/rollback` | POST | Roll back to a prior version | **ADMIN only** |
| `/mlops/versions/{id}/retire` | POST | Retire a version | **ADMIN only** |
| `/mlops/components/{id}/score` | POST | Safe scoring contract | ADMIN/ENGINEER |
| `/mlops/components/{id}/retraining-requests` | GET, POST | Request retraining | read: any; write: ADMIN/ENGINEER |
| `/mlops/retraining-requests/{id}/execute` | POST | Execute a requested retraining | ADMIN/ENGINEER |
| `/mlops/retraining-requests`, `/{id}` | GET | Inspect requests | any |
| `/mlops/shift-analysis` | GET, POST | Run / list shift analyses | read: any; write: ADMIN/ENGINEER |
| `/mlops/governance-events` | GET | Audit log, paginated, filterable by component | any |
| `/mlops/monitoring` | GET | Operational/data/model-performance summary | any |

Approval and activation/rollback/retirement are deliberately narrower than
the rest of this API's usual ADMIN/ENGINEER write boundary — those are the
steps that put a version into (or take it out of) production use, so this
platform requires ADMIN specifically. Every route is organization-scoped
server-side (never trusted from the frontend), every list is paginated and
bounded, and no route ever returns a raw model artifact — only ids and
JSON summaries, matching every other API in this platform.

## 16. Frontend — Intelligence Lifecycle & Governance page

`/mlops` reads real backend data throughout: component registration and
list, a per-component version table with real `lifecycle_stage` badges,
contextual lifecycle-action panels (evaluate/validate/approve/activate/
retire, each only shown when the version is actually in the stage that
action requires), a controlled-retraining panel with a real request/
execute flow, a governance audit-log feed, and a shift-analysis form whose
results table shows the real Welch/KS statistics or the honest
insufficient-data reason. `EXPERIMENTAL`/`VALIDATED`/`ACTIVE`/`HEURISTIC`/
`SYNTHETICALLY VALIDATED` distinctions are visible through the stage
badges and the version-kind labels shown alongside them — never collapsed
into a single generic "model" label.

## 17. Background job integration

**Scope decision, stated plainly:** MLOps lifecycle operations
(evaluation, validation, retraining execution) run synchronously in this
phase, calling the same synchronous training primitives
(`create_model_version`, `create_forecast_model`) Phases 3-4 already
built — and, per the §0 audit, `JobType.DETECTOR_TRAINING` was declared
but never wired to a handler even before this phase, so there was no
existing async training path to integrate with. Adding fake async
plumbing around an operation that has never been async anywhere else in
this codebase would be exactly the kind of infrastructure this project's
principles warn against. `RetrainingRequest` still carries real
`started_at`/`finished_at` timestamps and a real `status` progression
(mirroring `ConditionRun`/`ForecastRun`'s precedent for "long-running-ish
but synchronous" operations), so the request/execute split still gives
genuine before/after visibility — it is just not routed through
`app.jobs.runner`. The upgrade path is straightforward and explicitly
documented here rather than silently deferred: if a future phase adds
supervised training on larger data volumes, `execute_retraining_request`
can be split into a job-runner-backed async path using the exact
`MAX_DETECTION_ROWS_SYNC`-style threshold pattern `create_detection_run`
already establishes, without changing this module's public contract.

## 18. Security & reliability review

- Approval/activation/rollback/retirement require ADMIN specifically
  (§15) — narrower than this platform's usual write boundary, on purpose.
- Every service function re-validates `organization_id` scope before
  touching any row (`get_component`, `get_component_version`, `get_
  retraining_request`, `get_shift_analysis_run` all filter by org and
  404 rather than leak existence across tenants — verified by
  `test_cross_org_isolation_for_components`).
- No artifact is ever trusted merely by existing: `_check_activation_
  compatibility` re-resolves every dependency at activation time, not
  just at training time.
- Input validation happens before any inference: `score_via_
  active_component` checks the request shape before dispatching, and
  `execute_retraining_request` commits its `RUNNING` state before
  training so a mid-run exception can never leave a request stuck or the
  underlying row missing (the exact class of bug Phase 4's self-review
  found and fixed, applied here proactively).
- No silently-misleading failure output anywhere in this module — every
  failure path (`ConflictError`/`ValidationFailedError`/`NotFoundError`)
  returns this platform's standard error contract with an actionable
  message; there is no code path that returns a stale/default/fabricated
  result on failure.
- `GovernanceEvent.details` never carries a secret (§14) — reviewed line
  by line across every `_log_event` call site.
- No insecure convenience shortcuts: activation, rollback, and retirement
  each require the caller to pass explicit ids — there is no "activate the
  latest version" implicit default anywhere.

## 19. Tests

12 new tests in `tests/test_api_mlops.py`, real DB + HTTP via
`TestClient`, mirroring `tests/test_api_anomaly.py` and `tests/test_api_
condition.py`'s conventions. Coverage includes: the full
train → register → evaluate → validate → approve → activate → score →
retrain → activate-v2 → rollback lifecycle against a real Isolation Forest
detector (the flagship, §21); the forecasting-model honesty gate blocking
an unjustified model and the full lifecycle for a justified one; the
heuristic evidence-only validation gate; scoring refusal with no active
version (and its governance event); the component-type/version-kind
mismatch rejection; the differently-named-detector rejection (the bug
found in self-review, §5); role authorization (VIEWER blocked from every
write, and the ADMIN-only approve/activate boundary specifically);
cross-org isolation; both shift-analysis paths (insufficient data, and a
real detected mean shift); and the monitoring summary's real structure.
Every backend test in this platform — 216 total (204 carried over from
Phases 1-4 + 12 new here, after the self-review fix landed a 12th test)
— was actually run for this phase; see §23.

## 20. Documentation scope

This document describes only what was built: the registry and lifecycle
on top of Phases 3-4's real detectors/forecasters, controlled retraining
that calls real training code, on-demand shift analysis with two real
statistical tests, and an append-only governance log. It does not
document a model artifact store, a feature store, a message-broker-backed
job queue, or true hot-deployment infrastructure, because none of those
were built — §6, §17, and §9 say so explicitly rather than leaving the gap
implicit.

## 21. End-to-end validation against real data

Beyond the API test suite, this phase's stop condition — "at least one
real intelligence component can move through a traceable lifecycle... with
its inference outputs traceable back to the exact version that produced
them" — was validated against the platform's actual seeded database
(`Riverbend Manufacturing (Synthetic Demo)`, the real `bearing_temp_de`
sensor, 5,948 real measurements spanning ~21 days), using the real
service-layer functions directly (the same code paths the API calls), not
a script that re-implements or simulates any of it:

1. Trained a real Isolation Forest detector on real reference data (192
   real reference feature-vector rows) — `status=trained`.
2. Registered it as a `TRAINED_ML_MODEL` component version —
   `lifecycle_stage=trained`.
3. Ran the real synthetic evaluation harness — `lifecycle_stage=evaluated`.
4. Validated (human notes citing the real evaluation), approved, and
   activated it — `lifecycle_stage=active`.
5. Scored through the active component against a real held-out window: a
   genuine detection run, 96 real points scored, **1 real anomaly
   flagged** — not fabricated, and traceable via the returned provenance
   envelope back to this exact component/version.
6. Ran a real shift analysis, first week vs. last week of the real sensor
   history: a genuine +0.74°C mean increase, Welch's t-test
   p≈5.9×10⁻²⁶, KS-test p≈2.1×10⁻¹⁵ — a real, statistically strong
   finding on the platform's actual data, recorded with its stated
   limitations, and (correctly) not auto-triggering anything.
7. Requested and executed a real retraining — a genuine second
   `ModelVersion`/`ComponentVersion` (v2), confirmed the original v1
   **remained active** throughout (retraining never auto-activates).
8. Pushed v2 through the same gate and activated it, then rolled back with
   no explicit target — confirmed the component returned to v1
   (`version_number=1`), the pre-retraining version, never deleted.
9. Read back the real governance audit trail: 16 real events with real
   timestamps and the real admin user as actor.
10. Read back the real monitoring summary: one real active component,
    correctly showing v1 as active after the rollback.

Every number above is a genuine output of this run, not illustrative text.

## 22. Principal MLOps self-review

Reviewed against the checklist this phase specifies:

- **Traceability**: every scoring output carries a provenance envelope
  (§10); every lifecycle action is logged (§14). ✓.
- **Reproducibility**: stated honestly with its one real limitation — no
  per-run environment fingerprint (§7). ✓, with the gap named rather than
  hidden.
- **Feature-mismatch prevention**: pre-activation dependency re-resolution
  (§4/§9); Phase 3's existing `feature_schema` enforcement at scoring
  time. ✓.
- **Honest evaluation claims**: three distinct, task-appropriate gates
  (§8), none of them an invented universal accuracy threshold. ✓.
- **Controlled lifecycle transitions**: every transition is a named,
  single-purpose function; none happen as a side effect. ✓.
- **Safe active-version changes**: activation/rollback never delete,
  always demote; nothing auto-activates a freshly retrained version (§9,
  §13, verified live in §21 step 7). ✓.
- **Real monitoring signals**: §11's three sections are all backed by real
  counts/reused summaries; no fabricated drift score anywhere. ✓.
- **Heuristic-vs-ML distinction**: `ComponentType` has four honest values,
  enforced at registration (§5); the frontend surfaces the distinction
  directly (§16). ✓.
- **Safe failure**: `execute_retraining_request`'s commit-before-try
  pattern (§13/§18) prevents the exact "masked exception" bug class Phase
  4 found; every other write path uses the same standard error contract.
  ✓.
- **Appropriate, not over-engineered**: no artifact store, no message
  broker, no async training path, no PSI — each omission is a deliberate,
  documented choice (§6, §12, §17), not an oversight.

**One real weakness was found and fixed** during this review: the
version-number/component-name gap described in §5. It was not a
theoretical concern — it would have produced an opaque 500 (a unique-
constraint violation) the first time two differently-named detectors were
registered under one component, exactly the kind of silently-misleading
failure this platform's principles forbid. Fixed with an explicit 422 and
a regression test (`test_registering_a_differently_named_detector_is_
rejected`) before this phase's validation gate was run.

## 23. Validation gate — actually executed

- `python3 -m pytest -q tests/test_api_mlops.py` → **12 passed**.
- `python3 -m pytest -q` (the full backend suite) → **216 passed** (204
  carried over from Phases 1-4, unmodified and still green, + 12 new).
- `alembic upgrade head` on the real seeded database, followed by
  `alembic check` → **"No new upgrade operations detected."** (migration
  `806aec7b2bfd`, purely additive — five new tables, no drops, no changes
  to any existing table).
- Frontend: `npm run build` (`tsc -b && vite build`) → **built
  successfully**, zero TypeScript errors, zero build errors (one
  pre-existing informational chunk-size notice, not an error).
- The full real-data lifecycle in §21 was executed against the actual
  seeded database (not a fixture) — registry workflow, evaluation
  gate, validation gate, approval, activation, safe scoring with
  provenance, real shift analysis, controlled retraining, second
  activation, and rollback all completed successfully, with one real
  anomaly genuinely flagged and one real, statistically significant
  shift genuinely detected.

No test was skipped, and nothing above is asserted without having actually
been run in this session.

## 24. Phase 14 addendum — a second trained ML model joins the registry

Phase 14 added `app.anomaly.mahalanobis_detector` (a robust-covariance
multivariate anomaly detector — see `docs/ANOMALY_DETECTION.md` §1a for
what it detects and why). This section records what changed in THIS
layer as a result — not a restatement of §0-23 above, which describe the
registry as it stood at the end of Phase 5 and remain historically
accurate for that snapshot.

What changed here:

- `_COMPONENT_TYPE_TO_DETECTOR_TYPES` (renamed from the Phase 5-era
  `_COMPONENT_TYPE_TO_DETECTOR_TYPE`) now maps `TRAINED_ML_MODEL` to a SET
  of allowed detector types (`{isolation_forest, mahalanobis}`) instead of
  one fixed type — both are real `sklearn` estimators fit on data, the
  same category boundary `ComponentType`'s own docstring already drew.
  `STATISTICAL_DETECTOR` is unchanged (`{robust_zscore}` only).
- `register_version`'s detector-type compatibility check now tests set
  membership instead of equality — functionally identical for the two
  pre-existing types, newly permissive only for `mahalanobis` under
  `TRAINED_ML_MODEL`.
- `execute_retraining_request` could no longer assume one detector_type
  per component_type once `TRAINED_ML_MODEL` had two. New helper
  `_resolve_retraining_detector_type` resolves it, in order: an explicit
  `config["detector_type"]` (validated against what the component_type
  allows) → the component's own most recent existing version's detector
  type (retraining must reproduce the same detector, never silently
  switch it) → the component_type's sole allowed type if there is only
  one and no prior version exists. A `TRAINED_ML_MODEL` component with no
  prior version and no explicit `config.detector_type` now gets an honest
  `ValidationFailedError` naming what it needs, rather than a wrong guess.
- No new `ComponentType`, no new `VersionKind`, no new lifecycle stage, no
  schema migration — the existing registry design already generalized
  cleanly to a second trained model once the fixed 1:1 map was widened to
  a set.

Tests added and passing (see `tests/test_api_mlops.py`):
`test_mahalanobis_component_registers_under_trained_ml_model_and_retrains`
(full register → evaluate → validate → approve → activate → score →
retraining-request → execute lifecycle, asserting the retrained v2 is
still `detector_type=mahalanobis`) and
`test_robust_zscore_still_rejected_under_trained_ml_model` (the other
direction of the same fix: widening `TRAINED_ML_MODEL` to accept
`mahalanobis` must not also accidentally widen it to accept
`robust_zscore`). The full backend suite — 512 tests as of this addendum
(494 confirmed passing at the start of Phase 14 per
`docs/history/PHASE_13_REPORT.md`, +14 new pure-function tests for the
Mahalanobis detector itself, +4 new API-level tests covering this
registry change) — passes; see `docs/history/PHASE_14_REPORT.md` for the
complete run log.

## What this is not

This is not a model artifact store, a feature store, a message-broker-
backed orchestration system, a continuous/automated drift-detection
pipeline, or true zero-downtime hot deployment infrastructure. It is not a
second "trained ML model" this platform doesn't have, and it never
describes the robust z-score detector, the linear-trend forecaster, or the
degradation classifier as one. It does not retrain anything automatically,
does not activate anything automatically, and does not compute a PSI
score against bins this platform has no basis for choosing. What it is: a
real, working, tested, and live-validated governance layer that took one
genuinely trained model from a defined dataset and configuration, through
real processing and evaluation, to version registration and controlled
activation — with its inference outputs traceable back to the exact
version that produced them, and a controlled path to retrain, re-approve,
re-activate, or roll back, entirely through this platform's own real
application logic.
