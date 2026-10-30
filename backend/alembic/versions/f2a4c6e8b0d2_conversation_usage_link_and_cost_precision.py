"""Conversation → usage link, and cost precision that does not floor to zero.

Revision ID: f2a4c6e8b0d2
Revises: e7a9b1c3d5f8
Create Date: 2026-08-03

Two costing defects, both structural:

1. `conversation_sessions` had no reference to the voice session that produced
   it, while `usage_events.session_id` is keyed by exactly that. A conversation
   therefore could not be joined to the usage events it was billed from — no
   per-component breakdown, no audit, and no way to recompute a stored total.
   The link existed only inside the Mongo transcript document, which the
   control plane does not query for list or detail views.

2. `cost_usd` was `Numeric(8, 4)`, so any conversation costing less than
   0.00005 USD stored as exactly 0.0000. Real calls land there routinely: a
   6-second call measured 0.000516 USD and stored 0.0005, and cheaper ones
   stored zero — which is what made list costing look "missing". Usage events
   themselves already carry `Numeric(14, 6)`, so the conversation snapshot was
   the narrowest link in the chain. Widened to `Numeric(12, 6)` to match.

Additive and non-destructive: the new column is nullable (historical rows are
backfilled from the Mongo transcripts by
`backend/scripts/backfill_conversation_costs.py`, not by this migration), and
widening a DECIMAL preserves every stored value.
"""

import sqlalchemy as sa
from alembic import op

revision = "f2a4c6e8b0d2"
down_revision = "e7a9b1c3d5f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversation_sessions",
        sa.Column("session_id", sa.String(length=64), nullable=True),
    )
    # Costing lookups are always "the events for this conversation".
    op.create_index(
        "ix_conversations_session", "conversation_sessions", ["session_id"], unique=False
    )
    op.alter_column(
        "conversation_sessions",
        "cost_usd",
        existing_type=sa.Numeric(8, 4),
        type_=sa.Numeric(12, 6),
        existing_nullable=False,
    )


def downgrade() -> None:
    # Narrowing back rounds sub-0.0001 costs away; that is inherent to the old
    # column and is why the widening happened.
    op.alter_column(
        "conversation_sessions",
        "cost_usd",
        existing_type=sa.Numeric(12, 6),
        type_=sa.Numeric(8, 4),
        existing_nullable=False,
    )
    op.drop_index("ix_conversations_session", table_name="conversation_sessions")
    op.drop_column("conversation_sessions", "session_id")
