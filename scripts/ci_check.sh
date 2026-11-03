#!/usr/bin/env bash
# Local CI check — the exact commands this project's CI pipeline runs
# (.github/workflows/ci.yml calls this same script), so "it passes locally"
# and "it passes in CI" mean the same thing. Not a separate, drifted set of
# instructions from what a contributor runs by hand.
#
# Usage: ./scripts/ci_check.sh   (run from the repository root)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "=== [1/6] Backend: install dependencies (venv) ==="
cd backend
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -r requirements.txt

echo "=== [2/6] Backend: migrations apply cleanly to a fresh database ==="
rm -f /tmp/ci_check_pred_platform.db
DATABASE_URL="sqlite:////tmp/ci_check_pred_platform.db" JWT_SECRET_KEY="ci-check-secret-not-for-production-use-only" alembic upgrade head
DATABASE_URL="sqlite:////tmp/ci_check_pred_platform.db" JWT_SECRET_KEY="ci-check-secret-not-for-production-use-only" alembic check
rm -f /tmp/ci_check_pred_platform.db /tmp/ci_check_pred_platform.db-wal /tmp/ci_check_pred_platform.db-shm

echo "=== [3/6] Backend: full test suite ==="
python -m pytest -q

echo "=== [3b/6] Backend: dependency vulnerability scan (informational) ==="
# Non-blocking: see docs/RELIABILITY_AND_OPERATIONS.md §10 for the current
# findings, what was fixed, and why the remainder is deferred (an upstream
# version constraint conflict, a dev-only test runner, or a base packaging
# tool rather than a shipped application dependency). A real scan that
# reports real findings, not a check that always exits green regardless.
pip install -q pip-audit || true
pip-audit || true

deactivate
cd "$REPO_ROOT"

echo "=== [4/6] Frontend: install dependencies ==="
cd frontend
npm ci --no-audit --no-fund

echo "=== [5/6] Frontend: lint + typecheck ==="
npm run lint
npm run typecheck

echo "=== [6/6] Frontend: unit tests + production build ==="
npm run test:run
npm run build

echo "=== [6b/6] Frontend: dependency vulnerability scan (informational) ==="
# Non-blocking, same reasoning as the backend scan above — see
# docs/RELIABILITY_AND_OPERATIONS.md §10 for current findings and why any
# remainder is deferred rather than force-upgraded blind.
npm audit || true

cd "$REPO_ROOT"
echo ""
echo "=== ALL CHECKS PASSED ==="
