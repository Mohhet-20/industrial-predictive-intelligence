# Equipment Condition Intelligence & Forecasting — What This Layer Does and Doesn't Do

Status: **Phase 4 — Predictive Risk, Equipment Condition Intelligence &
Evidence-Based Forecasting.** This describes what is actually implemented
and tested today, built on top of Phase 3's anomaly detection layer (see
[ANOMALY_DETECTION.md](./ANOMALY_DETECTION.md)) without modifying it.

**Core principle, carried forward and sharpened from Phase 3's
ANOMALY ≠ FAILURE: this platform distinguishes ANOMALY (Phase 3: a
detector-flagged unusual period), CONDITION SIGNAL (this phase: trend/
deviation/variance/anomaly-persistence evidence about a sensor's recent
behavior), RISK ESTIMATE (this phase: a transparent, non-probabilistic
priority ordering — never a fabricated likelihood), and FAILURE PREDICTION
(NOT implemented — see §0). These four words are never used
interchangeably anywhere in this codebase, its API, or its UI.**

## 0. The evidence audit that determined what could be built

Phase 4's own instructions require this audit to happen fresh, against
the real repository, before writing any modeling code — not from memory
of what earlier phases assumed. It was re-run at the start of this
phase's work. Findings:

- **`MaintenanceEvent`** (`app.models.intelligence.MaintenanceEvent`):
  exists in the schema, described in its own docstring as "PLANNED — no
  ingestion workflow exists yet." Re-confirmed: **zero rows**, no API
  endpoint creates or reads one, no ingestion workflow of any kind.
- **The old `Prediction` table**: existed in the Phase-3 schema,
  explicitly reserved for this phase's work, and had **zero rows** in
  every environment this schema has run in. It has been dropped and
  replaced by `ForecastModel`/`ForecastRun` (see §3) — see the migration
  and `app.models.condition`'s module docstring.
- **The real seeded dataset** (`backend/data/pred_platform.db`): 47,489
  real (synthetically-generated but genuinely stored and queried)
  measurements across 9 sensors on 4 assets, spanning ~21 days. No
  sensor's row carries a failure label, a fault flag, or any outcome
  field. `app.synthetic.generator` (Phase 1) injects spikes, gaps, and
  one slow linear drift (`bearing_temp_de` on `PUMP-07`,
  `drift_per_day=0.05`) for demo realism — but these are unlabeled: there
  is no ground-truth timestamp anywhere saying "this is when the bearing
  actually degraded," only the raw sensor values themselves.
- **Phase 3's `Anomaly`/`AnomalyInvestigation` tables**: real, but they
  record *statistical unusualness*, not *failure outcomes*. An anomaly's
  `status` (resolved/false_positive/etc.) is a human's review judgment
  about the anomaly itself, never a confirmed link to an equipment
  failure — there is no field anywhere that says "and this turned out to
  be a real fault."
- **Asset/Sensor metadata**: `asset_type`, `location`, `expected_min/max`,
  `expected_interval_seconds` — operational metadata, not condition or
  outcome data.

