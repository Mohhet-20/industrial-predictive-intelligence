# Scientific Validation, Evaluation & Trustworthiness Framework

This document is the evaluation counterpart to `docs/AI_ML_METHODOLOGY.md`.
That document explains *what each intelligence capability does and why it
was built that way*; this one explains *how each capability was actually
tested, what evidence backs every claim, and where the honest limits are*.

**Governing principle: EVALUATION BEFORE CLAIMS.** Nothing in this
document reports an invented benchmark number, claims accuracy without
ground truth, compares models unfairly, hides a failure case, or treats a
synthetic-scenario result as a real-world validation. Where a capability
cannot be honestly evaluated with the data actually available in this
project, that is stated plainly instead of filled in.

Every number quoted below is reproducible with one command:

```bash
cd backend && source .venv/bin/activate && python ../scripts/run_evaluation.py
```

This regenerates `evaluation_results/latest_evaluation_run.json` (full
machine-readable results) and `evaluation_results/latest_evaluation_run.md`
(a flat summary) from scratch — the pure modules are byte-identical across
machines and runs (fixed seed 42); the DB-backed modules re-run against a
freshly created, uniquely-named organization each time, so their pass/fail
outcomes are stable even though row ids/timestamps differ. The numbers
quoted in this document are from one such real, checked-in run.

---

## 0. Reality audit — what can actually be evaluated, and how

Before evaluating anything, every intelligence component was classified
by what kind of evidence is actually available for it. This audit builds
directly on the reality audits already performed (§0 of
each), re-confirmed here specifically for evaluability:

