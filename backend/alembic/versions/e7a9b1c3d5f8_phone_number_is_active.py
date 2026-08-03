"""Phone number active flag.

Revision ID: e7a9b1c3d5f8
Revises: d6f8a0b2c4e6
Create Date: 2026-08-03

Adds `phone_numbers.is_active` — the admin gate for whether a number may take
NEW bot/channel assignments. Orthogonal to `status` (assigned / available /
porting / error), which keeps tracking the assignment lifecycle: a number can
be assigned AND inactive (existing routing preserved, new claims rejected).

Additive only; every existing number stays active, so production behavior is
unchanged until an operator deactivates a number.
"""

import sqlalchemy as sa
from alembic import op

revision = "e7a9b1c3d5f8"
down_revision = "d6f8a0b2c4e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "phone_numbers",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("phone_numbers", "is_active")
