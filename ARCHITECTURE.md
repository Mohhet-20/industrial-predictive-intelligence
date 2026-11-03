# Architecture — Industrial AI Predictive Intelligence Platform

Status: **Phase 1 — Foundation**, extended (not rewritten here) through
Phase 9. This document still accurately describes what it describes — the
domain model, module boundaries, and technology choices from Phase 1 — but
is a snapshot, not a living diagram; each later phase's own doc covers
what it added rather than retrofitting this one (see README.md for the
full phase-by-phase map). In particular: §7 (Security) and §8
(Observability) below describe the Phase 1 foundation only —
docs/RELIABILITY_AND_OPERATIONS.md §1–§13 is the current, complete account
of this system's deployment architecture, security hardening, and
observability as of Phase 7, and docs/SECURITY_AND_PRIVACY.md is the
readable summary. Sections marked PLANNED are not built. For real Mermaid
diagrams of the actual system architecture, data flow, intelligence flow,
provenance flow, and domain model, see
[docs/ARCHITECTURE_DIAGRAMS.md](./docs/ARCHITECTURE_DIAGRAMS.md); for the
consolidated AI/ML methodology across every intelligence layer, see
[docs/AI_ML_METHODOLOGY.md](./docs/AI_ML_METHODOLOGY.md); for the optional,
evidence-grounded AI copilot layered on top of everything below (never a
replacement for it), see [docs/COPILOT.md](./docs/COPILOT.md).

## 0. Repository audit (what this was built against)

The workspace (`F:\pred`, mounted here as the project root) was empty before
this phase. There was no existing stack, dataset, or convention to preserve.
The stack below was chosen fresh, using the constraints in the project brief
(strong typing, testability, reproducibility, no unjustified infrastructure).

## 1. Purpose

A platform for monitoring industrial equipment via sensor time-series data:
ingest → validate → store → explore → assess data quality, as a trustworthy
foundation for anomaly detection and predictive maintenance work in later
phases. It is decision support. It does not control equipment and does not
issue autonomous maintenance actions — every AI-derived output in this and
future phases is reviewable evidence for a human, not a command.

## 2. Domain model

```
Organization
  └─ User (role: admin | engineer | viewer)
  └─ Asset (piece of equipment)
       └─ Sensor (belongs to one asset, has a unit + declared valid range)
            └─ SensorMeasurement   (raw observation: sensor_id, ts, value, quality flags)
            └─ DataQualityIssue    (derived: references measurements/gaps, never mutates them)
       └─ MaintenanceEvent (PLANNED — table exists, no workflow yet)
  └─ DataSource (identifies where a batch came from: file, simulator, future stream)
  └─ IngestionBatch (one ingestion attempt: status, counts, errors)
  └─ BackgroundJob (async unit of work: status/progress/result/error)
  └─ ModelVersion / ModelRun / Prediction / Anomaly / AnomalyInvestigation
       (PLANNED — tables exist so the schema doesn't need breaking migrations
        later, but no training, scoring, or detection logic runs in Phase 1.
        Every API that would surface them returns an explicit "not yet
        available" response rather than fabricated numbers.)
```

Hard boundary enforced end to end: **raw measurements are immutable.**
Nothing — not quality checks, not future feature engineering, not future
models — ever overwrites a `SensorMeasurement` row. Derived data
(`DataQualityIssue` now; features/predictions later) always lives in its own
table with a foreign key back to what it was derived from.

## 3. Module boundaries

Single deployable backend (modular monolith), not microservices — the
project brief is explicit that distributed infrastructure for its own sake is a
failure, and nothing here needs independent scaling yet.

```
backend/app/
  core/       config, logging, security primitives, error handling — no domain knowledge
  db/         engine/session, base model, migrations
  models/     SQLAlchemy ORM (the domain model above)
  schemas/    Pydantic request/response contracts (never leak ORM models over the API)
  services/   ingestion, data quality, stats, jobs — business logic, framework-agnostic
  api/v1/     FastAPI routers — thin, delegate to services, own the HTTP contract
  jobs/       background job runner
  synthetic/  deterministic synthetic data generator (clearly out of the request path)
```

`frontend/` is a separate deployable (Vite/React/TS SPA) talking to the
backend only through the versioned REST API — no shared code, no reaching
into the backend's database.

## 4. Technology stack and why

