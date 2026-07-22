"""Database-driven voice provider catalog + per-language voice configuration.

Revision ID: f0a1b2c3d4e5
Revises: e5a7c9d1b3f6
Create Date: 2026-07-21

Additive only:
- provider_models: per-provider model catalog (capability, languages, codecs,
  sample rates, parameter schema) — the source of truth for provider-dependent
  configuration UIs and backend validation. Seeded by base_seed.
- voice_profiles.model_codes / provider_settings: which provider models a voice
  supports and its per-voice default parameters (e.g. ElevenLabs VoiceSettings).
- voice_bot_settings: provider-specific settings JSON per capability, dedicated
  STT language, TTS fallback engine and transport audio configuration.

No secrets are stored anywhere — provider credentials remain env: references.
Rollback: `alembic downgrade e5a7c9d1b3f6`.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f0a1b2c3d4e5"
down_revision: Union[str, None] = "e5a7c9d1b3f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table)


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return column in {c["name"] for c in inspector.get_columns(table)}


def _voice_profile_columns() -> list[sa.Column]:
    return [
        sa.Column("model_codes", sa.JSON, nullable=True),
        sa.Column("provider_settings", sa.JSON, nullable=True),
    ]


def _vbs_columns() -> list[sa.Column]:
    return [
        sa.Column("stt_language", sa.String(15), nullable=True),
        sa.Column("stt_settings", sa.JSON, nullable=True),
        sa.Column("tts_settings", sa.JSON, nullable=True),
        sa.Column("llm_settings", sa.JSON, nullable=True),
        sa.Column("fallback_provider", sa.String(40), nullable=True),
        sa.Column("fallback_model", sa.String(80), nullable=True),
        sa.Column("fallback_voice", sa.String(80), nullable=True),
        sa.Column("audio_settings", sa.JSON, nullable=True),
    ]


def upgrade() -> None:
    if not _has_table("provider_models"):
        op.create_table(
            "provider_models",
            sa.Column("id", sa.String(40), primary_key=True),
            sa.Column("provider_code", sa.String(50), nullable=False),
            sa.Column("capability", sa.String(20), nullable=False),
            sa.Column("code", sa.String(80), nullable=False),
            sa.Column("display_name", sa.String(150), nullable=False),
            sa.Column("languages", sa.JSON, nullable=True),
            sa.Column("codecs", sa.JSON, nullable=True),
            sa.Column("sample_rates", sa.JSON, nullable=True),
            sa.Column("streaming", sa.Boolean, nullable=False, server_default=sa.text("1")),
            sa.Column("params_schema", sa.JSON, nullable=True),
            sa.Column("is_default", sa.Boolean, nullable=False, server_default=sa.text("0")),
            sa.Column("status", sa.String(20), nullable=False, server_default="active"),
            sa.Column("sort_order", sa.Integer, nullable=False, server_default=sa.text("0")),
            sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
            sa.Column(
                "updated_at",
                sa.DateTime,
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("created_by", sa.String(40), nullable=True),
            sa.Column("updated_by", sa.String(40), nullable=True),
            sa.Column("is_deleted", sa.Boolean, nullable=False, server_default=sa.text("0")),
            sa.Column("deleted_at", sa.DateTime, nullable=True),
            sa.Column("deleted_by", sa.String(40), nullable=True),
            sa.UniqueConstraint("provider_code", "capability", "code", name="uq_provider_model"),
        )
        op.create_index("ix_provider_models_provider_code", "provider_models", ["provider_code"])
        op.create_index("ix_provider_models_capability", "provider_models", ["capability"])
        op.create_index("ix_provider_models_status", "provider_models", ["status"])

    for col in _voice_profile_columns():
        if not _has_column("voice_profiles", col.name):
            op.add_column("voice_profiles", col)

    for col in _vbs_columns():
        if not _has_column("voice_bot_settings", col.name):
            op.add_column("voice_bot_settings", col)


def downgrade() -> None:
    for col in reversed(_vbs_columns()):
        if _has_column("voice_bot_settings", col.name):
            op.drop_column("voice_bot_settings", col.name)
    for col in reversed(_voice_profile_columns()):
        if _has_column("voice_profiles", col.name):
            op.drop_column("voice_profiles", col.name)
    if _has_table("provider_models"):
        op.drop_index("ix_provider_models_status", "provider_models")
        op.drop_index("ix_provider_models_capability", "provider_models")
        op.drop_index("ix_provider_models_provider_code", "provider_models")
        op.drop_table("provider_models")
