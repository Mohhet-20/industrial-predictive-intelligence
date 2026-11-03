# Scenario Simulation & What-If Analysis (Phase 11)

**THIS IS SCENARIO ANALYSIS, NOT A PERFECT PHYSICAL DIGITAL TWIN.**

A scenario in this platform is a bounded, reproducible, non-destructive
hypothetical: a declared arithmetic modification (a level shift, a ramp, a
scaled variability, a bounded spike, an accumulated drift, or several of
these applied independently across sensors) applied to a **copy** of real
historical sensor data, then run through the same real analytical pipeline
this platform runs on real data. Nothing here claims to model bearing wear,
motor load, thermal physics, or any other real mechanism. Nothing here is a
prediction of what will actually happen. Every result surfaced anywhere in
this platform — API response, frontend page, investigation evidence link,
copilot answer — is labeled `SIMULATED SCENARIO RESULT` and is never
persisted into a real `Anomaly`, `DegradationSignal`, or `ForecastRun` row.

This document is the counterpart to `docs/SCIENTIFIC_VALIDATION.md` for this
one subsystem: it states what was built, exactly what was reused vs. newly
written, and — per Phase 11 §20 — how the simulation engine's own
correctness was actually tested (not the real detectors it calls, which are
already evaluated in `docs/SCIENTIFIC_VALIDATION.md` and
`docs/ANOMALY_DETECTION.md`/`docs/CONDITION_INTELLIGENCE.md`).

---

## 1. Scenario types

| Type | What it does | Magnitude means | Duration |
|---|---|---|---|
| `level_shift` | Adds a constant to every observation in the window | a fixed amount, sensor units | optional — unset = "to end of baseline window" |
| `trend_change` | Adds a linear ramp on top of real values | a **rate**, units/hour | optional |
| `variability_change` | Rescales each point's deviation from a rolling median | a multiplier (2.0 = twice as noisy, 0.5 = half) | optional |
| `temporary_spike` | Adds a bounded rectangular or triangular bump | peak height, sensor units | **required** — a spike with no end isn't temporary |
| `drift` | Accumulates a fixed **total** amount by window end | a fixed total reached at window end (not a rate — see below) | optional |
| `missing_data_gap` | Drops observations (sets them NaN) instead of changing them | a **drop fraction** in `[0, 1]` — 1.0 (default) drops the whole window | **required** — an unbounded gap isn't a bounded "interval" |
| `multi_sensor` | Runs any of the above, independently, per sensor | per sub-config | per sub-config |

`trend_change` vs. `drift` is a deliberate, disclosed distinction: `magnitude`
means "rate per hour" for one and "total amount reached by the end of this
window" for the other. Conflating them would silently change what a
configured scenario means depending on the analyst's assumption, so the two
are kept as separate types with separate docstrings rather than one type
with an ambiguous flag.

`missing_data_gap` (Phase 15 Phase H — "missing-data interval") is a
deliberate exception to "`magnitude` is a sensor-units value": it is the one
type where `magnitude` means a `[0, 1]` *fraction of the in-window
observations to drop*, never a sensor-units amount. `app.simulation.
validation` special-cases it explicitly — bypassing the generic
robust-std/range magnitude bound (which assumes sensor units and would
either wrongly reject a valid `0.5` or wrongly accept an invalid `5.0`) in
favor of a dedicated `[0, 1]` range check — rather than silently reusing
logic built for a different kind of value. When the fraction is below 1.0,
the points actually dropped are chosen by **deterministic, evenly-spaced
rank** within the window (`numpy.linspace` over position indices), never a
random sample — keeping this type exactly as reproducible as the other
five (§3). `app.simulation.pipeline.run_series_through_pipeline` drops the
resulting NaN rows once, at the single choke point every reused analytical
function flows through, before calling any of them — because a real missing
interval never reaches production code as a placeholder value, only as
fewer rows, and the simulated side must look the same way to be a fair
comparison. The honest effect of the gap is then visible directly in the
comparison output's `observation_count` (baseline vs. simulated), not
hidden inside a value distortion.

## 2. The one invariant every perturbation obeys

`app.simulation.perturbations` implements all seven types as **pure,
deterministic functions**: `series in → new series out`, no randomness, no
mutation of the input. Every one of them guarantees a single rule with no
per-type exceptions:

