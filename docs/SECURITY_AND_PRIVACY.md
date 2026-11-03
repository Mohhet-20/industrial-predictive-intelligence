# Security & Privacy

An honest account of this platform's security posture: what's real and
enforced, what's explicitly out of scope, and what's not covered. Full
technical detail and test references live in
`docs/RELIABILITY_AND_OPERATIONS.md` §2/§10/§21 — this document is the
readable summary a reviewer or hiring manager can scan in a few minutes.

**No compliance claim is made anywhere in this project.** No SOC 2, ISO
27001, PCI, HIPAA, NIST, or similar certification was pursued, mapped, or
implied. This is a self-review by the team that wrote the code, not an
independent audit or penetration test.

---

## Authentication & authorization

- **Authentication**: JWT (HS256), signed and expiring, issued on login.
  Passwords are hashed with bcrypt via `passlib`, never stored or logged
  in plaintext. Every protected route requires a valid `Authorization:
  Bearer <token>` header, verified server-side (`app/core/deps.py`) — the
  frontend's choice of what to show or hide is never the enforcement
  boundary.
- **Authorization (RBAC)**: three roles — `admin`, `engineer`, `viewer` —
  enforced with a FastAPI dependency (`require_roles(...)`) on every
  mutating or sensitive route. Tested directly, including negative cases
  (a viewer denied write access, an unauthenticated request denied
  entirely).
- **Multi-tenancy isolation**: every domain row carries `organization_id`;
  every query is scoped to the authenticated user's organization
  server-side via a single centralized dependency
  (`organization_scope`), rather than repeated (and potentially
  forgotten) per-query filtering.
- **Login hardening**: an in-memory, per-process rate limiter on
  `/auth/login` (5 attempts / 60 seconds by default, keyed by IP+email)
  blocks brute-force attempts; proven live with real repeated login
  calls hitting a real 429. A timing side-channel that let an attacker
  distinguish "unknown email" from "wrong password" by response time was
  found and fixed — the same bcrypt cost is now paid on both paths.

## Secrets handling

- Secrets (JWT signing key, database URL) come from environment
  variables via a validated `Settings` object — never hardcoded, never
  committed. `.env.example` files (backend, frontend, and the repo root
  for Docker Compose) contain no real secrets; `.env` is gitignored
  everywhere.
- **Production-mode fail-fast validation**: when `APP_ENV=production`,
  the app refuses to start (a hard crash at import time, not a warning)
  if the JWT secret is still the checked-in development default, if
  `CORS_ORIGINS` contains a wildcard, or if the database is in-memory
  SQLite. This is the single most important security control added in
  this project's reliability work — it makes an entire class of
  "someone forgot to change the demo secret in prod" incident
  structurally impossible rather than relying on a checklist.
- No backend secret or internal config value is ever sent to the
  frontend — the frontend only ever receives a public API base URL at
  build time, confirmed by grepping every `VITE_*` reference in the
  codebase.

## Input validation

- Every request body is validated against a typed Pydantic schema before
  any handler code runs; a schema mismatch returns a structured 422 with
  field-level detail, never a raw stack trace.
- Ingestion payloads get additional domain validation beyond schema
  shape: sensor/asset identity checks, timestamp sanity, unit and
  declared-range checks, deduplication — documented in full in
  `docs/INGESTION_CONTRACT.md`.
- Request bodies larger than a configured limit (10MB by default) are
  rejected with 413 before parsing begins, added specifically to close
  an unbounded-request-size gap.

## Auditability

- Three real, append-only audit trails exist and are tested:
  `GovernanceEvent` (MLOps lifecycle actions — evaluate, approve,
  activate, rollback, retire), `InvestigationStatusEvent` (investigation
  status transitions, guarded by an explicit allowed-transition map), and
  `InvestigationAnnotation` (human notes, never mutating the evidence
  they comment on).
- Every structured log line carries a correlation ID that threads a
  single request through service-layer and background-job code, so any
  action can be traced end to end from the log stream alone.

## Artifact & trust boundaries

- No model artifact is ever loaded from a file path derived from user
  input — there is no artifact file at all in this system. Every
  "model" is parameters in a database column, scored by code that ships
  with the application. This means there is no deserialization or
  path-traversal surface for model loading to harden, by construction
  rather than by a mitigation that could be bypassed.

## Logging considerations

- Structured JSON logs, reviewed specifically for secret leakage: every
  `logger`/`log_with_fields` call site was grepped for `password`,
  `token`, `secret`, `hashed_password` — none log credential material.
  Authentication logs the *outcome* of a login attempt, never the
  password itself.
- Internal exceptions are caught by a global handler and turned into a
  stable `{"error": {"code", "message", "request_id"}}` shape; the real
  exception (including any sensitive detail it might contain) is logged
  server-side only, never returned to the client.

## Response hardening

- `SecurityHeadersMiddleware` adds `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` on every
  response, and `Cache-Control: no-store` specifically on auth
  responses (a bearer token must never be cacheable). No CSP is set —
  there is no server-rendered HTML or inline-script surface on this
  pure-JSON API for a CSP to meaningfully restrict, so adding one would
  be a header for its own sake.

## Dependency vulnerability scanning

`pip-audit` (backend) and `npm audit` (frontend) were both actually run,
not assumed clean. Findings that could be fixed with a validated,
low-risk upgrade were fixed (full test suite re-run green after each).
Findings left open each have an individually stated reason (upstream
version-pin conflict, dev-only tooling with no production exposure, a
major-version bump whose regression surface wasn't validated in this
pass) — see `docs/RELIABILITY_AND_OPERATIONS.md` §10 for the complete,
current list. This is reviewed as part of the CI script
(`scripts/ci_check.sh`), so it doesn't silently go stale.

---

## What this does NOT cover (stated plainly)

- **No independent security audit or penetration test.** This is a
  self-review by the team that wrote the code, with the blind spots that
  implies.
- **No distributed rate limiting.** The login limiter is per-process,
  in-memory, and resets on restart — adequate for a single-instance
  deployment, not for a multi-instance one behind a load balancer without
  a shared store (Redis) in front of it.
- **No secret rotation mechanism.** Changing `JWT_SECRET_KEY` invalidates
  every issued token immediately with no grace period — a real
  operational rough edge for a live deployment, documented rather than
  fixed.
- **No general request-level idempotency keys.** A client-side network
  retry of a `POST` could, in principle, create a duplicate resource for
  endpoints without their own natural dedup key. The two job-backed
  writes that needed this (dataset builds, detection runs) have it; the
  rest do not.
- **No content security policy, no WAF, no DDoS protection** — none of
  these exist and none is implied by anything above.
- **No compliance certification of any kind.**

Every item in this section is a real, bounded gap named on purpose — this
project's standing discipline is to document what's missing rather than
imply it's handled.
