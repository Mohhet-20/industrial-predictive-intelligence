#!/usr/bin/env bash
# Runs on every container start, before the app process: apply pending
# Alembic migrations, then exec the real CMD (uvicorn) as PID 1 so it
# receives SIGTERM directly for graceful shutdown (no shell left in
# between — see docs/RELIABILITY_AND_OPERATIONS.md §17 "safe shutdown").
#
# Deliberately does NOT seed synthetic demo data — a fresh deploy applying
# schema migrations is a normal, expected step; auto-loading demo data into
# whatever this container's DATABASE_URL points at would be a real
# production hazard if that were ever pointed at a real database by
# mistake. Seeding is a documented manual step (see this repo's SETUP.md
# and docs/RELIABILITY_AND_OPERATIONS.md §17).
set -euo pipefail

echo "[entrypoint] applying database migrations..."
alembic upgrade head
echo "[entrypoint] migrations applied. starting: $*"

exec "$@"
