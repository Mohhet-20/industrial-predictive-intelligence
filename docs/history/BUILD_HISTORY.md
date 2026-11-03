# Build history

How the platform was built, phase by phase, with the validation evidence recorded at the end of each
phase. The current state is in the [README](../../README.md); this page is the historical record.

## How it was built

This project was built across 9 phases, each one beginning with a fresh,
repository-wide audit of what actually existed — never assuming a
capability was implemented just because an earlier phase planned it —
and each one ending with a real, self-found bug fixed before moving on.
That discipline is itself part of what this project demonstrates, so it's
recorded here rather than only in commit history that doesn't exist (this
repository has no git history — see [Known limitations](#known-limitations)).

- **Phase 1 — Foundation.** Asset/sensor management, ingestion with
  per-record validation, deduplication, data-quality checks, a background
  job runner, a labeled synthetic demo dataset, and a working frontend.
- **Phase 2 — Temporal intelligence.** Profiling, explicit
  resampling/interpolation, a feature framework with *structural* temporal-
  leakage prevention (tested), a training dataset builder, time-aware
  splitting utilities, multi-sensor correlation, change-candidate detection.
- **Phase 3 — Anomaly detection.** Robust z-score + Isolation Forest,
  causally-enforced reference/scoring separation, explainability from real
  calculations, a versioned detector lifecycle with human review, a
  deterministic synthetic evaluation harness.
- **Phase 4 — Condition intelligence & forecasting.** A fresh evidence
  audit that found no failure labels and scoped everything to that
  reality; five independent evidence types with no fabricated score; a
  human-gated three-stage degradation classification; a forecaster gated
  by an actual backtest against a naive baseline. Found and fixed a real
  mid-computation exception-handling bug, with a regression test proving it.
- **Phase 5 — MLOps lifecycle & governance.** A component/version
  registry on top of (not duplicating) Phase 3/4's own versioning; an
  8-stage lifecycle; task-specific evaluation gates; real shift analysis;
  a safe scoring contract; a full live end-to-end lifecycle run against
  the real seeded dataset. Found and fixed a real version-numbering
  collision risk, with a regression test.
- **Phase 6 — Investigation & orchestration.** A human-oriented
  Investigation model referencing (never duplicating) real evidence; a
  relational evidence graph; a unified five-category timeline; transparent
  prioritization; cross-layer summaries that never blur data, signals,
  model output, and human interpretation. Found and fixed a real
  evidence-relation direction bug, with a regression test and a live
  end-to-end investigation run.
- **Phase 7 — Production reliability, deployment, security, observability.**
  Fail-fast production config validation; a real metrics endpoint; job
  timeout + duplicate protection; login rate limiting + a timing-
  side-channel fix; security headers; a working CI pipeline. Found and
  fixed a silent build-tooling bug that had been leaking stray files into
  the frontend source tree since Phase 1, and the login timing
  side-channel, both during this phase's own principal-SRE self-review.
- **Phase 8 — Flagship finalization.** This documentation set, a final
  code-quality and UX-consistency pass, a live end-to-end acceptance test
  exercising the complete investigation workflow through real application
  logic, and the final validation gate below.
- **Phase 9 — Evidence-grounded AI copilot.** A readiness audit that
  classified every candidate evidence source before writing any copilot
  code; narrow, authorization-enforced retrieval tools (never raw DB
  access); a grounding/hallucination safeguard enforced architecturally —
  re-checking every model answer against its real evidence list, not just
  a system-prompt instruction — proven on the live orchestration path with
  an adversarial fake provider; an LLM provider abstraction where "no
  provider configured" is a first-class, tested, still-useful state; a
  context-aware frontend panel embedded in the actual platform, not a
  floating generic chatbot; and a live end-to-end acceptance run that
  deliberately asks an insufficient-evidence question and verifies the
  answer stays honest.
- **Phase 10 — Scientific validation.** A reproducible evaluation
  pipeline (`scripts/run_evaluation.py`) over robustness, data-quality
  impact, explainability, prioritization, copilot grounding, and
  adversarial/failure-case checks, with its real output committed at
  `evaluation_results/latest_evaluation_run.{json,md}` rather than
  described from memory. See [docs/SCIENTIFIC_VALIDATION.md](../SCIENTIFIC_VALIDATION.md).
- **Phase 11 — Scenario simulation / what-if analysis.** Non-destructive
  perturbation of real historical data through the *same* analytical
  pipeline every other layer uses — original observations are never
  mutated (verified byte-identical before/after a run), every result is
  labeled SIMULATED end to end including inside copilot grounding text,
  absurd magnitudes are rejected rather than silently clamped, and no
  combined/composite score is fabricated across multi-run comparisons.
  Explicitly **not** a physical digital twin. See
  [docs/SCENARIO_SIMULATION.md](../SCENARIO_SIMULATION.md).
- **Phase 12 — Closed-loop intelligence, outcome feedback & governed
  learning.** Outcome records (proposed/reviewed/verified/rejected/
  conflicting/unverified) linked to the anomaly/condition evidence they
  assess, with a structural guard against the exact leakage risk this
  domain invites — an outcome-feedback record being used before its own
  verification time — enforced as tested code paths, not developer
  discipline. Retraining candidates are human-governed; nothing
  auto-activates from feedback. See
  [docs/CLOSED_LOOP_FEEDBACK.md](../CLOSED_LOOP_FEEDBACK.md).

**301 backend tests pass** (267 carried over from Phases 1–8 + 34 for
Phase 9's copilot) and **15 frontend tests pass** (11 carried over from
Phase 7 + 4 for the copilot panel); the anomaly detection, condition/
forecasting, MLOps lifecycle, investigation, reliability/failure-handling,
and copilot workflows were all validated live against the real seeded
dataset or real fault injection, not just test fixtures. (As of Phase 13's
audit, the count was **494 backend tests** and **15 frontend tests**; as
of Phase 14's addition of the Mahalanobis multivariate detector, the
current, actual count is **512 backend tests** and **15 frontend tests**
— this paragraph is left as the real snapshot it was written as, rather
than silently edited to a number it didn't describe at the time; see
"Final validation results (Phase 14)".)

## Final validation results (Phase 8)

`./scripts/ci_check.sh`, actually run from the repository root as the
last step of this project's work, end to end:

```
=== [2/6] Backend: migrations apply cleanly to a fresh database ===
...7 migrations applied in order...
No new upgrade operations detected.
=== [3/6] Backend: full test suite ===
267 passed, 2 warnings in 71.82s
=== [3b/6] Backend: dependency vulnerability scan (informational) ===
Found 19 known vulnerabilities in 5 packages (see docs/RELIABILITY_AND_OPERATIONS.md §10)
=== [5/6] Frontend: lint + typecheck ===
✖ 1 problem (0 errors, 1 warning)
=== [6/6] Frontend: unit tests + production build ===
Test Files  2 passed (2) / Tests  11 passed (11)
✓ built in 4.77s
=== [6b/6] Frontend: dependency vulnerability scan (informational) ===
4 vulnerabilities (3 moderate, 1 high) (see docs/RELIABILITY_AND_OPERATIONS.md §10)
=== ALL CHECKS PASSED ===
```

`python ../scripts/e2e_acceptance_test.py`, run against the real seeded
demo database, real HTTP calls end to end:

```
ALL ACCEPTANCE CHECKS PASSED — real HTTP calls, real seeded database, no simulated steps.
```

Both were run after every Phase 8 documentation and code change, not
before — this is the actual, current state of the repository, not a
snapshot from earlier in the build.

## Final validation results (Phase 9)

`./scripts/ci_check.sh`, actually run from the repository root after every
Phase 9 code and documentation change, end to end:

```
=== [2/6] Backend: migrations apply cleanly to a fresh database ===
...8 migrations applied in order, including 604e27f002c4 (copilot
conversations and messages)...
No new upgrade operations detected.
=== [3/6] Backend: full test suite ===
301 passed, 2 warnings in 78.92s
=== [3b/6] Backend: dependency vulnerability scan (informational) ===
Found 19 known vulnerabilities in 5 packages (see docs/RELIABILITY_AND_OPERATIONS.md §10)
=== [5/6] Frontend: lint + typecheck ===
✖ 1 problem (0 errors, 1 warning)
=== [6/6] Frontend: unit tests + production build ===
Test Files  3 passed (3) / Tests  15 passed (15)
✓ built in 4.84s
=== [6b/6] Frontend: dependency vulnerability scan (informational) ===
4 vulnerabilities (3 moderate, 1 high) (see docs/RELIABILITY_AND_OPERATIONS.md §10)
=== ALL CHECKS PASSED ===
```

`python ../scripts/e2e_acceptance_test.py` (the Phase 8 investigation
workflow) still passes unchanged after every Phase 9 addition:

```
ALL ACCEPTANCE CHECKS PASSED — real HTTP calls, real seeded database, no simulated steps.
```

`python ../scripts/e2e_copilot_acceptance_test.py`, run against the real
seeded demo database, real HTTP calls end to end — including a deliberate
insufficient-evidence question, an explicit evidence-selection reference,
and a live proof that the grounding safeguard catches a fabricated
citation and an overclaim through the real API/DB path with no LLM call
involved:

```
ALL COPILOT ACCEPTANCE CHECKS PASSED — real HTTP calls, real seeded database, real grounding safeguard, no simulated evidence.
```

All three were run after every Phase 9 documentation and code change,
not before — this is the actual, current state of the repository, not a
snapshot from earlier in the build.

## Final validation results (Phase 13 — repository recovery & full-system audit)

Phase 13 was a verification/repair pass, not a new feature prompt: a
from-scratch audit of everything Phases 1–12 actually built, against the
real repository rather than prior phases' own summaries of themselves.
Headline finding: **the application code was not broken.** Everything
below that needed a real fix was in test/demo tooling, not in `app/`.

`./scripts/ci_check.sh`, run from the repository root:

```
=== [3/6] Backend: full test suite ===
494 passed, 2 warnings in 121.67s
=== [3b/6] Backend: dependency vulnerability scan (informational) ===
27 known vulnerabilities in 5 packages (ecdsa, pyasn1, pytest, python-jose,
starlette — see docs/RELIABILITY_AND_OPERATIONS.md §10 for the current count)
=== [5/6] Frontend: lint + typecheck ===
✖ 1 problem (0 errors, 1 warning) — unchanged, pre-existing (AuthContext.tsx)
=== [6/6] Frontend: unit tests + production build ===
Test Files  3 passed (3) / Tests  15 passed (15)
✓ built in ~5s
=== [6b/6] Frontend: dependency vulnerability scan (informational) ===
16 vulnerabilities (6 moderate, 8 high, 2 critical) — overwhelmingly in
dev/build tooling (vitest's tinypool, tailwindcss's build chain), not
shipped runtime code; react-router is the one exception that does ship —
see docs/RELIABILITY_AND_OPERATIONS.md §10
=== ALL CHECKS PASSED ===
```

**A real, reproducible bug was found and fixed in `scripts/e2e_acceptance_test.py`**
(the Phase 8 flagship end-to-end script), on a from-scratch run against a
freshly migrated, freshly seeded database — exactly the sequence SETUP.md
itself documents:

1. `TestClient(app, raise_server_exceptions=False)` was never used as a
   context manager, so FastAPI's startup event — which registers the
   background-job handlers — never ran. Any step that crossed into the
   async job path (anomaly detection over a large scoring window) failed
   with `"No handler registered for job type 'anomaly_detection_run'."`
   `scripts/e2e_scenario_acceptance_test.py` (Phase 11) had already hit
   and fixed this exact issue with an explicit comment explaining it; that
   same fix is now ported into this script too.
2. The script's self-heal path (used when a fresh database has no
   anomalies yet) filtered detectors by `status == "active"` — a value
   `DetectorResponse.status` (`ModelVersionStatus`) can never hold
   (trained/failed/archived only; "active" is the separate MLOps
   `ComponentVersion.lifecycle_stage`). The filter could never match
   anything, and a completely fresh database (no detector ever trained)
   had nothing for it to fall back to anyway, crashing with an unhandled
   `IndexError`. The script now trains a real robust z-score detector
   against the sensor's own observed history when none exists, and polls
   the resulting async job to completion rather than assuming it finished
   synchronously.

