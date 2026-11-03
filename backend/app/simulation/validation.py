"""Scenario validation (Phase 11 §3) — a pure, DB-independent check.

Every input here is already-loaded (sensor metadata, a baseline
observation count, a robust-statistics summary of the baseline window) so
this module stays as independently testable as app.anomaly.baseline and
app.condition.degradation. app.services.scenario_service is the only
caller, and it is the one that actually queries the database.

Every check either PASSES or produces an explicit, human-readable ERROR or
WARNING — nothing here silently clamps a magnitude, silently narrows a time
range, or silently swaps a unit. A scenario with any error is REJECTED
(status stays DRAFT; it cannot be run) — see
app.services.scenario_service.validate_scenario.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Optional

MIN_BASELINE_OBSERVATIONS = 30  # matches app.anomaly.baseline.MIN_FOR_BASELINE's "a baseline this small isn't trustworthy" reasoning
MAX_MAGNITUDE_ROBUST_STD_MULTIPLE = 10.0  # magnitude may not exceed this many baseline robust-std-equivalents
MAX_MAGNITUDE_RANGE_MULTIPLE = 5.0  # fallback bound (robust std unavailable/zero): multiple of the observed baseline range
MAX_MODIFICATION_DURATION_FRACTION = 1.0  # a modification window may never exceed the baseline window itself
MIN_MULTI_SENSOR_COUNT = 2

VALID_SCENARIO_TYPES = ("level_shift", "trend_change", "variability_change", "temporary_spike", "drift", "missing_data_gap", "multi_sensor")
# temporary_spike and missing_data_gap must both be a bounded pulse/gap to
# remain meaningfully distinct scenario types — every other type is free to
# leave duration unset and run to the end of the baseline window (see
# app.simulation.perturbations' module docstring's universal window
# invariant: an unset duration is not a special case, just a wider window).
_REQUIRES_DURATION = ("temporary_spike", "missing_data_gap")


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checked_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SensorContext:
    """Everything about one sensor's real data the validator needs, already
    loaded by the service layer — never fetched by this module."""

    sensor_id: str
    unit: str
    expected_interval_seconds: Optional[float]
    baseline_observation_count: int
    baseline_median: Optional[float]
    baseline_robust_std: Optional[float]  # None if too few observations to compute
    baseline_min: Optional[float]
    baseline_max: Optional[float]


def _check_time_windows(
    errors: list[str], warnings: list[str], *,
    baseline_window_start: datetime, baseline_window_end: datetime,
    modification_start: Optional[datetime], modification_duration_seconds: Optional[float],
    expected_interval_seconds: Optional[float],
) -> None:
    if baseline_window_end <= baseline_window_start:
        errors.append("baseline_window_end must be after baseline_window_start.")
        return

    if modification_start is None:
        return  # multi_sensor top-level config — checked per sub-scenario instead

    if modification_start < baseline_window_start or modification_start >= baseline_window_end:
        errors.append(
            f"modification_start ({modification_start.isoformat()}) must fall within the baseline window "
            f"[{baseline_window_start.isoformat()}, {baseline_window_end.isoformat()})."
        )
        return

    if modification_duration_seconds is not None:
        if modification_duration_seconds <= 0:
            errors.append("modification_duration_seconds must be positive when provided.")
        else:
            baseline_span = (baseline_window_end - baseline_window_start).total_seconds()
            if modification_duration_seconds > baseline_span * MAX_MODIFICATION_DURATION_FRACTION:
                errors.append(
                    f"modification_duration_seconds ({modification_duration_seconds:.0f}s) extends past the end of "
                    f"the baseline window ({baseline_span:.0f}s total) — shorten the duration or widen "
                    "baseline_window_end. A modification can never be silently truncated to fit."
                )
            elif expected_interval_seconds and modification_duration_seconds < expected_interval_seconds:
                warnings.append(
                    f"modification_duration_seconds ({modification_duration_seconds:.0f}s) is shorter than this "
                    f"sensor's expected sampling interval ({expected_interval_seconds:.0f}s) — the modification is "
                    "likely to affect zero or one real observation."
                )


def _check_unit(errors: list[str], *, requested_unit: Optional[str], sensor_unit: str) -> None:
    if requested_unit is None:
        errors.append("unit is required and must match the sensor's real unit.")
        return
    if requested_unit.strip().lower() != sensor_unit.strip().lower():
        errors.append(
            f"unit '{requested_unit}' does not match sensor unit '{sensor_unit}' — a scenario's magnitude must be "
            "expressed in the sensor's own units. This platform never silently converts or assumes unit compatibility."
        )


def _check_magnitude_bounds(errors: list[str], warnings: list[str], *, magnitude: Optional[float], ctx: SensorContext) -> None:
    if magnitude is None:
        errors.append("magnitude is required.")
        return
    if magnitude == 0:
        warnings.append("magnitude is 0 — this scenario will not change any value relative to the real baseline data.")

    if ctx.baseline_robust_std and ctx.baseline_robust_std > 0:
        bound = MAX_MAGNITUDE_ROBUST_STD_MULTIPLE * ctx.baseline_robust_std
        if abs(magnitude) > bound:
            errors.append(
                f"magnitude ({magnitude:g}) exceeds the configured safety bound of "
                f"{MAX_MAGNITUDE_ROBUST_STD_MULTIPLE:g}x the baseline window's robust standard deviation "
                f"({ctx.baseline_robust_std:.4g}, bound={bound:.4g}). This bound exists so a scenario cannot silently "
                "produce values with no relationship to how this sensor actually behaves. Choose a smaller "
                "magnitude, or widen the baseline window if this sensor's real variability is in fact larger."
            )
    elif ctx.baseline_min is not None and ctx.baseline_max is not None:
        observed_range = ctx.baseline_max - ctx.baseline_min
        bound = MAX_MAGNITUDE_RANGE_MULTIPLE * observed_range if observed_range > 0 else None
        if bound is not None and abs(magnitude) > bound:
            errors.append(
                f"magnitude ({magnitude:g}) exceeds {MAX_MAGNITUDE_RANGE_MULTIPLE:g}x the baseline window's observed "
                f"range ({observed_range:.4g}, bound={bound:.4g}) — the fallback safety bound used when there is too "
                "little baseline data to compute a robust standard deviation."
            )


def validate_single_sensor_scenario(
    *,
    scenario_type: str,
    sensor: SensorContext,
    baseline_window_start: datetime,
    baseline_window_end: datetime,
    modification_start: Optional[datetime],
    modification_duration_seconds: Optional[float],
    magnitude: Optional[float],
    unit: Optional[str],
    parameters: Optional[dict[str, Any]] = None,
) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    parameters = parameters or {}

    if scenario_type not in VALID_SCENARIO_TYPES:
        errors.append(f"Unknown scenario_type '{scenario_type}'. Supported: {VALID_SCENARIO_TYPES}.")
    elif scenario_type == "multi_sensor":
        errors.append("validate_single_sensor_scenario was called with scenario_type='multi_sensor' — use validate_multi_sensor_scenario instead.")

    if sensor.baseline_observation_count < MIN_BASELINE_OBSERVATIONS:
        errors.append(
            f"The baseline window has only {sensor.baseline_observation_count} real observation(s) for this "
            f"sensor; at least {MIN_BASELINE_OBSERVATIONS} are required so the scenario is built from a "
            "statistically meaningful sample of real data, not a handful of points."
        )

    _check_time_windows(
        errors, warnings, baseline_window_start=baseline_window_start, baseline_window_end=baseline_window_end,
        modification_start=modification_start, modification_duration_seconds=modification_duration_seconds,
        expected_interval_seconds=sensor.expected_interval_seconds,
    )

    if scenario_type in _REQUIRES_DURATION and modification_duration_seconds is None:
        errors.append(f"scenario_type '{scenario_type}' requires modification_duration_seconds.")

    if scenario_type == "temporary_spike":
        shape = parameters.get("shape", "rectangular")
        if shape not in ("rectangular", "triangular"):
            errors.append(f"parameters.shape '{shape}' is not supported for temporary_spike — use 'rectangular' or 'triangular'.")

    if scenario_type == "variability_change" and magnitude is not None and magnitude < 0:
        errors.append("variability_change magnitude must be >= 0 — it scales existing deviation from a rolling median, it cannot be negative.")

    _check_unit(errors, requested_unit=unit, sensor_unit=sensor.unit)

    if scenario_type == "missing_data_gap":
        # magnitude here is a DROP FRACTION in [0, 1] of the in-window
        # observations to remove (see
        # app.simulation.perturbations.apply_missing_data_gap) — not a
        # sensor-units value, so the generic robust-std/range magnitude
        # bound below does not apply, and unlike every other type, None is
        # a valid magnitude (it is treated as 1.0 — "drop the whole
        # window" — at run time).
        if magnitude is not None:
            if magnitude < 0 or magnitude > 1.0:
                errors.append(
                    f"missing_data_gap magnitude ({magnitude:g}) must be a fraction in [0, 1] — the share of "
                    "in-window observations to drop. It is not expressed in the sensor's units like every other "
                    "scenario type."
                )
            elif magnitude == 0:
                warnings.append("missing_data_gap magnitude is 0 — this scenario will not drop any observations relative to the real baseline data.")
    else:
        _check_magnitude_bounds(errors, warnings, magnitude=magnitude, ctx=sensor)

    return ValidationResult(ok=len(errors) == 0, errors=errors, warnings=warnings)


def validate_multi_sensor_scenario(
    *,
    sub_configs: list[dict[str, Any]],
    sensor_scope: list[str],
    sensors_by_id: dict[str, SensorContext],
    baseline_window_start: datetime,
    baseline_window_end: datetime,
) -> ValidationResult:
    """MULTI_SENSOR is deliberately the ONLY scenario type where more than
    one sensor is perturbed — and each sub-scenario is validated (and later
    applied) completely independently. This function never checks or
    implies any relationship BETWEEN sensors — see app.simulation's package
    docstring: "never auto-infer causality.\""""
    errors: list[str] = []
    warnings: list[str] = []

    if len(sub_configs) < MIN_MULTI_SENSOR_COUNT:
        errors.append(f"multi_sensor requires at least {MIN_MULTI_SENSOR_COUNT} per-sensor sub-configurations in parameters.per_sensor (found {len(sub_configs)}).")

    seen_sensor_ids: set[str] = set()
    for i, cfg in enumerate(sub_configs):
        sensor_id = cfg.get("sensor_id")
        if not sensor_id:
            errors.append(f"parameters.per_sensor[{i}] is missing 'sensor_id'.")
            continue
        if sensor_id in seen_sensor_ids:
            errors.append(f"parameters.per_sensor lists sensor '{sensor_id}' more than once.")
        seen_sensor_ids.add(sensor_id)
        if sensor_id not in sensor_scope:
            errors.append(f"parameters.per_sensor[{i}]'s sensor_id '{sensor_id}' is not in this scenario's sensor_scope.")
            continue
        sensor = sensors_by_id.get(sensor_id)
        if sensor is None:
            errors.append(f"No sensor context was loaded for '{sensor_id}'.")
            continue
        sub_scenario_type = cfg.get("scenario_type")
        try:
            modification_start = datetime.fromisoformat(cfg["modification_start"]) if cfg.get("modification_start") else None
        except (KeyError, ValueError):
            errors.append(f"parameters.per_sensor[{i}] has an invalid or missing modification_start.")
            modification_start = None

        sub_result = validate_single_sensor_scenario(
            scenario_type=sub_scenario_type,
            sensor=sensor,
            baseline_window_start=baseline_window_start,
            baseline_window_end=baseline_window_end,
            modification_start=modification_start,
            modification_duration_seconds=cfg.get("modification_duration_seconds"),
            magnitude=cfg.get("magnitude"),
            unit=cfg.get("unit"),
            parameters=cfg.get("parameters"),
        )
        errors += [f"per_sensor[{i}] ({sensor_id}): {e}" for e in sub_result.errors]
        warnings += [f"per_sensor[{i}] ({sensor_id}): {w}" for w in sub_result.warnings]

    return ValidationResult(ok=len(errors) == 0, errors=errors, warnings=warnings)
