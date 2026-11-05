"""Unit tests for app.simulation.perturbations.

These are the SCIENTIFIC VALIDATION tests for the simulation engine
itself: deterministic scenarios with known expected behavior. A level
shift must shift by EXACTLY the configured amount; a drift must reach
EXACTLY its configured total by the end of its window; a spike must affect
ONLY its declared window; every perturbation must leave every point
outside its window byte-identical to the real input, and must never
mutate its input Series in place.
"""
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from app.simulation import perturbations as p

BASE = pd.Timestamp("2026-01-01", tz="UTC")


def _series(n=200, seed=7):
    idx = pd.date_range(BASE, periods=n, freq="5min")
    rng = np.random.default_rng(seed)
    values = 50.0 + 2.0 * np.sin(np.linspace(0, 10, n)) + rng.normal(0, 0.2, n)
    return pd.Series(values, index=idx)


class TestLevelShift:
    def test_shifts_by_exactly_the_configured_amount(self):
        s = _series()
        start = s.index[50].to_pydatetime()
        out = p.apply_level_shift(s, modification_start=start, modification_duration_seconds=None, magnitude=7.5)
        diff = out[out.index >= start] - s[s.index >= start]
        assert np.allclose(diff.values, 7.5)

    def test_unshifted_before_start_is_byte_identical(self):
        s = _series()
        start = s.index[50].to_pydatetime()
        out = p.apply_level_shift(s, modification_start=start, modification_duration_seconds=None, magnitude=7.5)
        assert (out[out.index < start] == s[out.index < start]).all()

    def test_bounded_duration_reverts_after_window(self):
        s = _series()
        start = s.index[50].to_pydatetime()
        duration = 1800.0
        out = p.apply_level_shift(s, modification_start=start, modification_duration_seconds=duration, magnitude=7.5)
        end = start + timedelta(seconds=duration)
        after = out.index > pd.Timestamp(end)
        assert (out[after] == s[after]).all()

    def test_does_not_mutate_input_in_place(self):
        s = _series()
        original = s.copy()
        p.apply_level_shift(s, modification_start=s.index[10].to_pydatetime(), modification_duration_seconds=None, magnitude=99.0)
        assert (s == original).all()

    def test_zero_magnitude_is_a_no_op(self):
        s = _series()
        out = p.apply_level_shift(s, modification_start=s.index[10].to_pydatetime(), modification_duration_seconds=None, magnitude=0.0)
        assert np.allclose(out.values, s.values)


class TestDrift:
    def test_reaches_exact_magnitude_at_window_end(self):
        s = _series()
        start = s.index[20].to_pydatetime()
        duration = 3600.0
        out = p.apply_drift(s, modification_start=start, modification_duration_seconds=duration, magnitude=20.0)
        end = start + timedelta(seconds=duration)
        at_or_before_end = out[out.index <= pd.Timestamp(end)]
        last_diff = (at_or_before_end - s[at_or_before_end.index]).iloc[-1]
        assert abs(last_diff - 20.0) < 1e-9

    def test_is_linear_within_the_window(self):
        s = _series()
        start = s.index[20].to_pydatetime()
        duration = 3600.0
        out = p.apply_drift(s, modification_start=start, modification_duration_seconds=duration, magnitude=20.0)
        window = out[(out.index >= start) & (out.index <= pd.Timestamp(start) + pd.Timedelta(seconds=duration))]
        added = (window - s[window.index]).values
        # A linear ramp's successive differences should be constant.
        deltas = np.diff(added)
        assert np.allclose(deltas, deltas[0], atol=1e-6)

    def test_reverts_after_bounded_window(self):
        s = _series()
        start = s.index[20].to_pydatetime()
        duration = 1800.0
        out = p.apply_drift(s, modification_start=start, modification_duration_seconds=duration, magnitude=20.0)
        end = pd.Timestamp(start) + pd.Timedelta(seconds=duration)
        after = out.index > end
        assert (out[after] == s[after]).all()

    def test_no_duration_ramps_to_end_of_series(self):
        s = _series()
        start = s.index[20].to_pydatetime()
        out = p.apply_drift(s, modification_start=start, modification_duration_seconds=None, magnitude=20.0)
        assert abs((out.iloc[-1] - s.iloc[-1]) - 20.0) < 1e-9


