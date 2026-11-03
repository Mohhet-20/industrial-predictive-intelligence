# Architecture Diagrams

Real diagrams of the actual system — not an imagined enterprise
deployment. Every box below corresponds to a real module or table in this
repository; nothing here is aspirational. See `ARCHITECTURE.md` for the
prose walkthrough and `docs/RELIABILITY_AND_OPERATIONS.md` §1 for the
deployment-environment detail.

## 1. System architecture (actual deployment shape)

```mermaid
flowchart TB
    subgraph Client
        Browser["Browser (SPA)"]
    end

    subgraph Frontend["frontend/ — React + TypeScript + Vite"]
        SPA["Static bundle<br/>(served by Vite dev server locally,<br/>nginx in the container path)"]
    end

    subgraph Backend["backend/ — single FastAPI process (uvicorn)"]
        API["HTTP API<br/>(app/api/v1/*)"]
        Jobs["In-process background<br/>job runner (asyncio tasks)<br/>app/jobs/runner.py"]
        Metrics["In-process request-metrics<br/>collector — app/core/metrics.py"]
        API <--> Jobs
        API --> Metrics
    end

    subgraph Storage
        DB[("SQLite file, WAL mode<br/>backend/data/*.db")]
    end

    Browser -->|"fetch(), JSON, JWT bearer"| SPA
    SPA -->|"REST /api/v1/*"| API
    API -->|"SQLAlchemy"| DB
    Jobs -->|"SQLAlchemy"| DB

    style DB fill:#eef,stroke:#446
```

**No message broker, no cache, no third-party API call, no separate
worker process anywhere in this system.** The background job runner lives
inside the same uvicorn process as the API — there is one deployable
backend and one deployable frontend, full stop. This is a deliberate
modular-monolith choice (`ARCHITECTURE.md` §3/§4), not a placeholder for a
microservices design that was never built.

## 2. Data flow — ingestion to storage

```mermaid
flowchart LR
    Gen["Synthetic generator<br/>(seeded, deterministic)"] -->|"JSON/CSV"| Ingest["Ingestion validation<br/>schema + asset/sensor identity<br/>+ timestamp + unit + range"]
    Ingest -->|"valid rows"| Dedup["Dedup + out-of-order<br/>handling"]
    Dedup -->|"immutable"| Meas[("SensorMeasurement<br/>never overwritten")]
    Ingest -->|"rejected rows"| Batch[("IngestionBatch<br/>status + counts + errors")]
    Meas --> DQ["Data-quality checks<br/>gap / range / unit / timestamp"]
    DQ --> DQIssue[("DataQualityIssue<br/>references measurements,<br/>never mutates them")]
```

The hard boundary enforced end to end: **raw measurements are immutable.**
Nothing downstream — quality checks, features, models — ever overwrites a
`SensorMeasurement` row. Every derived table carries a foreign key back to
what it was derived from.

## 3. Intelligence flow — from raw data to an investigable signal

```mermaid
flowchart TB
    Meas[("SensorMeasurement<br/>(raw, immutable)")]

    Meas --> TS["Time-series intelligence<br/>profiling, features, correlation,<br/>change candidates"]
    Meas --> Anomaly["Anomaly detection<br/>robust z-score + Isolation Forest<br/>reference vs. scoring separation"]
    Meas --> Condition["Condition intelligence<br/>trend / variance / distribution evidence<br/>+ backtest-gated forecasting"]

    Anomaly --> AnomRow[("Anomaly<br/>severity tier, explainability,<br/>DQ context")]
    Condition --> DegSignal[("DegradationSignal<br/>3-stage, human-gated at top")]

    AnomRow --> MLOps["MLOps governance<br/>version, evaluate, approve,<br/>activate — every score traced<br/>to a component version"]
    Condition --> MLOps

    AnomRow --> Orchestration["Orchestration layer<br/>timeline, evidence graph,<br/>prioritization, provenance"]
    DegSignal --> Orchestration
    TS --> Orchestration

    Orchestration --> Investigation[("Investigation<br/>evidence links, status history,<br/>human annotations")]

    Investigation -->|"append-only"| Human["Human review / decision<br/>(never automatic)"]

    style Meas fill:#eef,stroke:#446
    style Human fill:#fef3c7,stroke:#92400e
```

This is the real shape of "decision support, not autonomous control": every
path terminates in evidence presented to a human, and nothing in this
diagram writes a maintenance action back to equipment — there is no such
integration in this system.

## 4. Provenance flow — how an output traces back to its source

```mermaid
sequenceDiagram
    participant U as User (browser)
    participant API as Orchestration API
    participant Gov as MLOps governance
    participant Model as ComponentVersion
    participant Data as SensorMeasurement

    U->>API: GET /orchestration/evidence/anomaly/{id}/provenance
    API->>Gov: which component/version produced this anomaly?
    Gov->>Model: look up the exact ComponentVersion (immutable, versioned)
    Model->>Data: which reference period / measurements was it fit against?
    Data-->>Model: real row references
    Model-->>Gov: version id, evaluation status, activation history
    Gov-->>API: full provenance record
    API-->>U: component name, version, method, reference period,<br/>evaluation/approval history — never a bare number with no origin
```

Every intelligence output the frontend renders can be traced through this
same chain — this is what "provenance" means concretely in this platform,
not a marketing word: a real foreign-key path from a displayed number back
to the exact code version and data window that produced it.

## 5. Domain model (core relationships)

```mermaid
erDiagram
    Organization ||--o{ User : has
    Organization ||--o{ Asset : owns
    Asset ||--o{ Sensor : has
    Sensor ||--o{ SensorMeasurement : records
    Sensor ||--o{ DataQualityIssue : "flagged on"
    Organization ||--o{ IngestionBatch : submits
    Organization ||--o{ BackgroundJob : runs
    Sensor ||--o{ Anomaly : detected_on
    Asset ||--o{ DegradationSignal : detected_on
    Organization ||--o{ ComponentVersion : governs
    ComponentVersion ||--o{ Anomaly : produced
    ComponentVersion ||--o{ DegradationSignal : produced
    Organization ||--o{ Investigation : opens
    Investigation ||--o{ EvidenceLink : references
    Investigation ||--o{ InvestigationAnnotation : has
    Investigation ||--o{ InvestigationStatusEvent : has
```

`EvidenceLink` is a reference, never a copy — it points at an `Anomaly`,
`DegradationSignal`, or other evidence row by id; an investigation never
duplicates or mutates the evidence it cites (`docs/INTELLIGENCE_ORCHESTRATION.md`
§2).

## 6. Control flow — what the system does and does not automate

```mermaid
flowchart LR
    Detect["Detection / analysis<br/>runs automatically<br/>(sync or background job)"] --> Flag["Result stored,<br/>surfaced to UI"]
    Flag --> Review{"Human reviews"}
    Review -->|"dismiss"| Closed["Marked reviewed —<br/>no further action"]
    Review -->|"investigate"| Invest["Investigation opened,<br/>evidence attached"]
    Review -->|"validate degradation"| Validated["Stage-3 designation —<br/>requires explicit justification,<br/>never automatic"]
    Invest --> Human2["Human decides next step<br/>(this platform stops here —<br/>no equipment control, no<br/>auto-generated maintenance order)"]

    style Human2 fill:#fef3c7,stroke:#92400e
    style Validated fill:#fef3c7,stroke:#92400e
```
