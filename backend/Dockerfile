# Multi-stage build for the backend API. Single deployable (modular
# monolith, per ARCHITECTURE.md §3) — no separate worker image, because the
# background job runner (app/jobs/runner.py) runs in-process inside this
# same server, not as a separate process. See docs/RELIABILITY_AND_OPERATIONS.md
# §1 for why this is the right shape for what this app actually is, and §17
# for how to run it.

FROM python:3.11-slim AS base

# Runtime OS deps: curl for the container HEALTHCHECK below (curl, not
# python, so a hung/GIL-blocked app process can't mask a real liveness
# failure by keeping the healthcheck's own interpreter responsive).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 appuser
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

# Liveness-only healthcheck: matches this app's own /api/v1/health (process
# is up, no dependency checks) rather than /health/ready — a container
# orchestrator's HEALTHCHECK/liveness probe restarting the container on a
# transient DB blip (which readiness WOULD report) would be the wrong
# reaction; only a genuinely wedged process should be restarted. See
# docs/RELIABILITY_AND_OPERATIONS.md §3 for the liveness/readiness split.
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/api/v1/health || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
