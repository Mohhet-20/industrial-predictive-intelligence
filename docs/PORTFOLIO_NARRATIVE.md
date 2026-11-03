# Portfolio Narrative

A concise, honest way to talk about this project in an interview or
technical discussion — not marketing copy. Every claim below is backed by
something in the repository; none of it is aspirational.

---

## 30-second elevator pitch

"I built an industrial equipment monitoring platform that ingests sensor
time-series data, detects statistically unusual behavior with two
complementary methods, tracks long-term condition evidence, and — the
part I'm actually proud of — governs every one of those intelligence
outputs through a real MLOps lifecycle so any number on screen traces back
to the exact model version and data window that produced it. The whole
thing is built around one discipline: never claim more than the data
actually supports. There's no failure-prediction model, because there are
no failure labels in the data to train one on — and the system says that
plainly instead of faking it."

## 2-minute project explanation

"The core problem in industrial monitoring software is that it's easy to
build something that *looks* intelligent — dashboards, anomaly scores,
health percentages — without any of it being backed by real evidence. I
built this platform specifically to resist that. It ingests sensor
time-series data for industrial equipment, validates and stores it
immutably, and runs it through several layers of intelligence: time-series
profiling and feature engineering, anomaly detection using two
complementary methods (a robust statistical detector and an Isolation
Forest), and condition/degradation evidence with a backtest-gated
forecaster that only produces a forecast when it's actually shown to beat
a naive baseline on held-out data.

The part that took the most engineering discipline was the layer on top:
an MLOps governance system that versions every detector and forecaster,
gates activation behind task-appropriate evaluation (never a single
universal accuracy threshold), and makes every score traceable back to the
exact component version and reference data that produced it — plus an
investigation workspace where a human can pull all of that evidence
together, attach annotations, and track review status, all through an
append-only audit trail.

I also spent real, otherwise-invisible effort on reliability: production
config validation that refuses to start with an unsafe secret, a real
metrics endpoint, background-job timeouts and duplicate protection, login
rate limiting, and a fixed timing side-channel — the kind of work that
doesn't show up in a demo but is exactly what separates a project from a
production-minded engineer's work.

What it doesn't do, on purpose: it doesn't predict failures. There's no
labeled failure data anywhere in this project, so building a failure
classifier would mean fabricating confidence the data can't support. The
system is explicit about that boundary everywhere it matters."

## 5-minute technical walkthrough

Use this as a talk track while actually clicking through the running app.

1. **Start with the data model.** "Every measurement is immutable — once
   ingested, nothing downstream ever overwrites it. Derived data
   (quality issues, features, anomalies) always lives in its own table
   with a foreign key back to what it came from. That's the foundation
   everything else is built on."
2. **Show ingestion + data quality.** "Here's a batch of synthetic sensor
   data going through validation — schema, unit, timestamp, range checks
   — and the data-quality issues it surfaces. This isn't cosmetic; every
   downstream intelligence output shows its data-quality context so a
   reviewer can weigh how much to trust it."
3. **Show an anomaly.** "This anomaly came from the robust z-score
   detector, scored against a fixed reference period — the detector never
   sees the period it's judging. Here's the explanation: which sensor
   deviated, by how much, relative to what baseline. That's a real
   calculation, not a generated sentence."
4. **Show provenance.** "Click through to provenance — this traces the
   anomaly back to the exact detector version, when it was evaluated and
   approved, and what reference data it was fit against. That's the MLOps
   governance layer doing its job."
5. **Show condition intelligence and the forecaster's honesty gate.**
   "Here's the degradation evidence — trend, variance change — kept
   separate, no fake composite health score. And here's the forecaster:
   on this data, its own backtest usually shows it doesn't beat a naive
   baseline, and it says so rather than forecasting anyway. That's the
   single clearest example of the project's core discipline."
6. **Show an investigation.** "A human opens an investigation, attaches
   this anomaly and this degradation signal as evidence — references, not
   copies — adds an annotation, and moves it through a review-status
   lifecycle. Every step is append-only and auditable."
7. **Show System Health.** "And here's the operational side: real
   request-latency percentiles, background-job outcomes, all measured
   from actual traffic in this process, not fabricated for the demo."
8. **Close on the boundary.** "What you won't find anywhere in this
   platform is a predicted failure date or a percentage chance of
   breakdown — because nothing here was trained or validated to produce
   one, and I'd rather the project be honest about that than look more
   impressive than it is."

## Recommended live demo flow

1. Log in with the seeded demo account.
2. Overview page → pick an asset with visible data-quality issues.
3. Data Explorer → show raw measurements + a gap or out-of-range flag.
4. Anomaly Intelligence → open a flagged anomaly → explanation → context.
5. Intelligence Workspace → provenance lookup for that anomaly.
6. Condition Intelligence → a degradation signal, and the forecaster's
   honest "backtest didn't justify a forecast" state if it's currently in
   that state (check first — don't narrate a state that isn't showing).
7. Intelligence Workspace → open an investigation, attach the anomaly,
   add an annotation, change status, show the status history.
8. System Health → real metrics, real readiness check.
9. Trigger one honest failure on purpose — e.g. try an insufficient-data
   forecast request, or hit a rate-limited login — to show the system
   fails safely and explicitly rather than crashing or faking success.

## Why naive approaches fail (the engineering challenge)

- **A single "health score"** combining multiple evidence types looks
  impressive and means nothing — averaging a z-score, a variance-change
  metric, and a distributional-shift statistic onto one 0–100 scale
  requires an arbitrary weighting with no principled basis. This project
  keeps evidence types separate specifically to avoid manufacturing false
  precision.
- **Forecasting without a backtest gate** is the classic trap: it's easy
  to draw a confident-looking trend line into the future, and much harder
  to admit that on this specific data, a naive last-value baseline beats
  your model. This project always checks first.
- **Anomaly detection without reference/scoring separation** leaks
  information — a detector that's allowed to "see" the exact window it's
  judging will always look more accurate than it really is. This is the
  time-series analogue of test-set leakage, and it's the single easiest
  mistake to make invisibly.
- **A trained "failure predictor" without failure labels** is the biggest
  trap of all in this domain, because it's exactly what a customer or
  interviewer expects a predictive-maintenance product to have. Building
  one anyway — on synthetic data with no real failure ground truth — would
  produce a number that looks like insight and is actually noise. Refusing
  to build it was a harder design decision than building it would have
  been.

## What I personally learned / key build decisions

- Deciding **what not to build** was consistently the harder call than
  deciding what to build — the RUL estimator, the composite health score,
  and the always-on forecaster were all cut or gated after checking
  whether the data actually supported them.
- Structural leakage prevention (reference/scoring separation,
  chronological splitting utilities) is worth building as enforced code
  paths, not developer discipline — it's the kind of mistake that doesn't
  announce itself until someone asks hard questions about validity.
  Deep-diving into it also revealed how the entire project treats "true
  to your data" as a first-class constraint rather than an afterthought.
- Reliability work (config validation, rate limiting, a timing
  side-channel fix, job timeouts) produced zero visible new features and
  was still some of the most valuable engineering in the whole project —
  a good discipline check for judging engineering work by its real risk
  reduction, not its demo-ability.
- Writing an honest self-review after each layer — trying to describe
  what the system actually does and noticing where the description didn't
  match reality — repeatedly surfaced real bugs (a version-numbering
  collision risk, an evidence-relation direction bug, a stray build-output
  leak, the login timing side-channel) that a "looks done, ship it" pass
  would have missed.