> Every timestamp OUTSIDE `[modification_start, modification_end]` is
> numerically identical to the original series at that timestamp, where
> `modification_end = modification_start + modification_duration_seconds`
> if a duration was given, else the last timestamp in the series.

This was not the first implementation. `trend_change` and `drift` originally
"held" their accumulated offset past the end of a bounded window (the
intuition being "a permanent trend change stays changed forever"), which
directly contradicted the "reverts outside its window" guarantee documented
alongside it and failed the non-destructive verification check the moment
both were exercised together. The fix was to remove all "hold" behavior and
make every type — including `drift`, which previously required a duration —
revert cleanly outside its own window, with "no duration" simply meaning
"the window runs to the end of the baseline period" rather than "changes
persist unboundedly." One universal rule, no type-specific carve-outs, is
what makes the non-destructive guarantee something a single shared check
(`verify_outside_window_unchanged`) can enforce for all seven types at once —
see §5.

## 3. Reproducibility is free, not engineered

Every perturbation is a pure function of `(real historical series, config)`
with **no seed anywhere** in this subsystem, because there is nothing
stochastic to seed. The same scenario, run twice, against the same
underlying real data, produces byte-identical output on any machine. This is
verified directly, not assumed — see §5.1.

## 4. What gets reused from the real platform, and what's new

`app.simulation.pipeline.run_series_through_pipeline` is the single choke
point that decides which series (real baseline or its perturbed copy) is
handed to which existing function. Every one of these is called
**unmodified**:

- `app.anomaly.baseline.compute_baseline` + `app.anomaly.robust_zscore.detect`
- `app.condition.evidence.compute_condition_evidence` + `app.condition.degradation.classify_degradation`
- `app.timeseries.change_detection.detect_change_candidates`
- `app.condition.forecasting.backtest_and_compare` (opt-in, since it roughly
  doubles the compute cost of a run)

**Documented scope boundary — not reused:** `app.anomaly.isolation_forest_detector`.
Isolation Forest is trained against a real, already-persisted `ModelVersion`'s
multivariate reference data. There is no honest way to substitute one
perturbed sensor's series into that multivariate feature vector without
either silently changing what the trained model represents, or fabricating
how sibling sensors would respond to a hypothesis this platform has no basis
for — which is exactly the kind of invented sensor relationship Phase 11's
governing constraints forbid. This is stated in `app.simulation.pipeline`'s
module docstring and in `PIPELINE_FUNCTION_PROVENANCE`, which is persisted
onto every `ScenarioRun.pipeline_versions` so it is inspectable per-run, not
just in source.

**New code, and only new code, in this subsystem:** the six perturbation
functions, scenario/run domain model and persistence, pre-flight validation,
comparison/sensitivity summarization, the REST API, and the frontend. No
analytical algorithm was reimplemented, approximated, or forked — see
`PIPELINE_FUNCTION_PROVENANCE` in `app/simulation/pipeline.py` for the exact
mapping persisted with every run.

## 5. Scientific validation of the simulation engine itself (§20)

Phase 11 §20 asks for validation of the *simulation system*, distinct from
the real detectors it calls (already evaluated elsewhere). That means: given
a known, hand-computable input and a declared perturbation, does the engine
produce the *exact* expected output — not "a plausible-looking one." All 74
tests below run against `tests/test_simulation_perturbations.py`,
`tests/test_simulation_validation.py`, `tests/test_simulation_comparison.py`,
`tests/test_api_scenario.py`, and `tests/test_scenario_integration.py`, and
all 74 pass as of this writing:

```
$ pytest tests/test_simulation_perturbations.py tests/test_simulation_validation.py \
         tests/test_simulation_comparison.py tests/test_api_scenario.py \
         tests/test_scenario_integration.py -q
........................................................................ [ 97%]
..                                                                       [100%]
74 passed in 15.71s
```

### 5.1 Exact-value determinism (35 tests, `test_simulation_perturbations.py`)

These are deterministic, hand-computed-expected-value tests — the closest
equivalent here to Phase 10's "known ground truth" discipline — not
statistical approximations:

