# AI / ML Technical Methodology

This document consolidates, in one place, the scientific methodology behind
every intelligence capability in this platform. It does not replace the
per-layer documents — `docs/ANOMALY_DETECTION.md`, `docs/CONDITION_INTELLIGENCE.md`,
`docs/MLOPS_GOVERNANCE.md`, `docs/INTELLIGENCE_ORCHESTRATION.md` — which
carry the full derivation, code references, and test evidence for each
claim below. This document exists so a technically knowledgeable reader
can understand what this platform can and cannot claim without reading
four separate 300-line documents first.

**Written for one purpose**: to make the boundary between "this is a
measured pattern" and "this is a validated prediction" impossible to miss.

---

## 1. The central distinction: anomaly, condition signal, risk, and failure prediction

These four words get used loosely in industrial monitoring products. This
platform draws a hard, enforced line between them, because conflating them
is the single most common way monitoring software overstates what it knows.

| Term | What it means here | What it does NOT mean |
|---|---|---|
| **Anomaly** | A measurement or short window of measurements that is statistically unusual relative to a declared reference period, per a specific, named method (robust z-score or Isolation Forest) | Not a fault. Not necessarily even a real problem — it is evidence a human should look at. |
| **Condition signal** | Evidence about equipment condition derived from a longer observation window (trend, variance change, distributional shift) | Not a diagnosis. Not a remaining-useful-life estimate. |
| **Risk / prioritization** | A transparent, factor-exposing ranking (persistence, cross-detector agreement, data-quality context) used to order what a human should look at first | Not a probability of failure. This platform never emits a number that looks like "73% chance of failure" because nothing here was trained or validated to produce one. |
| **Failure prediction** | A supervised model trained on real labeled failure/maintenance outcomes that predicts a specific future fault | **Not implemented anywhere in this platform.** This repository's evidence audits (Phase 4 §0, re-confirmed Phase 5 §0 and Phase 6 §0) found zero failure or maintenance-outcome labels in the available data. Building a failure predictor without those labels would mean fabricating the exact kind of unearned confidence this project is built to avoid. |

Every other section of this document, and every intelligence output in the
product, is scoped by this table. If a claim doesn't fit one of the first
three rows, it isn't made.

---

## 2. Anomaly detection methodology

