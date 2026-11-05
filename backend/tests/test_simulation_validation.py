"""Unit tests for app.simulation.validation — pure,
DB-independent checks. Confirms scenarios are REJECTED (never silently
clamped) for: insufficient baseline data, unit mismatch, out-of-bounds
magnitude, a modification window outside/exceeding the baseline window,
and multi-sensor configs with too few or malformed sub-scenarios."""
from datetime import datetime, timedelta, timezone

import pytest

from app.simulation import validation as v

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _ctx(**overrides) -> v.SensorContext:
    defaults = dict(
        sensor_id="s1", unit="celsius", expected_interval_seconds=300.0,
        baseline_observation_count=200, baseline_median=50.0, baseline_robust_std=2.0, baseline_min=40.0, baseline_max=60.0,
    )
    defaults.update(overrides)
    return v.SensorContext(**defaults)


class TestSingleSensorValidation:
    def test_a_well_formed_scenario_passes(self):
        result = v.validate_single_sensor_scenario(
            scenario_type="level_shift", sensor=_ctx(), baseline_window_start=BASE, baseline_window_end=BASE + timedelta(days=1),
            modification_start=BASE + timedelta(hours=12), modification_duration_seconds=None, magnitude=5.0, unit="celsius",
        )
        assert result.ok
        assert result.errors == []

    def test_rejects_insufficient_baseline_observations(self):
        result = v.validate_single_sensor_scenario(
            scenario_type="level_shift", sensor=_ctx(baseline_observation_count=5), baseline_window_start=BASE, baseline_window_end=BASE + timedelta(days=1),
            modification_start=BASE + timedelta(hours=1), modification_duration_seconds=None, magnitude=5.0, unit="celsius",
        )
        assert not result.ok
        assert any("real observation" in e for e in result.errors)

    def test_rejects_unit_mismatch(self):
        result = v.validate_single_sensor_scenario(
            scenario_type="level_shift", sensor=_ctx(unit="celsius"), baseline_window_start=BASE, baseline_window_end=BASE + timedelta(days=1),
            modification_start=BASE + timedelta(hours=1), modification_duration_seconds=None, magnitude=5.0, unit="fahrenheit",
        )
        assert not result.ok
        assert any("does not match sensor unit" in e for e in result.errors)

    def test_rejects_magnitude_beyond_the_robust_std_bound(self):
        result = v.validate_single_sensor_scenario(
            scenario_type="level_shift", sensor=_ctx(baseline_robust_std=1.0), baseline_window_start=BASE, baseline_window_end=BASE + timedelta(days=1),
            modification_start=BASE + timedelta(hours=1), modification_duration_seconds=None,
            magnitude=v.MAX_MAGNITUDE_ROBUST_STD_MULTIPLE * 1.0 + 5, unit="celsius",
        )
        assert not result.ok
        assert any("exceeds the configured safety bound" in e for e in result.errors)

    def test_accepts_magnitude_exactly_at_the_bound(self):
        result = v.validate_single_sensor_scenario(
            scenario_type="level_shift", sensor=_ctx(baseline_robust_std=1.0), baseline_window_start=BASE, baseline_window_end=BASE + timedelta(days=1),
            modification_start=BASE + timedelta(hours=1), modification_duration_seconds=None,
            magnitude=v.MAX_MAGNITUDE_ROBUST_STD_MULTIPLE * 1.0, unit="celsius",
        )
        assert result.ok

    def test_never_silently_clamps_it_rejects_with_an_explicit_message(self):
        """: 'reject unsafe scenarios, never silently clamp without
        explanation' — the out-of-bounds magnitude passed through
        unmodified in the error, it was never reduced to fit."""
        huge_magnitude = 10_000.0
        result = v.validate_single_sensor_scenario(
            scenario_type="level_shift", sensor=_ctx(baseline_robust_std=1.0), baseline_window_start=BASE, baseline_window_end=BASE + timedelta(days=1),
            modification_start=BASE + timedelta(hours=1), modification_duration_seconds=None, magnitude=huge_magnitude, unit="celsius",
        )
        assert not result.ok
        assert any(str(huge_magnitude).split(".")[0] in e or "1e+04" in e or f"{huge_magnitude:g}" in e for e in result.errors)

    def test_rejects_modification_start_before_baseline_window(self):
        result = v.validate_single_sensor_scenario(
            scenario_type="level_shift", sensor=_ctx(), baseline_window_start=BASE, baseline_window_end=BASE + timedelta(days=1),
            modification_start=BASE - timedelta(hours=1), modification_duration_seconds=None, magnitude=1.0, unit="celsius",
        )
        assert not result.ok
        assert any("must fall within the baseline window" in e for e in result.errors)

    def test_rejects_modification_duration_extending_past_baseline_window(self):
        result = v.validate_single_sensor_scenario(
            scenario_type="level_shift", sensor=_ctx(), baseline_window_start=BASE, baseline_window_end=BASE + timedelta(hours=2),
            modification_start=BASE + timedelta(hours=1), modification_duration_seconds=3600 * 5, magnitude=1.0, unit="celsius",
        )
        assert not result.ok
        assert any("extends past the end of the baseline window" in e for e in result.errors)

    def test_temporary_spike_requires_duration(self):
        result = v.validate_single_sensor_scenario(
            scenario_type="temporary_spike", sensor=_ctx(), baseline_window_start=BASE, baseline_window_end=BASE + timedelta(days=1),
            modification_start=BASE + timedelta(hours=1), modification_duration_seconds=None, magnitude=1.0, unit="celsius",
        )
        assert not result.ok
        assert any("requires modification_duration_seconds" in e for e in result.errors)

    def test_drift_does_not_require_duration(self):
        result = v.validate_single_sensor_scenario(
            scenario_type="drift", sensor=_ctx(), baseline_window_start=BASE, baseline_window_end=BASE + timedelta(days=1),
            modification_start=BASE + timedelta(hours=1), modification_duration_seconds=None, magnitude=1.0, unit="celsius",
        )
        assert result.ok

    def test_variability_change_rejects_negative_magnitude(self):
        result = v.validate_single_sensor_scenario(
            scenario_type="variability_change", sensor=_ctx(), baseline_window_start=BASE, baseline_window_end=BASE + timedelta(days=1),
            modification_start=BASE + timedelta(hours=1), modification_duration_seconds=None, magnitude=-1.0, unit="celsius",
        )
        assert not result.ok

    def test_warns_when_duration_shorter_than_expected_interval(self):
        result = v.validate_single_sensor_scenario(
            scenario_type="temporary_spike", sensor=_ctx(expected_interval_seconds=300.0), baseline_window_start=BASE, baseline_window_end=BASE + timedelta(days=1),
            modification_start=BASE + timedelta(hours=1), modification_duration_seconds=60.0, magnitude=1.0, unit="celsius",
        )
        assert result.ok  # a warning, not an error
        assert any("shorter than this sensor's expected sampling interval" in w for w in result.warnings)

    def test_unknown_scenario_type_is_rejected(self):
        result = v.validate_single_sensor_scenario(
            scenario_type="teleportation", sensor=_ctx(), baseline_window_start=BASE, baseline_window_end=BASE + timedelta(days=1),
            modification_start=BASE + timedelta(hours=1), modification_duration_seconds=None, magnitude=1.0, unit="celsius",
        )
        assert not result.ok


