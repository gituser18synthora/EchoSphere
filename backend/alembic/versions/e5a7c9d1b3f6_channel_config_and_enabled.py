"""Channel management: provider configuration JSON + traffic enable flag.

Revision ID: e5a7c9d1b3f6
Revises: c7d9e1a3b5f4
Create Date: 2026-07-20

Additive only. `config` stores provider-specific NON-secret fields (secrets are
env: references, validated at the API layer — raw secrets never reach the DB).
`enabled` gates live traffic: webhooks reject disabled channels.
Rollback: `alembic downgrade c7d9e1a3b5f4` drops the two columns.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5a7c9d1b3f6"
down_revision: Union[str, None] = "c7d9e1a3b5f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    if not _has_column("channel_configs", "config"):
        op.add_column("channel_configs", sa.Column("config", sa.JSON, nullable=True))
    if not _has_column("channel_configs", "enabled"):
        op.add_column(
            "channel_configs",
            sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("1")),
        )


def downgrade() -> None:
    if _has_column("channel_configs", "enabled"):
        op.drop_column("channel_configs", "enabled")
    if _has_column("channel_configs", "config"):
        op.drop_column("channel_configs", "config")