- **`TestLevelShift`** (5 tests): a level shift adds the exact configured
  constant within its window (`test_shifts_by_exactly_the_configured_amount`);
  points strictly before `modification_start` are byte-identical to the input
  (`test_unshifted_before_start_is_byte_identical`); a bounded window reverts
  exactly at its end (`test_bounded_duration_reverts_after_window`); the
  input `Series` object is never mutated in place
  (`test_does_not_mutate_input_in_place`); `magnitude=0` is a true no-op.
- **`TestDrift`** (4 tests): drift reaches its exact configured total at
  `modification_end`, to floating-point tolerance
  (`test_reaches_exact_magnitude_at_window_end`); the accumulation is
  linear within the window, verified at multiple intermediate points, not
  just the endpoint (`test_is_linear_within_the_window`); a bounded drift
  reverts after its window (`test_reverts_after_bounded_window`); an
  unbounded drift (no duration given) ramps smoothly to the end of the
  series rather than raising (`test_no_duration_ramps_to_end_of_series` —
  this is the case that was previously broken; see §2).
- **`TestTemporarySpike`** (4 tests): a spike affects only its declared
  window and nothing outside it
  (`test_affects_only_its_declared_window`); the triangular shape is
  verified to actually peak at the midpoint and return to exactly zero
  added value at both edges
  (`test_triangular_shape_peaks_at_midpoint_and_returns_to_zero_at_edges`);
  a missing/zero duration is rejected rather than silently defaulted
  (`test_requires_positive_duration`); an unknown `shape` string is rejected
  (`test_rejects_unknown_shape`).
- **`TestTrendChange`** (2 tests): the added ramp matches the configured
  hourly rate exactly at multiple sampled points, not just checked for
  "positive slope" (`test_adds_a_linear_ramp_at_the_configured_hourly_rate`);
  a bounded ramp reverts after its window.
