# Public demo API (synthetic data). Build context is the repository root:
#   docker build -f deploy/demo/Dockerfile -t pred-demo .
# Used by render.yaml. For a real deployment use backend/Dockerfile and docker-compose.yml instead.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN useradd --create-home --uid 10001 appuser
WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt
COPY backend backend
COPY scripts scripts

# Build the showcase database once, here, where CPU is plentiful: synthetic dataset, a trained detector and the
# anomaly it flags, an investigation and a scenario run. scripts/demo_start.py copies it fresh on every start.
WORKDIR /app/backend
RUN mkdir -p /app/demo \
 && export DATABASE_URL=sqlite:////app/demo/showcase.db \
 && alembic upgrade head \
 && python -m app.synthetic.cli --seed 42 --days 21 \
 && python ../scripts/e2e_acceptance_test.py > /dev/null \
 && python ../scripts/e2e_scenario_acceptance_test.py > /dev/null \
 && chown -R appuser:appuser /app

USER appuser
ENV APP_ENV=production PORT=8000
EXPOSE 8000
CMD ["python", "/app/scripts/demo_start.py"]