With both fixed, `python ../scripts/e2e_acceptance_test.py` passes clean
end to end from a freshly seeded database — login, assets/sensors,
measurements, data-quality context, a real trained-and-run detector,
anomaly context/explanation, provenance, condition attention list,
investigation creation, evidence attachment, annotation, status
transition, append-only verification, and the honest-failure-path checks
(404s, an empty-window response, admin-only metrics access) — all against
real HTTP calls through real application code:

```
ALL ACCEPTANCE CHECKS PASSED — real HTTP calls, real seeded database, no simulated steps.
```

`python ../scripts/e2e_copilot_acceptance_test.py` and
`python ../scripts/e2e_scenario_acceptance_test.py` both passed unchanged
(no application-code defects found in either):

```
ALL COPILOT ACCEPTANCE CHECKS PASSED — real HTTP calls, real seeded database, real grounding safeguard, no simulated evidence.
ALL SCENARIO SIMULATION ACCEPTANCE CHECKS PASSED — real HTTP calls, real seeded database, real historical data perturbed non-destructively, real analytical pipeline reused, every result labeled SIMULATED end to end.
```

`python ../scripts/run_evaluation.py` (Phase 10's evaluation pipeline)
also ran clean, refreshing `evaluation_results/latest_evaluation_run.{json,md}`.

**Also found and fixed in this pass:** this README's own doc table and
phase-by-phase history stopped at Phase 9 — `docs/SCENARIO_SIMULATION.md`
(Phase 11) and `docs/CLOSED_LOOP_FEEDBACK.md` (Phase 12) were missing
from the table entirely, and "Future directions (explicitly not built)"
still listed "a verified-outcome/training-label workflow" as unbuilt when
Phase 12 had in fact built exactly that. Both are corrected above.

See the Phase 13 completion report delivered alongside this codebase for
the full capability-by-capability evidence matrix, the security/data-
integrity audit findings, and the handoff list for Phase 14.

## Final validation results (Phase 14 — advanced industrial AI/ML intelligence engine)

Phase 14 asked for a substantial, integrated improvement to the
platform's real industrial intelligence capabilities, continuing from
Phases 1–13 rather than rebuilding anything working. The repository
audit (re-reading the actual anomaly/condition/forecasting code, not
prior phases' summaries of it) found two genuinely trainable ML
components already present (Isolation Forest, a backtest-gated linear
forecaster) and one clear, real, previously-unaddressed capability gap:
nothing in the anomaly layer could catch a point where every sensor
looks individually normal but the *relationship between sensors* has
broken down — robust z-score only ever sees one sensor, and Isolation
Forest's tree-ensemble score has no closed-form statistical meaning for
"these two sensors stopped moving together." That gap, not a larger or
more fashionable algorithm, is what this phase built against (per its
own instruction: "if the repository is already stronger than a proposed
capability, verify and focus on remaining gaps").

**What was built:** `app.anomaly.mahalanobis_detector` — a robust-
covariance (`sklearn.covariance.MinCovDet`) multivariate anomaly
detector, a third `DetectorType` wired through the exact same
`ModelVersion`/`ModelRun`/`Anomaly` lifecycle the existing two detectors
already use (zero new tables, zero Alembic migrations — `DetectorType` is
a plain-string column by design). Full detail in
[docs/ANOMALY_DETECTION.md](../ANOMALY_DETECTION.md) §1a/§22; the
MLOps registry change this required (Mahalanobis shares the
`TRAINED_ML_MODEL` component type with Isolation Forest, and retraining
now resolves detector_type from the component's own history rather than
assuming one fixed type per component type) is in
[docs/MLOPS_GOVERNANCE.md](../MLOPS_GOVERNANCE.md) §24.

**Real, executed evidence this is a genuinely new capability, not a
restatement of an existing one** — the central claim of this phase, and
the one most worth independently checking: a single test,
`tests/test_mahalanobis_detector.py::test_mahalanobis_catches_what_
univariate_zscore_cannot`, builds one deterministic, seeded,
honestly-labeled synthetic scenario (two sensors that normally move
together; a window where one is resampled from its own normal marginal
distribution but decoupled from the other, so neither sensor's own
statistics look unusual) and asserts, in the same test run, both that the
new detector recovers it (`precision=1.0, recall=1.0`) and that the
existing robust z-score detector run independently on each sensor does
not (`recall=0.0`). Like every other evaluation number in this platform,
this is pipeline-correctness evidence against synthetic, code-defined
ground truth — never a real-world accuracy claim (see
[docs/ANOMALY_DETECTION.md](../ANOMALY_DETECTION.md) §21).

Full backend suite, run from a clean checkout:

```
cd backend && source .venv/bin/activate && python -m pytest -q
512 passed, 3 warnings in 123.23s
```

(494 confirmed passing at the start of this phase, per the Phase 13
report; +14 new pure-function tests for the detector and its synthetic
scenario generator; +4 new API-level tests — 2 proving the detection
capability through the real HTTP API and real DB, 2 proving the MLOps
registry change — zero regressions anywhere else.) Frontend: `npx tsc -b`
— zero TypeScript errors after adding the third detector type to the
training form, sensor-selection copy, and three previously-hardcoded
label ternaries in `IntelligencePage.tsx`.

**A bug this phase's own development found and fixed before it could
reach a test file, documented rather than quietly corrected:** the first
version of the new synthetic multivariate scenario generator gave its
"two sensors normally move together" reference relationship almost no
real signal (an under-varying random walk drove the correlation down to
≈0.16), which made nearly every scoring point get flagged regardless of
whether the injected breakdown was nearby — caught by directly computing
the reference-period correlation rather than trusting the scenario's own
docstring, fixed by giving the driving sensor a real daily-cycle signal,
and locked in permanently by
`test_detect_flags_nothing_on_a_well_behaved_relationship`. A second,
smaller bug (flagged points reported as one row per grid point instead of
one merged event per sustained incident, inconsistent with this
platform's own existing `robust_zscore` convention and with this phase's
explicit instruction not to count every point in a sustained interval
separately) was found and fixed the same way, with its own regression
test.

**What this phase deliberately did not do:** build a supervised
predictive-maintenance model (still no failure/maintenance-outcome labels
exist — unchanged since Phase 13's audit), integrate forecast-residual
analysis into the anomaly layer (a real, open gap — `app.condition.
forecasting` and the anomaly layer remain independent, which is the
honest state to report rather than a rushed, under-tested integration),
or add a fourth detector merely for breadth — the brief's own
prioritization explicitly ranks "a small set of correctly implemented,
tested, well-evaluated methods" above "a large collection of disconnected
algorithms," and one well-justified, well-tested, fully-integrated
addition is what that principle calls for here.

See [`PHASE_14_REPORT.md`](./PHASE_14_REPORT.md)
for the full §A-N evidence report and the handoff list for Phase 15.

## Final validation results (Phase 15 — unified MLOps, evidence-grounded AI copilot, scenario lab, governed decision intelligence)

Phase 15 asked for integration improvements across four already-built
capability areas (governed MLOps lifecycle, the evidence-grounded copilot,
the Scenario Lab, and closed-loop outcome feedback) — explicitly not a
rewrite, and explicitly not a mandate to touch all four shallowly. Phase A
discovery (three parallel read-only agents, each reading real source and
real tests in one capability area) found the MLOps lifecycle itself already
mature through Phases 5 and 14, so this phase targeted the three highest-
value, independently-identified, well-scoped gaps instead — one per
remaining area — each reusing 100% of its area's existing architecture
rather than adding a parallel code path:

1. **Copilot prompt-injection defense-in-depth** (`app.copilot.grounding`).
   Evidence text was already protected on the *output* side
   (`validate_response_grounding()`, Phase 9) but not on the *input* side:
   a human-authored evidence field — an investigation annotation, an
   outcome description, a verification note — could contain text shaped
   like a model instruction and reach the prompt unmodified. Added
   `sanitize_evidence_text()`, applied to every evidence item inside
   `build_evidence_block()`, plus explicit `<evidence_data>` delimiters and
   a new system-prompt rule telling the model to treat that tagged content
   as data only. This is additive, not a replacement — the output-side
   safeguard still runs afterward regardless. See
   [docs/COPILOT.md](../COPILOT.md) §9.
2. **Scenario Lab `missing_data_gap` perturbation** (`app.simulation.
   perturbations`/`validation`/`pipeline`). The Scenario Lab supported five
   ways to *change* a value (level shift, trend, variability, spike, drift)
   but none to *remove* one — the "missing-data interval" explicitly named
   in this phase's own spec. Added a sixth, equally pure/deterministic
   perturbation type whose `magnitude` is deliberately **not** a sensor-
   units value like every other type — it's a `[0, 1]` drop fraction — with
   its own validation bound (bypassing the generic sensor-units magnitude
   check rather than silently reusing logic built for a different kind of
   value) and deterministic, rank-based (never random) partial-fraction
   thinning. `app.simulation.pipeline` drops the resulting NaN rows once, at
   the single choke point every reused analytical function flows through,
   so a simulated gap presents to those functions exactly as a real one
   would — as fewer rows, not a placeholder value. See
   [docs/SCENARIO_SIMULATION.md](../SCENARIO_SIMULATION.md) §1/§5.1/§5.2/§5.4.
3. **Outcome report-lag leakage check** (`app.feedback.matching`/
   `app.services.feedback_service`). The existing leakage check already
   caught an outcome whose own timestamp fell after an evaluation period,
   and a verification that happened after the run's `computed_at` — but not
   the distinct case this phase's spec explicitly calls out: "time an
   event occurred" vs. "time it became known to the platform." A human can
   truthfully report *today* that something happened *last week*, producing
   an `outcome_timestamp` safely inside an old evaluation period while the
   report itself didn't exist in this system until today. Added a third
   leakage shape to `evaluation_run_leakage_check` comparing
   `Outcome.created_at` (the report's own arrival time, newly documented as
   exactly that) against the run's `computed_at`, wired into the only real
   caller, `run_evaluation()`. See
   [docs/CLOSED_LOOP_FEEDBACK.md](../CLOSED_LOOP_FEEDBACK.md) §1–§3/§10.

**Testing, run from a clean checkout:**

```
cd backend && source .venv/bin/activate && python -m pytest -q
539 passed, 3 warnings in 129.99s
```

(539 confirmed passing, independently re-run at the end of this phase's
session; Phase 14's own README section reported 512 at its end, though
that figure was not independently re-verified at the start of this phase
before any edit, so the precise delta is not asserted here — only the final
539 is first-hand, just-executed evidence). New tests added directly by
this phase, each individually collected and run: 9 for copilot
sanitization [7 unit in `test_copilot_grounding.py` + 2 API-level in
`test_api_copilot.py`], 17 for the Scenario Lab addition [8 unit in
`test_simulation_perturbations.py::TestMissingDataGap` + 6 unit in
`test_simulation_validation.py::TestMissingDataGapValidation` + 3
API-level in `test_api_scenario.py::TestMissingDataGapScenario`] plus one
existing parametrized test widened (not duplicated) to cover the new type,
and 4 for the feedback leakage check in
`test_feedback_matching.py::TestEvaluationRunLeakageCheck` — zero
regressions in any pre-existing test anywhere in the suite. Frontend:
`ScenarioType`, `SCENARIO_TYPE_LABELS`, and the duration/magnitude field
labels in `ScenarioLabPage.tsx` were updated for the new type; `cd frontend
&& npx tsc -b` was re-run and exits 0 with zero TypeScript errors.

**What this phase deliberately did not do:** touch the MLOps governance
tables, model lifecycle states, drift-monitoring methodology, or retraining
gates — Phase A's own discovery found that area already mature and the
brief's own prioritization (§19) ranks a smaller, fully-integrated,
fully-tested set of changes above a wide, shallow pass across every section
of a 20+ phase spec; did not add a dedicated database-level integration test
reproducing the report-lag leak through a live `run_evaluation()` call (the
existing precedent for both of the leakage check's first two shapes is also
unit-level-only, in `test_feedback_matching.py` — this phase matched that
precedent rather than inventing a new testing tier for one new shape); and
did not add or run a frontend component/visual test for the new
`missing_data_gap` option in `ScenarioLabPage.tsx` beyond the typecheck
above — this frontend remains covered only by `tsc`, not by any
component-level test suite, which is unchanged from every prior phase.

See [`PHASE_15_REPORT.md`](./PHASE_15_REPORT.md)
for the full §A-O evidence report and the handoff list for Phase 16.

## Final validation results (Phase 16 — enterprise productization, production hardening & acceptance testing)

Phase 16 asked for a full-system re-verification pass — not a rewrite —
across security, reliability, AI/ML validity, testing, documentation, and
portfolio readiness, with an explicit instruction not to duplicate
capabilities already built. Reconnaissance (direct inspection plus two
parallel read-only audit agents — one a cross-system "dead UI / fake data /
placeholder" sweep per the phase's own Part 17, the other a targeted
security re-audit of the four feature areas added after Phase 7's original
hardening pass: copilot, Scenario Lab, MLOps, outcome feedback) confirmed
that Phases 7 and 13 had already built and verified nearly everything
Parts 3–5 and 12–13 of this phase's spec call for (health/readiness
separation, structured logging with correlation IDs, rate limiting, timing-
attack-safe auth, production-config guards, job timeouts/dedup, migration
safety, documented performance measurements). This phase's strategy was
therefore to re-verify that work under the current code rather than
rebuild it, and to find and fix whatever genuine gaps remained.

**The one confirmed, fixed gap:** `GET /api/v1/copilot/suggested-questions`
(`app/api/v1/copilot.py`) had **no authentication dependency at all**,
directly contradicting this module's own docstring claim that "every route
here requires authentication." Fixed by adding
`user: User = Depends(get_current_user)` (deliberately not
`organization_scope` — the response is static per `context_type` and
carries no tenant data, so there is nothing to scope by organization; the
fix is requiring a valid session, not adding an org filter with no real
target). A regression test, `test_suggested_questions_requires_authentication`,
was added to `backend/tests/test_api_copilot.py` immediately after the
analogous `/status` test. Everything else surfaced by both audit agents —
MLOps governance transitions, Scenario Lab authorization, outcome feedback
tenant isolation, and the rest of the copilot router — was confirmed
already correctly scoped and authenticated; no other code change was made.

**Testing, run from a clean checkout:**

```
cd backend && source .venv/bin/activate && python -m pytest -q
544 passed, 3 warnings
```

```
cd frontend && npm run lint && npx tsc -b && npx vitest run && npm run build
# lint: 1 pre-existing warning, 0 errors
# typecheck: 0 errors
# vitest: 15 passed
# build: succeeded (pre-existing chunk-size warning only)
```

```
cd backend && alembic upgrade head && alembic check   # clean, no pending migrations
python scripts/e2e_acceptance_test.py            # passed
python scripts/e2e_copilot_acceptance_test.py    # passed
python scripts/e2e_scenario_acceptance_test.py   # passed
python scripts/run_evaluation.py                 # re-run fresh this phase; passed
```

Dependency audits were re-run and show real count increases against the
figures documented in earlier phases — `pip-audit`: 27 vulnerabilities
across 5 packages (previously 19); `npm audit`: 16 vulnerabilities, 6
moderate/8 high/2 critical (previously 4). Neither increase traces to a
change made in this phase: both are new CVE disclosures against versions
that were already pinned and already reasoned about in prior phases'
documentation (the npm increase is almost entirely dev-tooling, via
`vitest`/PostCSS, plus the already-assessed `react-router` entry). No
dependency bump was made without a dedicated validation pass, consistent
with this project's existing policy on major-version upgrades.

**What this phase deliberately did not do:** re-measure performance
figures already measured and documented in Phase 7's own session (this
phase re-read and re-confirmed them rather than re-running a timing pass
that would only reproduce the same numbers against the same seeded
database); attempt the container-registry build validation that Phase 7
and Phase 13 both already documented as blocked by this sandbox's network
policy (confirmed again, not re-litigated); or chase down the exact
historical test-count delta between Phase 15's reported 539 and this
phase's freshly-run 544 — the current, just-executed 544 is reported as
first-hand evidence without asserting an unverifiable precise before/after
split for one added test.

**Remaining gaps, honestly carried forward:** the production container
build is still unvalidated against a real registry (sandbox network policy
only, not a code defect); CI (`.github/workflows/ci.yml`) mirrors
`scripts/ci_check.sh` but has still never executed on a real GitHub Actions
run, since the repository has no git remote in this environment; general
POST-request idempotency beyond the copilot/scenario endpoints' own
`dedup_key` pattern remains undocumented as a platform-wide guarantee; there
is no automated backup job, only a documented manual procedure; and
`app/services/mlops_service.py` lines 211/225 carry a minor, non-exploitable
note (no defensive `org_id` filter at that call site) left as a Best Next
Action rather than fixed, since no audit agent found a real cross-tenant
path to it.

**Final readiness assessment (kept separate, not collapsed into one
verdict):** local development — ready, fully scripted. Demo — ready, with
the honestly-labeled synthetic dataset documented in prior phases.
Automated-testing maturity — strong at the unit/API/E2E-script level (544
backend + 15 frontend + 3 real E2E acceptance scripts + 1 ML-evaluation
harness, all passing), weaker at frontend component/visual coverage and
real-CI execution. Security posture — solid for the threat model this
platform targets (tenant isolation, real server-side auth/authz, rate
limiting, timing-safe comparisons, production-config guards), with the one
gap found this phase now fixed and regression-tested. ML-evaluation
credibility — honest and mechanism-level (explicit leakage checks, real
baselines, uncertainty never overstated as probability), explicitly not
validated against a real LLM judgment layer. Deployment readiness —
configuration and runbook complete, container-build validation against a
real registry still outstanding.

See [`PHASE_16_REPORT.md`](./PHASE_16_REPORT.md)
for the full evidence report and best-next-actions handoff.
