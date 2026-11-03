# Time-Series Intelligence — What This Layer Does and Doesn't Do

Status: **Phase 2 — Time-Series Intelligence Foundation.** This describes
what is actually implemented and tested today. It is explicitly NOT the
anomaly-detection system — see "What this is not" at the bottom.

## 0. What this builds on

Everything here reads from `sensor_measurements` (Phase 1's immutable raw
data) through `app/timeseries/data_access.py`, the one module in this layer
that talks to the database. Every other `app/timeseries/*` module takes and
returns plain `pandas.Series`/dicts and has no SQLAlchemy dependency —
this is what makes them independently unit-testable without a database
(see `backend/tests/test_profiling.py`, `test_resampling.py`,
`test_analysis.py`, `test_features.py`, `test_dataset_builder.py`,
`test_splitting.py`, `test_multi_sensor.py`, `test_change_detection.py`).
`app/services/timeseries_service.py` and `app/services/feature_service.py`
are the thin, organization-scoped orchestration layer that connects the two.

## 1. Temporal leakage — the central design constraint

**The rule:** a feature computed "as of" timestamp T may use only
observations at or before T. A target (label) for a row anchored at T may
use only observations strictly after T. These two rules are enforced
structurally, not by caller discipline:

- `app/timeseries/features.py::compute_feature_value` takes the FULL sensor
  series (which may contain future data) and, as its first step, slices to
  `full_series[full_series.index <= as_of]` before any statistic touches
  the data. A caller cannot leak future data by forgetting to pre-filter —
  the enforcement lives in the one function every feature computation goes
  through.
- `app/timeseries/dataset_builder.py::_compute_target` is a structurally
  separate function that only ever sees `series[series.index > as_of]`.
  Features and targets are computed by different functions against
  independently-passed series objects, so a target value cannot end up
  inside a feature by accident.
- `app/timeseries/splitting.py`'s chronological/rolling/expanding splits
  are defined purely by timestamp boundaries (never row shuffling), and
  every split result carries a `leakage_safe: bool` that is independently
  re-verified (`_verify_no_overlap`), not just asserted.

**Proof, not just design intent:** `backend/tests/test_features.py`
parametrizes over every `FeatureKind` and plants a distinctive, unmistakable
value one second after `as_of`, asserting every kind is blind to it.
`backend/tests/test_dataset_builder.py::test_target_never_leaks_into_features`
and `backend/tests/test_api_timeseries.py::test_dataset_build_with_target_never_leaks_end_to_end`
do the same at the dataset-row and live-API level. All pass as of this
writing (see `backend/tests/` — run with `pytest`).

One caught bug worth recording: while writing these tests, the leakage
tests found a real defect in `_compute_target` (`TimedeltaIndex` has no
`.abs()` method in the pandas version pinned here — it raised
`AttributeError` on every target computation) and a real defect in the
"downsample for transport" step in `analysis.py`/`profiling.py` (floor
division silently failed to bound output size for `n` between 1x and 2x
the cap). Both are fixed and covered by regression tests now. This is
exactly the value of testing leakage/bounds explicitly rather than
trusting the implementation's own claims about itself.

## 2. Profiling vs. analysis — two different endpoints on purpose

`GET /timeseries/sensors/{id}/profile` (`app/timeseries/profiling.py`)
returns one fixed, cheap overview: observation count, sampling interval,
gaps, descriptive stats, distribution shape, a linear trend, rolling
mean/std, and mean rate of change — every field gated on a minimum
observation count (documented in the module) and `None` with a stated
reason when there isn't enough data, never a number computed on noise.

`GET /timeseries/sensors/{id}/analysis` (`app/timeseries/analysis.py`)
is the "dig deeper" endpoint: caller-chosen rolling window, volatility
window, and autocorrelation grid/lag range. Autocorrelation resamples onto
a regular grid first (dropping — never inventing — missing buckets) and
reports coverage, because ACF on 60%-coverage data is a much weaker signal
than on 98%-coverage data.

## 3. Resampling & interpolation — never invents an observation silently

`app/timeseries/resampling.py::resample_series` buckets a series into
fixed, explicitly-anchored windows (anchored at the caller's `start`, not
a calendar boundary, so repeated calls with the same `start`/`freq_seconds`
always align). The default `InterpolationPolicy.NONE` leaves empty buckets
as `value: null, is_observed: false`. `LINEAR` and `FORWARD_FILL` fill
them, but every filled point is flagged `is_interpolated: true` and
`is_observed: false` — downstream code (features, correlation, charts)
can always tell a real reading from a filled one. `MAX_BUCKETS = 20,000`
is enforced before any array is built.

## 4. Feature engineering — categories, gating, and reproducibility

Implemented feature kinds (`app/timeseries/features.py::FeatureKind`),
grouped by category:

- **Statistical**: mean, median, std, variance, min, max, quantile, iqr
- **Temporal**: lag, rolling_mean, rolling_std, rate_of_change, diff, ema
- **Distributional**: skewness, kurtosis — gated on n ≥ 30
- **Frequency**: spectral_dominant_period, spectral_energy (FFT-based) —
  gated on n ≥ 32 AND ≥ 70% grid coverage after resampling; most
  sensor/window combinations will not qualify, and the feature reports why
  rather than guessing
