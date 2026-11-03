"""Scenario simulation / what-if intelligence domain tables (Phase 11).

THIS IS SCENARIO ANALYSIS, NOT A PERFECT PHYSICAL DIGITAL TWIN — see
app.simulation's package docstring and docs/SCENARIO_SIMULATION.md. Nothing
in this schema, and nothing that writes to it, ever modifies a real
SensorMeasurement row: a `Scenario` and its `ScenarioRun`s are entirely
additive, application-layer records built from a COPY of real historical
data (see app.simulation.perturbations — pure functions over a
pandas Series, never the ORM).

- `Scenario` — a saved, reusable hypothetical config: which sensor(s), what
  kind of modification, over what real historical window. Mirrors
  app.models.condition.ForecastModel's immutable-config-plus-status
  pattern: editing a scenario's parameters after it has been RUN creates a
  new version (see app.services.scenario_service.update_scenario) rather
  than mutating a config a prior run already used — so a ScenarioRun's
  `config_snapshot` always stays meaningful.
- `ScenarioRun` — one EXECUTION of a scenario: loads the real baseline
  series, applies the perturbation, runs the exact same
  app.anomaly/app.condition/app.timeseries functions production uses on
  real data (never a re-implementation), and stores the full
  baseline-vs-simulated comparison. Mirrors app.models.condition.
  ConditionRun/app.models.intelligence.ModelRun's run-record pattern.
  `scenario_version` freezes which version of the Scenario produced this
  run, so a later edit to the scenario can never retroactively change what
  an old run's provenance claims it evaluated (§11).

Design choice worth recording: scenario results are NEVER written into
app.models.intelligence.Anomaly / app.models.condition.DegradationSignal /
app.models.condition.ForecastRun. Those tables are the real, production
detection/condition/forecast history; mixing simulated output into them —
even with an is_synthetic-style flag — would put "did this really happen"
and "what if this happened" one query away from being confused with each
other. Every simulated output lives only in `ScenarioRun.results`, labeled
"SIMULATED SCENARIO RESULT" wherever it is rendered (API, frontend,
copilot) — see app.simulation.pipeline.
"""
import enum
import uuid

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, utcnow


class ScenarioType(str, enum.Enum):
    LEVEL_SHIFT = "level_shift"
    TREND_CHANGE = "trend_change"
    VARIABILITY_CHANGE = "variability_change"
    TEMPORARY_SPIKE = "temporary_spike"
    DRIFT = "drift"
    MISSING_DATA_GAP = "missing_data_gap"
    MULTI_SENSOR = "multi_sensor"


class ScenarioStatus(str, enum.Enum):
    DRAFT = "draft"          # created, not yet passed validation
    VALIDATED = "validated"  # passed app.simulation.validation — safe to run
    ARCHIVED = "archived"    # soft-deleted; runs and results are preserved (§12 delete = archive, never a hard delete of history)


class Scenario(TimestampMixin, Base):
    __tablename__ = "scenarios"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True)
    # The primary/first sensor this scenario concerns. For MULTI_SENSOR,
    # every sensor involved (including this one) is in `sensor_scope`.
    sensor_id: Mapped[str] = mapped_column(ForeignKey("sensors.id", ondelete="CASCADE"), nullable=False, index=True)
    sensor_scope: Mapped[list] = mapped_column(JSON, nullable=False)  # list[str] of sensor_ids — always includes sensor_id

    name: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    scenario_type: Mapped[ScenarioType] = mapped_column(Enum(ScenarioType, native_enum=False, length=30), nullable=False)

    # The REAL historical window whose data is copied and perturbed. Never
    # a future window — see app.simulation's package docstring: this
    # platform explores "what if this observed period had looked
    # different," not "what will happen," which is what keeps every
    # simulated point traceable to a real, already-collected observation.
    baseline_window_start: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    baseline_window_end: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), nullable=False)
    # Optional, independent (non-overlapping) reference period for deviation
    # evidence — same "reference must end before analysis window" rule
    # app.services.condition_service enforces. Null = auto-derived at run
    # time (see app.services.scenario_service) or skipped if not enough
    # historical data exists.
    reference_window_start: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)
    reference_window_end: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)

    # The sub-window within [baseline_window_start, baseline_window_end]
    # where the perturbation is actually applied (§1). Not used by
    # MULTI_SENSOR at the top level — see parameters.per_sensor.
    modification_start: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)
    modification_duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    magnitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(50), nullable=True)  # must match the sensor's real unit at validation time

    # Type-specific extras (rolling_window_observations, spike shape,
    # per_sensor sub-configs for MULTI_SENSOR, model_version_id to compare
    # against, include_forecast_comparison) — see
    # app.schemas.scenario.ScenarioParameters for the documented shape.
    parameters: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    status: Mapped[ScenarioStatus] = mapped_column(Enum(ScenarioStatus, native_enum=False, length=20), nullable=False, default=ScenarioStatus.DRAFT)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Full output of app.simulation.validation's last check — errors AND
    # warnings, never silently dropped. Null until validate is called.
    validation_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    runs: Mapped[list["ScenarioRun"]] = relationship(back_populates="scenario", cascade="all, delete-orphan", order_by="ScenarioRun.created_at")


class ScenarioRunStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ScenarioRunKind(str, enum.Enum):
    SINGLE = "single"                    # one baseline-vs-simulated comparison
    SENSITIVITY_SWEEP = "sensitivity_sweep"  # a magnitude sweep (§9) — no single "simulated" series, a set of them


class ScenarioRun(Base):
    """One execution of a Scenario. `config_snapshot` and `scenario_version`
    together make every run replayable and honestly attributable, even
    after the parent Scenario is later edited (a new version) or archived —
    see module docstring."""

    __tablename__ = "scenario_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    scenario_id: Mapped[str] = mapped_column(ForeignKey("scenarios.id", ondelete="CASCADE"), nullable=False, index=True)
    scenario_version: Mapped[int] = mapped_column(Integer, nullable=False)

    run_kind: Mapped[ScenarioRunKind] = mapped_column(Enum(ScenarioRunKind, native_enum=False, length=30), nullable=False, default=ScenarioRunKind.SINGLE)
    status: Mapped[ScenarioRunStatus] = mapped_column(Enum(ScenarioRunStatus, native_enum=False, length=20), nullable=False, default=ScenarioRunStatus.QUEUED)

    # A full, replayable snapshot of the Scenario config THIS run used —
    # copied at run creation, never re-read live from the (possibly later
    # edited) Scenario row.
    config_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    # Identifies exactly which analytical code produced this run's numbers
    # — function names/module paths, and (if a real trained detector was
    # referenced for comparison) that ModelVersion's id + version. Never
    # silently swapped for a newer model on a re-run without this changing
    # (§11, §16).
    pipeline_versions: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # The full baseline-vs-simulated bundle — see
    # app.simulation.pipeline.run_single_sensor_scenario's return shape.
    # Null until the run finishes (or forever, on FAILED).
    results: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    background_job_id: Mapped[str | None] = mapped_column(ForeignKey("background_jobs.id"), nullable=True)

    started_at: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped["DateTime | None"] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    scenario: Mapped["Scenario"] = relationship(back_populates="runs")
