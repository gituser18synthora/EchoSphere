"""Preserve fractional collector timestamps so lease rounds do not skip polls.

Revision ID: 70b9ec4d2f11
Revises: 6fa4d8c2b031
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import DATETIME

revision = "70b9ec4d2f11"
down_revision = "6fa4d8c2b031"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("monitoring_samples", "checked_at", False),
    ("monitoring_service_states", "last_seen_at", False),
    ("monitoring_collector_leases", "lease_until", False),
    ("monitoring_collector_leases", "last_success_at", True),
)
_PRECISE = sa.DateTime().with_variant(DATETIME(fsp=6), "mysql")


def upgrade():
    for table, column, nullable in _COLUMNS:
        with op.batch_alter_table(table) as batch:
            batch.alter_column(column, existing_type=sa.DateTime(), type_=_PRECISE, existing_nullable=nullable)


def downgrade():
    for table, column, nullable in reversed(_COLUMNS):
        with op.batch_alter_table(table) as batch:
            batch.alter_column(column, existing_type=_PRECISE, type_=sa.DateTime(), existing_nullable=nullable)
