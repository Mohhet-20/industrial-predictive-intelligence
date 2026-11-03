"""anomaly detection intelligence layer

Revision ID: 27ddea19d63c
Revises: d674aa3f41c5
Create Date: 2026-08-29 21:55:49.429778

All four intelligence tables affected here (model_versions, model_runs,
anomalies, anomaly_investigations) — and predictions, which has a foreign
key into model_runs — were still empty in every environment this schema
has run in (Phase 3 is the first phase that actually writes rows into
them), so this migration drops and recreates them from scratch rather than
using SQLite's batch-alter machinery, which cannot cleanly rename/re-target
unnamed foreign key constraints. If this is ever run against a database
that already has rows in these tables, upgrade() will raise rather than
silently discard data.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '27ddea19d63c'
down_revision: Union[str, None] = 'd674aa3f41c5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_GUARD_TABLES = ["anomaly_investigations", "predictions", "anomalies", "model_runs", "model_versions"]


def _assert_empty(conn) -> None:
    for table in _GUARD_TABLES:
        count = conn.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar()
        if count:
            raise RuntimeError(
                f"Migration 27ddea19d63c drops and recreates '{table}', but it has {count} row(s). "
                "This migration was written assuming these Phase-1/2-era intelligence tables were "
                "still unused; back up the database and adapt this migration to an in-place ALTER "
                "path (or a data-preserving copy) before running it against non-empty data."
            )


def upgrade() -> None:
    conn = op.get_bind()
    _assert_empty(conn)

    op.drop_table("anomaly_investigations")
    op.drop_table("predictions")
    op.drop_table("anomalies")
    op.drop_table("model_runs")
    op.drop_table("model_versions")

    op.create_table(
        "model_versions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("detector_type", sa.Enum("ROBUST_ZSCORE", "ISOLATION_FOREST", name="detectortype", native_enum=False, length=30), nullable=False),
        sa.Column("detector_config", sa.JSON(), nullable=False),
        sa.Column("reference_period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reference_period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reference_observation_count", sa.Integer(), nullable=False),
        sa.Column("feature_schema", sa.JSON(), nullable=True),
        sa.Column("baseline_summary", sa.JSON(), nullable=True),
        sa.Column("status", sa.Enum("TRAINED", "FAILED", "ARCHIVED", name="modelversionstatus", native_enum=False, length=20), nullable=False),
        sa.Column("training_error", sa.String(length=1000), nullable=True),
        sa.Column("trained_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evaluation_results", sa.JSON(), nullable=True),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
    )

    op.create_table(
        "model_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("model_version_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.Enum("QUEUED", "RUNNING", "SUCCEEDED", "FAILED", name="modelrunstatus", native_enum=False, length=20), nullable=False),
        sa.Column("scored_period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scored_period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("background_job_id", sa.String(length=36), nullable=True),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
        sa.Column("metrics", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["model_version_id"], ["model_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["background_job_id"], ["background_jobs.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
    )

    op.create_table(
        "anomalies",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("asset_id", sa.String(length=36), nullable=False),
        sa.Column("sensor_id", sa.String(length=36), nullable=True),
        sa.Column("model_run_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.Enum("DETECTED", "UNDER_REVIEW", "ACKNOWLEDGED", "RESOLVED", "FALSE_POSITIVE", name="anomalystatus", native_enum=False, length=20), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("summary", sa.String(length=500), nullable=False),
        sa.Column("detector_type", sa.Enum("ROBUST_ZSCORE", "ISOLATION_FOREST", name="detectortype", native_enum=False, length=30), nullable=False),
        sa.Column("raw_score", sa.Float(), nullable=False),
        sa.Column("normalized_score", sa.Float(), nullable=True),
        sa.Column("threshold_used", sa.Float(), nullable=False),
        sa.Column("severity_tier", sa.Enum("OBSERVED_UNUSUAL", "CANDIDATE_ANOMALY", "HIGH_PRIORITY", name="anomalyseveritytier", native_enum=False, length=30), nullable=False),
        sa.Column("contributing_signals", sa.JSON(), nullable=True),
        sa.Column("data_quality_context", sa.JSON(), nullable=True),
        sa.Column("is_synthetic_scenario", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sensor_id"], ["sensors.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["model_run_id"], ["model_runs.id"], ondelete="CASCADE"),
    )

    op.create_table(
        "anomaly_investigations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("anomaly_id", sa.String(length=36), nullable=False),
        sa.Column("reviewed_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("from_status", sa.Enum("DETECTED", "UNDER_REVIEW", "ACKNOWLEDGED", "RESOLVED", "FALSE_POSITIVE", name="anomalystatus", native_enum=False, length=20), nullable=False),
        sa.Column("to_status", sa.Enum("DETECTED", "UNDER_REVIEW", "ACKNOWLEDGED", "RESOLVED", "FALSE_POSITIVE", name="anomalystatus", native_enum=False, length=20), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["anomaly_id"], ["anomalies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["users.id"]),
    )

    op.create_table(
        "predictions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("model_run_id", sa.String(length=36), nullable=False),
        sa.Column("asset_id", sa.String(length=36), nullable=False),
        sa.Column("sensor_id", sa.String(length=36), nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("prediction_type", sa.String(length=50), nullable=False),
        sa.ForeignKeyConstraint(["model_run_id"], ["model_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sensor_id"], ["sensors.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("predictions")
    op.drop_table("anomaly_investigations")
    op.drop_table("anomalies")
    op.drop_table("model_runs")
    op.drop_table("model_versions")

    op.create_table(
        "model_versions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("version", sa.String(length=50), nullable=False),
        sa.Column("model_type", sa.String(length=100), nullable=False),
        sa.Column("training_metadata", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "model_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("model_version_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.ForeignKeyConstraint(["model_version_id"], ["model_versions.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "anomalies",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("asset_id", sa.String(length=36), nullable=False),
        sa.Column("sensor_id", sa.String(length=36), nullable=True),
        sa.Column("model_run_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("summary", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sensor_id"], ["sensors.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["model_run_id"], ["model_runs.id"]),
    )
    op.create_table(
        "anomaly_investigations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("anomaly_id", sa.String(length=36), nullable=False),
        sa.Column("reviewed_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("conclusion", sa.String(length=200), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["anomaly_id"], ["anomalies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["users.id"]),
    )
    op.create_table(
        "predictions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("model_run_id", sa.String(length=36), nullable=False),
        sa.Column("asset_id", sa.String(length=36), nullable=False),
        sa.Column("sensor_id", sa.String(length=36), nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("prediction_type", sa.String(length=50), nullable=False),
        sa.ForeignKeyConstraint(["model_run_id"], ["model_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sensor_id"], ["sensors.id"], ondelete="CASCADE"),
    )