**Conclusion — which path applies (of Phase 4's Paths A/B/C/D):**

| Path | Requires | Supported here? |
|---|---|---|
| A — verified failure/outcome labels | Real records of what actually failed and when | **No.** Nothing in this schema or dataset records a failure outcome. |
| B — maintenance history (weak labels) | `MaintenanceEvent` rows correlating to sensor behavior | **No.** Zero rows, no ingestion workflow. |
| C — unlabeled sensor data only | Real time-series, no outcome labels | **Yes.** This is what actually exists: 47,489 real measurements, no labels. |
| D — synthetic labels | Code-injected, deterministically-seeded events, for pipeline validation only | **Available, and used** — exactly like Phase 3's `synthetic_scenarios` — but never as a real-world accuracy claim. |

**Path C is the primary and only path this phase builds real
functionality against.** Supervised predictive modeling (Phase 4 §5) is
explicitly **NOT implemented** — see §15. Every capability below is built
to work honestly with unlabeled sensor data alone: trend/deviation/
variance/persistence evidence (no labels needed), a conservative
three-stage degradation classification (no labels needed, deliberately
requires a human for its final stage — see §2), and a forecasting
capability evaluated against each sensor's own held-out future values
(self-supervised — no failure label needed to score it honestly, see §3).

## 1. Equipment Condition Intelligence — evidence, not a verdict

`app/condition/evidence.py`, exposed at
`GET /condition/sensors/{id}/evidence`. Five independent evidence types,
computed fresh on every call (stateless, never persisted — the same
pattern as Phase 2's `GET /timeseries/sensors/{id}/profile`):

- **Trend evidence** — reuses `app.timeseries.profiling.compute_profile`'s
  OLS trend line unmodified (same `p < 0.05`, `r² ≥ 0.1` significance
  gate documented in `docs/TEMPORAL_INTELLIGENCE.md`).
- **Persistent anomaly evidence** — real counts and timing of
  detector-flagged `Anomaly` rows (Phase 3) for this sensor in the
  window, including whether their frequency is rising within the window.
  Never re-scored here — this is genuine detection history.
- **Deviation from baseline** — the analysis window's median compared, in
  MAD-equivalent units, against a `app.anomaly.baseline`-built reference
  (the exact same robust median/MAD statistics the anomaly detector
  uses). Warns explicitly (`reference_overlaps_window`) if the caller
  supplies a reference period that isn't independent of the window —
  this endpoint is exploratory and doesn't reject that the way §2's
  persisted analysis does, but it never silently presents an overlapping
  comparison as if it were a clean one.
- **Variance change evidence** — reuses `app.timeseries.change_detection`'s
  variance-shift statistic unmodified.
- **Cross-sensor association** — Pearson correlation with sibling sensors
  on the same asset, reusing `app.timeseries.multi_sensor` and its
  standing association-not-causation caveat.

**Why there is no composite "health score":** blending a p-value, a MAD
ratio, a variance ratio, a correlation coefficient, and an anomaly count
into one number requires weights this platform has no validated basis for
choosing. `composite_score` is `null` in every response, always
accompanied by `composite_score_note` explaining why. A human reviewer
combines the five evidence types using engineering judgment this platform
does not have.

## 2. Degradation & long-term change analysis — the three-way distinction

`app/condition/degradation.py`, persisted via `ConditionRun` →
`DegradationSignal` (`POST /condition/runs`, mirrors
`app.services.anomaly_service.create_detection_run`'s structure).

- **`observed_trend`** — a statistically significant OLS trend exists
  *somewhere* in the analysis window. Descriptive only; many observed
  trends are transient or seasonal.
- **`degradation_candidate`** — the trend **persists**: the analysis
  window is split into 3 equal, non-overlapping sub-windows (via
  `app.timeseries.splitting.chronological_split` with equal fractions —
  the same leakage-verified boundary machinery from Phase 2, repurposed
  here to get independent sub-windows), and at least 2 of 3 must agree on
  a significant trend direction. AND it is **corroborated** by at least
  one independent statistic: deviation from baseline ≥ 2.0 MAD-equivalent
  units, or a rising anomaly-frequency trend within the window. Two
  independent statistics agreeing is real evidence; one fitted line over
  one window is not.
- **`validated_degradation`** — reachable **only** through
  `POST /condition/signals/{id}/validate`, an explicit human action
  requiring a non-empty justification note. Nothing in
  `classify_degradation` can produce this stage — enforced structurally
  (the function's return type is one of `None`/`observed_trend`/
  `degradation_candidate`; `tests/test_condition_core.py::
  test_classify_degradation_never_reaches_validated_automatically` checks
  this directly, including with evidence engineered to be maximally
  favorable). This platform has no maintenance-outcome data (§0) to
  confirm a candidate against — auto-promoting to "validated" would be
  exactly the unearned confidence Phase 4 forbids.

**Live validation against the real seeded dataset:** running this
analysis against `PUMP-07`'s `bearing_temp_de` sensor (the one sensor
`app.synthetic.generator` deliberately gives a slow wear-like drift) over
its full ~18-day post-reference window returns `signal_produced: false`
— the drift (0.05°C/day against a 3°C daily swing and realistic noise) is
real but does not clear the `r² ≥ 0.1` significance bar this platform
requires before calling anything a trend. This is the correct, honest
result: it demonstrates the classifier does not manufacture a signal just
because a sensor was *designed* to drift, and it is why
`tests/test_api_condition.py` uses an *engineered* (stronger, lower-noise,
no-seasonality) synthetic series to exercise the `degradation_candidate`
path — the real fleet's actual drift is genuinely too weak, at this data
volume, to clear this platform's own bar.

## 3. Forecasting — implemented only where a backtest actually justifies it

`app/condition/forecasting.py` + `ForecastModel`/`ForecastRun`
(`POST /condition/forecast-models`, `POST /condition/forecast-models/{id}/runs`).

This is deliberately **not** failure prediction or remaining-useful-life
estimation — it is a narrow, short-horizon extrapolation of a sensor's own
statistical behavior, self-supervised against its own held-out future
values (no failure label needed to score it honestly), never surfaced as
a maintenance recommendation.

**Fitting always backtests** (never optional, unlike Phase 3's detector
evaluation, because the result *gates* whether a forecast run is even
allowed):

- Fit on `[fit_start, fit_end)` only.
- Evaluate every method against every **real** observation strictly after
  `fit_end` up to `test_end` — never an interpolated or invented point.
  `tests/test_condition_core.py::test_backtest_fit_is_unaffected_by_future_corruption`
  proves this structurally: corrupting every value after `fit_end` to
  `1e9` does not change the fit at all.
- **Baselines** (always computed, always reported): `naive_last_value`
  (repeat the last fit-window value) and `seasonal_naive` (the value
  exactly one day earlier, if it falls within the fit window — this
  fleet has real daily seasonality, see `app.synthetic.generator`).
- **Candidate**: `linear_trend` — the same OLS method
  `app.timeseries.profiling` uses, extrapolated forward, with an
  uncertainty band from the in-sample residual standard deviation
  (`PREDICTION_INTERVAL_Z = 1.645`, explicitly labeled a rough spread, not
  a calibrated confidence interval).
- **Justification gate**: `linear_trend` must beat **every** baseline's
  MAE by ≥ 10% (`MIN_IMPROVEMENT_FRACTION`), with ≥ 40 fit observations
  and ≥ 10 evaluable held-out points. `ForecastModel.status` is
  `justified`, `not_justified`, or `failed` (insufficient data) —
  `not_justified` is a valid, honest, and — on this platform's real data
  — the *typical* outcome, not an error.

**Live validation against the real seeded dataset:** fitting against
`bearing_temp_de`'s real 21 days (14-day fit / 7-day held-out test)
returns `not_justified`: linear_trend MAE 1.958 vs. naive_last_value MAE
1.945 (a **−0.6%** "improvement," i.e. slightly worse). The sensor's
strong daily cycle dominates its weak drift, so a straight line loses to
simple persistence — exactly the honest negative result §4 requires
rather than a claim this platform cannot support. The positive path (a
model that *does* get `justified` and can run) is exercised in
`tests/test_api_condition.py` against a synthetic near-noiseless linear
ramp with no seasonality — the one shape where `linear_trend` should
legitimately win, proving the gate can say yes as well as no.

`POST .../runs` refuses with `409 conflict` against any non-`justified`
model — never silently produces numbers a backtest didn't support.

## 4. Provenance

Every `ConditionRun`, `DegradationSignal`, `ForecastModel`, and
`ForecastRun` row is written once and never overwritten (mirrors Phase
3's `ModelVersion`/`ModelRun`/`Anomaly` convention exactly). A
`DegradationSignal`'s `evidence` JSON — full sub-window trends, deviation,
anomaly persistence, variance evidence, and classification rationale — is
frozen at creation; only `status` and (via the dedicated validate action)
`stage` ever change afterward, both through the append-only
`DegradationReview` audit log. A `ForecastModel`'s `evaluation_results`
(the full backtest) is permanent; retraining creates a new version, never
an edit.

## 5. Explainability

Every evidence/classification payload distinguishes: **observed
evidence** (raw statistics — slope, p-value, MAD deviation, variance
ratio, anomaly counts — computed once and never restated differently),
**model output** (the OLS fit's parameters, the backtest MAE/RMSE
comparison), and **human interpretation** (`DegradationReview.notes`,
mandatory and freeform on validation). No natural-language explanation is
generated by a language model anywhere in this layer — every
`rationale`/`justification_note` string is built by directly interpolating
the real numbers that produced the classification, the same discipline
`app.anomaly.scoring` uses for anomaly explanations.

## 6. Honest uncertainty communication

No arbitrary confidence percentage appears anywhere in this layer. Forecast
uncertainty is a residual-standard-deviation-derived band, explicitly
labeled "a rough, honestly-labeled spread, not a calibrated confidence
interval" in `app.condition.forecasting`'s own module docstring, the API
schema, and the frontend caption under every forecast chart.

## 7. Risk prioritization — ordering, not probability

`GET /condition/attention-list` combines open `DegradationSignal`s and
open `Anomaly`s into one list, sorted by an explicit rank tuple
(`stage_rank`, `severity_rank`, `period_start`) that is returned **in
every item**, not hidden inside a single fabricated score. No probability
or likelihood-of-failure number is computed anywhere in this platform —
`sort_methodology` in the response says so explicitly, and
`tests/test_api_condition.py::test_attention_list_never_returns_fabricated_score`
asserts no `score`/`probability` key ever appears on an item.

## 8. Lifecycle & human review

`DegradationSignal.status` follows the exact same five-state lifecycle as
Phase 3's `Anomaly.status` (`detected → under_review → acknowledged →
resolved / false_positive`, with reopening allowed), enforced by
`CONDITION_ALLOWED_TRANSITIONS` and recorded append-only in
`DegradationReview` — see §2 for why `stage` promotion to
`validated_degradation` is a separate, mandatory-justification action.
`ForecastModel`/`ForecastRun` have no review lifecycle of their own (they
are read-only evidence outputs of a versioned, honesty-gated fit, the same
relationship `ModelVersion`/`ModelRun`/`Anomaly` has in Phase 3) — a
forecast a reviewer disagrees with is addressed by fitting a new model
version, never by editing history.

## 9. API surface (`/api/v1/condition/*`)

All organization-scoped, all bounded, all behind the same bearer-token
auth as the rest of the API. Condition-run creation, forecast fitting/
running, and review/validate actions require ADMIN or ENGINEER; reads are
open to any authenticated org member (VIEWER included).

| Endpoint | Purpose |
|---|---|
| `GET /sensors/{id}/evidence` | §1 — stateless, no composite score |
| `POST /runs`, `GET /runs`, `GET /runs/{id}` | §2 — degradation analysis execution |
| `GET /signals`, `GET /signals/{id}` | §2 — filterable degradation signals |
| `POST /signals/{id}/review` | §8 — status transition |
| `POST /signals/{id}/validate` | §2/§8 — the only path to `validated_degradation` |
| `GET /signals/{id}/reviews` | §8 — append-only audit log |
| `POST /forecast-models`, `GET /forecast-models`, `GET /forecast-models/{id}` | §3 — fit + mandatory backtest |
| `POST /forecast-models/{id}/runs`, `GET /forecast-runs`, `GET /forecast-runs/{id}` | §3 — gated forecast generation |
| `GET /attention-list` | §7 |
| `GET /monitoring` | §12 |

All computation here is **synchronous** — see
`app.services.condition_service`'s module docstring for why no
background-job split (unlike Phase 3's detection runs) is needed: every
operation is a closed-form, single-sensor, bounded-window computation.

## 10. Frontend — Condition Intelligence page

`frontend/src/pages/ConditionPage.tsx` (`/condition`, nav label
"Condition Intelligence" — deliberately not "AI Health Score" or similar):
a monitoring strip of real counts, a transparent attention list (rank
components visible per item, never a score), a sensor-analysis form that
drives both degradation runs and forecast fitting, a degradation-signal
list with full sub-window-trend evidence and a review/validate panel
(validation blocked client-side until a justification is typed, mirroring
the backend's own requirement), and a forecasting panel whose model table
shows the actual backtest MAE comparison per model and whose chart caption
states the uncertainty band is not a calibrated probability. `not_justified`
models are visibly present in the table (not hidden) but their "Select"
action is disabled, matching the backend's 409 refusal.

## 11. Tests

`tests/test_condition_core.py` (18 tests, no database): leakage-freeness
of the backtest fit (proven by future-corruption, not just asserted),
non-overlapping/chronologically-ordered sub-windows, the
never-auto-validated structural guarantee, both a case where a forecast
*is* legitimately justified (near-noiseless ramp) and one where it
honestly is not (this fleet's realistic daily-cycle shape), bounded/capped
forecast point generation, and the reference-overlap warning.
`tests/test_api_condition.py` (14 tests, real DB + HTTP): a full
run → signal → review → validate lifecycle, mandatory-notes and
wrong-stage refusal on validation, viewer-role write refusal, cross-org
isolation, `not_justified` correctly blocking a forecast run with `409`,
a `justified` model producing bounded/uncertainty-labeled points, honest
`failed` status on insufficient data, and a monkeypatch-based regression
test proving a mid-computation exception in either a condition run or a
forecast run is recorded as `status=failed` with a real error message
rather than crashing (this test was written specifically because writing
it caught a real bug — see §15).

## 12. Monitoring

`GET /condition/monitoring` (`app.services.condition_service.
get_condition_monitoring_summary`) reports real counts only: degradation
signals by stage/status, condition/forecast-model run outcomes by status
— and an explicit `not_implemented` list naming supervised failure
prediction, the same "real signals only, no fabricated drift metric"
discipline as Phase 3's `/anomaly/monitoring`.

## 13. Performance and scalability

Every computation loads one sensor's bounded time range via
`app.timeseries.data_access.load_measurement_series` (the same
`MAX_ROWS = 200,000` / `TooManyObservationsError` guard as everywhere
else in this codebase) and runs O(n) or O(n log n) closed-form statistics
— no iterative model training. Forecast point generation is capped at
`MAX_FORECAST_POINTS = 200`. Attention-list queries are capped at 500 open
rows per type before ranking and truncating to the requested `limit`.

## 14. Known limitations, stated plainly

- **No failure prediction of any kind.** Supervised predictive modeling
  (Phase 4 §5) is not implemented because Path A/B are not supported by
  this platform's data (§0) — this is a data limitation, not an
  engineering shortcut, and it should not be worked around by inventing
  labels.
- **The forecasting capability is narrow by design**: single-sensor,
  short-horizon, linear-trend-only. It is evaluated honestly and, on this
  platform's actual real data, is usually `not_justified` — that is the
  correct behavior, not a weakness to fix by lowering the bar.
- **Degradation classification needs real trend signal to fire.** A slow,
  genuine wear pattern smaller than a sensor's noise/seasonality (as with
  this fleet's actual `bearing_temp_de` drift, §2) will correctly produce
  no signal rather than a false one — meaning real early-stage wear can
  go unflagged by this method alone. This is the same conservative
  trade-off Phase 3 makes for anomaly detection thresholds.
- **`validated_degradation` requires a human every time.** There is no
  mechanism, and none is planned, for this platform to self-confirm a
  degradation candidate — it has no maintenance-outcome data to confirm
  against (§0).
- **A real bug was found and fixed while building this layer** (not just
  designed around): `create_condition_run` and `create_forecast_run`
  originally `db.flush()`ed their placeholder row before the analysis
  try/except block instead of committing it. On a genuine mid-computation
  failure, the `except` clause's `db.rollback()` would have discarded that
  flushed row entirely, and the subsequent re-fetch-by-id would return
  `None`, turning an honest `status=failed` outcome into an unhandled
  `AttributeError` that masked the real error. Fixed by committing the
  placeholder row before entering the try block (mirroring
  `app.services.anomaly_service.create_detection_run`'s existing pattern).
  Caught and permanently regression-tested via a monkeypatch-forced
  exception (§11) — reverting the fix and re-running that one test
  reproduces the original `AttributeError` exactly, confirming the test
  actually exercises the bug rather than passing vacuously.

## What this is not

- **Not failure prediction, not remaining-useful-life estimation, not
  predictive maintenance in the industry-standard sense.** Every
  response, docstring, and UI label in this layer says "condition
  signal," "degradation candidate," or "forecast," never "failure risk"
  or "RUL."
- **Not a health score.** `composite_score` is `null` everywhere it could
  appear, by design (§1).
- **Not a probability.** The attention list is an explicit, inspectable
  ordering (§7), never a likelihood.
- **Not self-validating.** `validated_degradation` requires a human,
  always (§2, §14).