- **`TestVariabilityChange`** (4 tests): `magnitude=0` collapses every point
  in the window exactly onto the rolling median
  (`test_zero_magnitude_collapses_to_rolling_median` — i.e. "zero noise" is
  verified to genuinely mean zero deviation, not approximately so);
  `magnitude=1` is proven to be a true no-op relative to the input;
  `magnitude=2` is verified to exactly double each point's deviation from
  the rolling median (`test_magnitude_two_doubles_deviation_from_rolling_median`);
  a negative magnitude is rejected (`test_rejects_negative_magnitude` — a
  variability multiplier cannot invert a deviation's sign without
  fabricating a physical meaning the platform doesn't have).
- **`TestMissingDataGap`** (7 tests, Phase 15): a full gap (`magnitude=1.0`)
  sets every in-window point to NaN
  (`test_full_gap_sets_every_in_window_point_to_nan`); `magnitude=None`
  (what validation alone permits for this type) behaves identically to an
  explicit `1.0` (`test_default_magnitude_is_a_full_gap`); every point
  strictly outside the window is byte-identical, `NaN`-safe comparison
  included (`test_outside_window_is_byte_identical_including_nan_safe_comparison`);
  a `0.5` fraction drops *some but not all* in-window points, and doing so
  twice with identical inputs drops the exact same points — proving the
  "deterministic rank-based selection, never random" claim, not just
  asserting it (`test_partial_fraction_drops_only_that_share_deterministically`);
  `magnitude=0` is a true no-op; a missing/non-positive duration is
  rejected (`test_requires_positive_duration` — an unbounded gap isn't a
  bounded "interval"); a fraction outside `[0, 1]` is rejected
  (`test_rejects_magnitude_outside_unit_interval`); the input `Series` is
  never mutated in place.
- **`TestDispatchAndInvariants`** (7 tests): all six single-sensor types
  dispatch correctly through the shared `apply_perturbation()` entry point
  and preserve the exact original timestamp grid — no resampling, no
  interpolation, no added/dropped points
  (`test_apply_perturbation_dispatches_and_preserves_timestamps`,
  parameterized across all 6 types, each given a magnitude valid for its
  own semantics — `missing_data_gap`'s `[0, 1]` fraction is not the
  sensor-units `3.0` the other five receive); an unknown `scenario_type` string is
  rejected rather than silently ignored; and — the single most
  safety-critical test in this subsystem —
  `test_verify_outside_window_unchanged_detects_a_violation` proves the
  non-destructive-guarantee checker itself is not a rubber stamp: it is
  fed a deliberately-corrupted series with one point changed outside its
  declared window and confirmed to catch it. A checker that always returns
  `True` would pass every other test in this file for the wrong reason;
  this test is what rules that out.

### 5.2 Validation logic (23 tests, `test_simulation_validation.py`)

Confirms `app.simulation.validation` **rejects** out-of-bounds
configurations rather than silently clamping them — magnitude beyond
`MAX_MAGNITUDE_ROBUST_STD_MULTIPLE` (10× the reference period's robust
standard deviation, falling back to `MAX_MAGNITUDE_RANGE_MULTIPLE` = 5× the
observed range when a robust std isn't available), too little baseline data
(`MIN_BASELINE_OBSERVATIONS = 30`), a missing required duration for
`temporary_spike` or `missing_data_gap`, and multi-sensor sub-config errors
(wrong count, wrong scenario type nesting, etc.) — each returning specific,
numeric error text rather than a generic rejection.
`TestMissingDataGapValidation` (6 tests, Phase 15) additionally confirms
`missing_data_gap`'s `[0, 1]` drop-fraction magnitude is checked against its
own dedicated bound, not the generic sensor-units one — including the case a
generic bound would get wrong in either direction: `magnitude=5.0` is small
enough to sail under most sensors' robust-std bound but must still be
rejected (it isn't a sensor-units value for this type), while `magnitude=
None` must be accepted (meaning "drop the whole window" at run time) even
though every other type requires a non-`None` magnitude.

### 5.3 Comparison logic (8 tests, `test_simulation_comparison.py`)

Confirms every comparison function computes a real, independently-derived
delta from two already-computed pipeline bundles — and, specifically,
`test_compare_bundles_never_produces_a_single_score_field` walks the entire
comparison output recursively checking for forbidden keys
(`risk_score`, `composite_score`, and similar) to make the "no fabricated
combined score" constraint (§8) a test assertion, not just a docstring
promise.

### 5.4 End-to-end HTTP behavior (19 tests, `test_api_scenario.py`)

Full create → validate → run → inspect workflows over real HTTP, including
one test that queries `SensorMeasurement` rows directly from the database
before and after a scenario run and asserts they are **byte-identical** —
the concrete, executable form of the non-destructive guarantee, not just a
unit-level check on the perturbation functions in isolation. Also covers:
validation rejection end-to-end (never clamps over HTTP either), archive
(never hard-delete) plus post-archive re-validate/re-run refusal (409),
sensitivity-sweep magnitude-count bounding, multi-sensor scenario execution
and the rejection of a single-sensor scenario incorrectly given multiple
sub-configs, role-based write authorization (403 for `viewer`), and
cross-organization isolation (404, never a silent empty result).
`TestMissingDataGapScenario` (3 tests, Phase 15) runs a full
`missing_data_gap` scenario end-to-end and confirms: the simulated bundle's
`observation_count` is genuinely lower than the baseline's (the gap actually
removed points from what the pipeline analyzed, not just from a value), the
real stored `SensorMeasurement` rows are byte-identical before and after
(same non-destructive guarantee as every other type), and both the
fraction-above-1 and missing-duration misconfigurations are rejected by
validation rather than silently accepted or clamped.

### 5.5 Investigation & copilot integration (7 tests, `test_scenario_integration.py`)

Confirms a scenario run can be attached to an investigation as evidence
without altering anything about the run or the investigation's other
evidence, and — separately — that the copilot always labels a scenario
result "SIMULATED"/"HYPOTHETICAL" in its grounding text, whether reached
directly, through an investigation's evidence links ("not a real
observation"), or via `evidence_selection`; and that asking about an
unrun scenario produces the honest "has not been run yet" rather than an
invented result.

### 5.6 What this validation does, and does not, establish

This confirms the simulation *engine* does exactly what it is configured to
do, byte-for-byte, every time. It does **not** — and no part of this
platform claims it does — establish that any scenario's hypothesized
modification reflects how this equipment would actually behave physically.
That claim would require a validated physical or empirical model this
project does not have (see `docs/AI_ML_METHODOLOGY.md` §1 on the same
honesty constraint for failure prediction). The real detectors invoked
against simulated data carry their own, separately-evaluated
evaluability classification in `docs/SCIENTIFIC_VALIDATION.md` §0 — running
them against a simulated series does not change what they were validated
against.

## 6. Non-destructive guarantee — defense in depth

Three independent layers, not one:

1. **By construction** — every perturbation function returns a fresh
   `.copy()` of its input; nothing in `app.simulation.*` ever calls `.loc[...] =`
   on a caller-owned object.
2. **Runtime re-verification** — `app.services.scenario_service.execute_scenario_run`
   calls `perturbations.verify_outside_window_unchanged` on the simulated
   series immediately before persisting any result, and refuses to persist
   (raising rather than silently continuing) if it ever disagrees with what
   the perturbation function claims to have done.
3. **Storage isolation** — `ScenarioRun.results` is a JSON blob on its own
   table. Nothing in this subsystem ever writes to `SensorMeasurement`,
   `Anomaly`, `DegradationSignal`, or `ForecastRun`. A simulated result and a
   real detection can never be confused because they are never stored in the
   same table, checked directly by `tests/test_api_scenario.py`'s
   database-level before/after assertion (§5.4).

## 7. Mandatory limitations — always attached, never optional

Every `ScenarioRun.results` includes a `limitations` list
(`app.simulation.comparison.MANDATORY_LIMITATIONS`) stating, in every run,
without exception: this is a hypothetical non-destructive exploration, not
a physical simulation or a prediction; no claim is made that robust
z-score / OLS trend / windowed variance capture this equipment's true
physical behavior; any relationship implied by a `multi_sensor` scenario was
set explicitly by the person configuring it, never inferred by the
platform; and any uncertainty band shown comes from in-sample residual
spread on real data, not a calibrated real-world probability. Sensitivity
sweeps add one further note explicitly disclaiming that the tested
magnitudes are read as any kind of physical dose-response curve, and no
trend line is ever fit across them.

## 8. Governance interfaces

- **Investigations** (`InvestigationEvidenceType.SCENARIO_RESULT`): a
  scenario run can be attached as evidence via the same generic
  `attach_evidence` machinery every other evidence type uses; the copilot's
  investigation-timeline tool always relabels it "SIMULATED... not a real
  observation" regardless of how the investigation itself was framed.
- **Copilot** (`CopilotContextType.SCENARIO`, `get_scenario_evidence`): a
  conversation scoped to a scenario reports its most recent runs (bounded to
  5), always labeled simulated, and honestly states "has not been run yet"
  for a scenario with no runs rather than inventing one.
- **REST API** (`/api/v1/scenarios/*`): every route is organization-scoped;
  writes (create/validate/run/sweep/archive) require ADMIN or ENGINEER;
  reads are open to any authenticated org member; `DELETE` archives rather
  than hard-deleting, so every prior run stays inspectable.
- **Frontend** ("Scenario Lab", `/scenarios`): every rendered result is
  wrapped in a fixed violet dashed-border banner reading "Simulated scenario
  result — hypothetical, not a real observation," styled in a distinct
  color family (violet/indigo) from every other page's real-data styling
  (slate/blue/emerald), so a simulated number cannot be mistaken for a real
  one by appearance alone.

## 9. Known limitations of this subsystem, stated plainly

- Isolation Forest is not reused for scenario simulation (§4) — a documented
  scope boundary, not an oversight.
- Scenario results carry only summary statistics and analytical outputs
  (mean/median/std/min/max, anomaly candidates, degradation stage, change
  candidates, forecast backtest) — the simulated series' individual points
  are not persisted or returned by the API, so the frontend cannot render a
  point-by-point overlay chart of baseline vs. simulated values; comparison
  is presented as statistic tables and per-magnitude sweep charts instead.
- `multi_sensor` scenarios require each sub-sensor's configuration to be
  supplied explicitly (as JSON in the current frontend) — there is no UI
  wizard yet for building per-sensor configs field-by-field.
- Sensitivity sweeps are bounded to 8 magnitude points and to single-sensor
  scenario types only (§9 of the spec: a multi-sensor sweep would require
  inventing a combined magnitude, which this platform refuses to do).
- No scenario has ever been, or can be, validated against a real subsequent
  outcome — there is no ground truth for "did the equipment actually behave
  this way" in any dataset available to this project. This is the same
  honesty boundary already documented for failure prediction in
  `docs/AI_ML_METHODOLOGY.md` §1, applied here to hypothetical scenarios
  instead of real predictions.