- **Domain**: not implemented. No verified domain-specific feature
  definition exists for this generic synthetic equipment fleet (a "duty
  cycle" or "load factor" feature needs a real operational definition this
  platform doesn't have). The category and registry hook exist so one can
  be added later without restructuring anything.

**Reproducibility:** `FeatureDefinition` rows are versioned and immutable
— `POST /timeseries/feature-definitions` always creates a new version
(`name`, `version`) and there is no update endpoint. A dataset built
against version 3 of a feature keeps meaning exactly what it meant then,
even after someone creates version 4.

## 5. Training dataset builder

`POST /timeseries/datasets` persists a `DatasetBuild` row holding a config
**snapshot** (sensor ids, feature specs, time grid, optional target spec)
— not the feature matrix itself, which can be large. `GET
/timeseries/datasets/{id}/rows?limit&offset` recomputes a page of the
matrix on demand from that config, which is safe because raw measurements
are immutable and feature definitions never change meaning in place.

Grids at or under `MAX_DATASET_ROWS_SYNC` (3,000 rows) build synchronously
in the request. Larger grids (up to `MAX_DATASET_ROWS_TOTAL` = 50,000 rows)
are submitted to the background job runner (`JobType.DATASET_BUILD`,
`app/jobs/handlers.py::_run_dataset_build`); the job computes and stores
only summary stats (`row_count_total`, `null_feature_rate`) — never the
full matrix — poll `GET /jobs/{job_id}` exactly like any other background
job.

## 6. Time-aware splitting & evaluation utilities

`app/timeseries/splitting.py`, exposed at `/timeseries/splits/*`:

- **Chronological holdout**: train / (optional) validation / test as
  contiguous, non-overlapping time ranges in that order.
- **Rolling window**: fixed-size train window that slides forward each
  fold — appropriate when older data should eventually be discarded.
- **Expanding window**: train window always starts at the beginning and
  grows each fold — appropriate when older data should never be discarded.

No function in this module shuffles rows. Shuffling a time series before
evaluation lets a model see interpolated neighbors of its own test points
during training, silently inflating every downstream metric — if a future
phase wants that for a specific, justified methodology, it should ask for
it explicitly rather than getting it by default here. These utilities are
foundation-only: nothing in this phase trains a model with them yet.

## 7. Multi-sensor temporal context — association, never causation

`app/timeseries/multi_sensor.py` aligns sensors onto a shared grid, then
reports Pearson correlation (gated on ≥ 10 overlapping observations) and
lagged cross-correlation (every lag reported, plus the strongest one — not
a single cherry-picked number). Every result carries the same repeated
caveat: *"This measures statistical association only. It is not evidence
of a causal relationship — shared timing, a common upstream cause, or
coincidence can all produce a strong correlation between two unrelated
sensors."* Missingness comparison ranks sensors by data completeness over
the same window, independently computed per sensor.

## 8. Change & behavior candidate detection

`app/timeseries/change_detection.py` slides a comparison point across a
series and scores the standardized difference between the window
immediately before and immediately after it (level-shift: mean difference
in pooled-std units; variance-shift: variance ratio). This is a plain,
inspectable effect-size statistic, not a fitted model — every candidate
can be explained in one sentence ("the average shifted from X to Y between
these two timestamps"). Candidates are merged when they fall within one
window's distance of each other and capped at 20 returned, sorted by
strength.

**These are candidates for human review, not confirmed anomalies and not
fault diagnoses.** A rigorous, validated anomaly-detection layer — with
proper evaluation against known-good/known-bad periods — is explicitly
planned for a later phase, not this one. The API, response shape, and
frontend copy all say "candidate," never "anomaly" or "risk level."

## 9. API surface (`/api/v1/timeseries/*`)

All bounded (every service call inherits Phase 1's `MAX_ROWS`/
`MAX_BUCKETS` guards and raises `422 validation_failed` rather than
truncating silently), all organization-scoped (every sensor lookup joins
through `Asset.organization_id`), all behind the same bearer-token auth as
the rest of the API.

| Endpoint | Purpose |
|---|---|
| `GET /sensors/{id}/profile` | Fixed overview (§2) |
| `GET /sensors/{id}/analysis` | Configurable rolling/volatility/ACF (§2) |
| `GET /sensors/{id}/resample` | Bucketed series with explicit interpolation flags (§3) |
| `GET /sensors/{id}/change-candidates` | §8 |
| `GET /correlation`, `/lagged-correlation`, `/missingness-comparison` | §7 |
| `POST /feature-preview` | Compute a feature's value right now, without saving a definition |
| `POST /feature-definitions`, `GET .../{id}` | §4 |
| `POST /datasets`, `GET /datasets/{id}`, `GET /datasets/{id}/rows` | §5 |
| `GET /splits/chronological`, `/rolling-window`, `/expanding-window` | §6 |

## 10. What this is NOT

- **Not the anomaly-detection system.** Change candidates (§8) are
  explicitly not anomalies. There is no scoring model, no "risk level,"
  no alerting.
- **Not a forecasting system.** Trend lines are descriptive fits over the
  requested window, not forecasts, and say so in their own `note` field.
- **Not causal analysis.** Correlation and lagged correlation are
  association only (§7), repeated in every response, not just this doc.
- **Not fully-featured ML tooling.** The feature framework, dataset
  builder, and splitting utilities are a reusable *foundation* — no model
  is trained anywhere in this phase's code.
- **Domain features are not implemented** (§4) — no invented "health
  score" or "duty cycle" masquerading as a validated engineering metric.