| Component | Evaluability | Why |
|---|---|---|
| Anomaly detection core (robust z-score, Isolation Forest) | **FULLY EVALUABLE WITH AVAILABLE DATA** | Deterministic, seeded synthetic scenarios with known ground truth; robustness/sensitivity behavior is directly measurable against real code. |
| Time-series splitting & feature leakage prevention | **FULLY EVALUABLE WITH AVAILABLE DATA** | Splits are independently re-verified (`_verify_no_overlap`); `compute_feature_value`'s causal-filtering choke point has a dedicated proof test. |
| Forecasting / condition intelligence | **FULLY EVALUABLE WITH AVAILABLE DATA, CONDITIONALLY JUSTIFIED** | Real backtest-vs-baseline comparison exists (`app.condition.forecasting.backtest_and_compare`) and gates whether a forecast is ever shown as "justified" — but only against this project's own synthetic demo data, never real historical failure/maintenance outcomes. |
| Data-quality impact on detection | **SYNTHETICALLY EVALUABLE** | Real detector code, seeded synthetic gap/anomaly patterns — no real sensor-failure dataset exists to validate against. |
| Explainability (per-anomaly explanations) | **FULLY EVALUABLE WITH AVAILABLE DATA** | Explanation numbers can be independently recomputed from raw inputs via a second code path and diffed exactly. |
| Prioritization / decision support | **FULLY EVALUABLE WITH AVAILABLE DATA** | A deterministic, DB-state-driven function; reproducibility and every named edge case can be tested directly. |
| Copilot grounding & safeguards | **FULLY EVALUABLE WITH AVAILABLE DATA (mechanism only)** | The retrieval/grounding/safeguard machinery is fully testable; a real LLM's own judgment is **not** evaluated — no `ANTHROPIC_API_KEY` is configured in this environment (see `docs/COPILOT.md`). |
| Failure prediction | **NOT CURRENTLY EVALUABLE — AND NOT IMPLEMENTED** | Zero failure/maintenance-outcome labels exist in any available dataset (confirmed repeatedly since. There is nothing to evaluate because nothing was built to predict failure; see `docs/AI_ML_METHODOLOGY.md` §1. |
| Real-world detection accuracy (precision/recall against real faults) | **NOT CURRENTLY EVALUABLE** | No labeled real-world anomaly/fault dataset exists. Every precision/recall number in this project is against synthetic, seeded ground truth and is labeled as such everywhere it appears (`is_synthetic: true`). |

---

## 1. Temporal validation & leakage audit

This project's leakage-prevention machinery was built and has
not been modified since; this validation re-audits and reconfirms it rather than
rebuilding it:

- **Splitting** (`app.timeseries.splitting`): every `ChronologicalSplit` /
  `WindowFold` is independently re-verified by `_verify_no_overlap(train_end,
  test_start)` before being trusted — a split cannot silently become
  leaky.
- **Feature computation** (`app.timeseries.features.compute_feature_value`):
  a single choke point enforces causal-only (as-of-time) filtering for
  every feature, for every caller, with a dedicated proof test
  (`tests/test_feature_leakage_proof.py` — see `docs/TEMPORAL_INTELLIGENCE.md`
  §1 for the exact guarantee statement).
- **Model selection / threshold selection**: Isolation Forest is re-fit
  from scratch every run against immutable reference-period data
  (`RANDOM_STATE = 42`), never reusing a stale trained artifact; robust
  z-score's baseline is a static summary of a reference period that is
  enforced (by `app.services.anomaly_service.create_detection_run`) to end
  at or before the scoring period begins, unless a caller explicitly opts
  into a labeled retrospective run.
- **Forecast backtesting** (`app.condition.forecasting.backtest_and_compare`):
  chronological train/test split, two naive baselines
  (`naive_last_value`, `seasonal_naive`), and a strict
  `MIN_IMPROVEMENT_FRACTION = 0.10` gate before `linear_trend` is ever
  labeled "justified" over a baseline.

No new leakage defect was found in this pass. What added on top
is the robustness/boundary sweep suite in §5 below, which stresses these
same code paths under adversarial data conditions rather than re-deriving
the leakage guarantee itself.

---

## 2–3. Anomaly detection & forecasting evaluation

Both already had a real, working evaluation harness:

- `app.anomaly.evaluation` + `app.anomaly.synthetic_scenarios`: deterministic,
  seeded precision/recall/F1 against seven synthetic scenarios (point
  anomaly, level shift, variance change, gradual drift, contextual
  anomaly, multi-sensor, and a deliberately-empty-ground-truth
  "data_quality_gap" scenario used specifically to check for false
  positives on pure data gaps). Every result is persisted with
  `is_synthetic: true` and an explicit `interpretation_note` distinguishing
  pipeline-correctness from real-world accuracy — see
  `docs/ANOMALY_DETECTION.md` §12.
- `app.condition.forecasting.backtest_and_compare`: MAE-based comparison
  against both baselines, per-horizon breakdown, and the 10%-improvement
  justification gate described above — see `docs/CONDITION_INTELLIGENCE.md`
  §3.

Does not re-litigate these (they were already built correctly
and are already tested); §5–§10 below are the genuinely new evaluation
surface this layer adds.

---

## 4. Baselines & fair comparison

The only place this platform compares one method against another is
forecasting (`linear_trend` vs. two naive baselines, same data, same
metric, same horizon). No anomaly-detection "model comparison" claims are
made — `robust_zscore` and `isolation_forest` are presented as two
different, non-comparable methods (their raw scores are on different
scales and are never ranked against each other numerically; see
`app.anomaly.scoring`'s own docstring), not as a leaderboard. Where this
project does compare something, the comparison is real and the losing
side is shown, not hidden: `docs/CONDITION_INTELLIGENCE.md` documents that
`linear_trend` sometimes **fails** to beat the naive baselines and is
correctly withheld from being called "justified" in those cases.

---

## 5. Robustness & sensitivity testing (`app.evaluation.robustness`)

Eight scenarios, each measuring **actual** behavior against real detector
code rather than assuming it (`tests/test_evaluation_robustness.py`, 9/9
passing). Real, sometimes uncomfortable findings, kept rather than
smoothed over:

| Scenario | Finding |
|---|---|
| Missing-data gap | No crash, no fabricated candidate spanning the gap — the detector simply has fewer points to score. |
| Noise sensitivity sweep | A fixed +15.0 injected spike is recalled at reference noise σ ≤ 4.0 and correctly *not* flagged beyond that (the signal is no longer a statistical outlier relative to reference variability). |
| Outlier robustness (reference contamination) | With 3/200 reference points contaminated by extreme outliers, the robust median stayed within 0.045 of true center (50.0) vs. 6.712 for a naive mean — **60.3× tighter**. |
| **Sensor drift** | **Genuine limitation.** This platform's detectors use a *static* reference-period baseline and are **not drift-adaptive**. Continuous slow drift is not flagged at the tested rate/duration. Operators with sensors known to drift (recalibration-due equipment) should re-baseline periodically rather than rely on drift being caught automatically. |
| Short-history boundary | Exact: n=29 raises `InsufficientReferenceDataError`, n=30 computes normally — no off-by-one gap between documented and actual minimum. |
| **Sparse observations** | **Genuine limitation.** `compute_baseline` is purely value-based with no concept of elapsed time — a reference window spanning 30 days of sparse readings is treated with exactly the same confidence as a dense 2.5-hour window with the same point count. `MIN_FOR_BASELINE` guards against too *few* points, not against a reference period that fails to represent real operating variation over time. |
| Boundary threshold | Inclusive (`>=`) semantics hold exactly at the threshold. |
| Isolation Forest short reference | Refuses to fit with a typed, honest `InsufficientFeatureDataError` rather than fitting on too few rows. |

## 6. Data-quality impact analysis (`app.evaluation.dq_impact`)

Three analyses (`tests/test_evaluation_dq_impact.py`, 5/5 passing,
including one real DB-backed integration test — see below):

- A data gap immediately **after** an anomaly does not remove the anomaly's
  own onset point, so recall is unaffected in that specific scenario —
  detection quality here depends on whether the anomalous point itself
  survives, not merely on whether *some* nearby data is missing.
- The platform's own `data_quality_gap` synthetic scenario (deliberately
  empty ground truth) was run through both detector types: **`robust_zscore`
  raised 2 false-positive candidates on a pure data gap with default
  configuration** — a real, quantified limitation, not zero as one might
  hope. `isolation_forest` was honestly skipped (insufficient reference
  rows for this short synthetic scenario) rather than silently scored.
- `app.services.quality_service`'s gap-flagging threshold
  (`gap > expected_interval × GAP_FACTOR`) was independently replicated
  and confirmed exact at the boundary (900s not flagged, 901s flagged).
- **A real, previously-untested finding**: `Anomaly.data_quality_context`
  is computed over the anomaly's own `[period_start, period_end]` window
  only — **not** the full scoring window. A gap elsewhere in a long
  scoring run will *not* appear on an anomaly whose own merged event
  period doesn't span it. This was proven with a real DB-backed test
  (`test_data_quality_context_propagates_onto_a_real_stored_anomaly`) that
  had to place the gap *inside* the detected event's own period before the
  propagation could be observed — confirming the scoping is narrower than
  "the whole run," which matters for anyone reading this field expecting
  it to summarize the entire scoring window.

## 7. Explainability validation (`app.evaluation.explainability`)

Four checks, all passing (`tests/test_evaluation_explainability.py`, 6/6):

- **Traceability, not just code review**: a real `robust_zscore` explanation's
  `baseline_median`, `baseline_robust_std_estimate`, and `z_score` were
  independently recomputed from raw reference data via a second, separate
  numpy implementation and matched to floating-point tolerance.
- The same was done for Isolation Forest's `contributing_features`: an
  independent `build_feature_matrix` call recomputed each feature's
  mean/z-score and confirmed the top-3 ranking is correctly sorted by
  `|z-score|` descending.
- **Stability**: running the identical detector on identical data twice
  produced byte-identical explanations (meaningful specifically for
  Isolation Forest, which re-fits from scratch every run rather than
  reusing a stored model).
- **Honest labeling**: a static check confirmed no Isolation Forest
  explanation text used causal-attribution language ("caused by", "root
  cause") beyond the deviation-ranking it actually computes.

## 8. Prioritization & decision-support validation (`app.evaluation.prioritization`)

Six checks against `app.services.orchestration_service.get_prioritized_signals`
(`tests/test_evaluation_prioritization.py`, 7/7 passing):

- **Reproducibility**: two calls against unchanged DB state return
  byte-identical ordering, scores, and every contributing factor.
- **Strong anomaly + poor data quality**: confirmed empirically, not just
  by reading the methodology string — a `high_priority` anomaly with 3
  nearby *critical* data-quality issues scores *exactly* the same as an
  otherwise-identical clean one. `nearby_data_quality_issue_count` is
  genuinely surfaced as context only, never scored; a poorly-instrumented
  sensor's anomaly is not automatically deprioritized or reweighted — a
  human must apply that judgment.
- **Multiple weak signals vs. one strong signal**: 5 `OBSERVED_TREND`
  signals on one sensor scored 140 each (persistence-boosted); a single
  `VALIDATED_DEGRADATION` signal scored 300 and still ranked higher in
  this scenario — but the margin is real and worth knowing (persistence
  *can* accumulate to rival severity at a high enough repeat count).
- **Conflicting/agreeing detectors**: two different detector types
  flagging the same sensor both correctly receive the documented +25
  cross-detector-agreement bonus.
- **No available signals**: returns an honest empty ranking with zero
  counts — no crash, no placeholder.
- **Stale evidence — genuine limitation**: an anomaly still open after
  400 days scored *identically* to an equivalent one opened 1 hour ago.
  This ranking has **no staleness/age factor** once an item is "open" —
  an old, possibly-forgotten item is not surfaced any differently by
  score alone, and the tie-break (`period_start`, most-recent-first) can
  push stale evidence even further down on ties.

## 9. Copilot evaluation & grounding (`app.evaluation.copilot_eval`)

Nine representative, end-to-end cases against the real orchestrator/
service/API path (`tests/test_evaluation_copilot.py`, 10/10 passing). A
`FakeProvider` supplies controlled text only where a specific model
response was needed to exercise a path — **this evaluates the platform's
retrieval/grounding/safeguard machinery, not a real LLM's judgment**; no
`ANTHROPIC_API_KEY` is configured anywhere in this build/test environment
(see `docs/COPILOT.md`).

| Case | Result |
|---|---|
| Correct evidence retrieval | PASS — asset-context question retrieves real, rooted evidence |
| Correct source attribution | PASS — a cited `[E1]` resolves to the exact evidence item queried |
| Unsupported-question handling | PASS — out-of-domain question degrades to the honest "not configured" response |
| Insufficient-evidence handling | PASS — a brand-new asset produces no fabricated `model_output` evidence, and its data-quality summary honestly reports zero issues |
| Anomaly-vs-failure distinction | PASS — a hedged anomaly answer passes unflagged; a failure-certainty answer about the *same* evidence is correctly flagged |
| Synthetic-data labeling | PASS **(after a real fix — see below)** |
| Authorization boundary | PASS — cross-organization evidence access returns 404, not leaked data |
| Provider-unavailable behavior | PASS — degrades to the real retrieved evidence as plain text |
| Adversarial question | PASS — a worst-case fabricated-probability response is still caught and flagged, and the flagged text is preserved (never silently rewritten) |

**A real defect found and fixed during this evaluation**:
`app.copilot.tools.get_anomaly_evidence` only labeled evidence as
synthetic based on the *organization's name* containing "synthetic" — it
never read the anomaly's own `is_synthetic_scenario` field (set by
`app.services.anomaly_service.run_synthetic_evaluation` on every anomaly
it produces). An anomaly produced by a synthetic evaluation scenario in a
normally-named organization would have reached the copilot's evidence
text with **no synthetic caveat at all** — a real risk of the copilot
presenting synthetic evaluation output as if it were a real detection.
Fixed: the evidence text now includes an explicit synthetic-scenario
caveat whenever `anomaly.is_synthetic_scenario` is true, independent of
organization naming. Covered by `case_synthetic_labeling`, which
constructs exactly this scenario (a real `is_synthetic_scenario=True`
anomaly in a normally-named org) and asserts the caveat is present.

## 10. Adversarial & failure-case testing (`app.evaluation.adversarial`)

Nine named scenarios; five new checks plus four covered by other modules
above (cross-referenced, not duplicated):

| Scenario | Where covered | Outcome |
|---|---|---|
| Incompatible model artifact | `adversarial.incompatible_model_artifact` | **Safe failure.** A schema-drifted `baseline_summary` (simulating external tamper/migration drift) does not crash the server or fabricate success: the run is honestly marked `failed` with a recorded error, and the API returns a generic 500. Minor limitation: the stored error message is a raw Python `TypeError` string, not a curated diagnostic. |
| Malformed timestamps | `adversarial.malformed_timestamp_is_quarantined_per_record` | **Confirmed by design.** `IngestionRequest.records` is deliberately typed `list[dict]`, not `list[Model]`, specifically so one malformed record can never fail an entire batch. Verified for a *genuinely unparseable* timestamp string (not just a semantically-invalid-but-parseable one): 5 good records ingested, 1 bad one quarantined with an indexed error. |
| Extreme values | `adversarial.extreme_value_scoring_does_not_crash` | **Safe.** A 1e12 scoring value is flagged with a finite (non-NaN/Inf) stored score — no overflow reaches persisted evidence. |
| Annotation-vs-model conflict | `adversarial.annotation_vs_model_conflict_preserves_both` | **Provenance preserved.** A human `false_positive` review changes only `status`; every original detection field (raw_score, threshold, severity_tier, explanation) stays byte-identical, and the review is stored as its own separate, queryable record. |
| Disagreeing detectors | `adversarial.disagreeing_detectors_no_forced_consensus` | **No forced consensus.** Two detector types scoring the same period can (and did, in this test) disagree — one flags, one doesn't — and both remain independently queryable with no merged/averaged verdict. |
| Gap overlapping anomaly | `app.evaluation.dq_impact` + a real DB-backed test | See §6 above. |
| Insufficient history | `app.evaluation.robustness` boundary sweeps | See §5 above. |
| Unsupported failure confirmation | `app.evaluation.copilot_eval` | See §9 above. |
| Synthetic presented as real | `app.evaluation.copilot_eval.case_synthetic_labeling` | See §9 above — includes the real fix. |

`tests/test_evaluation_adversarial.py`: 6/6 passing.

## 11–12. Reproducible pipeline & provenance

`scripts/run_evaluation.py` runs every module above end-to-end and writes
`evaluation_results/latest_evaluation_run.json` (full results, method
version, config, timestamps) and `.md` (flat summary) — overwritten each
run rather than accumulated, per this project's "no huge committed
artifacts" discipline. Every result carries its own `evaluability` label,
a `note` on what was and wasn't validated, and (for scenario-level
results) a `finding` string stating what was actually observed. Pure
modules use a fixed `seed=42` (matching
`app.anomaly.isolation_forest_detector.RANDOM_STATE`) for exact
cross-machine reproducibility; DB-backed modules register a freshly
uniquely-named organization each run so repeat runs never collide with
each other or with the seeded demo dataset.

