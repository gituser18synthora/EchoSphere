"""Monitoring samples, collector coordination and alert ownership metadata.

Revision ID: 6fa4d8c2b031
Revises: 5e9c3a7b1d2f
"""
from alembic import op
import sqlalchemy as sa

revision = "6fa4d8c2b031"
down_revision = "5e9c3a7b1d2f"
branch_labels = None
depends_on = None


def upgrade():
    for name, kind in (
        ("service_key", sa.String(96)),
        ("acknowledged_by", sa.String(40)), ("acknowledged_at", sa.DateTime()),
        ("resolved_by", sa.String(40)), ("resolved_at", sa.DateTime()),
    ):
        op.add_column("platform_alerts", sa.Column(name, kind, nullable=True))
    op.create_table("monitoring_samples",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("service_key", sa.String(96), nullable=False),
        sa.Column("checked_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=True))
    op.create_index("ix_monitoring_samples_service_time", "monitoring_samples", ["service_key", "checked_at"])
    op.create_index("ix_monitoring_samples_time", "monitoring_samples", ["checked_at"])
    op.create_table("monitoring_service_states",
        sa.Column("service_key", sa.String(96), primary_key=True),
        sa.Column("last_status", sa.String(20), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("alert_id", sa.String(40), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False))
    op.create_table("monitoring_collector_leases",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("owner", sa.String(64), nullable=True),
        sa.Column("lease_until", sa.DateTime(), nullable=False),
        sa.Column("last_success_at", sa.DateTime(), nullable=True))


def downgrade():
    op.drop_table("monitoring_collector_leases")
    op.drop_table("monitoring_service_states")
    op.drop_index("ix_monitoring_samples_time", table_name="monitoring_samples")
    op.drop_index("ix_monitoring_samples_service_time", table_name="monitoring_samples")
    op.drop_table("monitoring_samples")
    for name in ("resolved_at", "resolved_by", "acknowledged_at", "acknowledged_by", "service_key"):
        op.drop_column("platform_alerts", name)
