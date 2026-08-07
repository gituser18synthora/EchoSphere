"""Goal Engine configuration on voice_bot_settings.

Revision ID: d8f0b2c4e6a8
Revises: c4f6a8b0d2e4
Create Date: 2026-08-06

Agentic conversation architecture:
- voice_bot_settings.goal_policy — the bot's authored Goal Engine
  configuration (role, domain, goals, allowed/restricted topics, identity
  policy, slots, tool rules, escalation, completion criteria, tone,
  out-of-scope handling, safety). NULL keeps every existing bot on the safe
  default derived at runtime from its published prompt, intents and
  runtime-context domain policy — no behavior change without opting in.

Additive and non-destructive.
"""

import sqlalchemy as sa
from alembic import op

revision = "d8f0b2c4e6a8"
down_revision = "c4f6a8b0d2e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "voice_bot_settings",
        sa.Column("goal_policy", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("voice_bot_settings", "goal_policy")
