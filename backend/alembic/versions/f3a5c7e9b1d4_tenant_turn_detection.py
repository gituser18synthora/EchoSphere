"""Add tenant-specific turn detection configuration.

Revision ID: f3a5c7e9b1d4
Revises: d1f3a5c7e9b2
Create Date: 2026-08-24
"""

import sqlalchemy as sa
from alembic import op

revision = "f3a5c7e9b1d4"
down_revision = "d1f3a5c7e9b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenant_settings", sa.Column("turn_detection", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("tenant_settings", "turn_detection")
