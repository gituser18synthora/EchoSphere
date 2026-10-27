"""Per-bot identity confirmation mode on runtime_context_schemas.

Revision ID: 4d8b2f6a9c1e
Revises: f5b7d9e1a3c5
Create Date: 2026-10-27

runtime_context_schemas.identity_confirmation_mode — how a reply to the bot's
identity question ("kya meri baat Gaurav ji se ho rahi hai?") counts:
"standard" (an affirm-led reply such as "haan boliye" confirms) or "strict"
(a reply that only says go ahead gets one explicit re-ask first). NULL keeps
the domain policy's default — strict for collections, standard for every
other bot — so no existing bot changes by this migration.

Additive and non-destructive.
"""

import sqlalchemy as sa
from alembic import op

revision = "4d8b2f6a9c1e"
down_revision = "f5b7d9e1a3c5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runtime_context_schemas",
        sa.Column("identity_confirmation_mode", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("runtime_context_schemas", "identity_confirmation_mode")
