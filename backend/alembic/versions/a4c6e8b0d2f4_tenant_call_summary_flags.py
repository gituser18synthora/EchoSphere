"""Tenant-level post-call intelligence switches.

Two independent Super Admin controls on the tenant record:
``call_summary_enabled`` gates whether the post-call summary / outcome /
Next Best Action analysis runs at all, and ``use_previous_call_summary``
gates whether a new call loads the customer's latest stored summary into
the bot context. Both default to FALSE — existing tenants keep exactly the
behavior they had before summaries existed, and stored history is never
injected without an explicit opt-in.

Revision ID: a4c6e8b0d2f4
Revises: f1b3d5a7c9e2
Create Date: 2026-08-07
"""

import sqlalchemy as sa
from alembic import op

revision = "a4c6e8b0d2f4"
down_revision = "f1b3d5a7c9e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "call_summary_enabled",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "use_previous_call_summary",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("tenants", "use_previous_call_summary")
    op.drop_column("tenants", "call_summary_enabled")
