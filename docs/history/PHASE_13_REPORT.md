# Phase 13 Completion Report — Repository Recovery, Full-System Integration & Foundation Hardening

**Scope of this phase**: audit, not rebuild. Phases 1–12 were treated as
prior specs, not proof of a working system — every claim below was
re-verified against the real repository, real tests, and real HTTP calls
in this session, not carried over from earlier phases' own summaries.

**Headline finding**: the application code was not broken. The repository
was found in a healthy, fully-passing state (494/494 backend tests, clean
migrations, clean frontend build). The one genuinely broken thing in this
audit was in test/demo tooling (`scripts/e2e_acceptance_test.py`), not in
`app/` — found by actually running the documented setup-to-acceptance-test
sequence from a fresh database, exactly as a new developer would, and
tracing the first real failure to its root cause rather than assuming the
repository was intact because it compiled.

---

## A. Repository state

- **Not a git repository.** `git status` → `fatal: not a git repository`.
  There is a `.gitignore` but no `.git` directory anywhere in the tree, no
  commit history, no branches. This is a pre-existing condition (confirmed
  by the environment's own "Is a git repository: false" signal), not
  something this phase caused or could restore — there is no history to
  recover. This is stated plainly rather than worked around.
- **Stack** (verified from source, not inferred): FastAPI + Python 3.11
  backend (`backend/app/`), SQLAlchemy 2.0 + Alembic migrations, SQLite
  (WAL mode) at `backend/data/pred_platform.db`, JWT (HS256) + bcrypt auth;
  React 18 + TypeScript + Vite frontend (`frontend/`) with TanStack Query
  and Recharts. Single deployable backend (in-process asyncio job runner,
  no broker), single deployable frontend SPA — a deliberate modular
  monolith (`ARCHITECTURE.md` §3–4), not an unfinished microservices plan.
- **Environment**: `backend/.venv` and `frontend/node_modules` were both
  already present and populated — no reinstall needed. The dev database
  file existed (528 KB) but **every domain table was empty** (0 users, 0
  organizations, 0 measurements) — schema-only, from migrations having
  been applied with no synthetic seed ever loaded in this container. This
  was corrected in Phase C below using the project's own documented,
  idempotent seed command (`python -m app.synthetic.cli --seed 42 --days
  21`) — not a workaround, the exact step SETUP.md itself specifies.
- **CI**: `.github/workflows/ci.yml` exists, mirrors `scripts/ci_check.sh`
  exactly, and — as its own header honestly states — has never actually
  been run by GitHub Actions in this environment (no git remote/runner
  attached). `scripts/ci_check.sh` is real and was run directly, multiple
  times, in this session; see §E for full output.
- **17 docs under `docs/`** plus root-level `README.md`, `ARCHITECTURE.md`,
  `SETUP.md` cover every prior phase. Two were found missing from
  `README.md`'s own doc table despite existing on disk and being fully
  real (`docs/SCENARIO_SIMULATION.md`, Phase 11; `docs/CLOSED_LOOP_FEEDBACK.md`,
  Phase 12) — fixed in this phase (§D).
- **SQL-injection posture**: every `db.execute(...)` call site in the
  backend was located (`grep` across `app/` for raw-SQL patterns) and
  manually inspected: `stats_service.py`, `quality_service.py`,
  `ingestion_service.py`, `timeseries/data_access.py` (the actual module —
  not `services/timeseries/data_access.py`, which doesn't exist; the real
  path is `app/timeseries/data_access.py`), `health.py` (constant
  `text("SELECT 1")`, no interpolation), `db/session.py` (constant SQLite
  `PRAGMA` strings). Every one uses parameterized SQLAlchemy Core
  `select()`/`func` constructs or bound `.where()` clauses. **No
  string-interpolated or f-string-built SQL exists anywhere in the
  backend.**
- **No file-upload endpoint exists anywhere in the API** (`grep -r
  "UploadFile"` across `app/api/v1/` returns nothing). Ingestion is a pure
  JSON-body contract (`POST /ingestion/measurements`, `POST
  /ingestion/batches`) — there is no multipart file upload, and therefore
  no user-supplied file path to validate or sanitize. This narrows Phase D
  /Phase I's "safe file parsing" and "file upload/path handling" checklist
  items to "not applicable — no such surface exists," which is itself a
  verified finding, not an assumption.

## B. Phases 1–12 status (capability matrix)

Grounded in: direct source inspection, the 494-test suite, and four live
end-to-end scripts actually executed this session against the real
application and real seeded database (not fixtures).

| # | Capability | Status | Evidence |
|---|---|---|---|
| 1 | Foundation — assets/sensors, ingestion, dedup, data quality, background jobs | **Real, working** | 47,488 real synthetic records ingested live this session (3 assets, 8 sensors, 15 injected duplicates correctly deduped); `ARCHITECTURE.md` module boundaries match actual `app/` tree |
| 2 | Temporal intelligence — profiling, resampling, feature framework, leakage-safe splitting | **Real, working** | Exercised live via `/temporal-analysis` equivalent API calls in Phase C; profiling/trend code unit-tested (`test_profiling.py`, passing, including the flat-series edge case) |
| 3 | Anomaly detection — robust z-score + Isolation Forest, reference/scoring separation, detector lifecycle | **Real, working** | Trained a real detector live this session, ran real detection, got 3 real anomalies with real explanations (§C); `test_robust_zscore_full_workflow_e2e` and peers pass |
| 4 | Condition intelligence & forecasting — degradation evidence, backtest-gated forecaster | **Real, working** | Attention-list endpoint exercised live (§C step 8); forecaster's backtest-gating logic covered by `test_forecasting_component_validate_blocked_when_not_justified` (passing) — a forecast is refused, not faked, when it doesn't beat baseline |
| 5 | MLOps lifecycle & governance — component/version registry, 8-stage lifecycle, shift analysis | **Real, working** | `test_isolation_forest_full_lifecycle_e2e` (train→register→evaluate→validate→approve→activate→score→retrain→activate-v2→rollback) passes end to end against the real API; governance-event audit trail verified non-empty and covering the full sequence |
| 6 | Investigation & orchestration — evidence graph, timeline, prioritization | **Real, working** | Exercised live this session (§C steps 9–12): investigation created, evidence attached by reference, annotation added, status transitioned, append-only history confirmed (original anomaly evidence unchanged after being referenced) |
| 7 | Production reliability — config validation, rate limiting, security headers, CI | **Real, working** | `app/main.py`/`app/core/config.py` read in full this session: fail-fast production-safety validator (bad JWT secret / unsafe CORS+credentials / in-memory SQLite all refuse to start when `APP_ENV=production`), correct middleware ordering (body-size limit → security headers → CORS → correlation ID) |
| 8 | Flagship finalization — docs, final E2E acceptance test | **Real, with one bug found+fixed this session** | See §C/§D — the flagship `e2e_acceptance_test.py` had a real defect (never executed from a fresh DB until this session's audit); now fixed and passing |
| 9 | Evidence-grounded AI copilot | **Real, working** | `e2e_copilot_acceptance_test.py` run live this session, all checks passed: honest "not configured" evidence-only mode, grounding on real retrieved evidence, honest handling of an evidence-free asset, and — the strongest proof point — the architectural grounding safeguard caught a fabricated citation **and** an overclaim from a deliberately adversarial fake provider in a live run |
| 10 | Scientific validation — reproducible evaluation pipeline | **Real, working** | `scripts/run_evaluation.py` run live this session; completed cleanly, regenerated `evaluation_results/latest_evaluation_run.{json,md}` |
| 11 | Scenario simulation / what-if analysis | **Real, working** | `e2e_scenario_acceptance_test.py` run live this session, all checks passed: non-destructive guarantee verified byte-identical, absurd magnitude honestly rejected (not clamped), sensitivity sweep bound enforced, multi-sensor independence stated explicitly, copilot grounding labels scenario results SIMULATED even when linked into an investigation |
| 12 | Closed-loop intelligence / outcome feedback | **Real, working** | 24 leakage-prevention and feedback-matching tests pass (`pytest -k "leakage or feedback_matching"`), including the exact check Phase 13 calls out by name: an outcome-feedback record cannot be used before its own verification time |

**No capability in this list was found to exist only as empty files or
unimplemented stubs.** The honest gaps that do exist (no failure
prediction/RUL model — no failure labels exist to train one on; container
build never executed in this sandbox; copilot never called a real
Anthropic API in this environment) are the project's own stated,
deliberate scope boundaries, not undiscovered holes.

## C. End-to-end workflow — what was exercised, what failed, what was fixed

The documented path (`SETUP.md`) was followed exactly as a new developer
would, starting from the actual state found (empty DB):

1. `alembic upgrade head` — already applied; `alembic check` reports zero
   drift (11-revision chain).
2. `python -m app.synthetic.cli --seed 42 --days 21` — loaded the real,
   documented synthetic demo dataset (3 assets, 8 sensors, 47,488 accepted
   / 15 duplicate / 0 rejected records) into the previously-empty DB.
3. `python ../scripts/e2e_acceptance_test.py` — **failed** at step 2
   (login): the demo DB had no seeded user before step 2 ran. This was
   expected and resolved by step 2 above, not a code defect.
4. Re-run: **failed** at step 5 (anomaly detection) with an unhandled
   `IndexError`. Root-caused by direct API experimentation (not guesswork):
   - `DetectorResponse.status` is `ModelVersionStatus`
     (trained/failed/archived — confirmed in `app/models/intelligence.py`).
     There is no reachable `"active"` value on this field; that status
     lives on the separate MLOps `ComponentVersion.lifecycle_stage`. The
     script's self-heal filter (`status == "active"`) could therefore
     never match any detector, on any database, ever.
   - Because this is a freshly seeded DB with zero detectors ever trained,
     the self-heal path had nothing to run anyway, and the script crashed
     on `anomalies[0]` instead of failing with a clear assertion.
   - Separately, `TestClient(app, raise_server_exceptions=False)` was
     never used as a context manager, so FastAPI's startup event (which
     calls `register_job_handlers()`) never ran — confirmed by directly
     querying `background_jobs` rows and finding
     `"No handler registered for job type 'anomaly_detection_run'."` This
     is a known, previously-solved issue in this same codebase:
     `scripts/e2e_scenario_acceptance_test.py` (Phase 11) already carries
     an explicit comment and fix for exactly this gotcha — it just was
     never ported back into the Phase 8 flagship script.
5. **Fixed** both issues in `scripts/e2e_acceptance_test.py` (full diff
   described in §D) and re-ran from the same (now-seeded) database:
   **all 13 steps passed** — login, assets/sensors, real measurements,
   real data-quality summary, a real detector trained and run live
   (3 real anomalies produced), anomaly explanation/context, provenance
   tracing to the real detector version, condition attention-list,
   investigation creation, evidence attachment by reference, annotation,
   status transition, append-only-history verification, and the honest
   failure-path checks (404 on a nonexistent asset, a 200-with-explicit-
   insufficient-data response for an empty time window, admin-only
   metrics access).
6. `e2e_copilot_acceptance_test.py` and `e2e_scenario_acceptance_test.py`
   were also run live against the same now-seeded database: **both passed
   with no code changes needed.**
7. `scripts/run_evaluation.py` was run live: completed cleanly, all
   DB-backed checks passed, pure-module results written.
8. Full backend suite re-run after all fixes: **494 passed, 0 failed**
   (121.67s). Full `scripts/ci_check.sh` re-run from the repository root
   after all fixes: **`=== ALL CHECKS PASSED ===`**.

**Nothing remains blocked in the core workflow.** The one thing that
cannot be exercised in this environment is a live Anthropic API call (no
`ANTHROPIC_API_KEY` configured) and an actual Docker image build (sandbox
network policy blocks the registry) — both pre-existing, explicitly
documented limitations, re-confirmed rather than newly discovered.

## D. Engineering changes actually implemented

All changes are in test/demo tooling and documentation — **no file under
`backend/app/` or `frontend/src/` was modified**, because no defect was
found there.

1. **`scripts/e2e_acceptance_test.py`** (root cause: §C):
   - Added `from app.jobs.handlers import register_job_handlers` +
     `register_job_handlers()` immediately after client construction,
     with a comment explaining the `TestClient` lifespan gotcha (mirroring
     the existing fix in `e2e_scenario_acceptance_test.py`).
   - Rewrote the step-5 self-heal block: corrected the detector filter to
     check `status == "trained"` against the sensor actually present in
     `detector_config["sensor_ids"]` (not the unreachable `"active"`
     value); when no usable detector exists, it now trains a real one via
     `POST /anomaly/detectors` against the sensor's own observed history
     (queried via `GET /stats/sensors/{id}`, reference = first 70% of
     history, scoring = the remainder); removed a dead/unused `sensor_id`
     field from the run-creation payload (`DetectionRunCreateRequest` has
     no such field — it was silently ignored by Pydantic, never
     functional); added polling of the resulting background job to actual
     completion (`GET /jobs/{id}`) rather than assuming the run finished
     synchronously, so the script's own reported result is never
     fabricated.
2. **`README.md`**:
   - Added the two missing doc-table rows (`docs/SCENARIO_SIMULATION.md`,
     `docs/CLOSED_LOOP_FEEDBACK.md`).
   - Added Phase 10/11/12 entries to the "Build history" narrative (which
     previously stopped at Phase 9 despite three more prompts of real,
     tested work existing in the repository).
   - Removed the now-false "a verified-outcome/training-label workflow"
     line from "Future directions (explicitly not built)" — Phase 12
     built exactly that.
   - Added a "Final validation results (Phase 13)" section with this
     session's real `ci_check.sh`/e2e output, and an explicit note on the
     stale "301 backend tests" paragraph pointing to the real, current
     count rather than silently rewriting history.
3. **Dev database**: loaded the synthetic demo dataset via the project's
   own idempotent seed command (no schema change, no destructive
   operation — the DB was empty beforehand). Running the e2e scripts
   added their own documented test rows (a few extra investigations,
   detectors, scenarios, conversations) — exactly the behavior SETUP.md
   describes ("writes real rows into your dev database each time it
   runs").

**Not changed, and why**: `docs/RELIABILITY_AND_OPERATIONS.md` §10's
dependency-vulnerability counts (19 backend / 4 frontend) are also stale
relative to the real current scan (27 / 16 — see §G) — flagged in §I/§J
rather than rewritten, since that document has substantial surrounding
prose tied to the old numbers and a careful rewrite is better scoped as
its own focused task than folded into this audit's time budget.

## E. Test evidence — exact commands run

| Command | Result |
|---|---|
| `alembic check` (backend/) | No new upgrade operations detected — zero drift, 11-revision chain |
| `python -m pytest -q` (backend/) | **494 passed**, 2 pre-existing unrelated `scipy` RuntimeWarnings, 121.67s |
| `python -m pytest -q -k "leakage or feedback_matching"` | **24 passed** — includes the outcome-feedback-before-verification-time leakage guard |
| `pip-audit` (backend/) | 27 known vulnerabilities, 5 packages (informational, non-blocking) |
| `npm ci && npm run lint` (frontend/) | Clean except 1 pre-existing non-blocking warning in `AuthContext.tsx` |
| `npm run typecheck` (frontend/) | Clean |
| `npm run test:run` (frontend/) | **15 passed**, 3 files |
| `npm run build` (frontend/) | Clean, 898 modules, standard >500KB chunk-size advisory only |
| `npm audit` (frontend/) | 16 vulnerabilities (6 moderate/8 high/2 critical), informational |
| `bash scripts/ci_check.sh` (root) | **`=== ALL CHECKS PASSED ===`** — run twice this session, once before and once after the fixes in §D, both green |
| `python ../scripts/e2e_acceptance_test.py` | Failed twice (root-caused in §C), **passed** after the fix in §D |
| `python ../scripts/e2e_copilot_acceptance_test.py` | **Passed**, unmodified |
| `python ../scripts/e2e_scenario_acceptance_test.py` | **Passed**, unmodified |
| `python ../scripts/run_evaluation.py` | **Completed cleanly**, all DB-backed checks passed |

**Checks that could not be executed**: a live call against the real
Anthropic API (no key configured — by design, per `docs/COPILOT.md`); an
actual Docker/Compose container build (sandbox network policy blocks the
registry, per `docs/RELIABILITY_AND_OPERATIONS.md` §1 — re-confirmed, not
newly discovered).

## F. ML integrity

- **Leakage prevention is structural, not just disciplined**: reference/
  scoring separation for both detectors is enforced in code and covered by
  `test_synthetic_scenario_ground_truth_stays_out_of_the_reference_fraction`
  and peers; the forecaster's backtest-vs-naive-baseline gate is enforced
  by `ForecastModel.status` refusing `.../runs` unless `status=justified`
  (verified: `test_forecasting_component_validate_blocked_when_not_justified`
  blocks validation with a 409 when the backtest doesn't justify it); the
  specific risk Phase 13 calls out by name — an outcome-feedback record
  used before its own verification time — has a dedicated, passing test
  guard (part of the 24 in §E).
- **No supervised failure classifier or RUL estimator exists**, and this
  is the right call, not a gap: `MaintenanceEvent` has zero rows in every
  environment this schema has run in, including the real seeded database
  re-checked this session. Building one would fabricate confidence against
  labels that don't exist.
- **Reproducibility**: the Isolation Forest refits deterministically
  (`random_state=42`) from immutable stored config every run, never
  pickled; the robust z-score detector is non-trainable (closed-form
  median/MAD); the forecaster's OLS fit is closed-form. All three were
  exercised live this session and produced the same class of real,
  traceable output each time.
- **Evaluation methodology**: `scripts/run_evaluation.py`'s six modules
  (robustness, data-quality impact, explainability, prioritization,
  copilot grounding, adversarial) ran clean this session against the real
  seeded data and real HTTP stack where applicable, and wrote real,
  re-generated output files rather than hand-edited ones.

## G. Security and operational risks

- **SQL injection**: no exploitable surface found (§A) — exhaustive for
  every `db.execute` call site, not a sample.
- **Multi-tenancy**: the pattern verified (e.g.
  `app/api/v1/measurements.py`'s `sensor_summary_stats`) is
  router-level ownership validation (`_assert_sensors_in_org`) before
  delegating to a service that doesn't re-filter by `organization_id`
  itself — correct and safe as implemented, but worth noting as a pattern
  a future endpoint could get wrong if it skips the router-level check;
  not found to be currently violated anywhere sampled.
- **File uploads**: no surface exists (§A) — nothing to audit, verified
  by absence rather than assumed.
- **Production config**: fail-fast validation (bad JWT secret / unsafe
  CORS+credentials / in-memory SQLite) confirmed real and active when
  `APP_ENV=production`, re-read in full this session.
- **Dependency vulnerabilities — real, current counts** (not the stale
  ones in `docs/RELIABILITY_AND_OPERATIONS.md` §10):
  - Backend: **27** across 5 packages — `ecdsa` (no fix listed),
    `pyasn1` (4 CVEs, fix available), `pytest` (dev-only, fix is a major
    bump), `python-jose` (runtime auth dependency, no fix listed —
    the one here most worth prioritizing), `starlette` (runtime, FastAPI
    transitive, fix available).
  - Frontend: **16** (6 moderate/8 high/2 critical) — overwhelmingly
    dev/build-tooling (`tinypool` via vitest is the one marked critical,
    but it only ever runs the local test process, never the shipped app);
    `react-router`/`react-router-dom` is the one finding here that does
    ship in the production bundle (moderate: open-redirect +
    constructor-injection) and is the one worth prioritizing on this side.
- **Limitation of this review**: this was a targeted audit (injection,
  tenancy pattern, config, file-upload surface, dependency scan), not an
  exhaustive line-by-line security audit of every endpoint. No independent
  third-party security audit has been performed on this codebase, and none
  is claimed here.

## H. Synthetic and simulated behavior (explicit inventory)

- **All demo/dev data is synthetic**, generated by
  `app/synthetic/cli.py` with a fixed seed — stated in the generator's own
  output and in `docs/SYNTHETIC_DATA.md`. No real industrial system or
  real failure data exists anywhere in this repository.
- **Scenario Lab results are simulated by construction** and labeled
  `SIMULATED SCENARIO RESULT` end to end, including inside copilot
  grounding text, even when a scenario result is linked into an
  investigation — verified live this session.
- **The AI copilot ran in its "no provider configured" evidence-only
  mode** for every check this session (no `ANTHROPIC_API_KEY` set) — this
  is the honest, documented default state, not a simulated success.
  Section 10 of the copilot e2e script did exercise a **deliberately
  adversarial fake provider** to prove the grounding safeguard catches a
  fabricated citation and an overclaim — that fake provider is clearly a
  test double, not a claim about a real model's behavior.
- **No heuristic output anywhere claims to be a measured fact**: condition
  evidence types are kept separate (trend, variance, distributional —
  no composite "health score"); anomaly severity tiers are explicitly
  distinguished from failure probability; the forecaster only produces a
  forecast when its own backtest justifies it.

## I. Remaining priorities (most important unresolved items)

1. **`docs/RELIABILITY_AND_OPERATIONS.md` §10's vulnerability counts are
   stale** (19/4 vs. the real current 27/16) — needs a focused rewrite
   pass, not folded into this phase.
2. **`python-jose` (backend, runtime) and `react-router`/`react-router-dom`
   (frontend, ships in the production bundle)** are the two dependency
   findings worth prioritizing over the rest, since the others are either
   dev-only or have no fix available yet.
3. **The demo-data seed script does not train/activate any detector or
   forecaster** — a brand-new clone has measurements but zero intelligence
   output until a human (or a script) trains one. This is arguably
   intentional (ANOMALY_DETECTION.md describes an inline "train new
   detector" UI form as the expected first action), but it is worth an
   explicit decision for Phase 14: either document it clearly as a
   required first manual step in SETUP.md, or extend the synthetic CLI to
   optionally bootstrap one trained-and-run detector for a smoother
   first-run demo experience.
4. **This repository still has no version control.** Every future phase
   inherits the same no-diff, no-history risk this one did. Initializing
   git (even without a remote) would materially de-risk future recovery
   work, though doing so was out of scope for an audit-and-repair prompt
   and wasn't attempted here without being asked.
5. See §G for the multi-tenancy pattern note (not a found violation, but
   worth a deliberate convention check before any new endpoint is added).

## J. Phase 14 handoff

Starting from this evidence-based baseline, concrete, verified items for
the next phase to pick up (none of these are newly-discovered defects
blocking core functionality — the core platform works):

- Refresh `docs/RELIABILITY_AND_OPERATIONS.md` §10 with the current
  dependency-vulnerability counts and re-evaluate `python-jose` and
  `react-router` specifically for an upgrade path.
- Decide and implement the demo-seed bootstrap question in §I.3.
- Consider initializing git for this repository, even locally with no
  remote, to stop future phases from inheriting a zero-history recovery
  risk.
- No application-code defects, leakage risks, or broken integrations were
  found that need carrying forward — Phase 14 can build on the current
  `app/` and `frontend/src/` trees as-is.
