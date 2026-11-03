# Setup — Development Environment

Everything below (sections 1–4) runs with no external infrastructure (no
Docker, no Postgres, no Redis) — see ARCHITECTURE.md §6 for why SQLite is
the right choice for this foundation. Section 5 covers the optional
Docker/Compose path added in Phase 7 for a deployment-like environment —
see docs/RELIABILITY_AND_OPERATIONS.md §1 for the three-environment
distinction (LOCAL DEV / TESTING-CI / DEPLOYMENT-PRODUCTION-LIKE) this
file's sections map onto.

## Prerequisites

- Python 3.11+
- Node.js 20+ (repo was built/tested against Node 22)

## 1. Backend

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env             # edit if you want, defaults work for local dev

# Apply the database schema (creates backend/data/pred_platform.db)
alembic upgrade head

# Load the SYNTHETIC demo dataset (idempotent — safe to re-run)
python -m app.synthetic.cli --seed 42 --days 21
```

The generator prints a summary including the demo login:
`admin@riverbend-demo.io` / `ChangeMe123!` — change or remove this account
before this ever runs anywhere but a laptop.

Start the API:

```bash
uvicorn app.main:app --reload --port 8000
```

Verify it's up:

```bash
curl http://localhost:8000/api/v1/health/ready
# {"status":"ok","environment":"development","schema_version":"6a3b828a4788","checks":{"database":"ok","schema_version":"ok"}}
```

Real operational metrics (request volume/latency, background jobs,
ingestion, data quality, anomaly detection — see
docs/RELIABILITY_AND_OPERATIONS.md §12) are at
`GET /api/v1/system/metrics`, admin/engineer only.

Interactive API docs: http://localhost:8000/docs

## 2. Frontend

```bash
cd frontend
npm install
cp .env.example .env             # points at http://localhost:8000/api/v1 by default
npm run dev
```

Open http://localhost:5173 and log in with the demo credentials above.

## 3. Run the tests

```bash
cd backend
source .venv/bin/activate
python -m pytest -q
```

267 tests as of this build, covering ingestion validation, deduplication,
timestamp handling, data-quality checks, API contracts, auth/RBAC, the
async background-job path (including timeout and duplicate-submission
protection — Phase 7), anomaly detection, condition/forecasting, MLOps
governance, intelligence orchestration/investigation, and reliability/
security hardening (rate limiting, config validation, security headers,
readiness under a real broken-DB injection). All passing is part of
"done" — see the completion reports delivered alongside this codebase for
the actual run output.

Real dependency-vulnerability scan (informational, doesn't fail the run):

```bash
pip install pip-audit && pip-audit
```

See docs/RELIABILITY_AND_OPERATIONS.md §10 for the current findings and
why any of them are deferred rather than fixed.

## 4. Frontend checks

```bash
cd frontend
npm run lint       # eslint . — real, configured, and clean as of this build
npm run typecheck  # tsc -b — full cross-project type-check
npm run test:run   # vitest — this platform's frontend test suite
npm run build      # tsc -b && vite build — fails on any type error
npm audit          # informational — see docs/RELIABILITY_AND_OPERATIONS.md §10
```

Or run everything (backend + frontend, exactly what CI runs) in one shot
from the repository root:

```bash
./scripts/ci_check.sh
```

For a real, live, end-to-end demonstration of the actual investigation
workflow (not just unit tests) against the seeded demo dataset, run the
Phase 8 acceptance test from `backend/` with the venv active:

```bash
python ../scripts/e2e_acceptance_test.py
```

It logs in as the demo admin, inspects real measurements and data-quality
context, reviews a real anomaly's explanation and provenance, opens a
real investigation, attaches evidence, adds an annotation, transitions
status, confirms nothing was silently mutated, and checks two honest
failure paths. It writes real rows into your dev database each time it
runs (a new investigation per run), same as the live-validation scripts
from earlier phases.

Similarly, for the optional AI copilot (Phase 9), run its own acceptance
test the same way:

```bash
python ../scripts/e2e_copilot_acceptance_test.py
```

It exercises the copilot end to end against the real seeded dataset,
including asking a deliberately insufficient-evidence question and
checking the answer stays honest rather than fabricating a status. No
`ANTHROPIC_API_KEY` is required to run it — with none set (the default),
it validates the "not configured" evidence-only path, which is the honest
default state of this repository as delivered. See
[docs/COPILOT.md](./docs/COPILOT.md) for how to configure a real provider.

## Resetting the demo database

```bash
cd backend
rm -f data/pred_platform.db data/pred_platform.db-wal data/pred_platform.db-shm
alembic upgrade head
python -m app.synthetic.cli --seed 42 --days 21
```

## 5. Docker / Compose (deployment-like environment, optional)

```bash
cp .env.example .env   # repo root — generate a real JWT_SECRET_KEY, see the file itself
docker compose up --build
```

Backend on http://localhost:8000, frontend (nginx) on http://localhost:5173.
Migrations are applied automatically by the backend container's entrypoint
before it starts. `docker compose ps` shows real `healthy`/`unhealthy`
status once each service's `HEALTHCHECK` has run.

**Honesty note**: this configuration was written and statically validated
(`docker compose config` resolves correctly; the required-secret guard was
verified to fail closed when `.env` is missing) but the actual container
build could not be executed in the sandboxed environment this project was
built in — its network policy blocks the container registry. See
docs/RELIABILITY_AND_OPERATIONS.md §1 for the full explanation. Try this
section first on whatever machine you're reading this from; it is expected
to work, just not yet proven to.

## Common issues

- **`ModuleNotFoundError` running alembic/uvicorn**: activate the venv
  first (`source .venv/bin/activate`), and run commands from `backend/`.
- **Frontend can't reach the API / CORS errors**: confirm the backend is
  running on the port your frontend `.env`'s `VITE_API_BASE_URL` points at,
  and that `CORS_ORIGINS` in the backend `.env` includes your frontend's
  origin (defaults already cover `http://localhost:5173`).
- **"JWT_SECRET_KEY must be set..."**: the backend refuses to start with a
  short/empty secret — see `.env.example`.
- **"Refusing to start with APP_ENV=production and unsafe configuration"**:
  the Phase 7 production-safety check — see
  docs/RELIABILITY_AND_OPERATIONS.md §2. Only fires when `APP_ENV=production`;
  local dev is unaffected.
- **Login returns 429 after a handful of attempts while testing**: the
  Phase 7 anti-brute-force rate limit on `/auth/login` (5 attempts/60s by
  default, per IP+email) — wait a minute, or adjust
  `LOGIN_RATE_LIMIT_ATTEMPTS`/`LOGIN_RATE_LIMIT_WINDOW_SECONDS`. See
  docs/RELIABILITY_AND_OPERATIONS.md §10.
