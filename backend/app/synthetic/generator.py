"""Deterministic synthetic demo dataset generator.

Everything this module produces is SYNTHETIC — fabricated for demonstration
purposes. It is not real industrial sensor data and none of its "anomalies"
represent a real equipment failure. Every asset/sensor/organization name it
creates is marked accordingly so this is unmistakable downstream (in the
API, in the UI, in exports).

Determinism: given the same seed, this generator produces byte-identical
output every time (uses numpy's Generator API, not global random state), so
demo scenarios are reproducible across machines and across re-runs.

Realism, deliberately imperfect: normal operating behavior with daily
seasonality and noise, a slow drift on one sensor (simulating gradual wear),
a handful of sudden step-change/spike anomalies, randomly dropped
individual readings (missing data), one deliberate contiguous outage window
per asset (a data gap), and a few out-of-range values relative to each
sensor's declared operating envelope. The data is NOT perfectly clean, and
it is not supposed to be.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np

SYNTHETIC_ORG_NAME = "Riverbend Manufacturing (Synthetic Demo)"
SYNTHETIC_DATA_SOURCE_NAME = "synthetic-demo-generator-v1"


@dataclass
class SensorSpec:
    code: str
    name: str
    measurement_type: str
    unit: str
    baseline: float
    daily_amplitude: float          # how much the value swings over a 24h cycle
    noise_std: float
    expected_min: float
    expected_max: float
    expected_interval_seconds: float = 300.0  # 5 minutes
    drift_per_day: float = 0.0      # gradual wear/degradation trend
    spike_probability: float = 0.0008  # probability per sample of a sudden spike/step anomaly
    spike_magnitude: float = 0.0    # additive magnitude when a spike occurs (sign randomized)
    missing_probability: float = 0.01  # probability per sample of a dropped (missing) reading


@dataclass
class AssetSpec:
    code: str
    name: str
    asset_type: str
    location: str
    sensors: list[SensorSpec] = field(default_factory=list)


def default_scenario() -> list[AssetSpec]:
    """A small fleet: a pump, a compressor, and a conveyor motor — enough
    variety to exercise multiple sensor types and quality-issue patterns
    without needing an enormous dataset."""
    return [
        AssetSpec(
            code="PUMP-07",
            name="Centrifugal Pump 07 (Synthetic)",
            asset_type="centrifugal_pump",
            location="Building A — Utilities",
            sensors=[
                SensorSpec(
                    code="bearing_temp_de",
                    name="Bearing Temperature (Drive End)",
                    measurement_type="temperature",
                    unit="celsius",
                    baseline=58.0,
                    daily_amplitude=3.0,
                    noise_std=0.6,
                    expected_min=20.0,
                    expected_max=90.0,
                    drift_per_day=0.05,  # slowly overheating — the "wear" sensor
                    spike_probability=0.0006,
                    spike_magnitude=18.0,
                    missing_probability=0.01,
                ),
                SensorSpec(
                    code="vibration_rms",
                    name="Vibration RMS",
                    measurement_type="vibration",
                    unit="mm_s",
                    baseline=2.1,
                    daily_amplitude=0.3,
                    noise_std=0.15,
                    expected_min=0.0,
                    expected_max=7.1,
                    spike_probability=0.0008,
                    spike_magnitude=4.5,
                    missing_probability=0.015,
                ),
                SensorSpec(
                    code="discharge_pressure",
                    name="Discharge Pressure",
                    measurement_type="pressure",
                    unit="bar",
                    baseline=6.2,
                    daily_amplitude=0.4,
                    noise_std=0.1,
                    expected_min=3.0,
                    expected_max=9.0,
                    missing_probability=0.008,
                ),
            ],
        ),
        AssetSpec(
            code="COMP-12",
            name="Rotary Compressor 12 (Synthetic)",
            asset_type="rotary_compressor",
            location="Building B — Air Systems",
            sensors=[
                SensorSpec(
                    code="motor_temp",
                    name="Motor Winding Temperature",
                    measurement_type="temperature",
                    unit="celsius",
                    baseline=71.0,
                    daily_amplitude=4.0,
                    noise_std=0.9,
                    expected_min=25.0,
                    expected_max=105.0,
                    spike_probability=0.0005,
                    spike_magnitude=15.0,
                    missing_probability=0.012,
                ),
                SensorSpec(
                    code="output_pressure",
                    name="Output Pressure",
                    measurement_type="pressure",
                    unit="bar",
                    baseline=8.5,
                    daily_amplitude=0.6,
                    noise_std=0.2,
                    expected_min=5.0,
                    expected_max=11.5,
                    missing_probability=0.01,
                ),
                SensorSpec(
                    code="shaft_rpm",
                    name="Shaft Speed",
                    measurement_type="rotational_speed",
                    unit="rpm",
                    baseline=2950.0,
                    daily_amplitude=25.0,
                    noise_std=8.0,
                    expected_min=2600.0,
                    expected_max=3200.0,
                    missing_probability=0.01,
                ),
            ],
        ),
        AssetSpec(
            code="CONV-03",
            name="Conveyor Motor 03 (Synthetic)",
            asset_type="conveyor_motor",
            location="Building A — Packaging Line",
            sensors=[
                SensorSpec(
                    code="motor_current",
                    name="Motor Current",
                    measurement_type="current",
                    unit="amps",
                    baseline=14.2,
                    daily_amplitude=2.5,
                    noise_std=0.4,
                    expected_min=5.0,
                    expected_max=22.0,
                    spike_probability=0.001,
                    spike_magnitude=6.0,
                    missing_probability=0.02,
                ),
                SensorSpec(
                    code="belt_temp",
                    name="Belt Surface Temperature",
                    measurement_type="temperature",
                    unit="celsius",
                    baseline=34.0,
                    daily_amplitude=2.0,
                    noise_std=0.5,
                    expected_min=10.0,
                    expected_max=60.0,
                    missing_probability=0.01,
                ),
            ],
        ),
    ]


def _generate_sensor_series(
    rng: np.random.Generator, sensor: SensorSpec, timestamps: list[datetime]
) -> list[tuple[datetime, float | None]]:
    n = len(timestamps)
    t_hours = np.array([(ts - timestamps[0]).total_seconds() / 3600.0 for ts in timestamps])
    t_days = t_hours / 24.0

    daily_cycle = sensor.daily_amplitude * np.sin(2 * np.pi * t_hours / 24.0)
    drift = sensor.drift_per_day * t_days
    noise = rng.normal(0.0, sensor.noise_std, size=n)
    values = sensor.baseline + daily_cycle + drift + noise

    # Sudden spikes / step-change anomalies (synthetic — not a labeled real fault).
    spike_mask = rng.random(n) < sensor.spike_probability
    spike_signs = rng.choice([-1.0, 1.0], size=n)
    # Guarantee one spike in the most recent 15% of the series (a detector trained on the older history then always
    # has something to find in the newest data, which is what the demo and the acceptance script score). Placed at a
    # fixed position with no RNG draw, so every other generated value is identical to before.
    guaranteed_idx: int | None = None
    if sensor.spike_magnitude > 0 and n >= 40 and not spike_mask[int(n * 0.85):].any():
        guaranteed_idx = n - max(12, n // 20)
        spike_mask[guaranteed_idx] = True
        spike_signs[guaranteed_idx] = 1.0
    values = values + spike_mask * spike_signs * sensor.spike_magnitude

    missing_mask = rng.random(n) < sensor.missing_probability
    if guaranteed_idx is not None:
        missing_mask[guaranteed_idx] = False

    out: list[tuple[datetime, float | None]] = []
    for i, ts in enumerate(timestamps):
        out.append((ts, None if missing_mask[i] else round(float(values[i]), 3)))
    return out


def _apply_outage_window(
    series: list[tuple[datetime, float | None]], rng: np.random.Generator, min_gap_samples: int, max_gap_samples: int
) -> list[tuple[datetime, float | None]]:
    """Blanks out one contiguous window per series to guarantee at least
    one real DATA_GAP for the data-quality demo, on top of the randomly
    scattered missing_probability dropouts."""
    if len(series) < max_gap_samples + 10:
        return series
    gap_len = int(rng.integers(min_gap_samples, max_gap_samples + 1))
    start_idx = int(rng.integers(len(series) // 4, (3 * len(series)) // 4))
    out = list(series)
    for i in range(start_idx, min(start_idx + gap_len, len(out))):
        ts, _ = out[i]
        out[i] = (ts, None)
    return out


def generate_dataset(
    *,
    seed: int = 42,
    days: int = 21,
    interval_seconds: int = 300,
    end_time: datetime | None = None,
) -> dict[str, Any]:
    """Returns a dict with `assets`, `data_source_name`, and `records`
    (canonical ingestion-format dicts, ready for IngestionRequest/
    ingestion_service.ingest_records). Deterministic for a fixed seed."""
    rng = np.random.default_rng(seed)
    end_time = end_time or datetime.now(timezone.utc).replace(microsecond=0)
    start_time = end_time - timedelta(days=days)

    n_samples = int((end_time - start_time).total_seconds() // interval_seconds) + 1
    timestamps = [start_time + timedelta(seconds=i * interval_seconds) for i in range(n_samples)]

    assets = default_scenario()
    records: list[dict[str, Any]] = []
    generation_notes: list[str] = []

    for asset in assets:
        for sensor in asset.sensors:
            series = _generate_sensor_series(rng, sensor, timestamps)
            series = _apply_outage_window(
                series,
                rng,
                min_gap_samples=max(6, int(3600 * 2 / interval_seconds)),
                max_gap_samples=max(12, int(3600 * 6 / interval_seconds)),
            )
            dropped = sum(1 for _, v in series if v is None)
            generation_notes.append(
                f"{asset.code}/{sensor.code}: {len(series) - dropped} readings, {dropped} missing/gap samples"
            )
            for ts, value in series:
                if value is None:
                    continue  # missing data is represented by ABSENCE, not a null record
                records.append(
                    {
                        "asset_code": asset.code,
                        "sensor_code": sensor.code,
                        "timestamp": ts.isoformat(),
                        "value": value,
                        "unit": sensor.unit,
                    }
                )

    # A small number of deliberate duplicates, to demonstrate dedup handling
    # end to end. Picked deterministically from already-generated records.
    if records:
        dup_indices = rng.choice(len(records), size=min(15, len(records)), replace=False)
        duplicates = [dict(records[i]) for i in dup_indices]
        records.extend(duplicates)
        generation_notes.append(f"injected {len(duplicates)} duplicate records for dedup testing")

    return {
        "organization_name": SYNTHETIC_ORG_NAME,
        "data_source_name": SYNTHETIC_DATA_SOURCE_NAME,
        "assets": assets,
        "records": records,
        "seed": seed,
        "start_time": start_time,
        "end_time": end_time,
        "interval_seconds": interval_seconds,
        "generation_notes": generation_notes,
    }