class TestMissingDataGapValidation:
    """missing_data_gap's magnitude is a [0, 1] drop fraction, not a
    sensor-units value — it must bypass the generic robust-std/range bound
    (_check_magnitude_bounds) and get its own fraction check instead (see
    app.simulation.validation.validate_single_sensor_scenario)."""

    def test_requires_duration(self):
        result = v.validate_single_sensor_scenario(
            scenario_type="missing_data_gap", sensor=_ctx(), baseline_window_start=BASE, baseline_window_end=BASE + timedelta(days=1),
            modification_start=BASE + timedelta(hours=1), modification_duration_seconds=None, magnitude=1.0, unit="celsius",
        )
        assert not result.ok
        assert any("requires modification_duration_seconds" in e for e in result.errors)

    def test_fraction_magnitude_within_unit_interval_passes(self):
        result = v.validate_single_sensor_scenario(
            scenario_type="missing_data_gap", sensor=_ctx(), baseline_window_start=BASE, baseline_window_end=BASE + timedelta(days=1),
            modification_start=BASE + timedelta(hours=1), modification_duration_seconds=3600.0, magnitude=0.5, unit="celsius",
        )
        assert result.ok

    def test_none_magnitude_is_valid_and_means_drop_the_whole_window(self):
        result = v.validate_single_sensor_scenario(
            scenario_type="missing_data_gap", sensor=_ctx(), baseline_window_start=BASE, baseline_window_end=BASE + timedelta(days=1),
            modification_start=BASE + timedelta(hours=1), modification_duration_seconds=3600.0, magnitude=None, unit="celsius",
        )
        assert result.ok

    def test_rejects_magnitude_above_one(self):
        """A value like 5.0 would sail through the generic sensor-units
        bound check (small relative to baseline_robust_std) but must still
        be rejected here, because for this type it is not a sensor-units
        value at all — it is an invalid fraction."""
        result = v.validate_single_sensor_scenario(
            scenario_type="missing_data_gap", sensor=_ctx(), baseline_window_start=BASE, baseline_window_end=BASE + timedelta(days=1),
            modification_start=BASE + timedelta(hours=1), modification_duration_seconds=3600.0, magnitude=5.0, unit="celsius",
        )
        assert not result.ok
        assert any("must be a fraction in [0, 1]" in e for e in result.errors)

    def test_rejects_negative_magnitude(self):
        result = v.validate_single_sensor_scenario(
            scenario_type="missing_data_gap", sensor=_ctx(), baseline_window_start=BASE, baseline_window_end=BASE + timedelta(days=1),
            modification_start=BASE + timedelta(hours=1), modification_duration_seconds=3600.0, magnitude=-0.2, unit="celsius",
        )
        assert not result.ok

    def test_zero_magnitude_is_a_warning_not_an_error(self):
        result = v.validate_single_sensor_scenario(
            scenario_type="missing_data_gap", sensor=_ctx(), baseline_window_start=BASE, baseline_window_end=BASE + timedelta(days=1),
            modification_start=BASE + timedelta(hours=1), modification_duration_seconds=3600.0, magnitude=0.0, unit="celsius",
        )
        assert result.ok
        assert any("will not drop any observations" in w for w in result.warnings)


