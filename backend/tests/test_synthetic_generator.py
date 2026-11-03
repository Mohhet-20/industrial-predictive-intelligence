from dataclasses import replace
from datetime import datetime, timedelta, timezone

import numpy as np

from app.synthetic.generator import SensorSpec, _generate_sensor_series, generate_dataset

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
TIMESTAMPS = [BASE + timedelta(minutes=5 * i) for i in range(2000)]

SPEC = SensorSpec(
    code="t", name="t", measurement_type="temperature", unit="C", baseline=50.0, daily_amplitude=2.0,
    noise_std=0.5, expected_min=0.0, expected_max=100.0, spike_probability=0.0, spike_magnitude=20.0,
    missing_probability=0.0,
)


def _values(spec: SensorSpec, seed: int = 7) -> np.ndarray:
    return np.array([v for _, v in _generate_sensor_series(np.random.default_rng(seed), spec, TIMESTAMPS)])


def test_a_spike_is_guaranteed_in_the_most_recent_window():
    with_spike = _values(SPEC)
    without = _values(replace(SPEC, spike_magnitude=0.0))
    diff = with_spike - without
    changed = np.flatnonzero(np.abs(diff) > 0.01)
    assert len(changed) == 1
    idx = int(changed[0])
    assert idx >= int(len(TIMESTAMPS) * 0.85)
    assert abs(diff[idx] - 20.0) < 0.002  # values are rounded to 3 decimals


def test_the_guarantee_consumes_no_random_numbers():
    # Same seed, so every value except the one guaranteed spike must be identical: later sensors in a dataset
    # (which share the RNG stream) are therefore unaffected by the guarantee.
    with_spike = _values(SPEC)
    without = _values(replace(SPEC, spike_magnitude=0.0))
    assert np.count_nonzero(np.abs(with_spike - without) > 0.01) == 1


def test_no_extra_spike_when_one_already_falls_in_the_window():
    spec = replace(SPEC, spike_probability=1.0)  # every sample spikes, so the window is already covered
    values = _values(spec) - _values(replace(spec, spike_magnitude=0.0))
    assert np.allclose(np.abs(values), 20.0, atol=0.002)


def test_every_spiking_sensor_in_the_demo_dataset_has_a_recent_spike():
    data = generate_dataset(seed=42, days=21, end_time=BASE + timedelta(days=21))
    end = data["end_time"]
    recent = end - (end - data["start_time"]) * 0.15
    by_sensor: dict[tuple[str, str], list[float]] = {}
    for r in data["records"]:
        if datetime.fromisoformat(r["timestamp"]) >= recent:
            by_sensor.setdefault((r["asset_code"], r["sensor_code"]), []).append(r["value"])
    for asset in data["assets"]:
        for sensor in asset.sensors:
            if sensor.spike_magnitude <= 0:
                continue
            vals = np.array(by_sensor[(asset.code, sensor.code)])
            assert np.max(np.abs(vals - np.median(vals))) >= sensor.spike_magnitude * 0.6, (asset.code, sensor.code)