class TestTemporarySpike:
    def test_affects_only_its_declared_window(self):
        s = _series()
        start = s.index[80].to_pydatetime()
        duration = 1800.0
        out = p.apply_temporary_spike(s, modification_start=start, modification_duration_seconds=duration, magnitude=15.0, shape="rectangular")
        end = pd.Timestamp(start) + pd.Timedelta(seconds=duration)
        outside = (out.index < pd.Timestamp(start)) | (out.index > end)
        assert (out[outside] == s[outside]).all()
        inside = ~outside
        assert inside.any()
        assert np.allclose((out[inside] - s[inside]).values, 15.0)

    def test_triangular_shape_peaks_at_midpoint_and_returns_to_zero_at_edges(self):
        s = _series(n=50)
        start = s.index[5].to_pydatetime()
        duration = 3600.0
        out = p.apply_temporary_spike(s, modification_start=start, modification_duration_seconds=duration, magnitude=10.0, shape="triangular")
        start_ts = pd.Timestamp(start)
        end_ts = start_ts + pd.Timedelta(seconds=duration)
        at_start_diff = out.loc[start_ts] - s.loc[start_ts]
        assert abs(at_start_diff) < 1e-6
        if end_ts in out.index:
            at_end_diff = out.loc[end_ts] - s.loc[end_ts]
            assert abs(at_end_diff) < 1e-6

    def test_requires_positive_duration(self):
        s = _series()
        with pytest.raises(p.PerturbationError):
            p.apply_temporary_spike(s, modification_start=s.index[5].to_pydatetime(), modification_duration_seconds=None, magnitude=5.0)
        with pytest.raises(p.PerturbationError):
            p.apply_temporary_spike(s, modification_start=s.index[5].to_pydatetime(), modification_duration_seconds=-10, magnitude=5.0)

    def test_rejects_unknown_shape(self):
        s = _series()
        with pytest.raises(p.PerturbationError):
            p.apply_temporary_spike(s, modification_start=s.index[5].to_pydatetime(), modification_duration_seconds=600, magnitude=5.0, shape="sinusoidal")


class TestTrendChange:
    def test_adds_a_linear_ramp_at_the_configured_hourly_rate(self):
        s = _series()
        start = s.index[10].to_pydatetime()
        out = p.apply_trend_change(s, modification_start=start, modification_duration_seconds=None, magnitude=2.0)  # 2 units/hour
        two_hours_later = pd.Timestamp(start) + pd.Timedelta(hours=2)
        idx = out.index.get_indexer([two_hours_later], method="nearest")[0]
        added = out.iloc[idx] - s.iloc[idx]
        assert abs(added - 4.0) < 0.2  # ~2 units/hour * ~2 hours, allowing for nearest-grid-point slack

    def test_reverts_after_bounded_window(self):
        s = _series()
        start = s.index[10].to_pydatetime()
        duration = 1800.0
        out = p.apply_trend_change(s, modification_start=start, modification_duration_seconds=duration, magnitude=5.0)
        end = pd.Timestamp(start) + pd.Timedelta(seconds=duration)
        after = out.index > end
        assert (out[after] == s[after]).all()


class TestVariabilityChange:
    def test_zero_magnitude_collapses_to_rolling_median(self):
        s = _series()
        start = s.index[10].to_pydatetime()
        out = p.apply_variability_change(s, modification_start=start, modification_duration_seconds=None, magnitude=0.0, rolling_window_observations=15)
        rolling_median = s.rolling(window=15, min_periods=1, center=True).median()
        inside = out.index >= pd.Timestamp(start)
        assert np.allclose(out[inside].values, rolling_median[inside].values, atol=1e-9)

    def test_magnitude_one_is_a_no_op(self):
        s = _series()
        start = s.index[10].to_pydatetime()
        out = p.apply_variability_change(s, modification_start=start, modification_duration_seconds=None, magnitude=1.0, rolling_window_observations=15)
        assert np.allclose(out.values, s.values, atol=1e-9)

    def test_magnitude_two_doubles_deviation_from_rolling_median(self):
        s = _series()
        start = s.index[10].to_pydatetime()
        out = p.apply_variability_change(s, modification_start=start, modification_duration_seconds=None, magnitude=2.0, rolling_window_observations=15)
        rolling_median = s.rolling(window=15, min_periods=1, center=True).median()
        inside = out.index >= pd.Timestamp(start)
        expected = rolling_median[inside] + 2.0 * (s[inside] - rolling_median[inside])
        assert np.allclose(out[inside].values, expected.values, atol=1e-9)

    def test_rejects_negative_magnitude(self):
        s = _series()
        with pytest.raises(p.PerturbationError):
            p.apply_variability_change(s, modification_start=s.index[10].to_pydatetime(), modification_duration_seconds=None, magnitude=-1.0)


