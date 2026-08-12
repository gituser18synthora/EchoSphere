"""Human speech naturalness configuration.

Revision ID: d0f2a4b6c8e0
Revises: c8e0a2b4d6f8
Create Date: 2026-08-11

Human Speech / Naturalness layer:
- voice_bot_settings.human_speech — per-bot overrides for the naturalness
  planner (thinking fillers, acknowledgements, backchannels, prosody
  variation, gender agreement, micro pauses, self-correction and their
  probabilities/timing). Sparse: only overridden keys are stored.
- tenant_settings.human_speech — the tenant-wide override layer between the
  platform defaults and the bot override.

Resolution order at runtime: platform defaults (code) -> tenant override ->
bot override. NULL in both columns keeps a bot on the platform defaults.

Additive and non-destructive.
"""

import sqlalchemy as sa
from alembic import op

revision = "d0f2a4b6c8e0"
down_revision = "c8e0a2b4d6f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "voice_bot_settings",
        sa.Column("human_speech", sa.JSON(), nullable=True),
    )
    op.add_column(
        "tenant_settings",
        sa.Column("human_speech", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenant_settings", "human_speech")
    op.drop_column("voice_bot_settings", "human_speech")