class TestMultiSensorValidation:
    def test_requires_at_least_two_sub_configs(self):
        result = v.validate_multi_sensor_scenario(
            sub_configs=[{"sensor_id": "s1", "scenario_type": "level_shift", "modification_start": BASE.isoformat(), "magnitude": 1.0, "unit": "celsius"}],
            sensor_scope=["s1"], sensors_by_id={"s1": _ctx()}, baseline_window_start=BASE, baseline_window_end=BASE + timedelta(days=1),
        )
        assert not result.ok
        assert any("at least" in e for e in result.errors)

    def test_valid_two_sensor_config_passes(self):
        sub_configs = [
            {"sensor_id": "s1", "scenario_type": "level_shift", "modification_start": (BASE + timedelta(hours=1)).isoformat(), "magnitude": 1.0, "unit": "celsius"},
            {"sensor_id": "s2", "scenario_type": "drift", "modification_start": (BASE + timedelta(hours=2)).isoformat(), "magnitude": 2.0, "unit": "mm/s"},
        ]
        result = v.validate_multi_sensor_scenario(
            sub_configs=sub_configs, sensor_scope=["s1", "s2"],
            sensors_by_id={"s1": _ctx(sensor_id="s1", unit="celsius"), "s2": _ctx(sensor_id="s2", unit="mm/s")},
            baseline_window_start=BASE, baseline_window_end=BASE + timedelta(days=1),
        )
        assert result.ok

    def test_rejects_duplicate_sensor_ids(self):
        sub_configs = [
            {"sensor_id": "s1", "scenario_type": "level_shift", "modification_start": (BASE + timedelta(hours=1)).isoformat(), "magnitude": 1.0, "unit": "celsius"},
            {"sensor_id": "s1", "scenario_type": "drift", "modification_start": (BASE + timedelta(hours=2)).isoformat(), "magnitude": 2.0, "unit": "celsius"},
        ]
        result = v.validate_multi_sensor_scenario(
            sub_configs=sub_configs, sensor_scope=["s1"], sensors_by_id={"s1": _ctx()}, baseline_window_start=BASE, baseline_window_end=BASE + timedelta(days=1),
        )
        assert not result.ok
        assert any("more than once" in e for e in result.errors)

    def test_rejects_sensor_not_in_scope(self):
        sub_configs = [
            {"sensor_id": "s1", "scenario_type": "level_shift", "modification_start": (BASE + timedelta(hours=1)).isoformat(), "magnitude": 1.0, "unit": "celsius"},
            {"sensor_id": "s_outside", "scenario_type": "drift", "modification_start": (BASE + timedelta(hours=2)).isoformat(), "magnitude": 2.0, "unit": "celsius"},
        ]
        result = v.validate_multi_sensor_scenario(
            sub_configs=sub_configs, sensor_scope=["s1"], sensors_by_id={"s1": _ctx()}, baseline_window_start=BASE, baseline_window_end=BASE + timedelta(days=1),
        )
        assert not result.ok
        assert any("not in this scenario's sensor_scope" in e for e in result.errors)