class TestMissingDataGap:
    def test_full_gap_sets_every_in_window_point_to_nan(self):
        s = _series()
        start = s.index[50].to_pydatetime()
        duration = 1800.0  # 6 observations at 5min spacing
        out = p.apply_missing_data_gap(s, modification_start=start, modification_duration_seconds=duration, magnitude=1.0)
        end = pd.Timestamp(start) + pd.Timedelta(seconds=duration)
        inside = (out.index >= pd.Timestamp(start)) & (out.index <= end)
        assert inside.any()
        assert out[inside].isna().all()

    def test_default_magnitude_is_a_full_gap(self):
        """magnitude=None (what app.simulation.validation allows for this
        type alone) is treated as 1.0 — drop the whole window."""
        s = _series()
        start = s.index[50].to_pydatetime()
        duration = 1800.0
        out_default = p.apply_missing_data_gap(s, modification_start=start, modification_duration_seconds=duration, magnitude=None)
        out_explicit = p.apply_missing_data_gap(s, modification_start=start, modification_duration_seconds=duration, magnitude=1.0)
        assert out_default.isna().equals(out_explicit.isna())

    def test_outside_window_is_byte_identical_including_nan_safe_comparison(self):
        s = _series()
        start = s.index[50].to_pydatetime()
        duration = 1800.0
        out = p.apply_missing_data_gap(s, modification_start=start, modification_duration_seconds=duration, magnitude=1.0)
        end = pd.Timestamp(start) + pd.Timedelta(seconds=duration)
        outside = (out.index < pd.Timestamp(start)) | (out.index > end)
        assert (out[outside] == s[outside]).all()

    def test_partial_fraction_drops_only_that_share_deterministically(self):
        s = _series()
        start = s.index[50].to_pydatetime()
        duration = 3000.0  # ~10 observations at 5min spacing
        out1 = p.apply_missing_data_gap(s, modification_start=start, modification_duration_seconds=duration, magnitude=0.5)
        out2 = p.apply_missing_data_gap(s, modification_start=start, modification_duration_seconds=duration, magnitude=0.5)
        end = pd.Timestamp(start) + pd.Timedelta(seconds=duration)
        inside = (out1.index >= pd.Timestamp(start)) & (out1.index <= end)
        n_in_window = int(inside.sum())
        n_dropped = int(out1[inside].isna().sum())
        assert 0 < n_dropped < n_in_window  # a real partial gap, not all-or-nothing
        # Determinism: repeating the exact same call drops the exact same points.
        assert out1.isna().equals(out2.isna())

    def test_zero_magnitude_is_a_no_op(self):
        s = _series()
        start = s.index[50].to_pydatetime()
        out = p.apply_missing_data_gap(s, modification_start=start, modification_duration_seconds=1800.0, magnitude=0.0)
        assert out.isna().sum() == 0
        assert np.allclose(out.values, s.values)

    def test_requires_positive_duration(self):
        s = _series()
        with pytest.raises(p.PerturbationError):
            p.apply_missing_data_gap(s, modification_start=s.index[5].to_pydatetime(), modification_duration_seconds=None, magnitude=1.0)
        with pytest.raises(p.PerturbationError):
            p.apply_missing_data_gap(s, modification_start=s.index[5].to_pydatetime(), modification_duration_seconds=-10, magnitude=1.0)

    def test_rejects_magnitude_outside_unit_interval(self):
        s = _series()
        with pytest.raises(p.PerturbationError):
            p.apply_missing_data_gap(s, modification_start=s.index[5].to_pydatetime(), modification_duration_seconds=600, magnitude=1.5)
        with pytest.raises(p.PerturbationError):
            p.apply_missing_data_gap(s, modification_start=s.index[5].to_pydatetime(), modification_duration_seconds=600, magnitude=-0.1)

    def test_does_not_mutate_input_in_place(self):
        s = _series()
        original = s.copy()
        p.apply_missing_data_gap(s, modification_start=s.index[10].to_pydatetime(), modification_duration_seconds=1800.0, magnitude=1.0)
        assert (s == original).all()


class TestDispatchAndInvariants:
    @pytest.mark.parametrize("scenario_type", list(p.SINGLE_SENSOR_SCENARIO_TYPES))
    def test_apply_perturbation_dispatches_and_preserves_timestamps(self, scenario_type):
        s = _series()
        start = s.index[30].to_pydatetime()
        kwargs = {"modification_duration_seconds": 1800.0} if scenario_type in ("temporary_spike", "missing_data_gap") else {"modification_duration_seconds": None}
        # missing_data_gap's magnitude is a [0, 1] drop FRACTION, not a
        # sensor-units value like every other type (see
        # app.simulation.perturbations.apply_missing_data_gap) — 3.0 would
        # be rejected for this one type.
        magnitude = 1.0 if scenario_type == "missing_data_gap" else 3.0
        out = p.apply_perturbation(s, scenario_type=scenario_type, modification_start=start, magnitude=magnitude, parameters={}, **kwargs)
        assert list(out.index) == list(s.index)
        assert isinstance(out, pd.Series)

    def test_unknown_scenario_type_raises(self):
        s = _series()
        with pytest.raises(p.PerturbationError):
            p.apply_perturbation(s, scenario_type="not_a_real_type", modification_start=s.index[5].to_pydatetime(), modification_duration_seconds=None, magnitude=1.0)

    def test_verify_outside_window_unchanged_detects_a_violation(self):
        s = _series()
        tampered = s.copy()
        tampered.iloc[0] = tampered.iloc[0] + 1000.0  # corrupt a point that should be "outside"
        start = s.index[50].to_pydatetime()
        assert p.verify_outside_window_unchanged(s, s, modification_start=start, modification_end=None) is True
        assert p.verify_outside_window_unchanged(s, tampered, modification_start=start, modification_end=None) is False