| Concern | Choice | Why |
|---|---|---|
| Backend framework | FastAPI (Python 3.11) | async, Pydantic-native validation, generates an OpenAPI contract for free |
| ORM / migrations | SQLAlchemy 2.0 + Alembic | typed models, explicit migrations instead of implicit schema drift |
| Database | SQLite (file-based) for this foundation | see §7 below — a deliberate, documented choice, not an oversight |
| Auth | JWT (HS256) + bcrypt password hashing | stateless, standard, no framework lock-in |
| Background jobs | In-process asyncio job runner + a `BackgroundJob` table | the actual async work here (batch ingestion) is seconds-scale; a broker (Celery/Redis) is unjustified infrastructure until job volume or duration says otherwise (documented as a scaling path, not built) |
| Frontend | React 18 + TypeScript + Vite | typed, fast dev loop, no framework lock-in beyond React itself |
| Data fetching | TanStack Query | request caching/retry without hand-rolled state machines |
| Charts | Recharts | sufficient for time-series line charts + gap/anomaly markers; not a rendering bottleneck at this data scale |
| Styling | Tailwind CSS | fast to keep visually consistent without a component library dependency |

Nothing here was added speculatively; each row is used by something built
in this phase.

## 5. Data flow (the one workflow this phase proves end-to-end)

```
synthetic generator (seeded)
  → JSON/CSV canonical ingestion payload
    → POST /api/v1/ingestion/measurements  (sync path for small batches,
        or /ingestion/batches + BackgroundJob for larger ones)
      → validation (schema, asset/sensor identity, timestamp, unit, range)
        → dedup + out-of-order handling
          → SensorMeasurement rows (raw, immutable) + IngestionBatch record
            → data-quality service runs checks → DataQualityIssue rows
              → GET /api/v1/measurements, /api/v1/data-quality/*, /api/v1/stats/*
                → frontend Data Explorer + Data Quality pages render it
```

## 6. Time-series storage decision

Requirements at this stage: a handful of demo assets, tens of sensors,
weeks of minute-resolution data — on the order of 10^5–10^6 rows. That is
comfortably inside what a properly indexed relational table handles; a
dedicated TSDB (TimescaleDB, InfluxDB) would add operational surface area
(another service to run, back up, and reason about) with no present payoff.

Decision: `sensor_measurements` in SQLite, indexed on
`(sensor_id, timestamp)` and `(asset_id, timestamp)`, with all time-range
queries required to pass an explicit range (no unbounded "give me
everything" queries — see ingestion/query API). SQLite specifically (over
Postgres) so a developer needs zero infrastructure to run this foundation —
`pip install`, run migrations, go. The repository/service layer sits behind
an interface (`MeasurementRepository`) precisely so this can be swapped for
Postgres/Timescale later without touching services or the API — that swap
is the first recommended next step once real data volume shows up (see
completion report, "known limitations").

## 7. Security foundation

- Real JWT auth (bcrypt-hashed passwords, signed/expiring tokens), not a
  bypassable stub.
- Role-based authorization (`admin`, `engineer`, `viewer`) enforced with a
  FastAPI dependency on every mutating/sensitive route — never trusted from
  the frontend alone.
- Every domain row carries `organization_id`; every query is scoped to the
  authenticated user's organization server-side. This is the multi-tenancy
  isolation boundary.
- Secrets (JWT signing key, DB URL) come from environment variables via a
  validated Pydantic `Settings` object; `.env.example` ships with no real
  secrets; `.env` is gitignored.
- Internal exceptions are caught by a global handler and turned into a
  stable `{error: {code, message, request_id}}` shape — stack traces never
  reach the client.

## 8. Observability foundation

- `/health` (liveness) and `/health/ready` (checks DB connectivity).
- Structured JSON logging with a per-request correlation ID (middleware),
  propagated into service/job logs.
- `BackgroundJob` rows give real status/progress/error for async work — the
  System Health page reads this, it does not fabricate metrics.

## 9. What is explicitly NOT in this phase

Anomaly detection, feature engineering pipelines, model training/scoring,
explainability, and predictive maintenance risk are **PLANNED**. Their
tables exist (so later phases are additive migrations, not rewrites) and
their frontend page exists as a clearly-labeled "not yet available"
placeholder, but no detection or prediction logic runs anywhere in this
codebase yet. Nothing in the API or UI claims a prediction that doesn't
exist.
