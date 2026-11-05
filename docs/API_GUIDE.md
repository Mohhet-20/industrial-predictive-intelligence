# API Guide

This is a conceptual guide to the real API, grounded in the actual
implementation. For the complete, always-current, field-level schema for
every endpoint, run the backend and open **http://localhost:8000/docs**
(FastAPI's auto-generated interactive OpenAPI docs) or
**http://localhost:8000/redoc** — those are generated directly from the
code below, so they can't drift out of sync with what's actually running.
This document explains the concepts that aren't obvious from a schema
listing alone: how auth works, what pagination/error shapes look like,
and how the endpoints compose into the platform's real workflows.

Base URL for local dev: `http://localhost:8000/api/v1`.

---

## 1. Authentication

All endpoints except `/auth/register-organization` and `/auth/login`
require a bearer token.

```bash
# Register a brand-new organization + its first admin user
curl -X POST http://localhost:8000/api/v1/auth/register-organization \
  -H "Content-Type: application/json" \
  -d '{"organization_name":"Acme Manufacturing","admin_email":"admin@acme.io","admin_password":"ChangeMe123!","admin_full_name":"Jordan Lee"}'

# Log in
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@acme.io","password":"ChangeMe123!"}'
# -> {"access_token":"eyJ...","token_type":"bearer","expires_in_minutes":60}

# Use the token
curl http://localhost:8000/api/v1/auth/me \
  -H "Authorization: Bearer eyJ..."
```

Subsequent users within an organization are created by an admin (there is
no open self-registration beyond the first organization-bootstrap call).
Tokens are JWTs (HS256), expire after `JWT_ACCESS_TOKEN_EXPIRE_MINUTES`
(default 60), and carry no refresh mechanism — a real, documented, bounded
gap, not an oversight (see `docs/SECURITY_AND_PRIVACY.md`).

Every route beyond auth also enforces **role-based authorization**
server-side (`admin` / `engineer` / `viewer`) — a 403 with
`{"error":{"code":"not_authorized",...}}` means the authenticated user's
role doesn't permit that action, not a bug.

## 2. Error shape

Every error response, from every endpoint, has the same envelope:

```json
{
  "error": {
    "code": "not_found",
    "message": "Asset 'a1b2c3' was not found.",
    "request_id": "a7e2f9c1-...",
    "details": null
  }
}
```

`request_id` is the same correlation ID that appears in the server's
structured logs for that request — the fastest way to go from "a user
reported an error" to the exact log line. Common `code` values:
`validation_failed` (422), `not_found` (404), `conflict` (409),
`authentication_failed` (401), `not_authorized` (403), `rate_limited`
(429), `not_yet_available` (501 — a schema concept that exists but has no
working implementation, distinct from a broken request), `internal_error`
(500 — a real bug; stack traces never reach the client, only the generic
message and a request ID to look it up server-side).

## 3. Pagination

Every list endpoint returns the same `Page<T>` shape:

```json
{
  "items": [ /* ... */ ],
  "total": 143,
  "limit": 50,
  "offset": 0
}
```

`limit`/`offset` are query parameters on every list endpoint; every
service layer imposes its own hard upper cap on `limit` (e.g. measurement
queries cap page size regardless of what's requested) — there is no
"give me everything" query anywhere in this API.

## 4. Endpoint map (by capability area)

All prefixed with `/api/v1`.

| Area | Prefix | What it's for |
|---|---|---|
| Auth | `/auth` | register-organization, login, `/auth/me` |
| System | `/health`, `/health/ready`, `/system/metrics` | liveness, readiness (+ schema version), real operational metrics (admin/engineer only) |
| Assets | `/assets` | equipment + sensors (create, list, get, list a given asset's sensors) |
| Measurements | `/measurements`, `/stats/sensors/{id}` | raw time-range queries, per-sensor summary stats |
| Ingestion | `/ingestion` | sync (`/measurements`) and async batch (`/batches`) ingestion, batch status |
| Data quality | `/data-quality` | issues list, per-asset summary |
| Jobs | `/jobs` | background job status/progress/result |
| Time series | `/timeseries` | profiling, analysis, resampling, change candidates, correlation, feature definitions/preview, dataset builds, chronological/rolling/expanding splits |
| Anomaly | `/anomaly` | detectors, evaluation, detection runs, anomalies, review, context, per-anomaly investigations, monitoring |
| Condition | `/condition` | evidence, condition runs, degradation signals + review/validate, forecast models + runs, attention list, monitoring |
| MLOps | `/mlops` | components, versions, evaluate/validate/approve/activate/rollback/retire, scoring, retraining requests, shift analysis, governance events, monitoring |
| Investigations | `/investigations` | create, status transitions + history, evidence links, annotations, related investigations |
| Orchestration | `/orchestration` | unified asset timeline, prioritized signals, asset summary, cross-sensor context, evidence provenance lookup |

The full route-by-route request/response schema for every one of these is
what `/docs` renders live — this table is the map, `/docs` is the
territory.

## 5. A real workflow, end to end

The endpoints above compose into the platform's actual investigation
workflow like this:

```
POST /auth/login
  -> GET  /assets                                   (pick an asset)
  -> GET  /measurements?sensor_ids=...&start_time=...&end_time=...   (bounded range query, look at raw data)
  -> GET  /data-quality/summary/{asset_id}           (what to trust)
  -> GET  /anomaly/anomalies?asset_id=...            (what fired)
  -> GET  /anomaly/anomalies/{id}/context            (why it fired)
  -> GET  /orchestration/evidence/anomaly/{id}/provenance   (which model/version produced it)
  -> POST /investigations                            (open an investigation)
  -> POST /investigations/{id}/evidence               (attach the anomaly as evidence)
  -> POST /investigations/{id}/annotations            (human notes — never mutates the evidence)
  -> POST /investigations/{id}/status                 (move through the review lifecycle)
  -> GET  /investigations/{id}/status-history          (append-only audit trail)
```

Every write in this chain is role-checked and organization-scoped
server-side; every read composes real database rows — nothing in this
chain is a canned/fixture response.

## 6. Validation behavior

Request bodies are validated against a Pydantic schema before any handler
code runs. A field that's the wrong type, missing, or out of a declared
constraint (e.g. `admin_password` under 8 characters) returns a 422 with
per-field detail in `error.details`, generated directly from Pydantic's
own validation errors — never a hand-rolled, partial validation message.

## 7. What's intentionally not here

There is no GraphQL endpoint, no webhook/callback system, and no
API-key-based machine-to-machine auth path — only interactive JWT auth.
None of these were removed; none were ever built, because nothing in this
project's actual usage pattern (a human operator investigating equipment
behavior) needed them. Adding them would be exactly the kind of
unjustified surface area this project's design philosophy argues against.
