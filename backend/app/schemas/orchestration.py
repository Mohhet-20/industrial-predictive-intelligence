"""Pydantic schemas for the Intelligence Orchestration API (Phase 6).

Every one of these endpoints returns a genuinely variable-shaped
aggregation (a timeline mixes several event kinds, a prioritization list's
contributing_factors differ by item_type, provenance differs by evidence
type) — same loose-envelope rationale as app.schemas.timeseries's
AnalysisEnvelope and app.schemas.mlops's MonitoringResponse. The envelope
itself (this file) is typed; the payload inside is validated by
app.services.orchestration_service, which is unit- and integration-tested
directly."""
from typing import Any

from pydantic import BaseModel


class OrchestrationEnvelope(BaseModel):
    data: dict[str, Any]