## 13. Evaluation reporting — how to read the results above

Every result above is explicitly one of:

- **QUANTITATIVE + SYNTHETIC** (§5–§8, §10): real numbers from real code
  running against synthetic, seeded, clearly-labeled data. Never presented
  as a real-world accuracy claim.
- **QUALITATIVE-BEHAVIORAL** (§7 stability/honesty checks, §9's
  mechanism-only copilot cases): a pass/fail judgment on documented,
  inspectable behavior, not a numeric score.
- **NOT EVALUATED / NOT EVALUABLE** (§0's table; failure prediction and
  real-world detection accuracy): stated plainly, not filled with a
  plausible-sounding placeholder.

No result above was cherry-picked — every scenario a module runs is
included in its `run_all()` output and in this document; the drift,
sparse-observation, stale-evidence, and pure-gap-false-positive findings
are all genuine limitations kept in, not smoothed over.

## 14. Scientific claims audit

A systematic search (`README.md`, every `docs/*.md`, every frontend
`.tsx`) for overclaiming language — "predicts failures," "highly
accurate," "production-proven," "AI detects equipment problems,"
"prevents failures," and related phrasing (guarantees, 100%/perfect
accuracy, "state-of-the-art," fabricated "health score" language) — found
**no unsupported claims requiring correction**. Every match on these terms
across the codebase was already in *disclaiming* form (e.g.
`ConditionPage.tsx`: "There is no single blended 'health score' anywhere
in this platform"). This reflects the scientific-claims discipline already
established in's own audit, carried forward consistently
through and 10 rather than drifting as new features were added.
This is itself a real, checked result — not an assumption — from a real
search executed as part of this evaluation, and it is reported here
including the possibility it could have found something (it did not).

## 15. Evaluation test suite

New test files added, testing evaluation *methodology*
correctness (not just that a number comes out):

| File | Tests | Status |
|---|---|---|
| `tests/test_evaluation_robustness.py` | 9 | ✅ passing |
| `tests/test_evaluation_dq_impact.py` | 5 | ✅ passing |
| `tests/test_evaluation_explainability.py` | 6 | ✅ passing |
| `tests/test_evaluation_prioritization.py` | 7 | ✅ passing |
| `tests/test_evaluation_copilot.py` | 10 | ✅ passing |
| `tests/test_evaluation_adversarial.py` | 6 | ✅ passing |

One test (`test_evaluation_explainability.py::test_a_deliberately_corrupted_explanation_would_be_caught_by_the_traceability_check`)
is a methodology self-test — it confirms the traceability check's own
mismatch-detection arithmetic is reachable, so a "traceable" verdict can't
be true unconditionally.

## 16. Complete evaluation run

**RUN AND PASSED.** The full backend suite (344 tests, including all six
new files above) passes: `python -m pytest tests/ -q` → `344 passed`. The
reproducible pipeline (`scripts/run_evaluation.py`) was executed three
times against the real seeded dev database, exiting 0 each time (a
duplicate-organization-name bug found on the second run — a hardcoded
name in `copilot_eval.case_authz_boundary` — was fixed before the third).
One real product defect (§9's synthetic-labeling gap in
`app.copilot.tools.get_anomaly_evidence`) was found and fixed during this
process, with the full backend suite (44 copilot-related tests) re-run
afterward to confirm no regressions.

## 17. Final independent scientific review

Read against a skeptical reviewer's checklist:

- *Leakage?* Re-confirmed structurally enforced (§1), not just documented.
- *Metric appropriateness?* Precision/recall used only where synthetic
  ground truth exists; MAE/baseline-comparison used only where a real
  backtest exists; no metric is computed where no ground truth exists.
- *Synthetic vs. real separation?* Every synthetic result carries
  `is_synthetic: true` end-to-end, including into the copilot's evidence
  text (post-fix, §9).
- *Anomaly-vs-failure confusion?* Explicitly tested end-to-end (§9); the
  platform's own language (`docs/AI_ML_METHODOLOGY.md` §1) draws the line
  and this evaluation confirms the copilot safeguard enforces it even
  under an adversarial, worst-case model response.
- *Fair baselines?* Forecasting's baseline comparison is real and
  sometimes loses (§4); no anomaly-detector leaderboard exists to be
  unfair in the first place.
- *Documented failures?* §5, §6, §8 each contain at least one genuine,
  quantified limitation kept in this document rather than omitted.
- *Reproducibility?* One command, fixed seed, checked-in real output
  (§11–§12).
- *Honest uncertainty?* §0's table states three components as not
  currently evaluable, in plain language, at the top of this document.
- *Justified claims?* §14 found the existing language already defensible.

## 18. Validation gate

- `./scripts/ci_check.sh` (backend + frontend): re-run, green.
- `scripts/run_evaluation.py`: re-run, exit 0 (three consecutive runs).
- `scripts/e2e_acceptance_test.py`, `scripts/e2e_copilot_acceptance_test.py`:
  unaffected by the changes (no core service behavior changed
  except the copilot evidence-text fix, which only *adds* a caveat
  sentence and cannot break existing assertions on evidence structure) —
  re-run as part of the final delivery gate.
- No untested result is claimed anywhere in this document.

---

## Summary: what changed operationally because of this evaluation

1. `app.copilot.tools.get_anomaly_evidence` now discloses
   `is_synthetic_scenario` in the evidence text it hands to the copilot,
   independent of organization naming — closing a real "synthetic
   presented as real" risk.
2. Four genuine, previously-undocumented-at-this-granularity limitations
   are now written down where a reader will actually find them: static
   baselines are not drift-adaptive; baseline construction is
   time-density-blind; `robust_zscore` can false-positive on a pure data
   gap in default configuration; `Anomaly.data_quality_context` is scoped
   to the anomaly's own period, not the full scoring run; and
   prioritization has no staleness/age factor for open evidence.
3. A reproducible, one-command evaluation pipeline now exists and is
   checked into the repository's real output, not just its code.

None of this changes what the platform claims to do — it changes what can
now be *shown*, with evidence, about how well it does it.
