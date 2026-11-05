# Anomaly Detection Intelligence — What This Layer Does and Doesn't Do

This describes what is actually implemented, run, and
tested today. It is explicitly NOT a predictive-maintenance system — see
"What this is not" at the bottom, and `docs/TEMPORAL_INTELLIGENCE.md` for
the layer this one builds directly on.

**The one rule everything else here follows: an anomaly is not a failure.**
A detector flags a period as statistically unusual relative to a declared
reference period. Nothing in this codebase — not a variable name, not a
response field, not a piece of frontend copy — claims a flagged period
"will fail," is a "confirmed fault," or is "the cause of" anything. That
judgment belongs to a human reviewer with domain context this system does
not have, which is exactly what the review workflow (`AnomalyInvestigation`)
exists to capture.

## 0. What this builds on, and the truth audit that preceded it

Before writing any detection code, the actual state and 2 was
re-verified by reading the code and running the existing test suite (138
tests, all passing at the outset) rather than trusting prior
completion reports. What was confirmed real and reused directly:

- `app/timeseries/data_access.py` — bounded, organization-scoped raw
  measurement loading (`TooManyObservationsError` on oversized ranges).
- `app/timeseries/dataset_builder.py::build_feature_matrix` and
  `app/timeseries/features.py::compute_feature_value` — the causally-safe
  feature computation used by the Isolation Forest detector below, inheriting
  its leakage guarantee for free (see `docs/TEMPORAL_INTELLIGENCE.md` §1).
- `app/jobs/runner.py` — the background job runner, extended with a new
  `JobType.ANOMALY_DETECTION_RUN` handler rather than replaced.
- `app/services/timeseries_service.py::get_sensor_in_org` — reused as the
  one multi-tenancy choke point every anomaly service function goes through.

What was found NOT real, despite existing as schema: `Anomaly`,
`ModelVersion`, `ModelRun`, `AnomalyInvestigation` existed as empty
SQLAlchemy tables with no detection logic, and `/api/v1/intelligence/*`
returned `501 not_yet_available` for all three routes it exposed. This
phase's job was to make that schema and those routes real, not to work
around a partially-working system — there wasn't one.

## 1. Detection methods, and why these two

Two detectors are implemented, deliberately chosen to be complementary
rather than redundant:

**Robust z-score** (`app/anomaly/robust_zscore.py` + `app/anomaly/baseline.py`)
— a classical statistical method: score = (value − reference median) /
(reference MAD × 1.4826). Chosen over mean/std specifically because a
reference period built from real, imperfect sensor data will itself contain
some outliers, and mean/std are dragged by them in a way median/MAD is not
(see `test_compute_baseline_median_and_mad_are_correct` for a worked
example: 30 clean points plus one 1000.0 outlier leaves the median and MAD
untouched while the mean visibly shifts). Single-sensor only. Its
documented limitation: a single global baseline doesn't adapt to daily/
seasonal cycles, so a sensor with a strong diurnal swing will show elevated
z-scores at its own normal daily extremes. The synthetic evaluation below
empirically confirms this — it is the one scenario type (`contextual_anomaly`)
this detector consistently misses.

