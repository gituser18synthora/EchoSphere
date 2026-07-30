"""voice provider selection columns on voice_bot_settings

Revision ID: b2e4f6a8c0d2
Revises: d07bfc775dfa
Create Date: 2026-07-16

Additive only — NULL means "use the platform default provider".
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2e4f6a8c0d2"
down_revision: Union[str, None] = "d07bfc775dfa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = (
    ("stt_provider", sa.String(40)),
    ("stt_model", sa.String(80)),
    ("tts_provider", sa.String(40)),
    ("tts_model", sa.String(80)),
    ("tts_voice", sa.String(80)),
    ("llm_provider", sa.String(40)),
    ("llm_model", sa.String(80)),
)


def upgrade() -> None:
    for name, type_ in _COLUMNS:
        op.add_column("voice_bot_settings", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(_COLUMNS):
        op.drop_column("voice_bot_settings", name)