**Methods**: a robust z-score detector (median/MAD-based, resistant to the
outliers it's trying to detect — a plain mean/stdev z-score is not) and an
Isolation Forest detector (captures multivariate/nonlinear patterns the
univariate z-score can't). Both are real, fitted, versioned detectors —
not stubs. They were chosen and documented specifically for their
*complementary* strengths and limitations (`docs/ANOMALY_DETECTION.md` §1),
not because more methods looks more sophisticated.

**Baseline / reference-scoring separation**: every detector fit uses an
explicit reference period, and scoring only ever runs against data outside
that period — enforced structurally, not by convention, so a detector
cannot "learn" from the exact window it's being asked to judge
(`docs/ANOMALY_DETECTION.md` §2). This is the anomaly-detection analogue of
train/test separation, applied to time-ordered data where the more common
mistake (a random train/test split that leaks future information into the
past) is actively guarded against.

**Scoring architecture**: each method produces its own raw score and
severity tier; scores are **never cross-compared or combined into a single
number** across methods (`docs/ANOMALY_DETECTION.md` §3). A "multi-method
super-score" would be exactly the kind of fabricated composite this project
forbids — two different statistical methods on different scales don't
average into anything meaningful.

**Explainability**: every anomaly's explanation is derived from the real
calculation that produced it (which features/sensors deviated, by how
much, relative to what reference statistic) — never a templated or
model-generated sentence pretending to be an explanation
(`docs/ANOMALY_DETECTION.md` §9).

**Evaluation methodology — synthetic, and explicitly labeled as such**: this
platform has no ground-truth failure labels, so anomaly detection
performance cannot be validated against real outcomes. What exists instead
is a deterministic synthetic-scenario harness: known, injected anomalous
patterns scored against the real detector code, confirming the *pipeline*
works correctly (a real anomaly of a known shape is detected, a normal
period is not flagged) — explicitly **not** a real-world accuracy claim
(`docs/ANOMALY_DETECTION.md` §12–13). Any number derived from this harness
(precision/recall against the synthetic scenarios) describes pipeline
correctness, not production accuracy, and is labeled that way everywhere
it appears.

---

## 3. Condition intelligence & forecasting methodology

**Evidence audit before building anything**: Phase 4 began by re-auditing
what data actually exists and found no failure/maintenance labels — only
unlabeled sensor time-series. Every capability in this layer was scoped to
that reality rather than assumed (`docs/CONDITION_INTELLIGENCE.md` §0).

**Five independent condition-evidence types, no composite score**: trend,
variance/volatility change, distributional shift, and related signals are
each computed and shown independently. There is deliberately no single
"health score" combining them — a fabricated weighted average of
incompatible evidence types would look more polished and would mean less
(`docs/CONDITION_INTELLIGENCE.md` §1).

**Three-stage degradation classification, human-gated at the top**: a
conservative classification (based on sub-window persistence of evidence,
not a single noisy reading) has three stages, and the final ("validated")
stage is reachable **only** through an explicit, mandatorily-justified
human action — never automatically (`docs/CONDITION_INTELLIGENCE.md` §2).
The system will flag a candidate; it will not declare a stage 3
finding on its own authority.

**Forecasting, gated by an actual backtest**: a short-horizon forecaster
exists but only produces a forecast where a real, leak-free backtest on
held-out historical data shows it beats a naive baseline (e.g.
persistence/last-value). On this platform's actual demo data, that
backtest usually — honestly — does not favor the forecaster, and the
system says so rather than forecasting anyway
(`docs/CONDITION_INTELLIGENCE.md` §3). This is the single clearest example
in the codebase of the project's core discipline: build the honesty gate
first, and let it silence the feature when the evidence doesn't support
using it.

**Risk prioritization, not probability**: the attention-list ordering
surfaced in condition intelligence is transparent and factor-exposing
(which evidence types fired, how persistent they are) and is explicitly
**not** a probability of failure (`docs/CONDITION_INTELLIGENCE.md` §7).

**No remaining-useful-life estimation**: RUL estimation requires the same
failure-outcome labels this platform's audits confirmed don't exist. Not
implemented, not approximated, not implied.

---

## 4. Temporal validation methodology (leakage prevention)

Two different kinds of "the future leaking into the past" are both guarded
against, structurally, not by developer discipline alone:

1. **Anomaly baselines** — a detector's reference period is fixed before
   scoring and scoring never touches reference-period data (§2 above).
2. **Feature engineering / dataset building** (`docs/TEMPORAL_INTELLIGENCE.md`) —
   the feature framework enforces that a feature computed "as of" time *t*
   only reads data at or before *t*; chronological, rolling-window, and
   expanding-window splitting utilities exist specifically so a model
   trained in this platform's training-dataset builder cannot accidentally
   train on data that is, in wall-clock terms, from the future relative to
   its test set. This is tested directly (temporal-leakage-prevention
   tests in the Phase 2 test suite), not just documented as an intention.

---

## 5. MLOps evaluation methodology

**Task-specific gates, never a universal accuracy threshold**
(`docs/MLOPS_GOVERNANCE.md` §8): the one genuinely trained ML model
(Isolation Forest) has an evaluation contract different from the
statistical detector (robust z-score), which is different again from the
forecaster's backtest-vs-baseline gate, which is different again from the
heuristic signal methods (which are not "evaluated" in the ML sense at
all — they're deterministic rules, documented as such). A single
cross-cutting "must exceed 90% accuracy" rule would be scientifically
meaningless applied uniformly across a trained classifier, a statistical
threshold detector, and a rule-based heuristic — so no such rule exists.

**Data/feature shift analysis, not continuous drift monitoring**
(`docs/MLOPS_GOVERNANCE.md` §12): Welch's t-test and a KS-test are run
on-demand to compare a candidate scoring window against the reference
distribution a component was trained/validated on. This is discrete,
operator-triggered analysis — there is no continuously running drift
score, and none is claimed. PSI was deliberately not implemented (Welch's
t + KS already covers the same question at this project's scale without
adding a second, redundant statistic).

**Safe scoring contract**: every score returned by the platform traces
back to the exact component/version that produced it, and scoring
explicitly refuses (rather than silently falling back to a default) when
no version is active for a component (`docs/MLOPS_GOVERNANCE.md` §10).

---

## 6. Synthetic vs. real validation — the one distinction that matters most

Every validation claim in this platform's documentation falls into exactly
one of these two buckets, and every document says which:

- **Synthetic validation**: a deterministic generator produces known
  patterns (a labeled synthetic anomaly, a labeled synthetic degradation
  trend); the real application code is run against it; the result confirms
  the *pipeline* behaves correctly. This validates plumbing, not
  real-world predictive power.
- **Real-data validation (live, end-to-end)**: the real application code
  is run against the actual seeded demo dataset through the real HTTP API
  — confirming the system executes a genuine end-to-end workflow (ingest →
  detect → investigate → govern) without errors, using real database rows.
  This validates that the system *works*, not that its outputs are
  *accurate* — the demo dataset is synthetic sensor data, so "the anomaly
  detector correctly flagged the injected spike" is a pipeline-correctness
  result, not a real-world accuracy benchmark.

**Neither bucket is ever described as "real-world validation" or a
production accuracy claim.** No held-out real-world failure dataset exists
for this project to validate against, and none is claimed to.

---

## 7. Known assumptions and limitations (AI/ML specific)

- All demo/validation data is synthetic, generated by a documented,
  seeded generator (`docs/SYNTHETIC_DATA.md`). No claim in this platform
  is based on real industrial sensor history.
- No failure or maintenance-outcome labels exist anywhere in this
  project's data model or database. This is the reason supervised failure
  prediction and RUL estimation are not implemented — not an oversight.
- Isolation Forest and the forecaster are the only two components that are
  "trained" in the conventional ML sense; the robust z-score detector and
  the heuristic prioritization/change-detection methods are statistical/
  rule-based, not learned, and are documented as such rather than lumped
  in under a generic "AI" label.
- Anomaly and condition evidence quality is bounded by data quality — a
  sensor with poor coverage or many out-of-range readings produces
  correspondingly less trustworthy evidence, and the platform surfaces
  data-quality context alongside intelligence outputs specifically so a
  human reviewer can weigh that (§10 of `docs/ANOMALY_DETECTION.md`).
- Every human review, annotation, and validation action is stored as a
  distinct, append-only record, explicitly never merged into or confused
  with the original detection/analysis evidence it comments on
  (`docs/INTELLIGENCE_ORCHESTRATION.md` §9). There is still no
  verified-outcome/training-label workflow — a human annotation is
  captured as human interpretation, not promoted into ground truth for
  future model training.

---

## What this document is not

Not a claim that any model here has been validated against real industrial
failure outcomes — none has, because none exist for this project. Not a
performance benchmark suitable for comparing against a commercial
predictive-maintenance product — this is a synthetic-data engineering
demonstration. Not a claim that heuristic and statistical methods are
"AI" in the deep-learning sense — each method in this platform is named
for what it actually is (a statistical threshold, a trained isolation
forest, a rule-based heuristic), never blurred together under a single
marketing term.
