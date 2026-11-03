"""CLI entry point to load the synthetic demo dataset.

Usage (from backend/, with the venv active and migrations applied):
    python -m app.synthetic.cli --seed 42 --days 21

Idempotent: re-running with the same seed reuses the existing organization/
assets/sensors and re-ingests the same records — duplicates will be
correctly detected and reported rather than double-counted, which is
itself a live demonstration of the dedup logic.
"""
import argparse
import sys
from datetime import datetime, timezone

from app.core.logging import configure_logging
from app.db.session import SessionLocal
from app.models.asset import Asset
from app.models.data_source import DataSourceType
from app.models.organization import Organization
from app.models.sensor import Sensor
from app.models.user import User
from app.services import auth_service, ingestion_service
from app.synthetic.generator import SYNTHETIC_DATA_SOURCE_NAME, generate_dataset

DEMO_ADMIN_EMAIL = "admin@riverbend-demo.io"
DEMO_ADMIN_PASSWORD = "ChangeMe123!"  # noqa: S105 — demo-only credential, documented in SETUP.md
INGEST_CHUNK_SIZE = 1500


def _get_or_create_org_and_admin(db, organization_name: str) -> tuple[Organization, User]:
    org = db.query(Organization).filter(Organization.name == organization_name).first()
    if org:
        admin = db.query(User).filter(User.organization_id == org.id, User.email == DEMO_ADMIN_EMAIL).first()
        if admin is None:
            raise RuntimeError(
                f"Organization '{organization_name}' exists but the demo admin user is missing. "
                "This should not happen outside manual DB edits."
            )
        return org, admin
    org, admin = auth_service.register_organization(
        db,
        organization_name=organization_name,
        admin_email=DEMO_ADMIN_EMAIL,
        admin_password=DEMO_ADMIN_PASSWORD,
        admin_full_name="Demo Administrator",
    )
    return org, admin


def _get_or_create_assets(db, org: Organization, asset_specs) -> dict[str, Asset]:
    result: dict[str, Asset] = {}
    for spec in asset_specs:
        asset = db.query(Asset).filter(Asset.organization_id == org.id, Asset.code == spec.code).first()
        if asset is None:
            asset = Asset(
                organization_id=org.id,
                code=spec.code,
                name=spec.name,
                asset_type=spec.asset_type,
                location=spec.location,
                description="SYNTHETIC demo asset — generated data, not a real industrial system.",
            )
            db.add(asset)
            db.flush()
        result[spec.code] = asset

        for sensor_spec in spec.sensors:
            existing = db.query(Sensor).filter(Sensor.asset_id == asset.id, Sensor.code == sensor_spec.code).first()
            if existing is None:
                db.add(
                    Sensor(
                        asset_id=asset.id,
                        code=sensor_spec.code,
                        name=sensor_spec.name,
                        measurement_type=sensor_spec.measurement_type,
                        unit=sensor_spec.unit,
                        expected_min=sensor_spec.expected_min,
                        expected_max=sensor_spec.expected_max,
                        expected_interval_seconds=sensor_spec.expected_interval_seconds,
                    )
                )
    db.commit()
    return result


def load_synthetic_dataset(*, seed: int = 42, days: int = 21, interval_seconds: int = 300) -> dict:
    dataset = generate_dataset(seed=seed, days=days, interval_seconds=interval_seconds)

    db = SessionLocal()
    try:
        org, admin = _get_or_create_org_and_admin(db, dataset["organization_name"])
        _get_or_create_assets(db, org, dataset["assets"])

        results = []
        records = dataset["records"]
        for i in range(0, len(records), INGEST_CHUNK_SIZE):
            chunk = records[i : i + INGEST_CHUNK_SIZE]
            result = ingestion_service.ingest_records(
                db,
                organization_id=org.id,
                data_source_name=dataset["data_source_name"],
                raw_records=chunk,
                submitted_by_user_id=admin.id,
            )
            results.append(result)

        return {
            "organization_id": org.id,
            "organization_name": org.name,
            "admin_email": DEMO_ADMIN_EMAIL,
            "asset_count": len(dataset["assets"]),
            "sensor_count": sum(len(a.sensors) for a in dataset["assets"]),
            "records_generated": len(records),
            "records_accepted": sum(r.records_accepted for r in results),
            "records_rejected": sum(r.records_rejected for r in results),
            "records_duplicate": sum(r.records_duplicate for r in results),
            "batches": [r.batch_id for r in results],
            "generation_notes": dataset["generation_notes"],
        }
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Load the SYNTHETIC demo dataset for the Industrial AI Predictive Intelligence Platform.")
    parser.add_argument("--seed", type=int, default=42, help="Deterministic RNG seed (default: 42)")
    parser.add_argument("--days", type=int, default=21, help="How many days of history to generate (default: 21)")
    parser.add_argument("--interval-seconds", type=int, default=300, help="Sampling interval in seconds (default: 300 = 5 minutes)")
    args = parser.parse_args()

    configure_logging("INFO")
    summary = load_synthetic_dataset(seed=args.seed, days=args.days, interval_seconds=args.interval_seconds)

    print("\n=== SYNTHETIC demo dataset loaded ===")
    print(f"Organization : {summary['organization_name']}  ({summary['organization_id']})")
    print(f"Demo login   : {summary['admin_email']} / {DEMO_ADMIN_PASSWORD}")
    print(f"Assets       : {summary['asset_count']}")
    print(f"Sensors      : {summary['sensor_count']}")
    print(f"Records      : generated={summary['records_generated']}  accepted={summary['records_accepted']}  "
          f"rejected={summary['records_rejected']}  duplicate={summary['records_duplicate']}")
    print(f"Batches      : {', '.join(summary['batches'])}")
    print("\nPer-sensor generation notes:")
    for note in summary["generation_notes"]:
        print(f"  - {note}")
    print("\nReminder: this dataset is entirely SYNTHETIC. It does not represent")
    print("a real industrial system and its anomalies are not real equipment failures.\n")


if __name__ == "__main__":
    sys.exit(main() or 0)