**Isolation Forest** (`app/anomaly/isolation_forest_detector.py`, via
`sklearn.ensemble.IsolationForest`) — an unsupervised ML method operating
on rolling-window FEATURES (mean/std/rate-of-change per sensor, from
the feature layer), not raw values. Chosen because it makes no
distributional assumption, handles multivariate input natively (its
"multivariate mode" concatenates each configured sensor's features into
one wide vector per grid point, so it can learn cross-sensor relationships
— e.g. "this vibration reading is unremarkable alone but unusual given the
current temperature"), and trains in well under a second on this
platform's data volumes. It is explicitly NOT a deep-learning model —
nothing about a few thousand feature rows per sensor justifies one, and
depth here would be complexity for appearance, not for the data.

## 1a. A third detector, added: Mahalanobis distance (robust covariance)

**Mahalanobis distance** (`app/anomaly/mahalanobis_detector.py`, via
`sklearn.covariance.MinCovDet`) — closes a specific, real gap neither
detector above can cover: a point where EVERY sensor is individually
unremarkable, but the *relationship between them* has broken down (e.g.
vibration normally tracks load; on this reading it no longer does, even
though neither value alone is extreme). Robust z-score only ever looks at
one sensor. Isolation Forest can take multiple sensors, but its score
comes from an uninterpretable ensemble of random trees with no
closed-form statistical meaning and no per-relationship explanation.

Method: align every requested sensor (2-6; `MAX_SENSORS=6`) onto one time
grid (`app.timeseries.multi_sensor.align_sensors_on_grid` — mean
aggregation, no interpolation, a bucket missing any sensor's observation
is dropped, never fabricated), fit a robust covariance estimate
(`MinCovDet`, chosen over plain sample covariance for the identical
reason `app.anomaly.baseline` uses median/MAD instead of mean/std: a
reference period's own outliers would otherwise drag the estimate enough
to mask genuine anomalies) on the reference period's complete rows, and
score each scoring-window row by its (square-rooted) Mahalanobis distance
to that robust center. Requires `MIN_SAMPLES_PER_DIMENSION=10` reference
rows per sensor dimension in addition to the flat `MIN_REFERENCE_ROWS=30`
floor — a covariance matrix fit on too few samples relative to its
dimensionality is not a reliable estimate, enforced in code rather than
left to the caller.

Explicit, stated assumption: this method assumes the sensors' joint
relationship is approximately stable and elliptical — the same
assumption any covariance-based method makes — and is a **statistical
association measure, not a causal model** (the identical caveat
`app/timeseries/multi_sensor.py` already states for correlation; a high
score never identifies which sensor, if any, is physically at fault).
Flagged points within 30 minutes of each other merge into one event
(`_merge_into_events`, the same discipline `robust_zscore.py` already
applies) rather than one row per grid point — a sustained relationship
breakdown is one finding, not a flood of them.

**Real, executed evidence this is a genuinely new capability, not a
restatement of an existing one:** `tests/test_mahalanobis_detector.py::
test_mahalanobis_catches_what_univariate_zscore_cannot` builds a
deterministic, seeded, honestly-labeled synthetic scenario
(`app.anomaly.multivariate_synthetic_scenarios.build_correlation_
breakdown_scenario` — two sensors that normally move together, with a
window where one is resampled from its own normal marginal distribution
but decoupled from the other) and asserts, in one test, both that the
Mahalanobis detector recovers it (`precision=1.0, recall=1.0`) AND that
`app.anomaly.robust_zscore.detect` run independently on each sensor
alone does **not** (`recall=0.0`). This is executed, not asserted in
prose — see the test for the exact numbers.

None of the three detectors is persisted as a pickled model object. A `ModelVersion`
stores only JSON-serializable config (sensor IDs, feature specs, reference
period, threshold config) plus summary statistics; every detection run
re-fits the model fresh from that immutable config with a fixed
`random_state=42`. Since the underlying raw measurements never change,
this reproduces an identical model deterministically without the security
and complexity cost of deserializing pickled sklearn objects — the same
"recompute on demand" pattern the `DatasetBuild` already uses for
its feature matrix.

## 2. Baseline / "normal behavior" methodology

`app/anomaly/baseline.py::compute_baseline` restricts to the declared
`[reference_start, reference_end]` window, requires at least
`MIN_FOR_BASELINE = 30` observations (raising
`InsufficientReferenceDataError` otherwise — see §12 for how the API
surfaces this honestly rather than training on too little data), and
computes median/MAD/mean/std/min/max. For Isolation Forest, the analogous
step is `build_reference_summary`, which computes the reference period's
feature matrix and requires `MIN_REFERENCE_ROWS = 30` grid points with
every requested feature actually computed.

**Causal separation is enforced, not just documented.** By default,
`app.services.anomaly_service.create_detection_run` refuses to score a
period starting before the detector's `reference_period_end` — scoring
data from before the reference period ends would let a baseline see into
its own evaluation window. Where this check is genuinely useful for
deliberate retrospective/exploratory analysis, `allow_retrospective_scoring
= true` lifts it explicitly, and the resulting run's `metrics.retrospective`
field is set `true` so nobody downstream mistakes it for a causally-clean
detection replay. See `test_detection_run_blocks_retrospective_scoring_by_default`.

## 3. Unified scoring architecture — and what "unified" does not mean

Every `Anomaly` row carries: `period_start`/`period_end`, `asset_id`/
`sensor_id`, `detector_type` + the specific `model_run_id` (which pins an
exact `ModelVersion` — config, feature schema, threshold), `raw_score`,
`normalized_score` (Isolation Forest and Mahalanobis only — see below),
`threshold_used`, `severity_tier`, `contributing_signals` (the
explanation payload), `data_quality_context`, and `is_synthetic_scenario`.

**What is explicitly NOT unified: the scores themselves.** A robust
z-score of 5.6, an Isolation Forest score of 0.2, and a Mahalanobis
distance of 6.0 are not "the same amount of anomalous" — all three come
from different, non-comparable scales. `app/anomaly/scoring.py`'s module
docstring states this outright, and nothing in the codebase averages or
ranks raw scores across detector types. `normalized_score` for Isolation
Forest and Mahalanobis is a percentile rank of a candidate against its
OWN model's reference-period score distribution — justified because both
sides come from the same fitted model — and is explicitly documented as
not comparable to any other detector's `normalized_score`, including the
other one of this same pair. Any UI or API sort that mixes detector types
sorts by `severity_tier` (see §4), never by raw score.

## 4. Threshold policy and severity tiers

`app/anomaly/thresholds.py` derives one of three tiers from how far a
score's magnitude sits past its own detector's threshold:
`OBSERVED_UNUSUAL_BEHAVIOR` (at or just past threshold),
`CANDIDATE_ANOMALY` (≥ 1.3× threshold, configurable), `HIGH_PRIORITY_ANOMALY`
(≥ 1.8× threshold, configurable). Defaults per detector type
(`DETECTOR_DEFAULT_THRESHOLD`: 3.5 robust standard deviations for
robust z-score, 0.15 for the negated Isolation Forest decision function,
4.0 robust Mahalanobis distance units addition)
are declared constants, not tuned magic numbers, and are overridable per
detector at training time. No color, no "risk score," no incident is
created purely from a detection — flagging is separate from the human
review workflow in §5, and always will be (creating an "incident" concept
automatically from a score is exactly the kind of unmotivated escalation
this platform's core principle rules out).

## 5. Anomaly lifecycle and the append-only investigation log

`AnomalyStatus`: `DETECTED → UNDER_REVIEW / ACKNOWLEDGED / RESOLVED /
FALSE_POSITIVE`, with `RESOLVED`/`FALSE_POSITIVE` reopenable back to
`UNDER_REVIEW` (`app.services.anomaly_service.ALLOWED_TRANSITIONS`; any
other transition is rejected with `422` — see
`test_invalid_status_transition_rejected`).

The critical design choice: `transition_anomaly_status` is the ONLY place
that ever changes an `Anomaly` row after creation, and the only field it
changes is `status`. Every transition also inserts an append-only
`AnomalyInvestigation` row (`from_status`, `to_status`, `reviewed_by_user_id`,
`notes`, `created_at`) that is never updated or deleted. The original
detection evidence — `raw_score`, `detector_type`, `contributing_signals`,
`data_quality_context`, the period itself — is written once by the
detection run and is structurally unreachable from the review code path.
`test_robust_zscore_full_workflow_e2e` and the live end-to-end validation
in §"Validation actually performed" both assert `raw_score` is byte-identical
before and after two review transitions.

## 6. Multi-method intelligence — no fake super-score

When multiple detectors — any combination of robust z-score, Isolation
Forest, and Mahalanobis distance — are trained against the
same sensor(s), their results are shown side by side (same
`/anomaly/anomalies` list, filterable, each carrying its own
`detector_type`) — never merged into one combined score. Agreement or
disagreement between methods is something a human reviewer reads off the
list, not something this system computes and asserts a verdict from; doing
that without a validated calibration procedure would be exactly the
"fake super-score" the source spec warns against.

## 7. Temporal context — "what changed," not just a number

`GET /anomaly/anomalies/{id}/context` (`anomaly_service.get_anomaly_context`)
loads a bounded window around the flagged period (`context_hours`,
default 6, max 168) and returns before/during/after summary statistics
plus the raw series for charting. For a multivariate (multi-sensor)
anomaly, there is no single time series to show, and the endpoint says so
explicitly (`"This anomaly spans multiple sensors..."`) rather than
picking one sensor arbitrarily. The frontend renders this as a line chart
with the flagged period shaded, plus explicit before/during/after stat
cards — see §16.

## 8. Multi-sensor context — association, not causation

Isolation Forest's multivariate mode and the Mahalanobis
detector are the two places multiple sensors feed one detection. Isolation
Forest's `contributing_features` explanation shows each input feature's
own deviation from the reference period (§9) — this is still per-feature
statistical deviation, not a claim that one sensor's behavior caused
another's. The Mahalanobis detector's `explanation.caveat` field states
the same thing even more explicitly, every time: a high joint distance is
a statistical association measure and never identifies which sensor, if
any, is physically at fault. `app/timeseries/multi_sensor.py`'s existing
correlation caveat applies to both without modification:
nothing in this codebase computes or claims a causal relationship between
sensors.

## 9. Explainability — derived from real calculations, not generated text

Every `Anomaly.contributing_signals` payload is built directly from the
detector's own math, never a templated-sounding LLM-style narrative:

- **Robust z-score**: `baseline_median`, `baseline_robust_std_estimate`,
  the reference period, the observed value, and the exact z-score — the
  `description` field is a single sentence assembled from those same
  numbers (`f"Value {v:.4g} is {z:.1f} robust standard deviations..."`),
  not free text.
- **Isolation Forest**: `contributing_features` — the top 3 features by
  `|z-score|` against the reference period's own feature mean/std, each
  with `observed_value`, `reference_mean`, `z_score`. This is an explicit,
  documented approximation (`isolation_forest_detector.py`'s module
  docstring says so directly) — sklearn's `IsolationForest` has no
  built-in per-feature attribution, and this is not represented as
  SHAP-quality attribution.
- **Mahalanobis**: `contributing_sensors` — every input
  sensor's own univariate robust z-score (median/MAD, computed from the
  SAME reference period the covariance was fit on), sorted by `|z|`, plus
  an explicit `individually_unremarkable` boolean (true when every
  sensor's own `|z| < 2.0` despite the flagged joint distance — the
  precise signature of the relationship-breakdown case this detector
  exists for) and the non-causal `caveat` field from §8.

Every explanation states its method, what was compared to what, and (for
Isolation Forest and Mahalanobis) the explicit limitation that this is an
approximation or association measure, not a causal or SHAP-quality claim.

## 10. Data quality and confidence — never a fabricated percentage

Every `Anomaly` carries `data_quality_context` — observation count, gap
count, missing fraction, and any original data-quality warnings for its
specific window (`anomaly_service._data_quality_context`, reusing
`app.timeseries.profiling.compute_profile`). Nowhere does this system
invent a numeric "confidence: 87%" — the `data_quality_gap` synthetic
scenario (§13) exists specifically to make the point concrete: a pure
data gap can and does produce false-positive candidates from both
detectors (see the actual measured results in §"Evaluation results"
below), and the honest response is to surface the data-quality context
next to the score, not to suppress or silently downweight it with an
invented number.

## 11. Detector (ModelVersion) lifecycle

Training a detector always creates a new `ModelVersion` row —
`version = 1 + max(existing version for this org+name)`, the same
immutable-version pattern established for `FeatureDefinition`.
Retraining never overwrites a prior version in place, so an `Anomaly`
referencing an old `model_run_id` → `ModelVersion` keeps meaning exactly
what it meant when it was detected, permanently. `ModelVersionStatus`
(`TRAINED` / `FAILED` / `ARCHIVED`) and `training_error` mean a bad
reference period produces an honestly-`FAILED` detector (still `HTTP 201`
— training itself succeeded as an operation, its *outcome* failed) rather
than a `500` or a silently-broken detector — see
`test_training_with_insufficient_reference_data_reports_failed_not_500`.

## 12. Evaluation methodology — and why it's synthetic, not "accuracy"

**No labeled real fault data exists for this platform.** The original
demo dataset's injected spikes/step-changes are not annotated ground
truth (see `docs/SYNTHETIC_DATA.md`), and the real seeded dataset used in
end-to-end validation below has no maintenance records or confirmed-fault
labels at all. By design, that means no precision/
recall/accuracy claim is fabricated against real data.

What IS computed, honestly: `app/anomaly/synthetic_scenarios.py` generates
six deterministic, seeded scenarios (point anomalies, a level shift, a
variance change, gradual drift, a contextual anomaly, and a pure
data-quality gap with NO injected anomaly). `app/anomaly/evaluation.py`
splits each into a clean reference period and a scoring period, runs a
detector configured exactly like a given `ModelVersion` against it, and
matches candidates to the scenario's own code-defined ground truth
(interval overlap with a 15-minute default tolerance) to compute real
precision/recall/F1 — `POST /anomaly/detectors/{id}/evaluate`, service
function `run_synthetic_evaluation`. Every result carries `is_synthetic:
true` at every level (scenario result, aggregate, and the
`interpretation_note` field spells out the distinction in the response
itself), and this is stored on the detector (`ModelVersion.evaluation_results`)
rather than only shown once.

### Evaluation results actually observed (not illustrative — from a real run)

Running `/evaluate` against a robust z-score detector trained on real
`pump07-bearing-temp-baseline` reference data (see the live validation
in the completion report) produced:

| Scenario | Precision | Recall | Note |
|---|---|---|---|
| point_anomaly | 0.75 | 1.00 | 1 false positive alongside the 3 true spikes |
| level_shift | 1.00 | 1.00 | |
| variance_change | 0.50 | 1.00 | 1 false positive |
| gradual_drift | 1.00 | 1.00 | |
| contextual_anomaly | — | 0.00 | **missed** — confirms the documented single-baseline limitation (§1) |
| data_quality_gap | 0.00 | — (no ground truth) | 3 false positives from the gap alone |

Running the same suite against an Isolation Forest detector on the same
kind of data caught `contextual_anomaly` (recall 1.0) where robust
z-score missed it entirely — direct empirical confirmation of §1's
"Isolation Forest is less prone to this" claim — but missed
`gradual_drift` (recall 0.0) and produced 1 false positive on the pure
data-quality gap. This is exactly the kind of real, differentiated,
sometimes-uncomfortable finding a synthetic evaluation should surface:
neither detector is strictly better, and the honest answer to "which
detector should I use" is "look at what you need to catch."

**This is pipeline-correctness and relative-method validation. It is not,
and must never be read as, a claim about real-world detection accuracy on
this platform's actual industrial data** — see §"Known limitations."

## 13. Synthetic scenarios in detail

`app/anomaly/synthetic_scenarios.py`, all seeded via
`numpy.random.default_rng` (never global random state, so a given
`(scenario, seed)` pair is byte-identical every time —
`test_synthetic_scenario_generation_is_deterministic_for_a_fixed_seed`):

- `point_anomaly` — 3 isolated spikes
- `level_shift` — a sudden, sustained +12 shift
- `variance_change` — a 60-point burst of much higher variance, same mean
- `gradual_drift` — a slow linear ramp over the back two-thirds
- `contextual_anomaly` — a value typical of the daily peak occurring at
  the expected daily trough (normal in magnitude, unusual in context)
- `data_quality_gap` — a pure missing-data gap with **zero** injected
  ground truth, specifically to catch false positives caused by absence
  of data rather than a real behavior change

Every scenario keeps its injected anomaly at or after `REFERENCE_FRACTION
= 0.3` of the series, verified by
`test_synthetic_scenario_ground_truth_stays_out_of_the_reference_fraction`
— the reference/scoring split used for evaluation never accidentally
trains a baseline on the very behavior it's meant to catch.

## 14. API surface

All routes under `/api/v1/anomaly/*` (`app/api/v1/anomaly.py`),
organization-scoped via `Depends(organization_scope)`, every service call
re-validating that scope server-side:

- `POST /detectors`, `GET /detectors`, `GET /detectors/{id}` — train, list, inspect
- `POST /detectors/{id}/evaluate` — synthetic evaluation (§12)
- `POST /detectors/{id}/runs`, `GET /runs`, `GET /runs/{id}` — detection runs
- `GET /anomalies` (filter by `asset_id`/`sensor_id`/`status`/`severity_tier`/
  `start`/`end`, paginated), `GET /anomalies/{id}`, `GET /anomalies/{id}/context`
- `POST /anomalies/{id}/review`, `GET /anomalies/{id}/investigations`
- `GET /monitoring` (§18)

Training and detection-run creation and review actions require `ADMIN` or
`ENGINEER`; everything else is readable by any authenticated org member,
including `VIEWER` (`test_viewer_role_cannot_train_or_review_but_can_read`).
No route returns an internal model object — every response is the
JSON-serializable fields the Pydantic response schemas
(`app/schemas/anomaly.py`) declare.

## 15. Background jobs

Detection runs follow the identical sync/async split established
for dataset builds (`app.services.feature_service.create_dataset_build`):
an estimated row count under `MAX_DETECTION_ROWS_SYNC = 3000` runs inline
in the request; larger windows are submitted to the existing
`app.jobs.runner.job_runner` under the new `JobType.ANOMALY_DETECTION_RUN`,
handled by `app.jobs.handlers._run_anomaly_detection`, which calls the
exact same `execute_detection_run` the sync path calls — one code path,
two entry points. `ModelRun.status` (`QUEUED → RUNNING → SUCCEEDED/FAILED`)
and `error_message` reflect real outcomes; a failure leaves the run's
`error_message` populated and does not silently disappear (see the
try/except in `execute_detection_run`, which rolls back, marks the run
`FAILED` with the real exception message, and re-raises so the
`BackgroundJob` row is independently marked failed too).
Detector *training* stays synchronous — computing a reference baseline or
feature summary over a bounded reference period is fast enough not to
need the job runner, mirroring how keeps feature-definition
creation synchronous.

## 16. Frontend — Anomaly Intelligence page

`frontend/src/pages/IntelligencePage.tsx` (routed at `/intelligence`,
replacing the original "not yet available" placeholder) is a single
page covering: a live monitoring strip (trained/failed detector counts,
run outcomes, severity breakdown, "awaiting review" count — real numbers
from `GET /monitoring`, refetched every 15s); a detector table with an
inline "train new detector" form (asset → sensor selection, method —
robust z-score, Isolation Forest, or, Mahalanobis
distance, with the sensor picker switching to multi-select and a grid-
step control for both multivariate methods — reference window) and
per-detector "run detection" / "evaluate" actions
whose results render inline, including the honest per-scenario synthetic
evaluation breakdown; a filterable, paginated anomaly list (asset, sensor,
status, severity tier) with severity/status/synthetic badges; and, for a
selected anomaly, an investigation panel showing the method-appropriate
explanation (§9), data-quality context (§10), a before/during/after
temporal-context chart with the flagged period shaded (§7), a review form
constrained to the actual allowed next statuses (§5), and the full,
never-editable audit history. Every number rendered comes from a real
backend response; nothing is a static mock, and synthetic evaluation
results are labeled as such in the UI itself, not just the API.

## 17. Human-in-the-loop review workflow

Covered in detail in §5. From the frontend: selecting an anomaly shows
only the transitions actually allowed from its current status (derived
from the same `ALLOWED_TRANSITIONS` table the backend enforces, so the UI
can never offer a transition the API would reject), an optional notes
field, and, once any review has happened, the full audit trail
(`from_status → to_status`, reviewer, timestamp, notes) rendered directly
from `GET /anomalies/{id}/investigations` — never summarized or collapsed
in a way that could hide a prior state.

## 18. Detector/model monitoring — real signals only

`GET /anomaly/monitoring` (`anomaly_service.get_monitoring_summary`)
returns only things actually measurable from stored rows: run counts by
status, detector counts by status, per-detector run-count usage, anomaly
counts by severity tier and by status, total points scored across
succeeded runs, and the most recent failed runs with their real
`error_message`. It explicitly does NOT claim to track score-distribution
drift over time — the response's own `not_implemented` field states why:
this schema deliberately does not persist a per-observation score history
(see `app/models/intelligence.py`'s module docstring on why — an
unbounded per-observation score table for every 5-minute-cadence sensor
over a multi-week run would be exactly the kind of low-value unbounded
table this platform avoids). This is a real, small, honestly-scoped
monitoring foundation, not a fabricated enterprise dashboard.

## 19. Tests

172 backend tests pass as of this writing (`cd backend && pytest`): 138
carried over, plus:

- `tests/test_anomaly_detection_core.py` (22 tests) — pure-function tests
  against `app/anomaly/*` with no DB: baseline construction (including
  the too-few-observations refusal and the window-restriction check),
  robust z-score's zero-variability warning path, threshold/severity
  boundary conditions (including sign-independence), threshold-config
  validation refusals, ground-truth matching (exact overlap, tolerance
  boundary, unmatched-candidate-is-FP, unmatched-ground-truth-is-FN,
  the "no candidates and no ground truth is undefined, not perfect"
  case), and synthetic-scenario determinism/leakage checks.
- `tests/test_api_anomaly.py` (12 tests) — full HTTP-level workflows:
  the complete train → detect → investigate → review lifecycle for both
  detector types; insufficient-reference-data handling; the
  robust-z-score single-sensor constraint; a non-trained detector
  refusing a detection run (409); the retrospective-scoring guard and its
  explicit opt-in; an invalid status transition (422); cross-organization
  access returning 404; `VIEWER`-role write restrictions (403) alongside
  confirmed read access; the async job-runner path for large scoring
  windows; the synthetic evaluation endpoint; and the monitoring endpoint.

This suite deliberately spends real test cases on refusal and warning
paths (insufficient data, bad transitions, wrong role, wrong org, zero
variability) rather than only the happy path — a system that claims
honesty about its own limitations needs those limitations to be tested,
not just documented.

## 20. Performance and scalability — known, stated limits

- Detection scoring is bounded the same way the dataset builds
  are: `TooManyObservationsError` / `MAX_DETECTION_ROWS_SYNC` prevent an
  unbounded in-request computation; anything larger moves to the
  background job runner rather than blocking a request thread.
- No individual observation-level score is persisted — only points that
  cross a threshold become an `Anomaly` row, and `ModelRun.metrics` is a
  small, fixed-shape summary. This keeps the anomaly table itself bounded
  by actual detections, not by scoring volume.
- Isolation Forest re-fits from scratch on every detection run. For this
  platform's actual data volumes (thousands of feature rows) this is
  sub-second; it would NOT scale to a reference period large enough to
  produce hundreds of thousands of feature rows without either caching a
  fitted model (reintroducing the pickling tradeoff explicitly avoided
  here) or moving training itself onto the job runner. This is a stated,
  known limit, not a hidden one.
- `app.services.anomaly_service.get_monitoring_summary` runs a handful of
  `GROUP BY` aggregate queries per call — fine at this platform's current
  row counts, but would need pagination or time-windowing if a single
  organization accumulated a very large number of runs.
- The `in-process` job runner (the choice, reused unchanged here)
  means job state lives only in this process's database and does not
  survive a process restart mid-job — documented already in
  `app/jobs/runner.py` and unchanged.

## 21. Known limitations (stated plainly)

- **No real accuracy claim exists or is implied anywhere in this system.**
  The only evaluation numbers this platform produces are against
  synthetic, seeded, code-defined ground truth. See §12.
- Robust z-score's single global baseline does not adapt to daily/
  seasonal cycles — empirically confirmed by its `contextual_anomaly`
  miss in every evaluation run.
- Isolation Forest's per-feature "contributing features" explanation is
  an approximation (reference-period z-scores of each feature), not
  SHAP-quality attribution — stated in the module docstring and in every
  explanation payload's method description.
- Multi-sensor detection surfaces association/co-occurrence only — no
  causal claim is made or computable from what's implemented.
- Score-distribution drift over time is not tracked (§18) — would require
  persisting per-observation scores, deliberately not done here.
- Detector training does not currently run on the background job runner
  (only detection runs do) — acceptable at today's reference-period sizes,
  stated as a scale limit rather than hidden.
- The Mahalanobis detector assumes the sensors' joint
  relationship is approximately stable and elliptical, is bounded to 6
  sensors (a covariance fit on more dimensions than this platform's
  realistic reference window sizes support is not reliable), and — like
  every other claim in §21 — has no real-world accuracy evidence, only
  the synthetic pipeline-correctness evidence in §22.

## 22. Addendum — Mahalanobis detector, actually executed evidence

Everything in §1a is backed by tests that were actually run, not just
written:

- `tests/test_mahalanobis_detector.py` — **14 passed**: reference-data
  validation (too-few-rows, the dimensionality rule binding independently
  of the flat floor, too-many-sensors), detection correctness (flags
  nothing on a genuinely well-behaved relationship — a regression guard
  against a real bug this module's own development hit and fixed, see
  below — merges consecutive flagged points into one event, is
  deterministic across repeated calls, handles near-collinear sensors
  without crashing, reports "no scoring data" honestly rather than
  silently returning nothing), and the core capability claim itself
  (`test_mahalanobis_catches_what_univariate_zscore_cannot`:
  `precision=1.0, recall=1.0` for Mahalanobis vs. `recall=0.0` for
  robust z-score on each sensor alone, against the same injected
  scenario).
- `tests/test_api_anomaly.py::test_mahalanobis_multivariate_e2e` and
  `::test_mahalanobis_requires_at_least_two_sensors` — the same
  capability proven through the real HTTP API and real DB, not just pure
  functions: training, scoring, and the resulting `Anomaly` row's
  `detector_type`, `contributing_signals.individually_unremarkable`, and
  `contributing_signals.caveat` fields, plus the ≥2-sensor refusal.
- `tests/test_api_mlops.py::test_mahalanobis_component_registers_under_
  trained_ml_model_and_retrains` and `::test_robust_zscore_still_
  rejected_under_trained_ml_model` — the registry-layer change this
  addition required (see `docs/MLOPS_GOVERNANCE.md` §24): Mahalanobis
  shares the `TRAINED_ML_MODEL` component type with Isolation Forest, and
  retraining correctly infers which detector type to retrain as from the
  component's own history.
- Full backend suite: **512 passed**, zero failures, zero skips —
  confirming the new `DetectorType.MAHALANOBIS` enum value and its three
  new dispatch branches in `anomaly_service.py` broke nothing that
  enumerates or assumes a fixed set of detector types.
- Frontend: `npx tsc -b` — zero TypeScript errors after adding
  `"mahalanobis"` to the `DetectorType` union and wiring the training
  form, sensor-selection copy, and three detector-type label call sites
  in `IntelligencePage.tsx` through one shared `detectorTypeLabel` /
  `isMultiSensorDetector` helper pair rather than three more hardcoded
  ternaries.

**A bug this module's own development found and fixed, documented here
because it is exactly the kind of mistake a less careful synthetic
scenario would hide:** the first version of
`app.anomaly.multivariate_synthetic_scenarios.build_correlation_
breakdown_scenario` generated `sensor_a` as a near-flat random walk,
which made the intended "two sensors normally move together" reference
relationship almost nonexistent (correlation ≈0.16) — so nearly every
scoring point got flagged, breakdown or not, and the first draft of
`test_detect_flags_nothing_on_a_well_behaved_relationship` would have
passed for the wrong reason had it existed yet. Found by directly
computing the reference-period correlation rather than trusting the
scenario's docstring, fixed by giving `sensor_a` a real daily-cycle
signal, and locked in permanently by that regression test.

## What this is not

This is **not** predictive maintenance. `app.models.intelligence.Prediction`
remains an empty, unused table — deliberately not repurposed for anomaly
scores, so the two concepts never get conflated in the schema — and
`GET /api/v1/intelligence/predictions` still honestly returns
`501 not_yet_available`. Forecasting a future failure or estimating
remaining useful life needs validated labels and a methodology this layer
does not have and does not fabricate; that is explicitly reserved for a
later work, contingent on real label/methodology validation, not on
simply extending this anomaly-scoring layer.
